"""Simulated portfolio risk tracker for Phase 1a.

Per-pair fixed $1 risk. Aggregate cap = MAX_CONCURRENT (3) open
positions. Equity tracked starting at ACCOUNT_START ($100) — purely
simulated in 1a; in 1b this maps to real Bybit equity.

The risk manager is consulted at the FILLED event:
  - check `can_open()` — does opening a new position violate the cap?
  - if yes, the setup proceeds (engine doesn't have a "block" hook), but
    the risk manager records `signal_blocked_by_cap` in the trade row
    rather than treating it as filled. Phase 1b will actually skip the
    order.

Equity changes are recorded through the `equity` SQLite table for full
auditability.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional

from paper_trade.config import (
    ACCOUNT_START, AGGREGATE_RISK_CAP, MAX_CONCURRENT, MAX_LEVERAGE,
    RISK_DOLLARS, TAKER_FEE,
)
from paper_trade.sqlite_store import execute, query


@dataclass
class OpenPosition:
    pair: str
    setup_id: int
    direction: str           # long / short
    entry_price: float
    sl_price: float
    tp_price: float
    size_coins: float
    risk_dollars: float       # the $ at risk on this position (intended; realized may differ in 1b)
    leverage: float
    open_ts_ms: int


@dataclass
class RiskManager:
    equity: float = ACCOUNT_START
    positions: dict[tuple[str, int], OpenPosition] = field(default_factory=dict)

    # --- Read state ---------------------------------------------------

    def total_deployed_risk(self) -> float:
        return sum(p.risk_dollars for p in self.positions.values())

    def can_open(self) -> bool:
        if len(self.positions) >= MAX_CONCURRENT:
            return False
        if self.total_deployed_risk() + RISK_DOLLARS > AGGREGATE_RISK_CAP + 1e-6:
            return False
        return True

    # --- Sizing -------------------------------------------------------

    def size_position(self, entry: float, sl: float,
                      risk_dollars: float = RISK_DOLLARS,
                      max_leverage: float = MAX_LEVERAGE,
                      ) -> tuple[float, float, float]:
        """Per the live spec: never skip a setup due to sizing constraints.
        Returns (coin_size, leverage, realized_risk_dollars)."""
        sl_dist = abs(entry - sl)
        if sl_dist <= 0 or entry <= 0:
            return 0.0, 1.0, 0.0
        sl_dist_pct = sl_dist / entry
        notional = risk_dollars / sl_dist_pct
        required_lev = max(1.0, notional / self.equity) if self.equity > 0 else max_leverage
        if required_lev > max_leverage:
            leverage = max_leverage
            notional = self.equity * max_leverage
        else:
            leverage = required_lev
        coin_size = notional / entry
        realized_risk = coin_size * entry * sl_dist_pct
        return coin_size, float(leverage), float(realized_risk)

    # --- Mutations ---------------------------------------------------

    def open_position(self, pair: str, setup_id: int, direction: str,
                      entry: float, sl: float, tp: float,
                      size: float, leverage: float, realized_risk: float,
                      ts_ms: Optional[int] = None) -> OpenPosition:
        ts_ms = ts_ms if ts_ms is not None else int(time.time() * 1000)
        # Entry fee = size * entry * fee_rate (charged immediately).
        fee = size * entry * TAKER_FEE
        self.equity -= fee
        execute(
            "INSERT INTO equity(timestamp_ms, pair, setup_id, delta_quote, new_equity, reason) "
            "VALUES(?, ?, ?, ?, ?, ?)",
            (ts_ms, pair, setup_id, -fee, self.equity, "fill_fee"),
        )
        pos = OpenPosition(
            pair=pair, setup_id=setup_id, direction=direction,
            entry_price=entry, sl_price=sl, tp_price=tp,
            size_coins=size, risk_dollars=realized_risk,
            leverage=leverage, open_ts_ms=ts_ms,
        )
        self.positions[(pair, setup_id)] = pos
        return pos

    def close_position(self, pair: str, setup_id: int, exit_price: float,
                       outcome: str, ts_ms: Optional[int] = None,
                       ) -> tuple[float, float, float]:
        """Returns (gross_pnl, fees, net_pnl)."""
        ts_ms = ts_ms if ts_ms is not None else int(time.time() * 1000)
        key = (pair, setup_id)
        pos = self.positions.get(key)
        if pos is None:
            return 0.0, 0.0, 0.0
        sign = 1.0 if pos.direction == "long" else -1.0
        gross = sign * pos.size_coins * (exit_price - pos.entry_price)
        fee_close = pos.size_coins * exit_price * TAKER_FEE
        net = gross - fee_close
        self.equity += net
        execute(
            "INSERT INTO equity(timestamp_ms, pair, setup_id, delta_quote, new_equity, reason) "
            "VALUES(?, ?, ?, ?, ?, ?)",
            (ts_ms, pair, setup_id, net, self.equity, f"exit_pnl:{outcome}"),
        )
        del self.positions[key]
        return float(gross), float(fee_close), float(net)

    # --- Hydration on restart ----------------------------------------

    def restore_from_db(self) -> None:
        """On restart, equity is recomputed by summing the equity audit
        table; open positions are recovered from the most recent FILLED
        events that don't yet have a matching RESOLVED. (Phase 1a:
        nothing actually blocks restart — the engine will replay history
        and re-emit events; we simply trust SQLite's running balance.)"""
        rows = query("SELECT new_equity FROM equity ORDER BY id DESC LIMIT 1")
        if rows:
            self.equity = float(rows[0]["new_equity"])
        # Open positions: in Phase 1a we don't need to truly track in-flight
        # positions across restarts because the streaming engine will
        # re-emit events from the buffer and rebuild them. Phase 1b will
        # need a more careful reconciliation against Bybit's open positions.

__all__ = ["RiskManager", "OpenPosition"]
