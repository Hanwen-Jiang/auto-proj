from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from a6api_auto.trade_provider_inventory import (
    InventoryEntry,
    LiquidityState,
    PostgresProviderInventoryProjector,
    ProviderInventoryError,
    build_inventory_plan,
    next_desired_pool_size,
    project_active_provider_inventory,
    select_replacement,
)


def entry(merchant_id: int, *, funded: bool = True, health: int = 0) -> InventoryEntry:
    return InventoryEntry(
        model="gpt-5.6-sol",
        merchant_id=merchant_id,
        binding_id=300 + merchant_id,
        account_id=100 + merchant_id,
        token_id=200 + merchant_id,
        liquidity_state=LiquidityState.FUNDED if funded else LiquidityState.UNFUNDED,
        funding_eligible=funded,
        binding_ready=True,
        health_score=health,
    )


def test_inventory_keeps_desired_size_and_marks_missing_funding_vacant() -> None:
    plan = build_inventory_plan(
        [entry(1), entry(2), entry(3, funded=False)],
        desired_size=3,
        initial_max_active_slots=12,
    )

    assert plan.desired_size == 3
    assert [item.merchant_id for item in plan.active] == [1, 2]
    assert [item.merchant_id for item in plan.warm_standby] == []
    assert [item.merchant_id for item in plan.unfunded] == [3]
    assert plan.vacant_slot_nos == (3,)


def test_refilling_provider_above_hard_line_remains_active_eligible() -> None:
    refilling = replace(entry(1), liquidity_state=LiquidityState.REFILLING)

    plan = build_inventory_plan([refilling], desired_size=1)

    assert [item.merchant_id for item in plan.active] == [1]
    assert plan.vacant_slot_nos == ()


def test_positive_balance_with_unknown_transfer_remains_active_eligible() -> None:
    pending = replace(entry(1), liquidity_state=LiquidityState.TRANSFER_UNKNOWN)

    plan = build_inventory_plan([pending], desired_size=1)

    assert [item.merchant_id for item in plan.active] == [1]
    assert plan.vacant_slot_nos == ()


def test_replacement_is_deterministic_and_excludes_current_fault_domain() -> None:
    candidates = [entry(1, health=5), entry(2, health=10), entry(3, health=10)]

    selected = select_replacement(candidates, slot_no=2, excluded_account_ids={101})

    assert selected is not None
    assert selected.merchant_id == 2


def test_no_replacement_is_explicit_when_all_standby_accounts_are_unfunded() -> None:
    assert select_replacement([entry(1, funded=False)], slot_no=1) is None


def test_replacement_orders_runway_then_transfer_success_and_identity() -> None:
    now = datetime(2026, 7, 30, tzinfo=timezone.utc)
    candidates = [
        replace(
            entry(1),
            user_balance_runway_seconds=600,
            withdrawable_units=9,
            recent_provider_success_at=now,
        ),
        replace(
            entry(2),
            user_balance_runway_seconds=900,
            withdrawable_units=1,
            recent_provider_success_at=now - timedelta(hours=3),
        ),
        replace(
            entry(3),
            user_balance_runway_seconds=900,
            withdrawable_units=4,
            recent_provider_success_at=now - timedelta(hours=5),
        ),
        replace(
            entry(4),
            user_balance_runway_seconds=900,
            withdrawable_units=4,
            recent_provider_success_at=now - timedelta(minutes=5),
        ),
    ]

    selected = select_replacement(candidates, slot_no=1)

    assert selected is not None
    assert selected.merchant_id == 4


@pytest.mark.parametrize(
    "health_field",
    ("session_healthy", "token_healthy", "egress_healthy"),
)
def test_replacement_excludes_unhealthy_control_plane_evidence(
    health_field: str,
) -> None:
    unhealthy = replace(
        entry(1),
        user_balance_runway_seconds=10_000,
        **{health_field: False},
    )

    selected = select_replacement([unhealthy, entry(2)], slot_no=1)

    assert selected is not None
    assert selected.merchant_id == 2


