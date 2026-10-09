"""
backtesting/secondary_only_engine.py
====================================

v1.3 Secondary-Only engine. Primary's geometry is observed (wick to fib_1.0
must occur after arming) but no primary trade is placed. Only the secondary
is taken.

State machine — see strategies/secondary_only_v1.py docstring for the spec.

One setup at a time, with a single-slot queue (overwriting). On setup
resolution, the queued BOS is dispatched by replaying bars from the queued
BOS index forward to "catch up" the new candidate's state.

Per-trade output rows: see SecondaryRow dataclass below. Includes the v1.3
diagnostic fields ``primary_trigger_observed`` (bool) and
``primary_would_have_outcome`` ("tp" / "sl" / "still_open_at_secondary_fill").
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from shared.structure_analyzer import StructureAnalyzer


# -- Bias (logging only) ----------------------------------------------------

def compute_ema_bias(df_1h: pd.DataFrame, ema_fast: int, ema_slow: int) -> np.ndarray:
    closes = df_1h["close"].to_numpy(dtype=float)
    ema_f = pd.Series(closes).ewm(span=ema_fast, adjust=False).mean().to_numpy()
    ema_s = pd.Series(closes).ewm(span=ema_slow, adjust=False).mean().to_numpy()
    bias = np.full(len(df_1h), "none", dtype=object)
    for i in range(len(df_1h)):
        if ema_f[i] > ema_s[i]:
            bias[i] = "bullish"
        elif ema_f[i] < ema_s[i]:
            bias[i] = "bearish"
    return bias


def _to_ns_int64(ts_array: np.ndarray) -> np.ndarray:
    idx = pd.DatetimeIndex(pd.to_datetime(pd.Series(ts_array), utc=True))
    try:
        return idx.as_unit("ns").asi8
    except Exception:
        return idx.values.astype("datetime64[ns]").astype("int64")


def map_5m_to_1h_idx(ts_5m: np.ndarray, ts_1h: np.ndarray) -> np.ndarray:
    ts_5m_ns = _to_ns_int64(ts_5m)
    ts_1h_ns = _to_ns_int64(ts_1h)
    cutoff = ts_5m_ns - int(pd.Timedelta(hours=1).value)
    return np.clip(np.searchsorted(ts_1h_ns, cutoff, side="right") - 1, -1, len(ts_1h_ns) - 1)


# -- Trade row --------------------------------------------------------------

@dataclass
class SecondaryRow:
    setup_id: int
    direction: str                      # "long" / "short"  (secondary's direction)
    bos_timestamp: Any
    bos_direction: str                  # "bullish" / "bearish"

    # Swing / fibs (from finalized setup)
    swing_high_price: float
    swing_low_price: float
    # Engine-authoritative timestamps for the swing extremes. Avoids
    # post-hoc CSV scans that can pick the wrong bar when two bars share
    # a swing price (see setup 2499 audit).
    swing_high_timestamp: Any
    swing_low_timestamp: Any
    swing_size_pct: float
    fib_1_0: float
    fib_0_75: float
    fib_0_5: float
    fib_0_3: float
    fib_0: float
    fib_1_2: float

    # Setup phase timestamps
    setup_armed_timestamp: Any
    primary_trigger_timestamp: Any      # when wick to fib_1.0 was observed
    secondary_placed_timestamp: Any     # = primary_trigger_timestamp
    secondary_filled_timestamp: Any     # or None
    secondary_exit_timestamp: Any       # or None

    # Secondary trade
    entry_price: float
    sl_price: float                     # close-trigger threshold (fib_0.3)
    tp_price: float                     # wick fill (fib_1.0)
    sl_close_value: Optional[float]     # close that triggered SL (or None)
    exit_price: Optional[float]
    outcome: str                        # tp_hit / sl_hit / cancelled_pre_fill /
                                        # cancelled_pre_arm / cancelled_below_min_swing /
                                        # cancelled_by_invalidation / unfilled / open_eod /
                                        # never_armed / never_triggered

    # PnL
    r_planned: float
    r_realized: float
    position_size: float
    gross_pnl_quote: float
    fees_quote: float
    net_pnl_quote: float

    # v1.3 diagnostics
    primary_trigger_observed: bool
    primary_would_have_outcome: str     # tp / sl / still_open_at_secondary_fill / n/a

    # v1.5 hard-stop diagnostics (zero/False when feature disabled).
    hard_stop_R: float
    hard_stop_price: Optional[float]
    hard_stop_fired: bool

    # Context
    bias_1h: str
    btc_bias_1h: str
    hour_of_day_utc: int
    weekday_utc: int

    notes: str


# -- Engine -----------------------------------------------------------------

class SecondaryOnlyEngine:
    P_WAIT_BOS = "WAIT_BOS"
    P_AWAITING_RATCHET = "AWAITING_RATCHET"
    P_AWAITING_ARM = "AWAITING_ARM"
    P_AWAITING_PRIMARY_TRIG = "AWAITING_PRIMARY_TRIG"
    P_SECONDARY_PENDING = "SECONDARY_PENDING"
    P_SECONDARY_LIVE = "SECONDARY_LIVE"

    def __init__(self, cfg: dict, account_value: float = 10_000.0, verbose: bool = False):
        self.cfg = cfg
        self.account = float(account_value)
        self.risk_pct = float(cfg["risk_pct"])
        self.fee = float(cfg["taker_fee"])
        self.enforce_cap = bool(cfg.get("enforce_notional_cap", True))
        self.verbose = verbose
        self._reset_run_state()

    def _reset_run_state(self) -> None:
        """Reset all per-run state. Called by __init__ and by run()
        before each batch invocation."""
        cfg = self.cfg

        # Analyzer instance (now stateful per refactor C1).
        self._analyzer = StructureAnalyzer(
            reversal_threshold_pct=float(cfg["reversal_pct_5m"]),
            init_bars=int(cfg["init_bars"]),
        )

        # Per-bar 5m ring buffer (for queue dispatch replay; needs bars
        # from queued_bos_idx onwards). Mirrors the original run()'s
        # ts_5m / opens / highs / lows / closes arrays but populated
        # incrementally.
        self._ts_5m_list: list = []         # pd.Timestamp values
        self._open_list: list[float] = []
        self._high_list: list[float] = []
        self._low_list: list[float] = []
        self._close_list: list[float] = []
        self._states_list: list = []        # StructureState per bar (ring-buffer-aligned with 5m)
        self._n_5m: int = 0                 # absolute bar counter (== len of lists)

        # 1h state (incrementally built via append_1h).
        self._closes_1h: list[float] = []
        self._ts_1h_ns_list: list[int] = []
        self._bias_1h_list: list[str] = []   # one entry per 1h bar
        # Running EMAs for bias computation.
        ema_fast = int(cfg.get("htf_ema_fast", 12))
        ema_slow = int(cfg.get("htf_ema_slow", 21))
        self._ema_fast_alpha_1h = 2.0 / (ema_fast + 1)
        self._ema_slow_alpha_1h = 2.0 / (ema_slow + 1)
        self._ema_fast_1h: Optional[float] = None
        self._ema_slow_1h: Optional[float] = None

        # BTC 1h bias (separate stream; same EMAs).
        self._closes_btc_1h: list[float] = []
        self._ts_btc_1h_ns_list: list[int] = []
        self._bias_btc_1h_list: list[str] = []
        self._ema_fast_btc_1h: Optional[float] = None
        self._ema_slow_btc_1h: Optional[float] = None

        # Regime-gate config.
        self._regime_gate_pct = float(cfg.get("regime_gate_pct", 0.0) or 0.0)
        self._regime_lookback_h = int(cfg.get("regime_lookback_days", 90)) * 24
        self._regime_duration_bars = int(cfg.get("regime_duration_bars", 0) or 0)

        # Hard-stop config (= 0 means feature disabled).
        self._hard_stop_R = float(cfg.get("hard_stop_R", 0.0) or 0.0)

        # State machine.
        self._candidate: Optional[dict] = None
        self._queued_bos: Optional[int] = None
        self._pending_sl_exit: Optional[dict] = None
        self._next_setup_id: int = 1

        # Output accumulators.
        self._rows: list[SecondaryRow] = []
        self._agg = {
            "n_bos_events_total": 0,
            "n_bos_events_queued": 0,
            "n_bos_events_dropped_by_overwrite": 0,
            "n_bos_gated_out": 0,
            "n_setups_below_min_swing": 0,
            "n_setups_finalized": 0,
            "n_setups_armed": 0,
            "n_primary_triggers_observed": 0,
            "n_invalidated_pre_arm": 0,
            "n_invalidated_post_arm": 0,
            "n_secondary_placed": 0,
            "n_secondary_filled": 0,
            "n_secondary_cancelled_pre_fill": 0,
            "n_secondary_tp": 0,
            "n_secondary_sl": 0,
            "n_secondary_hard_stop": 0,
            "n_open_eod": 0,
        }

        # Event callback (set per-call by run() / step caller).
        self._event_cb = None

    # ----------------------------------------------------------------
    # Public step API (refactor C1)
    # ----------------------------------------------------------------

    def append_1h(self, ts, close: float) -> None:
        """Append a closed 1h bar (own pair). Updates EMAs and bias for
        any 5m bars that subsequently arrive via step()."""
        ts_ns = int(pd.Timestamp(ts).value)
        self._closes_1h.append(float(close))
        self._ts_1h_ns_list.append(ts_ns)
        # EMAs (Wilder-style aligned with pandas .ewm(adjust=False)).
        c = float(close)
        if self._ema_fast_1h is None:
            self._ema_fast_1h = c
        else:
            self._ema_fast_1h = self._ema_fast_alpha_1h * c + (1 - self._ema_fast_alpha_1h) * self._ema_fast_1h
        if self._ema_slow_1h is None:
            self._ema_slow_1h = c
        else:
            self._ema_slow_1h = self._ema_slow_alpha_1h * c + (1 - self._ema_slow_alpha_1h) * self._ema_slow_1h
        if self._ema_fast_1h > self._ema_slow_1h:
            bias = "bullish"
        elif self._ema_fast_1h < self._ema_slow_1h:
            bias = "bearish"
        else:
            bias = "none"
        self._bias_1h_list.append(bias)

    def append_btc_1h(self, ts, close: float) -> None:
        """Append a closed BTC 1h bar (for cross-asset bias logging)."""
        ts_ns = int(pd.Timestamp(ts).value)
        self._closes_btc_1h.append(float(close))
        self._ts_btc_1h_ns_list.append(ts_ns)
        c = float(close)
        if self._ema_fast_btc_1h is None:
            self._ema_fast_btc_1h = c
        else:
            self._ema_fast_btc_1h = self._ema_fast_alpha_1h * c + (1 - self._ema_fast_alpha_1h) * self._ema_fast_btc_1h
        if self._ema_slow_btc_1h is None:
            self._ema_slow_btc_1h = c
        else:
            self._ema_slow_btc_1h = self._ema_slow_alpha_1h * c + (1 - self._ema_slow_alpha_1h) * self._ema_slow_btc_1h
        if self._ema_fast_btc_1h > self._ema_slow_btc_1h:
            bias = "bullish"
        elif self._ema_fast_btc_1h < self._ema_slow_btc_1h:
            bias = "bearish"
        else:
            bias = "none"
        self._bias_btc_1h_list.append(bias)

    def step(self, ts, open_: float, high: float, low: float, close: float
             ) -> tuple[list[SecondaryRow], dict]:
        """Process one closed 5m bar. Returns (rows_resolved_this_bar,
        agg_snapshot). Rows resolved on this bar are also accumulated
        on `self._rows`. Events flow via `event_callback` set on
        instance.

        Both analyzer and engine state persist on the instance —
        repeated calls form a deterministic stream regardless of
        whether bars arrive one at a time or in a batch."""
        i = self._n_5m

        # Run analyzer on this bar; cache its state in our ring buffer.
        analyzer_state = self._analyzer.step(ts, high, low, close)

        # Append to per-bar arrays (queue-dispatch replay needs these).
        self._ts_5m_list.append(pd.Timestamp(ts))
        self._open_list.append(float(open_))
        self._high_list.append(float(high))
        self._low_list.append(float(low))
        self._close_list.append(float(close))
        self._states_list.append(analyzer_state)
        self._n_5m += 1

        rows_emitted_this_step: list[SecondaryRow] = []
        agg = self._agg

        # 0a. If a prior `intrabar_step` resolved the active candidate,
        # we may have a leftover queued BOS with `self._candidate is
        # None`. Dispatch it now so the queued setup is brought forward
        # at the earliest opportunity (parity with batch, which would
        # have dispatched on the same 5m bar that resolved the prior
        # candidate). No-op when there's no queue or when the candidate
        # is still active.
        if self._candidate is None and self._queued_bos is not None:
            self._candidate, self._queued_bos = self._dispatch_queue_step(
                i, rows_emitted_this_step,
            )
            if self._candidate is not None:
                self._next_setup_id += 1

        # 0. Process pending SL exit at this bar's open.
        if self._pending_sl_exit is not None:
            if self._candidate is None or self._candidate.get("phase") != self.P_SECONDARY_LIVE:
                self._pending_sl_exit = None  # safety
            else:
                self._close_secondary_at_sl(self._candidate, self._open_list[i],
                                             pd.Timestamp(ts), agg)
                self._emit("RESOLVED_SL", self._candidate, i)
                row = self._make_row(self._candidate)
                self._rows.append(row); rows_emitted_this_step.append(row)
                self._candidate = None
                self._pending_sl_exit = None
                self._candidate, self._queued_bos = self._dispatch_queue_step(i, rows_emitted_this_step)
                if self._candidate is not None:
                    self._next_setup_id += 1

        # 1. New BOS event detection.
        state = analyzer_state
        new_bos = (state.last_bos_idx == i and state.last_event in ("bos_up", "bos_down"))
        if new_bos:
            agg["n_bos_events_total"] += 1
            if not self._regime_gate_passes_step(i):
                agg["n_bos_gated_out"] += 1
                self._emit("BOS_GATED_OUT", None, i)
            elif self._candidate is None:
                self._candidate = self._new_candidate_step(self._next_setup_id, i, state)
                self._next_setup_id += 1
                self._emit("SETUP_OPENED", self._candidate, i)
            else:
                if self._queued_bos is not None:
                    agg["n_bos_events_dropped_by_overwrite"] += 1
                self._queued_bos = i
                agg["n_bos_events_queued"] += 1
                self._emit("BOS_QUEUED", None, i)

        # 2. Process active candidate one bar.
        if self._candidate is not None:
            resolved = self._step_candidate_at(self._candidate, i)
            if resolved == "sl_pending":
                self._pending_sl_exit = {"bar_idx": i, "trigger_close": self._close_list[i]}
            elif resolved is True:
                row = self._make_row(self._candidate)
                self._rows.append(row); rows_emitted_this_step.append(row)
                self._candidate = None
                self._candidate, self._queued_bos = self._dispatch_queue_step(i, rows_emitted_this_step)
                if self._candidate is not None:
                    self._next_setup_id += 1

        return rows_emitted_this_step, dict(agg)

    # ----------------------------------------------------------------
    # Sub-5m fill detection (1m WS path; live-only)
    # ----------------------------------------------------------------

    def intrabar_step(self, ts, high: float, low: float
                      ) -> list[tuple[str, dict]]:
        """Process a 1m bar (or any sub-5m frame) for WICK-BASED phase
        transitions on the active candidate. Does NOT advance the 5m
        index, does NOT append to ring buffers, does NOT run the
        analyzer or detect new BOS — those are 5m-only concerns.

        Returns a list of (kind, candidate_snapshot) for any
        transitions that fired. The streaming-engine wrapper consumes
        this list, dedupes against `_seen_anchors`, and emits to
        downstream sinks.

        Phase transitions covered (all wick-based, matching backtest's
        wick-fill assumption):
          * AWAITING_ARM            -> ARMED               (wick to fib_0.75)
          * AWAITING_PRIMARY_TRIG   -> PRIMARY_TRIGGERED   (wick to fib_1.0)
          * SECONDARY_PENDING       -> FILLED              (wick to fib_0.5)
          * SECONDARY_LIVE          -> RESOLVED_TP         (wick to fib_1.0)
          * SECONDARY_LIVE          -> RESOLVED_HARD_STOP  (wick to hard_stop_price; if enabled)

        Close-based transitions (invalidation past fib_0, primary SL
        past fib_1.2, secondary SL past fib_0.3) DO NOT fire from this
        method — they remain 5m-only because backtest evaluates them
        on 5m candle close, not on a 1m close.

        Same-bar fall-through is supported: if a 1m bar wicks fib_0.75
        AND fib_1.0, both ARMED and PRIMARY_TRIGGERED fire on this one
        intrabar call (analogous to step()'s same-5m-bar fall-through).
        Deduplication on the wrapper side handles the case where the
        next 5m close also satisfies the wick condition — the
        candidate's `phase` will already have advanced, so the 5m step
        is naturally idempotent.
        """
        c = self._candidate
        if c is None:
            return []
        ts_pd = pd.Timestamp(ts)
        bh = float(high); bl = float(low)
        agg = self._agg
        emitted: list[tuple[str, dict]] = []

        # Capture the candidate event-callback bridge so _emit() works
        # even when ts_5m_list[bar_idx] is out of range. We pass
        # bar_idx = self._n_5m - 1 to use the most recent 5m timestamp
        # as the anchor; the wrapper will overwrite bar_ts with the 1m
        # `ts` for fidelity. To avoid touching _emit's internals, we
        # bypass it here and append directly to `emitted`, letting the
        # wrapper translate to EngineEvent with the precise 1m ts.

        # Advance the candidate phase as far as wick-based transitions
        # allow on this single 1m frame.
        max_passes = 5  # bounded fall-through; phases monotonically advance
        for _ in range(max_passes):
            ph = c.get("phase")
            advanced = False

            # AWAITING_RATCHET / AWAITING_PRIMARY-pre-finalize use the
            # analyzer or fib computation; intrabar can't drive these.
            if ph == self.P_AWAITING_RATCHET:
                return emitted

            if ph == self.P_AWAITING_ARM:
                if self._is_armed(c, bh, bl):
                    c["setup_armed_timestamp"] = ts_pd
                    agg["n_setups_armed"] += 1
                    c["phase"] = self.P_AWAITING_PRIMARY_TRIG
                    emitted.append(("ARMED", dict(c)))
                    advanced = True
                else:
                    return emitted

            if c.get("phase") == self.P_AWAITING_PRIMARY_TRIG:
                if self._primary_triggered(c, bh, bl):
                    c["primary_trigger_observed"] = True
                    c["primary_trigger_timestamp"] = ts_pd
                    c["secondary_placed_timestamp"] = ts_pd
                    agg["n_primary_triggers_observed"] += 1
                    agg["n_secondary_placed"] += 1
                    c["phase"] = self.P_SECONDARY_PENDING
                    emitted.append(("PRIMARY_TRIGGERED", dict(c)))
                    advanced = True
                else:
                    return emitted

            if c.get("phase") == self.P_SECONDARY_PENDING:
                if self._secondary_fill_wick(c, bh, bl):
                    self._fill_secondary(c, ts_pd, agg)
                    c["primary_would_have_outcome"] = "tp"
                    c["phase"] = self.P_SECONDARY_LIVE
                    emitted.append(("FILLED", dict(c)))
                    advanced = True
                else:
                    return emitted

            if c.get("phase") == self.P_SECONDARY_LIVE:
                # Hard-stop wick first (highest priority).
                hsp = c.get("hard_stop_price")
                if hsp is not None and self._hard_stop_wick_hit(c, bh, bl, hsp):
                    self._close_secondary_at_hard_stop(c, hsp, ts_pd, agg)
                    emitted.append(("RESOLVED_HARD_STOP", dict(c)))
                    row = self._make_row(c)
                    self._rows.append(row)
                    self._candidate = None
                    return emitted
                # TP wick.
                if self._secondary_tp_wick(c, bh, bl):
                    self._close_secondary_at_tp(c, ts_pd, agg)
                    emitted.append(("RESOLVED_TP", dict(c)))
                    row = self._make_row(c)
                    self._rows.append(row)
                    self._candidate = None
                    return emitted
                return emitted

            if not advanced:
                return emitted

        return emitted

    def _new_candidate_step(self, setup_id: int, bos_idx: int, state) -> dict:
        """Build a new candidate using current engine-instance bias arrays
        (replacement for the legacy positional bias_1h_arr / idx_1h args)."""
        bias_1h_arr = np.asarray(self._bias_1h_list, dtype=object) if self._bias_1h_list else None
        ts_1h_ns_arr = np.asarray(self._ts_1h_ns_list, dtype=np.int64) if self._ts_1h_ns_list else None
        idx_1h = self._build_idx_1h_for_bar(bos_idx, ts_1h_ns_arr)
        btc_bias_arr = np.asarray(self._bias_btc_1h_list, dtype=object) if self._bias_btc_1h_list else None
        ts_btc_1h_ns_arr = np.asarray(self._ts_btc_1h_ns_list, dtype=np.int64) if self._ts_btc_1h_ns_list else None
        idx_btc_1h = self._build_idx_1h_for_bar(bos_idx, ts_btc_1h_ns_arr)
        return self._new_candidate(
            setup_id, bos_idx, pd.Timestamp(self._ts_5m_list[bos_idx]), state.last_event,
            state.swing_high, state.swing_low,
            bias_1h_arr, idx_1h, btc_bias_arr, idx_btc_1h,
        )

    def _build_idx_1h_for_bar(self, bar_5m_idx: int, ts_1h_ns_arr) -> Optional[np.ndarray]:
        """Build a faux idx_1h array for a SINGLE 5m bar (used by
        _new_candidate / _emit context — they do `idx_1h[bar_idx]`).
        Returns a length-(bar_idx+1) array where only [bar_idx] is
        meaningful; others are -1."""
        if ts_1h_ns_arr is None or len(ts_1h_ns_arr) == 0:
            return None
        ts_5m_ns = int(pd.Timestamp(self._ts_5m_list[bar_5m_idx]).value)
        cutoff = ts_5m_ns - int(pd.Timedelta(hours=1).value)
        j = int(np.clip(np.searchsorted(ts_1h_ns_arr, cutoff, side="right") - 1,
                        -1, len(ts_1h_ns_arr) - 1))
        out = np.full(bar_5m_idx + 1, -1, dtype=np.int64)
        out[bar_5m_idx] = j
        return out

    def _step_candidate_at(self, c: dict, i: int):
        """Wrapper around _step that uses engine-instance arrays."""
        return self._step(c, i,
                          self._open_list, self._high_list, self._low_list, self._close_list,
                          self._states_list, self.cfg, self._agg)

    def _regime_gate_passes_step(self, bar_5m_idx: int) -> bool:
        """Gate check against current 1h state. Computes returns_pct
        for the relevant 1h bar on demand (no full-array recompute)."""
        thr = self._regime_gate_pct
        if thr <= 0:
            return True
        if not self._ts_1h_ns_list or not self._closes_1h:
            return False
        ts_5m_ns = int(pd.Timestamp(self._ts_5m_list[bar_5m_idx]).value)
        cutoff = ts_5m_ns - int(pd.Timedelta(hours=1).value)
        ts_arr = self._ts_1h_ns_list  # list, but searchsorted needs sequence
        j = int(np.clip(np.searchsorted(ts_arr, cutoff, side="right") - 1,
                        -1, len(ts_arr) - 1))
        if j < 0:
            return False
        # Need history for the 90d lookback.
        if j < self._regime_lookback_h:
            return False
        closes = self._closes_1h
        c_lookback = closes[j - self._regime_lookback_h]
        if c_lookback <= 0:
            return False
        if self._regime_duration_bars <= 1:
            r = (closes[j] - c_lookback) / c_lookback * 100.0
            return abs(r) >= thr
        # Duration check: all bars in [j-dur+1..j] must satisfy.
        start = j - self._regime_duration_bars + 1
        if start < 0:
            return False
        for k in range(start, j + 1):
            if k < self._regime_lookback_h:
                return False
            cl = closes[k - self._regime_lookback_h]
            if cl <= 0:
                return False
            r = (closes[k] - cl) / cl * 100.0
            if abs(r) < thr:
                return False
        return True

    def _dispatch_queue_step(self, current_i: int, rows_out: list
                             ) -> tuple[Optional[dict], Optional[int]]:
        """Step-mode wrapper that uses engine-instance arrays for the
        queue-dispatch replay. Mirrors the legacy _dispatch_queue but
        sources its bars from self._open_list etc."""
        if self._queued_bos is None:
            return None, None
        q = self._queued_bos
        # Re-check regime gate at the original queued BOS bar.
        if not self._regime_gate_passes_step(q):
            self._agg["n_bos_gated_out"] += 1
            self._emit("BOS_GATED_OUT", None, q)
            return None, None
        qstate = self._states_list[q]
        new_cand = self._new_candidate_step(self._next_setup_id, q, qstate)
        self._emit("SETUP_OPENED", new_cand, q)
        # Replay bars q+1 .. current_i.
        for j in range(q + 1, current_i + 1):
            resolved = self._step_candidate_at(new_cand, j)
            if resolved == "sl_pending":
                if j + 1 <= current_i:
                    self._close_secondary_at_sl(new_cand, self._open_list[j + 1],
                                                 pd.Timestamp(self._ts_5m_list[j + 1]),
                                                 self._agg)
                    self._emit("RESOLVED_SL", new_cand, j + 1)
                    row = self._make_row(new_cand)
                    self._rows.append(row); rows_out.append(row)
                    return None, None
                else:
                    return new_cand, None
            elif resolved is True:
                row = self._make_row(new_cand)
                self._rows.append(row); rows_out.append(row)
                return None, None
        return new_cand, None

    # ----------------------------------------------------------------
    # Serialization (refactor C1 — checkpoint/restore)
    # ----------------------------------------------------------------

    def serialize(self) -> dict:
        """JSON-serializable engine state. Pair with from_state() to
        restore. Includes the StructureAnalyzer state, candidate dict,
        queue, pending SL exit, all 1h/btc state, ring buffers, agg
        counters."""
        def _f(v):
            if isinstance(v, float):
                if np.isnan(v): return "__nan__"
                if np.isposinf(v): return "__inf__"
                if np.isneginf(v): return "__-inf__"
            if isinstance(v, pd.Timestamp): return v.isoformat()
            return v

        def _enc_candidate(c):
            if c is None: return None
            out = {}
            for k, v in c.items():
                if isinstance(v, pd.Timestamp): out[k] = v.isoformat()
                elif isinstance(v, float): out[k] = _f(v)
                else: out[k] = v
            return out

        return {
            "schema_version": 1,
            "cfg_keys_used": {  # subset of cfg actually consumed; for sanity
                "regime_gate_pct": self._regime_gate_pct,
                "regime_lookback_h": self._regime_lookback_h,
                "regime_duration_bars": self._regime_duration_bars,
                "hard_stop_R": self._hard_stop_R,
            },
            "analyzer": self._analyzer.serialize(),
            "ts_5m_iso": [pd.Timestamp(t).isoformat() for t in self._ts_5m_list],
            "open_list": self._open_list,
            "high_list": self._high_list,
            "low_list": self._low_list,
            "close_list": self._close_list,
            # states are dataclasses; serialize to dicts with NaN encoding.
            "states_list": [
                {k: _f(getattr(s, k)) for k in s.__dataclass_fields__}
                for s in self._states_list
            ],
            "n_5m": self._n_5m,
            "closes_1h": self._closes_1h,
            "ts_1h_ns": self._ts_1h_ns_list,
            "bias_1h": self._bias_1h_list,
            "ema_fast_1h": _f(self._ema_fast_1h) if self._ema_fast_1h is not None else None,
            "ema_slow_1h": _f(self._ema_slow_1h) if self._ema_slow_1h is not None else None,
            "closes_btc_1h": self._closes_btc_1h,
            "ts_btc_1h_ns": self._ts_btc_1h_ns_list,
            "bias_btc_1h": self._bias_btc_1h_list,
            "ema_fast_btc_1h": _f(self._ema_fast_btc_1h) if self._ema_fast_btc_1h is not None else None,
            "ema_slow_btc_1h": _f(self._ema_slow_btc_1h) if self._ema_slow_btc_1h is not None else None,
            "candidate": _enc_candidate(self._candidate),
            "queued_bos": self._queued_bos,
            "pending_sl_exit": self._pending_sl_exit,
            "next_setup_id": self._next_setup_id,
            "agg": self._agg,
        }

    @classmethod
    def from_state(cls, d: dict, cfg: dict, account_value: float = 10_000.0,
                   verbose: bool = False) -> "SecondaryOnlyEngine":
        """Restore engine from a prior serialize() output.
        `cfg` must be the SAME cfg the engine was running with — config
        is not part of the checkpoint to keep restore explicit."""
        from shared.structure_analyzer import StructureAnalyzer, StructureState

        def _f(v):
            if isinstance(v, str):
                if v == "__nan__": return float("nan")
                if v == "__inf__": return float("inf")
                if v == "__-inf__": return float("-inf")
            return v

        e = cls(cfg, account_value=account_value, verbose=verbose)
        # Restore analyzer first (fully replaces the fresh instance).
        e._analyzer = StructureAnalyzer.from_state(d["analyzer"])
        # Restore lists.
        e._ts_5m_list = [pd.Timestamp(s) for s in d["ts_5m_iso"]]
        e._open_list = list(d["open_list"])
        e._high_list = list(d["high_list"])
        e._low_list = list(d["low_list"])
        e._close_list = list(d["close_list"])
        e._states_list = []
        for sd in d["states_list"]:
            e._states_list.append(StructureState(
                state=sd["state"],
                swing_high=_f(sd["swing_high"]),
                swing_high_idx=int(sd["swing_high_idx"]),
                swing_low=_f(sd["swing_low"]),
                swing_low_idx=int(sd["swing_low_idx"]),
                last_event=sd["last_event"],
                failed_test_count=int(sd["failed_test_count"]),
                last_bos_idx=int(sd["last_bos_idx"]),
                last_bos_level=_f(sd["last_bos_level"]),
                tracking_extreme=_f(sd["tracking_extreme"]),
                highest_H_in_swing=_f(sd["highest_H_in_swing"]),
                lowest_L_in_swing=_f(sd["lowest_L_in_swing"]),
                latest_H_in_swing_price=_f(sd["latest_H_in_swing_price"]),
                latest_L_in_swing_price=_f(sd["latest_L_in_swing_price"]),
            ))
        e._n_5m = int(d["n_5m"])
        e._closes_1h = list(d["closes_1h"])
        e._ts_1h_ns_list = list(d["ts_1h_ns"])
        e._bias_1h_list = list(d["bias_1h"])
        e._ema_fast_1h = _f(d["ema_fast_1h"]) if d["ema_fast_1h"] is not None else None
        e._ema_slow_1h = _f(d["ema_slow_1h"]) if d["ema_slow_1h"] is not None else None
        e._closes_btc_1h = list(d["closes_btc_1h"])
        e._ts_btc_1h_ns_list = list(d["ts_btc_1h_ns"])
        e._bias_btc_1h_list = list(d["bias_btc_1h"])
        e._ema_fast_btc_1h = _f(d["ema_fast_btc_1h"]) if d["ema_fast_btc_1h"] is not None else None
        e._ema_slow_btc_1h = _f(d["ema_slow_btc_1h"]) if d["ema_slow_btc_1h"] is not None else None
        # Restore candidate.
        cand = d["candidate"]
        if cand is not None:
            restored = {}
            for k, v in cand.items():
                if isinstance(v, str) and (
                    "_timestamp" in k or k in ("bos_timestamp",)
                ):
                    try:
                        restored[k] = pd.Timestamp(v)
                    except Exception:
                        restored[k] = v
                elif isinstance(v, str) and v in ("__nan__", "__inf__", "__-inf__"):
                    restored[k] = _f(v)
                else:
                    restored[k] = v
            e._candidate = restored
        else:
            e._candidate = None
        e._queued_bos = d["queued_bos"]
        e._pending_sl_exit = d["pending_sl_exit"]
        e._next_setup_id = int(d["next_setup_id"])
        e._agg = dict(d["agg"])
        return e

    def _finalize_eod_step(self) -> None:
        """End-of-data finalization. Mirrors the post-loop block in run()."""
        if self._candidate is None:
            return
        ph = self._candidate.get("phase")
        if ph == self.P_SECONDARY_LIVE:
            last_idx = self._n_5m - 1
            self._candidate["secondary_exit_timestamp"] = pd.Timestamp(self._ts_5m_list[last_idx])
            self._candidate["secondary_exit_price"] = float(self._close_list[last_idx])
            self._candidate["secondary_outcome"] = "open_eod"
            self._close_secondary_at_eod(self._candidate, self._close_list[last_idx],
                                          pd.Timestamp(self._ts_5m_list[last_idx]),
                                          self._agg)
            self._emit("OPEN_EOD", self._candidate, last_idx)
            self._rows.append(self._make_row(self._candidate))
        else:
            self._candidate["secondary_outcome"] = (
                self._candidate.get("secondary_outcome")
                or self._terminal_unfilled_label(ph)
            )
            self._rows.append(self._make_row(self._candidate))
        self._candidate = None

    # ----------------------------------------------------------------
    # Public batch API (backwards-compat wrapper)
    # ----------------------------------------------------------------

    def run(
        self,
        df_5m: pd.DataFrame,
        df_1h: Optional[pd.DataFrame] = None,
        df_btc_1h: Optional[pd.DataFrame] = None,
        event_callback=None,
    ) -> tuple[list[SecondaryRow], dict]:
        """Run the engine over `df_5m`.

        Backwards-compat wrapper that resets internal state, feeds 1h /
        btc_1h bars in chronological order, then loops `step()` over
        df_5m. Output is byte-identical to the historical batch
        implementation by construction (same per-bar logic, same state
        evolution).

        ``event_callback``: optional callable
        ``f(kind, candidate, bar_idx, bar_ts)``. Fired at each state
        transition. See `step()` docstring for event kinds. Behavior
        unchanged when None.
        """
        n = len(df_5m)
        if n == 0:
            return [], {}

        self._reset_run_state()
        self._event_cb = event_callback

        # Feed all 1h bars before any 5m bar.
        if df_1h is not None and len(df_1h) > 0:
            for _, row in df_1h.iterrows():
                self.append_1h(row["timestamp"], row["close"])
        if df_btc_1h is not None and len(df_btc_1h) > 0:
            for _, row in df_btc_1h.iterrows():
                self.append_btc_1h(row["timestamp"], row["close"])

        # Loop step() over 5m bars.
        ts_5m = pd.to_datetime(df_5m["timestamp"], utc=True).to_numpy()
        opens = df_5m["open"].to_numpy(dtype=float)
        highs = df_5m["high"].to_numpy(dtype=float)
        lows = df_5m["low"].to_numpy(dtype=float)
        closes = df_5m["close"].to_numpy(dtype=float)
        for i in range(n):
            self.step(ts_5m[i], opens[i], highs[i], lows[i], closes[i])

        # End-of-data finalize.
        self._finalize_eod_step()

        rows = self._rows
        agg = self._agg
        # Don't clear instance state — caller may inspect; but clear
        # callback ref so it doesn't leak across runs.
        self._event_cb = None
        return rows, agg

    def _emit(self, kind: str, candidate: Optional[dict], bar_idx: int) -> None:
        """Fire the optional event callback for a state transition.
        No-op when no callback was provided."""
        cb = getattr(self, "_event_cb", None)
        if cb is None:
            return
        ts = self._ts_5m_list[bar_idx] if 0 <= bar_idx < len(self._ts_5m_list) else None
        try:
            cb(kind, candidate, int(bar_idx), pd.Timestamp(ts) if ts is not None else None)
        except Exception:
            # Instrumentation must never affect engine correctness. If the
            # callback throws, swallow it (and stash for later inspection).
            self._cb_errors = getattr(self, "_cb_errors", [])
            self._cb_errors.append((kind, bar_idx))

    @staticmethod
    def _regime_gate_passes(bar_5m_idx, idx_1h, returns_pct, threshold_pct,
                            duration_bars: int = 0) -> bool:
        """v1.4: at BOS time, |90d return| >= threshold at the mapped 1h bar.
        v1.5: ALSO require that |return| >= threshold for ALL of the prior
        ``duration_bars`` 1h bars (sustained regime, not flickering).

        Returns True if the gate passes (or is disabled). False otherwise."""
        if threshold_pct <= 0 or returns_pct is None or idx_1h is None:
            return True
        j = int(idx_1h[bar_5m_idx])
        if j < 0 or j >= len(returns_pct):
            return False
        if duration_bars <= 1:
            return abs(float(returns_pct[j])) >= threshold_pct
        # Duration check: all bars in [j - duration_bars + 1 .. j] must pass.
        start = j - duration_bars + 1
        if start < 0:
            return False  # not enough history
        window = returns_pct[start:j + 1]
        return bool(np.all(np.abs(window) >= threshold_pct))

    # ---- Dispatch the queued BOS by replaying bars ----

    def _dispatch_queue(
        self, queued_bos, candidate, current_i,
        ts_5m, opens, highs, lows, closes, states, cfg, agg, next_setup_id,
        bias_1h_arr, idx_1h, btc_bias_arr, idx_btc_1h, rows,
    ):
        """If a BOS is queued, spawn a new candidate at queued_bos and replay
        bars [queued_bos+1 .. current_i] to bring its state up to current_i.
        Returns (new_candidate_or_None, new_queued_bos)."""
        if queued_bos is None:
            return None, None
        q = queued_bos
        # Re-check regime gate at original queued BOS bar (with duration).
        if not self._regime_gate_passes(
            q, self._idx_1h, self._returns_pct,
            self._regime_gate_pct, self._regime_duration_bars,
        ):
            agg["n_bos_gated_out"] += 1
            self._emit("BOS_GATED_OUT", None, q)
            return None, None
        qstate = states[q]
        new_cand = self._new_candidate(
            next_setup_id, q, pd.Timestamp(ts_5m[q]), qstate.last_event,
            qstate.swing_high, qstate.swing_low,
            bias_1h_arr, idx_1h, btc_bias_arr, idx_btc_1h,
        )
        self._emit("SETUP_OPENED", new_cand, q)
        # Replay bars from q+1 to current_i.
        for j in range(q + 1, current_i + 1):
            resolved = self._step(new_cand, j, opens, highs, lows, closes, states, cfg, agg)
            if resolved == "sl_pending":
                # Schedule exit at next bar's open during caller's main loop.
                # Since caller processes bars sequentially, we simulate:
                # at bar j+1's open the trade exits.
                if j + 1 <= current_i:
                    # Apply exit at j+1's open.
                    self._close_secondary_at_sl(new_cand, opens[j + 1], pd.Timestamp(ts_5m[j + 1]), agg)
                    self._emit("RESOLVED_SL", new_cand, j + 1)
                    rows.append(self._make_row(new_cand))
                    new_cand = None
                    return None, None
                else:
                    # Pending SL exit at the next real bar (caller will pick up).
                    return new_cand, None
            elif resolved is True:
                rows.append(self._make_row(new_cand))
                return None, None
        return new_cand, None

    # ---- Candidate construction ----

    def _new_candidate(
        self, setup_id, bos_idx, bos_ts, last_event,
        provisional_sH, provisional_sL,
        bias_1h_arr, idx_1h, btc_bias_arr, idx_btc_1h,
    ) -> dict:
        bos_dir = "bullish" if last_event == "bos_up" else "bearish"
        bias = "none"; btc_bias = "none"
        if bias_1h_arr is not None and idx_1h is not None:
            j = int(idx_1h[bos_idx])
            if j >= 0:
                bias = str(bias_1h_arr[j])
        if btc_bias_arr is not None and idx_btc_1h is not None:
            j = int(idx_btc_1h[bos_idx])
            if j >= 0:
                btc_bias = str(btc_bias_arr[j])
        return {
            "setup_id": setup_id,
            "bos_idx": bos_idx,
            "bos_timestamp": bos_ts,
            "bos_direction": bos_dir,
            "phase": self.P_AWAITING_RATCHET,
            # Provisional swings.
            "swing_high": float(provisional_sH),
            "swing_low": float(provisional_sL),
            # Authoritative pivot timestamps populated when the setup
            # finalizes after the post-BOS ratchet. None until then.
            "swing_high_timestamp": None,
            "swing_low_timestamp": None,
            "swing_size_pct": float("nan"),
            "fib_0": float("nan"),
            "fib_0_3": float("nan"),
            "fib_0_5": float("nan"),
            "fib_0_75": float("nan"),
            "fib_1_0": float("nan"),
            "fib_1_2": float("nan"),
            # Phase timestamps.
            "setup_armed_timestamp": None,
            "primary_trigger_timestamp": None,
            "secondary_placed_timestamp": None,
            "secondary_filled_timestamp": None,
            "secondary_exit_timestamp": None,
            # Secondary trade state.
            "entry_price": float("nan"),
            "sl_price": float("nan"),
            "tp_price": float("nan"),
            "sl_close_value": None,
            "exit_price": None,
            "secondary_outcome": "",
            "primary_trigger_observed": False,
            "primary_would_have_outcome": "n/a",
            "r_planned": float("nan"),
            "r_realized": 0.0,
            "position_size": 0.0,
            "fees_quote": 0.0,
            "gross_pnl_quote": 0.0,
            # Context.
            "bias_1h": bias,
            "btc_bias_1h": btc_bias,
            "hour_of_day_utc": int(pd.Timestamp(bos_ts).hour),
            "weekday_utc": int(pd.Timestamp(bos_ts).weekday()),
        }

    # ---- Per-bar state machine step ----

    def _step(self, c, i, opens, highs, lows, closes, states, cfg, agg):
        """Process one bar against candidate c. Returns:
            True   — setup resolved (row should be appended)
            "sl_pending" — SL trigger close just printed; exit at next bar open
            None / False — still active, continue
        """
        bh = float(highs[i]); bl = float(lows[i]); bc = float(closes[i])
        ts = pd.Timestamp(states[i].last_bos_idx) if False else None  # not used directly

        ph = c["phase"]
        bdir = c["bos_direction"]

        # AWAITING_RATCHET: detect engine ratchet.
        if ph == self.P_AWAITING_RATCHET:
            state = states[i]
            ratchet_done = False
            if bdir == "bearish":
                if state.swing_low != c["swing_low"]:
                    ratchet_done = True
            else:
                if state.swing_high != c["swing_high"]:
                    ratchet_done = True
            if ratchet_done:
                c["swing_high"] = float(state.swing_high)
                c["swing_low"] = float(state.swing_low)
                # Store engine-authoritative timestamps for the swing
                # extremes. The analyzer's state.swing_high_idx /
                # swing_low_idx point to the actual pivot bars in the
                # 5m history. Resolves ambiguity when two bars share a
                # swing price (see setup 2499 audit for the pathology).
                sH_idx = int(state.swing_high_idx)
                sL_idx = int(state.swing_low_idx)
                if 0 <= sH_idx < len(self._ts_5m_list):
                    c["swing_high_timestamp"] = pd.Timestamp(self._ts_5m_list[sH_idx])
                else:
                    c["swing_high_timestamp"] = None
                if 0 <= sL_idx < len(self._ts_5m_list):
                    c["swing_low_timestamp"] = pd.Timestamp(self._ts_5m_list[sL_idx])
                else:
                    c["swing_low_timestamp"] = None
                self._compute_fibs(c, cfg)
                if c["swing_size_pct"] < float(cfg["min_swing_pct"]):
                    agg["n_setups_below_min_swing"] += 1
                    c["secondary_outcome"] = "cancelled_below_min_swing"
                    self._emit("CANCELLED_BELOW_MIN_SWING", c, i)
                    return True
                agg["n_setups_finalized"] += 1
                c["phase"] = self.P_AWAITING_ARM
                ph = c["phase"]
                self._emit("FINALIZED", c, i)
            else:
                return None

        # AWAITING_ARM
        if ph == self.P_AWAITING_ARM:
            # Invalidation: close beyond fib_0.
            if self._invalid_pre_trigger(c, bc):
                agg["n_invalidated_pre_arm"] += 1
                c["secondary_outcome"] = "cancelled_by_invalidation"
                self._emit("CANCELLED_BY_INVALIDATION", c, i)
                return True
            # Arming check (wick to fib_0.75).
            if self._is_armed(c, bh, bl):
                c["setup_armed_timestamp"] = pd.Timestamp(self._current_ts(states, i))
                agg["n_setups_armed"] += 1
                c["phase"] = self.P_AWAITING_PRIMARY_TRIG
                ph = c["phase"]
                self._emit("ARMED", c, i)
                # Same bar can also satisfy primary trigger — fall through.
            else:
                return None

        # AWAITING_PRIMARY_TRIG
        if ph == self.P_AWAITING_PRIMARY_TRIG:
            # Invalidation: close beyond fib_0.
            if self._invalid_pre_trigger(c, bc):
                agg["n_invalidated_post_arm"] += 1
                c["secondary_outcome"] = "cancelled_by_invalidation"
                self._emit("CANCELLED_BY_INVALIDATION", c, i)
                return True
            # Primary trigger: wick to fib_1.0.
            if self._primary_triggered(c, bh, bl):
                c["primary_trigger_observed"] = True
                c["primary_trigger_timestamp"] = pd.Timestamp(self._current_ts(states, i))
                c["secondary_placed_timestamp"] = c["primary_trigger_timestamp"]
                agg["n_primary_triggers_observed"] += 1
                agg["n_secondary_placed"] += 1
                c["phase"] = self.P_SECONDARY_PENDING
                ph = c["phase"]
                self._emit("PRIMARY_TRIGGERED", c, i)
                # Same bar may also fill secondary OR cancel it. Fall through.
            else:
                return None

        # SECONDARY_PENDING
        if ph == self.P_SECONDARY_PENDING:
            # Cancel: close past fib_1.2 (would-be primary SL).
            if self._primary_sl_close_beyond(c, bc):
                c["primary_would_have_outcome"] = "sl"
                c["secondary_outcome"] = "cancelled_pre_fill"
                agg["n_secondary_cancelled_pre_fill"] += 1
                self._emit("CANCELLED_PRE_FILL", c, i)
                return True
            # Secondary fill: wick to fib_0.5.
            if self._secondary_fill_wick(c, bh, bl):
                self._fill_secondary(c, pd.Timestamp(self._current_ts(states, i)), agg)
                c["primary_would_have_outcome"] = "tp"   # primary's TP = fib_0.5 = secondary entry
                c["phase"] = self.P_SECONDARY_LIVE
                ph = c["phase"]
                self._emit("FILLED", c, i)
                # Same bar may TP or SL secondary. Fall through.
            else:
                return None

        # SECONDARY_LIVE
        if ph == self.P_SECONDARY_LIVE:
            # 1. Hard-stop wick (highest priority). If price wicks to
            # the hard-stop level intrabar, exit at that level. Whether
            # the bar later TPs is moot — by convention we assume the
            # adverse move came first (worst-case for the trader; the
            # standard close-vs-wick ambiguity convention).
            hsp = c.get("hard_stop_price")
            if hsp is not None and self._hard_stop_wick_hit(c, bh, bl, hsp):
                self._close_secondary_at_hard_stop(c, hsp,
                    pd.Timestamp(self._current_ts(states, i)), agg)
                self._emit("RESOLVED_HARD_STOP", c, i)
                return True
            # 2. Close-based SL: close beyond fib_0.3 (direction-aware).
            if self._secondary_sl_close(c, bc):
                c["sl_close_value"] = float(bc)
                # SL exit happens at NEXT bar's open; emit RESOLVED_SL there.
                return "sl_pending"
            # 3. TP: wick to fib_1.0.
            if self._secondary_tp_wick(c, bh, bl):
                self._close_secondary_at_tp(c, pd.Timestamp(self._current_ts(states, i)), agg)
                self._emit("RESOLVED_TP", c, i)
                return True
            return None

        return None

    @staticmethod
    def _current_ts(states, i):
        # Best-effort timestamp from analyzer state ordering. The analyzer
        # state itself doesn't carry per-bar timestamps; we rely on the
        # caller to pass through ts_5m elsewhere. Since this helper isn't
        # used anywhere that requires precise correctness (only for
        # diagnostic timestamps), return None.
        return None

    # ---- Fib + condition helpers ----

    def _compute_fibs(self, c: dict, cfg: dict) -> None:
        sH = c["swing_high"]; sL = c["swing_low"]
        rng = sH - sL
        denom = max(sH, sL)
        c["swing_size_pct"] = round((rng / denom) * 100.0, 4) if denom > 0 else 0.0
        if c["bos_direction"] == "bearish":
            c["fib_0"] = float(sL)
            c["fib_0_3"] = float(sL + 0.3 * rng)
            c["fib_0_5"] = float(sL + 0.5 * rng)
            c["fib_0_75"] = float(sL + 0.75 * rng)
            c["fib_1_0"] = float(sH)
            c["fib_1_2"] = float(sL + 1.2 * rng)
            # Secondary (long) entry/SL/TP.
            c["entry_price"] = c["fib_0_5"]
            c["sl_price"] = c["fib_0_3"]
            c["tp_price"] = c["fib_1_0"]
            c["secondary_direction"] = "long"
        else:  # bullish
            c["fib_0"] = float(sH)
            c["fib_0_3"] = float(sH - 0.3 * rng)
            c["fib_0_5"] = float(sH - 0.5 * rng)
            c["fib_0_75"] = float(sH - 0.75 * rng)
            c["fib_1_0"] = float(sL)
            c["fib_1_2"] = float(sH - 1.2 * rng)
            c["entry_price"] = c["fib_0_5"]
            c["sl_price"] = c["fib_0_3"]
            c["tp_price"] = c["fib_1_0"]
            c["secondary_direction"] = "short"
        # Planned R = |TP-entry| / |entry-SL|.
        risk = abs(c["entry_price"] - c["sl_price"])
        reward = abs(c["tp_price"] - c["entry_price"])
        c["r_planned"] = round(reward / risk, 3) if risk > 0 else 0.0
        # Hard-stop wick price (added on top of close-based SL). When
        # `hard_stop_R = 0` this is None and the engine ignores it.
        # `hard_stop_R = 1.0` puts the wick stop at the close-based
        # trigger level (fib_0.3); higher R values place it further
        # below entry (deeper backstop, less likely to fire).
        r_h = float(getattr(self, "_hard_stop_R", 0.0))
        if r_h > 0 and risk > 0:
            if c["bos_direction"] == "bearish":
                # Long secondary: hard stop sits below entry.
                c["hard_stop_price"] = c["entry_price"] - r_h * risk
            else:
                # Short secondary: hard stop sits above entry.
                c["hard_stop_price"] = c["entry_price"] + r_h * risk
            c["hard_stop_R"] = r_h
        else:
            c["hard_stop_price"] = None
            c["hard_stop_R"] = 0.0
        c["hard_stop_fired"] = False

    @staticmethod
    def _invalid_pre_trigger(c, bar_close):
        if c["bos_direction"] == "bearish":
            return bar_close < c["fib_0"]
        return bar_close > c["fib_0"]

    @staticmethod
    def _is_armed(c, bar_high, bar_low):
        if c["bos_direction"] == "bearish":
            return bar_high >= c["fib_0_75"]
        return bar_low <= c["fib_0_75"]

    @staticmethod
    def _primary_triggered(c, bar_high, bar_low):
        if c["bos_direction"] == "bearish":
            return bar_high >= c["fib_1_0"]
        return bar_low <= c["fib_1_0"]

    @staticmethod
    def _primary_sl_close_beyond(c, bar_close):
        # In bearish setup, primary's SL trigger = close > fib_1.2.
        # In bullish setup, primary's SL trigger = close < fib_1.2.
        if c["bos_direction"] == "bearish":
            return bar_close > c["fib_1_2"]
        return bar_close < c["fib_1_2"]

    @staticmethod
    def _secondary_fill_wick(c, bar_high, bar_low):
        if c["secondary_direction"] == "long":
            return bar_low <= c["entry_price"]
        return bar_high >= c["entry_price"]

    @staticmethod
    def _secondary_sl_close(c, bar_close):
        # Long secondary: SL on close BELOW fib_0.3 (which sits between fib_0=sL and fib_0.5).
        # Wait — for a bearish setup, fib_0.3 = sL + 0.3*rng (closer to sL). Long SL = close < fib_0.3.
        # For bullish setup, secondary is short, fib_0.3 = sH - 0.3*rng (closer to sH). Short SL = close > fib_0.3.
        if c["secondary_direction"] == "long":
            return bar_close < c["sl_price"]
        return bar_close > c["sl_price"]

    @staticmethod
    def _secondary_tp_wick(c, bar_high, bar_low):
        # Long TP = wick high >= fib_1.0. Short TP = wick low <= fib_1.0.
        if c["secondary_direction"] == "long":
            return bar_high >= c["tp_price"]
        return bar_low <= c["tp_price"]

    @staticmethod
    def _hard_stop_wick_hit(c, bar_high, bar_low, hsp: float) -> bool:
        # Long secondary: hard stop at price BELOW entry; fires when bar low <= hsp.
        # Short secondary: hard stop at price ABOVE entry; fires when bar high >= hsp.
        if c["secondary_direction"] == "long":
            return bar_low <= hsp
        return bar_high >= hsp

    # ---- Fill / close mechanics ----

    def _fill_secondary(self, c, ts, agg):
        c["secondary_filled_timestamp"] = ts
        risk_per_unit = abs(c["entry_price"] - c["sl_price"])
        risk_dollars = self.account * self.risk_pct
        size_by_risk = risk_dollars / risk_per_unit if risk_per_unit > 0 else 0.0
        if self.enforce_cap:
            size_by_notional = self.account / c["entry_price"] if c["entry_price"] > 0 else 0.0
            size = float(min(size_by_risk, size_by_notional))
        else:
            size = float(size_by_risk)
        c["position_size"] = size
        c["fees_quote"] = size * c["entry_price"] * self.fee  # entry fee
        agg["n_secondary_filled"] += 1

    def _close_secondary_at_tp(self, c, ts, agg):
        exit_price = c["tp_price"]
        c["secondary_exit_timestamp"] = ts
        c["exit_price"] = exit_price
        c["secondary_outcome"] = "tp_hit"
        sign = 1.0 if c["secondary_direction"] == "long" else -1.0
        gross = sign * c["position_size"] * (exit_price - c["entry_price"])
        fee_close = c["position_size"] * exit_price * self.fee
        c["gross_pnl_quote"] = round(gross, 4)
        c["fees_quote"] = round(c["fees_quote"] + fee_close, 4)
        risk_dollars = self.account * self.risk_pct
        c["r_realized"] = round(gross / risk_dollars, 4) if risk_dollars > 0 else 0.0
        agg["n_secondary_tp"] += 1

    def _close_secondary_at_sl(self, c, exit_price, ts, agg):
        c["secondary_exit_timestamp"] = ts
        c["exit_price"] = float(exit_price)
        c["secondary_outcome"] = "sl_hit"
        sign = 1.0 if c["secondary_direction"] == "long" else -1.0
        gross = sign * c["position_size"] * (exit_price - c["entry_price"])
        fee_close = c["position_size"] * exit_price * self.fee
        c["gross_pnl_quote"] = round(gross, 4)
        c["fees_quote"] = round(c["fees_quote"] + fee_close, 4)
        risk_dollars = self.account * self.risk_pct
        c["r_realized"] = round(gross / risk_dollars, 4) if risk_dollars > 0 else 0.0
        agg["n_secondary_sl"] += 1

    def _close_secondary_at_hard_stop(self, c, exit_price, ts, agg):
        c["secondary_exit_timestamp"] = ts
        c["exit_price"] = float(exit_price)
        c["secondary_outcome"] = "hard_stop_hit"
        c["hard_stop_fired"] = True
        sign = 1.0 if c["secondary_direction"] == "long" else -1.0
        gross = sign * c["position_size"] * (exit_price - c["entry_price"])
        fee_close = c["position_size"] * exit_price * self.fee
        c["gross_pnl_quote"] = round(gross, 4)
        c["fees_quote"] = round(c["fees_quote"] + fee_close, 4)
        risk_dollars = self.account * self.risk_pct
        c["r_realized"] = round(gross / risk_dollars, 4) if risk_dollars > 0 else 0.0
        agg["n_secondary_hard_stop"] += 1

    def _close_secondary_at_eod(self, c, exit_price, ts, agg):
        c["secondary_exit_timestamp"] = ts
        c["exit_price"] = float(exit_price)
        c["secondary_outcome"] = "open_eod"
        sign = 1.0 if c["secondary_direction"] == "long" else -1.0
        gross = sign * c["position_size"] * (exit_price - c["entry_price"])
        fee_close = c["position_size"] * exit_price * self.fee
        c["gross_pnl_quote"] = round(gross, 4)
        c["fees_quote"] = round(c["fees_quote"] + fee_close, 4)
        risk_dollars = self.account * self.risk_pct
        c["r_realized"] = round(gross / risk_dollars, 4) if risk_dollars > 0 else 0.0
        agg["n_open_eod"] += 1

    @staticmethod
    def _terminal_unfilled_label(phase: str) -> str:
        if phase == SecondaryOnlyEngine.P_AWAITING_RATCHET:
            return "never_finalized_eod"
        if phase == SecondaryOnlyEngine.P_AWAITING_ARM:
            return "never_armed_eod"
        if phase == SecondaryOnlyEngine.P_AWAITING_PRIMARY_TRIG:
            return "never_triggered_eod"
        if phase == SecondaryOnlyEngine.P_SECONDARY_PENDING:
            return "unfilled_eod"
        return "unknown_eod"

    # ---- Row builder ----

    def _make_row(self, c: dict) -> SecondaryRow:
        net_pnl = round(c["gross_pnl_quote"] - c["fees_quote"], 4)
        return SecondaryRow(
            setup_id=c["setup_id"],
            direction=c.get("secondary_direction", ""),
            bos_timestamp=c["bos_timestamp"],
            bos_direction=c["bos_direction"],
            swing_high_price=c["swing_high"],
            swing_low_price=c["swing_low"],
            swing_high_timestamp=c.get("swing_high_timestamp"),
            swing_low_timestamp=c.get("swing_low_timestamp"),
            swing_size_pct=c["swing_size_pct"],
            fib_1_0=c["fib_1_0"],
            fib_0_75=c["fib_0_75"],
            fib_0_5=c["fib_0_5"],
            fib_0_3=c["fib_0_3"],
            fib_0=c["fib_0"],
            fib_1_2=c["fib_1_2"],
            setup_armed_timestamp=c["setup_armed_timestamp"],
            primary_trigger_timestamp=c["primary_trigger_timestamp"],
            secondary_placed_timestamp=c["secondary_placed_timestamp"],
            secondary_filled_timestamp=c["secondary_filled_timestamp"],
            secondary_exit_timestamp=c["secondary_exit_timestamp"],
            entry_price=c["entry_price"],
            sl_price=c["sl_price"],
            tp_price=c["tp_price"],
            sl_close_value=c["sl_close_value"],
            exit_price=c["exit_price"],
            outcome=c["secondary_outcome"] or "open",
            r_planned=c["r_planned"],
            r_realized=c["r_realized"],
            position_size=c["position_size"],
            gross_pnl_quote=c["gross_pnl_quote"],
            fees_quote=c["fees_quote"],
            net_pnl_quote=net_pnl,
            primary_trigger_observed=bool(c["primary_trigger_observed"]),
            primary_would_have_outcome=c["primary_would_have_outcome"],
            hard_stop_R=float(c.get("hard_stop_R") or 0.0),
            hard_stop_price=c.get("hard_stop_price"),
            hard_stop_fired=bool(c.get("hard_stop_fired") or False),
            bias_1h=c["bias_1h"],
            btc_bias_1h=c["btc_bias_1h"],
            hour_of_day_utc=c["hour_of_day_utc"],
            weekday_utc=c["weekday_utc"],
            notes="",
        )


__all__ = ["SecondaryOnlyEngine", "SecondaryRow"]
