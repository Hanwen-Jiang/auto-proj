#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import stat
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import nullcontext
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row

from a6api_auto.client import A6ApiClient
from a6api_auto.enodot_admin import EnodotAdminClient
from a6api_auto.storage import SessionStore, read_private_json
from a6api_auto.trade_provider_auto_pool import (
    ProviderAutoPoolTarget,
    reconcile_provider_owned_channel_ban,
)
from a6api_auto.trade_provider_auto_pool_store import PostgresProviderAutoPoolStore
from a6api_auto.trade_provider_cost import PoolDiffKind
from a6api_auto.trade_provider_dispatch import (
    PostgresProviderDispatchSource,
    ProviderDispatchProjector,
    ProviderDispatchPublishResult,
    RedisProviderDispatchSink,
)
from a6api_auto.trade_provider_inventory import PostgresProviderInventoryProjector
from a6api_auto.trade_provider_pool import ProviderSlot, SlotState
from a6api_auto.trade_provider_pool_apply import (
    PostgresProviderRemoteContextSource,
    ProviderPoolApplyError,
    ProviderPoolApplyPlan,
    ProviderPoolApplyPlanner,
    ProviderPoolApplyPolicy,
    ProviderPoolApplyStage,
    ProviderRemoteContextRecord,
)
from a6api_auto.trade_provider_pool_reconciler import (
    PostgresProviderActionLedger,
    PostgresProviderActivationBarrier,
    PostgresProviderModelLease,
    ProviderPlatformBackoffActive,
    ProviderPoolReconciler,
    ProviderReadbackRetryRequired,
    ProviderRoundDeadlineExceeded,
    ReplanRequired,
)
from a6api_auto.trade_provider_pool_remote import (
    EnodotBillingFloorTarget,
    EnodotGroupUserBillingFloor,
    PostgresProviderPinBindingStore,
    PostgresProviderProbeHistory,
    ProviderPoolRemoteAdapter,
    ProviderRemoteSlotContext,
)
from a6api_auto.trade_provider_pool_shadow_controller import (
    PostgresProviderPoolPlanningAdapter,
    ProviderPoolShadowControllerConfig,
)
from a6api_auto.trade_provider_pool_store import PostgresProviderPoolStore
from a6api_auto.workflow import Workflow

_DIRECT_A6_ORIGIN = "https://a6.a6api.com"
_REQUEST_PLANE_CAPABILITY_PATH = "admin/internal/a6-provider/runtime-capabilities"
_MINIMUM_PROVIDER_MARGIN_BPS = 2_000
_TRUE_VALUES = {"1", "true", "yes", "on"}
_FALSE_VALUES = {"", "0", "false", "no", "off"}


class ProviderPoolApplyCommandError(ProviderPoolApplyError):
    pass


@dataclass(frozen=True)
class ProviderBillingTargetContract:
    """Exact Enodot scope affected by the group-user billing override."""

    model: str
    group_id: int
    user_id: int
    affected_api_key_ids: tuple[int, ...]
    group_name: str
    platform: str

    def __post_init__(self) -> None:
        model = str(self.model or "").strip()
        group_name = str(self.group_name or "").strip()
        platform = str(self.platform or "").strip().casefold()
        keys = tuple(
            sorted(
                {
                    _positive_id(value, "provider_apply_billing_key_invalid")
                    for value in self.affected_api_key_ids
                }
            )
        )
        if not model or not group_name or not platform or not keys:
            raise ProviderPoolApplyCommandError("provider_apply_billing_contract_invalid")
        object.__setattr__(self, "model", model)
        object.__setattr__(self, "group_name", group_name)
        object.__setattr__(self, "platform", platform)
        object.__setattr__(self, "affected_api_key_ids", keys)
        _positive_id(self.group_id, "provider_apply_billing_group_invalid")
        _positive_id(self.user_id, "provider_apply_billing_user_invalid")

    @property
    def confirmation_scope(self) -> str:
        keys = ",".join(str(value) for value in self.affected_api_key_ids)
        return (
            f"billing={self.model}/group-{self.group_id}/user-{self.user_id}"
            f"/keys-{keys}/name-{self.group_name}/platform-{self.platform}"
        )


def resolve_enabled(
    cli_enabled: bool,
    *,
    environ: Mapping[str, str] | None = None,
    env_name: str = "PROVIDER_POOL_RECONCILE_ENABLED",
) -> bool:
    if not isinstance(cli_enabled, bool):
        raise ProviderPoolApplyCommandError("provider_apply_flag_invalid")
    if cli_enabled:
        return True
    environment = os.environ if environ is None else environ
    value = str(environment.get(env_name, "")).strip().casefold()
    if value in _TRUE_VALUES:
        return True
    if value in _FALSE_VALUES:
        return False
    raise ProviderPoolApplyCommandError("provider_apply_flag_invalid")


def normalize_pin_probe_mode(value: object) -> str:
    mode = str(value or "").strip().casefold()
    if mode not in {"required", "disabled"}:
        raise ProviderPoolApplyCommandError("provider_apply_pin_probe_mode_invalid")
    return mode


