from __future__ import annotations

import json
from contextlib import nullcontext
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType
from typing import Any

import pytest

from a6api_auto.trade_provider_cost import PoolDiffKind, build_cost_plan
from a6api_auto.trade_provider_failure_repair import (
    PostgresProviderFailureRepairStore,
    ProviderFailureProbeRecovery,
    ProviderFailureRepairClaim,
    ProviderFailureRepairController,
    ProviderFailureRepairTarget,
    ProviderFailureRepairWorker,
)
from a6api_auto.trade_provider_pool import (
    InMemoryProviderPoolStore,
    ProviderBundle,
    ProviderSlot,
    SlotState,
)
from a6api_auto.trade_provider_pool_apply import ProviderPoolApplyPlan
from a6api_auto.trade_provider_pool_reconciler import (
    ProviderPoolReadback,
    ProviderPoolReconciler,
    ProviderSlotReadback,
)
from a6api_auto.trade_provider_pool_shadow import ProviderPoolShadowReport
from a6api_auto.trade_provider_pool_shadow_controller import (
    ActivePoolProjection,
    ProviderPoolShadowControllerReport,
)

MODEL = "gpt-5.6-sol"
CONFIG_VERSION = "a6-provider-topn-failover-v2"


def slot(
    channel_id: int,
    *,
    state: SlotState = SlotState.READY,
    schedulable: bool = True,
) -> ProviderSlot:
    return ProviderSlot(
        slot_no=1,
        channel_id=channel_id,
        rank=1,
        priority=2,
        assignment_id=31,
        enodot_binding_id=31,
        token_id=41,
        account_id=51,
        slot_state=state,
        funding_eligible=True,
        desired_schedulable=schedulable,
        observed_schedulable=schedulable,
        desired_cost_multiplier=Decimal("1"),
        observed_cost_multiplier=Decimal("1"),
        confirmed_pin_binding_id=61,
        pin_confirmed=True,
        probe_confirmed=True,
        state_version=7,
    )


def bundle(
    generation: int,
    channel_id: int,
    *,
    parent: int | None,
    source_hash: str,
) -> ProviderBundle:
    return ProviderBundle(
        model=MODEL,
        routing_generation=generation,
        parent_routing_generation=parent,
        cost_generation=generation,
        parent_cost_generation=parent,
        generation_source="failure-repair",
        source_hash=source_hash,
        ordered_channel_ids=(channel_id,),
        standby_channel_ids=(),
        desired_size=1,
        slots=(slot(channel_id),),
        cost_plan=build_cost_plan(
            {1: Decimal("1")},
            retry_reserve_multiplier="0",
            bootstrap_retry_reserve="0",
            realized_cost_p95_multiplier=None,
        ),
    )


def apply_plan() -> ProviderPoolApplyPlan:
    desired = bundle(2, 1012, parent=1, source_hash="b" * 64)
    shadow = ProviderPoolShadowReport(
        MODEL,
        PoolDiffKind.MEMBERSHIP_AND_COST,
        2,
        2,
        False,
    )
    report = ProviderPoolShadowControllerReport(
        shadow=shadow,
        bundle=desired,
        snapshot_sequence=1,
        snapshot_candidate_count=2,
        eligible_count=2,
        top_count=1,
        standby_count=1,
        vacant_count=0,
        excluded=(),
    )
    active = ActivePoolProjection(
        routing_generation=1,
        cost_generation=1,
        desired_size=1,
        ordered_channel_ids=(1008,),
        standby_channel_ids=(1012,),
        slots=(slot(1008),),
        cost_input_hash="a" * 64,
        billing_floor_multiplier=Decimal("1"),
        observed_billing_floor_multiplier=Decimal("1"),
    )
    return ProviderPoolApplyPlan(report, active)


