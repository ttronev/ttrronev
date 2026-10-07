"""/api/v1 (ТЗ-B0 §6, §7): every route answers 200 and validates against its
model on a fixture results folder; logs/tail is clamped to 2000 lines and
takes no path parameter; path-like values are 422; with TTRRONEV_API_KEY set
a missing or wrong key is 401 on every route while the un-versioned routes
are unaffected; health thresholds match the legacy /api/health.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from detectors import paths  # noqa: E402
from service import __version__ as APP_VERSION  # noqa: E402
from service import api as api_mod  # noqa: E402
from service import api_v1, registry  # noqa: E402
from shared.ioutil import atomic_write_json  # noqa: E402

PAIR = "TEST_USDT"
TFS = ["1w", "1d", "4h", "2h", "1h", "5m"]
ROUTES = [
    "/api/v1/health",
    "/api/v1/pairs",
    f"/api/v1/state/{PAIR}",
    f"/api/v1/candles/{PAIR}/1h",
    "/api/v1/logs/tail",
    "/api/v1/build/stages",
    "/api/v1/version",
]
MODELS = {
    "/api/v1/health": api_v1.HealthV1,
    "/api/v1/pairs": api_v1.PairsV1,
    f"/api/v1/state/{PAIR}": api_v1.StateV1,
    f"/api/v1/candles/{PAIR}/1h": api_v1.CandlesV1,
    "/api/v1/logs/tail": api_v1.LogsTailV1,
    "/api/v1/build/stages": api_v1.StagesV1,
    "/api/v1/version": api_v1.VersionV1,
}


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _write_worker_heartbeat(age_s: float = 0.0, phase: str = "running") -> None:
    now = datetime.now(timezone.utc)
    atomic_write_json(paths.worker_heartbeat_json(), {
        "updated_at": _iso(now - timedelta(seconds=age_s)),
        "phase": phase,
        "started_at": _iso(now - timedelta(hours=1)),
        "last_cycle_5m_s": 4.2,
        "rss_mb": 152.0,
    })


def _write_pair_heartbeat(pair: str, age_5m_s: float = 0.0) -> None:
    now = datetime.now(timezone.utc)
    stamps = {tf: _iso(now - timedelta(seconds=30)) for tf in TFS}
    stamps["5m"] = _iso(now - timedelta(seconds=age_5m_s))
    atomic_write_json(paths.heartbeat_json(pair), {"pair": pair, "tfs": stamps, "updated_at": _iso(now)})


@pytest.fixture
def fx(sandbox, monkeypatch):
    """A results folder with one ready pair, fresh heartbeats, a state, a
    live price, 12 hourly candles and a 2500-line worker log. Auth off."""
    monkeypatch.delenv(api_v1.ENV_API_KEY, raising=False)
    # service.registry resolves its path at import time: point it at the sandbox.
    monkeypatch.setattr(registry, "REGISTRY_PATH", paths.registry_json())
    monkeypatch.setattr(registry, "LOCK_PATH", paths.registry_json().with_suffix(".lock"))
    api_mod._cache.clear()
    api_mod._candle_cache.clear()

    now = datetime.now(timezone.utc)
    paths.results_root().mkdir(parents=True, exist_ok=True)
    paths.data_root().mkdir(parents=True, exist_ok=True)
    registry.save([{
        "pair": PAIR, "status": registry.READY,
        "added_ts": _iso(now - timedelta(hours=2)), "tfs_ready": list(TFS),
    }])
    _write_worker_heartbeat()
    paths.ensure_results_dir(PAIR)
    _write_pair_heartbeat(PAIR)
    atomic_write_json(paths.state_json(PAIR), {
        "pair": PAIR, "generated_at": _iso(now), "levels": [], "zones": [],
        "trend": {"1d": "up"}, "regime": {},
    })
    atomic_write_json(paths.live_json(PAIR), {"price": 100.0, "ts": _iso(now), "ok": True})

    t0 = int(now.timestamp() * 1000) // 3_600_000 * 3_600_000 - 12 * 3_600_000
    rows = [f"{t0 + i * 3_600_000},100,101,99,100.5,{1000 + i}" for i in range(12)]
    paths.raw_csv("1h", PAIR).write_text("timestamp,open,high,low,close,volume\n" + "\n".join(rows) + "\n", encoding="utf-8")
    paths.worker_log().write_text("".join(f"line {i}\n" for i in range(2500)), encoding="utf-8")
    return PAIR


@pytest.fixture
def client(fx):
    return TestClient(api_mod.app)


# ----------------------------------------------------------------- shape
@pytest.mark.parametrize("route", ROUTES)
def test_every_route_answers_200_and_validates(client, route):
    r = client.get(route)
    assert r.status_code == 200, r.text
    body = r.json()
    model = MODELS[route]
    parsed = model.model_validate(body)
    assert parsed.version == api_v1.API_VERSION
    datetime.fromisoformat(body["generated_at"])        # ISO, parseable


def test_state_is_the_legacy_body_wrapped(client):
    v1 = client.get(f"/api/v1/state/{PAIR}").json()
    legacy = client.get(f"/api/state/{PAIR}").json()
    assert v1["pair"] == PAIR
    assert v1["state"] == legacy
    assert v1["state"]["live"]["price"] == 100.0
    assert v1["state"]["levels"] == []


def test_candles_match_legacy_and_clamp(client):
    r = client.get(f"/api/v1/candles/{PAIR}/1h?limit=5").json()
    assert len(r["candles"]) == 5 and r["has_more"] is True
    assert r["candles"][-1][4] == 100.5
    legacy = client.get(f"/api/candles/{PAIR}/1h?limit=5").json()
    assert r["candles"] == legacy["candles"]
    r = client.get(f"/api/v1/candles/{PAIR}/1h?limit=5000").json()
    assert len(r["candles"]) == 12 and r["has_more"] is False


def test_pairs_lists_the_registry(client):
    r = client.get("/api/v1/pairs").json()
    assert [p["pair"] for p in r["pairs"]] == [PAIR]
    assert r["pairs"][0]["status"] == "ready"
    assert r["pairs"][0]["tfs_ready"] == TFS


def test_add_and_remove_pairs_wrap_the_legacy_contract(client, monkeypatch):
    monkeypatch.setattr(registry, "validate_symbol_okx", lambda pair: (True, ""))
    r = client.post("/api/v1/pairs", json={"symbol": "link/usdt"})
    assert r.status_code == 201, r.text
    body = api_v1.AddPairsV1.model_validate(r.json())
    assert body.queued == ["LINK_USDT"] and body.rejected == []
    assert "LINK_USDT" in [p["pair"] for p in client.get("/api/v1/pairs").json()["pairs"]]

    r = client.post("/api/v1/pairs", json={"symbols": ["link/usdt", "not a symbol!!"]})
    assert r.status_code == 422                       # all rejected: duplicate + invalid
    assert client.post("/api/v1/pairs", json={}).status_code == 422

    r = client.delete("/api/v1/pairs/LINK_USDT")
    assert r.status_code == 200 and r.json()["removed"] == "LINK_USDT"
    assert client.delete("/api/v1/pairs/LINK_USDT").status_code == 404
    assert client.delete("/api/v1/pairs/..%2Fx").status_code == 422


def test_unknown_pair_is_404(client):
    assert client.get("/api/v1/state/NOPE_USDT").status_code == 404
    assert client.get("/api/v1/candles/NOPE_USDT/1h").status_code == 404


def test_build_stages_mirrors_the_file(client):
    import json
    r = client.get("/api/v1/build/stages").json()
    on_disk = json.loads(paths.stages_json().read_text(encoding="utf-8"))
    assert [s["id"] for s in r["stages"]] == [s["id"] for s in on_disk["stages"]]
    assert r["updated"] == on_disk["updated"]


def test_version_fields(client, monkeypatch):
    r = client.get("/api/v1/version").json()
    assert r["app_version"] == APP_VERSION
    assert isinstance(r["git_commit"], str) and r["git_commit"]
    monkeypatch.setenv(api_v1.ENV_GIT_COMMIT, "abc1234")
    monkeypatch.setenv(api_v1.ENV_BUILT_AT, "2026-10-06T12:00:00+00:00")
    r = client.get("/api/v1/version").json()
    assert r["git_commit"] == "abc1234" and r["built_at"] == "2026-10-06T12:00:00+00:00"


# ------------------------------------------------------------- logs/tail
def test_logs_tail_default_and_clamp(client):
    r = client.get("/api/v1/logs/tail").json()
    assert len(r["lines"]) == api_v1.LOG_LINES_DEFAULT and r["truncated"] is True
    r = client.get("/api/v1/logs/tail?lines=5000").json()
    assert len(r["lines"]) == 2000 and r["truncated"] is True
    assert r["lines"][-1] == "line 2499" and r["lines"][0] == "line 500"
    assert r["file"] == "worker.log" and r["size_bytes"] > 0
    r = client.get("/api/v1/logs/tail?lines=3").json()
    assert r["lines"] == ["line 2497", "line 2498", "line 2499"]


def test_logs_tail_whole_small_file(client):
    paths.worker_log().write_text("a\nb\n", encoding="utf-8")
    r = client.get("/api/v1/logs/tail?lines=10").json()
    assert r["lines"] == ["a", "b"] and r["truncated"] is False


def test_logs_tail_missing_file_is_empty_not_500(client):
    paths.worker_log().unlink()
    r = client.get("/api/v1/logs/tail")
    assert r.status_code == 200
    assert r.json()["lines"] == [] and r.json()["size_bytes"] == 0


def test_logs_tail_takes_no_path_parameter(client):
    for q in ("file=/etc/passwd", "path=..%2F..%2F.env", "lines=5&name=x"):
        r = client.get(f"/api/v1/logs/tail?{q}")
        assert r.status_code == 422, q
    assert client.get("/api/v1/logs/tail?lines=0").status_code == 422
    assert client.get("/api/v1/logs/tail?lines=../x").status_code == 422


# ------------------------------------------------------------ validation
@pytest.mark.parametrize("url", [
    "/api/v1/state/..%2F..%2F.env",
    "/api/v1/state/sol.usdt",
    "/api/v1/state/SOL%2FUSDT",
    f"/api/v1/candles/{PAIR}/..",
    f"/api/v1/candles/{PAIR}/1h%2F..",
    "/api/v1/candles/..%2Fx/1h",
    f"/api/v1/candles/{PAIR}/1h?limit=abc",
    f"/api/v1/candles/{PAIR}/1h?before=..%2Fx",
])
def test_path_like_values_are_422(client, url):
    assert client.get(url).status_code == 422, url


# ------------------------------------------------------------------ auth
def test_auth_off_in_dev_mode(client):
    assert client.get("/api/v1/health").json()["auth_enabled"] is False


def test_auth_required_when_key_is_set(client, monkeypatch):
    monkeypatch.setenv(api_v1.ENV_API_KEY, "s3cret-key")
    for route in ROUTES:
        assert client.get(route).status_code == 401, route
        assert client.get(route, headers={"X-API-Key": "wrong"}).status_code == 401, route
        r = client.get(route, headers={"X-API-Key": "s3cret-key"})
        assert r.status_code == 200, (route, r.text)
    assert client.get("/api/v1/health", headers={"X-API-Key": "s3cret-key"}).json()["auth_enabled"] is True
    # the un-versioned routes and the pages are not gated
    assert client.get("/api/pairs").status_code == 200
    assert client.get("/api/health").status_code in (200, 503)
    assert client.get("/").status_code == 200
    assert client.get("/app/").status_code != 401


def test_key_never_echoed(client, monkeypatch):
    monkeypatch.setenv(api_v1.ENV_API_KEY, "s3cret-key")
    r = client.get("/api/v1/health", headers={"X-API-Key": "wrong-guess"})
    assert "s3cret-key" not in r.text and "wrong-guess" not in r.text


# ---------------------------------------------------------------- health
def test_health_fresh_is_ok(client):
    h = client.get("/api/v1/health").json()
    assert h["status"] == "ok" and h["worker_alive"] is True and h["worker_phase"] == "running"
    assert h["pairs_ready"] == 1 and h["pairs_total"] == 1 and h["pairs_stale"] == []
    assert h["cycle_5m_s"] == 4.2 and h["rss_mb"] == 152.0
    assert set(h["tfs"]) == set(TFS)
    assert h["tfs"]["5m"]["pairs_stamped"] == 1 and h["tfs"]["5m"]["age_s"] < 60
    assert list(h["pair_age_5m_s"]) == [PAIR] and h["pair_age_5m_s"][PAIR] < 60
    assert h["stale_after_s"] == 900 and h["warn_after_s"] == 300


def test_health_thresholds_match_the_legacy_dot(client):
    _write_pair_heartbeat(PAIR, age_5m_s=400)        # yellow: past WARN, before STALE
    api_mod._cache.clear()
    h = client.get("/api/v1/health").json()
    assert h["status"] == "degraded" and h["pairs_stale"] == []

    _write_pair_heartbeat(PAIR, age_5m_s=1000)       # red: past STALE
    api_mod._cache.clear()
    h = client.get("/api/v1/health").json()
    assert h["status"] == "degraded" and h["pairs_stale"] == [PAIR]
    legacy = client.get("/api/health")
    assert legacy.status_code == 503                 # same threshold, same verdict

    _write_pair_heartbeat(PAIR)
    _write_worker_heartbeat(phase="startup")
    api_mod._cache.clear()
    assert client.get("/api/v1/health").json()["status"] == "degraded"

    _write_worker_heartbeat(age_s=1000)              # worker dead
    api_mod._cache.clear()
    h = client.get("/api/v1/health").json()
    assert h["status"] == "down" and h["worker_alive"] is False and h["worker_phase"] is None


def test_health_survives_garbage_heartbeats(client):
    paths.heartbeat_json(PAIR).write_text('{"tfs": "nonsense"}', encoding="utf-8")
    paths.worker_heartbeat_json().write_text("not json", encoding="utf-8")
    api_mod._cache.clear()
    r = client.get("/api/v1/health")
    assert r.status_code == 200
    assert r.json()["status"] == "down" and r.json()["pairs_stale"] == [PAIR]
