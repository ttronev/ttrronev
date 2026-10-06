# ttrronev-service — 24/7 structure analysis + web dashboard

A read-only analysis service over the validated detector stack (L1 ranges →
range memory → strength). It keeps candles fresh from OKX, regenerates the
detectors on every bar close, and serves market state (trend, regime, active
ranges, S/R levels, price position) as a dark-theme dashboard + JSON API.
**No trading anywhere in this service.**

```
worker  (python -m service.worker)   API  (uvicorn service.api:app)
   │ fetch OKX → regen detectors        │ GET /            dashboard
   │ → state.json / live.json           │ GET /api/pairs
   │ → heartbeat.json / Telegram        │ GET /api/state/{pair}
   └── writes results/{PAIR}/…  ──────► └── GET /api/health (503 = worker dead)
```

## Layout on disk (per pair, all under `detectors/results/{PAIR}/`)

| file | writer | what |
|---|---|---|
| `range_detector_{tf}_layer1.json` | worker (regen) | L1 ranges per TF |
| `range_memory_{tf}.json` | worker (chain) | L5 levels + events |
| `state.json` | worker | the API/dashboard contract |
| `live.json` | worker (15s) | `{price, ts, ok}` — display only |
| `heartbeat.json` | worker | per-TF last-regen stamps for /api/health |
| `alerts_state.json` | worker (alerts lib) | Telegram dedup (survives restarts) |

Raw candles stay flat: `data/raw/{PAIR}_{tf}.csv`.

Requires Docker Compose >= 2.24 (the compose file uses the long
`env_file: {path, required: false}` syntax).

## Dev (Windows, Docker Desktop)

```bash
docker compose -f docker-compose.yml -f docker-compose.override.dev.yml up
```

Code is bind-mounted; the API runs `--reload`, so API/dashboard edits are
save → F5. Worker edits: `docker compose restart worker`. In dev the worker
prints alerts instead of DMing Telegram (`TTRRONEV_ALERTS_DRY_RUN=1` in the
override). If the repo is inside OneDrive, move it out (or into WSL2) —
bind mounts misbehave under OneDrive.

Without Docker: `python -m service.worker` and
`uvicorn service.api:app --port 8000` in two terminals.

## Prod (Linux VPS)

One-time setup:

1. `git clone <repo> /opt/ttrronev`
2. Create `/opt/ttrronev/.env` (chmod 600, never in git) with
   `TTRRONEV_TG_BOT_TOKEN` / `TTRRONEV_TG_CHAT_ID`.
   **Reissue the bot token first** (@BotFather → /revoke) — the old one
   sat in a repo archive.
3. Data. **A fresh clone needs NO manual data seeding** (since Stage 8):
   seed pairs whose CSVs are absent are registered as `bootstrapping` and
   the worker fetches their full history from OKX on first start (~5 min
   per pair); further coins are added from the dashboard UI. Optionally
   copy `data/raw/` from another machine first
   (`scp -r data/raw/ vps:/opt/ttrronev/data/`) — that preserves
   pre-OKX-listing history (e.g. BTC's 2017-2019 Binance-era candles,
   which OKX cannot provide) and skips the initial fetch.
4. Caddy: install `deploy/Caddyfile.example` (hash via `caddy hash-password`).
5. ufw: allow 22/80/443 only. Port 8000 is already loopback-bound in compose
   (override per machine with `TTRRONEV_PORT` in `.env`; keep Caddy in step).
6. Watchdog: install `deploy/service_health_check.cron` (instructions inside).
   It checks `/api/health` from the host every 5 min and DMs you on
   API-down / worker-dead / data-stale, plus a recovery notice — independent
   of Docker, so it still reports when Docker itself is down.

Deploy / update (every time):

```bash
./deploy.sh
```

(git pull --ff-only → compose build → up -d → ps.)

## Adding a pair

1. Add the line to `PAIRS` in [service/pairs.py](pairs.py).
2. Put its CSVs in `data/raw/` (`{PAIR}_{tf}.csv` for 1w/1d/4h/2h/1h/5m).
3. `docker compose restart worker`.

