"""
detectors/plot_range_detector.py — generic TF-aware range-detector plot.

Reads any range_detector_<tf>_layer1.json (4h, 1d, future 1w/1h), infers
the timeframe, and scales candle width + panel span accordingly. Per-panel
high-resolution export (one PNG per window).

Drawing (asymmetric-confirmation layer-1 schema):
  CONFIRMED: range_high (pink) + range_low (green) stepped bands from
    band_history; range_mid dashed; phase-2 confirm line (blue); end line
    (green=breakout_up / red=breakdown_down); failed_break x markers.
  PHASE-1 (timeout / phase_1_failed): predefined band only (faint, dashed
    edge); gray dotted "timeout" / magenta "phase_1_failed" end line.
  leg_end marker (v), colored by override/fallback.
"""
from __future__ import annotations
import argparse, json, sys
from pathlib import Path
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from matplotlib.patches import Rectangle
from matplotlib.lines import Line2D
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Per-TF plotting params: (bar_hours, weeks_per_panel, day_locator_interval).
TF_PLOT = {
    "1h": (1.0, 2, 1),
    "2h": (2.0, 3, 1),
    "4h": (4.0, 5, 2),
    "1d": (24.0, 26, 14),
    "1w": (168.0, 104, 56),
}


def _candles(ax, sub, bar_hours):
    w = bar_hours / 24.0 * 0.7
    x = mdates.date2num(sub["dt"].dt.to_pydatetime())
    for i in range(len(sub)):
        o, h, l, c = (sub["open"].iloc[i], sub["high"].iloc[i],
                       sub["low"].iloc[i], sub["close"].iloc[i])
        col = "#26a69a" if c >= o else "#ef5350"
        ax.plot([x[i], x[i]], [l, h], color=col, lw=0.6, zorder=1)
        ax.add_patch(Rectangle((x[i] - w / 2, min(o, c)), w, max(0.001, abs(c - o)),
                                facecolor=col, edgecolor=col, lw=0.3, zorder=2))


def _le_color(r):
    if r["fallback_used"]:
        return "#d32f2f"
    if r["wick_overridden"]:
        return "#2e7d32"
    return "#616161"


