#!/bin/sh
set -eu

usage() {
    echo "usage: $0 /absolute/path/to/hk-provider-regional.secrets.env" >&2
    exit 64
}

fail() {
    echo "stage-provider-regional-runtime: $1" >&2
    exit 1
}

safe_secret() {
    name=$1
    value=$2
    [ "${#value}" -ge 32 ] || fail "$name must contain at least 32 characters"
    [ "${#value}" -le 256 ] || fail "$name exceeds 256 characters"
    case "$value" in
        *[!A-Za-z0-9._~-]*) fail "$name must use URL-safe characters only" ;;
    esac
}

safe_scalar() {
    name=$1
    value=$2
    [ -n "$value" ] || fail "$name is empty"
    case "$value" in
        *[!A-Za-z0-9._~-]*) fail "$name contains unsupported characters" ;;
    esac
}

[ "$#" -eq 1 ] || usage
case "$1" in
    /*) secrets_file=$1 ;;
    *) secrets_file=$(cd "$(dirname "$1")" && pwd)/$(basename "$1") ;;
esac
[ -f "$secrets_file" ] || fail "private input is not a regular file"
[ ! -L "$secrets_file" ] || fail "private input must not be a symlink"
mode=$(stat -c %a "$secrets_file" 2>/dev/null || stat -f %Lp "$secrets_file")
[ "$mode" = 600 ] || fail "private input mode must be 0600"

# The input is a root-controlled shell environment file. Values are validated
# before they are rendered into service environments or the Valkey ACL.
# shellcheck disable=SC1090
. "$secrets_file"

runtime_password=${A6_PROVIDER_VALKEY_RUNTIME_PASSWORD:-}
projector_password=${A6_PROVIDER_VALKEY_PROJECTOR_PASSWORD:-}
subscriber_host=${PROVIDER_SUBSCRIBER_DATABASE_HOST:-}
subscriber_port=${PROVIDER_SUBSCRIBER_DATABASE_PORT:-}
subscriber_user=${PROVIDER_SUBSCRIBER_DATABASE_USER:-}
subscriber_password=${PROVIDER_SUBSCRIBER_DATABASE_PASSWORD:-}
subscriber_dbname=${PROVIDER_SUBSCRIBER_DATABASE_DBNAME:-}
subscriber_sslmode=${PROVIDER_SUBSCRIBER_DATABASE_SSLMODE:-}
projector_dsn=${PROVIDER_REGIONAL_PROJECTOR_DATABASE_DSN:-}

safe_secret A6_PROVIDER_VALKEY_RUNTIME_PASSWORD "$runtime_password"
safe_secret A6_PROVIDER_VALKEY_PROJECTOR_PASSWORD "$projector_password"
safe_secret PROVIDER_SUBSCRIBER_DATABASE_PASSWORD "$subscriber_password"
safe_scalar PROVIDER_SUBSCRIBER_DATABASE_HOST "$subscriber_host"
safe_scalar PROVIDER_SUBSCRIBER_DATABASE_PORT "$subscriber_port"
safe_scalar PROVIDER_SUBSCRIBER_DATABASE_USER "$subscriber_user"
safe_scalar PROVIDER_SUBSCRIBER_DATABASE_DBNAME "$subscriber_dbname"
safe_scalar PROVIDER_SUBSCRIBER_DATABASE_SSLMODE "$subscriber_sslmode"
case "$projector_dsn" in
    ''|*[![:print:]]*|*[[:space:]]*) fail "PROVIDER_REGIONAL_PROJECTOR_DATABASE_DSN is invalid" ;;
esac
[ "$subscriber_host" = 127.0.0.1 ] || fail "subscriber database must use the HK loopback tunnel"
[ "$subscriber_port" = 15432 ] || fail "subscriber database must use tunnel port 15432"
[ "$subscriber_user" = a6_provider_subscriber ] || fail "subscriber role must be a6_provider_subscriber"
case "$projector_dsn" in
    postgresql://a6_provider_projector:*@127.0.0.1:15432/*) ;;
    *) fail "projector DSN must use a6_provider_projector through 127.0.0.1:15432" ;;
esac

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
repo_dir=$(CDPATH= cd -- "$script_dir/../.." && pwd)
destdir=${DESTDIR:-}
if [ -z "$destdir" ]; then
    [ "$(id -u)" -eq 0 ] || fail "run as root when DESTDIR is empty"
    [ -x /usr/bin/valkey-server ] || fail "/usr/bin/valkey-server is missing"
    [ -x /usr/bin/valkey-cli ] || fail "/usr/bin/valkey-cli is missing"
    getent passwd valkey >/dev/null 2>&1 || fail "valkey service account is missing"
    getent passwd a6trader >/dev/null 2>&1 || fail "a6trader service account is missing"
    valkey_version=$(/usr/bin/valkey-server --version 2>/dev/null || true)
    case "$valkey_version" in
        *"v=7."*|*"v=8."*|*"v=9."*) ;;
        *) fail "Valkey 7.x or newer with ACL selector support is required" ;;
    esac
fi

umask 077
install -d -m 0750 "$destdir/etc/a6-provider-valkey-hk"
install -d -m 0750 "$destdir/etc/a6-provider-regional-hk/staged-systemd"
install -d -m 0750 "$destdir/etc/sub2api-hk"
install -d -m 0755 "$destdir/etc/systemd/system"
install -m 0640 "$script_dir/a6-provider-valkey-hk.conf" \
    "$destdir/etc/a6-provider-valkey-hk/valkey.conf"

acl_file=$destdir/etc/a6-provider-valkey-hk/users.acl
cat >"$acl_file" <<EOF
user default off
user a6-provider-runtime reset on >$runtime_password -@all resetkeys resetchannels +@connection +@pubsub +info +time +script|load +script|exists &a6:provider:* (+get +mget +hget +hmget +hgetall +zrange +zscore +zcard +hlen +exists ~a6:provider:dispatch:* ~a6:provider:fast-block:*) (+get +mget +hget +hmget +hgetall +zrange +zscore +zcard +hlen +exists +set +del +expire +persist +hset +hdel +hincrby +zadd +zrem +zremrangebyscore +eval +evalsha ~a6:provider:capacity:* ~sub2api:a6-readiness:*)
user a6-provider-projector reset on >$projector_password -@all resetkeys resetchannels +@connection +@pubsub +@transaction +info +time +script|load +script|exists &a6:provider:* (+@read +@write +@scripting -flushall -flushdb -keys -config -shutdown -replicaof -slaveof -migrate -module -acl -debug -restore -restore-asking ~a6:provider:*)
EOF
chmod 0640 "$acl_file"

regional_env=$destdir/etc/sub2api-hk/sub2api-hk-shadow-provider-regional.env
cat >"$regional_env" <<EOF
GATEWAY_A6_PROVIDER_REPLAY_ENABLED=true
GATEWAY_A6_PROVIDER_REPLAY_DURABLE_COORDINATION_ENABLED=true
GATEWAY_A6_PROVIDER_REPLAY_DURABLE_ATTRIBUTION_ENABLED=true
GATEWAY_A6_PROVIDER_REPLAY_CANDIDATE_SNAPSHOT_ENABLED=true
GATEWAY_A6_PROVIDER_REPLAY_DISPATCH_LEASE_ENABLED=true
GATEWAY_A6_PROVIDER_REPLAY_FAST_BLOCK_ENABLED=true
GATEWAY_A6_PROVIDER_REPLAY_USAGE_ATTRIBUTION_ENABLED=true
GATEWAY_A6_PROVIDER_REPLAY_SYNTHETIC_USER_FAULT_ENABLED=true
GATEWAY_A6_PROVIDER_REPLAY_STREAMING_DIRECT_ENABLED=false
GATEWAY_A6_PROVIDER_REPLAY_READINESS_ENABLED=true
GATEWAY_A6_PROVIDER_REPLAY_PROVIDER_REGIONAL_COORDINATION_ENABLED=true
GATEWAY_A6_PROVIDER_REPLAY_PROVIDER_REGIONAL_CAPACITY_ESCROW_ENABLED=true
GATEWAY_A6_PROVIDER_REPLAY_PROVIDER_REGIONAL_SUBSCRIBER_ENABLED=true
GATEWAY_A6_PROVIDER_REPLAY_PROVIDER_REGIONAL_REGION=hk
GATEWAY_A6_PROVIDER_REPLAY_PROVIDER_REGIONAL_PROJECTION_MAX_AGE_SECONDS=30
GATEWAY_A6_PROVIDER_REPLAY_PROVIDER_REGIONAL_CAPACITY_SLOT_TTL_SECONDS=900
GATEWAY_A6_PROVIDER_REPLAY_PROVIDER_REGIONAL_CAPACITY_RENEW_INTERVAL_SECONDS=60
PROVIDER_REDIS_HOST=127.0.0.1
PROVIDER_REDIS_PORT=6380
PROVIDER_REDIS_USERNAME=a6-provider-runtime
PROVIDER_REDIS_PASSWORD=$runtime_password
PROVIDER_REDIS_DB=0
PROVIDER_REDIS_DIAL_TIMEOUT_SECONDS=2
PROVIDER_REDIS_READ_TIMEOUT_SECONDS=1
PROVIDER_REDIS_WRITE_TIMEOUT_SECONDS=1
PROVIDER_REDIS_POOL_SIZE=128
PROVIDER_REDIS_MIN_IDLE_CONNS=16
PROVIDER_REDIS_ENABLE_TLS=false
PROVIDER_SUBSCRIBER_DATABASE_HOST=$subscriber_host
PROVIDER_SUBSCRIBER_DATABASE_PORT=$subscriber_port
PROVIDER_SUBSCRIBER_DATABASE_USER=$subscriber_user
PROVIDER_SUBSCRIBER_DATABASE_PASSWORD=$subscriber_password
PROVIDER_SUBSCRIBER_DATABASE_DBNAME=$subscriber_dbname
PROVIDER_SUBSCRIBER_DATABASE_SSLMODE=$subscriber_sslmode
PROVIDER_SUBSCRIBER_DATABASE_MAX_OPEN_CONNS=8
PROVIDER_SUBSCRIBER_DATABASE_MAX_IDLE_CONNS=4
PROVIDER_SUBSCRIBER_DATABASE_CONN_MAX_LIFETIME_MINUTES=30
PROVIDER_SUBSCRIBER_DATABASE_CONN_MAX_IDLE_TIME_MINUTES=5
EOF
chmod 0600 "$regional_env"

projector_env=$destdir/etc/a6-provider-regional-hk/projector.env
cat >"$projector_env" <<EOF
A6_TRADE_DATABASE_DSN=$projector_dsn
PROVIDER_REGIONAL_PROJECTOR_DATABASE_DSN=$projector_dsn
PROVIDER_DISPATCH_REDIS_URL=redis://a6-provider-projector:$projector_password@127.0.0.1:6380/0
PROVIDER_REDIS_PROJECTOR_USERNAME=a6-provider-projector
PROVIDER_REDIS_PROJECTOR_PASSWORD=$projector_password
PROVIDER_DISPATCH_RECONCILE_ENABLED=true
PROVIDER_REGIONAL_COORDINATION_ENABLED=true
EOF
chmod 0600 "$projector_env"

install -m 0644 "$repo_dir/deploy/systemd/a6-provider-valkey-hk.service" \
    "$destdir/etc/systemd/system/a6-provider-valkey-hk.service"
install -m 0644 "$repo_dir/deploy/systemd/a6-trader-provider-regional-hk.service" \
    "$destdir/etc/systemd/system/a6-trader-provider-regional-hk.service"
install -m 0644 "$repo_dir/deploy/systemd/a6-trader-provider-regional-hk.timer" \
    "$destdir/etc/systemd/system/a6-trader-provider-regional-hk.timer"
install -m 0644 "$script_dir/sub2api-hk-shadow-provider-regional.conf" \
    "$destdir/etc/a6-provider-regional-hk/staged-systemd/sub2api-hk-shadow-provider-regional.conf"

if [ -z "$destdir" ]; then
    install -d -o valkey -g valkey -m 0750 /var/lib/a6-provider-valkey-hk
    chown -R root:valkey /etc/a6-provider-valkey-hk
    chown -R root:root /etc/a6-provider-regional-hk /etc/sub2api-hk/sub2api-hk-shadow-provider-regional.env
fi

echo "HK Provider regional runtime staged; no unit was enabled, started, or restarted."
echo "The Sub2API drop-in remains staged under /etc/a6-provider-regional-hk/staged-systemd/."
