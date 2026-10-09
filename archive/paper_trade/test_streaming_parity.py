"""Parity test: streaming wrapper output == batch engine output.

The entire point of Phase 1a is to validate that running the engine on
a rolling buffer (one bar at a time) produces the EXACT same event
sequence as running the engine once on the full DataFrame.

Test strategy:
  1. Load 18mo of SOL 5m + 1h.
  2. Run batch engine once with event-callback → reference event log.
  3. Initialize StreamingEngine with empty buffer.
  4. Feed 1h bars first (so regime gate is ready), then iterate 5m bars
     one at a time, calling on_new_bar_5m() after each append.
  5. Collect all events emitted by the streaming wrapper.
  6. Assert: streaming_events == batch_events (anchor-keyed comparison).

Tolerance: ZERO. If any event differs in (kind, bos_timestamp, bar_ts,
relevant candidate fields), the test fails. This is the gate Phase 1a
must pass before we trust live execution.

Run:
    python -m paper_trade.test_streaming_parity --pair SOL/USDT
        --start 2025-01-01 --end 2025-04-01

(Default range is short to keep the test under a minute. Run on full
18mo before declaring Phase 1a ready.)
"""

from __future__ import annotations

import argparse
import time
from typing import Any, Optional

import pandas as pd

from backtesting.secondary_only_engine import SecondaryOnlyEngine
from paper_trade.bar_buffer import Bar, PairBuffers
from paper_trade.config import ENGINE_CONFIG
from paper_trade.streaming_engine import EngineEvent, StreamingEngine, _snapshot_candidate


def _to_anchor(kind: str, candidate: Optional[dict],
               bar_ts: Optional[pd.Timestamp]) -> tuple:
    if candidate is not None and candidate.get("bos_timestamp") is not None:
        return (kind, pd.Timestamp(candidate["bos_timestamp"]))
    return (kind, pd.Timestamp(bar_ts) if bar_ts is not None else None)


def _event_signature(ev_kind: str, candidate_snapshot: Optional[dict],
                     bar_ts: Optional[pd.Timestamp]) -> tuple:
    """Subset of fields that must match exactly between batch and stream."""
    if candidate_snapshot is None:
        return (ev_kind, None, str(bar_ts))
    bos = candidate_snapshot.get("bos_timestamp")
    sig = (
        ev_kind,
        str(pd.Timestamp(bos)) if bos is not None else None,
        candidate_snapshot.get("bos_direction"),
        round(float(candidate_snapshot.get("swing_high") or float("nan")), 6),
        round(float(candidate_snapshot.get("swing_low") or float("nan")), 6),
        candidate_snapshot.get("phase"),
    )
    return sig


def run_batch(df_5m: pd.DataFrame, df_1h: pd.DataFrame) -> list[tuple]:
    cfg = dict(ENGINE_CONFIG)
    eng = SecondaryOnlyEngine(cfg, account_value=10_000.0)
    sigs: list[tuple] = []

    def cb(kind: str, candidate: Optional[dict], bar_idx: int,
           bar_ts: Optional[pd.Timestamp]) -> None:
        # OPEN_EOD is a batch-only artifact (setup still live at end of
        # data). Streaming never emits it, so for parity we strip it
        # here too. See streaming_engine.py for rationale.
        if kind == "OPEN_EOD":
            return
        snap = _snapshot_candidate(candidate)
        sigs.append(_event_signature(kind, snap, bar_ts))

    eng.run(df_5m, df_1h, df_1h, event_callback=cb)
    return sigs


