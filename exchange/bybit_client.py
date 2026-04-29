"""Bybit v5 client wrapper for ttrronev.

Wraps Bybit's REST + WebSocket v5 API for USDT-margined perpetual futures.
Provides:

- Historical kline (OHLCV) backfill -> data/raw/ohlcv/
- Funding-rate history backfill    -> data/raw/funding/
- WebSocket streaming klines       (for live / paper trading)
- Order placement                  (only enabled in --live mode)

This is a starter skeleton. Network calls are stubbed; fill them in against
pybit (https://github.com/bybit-exchange/pybit) or raw httpx before going live.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Iterable, Iterator

logger = logging.getLogger(__name__)

APPROVED_SYMBOLS = (
    "BTCUSDT",
    "ETHUSDT",
    "XRPUSDT",
    "SOLUSDT",
    "DOGEUSDT",
    "TRXUSDT",
    "HYPEUSDT",
    "ADAUSDT",
)

BYBIT_REST_MAINNET = "https://api.bybit.com"
BYBIT_REST_TESTNET = "https://api-testnet.bybit.com"
BYBIT_WS_LINEAR_MAINNET = "wss://stream.bybit.com/v5/public/linear"
BYBIT_WS_LINEAR_TESTNET = "wss://stream-testnet.bybit.com/v5/public/linear"

VALID_INTERVALS = ("1", "3", "5", "15", "30", "60", "120", "240", "360", "720", "D", "W")


@dataclass(frozen=True)
class Kline:
    symbol: str
    interval: str
    open_time: int     # ms epoch
    open: float
    high: float
    low: float
    close: float
    volume: float
    turnover: float


@dataclass(frozen=True)
class FundingRate:
    symbol: str
    funding_time: int  # ms epoch
    funding_rate: float


@dataclass
class BybitConfig:
    api_key: str | None = None
    api_secret: str | None = None
    testnet: bool = True
    recv_window_ms: int = 5000

    @classmethod
    def from_env(cls) -> "BybitConfig":
        return cls(
            api_key=os.getenv("BYBIT_API_KEY"),
            api_secret=os.getenv("BYBIT_API_SECRET"),
            testnet=os.getenv("BYBIT_TESTNET", "true").lower() == "true",
        )

    @property
    def rest_base(self) -> str:
        return BYBIT_REST_TESTNET if self.testnet else BYBIT_REST_MAINNET

    @property
    def ws_linear(self) -> str:
        return BYBIT_WS_LINEAR_TESTNET if self.testnet else BYBIT_WS_LINEAR_MAINNET


class BybitClient:
    """Thin wrapper around Bybit v5 REST + WebSocket for the linear category."""

    def __init__(self, config: BybitConfig | None = None):
        self.config = config or BybitConfig.from_env()

    # --- validation -------------------------------------------------------------------------

    @staticmethod
    def _validate_symbol(symbol: str) -> None:
        if symbol not in APPROVED_SYMBOLS:
            raise ValueError(
                f"Symbol {symbol!r} is not in the approved trading universe: "
                f"{APPROVED_SYMBOLS}"
            )

    @staticmethod
    def _validate_interval(interval: str) -> None:
        if interval not in VALID_INTERVALS:
            raise ValueError(
                f"Interval {interval!r} not in Bybit v5 valid set: {VALID_INTERVALS}"
            )

    # --- REST: historical data --------------------------------------------------------------

    def fetch_klines(
        self,
        symbol: str,
        interval: str,
        start_ms: int,
        end_ms: int,
    ) -> Iterator[Kline]:
        """Yield klines for [start_ms, end_ms]. Pagination handled internally."""
        self._validate_symbol(symbol)
        self._validate_interval(interval)
        # TODO: call GET /v5/market/kline with category=linear and paginate.
        logger.info("fetch_klines stub: %s %s %d -> %d", symbol, interval, start_ms, end_ms)
        return iter(())

    def fetch_funding(self, symbol: str, start_ms: int, end_ms: int) -> Iterator[FundingRate]:
        """Yield funding rates for [start_ms, end_ms]."""
        self._validate_symbol(symbol)
        # TODO: call GET /v5/market/funding/history with category=linear.
        logger.info("fetch_funding stub: %s %d -> %d", symbol, start_ms, end_ms)
        return iter(())

    # --- WebSocket: live streams ------------------------------------------------------------

    def stream_klines(self, symbols: Iterable[str], interval: str):
        """Async generator yielding closed klines as they arrive on the WS."""
        symbols = list(symbols)
        for s in symbols:
            self._validate_symbol(s)
        self._validate_interval(interval)
        # TODO: connect to BYBIT_WS_LINEAR, subscribe kline.{interval}.{symbol}, yield Klines.
        raise NotImplementedError("stream_klines not implemented yet")

    # --- REST: trading (gated) --------------------------------------------------------------

    def place_order(self, *args, **kwargs):
        if self.config.testnet is False and not os.getenv("TTRRONEV_LIVE_OK"):
            raise RuntimeError(
                "Refusing to place a live order: TTRRONEV_LIVE_OK env var is not set. "
                "Live trading is opt-in to prevent accidental fills."
            )
        # TODO: implement POST /v5/order/create.
        raise NotImplementedError("place_order not implemented yet")


def main() -> None:
    raise SystemExit(
        "exchange.bybit_client is a library. Use a CLI wrapper (e.g. tools/fetch_history.py) "
        "to back-fill data, and call BybitClient from the bot for live streams."
    )


if __name__ == "__main__":
    main()
