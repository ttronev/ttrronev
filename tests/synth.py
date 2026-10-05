"""Synthetic candles for tests — deterministic, tiny, offline.

A series is impulse legs alternating direction, each followed by a
consolidation that retraces roughly half the leg and oscillates. With the 1h
config this reliably yields confirmed ranges that later end on a breakout,
which is what the layer-5 tests need (levels are born when a range ends).

Scaling by a POWER OF TWO is exact in binary floating point, so a series
scaled by 2**k must produce bit-identical detection decisions. That is what
makes the price-scale-invariance tests strict rather than approximate.
"""
from __future__ import annotations
import numpy as np
import pandas as pd

H_MS = 3_600_000
T0_MS = (1_700_000_000_000 // (7 * 24 * H_MS)) * (7 * 24 * H_MS)   # epoch-week aligned

SHIB_SCALE = 2.0 ** -24      # 100 -> ~5.96e-06 (SHIB traded at 5.158e-06 in 2026-08)
BTC_SCALE = 2.0 ** 10        # 100 -> 102,400


def synth_1h(seed: int = 2, cycles: int = 3, base: float = 100.0,
             scale: float = 1.0) -> pd.DataFrame:
    # RandomState, not default_rng: its stream is frozen across NumPy versions,
    # so this fixture is the same series on every machine and in CI.
    rng = np.random.RandomState(seed)
    closes = [base]
    for c in range(cycles):
        kind = "up" if c % 2 == 0 else "down"
        size = 0.10 if kind == "up" else 0.12
        sgn = 1 if kind == "up" else -1
        p0 = closes[-1]
        leg = p0 * size
        for _ in range(20):                                   # impulse
            closes.append(float(closes[-1] + sgn * leg / 20
                                + p0 * 0.001 * rng.standard_normal()))
        top = closes[-1]
        deep = top - sgn * 0.62 * leg                         # ~62% retrace
        shallow = top - sgn * 0.12 * leg
        x = top
        for i in range(110):                                  # consolidation
            target = deep + (shallow - deep) * (0.5 - 0.5 * np.cos(i / 9.0))
            x = x + 0.30 * (target - x) + p0 * 0.0015 * rng.standard_normal()
            closes.append(float(x))
    closes = np.array(closes)
    opens = np.concatenate([[closes[0]], closes[:-1]])
    wig = 0.001 * np.abs(rng.standard_normal(len(closes))) + 0.0004
    highs = np.maximum(opens, closes) * (1 + wig)
    lows = np.minimum(opens, closes) * (1 - wig)
    ts = T0_MS + np.arange(len(closes), dtype="int64") * H_MS
    df = pd.DataFrame({"timestamp": ts, "open": opens, "high": highs,
                       "low": lows, "close": closes, "volume": 1.0})
    return scale_prices(df, scale) if scale != 1.0 else df


def scale_prices(df: pd.DataFrame, k: float) -> pd.DataFrame:
    out = df.copy()
    for col in ("open", "high", "low", "close"):
        out[col] = out[col] * k
    return out


def resample(df_1h: pd.DataFrame, hours: int) -> pd.DataFrame:
    """Aggregate 1h bars into `hours`-hour bars on epoch-aligned boundaries,
    dropping a trailing incomplete bar (closed bars only, like the real feed)."""
    step = hours * H_MS
    g = df_1h.assign(_b=(df_1h["timestamp"] // step) * step).groupby("_b", sort=True)
    out = pd.DataFrame({
        "timestamp": g["timestamp"].first().index.astype("int64"),
        "open": g["open"].first().to_numpy(),
        "high": g["high"].max().to_numpy(),
        "low": g["low"].min().to_numpy(),
        "close": g["close"].last().to_numpy(),
        "volume": g["volume"].sum().to_numpy(),
    })
    full = g.size().to_numpy() == hours
    return out[full].reset_index(drop=True)


TF_HOURS = {"1h": 1, "2h": 2, "4h": 4, "1d": 24, "1w": 168}


def write_pair_csvs(data_root, pair: str, df_1h: pd.DataFrame) -> None:
    """Write {pair}_{tf}.csv for every structural timeframe into data_root."""
    from pathlib import Path
    data_root = Path(data_root)
    data_root.mkdir(parents=True, exist_ok=True)
    for tf, hours in TF_HOURS.items():
        d = df_1h if hours == 1 else resample(df_1h, hours)
        d.to_csv(data_root / f"{pair}_{tf}.csv", index=False)
