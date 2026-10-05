"""Isolation between the live service and everything else (detectors/paths.py),
and atomic artifact writes (shared/ioutil.py)."""
import json
import os

import pytest

from detectors import paths
from shared import ioutil


# ----------------------------------------------------------------- sandbox
def test_default_roots_are_the_live_folders(monkeypatch):
    monkeypatch.delenv(paths.ENV_RESULTS_ROOT, raising=False)
    monkeypatch.delenv(paths.ENV_DATA_ROOT, raising=False)
    assert paths.results_root() == paths.ROOT / "detectors" / "results"
    assert paths.data_root() == paths.ROOT / "data" / "raw"
    assert not paths.is_sandboxed()
    assert paths.l1_json("1d", "SOL_USDT") == \
        paths.ROOT / "detectors" / "results" / "SOL_USDT" / "range_detector_1d_layer1.json"
    assert paths.raw_csv("1h", "SOL_USDT") == paths.ROOT / "data" / "raw" / "SOL_USDT_1h.csv"


def test_env_redirects_every_artifact_path(sandbox):
    live = paths.ROOT / "detectors" / "results"
    assert paths.is_sandboxed()
    for p in (paths.results_dir("X_USDT"), paths.l1_json("1h", "X_USDT"),
              paths.mem_json("1h", "X_USDT"), paths.alerts_state("X_USDT"),
              paths.state_json("X_USDT"), paths.live_json("X_USDT"),
              paths.heartbeat_json("X_USDT"), paths.registry_json(),
              paths.worker_heartbeat_json(), paths.backfill_state_json()):
        assert sandbox in p.parents, p
        assert live not in p.parents, p
    assert sandbox in paths.raw_csv("1h", "X_USDT").parents


def test_roots_are_read_at_call_time(tmp_path, monkeypatch):
    monkeypatch.setenv(paths.ENV_RESULTS_ROOT, str(tmp_path / "a"))
    first = paths.results_dir("X_USDT")
    monkeypatch.setenv(paths.ENV_RESULTS_ROOT, str(tmp_path / "b"))
    assert paths.results_dir("X_USDT") != first


def test_sandboxed_consumer_never_fetches_or_regens(sandbox, monkeypatch):
    """A research run must not append candles the live worker also appends,
    nor trigger a cascade regen."""
    from data import freshness_monitor as fm

    def boom(*a, **k):
        raise AssertionError("ensure_fresh must not run in a sandbox")
    monkeypatch.setattr(fm, "ensure_fresh", boom)
    monkeypatch.setattr("sys.argv", ["prog"])
    fm.consumer_startup(end_consumer=True, pair="X_USDT")     # returns quietly


def test_unsandboxed_consumer_still_refreshes(monkeypatch):
    from data import freshness_monitor as fm
    monkeypatch.delenv(paths.ENV_RESULTS_ROOT, raising=False)
    calls = []
    monkeypatch.setattr(fm, "ensure_fresh", lambda **k: calls.append(k))
    monkeypatch.setattr("sys.argv", ["prog"])
    fm.consumer_startup(end_consumer=True, pair="X_USDT")
    assert calls == [{"regen": True, "pair": "X_USDT"}]
    monkeypatch.setattr("sys.argv", ["prog", "--no-freshness"])
    fm.consumer_startup(end_consumer=True, pair="X_USDT")
    assert len(calls) == 1


def test_chain_writes_land_only_in_the_sandbox(pipeline, sandbox):
    pipeline(pair="ISO_USDT", cycles=4)
    written = sorted(p.name for p in paths.results_dir("ISO_USDT").iterdir())
    assert "range_detector_1h_layer1.json" in written
    assert "range_memory_1h.json" in written
    assert not any(".tmp" in name for name in written)      # no temp files left behind
    assert not (paths.ROOT / "detectors" / "results" / "ISO_USDT").exists()
    assert not (paths.ROOT / "data" / "raw" / "ISO_USDT_1h.csv").exists()


# ------------------------------------------------------------ atomic writes
def test_atomic_write_roundtrip(tmp_path):
    p = tmp_path / "deep" / "x.json"
    ioutil.atomic_write_json(p, {"a": 1})
    assert json.loads(p.read_text()) == {"a": 1}
    assert ioutil.read_json(p) == {"a": 1}
    assert [q.name for q in p.parent.iterdir()] == ["x.json"]


def test_failed_write_keeps_the_previous_file(tmp_path, monkeypatch):
    """A crash mid-write must leave the last complete artifact, not a
    truncated JSON (the worker has been killed mid-cycle before)."""
    p = tmp_path / "x.json"
    ioutil.atomic_write_json(p, {"version": 1})

    def exploding_replace(src, dst):
        raise OSError("simulated kill between write and rename")
    monkeypatch.setattr(os, "replace", exploding_replace)
    with pytest.raises(OSError):
        ioutil.atomic_write_json(p, {"version": 2})
    monkeypatch.undo()
    assert json.loads(p.read_text()) == {"version": 1}
    assert [q.name for q in tmp_path.iterdir()] == ["x.json"]   # temp cleaned up


def test_unserialisable_payload_never_touches_the_file(tmp_path):
    p = tmp_path / "x.json"
    ioutil.atomic_write_json(p, {"version": 1})
    with pytest.raises(TypeError):
        ioutil.atomic_write_json(p, {"bad": object()})
    assert json.loads(p.read_text()) == {"version": 1}


def test_read_json_tolerates_missing_and_corrupt(tmp_path):
    assert ioutil.read_json(tmp_path / "nope.json", default={}) == {}
    (tmp_path / "bad.json").write_text("{ truncated", encoding="utf-8")
    assert ioutil.read_json(tmp_path / "bad.json", default=None) is None


def test_service_ioutil_reexports_the_shared_implementation():
    from service import ioutil as svc
    assert svc.atomic_write_json is ioutil.atomic_write_json
    assert svc.read_json is ioutil.read_json
