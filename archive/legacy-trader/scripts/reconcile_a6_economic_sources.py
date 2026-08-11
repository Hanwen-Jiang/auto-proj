#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import psycopg
from psycopg.rows import dict_row

from a6api_auto.trade_economic_facts import PostgresChannelRevenueReconciler
from a6api_auto.trade_economic_source import (
    EconomicProjectionBundle,
    EconomicProjectionSink,
    EconomicRoutingConfig,
    EconomicRoutingError,
    PostgresEconomicStore,
    RedisEconomicProjectionSink,
    build_projection_bundle,
)
from a6api_auto.trade_profit_settings import (
    DEFAULT_PROFIT_ON_COST_BPS,
    PostgresModelProfitSettingStore,
)

_TRUE = frozenset({"1", "true", "yes", "on"})
_FALSE = frozenset({"", "0", "false", "no", "off"})


def env_bool(
    name: str,
    *,
    default: bool,
    environ: Mapping[str, str] | None = None,
) -> bool:
    environment = os.environ if environ is None else environ
    if name not in environment:
        return default
    value = str(environment.get(name, "")).strip().casefold()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    raise EconomicRoutingError(f"economic_env_flag_invalid:{name}")


def env_int(
    name: str,
    *,
    default: int,
    environ: Mapping[str, str] | None = None,
) -> int:
    environment = os.environ if environ is None else environ
    value = str(environment.get(name, "")).strip()
    if not value:
        return default
    try:
        return int(value)
    except ValueError:
        raise EconomicRoutingError(f"economic_env_integer_invalid:{name}") from None


def resolve_config(
    environ: Mapping[str, str] | None = None,
) -> EconomicRoutingConfig:
    environment = os.environ if environ is None else environ
    return EconomicRoutingConfig(
        enabled=env_bool("ECONOMIC_PRIORITY1_ENABLED", default=False, environ=environment),
        plan_only=env_bool("ECONOMIC_PLAN_ONLY", default=True, environ=environment),
        projection_apply_enabled=env_bool(
            "ECONOMIC_PROJECTION_APPLY_ENABLED", default=False, environ=environment
        ),
        api_sources_enabled=env_bool(
            "ECONOMIC_API_SOURCES_ENABLED", default=False, environ=environment
        ),
        account_sources_enabled=env_bool(
            "ECONOMIC_ACCOUNT_SOURCES_ENABLED", default=False, environ=environment
        ),
        a6_gateway_enabled=env_bool(
            "ECONOMIC_A6_GATEWAY_ENABLED", default=False, environ=environment
        ),
        minimum_profit_on_cost_bps=env_int(
            "ECONOMIC_MINIMUM_PROFIT_ON_COST_BPS",
            default=DEFAULT_PROFIT_ON_COST_BPS,
            environ=environment,
        ),
        quota_snapshot_max_age_seconds=env_int(
            "ECONOMIC_QUOTA_SNAPSHOT_MAX_AGE_SECONDS", default=60, environ=environment
        ),
        channel_revenue_max_age_seconds=env_int(
            "ECONOMIC_CHANNEL_REVENUE_MAX_AGE_SECONDS", default=30, environ=environment
        ),
        health_max_age_seconds=env_int(
            "ECONOMIC_HEALTH_MAX_AGE_SECONDS", default=30, environ=environment
        ),
        recovery_confirmation_count=env_int(
            "ECONOMIC_RECOVERY_CONFIRMATION_COUNT", default=2, environ=environment
        ),
        cost_quantum=Decimal(str(environment.get("ECONOMIC_COST_QUANTUM", "0.000001")).strip()),
        config_version=str(
            environment.get("ECONOMIC_CONFIG_VERSION", "priority1-economic-v1")
        ).strip(),
    )


def revenue_config(environ: Mapping[str, str] | None = None) -> dict[str, object]:
    """Rolling channel revenue reconcile knobs; separate from eligibility config."""
    environment = os.environ if environ is None else environ
    return {
        "enabled": env_bool(
            "ECONOMIC_CHANNEL_REVENUE_RECONCILE_ENABLED", default=False, environ=environment
        ),
        "fx_cny_per_usd": Decimal(
            str(environment.get("ECONOMIC_FX_CNY_PER_USD", "7.2")).strip()
        ),
        "usage_window_seconds": env_int(
            "ECONOMIC_REVENUE_WINDOW_SECONDS", default=900, environ=environment
        ),
    }


