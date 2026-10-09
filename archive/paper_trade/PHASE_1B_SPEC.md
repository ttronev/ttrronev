# Phase 1b — Bybit live execution spec

**Status**: spec for review. No code yet.
**Prerequisite**: Phase 1a passes 7 days clean (parity stable, validator
zero-divergence, ≥10 simulated end-to-end trades, Telegram reliable).

Phase 1b adds real Bybit limit-order execution on top of the Phase 1a
shadow-tracker infrastructure. Engine, streaming wrapper, signal
decisions, level-proximity logging, daily validator, SQLite schema,
Telegram, health endpoint, systemd, external health-check cron — all
unchanged. The only thing being added is an **execution layer** that
turns engine events into Bybit API calls.

## Architecture diagram (text)

```
                     +----------------------+
                     |  Binance WS / REST   |  (1a; bars + regime)
                     +-----------+----------+
                                 |
                                 v
                     +----------------------+
                     |  Bar buffers (5m/1h) |--> SQLite bars table
                     +-----------+----------+
                                 |
                                 v
                     +----------------------+
                     |  Streaming engine    |--> Engine events
                     +-----------+----------+
                                 |
                                 v
   1a ----------------+         +---------------+ 1b
                      |         |               |
                      v         v               v
              +-------------+   |    +-----------------+
              |  EventSink  |   |    |  Order router   |
              | (1a sim PnL)|   |    | (Bybit limit/   |
              +------+------+   |    |  market orders) |
                     |          |    +--------+--------+
                     v          |             |
              +-------------+   |             v
              | RiskManager |<--+    +-----------------+
              |   (sim $)   |        |  Reconciler     |
              +-------------+        +-----------------+
                     |                       |
                     +-----------+-----------+
                                 v
                   +-------------------------+
                   |  SQLite (orders, fills, |
                   |   trades, slippage,     |
                   |   reconciliation log)   |
                   +-------------------------+
                                 |
                                 v
                          +--------------+
                          |   Telegram   |
                          +--------------+
```

## What changes vs Phase 1a

| Layer | 1a behavior | 1b behavior |
|-------|-------------|-------------|
| Bar feed | Binance public WS | **Bybit WS** (zero divergence between strategy data and exchange data) |
| Regime gate 1h | Binance | **Bybit** (consistent with execution venue) |
| Order placement | None — simulated | Bybit REST + WS for real order lifecycle |
| Position tracking | `RiskManager` simulated | `RiskManager` reads live Bybit positions; reconciles on every event |
| Equity | Simulated `ACCOUNT_START=100` | Real Bybit subaccount equity |
| Per-trade R | `net_pnl / RISK_DOLLARS` (1.0) | `net_pnl / realized_risk_dollars` (varies per trade due to leverage cap & min notional) |
| Fees | Modeled at 0.055% | Actual fees deducted by Bybit; we record both (model vs actual) |
| Slippage | Always 0 | Captured per-trade (limit fill price vs intent; market exit price vs SL trigger close) |
| Funding | Ignored | Recorded per trade if position spans funding settlement |
| Loss halts | Logged only | **Trading stops**: -$5 daily, -$15 weekly, manual reset via Telegram |

---

## 1. Bybit account setup (operator side, before code runs)

1. Create a **subaccount** on Bybit (not main account) for isolation.
2. Enable USDT perpetuals trading; verify you have access to SOLUSDT,
   AVAXUSDT, LINKUSDT contracts.
3. Fund $100 from main → subaccount. (Worst case loss with -15%
   weekly halt = $15.)
4. Create an API key with permissions:
   - **READ**: positions, orders, balance, trade history
   - **TRADE**: place / amend / cancel orders, set leverage
   - **NEVER**: withdraw, transfer between accounts/subaccounts
5. **IP-whitelist** the API key to the bot's VPS public IP only.
6. Store `BYBIT_API_KEY` + `BYBIT_API_SECRET` in `/opt/ttrronev/.env`,
   chmod 600. Never in code, logs, or Telegram messages.
