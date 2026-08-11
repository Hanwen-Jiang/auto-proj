from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from a6api_auto.trade_economic_facts import canonical_endpoint_fingerprint
from a6api_auto.trade_economic_source import (
    A6GatewayCostSnapshot,
    ChannelRevenueSnapshot,
    EconomicAttemptLedgerEntry,
    EconomicChannelProjection,
    EconomicGenerationActivation,
    EconomicProjectionBundle,
    EconomicRoutingConfig,
    EconomicRoutingError,
    EconomicSource,
    ProjectedEconomicSource,
    RedisEconomicProjectionSink,
    SourceCostContract,
    SourceQuotaSnapshot,
    aggregate_request_economics,
    build_economic_plan,
    build_projection_bundle,
    derive_source_cost,
    evaluate_channel_source,
    freeze_economic_candidates,
    minimum_channel_revenue,
    sort_projected_sources,
    weekly_amortized_source_cost,
)

NOW = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)
MODEL = "gpt-5.6-sol"


def config(**overrides: Any) -> EconomicRoutingConfig:
    values = {
        "enabled": True,
        "plan_only": True,
        "api_sources_enabled": True,
        "account_sources_enabled": True,
        "a6_gateway_enabled": True,
        "recovery_confirmation_count": 1,
    }
    values.update(overrides)
    return EconomicRoutingConfig(**values)


def source(
    source_id: int = 1,
    *,
    source_kind: str = "account",
    cost: Decimal | None = None,
    **overrides: Any,
) -> EconomicSource:
    del cost
    endpoint = f"https://fixture-{source_id}.invalid/v1"
    values = {
        "source_id": source_id,
        "source_key": f"source-{source_id}",
        "source_kind": source_kind,
        "origin_class": "fixture",
        "model": MODEL,
        "endpoint_ref": endpoint,
        "endpoint_fingerprint": canonical_endpoint_fingerprint(endpoint, "fixture"),
        "enodot_account_id": None if source_kind == "a6_gateway" else 1000 + source_id,
        "health_state": "HEALTHY",
        "health_observed_at": NOW - timedelta(seconds=2),
        "capacity_state": "AVAILABLE",
        "capacity_observed_at": NOW - timedelta(seconds=2),
        "capacity_epoch": "capacity-1",
        "enabled": True,
    }
    values.update(overrides)
    return EconomicSource(**values)


def account_contract(source_id: int = 1, **overrides: Any) -> SourceCostContract:
    values = {
        "contract_id": 100 + source_id,
        "source_id": source_id,
        "cost_mode": "weekly_amortized",
        "cycle_cost_cny": Decimal("120"),
        "cost_basis": "weekly",
        "quota_basis": "full_cycle",
        "effective_at": NOW - timedelta(days=1),
        "expires_at": NOW + timedelta(days=7),
        "config_version": "weekly-v1",
        "source_hash": "a" * 64,
    }
    values.update(overrides)
    return SourceCostContract(**values)


def api_contract(source_id: int = 2, **overrides: Any) -> SourceCostContract:
    values = {
        "contract_id": 100 + source_id,
        "source_id": source_id,
        "cost_mode": "declared_k",
        "declared_k_cny": Decimal("0.081000"),
        "effective_at": NOW - timedelta(days=1),
        "expires_at": NOW + timedelta(days=7),
        "config_version": "api-v1",
        "source_hash": "b" * 64,
    }
    values.update(overrides)
    return SourceCostContract(**values)


def quota(source_id: int = 1, **overrides: Any) -> SourceQuotaSnapshot:
    values = {
        "snapshot_id": 200 + source_id,
        "source_id": source_id,
        "contract_id": 100 + source_id,
        "quota_epoch": "week-2026-08-03",
        "used_a": Decimal("18"),
        "remaining_a": Decimal("82"),
        "total_a": Decimal("100"),
        "reset_at": NOW + timedelta(days=5),
        "observed_at": NOW - timedelta(seconds=5),
        "payload_hash": "c" * 64,
    }
    values.update(overrides)
    return SourceQuotaSnapshot(**values)


def revenue(channel_id: int = 10, **overrides: Any) -> ChannelRevenueSnapshot:
    values = {
        "snapshot_id": 300 + channel_id,
        "model": MODEL,
        "channel_id": channel_id,
        "enodot_api_key_id": 9000 + channel_id,
        "revenue_epoch": "revenue-v1",
        "channel_net_k_cny": Decimal("1.920000"),
        "settlement_contract_ref": "settlement-v1",
        "observed_at": NOW - timedelta(seconds=2),
        "payload_hash": f"{channel_id:064x}",
    }
    values.update(overrides)
    return ChannelRevenueSnapshot(**values)


