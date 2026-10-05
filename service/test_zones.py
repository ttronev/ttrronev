"""Unit test for the Stage 9a zone builder (service/state_builder zones).

Fixture is the real SOL_USDT nearest-price levels at ref = last 1h close
$96.98 (2026-08-26), hardcoded so the test is deterministic and needs no CSVs.

Run:  python -m service.test_zones
"""
from __future__ import annotations
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from service.state_builder import (
    _build_zones, _budget_zones, _clamp,
    ZONE_TOL_MIN, ZONE_TOL_MAX, ZONE_TOL_MULT,
)

REF = 96.98
TOL = 0.0035          # ~ the real ATR-normalized tol (0.3486%) at this snapshot

# Real SOL nearest levels (tf, price, score, class). last_event omitted where
# not needed; the builder tolerates last_event_ts=None.
def _L(tf, price, score, cls, ts=None, ev=None):
    return {"tf": tf, "price": price, "score": score, "class": cls,
            "source": f"{tf}_{price}", "last_event": ev, "last_event_ts": ts}

FIXTURE = [
    _L("2h", 96.88, 0.7957, "strong"), _L("1d", 97.30, 0.9880, "strong"),
    _L("2h", 96.41, 0.8333, "strong"), _L("1h", 96.37, 0.7255, "strong"),
    _L("2h", 94.26, 0.5508, "strong", "2026-08-22T02:00:00+00:00", "reclaimed"),
    _L("1h", 94.26, 0.6355, "strong", "2026-08-22T05:00:00+00:00", "historical_level_touched"),
    _L("1h", 93.42, 0.5212, "weak"),  _L("2h", 93.10, 0.6580, "strong"),
    _L("1h", 93.04, 0.6428, "strong"), _L("2h", 92.94, 0.7100, "strong"),
    _L("2h", 92.91, 0.5508, "strong"), _L("1h", 92.72, 0.6698, "strong"),
    _L("2h", 92.30, 0.6833, "strong"), _L("1h", 91.87, 0.4912, "weak"),
    _L("1h", 91.35, 0.6123, "strong"),
    _L("4h", 90.12, 0.7810, "strong", "2026-08-22T04:00:00+00:00", "rejected"),
    _L("2h", 90.12, 0.6545, "strong", "2026-08-22T04:00:00+00:00", "rejected"),
    _L("2h", 90.08, 0.5205, "weak"),  _L("2h", 89.88, 0.6580, "strong"),
    _L("1h", 89.88, 0.5480, "weak"),
]

PACK = [92.72, 92.91, 92.94, 93.04, 93.10]     # the "92.7-93.1 pack" (5 lines)


def _zones():
    return _build_zones(FIXTURE, REF, TOL)


def _zone_of(zones, price):
    hits = [z for z in zones if z["price_lo"] - 1e-9 <= price <= z["price_hi"] + 1e-9]
    assert len(hits) == 1, f"{price} in {len(hits)} zones, expected 1"
    return hits[0]


def test_reduces_lines_to_zones():
    z = _zones()
    assert len(z) < len(FIXTURE), f"no reduction: {len(z)} zones from {len(FIXTURE)} levels"
    assert len(z) <= 18, f"{len(z)} zones exceeds the 18-zone target"


def test_merge_94_26_1h_2h():
    z = _zone_of(_zones(), 94.26)
    assert z["confluence"] >= 2, z
    assert {"2h", "1h"} <= set(z["tfs"]), z["tfs"]


def test_merge_90_12_4h_2h():
    z = _zone_of(_zones(), 90.12)
    assert z["confluence"] >= 2, z
    assert {"4h", "2h"} <= set(z["tfs"]), z["tfs"]


def test_pack_92_93_is_one_band():
    zones = _zones()
    bands = {(z["price_lo"], z["price_hi"]) for z in (_zone_of(zones, p) for p in PACK)}
    assert len(bands) == 1, f"pack split across {len(bands)} zones: {bands}"
    band = _zone_of(zones, PACK[0])
    assert band["confluence"] >= 5, band["confluence"]


def test_no_two_zones_within_tol():
    z = sorted(_zones(), key=lambda x: x["price"])
    for a, b in zip(z, z[1:]):
        gap = (b["price_lo"] - a["price_hi"]) / a["price_hi"]
        assert gap > TOL, f"zones {a['price_hi']} and {b['price_lo']} gap {gap:.5f} <= tol {TOL}"


def test_center_is_score_weighted():
    # The 90.12 cluster's center leans toward the high-score 4h ($90.12, .781)
    # over the low-score weak members ($89.88 .548) -> center > simple midpoint.
    z = _zone_of(_zones(), 90.12)
    mid = (z["price_lo"] + z["price_hi"]) / 2
    assert z["price"] > mid, f"center {z['price']} not weighted above midpoint {mid}"


def test_budget_keeps_nearest_even_when_outranked():
    zones = _zones()
    # per_side=1 keeps only the top-RANKED zone per side; the nearest-below zone
    # ($96.88, 2h) is outranked by the 4h cluster, yet the rule must keep it.
    kept = _budget_zones(zones, REF, per_side=1)
    below = [z for z in zones if z["price"] <= REF]
    nearest_below = min(below, key=lambda z: abs(z["price"] - REF))
    assert any(z is nearest_below for z in kept), "nearest-below zone was dropped by the budget"


def test_tol_clamps_both_ends():
    assert _clamp(ZONE_TOL_MULT * 0.0005, ZONE_TOL_MIN, ZONE_TOL_MAX) == ZONE_TOL_MIN   # tiny ATR
    assert _clamp(ZONE_TOL_MULT * 0.10, ZONE_TOL_MIN, ZONE_TOL_MAX) == ZONE_TOL_MAX      # huge ATR
    mid = ZONE_TOL_MULT * 0.012                                                          # ~atr 1.2%
    assert _clamp(mid, ZONE_TOL_MIN, ZONE_TOL_MAX) == mid, "mid value should pass through"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"[test_zones] {name}: OK")
    print("[test_zones] all tests passed")
