#!/bin/sh
set -eu

fail() {
    echo "verify-provider-regional-runtime: $1" >&2
    exit 1
}

env_value() {
    key=$1
    file=$2
    sed -n "s/^${key}=//p" "$file" | tail -n 1
}

model=${1:-gpt-5.6-sol}
case "$model" in
    ''|*[!A-Za-z0-9._-]*) fail "model is invalid" ;;
esac
regional_only=${REGIONAL_ONLY:-0}
[ "$regional_only" = 0 ] || [ "$regional_only" = 1 ] || fail "REGIONAL_ONLY must be 0 or 1"
[ "$(id -u)" -eq 0 ] || fail "run as root"

base_env=/etc/sub2api-hk/sub2api-hk-shadow.env
regional_env=/etc/sub2api-hk/sub2api-hk-shadow-provider-regional.env
projector_env=/etc/a6-provider-regional-hk/projector.env
for file in "$base_env" "$regional_env" "$projector_env"; do
    [ -r "$file" ] || fail "$file is missing"
done
for command in curl psql valkey-cli python3 systemctl; do
    command -v "$command" >/dev/null 2>&1 || fail "$command is missing"
done

provider_host=$(env_value PROVIDER_REDIS_HOST "$regional_env")
provider_port=$(env_value PROVIDER_REDIS_PORT "$regional_env")
provider_username=$(env_value PROVIDER_REDIS_USERNAME "$regional_env")
provider_password=$(env_value PROVIDER_REDIS_PASSWORD "$regional_env")
global_host=$(env_value REDIS_HOST "$base_env")
global_port=$(env_value REDIS_PORT "$base_env")
global_password=$(env_value REDIS_PASSWORD "$base_env")
projector_dsn=$(env_value PROVIDER_REGIONAL_PROJECTOR_DATABASE_DSN "$projector_env")
projector_username=$(env_value PROVIDER_REDIS_PROJECTOR_USERNAME "$projector_env")
projector_password=$(env_value PROVIDER_REDIS_PROJECTOR_PASSWORD "$projector_env")

[ "$provider_host" = 127.0.0.1 ] || fail "Provider Redis host is not loopback"
[ "$provider_port" = 6380 ] || fail "Provider Redis port is not the dedicated port"
[ "$provider_username" = a6-provider-runtime ] || fail "Provider Redis runtime user is invalid"
[ -n "$provider_password" ] || fail "Provider Redis password is empty"
[ "$projector_username" = a6-provider-projector ] || fail "Provider Redis projector user is invalid"
[ -n "$projector_password" ] || fail "Provider Redis projector password is empty"
[ -n "$global_host" ] && [ -n "$global_port" ] || fail "global Redis endpoint is incomplete"
if [ "$provider_host:$provider_port" = "$global_host:$global_port" ]; then
    fail "Provider Redis aliases the global Redis endpoint"
fi
[ -n "$projector_dsn" ] || fail "projector DSN is empty"

systemctl is-active --quiet a6-provider-valkey-hk.service \
    || fail "dedicated Valkey is not active"
systemctl is-active --quiet a6-trader-provider-regional-hk.timer \
    || fail "regional projector timer is not active"
systemctl is-active --quiet sub2api-hk-shadow.service \
    || fail "Sub2API HK shadow is not active"
if systemctl is-active --quiet a6-shadow-redis.service; then
    fail "legacy ephemeral shadow Redis is still active"
fi

acl_file=/etc/a6-provider-valkey-hk/users.acl
[ -r "$acl_file" ] || fail "Provider Valkey ACL file is missing"
grep -Fqx 'user default off' "$acl_file" || fail "Valkey default user is not disabled"
grep -Fq 'user a6-provider-runtime reset on ' "$acl_file" \
    || fail "Valkey runtime ACL user is missing"
grep -Fq 'user a6-provider-projector reset on ' "$acl_file" \
    || fail "Valkey projector ACL user is missing"

provider_info=$(REDISCLI_AUTH="$provider_password" valkey-cli --no-auth-warning \
    --user "$provider_username" \
    -h "$provider_host" -p "$provider_port" --raw INFO server)
provider_epoch=$(printf '%s\n' "$provider_info" \
    | awk -F: '$1 == "run_id" {gsub(/\r/, "", $2); print $2; exit}')
[ -n "$provider_epoch" ] || fail "dedicated Valkey restart epoch is missing"