def claim(
    action: str = "repin_immediately",
    *,
    attempt_count: int = 1,
    target: ProviderFailureRepairTarget | None = None,
) -> ProviderFailureRepairClaim:
    return ProviderFailureRepairClaim(
        request_id=71,
        claim_token="claim-token",
        worker_id="repair-us-1",
        attempt_count=attempt_count,
        attempt_id="attempt-71",
        quarantine_id=81,
        control_action=action,
        config_version=CONFIG_VERSION,
        model=MODEL,
        routing_generation=1,
        cost_generation=1,
        current_slot=slot(1008, state=SlotState.DRAINING, schedulable=False),
        target=target,
    )


class Store:
    def __init__(self, value: ProviderFailureRepairClaim | None = None) -> None:
        self.claim = value
        self.calls: list[Any] = []
        self.bound: ProviderFailureRepairTarget | None = None
        self.active_target: ProviderFailureRepairTarget | None = None
        self.probe_recovery: ProviderFailureProbeRecovery | None = None

    def claim_next(self, *, worker_id: str):
        self.calls.append(("claim", worker_id))
        value, self.claim = self.claim, None
        return value

    def bind_target(self, repair_claim, target):
        self.calls.append(("bind", target))
        if self.bound is not None and self.bound != target:
            raise AssertionError("target drift")
        self.bound = target
        return target

    def read_active_target(self, repair_claim):
        self.calls.append(("active-readback", repair_claim.request_id))
        return self.active_target

    def read_probe_recovery(self, repair_claim):
        self.calls.append(("probe-recovery-readback", repair_claim.request_id))
        return self.probe_recovery

    def complete_probe_recovery(self, repair_claim, *, quick_probe_count):
        self.calls.append(
            ("probe-recovery-complete", repair_claim.request_id, quick_probe_count)
        )
        if self.probe_recovery is None:
            self.probe_recovery = ProviderFailureProbeRecovery(
                quick_probe_count,
                unblock_projection_id=91,
            )
        return self.probe_recovery

    def complete(self, repair_claim, *, result):
        self.calls.append(("complete", repair_claim.request_id, dict(result)))

    def fail(
        self,
        repair_claim,
        *,
        error_class,
        retry_after_seconds,
        terminal,
    ):
        self.calls.append(
            (
                "fail",
                repair_claim.request_id,
                error_class,
                retry_after_seconds,
                terminal,
            )
        )


class Planner:
    def __init__(self, plan: ProviderPoolApplyPlan, events: list[Any]) -> None:
        self.value = plan
        self.events = events

    def plan(self, repair_claim):
        self.events.append(("plan", repair_claim.request_id))
        return self.value


class ModelLease:
    def hold(self, model):
        assert model == MODEL
        return nullcontext()


