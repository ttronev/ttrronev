# Changelog

All notable changes to ttrronev will be documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project adheres
to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added — ttrronev-service Stage 9a: display zones + event de-noising (2026-08-28)
- **Display zones** (`service/state_builder.py`): new ADDITIVE `zones` field in
  `state.json` — the same strong+weak levels merged into ATR-normalized bands.
  Tolerance = `clamp(0.30 × ATR14(1h)/last_1h_close, 0.2%, 0.8%)` via the (until
  now unused) `shared/atr.py` Wilder ATR; greedy ascending cluster; each zone
  carries a strength-weighted center, member TFs, confluence, top_tf and a small
  member list. Selection budget: ≤6 per side by (top_tf rank, score) plus always
  the single nearest per side. On SOL (ref $96.98) this collapses 36 level-lines
  → 18 merged zones → 9 published (largest cluster 6). **`levels` is byte-
  unchanged** — the chart/table read `zones`; every other consumer still reads
  `levels`.
- **Frontend** (`service/static/app.js` + `index.html` + `style.css`): the chart
  draws zones as bands (thin dashed edges + a labeled center, e.g. `4H · S ×3`);
  a single-member zone draws as one line as before. Levels table rows become
  zones (price-or-`lo–hi`, TFs joined `4H+2H`, `×confluence`); a row click
  flashes the whole band. `window.__zonesRendered` + `__levelsRendered`
  acceptance hooks. Static asset version bumped to v13.
- **Event de-noising**: markers lose their text labels (meaning moves to a static
  legend); `historical_level_touched` is hidden unless the new `касания`
  toggle (`verboseEvents`, default off) is on. rejected/broken/reclaimed still
  draw by default. 7-day window + TF-visibility rule unchanged.
- H1: `.env.example` at repo root (placeholder keys) + README pointer — the real
  `.env` is untouched. H2: `memory.md` Trading-Universe / Risk-Limits sections
  now carry a `PLANNED — NOT ACTIVE` banner (resolves the README-«no live
  trading» vs memory.md-«authorized to trade» contradiction). H5:
  `scripts/pack_source.ps1` builds a credential-free, runtime-free
  `ttrronev_src.zip` (127 files / 0.3 MB).
- Tests: `service/test_zones.py` (8 checks — cross-TF merges, tol clamping,
  budget-keeps-nearest, pack-is-one-band, no-adjacent-within-tol). **No detection
  or memory recompute this stage** (display-only); L5/L5.1 artifacts unchanged.

### Added — ttrronev-service Stage 8: live chart + dynamic pairs (2026-07-30)
- Pair registry (`detectors/results/pairs.json`, seeded from service/pairs.py):
  `POST /api/pairs` (input normalized, symbol validated on OKX; 422/409),
  `DELETE /api/pairs/{pair}` (disk untouched). Worker watches the registry
  and bootstraps new pairs one at a time (1w→1d→4h→2h→1h→5m, full 1h
  history for bias parity, chain + state after the memory TFs) — adding a
  pair is a UI action, no code edits, no restarts (supersedes base
  criterion 4). Registry writes are lock-serialized (thread + cross-process
  lockfile).
- `GET /api/candles/{pair}/{tf}` (tail-reader, mtime cache, cap 1500) and
  extended `/api/state` (status, tfs_ready, recent_events_7d).
- Dashboard v2: TradingView Lightweight Charts 4.2.0 (pinned CDN, Apache-2.0
  attribution in the footer) — candles per TF with instant cached switching,
  level priceLines (kind/strength coded), active-range band lines (current +
  next-higher TF), 7-day event markers, toggles in localStorage, add-pair UI
  with N/6 bootstrap progress and queue display, heartbeat (green <5m /
  yellow <15m / red + stale banner), live last-bar approximation every 15s.
- `/api/health` now also carries worker-level liveness (registry-loop
  heartbeat), so a dead worker is detectable before any pair is ready.

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
