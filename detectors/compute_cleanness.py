"""
detectors/compute_cleanness.py — populate cleanness_metrics on every range
record. UTILITY ONLY: adds fields to existing records, no detection logic.

Six metrics per range (computed over the active life [confirm, end] on the
detection-TF bars):
    n_distinct_high_touches  distinct bars whose wick overlapped the
                             range_high band (>=3 bars apart, matching the
                             detector's touch rule)
    n_distinct_low_touches   same for range_low
    failed_break_count       len(failed_break_events)
    active_duration_bars     end_idx - confirm_idx
    time_inside_band_pct     % of closes in [range_low_lower,
                             range_high_upper] over the active life
    n_bos_absorbed           len(bos_inside_range)

Pending-only records (timeout / phase_1_failed) have no active phase -> all
six set to 0. NO classification here (no strong/weak/degenerate labels);
just the numbers.
"""
from __future__ import annotations
import json, sys
from pathlib import Path
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from detectors import paths
from shared.ioutil import atomic_write_json      # never leave a truncated artifact

TFS = ["1w", "1d", "4h", "2h", "1h"]


def _zero_metrics():
    return {
        "n_distinct_high_touches": 0, "n_distinct_low_touches": 0,
        "failed_break_count": 0, "active_duration_bars": 0,
        "time_inside_band_pct": 0.0, "n_bos_absorbed": 0,
    }


def compute_for_tf(tf: str, pair: str = paths.DEFAULT_PAIR) -> dict:
    out = json.loads(paths.l1_json(tf, pair).read_text())
    df = pd.read_csv(paths.raw_csv(tf, pair)).drop_duplicates("timestamp").sort_values("timestamp").reset_index(drop=True)
    tsv = pd.to_datetime(df["timestamp"], unit="ms", utc=True).values.astype("datetime64[ns]")
    high = df["high"].to_numpy(float); low = df["low"].to_numpy(float); close = df["close"].to_numpy(float)
    n = len(df)

    def idx_of(ts_iso):
        return int(np.searchsorted(tsv, np.datetime64(pd.Timestamp(ts_iso).tz_localize(None))))

    def touches(lo_i, hi_i, lo_edge, hi_edge):
        cnt = 0; last = -10
        for j in range(lo_i, hi_i + 1):
            if low[j] <= hi_edge and high[j] >= lo_edge:
                if j - last >= 3:
                    cnt += 1; last = j
        return cnt

    n_conf = 0
    for r in out["ranges"]:
        if not r["is_confirmed"]:
            r["cleanness_metrics"] = _zero_metrics()
            continue
        n_conf += 1
        ci = idx_of(r["range_phase_2_ts"])
        ei = idx_of(r["range_end_ts"]) if r["range_end_ts"] else n - 1
        ei = min(ei, n - 1)
        rll, rlu = r["range_low_lower"], r["range_low_upper"]
        rhl, rhu = r["range_high_lower"], r["range_high_upper"]
        closes = close[ci:ei + 1]
        r["cleanness_metrics"] = {
            "n_distinct_high_touches": touches(ci, ei, rhl, rhu),
            "n_distinct_low_touches": touches(ci, ei, rll, rlu),
            "failed_break_count": len(r["failed_break_events"]),
            "active_duration_bars": int(ei - ci),
            "time_inside_band_pct": round(float(100 * np.mean((closes >= rll) & (closes <= rhu))), 1) if len(closes) else 0.0,
            "n_bos_absorbed": len(r["bos_inside_range"]),
        }
    atomic_write_json(paths.l1_json(tf, pair), out)
    return {"tf": tf, "n_ranges": len(out["ranges"]), "n_confirmed": n_conf}


if __name__ == "__main__":
    from shared.memhygiene import install        # Memory hygiene: see MEMORY_HYGIENE.md
    install("compute_cleanness")
    from data.freshness_monitor import consumer_startup   # Freshness contract: see MEMORY_HYGIENE.md
    consumer_startup(end_consumer=False)                   # CSV refresh only (regen-chain step; --no-freshness to skip)
    for tf in TFS:
        s = compute_for_tf(tf)
        print(f"[cleanness] {s['tf']}: populated {s['n_ranges']} records ({s['n_confirmed']} confirmed)")
