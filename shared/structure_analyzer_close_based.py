"""
shared/structure_analyzer_close_based.py — CLOSE-BASED variant of the BOS
engine. DERIVED module (Option B). Sibling to shared/structure_analyzer.py.

Why this exists
---------------
The range-detector cascade needs the BOS engine to ignore liquidation
wicks. The original analyzer sets locked extremes and pivots from bar
high/low, so a single wick (e.g. SOL 1D 2026-02-06 low $67.31) becomes
locked_sL and blocks every later bos_down in the consolidation because
price never closes below $67 again -> no BOS -> no range.

The fix: identify pivots and set locked extremes from CLOSES, not
high/low. The lowest close in a downtrend swing becomes locked_sL; the
highest close in an uptrend swing becomes locked_sH. No wick threshold,
no "accepted close" rule, no calibration — closes are closes. For SOL
1D Feb-May 2026 this puts locked_sL at ~$78 (lowest close), so a later
close below $78 fires a clean bos_down.

THE ONE RULE DIFFERENCE vs the original
---------------------------------------
Everywhere the original used bar.high / bar.low for pivot detection,
locked-extreme setting, zigzag active extremes, tracking, and the
init seed, this variant uses bar.close. BOS *trigger* logic was already
close-based in the original and is unchanged.

Implementation: instead of transcribing the 200-line step() (transcription
risk + drift risk), we subclass the original and feed `close` in place of
`high` and `low`. With bh == bl == bc == close, every high/low reference
in the inherited step() collapses to close, and the wick-only failed_test
branch (`close > X and close <= X`) goes permanently dead. This is a
faithful close-based analyzer that CANNOT drift from the original logic —
it inherits it. (Periodic check: if the base step() signature or seed
mechanism changes, re-audit analyze() below.)

Regression safety: v1.4 (secondary_only_engine), bos_retest, and the
parity tests import the ORIGINAL shared/structure_analyzer.py and are
unaffected by this file. Only the range-detector core imports this one.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from shared.structure_analyzer import (
    StructureAnalyzer as _BaseStructureAnalyzer,
    StructureState,
    FailedTestLogger,
    to_engine_arrays,
    DEFAULT_REVERSAL_PCT_BY_TF,
    DEFAULT_INIT_BARS_BY_TF,
)


class CloseBasedStructureAnalyzer(_BaseStructureAnalyzer):
    """BOS engine that detects structure from CLOSES, not wicks.

    Identical to the base analyzer except high/low are replaced by close
    in every pivot/extreme/seed computation. See module docstring."""

    def step(self, ts, high, low, close) -> StructureState:
        # Collapse high/low to close so the inherited step() uses closes
        # everywhere. high/low are intentionally ignored.
        c = float(close)
        return super().step(ts, c, c, c)

    def analyze(self, df: pd.DataFrame) -> list[StructureState]:
        """Batch path. Pre-seeds locked extremes from the first init_bars
        CLOSES (not high/low), then loops the close-based step()."""
        n = len(df)
        if n == 0:
            return []

        self._reset_state()

        close = df["close"].to_numpy(dtype=float)
        ts = df["timestamp"].to_numpy() if "timestamp" in df.columns else np.arange(n)

        init_n = min(self.init_bars, n)
        self._set_seed(
            locked_sH=float(np.max(close[:init_n])),
            locked_sH_idx=int(np.argmax(close[:init_n])),
            locked_sL=float(np.min(close[:init_n])),
            locked_sL_idx=int(np.argmin(close[:init_n])),
        )

        states: list[StructureState] = [None] * n  # type: ignore
        for i in range(n):
            # step() collapses to close internally; pass close for all.
            states[i] = self.step(ts[i], close[i], close[i], close[i])
        return states


# Drop-in alias so consumers can `from ... import StructureAnalyzer`.
StructureAnalyzer = CloseBasedStructureAnalyzer

__all__ = [
    "CloseBasedStructureAnalyzer",
    "StructureAnalyzer",
    "StructureState",
    "FailedTestLogger",
    "to_engine_arrays",
    "DEFAULT_REVERSAL_PCT_BY_TF",
    "DEFAULT_INIT_BARS_BY_TF",
]
