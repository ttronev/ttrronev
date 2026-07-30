# Phase 1b — Bybit live execution spec **v2**

**Status**: spec for review. No code yet.

**Changes vs v1** (all of Egor's critique findings folded in):
- §1: VPS IP-change halt + NTP requirement
- §2: token bucket dimensioning (5/sec trade, 20/sec read); WS auth retry must complete before private subscriptions
- §3: TP-as-LIMIT with reconciliation-driven market fallback (open question 16.1 resolved)
- §3: 4xx retry policy specifics (400/422/401/403 = halt; 429 = retryable)
- §4: reconciliation mismatch HALTS, no silent auto-close; periodic reconcile every 5 min + on every order WS event
- §5: aggregate notional cap = 15× explained (2× safety vs 30× liquidation distance); position size rounds to lot before fee calc
- §6: maker/taker breakdown tracking
- §7: "day" = UTC midnight; /resume requires loss-amount confirmation
- §8: daily live-vs-backtest comparison (per-trade and daily aggregate), not just 30-day
- §9: PF gate raised — 1.5+ = validated, 1.3-1.5 = inconclusive (extend), <1.3 = pause
- §10: slippage outlier thresholds tunable
- §11: /halt and /resume two-step confirm
- §12: index on bybit_orders(pair, setup_id) and (status)
- §13: WS reconnect gap-fill, funding spike, manual subaccount transfer, scheduled maintenance, clock drift added to failure catalog
- §16: open questions resolved (TP=limit+fallback, funding=per-settlement-row, same subaccount, testnet thin fill = cancel & skip, /halt bot-wide)
- New §17: NTP requirement; halt if drift > 5s
- New §18: Bybit maintenance window detection
- New §19: Nightly live-vs-backtest CI

**Prerequisite (unchanged)**: Phase 1a passes 7 days clean on the
local machine, then 7 days on the VPS, before any 1b code is written.

---

## 1. Bybit account setup

(unchanged from v1, plus:)

- **VPS IP must be static.** If the VPS IP changes (provider migration,
  rebuild), Bybit's IP whitelist will reject all signed requests with
  401. The bot detects the 401 and **halts with a Telegram alert**
  identifying the issue. Operator must whitelist the new IP and
  `/resume` before trading restarts. **No silent retry against a
  non-whitelisted IP.**
- **NTP sync required.** Deploy script must enable
  `systemd-timesyncd` (or equivalent) and verify `chronyc tracking`
  shows offset < 1s before bot starts. Bybit signs requests with a
  ±5s timestamp window — clock drift = silent auth failures. See §17.

## 2. Bybit API integration

(unchanged from v1, plus:)

- **Token bucket dimensioning**:
  - **Trade endpoints** (order create/cancel/amend, set-leverage):
    Bybit v5 caps at 10 req/sec per UID across the entire trade
    category. We bucket at **5 req/sec, burst 5** (50% headroom).
  - **Read endpoints** (instruments-info, wallet-balance, position
    list, order list, execution list): Bybit caps at 50 req/sec per
    UID. We bucket at **20 req/sec, burst 20** (60% headroom).
  - Drops on rate-limit log to runtime_events; never to Telegram.

- **WS auth must complete before private subscriptions.** If
  authentication fails on a WS connect/reconnect, the bot does NOT
  silently fall back to public-only mode (which would miss order /
  execution / position events). Auth failure → close WS, log, retry
  with backoff. After 5 consecutive auth failures, halt + alert.

## 3. Order router

### Lifecycle (unchanged) but with explicit **TP-as-limit + reconciliation fallback**:

| Engine event | Router action |
|--------------|---------------|
| `FILLED` | Place limit entry at fib_0.5. On WS confirm fill → place TP **limit** at fib_1.0 |
| `RESOLVED_TP` | Don't blindly trust the engine. **Check Bybit's TP order status** (via WS event cache or REST). If filled → trade closed cleanly. If NOT filled → cancel TP limit, send market exit, log slippage as "engine-tp-vs-bybit divergence" |
| `RESOLVED_SL` | Send market order to close position; cancel any pending TP limit |

Why TP-as-limit:
- TP fires when 5m wick reaches fib_1.0. Engine emits RESOLVED_TP
  immediately, but a market exit takes ~50-200ms to send + match,
  during which price may pull back from fib_1.0 → market fills worse.
- Limit at fib_1.0 fills exactly at the level OR doesn't fill (price
  wicked but reverted before our limit could match). The reconciliation
  fallback handles the latter — if engine says closed but Bybit limit
  unfilled, we send market and log the divergence.

