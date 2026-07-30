# Strategy Log

A running log of every strategy ttrronev has *attempted*, including the ones
that failed. Failures are as important to keep as successes — they prevent
re-trying the same dead idea later.

Newest entries on top.

---

## DEFAULT STRUCTURAL READING  (foundational rules — every strategy inherits)

The engine's structural chain (in `shared/swing_detector.py`) is a
**threshold-reversal zigzag**, not a centered-pivot detector. It tracks
a "pending" high (or low) as the running extreme since the last pivot,
and **locks it as a chain pivot** the moment price reverses by
`min_move_pct%` from it.

The engine reads structure correctly when **all three** of these defaults
are in place. Each was added to fix a concrete miscount on real bars.

### Rule 1 — `swing_thresholds[5m] = 1.0`  (set 2026-05-01)

The original SOL-winner config used `0.8`, but at 0.8% any 5m bounce of
0.82% qualifies as a structural pivot — the chain fills up with micro-swings
that no trader would annotate.

Validation: SOL 2026-04-01 evening into 2026-04-02.
  - At 0.8%: chain has LH @ 21:25 (83.15) **plus** noise micro-LHs at
    21:55 (81.95) and 22:25 (81.46). The engine anchored on the 22:25
    micro-swing → 0.73-point fib geometry, doesn't match the chart.
  - At 1.0%: chain has only the structural pivots a trader reads —
    LH @ 21:25, LL @ 22:30, LL @ 01:15 (the real break of structure).

### Rule 2 — `bos_lh_anchor_n = 1`  (default — but only correct in a clean chain)

Anchors the post-BOS swing reference to the **latest** LH (longs) / HL
(shorts). This is correct **only when the chain itself is clean** (Rule 1).
Under a noisy chain the latest LH is a micro-swing; under a clean chain
it's the structural pivot.

### Rule 3 — `bos_close_confirmation = True`  (set 2026-05-01)

A new structural HH requires a **CLOSE above the prior chain HH's wick**.
Symmetric for LL: a new LL requires a CLOSE below the prior chain LL's
wick. Wick-only piercings (intrabar spikes that close back inside) are
demoted to LH/HL.

Validation: SOL 2026-04-16 to 2026-04-17.
  - HH @ Apr 16 20:00 = 90.45 (legitimate structural HH).
  - Apr 17 13:25 wicked to 90.49 (4 cents above 90.45) but closed at 89.54.
  - Apr 17 14:40 wicked to 90.72 but no candle in the entire 14:30–17:00
    window closed above 90.45.
  - Without rule 3: engine registered HHs at 13:25 and 14:40.
  - With rule 3: both are correctly demoted to LH; the 90.45 HH stands.

Implemented in `shared/swing_detector.py:_close_confirmed_high_kind` and
`_close_confirmed_low_kind`. Pivot PRICES remain wick-based (so SL/TP
geometry can use the wick); only the pivot LABEL is gated by the close.

### How to opt out (for replays)

To replay the original published SOL-winner reference numbers (326 trades /
54.29% WR / +1.18R EV), set in CONFIG:
  - `swing_thresholds[5m] = 0.8`
  - `bos_lh_anchor_n = 1`
  - `bos_close_confirmation = False`

Train/test numbers under the new defaults will look different. That's
expected — and correct.

---

## Template

```
### YYYY-MM-DD — <strategy_name> — <stage>

- **Hypothesis:** one line
- **Result:** key numbers (Sharpe, DD, win rate)
- **Decision:** promote / iterate / shelve
- **Reason:** one sentence
- **Doc:** strategies/strategy_<name>.md
```

---

## Active Strategy Roster

Seven strategies are scaffolded in code (`strategies/*.py`). Only
`BOS1hSimple` has been backtested so far (BTC/USDT only — see notes inline).
All other entries are at the **scaffolded** stage, awaiting a backtest run
on the 8-pair universe.

