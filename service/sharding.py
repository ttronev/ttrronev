"""
service/sharding.py — worker shard assignment (Stage 8b prep; NOT active
scaling yet — default PAIRS_SHARD=0/1 serves everything).

A future second worker = another compose service with PAIRS_SHARD=1/2.
Uses crc32, NOT Python's hash() (which is salted per process — two workers
would disagree on assignments).

Test:  python -m service.test_sharding
"""
from __future__ import annotations
import os
import zlib


def parse_shard(spec: str | None = None) -> tuple[int, int]:
    spec = spec if spec is not None else os.environ.get("PAIRS_SHARD", "0/1")
    try:
        i_s, n_s = spec.strip().split("/")
        i, n = int(i_s), int(n_s)
    except (ValueError, AttributeError):
        raise ValueError(f"bad PAIRS_SHARD {spec!r}; expected 'i/n' like '0/2'")
    if n < 1 or not (0 <= i < n):
        raise ValueError(f"bad PAIRS_SHARD {spec!r}; need 0 <= i < n")
    return i, n


def pair_shard(pair: str, n: int) -> int:
    return zlib.crc32(pair.encode("utf-8")) % n


def shard_pairs(pairs: list[str], spec: str | None = None) -> list[str]:
    i, n = parse_shard(spec)
    if n == 1:
        return list(pairs)
    return [p for p in pairs if pair_shard(p, n) == i]
