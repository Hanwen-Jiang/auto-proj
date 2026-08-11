from __future__ import annotations

from decimal import Decimal

import pytest

from a6api_auto.trade_provider_cost import (
    PoolDiffKind,
    ProviderCostError,
    RoutingView,
    build_cost_plan,
    classify_pool_diff,
    normalize_slot_cost,
)

HASH_A = "a" * 64
HASH_B = "b" * 64


def view(
    channels: tuple[int, ...],
    assignments: dict[int, str | None],
    cost_hash: str = HASH_A,
) -> RoutingView:
    return RoutingView(channels, assignments, cost_hash)


def test_cost_is_decimal_and_rounds_up_to_protective_quantum() -> None:
    assert normalize_slot_cost("1.0000001", quantum="0.000001") == Decimal("1.000001")
    plan = build_cost_plan(
        {1: "1.0000001", 2: "0.5"},
        retry_reserve_multiplier=None,
        bootstrap_retry_reserve="0.1",
        realized_cost_p95_multiplier=None,
        margin_bps=1000,
        headroom_bps=500,
    )

    assert plan.route_cost_multiplier == Decimal("1.0001")
    assert plan.pool_cost_multiplier == Decimal("1.1001")
    assert plan.billing_floor_multiplier == Decimal("1.2707")
    assert len(plan.cost_output_hash) == 64


def test_default_cost_quantum_never_rounds_below_enodot_decimal_10_4() -> None:
    assert normalize_slot_cost("0.011764") == Decimal("0.0118")
    plan = build_cost_plan(
        {1: "0.011764"},
        retry_reserve_multiplier="0.001",
        bootstrap_retry_reserve="0",
        realized_cost_p95_multiplier="0.013145674073",
        margin_bps=2000,
        headroom_bps=0,
    )

    assert plan.desired_slot_costs[1] == Decimal("0.0118")
    assert plan.pool_cost_multiplier == Decimal("0.0132")
    assert plan.billing_floor_multiplier == Decimal("0.0159")
    assert plan.quantum == Decimal("0.0001")


def test_empty_ready_set_keeps_previous_confirmed_floor() -> None:
    plan = build_cost_plan(
        {},
        retry_reserve_multiplier="0",
        bootstrap_retry_reserve="0",
        realized_cost_p95_multiplier=None,
        previous_billing_floor="2.5",
    )
    assert plan.billing_floor_multiplier == Decimal("2.5")
    assert plan.transition_floor_multiplier == Decimal("2.5")


def test_pool_diff_distinguishes_noop_cost_priority_membership_and_assignment() -> None:
    base = view((1, 2), {1: "p1", 2: "p2"})
    assert classify_pool_diff(base, view((1, 2), {1: "p1", 2: "p2"})) is PoolDiffKind.NOOP
    assert (
        classify_pool_diff(base, view((1, 2), {1: "p1", 2: "p2"}, HASH_B))
        is PoolDiffKind.COST_ONLY
    )
    assert (
        classify_pool_diff(base, view((2, 1), {1: "p1", 2: "p2"}))
        is PoolDiffKind.PRIORITY_AND_COST
    )
    assert (
        classify_pool_diff(base, view((1, 3), {1: "p1", 2: "p2"}))
        is PoolDiffKind.MEMBERSHIP_AND_COST
    )
    assert (
        classify_pool_diff(base, view((1, 2), {1: "p9", 2: "p2"}))
        is PoolDiffKind.LIQUIDITY_REPLACEMENT
    )


@pytest.mark.parametrize(
    ("current", "desired"),
    [
        ({1: "p1"}, {1: None}),
        ({1: None}, {1: "p1"}),
        ({}, {1: "p1"}),
        ({1: "p1"}, {}),
    ],
)
def test_pool_diff_treats_ready_vacant_transition_as_liquidity_replacement(
    current: dict[int, str | None], desired: dict[int, str | None]
) -> None:
    assert classify_pool_diff(view((1,), current), view((1,), desired)) is (
        PoolDiffKind.LIQUIDITY_REPLACEMENT
    )


def test_pool_diff_treats_missing_and_explicit_vacant_as_equivalent() -> None:
    assert classify_pool_diff(view((1,), {}), view((1,), {1: None})) is (
        PoolDiffKind.NOOP
    )


def test_cost_rejects_negative_or_non_finite_values() -> None:
    with pytest.raises(ProviderCostError, match="provider_cost_value_invalid"):
        build_cost_plan(
            {1: "-1"},
            retry_reserve_multiplier="0",
            bootstrap_retry_reserve="0",
            realized_cost_p95_multiplier=None,
        )