def projected(source_id: int, **overrides: Any) -> ProjectedEconomicSource:
    values = {
        "source_id": source_id,
        "source_kind": "api",
        "endpoint_fingerprint": f"{source_id:064x}",
        "account_ids": (1000 + source_id,),
        "source_k_cny": Decimal("0.081000"),
        "channel_net_k_cny": Decimal("0.100000"),
        "minimum_channel_k_cny": Decimal("0.129600"),
        "profit_on_cost_bps": 2345,
        "required_burn_a_per_second": Decimal(0),
        "capacity_epoch": "capacity-1",
        "cost_contract_id": 100 + source_id,
        "quota_epoch": None,
        "cost_snapshot_hash": f"{source_id + 100:064x}",
    }
    values.update(overrides)
    return ProjectedEconomicSource(**values)


def test_weekly_cost_uses_used_plus_remaining_and_stays_constant_after_consumption() -> None:
    before = weekly_amortized_source_cost("120", "18", "82")
    after = weekly_amortized_source_cost("120", "28", "72")

    assert before == after == Decimal("1.200000")


def test_quota_snapshot_is_atomic_and_new_epoch_cannot_mix_old_used_with_new_remaining() -> None:
    with pytest.raises(EconomicRoutingError, match="economic_quota_total_drift"):
        quota(used_a=Decimal("18"), remaining_a=Decimal("72"), total_a=Decimal("100"))

    reset = quota(
        quota_epoch="week-2026-08-10",
        used_a=Decimal("0"),
        remaining_a=Decimal("80"),
        total_a=Decimal("80"),
        payload_hash="d" * 64,
    )
    result = derive_source_cost(source(), account_contract(), reset, None, now=NOW, config=config())

    assert result.quota_epoch == "week-2026-08-10"
    assert result.source_k_cny == Decimal("1.500000")


def test_api_declared_k_enters_gate_without_currency_platform_or_delta_conversion() -> None:
    upstream = source(2, source_kind="api")
    result = derive_source_cost(upstream, api_contract(), None, None, now=NOW, config=config())

    assert result.source_k_cny == Decimal("0.081000")
    assert minimum_channel_revenue(result.source_k_cny) == Decimal("0.155520")


def test_exact_192_percent_gross_boundary_is_eligible_and_one_quantum_lower_exits_immediately() -> None:
    # Gate is (1 + 6000bps) * 1.2 A6 gross factor = 1.92 on cost (effective 9200 bps).
    upstream = source()
    cost = derive_source_cost(upstream, account_contract(), quota(), None, now=NOW, config=config())
    boundary = evaluate_channel_source(
        upstream,
        revenue(channel_net_k_cny=Decimal("2.304000"), payload_hash="d" * 64),
        cost,
        now=NOW,
        config=config(),
    )
    below = evaluate_channel_source(
        upstream,
        revenue(channel_net_k_cny=Decimal("2.303999"), payload_hash="e" * 64),
        cost,
        now=NOW,
        config=config(recovery_confirmation_count=2),
        previous=boundary,
    )

    assert boundary.eligibility_state == "ELIGIBLE"
    assert boundary.profit_on_cost_bps == 9200
    assert below.eligibility_state == "UNPROFITABLE"


def test_channel_source_eligibility_is_a_triple_not_a_global_source_switch() -> None:
    upstream = source()
    plan = build_economic_plan(
        MODEL,
        (upstream,),
        {1: account_contract()},
        {1: quota()},
        (
            revenue(10, channel_net_k_cny=Decimal("1.919999")),
            revenue(11, channel_net_k_cny=Decimal("2.400000")),
        ),
        a6_gateway=None,
        previous_records=None,
        parent_generation=None,
        now=NOW,
        config=config(),
    )

    states = {(item.channel_id, item.source_id): item.eligibility_state for item in plan.records}
    assert states == {(10, 1): "UNPROFITABLE", (11, 1): "ELIGIBLE"}


