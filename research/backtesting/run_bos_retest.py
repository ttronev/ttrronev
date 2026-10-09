"""
backtesting/run_bos_retest.py — sweep runner for the 1H BOS retest study.

Loads SOL_USDT_1h.csv, precomputes BOS events + 1H bias once, then runs
all 480 variants from strategies/bos_retest_v1.py CONFIG. Per-variant
trade rows are written to backtesting/results/bos_retest_v1_*.csv, and
the aggregate ranking is written to bos_retest_v1_summary.json.

Validation: searches each variant's trade list for a trade whose BOS
timestamp falls in the user-specified May 13-14 2026 window. The
findings are printed at the end and embedded in the summary JSON.
"""
from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import sys
import time
from dataclasses import asdict
from itertools import product
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Load strategy config without triggering data/__init__.py imports.
def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod   # Required: dataclasses lookup sys.modules[__module__]
    spec.loader.exec_module(mod)
    return mod


def variant_tag(p) -> str:
    """Stable, filename-safe tag encoding the sweep params."""
    buf = f"{p.buffer_pct:.3f}"
    minsw = f"{p.min_swing_pct:.3f}"
    return (
        f"{p.entry_rule}__buf{buf}__sl-{p.sl_placement}"
        f"__tp{p.tp_rr:g}R__minsw{minsw}__to{int(p.bars_to_timeout)}"
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", default="data/raw/SOL_USDT_1h.csv")
    parser.add_argument("--results-dir", default="backtesting/results")
    parser.add_argument("--write-per-variant-csv", action="store_true",
                        help="Write a per-variant trade CSV (480 files).")
    parser.add_argument("--validate-window-start", default="2026-05-13 00:00",
                        help="UTC ISO start for May 13-14 validation window.")
    parser.add_argument("--validate-window-end", default="2026-05-14 23:00",
                        help="UTC ISO end for validation window.")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    cfg_mod = _load_module("bos_retest_v1",
                           ROOT / "strategies" / "bos_retest_v1.py")
    cfg = cfg_mod.CONFIG
    engine = _load_module("bos_retest_engine",
                          ROOT / "backtesting" / "bos_retest_engine.py")

    # -- Load + precompute -------------------------------------------------
    data_path = Path(args.data)
    df = pd.read_csv(data_path).sort_values("timestamp").reset_index(drop=True)
    n_bars = len(df)
    ts_first = pd.Timestamp(df["timestamp"].iloc[0], unit="ms", tz="UTC")
    ts_last = pd.Timestamp(df["timestamp"].iloc[-1], unit="ms", tz="UTC")
    if not args.quiet:
        print(f"[load] {data_path.name}: {n_bars} bars  "
              f"{ts_first} -> {ts_last}")

    t0 = time.time()
    events, _state_arr = engine.precompute_bos_events(
        df, reversal_pct=cfg["reversal_pct_1h"], init_bars=cfg["init_bars"]
    )
    bias = engine.compute_ema_bias(
        df["close"].to_numpy(dtype=float),
        ema_fast=cfg["htf_ema_fast"], ema_slow=cfg["htf_ema_slow"],
    )
    t1 = time.time()
    if not args.quiet:
        print(f"[precompute] {len(events)} BOS events  "
              f"reversal_pct={cfg['reversal_pct_1h']}  in {t1-t0:.2f}s")

    # -- Sweep -------------------------------------------------------------
    grid = list(product(
        cfg["sweep_entry_rules"],
        cfg["sweep_buffers"],
        cfg["sweep_sl_placements"],
        cfg["sweep_tp_rr"],
        cfg["sweep_min_swings"],
        cfg["sweep_timeouts"],
    ))
    if not args.quiet:
        print(f"[sweep] {len(grid)} variants")

    results_dir = Path(args.results_dir)
    results_dir.mkdir(parents=True, exist_ok=True)

    win_start = pd.Timestamp(args.validate_window_start, tz="UTC")
    win_end = pd.Timestamp(args.validate_window_end, tz="UTC")

    summary_rows: list[dict] = []
    validation_hits: list[dict] = []   # records about the May 13-14 trade
    n_variants_with_textbook_trade = 0

    t0 = time.time()
    for i, (entry_rule, buf_pct, sl_pl, tp_rr, min_sw, timeout) in enumerate(grid):
        params = engine.VariantParams(
            entry_rule=entry_rule,
            buffer_pct=float(buf_pct),
            sl_placement=sl_pl,
            tp_rr=float(tp_rr),
            min_swing_pct=float(min_sw),
            bars_to_timeout=int(timeout),
        )
        rows, agg = engine.run_variant(
            df=df, events=events, bias=bias,
            params=params,
            risk_pct=float(cfg["risk_pct"]),
            fee_rate=float(cfg["taker_fee"]),
            account=float(cfg["account"]),
        )
        stats = engine.summarize_variant(rows)

        tag = variant_tag(params)
        if args.write_per_variant_csv:
            out_csv = results_dir / f"bos_retest_v1__{tag}.csv"
            with out_csv.open("w", newline="", encoding="utf-8") as f:
                if rows:
                    fieldnames = list(engine.trade_row_to_dict(rows[0]).keys())
                    writer = csv.DictWriter(f, fieldnames=fieldnames)
                    writer.writeheader()
                    for r in rows:
                        writer.writerow(engine.trade_row_to_dict(r))
                else:
                    f.write("")

        # Validation: did this variant open the specific textbook trade
        # (BOS bar at May 13 12:00, retest_level ~93.55, short)?
        textbook = []
        textbook_target_hit = False
        for r in rows:
            in_bos = (r.bos_ts is not None and win_start <= r.bos_ts <= win_end)
            in_fill = (r.fill_ts is not None and win_start <= r.fill_ts <= win_end)
            if (in_bos or in_fill) and r.outcome in ("tp", "sl", "open_eod"):
                is_textbook = (
                    r.bos_ts is not None
                    and pd.Timestamp(r.bos_ts) == pd.Timestamp("2026-05-13 12:00", tz="UTC")
                    and r.direction == "short"
                    and 93.45 <= r.retest_level <= 93.65
                )
                if is_textbook:
                    textbook_target_hit = True
                textbook.append({
                    "is_textbook_target": is_textbook,
                    "bos_ts": pd.Timestamp(r.bos_ts).isoformat(),
                    "fill_ts": pd.Timestamp(r.fill_ts).isoformat() if r.fill_ts else None,
                    "exit_ts": pd.Timestamp(r.exit_ts).isoformat() if r.exit_ts else None,
                    "direction": r.direction,
                    "retest_level": r.retest_level,
                    "entry_price": r.entry_price,
                    "sl_price": r.sl_price,
                    "tp_price": r.tp_price,
                    "outcome": r.outcome,
                    "r_net": r.r_net,
                })
        if textbook:
            n_variants_with_textbook_trade += 1
            validation_hits.append({
                "variant_tag": tag,
                "textbook_target_hit": textbook_target_hit,
                "trades_in_window": textbook,
            })

        summary_rows.append({
            "variant_idx": i,
            "tag": tag,
            "entry_rule": entry_rule,
            "buffer_pct": float(buf_pct),
            "sl_placement": sl_pl,
            "tp_rr": float(tp_rr),
            "min_swing_pct": float(min_sw),
            "bars_to_timeout": int(timeout),
            "n_bos_total": agg["n_bos_total"],
            "n_bos_dropped_active": agg["n_bos_dropped_active"],
            "n_bos_filtered_bias": agg["n_bos_filtered_bias"],
            "n_bos_filtered_min_swing": agg["n_bos_filtered_min_swing"],
            "n_setups_pending": agg["n_setups_pending"],
            "n_filled": stats["n_filled"],
            "n_unfilled": stats["n_unfilled"],
            "n_tp": agg["n_tp"],
            "n_sl": agg["n_sl"],
            "n_invalidated": agg["n_invalidated"],
            "n_timed_out": agg["n_timed_out"],
            "n_cancelled_continuation": agg.get("n_cancelled_continuation", 0),
            "n_open_eod": agg["n_open_eod"],
            "wr": stats["wr"],
            "avg_win_R": stats["avg_win_R"],
            "avg_loss_R": stats["avg_loss_R"],
            "ev_gross_R": stats["ev_gross_R"],
            "ev_net_R": stats["ev_net_R"],
            "pf_gross": stats["pf_gross"],
            "pf_net": stats["pf_net"],
            "max_dd_R": stats["max_dd_R"],
            "max_consec_losses": stats["max_consec_losses"],
            "sum_net_R": stats["sum_net_R"],
            "r_min": stats["r_min"],
            "r_max": stats["r_max"],
        })
        if not args.quiet and (i + 1) % 50 == 0:
            print(f"  variant {i+1}/{len(grid)}")
    t1 = time.time()
    if not args.quiet:
        print(f"[sweep] done in {t1-t0:.1f}s")

    # -- Rank + summarize --------------------------------------------------
    summary_rows_sorted = sorted(
        summary_rows, key=lambda r: r["ev_net_R"], reverse=True
    )
    top_20 = summary_rows_sorted[:20]

    # Find best variant satisfying decision rule.
    qualifying = [r for r in summary_rows_sorted
                  if r["ev_net_R"] is not None and r["pf_net"] is not None
                  and r["ev_net_R"] >= 0.20
                  and r["pf_net"] >= 1.5
                  and r["n_filled"] >= 100]
    decision = "no_signal"
    if qualifying:
        decision = "signal"
    else:
        marginals = [r for r in summary_rows_sorted
                     if r["ev_net_R"] is not None
                     and 0.05 <= r["ev_net_R"] < 0.20]
        if marginals:
            decision = "marginal"

    # Timeout distribution in top 20.
    timeout_top20_counts: dict[int, int] = {}
    for r in top_20:
        t = int(r["bars_to_timeout"])
        timeout_top20_counts[t] = timeout_top20_counts.get(t, 0) + 1

    summary_json = {
        "strategy_id": cfg["strategy_id"],
        "data_path": str(data_path),
        "n_bars": n_bars,
        "data_first_ts": ts_first.isoformat(),
        "data_last_ts": ts_last.isoformat(),
        "reversal_pct_1h": cfg["reversal_pct_1h"],
        "init_bars": cfg["init_bars"],
        "ema_fast": cfg["htf_ema_fast"],
        "ema_slow": cfg["htf_ema_slow"],
        "timeout_top20_counts": timeout_top20_counts,
        "risk_pct": cfg["risk_pct"],
        "taker_fee": cfg["taker_fee"],
        "n_bos_events_precomputed": len(events),
        "n_variants": len(grid),
        "decision": decision,
        "top_20_by_net_ev": top_20,
        "all_variants_ranked": summary_rows_sorted,
        "validation": {
            "window_start": args.validate_window_start,
            "window_end": args.validate_window_end,
            "n_variants_with_trade_in_window": n_variants_with_textbook_trade,
            "hits": validation_hits,
        },
    }
    out_json = results_dir / "bos_retest_v1_summary.json"
    out_json.write_text(json.dumps(summary_json, indent=2), encoding="utf-8")
    if not args.quiet:
        print(f"[write] {out_json}")

    # -- CLI ranking print -------------------------------------------------
    print("\n=== TOP 20 BY NET EV ===")
    print(f"{'rk':>3}  {'tag':<78}  {'N':>5}  {'WR':>5}  "
          f"{'evG':>6}  {'evN':>6}  {'pfN':>5}  {'DD':>6}  {'streak':>6}")
    for k, r in enumerate(top_20, start=1):
        pf = r["pf_net"]
        pf_s = f"{pf:.2f}" if (pf is not None) else "  inf"
        print(f"{k:>3}  {r['tag']:<78}  {r['n_filled']:>5}  "
              f"{r['wr']:>5.2%}  {r['ev_gross_R']:>6.3f}  "
              f"{r['ev_net_R']:>6.3f}  {pf_s:>5}  "
              f"{r['max_dd_R']:>6.2f}  {r['max_consec_losses']:>6}")
    print(f"\ntimeout distribution in top 20: {timeout_top20_counts}")

    print("\n=== VALIDATION: May 13 12:00 textbook BOS (retest ~93.55) ===")
    n_textbook_hits = sum(1 for h in validation_hits if h.get("textbook_target_hit"))
    print(f"window: {args.validate_window_start} -> {args.validate_window_end}")
    print(f"variants with any trade in window: {n_variants_with_textbook_trade}/{len(grid)}")
    print(f"variants that opened the May 13 12:00 short at retest~93.55: "
          f"{n_textbook_hits}/{len(grid)}")
    textbook_hits = [h for h in validation_hits if h.get("textbook_target_hit")]
    if textbook_hits:
        # Distribution by timeout
        by_to: dict[int, int] = {}
        for h in textbook_hits:
            # extract timeout from tag, format: ...__toNN
            to_part = h["variant_tag"].split("__to")[-1]
            to = int(to_part)
            by_to[to] = by_to.get(to, 0) + 1
        print(f"  by timeout: {by_to}")
        sample = textbook_hits[0]
        for t in sample["trades_in_window"]:
            if t.get("is_textbook_target"):
                print(f"  sample variant: {sample['variant_tag']}")
                print(f"    bos={t['bos_ts']}  fill={t['fill_ts']}  exit={t['exit_ts']}")
                print(f"    retest={t['retest_level']:.3f}  entry={t['entry_price']:.3f}  "
                      f"sl={t['sl_price']:.3f}  tp={t['tp_price']:.3f}")
                print(f"    outcome={t['outcome']}  r_net={t['r_net']}")
                break
    else:
        print("  *** TEXTBOOK BOS STILL NOT CAUGHT ***")

    print(f"\n=== DECISION: {decision} ===")


if __name__ == "__main__":
    from shared.memhygiene import install        # Memory hygiene: see MEMORY_HYGIENE.md
    install("run_bos_retest")
    main()
