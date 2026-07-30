"""Online learner — accumulates live trade outcomes and self-retrains.

Drop-in helper to be invoked from the live executor (or paper-trading
simulator) every time a trade closes:

    learner = OnlineLearner(pair="BTC/USDT", timeframe="15m")
    learner.record_trade_result(features=row_features, predicted_direction=1,
                                actual_net_pnl=42.5, entry_price=63100, exit_price=63380)

It logs every sample to ``ml_models/evaluation/live_feedback_<pair>_<tf>.jsonl``,
keeps a rolling state in ``ml_models/models/online_state_<pair>_<tf>.json``,
and triggers a retrain when:

  - ``MIN_SAMPLES_TO_RETRAIN`` (75) new live samples have arrived, OR
  - performance drift is detected on the most recent ``DRIFT_WINDOW`` (20)
    trades — win rate < 30% **or** total PnL < -$500.

A weekly retrain is also forced even without drift, via ``check_weekly_retrain``.

The retrain itself delegates to ``trendline_breakout_ml.run_all_timeframes``
restricted to this single (pair, timeframe). The new model is only swapped
into production if its classifier AUC improves by more than 0.02 **and** its
test-slice net PnL is at least the previous net PnL.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import joblib

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
MODELS_DIR = REPO_ROOT / "ml_models" / "models"
EVAL_DIR = REPO_ROOT / "ml_models" / "evaluation"


def _utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _pair_filename(pair: str) -> str:
    return pair.replace("/", "_")


class OnlineLearner:
    MIN_SAMPLES_TO_RETRAIN = 75
    DRIFT_WINDOW = 20
    DRIFT_WINRATE_THRESHOLD = 0.30
    DRIFT_PNL_THRESHOLD = -500.0
    WEEKLY_RETRAIN_DAYS = 7
    AUC_IMPROVEMENT_REQUIRED = 0.02

    def __init__(self, pair: str, timeframe: str):
        self.pair = pair
        self.timeframe = timeframe
        p = _pair_filename(pair)
        self.model_a_path = MODELS_DIR / f"trendline_direction_{p}_{timeframe}.pkl"
        self.model_b_path = MODELS_DIR / f"trendline_entry_{p}_{timeframe}.pkl"
        self.params_path = MODELS_DIR / f"trendline_params_{p}_{timeframe}.json"
        self.feedback_log = EVAL_DIR / f"live_feedback_{p}_{timeframe}.jsonl"
        self.state_path = MODELS_DIR / f"online_state_{p}_{timeframe}.json"

        MODELS_DIR.mkdir(parents=True, exist_ok=True)
        EVAL_DIR.mkdir(parents=True, exist_ok=True)

        self.new_samples: list[dict] = []
        self.trade_history: list[dict] = []
        self.last_retrain: str | None = None
        self.last_classifier_auc: float | None = None
        self.last_net_pnl: float | None = None
        self._load_state()

    # --- public API ------------------------------------------------------------------------

    def record_trade_result(
        self,
        features: dict,
        predicted_direction: int,
        actual_net_pnl: float,
        entry_price: float,
        exit_price: float,
    ) -> None:
        """Called by the executor / simulator when a live trade closes."""
        sample = {
            "features": features,
            "predicted_direction": int(predicted_direction),
            "actual_win": int(actual_net_pnl > 0),
            "net_pnl": float(actual_net_pnl),
            "entry_price": float(entry_price),
            "exit_price": float(exit_price),
            "timestamp": _utc_now_iso(),
        }
        self.new_samples.append(sample)
        self.trade_history.append(sample)
        self._append_feedback_log(sample)
        self._save_state()

        drift = self._check_drift()
        if drift or len(self.new_samples) >= self.MIN_SAMPLES_TO_RETRAIN:
            self._retrain(reason="drift" if drift else "sample_threshold")

    def check_weekly_retrain(self) -> None:
        """Trigger a retrain if 7+ days have passed since the last one."""
        if not self.last_retrain:
            return
        try:
            last = datetime.fromisoformat(self.last_retrain.replace("Z", "+00:00"))
        except Exception:
            return
        now = datetime.now(tz=timezone.utc)
        if (now - last).days >= self.WEEKLY_RETRAIN_DAYS:
            print(f"[{self.pair} {self.timeframe}] weekly retrain triggered")
            self._retrain(reason="weekly")

    # --- internals -------------------------------------------------------------------------

    def _check_drift(self) -> bool:
        if len(self.trade_history) < self.DRIFT_WINDOW:
            return False
        recent = self.trade_history[-self.DRIFT_WINDOW :]
        recent_wr = sum(t["actual_win"] for t in recent) / len(recent)
        recent_pnl = sum(t["net_pnl"] for t in recent)
        if recent_wr < self.DRIFT_WINRATE_THRESHOLD:
            print(
                f"DRIFT [{self.pair} {self.timeframe}]: "
                f"win rate {recent_wr:.1%} in last {self.DRIFT_WINDOW} trades — emergency retrain"
            )
            return True
        if recent_pnl < self.DRIFT_PNL_THRESHOLD:
            print(
                f"DRIFT [{self.pair} {self.timeframe}]: "
                f"net ${recent_pnl:+.0f} in last {self.DRIFT_WINDOW} trades — emergency retrain"
            )
            return True
        return False

    def _retrain(self, reason: str) -> None:
        all_feedback = self._load_all_feedback()
        if len(all_feedback) < self.MIN_SAMPLES_TO_RETRAIN:
            print(
                f"[{self.pair} {self.timeframe}] retrain skipped — "
                f"only {len(all_feedback)} samples (need {self.MIN_SAMPLES_TO_RETRAIN})"
            )
            return

        print(
            f"[{self.pair} {self.timeframe}] retraining "
            f"({len(self.new_samples)} new live samples, reason={reason})..."
        )

        # Capture old model performance for comparison.
        old_auc = self.last_classifier_auc or 0.0
        old_pnl = self.last_net_pnl if self.last_net_pnl is not None else float("-inf")

        # Delegate to the main pipeline restricted to this (pair, timeframe).
        try:
            from .trendline_breakout_ml import run_all_timeframes
        except ImportError:
            from ml_models.training.trendline_breakout_ml import run_all_timeframes

        results = run_all_timeframes([self.pair], [self.timeframe], n_trials=50)
        rec = results.get(f"{self.pair}_{self.timeframe}", {})
        new_auc = float(rec.get("classifier_auc", 0.0) or 0.0)
        new_pnl = float(rec.get("test_net_pnl", float("-inf")) or float("-inf"))

        # Swap-in policy: classifier AUC must improve by >= AUC_IMPROVEMENT_REQUIRED
        # AND new net PnL must be at least the old.
        if new_auc - old_auc >= self.AUC_IMPROVEMENT_REQUIRED and new_pnl >= old_pnl:
            self.last_classifier_auc = new_auc
            self.last_net_pnl = new_pnl
            self.new_samples = []
            self.last_retrain = _utc_now_iso()
            self._save_state()
            print(
                f"[{self.pair} {self.timeframe}] retrain COMPLETE — "
                f"AUC {old_auc:.3f}->{new_auc:.3f}, net ${old_pnl:+.0f}->${new_pnl:+.0f}"
            )
        else:
            print(
                f"[{self.pair} {self.timeframe}] retrain produced no meaningful improvement — keeping old model "
                f"(AUC {old_auc:.3f}->{new_auc:.3f}, net ${old_pnl:+.0f}->${new_pnl:+.0f})"
            )

    # --- state I/O -------------------------------------------------------------------------

    def _load_state(self) -> None:
        if not self.state_path.exists():
            return
        try:
            state = json.loads(self.state_path.read_text())
        except Exception:
            return
        self.trade_history = state.get("trade_history", [])
        self.last_retrain = state.get("last_retrain", None)
        self.last_classifier_auc = state.get("last_classifier_auc", None)
        self.last_net_pnl = state.get("last_net_pnl", None)

    def _save_state(self) -> None:
        state = {
            "pair": self.pair,
            "timeframe": self.timeframe,
            "last_retrain": self.last_retrain,
            "last_classifier_auc": self.last_classifier_auc,
            "last_net_pnl": self.last_net_pnl,
            # cap the history we keep on disk to the last 500
            "trade_history": self.trade_history[-500:],
            "total_trades_seen": len(self.trade_history),
            "updated_at": _utc_now_iso(),
        }
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(state, indent=2))

    def _append_feedback_log(self, sample: dict) -> None:
        self.feedback_log.parent.mkdir(parents=True, exist_ok=True)
        with self.feedback_log.open("a", encoding="utf-8") as f:
            f.write(json.dumps(sample) + "\n")

    def _load_all_feedback(self) -> list[dict]:
        if not self.feedback_log.exists():
            return []
        out: list[dict] = []
        with self.feedback_log.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except Exception:
                    continue
        return out

    # --- model load helpers (used by executor) ---------------------------------------------

    def load_models(self) -> dict[str, Any]:
        out = {"model_a": None, "model_b": None, "params": None}
        if self.model_a_path.exists():
            out["model_a"] = joblib.load(self.model_a_path)
        if self.model_b_path.exists():
            out["model_b"] = joblib.load(self.model_b_path)
        if self.params_path.exists():
            try:
                out["params"] = json.loads(self.params_path.read_text())
            except Exception:
                out["params"] = None
        return out
