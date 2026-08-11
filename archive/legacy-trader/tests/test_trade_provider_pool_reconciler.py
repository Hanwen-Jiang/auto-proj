from __future__ import annotations

import hashlib
import json
import threading
import time
from contextlib import nullcontext
from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace

import pytest

from a6api_auto.errors import A6MutationError
from a6api_auto.trade_provider_cost import PoolDiffKind, build_cost_plan
from a6api_auto.trade_provider_pool import (
    GenerationState,
    InMemoryProviderPoolStore,
    PricingState,
    ProviderBundle,
    ProviderSlot,
    SlotState,
)
from a6api_auto.trade_provider_pool_reconciler import (
    ActionClaimDisposition,
    IdempotencyLedger,
    PostgresProviderActionLedger,
    PostgresProviderActivationBarrier,
    PostgresProviderModelLease,
    ProviderPlatformBackoffActive,
    ProviderPoolReadback,
    ProviderPoolReconcileError,
    ProviderPoolReconciler,
    ProviderProbeResult,
    ProviderReadbackRetryRequired,
    ProviderRoundDeadlineExceeded,
    ProviderSlotReadback,
    ReplanRequired,
    _temporary_priority_slots,
)

KEY_FINGERPRINT = "a" * 64


def test_remote_action_phase_is_bounded_concurrent_and_result_order_is_stable() -> None:
    reconciler = ProviderPoolReconciler(
        InMemoryProviderPoolStore(),
        Remote(),
        remote_max_workers=2,
    )
    rendezvous = threading.Barrier(2, timeout=1)
    actions: list[str] = []

    def mutate(_key: str) -> bool:
        rendezvous.wait()
        return True

    reconciler._run_once_actions(
        bundle(),
        (("first", 1, mutate), ("second", 2, mutate)),
        actions,
    )

    assert actions == ["first", "second"]


def test_remote_action_phase_checks_round_deadline_before_mutation() -> None:
    reconciler = ProviderPoolReconciler(
        InMemoryProviderPoolStore(),
        Remote(),
        remote_max_workers=2,
        round_deadline=time.monotonic() - 1,
    )

    with pytest.raises(
        ProviderRoundDeadlineExceeded,
        match="provider_reconcile_round_deadline_exceeded",
    ):
        reconciler._run_once_actions(
            bundle(),
            (("late", 1, lambda _key: True),),
            [],
        )

    actions: list[str] = []
    with reconciler._suspend_round_deadline():
        reconciler._run_once_actions(
            bundle(),
            (("compensate", 2, lambda _key: True),),
            actions,
        )
    assert actions == ["compensate"]
    with pytest.raises(ProviderRoundDeadlineExceeded):
        reconciler._checkpoint()


