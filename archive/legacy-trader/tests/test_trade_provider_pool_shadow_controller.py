from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import pytest

from a6api_auto import trade_provider_pool_shadow_controller as shadow_controller
from a6api_auto.trade_provider_cost import PoolDiffKind
from a6api_auto.trade_provider_inventory import (
    InventoryEntry,
    InventoryState,
    LiquidityState,
)
from a6api_auto.trade_provider_pool import SlotState
from a6api_auto.trade_provider_pool_apply import ProviderPoolApplyPlanner
from a6api_auto.trade_provider_pool_rank import Candidate, ObserverSnapshot, snapshot_hash
from a6api_auto.trade_provider_pool_shadow_controller import (
    ActivePoolProjection,
    PlannedPoolProjection,
    PostgresProviderPoolPlanningAdapter,
    PostgresProviderPoolPlanStore,
    ProviderPoolPlanningEvidence,
    ProviderPoolShadowControllerConfig,
    ProviderPoolShadowControllerError,
    ProviderPoolSizePlanningEvidence,
    SingleModelProviderPoolShadowController,
)
from scripts.audit_a6_provider_shadow_journal import audit_journal_lines
from scripts.shadow_reconcile_a6_provider_pool import run_model

NOW = datetime(2026, 7, 29, 8, 0, tzinfo=timezone.utc)
MODEL = "gpt-5.6-sol"
SCAN_PAYLOAD = [{"channel_id": 10}]
SCAN_RESULT_HASH = hashlib.sha256(
    json.dumps(SCAN_PAYLOAD, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()
).hexdigest()


def candidate(
    channel_id: int,
    price: str,
    *,
    observed_at: datetime = NOW,
    price_cny: str | None = None,
) -> Candidate:
    return Candidate(
        channel_id=channel_id,
        model=MODEL,
        final_multiplier=Decimal(price),
        realtime_ratio_cny=None if price_cny is None else Decimal(price_cny),
        health_state="healthy",
        available=True,
        last_success_at=observed_at - timedelta(seconds=1),
        latency_ewma_ms=Decimal(channel_id),
        observer_health_count=2,
        observer_count=2,
        observed_at=observed_at,
    )


def snapshot(
    rows: tuple[Candidate, ...],
    *,
    observed_at: datetime = NOW,
    sequence: int = 71,
) -> ObserverSnapshot:
    value = ObserverSnapshot(
        model=MODEL,
        observed_at=observed_at,
        status="completed",
        candidates=rows,
        result_hash="0" * 64,
        candidate_count=len(rows),
        pages_complete=True,
        config_version="a6-provider-topn-failover-v2",
        sequence=sequence,
        health_observer_count=2,
    )
    return replace(value, result_hash=snapshot_hash(value))


def inventory(merchant_id: int, *, health_score: int = 0) -> InventoryEntry:
    return InventoryEntry(
        model=MODEL,
        merchant_id=merchant_id,
        binding_id=3000 + merchant_id,
        account_id=1000 + merchant_id,
        token_id=2000 + merchant_id,
        inventory_state=InventoryState.WARM_STANDBY,
        liquidity_state=LiquidityState.FUNDED,
        funding_eligible=True,
        binding_ready=True,
        health_score=health_score,
    )


@dataclass
class FakeSource:
    evidence: ProviderPoolPlanningEvidence
    calls: list[tuple[str, dict[str, Any]]]

    def load(self, model: str, **kwargs: Any) -> ProviderPoolPlanningEvidence:
        self.calls.append((model, kwargs))
        return self.evidence


class RecordingStore:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, int | None, int | None]] = []

    def plan(
        self,
        bundle: Any,
        *,
        expected_routing_generation: int | None,
        expected_cost_generation: int | None,
    ) -> object:
        self.calls.append((bundle, expected_routing_generation, expected_cost_generation))
        return object()


def evidence(
    *,
    market: ObserverSnapshot | None = None,
    active: ActivePoolProjection | None = None,
    latest_planned: PlannedPoolProjection | None = None,
    source_hash: str = "a" * 64,
    realized_cost_sample_count: int = 0,
    realized_cost_p95_multiplier: Decimal | None = None,
    pool_size: ProviderPoolSizePlanningEvidence | None = None,
) -> ProviderPoolPlanningEvidence:
    return ProviderPoolPlanningEvidence(
        snapshot=market
        or snapshot((candidate(10, "0.4"), candidate(20, "0.5"), candidate(30, "0.6"))),
        source_hash=source_hash,
        inventory=(inventory(1, health_score=10), inventory(2, health_score=5)),
        active=active,
        latest_planned=latest_planned,
        maximum_routing_generation=0 if active is None else active.routing_generation,
        maximum_cost_generation=0 if active is None else active.cost_generation,
        realized_cost_sample_count=realized_cost_sample_count,
        realized_cost_p95_multiplier=realized_cost_p95_multiplier,
        pool_size=pool_size,
    )


def controller_for(
    value: ProviderPoolPlanningEvidence,
    *,
    store: RecordingStore | None = None,
    config: ProviderPoolShadowControllerConfig | None = None,
) -> tuple[SingleModelProviderPoolShadowController, FakeSource]:
    source = FakeSource(value, [])
    return (
        SingleModelProviderPoolShadowController(
            source,
            config or ProviderPoolShadowControllerConfig(model=MODEL),
            store=store,
        ),
        source,
    )


def active_from_report(report: Any, *, observed_at: datetime) -> ActivePoolProjection:
    bundle = report.bundle
    return ActivePoolProjection(
        routing_generation=bundle.routing_generation,
        cost_generation=bundle.cost_generation,
        desired_size=bundle.desired_size,
        ordered_channel_ids=bundle.ordered_channel_ids,
        standby_channel_ids=bundle.standby_channel_ids,
        slots=bundle.slots,
        cost_input_hash=bundle.effective_cost_input_hash,
        billing_floor_multiplier=bundle.cost_plan.billing_floor_multiplier,
        realized_cost_p95_multiplier=bundle.cost_plan.realized_cost_p95_multiplier,
        observed_at=observed_at,
    )


def test_controller_builds_and_persists_one_plan_only_generation() -> None:
    store = RecordingStore()
    controller, source = controller_for(evidence(), store=store)

    report = controller.run(now=NOW + timedelta(seconds=1), persist=True)

    assert report.shadow.diff_kind is PoolDiffKind.MEMBERSHIP_AND_COST
    assert report.shadow.persisted is True
    assert report.shadow.activated is False
    assert report.shadow.remote_write_count == 0
    assert report.bundle.ordered_channel_ids == (10, 20)
    assert report.bundle.standby_channel_ids == (30,)
    assert [slot.account_id for slot in report.bundle.slots] == [1001, 1002]
    assert [slot.priority for slot in report.bundle.slots] == [2, 3]
    assert len(store.calls) == 1
    assert store.calls[0][1:] == (None, None)
    assert source.calls[0][0] == MODEL
    assert source.calls[0][1]["size_mode"] == "all_funded"
    assert report.summary()["plan_write_count"] == 1


def test_provider_cost_cap_uses_final_quantized_slot_cost() -> None:
    market = snapshot(
        (
            candidate(10, "0.0444", price_cny="0.300000"),
            candidate(20, "0.0445", price_cny="0.300001"),
            candidate(30, "0.0100"),
        )
    )
    controller, _ = controller_for(
        evidence(market=market),
        config=ProviderPoolShadowControllerConfig(
            model=MODEL,
            maximum_provider_cost_cny=Decimal("0.3"),
        ),
    )

    report = controller.run(now=NOW + timedelta(seconds=1))

    assert report.bundle.ordered_channel_ids == (10,)
    assert tuple(slot.slot_no for slot in report.bundle.ready_slots) == (1,)
    assert report.bundle.slots[1].slot_state is SlotState.VACANT
    assert report.excluded == (
        (20, "cost_cap_exceeded"),
        (30, "cost_cap_unverifiable"),
    )


