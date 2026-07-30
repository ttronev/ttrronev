"""In-memory rolling bar buffer per (pair, timeframe), persisted to SQLite.

The buffer is the SOURCE OF TRUTH for engine state in approach (a):
  - On each closed live bar, append to buffer + persist to SQLite.
  - On each new bar, hand the entire buffer to the streaming engine
    wrapper, which slices it appropriately and re-runs the batch engine.
  - On restart, hydrate buffer from SQLite (already persisted) +
    fetch any missed bars via REST.

Buffer eviction: keep at most BUFFER_5M / BUFFER_1H bars in memory; SQLite
keeps everything for forensic analysis. We only ever read the in-memory
buffer when invoking the engine — SQLite is the disaster-recovery copy.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import pandas as pd

from paper_trade.config import BUFFER_5M, BUFFER_1H, EXEC_TF, TREND_TF
from paper_trade.sqlite_store import upsert_bar, fetch_bars, latest_bar_ts


@dataclass(frozen=True)
class Bar:
    timestamp_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float

    def to_row(self) -> dict:
        return {
            "timestamp": pd.Timestamp(self.timestamp_ms, unit="ms", tz="UTC"),
            "open": self.open, "high": self.high, "low": self.low,
            "close": self.close, "volume": self.volume,
        }


def _max_for_tf(tf: str) -> int:
    if tf == "5m":
        return BUFFER_5M
    if tf == "1h":
        return BUFFER_1H
    raise ValueError(f"unsupported tf: {tf}")


class BarBuffer:
    """Per-(pair, tf) rolling buffer."""

    def __init__(self, pair: str, tf: str):
        self.pair = pair
        self.tf = tf
        self.max_bars = _max_for_tf(tf)
        self._bars: list[Bar] = []

    # --- Hydration ----------------------------------------------------

    def hydrate_from_sqlite(self) -> int:
        """Load the most recent `max_bars` from SQLite into memory.
        Returns number of bars hydrated."""
        rows = fetch_bars(self.pair, self.tf, limit=self.max_bars)
        self._bars = [
            Bar(int(r["timestamp_ms"]), float(r["open"]), float(r["high"]),
                float(r["low"]), float(r["close"]), float(r["volume"]))
            for r in rows
        ]
        return len(self._bars)

    # --- Append --------------------------------------------------------

    def append_closed(self, bar: Bar, persist: bool = True, inserted_ms: Optional[int] = None) -> bool:
        """Append a closed bar. Returns True if appended (new ts), False
        if it was a duplicate of the latest buffered bar."""
        if self._bars and self._bars[-1].timestamp_ms == bar.timestamp_ms:
            return False
        if self._bars and self._bars[-1].timestamp_ms > bar.timestamp_ms:
            # Out-of-order — should never happen for closed bars but guard.
            return False
        self._bars.append(bar)
        if len(self._bars) > self.max_bars:
            # Evict oldest from in-memory buffer; SQLite keeps the row.
            del self._bars[0:len(self._bars) - self.max_bars]
        if persist:
            import time as _t
            ins = inserted_ms if inserted_ms is not None else int(_t.time() * 1000)
            upsert_bar(self.pair, self.tf, bar.timestamp_ms,
                       bar.open, bar.high, bar.low, bar.close, bar.volume, ins)
        return True

    # --- Read --------------------------------------------------------

    def to_dataframe(self) -> pd.DataFrame:
        if not self._bars:
            return pd.DataFrame(
                columns=["timestamp", "open", "high", "low", "close", "volume"]
            )
        return pd.DataFrame([b.to_row() for b in self._bars])

    def __len__(self) -> int:
        return len(self._bars)

    def latest(self) -> Optional[Bar]:
        return self._bars[-1] if self._bars else None

    def latest_ts_ms(self) -> Optional[int]:
        return self._bars[-1].timestamp_ms if self._bars else None


class PairBuffers:
    """Holder for one pair's 5m + 1h buffers."""

    def __init__(self, pair: str):
        self.pair = pair
        self.b5m = BarBuffer(pair, EXEC_TF)
        self.b1h = BarBuffer(pair, TREND_TF)

    def hydrate(self) -> tuple[int, int]:
        return self.b5m.hydrate_from_sqlite(), self.b1h.hydrate_from_sqlite()

    def latest_5m_ts(self) -> Optional[int]:
        return latest_bar_ts(self.pair, EXEC_TF)

    def latest_1h_ts(self) -> Optional[int]:
        return latest_bar_ts(self.pair, TREND_TF)


__all__ = ["Bar", "BarBuffer", "PairBuffers"]
