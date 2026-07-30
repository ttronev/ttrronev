# Streaming Refactor (Option C1) — Design Note v2

**Status**: design for review. Code follows after sign-off.
**Trigger**: 1-month parity diagnostic confirmed H1 (init-seed drift on 5m
buffer eviction). Slice-and-rebatch is structurally incompatible with the
analyzer's path-dependent state. See `paper_trade/data/diag_1month_parity.json`.

**v2 changes**: added (1) rigorous init-bar correctness proof, (2) explicit
checkpoint-event-semantics decision (idempotent consumers + dedup-on-restore),
(3) backwards-compat byte-identity as a hard requirement, not a test outcome,
(4) restart recovery path with determinism as a validation invariant.

## Problem
Current streaming wrapper recomputes the analyzer from scratch on every new 5m
bar by calling `StructureAnalyzer.analyze(df)` against the rolling buffer.
Once the 5m buffer caps at 7000 bars (~24 days) and starts evicting, each new
call sees a different `init_seed_sH/sL` (= max/min of buffer's first 50
bars). That different seed produces different early BOS detection, which
propagates: locked extremes diverge, ratchet flags diverge, downstream BOS
events fire in streaming that don't fire in batch. Verified: 663 streaming-
only events on the 1-month SOL window, 100% H1.

## Architecture change
Move the analyzer and engine from "process df, emit list" to "process one bar,
update state, emit events." State held on the instance, persistent across
calls. Streaming wrapper holds one analyzer + engine per pair for the bot's
entire lifetime. Buffer eviction becomes harmless — analyzer state is
independent of buffer contents.

```
NEW                                       OLD (current)
-------------------------------------     ------------------------------
analyzer.step(ts, high, low, close)       analyzer.analyze(df) -> list
  -> StructureState                         (re-runs from buffer start
   (state held on instance)                 every call; eviction breaks it)

engine.step(bar_5m)                       engine.run(df_5m, df_1h, ...)
  -> (rows_just_resolved, agg_delta)        (re-runs from buffer start
   (candidate + queue + 1h closes            every call; same problem)
    held on instance)
```

## API contracts

### StructureAnalyzer
```python
class StructureAnalyzer:
    def __init__(self, reversal_threshold_pct, init_bars,
                 log_failed_test=None): ...

    # NEW — one bar at a time, state on instance.
    def step(self, ts, high, low, close) -> StructureState: ...

    # NEW — checkpoint & restore.
    def serialize(self) -> dict: ...
    @classmethod
    def from_state(cls, d, cfg) -> "StructureAnalyzer": ...

    # BACKWARDS-COMPAT — wraps step() in a loop. Existing callers unchanged.
    def analyze(self, df) -> list[StructureState]: ...
```

During init (first `init_bars` calls in streaming mode), `step()`
updates a running max/min of seen highs/lows and uses that as
`locked_sH/sL`. After init completes, locked extremes evolve only via
BOS events + ratchet, identical to the current implementation. See
"Init-bar correctness proof" below for the full argument that BOS
events fired by the new step() are bit-identical to the old analyze()
during AND after init, on identical input data.

### SecondaryOnlyEngine
```python
class SecondaryOnlyEngine:
    def __init__(self, cfg, account_value=10_000.0, verbose=False,
                 event_callback=None): ...

    # NEW — one bar at a time. Returns rows for setups that resolved on
    # this bar; agg_delta is the counters that incremented this bar.
    def step(self, bar_5m) -> tuple[list[SecondaryRow], dict]: ...

    # NEW — feed 1h / btc 1h closes as they arrive. Engine maintains a
    # rolling 1h close array internally for the regime gate.
    def append_1h(self, bar_1h) -> None: ...
    def append_btc_1h(self, bar_1h) -> None: ...

    # NEW — checkpoint & restore (includes analyzer state).
    def serialize(self) -> dict: ...
    @classmethod
    def from_state(cls, d, cfg, account_value, verbose) -> "SecondaryOnlyEngine": ...

    # BACKWARDS-COMPAT — loops step() over df_5m, interleaving 1h via
    # append_1h at the right boundaries. Existing callers unchanged.
    def run(self, df_5m, df_1h=None, df_btc_1h=None,
            event_callback=None) -> tuple[list[SecondaryRow], dict]: ...
```

