#!/bin/sh
set -eu

ADMIN_KEY_FILE=${SUB2API_CALLBACK_ADMIN_KEY_FILE:-/var/lib/a6-trader/secrets/enodot-admin-key.secret}
SETTINGS_FILE=${SUB2API_CALLBACK_SETTINGS_FILE:-/etc/sub2api/callback-probe-settings.json}
ADMIN_URL=${SUB2API_CALLBACK_ADMIN_URL:-http://127.0.0.1:48768/api/v1/admin/settings/probe-response-rules}

test -s "$ADMIN_KEY_FILE"
test -s "$SETTINGS_FILE"
admin_key=$(cat "$ADMIN_KEY_FILE")
response=$(mktemp)
trap 'rm -f "$response"' EXIT HUP INT TERM

attempt=1
while [ "$attempt" -le 30 ]; do
    if curl --fail --silent --show-error --max-time 20 \
        --request PUT \
        --header "x-api-key: $admin_key" \
        --header "Content-Type: application/json" \
        --data-binary "@$SETTINGS_FILE" \
        --output "$response" \
        "$ADMIN_URL" \
        && grep -q '"enabled":true' "$response" \
        && grep -q '"id":"callback-0001"' "$response"; then
        exit 0
    fi
    attempt=$((attempt + 1))
    sleep 1
done

exit 1
