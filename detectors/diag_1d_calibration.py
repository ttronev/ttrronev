"""
detectors/diag_1d_calibration.py — READ-ONLY layer-1 calibration diagnostic
for the 1D detector. No tuning; reports only.

Sections:
  A. Panel 1 (May-Sep 2024): BOS list w/ locked levels, range records,
     per-ended-range reentry offset + near-miss classification, union
     coverage + close distribution.
  B. Panel 2 (Oct 2024-Apr 2025): band_history range_high_upper sequence
     per range — did it stretch up as new highs printed, or did ranges
     end on the up-move?
  C. Comparison panels 1/2 (2024-05..2025-04) vs panels 4/5
     (2025-10..2026-05): avg range duration, failed_breaks/range,
     near-miss recoveries, range count.

Reentry / near-miss definition: a confirmed range that ends via
breakout/breakdown ends at bar j because no close re-entered [rll,rhu]
within [j+1, j+RL] (RL = recovery_lookahead). The first reentry (if any)
is at offset >= RL+1. "near-miss" = first reentry at offset in
[RL+1, RL+5] (price returned just past the window).
"""
from __future__ import annotations
import json, sys
from pathlib import Path
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from shared.structure_analyzer_close_based import StructureAnalyzer

DF = pd.read_csv(ROOT / "data/raw/SOL_USDT_1d.csv").sort_values("timestamp").reset_index(drop=True)
TS = pd.to_datetime(DF.timestamp, unit="ms", utc=True)
TSV = TS.values.astype("datetime64[ns]")
CLOSE = DF.close.to_numpy(float)
N = len(DF)
OUT = json.loads((ROOT / "detectors/results/SOL_USDT/range_detector_1d_layer1.json").read_text())
RL = int(OUT["config"]["recovery_lookahead"])


def idx_of(tiso):
    return int(np.searchsorted(TSV, np.datetime64(pd.Timestamp(tiso).tz_localize(None))))


def in_win(r, p0, p1):
    return p0 <= pd.Timestamp(r["range_phase_1_ts"]) <= p1


def reentry_offset(r):
    """First bar offset after end_idx where close re-enters [rll,rhu];
    None if never within 30 bars or range didn't end on a break."""
    if not r["is_confirmed"] or r["range_end_reason"] not in ("breakout_up", "breakdown_down"):
        return None
    if not r["range_end_ts"]:
        return None
    ei = idx_of(r["range_end_ts"])
    rll, rhu = r["range_low_lower"], r["range_high_upper"]
    for k in range(ei + 1, min(N, ei + 31)):
        if rll <= CLOSE[k] <= rhu:
            return k - ei
    return None


def panel_stats(ranges, p0, p1):
    conf = [r for r in ranges if r["is_confirmed"] and p0 <= pd.Timestamp(r["range_phase_2_ts"]) <= p1]
    if not conf:
        return dict(n=0)
    durs, fbs, nearmiss, farreentry, truebreak = [], [], 0, 0, 0
    for r in conf:
        ci = idx_of(r["range_phase_2_ts"])
        ei = idx_of(r["range_end_ts"]) if r["range_end_ts"] else N - 1
        durs.append(ei - ci)
        fbs.append(len(r["failed_break_events"]))
        off = reentry_offset(r)
        if off is None:
            truebreak += 1
        elif RL + 1 <= off <= RL + 5:
            nearmiss += 1
        else:
            farreentry += 1
    return dict(n=len(conf), avg_dur=np.mean(durs), avg_fb=np.mean(fbs),
                nearmiss=nearmiss, farreentry=farreentry, truebreak=truebreak)


print(f"1D detector — recovery_lookahead = {RL}  (current config)")
print("=" * 72)

# ---------------- A: Panel 1 May-Sep 2024 ----------------
p0, p1 = pd.Timestamp("2024-05-01", tz="UTC"), pd.Timestamp("2024-09-30", tz="UTC")
print("\n### A. PANEL 1 — May-Sep 2024 ###")
a = StructureAnalyzer(reversal_threshold_pct=0.05, init_bars=20)
states = a.analyze(DF)
print("\nA1. BOS events (close-based) w/ locked levels:")
for i, s in enumerate(states):
    if s.last_bos_idx == i and s.last_event in ("bos_up", "bos_down") and p0 <= TS.iloc[i] <= p1:
        print(f"   {TS.iloc[i].date()} {s.last_event:9s} broken={s.last_bos_level:6.1f} "
              f"close={CLOSE[i]:6.1f}  locked_sH={s.swing_high:6.1f} locked_sL={s.swing_low:6.1f}")

