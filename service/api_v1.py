"""/api/v1 — the versioned API the app shell reads (ТЗ-B0 §6).

Every response is a pydantic model carrying `version` and `generated_at`.

Auth: every request carries `X-API-Key`, compared in constant time with
TTRRONEV_API_KEY. When the variable is unset auth is OFF (dev mode) and
`/api/v1/health` reports `auth_enabled: false`, which the shell turns into
its persistent "dev mode, no auth" banner. The key is never logged.

The un-versioned /api/* routes are untouched. Where the shape must stay
identical to what the legacy page sees (state, candles) the handlers call the
legacy composition functions and wrap the result in the envelope, so the Desk
port reads exactly the same data.

List endpoints are wrapped in an object (`pairs`, `candles`, `stages`) so the
envelope fields have somewhere to live.

`/logs/tail` serves the worker's own log file (detectors/paths.worker_log)
and takes exactly one parameter, `lines`; any other query parameter is a 422.

Logging through `logging`; no print.
"""
from __future__ import annotations

import hmac
import logging
import os
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi import Path as PathParam
from pydantic import BaseModel, Field

from detectors import paths
from service import __version__ as APP_VERSION
from service import registry
from service.pairs import WORKER_TFS

log = logging.getLogger("ttrronev.api_v1")

API_VERSION = "1"
ENV_API_KEY = "TTRRONEV_API_KEY"
ENV_GIT_COMMIT = "TTRRONEV_GIT_COMMIT"
ENV_BUILT_AT = "TTRRONEV_BUILT_AT"

# Health thresholds — the same numbers the legacy /api/health and dashboard
# dot use: a pair is stale when its 5m stamp is older than STALE_AFTER_S; the
# dot turns yellow past WARN_AFTER_S.
STALE_AFTER_S = 15 * 60
WARN_AFTER_S = 5 * 60

LOG_LINES_DEFAULT = 500
LOG_LINES_MAX = 2000
LOG_TAIL_MAX_BYTES = 4 * 1024 * 1024
LOG_ALLOWED_PARAMS = frozenset({"lines"})

CANDLES_DEFAULT = 500
CANDLES_CAP = 1500

# A pair id is `BASE_QUOTE` in capitals/digits; anything else (a path, a dot,
# a slash) is rejected with 422 before it reaches the filesystem layer.
PAIR_PATTERN = r"^[A-Z0-9]{2,15}_[A-Z0-9]{2,10}$"
Tf = Literal["1w", "1d", "4h", "2h", "1h", "5m"]
assert set(Tf.__args__) == set(WORKER_TFS), "Tf literal must match service.pairs.WORKER_TFS"  # type: ignore[attr-defined]


# ------------------------------------------------------------------ auth
def configured_api_key() -> str | None:
    """The configured key, or None when auth is off (dev mode). Read per
    request so a container restart is not needed to turn auth on."""
    value = os.environ.get(ENV_API_KEY, "").strip()
    return value or None


def auth_enabled() -> bool:
    return configured_api_key() is not None


def require_api_key(x_api_key: str | None = Header(default=None, alias="X-API-Key")) -> None:
    key = configured_api_key()
    if key is None:
        return
    if not x_api_key or not hmac.compare_digest(x_api_key.encode("utf-8"), key.encode("utf-8")):
        raise HTTPException(
            status_code=401,
            detail="missing or invalid X-API-Key",
            headers={"WWW-Authenticate": "ApiKey"},
        )


router = APIRouter(prefix="/api/v1", tags=["v1"], dependencies=[Depends(require_api_key)])


# ---------------------------------------------------------------- models
def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class Envelope(BaseModel):
    version: str = API_VERSION
    generated_at: str = Field(default_factory=now_iso)


class TfHealth(BaseModel):
    last_regen_ts: str | None = None   # newest stamp across ready pairs
    age_s: float | None = None
    max_age_s: float | None = None     # oldest stamp across ready pairs (worst pair)
    pairs_stamped: int = 0