7. Set per-symbol leverage to **10x** (the bot's hard cap) via the
   Bybit UI or via the bot at startup; verify via API.

The bot **refuses to start** if:
- Either env var is missing.
- IP isn't whitelisted (Bybit returns 401).
- Symbol metadata (min notional, lot size, max leverage) can't be
  fetched. Trading without metadata = uncontrolled sizing risk.

---

## 2. Bybit API integration (`paper_trade/bybit_*.py`)

### `bybit_rest.py` — async REST client
Uses `aiohttp.ClientSession`. All requests HMAC-SHA256 signed per Bybit
v5 API. Endpoints we need:

| Method | Endpoint | Purpose |
|--------|----------|---------|
| GET | `/v5/market/instruments-info` | symbol metadata (lot size, min notional, max leverage) at startup |
| GET | `/v5/account/wallet-balance` | equity at startup + reconcile |
| GET | `/v5/position/list` | open positions reconciliation |
| GET | `/v5/order/realtime` | open orders reconciliation |
| POST | `/v5/order/create` | place limit / market orders |
| POST | `/v5/order/cancel` | cancel pending order |
| POST | `/v5/position/set-leverage` | set per-symbol leverage |
| GET | `/v5/execution/list` | actual fills + fees + funding |

Rate limits: Bybit v5 caps trade endpoints around ~10 req/sec per UID.
We're well under (one trade can take 2-3 calls; we make at most 3-5
calls per minute under normal load). Add a token bucket anyway as a
safety net.

### `bybit_ws.py` — async WebSocket
Two streams subscribed at startup:
1. **Public klines** for the 3 pairs at 5m and 1h. Replaces Binance WS
   from Phase 1a. Same `on_closed_bar` callback contract.
2. **Private** stream (authenticated) for:
   - `position` topic — live position updates
   - `order` topic — order state changes (new / partial-fill / filled / cancelled / rejected)
   - `execution` topic — actual fills with fees / funding

Auto-reconnect with exponential backoff (already proven in Phase 1a).
Auth: send `auth` op with HMAC signature on connect. Re-auth on every
reconnect.

---

## 3. Order router (`paper_trade/order_router.py`)

Lifecycle:

```
EngineEvent (FILLED expected) ──> RiskManager.size_position()
                                      |
                                      v
                              place limit entry order
                                      |
                                      v (Bybit `order` WS event: filled)
                              record fill, place TP limit
                              start watching for SL / TP
                                      |
                                      v (one of:)
                            +---------+----------+
                            |                    |
                  TP filled (WS)        Close-based SL trigger (5m close)
                            |                    |
                            v                    v
                      record close       Send market exit
                      (PnL from fill)    Wait for fill (WS)
                                                 |
                                                 v
                                           record close
```

Mapping engine events → router actions:

| Engine event | Router action |
|--------------|---------------|
| `ARMED` | none (logged; setup is "watch list") |
| `PRIMARY_TRIGGERED` | none (signal observed; no order yet) |
| `FILLED` (engine sees fill_wick at fib_0.5) | **Place limit entry at fib_0.5** (post-only=false; we want to fill on touch). On Bybit WS confirms fill → place TP limit at fib_1.0 |
| `RESOLVED_TP` | should already be filled by Bybit's TP limit; reconcile |
| `RESOLVED_SL` (close-based) | **Send market order to close**. Wait for fill via WS |
| `CANCELLED_*` | Cancel pending limit entry if not yet filled |
| `OPEN_EOD` | n/a (engine artifact) |

**Critical: SL is close-based, not touch-based.** Exchange stop-loss
orders trigger on touch — that's the wrong rule for v1.4. Our SL fires
only when a 5m bar **closes** beyond fib_0.3. The close-based detection
stays in the streaming engine; the router's only job on SL is to send
a market exit when the engine emits `RESOLVED_SL`.

### Why limit entries on fib_0.5

The engine assumes a wick fill at fib_0.5. Reality:
1. Bybit limit at fib_0.5 fills only when price prints ≤ fib_0.5
   (long) — same condition the engine uses.