class Remote:
    def __init__(self, events: list[Any], *, quick_probe_results=()) -> None:
        self.events = events
        self.schedulable = True
        self.billing_floor = Decimal("0")
        self.quick_probe_results = list(quick_probe_results)

    def drain_slot(self, value, *, idempotency_key):
        self.events.append(("drain", value.channel_id))
        self.schedulable = False
        return True

    def read_slot_schedulable(self, value):
        self.events.append(("schedulable-readback", value.channel_id, self.schedulable))
        return self.schedulable

    def probe_slot(self, value, *, idempotency_key):
        self.events.append(("probe", value.channel_id))
        if value.channel_id == 1008 and self.quick_probe_results:
            return self.quick_probe_results.pop(0)
        return True

    def stage_billing_floor(self, model, multiplier, *, idempotency_key):
        self.events.append(("floor", multiplier))
        self.billing_floor = multiplier
        return True

    def finalize_billing_floor(self, model, multiplier, *, idempotency_key):
        self.billing_floor = multiplier
        return True

    def read_billing_floor(self, model):
        return self.billing_floor

    def set_slot_cost(self, value, multiplier, *, idempotency_key):
        self.events.append(("cost", value.channel_id))
        return True

    def repin_slot(self, value, *, idempotency_key):
        self.events.append(("repin", value.channel_id))
        return True

    def set_priority(self, value, *, idempotency_key):
        self.events.append(("priority", value.channel_id))
        return True

    def set_assignment_metadata(self, desired, value, *, idempotency_key):
        self.events.append(("metadata", value.channel_id))
        return True

    def set_schedulable(self, value, enabled, *, idempotency_key):
        self.events.append(("set-schedulable", value.channel_id, enabled))
        self.schedulable = enabled
        return True

    def readback(self, desired, *, deferred_slot_nos=frozenset()):
        self.events.append(("strict-readback", self.schedulable))
        target = desired.slots[0]
        metadata = {
            "a6_provider": True,
            "a6_api_key_fingerprint": "f" * 64,
            "a6_provider_routing_generation": desired.routing_generation,
            "a6_provider_cost_generation": desired.cost_generation,
            "a6_provider_slot_no": target.slot_no,
            "a6_provider_token_ref_id": target.token_id,
            "a6_provider_channel_id": target.channel_id,
            "external_a6_token_id": 10_041,
            "a6_provider_rank": target.rank,
            "a6_provider_cost_multiplier": "1",
            "a6_provider_source_hash": desired.source_hash,
            "a6_provider_cost_input_hash": desired.effective_cost_input_hash,
            "a6_provider_cost_output_hash": desired.cost_plan.cost_output_hash,
        }
        return ProviderPoolReadback(
            MODEL,
            desired.cost_plan.transition_floor_multiplier,
            MappingProxyType(
                {
                    1: ProviderSlotReadback(
                        slot_no=1,
                        account_id=target.account_id,
                        token_id=target.token_id,
                        channel_id=target.channel_id,
                        priority=target.priority,
                        rate_multiplier=Decimal("1"),
                        schedulable=self.schedulable,
                        pin_binding_id=61,
                        pin_confirmed=True,
                        probe_confirmed=True,
                        assignment_metadata=metadata,
                    )
                }
            ),
        )


class OrderedPoolStore(InMemoryProviderPoolStore):
    def __init__(self, events: list[Any]) -> None:
        super().__init__()
        self.events = events

    def activate(self, desired, *, deferred_slot_nos=()):
        self.events.append(("activate", desired.routing_generation))
        return super().activate(desired, deferred_slot_nos=deferred_slot_nos)


def activate_initial(store: InMemoryProviderPoolStore) -> None:
    initial = bundle(1, 1008, parent=None, source_hash="a" * 64)
    store.plan(
        initial,
        expected_routing_generation=None,
        expected_cost_generation=None,
    )
    store.mark_applying(initial)
    store.confirm_cost(
        initial,
        observed_output_hash=initial.cost_plan.cost_output_hash,
        observed_billing_floor=initial.cost_plan.transition_floor_multiplier,
    )
    store.activate(initial)


def controller(
    store: Store,
    remote: Remote,
    events: list[Any],
    *,
    probes=2,
    probe_enabled=True,
    activation_publisher=None,
):
    pool_store = OrderedPoolStore(events)
    activate_initial(pool_store)
    events.clear()
    plan = apply_plan()

    def factory(_plan, ledger):
        return ProviderPoolReconciler(
            pool_store,
            remote,
            ledger=ledger,
            require_remote_readback=True,
            activation_probe_required=probe_enabled,
        )

    return ProviderFailureRepairController(
        store,
        remote,
        Planner(plan, events),
        factory,
        model_lease=ModelLease(),
        activation_publisher=activation_publisher,
        quick_probe_attempts=probes,
        quick_probe_enabled=probe_enabled,
    )


