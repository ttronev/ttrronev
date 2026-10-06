"""The app shell route: FastAPI serves frontend/dist at /app (ТЗ-B0 §4).

Checks: index + deep links (SPA fallback) return the built index.html with
no-cache; hashed assets are served immutable; a path-like value cannot read
outside frontend/dist; an unbuilt frontend answers 503 instead of a traceback;
the legacy dashboard at / is untouched.
"""
from __future__ import annotations

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from service import api as api_mod  # noqa: E402


@pytest.fixture
def client(sandbox):
    return TestClient(api_mod.app)


@pytest.fixture
def dist(tmp_path, monkeypatch):
    d = tmp_path / "dist"
    (d / "assets").mkdir(parents=True)
    (d / "index.html").write_text("<!doctype html><title>shell-index</title>", encoding="utf-8")
    (d / "assets" / "index-abc123.js").write_text("console.log('shell')", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("SECRET-OUTSIDE-DIST", encoding="utf-8")
    monkeypatch.setattr(api_mod, "APP_DIST", d)
    return d


@pytest.mark.parametrize("path", ["/app", "/app/", "/app/strategies", "/app/admin/health", "/app/no/such/page"])
def test_index_and_deep_links_serve_the_shell(client, dist, path):
    r = client.get(path)
    assert r.status_code == 200
    assert "shell-index" in r.text
    assert r.headers["cache-control"] == "no-cache"


def test_hashed_assets_are_immutable(client, dist):
    r = client.get("/app/assets/index-abc123.js")
    assert r.status_code == 200
    assert "console.log('shell')" in r.text
    assert "immutable" in r.headers["cache-control"]


def test_path_like_values_cannot_escape_dist(client, dist):
    for p in ("/app/%2e%2e/secret.txt", "/app/assets/%2e%2e/%2e%2e/secret.txt", "/app/..%2fsecret.txt"):
        r = client.get(p)
        assert "SECRET-OUTSIDE-DIST" not in r.text, p
        assert r.status_code in (200, 404), p


def test_unbuilt_frontend_answers_503(client, tmp_path, monkeypatch):
    monkeypatch.setattr(api_mod, "APP_DIST", tmp_path / "missing")
    r = client.get("/app/")
    assert r.status_code == 503
    assert "frontend" in r.json()["detail"]


def test_legacy_dashboard_root_is_untouched(client, dist):
    r = client.get("/")
    assert r.status_code == 200
    assert "ttrronev" in r.text
    assert "shell-index" not in r.text
