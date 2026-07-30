# Range Detector — Future Work

Parked items (do not build until explicitly scheduled). Recorded during
1D layer-1 calibration.

## 1. Volatility-adaptive `recovery_lookahead`
The fragmentation/early-end behavior is volatility-dependent: high-vol
periods (e.g. SOL May-Sep 2024, ~3.4% avg daily move) produce ranges
~2.6x shorter than low-vol periods (~2.7%), because excursions out of a
band scale with volatility. A *fixed* `recovery_lookahead` can't fit both
regimes — we settled on 18 (1D) as a worst-case-covering compromise.

Cleanest long-term fix: scale `recovery_lookahead` (and possibly
`band_zone_pct`) by trailing realized volatility, so high-vol stretches
automatically get a longer hold window and low-vol stretches a tighter
one. Design questions to settle: vol window length, scaling function
(linear? clamped?), per-TF baselines. This is a design project, not a
parameter tweak — hence parked.

## 2. Layer 5 — range memory (persistent S/R from broken ranges)
A broken range leaves behind persistent support/resistance levels that
get retested later. Concept surfaced during 1D panel review (Panel 3
Apr-Oct 2025 shows multi-tier support structure across time). Build only
after layers 2-4 exist. Will likely need: classification of broken-range
edges into strong/weak S/R, decay/retest tracking, and cross-referencing
with the active-range bands.

## 3. Cascade-driven band refinement (post layer 5)
Use lower-TF close clusters to refine higher-TF range bands:
* The 1D range gives the structural zone (wide, full price-acceptance area).
* 4H closes *inside* that zone define a refined sub-band (tighter, where
  price actually consolidates).
* 1H refines further; 15m/5m/1m for entry-level precision.
* The 1D band stays the "structural ceiling"; the 4H sub-band is the
  "trading ceiling." Gives both wide-zone accuracy ("are we in a range?")
  and tight-band precision (order placement).

Implementation note: requires the cascade nesting utility first
(`parent_range_id` linking each 4H range to its 1D parent). The schema
fields are already reserved on every range record.

## Build order reminder
1W detector (next TF) -> layers 2-4 in order -> then layer 5 + the
vol-adaptive recovery_lookahead above.
