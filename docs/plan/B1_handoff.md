# B1 handoff — Hyperliquid data layer

## Context for this task

- Track B, build-order step 1. Phase 0 is merged and running in Docker on the laptop; B0a (app shell) is on branch `app-shell-b0`. OKX stays the deep-history source for the detector; Hyperliquid becomes the live data feed and, later (B7), the execution venue.
- **Protected core:** nothing under `detectors/` is modified in this task. B1 is purely additive: a new `exchange/` client, new data files under the sandbox root, one new compose service.
- **Decision:** record live trades from day one — the trade recorder is part of this task and starts as soon as the service is up, because Hyperliquid does not let us backfill trades later.
- Pairs: the registry's pairs mapped to Hyperliquid coins through `exchange/symbols.py`. A registry pair with no Hyperliquid market is skipped and listed in `status` and `/health` as `unmapped`, never silently dropped.
- Dependencies: whatever client library is chosen (official `hyperliquid-python-sdk` or plain `websockets` + `httpx`) must be pinned in `requirements-service.txt`; no new dependency without a pinned version.
- Branch `hl-data-b1` off `main`. Open with a short plan (symbols → backfill → recorder → ctx poller → compose service → tests), then one layer at a time with a lock between them, per the standing rule.
- Report each Definition of Done item with evidence when finished (tests, ruff/mypy, memory self-report, the 24 h `status` output). Stop after B1: B0c (Desk widget board) comes next as a separate task.

---

# ТЗ-B1 — Hyperliquid data layer

B1 delivers one Python client that backfills candles, records every trade, and polls funding/open interest from Hyperliquid, writing under the sandbox root. OKX remains the deep-history source for the detector; this client serves the live window and, later, execution.

## 1. Goal

After B1 the project has, per pair: Hyperliquid candles for every interval the detector uses, a continuous local record of trades with no gap longer than 60 s, and a 15-second series of funding rate, open interest and mark/oracle premium.

## 2. Scope

In scope: REST backfill via `candleSnapshot`; WebSocket subscription to `trades` and `candle`; polling of `activeAssetCtx` (or the equivalent perps context call); gap detection and repair; storage; a `status` command; tests. Out of scope: any order placement, any change to the detector, any change to OKX fetching.

## 3. Inputs and constraints

- Hyperliquid mainnet for data (`https://api.hyperliquid.xyz`, `wss://api.hyperliquid.xyz/ws`); testnet URLs behind a `TTRRONEV_HL_ENV` variable for B7.
- `candleSnapshot` returns at most the most recent 5000 candles per interval. The client never assumes deeper history exists.
- Candle volume is in coins; `n` is the trade count. Store both.
- Pair naming: Hyperliquid uses the coin symbol (`SOL`); the repo uses `SOL_USDT`. One mapping table in `exchange/symbols.py`, tested.
- Rate limits: respect the published weight limits; on HTTP 429 back off exponentially from 1 s to 60 s.
- All paths resolve through `detectors/paths.py` under `TTRRONEV_RESULTS_ROOT` (A0.7). The client never writes to `data/raw/` OKX files.

## 4. Deliverables

| File | Contents |
| --- | --- |
| `exchange/hyperliquid_client.py` | `HLClient` with `backfill_candles(pair, interval)`, `run_trade_recorder(pairs)`, `run_ctx_poller(pairs, every=15)`, `status(pair)` |
| `exchange/symbols.py` | repo pair ↔ HL coin mapping |
| `exchange/hl_store.py` | Parquet writer for trades (one file per pair per UTC day), CSV writer for candles (`{PAIR}_{tf}_hl.csv`), JSON-lines writer for ctx |
| `tests/test_hl_client.py` | unit + integration tests (section 7) |
| `docs/hl_data_layer.md` | how to run, file layout, known limits |

## 5. Functional requirements

1. `backfill_candles` fetches up to 5000 candles for each interval in `{1m, 5m, 15m, 1h, 4h, 1d}`, merges into the local CSV by timestamp, and is idempotent: a second run changes nothing.
2. The trade recorder subscribes to `trades` for every pair in the registry, writes each trade (`ts_ms, px, sz, side, tid, hash`) to the day's Parquet file in batches of ≤ 500 rows or 5 s, whichever first, and flushes on shutdown.
3. Reconnect: on any socket error or a 30 s silence the recorder reconnects with backoff, re-subscribes, and logs a `gap` record with start and end timestamps.
4. Gap repair: after reconnect the recorder calls `recentTrades` (or the closest available endpoint) and inserts any trades missing by `tid`; duplicates are rejected by `tid`.
5. The ctx poller writes `{ts, funding, open_interest, mark_px, oracle_px, premium, day_ntl_vlm}` every 15 s per pair to JSON lines, rotating daily.
6. `status(pair)` prints: candles per interval with first/last timestamp, recorded trades for the last 24 h, largest gap in seconds, last ctx timestamp.
7. The recorder and poller run as one asyncio process, `python -m exchange.hyperliquid_client run`, with a `/health` JSON on `127.0.0.1:8766` (`last_trade_ts` per pair, `gaps_24h`).
8. Logging through the standard `logging` module; no `print` in library code; errors logged with the pair and the raw payload length, never the payload itself.
9. Memory: steady-state RSS under 300 MB for 20 pairs; peak RSS self-reported at shutdown per `MEMORY_HYGIENE.md`.

## 6. Interfaces

```python
class HLClient:
    def __init__(self, env: str = "mainnet", root: Path | None = None): ...
    def backfill_candles(self, pair: str, interval: str) -> int: ...  # rows added
    async def run_trade_recorder(self, pairs: list[str]) -> None: ...
    async def run_ctx_poller(self, pairs: list[str], every: int = 15) -> None: ...
    def status(self, pair: str) -> dict: ...
```

Trade Parquet schema: `ts_ms:int64, px:float64, sz:float64, side:str, tid:int64, hash:str`. Candle CSV columns match the existing OKX files exactly, plus `n_trades`.

## 7. Tests

- Unit: symbol mapping both ways for all registry pairs; candle merge is idempotent; `tid` dedupe; backoff schedule values; `status` on an empty store returns zeros, not an exception.
- Integration (recorded fixtures, no network in CI): replay a captured WebSocket stream with an injected disconnect; assert one `gap` record and zero duplicate `tid` after repair.
- Parity (network, run by you): HL vs OKX 1h closes on the overlapping window agree within 0.1 % for SOL, BTC, ETH; report the max deviation.
- Point-in-time: not applicable (no derived series).
- Static: `ruff` clean; `mypy` clean on `exchange/`.

## 8. Acceptance

1. Run `python -m exchange.hyperliquid_client run` for 24 h on the laptop Docker stack (as a third compose service).
2. Run `python -m exchange.hyperliquid_client status --pair SOL_USDT`.
3. Expected: trades recorded for 24 h, largest gap ≤ 60 s, ctx series present, candles for all six intervals with last timestamp within one interval of now.
4. Kill the process with SIGKILL and restart: the Parquet files are readable and the first new trade after restart has no duplicate `tid`.

## 9. Review checklist

- [ ] No API keys anywhere (B1 is unauthenticated; keys arrive in B7)
- [ ] Every write atomic (temp file + rename)
- [ ] Every path through `detectors/paths.py`
- [ ] Errors logged and surfaced in `/health`
- [ ] Peak RSS reported
- [ ] CHANGELOG, README, STRATEGY\_DOCUMENTATION updated

## 10. Rollback

The client is additive: delete the `*_hl.csv`, Parquet and ctx files and remove the compose service. No other file changes.
