"""
detectors/layer5_range_memory.py — Layer 5 v1 (range memory).

UTILITY ONLY (no detection logic). Reads each TF's layer-1 output + the
cleanness_metrics already populated by compute_cleanness.py, then:

  (1) REGISTRY. A confirmed range that has ENDED and passes the cleanness
      threshold becomes "memory". Each qualifying range contributes TWO
      historical levels — its high edge (range_high_upper, was_high=True)
      and its low edge (range_low_lower, was_high=False) — tagged with the
      source range id / tf / end ts / end reason.

      Cleanness threshold (locked with the user):
          active_duration_bars >= 10
          AND (n_distinct_high_touches + n_distinct_low_touches) >= 4
          AND time_inside_band_pct >= 85

  (2) RETEST EVENTS. For every level we scan the SAME TF's bars after the
      source range ended and emit four event types as price interacts with
      the level (0.3% band):

        historical_level_touched  wick re-enters the 0.3% band from the
                                  hold side and the bar does not break or
                                  cleanly reject (a gentle test that held).
        rejected                  wick pierces BEYOND the band to the break
                                  side but the bar closes back on the hold
                                  side (a sweep / stop-hunt that was
                                  rejected — a strong hold).
        broken                    a close that DECISIVELY clears the level
                                  on the break side (beyond the 1% break
                                  buffer, not merely the 0.3% touch band).
        reclaimed                 after a break, a decisive close back
                                  across to the original hold side (the
                                  break failed and the level was taken
                                  back).

      The hold / break sides are set per level from where price sits one
      bar after the source range ends (price above => the level acts as
      SUPPORT, hold side = above; price below => RESISTANCE, hold = below).
      So a former range top that price broke up through is remembered as
      support, and a former range bottom that price broke down through is
      remembered as resistance — matching how flipped levels behave.

      HYSTERESIS (so one approach => one classified outcome, not a per-bar
      stream): a break/reclaim must clear a 1% buffer beyond the level — a
      close 0.3% past it is not a structural break. After ANY event the
      level goes dormant and only re-arms once price has moved >1.5% away,
      so a level that price hovers on for weeks logs a single outcome per
      genuine visit instead of one event every bar.

  (3) OUTPUT. Per-TF file detectors/results/range_memory_<tf>.json holding
      the level registry and a flat historical_level_events list. Kept as a
      separate file (not merged into the layer-1 json) so layer 5 re-runs
      independently and the large layer-1 outputs stay detection-only.
      Each level also carries summary rollups for layer 5.1 (cycle_count =
      complete break->reclaim round trips; total_touches / total_rejections
      / total_breaks / total_reclaims; last_event_ts / last_event_type) —
      pure counts over the event stream, so 5.1 reads the chop signal
      without recounting.

v1 deliberately reports each level's ORIGINAL structural role (the side it
held when first remembered); strong/weak classification and role-flip
relabelling are layer 5.1.
"""
from __future__ import annotations
import json, sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from detectors import paths

TFS = ["1w", "1d", "4h", "2h", "1h"]

TOUCH_BAND_PCT = 0.003          # +/-0.3% around the level price (touch test)
BREAK_BUFFER_PCT = 0.01         # close must clear +/-1% beyond to break/reclaim
REARM_PCT = 0.03                # price must move >3% away (structural, above
                                # daily candle noise) before a new event logs
CLEANNESS = {"min_active_duration_bars": 10,
             "min_total_touches": 4,
             "min_time_inside_band_pct": 85}


def _summarize(evs: list) -> dict:
    """Per-level rollups for layer 5.1 (no new logic; counts the stream).
    A complete cycle = a `broken` later closed by a `reclaimed`. Events
    alternate B,R,B,R... (the scanner only reclaims while broken), so the
    cycle count is the number of reclaims that follow an open break."""
    tt = sum(1 for e in evs if e["type"] == "historical_level_touched")
    trj = sum(1 for e in evs if e["type"] == "rejected")
    tbk = sum(1 for e in evs if e["type"] == "broken")
    trc = sum(1 for e in evs if e["type"] == "reclaimed")
    cyc = 0; pending = False
    for e in evs:
        if e["type"] == "broken":
            pending = True
        elif e["type"] == "reclaimed" and pending:
            cyc += 1; pending = False
    return {
        "cycle_count": cyc,
        "total_touches": tt, "total_rejections": trj,
        "total_breaks": tbk, "total_reclaims": trc,
        "last_event_ts": evs[-1]["ts"] if evs else None,
        "last_event_type": evs[-1]["type"] if evs else None,
    }


