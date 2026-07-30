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

TREND_TFS = ["1d", "4h"]
LEVELS_PCT = 0.15               # include levels within +/-15% of reference
EVENT_TYPES = ("historical_level_touched", "rejected", "broken", "reclaimed")


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


def build_state(pair: str, state=None, live_price: float | None = None) -> dict:
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
        "recent_events_24h": _events(state, hours=24),
        # 7-day window feeds the chart's event markers (same event stream,
        # wider slice — no new analytics).
        "recent_events_7d": _events(state, hours=7 * 24),
    }


def write_state(pair: str, state=None, live_price: float | None = None) -> dict:
    doc = build_state(pair, state=state, live_price=live_price)
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
