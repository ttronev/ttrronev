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

SANDBOX ROOTS (isolation between the live service and research/experiments).
Two environment variables redirect every path below, read at CALL time:

    TTRRONEV_RESULTS_ROOT   replaces <repo>/detectors/results
    TTRRONEV_DATA_ROOT      replaces <repo>/data/raw

The live worker and research scripts used to share detectors/results/ and
overwrite each other's files ("whichever ran last wins"). Any run that is NOT
the live service — a calibration pass, an experiment, a test — must set
TTRRONEV_RESULTS_ROOT to its own folder. When it is set, the freshness hook
skips the OKX fetch and the cascade regen (see freshness_monitor), so a
sandboxed run never writes candles or live artifacts.
"""
from __future__ import annotations
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PAIR = "SOL_USDT"

ENV_RESULTS_ROOT = "TTRRONEV_RESULTS_ROOT"
ENV_DATA_ROOT = "TTRRONEV_DATA_ROOT"


def base_symbol(pair: str = DEFAULT_PAIR) -> str:
    """'SOL_USDT' -> 'SOL' (the okx_fetch / legacy freshness convention)."""
    return pair.split("_")[0]


def okx_inst_id(pair: str = DEFAULT_PAIR) -> str:
    """'SOL_USDT' -> 'SOL-USDT-SWAP' (linear perpetual, matches the CSVs)."""
    return pair.replace("_", "-") + "-SWAP"


def results_root() -> Path:
    """Folder holding every pair's artifact dir plus the service-wide files
    (pairs.json, worker_heartbeat.json, backfill_state.json)."""
    v = os.environ.get(ENV_RESULTS_ROOT)
    return Path(v).expanduser().resolve() if v else ROOT / "detectors" / "results"


def data_root() -> Path:
    """Folder holding the raw candle CSVs ({PAIR}_{tf}.csv)."""
    v = os.environ.get(ENV_DATA_ROOT)
    return Path(v).expanduser().resolve() if v else ROOT / "data" / "raw"


def is_sandboxed() -> bool:
    """True when artifacts are redirected away from the live results folder."""
    return bool(os.environ.get(ENV_RESULTS_ROOT))


def registry_json() -> Path:
    return results_root() / "pairs.json"


def worker_heartbeat_json() -> Path:
    return results_root() / "worker_heartbeat.json"


def backfill_state_json() -> Path:
    return results_root() / "backfill_state.json"


def results_dir(pair: str = DEFAULT_PAIR) -> Path:
    return results_root() / pair


def raw_csv(tf: str, pair: str = DEFAULT_PAIR) -> Path:
    return data_root() / f"{pair}_{tf}.csv"


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

def worker_log() -> Path:
    """The worker's own log file (a mirror of its stdout), the only file
    /api/v1/logs/tail serves."""
    return results_root() / "worker.log"


def stages_json() -> Path:
    """docs/plan/stages.json: the stage map as data (a repo file, not an
    artifact; served by /api/v1/build/stages)."""
    return ROOT / "docs" / "plan" / "stages.json"
