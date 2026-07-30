"""
backtesting/compare_continuation_rule.py — in-process A/B for the
continuation-cancel rule. Runs every variant in the sweep grid twice
(rule ON, rule OFF), keeps results in memory, reports aggregate deltas.
Does NOT write per-variant CSVs.
"""
from __future__ import annotations
import importlib.util, sys
from itertools import product
from pathlib import Path
import pandas as pd
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec); sys.modules[name] = m
    spec.loader.exec_module(m); return m

cfg = _load("cfg", ROOT / "strategies" / "bos_retest_v1.py").CONFIG
eng = _load("eng", ROOT / "backtesting" / "bos_retest_engine.py")

df = pd.read_csv(ROOT / "data/raw/SOL_USDT_1h.csv").sort_values("timestamp").reset_index(drop=True)
events, _ = eng.precompute_bos_events(df, cfg["reversal_pct_1h"], cfg["init_bars"])
bias = eng.compute_ema_bias(df["close"].to_numpy(float),
                             cfg["htf_ema_fast"], cfg["htf_ema_slow"])

grid = list(product(cfg["sweep_entry_rules"], cfg["sweep_buffers"],
                    cfg["sweep_sl_placements"], cfg["sweep_tp_rr"],
                    cfg["sweep_min_swings"], cfg["sweep_timeouts"]))
print(f"variants per side: {len(grid)}  -> running {2*len(grid)} total")

records = []
for entry_rule, buf, sl_pl, tp, min_sw, to in grid:
    out = {}
    for cont_flag in (True, False):
        p = eng.VariantParams(entry_rule, float(buf), sl_pl, float(tp),
                              float(min_sw), int(to), cont_flag)
        rows, agg = eng.run_variant(df, events, bias, p,
            risk_pct=float(cfg["risk_pct"]),
            fee_rate=float(cfg["taker_fee"]),
            account=float(cfg["account"]))
        st = eng.summarize_variant(rows)
        key = "on" if cont_flag else "off"
        out[f"n_{key}"] = st["n_filled"]
        out[f"ev_net_{key}"] = st["ev_net_R"]
        out[f"pf_net_{key}"] = st["pf_net"] if st["pf_net"] is not None else float("inf")
        out[f"wr_{key}"] = st["wr"]
        out[f"dd_{key}"] = st["max_dd_R"]
        out[f"sum_R_{key}"] = st["sum_net_R"]
        out[f"cancl_{key}"] = agg.get("n_cancelled_continuation", 0)
    out.update(dict(entry_rule=entry_rule, buf=buf, sl=sl_pl, tp=tp,
                    minsw=min_sw, to=to))
    out["d_ev_net"] = out["ev_net_off"] - out["ev_net_on"]
    out["d_sum_R"] = out["sum_R_off"] - out["sum_R_on"]
    out["d_n"] = out["n_off"] - out["n_on"]
    records.append(out)

R = pd.DataFrame(records)
# Aggregate across the whole grid
print("\n=== AGGREGATE OVER ALL 1920 VARIANTS ===")
print(f"mean N    (rule on):  {R['n_on'].mean():.0f}     "
      f"mean N    (off):  {R['n_off'].mean():.0f}")
print(f"mean evN  (rule on):  {R['ev_net_on'].mean():+.4f}R  "
      f"mean evN  (off):  {R['ev_net_off'].mean():+.4f}R   "
      f"delta(off-on): {R['d_ev_net'].mean():+.4f}R")
print(f"mean sumR (rule on):  {R['sum_R_on'].mean():+.2f}R    "
      f"mean sumR (off):  {R['sum_R_off'].mean():+.2f}R     "
      f"delta(off-on): {R['d_sum_R'].mean():+.2f}R")
print(f"mean WR   (rule on):  {R['wr_on'].mean():.2%}    "
      f"mean WR   (off):  {R['wr_off'].mean():.2%}")
print(f"mean DD   (rule on):  {R['dd_on'].mean():.2f}R     "
      f"mean DD   (off):  {R['dd_off'].mean():.2f}R")
print(f"# variants where rule HELPS (ev_off < ev_on): {(R['d_ev_net']<0).sum()} / {len(R)}")
print(f"# variants where rule HURTS (ev_off > ev_on): {(R['d_ev_net']>0).sum()} / {len(R)}")
print(f"# variants tied:                              {(R['d_ev_net']==0).sum()} / {len(R)}")

# Breakdown by tp and timeout
print("\n=== EV (net R) by TP_RR x rule ===")
piv = R.groupby("tp")[["ev_net_on","ev_net_off"]].mean().round(4)
piv["delta(off-on)"] = (piv["ev_net_off"] - piv["ev_net_on"]).round(4)
print(piv.to_string())

print("\n=== EV (net R) by timeout x rule ===")
piv2 = R.groupby("to")[["ev_net_on","ev_net_off"]].mean().round(4)
piv2["delta(off-on)"] = (piv2["ev_net_off"] - piv2["ev_net_on"]).round(4)
print(piv2.to_string())

print("\n=== EV (net R) by SL placement x rule ===")
piv3 = R.groupby("sl")[["ev_net_on","ev_net_off"]].mean().round(4)
piv3["delta(off-on)"] = (piv3["ev_net_off"] - piv3["ev_net_on"]).round(4)
print(piv3.to_string())

# Top variants under each rule (best ev_net)
print("\n=== TOP 5 (rule ON) ===")
top_on = R.nlargest(5, "ev_net_on")
print(top_on[["entry_rule","buf","sl","tp","minsw","to",
              "n_on","wr_on","ev_net_on","pf_net_on","dd_on"]].to_string(index=False))
print("\n=== TOP 5 (rule OFF) ===")
top_off = R.nlargest(5, "ev_net_off")
print(top_off[["entry_rule","buf","sl","tp","minsw","to",
               "n_off","wr_off","ev_net_off","pf_net_off","dd_off"]].to_string(index=False))

# Save
out_path = ROOT / "backtesting" / "results" / "continuation_rule_ab.csv"
R.to_csv(out_path, index=False)
print(f"\n[write] {out_path}")