def test_immediate_repair_drains_before_plan_and_reads_remote_before_ready_cas() -> None:
    events: list[Any] = []
    repair_store = Store()
    remote = Remote(events)

    result = controller(repair_store, remote, events).execute(claim())

    assert result.status == "completed"
    names = [event[0] for event in events]
    assert names[:3] == ["plan", "drain", "schedulable-readback"]
    strict_readbacks = [
        index for index, name in enumerate(names) if name == "strict-readback"
    ]
    assert names.index("repin") < strict_readbacks[0]
    assert strict_readbacks[0] < names.index("activate")
    assert names.index("activate") < names.index("set-schedulable")
    assert names.index("set-schedulable") < strict_readbacks[-1]
    assert result.target == ProviderFailureRepairTarget(2, 2, 1, 1012, 51, 41, "b" * 64)
    assert any(call[0] == "complete" for call in repair_store.calls)


def test_probe_then_repin_uses_bounded_probe_before_replacement() -> None:
    events: list[Any] = []
    repair_store = Store()
    remote = Remote(events, quick_probe_results=(False, False, True))

    result = controller(repair_store, remote, events, probes=2).execute(claim("probe_then_repin"))

    assert result.status == "completed"
    assert result.quick_probe_count == 2
    plan_index = events.index(("plan", 71))
    drain_index = events.index(("drain", 1008))
    repin_index = events.index(("repin", 1012))
    assert drain_index < plan_index < repin_index
    assert events[drain_index:plan_index].count(("probe", 1008)) == 2


def test_probe_disabled_repair_replaces_from_observer_plan_without_network_probe() -> None:
    events: list[Any] = []
    repair_store = Store()
    remote = Remote(events, quick_probe_results=(True,))

    result = controller(
        repair_store,
        remote,
        events,
        probe_enabled=False,
    ).execute(claim("probe_then_repin"))

    assert result.status == "completed"
    assert result.quick_probe_count == 0
    assert not any(event[0] == "probe" for event in events)
    assert events.index(("plan", 71)) < events.index(("repin", 1012))


def test_successful_quick_probe_restores_original_slot_without_replacement_write() -> None:
    events: list[Any] = []
    repair_store = Store()
    remote = Remote(events, quick_probe_results=(True,))

    result = controller(repair_store, remote, events).execute(
        claim("probe_then_repin")
    )

    assert result.status == "completed"
    assert result.quick_probe_count == 1
    assert result.target is None
    assert remote.schedulable is True
    assert ("probe", 1008) in events
    assert ("set-schedulable", 1008, True) in events
    assert not any(
        event[0] in {"repin", "activate", "floor", "cost", "metadata"}
        for event in events
    )
    assert not any(call[0] in {"bind", "complete"} for call in repair_store.calls)
    assert repair_store.calls[-1] == ("probe-recovery-complete", 71, 1)


def test_successful_quick_probe_does_not_depend_on_replacement_plan_availability() -> None:
    class ForbiddenPlanner:
        def plan(self, _repair_claim):
            raise AssertionError("replacement planner must stay cold")

    events: list[Any] = []
    repair_store = Store()
    remote = Remote(events, quick_probe_results=(True,))
    pool_store = OrderedPoolStore(events)
    activate_initial(pool_store)
    events.clear()

    def forbidden_factory(*_args):
        raise AssertionError("replacement reconciler must stay cold")

    repair_controller = ProviderFailureRepairController(
        repair_store,
        remote,
        ForbiddenPlanner(),
        forbidden_factory,
        model_lease=ModelLease(),
    )

    result = repair_controller.execute(claim("probe_then_repin"))

    assert result.status == "completed"
    assert result.quick_probe_count == 1
    assert not any(call[0] == "bind" for call in repair_store.calls)
    assert not any(event[0] == "repin" for event in events)


def test_completed_quick_probe_recovery_replay_has_zero_remote_or_replacement_write() -> None:
    events: list[Any] = []
    repair_store = Store()
    remote = Remote(events, quick_probe_results=(True,))
    repair_controller = controller(repair_store, remote, events)
    repair_claim = claim("probe_then_repin")

    first = repair_controller.execute(repair_claim)
    events.clear()
    calls_before_replay = len(repair_store.calls)
    replay = repair_controller.execute(repair_claim)

    assert first.status == replay.status == "completed"
    assert replay.quick_probe_count == 1
    assert events == []
    assert repair_store.calls[calls_before_replay:] == [
        ("probe-recovery-readback", 71)
    ]