def run_model(
    model: str,
    *,
    enabled: bool,
    apply: bool,
    required_ready_slots: int | None = None,
    minimum_ready_slots: int = 1,
    planner: ProviderPoolApplyPlanner | None = None,
    reconciler_factory: Callable[[ProviderPoolApplyPlan], ProviderPoolReconciler] | None = None,
    dispatch_projector: ProviderDispatchProjector | None = None,
    apply_policy: ProviderPoolApplyPolicy | None = None,
    billing_contract: str = "billing-unconfigured",
    pin_failover_attempts: int = 12,
    probe_failure_handler: Callable[[ProviderPoolApplyPlan, ReplanRequired], None]
    | None = None,
    platform_backoff_waiter: Callable[[float], None] | None = None,
) -> dict[str, Any]:
    model = str(model or "").strip()
    if not model:
        raise ProviderPoolApplyCommandError("provider_apply_model_invalid")
    if not isinstance(enabled, bool) or not isinstance(apply, bool):
        raise ProviderPoolApplyCommandError("provider_apply_mode_invalid")
    if (
        isinstance(minimum_ready_slots, bool)
        or not isinstance(minimum_ready_slots, int)
        or minimum_ready_slots < 0
    ):
        raise ProviderPoolApplyCommandError("provider_apply_minimum_ready_slots_invalid")
    if not enabled:
        return {
            "success": True,
            "model": model,
            "enabled": False,
            "apply": False,
            "action": "disabled",
            "activated": False,
            "remote_write_count": 0,
        }
    if planner is None:
        raise ProviderPoolApplyCommandError("provider_apply_planner_missing")
    if platform_backoff_waiter is not None and not callable(platform_backoff_waiter):
        raise ProviderPoolApplyCommandError("provider_apply_backoff_waiter_invalid")
    if (
        isinstance(pin_failover_attempts, bool)
        or not isinstance(pin_failover_attempts, int)
        or not 1 <= pin_failover_attempts <= 100
    ):
        raise ProviderPoolApplyCommandError(
            "provider_apply_pin_failover_attempts_invalid"
        )
    policy = apply_policy or ProviderPoolApplyPolicy(
        ProviderPoolApplyStage.DISABLED,
        0,
        "",
    )
    failed_pins: list[dict[str, int]] = []
    failed_providers: list[dict[str, Any]] = []
    rejected_provider_identities: set[tuple[int, int]] = set()
    while True:
        plan = planner.plan()
        if plan.bundle.model != model:
            raise ProviderPoolApplyCommandError("provider_apply_plan_model_mismatch")
        if apply:
            _require_complete_ready_slots(
                plan,
                required_ready_slots,
                minimum_ready_slots=minimum_ready_slots,
            )
            _assert_minimum_provider_profit(plan)
        required_confirmation = policy.expected_confirmation(
            model=plan.bundle.model,
            config_version=plan.bundle.config_version,
            billing_contract=billing_contract,
        )
        result = {
            **plan.summary(),
            "enabled": True,
            "apply": apply,
            "apply_stage": policy.stage.value,
            "max_membership_changes": policy.max_membership_changes,
            "changed_slot_nos": list(plan.changed_slot_nos),
            "required_confirmation": required_confirmation,
            "dispatch_published": False,
            "pin_failover_count": len(failed_pins),
            "failed_pins": list(failed_pins),
            "provider_failover_count": len(failed_providers),
            "failed_providers": list(failed_providers),
            "verified_slot_nos": list(getattr(plan, "verified_slot_nos", ())),
            "applying_replan_count": len(
                tuple(getattr(planner, "applying_replans", ()))
            ),
            "applying_replans": [
                {
                    "routing_generation": int(routing_generation),
                    "reason": str(reason),
                }
                for routing_generation, reason in tuple(
                    getattr(planner, "applying_replans", ())
                )
            ],
        }
        if not apply:
            return {
                **result,
                "action": "noop" if plan.diff_kind is PoolDiffKind.NOOP else "planned",
            }
        policy.authorize(plan, billing_contract=billing_contract)
        if plan.diff_kind is PoolDiffKind.NOOP:
            active = getattr(plan, "active", None)
            if not plan.billing_finalize_required:
                return {
                    **result,
                    "action": "noop",
                    **_project_active_dispatch(
                        dispatch_projector,
                        model=model,
                        routing_generation=(
                            None if active is None else active.routing_generation
                        ),
                        cost_generation=(
                            None if active is None else active.cost_generation
                        ),
                    ),
                }
            if reconciler_factory is None or active is None:
                raise ProviderPoolApplyCommandError(
                    "provider_apply_reconciler_factory_missing"
                )
            actions = reconciler_factory(plan).finalize_active_billing(
                plan.bundle,
                routing_generation=active.routing_generation,
                cost_generation=active.cost_generation,
            )
            return {
                **result,
                "action": "billing-finalized",
                "persisted": True,
                "remote_write_count": len(actions),
                "actions": list(actions),
                **_project_active_dispatch(
                    dispatch_projector,
                    model=model,
                    routing_generation=active.routing_generation,
                    cost_generation=active.cost_generation,
                ),
            }
        if reconciler_factory is None:
            raise ProviderPoolApplyCommandError(
                "provider_apply_reconciler_factory_missing"
            )
        reconciler = reconciler_factory(plan)
        try:
            report = reconciler.reconcile(
                plan.bundle,
                current_routing=plan.current_routing,
                current_assignments=plan.current_assignments,
                current_slots=plan.current_slots,
                current_cost_input_hash=plan.current_cost_input_hash,
                verified_slot_nos=getattr(plan, "verified_slot_nos", ()),
            )
        except ProviderPlatformBackoffActive as exc:
            observation = dict(exc.observation)
            document = observation.get("document")
            retry_after_ms = (
                document.get("retry_after_ms")
                if isinstance(document, Mapping)
                else None
            )
            return {
                **result,
                "action": "platform-backoff",
                "persisted": False,
                "activated": False,
                "remote_write_count": 0,
                "request_sent": False,
                "retry_after_ms": retry_after_ms,
                "probe": observation,
            }
        except ProviderReadbackRetryRequired as exc:
            return {
                **result,
                "action": "readback-retry",
                "persisted": False,
                "activated": False,
                "remote_write_count": None,
                "retry_error": exc.code,
                "readback_diagnostics": dict(exc.diagnostics),
            }
        except ProviderRoundDeadlineExceeded as exc:
            return {
                **result,
                "action": "round-deadline",
                "persisted": False,
                "activated": False,
                "remote_write_count": None,
                "retry_error": exc.code,
            }
        except ReplanRequired as exc:
            slot_no = exc.slot_no
            channel_id = exc.channel_id
            account_id = exc.account_id
            token_id = exc.token_id
            scope = exc.replan_scope
            if scope in {"platform", "internal", "unknown"}:
                return {
                    **result,
                    "action": "apply-retry",
                    "persisted": False,
                    "activated": False,
                    "remote_write_count": None,
                    "retry_error": exc.code,
                    "retry_scope": scope,
                    "probe": dict(exc.probe_observation),
                }
            reject_channel = getattr(planner, "reject_channel", None)
            reject_provider = getattr(planner, "reject_provider", None)
            retain_verified_slots = getattr(planner, "retain_verified_slots", None)
            fail_applying = getattr(reconciler, "fail_applying", None)
            if slot_no is None or not callable(fail_applying):
                raise ProviderPoolApplyCommandError(
                    "provider_apply_pin_failover_context_missing"
                ) from exc
            fail_applying(plan.bundle, reason=exc.code)
            if exc.verified_slot_nos:
                if not callable(retain_verified_slots):
                    raise ProviderPoolApplyCommandError(
                        "provider_apply_verified_slot_context_missing"
                    ) from exc
                retain_verified_slots(plan, exc.verified_slot_nos)
            if exc.replan_scope == "channel":
                if channel_id is None or not callable(reject_channel):
                    raise ProviderPoolApplyCommandError(
                        "provider_apply_channel_failover_context_missing"
                    ) from exc
                duplicate_channel = any(
                    item["channel_id"] == channel_id for item in failed_pins
                )
                if duplicate_channel:
                    raise ProviderPoolApplyCommandError(
                        "provider_apply_pin_failover_no_progress"
                    ) from exc
                failed_pins.append(
                    {
                        "slot_no": slot_no,
                        "channel_id": channel_id,
                    }
                )
                if len(failed_pins) + len(failed_providers) >= pin_failover_attempts:
                    raise ProviderPoolApplyCommandError(
                        "provider_apply_pin_candidates_exhausted"
                    ) from exc
                reject_channel(channel_id)
                continue
            if scope in {"liquidity", "provider"}:
                if (
                    account_id is None
                    or token_id is None
                    or not callable(reject_provider)
                ):
                    raise ProviderPoolApplyCommandError(
                        "provider_apply_provider_failover_context_missing"
                    ) from exc
                identity = (account_id, token_id)
                if identity in rejected_provider_identities:
                    raise ProviderPoolApplyCommandError(
                        "provider_apply_provider_failover_no_progress"
                    ) from exc
                if (
                    probe_failure_handler is not None
                ):
                    probe_failure_handler(plan, exc)
                failed_providers.append(
                    {
                        "slot_no": slot_no,
                        "account_id": account_id,
                        "token_id": token_id,
                        "channel_id": channel_id,
                        "replan_scope": scope,
                        "probe": dict(exc.probe_observation),
                    }
                )
                if len(failed_pins) + len(failed_providers) >= pin_failover_attempts:
                    raise ProviderPoolApplyCommandError(
                        "provider_apply_provider_candidates_exhausted"
                    ) from exc
                reject_provider(account_id, token_id)
                rejected_provider_identities.add(identity)
                continue
            raise ProviderPoolApplyCommandError(
                "provider_apply_probe_scope_invalid"
            ) from exc
        if (
            report.model != model
            or report.diff_kind is not plan.diff_kind
            or report.routing_generation != plan.bundle.routing_generation
            or report.cost_generation != plan.bundle.cost_generation
            or not report.activated
        ):
            raise ProviderPoolApplyCommandError("provider_apply_report_mismatch")
        publication = getattr(report, "dispatch_publication", None)
        if publication is None:
            dispatch = _project_active_dispatch(
                dispatch_projector,
                model=model,
                routing_generation=report.routing_generation,
                cost_generation=report.cost_generation,
            )
        else:
            dispatch = _format_dispatch_publication(publication)
        return {
            **result,
            "action": "activated",
            "persisted": True,
            "activated": True,
            "remote_write_count": len(report.actions),
            "actions": list(report.actions),
            **dispatch,
        }


