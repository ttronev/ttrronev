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
    symbol: str | None = None
    symbols: list[str] | None = None


@app.get("/api/pairs")
def api_pairs():
    entries = registry.load()
    return {"pairs": [e["pair"] for e in entries], "registry": entries}


@app.post("/api/pairs", status_code=201)
def api_add_pair(body: AddPairBody):
    """Single {"symbol"} or batch {"symbols": [...]} (Stage 8b). Invalid
    symbols never block the valid ones: response is {queued, rejected}.
    All-rejected -> 422 (single-symbol callers keep their readable error)."""
    raw_list = body.symbols if body.symbols is not None else (
        [body.symbol] if body.symbol else [])
    if not raw_list:
        raise HTTPException(status_code=422, detail="no symbol(s) given")
    queued, rejected = [], []
    seen = set()
    for raw in raw_list:
        try:
            pair = registry.normalize_symbol(raw)
        except ValueError as e:
            rejected.append({"symbol": raw, "reason": str(e)})
            continue
        if pair in seen:
            rejected.append({"symbol": raw, "reason": "duplicate in request"})
            continue
        seen.add(pair)
        ok, reason = registry.validate_symbol_okx(pair)
        if not ok:
            rejected.append({"symbol": raw, "reason": reason})
            continue
        try:
            registry.add_pair(pair)
            queued.append(pair)
        except KeyError as e:
            rejected.append({"symbol": raw, "reason": str(e.args[0])})
    if not queued:
        # single-symbol callers keep the 8.1 contract: 409 for an existing
        # pair, 422 with a plain readable string otherwise
        if len(rejected) == 1:
            reason = rejected[0]["reason"]
            code = 409 if "already in registry" in reason else 422
            raise HTTPException(status_code=code, detail=reason)
        raise HTTPException(status_code=422,
                            detail={"queued": [], "rejected": rejected})
    return {"queued": queued, "rejected": rejected}


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
_first_ts_cache: dict[str, tuple[int, int | None]] = {}


def _file_first_ts(csv_path: Path) -> int | None:
    """Timestamp of the first data row (mtime-cached)."""
    key = str(csv_path)
    mtime = csv_path.stat().st_mtime_ns
    hit = _first_ts_cache.get(key)
    if hit and hit[0] == mtime:
        return hit[1]
    ts = None
    with csv_path.open("rb") as f:
        for _ in range(3):                     # header + tolerance
            line = f.readline().decode("utf-8", errors="replace")
            try:
                ts = int(line.split(",", 1)[0])
                break
            except ValueError:
                continue
    _first_ts_cache[key] = (mtime, ts)
    return ts


def _parse_rows(raw: str, drop_torn_tail: bool) -> list:
    lines = raw.splitlines()
    if drop_torn_tail and raw and not raw.endswith(("\n", "\r")):
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
            continue
    rows.sort(key=lambda r: r[0])
    dedup = []
    for r in rows:
        if dedup and dedup[-1][0] == r[0]:
            dedup[-1] = r
        else:
            dedup.append(r)
    return dedup


def _read_candles_before(csv_path: Path, before_ms: int, limit: int) -> tuple[list, bool]:
    """Last `limit` rows strictly BEFORE before_ms, via a byte-offset binary
    search (never parses the whole file — a legacy 5m CSV is ~80MB).
    Returns (rows, has_more)."""
    size = csv_path.stat().st_size
    with csv_path.open("rb") as f:
        def ts_at(pos: int):
            """ts of the first complete line at/after byte pos."""
            f.seek(pos)
            if pos:
                f.readline()                    # skip partial line
            for _ in range(3):
                line = f.readline().decode("utf-8", errors="replace")
                if not line:
                    return None
                try:
                    return int(line.split(",", 1)[0])
                except ValueError:
                    continue                    # header line
            return None

        lo, hi = 0, size                        # find ~byte pos of before_ms
        while hi - lo > 4096:
            mid = (lo + hi) // 2
            t = ts_at(mid)
            if t is None or t >= before_ms:
                hi = mid
            else:
                lo = mid
        want = max(limit * 200, 65536)
        start = max(0, hi - want)
        f.seek(start)
        if start:
            f.readline()
        raw = f.read(min(size - start, want + 65536)).decode("utf-8", errors="replace")
    rows = [r for r in _parse_rows(raw, drop_torn_tail=False) if r[0] < before_ms]
    rows = rows[-limit:]
    first_ts = _file_first_ts(csv_path)
    has_more = bool(rows) and first_ts is not None and rows[0][0] > first_ts
    return rows, has_more


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
    # A file not ending in a newline is mid-append: the torn last row is
    # dropped by the parser.
    tail = _parse_rows(raw, drop_torn_tail=True)[-CANDLES_CAP:]
    _candle_cache[key] = (mtime, tail)
    return tail[-limit:]


