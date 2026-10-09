"""Parity-with-disconnect test: simulate a mid-stream WS disconnect and
verify the reconnect gap-fill produces correct buffer state and
byte-identical engine events vs an uninterrupted batch run.

Scenario simulated:
  1. Stream 5m + 1h bars for 100 closed 5m bars normally (warmup).
  2. Disconnect: skip the next 6 5m bars (= 30 min) AND any 1h bars
     in that window. WS would receive nothing during this period in
     production.
  3. Reconnect gap-fill: append all skipped bars in chronological
     order in a single batch — exactly what `_gap_fill` does after
     a real WS reconnect — then trigger one engine pass per pair.
  4. Resume normal streaming for the remaining bars.

Assertion: the streaming-engine event signature list must equal a
batch run on the full uninterrupted dataset. The reconnect gap-fill
must produce ZERO divergence; bars closed during the disconnect must
end up in the buffer in the right order, and the engine must catch
up via dedup on the next call.

This is a unit-style harness; it does not touch the real WS or REST
clients. It directly exercises `bar_buffer.append_closed` and
`StreamingEngine.on_new_bar_5m` to validate the LOGIC of reconnect
recovery.
"""

from __future__ import annotations

import argparse
from typing import Optional

import pandas as pd

from paper_trade.bar_buffer import Bar, PairBuffers
from paper_trade.config import MIN_5M_FOR_ENGINE
from paper_trade.streaming_engine import EngineEvent, StreamingEngine, _snapshot_candidate
from paper_trade.test_streaming_parity import (
    run_batch, _make_bar, _event_signature, diff_event_lists,
)