class HealthV1(Envelope):
    status: Literal["ok", "degraded", "down"]
    worker_alive: bool
    worker_phase: str | None
    auth_enabled: bool
    tfs: dict[str, TfHealth]
    pairs_ready: int
    pairs_total: int
    pairs_stale: list[str]
    pair_age_5m_s: dict[str, float | None]   # ready pairs: seconds since their last 5m regen
    cycle_5m_s: float | None      # last 5m pass duration, from the worker heartbeat
    rss_mb: float | None          # worker peak RSS so far, from the worker heartbeat
    stale_after_s: int = STALE_AFTER_S
    warn_after_s: int = WARN_AFTER_S


class PairV1(BaseModel):
    pair: str
    status: str
    tfs_ready: list[str]
    added_ts: str | None = None
    error_reason: str | None = None


class PairsV1(Envelope):
    pairs: list[PairV1]


class AddPairsBody(BaseModel):
    symbol: str | None = None
    symbols: list[str] | None = None


class AddPairsV1(Envelope):
    queued: list[str]
    rejected: list[dict[str, str]]


class RemovePairV1(Envelope):
    removed: str


class StateV1(Envelope):
    pair: str
    state: dict[str, Any]         # the legacy /api/state body, shape unchanged


class CandlesV1(Envelope):
    pair: str
    tf: str
    candles: list[list[float]]    # [t, o, h, l, c, v]
    has_more: bool


class LogsTailV1(Envelope):
    lines: list[str]
    file: str
    size_bytes: int
    truncated: bool               # the file holds more lines than returned


class StageV1(BaseModel):
    id: str
    name: str
    track: str
    depends_on: list[str]
    status: Literal["not_started", "in_progress", "in_review", "blocked", "done"]
    evidence_url: str | None
    updated: str


class StagesV1(Envelope):
    updated: str
    source: str
    stages: list[StageV1]


class VersionV1(Envelope):
    git_commit: str
    built_at: str | None
    app_version: str


# --------------------------------------------------------------- helpers
def _legacy():
    # Lazy: service.api includes this router, so a top-level import would be
    # circular. By the time a request arrives service.api is fully loaded.
    import service.api as legacy
    return legacy


def _parse_ts(value: Any) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value).timestamp()
    except ValueError:
        return None


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=UTC).isoformat(timespec="seconds")


def _tail_lines(path: Path, n: int, size: int) -> tuple[list[str], bool]:
    """Last `n` complete lines of a text file, reading only its tail bytes
    (doubling the window until enough lines are in hand, capped at
    LOG_TAIL_MAX_BYTES). Returns (lines, truncated)."""
    want = min(size, max(n * 160, 65536))
    with path.open("rb") as f:
        while True:
            start = max(0, size - want)
            f.seek(start)
            text = f.read(size - start).decode("utf-8", errors="replace")
            lines = text.split("\n")
            if start > 0:
                lines = lines[1:]                   # partial first line
            if lines and lines[-1] == "":
                lines = lines[:-1]                  # trailing newline
            if start == 0 or len(lines) > n or want >= LOG_TAIL_MAX_BYTES:
                break
            want = min(size, want * 2, LOG_TAIL_MAX_BYTES)
    lines = [ln.rstrip("\r") for ln in lines]
    truncated = len(lines) > n or start > 0
    return lines[-n:], truncated


_git_commit_cache: str | None = None


def git_commit() -> str:
    """TTRRONEV_GIT_COMMIT (stamped by the image build), else the checkout's
    short HEAD when a .git folder is present (dev), else "unknown"."""
    global _git_commit_cache
    env = os.environ.get(ENV_GIT_COMMIT, "").strip()
    if env:
        return env
    if _git_commit_cache is None:
        _git_commit_cache = "unknown"
        if (paths.ROOT / ".git").exists():
            try:
                out = subprocess.run(
                    ["git", "rev-parse", "--short", "HEAD"],
                    cwd=paths.ROOT, capture_output=True, text=True, timeout=5, check=False,
                )
                if out.returncode == 0 and out.stdout.strip():
                    _git_commit_cache = out.stdout.strip()
            except (OSError, subprocess.SubprocessError):
                pass
    return _git_commit_cache


