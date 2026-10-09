"""Phase 1a entry point — async orchestration.

Boot sequence:
  1. init_schema, log start.
  2. Hydrate per-pair PairBuffers from SQLite.
  3. REST gap-fill: any closed bars between SQLite's latest and now.
  4. Restore each pair's engine state from `engine_state` checkpoint
     (if present); else fresh engine. Rebuild StreamingEngine
     `_seen_anchors` from SQLite tables for cross-restart dedup.
  5. Start health server.
  6. Send "bot online" Telegram.
  7. Connect WS; on each closed bar: append → step engine → checkpoint
     (if events fired).
  8. Heartbeat scheduler (every HEARTBEAT_SECONDS) — also writes a
     periodic checkpoint regardless of events.
  9. Daily-validator scheduler (00:05 UTC).
 10. SIGTERM/SIGINT → graceful shutdown.

Run:
    python -m paper_trade.run
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import shutil
import signal
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import aiohttp
import pandas as pd

from paper_trade.bar_buffer import Bar, PairBuffers
from paper_trade.binance_rest import fetch_gap, fetch_recent
from paper_trade.binance_ws import BinanceKlineWS
from paper_trade.config import (
    BUFFER_1H, BUFFER_5M, EXEC_TF, HEARTBEAT_SECONDS, INTRABAR_TF, LOG_DIR,
    PAIRS, PHASE, TREND_TF,
)
from paper_trade.daily_validator import validate_day
from paper_trade.event_sink import EventSink
from paper_trade.health_server import HealthState, start_server
from paper_trade.risk_manager import RiskManager
from paper_trade.setup_numbering import SetupNumberAllocator
from paper_trade.sqlite_store import (
    init_schema, log_runtime, query, upsert_bar,
    save_engine_state, load_engine_state, rebuild_seen_anchors_from_db,
)
from paper_trade.streaming_engine import StreamingEngine
from paper_trade.telegram_notifier import TelegramNotifier

from paper_trade.config import ACCOUNT_START, DATA_DIR, SQLITE_PATH


_LOG = logging.getLogger("paper_trade.run")


# ----------------------------------------------------------------------
# Fresh-start protocol (--reset)
# ----------------------------------------------------------------------
#
# Required when an engine logic change ships that affects which setups
# fire or how they resolve, including (non-exhaustive):
#   * StructureAnalyzer (swing detection, BOS, ratchet)
#   * SecondaryOnlyEngine (step / intrabar_step / fill mechanics / SL /
#     TP / hard-stop / regime gate)
#   * StreamingEngine wrapper (event ordering, dedup keys, checkpoint
#     restore semantics)
#   * config: regime_gate_pct / regime_lookback_h / risk_pct / fees /
#     min_swing_pct (anything that changes which setups exist)
#
# NOT required for:
#   * Telegram message format changes (templates only)
#   * EventSink routing tweaks (which kinds go to Telegram vs SQLite)
#   * SetupNumberAllocator changes (numbering scheme only)
#   * Run.py orchestration tweaks that don't touch engine behavior
#   * Validator / heartbeat / health-server changes
#
# Implementation: --reset <tag> moves the existing
# `paper_trade/data/paper_trade.sqlite` to
# `paper_trade/data/archive/paper_trade_pre_<YYYYMMDD>_<tag>.sqlite`
# BEFORE init_schema() runs, so the next call creates a fresh empty
# DB. Setup numbering naturally restarts at SOL-1, AVAX-1, LINK-1
# (SetupNumberAllocator queries MAX(setup_num); empty table yields 0+1=1).


_RESET_TAG_RE = re.compile(r"^[a-z0-9_]{1,40}$")


def _archive_for_reset(tag: str) -> Optional[Path]:
    """Move the existing live SQLite to
    `<DATA_DIR>/archive/paper_trade_pre_<YYYYMMDD>_<tag>.sqlite`.

    Tag must be `[a-z0-9_]{1,40}` so the filename is portable and
    operator-readable. Date is computed in UTC so cross-tz operators
    see the same archive name.

    Returns the archive Path on success, or None if there was no
    existing DB to archive (treated as a clean fresh start).
    """
    if not _RESET_TAG_RE.match(tag):
        raise ValueError(
            f"--reset tag must match [a-z0-9_]{{1,40}}; got {tag!r}",
        )
    src = Path(SQLITE_PATH)
    if not src.exists():
        _LOG.info("--reset: no existing SQLite at %s; starting fresh", src)
        return None
    archive_dir = DATA_DIR / "archive"
    archive_dir.mkdir(parents=True, exist_ok=True)
    today = datetime.now(tz=timezone.utc).strftime("%Y%m%d")
    dst = archive_dir / f"paper_trade_pre_{today}_{tag}.sqlite"
    # Refuse to overwrite an existing archive — operators should pick a
    # fresh tag rather than silently clobber a prior reset.
    if dst.exists():
        raise FileExistsError(
            f"archive target already exists: {dst}. Pick a different "
            f"--reset tag or remove the existing archive.",
        )
    # Move main DB plus WAL/SHM sidecars (if present), so the archived
    # DB is fully self-contained and openable later for forensics.
    shutil.move(str(src), str(dst))
    for ext in ("-wal", "-shm"):
        sidecar_src = Path(str(src) + ext)
        if sidecar_src.exists():
            shutil.move(str(sidecar_src), str(Path(str(dst) + ext)))
    _LOG.info("--reset: archived %s -> %s", src, dst)
    return dst


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        handlers=[
            logging.FileHandler(LOG_DIR / "paper_trade.log", encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )


# ----------------------------------------------------------------------
# Bootstrap helpers
# ----------------------------------------------------------------------

async def _hydrate_and_gap_fill(
    pair_buffers: dict[str, PairBuffers],
    session: aiohttp.ClientSession,
) -> None:
    """For each pair: hydrate from SQLite, then REST-seed (if empty) or
    REST-gap-fill (if non-empty) to bring buffers current."""
    for pair, bufs in pair_buffers.items():
        n5_db, n1_db = bufs.hydrate()
        _LOG.info("hydrated %s from SQLite: 5m=%d 1h=%d", pair, n5_db, n1_db)

        # If SQLite is empty for either tf, do a fresh REST seed sized to
        # the in-memory buffer cap.
        if n5_db < 200:
            seed = await fetch_recent(session, pair, EXEC_TF, n_bars=BUFFER_5M)
            for bar in seed:
                bufs.b5m.append_closed(bar, persist=True)
            _LOG.info("seeded %s 5m from REST: +%d bars", pair, len(seed))
        if n1_db < 200:
            seed = await fetch_recent(session, pair, TREND_TF, n_bars=BUFFER_1H)
            for bar in seed:
                bufs.b1h.append_closed(bar, persist=True)
            _LOG.info("seeded %s 1h from REST: +%d bars", pair, len(seed))

    # In all other cases (and idempotently after fresh seeds): gap-fill
    # any remaining stale window. Same path used by the WS reconnect
    # handler — separating ensures both call sites use identical logic.
    await _gap_fill(pair_buffers, session, source="startup")


async def _gap_fill(
    pair_buffers: dict[str, PairBuffers],
    session: aiohttp.ClientSession,
    source: str = "reconnect",
) -> dict[str, dict[str, int]]:
    """Fetch and append any closed bars between each buffer's latest
    timestamp and now. Idempotent — if a buffer is already current,
    fetch_gap returns an empty list and the buffer is unchanged.

    Used by:
      * `_hydrate_and_gap_fill` at startup (after SQLite hydration)
      * `reconnect_handler` after every successful WS reconnect

    Returns {pair: {tf: n_bars_appended}} for runtime_events logging."""
    stats: dict[str, dict[str, int]] = {p: {} for p in pair_buffers}
    for pair, bufs in pair_buffers.items():
        for tf, buf in (("5m", bufs.b5m), ("1h", bufs.b1h)):
            last = buf.latest_ts_ms()
            if last is None:
                stats[pair][tf] = 0
                continue
            try:
                gap = await fetch_gap(session, pair, tf, last)
            except Exception as e:
                _LOG.warning("[%s] gap-fill failed for %s %s: %s",
                             source, pair, tf, type(e).__name__)
                stats[pair][tf] = -1
                continue
            n_app = 0
            for bar in gap:
                if buf.append_closed(bar, persist=True):
                    n_app += 1
            stats[pair][tf] = n_app
            if n_app > 0:
                latest_ts = buf.latest_ts_ms()
                _LOG.info("[%s] gap-filled %s %s: +%d bars (last %s)",
                          source, pair, tf, n_app,
                          datetime.fromtimestamp(latest_ts / 1000, tz=timezone.utc)
                          if latest_ts else "?")
    return stats


# ----------------------------------------------------------------------
# Schedulers
# ----------------------------------------------------------------------

async def _heartbeat_loop(
    notifier: TelegramNotifier,
    pair_buffers: dict[str, PairBuffers],
    streaming: dict[str, StreamingEngine],
    risk: RiskManager,
    started_ms: int,
    stop_event: asyncio.Event,
) -> None:
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=HEARTBEAT_SECONDS)
        except asyncio.TimeoutError:
            pass
        if stop_event.is_set():
            break
        uptime_min = int((time.time() * 1000 - started_ms) // 60000)
        status = {}
        for pair, bufs in pair_buffers.items():
            ts5 = bufs.b5m.latest_ts_ms()
            if ts5 is None:
                bar_line = "no bars yet"
            else:
                age_min = int((time.time() * 1000 - ts5) // 60000)
                bar_line = f"last 5m {age_min}m ago  (n5={len(bufs.b5m)})"
            # Pull the active-candidate one-liner from the streaming
            # engine so the operator sees "no active setup" or e.g.
            # "armed long  bos 04-25 16:35  entry@85.17 ..." in every
            # heartbeat. Without this the heartbeat only confirms WS
            # plumbing is alive; this confirms engine logic is alive
            # AND surfaces in-flight setups that haven't yet hit
            # ARMED/FILLED/etc. transitions.
            try:
                cand_line = streaming[pair].candidate_status()
            except Exception:
                cand_line = "?"
            status[pair] = f"{bar_line}\n      candidate: {cand_line}"
        # Periodic engine-state checkpoint. Idempotent (INSERT OR
        # REPLACE on (pair, kind)). Runs even during idle periods so
        # the checkpoint is never older than HEARTBEAT_SECONDS.
        for pair, stream in streaming.items():
            try:
                state_json = json.dumps(stream.serialize_engine_state(), default=str)
                last_ts = pair_buffers[pair].b5m.latest_ts_ms()
                save_engine_state(pair, state_json, last_5m_ts_ms=last_ts)
            except Exception:
                _LOG.exception("heartbeat checkpoint failed for %s", pair)
        try:
            await notifier.heartbeat(uptime_min, status, len(risk.positions))
        except Exception:
            _LOG.exception("heartbeat send failed")


async def _daily_summary_loop(
    notifier: TelegramNotifier,
    risk: RiskManager,
    pairs: tuple[str, ...],
    stop_event: asyncio.Event,
) -> None:
    """Send a daily summary at 00:00 UTC covering the prior UTC day.
    Independent of the daily validator (which runs at 00:05 UTC and
    targets a different concern: streaming-vs-batch parity)."""
    while not stop_event.is_set():
        now = datetime.now(tz=timezone.utc)
        target = now.replace(hour=0, minute=0, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        wait_s = max(60.0, (target - now).total_seconds())
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=wait_s)
        except asyncio.TimeoutError:
            pass
        if stop_event.is_set():
            break

        # Summary covers the day that just ended (00:00 UTC of yesterday
        # to 00:00 UTC of today). When this fires at e.g. 2026-05-11
        # 00:00 UTC, day_str = '2026-05-10'.
        now2 = datetime.now(tz=timezone.utc)
        day_dt = (now2 - timedelta(seconds=60)).replace(
            hour=0, minute=0, second=0, microsecond=0,
        )
        day_str = day_dt.strftime("%Y-%m-%d")
        day_start_ms = int(day_dt.timestamp() * 1000)
        day_end_ms = int((day_dt + timedelta(days=1)).timestamp() * 1000) - 1

        try:
            # Closed trades in window.
            rows = query(
                "SELECT pair, outcome, r_realized, net_pnl_quote FROM trades "
                "WHERE exit_timestamp_ms >= ? AND exit_timestamp_ms <= ? "
                "AND outcome IN ('tp_hit', 'sl_hit')",
                (day_start_ms, day_end_ms),
            )
            n_closed = len(rows)
            n_tp = sum(1 for r in rows if r["outcome"] == "tp_hit")
            n_sl = n_closed - n_tp
            r_sum = sum(float(r["r_realized"]) for r in rows)
            pnl = sum(float(r["net_pnl_quote"]) for r in rows)
            per_pair_data: dict[str, dict] = {p: {"pair": p, "w": 0, "l": 0, "r_sum": 0.0} for p in pairs}
            for r in rows:
                p = r["pair"]
                if p not in per_pair_data:
                    per_pair_data[p] = {"pair": p, "w": 0, "l": 0, "r_sum": 0.0}
                if r["outcome"] == "tp_hit":
                    per_pair_data[p]["w"] += 1
                else:
                    per_pair_data[p]["l"] += 1
                per_pair_data[p]["r_sum"] += float(r["r_realized"])
            per_pair = list(per_pair_data.values())

            # Setup-event counts in window.
            armed_rows = query(
                "SELECT COUNT(*) AS n FROM setups "
                "WHERE event = 'ARMED' AND timestamp_ms >= ? AND timestamp_ms <= ?",
                (day_start_ms, day_end_ms),
            )
            n_armed = int(armed_rows[0]["n"]) if armed_rows else 0
            filled_rows = query(
                "SELECT COUNT(*) AS n FROM setups "
                "WHERE event = 'FILLED' AND timestamp_ms >= ? AND timestamp_ms <= ?",
                (day_start_ms, day_end_ms),
            )
            n_filled = int(filled_rows[0]["n"]) if filled_rows else 0
            cancelled_rows = query(
                "SELECT COUNT(*) AS n FROM setups "
                "WHERE event LIKE 'CANCELLED_%' AND timestamp_ms >= ? AND timestamp_ms <= ?",
                (day_start_ms, day_end_ms),
            )
            n_cancelled = int(cancelled_rows[0]["n"]) if cancelled_rows else 0

            await notifier.daily_summary(
                day_str=day_str,
                n_closed=n_closed, n_tp=n_tp, n_sl=n_sl,
                r_sum=r_sum, pnl=pnl,
                equity_now=risk.equity, equity_start=ACCOUNT_START,
                per_pair=per_pair,
                n_armed=n_armed, n_filled=n_filled, n_cancelled=n_cancelled,
            )
            log_runtime("daily_summary_sent", {
                "day": day_str, "n_closed": n_closed,
                "r_sum": round(r_sum, 4), "pnl": round(pnl, 2),
            })
        except Exception:
            _LOG.exception("daily summary failed")


async def _daily_validator_loop(
    notifier: TelegramNotifier,
    pair_buffers: dict[str, PairBuffers],
    health: HealthState,
    stop_event: asyncio.Event,
) -> None:
    while not stop_event.is_set():
        # Sleep until next 00:05 UTC (5min grace after midnight so all
        # bars from the prior day have arrived).
        now = datetime.now(tz=timezone.utc)
        target = now.replace(hour=0, minute=5, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        wait_s = max(60.0, (target - now).total_seconds())
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=wait_s)
        except asyncio.TimeoutError:
            pass
        if stop_event.is_set():
            break
        # Validate yesterday.
        yesterday = (datetime.now(tz=timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
        all_ok = True
        for pair in pair_buffers.keys():
            try:
                result = validate_day(pair, yesterday)
            except Exception:
                _LOG.exception("validator failed for %s %s", pair, yesterday)
                continue
            if result["diverged"]:
                all_ok = False
                try:
                    await notifier.validation_alert(
                        yesterday, pair, result["streaming"], result["batch"],
                    )
                except Exception:
                    _LOG.exception("validation alert send failed")
        health.last_validation_ok = all_ok
        health.last_validation_day = yesterday
        log_runtime("daily_validator_done", {"day": yesterday, "all_ok": all_ok})


# ----------------------------------------------------------------------
# Closed-bar handler
# ----------------------------------------------------------------------

class _BarHandler:
    """Stateful callback wired to BinanceKlineWS. Routes closed bars to
    the right buffer, triggers the streaming engine on every new 5m,
    and checkpoints engine state to SQLite when events are emitted (so
    crash-after-event windows are bounded by a single bar)."""

    def __init__(self,
                 pair_buffers: dict[str, PairBuffers],
                 streaming: dict[str, StreamingEngine],
                 health: HealthState,
                 loop: asyncio.AbstractEventLoop) -> None:
        self.pair_buffers = pair_buffers
        self.streaming = streaming
        self.health = health
        self.loop = loop

    def _step_and_checkpoint(self, pair: str, bar: Bar) -> int:
        """Runs in executor: invoke streaming.on_new_bar_5m and
        checkpoint state on EVERY 5m bar (regardless of whether events
        fired). Returns count of new events (caller may use for
        diagnostics).

        Rationale: checkpointing only on event-emitting bars meant a
        quiet pair (no BOS / setup transition) could go an hour or more
        without its `engine_state` row updating, leaving the row
        misleadingly stale and forcing a long bootstrap-replay on
        restart. Saving every bar bounds the staleness to one bar
        (~5 min). The state JSON is small (~1MB per pair worst case)
        and SQLite WAL handles per-bar writes comfortably.
        """
        new_events = self.streaming[pair].on_new_bar_5m()
        try:
            state_json = json.dumps(self.streaming[pair].serialize_engine_state(), default=str)
            save_engine_state(pair, state_json, last_5m_ts_ms=bar.timestamp_ms)
        except Exception:
            _LOG.exception("checkpoint write failed for %s", pair)
        return len(new_events)

    def _intrabar_step(self, pair: str, bar: Bar) -> int:
        """Runs in executor: routes 1m bar through the engine's
        intrabar fill-detection path. Returns count of new (deduped)
        events emitted on this 1m frame.

        Critically does NOT save engine_state here — intrabar events
        advance the candidate's phase, but the next 5m
        `_step_and_checkpoint` call will persist that state. Saving
        every 1m would 5x our checkpoint write rate for marginal
        recovery benefit (a crash between 1m and 5m re-replays
        the missing 1m bars from REST gap-fill, which the engine
        treats as 1m intrabar checks if we wire that on startup; but
        the typical case — wick fired intrabar then crash 30s later —
        is recovered when the next 5m bar arrives, since the engine's
        phase will have been persisted as part of the *last* 5m
        checkpoint, plus a fresh re-evaluation from the buffer's
        latest 1m). Bottom line: 1m is best-effort latency; 5m is the
        durability boundary."""
        try:
            new_events = self.streaming[pair].on_new_bar_1m(
                ts_ms=bar.timestamp_ms,
                high=bar.high, low=bar.low,
            )
        except Exception:
            _LOG.exception("intrabar step failed for %s", pair)
            return 0
        return len(new_events)

    async def __call__(self, pair: str, tf: str, bar: Bar) -> None:
        bufs = self.pair_buffers.get(pair)
        if tf == EXEC_TF:
            if bufs is None:
                return
            bufs.b5m.append_closed(bar, persist=True)
            self.health.mark_bar(pair, bar.timestamp_ms)
            # Run engine + checkpoint in executor to avoid blocking the WS loop.
            await self.loop.run_in_executor(None, self._step_and_checkpoint, pair, bar)
        elif tf == TREND_TF:
            if bufs is None:
                return
            bufs.b1h.append_closed(bar, persist=True)
        elif tf == INTRABAR_TF:
            # 1m bar: sub-5m fill detection. NOT persisted to bar
            # buffers (those are 5m + 1h only); routed straight to
            # engine.intrabar_step via on_new_bar_1m. If no candidate
            # is active for this pair, the engine returns immediately.
            if pair not in self.streaming:
                return
            await self.loop.run_in_executor(
                None, self._intrabar_step, pair, bar,
            )


# ----------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------

async def amain(args: argparse.Namespace) -> None:
    _setup_logging()
    # --reset: archive the existing SQLite BEFORE init_schema() opens
    # it, so the next call creates a fresh empty DB.
    reset_archive_path: Optional[Path] = None
    if getattr(args, "reset", None):
        reset_archive_path = _archive_for_reset(args.reset)
    init_schema()
    started_ms = int(time.time() * 1000)
    started_iso = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    start_detail: dict = {"phase": PHASE, "pairs": list(PAIRS)}
    if reset_archive_path is not None:
        # Path stored relative to repo root for portability in the
        # runtime_events log.
        try:
            from paper_trade.config import REPO_ROOT
            start_detail["reset_archive"] = str(
                reset_archive_path.relative_to(REPO_ROOT)
            )
        except Exception:
            start_detail["reset_archive"] = str(reset_archive_path)
        start_detail["reset_tag"] = args.reset
    log_runtime("start", start_detail)

    pair_buffers: dict[str, PairBuffers] = {p: PairBuffers(p) for p in PAIRS}
    risk = RiskManager()
    risk.restore_from_db()

    health = HealthState(started_ms=started_ms, equity=risk.equity)

    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def _stop_handler(*_: object) -> None:
        if not stop_event.is_set():
            _LOG.info("shutdown signal received")
            stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _stop_handler)
        except NotImplementedError:
            # Windows: signal handling on the loop is limited. The
            # default behavior (KeyboardInterrupt -> CancelledError) is
            # acceptable for dev.
            pass

    async with TelegramNotifier() as notifier:
        runner, site = await start_server(health)
        # One long-lived ClientSession covers BOTH the startup hydrate
        # path AND the WS reconnect gap-fill handler. Closing it would
        # leave the reconnect handler with a dead session.
        async with aiohttp.ClientSession() as session:
            try:
                _LOG.info("hydrating buffers + REST gap-fill")
                await _hydrate_and_gap_fill(pair_buffers, session)

                # Surface the archive path on Telegram only when --reset
                # is explicitly used. Path is repo-relative for clarity.
                reset_archive_str: Optional[str] = None
                if reset_archive_path is not None:
                    try:
                        from paper_trade.config import REPO_ROOT
                        reset_archive_str = str(
                            reset_archive_path.relative_to(REPO_ROOT)
                        ).replace("\\", "/")
                    except Exception:
                        reset_archive_str = str(reset_archive_path)
                await notifier.bot_online(
                    PHASE, PAIRS,
                    started_iso=started_iso,
                    reset_archive=reset_archive_str,
                )

                # Per-pair sequential setup numbering (SOL-1, SOL-2,
                # AVAX-1, ...). Restored from SQLite max(setup_num).
                setup_num_alloc = SetupNumberAllocator(PAIRS)

                # Build streaming engines + event sink.
                sink = EventSink(risk=risk, health=health, notifier=notifier,
                                 buffers_by_pair=pair_buffers,
                                 setup_num_alloc=setup_num_alloc,
                                 loop=loop)
                streaming: dict[str, StreamingEngine] = {}
                # Restart-freshness summary, one row per pair, sent
                # to Telegram after the engine catch-up loop completes.
                startup_rows: list[dict] = []
                for pair, bufs in pair_buffers.items():
                    streaming[pair] = StreamingEngine(
                        pair=pair, buffers=bufs, on_event=sink,
                    )
                    # Restore engine state from checkpoint, if any.
                    saved = load_engine_state(pair)
                    seen = rebuild_seen_anchors_from_db(pair)
                    status = "cold_bootstrap"
                    cp_ts_str = "-"
                    if saved is not None:
                        state_json, last_5m_ts_ms = saved
                        try:
                            state_dict = json.loads(state_json)
                            streaming[pair].load_engine_state(state_dict, seen_anchors=seen)
                            status = "restored"
                            if last_5m_ts_ms:
                                cp_ts_str = datetime.fromtimestamp(
                                    last_5m_ts_ms / 1000, tz=timezone.utc,
                                ).strftime("%Y-%m-%d %H:%M UTC")
                            _LOG.info("restored engine state for %s "
                                      "(checkpoint last_5m_ts=%s, seen_anchors=%d)",
                                      pair, cp_ts_str, len(seen))
                            log_runtime("engine_state_restored",
                                        {"pair": pair, "last_5m_ts_ms": last_5m_ts_ms,
                                         "seen_anchors": len(seen)})
                        except Exception:
                            _LOG.exception("engine state restore failed for %s; starting fresh", pair)
                            status = "cold_bootstrap"
                    else:
                        # No prior checkpoint — fresh start. But if SQLite
                        # has prior signals/setups (from a manual replay
                        # or earlier session), still seed _seen_anchors
                        # so dedup works correctly.
                        if seen:
                            streaming[pair]._seen_anchors = seen
                            _LOG.info("seeded %s _seen_anchors from SQLite (%d entries)",
                                      pair, len(seen))

                    # ----- Restart-freshness catch-up -----
                    # Engine state may lag the buffer by N bars (engine
                    # was checkpointed at bar T, but the buffer has been
                    # gap-filled to bar T+N). Replay the missing bars
                    # through the persistent engine NOW, before WS
                    # starts, so the engine is current the instant the
                    # first live WS bar arrives. on_new_bar_5m() walks
                    # from `_last_5m_processed_ts_ms` to the buffer head
                    # — exactly what we need.
                    #
                    # CRITICAL: suppress the EventSink callback during
                    # bootstrap replay. Otherwise, on a fresh-DB launch
                    # (`--reset` or first ever launch), `_seen_anchors`
                    # starts empty and EVERY historical event in the
                    # buffer (potentially thousands of BOS / ARMED /
                    # FILLED / RESOLVED events spanning weeks) gets
                    # persisted into the (supposedly clean) DB and
                    # blasted onto Telegram. We swap `on_event` to a
                    # no-op for the duration of the catch-up call. The
                    # engine still updates its internal state
                    # (analyzer, 1h closes, regime cache,
                    # `_seen_anchors`) so future post-replay events
                    # dedup correctly against the historical anchors.
                    # If a candidate happens to be mid-flight at the
                    # end of replay, its next state transition (via a
                    # live WS bar) will fire normally with whatever
                    # setup_num the allocator assigns at that moment.
                    buf_ts_ms = pair_buffers[pair].b5m.latest_ts_ms()
                    last_proc_before = streaming[pair]._last_5m_processed_ts_ms
                    bars_replayed = 0
                    if buf_ts_ms is not None:
                        # Count expected replays: 5m bars in buffer with ts > last_proc_before.
                        if last_proc_before is None:
                            bars_replayed = sum(
                                1 for b in pair_buffers[pair].b5m._bars
                            )
                        else:
                            bars_replayed = sum(
                                1 for b in pair_buffers[pair].b5m._bars
                                if b.timestamp_ms > last_proc_before
                            )
                        _original_on_event = streaming[pair].on_event
                        streaming[pair].on_event = lambda ev: None  # silence replay
                        try:
                            await loop.run_in_executor(
                                None, streaming[pair].on_new_bar_5m,
                            )
                        except Exception:
                            _LOG.exception("startup catch-up failed for %s", pair)
                        finally:
                            streaming[pair].on_event = _original_on_event
                        # Persist the freshly caught-up state so a crash
                        # right after this point doesn't replay again.
                        try:
                            state_json2 = json.dumps(
                                streaming[pair].serialize_engine_state(),
                                default=str,
                            )
                            save_engine_state(pair, state_json2,
                                              last_5m_ts_ms=buf_ts_ms)
                        except Exception:
                            _LOG.exception("startup catch-up save failed for %s", pair)

                    after_ts_ms = streaming[pair]._last_5m_processed_ts_ms
                    after_ts_str = (
                        datetime.fromtimestamp(after_ts_ms / 1000, tz=timezone.utc)
                        .strftime("%Y-%m-%d %H:%M UTC")
                        if after_ts_ms else "-"
                    )
                    buf_ts_str = (
                        datetime.fromtimestamp(buf_ts_ms / 1000, tz=timezone.utc)
                        .strftime("%Y-%m-%d %H:%M UTC")
                        if buf_ts_ms else "-"
                    )
                    try:
                        candidate_summary = streaming[pair].candidate_status()
                    except Exception:
                        candidate_summary = "?"
                    startup_rows.append({
                        "pair": pair,
                        "status": status,
                        "checkpoint_ts": cp_ts_str,
                        "buffer_ts": buf_ts_str,
                        "bars_replayed": bars_replayed,
                        "last_5m_after": after_ts_str,
                        "candidate": candidate_summary,
                    })
                    _LOG.info("startup catch-up %s: status=%s checkpoint=%s "
                              "buffer=%s replayed=%d engine_now=%s",
                              pair, status, cp_ts_str, buf_ts_str,
                              bars_replayed, after_ts_str)
                    cp_ts_ms = (saved[1]
                                if (saved is not None and saved[1] is not None)
                                else None)
                    log_runtime("startup_catchup", {
                        "pair": pair, "status": status,
                        "checkpoint_ts_ms": cp_ts_ms,
                        "buffer_ts_ms": buf_ts_ms,
                        "bars_replayed": bars_replayed,
                        "engine_now_ts_ms": after_ts_ms,
                    })

                # Send the per-pair startup summary BEFORE WS starts so
                # the operator sees the catch-up confirmation before any
                # live event lands.
                try:
                    await notifier.startup_summary(startup_rows)
                except Exception:
                    _LOG.exception("startup_summary send failed")

                # Reconnect handler: invoked by BinanceKlineWS after
                # every successful (re)connect, BEFORE any WS frame is
                # processed. Performs REST gap-fill for both 5m and 1h
                # to backfill bars closed during the disconnect window,
                # then triggers a single engine pass per pair to emit
                # any signals that fire as a result of the gap-filled
                # bars (dedup ensures no duplicate event firings).
                #
                # If gap-fill fails (network blip during reconnect), we
                # raise — the WS outer loop catches it, logs ws_disconnect,
                # and retries with backoff. Engine never sees a partial
                # buffer.
                async def reconnect_handler() -> None:
                    t0 = time.time()
                    stats = await _gap_fill(pair_buffers, session, source="reconnect")
                    elapsed_ms = int((time.time() - t0) * 1000)
                    log_runtime("ws_reconnect_gap_fill", {
                        "elapsed_ms": elapsed_ms,
                        "stats": stats,
                    })
                    # Trigger engine for every pair so any signals
                    # latent in the just-filled bars fire promptly.
                    for pair, stream in streaming.items():
                        try:
                            await loop.run_in_executor(None, stream.on_new_bar_5m)
                        except Exception:
                            _LOG.exception("post-reconnect engine pass failed for %s", pair)

                # Wire WS with reconnect handler. We subscribe to three
                # timeframes per pair: 5m (engine timeline), 1h (regime
                # gate input), and 1m (sub-5m fill detection). The 1m
                # path advances the active candidate's wick-based
                # phases (ARMED, PRIMARY_TRIGGERED, FILLED, RESOLVED_TP,
                # RESOLVED_HARD_STOP) up to ~60s after the actual price
                # touch, vs. up to ~5min if we waited for the 5m close.
                # Backtest assumes wick fills, so this brings live
                # behavior to within ~1 minute of backtest parity.
                stream_pairs = (
                    [(p, EXEC_TF) for p in PAIRS]
                    + [(p, TREND_TF) for p in PAIRS]
                    + [(p, INTRABAR_TF) for p in PAIRS]
                )
                handler = _BarHandler(pair_buffers, streaming, health, loop)
                ws = BinanceKlineWS(stream_pairs, handler, on_reconnect=reconnect_handler)

                # Schedulers.
                tasks = [
                    asyncio.create_task(ws.run(), name="ws"),
                    asyncio.create_task(_heartbeat_loop(
                        notifier, pair_buffers, streaming, risk, started_ms, stop_event,
                    ), name="heartbeat"),
                    asyncio.create_task(_daily_validator_loop(
                        notifier, pair_buffers, health, stop_event,
                    ), name="validator"),
                    asyncio.create_task(_daily_summary_loop(
                        notifier, risk, PAIRS, stop_event,
                    ), name="daily_summary"),
                ]
                done, pending = await asyncio.wait(
                    [*tasks, asyncio.create_task(stop_event.wait(), name="stop")],
                    return_when=asyncio.FIRST_COMPLETED,
                )
                ws.stop()
                for t in pending:
                    t.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
            finally:
                try:
                    await notifier.bot_offline("shutdown")
                except Exception:
                    pass
                await site.stop()
                await runner.cleanup()
                log_runtime("stop", {})


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument(
        "--reset", metavar="TAG", default=None,
        help=(
            "FRESH-START protocol: archive the existing SQLite to "
            "paper_trade/data/archive/paper_trade_pre_<YYYYMMDD>_<TAG>.sqlite "
            "and start with a brand-new empty DB. Required when an "
            "engine logic change (analyzer / secondary_only_engine / "
            "streaming wrapper / regime gate / fill mechanics) is "
            "being deployed. NOT required for Telegram template / "
            "EventSink routing / numbering / orchestration changes. "
            "TAG must be [a-z0-9_]{1,40}, e.g. 'checkpoint_fix' or "
            "'1m_fill_detection'."
        ),
    )
    args = p.parse_args()
    try:
        asyncio.run(amain(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
