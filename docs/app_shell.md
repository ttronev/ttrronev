# App shell (B0a) — how to run, settings, build, known limits

The app is a thin client over the service's `/api/v1`. It does no data
processing: the service computes, the app shows. B0a delivered the
structure: every screen clickable, five of them live, the rest on mock
data under a banner that names the stage bringing their real data.

## Run

Prerequisites: Node 22+ (24 used in development) for the dev server and
tests; Docker for the stack. The service image builds the app itself.

```bash
cd frontend && npm ci
```

| Goal | Command | Opens |
|---|---|---|
| The stack (service + app) | `docker compose build && docker compose up -d` | http://localhost:8090/app/ (port from `TTRRONEV_PORT`, default 8000) |
| Dev server against the local Docker stack | `npm run dev` | http://127.0.0.1:5173/app/ |
| Dev server on fixtures, no service needed | `npm run dev:mock` | http://127.0.0.1:5173/app/ |
| Production build (served by FastAPI) | `npm run build` → `frontend/dist` | http://localhost:8090/app/ |
| Mock build for the smoke test | `npm run build:mock` → `frontend/dist-mock` | `npm run preview:mock` |

Under the dev server `/api` is proxied to `VITE_SERVICE_URL`
(default `http://127.0.0.1:8090`). When FastAPI serves the build the service
is the page's own origin. The old dashboard stays at `/` until the owner
signs off Desk parity; it is then removed in its own commit and the shell
moves to `/`.

Mock mode is the `VITE_MOCK=1` flag; `--mode mock` sets it from the npm
scripts (works on Windows). In mock mode the client never calls the network;
every endpoint resolves from `src/mock/api.ts` (+ `src/mock/planned.ts` for
the planned pages), and the top bar shows a "mock mode" pill. Independently
of that flag, pages whose data arrives in a later stage always carry the
banner `Mock data — real data arrives in stage Bx — status: <from
stages.json>`.

## Screens

| Entry | B0a | Data |
|---|---|---|
| Desk | live | the legacy dashboard ported: chart (Lightweight Charts 4.2.0), zones as bands, active ranges, 7-day markers, toggles, pair registry with add-pair, live price with seconds, nearest levels |
| Strategies, Backtests | B11 | mock: lifecycle + headline numbers; run form + runs with config hash and commit |
| Bots, Connections | B12 | mock: modes, status, pause/kill; permissions, test connection |
| Live log | B6 | mock: event stream with bot / pair / type filters |
| Chat | B8 | mock: thread with screen context |
| Account | B13 | mock: profile, channels, masked tokens, LLM usage, export |
| Health | live | service verdict (ok / degraded / down), worker, last 5m cycle, peak RSS, API latency, heartbeat per TF, pairs and bootstrap state; 15 s |
| Logs | live | tail of `worker.log` (≤ 2000 lines), follow, text filter; 5 s |
| Build | live | stage table + "Now" strip from `/api/v1/build/stages` (bundled snapshot as fallback) |
| Agent log | B8 | mock: proposed / ran / refused / vetoed |
| Settings | live | service URL, API token (masked, local), test connection, theme follows the system |

Disabled buttons on the mock pages carry `title="arrives in stage Bx"`.

## The API the shell reads: `/api/v1`

Server side: `service/api_v1.py`, mounted by `service/api.py`. The client:
`frontend/src/lib/api.ts`. Every response carries `version` ("1") and
`generated_at` (ISO, UTC). List payloads are wrapped so the envelope has a
home.

| Route | Returns |
|---|---|
| `GET /api/v1/health` | `status` ok / degraded / down, `worker_alive`, `worker_phase`, `auth_enabled`, per-timeframe `tfs`, `pairs_ready`, `pairs_total`, `pairs_stale`, `pair_age_5m_s`, `cycle_5m_s`, `rss_mb`, thresholds |
| `GET /api/v1/pairs` | `pairs: [{pair, status, tfs_ready, added_ts, error_reason}]` |
| `POST /api/v1/pairs` | `{symbol}` or `{symbols}` → `queued`, `rejected` (the legacy registry contract) |
| `DELETE /api/v1/pairs/{pair}` | `removed` (disk untouched) |
| `GET /api/v1/state/{pair}` | `state`: the legacy `/api/state` body, shape unchanged |
| `GET /api/v1/candles/{pair}/{tf}?limit=500&before=` | `candles: [[t, o, h, l, c, v], ...]`, `has_more`; `limit` clamped to 1500 |
| `GET /api/v1/logs/tail?lines=500` | `lines`, `file`, `size_bytes`, `truncated`; clamped to 2000; only `lines` is accepted |
| `GET /api/v1/build/stages` | `docs/plan/stages.json` |
| `GET /api/v1/version` | `git_commit`, `built_at`, `app_version` (stamped by the image build) |

