# ttrronev: each recipe line is one command you can paste into PowerShell when `make` is absent.
# PowerShell 5.1 has no `&&`: run the lines of a target one after the other (or join them with `;`).
# Production `up` sends real Telegram alerts when .env holds the token; `dev` prints them instead.
.PHONY: dev dev-ui test build up down logs clean

dev:          ## dev compose stack: bind mounts, API --reload, alerts printed not sent
	docker compose -f docker-compose.yml -f docker-compose.override.dev.yml up

dev-ui:       ## the Vite dev server for the app (proxies /api to the local stack on 8090)
	npm --prefix frontend run dev

test:         ## pytest (synthetic, offline), then vitest
	python -m pytest
	npm --prefix frontend test

build:        ## frontend typecheck + build, then the service image (its node stage builds the shell again)
	npm --prefix frontend run build
	docker compose build

up:           ## production stack
	docker compose up -d

down:
	docker compose down

logs:
	docker compose logs -f --tail=200

clean:        ## build outputs and caches only; never data/raw or detectors/results
	python -c "import shutil; [shutil.rmtree(p, ignore_errors=True) for p in ('frontend/dist', 'frontend/dist-mock', 'frontend/playwright-report', 'frontend/test-results', '.pytest_cache', '.mypy_cache', '.ruff_cache')]"
