"""
detectors/replay_validate.py — forward-only replay audit of the DETECTION layer.

The layer-4 backtests were point-in-time at the STRATEGY level (events sliced by
ts). But ranges and events are computed in BATCH over the whole series. If the
detector uses future bars to set a range's bands, its confirm, or its end, then
"events <= T" still carries hindsight baked into those values.

This harness re-runs `detect_ranges` forward-only and compares to a batch
reference computed on the SAME (possibly sliced) data, answering:
  * CONFIRM timing — at which bar does a forward run FIRST report each batch
    range confirmed? (0 = honest; >0 = replay lagged; <0 = batch backdated)
  * BAND evolution — bands at first-confirm vs the batch FINAL bands.
  * END-known lag — at which bar does a forward run first report a range ENDED?
    The engine waits `recovery_lookahead` bars to rule out a recovery, so batch
    `range_end_ts` is knowable only end+recovery_lookahead — the leak layer 4
    inherited (it treated a level as existing at source_end_ts).
  * RETRACTED ranges — confirmed in a forward run but dropped by batch
    (non-monotonic = a real bug, not just a lag).

Self-contained: the batch reference is recomputed from the sliced data, so any
--start/--end window is internally consistent (no dependence on stored JSON).
bias is sliced to <= window end but feeds only `ema_1h_state` metadata, not the
audited fields.

CLI (run these yourself — heavy passes are not auto-run):
  python -m detectors.replay_validate --tf 1w 1d                # full per-bar
  python -m detectors.replay_validate --tf 4h --mode anchored --start 2025-06-01 --end 2026-06-01
  python -m detectors.replay_validate --tf 1h --mode anchored --start 2025-12-01 --end 2026-06-01
Default mode: full for 1w/1d, anchored for 4h/2h/1h.
"""
from __future__ import annotations
import argparse, gc, json, sys, time
from pathlib import Path
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _rss_mb():
    """Best-effort resident-set size in MB (peak-memory self-check)."""
    try:
        import psutil
        return psutil.Process().memory_info().rss / 1e6
    except Exception:
        pass
    try:
        import ctypes
        k32 = ctypes.WinDLL("kernel32"); psapi = ctypes.WinDLL("psapi")

        class PMC(ctypes.Structure):
            _fields_ = [("cb", ctypes.c_ulong), ("pf", ctypes.c_ulong),
                        ("pws", ctypes.c_size_t), ("ws", ctypes.c_size_t)] + \
                       [(c, ctypes.c_size_t) for c in "abcdef"]
        k32.GetCurrentProcess.restype = ctypes.c_void_p
        psapi.GetProcessMemoryInfo.argtypes = [ctypes.c_void_p, ctypes.POINTER(PMC), ctypes.c_ulong]
        c = PMC(); c.cb = ctypes.sizeof(PMC)
        psapi.GetProcessMemoryInfo(k32.GetCurrentProcess(), ctypes.byref(c), c.cb)
        return c.ws / 1e6
    except Exception:
        return None

from detectors.range_detector_core import detect_ranges
import detectors.range_detector_1w as m1w
import detectors.range_detector_1d as m1d
import detectors.range_detector_4h as m4h
import detectors.range_detector_2h as m2h
import detectors.range_detector_1h as m1h

CFG = {"1w": m1w.config, "1d": m1d.config, "4h": m4h.config,
       "2h": m2h.config, "1h": m1h.config}
CSV = lambda tf: ROOT / f"data/raw/SOL_USDT_{tf}.csv"


def _bands(r):
    g = lambda v: round(v, 4) if v is not None else None
    return (g(r.range_low_lower), g(r.range_low_upper),
            g(r.range_high_lower), g(r.range_high_upper))


def _slice(df, start, end):
    ts = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    mask = pd.Series(True, index=df.index)
    if start:
        mask &= ts >= pd.Timestamp(start, tz="UTC")
    if end:
        mask &= ts <= pd.Timestamp(end, tz="UTC")
    return df[mask].reset_index(drop=True)