def legacy_provider_action_key(desired, action, target):
    document = {
        "config_version": desired.config_version,
        "model": desired.model,
        "routing_generation": desired.routing_generation,
        "cost_generation": desired.cost_generation,
        "action": action,
        "target": target,
    }
    digest = hashlib.sha256(
        json.dumps(
            document,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    scope_digest = hashlib.sha256(
        f"{desired.config_version}\0{desired.model}".encode()
    ).hexdigest()
    action_digest = hashlib.sha256(str(action).encode()).hexdigest()
    return (
        f"a6pp:{scope_digest[:16]}:r{desired.routing_generation}:"
        f"c{desired.cost_generation}:a{action_digest[:16]}:t{digest[:24]}"
    )


def assignment_metadata(desired, slot):
    return {
        "a6_provider": True,
        "a6_api_key_fingerprint": KEY_FINGERPRINT,
        "a6_provider_routing_generation": desired.routing_generation,
        "a6_provider_cost_generation": desired.cost_generation,
        "a6_provider_slot_no": slot.slot_no,
        "a6_provider_token_ref_id": slot.token_id,
        "a6_provider_channel_id": slot.channel_id,
        "external_a6_token_id": 10_000 + slot.token_id,
        "a6_provider_rank": slot.rank,
        "a6_provider_cost_multiplier": format(
            desired.cost_plan.desired_slot_costs[slot.slot_no],
            "f",
        ),
        "a6_provider_source_hash": desired.source_hash,
        "a6_provider_cost_input_hash": desired.effective_cost_input_hash,
        "a6_provider_cost_output_hash": desired.cost_plan.cost_output_hash,
    }


def request_plane_capability(model="gpt-5.6-sol", **overrides):
    document = {
        "capability": "a6_provider_runtime",
        "model": model,
        "instance_id": "sub2api-fixture-1",
        "contract_version": "1.0",
        "pre_send_advisory_lock_namespace": 0x41365052,
        "paid_send_max_attempts": 1,
        "http_pre_send_lease": True,
        "sse_pre_send_lease": True,
        "ws_per_billing_turn_budget": True,
        "dispatch_lease_enabled": True,
    }
    document.update(overrides)
    return document


def dispatch_publication(
    *,
    routing_generation=1,
    cost_generation=1,
    state="active",
    changed=True,
):
    return SimpleNamespace(
        key="dispatch-key",
        digest="f" * 64,
        changed=changed,
        routing_generation=routing_generation,
        cost_generation=cost_generation,
        state=state,
    )


class Remote:
    def __init__(self) -> None:
        self.calls = []
        self.billing_floor = Decimal("0")
        self.schedulable = {}

    def stage_billing_floor(self, model, multiplier, *, idempotency_key):
        self.calls.append(("floor", model, multiplier, idempotency_key))
        self.billing_floor = multiplier

    def finalize_billing_floor(self, model, multiplier, *, idempotency_key):
        self.calls.append(("final-floor", model, multiplier, idempotency_key))
        self.billing_floor = multiplier

    def read_billing_floor(self, _model):
        return self.billing_floor

    def set_slot_cost(self, slot, multiplier, *, idempotency_key):
        self.calls.append(("cost", slot.slot_no, multiplier, idempotency_key))

    def drain_slot(self, slot, *, idempotency_key):
        self.calls.append(("drain", slot.slot_no, idempotency_key))

    def repin_slot(self, slot, *, idempotency_key):
        self.calls.append(("repin", slot.slot_no, idempotency_key))

    def probe_slot(self, slot, *, idempotency_key):
        self.calls.append(("probe", slot.slot_no, idempotency_key))
        return True

    def set_priority(self, slot, *, idempotency_key):
        self.calls.append(("priority", slot.slot_no, idempotency_key))

    def set_assignment_metadata(self, desired, slot, *, idempotency_key):
        self.calls.append(("metadata", slot.slot_no, idempotency_key))

    def set_schedulable(self, slot, enabled, *, idempotency_key):
        self.calls.append(("schedulable", slot.slot_no, enabled, idempotency_key))
        key = (slot.account_id, slot.token_id)
        changed = self.schedulable.get(key, True) is not enabled
        self.schedulable[key] = enabled
        return changed

    def read_slot_schedulable(self, slot):
        return self.schedulable.get((slot.account_id, slot.token_id), True)


class ReadbackRemote(Remote):
    def __init__(self, *, billing_floor=None, no_pin_slot_nos=frozenset()) -> None:
        super().__init__()
        self.readback_billing_floor = billing_floor
        self.no_pin_slot_nos = frozenset(no_pin_slot_nos)

    def readback(self, desired, *, deferred_slot_nos=frozenset()):
        self.calls.append(("readback", desired.routing_generation))
        return ProviderPoolReadback(
            model=desired.model,
            billing_floor_multiplier=(
                desired.cost_plan.transition_floor_multiplier
                if self.readback_billing_floor is None
                else self.readback_billing_floor
            ),
            slots={
                slot.slot_no: ProviderSlotReadback(
                    slot_no=slot.slot_no,
                    account_id=slot.account_id,
                    token_id=(
                        None
                        if slot.slot_no in self.no_pin_slot_nos
                        else slot.token_id
                    ),
                    channel_id=(
                        None
                        if slot.slot_no in self.no_pin_slot_nos
                        else slot.channel_id
                    ),
                    priority=slot.priority,
                    rate_multiplier=desired.cost_plan.desired_slot_costs[slot.slot_no],
                    schedulable=self.read_slot_schedulable(slot),
                    pin_binding_id=(
                        None
                        if slot.slot_no in self.no_pin_slot_nos
                        else 1000 + slot.slot_no
                    ),
                    pin_confirmed=slot.slot_no not in self.no_pin_slot_nos,
                    probe_confirmed=slot.slot_no not in self.no_pin_slot_nos,
                    assignment_metadata=assignment_metadata(desired, slot),
                )
                for slot in desired.slots
                if slot.slot_state is SlotState.READY
            },
        )


class LedgerCursor:
    def __init__(self, database):
        self.database = database
        self.result = None
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, query, params=None):
        normalized = " ".join(query.split())
        self.database.calls.append((normalized, params))
        self.result = None
        self.rowcount = 0
        if (
            normalized.startswith("SELECT id, idempotency_key, status, desired_state")
            and "WHERE action_type = %s" in normalized
        ):
            if self.database.action is not None:
                desired = json.loads(params[5])
                action = self.database.action
                if (
                    action["action_type"] == params[1]
                    and action["entity_type"] == params[2]
                    and action["entity_id"] == params[3]
                    and action["config_version"] == params[4]
                    and action["desired_state"] == desired
                ):
                    self.result = self._claimable_action(action)
        elif normalized.startswith("INSERT INTO a6_trade.trade_control_actions"):
            if self.database.action is None:
                self.database.action = {
                    "id": 1,
                    "idempotency_key": params[0],
                    "action_type": params[1],
                    "entity_type": params[2],
                    "entity_id": params[3],
                    "status": "started",
                    "desired_state": json.loads(params[4]),
                    "config_version": params[5],
                    "claimable": False,
                }
                self.result = dict(self.database.action)
                self.rowcount = 1
        elif (
            normalized.startswith("SELECT id, idempotency_key, status, desired_state")
            and "WHERE idempotency_key = %s" in normalized
        ):
            if (
                self.database.action is not None
                and self.database.action["idempotency_key"] == params[1]
            ):
                self.result = self._claimable_action(self.database.action)
        elif normalized.startswith(
            "UPDATE a6_trade.trade_control_action_attempts SET status = 'failed'"
        ):
            for attempt in self.database.attempts:
                if attempt["action_id"] == params[2] and attempt["status"] == "started":
                    attempt["status"] = "failed"
                    attempt["error_class"] = params[0]
                    self.rowcount += 1
        elif normalized.startswith("UPDATE a6_trade.trade_control_actions SET status = 'started'"):
            self.database.action["status"] = "started"
            self.rowcount = 1
        elif normalized.startswith("SELECT coalesce(max(attempt_no)"):
            self.result = {"attempt_no": len(self.database.attempts) + 1}
        elif normalized.startswith("INSERT INTO a6_trade.trade_control_action_attempts"):
            self.database.attempts.append(
                {"action_id": params[0], "attempt_no": params[1], "status": "started"}
            )
            self.rowcount = 1
        elif normalized.startswith("UPDATE a6_trade.trade_control_actions SET observed_after"):
            if self.database.action["status"] == "started":
                self.database.action["status"] = params[1]
                self.database.action["error_class"] = params[2]
                self.database.action["observed_after"] = (
                    json.loads(params[0]) if params[0] is not None else None
                )
                self.rowcount = 1
        elif normalized.startswith(
            "UPDATE a6_trade.trade_control_action_attempts SET observed_after"
        ):
            matching = [
                item
                for item in self.database.attempts
                if item["action_id"] == params[5]
                and item["attempt_no"] == params[6]
                and item["status"] == "started"
            ]
            if matching:
                matching[0]["status"] = params[1]
                matching[0]["error_class"] = params[2]
                self.rowcount = 1
        return self

    def _claimable_action(self, value):
        action = dict(value)
        action["claimable"] = (
            action["status"] not in {"started", "applying"} or self.database.force_stale
        )
        return action

    def fetchone(self):
        return self.result


class LedgerConnection:
    def __init__(self, database):
        self.database = database

    def cursor(self):
        return LedgerCursor(self.database)

    def commit(self):
        self.database.commits += 1

    def rollback(self):
        self.database.rollbacks += 1

    def close(self):
        self.database.closes += 1


class LedgerDatabase:
    def __init__(self):
        self.action = None
        self.attempts = []
        self.calls = []
        self.commits = 0
        self.rollbacks = 0
        self.closes = 0
        self.force_stale = False

    def connect(self):
        return LedgerConnection(self)


class SagaStore:
    def __init__(self):
        self.calls = []

    def plan(self, bundle, **kwargs):
        self.calls.append(("plan", bundle.routing_generation, kwargs))

    def mark_applying(self, bundle):
        self.calls.append(("applying", bundle.routing_generation))

    def confirm_cost(self, bundle, **kwargs):
        self.calls.append(("confirm", bundle.cost_generation, kwargs))

    def activate(self, bundle, *, deferred_slot_nos=()):
        self.calls.append(("activate", bundle.routing_generation))


class SlotAwareRemote(Remote):
    def set_slot_cost(self, slot, multiplier, *, idempotency_key):
        self.calls.append(("cost", slot.slot_no, slot.account_id, idempotency_key))

    def drain_slot(self, slot, *, idempotency_key):
        self.calls.append(("drain", slot.slot_no, slot.account_id, idempotency_key))

    def repin_slot(self, slot, *, idempotency_key):
        self.calls.append(("repin", slot.slot_no, slot.account_id, idempotency_key))

    def probe_slot(self, slot, *, idempotency_key):
        self.calls.append(("probe", slot.slot_no, slot.account_id, idempotency_key))
        return True

    def set_priority(self, slot, *, idempotency_key):
        self.calls.append(("priority", slot.slot_no, slot.account_id, idempotency_key))

    def set_assignment_metadata(self, desired, slot, *, idempotency_key):
        self.calls.append(("metadata", slot.slot_no, slot.account_id, idempotency_key))

    def set_schedulable(self, slot, enabled, *, idempotency_key):
        self.calls.append(("schedulable", slot.slot_no, slot.account_id, enabled, idempotency_key))
        key = (slot.account_id, slot.token_id)
        changed = self.schedulable.get(key, True) is not enabled
        self.schedulable[key] = enabled
        return changed


def bundle() -> ProviderBundle:
    plan = build_cost_plan(
        {1: "1"},
        retry_reserve_multiplier="0",
        bootstrap_retry_reserve="0",
        realized_cost_p95_multiplier=None,
    )
    slot = ready_slot(1, 1, "p1", 11, 21, priority=2)
    return ProviderBundle(
        model="gpt-5.6-sol",
        routing_generation=1,
        parent_routing_generation=None,
        cost_generation=1,
        parent_cost_generation=None,
        generation_source="bootstrap",
        source_hash="a" * 64,
        ordered_channel_ids=(1,),
        standby_channel_ids=(),
        desired_size=1,
        slots=(slot,),
        cost_plan=plan,
    )


def ready_slot(
    slot_no: int,
    channel_id: int,
    assignment_id: str,
    token_id: int,
    account_id: int,
    *,
    priority: int,
) -> ProviderSlot:
    return ProviderSlot(
        slot_no=slot_no,
        channel_id=channel_id,
        rank=slot_no,
        priority=priority,
        assignment_id=assignment_id,
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


def replacement_bundle(slots: tuple[ProviderSlot, ...]) -> ProviderBundle:
    plan = build_cost_plan(
        {
            slot.slot_no: slot.desired_cost_multiplier
            for slot in slots
            if slot.slot_state is SlotState.READY
        },
        retry_reserve_multiplier="0",
        bootstrap_retry_reserve="0",
        realized_cost_p95_multiplier=None,
    )
    return ProviderBundle(
        model="gpt-5.6-sol",
        routing_generation=2,
        parent_routing_generation=1,
        cost_generation=2,
        parent_cost_generation=1,
        generation_source="replacement",
        source_hash="b" * 64,
        ordered_channel_ids=tuple(slot.channel_id for slot in slots if slot.channel_id is not None),
        standby_channel_ids=(),
        desired_size=len(slots),
        slots=slots,
        cost_plan=plan,
    )


def bootstrap_bundle(slots: tuple[ProviderSlot, ...]) -> ProviderBundle:
    desired = replacement_bundle(slots)
    return replace(
        desired,
        routing_generation=1,
        parent_routing_generation=None,
        cost_generation=1,
        parent_cost_generation=None,
        generation_source="bootstrap",
        source_hash="a" * 64,
    )


def test_reconciler_orders_saga_and_activates_bundle() -> None:
    remote = Remote()
    store = InMemoryProviderPoolStore()
    report = ProviderPoolReconciler(store, remote).reconcile(bundle())

    assert report.activated is True
    assert report.diff_kind is PoolDiffKind.MEMBERSHIP_AND_COST
    assert [call[0] for call in remote.calls] == [
        "schedulable",
        "drain",
        "floor",
        "cost",
        "repin",
        "probe",
        "priority",
        "metadata",
        "schedulable",
    ]


def test_strict_remote_readback_confirms_every_ready_slot_before_activation() -> None:
    remote = ReadbackRemote()
    store = InMemoryProviderPoolStore()

    report = ProviderPoolReconciler(
        store,
        remote,
        require_remote_readback=True,
    ).reconcile(bundle())

    assert report.activated is True
    assert remote.calls[-1] == ("readback", 1)
    assert store.active(bundle().model) is not None


@pytest.mark.parametrize(
    ("activation_probe_required", "expected_readback_count"),
    ((False, 1), (True, 2)),
)
def test_zero_probe_skips_only_the_post_activation_full_readback(
    activation_probe_required: bool,
    expected_readback_count: int,
) -> None:
    remote = ReadbackRemote()

    report = ProviderPoolReconciler(
        InMemoryProviderPoolStore(),
        remote,
        require_remote_readback=True,
        activation_probe_required=activation_probe_required,
    ).reconcile(bundle())

    assert report.activated is True
    assert [call[0] for call in remote.calls].count("readback") == expected_readback_count


def test_pre_activation_readback_mismatch_keeps_generation_for_in_place_retry() -> None:
    class WrongPriorityRemote(ReadbackRemote):
        def readback(self, desired, *, deferred_slot_nos=frozenset()):
            observed = super().readback(desired)
            slot = observed.slots[1]
            return replace(
                observed,
                slots={1: replace(slot, priority=(slot.priority or 0) + 1)},
            )

    with pytest.raises(ProviderReadbackRetryRequired) as raised:
        ProviderPoolReconciler(
            InMemoryProviderPoolStore(),
            WrongPriorityRemote(),
            require_remote_readback=True,
        ).reconcile(bundle())

    diagnostics = raised.value.diagnostics
    assert diagnostics["cause_code"] == "provider_reconcile_slot_readback_mismatch"
    assert diagnostics["slot_no"] == 1
    assert diagnostics["mismatch_fields"] == ["priority"]
    assert diagnostics["expected"]["priority"] == 2
    assert diagnostics["observed"]["priority"] == 3


def test_pre_activation_readback_repairs_all_drifted_costs_in_one_round() -> None:
    class MultiCostDriftRemote(ReadbackRemote):
        def __init__(self):
            super().__init__()
            self.drifted_slot_nos: set[int] = set()
            self.repair_phase = False
            self.repaired_slot_nos: list[int] = []

        def readback(self, desired, *, deferred_slot_nos=frozenset()):
            observed = super().readback(desired)
            if not self.drifted_slot_nos:
                return observed
            self.repair_phase = True
            return replace(
                observed,
                slots={
                    slot_no: (
                        replace(slot, rate_multiplier=Decimal("9"))
                        if slot_no in self.drifted_slot_nos
                        else slot
                    )
                    for slot_no, slot in observed.slots.items()
                },
            )

        def set_slot_cost(self, slot, multiplier, *, idempotency_key):
            result = super().set_slot_cost(
                slot,
                multiplier,
                idempotency_key=idempotency_key,
            )
            if self.repair_phase and slot.slot_no in self.drifted_slot_nos:
                self.drifted_slot_nos.remove(slot.slot_no)
                self.repaired_slot_nos.append(slot.slot_no)
            return result

    parent = bootstrap_bundle(
        (
            ready_slot(1, 101, "parent-1", 11, 21, priority=2),
            ready_slot(2, 102, "parent-2", 12, 22, priority=3),
        )
    )
    candidate = replacement_bundle(
        (
            replace(
                parent.slots[0],
                desired_cost_multiplier=Decimal("1.1"),
                observed_cost_multiplier=Decimal("1.1"),
            ),
            replace(
                parent.slots[1],
                desired_cost_multiplier=Decimal("1.2"),
                observed_cost_multiplier=Decimal("1.2"),
            ),
        )
    )
    store = InMemoryProviderPoolStore()
    remote = MultiCostDriftRemote()
    reconciler = ProviderPoolReconciler(
        store,
        remote,
        require_remote_readback=True,
        activation_probe_required=False,
    )
    reconciler.reconcile(parent)
    remote.drifted_slot_nos = {1, 2}

    with pytest.raises(ProviderReadbackRetryRequired) as raised:
        reconciler.reconcile(
            candidate,
            current_routing=parent.ordered_channel_ids,
            current_slots={slot.slot_no: slot for slot in parent.slots},
            current_cost_input_hash=parent.effective_cost_input_hash,
        )

    assert raised.value.diagnostics["mismatch_slot_nos"] == [1, 2]
    assert remote.repaired_slot_nos == [1, 2]

    report = reconciler.reconcile(
        replace(
            candidate,
            generation_state=GenerationState.APPLYING,
            pricing_state=PricingState.APPLYING,
        ),
        current_routing=parent.ordered_channel_ids,
        current_slots={slot.slot_no: slot for slot in parent.slots},
        current_cost_input_hash=parent.effective_cost_input_hash,
    )
    assert report.activated is True


def test_pre_activation_readback_repairs_all_assignment_metadata_in_one_round() -> None:
    class MultiMetadataDriftRemote(ReadbackRemote):
        def __init__(self):
            super().__init__()
            self.drifted_slot_nos: set[int] = set()
            self.repair_phase = False
            self.repaired_slot_nos: list[int] = []

        def readback(self, desired, *, deferred_slot_nos=frozenset()):
            observed = super().readback(desired)
            if not self.drifted_slot_nos:
                return observed
            self.repair_phase = True
            slots = dict(observed.slots)
            for slot_no in self.drifted_slot_nos:
                slot = slots[slot_no]
                metadata = dict(slot.assignment_metadata)
                metadata["a6_provider_routing_generation"] = desired.routing_generation - 1
                slots[slot_no] = replace(slot, assignment_metadata=metadata)
            return replace(observed, slots=slots)

        def set_assignment_metadata(self, desired, slot, *, idempotency_key):
            result = super().set_assignment_metadata(
                desired,
                slot,
                idempotency_key=idempotency_key,
            )
            if self.repair_phase and slot.slot_no in self.drifted_slot_nos:
                self.drifted_slot_nos.remove(slot.slot_no)
                self.repaired_slot_nos.append(slot.slot_no)
            return result

    parent = bootstrap_bundle(
        (
            ready_slot(1, 101, "parent-1", 11, 21, priority=2),
            ready_slot(2, 102, "parent-2", 12, 22, priority=3),
        )
    )
    candidate = replacement_bundle(
        (
            replace(
                parent.slots[0],
                desired_cost_multiplier=Decimal("1.1"),
                observed_cost_multiplier=Decimal("1.1"),
            ),
            replace(
                parent.slots[1],
                desired_cost_multiplier=Decimal("1.2"),
                observed_cost_multiplier=Decimal("1.2"),
            ),
        )
    )
    store = InMemoryProviderPoolStore()
    remote = MultiMetadataDriftRemote()
    reconciler = ProviderPoolReconciler(
        store,
        remote,
        require_remote_readback=True,
        activation_probe_required=False,
    )
    reconciler.reconcile(parent)
    remote.drifted_slot_nos = {1, 2}

    with pytest.raises(ProviderReadbackRetryRequired) as raised:
        reconciler.reconcile(
            candidate,
            current_routing=parent.ordered_channel_ids,
            current_slots={slot.slot_no: slot for slot in parent.slots},
            current_cost_input_hash=parent.effective_cost_input_hash,
        )

    assert raised.value.diagnostics["cause_code"] == (
        "provider_reconcile_assignment_metadata_readback_mismatch"
    )
    assert raised.value.diagnostics["mismatch_slot_nos"] == [1, 2]
    assert remote.repaired_slot_nos == [1, 2]

    report = reconciler.reconcile(
        replace(
            candidate,
            generation_state=GenerationState.APPLYING,
            pricing_state=PricingState.APPLYING,
        ),
        current_routing=parent.ordered_channel_ids,
        current_slots={slot.slot_no: slot for slot in parent.slots},
        current_cost_input_hash=parent.effective_cost_input_hash,
    )
    assert report.activated is True


def test_pre_activation_readback_repairs_drifted_unchanged_pin_without_probe() -> None:
    class DriftRepairRemote(ReadbackRemote):
        def __init__(self):
            super().__init__()
            self.drifted = True
            self.repin_channels = []

        def readback(self, desired, *, deferred_slot_nos=frozenset()):
            observed = super().readback(desired)
            if not self.drifted:
                return observed
            slot = observed.slots[1]
            return replace(
                observed,
                slots={
                    **observed.slots,
                    1: replace(
                        slot,
                        token_id=None,
                        channel_id=999,
                        pin_binding_id=None,
                        pin_confirmed=False,
                    ),
                },
            )

        def repin_slot(self, slot, *, idempotency_key):
            self.repin_channels.append(slot.channel_id)
            if slot.slot_no == 1:
                self.drifted = False
            return super().repin_slot(slot, idempotency_key=idempotency_key)

        def probe_slot(self, *_args, **_kwargs):
            raise AssertionError("readback repair must not send a Probe")

    parent = bootstrap_bundle(
        (
            ready_slot(1, 101, "parent-1", 11, 21, priority=2),
            ready_slot(2, 102, "parent-2", 12, 22, priority=3),
        )
    )
    candidate = replacement_bundle(
        (
            parent.slots[0],
            ready_slot(2, 202, "candidate-2", 32, 42, priority=3),
        )
    )
    store = InMemoryProviderPoolStore()
    remote = DriftRepairRemote()
    reconciler = ProviderPoolReconciler(
        store,
        remote,
        require_remote_readback=True,
        activation_probe_required=False,
    )
    remote.drifted = False
    reconciler.reconcile(parent)
    remote.drifted = True
    remote.repin_channels.clear()

    with pytest.raises(ProviderReadbackRetryRequired) as raised:
        reconciler.reconcile(
            candidate,
            current_routing=parent.ordered_channel_ids,
            current_slots={slot.slot_no: slot for slot in parent.slots},
            current_cost_input_hash=parent.effective_cost_input_hash,
        )

    assert raised.value.diagnostics["slot_no"] == 1
    assert remote.repin_channels == [202, 101]

    report = reconciler.reconcile(
        replace(
            candidate,
            generation_state=GenerationState.APPLYING,
            pricing_state=PricingState.APPLYING,
        ),
        current_routing=parent.ordered_channel_ids,
        current_slots={slot.slot_no: slot for slot in parent.slots},
        current_cost_input_hash=parent.effective_cost_input_hash,
    )
    assert report.activated is True


def test_readback_pin_repair_channel_error_restores_parent_and_requires_replan() -> None:
    class FailedRepairRemote(ReadbackRemote):
        def __init__(self):
            super().__init__()
            self.failed_generation = None
            self.repin_channels = []
            self.channel_attempts = {}

        def repin_slot(self, slot, *, idempotency_key):
            self.repin_channels.append(slot.channel_id)
            attempts = self.channel_attempts.get(slot.channel_id, 0) + 1
            self.channel_attempts[slot.channel_id] = attempts
            if slot.channel_id == 202 and attempts >= 2:
                raise A6MutationError(
                    "marketplace_pin_failed",
                    operation="marketplace_pin",
                    status_code=400,
                )
            return super().repin_slot(slot, idempotency_key=idempotency_key)

        def readback(self, desired, *, deferred_slot_nos=frozenset()):
            observed = super().readback(desired)
            if desired.routing_generation != self.failed_generation:
                return observed
            slot = observed.slots[2]
            return replace(
                observed,
                slots={
                    **observed.slots,
                    2: replace(
                        slot,
                        token_id=None,
                        channel_id=None,
                        pin_binding_id=None,
                        pin_confirmed=False,
                    ),
                },
            )

    parent = bootstrap_bundle(
        (
            ready_slot(1, 101, "parent-1", 11, 21, priority=2),
            ready_slot(2, 102, "parent-2", 12, 22, priority=3),
        )
    )
    candidate = replacement_bundle(
        (
            parent.slots[0],
            ready_slot(2, 202, "candidate-2", 32, 42, priority=3),
        )
    )
    store = InMemoryProviderPoolStore()
    remote = FailedRepairRemote()
    ProviderPoolReconciler(
        store,
        remote,
        require_remote_readback=True,
        activation_probe_required=False,
    ).reconcile(parent)
    remote.failed_generation = candidate.routing_generation
    remote.repin_channels.clear()

    with pytest.raises(ReplanRequired) as raised:
        ProviderPoolReconciler(
            store,
            remote,
            require_remote_readback=True,
            activation_probe_required=False,
        ).reconcile(
            candidate,
            current_routing=parent.ordered_channel_ids,
            current_slots={slot.slot_no: slot for slot in parent.slots},
            current_cost_input_hash=parent.effective_cost_input_hash,
        )

    assert raised.value.replan_scope == "channel"
    assert raised.value.slot_no == 2
    assert raised.value.channel_id == 202
    assert raised.value.probe_observation["http_status"] == 400
    assert raised.value.probe_observation["provider_error_code"] is None
    assert (
        raised.value.probe_observation["document"]["classification_source"]
        == "nontransient_pin_http_status"
    )
    assert remote.channel_attempts[202] == 2
    assert 102 in remote.repin_channels
    active = store.active(parent.model)
    assert active is not None
    assert active.routing_generation == parent.routing_generation


def test_pre_activation_transport_read_failure_does_not_restore_parent() -> None:
    class TransientReadRemote(ReadbackRemote):
        fail_generation = None

        def readback(self, desired, *, deferred_slot_nos=frozenset()):
            if desired.routing_generation == self.fail_generation:
                self.calls.append(("readback_transport_failed", desired.routing_generation))
                raise RuntimeError("a6_transport_failed")
            return super().readback(desired)

    parent = bootstrap_bundle(
        (
            ready_slot(1, 101, "parent-1", 11, 21, priority=2),
            ready_slot(2, 102, "parent-2", 12, 22, priority=3),
        )
    )
    candidate = replacement_bundle(
        (
            ready_slot(1, 201, "candidate-1", 31, 41, priority=2),
            ready_slot(2, 202, "candidate-2", 32, 42, priority=3),
        )
    )
    store = InMemoryProviderPoolStore()
    remote = TransientReadRemote()
    ProviderPoolReconciler(
        store,
        remote,
        require_remote_readback=True,
    ).reconcile(parent)
    remote.calls.clear()
    remote.fail_generation = candidate.routing_generation

    with pytest.raises(ProviderReadbackRetryRequired, match="a6_transport_failed"):
        ProviderPoolReconciler(
            store,
            remote,
            require_remote_readback=True,
        ).reconcile(
            candidate,
            current_routing=parent.ordered_channel_ids,
            current_slots={slot.slot_no: slot for slot in parent.slots},
            current_cost_input_hash=parent.effective_cost_input_hash,
        )

    assert remote.calls[-1] == ("readback_transport_failed", 2)
    assert [call[0] for call in remote.calls].count("repin") == 2
    active = store.active(parent.model)
    assert active is not None
    assert active.routing_generation == parent.routing_generation


def test_pre_activation_accepts_fail_closed_unchanged_slot_then_reenables_it() -> None:
    parent = bootstrap_bundle(
        (
            ready_slot(1, 101, "parent-1", 11, 21, priority=2),
            ready_slot(2, 102, "parent-2", 12, 22, priority=3),
        )
    )
    candidate = replacement_bundle(
        (
            parent.slots[0],
            ready_slot(2, 202, "candidate-2", 32, 42, priority=3),
        )
    )
    store = InMemoryProviderPoolStore()
    remote = ReadbackRemote()
    ProviderPoolReconciler(
        store,
        remote,
        require_remote_readback=True,
    ).reconcile(parent)
    remote.schedulable[(parent.slots[0].account_id, parent.slots[0].token_id)] = False

    report = ProviderPoolReconciler(
        store,
        remote,
        require_remote_readback=True,
    ).reconcile(
        candidate,
        current_routing=parent.ordered_channel_ids,
        current_slots={slot.slot_no: slot for slot in parent.slots},
        current_cost_input_hash=parent.effective_cost_input_hash,
    )

    assert report.activated is True
    assert all(remote.read_slot_schedulable(slot) is True for slot in candidate.ready_slots)


def test_assignment_metadata_readback_uses_strict_types_before_activation() -> None:
    class WrongMetadataRemote(ReadbackRemote):
        def readback(self, desired, *, deferred_slot_nos=frozenset()):
            observed = super().readback(desired)
            slot = observed.slots[1]
            metadata = dict(slot.assignment_metadata)
            metadata["a6_provider_routing_generation"] = True
            return replace(
                observed,
                slots={1: replace(slot, assignment_metadata=metadata)},
            )

    store = InMemoryProviderPoolStore()

    with pytest.raises(
        ProviderPoolReconcileError,
        match="provider_reconcile_assignment_metadata_readback_mismatch",
    ):
        ProviderPoolReconciler(
            store,
            WrongMetadataRemote(),
            require_remote_readback=True,
        ).reconcile(bundle())

    assert store.active(bundle().model) is None


def test_strict_remote_readback_capability_is_checked_before_planning() -> None:
    store = SagaStore()

    with pytest.raises(
        ProviderPoolReconcileError,
        match="provider_reconcile_remote_readback_required",
    ):
        ProviderPoolReconciler(
            store,
            Remote(),
            require_remote_readback=True,
        ).reconcile(bundle())

    assert store.calls == []


def test_request_plane_paid_send_capability_fails_before_any_write() -> None:
    store = SagaStore()
    remote = ReadbackRemote()

    with pytest.raises(
        ProviderPoolReconcileError,
        match="provider_request_plane_capability_mismatch",
    ):
        ProviderPoolReconciler(
            store,
            remote,
            request_plane_preflight=lambda _model: request_plane_capability(
                paid_send_max_attempts=2
            ),
            require_request_plane_preflight=True,
            require_remote_readback=True,
        ).reconcile(bundle())

    assert store.calls == []
    assert remote.calls == []


def test_platform_backoff_preflight_blocks_before_plan_and_remote_write() -> None:
    class BackedOffRemote(ReadbackRemote):
        def platform_backoff(self, model):
            assert model == bundle().model
            return ProviderProbeResult(
                confirmed=False,
                protocol="chat_completions",
                http_status=None,
                error_code="provider_probe_backoff_active",
                failure_owner="platform",
                replan_scope="platform",
                document={
                    "request_sent": False,
                    "retry_after_ms": 42_000,
                    "source_action_id": 123,
                },
            )

    store = SagaStore()
    remote = BackedOffRemote()

    with pytest.raises(ProviderPlatformBackoffActive) as raised:
        ProviderPoolReconciler(store, remote).reconcile(bundle())

    assert raised.value.observation["document"]["retry_after_ms"] == 42_000
    assert store.calls == []
    assert remote.calls == []


def test_billing_readback_below_transition_floor_blocks_activation() -> None:
    remote = ReadbackRemote(billing_floor=Decimal("0"))
    store = InMemoryProviderPoolStore()

    with pytest.raises(
        ProviderPoolReconcileError,
        match="provider_reconcile_billing_readback_mismatch",
    ):
        ProviderPoolReconciler(
            store,
            remote,
            require_remote_readback=True,
        ).reconcile(bundle())

    assert store.active(bundle().model) is None


def test_lower_billing_floor_is_finalized_only_after_bundle_activation() -> None:
    events = []
    desired_cost = Decimal("0.5")
    cost_plan = build_cost_plan(
        {1: desired_cost},
        retry_reserve_multiplier="0",
        bootstrap_retry_reserve="0",
        realized_cost_p95_multiplier=None,
        previous_billing_floor=Decimal("2"),
    )
    original = bundle()
    desired = replace(
        original,
        slots=(
            replace(
                original.slots[0],
                desired_cost_multiplier=desired_cost,
                observed_cost_multiplier=desired_cost,
            ),
        ),
        cost_plan=cost_plan,
    )

    class OrderedStore(InMemoryProviderPoolStore):
        def activate(self, value, *, deferred_slot_nos=()):
            events.append("activate")
            return super().activate(value)

        def record_billing_floor(self, model, **kwargs):
            events.append(("record-floor", kwargs["observed_billing_floor"]))
            return super().record_billing_floor(model, **kwargs)

    class OrderedRemote(ReadbackRemote):
        def stage_billing_floor(self, model, multiplier, *, idempotency_key):
            events.append(("stage-floor", multiplier))
            return super().stage_billing_floor(
                model,
                multiplier,
                idempotency_key=idempotency_key,
            )

        def finalize_billing_floor(self, model, multiplier, *, idempotency_key):
            events.append(("final-floor", multiplier))
            return super().finalize_billing_floor(
                model,
                multiplier,
                idempotency_key=idempotency_key,
            )

    report = ProviderPoolReconciler(
        OrderedStore(),
        OrderedRemote(),
        require_remote_readback=True,
    ).reconcile(desired)

    assert report.actions[-1] == "finalize_billing_floor"
    assert events.index("activate") < events.index(
        ("final-floor", cost_plan.billing_floor_multiplier)
    )
    assert events[-1] == ("record-floor", cost_plan.billing_floor_multiplier)


def test_crash_after_final_billing_write_resumes_from_readback_without_rewrite() -> None:
    cost_plan = build_cost_plan(
        {1: Decimal("0.5")},
        retry_reserve_multiplier="0",
        bootstrap_retry_reserve="0",
        realized_cost_p95_multiplier=None,
        previous_billing_floor=Decimal("2"),
    )
    original = bundle()
    desired = replace(
        original,
        slots=(
            replace(
                original.slots[0],
                desired_cost_multiplier=Decimal("0.5"),
                observed_cost_multiplier=Decimal("0.5"),
            ),
        ),
        cost_plan=cost_plan,
    )
    ledger = IdempotencyLedger()

    class CrashAfterWriteRemote(ReadbackRemote):
        def __init__(self):
            super().__init__()
            self.final_write_count = 0
            self.crash = True

        def finalize_billing_floor(self, model, multiplier, *, idempotency_key):
            self.final_write_count += 1
            super().finalize_billing_floor(
                model,
                multiplier,
                idempotency_key=idempotency_key,
            )
            if self.crash:
                self.crash = False
                raise KeyboardInterrupt("fixture-crash-after-remote-write")
            return True

    store = InMemoryProviderPoolStore()
    remote = CrashAfterWriteRemote()
    reconciler = ProviderPoolReconciler(
        store,
        remote,
        ledger=ledger,
        require_remote_readback=True,
    )

    with pytest.raises(KeyboardInterrupt, match="fixture-crash"):
        reconciler.reconcile(desired)

    assert store.active(desired.model) is not None
    assert remote.final_write_count == 1
    finalize_key = ledger.key(
        desired,
        "finalize_billing_floor",
        (desired.model, format(cost_plan.billing_floor_multiplier, "f")),
    )
    assert finalize_key in ledger.in_progress

    with pytest.raises(
        ProviderPoolReconcileError,
        match="provider_reconcile_action_in_progress",
    ):
        reconciler.finalize_active_billing(
            desired,
            routing_generation=desired.routing_generation,
            cost_generation=desired.cost_generation,
        )

    ledger.in_progress.remove(finalize_key)
    actions = reconciler.finalize_active_billing(
        desired,
        routing_generation=desired.routing_generation,
        cost_generation=desired.cost_generation,
    )

    assert actions == ()
    assert remote.final_write_count == 1
    assert finalize_key in ledger.completed


def test_model_lease_wraps_the_complete_saga() -> None:
    events = []

    class LeaseContext:
        def __enter__(self):
            events.append("lease-enter")

        def __exit__(self, *_args):
            events.append("lease-exit")

    class Lease:
        def hold(self, model):
            events.append(("lease", model))
            return LeaseContext()

    class LeaseAwareRemote(ReadbackRemote):
        def stage_billing_floor(self, model, multiplier, *, idempotency_key):
            assert events[-1] == "lease-enter"
            super().stage_billing_floor(
                model,
                multiplier,
                idempotency_key=idempotency_key,
            )

    report = ProviderPoolReconciler(
        InMemoryProviderPoolStore(),
        LeaseAwareRemote(),
        model_lease=Lease(),
        require_remote_readback=True,
    ).reconcile(bundle())

    assert report.activated is True
    assert events == [("lease", "gpt-5.6-sol"), "lease-enter", "lease-exit"]


def test_postgres_model_lease_uses_session_lock_and_releases_it() -> None:
    class Cursor:
        def __init__(self, connection):
            self.connection = connection
            self.result = None

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, query, params):
            self.connection.calls.append((" ".join(query.split()), params))
            self.result = (True,)

        def fetchone(self):
            return self.result

    class Connection:
        def __init__(self):
            self.calls = []
            self.commits = 0
            self.closed = False

        def cursor(self):
            return Cursor(self)

        def commit(self):
            self.commits += 1

        def close(self):
            self.closed = True

    connection = Connection()
    with PostgresProviderModelLease(lambda: connection).hold("gpt-5.6-sol"):
        assert connection.closed is False

    assert [call[0].split("(")[0].strip() for call in connection.calls] == [
        "SELECT pg_try_advisory_lock",
        "SELECT pg_advisory_unlock",
    ]
    assert {call[1][0] for call in connection.calls} == {0x41365051}
    assert 0x41365050 not in {call[1][0] for call in connection.calls}
    assert connection.commits == 2
    assert connection.closed is True


def test_activation_barrier_covers_database_activation_and_dispatch_publication() -> None:
    events = []
    publication = dispatch_publication()

    class BarrierContext:
        def __enter__(self):
            events.append("barrier-enter")

        def __exit__(self, *_args):
            events.append("barrier-exit")

    class Barrier:
        def hold(self, model):
            assert model == "gpt-5.6-sol"
            return BarrierContext()

    class Store(InMemoryProviderPoolStore):
        def activate(self, desired, *, deferred_slot_nos=()):
            assert events[-1] == "barrier-enter"
            events.append("activate")
            return super().activate(desired)

    store = Store()

    def publish(model, routing_generation, cost_generation):
        assert events[-1] == "activate"
        assert store.active(model) is not None
        assert (routing_generation, cost_generation) == (1, 1)
        events.append("publish")
        return publication

    report = ProviderPoolReconciler(
        store,
        ReadbackRemote(),
        activation_barrier=Barrier(),
        activation_publisher=publish,
        activation_compensator=lambda *_args: pytest.fail(
            "successful activation must not compensate"
        ),
        require_remote_readback=True,
    ).reconcile(bundle())

    assert report.dispatch_publication is publication
    assert events == ["barrier-enter", "activate", "publish", "barrier-exit"]


def test_activation_success_order_keeps_remote_closed_until_dispatch_readback() -> None:
    events = []

    class BarrierContext:
        def __enter__(self):
            return None

        def __exit__(self, *_args):
            return False

    class Barrier:
        def hold(self, _model):
            return BarrierContext()

    class OrderedStore(InMemoryProviderPoolStore):
        def activate(self, desired, *, deferred_slot_nos=()):
            events.append("db-active")
            return super().activate(desired)

    class OrderedRemote(ReadbackRemote):
        def set_schedulable(self, slot, enabled, *, idempotency_key):
            if enabled:
                events.append("remote-enable")
            return super().set_schedulable(
                slot,
                enabled,
                idempotency_key=idempotency_key,
            )

        def readback(self, desired, *, deferred_slot_nos=frozenset()):
            result = super().readback(desired)
            states = {value.schedulable for value in result.slots.values()}
            events.append(
                "remote-enable/readback-true"
                if states == {True}
                else "remote-config/readback-false"
            )
            return result

    def preflight(model):
        events.append("preflight")
        return request_plane_capability(model)

    def publish(_model, _routing, _cost):
        events.extend(("redis-publish", "redis-readback"))
        return dispatch_publication()

    report = ProviderPoolReconciler(
        OrderedStore(),
        OrderedRemote(),
        activation_barrier=Barrier(),
        activation_publisher=publish,
        activation_compensator=lambda *_args: pytest.fail(
            "successful activation must not compensate"
        ),
        request_plane_preflight=preflight,
        require_request_plane_preflight=True,
        require_remote_readback=True,
    ).reconcile(bundle())

    assert report.activated is True
    assert events == [
        "preflight",
        "remote-config/readback-false",
        "db-active",
        "redis-publish",
        "redis-readback",
        "remote-enable",
        "remote-enable/readback-true",
    ]


def test_database_activation_failure_never_leaves_remote_enabled() -> None:
    class FailingStore(InMemoryProviderPoolStore):
        def activate(self, _desired, *, deferred_slot_nos=()):
            raise RuntimeError("fixture-db-activate-failed")

    desired = bundle()
    remote = ReadbackRemote()
    ledger = IdempotencyLedger()
    old_claim = ledger.claim(desired, "set_schedulable", (1, True))
    ledger.complete(old_claim, observed_after={"verified": True}, elapsed_ms=0)
    store = FailingStore()

    with pytest.raises(RuntimeError, match="fixture-db-activate-failed"):
        ProviderPoolReconciler(
            store,
            remote,
            ledger=ledger,
            require_remote_readback=True,
        ).reconcile(desired)

    assert store.active(desired.model) is None
    assert remote.read_slot_schedulable(desired.slots[0]) is False
    assert any(
        ":hold_schedulable_disabled:" in key for key in ledger.completed
    )


@pytest.mark.parametrize(
    "publish_error",
    (
        RuntimeError("fixture-redis-publish-failed"),
        ProviderPoolReconcileError("provider_dispatch_redis_readback_mismatch"),
    ),
)
def test_dispatch_failure_restores_database_redis_and_remote(
    publish_error,
) -> None:
    class BarrierContext:
        def __enter__(self):
            return None

        def __exit__(self, *_args):
            return False

    class Barrier:
        def hold(self, _model):
            return BarrierContext()

    desired = bundle()
    store = InMemoryProviderPoolStore()
    remote = ReadbackRemote()
    restored = []

    def publish(*_args):
        raise publish_error

    def compensate(model, routing, cost):
        restored.append((model, routing, cost))
        return dispatch_publication(
            routing_generation=0,
            cost_generation=0,
            state="absent",
        )

    with pytest.raises(type(publish_error), match=str(publish_error)):
        ProviderPoolReconciler(
            store,
            remote,
            activation_barrier=Barrier(),
            activation_publisher=publish,
            activation_compensator=compensate,
            require_remote_readback=True,
        ).reconcile(desired)

    assert store.active(desired.model) is None
    assert remote.read_slot_schedulable(desired.slots[0]) is False
    assert restored == [(desired.model, 1, 1)]


def test_partial_remote_enable_failure_disables_every_slot_and_rolls_back() -> None:
    class BarrierContext:
        def __enter__(self):
            return None

        def __exit__(self, *_args):
            return False

    class Barrier:
        def hold(self, _model):
            return BarrierContext()

    class FailingSecondEnableRemote(ReadbackRemote):
        def set_schedulable(self, slot, enabled, *, idempotency_key):
            if enabled and slot.slot_no == 2:
                self.calls.append(
                    ("schedulable", slot.slot_no, enabled, idempotency_key)
                )
                raise RuntimeError("fixture-second-enable-failed")
            return super().set_schedulable(
                slot,
                enabled,
                idempotency_key=idempotency_key,
            )

    slots = (
        ready_slot(1, 1, "p1", 11, 21, priority=2),
        ready_slot(2, 2, "p2", 12, 22, priority=3),
    )
    desired = bootstrap_bundle(slots)
    store = InMemoryProviderPoolStore()
    remote = FailingSecondEnableRemote()

    with pytest.raises(
        ProviderPoolReconcileError,
        match="provider_reconcile_enable_schedulable_after_dispatch_failed",
    ):
        ProviderPoolReconciler(
            store,
            remote,
            activation_barrier=Barrier(),
            activation_publisher=lambda *_args: dispatch_publication(),
            activation_compensator=lambda *_args: dispatch_publication(
                routing_generation=0,
                cost_generation=0,
                state="absent",
            ),
            require_remote_readback=True,
        ).reconcile(desired)

    assert store.active(desired.model) is None
    assert all(remote.read_slot_schedulable(slot) is False for slot in slots)


def test_postgres_activation_barrier_uses_matching_exclusive_namespace() -> None:
    class Cursor:
        def __init__(self, connection):
            self.connection = connection
            self.result = None

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, query, params):
            self.connection.calls.append((" ".join(query.split()), params))
            self.result = (True,)

        def fetchone(self):
            return self.result

    class Connection:
        def __init__(self):
            self.calls = []
            self.commits = 0
            self.closed = False

        def cursor(self):
            return Cursor(self)

        def commit(self):
            self.commits += 1

        def close(self):
            self.closed = True

    connection = Connection()
    with PostgresProviderActivationBarrier(lambda: connection).hold("gpt-5.6-sol"):
        assert connection.closed is False

    assert [call[0].split("(")[0].strip() for call in connection.calls] == [
        "SELECT pg_advisory_lock",
        "SELECT pg_advisory_unlock",
    ]
    assert {call[1][0] for call in connection.calls} == {0x41365052}
    assert connection.commits == 2
    assert connection.closed is True


