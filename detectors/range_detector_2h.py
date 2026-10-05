"""
detectors/range_detector_2h.py — 2H wrapper around range_detector_core.

Sits between 4H (trading TF) and 1H (entry TF) in the cascade
1W -> 1D -> 4H -> 2H -> 1H. Wick-based engine (microstructure matters at
this scale, same as 4H/1H). Intermediate parameters.

Config (between 4H and 1H):
    analyzer_reversal_pct=0.020, init=36, retrace >= 0.30 (arming threshold),
    band_zone=0.25, recovery_lookahead=16 (~32h), max_pending_bars=36
    (~3 days), min_pending_closes=6.
min_pending_closes may be raised if degeneracy > 25% (same lever as 1H).

MIN_PENDING_CLOSES is the ONE default for config(), run() and the CLI. They
used to disagree (config/CLI 10, run 6): the service and every saved artifact
ran 6 while replay_validate — which builds its config from config() — audited
10. 6 is what production has always run, so 6 is the value.
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


MIN_PENDING_CLOSES = 6          # single source for config() / run() / CLI


def config(min_pending_closes=MIN_PENDING_CLOSES) -> CoreRangeConfig:
    return CoreRangeConfig(
        timeframe="2h",
        analyzer_reversal_pct=0.020, analyzer_init_bars=36,
        retrace_low=0.30,
        band_zone_pct=0.25,
        recovery_lookahead=16,          # 16 x 2H = ~32h
        max_pending_bars=36,            # ~3 days
        min_pending_closes=min_pending_closes,
        wick_multiplier=2.0, wick_lookback_bars=36, accepted_lookahead=8,
        use_close_based_extremes=False,  # wick-based (like 4H/1H)
    )


def run(data_2h=None, data_1h=None, out_json=None,
        min_pending_closes=MIN_PENDING_CLOSES, pair=None):
    pair = pair or paths.DEFAULT_PAIR
    data_2h = data_2h or paths.raw_csv("2h", pair)
    data_1h = data_1h or paths.raw_csv("1h", pair)
    out_json = out_json or paths.ensure_results_dir(pair) / "range_detector_2h_layer1.json"
    df = pd.read_csv(data_2h).drop_duplicates("timestamp").sort_values("timestamp")
    df_bias = pd.read_csv(data_1h).drop_duplicates("timestamp").sort_values("timestamp")
    output, ranges = detect_ranges(df, df_bias, config(min_pending_closes))
    save_json(output, Path(out_json))
    return output, ranges


if __name__ == "__main__":
    from shared.memhygiene import install        # Memory hygiene: see MEMORY_HYGIENE.md
    install("range_detector_2h")
    import numpy as np
    p = argparse.ArgumentParser()
    p.add_argument("--pair", default=None)
    p.add_argument("--data-2h", default=None)
    p.add_argument("--data-1h", default=None)
    p.add_argument("--out-json", default=None)
    p.add_argument("--min-pending-closes", type=int, default=MIN_PENDING_CLOSES)
    p.add_argument("--no-freshness", action="store_true", help="Skip pre-run data freshness check.")
    a = p.parse_args()
    if not a.no_freshness:
        try:
            from data.freshness_monitor import check_and_update
            check_and_update(pair=a.pair or paths.DEFAULT_PAIR)
        except Exception as e:
            print(f"[freshness] skipped ({e})")
    out, ranges = run(a.data_2h, a.data_1h, a.out_json, a.min_pending_closes, pair=a.pair)
    print(f"[range_detector_2h] minPC={a.min_pending_closes}  {out['n_ranges_total']} records, "
          f"confirmed={out['n_confirmed']} timeout={out['n_phase_1_timeout']} failed={out['n_phase_1_failed']}")
    conf = [r for r in ranges if r.is_confirmed]
    if conf:
        def wp(r):
            mid=(r.range_high_upper+r.range_low_lower)/2
            return (r.range_high_upper-r.range_high_lower)/mid,(r.range_low_upper-r.range_low_lower)/mid
        hpc=np.array([wp(r)[0] for r in conf])
        degen=100*np.mean([(wp(r)[0]<0.003 or wp(r)[1]<0.003) for r in conf])
        print(f"  median band%={np.median(hpc):.2%}  degeneracy={degen:.0f}%")
