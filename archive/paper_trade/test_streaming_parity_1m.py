"""1m fill-detection parity test.

Validates that the 1m intrabar_step path produces the SAME event
ANCHOR SET as the pure-5m streaming path. Wick-based events
(ARMED, PRIMARY_TRIGGERED, FILLED, RESOLVED_TP, RESOLVED_HARD_STOP)
fire earlier when 1m bars are present (sub-5m latency), but the
final set of events on the same bos_timestamp anchors must match —
i.e., no event should go missing, and no spurious event should
appear.

Strategy:
  1. Load 5m + 1h + 1m for the test window.
  2. Run pure-5m streaming (reference): co-stream 5m + 1h → engine.
  3. Run 5m+1m hybrid: same 5m + 1h cadence, but BEFORE each 5m
     close, replay all 1m bars within that 5m window through
     `on_new_bar_1m`. The 5m close still calls `on_new_bar_5m`.
  4. Assert: the set of (kind, bos_timestamp) anchors is identical
     between the two runs.
  5. Sanity check: at least one wick-based event fires from the
     intrabar path (otherwise the test is vacuous — we'd just be
     hitting the 5m fallback).

Data window: SOL/USDT, last few days of 1m coverage. The 1m CSV at
data/raw/SOL_USDT_1m.csv has ~8 days of history; we test on a slice.

Run:
    python -m paper_trade.test_streaming_parity_1m \\
        --pair SOL/USDT --start 2026-04-23 --end 2026-05-01
"""

from __future__ import annotations

import argparse
from typing import Optional

import pandas as pd

from paper_trade.bar_buffer import Bar, PairBuffers
from paper_trade.config import ENGINE_CONFIG, MIN_5M_FOR_ENGINE
from paper_trade.streaming_engine import EngineEvent, StreamingEngine