def test_all_funded_pool_size_only_grows_and_keeps_vacant_capacity() -> None:
    now = datetime(2026, 7, 30, tzinfo=timezone.utc)

    assert (
        next_desired_pool_size(
            size_mode="all_funded",
            current_desired_size=8,
            funded_eligible_provider_count=2,
            now=now,
            observation_started_at=now,
        )
        == 8
    )
    assert (
        next_desired_pool_size(
            size_mode="all_funded",
            current_desired_size=8,
            funded_eligible_provider_count=15,
            now=now,
            observation_started_at=now,
        )
        == 12
    )
    assert (
        next_desired_pool_size(
            size_mode="all_funded",
            current_desired_size=15,
            funded_eligible_provider_count=2,
            now=now,
            observation_started_at=now,
            initial_max_active_slots=12,
        )
        == 15
    )


def test_evidence_based_pool_waits_14_days_and_shrinks_one_per_72_hours() -> None:
    now = datetime(2026, 7, 30, tzinfo=timezone.utc)
    common = {
        "size_mode": "evidence_based",
        "current_desired_size": 8,
        "funded_eligible_provider_count": 8,
        "now": now,
        "p99_concurrency": 8,
        "safe_concurrency_per_slot": 4,
        "target_utilization": "0.5",
        "p99_simultaneous_failed_slots": 1,
        "providers_needed_for_24h_liquidity_runway": 4,
    }

    assert (
        next_desired_pool_size(
            **common,
            observation_started_at=now - timedelta(days=13, hours=23),
        )
        == 8
    )
    assert (
        next_desired_pool_size(
            **common,
            observation_started_at=now - timedelta(days=14),
            last_shrink_at=now - timedelta(hours=71, minutes=59),
        )
        == 8
    )
    assert (
        next_desired_pool_size(
            **common,
            observation_started_at=now - timedelta(days=14),
            last_shrink_at=now - timedelta(hours=72),
        )
        == 7
    )


def test_evidence_based_pool_expands_immediately_for_required_capacity() -> None:
    now = datetime(2026, 7, 30, tzinfo=timezone.utc)

    assert (
        next_desired_pool_size(
            size_mode="evidence_based",
            current_desired_size=3,
            funded_eligible_provider_count=3,
            now=now,
            observation_started_at=now,
            p99_concurrency=41,
            safe_concurrency_per_slot=10,
            target_utilization="0.7",
            p99_simultaneous_failed_slots=2,
            providers_needed_for_24h_liquidity_runway=5,
        )
        == 6
    )


def test_pool_size_policy_properties_hold_across_capacity_inputs() -> None:
    now = datetime(2026, 7, 30, tzinfo=timezone.utc)
    observation_started_at = now - timedelta(days=30)

    for current_size in range(1, 20):
        for funded_count in range(0, 20):
            bootstrap = next_desired_pool_size(
                size_mode="all_funded",
                current_desired_size=current_size,
                funded_eligible_provider_count=funded_count,
                now=now,
                observation_started_at=observation_started_at,
            )
            assert bootstrap >= current_size
            assert bootstrap == max(current_size, min(12, funded_count))

            for p99_concurrency in (0, 1, 7, 41, 120):
                evidence_based = next_desired_pool_size(
                    size_mode="evidence_based",
                    current_desired_size=current_size,
                    funded_eligible_provider_count=funded_count,
                    now=now,
                    observation_started_at=observation_started_at,
                    last_shrink_at=now - timedelta(hours=72),
                    p99_concurrency=p99_concurrency,
                    safe_concurrency_per_slot=10,
                    target_utilization="0.7",
                    p99_simultaneous_failed_slots=funded_count % 5,
                    providers_needed_for_24h_liquidity_runway=funded_count % 9,
                )
                if evidence_based < current_size:
                    assert evidence_based == current_size - 1
                else:
                    assert evidence_based <= 12


