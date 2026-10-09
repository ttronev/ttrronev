"""
shared/structure_analyzer.py — BOS engine.

Single rule: BOS fires ONLY when a candle closes beyond the LOCKED extreme
of the current swing. Locked extremes are set at the moment of a flip or
continuation event and never update from in-swing pivots — except a single
ratchet to the next confirmed same-direction pivot immediately after the
event. After that ratchet they are FIXED until the next BOS.

Per-swing state
---------------
    locked_sH   the close-above level that fires a bullish event
    locked_sL   the close-below level that fires a bearish event
                (both immutable inside the swing except for the post-event ratchet)

    tracking_LL   running min of bar lows in a downtrend  (descriptive only)
    tracking_HH   running max of bar highs in an uptrend (descriptive only)

    highest_H_in_swing   highest CONFIRMED pivot H price within the swing
                          → continuation carryover for new locked_sH
    lowest_L_in_swing    lowest CONFIRMED pivot L price within the swing
                          → continuation carryover for new locked_sL in uptrend
    latest_H_in_swing    most recent CONFIRMED pivot H within the swing
                          → flip carryover for new locked_sH on bos_down from uptrend
    latest_L_in_swing    most recent CONFIRMED pivot L within the swing
                          → flip carryover for new locked_sL on bos_up from downtrend

API
---
The analyzer is **stateful**. Process bars one at a time via `step()`,
or call `analyze(df)` to loop over a DataFrame (backwards-compat).

    a = StructureAnalyzer(reversal_threshold_pct=0.005, init_bars=50)
    state = a.step(ts, high, low, close)   # one bar
    states = a.analyze(df)                 # entire df, list[StructureState]

Both paths produce mathematically identical event streams on identical
input. See `archive/paper_trade/REFACTOR_C1_DESIGN.md` for the proof.

Persistence: `serialize() -> dict` and `from_state(d, cfg) -> Analyzer`
support checkpoint/restore. JSON-safe.

Init handling
-------------
* `analyze(df)` (batch path): pre-computes locked_sH = max(highs[:init_bars])
  and locked_sL = min(lows[:init_bars]) BEFORE the loop, then loops step().
  Per-bar StructureState during init bars uses the pre-set seed value —
  byte-identical to the historical analyze() implementation.
* `step()` in streaming mode: maintains a running max/min during the first
  `init_bars` calls, finalizing at call # init_bars. BOS detection is NOT
  gated on init completion — the math (close ≤ running-max-of-highs)
  guarantees no BOS can fire during init regardless. After init, behavior
  is identical to the batch path.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Callable, Optional

import numpy as np
import pandas as pd


DEFAULT_REVERSAL_PCT_BY_TF: dict[str, float] = {
    "1m":  0.0030,
    "5m":  0.0050,
    "15m": 0.0080,
    "30m": 0.0120,
    "1h":  0.0200,
    "4h":  0.0350,
    "1d":  0.0500,
}

DEFAULT_INIT_BARS_BY_TF: dict[str, int] = {
    "1m":  50,
    "5m":  50,
    "15m": 30,
    "30m": 25,
    "1h":  20,
    "4h":  20,
    "1d":  20,
}


@dataclass
class StructureState:
    """Per-bar snapshot."""
    state: str                     # 'uptrend' | 'downtrend' | 'undetermined'
    swing_high: float              # = locked_sH
    swing_high_idx: int
    swing_low: float               # = locked_sL
    swing_low_idx: int
    last_event: str                # 'init' | 'bos_up' | 'bos_down' | 'failed_test_high' | 'failed_test_low' | 'none'
    failed_test_count: int
    last_bos_idx: int              # bar idx of the last BOS event (-1 if none)
    last_bos_level: float          # price level that was broken at last BOS (NaN if none)
    tracking_extreme: float        # tracking_LL in downtrend, tracking_HH in uptrend
    highest_H_in_swing: float
    lowest_L_in_swing: float
    latest_H_in_swing_price: float
    latest_L_in_swing_price: float


class StructureAnalyzer:
    """BOS engine implementing the locked-extreme rule. See module docstring."""

    def __init__(
        self,
        reversal_threshold_pct: float = 0.005,
        init_bars: int = 50,
        log_failed_test: Optional[Callable[[dict], None]] = None,
    ):
        if reversal_threshold_pct <= 0:
            raise ValueError("reversal_threshold_pct must be > 0")
        self.reversal_pct = float(reversal_threshold_pct)
        self.init_bars = int(init_bars)
        self._log_failed_test = log_failed_test
        self._reset_state()

    # -- state lifecycle -----------------------------------------------------

    def _reset_state(self) -> None:
        # Locked extremes start at sentinels; they're either pre-seeded by
        # the analyze() wrapper or grown by running max/min in step()
        # during the first init_bars calls.
        self._locked_sH: float = float("-inf")
        self._locked_sH_idx: int = -1
        self._locked_sL: float = float("inf")
        self._locked_sL_idx: int = -1
        self._state: str = "undetermined"

        # Zigzag active extremes (sentinels chosen so first comparison is True).
        self._zz_dir: int = 0
        self._active_high_price: float = float("-inf")
        self._active_high_idx: int = -1
        self._active_low_price: float = float("inf")
        self._active_low_idx: int = -1

        # Per-swing trackers.
        self._tracking_LL: float = float("inf")
        self._tracking_LL_idx: int = -1
        self._tracking_HH: float = float("-inf")
        self._tracking_HH_idx: int = -1
        self._highest_H_price: float = float("-inf")
        self._highest_H_idx: int = -1
        self._lowest_L_price: float = float("inf")
        self._lowest_L_idx: int = -1
        self._latest_H_price: float = float("nan")
        self._latest_H_idx: int = -1
        self._latest_L_price: float = float("nan")
        self._latest_L_idx: int = -1

        # Ratchet state.
        self._sH_ratchet_pending: bool = False
        self._sL_ratchet_pending: bool = False

        # Last event / counters.
        self._last_event: str = "init"
        self._failed_test_count: int = 0
        self._last_bos_idx: int = -1
        self._last_bos_level: float = float("nan")

        # Bar counter (monotonic).
        self._n: int = 0

        # Init seed status. Either pre-set by analyze()'s wrapper, or
        # finalized by step() at the init_bars-th call.
        self._init_seeded: bool = False

        # Track timestamps so failed-test logger can include them.
        self._last_ts = None

    def _set_seed(
        self,
        locked_sH: float, locked_sH_idx: int,
        locked_sL: float, locked_sL_idx: int,
    ) -> None:
        """Pre-seed locked extremes BEFORE any step() call. Used by the
        analyze() wrapper to preserve byte-identity with historical
        per-bar StructureState during init bars."""
        self._locked_sH = float(locked_sH)
        self._locked_sH_idx = int(locked_sH_idx)
        self._locked_sL = float(locked_sL)
        self._locked_sL_idx = int(locked_sL_idx)
        self._init_seeded = True

    # -- core: one-bar step --------------------------------------------------

    def step(self, ts, high, low, close) -> StructureState:
        """Process one bar. Returns the per-bar StructureState.

        State persists on the instance — call repeatedly with successive
        bars in chronological order. Same-data event stream is identical
        whether bars arrive via step() (one at a time) or analyze() (loop).
        """
        i = self._n
        bh = float(high)
        bl = float(low)
        bc = float(close)
        self._last_ts = ts

        # Step 1: Update zigzag active extremes.
        if self._zz_dir in (0, +1):
            if bh > self._active_high_price:
                self._active_high_price = bh
                self._active_high_idx = i
        if self._zz_dir in (0, -1):
            if bl < self._active_low_price:
                self._active_low_price = bl
                self._active_low_idx = i

        # Step 1b: If not pre-seeded, accumulate running max/min into
        # locked_sH/sL. Finalize at bar i = init_bars - 1.
        if not self._init_seeded:
            if bh > self._locked_sH:
                self._locked_sH = bh
                self._locked_sH_idx = i
            if bl < self._locked_sL:
                self._locked_sL = bl
                self._locked_sL_idx = i
            if i + 1 >= self.init_bars:
                self._init_seeded = True

        # Step 2: Confirm pivot if the threshold-reversal triggers.
        new_pivot: Optional[tuple[str, int, float]] = None
        if self._zz_dir in (0, +1) and self._active_high_price > 0:
            if bc < self._active_high_price * (1.0 - self.reversal_pct):
                new_pivot = ("H", self._active_high_idx, self._active_high_price)
                self._active_low_price = bl
                self._active_low_idx = i
                self._zz_dir = -1
        if new_pivot is None and self._zz_dir in (0, -1) and self._active_low_price > 0:
            if bc > self._active_low_price * (1.0 + self.reversal_pct):
                new_pivot = ("L", self._active_low_idx, self._active_low_price)
                self._active_high_price = bh
                self._active_high_idx = i
                self._zz_dir = +1

        # Step 3: Apply pivot.
        if new_pivot is not None:
            kind, p_idx, p_price = new_pivot
            if kind == "H":
                if p_price > self._highest_H_price:
                    self._highest_H_price = p_price
                    self._highest_H_idx = p_idx
                self._latest_H_price = p_price
                self._latest_H_idx = p_idx
                if self._sH_ratchet_pending and p_price > self._locked_sH:
                    self._locked_sH = p_price
                    self._locked_sH_idx = p_idx
                if self._sH_ratchet_pending:
                    self._sH_ratchet_pending = False
            else:  # "L"
                if p_price < self._lowest_L_price:
                    self._lowest_L_price = p_price
                    self._lowest_L_idx = p_idx
                self._latest_L_price = p_price
                self._latest_L_idx = p_idx
                if self._sL_ratchet_pending and p_price < self._locked_sL:
                    self._locked_sL = p_price
                    self._locked_sL_idx = p_idx
                if self._sL_ratchet_pending:
                    self._sL_ratchet_pending = False

        # Step 4: Update tracking_LL / tracking_HH from this bar.
        if self._state in ("downtrend", "undetermined"):
            if bl < self._tracking_LL:
                self._tracking_LL = bl
                self._tracking_LL_idx = i
        if self._state in ("uptrend", "undetermined"):
            if bh > self._tracking_HH:
                self._tracking_HH = bh
                self._tracking_HH_idx = i

        # Step 5: BOS check.
        # While a ratchet is pending on a side, BOS on THAT side is
        # suppressed — locked level is provisional and waiting for
        # the next same-side pivot to lock it. Opposite-side BOS
        # (a flip back) is still active.
        event = "none"
        if bc > self._locked_sH and not self._sH_ratchet_pending:
            broken_level = self._locked_sH
            if self._state == "downtrend":
                if not np.isnan(self._latest_L_price):
                    new_locked_sL = self._latest_L_price
                    new_locked_sL_idx = self._latest_L_idx
                else:
                    new_locked_sL = bl
                    new_locked_sL_idx = i
                self._state = "uptrend"
            elif self._state == "uptrend":
                if self._lowest_L_price != float("inf"):
                    new_locked_sL = self._lowest_L_price
                    new_locked_sL_idx = self._lowest_L_idx
                else:
                    new_locked_sL = self._locked_sL
                    new_locked_sL_idx = self._locked_sL_idx
            else:  # undetermined
                self._state = "uptrend"
                new_locked_sL = self._locked_sL  # keep init low
                new_locked_sL_idx = self._locked_sL_idx
            self._locked_sH = bc            # provisional, will ratchet
            self._locked_sH_idx = i
            self._locked_sL = new_locked_sL
            self._locked_sL_idx = new_locked_sL_idx
            self._tracking_LL = float("inf"); self._tracking_LL_idx = -1
            self._tracking_HH = bh; self._tracking_HH_idx = i
            self._highest_H_price = float("-inf"); self._highest_H_idx = -1
            self._lowest_L_price = float("inf"); self._lowest_L_idx = -1
            self._latest_H_price = float("nan"); self._latest_H_idx = -1
            self._latest_L_price = float("nan"); self._latest_L_idx = -1
            self._sH_ratchet_pending = True
            self._sL_ratchet_pending = False
            event = "bos_up"
            self._last_bos_idx = i
            self._last_bos_level = float(broken_level)
            self._failed_test_count = 0
        elif bc < self._locked_sL and not self._sL_ratchet_pending:
            broken_level = self._locked_sL
            if self._state == "uptrend":
                if not np.isnan(self._latest_H_price):
                    new_locked_sH = self._latest_H_price
                    new_locked_sH_idx = self._latest_H_idx
                else:
                    new_locked_sH = bh
                    new_locked_sH_idx = i
                self._state = "downtrend"
            elif self._state == "downtrend":
                if self._highest_H_price != float("-inf"):
                    new_locked_sH = self._highest_H_price
                    new_locked_sH_idx = self._highest_H_idx
                else:
                    new_locked_sH = self._locked_sH
                    new_locked_sH_idx = self._locked_sH_idx
            else:  # undetermined
                self._state = "downtrend"
                new_locked_sH = self._locked_sH
                new_locked_sH_idx = self._locked_sH_idx
            self._locked_sL = bc
            self._locked_sL_idx = i
            self._locked_sH = new_locked_sH
            self._locked_sH_idx = new_locked_sH_idx
            self._tracking_LL = bl; self._tracking_LL_idx = i
            self._tracking_HH = float("-inf"); self._tracking_HH_idx = -1
            self._highest_H_price = float("-inf"); self._highest_H_idx = -1
            self._lowest_L_price = float("inf"); self._lowest_L_idx = -1
            self._latest_H_price = float("nan"); self._latest_H_idx = -1
            self._latest_L_price = float("nan"); self._latest_L_idx = -1
            self._sL_ratchet_pending = True
            self._sH_ratchet_pending = False
            event = "bos_down"
            self._last_bos_idx = i
            self._last_bos_level = float(broken_level)
            self._failed_test_count = 0
        else:
            # No BOS — check for failed test (descriptive).
            if self._state in ("uptrend", "downtrend"):
                if bh > self._locked_sH and bc <= self._locked_sH:
                    event = "failed_test_high"
                    self._failed_test_count += 1
                    self._emit_failed_test(
                        ts, "failed_test_high",
                        self._locked_sH, bh, bc, self._state, self._failed_test_count,
                    )
                elif bl < self._locked_sL and bc >= self._locked_sL:
                    event = "failed_test_low"
                    self._failed_test_count += 1
                    self._emit_failed_test(
                        ts, "failed_test_low",
                        self._locked_sL, bl, bc, self._state, self._failed_test_count,
                    )

        if event != "none":
            self._last_event = event

        # Build snapshot.
        tracking_extreme = (
            self._tracking_LL if self._state == "downtrend"
            else self._tracking_HH if self._state == "uptrend"
            else float("nan")
        )
        snapshot = StructureState(
            state=self._state,
            swing_high=self._locked_sH,
            swing_high_idx=self._locked_sH_idx,
            swing_low=self._locked_sL,
            swing_low_idx=self._locked_sL_idx,
            last_event=self._last_event,
            failed_test_count=self._failed_test_count,
            last_bos_idx=self._last_bos_idx,
            last_bos_level=self._last_bos_level,
            tracking_extreme=tracking_extreme,
            highest_H_in_swing=self._highest_H_price if self._highest_H_price != float("-inf") else float("nan"),
            lowest_L_in_swing=self._lowest_L_price if self._lowest_L_price != float("inf") else float("nan"),
            latest_H_in_swing_price=self._latest_H_price,
            latest_L_in_swing_price=self._latest_L_price,
        )

        self._n += 1
        return snapshot

    # -- backwards-compat: full-df batch ------------------------------------

    def analyze(self, df: pd.DataFrame) -> list[StructureState]:
        """Process an entire DataFrame and return per-bar StructureState
        snapshots. Resets internal state, pre-seeds the locked extremes
        from the first init_bars rows (preserving byte-identity with the
        historical batch behavior), then loops step()."""
        n = len(df)
        if n == 0:
            return []

        self._reset_state()

        high = df["high"].to_numpy(dtype=float)
        low = df["low"].to_numpy(dtype=float)
        close = df["close"].to_numpy(dtype=float)
        ts = df["timestamp"].to_numpy() if "timestamp" in df.columns else np.arange(n)

        # Pre-seed locked extremes from first init_bars rows.
        init_n = min(self.init_bars, n)
        self._set_seed(
            locked_sH=float(np.max(high[:init_n])),
            locked_sH_idx=int(np.argmax(high[:init_n])),
            locked_sL=float(np.min(low[:init_n])),
            locked_sL_idx=int(np.argmin(low[:init_n])),
        )

        states: list[StructureState] = [None] * n  # type: ignore
        for i in range(n):
            states[i] = self.step(ts[i], high[i], low[i], close[i])
        return states

    # -- internals -----------------------------------------------------------

    def _emit_failed_test(
        self,
        ts,
        event: str,
        swing_level: float,
        wick_price: float,
        close_price: float,
        state_before: str,
        count_after: int,
    ) -> None:
        if self._log_failed_test is None:
            return
        self._log_failed_test({
            "timestamp": pd.Timestamp(ts).isoformat() if ts is not None else None,
            "event": event,
            "swing_level": float(swing_level),
            "wick_price": float(wick_price),
            "close_price": float(close_price),
            "structure_state_before": state_before,
            "failed_test_count_after": int(count_after),
        })

    # -- checkpoint ----------------------------------------------------------

    def serialize(self) -> dict:
        """JSON-serializable snapshot of full internal state. Pair with
        from_state() to restore."""
        def _f(v):
            # Encode special floats as strings so JSON round-trips.
            if isinstance(v, float):
                if np.isnan(v):
                    return "__nan__"
                if np.isposinf(v):
                    return "__inf__"
                if np.isneginf(v):
                    return "__-inf__"
            return v
        return {
            "schema_version": 1,
            "cfg": {
                "reversal_threshold_pct": self.reversal_pct,
                "init_bars": self.init_bars,
            },
            "locked_sH": _f(self._locked_sH),
            "locked_sH_idx": self._locked_sH_idx,
            "locked_sL": _f(self._locked_sL),
            "locked_sL_idx": self._locked_sL_idx,
            "state": self._state,
            "zz_dir": self._zz_dir,
            "active_high_price": _f(self._active_high_price),
            "active_high_idx": self._active_high_idx,
            "active_low_price": _f(self._active_low_price),
            "active_low_idx": self._active_low_idx,
            "tracking_LL": _f(self._tracking_LL),
            "tracking_LL_idx": self._tracking_LL_idx,
            "tracking_HH": _f(self._tracking_HH),
            "tracking_HH_idx": self._tracking_HH_idx,
            "highest_H_price": _f(self._highest_H_price),
            "highest_H_idx": self._highest_H_idx,
            "lowest_L_price": _f(self._lowest_L_price),
            "lowest_L_idx": self._lowest_L_idx,
            "latest_H_price": _f(self._latest_H_price),
            "latest_H_idx": self._latest_H_idx,
            "latest_L_price": _f(self._latest_L_price),
            "latest_L_idx": self._latest_L_idx,
            "sH_ratchet_pending": self._sH_ratchet_pending,
            "sL_ratchet_pending": self._sL_ratchet_pending,
            "last_event": self._last_event,
            "failed_test_count": self._failed_test_count,
            "last_bos_idx": self._last_bos_idx,
            "last_bos_level": _f(self._last_bos_level),
            "n": self._n,
            "init_seeded": self._init_seeded,
        }

    @classmethod
    def from_state(cls, d: dict, log_failed_test=None) -> "StructureAnalyzer":
        """Restore from a prior `serialize()` output."""
        def _f(v):
            if isinstance(v, str):
                if v == "__nan__": return float("nan")
                if v == "__inf__": return float("inf")
                if v == "__-inf__": return float("-inf")
            return v
        cfg = d.get("cfg", {})
        a = cls(
            reversal_threshold_pct=cfg.get("reversal_threshold_pct", 0.005),
            init_bars=cfg.get("init_bars", 50),
            log_failed_test=log_failed_test,
        )
        a._locked_sH = _f(d["locked_sH"])
        a._locked_sH_idx = int(d["locked_sH_idx"])
        a._locked_sL = _f(d["locked_sL"])
        a._locked_sL_idx = int(d["locked_sL_idx"])
        a._state = d["state"]
        a._zz_dir = int(d["zz_dir"])
        a._active_high_price = _f(d["active_high_price"])
        a._active_high_idx = int(d["active_high_idx"])
        a._active_low_price = _f(d["active_low_price"])
        a._active_low_idx = int(d["active_low_idx"])
        a._tracking_LL = _f(d["tracking_LL"])
        a._tracking_LL_idx = int(d["tracking_LL_idx"])
        a._tracking_HH = _f(d["tracking_HH"])
        a._tracking_HH_idx = int(d["tracking_HH_idx"])
        a._highest_H_price = _f(d["highest_H_price"])
        a._highest_H_idx = int(d["highest_H_idx"])
        a._lowest_L_price = _f(d["lowest_L_price"])
        a._lowest_L_idx = int(d["lowest_L_idx"])
        a._latest_H_price = _f(d["latest_H_price"])
        a._latest_H_idx = int(d["latest_H_idx"])
        a._latest_L_price = _f(d["latest_L_price"])
        a._latest_L_idx = int(d["latest_L_idx"])
        a._sH_ratchet_pending = bool(d["sH_ratchet_pending"])
        a._sL_ratchet_pending = bool(d["sL_ratchet_pending"])
        a._last_event = d["last_event"]
        a._failed_test_count = int(d["failed_test_count"])
        a._last_bos_idx = int(d["last_bos_idx"])
        a._last_bos_level = _f(d["last_bos_level"])
        a._n = int(d["n"])
        a._init_seeded = bool(d["init_seeded"])
        return a


# -- output adapter for engines that expected the prior 5-array contract ---

def to_engine_arrays(
    states: list[StructureState],
    gate_on_bos_event: bool = False,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Convert ``StructureState`` list to the 5-tuple of numpy arrays the
    legacy engine code consumed. Maps:
        analyzer.state == "uptrend"     -> "long"
        analyzer.state == "downtrend"   -> "short"
        analyzer.state == "undetermined" -> None
    """
    n = len(states)
    state_arr = np.full(n, None, dtype=object)
    bos_idx = np.full(n, -1, dtype=np.int64)
    bos_level = np.full(n, np.nan, dtype=float)
    origin_high = np.full(n, -1, dtype=np.int64)
    origin_low = np.full(n, -1, dtype=np.int64)
    for i, s in enumerate(states):
        if s.state == "uptrend":
            mapped = "long"
        elif s.state == "downtrend":
            mapped = "short"
        else:
            mapped = None
        if gate_on_bos_event and mapped is not None and s.last_event not in ("bos_up", "bos_down"):
            mapped = None
        state_arr[i] = mapped
        bos_idx[i] = s.last_bos_idx
        bos_level[i] = s.last_bos_level
        origin_high[i] = s.swing_high_idx
        origin_low[i] = s.swing_low_idx
    return state_arr, bos_idx, bos_level, origin_high, origin_low


# -- JSONL failed-test logger -----------------------------------------------

class FailedTestLogger:
    def __init__(self, log_dir: Path, pair: str, timeframe: str):
        log_dir = Path(log_dir)
        log_dir.mkdir(parents=True, exist_ok=True)
        safe_pair = pair.replace("/", "_")
        self.path = log_dir / f"{safe_pair}_{timeframe}.jsonl"
        self.pair = pair
        self.timeframe = timeframe
        self.path.write_text("")

    def __call__(self, payload: dict) -> None:
        line = dict(payload)
        line["pair"] = self.pair
        line["timeframe"] = self.timeframe
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(line) + "\n")


__all__ = [
    "StructureState",
    "StructureAnalyzer",
    "FailedTestLogger",
    "to_engine_arrays",
    "DEFAULT_REVERSAL_PCT_BY_TF",
    "DEFAULT_INIT_BARS_BY_TF",
]
