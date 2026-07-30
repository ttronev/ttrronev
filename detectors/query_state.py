"""
detectors/query_state.py — trader-facing "what's structurally important near
current price?" query over the locked layers (L1 detection + L5 memory + L5.1
strength). READ-ONLY: no detection, no writes. A thin read API so the question
is answerable in a few lines:

    from detectors.query_state import State
    s = State()
    s.price                      # current price (last 1H close)
    s.active_range("1d")         # the live 1D range record (or None)
    s.band_position("1d")        # where price sits in that range
    s.near_levels("1d", pct=0.05)# strong/weak historical levels within +/-5%
    s.recent_events(hours=24)    # touched/rejected events in the last 24h

`python -m detectors.query_state` prints the full current-state report.
"""
from __future__ import annotations
import json, sys
from pathlib import Path
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from detectors import paths

TFS = ["1w", "1d", "4h", "2h", "1h"]
_L1 = lambda tf, pair=paths.DEFAULT_PAIR: paths.l1_json(tf, pair)
_MEM = lambda tf, pair=paths.DEFAULT_PAIR: paths.mem_json(tf, pair)
_CSV = lambda tf, pair=paths.DEFAULT_PAIR: paths.raw_csv(tf, pair)

# A layer-1 json bigger than this is a legacy full-history artifact (the 87MB
# 5m file) — skip it rather than blow the <500MB RAM target. The service's
# windowed regen keeps 5m output far below this.
_L1_MAX_BYTES = 30 * 1024 * 1024


class State:
    def __init__(self, pair: str = paths.DEFAULT_PAIR, live: bool = True):
        self.pair = pair
        self._l1 = {tf: json.loads(_L1(tf, pair).read_text()) for tf in TFS}
        self._mem = {tf: json.loads(_MEM(tf, pair).read_text()) for tf in TFS}
        # 5m has layer-1 only (no range memory); load it when present so
        # active_range("5m") / band_position("5m") work for the service.
        p5 = _L1("5m", pair)
        if p5.exists() and p5.stat().st_size <= _L1_MAX_BYTES:
            try:
                self._l1["5m"] = json.loads(p5.read_text())
            except Exception:
                pass
        h = (pd.read_csv(_CSV("1h", pair)).drop_duplicates("timestamp")
             .sort_values("timestamp"))
        self.price = float(h["close"].iloc[-1])          # last CLOSED 1H bar (what detectors consume)
        self.now = pd.to_datetime(h["timestamp"].iloc[-1], unit="ms", utc=True)
        # Live spot — DISPLAY + PROXIMITY only; falls back to last close if the
        # ticker fetch fails or live=False (offline / deterministic runs).
        self.live_price = self.price
        self.live_ok = False
        if live:
            try:
                from data.freshness_monitor import get_live_price
                lp = get_live_price(inst_id=paths.okx_inst_id(pair))
                if lp:
                    self.live_price = float(lp); self.live_ok = True
            except Exception:
                pass
        self.live_ts = pd.Timestamp.now(tz="UTC")    # snapshot/fetch time (seconds precision)

    # --- ranges -----------------------------------------------------------
    def confirmed(self, tf):
        return [r for r in self._l1[tf]["ranges"] if r["is_confirmed"]]

    def active_range(self, tf):
        """The live (confirmed, not-yet-ended) range on a TF, or None."""
        act = [r for r in self.confirmed(tf) if not r["range_end_ts"]]
        return act[-1] if act else None

    def bars_since(self, tf, ts_iso):
        df = pd.read_csv(_CSV(tf, self.pair), usecols=["timestamp"])
        q = pd.Timestamp(ts_iso).value // 1_000_000
        return int((df["timestamp"] >= q).sum()) - 1

    def band_position(self, tf):
        r = self.active_range(tf)
        if not r:
            return None
        ll, lu = r["range_low_lower"], r["range_low_upper"]
        hl, hu = r["range_high_lower"], r["range_high_upper"]
        p = self.price
        span = hu - ll
        pct = (p - ll) / span if span > 0 else 0.0
        if p < ll:
            zone = "BELOW range (broken low band)"
        elif p <= lu:
            zone = "in LOW band (support)"
        elif p < hl:
            zone = "mid-range"
        elif p <= hu:
            zone = "in HIGH band (resistance)"
        else:
            zone = "ABOVE range (broken high band)"
        return {"zone": zone, "pct_of_range": round(pct, 3),
                "low_band": [ll, lu], "high_band": [hl, hu]}

    # --- levels -----------------------------------------------------------
    def near_levels(self, tf, pct=0.05, classes=("strong", "weak")):
        ref = self.live_price                     # proximity measured from LIVE spot
        lo, hi = ref * (1 - pct), ref * (1 + pct)
        out = []
        for L in self._mem[tf]["levels"]:
            if L.get("strength_class") not in classes:
                continue
            if lo <= L["price"] <= hi:
                out.append({
                    "price": L["price"],
                    "dist_pct": round((L["price"] - ref) / ref, 4),
                    "class": L["strength_class"], "score": L["strength_score"],
                    "source": L["source_range_id"], "cycle_count": L["cycle_count"],
                    "last_event": L["last_event_type"],
                    "last_event_ts": L["last_event_ts"],   # full ISO; display code truncates
                })
        return sorted(out, key=lambda x: abs(x["dist_pct"]))

    # --- events -----------------------------------------------------------
    def recent_events(self, hours=24, types=("historical_level_touched", "rejected"),
                      tfs=None):
        cutoff = self.now - pd.Timedelta(hours=hours)
        out = []
        for tf in (tfs or TFS):
            for e in self._mem[tf]["historical_level_events"]:
                if e["type"] in types and pd.Timestamp(e["ts"]) >= cutoff:
                    out.append({"tf": tf, **e})
        return sorted(out, key=lambda x: x["ts"])

    def nests_in(self, child_id, parent_tf):
        r = self.active_range(parent_tf)
        return bool(r) and child_id in r["child_range_ids"]


