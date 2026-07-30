"""
detectors/analyze_5m_layer1.py — read-only layer-1 report for 5m calibration.

Reusable across calibration iterations: point it at any
range_detector_5m_layer1.json to compare param settings (baseline vs
min_pending_closes tweaks). LIGHT — reads only the layer-1 JSON (no candles, no
detection, no heavy compute).

cleanness_metrics is None until compute_cleanness runs (a post-lock cascade
step), so "cleanest" here is a PROXY built from the same raw signals the real
metric uses: distinct touch counts (metadata_current), failed-break-recovered
count, BOS-absorbed count, active duration. Clearly labelled as a proxy.
"""
from __future__ import annotations
import argparse, json
from pathlib import Path
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_JSON = ROOT / "detectors/results/SOL_USDT/range_detector_5m_layer1.json"


def _derive(r):
    """Per-range derived fields from raw layer-1 record (no bars needed)."""
    hl, hu = r["range_high_lower"], r["range_high_upper"]
    ll, lu = r["range_low_lower"], r["range_low_upper"]
    mid = r.get("range_mid") or (hu + ll) / 2
    mc = r.get("metadata_current", {}) or {}
    touches = (mc.get("n_distinct_touches_of_range_low", 0) or 0) + \
              (mc.get("n_distinct_touches_of_range_high", 0) or 0)
    return {
        "id": r["range_id"],
        "confirm_ts": r.get("range_phase_2_ts"),
        "end_ts": r.get("range_end_ts"),
        "end_reason": r.get("range_end_reason"),
        "mid": mid,
        "height_pct": (hu - ll) / mid * 100 if mid else 0.0,       # outer span as % of price
        "hi_band": hu - hl, "lo_band": lu - ll,                    # edge band thickness ($)
        "min_band": min(hu - hl, lu - ll),
        "band_pct": min(hu - hl, lu - ll) / mid * 100 if mid else 0.0,
        "touches": touches,
        "dur_bars": mc.get("time_range_active_bars", 0) or 0,
        "failed_breaks": len(r.get("failed_break_events", []) or []),
        "bos_absorbed": len(r.get("bos_inside_range", []) or []),
        "low_lower": ll, "high_upper": hu,
    }


