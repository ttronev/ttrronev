# ttrronev — Phase 1 paper-trade / live execution

Two-stage rollout of the v1.4 secondary-only strategy on SOL+AVAX+LINK:

- **Phase 1a (this code)**: shadow tracker. Live Binance 5m WS feed,
  streaming-engine wrapper around the validated batch engine, simulated
  $1-risk trades, SQLite + Telegram + /health endpoint. No exchange
  execution.
- **Phase 1b (after 1a passes 7 clean days)**: layers Bybit limit
  orders on top.

## What's here

```
paper_trade/
  config.py                 CLI/env config — pairs, buffer sizes, risk
  sqlite_store.py           8-table schema + connection helpers
  bar_buffer.py             rolling per-pair buffers, persisted
  streaming_engine.py       slice-and-rebatch wrapper around engine
  level_proximity.py        per-setup level-proximity logger (15 levels)
  risk_manager.py           simulated $1 risk, $3 cap, equity audit
  binance_ws.py             WebSocket kline client w/ auto-reconnect
  binance_rest.py           REST kline fetcher for hydration + gap-fill
  telegram_notifier.py      token-bucketed, sanitized async sink
  health_server.py          /health on 127.0.0.1:8765
  daily_validator.py        00:05 UTC streaming-vs-batch diff
  event_sink.py             EngineEvent -> SQLite + Telegram + Risk
  run.py                    main async orchestration
  dry_run_replay.py         end-to-end replay at accelerated speed
  test_streaming_parity.py  unit-style streaming==batch proof
```

## Local dev

```
cd C:\Users\etron\Desktop\ttrronev
python -m venv .venv
.venv\Scripts\activate          # or source .venv/bin/activate on Linux
pip install -r paper_trade/requirements.txt

# Telegram creds (optional for local; bot is no-op without them)
set TTRRONEV_TG_BOT_TOKEN=...
set TTRRONEV_TG_CHAT_ID=...

# Live shadow run
python -m paper_trade.run

# OR end-to-end integration smoke test (replay history at 2 days/min)
python -m paper_trade.dry_run_replay --start 2026-04-15 --end 2026-05-04 \
    --speed-days-per-min 2.0
```

## Validation gate before going live

1. **Streaming parity** — must pass on full 18mo for SOL+AVAX+LINK:
   ```
   python -m paper_trade.test_streaming_parity --pair SOL/USDT --start 2024-11-04 --end 2026-05-04
   python -m paper_trade.test_streaming_parity --pair AVAX/USDT --start 2024-11-04 --end 2026-05-04
   python -m paper_trade.test_streaming_parity --pair LINK/USDT --start 2024-11-04 --end 2026-05-04
   ```
   Each run takes hours (engine re-runs once per bar; growing buffer →
   roughly N² cost). Run once before live; not part of CI.

2. **Dry-run integration** — feed historical bars through the full
   pipeline. Confirms WS handler / SQLite / Telegram / health endpoint
   wiring is correct under bursts:
   ```
   python -m paper_trade.dry_run_replay --speed-days-per-min 2.0
   ```
   Watches logs for: SQLite contention, Telegram drops, health
   endpoint stale events, level-proximity computation errors.

## Production deploy (Hetzner Singapore CX22)

```
# As root (or via cloud-init):
useradd -r -s /bin/false ttrronev
mkdir -p /opt/ttrronev
chown -R ttrronev:ttrronev /opt/ttrronev

# Sync the project (rsync from your dev box, or git clone the repo).
sudo -u ttrronev git clone <repo> /opt/ttrronev
cd /opt/ttrronev
sudo -u ttrronev python3.12 -m venv .venv
sudo -u ttrronev .venv/bin/pip install -r paper_trade/requirements.txt

# Create the env file. NEVER commit this.
sudo -u ttrronev tee /opt/ttrronev/.env >/dev/null <<EOF
TTRRONEV_TG_BOT_TOKEN=...
TTRRONEV_TG_CHAT_ID=...
TTRRONEV_PHASE=1a
EOF
chmod 600 /opt/ttrronev/.env

# A separate env-cron file for the external health checker (so the
# bot's env isn't readable by the cron user).
sudo tee /opt/ttrronev/.env-cron >/dev/null <<EOF
TTRRONEV_TG_BOT_TOKEN=...
TTRRONEV_TG_CHAT_ID=...
EOF
chmod 600 /opt/ttrronev/.env-cron

# Install and start the service.
cp /opt/ttrronev/deploy/paper_trade.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable paper_trade
systemctl start paper_trade

# External health checker.
cp /opt/ttrronev/deploy/health_check.sh /opt/ttrronev/deploy/health_check.sh
chmod +x /opt/ttrronev/deploy/health_check.sh
cp /opt/ttrronev/deploy/health_check.cron /etc/cron.d/ttrronev-health
systemctl reload cron
```

Watch logs:
```
journalctl -u paper_trade -f
tail -f /opt/ttrronev/paper_trade/logs/paper_trade.log
sqlite3 /opt/ttrronev/paper_trade/data/paper_trade.sqlite \
    "SELECT * FROM runtime_events ORDER BY id DESC LIMIT 20"
```

## Phase 1a exit criteria (per spec)

Before flipping to Phase 1b:
- 7 consecutive days w/ no validation divergences vs batch
- Successful manual restart drill (kill -9 the process; verify state
  rebuilds; verify "bot online" Telegram on recovery)
- ≥10 simulated end-to-end trades (signal → fill → resolve)
- Telegram heartbeat & event reliability confirmed (no missed; no false)

## Security notes

- `.env` and `.env-cron` chmod 600, owned by service user. Never in git.
- Telegram bot token is read from env; never logged. The notifier
  redacts API-key-shaped patterns in messages defensively.
- No credentials in any path of any module — all goes through env.
- Phase 1b API keys (Bybit) follow same env-only pattern + IP whitelisting.
