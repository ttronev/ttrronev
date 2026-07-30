# Changelog

All notable changes to ttrronev will be documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project adheres
to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added — ttrronev-service (2026-07-28)
- `service/` package: 24/7 analysis worker (`python -m service.worker`) +
  FastAPI web API/dashboard (`uvicorn service.api:app`). Fetches OKX candles
  on every bar close, regenerates the detector stack (windowed: 5m→45d,
  1h/2h→1y, 4h→2y, 1d/1w→full), publishes per-pair `state.json` / `live.json`
  / `heartbeat.json`, reuses `detectors/alerts.py` for Telegram. No trading.
- Pair-scoped results layout: detector artifacts moved to
  `detectors/results/{PAIR}/` (migration: `python -m service.migrate_results`);
  `detectors/paths.py` is the single path source; pair is a parameter through
  the detector wrappers, chain scripts, `query_state`, `alerts`,
  `freshness_monitor`, `plot_range_detector`. Detector logic unchanged —
  verified byte-identical on SOL 1d before/after.
- Docker: one image (`Dockerfile` + pinned `requirements-service.txt`),
  `docker-compose.yml` (prod) + `docker-compose.override.dev.yml` (Windows
  dev with reload), `deploy.sh` + `deploy/Caddyfile.example` for the VPS.

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
