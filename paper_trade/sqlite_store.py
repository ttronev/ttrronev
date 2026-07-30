"""SQLite schema + connection helpers for paper-trade state.

Schema rationale: every event the bot observes is logged as a row in an
append-only table. State (open setups, current positions, equity) is
reconstructable by replaying these tables, so the DB is the system of
record. No "current_state.json" with mutable mirror-of-truth.

Tables:
    bars              - raw closed bars (5m + 1h), keyed by (pair, tf, timestamp_ms)
    signals           - BOS events that passed the regime gate (one row per finalized setup)
    setups            - setup lifecycle rows: ARMED / PRIMARY_TRIGGERED / FILLED / RESOLVED
    trades            - one row per closed simulated/live trade
    level_proximity   - per-setup snapshot of all level distances (logged metadata only)
    equity            - one row per equity-changing event (fill, exit, fee, funding)
    validation        - daily streaming-vs-batch divergence diff results
    runtime_events    - feed disconnects, restarts, errors, heartbeats
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional

import pandas as pd

from paper_trade.config import SQLITE_PATH


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS bars (
    pair          TEXT    NOT NULL,
    tf            TEXT    NOT NULL,
    timestamp_ms  INTEGER NOT NULL,
    open          REAL    NOT NULL,
    high          REAL    NOT NULL,
    low           REAL    NOT NULL,
    close         REAL    NOT NULL,
    volume        REAL    NOT NULL,
    inserted_ms   INTEGER NOT NULL,
    PRIMARY KEY (pair, tf, timestamp_ms)
);

CREATE INDEX IF NOT EXISTS idx_bars_pair_tf_ts
    ON bars (pair, tf, timestamp_ms);

CREATE TABLE IF NOT EXISTS signals (
    setup_id              INTEGER NOT NULL,
    pair                  TEXT    NOT NULL,
    bos_timestamp_ms      INTEGER NOT NULL,
    bos_direction         TEXT    NOT NULL,            -- bullish / bearish
    swing_high            REAL    NOT NULL,
    swing_low             REAL    NOT NULL,
    swing_size_pct        REAL    NOT NULL,
    fib_0                 REAL    NOT NULL,
    fib_0_3               REAL    NOT NULL,
    fib_0_5               REAL    NOT NULL,
    fib_0_75              REAL    NOT NULL,
    fib_1_0               REAL    NOT NULL,
    fib_1_2               REAL    NOT NULL,
    regime_gate_value_pct REAL,                        -- |90d return| at BOS
    regime_gate_passed    INTEGER NOT NULL,
    bias_1h               TEXT,
    btc_bias_1h           TEXT,
    PRIMARY KEY (pair, setup_id)
);

CREATE TABLE IF NOT EXISTS setups (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    pair              TEXT    NOT NULL,
    setup_id          INTEGER NOT NULL,
    setup_num         INTEGER,             -- per-pair sequential setup number; SOL-1, SOL-2, ...
    event             TEXT    NOT NULL,   -- SETUP_OPENED / FINALIZED / ARMED / PRIMARY_TRIGGERED / FILLED / RESOLVED_TP / RESOLVED_SL / CANCELLED_*
    timestamp_ms      INTEGER NOT NULL,
    bos_timestamp_ms  INTEGER,             -- setup anchor; nullable for very early events only
    bos_direction     TEXT,                -- 'bullish' / 'bearish'
    price             REAL,
    detail            TEXT
);

CREATE INDEX IF NOT EXISTS idx_setups_pair_id ON setups (pair, setup_id);

CREATE TABLE IF NOT EXISTS trades (
    pair                  TEXT    NOT NULL,
    setup_id              INTEGER NOT NULL,
    direction             TEXT    NOT NULL,
    bos_timestamp_ms      INTEGER NOT NULL,
    entry_timestamp_ms    INTEGER,
    exit_timestamp_ms     INTEGER,
    entry_price           REAL,
    sl_price              REAL,
    tp_price              REAL,
    exit_price            REAL,
    outcome               TEXT,
    r_realized            REAL,
    position_size         REAL,
    gross_pnl_quote       REAL,
    fees_quote            REAL,
    net_pnl_quote         REAL,
    leverage              REAL,                        -- Phase 1b: realized leverage
    intended_risk_dollars REAL,
    realized_risk_dollars REAL,
    slippage_entry        REAL,                        -- Phase 1b only
    slippage_exit         REAL,                        -- Phase 1b only
    funding_paid          REAL,                        -- Phase 1b only
    backtest_expectation_json TEXT,                    -- JSON: what backtest engine would have predicted
    PRIMARY KEY (pair, setup_id)
);

CREATE TABLE IF NOT EXISTS level_proximity (
    pair          TEXT    NOT NULL,
    setup_id      INTEGER NOT NULL,
    level_name    TEXT    NOT NULL,
    level_price   REAL,
    dist_pct      REAL,
    dist_R        REAL,
    abs_R         REAL,
    PRIMARY KEY (pair, setup_id, level_name)
);

CREATE TABLE IF NOT EXISTS equity (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp_ms  INTEGER NOT NULL,
    pair          TEXT,
    setup_id      INTEGER,
    delta_quote   REAL    NOT NULL,
    new_equity    REAL    NOT NULL,
    reason        TEXT    NOT NULL    -- fill_fee / exit_pnl / funding / restart_audit
);

CREATE TABLE IF NOT EXISTS validation (
    date_utc          TEXT NOT NULL,    -- YYYY-MM-DD
    pair              TEXT NOT NULL,
    streaming_count   INTEGER NOT NULL,
    batch_count       INTEGER NOT NULL,
    diverged          INTEGER NOT NULL,
    detail_json       TEXT,
    PRIMARY KEY (date_utc, pair)
);

-- Engine state checkpoint per (pair, kind). Single row per (pair,
-- kind) — INSERT OR REPLACE. JSON-serialized engine state from the
-- C1 refactor; restored on bot startup. See REFACTOR_C1_DESIGN.md.
CREATE TABLE IF NOT EXISTS engine_state (
    pair          TEXT NOT NULL,
    kind          TEXT NOT NULL,    -- 'engine'  (analyzer state nested inside)
    saved_ms      INTEGER NOT NULL,
    last_5m_ts_ms INTEGER,           -- for diagnostics
    state_json    TEXT NOT NULL,
    PRIMARY KEY (pair, kind)
);

-- Telegram dedup across restarts. Anchors are (kind, anchor_str).
-- See PHASE_1B_SPEC v2 §4 / event-semantics decision in
-- REFACTOR_C1_DESIGN.md.
CREATE TABLE IF NOT EXISTS telegram_seen_anchors (
    kind        TEXT NOT NULL,
    anchor_str  TEXT NOT NULL,
    sent_ms     INTEGER NOT NULL,
    PRIMARY KEY (kind, anchor_str)
);

CREATE TABLE IF NOT EXISTS runtime_events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp_ms  INTEGER NOT NULL,
    kind          TEXT    NOT NULL,    -- start / stop / ws_disconnect / ws_reconnect / error / heartbeat / restart_recovery
    detail        TEXT
);
"""


