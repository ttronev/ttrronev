"""Layer 5 event state machine (detectors/layer5_range_memory._scan_level).

Hand-built bars around a level at 100 with the fixed v1 thresholds:
touch band +/-0.3% (99.7 .. 100.3), break buffer 1% (99 / 101), re-arm 3%.
Price starts above, so the level is SUPPORT (hold side = above)."""
import numpy as np

from detectors.layer5_range_memory import (
    _scan_level, _summarize, _passes_cleanness,
    TOUCH_BAND_PCT, BREAK_BUFFER_PCT, REARM_PCT, CLEANNESS,
)
from tests.synth import SHIB_SCALE

LEVEL = 100.0

#          close    high    low     what happens
BARS = [
    (102.0, 102.5, 101.5),   # 0  the source range's END bar (scan starts after it)
    (105.0, 105.5, 104.5),   # 1  above the level -> role = support
    (100.8, 101.5, 100.1),   # 2  wick into the band, no pierce      -> touched
    (100.5, 100.9, 100.2),   # 3  hovering inside 3%: dormant, no event
    (104.0, 104.4, 103.5),   # 4  >3% away: re-armed
    (100.4, 100.9, 99.5),    # 5  wick THROUGH the band, close holds  -> rejected
    (103.5, 103.9, 103.1),   # 6  re-armed
    (98.5, 100.2, 98.2),     # 7  close below the 1% buffer           -> broken
    (96.5, 96.9, 96.1),      # 8  re-armed (still broken)
    (99.9, 100.2, 99.0),     # 9  retest FROM BELOW, rejected lower   -> (nothing)
    (96.0, 96.4, 95.6),      # 10
    (101.5, 101.9, 100.6),   # 11 close back above the buffer         -> reclaimed
]


def _scan(bars=BARS, level=LEVEL, k=1.0):
    close = np.array([b[0] for b in bars]) * k
    high = np.array([b[1] for b in bars]) * k
    low = np.array([b[2] for b in bars]) * k
    n = len(bars)
    return _scan_level(level * k, 0, high=high, low=low, close=close,
                       ts_iso=[f"bar{i:02d}" for i in range(n)], n=n)


def test_thresholds_are_the_locked_v1_values():
    assert (TOUCH_BAND_PCT, BREAK_BUFFER_PCT, REARM_PCT) == (0.003, 0.01, 0.03)
    assert CLEANNESS == {"min_active_duration_bars": 10, "min_total_touches": 4,
                         "min_time_inside_band_pct": 85}


def test_event_sequence():
    ev = _scan()
    assert [(e["ts"], e["type"]) for e in ev] == [
        ("bar02", "historical_level_touched"),
        ("bar05", "rejected"),
        ("bar07", "broken"),
        ("bar11", "reclaimed"),
    ]
    assert {e["role"] for e in ev} == {"support"}


def test_one_event_per_visit_while_price_hovers():
    hover = BARS[:3] + [(100.4, 100.9, 100.0)] * 20      # sits on the level
    ev = _scan(hover)
    assert [e["type"] for e in ev] == ["historical_level_touched"]


def test_summary_counts_one_full_break_reclaim_cycle():
    s = _summarize(_scan())
    assert s["cycle_count"] == 1
    assert (s["total_touches"], s["total_rejections"],
            s["total_breaks"], s["total_reclaims"]) == (1, 1, 1, 1)
    assert s["last_event_type"] == "reclaimed"


def test_resistance_role_when_price_starts_below():
    bars = [(98.0, 98.5, 97.5), (95.0, 95.5, 94.5), (99.6, 99.9, 98.9),
            (95.5, 96.0, 95.0), (99.6, 100.5, 99.0)]
    ev = _scan(bars)
    assert [e["type"] for e in ev] == ["historical_level_touched", "rejected"]
    assert {e["role"] for e in ev} == {"resistance"}


def test_same_events_at_shib_scale():
    base, tiny = _scan(), _scan(k=SHIB_SCALE)
    assert [(e["ts"], e["type"]) for e in tiny] == [(e["ts"], e["type"]) for e in base]


def test_KNOWN_GAP_flip_retest_is_invisible_while_broken():
    """Audit hole 5 — pinned on purpose, not endorsed.

    Bar 9: the broken support is retested from underneath and rejected lower —
    the classic support-turned-resistance flip. v1 only watches for a RECLAIM
    while a level is broken, so nothing is logged and no alert can fire. When
    flip-retest events are added (as a measured experiment), this test must be
    rewritten in the same change."""
    ev = _scan()
    assert "bar09" not in [e["ts"] for e in ev]


def test_cleanness_gate():
    ok = {"active_duration_bars": 10, "n_distinct_high_touches": 2,
          "n_distinct_low_touches": 2, "time_inside_band_pct": 85.0}
    assert _passes_cleanness(ok)
    assert not _passes_cleanness({**ok, "active_duration_bars": 9})
    assert not _passes_cleanness({**ok, "n_distinct_low_touches": 1})
    assert not _passes_cleanness({**ok, "time_inside_band_pct": 84.9})
    # A detector re-run wipes cleanness to None until compute_cleanness runs:
    # the gate must reject (this once silently emptied the 1D registry).
    assert not _passes_cleanness(None)
    assert not _passes_cleanness({"active_duration_bars": None})
