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
  registry     — every ~10s re-reads detectors/results/pairs.json (Stage
  loop           8.1); a pair with status=bootstrapping is bootstrapped
                 strictly one at a time (1h CSV first for bias, then
                 1w→1d→4h→2h→1h→5m, chain + state after the memory TFs),
                 then joins the bar-close cycles as ready.

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
import random
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
from service import regen, registry, scheduler
from service.ioutil import atomic_write_json, read_json
from service.pairs import (
    WORKER_TFS, MEMORY_TFS, TF_MS,
    ERROR_ALERT_COOLDOWN_S, FETCH_RETRY_DELAY_S, LIVE_PRICE_INTERVAL_S,
    CYCLE_WARN_S_DEFAULT, PAIR_JITTER_S, BACKFILL_ORDER,
)
from service.sharding import shard_pairs
from shared.memhygiene import peak_rss_mb

REGISTRY_POLL_S = 10            # how often the worker re-reads pairs.json
BOOTSTRAP_TF_PAUSE_S = 1.0      # politeness pause between bootstrap TF steps

# A pair whose 5m cycle fails this many passes in a row WHILE other pairs
# succeed is moved to status=error (see Worker._update_quarantine). 12 passes
# = about an hour.
QUARANTINE_AFTER = 12

# 5m pass budget (log + Telegram warning past this). Env-overridable so the
# acceptance test can trip it deliberately.
CYCLE_WARN_S = float(os.environ.get("TTRRONEV_CYCLE_WARN_S", CYCLE_WARN_S_DEFAULT))