def test_reconciler_noop_skips_plan_and_every_remote_write() -> None:
    class RejectingStore:
        def __getattr__(self, name):
            raise AssertionError(f"unexpected store call: {name}")

    desired = bundle()
    remote = Remote()
    report = ProviderPoolReconciler(RejectingStore(), remote).reconcile(
        desired,
        current_routing=desired.ordered_channel_ids,
        current_assignments={slot.slot_no: slot.assignment_id for slot in desired.slots},
        current_cost_input_hash=desired.effective_cost_input_hash,
    )

    assert report.diff_kind is PoolDiffKind.NOOP
    assert report.actions == ()
    assert report.activated is False
    assert remote.calls == []


def test_membership_change_only_mutates_changed_slot_and_drains_old_assignment() -> None:
    old_a = ready_slot(1, 1, "p1", 11, 21, priority=2)
    unchanged_b = ready_slot(2, 2, "p2", 12, 22, priority=3)
    new_c = ready_slot(1, 3, "p3", 13, 23, priority=2)
    desired = replacement_bundle((new_c, unchanged_b))
    remote = SlotAwareRemote()
    ledger = IdempotencyLedger()

    report = ProviderPoolReconciler(SagaStore(), remote, ledger=ledger).reconcile(
        desired,
        current_routing=(1, 2),
        current_slots={1: old_a, 2: unchanged_b},
        current_cost_input_hash="a" * 64,
    )

    assert report.diff_kind is PoolDiffKind.MEMBERSHIP_AND_COST
    assert [call[0] for call in remote.calls] == [
        "schedulable",
        "schedulable",
        "drain",
        "drain",
        "floor",
        "cost",
        "repin",
        "probe",
        "priority",
        "metadata",
        "metadata",
        "schedulable",
    ]
    drain_calls = [call for call in remote.calls if call[0] == "drain"]
    assert [call[2] for call in drain_calls] == [21, 23]
    assert drain_calls[0][-1] != drain_calls[1][-1]
    assert {
        ledger.key(desired, "drain_slot", (1, 21, 11)),
        ledger.key(desired, "drain_slot", (1, 23, 13)),
    }.issubset(ledger.completed)
    assert report.actions.count("drain_slot") == 2
    assert next(call for call in remote.calls if call[0] == "repin")[2] == 23
    assert all(
        len(call) < 2 or call[1] != 2
        for call in remote.calls
        if call[0] not in {"metadata", "schedulable"}
    )
    assert [call[1] for call in remote.calls if call[0] == "metadata"] == [1, 2]
    assert [call[2] for call in remote.calls if call[0] == "schedulable"] == [
        21,
        23,
        23,
    ]
    assert remote.read_slot_schedulable(unchanged_b) is True