def test_remote_drain_readback_mismatch_retries_without_repinning() -> None:
    class DriftingRemote(Remote):
        def drain_slot(self, value, *, idempotency_key):
            self.events.append(("drain", value.channel_id))
            return False

    events: list[Any] = []
    repair_store = Store()
    remote = DriftingRemote(events)

    result = controller(repair_store, remote, events).execute(claim())

    assert result.status == "pending"
    assert result.error_class == "provider_failure_repair_remote_drain_readback_mismatch"
    assert not any(event[0] == "repin" for event in events)
    assert repair_store.calls[-1][:3] == (
        "fail",
        71,
        "provider_failure_repair_remote_drain_readback_mismatch",
    )


def test_bound_target_active_readback_completes_interrupted_request_without_remote_write() -> None:
    target = ProviderFailureRepairTarget(2, 2, 1, 1012, 51, 41, "b" * 64)
    repair_store = Store()
    repair_store.active_target = target
    events: list[Any] = []
    remote = Remote(events)

    result = controller(repair_store, remote, events).execute(claim(target=target))

    assert result.status == "completed"
    assert result.target == target
    assert events == []
    assert repair_store.calls[-1][0] == "complete"


def test_active_target_recovery_republishes_dispatch_before_completion() -> None:
    target = ProviderFailureRepairTarget(2, 2, 1, 1012, 51, 41, "b" * 64)
    repair_store = Store()
    repair_store.active_target = target
    events: list[Any] = []
    remote = Remote(events)

    def publish(model, routing_generation, cost_generation):
        events.append(("publish", model, routing_generation, cost_generation))

    result = controller(
        repair_store,
        remote,
        events,
        activation_publisher=publish,
    ).execute(claim(target=target))

    assert result.status == "completed"
    assert events == [("publish", MODEL, 2, 2)]
    assert [call[0] for call in repair_store.calls[-2:]] == [
        "active-readback",
        "complete",
    ]


def test_active_target_dispatch_failure_remains_retryable() -> None:
    target = ProviderFailureRepairTarget(2, 2, 1, 1012, 51, 41, "b" * 64)
    repair_store = Store()
    repair_store.active_target = target
    events: list[Any] = []

    def publish(*_args):
        raise RuntimeError("redis down")

    result = controller(
        repair_store,
        Remote(events),
        events,
        activation_publisher=publish,
    ).execute(claim(target=target))

    assert result.status == "pending"
    assert result.error_class == "runtimeerror"
    assert not any(call[0] == "complete" for call in repair_store.calls)
    assert repair_store.calls[-1][:2] == ("fail", 71)


def test_worker_claims_one_request_and_reports_idle_after_queue_is_empty() -> None:
    events: list[Any] = []
    repair_store = Store(claim())
    remote = Remote(events)
    repair_controller = controller(repair_store, remote, events)
    worker = ProviderFailureRepairWorker(
        repair_store,
        repair_controller,
        worker_id="repair-us-1",
    )

    assert worker.run_once().status == "completed"
    assert worker.run_once().status == "idle"


def test_worker_replays_failed_post_recovery_projection_before_next_claim() -> None:
    events: list[Any] = []
    repair_store = Store(claim("probe_then_repin"))
    remote = Remote(events, quick_probe_results=(True,))
    repair_controller = controller(repair_store, remote, events)
    projection_calls = 0

    def replay_fast_blocks():
        nonlocal projection_calls
        projection_calls += 1
        if projection_calls == 2:
            raise RuntimeError("redis unavailable")
        return {
            "processed": 1 if projection_calls == 3 else 0,
            "projected": 1 if projection_calls == 3 else 0,
            "superseded": 0,
            "failed": 0,
        }

    worker = ProviderFailureRepairWorker(
        repair_store,
        repair_controller,
        worker_id="repair-us-1",
        fast_block_replayer=replay_fast_blocks,
    )

    with pytest.raises(RuntimeError, match="redis unavailable"):
        worker.run_once()
    events_after_recovery = list(events)

    resumed = worker.run_once()

    assert resumed.status == "idle"
    assert projection_calls == 3
    assert events == events_after_recovery
    assert repair_store.probe_recovery == ProviderFailureProbeRecovery(1, 91)