def built_at() -> str | None:
    return os.environ.get(ENV_BUILT_AT, "").strip() or None


# ---------------------------------------------------------------- routes
@router.get("/health", response_model=HealthV1)
def health() -> HealthV1:
    legacy = _legacy()
    now = time.time()

    whb = legacy._read_cached(paths.worker_heartbeat_json())
    whb = whb if isinstance(whb, dict) else {}
    whb_ts = _parse_ts(whb.get("updated_at"))
    worker_alive = whb_ts is not None and (now - whb_ts) < STALE_AFTER_S
    phase = whb.get("phase") if worker_alive and isinstance(whb.get("phase"), str) else None

    entries = [e for e in registry.load() if isinstance(e, dict) and "pair" in e]
    ready = [e["pair"] for e in entries if e.get("status") == registry.READY]

    latest: dict[str, float] = {}
    oldest: dict[str, float] = {}
    counts: dict[str, int] = {}
    stale: list[str] = []
    pair_age: dict[str, float | None] = {}
    worst_5m_age: float | None = None
    for pair in ready:
        hb = legacy._read_cached(paths.heartbeat_json(pair))
        stamps = hb.get("tfs") if isinstance(hb, dict) else None
        stamps = stamps if isinstance(stamps, dict) else {}
        age_5m: float | None = None
        for tf, raw in stamps.items():
            ts = _parse_ts(raw)
            if ts is None:
                continue
            latest[tf] = max(latest.get(tf, ts), ts)
            oldest[tf] = min(oldest.get(tf, ts), ts)
            counts[tf] = counts.get(tf, 0) + 1
            if tf == "5m":
                age_5m = now - ts
        pair_age[pair] = round(age_5m, 1) if age_5m is not None else None
        if age_5m is None or age_5m > STALE_AFTER_S:
            stale.append(pair)
        elif worst_5m_age is None or age_5m > worst_5m_age:
            worst_5m_age = age_5m

    tfs: dict[str, TfHealth] = {}
    for tf in WORKER_TFS:
        if tf in latest:
            tfs[tf] = TfHealth(
                last_regen_ts=_iso(latest[tf]),
                age_s=round(now - latest[tf], 1),
                max_age_s=round(now - oldest[tf], 1),
                pairs_stamped=counts[tf],
            )
        else:
            tfs[tf] = TfHealth()

    if not worker_alive:
        status: Literal["ok", "degraded", "down"] = "down"
    elif phase == "startup" or stale or (worst_5m_age is not None and worst_5m_age > WARN_AFTER_S):
        status = "degraded"
    else:
        status = "ok"

    cycle = whb.get("last_cycle_5m_s")
    rss = whb.get("rss_mb")
    return HealthV1(
        status=status,
        worker_alive=worker_alive,
        worker_phase=phase,
        auth_enabled=auth_enabled(),
        tfs=tfs,
        pairs_ready=len(ready),
        pairs_total=len(entries),
        pairs_stale=stale,
        pair_age_5m_s=pair_age,
        cycle_5m_s=float(cycle) if isinstance(cycle, (int, float)) else None,
        rss_mb=float(rss) if isinstance(rss, (int, float)) else None,
    )


@router.get("/pairs", response_model=PairsV1)
def pairs() -> PairsV1:
    out = []
    for e in registry.load():
        if not isinstance(e, dict) or "pair" not in e:
            continue
        out.append(PairV1(
            pair=str(e["pair"]),
            status=str(e.get("status") or "unknown"),
            tfs_ready=[str(t) for t in (e.get("tfs_ready") or [])],
            added_ts=e.get("added_ts") if isinstance(e.get("added_ts"), str) else None,
            error_reason=e.get("error_reason") if isinstance(e.get("error_reason"), str) else None,
        ))
    return PairsV1(pairs=out)


@router.post("/pairs", response_model=AddPairsV1, status_code=201)
def add_pairs(body: AddPairsBody) -> AddPairsV1:
    """Same contract as the legacy POST /api/pairs (single `symbol` or a
    `symbols` batch; all-rejected is 422/409), wrapped in the envelope."""
    legacy = _legacy()
    res = legacy.api_add_pair(legacy.AddPairBody(symbol=body.symbol, symbols=body.symbols))
    return AddPairsV1(queued=list(res["queued"]), rejected=list(res["rejected"]))


