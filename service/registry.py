"""
service/registry.py — the dynamic pair registry (Stage 8.1).

detectors/results/pairs.json is the single source of truth for which pairs
the service tracks and their lifecycle:

    [{"pair": "SOL_USDT", "status": "bootstrapping|ready|error",
      "error_reason": "...",            # only for status=error
      "added_ts": "...", "tfs_ready": ["1w", ...]}]

service/pairs.py PAIRS is now only the SEED: if pairs.json is missing on
first start, it is created from PAIRS with status=ready (those pairs'
artifacts already exist). All writes are atomic (ioutil.atomic_write_json).

Two writers exist (API: add/remove; worker: status/tfs_ready updates), and
the API itself serves mutations from a threadpool — so every read-modify-
write is serialized by BOTH a module-level threading.Lock (in-process) and
a lockfile on the shared volume (cross-process, O_CREAT|O_EXCL with a
stale-lock timeout). Without the lock a DELETE racing a worker write could
permanently resurrect the removed pair. Read-only helpers stay lock-free
(atomic_write_json guarantees they never see a torn file).
"""
from __future__ import annotations
import os
import re
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pandas as pd

from detectors import paths
from service.ioutil import atomic_write_json, read_json
from service.pairs import PAIRS as SEED_PAIRS

REGISTRY_PATH = paths.ROOT / "detectors" / "results" / "pairs.json"
LOCK_PATH = REGISTRY_PATH.with_suffix(".lock")
LOCK_STALE_S = 30              # a lockfile older than this = crashed holder
LOCK_TIMEOUT_S = 10

BOOTSTRAPPING = "bootstrapping"
READY = "ready"
ERROR = "error"

_THREAD_LOCK = threading.Lock()


@contextmanager
def _locked():
    """Serialize registry read-modify-writes across threads AND processes
    (API container vs worker container share the results volume)."""
    with _THREAD_LOCK:
        LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
        t0 = time.monotonic()
        fd = None
        while fd is None:
            try:
                fd = os.open(LOCK_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                try:
                    if time.time() - LOCK_PATH.stat().st_mtime > LOCK_STALE_S:
                        LOCK_PATH.unlink(missing_ok=True)
                        continue
                except OSError:
                    pass
                if time.monotonic() - t0 > LOCK_TIMEOUT_S:
                    raise TimeoutError("registry lock timeout")
                time.sleep(0.05)
        try:
            yield
        finally:
            os.close(fd)
            try:
                LOCK_PATH.unlink()
            except OSError:
                pass


# ------------------------------------------------------------------ symbol
def normalize_symbol(raw: str) -> str:
    """'LINK/USDT', 'LINKUSDT', 'link usdt', 'link' -> 'LINK_USDT'.
    Raises ValueError on garbage."""
    s = re.sub(r"[\s/\-_]+", "", (raw or "").strip().upper())
    if s.endswith("USDT"):
        s = s[:-4]
    if not s or not s.isalnum():
        raise ValueError(f"cannot parse symbol from {raw!r}")
    return f"{s}_USDT"


def validate_symbol_okx(pair: str) -> tuple[bool, str]:
    """Check the pair's linear perpetual exists on OKX (the project's only
    data source). Returns (ok, human_readable_reason)."""
    from data.freshness_monitor import _okx
    inst = paths.okx_inst_id(pair)
    try:
        d = _okx._http_get(f"{_okx.OKX_BASE}/api/v5/public/instruments",
                           {"instType": "SWAP", "instId": inst})
    except Exception as e:
        return False, f"OKX instruments query failed: {type(e).__name__}"
    if d.get("code") != "0":
        return False, f"OKX error: {d.get('msg') or d.get('code')}"
    if not d.get("data"):
        return False, f"no {inst} linear perpetual on OKX"
    state = d["data"][0].get("state")
    if state != "live":
        return False, f"{inst} exists on OKX but is not live (state={state})"
    return True, "ok"


# ---------------------------------------------------------------- registry
def _now_iso() -> str:
    return pd.Timestamp.now(tz="UTC").isoformat(timespec="seconds")


def seed_if_missing() -> None:
    """First-start seed from service/pairs.py. A seed pair whose raw CSVs
    are absent (fresh git clone on a new machine — data/raw is gitignored)
    is seeded as BOOTSTRAPPING so the worker fetches its history itself:
    clone -> docker compose up -d is all a new box needs."""
    if REGISTRY_PATH.exists():
        return
    with _locked():
        if REGISTRY_PATH.exists():             # another process seeded first
            return
        entries = []
        for p in SEED_PAIRS:
            has_data = paths.raw_csv("1h", p).exists()
            entries.append(
                {"pair": p, "status": READY, "added_ts": _now_iso(),
                 "tfs_ready": ["1w", "1d", "4h", "2h", "1h", "5m"]}
                if has_data else
                {"pair": p, "status": BOOTSTRAPPING, "added_ts": _now_iso(),
                 "tfs_ready": []})
        atomic_write_json(REGISTRY_PATH, entries)


def load() -> list[dict]:
    seed_if_missing()
    data = read_json(REGISTRY_PATH, default=None)
    return data if isinstance(data, list) else []


def save(entries: list[dict]) -> None:
    atomic_write_json(REGISTRY_PATH, entries)


def mtime_ns() -> int:
    try:
        return REGISTRY_PATH.stat().st_mtime_ns
    except OSError:
        return 0


def get(pair: str) -> dict | None:
    for e in load():
        if e["pair"] == pair:
            return e
    return None


def known_pairs() -> list[str]:
    return [e["pair"] for e in load()]


def ready_pairs() -> list[str]:
    return [e["pair"] for e in load() if e.get("status") == READY]


def add_pair(pair: str) -> dict:
    """Add (or retry an errored) pair as bootstrapping. Raises KeyError with
    a message when the pair is already bootstrapping/ready (HTTP 409)."""
    with _locked():
        entries = load()
        for e in entries:
            if e["pair"] == pair:
                if e.get("status") == ERROR:
                    e["status"] = BOOTSTRAPPING       # retry
                    e.pop("error_reason", None)
                    e["tfs_ready"] = []
                    e["added_ts"] = _now_iso()
                    save(entries)
                    return e
                raise KeyError(f"{pair} already in registry (status={e.get('status')})")
        e = {"pair": pair, "status": BOOTSTRAPPING, "added_ts": _now_iso(),
             "tfs_ready": []}
        entries.append(e)
        save(entries)
        return e


def remove_pair(pair: str) -> bool:
    with _locked():
        entries = load()
        kept = [e for e in entries if e["pair"] != pair]
        if len(kept) == len(entries):
            return False
        save(kept)
        return True


def update_entry(pair: str, **fields) -> None:
    """Merge fields into the pair's entry (no-op if the pair was removed)."""
    with _locked():
        entries = load()
        for e in entries:
            if e["pair"] == pair:
                e.update(fields)
                save(entries)
                return


def mark_tf_ready(pair: str, tf: str) -> None:
    with _locked():
        entries = load()
        for e in entries:
            if e["pair"] == pair:
                if tf not in e.setdefault("tfs_ready", []):
                    e["tfs_ready"].append(tf)
                save(entries)
                return
