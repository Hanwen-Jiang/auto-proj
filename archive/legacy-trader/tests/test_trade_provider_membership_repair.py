from __future__ import annotations

import inspect
from dataclasses import replace
from pathlib import Path

from a6api_auto.trade_provider_membership_repair import (
    PostgresProviderMembershipRepairStore,
    ProviderMembershipRepairClaim,
    ProviderMembershipRepairConsumer,
    ProviderMembershipSlotState,
)


def claim() -> ProviderMembershipRepairClaim:
    return ProviderMembershipRepairClaim(
        request_id=11,
        claim_token="claim-token",
        model="gpt-5.6-sol",
        source_routing_generation=100,
        source_cost_generation=100,
        slot_no=4,
        outgoing_merchant_id=15,
        outgoing_enodot_account_id=150,
        outgoing_a6_token_id=1500,
        reason_code="provider_funding_contract_failed",
        attempt_count=1,
    )


def slot(**overrides) -> ProviderMembershipSlotState:
    values = {
        "routing_generation": 101,
        "cost_generation": 101,
        "slot_no": 4,
        "slot_state": "READY",
        "merchant_id": 16,
        "enodot_account_id": 160,
        "a6_token_id": 1600,
        "route_mode": "auto_pool",
        "observed_schedulable": True,
        "auto_pool_generation_id": 7,
        "auto_pool_generation_state": "active",
        "auto_pool_account_state": "ready",
        "ban_confirmed": True,
        "auto_pool_confirmed": True,
        "cost_confirmed": True,
        "schedulable_confirmed": True,
    }
    values.update(overrides)
    return ProviderMembershipSlotState(**values)


class FakeStore:
    def __init__(self, observed: ProviderMembershipSlotState) -> None:
        self.observed = observed
        self.claimed = False
        self.deferred = []
        self.completed = []

    def claim(self, *, worker_id: str):
        assert worker_id == "worker"
        if self.claimed:
            return None
        self.claimed = True
        return claim()

    def observe(self, item):
        assert item.request_id == 11
        return self.observed

    def defer(self, item, **kwargs):
        self.deferred.append((item, kwargs))

    def complete(self, item, **kwargs):
        self.completed.append((item, kwargs))


class Trigger:
    def __init__(self) -> None:
        self.calls = []

    def wake(self, *, model: str, request_id: int) -> None:
        self.calls.append((model, request_id))


def consumer(store: FakeStore):
    membership = Trigger()
    auto_pool = Trigger()
    return (
        ProviderMembershipRepairConsumer(
            store,
            worker_id="worker",
            membership_apply_trigger=membership,
            auto_pool_trigger=auto_pool,
        ),
        membership,
        auto_pool,
    )


def test_vacant_slot_requests_membership_apply_and_stays_open() -> None:
    store = FakeStore(
        slot(
            slot_state="VACANT",
            merchant_id=None,
            enodot_account_id=None,
            a6_token_id=None,
            route_mode="fixed_pin",
            observed_schedulable=False,
            auto_pool_generation_id=None,
            auto_pool_generation_state=None,
            auto_pool_account_state=None,
            ban_confirmed=False,
            auto_pool_confirmed=False,
            cost_confirmed=False,
            schedulable_confirmed=False,
        )
    )
    worker, membership, auto_pool = consumer(store)

    result = worker.run_once()

    assert result["status"] == "waiting"
    assert result["action"] == "membership_apply_requested"
    assert membership.calls == [("gpt-5.6-sol", 11)]
    assert auto_pool.calls == []
    assert store.deferred[0][1]["state"] is None
    assert store.completed == []


def test_replacement_waits_for_auto_pool_exact_readbacks() -> None:
    store = FakeStore(slot(ban_confirmed=False, auto_pool_account_state="applying"))
    worker, membership, auto_pool = consumer(store)

    result = worker.run_once()

    assert result["status"] == "waiting"
    assert result["action"] == "auto_pool_preflight_requested"
    assert membership.calls == []
    assert auto_pool.calls == [("gpt-5.6-sol", 11)]
    assert store.completed == []


def test_replacement_completes_only_after_active_auto_pool_readbacks() -> None:
    store = FakeStore(slot())
    worker, membership, auto_pool = consumer(store)

    result = worker.run_once()

    assert result["status"] == "completed"
    assert result["auto_pool_ready"] is True
    assert membership.calls == []
    assert auto_pool.calls == []
    assert store.deferred == []
    assert store.completed[0][1]["state"].replacement_identity == (16, 160, 1600)


def test_outgoing_identity_is_never_accepted_as_replacement() -> None:
    store = FakeStore(
        replace(
            slot(),
            merchant_id=15,
            enodot_account_id=150,
            a6_token_id=1500,
        )
    )
    worker, _membership, _auto_pool = consumer(store)

    result = worker.run_once()

    assert result["status"] == "retry"
    assert result["error_class"] == "provider_membership_repair_outgoing_identity_still_active"


def test_replacement_rejects_partial_outgoing_identity_reuse() -> None:
    store = FakeStore(slot(enodot_account_id=150))
    worker, _membership, _auto_pool = consumer(store)

    result = worker.run_once()

    assert result["status"] == "retry"
    assert result["error_class"] == "provider_membership_repair_outgoing_identity_still_active"


def test_replacement_generation_must_advance_past_retired_source() -> None:
    store = FakeStore(slot(routing_generation=100, cost_generation=100))
    worker, _membership, _auto_pool = consumer(store)

    result = worker.run_once()

    assert result["status"] == "retry"
    assert result["error_class"] == "provider_membership_repair_generation_not_advanced"


def test_membership_store_uses_durable_skip_locked_claim_and_completion_cas() -> None:
    source = inspect.getsource(PostgresProviderMembershipRepairStore)

    assert "FOR UPDATE SKIP LOCKED" in source
    assert "status = 'running'" in source
    assert "claim_token = %s" in source
    assert "status = 'completed'" in source
    assert "provider_auto_pool_accounts" in source
    assert "ban_confirmed" in source


def test_membership_migration_and_systemd_wiring_are_packaged() -> None:
    root = Path(__file__).resolve().parents[1]
    migration = root / "migrations/0069_provider_membership_consumer_and_liquidity_tokens.sql"
    packaged = (
        root
        / "src/a6api_auto/data/migrations"
        / "0069_provider_membership_consumer_and_liquidity_tokens.sql"
    )
    assert migration.read_bytes() == packaged.read_bytes()
    text = migration.read_text()
    assert "pg_notify" in text
    assert "a6_provider_membership_repair" in text
    assert "a6_token_duty_token_merchant_fk" in text
    assert "a6_liquidity_route_duty_model_fk" in text

    target = (root / "deploy/systemd/a6-trader-control.target").read_text()
    apply_unit = (
        root / "deploy/systemd/a6-trader-provider-pool-apply.service"
    ).read_text()
    assert "Wants=a6-trader-provider-membership-repair.service" in target
    assert "Wants=a6-trader-provider-membership-apply.path" in target
    assert "OnSuccess=a6-trader-observer.service" in apply_unit
