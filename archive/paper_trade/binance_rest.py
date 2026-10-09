"""Async Binance REST client — kline history.

Used for:
  - Initial buffer hydration on startup (fetch ~24 days of 5m + ~146
    days of 1h per pair if SQLite is empty)
  - Restart gap-fill (fetch any closed bars that arrived while the bot
    was down)

Rate limit: Binance public-API klines weight is 1 per request, hard
cap 1200/min. We sleep 100ms between requests for safety; serial.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

import aiohttp

from paper_trade.bar_buffer import Bar
from paper_trade.config import BINANCE_REST_URL


_LOG = logging.getLogger("paper_trade.binance_rest")

INTERVAL_MS = {
    "1m":  60_000,
    "5m":  300_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "1h":  3_600_000,
    "4h":  14_400_000,
    "1d":  86_400_000,
}


# Note: we DO NOT add 1m to BinanceKlineWS's reconnect gap-fill set —
# the 1m intrabar path is a best-effort latency optimization, not a
# durability guarantee. After a disconnect, when the next 5m bar
# closes, the engine's regular `step()` re-evaluates the candidate
# against the 5m's high/low, which subsumes whatever the missed 1m
# bars saw. Backtest already used 5m-only wick-fills, so the absence
# of 1m gap-fill does not introduce divergence.

LIMIT = 1000  # Binance max per request


def _symbol(pair: str) -> str:
    return pair.replace("/", "").upper()


async def fetch_klines(session: aiohttp.ClientSession, pair: str, tf: str,
                       start_ms: int, end_ms: Optional[int] = None,
                       max_bars: Optional[int] = None) -> list[Bar]:
    """Fetch klines from Binance public REST. Returns CLOSED bars only.

    The last open bar is filtered out (Binance returns it with a still-
    advancing close time)."""
    out: list[Bar] = []
    cursor = start_ms
    interval_ms = INTERVAL_MS[tf]
    while True:
        params = {
            "symbol": _symbol(pair),
            "interval": tf,
            "startTime": cursor,
            "limit": LIMIT,
        }
        if end_ms is not None:
            params["endTime"] = end_ms
        try:
            async with session.get(BINANCE_REST_URL, params=params, timeout=30) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    raise RuntimeError(f"binance REST {resp.status}: {body[:200]}")
                rows = await resp.json()
        except Exception as e:
            _LOG.warning("REST fetch failed (%s): %s — sleeping 5s",
                         pair, type(e).__name__)
            await asyncio.sleep(5)
            continue
        if not rows:
            break
        # Filter: keep bars where openTime + interval <= now (closed).
        import time as _t
        now_ms = int(_t.time() * 1000)
        for r in rows:
            open_ts = int(r[0])
            if open_ts + interval_ms > now_ms:
                # Still-forming bar; skip.
                continue
            if end_ms is not None and open_ts > end_ms:
                continue
            bar = Bar(
                timestamp_ms=open_ts,
                open=float(r[1]),
                high=float(r[2]),
                low=float(r[3]),
                close=float(r[4]),
                volume=float(r[5]),
            )
            out.append(bar)
            if max_bars is not None and len(out) >= max_bars:
                return out
        if len(rows) < LIMIT:
            break
        last_ts = int(rows[-1][0])
        cursor = last_ts + interval_ms
        await asyncio.sleep(0.1)
    return out


async def fetch_recent(session: aiohttp.ClientSession, pair: str, tf: str,
                       n_bars: int) -> list[Bar]:
    """Fetch the most recent `n_bars` closed bars."""
    import time as _t
    now_ms = int(_t.time() * 1000)
    interval_ms = INTERVAL_MS[tf]
    start_ms = now_ms - (n_bars + 5) * interval_ms  # +5 buffer
    return await fetch_klines(session, pair, tf, start_ms, max_bars=n_bars + 5)


async def fetch_gap(session: aiohttp.ClientSession, pair: str, tf: str,
                    last_ts_ms: int) -> list[Bar]:
    """Fetch all closed bars after `last_ts_ms` up to now."""
    interval_ms = INTERVAL_MS[tf]
    return await fetch_klines(session, pair, tf, last_ts_ms + interval_ms)


__all__ = ["fetch_klines", "fetch_recent", "fetch_gap", "INTERVAL_MS"]
