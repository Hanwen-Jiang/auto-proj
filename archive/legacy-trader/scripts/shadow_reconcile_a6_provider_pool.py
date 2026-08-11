#!/usr/bin/env python3
from __future__ import annotations

import argparse
import fcntl
import json
import os
import stat
import time
from collections.abc import Callable, Mapping
from contextlib import suppress
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row

from a6api_auto.trade_provider_inventory import PostgresProviderInventoryProjector
from a6api_auto.trade_provider_pool_rank import ProviderPoolRankError
from a6api_auto.trade_provider_pool_shadow import reconcile_shadow_document
from a6api_auto.trade_provider_pool_shadow_controller import (
    PostgresProviderPoolPlanningAdapter,
    PostgresProviderPoolPlanStore,
    ProviderPoolShadowControllerConfig,
    ProviderPoolShadowControllerError,
    SingleModelProviderPoolShadowController,
)

_MAX_DOCUMENT_BYTES = 4 * 1024 * 1024
_MAX_AUDIT_RECORD_BYTES = 512 * 1024
_TRUE_VALUES = {"1", "true", "yes", "on"}
_FALSE_VALUES = {"", "0", "false", "no", "off"}
_POSTGRES_APPLICATION_NAME = "a6-provider-pool-shadow"
_POSTGRES_CONNECT_TIMEOUT_SECONDS = 5
_POSTGRES_OPTIONS = (
    "-c statement_timeout=45000 -c lock_timeout=5000 -c idle_in_transaction_session_timeout=50000"
)
_TRANSIENT_SHADOW_FAILURE_CODES = frozenset(
    {
        "provider_snapshot_stale",
        "provider_snapshot_observer_quorum_insufficient",
        "provider_snapshot_out_of_order",
        "provider_shadow_controller_snapshot_missing",
        "provider_shadow_controller_snapshot_incomplete",
        "provider_shadow_controller_snapshot_candidate_count_mismatch",
    }
)


class ProviderPoolShadowCommandError(RuntimeError):
    pass


class ProviderPoolShadowRetryExhausted(ProviderPoolShadowCommandError):
    def __init__(self, failure: Exception, attempt_count: int) -> None:
        self.failure_type = type(failure).__name__
        self.failure_code = shadow_failure_code(failure)
        self.attempt_count = attempt_count
        super().__init__(self.failure_code)


def shadow_failure_code(exc: Exception) -> str:
    if isinstance(exc, ProviderPoolShadowRetryExhausted):
        return exc.failure_code
    value = str(exc).strip()
    if (
        1 <= len(value) <= 128
        and value.startswith("provider_")
        and all(
            character.isascii() and (character.isalnum() or character == "_") for character in value
        )
    ):
        return value
    return type(exc).__name__


def shadow_failure_details(exc: Exception) -> tuple[str, str, int]:
    if isinstance(exc, ProviderPoolShadowRetryExhausted):
        return exc.failure_type, exc.failure_code, exc.attempt_count
    return type(exc).__name__, shadow_failure_code(exc), 1


