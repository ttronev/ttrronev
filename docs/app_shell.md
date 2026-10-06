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
| Service side (route, stages.json) | `pytest tests/test_app_shell.py tests/test_stages.py` |

## Build and deploy

Layer 5 of B0a: multi-stage Dockerfile (node stage builds `frontend/dist`,
copied into the Python image) and CI.

## Known limits

- B0a: browser only; no pywebview window, no PyInstaller build (after B0b).
- The mock banner shows the stage status as of the build, not live.

## Status of B0a layers

| Layer | Content | Status |
|---|---|---|
| 1 | Scaffold: sidebar (13 entries), router, mock mode, banners, stages.json, `/app` route, tests | done |
| 2 | `/api/v1` router + auth | next |
| 3 | Desk port (Lightweight Charts) | |
| 4 | Health, Logs, Build, Settings live; planned pages on mock data | |
| 5 | Dockerfile node stage, CI, docs, DoD report | |