@router.delete("/pairs/{pair}", response_model=RemovePairV1)
def remove_pair(pair: str = PathParam(pattern=PAIR_PATTERN)) -> RemovePairV1:
    res = _legacy().api_remove_pair(pair)          # 404 for an unknown pair; disk untouched
    return RemovePairV1(removed=str(res["removed"]))


@router.get("/state/{pair}", response_model=StateV1)
def state(pair: str = PathParam(pattern=PAIR_PATTERN)) -> StateV1:
    body = _legacy().api_state(pair)          # 404 for an unknown pair, same shape as the legacy page
    return StateV1(pair=pair, state=body)


@router.get("/candles/{pair}/{tf}", response_model=CandlesV1)
def candles(
    pair: str = PathParam(pattern=PAIR_PATTERN),
    tf: Tf = PathParam(),
    limit: int = Query(CANDLES_DEFAULT, ge=1),
    before: int | None = Query(None, ge=0),
) -> CandlesV1:
    limit = min(limit, CANDLES_CAP)
    body = _legacy().api_candles(pair, tf, limit=limit, before=before)
    return CandlesV1(pair=pair, tf=tf, candles=body["candles"], has_more=bool(body.get("has_more")))


# Path-like values. A decoded slash inside a parameter (`..%2F..%2F.env`)
# never matches the routes above, so without these fallbacks it would be a
# plain 404. The contract (ТЗ-B0 §7) is 422 for any path-like value.
@router.get("/state/{rest:path}", include_in_schema=False)
def state_path_like(rest: str) -> None:
    raise HTTPException(status_code=422, detail="pair must match BASE_QUOTE; path-like values are rejected")


@router.delete("/pairs/{rest:path}", include_in_schema=False)
def remove_pair_path_like(rest: str) -> None:
    raise HTTPException(status_code=422, detail="pair must match BASE_QUOTE; path-like values are rejected")


@router.get("/candles/{rest:path}", include_in_schema=False)
def candles_path_like(rest: str) -> None:
    raise HTTPException(
        status_code=422,
        detail=f"expected /candles/{{BASE_QUOTE}}/{{tf}} with tf in {list(WORKER_TFS)}; path-like values are rejected",
    )


@router.get("/logs/tail", response_model=LogsTailV1)
def logs_tail(request: Request, lines: int = Query(LOG_LINES_DEFAULT, ge=1)) -> LogsTailV1:
    extra = sorted(set(request.query_params.keys()) - LOG_ALLOWED_PARAMS)
    if extra:
        raise HTTPException(
            status_code=422,
            detail=f"unsupported parameter(s) {extra}: logs/tail serves the service log only and takes `lines`",
        )
    n = min(lines, LOG_LINES_MAX)
    path = paths.worker_log()
    try:
        size = path.stat().st_size
    except OSError:
        return LogsTailV1(lines=[], file=path.name, size_bytes=0, truncated=False)
    try:
        tail, truncated = _tail_lines(path, n, size)
    except OSError:
        log.warning("logs/tail: %s vanished mid-read", path.name)
        return LogsTailV1(lines=[], file=path.name, size_bytes=0, truncated=False)
    return LogsTailV1(lines=tail, file=path.name, size_bytes=size, truncated=truncated)


@router.get("/build/stages", response_model=StagesV1)
def build_stages() -> StagesV1:
    data = _legacy()._read_cached(paths.stages_json())
    if not isinstance(data, dict) or not isinstance(data.get("stages"), list):
        raise HTTPException(status_code=503, detail="docs/plan/stages.json missing or unreadable")
    return StagesV1(
        updated=str(data.get("updated", "")),
        source=str(data.get("source", "")),
        stages=[StageV1(**s) for s in data["stages"]],
    )


@router.get("/version", response_model=VersionV1)
def version() -> VersionV1:
    return VersionV1(git_commit=git_commit(), built_at=built_at(), app_version=APP_VERSION)
