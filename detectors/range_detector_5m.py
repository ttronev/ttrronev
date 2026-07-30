"""
detectors/range_detector_5m.py — 5m wrapper around range_detector_core. DRAFT.

Fastest TF, BELOW the existing cascade (1W -> 1D -> 4H -> 2H -> 1H -> 5m). 5m
sub-ranges are expected to nest inside 1H ranges. WICK-BASED engine (like
1H/4H): at 5m, intra-bar sweeps / stop hunts are the dominant microstructure and
must shape the bands, not be filtered as noise. use_close_based_extremes=False.

STATUS: drafted as Task-6 pre-work. NOT YET RUN / NOT CALIBRATED. Requires
data/raw/SOL_USDT_5m.csv (run `python -m data.bootstrap_5m` first). The params
below are STARTING points from the build spec — calibrate against raw 5m candles
before locking, exactly like every other TF (see feedback-build-discipline).

Starting config (spec):
    analyzer_reversal_pct=0.005   # 0.5% swings (5m is fast/small)
    analyzer_init_bars=200        # ~16.7h warm-up before the ratchet analyzer
    retrace 0.40-0.65
    band_zone_pct=0.25            # looser bands, like 1H/4H
    recovery_lookahead=36         # 36 * 5m = ~3h false-break tolerance / hold
    max_pending_bars=144          # ~12h pending-range timeout
    min_pending_closes=5          # >=5 closes before a confirm

CALIBRATION WATCH-OUTS (carried from 1H, which is the closest analogue):
  * Low reversal_pct (0.005) at a fast TF tends to FLOOD with degenerate short
    consolidations. On 1H, reversal_pct/recovery_lookahead could NOT separate
    good sub-ranges from degenerate ones (same axis) — the fix was raising
    min_pending_closes (3 -> 10). Expect to bump min_pending_closes here too;
    the __main__ summary prints the degenerate(<$1) band count to watch it.
  * recovery_lookahead too large makes sub-ranges span the parent TF's scale
    (no added resolution). 36 (~3h) keeps 5m ranges minutes-to-hours.

POST-BOOTSTRAP INTEGRATION (do AFTER 5m data exists + this is calibrated, and
AFTER alerts are confirmed live — these are the 6-TF cascade wiring, NOT part of
this draft):
  1. freshness_monitor: register "5m" in STALE_HOURS (~0.5h) + TFS so it
     auto-extends (TF_TO_OKX/TF_TO_MS already have 5m).
  2. cascade_nesting: add 5m as the 6th TF (child of 1H).
  3. compute_cleanness / compute_known_at: run for 5m.
  4. layer5_range_memory + layer5_1_strength: extend to 5m.
  5. alerts.py + query_state.py: add "5m" to TFS for 5m-level coverage.
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

DATA_5M = paths.raw_csv("5m")            # default-pair convenience (legacy constant)


def config(retrace_low=0.40, retrace_high=0.65,
           min_pending_closes=5, reversal_pct=0.005) -> CoreRangeConfig:
    return CoreRangeConfig(
        timeframe="5m",
        analyzer_reversal_pct=reversal_pct, analyzer_init_bars=200,
        retrace_low=retrace_low, retrace_high=retrace_high,
        band_zone_pct=0.25,
        recovery_lookahead=36,          # 36 * 5m = ~3h hold / false-break tolerance
        max_pending_bars=144,           # ~12h
        min_pending_closes=min_pending_closes,   # calibration lever (CLI --min-pending-closes)
        # Wick params not in the spec; scaled from 1H (wick_lookback 48->144 ~12h,
        # accepted_lookahead == recovery_lookahead as on 1H). Calibrate.
        wick_multiplier=2.0, wick_lookback_bars=144, accepted_lookahead=36,
        use_close_based_extremes=False,  # 5m keeps the wick-based engine
    )


def run(data_5m=None, out_json=None,
        retrace_low=0.40, retrace_high=0.65,
        min_pending_closes=5, reversal_pct=0.005, pair=None):
    pair = pair or paths.DEFAULT_PAIR
    data_5m = data_5m or paths.raw_csv("5m", pair)
    out_json = out_json or paths.ensure_results_dir(pair) / "range_detector_5m_layer1.json"
    if not Path(data_5m).exists():
        raise FileNotFoundError(
            f"{data_5m} not found. Bootstrap it first: "
            f"python -m data.bootstrap_5m")
    df = pd.read_csv(data_5m).drop_duplicates("timestamp").sort_values("timestamp")
    print(f"[range_detector_5m] {len(df):,} bars loaded; detecting (wick-based, full history, "
          f"min_pending_closes={min_pending_closes}) — allow a few minutes...", flush=True)
    df_bias = df                         # detection TF and bias TF are both 5m (like 1H)
    output, ranges = detect_ranges(
        df, df_bias, config(retrace_low, retrace_high, min_pending_closes, reversal_pct))
    print(f"[range_detector_5m] detection complete: {len(ranges)} range records.", flush=True)
    save_json(output, Path(out_json))
    return output, ranges


if __name__ == "__main__":
    from shared.memhygiene import install        # Memory hygiene: see MEMORY_HYGIENE.md
    install("range_detector_5m")
    p = argparse.ArgumentParser()
    p.add_argument("--pair", default=None)
    p.add_argument("--data-5m", default=None)
    p.add_argument("--out-json", default=None)
    p.add_argument("--retrace-low", type=float, default=0.40)
    p.add_argument("--retrace-high", type=float, default=0.65)
    p.add_argument("--min-pending-closes", type=int, default=5, dest="min_pending_closes",
                   help="closes required before a confirm — the chop filter (calibration lever).")
    p.add_argument("--reversal-pct", type=float, default=0.005, dest="reversal_pct",
                   help="analyzer swing threshold (keep fixed while isolating min-pending-closes).")
    a = p.parse_args()
    # NOTE: no freshness hook yet — 5m is not registered in freshness_monitor
    # (would KeyError on STALE_HOURS["5m"]). Bootstrap/extend 5m manually until
    # the POST-BOOTSTRAP INTEGRATION step 1 is done.
    out, ranges = run(a.data_5m, a.out_json, a.retrace_low, a.retrace_high,
                      a.min_pending_closes, a.reversal_pct, pair=a.pair)
    print(f"[range_detector_5m] retrace={a.retrace_low}-{a.retrace_high}  "
          f"{out['n_ranges_total']} records, confirmed={out['n_confirmed']} "
          f"timeout={out['n_phase_1_timeout']} failed={out['n_phase_1_failed']} "
          f"-> {a.out_json or paths.l1_json('5m', a.pair or paths.DEFAULT_PAIR)}")
    conf = [r for r in ranges if r.is_confirmed]
    import numpy as np
    if conf:
        hw = np.array([r.range_high_upper - r.range_high_lower for r in conf])
        lw = np.array([r.range_low_upper - r.range_low_lower for r in conf])
        print(f"  band widths: high median=${np.median(hw):.2f} low median=${np.median(lw):.2f}  "
              f"degenerate(<$1)={int(((hw<1)|(lw<1)).sum())}")
