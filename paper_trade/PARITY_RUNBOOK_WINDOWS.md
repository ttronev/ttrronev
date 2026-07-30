# Parity test runbook — Windows

For Egor: the pre-live gate that must pass before flipping `paper_trade.run`
to live data. Three independent runs, one per pair, each verifies the
streaming engine produces byte-identical output to the batch engine on
the full 18-month evaluation window.

## Compatibility verdict

**Windows-compatible.** No subprocess calls, no POSIX-only modules, no
shell-out. Pure Python + pandas + numpy. Confirmed by code audit:

```
grep -E "subprocess|os\.fork|fcntl|posix|aiohttp|sqlite|setlocale|/dev/|/tmp"
    paper_trade/test_streaming_parity.py
    paper_trade/streaming_engine.py
    paper_trade/bar_buffer.py
# no matches
```

The test imports `paper_trade.bar_buffer`, which transitively imports
`paper_trade.config` and `paper_trade.sqlite_store`. Config creates the
data + log directories (idempotent, safe under parallel processes via
`mkdir(exist_ok=True)`). The SQLite store is import-only — no
connection opens unless `persist=True` is set on bar appends, which the
parity test deliberately bypasses (`persist=False`). **No SQLite file
gets created or written during the parity test.**

## Dependencies on Windows

Minimum, from a fresh Python 3.10+:

```powershell
pip install pandas numpy
```

The test does NOT need `aiohttp` (verified by simulating its absence).
You can install full deps later when running `paper_trade.run` for
real.

## Disk + memory per test

Measured on the dev machine for the largest pair (SOL has 5+ years of
5m data; AVAX/LINK only have the 18mo we fetched):

| Pair | 5m CSV (disk) | 1h CSV (disk) | Peak RSS per process |
|------|--------------:|--------------:|---------------------:|
| SOL  | 45.3 MB       | 3.8 MB        | ~500 MB              |
| AVAX | 11.6 MB       | 1.0 MB        | ~250 MB              |
| LINK | 11.6 MB       | 1.0 MB        | ~250 MB              |

Totals if running all three in parallel: ~1 GB RAM, ~75 MB disk reads,
zero disk writes (other than captured stdout, <10 MB).

## Parallel safety

**Yes, safe to run all three in parallel** on one machine.

- Each test is its own Python process with its own memory.
- All file I/O is read-only on independent CSV files (one pair per process).
- The bar buffer uses `persist=False` — no SQLite contention.
- Module-level state is read-only (config constants).
- `mkdir(exist_ok=True)` is race-safe under concurrent processes.

## Wall time estimates

The streaming wrapper re-runs the batch engine on every new 5m bar.
Buffer caps at 7000 5m bars after ~24 days of data, so per-bar cost is
roughly constant after warmup. For an 18-month window (~152k 5m bars):

- SOL: ~3-4 hours
- AVAX: ~3-4 hours
- LINK: ~4-6 hours (LINK has more events per unit time → more snapshots)

**Run in parallel: ~4-6 hours wall time total** (limited by LINK, the
slowest pair). Run sequentially: ~12 hours.

## How to run on Windows (parallel, recommended)

Open three PowerShell windows (or three tabs in Windows Terminal). In
each, navigate to the project root and activate the venv:

```powershell
# Window 1 — SOL
cd C:\Users\etron\Desktop\ttrronev
.venv\Scripts\activate
python -m paper_trade.test_streaming_parity --pair SOL/USDT `
    --start 2024-11-04 --end 2026-05-04 *> sol_parity.log
```

```powershell
# Window 2 — AVAX
cd C:\Users\etron\Desktop\ttrronev
.venv\Scripts\activate
python -m paper_trade.test_streaming_parity --pair AVAX/USDT `
    --start 2024-11-04 --end 2026-05-04 *> avax_parity.log
```

```powershell
# Window 3 — LINK
cd C:\Users\etron\Desktop\ttrronev
.venv\Scripts\activate
python -m paper_trade.test_streaming_parity --pair LINK/USDT `
    --start 2024-11-04 --end 2026-05-04 *> link_parity.log
```

(`*>` redirects both stdout and stderr in PowerShell.)

## What "PASS" looks like

Each run prints progress to its log file every 10%:

```
=== Parity test SOL/USDT 2024-11-04 -> 2026-05-04 ===
  5m bars: 157,249   1h bars: 50,183
  batch run: 1.1s   N events
    streaming: 0% (0/157049 bars)
    streaming: 10% (15705/157049 bars)
    ...
    streaming: 100% (157048/157049 bars)
  streaming run: XXXX.Xs   N events

  PARITY: PASS  (N events identical)
```

**Anything other than `PARITY: PASS` is a fail.** If it fails, capture
the log and report — the fail output includes the first 10 differing
events.

## Gotchas to know about

1. **Don't put the laptop to sleep.** Windows aggressive power-saving
   will throttle background Python processes. In Settings → System →
   Power & sleep, set "Sleep" to Never (plug it in first).
2. **Console code page**. Test prints are pure ASCII, so `cp1252` (the
   Windows default) doesn't matter here. If you redirect output, use
   `*>` or `2>&1 | tee`.
3. **Don't open the project files in a text editor while the tests
   run** — pandas keeps file handles open briefly during reads;
   shouldn't conflict but no reason to risk it.
4. **Background CPU usage**. Each parity test pegs one core ~100% for
   hours. Three in parallel = three cores. Modern laptops are fine; a
   smaller dual-core machine should run them sequentially.

## Reporting back

After all three finish (or any one fails), share the three log files.
Both pass + log file is enough for the gate.