@pytest.mark.parametrize(
    ("source_value", "contract_value", "quota_value", "revenue_value", "expected"),
    [
        (
            source(),
            account_contract(expires_at=NOW),
            quota(),
            revenue(),
            "COST_UNKNOWN",
        ),
        (
            source(),
            account_contract(),
            quota(observed_at=NOW - timedelta(seconds=61)),
            revenue(),
            "COST_UNKNOWN",
        ),
        (
            source(),
            account_contract(),
            quota(),
            revenue(observed_at=NOW - timedelta(seconds=31)),
            "REVENUE_UNKNOWN",
        ),
        (
            source(health_observed_at=NOW - timedelta(seconds=31)),
            account_contract(),
            quota(),
            revenue(),
            "UNHEALTHY",
        ),
    ],
)
def test_stale_contract_quota_revenue_or_health_closes_only_the_pair(
    source_value: EconomicSource,
    contract_value: SourceCostContract,
    quota_value: SourceQuotaSnapshot,
    revenue_value: ChannelRevenueSnapshot,
    expected: str,
) -> None:
    cost = derive_source_cost(
        source_value,
        contract_value,
        quota_value,
        None,
        now=NOW,
        config=config(),
    )
    result = evaluate_channel_source(source_value, revenue_value, cost, now=NOW, config=config())

    assert result.eligibility_state == expected


def test_burn_rate_wins_inside_hard_gate_but_never_makes_unprofitable_pair_eligible() -> None:
    burn_source = projected(
        1,
        source_kind="account",
        source_k_cny=Decimal("0.090000"),
        minimum_channel_k_cny=Decimal("0.144000"),
        required_burn_a_per_second=Decimal("0.01"),
    )
    cheaper_api = projected(2, source_k_cny=Decimal("0.081000"))

    assert sort_projected_sources((cheaper_api, burn_source))[0].source_id == 1

    upstream = source(configured_reserve_a=Decimal("10"))
    cost = derive_source_cost(
        upstream,
        account_contract(),
        quota(reset_at=NOW + timedelta(seconds=100)),
        None,
        now=NOW,
        config=config(),
    )
    rejected = evaluate_channel_source(
        upstream,
        revenue(channel_net_k_cny=Decimal("1.919999")),
        cost,
        now=NOW,
        config=config(),
    )

    assert cost.required_burn_a_per_second > 0
    assert rejected.eligibility_state == "UNPROFITABLE"


def test_a6_gateway_projects_priority_one_without_changing_provider_priority_contract() -> None:
    gateway_source = source(3, source_kind="a6_gateway")
    gateway = A6GatewayCostSnapshot(
        model=MODEL,
        routing_generation=7200,
        cost_generation=7200,
        source_k_cny=Decimal("0.081000"),
        cost_output_hash="f" * 64,
        ready_account_ids=(51, 52),
        activated_at=NOW - timedelta(seconds=2),
    )
    plan = build_economic_plan(
        MODEL,
        (gateway_source,),
        {},
        {},
        (revenue(channel_net_k_cny=Decimal("0.160000")),),
        a6_gateway=gateway,
        previous_records=None,
        parent_generation=None,
        now=NOW,
        config=config(),
    )
    activation = EconomicGenerationActivation(
        model=MODEL,
        economic_generation=1,
        generation_id=1,
        input_hash=plan.input_hash,
        output_hash=plan.output_hash,
        changed=True,
        plan_only=True,
        activated_at=NOW,
    )
    bundle = build_projection_bundle(plan, activation, (gateway_source,), a6_gateway=gateway)

    projected_doc = bundle.document()["channels"][0]["sources"][0]
    assert projected_doc["priority"] == 1
    assert projected_doc["account_ids"] == [51, 52]
    assert projected_doc["a6_routing_generation"] == 7200
    implementation = Path("src/a6api_auto/trade_economic_source.py").read_text()
    assert "slot.desired_priority >= 2" in implementation
    assert "slot.desired_priority = slot.desired_rank + 1" in implementation


def test_replay_uses_frozen_generation_and_failed_attempt_cost_is_in_total_profit() -> None:
    channel = EconomicChannelProjection(
        channel_id=10,
        revenue_snapshot_id=310,
        revenue_snapshot_hash="a" * 64,
        enodot_api_key_id=9010,
        sub2api_group_id=None,
        sources=(projected(1), projected(2)),
    )
    bundle = EconomicProjectionBundle(
        model=MODEL,
        economic_generation=42,
        input_hash="b" * 64,
        output_hash="c" * 64,
        evaluated_at=NOW,
        channels=(channel,),
    )
    frozen = freeze_economic_candidates(bundle, api_key_id=9010, group_id=None)
    assert frozen is not None
    first = frozen.select()
    second = frozen.select(attempted_source_ids={first.source_id})

    assert frozen.economic_generation == 42
    assert first.source_id == 1
    assert second is not None and second.source_id == 2
    assert frozen.select(attempted_source_ids={1, 2}) is None

    totals = aggregate_request_economics(
        (
            EconomicAttemptLedgerEntry(
                attempt_id="attempt-1",
                source_id=1,
                source_k_cny=Decimal("0.081"),
                channel_net_k_cny=Decimal("0.100"),
                attempt_outcome="failed",
                realized_a=Decimal("10"),
                realized_cost_cny=Decimal("0.40"),
                realized_revenue_cny=Decimal("0"),
            ),
            EconomicAttemptLedgerEntry(
                attempt_id="attempt-2",
                source_id=2,
                source_k_cny=Decimal("0.081"),
                channel_net_k_cny=Decimal("0.100"),
                attempt_outcome="won",
                realized_a=Decimal("10"),
                realized_cost_cny=Decimal("0.81"),
                realized_revenue_cny=Decimal("1.00"),
            ),
        )
    )
    assert totals["realized_cost_cny"] == Decimal("1.21")
    assert totals["realized_profit_cny"] == Decimal("-0.21")
    assert totals["realized_profit_negative"] is True