State held on instance: candidate dict, queued_bos idx, pending_sl_exit,
next_setup_id, agg, analyzer, 1h closes array, idx_1h running map, regime
gate caches. The pivot-tracker state currently inside `analyze()`'s loop
moves into the analyzer's instance attributes.

## Init-bar correctness proof

**Claim**: For all bars i ≥ init_bars on identical input, `step()`'s
internal state and emitted events are bit-identical to `analyze(df)`'s
loop at index i.

**Setup**:
- `analyze(df)` (current behavior): pre-computes `locked_sH = max(highs[0:init_bars])`
  and `locked_sL = min(lows[0:init_bars])` BEFORE the loop. Then the loop
  iterates i = 0..n-1 with these values fixed during init bars.
- `step()` in streaming mode: maintains a running max/min as bars arrive.
  At step k where k < init_bars: `locked_sH = max(highs[0:k+1])`,
  `locked_sL = min(lows[0:k+1])`. At step k = init_bars - 1: these reach
  `max(highs[0:init_bars])` and `min(lows[0:init_bars])` respectively (= the
  same final seed values that analyze() pre-computes). After step k =
  init_bars - 1, the running-max stops updating from incoming bars; locked
  extremes only update via BOS-event carryover or post-event ratchet, which
  is identical logic in both code paths.

**Lemma 1 (no BOS during init)**: For any i in [0, init_bars - 1], no
BOS_UP fires in either analyze() or step() on the same data.
- analyze(): At bar i, `bc[i] ≤ bh[i] ≤ max(highs[0:init_bars]) = locked_sH`.
  The BOS_UP trigger is `bc > locked_sH`. False. ✓
- step(): At bar i, `bc[i] ≤ bh[i] ≤ max(highs[0:i+1]) = locked_sH`.
  Same trigger, same conclusion. False. ✓
- Symmetric for BOS_DOWN: `bc[i] ≥ bl[i] ≥ min(lows[0:i+1]) = locked_sL`.
  Trigger is `bc < locked_sL`, false in both.

**Lemma 2 (no failed_test events during init)**: failed_test_high /
failed_test_low events are gated on `state in ("uptrend", "downtrend")`.
During init in both code paths, state remains "undetermined" (no BOS has
fired by Lemma 1). So failed_test events never fire during init in either
mode.

**Lemma 3 (pivot trackers reset at first post-init BOS)**: In both
code paths, when the first BOS fires (at some i ≥ init_bars), the per-swing
pivot trackers (highest_H, latest_H, lowest_L, latest_L) are unconditionally
reset to their sentinel values:
```python
highest_H_price = float("-inf"); highest_H_idx = -1
lowest_L_price  = float("inf");  lowest_L_idx  = -1
latest_H_price  = float("nan");  latest_H_idx = -1
latest_L_price  = float("nan");  latest_L_idx = -1
```
Whatever pivots may have been confirmed during init in either mode are
**discarded** at the first BOS. Pivot tracking accumulated during init
does not survive into post-init engine behavior.

