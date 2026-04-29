# ttrronev — Persistent Memory

This file is the bot's long-term memory. It survives restarts and is synced with
OpenClaw via `openclaw/memory_sync.md`. Anything written here should be:

- **Durable** — still relevant across sessions / market regimes
- **Specific** — concrete facts, numbers, dates; not vague impressions
- **Actionable** — should change a future decision, not just be trivia

If a note is only useful for the current session, log it to `logs/trades/` instead.

---

## Trading Universe

The bot is currently authorized to trade **only** these 8 Bybit perpetual futures:

```
BTC/USDT, ETH/USDT, XRP/USDT, SOL/USDT, DOGE/USDT, TRX/USDT, HYPE/USDT, ADA/USDT
```

Adding or removing a pair requires explicit owner approval and a new entry below.

## Risk Limits (hard, enforced in code)

- Max leverage per position: 3x
- Max concurrent positions: 4
- Max account drawdown before auto-halt: 15%
- Per-trade risk cap: 1% of equity
- Daily loss limit: 4% of equity (halts trading until next UTC day)

## Lessons Learned

> Append-only. Each entry: `YYYY-MM-DD — pair / strategy — what happened — what to do differently.`

- _(none yet — bot has not traded)_

## Known Issues / Caveats

- HYPE/USDT has shorter price history than the others; backtests starting before
  HYPE's listing date must skip this symbol or the equity curve will be biased.
- Bybit funding intervals are 8h for all listed pairs; the backtester must charge
  funding at 00:00, 08:00, 16:00 UTC to match production.

## Strategy Performance Snapshots

| Strategy        | Backtest Sharpe | OOS Sharpe | Status     | Last Updated |
|-----------------|-----------------|------------|------------|--------------|
| _(none yet)_    | —               | —          | —          | —            |

## Open Questions

- Should funding-rate carry be modeled as a separate alpha or just a cost?
- Walk-forward window length: 90d train / 30d test, or 180/60? Decide after
  first model is trained.