_LOCK = threading.RLock()
_CONN: Optional[sqlite3.Connection] = None


def _connect() -> sqlite3.Connection:
    global _CONN
    if _CONN is None:
        path = Path(SQLITE_PATH)
        path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False because the WS callback and the periodic
        # heartbeat may share the connection. _LOCK protects writes.
        _CONN = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        _CONN.execute("PRAGMA journal_mode=WAL;")
        _CONN.execute("PRAGMA synchronous=NORMAL;")
        _CONN.row_factory = sqlite3.Row
    return _CONN


def init_schema() -> None:
    with _LOCK:
        conn = _connect()
        conn.executescript(SCHEMA_SQL)
        _migrate_setup_num(conn)


def _migrate_setup_num(conn: sqlite3.Connection) -> None:
    """If `setups` predates the setup_num column, add it and backfill
    per-pair sequential numbers from existing rows. Idempotent: a
    second invocation is a no-op."""
    cur = conn.execute("PRAGMA table_info(setups)")
    cols = {row[1] for row in cur.fetchall()}
    if "setup_num" not in cols:
        conn.execute("ALTER TABLE setups ADD COLUMN setup_num INTEGER")
    # Backfill any rows where setup_num is NULL. Includes both the case
    # of a fresh ALTER and any historical rows missed by previous
    # migrations.
    null_count = conn.execute(
        "SELECT COUNT(*) FROM setups WHERE setup_num IS NULL"
    ).fetchone()[0]
    if null_count == 0:
        return
    # Per pair, enumerate distinct setup_ids in ascending order; assign
    # 1, 2, 3, ...
    rows = conn.execute(
        "SELECT DISTINCT pair, setup_id FROM setups WHERE setup_num IS NULL "
        "ORDER BY pair, setup_id"
    ).fetchall()
    counters: dict[str, int] = {}
    # Seed each pair's counter from the existing max(setup_num) so we
    # don't collide with rows that already had setup_num populated.
    for pair_row in conn.execute("SELECT DISTINCT pair FROM setups").fetchall():
        pair = pair_row[0]
        max_row = conn.execute(
            "SELECT MAX(setup_num) FROM setups WHERE pair = ?", (pair,)
        ).fetchone()
        counters[pair] = (max_row[0] or 0)
    for r in rows:
        pair, sid = r[0], int(r[1])
        counters[pair] = counters.get(pair, 0) + 1
        conn.execute(
            "UPDATE setups SET setup_num = ? WHERE pair = ? AND setup_id = ?",
            (counters[pair], pair, sid),
        )


