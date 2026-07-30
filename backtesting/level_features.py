"""Stage 1: log price-level proximity features for every v1.4 setup.

For each setup row in a per-pair trades CSV, compute proximity (in signed
percent and signed R-multiples vs the structural stop distance) to a
catalog of price levels, plus the absolute R distance for bucket analysis.

Levels logged (each as `{name}_price`, `{name}_dist_pct`, `{name}_dist_R`,
`{name}_abs_R`):

  yearly_open                — open of the year (Jan 1 00:00 UTC) of the BOS
  monthly_open               — open of the BOS bar's calendar month
  weekly_open                — open of the BOS bar's ISO week (Mon 00:00 UTC)
  daily_open                 — open of the BOS bar's UTC day
  prior_month_high / _low    — prior calendar month's high/low (1d-resample)
  prior_week_high / _low     — prior ISO week's high/low
  prior_day_high / _low      — prior UTC day's high/low
  untested_4h_high / _low    — most recent 2-bar fractal swing not yet retraded
  untested_1h_high / _low    — same on 1h
  poc_5m_100                 — volume-profile POC of the 100 5m bars before BOS

Reference price = `entry_price` (fib_0.5). For setups that cancelled before
fibs were computed (entry NaN), reference = BOS bar close from the 5m CSV.

Stop distance R = |entry - sl|. If 0 / NaN, dist_R is NaN.

Output: same trades CSV with all level columns appended, written next to
the input with `_levels` suffix. No filtering applied — Stage 1 is pure
logging.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DATA_RAW = ROOT / "data" / "raw"
RESULTS_DIR = ROOT / "backtesting" / "results"


def _to_ns_int64(ts_like) -> np.ndarray:
    """Force ns-resolution int64 timestamps. ms-resolution DatetimeIndexes
    return ms via .asi8, which silently breaks searchsorted vs ns refs."""
    idx = pd.DatetimeIndex(pd.to_datetime(pd.Series(ts_like), utc=True))
    try:
        return idx.as_unit("ns").asi8
    except Exception:
        return idx.values.astype("datetime64[ns]").astype("int64")


# --- I/O ----------------------------------------------------------------

def _pair_filename(pair: str) -> str:
    return pair.replace("/", "_")


def load_5m_1h(pair: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    p5 = DATA_RAW / f"{_pair_filename(pair)}_5m.csv"
    p1 = DATA_RAW / f"{_pair_filename(pair)}_1h.csv"
    df5 = pd.read_csv(p5)
    df1 = pd.read_csv(p1)
    df5["timestamp"] = pd.to_datetime(df5["timestamp"], unit="ms", utc=True)
    df1["timestamp"] = pd.to_datetime(df1["timestamp"], unit="ms", utc=True)
    return df5.reset_index(drop=True), df1.reset_index(drop=True)


def resample_ohlcv(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    """Resample a 1h OHLCV DataFrame to a coarser timeframe."""
    s = df.set_index("timestamp")
    out = s.resample(rule, label="left", closed="left").agg({
        "open":  "first",
        "high":  "max",
        "low":   "min",
        "close": "last",
        "volume": "sum",
    }).dropna(subset=["open"]).reset_index()
    return out


# --- Fractal pivots -----------------------------------------------------

def fractal_pivots(highs: np.ndarray, lows: np.ndarray, n: int = 2
                   ) -> tuple[np.ndarray, np.ndarray]:
    """Return (high_pivot_indices, low_pivot_indices) using n-bar fractals.

    A bar i is a swing high if highs[i] > highs[i-k] and highs[i] > highs[i+k]
    for k in 1..n. (Strict — equal-highs don't qualify, which avoids
    double-counting flat tops.) Indices in the range [n, len-n-1]."""
    N = len(highs)
    hp, lp = [], []
    for i in range(n, N - n):
        h = highs[i]; l = lows[i]
        is_h = True; is_l = True
        for k in range(1, n + 1):
            if not (h > highs[i - k] and h > highs[i + k]):
                is_h = False
            if not (l < lows[i - k] and l < lows[i + k]):
                is_l = False
            if not is_h and not is_l:
                break
        if is_h: hp.append(i)
        if is_l: lp.append(i)
    return np.asarray(hp, dtype=int), np.asarray(lp, dtype=int)


def latest_untested_pivot(
    pivot_idxs: np.ndarray,
    levels: np.ndarray,             # highs[pivot_idxs] or lows[pivot_idxs]
    series: np.ndarray,             # bar highs (for high pivots) or lows (for lows)
    bos_idx: int,
    direction: str,                 # "high" or "low"
) -> Optional[float]:
    """Walk pivots backwards; return level of most recent one whose extreme
    has not been retraced through bos_idx (inclusive). The pivot must have
    formed before bos_idx (strict)."""
    # Find pivots strictly before bos_idx and confirmed (i + n <= bos_idx
    # — but we already require pivot_idx < bos_idx; the +n confirmation is
    # naturally enforced by waiting for fractal_pivots to have indexed it).
    if len(pivot_idxs) == 0:
        return None
    cutoff = np.searchsorted(pivot_idxs, bos_idx, side="left")
    if cutoff == 0:
        return None
    for k in range(cutoff - 1, -1, -1):
        f = int(pivot_idxs[k])
        lvl = float(levels[k])
        # Window: bars f+1 .. bos_idx (inclusive). If any bar in that
        # window has touched the level, it's tested.
        if f + 1 > bos_idx:
            continue
        window = series[f + 1: bos_idx + 1]
        if direction == "high":
            if window.size == 0 or window.max() < lvl:
                return lvl
        else:
            if window.size == 0 or window.min() > lvl:
                return lvl
    return None


# --- Period anchors -----------------------------------------------------

def precompute_period_lookups(df_1d: pd.DataFrame, df_1h: pd.DataFrame
                              ) -> dict:
    """Build per-period (year/month/week/day) open prices and prior-period
    H/L tables, indexed by period-start UTC timestamps."""
    s1d = df_1d.set_index("timestamp").sort_index()
    s1h = df_1h.set_index("timestamp").sort_index()

    # Daily open / high / low: directly from 1d resample.
    daily_open = s1d["open"]
    daily_high = s1d["high"]
    daily_low = s1d["low"]

    # Weekly open / high / low (ISO week, Mon start).
    weekly = s1h.resample("W-MON", label="left", closed="left").agg({
        "open": "first", "high": "max", "low": "min",
    }).dropna(subset=["open"])

    # Monthly.
    monthly = s1h.resample("MS", label="left", closed="left").agg({
        "open": "first", "high": "max", "low": "min",
    }).dropna(subset=["open"])

    # Yearly.
    yearly = s1h.resample("YS", label="left", closed="left").agg({
        "open": "first", "high": "max", "low": "min",
    }).dropna(subset=["open"])

    return {
        "daily": daily_open.to_frame("open").join(daily_high.rename("high")).join(daily_low.rename("low")),
        "weekly": weekly,
        "monthly": monthly,
        "yearly": yearly,
    }


def lookup_period(table: pd.DataFrame, ts: pd.Timestamp, col: str,
                  prior: bool = False) -> Optional[float]:
    """Find the period containing ts (or the prior one). Returns col value or None."""
    if len(table) == 0:
        return None
    idx = table.index
    pos = idx.searchsorted(ts, side="right") - 1
    if prior:
        pos -= 1
    if pos < 0 or pos >= len(idx):
        return None
    return float(table[col].iloc[pos])


# --- Volume profile POC -------------------------------------------------

def poc_5m_window(highs: np.ndarray, lows: np.ndarray, vols: np.ndarray,
                  closes: np.ndarray, end_idx: int, window: int = 100,
                  bins: int = 50) -> Optional[float]:
    """POC of the `window` 5m bars ending at end_idx-1 (exclusive of bos bar).

    Bars contribute uniformly to a price range [low, high]; we approximate
    by binning bar closes weighted by volume."""
    start = max(0, end_idx - window)
    if end_idx <= start:
        return None
    h = highs[start:end_idx]
    l = lows[start:end_idx]
    v = vols[start:end_idx]
    c = closes[start:end_idx]
    if len(c) == 0:
        return None
    # Bin by close (simple POC). For each bin, sum volume.
    pmin = float(l.min())
    pmax = float(h.max())
    if pmax <= pmin:
        return float(c[-1])
    edges = np.linspace(pmin, pmax, bins + 1)
    # Distribute each bar's volume across all bins it overlaps (proportional
    # to bar's price coverage in that bin). For simplicity: bar contributes
    # uniformly across its [low, high] range.
    centers = 0.5 * (edges[:-1] + edges[1:])
    profile = np.zeros(bins, dtype=float)
    for i in range(len(c)):
        lo = l[i]; hi = h[i]; vi = v[i]
        if hi <= lo or vi <= 0:
            # degenerate; bin by close
            j = min(bins - 1, max(0, int((c[i] - pmin) / (pmax - pmin) * bins)))
            profile[j] += vi
            continue
        # Bin range covered by [lo, hi]:
        j_lo = min(bins - 1, max(0, int((lo - pmin) / (pmax - pmin) * bins)))
        j_hi = min(bins - 1, max(0, int((hi - pmin) / (pmax - pmin) * bins)))
        n_cover = j_hi - j_lo + 1
        if n_cover <= 0:
            continue
        share = vi / n_cover
        profile[j_lo:j_hi + 1] += share
    return float(centers[int(profile.argmax())])


# --- Main feature builder -----------------------------------------------

LEVEL_NAMES = [
    "yearly_open", "monthly_open", "weekly_open", "daily_open",
    "prior_month_high", "prior_month_low",
    "prior_week_high", "prior_week_low",
    "prior_day_high", "prior_day_low",
    "untested_4h_high", "untested_4h_low",
    "untested_1h_high", "untested_1h_low",
    "poc_5m_100",
]


def compute_features_for_pair(pair: str, trades_csv: Path) -> pd.DataFrame:
    print(f"\n=== {pair} :: levels for {trades_csv.name} ===")
    t0 = time.time()
    trades = pd.read_csv(trades_csv)
    df_5m, df_1h = load_5m_1h(pair)
    df_4h = resample_ohlcv(df_1h, "4h")
    df_1d = resample_ohlcv(df_1h, "1D")

    print(f"  trades: {len(trades):,}   5m: {len(df_5m):,}   1h: {len(df_1h):,}   "
          f"4h: {len(df_4h):,}   1d: {len(df_1d):,}")

    # 5m arrays for POC + bos lookup. Force ns resolution — DatetimeIndex
    # may be ms-unit, which makes .asi8 return ms and silently breaks
    # searchsorted against ns-unit reference timestamps.
    ts_5m_ns = _to_ns_int64(df_5m["timestamp"].to_numpy())
    h5 = df_5m["high"].to_numpy(dtype=float)
    l5 = df_5m["low"].to_numpy(dtype=float)
    c5 = df_5m["close"].to_numpy(dtype=float)
    v5 = df_5m["volume"].to_numpy(dtype=float)

    # Fractal pivots on 1h and 4h.
    h1 = df_1h["high"].to_numpy(dtype=float)
    l1 = df_1h["low"].to_numpy(dtype=float)
    ts_1h_ns = _to_ns_int64(df_1h["timestamp"].to_numpy())
    pivots_1h_h_idx, pivots_1h_l_idx = fractal_pivots(h1, l1, n=2)
    pivots_1h_h_lvl = h1[pivots_1h_h_idx]
    pivots_1h_l_lvl = l1[pivots_1h_l_idx]

    h4 = df_4h["high"].to_numpy(dtype=float)
    l4 = df_4h["low"].to_numpy(dtype=float)
    ts_4h_ns = _to_ns_int64(df_4h["timestamp"].to_numpy())
    pivots_4h_h_idx, pivots_4h_l_idx = fractal_pivots(h4, l4, n=2)
    pivots_4h_h_lvl = h4[pivots_4h_h_idx]
    pivots_4h_l_lvl = l4[pivots_4h_l_idx]

    # Period lookup tables.
    periods = precompute_period_lookups(df_1d, df_1h)

    # Output cols.
    cols = {f"{n}_price": np.full(len(trades), np.nan) for n in LEVEL_NAMES}
    cols.update({f"{n}_dist_pct": np.full(len(trades), np.nan) for n in LEVEL_NAMES})
    cols.update({f"{n}_dist_R":   np.full(len(trades), np.nan) for n in LEVEL_NAMES})
    cols.update({f"{n}_abs_R":    np.full(len(trades), np.nan) for n in LEVEL_NAMES})
    cols["ref_price_used"] = np.full(len(trades), np.nan)
    cols["stop_dist_used"] = np.full(len(trades), np.nan)

    # Process each setup.
    bos_ts_arr = pd.to_datetime(trades["bos_timestamp"], utc=True).to_numpy()

    for k in range(len(trades)):
        row = trades.iloc[k]
        bos_ts = pd.Timestamp(bos_ts_arr[k])
        bos_ns = bos_ts.value
        # Reference price: entry_price; fallback to bos bar close.
        entry = row.get("entry_price")
        sl = row.get("sl_price")
        try:
            entry = float(entry); sl = float(sl)
        except (TypeError, ValueError):
            entry = np.nan; sl = np.nan
        bos_5m_idx = int(np.searchsorted(ts_5m_ns, bos_ns, side="left"))
        if bos_5m_idx >= len(c5):
            bos_5m_idx = len(c5) - 1
        if not np.isfinite(entry):
            entry = float(c5[bos_5m_idx])
        if np.isfinite(entry) and np.isfinite(sl):
            stop_dist = abs(entry - sl)
        else:
            stop_dist = np.nan
        cols["ref_price_used"][k] = entry
        cols["stop_dist_used"][k] = stop_dist

        # Period-based levels.
        levels: dict[str, Optional[float]] = {}
        levels["yearly_open"]  = lookup_period(periods["yearly"],  bos_ts, "open")
        levels["monthly_open"] = lookup_period(periods["monthly"], bos_ts, "open")
        levels["weekly_open"]  = lookup_period(periods["weekly"],  bos_ts, "open")
        levels["daily_open"]   = lookup_period(periods["daily"],   bos_ts, "open")
        levels["prior_month_high"] = lookup_period(periods["monthly"], bos_ts, "high", prior=True)
        levels["prior_month_low"]  = lookup_period(periods["monthly"], bos_ts, "low",  prior=True)
        levels["prior_week_high"]  = lookup_period(periods["weekly"],  bos_ts, "high", prior=True)
        levels["prior_week_low"]   = lookup_period(periods["weekly"],  bos_ts, "low",  prior=True)
        levels["prior_day_high"]   = lookup_period(periods["daily"],   bos_ts, "high", prior=True)
        levels["prior_day_low"]    = lookup_period(periods["daily"],   bos_ts, "low",  prior=True)

        # Untested fractal swings.
        bos_1h_idx = int(np.searchsorted(ts_1h_ns, bos_ns, side="right") - 1)
        bos_4h_idx = int(np.searchsorted(ts_4h_ns, bos_ns, side="right") - 1)
        levels["untested_1h_high"] = latest_untested_pivot(
            pivots_1h_h_idx, pivots_1h_h_lvl, h1, bos_1h_idx, "high"
        ) if bos_1h_idx >= 0 else None
        levels["untested_1h_low"] = latest_untested_pivot(
            pivots_1h_l_idx, pivots_1h_l_lvl, l1, bos_1h_idx, "low"
        ) if bos_1h_idx >= 0 else None
        levels["untested_4h_high"] = latest_untested_pivot(
            pivots_4h_h_idx, pivots_4h_h_lvl, h4, bos_4h_idx, "high"
        ) if bos_4h_idx >= 0 else None
        levels["untested_4h_low"] = latest_untested_pivot(
            pivots_4h_l_idx, pivots_4h_l_lvl, l4, bos_4h_idx, "low"
        ) if bos_4h_idx >= 0 else None

        # POC on 5m.
        levels["poc_5m_100"] = poc_5m_window(h5, l5, v5, c5, bos_5m_idx, 100, 50)

        # Distances.
        for name in LEVEL_NAMES:
            lv = levels.get(name)
            if lv is None or not np.isfinite(lv) or not np.isfinite(entry):
                continue
            cols[f"{name}_price"][k] = lv
            if entry != 0:
                cols[f"{name}_dist_pct"][k] = (lv - entry) / entry * 100.0
            if np.isfinite(stop_dist) and stop_dist > 0:
                d_r = (lv - entry) / stop_dist
                cols[f"{name}_dist_R"][k] = d_r
                cols[f"{name}_abs_R"][k]  = abs(d_r)

    out = trades.copy()
    for k, v in cols.items():
        out[k] = v
    elapsed = time.time() - t0
    print(f"  done in {elapsed:.1f}s")
    return out


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--pairs", nargs="+",
                   default=["SOL/USDT", "AVAX/USDT", "LINK/USDT"])
    p.add_argument("--label", type=str, default="v1.4_18mo_5pair",
                   help="Match the variant label of the trades CSVs to enrich.")
    args = p.parse_args()

    for pair in args.pairs:
        # Find latest trades CSV for this pair+label.
        candidates = sorted(RESULTS_DIR.glob(
            f"secondary_only_v1_{_pair_filename(pair)}_*_{args.label}.csv"
        ))
        if not candidates:
            print(f"NO trades CSV for {pair} (label={args.label})")
            continue
        trades_csv = candidates[-1]  # most recent timestamp wins
        enriched = compute_features_for_pair(pair, trades_csv)
        out_path = trades_csv.with_name(trades_csv.stem + "_levels.csv")
        enriched.to_csv(out_path, index=False)
        print(f"  -> {out_path.name}  ({len(enriched.columns)} cols, {len(enriched):,} rows)")


if __name__ == "__main__":
    main()
