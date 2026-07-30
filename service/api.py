"""
service/api.py — FastAPI over the worker's published JSON files + the pair
registry (Stage 8).

    GET    /api/pairs               -> registry entries (+ flat names)
    POST   /api/pairs {"symbol"}    -> normalize + validate on OKX, add as
                                       bootstrapping (409 dup, 422 invalid)
    DELETE /api/pairs/{pair}        -> remove from registry (disk untouched)
    GET    /api/state/{pair}        -> registry fields + state.json + live.json
                                       (404 unknown; bootstrapping pairs get a
                                       200 partial doc so the UI can show
                                       progress)
    GET    /api/candles/{pair}/{tf} -> last N bars from data/raw CSV
                                       (?limit=500, cap 1500; mtime cache)
    GET    /api/health              -> worker heartbeat; 503 when the freshest
                                       5m regen is older than STALE_AFTER_S
    GET    /                        -> the dashboard (service/static/)

The API is the registry's second writer (add/remove only; the worker owns
status/tfs_ready) — the compose api service therefore mounts
detectors/results read-write. Candle CSVs stay read-only.

Run:  uvicorn service.api:app --host 0.0.0.0 --port 8000
"""
from __future__ import annotations
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import json
import time

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from detectors import paths
from service import registry
from service.pairs import WORKER_TFS

STALE_AFTER_S = 15 * 60          # health: worker considered dead past this
CANDLES_DEFAULT = 500
CANDLES_CAP = 1500
STATIC_DIR = Path(__file__).resolve().parent / "static"

app = FastAPI(title="ttrronev-service", docs_url=None, redoc_url=None)

_cache: dict[str, tuple[int, dict | None]] = {}
_candle_cache: dict[str, tuple[int, list]] = {}


def _read_cached(path: Path) -> dict | None:
    """mtime-cached JSON read; None when missing/unreadable."""
    key = str(path)
    try:
        mtime = path.stat().st_mtime_ns
    except OSError:
        _cache.pop(key, None)
        return None
    hit = _cache.get(key)
    if hit and hit[0] == mtime:
        return hit[1]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return hit[1] if hit else None      # mid-replace race: keep last good
    _cache[key] = (mtime, data)
    return data


# ------------------------------------------------------------------- pairs
class AddPairBody(BaseModel):
    symbol: str


@app.get("/api/pairs")
def api_pairs():
    entries = registry.load()
    return {"pairs": [e["pair"] for e in entries], "registry": entries}


@app.post("/api/pairs", status_code=201)
def api_add_pair(body: AddPairBody):
    try:
        pair = registry.normalize_symbol(body.symbol)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    ok, reason = registry.validate_symbol_okx(pair)
    if not ok:
        raise HTTPException(status_code=422, detail=reason)
    try:
        entry = registry.add_pair(pair)
    except KeyError as e:
        raise HTTPException(status_code=409, detail=str(e.args[0]))
    return entry


@app.delete("/api/pairs/{pair}")
def api_remove_pair(pair: str):
    if not registry.remove_pair(pair):
        raise HTTPException(status_code=404, detail=f"unknown pair: {pair}")
    # Evict the pair's cached docs so a later re-add starts clean and the
    # caches don't pin removed pairs' data forever.
    for key in [str(paths.state_json(pair)), str(paths.live_json(pair)),
                str(paths.heartbeat_json(pair))]:
        _cache.pop(key, None)
    for tf in WORKER_TFS:
        _candle_cache.pop(str(paths.raw_csv(tf, pair)), None)
    return {"removed": pair}       # CSVs and artifacts stay on disk by design


# ------------------------------------------------------------------- state
@app.get("/api/state/{pair}")
def api_state(pair: str):
    entry = registry.get(pair)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"unknown pair: {pair}")
    state = _read_cached(paths.state_json(pair)) or {}
    live = _read_cached(paths.live_json(pair)) or {"price": None, "ok": False}
    # Delete-then-readd: while NOT ready, on-disk docs older than this
    # incarnation belong to the previous one — hide them so the "partial
    # bootstrapping doc" is really partial (no stale price flagged live).
    if entry.get("status") != registry.READY:
        from datetime import datetime
        try:
            added = datetime.fromisoformat(entry.get("added_ts"))
            gen = state.get("generated_at")
            if gen and datetime.fromisoformat(gen) < added:
                state = {}
            lts = live.get("ts")
            if lts and datetime.fromisoformat(lts) < added:
                live = {"price": None, "ts": None, "ok": False}
        except (TypeError, ValueError):
            state, live = {}, {"price": None, "ts": None, "ok": False}
    live.setdefault("ts", None)
    return {
        "pair": pair,
        **state,
        "live": live,
        "status": entry.get("status"),
        "tfs_ready": entry.get("tfs_ready", []),
        "error_reason": entry.get("error_reason"),
    }