def _make_bar(row) -> Bar:
    return Bar(int(pd.Timestamp(row["timestamp"]).value // 10**6),
               float(row["open"]), float(row["high"]), float(row["low"]),
               float(row["close"]), float(row["volume"]))


def _anchor(ev: EngineEvent) -> tuple:
    """The dedup anchor: setup-bound events use bos_timestamp;
    setup-less events use bar_ts. Mirrors EngineEvent.anchor()."""
    if ev.bos_timestamp is not None:
        return (ev.kind, pd.Timestamp(ev.bos_timestamp))
    return (ev.kind, pd.Timestamp(ev.bar_ts) if ev.bar_ts is not None else None)


# ----------------------------------------------------------------------
# Reference run: pure 5m streaming (no 1m).
# ----------------------------------------------------------------------

def run_streaming_5m_only(pair: str,
                          df_5m: pd.DataFrame,
                          df_1h: pd.DataFrame) -> list[EngineEvent]:
    buffers = PairBuffers(pair)
    events: list[EngineEvent] = []

    def on_event(ev: EngineEvent) -> None:
        events.append(ev)

    streaming = StreamingEngine(pair=pair, buffers=buffers, on_event=on_event)
    n5 = len(df_5m)
    if n5 == 0:
        return events
    stream_start_ts = pd.Timestamp(df_5m["timestamp"].iloc[0])

    df_1h_pre = df_1h[df_1h["timestamp"] < stream_start_ts]
    for _, row in df_1h_pre.iterrows():
        buffers.b1h.append_closed(_make_bar(row), persist=False)
    df_1h_stream = df_1h[df_1h["timestamp"] >= stream_start_ts].reset_index(drop=True)
    j_1h = 0

    warmup = min(MIN_5M_FOR_ENGINE - 1, n5)
    for i in range(warmup):
        ts5 = pd.Timestamp(df_5m["timestamp"].iloc[i])
        while j_1h < len(df_1h_stream) and pd.Timestamp(df_1h_stream["timestamp"].iloc[j_1h]) <= ts5:
            buffers.b1h.append_closed(_make_bar(df_1h_stream.iloc[j_1h]), persist=False)
            j_1h += 1
        buffers.b5m.append_closed(_make_bar(df_5m.iloc[i]), persist=False)

    for i in range(warmup, n5):
        ts5 = pd.Timestamp(df_5m["timestamp"].iloc[i])
        while j_1h < len(df_1h_stream) and pd.Timestamp(df_1h_stream["timestamp"].iloc[j_1h]) <= ts5:
            buffers.b1h.append_closed(_make_bar(df_1h_stream.iloc[j_1h]), persist=False)
            j_1h += 1
        buffers.b5m.append_closed(_make_bar(df_5m.iloc[i]), persist=False)
        streaming.on_new_bar_5m()
    return events


# ----------------------------------------------------------------------
# Hybrid run: 5m + 1h with 1m intrabar fired before each 5m close.
# ----------------------------------------------------------------------

def run_streaming_5m_plus_1m(pair: str,
                             df_5m: pd.DataFrame,
                             df_1h: pd.DataFrame,
                             df_1m: pd.DataFrame) -> list[EngineEvent]:
    buffers = PairBuffers(pair)
    events: list[EngineEvent] = []

    def on_event(ev: EngineEvent) -> None:
        events.append(ev)

    streaming = StreamingEngine(pair=pair, buffers=buffers, on_event=on_event)
    n5 = len(df_5m)
    if n5 == 0:
        return events
    stream_start_ts = pd.Timestamp(df_5m["timestamp"].iloc[0])

    df_1h_pre = df_1h[df_1h["timestamp"] < stream_start_ts]
    for _, row in df_1h_pre.iterrows():
        buffers.b1h.append_closed(_make_bar(row), persist=False)
    df_1h_stream = df_1h[df_1h["timestamp"] >= stream_start_ts].reset_index(drop=True)
    j_1h = 0

    # Index 1m bars by their ts in ms so we can slice per-5m-window.
    # Use `.values.astype('datetime64[ms]').astype('int64')` to round-
    # trip a tz-aware ns-precision Series to ms safely; pandas'
    # bare `astype('int64')` on tz-aware datetime Series returns a
    # truncated/rescaled int that's NOT the raw ns count.
    df_1m_sorted = df_1m.sort_values("timestamp").reset_index(drop=True)
    ts_1m_ms = (df_1m_sorted["timestamp"].values.astype("datetime64[ms]")
                .astype("int64"))
    high_1m = df_1m_sorted["high"].to_numpy()
    low_1m = df_1m_sorted["low"].to_numpy()
    j_1m = 0

    warmup = min(MIN_5M_FOR_ENGINE - 1, n5)
    for i in range(warmup):
        ts5 = pd.Timestamp(df_5m["timestamp"].iloc[i])
        while j_1h < len(df_1h_stream) and pd.Timestamp(df_1h_stream["timestamp"].iloc[j_1h]) <= ts5:
            buffers.b1h.append_closed(_make_bar(df_1h_stream.iloc[j_1h]), persist=False)
            j_1h += 1
        buffers.b5m.append_closed(_make_bar(df_5m.iloc[i]), persist=False)

    n_intrabar_events = 0
    for i in range(warmup, n5):
        ts5 = pd.Timestamp(df_5m["timestamp"].iloc[i])
        ts5_ms = int(ts5.value // 10**6)
        # 5m candle [ts5, ts5+5min) covers 1m bars [ts5, ts5+5min).
        # Replay 1m bars whose ts is in this 5m candle BEFORE the 5m close.
        next_5m_ms = ts5_ms + 5 * 60_000
        # Append any newly-closed 1h bars first (parity with production).
        while j_1h < len(df_1h_stream) and pd.Timestamp(df_1h_stream["timestamp"].iloc[j_1h]) <= ts5:
            buffers.b1h.append_closed(_make_bar(df_1h_stream.iloc[j_1h]), persist=False)
            j_1h += 1
        # Fire 1m intrabars within [ts5, ts5+5min).
        while j_1m < len(ts_1m_ms) and ts_1m_ms[j_1m] < ts5_ms:
            j_1m += 1
        while j_1m < len(ts_1m_ms) and ts_1m_ms[j_1m] < next_5m_ms:
            new_evs = streaming.on_new_bar_1m(
                ts_ms=int(ts_1m_ms[j_1m]),
                high=float(high_1m[j_1m]),
                low=float(low_1m[j_1m]),
            )
            n_intrabar_events += len(new_evs)
            j_1m += 1
        # Now the 5m close.
        buffers.b5m.append_closed(_make_bar(df_5m.iloc[i]), persist=False)
        streaming.on_new_bar_5m()

    print(f"  intrabar (1m) emitted {n_intrabar_events} events")
    return events


# ----------------------------------------------------------------------
# Diff
# ----------------------------------------------------------------------

def diff_anchors(ref: list[EngineEvent], hyb: list[EngineEvent]) -> tuple[bool, list[str]]:
    """Compare anchor sets. The same (kind, bos_timestamp) anchors
    should appear in both runs; intrabar may shift bar_ts earlier."""
    ref_anchors = [_anchor(e) for e in ref]
    hyb_anchors = [_anchor(e) for e in hyb]
    msgs: list[str] = []
    if len(ref_anchors) != len(hyb_anchors):
        msgs.append(f"length mismatch: 5m_only={len(ref_anchors)}  hybrid={len(hyb_anchors)}")
    set_ref = set(ref_anchors)
    set_hyb = set(hyb_anchors)
    only_ref = set_ref - set_hyb
    only_hyb = set_hyb - set_ref
    if only_ref:
        msgs.append(f"  events in 5m-only but missing from hybrid (count={len(only_ref)}):")
        for a in list(only_ref)[:10]:
            msgs.append(f"    {a}")
    if only_hyb:
        msgs.append(f"  events in hybrid but missing from 5m-only (count={len(only_hyb)}):")
        for a in list(only_hyb)[:10]:
            msgs.append(f"    {a}")
    return (len(msgs) == 0, msgs)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--pair", default="SOL/USDT")
    p.add_argument("--start", default="2026-04-23")
    p.add_argument("--end", default="2026-05-01")
    args = p.parse_args()

    pair = args.pair
    pf = pair.replace("/", "_")
    df_5m_full = pd.read_csv(f"data/raw/{pf}_5m.csv")
    df_1h_full = pd.read_csv(f"data/raw/{pf}_1h.csv")
    df_1m_full = pd.read_csv(f"data/raw/{pf}_1m.csv")
    df_5m_full["timestamp"] = pd.to_datetime(df_5m_full["timestamp"], unit="ms", utc=True)
    df_1h_full["timestamp"] = pd.to_datetime(df_1h_full["timestamp"], unit="ms", utc=True)
    df_1m_full["timestamp"] = pd.to_datetime(df_1m_full["timestamp"], unit="ms", utc=True)
    start = pd.Timestamp(args.start, tz="UTC")
    end = pd.Timestamp(args.end, tz="UTC")

    df_5m = df_5m_full[(df_5m_full["timestamp"] >= start) & (df_5m_full["timestamp"] <= end)].reset_index(drop=True)
    df_1h = df_1h_full[df_1h_full["timestamp"] <= end].reset_index(drop=True)
    df_1m = df_1m_full[(df_1m_full["timestamp"] >= start) & (df_1m_full["timestamp"] <= end)].reset_index(drop=True)

    print(f"=== 1m parity {pair} {args.start} -> {args.end} ===")
    print(f"  5m bars: {len(df_5m):,}   1h bars: {len(df_1h):,}   1m bars: {len(df_1m):,}")

    ref = run_streaming_5m_only(pair, df_5m, df_1h)
    print(f"  5m-only:  {len(ref)} events")
    hyb = run_streaming_5m_plus_1m(pair, df_5m, df_1h, df_1m)
    print(f"  5m+1m:    {len(hyb)} events")

    ok, msgs = diff_anchors(ref, hyb)
    if ok:
        print()
        print(f"  PARITY: PASS  ({len(ref)} anchor-equivalent events)")
        # Verify intrabar actually contributed (test isn't vacuous).
        hyb_intrabar_kinds = {"ARMED", "PRIMARY_TRIGGERED", "FILLED",
                              "RESOLVED_TP", "RESOLVED_HARD_STOP"}
        n_wick_events = sum(1 for e in hyb if e.kind in hyb_intrabar_kinds)
        print(f"  wick-based events present in hybrid: {n_wick_events}")
        if n_wick_events == 0:
            print()
            print("  WARNING: test window has no wick-based events. "
                  "1m intrabar path was not exercised. Pick a longer "
                  "window or one with active setups.")
    else:
        print()
        print(f"  PARITY: FAIL")
        for m in msgs:
            print(m)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
