#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import socket
from collections.abc import Callable, Mapping
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row

from a6api_auto.enodot_admin import EnodotAdminClient
from a6api_auto.trade_provider_failure_repair import (
    PostgresProviderFailureRepairStore,
    ProviderFailureRepairClaim,
    ProviderFailureRepairController,
    ProviderFailureRepairError,
    ProviderFailureRepairRun,
    ProviderFailureRepairRuntime,
    ProviderFailureRepairWorker,
)
from a6api_auto.trade_provider_fast_block import RedisProviderFastBlockProjector
from a6api_auto.trade_provider_pool_apply import (
    PostgresProviderRemoteContextSource,
    ProviderPoolApplyPlan,
    ProviderPoolApplyPlanner,
    ProviderPoolApplyPolicy,
    ProviderPoolApplyStage,
)
from a6api_auto.trade_provider_pool_reconciler import (
    PostgresProviderActionLedger,
    PostgresProviderActivationBarrier,
    PostgresProviderModelLease,
    ProviderActionLedger,
    ProviderPoolReconciler,
)
from a6api_auto.trade_provider_pool_shadow_controller import (
    PostgresProviderPoolPlanningAdapter,
    ProviderPoolShadowControllerConfig,
)
from a6api_auto.trade_provider_pool_store import PostgresProviderPoolStore
from a6api_auto.trade_publication_control import PostgresPublicationControl

try:
    from . import apply_a6_provider_pool as apply_runtime
except ImportError:  # pragma: no cover - direct script execution
    import apply_a6_provider_pool as apply_runtime


class ProviderFailureRepairCommandError(ProviderFailureRepairError):
    pass


class _PendingFastBlockReplayer:
    def __init__(self, control: PostgresPublicationControl, *, limit: int = 500) -> None:
        self.control = control
        self.limit = limit

    def __call__(self) -> Mapping[str, Any]:
        result = self.control.project_pending_fast_blocks(
            limit=self.limit,
            stop_on_failure=True,
        )
        if int(result.get("failed") or 0) > 0:
            raise ProviderFailureRepairCommandError(
                "provider_failure_repair_fast_block_projection_failed"
            )
        return result


def build_fast_block_replayer(
    connection_factory: Callable[[], Any],
    *,
    redis_url: str,
    key_prefix: str = "a6:provider:fast-block:v1",
    notification_channel: str = "a6:provider:fast-block:changed:v1",
) -> _PendingFastBlockReplayer:
    redis_url = str(redis_url).strip()
    if not redis_url:
        raise ProviderFailureRepairCommandError(
            "provider_failure_repair_fast_block_redis_url_missing"
        )
    try:
        import redis
    except ImportError:
        raise ProviderFailureRepairCommandError(
            "provider_failure_repair_fast_block_redis_dependency_missing"
        ) from None
    client = redis.Redis.from_url(
        redis_url,
        decode_responses=True,
        socket_connect_timeout=2,
        socket_timeout=2,
        health_check_interval=30,
    )
    return _PendingFastBlockReplayer(
        PostgresPublicationControl(
            connection_factory,
            fast_block_projector=RedisProviderFastBlockProjector(
                client,
                key_prefix=key_prefix,
                notification_channel=notification_channel,
            ),
        )
    )


class _PolicyBoundFailurePlanner:
    """Bind every claimed repair to the reviewed model, config, and apply policy."""

    def __init__(
        self,
        planner: ProviderPoolApplyPlanner,
        *,
        model: str,
        config_version: str,
        apply_policy: ProviderPoolApplyPolicy,
        billing_contract: str,
    ) -> None:
        self.planner = planner
        self.model = model
        self.config_version = config_version
        self.apply_policy = apply_policy
        self.billing_contract = billing_contract

    def plan(self, claim: ProviderFailureRepairClaim) -> ProviderPoolApplyPlan:
        if claim.model != self.model or claim.config_version != self.config_version:
            raise ProviderFailureRepairCommandError("provider_failure_repair_claim_scope_mismatch")
        plan = self.planner.plan()
        if (
            plan.bundle.model != self.model
            or plan.bundle.config_version != self.config_version
            or plan.bundle.generation_source != "failure_repair"
        ):
            raise ProviderFailureRepairCommandError("provider_failure_repair_plan_scope_mismatch")
        self.apply_policy.authorize(
            plan,
            billing_contract=self.billing_contract,
        )
        return plan