**Lemma 4 (carryover at first BOS uses locked extremes, not trackers)**:
The first post-init BOS transitions state from "undetermined" to
"uptrend" or "downtrend". The carryover branch in this case is:
```python
else:  # undetermined
    state = "uptrend"
    new_locked_sL = locked_sL  # keep init seed value
```
i.e. the carryover for the first BOS uses the current `locked_sH/sL`,
not any pivot tracker. By the pre-condition (we're at i ≥ init_bars),
both code paths have `locked_sH = max(highs[0:init_bars])` and
`locked_sL = min(lows[0:init_bars])` (analyze: pre-computed; step:
final running max at end of init). Carryover values are identical.

**Conclusion**: At i = init_bars, both code paths have:
- Identical `locked_sH`, `locked_sL` (pre-set vs final running max — same value).
- State "undetermined" (no BOS has fired in either, by Lemma 1).
- Pivot trackers may differ in transient state (running pivot updates
  during init), but per Lemma 3 they reset at the next BOS; by Lemma 4
  the first BOS doesn't consult them anyway.

For all i ≥ init_bars: same input + same locked_sH/sL + same state +
identical engine logic ⇒ identical events emitted, identical state
evolution, identical locked extremes at every subsequent bar. ∎

**Edge case — data shorter than init_bars (n < init_bars)**: Both modes
finalize seed at `init_n = min(init_bars, n)`. analyze() does this
explicitly via pre-loop `min()`. step() in streaming mode would still
be in init at the end of available data. The `analyze(df)` wrapper
handles this by passing `init_n` explicitly to a `_finalize_seed_with_n`
hook on the analyzer instance, ensuring the wrapper output is byte-
identical to current behavior. In streaming mode this case can't occur
in practice — the bot is always seeded with at least BUFFER_5M bars
via REST gap-fill before WS streaming begins.

**Explicit assumption**: BOS detection is NOT gated on init completion
(matching analyze()'s current behavior — the loop runs BOS check for
all i, not just i ≥ init_bars). Lemma 1 proves it can't fire during
init regardless. The decision to NOT gate keeps the code paths
syntactically identical to analyze() and avoids special-casing.

## Checkpoint event semantics

**Decision: idempotent consumers + dedup-on-restore, NOT atomic event-
state commit.**

**Rationale**:
- Atomic event-state commit would require a transactional outbox for
  Telegram (since Telegram sends are not transactional with SQLite).
  That's ~200 LOC + an outbox-drain task + retry logic + telemetry on
  outbox depth. Significant complexity.
- All event consumers can be made idempotent at modest cost:
  * **SQLite `signals` / `setups` / `trades` / `level_proximity`**: composite
    primary key `(pair, setup_id)` (or `(pair, setup_id, event)` for setups);
    use `INSERT OR REPLACE`. Already idempotent in current code.
  * **Telegram**: extend the existing rate-limited notifier with a
    `telegram_seen_anchors` table (kind, anchor) — message dispatch
    checks this table and skips if already sent. Persistence ensures
    dedup survives restart.
  * **Phase 1b order router**: every Bybit request carries an
    idempotency key (`bybit_link_id` = `f"{pair}-{setup_id}-{purpose}"`).
    Bybit rejects duplicate creates with same link_id. Already in spec.
- The engine's `_seen_anchors` set (currently in StreamingEngine's
  per-instance dedup) is **rebuilt from SQLite** on startup by reading
  all (kind, anchor) tuples from `signals` + `setups` + `runtime_events`
  tables. This guarantees that on restart, any event already committed
  downstream is skipped on replay — even if the engine state checkpoint
  is older than the most recent emitted event.

**Concrete sequence on event emission**:
1. Engine's step() identifies a state transition (e.g. ARMED).
2. step() invokes `event_callback(event)`. The downstream sink
   (EventSink) writes to SQLite tables (idempotent), updates risk
   manager, queues Telegram (which checks `telegram_seen_anchors`).
3. step() returns. Caller (run.py main loop) calls
   `engine.serialize()` → SQLite engine_state row. ALSO idempotent
   (INSERT OR REPLACE).
4. If crash between steps 2 and 3: downstream has the event, state
   checkpoint is stale. On restart: rebuilt `_seen_anchors` includes
   the event (from SQLite); replay won't re-emit it.
5. If crash between steps 1 and 2 (less likely — sub-millisecond
   window): event is lost from downstream. State checkpoint is
   stale. On restart: rebuilt `_seen_anchors` does NOT include
   the event; replay re-emits it. Net effect: event is delivered
   (eventually-once, not exactly-once-but-at-least-once).

**Checkpoint cadence**:
- Every 60 seconds on heartbeat (covers idle periods).
- Immediately after `engine.step()` returns from a call that emitted
  any event (covers crash-after-event window in step 3 above).