def _require_complete_ready_slots(
    plan: ProviderPoolApplyPlan,
    required_ready_slots: int | None,
    *,
    minimum_ready_slots: int = 1,
) -> None:
    try:
        bundle = plan.bundle
        desired_size = int(bundle.desired_size)
        slots = tuple(bundle.slots)
        ordered_channel_ids = tuple(bundle.ordered_channel_ids)
        ready_count = sum(slot.slot_state is SlotState.READY for slot in slots)
        vacant_count = sum(slot.slot_state is SlotState.VACANT for slot in slots)
    except (AttributeError, TypeError, ValueError):
        raise ProviderPoolApplyCommandError("provider_apply_required_ready_slots_invalid") from None
    if required_ready_slots is not None and (
        isinstance(required_ready_slots, bool)
        or not isinstance(required_ready_slots, int)
        or required_ready_slots < 1
    ):
        raise ProviderPoolApplyCommandError("provider_apply_required_ready_slots_invalid")
    if (
        isinstance(minimum_ready_slots, bool)
        or not isinstance(minimum_ready_slots, int)
        or minimum_ready_slots < 0
    ):
        raise ProviderPoolApplyCommandError("provider_apply_minimum_ready_slots_invalid")
    if (
        desired_size < minimum_ready_slots
        or len(slots) != desired_size
        or ready_count < minimum_ready_slots
        or (
            required_ready_slots is not None
            and ready_count != required_ready_slots
        )
        or len(ordered_channel_ids) > desired_size
        or len(ordered_channel_ids) != len(set(ordered_channel_ids))
        or ready_count + vacant_count != desired_size
    ):
        raise ProviderPoolApplyCommandError("provider_apply_required_ready_slots_unavailable")


def _ready_slot_count(plan: ProviderPoolApplyPlan) -> int:
    try:
        return sum(slot.slot_state is SlotState.READY for slot in plan.bundle.slots)
    except (AttributeError, TypeError):
        raise ProviderPoolApplyCommandError("provider_apply_required_ready_slots_invalid") from None


def _project_active_dispatch(
    projector: ProviderDispatchProjector | None,
    *,
    model: str,
    routing_generation: int | None,
    cost_generation: int | None,
) -> dict[str, Any]:
    if projector is None:
        return {"dispatch_published": False}
    if routing_generation is None or cost_generation is None:
        raise ProviderPoolApplyCommandError("provider_apply_dispatch_generation_missing")
    published: ProviderDispatchPublishResult = projector.reconcile(
        model,
        expected_routing_generation=routing_generation,
        expected_cost_generation=cost_generation,
    )
    return _format_dispatch_publication(published)


def _format_dispatch_publication(
    published: ProviderDispatchPublishResult | object,
) -> dict[str, Any]:
    try:
        key = str(published.key)
        digest = str(published.digest)
        changed = published.changed
        routing_generation = int(published.routing_generation)
        cost_generation = int(published.cost_generation)
        state = str(published.state)
    except (AttributeError, TypeError, ValueError):
        raise ProviderPoolApplyCommandError("provider_apply_dispatch_publication_invalid") from None
    if (
        not key
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest.casefold())
        or not isinstance(changed, bool)
        or routing_generation < 0
        or cost_generation < 0
        or state not in {"active", "absent"}
    ):
        raise ProviderPoolApplyCommandError("provider_apply_dispatch_publication_invalid")
    return {
        "dispatch_published": True,
        "dispatch_changed": changed,
        "dispatch_key": key,
        "dispatch_digest": digest.casefold(),
        "dispatch_routing_generation": routing_generation,
        "dispatch_cost_generation": cost_generation,
        "dispatch_state": state,
    }


def build_remote_adapter(
    plan: ProviderPoolApplyPlan,
    records: Sequence[ProviderRemoteContextRecord],
    *,
    connection_factory: Callable[[], Any],
    sessions_dir: Path,
    proxy_by_user_id: Mapping[int, str],
    enodot_admin: EnodotAdminClient,
    billing_target: ProviderBillingTargetContract,
    probe_timeout_seconds: float = 24,
    probe_connect_timeout_seconds: float = 8,
    probe_max_output_tokens: int = 16,
    pin_probe_mode: str = "required",
    remote_max_workers: int = 1,
    remote_readback_attempts: int = 1,
    remote_request_timeout_seconds: float = 30,
) -> ProviderPoolRemoteAdapter:
    compensation_token_ids = frozenset(
        int(slot.token_id)
        for slot in plan.context_slots
        if slot.slot_state is not SlotState.VACANT and slot.token_id is not None
    )
    return build_remote_adapter_for_slots(
        plan.bundle.model,
        plan.context_slots,
        compensation_token_ids,
        records,
        connection_factory=connection_factory,
        sessions_dir=sessions_dir,
        proxy_by_user_id=proxy_by_user_id,
        enodot_admin=enodot_admin,
        billing_target=billing_target,
        probe_timeout_seconds=probe_timeout_seconds,
        probe_connect_timeout_seconds=probe_connect_timeout_seconds,
        probe_max_output_tokens=probe_max_output_tokens,
        pin_probe_mode=pin_probe_mode,
        remote_max_workers=remote_max_workers,
        remote_readback_attempts=remote_readback_attempts,
        remote_request_timeout_seconds=remote_request_timeout_seconds,
    )