# ----------------------------------------------------------------- candles
def _read_candle_tail(csv_path: Path, limit: int) -> list:
    """Last `limit` [ts,o,h,l,c,v] rows. Reads only the file's tail bytes
    (a 5m CSV is tens of MB; parsing it whole per request would be silly),
    cached per mtime for up to CANDLES_CAP rows."""
    key = str(csv_path)
    mtime = csv_path.stat().st_mtime_ns
    hit = _candle_cache.get(key)
    if hit and hit[0] == mtime:
        return hit[1][-limit:]
    # ~64 bytes/row typical; 3x margin, min 64KB.
    want = max(CANDLES_CAP * 200, 65536)
    size = csv_path.stat().st_size
    with csv_path.open("rb") as f:
        if size > want:
            f.seek(size - want)
            f.readline()                       # drop the partial first line
        raw = f.read().decode("utf-8", errors="replace")
    lines = raw.splitlines()
    # A file not ending in a newline is mid-append: the last line may be a
    # torn row that would parse as a wrong-but-valid candle. Drop it.
    if raw and not raw.endswith(("\n", "\r")):
        lines = lines[:-1]
    rows = []
    for line in lines:
        parts = line.split(",")
        if len(parts) < 6:
            continue
        try:
            rows.append([int(parts[0]), float(parts[1]), float(parts[2]),
                         float(parts[3]), float(parts[4]), float(parts[5])])
        except ValueError:
            continue                            # header / garbage line
    rows.sort(key=lambda r: r[0])
    # Dedup on ts (keep last occurrence) — cheap since already sorted.
    dedup = []
    for r in rows:
        if dedup and dedup[-1][0] == r[0]:
            dedup[-1] = r
        else:
            dedup.append(r)
    tail = dedup[-CANDLES_CAP:]
    _candle_cache[key] = (mtime, tail)
    return tail[-limit:]


@app.get("/api/candles/{pair}/{tf}")
def api_candles(pair: str, tf: str, limit: int = CANDLES_DEFAULT):
    if registry.get(pair) is None:
        raise HTTPException(status_code=404, detail=f"unknown pair: {pair}")
    if tf not in WORKER_TFS:
        raise HTTPException(status_code=404, detail=f"unknown tf: {tf}")
    csv_path = paths.raw_csv(tf, pair)
    limit = max(1, min(int(limit), CANDLES_CAP))
    try:
        candles = _read_candle_tail(csv_path, limit)
    except OSError:                 # missing / vanished mid-read -> 404, not 500
        raise HTTPException(status_code=404,
                            detail=f"no candle data for {pair} {tf} yet")
    return {"pair": pair, "tf": tf, "candles": candles}


# ------------------------------------------------------------------ health
@app.get("/api/health")
def api_health():
    now = time.time()
    pairs_out = {}
    # Worker-level liveness (stamped by the registry loop every ~10s):
    # detects a dead worker even when NO pair is ready yet (first
    # bootstrap, all-error registry) — a vacuous per-pair pass is not ok.
    worker_alive = False
    try:
        whb = _read_cached(paths.ROOT / "detectors" / "results" / "worker_heartbeat.json") or {}
        from datetime import datetime
        ts = whb.get("updated_at")
        if ts:
            worker_alive = (now - datetime.fromisoformat(ts).timestamp()) < STALE_AFTER_S
    except Exception:
        worker_alive = False
    ok = worker_alive
    for pair in registry.ready_pairs():
        # Any malformed heartbeat (wrong types, bad timestamp) counts as
        # stale — health must return its 200/503 JSON body, never a 500.
        age_s = None
        tfs = {}
        try:
            hb = _read_cached(paths.heartbeat_json(pair)) or {}
            tfs = hb.get("tfs", {}) if isinstance(hb, dict) else {}
            hb_5m = tfs.get("5m") if isinstance(tfs, dict) else None
            if hb_5m:
                from datetime import datetime
                age_s = now - datetime.fromisoformat(hb_5m).timestamp()
        except Exception:
            age_s = None
        stale = age_s is None or age_s > STALE_AFTER_S
        ok = ok and not stale
        pairs_out[pair] = {
            "worker_last_regen_ts": tfs,
            "regen_5m_age_s": round(age_s) if age_s is not None else None,
            "stale": stale,
        }
    body = {"ok": ok, "worker_alive": worker_alive,
            "stale_after_s": STALE_AFTER_S, "pairs": pairs_out}
    return JSONResponse(status_code=200 if ok else 503, content=body)


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
