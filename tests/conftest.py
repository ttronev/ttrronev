"""Shared pytest setup.

Every test in tests/ must be FAST and OFFLINE: synthetic candles only, no
network, and NO reads or writes under the live data/raw or detectors/results.
Anything that touches artifacts uses the `sandbox` fixture, which points the
path layer at a temp folder via TTRRONEV_RESULTS_ROOT / TTRRONEV_DATA_ROOT
(see detectors/paths.py). Heavy passes (replays, full-history regens) are not
tests — see docs/runbooks/memory_hygiene.md.
"""
import importlib
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

STRUCT_TFS = ["1w", "1d", "4h", "2h", "1h"]


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """Redirect every artifact + candle path into tmp_path."""
    monkeypatch.setenv("TTRRONEV_RESULTS_ROOT", str(tmp_path / "results"))
    monkeypatch.setenv("TTRRONEV_DATA_ROOT", str(tmp_path / "raw"))
    return tmp_path


def run_chain(pair: str) -> dict:
    """Layer-1 detection for the five structural timeframes, then the derived
    chain in its load-bearing order (cleanness -> nesting -> known_at ->
    range memory -> strength). Paths resolve through the active sandbox."""
    import detectors.compute_cleanness as cc
    import detectors.cascade_nesting as cn
    import detectors.compute_known_at as ck
    import detectors.layer5_range_memory as l5
    import detectors.layer5_1_strength as l51
    l1 = {}
    for tf in STRUCT_TFS:
        mod = importlib.import_module(f"detectors.range_detector_{tf}")
        out, _ranges = mod.run(pair=pair)
        l1[tf] = out
    for tf in STRUCT_TFS:
        cc.compute_for_tf(tf, pair)
    cn.run(verbose=False, pair=pair)
    for tf in STRUCT_TFS:
        ck.compute_for_tf(tf, pair)
    mem = l5.run(verbose=False, pair=pair)
    strength = l51.run(verbose=False, pair=pair)
    return {"l1": l1, "mem": mem, "strength": strength}


@pytest.fixture
def pipeline(sandbox):
    """Factory: write synthetic candles for a pair and run the whole chain."""
    from detectors import paths
    from tests.synth import synth_1h, write_pair_csvs

    def _run(pair="TEST_USDT", scale=1.0, seed=2, cycles=10):
        write_pair_csvs(paths.data_root(), pair, synth_1h(seed=seed, cycles=cycles, scale=scale))
        return run_chain(pair)
    return _run