inwin = [r for r in OUT["ranges"] if in_win(r, p0, p1)]
conf = [r for r in inwin if r["is_confirmed"]]
print(f"\nA2. range records: {len(inwin)} total, {len(conf)} confirmed")
covered = set()
for r in inwin:
    if r["is_confirmed"]:
        ci = idx_of(r["range_phase_2_ts"]); ei = idx_of(r["range_end_ts"]) if r["range_end_ts"] else N - 1
        covered.update(range(ci, ei + 1))
        off = reentry_offset(r)
        cls = ("true_break" if off is None else
               f"NEAR-MISS(+{off}, {off-RL} past window)" if RL + 1 <= off <= RL + 5 else
               f"far_reentry(+{off})")
        print(f"   {r['range_id']:<26} confirm={r['range_phase_2_ts'][:10]} "
              f"end={r['range_end_ts'][:10] if r['range_end_ts'] else 'ACTIVE':<10} "
              f"({str(r['range_end_reason']):14s}) high=[{r['range_high_lower']:.0f},{r['range_high_upper']:.0f}] "
              f"low=[{r['range_low_lower']:.0f},{r['range_low_upper']:.0f}]  reentry={cls}")
    else:
        print(f"   {r['range_id']:<26} {r['phase_1_outcome']}")

i0, i1 = idx_of("2024-05-01"), idx_of("2024-09-30")
tot = i1 - i0 + 1
incov = len([b for b in covered if i0 <= b <= i1])
seg = CLOSE[i0:i1 + 1]
print(f"\nA3. coverage: {tot} period bars, {incov} inside an active range ({100*incov/tot:.0f}%)")
print(f"    close range {seg.min():.0f}-{seg.max():.0f}; "
      f"% closes in [130,180]={np.mean((seg>=130)&(seg<=180)):.0%}, "
      f"[140,180]={np.mean((seg>=140)&(seg<=180)):.0%}")

# ---------------- B: Panel 2 Oct 2024-Apr 2025 ----------------
p0, p1 = pd.Timestamp("2024-10-01", tz="UTC"), pd.Timestamp("2025-04-30", tz="UTC")
print("\n### B. PANEL 2 — Oct 2024-Apr 2025 (range_high stretch) ###")
i0, i1 = idx_of("2024-10-01"), idx_of("2025-04-30")
print(f"    period close max = {CLOSE[i0:i1+1].max():.0f}  (multi-touch top user sees ~250-275)")
conf2 = [r for r in OUT["ranges"] if r["is_confirmed"] and p0 <= pd.Timestamp(r["range_phase_2_ts"]) <= p1]
for r in conf2:
    seq = []
    for h in r["band_history"]:
        v = round(h["rh_u"], 1)
        if not seq or seq[-1][1] != v:
            seq.append((h["ts"][:10], v))
    stretched = seq[-1][1] - seq[0][1]
    line = f"   {r['range_id']:<26} confirm={r['range_phase_2_ts'][:10]} end={r['range_end_ts'][:10] if r['range_end_ts'] else 'ACTIVE'} ({r['range_end_reason']})"
    print(line)
    print(f"      range_high_upper: {seq[0][1]} -> {seq[-1][1]}  (stretched +{stretched:.0f})  steps={len(seq)}")
    if r["range_end_reason"] == "breakout_up" and r["range_end_ts"]:
        ei = idx_of(r["range_end_ts"])
        nxt = [round(CLOSE[ei+k], 0) for k in range(1, 9) if ei + k < N]
        print(f"      ended breakout_up: close={CLOSE[ei]:.0f} vs rhu={r['range_high_upper']:.0f}; next closes {nxt}")

# ---------------- C: comparison ----------------
print("\n### C. COMPARISON — problem panels 1/2 vs clean panels 4/5 ###")
groups = {
    "panels 1/2 (2024-05..2025-04, high-vol)": (pd.Timestamp("2024-05-01", tz="UTC"), pd.Timestamp("2025-04-30", tz="UTC")),
    "panels 4/5 (2025-10..2026-05, low-vol)":  (pd.Timestamp("2025-10-01", tz="UTC"), pd.Timestamp("2026-05-31", tz="UTC")),
}
print(f"   {'group':<42} {'#rng':>5} {'avgDur':>7} {'avgFB':>6} {'nearMiss':>9} {'farRe':>6} {'trueBrk':>8}")
for name, (g0, g1) in groups.items():
    s = panel_stats(OUT["ranges"], g0, g1)
    if s["n"] == 0:
        print(f"   {name:<42} (none)"); continue
    print(f"   {name:<42} {s['n']:>5} {s['avg_dur']:>7.1f} {s['avg_fb']:>6.1f} "
          f"{s['nearmiss']:>9} {s['farreentry']:>6} {s['truebreak']:>8}")
# realized volatility per group (avg abs daily close-to-close %)
for name, (g0, g1) in groups.items():
    a0, a1 = idx_of(g0.tz_localize(None).isoformat()), idx_of(g1.tz_localize(None).isoformat())
    seg = CLOSE[a0:a1+1]
    vol = np.mean(np.abs(np.diff(seg) / seg[:-1]))
    print(f"   realized vol (avg |daily %|) — {name}: {vol:.2%}")
