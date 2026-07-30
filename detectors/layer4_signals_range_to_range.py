"""
detectors/layer4_signals_range_to_range.py — Layer 4 v1, setup #2: range-to-range.

The complement of setup #1 (retest_fail). Where setup #1 trades a level that
HELD (`rejected` => fade), this trades a level that FAILED (`broken` =>
continuation toward the next structural level): "broke this range edge, run to
the next one."

Trigger / direction
-------------------
A layer-5 `broken` event on a strong/weak range-edge level. The `broken` event
fires at its own bar (a decisive close past the 1% buffer) — no forward
confirmation, so it is point-in-time clean (unlike the range detector's end,
which needs recovery_lookahead bars to settle).
  * resistance-role level broken  => price closed ABOVE it  => LONG
  * support-role level broken      => price closed BELOW it  => SHORT

Entry / SL / TP / time-stop
---------------------------
  * Entry: close of the break bar.
  * SL: just back across the broken level (re-cross = failed break) =
    level -/+ 1.5*touch_band on the side price came from.
  * TP: nearest STRONG historical level in the break direction (same TF), or
    the active 1D range edge; planned R:R in [2, 6] else SKIP.
  * Time-stop: max-favorable-excursion < 0.5R by 24 bars => exit at that close.

All heavy lifting (data load, point-in-time strength, TP picking, simulation,
cascade context) is reused from the setup-#1 Layer4 harness by composition, so
the two setups stay independent modules but share one tested engine.
"""
from __future__ import annotations
import json, sys
from pathlib import Path
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from detectors.layer4_signals_retest_fail import (
    Layer4, _stats, _print, _ns, TFS, TF_RANK,
    TOUCH_BAND_PCT, SL_BAND_MULT, FEE)


class RangeToRange:
    def __init__(self):
        self.b = Layer4()                      # reuse the loaded data + helpers

    def generate(self, tf, pit=True):
        b = self.b; out = []; n = len(b.bars[tf]["c"])
        for L, ets, ka in b.lev[tf]:
            role = L.get("initial_role")
            if role == "resistance":
                direction = "long"             # broke UP through resistance
            elif role == "support":
                direction = "short"            # broke DOWN through support
            else:
                continue
            lvl = L["price"]
            for e in L["events"]:
                if e["type"] != "broken":
                    continue
                T = e["ts"]; T_ns = _ns(T)
                if pit and ka is not None and T_ns < ka:
                    continue                 # level not yet forward-only knowable
                cls = b.pit_strength(L, ets, T_ns, tf)[0] if pit else L.get("strength_class")
                if cls not in ("strong", "weak"):
                    continue
                ei = b.idx_of(tf, T)
                if ei < 0 or ei >= n - 1:
                    continue
                entry = float(e["bar_close"])
                if direction == "long":
                    sl = lvl * (1 - SL_BAND_MULT * TOUCH_BAND_PCT)
                    if sl >= entry:
                        continue
                else:
                    sl = lvl * (1 + SL_BAND_MULT * TOUCH_BAND_PCT)
                    if sl <= entry:
                        continue
                tp, rr = b._pick_tp(tf, direction, entry, sl, T, pit)
                if tp is None:
                    continue
                exit_px, outcome, xidx = b._simulate(tf, direction, ei, entry, sl, tp)
                risk = abs(entry - sl)
                gross_R = ((entry - exit_px) if direction == "short" else (exit_px - entry)) / risk
                fee_R = (entry + exit_px) * FEE / risk
                out.append({
                    "tf": tf, "direction": direction,
                    "entry_ts": T, "entry_price": round(entry, 4),
                    "sl_price": round(sl, 4), "tp_price": round(tp, 4),
                    "rr_planned": rr,
                    "level_tested": round(lvl, 4),
                    "level_source_range_id": L["source_range_id"],
                    "level_strength_class": cls,
                    "level_src_tf_ge_4h": TF_RANK[tf] >= 3,
                    "trigger_event_id": f"{L['level_id']}@{T[:13]}",
                    "cascade_context": b._alignment(direction, entry, T),
                    "exit_ts": pd.Timestamp(b.bars[tf]["ts"][xidx]).isoformat(),
                    "exit_price": round(exit_px, 4), "outcome": outcome,
                    "r_gross": round(float(gross_R), 4),
                    "r_net": round(float(gross_R - fee_R), 4),
                })
        return out

    def run(self, pit=True, save=True):
        result = {"version": 1, "setup": "range_to_range", "point_in_time": pit, "by_tf": {}}
        all_c = []
        for tf in TFS:
            c = self.generate(tf, pit=pit)
            all_c += c
            result["by_tf"][tf] = _stats(c)
        result["candidates"] = all_c
        if save:
            tag = "pit" if pit else "naive"
            p = ROOT / f"backtesting/results/layer4_range_to_range_{tag}.json"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(result, indent=2), encoding="utf-8")
        return result


if __name__ == "__main__":
    from shared.memhygiene import install        # Memory hygiene: see MEMORY_HYGIENE.md
    install("layer4_range_to_range")
    from data.freshness_monitor import consumer_startup   # Freshness contract: see MEMORY_HYGIENE.md
    consumer_startup(end_consumer=True)                    # auto-fetch + cascade regen (--no-freshness / --no-regen)
    from detectors.layer4_signals_retest_fail import warn_if_windowed
    warn_if_windowed()                                     # service worker may have windowed the shared artifacts
    bt = RangeToRange()
    _print(bt.run(pit=True), "range_to_range POINT-IN-TIME (honest)")
    _print(bt.run(pit=False), "range_to_range naive (look-ahead)")