def append_shadow_audit_record(
    path: Path,
    result: Mapping[str, Any],
    *,
    observed_at: datetime | None = None,
) -> None:
    if not isinstance(result, Mapping):
        raise ProviderPoolShadowCommandError("provider_shadow_audit_record_invalid")
    timestamp = observed_at or datetime.now(timezone.utc)
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ProviderPoolShadowCommandError("provider_shadow_audit_record_invalid")
    timestamp = timestamp.astimezone(timezone.utc)
    try:
        message = json.dumps(
            dict(result),
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        record = json.dumps(
            {
                "__REALTIME_TIMESTAMP": str(int(timestamp.timestamp() * 1_000_000)),
                "MESSAGE": message,
            },
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        payload = f"{record}\n".encode("ascii")
    except (TypeError, ValueError, OverflowError):
        raise ProviderPoolShadowCommandError("provider_shadow_audit_record_invalid") from None
    if len(payload) > _MAX_AUDIT_RECORD_BYTES:
        raise ProviderPoolShadowCommandError("provider_shadow_audit_record_invalid")

    candidate = path.expanduser()
    if not candidate.is_absolute() or not candidate.name:
        raise ProviderPoolShadowCommandError("provider_shadow_audit_path_invalid")
    try:
        resolved = candidate.parent.resolve(strict=True) / candidate.name
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
        flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(resolved, flags, 0o600)
        try:
            info = os.fstat(descriptor)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) & 0o077
            ):
                raise OSError("audit file permissions invalid")
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            original_size = os.fstat(descriptor).st_size
            try:
                remaining = memoryview(payload)
                while remaining:
                    written = os.write(descriptor, remaining)
                    if written <= 0:
                        raise OSError("audit file write incomplete")
                    remaining = remaining[written:]
                os.fsync(descriptor)
            except OSError:
                # A full filesystem may accept only a prefix. Preserve the last
                # complete JSONL boundary before a later run appends its record.
                with suppress(OSError):
                    os.ftruncate(descriptor, original_size)
                    os.fsync(descriptor)
                raise
        finally:
            os.close(descriptor)
    except OSError:
        raise ProviderPoolShadowCommandError("provider_shadow_audit_write_failed") from None


def _postgres_connection_factory(dsn: str) -> Any:
    if not str(dsn).strip():
        raise ProviderPoolShadowCommandError("provider_shadow_database_dsn_missing")

    def connect() -> Any:
        return psycopg.connect(
            dsn,
            row_factory=dict_row,
            application_name=_POSTGRES_APPLICATION_NAME,
            connect_timeout=_POSTGRES_CONNECT_TIMEOUT_SECONDS,
            options=_POSTGRES_OPTIONS,
        )

    return connect


def resolve_inventory_projection_enabled(
    *,
    environ: Mapping[str, str] | None = None,
    env_name: str = "PROVIDER_INVENTORY_RECONCILE_ENABLED",
) -> bool:
    environment = os.environ if environ is None else environ
    value = str(environment.get(env_name, "")).strip().casefold()
    if value in _TRUE_VALUES:
        return True
    if value in _FALSE_VALUES:
        return False
    raise ProviderPoolShadowCommandError("provider_inventory_flag_invalid")


def _document(path: Path) -> dict[str, Any]:
    resolved = path.expanduser().resolve()
    try:
        info = resolved.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_DOCUMENT_BYTES:
            raise ProviderPoolShadowCommandError("provider_shadow_document_invalid")
        value = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ProviderPoolShadowCommandError("provider_shadow_document_invalid") from None
    if not isinstance(value, Mapping):
        raise ProviderPoolShadowCommandError("provider_shadow_document_invalid")
    return dict(value)


def run(
    document: Mapping[str, Any],
    *,
    persist: bool,
    connection_factory: Any | None = None,
) -> dict[str, Any]:
    if not isinstance(persist, bool):
        raise ProviderPoolShadowCommandError("provider_shadow_persist_invalid")
    if persist and not callable(connection_factory):
        raise ProviderPoolShadowCommandError("provider_shadow_connection_factory_missing")
    store = PostgresProviderPoolPlanStore(connection_factory) if persist else None
    return reconcile_shadow_document(document, store=store, persist=persist).summary()


def run_model(
    model: str,
    *,
    persist: bool,
    connection_factory: Any,
    now: datetime | None = None,
    config: ProviderPoolShadowControllerConfig | None = None,
    inventory_projector: Any | None = None,
    project_inventory: bool = False,
) -> dict[str, Any]:
    if not callable(connection_factory):
        raise ProviderPoolShadowCommandError("provider_shadow_connection_factory_missing")
    planning = config or ProviderPoolShadowControllerConfig(model=str(model))
    if planning.model != str(model):
        raise ProviderPoolShadowCommandError("provider_shadow_model_config_mismatch")
    if not isinstance(project_inventory, bool):
        raise ProviderPoolShadowCommandError("provider_inventory_flag_invalid")
    projection = None
    if persist and project_inventory:
        projector = inventory_projector or PostgresProviderInventoryProjector(connection_factory)
        projection = projector.reconcile(str(model))
    source = PostgresProviderPoolPlanningAdapter(connection_factory)
    store = PostgresProviderPoolPlanStore(connection_factory) if persist else None
    controller = SingleModelProviderPoolShadowController(source, planning, store=store)
    result = controller.run(
        now=now or datetime.now(timezone.utc),
        persist=persist,
    ).summary()
    if projection is not None:
        result["inventory_projection"] = projection.summary()
    elif persist:
        result["inventory_projection"] = {"enabled": False}
    return result