def _passes_cleanness(cm: dict) -> bool:
    if cm is None or cm.get("active_duration_bars") is None:
        return False
    return (cm["active_duration_bars"] >= CLEANNESS["min_active_duration_bars"]
            and (cm["n_distinct_high_touches"] + cm["n_distinct_low_touches"])
                >= CLEANNESS["min_total_touches"]
            and cm["time_inside_band_pct"] >= CLEANNESS["min_time_inside_band_pct"])


def _scan_level(price, end_idx, *, high, low, close, ts_iso, n):
    """Episode scan over bars (end_idx, n) for one level. `side` (hold side)
    is fixed from the first post-end bar; `broken` tracks whether the level
    is currently lost; `armed` gates one event per approach (re-arms only
    after price moves >REARM_PCT away). Returns the list of retest events."""
    th_hi = price * (1 + TOUCH_BAND_PCT); th_lo = price * (1 - TOUCH_BAND_PCT)
    bk_hi = price * (1 + BREAK_BUFFER_PCT); bk_lo = price * (1 - BREAK_BUFFER_PCT)
    start = end_idx + 1
    if start >= n:
        return []
    # Baseline orientation: above => support (hold above), below => resistance.
    side = "above" if close[start] >= price else "below"
    hold = side
    brk = "below" if side == "above" else "above"
    role = "support" if side == "above" else "resistance"
    broken = False
    armed = True
    events = []
    for j in range(start, n):
        c = float(close[j]); hi = float(high[j]); lo = float(low[j])
        if not armed:
            if abs(c - price) / price > REARM_PCT:   # price left the zone -> reset
                armed = True
            else:
                continue
        ev = None
        if broken:
            # reclaim = decisive close back beyond the buffer on the hold side
            if (hold == "above" and c > bk_hi) or (hold == "below" and c < bk_lo):
                ev = "reclaimed"; broken = False
        else:
            # break = decisive close beyond the buffer on the break side
            if (brk == "above" and c > bk_hi) or (brk == "below" and c < bk_lo):
                ev = "broken"; broken = True
            elif lo <= th_hi and hi >= th_lo:        # wick entered the touch band
                pierced = (lo < th_lo) if brk == "below" else (hi > th_hi)
                closed_hold = (c >= price) if hold == "above" else (c <= price)
                ev = "rejected" if (pierced and closed_hold) else "historical_level_touched"
        if ev is None:
            continue
        armed = False
        events.append({
            "ts": ts_iso[j],
            "type": ev,
            "role": role,
            "level_price": round(price, 6),
            "bar_close": round(c, 6),
            "bar_high": round(hi, 6),
            "bar_low": round(lo, 6),
            "dist_pct": round((c - price) / price, 5),
            "bars_since_source_end": int(j - end_idx),
        })
        last_soft = j
    return events


