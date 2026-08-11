"""Stable Provider slots and routing/cost generation state transitions."""

from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from decimal import Decimal
from enum import Enum
from types import MappingProxyType
from typing import Protocol

from .trade_provider_cost import CostPlan


class ProviderPoolError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class SlotState(str, Enum):
    PROVISIONING = "PROVISIONING"
    DRAINING = "DRAINING"
    REPINNING = "REPINNING"
    PROBING = "PROBING"
    READY = "READY"
    REPAIRING = "REPAIRING"
    QUARANTINED = "QUARANTINED"
    VACANT = "VACANT"


class GenerationState(str, Enum):
    PLANNED = "planned"
    APPLYING = "applying"
    ACTIVE = "active"
    FAILED = "failed"
    SUPERSEDED = "superseded"


class PricingState(str, Enum):
    PLANNED = "planned"
    APPLYING = "applying"
    CONFIRMED = "confirmed"
    ACTIVE = "active"
    FAILED = "failed"
    SUPERSEDED = "superseded"


@dataclass(frozen=True)
class ProviderSlot:
    slot_no: int
    channel_id: int | None
    rank: int | None
    priority: int | None
    assignment_id: str | int | None
    enodot_binding_id: int | None
    token_id: int | None
    account_id: int | None
    slot_state: SlotState
    funding_eligible: bool
    desired_schedulable: bool
    observed_schedulable: bool = False
    desired_cost_multiplier: Decimal | None = None
    observed_cost_multiplier: Decimal | None = None
    confirmed_pin_binding_id: int | None = None
    pin_confirmed: bool = False
    probe_confirmed: bool = False
    state_version: int = 1

    def __post_init__(self) -> None:
        if isinstance(self.slot_no, bool) or self.slot_no < 1:
            raise ProviderPoolError("provider_slot_no_invalid")
        if isinstance(self.state_version, bool) or self.state_version < 1:
            raise ProviderPoolError("provider_slot_state_version_invalid")
        if not all(
            isinstance(value, bool)
            for value in (
                self.funding_eligible,
                self.desired_schedulable,
                self.observed_schedulable,
                self.pin_confirmed,
                self.probe_confirmed,
            )
        ):
            raise ProviderPoolError("provider_slot_boolean_invalid")
        if self.slot_state is SlotState.VACANT:
            if any(
                value is not None
                for value in (
                    self.assignment_id,
                    self.enodot_binding_id,
                    self.token_id,
                    self.account_id,
                    self.confirmed_pin_binding_id,
                )
            ):
                raise ProviderPoolError("provider_vacant_assignment_present")
            if self.desired_schedulable or self.observed_schedulable or self.funding_eligible:
                raise ProviderPoolError("provider_vacant_schedulable")
        if self.slot_state is SlotState.READY:
            required = (
                self.channel_id,
                self.rank,
                self.priority,
                self.assignment_id,
                self.enodot_binding_id,
                self.token_id,
                self.account_id,
                self.desired_cost_multiplier,
                self.observed_cost_multiplier,
            )
            if any(value is None for value in required):
                raise ProviderPoolError("provider_ready_fields_missing")
            if not (
                self.funding_eligible
                and self.desired_schedulable
                and self.observed_schedulable
                and self.pin_confirmed
                and self.probe_confirmed
            ):
                raise ProviderPoolError("provider_ready_confirmation_missing")
            if self.desired_cost_multiplier != self.observed_cost_multiplier:
                raise ProviderPoolError("provider_ready_cost_readback_mismatch")
        for name in (
            "channel_id",
            "rank",
            "priority",
            "enodot_binding_id",
            "token_id",
            "account_id",
            "confirmed_pin_binding_id",
        ):
            value = getattr(self, name)
            if value is not None and (isinstance(value, bool) or value < 1):
                raise ProviderPoolError(f"provider_slot_{name}_invalid")

    @property
    def slot_name(self) -> str:
        return f"a6-provider-{self.slot_no}"