def test_provider_cost_cap_vacates_expensive_incumbents_when_supply_is_exhausted() -> None:
    funded = tuple(inventory(index) for index in range(1, 7))
    initial_rows = tuple(
        candidate(channel_id, price, price_cny=price_cny)
        for channel_id, price, price_cny in (
            (10, "0.0156", "0.10532"),
            (20, "0.0288", "0.194437"),
            (30, "0.0432", "0.291656"),
            (40, "0.0432", "0.291656"),
            (50, "0.0696", "0.46989"),
            (60, "0.0889", "0.600191"),
        )
    )
    initial_evidence = replace(
        evidence(market=snapshot(initial_rows)),
        inventory=funded,
    )
    initial, _ = controller_for(initial_evidence)
    initial_report = initial.run(now=NOW + timedelta(seconds=1))
    active = active_from_report(initial_report, observed_at=NOW + timedelta(seconds=1))
    next_observed_at = NOW + timedelta(seconds=2)
    capped_rows = tuple(
        replace(row, observed_at=next_observed_at)
        for row in initial_rows
    )
    capped_evidence = replace(
        initial_evidence,
        snapshot=snapshot(capped_rows, observed_at=next_observed_at, sequence=72),
        active=active,
        maximum_routing_generation=active.routing_generation,
        maximum_cost_generation=active.cost_generation,
        source_hash="b" * 64,
    )
    capped, _ = controller_for(
        capped_evidence,
        config=ProviderPoolShadowControllerConfig(
            model=MODEL,
            maximum_provider_cost_cny=Decimal("0.3"),
        ),
    )

    report = capped.run(now=NOW + timedelta(seconds=3))

    assert report.bundle.desired_size == 6
    assert report.bundle.ordered_channel_ids == (10, 20, 30, 40)
    assert tuple(slot.slot_no for slot in report.bundle.ready_slots) == (1, 2, 3, 4)
    assert tuple(
        slot.slot_no for slot in report.bundle.slots if slot.slot_state is SlotState.VACANT
    ) == (5, 6)
    assert report.excluded == (
        (50, "cost_cap_exceeded"),
        (60, "cost_cap_exceeded"),
    )


def test_apply_planner_replaces_a_rejected_pin_without_shrinking_slots() -> None:
    source = FakeSource(evidence(), [])
    planner = ProviderPoolApplyPlanner(
        source,
        ProviderPoolShadowControllerConfig(model=MODEL),
    )

    planner.reject_channel(10)
    plan = planner.plan(now=NOW + timedelta(seconds=1))

    assert plan.bundle.desired_size == 2
    assert plan.bundle.ordered_channel_ids == (20, 30)
    assert len(plan.bundle.ready_slots) == 2
    assert planner.rejected_channel_ids == frozenset({10})


def test_apply_planner_keeps_verified_pin_in_its_slot_while_replacing_failure() -> None:
    source = FakeSource(evidence(), [])
    planner = ProviderPoolApplyPlanner(
        source,
        ProviderPoolShadowControllerConfig(model=MODEL),
    )
    first = planner.plan(now=NOW + timedelta(seconds=1))

    planner.retain_verified_slots(first, (2,))
    planner.reject_channel(10)
    second = planner.plan(now=NOW + timedelta(seconds=1))

    by_slot = {slot.slot_no: slot.channel_id for slot in second.bundle.ready_slots}
    assert by_slot == {1: 30, 2: 20}
    assert second.verified_slot_nos == (2,)
    assert second.active is None


def test_apply_planner_keeps_verified_pin_when_it_falls_outside_new_topn() -> None:
    source = FakeSource(evidence(), [])
    planner = ProviderPoolApplyPlanner(
        source,
        ProviderPoolShadowControllerConfig(model=MODEL),
    )
    first = planner.plan(now=NOW + timedelta(seconds=1))
    planner.retain_verified_slots(first, (2,))
    planner.reject_channel(10)
    source.evidence = evidence(
        market=snapshot(
            (
                candidate(30, "0.1", observed_at=NOW + timedelta(seconds=2)),
                candidate(40, "0.2", observed_at=NOW + timedelta(seconds=2)),
                candidate(20, "0.9", observed_at=NOW + timedelta(seconds=2)),
            ),
            observed_at=NOW + timedelta(seconds=2),
            sequence=72,
        )
    )

    second = planner.plan(now=NOW + timedelta(seconds=3))

    by_slot = {slot.slot_no: slot.channel_id for slot in second.bundle.ready_slots}
    assert by_slot == {1: 30, 2: 20}
    assert second.bundle.ordered_channel_ids == (30, 20)
    assert second.verified_slot_nos == (2,)


def test_controller_resumes_applying_generation_after_market_rank_changes() -> None:
    initial_market = snapshot(
        (
            candidate(20, "0.4"),
            candidate(30, "0.5"),
            candidate(10, "0.6"),
        )
    )
    initial, _ = controller_for(evidence(market=initial_market))
    original = initial.run(now=NOW + timedelta(seconds=1)).bundle
    applying = PlannedPoolProjection(
        routing_generation=original.routing_generation,
        cost_generation=original.cost_generation,
        parent_routing_generation=original.parent_routing_generation,
        parent_cost_generation=original.parent_cost_generation,
        desired_size=original.desired_size,
        ordered_channel_ids=original.ordered_channel_ids,
        standby_channel_ids=original.standby_channel_ids,
        cost_input_hash=original.effective_cost_input_hash,
        cost_output_hash=original.cost_plan.cost_output_hash,
        config_version=original.config_version,
        source_hash=original.source_hash,
        generation_source=original.generation_source,
        generation_state="applying",
        pricing_state="applying",
        cost_plan=original.cost_plan,
    )
    changed_market = snapshot(
        (
            candidate(10, "0.3", observed_at=NOW + timedelta(seconds=2)),
            candidate(20, "0.35", observed_at=NOW + timedelta(seconds=2)),
            candidate(30, "0.5", observed_at=NOW + timedelta(seconds=2)),
        ),
        observed_at=NOW + timedelta(seconds=2),
        sequence=72,
    )
    resumed_evidence = replace(
        evidence(market=changed_market, latest_planned=applying),
        maximum_routing_generation=original.routing_generation,
        maximum_cost_generation=original.cost_generation,
    )
    resumed, _ = controller_for(resumed_evidence)

    report = resumed.run(now=NOW + timedelta(seconds=3))

    assert report.reused_planned is True
    assert report.bundle.routing_generation == original.routing_generation
    assert report.bundle.ordered_channel_ids == (20, 30)
    assert report.bundle.slots[0].desired_cost_multiplier == Decimal("0.4")


def test_apply_planner_fails_invalidated_applying_generation_and_replans() -> None:
    initial_market = snapshot(
        (
            candidate(20, "0.4"),
            candidate(30, "0.5"),
            candidate(10, "0.6"),
        )
    )
    initial, _ = controller_for(evidence(market=initial_market))
    original = initial.run(now=NOW + timedelta(seconds=1)).bundle
    applying = PlannedPoolProjection(
        routing_generation=original.routing_generation,
        cost_generation=original.cost_generation,
        parent_routing_generation=original.parent_routing_generation,
        parent_cost_generation=original.parent_cost_generation,
        desired_size=original.desired_size,
        ordered_channel_ids=original.ordered_channel_ids,
        standby_channel_ids=original.standby_channel_ids,
        cost_input_hash=original.effective_cost_input_hash,
        cost_output_hash=original.cost_plan.cost_output_hash,
        config_version=original.config_version,
        source_hash=original.source_hash,
        generation_source=original.generation_source,
        generation_state="applying",
        pricing_state="applying",
        cost_plan=original.cost_plan,
    )
    changed_market = snapshot(
        (
            candidate(10, "0.2", observed_at=NOW + timedelta(seconds=2)),
            candidate(40, "0.3", observed_at=NOW + timedelta(seconds=2)),
            replace(
                candidate(20, "0.4", observed_at=NOW + timedelta(seconds=2)),
                available=False,
                health_state="cooling",
            ),
            replace(
                candidate(30, "0.5", observed_at=NOW + timedelta(seconds=2)),
                available=False,
                health_state="cooling",
            ),
        ),
        observed_at=NOW + timedelta(seconds=2),
        sequence=72,
    )
    applying_evidence = replace(
        evidence(market=changed_market, latest_planned=applying),
        maximum_routing_generation=original.routing_generation,
        maximum_cost_generation=original.cost_generation,
    )
    source = FakeSource(applying_evidence, [])
    replans: list[tuple[int, str]] = []

    def fail_projection(latest: PlannedPoolProjection, reason: str) -> None:
        replans.append((latest.routing_generation, reason))
        source.evidence = replace(source.evidence, latest_planned=None)

    planner = ProviderPoolApplyPlanner(
        source,
        ProviderPoolShadowControllerConfig(model=MODEL),
        applying_replan_handler=fail_projection,
    )

    plan = planner.plan(now=NOW + timedelta(seconds=3))

    assert replans == [
        (
            original.routing_generation,
            "provider_shadow_controller_apply_in_progress_replan_required",
        )
    ]
    assert planner.applying_replans == tuple(replans)
    assert plan.bundle.routing_generation == original.routing_generation + 1
    assert plan.bundle.ordered_channel_ids == (10, 40)


