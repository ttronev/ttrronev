#!/usr/bin/env bash
# ttrronev-service one-command deploy (run ON the VPS).
# Prereqs (once): /opt/ttrronev is a git clone; .env exists there (chmod 600,
# never committed) with TTRRONEV_TG_BOT_TOKEN / TTRRONEV_TG_CHAT_ID (and
# TTRRONEV_API_KEY once the app shell is used from outside the box).
set -euo pipefail
cd /opt/ttrronev
git pull --ff-only
# The image builds the app shell itself (node stage); the args stamp
# /api/v1/version with the deployed commit and build time.
GIT_COMMIT="$(git rev-parse --short HEAD)" BUILT_AT="$(date -u +%FT%TZ)" \
  docker compose build --build-arg GIT_COMMIT="$GIT_COMMIT" --build-arg BUILT_AT="$BUILT_AT"
docker compose up -d
docker compose ps
