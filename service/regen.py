"""
service/regen.py — fetch + windowed detector regeneration for one (pair, TF).

Wraps the EXISTING detector stack without touching its logic:

  * fetch      — data/freshness_monitor.check_and_update (OKX, closed bars only)
  * detection  — detectors/range_detector_{tf}.run(), fed a WINDOWED CSV slice
                 for the fast TFs (see service/pairs.REGEN_WINDOW_DAYS); 1d/1w
                 run on full history. Windowing is done by writing a sliced
                 copy of the raw CSV and passing its path into run() — the
                 detector itself is unchanged.
  * chain      — cleanness -> nesting -> known_at -> layer5 -> layer5.1, the
                 load-bearing order from MEMORY_HYGIENE.md, at most once per
                 CHAIN_MIN_INTERVAL_S (levels only change when a range ends).

Window caveat (by design, per the service spec): a windowed run rebuilds
detector state from the window's left edge, so ranges/EMA bias near that edge
differ from a full-history run. Windows are sized (45d for 5m, 1y for 1h/2h,
2y for 4h) so everything near current price is far inside the window. The
FORWARD-LOOK CONTRACT (level_available_ts gating) is untouched.
"""
from __future__ import annotations
import gc
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pandas as pd

from detectors import paths
from shared import csvtail
from service.pairs import (MEMORY_TFS, REGEN_WINDOW_DAYS, TF_MS,
                           BACKFILL_5M_MAX_DAYS)

DAY_MS = 24 * 60 * 60 * 1000

# TFs whose detector takes a separate 1h bias CSV (1h and 5m self-bias).
_BIAS_TFS = {"1w", "1d", "4h", "2h"}

# ------------------------------------------------------------ restart logic
# A restart used to regenerate every timeframe of every pair (~15 min for 19
# pairs) whether or not anything had changed. Now a timeframe is regenerated
# at startup only if (a) new candles arrived, (b) its artifact is missing or
# older than its CSV, or (c) the code that produces artifacts changed since
# they were written. (c) is detected with a fingerprint of the detection
# source files, stored per pair in regen_marker.json — so deploying a detector
# change forces exactly one full regen, with no version number to remember.
_FINGERPRINT_FILES = (
    ["detectors/range_detector_core.py"]
    + [f"detectors/range_detector_{tf}.py" for tf in ("1w", "1d", "4h", "2h", "1h", "5m")]
    + ["detectors/compute_cleanness.py", "detectors/cascade_nesting.py",
       "detectors/compute_known_at.py", "detectors/layer5_range_memory.py",
       "detectors/layer5_1_strength.py",
       "shared/structure_analyzer.py", "shared/structure_analyzer_close_based.py",
       "shared/pricefmt.py", "service/pairs.py"]
)
_fingerprint_cache: str | None = None


def code_fingerprint(files=None, root: Path = ROOT) -> str:
    """Short hash of the source files that determine detector/chain output.
    Line endings are normalised so a Windows checkout and the Linux image of
    the same commit agree."""
    global _fingerprint_cache
    if files is None and _fingerprint_cache is not None:
        return _fingerprint_cache
    import hashlib
    h = hashlib.sha256()
    for rel in sorted(files if files is not None else _FINGERPRINT_FILES):
        h.update(rel.encode("utf-8"))
        h.update(b"\0")
        h.update((Path(root) / rel).read_bytes().replace(b"\r\n", b"\n"))
        h.update(b"\0")
    fp = h.hexdigest()[:16]
    if files is None:
        _fingerprint_cache = fp
    return fp


def marker_path(pair: str) -> Path:
    return paths.results_dir(pair) / "regen_marker.json"


def read_marker(pair: str) -> dict:
    from shared.ioutil import read_json
    m = read_json(marker_path(pair), default={})
    return m if isinstance(m, dict) else {}


def write_marker(pair: str) -> None:
    from shared.ioutil import atomic_write_json
    atomic_write_json(marker_path(pair), {
        "fingerprint": code_fingerprint(),
        "written_at": pd.Timestamp.now(tz="UTC").isoformat(timespec="seconds")})


def l1_is_stale(pair: str, tf: str) -> bool:
    """True when the layer-1 artifact is missing, or older than its candle
    CSV (candles were appended but the detector has not run since — e.g. the
    process died between fetch and regen)."""
    l1, csv = paths.l1_json(tf, pair), paths.raw_csv(tf, pair)
    try:
        return l1.stat().st_mtime < csv.stat().st_mtime
    except OSError:
        return True


def chain_outputs_missing(pair: str) -> bool:
    return any(not paths.mem_json(tf, pair).exists() for tf in MEMORY_TFS)