def test_membership_change_defers_changed_slot_enable_to_auto_pool() -> None:
    old_a = ready_slot(1, 1, "p1", 11, 21, priority=2)
    unchanged_b = ready_slot(2, 2, "p2", 12, 22, priority=3)
    new_c = ready_slot(1, 3, "p3", 13, 23, priority=2)
    parent = bootstrap_bundle((old_a, unchanged_b))
    desired = replacement_bundle((new_c, unchanged_b))
    store = InMemoryProviderPoolStore()
    ProviderPoolReconciler(store, Remote()).reconcile(parent)
    remote = ReadbackRemote(no_pin_slot_nos={1})
    published = []

    class Barrier:
        def hold(self, _model):
            return nullcontext()

    def publish(model, routing_generation, cost_generation):
        active = store.active(model)
        assert active is not None
        assert active.routing_generation == routing_generation
        assert active.cost_generation == cost_generation
        assert [slot.slot_state for slot in active.slots] == [
            SlotState.REPAIRING,
            SlotState.READY,
        ]
        published.append(active.dispatch_hash)
        return dispatch_publication(routing_generation=2, cost_generation=2)

    report = ProviderPoolReconciler(
        store,
        remote,
        activation_barrier=Barrier(),
        activation_publisher=publish,
        activation_compensator=lambda *_args: pytest.fail(
            "successful deferred activation must not compensate"
        ),
        require_remote_readback=True,
        defer_membership_to_auto_pool=True,
    ).reconcile(
        desired,
        current_routing=(1, 2),
        current_slots={1: old_a, 2: unchanged_b},
        current_cost_input_hash=parent.effective_cost_input_hash,
    )

    active = store.active(desired.model)
    assert report.activated is True
    assert "defer_membership_to_auto_pool" in report.actions
    assert published
    assert active is not None
    assert active.slots[0].slot_state is SlotState.REPAIRING
    assert active.slots[0].desired_schedulable is False
    assert active.slots[0].observed_schedulable is False
    assert active.slots[0].confirmed_pin_binding_id is None
    assert active.slots[0].pin_confirmed is False
    assert active.slots[1].slot_state is SlotState.READY
    assert "repin_slot" not in [call[0] for call in remote.calls]
    assert "set_assignment_metadata" not in [
        call[0] for call in remote.calls if call[1] == new_c.slot_no
    ]
    assert remote.read_slot_schedulable(new_c) is False
    assert not any(
        call[0] == "schedulable"
        and call[1] == new_c.slot_no
        and call[2] is True
        for call in remote.calls
    )


