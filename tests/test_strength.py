"""Layer 5.1 strength scoring (detectors/layer5_1_strength)."""
import json

import pytest

from detectors import paths
from detectors.layer5_1_strength import (
    _factors, _score, classify_tf, WEIGHTS, TF_WEIGHT,
    NEUTRALIZED_CYCLES, STRONG_MAX_CYCLES, STRONG_SCORE,
)

CLEAN_SRC = {"time_inside_band_pct": 98.8, "active_duration_bars": 85}


def _level(cycles=0, touches=0, rejections=0, breaks=0):
    return {"cycle_count": cycles, "total_touches": touches,
            "total_rejections": rejections, "total_breaks": breaks}


def test_locked_constants():
    assert (NEUTRALIZED_CYCLES, STRONG_MAX_CYCLES, STRONG_SCORE) == (5, 1, 0.55)
    assert WEIGHTS == {"chop": 0.35, "hold": 0.30, "clean": 0.15, "dur": 0.10, "tf": 0.10}
    assert abs(sum(WEIGHTS.values()) - 1.0) < 1e-12
    assert TF_WEIGHT == {"1w": 1.0, "1d": 1.0, "4h": 0.6, "2h": 0.4, "1h": 0.3}


def test_factors_are_bounded():
    for lvl in (_level(), _level(9, 50, 3, 40), _level(2, 0, 7, 0)):
        for tf in TF_WEIGHT:
            f = _factors(lvl, CLEAN_SRC, tf)
            assert all(0.0 <= v <= 1.0 for v in f.values()), (lvl, tf, f)
            assert 0.0 <= _score(f) <= 1.0


def test_held_level_outscores_a_broken_one():
    held = _score(_factors(_level(0, 2, 4, 0), CLEAN_SRC, "4h"))
    lost = _score(_factors(_level(0, 0, 0, 4), CLEAN_SRC, "4h"))
    chopped = _score(_factors(_level(4, 2, 1, 5), CLEAN_SRC, "4h"))
    assert held > lost > chopped


def test_missing_source_cleanness_scores_zero_for_those_factors():
    f = _factors(_level(), {}, "1h")
    assert f["clean"] == 0.0 and f["dur"] == 0.0


def test_KNOWN_GAP_an_untested_level_scores_strong():
    """Audit hole 4 — pinned on purpose, not endorsed.

    A level nobody has ever touched gets chop=1.0 (no break/reclaim cycles)
    and a neutral hold=0.5, so on 1D it lands at 0.838 — comfortably 'strong'.
    'Strong' therefore means 'not yet disproved', not 'proved'. Evidence
    weighting is a planned, measured experiment; change this test with it."""
    score = _score(_factors(_level(), CLEAN_SRC, "1d"))
    assert round(score, 3) == 0.838
    assert score >= STRONG_SCORE


@pytest.mark.parametrize("cycles,expected", [(0, "strong"), (1, "strong"),
                                             (2, "weak"), (4, "weak"),
                                             (5, "neutralized"), (9, "neutralized")])
def test_cycle_count_gates_the_class(sandbox, cycles, expected):
    pair, tf = "GATE_USDT", "1d"
    paths.ensure_results_dir(pair)
    paths.l1_json(tf, pair).write_text(json.dumps({"ranges": [
        {"range_id": "R000", "cleanness_metrics": CLEAN_SRC}]}), encoding="utf-8")
    paths.mem_json(tf, pair).write_text(json.dumps({"levels": [
        {"level_id": "R000_HI", "source_range_id": "R000",
         **_level(cycles, 0, 6, 0)}]}), encoding="utf-8")
    res = classify_tf(tf, pair)
    lvl = json.loads(paths.mem_json(tf, pair).read_text())["levels"][0]
    assert lvl["strength_class"] == expected
    assert res["dist"][expected] == 1
    assert set(lvl["strength_factors"]) == set(WEIGHTS)
