"""
detectors/range_detector_1h.py — 1H wrapper around range_detector_core.

Lowest TF of the upper cascade (1W -> 1D -> 4H -> 1H). WICK-BASED engine
(same as 4H): 1H sits closer to the trading TF than the structural TFs,
so intra-bar exploration (liquidity sweeps, stop hunts) is meaningful
microstructure and should shape the bands — not be filtered as noise.

1H sub-ranges are expected to nest inside the 4H ranges already approved.

Starting config (hourly = smaller swings, deeper retraces, longer holds
in bar count):
    analyzer_reversal_pct=0.015, init=48, retrace >= 0.40 (arming threshold),
    band_zone=0.25 (looser, like 4H), recovery_lookahead=24 (=1 day),
    max_pending_bars=48 (=2 days).
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


RETRACE_LOW = 0.40              # single source for config() / run() / CLI


def config(retrace_low=RETRACE_LOW) -> CoreRangeConfig:
    return CoreRangeConfig(
        timeframe="1h",
        analyzer_reversal_pct=0.015, analyzer_init_bars=48,
        retrace_low=retrace_low,
        band_zone_pct=0.25,
        # Calibrated for fine sub-range nesting inside 4H ranges:
        #  * recovery_lookahead 24 made 1H ranges span weeks (≈ 4H scale,
        #    no added resolution). 12 lets them last hours-to-days.
        #  * min_pending_closes 3 -> 10: 1H at reversal_pct=0.015 floods
        #    with degenerate short consolidations (45% degenerate). Neither
        #    recovery_lookahead nor reversal_pct could separate good sub-
        #    ranges from degenerate ones (same axis). Requiring >=10 closes
        #    before a confirm cuts degeneracy 45%->22% while holding the
        #    Feb-May 2026 nesting at 13 and count at 357.
        recovery_lookahead=12,          # 12 hourly bars = ~12h hold
        max_pending_bars=48,            # ~2 days
        min_pending_closes=10,
        wick_multiplier=2.0, wick_lookback_bars=48, accepted_lookahead=12,
        use_close_based_extremes=False,  # 1H keeps the wick-based engine (like 4H)
    )


def run(data_1h=None, out_json=None,
        retrace_low=RETRACE_LOW, pair=None):
    pair = pair or paths.DEFAULT_PAIR
    data_1h = data_1h or paths.raw_csv("1h", pair)
    out_json = out_json or paths.ensure_results_dir(pair) / "range_detector_1h_layer1.json"
    df = pd.read_csv(data_1h).drop_duplicates("timestamp").sort_values("timestamp")
    # detection TF and bias TF are both 1H here.
    df_bias = df
    output, ranges = detect_ranges(df, df_bias, config(retrace_low))
    save_json(output, Path(out_json))
    return output, ranges


if __name__ == "__main__":
    from shared.memhygiene import install        # Memory hygiene: see MEMORY_HYGIENE.md
    install("range_detector_1h")
    p = argparse.ArgumentParser()
    p.add_argument("--pair", default=None)
    p.add_argument("--data-1h", default=None)
    p.add_argument("--out-json", default=None)
    p.add_argument("--retrace-low", type=float, default=RETRACE_LOW)
    p.add_argument("--no-freshness", action="store_true", help="Skip pre-run data freshness check.")
    a = p.parse_args()
    if not a.no_freshness:
        try:
            from data.freshness_monitor import check_and_update
            check_and_update(pair=a.pair or paths.DEFAULT_PAIR)
        except Exception as e:
            print(f"[freshness] skipped ({e})")
    out, ranges = run(a.data_1h, a.out_json, a.retrace_low, pair=a.pair)
    print(f"[range_detector_1h] retrace>={a.retrace_low}  "
          f"{out['n_ranges_total']} records, confirmed={out['n_confirmed']} "
          f"timeout={out['n_phase_1_timeout']} failed={out['n_phase_1_failed']} "
          f"-> {a.out_json or paths.l1_json('1h', a.pair or paths.DEFAULT_PAIR)}")
    conf = [r for r in ranges if r.is_confirmed]
    import numpy as np
    if conf:
        hw = np.array([r.range_high_upper - r.range_high_lower for r in conf])
        lw = np.array([r.range_low_upper - r.range_low_lower for r in conf])
        print(f"  band widths: high median=${np.median(hw):.1f} low median=${np.median(lw):.1f}  "
              f"degenerate(<$1)={int(((hw<1)|(lw<1)).sum())}")
