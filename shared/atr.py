"""shared/atr.py — Wilder's ATR(N) computation.

ATR(N) per J. Welles Wilder (1978):
    TR_t = max(high_t - low_t, |high_t - close_{t-1}|, |low_t - close_{t-1}|)
    ATR_t for t < N is undefined (NaN).
    ATR_N = mean(TR_1 .. TR_N)
    ATR_t = (ATR_{t-1} * (N - 1) + TR_t) / N    for t > N

Returned as a float numpy array of len(df) with NaN for warmup bars.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def compute_atr(df: pd.DataFrame, period: int = 14) -> np.ndarray:
    """Wilder's ATR(period) over an OHLC dataframe.

    Args:
        df: dataframe with columns ``high``, ``low``, ``close``.
        period: ATR window. Default 14 per Wilder.

    Returns:
        np.ndarray of float, length == len(df). NaN for the first ``period``
        bars (warmup), Wilder-smoothed values thereafter.
    """
    n = len(df)
    if n == 0:
        return np.array([], dtype=float)

    high = df["high"].to_numpy(dtype=float)
    low = df["low"].to_numpy(dtype=float)
    close = df["close"].to_numpy(dtype=float)

    tr = np.full(n, np.nan, dtype=float)
    tr[0] = high[0] - low[0]
    for i in range(1, n):
        tr[i] = max(
            high[i] - low[i],
            abs(high[i] - close[i - 1]),
            abs(low[i] - close[i - 1]),
        )

    atr = np.full(n, np.nan, dtype=float)
    if n < period:
        return atr

    # Seed: simple mean of first `period` TR values.
    atr[period - 1] = float(np.mean(tr[:period]))
    # Wilder smoothing thereafter.
    for i in range(period, n):
        atr[i] = (atr[i - 1] * (period - 1) + tr[i]) / period

    return atr


__all__ = ["compute_atr"]
