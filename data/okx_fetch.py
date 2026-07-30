"""
data/okx_fetch.py — OKX v5 REST candle fetcher.

User is geo-blocked from Binance and Bybit (Russia, CloudFront-level block,
VPN doesn't work). OKX REST endpoints are reachable directly. This module
is the ONLY way new candle data should be pulled into this project. No
Binance fallback — Binance attempts will fail and we don't retry.

Output schema matches the existing Binance CSVs in data/raw/:
    timestamp,open,high,low,close,volume
    <ms_epoch>,<float>,<float>,<float>,<float>,<float>

By default targets linear perpetuals (instId=<SYM>-USDT-SWAP). For SOL
that means SOL-USDT-SWAP. Volume column = OKX `vol` (contracts; for
linear SWAP w/ ctVal=1 base unit, this equals base-currency volume).

Usage
-----
    # CLI: extend an existing CSV up to current time.
    python data/okx_fetch.py --pair SOL --tf 1h --extend

    # Programmatic.
    from data.okx_fetch import fetch_okx_candles, append_to_csv
    rows = fetch_okx_candles("SOL-USDT-SWAP", "1H",
                             start_ms=1777881600000, end_ms=None)
    append_to_csv("data/raw/SOL_USDT_1h.csv", rows)
"""
from __future__ import annotations

import argparse
import csv
import time
from pathlib import Path
from typing import Optional

import requests


OKX_BASE = "https://www.okx.com"

# Map our TF tags -> OKX `bar` values.
# IMPORTANT: daily/weekly use the *utc* variants so candles align to
# 00:00 UTC, matching the existing Binance CSVs. Plain "1D"/"1W" on OKX
# use Hong Kong (UTC+8) day boundaries -> 16:00 UTC timestamps, which
# would misalign the series.
TF_TO_OKX = {
    "1m":  "1m",
    "3m":  "3m",
    "5m":  "5m",
    "15m": "15m",
    "30m": "30m",
    "1h":  "1H",
    "2h":  "2H",
    "4h":  "4H",
    "1d":  "1Dutc",
    "1w":  "1Wutc",
}

TF_TO_MS = {
    "1m":  60_000,
    "3m":  3 * 60_000,
    "5m":  5 * 60_000,
    "15m": 15 * 60_000,
    "30m": 30 * 60_000,
    "1h":  60 * 60_000,
    "2h":  2 * 60 * 60_000,
    "4h":  4 * 60 * 60_000,
    "1d":  24 * 60 * 60_000,
}


def _http_get(url: str, params: dict, max_attempts: int = 3) -> dict:
    """GET with bounded retries on transient errors. NEVER retries on
    auth-style failures (401/403) — we fail fast and let the caller
    abort. This module never touches Binance/Bybit so geo-block
    cascades cannot start here."""
    last_err: Optional[Exception] = None
    for attempt in range(max_attempts):
        try:
            r = requests.get(url, params=params, timeout=15)
        except requests.RequestException as e:
            last_err = e
            time.sleep(0.5 * (attempt + 1))
            continue
        if r.status_code in (401, 403):
            raise RuntimeError(
                f"OKX returned {r.status_code} — auth-class failure. Not retrying. "
                f"Body: {r.text[:200]}"
            )
        if r.status_code == 200:
            return r.json()
        last_err = RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
        time.sleep(0.5 * (attempt + 1))
    raise RuntimeError(f"OKX request failed after {max_attempts} attempts: {last_err}")


def fetch_okx_candles(
    inst_id: str,
    bar: str,
    start_ms: Optional[int] = None,
    end_ms: Optional[int] = None,
    page_size: int = 100,
    max_pages: int = 10000,
) -> list[tuple[int, float, float, float, float, float]]:
    """Fetch OHLCV candles from OKX. Returns CLOSED bars only with
    start_ms < ts <= end_ms (end_ms defaults to now), sorted ascending
    by timestamp.

    Pagination walks BACKWARDS using OKX's `after` (= upper-bound,
    exclusive). Each request returns up to `page_size` candles newest-
    first; we advance by taking the OLDEST ts from each response as
    the next `after` cursor. Stop when we cross start_ms or hit an
    empty response. Walking forwards via `before` does NOT work for
    large gaps (OKX returns the newest 100 satisfying the filter, not
    the oldest)."""
    if bar not in TF_TO_OKX.values() and bar in TF_TO_OKX:
        bar = TF_TO_OKX[bar]

    url = f"{OKX_BASE}/api/v5/market/history-candles"
    rows: list[tuple[int, float, float, float, float, float]] = []
    seen_ts: set[int] = set()

    if end_ms is None:
        end_ms = int(time.time() * 1000)

    cursor_after: Optional[int] = end_ms + 1  # exclusive upper bound
    lower_bound = int(start_ms) if start_ms is not None else 0

    for _ in range(max_pages):
        params: dict = {"instId": inst_id, "bar": bar, "limit": str(page_size)}
        if cursor_after is not None:
            params["after"] = str(cursor_after)
        data = _http_get(url, params)
        if data.get("code") != "0":
            raise RuntimeError(f"OKX error: {data}")
        candles = data.get("data") or []
        if not candles:
            break
        # Response is newest-first. Track oldest ts seen for the next
        # `after` cursor.
        oldest_ts: Optional[int] = None
        added = 0
        for c in candles:
            ts = int(c[0])
            confirm = c[8] if len(c) > 8 else "1"
            if oldest_ts is None or ts < oldest_ts:
                oldest_ts = ts
            if confirm != "1":
                continue
            if ts > end_ms or ts <= lower_bound:
                continue
            if ts in seen_ts:
                continue
            rows.append((
                ts, float(c[1]), float(c[2]), float(c[3]),
                float(c[4]), float(c[5]),
            ))
            seen_ts.add(ts)
            added += 1
        if oldest_ts is None:
            break
        # If oldest in this response is already <= lower_bound, we've
        # crossed the start and are done.
        if oldest_ts <= lower_bound:
            break
        cursor_after = oldest_ts
        # OKX history-candles public-rate: ~20 req / 2s.
        time.sleep(0.12)

    rows.sort(key=lambda r: r[0])
    return rows


