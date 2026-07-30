"""
detectors/layer4_signals_retest_fail.py — Layer 4 v1, setup #1: retest-then-fail.

First thin signal layer on top of the locked information layer (L1 ranges +
L5 memory + L5.1 strength). Candidate generation + backtest ONLY — no
auto/paper trading. Each candidate is a structured trade idea with
entry/SL/TP; the backtest simulates it on that TF's bars.

Setup (locked design — role-based, range-edge levels)
-----------------------------------------------------
A retest-then-fail SHORT fires on a layer-5 `rejected` event at a RESISTANCE
level; the LONG mirror fires on a `rejected` event at a SUPPORT level:
  * Eligibility: strength_class in {strong, weak} (neutralized = noise).
    initial_role decides direction (resistance => short, support => long).
  * Trigger: a `rejected` event = a wick that pierced beyond the level and
    closed back on the hold side (a retest that failed). The 3% re-arm means
    one approach emits one event, so "touched-then-rejected" collapses to it.
  * Entry: close of the rejection bar.
  * SL: short => max(level*(1+1.5*band), rejection high); long => mirror.
  * TP: nearest STRONG historical level beyond entry (same TF) or the active
    1D range edge at entry; require planned R:R >= 2 else SKIP.
  * Time-stop: no progress (no new low short / new high long) in 24 bars => BE.

POINT-IN-TIME (pit=True, default) — the honest backtest
-------------------------------------------------------
The naive backtest leaks the future: a level's strength_class is computed
over its WHOLE event history (to 2026-06), and TP targets include levels
whose source range ended AFTER the trade. Both bias results optimistic.
In pit mode every classification is recomputed from only the events at or
before the entry bar, and TP targets are limited to levels whose source
range had already ended (and that classify strong) as of entry. The trigger
event, initial_role, and source-range cleanness are already point-in-time
(the level only exists after its source range ends). Simulation uses future
PRICE only — that is the real outcome, not look-ahead.

Strength is recomputed with the SAME locked layer-5/5.1 functions, just fed a
time-truncated event slice, so classification logic stays identical.
"""
from __future__ import annotations
import json, sys
from pathlib import Path
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from detectors.layer5_range_memory import _summarize
from detectors.layer5_1_strength import (
    _factors, _score, NEUTRALIZED_CYCLES, STRONG_MAX_CYCLES, STRONG_SCORE)
from detectors import paths

TFS = ["1w", "1d", "4h", "2h", "1h"]
TF_RANK = {"1h": 1, "2h": 2, "4h": 3, "1d": 4, "1w": 5}     # >=4H means rank>=3
_L1 = lambda tf: paths.l1_json(tf)          # default pair (research scripts are SOL-only)
_MEM = lambda tf: paths.mem_json(tf)
_CSV = lambda tf: paths.raw_csv(tf)

TOUCH_BAND_PCT = 0.003       # matches L5 detection band


