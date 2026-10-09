"""
shared/pricefmt.py — price rounding and display that work at ANY price scale.

Memory hygiene: see docs/runbooks/memory_hygiene.md (pure functions, no I/O).

Why this exists: the stack was calibrated on SOL (~$100) and rounded every
published price to 6 decimals and printed 2. That is fine for SOL and BTC and
silently destroys sub-cent assets. SHIB trades near 0.000005: six decimals
leaves ONE significant digit, so every band and level collapsed onto 0.000005
or 0.000006, and alert text printed "$0.00".

round_price   at or above LEGACY_FLOOR ($0.01): exactly round(x, 6), so every
              existing artifact at those prices stays byte-identical.
              Below it: SUB_CENT_SIG_DIGITS significant digits. Stored level
              prices feed the 0.3% touch band, so the rounding error must be
              negligible against it — 8 digits is ~5e-8 relative. (5 digits
              was tried first: its 0.005% error flipped a borderline touch
              event in the scale-invariance test.)
fmt_price     display string; mirrors fmtPrice() in service/static/app.js
              (keep the two in step).
"""
from __future__ import annotations
import math

LEGACY_DECIMALS = 6        # what every artifact used before
LEGACY_FLOOR = 0.01        # at/above this price, behave exactly as before
SUB_CENT_SIG_DIGITS = 8    # significant digits kept below LEGACY_FLOOR
MAX_DECIMALS = 18


def price_decimals(x) -> int:
    """Decimals to keep when ROUNDING a price for storage."""
    try:
        ax = abs(float(x))
    except (TypeError, ValueError):
        return LEGACY_DECIMALS
    if not math.isfinite(ax) or ax == 0.0 or ax >= LEGACY_FLOOR:
        return LEGACY_DECIMALS
    need = SUB_CENT_SIG_DIGITS - 1 - math.floor(math.log10(ax))
    return max(LEGACY_DECIMALS, min(MAX_DECIMALS, need))


def round_price(x):
    """Round a price for storage. None passes through. Identical to
    round(x, 6) for |x| >= 0.01."""
    if x is None:
        return None
    return round(float(x), price_decimals(x))


def display_decimals(x) -> int:
    """Decimals to SHOW (alerts, reports, dashboard)."""
    ax = abs(float(x))
    if ax >= 1000:
        return 1
    if ax >= 10:
        return 2
    if ax >= 0.1:
        return 4
    if ax == 0.0 or not math.isfinite(ax):
        return 2
    # Below $0.10: at least 6 decimals (legacy look), more when needed to
    # keep 4 significant digits (SHIB: 0.000005158).
    return max(6, min(14, 3 - math.floor(math.log10(ax))))


def fmt_price(x, symbol: str = "$") -> str:
    """'$96.98', '$78,419.9', '$0.6512', '$0.091200', '$0.000005158'."""
    if x is None:
        return "n/a"
    try:
        xf = float(x)
    except (TypeError, ValueError):
        return "n/a"
    if not math.isfinite(xf):
        return "n/a"
    return f"{symbol}{xf:,.{display_decimals(xf)}f}"


def price_key(x) -> str:
    """Stable string form of a price for dedup keys / identities. Equal to
    str(round(x, 6)) for |x| >= 0.01, so keys written before this module
    existed keep matching."""
    return str(round_price(x))


__all__ = ["price_decimals", "round_price", "display_decimals", "fmt_price",
           "price_key", "LEGACY_DECIMALS", "LEGACY_FLOOR", "SUB_CENT_SIG_DIGITS"]
