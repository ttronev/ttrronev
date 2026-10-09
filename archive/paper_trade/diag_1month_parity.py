"""Parity-divergence diagnostic for the 1-month SOL window.

Goal: identify which of three hypotheses explains the divergent events.

  H1: 5m buffer eviction shifts init_seed → different initial
      locked_sH / locked_sL → different BOS detection downstream.

  H2: 1h buffer eviction makes returns_pct[idx] for the same
      target 1h bar drift across runs → regime gate flips between
      calls for the same BOS bar.

  H3: init_seed and returns_pct both match, but the analyzer's
      in-swing pivot tracking (highest_H, latest_H, lowest_L,
      latest_L) accumulates differently when reseeded from a
      different start bar → different carryover at later BOSes.

The three are NOT mutually exclusive. Run all three checks on the
same data; report what matches.

Output: per-divergent-event row with the buffer state at emission,
the analyzer state at the relevant bar in both batch and streaming
modes, and an H1/H2/H3 classification.
"""

from __future__ import annotations

import json
import time
from collections import defaultdict
from dataclasses import asdict
from typing import Optional

import numpy as np
import pandas as pd

from backtesting.secondary_only_engine import SecondaryOnlyEngine, map_5m_to_1h_idx, _to_ns_int64
from paper_trade.bar_buffer import Bar, PairBuffers
from paper_trade.config import (
    BUFFER_5M, BUFFER_1H, ENGINE_CONFIG, MIN_5M_FOR_ENGINE, MIN_1H_FOR_REGIME,
)
from paper_trade.streaming_engine import StreamingEngine, _snapshot_candidate
from paper_trade.test_streaming_parity import _make_bar


# ---------------------------------------------------------------------
# Analyzer state capture (re-implements the analyzer to expose per-bar
# state, including pivot trackers, that the public API doesn't surface).
# Any change here should be a faithful re-implementation of
# StructureAnalyzer.analyze().
# ---------------------------------------------------------------------