### 2026-04-30 — SMCMultiTimeframe — scaffolded

- **File:** [smc_mtf_short.py](smc_mtf_short.py)
- **Codename:** Project Abyss — Range High 0.75 Reversal
- **Direction:** **short-only**
- **Timeframe:** 15m signal frame; auxiliary 1m and 4h CSVs auto-loaded for
  the same pair, plus `BTC_USDT_4h.csv` for non-BTC correlation
- **Universe:** all 8 approved pairs
- **Hypothesis:** When (a) the 4h is in a defined downtrend (price < 21EMA,
  three lower highs, RSI < 50, plus either a 3-drives-up volume divergence
  or BTC also leaning bearish), (b) the 15m is in the upper 25% of its
  recent 50-bar range, (c) RSI divergence + lower-high structure +
  liquidity sweep all line up, and (d) a 1m MSB-and-retest fires inside
  the London or NY session — the resulting short has a high enough hit
  rate to clear a strict 1.5R minimum-RR gate.

#### Daily session levels (recomputed at 00:00 UTC)
- Asia Low (20:00→00:00 UTC), Tokyo/Daily Open (00:00 UTC), London Open
  (07:00 UTC), NY Open (13:30 UTC)
- PDH / PDL — previous calendar day's high / low
- Monday High — most recent Monday's daily high
- Week High — running max of 4h highs since most recent Monday 00:00 UTC

#### Range geometry (15m)
- Range High = rolling 50-bar high, Range Low = rolling 50-bar low
- Range must be ≥ 1% wide (else skip)
- Premium zone = `[range_low + 0.75 × width, range_high]`
- Discount zone = `[range_low, range_low + 0.25 × width]`

#### HTF bias (4h, plus optional confluence)
Primary bear gate (all required):
1. close < EMA(21)
2. last 3 4h candles each below the previous high (consecutive LHs)
3. RSI(14) < 50

Confluence (one required for non-BTC pairs):
- 3-drives-up volume divergence — three consecutive 4h swing highs each
  with strictly lower volume; latches True until close > the 3rd peak
  invalidates it
- BTC bearish — BTC 4h in three-LH structure AND `close < weekly_high × 0.99`

#### 15m setup
- Bearish RSI divergence within last 10 bars (compare consecutive 15m
  swing highs in a 20-bar window: price higher, RSI lower)
- ≥ 2 LHs within the last 30 15m bars
- Liquidity sweep of Range High **or** PDH within last 5 bars
  (`high > level AND close < level`)

#### 1m MSB + retest
- Find most recent 1m swing low (lookback 3 each side) with age ≤ 30 bars
- 1m close below that level = MSB
- Retest within next 10 1m bars: a bar whose `high` is within 0.15% of the
  broken level **and** whose `close` is back below it
- Entry triggers on the 15m bar that contains the retest's 1m close

#### Entry / exit
- **Session window:** London 07:00–12:00 UTC or NY 13:30–20:00 UTC only
- **Stop-loss:** `max(last LH high, range high, PDH) × 1.001`. Skip if
  stop-distance > 2% of entry.
- **Take-profit:** if `PDL > range_low`, weighted `(PDL + range_low) / 2`;
  else `range_low`. Skip if implied RR < 1.5.
- **Invalidation exit:** 15m close above the most recent LH closes any
  open short at that bar's close. (Adapter logs `exit_reason="signal_exit"`;
  the strategy column name distinguishes it from indicator-exit strategies.)
- **Risk:** 1% of equity per trade.
- **Min candles required:** 500.

#### Operational notes
- Auxiliary CSVs are loaded by the strategy itself based on `self._pair`
  (set by the engine adapter). Missing files write a warning to
  `logs/errors/smc_mtf.jsonl` and skip the pair without crashing.
- Strategy is **short-only**: never emits `signal=+1` and never opens longs.

- **Stage:** scaffolded — pending backtest. No backtest results yet.

### 2026-04-30 — BOS1hSimple — scaffolded