def read_last_ts_ms(csv_path: Path) -> Optional[int]:
    """Return the timestamp_ms of the final row of an OHLCV CSV, or
    None if the file is empty or missing."""
    if not csv_path.exists():
        return None
    last_ts: Optional[int] = None
    with csv_path.open("r", newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        header = next(reader, None)
        if header is None:
            return None
        for row in reader:
            if not row:
                continue
            try:
                last_ts = int(row[0])
            except (ValueError, IndexError):
                continue
    return last_ts


def append_to_csv(
    csv_path: Path,
    rows: list[tuple[int, float, float, float, float, float]],
) -> int:
    """Append OHLCV rows to a CSV, deduplicating on timestamp_ms vs
    existing content. Returns the count of rows actually written."""
    csv_path = Path(csv_path)
    existing_ts: set[int] = set()
    if csv_path.exists():
        with csv_path.open("r", newline="", encoding="utf-8") as f:
            reader = csv.reader(f)
            header = next(reader, None)
            for row in reader:
                if not row:
                    continue
                try:
                    existing_ts.add(int(row[0]))
                except (ValueError, IndexError):
                    continue
    new_rows = [r for r in rows if r[0] not in existing_ts]
    if not new_rows:
        return 0
    write_header = not csv_path.exists()
    with csv_path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if write_header:
            writer.writerow(["timestamp", "open", "high", "low", "close", "volume"])
        for r in new_rows:
            writer.writerow(r)
    return len(new_rows)


def extend_csv(
    pair: str,
    tf: str,
    inst_id: Optional[str] = None,
    data_dir: Path = Path("data/raw"),
) -> tuple[int, Optional[int], Optional[int]]:
    """Extend `<pair>_USDT_<tf>.csv` up to current time using OKX SWAP.
    Returns (rows_added, first_new_ts_ms, last_new_ts_ms)."""
    if tf not in TF_TO_OKX:
        raise ValueError(f"Unsupported TF: {tf}")
    csv_path = data_dir / f"{pair}_USDT_{tf}.csv"
    last_ts = read_last_ts_ms(csv_path)
    if last_ts is None:
        raise FileNotFoundError(
            f"{csv_path} is missing or empty. This fetcher only appends. "
            f"To bootstrap a new pair from scratch, call fetch_okx_candles() "
            f"directly with start_ms set to your desired start."
        )
    inst_id = inst_id or f"{pair}-USDT-SWAP"
    bar = TF_TO_OKX[tf]
    rows = fetch_okx_candles(inst_id=inst_id, bar=bar, start_ms=last_ts)
    if not rows:
        return 0, None, None
    n = append_to_csv(csv_path, rows)
    first_new = rows[0][0] if rows else None
    last_new = rows[-1][0] if rows else None
    return n, first_new, last_new


def _fmt_ts(ms: Optional[int]) -> str:
    if ms is None:
        return "None"
    import datetime as dt
    return dt.datetime.utcfromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M:%S UTC")


def main() -> None:
    p = argparse.ArgumentParser(description="Extend a raw OHLCV CSV with OKX SWAP data.")
    p.add_argument("--pair", required=True, help="e.g. SOL")
    p.add_argument("--tf", required=True, choices=list(TF_TO_OKX.keys()))
    p.add_argument("--inst-id", default=None,
                   help="Override default <PAIR>-USDT-SWAP, e.g. SOL-USDT for spot.")
    p.add_argument("--data-dir", default="data/raw")
    args = p.parse_args()

    csv_path = Path(args.data_dir) / f"{args.pair}_USDT_{args.tf}.csv"
    last_before = read_last_ts_ms(csv_path)
    print(f"[okx_fetch] {csv_path.name} last ts before: {_fmt_ts(last_before)}")
    n, first_new, last_new = extend_csv(
        pair=args.pair, tf=args.tf, inst_id=args.inst_id,
        data_dir=Path(args.data_dir),
    )
    print(f"[okx_fetch] appended {n} rows. first_new={_fmt_ts(first_new)} "
          f"last_new={_fmt_ts(last_new)}")


if __name__ == "__main__":
    import sys                                    # Memory hygiene: see MEMORY_HYGIENE.md
    from pathlib import Path as _P
    sys.path.insert(0, str(_P(__file__).resolve().parents[1]))
    from shared.memhygiene import install
    install("okx_fetch")
    main()
