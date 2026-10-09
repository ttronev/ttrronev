# memory_hygiene.md — memory-efficiency contract for all local scripts

This machine is RAM-constrained. **Memory-efficient by default is a hard ship
requirement, not a nice-to-have.** A script that can't meet its target gets
reworked before it ships.

## Peak-RSS targets

| Script class | Peak RSS target |
|---|---|
| Utility scripts (detectors-as-utility, cleanness, nesting, known_at, layer5/5.1, query, fetchers) | **< 500 MB** |
| Full-history replays / heavy analytical passes | **< 2 GB** |

Every script ends with a `[mem] <name> peak RSS = N MB` line (via
`shared/memhygiene.py`). If peak exceeds the target, that's a **bug**, not a
feature.

## Design rules (all future work)

1. **Stream, don't accumulate.** Process bar-by-bar where possible. Write
   outputs to disk incrementally instead of building giant in-memory lists.
   Prefer generators (`yield`) over `return [...]` for large datasets.
2. **Slice, don't load.** If a script needs only a window, slice at read time
   (`pd.read_csv(..., nrows=/skiprows=)` or chunked iteration). Don't read
   50,000 rows to use 200.
3. **Drop what you don't need.** `del` source dataframes after deriving values;
   `gc.collect()` at stage boundaries.
4. **Cap window sizes.** Forward windows (`recovery_lookahead`, etc.) cap at the
   configured value. Audit any `data[i:]` — it should usually be
   `data[i:i+window]`.
5. **No silent buffering.** `flush=True` on prints; print progress incrementally
   so it's visible AND memory doesn't grow.
6. **Avoid copies.** Use `.loc`/`.iloc` views; when a copy is needed, slice
   first then copy (`df.loc[a:b].copy()`).
7. **No backgrounded processes.** Never use subprocess/multiprocessing/threading
   without explicit cleanup — every spawned process `.join()`ed or
   `.terminate()`ed before main exits.
8. **Self-report peak RSS.** Every script ends with a peak-RSS print.

## Required teardown (every runnable script)

Use `shared/memhygiene.py`:

```python
from shared.memhygiene import finalize   # or: install
if __name__ == "__main__":
    ...                                   # work; free big objects between stages
    finalize("script_name")               # del + gc.collect + peak-RSS + sys.exit(0)
```

`finalize()` does: drop passed objects → `gc.collect()` → report peak RSS →
flush stdout/stderr → `sys.exit(0)` so Windows releases the process cleanly.
`install("name")` is the one-line atexit retrofit (gc + report on any exit
path). Also: file handles in `with`/`try-finally`; `atexit` for cleanup.

## Running discipline (who runs what)

- **Heavy passes are the user's to run.** Anything processing more than a few
  hundred bars, or invoking the replay harness, is a **command handed to the
  user** for terminal execution — never run by the assistant.
- The assistant validates an edit with a **tiny smoke test (≤ 50 bars only)**,
  confirms peak RSS is under target, then hands over a larger validation
  command.

## Freshness contract (data staleness + cascade regen)

Every script that reads `data/raw/*.csv` or `detectors/results/*.json` calls
freshness on startup via `data/freshness_monitor.consumer_startup()`, unless
`--no-freshness` is passed.

- **End-consumers** (read DERIVED files: `query_state`, `layer4_signals_*`) call
  `consumer_startup(end_consumer=True)` → refresh CSVs from OKX AND, if any TF
  got new bars, **auto-regenerate the full derived chain** (Option B) so the
  JSONs they read aren't stale. `--no-regen` fetches but skips the regen.
- **Regen-chain scripts** (`compute_cleanness`, `cascade_nesting`,
  `compute_known_at`, `layer5_range_memory`, `layer5_1_strength`) call
  `consumer_startup(end_consumer=False)` → refresh CSVs only (they ARE the
  regen; auto-regen would be circular).
- **Detectors** keep their own `--no-freshness` hook (predates consumer_startup);
  the auto-regen calls their `run()` directly, so freshness never double-fires.

**The chain order is load-bearing:** `detector → compute_cleanness →
cascade_nesting → compute_known_at → layer5_range_memory → layer5_1_strength`.
A detector re-run WIPES the cleanness / nesting / known_at fields, so they MUST
be re-applied after. A PARTIAL regen (skipping cleanness) silently empties the
level registry — this dropped all 1D levels on 2026-06-03 (None cleanness →
`_passes_cleanness` rejects every range → 0 levels). `_regen_all()` runs the full
chain in-process, memory-bounded (gc between stages).

**Modes:** `--no-freshness` = cached state, fast + deterministic (light path,
< 500 MB). default = auto-fetch + cascade regen (HEAVY path when bars arrive,
< 2 GB target). `--no-regen` = fetch but don't regen (debugging).

## User-side verification protocol

1. Open Task Manager before running any script.
2. After the terminal prompt returns, wait **5 seconds**.
3. If `python.exe` is still in the process list → that script failed cleanup.
4. Report the failing script name; it gets fixed and re-handed.

## Header reference

Every **new** script's header comment must reference this file, e.g.:
`# Memory hygiene: see docs/runbooks/memory_hygiene.md (targets, teardown via shared/memhygiene.py).`

## Retrofit status (existing scripts)

- **Retrofitted (active):** range_detector_{1w,1d,4h,2h,1h}, compute_cleanness,
  cascade_nesting, compute_known_at, layer5_range_memory, layer5_1_strength,
  layer4_signals_{retest_fail,range_to_range}, replay_validate, query_state,
  plot_range_detector (matplotlib: `plt.close('all')`), freshness_monitor,
  okx_fetch, run_bos_retest.

## Deleted dead code (2026-06-02 — DO NOT recreate)

These were superseded by `range_detector_core.py` + the `range_detector_{tf}.py`
wrappers (detection) and `okx_fetch.py` (data). They were deleted, not
retrofitted. Future-Claude: do not revive these — the active equivalents exist.

- `detectors/range_detector.py` (v1), `range_detector_v2.py`,
  `range_detector_v3.py`, `range_detector_v3_1.py`
- `detectors/plot_range_detector_v2.py`, `plot_range_detector_v3.py`,
  `plot_range_detector_v3_1.py`
- `research/backtesting/run_entry_sweep.py`, `run_hard_stop_sweep.py`,
  `run_secondary_only.py`, `run_symmetric_bos_fib.py`, `analyze_levels.py`,
  `analyze_regimes_and_bands.py`
- `data/fetch_data.py`, `data/fetch_binance.py` (Binance/ccxt — geo-blocked; OKX now)

Now-orphaned by the above (no remaining runner): `research/backtesting/secondary_only_engine_v14exp.py`
— a further deletion candidate if confirmed unused. `research/backtesting/level_features.py`
is KEPT (imported by `archive/paper_trade/level_proximity.py`).