def test_apply_planner_keeps_frozen_bundle_when_new_snapshot_changes_every_channel() -> None:
    initial_market = snapshot(
        (
            candidate(20, "0.4"),
            candidate(30, "0.5"),
            candidate(10, "0.6"),
        )
    )
    initial, _ = controller_for(evidence(market=initial_market))
    original = initial.run(now=NOW + timedelta(seconds=1)).bundle
    applying = PlannedPoolProjection(
        routing_generation=original.routing_generation,
        cost_generation=original.cost_generation,
        parent_routing_generation=original.parent_routing_generation,
        parent_cost_generation=original.parent_cost_generation,
        desired_size=original.desired_size,
        ordered_channel_ids=original.ordered_channel_ids,
        standby_channel_ids=original.standby_channel_ids,
        cost_input_hash=original.effective_cost_input_hash,
        cost_output_hash=original.cost_plan.cost_output_hash,
        config_version=original.config_version,
        source_hash=original.source_hash,
        generation_source=original.generation_source,
        generation_state="applying",
        pricing_state="applying",
        cost_plan=original.cost_plan,
    )
    changed_market = snapshot(
        (
            candidate(10, "0.2", observed_at=NOW + timedelta(seconds=2)),
            candidate(40, "0.3", observed_at=NOW + timedelta(seconds=2)),
        ),
        observed_at=NOW + timedelta(seconds=2),
        sequence=72,
    )
    source = FakeSource(
        replace(
            evidence(market=changed_market, latest_planned=applying),
            maximum_routing_generation=original.routing_generation,
            maximum_cost_generation=original.cost_generation,
        ),
        [],
    )
    loaded: list[PlannedPoolProjection] = []

    def load_frozen(latest: PlannedPoolProjection):
        loaded.append(latest)
        return original

    planner = ProviderPoolApplyPlanner(
        source,
        ProviderPoolShadowControllerConfig(model=MODEL),
        applying_replan_handler=lambda *_args: pytest.fail(
            "frozen applying generation was discarded"
        ),
        applying_bundle_loader=load_frozen,
    )

    plan = planner.plan(now=NOW + timedelta(seconds=3))

    assert loaded == [applying]
    assert plan.bundle == original
    assert plan.report.reused_planned is True
    assert planner.applying_replans == ()


def test_apply_planner_replans_when_applying_cost_slots_outnumber_ready_slots() -> None:
    initial, _ = controller_for(evidence())
    original = initial.run(now=NOW + timedelta(seconds=1)).bundle
    applying = PlannedPoolProjection(
        routing_generation=original.routing_generation,
        cost_generation=original.cost_generation,
        parent_routing_generation=original.parent_routing_generation,
        parent_cost_generation=original.parent_cost_generation,
        desired_size=original.desired_size,
        ordered_channel_ids=original.ordered_channel_ids,
        standby_channel_ids=original.standby_channel_ids,
        cost_input_hash=original.effective_cost_input_hash,
        cost_output_hash=original.cost_plan.cost_output_hash,
        config_version=original.config_version,
        source_hash=original.source_hash,
        generation_source=original.generation_source,
        generation_state="applying",
        pricing_state="applying",
        cost_plan=original.cost_plan,
    )
    changed_market = snapshot(
        (candidate(10, "0.2", observed_at=NOW + timedelta(seconds=2)),),
        observed_at=NOW + timedelta(seconds=2),
        sequence=72,
    )
    source = FakeSource(
        replace(
            evidence(market=changed_market, latest_planned=applying),
            maximum_routing_generation=original.routing_generation,
            maximum_cost_generation=original.cost_generation,
        ),
        [],
    )
    replans: list[tuple[int, str]] = []

    def fail_projection(latest: PlannedPoolProjection, reason: str) -> None:
        replans.append((latest.routing_generation, reason))
        source.evidence = replace(source.evidence, latest_planned=None)

    planner = ProviderPoolApplyPlanner(
        source,
        ProviderPoolShadowControllerConfig(model=MODEL),
        applying_replan_handler=fail_projection,
    )

    plan = planner.plan(now=NOW + timedelta(seconds=3))

    assert replans == [
        (
            original.routing_generation,
            "provider_shadow_controller_apply_in_progress_replan_required",
        )
    ]
    assert plan.bundle.routing_generation == original.routing_generation + 1
    assert plan.bundle.ordered_channel_ids == (10,)
    assert tuple(slot.slot_no for slot in plan.bundle.ready_slots) == (1,)


def test_controller_emits_replayable_phase2_shadow_evidence() -> None:
    source_evidence = evidence(
        realized_cost_sample_count=100,
        realized_cost_p95_multiplier=Decimal("0.55"),
    )
    controller, _ = controller_for(source_evidence)

    summary = controller.run(now=NOW + timedelta(seconds=1)).summary()
    audit = summary["shadow_evidence"]

    assert audit["schema"] == "a6-provider-shadow-evidence-v1"
    assert set(audit) == {
        "schema",
        "snapshot",
        "selection",
        "cost",
        "semantic_stability",
    }
    assert audit["snapshot"] == {
        "status": "completed",
        "pages_complete": True,
        "candidate_count": 3,
        "result_hash": source_evidence.snapshot.result_hash,
        "result_hash_verified": True,
        "source_hash": source_evidence.source_hash,
    }
    assert [row["channel_id"] for row in audit["selection"]["top"]] == [10, 20]
    assert [row["channel_id"] for row in audit["selection"]["standby"]] == [30]
    assert audit["selection"]["top_owned_overlap_count"] == 0
    assert audit["selection"]["standby_owned_overlap_count"] == 0
    assert (
        audit["cost"]["input_hash"]
        == summary["shadow_evidence"]["semantic_stability"]["input"]["cost_input_hash"]
    )
    assert audit["cost"]["output_hash"] == audit["semantic_stability"]["output_hash"]
    assert audit["cost"]["coverage"] == {
        "minimum_realized_cost_samples": 100,
        "realized_cost_sample_count": 100,
        "observed_realized_cost_p95_multiplier": "0.55",
        "applied_realized_cost_p95_multiplier": "0.55",
        "actual_cost_coverage_state": "covered",
        "route_plus_retry_covered": True,
        "realized_cost_covered": True,
        "protected_floor_covered": True,
    }
    sizing = summary["pool_size_evidence"]
    assert sizing["schema"] == "a6-provider-pool-size-evidence-v1"
    assert sizing["input"]["current_desired_size"] == 1
    assert sizing["input"]["funded_eligible_provider_count"] == 2
    assert sizing["output"] == {"desired_size": 2, "action": "expand"}
    assert summary["desired_size"] == 2


def test_semantic_hashes_ignore_new_snapshot_identity_when_decision_is_unchanged() -> None:
    first_controller, _ = controller_for(
        evidence(
            realized_cost_sample_count=100,
            realized_cost_p95_multiplier=Decimal("0.55"),
        )
    )
    refreshed_snapshot = snapshot(
        (
            candidate(10, "0.4", observed_at=NOW + timedelta(seconds=10)),
            candidate(20, "0.5", observed_at=NOW + timedelta(seconds=10)),
            candidate(30, "0.6", observed_at=NOW + timedelta(seconds=10)),
        ),
        observed_at=NOW + timedelta(seconds=10),
        sequence=72,
    )
    second_controller, _ = controller_for(
        evidence(
            market=refreshed_snapshot,
            source_hash="b" * 64,
            realized_cost_sample_count=100,
            realized_cost_p95_multiplier=Decimal("0.55"),
        )
    )

    first = first_controller.run(now=NOW + timedelta(seconds=1)).summary()["shadow_evidence"]
    second = second_controller.run(now=NOW + timedelta(seconds=11)).summary()["shadow_evidence"]

    assert first["snapshot"]["result_hash"] != second["snapshot"]["result_hash"]
    assert first["snapshot"]["source_hash"] != second["snapshot"]["source_hash"]
    assert first["semantic_stability"]["input_hash"] == second["semantic_stability"]["input_hash"]
    assert first["semantic_stability"]["output_hash"] == second["semantic_stability"]["output_hash"]


def test_realized_cost_p95_is_applied_only_after_minimum_sample_count() -> None:
    below, _ = controller_for(
        evidence(
            realized_cost_sample_count=99,
            realized_cost_p95_multiplier=Decimal("0.95"),
        )
    )
    ready, _ = controller_for(
        evidence(
            realized_cost_sample_count=100,
            realized_cost_p95_multiplier=Decimal("0.95"),
        )
    )

    below_report = below.run(now=NOW + timedelta(seconds=1))
    ready_report = ready.run(now=NOW + timedelta(seconds=1))

    assert below_report.bundle.cost_plan.realized_cost_p95_multiplier is None
    assert ready_report.bundle.cost_plan.realized_cost_p95_multiplier == Decimal("0.95")
    assert ready_report.bundle.cost_plan.pool_cost_multiplier == Decimal("0.95")
    assert ready_report.summary()["realized_cost_sample_count"] == 100
    assert ready_report.summary()["realized_cost_p95_multiplier"] == "0.95"
    assert (
        below_report.bundle.effective_cost_input_hash
        != ready_report.bundle.effective_cost_input_hash
    )


