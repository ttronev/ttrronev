"""
service/worker.py — the 24/7 analysis worker (python -m service.worker).

One asyncio process, two regen loops per pair plus a live-price poller:

  5m loop      — every 5m close (+buffer): fetch, regen the windowed 5m
                 layer-1, rebuild state.json, scan alerts, stamp heartbeat.
  structural   — every 1H boundary (+buffer): fetch every TF whose bar just
  loop           closed (1h/2h/4h/1d; 1w is checked at the daily boundary —
                 the fetch decides whether a weekly bar landed), regen their
                 detectors slowest-first, then run the derived chain ONCE
                 (cleanness → nesting → known_at → L5 → L5.1 — the
                 load-bearing order from MEMORY_HYGIENE.md), then rebuild
                 state + alerts. Batching guarantees the chain never runs
                 between two detector regens of the same boundary, and makes
                 "range memory recomputes at most hourly" true by
                 construction — no interval arithmetic.
  live loop    — every LIVE_PRICE_INTERVAL_S: OKX ticker → live.json
                 (display + proximity only; never detection input).

All regen work runs on a single-thread executor: strictly serialized (the
chain order is load-bearing; the box is RAM-constrained). Every published
JSON is written atomically (service/ioutil.py).

Fail loud, stay alive: an exception in one cycle is logged AND sent to
Telegram ("worker error"), but the loop continues; the same error key
re-alerts at most once per ERROR_ALERT_COOLDOWN_S.

A cycle that overruns its interval self-heals: detection is window-batch
(each regen re-reads the whole window), so a late tick republishes the same
truth the missed tick would have.
"""
from __future__ import annotations
import asyncio
import os
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pandas as pd

from detectors import paths
from detectors.alerts import _load_dotenv, _telegram_send
from service import regen, scheduler
from service.ioutil import atomic_write_json, read_json
from service.pairs import (
    PAIRS, WORKER_TFS, MEMORY_TFS, TF_MS,
    ERROR_ALERT_COOLDOWN_S, FETCH_RETRY_DELAY_S, LIVE_PRICE_INTERVAL_S,
)

REGEN_5M_BUDGET_S = 60          # spec: full 5m cycle must fit in this

# Structural TFs, slowest first (the detector regen order).
STRUCTURAL_TFS = [tf for tf in WORKER_TFS if tf != "5m"]

# TTRRONEV_ALERTS_DRY_RUN=1 -> alerts are printed, not sent (dev compose).
ALERTS_DRY_RUN = os.environ.get("TTRRONEV_ALERTS_DRY_RUN", "") in ("1", "true", "yes")


def log(msg: str) -> None:
    ts = pd.Timestamp.now(tz="UTC").strftime("%Y-%m-%d %H:%M:%S")
    print(f"{ts} {msg}", flush=True)


def _due_structural_tfs(boundary_ms: int) -> list[str]:
    """TFs whose bar closed exactly at this epoch-aligned 1H boundary.
    1w rides the daily boundary: OKX weekly bars don't align to epoch weeks,
    so we check daily and let the fetch decide (0 new rows on non-close days)."""
    due = [tf for tf in ("1d", "4h", "2h", "1h") if boundary_ms % TF_MS[tf] == 0]
    if boundary_ms % TF_MS["1d"] == 0:
        due.insert(0, "1w")
    return [tf for tf in STRUCTURAL_TFS if tf in due]