This addresses the canonical engine-says-closed-but-Bybit-says-open
state mismatch. Without the fallback, the engine could emit
RESOLVED_TP on a wick that didn't actually take our limit, and we'd
have an open Bybit position the bot thinks is closed.

### Retry policy on REST errors

| Status | Action |
|--------|--------|
| 200 | OK |
| 429 (rate limit) | Retry up to 5x with exponential backoff (1, 2, 4, 8, 16s). After 5 fails, alert + skip the action, no halt |
| 5xx | Retry up to 3x with exponential backoff (1, 2, 4s). After 3 fails, alert + skip + log; if 5xx is from order placement, halt to prevent uncontrolled retries |
| 400 (bad params) | NO retry. Bug in code or unexpected Bybit response. **Halt + alert with full request body**. |
| 422 (unprocessable) | NO retry. Halt + alert. |
| 401 (auth/IP whitelist) | NO retry. Halt + alert. Operator must verify IP and re-enable. |
| 403 (permission) | NO retry. Halt + alert. |
| Network timeout | Retry 3x, then alert + skip (idempotency-safe operations only) |

## 4. Reconciliation

### Cadence:
- **At startup** (always)
- **On every order/position/execution WS event** (immediate sanity check)
- **Periodic**: every 5 minutes (catches silent state drift even if no events fire)

### Mismatch behaviors — **all halt, no silent auto-correction**:

| Mismatch | Behavior |
|----------|----------|
| Local trade open, Bybit shows no position | **HALT.** Could be: liquidation we missed, manual close by operator on the website, or position transferred. Alert with full state dump (last trade row, last Bybit position snapshot). Require manual investigation + `/resume`. **No "mark closed at last known price" silent loss-taking.** |
| Local has no open trade, Bybit shows position | **HALT + alert.** Manual position outside the bot's control. |
| Bybit has open order not tracked locally | Cancel it (idempotent), log + alert. Don't halt — could be a rejected-then-retried order from a prior session that didn't get cleaned up. |
| Wallet balance differs from local equity by > $0.50 | Log + alert; if difference > $5, halt. Could be unaccounted funding. |

The previous v1 spec had "Local thinks position is open, Bybit shows
no position → recoverable. Mark trade as closed at last known price."
**v2 removes that.** Silent auto-close hides real failures.

## 5. RiskManager extension (LiveRiskManager)

(formula from v1 unchanged)

- **Aggregate notional cap = 15× equity** with this rationale:
  - Per-pair leverage cap: 10x.
  - Max concurrent positions: 3.
  - Naive max notional: 30× equity.
  - At 30× total leverage, liquidation distance from current price is
    ~3.3% adverse move (1/30). At 15×, ~6.6%. Bybit's maintenance
    margin can liquidate before our modeled SL fires if the position
    is too leveraged — the 2× safety buffer between 15× and 30×
    accounts for this.
- **Position size rounds to lot precision BEFORE fee calculation.**
  Otherwise modeled fee differs from actual by lot-rounding error. The
  v1 sizing formula implies this but v2 makes it explicit:
  ```
  coin_size_raw = notional / entry
  coin_size = round_down(coin_size_raw, lot_precision)
  realized_notional = coin_size * entry
  realized_risk = realized_notional * sl_dist_pct
  fee_modeled = realized_notional * TAKER_FEE  # NOT raw notional
  ```

## 6. Slippage tracking

(unchanged, plus:)

- **Maker/taker breakdown** in fees logged. Bybit fees differ for
  maker (typically 0.018%) vs taker (0.055%). Our limit entries are
  `post_only=false` so we expect taker, but if Bybit fills our limit
  against a passive opposite-side order, we get maker rate. The
  difference matters for live-vs-backtest validation (backtest
  assumes taker on both sides). New trades-table column:
  `entry_fee_role` ('maker' | 'taker'), `exit_fee_role` (same).

## 7. Daily / weekly loss limits + halt

(unchanged, plus:)

- **"Day" = UTC midnight rollover.** Daily P&L is computed from the
  equity table sum since 00:00 UTC.
- **/resume requires explicit loss acknowledgement.** A bare `/resume`
  is rejected when halt is active. The operator must send
  `/resume confirm-loss-$X.XX` where `$X.XX` is the displayed loss
  in the halt alert. This prevents fat-finger /resume past a halt
  without operator seeing the damage. The bot treats the confirm
  string as a literal — typos rejected.

## 8. Live-vs-backtest validation

