"""
strategies/bos_retest_v1.py — 1H BOS retest continuation, research-mode.

Setup
-----
* 1H bullish BOS in already-bullish 1H bias (EMA12 > EMA21), OR
* 1H bearish BOS in already-bearish 1H bias (EMA12 < EMA21).
* "Already-bullish/bearish" = bias[i] aligned AND bias[i-1] also aligned;
  the BOS bar is NOT allowed to be the bias-flip bar.

Retest
------
* Bearish setup retest level = the level broken by the BOS = analyzer's
  `last_bos_level` on the bos_down event (a structural low). Mirror for
  bullish (structural high).
* Entry zone is a rectangle around the retest level:
    Bearish:  zone = [retest_level, retest_level * (1 + buffer)]
    Bullish:  zone = [retest_level * (1 - buffer), retest_level]
* Entry rule (sweep): zone_far (retest itself), zone_mid, zone_near
  (the buffered side). "Far" = farthest from current price = price must
  travel deepest to fill.

Min swing filter
----------------
* size of the BOS leg = |pre_bos_swing_extreme - bos_close| / bos_close.
* pre_bos_swing_extreme = post-BOS locked_sH (for bos_down) or locked_sL
  (for bos_up) — the analyzer captures this on the BOS bar.

SL
--
* Tight:  bearish -> max bar high over [BOS+1, fill_bar];
          bullish -> min bar low  over [BOS+1, fill_bar].
* Wide:   bearish -> pre_bos_swing_high; bullish -> pre_bos_swing_low.

TP: fixed R-multiple at {1, 1.5, 2, 3, 5} R from entry.

Invalidation (baseline, after 2026-05-21 review)
------------------------------------------------
* Pre-fill: 1H close back through retest level (close > retest for bear,
  close < retest for bull) cancels the pending limit.
* Time-based: no fill within `bars_to_timeout` bars after BOS cancels.
* Continuation-cancel: OFF by default. Each legitimate structural BOS
  (per the ratcheted-locked-extreme rule in shared/structure_analyzer.py)
  fires its own independent setup. Same-direction BOSes do NOT cancel
  each other's pending limits. The toggle `use_continuation_cancel` is
  retained on `VariantParams` for optional A/B testing later.

Risk: 1% of account per trade. Fees: 0.055% per side (Bybit/OKX taker).

Multiple concurrent setups allowed. R-unit EV/WR/PF are per-trade-
independent statistics; gating "one trade at a time" silently drops
real setups (e.g. when a long-running 5R-target short holds open for
months while subsequent BOSes fire).

Sweep grid (1920 variants)
--------------------------
    3 entry rules x 4 buffers x 2 SL placements x 5 TP RR x 4 min-swings
    x 4 timeouts.
"""
from __future__ import annotations


CONFIG: dict = {
    "strategy_id":   "bos_retest_v1",
    "strategy_name": "1H BOS retest continuation (research)",

    "pair": "SOL/USDT",
    "exec_tf":  "1h",
    "trend_tf": "1h",

    # Analyzer config for 1H BOS detection. 0.0075 is chosen so the
    # May 12-13 2026 swing low at 93.55 becomes the broken level on
    # the May 13 12:00 bos_down (user-shown validation trade).
    "reversal_pct_1h": 0.0075,
    "init_bars":       20,

    # 1H bias EMAs.
    "htf_ema_fast": 12,
    "htf_ema_slow": 21,

    # Risk + fees.
    "risk_pct":  0.01,
    "taker_fee": 0.00055,

    # Sweep grid -- 3*4*2*5*4*4 = 1920 variants.
    "sweep_entry_rules":   ("zone_far", "zone_mid", "zone_near"),
    "sweep_buffers":       (0.000, 0.002, 0.005, 0.010),
    "sweep_sl_placements": ("tight", "wide"),
    "sweep_tp_rr":         (1.0, 1.5, 2.0, 3.0, 5.0),
    "sweep_min_swings":    (0.000, 0.020, 0.030, 0.050),
    # Time-based invalidation: cancel pending limit if not filled within
    # this many bars after BOS. Added as a sweep dimension (was scalar):
    # the textbook 2026-05-13 12:00 short -> 2026-05-14 16:00 retest is
    # 28h late, just outside the 24-bar window. {48, 72, 96} test whether
    # giving the setup more patience preserves the structural premise or
    # just lets stale BOSes fill into adverse moves.
    "sweep_timeouts":      (24, 48, 72, 96),

    # Account size (notional only -- all reported stats are in R units
    # so this only affects display dollars).
    "account":   10_000.0,
}


__all__ = ["CONFIG"]
