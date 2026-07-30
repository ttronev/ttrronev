"""
backtesting/bos_retest_engine.py — 1H BOS retest continuation engine.

The engine has two stages so that sweeping 480 variants is cheap:

1. **Precompute** (once per data load):
   * StructureAnalyzer pass over the 1H bars -> BOS events + per-bar swing
     levels.
   * Rolling EMA12 / EMA21 -> per-bar 1H bias array.
   * Each BOS event is enriched with `pre_bos_swing_extreme` and
     `swing_size_pct` so per-variant filtering is just a list scan.

2. **Per-variant resolution**:
   * Iterate BOS events in chronological order.
   * Apply bias + min-swing filters.
   * Compute the entry zone, entry price, and reference SL points.
   * Walk forward bar-by-bar to detect fill / pre-fill invalidation /
     timeout, then walk further to detect TP/SL.
   * No concurrency limit -- every qualifying BOS produces an independent
     setup. R-unit EV/WR/PF are per-trade statistics; gating on "one
     trade at a time" silently drops setups when a single TP-target=5R
     short stays open for months (e.g. SOL Nov 2025 -> May 2026).

Conservative tie-breakers
-------------------------
* If a single bar reaches both the entry and the TP/SL conditions, we
  assume the entry fills first, then SL precedes TP on the same bar.
* If a single bar would hit both invalidation-close and the entry-fill
  wick, we treat the wick as filling first (the limit fires intrabar
  before the close).

The retest level for a bearish setup is the analyzer's `last_bos_level`
on the bos_down -- i.e. the prior locked structural low. Bullish mirrors.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from shared.structure_analyzer import StructureAnalyzer


# -- Precomputed BOS event --------------------------------------------------

@dataclass
class BOSEvent:
    bos_idx: int                 # bar index in df_1h
    bos_ts: pd.Timestamp
    direction: str               # 'short' (bos_down) or 'long' (bos_up)
    bos_close: float
    retest_level: float          # = last_bos_level (the broken level)
    pre_bos_swing_extreme: float # bearish: new locked_sH; bullish: new locked_sL
    swing_size_pct: float        # |pre_bos_swing_extreme - bos_close| / bos_close


# -- Single resolved trade --------------------------------------------------

@dataclass
class TradeRow:
    setup_id: int
    direction: str
    bos_ts: pd.Timestamp
    bos_idx: int
    bos_close: float
    retest_level: float
    pre_bos_swing_extreme: float
    swing_size_pct: float

    # Variant params (denormalized for CSV self-containment).
    entry_rule: str
    buffer_pct: float
    sl_placement: str
    tp_rr: float
    min_swing_pct: float
    bars_to_timeout: int

    # Entry / exit
    fill_ts: Optional[pd.Timestamp]
    fill_idx: int
    entry_price: float
    sl_price: float
    tp_price: float
    exit_ts: Optional[pd.Timestamp]
    exit_idx: int
    exit_price: Optional[float]
    outcome: str   # 'tp', 'sl', 'cancelled_invalidated', 'cancelled_timeout', 'open_eod', 'unfilled'

    # PnL in R + dollars
    r_gross: float
    r_net: float
    gross_pnl_quote: float
    fees_quote: float
    net_pnl_quote: float
    position_size: float
    risk_dollars: float


# -- Public API -------------------------------------------------------------

def compute_ema_bias(closes: np.ndarray, ema_fast: int, ema_slow: int) -> np.ndarray:
    """Return per-bar 'bullish' / 'bearish' / 'none' (string array)."""
    f = pd.Series(closes).ewm(span=ema_fast, adjust=False).mean().to_numpy()
    s = pd.Series(closes).ewm(span=ema_slow, adjust=False).mean().to_numpy()
    bias = np.full(len(closes), "none", dtype=object)
    for i in range(len(closes)):
        if f[i] > s[i]:
            bias[i] = "bullish"
        elif f[i] < s[i]:
            bias[i] = "bearish"
    return bias


def precompute_bos_events(df: pd.DataFrame, reversal_pct: float, init_bars: int
                          ) -> tuple[list[BOSEvent], np.ndarray]:
    """One analyzer pass over the 1H bars. Returns:
        events: list of BOSEvent (chronological)
        per_bar_state: 'uptrend' / 'downtrend' / 'undetermined' (for diag)
    """
    a = StructureAnalyzer(reversal_threshold_pct=reversal_pct, init_bars=init_bars)
    states = a.analyze(df)
    events: list[BOSEvent] = []
    per_bar_state = np.empty(len(states), dtype=object)
    close = df["close"].to_numpy(dtype=float)
    ts_col = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    for i, s in enumerate(states):
        per_bar_state[i] = s.state
        if s.last_bos_idx != i:
            continue
        if s.last_event == "bos_down":
            direction = "short"
            pre_bos_extreme = s.swing_high
        elif s.last_event == "bos_up":
            direction = "long"
            pre_bos_extreme = s.swing_low
        else:
            continue
        retest = s.last_bos_level
        if not np.isfinite(retest) or not np.isfinite(pre_bos_extreme):
            continue
        bos_c = float(close[i])
        if bos_c <= 0:
            continue
        swing_size = abs(pre_bos_extreme - bos_c) / bos_c
        events.append(BOSEvent(
            bos_idx=i,
            bos_ts=ts_col.iloc[i],
            direction=direction,
            bos_close=bos_c,
            retest_level=float(retest),
            pre_bos_swing_extreme=float(pre_bos_extreme),
            swing_size_pct=float(swing_size),
        ))
    return events, per_bar_state


def _compute_entry_price(direction: str, retest: float, buffer_pct: float,
                         entry_rule: str) -> float:
    """Entry price for a given retest zone + rule.

    Bearish zone: [retest, retest*(1+buf)]  (zone above retest_level)
        zone_far  = retest                  (deepest fill -- full retest)
        zone_mid  = retest * (1 + buf/2)
        zone_near = retest * (1 + buf)      (shallowest fill)
    Bullish zone: [retest*(1-buf), retest]  (zone below retest_level)
        zone_far  = retest                  (deepest fill)
        zone_mid  = retest * (1 - buf/2)
        zone_near = retest * (1 - buf)
    """
    if direction == "short":
        if entry_rule == "zone_far":
            return retest
        if entry_rule == "zone_mid":
            return retest * (1.0 + buffer_pct / 2.0)
        if entry_rule == "zone_near":
            return retest * (1.0 + buffer_pct)
    else:  # long
        if entry_rule == "zone_far":
            return retest
        if entry_rule == "zone_mid":
            return retest * (1.0 - buffer_pct / 2.0)
        if entry_rule == "zone_near":
            return retest * (1.0 - buffer_pct)
    raise ValueError(f"bad entry_rule={entry_rule}")


@dataclass
class VariantParams:
    entry_rule: str
    buffer_pct: float
    sl_placement: str
    tp_rr: float
    min_swing_pct: float
    bars_to_timeout: int
    # Baseline: continuation-cancel OFF. Multiple same-direction BOSes
    # all fire as independent setups (e.g. May 13 12:00 + May 14 03:00
    # are both legitimate structural breaks per the ratcheted-locked-
    # extreme rule -- the engine doesn't emit phantom in-swing BOSes,
    # so killing one because another fires would throw away real signal).
    # Toggle preserved for optional A/B later.
    use_continuation_cancel: bool = False
    # Leg-count filter ("wait for N-th BOS in the leg"). Default 1 takes
    # every BOS; kept as a knob so future research can re-test it.
    min_leg_count: int = 1


def run_variant(
    df: pd.DataFrame,
    events: list[BOSEvent],
    bias: np.ndarray,
    params: VariantParams,
    risk_pct: float,
    fee_rate: float,
    account: float,
) -> tuple[list[TradeRow], dict]:
    """Resolve one variant's trades over precomputed BOS events.

    Returns (trade_rows, agg) where agg counts setup-level outcomes.
    """
    bars_to_timeout = int(params.bars_to_timeout)
    high = df["high"].to_numpy(dtype=float)
    low = df["low"].to_numpy(dtype=float)
    close = df["close"].to_numpy(dtype=float)
    ts_col = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    n_bars = len(df)

    rows: list[TradeRow] = []
    agg = {
        "n_bos_total": len(events),
        "n_bos_dropped_active": 0,   # always 0 in research mode (no concurrency)
        "n_bos_filtered_bias": 0,
        "n_bos_filtered_min_swing": 0,
        "n_bos_filtered_leg_count": 0,
        "n_setups_pending": 0,
        "n_filled": 0,
        "n_invalidated": 0,
        "n_timed_out": 0,
        "n_cancelled_continuation": 0,
        "n_tp": 0,
        "n_sl": 0,
        "n_open_eod": 0,
    }

    # Precompute next-same-direction-BOS index per event. A pending setup
    # is cancelled when a continuation BOS in its direction fires (the
    # structural level we were going to retest is no longer the relevant
    # one -- price has structurally extended). The cancel applies only
    # to PENDING setups; once a setup fills, the live trade is decoupled
    # and the new BOS becomes its own independent setup.
    n_evts = len(events)
    next_same_dir_bos_idx: list[int] = [-1] * n_evts
    last_short_pos = -1
    last_long_pos = -1
    for k in range(n_evts - 1, -1, -1):
        e = events[k]
        if e.direction == "short":
            next_same_dir_bos_idx[k] = events[last_short_pos].bos_idx if last_short_pos >= 0 else n_bars
            last_short_pos = k
        else:
            next_same_dir_bos_idx[k] = events[last_long_pos].bos_idx if last_long_pos >= 0 else n_bars
            last_long_pos = k

    # Precompute leg-count per event: how many BIAS-ALIGNED same-direction
    # BOSes have occurred (this one inclusive) since the last opposite-
    # direction BOS. Bias-filtered same-dir BOSes don't increment the
    # counter (the strategy "didn't see" them as tradeable), but ANY
    # opposite-direction BOS resets the counter (structural shift).
    #
    # User rule: with min_leg_count=2 we skip the first tradeable BOS in
    # each leg as a potential false breakout; the textbook setup is the
    # 2nd-in-leg (May 13 12:00 short follows the May 12 15:00 first short
    # AFTER bias-flip; May 12 01:00 didn't pass bias and isn't counted).
    leg_count: list[int] = [0] * n_evts
    short_in_leg = 0
    long_in_leg = 0
    for k, e in enumerate(events):
        i = e.bos_idx
        bias_passes = False
        if i >= 1:
            if e.direction == "short":
                bias_passes = (bias[i] == "bearish" and bias[i - 1] == "bearish")
            else:
                bias_passes = (bias[i] == "bullish" and bias[i - 1] == "bullish")
        if e.direction == "short":
            long_in_leg = 0           # opposite-dir structural event resets
            if bias_passes:
                short_in_leg += 1
                leg_count[k] = short_in_leg
            else:
                leg_count[k] = 0      # bias-filtered: not counted
        else:
            short_in_leg = 0
            if bias_passes:
                long_in_leg += 1
                leg_count[k] = long_in_leg
            else:
                leg_count[k] = 0

    # Research mode: every qualifying BOS becomes its own independent
    # setup. We don't gate on a "one-trade-at-a-time" rule because R-unit
    # EV/PF/WR are per-trade-independent statistics — the user-level
    # decision about concurrency is risk management, not edge measurement.
    # (The v1.4 single-slot rule caused a 2025-11 trade going open_eod to
    # silently swallow ~6 months of subsequent setups.)
    setup_id = 0

    for ev_pos, ev in enumerate(events):
        # Bias filter: bias[i] aligned AND bias[i-1] also aligned (bias must
        # have flipped BEFORE the BOS bar, not on it).
        need = "bullish" if ev.direction == "long" else "bearish"
        if ev.bos_idx < 1:
            continue
        if bias[ev.bos_idx] != need or bias[ev.bos_idx - 1] != need:
            agg["n_bos_filtered_bias"] += 1
            continue

        # Min-swing filter.
        if ev.swing_size_pct < params.min_swing_pct:
            agg["n_bos_filtered_min_swing"] += 1
            continue

        # Leg-count filter: confirmation rule. The first N-1 same-direction
        # BOSes in a trend leg are treated as potential false breakouts
        # and skipped; only the N-th or later qualifies.
        if leg_count[ev_pos] < int(params.min_leg_count):
            agg["n_bos_filtered_leg_count"] += 1
            continue

        agg["n_setups_pending"] += 1
        setup_id += 1

        entry_price = _compute_entry_price(
            ev.direction, ev.retest_level, params.buffer_pct, params.entry_rule
        )

        # Walk forward from bos_idx+1 to detect fill / invalidation /
        # timeout / continuation-BOS cancel. The continuation cap is the
        # bar of the next same-direction BOS event -- inclusive in the
        # walk (intrabar wick can still fire the limit before end-of-bar
        # closes the new BOS), but if no fill on that bar the setup is
        # cancelled at end-of-bar.
        cont_idx = (next_same_dir_bos_idx[ev_pos]
                    if params.use_continuation_cancel else n_bars)
        timeout_last_idx = ev.bos_idx + bars_to_timeout
        # Upper-exclusive bound for the walk:
        upper_excl = min(
            n_bars,
            ev.bos_idx + 1 + bars_to_timeout,
            cont_idx + 1,   # include the continuation bar for intrabar fill
        )

        fill_idx = -1
        max_high_window = -np.inf
        min_low_window = np.inf
        outcome_pre_fill: Optional[str] = None
        last_walk_idx = ev.bos_idx
        for j in range(ev.bos_idx + 1, upper_excl):
            last_walk_idx = j
            bh = high[j]
            bl = low[j]
            bc = close[j]
            max_high_window = max(max_high_window, bh)
            min_low_window = min(min_low_window, bl)

            # Fill check: limit at entry_price.
            if ev.direction == "short":
                filled = bh >= entry_price
            else:
                filled = bl <= entry_price

            if filled:
                fill_idx = j
                break

            # Pre-fill invalidation: close back through retest in
            # opposing direction. (Continuation-BOS cancel below has
            # lower precedence than invalidation because invalidation
            # fires at this bar's close vs continuation cancellation
            # which lives at the NEXT same-dir BOS bar; if both happen
            # on the same bar AND that bar is also the continuation bar
            # AND it closes opposite of our setup -- impossible, since
            # a bos_down requires close BELOW the locked sL, opposite of
            # a bearish setup's invalidation needing close ABOVE the
            # retest level.)
            if ev.direction == "short" and bc > ev.retest_level:
                outcome_pre_fill = "cancelled_invalidated"
                break
            if ev.direction == "long" and bc < ev.retest_level:
                outcome_pre_fill = "cancelled_invalidated"
                break

        if fill_idx < 0:
            # No fill. Determine cancellation cause in priority:
            #   invalidation > continuation > timeout > end-of-data
            if outcome_pre_fill == "cancelled_invalidated":
                outcome = "cancelled_invalidated"
                agg["n_invalidated"] += 1
            elif cont_idx < n_bars and last_walk_idx >= cont_idx:
                outcome = "cancelled_continuation"
                agg["n_cancelled_continuation"] += 1
            elif last_walk_idx >= timeout_last_idx:
                outcome = "cancelled_timeout"
                agg["n_timed_out"] += 1
            elif last_walk_idx >= n_bars - 1:
                outcome = "unfilled"
                agg["n_timed_out"] += 1   # same bucket
            else:
                # Should not happen but be defensive.
                outcome = "cancelled_timeout"
                agg["n_timed_out"] += 1
            rows.append(_unfilled_row(
                setup_id, ev, params, ts_col, outcome,
            ))
            # No active hold; next BOS can proceed.
            continue

        # FILL — compute SL.
        # Tight SL = max(high) over (BOS, fill_idx] for short;
        #           min(low)  over (BOS, fill_idx] for long.
        # (max_high_window / min_low_window already reflect [BOS+1, fill_idx].)
        if params.sl_placement == "tight":
            if ev.direction == "short":
                sl_price = float(max_high_window)
            else:
                sl_price = float(min_low_window)
        else:  # wide
            sl_price = float(ev.pre_bos_swing_extreme)

        # Defensive: SL must be on the correct side of entry.
        if ev.direction == "short" and sl_price <= entry_price:
            sl_price = max(entry_price * (1.0 + 1e-6), sl_price)
        if ev.direction == "long" and sl_price >= entry_price:
            sl_price = min(entry_price * (1.0 - 1e-6), sl_price)

        # TP at fixed R-multiple.
        risk_per_unit = abs(entry_price - sl_price)
        if risk_per_unit <= 0:
            # Degenerate; treat as immediate cancel.
            rows.append(_unfilled_row(
                setup_id, ev, params, ts_col, "cancelled_invalidated",
            ))
            agg["n_invalidated"] += 1
            continue
        if ev.direction == "short":
            tp_price = entry_price - params.tp_rr * risk_per_unit
        else:
            tp_price = entry_price + params.tp_rr * risk_per_unit

        agg["n_filled"] += 1

        # Position sizing.
        risk_dollars = account * risk_pct
        position_size = risk_dollars / risk_per_unit

        # Resolve outcome from fill_idx onward.
        # Tie-breaker: if same bar reaches both SL and TP, assume SL first.
        # Entry bar must also be checked (e.g., short fill at high then
        # low could be below TP or above SL on same bar).
        exit_idx = -1
        exit_price: Optional[float] = None
        outcome = "open_eod"
        for k in range(fill_idx, n_bars):
            kh = high[k]
            kl = low[k]
            if ev.direction == "short":
                hit_sl = kh >= sl_price
                hit_tp = kl <= tp_price
                if hit_sl and hit_tp:
                    exit_idx = k
                    exit_price = sl_price
                    outcome = "sl"
                    break
                if hit_sl:
                    exit_idx = k
                    exit_price = sl_price
                    outcome = "sl"
                    break
                if hit_tp:
                    exit_idx = k
                    exit_price = tp_price
                    outcome = "tp"
                    break
            else:
                hit_sl = kl <= sl_price
                hit_tp = kh >= tp_price
                if hit_sl and hit_tp:
                    exit_idx = k
                    exit_price = sl_price
                    outcome = "sl"
                    break
                if hit_sl:
                    exit_idx = k
                    exit_price = sl_price
                    outcome = "sl"
                    break
                if hit_tp:
                    exit_idx = k
                    exit_price = tp_price
                    outcome = "tp"
                    break

        if exit_idx < 0:
            # Open at end of data — close at last close.
            exit_idx = n_bars - 1
            exit_price = float(close[exit_idx])
            outcome = "open_eod"
            agg["n_open_eod"] += 1
        elif outcome == "sl":
            agg["n_sl"] += 1
        elif outcome == "tp":
            agg["n_tp"] += 1

        # PnL.
        sign = 1.0 if ev.direction == "long" else -1.0
        gross = sign * position_size * (exit_price - entry_price)
        fees = position_size * (entry_price + exit_price) * fee_rate
        net = gross - fees
        r_gross = gross / risk_dollars
        r_net = net / risk_dollars

        rows.append(TradeRow(
            setup_id=setup_id,
            direction=ev.direction,
            bos_ts=ev.bos_ts,
            bos_idx=ev.bos_idx,
            bos_close=ev.bos_close,
            retest_level=ev.retest_level,
            pre_bos_swing_extreme=ev.pre_bos_swing_extreme,
            swing_size_pct=ev.swing_size_pct,
            entry_rule=params.entry_rule,
            buffer_pct=params.buffer_pct,
            sl_placement=params.sl_placement,
            tp_rr=params.tp_rr,
            min_swing_pct=params.min_swing_pct,
            bars_to_timeout=int(params.bars_to_timeout),
            fill_ts=ts_col.iloc[fill_idx],
            fill_idx=fill_idx,
            entry_price=float(entry_price),
            sl_price=float(sl_price),
            tp_price=float(tp_price),
            exit_ts=ts_col.iloc[exit_idx],
            exit_idx=exit_idx,
            exit_price=float(exit_price),
            outcome=outcome,
            r_gross=round(r_gross, 4),
            r_net=round(r_net, 4),
            gross_pnl_quote=round(gross, 4),
            fees_quote=round(fees, 4),
            net_pnl_quote=round(net, 4),
            position_size=round(position_size, 6),
            risk_dollars=risk_dollars,
        ))

    return rows, agg


def _unfilled_row(setup_id: int, ev: BOSEvent, params: VariantParams,
                  ts_col, outcome: str) -> TradeRow:
    """Build a TradeRow for setups that never filled (cancelled / unfilled).
    R / PnL fields are zero."""
    return TradeRow(
        setup_id=setup_id,
        direction=ev.direction,
        bos_ts=ev.bos_ts,
        bos_idx=ev.bos_idx,
        bos_close=ev.bos_close,
        retest_level=ev.retest_level,
        pre_bos_swing_extreme=ev.pre_bos_swing_extreme,
        swing_size_pct=ev.swing_size_pct,
        entry_rule=params.entry_rule,
        buffer_pct=params.buffer_pct,
        sl_placement=params.sl_placement,
        tp_rr=params.tp_rr,
        min_swing_pct=params.min_swing_pct,
        bars_to_timeout=int(params.bars_to_timeout),
        fill_ts=None,
        fill_idx=-1,
        entry_price=float("nan"),
        sl_price=float("nan"),
        tp_price=float("nan"),
        exit_ts=None,
        exit_idx=-1,
        exit_price=None,
        outcome=outcome,
        r_gross=0.0,
        r_net=0.0,
        gross_pnl_quote=0.0,
        fees_quote=0.0,
        net_pnl_quote=0.0,
        position_size=0.0,
        risk_dollars=0.0,
    )


# -- Aggregate stats --------------------------------------------------------

def summarize_variant(rows: list[TradeRow]) -> dict:
    """Compute summary stats for one variant from its trade rows.

    Only FILLED trades count toward N / WR / EV. Unfilled / cancelled
    setups are reported separately."""
    filled = [r for r in rows if r.outcome in ("tp", "sl", "open_eod")]
    n_filled = len(filled)
    if n_filled == 0:
        return {
            "n_filled": 0, "n_unfilled": len(rows) - n_filled,
            "wr": 0.0, "avg_win_R": 0.0, "avg_loss_R": 0.0,
            "ev_gross_R": 0.0, "ev_net_R": 0.0,
            "pf_gross": 0.0, "pf_net": 0.0,
            "max_dd_R": 0.0, "max_consec_losses": 0,
            "sum_net_R": 0.0,
            "r_min": 0.0, "r_max": 0.0,
        }
    r_gross = np.array([r.r_gross for r in filled], dtype=float)
    r_net = np.array([r.r_net for r in filled], dtype=float)
    wins_mask = r_gross > 0
    losses_mask = r_gross <= 0
    wr = float(wins_mask.mean())
    avg_win_R = float(r_gross[wins_mask].mean()) if wins_mask.any() else 0.0
    avg_loss_R = float(r_gross[losses_mask].mean()) if losses_mask.any() else 0.0
    ev_gross = float(r_gross.mean())
    ev_net = float(r_net.mean())
    sum_wins_gross = float(r_gross[wins_mask].sum())
    sum_losses_gross_abs = float(-r_gross[losses_mask].sum())
    pf_gross = (sum_wins_gross / sum_losses_gross_abs) if sum_losses_gross_abs > 0 else float("inf")
    sum_wins_net = float(r_net[r_net > 0].sum())
    sum_losses_net_abs = float(-r_net[r_net <= 0].sum())
    pf_net = (sum_wins_net / sum_losses_net_abs) if sum_losses_net_abs > 0 else float("inf")

    eq = np.cumsum(r_net)
    peaks = np.maximum.accumulate(eq)
    dd = peaks - eq
    max_dd = float(dd.max())

    # Max consecutive losses (net).
    losers = (r_net <= 0).astype(int)
    max_streak = cur = 0
    for x in losers:
        cur = cur + 1 if x else 0
        if cur > max_streak:
            max_streak = cur

    return {
        "n_filled": int(n_filled),
        "n_unfilled": int(len(rows) - n_filled),
        "wr": round(wr, 4),
        "avg_win_R": round(avg_win_R, 4),
        "avg_loss_R": round(avg_loss_R, 4),
        "ev_gross_R": round(ev_gross, 4),
        "ev_net_R": round(ev_net, 4),
        "pf_gross": round(pf_gross, 4) if np.isfinite(pf_gross) else None,
        "pf_net": round(pf_net, 4) if np.isfinite(pf_net) else None,
        "max_dd_R": round(max_dd, 4),
        "max_consec_losses": int(max_streak),
        "sum_net_R": round(float(eq[-1]), 4),
        "r_min": round(float(r_net.min()), 4),
        "r_max": round(float(r_net.max()), 4),
    }


def trade_row_to_dict(r: TradeRow) -> dict:
    d = asdict(r)
    # Make timestamps CSV-friendly.
    for key in ("bos_ts", "fill_ts", "exit_ts"):
        v = d.get(key)
        if v is not None:
            d[key] = pd.Timestamp(v).isoformat()
    return d


__all__ = [
    "BOSEvent", "TradeRow", "VariantParams",
    "compute_ema_bias", "precompute_bos_events",
    "run_variant", "summarize_variant", "trade_row_to_dict",
]