def main():
    p = argparse.ArgumentParser(description="Read-only 5m layer-1 calibration report.")
    p.add_argument("--json", default=str(DEFAULT_JSON))
    p.add_argument("--demand-low", type=float, default=61.57)
    p.add_argument("--demand-high", type=float, default=65.20)
    p.add_argument("--recent-days", type=int, default=30)
    a = p.parse_args()

    d = json.loads(Path(a.json).read_text())
    rs = d["ranges"]
    conf = [_derive(r) for r in rs if r.get("is_confirmed")]
    last_ts = pd.Timestamp(d["data_last_ts"])
    print(f"=== 5m layer-1 report: {Path(a.json).name} ===")
    print(f"config: min_pending_closes={d['config'].get('min_pending_closes')}  "
          f"reversal_pct={d['config'].get('analyzer_reversal_pct')}  "
          f"recovery_lookahead={d['config'].get('recovery_lookahead')}")
    n_deg = sum(1 for c in conf if c["min_band"] < 1.0)
    med_band = pd.Series([c["min_band"] for c in conf]).median()
    med_band_pct = pd.Series([c["band_pct"] for c in conf]).median()
    med_height_pct = pd.Series([c["height_pct"] for c in conf]).median()
    print(f"total={d['n_ranges_total']}  confirmed={len(conf)}  "
          f"pending(timeout+failed)={d['n_phase_1_timeout']}+{d['n_phase_1_failed']}")
    print(f"degenerate(min edge band <$1)={n_deg} ({n_deg/len(conf)*100:.0f}%)  "
          f"median edge band=${med_band:.2f} ({med_band_pct:.2f}% of price)  "
          f"median range height={med_height_pct:.2f}%")
    print(f"data through {last_ts:%Y-%m-%d %H:%M UTC}\n")

    # 1. confirmed per year (regime check) -------------------------------
    print("--- (1) confirmed ranges per year ---")
    yr = pd.Series([pd.Timestamp(c["confirm_ts"]).year for c in conf]).value_counts().sort_index()
    for y, n in yr.items():
        print(f"   {y}: {n:>4}  {'#'*int(n/yr.max()*40)}")

    # 2. last N days + June 7-8 demand-zone coverage ---------------------
    cutoff = last_ts - pd.Timedelta(days=a.recent_days)
    recent = [c for c in conf if pd.Timestamp(c["confirm_ts"]) >= cutoff]
    print(f"\n--- (2) confirmed in last {a.recent_days} days: {len(recent)} ---")
    # demand-zone overlap: range [low_lower, high_upper] intersects [demand_low, demand_high]
    dz = [c for c in recent if c["high_upper"] >= a.demand_low and c["low_lower"] <= a.demand_high]
    print(f"    overlapping the ${a.demand_low}-${a.demand_high} demand zone: {len(dz)}")
    for c in sorted(recent, key=lambda x: x["confirm_ts"])[-15:]:
        flag = "  <== in demand zone" if c in dz else ""
        act = "ACTIVE" if not c["end_ts"] else (c["end_reason"] or "ended")
        print(f"   {c['confirm_ts'][:16]}  ${c['low_lower']:.2f}-${c['high_upper']:.2f} "
              f"(h={c['height_pct']:.1f}% t={c['touches']} dur={c['dur_bars']}b) {act}{flag}")

    # 3. top 20 by proxy cleanness --------------------------------------
    print(f"\n--- (3) top 20 by PROXY cleanness (touches + failed-breaks + bos + dur/12) ---")
    for c in conf:
        c["proxy"] = c["touches"] + c["failed_breaks"] + c["bos_absorbed"] + c["dur_bars"] / 12.0
    top = sorted(conf, key=lambda x: x["proxy"], reverse=True)[:20]
    print(f"   {'range_id':<26} {'date':<11} {'price':>7} {'height%':>7} {'touch':>5} {'fb':>3} {'bos':>3} {'dur(h)':>6}")
    for c in top:
        print(f"   {c['id']:<26} {c['confirm_ts'][:10]:<11} ${c['mid']:>6.2f} "
              f"{c['height_pct']:>6.1f}% {c['touches']:>5} {c['failed_breaks']:>3} "
              f"{c['bos_absorbed']:>3} {c['dur_bars']/12:>6.1f}")
    meaningful = sum(1 for c in top if c["height_pct"] >= 1.0 and c["touches"] >= 4)
    print(f"   -> {meaningful}/20 look meaningful (height>=1% AND touches>=4) vs chop")

    # 4. band-width % distribution --------------------------------------
    print(f"\n--- (4) range height as % of price (the price-invariant 'real range' signal) ---")
    buckets = [(0, 0.3), (0.3, 0.5), (0.5, 1.0), (1.0, 2.0), (2.0, 5.0), (5.0, 1e9)]
    hp = [c["height_pct"] for c in conf]
    for lo, hi in buckets:
        n = sum(1 for h in hp if lo <= h < hi)
        lbl = f"{lo:.1f}-{hi:.1f}%" if hi < 1e9 else f">{lo:.0f}%"
        print(f"   {lbl:<10} {n:>4} ({n/len(conf)*100:>4.0f}%)  {'#'*int(n/len(conf)*60)}")
    print(f"\n   NOTE: the absolute 'degenerate <$1' metric is price-confounded -- $1 was "
          f"~10% of price\n   at $10 (2022) but ~0.4% at the $260 (2024) peak. Range height % "
          f"(above) is the\n   era-invariant chop signal; consider a %-of-price degeneracy gate "
          f"when locking.")


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(ROOT))
    from shared.memhygiene import install        # Memory hygiene: see MEMORY_HYGIENE.md
    install("analyze_5m_layer1")
    main()
