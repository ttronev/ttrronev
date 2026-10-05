"""
service/ioutil.py — kept for existing imports; the implementation moved to
shared/ioutil.py so the detector chain can write atomically too without
importing service/.
"""
from __future__ import annotations
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from shared.ioutil import atomic_write_json, atomic_write_text, read_json  # noqa: F401

__all__ = ["atomic_write_json", "atomic_write_text", "read_json"]
