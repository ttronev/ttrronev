# ttrronev

Crypto market-structure analysis: a validated multi-timeframe range and level
detector (`detectors/`, the protected core) run 24/7 by a worker, a FastAPI
service and a React app (Desk chart with zones, health, logs, build status).
No live trading: the bot described in `docs/agent/` is planned, not active
(see [CLAUDE.md](CLAUDE.md)).

## Run

```bash
git clone https://github.com/ttronev/ttrronev && cd ttrronev
docker compose up -d      # builds the image (a node stage builds the app); the seed pair bootstraps in ~5 min
```

App: http://localhost:8000/app · old dashboard: http://localhost:8000 (until
Desk parity is signed off). Port: `TTRRONEV_PORT` in `.env`. Telegram alerts:
copy `.env.example` to `.env` and fill the token and chat id. App API token:
`TTRRONEV_API_KEY` (unset = dev mode, no auth; the app says so).

Dev: `make dev` (compose with bind mounts and dry-run alerts), `make dev-ui`
(Vite), `make test`, `make build`. Every Makefile target is one line you can
paste into PowerShell when `make` is absent.

## Map

| Path | What |
|---|---|
| `detectors/` | PROTECTED CORE: range detection, cleanness, nesting, known-at, range memory, strength, alerts, `paths.py`. Byte-identical by manifest (`scripts/check_layout.py`). |
| `shared/` | pure helpers: pricefmt, csvtail, ioutil, atr, memhygiene, structure_analyzer |
| `service/` | worker, regen, api (+ `api_v1`), state_builder, `static/` (the old dashboard until Desk parity) |
| `exchange/` | Hyperliquid client (B1); later the execution adapter (B7) |
| `frontend/` | the React app (B0a); built by the image's node stage, served at `/app` |
| `data/` | OKX fetch, bootstrap and freshness code; `data/raw/` candles stay gitignored |
| `deploy/` | `deploy.sh`, Caddy example, host watchdog |
| `docs/plan/` | stage map (`stages.json`), handoffs |
| `docs/runbooks/` | service, deploy, app_shell, memory_hygiene, validation_status |
| `docs/decisions/` | hyperliquid, thin-client, protected-core, frontend-stack |
| `docs/agent/` | the in-app agent's charter (memory.md, personality.md), used from B8 |
| `research/backtesting/` | engines, level features, replay audits; `results/` gitignored |
| `archive/` | frozen: paper_trade, ml_models, openclaw, strategies, exchange_stubs ([why](archive/README.md)) |
| `scripts/` | `check_layout.py` (layout guard + detectors manifest), `pack_source.ps1` |
| `tests/` | pytest by package: detectors, service, shared, exchange; the app's own tests live in `frontend/` |

Runbooks: [service](docs/runbooks/service.md) · [deploy](docs/runbooks/deploy.md) ·
[app shell](docs/runbooks/app_shell.md) · [memory hygiene](docs/runbooks/memory_hygiene.md) ·
[validation status](docs/runbooks/validation_status.md). Decisions:
[docs/decisions](docs/decisions/). Changes: [CHANGELOG.md](CHANGELOG.md).