@dataclass
class Database:
    row: dict[str, Any]
    queries: list[tuple[str, tuple[Any, ...]]]
    commits: int = 0
    rollbacks: int = 0
    request_status: str = "running"
    request_result: dict[str, Any] | None = None
    quarantine_state: str = "active"
    slot_state: str = "DRAINING"
    unblock_projection: dict[str, Any] | None = None

    def connect(self):
        return Connection(self)


class Cursor:
    def __init__(self, database: Database) -> None:
        self.database = database
        self.result = None
        self.description = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, query, params=None):
        sql = " ".join(query.split())
        values = tuple(params or ())
        self.database.queries.append((sql, values))
        self.result = None
        if sql.startswith(
            "SELECT request.result, projection.id AS unblock_projection_id"
        ):
            if (
                self.database.request_status == "completed"
                and self.database.quarantine_state == "closed"
                and self.database.slot_state == "READY"
                and self.database.unblock_projection is not None
            ):
                self.result = {
                    "result": self.database.request_result,
                    "unblock_projection_id": self.database.unblock_projection["id"],
                }
        elif sql.startswith("SELECT request.id AS request_id"):
            if self.database.request_status == "running":
                self.result = {
                    "request_id": self.database.row["id"],
                    "quarantine_id": self.database.row["quarantine_id"],
                    "quarantine_state": self.database.quarantine_state,
                    "slot_id": 101,
                    "state_version": self.database.row["slot_state_version"],
                    "slot_state": self.database.slot_state,
                    "desired_schedulable": False,
                    "observed_schedulable": False,
                }
        elif "SET state = 'probing', last_probe_at = now()" in sql:
            if self.database.quarantine_state == "active":
                self.database.quarantine_state = "probing"
                self.result = {"id": values[0], "state": "probing"}
        elif "SET desired_schedulable = true" in sql:
            if self.database.slot_state == "DRAINING":
                self.database.slot_state = "READY"
                self.result = {
                    "id": values[0],
                    "slot_state": "READY",
                    "desired_schedulable": True,
                    "observed_schedulable": True,
                    "state_version": values[1] + 1,
                }
        elif "SET state = 'closed', ends_at = now()" in sql:
            if self.database.quarantine_state == "probing":
                self.database.quarantine_state = "closed"
                self.result = {"id": values[1], "state": "closed"}
        elif sql.startswith("INSERT INTO a6_trade.provider_fast_block_outbox"):
            if self.database.unblock_projection is None:
                self.database.unblock_projection = {
                    "id": 91,
                    "idempotency_key": values[0],
                    "operation": "unblock",
                    "model": values[1],
                    "routing_generation": values[2],
                    "cost_generation": values[3],
                    "slot_no": values[4],
                    "channel_id": values[5],
                    "attempt_id": None,
                    "quarantine_id": values[6],
                    "projection_key": values[7],
                    "payload": json.loads(values[8]),
                    "projection_state": "pending",
                    "failure_occurred_at": None,
                    "recovery_verified_at": "2026-07-30T00:00:00Z",
                }
                self.result = self.database.unblock_projection
        elif sql.startswith(
            "SELECT id, idempotency_key, operation, model, routing_generation"
        ) and "FROM a6_trade.provider_fast_block_outbox" in sql:
            self.result = self.database.unblock_projection
        elif sql.startswith("WITH candidate AS"):
            self.result = {
                **self.database.row,
                "claim_token": str(values[1]),
                "claimed_by": str(values[2]),
            }
        elif "SET target_routing_generation = COALESCE" in sql:
            self.result = {
                "target_routing_generation": values[0],
                "target_cost_generation": values[1],
                "slot_no": self.database.row["slot_no"],
                "target_channel_id": values[2],
                "target_account_id": values[3],
                "target_token_id": values[4],
                "target_source_hash": values[5],
            }
        elif "SET status = 'completed', result" in sql:
            self.database.request_status = "completed"
            self.database.request_result = json.loads(values[0])
            self.result = {"id": values[1]}
        elif "SET status = %s, claim_token = NULL" in sql:
            self.result = {"id": values[4]}

    def fetchone(self):
        return self.result


