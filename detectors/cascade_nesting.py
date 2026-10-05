"""
detectors/cascade_nesting.py — populate parent_range_id / child_range_ids
across the TF cascade. UTILITY ONLY: links existing records, no detection.

Cascade: 1H -> 4H -> 1D -> 1W (each child gets the smallest containing
parent one TF up). For a child range R, a parent candidate R' qualifies if
R's active time span [confirm, end] sits inside R''s span AND R's price
band [range_low_lower, range_high_upper] sits inside R''s band — both with
tolerance (bands across TFs differ by a few %, so strict containment is
too brittle; e.g. 1D R014 [79,97] vs 1W R012 [81,96]). Among qualifying
parents the SMALLEST (tightest price band) is chosen.

Active ranges (no end) use a far-future sentinel so an active parent can
contain an active child. Confirmed children with no qualifying parent are
reported as orphans (a 4H range with no 1D parent is unusual -> flag).

Only CONFIRMED ranges participate (pending/timeout/failed are skipped).
"""
from __future__ import annotations
import json, sys
from pathlib import Path
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from detectors import paths
from shared.ioutil import atomic_write_json      # never leave a truncated artifact

TFS = ["1w", "1d", "4h", "2h", "1h"]
CHILD_PARENT = [("1h", "2h"), ("2h", "4h"), ("4h", "1d"), ("1d", "1w")]
FAR = pd.Timestamp("2100-01-01", tz="UTC")

# tolerances
PRICE_TOL = 0.05                       # parent band expanded 5% each side
TIME_TOL = {"2h": pd.Timedelta(hours=6),   # ~3 parent bars on the start edge
            "4h": pd.Timedelta(hours=8),
            "1d": pd.Timedelta(days=2),
            "1w": pd.Timedelta(weeks=2)}


def _extent(r):
    s = pd.Timestamp(r["range_phase_2_ts"])
    e = pd.Timestamp(r["range_end_ts"]) if r["range_end_ts"] else FAR
    return s, e


def _contains(parent, child, parent_tf):
    # Time: the child's MIDPOINT must fall inside the parent's extent
    # (with tolerance). A faster child TF often confirms BEFORE its slower
    # parent (e.g. 4H R001 confirms May 14, its 1D parent R007 May 18), so
    # strict "parent starts first" containment is wrong. Midpoint-in-parent
    # is robust to that while still requiring the child to sit within the
    # parent's life.
    ps, pe = _extent(parent); cs, ce = _extent(child)
    tol = TIME_TOL[parent_tf]
    cmid = cs + (ce - cs) / 2 if ce < FAR else cs
    if not (ps - tol <= cmid <= pe + tol):
        return False
    plo = parent["range_low_lower"] * (1 - PRICE_TOL)
    phi = parent["range_high_upper"] * (1 + PRICE_TOL)
    return plo <= child["range_low_lower"] and child["range_high_upper"] <= phi


def _band_h(r):
    return r["range_high_upper"] - r["range_low_lower"]


def run(verbose=True, pair=paths.DEFAULT_PAIR):
    data = {tf: json.loads(paths.l1_json(tf, pair).read_text()) for tf in TFS}
    # reset links (idempotent)
    for tf in data:
        for r in data[tf]["ranges"]:
            r["parent_range_id"] = None
            r["child_range_ids"] = []
    # index confirmed ranges by id per TF
    conf = {tf: [r for r in data[tf]["ranges"] if r["is_confirmed"]] for tf in data}
    by_id = {r["range_id"]: r for tf in data for r in data[tf]["ranges"]}

    orphans = {}
    for child_tf, parent_tf in CHILD_PARENT:
        orphans[child_tf] = []
        for c in conf[child_tf]:
            cands = [p for p in conf[parent_tf] if _contains(p, c, parent_tf)]
            if not cands:
                orphans[child_tf].append(c["range_id"]); continue
            parent = min(cands, key=_band_h)   # smallest (tightest) parent
            c["parent_range_id"] = parent["range_id"]
            parent["child_range_ids"].append(c["range_id"])

    for tf in TFS:
        atomic_write_json(paths.l1_json(tf, pair), data[tf])

    if verbose:
        for child_tf, parent_tf in CHILD_PARENT:
            linked = sum(1 for c in conf[child_tf] if c["parent_range_id"])
            print(f"[nesting] {child_tf}->{parent_tf}: {linked}/{len(conf[child_tf])} linked, "
                  f"{len(orphans[child_tf])} orphan(s)")
            if orphans[child_tf]:
                print(f"           orphans: {orphans[child_tf][:8]}{'...' if len(orphans[child_tf])>8 else ''}")
    return data, orphans


if __name__ == "__main__":
    from shared.memhygiene import install        # Memory hygiene: see MEMORY_HYGIENE.md
    install("cascade_nesting")
    from data.freshness_monitor import consumer_startup   # Freshness contract: see MEMORY_HYGIENE.md
    consumer_startup(end_consumer=False)                   # CSV refresh only (regen-chain step; --no-freshness to skip)
    run()