def trace_analyzer_state(df_5m: pd.DataFrame, target_ts: pd.Timestamp,
                         reversal_pct: float = 0.005,
                         init_bars: int = 50) -> Optional[dict]:
    """Run the analyzer on df_5m and return state at the bar at
    target_ts (PRE-BOS-event resolution — i.e. the locked levels and
    pivot trackers before the BOS at target_ts fires)."""
    n = len(df_5m)
    if n == 0:
        return None
    high = df_5m["high"].to_numpy(dtype=float)
    low = df_5m["low"].to_numpy(dtype=float)
    close = df_5m["close"].to_numpy(dtype=float)
    ts = df_5m["timestamp"].to_numpy()
    target_idx = -1
    for i in range(n):
        if pd.Timestamp(ts[i]) == target_ts:
            target_idx = i
            break
    if target_idx < 0:
        return None

    init_n = min(init_bars, n)
    locked_sH = float(np.max(high[:init_n])); locked_sH_idx = int(np.argmax(high[:init_n]))
    locked_sL = float(np.min(low[:init_n])); locked_sL_idx = int(np.argmin(low[:init_n]))
    state = "undetermined"
    zz_dir = 0
    active_high_price = float(high[0]); active_high_idx = 0
    active_low_price = float(low[0]); active_low_idx = 0
    tracking_LL = float("inf"); tracking_HH = float("-inf")
    highest_H_price = float("-inf"); highest_H_idx = -1
    lowest_L_price = float("inf"); lowest_L_idx = -1
    latest_H_price = float("nan"); latest_H_idx = -1
    latest_L_price = float("nan"); latest_L_idx = -1
    sH_ratchet_pending = False; sL_ratchet_pending = False

    for i in range(n):
        bh = float(high[i]); bl = float(low[i]); bc = float(close[i])

        if zz_dir in (0, +1) and bh > active_high_price:
            active_high_price = bh; active_high_idx = i
        if zz_dir in (0, -1) and bl < active_low_price:
            active_low_price = bl; active_low_idx = i

        new_pivot = None
        if zz_dir in (0, +1) and active_high_price > 0 and bc < active_high_price * (1.0 - reversal_pct):
            new_pivot = ("H", active_high_idx, active_high_price)
            active_low_price = bl; active_low_idx = i; zz_dir = -1
        if new_pivot is None and zz_dir in (0, -1) and active_low_price > 0 and bc > active_low_price * (1.0 + reversal_pct):
            new_pivot = ("L", active_low_idx, active_low_price)
            active_high_price = bh; active_high_idx = i; zz_dir = +1
        if new_pivot is not None:
            kind, p_idx, p_price = new_pivot
            if kind == "H":
                if p_price > highest_H_price:
                    highest_H_price = p_price; highest_H_idx = p_idx
                latest_H_price = p_price; latest_H_idx = p_idx
                if sH_ratchet_pending and p_price > locked_sH:
                    locked_sH = p_price; locked_sH_idx = p_idx
                if sH_ratchet_pending: sH_ratchet_pending = False
            else:
                if p_price < lowest_L_price:
                    lowest_L_price = p_price; lowest_L_idx = p_idx
                latest_L_price = p_price; latest_L_idx = p_idx
                if sL_ratchet_pending and p_price < locked_sL:
                    locked_sL = p_price; locked_sL_idx = p_idx
                if sL_ratchet_pending: sL_ratchet_pending = False

        if state in ("downtrend", "undetermined") and bl < tracking_LL:
            tracking_LL = bl
        if state in ("uptrend", "undetermined") and bh > tracking_HH:
            tracking_HH = bh

        # When we reach the target bar, capture state PRE-BOS.
        if i == target_idx:
            return {
                "state": state,
                "locked_sH": locked_sH,
                "locked_sH_ts": str(pd.Timestamp(ts[locked_sH_idx])) if locked_sH_idx >= 0 else None,
                "locked_sL": locked_sL,
                "locked_sL_ts": str(pd.Timestamp(ts[locked_sL_idx])) if locked_sL_idx >= 0 else None,
                "highest_H": highest_H_price if highest_H_price != float("-inf") else None,
                "highest_H_ts": str(pd.Timestamp(ts[highest_H_idx])) if highest_H_idx >= 0 else None,
                "latest_H": latest_H_price if not np.isnan(latest_H_price) else None,
                "latest_H_ts": str(pd.Timestamp(ts[latest_H_idx])) if latest_H_idx >= 0 else None,
                "lowest_L": lowest_L_price if lowest_L_price != float("inf") else None,
                "lowest_L_ts": str(pd.Timestamp(ts[lowest_L_idx])) if lowest_L_idx >= 0 else None,
                "latest_L": latest_L_price if not np.isnan(latest_L_price) else None,
                "latest_L_ts": str(pd.Timestamp(ts[latest_L_idx])) if latest_L_idx >= 0 else None,
                "sH_ratchet_pending": sH_ratchet_pending,
                "sL_ratchet_pending": sL_ratchet_pending,
                "init_seed_sH": float(np.max(high[:init_n])),
                "init_seed_sL": float(np.min(low[:init_n])),
                "init_seed_first_ts": str(pd.Timestamp(ts[0])),
                "init_seed_last_ts": str(pd.Timestamp(ts[init_n - 1])),
                "n_bars_processed": i + 1,
                "bar_close": bc,
                "bar_high": bh,
                "bar_low": bl,
            }

        # Apply BOS resolution after capturing state (so future bars
        # are processed correctly if needed — but we return at target).
        event = "none"
        if bc > locked_sH and not sH_ratchet_pending:
            if state == "downtrend":
                new_sL = latest_L_price if not np.isnan(latest_L_price) else bl
                new_sL_idx = latest_L_idx if latest_L_idx >= 0 else i
                state = "uptrend"
            elif state == "uptrend":
                new_sL = lowest_L_price if lowest_L_price != float("inf") else locked_sL
                new_sL_idx = lowest_L_idx if lowest_L_idx >= 0 else locked_sL_idx
            else:
                new_sL = locked_sL; new_sL_idx = locked_sL_idx; state = "uptrend"
            locked_sH = bc; locked_sH_idx = i
            locked_sL = new_sL; locked_sL_idx = new_sL_idx
            tracking_LL = float("inf"); tracking_HH = bh
            highest_H_price = float("-inf"); highest_H_idx = -1
            lowest_L_price = float("inf"); lowest_L_idx = -1
            latest_H_price = float("nan"); latest_H_idx = -1
            latest_L_price = float("nan"); latest_L_idx = -1
            sH_ratchet_pending = True; sL_ratchet_pending = False
            event = "bos_up"
        elif bc < locked_sL and not sL_ratchet_pending:
            if state == "uptrend":
                new_sH = latest_H_price if not np.isnan(latest_H_price) else bh
                new_sH_idx = latest_H_idx if latest_H_idx >= 0 else i
                state = "downtrend"
            elif state == "downtrend":
                new_sH = highest_H_price if highest_H_price != float("-inf") else locked_sH
                new_sH_idx = highest_H_idx if highest_H_idx >= 0 else locked_sH_idx
            else:
                new_sH = locked_sH; new_sH_idx = locked_sH_idx; state = "downtrend"
            locked_sL = bc; locked_sL_idx = i
            locked_sH = new_sH; locked_sH_idx = new_sH_idx
            tracking_LL = bl; tracking_HH = float("-inf")
            highest_H_price = float("-inf"); highest_H_idx = -1
            lowest_L_price = float("inf"); lowest_L_idx = -1
            latest_H_price = float("nan"); latest_H_idx = -1
            latest_L_price = float("nan"); latest_L_idx = -1
            sL_ratchet_pending = True; sH_ratchet_pending = False
            event = "bos_down"

    return None