global_info=$(REDISCLI_AUTH="$global_password" valkey-cli --no-auth-warning \
    -h "$global_host" -p "$global_port" --raw INFO server)
global_epoch=$(printf '%s\n' "$global_info" \
    | awk -F: '$1 == "run_id" {gsub(/\r/, "", $2); print $2; exit}')
[ -n "$global_epoch" ] || fail "global Redis restart epoch is missing"
[ "$provider_epoch" != "$global_epoch" ] || fail "Provider and global Redis have the same run_id"

default_auth=$(REDISCLI_AUTH="$provider_password" valkey-cli --no-auth-warning \
    -h "$provider_host" -p "$provider_port" --raw PING 2>&1 || true)
case "$default_auth" in
    *WRONGPASS*|*NOPERM*) ;;
    *) fail "Provider Valkey default user accepted runtime credentials" ;;
esac

denied=$(REDISCLI_AUTH="$provider_password" valkey-cli --no-auth-warning \
    --user "$provider_username" \
    -h "$provider_host" -p "$provider_port" --raw GET sub2api:global-state-probe 2>&1 || true)
case "$denied" in
    *NOPERM*) ;;
    *) fail "Provider Valkey ACL permits non-Provider key access" ;;
esac

probe_suffix="acl-probe-$$"
dispatch_probe="a6:provider:dispatch:v1:$probe_suffix"
lease_probe="a6:provider:capacity:v1:{hk:$probe_suffix}:lease"
projection_write=$(REDISCLI_AUTH="$provider_password" valkey-cli --no-auth-warning \
    --user "$provider_username" -h "$provider_host" -p "$provider_port" --raw \
    SET "$dispatch_probe" blocked EX 30 2>&1 || true)
case "$projection_write" in
    *NOPERM*) ;;
    *) fail "runtime ACL permits projection writes" ;;
esac
REDISCLI_AUTH="$provider_password" valkey-cli --no-auth-warning \
    --user "$provider_username" -h "$provider_host" -p "$provider_port" --raw \
    GET "$dispatch_probe" >/dev/null \
    || fail "runtime ACL rejects projection reads"
lease_value=$(REDISCLI_AUTH="$provider_password" valkey-cli --no-auth-warning \
    --user "$provider_username" -h "$provider_host" -p "$provider_port" --raw \
    EVAL "redis.call('SET', KEYS[1], ARGV[1], 'EX', 30); return redis.call('GET', KEYS[1])" \
    1 "$lease_probe" ok)
[ "$lease_value" = ok ] || fail "runtime ACL rejects lease scripts"
REDISCLI_AUTH="$provider_password" valkey-cli --no-auth-warning \
    --user "$provider_username" -h "$provider_host" -p "$provider_port" --raw \
    DEL "$lease_probe" >/dev/null \
    || fail "runtime ACL rejects lease cleanup"
projector_value=$(REDISCLI_AUTH="$projector_password" valkey-cli --no-auth-warning \
    --user "$projector_username" -h "$provider_host" -p "$provider_port" --raw \
    SET "$dispatch_probe" ok EX 30)
[ "$projector_value" = OK ] || fail "projector ACL rejects projection writes"
REDISCLI_AUTH="$projector_password" valkey-cli --no-auth-warning \
    --user "$projector_username" -h "$provider_host" -p "$provider_port" --raw \
    DEL "$dispatch_probe" >/dev/null \
    || fail "projector ACL rejects projection cleanup"

database_role=$(psql "$projector_dsn" -XAtqc 'SELECT current_user')
[ "$database_role" = a6_provider_projector ] || fail "projector DSN uses the wrong role"

gate_python=/opt/a6-trader-hk/current/venv/bin/python
[ -x "$gate_python" ] || fail "Trader release Python is missing"
snapshot_dir=$(mktemp -d)
health_body=
ready_body=
cleanup() {
    rm -rf "$snapshot_dir"
    [ -z "$health_body" ] || rm -f "$health_body"
    [ -z "$ready_body" ] || rm -f "$ready_body"
}
trap cleanup EXIT HUP INT TERM

