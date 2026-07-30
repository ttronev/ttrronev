"""
detectors/paths.py — single source of truth for pair-scoped artifact paths.

Pair format is the full CSV prefix, e.g. "SOL_USDT" (NOT bare "SOL").
All detector artifacts live under detectors/results/{PAIR}/:

    detectors/results/SOL_USDT/range_detector_1d_layer1.json
    detectors/results/SOL_USDT/range_memory_1d.json
    detectors/results/SOL_USDT/alerts_state.json
    detectors/results/SOL_USDT/state.json        (service)
    detectors/results/SOL_USDT/live.json         (service)
    detectors/results/SOL_USDT/heartbeat.json    (service)

Raw candles stay flat: data/raw/{PAIR}_{tf}.csv (unchanged naming).

Adding a pair requires NO path edits anywhere — every consumer resolves
through these helpers. See service/pairs.py for the pair roster.
"""
from __future__ import annotations
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PAIR = "SOL_USDT"


def base_symbol(pair: str = DEFAULT_PAIR) -> str:
    """'SOL_USDT' -> 'SOL' (the okx_fetch / legacy freshness convention)."""
    return pair.split("_")[0]


def okx_inst_id(pair: str = DEFAULT_PAIR) -> str:
    """'SOL_USDT' -> 'SOL-USDT-SWAP' (linear perpetual, matches the CSVs)."""
    return pair.replace("_", "-") + "-SWAP"


def results_dir(pair: str = DEFAULT_PAIR) -> Path:
    return ROOT / "detectors" / "results" / pair


def raw_csv(tf: str, pair: str = DEFAULT_PAIR) -> Path:
    return ROOT / "data" / "raw" / f"{pair}_{tf}.csv"


def l1_json(tf: str, pair: str = DEFAULT_PAIR) -> Path:
    return results_dir(pair) / f"range_detector_{tf}_layer1.json"


def mem_json(tf: str, pair: str = DEFAULT_PAIR) -> Path:
    return results_dir(pair) / f"range_memory_{tf}.json"


def alerts_state(pair: str = DEFAULT_PAIR) -> Path:
    return results_dir(pair) / "alerts_state.json"


def state_json(pair: str = DEFAULT_PAIR) -> Path:
    return results_dir(pair) / "state.json"


def live_json(pair: str = DEFAULT_PAIR) -> Path:
    return results_dir(pair) / "live.json"


def heartbeat_json(pair: str = DEFAULT_PAIR) -> Path:
    return results_dir(pair) / "heartbeat.json"


def ensure_results_dir(pair: str = DEFAULT_PAIR) -> Path:
    d = results_dir(pair)
    d.mkdir(parents=True, exist_ok=True)
    return d