def behind_tfs(pair: str, boundary_ms: int, tfs=("1d", "4h", "2h", "1h")) -> list[str]:
    """Timeframes whose CSV does not yet hold the bar that closed at or before
    `boundary_ms`. The structural loop only knows which timeframes close ON a
    boundary; a boundary missed while the process was stalled or down, or a
    fetch that failed, would otherwise leave that timeframe stale until its
    own next close (up to a day for 1d). 1w is excluded: it is polled at the
    daily boundary and a fetch returning nothing is normal."""
    out = []
    for tf in tfs:
        step = TF_MS[tf]
        last_closed_open = (boundary_ms // step) * step - step
        last = csvtail.last_ts_ms(paths.raw_csv(tf, pair))
        if last is not None and last < last_closed_open:
            out.append(tf)
    return out


def _tmp_dir(pair: str) -> Path:
    d = paths.results_dir(pair) / ".tmp"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _window_slice(pair: str, tf: str, days: int, tag: str) -> Path:
    """Write data/raw/{pair}_{tf}.csv restricted to the last `days` days into
    the pair's .tmp dir and return the slice's path.

    Reads only the file's TAIL (shared/csvtail): this runs every 5 minutes per
    pair, and parsing a whole 5m history (~950k rows for BTC) to keep its last
    ~13k was the worker's largest avoidable memory and CPU cost."""
    src = paths.raw_csv(tf, pair)
    last = csvtail.last_ts_ms(src)
    if last is None:
        raise RuntimeError(f"{src.name}: no candle rows to slice")
    cutoff = last - days * DAY_MS
    est_rows = int(days * DAY_MS // TF_MS[tf]) + 16
    df = csvtail.read_tail(src, rows=est_rows, since_ms=cutoff)
    df = df[df["timestamp"] >= cutoff]
    dst = _tmp_dir(pair) / f"{pair}_{tf}_{tag}.csv"
    df.to_csv(dst, index=False)
    del df
    gc.collect()
    return dst


def fetch_tf(pair: str, tf: str, log=print) -> int:
    """Extend the raw CSV for one TF from OKX. Returns rows appended.

    Calls okx_fetch.extend_csv DIRECTLY — not check_and_update — because the
    freshness monitor's STALE_HOURS gate is a reactive heuristic (~2-6x the
    bar interval) that never trips right at bar close; the worker KNOWS a bar
    just closed, so it must fetch unconditionally. extend_csv is idempotent
    (appends only ts > last CSV ts; returns 0 when nothing new).

    A missing/empty raw CSV raises (FileNotFoundError from extend_csv) —
    that's a config error the worker must alert on, never silently skip."""
    from data.freshness_monitor import _okx, _base
    n, _first, last_new = _okx.extend_csv(_base(pair), tf,
                                          data_dir=paths.data_root())
    if log and n:
        import datetime as dt
        last_s = dt.datetime.fromtimestamp(last_new / 1000, tz=dt.timezone.utc
                                           ).strftime("%Y-%m-%d %H:%M UTC") if last_new else "?"
        log(f"[regen] {pair} {tf}: +{n} bars (last {last_s})")
    return int(n)


def bootstrap_tf(pair: str, tf: str, log=print) -> int:
    """Fetch a NEW pair's history for one TF from OKX (Stage 8.1). Unlike
    fetch_tf, this can start from an empty/missing CSV: it fetches the
    regen window (full history for 1d/1w) with a small margin and writes
    the CSV from scratch. Idempotent — an existing CSV is just extended.
    Rate limits are respected by okx_fetch's per-page sleep. Returns rows
    added."""
    import time as _t
    from data.freshness_monitor import _okx
    csv_path = paths.raw_csv(tf, pair)
    if csv_path.exists() and _okx.read_last_ts_ms(csv_path):
        return fetch_tf(pair, tf, log=log)
    # 1h gets FULL history (not its 1y regen window): it is the bias input
    # for the full-history 1d/1w detectors and the source of the 4h regen's
    # 2y slice — matching what the seed pairs have.
    days = None if tf == "1h" else REGEN_WINDOW_DAYS.get(tf)
    start_ms = None
    if days:
        start_ms = int(_t.time() * 1000) - (days + 3) * DAY_MS   # +3d margin
    inst = paths.okx_inst_id(pair)
    rows = _okx.fetch_okx_candles(inst, _okx.TF_TO_OKX[tf], start_ms=start_ms)
    if not rows:
        raise RuntimeError(f"bootstrap {pair} {tf}: OKX returned no candles")
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    n = _okx.append_to_csv(csv_path, rows)
    if log:
        log(f"[bootstrap] {pair} {tf}: fetched {n} bars "
            f"({'full history' if not days else f'{days}d window'})")
    return n


def _prepend_rows_to_csv(csv_path, rows) -> int:
    """Prepend older-than-existing OHLCV rows to a CSV by streaming: header +
    new rows + existing data bytes. Never parses the existing file (a legacy
    5m CSV is ~80MB). Atomic via same-dir temp + os.replace."""
    import csv as _csv
    import os as _os
    tmp = csv_path.with_name(f".{csv_path.name}.bf{_os.getpid()}")
    with csv_path.open("rb") as src:
        header = src.readline().decode("utf-8", errors="replace")
        with tmp.open("w", newline="", encoding="utf-8") as out:
            out.write(header if header.strip()
                      else "timestamp,open,high,low,close,volume\n")
            _csv.writer(out).writerows(rows)
        with tmp.open("ab") as outb:            # existing data, streamed
            while True:
                chunk = src.read(1 << 20)
                if not chunk:
                    break
                outb.write(chunk)
    _os.replace(tmp, csv_path)
    return len(rows)


def backfill_tf(pair: str, tf: str, batch_candles: int = 1000,
                max_batches: int = 5, log=print) -> str:
    """One deep-backfill slice for chart history (Stage 8b): fetch up to
    max_batches x batch_candles older candles BEFORE the CSV's first row and
    prepend them. Writes ONLY data/raw — detectors keep their own windows.
    5m is capped at BACKFILL_5M_MAX_DAYS. Returns 'complete' | 'partial'."""
    import time as _t
    from data.freshness_monitor import _okx
    csv_path = paths.raw_csv(tf, pair)
    if not csv_path.exists():
        return "complete"                       # nothing to extend backwards
    first_ts = None
    with csv_path.open("rb") as f:
        for _ in range(3):
            line = f.readline().decode("utf-8", errors="replace")
            try:
                first_ts = int(line.split(",", 1)[0])
                break
            except ValueError:
                continue
    if first_ts is None:
        return "complete"
    floor_ms = None
    if tf == "5m":
        floor_ms = int(_t.time() * 1000) - BACKFILL_5M_MAX_DAYS * DAY_MS
        if first_ts <= floor_ms:
            return "complete"
    inst = paths.okx_inst_id(pair)
    total = 0
    for _ in range(max_batches):
        rows = _okx.fetch_okx_candles(
            inst, _okx.TF_TO_OKX[tf],
            start_ms=floor_ms, end_ms=first_ts - 1,
            max_pages=max(1, batch_candles // 100))
        rows = [r for r in rows if r[0] < first_ts]
        if not rows:
            if log and total:
                log(f"[backfill] {pair} {tf}: +{total} bars (reached listing)")
            return "complete"
        _prepend_rows_to_csv(csv_path, rows)
        total += len(rows)
        first_ts = rows[0][0]
        if floor_ms is not None and first_ts <= floor_ms:
            if log:
                log(f"[backfill] {pair} {tf}: +{total} bars (reached {BACKFILL_5M_MAX_DAYS}d cap)")
            return "complete"
        _t.sleep(1.0)                           # politeness between batches
    if log and total:
        log(f"[backfill] {pair} {tf}: +{total} bars (more remains)")
    return "partial"


def regen_detector(pair: str, tf: str, log=print) -> float:
    """Run layer-1 detection for one TF (windowed where configured).
    Returns the run's duration in seconds."""
    t0 = time.monotonic()
    days = REGEN_WINDOW_DAYS.get(tf)

    if tf == "5m":
        import detectors.range_detector_5m as mod
        data = _window_slice(pair, "5m", days, "win") if days else None
        mod.run(data_5m=data, pair=pair)
    elif tf == "1h":
        import detectors.range_detector_1h as mod
        data = _window_slice(pair, "1h", days, "win") if days else None
        mod.run(data_1h=data, pair=pair)
    elif tf in _BIAS_TFS:
        import importlib
        mod = importlib.import_module(f"detectors.range_detector_{tf}")
        kw = {"pair": pair}
        if days:
            kw[f"data_{tf}"] = _window_slice(pair, tf, days, "win")
            kw["data_1h"] = _window_slice(pair, "1h", days, "win1h")
        mod.run(**kw)
    else:
        raise ValueError(f"unknown service TF: {tf}")

    gc.collect()
    dt = time.monotonic() - t0
    if log:
        log(f"[regen] {pair} {tf}: layer-1 regenerated in {dt:.1f}s"
            + (f" (window {days}d)" if days else " (full history)"))
    return dt


def regen_chain(pair: str, log=print) -> float:
    """Re-run the derived chain for the memory TFs, in the load-bearing order.
    MUST follow any detector re-run of a memory TF (the detector wipes the
    cleanness/nesting/known_at fields; a partial chain silently empties the
    level registry — see MEMORY_HYGIENE.md). Returns duration in seconds."""
    t0 = time.monotonic()
    import detectors.compute_cleanness as _cc
    import detectors.cascade_nesting as _cn
    import detectors.compute_known_at as _ck
    import detectors.layer5_range_memory as _l5
    import detectors.layer5_1_strength as _l51
    for tf in MEMORY_TFS:
        _cc.compute_for_tf(tf, pair)
    _cn.run(verbose=False, pair=pair)
    for tf in MEMORY_TFS:
        _ck.compute_for_tf(tf, pair)
    gc.collect()
    _l5.run(verbose=False, pair=pair)
    gc.collect()
    _l51.run(verbose=False, pair=pair)
    gc.collect()
    dt = time.monotonic() - t0
    if log:
        log(f"[regen] {pair}: derived chain (cleanness->nesting->known_at->L5->L5.1) in {dt:.1f}s")
    return dt
