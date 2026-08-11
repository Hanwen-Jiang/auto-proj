from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import pytest

from a6api_auto.trade_provider_dispatch import (
    PostgresProviderDispatchSource,
    ProviderDispatchBundle,
    ProviderDispatchError,
    ProviderDispatchProjector,
    ProviderDispatchPublishResult,
    ProviderDispatchSlot,
    RedisProviderDispatchSink,
)


def dispatch_slot(**overrides: Any) -> ProviderDispatchSlot:
    values = {
        "slot_no": 1,
        "account_id": 51,
        "token_ref_id": 41,
        "external_token_id": 14093,
        "channel_id": 1008,
        "rank": 1,
        "priority": 2,
        "cost_multiplier": Decimal("1.25000000"),
    }
    values.update(overrides)
    return ProviderDispatchSlot(**values)


def dispatch_bundle(**overrides: Any) -> ProviderDispatchBundle:
    values = {
        "model": "gpt-5.6-sol",
        "routing_generation": 42,
        "cost_generation": 19,
        "source_hash": "b" * 64,
        "cost_input_hash": "c" * 64,
        "cost_output_hash": "d" * 64,
        "ready_slots": (dispatch_slot(),),
    }
    values.update(overrides)
    return ProviderDispatchBundle(**values)


class FakeRedis:
    def __init__(self, result: object = 1, *, readback: object = None) -> None:
        self.result = result
        self.readback = readback
        self.value: object = None
        self.calls: list[tuple[Any, ...]] = []
        self.get_calls: list[str] = []

    def eval(self, *args: Any) -> object:
        self.calls.append(args)
        if self.result in {0, 1}:
            self.value = args[3]
        return self.result

    def get(self, key: str) -> object:
        self.get_calls.append(key)
        return self.value if self.readback is None else self.readback


def test_redis_sink_uses_one_atomic_script_and_canonical_payload() -> None:
    redis = FakeRedis()
    sink = RedisProviderDispatchSink(redis)

    result = sink.publish(dispatch_bundle())

    assert result.changed is True
    assert result.key == "a6:provider:dispatch:v1:gpt-5.6-sol"
    assert len(redis.calls) == 1
    (
        script,
        key_count,
        key,
        payload,
        schema,
        model,
        routing,
        cost,
        state,
        channel,
        notice,
    ) = redis.calls[0]
    assert "redis.call('SET', KEYS[1], ARGV[1])" in script
    assert "redis.call('PUBLISH', ARGV[7], ARGV[8])" in script
    assert key_count == 1
    assert key == result.key
    assert schema == 1
    assert model == "gpt-5.6-sol"
    assert (routing, cost) == (42, 19)
    assert state == "active"
    assert channel == "a6:provider:dispatch:changed:v1"
    document = json.loads(payload)
    assert document["ready_dispatch_set"] == [
        {
            "account_id": 51,
            "channel_id": 1008,
            "cost_multiplier": "1.25000000",
            "external_token_id": 14093,
            "priority": 2,
            "rank": 1,
            "slot_no": 1,
            "token_ref_id": 41,
        }
    ]
    assert json.loads(notice)["digest"] == result.digest
    assert json.loads(notice)["state"] == "active"
    assert redis.get_calls == [result.key]


def test_redis_sink_rejects_non_authoritative_readback_after_publish() -> None:
    redis = FakeRedis(readback='{"state":"wrong"}')

    with pytest.raises(
        ProviderDispatchError,
        match="provider_dispatch_redis_readback_mismatch",
    ):
        RedisProviderDispatchSink(redis).publish(dispatch_bundle())


def test_redis_sink_restores_lower_generation_with_failed_generation_cas() -> None:
    redis = FakeRedis()
    sink = RedisProviderDispatchSink(redis)
    parent = dispatch_bundle(routing_generation=41, cost_generation=18)

    result = sink.restore(
        parent,
        expected_current_routing_generation=42,
        expected_current_cost_generation=19,
    )

    assert (result.routing_generation, result.cost_generation, result.state) == (
        41,
        18,
        "active",
    )
    script, key_count, key, payload, schema, model, routing, cost, channel, notice = (
        redis.calls[0]
    )
    assert "tonumber(decoded.routing_generation) ~= tonumber(ARGV[4])" in script
    assert "redis.call('SET', KEYS[1], ARGV[1])" in script
    assert (key_count, key, schema, model, routing, cost) == (
        1,
        result.key,
        1,
        "gpt-5.6-sol",
        42,
        19,
    )
    assert payload == parent.payload()
    assert channel == "a6:provider:dispatch:changed:v1"
    assert json.loads(notice)["restored_from_routing_generation"] == 42
    assert redis.get_calls == [result.key]


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (-1, "provider_dispatch_redis_stale_generation"),
        (7, "provider_dispatch_redis_result_invalid"),
        ("bad", "provider_dispatch_redis_result_invalid"),
    ],
)
def test_redis_sink_fails_closed_on_rejected_result(status: object, code: str) -> None:
    with pytest.raises(ProviderDispatchError, match=code):
        RedisProviderDispatchSink(FakeRedis(status)).publish(dispatch_bundle())


