"""
detectors/range_detector_1d.py — 1D wrapper around range_detector_core.

1D config (per cascade spec). Shallow retraces on daily; bigger BOS swings.
Retrace zone starts 0.20-0.30; if the Feb-May 2026 consolidation doesn't
fire, the runner can sweep down (0.15-0.30, then 0.10-0.35) via --retrace.
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


def config(retrace_low=0.20, retrace_high=0.30) -> CoreRangeConfig:
    return CoreRangeConfig(
        timeframe="1d",
        analyzer_reversal_pct=0.05, analyzer_init_bars=20,
        retrace_low=retrace_low, retrace_high=retrace_high,
        # Tightened 0.25 -> 0.15 for trading precision. Diagnostic showed
        # 0.15 keeps Panel-1 as one range, R014 non-degenerate, Panel-2
        # top still stretches to 262, confirmed count holds at 12.
        band_zone_pct=0.15,
        # Calibrated in two steps:
        #  * 5 (spec start) fragmented the Feb-May 2026 consolidation.
        #  * 10 fixed Feb-May but the full-history diagnostic showed
        #    high-vol periods (May-Sep 2024) still fragment: ranges ended
        #    on out-and-back excursions that re-entered at +14 to +18
        #    bars (near-misses just past the 10-bar window).
        #  * 18 catches those reentries; low-vol panels (Oct'25-May'26)
        #    are unaffected (their ranges already run ~29 bars).
        # Future work: scale recovery_lookahead by realized volatility
        # instead of a fixed value (see detectors/FUTURE_WORK.md).
        recovery_lookahead=18,          # 18 daily bars
        max_pending_bars=20,            # ~3 weeks on daily
        min_pending_closes=3,
        wick_multiplier=2.0, wick_lookback_bars=20, accepted_lookahead=6,
        use_close_based_extremes=True,   # 1D ignores liquidation wicks (Feb-May 2026)
    )


def run(data_1d=None, data_1h=None, out_json=None,
        retrace_low=0.20, retrace_high=0.30, pair=paths.DEFAULT_PAIR):
    data_1d = data_1d or paths.raw_csv("1d", pair)
    data_1h = data_1h or paths.raw_csv("1h", pair)
    out_json = out_json or paths.ensure_results_dir(pair) / "range_detector_1d_layer1.json"
    df = pd.read_csv(data_1d)
    df_bias = pd.read_csv(data_1h).drop_duplicates("timestamp").sort_values("timestamp")
    output, ranges = detect_ranges(df, df_bias, config(retrace_low, retrace_high))
    save_json(output, Path(out_json))
    return output, ranges


if __name__ == "__main__":
    from shared.memhygiene import install        # Memory hygiene: see MEMORY_HYGIENE.md
    install("range_detector_1d")
    p = argparse.ArgumentParser()
    p.add_argument("--pair", default=paths.DEFAULT_PAIR)
    p.add_argument("--data-1d", default=None)
    p.add_argument("--data-1h", default=None)
    p.add_argument("--out-json", default=None)
    p.add_argument("--retrace-low", type=float, default=0.20)
    p.add_argument("--retrace-high", type=float, default=0.30)
    p.add_argument("--no-freshness", action="store_true", help="Skip pre-run data freshness check.")
    a = p.parse_args()
    if not a.no_freshness:
        try:
            from data.freshness_monitor import check_and_update
            check_and_update(pair=a.pair or paths.DEFAULT_PAIR)
        except Exception as e:
            print(f"[freshness] skipped ({e})")
    out, ranges = run(a.data_1d, a.data_1h, a.out_json, a.retrace_low, a.retrace_high,
                      pair=a.pair)
    print(f"[range_detector_1d] retrace={a.retrace_low}-{a.retrace_high}  "
          f"{out['n_ranges_total']} records, confirmed={out['n_confirmed']} "
          f"timeout={out['n_phase_1_timeout']} failed={out['n_phase_1_failed']} "
          f"-> {a.out_json or paths.l1_json('1d', a.pair)}")