def test_realized_cost_sample_gap_retains_last_active_p95() -> None:
    initial_controller, _ = controller_for(
        evidence(
            realized_cost_sample_count=100,
            realized_cost_p95_multiplier=Decimal("0.95"),
        )
    )
    initial = initial_controller.run(now=NOW + timedelta(seconds=1))
    active = active_from_report(initial, observed_at=NOW - timedelta(seconds=1))
    resumed, _ = controller_for(evidence(active=active))

    report = resumed.run(now=NOW + timedelta(seconds=1))

    assert report.bundle.cost_plan.realized_cost_p95_multiplier == Decimal("0.95")
    assert report.shadow.diff_kind is PoolDiffKind.NOOP


def test_priority_staging_range_is_reserved_by_config_validation() -> None:
    with pytest.raises(
        ProviderPoolShadowControllerError,
        match="provider_shadow_controller_priority_range_invalid",
    ):
        ProviderPoolShadowControllerConfig(
            model=MODEL,
            priority_offset=1,
            initial_max_active_slots=17_000,
        )


@pytest.mark.parametrize(
    ("overrides", "error"),
    (
        ({"size_mode": "adaptive"}, "provider_shadow_controller_size_mode_invalid"),
        (
            {"minimum_active_slots": 13},
            "provider_shadow_controller_pool_size_bounds_invalid",
        ),
        (
            {"minimum_pool_observation_days": 13},
            "provider_shadow_controller_minimum_pool_observation_days_invalid",
        ),
        (
            {"minimum_pool_shrink_interval_hours": 71},
            "provider_shadow_controller_minimum_pool_shrink_interval_hours_invalid",
        ),
        (
            {"target_utilization": Decimal("1e-13")},
            "provider_shadow_controller_target_utilization_invalid",
        ),
    ),
)
def test_pool_size_config_rejects_weakened_or_unbounded_guards(
    overrides: dict[str, Any],
    error: str,
) -> None:
    with pytest.raises(ProviderPoolShadowControllerError, match=error):
        ProviderPoolShadowControllerConfig(model=MODEL, **overrides)


def test_controller_noop_does_not_write_a_generation() -> None:
    first, _ = controller_for(evidence())
    initial = first.run(now=NOW + timedelta(seconds=1))
    active = active_from_report(initial, observed_at=NOW)
    newer_snapshot = snapshot(
        (
            candidate(10, "0.4", observed_at=NOW + timedelta(seconds=10)),
            candidate(20, "0.5", observed_at=NOW + timedelta(seconds=10)),
            candidate(30, "0.6", observed_at=NOW + timedelta(seconds=10)),
        ),
        observed_at=NOW + timedelta(seconds=10),
        sequence=72,
    )
    store = RecordingStore()
    controller, _ = controller_for(
        evidence(market=newer_snapshot, active=active, source_hash="b" * 64),
        store=store,
    )

    report = controller.run(now=NOW + timedelta(seconds=11), persist=True)

    assert report.shadow.diff_kind is PoolDiffKind.NOOP
    assert report.shadow.persisted is False
    assert report.summary()["plan_write_count"] == 0
    assert store.calls == []


def test_controller_reorders_priorities_without_moving_slots_or_accounts() -> None:
    first, _ = controller_for(evidence())
    initial = first.run(now=NOW + timedelta(seconds=1))
    active = active_from_report(initial, observed_at=NOW)
    reordered = snapshot(
        (
            candidate(10, "0.6", observed_at=NOW + timedelta(seconds=10)),
            candidate(20, "0.3", observed_at=NOW + timedelta(seconds=10)),
            candidate(30, "0.7", observed_at=NOW + timedelta(seconds=10)),
        ),
        observed_at=NOW + timedelta(seconds=10),
        sequence=72,
    )
    controller, _ = controller_for(evidence(market=reordered, active=active))

    report = controller.run(now=NOW + timedelta(seconds=11))

    assert report.shadow.diff_kind is PoolDiffKind.PRIORITY_AND_COST
    assert [slot.channel_id for slot in report.bundle.slots] == [10, 20]
    assert [slot.account_id for slot in report.bundle.slots] == [1001, 1002]
    assert [slot.rank for slot in report.bundle.slots] == [2, 1]
    assert [slot.priority for slot in report.bundle.slots] == [3, 2]


def test_controller_reuses_same_semantic_planned_generation_without_write() -> None:
    first, _ = controller_for(evidence())
    initial = first.run(now=NOW + timedelta(seconds=1))
    bundle = initial.bundle
    planned = PlannedPoolProjection(
        routing_generation=bundle.routing_generation,
        cost_generation=bundle.cost_generation,
        parent_routing_generation=None,
        parent_cost_generation=None,
        desired_size=bundle.desired_size,
        ordered_channel_ids=bundle.ordered_channel_ids,
        standby_channel_ids=bundle.standby_channel_ids,
        cost_input_hash=bundle.effective_cost_input_hash,
        cost_output_hash=bundle.cost_plan.cost_output_hash,
        config_version=bundle.config_version,
        source_hash=bundle.source_hash,
        generation_source=bundle.generation_source,
    )
    store = RecordingStore()
    repeated = replace(
        evidence(source_hash="c" * 64),
        latest_planned=planned,
        maximum_routing_generation=bundle.routing_generation,
        maximum_cost_generation=bundle.cost_generation,
    )
    controller, _ = controller_for(repeated, store=store)

    report = controller.run(now=NOW + timedelta(seconds=1), persist=True)

    assert report.reused_planned is True
    assert report.bundle.routing_generation == bundle.routing_generation
    assert report.bundle.cost_generation == bundle.cost_generation
    assert report.shadow.persisted is False
    assert store.calls == []


def test_planned_pool_retains_vacant_slot_when_funded_inventory_drops() -> None:
    first, _ = controller_for(evidence())
    initial = first.run(now=NOW + timedelta(seconds=1))
    bundle = initial.bundle
    planned = PlannedPoolProjection(
        routing_generation=bundle.routing_generation,
        cost_generation=bundle.cost_generation,
        parent_routing_generation=None,
        parent_cost_generation=None,
        desired_size=bundle.desired_size,
        ordered_channel_ids=bundle.ordered_channel_ids,
        standby_channel_ids=bundle.standby_channel_ids,
        cost_input_hash=bundle.effective_cost_input_hash,
        cost_output_hash=bundle.cost_plan.cost_output_hash,
        config_version=bundle.config_version,
        source_hash=bundle.source_hash,
        generation_source=bundle.generation_source,
    )
    reduced = replace(
        evidence(source_hash="d" * 64),
        inventory=(inventory(1, health_score=10),),
        latest_planned=planned,
        maximum_routing_generation=bundle.routing_generation,
        maximum_cost_generation=bundle.cost_generation,
    )
    controller, _ = controller_for(reduced)

    report = controller.run(now=NOW + timedelta(seconds=1))

    assert report.bundle.desired_size == 2
    assert [slot.slot_state.value for slot in report.bundle.slots] == ["READY", "VACANT"]
    assert report.vacant_count == 1
    assert report.bundle.ordered_channel_ids == (10, 20)


def test_refilling_inventory_is_qualified_for_sizing_and_slot_assignment() -> None:
    refilling = replace(
        inventory(1, health_score=10),
        liquidity_state=LiquidityState.REFILLING,
    )
    controller, _ = controller_for(replace(evidence(), inventory=(refilling,)))

    report = controller.run(now=NOW + timedelta(seconds=1))

    assert report.summary()["pool_size_evidence"]["input"][
        "funded_eligible_provider_count"
    ] == 1
    assert report.vacant_count == 0
    assert [slot.account_id for slot in report.bundle.slots] == [refilling.account_id]
    assert [slot.slot_state.value for slot in report.bundle.slots] == ["READY"]


@pytest.mark.parametrize(
    "overrides",
    (
        {"funding_eligible": False},
        {"binding_ready": False},
        {"inventory_state": InventoryState.RETIRED},
        {"inventory_state": InventoryState.REPAIRING},
    ),
)
def test_refilling_inventory_requires_funding_binding_and_operable_state(
    overrides: dict[str, Any],
) -> None:
    refilling = replace(
        inventory(1, health_score=10),
        liquidity_state=LiquidityState.REFILLING,
        **overrides,
    )
    controller, _ = controller_for(replace(evidence(), inventory=(refilling,)))

    report = controller.run(now=NOW + timedelta(seconds=1))

    assert report.summary()["pool_size_evidence"]["input"][
        "funded_eligible_provider_count"
    ] == 0
    assert report.vacant_count == 1
    assert [slot.slot_state.value for slot in report.bundle.slots] == ["VACANT"]


