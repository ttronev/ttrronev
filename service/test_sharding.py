"""Unit test for service/sharding.py (Stage 8b acceptance 4).

Run:  python -m service.test_sharding
"""
from __future__ import annotations
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from service.sharding import parse_shard, shard_pairs

PAIRS = ["SOL_USDT", "LINK_USDT", "BTC_USDT", "ETH_USDT", "XRP_USDT",
         "BNB_USDT", "DOGE_USDT", "TRX_USDT", "ADA_USDT", "HYPE_USDT",
         "XLM_USDT", "BCH_USDT", "SUI_USDT", "AVAX_USDT", "LTC_USDT",
         "HBAR_USDT", "TON_USDT", "DOT_USDT", "SHIB_USDT", "UNI_USDT"]


def test_default_is_identity():
    assert shard_pairs(PAIRS, "0/1") == PAIRS


def test_two_shards_partition():
    s0 = set(shard_pairs(PAIRS, "0/2"))
    s1 = set(shard_pairs(PAIRS, "1/2"))
    assert s0.isdisjoint(s1), f"overlap: {s0 & s1}"
    assert s0 | s1 == set(PAIRS), f"missing: {set(PAIRS) - (s0 | s1)}"
    assert s0 and s1, "degenerate split (one shard empty on 20 pairs)"


def test_stable_across_calls():
    assert shard_pairs(PAIRS, "0/2") == shard_pairs(PAIRS, "0/2")


def test_bad_specs_rejected():
    for bad in ("", "2/2", "-1/2", "1", "a/b", "0/0"):
        try:
            parse_shard(bad)
        except ValueError:
            continue
        raise AssertionError(f"{bad!r} should be rejected")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"[test_sharding] {name}: OK")
    print("[test_sharding] all tests passed")