def fmt_ts(ts):
    """YYYY-MM-DD HH:MM:SS UTC — full precision, no abbreviations."""
    return pd.Timestamp(ts).strftime("%Y-%m-%d %H:%M:%S UTC")


def _report(pair=paths.DEFAULT_PAIR):
    s = State(pair)
    print(f"{'='*70}\nCURRENT STATE  {pair}  -  {fmt_ts(s.live_ts)}\n{'='*70}")
    print(f"{paths.base_symbol(pair)} price (last 1H close): ${s.price:.2f}  (bar @ {fmt_ts(s.now)})")
    if s.live_ok:
        print(f"        live spot (NOW): ${s.live_price:.2f}  (fetched @ {fmt_ts(s.live_ts)})")
    else:
        print(f"        live spot: unavailable — using last 1H close")
    print()

    for tf in ("1d", "1w"):
        r = s.active_range(tf)
        if not r:
            print(f"[{tf.upper()}] no active range\n"); continue
        bp = s.band_position(tf)
        days = (s.now - pd.Timestamp(r["range_phase_2_ts"])).days
        bars = s.bars_since(tf, r["range_phase_2_ts"])
        print(f"[{tf.upper()}] ACTIVE {r['range_id']}")
        print(f"     band: low [{bp['low_band'][0]:.2f}, {bp['low_band'][1]:.2f}]  "
              f"high [{bp['high_band'][0]:.2f}, {bp['high_band'][1]:.2f}]")
        print(f"     confirmed {r['range_phase_2_ts'][:10]}  ({days}d / {bars} {tf} bars ago)")
        print(f"     price position: {bp['zone']}  ({bp['pct_of_range']:+.0%} of range height)")
        print(f"     children: {r['child_range_ids']}\n")

    # nesting check: is the active 1D range a child of the active 1W range?
    d = s.active_range("1d")
    if d:
        ok = s.nests_in(d["range_id"], "1w")
        print(f"NESTING: active 1D {d['range_id']} in active 1W child_range_ids? {ok}\n")

    print(f"{'-'*70}\nSTRONG/WEAK 1D levels within +/-5% of ${s.price:.2f}:")
    nd = s.near_levels("1d", 0.05)
    if not nd: print("   (none)")
    for L in nd:
        print(f"   ${L['price']:>7.2f} ({L['dist_pct']:+.1%})  {L['class']:<6} "
              f"src={L['source']:<26} cyc={L['cycle_count']} "
              f"last={L['last_event']}@{(L['last_event_ts'] or '')[:10]}")

    for tf in ("4h", "2h"):
        print(f"\nSTRONG/WEAK {tf.upper()} levels within +/-3% of ${s.price:.2f}:")
        nn = s.near_levels(tf, 0.03)
        if not nn: print("   (none)")
        for L in nn:
            print(f"   ${L['price']:>7.2f} ({L['dist_pct']:+.1%})  {L['class']:<6} "
                  f"src={L['source']:<28} cyc={L['cycle_count']} "
                  f"last={L['last_event']}@{(L['last_event_ts'] or '')[:10]}")

    print(f"\n{'-'*70}\nTouched/rejected events in the last 24h (all TFs):")
    ev = s.recent_events(24)
    if not ev: print("   (none)")
    for e in ev:
        print(f"   {e['ts'][:16]}  {e['tf']:>2}  {e['type']:<26} "
              f"level ${e['level_price']:.2f} ({e['source_range_id']})  close=${e['bar_close']:.2f}")


if __name__ == "__main__":
    import argparse
    from shared.memhygiene import install        # Memory hygiene: see MEMORY_HYGIENE.md
    install("query_state")
    _ap = argparse.ArgumentParser(add_help=False)
    _ap.add_argument("--pair", default=paths.DEFAULT_PAIR)
    _a, _ = _ap.parse_known_args()
    from data.freshness_monitor import consumer_startup   # Freshness contract: see MEMORY_HYGIENE.md
    consumer_startup(end_consumer=True, pair=_a.pair)      # auto-fetch + cascade regen (--no-freshness / --no-regen)
    _report(_a.pair)