def run_streaming_with_disconnect(
    pair: str,
    df_5m: pd.DataFrame,
    df_1h: pd.DataFrame,
    disconnect_after_n_5m: int,
    n_5m_dropped: int,
) -> list[tuple]:
    """Mirrors the production Phase-1a flow with a simulated WS
    disconnect at bar `disconnect_after_n_5m` for the duration of
    `n_5m_dropped` 5m intervals.

    During the disconnect: 5m + 1h bars are NOT appended in real time
    (WS would deliver nothing).

    On reconnect: a single batch of "missed" bars is appended in
    chronological order (same as `_gap_fill` does after a real
    reconnect), then a single engine pass runs per pair to catch up
    on any signals fired by the now-buffered bars."""
    buffers = PairBuffers(pair)

    sigs: list[tuple] = []

    def on_event(ev: EngineEvent) -> None:
        sigs.append(_event_signature(ev.kind, ev.candidate_snapshot, ev.bar_ts))

    streaming = StreamingEngine(pair=pair, buffers=buffers, on_event=on_event)

    n5 = len(df_5m)
    if n5 == 0:
        return sigs
    stream_start_ts = pd.Timestamp(df_5m["timestamp"].iloc[0])

    # Phase 1: pre-load 1h before stream_start.
    df_1h_pre = df_1h[df_1h["timestamp"] < stream_start_ts]
    for _, row in df_1h_pre.iterrows():
        buffers.b1h.append_closed(_make_bar(row), persist=False)

    df_1h_stream = df_1h[df_1h["timestamp"] >= stream_start_ts].reset_index(drop=True)
    j_1h = 0

    # Phase 2: 5m warmup.
    warmup = min(MIN_5M_FOR_ENGINE - 1, n5)
    for i in range(warmup):
        ts5 = pd.Timestamp(df_5m["timestamp"].iloc[i])
        while j_1h < len(df_1h_stream) and pd.Timestamp(df_1h_stream["timestamp"].iloc[j_1h]) <= ts5:
            buffers.b1h.append_closed(_make_bar(df_1h_stream.iloc[j_1h]), persist=False)
            j_1h += 1
        buffers.b5m.append_closed(_make_bar(df_5m.iloc[i]), persist=False)

    # Phase 3a: stream up to the disconnect point.
    disconnect_idx = warmup + disconnect_after_n_5m
    disconnect_idx = min(disconnect_idx, n5)
    for i in range(warmup, disconnect_idx):
        ts5 = pd.Timestamp(df_5m["timestamp"].iloc[i])
        while j_1h < len(df_1h_stream) and pd.Timestamp(df_1h_stream["timestamp"].iloc[j_1h]) <= ts5:
            buffers.b1h.append_closed(_make_bar(df_1h_stream.iloc[j_1h]), persist=False)
            j_1h += 1
        buffers.b5m.append_closed(_make_bar(df_5m.iloc[i]), persist=False)
        streaming.on_new_bar_5m()

    # Phase 3b: SIMULATE DISCONNECT — skip the next n_5m_dropped 5m
    # bars and any 1h bars whose timestamp falls in that window.
    drop_end_idx = min(disconnect_idx + n_5m_dropped, n5)
    if drop_end_idx > disconnect_idx:
        drop_window_end_ts = pd.Timestamp(df_5m["timestamp"].iloc[drop_end_idx - 1])
    else:
        drop_window_end_ts = stream_start_ts
    print(f"  [disconnect] simulated WS down at idx {disconnect_idx}, dropping "
          f"{drop_end_idx - disconnect_idx} 5m bars "
          f"(through {drop_window_end_ts})")

    # Advance j_1h past the drop window without appending.
    j_1h_during_drop = j_1h
    while j_1h_during_drop < len(df_1h_stream) and \
          pd.Timestamp(df_1h_stream["timestamp"].iloc[j_1h_during_drop]) <= drop_window_end_ts:
        j_1h_during_drop += 1
    n_1h_dropped = j_1h_during_drop - j_1h

    # Phase 3c: RECONNECT GAP-FILL — append all dropped bars in
    # chronological order, mirroring `_gap_fill` behavior.
    print(f"  [reconnect] gap-filling {drop_end_idx - disconnect_idx} 5m + "
          f"{n_1h_dropped} 1h bars")
    while j_1h < len(df_1h_stream) and \
          pd.Timestamp(df_1h_stream["timestamp"].iloc[j_1h]) <= drop_window_end_ts:
        buffers.b1h.append_closed(_make_bar(df_1h_stream.iloc[j_1h]), persist=False)
        j_1h += 1
    for i in range(disconnect_idx, drop_end_idx):
        buffers.b5m.append_closed(_make_bar(df_5m.iloc[i]), persist=False)
    # Single engine pass after reconnect to emit any signals from the
    # gap-filled bars. Mirrors run.py's reconnect_handler.
    streaming.on_new_bar_5m()

    # Phase 4: resume normal streaming.
    for i in range(drop_end_idx, n5):
        ts5 = pd.Timestamp(df_5m["timestamp"].iloc[i])
        while j_1h < len(df_1h_stream) and pd.Timestamp(df_1h_stream["timestamp"].iloc[j_1h]) <= ts5:
            buffers.b1h.append_closed(_make_bar(df_1h_stream.iloc[j_1h]), persist=False)
            j_1h += 1
        buffers.b5m.append_closed(_make_bar(df_5m.iloc[i]), persist=False)
        streaming.on_new_bar_5m()

    return sigs


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--pair", default="SOL/USDT")
    p.add_argument("--start", default="2026-04-22")
    p.add_argument("--end", default="2026-05-04")
    p.add_argument("--disconnect-after-bars", type=int, default=500,
                   help="Trigger simulated disconnect after this many post-warmup 5m bars.")
    p.add_argument("--drop-bars", type=int, default=6,
                   help="Number of 5m bars to drop (simulating WS down). 6 = 30 min.")
    args = p.parse_args()

    pair = args.pair
    pf = pair.replace("/", "_")
    df_5m_full = pd.read_csv(f"data/raw/{pf}_5m.csv")
    df_1h_full = pd.read_csv(f"data/raw/{pf}_1h.csv")
    df_5m_full["timestamp"] = pd.to_datetime(df_5m_full["timestamp"], unit="ms", utc=True)
    df_1h_full["timestamp"] = pd.to_datetime(df_1h_full["timestamp"], unit="ms", utc=True)
    start = pd.Timestamp(args.start, tz="UTC")
    end = pd.Timestamp(args.end, tz="UTC")
    df_5m = df_5m_full[(df_5m_full["timestamp"] >= start) & (df_5m_full["timestamp"] <= end)].reset_index(drop=True)
    df_1h = df_1h_full[df_1h_full["timestamp"] <= end].reset_index(drop=True)

    print(f"=== Reconnect-parity test {pair} {args.start} -> {args.end} ===")
    print(f"  5m bars: {len(df_5m):,}   1h bars: {len(df_1h):,}")
    print(f"  disconnect after: {args.disconnect_after_bars} 5m bars post-warmup")
    print(f"  drop window: {args.drop_bars} 5m bars (= {args.drop_bars * 5} min)")
    print()

    print("running batch (uninterrupted reference) ...")
    batch_sigs = run_batch(df_5m, df_1h)
    print(f"  batch events: {len(batch_sigs)}")

    print()
    print("running streaming with simulated disconnect ...")
    stream_sigs = run_streaming_with_disconnect(
        pair, df_5m, df_1h,
        disconnect_after_n_5m=args.disconnect_after_bars,
        n_5m_dropped=args.drop_bars,
    )
    print(f"  streaming events: {len(stream_sigs)}")

    print()
    ok, msgs = diff_event_lists(batch_sigs, stream_sigs)
    if ok:
        print(f"  RECONNECT PARITY: PASS  ({len(batch_sigs)} events identical)")
    else:
        print("  RECONNECT PARITY: FAIL")
        for m in msgs:
            print(m)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