def test_transfer_unknown_inventory_is_excluded_even_when_funding_eligible() -> None:
    transfer_unknown = replace(
        inventory(1, health_score=10),
        liquidity_state=LiquidityState.TRANSFER_UNKNOWN,
    )
    controller, _ = controller_for(replace(evidence(), inventory=(transfer_unknown,)))

    report = controller.run(now=NOW + timedelta(seconds=1))

    assert report.summary()["pool_size_evidence"]["input"][
        "funded_eligible_provider_count"
    ] == 0
    assert report.vacant_count == 1
    assert [slot.account_id for slot in report.bundle.slots] == [None]
    assert [slot.slot_state.value for slot in report.bundle.slots] == ["VACANT"]


def retained_planned_pool(desired_size: int) -> PlannedPoolProjection:
    return PlannedPoolProjection(
        routing_generation=8,
        cost_generation=8,
        parent_routing_generation=None,
        parent_cost_generation=None,
        desired_size=desired_size,
        ordered_channel_ids=(10, 20),
        standby_channel_ids=(30,),
        cost_input_hash="e" * 64,
        cost_output_hash="f" * 64,
        config_version="a6-provider-topn-failover-v2",
        source_hash="d" * 64,
        generation_source="observer-shadow",
    )


@pytest.mark.parametrize(
    ("observation_age", "last_shrink_age", "expected_size"),
    (
        (timedelta(days=13, hours=23, minutes=59), None, 8),
        (timedelta(days=14), timedelta(hours=71, minutes=59), 8),
        (timedelta(days=14), timedelta(hours=72), 7),
    ),
)
def test_evidence_based_controller_enforces_observation_and_shrink_boundaries(
    observation_age: timedelta,
    last_shrink_age: timedelta | None,
    expected_size: int,
) -> None:
    decision_at = NOW + timedelta(seconds=1)
    sizing = ProviderPoolSizePlanningEvidence(
        observation_started_at=decision_at - observation_age,
        last_shrink_at=(None if last_shrink_age is None else decision_at - last_shrink_age),
        p99_concurrency=1,
        p99_simultaneous_failed_slots=0,
        providers_needed_for_24h_liquidity_runway=3,
        liquidity_runway_evidence_complete=True,
        liquidity_runway_capacity_sufficient=True,
    )
    value = replace(
        evidence(pool_size=sizing),
        latest_planned=retained_planned_pool(8),
        maximum_routing_generation=8,
        maximum_cost_generation=8,
    )
    controller, _ = controller_for(
        value,
        config=ProviderPoolShadowControllerConfig(
            model=MODEL,
            size_mode="evidence_based",
            safe_concurrency_per_slot=10,
        ),
    )

    report = controller.run(now=decision_at)

    assert report.bundle.desired_size == expected_size
    assert report.summary()["pool_size_evidence"]["output"]["desired_size"] == (expected_size)


def test_planned_shrink_is_a_durable_restart_cooldown_fact() -> None:
    first_decision_at = NOW + timedelta(seconds=1)
    config = ProviderPoolShadowControllerConfig(
        model=MODEL,
        size_mode="evidence_based",
        safe_concurrency_per_slot=10,
    )
    first_evidence = replace(
        evidence(
            pool_size=ProviderPoolSizePlanningEvidence(
                observation_started_at=first_decision_at - timedelta(days=30),
                p99_concurrency=1,
                providers_needed_for_24h_liquidity_runway=3,
                liquidity_runway_evidence_complete=True,
                liquidity_runway_capacity_sufficient=True,
            )
        ),
        latest_planned=retained_planned_pool(8),
        maximum_routing_generation=8,
        maximum_cost_generation=8,
    )
    first_controller, _ = controller_for(first_evidence, config=config)

    first = first_controller.run(now=first_decision_at)

    assert first.bundle.desired_size == 7
    restarted_evidence = replace(
        first_evidence,
        latest_planned=retained_planned_pool(7),
        maximum_routing_generation=9,
        maximum_cost_generation=9,
        pool_size=replace(
            first_evidence.pool_size,
            last_shrink_at=first_decision_at,
        ),
    )
    restarted_controller, _ = controller_for(restarted_evidence, config=config)

    restarted = restarted_controller.run(now=first_decision_at + timedelta(seconds=30))

    assert restarted.bundle.desired_size == 7
    sizing_input = restarted.summary()["pool_size_evidence"]["input"]
    assert sizing_input["last_shrink_at"] == first_decision_at.isoformat()
    assert restarted.summary()["pool_size_evidence"]["output"]["action"] == "hold"


def test_pending_evidence_based_shrink_is_not_expanded_back_to_active_size() -> None:
    decision_at = NOW + timedelta(seconds=1)
    active = ActivePoolProjection(
        routing_generation=7,
        cost_generation=7,
        desired_size=8,
        ordered_channel_ids=(10, 20),
        standby_channel_ids=(30,),
        slots=(),
        cost_input_hash="a" * 64,
        billing_floor_multiplier=Decimal("0"),
        observed_at=NOW - timedelta(seconds=1),
    )
    pending_shrink = replace(
        retained_planned_pool(7),
        parent_routing_generation=7,
        parent_cost_generation=7,
    )
    sizing = ProviderPoolSizePlanningEvidence(
        observation_started_at=decision_at - timedelta(days=30),
        last_shrink_at=decision_at - timedelta(seconds=30),
        p99_concurrency=1,
        providers_needed_for_24h_liquidity_runway=3,
        liquidity_runway_evidence_complete=True,
        liquidity_runway_capacity_sufficient=True,
    )
    value = replace(
        evidence(active=active, latest_planned=pending_shrink, pool_size=sizing),
        maximum_routing_generation=8,
        maximum_cost_generation=8,
    )
    controller, _ = controller_for(
        value,
        config=ProviderPoolShadowControllerConfig(
            model=MODEL,
            size_mode="evidence_based",
            safe_concurrency_per_slot=10,
        ),
    )

    report = controller.run(now=decision_at)

    assert report.summary()["pool_size_evidence"]["input"]["current_desired_size"] == 7
    assert report.bundle.desired_size == 7
    assert report.summary()["pool_size_evidence"]["output"]["action"] == "hold"


def test_controller_resumes_same_semantic_applying_generation_after_restart() -> None:
    first, _ = controller_for(evidence())
    initial = first.run(now=NOW + timedelta(seconds=1))
    bundle = initial.bundle
    applying = PlannedPoolProjection(
        routing_generation=bundle.routing_generation,
        cost_generation=bundle.cost_generation,
        parent_routing_generation=None,
        parent_cost_generation=None,
        desired_size=bundle.desired_size,
        ordered_channel_ids=bundle.ordered_channel_ids,
        standby_channel_ids=bundle.standby_channel_ids,
        cost_input_hash=bundle.effective_cost_input_hash,
        cost_output_hash=bundle.cost_plan.cost_output_hash,
        config_version=bundle.config_version,
        source_hash=bundle.source_hash,
        generation_source=bundle.generation_source,
        generation_state="applying",
        pricing_state="confirmed",
    )
    controller, _ = controller_for(
        replace(
            evidence(source_hash="c" * 64),
            latest_planned=applying,
            maximum_routing_generation=bundle.routing_generation,
            maximum_cost_generation=bundle.cost_generation,
        )
    )

    report = controller.run(now=NOW + timedelta(minutes=10))

    assert report.reused_planned is True
    assert report.bundle.routing_generation == bundle.routing_generation
    assert report.bundle.cost_generation == bundle.cost_generation