BACKFILL_STATE_PATH = None      # resolved lazily via paths (see _backfill_state)

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
        self.fail_streak: dict[str, int] = {}           # pair -> consecutive failed 5m passes
        self.phase = "startup"                          # -> "running" once startup regen is done
        self.started_at = pd.Timestamp.now(tz="UTC").isoformat(timespec="seconds")

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

    def _rebuild_state_and_alerts(self, pair: str, seed_alerts: bool = False) -> None:
        """seed_alerts=True (bootstrap): prime the dedup state SILENTLY —
        a brand-new pair has no alerts_state.json, so a normal scan would
        burst out every recent historical event as a fresh Telegram DM."""
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
        if seed_alerts:
            _, fired = alerts.run_once(dry_run=True, fired=fired,
                                       log=lambda m: None, pair=pair, state=state)
            alerts._save_state(fired, pair)     # persist the primed dedup map
        else:
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
        clock_due = _due_structural_tfs(boundary_ms)
        # Catch-up by data, not only by clock: a boundary missed while the
        # process was stalled/down, or a fetch that failed, used to leave that
        # timeframe stale until its OWN next close (up to a day for 1d).
        behind = [tf for tf in regen.behind_tfs(pair, boundary_ms) if tf not in clock_due]
        if behind:
            log(f"[worker] {pair} structural: catching up {behind} "
                f"(CSV is behind the last closed bar)")
        due = [tf for tf in STRUCTURAL_TFS if tf in clock_due or tf in behind]
        fetched: dict[str, int] = {}
        for tf in due:
            try:
                fetched[tf] = regen.fetch_tf(pair, tf, log=log)
            except Exception as e:
                self.report_error(f"{pair}:{tf}:fetch", e)
        # One batch retry for TFs whose closed bar isn't published yet
        # (1w is exempt: 0 new rows on a non-week-close day is normal; a
        # catch-up TF returning nothing means the exchange has nothing newer).
        missing = [tf for tf in clock_due if tf != "1w" and fetched.get(tf) == 0]
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

    # ---------------------------------------------------------- bootstrap
    def _bootstrap_step_sync(self, pair: str, tf: str) -> None:
        """One TF step of a new pair's bootstrap (runs on the regen executor).
        Fetches the TF's history window and regenerates its detector."""
        regen.bootstrap_tf(pair, tf, log=log)
        regen.regen_detector(pair, tf, log=log)

    async def bootstrap_pair(self, pair: str) -> None:
        """Bootstrap one new pair: 1h CSV first (it is the bias input for
        every older-TF detector), then the spec order 1w→1d→4h→2h→1h→5m,
        chain + state after the memory TFs, ready at the end. Each TF step
        is a separate executor job so ready pairs' bar-close cycles
        interleave with a long bootstrap."""
        loop = asyncio.get_running_loop()
        log(f"[bootstrap] {pair}: starting")

        def _seed_state():
            self._rebuild_state_and_alerts(pair, seed_alerts=True)

        try:
            if registry.get(pair) is None:      # deleted while queued
                log(f"[bootstrap] {pair}: removed from registry; skipping")
                return
            await loop.run_in_executor(self.regen_pool, regen.bootstrap_tf, pair, "1h", log)
            for tf in ["1w", "1d", "4h", "2h", "1h"]:
                if registry.get(pair) is None:
                    log(f"[bootstrap] {pair}: removed from registry; aborting")
                    return
                await loop.run_in_executor(self.regen_pool, self._bootstrap_step_sync, pair, tf)
                registry.mark_tf_ready(pair, tf)
                await asyncio.sleep(BOOTSTRAP_TF_PAUSE_S)
            if registry.get(pair) is None:
                log(f"[bootstrap] {pair}: removed from registry; aborting")
                return
            await loop.run_in_executor(self.regen_pool, regen.regen_chain, pair, log)
            await loop.run_in_executor(self.regen_pool, _seed_state)
            if registry.get(pair) is None:
                return
            await loop.run_in_executor(self.regen_pool, self._bootstrap_step_sync, pair, "5m")
            registry.mark_tf_ready(pair, "5m")
            await loop.run_in_executor(self.regen_pool, _seed_state)
            await loop.run_in_executor(self.regen_pool, self._heartbeat, pair, WORKER_TFS)
            registry.update_entry(pair, status=registry.READY)
            log(f"[bootstrap] {pair}: READY")
        except Exception as e:
            # Report FIRST — the registry write below can itself fail (full
            # volume etc.) and must never take down the loop unalerted.
            self.report_error(f"{pair}:bootstrap", e)
            try:
                registry.update_entry(pair, status=registry.ERROR,
                                      error_reason=str(e)[:200])
            except Exception as e2:
                self.report_error(f"{pair}:bootstrap:registry-write", e2)

    # ---------------------------------------------------------- backfill
    def _backfill_state_file(self):
        return paths.backfill_state_json()

    def _next_backfill(self):
        """Next (pair, tf) whose deep history isn't complete, cheap TFs
        first (Stage 8b: raw-CSV-only depth for chart viewing)."""
        state = read_json(self._backfill_state_file(), default={}) or {}
        for pair in self._my_pairs():
            for tf in BACKFILL_ORDER:
                if state.get(pair, {}).get(tf) != "complete":
                    return pair, tf
        return None

    def _backfill_slice_sync(self, pair: str, tf: str) -> None:
        res = regen.backfill_tf(pair, tf, log=log)
        if res == "complete":
            state = read_json(self._backfill_state_file(), default={}) or {}
            state.setdefault(pair, {})[tf] = "complete"
            atomic_write_json(self._backfill_state_file(), state)

    async def heartbeat_loop(self) -> None:
        """Worker-level liveness, stamped every REGISTRY_POLL_S so /api/health
        can tell a dead worker from one that is catching up. Runs as its own
        task, started BEFORE the startup regen: the stamp used to live in the
        registry loop, which only starts after startup, so health reported the
        worker dead for the whole of every restart."""
        while True:
            try:
                atomic_write_json(paths.worker_heartbeat_json(), {
                    "updated_at": pd.Timestamp.now(tz="UTC").isoformat(timespec="seconds"),
                    "phase": self.phase,
                    "started_at": self.started_at,
                })
            except Exception as e:
                self.report_error("worker-heartbeat", e)
            await asyncio.sleep(REGISTRY_POLL_S)

    async def registry_loop(self) -> None:
        """Re-read pairs.json every REGISTRY_POLL_S (by mtime) and bootstrap
        new pairs strictly one at a time, oldest added_ts first; when NO
        bootstrap is pending, run one low-priority deep-backfill slice per
        pass. (Worker liveness is stamped by heartbeat_loop.)"""
        last_mtime = -1
        while True:
            try:
                m = registry.mtime_ns()
                if m != last_mtime:
                    last_mtime = m
                    entries = registry.load()
                    mine = set(shard_pairs([e["pair"] for e in entries]))
                    pending = sorted(
                        (e for e in entries
                         if e.get("status") == registry.BOOTSTRAPPING
                         and e["pair"] in mine),
                        key=lambda e: e.get("added_ts", ""))
                    for e in pending:
                        await self.bootstrap_pair(e["pair"])   # sequential = the queue
                        last_mtime = -1                        # re-read after each
                # Deep backfill only when the bootstrap queue is idle.
                still_pending = any(e.get("status") == registry.BOOTSTRAPPING
                                    for e in registry.load())
                if not still_pending:
                    nxt = self._next_backfill()
                    if nxt:
                        loop = asyncio.get_running_loop()
                        await loop.run_in_executor(
                            self.regen_pool, self._backfill_slice_sync, *nxt)
            except Exception as e:
                self.report_error("registry-loop", e)
            await asyncio.sleep(REGISTRY_POLL_S)

    def _startup_sync(self, pair: str) -> None:
        """Bring one pair up to date at process start. Every timeframe is
        fetched (cheap when nothing is new); a timeframe is REGENERATED only
        if new candles arrived, its artifact is missing/older than its CSV, or
        the detection code changed since the artifacts were written (see
        'restart logic' in service/regen.py). A restart with current data and
        unchanged code costs a few HTTP calls per pair, not a full regen."""
        force = regen.read_marker(pair).get("fingerprint") != regen.code_fingerprint()
        regen_tfs, verified = [], []
        for tf in STRUCTURAL_TFS + ["5m"]:
            fetched, fetch_ok = 0, True
            try:
                fetched = regen.fetch_tf(pair, tf, log=log)
            except Exception as e:
                fetch_ok = False
                self.report_error(f"{pair}:{tf}:startup-fetch", e)
            if force or fetched > 0 or regen.l1_is_stale(pair, tf):
                regen.regen_detector(pair, tf, log=log)
                regen_tfs.append(tf)
            if fetch_ok:
                verified.append(tf)          # confirmed current as of now
        if (force or any(tf in MEMORY_TFS for tf in regen_tfs)
                or regen.chain_outputs_missing(pair)):
            regen.regen_chain(pair, log=log)
        self._rebuild_state_and_alerts(pair)
        # Stamp only what was verified: a timeframe whose fetch failed keeps
        # its old stamp, so health goes stale instead of claiming freshness.
        self._heartbeat(pair, verified)
        regen.write_marker(pair)
        if regen_tfs:
            log(f"[worker] {pair}: startup complete — regenerated {regen_tfs}"
                + (" (detection code changed)" if force else ""))
        else:
            log(f"[worker] {pair}: startup complete — artifacts current, nothing regenerated")

    def _update_quarantine(self, ok_pairs: list, failed: list) -> list:
        """Handle PAIR-SPECIFIC failure. A pair whose 5m cycle keeps failing
        while other pairs succeed (delisted instrument, corrupt CSV) is moved
        to status=error after QUARANTINE_AFTER consecutive failed passes: it
        leaves the cycles and stops holding the service's health red forever.
        Re-adding it from the dashboard retries it.

        A pass in which NO pair succeeded is a systemic outage (exchange or
        network down) and never counts — otherwise an hour of OKX downtime
        would quarantine the whole universe. `failed` is [(pair, exc), ...].
        Returns the pairs quarantined by this call."""
        for p in ok_pairs:
            self.fail_streak.pop(p, None)
        if not ok_pairs:
            return []
        quarantined = []
        for p, exc in failed:
            n = self.fail_streak.get(p, 0) + 1
            self.fail_streak[p] = n
            if n < QUARANTINE_AFTER:
                continue
            reason = f"quarantined after {n} failed 5m cycles: {str(exc)[:140]}"
            try:
                registry.update_entry(p, status=registry.ERROR, error_reason=reason)
            except Exception as e:
                self.report_error(f"{p}:quarantine:registry-write", e)
                continue
            self.fail_streak.pop(p, None)
            quarantined.append(p)
            log(f"[worker] {p}: QUARANTINED — {reason}")
            try:
                self.live_pool.submit(
                    _telegram_send,
                    f"⚠️ ttrronev-service: {p} quarantined\n{reason}\n"
                    f"Other pairs are unaffected. Re-add it from the dashboard to retry.",
                    False, lambda m: None)
            except Exception:
                pass
        return quarantined

    def _live_sync(self, pair: str) -> None:
        from data.freshness_monitor import get_live_price
        px = get_live_price(inst_id=paths.okx_inst_id(pair))
        atomic_write_json(paths.live_json(pair), {
            "price": float(px) if px else None,
            "ts": pd.Timestamp.now(tz="UTC").isoformat(timespec="seconds"),
            "ok": px is not None,
        })

    # ------------------------------------------------------------ async loops
    def _my_pairs(self) -> list[str]:
        """Ready pairs this worker serves (PAIRS_SHARD filtered; default
        0/1 = everything — sharding is prep, not active scaling)."""
        return shard_pairs(registry.ready_pairs())

    async def five_min_loop(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            now_ms = int(time.time() * 1000)
            run_at = scheduler.next_run_at_ms("5m", now_ms)
            await asyncio.sleep(max(0.0, (run_at - now_ms) / 1000))
            t0 = time.monotonic()
            per_pair, ok_pairs, failed = [], [], []
            for i, pair in enumerate(self._my_pairs()):
                if i:                          # jitter: no aligned API bursts
                    await asyncio.sleep(PAIR_JITTER_S + random.random() * PAIR_JITTER_S)
                tp = time.monotonic()
                try:
                    await loop.run_in_executor(
                        self.regen_pool, self._cycle_5m_sync, pair)
                    ok_pairs.append(pair)
                except Exception as e:
                    self.report_error(f"{pair}:5m:cycle", e)
                    failed.append((pair, e))
                per_pair.append((pair, time.monotonic() - tp))
            self._update_quarantine(ok_pairs, failed)
            if per_pair:
                total = time.monotonic() - t0
                breakdown = ", ".join(f"{p}: {d:.1f}s" for p, d in per_pair)
                rss = peak_rss_mb()
                log(f"[worker] 5m pass: total {total:.1f}s over "
                    f"{len(per_pair)} pair(s)"
                    + (f", peak RSS {rss:.0f} MB" if rss is not None else "")
                    + f" ({breakdown})")
                if total > CYCLE_WARN_S:
                    self.report_error(
                        "5m-pass-budget",
                        RuntimeError(f"5m pass took {total:.1f}s over "
                                     f"{len(per_pair)} pairs (> {CYCLE_WARN_S:.0f}s)"))

    async def structural_loop(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            now_ms = int(time.time() * 1000)
            run_at = scheduler.next_run_at_ms("1h", now_ms)
            boundary_ms = run_at - scheduler.CLOSE_BUFFER_MS
            await asyncio.sleep(max(0.0, (run_at - now_ms) / 1000))
            for i, pair in enumerate(self._my_pairs()):
                if i:
                    await asyncio.sleep(PAIR_JITTER_S + random.random() * PAIR_JITTER_S)
                try:
                    await loop.run_in_executor(
                        self.regen_pool, self._structural_cycle_sync, pair, boundary_ms)
                except Exception as e:
                    self.report_error(f"{pair}:structural:cycle", e)

    async def live_loop(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            for pair in self._my_pairs():
                try:
                    await loop.run_in_executor(self.live_pool, self._live_sync, pair)
                except Exception as e:
                    self.report_error(f"{pair}:live", e)
            await asyncio.sleep(LIVE_PRICE_INTERVAL_S)

    async def main(self) -> None:
        _load_dotenv()
        registry.seed_if_missing()
        ready = registry.ready_pairs()
        log(f"[worker] starting: ready={ready} tfs={WORKER_TFS}"
            + (" [alerts DRY-RUN]" if ALERTS_DRY_RUN else ""))
        loop = asyncio.get_running_loop()
        hb_task = asyncio.create_task(self.heartbeat_loop())   # FIRST: alive during startup
        live_task = asyncio.create_task(self.live_loop())
        t0 = time.monotonic()
        for pair in ready:
            try:
                await loop.run_in_executor(self.regen_pool, self._startup_sync, pair)
            except Exception as e:
                self.report_error(f"{pair}:startup", e)
        self.phase = "running"
        log(f"[worker] startup finished for {len(ready)} pair(s) in "
            f"{time.monotonic() - t0:.0f}s; entering bar-close loops")
        tasks = [asyncio.create_task(self.five_min_loop()),
                 asyncio.create_task(self.structural_loop()),
                 asyncio.create_task(self.registry_loop())]
        try:
            await asyncio.gather(hb_task, live_task, *tasks)
        finally:
            for t in (hb_task, live_task, *tasks):
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