2. Fill price = fib_0.5 exactly (it's a limit, not market). Slippage
   only on partial fills (rare for $1-risk-sized SOL/AVAX/LINK).
3. **Post-only = false** because we want to fill on touch, not be a
   maker that waits for someone to take us. That accepts taker fees on
   the entry — already modeled at 0.055%.

### Retry logic

On transient network/API failures:
- **3 retries** with exponential backoff: 1s, 2s, 4s.
- After 3 fails, abort the action, fire Telegram alert with action
  + setup_id, do NOT retry indefinitely.
- Don't retry on **client errors** (400/401/403/422) — those are bugs,
  not transient. Alert immediately.
- For partial-fill retries: cancel residual + resize remainder; only if
  realized_risk would still be ≥ 0.5 × intended_risk. Otherwise accept
  the partial as-is.

---

## 4. Reconciliation (`paper_trade/reconciler.py`)

Runs at:
- **Bot startup** (always)
- After every order/position/execution WS event (sanity check)
- On any unhandled exception that triggers a pause

Compares Bybit truth against local SQLite:

```
        Bybit                        Local
--------------------------- ↔ ---------------------------
  open positions (per pair)    open trades in `trades`
                                without exit_timestamp_ms
  open orders (limit, TP)      pending orders in router state
  wallet balance               equity table running sum
```

**Mismatch behaviors**:
- Local thinks position is open, Bybit shows no position → recoverable.
  Mark trade as closed at last known price; alert.
- Local has no open trade, Bybit shows position → **HALT**. Manual
  intervention required (someone placed a manual order, or restart
  recovery missed something). Telegram alert with details.
- Open order on Bybit not tracked locally → cancel it, alert. (Could be
  a stale order from a prior session.)
- Wallet balance differs from local equity by > $0.50 → log + alert.
  Could be funding payments we missed accounting for.

**Restart-time reconciliation**: bot does NOT trade until it has
performed a successful reconcile with zero unresolved mismatches. If
mismatches exist, it pauses and waits for a Telegram command
(`/resume` or `/halt`) from the operator.

---

## 5. RiskManager extension (`paper_trade/risk_manager.py`)

Phase 1a's `RiskManager` becomes the parent class. Phase 1b adds a
`LiveRiskManager`:

```python
class LiveRiskManager(RiskManager):
    def __init__(self, bybit_rest, bybit_ws):
        ...

    async def refresh_equity(self) -> float:
        # GET /v5/account/wallet-balance
        # Sets self.equity from real account.

    def size_position(self, entry, sl, intended_risk_dollars=1.0,
                      max_leverage=10.0) -> SizingResult:
        # The user-provided formula (verbatim from spec):
        sl_dist = abs(entry - sl)
        sl_dist_pct = sl_dist / entry
        notional = intended_risk_dollars / sl_dist_pct
        if notional < self.symbol_min_notional:
            notional = self.symbol_min_notional
            # log: realized risk > intended (rare; tiny SL distances)
        required_lev = max(1.0, notional / self.equity)
        if required_lev > max_leverage:
            leverage = max_leverage
            notional = self.equity * max_leverage
            # log: realized risk < intended
        else:
            leverage = required_lev
        coin_size = round_to_lot(notional / entry, self.symbol_lot_precision)
        realized_notional = coin_size * entry
        realized_risk = realized_notional * sl_dist_pct
        return SizingResult(coin_size, leverage, realized_notional, realized_risk)
```

**Never skip** a setup due to sizing constraints (per user spec). If
leverage cap or min notional binds, take the trade with realized risk
clamped (logged in the trades table as `intended_risk_dollars` vs
`realized_risk_dollars`).

**Aggregate exposure cap**: total notional across open positions ≤
**15× equity**. With $100 equity and 10x per-trade max, this caps at 3
concurrent positions averaging 5x each. Equivalent to Phase 1a's "max 3
concurrent positions" rule, expressed in notional terms.

---

## 6. Slippage tracking

Per-trade comparison logged in the existing `trades` table (columns
already defined in 1a schema):

| Column | Meaning |
|--------|---------|
| `slippage_entry` | `actual_fill_price - intended_entry_price` (signed; positive = adverse for long) |
| `slippage_exit` | For market exits only: `actual_fill_price - sl_trigger_close` |
| `funding_paid` | Cumulative funding rate × notional × time-fraction held (signed) |
| `backtest_expectation_json` | What the backtest engine would have predicted for this trade with model fees and zero slippage |

**Limit entries should have slippage = 0** (limit price is the fill
price). Non-zero slippage on entry indicates a partial fill or some
exchange weirdness — alert if observed.

**Market exits** (close-based SL) will have slippage. Track distribution
to see if SL execution costs more than the model assumed.

---

## 7. Daily / weekly loss limits + halt

| Threshold | Behavior |
|-----------|----------|
| Daily P&L ≤ −$5 | **HALT**. Cancel all open limit orders. Close any open positions (market). Send Telegram alert. Wait for `/resume` command. |
| Weekly P&L ≤ −$15 | Same as daily. The 7-day P&L is computed from the equity table at midnight UTC each day. |
| Unhandled exception | Pause new entries; existing positions managed normally; alert with stack trace; require `/resume`. |
| Reconciliation mismatch | Pause new entries; alert; require manual investigation. |

Halt state persists in SQLite (`halt_state` row in a new
`halt_state` table — single-row table updated on every halt/resume).
On restart, the bot reads halt state and refuses to trade if halted.

---

## 8. Live-vs-backtest validation

Per closed trade, log both **expected** (backtest model) and
**actual** (Bybit) numbers:

| Field | Expected | Actual |
|-------|----------|--------|
| Fill price (entry) | `fib_0.5` | Bybit fill price |
| Fill price (exit, TP) | `fib_1.0` | Bybit fill price |
| Fill price (exit, SL) | next-bar open | Bybit market fill price |
| Fees | `0.055% × notional × 2` | Bybit actual fees |
| Funding | 0 | Bybit accrued funding |
| Net R | `gross - model_fees / risk` | `gross - actual_fees - funding` / risk |

After 30 days, aggregate:
- Live WR vs backtest WR
- Live avg R vs backtest avg R
- Live net EV vs backtest net EV
- Slippage distribution (mean, median, p90)
- Sizing skip frequency (target: ~0; log realized vs intended risk distribution)
- Funding cost as a fraction of gross P&L

---

## 9. Staged ramp

| Stage | Duration | What | Pass criterion to advance |
|-------|----------|------|---------------------------|
| **Phase 1a** | 7-14 days | Shadow tracker on live Binance | 7 days no validation divergences, ≥10 simulated trades, Telegram reliable |
| **Bybit testnet** | 3-5 days | Full bot logic on testnet, simulated funds | Successful end-to-end trade execution, reconciliation clean, no order rejections |
| **Mainnet $10** | 5-7 days | Real money, $1 risk against $10 equity | ≥10 real trades, no exchange errors, slippage within model assumptions |
| **Mainnet $100** | 30 days | Real money, $1 fixed risk | Live net EV > 0 AND PF > 1.3 → scale to 0.5% risk on larger bankroll. Live near zero → extend 30 more days. Live materially negative → pause and investigate. |

Each stage **must explicitly pass** before advancing. No shortcuts.
Operator confirms via Telegram before each transition.

---

## 10. Telegram additions for 1b

On top of 1a's events:
- **Order placed**: `entry limit @ X for SOLUSDT, size N, leverage 5x, intended_risk $1.00`
- **Order partial-fill**: `partial fill X / Y @ Z`
- **Order rejected**: `rejected: <Bybit reason>` — alert with severity
- **Real PnL per close**: replaces Phase 1a's simulated close message
  (use the actual fill price + actual fees + funding)
- **Slippage outlier**: flag any entry slippage > 0.05% (limit
  shouldn't slip) or market exit slippage > 0.5% (excessive)
- **Halt fired**: `daily/weekly loss halt active. Open positions
  closed. /resume after manual review.`
- **Reconciliation mismatch**: `mismatch: <description>. Trading
  paused. /resume after manual review.`
- **Real equity Δ today** in daily summary (vs simulated in 1a)

Telegram security from 1a applies unchanged: token never logged,
rate-limited at 15/min, message sanitization for API keys / tokens.
**No account balance specifics** in messages — only deltas in $ / R.

---

## 11. Operator commands (Telegram → bot)

A new minimal command parser. Bot accepts these from the configured
`TG_CHAT_ID` only (auth = trust the chat ID):

| Command | Effect |
|---------|--------|
| `/status` | reply with current pair states, n_open_positions, today's P&L, halt state |
| `/halt` | manual halt; cancel orders, close positions, set halt state |
| `/resume` | clear halt state (after operator review); re-enable trading |
| `/positions` | list open positions with entry, current price, unrealized P&L |

These are the only commands. No `/place-order` etc. — discretionary
overrides are **explicitly off the table** per Phase 1b spec.

---

## 12. New SQLite tables / column additions

Additive, no breaking changes:

```sql
CREATE TABLE IF NOT EXISTS bybit_orders (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    pair            TEXT NOT NULL,
    setup_id        INTEGER,
    bybit_order_id  TEXT NOT NULL UNIQUE,
    bybit_link_id   TEXT,           -- our client-side ID
    side            TEXT NOT NULL,  -- buy / sell
    order_type      TEXT NOT NULL,  -- limit / market
    purpose         TEXT NOT NULL,  -- entry / tp / sl_market_exit / cancel
    size_coins      REAL NOT NULL,
    price           REAL,           -- null for market
    status          TEXT NOT NULL,  -- new / partial_filled / filled / cancelled / rejected
    filled_size     REAL,
    avg_fill_price  REAL,
    fees_paid       REAL,
    placed_ms       INTEGER NOT NULL,
    last_event_ms   INTEGER NOT NULL,
    raw_response    TEXT             -- JSON of Bybit's last response for forensics
);

CREATE TABLE IF NOT EXISTS halt_state (
    id              INTEGER PRIMARY KEY CHECK (id = 1),
    halted          INTEGER NOT NULL,
    reason          TEXT,
    halted_at_ms    INTEGER
);
```

Column additions to existing `trades`:
- `bybit_entry_order_id`
- `bybit_tp_order_id`
- `bybit_exit_order_id`

(No migration script needed because `INSERT OR REPLACE` is the only
write pattern; new columns will simply be NULL on rows from Phase 1a.)

---

## 13. Failure modes & responses

Cataloging known failure modes so we know what each looks like:

| Failure | Detection | Response |
|---------|-----------|----------|
| Bybit WS disconnect | aiohttp WS closed | Auto-reconnect (1a code path), log to runtime_events, no halt |
| Bybit REST 5xx | aiohttp response | Retry 3x exp backoff; if all fail, alert + skip the action |
| Bybit REST 4xx (auth/param) | aiohttp response | NO retry; alert immediately; halt new entries until reviewed |
| Order rejected (insufficient margin) | order WS event | Should be impossible if sizing is correct; alert + halt; investigation |
| Position appears not in our books | reconciler | Alert + halt; manual investigation |
| Partial fill | order WS event | Cancel residual; if filled ≥ 0.5x intended, treat as full; else alert |
| Funding settlement at unexpected time | execution WS event | Log; no action |
| Bybit liquidates our position | position WS / execution event | Should be impossible if leverage cap is correctly enforced; alert + halt + investigation |
| VPS network outage | health endpoint stale | External cron alerts at 2-fail; systemd restarts service; reconcile on restart |
| Equity drifts > $0.50 from local | reconciler periodic check | Alert; investigate; usually unaccounted funding |

---

## 14. What's explicitly **off the table** in Phase 1b

(re-stating from the user's earlier instruction so it's recorded here):
- No discretionary overrides during execution. Bot trades or doesn't.
- No strategy changes during the 30-day evaluation. v1.4 as-is.
- No level-based filters. Track composite proximity in logs only.
- No re-running historical backtests during 1b. Live data is the test.
- No leverage above 10x cap, ever.

---

## 15. Build order (after spec review)

Sub-tasks once spec is approved (rough sequence; some can run in parallel):

1. `bybit_rest.py` — REST client + signing + symbol metadata fetch.
2. `bybit_ws.py` — public + private WS streams.
3. `order_router.py` — limit entry / TP / market exit lifecycle.
4. `reconciler.py` — startup + periodic reconciliation logic.
5. `risk_manager.py` extension — `LiveRiskManager` subclass.
6. Slippage tracking + new SQLite columns + tables.
7. Halt logic + `/halt`/`/resume` commands in Telegram.
8. Live-vs-backtest validation per closed trade.
9. Stage progression scaffolding (testnet flag, ramp config).
10. Operator runbook update for the staged ramp.

Estimated effort: 1-2 weeks engineering after spec approval, plus the
3-5 days testnet + 5-7 days $10 mainnet ramp before $100 main run.

---

## 16. Open questions for review

1. **TP order: limit or no order?** Spec above places a TP **limit order**
   on Bybit when entry fills. Alternative: don't place a TP order on the
   exchange; instead, monitor the engine and send a **market exit** when
   the engine emits `RESOLVED_TP`. Pros of market exit: zero
   exchange-side risk of mismatched levels (e.g., if our fib values get
   recomputed). Cons: TP fills at a worse price than fib_1.0 in a fast
   market. Which do you want?

2. **Funding cadence for tracking**: Bybit settles funding every 8h. Do
   you want a separate ledger row for each funding settlement, or
   accumulate into the trade row at close? Spec assumes the latter
   (simpler), at a small cost in real-time visibility.

3. **Subaccount transfer for ramp**: when ramping $10 → $100, do you
   prefer (a) topping up the same subaccount, or (b) creating a new
   subaccount each stage? (b) gives cleaner per-stage equity history;
   (a) preserves the position and trade record.

4. **What's the rule if testnet refuses to fill our limits at fib_0.5?**
   Testnet liquidity is sometimes thin. If a limit sits unfilled for >
   the engine's expected fill window (typically same bar to a few bars
   later), do we (a) cancel and skip the trade, or (b) wait
   indefinitely? Spec implies (a) — cancel on engine `CANCELLED_*` —
   but testnet might cause many such cancels. Worth being explicit.

5. **`/halt` granularity**: bot-wide, or per-pair? Spec assumes bot-wide
   (simpler, matches "halt new entries" semantics). Per-pair is more
   surgical but adds state complexity.

---

## 17. Approval checklist

Before any 1b code is written, confirm:

- [ ] Spec items 1-15 reviewed
- [ ] Open questions 16.1-16.5 answered
- [ ] Bybit subaccount + API key + IP whitelist set up
- [ ] $100 in subaccount (only after 1a passes)
- [ ] 1a has 7+ clean days of operation
- [ ] Phase 1a has ≥10 simulated end-to-end trades observed
