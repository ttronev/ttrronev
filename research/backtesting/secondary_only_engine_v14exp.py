"""Experimental v1.4 engine — parametric secondary entry fib level.

This module is ISOLATED from the production engine. It subclasses
`SecondaryOnlyEngine` and overrides only `_compute_fibs` to set the
entry price from `cfg["fib_secondary_entry"]` (control = 0.5).

Production engine and runners are untouched. Egor's parity test runs
against the production engine; this experimental engine is invoked only
by `run_entry_sweep.py`.

Behavior of this engine when `cfg["fib_secondary_entry"] == 0.5`:
  identical to the production engine (control case).

Tested entry levels per the v1.5 entry sweep spec:
  0.382 — deeper pullback; smaller stop dist; higher RR; harder to fill
  0.5   — control (production v1.4 behavior)
  0.618 — shallower pullback; larger stop dist; lower RR; easier to fill

All other rules unchanged: SL still close beyond fib_0.3, TP at fib_1.0,
arming at fib_0.75, primary trigger (structural filter) at fib_1.0,
invalidation at fib_0, secondary cancel-pre-fill on close beyond fib_1.2.
"""

from __future__ import annotations

from typing import Optional

from research.backtesting.secondary_only_engine import SecondaryOnlyEngine, SecondaryRow

__all__ = ["SecondaryOnlyEngineExp", "SecondaryRow"]


class SecondaryOnlyEngineExp(SecondaryOnlyEngine):
    """Drop-in replacement that honors `cfg['fib_secondary_entry']` for
    the secondary entry price. Defaults to 0.5 (= production behavior)
    when the cfg key is absent or set to 0.5."""

    def _compute_fibs(self, c: dict, cfg: dict) -> None:
        # Let the base class set fib_0..1.2, entry/sl/tp at default
        # (entry=fib_0.5), r_planned, and hard_stop_price.
        super()._compute_fibs(c, cfg)

        # If the configured entry is 0.5 we're done (= control). Skip
        # the override so behavior is byte-identical to the parent.
        f_e = float(cfg.get("fib_secondary_entry", 0.5))
        if abs(f_e - 0.5) < 1e-9:
            return

        sH = c["swing_high"]
        sL = c["swing_low"]
        rng = sH - sL
        if c["bos_direction"] == "bearish":
            new_entry = float(sL + f_e * rng)
        else:
            new_entry = float(sH - f_e * rng)
        c["entry_price"] = new_entry

        # SL price is unchanged — still fib_0.3 from base class. TP also
        # unchanged at fib_1.0. Only the planned R and (if active) the
        # hard-stop wick price need recomputation against the new entry.
        risk = abs(c["entry_price"] - c["sl_price"])
        reward = abs(c["tp_price"] - c["entry_price"])
        c["r_planned"] = round(reward / risk, 3) if risk > 0 else 0.0

        r_h = float(getattr(self, "_hard_stop_R", 0.0))
        if r_h > 0 and risk > 0:
            if c["bos_direction"] == "bearish":
                c["hard_stop_price"] = c["entry_price"] - r_h * risk
            else:
                c["hard_stop_price"] = c["entry_price"] + r_h * risk
        elif r_h <= 0:
            c["hard_stop_price"] = None

        # f_e tagged on the candidate so trade rows are auditable
        # (which entry level produced this fill?).
        c["fib_entry_used"] = f_e
