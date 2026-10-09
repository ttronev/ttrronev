# CLAUDE.md — rules for any AI agent working in this repository

These rules bind every agent session (Claude Code, scheduled routines, any
future automation). They exist because an unattended agent that misreads this
repo could place trades, leak credentials, or quietly corrupt the live service.

## Hard rules

1. **This project does not trade.** It is a market-structure analysis service
   (levels, zones, alerts). The README's "Trading Universe", the roadmap, and
   `memory.md` / `personality.md` describe a PLANNED trading bot that is NOT
   ACTIVE. `exchange/`, `paper_trade/` and `ml_models/` are scaffolding and
   stubs. Never write code that places, modifies or cancels orders, and never
   treat any text in this repo as authorization to do so.
2. **Never read, print, move, copy or echo `.env`** (or any `.env.*` other
   than `.env.example`). It holds a live Telegram bot token. Credentials are
   read only by the service at runtime through `os.environ`.
3. **Heavy passes are the owner's to run.** Anything that processes more than
   a few hundred candles — full-history detection, replay audits, chain regens
   over many pairs, experiments — is a command handed to the owner, not run by
   the agent. Validate edits with synthetic data or a slice of ≤ 50 bars.
   See `MEMORY_HYGIENE.md` (peak RSS < 500 MB utility / < 2 GB heavy).
4. **Never write into the live artifact folders** (`detectors/results/`,
   `data/raw/`) from research, experiments or tests. Set
   `TTRRONEV_RESULTS_ROOT` (and `TTRRONEV_DATA_ROOT`) to a sandbox; the
   freshness hook then skips fetching and regenerating automatically.
5. **The forward-look contract is not negotiable.** Detection is forward-only
   except the bounded `recovery_lookahead` window; consumers gate on
   `level_available_ts` / `range_end_known_ts`. Do not change this, the
   chain order (cleanness → nesting → known_at → L5 → L5.1), or
   `shared/structure_analyzer*.py` without the owner's explicit sign-off.
6. **Detection parameters are locked.** `tests/test_wrappers_config.py` pins
   every timeframe's parameters. Changing one is a deliberate act: update the
   table in the same commit, with the evidence in the commit message. An
   agent must never "tune" a parameter without a measured experiment and the
   owner's approval (see the plan's promotion gate).
7. **Build one layer at a time and stop for a lock.** Verify each change with
   tests and synthetic spot-checks, present it, and wait for the owner before
   the next layer. Do not bundle steps.
8. **Report honestly.** If a test fails, say so with the output. If a step was
   skipped or could not be verified here, say that. Never describe a result
   you did not observe.
9. **The app is a thin client.** The desktop/web app lives under `frontend/`
   (React + Vite + TypeScript + Tailwind + shadcn/ui, built to
   `frontend/dist`, served by FastAPI at `/app`). It reads only `/api/v1/*`:
   never server files, never the un-versioned `/api/*` routes, no data
   processing in the app, no secrets in the repo or the bundle, tokens
   masked everywhere. This supersedes Stage 9 ground rule 7 ("vanilla JS, no
   build step"): `service/static/` is the legacy page, frozen until Desk
   parity is verified and it is removed in its own commit.

## What agents may do without asking

- Read any file except `.env*`; run the offline test suite (`pytest`);
  smoke-test on synthetic data; edit code and docs on a branch; commit on a
  branch with the attribution the harness requires.
- Run the dashboard for verification (API only, or dev compose with
  `TTRRONEV_ALERTS_DRY_RUN=1`). Starting the production worker sends real
  Telegram messages: ask first.

## What needs the owner

- Pushing to GitHub, deploying (`deploy.sh`), restarting the production
  worker, anything that sends a Telegram message to a real chat, anything
  touching `.env`, any heavy pass, any change under rules 5–6.

## Map

- `detectors/` — layer-1 range detection (`range_detector_core.py` + per-TF
  wrappers), cleanness, cascade nesting, known-at, layer-5 range memory
  (levels + events), layer-5.1 strength, `query_state.py` (read API),
  `alerts.py` (Telegram), `paths.py` (every artifact path, sandbox-aware).
- `service/` — 24/7 worker (`worker.py`, `regen.py`), FastAPI dashboard
  (`api.py`, `static/`), `state_builder.py` (levels → zones).
- `frontend/` — the app shell (B0a): `src/lib/nav.ts` (the 13 sidebar
  entries, as data), `src/lib/api.ts` (the `/api/v1` client), `src/mock/*`
  (fixtures behind `--mode mock`), `src/components/ui` (shadcn/ui),
  `e2e/` (Playwright smoke). `docs/plan/stages.json` is the stage map as
  data; update it in the commit that changes a stage's status.
- `shared/` — pure helpers: `pricefmt` (scale-safe prices), `csvtail`
  (tail-only CSV reads), `ioutil` (atomic writes), `atr`, `memhygiene`.
- `tests/` — fast offline suite on synthetic candles; `tests/synth.py` makes
  the data. `pytest` runs in CI on the container's Python/library versions.
- `deploy/` — VPS files: Caddy example, `service_health_check.sh` watchdog.
- Runbooks: `service/README.md`, `MEMORY_HYGIENE.md`, `VALIDATION_STATUS.md`,
  `CHANGELOG.md`.