def replay(tf, mode="auto", start=None, end=None, verbose=True):
    cfg = CFG[tf]()
    if mode == "auto":
        mode = "full" if tf in ("1w", "1d") else "anchored"
    df = (pd.read_csv(CSV(tf)).drop_duplicates("timestamp")
          .sort_values("timestamp").reset_index(drop=True))
    df = _slice(df, start, end)
    n = len(df)
    if n < cfg.analyzer_init_bars + 5:
        return {"tf": tf, "error": f"slice too short ({n} bars)"}
    # MEMORY: bias feeds only the ema_1h_state METADATA (not ranges/bands/events),
    # so we use each forward window AS ITS OWN bias instead of re-processing the
    # full ~50k-row 1h series on every re-run. This keeps each iteration O(window)
    # and lets memory stay flat — the audited fields are bias-independent.
    ts_iso = [t.isoformat() for t in pd.to_datetime(df["timestamp"], unit="ms", utc=True)]
    idx_of = {t: i for i, t in enumerate(ts_iso)}
    rec_la = cfg.recovery_lookahead

    # batch reference on the (sliced) data (window IS its own bias)
    _, batch_ranges = detect_ranges(df, df, cfg)
    batch_conf = [r for r in batch_ranges if r.is_confirmed]
    batch_keys = {r.range_phase_2_ts: r for r in batch_conf}

    start_i = max(cfg.analyzer_init_bars + 2, 5)
    if mode == "full":
        checkpoints = list(range(start_i, n))
    else:
        cps = set()
        for r in batch_conf:
            ci = idx_of.get(r.range_phase_2_ts)
            if ci is not None:
                cps.update({ci - 1, ci})
            if r.range_end_ts and r.range_end_ts in idx_of:
                ei = idx_of[r.range_end_ts]
                cps.update({ei, ei + rec_la})
        checkpoints = sorted(i for i in cps if start_i <= i < n)

    first_seen, first_ended, ever_seen = {}, {}, set()
    last_end, stable_since, retracted_ends = {}, {}, 0   # end-monotonicity (O(ranges) memory)
    t0 = time.time(); peak_rss = _rss_mb() or 0.0
    total = len(checkpoints)
    for c, i in enumerate(checkpoints):
        dfi = df.iloc[:i + 1]
        try:
            _, ranges_i = detect_ranges(dfi, dfi, cfg)     # window is its own bias
        except Exception:
            ranges_i = []
        for r in ranges_i:
            if not r.is_confirmed:
                continue
            k = r.range_phase_2_ts
            ever_seen.add(k)
            if k not in first_seen:
                first_seen[k] = (i, _bands(r))
            if r.range_end_ts and k not in first_ended:
                first_ended[k] = (i, r.range_end_ts)
            # end-monotonicity: an end declared then withdrawn/changed at a later
            # checkpoint = a (premature) retraction. stable_since[k] = the
            # checkpoint the CURRENT end value was set, so the end is only STABLY
            # known from then on. A clean break declares once (stable lag ~0); an
            # ambiguous break that briefly recovers retracts then re-settles
            # (stable lag up to recovery_lookahead, retracted_ends > 0).
            cur_end = r.range_end_ts
            prev = last_end.get(k, "__init__")
            if cur_end != prev:
                stable_since[k] = i
                if prev not in ("__init__", None):
                    retracted_ends += 1
            last_end[k] = cur_end
        del ranges_i, dfi
        if c % 10 == 0:                                     # progress + memory watch
            cur = _rss_mb() or 0.0; peak = max(peak_rss, cur); peak_rss = peak
            if verbose:
                print(f"   [{tf}] bar {c}/{total}  win={i+1}  RSS={cur:.0f}MB  ({time.time()-t0:.0f}s)",
                      flush=True)
        if c % 50 == 0:
            gc.collect()
    peak_rss = max(peak_rss, _rss_mb() or 0.0)

    rows = []
    for k, br in batch_keys.items():
        ci = idx_of.get(k); fs = first_seen.get(k); fe = first_ended.get(k)
        fb = (br.range_low_lower, br.range_low_upper, br.range_high_lower, br.range_high_upper)
        band_drift = None
        if fs:
            band_drift = round(max(abs((a or 0) - (b or 0)) for a, b in zip(fs[1], fb)), 4)
        end_lag = None                       # first end-declaration (can be premature)
        if br.range_end_ts and fe and br.range_end_ts in idx_of:
            end_lag = fe[0] - idx_of[br.range_end_ts]
        stable_end_lag = None                 # honest: checkpoint the end STABLY settled
        ss = stable_since.get(k)
        if br.range_end_ts and ss is not None and br.range_end_ts in idx_of:
            stable_end_lag = ss - idx_of[br.range_end_ts]
        rows.append({"key": k,
                     "confirm_lag": (fs[0] - ci) if (fs and ci is not None) else None,
                     "band_drift": band_drift, "end_lag": end_lag,
                     "stable_end_lag": stable_end_lag,
                     "ever_confirmed_in_replay": k in ever_seen})
    return {"tf": tf, "mode": mode, "start": start, "end": end, "n_bars": n,
            "n_checkpoints": len(checkpoints), "n_batch_confirmed": len(batch_conf),
            "rec_lookahead": rec_la, "rows": rows,
            "retracted": sorted(ever_seen - set(batch_keys)),
            "retracted_end_count": retracted_ends,
            "peak_rss_mb": round(peak_rss, 0),
            "elapsed_s": round(time.time() - t0, 1)}