def warn_if_windowed(tfs=None):
    """The service worker regenerates 1h/2h/4h layer-1 on a LIMITED window
    (service/pairs.REGEN_WINDOW_DAYS) into the same artifact files this
    backtest reads. A full-history backtest on windowed inputs is silently
    wrong — detect and warn loudly (coverage = l1 data_first_ts vs CSV
    first bar; >30d gap means the artifact is truncated)."""
    import json as _json
    import pandas as _pd
    for tf in (tfs or TFS):
        try:
            l1_first = _json.loads(_L1(tf).read_text())["data_first_ts"]
            csv_first = int(_pd.read_csv(_CSV(tf), usecols=["timestamp"],
                                         nrows=1)["timestamp"].iloc[0])
            gap_d = (_pd.Timestamp(l1_first).value // 1_000_000 - csv_first) / 86_400_000
            if gap_d > 30:
                print(f"[layer4] *** WARNING: {tf} layer-1 artifact is WINDOWED "
                      f"(starts {str(l1_first)[:10]}, {gap_d:.0f}d after the CSV). "
                      f"This backtest is NOT full-history — regen the chain "
                      f"full-history first (see service/README.md, Known trade-offs).",
                      flush=True)
        except Exception:
            pass
SL_BAND_MULT = 1.5           # SL = level +/- 1.5 * touch band (or beyond wick)
MIN_RR = 2.0                 # require planned R:R >= this
MAX_RR = 6.0                 # ...and <= this: a target needing >6R means the
                             # stop is so tight it's a noise-stopped lottery
                             # ticket — skip rather than count it as edge.
TIME_STOP_BARS = 24          # bars after which a non-progressing trade is cut
PROGRESS_R = 0.5             # "progress" = max favorable excursion >= this many
                             # R by the time-stop bar; else exit at that close.
FEE = 0.00055                # per side (taker), same as bos_retest


def _ns(ts):
    return np.datetime64(pd.Timestamp(ts).tz_localize(None))


def _classify(stats, src_cm, tf):
    """Locked L5.1 class from a (possibly time-truncated) stats dict."""
    f = _factors(stats, src_cm, tf)
    score = _score(f)
    if stats["cycle_count"] >= NEUTRALIZED_CYCLES:
        cls = "neutralized"
    elif stats["cycle_count"] <= STRONG_MAX_CYCLES and score >= STRONG_SCORE:
        cls = "strong"
    else:
        cls = "weak"
    return cls, round(score, 4)


class Layer4:
    def __init__(self):
        self.l1 = {tf: json.loads(_L1(tf).read_text()) for tf in TFS}
        self.mem = {tf: json.loads(_MEM(tf).read_text()) for tf in TFS}
        self.bars = {}
        for tf in TFS:
            df = (pd.read_csv(_CSV(tf)).drop_duplicates("timestamp")
                  .sort_values("timestamp").reset_index(drop=True))
            self.bars[tf] = {
                "ts": pd.to_datetime(df["timestamp"], unit="ms", utc=True)
                        .values.astype("datetime64[ns]"),
                "h": df["high"].to_numpy(float), "l": df["low"].to_numpy(float),
                "c": df["close"].to_numpy(float),
            }
        self.conf = {tf: [r for r in self.l1[tf]["ranges"] if r["is_confirmed"]]
                     for tf in TFS}
        # cleanness of each confirmed range (for point-in-time strength)
        self.src_cm = {tf: {r["range_id"]: r.get("cleanness_metrics", {})
                            for r in self.conf[tf]} for tf in TFS}
        # per-level event-ts arrays + source-end-ns, for fast PIT slicing
        self.lev = {}
        for tf in TFS:
            rows = []
            for L in self.mem[tf]["levels"]:
                ets = np.array([_ns(e["ts"]) for e in L["events"]],
                               dtype="datetime64[ns]") if L["events"] else np.array([], dtype="datetime64[ns]")
                # forward-only knowable time = level_available_ts (source_end +
                # recovery_lookahead); fall back to source_end if absent. Layer 4
                # gates triggers + TP targets on this, never on source_end_ts.
                kt = L.get("level_available_ts") or L.get("source_end_ts")
                ka = _ns(kt) if kt else None
                rows.append((L, ets, ka))
            self.lev[tf] = rows

    # -- helpers -----------------------------------------------------------
    def idx_of(self, tf, ts_iso):
        return int(np.searchsorted(self.bars[tf]["ts"], _ns(ts_iso)))

    def pit_strength(self, level, ets, T_ns, tf):
        """Class of `level` using only its events at/<= T_ns."""
        if len(ets):
            k = int(np.searchsorted(ets, T_ns, side="right"))
            subset = level["events"][:k]
        else:
            subset = []
        stats = _summarize(subset)
        return _classify(stats, self.src_cm[tf].get(level["source_range_id"], {}), tf)

    def range_active_at(self, tf, ts):
        t = pd.Timestamp(ts); hit = None
        for r in self.conf[tf]:
            cs = pd.Timestamp(r["range_phase_2_ts"])
            ce = pd.Timestamp(r["range_end_ts"]) if r["range_end_ts"] else pd.Timestamp("2100-01-01", tz="UTC")
            if cs <= t <= ce and (hit is None or cs > pd.Timestamp(hit["range_phase_2_ts"])):
                hit = r
        return hit

    def _active_1d_edge_at(self, T, direction):
        """Point-in-time edge of the 1D range active at T (band as known then,
        via band_history); None if no settled band yet."""
        r = self.range_active_at("1d", T)
        if not r:
            return None
        band = None
        for h in (r.get("band_history") or []):
            if pd.Timestamp(h["ts"]) <= pd.Timestamp(T):
                band = h
            else:
                break
        if band is None:
            return None
        return band["rl_l"] if direction == "short" else band["rh_u"]

    @staticmethod
    def _band_pct(r, price):
        ll, hu = r["range_low_lower"], r["range_high_upper"]
        return (price - ll) / (hu - ll) if hu > ll else 0.0

    def _alignment(self, direction, entry_price, entry_ts):
        ctx = {"1d_band_position": None, "1w_band_position": None, "alignment_score": 0.0}
        flags = []
        for tf, key in (("1d", "1d_band_position"), ("1w", "1w_band_position")):
            r = self.range_active_at(tf, entry_ts)
            if not r:
                continue
            pct = self._band_pct(r, entry_price)
            ctx[key] = round(pct, 3)
            flags.append(1.0 if ((direction == "short") == (pct <= 0.5)) else 0.0)
        ctx["alignment_score"] = round(float(np.mean(flags)), 3) if flags else 0.0
        return ctx

    def _pick_tp(self, tf, direction, entry, sl, T, pit):
        risk = abs(entry - sl)
        if risk <= 0:
            return None, None
        T_ns = _ns(T)
        cands = []
        for L2, ets2, ka2 in self.lev[tf]:
            p = L2["price"]
            if direction == "short" and not p < entry:
                continue
            if direction == "long" and not p > entry:
                continue
            if pit:
                if ka2 is None or ka2 > T_ns:        # level not yet KNOWABLE
                    continue
                cls, _ = self.pit_strength(L2, ets2, T_ns, tf)
            else:
                cls = L2.get("strength_class")
            if cls == "strong":
                cands.append(p)
        edge = self._active_1d_edge_at(T, direction)
        if edge is not None and ((direction == "short" and edge < entry) or
                                 (direction == "long" and edge > entry)):
            cands.append(edge)
        valid = []
        for tp in cands:
            rr = (entry - tp) / risk if direction == "short" else (tp - entry) / risk
            if MIN_RR <= rr <= MAX_RR:
                valid.append((tp, rr))
        if not valid:
            return None, None
        tp, rr = min(valid, key=lambda x: abs(x[0] - entry))
        return float(tp), round(float(rr), 3)

    def _simulate(self, tf, direction, entry_idx, entry, sl, tp):
        h, l, c = self.bars[tf]["h"], self.bars[tf]["l"], self.bars[tf]["c"]
        n = len(c); risk = abs(entry - sl); mfe = 0.0
        for k in range(entry_idx + 1, n):
            if direction == "short":
                if h[k] >= sl:
                    return sl, "sl", k
                if l[k] <= tp:
                    return tp, "tp", k
                fav = entry - l[k]                       # favorable excursion (down)
            else:
                if l[k] <= sl:
                    return sl, "sl", k
                if h[k] >= tp:
                    return tp, "tp", k
                fav = h[k] - entry
            if fav > mfe:
                mfe = fav
            # cut a trade that hasn't moved >= PROGRESS_R in our favor by now
            if (k - entry_idx) >= TIME_STOP_BARS and (mfe / risk) < PROGRESS_R:
                return float(c[k]), "time_stop", k
        return float(c[-1]), "open_eod", n - 1

    # -- candidate generation ---------------------------------------------
    def generate(self, tf, pit=True):
        out = []
        n = len(self.bars[tf]["c"])
        for L, ets, ka in self.lev[tf]:
            role = L.get("initial_role")
            if role == "resistance":
                direction = "short"
            elif role == "support":
                direction = "long"
            else:
                continue
            lvl = L["price"]
            for e in L["events"]:
                if e["type"] != "rejected":
                    continue
                T = e["ts"]; T_ns = _ns(T)
                if pit and ka is not None and T_ns < ka:
                    continue                 # level not yet forward-only knowable
                if pit:
                    cls, _ = self.pit_strength(L, ets, T_ns, tf)
                else:
                    cls = L.get("strength_class")
                if cls not in ("strong", "weak"):
                    continue
                ei = self.idx_of(tf, T)
                if ei < 0 or ei >= n - 1:
                    continue
                entry = float(e["bar_close"])
                if direction == "short":
                    sl = max(lvl * (1 + SL_BAND_MULT * TOUCH_BAND_PCT), float(e["bar_high"]))
                else:
                    sl = min(lvl * (1 - SL_BAND_MULT * TOUCH_BAND_PCT), float(e["bar_low"]))
                if (direction == "short" and sl <= entry) or (direction == "long" and sl >= entry):
                    continue
                tp, rr = self._pick_tp(tf, direction, entry, sl, T, pit)
                if tp is None:
                    continue
                exit_px, outcome, xidx = self._simulate(tf, direction, ei, entry, sl, tp)
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
                    "cascade_context": self._alignment(direction, entry, T),
                    "exit_ts": pd.Timestamp(self.bars[tf]["ts"][xidx]).isoformat(),
                    "exit_price": round(exit_px, 4), "outcome": outcome,
                    "r_gross": round(float(gross_R), 4),
                    "r_net": round(float(gross_R - fee_R), 4),
                })
        return out

    def run(self, pit=True, save=True):
        result = {"version": 1, "setup": "retest_then_fail", "point_in_time": pit,
                  "params": {"touch_band_pct": TOUCH_BAND_PCT, "sl_band_mult": SL_BAND_MULT,
                             "min_rr": MIN_RR, "time_stop_bars": TIME_STOP_BARS, "fee": FEE},
                  "by_tf": {}}
        all_c = []
        for tf in TFS:
            c = self.generate(tf, pit=pit)
            all_c += c
            result["by_tf"][tf] = _stats(c)
        result["candidates"] = all_c
        if save:
            tag = "pit" if pit else "naive"
            p = ROOT / f"backtesting/results/layer4_retest_fail_{tag}.json"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(result, indent=2), encoding="utf-8")
        return result