def test_membership_change_drains_same_remote_identity_only_once() -> None:
    previous = ready_slot(1, 1, "p1", 11, 21, priority=2)
    desired_slot = replace(previous, channel_id=3)
    remote = SlotAwareRemote()
    ledger = IdempotencyLedger()

    report = ProviderPoolReconciler(SagaStore(), remote, ledger=ledger).reconcile(
        replacement_bundle((desired_slot,)),
        current_routing=(1,),
        current_slots={1: previous},
        current_cost_input_hash="a" * 64,
    )

    assert report.diff_kind is PoolDiffKind.MEMBERSHIP_AND_COST
    assert [call[0] for call in remote.calls].count("drain") == 1
    assert next(call for call in remote.calls if call[0] == "drain")[2] == 21
    drain_keys = {key for key in ledger.completed if ":drain_slot:" in key}
    assert drain_keys == {
        ledger.key(replacement_bundle((desired_slot,)), "drain_slot", (1, 21, 11))
    }
    assert [call[0] for call in remote.calls].index("drain") < [
        call[0] for call in remote.calls
    ].index("repin")


def test_membership_change_drains_cross_slot_identities_only_once() -> None:
    current_a = ready_slot(1, 1, "p1", 11, 21, priority=2)
    current_b = ready_slot(2, 2, "p2", 12, 22, priority=3)
    desired_a = replace(current_a, slot_no=2, rank=2, priority=3)
    desired_b = replace(current_b, slot_no=1, rank=1, priority=2)
    desired = replacement_bundle((desired_b, desired_a))
    remote = SlotAwareRemote()
    ledger = IdempotencyLedger()

    report = ProviderPoolReconciler(SagaStore(), remote, ledger=ledger).reconcile(
        desired,
        current_routing=(1, 2),
        current_slots={1: current_a, 2: current_b},
        current_cost_input_hash=desired.effective_cost_input_hash,
    )

    drain_calls = [call for call in remote.calls if call[0] == "drain"]
    assert report.diff_kind is PoolDiffKind.LIQUIDITY_REPLACEMENT
    assert [(call[1], call[2]) for call in drain_calls] == [(1, 21), (1, 22)]
    assert report.actions.count("drain_slot") == 2
    assert {key for key in ledger.completed if ":drain_slot:" in key} == {
        ledger.key(desired, "drain_slot", (1, 21, 11)),
        ledger.key(desired, "drain_slot", (1, 22, 12)),
    }


