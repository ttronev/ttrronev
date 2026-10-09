"""Per-setup level-proximity computation, reusing the Stage 1 catalog.

Phase 1a logs proximity for every setup as METADATA. The strategy does
NOT filter on proximity (per the strict reading of Stage 2's analysis).
This data accumulates as a forward-test of the post-hoc composite signal
identified in Stage 2 — after 100+ live setups we'll have OOS evidence
on whether the "any non-POC level <= 0.5R" pattern holds.

Reuses the helpers from `backtesting.level_features` so backtest and
live code paths compute proximity identically.
"""

from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd

from backtesting.level_features import (
    LEVEL_NAMES,
    fractal_pivots,
    latest_untested_pivot,
    lookup_period,
    poc_5m_window,
    precompute_period_lookups,
    resample_ohlcv,
    _to_ns_int64,
)


def compute_proximity(pair: str,
                      df_5m: pd.DataFrame,
                      df_1h: pd.DataFrame,
                      bos_timestamp: pd.Timestamp,
                      entry_price: float,
                      sl_price: float) -> dict[str, dict[str, Optional[float]]]:
    """Return {level_name: {price, dist_pct, dist_R, abs_R}} for one setup.

    Resamples 1h -> 4h and 1h -> 1d on the fly (same path as the
    backtest level-features module). Inputs are the live buffers; this
    is cheap (<50ms per call) for buffer sizes used in Phase 1a."""

    df_4h = resample_ohlcv(df_1h, "4h")
    df_1d = resample_ohlcv(df_1h, "1D")

    h5 = df_5m["high"].to_numpy(dtype=float)
    l5 = df_5m["low"].to_numpy(dtype=float)
    c5 = df_5m["close"].to_numpy(dtype=float)
    v5 = df_5m["volume"].to_numpy(dtype=float)
    ts_5m_ns = _to_ns_int64(df_5m["timestamp"].to_numpy())

    h1 = df_1h["high"].to_numpy(dtype=float)
    l1 = df_1h["low"].to_numpy(dtype=float)
    ts_1h_ns = _to_ns_int64(df_1h["timestamp"].to_numpy())
    p_1h_h_idx, p_1h_l_idx = fractal_pivots(h1, l1, n=2)
    p_1h_h_lvl = h1[p_1h_h_idx]
    p_1h_l_lvl = l1[p_1h_l_idx]

    h4 = df_4h["high"].to_numpy(dtype=float)
    l4 = df_4h["low"].to_numpy(dtype=float)
    ts_4h_ns = _to_ns_int64(df_4h["timestamp"].to_numpy())
    p_4h_h_idx, p_4h_l_idx = fractal_pivots(h4, l4, n=2)
    p_4h_h_lvl = h4[p_4h_h_idx]
    p_4h_l_lvl = l4[p_4h_l_idx]

    periods = precompute_period_lookups(df_1d, df_1h)

    bos_ns = pd.Timestamp(bos_timestamp).value
    bos_5m = int(np.searchsorted(ts_5m_ns, bos_ns, side="left"))
    if bos_5m >= len(c5):
        bos_5m = len(c5) - 1
    bos_1h = int(np.searchsorted(ts_1h_ns, bos_ns, side="right") - 1)
    bos_4h = int(np.searchsorted(ts_4h_ns, bos_ns, side="right") - 1)

    # Reference price for proximity = entry_price (fib_0.5).
    ref = float(entry_price)
    stop = abs(float(entry_price) - float(sl_price)) if np.isfinite(sl_price) else float("nan")

    levels: dict[str, Optional[float]] = {}
    levels["yearly_open"]  = lookup_period(periods["yearly"],  bos_timestamp, "open")
    levels["monthly_open"] = lookup_period(periods["monthly"], bos_timestamp, "open")
    levels["weekly_open"]  = lookup_period(periods["weekly"],  bos_timestamp, "open")
    levels["daily_open"]   = lookup_period(periods["daily"],   bos_timestamp, "open")
    levels["prior_month_high"] = lookup_period(periods["monthly"], bos_timestamp, "high", prior=True)
    levels["prior_month_low"]  = lookup_period(periods["monthly"], bos_timestamp, "low",  prior=True)
    levels["prior_week_high"]  = lookup_period(periods["weekly"],  bos_timestamp, "high", prior=True)
    levels["prior_week_low"]   = lookup_period(periods["weekly"],  bos_timestamp, "low",  prior=True)
    levels["prior_day_high"]   = lookup_period(periods["daily"],   bos_timestamp, "high", prior=True)
    levels["prior_day_low"]    = lookup_period(periods["daily"],   bos_timestamp, "low",  prior=True)
    levels["untested_1h_high"] = (latest_untested_pivot(p_1h_h_idx, p_1h_h_lvl, h1, bos_1h, "high")
                                  if bos_1h >= 0 else None)
    levels["untested_1h_low"]  = (latest_untested_pivot(p_1h_l_idx, p_1h_l_lvl, l1, bos_1h, "low")
                                  if bos_1h >= 0 else None)
    levels["untested_4h_high"] = (latest_untested_pivot(p_4h_h_idx, p_4h_h_lvl, h4, bos_4h, "high")
                                  if bos_4h >= 0 else None)
    levels["untested_4h_low"]  = (latest_untested_pivot(p_4h_l_idx, p_4h_l_lvl, l4, bos_4h, "low")
                                  if bos_4h >= 0 else None)
    levels["poc_5m_100"]  = poc_5m_window(h5, l5, v5, c5, bos_5m, 100, 50)

    out: dict[str, dict[str, Optional[float]]] = {}
    for name in LEVEL_NAMES:
        lv = levels.get(name)
        rec: dict[str, Optional[float]] = {"price": None, "dist_pct": None,
                                            "dist_R": None, "abs_R": None}
        if lv is not None and np.isfinite(lv) and np.isfinite(ref):
            rec["price"] = float(lv)
            if ref != 0:
                rec["dist_pct"] = (lv - ref) / ref * 100.0
            if np.isfinite(stop) and stop > 0:
                d = (lv - ref) / stop
                rec["dist_R"] = float(d)
                rec["abs_R"] = float(abs(d))
        out[name] = rec
    return out


__all__ = ["compute_proximity"]
