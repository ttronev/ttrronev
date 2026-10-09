# R0 handoff — Repository reorganization

Part 1 is context and the decisions the ТЗ leaves open; part 2 is ТЗ-R0 verbatim from the master plan.

## 1. Context for this task

- Build-order step 1. R0 runs before any Track B code lands, so that B1 (exchange/), B2 (shared/volume_profile.py) and the rest arrive into the final layout. B1's brief is already in docs/plan/B1_handoff.md; do not start it.
- Prerequisite: app-shell-b0 (head a0056ee) merged into main. As of 2026-10-09 it is not. If main does not contain a0056ee when you start, merge app-shell-b0 into main locally with a merge commit (no squash, no rebase), run python -m pytest and npm test in frontend/, and tell the owner to push main. Then branch repo-reorg-r0 off that main.
- Protected core: detectors/ is never modified — not even a comment or a stale path string. Byte-identical is the acceptance criterion, proven by a SHA-256 manifest (see §4). The three places in detectors/ that still name old paths (layer4_signals_*.py and replay_validate.py write under backtesting/results/; docstrings mention MEMORY_HYGIENE.md and service/README.md) stay exactly as they are; §3 says how to handle the output path.
- Moves only. The diff must read as renames plus the import, path, ignore and config lines the renames force, plus the documents the ТЗ names. No refactor, no lint fixing, no behaviour change. If a file does not appear in the moves table (ТЗ §4) or in §2 below, it stays where it is; if you think something else should move, list it in your opening plan and wait.
- Rules that still bind (CLAUDE.md): never read, print, move or copy .env (it is untracked and stays at the root — compose reads it there); no pushes (the owner pushes); no heavy passes; starting the production worker sends real Telegram messages, so compose checks run through docker-compose.override.dev.yml with TTRRONEV_ALERTS_DRY_RUN=1, and the final docker compose up -d on the live laptop stack is the owner's step.
- Work one layer at a time with a lock between layers (§5): run that layer's check, commit on the branch, then start the next. The branch is squash-merged into main so the ТЗ's rollback ("one commit, git revert") holds; git log --follow works after a squash because git detects renames from content, not from commit metadata.

## 2. Decisions the ТЗ leaves open — settled here

