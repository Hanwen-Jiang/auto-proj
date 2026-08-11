"""Plan-only Provider pool reconciliation for PostgreSQL shadow generations."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

from .trade_provider_cost import PoolDiffKind, RoutingView, build_cost_plan, classify_pool_diff
from .trade_provider_pool import ProviderBundle, ProviderSlot, SlotState


class ProviderPoolShadowError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class ProviderPoolShadowStore(Protocol):
    def plan(
        self,
        bundle: ProviderBundle,
        *,
        expected_routing_generation: int | None,
        expected_cost_generation: int | None,
    ) -> object: ...


@dataclass(frozen=True)
class ProviderPoolShadowInput:
    bundle: ProviderBundle
    current_routing: tuple[int, ...] = ()
    current_assignments: Mapping[int, str | int | None] | None = None
    current_cost_input_hash: str = "0" * 64


@dataclass(frozen=True)
class ProviderPoolShadowReport:
    model: str
    diff_kind: PoolDiffKind
    routing_generation: int
    cost_generation: int
    persisted: bool
    activated: bool = False
    remote_write_count: int = 0

    def summary(self) -> dict[str, Any]:
        return {
            "success": True,
            "mode": "shadow",
            "model": self.model,
            "diff_kind": self.diff_kind.value,
            "routing_generation": self.routing_generation,
            "cost_generation": self.cost_generation,
            "persisted": self.persisted,
            "activated": self.activated,
            "remote_write_count": self.remote_write_count,
        }


class ProviderPoolShadowReconciler:
    """Validate and optionally persist a planned generation without remote writes."""

    def __init__(self, store: ProviderPoolShadowStore | None = None) -> None:
        self.store = store

    def reconcile(
        self,
        desired: ProviderPoolShadowInput,
        *,
        persist: bool = False,
    ) -> ProviderPoolShadowReport:
        if not isinstance(persist, bool):
            raise ProviderPoolShadowError("provider_shadow_persist_invalid")
        bundle = desired.bundle
        current = RoutingView(
            desired.current_routing,
            desired.current_assignments or {},
            desired.current_cost_input_hash,
        )
        target = RoutingView(
            bundle.ordered_channel_ids,
            {slot.slot_no: slot.assignment_id for slot in bundle.slots},
            bundle.effective_cost_input_hash,
        )
        diff = classify_pool_diff(current, target)
        should_persist = persist and diff is not PoolDiffKind.NOOP
        if should_persist:
            if self.store is None:
                raise ProviderPoolShadowError("provider_shadow_store_missing")
            self.store.plan(
                bundle,
                expected_routing_generation=bundle.parent_routing_generation,
                expected_cost_generation=bundle.parent_cost_generation,
            )
        return ProviderPoolShadowReport(
            model=bundle.model,
            diff_kind=diff,
            routing_generation=bundle.routing_generation,
            cost_generation=bundle.cost_generation,
            persisted=should_persist,
        )


def reconcile_shadow_document(
    document: Mapping[str, Any],
    *,
    store: ProviderPoolShadowStore | None = None,
    persist: bool = False,
) -> ProviderPoolShadowReport:
    desired = shadow_input_from_mapping(document)
    return ProviderPoolShadowReconciler(store).reconcile(desired, persist=persist)


def shadow_input_from_mapping(document: Mapping[str, Any]) -> ProviderPoolShadowInput:
    if not isinstance(document, Mapping):
        raise ProviderPoolShadowError("provider_shadow_document_invalid")
    cost_raw = _mapping(document.get("cost_plan"), "provider_shadow_cost_plan_invalid")
    desired_slot_costs = _decimal_mapping(
        cost_raw.get("desired_slot_costs"),
        "provider_shadow_slot_costs_invalid",
    )
    cost_plan = build_cost_plan(
        desired_slot_costs,
        retry_reserve_multiplier=_optional_decimal(cost_raw.get("retry_reserve_multiplier")),
        bootstrap_retry_reserve=_decimal(
            cost_raw.get("bootstrap_retry_reserve", "0"),
            "provider_shadow_bootstrap_retry_invalid",
        ),
        realized_cost_p95_multiplier=_optional_decimal(
            cost_raw.get("realized_cost_p95_multiplier")
        ),
        margin_bps=_integer(cost_raw.get("margin_bps", 1000), "provider_shadow_margin_invalid"),
        headroom_bps=_integer(
            cost_raw.get("headroom_bps", 500), "provider_shadow_headroom_invalid"
        ),
        previous_billing_floor=_decimal(
            cost_raw.get("previous_billing_floor", "0"),
            "provider_shadow_previous_floor_invalid",
        ),
        conservative_fallback_cost=_decimal(
            cost_raw.get("conservative_fallback_cost", "0"),
            "provider_shadow_fallback_cost_invalid",
        ),
        quantum=_decimal(
            cost_raw.get("quantum", "0.0001"),
            "provider_shadow_quantum_invalid",
        ),
    )
    slots_raw = document.get("slots")
    if not isinstance(slots_raw, Sequence) or isinstance(slots_raw, (str, bytes)):
        raise ProviderPoolShadowError("provider_shadow_slots_invalid")
    slots = tuple(_slot_from_mapping(item, cost_plan.desired_slot_costs) for item in slots_raw)
    bundle = ProviderBundle(
        model=_text(document.get("model"), "provider_shadow_model_invalid"),
        routing_generation=_positive_integer(
            document.get("routing_generation"), "provider_shadow_routing_generation_invalid"
        ),
        parent_routing_generation=_optional_positive_integer(
            document.get("parent_routing_generation"),
            "provider_shadow_parent_routing_generation_invalid",
        ),
        cost_generation=_positive_integer(
            document.get("cost_generation"), "provider_shadow_cost_generation_invalid"
        ),
        parent_cost_generation=_optional_positive_integer(
            document.get("parent_cost_generation"),
            "provider_shadow_parent_cost_generation_invalid",
        ),
        generation_source=_text(
            document.get("generation_source"), "provider_shadow_generation_source_invalid"
        ),
        source_hash=_hash(document.get("source_hash"), "provider_shadow_source_hash_invalid"),
        ordered_channel_ids=_positive_integer_tuple(
            document.get("ordered_channel_ids"), "provider_shadow_ordered_channels_invalid"
        ),
        standby_channel_ids=_positive_integer_tuple(
            document.get("standby_channel_ids", ()),
            "provider_shadow_standby_channels_invalid",
        ),
        desired_size=_positive_integer(
            document.get("desired_size"), "provider_shadow_desired_size_invalid"
        ),
        slots=slots,
        cost_plan=cost_plan,
        cost_input_hash=_hash(
            document.get("cost_input_hash"), "provider_shadow_cost_input_hash_invalid"
        ),
        token_mix_version=_text(
            document.get("token_mix_version", "bootstrap-v1"),
            "provider_shadow_token_mix_version_invalid",
        ),
        exchange_rate_version=_text(
            document.get("exchange_rate_version", "identity-v1"),
            "provider_shadow_exchange_rate_version_invalid",
        ),
        config_version=_text(
            document.get("config_version", "a6-provider-topn-failover-v2"),
            "provider_shadow_config_version_invalid",
        ),
    )
    current_raw = document.get("current", {})
    current = _mapping(current_raw, "provider_shadow_current_invalid")
    assignments_raw = _mapping(
        current.get("assignments", {}), "provider_shadow_current_assignments_invalid"
    )
    assignments = {
        _positive_integer(key, "provider_shadow_current_slot_invalid"): _assignment(value)
        for key, value in assignments_raw.items()
    }
    current_hash = _hash(
        current.get("cost_input_hash", "0" * 64),
        "provider_shadow_current_cost_input_hash_invalid",
    )
    return ProviderPoolShadowInput(
        bundle=bundle,
        current_routing=_positive_integer_tuple(
            current.get("ordered_channel_ids", ()),
            "provider_shadow_current_channels_invalid",
        ),
        current_assignments=assignments,
        current_cost_input_hash=current_hash,
    )


def _slot_from_mapping(
    value: object,
    desired_slot_costs: Mapping[int, Decimal],
) -> ProviderSlot:
    row = _mapping(value, "provider_shadow_slot_invalid")
    slot_no = _positive_integer(row.get("slot_no"), "provider_shadow_slot_no_invalid")
    try:
        state = SlotState(_text(row.get("slot_state"), "provider_shadow_slot_state_invalid"))
    except ValueError:
        raise ProviderPoolShadowError("provider_shadow_slot_state_invalid") from None
    desired_cost = desired_slot_costs.get(slot_no)
    raw_observed_cost = row.get("observed_cost_multiplier")
    return ProviderSlot(
        slot_no=slot_no,
        channel_id=_optional_positive_integer(
            row.get("channel_id"), "provider_shadow_slot_channel_invalid"
        ),
        rank=_optional_positive_integer(row.get("rank"), "provider_shadow_slot_rank_invalid"),
        priority=_optional_positive_integer(
            row.get("priority"), "provider_shadow_slot_priority_invalid"
        ),
        assignment_id=_assignment(row.get("assignment_id")),
        enodot_binding_id=_optional_positive_integer(
            row.get("enodot_binding_id"),
            "provider_shadow_slot_binding_invalid",
        ),
        token_id=_optional_positive_integer(
            row.get("token_id"), "provider_shadow_slot_token_invalid"
        ),
        account_id=_optional_positive_integer(
            row.get("account_id"), "provider_shadow_slot_account_invalid"
        ),
        slot_state=state,
        funding_eligible=_boolean(
            row.get("funding_eligible", False), "provider_shadow_slot_funding_invalid"
        ),
        desired_schedulable=_boolean(
            row.get("desired_schedulable", False),
            "provider_shadow_slot_desired_schedulable_invalid",
        ),
        observed_schedulable=_boolean(
            row.get("observed_schedulable", False),
            "provider_shadow_slot_observed_schedulable_invalid",
        ),
        desired_cost_multiplier=desired_cost,
        observed_cost_multiplier=(
            None
            if raw_observed_cost is None
            else _decimal(raw_observed_cost, "provider_shadow_slot_observed_cost_invalid")
        ),
        pin_confirmed=_boolean(
            row.get("pin_confirmed", False), "provider_shadow_slot_pin_invalid"
        ),
        probe_confirmed=_boolean(
            row.get("probe_confirmed", False), "provider_shadow_slot_probe_invalid"
        ),
        state_version=_positive_integer(
            row.get("state_version", 1), "provider_shadow_slot_state_version_invalid"
        ),
    )


def _mapping(value: object, code: str) -> Mapping[Any, Any]:
    if not isinstance(value, Mapping):
        raise ProviderPoolShadowError(code)
    return value


def _text(value: object, code: str) -> str:
    result = str(value or "").strip()
    if not result:
        raise ProviderPoolShadowError(code)
    return result


def _hash(value: object, code: str) -> str:
    result = _text(value, code).casefold()
    if len(result) != 64 or any(char not in "0123456789abcdef" for char in result):
        raise ProviderPoolShadowError(code)
    return result


def _integer(value: object, code: str) -> int:
    if isinstance(value, bool):
        raise ProviderPoolShadowError(code)
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ProviderPoolShadowError(code) from None


def _positive_integer(value: object, code: str) -> int:
    result = _integer(value, code)
    if result < 1:
        raise ProviderPoolShadowError(code)
    return result


def _optional_positive_integer(value: object, code: str) -> int | None:
    return None if value is None else _positive_integer(value, code)


def _positive_integer_tuple(value: object, code: str) -> tuple[int, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ProviderPoolShadowError(code)
    result = tuple(_positive_integer(item, code) for item in value)
    if len(result) != len(set(result)):
        raise ProviderPoolShadowError(code)
    return result


def _decimal(value: object, code: str) -> Decimal:
    if isinstance(value, bool):
        raise ProviderPoolShadowError(code)
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ProviderPoolShadowError(code) from None
    if not result.is_finite():
        raise ProviderPoolShadowError(code)
    return result


def _optional_decimal(value: object) -> Decimal | None:
    return None if value is None else _decimal(value, "provider_shadow_decimal_invalid")


def _decimal_mapping(value: object, code: str) -> dict[int, Decimal]:
    rows = _mapping(value, code)
    result: dict[int, Decimal] = {}
    for key, raw in rows.items():
        slot = _positive_integer(key, code)
        if slot in result:
            raise ProviderPoolShadowError(code)
        result[slot] = _decimal(raw, code)
    return result


def _boolean(value: object, code: str) -> bool:
    if not isinstance(value, bool):
        raise ProviderPoolShadowError(code)
    return value


def _assignment(value: object) -> str | int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ProviderPoolShadowError("provider_shadow_assignment_invalid")
    if isinstance(value, int):
        if value < 1:
            raise ProviderPoolShadowError("provider_shadow_assignment_invalid")
        return value
    result = str(value).strip()
    if not result:
        raise ProviderPoolShadowError("provider_shadow_assignment_invalid")
    return result


__all__ = [
    "ProviderPoolShadowError",
    "ProviderPoolShadowInput",
    "ProviderPoolShadowReconciler",
    "ProviderPoolShadowReport",
    "reconcile_shadow_document",
    "shadow_input_from_mapping",
]
