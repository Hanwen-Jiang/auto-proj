from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from a6api_auto.trade_provider_cost import build_cost_plan
from a6api_auto.trade_provider_pool import (
    GenerationState,
    InMemoryProviderPoolStore,
    PricingState,
    ProviderBundle,
    ProviderPoolError,
    ProviderSlot,
    SlotState,
    assign_channels_to_stable_slots,
)


def ready_slot(slot_no: int, channel_id: int, account_id: int, token_id: int) -> ProviderSlot:
    return ProviderSlot(
        slot_no=slot_no,
        channel_id=channel_id,
        rank=slot_no,
        priority=slot_no + 1,
        assignment_id=f"p{account_id}",
        enodot_binding_id=30_000 + account_id,
        token_id=token_id,
        account_id=account_id,
        slot_state=SlotState.READY,
        funding_eligible=True,
        desired_schedulable=True,
        observed_schedulable=True,
        desired_cost_multiplier=Decimal("1"),
        observed_cost_multiplier=Decimal("1"),
        pin_confirmed=True,
        probe_confirmed=True,
    )


def bundle(routing: int = 1, cost: int = 1) -> ProviderBundle:
    plan = build_cost_plan(
        {1: "1"},
        retry_reserve_multiplier="0",
        bootstrap_retry_reserve="0",
        realized_cost_p95_multiplier=None,
    )
    return ProviderBundle(
        model="gpt-5.6-sol",
        routing_generation=routing,
        parent_routing_generation=None if routing == 1 else routing - 1,
        cost_generation=cost,
        parent_cost_generation=None if cost == 1 else cost - 1,
        generation_source="bootstrap",
        source_hash="a" * 64,
        ordered_channel_ids=(1,),
        standby_channel_ids=(),
        desired_size=1,
        slots=(ready_slot(1, 1, 10, 20),),
        cost_plan=plan,
    )


def activate(store: InMemoryProviderPoolStore, desired: ProviderBundle) -> ProviderBundle:
    store.plan(
        desired,
        expected_routing_generation=desired.parent_routing_generation,
        expected_cost_generation=desired.parent_cost_generation,
    )
    store.mark_applying(desired)
    store.confirm_cost(
        desired,
        observed_output_hash=desired.cost_plan.cost_output_hash,
        observed_billing_floor=desired.cost_plan.billing_floor_multiplier,
    )
    return store.activate(desired)


def test_stable_slot_planner_retains_incumbent_and_marks_missing_vacant() -> None:
    current = (ready_slot(1, 20, 10, 20), ready_slot(2, 30, 11, 21))

    result = assign_channels_to_stable_slots(current, (30, 40), desired_size=2)

    assert result[1] == (40, 2, 3)
    assert result[2] == (30, 1, 2)


def test_in_memory_bundle_store_enforces_cost_confirmation_and_generation_cas() -> None:
    store = InMemoryProviderPoolStore()
    first = bundle()
    store.plan(first, expected_routing_generation=None, expected_cost_generation=None)
    store.mark_applying(first)
    with pytest.raises(ProviderPoolError, match="cost_not_confirmed"):
        store.activate(first)
    store.confirm_cost(
        first,
        observed_output_hash=first.cost_plan.cost_output_hash,
        observed_billing_floor=first.cost_plan.billing_floor_multiplier,
    )
    active = store.activate(first)
    assert active.generation_state is GenerationState.ACTIVE
    assert active.pricing_state is PricingState.ACTIVE
    with pytest.raises(ProviderPoolError, match="plan_cas_mismatch"):
        store.plan(bundle(2, 2), expected_routing_generation=0, expected_cost_generation=1)


def test_in_memory_activation_rollback_restores_parent_and_is_retriable() -> None:
    store = InMemoryProviderPoolStore()
    first = bundle()
    second = replace(bundle(2, 2), source_hash="b" * 64)
    activate(store, first)
    activate(store, second)

    rollback = store.rollback_activation(second)
    repeated = store.rollback_activation(second)

    assert rollback == repeated
    assert rollback.restored_routing_generation == 1
    assert rollback.restored_cost_generation == 1
    restored = store.active(second.model)
    assert restored is not None
    assert restored.routing_generation == 1
    assert restored.generation_state is GenerationState.ACTIVE
    assert restored.pricing_state is PricingState.ACTIVE

    applying = store.mark_applying(second)
    assert applying.generation_state is GenerationState.APPLYING
    assert applying.pricing_state is PricingState.CONFIRMED
    store.confirm_cost(
        second,
        observed_output_hash=second.cost_plan.cost_output_hash,
        observed_billing_floor=second.cost_plan.billing_floor_multiplier,
    )
    assert store.activate(second).routing_generation == 2


def test_in_memory_activation_rollback_without_parent_restores_empty_active_state() -> None:
    store = InMemoryProviderPoolStore()
    first = bundle()
    activate(store, first)

    rollback = store.rollback_activation(first)
    assert rollback.restored_routing_generation is None
    assert rollback.restored_cost_generation is None
    assert store.active(first.model) is None
    assert store.rollback_activation(first) == rollback

    store.mark_applying(first)
    store.confirm_cost(
        first,
        observed_output_hash=first.cost_plan.cost_output_hash,
        observed_billing_floor=first.cost_plan.billing_floor_multiplier,
    )
    assert store.activate(first).routing_generation == 1


def test_in_memory_activation_rollback_rejects_a_stale_generation() -> None:
    store = InMemoryProviderPoolStore()
    first = bundle()
    second = replace(bundle(2, 2), source_hash="b" * 64)
    third = replace(bundle(3, 3), source_hash="c" * 64)
    activate(store, first)
    activate(store, second)
    activate(store, third)

    with pytest.raises(ProviderPoolError, match="rollback_cas_mismatch"):
        store.rollback_activation(second)

    current = store.active(first.model)
    assert current is not None
    assert current.routing_generation == 3
