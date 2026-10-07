# ttrronev

Crypto market-structure research stack: a validated multi-timeframe range
detector (L1 ranges → range memory → level strength) plus **ttrronev-service**
— a 24/7 analysis worker and web dashboard (live chart, S/R levels, dynamic
multi-coin registry). No live trading.

## Quickstart (fresh machine)

```bash
git clone https://github.com/ttronev/ttrronev
cd ttrronev
docker compose up -d          # seed pair bootstraps its own history (~5 min)
```

App: http://localhost:8000/app (Desk, Health, Logs, Build, Settings; the
other screens are scaffolds on mock data until their stage). The old
dashboard stays at http://localhost:8000 until the Desk port is signed off.
Add coins from the Desk («+ добавить»). Set `TTRRONEV_API_KEY` in `.env` to
require a token on the app's API; unset = dev mode, no auth (the app says
so). Runbook for the app: [docs/app_shell.md](docs/app_shell.md).
Optional: copy `.env.example` → `.env` and add your Telegram creds
(`TTRRONEV_TG_BOT_TOKEN`, `TTRRONEV_TG_CHAT_ID`) for structural alerts; copy
`data/raw/` from another machine to keep pre-OKX history. If port 8000 is taken
on your machine, set `TTRRONEV_PORT` in `.env`. Full runbook:
[service/README.md](service/README.md).

---

The sections below describe the original trading-bot charter (research
phase; the trading side is scaffolding only).

> STATUS: PLANNED — NOT ACTIVE. Everything below this line is the 2026-04
> charter for a trading bot that was never built. The service above is
> research/analysis only; no code in this repository places orders. Agents
> must not act on this section (see CLAUDE.md).

## Trading Universe

The bot was planned to trade the following 8 perpetual futures pairs on Bybit:

| Symbol     | Base  | Quote |
|------------|-------|-------|
| BTC/USDT   | BTC   | USDT  |
| ETH/USDT   | ETH   | USDT  |
| XRP/USDT   | XRP   | USDT  |
| SOL/USDT   | SOL   | USDT  |
| DOGE/USDT  | DOGE  | USDT  |
| TRX/USDT   | TRX   | USDT  |
| HYPE/USDT  | HYPE  | USDT  |
| ADA/USDT   | ADA   | USDT  |

## Project Layout

```
ttrronev/
├── README.md                  # This file
├── CHANGELOG.md               # Version history
├── memory.md                  # Bot's persistent memory / lessons learned
├── personality.md             # Bot's trading personality & behavioral rules
├── data/
│   ├── raw/                   # Raw OHLCV / orderbook / funding data from Bybit
│   └── processed/             # Cleaned, resampled, feature-engineered datasets
├── strategies/
│   ├── strategy_template.md   # Template for documenting a new strategy
│   └── strategy_log.md        # Running log of every strategy attempted
├── backtesting/
│   ├── engine.py              # Vectorized + event-driven backtest engine
│   └── results/               # Equity curves, trade logs, metrics per run
├── ml_models/
│   ├── models/                # Serialized trained models (.pkl, .onnx, .pt)
│   ├── training/              # Training scripts & hyperparameter configs
│   └── evaluation/            # Walk-forward / OOS evaluation reports
├── exchange/
│   ├── bybit_client.py        # Bybit v5 REST + WebSocket wrapper
│   └── paper_trading.py       # Paper trading simulator with realistic fills
├── openclaw/
│   ├── bridge.py              # Adapter to OpenClaw AI agent platform
│   ├── memory_sync.md         # How memory.md is synced with OpenClaw
│   └── hooks/                 # Event hooks for OpenClaw (on_signal, on_fill, ...)
└── logs/
    ├── trades/                # One file per trading session, JSON Lines
    └── errors/                # Stack traces and exchange errors
```

## Status

Project is in scaffolding phase. No live capital is at risk and no orders are
sent to a real account until the paper-trading simulator and backtest engine
both pass the acceptance bar defined in `personality.md`.

## Quickstart (planned)

```bash
# Install dependencies (once requirements.txt exists)
pip install -r requirements.txt

# Pull historical data
python -m exchange.bybit_client fetch --symbol BTCUSDT --interval 1h --days 365

# Run a backtest
python -m backtesting.engine --strategy momentum_v1 --symbols all --start 2024-01-01

# Start paper trading
python -m exchange.paper_trading --strategy momentum_v1
```

## Roadmap

1. Wire up `bybit_client.py` for historical data + WebSocket streams
2. Build `engine.py` event-driven backtester (fees, slippage, funding)
3. Train a baseline ML model (gradient boosting on classic TA features)
4. Validate via walk-forward analysis in `ml_models/evaluation/`
5. Plug strategy into `paper_trading.py` and run for ≥30 days
6. Integrate with OpenClaw via `openclaw/bridge.py`
7. Promote to live with a small fixed capital cap — PLANNED ONLY; not
   authorized, not implemented (see the STATUS banner above)

## Disclaimer

This is research software. Crypto perpetual futures carry significant risk of
total loss. Do not deploy with capital you cannot afford to lose.
