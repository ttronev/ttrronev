"""
data/bootstrap_5m.py — one-time bootstrap of data/raw/SOL_USDT_5m.csv from OKX
SWAP (full available 5m history).

WHY a dedicated script: okx_fetch's CLI only EXTENDS an existing CSV
(extend_csv raises FileNotFoundError on a missing file by design). Bootstrapping
a brand-new TF means calling fetch_okx_candles() + append_to_csv() directly with
start_ms=None (= walk back to the earliest bar OKX still serves). OKX history-
candles retains ~1-2y of 5m, so expect ~150k+ rows and a few minutes of paging.

HEAVY — run ONCE (it's yours to run). After this, the 5m CSV is kept current the
normal way (okx_fetch --extend, once 5m is registered in freshness_monitor).

Memory hygiene: see docs/runbooks/memory_hygiene.md. The full row list for ~150k 5m bars is
~30-50MB (tuples) + the dedup ts-set — well under the 500MB utility ceiling;
peak RSS is self-reported on exit.

Usage:
    .venv\\Scripts\\python.exe -m data.bootstrap_5m
    .venv\\Scripts\\python.exe -m data.bootstrap_5m --start-date 2024-01-01   # optional floor
"""
from __future__ import annotations
import argparse
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Load okx_fetch by path (same pattern as freshness_monitor — avoids importing
# heavy/legacy package siblings).
_spec = importlib.util.spec_from_file_location("okx_fetch", ROOT / "data" / "okx_fetch.py")
_okx = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(_okx)

PAIR = "SOL"
TF = "5m"
INST = "SOL-USDT-SWAP"          # SWAP, matching the rest of the cascade's CSVs (NOT spot)
CSV = ROOT / "data" / "raw" / f"{PAIR}_USDT_{TF}.csv"


def main():
    p = argparse.ArgumentParser(description="Bootstrap SOL 5m CSV from OKX SWAP (full history).")
    p.add_argument("--start-date", default=None,
                   help="Optional UTC floor 'YYYY-MM-DD' (default: earliest OKX serves).")
    p.add_argument("--force", action="store_true",
                   help="Re-fetch even if the CSV already exists (appends/dedups).")
    a = p.parse_args()

    existing_last = _okx.read_last_ts_ms(CSV)
    if existing_last is not None and not a.force:
        print(f"[bootstrap_5m] {CSV.name} already exists (last bar "
              f"{_okx._fmt_ts(existing_last)}). It only needs EXTENDING now:")
        print(f"               .venv\\Scripts\\python.exe -m data.okx_fetch --pair SOL --tf 5m")
        print(f"               (re-run with --force to re-bootstrap full history.)")
        return

    start_ms = None
    if a.start_date:
        import datetime as dt
        start_ms = int(dt.datetime.strptime(a.start_date, "%Y-%m-%d")
                       .replace(tzinfo=dt.timezone.utc).timestamp() * 1000)

    floor = a.start_date or "earliest available"
    print(f"[bootstrap_5m] fetching FULL 5m history for {INST} from OKX "
          f"(floor={floor}); paginates backwards ~150k+ bars, a few minutes...",
          flush=True)
    rows = _okx.fetch_okx_candles(inst_id=INST, bar="5m", start_ms=start_ms, page_size=100)
    if not rows:
        print("[bootstrap_5m] NO ROWS returned — check connectivity / instId. Nothing written.")
        return

    n = _okx.append_to_csv(CSV, rows)
    print(f"[bootstrap_5m] wrote {n} new rows -> {CSV}")
    print(f"[bootstrap_5m] range: {_okx._fmt_ts(rows[0][0])}  ->  {_okx._fmt_ts(rows[-1][0])}")
    print(f"[bootstrap_5m] total bars fetched: {len(rows)}  "
          f"(~{len(rows) * 5 / 60 / 24:.0f} days of 5m)")


if __name__ == "__main__":
    from shared.memhygiene import install        # Memory hygiene: see docs/runbooks/memory_hygiene.md
    install("bootstrap_5m")
    main()
