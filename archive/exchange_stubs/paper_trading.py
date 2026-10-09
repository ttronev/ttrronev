"""Paper-trading simulator for ttrronev.

Streams live Bybit data via BybitClient, asks a Strategy for orders, and
simulates fills using the same fee/slippage model as the backtester. No real
orders are placed. Trades are logged to logs/trades/<session_id>.jsonl.

A strategy must run here for ≥30 days with realized Sharpe ≥ 0.7 before it is
eligible for live deployment (see personality.md).
"""

from __future__ import annotations

import json
import logging
import signal
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from .bybit_client import APPROVED_SYMBOLS, BybitClient

logger = logging.getLogger(__name__)


@dataclass
class PaperPosition:
    symbol: str
    qty: float = 0.0
    entry_price: float = 0.0
    opened_at: int = 0


@dataclass
class PaperPortfolio:
    equity: float
    cash: float
    positions: dict[str, PaperPosition] = field(default_factory=dict)


@dataclass
class PaperConfig:
    starting_equity: float = 10_000.0
    taker_fee: float = 0.00055
    maker_fee: float = 0.0002
    slippage_ticks: int = 1
    max_leverage: float = 3.0
    max_positions: int = 4
    per_trade_risk: float = 0.01
    daily_loss_limit: float = 0.04
    log_dir: Path = field(default_factory=lambda: Path("logs/trades"))
    error_dir: Path = field(default_factory=lambda: Path("logs/errors"))
    interval: str = "60"   # 1h klines drive the simulator by default


class PaperTrader:
    """Simulates trading against live Bybit data without sending real orders."""

    def __init__(
        self,
        strategy,
        symbols: Iterable[str] = APPROVED_SYMBOLS,
        config: PaperConfig | None = None,
        client: BybitClient | None = None,
    ):
        self.strategy = strategy
        self.symbols = tuple(symbols)
        for s in self.symbols:
            if s not in APPROVED_SYMBOLS:
                raise ValueError(f"Symbol {s!r} is not in approved universe.")
        self.config = config or PaperConfig()
        self.client = client or BybitClient()
        self.portfolio = PaperPortfolio(
            equity=self.config.starting_equity,
            cash=self.config.starting_equity,
        )
        self.session_id = self._make_session_id()
        self.config.log_dir.mkdir(parents=True, exist_ok=True)
        self.config.error_dir.mkdir(parents=True, exist_ok=True)
        self._trade_log = (self.config.log_dir / f"{self.session_id}.jsonl").open("w")
        self._stop = False

    def _make_session_id(self) -> str:
        ts = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        return f"paper_{ts}_{self.strategy.name}_{uuid.uuid4().hex[:6]}"

    # --- main loop --------------------------------------------------------------------------

    def run(self) -> None:
        """Block forever, processing closed klines as they arrive."""
        signal.signal(signal.SIGINT, self._handle_sigint)
        signal.signal(signal.SIGTERM, self._handle_sigint)
        logger.info(
            "paper-trade session %s starting on %d symbols (%s)",
            self.session_id, len(self.symbols), self.config.interval,
        )
        try:
            self._stream_loop()
        except Exception:
            self._record_error()
            raise
        finally:
            self._trade_log.close()
            logger.info("paper-trade session %s stopped", self.session_id)

    def _stream_loop(self) -> None:
        # TODO: replace with real `for kline in self.client.stream_klines(...)`.
        logger.warning(
            "PaperTrader._stream_loop is a stub; wire BybitClient.stream_klines() in."
        )
        while not self._stop:
            time.sleep(1)

    # --- signal / risk handling -------------------------------------------------------------

    def _handle_sigint(self, *_a) -> None:
        logger.info("received stop signal, flattening positions and exiting...")
        self._stop = True
        self._flatten_all("shutdown")

    def _flatten_all(self, reason: str) -> None:
        # TODO: emit closing fills for each open position.
        for sym, pos in list(self.portfolio.positions.items()):
            if pos.qty == 0.0:
                continue
            logger.info("flattening %s qty=%s reason=%s", sym, pos.qty, reason)

    def _record_error(self) -> None:
        import traceback
        path = self.config.error_dir / f"{self.session_id}.txt"
        path.write_text(traceback.format_exc())
        logger.error("error recorded to %s", path)

    def _log_event(self, event: dict) -> None:
        event.setdefault("ts", int(time.time() * 1000))
        event.setdefault("session", self.session_id)
        self._trade_log.write(json.dumps(event) + "\n")
        self._trade_log.flush()


def main() -> None:
    raise SystemExit(
        "exchange.paper_trading is a library; provide a Strategy and call "
        "PaperTrader(strategy).run() from a session script."
    )


if __name__ == "__main__":
    main()