def build_remote_adapter_for_slots(
    model: str,
    slots: Sequence[ProviderSlot],
    desired_token_ids: frozenset[int],
    records: Sequence[ProviderRemoteContextRecord],
    *,
    connection_factory: Callable[[], Any],
    sessions_dir: Path,
    proxy_by_user_id: Mapping[int, str],
    enodot_admin: EnodotAdminClient,
    billing_target: ProviderBillingTargetContract,
    probe_timeout_seconds: float = 24,
    probe_connect_timeout_seconds: float = 8,
    probe_max_output_tokens: int = 16,
    pin_probe_mode: str = "required",
    remote_max_workers: int = 1,
    remote_readback_attempts: int = 1,
    remote_request_timeout_seconds: float = 30,
) -> ProviderPoolRemoteAdapter:
    pin_probe_mode = normalize_pin_probe_mode(pin_probe_mode)
    root = sessions_dir.expanduser().resolve()
    direct_probe = _direct_probe_client(probe_timeout_seconds)
    contexts = tuple(
        _slot_context(
            record,
            sessions_dir=root,
            proxy_by_user_id=proxy_by_user_id,
            require_secret=record.token_ref_id in desired_token_ids,
            request_timeout_seconds=remote_request_timeout_seconds,
        )
        for record in records
    )
    expected_tokens = {
        int(slot.token_id)
        for slot in slots
        if slot.slot_state is not SlotState.VACANT and slot.token_id is not None
    }
    if {context.token_ref_id for context in contexts} != expected_tokens:
        raise ProviderPoolApplyCommandError("provider_apply_runtime_context_mismatch")
    if billing_target.model != model:
        raise ProviderPoolApplyCommandError("provider_apply_billing_model_mismatch")
    _validate_billing_target(enodot_admin, billing_target)
    billing = EnodotGroupUserBillingFloor(
        enodot_admin,
        {
            model: EnodotBillingFloorTarget(
                billing_target.group_id,
                billing_target.user_id,
            )
        },
        group_lock=enodot_admin.postgres_group_rate_lock(connection_factory),
    )
    def pre_enable_guard(context: ProviderRemoteSlotContext) -> None:
        if context.panel_client is None:
            raise ProviderPoolApplyCommandError("provider_apply_owned_channel_ban_client_missing")
        reconcile_provider_owned_channel_ban(
            context.panel_client,
            ProviderAutoPoolTarget(
                context.a6_user_id,
                context.external_token_id,
                model,
            ),
            PostgresProviderAutoPoolStore(connection_factory).owned_resources(
                model,
                merchant_id=context.merchant_id,
            ),
            page_size=100,
            owned_listings_by_channel=PostgresProviderAutoPoolStore(
                connection_factory
            ).owned_listings_by_channel(model, merchant_id=context.merchant_id),
        )

    return ProviderPoolRemoteAdapter(
        model,
        enodot_admin,
        billing,
        PostgresProviderPinBindingStore(connection_factory),
        direct_probe,
        contexts,
        pre_enable_guard=pre_enable_guard,
        probe_timeout_seconds=probe_timeout_seconds,
        probe_connect_timeout_seconds=probe_connect_timeout_seconds,
        probe_max_output_tokens=probe_max_output_tokens,
        probe_history=PostgresProviderProbeHistory(connection_factory),
        activation_probe_mode=pin_probe_mode,
        readback_max_workers=remote_max_workers,
        readback_max_attempts=remote_readback_attempts,
    )


def build_reconciler(
    plan: ProviderPoolApplyPlan,
    records: Sequence[ProviderRemoteContextRecord],
    *,
    connection_factory: Callable[[], Any],
    sessions_dir: Path,
    proxy_by_user_id: Mapping[int, str],
    enodot_admin: EnodotAdminClient,
    request_plane_admin: EnodotAdminClient,
    billing_target: ProviderBillingTargetContract,
    dispatch_projector: ProviderDispatchProjector | None = None,
    probe_timeout_seconds: float = 24,
    probe_connect_timeout_seconds: float = 8,
    probe_max_output_tokens: int = 16,
    pin_probe_mode: str = "required",
    remote_max_workers: int = 1,
    remote_readback_attempts: int = 1,
    remote_request_timeout_seconds: float = 30,
    round_deadline: float | None = None,
    defer_membership_to_auto_pool: bool = False,
) -> ProviderPoolReconciler:
    pin_probe_mode = normalize_pin_probe_mode(pin_probe_mode)
    _assert_minimum_provider_profit(plan)
    if dispatch_projector is None:
        raise ProviderPoolApplyCommandError("provider_apply_dispatch_projector_missing")
    remote = build_remote_adapter(
        plan,
        records,
        connection_factory=connection_factory,
        sessions_dir=sessions_dir,
        proxy_by_user_id=proxy_by_user_id,
        enodot_admin=enodot_admin,
        billing_target=billing_target,
        probe_timeout_seconds=probe_timeout_seconds,
        probe_connect_timeout_seconds=probe_connect_timeout_seconds,
        probe_max_output_tokens=probe_max_output_tokens,
        pin_probe_mode=pin_probe_mode,
        remote_max_workers=remote_max_workers,
        remote_readback_attempts=remote_readback_attempts,
        remote_request_timeout_seconds=remote_request_timeout_seconds,
    )
    return ProviderPoolReconciler(
        PostgresProviderPoolStore(connection_factory),
        remote,
        ledger=PostgresProviderActionLedger(connection_factory),
        model_lease=PostgresProviderModelLease(connection_factory),
        activation_barrier=PostgresProviderActivationBarrier(connection_factory),
        activation_publisher=(
            lambda model, routing_generation, cost_generation: dispatch_projector.reconcile(
                model,
                expected_routing_generation=routing_generation,
                expected_cost_generation=cost_generation,
            )
        ),
        activation_compensator=(
            lambda model, routing_generation, cost_generation: dispatch_projector.restore(
                model,
                expected_current_routing_generation=routing_generation,
                expected_current_cost_generation=cost_generation,
            )
        ),
        request_plane_preflight=lambda model: request_plane_admin.request(
            "GET",
            _REQUEST_PLANE_CAPABILITY_PATH,
            params={"model": model},
        ),
        require_request_plane_preflight=True,
        require_remote_readback=True,
        activation_probe_required=pin_probe_mode == "required",
        defer_membership_to_auto_pool=defer_membership_to_auto_pool,
        remote_max_workers=remote_max_workers,
        round_deadline=round_deadline,
    )