def _draw_record(ax, r, ps, pe):
    t_p1 = pd.Timestamp(r["range_phase_1_ts"])
    t_end = pd.Timestamp(r["range_end_ts"]) if r["range_end_ts"] else pe
    if t_end < ps or t_p1 > pe:
        return
    direction = r["impulse_direction"]
    if r["is_confirmed"]:
        hist = r["band_history"]
        for k, h in enumerate(hist):
            a = pd.Timestamp(h["ts"])
            b = pd.Timestamp(hist[k + 1]["ts"]) if k + 1 < len(hist) else t_end
            if b < ps or a > pe:
                continue
            xa = mdates.date2num(max(a, ps)); xb = mdates.date2num(min(b, pe))
            wpx = max(0.0001, xb - xa)
            ax.add_patch(Rectangle((xa, h["rh_l"]), wpx, max(0.0, h["rh_u"] - h["rh_l"]),
                                    facecolor="#ef5350", alpha=0.16, edgecolor="#ef5350",
                                    lw=0.4, zorder=3))
            ax.add_patch(Rectangle((xa, h["rl_l"]), wpx, max(0.0, h["rl_u"] - h["rl_l"]),
                                    facecolor="#26a69a", alpha=0.16, edgecolor="#26a69a",
                                    lw=0.4, zorder=3))
        x0 = mdates.date2num(max(pd.Timestamp(r["range_phase_2_ts"]), ps))
        x1 = mdates.date2num(min(t_end, pe))
        ax.hlines(r["range_mid"], xmin=x0, xmax=x1, colors="#5c6bc0", lw=1.0,
                   linestyles=(0, (4, 3)), zorder=4)
        t_p2 = pd.Timestamp(r["range_phase_2_ts"])
        if ps <= t_p2 <= pe:
            ax.axvline(mdates.date2num(t_p2), color="#1565c0", lw=1.3, zorder=4)
            m = r["metadata_at_creation"]
            ax.annotate(f"P2 {direction}|{m.get('ema_1h_state','')[:5]}",
                        (mdates.date2num(t_p2), r["range_high_upper"]),
                        fontsize=8, rotation=90, va="bottom", ha="right",
                        color="#1565c0", zorder=6)
        if r["range_end_ts"] and ps <= t_end <= pe:
            col = "#2e7d32" if r["range_end_reason"] == "breakout_up" else "#c62828"
            ax.axvline(mdates.date2num(t_end), color=col, lw=1.4, zorder=4)
        for fb in r["failed_break_events"]:
            t_fb = pd.Timestamp(fb["ts"])
            if ps <= t_fb <= pe:
                c = "#ff9800" if fb["direction"] == "up" else "#7b1fa2"
                ax.scatter(mdates.date2num(t_fb), fb["close"], marker="x", s=55,
                            color=c, lw=1.6, zorder=7)
    else:
        pb = r["phase_1_band"]
        x0 = mdates.date2num(max(t_p1, ps)); x1 = mdates.date2num(min(t_end, pe))
        wpx = max(0.0001, x1 - x0)
        if direction == "up":
            lo, hi, col = pb["range_high_lower"], pb["range_high_upper"], "#ef5350"
        else:
            lo, hi, col = pb["range_low_lower"], pb["range_low_upper"], "#26a69a"
        ax.add_patch(Rectangle((x0, lo), wpx, max(0.0, hi - lo), facecolor=col,
                                alpha=0.08, edgecolor=col, lw=0.5, linestyle="--", zorder=3))
        endcol = "#9e9e9e" if r["phase_1_outcome"] == "timeout" else "#ad1457"
        if ps <= t_end <= pe:
            ax.axvline(mdates.date2num(t_end), color=endcol, lw=1.0,
                       linestyle=(0, (2, 2)), zorder=4)
            ax.annotate(r["phase_1_outcome"], (mdates.date2num(t_end), hi),
                        fontsize=7, rotation=90, va="bottom", ha="right",
                        color=endcol, zorder=6)
    t_le = pd.Timestamp(r["leg_end_ts"])
    if ps <= t_le <= pe:
        le_p = (r["leg_end_price_accepted"] if r["wick_overridden"] else r["leg_end_price_raw"])
        ax.scatter(mdates.date2num(t_le), le_p, marker="v", s=70,
                    color=_le_color(r), edgecolors="black", lw=0.5, zorder=6)


def _panels(df, weeks):
    t0, t1 = df["dt"].iloc[0], df["dt"].iloc[-1]
    out = []; cur = t0; step = pd.Timedelta(weeks=weeks)
    while cur < t1:
        nxt = min(cur + step, t1 + pd.Timedelta(hours=4))
        out.append((cur, nxt)); cur = nxt
    return out


