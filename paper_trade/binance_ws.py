"""Async Binance WebSocket kline client.

Subscribes to kline streams for a list of (pair, tf) combinations and
emits a callback on every CLOSED bar. Auto-reconnects with exponential
backoff. Logs reconnect events to runtime_events.

Binance combined-stream URL:
    wss://stream.binance.com:9443/stream?streams=solusdt@kline_5m/avaxusdt@kline_5m/...

Each frame on a combined stream is wrapped in {stream, data}. The kline
payload at data.k.x indicates whether the bar has closed.

We rely on aiohttp.ClientSession.ws_connect for the underlying
WebSocket (one less dep vs the standalone `websockets` library).
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Awaitable, Callable, Iterable, Optional

import aiohttp

from paper_trade.bar_buffer import Bar
from paper_trade.config import BINANCE_WS_URL
from paper_trade.sqlite_store import log_runtime


_LOG = logging.getLogger("paper_trade.binance_ws")


def _stream_id(pair: str, tf: str) -> str:
    sym = pair.replace("/", "").lower()
    return f"{sym}@kline_{tf}"


# Type alias: callback fired on every closed bar.
ClosedBarCb = Callable[[str, str, Bar], Awaitable[None]]


class BinanceKlineWS:
    """Long-running WebSocket consumer.

    Optional ``on_reconnect`` callback runs after every successful
    (re)connect, BEFORE the message loop processes any frame. This is
    where the live bot does REST gap-fill to backfill any bars that
    closed during the disconnect window. The message loop is naturally
    blocked while ``on_reconnect`` is awaiting — aiohttp buffers
    incoming WS frames until we re-enter ``async for msg in ws:``, so
    the engine never sees a partial buffer.
    """

    def __init__(self, pair_tfs: Iterable[tuple[str, str]],
                 on_closed_bar: ClosedBarCb,
                 on_reconnect: Optional[Callable[[], Awaitable[None]]] = None) -> None:
        self._pair_tfs: list[tuple[str, str]] = list(pair_tfs)
        self._on_closed_bar = on_closed_bar
        self._on_reconnect = on_reconnect
        self._stream_to_pair_tf: dict[str, tuple[str, str]] = {
            _stream_id(p, t): (p, t) for p, t in self._pair_tfs
        }
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    def _build_url(self) -> str:
        streams = "/".join(self._stream_to_pair_tf.keys())
        return f"{BINANCE_WS_URL}?streams={streams}"

    async def run(self) -> None:
        backoff = 1.0
        url = self._build_url()
        async with aiohttp.ClientSession() as session:
            while not self._stop.is_set():
                try:
                    log_runtime("ws_connecting", {"streams": list(self._stream_to_pair_tf.keys())})
                    async with session.ws_connect(url, heartbeat=30,
                                                   max_msg_size=4 * 1024 * 1024) as ws:
                        log_runtime("ws_connected", {"streams_count": len(self._stream_to_pair_tf)})
                        # Run the reconnect handler BEFORE accepting any
                        # WS frames. If it raises, the outer try/except
                        # treats it as a connection failure and we retry
                        # with backoff. Engine never sees a partial buffer.
                        if self._on_reconnect is not None:
                            await self._on_reconnect()
                        backoff = 1.0
                        async for msg in ws:
                            if self._stop.is_set():
                                break
                            if msg.type == aiohttp.WSMsgType.TEXT:
                                await self._handle_text(msg.data)
                            elif msg.type in (aiohttp.WSMsgType.CLOSED,
                                              aiohttp.WSMsgType.ERROR):
                                _LOG.warning("ws closed/error: %s", msg.type)
                                break
                except Exception as e:
                    _LOG.warning("ws loop error: %s", type(e).__name__)
                    log_runtime("ws_disconnected", {"reason": type(e).__name__})
                if self._stop.is_set():
                    break
                # Backoff before reconnect; cap at 60s.
                wait = min(60.0, backoff)
                _LOG.info("ws reconnecting in %.1fs", wait)
                await asyncio.sleep(wait)
                backoff = min(60.0, backoff * 2.0)

    async def _handle_text(self, raw: str) -> None:
        try:
            envelope = json.loads(raw)
        except json.JSONDecodeError:
            return
        stream = envelope.get("stream")
        data = envelope.get("data")
        if not stream or not data:
            return
        k = data.get("k")
        if not k:
            return
        if not k.get("x"):
            # Bar still forming; ignore until close.
            return
        pair_tf = self._stream_to_pair_tf.get(stream)
        if pair_tf is None:
            return
        pair, tf = pair_tf
        try:
            bar = Bar(
                timestamp_ms=int(k["t"]),
                open=float(k["o"]),
                high=float(k["h"]),
                low=float(k["l"]),
                close=float(k["c"]),
                volume=float(k["v"]),
            )
        except (KeyError, ValueError, TypeError):
            return
        try:
            await self._on_closed_bar(pair, tf, bar)
        except Exception as e:
            _LOG.exception("closed-bar handler failed: %s", type(e).__name__)


__all__ = ["BinanceKlineWS", "ClosedBarCb"]