def _report(res):
    if res.get("error"):
        print(f"\n=== {res['tf']}: SKIPPED — {res['error']} ==="); return res
    rows = res["rows"]
    seen = [r for r in rows if r["ever_confirmed_in_replay"]]
    never = [r for r in rows if not r["ever_confirmed_in_replay"]]
    cl = [r["confirm_lag"] for r in seen if r["confirm_lag"] is not None]
    el = [r["end_lag"] for r in rows if r["end_lag"] is not None]
    sel = [r["stable_end_lag"] for r in rows if r["stable_end_lag"] is not None]
    bd = [r["band_drift"] for r in seen if r["band_drift"] is not None]
    backdated = [r for r in seen if r["confirm_lag"] is not None and r["confirm_lag"] < 0]
    win = f"{res['start'] or 'begin'}..{res['end'] or 'end'}"
    print(f"\n=== {res['tf']} replay audit ({res['mode']}, {win}, {res['n_bars']} bars, "
          f"{res['n_checkpoints']} checkpoints, {res['elapsed_s']}s, peak RSS {res.get('peak_rss_mb','?')}MB) ===")
    print(f"  batch confirmed ranges: {res['n_batch_confirmed']}  recovery_lookahead={res['rec_lookahead']}")
    print(f"  CONFIRM lag (replay_first_confirm - batch_confirm_bar): "
          f"min={min(cl) if cl else '-'} median={int(np.median(cl)) if cl else '-'} max={max(cl) if cl else '-'}")
    print(f"     never confirmed in replay before final run: {len(never)}")
    print(f"  END first-declared lag (often premature/0 at the data edge): "
          f"min={min(el) if el else '-'} median={int(np.median(el)) if el else '-'} max={max(el) if el else '-'}")
    print(f"  END stable lag (honest — end settled & stayed; ambiguous breaks ~recovery_lookahead): "
          f"min={min(sel) if sel else '-'} median={int(np.median(sel)) if sel else '-'} max={max(sel) if sel else '-'} "
          f"(<= rec_lookahead={res['rec_lookahead']} OK; > it = deeper issue)")
    rec = res.get("retracted_end_count", 0)
    print(f"  RETRACTED ENDS (declared then withdrawn = premature edge declarations; expected on "
          f"ambiguous breaks, Layer 4 insulated via level_available_ts): {rec}")
    print("  BAND drift final-vs-first-confirm $: "
          + (f"median={np.median(bd):.3f} max={max(bd):.3f}" if bd else "-"))
    print(f"  RETRACTED (replay-confirmed, batch-dropped = BUG): {len(res['retracted'])}"
          + (f"  {res['retracted'][:6]}" if res['retracted'] else ""))
    print(f"  BACKDATED confirms (replay earlier than batch = BUG): {len(backdated)}")
    verdict = ("FORWARD-ONLY OK (lags are the known recovery_lookahead window)"
               if not res["retracted"] and not backdated and (max(cl) if cl else 0) <= 0
               else "DIVERGENCES — inspect")
    print(f"  >> {verdict}")
    return res


def main():
    p = argparse.ArgumentParser(description="Forward-only replay audit of the detection layer.")
    p.add_argument("--tf", nargs="+", default=["1w", "1d"], choices=list(CFG))
    p.add_argument("--mode", default="auto", choices=["auto", "full", "anchored"])
    p.add_argument("--start", default=None, help="UTC date e.g. 2025-06-01 (slice lower bound)")
    p.add_argument("--end", default=None, help="UTC date e.g. 2026-06-01 (slice upper bound)")
    a = p.parse_args()
    for tf in a.tf:
        res = replay(tf, mode=a.mode, start=a.start, end=a.end)
        _report(res)
        tag = f"{tf}_{a.start or 'b'}_{a.end or 'e'}".replace("-", "")
        (ROOT / f"backtesting/results/replay_audit_{tag}.json").write_text(
            json.dumps(res, indent=2), encoding="utf-8")


if __name__ == "__main__":
    from shared.memhygiene import install        # Memory hygiene: see MEMORY_HYGIENE.md
    install("replay_validate")
    main()