def run_model(
    model: str,
    *,
    enabled: bool,
    apply: bool,
    worker_factory: Callable[[], ProviderFailureRepairWorker] | None = None,
) -> tuple[dict[str, Any], int]:
    model = str(model or "").strip()
    if not model:
        raise ProviderFailureRepairCommandError("provider_failure_repair_model_invalid")
    if not isinstance(enabled, bool) or not isinstance(apply, bool):
        raise ProviderFailureRepairCommandError("provider_failure_repair_mode_invalid")
    if not enabled:
        return (
            {
                "success": True,
                "model": model,
                "enabled": False,
                "apply": False,
                "action": "disabled",
                "remote_write_count": 0,
            },
            0,
        )
    if not apply:
        return (
            {
                "success": True,
                "model": model,
                "enabled": True,
                "apply": False,
                "action": "dry-run",
                "remote_write_count": 0,
            },
            0,
        )
    if worker_factory is None:
        raise ProviderFailureRepairCommandError("provider_failure_repair_worker_factory_missing")
    run = worker_factory().run_once()
    return _format_run(model, run), _run_exit_code(run)


def authorize_repair_policy(
    policy: ProviderPoolApplyPolicy,
    *,
    model: str,
    config_version: str,
    billing_contract: str,
) -> str:
    """Perform the policy checks that must pass before the first queue claim."""

    if policy.stage not in {
        ProviderPoolApplyStage.MEMBERSHIP_AND_COST,
        ProviderPoolApplyStage.FULL,
    }:
        raise ProviderFailureRepairCommandError("provider_failure_repair_membership_stage_required")
    if policy.max_membership_changes < 1:
        raise ProviderFailureRepairCommandError("provider_failure_repair_membership_limit_required")
    expected = policy.expected_confirmation(
        model=model,
        config_version=config_version,
        billing_contract=billing_contract,
    )
    if not policy.confirmation or policy.confirmation != expected:
        raise ProviderFailureRepairCommandError("provider_failure_repair_confirmation_mismatch")
    return expected