@pytest.mark.parametrize("target_utilization", ("1e-13", "1e999999"))
def test_pool_size_policy_rejects_unbounded_utilization(
    target_utilization: str,
) -> None:
    now = datetime(2026, 7, 30, tzinfo=timezone.utc)

    with pytest.raises(
        ProviderInventoryError,
        match="provider_inventory_target_utilization_invalid",
    ):
        next_desired_pool_size(
            size_mode="evidence_based",
            current_desired_size=3,
            funded_eligible_provider_count=3,
            now=now,
            observation_started_at=now - timedelta(days=14),
            target_utilization=target_utilization,
        )


class _ProjectionCursor:
    def __init__(self) -> None:
        self.executions: list[tuple[str, tuple[object, ...]]] = []
        self.rowcount = -1
        self._row = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql: str, params: tuple[object, ...]) -> None:
        self.executions.append((sql, params))
        if "WITH canonical AS" in sql:
            self._row = {
                "canonical_count": 6,
                "funded_count": 2,
                "changed_count": 4,
            }
        elif "WITH active_slots AS MATERIALIZED" in sql:
            self._row = {
                "active_slot_count": 2,
                "projected_count": 2,
                "demoted_count": 1,
            }
        elif "UPDATE a6_trade.provider_inventory inventory" in sql:
            self.rowcount = 3

    def fetchone(self):
        return self._row


class _ProjectionConnection:
    def __init__(self) -> None:
        self.cursor_value = _ProjectionCursor()
        self.committed = False
        self.rolled_back = False
        self.closed = False

    def cursor(self):
        return self.cursor_value

    def commit(self) -> None:
        self.committed = True

    def rollback(self) -> None:
        self.rolled_back = True

    def close(self) -> None:
        self.closed = True


def test_postgres_inventory_projector_upserts_canonical_and_retires_absent() -> None:
    connection = _ProjectionConnection()

    report = PostgresProviderInventoryProjector(lambda: connection).reconcile("gpt-5.6-sol")

    assert report.summary() == {
        "model": "gpt-5.6-sol",
        "canonical_count": 6,
        "funded_count": 2,
        "changed_count": 4,
        "retired_count": 3,
    }
    assert connection.committed is True
    assert connection.rolled_back is False
    assert connection.closed is True
    statements = [sql for sql, _params in connection.cursor_value.executions]
    assert "pg_advisory_xact_lock" in statements[0]
    assert "a6-provider-inventory:" in connection.cursor_value.executions[1][1][0]
    assert "pending_transfer.id IS NOT NULL" in statements[2]
    assert "intent.state = 'executing'" in statements[2]
    assert ">= 2 * merchant.quota_per_unit" in statements[2]
    assert "provider_user_balance_insufficient_unfunded" in statements[2]
    assert "coalesce(binding.last_error_class IN" in statements[2]
    assert "financial.withdrawable_micros" in statements[2]
    assert "< merchant.quota_per_unit" in statements[2]
    assert "AS funding_eligible" in statements[2]
    assert "ON CONFLICT (model, merchant_id) DO UPDATE" in statements[2]
    assert "inventory.inventory_state <> 'RETIRED'" in statements[3]
    assert "WITH active_slots AS MATERIALIZED" in statements[5]
    assert "matched_inventory AS MATERIALIZED" in statements[5]


def test_active_inventory_projection_rejects_missing_active_identity() -> None:
    cursor = _ProjectionCursor()
    original_execute = cursor.execute

    def execute(sql: str, params: tuple[object, ...]) -> None:
        original_execute(sql, params)
        if "WITH active_slots AS MATERIALIZED" in sql:
            cursor._row = {
                "active_slot_count": 2,
                "projected_count": 1,
                "demoted_count": 0,
            }

    cursor.execute = execute  # type: ignore[method-assign]

    with pytest.raises(
        ProviderInventoryError,
        match="provider_inventory_active_projection_mismatch",
    ):
        project_active_provider_inventory(
            cursor,
            "gpt-5.6-sol",
            routing_generation=7,
            expected_ready_count=2,
        )


def test_postgres_inventory_projector_rejects_empty_model_before_connecting() -> None:
    with pytest.raises(ProviderInventoryError, match="provider_inventory_model_invalid"):
        PostgresProviderInventoryProjector(
            lambda: pytest.fail("connection must stay cold")
        ).reconcile(" ")