def test_controller_blocks_new_generation_while_different_apply_is_in_progress() -> None:
    first, _ = controller_for(evidence())
    initial = first.run(now=NOW + timedelta(seconds=1))
    bundle = initial.bundle
    applying = PlannedPoolProjection(
        routing_generation=bundle.routing_generation,
        cost_generation=bundle.cost_generation,
        parent_routing_generation=None,
        parent_cost_generation=None,
        desired_size=bundle.desired_size,
        ordered_channel_ids=bundle.ordered_channel_ids,
        standby_channel_ids=bundle.standby_channel_ids,
        cost_input_hash=bundle.effective_cost_input_hash,
        cost_output_hash=bundle.cost_plan.cost_output_hash,
        config_version=bundle.config_version,
        source_hash=bundle.source_hash,
        generation_source=bundle.generation_source,
        generation_state="applying",
        pricing_state="applying",
    )
    changed_market = snapshot(
        (
            candidate(10, "0.9"),
            candidate(20, "0.2"),
            candidate(30, "0.6"),
        )
    )
    controller, _ = controller_for(
        replace(
            evidence(market=changed_market, source_hash="d" * 64),
            latest_planned=applying,
            maximum_routing_generation=bundle.routing_generation,
            maximum_cost_generation=bundle.cost_generation,
        )
    )

    with pytest.raises(
        ProviderPoolShadowControllerError,
        match="provider_shadow_controller_apply_in_progress_replan_required",
    ):
        controller.run(now=NOW + timedelta(seconds=1))


def test_active_slot_projection_preserves_enodot_binding_identity() -> None:
    row = {
        "slot_no": 1,
        "enodot_binding_id": 3001,
        "enodot_account_id": 1001,
        "a6_token_id": 2001,
        "desired_channel_id": 10,
        "desired_rank": 1,
        "desired_priority": 2,
        "desired_slot_cost_multiplier": Decimal("0.4"),
        "observed_rate_multiplier": Decimal("0.4"),
        "funding_eligible": True,
        "slot_state": "READY",
        "desired_schedulable": True,
        "observed_schedulable": True,
        "confirmed_pin_binding_id": 4001,
        "pin_confirmed": True,
        "probe_confirmed": True,
        "state_version": 1,
    }

    slot = shadow_controller._slot_from_row(row)

    assert slot.enodot_binding_id == 3001
    assert "enodot_binding_id" in shadow_controller._ACTIVE_SLOTS_SQL


class ScriptedCursor:
    def __init__(self, connection: ScriptedConnection) -> None:
        self.connection = connection
        self.rows: list[dict[str, Any]] = []
        self.description = None

    def __enter__(self) -> ScriptedCursor:
        return self

    def __exit__(self, *_args: object) -> bool:
        return False

    def execute(self, query: str, params: Any = None) -> ScriptedCursor:
        normalized = " ".join(query.split())
        self.connection.calls.append((normalized, params))
        if normalized.startswith("SET TRANSACTION"):
            self.rows = []
        elif normalized.startswith("SELECT id, model, completed_at"):
            self.rows = [
                {
                    "id": 71,
                    "model": MODEL,
                    "completed_at": NOW,
                    "page_count": 2,
                    "candidate_count": self.connection.raw_candidate_count,
                    "result_hash": self.connection.scan_result_hash,
                    "result_payload": self.connection.scan_payload,
                    "status": "completed",
                }
            ]
        elif normalized.startswith(
            "SELECT jsonb_array_length(scan.result_payload)::integer AS candidate_count,"
        ):
            self.rows = [
                {
                    "candidate_count": self.connection.raw_candidate_count,
                    "health_observer_count": self.connection.raw_health_observer_count,
                }
            ]
        elif normalized.startswith("SELECT candidate.model, candidate.channel_id"):
            self.rows = list(self.connection.candidate_rows)
        elif "AS resource_type" in normalized:
            self.rows = list(self.connection.owned_resource_rows)
        elif normalized.startswith("WITH sizing_params AS"):
            self.rows = [
                {
                    "observation_started_at": self.connection.observation_started_at,
                    "last_shrink_at": self.connection.last_shrink_at,
                    "p99_concurrency": self.connection.p99_concurrency,
                    "p99_simultaneous_failed_slots": (
                        self.connection.p99_simultaneous_failed_slots
                    ),
                    "liquidity_provider_count": (self.connection.liquidity_provider_count),
                    "liquidity_runway_evidence_complete": (
                        self.connection.liquidity_runway_evidence_complete
                    ),
                    "liquidity_runway_capacity_sufficient": (
                        self.connection.liquidity_runway_capacity_sufficient
                    ),
                    "providers_needed_for_24h_liquidity_runway": (
                        self.connection.providers_needed_for_24h_liquidity_runway
                    ),
                }
            ]
        elif "FROM a6_trade.provider_inventory" in normalized:
            evidence_inventory = "a6_financial_snapshots" in normalized
            self.rows = [
                {
                    "merchant_id": 1,
                    "enodot_binding_id": 3001,
                    "enodot_account_id": 1001,
                    "a6_token_id": 2001,
                    "inventory_state": "WARM_STANDBY",
                    "liquidity_state": "FUNDED",
                    "funding_eligible": True,
                    "schedulable": False,
                    "assigned_slot_no": None,
                    "last_financial_observed_at": self.connection.financial_observed_at,
                    "binding_state": "bound",
                    "token_status": "active",
                    "merchant_status": "supplier_submitted",
                    "roles": ["provider", "observer"],
                    "withdrawable_units": 900,
                    "user_balance_runway_seconds": (172_800 if evidence_inventory else 0),
                    "egress_healthy": True,
                    "recent_provider_success_at": (
                        NOW - timedelta(minutes=1) if evidence_inventory else None
                    ),
                }
            ]
        elif "FROM a6_trade.provider_quarantines" in normalized:
            self.rows = [{"channel_id": 99, "ends_at": NOW + timedelta(minutes=10)}]
        elif "generation.generation_state = 'active'" in normalized:
            self.rows = [] if self.connection.active_row is None else [self.connection.active_row]
        elif "generation.generation_state = 'planned'" in normalized:
            self.rows = [] if self.connection.planned_row is None else [self.connection.planned_row]
        elif "FROM a6_trade.provider_pool_slots" in normalized:
            self.rows = list(self.connection.active_slot_rows)
        elif "maximum_routing_generation" in normalized:
            self.rows = [{"maximum_routing_generation": 4, "maximum_cost_generation": 6}]
        elif "to_regclass('public.usage_logs')" in normalized:
            self.rows = [{"readable": self.connection.usage_logs_readable}]
        elif normalized.startswith("WITH realized_params AS"):
            self.rows = [
                {
                    "sample_count": self.connection.realized_cost_sample_count,
                    "realized_cost_p95_multiplier": (self.connection.realized_cost_p95_multiplier),
                }
            ]
        else:
            raise AssertionError(f"unexpected SQL: {normalized}")
        return self

    def fetchone(self) -> dict[str, Any] | None:
        return None if not self.rows else self.rows[0]

    def fetchall(self) -> list[dict[str, Any]]:
        return list(self.rows)


class ScriptedConnection:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.commits = 0
        self.rollbacks = 0
        self.closed = False
        self.raw_candidate_count = 1
        self.raw_health_observer_count = 2
        self.scan_payload = SCAN_PAYLOAD
        self.scan_result_hash = SCAN_RESULT_HASH
        self.usage_logs_readable = True
        self.realized_cost_sample_count = 0
        self.realized_cost_p95_multiplier = None
        self.observation_started_at = NOW - timedelta(days=15)
        self.last_shrink_at = None
        self.p99_concurrency = 1
        self.p99_simultaneous_failed_slots = 0
        self.liquidity_provider_count = 1
        self.liquidity_runway_evidence_complete = True
        self.liquidity_runway_capacity_sufficient = True
        self.providers_needed_for_24h_liquidity_runway = 1
        self.financial_observed_at = NOW
        self.active_row = None
        self.active_slot_rows: list[dict[str, Any]] = []
        self.planned_row = None
        self.candidate_rows = [
            {
                "model": MODEL,
                "channel_id": 10,
                "listing_id": 110,
                "supplier_id": 210,
                "price_key": Decimal("0.4"),
                "final_multiplier": Decimal("0.4"),
                "available": True,
                "is_owned": False,
                "health_state": "healthy",
                "last_success_at": NOW - timedelta(seconds=1),
                "latency_ewma_ms": Decimal("50"),
                "observed_at": NOW,
                "observer_count": 2,
                "observer_health_count": 2,
            }
        ]
        self.owned_resource_rows = [
            {"resource_type": "supplier", "resource_id": 901},
            {"resource_type": "channel", "resource_id": 902},
            {"resource_type": "listing", "resource_id": 903},
        ]

    def cursor(self) -> ScriptedCursor:
        return ScriptedCursor(self)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        self.closed = True