def build_worker(
    *,
    model: str,
    config: ProviderPoolShadowControllerConfig,
    apply_policy: ProviderPoolApplyPolicy,
    billing_target: apply_runtime.ProviderBillingTargetContract,
    connection_factory: Callable[[], Any],
    redis_url: str,
    redis_key_prefix: str,
    redis_notification_channel: str,
    request_plane_url: str,
    fast_block_redis_key_prefix: str = "a6:provider:fast-block:v1",
    fast_block_redis_notification_channel: str = ("a6:provider:fast-block:changed:v1"),
    sessions_dir: Path,
    proxy_map_file: Path,
    enodot_url: str,
    enodot_admin_key_file: Path,
    worker_id: str,
    stale_claim_seconds: int = 300,
    materialize_limit: int = 100,
    quick_probe_attempts: int = 2,
    max_repair_attempts: int = 8,
    retry_after_seconds: int = 30,
    probe_timeout_seconds: float = 6,
    probe_max_output_tokens: int = 16,
    pin_probe_mode: str = "required",
) -> ProviderFailureRepairWorker:
    pin_probe_mode = apply_runtime.normalize_pin_probe_mode(pin_probe_mode)
    billing_contract = billing_target.confirmation_scope
    planner = _PolicyBoundFailurePlanner(
        ProviderPoolApplyPlanner(
            PostgresProviderPoolPlanningAdapter(connection_factory),
            config,
        ),
        model=model,
        config_version=config.config_version,
        apply_policy=apply_policy,
        billing_contract=billing_contract,
    )
    store = PostgresProviderFailureRepairStore(
        connection_factory,
        stale_claim_seconds=stale_claim_seconds,
        materialize_limit=materialize_limit,
    )
    ledger = PostgresProviderActionLedger(
        connection_factory,
        requested_by=worker_id,
    )
    fast_block_replayer = build_fast_block_replayer(
        connection_factory,
        redis_url=redis_url,
        key_prefix=fast_block_redis_key_prefix,
        notification_channel=fast_block_redis_notification_channel,
    )
    dispatch_projector = None

    def active_dispatch_projector():
        nonlocal dispatch_projector
        if dispatch_projector is None:
            dispatch_projector = apply_runtime.build_dispatch_projector(
                connection_factory,
                redis_url=redis_url,
                key_prefix=redis_key_prefix,
                notification_channel=redis_notification_channel,
            )
        return dispatch_projector

    def publish_active_dispatch(
        active_model: str,
        routing_generation: int,
        cost_generation: int,
    ) -> object:
        return active_dispatch_projector().reconcile(
            active_model,
            expected_routing_generation=routing_generation,
            expected_cost_generation=cost_generation,
        )

    def restore_active_dispatch(
        active_model: str,
        failed_routing_generation: int,
        failed_cost_generation: int,
    ) -> object:
        return active_dispatch_projector().restore(
            active_model,
            expected_current_routing_generation=failed_routing_generation,
            expected_current_cost_generation=failed_cost_generation,
        )

    context_source = PostgresProviderRemoteContextSource(connection_factory)

    def build_remote_for_slots(slots, *, desired_token_ids):
        records = context_source.load(
            model,
            slots,
            desired_token_ids=desired_token_ids,
        )
        admin = EnodotAdminClient(
            enodot_url,
            apply_runtime._private_text(enodot_admin_key_file),
        )
        return apply_runtime.build_remote_adapter_for_slots(
            model,
            slots,
            desired_token_ids,
            records,
            connection_factory=connection_factory,
            sessions_dir=sessions_dir,
            proxy_by_user_id=apply_runtime._proxy_map(proxy_map_file),
            enodot_admin=admin,
            billing_target=billing_target,
            probe_timeout_seconds=probe_timeout_seconds,
            probe_max_output_tokens=probe_max_output_tokens,
            pin_probe_mode=pin_probe_mode,
        )

    def probe_remote_factory(claim: ProviderFailureRepairClaim):
        token_id = claim.current_slot.token_id
        if token_id is None:
            raise ProviderFailureRepairCommandError("provider_failure_repair_probe_token_missing")
        return build_remote_for_slots(
            (claim.current_slot,),
            desired_token_ids=frozenset({token_id}),
        )

    def runtime_factory(
        claim: ProviderFailureRepairClaim,
        plan: ProviderPoolApplyPlan,
        shared_ledger: ProviderActionLedger,
    ) -> ProviderFailureRepairRuntime:
        if shared_ledger is not ledger or claim.model != model:
            raise ProviderFailureRepairCommandError(
                "provider_failure_repair_runtime_scope_mismatch"
            )
        remote = build_remote_for_slots(
            plan.context_slots,
            desired_token_ids=plan.desired_token_ids,
        )
        active_dispatch_projector()
        request_plane_admin = EnodotAdminClient(
            request_plane_url,
            apply_runtime._private_text(enodot_admin_key_file),
            timeout=3,
        )

        def reconciler_factory(
            requested_plan: ProviderPoolApplyPlan,
            requested_ledger: ProviderActionLedger,
        ) -> ProviderPoolReconciler:
            if requested_plan is not plan or requested_ledger is not shared_ledger:
                raise ProviderFailureRepairCommandError(
                    "provider_failure_repair_reconciler_scope_mismatch"
                )
            return ProviderPoolReconciler(
                PostgresProviderPoolStore(connection_factory),
                remote,
                ledger=requested_ledger,
                activation_barrier=PostgresProviderActivationBarrier(connection_factory),
                activation_publisher=publish_active_dispatch,
                activation_compensator=restore_active_dispatch,
                request_plane_preflight=lambda active_model: request_plane_admin.request(
                    "GET",
                    apply_runtime._REQUEST_PLANE_CAPABILITY_PATH,
                    params={"model": active_model},
                ),
                require_request_plane_preflight=True,
                require_remote_readback=True,
                activation_probe_required=pin_probe_mode == "required",
            )

        return ProviderFailureRepairRuntime(remote, reconciler_factory)

    controller = ProviderFailureRepairController(
        store,
        None,
        planner,
        None,
        model_lease=PostgresProviderModelLease(connection_factory),
        runtime_factory=runtime_factory,
        probe_remote_factory=probe_remote_factory,
        ledger=ledger,
        activation_publisher=publish_active_dispatch,
        quick_probe_attempts=quick_probe_attempts,
        quick_probe_enabled=pin_probe_mode == "required",
        max_repair_attempts=max_repair_attempts,
        retry_after_seconds=retry_after_seconds,
    )
    return ProviderFailureRepairWorker(
        store,
        controller,
        worker_id=worker_id,
        fast_block_replayer=fast_block_replayer,
    )


