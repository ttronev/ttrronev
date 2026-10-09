"""The worker's stdout mirror (worker.log, served by /api/v1/logs/tail) is
opt-in: only the worker process turns it on (in main()). Library code, unit
tests and tools that import service.worker must never write into the live
results folder just by calling log()."""
from __future__ import annotations

from detectors import paths
from service import worker as worker_mod


def test_log_file_mirror_is_off_unless_enabled(sandbox, monkeypatch, capsys):
    monkeypatch.setattr(worker_mod, "LOG_FILE_ENABLED", False)
    worker_mod.log("hello from a test")
    assert "hello from a test" in capsys.readouterr().out
    assert not paths.worker_log().exists()


def test_log_file_mirror_appends_when_enabled(sandbox, monkeypatch):
    monkeypatch.setattr(worker_mod, "LOG_FILE_ENABLED", True)
    worker_mod.log("first")
    worker_mod.log("second")
    lines = paths.worker_log().read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert lines[0].endswith(" first") and lines[1].endswith(" second")
    assert lines[0][:4].isdigit()           # "YYYY-MM-DD HH:MM:SS msg"


def test_log_file_rotates_once_past_the_cap(sandbox, monkeypatch):
    monkeypatch.setattr(worker_mod, "LOG_FILE_ENABLED", True)
    monkeypatch.setattr(worker_mod, "LOG_FILE_MAX_BYTES", 64)
    for i in range(10):
        worker_mod.log(f"line {i} " + "x" * 40)
    current = paths.worker_log()
    rotated = current.with_name(current.name + ".1")
    assert current.exists() and rotated.exists()
    assert current.stat().st_size <= 64 + 80       # at most one line past the cap
