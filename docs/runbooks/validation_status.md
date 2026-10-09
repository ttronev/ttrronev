# validation_status.md — what has been validated, and how

Records the trustworthiness of the detection + signal foundation, so future
work isn't re-litigated. Updated 2026-06-02.

## Detector forward-only contract — VALIDATED ✅

The multi-TF range detector (`range_detector_core.py` + `range_detector_{tf}.py`)
is **forward-only except for one bounded, intentional window**: the
`recovery_lookahead` false-break tolerance. This was verified by replaying the
detector candle-by-candle (`detectors/replay_validate.py`) and auditing each
confirmed range's first-known vs batch timestamps.

**Audit results (smoke slice + diagnosis, 2026-06-02):**
- **Retracted confirms: 0** — no range is confirmed in a forward run and later
  dropped by batch (no non-monotonic confirms).
- **Backdated confirms: 0** — batch never "knows" a confirm before its bar.
- **Confirm lag:** 0 to `recovery_lookahead` bars — caused only by the recovery
  window (a pre-confirm forming-break's recovery resolving after the confirm
  bar). Bounded, intentional, not hidden hindsight.
- **End-known lag:** the structural `range_end_ts` is knowable only at
  `end + recovery_lookahead` (the full window must pass to rule out a recovery).
- **Band drift:** an ACTIVE range's bands evolve as new closes arrive — expected,
  not a leak. Layer-5 LEVELS are frozen at range end, so this never reaches
  Layer 4.
- **retracted_end_count:** a premature end declared at the data edge (empty
  recovery window) can later withdraw if a recovery prints within
  `recovery_lookahead`. Expected on AMBIGUOUS breaks; Layer 4 is insulated
  because it uses `level_available_ts` (= `end + recovery_lookahead`), never the
  premature/structural end. Validate on the ambiguous slice 1D R024
  (`failed_break@2024-07-15` recovered at 18/18 bars).

**Bug found + fixed via this harness:** an infinite-loop in `detect_ranges`
(unbounded `RangeRecord` append when a prefix window ended on a BOS bar) that
exhausted RAM. Fixed at root; byte-identical on full data (all confirmed counts
+ R038/R202 bands + bos_retest +2.4581R preserved).

**Conclusion:** the forward-only contract holds. The `recovery_lookahead` window
is the ONLY look-ahead, it is bounded and documented (see the FORWARD-LOOK
CONTRACT section in `range_detector_core.py`), and consumers close it via
`level_available_ts`. **Strategy work is built on a trustworthy foundation.**

## Layer 4 signal framework — honest baseline LOCKED

Point-in-time, leak-closed (`level_available_ts` gating): retest-fail
~-0.087R overall (thin-to-negative); range-to-range ~+0.045R overall, only 4H
+0.250R (N=195) positive but outlier-dependent + regime-decayed. Earlier "wow"
runs were 50-97% recovery_lookahead leak, now closed. **Calibration anchor for
future setups: > +0.5R avgR on PIT 4H/2H with N >= 100, robustness-checked.**
Details in the `layer4-research-findings` memory.

## Memory hygiene — VALIDATED ✅

All active scripts self-report peak RSS and exit clean (see `memory_hygiene.md`).
User-verified: query_state 89MB, compute_known_at 107MB, range_detector_1d 135MB;
`python.exe` releases within 5s of prompt return.

## How to re-validate (commands are the user's to run)

```powershell
# Forward-only replay audits (per memory_hygiene.md: user runs replay passes)
.venv\Scripts\python.exe -m detectors.replay_validate --tf 1w 1d                 # full per-bar
.venv\Scripts\python.exe -m detectors.replay_validate --tf 1d --mode full --start 2024-04-01 --end 2024-11-30   # ambiguous-break (R024)
.venv\Scripts\python.exe -m detectors.replay_validate --tf 4h --mode anchored --start 2025-06-01 --end 2026-06-01
```

## Known paths

Three frozen scripts in `detectors/` — `layer4_signals_range_to_range.py`,
`layer4_signals_retest_fail.py` and `replay_validate.py` — write their output
under `backtesting/results/` at the repository root, the pre-R0 location. They
are owner-run research passes and the protected core is not edited for a path,
so the root path stays gitignored (and excluded from the image) until the
detector is next changed through the A3 gate. Move their output into
`research/backtesting/results/` by hand.