def execute(
    args: argparse.Namespace,
    *,
    environ: Mapping[str, str] | None = None,
    connector: Callable[..., Any] = psycopg.connect,
) -> tuple[dict[str, Any], int]:
    environment = os.environ if environ is None else environ
    enabled = apply_runtime.resolve_enabled(
        False,
        environ=environment,
        env_name=args.enabled_env,
    )
    if not enabled or not args.apply:
        return run_model(
            args.model,
            enabled=enabled,
            apply=args.apply,
        )

    apply_policy = apply_runtime.resolve_apply_policy(
        args.apply_stage,
        args.max_membership_changes,
        args.apply_confirmation,
        environ=environment,
        stage_env=args.apply_stage_env,
        membership_limit_env=args.membership_limit_env,
        confirmation_env=args.apply_confirmation_env,
    )
    billing_target = apply_runtime.resolve_billing_target(
        args.model,
        group_id=args.billing_group_id,
        user_id=args.billing_user_id,
        api_key_ids=args.billing_api_key_ids,
        group_name=args.billing_group_name,
        platform=args.billing_platform,
        required=True,
        group_id_env=args.billing_group_id_env,
        user_id_env=args.billing_user_id_env,
        api_key_ids_env=args.billing_api_key_ids_env,
        group_name_env=args.billing_group_name_env,
        platform_env=args.billing_platform_env,
        environ=environment,
    )
    if billing_target is None:
        raise ProviderFailureRepairCommandError("provider_failure_repair_billing_target_missing")
    authorize_repair_policy(
        apply_policy,
        model=args.model,
        config_version=args.config_version,
        billing_contract=billing_target.confirmation_scope,
    )

    dsn = str(environment.get(args.database_dsn_env, "")).strip()
    if not dsn:
        raise ProviderFailureRepairCommandError("provider_failure_repair_database_dsn_missing")
    redis_url = str(environment.get(args.dispatch_redis_url_env, "")).strip()
    if not redis_url:
        raise ProviderFailureRepairCommandError(
            "provider_failure_repair_dispatch_redis_url_missing"
        )
    request_plane_url = apply_runtime._runtime_text(
        args.request_plane_url,
        env_name=args.request_plane_url_env,
        code="provider_failure_repair_request_plane_url_missing",
        environ=environment,
    )

    def connection_factory() -> Any:
        return connector(dsn, row_factory=dict_row)

    config = ProviderPoolShadowControllerConfig(
        model=args.model,
        config_version=args.config_version,
        generation_source="failure_repair",
        observer_freshness_seconds=args.observer_freshness_seconds,
        inventory_freshness_seconds=args.inventory_freshness_seconds,
        minimum_health_observers=args.minimum_health_observers,
        initial_max_active_slots=args.initial_max_active_slots,
        standby_count=args.standby_count,
        bootstrap_retry_reserve=args.bootstrap_retry_reserve,
        retry_reserve_multiplier=args.retry_reserve_multiplier,
        realized_cost_window_hours=args.realized_cost_window_hours,
        realized_cost_match_tolerance_seconds=(args.realized_cost_match_tolerance_seconds),
        minimum_cost_samples=args.minimum_cost_samples,
        margin_bps=args.margin_bps,
        headroom_bps=args.headroom_bps,
    )
    worker_id = _worker_id(args.worker_id, environment, args.worker_id_env)

    def worker_factory() -> ProviderFailureRepairWorker:
        return build_worker(
            model=args.model,
            config=config,
            apply_policy=apply_policy,
            billing_target=billing_target,
            connection_factory=connection_factory,
            redis_url=redis_url,
            redis_key_prefix=args.dispatch_redis_key_prefix,
            redis_notification_channel=args.dispatch_redis_notification_channel,
            request_plane_url=request_plane_url,
            fast_block_redis_key_prefix=args.fast_block_redis_key_prefix,
            fast_block_redis_notification_channel=(args.fast_block_redis_notification_channel),
            sessions_dir=args.sessions_dir,
            proxy_map_file=args.proxy_map_file,
            enodot_url=args.enodot_url,
            enodot_admin_key_file=args.enodot_admin_key_file,
            worker_id=worker_id,
            stale_claim_seconds=args.stale_claim_seconds,
            materialize_limit=args.materialize_limit,
            quick_probe_attempts=args.quick_probe_attempts,
            max_repair_attempts=args.max_repair_attempts,
            retry_after_seconds=args.retry_after_seconds,
            probe_timeout_seconds=args.probe_timeout_seconds,
            probe_max_output_tokens=args.probe_max_output_tokens,
            pin_probe_mode=args.pin_probe_mode,
        )

    return run_model(
        args.model,
        enabled=True,
        apply=True,
        worker_factory=worker_factory,
    )


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(description="Repair one durable A6 Provider failure request")
    command.add_argument("--model", required=True)
    command.add_argument("--apply", action="store_true")
    command.add_argument(
        "--enabled-env",
        default="PROVIDER_FAILURE_REPAIR_ENABLED",
    )
    command.add_argument("--database-dsn-env", default="A6_TRADE_DATABASE_DSN")
    command.add_argument(
        "--dispatch-redis-url-env",
        default="PROVIDER_DISPATCH_REDIS_URL",
    )
    command.add_argument(
        "--dispatch-redis-key-prefix",
        default="a6:provider:dispatch:v1",
    )
    command.add_argument(
        "--dispatch-redis-notification-channel",
        default="a6:provider:dispatch:changed:v1",
    )
    command.add_argument("--request-plane-url")
    command.add_argument(
        "--request-plane-url-env",
        default="PROVIDER_REQUEST_PLANE_URL",
    )
    command.add_argument(
        "--fast-block-redis-key-prefix",
        default="a6:provider:fast-block:v1",
    )
    command.add_argument(
        "--fast-block-redis-notification-channel",
        default="a6:provider:fast-block:changed:v1",
    )
    command.add_argument("--enodot-url", default="https://ai.enodot.shop")
    command.add_argument(
        "--enodot-admin-key-file",
        type=Path,
        default=Path("/var/lib/a6-trader/secrets/enodot-admin-key.secret"),
    )
    command.add_argument(
        "--sessions-dir",
        type=Path,
        default=Path("/var/lib/a6-trader/secrets/sessions"),
    )
    command.add_argument(
        "--proxy-map-file",
        type=Path,
        default=Path("/var/lib/a6-trader/secrets/proxy-map.json"),
    )
    command.add_argument("--billing-group-id", type=int)
    command.add_argument("--billing-user-id", type=int)
    command.add_argument("--billing-api-key-ids")
    command.add_argument("--billing-group-name")
    command.add_argument("--billing-platform")
    command.add_argument(
        "--billing-group-id-env",
        default="PROVIDER_POOL_BILLING_GROUP_ID",
    )
    command.add_argument(
        "--billing-user-id-env",
        default="PROVIDER_POOL_BILLING_USER_ID",
    )
    command.add_argument(
        "--billing-api-key-ids-env",
        default="PROVIDER_POOL_BILLING_API_KEY_IDS",
    )
    command.add_argument(
        "--billing-group-name-env",
        default="PROVIDER_POOL_BILLING_GROUP_NAME",
    )
    command.add_argument(
        "--billing-platform-env",
        default="PROVIDER_POOL_BILLING_PLATFORM",
    )
    command.add_argument("--apply-stage")
    command.add_argument("--max-membership-changes", type=int)
    command.add_argument("--apply-confirmation")
    command.add_argument("--apply-stage-env", default="PROVIDER_POOL_APPLY_STAGE")
    command.add_argument(
        "--membership-limit-env",
        default="PROVIDER_POOL_MAX_MEMBERSHIP_CHANGES",
    )
    command.add_argument(
        "--apply-confirmation-env",
        default="PROVIDER_POOL_APPLY_CONFIRMATION",
    )
    command.add_argument("--observer-freshness-seconds", type=int, default=120)
    command.add_argument("--inventory-freshness-seconds", type=int, default=900)
    command.add_argument("--minimum-health-observers", type=int, default=2)
    command.add_argument("--initial-max-active-slots", type=int, default=12)
    command.add_argument("--standby-count", type=int, default=12)
    command.add_argument(
        "--bootstrap-retry-reserve",
        type=Decimal,
        default=Decimal("0.2"),
    )
    command.add_argument("--retry-reserve-multiplier", type=Decimal)
    command.add_argument("--realized-cost-window-hours", type=int, default=24)
    command.add_argument(
        "--realized-cost-match-tolerance-seconds",
        type=int,
        default=5,
    )
    command.add_argument("--minimum-cost-samples", type=int, default=100)
    command.add_argument("--margin-bps", type=int, default=1000)
    command.add_argument("--headroom-bps", type=int, default=500)
    command.add_argument("--probe-timeout-seconds", type=float, default=6)
    command.add_argument("--probe-max-output-tokens", type=int, default=16)
    command.add_argument(
        "--pin-probe-mode",
        choices=("required", "disabled"),
        default="required",
    )
    command.add_argument("--quick-probe-attempts", type=int, default=2)
    command.add_argument("--max-repair-attempts", type=int, default=8)
    command.add_argument("--retry-after-seconds", type=int, default=30)
    command.add_argument("--stale-claim-seconds", type=int, default=300)
    command.add_argument("--materialize-limit", type=int, default=100)
    command.add_argument("--worker-id")
    command.add_argument(
        "--worker-id-env",
        default="PROVIDER_FAILURE_REPAIR_WORKER_ID",
    )
    command.add_argument("--config-version", default="a6-provider-topn-failover-v2")
    return command


