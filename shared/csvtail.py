"""
shared/csvtail.py — read only the END of a candle CSV.

Memory hygiene: see docs/runbooks/memory_hygiene.md, rule 2 ("slice, don't load"). The
service's 5-minute path used to pd.read_csv() whole files to use their last
few hundred rows: a 45-day 5m window is ~13k rows, the BTC 5m file is ~950k.
These helpers seek to the tail and parse only that.

Assumes the file is ascending by timestamp, which the writers guarantee
(okx_fetch appends newer bars; backfill prepends older ones atomically).
A final line cut short by a concurrent append is dropped.
"""
from __future__ import annotations
import io
from pathlib import Path

import pandas as pd

CANDLE_COLUMNS = ["timestamp", "open", "high", "low", "close", "volume"]
_BYTES_PER_ROW = 128          # generous; real rows are ~50-90 bytes
_MIN_CHUNK = 64 * 1024


def _header(f) -> tuple[list[str], int]:
    """(column names, byte offset where data starts)."""
    f.seek(0)
    first = f.readline()
    names = [c.strip() for c in first.decode("utf-8", errors="replace").split(",")]
    if not names or names[0] != "timestamp":
        return list(CANDLE_COLUMNS), 0           # headerless file: data from byte 0
    return names, f.tell()


def _parse(data: bytes, names: list[str], drop_first_partial: bool) -> pd.DataFrame:
    if drop_first_partial:
        nl = data.find(b"\n")
        data = data[nl + 1:] if nl >= 0 else b""
    if data and not data.endswith((b"\n", b"\r")):
        # Last line has no terminator: it is being written right now. Keep it
        # only if it already has every field.
        cut = data.rfind(b"\n")
        last = data[cut + 1:]
        if last.count(b",") != len(names) - 1:
            data = data[:cut + 1] if cut >= 0 else b""
    if not data.strip():
        return pd.DataFrame({c: pd.Series(dtype="float64") for c in names}).astype(
            {"timestamp": "int64"}) if "timestamp" in names else pd.DataFrame(columns=names)
    df = pd.read_csv(io.BytesIO(data), header=None, names=names,
                     on_bad_lines="skip", low_memory=False)
    df["timestamp"] = pd.to_numeric(df["timestamp"], errors="coerce")
    df = df.dropna(subset=["timestamp"])
    df["timestamp"] = df["timestamp"].astype("int64")
    return df.reset_index(drop=True)


def read_tail(path, rows: int | None = None, since_ms: int | None = None,
              columns: list[str] | None = None) -> pd.DataFrame:
    """The last part of a candle CSV as a DataFrame.

    rows       return AT LEAST this many trailing rows (all rows if the file
               has fewer). Callers wanting exactly N use .tail(N).
    since_ms   return every row with timestamp >= since_ms (and possibly a few
               earlier ones; filter exactly if it matters).
    Both given: both are satisfied. Neither: the whole file.
    """
    path = Path(path)
    size = path.stat().st_size
    with path.open("rb") as f:
        names, data_start = _header(f)
        if rows is None and since_ms is None:
            want = size
        else:
            want = max(_MIN_CHUNK, (rows or 0) * _BYTES_PER_ROW)
        while True:
            start = max(data_start, size - want)
            f.seek(start)
            df = _parse(f.read(size - start), names, drop_first_partial=start > data_start)
            whole = start <= data_start
            enough_rows = rows is None or len(df) >= rows
            reaches_back = (since_ms is None or len(df) == 0
                            or int(df["timestamp"].iloc[0]) < since_ms)
            if whole or (enough_rows and reaches_back and len(df) > 0):
                break
            want *= 2
    if columns is not None:
        df = df[list(columns)]
    return df


def last_ts_ms(path) -> int | None:
    """Timestamp of the last complete data row, or None (missing/empty file)."""
    path = Path(path)
    try:
        size = path.stat().st_size
    except OSError:
        return None
    if size == 0:
        return None
    with path.open("rb") as f:
        f.seek(max(0, size - 4096))
        tail = f.read().decode("utf-8", errors="replace")
    complete = tail.endswith(("\n", "\r"))
    lines = [ln for ln in tail.splitlines() if ln.strip()]
    if not complete and lines:
        if lines[-1].count(",") != len(CANDLE_COLUMNS) - 1:
            lines = lines[:-1]                    # torn final line
    for line in reversed(lines):
        try:
            return int(line.split(",", 1)[0])
        except ValueError:
            continue                              # header, or a partial first line
    return None


__all__ = ["read_tail", "last_ts_ms", "CANDLE_COLUMNS"]