def test_dispatch_bundle_rejects_mixed_or_duplicate_identity() -> None:
    duplicate = dispatch_slot(
        slot_no=2,
        account_id=51,
        token_ref_id=42,
        external_token_id=14094,
        channel_id=1009,
        rank=2,
        priority=3,
    )

    with pytest.raises(ProviderDispatchError, match="provider_dispatch_account_duplicate"):
        dispatch_bundle(ready_slots=(dispatch_slot(), duplicate))


@pytest.mark.parametrize(
    "overrides",
    (
        {"rank": 1, "priority": 1},
        {"rank": 1, "priority": 3},
    ),
)
def test_dispatch_slot_requires_rank_derived_provider_priority(overrides) -> None:
    with pytest.raises(ProviderDispatchError, match="provider_dispatch_slot_priority_invalid"):
        dispatch_slot(**overrides)


def test_absent_projection_is_a_versioned_empty_tombstone() -> None:
    tombstone = ProviderDispatchBundle.tombstone(
        "gpt-5.6-sol",
        routing_generation=42,
        cost_generation=19,
    )

    assert tombstone.document()["state"] == "absent"
    assert tombstone.document()["ready_dispatch_set"] == []
    assert tombstone.source_hash == "0" * 64


@dataclass
class FakeDatabase:
    active: dict[str, Any] | None
    slots: list[dict[str, Any]]
    cursor: dict[str, Any] | None = None
    commits: int = 0
    rollbacks: int = 0
    closes: int = 0

    def connect(self) -> FakeConnection:
        return FakeConnection(self)


class FakeConnection:
    def __init__(self, database: FakeDatabase) -> None:
        self.database = database

    def cursor(self) -> FakeCursor:
        return FakeCursor(self.database)

    def commit(self) -> None:
        self.database.commits += 1

    def rollback(self) -> None:
        self.database.rollbacks += 1

    def close(self) -> None:
        self.database.closes += 1


class FakeCursor:
    def __init__(self, database: FakeDatabase) -> None:
        self.database = database
        self.one: dict[str, Any] | None = None
        self.many: list[dict[str, Any]] = []
        self.calls: list[tuple[str, object]] = []

    def __enter__(self) -> FakeCursor:
        return self

    def __exit__(self, *_args: object) -> bool:
        return False

    def execute(self, query: str, params: object = None) -> None:
        normalized = " ".join(query.split())
        self.calls.append((normalized, params))
        self.one = None
        self.many = []
        if normalized.startswith("SELECT generation.model"):
            self.one = self.database.active
        elif normalized.startswith("SELECT coalesce"):
            self.one = self.database.cursor or {
                "routing_generation": 0,
                "cost_generation": 0,
            }
        elif normalized.startswith("SELECT slot.slot_no"):
            self.many = self.database.slots

    def fetchone(self) -> dict[str, Any] | None:
        return self.one

    def fetchall(self) -> list[dict[str, Any]]:
        return self.many


def active_row(**overrides: Any) -> dict[str, Any]:
    values = {
        "model": "gpt-5.6-sol",
        "routing_generation": 42,
        "cost_generation": 19,
        "source_hash": "b" * 64,
        "cost_input_hash": "c" * 64,
        "cost_output_hash": "d" * 64,
        "desired_size": 2,
    }
    values.update(overrides)
    return values


def ready_row(**overrides: Any) -> dict[str, Any]:
    values = {
        "slot_no": 1,
        "slot_state": "READY",
        "account_id": 51,
        "token_ref_id": 41,
        "external_token_id": 14093,
        "channel_id": 1008,
        "rank": 1,
        "priority": 2,
        "desired_slot_cost_multiplier": Decimal("1.25"),
        "observed_rate_multiplier": Decimal("1.25"),
        "liquidity_state": "FUNDED",
        "funding_eligible": True,
        "desired_schedulable": True,
        "observed_schedulable": True,
        "confirmed_pin_binding_id": 61,
        "pin_confirmed": True,
        "probe_confirmed": True,
        "token_status": "active",
        "binding_state": "bound",
        "current_pin_binding_id": 61,
    }
    values.update(overrides)
    return values


def vacant_row(**overrides: Any) -> dict[str, Any]:
    values = {
        "slot_no": 2,
        "slot_state": "VACANT",
        "account_id": None,
        "token_ref_id": None,
        "external_token_id": None,
        "channel_id": None,
        "rank": None,
        "priority": None,
        "desired_slot_cost_multiplier": None,
        "observed_rate_multiplier": None,
        "liquidity_state": "UNFUNDED",
        "funding_eligible": False,
        "desired_schedulable": False,
        "observed_schedulable": False,
        "confirmed_pin_binding_id": None,
        "pin_confirmed": False,
        "probe_confirmed": False,
        "token_status": None,
        "binding_state": None,
        "current_pin_binding_id": None,
    }
    values.update(overrides)
    return values


