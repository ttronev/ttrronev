"""Daily streaming-vs-batch validator.

At 00:00 UTC, replay yesterday's bars through the batch engine and
compare the resulting event stream to what the streaming wrapper logged
to SQLite during the day. Any divergence is a bug.

Implementation:
  * Pull the prior UTC day's 5m + 1h bars from SQLite (extended on the
    1h side to cover the regime gate's 90d lookback — same as the live
    streaming engine sees).
  * Run the batch engine with event_callback. Build event signatures.
  * Pull the streaming engine's signatures from SQLite (signals,
    setups, trades tables — reconstruct the stream).
  * Compare. Write a row to `validation` table. Telegram alert on
    divergence (caller passes a TelegramNotifier).

Note: comparing against SQLite-derived signatures means we're testing
"what got persisted" matches "what the batch engine would emit" — this
catches both engine-streaming bugs and event-write bugs in one test.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Optional

import pandas as pd

from backtesting.secondary_only_engine import SecondaryOnlyEngine
from paper_trade.config import ENGINE_CONFIG, MIN_1H_FOR_REGIME
from paper_trade.sqlite_store import execute, fetch_bars, query, tx
from paper_trade.streaming_engine import _snapshot_candidate


def _utc_day_bounds(day_str: str) -> tuple[int, int]:
    """`day_str` = 'YYYY-MM-DD' UTC. Returns (start_ms, end_ms)."""
    d = datetime.strptime(day_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    start_ms = int(d.timestamp() * 1000)
    end_ms = int((d + timedelta(days=1)).timestamp() * 1000) - 1
    return start_ms, end_ms


def _bars_from_db(pair: str, tf: str) -> pd.DataFrame:
    rows = fetch_bars(pair, tf)
    if not rows:
        return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])
    df = pd.DataFrame(rows)
    df["timestamp"] = pd.to_datetime(df["timestamp_ms"], unit="ms", utc=True)
    return df[["timestamp", "open", "high", "low", "close", "volume"]].reset_index(drop=True)


def _batch_signatures_for_day(pair: str, day_str: str) -> list[tuple]:
    """Run the batch engine on FULL 5m history up to end-of-day, then
    filter events to those whose bar_ts falls within the validation
    day. (Slicing the 5m input itself would change BOS detections —
    the structure analyzer needs prior history.)"""
    df_5m_full = _bars_from_db(pair, "5m")
    df_1h_full = _bars_from_db(pair, "1h")
    if df_5m_full.empty:
        return []
    start_ms, end_ms = _utc_day_bounds(day_str)
    start_ts = pd.Timestamp(start_ms, unit="ms", tz="UTC")
    end_ts = pd.Timestamp(end_ms, unit="ms", tz="UTC")

    df_5m = df_5m_full[df_5m_full["timestamp"] <= end_ts].reset_index(drop=True)
    df_1h = df_1h_full[df_1h_full["timestamp"] <= end_ts].reset_index(drop=True)
    if len(df_1h) < MIN_1H_FOR_REGIME or len(df_5m) == 0:
        return []

    cfg = dict(ENGINE_CONFIG)
    eng = SecondaryOnlyEngine(cfg, account_value=10_000.0)
    sigs: list[tuple] = []

    def cb(kind: str, candidate: Optional[dict], bar_idx: int,
           bar_ts: Optional[pd.Timestamp]) -> None:
        # Skip non-decision events.
        if kind in ("OPEN_EOD", "BOS_GATED_OUT", "BOS_QUEUED"):
            return
        if bar_ts is None:
            return
        bts = pd.Timestamp(bar_ts)
        if bts < start_ts or bts > end_ts:
            return
        snap = _snapshot_candidate(candidate)
        bos = snap.get("bos_timestamp") if snap else None
        # Trade-decision signature: kind + setup anchor + direction +
        # event bar_ts. We deliberately exclude internal-state details
        # (swing_high/low are provisional at SETUP_OPENED; phase is
        # ambiguous for CANCELLED_BY_INVALIDATION which fires from
        # two phases). The streaming-parity test (test_streaming_parity)
        # already proves byte-equivalence on the raw engine event
        # stream — the daily validator's job here is just to confirm
        # trade decisions reach SQLite the same way live as in batch.
        sig = (
            kind,
            str(pd.Timestamp(bos)) if bos is not None else None,
            snap.get("bos_direction") if snap else None,
            str(bts),
        )
        sigs.append(sig)

    eng.run(df_5m, df_1h, df_1h, event_callback=cb)
    return sigs


def _streaming_signatures_for_day(pair: str, day_str: str) -> list[tuple]:
    """Reconstruct event signatures from what was persisted to SQLite
    by the streaming engine during the day. Signature shape mirrors
    `_batch_signatures_for_day`: (kind, bos_timestamp, bos_direction,
    bar_ts)."""
    start_ms, end_ms = _utc_day_bounds(day_str)
    # SETUP_OPENED rows are written before signals exist, but signals
    # is populated at FINALIZED time which always follows. By the time
    # we validate (00:05 UTC for prior day), every SETUP_OPENED that
    # made it to FINALIZED has a signals row.
    # SETUP_OPENED that never finalized (cancelled_below_min_swing)
    # also never write signals — but the cancel happens on the same
    # bar as SETUP_OPENED so the same setup_id appears in setups twice
    # back-to-back; we look up signals first and fall back to None.
    # bos_timestamp_ms + bos_direction live directly on setups now,
    # so reconstruction is a single query — no signals lookup needed.
    rows = query(
        "SELECT event, timestamp_ms, bos_timestamp_ms, bos_direction FROM setups "
        "WHERE pair = ? AND timestamp_ms >= ? AND timestamp_ms <= ? "
        "ORDER BY id ASC",
        (pair, start_ms, end_ms),
    )
    out: list[tuple] = []
    for r in rows:
        bos_ms = r["bos_timestamp_ms"]
        bos_ts = (str(pd.Timestamp(int(bos_ms), unit="ms", tz="UTC"))
                  if bos_ms is not None else None)
        bar_ts = str(pd.Timestamp(int(r["timestamp_ms"]), unit="ms", tz="UTC"))
        out.append((r["event"], bos_ts, r["bos_direction"], bar_ts))
    return out


def validate_day(pair: str, day_str: str) -> dict:
    """Run the comparison and persist a row in `validation`."""
    batch_sigs = _batch_signatures_for_day(pair, day_str)
    streaming_sigs = _streaming_signatures_for_day(pair, day_str)
    diverged = batch_sigs != streaming_sigs
    detail = None
    if diverged:
        # Truncate detail — full lists can be big.
        detail = json.dumps({
            "first_batch": batch_sigs[:5],
            "first_streaming": streaming_sigs[:5],
            "batch_total": len(batch_sigs),
            "streaming_total": len(streaming_sigs),
        }, default=str)
    with tx() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO validation"
            "(date_utc, pair, streaming_count, batch_count, diverged, detail_json) "
            "VALUES(?, ?, ?, ?, ?, ?)",
            (day_str, pair, len(streaming_sigs), len(batch_sigs), 1 if diverged else 0, detail),
        )
    return {
        "pair": pair, "day": day_str,
        "batch": len(batch_sigs), "streaming": len(streaming_sigs),
        "diverged": diverged,
    }


__all__ = ["validate_day"]