# ---------------------------------------------------------------------
# Returns_pct computation (faithful copy of engine logic).
# ---------------------------------------------------------------------

def compute_returns_pct(closes_1h: np.ndarray, regime_lookback_h: int = 2160
                        ) -> np.ndarray:
    n = len(closes_1h)
    rp = np.zeros(n, dtype=float)
    if n <= regime_lookback_h:
        return rp
    for k in range(regime_lookback_h, n):
        if closes_1h[k - regime_lookback_h] > 0:
            rp[k] = (closes_1h[k] - closes_1h[k - regime_lookback_h]) / closes_1h[k - regime_lookback_h] * 100
    return rp


def gate_value_for_5m(df_5m: pd.DataFrame, df_1h: pd.DataFrame,
                      target_5m_ts: pd.Timestamp,
                      regime_lookback_h: int = 2160) -> dict:
    """Return the gate-relevant state at the target 5m bar."""
    ts_5m_arr = df_5m["timestamp"].to_numpy()
    target_5m_idx = -1
    for i in range(len(ts_5m_arr)):
        if pd.Timestamp(ts_5m_arr[i]) == target_5m_ts:
            target_5m_idx = i
            break
    if target_5m_idx < 0:
        return {"error": "target_ts not in df_5m"}
    ts_5m_ns = _to_ns_int64(df_5m["timestamp"].to_numpy())
    ts_1h_ns = _to_ns_int64(df_1h["timestamp"].to_numpy())
    idx_1h_arr = map_5m_to_1h_idx(ts_5m_ns, ts_1h_ns)
    j = int(idx_1h_arr[target_5m_idx])
    closes_1h = df_1h["close"].to_numpy(dtype=float)
    rp = compute_returns_pct(closes_1h, regime_lookback_h)
    val = float(rp[j]) if 0 <= j < len(rp) else None
    if 0 <= j < len(closes_1h):
        ts_at_j = str(pd.Timestamp(df_1h["timestamp"].iloc[j]))
    else:
        ts_at_j = None
    if 0 <= j - regime_lookback_h < len(closes_1h):
        ts_at_lookback = str(pd.Timestamp(df_1h["timestamp"].iloc[j - regime_lookback_h]))
        close_at_lookback = float(closes_1h[j - regime_lookback_h])
    else:
        ts_at_lookback = None
        close_at_lookback = None
    return {
        "j_1h_idx": j,
        "ts_at_j": ts_at_j,
        "close_at_j": float(closes_1h[j]) if 0 <= j < len(closes_1h) else None,
        "j_minus_lookback_idx": j - regime_lookback_h,
        "ts_at_lookback": ts_at_lookback,
        "close_at_lookback": close_at_lookback,
        "returns_pct": val,
        "n_1h_bars": len(closes_1h),
    }


# ---------------------------------------------------------------------
# Main diagnostic
# ---------------------------------------------------------------------