class Connection:
    def __init__(self, database: Database) -> None:
        self.database = database

    def cursor(self):
        return Cursor(self.database)

    def commit(self):
        self.database.commits += 1

    def rollback(self):
        self.database.rollbacks += 1

    def close(self):
        pass


def repair_row() -> dict[str, Any]:
    return {
        "id": 71,
        "attempt_count": 1,
        "attempt_id": "attempt-71",
        "quarantine_id": 81,
        "control_action": "repin_immediately",
        "config_version": CONFIG_VERSION,
        "model": MODEL,
        "routing_generation": 1,
        "cost_generation": 1,
        "slot_no": 1,
        "enodot_binding_id": 31,
        "enodot_account_id": 51,
        "a6_token_id": 41,
        "failed_channel_id": 1008,
        "desired_rank": 1,
        "desired_priority": 2,
        "desired_slot_cost_multiplier": Decimal("1"),
        "observed_rate_multiplier": Decimal("1"),
        "funding_eligible": True,
        "confirmed_pin_binding_id": 61,
        "pin_confirmed": True,
        "probe_confirmed": True,
        "slot_state_version": 7,
        "target_routing_generation": None,
    }


def test_postgres_store_materializes_then_claims_with_skip_locked() -> None:
    database = Database(repair_row(), [])
    store = PostgresProviderFailureRepairStore(database.connect)

    repair_claim = store.claim_next(worker_id="repair-us-1")

    assert repair_claim is not None
    assert repair_claim.current_slot.channel_id == 1008
    assert repair_claim.claim_token
    rendered = "\n".join(query for query, _ in database.queries)
    claim_sql = next(
        query for query, _ in database.queries if query.startswith("WITH candidate AS")
    )
    assert "INSERT INTO a6_trade.provider_failure_repair_requests" in rendered
    assert "failure_control_action IN ( 'repin_immediately', 'probe_then_repin' )" in rendered
    assert "FOR UPDATE OF request SKIP LOCKED" in rendered
    assert (
        "WHERE ( ( request.status = 'pending' AND request.next_attempt_at <= now() ) "
        "OR ( request.status = 'running' AND request.claimed_at <= now() - "
        "(%s * interval '1 second') ) ) AND ( EXISTS (" in claim_sql
    )
    assert "request.target_routing_generation" in rendered
    assert "generation.source_hash = request.target_source_hash" in rendered
    assert database.commits == 1
    assert database.rollbacks == 0


def test_postgres_store_freezes_target_and_uses_claim_token_for_finish_cas() -> None:
    database = Database(repair_row(), [])
    store = PostgresProviderFailureRepairStore(database.connect)
    repair_claim = claim()
    target = ProviderFailureRepairTarget(2, 2, 1, 1012, 51, 41, "b" * 64)

    assert store.bind_target(repair_claim, target) == target
    store.complete(repair_claim, result={"target_source_hash": target.source_hash})

    rendered = "\n".join(query for query, _ in database.queries)
    assert "target_routing_generation = COALESCE" in rendered
    assert "WHERE id = %s AND status = 'running' AND claim_token = %s" in rendered
    completion_params = next(
        params for query, params in database.queries if "SET status = 'completed', result" in query
    )
    assert json.loads(completion_params[0]) == {"target_source_hash": "b" * 64}
    assert completion_params[1:] == (71, "claim-token")
    assert database.commits == 2


