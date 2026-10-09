"""
backtesting/symmetric_bos_fib_engine.py
=======================================

Symmetric BOS-Fib v1 engine. State machine consuming BOS events from
``shared.structure_analyzer`` and producing primary/secondary trades.

Phases
------
WAIT_BOS                  no setup; watching engine for new BOS event
AWAITING_RATCHET          BOS fired; waiting for engine's locked extreme
                          to ratchet from breaking-close to next pivot
AWAITING_ARM              swing values locked; waiting for price to wick
                          to fib 0.75 (the arming trigger)
PRIMARY_PENDING           primary limit placed at fib 1.0 (swing extreme);
                          waiting for fill, SL invalidation, or wick fill
PRIMARY_LIVE_SECONDARY_PENDING
                          primary filled, secondary limit placed at fib 0.5;
                          watching primary's SL/TP and secondary's fill
SECONDARY_LIVE            primary TPed, secondary filled (same wick to 0.5);
                          watching secondary's SL/TP independently
PENDING_SL_EXIT           SL trigger close just printed; exit at next bar's
                          open (for primary OR secondary)

Only ONE setup is active at a time. New BOS events while phase != WAIT_BOS
are ignored.

Per-setup ratchet detection: when the engine's locked_sL (bearish) or
locked_sH (bullish) changes from the breaking-close to a different value,
the strategy treats that as the ratchet completing and finalizes swing
levels. The engine's ratchet logic is single-shot (per BOS), so this
detection is unambiguous.
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

from shared.structure_analyzer import StructureAnalyzer, StructureState


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


# -- Trade row ---------------------------------------------------------------

@dataclass
class TradeRow:
    setup_id: int
    trade_id: str                       # "primary" / "secondary"
    direction: str                      # "long" / "short"

    # BOS context
    bos_timestamp: Any
    bos_direction: str                  # "bullish" / "bearish"

    # Swing / fib geometry
    swing_high_price: float
    swing_low_price: float
    swing_size_pct: float
    fib_1_0: float
    fib_0_75: float
    fib_0_5: float
    fib_0_3: float
    fib_0: float
    fib_1_2: float

    # Setup timing
    setup_armed_timestamp: Any
    order_placed_timestamp: Any
    order_filled_timestamp: Any

    # Trade levels
    entry_price: float
    sl_price: float
    tp_price: float
    sl_close_value: Optional[float]     # the close that triggered SL (or None)

    # Exit
    exit_timestamp: Any
    exit_price: Optional[float]
    outcome: str                        # tp_hit / sl_hit / cancelled_by_invalidation /
                                        # cancelled_by_primary_sl / unfilled / open_eod

    # PnL
    r_planned: float
    r_realized: float
    position_size: float
    gross_pnl_quote: float
    fees_quote: float
    net_pnl_quote: float

    # Context
    bias_1h: str
    btc_bias_1h: str
    hour_of_day_utc: int
    weekday_utc: int

    notes: str


# -- Engine ------------------------------------------------------------------

class SymmetricBosFibEngine:
    P_WAIT_BOS = "WAIT_BOS"
    P_AWAITING_RATCHET = "AWAITING_RATCHET"
    P_AWAITING_ARM = "AWAITING_ARM"
    P_PRIMARY_PENDING = "PRIMARY_PENDING"
    P_PRIMARY_LIVE = "PRIMARY_LIVE"           # secondary pending
    P_SECONDARY_LIVE = "SECONDARY_LIVE"

    def __init__(self, cfg: dict, account_value: float = 10_000.0, verbose: bool = False):
        self.cfg = cfg
        self.account = float(account_value)
        self.risk_pct = float(cfg["risk_pct"])
        self.fee = float(cfg["taker_fee"])
        self.enforce_cap = bool(cfg.get("enforce_notional_cap", True))
        self.verbose = verbose

    def run(
        self,
        df_5m: pd.DataFrame,
        df_1h: Optional[pd.DataFrame] = None,
        df_btc_1h: Optional[pd.DataFrame] = None,
    ) -> tuple[list[TradeRow], dict]:
        cfg = self.cfg
        n = len(df_5m)
        if n == 0:
            return [], {}

        ts_5m = pd.to_datetime(df_5m["timestamp"], utc=True).to_numpy()
        opens_5m = df_5m["open"].to_numpy(dtype=float)
        highs_5m = df_5m["high"].to_numpy(dtype=float)
        lows_5m = df_5m["low"].to_numpy(dtype=float)
        closes_5m = df_5m["close"].to_numpy(dtype=float)

        # BOS engine.
        analyzer = StructureAnalyzer(
            reversal_threshold_pct=float(cfg["reversal_pct_5m"]),
            init_bars=int(cfg["init_bars"]),
        )
        states = analyzer.analyze(df_5m)

        # 1h bias (logging only).
        bias_1h = None
        idx_1h_for_5m = None
        if df_1h is not None and len(df_1h) > 0:
            bias_1h = compute_ema_bias(df_1h, int(cfg["htf_ema_fast"]), int(cfg["htf_ema_slow"]))
            ts_1h = pd.to_datetime(df_1h["timestamp"], utc=True).to_numpy()
            idx_1h_for_5m = map_5m_to_1h_idx(ts_5m, ts_1h)

        btc_bias_1h = None
        idx_btc_1h_for_5m = None
        if df_btc_1h is not None and len(df_btc_1h) > 0:
            btc_bias_1h = compute_ema_bias(df_btc_1h, int(cfg["htf_ema_fast"]), int(cfg["htf_ema_slow"]))
            ts_btc_1h = pd.to_datetime(df_btc_1h["timestamp"], utc=True).to_numpy()
            idx_btc_1h_for_5m = map_5m_to_1h_idx(ts_5m, ts_btc_1h)

        # ---- counters ----
        agg = {
            "n_bos_events_total": 0,
            "n_bos_events_queued": 0,                  # queued (replaced any prior queued)
            "n_bos_events_queued_then_processed": 0,   # popped from queue and dispatched
            "n_bos_events_dropped_by_overwrite": 0,    # queued, then overwritten before processing
            "n_setups_below_min_swing": 0,
            "n_setups_above_max_swing": 0,
            "n_setups_finalized": 0,
            "n_setups_armed": 0,
            "n_invalidated_pre_arm": 0,         # close past fib_0 before arming
            "n_invalidated_post_arm": 0,        # close past fib_0 after arming, pre-fill
            "n_primary_filled": 0,
            "n_primary_unfilled": 0,
            "n_primary_tp": 0,
            "n_primary_sl": 0,
            "n_secondary_filled": 0,
            "n_secondary_cancelled": 0,
            "n_secondary_tp": 0,
            "n_secondary_sl": 0,
        }

        rows: list[TradeRow] = []
        next_setup_id = 1

        phase = self.P_WAIT_BOS
        setup: Optional[dict] = None     # current setup dict
        # Pending SL exits (executed at next bar's open)
        pending_sl_exit: Optional[dict] = None  # {"trade": "primary"|"secondary", ...}

        # Most-recent locked extreme at BOS time (for ratchet detection)
        bos_provisional_locked: Optional[float] = None  # the breaking close

        # Queued BOS event (single-slot; new BOS while setup active overwrites
        # any prior queued event). On active-setup resolution, the queued
        # event is dispatched: a new setup is created, and the loop rewinds
        # to the queued BOS bar to replay strategy logic against it.
        queued_bos_idx: int = -1
        queued_bos_dir: str = ""

        i = 0
        while i < n:
            bar_ts = pd.Timestamp(ts_5m[i])
            bh = float(highs_5m[i])
            bl = float(lows_5m[i])
            bc = float(closes_5m[i])
            bo = float(opens_5m[i])
            state = states[i]

            # --- 0. Process pending SL exit FIRST (executes at this bar's open) ---
            if pending_sl_exit is not None:
                exit_price = bo
                if pending_sl_exit["trade"] == "primary":
                    self._close_primary_at_sl(setup, exit_price, bar_ts, agg)
                    rows.append(self._make_primary_row(setup))
                    # cancel secondary
                    setup["secondary"]["outcome"] = "cancelled_by_primary_sl"
                    rows.append(self._make_secondary_row(setup))
                    agg["n_secondary_cancelled"] += 1
                    setup = None
                    phase = self.P_WAIT_BOS
                else:  # secondary
                    self._close_secondary_at_sl(setup, exit_price, bar_ts, agg)
                    rows.append(self._make_primary_row(setup))      # already closed at TP
                    rows.append(self._make_secondary_row(setup))
                    setup = None
                    phase = self.P_WAIT_BOS
                pending_sl_exit = None
                continue

            # --- 1. New BOS event detection ---
            new_bos = (state.last_bos_idx == i and state.last_event in ("bos_up", "bos_down"))
            if new_bos:
                agg["n_bos_events_total"] += 1
                if phase != self.P_WAIT_BOS:
                    # Queue (single-slot — overwrite any prior queued BOS).
                    if queued_bos_idx >= 0:
                        agg["n_bos_events_dropped_by_overwrite"] += 1
                    queued_bos_idx = i
                    queued_bos_dir = "bullish" if state.last_event == "bos_up" else "bearish"
                    agg["n_bos_events_queued"] += 1
                    if self.verbose:
                        print(f"  {bar_ts}  BOS {queued_bos_dir} QUEUED (setup #{setup['setup_id']} active)")
                    new_bos = False  # don't process here

            if new_bos:
                bos_dir = "bullish" if state.last_event == "bos_up" else "bearish"
                setup = self._new_setup(
                    next_setup_id, i, bar_ts, bos_dir,
                    state.swing_high, state.swing_low,
                    bias_1h, idx_1h_for_5m, btc_bias_1h, idx_btc_1h_for_5m,
                )
                next_setup_id += 1
                bos_provisional_locked = (state.swing_low if bos_dir == "bearish" else state.swing_high)
                phase = self.P_AWAITING_RATCHET
                if self.verbose:
                    print(f"  {bar_ts}  BOS {bos_dir} — setup #{setup['setup_id']} pending ratchet "
                          f"(provisional sH={state.swing_high:.4f}, sL={state.swing_low:.4f})")

            # --- 2. AWAITING_RATCHET: detect ratchet (engine swing_low or swing_high changes) ---
            if phase == self.P_AWAITING_RATCHET and setup is not None:
                ratchet_done = False
                if setup["bos_direction"] == "bearish":
                    # swing_low ratchets from breaking close to next L pivot.
                    if state.swing_low != bos_provisional_locked:
                        ratchet_done = True
                else:
                    if state.swing_high != bos_provisional_locked:
                        ratchet_done = True

                if ratchet_done:
                    # Finalize swing values.
                    self._finalize_setup(setup, state.swing_high, state.swing_low, cfg)
                    swing_size = setup["swing_size_pct"]
                    min_swing = float(cfg["min_swing_pct"])
                    max_swing = float(cfg.get("max_swing_pct", float("inf")))
                    if swing_size < min_swing:
                        agg["n_setups_below_min_swing"] += 1
                        if self.verbose:
                            print(f"  {bar_ts}  setup #{setup['setup_id']} discarded — "
                                  f"swing {swing_size:.2f}% < min {min_swing}%")
                        setup = None
                        phase = self.P_WAIT_BOS
                    elif swing_size > max_swing:
                        agg["n_setups_above_max_swing"] += 1
                        if self.verbose:
                            print(f"  {bar_ts}  setup #{setup['setup_id']} discarded — "
                                  f"swing {swing_size:.2f}% > max {max_swing}%")
                        setup = None
                        phase = self.P_WAIT_BOS
                    else:
                        agg["n_setups_finalized"] += 1
                        phase = self.P_AWAITING_ARM
                        if self.verbose:
                            print(f"  {bar_ts}  setup #{setup['setup_id']} finalized — "
                                  f"sH={setup['swing_high']:.4f} sL={setup['swing_low']:.4f} "
                                  f"size={swing_size:.2f}%  fib_0.75={setup['fib_0_75']:.4f}  "
                                  f"fib_1.0={setup['fib_1_0']:.4f}")

            # --- 3. AWAITING_ARM: watch for arming wick OR invalidation ---
            if phase == self.P_AWAITING_ARM and setup is not None:
                if self._check_invalidation(setup, bc):
                    setup["primary"]["outcome"] = "cancelled_by_invalidation"
                    setup["primary"]["exit_timestamp"] = bar_ts
                    setup["secondary"]["outcome"] = "cancelled_by_invalidation"
                    rows.append(self._make_primary_row(setup))
                    rows.append(self._make_secondary_row(setup))
                    agg["n_invalidated_pre_arm"] += 1
                    if self.verbose:
                        print(f"  {bar_ts}  setup #{setup['setup_id']} INVALIDATED pre-arm "
                              f"(close {bc:.4f} past fib_0 {setup['fib_0']:.4f})")
                    setup = None
                    phase = self.P_WAIT_BOS
                elif self._check_arming(setup, bh, bl):
                    setup["setup_armed_timestamp"] = bar_ts
                    setup["primary"]["order_placed_timestamp"] = bar_ts
                    agg["n_setups_armed"] += 1
                    phase = self.P_PRIMARY_PENDING
                    if self.verbose:
                        print(f"  {bar_ts}  setup #{setup['setup_id']} ARMED — "
                              f"primary limit @ fib_1.0 {setup['fib_1_0']:.4f}")

            # --- 4. PRIMARY_PENDING: watch for fill OR invalidation ---
            if phase == self.P_PRIMARY_PENDING and setup is not None:
                if self._check_invalidation(setup, bc):
                    setup["primary"]["outcome"] = "cancelled_by_invalidation"
                    setup["primary"]["exit_timestamp"] = bar_ts
                    setup["secondary"]["outcome"] = "cancelled_by_invalidation"
                    rows.append(self._make_primary_row(setup))
                    rows.append(self._make_secondary_row(setup))
                    agg["n_invalidated_post_arm"] += 1
                    if self.verbose:
                        print(f"  {bar_ts}  setup #{setup['setup_id']} INVALIDATED post-arm "
                              f"(close {bc:.4f} past fib_0 {setup['fib_0']:.4f})")
                    setup = None
                    phase = self.P_WAIT_BOS
                elif self._check_primary_fill(setup, bh, bl):
                    self._fill_primary(setup, bar_ts, agg)
                    setup["secondary"]["order_placed_timestamp"] = bar_ts
                    agg["n_primary_filled"] += 1
                    phase = self.P_PRIMARY_LIVE
                    if self.verbose:
                        print(f"  {bar_ts}  setup #{setup['setup_id']} primary FILLED @ "
                              f"{setup['primary']['entry_price']:.4f}  secondary limit @ "
                              f"{setup['secondary']['entry_price']:.4f}")

            # --- 5. PRIMARY_LIVE: watch for primary SL or wick to TP (which fills secondary too) ---
            if phase == self.P_PRIMARY_LIVE and setup is not None:
                # Primary SL check (close past fib_1.2 — direction-aware)
                if self._check_primary_sl_close(setup, bc):
                    setup["primary"]["sl_close_value"] = bc
                    pending_sl_exit = {"trade": "primary", "trigger_close": bc, "trigger_idx": i}
                    if self.verbose:
                        print(f"  {bar_ts}  setup #{setup['setup_id']} primary SL CLOSE @ {bc:.4f} "
                              f"(threshold {setup['primary']['sl_price']:.4f}) — exit next open")
                    # Don't transition phase yet; pending_sl_exit handles it on next bar.
                elif self._check_primary_tp_wick(setup, bh, bl):
                    # Primary TP fills via wick to fib_0.5; secondary fills on same wick.
                    self._close_primary_at_tp(setup, bar_ts, agg)
                    self._fill_secondary(setup, bar_ts, agg)
                    agg["n_primary_tp"] += 1
                    agg["n_secondary_filled"] += 1
                    phase = self.P_SECONDARY_LIVE
                    if self.verbose:
                        print(f"  {bar_ts}  setup #{setup['setup_id']} primary TP @ "
                              f"{setup['primary']['tp_price']:.4f}, secondary FILLED")

            # --- 6. SECONDARY_LIVE: watch for secondary SL/TP ---
            if phase == self.P_SECONDARY_LIVE and setup is not None:
                if self._check_secondary_sl_close(setup, bc):
                    setup["secondary"]["sl_close_value"] = bc
                    pending_sl_exit = {"trade": "secondary", "trigger_close": bc, "trigger_idx": i}
                    if self.verbose:
                        print(f"  {bar_ts}  setup #{setup['setup_id']} secondary SL CLOSE @ {bc:.4f} "
                              f"(threshold {setup['secondary']['sl_price']:.4f}) — exit next open")
                elif self._check_secondary_tp_wick(setup, bh, bl):
                    self._close_secondary_at_tp(setup, bar_ts, agg)
                    rows.append(self._make_primary_row(setup))
                    rows.append(self._make_secondary_row(setup))
                    agg["n_secondary_tp"] += 1
                    if self.verbose:
                        print(f"  {bar_ts}  setup #{setup['setup_id']} secondary TP @ "
                              f"{setup['secondary']['tp_price']:.4f}")
                    setup = None
                    phase = self.P_WAIT_BOS

            # --- 7. Dispatch queued BOS if a setup just resolved ---
            if phase == self.P_WAIT_BOS and queued_bos_idx >= 0:
                qbos_idx = queued_bos_idx
                qbos_dir = queued_bos_dir
                # Reset the queue.
                queued_bos_idx = -1
                queued_bos_dir = ""
                # Create new setup using the engine state at the queued BOS bar.
                state_at_qbos = states[qbos_idx]
                setup = self._new_setup(
                    next_setup_id, qbos_idx, pd.Timestamp(ts_5m[qbos_idx]), qbos_dir,
                    state_at_qbos.swing_high, state_at_qbos.swing_low,
                    bias_1h, idx_1h_for_5m, btc_bias_1h, idx_btc_1h_for_5m,
                )
                setup["from_queue"] = True
                next_setup_id += 1
                bos_provisional_locked = (
                    state_at_qbos.swing_low if qbos_dir == "bearish"
                    else state_at_qbos.swing_high
                )
                phase = self.P_AWAITING_RATCHET
                agg["n_bos_events_queued_then_processed"] += 1
                if self.verbose:
                    print(f"  {bar_ts}  DISPATCH queued BOS {qbos_dir} from {pd.Timestamp(ts_5m[qbos_idx])} "
                          f"(setup #{setup['setup_id']}); rewinding loop")
                # Rewind the loop to bos_idx so subsequent iterations replay
                # bars from qbos_idx + 1 onward against the new setup.
                i = qbos_idx
                # fall through to i += 1

            i += 1

        # End-of-data: finalize anything in flight.
        if setup is not None:
            # If we're mid-setup, mark unresolved.
            if phase == self.P_AWAITING_ARM:
                setup["primary"]["outcome"] = "unfilled"
                setup["secondary"]["outcome"] = "unfilled"
                rows.append(self._make_primary_row(setup))
                rows.append(self._make_secondary_row(setup))
            elif phase == self.P_PRIMARY_PENDING:
                setup["primary"]["outcome"] = "unfilled"
                setup["secondary"]["outcome"] = "unfilled"
                agg["n_primary_unfilled"] += 1
                rows.append(self._make_primary_row(setup))
                rows.append(self._make_secondary_row(setup))
            elif phase == self.P_PRIMARY_LIVE:
                # Primary still open at EOD.
                setup["primary"]["outcome"] = "open_eod"
                setup["primary"]["exit_timestamp"] = pd.Timestamp(ts_5m[-1])
                setup["primary"]["exit_price"] = float(closes_5m[-1])
                setup["secondary"]["outcome"] = "unfilled"
                rows.append(self._make_primary_row(setup))
                rows.append(self._make_secondary_row(setup))
            elif phase == self.P_SECONDARY_LIVE:
                setup["secondary"]["outcome"] = "open_eod"
                setup["secondary"]["exit_timestamp"] = pd.Timestamp(ts_5m[-1])
                setup["secondary"]["exit_price"] = float(closes_5m[-1])
                rows.append(self._make_primary_row(setup))
                rows.append(self._make_secondary_row(setup))

        return rows, agg

    # -- helpers ------------------------------------------------------------

    def _new_setup(
        self, setup_id: int, bos_idx: int, bos_ts: pd.Timestamp, bos_direction: str,
        provisional_sH: float, provisional_sL: float,
        bias_1h, idx_1h_for_5m, btc_bias, idx_btc_for_5m,
    ) -> dict:
        # Bias snapshot at BOS time.
        bias = "none"
        btc_bias_v = "none"
        if bias_1h is not None and idx_1h_for_5m is not None:
            j = int(idx_1h_for_5m[bos_idx])
            if j >= 0:
                bias = str(bias_1h[j])
        if btc_bias is not None and idx_btc_for_5m is not None:
            j = int(idx_btc_for_5m[bos_idx])
            if j >= 0:
                btc_bias_v = str(btc_bias[j])

        return {
            "setup_id": setup_id,
            "bos_idx": bos_idx,
            "bos_timestamp": bos_ts,
            "bos_direction": bos_direction,
            # provisional swings; locked at finalization.
            "swing_high": provisional_sH,
            "swing_low": provisional_sL,
            "swing_size_pct": float("nan"),
            # fibs filled at finalization.
            "fib_1_0": float("nan"),
            "fib_0_75": float("nan"),
            "fib_0_5": float("nan"),
            "fib_0_3": float("nan"),
            "fib_0": float("nan"),
            "fib_1_2": float("nan"),
            # timing
            "setup_armed_timestamp": None,
            # context
            "bias_1h": bias,
            "btc_bias_1h": btc_bias_v,
            "hour_of_day_utc": int(bos_ts.hour),
            "weekday_utc": int(bos_ts.weekday()),
            # leg state
            "primary": self._new_leg(),
            "secondary": self._new_leg(),
        }

    @staticmethod
    def _new_leg() -> dict:
        return {
            "direction": "",
            "order_placed_timestamp": None,
            "order_filled_timestamp": None,
            "entry_price": float("nan"),
            "sl_price": float("nan"),
            "tp_price": float("nan"),
            "sl_close_value": None,
            "exit_timestamp": None,
            "exit_price": None,
            "outcome": "",
            "r_planned": float("nan"),
            "r_realized": 0.0,
            "position_size": 0.0,
            "gross_pnl_quote": 0.0,
            "fees_quote": 0.0,
            "net_pnl_quote": 0.0,
        }

    def _finalize_setup(self, setup: dict, swing_h: float, swing_l: float, cfg: dict) -> None:
        setup["swing_high"] = float(swing_h)
        setup["swing_low"] = float(swing_l)
        rng = swing_h - swing_l
        denom = max(swing_h, swing_l)
        size_pct = (rng / denom) * 100.0 if denom > 0 else 0.0
        setup["swing_size_pct"] = round(size_pct, 4)

        # Fib 0..1.2 with direction-aware mapping per spec:
        # bearish: fib_1.0 = swing_h, fib_0 = swing_l.  Levels = swing_l + level * range.
        # bullish: fib_1.0 = swing_l, fib_0 = swing_h.  Levels = swing_h - level * range.
        if setup["bos_direction"] == "bearish":
            setup["fib_0"] = float(swing_l)
            setup["fib_0_3"] = float(swing_l + 0.3 * rng)
            setup["fib_0_5"] = float(swing_l + 0.5 * rng)
            setup["fib_0_75"] = float(swing_l + 0.75 * rng)
            setup["fib_1_0"] = float(swing_h)
            setup["fib_1_2"] = float(swing_l + 1.2 * rng)
            # Primary direction: short.
            setup["primary"]["direction"] = "short"
            setup["primary"]["entry_price"] = setup["fib_1_0"]
            setup["primary"]["sl_price"] = setup["fib_1_2"]   # close-trigger threshold
            setup["primary"]["tp_price"] = setup["fib_0_5"]
            # Secondary direction: long (counter).
            setup["secondary"]["direction"] = "long"
            setup["secondary"]["entry_price"] = setup["fib_0_5"]
            setup["secondary"]["sl_price"] = setup["fib_0_3"]
            setup["secondary"]["tp_price"] = setup["fib_1_0"]
        else:  # bullish
            setup["fib_0"] = float(swing_h)
            setup["fib_0_3"] = float(swing_h - 0.3 * rng)
            setup["fib_0_5"] = float(swing_h - 0.5 * rng)
            setup["fib_0_75"] = float(swing_h - 0.75 * rng)
            setup["fib_1_0"] = float(swing_l)
            setup["fib_1_2"] = float(swing_h - 1.2 * rng)
            setup["primary"]["direction"] = "long"
            setup["primary"]["entry_price"] = setup["fib_1_0"]
            setup["primary"]["sl_price"] = setup["fib_1_2"]
            setup["primary"]["tp_price"] = setup["fib_0_5"]
            setup["secondary"]["direction"] = "short"
            setup["secondary"]["entry_price"] = setup["fib_0_5"]
            setup["secondary"]["sl_price"] = setup["fib_0_3"]
            setup["secondary"]["tp_price"] = setup["fib_1_0"]

        # R planned (per leg). 1R = |entry - sl|.
        for leg_name in ("primary", "secondary"):
            leg = setup[leg_name]
            risk = abs(leg["entry_price"] - leg["sl_price"])
            reward = abs(leg["tp_price"] - leg["entry_price"])
            leg["r_planned"] = round(reward / risk, 3) if risk > 0 else 0.0

    @staticmethod
    def _check_invalidation(setup: dict, bar_close: float) -> bool:
        if setup["bos_direction"] == "bearish":
            return bar_close < setup["fib_0"]
        return bar_close > setup["fib_0"]

    @staticmethod
    def _check_arming(setup: dict, bar_high: float, bar_low: float) -> bool:
        if setup["bos_direction"] == "bearish":
            return bar_high >= setup["fib_0_75"]
        return bar_low <= setup["fib_0_75"]

    @staticmethod
    def _check_primary_fill(setup: dict, bar_high: float, bar_low: float) -> bool:
        leg = setup["primary"]
        if leg["direction"] == "short":
            return bar_high >= leg["entry_price"]
        return bar_low <= leg["entry_price"]

    @staticmethod
    def _check_primary_sl_close(setup: dict, bar_close: float) -> bool:
        leg = setup["primary"]
        if leg["direction"] == "short":
            return bar_close > leg["sl_price"]
        return bar_close < leg["sl_price"]

    @staticmethod
    def _check_primary_tp_wick(setup: dict, bar_high: float, bar_low: float) -> bool:
        leg = setup["primary"]
        if leg["direction"] == "short":
            return bar_low <= leg["tp_price"]
        return bar_high >= leg["tp_price"]

    @staticmethod
    def _check_secondary_sl_close(setup: dict, bar_close: float) -> bool:
        leg = setup["secondary"]
        if leg["direction"] == "short":
            return bar_close > leg["sl_price"]
        return bar_close < leg["sl_price"]

    @staticmethod
    def _check_secondary_tp_wick(setup: dict, bar_high: float, bar_low: float) -> bool:
        leg = setup["secondary"]
        if leg["direction"] == "short":
            return bar_low <= leg["tp_price"]
        return bar_high >= leg["tp_price"]

    # -- fill / close mechanics --------------------------------------------

    def _fill_primary(self, setup: dict, ts: pd.Timestamp, agg: dict) -> None:
        leg = setup["primary"]
        leg["order_filled_timestamp"] = ts
        risk_per_unit = abs(leg["entry_price"] - leg["sl_price"])
        risk_dollars = self.account * self.risk_pct
        size_by_risk = risk_dollars / risk_per_unit if risk_per_unit > 0 else 0.0
        if self.enforce_cap:
            size_by_notional = self.account / leg["entry_price"] if leg["entry_price"] > 0 else 0.0
            size = float(min(size_by_risk, size_by_notional))
        else:
            size = float(size_by_risk)
        leg["position_size"] = size
        leg["fees_quote"] = size * leg["entry_price"] * self.fee  # entry fee

    def _close_primary_at_tp(self, setup: dict, ts: pd.Timestamp, agg: dict) -> None:
        leg = setup["primary"]
        exit_price = leg["tp_price"]
        leg["exit_timestamp"] = ts
        leg["exit_price"] = exit_price
        leg["outcome"] = "tp_hit"
        sign = 1.0 if leg["direction"] == "long" else -1.0
        gross = sign * leg["position_size"] * (exit_price - leg["entry_price"])
        fee_close = leg["position_size"] * exit_price * self.fee
        leg["gross_pnl_quote"] = round(gross, 4)
        leg["fees_quote"] = round(leg["fees_quote"] + fee_close, 4)
        leg["net_pnl_quote"] = round(gross - leg["fees_quote"], 4)
        risk_dollars = self.account * self.risk_pct
        leg["r_realized"] = round(gross / risk_dollars, 4) if risk_dollars > 0 else 0.0

    def _close_primary_at_sl(self, setup: dict, exit_price: float, ts: pd.Timestamp, agg: dict) -> None:
        leg = setup["primary"]
        leg["exit_timestamp"] = ts
        leg["exit_price"] = exit_price
        leg["outcome"] = "sl_hit"
        sign = 1.0 if leg["direction"] == "long" else -1.0
        gross = sign * leg["position_size"] * (exit_price - leg["entry_price"])
        fee_close = leg["position_size"] * exit_price * self.fee
        leg["gross_pnl_quote"] = round(gross, 4)
        leg["fees_quote"] = round(leg["fees_quote"] + fee_close, 4)
        leg["net_pnl_quote"] = round(gross - leg["fees_quote"], 4)
        risk_dollars = self.account * self.risk_pct
        leg["r_realized"] = round(gross / risk_dollars, 4) if risk_dollars > 0 else 0.0
        agg["n_primary_sl"] += 1

    def _fill_secondary(self, setup: dict, ts: pd.Timestamp, agg: dict) -> None:
        leg = setup["secondary"]
        leg["order_filled_timestamp"] = ts
        risk_per_unit = abs(leg["entry_price"] - leg["sl_price"])
        risk_dollars = self.account * self.risk_pct
        size_by_risk = risk_dollars / risk_per_unit if risk_per_unit > 0 else 0.0
        if self.enforce_cap:
            size_by_notional = self.account / leg["entry_price"] if leg["entry_price"] > 0 else 0.0
            size = float(min(size_by_risk, size_by_notional))
        else:
            size = float(size_by_risk)
        leg["position_size"] = size
        leg["fees_quote"] = size * leg["entry_price"] * self.fee

    def _close_secondary_at_tp(self, setup: dict, ts: pd.Timestamp, agg: dict) -> None:
        leg = setup["secondary"]
        exit_price = leg["tp_price"]
        leg["exit_timestamp"] = ts
        leg["exit_price"] = exit_price
        leg["outcome"] = "tp_hit"
        sign = 1.0 if leg["direction"] == "long" else -1.0
        gross = sign * leg["position_size"] * (exit_price - leg["entry_price"])
        fee_close = leg["position_size"] * exit_price * self.fee
        leg["gross_pnl_quote"] = round(gross, 4)
        leg["fees_quote"] = round(leg["fees_quote"] + fee_close, 4)
        leg["net_pnl_quote"] = round(gross - leg["fees_quote"], 4)
        risk_dollars = self.account * self.risk_pct
        leg["r_realized"] = round(gross / risk_dollars, 4) if risk_dollars > 0 else 0.0

    def _close_secondary_at_sl(self, setup: dict, exit_price: float, ts: pd.Timestamp, agg: dict) -> None:
        leg = setup["secondary"]
        leg["exit_timestamp"] = ts
        leg["exit_price"] = exit_price
        leg["outcome"] = "sl_hit"
        sign = 1.0 if leg["direction"] == "long" else -1.0
        gross = sign * leg["position_size"] * (exit_price - leg["entry_price"])
        fee_close = leg["position_size"] * exit_price * self.fee
        leg["gross_pnl_quote"] = round(gross, 4)
        leg["fees_quote"] = round(leg["fees_quote"] + fee_close, 4)
        leg["net_pnl_quote"] = round(gross - leg["fees_quote"], 4)
        risk_dollars = self.account * self.risk_pct
        leg["r_realized"] = round(gross / risk_dollars, 4) if risk_dollars > 0 else 0.0
        agg["n_secondary_sl"] += 1

    # -- row builders ------------------------------------------------------

    def _make_primary_row(self, setup: dict) -> TradeRow:
        return self._make_row(setup, "primary")

    def _make_secondary_row(self, setup: dict) -> TradeRow:
        return self._make_row(setup, "secondary")

    def _make_row(self, setup: dict, leg_name: str) -> TradeRow:
        leg = setup[leg_name]
        return TradeRow(
            setup_id=setup["setup_id"],
            trade_id=leg_name,
            direction=leg["direction"],
            bos_timestamp=setup["bos_timestamp"],
            bos_direction=setup["bos_direction"],
            swing_high_price=setup["swing_high"],
            swing_low_price=setup["swing_low"],
            swing_size_pct=setup["swing_size_pct"],
            fib_1_0=setup["fib_1_0"],
            fib_0_75=setup["fib_0_75"],
            fib_0_5=setup["fib_0_5"],
            fib_0_3=setup["fib_0_3"],
            fib_0=setup["fib_0"],
            fib_1_2=setup["fib_1_2"],
            setup_armed_timestamp=setup["setup_armed_timestamp"],
            order_placed_timestamp=leg["order_placed_timestamp"],
            order_filled_timestamp=leg["order_filled_timestamp"],
            entry_price=leg["entry_price"],
            sl_price=leg["sl_price"],
            tp_price=leg["tp_price"],
            sl_close_value=leg["sl_close_value"],
            exit_timestamp=leg["exit_timestamp"],
            exit_price=leg["exit_price"],
            outcome=leg["outcome"] or "open",
            r_planned=leg["r_planned"],
            r_realized=leg["r_realized"],
            position_size=leg["position_size"],
            gross_pnl_quote=leg["gross_pnl_quote"],
            fees_quote=leg["fees_quote"],
            net_pnl_quote=leg["net_pnl_quote"],
            bias_1h=setup["bias_1h"],
            btc_bias_1h=setup["btc_bias_1h"],
            hour_of_day_utc=setup["hour_of_day_utc"],
            weekday_utc=setup["weekday_utc"],
            notes="",
        )


__all__ = ["SymmetricBosFibEngine", "TradeRow"]
