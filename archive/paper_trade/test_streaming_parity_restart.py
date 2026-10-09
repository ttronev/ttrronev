"""Validation Test 4: restart drill at the streaming-wrapper level.

Spec (per REFACTOR_C1_DESIGN.md §4):
  1. Run baseline: streaming engine processes 1-month SOL window
     uninterrupted; capture event log + final engine state JSON.
  2. Run drill: identical streaming run, kill the process via
     simulated SIGKILL at bar 2026-04-15 12:00 UTC (~half-way),
     restart, allow replay from checkpoint to catch up to the same
     final bar.
  3. Drill's event log must be byte-identical to baseline's.
  4. Drill's final engine state JSON must be byte-identical to
     baseline's.
"""

from __future__ import annotations

import argparse
import json
from typing import Optional

import pandas as pd

from paper_trade.bar_buffer import Bar, PairBuffers
from paper_trade.config import MIN_5M_FOR_ENGINE
from paper_trade.streaming_engine import EngineEvent, StreamingEngine
from paper_trade.test_streaming_parity import _make_bar, _event_signature


def _run_baseline(pair: str, df_5m: pd.DataFrame, df_1h: pd.DataFrame
                  ) -> tuple[list[tuple], dict]:
    """Process the entire window as a single uninterrupted streaming run.
    Returns (event_signatures, engine_state_json)."""
    buffers = PairBuffers(pair)
    stream_start_ts = pd.Timestamp(df_5m["timestamp"].iloc[0])
    df_1h_pre = df_1h[df_1h["timestamp"] < stream_start_ts]
    for _, row in df_1h_pre.iterrows():
        buffers.b1h.append_closed(_make_bar(row), persist=False)
    df_1h_stream = df_1h[df_1h["timestamp"] >= stream_start_ts].reset_index(drop=True)

    sigs: list[tuple] = []
    def on_event(ev: EngineEvent) -> None:
        sigs.append(_event_signature(ev.kind, ev.candidate_snapshot, ev.bar_ts))

    streaming = StreamingEngine(pair=pair, buffers=buffers, on_event=on_event)

    j_1h = 0
    n5 = len(df_5m)
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

    final_state = streaming.serialize_engine_state()
    return sigs, final_state


