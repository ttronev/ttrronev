"""
service/api.py — read-only FastAPI over the worker's published JSON files.

    GET /api/pairs         -> {"pairs": [...]}
    GET /api/state/{pair}  -> state.json merged with live.json (404: unknown
                              pair; 503: pair known but state not built yet)
    GET /api/health        -> {"ok": bool, ...}; HTTP 503 when the freshest
                              5m heartbeat is older than STALE_AFTER_S (the
                              worker is dead or wedged)
    GET /                  -> the dashboard (service/static/index.html)

Reads only from disk (the worker is the single writer; writes are atomic).
Per-file cache keyed on mtime_ns — a poll every 15s costs a stat(), not a
parse. No auth here by design: exposure is Caddy's job (v1). CORS stays
closed (same-origin dashboard only).

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

from detectors import paths
from service.pairs import PAIRS

STALE_AFTER_S = 15 * 60          # health: worker considered dead past this
STATIC_DIR = Path(__file__).resolve().parent / "static"

app = FastAPI(title="ttrronev-service", docs_url=None, redoc_url=None)

_cache: dict[str, tuple[int, dict | None]] = {}


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


@app.get("/api/pairs")
def api_pairs():
    return {"pairs": PAIRS}


@app.get("/api/state/{pair}")
def api_state(pair: str):
    if pair not in PAIRS:
        raise HTTPException(status_code=404, detail=f"unknown pair: {pair}")
    state = _read_cached(paths.state_json(pair))
    if state is None:
        raise HTTPException(status_code=503,
                            detail=f"state for {pair} not built yet")
    live = _read_cached(paths.live_json(pair)) or {"price": None, "ts": None, "ok": False}
    return {**state, "live": live}


@app.get("/api/health")
def api_health():
    now = time.time()
    pairs_out = {}
    ok = True
    for pair in PAIRS:
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
    body = {"ok": ok, "stale_after_s": STALE_AFTER_S, "pairs": pairs_out}
    return JSONResponse(status_code=200 if ok else 503, content=body)


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