@dataclass(frozen=True)
class ProviderBundle:
    model: str
    routing_generation: int
    parent_routing_generation: int | None
    cost_generation: int
    parent_cost_generation: int | None
    generation_source: str
    source_hash: str
    ordered_channel_ids: tuple[int, ...]
    standby_channel_ids: tuple[int, ...]
    desired_size: int
    slots: tuple[ProviderSlot, ...]
    cost_plan: CostPlan
    cost_input_hash: str | None = None
    token_mix_version: str = "bootstrap-v1"
    exchange_rate_version: str = "identity-v1"
    generation_state: GenerationState = GenerationState.PLANNED
    pricing_state: PricingState = PricingState.PLANNED
    config_version: str = "a6-provider-topn-failover-v2"

    def __post_init__(self) -> None:
        if not str(self.model).strip():
            raise ProviderPoolError("provider_bundle_model_invalid")
        for name in ("routing_generation", "cost_generation", "desired_size"):
            value = getattr(self, name)
            if isinstance(value, bool) or value < 1:
                raise ProviderPoolError(f"provider_bundle_{name}_invalid")
        if len(self.slots) != self.desired_size:
            raise ProviderPoolError("provider_bundle_size_mismatch")
        slot_nos = [slot.slot_no for slot in self.slots]
        if slot_nos != list(range(1, self.desired_size + 1)):
            raise ProviderPoolError("provider_bundle_slots_not_dense")
        ready = [slot for slot in self.slots if slot.slot_state is SlotState.READY]
        ready_channels = [slot.channel_id for slot in ready]
        ready_tokens = [slot.token_id for slot in ready]
        ready_accounts = [slot.account_id for slot in ready]
        if len(ready_channels) != len(set(ready_channels)):
            raise ProviderPoolError("provider_bundle_ready_channel_duplicate")
        if len(ready_tokens) != len(set(ready_tokens)):
            raise ProviderPoolError("provider_bundle_ready_token_duplicate")
        if len(ready_accounts) != len(set(ready_accounts)):
            raise ProviderPoolError("provider_bundle_ready_account_duplicate")
        # A fail-closed assigned slot may remain REPAIRING while its cost intent is
        # still part of the immutable generation. Only VACANT slots lack a cost.
        expected_cost_slots = {
            slot.slot_no for slot in self.slots if slot.slot_state is not SlotState.VACANT
        }
        if set(self.cost_plan.desired_slot_costs) != expected_cost_slots:
            raise ProviderPoolError("provider_bundle_cost_slots_mismatch")
        if not _sha256(self.source_hash):
            raise ProviderPoolError("provider_bundle_source_hash_invalid")
        if self.cost_input_hash is not None and not _sha256(self.cost_input_hash):
            raise ProviderPoolError("provider_bundle_cost_input_hash_invalid")
        if not str(self.token_mix_version).strip():
            raise ProviderPoolError("provider_bundle_token_mix_version_invalid")
        if not str(self.exchange_rate_version).strip():
            raise ProviderPoolError("provider_bundle_exchange_rate_version_invalid")

    @property
    def ready_slots(self) -> tuple[ProviderSlot, ...]:
        return tuple(slot for slot in self.slots if slot.slot_state is SlotState.READY)

    @property
    def effective_cost_input_hash(self) -> str:
        return self.source_hash if self.cost_input_hash is None else self.cost_input_hash

    @property
    def dispatch_hash(self) -> str:
        document = {
            "model": self.model,
            "routing_generation": self.routing_generation,
            "cost_generation": self.cost_generation,
            "ready": [
                {
                    "slot_no": slot.slot_no,
                    "enodot_binding_id": slot.enodot_binding_id,
                    "account_id": slot.account_id,
                    "token_id": slot.token_id,
                    "channel_id": slot.channel_id,
                    "priority": slot.priority,
                }
                for slot in sorted(self.ready_slots, key=lambda item: (item.priority, item.slot_no))
            ],
        }
        return hashlib.sha256(
            json.dumps(document, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()


@dataclass(frozen=True)
class ProviderActivationRollback:
    model: str
    rolled_back_routing_generation: int
    rolled_back_cost_generation: int
    restored_routing_generation: int | None
    restored_cost_generation: int | None


class ProviderPoolStore(Protocol):
    """Persistence boundary used by the remote reconciliation saga."""

    def active(self, model: str) -> object | None: ...

    def plan(
        self,
        bundle: ProviderBundle,
        *,
        expected_routing_generation: int | None,
        expected_cost_generation: int | None,
    ) -> object: ...

    def mark_applying(self, bundle: ProviderBundle) -> object: ...

    def confirm_cost(
        self,
        bundle: ProviderBundle,
        *,
        observed_output_hash: str,
        observed_billing_floor: Decimal,
    ) -> object: ...

    def activate(
        self,
        bundle: ProviderBundle,
        *,
        deferred_slot_nos: Sequence[int] = (),
    ) -> object: ...

    def hold_membership_for_auto_pool(
        self,
        bundle: ProviderBundle,
        *,
        slot_nos: Sequence[int],
    ) -> object: ...

    def rollback_activation(self, bundle: ProviderBundle) -> ProviderActivationRollback: ...

    def record_billing_floor(
        self,
        model: str,
        *,
        cost_generation: int,
        observed_billing_floor: Decimal,
    ) -> object: ...


def assign_channels_to_stable_slots(
    current_slots: Sequence[ProviderSlot],
    ordered_channel_ids: Sequence[int],
    *,
    desired_size: int,
    priority_offset: int = 1,
) -> Mapping[int, tuple[int | None, int | None, int | None]]:
    """Retain incumbent channels and allocate only entrants to released slots.

    The returned mapping is ``slot_no -> (channel_id, rank, priority)``.  A missing
    candidate is represented as an explicit vacant tuple.
    """

    if isinstance(desired_size, bool) or desired_size < 1:
        raise ProviderPoolError("provider_slot_plan_size_invalid")
    if isinstance(priority_offset, bool) or priority_offset < 0:
        raise ProviderPoolError("provider_slot_plan_priority_offset_invalid")
    ordered = tuple(
        _positive_int(item, "provider_slot_plan_channel_invalid") for item in ordered_channel_ids
    )
    if len(ordered) != len(set(ordered)):
        raise ProviderPoolError("provider_slot_plan_channel_duplicate")
    ranks = {channel_id: rank for rank, channel_id in enumerate(ordered[:desired_size], start=1)}
    current_by_slot = {slot.slot_no: slot for slot in current_slots if slot.slot_no <= desired_size}
    result: dict[int, tuple[int | None, int | None, int | None]] = {}
    retained: set[int] = set()
    free_slots: list[int] = []
    for slot_no in range(1, desired_size + 1):
        incumbent = current_by_slot.get(slot_no)
        if incumbent is not None and incumbent.channel_id in ranks:
            channel_id = incumbent.channel_id
            rank = ranks[channel_id]
            result[slot_no] = (channel_id, rank, priority_offset + rank)
            retained.add(channel_id)
        else:
            free_slots.append(slot_no)

    entrants = [channel_id for channel_id in ordered[:desired_size] if channel_id not in retained]
    for slot_no, channel_id in zip(free_slots, entrants, strict=False):
        rank = ranks[channel_id]
        result[slot_no] = (channel_id, rank, priority_offset + rank)
    for slot_no in free_slots[len(entrants) :]:
        result[slot_no] = (None, None, None)
    return MappingProxyType(dict(sorted(result.items())))


class InMemoryProviderPoolStore:
    """Thread-safe executable CAS model used by shadow mode and unit tests."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._active: dict[str, ProviderBundle] = {}
        self._planned: dict[tuple[str, int], ProviderBundle] = {}

    def active(self, model: str) -> ProviderBundle | None:
        with self._lock:
            return self._active.get(str(model))

    def plan(
        self,
        bundle: ProviderBundle,
        *,
        expected_routing_generation: int | None,
        expected_cost_generation: int | None,
    ) -> ProviderBundle:
        with self._lock:
            current = self._active.get(bundle.model)
            observed_routing = None if current is None else current.routing_generation
            observed_cost = None if current is None else current.cost_generation
            if (
                observed_routing != expected_routing_generation
                or observed_cost != expected_cost_generation
            ):
                raise ProviderPoolError("provider_bundle_plan_cas_mismatch")
            if bundle.parent_routing_generation != expected_routing_generation:
                raise ProviderPoolError("provider_bundle_parent_routing_mismatch")
            if bundle.parent_cost_generation != expected_cost_generation:
                raise ProviderPoolError("provider_bundle_parent_cost_mismatch")
            key = (bundle.model, bundle.routing_generation)
            existing = self._planned.get(key)
            if existing is not None:
                if existing != bundle:
                    raise ProviderPoolError("provider_bundle_generation_conflict")
                return existing
            self._planned[key] = bundle
            return bundle

    def mark_applying(self, desired: ProviderBundle) -> ProviderBundle:
        with self._lock:
            bundle = self._planned_bundle(
                desired.model,
                desired.routing_generation,
            )
            if bundle.generation_state not in {GenerationState.PLANNED, GenerationState.APPLYING}:
                raise ProviderPoolError("provider_bundle_not_plannable")
            if bundle.pricing_state not in {
                PricingState.PLANNED,
                PricingState.APPLYING,
                PricingState.CONFIRMED,
            }:
                raise ProviderPoolError("provider_bundle_cost_not_plannable")
            updated = replace(
                bundle,
                generation_state=GenerationState.APPLYING,
                pricing_state=(
                    PricingState.CONFIRMED
                    if bundle.pricing_state is PricingState.CONFIRMED
                    else PricingState.APPLYING
                ),
            )
            self._planned[(desired.model, desired.routing_generation)] = updated
            return updated

    def confirm_cost(
        self,
        desired: ProviderBundle,
        *,
        observed_output_hash: str,
        observed_billing_floor: Decimal,
    ) -> ProviderBundle:
        with self._lock:
            bundle = self._planned_bundle(
                desired.model,
                desired.routing_generation,
            )
            if bundle.generation_state is not GenerationState.APPLYING:
                raise ProviderPoolError("provider_bundle_not_applying")
            if observed_output_hash != bundle.cost_plan.cost_output_hash:
                raise ProviderPoolError("provider_bundle_cost_hash_mismatch")
            if observed_billing_floor < bundle.cost_plan.billing_floor_multiplier:
                raise ProviderPoolError("provider_bundle_billing_floor_too_low")
            updated = replace(bundle, pricing_state=PricingState.CONFIRMED)
            self._planned[(desired.model, desired.routing_generation)] = updated
            return updated

    def activate(
        self,
        desired: ProviderBundle,
        *,
        deferred_slot_nos: Sequence[int] = (),
    ) -> ProviderBundle:
        with self._lock:
            model = desired.model
            routing_generation = desired.routing_generation
            bundle = self._planned_bundle(model, routing_generation)
            current = self._active.get(model)
            observed_routing = None if current is None else current.routing_generation
            observed_cost = None if current is None else current.cost_generation
            if (
                observed_routing != desired.parent_routing_generation
                or observed_cost != desired.parent_cost_generation
            ):
                raise ProviderPoolError("provider_bundle_activate_cas_mismatch")
            if bundle.generation_state is not GenerationState.APPLYING:
                raise ProviderPoolError("provider_bundle_not_applying")
            if bundle.pricing_state is not PricingState.CONFIRMED:
                raise ProviderPoolError("provider_bundle_cost_not_confirmed")
            if any(
                slot.slot_state not in {SlotState.READY, SlotState.VACANT} for slot in bundle.slots
            ):
                raise ProviderPoolError("provider_bundle_slots_unverified")
            deferred = _slot_no_set(deferred_slot_nos, "provider_auto_pool_defer_slot_invalid")

            def comparable(slot: ProviderSlot) -> object:
                if slot.slot_no in deferred:
                    return (
                        slot.slot_no,
                        slot.channel_id,
                        slot.rank,
                        slot.priority,
                        slot.assignment_id,
                        slot.enodot_binding_id,
                        slot.token_id,
                        slot.account_id,
                        slot.slot_state,
                        slot.funding_eligible,
                        slot.desired_schedulable,
                        slot.observed_schedulable,
                        slot.desired_cost_multiplier,
                        slot.observed_cost_multiplier,
                    )
                return replace(slot, confirmed_pin_binding_id=None)

            if len(desired.slots) != len(bundle.slots) or any(
                comparable(slot) != comparable(bundle.slots[index])
                for index, slot in enumerate(desired.slots)
            ):
                raise ProviderPoolError("provider_bundle_activation_readback_conflict")
            activated = replace(
                bundle,
                slots=tuple(
                    replace(
                        slot,
                        slot_state=SlotState.REPAIRING,
                        desired_schedulable=False,
                        observed_schedulable=False,
                        confirmed_pin_binding_id=None,
                        pin_confirmed=False,
                        probe_confirmed=False,
                        state_version=slot.state_version + 1,
                    )
                    if slot.slot_no in deferred
                    else slot
                    for slot in desired.slots
                ),
                generation_state=GenerationState.ACTIVE,
                pricing_state=PricingState.ACTIVE,
            )
            if current is not None:
                superseded = replace(
                    current,
                    generation_state=GenerationState.SUPERSEDED,
                    pricing_state=PricingState.SUPERSEDED,
                )
                self._planned[(model, current.routing_generation)] = superseded
            self._active[model] = activated
            self._planned[(model, routing_generation)] = activated
            return activated

    def hold_membership_for_auto_pool(
        self,
        desired: ProviderBundle,
        *,
        slot_nos: Sequence[int],
    ) -> ProviderBundle:
        held = _slot_no_set(slot_nos, "provider_auto_pool_hold_slot_invalid")
        if not held:
            raise ProviderPoolError("provider_auto_pool_hold_slots_empty")
        with self._lock:
            active = self._active.get(desired.model)
            if (
                active is None
                or active.routing_generation != desired.routing_generation
                or active.cost_generation != desired.cost_generation
            ):
                raise ProviderPoolError("provider_auto_pool_hold_generation_mismatch")
            desired_by_slot = {slot.slot_no: slot for slot in desired.slots}
            if not held.issubset(desired_by_slot):
                raise ProviderPoolError("provider_auto_pool_hold_slot_invalid")
            slots = []
            for slot in active.slots:
                if slot.slot_no not in held:
                    slots.append(slot)
                    continue
                expected = desired_by_slot[slot.slot_no]
                if (
                    slot.slot_state is not SlotState.READY
                    or slot.account_id != expected.account_id
                    or slot.token_id != expected.token_id
                ):
                    raise ProviderPoolError("provider_auto_pool_hold_slot_conflict")
                slots.append(
                    replace(
                        slot,
                        slot_state=SlotState.REPAIRING,
                        desired_schedulable=False,
                        observed_schedulable=False,
                        state_version=slot.state_version + 1,
                    )
                )
            updated = replace(active, slots=tuple(slots))
            self._active[desired.model] = updated
            self._planned[(desired.model, desired.routing_generation)] = updated
            return updated

    def rollback_activation(self, desired: ProviderBundle) -> ProviderActivationRollback:
        """CAS-compensate an activation while retaining the candidate for retry."""

        with self._lock:
            model = desired.model
            candidate = self._planned_bundle(model, desired.routing_generation)
            if (
                candidate.cost_generation != desired.cost_generation
                or candidate.parent_routing_generation != desired.parent_routing_generation
                or candidate.parent_cost_generation != desired.parent_cost_generation
            ):
                raise ProviderPoolError("provider_bundle_rollback_cas_mismatch")

            current = self._active.get(model)
            candidate_is_active = (
                current is candidate
                and candidate.generation_state is GenerationState.ACTIVE
                and candidate.pricing_state is PricingState.ACTIVE
            )
            candidate_is_retriable = (
                candidate.generation_state is GenerationState.APPLYING
                and candidate.pricing_state is PricingState.CONFIRMED
            )
            parent: ProviderBundle | None = None
            if desired.parent_routing_generation is not None:
                parent = self._planned.get((model, desired.parent_routing_generation))
                if parent is None or parent.cost_generation != desired.parent_cost_generation:
                    raise ProviderPoolError("provider_bundle_rollback_cas_mismatch")

            if candidate_is_active:
                if parent is None:
                    if desired.parent_cost_generation is not None or current is not candidate:
                        raise ProviderPoolError("provider_bundle_rollback_cas_mismatch")
                elif (
                    parent.generation_state is not GenerationState.SUPERSEDED
                    or parent.pricing_state is not PricingState.SUPERSEDED
                ):
                    raise ProviderPoolError("provider_bundle_rollback_cas_mismatch")

                retryable = replace(
                    candidate,
                    generation_state=GenerationState.APPLYING,
                    pricing_state=PricingState.CONFIRMED,
                )
                self._planned[(model, desired.routing_generation)] = retryable
                if parent is None:
                    self._active.pop(model, None)
                else:
                    parent = replace(
                        parent,
                        generation_state=GenerationState.ACTIVE,
                        pricing_state=PricingState.ACTIVE,
                    )
                    self._planned[(model, parent.routing_generation)] = parent
                    self._active[model] = parent
            elif candidate_is_retriable:
                if parent is None:
                    if current is not None or desired.parent_cost_generation is not None:
                        raise ProviderPoolError("provider_bundle_rollback_cas_mismatch")
                elif (
                    current is not parent
                    or parent.generation_state is not GenerationState.ACTIVE
                    or parent.pricing_state is not PricingState.ACTIVE
                ):
                    raise ProviderPoolError("provider_bundle_rollback_cas_mismatch")
            else:
                raise ProviderPoolError("provider_bundle_rollback_cas_mismatch")

            return ProviderActivationRollback(
                model=model,
                rolled_back_routing_generation=desired.routing_generation,
                rolled_back_cost_generation=desired.cost_generation,
                restored_routing_generation=desired.parent_routing_generation,
                restored_cost_generation=desired.parent_cost_generation,
            )

    def record_billing_floor(
        self,
        model: str,
        *,
        cost_generation: int,
        observed_billing_floor: Decimal,
    ) -> ProviderBundle:
        with self._lock:
            active = self._active.get(str(model))
            if active is None or active.cost_generation != cost_generation:
                raise ProviderPoolError("provider_bundle_billing_record_cas_mismatch")
            if observed_billing_floor < active.cost_plan.billing_floor_multiplier:
                raise ProviderPoolError("provider_bundle_billing_floor_too_low")
            return active

    def _planned_bundle(self, model: str, routing_generation: int) -> ProviderBundle:
        bundle = self._planned.get((str(model), int(routing_generation)))
        if bundle is None:
            raise ProviderPoolError("provider_bundle_not_found")
        return bundle


def _positive_int(value: object, code: str) -> int:
    if isinstance(value, bool):
        raise ProviderPoolError(code)
    try:
        result = int(value)
    except (TypeError, ValueError):
        raise ProviderPoolError(code) from None
    if result < 1:
        raise ProviderPoolError(code)
    return result


def _slot_no_set(values: Sequence[int], code: str) -> frozenset[int]:
    result = tuple(_positive_int(value, code) for value in values)
    if len(result) != len(set(result)):
        raise ProviderPoolError(code)
    return frozenset(result)


def _sha256(value: object) -> bool:
    text = str(value)
    return len(text) == 64 and all(char in "0123456789abcdefABCDEF" for char in text)
