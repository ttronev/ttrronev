"""
detectors/compute_known_at.py — annotate every confirmed range with the bar at
which its confirmation / end becomes forward-only KNOWABLE. UTILITY ONLY: adds
fields, never changes detection (structural confirm_ts / end_ts / bands are left
byte-identical, so all regression anchors hold).

Why: the replay audit showed batch "knows" a range's confirm/end earlier than a
forward-only run can — because the recovery_lookahead window looks forward to
rule out a failed break. That lag is the window's inherent latency, not a bug.
Rather than re-timestamp the detector (which would re-baseline everything), we
publish the knowable bar so Layer 4 / strategy logic can avoid the look-ahead.

  range_end_known_ts        = ts[end_idx + recovery_lookahead]   (EXACT — the
                              non-recovery is only confirmed once the full
                              window passes; None for still-active ranges)
  range_confirmed_known_ts  = ts[confirm_idx + recovery_lookahead]   (UPPER
                              BOUND — a pre-confirm forming-break resolves within
                              recovery_lookahead of the break, and the break is
                              <= confirm; true lag is usually smaller, e.g. the
                              1D smoke slice saw 1 and 6 vs the 18 bound)
  known_at_lag_bars         = recovery_lookahead used (from the run's config)

Levels (layer 5) are born at a range's END, so the field Layer 4 needs —
"a level is knowable at source_end + recovery_lookahead" — is the EXACT
range_end_known_ts. Run AFTER detection, BEFORE layer5_range_memory.
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


def compute_for_tf(tf: str, pair: str = paths.DEFAULT_PAIR) -> dict:
    out = json.loads(paths.l1_json(tf, pair).read_text())
    rl = int(out["config"]["recovery_lookahead"])
    df = (pd.read_csv(paths.raw_csv(tf, pair)).drop_duplicates("timestamp")
          .sort_values("timestamp").reset_index(drop=True))
    ts_iso = [t.isoformat() for t in pd.to_datetime(df["timestamp"], unit="ms", utc=True)]
    idx_of = {t: i for i, t in enumerate(ts_iso)}
    n = len(ts_iso)

    n_conf = 0
    for r in out["ranges"]:
        if not r["is_confirmed"]:
            r["range_confirmed_known_ts"] = None
            r["range_end_known_ts"] = None
            r["known_at_lag_bars"] = rl
            continue
        n_conf += 1
        ci = idx_of.get(r["range_phase_2_ts"])
        r["range_confirmed_known_ts"] = (ts_iso[min(ci + rl, n - 1)] if ci is not None else None)
        if r["range_end_ts"]:
            ei = idx_of.get(r["range_end_ts"])
            r["range_end_known_ts"] = (ts_iso[min(ei + rl, n - 1)] if ei is not None else None)
        else:
            r["range_end_known_ts"] = None      # still active — end not knowable yet
        r["known_at_lag_bars"] = rl
    atomic_write_json(paths.l1_json(tf, pair), out)
    return {"tf": tf, "rl": rl, "n_confirmed": n_conf}


if __name__ == "__main__":
    from shared.memhygiene import install        # Memory hygiene: see MEMORY_HYGIENE.md
    install("compute_known_at")
    from data.freshness_monitor import consumer_startup   # Freshness contract: see MEMORY_HYGIENE.md
    consumer_startup(end_consumer=False)                   # CSV refresh only (regen-chain step; --no-freshness to skip)
    for tf in TFS:
        s = compute_for_tf(tf)
        print(f"[known_at] {s['tf']}: recovery_lookahead={s['rl']}  "
              f"annotated {s['n_confirmed']} confirmed ranges")