- Both writes are `INSERT OR REPLACE` on `(pair, kind)` so duplicates
  are harmless.

## Backwards-compat byte-identity as a hard requirement

**Requirement, not a test outcome**: the `analyze(df)` and `run(df_5m,
df_1h, df_btc_1h)` wrappers MUST produce byte-identical output to the
current production code on:

- All trades CSV rows from a v1.4 backtest of full 18-month SOL+AVAX+LINK
  data: same column values, same row count, same row ordering, same
  outcome distribution, same r_realized to 6 decimal places.
- All engine_counters in the agg dict: every counter value matches
  exactly.
- All v1.4 OOS trades in particular (the user's prior ship-decision
  data point): SOL OOS netEV +0.171 / PF 2.00, AVAX OOS netEV +0.151 /
  PF 1.96, LINK OOS netEV +0.132 / PF 1.87 must reproduce exactly,
  with identical individual trade rows.

**This is a non-negotiable invariant**, not just an aspirational test
target. If validation test #1 fails, the refactor is wrong somewhere
and must be fixed before any other validation test is considered. No
"passes-on-small-window-then-fails-on-larger" repeats: the regression
diff is checked for byte-identity, full data, before declaring done.

**Reference data**: a copy of the current production trades CSVs for
SOL+AVAX+LINK 18mo is frozen at `backtesting/results/_pre_refactor_baseline/`
before any code change. Validation test #1 diffs new output against
these frozen baselines.

## Restart drill recovery path

**Sequence on bot startup (new or restart)**:

1. Read SQLite schema; init if missing (idempotent).
2. For each pair: hydrate bar buffers from SQLite `bars` table (most
   recent BUFFER_5M and BUFFER_1H bars).
3. REST gap-fill: for each (pair, tf), fetch closed bars between
   buffer's latest_ts and now. Append to buffer, persist to SQLite.
4. Restore engine state per pair:
   - Read `engine_state` row for (pair, "engine"). If missing, fresh
     init (first-run path).
   - `engine = SecondaryOnlyEngine.from_state(json_blob, cfg, ...)`.
     This restores: candidate dict, queued_bos, pending_sl_exit,
     next_setup_id, agg counters, analyzer state (with all
     pivot trackers, ratchet flags, locked extremes), 1h closes
     buffer, last_processed_5m_ts.
5. Rebuild `_seen_anchors` for the StreamingEngine wrapper by reading
   all (kind, anchor) tuples from `signals` + `setups` + `runtime_events`
   tables. Pre-populate the dedup set.
6. Replay: for each pair, find bars in the (now-current) buffer with
   `ts > engine.last_processed_5m_ts`. Feed them to `engine.step()`
   in chronological order, interleaving 1h via `engine.append_1h`
   when a new 1h ts is reached.
   - Events emitted during replay flow through the EventSink as
     usual; dedup catches anchors already in restored `_seen_anchors`.
7. Once replay reaches buffer's latest bar, normal streaming resumes.
   WebSocket message loop starts.
8. Send "bot online (restored from checkpoint)" Telegram. Log to
   runtime_events.

**Determinism is a validation invariant, not a test outcome**:
Replay producing identical engine state to a no-crash baseline at the
same bar must hold for any restart point. Validation test #4 exercises
this: kill bot at one specific bar, restart, compare end state and
event log to a no-restart run.

**Non-determinism risks** to guard against in the implementation:
- Floating-point summation order (e.g. `np.sum` on a re-loaded array
  with different memory layout). Use the same code path on both
  baseline and restart (no parallel reductions).
- Timestamp resolution. Use ns-resolution internally (`_to_ns_int64`
  helper already in the codebase) to avoid ms-vs-ns wrap.
- Python set iteration order. Where order matters, use `list` not `set`,
  or sort sets at use sites.
- Environment-dependent JSON serialization (`json.dumps` of a dict with
  non-string keys can be Python-version-specific). Always cast keys to
  str in `serialize()`; test on the exact Python version that runs prod.

