# Strategy: <NAME_HERE>

> Copy this file to `strategy_<name>.md` for each new strategy. Fill every
> section. An incomplete strategy doc means the strategy cannot be backtested,
> let alone go live.

## 1. Hypothesis

One paragraph. What market inefficiency or behavior is this strategy
exploiting? Why should it work? Why hasn't it been arbitraged away?

## 2. Universe & Timeframe

- **Symbols:** _(subset of the 8 approved pairs, or "all")_
- **Signal timeframe:** _(e.g. 1h)_
- **Execution timeframe:** _(e.g. 15m)_
- **Holding horizon:** _(typical & max)_

## 3. Signal Definition

Precise, code-ready definition. Include all parameter values.

```
entry_long  := <condition>
entry_short := <condition>
exit        := <condition>  # stop-loss, take-profit, time-based, signal flip
```

## 4. Position Sizing & Risk

- Per-trade risk: ___ % of equity
- Stop-loss: _(ATR multiple, fixed %, structural?)_
- Take-profit / trailing logic: ___
- Max leverage: ___ (must be ≤ 3x per `personality.md`)
- Concurrent position cap: ___

## 5. Costs Model

What fee tier, slippage assumption, and funding handling does the backtest use?
Defaults: taker 0.055%, slippage 1 tick, funding charged at 00/08/16 UTC.

## 6. Backtest Plan

- Date range: ___
- Walk-forward: ___ train / ___ test, rolling
- Metrics required: Sharpe, Sortino, max DD, win rate, avg win/loss, turnover

## 7. Acceptance Criteria

The strategy passes if it meets the bar in `personality.md`:

- [ ] Backtest Sharpe ≥ 1.5
- [ ] Walk-forward Sharpe ≥ 1.0
- [ ] Max drawdown ≤ 20%
- [ ] No symbol > 40% of PnL
- [ ] ≥ 30 days paper-trading with realized Sharpe ≥ 0.7

## 8. Failure Modes

What environments will hurt this strategy? (e.g. choppy ranges, low vol,
exchange outage, funding spikes). What's the kill-switch?

## 9. Status

| Stage             | Result | Date |
|-------------------|--------|------|
| Hypothesis review |        |      |
| Backtest          |        |      |
| Walk-forward      |        |      |
| Paper trading     |        |      |
| Live              |        |      |

## 10. Notes / Iterations

Append-only log of changes to this strategy. Date every entry.