| Item | Decision |
|---|---|
| requirements.txt (root; unpinned ccxt / pybit / backtrader / torch / flask — the archived stack) | git mv to archive/requirements.txt. The live stack is requirements-service.txt + requirements-dev.txt. If research/backtesting needs anything beyond those two files, say which modules and why in the report; do not add a requirements file for it in R0. |
| deploy/health_check.sh, deploy/health_check.cron, deploy/paper_trade.service (the paper-trade era watchdog and unit; health_check.sh checks /opt/ttrronev/paper_trade/...) | git mv to archive/paper_trade/deploy/. deploy/ keeps deploy.sh (moved in from the root), Caddyfile.example, service_health_check.sh, service_health_check.cron. |
| docs/app_shell.md (B0a notes) | git mv to docs/runbooks/app_shell.md. |
| service/test_sharding.py, service/test_zones.py | Stay in service/ (they also run as python -m service.test_zones). pytest.ini keeps testpaths = tests service. Only tests/test_*.py move. |
| tests/conftest.py, tests/synth.py | Stay at tests/ (shared by every package's tests). |
| scripts/pack_source.ps1 | Already in scripts/; only its $nestedSkip paths change (§3). |
| Runbook file names | Lowercase, as the layout shows: docs/runbooks/service.md, memory_hygiene.md, validation_status.md. A rename inside a git mv keeps history. Frozen docstrings in detectors/ keep saying MEMORY_HYGIENE.md; that is accepted. |
| data/, detectors/, frontend/, service/, shared/, .github/, .claude/, .env.example | Do not move. |
| R0's track in stages.json | "B" (the schema allows one letter; the plan says "both"; R0 is step 1 of the Track B build order). Schema unchanged. |
| Where the manifest lives | scripts/detectors_manifest.sha256, written once before the first move, verified by scripts/check_layout.py and by a new CI step (§4). |
| docs/decisions/ and docs/runbooks/deploy.md | New files, text given in §6 — paste, do not compose. |
| Makefile on Windows | Every target is one docker compose / npm --prefix frontend / python -m line that can be copied into PowerShell when make is absent. dev = the dev compose stack (API with dry-run alerts); dev-ui = npm --prefix frontend run dev. |

## 3. Traps found while reading the branch — handle each explicitly

1. Tests compute the repo root from __file__. tests/test_stages.py uses Path(__file__).resolve().parents[1]; after the move to tests/service/ that must be parents[2]. Grep every moved test for parents[ and __file__ and fix the depth. Add an empty __init__.py to tests/ and to each tests/<package>/ so pytest's default import mode puts the repo root on sys.path for every module and from tests.synth import … keeps working under both pytest and python -m pytest.
2. tests/test_stages.py::test_in_progress_stages_are_what_the_shell_claims asserts ["B0"]. After §7 the in-progress stage is R0 and B0 is in_review; change the expectation to ["R0"] (the comment says the list is what the current branch builds). Count stays 196 — a changed expectation, not a removed test.
3. CI names test files by path. .github/workflows/tests.yml runs ruff check service/api_v1.py tests/test_api_v1.py tests/test_app_shell.py tests/test_stages.py tests/test_worker_log.py and mypy service/api_v1.py; those paths become tests/service/…. ТЗ rule 6 asks for ruff and mypy on service/, shared/, exchange/ — try it; measured on this branch the wider scope is not clean (ruff: dozens of findings in service/ and shared/; mypy: 7 errors in 3 files), and fixing them is not R0. So: keep today's file set at its new paths, add exchange/ (empty package), and report the exact counts of the wider run in the report. Widening is a later task.
4. Docker ignore semantics. The service stage does COPY . . and the node stage does COPY docs/plan /src/docs/plan. To exclude docs/ except the plan, write docs/* then !docs/plan — docs/ followed by !docs/plan does not re-include. Replace backtesting/results, paper_trade/data, paper_trade/logs, ml_models and the four parity/trade-log lines with: archive, research, tests, scripts, docs/*, !docs/plan, backtesting/results (see trap 6). Keep the frontend lines.
5. .gitignore paths move with the trees: /backtesting/results/ → add /research/backtesting/results/ (keep the old line, trap 6); /paper_trade/data/, /paper_trade/logs/ → /archive/paper_trade/data/, /archive/paper_trade/logs/; /ml_models/models/ and its ! line → /archive/ml_models/models/ (or drop both — nothing under ml_models/models/ is tracked).
6. Frozen scripts in detectors/ write to backtesting/results/ at the repo root (layer4_signals_range_to_range.py:117, layer4_signals_retest_fail.py:334, replay_validate.py:250). They are owner-run research passes and cannot be edited. Keep /backtesting/results/ in .gitignore and in .dockerignore, and add one paragraph to docs/runbooks/validation_status.md ("Known paths"): these three write under backtesting/results/ at the root until the detector is next changed through the A3 gate; move their output into research/backtesting/results/ by hand.
7. scripts/pack_source.ps1 skips \backtesting\results\, \paper_trade\data\, \paper_trade\logs\ by path fragment; update to the new fragments and add \archive\ only if the owner wants the archive out of the source pack (default: keep it in — it is small).
8. Archived code references the old layout (archive/paper_trade/level_proximity.py imports backtesting.level_features; MEMORY_HYGIENE.md says so). Do not fix it. archive/README.md states: frozen, not importable or runnable as-is, imports name the pre-R0 layout; see git history before the R0 commit to run any of it.
9. Comment-only path mentions outside detectors/ (shared/structure_analyzer.py:38 → archive/paper_trade/REFACTOR_C1_DESIGN.md; pytest.ini header; MEMORY_HYGIENE.md, VALIDATION_STATUS.md bodies; CLAUDE.md rule 1's mention of exchange/, paper_trade/, ml_models/) — update the path text, nothing else. CLAUDE.md rules keep their substance; only path mentions and the Map section change.
10. exchange/ has no __init__.py today (two stub files). After git mv exchange/*.py archive/exchange_stubs/, create exchange/__init__.py with a one-line docstring ("Hyperliquid client — B1") so exchange is an importable package and a ruff/mypy target; create tests/exchange/__init__.py (empty).
11. The four root files avax_parity.log, link_parity.log, sol_parity.log, sol_3rr_trade_log.csv: git mv into research/backtesting/results/, then git rm --cached them so the tree stops tracking them (they stay on disk, gitignored; history keeps them). This is the one sanctioned departure from "git mv only", and the ТЗ's moves table asks for it.
12. docs/plan/ does not move — detectors/paths.py::stages_json(), the Dockerfile node stage and the Vite @plan alias (frontend/vite.config.ts, tsconfig.app.json) all point at it.

## 4. Manifest and scripts/check_layout.py

Write scripts/check_layout.py (standard library only, runs on Windows and Linux) with two modes:

- python scripts/check_layout.py --write-manifest — run once, before the first git mv, on the clean main checkout: SHA-256 of every git-tracked file under detectors/ (git ls-files -z detectors; detectors/results/ is untracked runtime data and is not covered), one "<sha256>  <posix path>" line per file, sorted by path, written to scripts/detectors_manifest.sha256. Commit it in the first layer. It is regenerated only on the owner's explicit instruction when the detector changes through the A3 gate.
- python scripts/check_layout.py (default) — recomputes the manifest and compares; checks every "From" path of the moves table (ТЗ §4) and of §2 is absent and every "To" path present; checks exchange/__init__.py, archive/README.md, docs/runbooks/service.md, docs/agent/memory.md, docs/agent/personality.md, deploy/deploy.sh exist; greps service shared detectors exchange data for import|from (paper_trade|backtesting|archive|research); prints layout ok and exits 0, or lists every failure and exits 1.

Add python scripts/check_layout.py as a step in the tests job of .github/workflows/tests.yml right after pytest. From this commit on, CI fails if any byte under detectors/ changes — that is the point: the level tracker is the project's most valuable asset.

## 5. Layers (lock between each: check green, commit, then continue)

| Layer | Work | Check |
|---|---|---|
| L0 | Branch repo-reorg-r0; check_layout.py --write-manifest; commit the script + manifest | pytest 196, vitest 69 (baseline numbers recorded in the report) |
| L1 | Every git mv in ТЗ §4 and §2; exchange/__init__.py, tests/*/__init__.py; trap 11 | git status shows renames only (git diff --cached -M --stat) |
| L2 | Import and path fixes the moves force (traps 1, 7, 9); .gitignore, .dockerignore (traps 4–6); pytest.ini comments and addopts = -q --ignore=archive --ignore=research; CI paths (trap 3) + the check_layout.py step | python -m pytest = 196; ruff + mypy on the CI set clean; docker compose config --quiet |
| L3 | README.md (map + quickstart, ≤ 60 lines), CLAUDE.md Map + path mentions, archive/README.md, docs/decisions/*, docs/runbooks/deploy.md, the one-line pointer service/README.md → docs/runbooks/service.md, Makefile, CHANGELOG entry | every relative link in README, CLAUDE.md and the runbooks resolves (script or by hand, listed in the report) |
| L4 | docs/plan/stages.json and stage_ids.json from §7; trap 2 | pytest 196; vitest 69; npm run build in frontend/ |
| L5 | docker compose build (image not larger than before — record both sizes); dev stack up with dry-run alerts: dashboard and /app serve; check_layout.py → layout ok; git log --follow on research/backtesting/level_features.py, docs/runbooks/service.md, tests/service/test_api_v1.py | the report in §8 |

## 6. Text for the new documents (paste; do not compose)

docs/decisions/hyperliquid.md

2026-10-05 — Execution venue. Hyperliquid and its API; Bybit dropped. OKX remains the deep-history source for the detector. Paper trading first, on Hyperliquid testnet, through an API agent wallet that can trade but never withdraw.

docs/decisions/thin-client.md

2026-10-05/07 — Thin client. The engine runs on the server; the app is a client. The .exe is a thin launcher with no engine inside: the UI is served by the service at /app, so UI changes redeploy the service, not the .exe; the launcher points at whichever service URL is in Settings. Any multi-user product runs on a Claude Platform API key with per-user token metering — never on the personal subscription and never with a key inside the app.

docs/decisions/protected-core.md

after B0a — Protected core. The range and level detector (detectors/) is never modified by a stage. R0 adds a SHA-256 manifest (scripts/detectors_manifest.sha256, checked by scripts/check_layout.py in CI); a golden-artifact test joins it from B3. Detector parameters are locked (pinned-table test). Detector changes happen only as named challengers through the A3 gate, with the owner's sign-off.

docs/decisions/frontend-stack.md

2026-10-06 — Frontend stack. React + Vite + TypeScript + Tailwind with shadcn/ui under frontend/, built to dist/ by the image's node stage and served by FastAPI; Lightweight Charts kept for charts. The design pass (B0b) is done in Claude Design from the public repo; its handoff bundle is applied by Claude Code.

docs/runbooks/deploy.md

Deploy files. deploy/deploy.sh builds the image with the git commit stamped and restarts the stack; deploy/Caddyfile.example is the reverse proxy; deploy/service_health_check.sh + .cron is the host watchdog (/api/health). Procedure: docs/runbooks/service.md, section Prod. Secrets: only .env.example is in git; .env lives on the host.

archive/README.md — one paragraph per folder, then the frozen notice from trap 8:

paper_trade/ — Phase 1a shadow tracker (May 2026), Binance stream, SQLite store, parity drills; its ideas return as the ledger (A1), the journal (B4) and paper execution (B7). ml_models/ — online learner and self-evolving optimizer scaffolds; superseded by the scorecard and gate (A2, A3). openclaw/ — bridge to an external agent runtime; superseded by the in-app agent (B8). strategies/ — the first strategy notes and log; superseded by the strategy spec (B11). exchange_stubs/ — Bybit client and paper-trading stubs; superseded by Hyperliquid (B1, B7). requirements.txt — the archived stack's dependencies.

## 7. docs/plan/stages.json — the complete stage map

Replace the 13-entry file with all 23 stages below; stage_ids.json lists the same 23 ids. Names and dependencies are the master plan's Stage map. updated = the commit date; evidence_url null except P0.

| id | name | track | depends_on | status |
|---|---|---|---|---|
| P0 | Baseline: Stage 9a committed, token rotated | A | — | done (evidence https://github.com/ttronev/ttrronev/commit/dbbc762) |
| A0 | Harden & run (0.1–0.9) | A | P0 | in_review |
| R0 | Repository reorganization | B | B0 | in_progress |
| B0 | App shell v0 | B | — | in_review |
| B0c | Desk widget board | B | B0 | not_started |
| A1 | Ledger | A | A0 | not_started |
| A2 | Scorecard | A | A1 | not_started |
| A3 | Experiments & gate | A | A2 | not_started |
| A4 | Agents | A | A3 | not_started |
| B1 | Hyperliquid data layer | B | A0 | not_started |
| B2 | Volume profile engine | B | B1 | not_started |
| B3 | Zone × profile fusion | B | B2, A0 | not_started |
| B4 | Trading journal | B | A1 | not_started |
| B5 | Candidate finders | B | B3, B4 | not_started |
| B6 | Candidate delivery | B | B5 | not_started |
| B7 | Paper execution on HL testnet | B | B6 | not_started |
| B8 | In-app agent | B | B4, A2 | not_started |
| B9 | Full app + .exe | B | B6, B8 | not_started |
| B10 | Stocks screener | B | A0 | not_started |
| B11 | Strategy spec, builder and backtest screen | B | B4, A2 | not_started |
| B12 | Bots and connections | B | B7, B11 | not_started |
| B13 | Account and multi-user | B | B9, B12 | not_started |
| B14 | Hub | B | B13, B11 | not_started |

source: "Master plan document, Stage map tab; the coding agent updates this file in the commit that changes a stage's status". The frontend's nav.ts stage references (B6, B8, B11, B12, B13) all exist in this list; nav.ts itself is not touched in R0.

## 8. When finished, report

- Manifest: check_layout.py output (layout ok), and git diff --stat main -- detectors/ (must be empty).
- pytest and vitest counts before (L0) and after (L5); CI link once the owner has pushed.
- docker compose build: image size before and after; dev stack: /api/health, / and /app responses.
- The grep from ТЗ §6 (empty) and the three git log --follow outputs (first and last commit each).
- Wider ruff/mypy counts (trap 3), so the lint-widening task can be sized.
- Anything you found that was not in the moves table and left in place.

Stop after R0. Next is B1 (docs/plan/B1_handoff.md), as a separate task on a new branch off the merged main.

---

# ТЗ-R0 — Repository reorganization

R0 makes the repository easy to work in before Track B starts: production code, research, archive and documentation each get one place; the protected core stays exactly where it is; nothing changes behaviour. Moves only, plus the configuration the moves force.

## 1. Goal

After R0 the root holds configuration files and three documents; every top-level directory has one purpose; pytest (196), vitest (69), the Docker build and the running Docker stack behave exactly as before; every file under detectors/ has the same SHA-256 as before.

## 2. Scope

In scope: git mv per the target layout; the import and path updates those moves force; .dockerignore; CI and lint scope; pytest.ini test paths; a README map; a Makefile; an archive README; the stage map fix below. Out of scope: any refactor; renaming any Python package production imports (detectors, shared, service, exchange keep their names and locations); deleting history (nothing is removed from git, only moved).

## 3. Target layout

```
ttrronev/
├── README.md               what it is, how to run, the map (≤ 60 lines)
├── CLAUDE.md               agent rules (stays at root)
├── CHANGELOG.md
├── Makefile                dev, test, build, up, down, logs, clean
├── Dockerfile · docker-compose.yml · docker-compose.override.dev.yml · .dockerignore
├── pytest.ini · ruff.toml · mypy.ini · requirements-service.txt · requirements-dev.txt
├── detectors/              PROTECTED CORE — unchanged, byte for byte
├── shared/                 pure helpers (pricefmt, csvtail, ioutil, atr, memhygiene, structure_analyzer); later volume_profile, profile_fusion
├── service/                worker, regen, api, api_v1, state_builder, static (old dashboard until Desk parity)
├── exchange/               Hyperliquid client (B1); later the execution adapter (B7)
├── frontend/               the React app (B0a)
├── data/                   bootstrap and freshness code only; data/raw stays gitignored
├── deploy/                 deploy.sh, Caddyfile.example, service_health_check.sh, systemd units
├── docs/
│   ├── plan/               stages.json, stage_ids.json, stages.schema.json, handoffs (B1_handoff.md …)
│   ├── runbooks/           service.md (was service/README.md), memory_hygiene.md, validation_status.md, deploy.md
│   ├── decisions/          one short file per decision: hyperliquid, thin-client, protected-core, frontend-stack
│   └── agent/              memory.md, personality.md (the in-app agent's charter, used from B8)
├── research/
│   └── backtesting/        engines, level_features, replay audits, secondary-only research; results/ gitignored
├── archive/                frozen, never imported, excluded from tests, lint and the image:
│   ├── paper_trade/        Phase 1a shadow tracker (May 2026)
│   ├── ml_models/ · openclaw/ · strategies/ · exchange_stubs/
│   └── README.md           what each folder was, why it is frozen, where its ideas went in the plan
├── scripts/                pack_source.ps1 and dev helpers
└── tests/                  pytest, mirrored by package: detectors/, service/, shared/, exchange/, frontend is tested in frontend/
```

## 4. Moves

| From | To |
|---|---|
| backtesting/ | research/backtesting/ |
| paper_trade/, ml_models/, openclaw/, strategies/ | archive/<same name>/ |
| exchange/*.py (current stubs) | archive/exchange_stubs/; exchange/ is recreated empty for B1 |
| service/README.md | docs/runbooks/service.md (a one-line pointer stays in service/) |
| MEMORY_HYGIENE.md, VALIDATION_STATUS.md | docs/runbooks/ |
| memory.md, personality.md | docs/agent/ |
| deploy.sh | deploy/deploy.sh |
| avax_parity.log, link_parity.log, sol_parity.log, sol_3rr_trade_log.csv | research/backtesting/results/ (gitignored) — removed from the tree, kept in history |
| tests/test_*.py | tests/<package>/test_*.py with the same names |

docs/plan/ does not move: detectors/paths.py and the Dockerfile reference it.

## 5. Rules

- git mv only, so git log --follow keeps every file's history.
- Nothing under detectors/ changes: a SHA-256 manifest of the directory is taken before and after and compared in the acceptance step.
- archive/ is excluded from pytest collection, ruff, mypy and the Docker image (.dockerignore), and archive/README.md says so. Nothing in production imports from it (grep proves it).
- research/ is excluded from the Docker image; its code keeps working from the repo root with python -m research.backtesting.<module> — update the few imports and the CLI lines in docs that break.
- .dockerignore also excludes frontend/node_modules, tests/, .git/, docs/ except docs/plan/.
- CI: pytest from the new testpaths; ruff and mypy on service/, shared/, exchange/ only; the frontend job unchanged.
- CLAUDE.md: the Map section rewritten for the new layout; rules unchanged.
- README.md: rewritten as the map plus quickstart, under 60 lines, linking to the runbooks.
- Makefile targets: dev (vite dev + API), test (pytest + vitest), build (frontend + image), up, down, logs, clean.
- docs/plan/stages.json and stage_ids.json list all stages of the master plan (P0, A0–A4, B0, B0c, B1–B14, R0) with their current statuses; the B0a build shipped 13 of them.

## 6. Tests and acceptance

- pytest reports the same count as before the move (196) and vitest the same (69); CI green on the branch.
- docker compose build succeeds; the image is not larger than before; docker compose up -d with the existing .env reaches healthy and the dashboard and /app serve.
- SHA-256 manifest of detectors/ identical before and after.
- grep -r "import paper_trade\|from paper_trade\|import backtesting\|from backtesting" --include=*.py service shared detectors exchange returns nothing.
- git log --follow on three moved files shows their old history.
- Acceptance command: make test && make build green, then python scripts/check_layout.py prints layout ok (it checks the moves table and the detectors manifest).

## 7. Review checklist

- Moves only; no logic edited (diff shows renames plus import/path lines)
- detectors/ manifest identical
- archive excluded from tests, lint, image; nothing imports it
- README map, CLAUDE.md map, runbook links all resolve
- CHANGELOG entry

## 8. Rollback

One commit; git revert restores the old layout exactly.