def test_plan_and_dashboard_inputs_are_deterministically_recomputable() -> None:
    sources = (source(2, source_kind="api"), source())
    contracts = {1: account_contract(), 2: api_contract()}
    quotas = {1: quota()}
    revenues = (revenue(11, channel_net_k_cny=Decimal("1.6")), revenue(10))

    first = build_economic_plan(
        MODEL,
        sources,
        contracts,
        quotas,
        revenues,
        a6_gateway=None,
        previous_records=None,
        parent_generation=None,
        now=NOW,
        config=config(),
    )
    second = build_economic_plan(
        MODEL,
        tuple(reversed(sources)),
        contracts,
        quotas,
        tuple(reversed(revenues)),
        a6_gateway=None,
        previous_records=None,
        parent_generation=None,
        now=NOW,
        config=config(),
    )

    assert first.input_hash == second.input_hash
    assert first.output_hash == second.output_hash
    assert [item.semantic_document() for item in first.records] == [
        item.semantic_document() for item in second.records
    ]


class FakeRedis:
    def __init__(self, result: object = 1, *, readback: object = None) -> None:
        self.result = result
        self.readback = readback
        self.value: object = None
        self.calls: list[tuple[Any, ...]] = []

    def eval(self, *args: Any) -> object:
        self.calls.append(args)
        if self.result in {0, 1}:
            self.value = args[3]
        return self.result

    def get(self, _key: str) -> object:
        return self.value if self.readback is None else self.readback


def test_redis_projection_uses_monotonic_generation_cas_and_exact_readback() -> None:
    redis = FakeRedis()
    bundle = EconomicProjectionBundle(
        model=MODEL,
        economic_generation=42,
        input_hash="a" * 64,
        output_hash="b" * 64,
        evaluated_at=NOW,
        channels=(),
    )

    result = RedisEconomicProjectionSink(redis).publish(bundle)

    assert result.changed is True
    script = redis.calls[0][0]
    assert "current_generation >= desired_generation" in script
    assert "return -2" in script
    assert json.loads(redis.calls[0][3])["economic_generation"] == 42


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (-1, "economic_redis_stale_generation"),
        (-2, "economic_redis_existing_projection_invalid"),
        (7, "economic_redis_result_invalid"),
    ],
)
def test_redis_projection_rejects_generation_or_existing_identity_conflict(
    status: int, code: str
) -> None:
    bundle = EconomicProjectionBundle(
        model=MODEL,
        economic_generation=42,
        input_hash="a" * 64,
        output_hash="b" * 64,
        evaluated_at=NOW,
        channels=(),
    )

    with pytest.raises(EconomicRoutingError, match=code):
        RedisEconomicProjectionSink(FakeRedis(status)).publish(bundle)


def test_priority1_migration_is_packaged_and_preserves_provider_priority_boundary() -> None:
    migration = Path("migrations/0062_priority1_economic_routing.sql").read_text()
    packaged = Path(
        "src/a6api_auto/data/migrations/0062_priority1_economic_routing.sql"
    ).read_text()

    assert packaged == migration
    for table in (
        "sub2api_economic_sources",
        "sub2api_source_cost_contracts",
        "sub2api_source_quota_snapshots",
        "sub2api_channel_revenue_snapshots",
        "sub2api_economic_generations",
        "sub2api_channel_source_eligibility",
        "sub2api_economic_projection_receipts",
        "sub2api_economic_request_envelopes",
        "sub2api_economic_attempts",
    ):
        assert f"a6_trade.{table}" in migration
    assert "source_kind IN ('api', 'account', 'a6_gateway')" in migration
    assert "economic_hop_count BETWEEN 0 AND 1" in migration