- **File:** [bos_1h_simple.py](bos_1h_simple.py)
- **Timeframe:** 1h
- **Universe:** all 8 approved pairs
- **Hypothesis:** A clean 1h close beyond a recent confirmed structural
  swing — with a strong-bodied candle and a tight stop — produces a
  short-lived edge that's enough to capture 1.5R before mean reversion
  sets in. Pure price action; no indicators are used to gate entries.
- **Indicators:** none. Pure price action.
- **Entry:**
  - LONG  — close > most recent confirmed swing high (10 candles each side)
  - SHORT — close < most recent confirmed swing low
  - Swing being broken must be at most 50 candles old.
- **Filters:**
  - SL distance ≤ 3% of entry (rejects wide setups)
  - BOS-candle body ≥ 30% of total range (rejects indecision candles)
- **Stop:** lowest low (long) / highest high (short) of the 3 candles
  immediately preceding the BOS candle, including wicks.
- **Exit:** fixed 1.5R take-profit; no break-even, no indicator exit.
- **Concurrency:** the engine adapter naturally enforces "1 trade per pair
  at a time" (no new entry while a position is open).
- **Risk:** 1% of equity per trade.
- **Min candles required:** 30.
- **Stage:** scaffolded — pending backtest.

### 2026-04-30 — EMACrossExit — scaffolded

- **File:** [ema_cross_exit.py](ema_cross_exit.py)
- **Timeframe:** 15m
- **Universe:** all 8 approved pairs
- **Hypothesis:** A clean break of the recent 20-bar high / low that holds
  typically runs further than a 1.5R scalp can capture. The cleanest exit
  is an EMA12/21 cross paired with a candle that fails to extend in the
  trade's direction (loss of momentum, structurally confirmed). Without
  that confirmation, ride the move out to a wide 5R fallback.
- **Indicators:** EMA(12), EMA(21), EMA(50). No volume, no Bollinger Bands.
  EMA50 is computed but not used as a signal filter in v1 — left in place
  for later iterations.
- **Entry:**
  - LONG  — close > 20-bar rolling high (excluding the current bar)
  - SHORT — close < 20-bar rolling low (excluding the current bar)
- **Stop:** lowest low (long) / highest high (short) of the 3 candles
  immediately preceding the BOS candle, including wicks.
- **Exit (primary, indicator):** direction-aware, on the *same* bar:
  - LONG closes when EMA12 crosses below EMA21 **and** `high ≤ prev high`
  - SHORT closes when EMA12 crosses above EMA21 **and** `low ≥ prev low`
  Adapter logs as `exit_reason="signal_exit"`.
- **Exit (fallback):** fixed 5R take-profit if the indicator exit never fires.
- **Risk:** 1% of equity per trade (uniform across all strategies).
- **Min candles required:** 220 (200 EMA-warmup buffer + 20 swing window).
- **Stage:** scaffolded — pending backtest.

### 2026-04-29 — VolumeProfileFade — scaffolded

- **File:** [volume_profile_fade.py](volume_profile_fade.py)
- **Timeframe:** 1h
- **Universe:** all 8 approved pairs
- **Hypothesis:** Excursions outside a 14-day volume profile's value area
  that fail and revert *back* into the value area are mean-reverting
  inefficiencies; fade them toward the opposite VA edge.
- **Indicators:** rolling 14-day (336 candle) volume profile, 100 buckets,
  70% value area; recomputed every 168 candles.
- **Entry:**
  - SHORT — previous bar was above VAH **and** current close re-enters below VAH
  - LONG  — previous bar was below VAL **and** current close re-enters above VAL
- **Exit:** SHORT TP = VAL, LONG TP = VAH (target the *opposite* edge, not POC).
- **Stop:** highest high (shorts) or lowest low (longs) of the entire
  out-of-VA streak that just failed.
- **Risk:** 1% of equity per trade.
- **Min candles required:** 336.
- **Stage:** scaffolded — pending backtest.

