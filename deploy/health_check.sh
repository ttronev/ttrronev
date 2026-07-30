#!/usr/bin/env bash
# External health-check script. Pings the bot's /health endpoint;
# tracks consecutive failures in a state file. On 2-in-a-row failures,
# fires a Telegram alert directly from the cron — so we get notified
# even if the bot is hung but systemd hasn't restarted it yet.
#
# Source TG creds from a SEPARATE env file owned by root, chmod 600.
# These are NOT the bot's main env; they're a copy used only here.
set -euo pipefail

ENVFILE="/opt/ttrronev/.env-cron"
STATE_FILE="/opt/ttrronev/paper_trade/data/health_check.state"
URL="http://127.0.0.1:8765/health"
THRESHOLD=2

if [ -r "$ENVFILE" ]; then
    # shellcheck disable=SC1090
    . "$ENVFILE"
fi

mkdir -p "$(dirname "$STATE_FILE")"
fail_count=0
if [ -r "$STATE_FILE" ]; then
    fail_count="$(cat "$STATE_FILE")"
fi

if curl -sf -m 10 "$URL" >/dev/null 2>&1; then
    # Reset on success.
    if [ "$fail_count" -ge "$THRESHOLD" ]; then
        # Recovery alert.
        if [ -n "${TTRRONEV_TG_BOT_TOKEN:-}" ] && [ -n "${TTRRONEV_TG_CHAT_ID:-}" ]; then
            curl -sf -m 10 -X POST \
                "https://api.telegram.org/bot${TTRRONEV_TG_BOT_TOKEN}/sendMessage" \
                -d "chat_id=${TTRRONEV_TG_CHAT_ID}" \
                -d "text=health check recovered" >/dev/null || true
        fi
    fi
    echo 0 > "$STATE_FILE"
    exit 0
fi

fail_count=$((fail_count + 1))
echo "$fail_count" > "$STATE_FILE"
if [ "$fail_count" -eq "$THRESHOLD" ]; then
    if [ -n "${TTRRONEV_TG_BOT_TOKEN:-}" ] && [ -n "${TTRRONEV_TG_CHAT_ID:-}" ]; then
        curl -sf -m 10 -X POST \
            "https://api.telegram.org/bot${TTRRONEV_TG_BOT_TOKEN}/sendMessage" \
            -d "chat_id=${TTRRONEV_TG_CHAT_ID}" \
            -d "text=health check FAILED ${fail_count}x in a row at ${URL}" >/dev/null || true
    fi
fi
exit 1