@app.get("/api/candles/{pair}/{tf}")
def api_candles(pair: str, tf: str, limit: int = CANDLES_DEFAULT,
                before: int | None = None):
    """Last `limit` bars; `before` (ms) pages back in time (Stage 8b).
    `has_more` says whether older data exists on disk."""
    if registry.get(pair) is None:
        raise HTTPException(status_code=404, detail=f"unknown pair: {pair}")
    if tf not in WORKER_TFS:
        raise HTTPException(status_code=404, detail=f"unknown tf: {tf}")
    csv_path = paths.raw_csv(tf, pair)
    limit = max(1, min(int(limit), CANDLES_CAP))
    try:
        if before is not None:
            candles, has_more = _read_candles_before(csv_path, int(before), limit)
        else:
            candles = _read_candle_tail(csv_path, limit)
            first_ts = _file_first_ts(csv_path)
            has_more = bool(candles) and first_ts is not None and candles[0][0] > first_ts
    except OSError:                 # missing / vanished mid-read -> 404, not 500
        raise HTTPException(status_code=404,
                            detail=f"no candle data for {pair} {tf} yet")
    return {"pair": pair, "tf": tf, "candles": candles, "has_more": has_more}


# ------------------------------------------------------------------ health
@app.get("/api/health")
def api_health():
    now = time.time()
    pairs_out = {}
    # Worker-level liveness (stamped by the registry loop every ~10s):
    # detects a dead worker even when NO pair is ready yet (first
    # bootstrap, all-error registry) — a vacuous per-pair pass is not ok.
    worker_alive = False
    worker_phase = None
    try:
        whb = _read_cached(paths.worker_heartbeat_json()) or {}
        from datetime import datetime
        ts = whb.get("updated_at")
        if ts:
            worker_alive = (now - datetime.fromisoformat(ts).timestamp()) < STALE_AFTER_S
        # "startup" = alive and catching pairs up after a (re)start; "running"
        # = in the bar-close loops. Lets a watchdog tell a restart from a hang.
        worker_phase = whb.get("phase") if worker_alive else None
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
    body = {"ok": ok, "worker_alive": worker_alive, "worker_phase": worker_phase,
            "stale_after_s": STALE_AFTER_S, "pairs": pairs_out}
    return JSONResponse(status_code=200 if ok else 503, content=body)


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# --- App shell (B0a) -------------------------------------------------------
# The React build (frontend/dist) is served under /app; the legacy dashboard
# keeps "/" until Desk parity is verified and it is removed in its own commit.
# Deep links (/app/admin/health) fall back to index.html. index.html is never
# cached (a cached index was the "pushed but nothing changed" symptom of the
# old page); hashed assets under /app/assets are immutable.
APP_DIST = Path(__file__).resolve().parents[1] / "frontend" / "dist"


def _app_asset(rel: str) -> Path | None:
    """A file inside APP_DIST for the request path, or None (never outside it)."""
    if not rel:
        return None
    root = APP_DIST.resolve()
    try:
        candidate = (root / rel).resolve()
    except (OSError, RuntimeError):
        return None
    if candidate == root or not candidate.is_relative_to(root) or not candidate.is_file():
        return None
    return candidate


@app.get("/app", include_in_schema=False)
@app.get("/app/{rest:path}", include_in_schema=False)
def app_shell(rest: str = ""):
    index = APP_DIST / "index.html"
    if not index.is_file():
        raise HTTPException(
            status_code=503,
            detail="app shell not built: run `npm run build` in frontend/ (the Docker image builds it)",
        )
    asset = _app_asset(rest)
    if asset is not None:
        cache = "public, max-age=31536000, immutable" if rest.startswith("assets/") else "no-cache"
        return FileResponse(asset, headers={"Cache-Control": cache})
    return FileResponse(index, headers={"Cache-Control": "no-cache"})

# --- /api/v1 (B0a): the versioned, token-gated API the app shell reads.
# Imported last on purpose: service.api_v1 imports this module lazily
# (it reuses the legacy composition for state and candles).
from service.api_v1 import router as v1_router  # noqa: E402

app.include_router(v1_router)