### 2026-04-29 — BOSScalp — scaffolded

- **File:** [bos_scalp.py](bos_scalp.py)
- **Timeframe:** 5m
- **Universe:** all 8 approved pairs
- **Hypothesis:** A clean close beyond a recent confirmed swing point produces
  short-lived momentum; a tight stop under the interim swing and a fixed 1.5R
  target captures it before mean-reversion sets in.
- **Indicators:** swing highs/lows on a 5-on-each-side window; 3-candle
  interim extreme for stop placement.
- **Entry:** close beyond the most recent confirmed swing high (long) or
  swing low (short), where the swing is at most 20 candles old.
- **Filters:**
  - SL distance ≤ 2% of entry (rejects wide setups)
  - Candle body ≥ 50% of range (rejects indecision candles)
- **Exit:** fixed 1.5R take-profit; SL is the 3-bar interim low/high.
- **Risk:** 1.5% of equity per trade.
- **Min candles required:** 30.
- **Stage:** scaffolded — pending backtest.

### 2026-04-29 — EMABandBOS — scaffolded

- **File:** [ema_band_bos.py](ema_band_bos.py)
- **Timeframe:** 5m
- **Universe:** all 8 approved pairs
- **Hypothesis:** BOS trades aligned with the 200 EMA trend *and* the 12/21
  EMA momentum band have a much higher edge than naked BOS, because they
  filter out counter-trend breakouts that tend to fail.
- **Indicators:** EMA(12), EMA(21), EMA(200); swing highs/lows on a 5-each-side
  window.
- **Entry:**
  - LONG — close > EMA200 **and** EMA12 > EMA21 **and** BOS-long **and**
    close > EMA12
  - SHORT — close < EMA200 **and** EMA12 < EMA21 **and** BOS-short **and**
    close < EMA12
- **Filters:**
  - EMA200 last cross was ≥ 3 candles ago
  - EMA12/EMA21 last cross was ≥ 2 candles ago
- **Exit:** 1.5R target with 0.1% taker fee priced into the TP; trades whose
  net edge after fees is negative are dropped.
- **Stop:** the safer of (previous candle extreme, BOS level).
- **Risk:** 1% of equity per trade.
- **Min candles required:** 210.
- **Stage:** scaffolded — pending backtest.

### 2026-04-29 — BlueBeltNYSession — scaffolded

- **File:** [blue_belt_ny_session.py](blue_belt_ny_session.py)
- **Timeframe:** 5m
- **Universe:** all 8 approved pairs (USDT-perp; BTC/ETH liquidity preferred)
- **Hypothesis:** The first high-volume 5m candle after the NY cash open
  (14:30 UTC) frames the session's likely range; close-through of that
  bracket's high or low is a high-edge breakout for the rest of the
  session window (until 21:00 UTC).
- **Bracket identification:** first 5m candle on or after 14:30 UTC where
  `volume > volume_20_ma`. If none by 16:00 UTC, the session is skipped.
- **Entry:** at most one long and one short per session, on close beyond the
  bracket high (long) or low (short), inside the 14:30–21:00 UTC window.
- **Stop:** opposite side of the bracket candle.
- **Exit (planned, executor-side):**
  - Move SL to break-even when price reaches +1R (`be_arm_price` is emitted)
  - Exit early at the next swing-high (long) / swing-low (short) where
    candle volume < `vol_ma_20` (no-momentum exit)
  - Fallback fixed TP at 2.4R from entry
  - Forced flat at 21:00 UTC if still open
- **Risk:** 1% of equity per trade (uniform across all 4 strategies).
- **Costs accounted for in target sizing:** 0.1% fee per side + 0.05% slippage.
- **Min candles required:** 20.
- **Stage:** scaffolded — pending backtest.

---

## Entries

_(no completed backtest entries yet — populate after the first run of
`backtesting/engine.py` against each strategy)_
