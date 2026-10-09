"""
data/freshness_monitor.py — active, automatic data freshness for the
range-detector pipeline.

The saved "freshness-first" workflow was reactive (engineer checks before
running). This makes it active: detectors call check_and_update() at
startup, which auto-fetches stale TFs from OKX before detection runs.
A standalone --watch mode keeps data current on a loop for eventual live
signal work.

Staleness thresholds (last CLOSED bar older than this => stale; set to ~2x
the bar interval so the still-forming current bar never trips it):
    1h: 2h   2h: 4h   4h: 8h   1d: 36h   1w: 8 days

Source: OKX only (Binance/Bybit geo-blocked). Daily/weekly use the *utc*
bar variants (handled in okx_fetch.TF_TO_OKX) for 00:00-UTC alignment.
"""
from __future__ import annotations

import argparse
import datetime as dt
import importlib.util
import sys
import time
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Load okx_fetch by path (avoids importing heavy/legacy package siblings).
_spec = importlib.util.spec_from_file_location("okx_fetch", ROOT / "data" / "okx_fetch.py")
_okx = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(_okx)

from detectors import paths as _paths       # path resolution only (stdlib-light)

# Pair convention: the FULL CSV prefix, e.g. "SOL_USDT" (see detectors/paths.py).
DEFAULT_PAIR = "SOL_USDT"

STALE_HOURS = {"5m": 0.5, "1h": 2, "2h": 4, "4h": 8, "1d": 36, "1w": 8 * 24}
TFS = ["1h", "2h", "4h", "1d", "1w"]     # default check set; 5m is fetched
                                          # on-demand by the service worker


def _base(pair: str) -> str:
    """'SOL_USDT' -> 'SOL' (okx_fetch's pair convention)."""
    return pair.split("_")[0]


def _csv_path(tf: str, pair: str = DEFAULT_PAIR) -> Path:
    return _paths.raw_csv(tf, pair)          # honours TTRRONEV_DATA_ROOT


def _last_ts_ms(tf: str, pair: str = DEFAULT_PAIR):
    p = _csv_path(tf, pair)
    if not p.exists():
        return None
    # MAX timestamp (robust to any row-order issues), read only the column.
    s = pd.read_csv(p, usecols=["timestamp"])["timestamp"]
    return int(s.max()) if len(s) else None


