# App shell (B0) — how to run, settings, build, known limits

The app is a thin client over the service's `/api/v1`. It does no data
processing: the service computes, the app shows. This document is completed
layer by layer during B0a; the status table at the end says what is live.

## Run

Prerequisites: Node 22+ (24 used in development), the Python service.

```bash
cd frontend && npm ci
```

| Goal | Command | Opens |
|---|---|---|
| Dev server against the local Docker stack (8090) | `npm run dev` | http://127.0.0.1:5173/app/ |
| Dev server on fixtures, no service needed | `npm run dev:mock` | http://127.0.0.1:5173/app/ |
| Production build (served by FastAPI) | `npm run build` → `frontend/dist` | http://localhost:8090/app/ |
| Mock build for the smoke test | `npm run build:mock` → `frontend/dist-mock` | `npm run preview:mock` |

Under the dev server `/api` is proxied to `VITE_SERVICE_URL`
(default `http://127.0.0.1:8090`). When FastAPI serves the build the service
is the page's own origin. The old dashboard stays at `/` until Desk parity is
verified; the shell is at `/app`.

Mock mode is the `VITE_MOCK=1` flag; `--mode mock` sets it from the npm
scripts (works on Windows). In mock mode the client never calls the network;
every endpoint resolves from `src/mock/api.ts`, and the top bar shows a
"mock mode" pill. Independently of that flag, pages whose data arrives in a
later stage always carry the banner
`Mock data — real data arrives in stage Bx — status: <from stages.json>`.

## The API the shell reads: `/api/v1`

Server side: `service/api_v1.py`, mounted by `service/api.py`. The client:
`frontend/src/lib/api.ts`. Every response carries `version` ("1") and
`generated_at` (ISO, UTC). List payloads are wrapped so the envelope has a
home.

| Route | Returns |
|---|---|
| `GET /api/v1/health` | `status` ok / degraded / down, `worker_alive`, `worker_phase`, `auth_enabled`, per-timeframe `tfs`, `pairs_ready`, `pairs_total`, `pairs_stale`, `cycle_5m_s`, `rss_mb`, thresholds |
| `GET /api/v1/pairs` | `pairs: [{pair, status, tfs_ready, added_ts, error_reason}]` |
| `GET /api/v1/state/{pair}` | `state`: the legacy `/api/state` body, shape unchanged |
| `GET /api/v1/candles/{pair}/{tf}?limit=500&before=` | `candles: [[t, o, h, l, c, v], ...]`, `has_more`; `limit` clamped to 1500 |
| `GET /api/v1/logs/tail?lines=500` | `lines`, `file`, `size_bytes`, `truncated`; clamped to 2000; only `lines` is accepted |
| `GET /api/v1/build/stages` | `docs/plan/stages.json` |
| `GET /api/v1/version` | `git_commit`, `built_at`, `app_version` |

Health thresholds are the legacy ones: a pair is stale when its 5m stamp is
older than 15 min (`degraded`, listed in `pairs_stale`); past 5 min the
status is `degraded` too (the dashboard's yellow); a worker heartbeat older
than 15 min is `down`; `worker_phase: startup` is `degraded`.

A pair id is `BASE_QUOTE`; anything path-like in a parameter is a 422.
Unknown pairs are 404.

**Auth.** With `TTRRONEV_API_KEY` set on the service, every `/api/v1`
request carries `X-API-Key` or gets 401. Unset means dev mode: auth off, and
the shell shows the persistent "dev mode, no auth" banner from
`health.auth_enabled`. The token is never logged and never appears in a
response. The un-versioned `/api/*` routes and the pages are not gated.

**Worker log.** The worker mirrors its stdout into `worker.log` under the
results root (one rotation past 20 MB). That file is the only thing
`/api/v1/logs/tail` serves.

## Settings

B0a runs in the browser, so Settings keeps the service URL and the API token
in `localStorage` (`ttrronev.serviceUrl`, `ttrronev.apiToken`), masked in
the UI. The `%LOCALAPPDATA%\ttrronev\settings.json` file belongs to the
pywebview window (`app/`), deferred past B0b.

## Stage map

`docs/plan/stages.json` is the stage map as data (schema:
`docs/plan/stages.schema.json`; the committed id list the tests compare
against: `docs/plan/stage_ids.json`). The build bundles a snapshot of it for
the banners; the Build page reads the live copy through
`/api/v1/build/stages`. Change a stage's status in the same commit as the
work that changes it.

## Tests

| What | Command |
|---|---|
| Unit (vitest, jsdom) | `cd frontend && npm test` |
| Smoke (Playwright, Chromium, mock build) | `cd frontend && npx playwright install chromium && npm run test:e2e` |
| Service side (routes, auth, stages.json) | `pytest tests/test_api_v1.py tests/test_app_shell.py tests/test_stages.py` |

## Build and deploy

Layer 5 of B0a: multi-stage Dockerfile (node stage builds `frontend/dist`,
copied into the Python image) and CI. Until then a local `npm run build`
followed by `docker compose build api && docker compose up -d --no-deps api`
puts the shell on the Docker stack.

## Known limits

- B0a: browser only; no pywebview window, no PyInstaller build (after B0b).
- The mock banner shows the stage status as of the build, not live.
- `cycle_5m_s` and `rss_mb` are `null` until a worker built from this code
  has completed a 5m pass.

## Status of B0a layers

| Layer | Content | Status |
|---|---|---|
| 1 | Scaffold: sidebar (13 entries), router, mock mode, banners, stages.json, `/app` route, tests | done |
| 2 | `/api/v1` router, auth, dev-mode banner, worker log file | done |
| 3 | Desk port (Lightweight Charts) | next |
| 4 | Health, Logs, Build, Settings live; planned pages on mock data | |
| 5 | Dockerfile node stage, CI, docs, DoD report | |
