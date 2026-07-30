"""
service/ioutil.py — atomic JSON writes + tolerant reads for the service files.

Every JSON the worker publishes (state.json, live.json, heartbeat.json) is
written to a temp file IN THE SAME DIRECTORY and moved into place with
os.replace(), which is atomic on both NTFS and POSIX — the API process can
never observe a half-written file.
"""
from __future__ import annotations
import json
import os
from pathlib import Path


def atomic_write_json(path: Path, obj) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp{os.getpid()}")
    tmp.write_text(json.dumps(obj, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def read_json(path: Path, default=None):
    """Best-effort read: missing / unparsable file -> default."""
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return default