def _fmt(ms):
    if ms is None:
        return "—"
    return dt.datetime.fromtimestamp(ms / 1000, tz=dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def get_live_price(inst_id: str = "SOL-USDT-SWAP", log=None):
    """Live last-traded price from OKX's public ticker (no API key). For DISPLAY
    and PROXIMITY only — the detectors consume CLOSED bars ONLY and never see
    this. SOL-USDT-SWAP matches the candle CSVs (also SWAP). Reuses okx_fetch's
    _http_get (requests + retry; a bare urllib User-Agent gets WAF-blocked).
    Returns a float, or None on failure (callers fall back to last close)."""
    try:
        d = _okx._http_get(f"{_okx.OKX_BASE}/api/v5/market/ticker", {"instId": inst_id})
        if d.get("code") == "0" and d.get("data"):
            return float(d["data"][0]["last"])
    except Exception as e:
        if log:
            log(f"[freshness] live price fetch failed: {type(e).__name__}")
    return None


def check_and_update(tfs=None, auto_fetch: bool = True, pair: str = DEFAULT_PAIR,
                     log=print) -> dict:
    """Time anchor + freshness check for each TF. If a TF is stale and
    auto_fetch, extend it from OKX. Returns a per-TF report dict and logs
    the current UTC time + each TF's last bar / age / action."""
    tfs = tfs or TFS
    now_ms = int(time.time() * 1000)
    now_iso = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    report = {"checked_at": now_iso, "tfs": {}}
    if log:
        log(f"[freshness] time anchor: now={now_iso}")
    for tf in tfs:
        last = _last_ts_ms(tf, pair)
        age_h = (now_ms - last) / 3.6e6 if last is not None else float("inf")
        stale = age_h > STALE_HOURS[tf]
        fetched = 0
        err = None
        if stale and auto_fetch and last is not None:
            try:
                n, _f, _l = _okx.extend_csv(_base(pair), tf,
                                            data_dir=_paths.data_root())
                fetched = int(n)
                last = _last_ts_ms(tf, pair)
                age_h = (now_ms - last) / 3.6e6
            except Exception as e:  # never let a fetch failure block detection
                err = str(e)[:120]
        report["tfs"][tf] = {
            "last_bar": _fmt(last), "age_hours": round(age_h, 1),
            "threshold_hours": STALE_HOURS[tf], "was_stale": stale,
            "fetched": fetched, "error": err,
        }
        if log:
            if err:
                status = f"FETCH FAILED: {err}"
            elif fetched:
                status = f"UPDATED +{fetched}"
            elif stale:
                # threshold tripped but OKX has no newer closed bar -> the
                # current bar is simply still forming; data IS up to date.
                status = "current (latest closed bar; awaiting next)"
            else:
                status = "fresh"
            log(f"  {tf:>3}: last={_fmt(last)}  age={age_h:>5.1f}h "
                f"(thr {STALE_HOURS[tf]}h)  [{status}]")
    return report


def _regen_all(log=print, pair: str = DEFAULT_PAIR):
    """Re-run the FULL derived chain in-process, memory-bounded (sequential
    with gc between stages). Order is load-bearing: a detector re-run WIPES the
    cleanness / nesting / known_at fields, so they MUST be re-applied after:
        detector -> cleanness -> nesting -> known_at -> layer5 -> layer5.1.
    (This is the cascade-aware regen — Option B. Functions are called directly,
    so the detectors' own __main__ freshness hooks do NOT re-trigger.)"""
    import gc
    sys.path.insert(0, str(ROOT))
    import detectors.range_detector_1w as _m1w
    import detectors.range_detector_1d as _m1d
    import detectors.range_detector_4h as _m4h
    import detectors.range_detector_2h as _m2h
    import detectors.range_detector_1h as _m1h
    import detectors.compute_cleanness as _cc
    import detectors.cascade_nesting as _cn
    import detectors.compute_known_at as _ck
    import detectors.layer5_range_memory as _l5
    import detectors.layer5_1_strength as _l51
    dmods = {"1w": _m1w, "1d": _m1d, "4h": _m4h, "2h": _m2h, "1h": _m1h}
    order = ["1w", "1d", "4h", "2h", "1h"]
    if log: log("[regen] layer-1 detection (5 TFs)...")
    for tf in order:
        dmods[tf].run(pair=pair); gc.collect()
    if log: log("[regen] cleanness...")
    for tf in order:
        _cc.compute_for_tf(tf, pair)
    if log: log("[regen] cascade nesting...")
    _cn.run(verbose=False, pair=pair)
    if log: log("[regen] known_at...")
    for tf in order:
        _ck.compute_for_tf(tf, pair)
    gc.collect()
    if log: log("[regen] layer-5 range memory...")
    _l5.run(verbose=False, pair=pair); gc.collect()
    if log: log("[regen] layer-5.1 strength...")
    _l51.run(verbose=False, pair=pair); gc.collect()
    if log: log("[regen] derived chain regenerated.")


def ensure_fresh(regen: bool = False, pair: str = DEFAULT_PAIR, log=print) -> dict:
    """Consumer entry point. Refresh CSVs from OKX (check_and_update); if
    `regen` and any TF got new bars, re-run the full derived chain so the
    downstream JSONs aren't left stale. Returns the freshness report."""
    report = check_and_update(pair=pair, log=log)
    updated = [tf for tf, info in report["tfs"].items() if info.get("fetched", 0) > 0]
    if regen and updated:
        if log:
            log(f"[freshness] {len(updated)} TF(s) got new bars {updated} -> "
                f"cascading regen of derived files (heavier path)...")
        _regen_all(log=log, pair=pair)
    elif regen and log:
        log("[freshness] no new bars -> derived files current, skipping regen.")
    return report


def consumer_startup(end_consumer: bool = False, pair: str = DEFAULT_PAIR):
    """Freshness hook for consumer scripts — call at the top of __main__.
    Parses --no-freshness (skip the whole check) and --no-regen (fetch but
    don't cascade-regen; honored for end-consumers only).

    END-CONSUMERS (read DERIVED files: query_state, layer4 setups) default to
    regen-on-update so the user never has to remember the 6-step chain.
    REGEN-CHAIN scripts (cleanness/nesting/known_at/layer5/5.1) only refresh the
    CSVs (regen=False) — they ARE the regen, auto-regen would be circular.
    See docs/runbooks/memory_hygiene.md 'Freshness contract'."""
    p = argparse.ArgumentParser(add_help=False)
    p.add_argument("--no-freshness", action="store_true")
    p.add_argument("--no-regen", action="store_true")
    a, _ = p.parse_known_args()
    if a.no_freshness:
        return
    if _paths.is_sandboxed():
        # A sandboxed run (TTRRONEV_RESULTS_ROOT set: research, experiment,
        # test) must never append candles the live worker is also appending,
        # nor trigger a full regen. It reads what its sandbox holds.
        print(f"[freshness] sandbox ({_paths.ENV_RESULTS_ROOT} is set) -> "
              f"no OKX fetch, no cascade regen.", flush=True)
        return
    ensure_fresh(regen=(end_consumer and not a.no_regen), pair=pair)


def watch(interval_s: int = 300, pair: str = DEFAULT_PAIR):
    """Run forever, auto-updating every interval_s seconds."""
    print(f"[freshness] --watch started, interval={interval_s}s. Ctrl-C to stop.")
    while True:
        check_and_update(pair=pair)
        time.sleep(interval_s)


def main():
    p = argparse.ArgumentParser(description="SOL data freshness monitor (OKX).")
    p.add_argument("--watch", action="store_true", help="Run forever, refresh every --interval.")
    p.add_argument("--interval", type=int, default=300, help="Watch interval seconds (default 300).")
    p.add_argument("--no-fetch", action="store_true", help="Check only, don't auto-fetch.")
    p.add_argument("--pair", default=DEFAULT_PAIR)
    a = p.parse_args()
    if a.watch:
        watch(a.interval, a.pair)
    else:
        check_and_update(auto_fetch=not a.no_fetch, pair=a.pair)


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))                # Memory hygiene: see docs/runbooks/memory_hygiene.md
    from shared.memhygiene import install
    install("freshness_monitor")
    main()