def _make_bar(row) -> Bar:
    return Bar(int(pd.Timestamp(row["timestamp"]).value // 10**6),
               float(row["open"]), float(row["high"]), float(row["low"]),
               float(row["close"]), float(row["volume"]))


def run_streaming(pair: str, df_5m: pd.DataFrame, df_1h: pd.DataFrame) -> list[tuple]:
    """Feed bars to the streaming wrapper one-at-a-time, **co-streaming
    1h with 5m chronologically** (Option A from the parity-bug fix).

    Mirrors the production data flow:
      * Pre-load the 1h buffer with the LAST `BUFFER_1H` bars closed
        BEFORE the 5m streaming window starts. This is what the live
        bot's REST gap-fill seeds at startup.
      * Walk df_5m chronologically. Before each 5m bar's engine call,
        append any 1h bars whose timestamp <= the 5m bar's timestamp.
        This is what the live WS feed delivers in real time (1h bars
        close at the top of each hour and arrive between 5m bars).

    The PRIOR version pre-loaded ALL 1h history at once, which capped
    the buffer at the LAST 3500 bars (~146 days) covering only the END
    of the streaming window. Early 5m bars in the window had no 1h
    coverage, so map_5m_to_1h_idx returned -1 and the regime gate
    falsely failed for ~400 days of bars. That's not a real production
    scenario — production never streams 5m bars from outside the 1h
    buffer's coverage."""
    from paper_trade.config import MIN_5M_FOR_ENGINE

    buffers = PairBuffers(pair)

    sigs: list[tuple] = []

    def on_event(ev: EngineEvent) -> None:
        sigs.append(_event_signature(ev.kind, ev.candidate_snapshot, ev.bar_ts))

    streaming = StreamingEngine(pair=pair, buffers=buffers, on_event=on_event)

    n5 = len(df_5m)
    if n5 == 0:
        return sigs
    stream_start_ts = pd.Timestamp(df_5m["timestamp"].iloc[0])

    # Phase 1: pre-load 1h bars BEFORE the 5m stream starts (mimics
    # production REST gap-fill of the last BUFFER_1H bars). The buffer
    # caps at BUFFER_1H, so feeding all earlier history naturally
    # leaves only the most recent BUFFER_1H bars resident.
    df_1h_pre = df_1h[df_1h["timestamp"] < stream_start_ts]
    for _, row in df_1h_pre.iterrows():
        buffers.b1h.append_closed(_make_bar(row), persist=False)

    # Phase 2: 1h bars at or after stream_start are co-streamed.
    df_1h_stream = df_1h[df_1h["timestamp"] >= stream_start_ts].reset_index(drop=True)
    j_1h = 0  # iterator into df_1h_stream

    # Phase 3: warmup 5m bars, no engine call. Co-stream 1h alongside.
    warmup = min(MIN_5M_FOR_ENGINE - 1, n5)
    for i in range(warmup):
        ts5 = pd.Timestamp(df_5m["timestamp"].iloc[i])
        while j_1h < len(df_1h_stream) and pd.Timestamp(df_1h_stream["timestamp"].iloc[j_1h]) <= ts5:
            buffers.b1h.append_closed(_make_bar(df_1h_stream.iloc[j_1h]), persist=False)
            j_1h += 1
        buffers.b5m.append_closed(_make_bar(df_5m.iloc[i]), persist=False)

    # Phase 4: stream 5m + co-stream 1h, calling engine on every 5m close.
    last_pct = -1
    for i in range(warmup, n5):
        ts5 = pd.Timestamp(df_5m["timestamp"].iloc[i])
        while j_1h < len(df_1h_stream) and pd.Timestamp(df_1h_stream["timestamp"].iloc[j_1h]) <= ts5:
            buffers.b1h.append_closed(_make_bar(df_1h_stream.iloc[j_1h]), persist=False)
            j_1h += 1
        buffers.b5m.append_closed(_make_bar(df_5m.iloc[i]), persist=False)
        streaming.on_new_bar_5m()
        pct = int((i - warmup) * 100 / max(1, n5 - warmup))
        if pct != last_pct and pct % 10 == 0:
            print(f"    streaming: {pct}% ({i - warmup}/{n5 - warmup} bars)")
            last_pct = pct
    return sigs


def diff_event_lists(batch: list[tuple], streaming: list[tuple]
                     ) -> tuple[bool, list[str]]:
    msgs: list[str] = []
    if len(batch) != len(streaming):
        msgs.append(f"length mismatch: batch={len(batch)}  streaming={len(streaming)}")
    n = min(len(batch), len(streaming))
    diffs = 0
    for i in range(n):
        if batch[i] != streaming[i]:
            diffs += 1
            if diffs <= 10:
                msgs.append(f"  [{i}] batch={batch[i]}  streaming={streaming[i]}")
    if diffs > 10:
        msgs.append(f"  ... and {diffs - 10} more")
    return (len(msgs) == 0, msgs)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--pair", default="SOL/USDT")
    p.add_argument("--start", default="2026-04-01",
                   help="UTC start of test window for 5m bars. Defaults to ~1 month.")
    p.add_argument("--end", default="2026-05-04")
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
    # Use full 1h history so the regime gate has its 90-day rolling window.
    df_1h = df_1h_full[df_1h_full["timestamp"] <= end].reset_index(drop=True)

    print(f"=== Parity test {pair} {args.start} -> {args.end} ===")
    print(f"  5m bars: {len(df_5m):,}   1h bars: {len(df_1h):,}")

    t0 = time.time()
    batch_sigs = run_batch(df_5m, df_1h)
    t_batch = time.time() - t0
    print(f"  batch run: {t_batch:.1f}s   {len(batch_sigs)} events")

    t0 = time.time()
    stream_sigs = run_streaming(pair, df_5m, df_1h)
    t_stream = time.time() - t0
    print(f"  streaming run: {t_stream:.1f}s   {len(stream_sigs)} events")

    ok, msgs = diff_event_lists(batch_sigs, stream_sigs)
    if ok:
        print()
        print(f"  PARITY: PASS  ({len(batch_sigs)} events identical)")
    else:
        print()
        print(f"  PARITY: FAIL")
        for m in msgs:
            print(m)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
