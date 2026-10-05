"""
service/state_builder.py — serialize one pair's market state to state.json.

A THIN serialization over detectors/query_state.State — no new analytics.
Everything here is a re-projection of fields the locked layers already
publish:

  trend (1d/4h, v1):  the latest layer-1 range record's impulse_direction
      (the sign of the most recent BOS impulse leg — structure) combined
      with its metadata_current.ema_1h_state (the 1H EMA bias the detector
      already tracks). Both bullish -> "up", both bearish -> "down",
      disagreement or no data -> "neutral". basis: "structure+ema".

  regime (1d/4h, v1):  active range -> "ranging"; no active range but the
      last confirmed range ended less than recovery_lookahead bars ago (its
      end is not yet forward-KNOWN, per the FORWARD-LOOK CONTRACT) ->
      "transition"; otherwise "trending".

  active_ranges:  State.active_range + State.band_position per TF.
  levels:         State.near_levels (strong/weak) within +/-15% of the
                  reference price, all memory TFs, sorted by |distance|.
  recent_events_24h:  State.recent_events, all four event types.

The reference price for level distances is the live spot when the caller
passes one (the worker reads its own live.json), else the last 1H close.
"""
from __future__ import annotations
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pandas as pd

from detectors import paths
from service.ioutil import atomic_write_json
from service.pairs import WORKER_TFS, MEMORY_TFS, TF_MS
from shared.atr import compute_atr
from shared.pricefmt import round_price

TREND_TFS = ["1d", "4h"]
LEVELS_PCT = 0.15               # include levels within +/-15% of reference
EVENT_TYPES = ("historical_level_touched", "rejected", "broken", "reclaimed")

# --- Stage 9a: display zones (ATR-normalized merge over the SAME levels) ------
# Cross-TF precedence; mirrors the frontend TF_RANK in app.js (Stage 9-H3 will
# unify these via GET /api/config so the two can't silently drift).
TF_RANK = {"5m": 0, "1h": 1, "2h": 2, "4h": 3, "1d": 4, "1w": 5}
ZONE_TOL_MULT = 0.30            # tol_pct = 0.30 * atr_pct(1h), then clamped
ZONE_TOL_MIN, ZONE_TOL_MAX = 0.002, 0.008
ZONE_TOL_FALLBACK = 0.0035     # 1h history < 20 bars, or ATR NaN
ZONE_WEIGHT_FLOOR = 0.1        # strength_score floor for the weighted center
ZONE_PER_SIDE = 6              # selection budget per above/below side


def _ema_1h_state(state, tf: str) -> str | None:
    """CURRENT 1H EMA bias, recomputed from the raw 1H closes with the same
    ema_fast/ema_slow the detector's config uses. The l1 records' own
    metadata_current.ema_1h_state is frozen at each range's end, so with no
    active range it can be weeks stale — recomputing keeps the published
    trend honest without new analytics (same formula, same params)."""
    cfg = state._l1.get(tf, {}).get("config") or {}
    fast, slow = cfg.get("ema_fast"), cfg.get("ema_slow")
    if not fast or not slow:
        return None
    closes = pd.read_csv(paths.raw_csv("1h", state.pair), usecols=["close"])["close"]
    if len(closes) < slow * 3:
        return None
    tail = closes.tail(slow * 10)
    ef = tail.ewm(span=fast, adjust=False).mean().iloc[-1]
    es = tail.ewm(span=slow, adjust=False).mean().iloc[-1]
    return "green" if ef > es else "red"


def _trend(state, tf: str) -> str:
    ranges = state._l1.get(tf, {}).get("ranges") or []
    if not ranges:
        return "neutral"
    direction = ranges[-1].get("impulse_direction")      # latest BOS impulse: "up"/"down"
    ema = _ema_1h_state(state, tf)                       # "green" / "red" / None
    if direction == "up" and ema == "green":
        return "up"
    if direction == "down" and ema == "red":
        return "down"
    return "neutral"


def _regime(state, tf: str) -> str:
    if state.active_range(tf):
        return "ranging"
    l1 = state._l1.get(tf, {})
    ended = [r for r in l1.get("ranges") or []
             if r.get("is_confirmed") and r.get("range_end_ts")]
    if ended:
        last = max(ended, key=lambda r: r["range_end_ts"])
        # Prefer the published bar-indexed knowable time (compute_known_at);
        # fall back to wall-clock arithmetic only when the field is absent
        # (e.g. 5m, which skips the chain).
        if last.get("range_end_known_ts"):
            known_at = pd.Timestamp(last["range_end_known_ts"])
        else:
            rl = int(l1["config"]["recovery_lookahead"])
            known_at = pd.Timestamp(last["range_end_ts"]) + pd.Timedelta(
                milliseconds=rl * TF_MS[tf])
        if state.now < known_at:
            return "transition"          # end not yet forward-known
    return "trending"