def _stats(cands):
    sim = [c for c in cands if c["outcome"] in ("tp", "sl", "time_stop", "open_eod")]
    n = len(sim)
    if n == 0:
        return {"n": 0}
    rn = np.array([c["r_net"] for c in sim]); wins = rn > 0
    return {
        "n": n, "n_tp": sum(c["outcome"] == "tp" for c in sim),
        "n_sl": sum(c["outcome"] == "sl" for c in sim),
        "n_ts": sum(c["outcome"] == "time_stop" for c in sim),
        "n_open": sum(c["outcome"] == "open_eod" for c in sim),
        "win_rate": round(float(wins.mean()), 4), "avg_R": round(float(rn.mean()), 4),
        "sum_R": round(float(rn.sum()), 4),
        "avg_win_R": round(float(rn[wins].mean()), 4) if wins.any() else 0.0,
        "avg_loss_R": round(float(rn[~wins].mean()), 4) if (~wins).any() else 0.0,
    }


def _print(res, label):
    print(f"\n[layer4 retest_fail | {label}] net R per TF:")
    for tf in TFS:
        s = res["by_tf"][tf]
        if s.get("n", 0) == 0:
            print(f"  {tf}: 0 candidates"); continue
        print(f"  {tf}: n={s['n']:>4} WR={s['win_rate']:.0%} avgR={s['avg_R']:+.3f} "
              f"sumR={s['sum_R']:+.1f} (tp={s['n_tp']} sl={s['n_sl']} ts={s['n_ts']} open={s['n_open']})")
    tot = _stats(res["candidates"])
    print(f"  ALL: n={tot['n']} WR={tot['win_rate']:.0%} avgR={tot['avg_R']:+.3f} sumR={tot['sum_R']:+.1f}")


if __name__ == "__main__":
    from shared.memhygiene import install        # Memory hygiene: see MEMORY_HYGIENE.md
    install("layer4_retest_fail")
    from data.freshness_monitor import consumer_startup   # Freshness contract: see MEMORY_HYGIENE.md
    consumer_startup(end_consumer=True)                    # auto-fetch + cascade regen (--no-freshness / --no-regen)
    warn_if_windowed()                                     # service worker may have windowed the shared artifacts
    bt = Layer4()
    _print(bt.run(pit=True), "POINT-IN-TIME (honest)")
    _print(bt.run(pit=False), "naive (look-ahead, for comparison)")
