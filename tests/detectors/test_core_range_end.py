"""How a confirmed range ends (range_detector_core.simulate_confirmed), and
what compute_known_at says about when that end is knowable.

A close outside the band opens a recovery window of `recovery_lookahead` bars:
price back inside => failed break, the range lives on; no return => the range
ended at the break bar."""
import numpy as np
import pandas as pd

from detectors.range_detector_core import CoreRangeConfig, simulate_confirmed, _cluster_band

CFG = CoreRangeConfig(timeframe="1h", band_zone_pct=0.25, recovery_lookahead=4)

# Bars 0..9 oscillate 100..104 (the confirmed range). Index 9 = confirm bar.
INSIDE = [100.0, 104.0, 101.0, 103.0, 100.5, 103.5, 101.5, 102.5, 100.2, 103.8]


def _run(tail):
    close = np.array(INSIDE + tail, dtype=float)
    n = len(close)
    ts = pd.Series(pd.to_datetime(np.arange(n) * 3_600_000, unit="ms", utc=True))
    return simulate_confirmed(0, 9, close=close, low=close - 0.1, high=close + 0.1,
                              ts_col=ts, n=n, cfg=CFG)


def test_cluster_band_takes_the_outer_quarters():
    rll, rlu, rhl, rhu = _cluster_band(INSIDE, 0.25)
    assert (rll, rhu) == (100.0, 104.0)
    assert rll <= rlu < rhl <= rhu


def test_break_that_returns_inside_the_window_is_a_failed_break():
    _hist, failed, end_idx, reason, _band, _win = _run([106.0, 106.5, 102.0, 101.0, 103.0])
    assert reason is None and end_idx == 14                 # still active at the last bar
    assert len(failed) == 1
    break_idx, direction, _close, bars_to_recover = failed[0]
    assert (break_idx, direction, bars_to_recover) == (10, "up", 2)


def test_break_that_stays_out_for_the_whole_window_ends_the_range():
    _h, failed, end_idx, reason, _b, _w = _run([106.0, 106.5, 107.0, 107.5, 108.0, 108.5])
    assert (end_idx, reason, failed) == (10, "breakout_up", [])


def test_breakdown_direction():
    _h, _f, end_idx, reason, _b, _w = _run([98.0, 97.5, 97.0, 96.5, 96.0])
    assert (end_idx, reason) == (10, "breakdown_down")


def test_return_on_the_last_bar_of_the_window_still_counts():
    # window = bars 11..14 (4 bars); recovery on bar 14 is inside it
    _h, failed, _e, reason, _b, _w = _run([106.0, 106.5, 107.0, 106.8, 103.0])
    assert reason is None and failed[0][3] == 4


def test_KNOWN_GAP_an_end_at_the_newest_bar_is_declared_before_it_is_knowable():
    """Audit hole 3 — pinned on purpose, not endorsed.

    The break is the LAST bar available: the recovery window is empty, so the
    engine reports the range as ended immediately. One more bar back inside
    would turn the same break into a failed break (next test). Offline this is
    harmless — consumers gate on the knowable time. LIVE it is not: the newest
    bar is always the data edge, so every edge break first shows up as a final
    end. The planned fix (plan item 0.5) flags such ends as provisional in
    compute_known_at; the engine itself stays as it is."""
    _h, _f, end_idx, reason, _b, _w = _run([106.0])
    assert (end_idx, reason) == (10, "breakout_up")


def test_the_same_break_is_withdrawn_once_price_returns():
    _h, failed, _e, reason, _b, _w = _run([106.0, 102.0])
    assert reason is None and len(failed) == 1