def _active_range(state, tf: str):
    r = state.active_range(tf)
    bp = state.band_position(tf)
    if not r or not bp:
        return None
    return {
        "range_id": r["range_id"],
        "low": bp["low_band"],
        "high": bp["high_band"],
        "mid": r.get("range_mid"),
        "band_position_pct": round(bp["pct_of_range"] * 100, 1),
        "zone": bp["zone"],
    }


def _levels(state, ref_price: float):
    out = []
    for tf in MEMORY_TFS:
        for L in state.near_levels(tf, LEVELS_PCT):
            out.append({
                "tf": tf,
                "price": L["price"],
                "kind": "support" if L["price"] <= ref_price else "resistance",
                "strength": L["class"],
                "dist_pct_from_live": round((L["price"] - ref_price) / ref_price * 100, 3),
                "last_event": L["last_event"],
                "last_event_ts": L["last_event_ts"] or None,
                "source_range_id": L["source"],
            })
    out.sort(key=lambda x: abs(x["dist_pct_from_live"]))
    return out


def _events(state, hours=24):
    return [{
        "tf": e["tf"],
        "ts": e["ts"],
        "type": e["type"],
        "level_price": e["level_price"],
        "source_range_id": e["source_range_id"],
        "bar_close": e["bar_close"],
    } for e in state.recent_events(hours=hours, types=EVENT_TYPES)]


def _clamp(x, lo, hi):
    return max(lo, min(hi, x))


def _zone_tol_pct(pair: str, log=None) -> float:
    """Merge tolerance = clamp(0.30 * ATR14(1h)/last_1h_close, 0.2%, 0.8%).
    Wilder ATR via shared.atr on the 1h CSV tail(200) — an OWN high/low/close
    read, separate from the close-only _ema_1h_state read. Falls back to 0.35%
    (+ warn) when the 1h CSV has < 20 bars or ATR is NaN."""
    try:
        df = pd.read_csv(paths.raw_csv("1h", pair),
                         usecols=["high", "low", "close"]).tail(200)
        if len(df) < 20:
            raise ValueError(f"only {len(df)} 1h bars")
        atr_last = float(compute_atr(df, 14)[-1])
        close = float(df["close"].iloc[-1])
        atr_pct = atr_last / close if close else float("nan")
        if atr_pct != atr_pct:                          # NaN
            raise ValueError("ATR is NaN")
        return _clamp(ZONE_TOL_MULT * atr_pct, ZONE_TOL_MIN, ZONE_TOL_MAX)
    except Exception as e:
        (log or print)(f"[zones] {pair}: ATR tol fallback {ZONE_TOL_FALLBACK}: {e}")
        return ZONE_TOL_FALLBACK


def _collect_zone_levels(state):
    """Per-TF strong+weak levels with the fields the zone builder needs — a
    superset of what _levels() publishes (adds strength_score), collected
    separately so the byte-compatible `levels` field stays untouched."""
    out = []
    for tf in MEMORY_TFS:
        for L in state.near_levels(tf, LEVELS_PCT):
            out.append({
                "tf": tf, "price": L["price"], "score": L["score"],
                "class": L["class"], "source": L["source"],
                "last_event": L["last_event"],
                "last_event_ts": L["last_event_ts"] or None,
            })
    return out


def _emit_zone(members, ref_price: float) -> dict:
    prices = [m["price"] for m in members]
    wsum = sum(max(m["score"], ZONE_WEIGHT_FLOOR) for m in members)
    center = sum(m["price"] * max(m["score"], ZONE_WEIGHT_FLOOR)
                 for m in members) / wsum
    tfs = sorted({m["tf"] for m in members}, key=lambda t: -TF_RANK[t])
    evented = [m for m in members if m["last_event_ts"]]
    last = max(evented, key=lambda m: m["last_event_ts"]) if evented else None
    return {
        "price": round_price(center),
        "price_lo": min(prices),
        "price_hi": max(prices),
        "kind": "support" if center <= ref_price else "resistance",
        "tfs": tfs,
        "top_tf": tfs[0],
        "strength": "strong" if any(m["class"] == "strong" for m in members) else "weak",
        "score": round(max(m["score"] for m in members), 4),
        "confluence": len(members),
        "dist_pct_from_live": round((center - ref_price) / ref_price * 100, 3),
        "last_event": last["last_event"] if last else None,
        "last_event_ts": last["last_event_ts"] if last else None,
        "members": [{"tf": m["tf"], "price": m["price"], "score": m["score"],
                     "class": m["class"], "source_range_id": m["source"]}
                    for m in members],
    }


