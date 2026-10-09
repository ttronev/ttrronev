"""SOL+BTC 5m Secondary-Only v1.3 — derived from symmetric_bos_fib_v1.

Drops the primary trade entirely. Primary's geometry is used only as a
structural filter — wick must reach fib_1.0 (the swing extreme) before
secondary can be placed. No primary fees, no primary risk.

Secondary trade (bearish BOS — mirror for bullish):
    Entry:  limit long at fib_0.5 (filled on wick low <= fib_0.5)
    TP:     fib_1.0 (the swing high) — filled on wick high >= fib_1.0
    SL:     5m candle close below fib_0.3 — exit at NEXT candle open

State machine per setup:
    WAIT_BOS              — no setup; watching engine for new BOS event
    AWAITING_RATCHET      — BOS fired; waiting for engine to lock swing
                             via single ratchet
    AWAITING_ARM          — fibs computed; waiting for wick to fib_0.75
    AWAITING_PRIMARY_TRIG — armed; waiting for wick to fib_1.0
                             (= structural filter; no order placed)
    SECONDARY_PENDING     — primary trigger observed; secondary limit at
                             fib_0.5 placed; watching for fill OR cancel
                             on close beyond fib_1.2
    SECONDARY_LIVE        — secondary filled; watching SL (close beyond
                             fib_0.3) / TP (wick to fib_1.0)

Invalidations (priority order):
    Before primary trigger:        close beyond fib_0  -> discard
    After primary trigger,
        before secondary fills:    close beyond fib_1.2 -> cancel
    After secondary fills:         own TP/SL only

One setup at a time; queue up to one BOS event while active. Queue
mechanism: when active setup resolves, replay bars from queued BOS bar
forward to determine current phase (retroactive check for arming /
trigger / invalidation / cancel that happened during prior active).

No filters: no regime gate, no swing-size cap (only min 1.0%), no session
filter. Pair-agnostic. 1% account risk per secondary trade.
"""

from __future__ import annotations


CONFIG: dict = {
    "strategy_id":   "secondary_only_v1",
    "strategy_name": "Secondary-Only v1.3 (BOS-fib, primary as filter)",

    # Timeframes
    "exec_tf":  "5m",
    "trend_tf": "1h",

    # BOS engine
    "reversal_pct_5m": 0.005,
    "init_bars":       50,

    # Strategy filters
    "min_swing_pct":   1.0,    # only filter — no max
    # v1.4: regime gate — at BOS time, compute 90-day rolling return on
    # the 1h closes; require |return| >= regime_gate_pct to take setup.
    # 0.0 = gate disabled (v1.3 behavior).
    "regime_gate_pct": 0.0,
    "regime_lookback_days": 90,
    # v1.5: duration confirmation. At BOS, look back regime_duration_bars
    # 1h bars; ALL of them must satisfy |return| >= regime_gate_pct for
    # the gate to pass. Filters out transient regime triggers (e.g., a
    # 7-day price drop that only briefly shows -30% rolling return).
    # 0 or 1 = no duration check (v1.4 behavior).
    "regime_duration_bars": 0,

    # Fib levels (full mirror)
    "fib_armed":         0.75,
    "fib_primary_trig":  1.0,   # primary's hypothetical entry (structural filter)
    "fib_secondary_entry": 0.5,
    "fib_secondary_tp":  1.0,
    "fib_invalidation":  0.0,
    "fib_secondary_sl":  0.3,
    "fib_primary_sl":    1.2,   # used to cancel secondary if hit pre-fill

    # 1h bias EMAs (logging only)
    "htf_ema_fast": 12,
    "htf_ema_slow": 21,

    # Risk
    "risk_pct":           0.01,
    "taker_fee":          0.00055,
    "enforce_notional_cap": True,
}


PAIRS: tuple[str, ...] = ("SOL/USDT", "BTC/USDT")


__all__ = ["CONFIG", "PAIRS"]
