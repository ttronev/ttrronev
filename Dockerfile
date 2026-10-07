# One image for both services (worker + api); compose picks the command.
#
# Stage 1 builds the app shell (frontend/dist) with Node; stage 2 is the
# Python service with that build copied in, so `docker compose build` needs
# no local npm. Build args stamp /api/v1/version:
#   docker build --build-arg GIT_COMMIT=$(git rev-parse --short HEAD) \
#                --build-arg BUILT_AT=$(date -u +%FT%TZ) .
# (deploy.sh passes them; a plain `docker compose build` reports "unknown".)

# --- stage 1: app shell ----------------------------------------------------
FROM node:24-alpine AS frontend
WORKDIR /src/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
# The build bundles docs/plan/stages.json (the @plan alias is ../docs/plan).
COPY docs/plan /src/docs/plan
RUN npm run build

# --- stage 2: service --------------------------------------------------------
FROM python:3.12-slim
ARG GIT_COMMIT=unknown
ARG BUILT_AT=
WORKDIR /app
COPY requirements-service.txt .
RUN pip install --no-cache-dir -r requirements-service.txt
COPY . .
COPY --from=frontend /src/frontend/dist ./frontend/dist
# Unbuffered so `docker compose logs` streams the worker's progress lines;
# no bytecode so dev bind mounts don't litter the host tree with __pycache__.
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    TTRRONEV_GIT_COMMIT=${GIT_COMMIT} TTRRONEV_BUILT_AT=${BUILT_AT}