def _build_zones(levels, ref_price: float, tol_pct: float):
    """Greedy ascending merge: a level joins the current cluster iff its price is
    within tol_pct of the cluster's running MAX price; else it opens a new one.
    Deterministic (stable price sort)."""
    if not levels:
        return []
    ordered = sorted(levels, key=lambda x: x["price"])
    clusters, cur, cmax = [], [ordered[0]], ordered[0]["price"]
    for L in ordered[1:]:
        if cmax > 0 and (L["price"] - cmax) / cmax <= tol_pct:
            cur.append(L)
            cmax = L["price"]                           # ascending sort => new max
        else:
            clusters.append(cur)
            cur, cmax = [L], L["price"]
    clusters.append(cur)
    return [_emit_zone(c, ref_price) for c in clusters]


def _budget_zones(zones, ref_price: float, per_side: int = ZONE_PER_SIDE):
    """Keep up to `per_side` zones each side of ref, ranked by
    (TF_RANK[top_tf] desc, score desc); ALWAYS additionally include the single
    nearest zone per side even if it lost the ranking. Publish sorted by |dist|.
    The full pre-budget set is not published."""
    kept = []
    for side in ([z for z in zones if z["price"] > ref_price],
                 [z for z in zones if z["price"] <= ref_price]):
        if not side:
            continue
        keep = sorted(side, key=lambda z: (-TF_RANK[z["top_tf"]], -z["score"]))[:per_side]
        nearest = min(side, key=lambda z: abs(z["price"] - ref_price))
        if not any(z is nearest for z in keep):
            keep.append(nearest)
        kept.extend(keep)
    kept.sort(key=lambda z: abs(z["dist_pct_from_live"]))
    return kept


def _zones(state, ref_price: float, pair: str, log=None):
    tol = _zone_tol_pct(pair, log=log)
    zones = _build_zones(_collect_zone_levels(state), ref_price, tol)
    return _budget_zones(zones, ref_price)


def build_state(pair: str, state=None, live_price: float | None = None, log=None) -> dict:
    """Build the state dict. `state` may be a pre-built query_state.State
    (the worker passes its own); `live_price` overrides the level-distance
    reference (the worker passes the price from its live.json)."""
    if state is None:
        from detectors.query_state import State
        state = State(pair, live=False)
    ref = float(live_price) if live_price is not None else float(state.live_price)
    state.live_price = ref     # NB: mutates the caller's State so near_levels
                               # measures from ref (worker discards it after)
    return {
        "pair": pair,
        "generated_at": pd.Timestamp.now(tz="UTC").isoformat(timespec="seconds"),
        "last_bar_ts": state.now.isoformat(),
        "price_last_close_1h": state.price,
        "trend": {**{tf: _trend(state, tf) for tf in TREND_TFS},
                  "basis": "structure+ema"},
        "regime": {tf: _regime(state, tf) for tf in TREND_TFS},
        "active_ranges": {tf: (_active_range(state, tf) if tf in state._l1 else None)
                          for tf in WORKER_TFS},
        "levels": _levels(state, ref),
        # Stage 9a: ATR-normalized display zones over the same strong+weak
        # levels. ADDITIVE — `levels` above is unchanged; the chart/table read
        # `zones`, everything else still reads `levels`.
        "zones": _zones(state, ref, pair, log=log),
        "recent_events_24h": _events(state, hours=24),
        # 7-day window feeds the chart's event markers (same event stream,
        # wider slice — no new analytics).
        "recent_events_7d": _events(state, hours=7 * 24),
    }


def write_state(pair: str, state=None, live_price: float | None = None, log=None) -> dict:
    doc = build_state(pair, state=state, live_price=live_price, log=log)
    atomic_write_json(paths.state_json(pair), doc)
    return doc


if __name__ == "__main__":
    import argparse, json
    p = argparse.ArgumentParser()
    p.add_argument("--pair", default=paths.DEFAULT_PAIR)
    a = p.parse_args()
    doc = write_state(a.pair)
    print(json.dumps({k: doc[k] for k in
                      ("pair", "generated_at", "price_last_close_1h", "trend", "regime")},
                     indent=2))
    print(f"[state_builder] {a.pair}: {len(doc['levels'])} levels, "
          f"{len(doc['recent_events_24h'])} events -> {paths.state_json(a.pair)}")