deadline=$(( $(date +%s) + 90 ))
projection_status=
capacity_status=
while [ "$(date +%s)" -le "$deadline" ]; do
    projection_status=$(psql "$projector_dsn" -XAt -F '|' -c "
        SELECT count(*),
               count(*) FILTER (
                   WHERE projection_state = 'ready'
                     AND subscriber_connected
                     AND subscriber_cursor = last_event_id
                     AND subscriber_verified_at IS NOT NULL
                     AND verified_at IS NOT NULL
                     AND last_error_class IS NULL
                     AND redis_restart_epoch = '$provider_epoch'
               )
        FROM a6_trade.provider_region_projection_receipts
        WHERE region = 'hk'
          AND model = '$model'
          AND projection_type IN ('dispatch', 'fast_block')
    ")
    capacity_status=$(psql "$projector_dsn" -XAt -F '|' -c "
        SELECT count(*),
               count(*) FILTER (
                   WHERE receipt.receipt_state = 'ready'
                     AND receipt.allocation_version = allocation.allocation_version
                     AND receipt.quota = allocation.quota
                     AND receipt.occupancy <= receipt.quota
                     AND receipt.redis_restart_epoch = '$provider_epoch'
                     AND receipt.verified_at IS NOT NULL
                     AND receipt.last_error_class IS NULL
               ),
               coalesce(sum(allocation.quota), 0)
        FROM a6_trade.provider_region_capacity_allocations allocation
        LEFT JOIN a6_trade.provider_region_capacity_receipts receipt
          ON receipt.enodot_account_id = allocation.enodot_account_id
         AND receipt.region = allocation.region
        WHERE allocation.region = 'hk'
          AND allocation.valid_to IS NULL
    ")
    projection_total=${projection_status%%|*}
    projection_ready=${projection_status#*|}
    capacity_total=${capacity_status%%|*}
    capacity_rest=${capacity_status#*|}
    capacity_ready=${capacity_rest%%|*}
    capacity_quota=${capacity_rest##*|}
    if [ "$projection_total" = 2 ] && [ "$projection_ready" = 2 ] \
        && [ "$capacity_total" -gt 0 ] && [ "$capacity_ready" = "$capacity_total" ] \
        && [ "$capacity_quota" -gt 0 ]; then
        break
    fi
    sleep 2
done

[ "${projection_status:-}" = '2|2' ] \
    || fail "dispatch/fast-block receipts did not converge: ${projection_status:-missing}"
capacity_total=${capacity_status%%|*}
capacity_rest=${capacity_status#*|}
capacity_ready=${capacity_rest%%|*}
capacity_quota=${capacity_rest##*|}
[ "$capacity_total" -gt 0 ] && [ "$capacity_ready" = "$capacity_total" ] \
    && [ "$capacity_quota" -gt 0 ] \
    || fail "capacity receipts did not converge: ${capacity_status:-missing}"

projection_receipts_file=$snapshot_dir/projection-receipts.json
psql "$projector_dsn" -XAt -v ON_ERROR_STOP=1 -v model="$model" -c "
    SELECT coalesce(json_agg(snapshot ORDER BY projection_type), '[]'::json)
    FROM (
        SELECT region, model, projection_type, routing_generation,
               cost_generation, source_hash, projection_digest,
               last_event_id, subscriber_cursor, subscriber_connected,
               redis_restart_epoch, projection_state, last_error_class
        FROM a6_trade.provider_region_projection_receipts
        WHERE region = 'hk'
          AND model = :'model'
          AND projection_type IN ('dispatch', 'fast_block')
    ) snapshot
" >"$projection_receipts_file"

capacity_rows_file=$snapshot_dir/capacity-rows.json
psql "$projector_dsn" -XAt -v ON_ERROR_STOP=1 -c "
    SELECT coalesce(json_agg(snapshot ORDER BY enodot_account_id), '[]'::json)
    FROM (
        SELECT allocation.enodot_account_id, allocation.region,
               allocation.allocation_version, allocation.quota,
               allocation.account_concurrency, allocation.allocation_state,
               receipt.allocation_version AS receipt_allocation_version,
               receipt.quota AS receipt_quota,
               receipt.occupancy AS receipt_occupancy,
               receipt.occupancy_digest AS receipt_occupancy_digest,
               receipt.lease_cursor AS receipt_lease_cursor,
               receipt.redis_restart_epoch AS receipt_redis_restart_epoch,
               receipt.receipt_state, receipt.receipt_version,
               receipt.last_error_class AS receipt_last_error_class
        FROM a6_trade.provider_region_capacity_allocations allocation
        LEFT JOIN a6_trade.provider_region_capacity_receipts receipt
          ON receipt.enodot_account_id = allocation.enodot_account_id
         AND receipt.region = allocation.region
        WHERE allocation.region = 'hk'
          AND allocation.valid_to IS NULL
    ) snapshot
" >"$capacity_rows_file"

capacity_overallocated_accounts=$(psql "$projector_dsn" -XAt -v ON_ERROR_STOP=1 -c "
    SELECT count(*)
    FROM (
        SELECT enodot_account_id
        FROM a6_trade.provider_region_capacity_allocations
        WHERE valid_to IS NULL
        GROUP BY enodot_account_id
        HAVING sum(quota) > max(account_concurrency)
            OR min(account_concurrency) <> max(account_concurrency)
    ) violations
")

dispatch_meta_file=$snapshot_dir/dispatch-meta.json
fast_block_meta_file=$snapshot_dir/fast-block-meta.json
REDISCLI_AUTH="$provider_password" valkey-cli --no-auth-warning \
    --user "$provider_username" \
    -h "$provider_host" -p "$provider_port" --raw \
    GET "a6:provider:dispatch:v1:$model:meta" >"$dispatch_meta_file"
REDISCLI_AUTH="$provider_password" valkey-cli --no-auth-warning \
    --user "$provider_username" \
    -h "$provider_host" -p "$provider_port" --raw \
    GET "a6:provider:fast-block:v1:$model:meta" >"$fast_block_meta_file"

capacity_accounts=$(python3 - "$capacity_rows_file" <<'PY'
import json
import pathlib
import sys

rows = json.loads(pathlib.Path(sys.argv[1]).read_text())
if not isinstance(rows, list):
    raise SystemExit("capacity rows are invalid")
for row in rows:
    account_id = row.get("enodot_account_id") if isinstance(row, dict) else None
    if isinstance(account_id, bool) or not isinstance(account_id, int) or account_id < 1:
        raise SystemExit("capacity account is invalid")
    print(account_id)
PY
)
for account_id in $capacity_accounts; do
    REDISCLI_AUTH="$provider_password" valkey-cli --no-auth-warning \
        --user "$provider_username" \
        -h "$provider_host" -p "$provider_port" --raw \
        HGETALL "a6:provider:capacity:v1:{hk:$account_id}:meta" \
        >"$snapshot_dir/capacity-$account_id.hgetall"
done

if ! exact_gate=$(
    "$gate_python" -m a6api_auto.trade_provider_regional_gate \
        --region hk \
        --model "$model" \
        --redis-restart-epoch "$provider_epoch" \
        --projection-receipts-file "$projection_receipts_file" \
        --dispatch-meta-file "$dispatch_meta_file" \
        --fast-block-meta-file "$fast_block_meta_file" \
        --capacity-rows-file "$capacity_rows_file" \
        --capacity-metadata-dir "$snapshot_dir" \
        --capacity-overallocated-accounts "$capacity_overallocated_accounts"
); then
    fail "PostgreSQL and Provider Redis exact state did not converge"
fi

health_body=$(mktemp)
ready_body=$(mktemp)
health_code=$(curl --silent --show-error --max-time 5 --output "$health_body" \
    --write-out '%{http_code}' http://127.0.0.1:48769/healthz)
[ "$health_code" = 200 ] || fail "/healthz returned $health_code"
ready_code=$(curl --silent --show-error --max-time 5 --output "$ready_body" \
    --write-out '%{http_code}' http://127.0.0.1:48769/readyz)
python3 - "$ready_body" "$ready_code" "$regional_only" <<'PY'
import json
import pathlib
import sys

document = json.loads(pathlib.Path(sys.argv[1]).read_text())
checks = document.get("checks")
if not isinstance(checks, dict):
    raise SystemExit("readiness checks are missing")
regional = ("regional_redis", "regional_projector", "regional_subscriber", "regional_capacity")
failed = {name: checks.get(name) for name in regional if checks.get(name, {}).get("status") != "ok"}
if failed:
    raise SystemExit(f"regional readiness failed: {failed}")
if sys.argv[3] == "0" and (sys.argv[2] != "200" or document.get("status") != "ready"):
    raise SystemExit(f"full readiness failed: http={sys.argv[2]} status={document.get('status')}")
PY

printf '{"success":true,"model":"%s","provider_restart_epoch":"%s","global_restart_epoch":"%s","projection_receipts":"%s","capacity_receipts":"%s","exact_gate":%s,"readyz_http":%s}\n' \
    "$model" "$provider_epoch" "$global_epoch" "$projection_status" "$capacity_status" "$exact_gate" "$ready_code"
