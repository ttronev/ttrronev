"""Glue layer: StreamingEngine.EngineEvent -> SQLite + Telegram + RiskManager.

Routing per Phase 1a Telegram redesign:

  Persisted to SQLite (always, regardless of Telegram routing):
    BOS_GATED_OUT, BOS_QUEUED, SETUP_OPENED, FINALIZED, ARMED,
    PRIMARY_TRIGGERED, FILLED, RESOLVED_TP, RESOLVED_SL,
    CANCELLED_PRE_FILL, CANCELLED_BY_INVALIDATION,
    CANCELLED_BELOW_MIN_SWING.

  Sent to Telegram:
    ARMED               -> setup_armed
    PRIMARY_TRIGGERED   -> primary_triggered
    FILLED              -> filled
    RESOLVED_TP         -> closed_tp
    RESOLVED_SL         -> closed_sl
    CANCELLED_PRE_FILL  -> cancelled (reason=primary_sl_pre_secondary_fill)
    CANCELLED_BY_INVALIDATION -> cancelled
        - if snap.phase == AWAITING_ARM       -> reason=invalidation_pre_arm
        - if snap.phase == AWAITING_PRIMARY_TRIG -> reason=invalidation_pre_fill

  Suppressed from Telegram (still in SQLite for analysis):
    SETUP_OPENED, FINALIZED, BOS_QUEUED, BOS_GATED_OUT,
    CANCELLED_BELOW_MIN_SWING.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

import pandas as pd

from paper_trade.bar_buffer import PairBuffers
from paper_trade.config import RISK_DOLLARS
from paper_trade.health_server import HealthState
from paper_trade.level_proximity import compute_proximity
from paper_trade.risk_manager import RiskManager
from paper_trade.setup_numbering import SetupNumberAllocator
from paper_trade.sqlite_store import execute, log_runtime, query, tx
from paper_trade.streaming_engine import EngineEvent
from paper_trade.telegram_notifier import TelegramNotifier


_LOG = logging.getLogger("paper_trade.event_sink")


# Engine-event kinds we send to Telegram. All others are SQLite-only.
TELEGRAM_KINDS = (
    "ARMED",
    "PRIMARY_TRIGGERED",
    "FILLED",
    "RESOLVED_TP",
    "RESOLVED_SL",
    "CANCELLED_PRE_FILL",
    "CANCELLED_BY_INVALIDATION",
)


class EventSink:
    def __init__(self,
                 risk: RiskManager,
                 health: HealthState,
                 notifier: TelegramNotifier,
                 buffers_by_pair: dict[str, PairBuffers],
                 setup_num_alloc: SetupNumberAllocator,
                 loop: Optional[asyncio.AbstractEventLoop] = None) -> None:
        self.risk = risk
        self.health = health
        self.notifier = notifier
        self.buffers_by_pair = buffers_by_pair
        self.setup_num_alloc = setup_num_alloc
        self.loop = loop  # set by run.py once event loop is available

    # ----------------------------------------------------------------
    # Sync entry point — called from streaming engine (which runs in
    # an executor thread). We dispatch to async helpers via the loop.
    # ----------------------------------------------------------------

    def __call__(self, ev: EngineEvent) -> None:
        try:
            self._persist_event(ev)
        except Exception:
            _LOG.exception("persist failed for %s", ev.kind)
        # Fire async tasks for Telegram. Suppress events that are
        # SQLite-only per the Phase 1a redesign.
        if self.loop is None:
            return
        if ev.kind in TELEGRAM_KINDS:
            asyncio.run_coroutine_threadsafe(self._async_handle(ev), self.loop)

    # ----------------------------------------------------------------
    # Helpers
    # ----------------------------------------------------------------

    def _setup_num(self, pair: str, engine_setup_id: int) -> int:
        return self.setup_num_alloc.get_or_assign(pair, engine_setup_id)

    @staticmethod
    def _bos_ms(snap: dict) -> Optional[int]:
        bts = snap.get("bos_timestamp") if snap else None
        if bts is None:
            return None
        return int(pd.Timestamp(bts).value // 10**6)

    def _bar_close_at(self, pair: str, ts_ms: int) -> Optional[float]:
        rows = query(
            "SELECT close FROM bars WHERE pair = ? AND tf = '5m' AND timestamp_ms = ?",
            (pair, ts_ms),
        )
        if rows:
            return float(rows[0]["close"])
        # Fallback: scan in-memory buffer (covers test paths where SQLite
        # persist is bypassed, e.g. dry_run_replay).
        bufs = self.buffers_by_pair.get(pair)
        if bufs is None:
            return None
        for bar in reversed(bufs.b5m._bars):
            if bar.timestamp_ms == ts_ms:
                return float(bar.close)
        return None

    def _insert_setup_row(self, pair: str, setup_id: int, setup_num: int,
                          event: str, ts_ms: int, bos_ms: Optional[int],
                          bos_dir: Optional[str], price: Optional[float],
                          detail: str, conn=None) -> None:
        sql = (
            "INSERT INTO setups("
            "pair, setup_id, setup_num, event, timestamp_ms, "
            "bos_timestamp_ms, bos_direction, price, detail"
            ") VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)"
        )
        params = (pair, setup_id, setup_num, event, ts_ms,
                  bos_ms, bos_dir, price, detail)
        if conn is not None:
            conn.execute(sql, params)
        else:
            execute(sql, params)

    # ----------------------------------------------------------------
    # Sync persistence (runs in executor thread; SQLite is RLock-safe)
    # ----------------------------------------------------------------

    def _persist_event(self, ev: EngineEvent) -> None:
        snap = ev.candidate_snapshot
        ts_ms = (int(pd.Timestamp(ev.bar_ts).value // 10**6)
                 if ev.bar_ts is not None else int(time.time() * 1000))

        if ev.kind == "BOS_GATED_OUT":
            log_runtime("bos_gated_out", {"pair": ev.pair, "bar_ts": str(ev.bar_ts)})
            return
        if ev.kind == "BOS_QUEUED":
            log_runtime("bos_queued", {"pair": ev.pair, "bar_ts": str(ev.bar_ts)})
            return
        if snap is None:
            return

        sid = int(snap["setup_id"])
        setup_num = self._setup_num(ev.pair, sid)
        bos_ms = self._bos_ms(snap)

        if ev.kind == "SETUP_OPENED":
            # Don't insert into `signals` yet — fibs aren't computed
            # until FINALIZED. Just log to setups for traceability.
            self._insert_setup_row(
                ev.pair, sid, setup_num, ev.kind, ts_ms,
                bos_ms, ev.bos_direction or "", None, "",
            )
            return

        if ev.kind == "FINALIZED":
            with tx() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO signals("
                    "setup_id, pair, bos_timestamp_ms, bos_direction, "
                    "swing_high, swing_low, swing_size_pct, "
                    "fib_0, fib_0_3, fib_0_5, fib_0_75, fib_1_0, fib_1_2, "
                    "regime_gate_value_pct, regime_gate_passed, "
                    "bias_1h, btc_bias_1h"
                    ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        sid, ev.pair, bos_ms, snap["bos_direction"],
                        float(snap["swing_high"]), float(snap["swing_low"]),
                        float(snap["swing_size_pct"]),
                        float(snap["fib_0"]), float(snap["fib_0_3"]),
                        float(snap["fib_0_5"]), float(snap["fib_0_75"]),
                        float(snap["fib_1_0"]), float(snap["fib_1_2"]),
                        None, 1,
                        snap.get("bias_1h"), snap.get("btc_bias_1h"),
                    ),
                )
                self._insert_setup_row(
                    ev.pair, sid, setup_num, ev.kind, ts_ms,
                    bos_ms, snap.get("bos_direction"), None, "", conn=conn,
                )
            self._log_level_proximity(ev)
            return

        # All other kinds: append to setups lifecycle table.
        price = (snap.get("entry_price") if ev.kind in ("FILLED", "PRIMARY_TRIGGERED")
                 else snap.get("exit_price"))
        self._insert_setup_row(
            ev.pair, sid, setup_num, ev.kind, ts_ms,
            bos_ms, snap.get("bos_direction"), price,
            snap.get("secondary_outcome") or "",
        )

        # Risk manager: open on FILLED, close on RESOLVED_*.
        if ev.kind == "FILLED":
            self._handle_fill(ev, snap, ts_ms)
        elif ev.kind in ("RESOLVED_TP", "RESOLVED_SL"):
            self._handle_close(ev, snap, ts_ms)

    # ----------------------------------------------------------------
    # Risk + trades persistence
    # ----------------------------------------------------------------

    def _handle_fill(self, ev: EngineEvent, snap: dict, ts_ms: int) -> None:
        if not self.risk.can_open():
            log_runtime("signal_blocked_by_cap", {"pair": ev.pair, "setup_id": snap["setup_id"]})
            return
        entry = float(snap["entry_price"])
        sl = float(snap["sl_price"])
        tp = float(snap["tp_price"])
        size, leverage, realized_risk = self.risk.size_position(entry, sl)
        if size <= 0:
            log_runtime("signal_zero_size", {"pair": ev.pair, "setup_id": snap["setup_id"]})
            return
        self.risk.open_position(
            pair=ev.pair, setup_id=int(snap["setup_id"]),
            direction=str(snap["secondary_direction"]),
            entry=entry, sl=sl, tp=tp,
            size=size, leverage=leverage, realized_risk=realized_risk,
            ts_ms=ts_ms,
        )
        # Stash on the candidate snapshot for the matching Telegram dispatch.
        snap["_intended_risk"] = RISK_DOLLARS
        snap["_realized_risk"] = realized_risk
        snap["_size"] = size
        snap["_leverage"] = leverage
        # Update health.
        self.health.n_open_positions = len(self.risk.positions)
        self.health.equity = self.risk.equity

    def _handle_close(self, ev: EngineEvent, snap: dict, ts_ms: int) -> None:
        outcome = "tp_hit" if ev.kind == "RESOLVED_TP" else "sl_hit"
        exit_price = (float(snap["exit_price"]) if snap.get("exit_price") is not None
                      else float(snap["tp_price" if outcome == "tp_hit" else "sl_price"]))
        # Capture position metadata BEFORE close_position() removes it.
        pos = self.risk.positions.get((ev.pair, int(snap["setup_id"])))
        size_at_fill = pos.size_coins if pos else 0.0
        leverage_at_fill = pos.leverage if pos else 1.0
        realized_risk_at_fill = pos.risk_dollars if pos else RISK_DOLLARS
        gross, fees, net = self.risk.close_position(
            pair=ev.pair, setup_id=int(snap["setup_id"]),
            exit_price=exit_price, outcome=outcome, ts_ms=ts_ms,
        )
        r = net / RISK_DOLLARS if RISK_DOLLARS > 0 else 0.0
        with tx() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO trades("
                "pair, setup_id, direction, bos_timestamp_ms, "
                "entry_timestamp_ms, exit_timestamp_ms, "
                "entry_price, sl_price, tp_price, exit_price, outcome, "
                "r_realized, position_size, gross_pnl_quote, fees_quote, net_pnl_quote, "
                "leverage, intended_risk_dollars, realized_risk_dollars, "
                "slippage_entry, slippage_exit, funding_paid, "
                "backtest_expectation_json"
                ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    ev.pair, int(snap["setup_id"]),
                    str(snap["secondary_direction"]),
                    self._bos_ms(snap),
                    int(pd.Timestamp(snap["secondary_filled_timestamp"]).value // 10**6)
                    if snap.get("secondary_filled_timestamp") is not None else None,
                    ts_ms,
                    float(snap["entry_price"]), float(snap["sl_price"]),
                    float(snap["tp_price"]), exit_price, outcome,
                    r, float(size_at_fill),
                    gross, fees, net,
                    float(leverage_at_fill),
                    RISK_DOLLARS, float(realized_risk_at_fill),
                    None, None, None, None,
                ),
            )
        self.health.n_open_positions = len(self.risk.positions)
        self.health.equity = self.risk.equity

    # ----------------------------------------------------------------
    # Level proximity (for FINALIZED only)
    # ----------------------------------------------------------------

    def _log_level_proximity(self, ev: EngineEvent) -> None:
        snap = ev.candidate_snapshot
        if snap is None:
            return
        bufs = self.buffers_by_pair.get(ev.pair)
        if bufs is None:
            return
        df_5m = bufs.b5m.to_dataframe()
        df_1h = bufs.b1h.to_dataframe()
        if df_5m.empty or df_1h.empty:
            return
        try:
            prox = compute_proximity(
                pair=ev.pair,
                df_5m=df_5m, df_1h=df_1h,
                bos_timestamp=pd.Timestamp(snap["bos_timestamp"]),
                entry_price=float(snap["entry_price"]),
                sl_price=float(snap["sl_price"]),
            )
        except Exception:
            _LOG.exception("level proximity compute failed")
            return
        rows = []
        for name, rec in prox.items():
            rows.append((
                ev.pair, int(snap["setup_id"]), name,
                rec.get("price"), rec.get("dist_pct"),
                rec.get("dist_R"), rec.get("abs_R"),
            ))
        with tx() as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO level_proximity"
                "(pair, setup_id, level_name, level_price, dist_pct, dist_R, abs_R) "
                "VALUES(?,?,?,?,?,?,?)",
                rows,
            )

    # ----------------------------------------------------------------
    # Telegram dispatchers (called via run_coroutine_threadsafe)
    # ----------------------------------------------------------------

    async def _async_handle(self, ev: EngineEvent) -> None:
        snap = ev.candidate_snapshot
        if snap is None:
            return
        try:
            sid = int(snap["setup_id"])
            setup_num = self._setup_num(ev.pair, sid)

            if ev.kind == "ARMED":
                fibs = {
                    "1.2": float(snap["fib_1_2"]),
                    "1.0": float(snap["fib_1_0"]),
                    "0.75": float(snap["fib_0_75"]),
                    "0.5": float(snap["fib_0_5"]),
                    "0.3": float(snap["fib_0_3"]),
                    "0": float(snap["fib_0"]),
                }
                primary_limit = float(snap["fib_1_0"])  # primary trigger = fib_1.0 wick
                # Direction shown to user: trade direction (long/short).
                direction = str(snap.get("secondary_direction") or "")
                await self.notifier.setup_armed(
                    pair=ev.pair, setup_num=setup_num, direction=direction,
                    swing_high=float(snap["swing_high"]),
                    swing_low=float(snap["swing_low"]),
                    swing_high_ts=snap.get("swing_high_timestamp"),
                    swing_low_ts=snap.get("swing_low_timestamp"),
                    swing_size_pct=float(snap["swing_size_pct"]),
                    fibs=fibs,
                    primary_limit=primary_limit,
                )

            elif ev.kind == "PRIMARY_TRIGGERED":
                await self.notifier.primary_triggered(
                    pair=ev.pair, setup_num=setup_num,
                    fib_1_0=float(snap["fib_1_0"]),
                    entry=float(snap["entry_price"]),
                    sl=float(snap["sl_price"]),
                    tp=float(snap["tp_price"]),
                )

            elif ev.kind == "FILLED":
                # Fields stashed by _handle_fill (same dict, same call frame).
                await self.notifier.filled(
                    pair=ev.pair, setup_num=setup_num,
                    entry=float(snap["entry_price"]),
                    sl=float(snap["sl_price"]),
                    tp=float(snap["tp_price"]),
                    size_coins=float(snap.get("_size") or 0.0),
                    leverage=float(snap.get("_leverage") or 1.0),
                    intended_risk=RISK_DOLLARS,
                    realized_risk=float(snap.get("_realized_risk") or RISK_DOLLARS),
                )

            elif ev.kind in ("RESOLVED_TP", "RESOLVED_SL"):
                # Read the trade row we just persisted.
                rows = query(
                    "SELECT net_pnl_quote, r_realized, exit_price FROM trades "
                    "WHERE pair = ? AND setup_id = ?",
                    (ev.pair, sid),
                )
                if rows:
                    pnl = float(rows[0]["net_pnl_quote"])
                    r = float(rows[0]["r_realized"])
                    exit_p = float(rows[0]["exit_price"])
                else:
                    pnl = 0.0; r = 0.0
                    exit_p = float(snap.get("exit_price") or snap.get("tp_price"))
                equity = self.risk.equity
                if ev.kind == "RESOLVED_TP":
                    r_planned = float(snap.get("r_planned") or 0.0)
                    await self.notifier.closed_tp(
                        pair=ev.pair, setup_num=setup_num,
                        exit_price=exit_p, r_planned=r_planned,
                        r_realized=r, pnl=pnl, equity=equity,
                    )
                else:
                    await self.notifier.closed_sl(
                        pair=ev.pair, setup_num=setup_num,
                        exit_price=exit_p, r_realized=r, pnl=pnl, equity=equity,
                    )

            elif ev.kind == "CANCELLED_PRE_FILL":
                # primary_sl_pre_secondary_fill: 5m close past fib_1.2.
                bar_ms = (int(pd.Timestamp(ev.bar_ts).value // 10**6)
                          if ev.bar_ts is not None else None)
                bar_close = self._bar_close_at(ev.pair, bar_ms) if bar_ms else None
                close_str = (f"{bar_close:.4f}" if bar_close is not None else "?")
                hm = (ev.bar_ts.strftime("%H:%M") if ev.bar_ts is not None else "??:??")
                detail = f"5m close past fib_1.2 at {close_str} @ {hm} UTC"
                await self.notifier.cancelled(
                    pair=ev.pair, setup_num=setup_num,
                    reason="primary_sl_pre_secondary_fill",
                    detail=detail,
                )

            elif ev.kind == "CANCELLED_BY_INVALIDATION":
                # phase distinguishes pre-arm vs post-arm at cancel time.
                phase = snap.get("phase") or ""
                if phase == "AWAITING_ARM":
                    reason = "invalidation_pre_arm"
                elif phase == "AWAITING_PRIMARY_TRIG":
                    reason = "invalidation_pre_fill"
                else:
                    reason = "invalidation"
                bar_ms = (int(pd.Timestamp(ev.bar_ts).value // 10**6)
                          if ev.bar_ts is not None else None)
                bar_close = self._bar_close_at(ev.pair, bar_ms) if bar_ms else None
                close_str = (f"{bar_close:.4f}" if bar_close is not None else "?")
                hm = (ev.bar_ts.strftime("%H:%M") if ev.bar_ts is not None else "??:??")
                detail = f"5m close past fib_0 at {close_str} @ {hm} UTC"
                await self.notifier.cancelled(
                    pair=ev.pair, setup_num=setup_num,
                    reason=reason, detail=detail,
                )

        except Exception:
            _LOG.exception("telegram dispatch failed for %s", ev.kind)


__all__ = ["EventSink", "TELEGRAM_KINDS"]