def build_for_tf(tf: str, pair: str = paths.DEFAULT_PAIR) -> dict:
    out = json.loads(paths.l1_json(tf, pair).read_text())
    df = (pd.read_csv(paths.raw_csv(tf, pair)).drop_duplicates("timestamp")
          .sort_values("timestamp").reset_index(drop=True))
    tsv = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    tsv_ns = tsv.values.astype("datetime64[ns]")
    ts_iso = [t.isoformat() for t in tsv]
    high = df["high"].to_numpy(float); low = df["low"].to_numpy(float)
    close = df["close"].to_numpy(float)
    n = len(df)

    def idx_of(ts_iso_str):
        return int(np.searchsorted(
            tsv_ns, np.datetime64(pd.Timestamp(ts_iso_str).tz_localize(None))))

    levels = []
    all_events = []
    for r in out["ranges"]:
        if not r["is_confirmed"] or not r["range_end_ts"]:
            continue                     # active or non-confirmed: not memory yet
        if not _passes_cleanness(r.get("cleanness_metrics")):
            continue
        end_idx = min(idx_of(r["range_end_ts"]), n - 1)
        for was_high, edge_price in ((True, r["range_high_upper"]),
                                     (False, r["range_low_lower"])):
            if edge_price is None:
                continue
            level_id = f"{r['range_id']}_{'HI' if was_high else 'LO'}"
            evs = _scan_level(float(edge_price), end_idx, high=high, low=low,
                              close=close, ts_iso=ts_iso, n=n)
            # FORWARD-ONLY knowable time. The level (born at the range's end) is
            # only certain once the recovery window passes: level_available_ts =
            # range_end_ts + recovery_lookahead (EXACT, from compute_known_at).
            # Each event's known_at is when it could first be ACTED on =
            # max(event ts, level_available_ts). Layer 4 gates on these, NOT on
            # source_end_ts. (range_end_known_ts is ISO same-format as ts, so the
            # string compare is a valid chronological compare.)
            level_available_ts = r.get("range_end_known_ts")
            for e in evs:
                e["known_at"] = (e["ts"] if (level_available_ts is None
                                 or e["ts"] >= level_available_ts) else level_available_ts)
            initial_role = None
            if end_idx + 1 < n:
                initial_role = "support" if close[end_idx + 1] >= edge_price else "resistance"
            lvl = {
                "level_id": level_id,
                "source_range_id": r["range_id"],
                "source_tf": tf,
                "was_high": was_high,
                "price": round(float(edge_price), 6),
                "source_end_ts": r["range_end_ts"],
                "source_end_reason": r["range_end_reason"],
                "level_available_ts": level_available_ts,   # source_end + recovery_lookahead (EXACT)
                "initial_role": initial_role,
                "n_events": len(evs),
                **_summarize(evs),
                "events": evs,
            }
            levels.append(lvl)
            for e in evs:
                all_events.append({
                    "level_id": level_id,
                    "source_range_id": r["range_id"],
                    "was_high": was_high,
                    **e,
                })

    by_type = {}
    for e in all_events:
        by_type[e["type"]] = by_type.get(e["type"], 0) + 1

    result = {
        "layer": 5, "version": 1, "timeframe": tf,
        "params": {
            "touch_band_pct": TOUCH_BAND_PCT,
            "break_buffer_pct": BREAK_BUFFER_PCT,
            "rearm_pct": REARM_PCT,
            "cleanness_threshold": CLEANNESS,
        },
        "data_first_ts": ts_iso[0] if n else None,
        "data_last_ts": ts_iso[-1] if n else None,
        "n_levels": len(levels),
        "n_events": len(all_events),
        "events_by_type": by_type,
        "levels": levels,
        "historical_level_events": all_events,
    }
    paths.mem_json(tf, pair).write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result


def run(tfs=None, verbose=True, pair=paths.DEFAULT_PAIR):
    tfs = tfs or TFS
    results = {}
    for tf in tfs:
        res = build_for_tf(tf, pair)
        results[tf] = res
        if verbose:
            bt = res["events_by_type"]
            print(f"[range_memory] {tf}: {res['n_levels']} levels  "
                  f"{res['n_events']} events  "
                  f"(touched={bt.get('historical_level_touched',0)} "
                  f"rejected={bt.get('rejected',0)} "
                  f"broken={bt.get('broken',0)} "
                  f"reclaimed={bt.get('reclaimed',0)})  -> {paths.mem_json(tf, pair)}")
    return results


if __name__ == "__main__":
    from shared.memhygiene import install        # Memory hygiene: see MEMORY_HYGIENE.md
    install("layer5_range_memory")
    from data.freshness_monitor import consumer_startup   # Freshness contract: see MEMORY_HYGIENE.md
    consumer_startup(end_consumer=False)                   # CSV refresh only (regen-chain step; --no-freshness to skip)
    run()
