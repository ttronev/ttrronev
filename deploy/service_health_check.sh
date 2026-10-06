#!/usr/bin/env bash
# External watchdog for ttrronev-service (worker + API). Run from cron every
# 5 minutes ON THE HOST, outside Docker, so it still reports when Docker or the
# containers themselves are down. (deploy/health_check.sh is the older script
# for the paper-trade bot on port 8765 — it does NOT watch this service.)
#
# Reads /api/health and tells four situations apart:
#   API unreachable             -> alert "API down"
#   worker_alive = false        -> alert "worker dead"
#   worker in startup           -> quiet, up to STARTUP_GRACE checks (a restart
#                                  that regenerates everything takes ~15 min)
#   worker running, data stale  -> alert "data stale"
# Sends ONE Telegram message after THRESHOLD consecutive bad checks and one
# recovery message when health returns. No LLM, no dependencies beyond curl.
#
# Telegram creds come from a SEPARATE root-owned file (chmod 600), not the
# service's .env:   /opt/ttrronev/.env-cron
#     TTRRONEV_TG_BOT_TOKEN=...
#     TTRRONEV_TG_CHAT_ID=...
#     TTRRONEV_PORT=8000          # only if you changed the port
set -euo pipefail

ENVFILE="${TTRRONEV_CRON_ENV:-/opt/ttrronev/.env-cron}"
if [ -r "$ENVFILE" ]; then
    # shellcheck disable=SC1090
    . "$ENVFILE"
fi

STATE_FILE="${TTRRONEV_HEALTH_STATE:-/var/tmp/ttrronev_service_health.state}"
URL="http://127.0.0.1:${TTRRONEV_PORT:-8000}/api/health"
THRESHOLD=2          # consecutive bad checks before alerting (= 10 min)
STARTUP_GRACE=9      # checks a worker may spend in startup (= 45 min)

notify() {
    if [ -n "${TTRRONEV_TG_BOT_TOKEN:-}" ] && [ -n "${TTRRONEV_TG_CHAT_ID:-}" ]; then
        curl -sf -m 10 -X POST \
            "https://api.telegram.org/bot${TTRRONEV_TG_BOT_TOKEN}/sendMessage" \
            -d "chat_id=${TTRRONEV_TG_CHAT_ID}" \
            --data-urlencode "text=$1" >/dev/null || true
    fi
}

fail_count=0
startup_count=0
if [ -r "$STATE_FILE" ]; then
    read -r fail_count startup_count < "$STATE_FILE" || true
fi
fail_count="${fail_count//[^0-9]/}"; fail_count="${fail_count:-0}"          # digits only
startup_count="${startup_count//[^0-9]/}"; startup_count="${startup_count:-0}"

body_file="$(mktemp)"
trap 'rm -f "$body_file"' EXIT
code="$(curl -s -m 10 -o "$body_file" -w '%{http_code}' "$URL" 2>/dev/null || true)"
body="$(cat "$body_file" 2>/dev/null || true)"

reason=""
if [ "$code" = "200" ]; then
    reason=""
elif [ -z "$body" ] || [ "$code" = "000" ]; then
    reason="API down (no answer from ${URL})"
elif printf '%s' "$body" | grep -q '"worker_alive":false'; then
    reason="worker dead (no heartbeat for 15+ min)"
elif printf '%s' "$body" | grep -q '"worker_phase":"startup"'; then
    startup_count=$((startup_count + 1))
    if [ "$startup_count" -le "$STARTUP_GRACE" ]; then
        echo "$fail_count $startup_count" > "$STATE_FILE"
        echo "$(date -u +%FT%TZ) starting (${startup_count}/${STARTUP_GRACE})"
        exit 0
    fi
    reason="worker stuck in startup for $((startup_count * 5)) min"
else
    stale="$(printf '%s' "$body" | grep -o '"stale":true' | wc -l | tr -d ' ')"
    reason="data stale for ${stale} pair(s) (worker alive but not regenerating)"
fi

if [ -z "$reason" ]; then
    if [ "$fail_count" -ge "$THRESHOLD" ]; then
        notify "✅ ttrronev-service recovered"
    fi
    echo "0 0" > "$STATE_FILE"
    echo "$(date -u +%FT%TZ) ok"
    exit 0
fi

fail_count=$((fail_count + 1))
echo "$fail_count $startup_count" > "$STATE_FILE"
echo "$(date -u +%FT%TZ) FAIL ${fail_count}: ${reason}"
if [ "$fail_count" -eq "$THRESHOLD" ]; then
    notify "🚨 ttrronev-service: ${reason}"
fi
exit 1
