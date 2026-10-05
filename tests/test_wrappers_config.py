"""Per-timeframe detector wrappers: one config per timeframe, however it is
reached, and no silent drift of the locked detection parameters.

Background: range_detector_2h used min_pending_closes=10 from config() and the
CLI but 6 from run(). The service calls run(); replay_validate builds its
config from config(). So the forward-only audit tested a config production
never ran."""
import importlib
import inspect
from dataclasses import asdict, fields

import pytest

from detectors.range_detector_core import CoreRangeConfig

TFS = ["1w", "1d", "4h", "2h", "1h", "5m"]

# LOCKED detection parameters, as run in production. Changing any of these
# changes what the detector finds: update this table in the SAME commit, on
# purpose, with the evidence in the commit message.
LOCKED = {
    #        reversal  init  retrace  zone   recov  maxpend  minclose  wicklb  acc   close-based
    "1w": (0.08,    12,   0.15,   0.15,   8,    12,      3,        12,     4,    True),
    "1d": (0.05,    20,   0.20,   0.15,  18,    20,      3,        20,     6,    True),
    "4h": (0.035,   20,   0.725,  0.25,  12,    24,      3,        20,     6,    False),
    "2h": (0.020,   36,   0.30,   0.25,  16,    36,      6,        36,     8,    False),
    "1h": (0.015,   48,   0.40,   0.25,  12,    48,     10,        48,    12,    False),
    "5m": (0.005,  200,   0.40,   0.25,  36,   144,      5,       144,    36,    False),
}


def _mod(tf):
    return importlib.import_module(f"detectors.range_detector_{tf}")


@pytest.mark.parametrize("tf", TFS)
def test_run_defaults_build_the_same_config_as_config(tf):
    mod = _mod(tf)
    run_params = inspect.signature(mod.run).parameters
    cfg_params = inspect.signature(mod.config).parameters
    from_run = mod.config(**{name: run_params[name].default
                             for name in cfg_params if name in run_params})
    assert asdict(from_run) == asdict(mod.config()), (
        f"{tf}: run() and config() defaults disagree")


@pytest.mark.parametrize("tf", TFS)
def test_locked_parameters(tf):
    c = _mod(tf).config()
    got = (c.analyzer_reversal_pct, c.analyzer_init_bars, c.retrace_low,
           c.band_zone_pct, c.recovery_lookahead, c.max_pending_bars,
           c.min_pending_closes, c.wick_lookback_bars, c.accepted_lookahead,
           c.use_close_based_extremes)
    assert got == LOCKED[tf]
    assert c.timeframe == tf
    assert c.wick_multiplier == 2.0


def test_2h_runs_six_everywhere():
    m = _mod("2h")
    assert m.MIN_PENDING_CLOSES == 6
    assert m.config().min_pending_closes == 6
    assert inspect.signature(m.run).parameters["min_pending_closes"].default == 6


def test_dead_retrace_high_knob_is_gone():
    assert "retrace_high" not in {f.name for f in fields(CoreRangeConfig)}
    for tf in TFS:
        mod = _mod(tf)
        assert "retrace_high" not in inspect.signature(mod.config).parameters
        assert "retrace_high" not in inspect.signature(mod.run).parameters
