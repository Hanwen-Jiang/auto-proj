#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
from collections.abc import Mapping
from typing import Any

import psycopg
from psycopg.rows import dict_row

from a6api_auto.trade_provider_dispatch import (
    PostgresProviderDispatchSource,
    ProviderDispatchError,
    ProviderDispatchSink,
    ProviderDispatchSource,
    RedisProviderDispatchSink,
)
from a6api_auto.trade_provider_fast_block import RedisProviderFastBlockProjector
from a6api_auto.trade_provider_region import PostgresProviderRegionStore
from a6api_auto.trade_provider_regional_runtime import (
    PostgresProviderRegionalSource,
    ProviderRegionalNotificationConsumer,
    ProviderRegionalProjectionRuntime,
    ProviderRegionalRuntimeError,
    RedisProviderRegionalCapacityProjector,
    RedisProviderRegionalDispatchProjector,
    RedisProviderRegionalFastBlockProjector,
)

_TRUE_VALUES = {"1", "true", "yes", "on"}
_FALSE_VALUES = {"", "0", "false", "no", "off"}


def resolve_enabled(
    cli_enabled: bool,
    *,
    env_name: str = "PROVIDER_DISPATCH_RECONCILE_ENABLED",
    environ: Mapping[str, str] | None = None,
) -> bool:
    if not isinstance(cli_enabled, bool):
        raise ProviderDispatchError("provider_dispatch_flag_invalid")
    if cli_enabled:
        return True
    environment = os.environ if environ is None else environ
    value = str(environment.get(env_name, "")).strip().casefold()
    if value in _TRUE_VALUES:
        return True
    if value in _FALSE_VALUES:
        return False
    raise ProviderDispatchError("provider_dispatch_flag_invalid")


def resolve_regional_projector_dsn(
    env_name: str,
    *,
    environ: Mapping[str, str] | None = None,
) -> str:
    environment = os.environ if environ is None else environ
    dsn = str(environment.get(env_name, "")).strip()
    if not dsn:
        raise ProviderRegionalRuntimeError("provider_regional_database_dsn_missing")
    return dsn


def run_model(
    model: str,
    *,
    enabled: bool,
    publish: bool,
    source: ProviderDispatchSource | None = None,
    sink: ProviderDispatchSink | None = None,
) -> dict[str, Any]:
    model = str(model).strip()
    if not model:
        raise ProviderDispatchError("provider_dispatch_model_invalid")
    if not isinstance(enabled, bool) or not isinstance(publish, bool):
        raise ProviderDispatchError("provider_dispatch_mode_invalid")
    if not enabled:
        return {
            "success": True,
            "model": model,
            "enabled": False,
            "publish": False,
            "action": "disabled",
        }
    if source is None:
        raise ProviderDispatchError("provider_dispatch_source_missing")
    active = source.load_active(model)
    if active is None:
        raise ProviderDispatchError("provider_dispatch_projection_missing")
    result = {
        "success": True,
        "model": model,
        "enabled": True,
        "publish": publish,
        "routing_generation": active.routing_generation,
        "cost_generation": active.cost_generation,
        "ready_count": len(active.ready_slots),
        "state": active.state,
        "digest": active.digest(),
    }
    if not publish:
        return {
            **result,
            "action": "planned" if active.state == "active" else "tombstone-planned",
            "published": False,
        }
    if sink is None:
        raise ProviderDispatchError("provider_dispatch_sink_missing")
    published = sink.publish(active)
    return {
        **result,
        "action": (
            "unchanged"
            if not published.changed
            else "published"
            if active.state == "active"
            else "tombstoned"
        ),
        "published": True,
        "changed": published.changed,
        "key": published.key,
    }


def run_regional_model(
    model: str,
    *,
    enabled: bool,
    publish: bool,
    region: str,
    runtime: ProviderRegionalProjectionRuntime | None = None,
) -> dict[str, Any]:
    model = str(model).strip()
    if not model:
        raise ProviderRegionalRuntimeError("provider_regional_model_invalid")
    if not isinstance(enabled, bool) or not isinstance(publish, bool):
        raise ProviderRegionalRuntimeError("provider_regional_mode_invalid")
    if not enabled:
        return {
            "success": True,
            "model": model,
            "enabled": False,
            "publish": False,
            "action": "disabled",
        }
    if not publish:
        raise ProviderRegionalRuntimeError("provider_regional_publish_required")
    if runtime is None:
        raise ProviderRegionalRuntimeError("provider_regional_runtime_missing")
    return runtime.reconcile(model, region=region)


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(
        description="Project the PostgreSQL active Provider dispatch bundle to Redis"
    )
    command.add_argument("--model", required=True)
    command.add_argument("--publish", action="store_true")
    command.add_argument("--enabled", action="store_true")
    command.add_argument(
        "--enabled-env",
        default="PROVIDER_DISPATCH_RECONCILE_ENABLED",
    )
    command.add_argument("--database-dsn-env", default="A6_TRADE_DATABASE_DSN")
    command.add_argument(
        "--redis-url-env",
        default="PROVIDER_DISPATCH_REDIS_URL",
    )
    command.add_argument("--redis-key-prefix", default="a6:provider:dispatch:v1")
    command.add_argument(
        "--redis-notification-channel",
        default="a6:provider:dispatch:changed:v1",
    )
    command.add_argument("--regional-enabled", action="store_true")
    command.add_argument(
        "--regional-enabled-env",
        default="PROVIDER_REGIONAL_COORDINATION_ENABLED",
    )
    command.add_argument("--region")
    command.add_argument("--region-env", default="PROVIDER_REGIONAL_REGION")
    command.add_argument(
        "--regional-projector-dsn-env",
        default="PROVIDER_REGIONAL_PROJECTOR_DATABASE_DSN",
    )
    command.add_argument(
        "--regional-fast-block-key-prefix",
        default="a6:provider:fast-block:v1",
    )
    command.add_argument(
        "--regional-fast-block-notification-channel",
        default="a6:provider:fast-block:changed:v1",
    )
    command.add_argument(
        "--regional-capacity-key-prefix",
        default="a6:provider:capacity:v1",
    )
    command.add_argument(
        "--regional-capacity-notification-channel",
        default="a6:provider:capacity:changed:v1",
    )
    command.add_argument("--regional-capacity-lease-ttl-seconds", type=int, default=900)
    command.add_argument("--regional-restart-grace-seconds", type=int, default=900)
    command.add_argument("--regional-watch", action="store_true")
    command.add_argument("--regional-reconnect-seconds", type=float, default=1.0)
    command.add_argument("--regional-poll-seconds", type=float, default=1.0)
    return command