Health thresholds are the legacy ones: a pair is stale when its 5m stamp is
older than 15 min (`degraded`, listed in `pairs_stale`); past 5 min the
status is `degraded` too (the dashboard's yellow); a worker heartbeat older
than 15 min is `down`; `worker_phase: startup` is `degraded`. Measured on
the laptop: health, state and pairs answer in 7–30 ms.

A pair id is `BASE_QUOTE`; anything path-like in a parameter is a 422.
Unknown pairs are 404.

**Auth.** With `TTRRONEV_API_KEY` set on the service, every `/api/v1`
request carries `X-API-Key` or gets 401. Unset means dev mode: auth off, and
the shell shows the persistent "dev mode, no auth" banner from
`health.auth_enabled`. The token is never logged and never appears in a
response. The un-versioned `/api/*` routes and the pages are not gated.

**Worker log.** The worker mirrors its stdout into `worker.log` under the
results root (one rotation past 20 MB), only when running as the worker
process. That file is the only thing `/api/v1/logs/tail` serves.

## Settings

B0a runs in the browser, so Settings keeps the service URL and the API token
in `localStorage` (`ttrronev.serviceUrl`, `ttrronev.apiToken`), masked in
the UI. The `%LOCALAPPDATA%\ttrronev\settings.json` file belongs to the
pywebview window (`app/`), deferred past B0b. Pointing the URL at another
origin (the VPS through Caddy) needs CORS on the service: not enabled yet.

## Stage map

`docs/plan/stages.json` is the stage map as data (schema:
`docs/plan/stages.schema.json`; the committed id list the tests compare
against: `docs/plan/stage_ids.json`). The build bundles a snapshot of it for
the banners; the Build page reads the live copy through
`/api/v1/build/stages`. Change a stage's status in the same commit as the
work that changes it. The owner's master plan document is the source of
truth; the stage names here are the shell's reading of it.

## Tests

| What | Command |
|---|---|
| Unit (vitest, jsdom) | `cd frontend && npm test` |
| Smoke (Playwright, Chromium, mock build) | `cd frontend && npx playwright install chromium && npm run test:e2e` |
| Service side (routes, auth, stages.json, worker log) | `pytest tests/test_api_v1.py tests/test_app_shell.py tests/test_stages.py tests/test_worker_log.py` |
| Static | `ruff check service/api_v1.py` · `mypy service/api_v1.py` |

CI (`.github/workflows/tests.yml`) runs all of the above plus a docker
build of the image.

## Build and deploy

The Dockerfile's node stage builds `frontend/dist` and the Python stage
copies it in; no npm on the host. Build args `GIT_COMMIT` and `BUILT_AT`
stamp `/api/v1/version` (`deploy.sh` passes them; a plain
`docker compose build` reports "unknown"). After a pull:

```bash
docker compose build && docker compose up -d
```

## Known limits

- B0a: browser only; no pywebview window, no PyInstaller build (after B0b).
- The mock banner shows the stage status as of the build, not live.
- `cycle_5m_s`, `rss_mb` and the Logs page are empty until a worker built
  from this code has run (restart the worker after the rebuild).
- Labels on the Desk are the legacy page's (Russian) for parity; the design
  pass (B0b) decides the language and the look.

## Parity evidence (Desk vs the legacy page)

Checked on 2026-10-07 on the laptop service with live SOL_USDT data, 1h
view, same browser profile: both pages rendered 13 zones with identical
prices, bands and confluence (`window.__zonesRendered`), the same 13
nearest-level rows in the same order, the same live price, health dot and
footer status, and 7 chart canvases each. The old page is removed in its own
commit once the owner signs this off.