def test_bootstrap_drain_failure_blocks_price_and_pin_writes() -> None:
    class FailingDrainRemote(SlotAwareRemote):
        def drain_slot(self, slot, *, idempotency_key):
            self.calls.append(("drain", slot.slot_no, slot.account_id, idempotency_key))
            raise RuntimeError("fixture-drain-failed")

    remote = FailingDrainRemote()

    with pytest.raises(
        ProviderPoolReconcileError,
        match="provider_reconcile_drain_slot_failed",
    ):
        ProviderPoolReconciler(SagaStore(), remote).reconcile(bundle())

    assert [call[0] for call in remote.calls] == ["schedulable", "drain"]
    assert remote.read_slot_schedulable(bundle().slots[0]) is False


def test_bootstrap_drain_crash_resumes_from_readback_without_second_write() -> None:
    ledger = IdempotencyLedger()

    class CrashAfterDrainRemote(SlotAwareRemote):
        def __init__(self):
            super().__init__()
            self.drained_accounts: set[int] = set()
            self.drain_write_count = 0
            self.crash = True

        def drain_slot(self, slot, *, idempotency_key):
            self.calls.append(("drain", slot.slot_no, slot.account_id, idempotency_key))
            if slot.account_id in self.drained_accounts:
                return False
            self.drained_accounts.add(slot.account_id)
            self.drain_write_count += 1
            if self.crash:
                self.crash = False
                raise KeyboardInterrupt("fixture-crash-after-drain")
            return True

    desired = bundle()
    remote = CrashAfterDrainRemote()
    reconciler = ProviderPoolReconciler(
        SagaStore(),
        remote,
        ledger=ledger,
    )

    with pytest.raises(KeyboardInterrupt, match="fixture-crash-after-drain"):
        reconciler.reconcile(desired)

    drain_target = (1, 21, 11)
    stale_key = ledger.key(desired, "drain_slot", drain_target)
    assert stale_key in ledger.in_progress
    ledger.in_progress.remove(stale_key)

    report = reconciler.reconcile(desired)

    assert report.activated is True
    assert remote.drain_write_count == 1
    assert [call[0] for call in remote.calls].count("drain") == 2
    assert stale_key in ledger.completed


def test_assignment_metadata_crash_resumes_from_readback_without_second_write() -> None:
    ledger = IdempotencyLedger()

    class CrashAfterMetadataWriteRemote(SlotAwareRemote):
        def __init__(self):
            super().__init__()
            self.metadata = {}
            self.metadata_write_count = 0
            self.crash = True

        def set_assignment_metadata(self, desired, slot, *, idempotency_key):
            self.calls.append(("metadata", slot.slot_no, slot.account_id, idempotency_key))
            expected = assignment_metadata(desired, slot)
            if self.metadata == expected:
                return False
            self.metadata = expected
            self.metadata_write_count += 1
            if self.crash:
                self.crash = False
                raise KeyboardInterrupt("fixture-crash-after-assignment-metadata")
            return True

    desired = bundle()
    remote = CrashAfterMetadataWriteRemote()
    reconciler = ProviderPoolReconciler(SagaStore(), remote, ledger=ledger)

    with pytest.raises(KeyboardInterrupt, match="assignment-metadata"):
        reconciler.reconcile(desired)

    target = (1, 21, 11, 1)
    stale_key = ledger.key(desired, "set_assignment_metadata", target)
    assert stale_key in ledger.in_progress
    assert remote.metadata_write_count == 1
    ledger.in_progress.remove(stale_key)

    report = reconciler.reconcile(desired)

    assert report.activated is True
    assert remote.metadata_write_count == 1
    assert [call[0] for call in remote.calls].count("metadata") == 2
    assert stale_key in ledger.completed


def test_priority_reorder_uses_disjoint_temporary_interval_before_final_values() -> None:
    desired_a = ready_slot(1, 1, "p1", 11, 21, priority=2)
    desired_b = ready_slot(2, 2, "p2", 12, 22, priority=3)
    current_a = replace(desired_a, rank=2, priority=3)
    current_b = replace(desired_b, rank=1, priority=2)
    desired = replacement_bundle((desired_a, desired_b))

    class PriorityRemote(SlotAwareRemote):
        def set_priority(self, slot, *, idempotency_key):
            self.calls.append(("priority", slot.slot_no, slot.priority, idempotency_key))

    remote = PriorityRemote()
    report = ProviderPoolReconciler(SagaStore(), remote).reconcile(
        desired,
        current_routing=(2, 1),
        current_slots={1: current_a, 2: current_b},
        current_cost_input_hash=desired.effective_cost_input_hash,
    )

    assert report.diff_kind is PoolDiffKind.PRIORITY_AND_COST
    assert [call[1:3] for call in remote.calls if call[0] == "priority"] == [
        (1, 4),
        (2, 5),
        (1, 2),
        (2, 3),
    ]
    assert report.actions.count("stage_priority") == 2
    assert report.actions.count("set_priority") == 2


def test_priority_stage_resumes_from_remote_readback_without_duplicate_write() -> None:
    desired_a = ready_slot(1, 1, "p1", 11, 21, priority=2)
    desired_b = ready_slot(2, 2, "p2", 12, 22, priority=3)
    current_a = replace(desired_a, rank=2, priority=3)
    current_b = replace(desired_b, rank=1, priority=2)
    desired = replacement_bundle((desired_a, desired_b))
    ledger = IdempotencyLedger()

    class CrashPriorityRemote(SlotAwareRemote):
        def __init__(self):
            super().__init__()
            self.priorities = {21: 3, 22: 2}
            self.priority_writes = []
            self.crash = True

        def set_priority(self, slot, *, idempotency_key):
            if self.priorities[slot.account_id] == slot.priority:
                return False
            self.priorities[slot.account_id] = slot.priority
            self.priority_writes.append((slot.slot_no, slot.priority))
            if self.crash and (slot.slot_no, slot.priority) == (1, 4):
                self.crash = False
                raise KeyboardInterrupt("fixture-crash-after-priority-stage")
            return True

    remote = CrashPriorityRemote()
    reconciler = ProviderPoolReconciler(SagaStore(), remote, ledger=ledger)
    kwargs = {
        "current_routing": (2, 1),
        "current_slots": {1: current_a, 2: current_b},
        "current_cost_input_hash": desired.effective_cost_input_hash,
    }

    with pytest.raises(KeyboardInterrupt, match="priority-stage"):
        reconciler.reconcile(desired, **kwargs)

    stale_key = ledger.key(desired, "stage_priority", (1, 4))
    assert stale_key in ledger.in_progress
    ledger.in_progress.remove(stale_key)

    report = reconciler.reconcile(desired, **kwargs)

    assert report.activated is True
    assert remote.priority_writes == [(1, 4), (2, 5), (1, 2), (2, 3)]
    assert stale_key in ledger.completed


def test_ready_to_vacant_drains_old_assignment_without_repinning_placeholder() -> None:
    previous = ready_slot(1, 1, "p1", 11, 21, priority=2)
    vacant = ProviderSlot(
        slot_no=1,
        channel_id=None,
        rank=None,
        priority=None,
        assignment_id=None,
        enodot_binding_id=None,
        token_id=None,
        account_id=None,
        slot_state=SlotState.VACANT,
        funding_eligible=False,
        desired_schedulable=False,
    )
    remote = SlotAwareRemote()

    report = ProviderPoolReconciler(SagaStore(), remote).reconcile(
        replacement_bundle((vacant,)),
        current_routing=(1,),
        current_slots={1: previous},
        current_cost_input_hash="a" * 64,
    )

    assert report.diff_kind is PoolDiffKind.MEMBERSHIP_AND_COST
    assert [call[0] for call in remote.calls] == [
        "schedulable",
        "drain",
        "floor",
    ]
    assert remote.calls[1][2] == 21


def test_membership_apply_requires_current_slot_snapshot_for_existing_assignment() -> None:
    desired = replacement_bundle((ready_slot(1, 3, "p3", 13, 23, priority=2),))
    store = SagaStore()

    with pytest.raises(
        ProviderPoolReconcileError, match="provider_reconcile_current_slots_required"
    ):
        ProviderPoolReconciler(store, SlotAwareRemote()).reconcile(
            desired,
            current_routing=(3,),
            current_assignments={1: "p1"},
            current_cost_input_hash="a" * 64,
        )

    assert store.calls == []


def test_in_memory_ledger_blocks_parallel_claim_and_releases_failed_claim() -> None:
    ledger = IdempotencyLedger()
    first = ledger.claim(bundle(), "repin_slot", 1)
    second = ledger.claim(bundle(), "repin_slot", 1)

    assert first.disposition is ActionClaimDisposition.CLAIMED
    assert second.disposition is ActionClaimDisposition.BUSY

    ledger.fail(first, error_class="provider_reconcile_repin_slot_failed", elapsed_ms=1)
    retry = ledger.claim(bundle(), "repin_slot", 1)

    assert retry.disposition is ActionClaimDisposition.CLAIMED
    ledger.complete(retry, observed_after={"verified": True}, elapsed_ms=2)
    assert ledger.claim(bundle(), "repin_slot", 1).disposition is (ActionClaimDisposition.COMPLETED)


def test_reconciler_records_probe_failure_and_allows_retry() -> None:
    class FailingProbeRemote(Remote):
        def probe_slot(self, slot, *, idempotency_key):
            self.calls.append(("probe", slot.slot_no, idempotency_key))
            return False

    ledger = IdempotencyLedger()
    remote = FailingProbeRemote()

    with pytest.raises(
        ReplanRequired,
        match="provider_reconcile_probe_failed",
    ) as raised:
        ProviderPoolReconciler(InMemoryProviderPoolStore(), remote, ledger=ledger).reconcile(
            bundle()
        )

    assert raised.value.slot_no == 1
    assert raised.value.channel_id == 1
    probe_key = ledger.key(bundle(), "probe_slot", 1)
    assert probe_key not in ledger.completed
    assert probe_key not in ledger.in_progress


def test_reconciler_preserves_liquidity_probe_scope_and_identity() -> None:
    class QuotaProbeRemote(Remote):
        def probe_slot(self, slot, *, idempotency_key):
            return ProviderProbeResult(
                confirmed=False,
                protocol="responses",
                http_status=403,
                error_code="insufficient_quota",
                failure_owner="liquidity",
                replan_scope="liquidity",
                document={"error": {"code": "insufficient_quota"}},
            )

    with pytest.raises(ReplanRequired) as raised:
        ProviderPoolReconciler(
            InMemoryProviderPoolStore(), QuotaProbeRemote()
        ).reconcile(bundle())

    assert raised.value.replan_scope == "liquidity"
    assert raised.value.slot_no == 1
    assert raised.value.channel_id == 1
    assert raised.value.account_id == 21
    assert raised.value.token_id == 11
    assert raised.value.probe_observation["http_status"] == 403


