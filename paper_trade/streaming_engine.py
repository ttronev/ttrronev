"""Streaming wrapper around the batch SecondaryOnlyEngine.

Design (approach `a` — slice and re-batch):

  1. Maintain a rolling 5m + 1h bar buffer per pair (in `bar_buffer`).
  2. On every closed 5m bar, hand the full buffer to the batch engine
     and re-run it. The engine processes deterministically.
  3. The engine fires event-callbacks for every state transition. We
     dedupe (kind, anchor_ts) to surface only NEW events vs the previous
     run; those are dispatched to downstream sinks (SQLite, Telegram,
     risk manager).

This is the core of Phase 1a. Equivalence with the batch engine is
property-tested (see `test_streaming_parity.py`): replaying historical
bars one-at-a-time through this wrapper must produce the exact same
event sequence as a single batch run on the full df.

Event "anchor" key:
  - candidate-bound events: (kind, candidate.bos_timestamp) — the BOS
    timestamp uniquely identifies a setup across runs even after buffer
    eviction shifts internal setup_ids.
  - candidate-less events (BOS_GATED_OUT, BOS_QUEUED): (kind, bar_ts).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import pandas as pd

from backtesting.secondary_only_engine import SecondaryOnlyEngine
from paper_trade.bar_buffer import PairBuffers
from paper_trade.config import (
    ENGINE_CONFIG, MIN_5M_FOR_ENGINE, MIN_1H_FOR_REGIME,
)


# Event kinds the engine emits — kept here as documentation + a
# canonical list the test compares against.
ENGINE_EVENT_KINDS: tuple[str, ...] = (
    "BOS_GATED_OUT",
    "BOS_QUEUED",
    "SETUP_OPENED",
    "FINALIZED",
    "CANCELLED_BELOW_MIN_SWING",
    "ARMED",
    "PRIMARY_TRIGGERED",
    "CANCELLED_PRE_FILL",
    "CANCELLED_BY_INVALIDATION",
    "FILLED",
    "RESOLVED_TP",
    "RESOLVED_SL",
    "RESOLVED_HARD_STOP",
    "OPEN_EOD",
)


@dataclass
class EngineEvent:
    """Normalized engine-emitted event with all info downstream needs."""
    kind: str
    pair: str
    bar_idx: int
    bar_ts: pd.Timestamp                 # bar at which transition occurred
    bos_timestamp: Optional[pd.Timestamp]  # setup anchor; None for unbound events
    bos_direction: Optional[str]
    candidate_snapshot: Optional[dict]   # frozen copy of candidate state at event

    def anchor(self) -> tuple:
        """Stable dedup key across re-runs."""
        if self.bos_timestamp is not None:
            return (self.kind, self.bos_timestamp)
        return (self.kind, self.bar_ts)


def _snapshot_candidate(c: Optional[dict]) -> Optional[dict]:
    """Shallow copy of the relevant candidate fields. The engine mutates
    the candidate dict over the setup's lifetime; we need a snapshot at
    transition time."""
    if c is None:
        return None
    keys = (
        "setup_id", "phase", "bos_timestamp", "bos_direction",
        "swing_high", "swing_low", "swing_size_pct",
        "swing_high_timestamp", "swing_low_timestamp",
        "fib_0", "fib_0_3", "fib_0_5", "fib_0_75", "fib_1_0", "fib_1_2",
        "entry_price", "sl_price", "tp_price",
        "secondary_direction",
        "setup_armed_timestamp", "primary_trigger_timestamp",
        "secondary_placed_timestamp", "secondary_filled_timestamp",
        "secondary_exit_timestamp",
        "exit_price", "secondary_outcome",
        "primary_trigger_observed", "primary_would_have_outcome",
        "r_planned", "r_realized", "position_size", "fees_quote", "gross_pnl_quote",
        "bias_1h", "btc_bias_1h", "hour_of_day_utc", "weekday_utc",
    )
    return {k: c.get(k) for k in keys}


@dataclass
class StreamingEngine:
    """One streaming wrapper per pair.

    **Refactor C1**: instead of slice-and-rebatch (which broke once the
    5m buffer evicted past ~24 days), this now uses the engine's new
    `step()` API. State is held on a SINGLE persistent
    SecondaryOnlyEngine instance per pair; bars are fed one-by-one via
    `engine.step()`. Buffer eviction in the underlying bar buffer is
    harmless — analyzer state lives on the engine, not the buffer.

    On each new closed 5m bar the wrapper:
      1. Calls `engine.append_1h()` for any 1h bars that closed since
         the last 5m advance.
      2. Calls `engine.step()` with the new 5m bar.
      3. Captures any events emitted via the engine's event_callback.
      4. Dedupes by (kind, bos_timestamp) to avoid double-emission
         after restart-replay.
    """
    pair: str
    buffers: PairBuffers
    on_event: Callable[[EngineEvent], None] = lambda e: None
    cfg: dict = field(default_factory=lambda: dict(ENGINE_CONFIG))
    account_value: float = 10_000.0

    # Internal state.
    _seen_anchors: set = field(default_factory=set)
    _engine: Optional[SecondaryOnlyEngine] = None
    _last_1h_appended_ts_ms: Optional[int] = None
    _last_5m_processed_ts_ms: Optional[int] = None
    _started: bool = False

    def __post_init__(self) -> None:
        # Don't construct the engine here — defer until first
        # on_new_bar_5m so __init__ is cheap. Engine is built lazily
        # OR restored explicitly via load_engine_state().
        if self._engine is None:
            self._engine = SecondaryOnlyEngine(self.cfg, account_value=self.account_value)

    # -- restore from checkpoint --

    def load_engine_state(self, state_dict: dict, seen_anchors: Optional[set] = None) -> None:
        """Restore the engine's internal state from a previously
        serialized checkpoint. Optionally restore `_seen_anchors` so
        replay-after-restart doesn't re-emit committed events."""
        self._engine = SecondaryOnlyEngine.from_state(
            state_dict, self.cfg, account_value=self.account_value
        )
        if seen_anchors is not None:
            self._seen_anchors = set(seen_anchors)
        # last_processed_ts is implicit via engine._n_5m and the cached
        # ts_5m_list; we don't need an explicit field here.
        if self._engine._ts_5m_list:
            self._last_5m_processed_ts_ms = int(
                pd.Timestamp(self._engine._ts_5m_list[-1]).value // 10**6
            )
        if self._engine._ts_1h_ns_list:
            self._last_1h_appended_ts_ms = int(self._engine._ts_1h_ns_list[-1] // 10**6)
        self._started = True

    def serialize_engine_state(self) -> dict:
        """Serialize the underlying engine state for checkpointing."""
        return self._engine.serialize() if self._engine else {}

    def candidate_status(self) -> str:
        """Human-readable single-line summary of the active candidate
        (or "no active setup" when there's none). Used in the Telegram
        startup summary and the periodic heartbeat so the operator
        always knows whether the engine is currently tracking
        anything. Read-only — does NOT touch engine state.

        Examples:
          "no active setup"
          "armed long  bos 04-25 16:35  entry@85.17 SL@84.99 TP@85.63"
          "primary triggered short  bos 04-21 14:30  entry@84.50 ..."
        """
        if self._engine is None or self._engine._candidate is None:
            return "no active setup"
        c = self._engine._candidate
        phase = c.get("phase") or "?"
        # Phase label, lower-case + readable
        phase_label = {
            "AWAITING_RATCHET": "awaiting ratchet",
            "AWAITING_ARM": "awaiting arm",
            "AWAITING_PRIMARY_TRIG": "armed",
            "SECONDARY_PENDING": "primary triggered",
            "SECONDARY_LIVE": "filled",
        }.get(phase, phase.lower())
        side = (c.get("secondary_direction")
                or ("long" if c.get("bos_direction") == "bearish" else "short"))
        bos_ts = c.get("bos_timestamp")
        bos_str = (pd.Timestamp(bos_ts).strftime("%m-%d %H:%M")
                   if bos_ts is not None else "?")
        # Show entry/SL/TP only when fibs have been computed (post-FINALIZED).
        entry = c.get("entry_price"); sl = c.get("sl_price"); tp = c.get("tp_price")
        if entry is not None and sl is not None and tp is not None:
            return (f"{phase_label} {side}  bos {bos_str}  "
                    f"entry@{entry:.4g} SL@{sl:.4g} TP@{tp:.4g}")
        return f"{phase_label} {side}  bos {bos_str}  (fibs pending)"

    # -- main entry: new closed 5m bar --

    def on_new_bar_5m(self) -> list[EngineEvent]:
        """Process any not-yet-processed 5m bars in the buffer (and any
        new 1h bars). Returns the list of NEW (deduped) events emitted
        on this call. Events from historical buffer bars on first call
        flow through the normal callback; dedup against `_seen_anchors`
        suppresses re-emission after restart."""
        b5m_bars = self.buffers.b5m._bars
        b1h_bars = self.buffers.b1h._bars
        if not b5m_bars:
            return []
        if len(b5m_bars) < MIN_5M_FOR_ENGINE:
            return []
        # NOTE: previously gated on `len(b1h_bars) < MIN_1H_FOR_REGIME`
        # to avoid running the engine on a too-small 1h history. With
        # the C1 refactor that's no longer necessary — the engine's
        # `_regime_gate_passes_step` correctly returns False for any
        # 5m bar whose mapped 1h idx is below the 90d-lookback
        # threshold, which produces BOS_GATED_OUT events that match
        # batch behavior for the same time-series. Gating here would
        # additionally cause 5m buffer eviction for pairs whose 1h
        # history starts at the test window (e.g. AVAX/LINK 18mo),
        # which would silently lose early-period events.

        events_this_call: list[EngineEvent] = []

        def cb(kind: str, candidate: Optional[dict], bar_idx: int,
               bar_ts: Optional[pd.Timestamp]) -> None:
            if kind == "OPEN_EOD":
                return  # batch-only artifact; not meaningful in streaming
            snap = _snapshot_candidate(candidate)
            ev = EngineEvent(
                kind=kind,
                pair=self.pair,
                bar_idx=int(bar_idx),
                bar_ts=pd.Timestamp(bar_ts) if bar_ts is not None else None,
                bos_timestamp=(pd.Timestamp(candidate["bos_timestamp"])
                               if candidate is not None and candidate.get("bos_timestamp") is not None
                               else None),
                bos_direction=(str(candidate.get("bos_direction"))
                               if candidate is not None else None),
                candidate_snapshot=snap,
            )
            events_this_call.append(ev)

        self._engine._event_cb = cb

        # Walk both 5m and 1h timelines in chronological order. Feed
        # any not-yet-seen 1h bars before any 5m bar with a later ts;
        # this matches the order events would arrive in production WS
        # and ensures the regime gate sees correct 1h state at each
        # 5m's processing time.
        i5 = 0
        i1 = 0
        n5 = len(b5m_bars); n1 = len(b1h_bars)
        # Skip already-processed bars in each stream.
        while i5 < n5 and (self._last_5m_processed_ts_ms is not None
                           and b5m_bars[i5].timestamp_ms <= self._last_5m_processed_ts_ms):
            i5 += 1
        while i1 < n1 and (self._last_1h_appended_ts_ms is not None
                           and b1h_bars[i1].timestamp_ms <= self._last_1h_appended_ts_ms):
            i1 += 1
        while i5 < n5 or i1 < n1:
            t5 = b5m_bars[i5].timestamp_ms if i5 < n5 else None
            t1 = b1h_bars[i1].timestamp_ms if i1 < n1 else None
            # Append 1h bars whose ts <= next 5m's ts. Convention: 1h
            # at the boundary goes BEFORE the 5m bar, so the 1h state
            # is current when the 5m's gate is evaluated.
            if t1 is not None and (t5 is None or t1 <= t5):
                ts_pd = pd.Timestamp(t1, unit="ms", tz="UTC")
                self._engine.append_1h(ts_pd, b1h_bars[i1].close)
                self._engine.append_btc_1h(ts_pd, b1h_bars[i1].close)
                self._last_1h_appended_ts_ms = t1
                i1 += 1
                continue
            # Otherwise, feed the next 5m bar.
            bar = b5m_bars[i5]
            ts_pd = pd.Timestamp(bar.timestamp_ms, unit="ms", tz="UTC")
            self._engine.step(ts_pd, bar.open, bar.high, bar.low, bar.close)
            self._last_5m_processed_ts_ms = bar.timestamp_ms
            i5 += 1

        self._engine._event_cb = None
        self._started = True

        # Dedup against seen anchors.
        new_events: list[EngineEvent] = []
        for ev in events_this_call:
            key = ev.anchor()
            if key in self._seen_anchors:
                continue
            self._seen_anchors.add(key)
            new_events.append(ev)

        for ev in new_events:
            try:
                self.on_event(ev)
            except Exception:
                pass

        return new_events

    # -- 1m intrabar fill-detection path --

    def on_new_bar_1m(self, ts_ms: int, high: float, low: float
                      ) -> list[EngineEvent]:
        """Process a closed 1m bar for sub-5m fill detection. Routes to
        the engine's `intrabar_step()` which advances ONLY the active
        candidate's wick-based phases (ARMED, PRIMARY_TRIGGERED, FILLED,
        RESOLVED_TP, RESOLVED_HARD_STOP). Close-based transitions stay
        on the 5m timeline.

        The dedup keyset is shared with `on_new_bar_5m`, so when the
        next 5m bar closes after an intrabar fire, the engine's regular
        5m step() will not re-emit the same event (phase has already
        advanced) — and even if it did, the (kind, bos_timestamp)
        anchor matches and dedup suppresses it.

        Returns the list of NEW events emitted on this 1m frame
        (post-dedup). Most 1m bars produce zero events.
        """
        if self._engine is None:
            return []
        if self._engine._candidate is None:
            return []
        ts_pd = pd.Timestamp(ts_ms, unit="ms", tz="UTC")
        try:
            raw = self._engine.intrabar_step(ts_pd, high, low)
        except Exception:
            return []
        if not raw:
            return []
        new_events: list[EngineEvent] = []
        bar_idx = max(0, self._engine._n_5m - 1)
        for kind, candidate in raw:
            snap = _snapshot_candidate(candidate)
            ev = EngineEvent(
                kind=kind,
                pair=self.pair,
                bar_idx=int(bar_idx),
                bar_ts=ts_pd,
                bos_timestamp=(pd.Timestamp(candidate["bos_timestamp"])
                               if candidate is not None
                               and candidate.get("bos_timestamp") is not None
                               else None),
                bos_direction=(str(candidate.get("bos_direction"))
                               if candidate is not None else None),
                candidate_snapshot=snap,
            )
            key = ev.anchor()
            if key in self._seen_anchors:
                continue
            self._seen_anchors.add(key)
            new_events.append(ev)

        for ev in new_events:
            try:
                self.on_event(ev)
            except Exception:
                pass
        return new_events


__all__ = ["StreamingEngine", "EngineEvent", "ENGINE_EVENT_KINDS"]