def run_model(
    model: str,
    *,
    reconcile_enabled: bool,
    config: EconomicRoutingConfig,
    store: PostgresEconomicStore | None = None,
    sink: EconomicProjectionSink | None = None,
    redis_key_prefix: str = "a6:economic:priority1:v1",
    now: datetime | None = None,
) -> dict[str, Any]:
    model = str(model).strip()
    if not model:
        raise EconomicRoutingError("economic_model_invalid")
    if not reconcile_enabled:
        return {
            "success": True,
            "model": model,
            "reconcile_enabled": False,
            "action": "disabled",
        }
    if store is None:
        raise EconomicRoutingError("economic_store_missing")
    evaluated_at = now or datetime.now(timezone.utc)
    plan, sources, gateway = store.build_plan(model, config=config, now=evaluated_at)
    activation = store.activate(plan)
    bundle = build_projection_bundle(plan, activation, sources, a6_gateway=gateway)
    state_counts: dict[str, int] = {}
    for record in plan.records:
        state_counts[record.eligibility_state] = state_counts.get(record.eligibility_state, 0) + 1
    result: dict[str, Any] = {
        "success": True,
        "model": model,
        "reconcile_enabled": True,
        "economic_enabled": config.enabled,
        "plan_only": config.plan_only,
        "projection_apply_enabled": config.projection_apply_enabled,
        "minimum_profit_on_cost_bps": config.minimum_profit_on_cost_bps,
        "economic_generation": activation.economic_generation,
        "generation_changed": activation.changed,
        "input_hash": activation.input_hash,
        "output_hash": activation.output_hash,
        "source_count": len(sources),
        "channel_count": len({record.channel_id for record in plan.records}),
        "eligible_channel_count": len(bundle.channels),
        "eligibility_states": dict(sorted(state_counts.items())),
    }
    redis_key = f"{redis_key_prefix}:{model}"
    if not config.projection_apply_enabled:
        store.record_projection_receipt(
            bundle,
            redis_key=redis_key,
            projection_state="plan_only",
        )
        return {**result, "action": "planned", "published": False}
    if sink is None:
        raise EconomicRoutingError("economic_projection_sink_missing")
    desired: EconomicProjectionBundle = bundle
    receipt_state = "projected"
    if not config.enabled:
        desired = EconomicProjectionBundle.tombstone(
            model,
            activation.economic_generation,
            evaluated_at=plan.evaluated_at,
        )
        receipt_state = "withdrawn"
    elif config.plan_only:
        store.record_projection_receipt(
            bundle,
            redis_key=redis_key,
            projection_state="plan_only",
        )
        return {**result, "action": "planned", "published": False}
    try:
        published = sink.publish(desired)
    except Exception as exc:
        store.record_projection_receipt(
            desired,
            redis_key=redis_key,
            projection_state="error",
            last_error_class=getattr(exc, "code", type(exc).__name__),
        )
        raise
    store.record_projection_receipt(
        desired,
        redis_key=published.key,
        projection_state=receipt_state,
        projected_at=evaluated_at,
    )
    return {
        **result,
        "action": "withdrawn" if desired.state == "absent" else "published",
        "published": True,
        "projection_changed": published.changed,
        "projection_digest": published.digest,
        "redis_key": published.key,
    }


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(
        description="Evaluate and project Priority 1 economic source eligibility"
    )
    command.add_argument("--model", required=True)
    command.add_argument("--reconcile-enabled-env", default="ECONOMIC_PRIORITY1_RECONCILE_ENABLED")
    command.add_argument("--database-dsn-env", default="A6_TRADE_DATABASE_DSN")
    command.add_argument("--redis-url-env", default="PROVIDER_DISPATCH_REDIS_URL")
    command.add_argument("--redis-key-prefix", default="a6:economic:priority1:v1")
    command.add_argument(
        "--redis-notification-channel",
        default="a6:economic:priority1:changed:v1",
    )
    return command


def main() -> None:
    args = parser().parse_args()
    reconcile_enabled = env_bool(args.reconcile_enabled_env, default=False)
    config = resolve_config()
    if not reconcile_enabled:
        print(
            json.dumps(
                run_model(
                    args.model,
                    reconcile_enabled=False,
                    config=config,
                ),
                ensure_ascii=True,
                sort_keys=True,
            )
        )
        return
    dsn = str(os.environ.get(args.database_dsn_env, "")).strip()
    if not dsn:
        raise EconomicRoutingError("economic_database_dsn_missing")

    def connection_factory() -> Any:
        return psycopg.connect(dsn, row_factory=dict_row)

    runtime_setting = PostgresModelProfitSettingStore(connection_factory).get(args.model)
    if runtime_setting is not None:
        config = replace(
            config,
            minimum_profit_on_cost_bps=runtime_setting.minimum_profit_on_cost_bps,
        )

    store = PostgresEconomicStore(connection_factory)
    revenue = revenue_config()
    revenue_summary: dict[str, object] = {}
    if revenue["enabled"]:
        reconciler = PostgresChannelRevenueReconciler(connection_factory)
        revenue_summary = reconciler.reconcile(
            args.model,
            now=datetime.now(timezone.utc),
            fx_cny_per_usd=revenue["fx_cny_per_usd"],  # type: ignore[arg-type]
            usage_window_seconds=int(revenue["usage_window_seconds"]),
        )
    sink = None
    if config.projection_apply_enabled:
        redis_url = str(os.environ.get(args.redis_url_env, "")).strip()
        if not redis_url:
            raise EconomicRoutingError("economic_redis_url_missing")
        try:
            import redis
        except ImportError:
            raise EconomicRoutingError("economic_redis_dependency_missing") from None
        sink = RedisEconomicProjectionSink(
            redis.Redis.from_url(
                redis_url,
                decode_responses=True,
                socket_connect_timeout=2,
                socket_timeout=2,
                health_check_interval=30,
            ),
            key_prefix=args.redis_key_prefix,
            notification_channel=args.redis_notification_channel,
        )
    output = run_model(
        args.model,
        reconcile_enabled=True,
        config=config,
        store=store,
        sink=sink,
        redis_key_prefix=args.redis_key_prefix,
    )
    output = {**output, "channel_revenue": revenue_summary}
    print(json.dumps(output, ensure_ascii=True, sort_keys=True))


if __name__ == "__main__":
    main()