def test_reconciler_reports_earlier_success_when_later_slot_probe_fails() -> None:
    class FailingSecondProbeRemote(Remote):
        def probe_slot(self, slot, *, idempotency_key):
            self.calls.append(("probe", slot.slot_no, idempotency_key))
            return slot.slot_no == 1

    desired = bootstrap_bundle(
        (
            ready_slot(1, 101, "p1", 11, 21, priority=2),
            ready_slot(2, 102, "p2", 12, 22, priority=3),
        )
    )

    with pytest.raises(ReplanRequired) as raised:
        ProviderPoolReconciler(
            InMemoryProviderPoolStore(),
            FailingSecondProbeRemote(),
        ).reconcile(desired)

    assert raised.value.slot_no == 2
    assert raised.value.channel_id == 102
    assert raised.value.verified_slot_nos == (1,)


def test_channel_probe_failure_restores_parent_before_replan() -> None:
    class ParentRestoringRemote(ReadbackRemote):
        def __init__(self):
            super().__init__()
            self.failed_channel = None
            self.repin_channels = []

        def repin_slot(self, slot, *, idempotency_key):
            self.repin_channels.append(slot.channel_id)
            return super().repin_slot(slot, idempotency_key=idempotency_key)

        def probe_slot(self, slot, *, idempotency_key):
            self.calls.append(("probe", slot.slot_no, slot.channel_id, idempotency_key))
            if slot.channel_id == self.failed_channel:
                return ProviderProbeResult(
                    confirmed=False,
                    protocol="responses",
                    http_status=503,
                    error_code="service_unavailable",
                        failure_owner="channel",
                        replan_scope="channel",
                    document={"request_sent": True},
                )
            return True

    parent = bundle()
    store = InMemoryProviderPoolStore()
    remote = ParentRestoringRemote()
    ProviderPoolReconciler(
        store,
        remote,
        require_remote_readback=True,
    ).reconcile(parent)

    remote.calls.clear()
    remote.repin_channels.clear()
    remote.failed_channel = 2
    desired_slot = replace(parent.slots[0], channel_id=2)
    desired = replacement_bundle((desired_slot,))

    with pytest.raises(ReplanRequired) as raised:
        ProviderPoolReconciler(
            store,
            remote,
            require_remote_readback=True,
        ).reconcile(
            desired,
            current_routing=parent.ordered_channel_ids,
            current_slots={1: parent.slots[0]},
            current_cost_input_hash=parent.effective_cost_input_hash,
        )

    assert raised.value.verified_slot_nos == ()
    assert remote.repin_channels == [2, 1]
    assert remote.read_slot_schedulable(parent.slots[0]) is True
    active = store.active(parent.model)
    assert active is not None
    assert active.routing_generation == parent.routing_generation
    assert active.cost_generation == parent.cost_generation
    assert active.slots[0].channel_id == parent.slots[0].channel_id


def test_each_parent_compensation_pass_reasserts_completed_remote_fields() -> None:
    class CostTrackingRemote(ReadbackRemote):
        def __init__(self) -> None:
            super().__init__()
            self.costs = {}

        def set_slot_cost(self, slot, multiplier, *, idempotency_key):
            self.costs[slot.slot_no] = multiplier
            return super().set_slot_cost(
                slot,
                multiplier,
                idempotency_key=idempotency_key,
            )

        def readback(self, desired, *, deferred_slot_nos=frozenset()):
            raw = super().readback(desired)
            return ProviderPoolReadback(
                raw.model,
                raw.billing_floor_multiplier,
                {
                    slot_no: replace(
                        observed,
                        rate_multiplier=self.costs.get(
                            slot_no,
                            observed.rate_multiplier,
                        ),
                    )
                    for slot_no, observed in raw.slots.items()
                },
            )

    parent = bundle()
    candidate = replacement_bundle((replace(parent.slots[0], channel_id=2),))
    remote = CostTrackingRemote()
    ledger = IdempotencyLedger()
    reconciler = ProviderPoolReconciler(
        InMemoryProviderPoolStore(),
        remote,
        ledger=ledger,
        require_remote_readback=True,
    )
    current_slots = {1: parent.slots[0]}

    reconciler._restore_parent_after_failure(
        candidate,
        parent,
        current_slots=current_slots,
        cause=RuntimeError("fixture-first-compensation"),
    )
    first_keys = [
        call[-1]
        for call in remote.calls
        if call[0] == "cost"
    ]
    remote.costs[1] = Decimal("9.9")

    reconciler._restore_parent_after_failure(
        candidate,
        parent,
        current_slots=current_slots,
        cause=RuntimeError("fixture-second-compensation"),
    )
    all_keys = [
        call[-1]
        for call in remote.calls
        if call[0] == "cost"
    ]

    assert remote.costs[1] == parent.cost_plan.desired_slot_costs[1]
    assert len(first_keys) == 1
    assert len(all_keys) == 2
    assert all_keys[0] != all_keys[1]


def test_channel_probe_failure_keeps_unchanged_parent_slot_schedulable() -> None:
    class RollingParentRemote(ReadbackRemote):
        def __init__(self):
            super().__init__()
            self.failed_channel = None
            self.repin_channels = []
            self.probe_channels = []

        def repin_slot(self, slot, *, idempotency_key):
            self.repin_channels.append(slot.channel_id)
            return super().repin_slot(slot, idempotency_key=idempotency_key)

        def probe_slot(self, slot, *, idempotency_key):
            self.probe_channels.append(slot.channel_id)
            if slot.channel_id == self.failed_channel:
                return ProviderProbeResult(
                    confirmed=False,
                    protocol="responses",
                    http_status=None,
                    error_code="provider_probe_transport_failed",
                        failure_owner="channel",
                        replan_scope="channel",
                    document={"request_sent": True, "curl_code": 28},
                )
            return True

    parent = bootstrap_bundle(
        (
            ready_slot(1, 101, "parent-1", 11, 21, priority=2),
            ready_slot(2, 102, "parent-2", 12, 22, priority=3),
        )
    )
    store = InMemoryProviderPoolStore()
    remote = RollingParentRemote()
    ProviderPoolReconciler(
        store,
        remote,
        require_remote_readback=True,
    ).reconcile(parent)
    remote.calls.clear()
    remote.repin_channels.clear()
    remote.probe_channels.clear()
    remote.failed_channel = 201
    candidate = replacement_bundle(
        (
            ready_slot(1, 201, "candidate-1", 31, 41, priority=2),
            parent.slots[1],
        )
    )

    with pytest.raises(ReplanRequired, match="provider_reconcile_probe_failed"):
        ProviderPoolReconciler(
            store,
            remote,
            require_remote_readback=True,
        ).reconcile(
            candidate,
            current_routing=parent.ordered_channel_ids,
            current_slots={slot.slot_no: slot for slot in parent.slots},
            current_cost_input_hash=parent.effective_cost_input_hash,
        )

    disabled_accounts = [
        call[1]
        for call in remote.calls
        if call[0] == "schedulable" and call[2] is False
    ]
    assert disabled_accounts == [1, 1, 1, 1]
    assert all(
        call[1] != 2
        for call in remote.calls
        if call[0] == "schedulable" and call[2] is False
    )
    assert all(call[1] != 2 for call in remote.calls if call[0] == "drain")
    assert remote.repin_channels == [201, 101]
    assert remote.probe_channels == [201, 101]
    assert remote.read_slot_schedulable(parent.slots[0]) is True
    assert remote.read_slot_schedulable(parent.slots[1]) is True


def test_parent_repin_failure_preserves_original_replan_after_fail_closed() -> None:
    class FailingParentRepinRemote(ReadbackRemote):
        def __init__(self):
            super().__init__()
            self.failed_probe_channel = None
            self.failed_repin_channel = None

        def repin_slot(self, slot, *, idempotency_key):
            if slot.channel_id == self.failed_repin_channel:
                raise RuntimeError("fixture-parent-repin-failed")
            return super().repin_slot(slot, idempotency_key=idempotency_key)

        def probe_slot(self, slot, *, idempotency_key):
            if slot.channel_id == self.failed_probe_channel:
                return ProviderProbeResult(
                    confirmed=False,
                    protocol="responses",
                    http_status=None,
                    error_code="provider_probe_transport_failed",
                    failure_owner="channel",
                    replan_scope="channel",
                    document={"request_sent": True, "curl_code": 28},
                )
            return True

    parent = bootstrap_bundle(
        (
            ready_slot(1, 101, "parent-1", 11, 21, priority=2),
            ready_slot(2, 102, "parent-2", 12, 22, priority=3),
        )
    )
    candidate = replacement_bundle(
        (
            ready_slot(1, 201, "candidate-1", 31, 41, priority=2),
            ready_slot(2, 202, "candidate-2", 32, 42, priority=3),
        )
    )
    store = InMemoryProviderPoolStore()
    remote = FailingParentRepinRemote()
    ProviderPoolReconciler(
        store,
        remote,
        require_remote_readback=True,
    ).reconcile(parent)
    remote.failed_probe_channel = 201
    remote.failed_repin_channel = 102

    with pytest.raises(ReplanRequired) as raised:
        ProviderPoolReconciler(
            store,
            remote,
            require_remote_readback=True,
        ).reconcile(
            candidate,
            current_routing=parent.ordered_channel_ids,
            current_slots={slot.slot_no: slot for slot in parent.slots},
            current_cost_input_hash=parent.effective_cost_input_hash,
        )

    assert raised.value.code == "provider_reconcile_probe_failed"
    assert raised.value.slot_no == 1
    assert raised.value.channel_id == 201
    assert raised.value.account_id == 41
    assert raised.value.token_id == 31
    assert raised.value.replan_scope == "channel"
    assert raised.value.probe_observation["document"]["curl_code"] == 28
    assert raised.value.verified_slot_nos == ()
    assert all(
        remote.read_slot_schedulable(slot) is False
        for slot in (*parent.slots, *candidate.slots)
    )


def test_candidate_pin_transport_failure_keeps_partial_generation_for_retry() -> None:
    class FailingCandidatePinRemote(ReadbackRemote):
        def __init__(self):
            super().__init__()
            self.failed_channels = set()
            self.repin_channels = []

        def repin_slot(self, slot, *, idempotency_key):
            self.repin_channels.append(slot.channel_id)
            if slot.channel_id in self.failed_channels:
                raise RuntimeError("a6_transport_failed")
            return super().repin_slot(slot, idempotency_key=idempotency_key)

    parent = bootstrap_bundle(
        (
            ready_slot(1, 101, "parent-1", 11, 21, priority=2),
            ready_slot(2, 102, "parent-2", 12, 22, priority=3),
        )
    )
    candidate = replacement_bundle(
        (
            ready_slot(1, 201, "candidate-1", 31, 41, priority=2),
            ready_slot(2, 202, "candidate-2", 32, 42, priority=3),
        )
    )
    store = InMemoryProviderPoolStore()
    remote = FailingCandidatePinRemote()
    ProviderPoolReconciler(
        store,
        remote,
        require_remote_readback=True,
    ).reconcile(parent)
    remote.failed_channels = {102, 202}

    with pytest.raises(ReplanRequired) as raised:
        ProviderPoolReconciler(
            store,
            remote,
            require_remote_readback=True,
        ).reconcile(
            candidate,
            current_routing=parent.ordered_channel_ids,
            current_slots={slot.slot_no: slot for slot in parent.slots},
            current_cost_input_hash=parent.effective_cost_input_hash,
        )

    assert raised.value.code == "provider_reconcile_pin_failed"
    assert raised.value.slot_no == 2
    assert raised.value.channel_id == 202
    assert raised.value.account_id == 42
    assert raised.value.token_id == 32
    assert raised.value.replan_scope == "platform"
    assert raised.value.probe_observation["error_code"] == "a6_transport_failed"
    assert raised.value.probe_observation["document"]["operation"] == "pin"
    assert raised.value.verified_slot_nos == (1,)
    assert remote.repin_channels[-2:] == [201, 202]
    assert all(
        remote.read_slot_schedulable(slot) is False
        for slot in (*parent.slots, *candidate.slots)
    )


