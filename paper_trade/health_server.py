"""Tiny aiohttp /health endpoint for external supervision.

Returns 200 + JSON body when:
  * Last 5m bar received per pair is within HEALTH_STALE_BARS * 5min
  * No fatal error tripped the bot

Returns 503 + JSON body when stale or in failure state. The external
cron (see deploy README) pings this every 5 minutes; two consecutive
fails => Telegram alert from the cron itself.

The health server runs in the same asyncio loop as the rest of the bot.
Public state is shared via a `HealthState` dataclass passed by reference.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional

import aiohttp.web as web

from paper_trade.config import (
    HEALTH_HOST, HEALTH_PORT, HEALTH_STALE_BARS, PHASE,
)


@dataclass
class HealthState:
    started_ms: int = field(default_factory=lambda: int(time.time() * 1000))
    last_bar_ms: dict[str, int] = field(default_factory=dict)        # pair -> last 5m bar ts
    n_open_positions: int = 0
    equity: float = 0.0
    last_error: Optional[str] = None
    last_validation_ok: Optional[bool] = None
    last_validation_day: Optional[str] = None

    def mark_bar(self, pair: str, ts_ms: int) -> None:
        prior = self.last_bar_ms.get(pair, 0)
        if ts_ms > prior:
            self.last_bar_ms[pair] = ts_ms

    def staleness_ok(self) -> bool:
        if not self.last_bar_ms:
            return True  # not started yet — let startup grace
        now_ms = int(time.time() * 1000)
        threshold = HEALTH_STALE_BARS * 5 * 60 * 1000
        for ts in self.last_bar_ms.values():
            if now_ms - ts > threshold:
                return False
        return True


def make_app(state: HealthState) -> web.Application:
    app = web.Application()

    async def health(request: web.Request) -> web.Response:
        ok = state.staleness_ok() and state.last_error is None
        body = {
            "status": "ok" if ok else "degraded",
            "phase": PHASE,
            "uptime_ms": int(time.time() * 1000) - state.started_ms,
            "last_bar_ms": dict(state.last_bar_ms),
            "n_open_positions": state.n_open_positions,
            "last_error": state.last_error,
            "last_validation_ok": state.last_validation_ok,
            "last_validation_day": state.last_validation_day,
        }
        status = 200 if ok else 503
        return web.json_response(body, status=status)

    app.router.add_get("/health", health)
    return app


async def start_server(state: HealthState) -> tuple[web.AppRunner, web.TCPSite]:
    app = make_app(state)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, HEALTH_HOST, HEALTH_PORT)
    await site.start()
    return runner, site


__all__ = ["HealthState", "make_app", "start_server"]