@contextmanager
def tx() -> Iterator[sqlite3.Connection]:
    """Yield a connection in a BEGIN/COMMIT block under the global lock."""
    with _LOCK:
        conn = _connect()
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise


def query(sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
    with _LOCK:
        conn = _connect()
        cur = conn.execute(sql, tuple(params))
        return list(cur.fetchall())


def execute(sql: str, params: Iterable[Any] = ()) -> None:
    with _LOCK:
        conn = _connect()
        conn.execute(sql, tuple(params))


def executemany(sql: str, rows: Iterable[Iterable[Any]]) -> None:
    with _LOCK:
        conn = _connect()
        conn.executemany(sql, [tuple(r) for r in rows])


# --- Convenience writers ------------------------------------------------

def upsert_bar(pair: str, tf: str, ts_ms: int, o: float, h: float, l_: float,
               c: float, v: float, inserted_ms: int) -> None:
    execute(
        "INSERT OR REPLACE INTO bars(pair, tf, timestamp_ms, open, high, low, close, volume, inserted_ms) "
        "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (pair, tf, int(ts_ms), float(o), float(h), float(l_), float(c), float(v), int(inserted_ms)),
    )


def latest_bar_ts(pair: str, tf: str) -> Optional[int]:
    rows = query(
        "SELECT MAX(timestamp_ms) AS t FROM bars WHERE pair = ? AND tf = ?",
        (pair, tf),
    )
    if not rows or rows[0]["t"] is None:
        return None
    return int(rows[0]["t"])


def fetch_bars(pair: str, tf: str, limit: Optional[int] = None) -> list[dict]:
    if limit is None:
        rows = query(
            "SELECT timestamp_ms, open, high, low, close, volume FROM bars "
            "WHERE pair = ? AND tf = ? ORDER BY timestamp_ms ASC",
            (pair, tf),
        )
    else:
        rows = query(
            "SELECT timestamp_ms, open, high, low, close, volume FROM bars "
            "WHERE pair = ? AND tf = ? ORDER BY timestamp_ms DESC LIMIT ?",
            (pair, tf, int(limit)),
        )
        rows = list(reversed(rows))
    return [dict(r) for r in rows]


def save_engine_state(pair: str, state_json: str,
                      last_5m_ts_ms: Optional[int] = None) -> None:
    """Idempotent checkpoint write. INSERT OR REPLACE on (pair, kind)."""
    import time as _t
    execute(
        "INSERT OR REPLACE INTO engine_state(pair, kind, saved_ms, last_5m_ts_ms, state_json) "
        "VALUES(?, ?, ?, ?, ?)",
        (pair, "engine", int(_t.time() * 1000), last_5m_ts_ms, state_json),
    )


def load_engine_state(pair: str) -> Optional[tuple[str, int]]:
    """Returns (state_json, last_5m_ts_ms) or None if no checkpoint exists."""
    rows = query(
        "SELECT state_json, last_5m_ts_ms FROM engine_state WHERE pair = ? AND kind = 'engine'",
        (pair,),
    )
    if not rows:
        return None
    return rows[0]["state_json"], rows[0]["last_5m_ts_ms"]


def telegram_anchor_seen(kind: str, anchor_str: str) -> bool:
    rows = query(
        "SELECT 1 FROM telegram_seen_anchors WHERE kind = ? AND anchor_str = ? LIMIT 1",
        (kind, anchor_str),
    )
    return bool(rows)


def telegram_anchor_record(kind: str, anchor_str: str) -> None:
    import time as _t
    execute(
        "INSERT OR IGNORE INTO telegram_seen_anchors(kind, anchor_str, sent_ms) "
        "VALUES(?, ?, ?)",
        (kind, anchor_str, int(_t.time() * 1000)),
    )


def rebuild_seen_anchors_from_db(pair: str) -> set:
    """Rebuild StreamingEngine `_seen_anchors` from the SQLite signals
    + setups + runtime_events tables. Used on bot startup so replay-
    after-restart dedup catches events already committed downstream.

    Anchor format mirrors `EngineEvent.anchor()`:
      (kind, bos_timestamp_iso) for setup-bound events
      (kind, bar_ts_iso)        for setup-less events (BOS_QUEUED, BOS_GATED_OUT)
    """
    out: set = set()
    # signals: bos_timestamp_ms is the anchor.
    rows = query(
        "SELECT bos_timestamp_ms FROM signals WHERE pair = ?", (pair,),
    )
    sig_map = {}
    for r in rows:
        ts_iso = pd.Timestamp(int(r["bos_timestamp_ms"]), unit="ms", tz="UTC").isoformat()
        sig_map[r["bos_timestamp_ms"]] = ts_iso
        # FINALIZED is written when signals row is created.
        out.add(("FINALIZED", ts_iso))
    # setups: each lifecycle event already references bos_timestamp_ms.
    rows = query(
        "SELECT event, bos_timestamp_ms FROM setups WHERE pair = ?", (pair,),
    )
    for r in rows:
        bos_ms = r["bos_timestamp_ms"]
        if bos_ms is None:
            continue
        ts_iso = sig_map.get(bos_ms) or pd.Timestamp(int(bos_ms), unit="ms", tz="UTC").isoformat()
        out.add((r["event"], ts_iso))
    # runtime_events: BOS_QUEUED / BOS_GATED_OUT carry bar_ts in detail JSON.
    import json as _json
    rows = query(
        "SELECT kind, detail FROM runtime_events WHERE kind IN ('bos_queued', 'bos_gated_out')",
    )
    for r in rows:
        try:
            d = _json.loads(r["detail"]) if r["detail"] else {}
            if d.get("pair") == pair and d.get("bar_ts"):
                bar_ts = pd.Timestamp(d["bar_ts"]).isoformat()
                kind = "BOS_QUEUED" if r["kind"] == "bos_queued" else "BOS_GATED_OUT"
                out.add((kind, bar_ts))
        except Exception:
            pass
    return out


def log_runtime(kind: str, detail: Any = None) -> None:
    import time as _t
    payload = detail if isinstance(detail, str) else json.dumps(detail, default=str) if detail is not None else None
    execute(
        "INSERT INTO runtime_events(timestamp_ms, kind, detail) VALUES(?, ?, ?)",
        (int(_t.time() * 1000), kind, payload),
    )


__all__ = [
    "init_schema", "tx", "query", "execute", "executemany",
    "upsert_bar", "latest_bar_ts", "fetch_bars", "log_runtime",
    "save_engine_state", "load_engine_state",
    "telegram_anchor_seen", "telegram_anchor_record",
    "rebuild_seen_anchors_from_db",
]