def test_postgres_adapter_reads_one_consistent_model_snapshot() -> None:
    connection = ScriptedConnection()
    adapter = PostgresProviderPoolPlanningAdapter(lambda: connection)

    result = adapter.load(
        MODEL,
        now=NOW + timedelta(seconds=1),
        observer_freshness_seconds=120,
        inventory_freshness_seconds=30,
        config_version="a6-provider-topn-failover-v2",
    )

    assert result.snapshot.sequence == 71
    assert result.snapshot.result_hash == snapshot_hash(result.snapshot)
    assert result.source_hash == SCAN_RESULT_HASH
    assert result.owned_supplier_ids == {901}
    assert result.owned_channel_ids == {902}
    assert result.owned_listing_ids == {903}
    assert result.inventory[0].funding_eligible is True
    assert result.inventory[0].user_balance_runway_seconds == 0
    assert result.inventory[0].withdrawable_units == 900
    assert result.inventory[0].recent_provider_success_at is None
    assert result.quarantines == {99: NOW + timedelta(minutes=10)}
    assert result.maximum_routing_generation == 4
    assert result.maximum_cost_generation == 6
    assert result.pool_size == ProviderPoolSizePlanningEvidence(
        observation_started_at=NOW + timedelta(seconds=1),
        providers_needed_for_24h_liquidity_runway=1,
    )
    assert not any(query.startswith("WITH sizing_params AS") for query, _params in connection.calls)
    assert (
        " ".join(shadow_controller._INVENTORY_SQL.split()),
        (MODEL,),
    ) in connection.calls
    all_sql = " ".join(query for query, _params in connection.calls)
    for historical_source in (
        "provider_request_envelopes",
        "a6_financial_snapshots",
        "provider_attempts",
    ):
        assert historical_source not in all_sql
    assert connection.calls[0][0].startswith(
        "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"
    )
    assert connection.commits == 1
    assert connection.rollbacks == 0
    assert connection.closed is True


def test_default_inventory_freshness_keeps_one_missed_collector_round() -> None:
    connection = ScriptedConnection()
    connection.financial_observed_at = NOW - timedelta(seconds=361)
    config = ProviderPoolShadowControllerConfig(model=MODEL)

    result = PostgresProviderPoolPlanningAdapter(lambda: connection).load(
        MODEL,
        now=NOW,
        observer_freshness_seconds=config.observer_freshness_seconds,
        inventory_freshness_seconds=config.inventory_freshness_seconds,
        config_version=config.config_version,
    )

    assert config.inventory_freshness_seconds == 900
    assert result.inventory[0].funding_eligible is True


def test_default_inventory_freshness_expires_after_bounded_grace() -> None:
    connection = ScriptedConnection()
    connection.financial_observed_at = NOW - timedelta(seconds=901)
    config = ProviderPoolShadowControllerConfig(model=MODEL)

    result = PostgresProviderPoolPlanningAdapter(lambda: connection).load(
        MODEL,
        now=NOW,
        observer_freshness_seconds=config.observer_freshness_seconds,
        inventory_freshness_seconds=config.inventory_freshness_seconds,
        config_version=config.config_version,
    )

    assert result.inventory[0].funding_eligible is False


def test_explicit_inventory_freshness_override_remains_effective() -> None:
    connection = ScriptedConnection()
    connection.financial_observed_at = NOW - timedelta(seconds=361)
    config = ProviderPoolShadowControllerConfig(
        model=MODEL,
        inventory_freshness_seconds=360,
    )

    result = PostgresProviderPoolPlanningAdapter(lambda: connection).load(
        MODEL,
        now=NOW,
        observer_freshness_seconds=config.observer_freshness_seconds,
        inventory_freshness_seconds=config.inventory_freshness_seconds,
        config_version=config.config_version,
    )

    assert result.inventory[0].funding_eligible is False


def test_postgres_adapter_reads_realized_cost_p95_from_same_snapshot() -> None:
    connection = ScriptedConnection()
    connection.realized_cost_sample_count = 135
    connection.realized_cost_p95_multiplier = Decimal("0.007197")

    result = PostgresProviderPoolPlanningAdapter(lambda: connection).load(
        MODEL,
        now=NOW + timedelta(seconds=1),
        observer_freshness_seconds=120,
        inventory_freshness_seconds=30,
        config_version="a6-provider-topn-failover-v2",
        realized_cost_window_hours=24,
        realized_cost_match_tolerance_seconds=5,
    )

    assert result.realized_cost_sample_count == 135
    assert result.realized_cost_p95_multiplier == Decimal("0.007197")
    realized_call = next(
        call for call in connection.calls if call[0].startswith("WITH realized_params AS")
    )
    assert realized_call[1] == (NOW - timedelta(hours=24, seconds=-1), 5, MODEL)


def test_postgres_adapter_fails_closed_when_usage_source_is_unreadable() -> None:
    connection = ScriptedConnection()
    connection.usage_logs_readable = False

    with pytest.raises(
        ProviderPoolShadowControllerError,
        match="provider_shadow_controller_realized_cost_source_unavailable",
    ):
        PostgresProviderPoolPlanningAdapter(lambda: connection).load(
            MODEL,
            now=NOW + timedelta(seconds=1),
            observer_freshness_seconds=120,
            inventory_freshness_seconds=30,
            config_version="a6-provider-topn-failover-v2",
        )

    assert connection.commits == 0
    assert connection.rollbacks == 1
    assert connection.closed is True


def test_realized_cost_sql_requires_unique_non_probe_success_matches() -> None:
    normalized = " ".join(shadow_controller._REALIZED_COST_SQL.split())

    assert "usage.total_cost::numeric AS successful_equivalent_cost" in normalized
    assert "event.probe_run_id IS NULL" in normalized
    assert "event.result_class = '2'" in normalized
    assert "event.a6_token_ref_id = usage.a6_token_ref_id" in normalized
    assert "event.prompt_tokens = usage.prompt_tokens" in normalized
    assert "event.completion_tokens = usage.completion_tokens" in normalized
    assert "usage.created_at - params.match_tolerance" in normalized
    assert "usage_candidate_count = 1" in normalized
    assert "provider_event_candidate_count = 1" in normalized
    assert "percentile_cont(0.95)" in normalized


def test_pool_size_sql_uses_durable_history_and_complete_postgres_facts() -> None:
    normalized = " ".join(shadow_controller._POOL_SIZE_EVIDENCE_SQL.split())

    assert "lag(generation.desired_size)" in normalized
    assert "history.desired_size < history.previous_desired_size" in normalized
    assert normalized.count("percentile_cont(0.99)") == 2
    assert "count(DISTINCT attempt.slot_no)" in normalized
    assert "attempt.state = 'failed'" in normalized
    assert "provider_request_envelopes" in normalized
    assert "provider_attempts" in normalized
    assert "provider_inventory" in normalized
    assert "a6_financial_snapshots" in normalized
    assert "interval '24 hours'" in normalized
    assert "params.decision_at - interval '14 days'" in normalized
    assert "greatest( facts.observation_started_at" in normalized
    assert "inventory.liquidity_state IN ('FUNDED', 'REFILLING')" in normalized


def test_all_funded_inventory_sql_excludes_historical_evidence_sources() -> None:
    normalized = " ".join(shadow_controller._INVENTORY_SQL.split())

    assert "a6_financial_current" in normalized
    assert "a6_financial_snapshots" not in normalized
    assert "provider_attempts" not in normalized
    assert "provider_request_envelopes" not in normalized


def test_evidence_based_adapter_reads_full_pool_size_evidence() -> None:
    connection = ScriptedConnection()

    result = PostgresProviderPoolPlanningAdapter(lambda: connection).load(
        MODEL,
        now=NOW + timedelta(seconds=1),
        observer_freshness_seconds=120,
        inventory_freshness_seconds=30,
        config_version="a6-provider-topn-failover-v2",
        size_mode="evidence_based",
    )

    assert result.pool_size == ProviderPoolSizePlanningEvidence(
        observation_started_at=NOW - timedelta(days=15),
        p99_concurrency=1,
        providers_needed_for_24h_liquidity_runway=1,
        liquidity_runway_evidence_complete=True,
        liquidity_runway_capacity_sufficient=True,
    )
    assert result.inventory[0].user_balance_runway_seconds == 172_800
    assert result.inventory[0].recent_provider_success_at == NOW - timedelta(minutes=1)
    assert (
        " ".join(shadow_controller._INVENTORY_EVIDENCE_SQL.split()),
        (MODEL,),
    ) in connection.calls
    sizing_calls = [
        call for call in connection.calls if call[0].startswith("WITH sizing_params AS")
    ]
    assert sizing_calls == [
        (
            " ".join(shadow_controller._POOL_SIZE_EVIDENCE_SQL.split()),
            (MODEL, NOW + timedelta(seconds=1), NOW - timedelta(seconds=29)),
        )
    ]