def test_postgres_probe_recovery_atomically_restores_and_deduplicates_unblock() -> None:
    database = Database(repair_row(), [])
    store = PostgresProviderFailureRepairStore(database.connect)
    repair_claim = claim("probe_then_repin")

    recovered = store.complete_probe_recovery(
        repair_claim,
        quick_probe_count=1,
    )
    replay = store.complete_probe_recovery(
        repair_claim,
        quick_probe_count=1,
    )

    assert recovered == ProviderFailureProbeRecovery(1, 91)
    assert replay == ProviderFailureProbeRecovery(1, 91, deduplicated=True)
    assert database.slot_state == "READY"
    assert database.quarantine_state == "closed"
    assert database.request_status == "completed"
    assert database.request_result == {
        "outcome": "probe_recovered",
        "routing_generation": 1,
        "cost_generation": 1,
        "slot_no": 1,
        "channel_id": 1008,
        "account_id": 51,
        "token_id": 41,
        "confirmed_pin_binding_id": 61,
        "quick_probe_count": 1,
        "unblock_projection_id": 91,
        "original_assignment_preserved": True,
        "replacement_written": False,
    }
    rendered = "\n".join(query for query, _ in database.queries)
    assert "SET state = 'probing'" in rendered
    assert "slot_state = 'READY'" in rendered
    assert "SET state = 'closed'" in rendered
    assert rendered.count("INSERT INTO a6_trade.provider_fast_block_outbox") == 1
    assert "VALUES ( %s, 'unblock'" in rendered
    assert "SET status = 'completed', result" in rendered
    assert not any(call.startswith("SET target_routing_generation") for call, _ in database.queries)
    assert database.commits == 2
    assert database.rollbacks == 0


def test_postgres_store_retry_and_terminal_failure_clear_claim_ownership() -> None:
    database = Database(repair_row(), [])
    store = PostgresProviderFailureRepairStore(database.connect)

    store.fail(
        claim(),
        error_class="provider_failure_repair_probe_failed",
        retry_after_seconds=30,
        terminal=False,
    )
    store.fail(
        claim(attempt_count=8),
        error_class="provider_failure_repair_probe_failed",
        retry_after_seconds=30,
        terminal=True,
    )

    mutations = [
        params
        for query, params in database.queries
        if "SET status = %s, claim_token = NULL" in query
    ]
    assert mutations[0][:4] == (
        "pending",
        False,
        30,
        "provider_failure_repair_probe_failed",
    )
    assert mutations[1][:4] == (
        "failed",
        True,
        30,
        "provider_failure_repair_probe_failed",
    )


def test_failure_repair_migration_is_packaged_with_durable_cas_contract() -> None:
    migration = Path("migrations/0039_provider_failure_repair_requests.sql").read_text()
    packaged = Path(
        "src/a6api_auto/data/migrations/0039_provider_failure_repair_requests.sql"
    ).read_text()

    assert migration == packaged
    assert "provider_failure_repair_requests" in migration
    assert "repin_immediately" in migration
    assert "probe_then_repin" in migration
    assert "claim_token text" in migration
    assert "provider_failure_repair_target_complete" in migration
    assert "provider_failure_repair_claim_idx" in migration
    assert "provider_failure_repair_one_open_slot_idx" in migration
    assert "request bodies" in migration
    assert "api key" in migration.casefold()


def test_completion_result_contains_only_frozen_identities_and_hashes() -> None:
    repair_store = Store()
    events: list[Any] = []
    result = controller(repair_store, Remote(events), events).execute(claim())
    completion = next(call[2] for call in repair_store.calls if call[0] == "complete")

    assert result.status == "completed"
    assert json.dumps(completion, sort_keys=True)
    assert not {"request_body", "response_body", "api_key", "token"} & set(completion)