class Worker:
    def __init__(self):
        self.regen_pool = ThreadPoolExecutor(max_workers=1,
                                             thread_name_prefix="regen")
        self.live_pool = ThreadPoolExecutor(max_workers=1,
                                            thread_name_prefix="live")
        self.last_error_at: dict[str, float] = {}      # error key -> monotonic ts
        self.alert_fired: dict[str, dict] = {}          # pair -> dedup map

    # ------------------------------------------------------------- errors
    def report_error(self, key: str, exc: BaseException) -> None:
        log(f"[worker] ERROR {key}: {exc}\n{traceback.format_exc()}")
        now = time.monotonic()
        last = self.last_error_at.get(key)
        if last is not None and now - last < ERROR_ALERT_COOLDOWN_S:
            return
        self.last_error_at[key] = now
        # Send off-thread so an exception handler on the event loop never
        # blocks scheduling for the ~10s HTTP timeout.
        try:
            self.live_pool.submit(
                _telegram_send, f"⚠️ ttrronev-service worker error\n{key}: {exc}",
                False, lambda m: None)
        except Exception:
            pass                                        # alerting must never kill the loop

    # -------------------------------------------------------- blocking work
    def _heartbeat(self, pair: str, tfs) -> None:
        hb = read_json(paths.heartbeat_json(pair), default={}) or {}
        stamped = hb.get("tfs", {})
        now_iso = pd.Timestamp.now(tz="UTC").isoformat(timespec="seconds")
        for tf in tfs:
            stamped[tf] = now_iso
        hb.update({"pair": pair, "tfs": stamped, "updated_at": now_iso})
        atomic_write_json(paths.heartbeat_json(pair), hb)

    def _rebuild_state_and_alerts(self, pair: str) -> None:
        from detectors.query_state import State
        from detectors import alerts
        from service.state_builder import write_state
        live = read_json(paths.live_json(pair), default={}) or {}
        live_px = live.get("price") if live.get("ok") else None
        state = State(pair, live=False)
        if live_px is not None:
            state.live_price = float(live_px)
            state.live_ok = True
        write_state(pair, state=state, live_price=live_px)
        fired = self.alert_fired.get(pair)
        _, fired = alerts.run_once(dry_run=ALERTS_DRY_RUN, fired=fired,
                                   log=lambda m: log(f"[alerts] {m}"),
                                   pair=pair, state=state)
        self.alert_fired[pair] = fired

    def _cycle_5m_sync(self, pair: str) -> None:
        """One 5m cycle. Runs on the regen executor."""
        t0 = time.monotonic()
        fetched = regen.fetch_tf(pair, "5m", log=log)
        if not fetched:
            # Candle may not be published yet — one bounded retry.
            time.sleep(FETCH_RETRY_DELAY_S)
            fetched = regen.fetch_tf(pair, "5m", log=log)
            if not fetched and paths.l1_json("5m", pair).exists():
                log(f"[worker] {pair} 5m: no new bar; skipping regen")
                self._heartbeat(pair, ["5m"])
                return
        regen.regen_detector(pair, "5m", log=log)
        self._rebuild_state_and_alerts(pair)
        self._heartbeat(pair, ["5m"])
        dt = time.monotonic() - t0
        log(f"[worker] {pair} 5m: cycle complete in {dt:.1f}s")
        if dt > REGEN_5M_BUDGET_S:
            self.report_error(f"{pair}:5m:budget",
                              RuntimeError(f"5m cycle took {dt:.1f}s (> {REGEN_5M_BUDGET_S}s budget)"))

    def _structural_cycle_sync(self, pair: str, boundary_ms: int) -> None:
        """One structural-boundary cycle. Runs on the regen executor."""
        t0 = time.monotonic()
        due = _due_structural_tfs(boundary_ms)
        fetched: dict[str, int] = {}
        for tf in due:
            try:
                fetched[tf] = regen.fetch_tf(pair, tf, log=log)
            except Exception as e:
                self.report_error(f"{pair}:{tf}:fetch", e)
        # One batch retry for TFs whose closed bar isn't published yet
        # (1w is exempt: 0 new rows on a non-week-close day is normal).
        missing = [tf for tf in due if tf != "1w" and fetched.get(tf) == 0]
        if missing:
            time.sleep(FETCH_RETRY_DELAY_S)
            for tf in missing:
                try:
                    fetched[tf] = regen.fetch_tf(pair, tf, log=log)
                except Exception as e:
                    self.report_error(f"{pair}:{tf}:fetch", e)

        regen_tfs = []
        for tf in due:
            if fetched.get(tf, 0) > 0 or not paths.l1_json(tf, pair).exists():
                try:
                    regen.regen_detector(pair, tf, log=log)
                    regen_tfs.append(tf)
                except Exception as e:
                    self.report_error(f"{pair}:{tf}:regen", e)

        if any(tf in MEMORY_TFS for tf in regen_tfs):
            regen.regen_chain(pair, log=log)
        if regen_tfs:
            self._rebuild_state_and_alerts(pair)
        self._heartbeat(pair, due)
        log(f"[worker] {pair} structural: due={due} regenerated={regen_tfs} "
            f"in {time.monotonic() - t0:.1f}s")

    def _startup_sync(self, pair: str) -> None:
        """Cold start: fetch + regen every TF once so state.json exists."""
        log(f"[worker] {pair}: startup regen of {WORKER_TFS}")
        for tf in STRUCTURAL_TFS + ["5m"]:
            try:
                regen.fetch_tf(pair, tf, log=log)
            except Exception as e:
                self.report_error(f"{pair}:{tf}:startup-fetch", e)
            regen.regen_detector(pair, tf, log=log)
        regen.regen_chain(pair, log=log)
        self._rebuild_state_and_alerts(pair)
        self._heartbeat(pair, WORKER_TFS)
        log(f"[worker] {pair}: startup complete")

    def _live_sync(self, pair: str) -> None:
        from data.freshness_monitor import get_live_price
        px = get_live_price(inst_id=paths.okx_inst_id(pair))
        atomic_write_json(paths.live_json(pair), {
            "price": float(px) if px else None,
            "ts": pd.Timestamp.now(tz="UTC").isoformat(timespec="seconds"),
            "ok": px is not None,
        })

    # ------------------------------------------------------------ async loops
    async def five_min_loop(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            now_ms = int(time.time() * 1000)
            run_at = scheduler.next_run_at_ms("5m", now_ms)
            await asyncio.sleep(max(0.0, (run_at - now_ms) / 1000))
            for pair in PAIRS:
                try:
                    await loop.run_in_executor(
                        self.regen_pool, self._cycle_5m_sync, pair)
                except Exception as e:
                    self.report_error(f"{pair}:5m:cycle", e)

    async def structural_loop(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            now_ms = int(time.time() * 1000)
            run_at = scheduler.next_run_at_ms("1h", now_ms)
            boundary_ms = run_at - scheduler.CLOSE_BUFFER_MS
            await asyncio.sleep(max(0.0, (run_at - now_ms) / 1000))
            for pair in PAIRS:
                try:
                    await loop.run_in_executor(
                        self.regen_pool, self._structural_cycle_sync, pair, boundary_ms)
                except Exception as e:
                    self.report_error(f"{pair}:structural:cycle", e)

    async def live_loop(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            for pair in PAIRS:
                try:
                    await loop.run_in_executor(self.live_pool, self._live_sync, pair)
                except Exception as e:
                    self.report_error(f"{pair}:live", e)
            await asyncio.sleep(LIVE_PRICE_INTERVAL_S)

    async def main(self) -> None:
        _load_dotenv()
        log(f"[worker] starting: pairs={PAIRS} tfs={WORKER_TFS}"
            + (" [alerts DRY-RUN]" if ALERTS_DRY_RUN else ""))
        loop = asyncio.get_running_loop()
        live_task = asyncio.create_task(self.live_loop())
        for pair in PAIRS:
            try:
                await loop.run_in_executor(self.regen_pool, self._startup_sync, pair)
            except Exception as e:
                self.report_error(f"{pair}:startup", e)
        tasks = [asyncio.create_task(self.five_min_loop()),
                 asyncio.create_task(self.structural_loop())]
        try:
            await asyncio.gather(live_task, *tasks)
        finally:
            for t in (live_task, *tasks):
                t.cancel()
            self.regen_pool.shutdown(wait=False, cancel_futures=True)
            self.live_pool.shutdown(wait=False, cancel_futures=True)


def main() -> None:
    try:
        asyncio.run(Worker().main())
    except KeyboardInterrupt:
        log("[worker] stopped (Ctrl+C). clean exit.")


if __name__ == "__main__":
    main()