No path edits anywhere — everything resolves through `detectors/paths.py`.

## Known trade-offs (accepted by design — revisit deliberately)

- **1h/2h level memory only spans the 1-year regen window** (4h: 2 years).
  Levels whose source range is older fall out of `range_memory_{tf}.json`
  after a worker regen. Long-horizon structure lives in the full-history
  1d/1w memory. If old intraday levels matter, raise the window in
  `service/pairs.py` and pay the regen cost.
- **The service's artifacts are windowed** (1h/2h/4h layer-1 files cover
  1y/1y/2y). Research needing full history must NOT run against the live
  folder — until the sandbox roots existed, a research regen and the worker
  silently overwrote each other. Run research with
  `TTRRONEV_RESULTS_ROOT=<your folder>` (and `--no-freshness`, or let the
  sandbox guard skip the fetch): it reads the same candles and writes only
  there. The layer4 scripts still print a loud coverage warning on truncated
  inputs.
- **`range_id`s renumber as a window slides** (the ordinal counts from the
  window's first bar). Alert dedup is therefore keyed on level PRICE, not
  id; treat `range_id` in `state.json` as a display label, not an identity.

## Restarts, limits, failure handling

- **Restart is cheap now.** Startup fetches every timeframe (one cheap HTTP
  call each when nothing is new) and regenerates ONLY what needs it: new
  candles, a missing/outdated artifact, or a change in the detection code
  (a fingerprint of the detector/chain sources, stored per pair in
  `regen_marker.json`). Deploying a detector change forces exactly one full
  regen; a plain restart with current data takes seconds per pair.
- **Health during startup.** The worker heartbeat starts BEFORE the startup
  regen, so `/api/health` reports `worker_alive: true` and
  `worker_phase: "startup"` immediately; per-pair `stale` flags clear as pairs
  catch up. The watchdog stays quiet for up to 45 min of startup.
- **Catch-up by data.** Each hourly cycle also checks whether any of
  1h/2h/4h/1d is behind its last closed bar (a close missed while the process
  was stalled, or a fetch that failed) and fetches it, instead of waiting for
  that timeframe's next close.
- **Quarantine.** A pair whose 5m cycle fails ~12 passes in a row WHILE other
  pairs succeed (delisted instrument, broken CSV) is set to `status: error`
  with the reason, leaves the cycles, stops holding global health red, and
  you get one DM. Re-add it from the dashboard to retry. A pass where every
  pair fails is treated as an exchange/network outage and never quarantines.
- **Memory.** Containers have `mem_limit`s (worker `TTRRONEV_WORKER_MEM`,
  default 2g — generous on purpose; api 512m). The 5m pass log prints the
  worker's peak RSS: tighten the limit once you have seen the real number.
  The 5-minute path reads only the tail of each CSV (`shared/csvtail.py`).
- **Logs** rotate (20 MB × 5 per container). Every artifact write is atomic
  (`shared/ioutil.py`): a kill mid-write leaves the previous complete file.
- **Sandboxing.** Research, experiments and tests set
  `TTRRONEV_RESULTS_ROOT` (and `TTRRONEV_DATA_ROOT`) to their own folders;
  the freshness hook then skips fetch + regen. The live service never shares
  files with a sandboxed run.
- **Tests.** `python -m pytest` — fast, offline, synthetic candles; runs in CI
  on the image's Python/pandas/numpy versions (`.github/workflows/tests.yml`).

## Operational notes

- **Regen windows** (service/pairs.py): 5m→45d, 1h/2h→1y, 4h→2y, 1d/1w→full.
  Ranges near a window's left edge are untrusted by construction; anything
  near current price is far inside. The FORWARD-LOOK CONTRACT
  (`level_available_ts`) is untouched.
- The L5 chain reruns at most hourly (levels only change when a range ends).
- The 5m cycle logs its duration and must stay < 60s (measured ~2s).
- All service JSONs are written atomically (`os.replace`) — the API never
  sees a torn file.
- Worker errors are logged + sent to Telegram, rate-limited to one DM per
  error key per hour; the loop never dies.
- `/api/health` returns 503 when the freshest 5m regen is older than 15 min.
