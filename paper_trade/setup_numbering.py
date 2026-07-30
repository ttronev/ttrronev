"""Per-pair sequential setup number allocator.

Engine assigns global `setup_id` (incrementing across all pairs in a
single run; resets per engine instance). For Telegram display we want
per-pair numbering: SOL-1, SOL-2, AVAX-1, etc. — and that numbering
must persist across bot restarts.

Mechanism: on bot startup, query SQLite for the max setup_num per pair
and seed an in-memory counter at that max + 1. Map (pair, engine
setup_id) -> setup_num; reuse the same number for every event that
references the same setup.
"""

from __future__ import annotations

from typing import Iterable

from paper_trade.sqlite_store import query


class SetupNumberAllocator:
    """Allocates per-pair sequential setup numbers, restored from
    SQLite on init."""

    def __init__(self, pairs: Iterable[str]) -> None:
        self._next_num: dict[str, int] = {}
        # (pair, engine_setup_id) -> setup_num
        self._by_id: dict[tuple[str, int], int] = {}

        for pair in pairs:
            row = query(
                "SELECT MAX(setup_num) AS m FROM setups WHERE pair = ?",
                (pair,),
            )
            cur_max = row[0]["m"] if row and row[0]["m"] is not None else 0
            self._next_num[pair] = int(cur_max) + 1

        # Restore the (pair, setup_id) -> setup_num mapping so that
        # any in-flight setups (e.g. a setup that opened pre-restart
        # and is still pending after restart) get the same setup_num
        # they had before. Engine `setup_id` is stable across restart
        # because the engine state was checkpointed.
        rows = query(
            "SELECT DISTINCT pair, setup_id, setup_num FROM setups "
            "WHERE setup_num IS NOT NULL"
        )
        for r in rows:
            self._by_id[(r["pair"], int(r["setup_id"]))] = int(r["setup_num"])

    def get_or_assign(self, pair: str, engine_setup_id: int) -> int:
        """Return setup_num for (pair, engine_setup_id). Allocates a
        new one (current max + 1 for the pair) if none exists yet."""
        key = (pair, int(engine_setup_id))
        if key in self._by_id:
            return self._by_id[key]
        # Defensive seed: if we encounter a pair we didn't see at init.
        if pair not in self._next_num:
            self._next_num[pair] = 1
        num = self._next_num[pair]
        self._next_num[pair] = num + 1
        self._by_id[key] = num
        return num


__all__ = ["SetupNumberAllocator"]
