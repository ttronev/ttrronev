"""
detectors/range_detector_4h.py — 4H wrapper around range_detector_core.

Calibrated 4H config (matches the approved v3.1 build):
    analyzer_reversal_pct=0.035, retrace >= 0.725 (arming threshold),
    recovery=12, max_pending_bars=24 (=96h), min_pending_closes=3.
"""
from __future__ import annotations
import argparse, sys
from pathlib import Path
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from detectors.range_detector_core import CoreRangeConfig, detect_ranges, save_json
from detectors import paths


def config() -> CoreRangeConfig:
    return CoreRangeConfig(
        timeframe="4h",
        analyzer_reversal_pct=0.035, analyzer_init_bars=20,
        retrace_low=0.725,
        band_zone_pct=0.25,
        recovery_lookahead=12,
        max_pending_bars=24,            # 24 4H bars = 96h
        min_pending_closes=3,
        wick_multiplier=2.0, wick_lookback_bars=20, accepted_lookahead=6,
        use_close_based_extremes=False,  # 4H keeps the approved wick-based engine
    )


def run(data_4h=None, data_1h=None, out_json=None, pair=None):
    pair = pair or paths.DEFAULT_PAIR
    data_4h = data_4h or paths.raw_csv("4h", pair)
    data_1h = data_1h or paths.raw_csv("1h", pair)
    out_json = out_json or paths.ensure_results_dir(pair) / "range_detector_4h_layer1.json"
    df = pd.read_csv(data_4h)
    df_bias = pd.read_csv(data_1h).drop_duplicates("timestamp").sort_values("timestamp")
    output, ranges = detect_ranges(df, df_bias, config())
    save_json(output, Path(out_json))
    return output, ranges


if __name__ == "__main__":
    from shared.memhygiene import install        # Memory hygiene: see MEMORY_HYGIENE.md
    install("range_detector_4h")
    p = argparse.ArgumentParser()
    p.add_argument("--pair", default=None)
    p.add_argument("--data-4h", default=None)
    p.add_argument("--data-1h", default=None)
    p.add_argument("--out-json", default=None)
    p.add_argument("--no-freshness", action="store_true", help="Skip pre-run data freshness check.")
    a = p.parse_args()
    if not a.no_freshness:
        try:
            from data.freshness_monitor import check_and_update
            check_and_update(pair=a.pair or paths.DEFAULT_PAIR)
        except Exception as e:
            print(f"[freshness] skipped ({e})")
    out, ranges = run(a.data_4h, a.data_1h, a.out_json, pair=a.pair)
    print(f"[range_detector_4h] {out['n_ranges_total']} records, "
          f"confirmed={out['n_confirmed']} timeout={out['n_phase_1_timeout']} "
          f"failed={out['n_phase_1_failed']} "
          f"-> {a.out_json or paths.l1_json('4h', a.pair or paths.DEFAULT_PAIR)}")
