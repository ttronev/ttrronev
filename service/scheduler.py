"""
service/scheduler.py — pure bar-close arithmetic for the worker loop.

All service TFs except 1w align to the epoch (00:00 UTC): 5m/1h/2h/4h/1d
interval lengths divide a UTC day, so `((now // interval) + 1) * interval`
is the next close. 1w does NOT use epoch-week modulo (the epoch started on
a Thursday, while OKX 1Wutc candles do not) — the 1w slot instead ticks at
every DAILY boundary and the worker regenerates only if the fetch actually
returned a new weekly bar. A quiet daily tick costs one cheap HTTP check.
"""
from __future__ import annotations

from service.pairs import TF_MS, CLOSE_BUFFER_S

DAY_MS = TF_MS["1d"]
CLOSE_BUFFER_MS = CLOSE_BUFFER_S * 1000


def tick_interval_ms(tf: str) -> int:
    """The scheduling interval for a TF (1w rides the daily boundary)."""
    return DAY_MS if tf == "1w" else TF_MS[tf]


def next_run_at_ms(tf: str, now_ms: int, buffer_s: int = CLOSE_BUFFER_S) -> int:
    """Next bar-close boundary strictly after now, plus the publish buffer."""
    interval = tick_interval_ms(tf)
    next_close = (now_ms // interval + 1) * interval
    return next_close + buffer_s * 1000