def test_probe_disabled_platform_failure_keeps_generation_without_parent_repin() -> None:
    class ZeroProbeCompensationRemote(ReadbackRemote):
        def __init__(self):
            super().__init__()
            self.failed_channel = None
            self.repin_channels = []

        def repin_slot(self, slot, *, idempotency_key):
            self.repin_channels.append(slot.channel_id)
            if slot.channel_id == self.failed_channel:
                raise RuntimeError("a6_transport_failed")
            return super().repin_slot(slot, idempotency_key=idempotency_key)

        def probe_slot(self, *_args, **_kwargs):
            raise AssertionError("activation probe must stay cold")

        def probe_slot_for_recovery(self, *_args, **_kwargs):
            raise AssertionError("recovery probe must stay cold")

    parent = bootstrap_bundle(
        (
            ready_slot(1, 101, "parent-1", 11, 21, priority=2),
            ready_slot(2, 102, "parent-2", 12, 22, priority=3),
        )
    )
    candidate = replacement_bundle(
        (
            ready_slot(1, 201, "candidate-1", 31, 41, priority=2),
            ready_slot(2, 202, "candidate-2", 32, 42, priority=3),
        )
    )
    store = InMemoryProviderPoolStore()
    remote = ZeroProbeCompensationRemote()
    ProviderPoolReconciler(
        store,
        remote,
        require_remote_readback=True,
        activation_probe_required=False,
    ).reconcile(parent)
    remote.repin_channels.clear()
    remote.failed_channel = 202

    with pytest.raises(ReplanRequired, match="provider_reconcile_pin_failed"):
        ProviderPoolReconciler(
            store,
            remote,
            require_remote_readback=True,
            activation_probe_required=False,
        ).reconcile(
            candidate,
            current_routing=parent.ordered_channel_ids,
            current_slots={slot.slot_no: slot for slot in parent.slots},
            current_cost_input_hash=parent.effective_cost_input_hash,
        )

    assert remote.repin_channels == [201, 202]
    assert all(remote.read_slot_schedulable(slot) is False for slot in parent.slots)
    active = store.active(parent.model)
    assert active is not None
    assert active.routing_generation == parent.routing_generation


def test_reconciler_does_not_repeat_probe_for_preverified_slot() -> None:
    class RecordingProbeRemote(Remote):
        def probe_slot(self, slot, *, idempotency_key):
            assert slot.slot_no != 1
            self.calls.append(("probe", slot.slot_no, idempotency_key))
            return True

    desired = bootstrap_bundle(
        (
            ready_slot(1, 101, "p1", 11, 21, priority=2),
            ready_slot(2, 102, "p2", 12, 22, priority=3),
        )
    )
    remote = RecordingProbeRemote()

    report = ProviderPoolReconciler(InMemoryProviderPoolStore(), remote).reconcile(
        desired,
        verified_slot_nos=(1,),
    )

    assert report.activated is True
    assert [(call[0], call[1]) for call in remote.calls if call[0] == "probe"] == [
        ("probe", 2)
    ]


@pytest.mark.parametrize(
    "error_code",
    (
        "provider_probe_transport_failed",
        "provider_probe_stream_invalid",
        "provider_chat_probe_transport_failed",
        "provider_chat_probe_response_invalid",
    ),
)
def test_reconciler_replans_after_probe_transport_failure(error_code: str) -> None:
    class TransportFailingProbeRemote(Remote):
        def probe_slot(self, slot, *, idempotency_key):
            self.calls.append(("probe", slot.slot_no, idempotency_key))
            raise RuntimeError(error_code)

    ledger = IdempotencyLedger()

    with pytest.raises(ReplanRequired, match="provider_reconcile_probe_failed") as raised:
        ProviderPoolReconciler(
            InMemoryProviderPoolStore(),
            TransportFailingProbeRemote(),
            ledger=ledger,
        ).reconcile(bundle())

    assert raised.value.slot_no == 1
    assert raised.value.channel_id == 1
    probe_key = ledger.key(bundle(), "probe_slot", 1)
    assert probe_key not in ledger.completed
    assert probe_key not in ledger.in_progress


def test_reconciler_does_not_replan_after_internal_probe_failure() -> None:
    class InternalFailingProbeRemote(Remote):
        def probe_slot(self, slot, *, idempotency_key):
            raise RuntimeError("provider_key_unauthorized")

    with pytest.raises(
        ProviderPoolReconcileError,
        match="provider_reconcile_probe_slot_failed",
    ):
        ProviderPoolReconciler(
            InMemoryProviderPoolStore(),
            InternalFailingProbeRemote(),
        ).reconcile(bundle())


def test_reconciler_rejects_busy_persistent_action() -> None:
    ledger = IdempotencyLedger()
    ledger.claim(bundle(), "drain_slot", (1, 21, 11))
    remote = Remote()

    with pytest.raises(ProviderPoolReconcileError, match="provider_reconcile_action_in_progress"):
        ProviderPoolReconciler(InMemoryProviderPoolStore(), remote, ledger=ledger).reconcile(
            bundle()
        )

    assert [call[:3] for call in remote.calls] == [("schedulable", 1, False)]
    assert remote.read_slot_schedulable(bundle().slots[0]) is False


def test_postgres_ledger_persists_completion_across_instances() -> None:
    database = LedgerDatabase()
    first_ledger = PostgresProviderActionLedger(database.connect)
    claim = first_ledger.claim(bundle(), "repin_slot", 1)

    assert claim.disposition is ActionClaimDisposition.CLAIMED
    assert claim.action_id == 1
    assert claim.attempt_no == 1

    first_ledger.complete(
        claim,
        observed_after={"verified": True, "channel_id": 1},
        elapsed_ms=12,
    )
    resumed = PostgresProviderActionLedger(database.connect).claim(bundle(), "repin_slot", 1)

    assert resumed.disposition is ActionClaimDisposition.COMPLETED
    assert database.action["status"] == "completed"
    assert database.action["observed_after"] == {"verified": True, "channel_id": 1}
    assert database.attempts == [
        {
            "action_id": 1,
            "attempt_no": 1,
            "status": "completed",
            "error_class": None,
        }
    ]
    assert database.rollbacks == 0
    assert database.commits == database.closes


def test_postgres_ledger_reuses_completed_checkpoint_from_legacy_key_format() -> None:
    desired = bundle()
    database = LedgerDatabase()
    ledger = PostgresProviderActionLedger(database.connect)
    first = ledger.claim(desired, "repin_slot", 1)
    legacy_key = legacy_provider_action_key(desired, "repin_slot", 1)
    database.action["idempotency_key"] = legacy_key
    ledger.complete(first, observed_after={"verified": True}, elapsed_ms=4)
    database.calls.clear()

    resumed = PostgresProviderActionLedger(database.connect).claim(
        desired,
        "repin_slot",
        1,
    )

    assert resumed.disposition is ActionClaimDisposition.COMPLETED
    assert resumed.action_id == first.action_id
    assert resumed.key == legacy_key
    assert not any(
        query.startswith("INSERT INTO a6_trade.trade_control_actions")
        for query, _params in database.calls
    )


def test_postgres_ledger_retries_failed_checkpoint_from_legacy_key_format() -> None:
    desired = bundle()
    database = LedgerDatabase()
    ledger = PostgresProviderActionLedger(database.connect)
    first = ledger.claim(desired, "probe_slot", 1)
    legacy_key = legacy_provider_action_key(desired, "probe_slot", 1)
    database.action["idempotency_key"] = legacy_key
    ledger.fail(first, error_class="provider_reconcile_probe_slot_failed", elapsed_ms=7)
    database.calls.clear()

    retry = PostgresProviderActionLedger(database.connect).claim(
        desired,
        "probe_slot",
        1,
    )

    assert retry.disposition is ActionClaimDisposition.CLAIMED
    assert retry.action_id == first.action_id
    assert retry.attempt_no == 2
    assert retry.key == legacy_key
    assert not any(
        query.startswith("INSERT INTO a6_trade.trade_control_actions")
        for query, _params in database.calls
    )


def test_postgres_ledger_blocks_live_claim_and_retries_failed_attempt() -> None:
    database = LedgerDatabase()
    ledger = PostgresProviderActionLedger(database.connect)
    first = ledger.claim(bundle(), "set_priority", 1)

    assert ledger.claim(bundle(), "set_priority", 1).disposition is (ActionClaimDisposition.BUSY)

    ledger.fail(first, error_class="provider_reconcile_set_priority_failed", elapsed_ms=7)
    retry = PostgresProviderActionLedger(database.connect).claim(bundle(), "set_priority", 1)

    assert retry.disposition is ActionClaimDisposition.CLAIMED
    assert retry.attempt_no == 2
    assert database.attempts[0]["status"] == "failed"
    assert database.attempts[1]["status"] == "started"


def test_postgres_ledger_reclaims_crashed_attempt_and_closes_stale_audit_row() -> None:
    database = LedgerDatabase()
    first = PostgresProviderActionLedger(database.connect).claim(bundle(), "set_slot_cost", 1)

    assert first.disposition is ActionClaimDisposition.CLAIMED
    database.force_stale = True
    resumed = PostgresProviderActionLedger(database.connect).claim(bundle(), "set_slot_cost", 1)

    assert resumed.disposition is ActionClaimDisposition.CLAIMED
    assert resumed.attempt_no == 2
    assert database.attempts[0]["status"] == "failed"
    assert database.attempts[0]["error_class"] == ("provider_action_ledger_stale_reclaimed")
    assert database.attempts[1]["status"] == "started"


def test_priority_staging_accepts_sparse_ranks_with_vacant_slots() -> None:
    active = tuple(
        ready_slot(
            slot_no,
            slot_no,
            f"p{slot_no}",
            10 + slot_no,
            20 + slot_no,
            priority=slot_no + 1,
        )
        for slot_no in (1, 2, 3, 6, 10)
    )
    bundle_slots = []
    for slot_no in range(1, 13):
        if slot_no in {1, 2, 3, 6, 10}:
            slot = active[[1, 2, 3, 6, 10].index(slot_no)]
            bundle_slots.append(replace(slot, rank=slot_no, priority=slot_no + 1))
        else:
            bundle_slots.append(
                ProviderSlot(
                    slot_no=slot_no,
                    channel_id=None,
                    rank=None,
                    priority=None,
                    assignment_id=None,
                    enodot_binding_id=None,
                    token_id=None,
                    account_id=None,
                    slot_state=SlotState.VACANT,
                    funding_eligible=False,
                    desired_schedulable=False,
                )
            )
    candidate = replacement_bundle(tuple(bundle_slots))
    staged = _temporary_priority_slots(candidate, tuple(active))
    assert len(staged) == 5
    assert len({slot.priority for slot in staged}) == 5
