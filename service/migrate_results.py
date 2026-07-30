"""
service/migrate_results.py — one-time migration to the pair-scoped results
layout (Stage 0 of the service spec).

Moves the flat detector artifacts

    detectors/results/range_detector_{tf}_layer1.json
    detectors/results/range_memory_{tf}.json
    detectors/results/alerts_state.json

into detectors/results/SOL_USDT/ (the pre-migration data is all SOL).
Legacy artifacts (range_detector_v*_layer1.json, PNG panel dirs, named
snapshot dirs) are left where they are — nothing consumes them.

Idempotent: already-migrated files are skipped; an existing destination
file is never overwritten (the flat original is left in place and
reported so you can resolve manually).

Run:  python -m service.migrate_results
"""
from __future__ import annotations
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from detectors import paths

LEGACY_PAIR = "SOL_USDT"
FLAT = ROOT / "detectors" / "results"

TFS_L1 = ["1w", "1d", "4h", "2h", "1h", "5m"]
TFS_MEM = ["1w", "1d", "4h", "2h", "1h"]


def _candidates():
    for tf in TFS_L1:
        yield FLAT / f"range_detector_{tf}_layer1.json", paths.l1_json(tf, LEGACY_PAIR)
    for tf in TFS_MEM:
        yield FLAT / f"range_memory_{tf}.json", paths.mem_json(tf, LEGACY_PAIR)
    yield FLAT / "alerts_state.json", paths.alerts_state(LEGACY_PAIR)


def main():
    paths.ensure_results_dir(LEGACY_PAIR)
    moved = skipped = conflicts = 0
    for src, dst in _candidates():
        if not src.exists():
            skipped += 1
            continue
        if dst.exists():
            print(f"[migrate] CONFLICT: {dst.relative_to(ROOT)} already exists; "
                  f"left {src.name} in place")
            conflicts += 1
            continue
        src.replace(dst)          # same filesystem -> atomic rename
        print(f"[migrate] {src.relative_to(ROOT)} -> {dst.relative_to(ROOT)}")
        moved += 1
    print(f"[migrate] done: {moved} moved, {skipped} absent, {conflicts} conflict(s)")
    return 0 if conflicts == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