- **Daily comparison**, not just 30-day aggregate. Run automatically:
  - **Per closed trade**: log expected (model fees, zero slippage,
    next-bar-open exit) vs actual (Bybit fees, slippage, market exit
    fill). If per-trade R divergence > **0.2R**, alert ("slippage
    outlier").
  - **Daily aggregate** (run at 00:05 UTC for prior day): compute
    avg-R divergence across the day's closed trades. If
    `|avg(live R) - avg(backtest R)| > 0.05R`, alert ("daily drift").
- **30-day aggregate** still produced for the phase-promotion decision
  but is no longer the primary validation cadence.

## 9. Staged ramp

(unchanged windows, but PF gate raised:)

| Stage | Pass criterion |
|-------|----------------|
| **Bybit testnet** (3-5d) | End-to-end trade execution clean; reconciliation never halts; no order rejections beyond known causes |
| **Mainnet $10** (5-7d) | ≥10 real trades; no exchange errors; slippage within 2σ of model |
| **Mainnet $100** (30d) | **PF ≥ 1.5 AND live net EV > 0** → validated, scale to 0.5% risk on larger bankroll |
| | **PF 1.3-1.5 OR net EV near zero (-0.05R to +0.05R)** → inconclusive, extend 30 more days |
| | **PF < 1.3 OR net EV materially negative (< -0.05R)** → pause, investigate divergence |

v1 had `PF > 1.3` as the validation threshold. That's too low vs
backtest's 1.74-1.94 — could pass with marginal edge. v2 raises to
≥1.5 to clear cleanly.

## 10. Telegram (1b additions)

(unchanged from v1, plus:)

- **Slippage outlier thresholds tunable in config**, not hardcoded:
  ```python
  # paper_trade/config.py
  SLIPPAGE_ENTRY_OUTLIER_PCT: float = 0.0005   # 0.05% — limit shouldn't slip
  SLIPPAGE_EXIT_OUTLIER_PCT: float = 0.0050    # 0.50% — market exits can slip
  PER_TRADE_R_DIVERGENCE_ALERT: float = 0.20   # alert if live R differs from model by >0.2R
  DAILY_R_DIVERGENCE_ALERT: float = 0.05       # alert if daily avg R differs by >0.05R
  ```

## 11. Operator commands

(unchanged from v1, plus two-step confirmation:)

| Command | Effect |
|---------|--------|
| `/halt` | Bot replies with current state (open positions, today's P&L). To execute, operator must reply `/halt confirm` within 60 seconds. |
| `/resume` | If no halt active: no-op. If halt active: bot replies with halt reason and loss amount. To execute, operator must reply `/resume confirm-loss-$X.XX` matching the displayed loss. |

This prevents one-character fat-fingers from pausing/un-pausing
unintentionally.

## 12. SQLite tables

(unchanged from v1, plus indices:)

```sql
CREATE INDEX IF NOT EXISTS idx_bybit_orders_pair_setup
    ON bybit_orders (pair, setup_id);
CREATE INDEX IF NOT EXISTS idx_bybit_orders_status
    ON bybit_orders (status);
```

The reconciler's hot queries are "find open orders for pair X" and
"find open orders by status." Without the indices, full-table scans
on every reconcile cycle — fine at low order volume but degrades.

Schema additions for fee role tracking (per §6):
```sql
ALTER TABLE trades ADD COLUMN entry_fee_role TEXT;
ALTER TABLE trades ADD COLUMN exit_fee_role  TEXT;
```

## 13. Failure modes

(extended from v1 with the missing items:)

| Failure | Detection | Response |
|---------|-----------|----------|
| ... (v1 catalog, unchanged) ... | | |
| **WS reconnect with stale buffer** | WS disconnect > 5min; reconnect_handler must run REST gap-fill before resuming engine calls. **Same fix as Phase 1a's reconnect bug — applies to both Binance public WS (1a) and Bybit private WS (1b)** | Reconnect handler runs REST gap-fill on both 5m + 1h before re-entering message loop. Engine never sees partial buffer. Logged to runtime_events as `ws_reconnect_gap_fill` with stats. |
| **Bybit funding spike** | execution event with funding_paid != 0; if abs(funding_paid) > 0.5% of position notional in a single settlement, alert | Don't halt — funding is normal. But flag outliers for the live-vs-backtest divergence check (model assumes 0 funding). |
| **Manual subaccount transfer by operator** | wallet-balance refresh shows discrepancy not explained by trade P&L + funding | Log + alert. If transfer brings equity below $10 (the staged-ramp minimum), halt new entries until operator confirms. |
| **Bybit scheduled maintenance** | REST returns 503 OR WS connection drops with code 1001 (server going away) AND Bybit's status page is checkable via runtime_events | Enter "exchange unavailable" state: halt new entries, manage existing positions via local SL/TP rules (no router actions), alert. Periodic ping every 60s; resume when 200 OK + WS reconnects cleanly. |
| **Clock drift / NTP fail** | Bybit returns -10004 / -10006 (timestamp out of recv-window) on signed requests; or our local clock vs Bybit server time differs > 5s | HALT + alert. Operator must fix NTP and `/resume`. See §17. |

## 14. Off-the-table

(unchanged from v1)

## 15. Build order

(unchanged from v1, plus:)

- Add: **§19 nightly CI validator** as a dedicated cron job, runs the
  prior 24h of live data through the batch engine and diffs against
  the live trade log. Same approach as 1a's daily validator but with
  order/fee/slippage details.

## 16. Open questions — **all resolved**

1. **TP order: LIMIT with reconciliation fallback.** See §3.
2. **Funding cadence: separate ledger row per settlement.** Cleaner
   audit; storage trivial (~3 settlements × 3 pairs × 365 = 3,285
   rows/year). Existing `equity` table accommodates this with
   `reason='funding'`.
3. **Subaccount per stage: same subaccount, top up.** Preserves
   complete trade history under one ID; avoids API key rotation;
   Bybit may rate-limit subaccount creation.
4. **Testnet thin fill: cancel and skip.** Same trigger as production
   `CANCELLED_PRE_FILL` on close past fib_1.2. Don't wait
   indefinitely.
5. **/halt granularity: bot-wide.** Per-pair halts violate the "no
   discretionary" rule and add state complexity. Each pair is
   independently sized so a bot-wide halt is appropriate.

## 17. Time sync (NEW)

NTP must be running on the VPS. Verification at bot startup:

1. `chronyc tracking` shows `Last offset` < 1s. If > 5s, halt + alert.
2. Compare local time vs Bybit's `/v5/market/time` response. If
   `|local - bybit| > 5s`, halt + alert.

These checks repeat **every 30 minutes** during the heartbeat. Drift
that grows over time triggers a halt before it causes auth failures.

Deploy script additions:
```bash
sudo apt-get install -y chrony
sudo systemctl enable --now chronyd
sleep 5
chronyc tracking | grep "Last offset" || (echo "NTP not synced"; exit 1)
```

## 18. Exchange unavailable / maintenance (NEW)

Bybit maintenance windows are announced in advance but the bot
should also detect them automatically.

**Detection signals:**
- REST returns 503 (Service Unavailable) on multiple consecutive calls
- Public WS connection drops with code 1001 (server going away)
- Private WS auth fails repeatedly with non-credential error codes

**State machine:**
```
NORMAL ──503/1001 (multi)──> EXCHANGE_UNAVAILABLE
EXCHANGE_UNAVAILABLE ──60s ping passes──> NORMAL
```

**During EXCHANGE_UNAVAILABLE:**
- Halt new entries (no order placement)
- Manage existing positions via local SL/TP rules — but router can't
  send market exits, so SL triggers during this window are queued
  and sent on recovery
- Alert at entry + every 15 min while still down
- Heartbeat reflects state

**On recovery:**
- Run reconcile (positions / orders / wallet)
- Process queued SL exits
- Resume normal trading
- Alert with downtime duration

## 19. Nightly live-vs-backtest CI (NEW)

A separate cron job runs nightly at 00:30 UTC (after the 00:05
daily validator). For each pair:

1. Pull yesterday's 5m + 1h bars from SQLite.
2. Run the batch engine on them with full 1h history (extending
   back 90 days for the regime gate).
3. Compare batch-emitted trade decisions against the live trade
   log (`trades` table for that pair, that day):
   - Same setups identified? (same BOS timestamps)
   - Same fill outcomes? (TP / SL / cancelled)
   - Same realized R within slippage tolerance?
4. Write divergence summary to `validation` table with `source='nightly_ci'`.
5. Alert on **any** trade-decision divergence (not just R).
6. After 7 consecutive nights without divergence, the system is
   considered "trade-decision parity validated" and the cron can
   downgrade to weekly.

This catches strategy drift over time that the per-trade slippage
checks (§8) wouldn't notice.

---

## Approval checklist (v2)

Before any 1b code is written, confirm:

- [ ] §1-19 reviewed
- [ ] All v1 open questions answered (covered in §16)
- [ ] Bybit subaccount + API key + IP whitelist set up (operator side)
- [ ] $100 funded (only after 1a passes locally + on VPS)
- [ ] 1a has 7+ clean days of operation locally
- [ ] 1a has 7+ clean days on VPS (sustained run)
- [ ] Phase 1a has ≥10 simulated end-to-end trades observed
- [ ] NTP configured and verified on VPS
- [ ] VPS IP confirmed static