def plot_panels(df, output, out_dir, weeks=None, dpi=150, pair="SOL_USDT"):
    tf = output.get("timeframe", "4h")
    sym = pair.split("_")[0]                 # label prefix, e.g. "SOL"
    bar_hours, def_weeks, day_int = TF_PLOT.get(tf, (4.0, 5, 2))
    weeks = weeks or def_weeks
    df = df.copy(); df["dt"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df = df.sort_values("dt").reset_index(drop=True)
    ranges = output["ranges"]
    out_dir = Path(out_dir); out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    panels = _panels(df, weeks)
    for pi, (psd, ped) in enumerate(panels):
        fig, ax = plt.subplots(figsize=(19, 8.4))
        sub = df[(df["dt"] >= psd) & (df["dt"] < ped)]
        if len(sub):
            _candles(ax, sub, bar_hours)
        ax.set_xlim(mdates.date2num(psd), mdates.date2num(ped))
        ax.xaxis_date()
        ax.xaxis.set_major_locator(mdates.DayLocator(interval=day_int))
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m-%d"))
        ax.tick_params(axis="x", labelsize=8, rotation=45)
        ax.grid(True, alpha=0.25, lw=0.5)
        ax.set_ylabel(pair.replace("_", "/"), fontsize=11)
        for r in ranges:
            _draw_record(ax, r, psd, ped)
        ax.set_title(
            f"Range Detector {tf.upper()} L1 — {sym} — panel {pi+1}/{len(panels)} — "
            f"{psd.strftime('%Y-%m-%d')} to {(ped-pd.Timedelta(hours=bar_hours)).strftime('%Y-%m-%d')}\n"
            f"engine={'close-based' if output['config'].get('use_close_based_extremes') else 'wick-based'}  "
            f"confirmed={output['n_confirmed']} timeout={output['n_phase_1_timeout']} failed={output['n_phase_1_failed']}",
            fontsize=11)
        legend = [
            Line2D([0],[0], color="#ef5350", lw=7, alpha=0.35, label="range_high band"),
            Line2D([0],[0], color="#26a69a", lw=7, alpha=0.35, label="range_low band"),
            Line2D([0],[0], color="#5c6bc0", lw=1.2, linestyle="--", label="range_mid"),
            Line2D([0],[0], color="#1565c0", lw=1.3, label="phase-2 confirm"),
            Line2D([0],[0], color="#2e7d32", lw=1.4, label="end breakout_up"),
            Line2D([0],[0], color="#c62828", lw=1.4, label="end breakdown_down"),
            Line2D([0],[0], color="#9e9e9e", lw=1.0, linestyle=":", label="phase_1 timeout"),
            Line2D([0],[0], color="#ad1457", lw=1.0, linestyle=":", label="phase_1 failed"),
            Line2D([0],[0], marker="v", color="w", markerfacecolor="#616161",
                   markeredgecolor="k", markersize=9, label="leg_end"),
            Line2D([0],[0], marker="x", color="#ff9800", linestyle="", markersize=8,
                   label="failed_break up (ceiling held)"),
            Line2D([0],[0], marker="x", color="#7b1fa2", linestyle="", markersize=8,
                   label="failed_break down (floor held)"),
        ]
        ax.legend(handles=legend, loc="upper left", fontsize=8, framealpha=0.92, ncol=2)
        fig.tight_layout()
        # Descriptive filename so saved files are self-identifying.
        outp = out_dir / (f"{sym}_{tf}_range_detector_L1_"
                          f"{psd.strftime('%Y-%m-%d')}_to_"
                          f"{(ped-pd.Timedelta(hours=bar_hours)).strftime('%Y-%m-%d')}.png")
        fig.savefig(outp, dpi=dpi); plt.close(fig)
        written.append(outp)
    return written


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--json", required=True)
    p.add_argument("--data", required=True)
    p.add_argument("--panel-dir", required=True)
    p.add_argument("--weeks-per-panel", type=int, default=None)
    p.add_argument("--dpi", type=int, default=150)
    p.add_argument("--pair", default="SOL_USDT")
    a = p.parse_args()
    df = pd.read_csv(a.data)
    output = json.loads(Path(a.json).read_text())
    paths = plot_panels(df, output, Path(a.panel_dir), a.weeks_per_panel, a.dpi, a.pair)
    print(f"[plot {output.get('timeframe')}] wrote {len(paths)} panels to {a.panel_dir} @ {a.dpi}dpi")
    plt.close("all")                  # free ALL matplotlib figures (per-panel close
    del df, output, paths             # already runs in plot_panels; this is a backstop)


if __name__ == "__main__":
    from shared.memhygiene import finalize    # Memory hygiene: see MEMORY_HYGIENE.md
    main()
    finalize("plot_range_detector")
