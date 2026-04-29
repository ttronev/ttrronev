# Changelog

All notable changes to ttrronev will be documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project adheres
to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Initial project scaffolding for ttrronev autonomous crypto trading bot
- Top-level README, memory.md, personality.md
- `data/raw/` and `data/processed/` directories for market data lifecycle
- `strategies/` with strategy template and running strategy log
- `backtesting/engine.py` skeleton (event-driven backtester)
- `ml_models/` tree with models / training / evaluation subfolders
- `exchange/bybit_client.py` skeleton for Bybit v5 REST + WebSocket
- `exchange/paper_trading.py` skeleton for paper-trading simulator
- `openclaw/bridge.py` adapter stub for OpenClaw AI agent platform
- `openclaw/memory_sync.md` describing memory sync protocol
- `openclaw/hooks/` for event-driven OpenClaw integration
- `logs/trades/` and `logs/errors/` directories

### Trading Universe
- BTC/USDT, ETH/USDT, XRP/USDT, SOL/USDT, DOGE/USDT, TRX/USDT, HYPE/USDT, ADA/USDT (perpetual futures, Bybit)

## [0.0.0] - 2026-04-29

- Project bootstrapped.