def test_postgres_source_builds_ready_set_from_one_repeatable_read_snapshot() -> None:
    database = FakeDatabase(active_row(), [ready_row(), vacant_row()])

    bundle = PostgresProviderDispatchSource(database.connect).load_active(
        "gpt-5.6-sol"
    )

    assert bundle is not None
    assert bundle.state == "active"
    assert (bundle.routing_generation, bundle.cost_generation) == (42, 19)
    assert tuple(slot.account_id for slot in bundle.ready_slots) == (51,)
    assert bundle.ready_slots[0].external_token_id == 14093
    assert database.commits == 1
    assert database.rollbacks == 0
    assert database.closes == 1


@pytest.mark.parametrize(
    ("rows", "code"),
    [
        ([ready_row(observed_schedulable=False), vacant_row()], "unconfirmed"),
        ([ready_row(observed_rate_multiplier=Decimal("1.24")), vacant_row()], "cost_mismatch"),
        ([ready_row(priority=1), vacant_row()], "priority_invalid"),
        ([ready_row(priority=3), vacant_row()], "priority_invalid"),
        ([ready_row(liquidity_state="UNFUNDED"), vacant_row()], "unconfirmed"),
        ([ready_row(token_status="disabled"), vacant_row()], "unconfirmed"),
        ([ready_row(binding_state="retired"), vacant_row()], "unconfirmed"),
        ([ready_row(current_pin_binding_id=None), vacant_row()], "unconfirmed"),
        ([ready_row(), vacant_row(account_id=99)], "vacant_slot_populated"),
        ([ready_row()], "slot_count_mismatch"),
    ],
)
def test_postgres_source_rejects_non_dispatchable_active_rows(
    rows: list[dict[str, Any]], code: str
) -> None:
    database = FakeDatabase(active_row(), rows)

    with pytest.raises(ProviderDispatchError, match=f"provider_dispatch_.*{code}"):
        PostgresProviderDispatchSource(database.connect).load_active("gpt-5.6-sol")


def test_postgres_source_emits_tombstone_at_last_activated_cursor() -> None:
    database = FakeDatabase(
        None,
        [],
        cursor={"routing_generation": 41, "cost_generation": 18},
    )

    projection = PostgresProviderDispatchSource(database.connect).load_active(
        "gpt-5.6-sol"
    )

    assert projection is not None
    assert projection.state == "absent"
    assert (projection.routing_generation, projection.cost_generation) == (41, 18)
    assert projection.ready_slots == ()


class StaticSource:
    def __init__(self, value: ProviderDispatchBundle | None) -> None:
        self.value = value

    def load_active(self, _model: str) -> ProviderDispatchBundle | None:
        return self.value


class RecordingSink:
    def __init__(self) -> None:
        self.values: list[ProviderDispatchBundle] = []

    def publish(self, bundle: ProviderDispatchBundle) -> ProviderDispatchPublishResult:
        self.values.append(bundle)
        return ProviderDispatchPublishResult(
            "key",
            bundle.digest(),
            True,
            bundle.routing_generation,
            bundle.cost_generation,
            bundle.state,
        )

    def restore(
        self,
        bundle: ProviderDispatchBundle,
        **_kwargs: Any,
    ) -> ProviderDispatchPublishResult:
        return self.publish(bundle)


def test_projector_checks_postgres_generation_before_redis_publish() -> None:
    sink = RecordingSink()
    projector = ProviderDispatchProjector(StaticSource(dispatch_bundle()), sink)

    with pytest.raises(
        ProviderDispatchError,
        match="provider_dispatch_active_generation_mismatch",
    ):
        projector.reconcile(
            "gpt-5.6-sol",
            expected_routing_generation=43,
            expected_cost_generation=20,
        )

    assert sink.values == []


def test_projector_publishes_matching_active_generation() -> None:
    sink = RecordingSink()
    projector = ProviderDispatchProjector(StaticSource(dispatch_bundle()), sink)

    result = projector.reconcile(
        "gpt-5.6-sol",
        expected_routing_generation=42,
        expected_cost_generation=19,
    )

    assert result.changed is True
    assert sink.values == [dispatch_bundle()]


def test_projector_restores_database_authoritative_parent_projection() -> None:
    parent = dispatch_bundle(routing_generation=41, cost_generation=18)
    sink = RecordingSink()
    projector = ProviderDispatchProjector(StaticSource(parent), sink)

    result = projector.restore(
        "gpt-5.6-sol",
        expected_current_routing_generation=42,
        expected_current_cost_generation=19,
    )

    assert (result.routing_generation, result.cost_generation) == (41, 18)
    assert sink.values == [parent]