def _run_drill(pair: str, df_5m: pd.DataFrame, df_1h: pd.DataFrame,
               kill_at_ts: pd.Timestamp) -> tuple[list[tuple], dict]:
    """Process bars up to kill_at_ts. Serialize engine state. Throw
    away the streaming wrapper. Restore from the checkpoint into a new
    wrapper. Continue with remaining bars. Returns the COMBINED event
    signatures and the final engine state."""

    sigs: list[tuple] = []
    seen_after_restart: set = set()

    def on_event_pre(ev: EngineEvent) -> None:
        sigs.append(_event_signature(ev.kind, ev.candidate_snapshot, ev.bar_ts))
        # Track anchors for post-restart dedup.
        if ev.bos_timestamp is not None:
            seen_after_restart.add((ev.kind, pd.Timestamp(ev.bos_timestamp)))
        else:
            seen_after_restart.add((ev.kind, ev.bar_ts))

    def on_event_post(ev: EngineEvent) -> None:
        sigs.append(_event_signature(ev.kind, ev.candidate_snapshot, ev.bar_ts))

    buffers = PairBuffers(pair)
    stream_start_ts = pd.Timestamp(df_5m["timestamp"].iloc[0])
    df_1h_pre = df_1h[df_1h["timestamp"] < stream_start_ts]
    for _, row in df_1h_pre.iterrows():
        buffers.b1h.append_closed(_make_bar(row), persist=False)
    df_1h_stream = df_1h[df_1h["timestamp"] >= stream_start_ts].reset_index(drop=True)

    streaming = StreamingEngine(pair=pair, buffers=buffers, on_event=on_event_pre)

    j_1h = 0
    n5 = len(df_5m)
    warmup = min(MIN_5M_FOR_ENGINE - 1, n5)
    kill_idx = None
    for i in range(warmup):
        ts5 = pd.Timestamp(df_5m["timestamp"].iloc[i])
        while j_1h < len(df_1h_stream) and pd.Timestamp(df_1h_stream["timestamp"].iloc[j_1h]) <= ts5:
            buffers.b1h.append_closed(_make_bar(df_1h_stream.iloc[j_1h]), persist=False)
            j_1h += 1
        buffers.b5m.append_closed(_make_bar(df_5m.iloc[i]), persist=False)

    for i in range(warmup, n5):
        ts5 = pd.Timestamp(df_5m["timestamp"].iloc[i])
        if ts5 >= kill_at_ts and kill_idx is None:
            kill_idx = i
            break
        while j_1h < len(df_1h_stream) and pd.Timestamp(df_1h_stream["timestamp"].iloc[j_1h]) <= ts5:
            buffers.b1h.append_closed(_make_bar(df_1h_stream.iloc[j_1h]), persist=False)
            j_1h += 1
        buffers.b5m.append_closed(_make_bar(df_5m.iloc[i]), persist=False)
        streaming.on_new_bar_5m()

    if kill_idx is None:
        raise RuntimeError("kill_at_ts is past the end of df_5m")

    # SIMULATED CRASH: serialize engine state, throw away the wrapper.
    state = streaming.serialize_engine_state()
    state_json = json.dumps(state, default=str)
    sigs_pre = list(sigs)
    print(f"  [drill] killed at idx={kill_idx} ts={pd.Timestamp(df_5m['timestamp'].iloc[kill_idx])}")
    print(f"  [drill] events emitted pre-crash: {len(sigs_pre)}")
    print(f"  [drill] checkpoint size: {len(state_json):,} bytes")

    # RESTART: fresh buffers + REST gap-fill simulation. In a real
    # restart the bot would hydrate from SQLite + REST. We simulate by
    # rebuilding the buffer from df_5m up to kill_idx, then continuing.
    buffers2 = PairBuffers(pair)
    for _, row in df_1h_pre.iterrows():
        buffers2.b1h.append_closed(_make_bar(row), persist=False)
    # Replay bars up to kill_idx into buffers2.
    j_1h2 = 0
    for i in range(kill_idx):
        ts5 = pd.Timestamp(df_5m["timestamp"].iloc[i])
        while j_1h2 < len(df_1h_stream) and pd.Timestamp(df_1h_stream["timestamp"].iloc[j_1h2]) <= ts5:
            buffers2.b1h.append_closed(_make_bar(df_1h_stream.iloc[j_1h2]), persist=False)
            j_1h2 += 1
        buffers2.b5m.append_closed(_make_bar(df_5m.iloc[i]), persist=False)

    streaming2 = StreamingEngine(pair=pair, buffers=buffers2, on_event=on_event_post)
    state_back = json.loads(state_json)
    streaming2.load_engine_state(state_back, seen_anchors=seen_after_restart)
    print(f"  [drill] restored. continuing from idx={kill_idx}.")

    # Continue with bars from kill_idx onwards.
    j_1h3 = j_1h2
    for i in range(kill_idx, n5):
        ts5 = pd.Timestamp(df_5m["timestamp"].iloc[i])
        while j_1h3 < len(df_1h_stream) and pd.Timestamp(df_1h_stream["timestamp"].iloc[j_1h3]) <= ts5:
            buffers2.b1h.append_closed(_make_bar(df_1h_stream.iloc[j_1h3]), persist=False)
            j_1h3 += 1
        buffers2.b5m.append_closed(_make_bar(df_5m.iloc[i]), persist=False)
        streaming2.on_new_bar_5m()

    final_state = streaming2.serialize_engine_state()
    return sigs, final_state


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--pair", default="SOL/USDT")
    p.add_argument("--start", default="2026-04-01")
    p.add_argument("--end", default="2026-05-04")
    p.add_argument("--kill-ts", default="2026-04-15 12:00:00",
                   help="UTC timestamp at which to simulate the crash.")
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
    kill_ts = pd.Timestamp(args.kill_ts, tz="UTC")

    print(f"=== Restart-drill parity {pair} {args.start} -> {args.end} (kill at {args.kill_ts}) ===")
    print(f"  5m: {len(df_5m):,}   1h: {len(df_1h):,}")

    print()
    print("BASELINE: uninterrupted streaming run")
    baseline_sigs, baseline_state = _run_baseline(pair, df_5m, df_1h)
    print(f"  baseline events: {len(baseline_sigs)}")

    print()
    print("DRILL: kill mid-stream, serialize, restore, continue")
    drill_sigs, drill_state = _run_drill(pair, df_5m, df_1h, kill_ts)
    print(f"  drill events:    {len(drill_sigs)}")

    print()
    if baseline_sigs == drill_sigs:
        print(f"  EVENT LOG: BYTE-IDENTICAL  ({len(baseline_sigs)} events)")
    else:
        print(f"  EVENT LOG: DIFFER  (baseline={len(baseline_sigs)}, drill={len(drill_sigs)})")
        n = min(len(baseline_sigs), len(drill_sigs))
        diffs = [(i, baseline_sigs[i], drill_sigs[i]) for i in range(n) if baseline_sigs[i] != drill_sigs[i]]
        for i, b, d in diffs[:5]:
            print(f"    [{i}] baseline: {b}")
            print(f"        drill:    {d}")
        raise SystemExit(1)

    # Compare final state JSON.
    baseline_json = json.dumps(baseline_state, default=str, sort_keys=True)
    drill_json = json.dumps(drill_state, default=str, sort_keys=True)
    if baseline_json == drill_json:
        print(f"  FINAL STATE: BYTE-IDENTICAL")
    else:
        print(f"  FINAL STATE: DIFFER")
        # Show first 500 chars of each.
        for k in sorted(set(baseline_state.keys()) | set(drill_state.keys())):
            bv = json.dumps(baseline_state.get(k), default=str, sort_keys=True)
            dv = json.dumps(drill_state.get(k), default=str, sort_keys=True)
            if bv != dv:
                print(f"    key '{k}' differs:")
                print(f"      baseline: {bv[:200]}")
                print(f"      drill:    {dv[:200]}")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