def test_postgres_adapter_preserves_shrink_cooldown_across_restarts() -> None:
    last_shrink_at = NOW - timedelta(hours=1)
    results = []
    for _ in range(2):
        connection = ScriptedConnection()
        connection.last_shrink_at = last_shrink_at
        results.append(
            PostgresProviderPoolPlanningAdapter(lambda connection=connection: connection).load(
                MODEL,
                now=NOW + timedelta(seconds=1),
                observer_freshness_seconds=120,
                inventory_freshness_seconds=30,
                config_version="a6-provider-topn-failover-v2",
                size_mode="evidence_based",
            )
        )

    assert [result.pool_size.last_shrink_at for result in results] == [
        last_shrink_at,
        last_shrink_at,
    ]


def test_postgres_adapter_fails_closed_on_incomplete_liquidity_history() -> None:
    connection = ScriptedConnection()
    connection.planned_row = {
        "routing_generation": 8,
        "cost_generation": 8,
        "parent_routing_generation": None,
        "parent_cost_generation": None,
        "desired_size": 8,
        "ordered_channel_ids": [10],
        "standby_channel_ids": [],
        "cost_input_hash": "a" * 64,
        "cost_output_hash": "b" * 64,
        "config_version": "a6-provider-topn-failover-v2",
        "source_hash": "c" * 64,
        "generation_source": "observer-shadow",
        "generation_state": "planned",
        "pricing_state": "planned",
    }
    connection.liquidity_runway_evidence_complete = False
    connection.liquidity_runway_capacity_sufficient = False
    connection.providers_needed_for_24h_liquidity_runway = 1

    result = PostgresProviderPoolPlanningAdapter(lambda: connection).load(
        MODEL,
        now=NOW + timedelta(seconds=1),
        observer_freshness_seconds=120,
        inventory_freshness_seconds=30,
        config_version="a6-provider-topn-failover-v2",
        size_mode="evidence_based",
    )

    assert result.pool_size.providers_needed_for_24h_liquidity_runway == 8
    assert result.pool_size.liquidity_runway_evidence_complete is False
    assert result.pool_size.liquidity_runway_capacity_sufficient is False


def test_postgres_adapter_separates_raw_integrity_count_from_filtered_candidates() -> None:
    connection = ScriptedConnection()
    connection.raw_candidate_count = 4
    connection.scan_payload = [
        {"channel_id": 10},
        {"channel_id": 20},
        {"channel_id": 30},
        {"channel_id": 40},
    ]
    connection.scan_result_hash = hashlib.sha256(
        json.dumps(
            connection.scan_payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()

    result = PostgresProviderPoolPlanningAdapter(lambda: connection).load(
        MODEL,
        now=NOW + timedelta(seconds=1),
        observer_freshness_seconds=120,
        inventory_freshness_seconds=30,
        config_version="a6-provider-topn-failover-v2",
    )

    assert result.snapshot.candidate_count == 1
    assert result.snapshot.health_observer_count == 2
    assert [row.channel_id for row in result.snapshot.candidates] == [10]


def test_postgres_adapter_preserves_quorum_when_all_owned_candidates_are_filtered() -> None:
    connection = ScriptedConnection()
    connection.candidate_rows = []

    result = PostgresProviderPoolPlanningAdapter(lambda: connection).load(
        MODEL,
        now=NOW + timedelta(seconds=1),
        observer_freshness_seconds=120,
        inventory_freshness_seconds=30,
        config_version="a6-provider-topn-failover-v2",
    )

    assert result.snapshot.candidates == ()
    assert result.snapshot.candidate_count == 0
    assert result.snapshot.health_observer_count == 2


def test_candidate_sql_uses_three_typed_anti_joins() -> None:
    normalized = " ".join(shadow_controller._SNAPSHOT_CANDIDATES_SQL.split())

    assert normalized.count("AND NOT EXISTS") == 3
    assert "owned_supplier.supplier_id = signal.supplier_id" in normalized
    assert "owned_channel.a6_channel_id = candidate.channel_id" in normalized
    assert "owned_listing.a6_listing_id = candidate.listing_id" in normalized
    assert "owned_supplier.supplier_id = candidate.channel_id" not in normalized
    assert "owned_channel.a6_channel_id = signal.supplier_id" not in normalized
    assert "owned_listing.a6_listing_id = candidate.channel_id" not in normalized


def test_candidate_sql_uses_immutable_scan_payload_not_signal_scan_ownership() -> None:
    candidates = " ".join(shadow_controller._SNAPSHOT_CANDIDATES_SQL.split())
    count = " ".join(shadow_controller._SNAPSHOT_CANDIDATE_COUNT_SQL.split())

    assert "jsonb_array_elements(scan.result_payload)" in candidates
    assert "jsonb_array_elements(scan.result_payload)" in count
    assert "LEFT JOIN a6_trade.a6_market_channel_signals_current signal" in candidates
    assert "THEN to_timestamp(" in candidates
    assert "signal.scan_run_id" not in candidates
    assert "signal.scan_run_id" not in count


def test_candidate_sql_merges_success_sources_and_prefers_business_latency() -> None:
    normalized = " ".join(shadow_controller._SNAPSHOT_CANDIDATES_SQL.split())

    assert (
        "CASE WHEN business_success.last_success_at IS NULL "
        "THEN candidate.last_success_at "
        "WHEN candidate.last_success_at IS NULL "
        "THEN business_success.last_success_at "
        "ELSE greatest( business_success.last_success_at, "
        "candidate.last_success_at ) END AS last_success_at"
    ) in normalized
    assert (
        "coalesce( business_latency.elapsed_ms, signal.recent_p50_ms ) AS latency_ewma_ms"
    ) in normalized
    assert normalized.count("event.result_class = '2'") == 2
    assert normalized.count("event.probe_run_id IS NULL") == 2
    assert normalized.count("ORDER BY event.occurred_at DESC, event.id DESC") == 2
    assert "event.elapsed_ms IS NOT NULL" in normalized
    assert "event.elapsed_ms >= 0" in normalized
    assert "recent_success_rate" not in normalized


def test_latest_scan_sql_skips_in_progress_observer_scan() -> None:
    latest = " ".join(shadow_controller._LATEST_SCAN_SQL.split())

    assert "status = 'completed'" in latest
    assert "completed_at IS NOT NULL" in latest
    assert "ORDER BY started_at DESC, id DESC" in latest


def test_postgres_adapter_rejects_tampered_observer_result_hash() -> None:
    connection = ScriptedConnection()
    connection.scan_result_hash = "f" * 64

    with pytest.raises(Exception, match="provider_shadow_controller_snapshot_result_hash_mismatch"):
        PostgresProviderPoolPlanningAdapter(lambda: connection).load(
            MODEL,
            now=NOW + timedelta(seconds=1),
            observer_freshness_seconds=120,
            inventory_freshness_seconds=30,
            config_version="a6-provider-topn-failover-v2",
        )


def test_shadow_command_model_mode_uses_controller_without_plan_write() -> None:
    connection = ScriptedConnection()
    connection.realized_cost_sample_count = 100
    connection.realized_cost_p95_multiplier = Decimal("0.55")

    result = run_model(
        MODEL,
        persist=False,
        connection_factory=lambda: connection,
        now=NOW + timedelta(seconds=1),
    )

    assert result["mode"] == "shadow"
    assert result["model"] == MODEL
    assert result["persisted"] is False
    assert result["activated"] is False
    assert result["remote_write_count"] == 0
    assert result["plan_write_count"] == 0
    assert result["pool_size_evidence"]["schema"] == ("a6-provider-pool-size-evidence-v1")
    assert result["pool_size_evidence"]["input"]["observation_started_at"] == (
        (NOW + timedelta(seconds=1)).isoformat()
    )
    assert not any(query.startswith("WITH sizing_params AS") for query, _params in connection.calls)

    result["inventory_projection"] = {"enabled": True, "changed_count": 0}
    result["attempt_count"] = 1
    lines = [
        json.dumps(
            {
                "__REALTIME_TIMESTAMP": str(
                    int((NOW + timedelta(seconds=offset)).timestamp() * 1_000_000)
                ),
                "MESSAGE": json.dumps(result),
            }
        )
        for offset in (0, 30)
    ]
    audit = audit_journal_lines(
        lines,
        model=MODEL,
        minimum_window_seconds=30,
        maximum_gap_seconds=31,
    )
    assert audit["success"] is True
    assert audit["semantic_input_count"] == 1
    assert audit["pool_size_input_count"] == 1
    assert audit["pool_size_output_count"] == 1


def test_postgres_plan_store_exposes_only_shadow_persistence_capability() -> None:
    store = PostgresProviderPoolPlanStore(lambda: ScriptedConnection())

    assert callable(store.plan)
    assert not hasattr(store, "mark_applying")
    assert not hasattr(store, "confirm_cost")
    assert not hasattr(store, "activate")
