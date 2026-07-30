"""
detectors/range_detector_core.py — generic, TF-agnostic range detector.

Shared layer-1 detection logic for the multi-TF cascade
    1W -> 1D -> 4H -> 1H -> 15m -> 5m -> 1m
Each TF runs the SAME logic with TF-specific config (thin wrappers in
range_detector_<tf>.py). Layers 2-4 not built.

Logic (from the calibrated v3.1 "asymmetric confirmation" build)
----------------------------------------------------------------
* Impulse leg from the BOS engine: down -> leg_start=latest_LH, leg_end=
  breakdown low; up -> leg_start=latest_HL, leg_end=breakout high.
* Liquidation-wick override on leg_end (first green/red close after a
  spike, else fallback).
* Asymmetric confirmation: the impulse-side band is predefined at leg_end
  (phase 1); the opposite band is pending until price retraces into the
  retrace zone (arming) then a close re-enters the consolidation body
  (close back above projected range_low_upper for up / below projected
  range_high_lower for down) with >= min_pending_closes in the window.
* Cluster bands (top/bottom band_zone_pct of the close range in a rolling
  window from leg_end); wicks excluded. Inside close -> extend; close
  beyond edge that recovers within recovery_lookahead -> failed_break;
  no recovery -> range end (breakout_up / breakdown_down).
* One range active at a time; absorbed BOS logged.
* Phase-1 outcomes: confirmed | timeout (max_pending_bars) | phase_1_failed
  (predefined-edge break / forming-floor collapse w/o recovery).

All timing is in BARS of the detection TF (max_pending_bars,
recovery_lookahead) so the same config shape works on every TF.

FORWARD-LOOK CONTRACT (recovery_lookahead) — read before any backtest
---------------------------------------------------------------------
Detection is forward-only EXCEPT one bounded, intentional window: the
recovery_lookahead false-break tolerance. When a close breaks the band the
engine looks ahead up to recovery_lookahead bars to see whether price recovers
(failed_break) before declaring a confirm or an end. So, for any point-in-time
/ live consumer:
  * A range's END is reliably KNOWABLE only at end + recovery_lookahead (the
    full window must pass to rule out a recovery). range_end_ts is the
    STRUCTURAL bar; the knowable bar is later.
  * A range's CONFIRM can lag up to recovery_lookahead bars too, when a
    pre-confirm forming-break's recovery resolves after the confirm bar.
  * Hence a LEVEL emitted by an ended range is knowable only at
    range_end_ts + recovery_lookahead. compute_known_at.py publishes this as
    range_end_known_ts; layer-5 levels carry level_available_ts; Layer 4 gates
    triggers on level_available_ts, NEVER on source_end_ts.
This is the ONLY forward-look in the detector — verified by
detectors/replay_validate.py (0 retracted / 0 backdated confirms; see
MEMORY_HYGIENE.md for the run protocol).

Cascade hooks (schema reserved, NOT computed here):
  timeframe, parent_range_id, child_range_ids, cleanness_metrics{...}.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Optional
import json
import sys

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# The analyzer (wick-based original vs close-based sibling) is chosen
# per-TF inside detect_ranges via cfg.use_close_based_extremes. High TFs
# (1W/1D) use close-based so liquidation wicks don't set locked extremes;
# 4H/1H keep the original wick-based engine (close-based degrades the
# approved 4H ranges). v1.4 / bos_retest / parity import the original
# directly and are unaffected either way.


# --------------------------------------------------------------------- config

@dataclass
class CoreRangeConfig:
    timeframe: str = "4h"                 # label only
    # Liquidation wick
    wick_multiplier:    float = 2.0
    wick_lookback_bars: int   = 20
    accepted_lookahead: int   = 6
    # Retracement trigger / arming
    retrace_low:  float = 0.725
    retrace_high: float = 0.750
    # Band computation
    band_zone_pct: float = 0.25
    # Range end (confirmed phase) — bars of the detection TF
    recovery_lookahead: int = 12
    # Phase-1 pending — bars of the detection TF
    max_pending_bars: int = 24
    min_pending_closes: int = 3
    # BOS engine
    analyzer_reversal_pct: float = 0.035
    analyzer_init_bars:    int   = 20
    # Close-based extremes: True -> pivots/locked levels from closes
    # (liquidation wicks ignored; for 1W/1D). False -> original wick-based
    # engine (for 4H/1H). Default False = original behavior.
    use_close_based_extremes: bool = False
    # 1H EMA bias (metadata only)
    ema_fast: int = 12
    ema_slow: int = 21
    ema_just_flipped_bars: int = 6


def _empty_cleanness() -> dict:
    """Reserved cascade schema — populated later by the cleanness utility."""
    return {
        "n_distinct_high_touches": None,
        "n_distinct_low_touches": None,
        "failed_break_count": None,
        "n_bos_absorbed": None,
        "active_duration_bars": None,
        "time_inside_band_pct": None,
    }


# --------------------------------------------------------------------- helpers

def _wick_beyond_body(direction, o, h, l, c) -> float:
    if direction == "down":
        return max(0.0, min(o, c) - l)
    return max(0.0, h - max(o, c))


def _avg_prior_range(high, low, end_excl, lb) -> float:
    start = max(0, end_excl - lb)
    if start >= end_excl:
        return float("nan")
    return float(np.mean(high[start:end_excl] - low[start:end_excl]))


def _first_accepted(direction, open_, close, spike_idx, lookahead, n):
    for k in range(spike_idx + 1, min(n, spike_idx + 1 + lookahead)):
        if direction == "down" and close[k] > open_[k]:
            return k, float(close[k])
        if direction == "up" and close[k] < open_[k]:
            return k, float(close[k])
    return None, None


def _cluster_band(closes, zone):
    mn = min(closes); mx = max(closes)
    rng = mx - mn
    if rng <= 0:
        return mn, mn, mx, mx
    bottom_hi = mn + rng * zone
    top_lo = mx - rng * zone
    bottom = [c for c in closes if c <= bottom_hi]
    top = [c for c in closes if c >= top_lo]
    return min(bottom), max(bottom), min(top), max(top)


def _compute_bias_ema(df_bias, cfg):
    closes = df_bias["close"].to_numpy(float)
    ef = pd.Series(closes).ewm(span=cfg.ema_fast, adjust=False).mean().to_numpy()
    es = pd.Series(closes).ewm(span=cfg.ema_slow, adjust=False).mean().to_numpy()
    return df_bias["timestamp"].to_numpy(dtype="int64"), ef > es


def _bias_state_at(ts_iso, ts_ms, green, just_flipped_bars):
    q = pd.Timestamp(ts_iso).value // 1_000_000
    pos = int(np.searchsorted(ts_ms, q, side="right") - 1)
    if pos < 0:
        return "unknown"
    cur = bool(green[pos])
    lo = max(0, pos - just_flipped_bars)
    for k in range(pos, lo, -1):
        if bool(green[k]) != bool(green[k - 1]):
            return "just_flipped"
    return "green" if cur else "red"


def _json_clean(o):
    if isinstance(o, dict):
        return {k: _json_clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_json_clean(x) for x in o]
    if isinstance(o, np.bool_):
        return bool(o)
    if isinstance(o, np.integer):
        return int(o)
    if isinstance(o, np.floating):
        f = float(o); return None if np.isnan(f) else f
    if isinstance(o, float) and np.isnan(o):
        return None
    return o


def save_json(output, path):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_json_clean(output), indent=2), encoding="utf-8")


# --------------------------------------------------------------------- record

@dataclass
class RangeRecord:
    range_id: str
    timeframe: str
    impulse_direction: str
    leg_start_ts: str
    leg_start_price: float
    leg_end_ts: str
    leg_end_price_raw: float
    leg_end_price_accepted: Optional[float]
    wick_overridden: bool
    accepted_offset_pct: Optional[float]
    fallback_used: bool
    range_phase_1_ts: str
    range_phase_2_ts: Optional[str]
    is_confirmed: bool
    phase_1_outcome: str
    phase_1_band: dict = field(default_factory=dict)
    phase_2_band: Optional[dict] = None
    range_low_lower: Optional[float] = None
    range_low_upper: Optional[float] = None
    range_high_lower: Optional[float] = None
    range_high_upper: Optional[float] = None
    range_mid: Optional[float] = None
    band_history: list = field(default_factory=list)
    failed_break_events: list = field(default_factory=list)
    bos_inside_range: list = field(default_factory=list)
    metadata_at_creation: dict = field(default_factory=dict)
    metadata_current: dict = field(default_factory=dict)
    range_end_ts: Optional[str] = None
    range_end_reason: Optional[str] = None
    # --- cascade hooks (reserved; populated by later utilities) ---
    parent_range_id: Optional[str] = None
    child_range_ids: list = field(default_factory=list)
    cleanness_metrics: dict = field(default_factory=_empty_cleanness)


# --------------------------------------------------------------- pending phase

def simulate_pending(b_idx, direction, leg_start, *, high, low, close, ts_col,
                     n, cfg):
    """Phase-1 state machine. Timeout measured in BARS (max_pending_bars)."""
    zone = cfg.band_zone_pct
    leg_end_idx = b_idx
    armed = False
    j = b_idx
    while j < n:
        if direction == "up" and high[j] > high[leg_end_idx]:
            leg_end_idx = j
        elif direction == "down" and low[j] < low[leg_end_idx]:
            leg_end_idx = j

        win = [float(close[k]) for k in range(leg_end_idx, j + 1)]
        rll, rlu, rhl, rhu = _cluster_band(win, zone)
        if direction == "up":
            predefined = (rhl, rhu); projected = (rll, rlu)
            ext = float(high[leg_end_idx])
            if low[j] <= ext - (ext - leg_start) * cfg.retrace_low:
                armed = True
        else:
            predefined = (rll, rlu); projected = (rhl, rhu)
            ext = float(low[leg_end_idx])
            if high[j] >= ext + (leg_start - ext) * cfg.retrace_low:
                armed = True
        range_mid = ((predefined[0] + predefined[1]) / 2
                     + (projected[0] + projected[1]) / 2) / 2

        if (j - b_idx) > cfg.max_pending_bars:
            return dict(outcome="timeout", leg_end_idx=leg_end_idx, end_idx=j,
                        predefined_band=predefined, projected_band=projected,
                        range_mid=range_mid, armed=armed)

        win_count = j - leg_end_idx + 1
        if armed and win_count >= cfg.min_pending_closes:
            if direction == "up" and close[j] > projected[1]:
                return dict(outcome="confirmed", leg_end_idx=leg_end_idx,
                            confirm_idx=j, predefined_band=predefined,
                            projected_band=projected, range_mid=range_mid, armed=armed)
            if direction == "down" and close[j] < projected[0]:
                return dict(outcome="confirmed", leg_end_idx=leg_end_idx,
                            confirm_idx=j, predefined_band=predefined,
                            projected_band=projected, range_mid=range_mid, armed=armed)
        if armed and j > leg_end_idx:
            prior = [float(close[k]) for k in range(leg_end_idx, j)]
            if prior:
                p_rll, p_rlu, p_rhl, p_rhu = _cluster_band(prior, zone)
                broke = (close[j] < p_rll) if direction == "up" else (close[j] > p_rhu)
                if broke:
                    rec_end = min(n, j + 1 + cfg.recovery_lookahead)
                    if direction == "up":
                        recovered = any(close[k] >= p_rll for k in range(j + 1, rec_end))
                    else:
                        recovered = any(close[k] <= p_rhu for k in range(j + 1, rec_end))
                    if not recovered:
                        return dict(outcome="phase_1_failed", leg_end_idx=leg_end_idx,
                                    end_idx=j, predefined_band=predefined,
                                    projected_band=projected, range_mid=range_mid,
                                    armed=armed)
        j += 1
    return dict(outcome="timeout", leg_end_idx=leg_end_idx, end_idx=n - 1,
                predefined_band=predefined, projected_band=projected,
                range_mid=range_mid, armed=armed)


# ------------------------------------------------------------- confirmed phase

def simulate_confirmed(leg_end_idx, confirm_idx, *, close, low, high, ts_col,
                       n, cfg):
    zone = cfg.band_zone_pct
    window = list(range(leg_end_idx, confirm_idx + 1))
    band = _cluster_band([float(close[k]) for k in window], zone)
    history = [(confirm_idx, band)]
    failed = []
    end_idx = None; end_reason = None
    j = confirm_idx + 1
    while j < n:
        rll, rlu, rhl, rhu = band
        c = float(close[j])
        if rll <= c <= rhu:
            window.append(j)
            band = _cluster_band([float(close[k]) for k in window], zone)
            history.append((j, band)); j += 1; continue
        br_dir = "up" if c > rhu else "down"
        recovered_at = None
        for k in range(j + 1, min(n, j + 1 + cfg.recovery_lookahead)):
            if rll <= float(close[k]) <= rhu:
                recovered_at = k; break
        if recovered_at is not None:
            for m in range(j, recovered_at + 1):
                window.append(m)
            band = _cluster_band([float(close[k]) for k in window], zone)
            history.append((recovered_at, band))
            failed.append((j, br_dir, c, recovered_at - j))
            j = recovered_at + 1
        else:
            end_idx = j
            end_reason = "breakout_up" if br_dir == "up" else "breakdown_down"
            break
    if end_idx is None:
        end_idx = n - 1; end_reason = None
    return history, failed, end_idx, end_reason, band, window


# --------------------------------------------------------------------- detect

def detect_ranges(df, df_bias, cfg: CoreRangeConfig):
    """Run the detector on `df` (detection TF). `df_bias` supplies the 1H
    EMA bias metadata. Returns (output_dict, list[RangeRecord])."""
    df = df.sort_values("timestamp").reset_index(drop=True)
    if cfg.use_close_based_extremes:
        from shared.structure_analyzer_close_based import StructureAnalyzer as _Analyzer
    else:
        from shared.structure_analyzer import StructureAnalyzer as _Analyzer
    a = _Analyzer(reversal_threshold_pct=cfg.analyzer_reversal_pct,
                  init_bars=cfg.analyzer_init_bars)
    states = a.analyze(df)
    high = df["high"].to_numpy(float); low = df["low"].to_numpy(float)
    open_ = df["open"].to_numpy(float); close = df["close"].to_numpy(float)
    ts_col = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    n = len(df)
    ts_ms_b, green_b = _compute_bias_ema(df_bias, cfg)

    bos = [(i, s.last_event) for i, s in enumerate(states)
           if s.last_bos_idx == i and s.last_event in ("bos_up", "bos_down")]

    def ema_at(idx):
        return _bias_state_at(ts_col.iloc[idx].isoformat(), ts_ms_b, green_b,
                              cfg.ema_just_flipped_bars)

    def leg_start_of(b_idx, direction):
        st = states[b_idx - 1] if b_idx >= 1 else states[b_idx]
        if direction == "down":
            v = st.latest_H_in_swing_price
            if v is None or (isinstance(v, float) and np.isnan(v)):
                v = st.swing_high
        else:
            v = st.latest_L_in_swing_price
            if v is None or (isinstance(v, float) and np.isnan(v)):
                v = st.swing_low
        return float(v)

    def wick_override(leg_end_idx, direction):
        raw = float(low[leg_end_idx]) if direction == "down" else float(high[leg_end_idx])
        wsz = _wick_beyond_body(direction, open_[leg_end_idx], high[leg_end_idx],
                                 low[leg_end_idx], close[leg_end_idx])
        avg = _avg_prior_range(high, low, leg_end_idx, cfg.wick_lookback_bars)
        thr = cfg.wick_multiplier * avg if not np.isnan(avg) else float("nan")
        if not ((not np.isnan(thr)) and wsz > thr):
            return raw, None, False, None, False
        ai, ap = _first_accepted(direction, open_, close, leg_end_idx,
                                 cfg.accepted_lookahead, n)
        if ai is None:
            return raw, None, False, None, True
        return raw, float(ap), True, abs(ap - raw) / raw, False

    def touches(lo_i, hi_i, lo_edge, hi_edge):
        cnt = 0; last = -10
        for j in range(lo_i, hi_i + 1):
            if low[j] <= hi_edge and high[j] >= lo_edge:
                if j - last >= 3:
                    cnt += 1; last = j
        return cnt

    ranges = []
    n_timeout = 0; n_failed = 0
    bnum = 0; scan_from = 0
    while bnum < len(bos):
        b_idx, kind = bos[bnum]
        if b_idx < scan_from:
            bnum += 1; continue
        direction = "down" if kind == "bos_down" else "up"
        leg_start = leg_start_of(b_idx, direction)
        pend = simulate_pending(b_idx, direction, leg_start, high=high, low=low,
                                close=close, ts_col=ts_col, n=n, cfg=cfg)
        leg_end_idx = pend["leg_end_idx"]
        raw, acc, overr, off, fb = wick_override(leg_end_idx, direction)
        pre = pend["predefined_band"]
        pre_dict = ({"range_high_lower": round(pre[0], 6), "range_high_upper": round(pre[1], 6)}
                    if direction == "up" else
                    {"range_low_lower": round(pre[0], 6), "range_low_upper": round(pre[1], 6)})

        common = dict(
            timeframe=cfg.timeframe, impulse_direction=direction,
            leg_start_ts=ts_col.iloc[(b_idx - 1) if b_idx >= 1 else b_idx].isoformat(),
            leg_start_price=round(leg_start, 6),
            leg_end_ts=ts_col.iloc[leg_end_idx].isoformat(),
            leg_end_price_raw=round(raw, 6),
            leg_end_price_accepted=(round(acc, 6) if acc is not None else None),
            wick_overridden=overr,
            accepted_offset_pct=(round(off, 6) if off is not None else None),
            fallback_used=fb,
            range_phase_1_ts=ts_col.iloc[leg_end_idx].isoformat(),
            phase_1_band=pre_dict,
        )

        if pend["outcome"] in ("timeout", "phase_1_failed"):
            end_idx = pend["end_idx"]
            if pend["outcome"] == "timeout": n_timeout += 1
            else: n_failed += 1
            ranges.append(RangeRecord(
                range_id=f"R{len(ranges):03d}_{cfg.timeframe}_{direction}_{ts_col.iloc[leg_end_idx].strftime('%Y%m%d')}_{pend['outcome']}",
                range_phase_2_ts=None, is_confirmed=False,
                phase_1_outcome=pend["outcome"],
                range_end_ts=ts_col.iloc[end_idx].isoformat(), range_end_reason=None,
                **common,
            ))
            scan_from = end_idx
            # ALWAYS advance past the BOS we just processed (nb = bnum+1),
            # then skip any further BOS before scan_from. Without the +1, a
            # window ending on a BOS bar yields end_idx == b_idx, scan_from
            # never exceeds b_idx, bnum stalls, and the outer loop re-appends
            # this range forever (unbounded memory). Byte-identical on full
            # data, where end_idx > b_idx always.
            nb = bnum + 1
            while nb < len(bos) and bos[nb][0] < scan_from:
                nb += 1
            bnum = nb
            continue

        confirm_idx = pend["confirm_idx"]
        history, failed, end_idx, end_reason, final_band, window = \
            simulate_confirmed(leg_end_idx, confirm_idx, close=close, low=low,
                               high=high, ts_col=ts_col, n=n, cfg=cfg)
        rll, rlu, rhl, rhu = final_band
        rmid = ((rll + rlu) / 2 + (rhl + rhu) / 2) / 2
        conf_dict = ({"range_low_lower": round(rll, 6), "range_low_upper": round(rlu, 6)}
                     if direction == "up" else
                     {"range_high_lower": round(rhl, 6), "range_high_upper": round(rhu, 6)})

        def meta(at_idx, end_for_active):
            wc = [float(close[k]) for k in range(leg_end_idx, end_for_active + 1)]
            return {
                "ema_1h_state": ema_at(at_idx),
                "n_4h_closes_in_range_low_band": int(sum(1 for c in wc if rll <= c <= rlu)),
                "n_4h_closes_in_range_high_band": int(sum(1 for c in wc if rhl <= c <= rhu)),
                "n_distinct_touches_of_range_low": touches(leg_end_idx, end_for_active, rll, rlu),
                "n_distinct_touches_of_range_high": touches(leg_end_idx, end_for_active, rhl, rhu),
                "time_from_leg_end_to_range_creation_bars": int(confirm_idx - leg_end_idx),
                "time_range_active_bars": int(end_for_active - confirm_idx),
            }

        inside = [{"ts": ts_col.iloc[bi].isoformat(), "kind": bk}
                  for (bi, bk) in bos if confirm_idx <= bi <= end_idx]

        ranges.append(RangeRecord(
            range_id=f"R{len(ranges):03d}_{cfg.timeframe}_{direction}_{ts_col.iloc[confirm_idx].strftime('%Y%m%d')}",
            range_phase_2_ts=ts_col.iloc[confirm_idx].isoformat(),
            is_confirmed=True, phase_1_outcome="confirmed", phase_2_band=conf_dict,
            range_low_lower=round(rll, 6), range_low_upper=round(rlu, 6),
            range_high_lower=round(rhl, 6), range_high_upper=round(rhu, 6),
            range_mid=round(rmid, 6),
            band_history=[{"ts": ts_col.iloc[bi].isoformat(),
                           "rl_l": round(bd[0], 6), "rl_u": round(bd[1], 6),
                           "rh_l": round(bd[2], 6), "rh_u": round(bd[3], 6)}
                          for (bi, bd) in history],
            failed_break_events=[{"ts": ts_col.iloc[fi].isoformat(), "direction": fd,
                                   "close": round(fc, 6), "recovered_within_bars": int(rb)}
                                  for (fi, fd, fc, rb) in failed],
            bos_inside_range=inside,
            metadata_at_creation=meta(confirm_idx, confirm_idx),
            metadata_current=meta(end_idx, end_idx),
            range_end_ts=(ts_col.iloc[end_idx].isoformat() if end_reason else None),
            range_end_reason=end_reason,
            **common,
        ))
        scan_from = end_idx
        # Always advance past the processed BOS (see note above) so a window
        # ending on a confirmed range's BOS bar can't stall the cursor.
        nb = bnum + 1
        while nb < len(bos) and bos[nb][0] < scan_from:
            nb += 1
        bnum = nb

    output = {
        "config": asdict(cfg), "layer": 1, "core_version": 1,
        "timeframe": cfg.timeframe,
        "n_bos_events": len(bos), "n_ranges_total": len(ranges),
        "n_confirmed": sum(1 for r in ranges if r.is_confirmed),
        "n_phase_1_timeout": n_timeout, "n_phase_1_failed": n_failed,
        "data_first_ts": ts_col.iloc[0].isoformat(),
        "data_last_ts": ts_col.iloc[-1].isoformat(),
        "ranges": [asdict(r) for r in ranges],
    }
    return output, ranges
