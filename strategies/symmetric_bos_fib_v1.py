"""SOL/USDT 5m Symmetric BOS-Fib Strategy v1.

Spec — see ``backtesting/symmetric_bos_fib_engine.py`` docstring for the
full state machine.

After each BOS event from ``shared.structure_analyzer``, identify the swing
(post-ratchet for trending side) and, if the swing is at least 1.2% wide,
arm a setup. When price wicks to fib 0.75, place the primary limit at fib
1.0 (the swing extreme). On primary fill, place the secondary limit at
fib 0.5 (opposite direction). Both legs targeted at the swing midpoint /
extreme respectively. SL on candle close past fib 1.2 (primary) or fib
0.3 (secondary) — exit at next candle open.

Only one setup is active at a time. While any setup is active (orders
pending OR trades live), all new BOS events from the engine are ignored.
"""

from __future__ import annotations


CONFIG: dict = {
    "strategy_id":    "symmetric_bos_fib_v1",
    "strategy_name":  "Symmetric BOS-Fib v1 (5m primary + 0.5 secondary)",

    # Timeframes
    "exec_tf":        "5m",
    "trend_tf":       "1h",

    # BOS engine
    "reversal_pct_5m": 0.005,
    "init_bars":       50,

    # Strategy filter
    "min_swing_pct":   1.5,
    "max_swing_pct":   2.0,

    # Fib levels (constants — full mirror)
    "fib_armed":         0.75,
    "fib_primary_entry": 1.0,
    "fib_primary_tp":    0.5,
    "fib_invalidation":  0.0,
    "fib_primary_sl":    1.2,
    "fib_secondary_sl":  0.3,

    # 1h bias (logging only)
    "htf_ema_fast":   12,
    "htf_ema_slow":   21,

    # Risk
    "risk_pct":           0.01,
    "taker_fee":          0.00055,
    "enforce_notional_cap": True,
}


PAIRS: tuple[str, ...] = ("SOL/USDT",)
BTC_PAIR: str = "BTC/USDT"


__all__ = ["CONFIG", "PAIRS", "BTC_PAIR"]