def main() -> None:
    pair = "SOL/USDT"
    pf = pair.replace("/", "_")
    start = pd.Timestamp("2026-04-01", tz="UTC")
    end = pd.Timestamp("2026-05-04", tz="UTC")

    df_5m_full = pd.read_csv(f"data/raw/{pf}_5m.csv")
    df_1h_full = pd.read_csv(f"data/raw/{pf}_1h.csv")
    df_5m_full["timestamp"] = pd.to_datetime(df_5m_full["timestamp"], unit="ms", utc=True)
    df_1h_full["timestamp"] = pd.to_datetime(df_1h_full["timestamp"], unit="ms", utc=True)

    df_5m = df_5m_full[(df_5m_full["timestamp"] >= start) & (df_5m_full["timestamp"] <= end)].reset_index(drop=True)
    df_1h_for_batch = df_1h_full[df_1h_full["timestamp"] <= end].reset_index(drop=True)
    df_1h_pre = df_1h_full[df_1h_full["timestamp"] < start]
    df_1h_stream_iter = df_1h_full[(df_1h_full["timestamp"] >= start) & (df_1h_full["timestamp"] <= end)].reset_index(drop=True)

    print(f"=== 1-month parity diagnostic on {pair} ===")
    print(f"  window: {start} -> {end}")
    print(f"  5m bars (window): {len(df_5m):,}")
    print(f"  1h bars (full for batch): {len(df_1h_for_batch):,}")
    print(f"  1h bars to pre-load (before stream_start): {len(df_1h_pre):,} (capped to BUFFER_1H={BUFFER_1H} on append)")
    print(f"  1h bars to co-stream: {len(df_1h_stream_iter):,}")

    # ---- Batch run: full event capture ----
    print()
    print("running BATCH ...")
    cfg = dict(ENGINE_CONFIG)
    eng = SecondaryOnlyEngine(cfg, account_value=10_000.0)
    batch_events: list[dict] = []

    def batch_cb(kind: str, candidate: Optional[dict], bar_idx: int,
                 bar_ts: Optional[pd.Timestamp]) -> None:
        if kind == "OPEN_EOD":
            return
        snap = _snapshot_candidate(candidate)
        bos_ts = snap.get("bos_timestamp") if snap else None
        batch_events.append({
            "kind": kind,
            "bos_ts": str(pd.Timestamp(bos_ts)) if bos_ts is not None else None,
            "bar_ts": str(pd.Timestamp(bar_ts)) if bar_ts is not None else None,
            "bos_dir": snap.get("bos_direction") if snap else None,
            "swing_high": snap.get("swing_high") if snap else None,
            "swing_low": snap.get("swing_low") if snap else None,
        })

    t0 = time.time()
    eng.run(df_5m, df_1h_for_batch, df_1h_for_batch, event_callback=batch_cb)
    print(f"  batch: {len(batch_events)} events in {time.time()-t0:.1f}s")

    # Set of (kind, bos_ts_or_bar_ts) anchors in batch.
    def anchor(ev: dict) -> tuple:
        return (ev["kind"], ev["bos_ts"] if ev["bos_ts"] else ev["bar_ts"])

    batch_anchors = {anchor(e) for e in batch_events}

    # ---- Streaming run: instrumented ----
    print()
    print("running STREAMING (instrumented; this takes ~20-30 min) ...")
    buffers = PairBuffers(pair)
    for _, row in df_1h_pre.iterrows():
        buffers.b1h.append_closed(_make_bar(row), persist=False)
    print(f"  pre-loaded 1h buffer: {len(buffers.b1h)} bars (cap = {BUFFER_1H})")
    print(f"  pre-loaded 1h buffer first/last ts: "
          f"{pd.Timestamp(buffers.b1h._bars[0].timestamp_ms, unit='ms', tz='UTC')} "
          f"-> {pd.Timestamp(buffers.b1h._bars[-1].timestamp_ms, unit='ms', tz='UTC')}")

    streaming_events: list[dict] = []
    seen_anchors: set = set()
    calls_meta: list[dict] = []

    def stream_on_event(ev) -> None:
        a = (ev.kind, str(pd.Timestamp(ev.bos_timestamp)) if ev.bos_timestamp is not None else str(ev.bar_ts))
        if a in seen_anchors:
            return
        seen_anchors.add(a)
        snap = ev.candidate_snapshot
        streaming_events.append({
            "kind": ev.kind,
            "bos_ts": str(pd.Timestamp(ev.bos_timestamp)) if ev.bos_timestamp is not None else None,
            "bar_ts": str(pd.Timestamp(ev.bar_ts)) if ev.bar_ts is not None else None,
            "bos_dir": snap.get("bos_direction") if snap else None,
            "swing_high": snap.get("swing_high") if snap else None,
            "swing_low": snap.get("swing_low") if snap else None,
            "first_emitted_at_call_idx": len(calls_meta) - 1,
        })

    streaming = StreamingEngine(pair=pair, buffers=buffers, on_event=stream_on_event)

    n5 = len(df_5m)
    warmup = min(MIN_5M_FOR_ENGINE - 1, n5)
    j_1h = 0

    # Warmup phase.
    for i in range(warmup):
        ts5 = pd.Timestamp(df_5m["timestamp"].iloc[i])
        while j_1h < len(df_1h_stream_iter) and pd.Timestamp(df_1h_stream_iter["timestamp"].iloc[j_1h]) <= ts5:
            buffers.b1h.append_closed(_make_bar(df_1h_stream_iter.iloc[j_1h]), persist=False)
            j_1h += 1
        buffers.b5m.append_closed(_make_bar(df_5m.iloc[i]), persist=False)

    t0 = time.time()
    last_pct = -1
    for i in range(warmup, n5):
        ts5 = pd.Timestamp(df_5m["timestamp"].iloc[i])
        while j_1h < len(df_1h_stream_iter) and pd.Timestamp(df_1h_stream_iter["timestamp"].iloc[j_1h]) <= ts5:
            buffers.b1h.append_closed(_make_bar(df_1h_stream_iter.iloc[j_1h]), persist=False)
            j_1h += 1
        buffers.b5m.append_closed(_make_bar(df_5m.iloc[i]), persist=False)
        # Capture meta BEFORE engine call.
        b5_first = buffers.b5m._bars[0].timestamp_ms if buffers.b5m._bars else None
        b5_last = buffers.b5m._bars[-1].timestamp_ms if buffers.b5m._bars else None
        b1_first = buffers.b1h._bars[0].timestamp_ms if buffers.b1h._bars else None
        b1_last = buffers.b1h._bars[-1].timestamp_ms if buffers.b1h._bars else None
        first50 = buffers.b5m._bars[:50]
        seed_sH = max((b.high for b in first50), default=None)
        seed_sL = min((b.low for b in first50), default=None)
        calls_meta.append({
            "call_idx": len(calls_meta),
            "call_bar_ts": str(ts5),
            "n_5m": len(buffers.b5m),
            "n_1h": len(buffers.b1h),
            "b5m_first_ts": str(pd.Timestamp(b5_first, unit="ms", tz="UTC")) if b5_first else None,
            "b5m_last_ts": str(pd.Timestamp(b5_last, unit="ms", tz="UTC")) if b5_last else None,
            "b1h_first_ts": str(pd.Timestamp(b1_first, unit="ms", tz="UTC")) if b1_first else None,
            "b1h_last_ts": str(pd.Timestamp(b1_last, unit="ms", tz="UTC")) if b1_last else None,
            "seed_sH": seed_sH,
            "seed_sL": seed_sL,
        })
        streaming.on_new_bar_5m()
        pct = int((i - warmup) * 100 / max(1, n5 - warmup))
        if pct != last_pct and pct % 10 == 0:
            elapsed = time.time() - t0
            print(f"    {pct}% ({i - warmup}/{n5 - warmup})  elapsed={elapsed:.0f}s")
            last_pct = pct

    print(f"  streaming: {len(streaming_events)} events, {len(calls_meta)} calls in {time.time()-t0:.1f}s")

    streaming_anchors = {anchor(e) for e in streaming_events}

    # ---- Set diff ----
    in_streaming_only = streaming_anchors - batch_anchors
    in_batch_only = batch_anchors - streaming_anchors
    common = streaming_anchors & batch_anchors

    print()
    print("=== SET DIFF ===")
    print(f"  common anchors:        {len(common):>5}")
    print(f"  streaming-only anchors: {len(in_streaming_only):>5}")
    print(f"  batch-only anchors:    {len(in_batch_only):>5}")

    # ---- Group streaming-only by kind ----
    so_by_kind = defaultdict(list)
    for ev in streaming_events:
        if anchor(ev) in in_streaming_only:
            so_by_kind[ev["kind"]].append(ev)
    bo_by_kind = defaultdict(list)
    for ev in batch_events:
        if anchor(ev) in in_batch_only:
            bo_by_kind[ev["kind"]].append(ev)

    print()
    print("Streaming-only events by kind:")
    for k, v in sorted(so_by_kind.items(), key=lambda x: -len(x[1])):
        print(f"  {k:32s}  {len(v):>5}")
    print()
    print("Batch-only events by kind:")
    for k, v in sorted(bo_by_kind.items(), key=lambda x: -len(x[1])):
        print(f"  {k:32s}  {len(v):>5}")

    # ---- Detailed inspection of first 10 streaming-only events ----
    print()
    print("=== SAMPLE: first 10 STREAMING-ONLY events ===")
    samples_to_inspect = []
    for ev in streaming_events[:10000]:
        if anchor(ev) in in_streaming_only and ev["bos_ts"] is not None:
            samples_to_inspect.append(ev)
        if len(samples_to_inspect) >= 10:
            break

    diagnoses = []
    for n_idx, ev in enumerate(samples_to_inspect):
        bos_ts = pd.Timestamp(ev["bos_ts"])
        call_idx = ev["first_emitted_at_call_idx"]
        meta = calls_meta[call_idx] if 0 <= call_idx < len(calls_meta) else None

        print()
        print(f"--- streaming-only [{n_idx+1}/10] ---")
        print(f"  kind={ev['kind']}  bos_ts={ev['bos_ts']}  bar_ts={ev['bar_ts']}")
        print(f"  emitted by streaming call_idx={call_idx}")
        if meta:
            print(f"    call_bar_ts={meta['call_bar_ts']}")
            print(f"    n_5m={meta['n_5m']}  n_1h={meta['n_1h']}")
            print(f"    b5m: {meta['b5m_first_ts']} -> {meta['b5m_last_ts']}")
            print(f"    b1h: {meta['b1h_first_ts']} -> {meta['b1h_last_ts']}")
            print(f"    streaming init_seed:  sH={meta['seed_sH']}  sL={meta['seed_sL']}")

        # Reconstruct streaming buffer at the time of this call.
        # 5m: bars from b5m_first_ts to b5m_last_ts.
        if meta is None:
            continue
        b5_first_ts = pd.Timestamp(meta["b5m_first_ts"])
        b5_last_ts = pd.Timestamp(meta["b5m_last_ts"])
        df_5m_buf = df_5m_full[(df_5m_full["timestamp"] >= b5_first_ts) & (df_5m_full["timestamp"] <= b5_last_ts)].reset_index(drop=True)
        b1_first_ts = pd.Timestamp(meta["b1h_first_ts"])
        b1_last_ts = pd.Timestamp(meta["b1h_last_ts"])
        df_1h_buf = df_1h_full[(df_1h_full["timestamp"] >= b1_first_ts) & (df_1h_full["timestamp"] <= b1_last_ts)].reset_index(drop=True)

        # Streaming analyzer state at bos_ts.
        try:
            stream_state = trace_analyzer_state(df_5m_buf, bos_ts)
        except Exception as e:
            stream_state = {"error": str(e)}
        # Batch analyzer state at bos_ts (full df_5m, full df_1h).
        try:
            batch_state = trace_analyzer_state(df_5m, bos_ts)
        except Exception as e:
            batch_state = {"error": str(e)}

        print(f"  ANALYZER state at bos_ts (PRE-BOS resolution):")
        print(f"    BATCH (full df_5m from {start}):")
        for k in ["state", "locked_sH", "locked_sH_ts", "locked_sL", "locked_sL_ts",
                  "highest_H", "highest_H_ts", "latest_H", "latest_H_ts",
                  "lowest_L", "lowest_L_ts", "latest_L", "latest_L_ts",
                  "sH_ratchet_pending", "sL_ratchet_pending",
                  "init_seed_sH", "init_seed_sL", "init_seed_first_ts", "init_seed_last_ts",
                  "n_bars_processed", "bar_close"]:
            print(f"      {k:24s}: {batch_state.get(k) if batch_state else 'NA'}")
        print(f"    STREAMING (buffer at call_idx={call_idx}, n_5m={len(df_5m_buf)}):")
        for k in ["state", "locked_sH", "locked_sH_ts", "locked_sL", "locked_sL_ts",
                  "highest_H", "highest_H_ts", "latest_H", "latest_H_ts",
                  "lowest_L", "lowest_L_ts", "latest_L", "latest_L_ts",
                  "sH_ratchet_pending", "sL_ratchet_pending",
                  "init_seed_sH", "init_seed_sL", "init_seed_first_ts", "init_seed_last_ts",
                  "n_bars_processed", "bar_close"]:
            print(f"      {k:24s}: {stream_state.get(k) if stream_state else 'NA'}")

        # Gate value.
        gate_batch = gate_value_for_5m(df_5m, df_1h_for_batch, bos_ts)
        gate_stream = gate_value_for_5m(df_5m_buf, df_1h_buf, bos_ts)
        print(f"  REGIME GATE returns_pct at bos_ts:")
        print(f"    BATCH:")
        for k, v in gate_batch.items():
            print(f"      {k:24s}: {v}")
        print(f"    STREAMING:")
        for k, v in gate_stream.items():
            print(f"      {k:24s}: {v}")

        # H1/H2/H3 verdict.
        h1_init_seed_diff = (
            batch_state and stream_state
            and (batch_state.get("init_seed_sH") != stream_state.get("init_seed_sH")
                 or batch_state.get("init_seed_sL") != stream_state.get("init_seed_sL"))
        )
        h2_returns_pct_diff = (
            gate_batch.get("returns_pct") is not None
            and gate_stream.get("returns_pct") is not None
            and abs(gate_batch["returns_pct"] - gate_stream["returns_pct"]) > 0.01
        )
        # H3: same init seed, same returns_pct, but pivot tracking diverged.
        h3_pivot_diff = (
            batch_state and stream_state
            and not h1_init_seed_diff
            and (batch_state.get("highest_H") != stream_state.get("highest_H")
                 or batch_state.get("latest_H") != stream_state.get("latest_H")
                 or batch_state.get("lowest_L") != stream_state.get("lowest_L")
                 or batch_state.get("latest_L") != stream_state.get("latest_L")
                 or batch_state.get("locked_sH") != stream_state.get("locked_sH")
                 or batch_state.get("locked_sL") != stream_state.get("locked_sL"))
        )
        verdict = []
        if h1_init_seed_diff: verdict.append("H1")
        if h2_returns_pct_diff: verdict.append("H2")
        if h3_pivot_diff: verdict.append("H3")
        if not verdict: verdict.append("UNCLASSIFIED")
        print(f"  VERDICT: {','.join(verdict)}")
        diagnoses.append({"event": ev, "verdict": verdict})

    # ---- Summary tally ----
    print()
    print("=" * 60)
    print("DIAGNOSIS TALLY (first 10 streaming-only events)")
    print("=" * 60)
    counts = defaultdict(int)
    for d in diagnoses:
        for v in d["verdict"]:
            counts[v] += 1
    for k in ["H1", "H2", "H3", "UNCLASSIFIED"]:
        print(f"  {k}: {counts[k]} of {len(diagnoses)}")

    # Write full event lists + per-event diagnoses to JSON for offline analysis.
    out_path = "paper_trade/data/diag_1month_parity.json"
    payload = {
        "window": {"start": str(start), "end": str(end)},
        "counts": {
            "batch_events": len(batch_events),
            "streaming_events": len(streaming_events),
            "common": len(common),
            "streaming_only": len(in_streaming_only),
            "batch_only": len(in_batch_only),
        },
        "streaming_only_by_kind": {k: len(v) for k, v in so_by_kind.items()},
        "batch_only_by_kind": {k: len(v) for k, v in bo_by_kind.items()},
        "diagnoses_first_10": diagnoses,
    }
    with open(out_path, "w") as f:
        json.dump(payload, f, indent=2, default=str)
    print(f"\n-> {out_path}")


if __name__ == "__main__":
    main()
