"""Event-driven backtesting engine for ttrronev.

The engine replays historical OHLCV bars one at a time, asks a Strategy object
for orders, and simulates fills with realistic fees, slippage, and funding.
Equity curves and trade logs are written to backtesting/results/<run_id>/.

This is a starter skeleton — fills, funding, and metrics are stubbed and must
be implemented before any backtest result can be trusted.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator, Protocol

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

# Bybit perp fee tier assumed for the bot. Override per-run if needed.
DEFAULT_TAKER_FEE = 0.00055  # 5.5 bps
DEFAULT_MAKER_FEE = 0.0002   # 2.0 bps
DEFAULT_SLIPPAGE_TICKS = 1
FUNDING_HOURS_UTC = (0, 8, 16)


@dataclass(frozen=True)
class Bar:
    symbol: str
    open_time: int       # ms epoch, UTC
    open: float
    high: float
    low: float
    close: float
    volume: float
    turnover: float


@dataclass(frozen=True)
class Order:
    symbol: str
    side: str            # "long" | "short" | "flat"
    qty: float           # base-asset units; sign matches side
    order_type: str = "market"  # "market" | "limit"
    limit_price: float | None = None
    reason: str = ""     # free-form, written to trade log


@dataclass
class Position:
    symbol: str
    qty: float = 0.0     # signed: + long, - short
    entry_price: float = 0.0
    opened_at: int = 0   # ms epoch

    @property
    def is_flat(self) -> bool:
        return self.qty == 0.0


@dataclass
class Fill:
    order: Order
    price: float
    fee: float
    timestamp: int


@dataclass
class BacktestConfig:
    starting_equity: float = 10_000.0
    taker_fee: float = DEFAULT_TAKER_FEE
    maker_fee: float = DEFAULT_MAKER_FEE
    slippage_ticks: int = DEFAULT_SLIPPAGE_TICKS
    max_leverage: float = 3.0
    max_positions: int = 4
    per_trade_risk: float = 0.01
    daily_loss_limit: float = 0.04
    results_dir: Path = field(default_factory=lambda: Path("backtesting/results"))


class Strategy(Protocol):
    """Anything that can convert bars into orders is a Strategy."""

    name: str

    def on_bar(self, bar: Bar, portfolio: "Portfolio") -> Iterable[Order]: ...


@dataclass
class Portfolio:
    equity: float
    positions: dict[str, Position] = field(default_factory=dict)
    realized_pnl: float = 0.0

    def position(self, symbol: str) -> Position:
        return self.positions.setdefault(symbol, Position(symbol=symbol))


class BacktestEngine:
    """Replay bars and simulate trading."""

    def __init__(self, config: BacktestConfig, strategy: Strategy):
        self.config = config
        self.strategy = strategy
        self.portfolio = Portfolio(equity=config.starting_equity)
        self.run_id = self._make_run_id()
        self.results_path = config.results_dir / self.run_id
        self.results_path.mkdir(parents=True, exist_ok=True)
        self._trade_log = (self.results_path / "trades.jsonl").open("w")
        self._equity_curve: list[tuple[int, float]] = []

    def _make_run_id(self) -> str:
        ts = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        return f"{ts}_{self.strategy.name}_{uuid.uuid4().hex[:6]}"

    def run(self, bars: Iterator[Bar]) -> dict:
        for bar in bars:
            if bar.symbol not in APPROVED_SYMBOLS:
                raise ValueError(
                    f"Symbol {bar.symbol!r} not in approved universe; refusing to backtest."
                )
            self._maybe_charge_funding(bar)
            orders = list(self.strategy.on_bar(bar, self.portfolio))
            for order in orders:
                fill = self._simulate_fill(order, bar)
                self._apply_fill(fill)
                self._log_trade(fill, bar)
            self._mark_to_market(bar)
        self._trade_log.close()
        metrics = self._compute_metrics()
        (self.results_path / "metrics.json").write_text(json.dumps(metrics, indent=2))
        return metrics

    # --- fill / accounting stubs (TO BE IMPLEMENTED) -----------------------------------------

    def _simulate_fill(self, order: Order, bar: Bar) -> Fill:
        # Naive market-order fill at next bar's open with slippage; refine before trusting results.
        slip = self.config.slippage_ticks * 0.0001 * bar.close  # placeholder tick size
        if order.side == "long":
            price = bar.close + slip
        elif order.side == "short":
            price = bar.close - slip
        else:
            price = bar.close
        fee = abs(order.qty) * price * self.config.taker_fee
        return Fill(order=order, price=price, fee=fee, timestamp=bar.open_time)

    def _apply_fill(self, fill: Fill) -> None:
        pos = self.portfolio.position(fill.order.symbol)
        # TODO: handle partial closes, flips, and signed qty correctly.
        pos.qty += fill.order.qty if fill.order.side == "long" else -fill.order.qty
        if pos.entry_price == 0.0:
            pos.entry_price = fill.price
            pos.opened_at = fill.timestamp
        self.portfolio.equity -= fill.fee

    def _maybe_charge_funding(self, bar: Bar) -> None:
        # TODO: charge funding at 00/08/16 UTC based on funding-rate history.
        return

    def _mark_to_market(self, bar: Bar) -> None:
        pos = self.portfolio.positions.get(bar.symbol)
        if pos is None or pos.is_flat:
            return
        # TODO: update unrealized PnL into self.portfolio.equity correctly.
        self._equity_curve.append((bar.open_time, self.portfolio.equity))

    def _log_trade(self, fill: Fill, bar: Bar) -> None:
        record = {
            "ts": fill.timestamp,
            "symbol": fill.order.symbol,
            "side": fill.order.side,
            "qty": fill.order.qty,
            "price": fill.price,
            "fee": fill.fee,
            "reason": fill.order.reason,
            "bar_close": bar.close,
        }
        self._trade_log.write(json.dumps(record) + "\n")

    def _compute_metrics(self) -> dict:
        # TODO: real Sharpe, Sortino, max DD, win rate, etc.
        if not self._equity_curve:
            return {"run_id": self.run_id, "warning": "no bars processed"}
        start = self._equity_curve[0][1]
        end = self._equity_curve[-1][1]
        return {
            "run_id": self.run_id,
            "strategy": self.strategy.name,
            "starting_equity": start,
            "ending_equity": end,
            "return_pct": (end - start) / start * 100.0,
            "n_bars": len(self._equity_curve),
            "TODO": "implement Sharpe/DD/turnover/etc.",
        }


def main() -> None:
    raise SystemExit(
        "backtesting.engine is a library; invoke a strategy runner that passes a "
        "Strategy and a bar iterator into BacktestEngine.run()."
    )


if __name__ == "__main__":
    main()
