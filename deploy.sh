#!/usr/bin/env bash
# ttrronev-service one-command deploy (run ON the VPS).
# Prereqs (once): /opt/ttrronev is a git clone; .env exists there (chmod 600,
# never committed) with TTRRONEV_TG_BOT_TOKEN / TTRRONEV_TG_CHAT_ID.
set -euo pipefail
cd /opt/ttrronev
git pull --ff-only
docker compose build
docker compose up -d
docker compose ps