def record_provider_probe_failure(
    plan: ProviderPoolApplyPlan,
    failure: ReplanRequired,
    *,
    connection_factory: Callable[[], Any],
    dispatch_projector: ProviderDispatchProjector | None = None,
) -> None:
    """Persist account-owned Probe evidence before selecting a replacement."""

    if failure.replan_scope not in {"liquidity", "provider"}:
        raise ProviderPoolApplyCommandError("provider_apply_probe_record_scope_invalid")
    if failure.slot_no is None or failure.account_id is None or failure.token_id is None:
        raise ProviderPoolApplyCommandError("provider_apply_probe_record_identity_missing")
    slot = next(
        (
            item
            for item in plan.bundle.ready_slots
            if item.slot_no == failure.slot_no
            and item.account_id == failure.account_id
            and item.token_id == failure.token_id
        ),
        None,
    )
    if slot is None or slot.enodot_binding_id is None:
        raise ProviderPoolApplyCommandError("provider_apply_probe_record_identity_mismatch")
    error_code = str(
        failure.probe_observation.get("error_code") or "provider_probe_failed"
    ).strip()[:128]
    connection = connection_factory()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE a6_trade.enodot_account_bindings
                SET last_error_class = %s, last_error_at = now(), updated_at = now()
                WHERE id = %s AND enodot_account_id = %s AND a6_token_id = %s
                  AND state = 'bound'
                """,
                (
                    error_code,
                    slot.enodot_binding_id,
                    failure.account_id,
                    failure.token_id,
                ),
            )
            if cursor.rowcount != 1:
                raise ProviderPoolApplyCommandError(
                    "provider_apply_probe_binding_record_mismatch"
                )
            cursor.execute(
                """
                UPDATE a6_trade.provider_inventory
                SET inventory_state = 'REPAIRING', funding_eligible = false,
                    schedulable = false, state_version = state_version + 1,
                    updated_at = now()
                WHERE model = %s AND enodot_binding_id = %s
                  AND enodot_account_id = %s AND a6_token_id = %s
                """,
                (
                    plan.bundle.model,
                    slot.enodot_binding_id,
                    failure.account_id,
                    failure.token_id,
                ),
            )
            if cursor.rowcount != 1:
                raise ProviderPoolApplyCommandError(
                    "provider_apply_probe_inventory_record_mismatch"
                )
            cursor.execute(
                """
                UPDATE a6_trade.provider_pool_slots
                SET slot_state = 'REPAIRING', funding_eligible = false,
                    desired_schedulable = false, observed_schedulable = false,
                    probe_confirmed = false, state_version = state_version + 1,
                    updated_at = now()
                WHERE model = %s AND enodot_account_id = %s
                  AND a6_token_id = %s AND valid_to IS NULL
                  AND slot_state <> 'VACANT'
                """,
                (plan.bundle.model, failure.account_id, failure.token_id),
            )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        close = getattr(connection, "close", None)
        if callable(close):
            close()

    active = plan.active
    if dispatch_projector is not None and active is not None:
        dispatch_projector.reconcile(
            plan.bundle.model,
            expected_routing_generation=active.routing_generation,
            expected_cost_generation=active.cost_generation,
        )


def _assert_minimum_provider_profit(plan: ProviderPoolApplyPlan) -> None:
    try:
        cost_plan = plan.bundle.cost_plan
        pool_cost = Decimal(cost_plan.pool_cost_multiplier)
        billing_floor = Decimal(cost_plan.billing_floor_multiplier)
        margin_bps = int(cost_plan.margin_bps)
    except (AttributeError, TypeError, ValueError):
        raise ProviderPoolApplyCommandError("provider_apply_minimum_profit_invalid") from None
    minimum_floor = pool_cost * (Decimal(10_000 + _MINIMUM_PROVIDER_MARGIN_BPS) / Decimal(10_000))
    if margin_bps < _MINIMUM_PROVIDER_MARGIN_BPS or billing_floor < minimum_floor:
        raise ProviderPoolApplyCommandError("provider_apply_minimum_profit_not_met")


def _slot_context(
    context: ProviderRemoteContextRecord,
    *,
    sessions_dir: Path,
    proxy_by_user_id: Mapping[int, str],
    require_secret: bool = True,
    request_timeout_seconds: float = 30,
    round_deadline: float | None = None,
) -> ProviderRemoteSlotContext:
    if not isinstance(require_secret, bool):
        raise ProviderPoolApplyCommandError("provider_apply_context_secret_mode_invalid")
    if not require_secret:
        return ProviderRemoteSlotContext(
            token_ref_id=context.token_ref_id,
            external_token_id=context.external_token_id,
            key_fingerprint=context.key_fingerprint,
            merchant_id=context.merchant_id,
            a6_user_id=context.a6_user_id,
            enodot_binding_id=context.enodot_binding_id,
            enodot_account_id=context.enodot_account_id,
            panel_client=None,
            probe_api_key="",
        )
    session_path = Path(context.session_ref).expanduser().resolve()
    if not session_path.is_relative_to(sessions_dir):
        raise ProviderPoolApplyCommandError("provider_apply_session_ref_outside_root")
    store = SessionStore(session_path)
    record = store.load()
    if int(record.user_id or 0) != context.a6_user_id:
        raise ProviderPoolApplyCommandError("provider_apply_session_identity_mismatch")
    api_key = str(record.a6api_key or "").strip()
    fingerprint = hashlib.sha256(api_key.encode()).hexdigest()
    if not api_key or not hmac.compare_digest(fingerprint, context.key_fingerprint):
        raise ProviderPoolApplyCommandError("provider_apply_probe_key_fingerprint_mismatch")
    if context.key_secret_ref != f"{context.session_ref}#a6api_key":
        raise ProviderPoolApplyCommandError("provider_apply_key_secret_ref_mismatch")
    proxy_url = str(proxy_by_user_id.get(context.a6_user_id) or "").strip()
    if not proxy_url:
        raise ProviderPoolApplyCommandError("provider_apply_panel_proxy_missing")
    panel = _LazyAuthenticatedPanelClient(
        A6ApiClient(
            record.origin,
            proxy_url=proxy_url,
            timeout=request_timeout_seconds,
        ),
        store,
        record,
        expected_user_id=context.a6_user_id,
        round_deadline=round_deadline,
    )
    return ProviderRemoteSlotContext(
        token_ref_id=context.token_ref_id,
        external_token_id=context.external_token_id,
        key_fingerprint=context.key_fingerprint,
        merchant_id=context.merchant_id,
        a6_user_id=context.a6_user_id,
        enodot_binding_id=context.enodot_binding_id,
        enodot_account_id=context.enodot_account_id,
        panel_client=panel,
        probe_api_key=api_key,
    )


def _validate_billing_target(
    admin: EnodotAdminClient,
    target: ProviderBillingTargetContract,
) -> None:
    user = admin.get_user(target.user_id)
    if (
        int(user.get("id") or 0) != target.user_id
        or str(user.get("status") or "").strip().casefold() != "active"
    ):
        raise ProviderPoolApplyCommandError("provider_apply_billing_user_mismatch")
    document = admin.request("GET", f"admin/users/{target.user_id}/api-keys")
    items = document.get("items") if isinstance(document, Mapping) else None
    if not isinstance(items, Sequence) or isinstance(items, (str, bytes)):
        raise ProviderPoolApplyCommandError("provider_apply_billing_keys_invalid")
    scoped: dict[int, Mapping[str, Any]] = {}
    for raw in items:
        if not isinstance(raw, Mapping):
            raise ProviderPoolApplyCommandError("provider_apply_billing_keys_invalid")
        if (
            int(raw.get("user_id") or 0) == target.user_id
            and int(raw.get("group_id") or 0) == target.group_id
            and str(raw.get("status") or "").strip().casefold() == "active"
        ):
            key_id = _positive_id(raw.get("id"), "provider_apply_billing_keys_invalid")
            if key_id in scoped:
                raise ProviderPoolApplyCommandError("provider_apply_billing_keys_invalid")
            scoped[key_id] = raw
    if tuple(sorted(scoped)) != target.affected_api_key_ids:
        raise ProviderPoolApplyCommandError("provider_apply_billing_key_scope_mismatch")
    for item in scoped.values():
        group = item.get("group")
        if not isinstance(group, Mapping) or (
            int(group.get("id") or 0) != target.group_id
            or str(group.get("name") or "").strip() != target.group_name
            or str(group.get("platform") or "").strip().casefold() != target.platform
            or str(group.get("status") or "").strip().casefold() != "active"
        ):
            raise ProviderPoolApplyCommandError("provider_apply_billing_group_mismatch")


class _LazyAuthenticatedPanelClient:
    """Authenticate panel sessions only when an A6 panel operation is required."""

    def __init__(
        self,
        client: A6ApiClient,
        store: SessionStore,
        record: Any,
        *,
        expected_user_id: int,
        round_deadline: float | None = None,
    ) -> None:
        self.client = client
        self.store = store
        self.record = record
        self.expected_user_id = expected_user_id
        self.round_deadline = round_deadline
        self.authenticated = False

    @property
    def proxy_url(self) -> str:
        return self.client.proxy_url

    def marketplace_pins(self, user_id: int) -> list[dict[str, Any]]:
        with self._deadline_scope():
            self._authenticate(user_id)
            return self.client.marketplace_pins(user_id)

    def marketplace_pin(self, user_id: int, **kwargs: Any) -> dict[str, Any]:
        with self._deadline_scope():
            self._authenticate(user_id)
            return self.client.marketplace_pin(user_id, **kwargs)

    def _deadline_scope(self) -> Any:
        if self.round_deadline is None:
            return nullcontext()
        if time.monotonic() >= self.round_deadline:
            raise ProviderRoundDeadlineExceeded()
        return self.client.request_deadline(self.round_deadline)

    def _authenticate(self, user_id: int) -> None:
        if user_id != self.expected_user_id:
            raise ProviderPoolApplyCommandError("provider_apply_panel_identity_mismatch")
        if self.authenticated:
            return
        if str(self.record.status or "") == "allocated":
            raise ProviderPoolApplyCommandError("provider_apply_session_not_registered")
        Workflow(self.client, _NoMailbox(), self.store).ensure_authenticated(self.record)
        if int(self.record.user_id or 0) != self.expected_user_id:
            raise ProviderPoolApplyCommandError("provider_apply_authenticated_identity_mismatch")
        self.authenticated = True


class _NoMailbox:
    def snapshot(self, _email: str) -> set[str]:
        raise ProviderPoolApplyCommandError("provider_apply_mailbox_forbidden")

    def poll_code(self, *_args: Any, **_kwargs: Any) -> str:
        raise ProviderPoolApplyCommandError("provider_apply_mailbox_forbidden")


def _direct_probe_client(timeout: float) -> A6ApiClient:
    client = A6ApiClient(_DIRECT_A6_ORIGIN, proxy_url="", timeout=timeout)
    if client.origin != _DIRECT_A6_ORIGIN or client.proxy_url:
        raise ProviderPoolApplyCommandError("provider_apply_probe_client_not_direct")
    return client


def _private_text(path: Path) -> str:
    resolved = path.expanduser().resolve()
    try:
        info = resolved.stat()
        value = resolved.read_text(encoding="utf-8").strip()
    except OSError:
        raise ProviderPoolApplyCommandError("provider_apply_admin_key_unreadable") from None
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or not value:
        raise ProviderPoolApplyCommandError("provider_apply_admin_key_invalid")
    return value


def _proxy_map(path: Path) -> dict[int, str]:
    try:
        document = read_private_json(path)
        result = {int(key): str(value).strip() for key, value in document.items()}
    except (TypeError, ValueError):
        raise ProviderPoolApplyCommandError("provider_apply_proxy_map_invalid") from None
    if any(user_id < 1 or not value for user_id, value in result.items()):
        raise ProviderPoolApplyCommandError("provider_apply_proxy_map_invalid")
    return result


def _runtime_id(
    value: int | None,
    *,
    env_name: str,
    environ: Mapping[str, str] | None = None,
) -> int:
    environment = os.environ if environ is None else environ
    raw: object = value if value is not None else environment.get(env_name)
    return _positive_id(raw, "provider_apply_billing_target_missing")


def resolve_apply_policy(
    stage: str | None,
    max_membership_changes: int | None,
    confirmation: str | None,
    *,
    environ: Mapping[str, str] | None = None,
    stage_env: str = "PROVIDER_POOL_APPLY_STAGE",
    membership_limit_env: str = "PROVIDER_POOL_MAX_MEMBERSHIP_CHANGES",
    confirmation_env: str = "PROVIDER_POOL_APPLY_CONFIRMATION",
) -> ProviderPoolApplyPolicy:
    environment = os.environ if environ is None else environ
    raw_stage = stage if stage is not None else environment.get(stage_env, "DISABLED")
    try:
        parsed_stage = ProviderPoolApplyStage(str(raw_stage or "").strip().upper())
    except ValueError:
        raise ProviderPoolApplyCommandError("provider_apply_stage_invalid") from None
    raw_limit: object = (
        max_membership_changes
        if max_membership_changes is not None
        else environment.get(membership_limit_env, "0")
    )
    if isinstance(raw_limit, bool):
        raise ProviderPoolApplyCommandError("provider_apply_membership_limit_invalid")
    try:
        parsed_limit = int(raw_limit)
    except (TypeError, ValueError):
        raise ProviderPoolApplyCommandError("provider_apply_membership_limit_invalid") from None
    parsed_confirmation = str(
        confirmation if confirmation is not None else environment.get(confirmation_env, "")
    ).strip()
    return ProviderPoolApplyPolicy(
        parsed_stage,
        parsed_limit,
        parsed_confirmation,
    )


def _runtime_text(
    value: str | None,
    *,
    env_name: str,
    code: str,
    environ: Mapping[str, str] | None = None,
) -> str:
    environment = os.environ if environ is None else environ
    result = str(value if value is not None else environment.get(env_name, "")).strip()
    if not result:
        raise ProviderPoolApplyCommandError(code)
    return result


def _runtime_ids(
    value: str | None,
    *,
    env_name: str,
    environ: Mapping[str, str] | None = None,
) -> tuple[int, ...]:
    raw = _runtime_text(
        value,
        env_name=env_name,
        code="provider_apply_billing_keys_missing",
        environ=environ,
    )
    try:
        values = tuple(
            _positive_id(item.strip(), "provider_apply_billing_key_invalid")
            for item in raw.split(",")
        )
    except ProviderPoolApplyCommandError:
        raise
    if not values or len(values) != len(set(values)):
        raise ProviderPoolApplyCommandError("provider_apply_billing_key_invalid")
    return tuple(sorted(values))


def resolve_billing_target(
    model: str,
    *,
    group_id: int | None,
    user_id: int | None,
    api_key_ids: str | None,
    group_name: str | None,
    platform: str | None,
    required: bool,
    group_id_env: str = "PROVIDER_POOL_BILLING_GROUP_ID",
    user_id_env: str = "PROVIDER_POOL_BILLING_USER_ID",
    api_key_ids_env: str = "PROVIDER_POOL_BILLING_API_KEY_IDS",
    group_name_env: str = "PROVIDER_POOL_BILLING_GROUP_NAME",
    platform_env: str = "PROVIDER_POOL_BILLING_PLATFORM",
    environ: Mapping[str, str] | None = None,
) -> ProviderBillingTargetContract | None:
    """Resolve an exact billing scope for apply or an explicitly scoped dry-run."""

    if not isinstance(required, bool):
        raise ProviderPoolApplyCommandError("provider_apply_billing_mode_invalid")
    environment = os.environ if environ is None else environ
    direct_values = (group_id, user_id, api_key_ids, group_name, platform)
    env_names = (
        group_id_env,
        user_id_env,
        api_key_ids_env,
        group_name_env,
        platform_env,
    )
    requested = (
        required
        or any(value is not None for value in direct_values)
        or any(str(environment.get(name, "")).strip() for name in env_names)
    )
    if not requested:
        return None
    return ProviderBillingTargetContract(
        model=model,
        group_id=_runtime_id(
            group_id,
            env_name=group_id_env,
            environ=environment,
        ),
        user_id=_runtime_id(
            user_id,
            env_name=user_id_env,
            environ=environment,
        ),
        affected_api_key_ids=_runtime_ids(
            api_key_ids,
            env_name=api_key_ids_env,
            environ=environment,
        ),
        group_name=_runtime_text(
            group_name,
            env_name=group_name_env,
            code="provider_apply_billing_group_name_missing",
            environ=environment,
        ),
        platform=_runtime_text(
            platform,
            env_name=platform_env,
            code="provider_apply_billing_platform_missing",
            environ=environment,
        ),
    )


def _positive_id(value: object, code: str) -> int:
    if isinstance(value, bool):
        raise ProviderPoolApplyCommandError(code)
    try:
        result = int(value)
    except (TypeError, ValueError):
        raise ProviderPoolApplyCommandError(code) from None
    if result < 1:
        raise ProviderPoolApplyCommandError(code)
    return result


def build_dispatch_projector(
    connection_factory: Callable[[], Any],
    *,
    redis_url: str,
    key_prefix: str,
    notification_channel: str,
) -> ProviderDispatchProjector:
    redis_url = str(redis_url).strip()
    if not redis_url:
        raise ProviderPoolApplyCommandError("provider_apply_dispatch_redis_url_missing")
    try:
        import redis
    except ImportError:
        raise ProviderPoolApplyCommandError(
            "provider_apply_dispatch_redis_dependency_missing"
        ) from None
    client = redis.Redis.from_url(
        redis_url,
        decode_responses=True,
        socket_connect_timeout=2,
        socket_timeout=2,
        health_check_interval=30,
    )
    return ProviderDispatchProjector(
        PostgresProviderDispatchSource(connection_factory),
        RedisProviderDispatchSink(
            client,
            key_prefix=key_prefix,
            notification_channel=notification_channel,
        ),
    )


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(
        description="Plan or apply one A6 Provider routing/cost generation"
    )
    command.add_argument("--model", required=True)
    command.add_argument("--apply", action="store_true")
    command.add_argument(
        "--provider-pool-reconcile-enabled",
        action="store_true",
        help="explicitly enable this default-off controller",
    )
    command.add_argument("--enabled-env", default="PROVIDER_POOL_RECONCILE_ENABLED")
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
        help="A6 panel proxy map; never used by the Provider Responses probe",
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
    command.add_argument("--minimum-active-slots", type=int, default=4)
    command.add_argument("--pin-failover-attempts", type=int, default=12)
    command.add_argument("--required-ready-slots", type=int)
    command.add_argument("--standby-count", type=int, default=12)
    command.add_argument("--bootstrap-retry-reserve", type=Decimal, default=Decimal("0.2"))
    command.add_argument("--retry-reserve-multiplier", type=Decimal)
    command.add_argument("--realized-cost-window-hours", type=int, default=24)
    command.add_argument("--realized-cost-match-tolerance-seconds", type=int, default=5)
    command.add_argument("--minimum-cost-samples", type=int, default=100)
    command.add_argument(
        "--margin-bps",
        type=int,
        default=_MINIMUM_PROVIDER_MARGIN_BPS,
    )
    command.add_argument("--headroom-bps", type=int, default=500)
    command.add_argument(
        "--maximum-provider-cost-cny",
        type=Decimal,
        default=Decimal("0.3"),
    )
    command.add_argument("--probe-timeout-seconds", type=float, default=24)
    command.add_argument("--probe-connect-timeout-seconds", type=float, default=8)
    command.add_argument("--probe-max-output-tokens", type=int, default=16)
    command.add_argument("--remote-max-workers", type=int, default=1)
    command.add_argument("--remote-readback-attempts", type=int, default=2)
    command.add_argument("--remote-request-timeout-seconds", type=float, default=5)
    command.add_argument("--round-time-budget-seconds", type=float, default=45)
    command.add_argument(
        "--pin-probe-mode",
        choices=("required", "disabled"),
        default="required",
    )
    command.add_argument(
        "--defer-membership-to-auto-pool",
        action="store_true",
        help="keep changed Provider identities unschedulable until auto-pool activation",
    )
    command.add_argument(
        "--auto-pool-enabled-env",
        default="PROVIDER_AUTO_POOL_ENABLED",
    )
    command.add_argument("--config-version", default="a6-provider-topn-failover-v2")
    return command


def main() -> None:
    args = parser().parse_args()
    if not 1 <= args.remote_max_workers <= 32:
        raise ProviderPoolApplyCommandError("provider_apply_remote_workers_invalid")
    if not 1 <= args.remote_readback_attempts <= 5:
        raise ProviderPoolApplyCommandError("provider_apply_readback_attempts_invalid")
    if (
        not 1 <= args.remote_request_timeout_seconds <= 30
        or not 10 <= args.round_time_budget_seconds <= 300
        or args.remote_request_timeout_seconds > args.round_time_budget_seconds
    ):
        raise ProviderPoolApplyCommandError("provider_apply_round_timing_invalid")
    round_deadline = time.monotonic() + args.round_time_budget_seconds
    enabled = resolve_enabled(
        args.provider_pool_reconcile_enabled,
        env_name=args.enabled_env,
    )
    if not enabled:
        print(
            json.dumps(
                run_model(args.model, enabled=False, apply=args.apply),
                ensure_ascii=True,
                sort_keys=True,
            )
        )
        return
    if args.apply and args.defer_membership_to_auto_pool:
        auto_pool_enabled = str(
            os.environ.get(args.auto_pool_enabled_env, "")
        ).strip().casefold()
        if auto_pool_enabled not in _TRUE_VALUES:
            raise ProviderPoolApplyCommandError(
                "provider_apply_auto_pool_defer_not_enabled"
            )
    dsn = str(os.environ.get(args.database_dsn_env, "")).strip()
    if not dsn:
        raise ProviderPoolApplyCommandError("provider_apply_database_dsn_missing")

    def connection_factory() -> Any:
        return psycopg.connect(dsn, row_factory=dict_row)

    if args.apply:
        PostgresProviderInventoryProjector(connection_factory).reconcile(args.model)

    dispatch_projector = None
    request_plane_url = None
    if args.apply:
        dispatch_projector = build_dispatch_projector(
            connection_factory,
            redis_url=os.environ.get(args.dispatch_redis_url_env, ""),
            key_prefix=args.dispatch_redis_key_prefix,
            notification_channel=args.dispatch_redis_notification_channel,
        )
        request_plane_url = _runtime_text(
            args.request_plane_url,
            env_name=args.request_plane_url_env,
            code="provider_apply_request_plane_url_missing",
        )

    config = ProviderPoolShadowControllerConfig(
        model=args.model,
        observer_freshness_seconds=args.observer_freshness_seconds,
        inventory_freshness_seconds=args.inventory_freshness_seconds,
        minimum_health_observers=args.minimum_health_observers,
        initial_max_active_slots=args.initial_max_active_slots,
        minimum_active_slots=args.minimum_active_slots,
        standby_count=args.standby_count,
        bootstrap_retry_reserve=args.bootstrap_retry_reserve,
        retry_reserve_multiplier=args.retry_reserve_multiplier,
        realized_cost_window_hours=args.realized_cost_window_hours,
        realized_cost_match_tolerance_seconds=(args.realized_cost_match_tolerance_seconds),
        minimum_cost_samples=args.minimum_cost_samples,
        margin_bps=args.margin_bps,
        headroom_bps=args.headroom_bps,
        maximum_provider_cost_cny=args.maximum_provider_cost_cny,
        config_version=args.config_version,
    )
    provider_store = PostgresProviderPoolStore(connection_factory)
    planner = ProviderPoolApplyPlanner(
        PostgresProviderPoolPlanningAdapter(connection_factory),
        config,
        applying_replan_handler=(
            None
            if not args.apply
            else lambda projection, reason: provider_store.fail_applying_projection(
                args.model,
                projection,
                reason=reason,
            )
        ),
        applying_bundle_loader=(
            None
            if not args.apply
            else lambda projection: provider_store.load_applying_bundle(
                args.model,
                routing_generation=projection.routing_generation,
                cost_generation=projection.cost_generation,
            )
        ),
    )
    apply_policy = resolve_apply_policy(
        args.apply_stage,
        args.max_membership_changes,
        args.apply_confirmation,
        stage_env=args.apply_stage_env,
        membership_limit_env=args.membership_limit_env,
        confirmation_env=args.apply_confirmation_env,
    )
    billing_target = resolve_billing_target(
        args.model,
        group_id=args.billing_group_id,
        user_id=args.billing_user_id,
        api_key_ids=args.billing_api_key_ids,
        group_name=args.billing_group_name,
        platform=args.billing_platform,
        required=args.apply,
        group_id_env=args.billing_group_id_env,
        user_id_env=args.billing_user_id_env,
        api_key_ids_env=args.billing_api_key_ids_env,
        group_name_env=args.billing_group_name_env,
        platform_env=args.billing_platform_env,
    )
    reconciler_factory = None
    if args.apply:

        def reconciler_factory(plan: ProviderPoolApplyPlan) -> ProviderPoolReconciler:
            context_source = PostgresProviderRemoteContextSource(connection_factory)
            records = context_source.load(
                plan.bundle.model,
                plan.context_slots,
                desired_token_ids=plan.desired_token_ids,
            )
            admin = EnodotAdminClient(
                args.enodot_url,
                _private_text(args.enodot_admin_key_file),
                timeout=args.remote_request_timeout_seconds,
            )
            request_plane_admin = EnodotAdminClient(
                request_plane_url,
                _private_text(args.enodot_admin_key_file),
                timeout=min(3, args.remote_request_timeout_seconds),
            )
            return build_reconciler(
                plan,
                records,
                connection_factory=connection_factory,
                sessions_dir=args.sessions_dir,
                proxy_by_user_id=_proxy_map(args.proxy_map_file),
                enodot_admin=admin,
                request_plane_admin=request_plane_admin,
                billing_target=billing_target,
                dispatch_projector=dispatch_projector,
                probe_timeout_seconds=args.probe_timeout_seconds,
                probe_connect_timeout_seconds=args.probe_connect_timeout_seconds,
                probe_max_output_tokens=args.probe_max_output_tokens,
                pin_probe_mode=args.pin_probe_mode,
                remote_max_workers=args.remote_max_workers,
                remote_readback_attempts=args.remote_readback_attempts,
                remote_request_timeout_seconds=args.remote_request_timeout_seconds,
                round_deadline=round_deadline,
                defer_membership_to_auto_pool=(
                    args.defer_membership_to_auto_pool
                ),
            )

    result = run_model(
        args.model,
        enabled=True,
        apply=args.apply,
        required_ready_slots=args.required_ready_slots,
        minimum_ready_slots=0,
        planner=planner,
        reconciler_factory=reconciler_factory,
        dispatch_projector=dispatch_projector,
        apply_policy=apply_policy,
        billing_contract=(
            "billing-unconfigured" if billing_target is None else billing_target.confirmation_scope
        ),
        pin_failover_attempts=args.pin_failover_attempts,
        probe_failure_handler=(
            None
            if not args.apply
            else lambda plan, failure: record_provider_probe_failure(
                plan,
                failure,
                connection_factory=connection_factory,
                dispatch_projector=dispatch_projector,
            )
        ),
    )
    print(json.dumps(result, ensure_ascii=True, sort_keys=True))


if __name__ == "__main__":
    main()
