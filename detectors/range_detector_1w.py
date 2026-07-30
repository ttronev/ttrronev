"""
detectors/range_detector_1w.py — 1W wrapper around range_detector_core.

Outermost TF of the cascade (1W -> 1D -> 4H -> 1H -> ...). Close-based
engine (like 1D), so liquidation wicks don't set locked extremes. Weekly
ranges are the macro structural zones that contain many 1D ranges.

Starting config (weekly = bigger swings, shallower retraces, longer holds
in calendar time but few bars):
    analyzer_reversal_pct=0.08, init=12, retrace 0.15-0.30,
    band_zone=0.15 (carried from 1D), recovery_lookahead=8 (=2 months),
    max_pending_bars=12 (=3 months).
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


def config(retrace_low=0.15, retrace_high=0.30) -> CoreRangeConfig:
    return CoreRangeConfig(
        timeframe="1w",
        analyzer_reversal_pct=0.08, analyzer_init_bars=12,
        retrace_low=retrace_low, retrace_high=retrace_high,
        band_zone_pct=0.15,
        recovery_lookahead=8,           # 8 weekly bars = ~2 months
        max_pending_bars=12,            # ~3 months
        min_pending_closes=3,
        wick_multiplier=2.0, wick_lookback_bars=12, accepted_lookahead=4,
        use_close_based_extremes=True,  # weekly ignores liquidation wicks
    )


def run(data_1w=None, data_1h=None, out_json=None,
        retrace_low=0.15, retrace_high=0.30, pair=None):
    pair = pair or paths.DEFAULT_PAIR
    data_1w = data_1w or paths.raw_csv("1w", pair)
    data_1h = data_1h or paths.raw_csv("1h", pair)
    out_json = out_json or paths.ensure_results_dir(pair) / "range_detector_1w_layer1.json"
    df = pd.read_csv(data_1w)
    df_bias = pd.read_csv(data_1h).drop_duplicates("timestamp").sort_values("timestamp")
    output, ranges = detect_ranges(df, df_bias, config(retrace_low, retrace_high))
    save_json(output, Path(out_json))
    return output, ranges


if __name__ == "__main__":
    from shared.memhygiene import install        # Memory hygiene: see MEMORY_HYGIENE.md
    install("range_detector_1w")
    p = argparse.ArgumentParser()
    p.add_argument("--pair", default=None)
    p.add_argument("--data-1w", default=None)
    p.add_argument("--data-1h", default=None)
    p.add_argument("--out-json", default=None)
    p.add_argument("--retrace-low", type=float, default=0.15)
    p.add_argument("--retrace-high", type=float, default=0.30)
    p.add_argument("--no-freshness", action="store_true", help="Skip pre-run data freshness check.")
    a = p.parse_args()
    if not a.no_freshness:
        try:
            from data.freshness_monitor import check_and_update
            check_and_update(pair=a.pair or paths.DEFAULT_PAIR)
        except Exception as e:
            print(f"[freshness] skipped ({e})")
    out, ranges = run(a.data_1w, a.data_1h, a.out_json, a.retrace_low, a.retrace_high,
                      pair=a.pair)
    print(f"[range_detector_1w] retrace={a.retrace_low}-{a.retrace_high}  "
          f"{out['n_ranges_total']} records, confirmed={out['n_confirmed']} "
          f"timeout={out['n_phase_1_timeout']} failed={out['n_phase_1_failed']} "
          f"-> {a.out_json or paths.l1_json('1w', a.pair or paths.DEFAULT_PAIR)}")
    conf = [r for r in ranges if r.is_confirmed]
    for r in conf:
        print(f"  {r.range_id}  confirm={r.range_phase_2_ts[:10]} "
              f"end={r.range_end_ts[:10] if r.range_end_ts else 'ACTIVE'} ({r.range_end_reason}) "
              f"low=[{r.range_low_lower:.0f},{r.range_low_upper:.0f}] high=[{r.range_high_lower:.0f},{r.range_high_upper:.0f}]")
