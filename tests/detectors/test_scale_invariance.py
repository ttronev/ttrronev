"""Price-scale invariance: the stack must find the SAME structure whether an
asset trades at $100, $100,000 or SHIB's 0.000005.

Every rule in the detector is relative (percent of price), so scaling a candle
series by a constant must scale the output and change nothing else. Scaling by
a power of two is exact in binary floating point, which makes these checks
strict. They fail on any absolute-dollar assumption — the class of bug that
left SHIB with one significant digit (published prices were round(x, 6))."""
import json

import pytest

from detectors import paths
from detectors.range_detector_core import detect_ranges
import detectors.range_detector_1h as m1h
from detectors.layer5_range_memory import _scan_level
from tests.synth import synth_1h, scale_prices, SHIB_SCALE, BTC_SCALE

BAND_KEYS = ("range_low_lower", "range_low_upper", "range_high_lower", "range_high_upper")


def _detect(df):
    out, ranges = detect_ranges(df, df, m1h.config())
    return out, ranges


def test_synthetic_series_yields_a_confirmed_ended_range():
    _, ranges = _detect(synth_1h())
    ended = [r for r in ranges if r.is_confirmed and r.range_end_ts]
    assert ended, "fixture must contain a confirmed range that ended"


@pytest.mark.parametrize("k", [SHIB_SCALE, BTC_SCALE])
def test_core_detection_is_scale_invariant(k):
    base_df = synth_1h()
    out_b, base = _detect(base_df)
    out_s, scaled = _detect(scale_prices(base_df, k))

    assert out_s["n_bos_events"] == out_b["n_bos_events"]
    assert [r.range_id for r in scaled] == [r.range_id for r in base]
    for rb, rs in zip(base, scaled):
        assert (rs.is_confirmed, rs.phase_1_outcome, rs.range_phase_2_ts,
                rs.range_end_ts, rs.range_end_reason) == \
               (rb.is_confirmed, rb.phase_1_outcome, rb.range_phase_2_ts,
                rb.range_end_ts, rb.range_end_reason)
        if not rb.is_confirmed:
            continue
        for key in BAND_KEYS:
            vb, vs = getattr(rb, key), getattr(rs, key)
            assert abs(vs / k - vb) / vb < 1e-4, (rb.range_id, key, vb, vs)
        assert len(rs.band_history) == len(rb.band_history)
        assert len(rs.failed_break_events) == len(rb.failed_break_events)


def test_shib_scale_bands_keep_their_digits():
    base_df = synth_1h()
    _, base = _detect(base_df)
    _, shib = _detect(scale_prices(base_df, SHIB_SCALE))
    checked = 0
    for rb, rs in zip(base, shib):
        if not rb.is_confirmed:
            continue
        # A real range has a low edge clearly below its high edge...
        assert rs.range_low_lower < rs.range_high_upper
        height_b = (rb.range_high_upper - rb.range_low_lower) / rb.range_low_lower
        height_s = (rs.range_high_upper - rs.range_low_lower) / rs.range_low_lower
        assert abs(height_s - height_b) < 1e-3
        # ...and this is exactly what the old rounding destroyed: at this
        # scale round(x, 6) is off by whole percents.
        legacy_err = abs(round(rs.range_low_lower, 6) - rs.range_low_lower) / rs.range_low_lower
        assert legacy_err > 0.01, "test is not sensitive to the bug it guards"
        checked += 1
    assert checked >= 1


@pytest.mark.parametrize("k", [SHIB_SCALE, BTC_SCALE])
def test_level_event_scan_is_scale_invariant(k):
    df = synth_1h(seed=10, cycles=6)        # yields touched + broken + reclaimed
    hi, lo, cl = (df[c].to_numpy(float) for c in ("high", "low", "close"))
    n = len(df)
    ts = [str(i) for i in range(n)]
    price, end_idx = float(cl[150]), 150
    base = _scan_level(price, end_idx, high=hi, low=lo, close=cl, ts_iso=ts, n=n)
    scaled = _scan_level(price * k, end_idx, high=hi * k, low=lo * k, close=cl * k,
                         ts_iso=ts, n=n)
    assert {e["type"] for e in base} >= {"historical_level_touched", "broken", "reclaimed"}
    assert [(e["ts"], e["type"], e["role"]) for e in scaled] == \
           [(e["ts"], e["type"], e["role"]) for e in base]
    for eb, es in zip(base, scaled):
        assert abs(es["level_price"] / k - eb["level_price"]) / eb["level_price"] < 1e-4


def test_whole_chain_is_scale_invariant(pipeline):
    """Detector -> cleanness -> nesting -> known_at -> range memory -> strength
    -> published state: same levels, same events, same classes at SHIB scale."""
    from detectors.query_state import State
    from service.state_builder import build_state

    base = pipeline(pair="BASE_USDT")
    shib = pipeline(pair="TINY_USDT", scale=SHIB_SCALE)

    total_levels = 0
    for tf in ("1w", "1d", "4h", "2h", "1h"):
        mb, ms = base["mem"][tf], shib["mem"][tf]
        assert ms["n_levels"] == mb["n_levels"], tf
        assert ms["events_by_type"] == mb["events_by_type"], tf
        assert shib["strength"][tf]["dist"] == base["strength"][tf]["dist"], tf
        total_levels += mb["n_levels"]
    assert total_levels > 0, "fixture should produce levels"

    # Published level prices: distinct at SHIB scale (they all collapsed onto
    # one or two values before) and equal to the scaled base prices.
    mem_b = json.loads(paths.mem_json("1h", "BASE_USDT").read_text())
    mem_s = json.loads(paths.mem_json("1h", "TINY_USDT").read_text())
    pb = [L["price"] for L in mem_b["levels"]]
    ps = [L["price"] for L in mem_s["levels"]]
    assert len(set(ps)) == len(set(pb))
    for b, s in zip(pb, ps):
        assert abs(s / SHIB_SCALE - b) / b < 1e-4

    doc_b = build_state("BASE_USDT", state=State("BASE_USDT", live=False))
    doc_s = build_state("TINY_USDT", state=State("TINY_USDT", live=False))
    assert len(doc_s["levels"]) == len(doc_b["levels"])
    assert len(doc_s["zones"]) == len(doc_b["zones"])
    assert [z["confluence"] for z in doc_s["zones"]] == [z["confluence"] for z in doc_b["zones"]]
    assert doc_s["regime"] == doc_b["regime"] and doc_s["trend"] == doc_b["trend"]