def run_model_with_retry(
    model: str,
    *,
    persist: bool,
    connection_factory: Any,
    now: datetime | None = None,
    config: ProviderPoolShadowControllerConfig | None = None,
    inventory_projector: Any | None = None,
    project_inventory: bool = False,
    retry_attempts: int = 3,
    retry_delay_seconds: float = 3.0,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[dict[str, Any], int]:
    if (
        isinstance(retry_attempts, bool)
        or not isinstance(retry_attempts, int)
        or not 1 <= retry_attempts <= 10
    ):
        raise ProviderPoolShadowCommandError("provider_shadow_retry_attempts_invalid")
    if (
        isinstance(retry_delay_seconds, bool)
        or not isinstance(retry_delay_seconds, (int, float))
        or not 0 <= retry_delay_seconds <= 30
    ):
        raise ProviderPoolShadowCommandError("provider_shadow_retry_delay_invalid")
    if not callable(sleep):
        raise ProviderPoolShadowCommandError("provider_shadow_retry_sleep_invalid")

    for attempt_count in range(1, retry_attempts + 1):
        try:
            result = run_model(
                model,
                persist=persist,
                connection_factory=connection_factory,
                now=now,
                config=config,
                inventory_projector=inventory_projector,
                project_inventory=project_inventory,
            )
            return result, attempt_count
        except (ProviderPoolRankError, ProviderPoolShadowControllerError) as exc:
            failure_code = shadow_failure_code(exc)
            if (
                failure_code not in _TRANSIENT_SHADOW_FAILURE_CODES
                or attempt_count >= retry_attempts
            ):
                if attempt_count == 1:
                    raise
                raise ProviderPoolShadowRetryExhausted(
                    exc,
                    attempt_count,
                ) from exc
            sleep(float(retry_delay_seconds))

    raise AssertionError("unreachable")


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(
        description="Validate or persist a plan-only A6 Provider pool shadow generation"
    )
    source = command.add_mutually_exclusive_group(required=True)
    source.add_argument("--bundle-file", type=Path)
    source.add_argument(
        "--model",
        help="build one model directly from PostgreSQL Observer/inventory evidence",
    )
    command.add_argument(
        "--persist",
        action="store_true",
        help="persist PostgreSQL planned generations; remote writes and activation remain disabled",
    )
    command.add_argument("--database-dsn-env", default="A6_TRADE_DATABASE_DSN")
    command.add_argument("--observer-freshness-seconds", type=int, default=120)
    command.add_argument("--inventory-freshness-seconds", type=int, default=900)
    command.add_argument("--minimum-health-observers", type=int, default=2)
    command.add_argument(
        "--size-mode",
        choices=("all_funded", "evidence_based"),
        default="all_funded",
    )
    command.add_argument("--initial-max-active-slots", type=int, default=12)
    command.add_argument("--minimum-active-slots", type=int, default=4)
    command.add_argument("--safe-concurrency-per-slot", type=int, default=1)
    command.add_argument("--target-utilization", type=Decimal, default=Decimal("0.7"))
    command.add_argument("--minimum-pool-observation-days", type=int, default=14)
    command.add_argument(
        "--minimum-pool-shrink-interval-hours",
        type=int,
        default=72,
    )
    command.add_argument("--standby-count", type=int, default=12)
    command.add_argument("--bootstrap-retry-reserve", type=Decimal, default=Decimal("0.2"))
    command.add_argument("--retry-reserve-multiplier", type=Decimal)
    command.add_argument("--realized-cost-window-hours", type=int, default=24)
    command.add_argument("--realized-cost-match-tolerance-seconds", type=int, default=5)
    command.add_argument("--minimum-cost-samples", type=int, default=100)
    command.add_argument("--margin-bps", type=int, default=1000)
    command.add_argument("--headroom-bps", type=int, default=500)
    command.add_argument(
        "--maximum-provider-cost-cny",
        type=Decimal,
        default=Decimal("0.3"),
    )
    command.add_argument("--config-version", default="a6-provider-topn-failover-v2")
    command.add_argument("--transient-retry-attempts", type=int, default=3)
    command.add_argument(
        "--transient-retry-delay-seconds",
        type=float,
        default=3.0,
    )
    command.add_argument(
        "--audit-log-file",
        type=Path,
        help="append journal-compatible, credential-free shadow evidence",
    )
    return command


def main() -> None:
    args = parser().parse_args()
    attempt_count = 1
    try:
        connection_factory = None
        if args.persist or args.model is not None:
            dsn = os.environ.get(args.database_dsn_env, "").strip()
            connection_factory = _postgres_connection_factory(dsn)
        if args.model is not None:
            result, attempt_count = run_model_with_retry(
                args.model,
                persist=args.persist,
                connection_factory=connection_factory,
                project_inventory=resolve_inventory_projection_enabled(),
                config=ProviderPoolShadowControllerConfig(
                    model=args.model,
                    observer_freshness_seconds=args.observer_freshness_seconds,
                    inventory_freshness_seconds=args.inventory_freshness_seconds,
                    minimum_health_observers=args.minimum_health_observers,
                    size_mode=args.size_mode,
                    initial_max_active_slots=args.initial_max_active_slots,
                    minimum_active_slots=args.minimum_active_slots,
                    safe_concurrency_per_slot=args.safe_concurrency_per_slot,
                    target_utilization=args.target_utilization,
                    minimum_pool_observation_days=(args.minimum_pool_observation_days),
                    minimum_pool_shrink_interval_hours=(args.minimum_pool_shrink_interval_hours),
                    standby_count=args.standby_count,
                    bootstrap_retry_reserve=args.bootstrap_retry_reserve,
                    retry_reserve_multiplier=args.retry_reserve_multiplier,
                    realized_cost_window_hours=args.realized_cost_window_hours,
                    realized_cost_match_tolerance_seconds=(
                        args.realized_cost_match_tolerance_seconds
                    ),
                    minimum_cost_samples=args.minimum_cost_samples,
                    margin_bps=args.margin_bps,
                    headroom_bps=args.headroom_bps,
                    maximum_provider_cost_cny=args.maximum_provider_cost_cny,
                    config_version=args.config_version,
                ),
                retry_attempts=args.transient_retry_attempts,
                retry_delay_seconds=args.transient_retry_delay_seconds,
            )
        else:
            result = run(
                _document(args.bundle_file),
                persist=args.persist,
                connection_factory=connection_factory,
            )
    except Exception as exc:
        if args.audit_log_file is not None and args.model is not None:
            failure_type, failure_code, attempt_count = shadow_failure_details(exc)
            failure = {
                "success": False,
                "mode": "shadow",
                "model": args.model,
                "activated": False,
                "plan_write_count": 0,
                "remote_write_count": 0,
                "failure_type": failure_type,
                "failure_code": failure_code,
                "attempt_count": attempt_count,
            }
            with suppress(ProviderPoolShadowCommandError):
                append_shadow_audit_record(args.audit_log_file, failure)
        raise
    if args.model is not None:
        result["attempt_count"] = attempt_count
    if args.audit_log_file is not None:
        append_shadow_audit_record(args.audit_log_file, result)
    print(json.dumps(result, ensure_ascii=True, sort_keys=True))


if __name__ == "__main__":
    main()
