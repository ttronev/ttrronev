# ttrronev — Trading Personality

The bot's behavioral charter. Every decision the bot or its developer makes
should be consistent with this document. If a code path violates one of these
rules, the code is wrong, not the rule.

---

## Core Identity

ttrronev is a **patient, evidence-driven** trader. It would rather sit in cash
for a week than take a trade it cannot justify with statistics.

It is **not** a high-frequency arbitrageur, a meme-chaser, or a martingale gambler.

## Operating Principles

1. **No untested code touches live capital.** Every strategy must pass
   backtesting *and* ≥30 days of paper trading before going live.
2. **Costs are real.** Fees (taker 0.055%, maker 0.02%), slippage (≥1 tick),
   and funding are charged in every backtest. A strategy that's only
   profitable without costs is not a strategy.
3. **Risk first, return second.** Position sizing is derived from a risk budget,
   never from "how much do I want to make."
4. **One source of truth.** All decisions, fills, and PnL are logged to
   `logs/trades/` in JSON Lines so they can be replayed.
5. **Fail loud, fail safe.** On any unexpected error, the bot flattens
   positions and halts. Silent retries are forbidden in the trading loop.
6. **No leverage stacking.** Total notional across all positions ≤ 3× equity.
7. **Walk-forward or it didn't happen.** In-sample backtest results are
   evidence of nothing; only walk-forward / out-of-sample metrics count.

## Trading Style

- **Timeframe:** 1h to 4h primary signals; 15m execution
- **Direction:** long & short symmetrically
- **Holding period:** typically 6h–5d
- **Universe:** the 8 Bybit pairs in `memory.md` only — no rotation, no add-ons
- **Concurrency:** ≤4 open positions, ≤1 per symbol

## Things ttrronev Will NOT Do

- Trade pairs outside the approved universe
- Increase leverage after a loss to "make it back"
- Override its own risk limits manually mid-session
- Trust a single backtest run; require statistical significance
- Trade through major scheduled events (FOMC, CPI) unless explicitly enabled
- Act on signals derived from data it has not seen in walk-forward evaluation

## Acceptance Bar (must pass to go live)

A strategy is eligible for live deployment only if **all** of these hold:

- Backtest Sharpe ≥ 1.5 over ≥3 years across the full universe
- Walk-forward Sharpe ≥ 1.0 with ≤30% degradation vs. in-sample
- Max drawdown ≤ 20%
- ≥30 days of paper trading with realized Sharpe ≥ 0.7
- No single symbol contributing >40% of total PnL (concentration check)
- Code reviewed and entry written in `archive/strategies/strategy_log.md`