def main(
    argv: list[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    connector: Callable[..., Any] = psycopg.connect,
) -> int:
    args = parser().parse_args(argv)
    try:
        result, exit_code = execute(
            args,
            environ=environ,
            connector=connector,
        )
    except Exception as exc:
        environment = os.environ if environ is None else environ
        try:
            enabled = apply_runtime.resolve_enabled(
                False,
                environ=environment,
                env_name=args.enabled_env,
            )
        except Exception:
            enabled = False
        result = {
            "success": False,
            "model": str(args.model or "").strip(),
            "enabled": enabled,
            "apply": bool(args.apply),
            "action": "error",
            "error_class": _exception_code(exc),
            "remote_write_count": None,
        }
        exit_code = 1
    print(json.dumps(result, ensure_ascii=True, sort_keys=True))
    return exit_code


def _format_run(model: str, run: ProviderFailureRepairRun) -> dict[str, Any]:
    if run.status not in {"idle", "completed", "pending", "failed"}:
        raise ProviderFailureRepairCommandError("provider_failure_repair_status_invalid")
    target = None
    if run.target is not None:
        target = {
            "routing_generation": run.target.routing_generation,
            "cost_generation": run.target.cost_generation,
            "slot_no": run.target.slot_no,
            "channel_id": run.target.channel_id,
            "account_id": run.target.account_id,
            "token_id": run.target.token_id,
            "source_hash": run.target.source_hash,
        }
    return {
        "success": run.status in {"idle", "completed"},
        "model": model,
        "enabled": True,
        "apply": True,
        "action": run.status,
        "request_id": run.request_id,
        "control_action": run.control_action,
        "attempt_count": run.attempt_count,
        "quick_probe_count": run.quick_probe_count,
        "target": target,
        "error_class": run.error_class,
    }


def _run_exit_code(run: ProviderFailureRepairRun) -> int:
    if run.status in {"idle", "completed"}:
        return 0
    if run.status == "pending":
        return 75
    if run.status == "failed":
        return 1
    raise ProviderFailureRepairCommandError("provider_failure_repair_status_invalid")


def _worker_id(
    cli_value: str | None,
    environment: Mapping[str, str],
    env_name: str,
) -> str:
    value = str(cli_value or environment.get(env_name, "")).strip()
    if value:
        return value
    hostname = re.sub(r"[^A-Za-z0-9_.-]", "_", socket.gethostname())[:80] or "host"
    return f"provider-failure-repair:{hostname}:{os.getpid()}"


def _exception_code(exc: Exception) -> str:
    code = getattr(exc, "code", None)
    value = str(code if isinstance(code, str) and code else type(exc).__name__)
    return value.strip().casefold().replace("-", "_")[:160]


if __name__ == "__main__":
    raise SystemExit(main())