## State persistence
- New SQLite table `engine_state` (per pair × kind: "analyzer" | "engine").
  Single TEXT column with JSON-serialized state. `INSERT OR REPLACE`.
  See "Checkpoint event semantics" above for cadence and atomicity rules.
- New SQLite table `telegram_seen_anchors` (kind, anchor) for Telegram
  dedup across restarts.

## Backwards compatibility
The `analyze(df)` and `run(df_5m, df_1h, df_btc_1h)` APIs remain. They wrap
the new `step()` in a loop, preserving identical output to the current
production behavior. All existing callers — `run_secondary_only.py`,
`run_hard_stop_sweep.py`, `run_entry_sweep.py`, `level_features.py`, the
daily validator, the existing parity test — continue to work unchanged.

## Validation gate (must pass all 5 before declaring complete)

Byte-identity per the "Backwards-compat byte-identity" section above is a
hard requirement. Tests below check that the requirement holds, not that
they are aspirational targets — failures are fixed before considering the
refactor complete.

1. **Backtest byte-identity**: `python -m backtesting.run_secondary_only
   --pairs SOL/USDT AVAX/USDT LINK/USDT --start 2024-11-04 --end 2026-05-04
   --regime-gate-pct 30 --variant-label refactor_c1` produces trades CSVs
   byte-identical to the frozen pre-refactor baselines at
   `backtesting/results/_pre_refactor_baseline/`. Diff via
   `cmp` (or python `pd.testing.assert_frame_equal` with
   `check_exact=True`). Must include all v1.4 OOS trade rows verbatim.
   Engine_counters in summary JSON must also match exactly.

2. **Multi-window parity**: 12-day (`2026-04-22` → `2026-05-04`),
   1-month (`2026-04-01` → `2026-05-04`), and 18-month (`2024-11-04`
   → `2026-05-04`) SOL parity all pass byte-identical (event lists equal
   as ordered lists, anchor-keyed). The 1-month and 18-month windows are
   the ones that the previous slice-and-rebatch architecture failed —
   both must now pass.

3. **All-pair parity**: SOL, AVAX, LINK 18-month parity all pass byte-
   identical. Each runs independently per the parity test runbook.

4. **Restart drill** (with replay determinism):
   - Run baseline: streaming engine processes 1-month SOL window
     uninterrupted; capture event log + final engine state JSON.
   - Run drill: identical streaming run, kill the process via SIGKILL
     at bar `2026-04-15 12:00 UTC` (~half-way), restart, allow replay
     from checkpoint to catch up to the same final bar.
   - Drill's event log must be byte-identical to baseline's event log
     (anchor-keyed; replay must NOT re-emit events that fired before
     the kill).
   - Drill's final engine state JSON must be byte-identical to
     baseline's (locked_sH/sL, candidate dict, pivot trackers,
     analyzer state, all match).
   - **Determinism is the asserted invariant**: identical input bars
     in identical order must produce identical state regardless of
     whether processing was interrupted by a restart.

5. **Reconnect-simulation parity**: existing
   `test_streaming_parity_reconnect.py` continues to pass under the
   new architecture (SOL 12-day with simulated 30-min and 5-hour
   disconnects, byte-identical event lists). Verify; should be
   trivially true under persistent state but not assumed.

## Estimated effort
1-2 days. Main risks: (a) seed-init edge case in step() vs analyze(); (b)
checkpoint restore needs to be deterministic across Python versions/envs.
Both addressed by validation tests #1 and #4.

## What's NOT changing
- Strategy logic. v1.4 rules unchanged.
- Engine event semantics. Event kinds, anchors, dedup — same.
- The reconnect gap-fill in `binance_ws.py` + `run.py` — independently correct.
- Phase 1b spec v2 — independently correct, doesn't depend on the engine's
  internal API.
- The level-features pipeline, hard-stop sweep, entry sweep, daily validator —
  all consume the existing batch APIs that wrap `step()`.