def main() -> None:
    args = parser().parse_args()
    enabled = resolve_enabled(args.enabled, env_name=args.enabled_env)
    regional_enabled = resolve_enabled(
        args.regional_enabled,
        env_name=args.regional_enabled_env,
    )
    if not enabled:
        print(
            json.dumps(
                run_model(args.model, enabled=False, publish=args.publish),
                ensure_ascii=True,
                sort_keys=True,
            )
        )
        return
    dsn = str(os.environ.get(args.database_dsn_env, "")).strip()
    if not dsn:
        raise ProviderDispatchError("provider_dispatch_database_dsn_missing")

    def connection_factory() -> Any:
        return psycopg.connect(dsn, row_factory=dict_row)

    source = PostgresProviderDispatchSource(connection_factory)
    sink = None
    redis_client = None
    if args.publish:
        redis_url = str(os.environ.get(args.redis_url_env, "")).strip()
        if not redis_url:
            raise ProviderDispatchError("provider_dispatch_redis_url_missing")
        try:
            import redis
        except ImportError:
            raise ProviderDispatchError("provider_dispatch_redis_dependency_missing") from None
        redis_client = redis.Redis.from_url(
            redis_url,
            decode_responses=True,
            socket_connect_timeout=2,
            socket_timeout=2,
            health_check_interval=30,
        )
        sink = RedisProviderDispatchSink(
            redis_client,
            key_prefix=args.redis_key_prefix,
            notification_channel=args.redis_notification_channel,
        )
    if regional_enabled:
        if not args.publish or redis_client is None or sink is None:
            raise ProviderRegionalRuntimeError("provider_regional_publish_required")
        region = str(args.region or os.environ.get(args.region_env, "")).strip()
        if not region:
            raise ProviderRegionalRuntimeError("provider_regional_region_missing")
        projector_dsn = resolve_regional_projector_dsn(args.regional_projector_dsn_env)

        def projector_connection_factory() -> Any:
            return psycopg.connect(projector_dsn, row_factory=dict_row)

        regional_dispatch_source = PostgresProviderDispatchSource(projector_connection_factory)
        regional_source = PostgresProviderRegionalSource(projector_connection_factory)
        projector_store = PostgresProviderRegionStore(projector_connection_factory)
        runtime = ProviderRegionalProjectionRuntime(
            redis_client=redis_client,
            dispatch_source=regional_dispatch_source,
            regional_source=regional_source,
            dispatch_projector=RedisProviderRegionalDispatchProjector(
                redis_client,
                sink,
                key_prefix=args.redis_key_prefix,
            ),
            fast_block_projector=RedisProviderRegionalFastBlockProjector(
                redis_client,
                RedisProviderFastBlockProjector(
                    redis_client,
                    key_prefix=args.regional_fast_block_key_prefix,
                    notification_channel=args.regional_fast_block_notification_channel,
                ),
                key_prefix=args.regional_fast_block_key_prefix,
                notification_channel=args.regional_fast_block_notification_channel,
            ),
            capacity_projector=RedisProviderRegionalCapacityProjector(
                redis_client,
                key_prefix=args.regional_capacity_key_prefix,
                notification_channel=args.regional_capacity_notification_channel,
                lease_ttl_seconds=args.regional_capacity_lease_ttl_seconds,
            ),
            projection_store=projector_store,
            capacity_store=projector_store,
            restart_grace_seconds=args.regional_restart_grace_seconds,
        )
        if args.regional_watch:
            ProviderRegionalNotificationConsumer(
                runtime,
                dispatch_channel=args.redis_notification_channel,
                fast_block_channel=args.regional_fast_block_notification_channel,
                capacity_channel=args.regional_capacity_notification_channel,
            ).run_forever(
                redis_client,
                model=args.model,
                region=region,
                reconnect_seconds=args.regional_reconnect_seconds,
                poll_seconds=args.regional_poll_seconds,
            )
            return
        print(
            json.dumps(
                run_regional_model(
                    args.model,
                    enabled=True,
                    publish=True,
                    region=region,
                    runtime=runtime,
                ),
                ensure_ascii=True,
                sort_keys=True,
            )
        )
        return
    print(
        json.dumps(
            run_model(
                args.model,
                enabled=True,
                publish=args.publish,
                source=source,
                sink=sink,
            ),
            ensure_ascii=True,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
