"""
shared/ioutil.py — atomic JSON writes + tolerant reads.

Memory hygiene: see docs/runbooks/memory_hygiene.md (no buffering beyond one document).

Every JSON artifact — the service's state/live/heartbeat files AND the detector
chain's layer-1 / range-memory files — is written to a temp file IN THE SAME
DIRECTORY and moved into place with os.replace(), which is atomic on NTFS and
POSIX. A reader can never observe a half-written file, and a process killed
mid-write (the worker has been OOM-killed before) leaves the previous complete
file in place instead of a truncated one that breaks every later read.

Lives in shared/ so detectors/ can use it without importing service/.
service/ioutil.py re-exports these names for existing imports.
"""
from __future__ import annotations
import json
import os
from pathlib import Path


def atomic_write_text(path, text: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp{os.getpid()}")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    finally:
        if tmp.exists():                      # only when replace did not happen
            try:
                tmp.unlink()
            except OSError:
                pass


def atomic_write_json(path, obj, indent: int = 2) -> None:
    atomic_write_text(path, json.dumps(obj, indent=indent))


def read_json(path, default=None):
    """Best-effort read: missing / unparsable file -> default."""
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return default


__all__ = ["atomic_write_text", "atomic_write_json", "read_json"]
