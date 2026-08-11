"""Build plan-only Provider generations from PostgreSQL Observer evidence.

The adapter reads one model in a repeatable-read transaction.  The controller owns
only the ``plan`` persistence capability, so this path cannot call A6/Enodot or move a
generation into applying/active.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import MappingProxyType
from typing import Any, Protocol

from .trade_provider_cost import (
    DEFAULT_COST_QUANTUM,
    CostPlan,
    build_cost_plan,
    normalize_slot_cost,
)
from .trade_provider_inventory import (
    InventoryEntry,
    InventoryState,
    LiquidityState,
    next_desired_pool_size,
)
from .trade_provider_pool import (
    ProviderBundle,
    ProviderSlot,
    SlotState,
    assign_channels_to_stable_slots,
)
from .trade_provider_pool_rank import (
    Candidate,
    ObserverSnapshot,
    ProviderPoolRankError,
    RankedPool,
    accept_snapshot,
    candidate_from_mapping,
    cost_input_hash,
    effective_price,
    effective_price_cny,
    rank_candidates,
    snapshot_hash,
)
from .trade_provider_pool_shadow import (
    ProviderPoolShadowInput,
    ProviderPoolShadowReconciler,
    ProviderPoolShadowReport,
    ProviderPoolShadowStore,
)
from .trade_provider_pool_shadow_evidence import build_shadow_evidence
from .trade_provider_pool_size_evidence import build_pool_size_evidence
from .trade_provider_pool_store import PostgresProviderPoolStore


class ProviderPoolShadowControllerError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class ProviderPoolShadowControllerConfig:
    model: str
    config_version: str = "a6-provider-topn-failover-v2"
    generation_source: str = "observer-shadow"
    observer_freshness_seconds: int = 120
    inventory_freshness_seconds: int = 900
    minimum_health_observers: int = 2
    size_mode: str = "all_funded"
    initial_max_active_slots: int = 12
    minimum_active_slots: int = 4
    safe_concurrency_per_slot: int = 1
    target_utilization: Decimal = Decimal("0.7")
    minimum_pool_observation_days: int = 14
    minimum_pool_shrink_interval_hours: int = 72
    standby_count: int = 12
    priority_offset: int = 1
    retry_reserve_multiplier: Decimal | None = None
    bootstrap_retry_reserve: Decimal = Decimal("0.2")
    realized_cost_window_hours: int = 24
    realized_cost_match_tolerance_seconds: int = 5
    minimum_cost_samples: int = 100
    realized_cost_p95_multiplier: Decimal | None = None
    margin_bps: int = 1000
    headroom_bps: int = 500
    conservative_fallback_cost: Decimal = Decimal("0")
    exchange_factor: Decimal = Decimal("1")
    fee_factor: Decimal = Decimal("1")
    maximum_provider_cost_cny: Decimal | None = None
    token_mix_version: str = "bootstrap-v1"
    exchange_rate_version: str = "identity-v1"
    retry_reserve_window: str = "bootstrap"
    realized_cost_window: str = "provider-usage-match-v1"
    quantization_version: str = "enodot-decimal-10-4-ceiling-v1"
    quantum: Decimal = DEFAULT_COST_QUANTUM

    def __post_init__(self) -> None:
        if not str(self.model).strip():
            raise ProviderPoolShadowControllerError("provider_shadow_controller_model_invalid")
        for name in (
            "observer_freshness_seconds",
            "inventory_freshness_seconds",
            "minimum_health_observers",
            "initial_max_active_slots",
            "minimum_active_slots",
            "safe_concurrency_per_slot",
            "minimum_pool_observation_days",
            "minimum_pool_shrink_interval_hours",
            "realized_cost_window_hours",
            "realized_cost_match_tolerance_seconds",
            "minimum_cost_samples",
        ):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or value < 1
                or value > 2_147_483_647
            ):
                raise ProviderPoolShadowControllerError(
                    f"provider_shadow_controller_{name}_invalid"
                )
        mode = str(self.size_mode or "").strip().casefold()
        if mode not in {"all_funded", "evidence_based"}:
            raise ProviderPoolShadowControllerError(
                "provider_shadow_controller_size_mode_invalid"
            )
        object.__setattr__(self, "size_mode", mode)
        if self.minimum_active_slots > self.initial_max_active_slots:
            raise ProviderPoolShadowControllerError(
                "provider_shadow_controller_pool_size_bounds_invalid"
            )
        if self.minimum_pool_observation_days < 14:
            raise ProviderPoolShadowControllerError(
                "provider_shadow_controller_minimum_pool_observation_days_invalid"
            )
        if self.minimum_pool_shrink_interval_hours < 72:
            raise ProviderPoolShadowControllerError(
                "provider_shadow_controller_minimum_pool_shrink_interval_hours_invalid"
            )
        try:
            utilization = Decimal(str(self.target_utilization))
        except (ArithmeticError, ValueError):
            raise ProviderPoolShadowControllerError(
                "provider_shadow_controller_target_utilization_invalid"
            ) from None
        if not utilization.is_finite() or utilization <= 0 or utilization > 1:
            raise ProviderPoolShadowControllerError(
                "provider_shadow_controller_target_utilization_invalid"
            )
        sign, digits, exponent = utilization.as_tuple()
        if sign or len(digits) > 18 or exponent < -12 or exponent > 0:
            raise ProviderPoolShadowControllerError(
                "provider_shadow_controller_target_utilization_invalid"
            )
        object.__setattr__(self, "target_utilization", utilization)
        if isinstance(self.standby_count, bool) or self.standby_count < 0:
            raise ProviderPoolShadowControllerError(
                "provider_shadow_controller_standby_count_invalid"
            )
        if isinstance(self.priority_offset, bool) or self.priority_offset < 0:
            raise ProviderPoolShadowControllerError(
                "provider_shadow_controller_priority_offset_invalid"
            )
        if self.priority_offset + 2 * self.initial_max_active_slots > 32767:
            raise ProviderPoolShadowControllerError(
                "provider_shadow_controller_priority_range_invalid"
            )
        if not str(self.realized_cost_window).strip():
            raise ProviderPoolShadowControllerError(
                "provider_shadow_controller_realized_cost_window_invalid"
            )
        if self.realized_cost_p95_multiplier is not None:
            realized = _optional_decimal(self.realized_cost_p95_multiplier)
            if realized is None or not realized.is_finite() or realized < 0:
                raise ProviderPoolShadowControllerError(
                    "provider_shadow_controller_realized_cost_invalid"
                )
            object.__setattr__(self, "realized_cost_p95_multiplier", realized)
        if self.maximum_provider_cost_cny is not None:
            maximum_cost = _optional_decimal(self.maximum_provider_cost_cny)
            if maximum_cost is None or not maximum_cost.is_finite() or maximum_cost <= 0:
                raise ProviderPoolShadowControllerError(
                    "provider_shadow_controller_maximum_provider_cost_cny_invalid"
                )
            object.__setattr__(
                self,
                "maximum_provider_cost_cny",
                maximum_cost,
            )


@dataclass(frozen=True)
class ActivePoolProjection:
    routing_generation: int
    cost_generation: int
    desired_size: int
    ordered_channel_ids: tuple[int, ...]
    standby_channel_ids: tuple[int, ...]
    slots: tuple[ProviderSlot, ...]
    cost_input_hash: str
    billing_floor_multiplier: Decimal
    realized_cost_p95_multiplier: Decimal | None = None
    observed_billing_floor_multiplier: Decimal | None = None
    observed_at: datetime | None = None

    @property
    def assignments(self) -> Mapping[int, str | int | None]:
        return MappingProxyType(
            {slot.slot_no: slot.assignment_id for slot in self.slots}
        )


@dataclass(frozen=True)
class PlannedPoolProjection:
    routing_generation: int
    cost_generation: int
    parent_routing_generation: int | None
    parent_cost_generation: int | None
    desired_size: int
    ordered_channel_ids: tuple[int, ...]
    standby_channel_ids: tuple[int, ...]
    cost_input_hash: str
    cost_output_hash: str
    config_version: str
    source_hash: str
    generation_source: str
    generation_state: str = "planned"
    pricing_state: str = "planned"
    cost_plan: CostPlan | None = None


@dataclass(frozen=True)
class ProviderPoolSizePlanningEvidence:
    observation_started_at: datetime
    last_shrink_at: datetime | None = None
    p99_concurrency: int = 0
    p99_simultaneous_failed_slots: int = 0
    providers_needed_for_24h_liquidity_runway: int = 0
    liquidity_runway_evidence_complete: bool = False
    liquidity_runway_capacity_sufficient: bool = False

    def __post_init__(self) -> None:
        observation_start = _aware(
            self.observation_started_at,
            "provider_shadow_controller_observation_start_invalid",
        )
        object.__setattr__(self, "observation_started_at", observation_start)
        if self.last_shrink_at is not None:
            previous_shrink = _aware(
                self.last_shrink_at,
                "provider_shadow_controller_last_shrink_invalid",
            )
            if previous_shrink < observation_start:
                raise ProviderPoolShadowControllerError(
                    "provider_shadow_controller_last_shrink_invalid"
                )
            object.__setattr__(self, "last_shrink_at", previous_shrink)
        for name in (
            "p99_concurrency",
            "p99_simultaneous_failed_slots",
            "providers_needed_for_24h_liquidity_runway",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ProviderPoolShadowControllerError(
                    f"provider_shadow_controller_{name}_invalid"
                )
        if not isinstance(self.liquidity_runway_evidence_complete, bool) or not isinstance(
            self.liquidity_runway_capacity_sufficient, bool
        ):
            raise ProviderPoolShadowControllerError(
                "provider_shadow_controller_liquidity_runway_evidence_invalid"
            )
        if (
            self.liquidity_runway_capacity_sufficient
            and not self.liquidity_runway_evidence_complete
        ):
            raise ProviderPoolShadowControllerError(
                "provider_shadow_controller_liquidity_runway_evidence_invalid"
            )


@dataclass(frozen=True)
class ProviderPoolPlanningEvidence:
    snapshot: ObserverSnapshot
    source_hash: str
    inventory: tuple[InventoryEntry, ...]
    owned_supplier_ids: frozenset[int] = frozenset()
    owned_channel_ids: frozenset[int] = frozenset()
    owned_listing_ids: frozenset[int] = frozenset()
    owned_ids: frozenset[int] = frozenset()
    quarantines: Mapping[int, datetime] | None = None
    active: ActivePoolProjection | None = None
    latest_planned: PlannedPoolProjection | None = None
    maximum_routing_generation: int = 0
    maximum_cost_generation: int = 0
    realized_cost_sample_count: int = 0
    realized_cost_p95_multiplier: Decimal | None = None
    pool_size: ProviderPoolSizePlanningEvidence | None = None
    slot_hints: Mapping[int, ProviderSlot] | None = None

    def __post_init__(self) -> None:
        if not _sha256(self.source_hash):
            raise ProviderPoolShadowControllerError(
                "provider_shadow_controller_source_hash_invalid"
            )
        if min(self.maximum_routing_generation, self.maximum_cost_generation) < 0:
            raise ProviderPoolShadowControllerError(
                "provider_shadow_controller_generation_cursor_invalid"
            )
        if (
            isinstance(self.realized_cost_sample_count, bool)
            or not isinstance(self.realized_cost_sample_count, int)
            or self.realized_cost_sample_count < 0
        ):
            raise ProviderPoolShadowControllerError(
                "provider_shadow_controller_realized_cost_sample_count_invalid"
            )
        realized = _optional_decimal(self.realized_cost_p95_multiplier)
        if realized is not None and (
            not realized.is_finite()
            or realized < 0
            or self.realized_cost_sample_count < 1
        ):
            raise ProviderPoolShadowControllerError(
                "provider_shadow_controller_realized_cost_invalid"
            )
        object.__setattr__(self, "realized_cost_p95_multiplier", realized)
        pool_size = self.pool_size or ProviderPoolSizePlanningEvidence(
            observation_started_at=self.snapshot.observed_at,
        )
        if not isinstance(pool_size, ProviderPoolSizePlanningEvidence):
            raise ProviderPoolShadowControllerError(
                "provider_shadow_controller_pool_size_evidence_invalid"
            )
        object.__setattr__(self, "pool_size", pool_size)
        for name in (
            "owned_supplier_ids",
            "owned_channel_ids",
            "owned_listing_ids",
            "owned_ids",
        ):
            values = frozenset(int(value) for value in getattr(self, name))
            if any(value < 1 for value in values):
                raise ProviderPoolShadowControllerError(
                    "provider_shadow_controller_owned_resource_id_invalid"
                )
            object.__setattr__(self, name, values)
        object.__setattr__(
            self,
            "quarantines",
            MappingProxyType(dict(self.quarantines or {})),
        )
        hints: dict[int, ProviderSlot] = {}
        for raw_slot_no, slot in dict(self.slot_hints or {}).items():
            if (
                isinstance(raw_slot_no, bool)
                or not isinstance(raw_slot_no, int)
                or raw_slot_no < 1
                or not isinstance(slot, ProviderSlot)
                or slot.slot_no != raw_slot_no
                or slot.slot_state is not SlotState.READY
                or raw_slot_no in hints
            ):
                raise ProviderPoolShadowControllerError(
                    "provider_shadow_controller_slot_hint_invalid"
                )
            hints[raw_slot_no] = slot
        object.__setattr__(self, "slot_hints", MappingProxyType(hints))


class ProviderPoolPlanningEvidenceSource(Protocol):
    def load(
        self,
        model: str,
        *,
        now: datetime,
        observer_freshness_seconds: int,
        inventory_freshness_seconds: int,
        config_version: str,
        size_mode: str = "all_funded",
        realized_cost_window_hours: int = 24,
        realized_cost_match_tolerance_seconds: int = 5,
    ) -> ProviderPoolPlanningEvidence: ...


class PostgresProviderPoolPlanStore:
    """Narrow wrapper that exposes no applying/confirmation/activation methods."""

    def __init__(self, connection_factory: Callable[[], Any]) -> None:
        self._store = PostgresProviderPoolStore(connection_factory)

    def plan(
        self,
        bundle: ProviderBundle,
        *,
        expected_routing_generation: int | None,
        expected_cost_generation: int | None,
    ) -> object:
        return self._store.plan(
            bundle,
            expected_routing_generation=expected_routing_generation,
            expected_cost_generation=expected_cost_generation,
        )


@dataclass(frozen=True)
class ProviderPoolShadowControllerReport:
    shadow: ProviderPoolShadowReport
    bundle: ProviderBundle
    snapshot_sequence: int | None
    snapshot_candidate_count: int
    eligible_count: int
    top_count: int
    standby_count: int
    vacant_count: int
    excluded: tuple[tuple[int, str], ...]
    reused_planned: bool = False
    realized_cost_sample_count: int = 0
    realized_cost_p95_multiplier: Decimal | None = None
    shadow_evidence: Mapping[str, Any] | None = None
    pool_size_evidence: Mapping[str, Any] | None = None

    def summary(self) -> dict[str, Any]:
        return {
            **self.shadow.summary(),
            "snapshot_sequence": self.snapshot_sequence,
            "snapshot_candidate_count": self.snapshot_candidate_count,
            "eligible_count": self.eligible_count,
            "top_count": self.top_count,
            "standby_count": self.standby_count,
            "vacant_count": self.vacant_count,
            "desired_size": self.bundle.desired_size,
            "excluded_count": len(self.excluded),
            "reused_planned": self.reused_planned,
            "realized_cost_sample_count": self.realized_cost_sample_count,
            "realized_cost_p95_multiplier": (
                None
                if self.realized_cost_p95_multiplier is None
                else format(self.realized_cost_p95_multiplier, "f")
            ),
            "plan_write_count": 1 if self.shadow.persisted else 0,
            "shadow_evidence": dict(self.shadow_evidence or {}),
            "pool_size_evidence": dict(self.pool_size_evidence or {}),
        }


class SingleModelProviderPoolShadowController:
    """Plan one configured model without exposing remote/apply capabilities."""

    def __init__(
        self,
        source: ProviderPoolPlanningEvidenceSource,
        config: ProviderPoolShadowControllerConfig,
        *,
        store: ProviderPoolShadowStore | None = None,
    ) -> None:
        self.source = source
        self.config = config
        self.store = store

    def run(
        self,
        *,
        now: datetime,
        persist: bool = False,
    ) -> ProviderPoolShadowControllerReport:
        now = _aware(now, "provider_shadow_controller_now_invalid")
        if not isinstance(persist, bool):
            raise ProviderPoolShadowControllerError(
                "provider_shadow_controller_persist_invalid"
            )
        evidence = self.source.load(
            self.config.model,
            now=now,
            observer_freshness_seconds=self.config.observer_freshness_seconds,
            inventory_freshness_seconds=self.config.inventory_freshness_seconds,
            config_version=self.config.config_version,
            size_mode=self.config.size_mode,
            realized_cost_window_hours=self.config.realized_cost_window_hours,
            realized_cost_match_tolerance_seconds=(
                self.config.realized_cost_match_tolerance_seconds
            ),
        )
        resume_applying = _resumes_applying_generation(
            evidence.latest_planned,
            config=self.config,
        )
        accepted = accept_snapshot(
            evidence.snapshot,
            now=evidence.snapshot.observed_at if resume_applying else now,
            freshness_seconds=self.config.observer_freshness_seconds,
            minimum_health_observers=self.config.minimum_health_observers,
            current_observed_at=(
                None
                if resume_applying or evidence.active is None
                else evidence.active.observed_at
            ),
            expected_model=self.config.model,
            expected_config_version=self.config.config_version,
        )
        desired, ranked, reused, pool_size_evidence = build_shadow_generation(
            replace(evidence, snapshot=accepted),
            config=self.config,
            now=now,
        )
        shadow = ProviderPoolShadowReconciler(self.store).reconcile(
            desired,
            persist=persist and not reused,
        )
        bundle = desired.bundle
        shadow_evidence = build_shadow_evidence(
            snapshot=accepted,
            source_hash=evidence.source_hash,
            ranked=ranked,
            owned_supplier_ids=evidence.owned_supplier_ids,
            owned_channel_ids=evidence.owned_channel_ids | evidence.owned_ids,
            owned_listing_ids=evidence.owned_listing_ids,
            bundle=bundle,
            previous_billing_floor=(
                Decimal("0")
                if evidence.active is None
                else evidence.active.billing_floor_multiplier
            ),
            conservative_fallback_cost=self.config.conservative_fallback_cost,
            retry_reserve_window=self.config.retry_reserve_window,
            realized_cost_window=(
                f"{self.config.realized_cost_window}:"
                f"{self.config.realized_cost_window_hours}h"
            ),
            quantization_version=self.config.quantization_version,
            raw_realized_cost_sample_count=evidence.realized_cost_sample_count,
            raw_realized_cost_p95_multiplier=evidence.realized_cost_p95_multiplier,
            minimum_realized_cost_samples=self.config.minimum_cost_samples,
        )
        return ProviderPoolShadowControllerReport(
            shadow=shadow,
            bundle=bundle,
            snapshot_sequence=accepted.sequence,
            snapshot_candidate_count=len(accepted.candidates),
            eligible_count=len(ranked.eligible),
            top_count=len(ranked.top),
            standby_count=len(ranked.standby),
            vacant_count=sum(
                slot.slot_state is SlotState.VACANT for slot in desired.bundle.slots
            ),
            excluded=ranked.excluded,
            reused_planned=reused,
            realized_cost_sample_count=evidence.realized_cost_sample_count,
            realized_cost_p95_multiplier=evidence.realized_cost_p95_multiplier,
            shadow_evidence=shadow_evidence,
            pool_size_evidence=pool_size_evidence,
        )


def _resumes_applying_generation(
    latest: PlannedPoolProjection | None,
    *,
    config: ProviderPoolShadowControllerConfig,
) -> bool:
    return bool(
        latest is not None
        and latest.generation_state == "applying"
        and latest.pricing_state in {"applying", "confirmed"}
        and latest.config_version == config.config_version
        and latest.generation_source == config.generation_source
    )


def build_shadow_generation(
    evidence: ProviderPoolPlanningEvidence,
    *,
    config: ProviderPoolShadowControllerConfig,
    now: datetime,
) -> tuple[ProviderPoolShadowInput, RankedPool, bool, Mapping[str, Any]]:
    """Convert accepted snapshot/inventory evidence into an immutable shadow bundle."""

    now = _aware(now, "provider_shadow_controller_now_invalid")
    if evidence.snapshot.model != config.model:
        raise ProviderPoolShadowControllerError(
            "provider_shadow_controller_model_mismatch"
        )
    qualified = _qualified_inventory(evidence.inventory, model=config.model)
    active = evidence.active
    if config.size_mode == "evidence_based" and evidence.latest_planned is not None:
        retained_size = evidence.latest_planned.desired_size
    else:
        retained_size = max(
            0 if active is None else active.desired_size,
            0 if evidence.latest_planned is None else evidence.latest_planned.desired_size,
        )
    current_desired_size = max(1, retained_size)
    size_evidence = evidence.pool_size
    if size_evidence is None:  # pragma: no cover - normalized by the evidence contract
        raise ProviderPoolShadowControllerError(
            "provider_shadow_controller_pool_size_evidence_invalid"
        )
    providers_needed = size_evidence.providers_needed_for_24h_liquidity_runway
    if (
        not size_evidence.liquidity_runway_evidence_complete
        or not size_evidence.liquidity_runway_capacity_sufficient
    ):
        providers_needed = max(providers_needed, current_desired_size)
    minimum_observation = timedelta(days=config.minimum_pool_observation_days)
    minimum_shrink_interval = timedelta(
        hours=config.minimum_pool_shrink_interval_hours
    )
    desired_size = next_desired_pool_size(
        size_mode=config.size_mode,
        current_desired_size=current_desired_size,
        funded_eligible_provider_count=len(qualified),
        now=now,
        observation_started_at=size_evidence.observation_started_at,
        last_shrink_at=size_evidence.last_shrink_at,
        p99_concurrency=size_evidence.p99_concurrency,
        safe_concurrency_per_slot=config.safe_concurrency_per_slot,
        target_utilization=config.target_utilization,
        p99_simultaneous_failed_slots=(
            size_evidence.p99_simultaneous_failed_slots
        ),
        providers_needed_for_24h_liquidity_runway=providers_needed,
        minimum_active_slots=config.minimum_active_slots,
        initial_max_active_slots=config.initial_max_active_slots,
        minimum_observation=minimum_observation,
        minimum_shrink_interval=minimum_shrink_interval,
    )
    pool_size_evidence = build_pool_size_evidence(
        size_mode=config.size_mode,
        current_desired_size=current_desired_size,
        funded_eligible_provider_count=len(qualified),
        decision_at=now,
        observation_started_at=size_evidence.observation_started_at,
        last_shrink_at=size_evidence.last_shrink_at,
        p99_concurrency=size_evidence.p99_concurrency,
        safe_concurrency_per_slot=config.safe_concurrency_per_slot,
        target_utilization=config.target_utilization,
        p99_simultaneous_failed_slots=(
            size_evidence.p99_simultaneous_failed_slots
        ),
        providers_needed_for_24h_liquidity_runway=providers_needed,
        liquidity_runway_evidence_complete=(
            size_evidence.liquidity_runway_evidence_complete
        ),
        liquidity_runway_capacity_sufficient=(
            size_evidence.liquidity_runway_capacity_sufficient
        ),
        minimum_active_slots=config.minimum_active_slots,
        initial_max_active_slots=config.initial_max_active_slots,
        minimum_observation=minimum_observation,
        minimum_shrink_interval=minimum_shrink_interval,
        desired_size=desired_size,
    )
    if config.priority_offset + 2 * desired_size > 32767:
        raise ProviderPoolShadowControllerError(
            "provider_shadow_controller_priority_range_invalid"
        )

    ranked = rank_candidates(
        evidence.snapshot.candidates,
        model=config.model,
        owned_ids=evidence.owned_ids,
        owned_supplier_ids=evidence.owned_supplier_ids,
        owned_channel_ids=evidence.owned_channel_ids,
        owned_listing_ids=evidence.owned_listing_ids,
        quarantines=evidence.quarantines or {},
        now=now,
        n=desired_size,
        standby_count=config.standby_count,
        minimum_health_observers=config.minimum_health_observers,
    )
    ranked = _enforce_provider_cost_cap(
        ranked,
        config=config,
        desired_size=desired_size,
    )
    ranked = _retain_unpublished_generation_candidates(
        ranked,
        evidence.latest_planned,
        desired_size=desired_size,
    )
    ranked = _retain_verified_slot_candidates(
        ranked,
        evidence.slot_hints or {},
        desired_size=desired_size,
    )
    latest = evidence.latest_planned
    applying_cost_plan = _applying_cost_plan(
        latest,
        ranked,
        desired_size=desired_size,
    )
    current_slots = _slots_with_planning_hints(
        () if active is None else active.slots,
        evidence.slot_hints or {},
    )
    channel_plan = assign_channels_to_stable_slots(
        current_slots,
        tuple(row.channel_id for row in ranked.top),
        desired_size=desired_size,
        priority_offset=config.priority_offset,
    )
    account_plan = _assign_inventory_to_slots(
        current_slots,
        qualified,
        desired_size=desired_size,
    )
    by_channel = {row.channel_id: row for row in ranked.top}
    desired_slot_costs: dict[int, Decimal] = {}
    slots: list[ProviderSlot] = []
    assignments: dict[int, int | None] = {}
    ready_slot_nos: list[int] = []
    for slot_no in range(1, desired_size + 1):
        channel_id, rank, priority = channel_plan[slot_no]
        account = account_plan.get(slot_no)
        if channel_id is None or account is None:
            assignments[slot_no] = None
            slots.append(_vacant_slot(slot_no))
            continue
        row = by_channel[channel_id]
        observed_slot_cost = normalize_slot_cost(
            effective_price(row),
            exchange_factor=config.exchange_factor,
            fee_factor=config.fee_factor,
            quantum=config.quantum,
        )
        slot_cost = observed_slot_cost
        if applying_cost_plan is not None:
            persisted_slot_cost = applying_cost_plan.desired_slot_costs.get(slot_no)
            if persisted_slot_cost is None or persisted_slot_cost < observed_slot_cost:
                raise ProviderPoolShadowControllerError(
                    "provider_shadow_controller_applying_cost_no_longer_covers"
                )
            slot_cost = persisted_slot_cost
        desired_slot_costs[slot_no] = slot_cost
        assignments[slot_no] = account.account_id
        ready_slot_nos.append(slot_no)
        slots.append(
            ProviderSlot(
                slot_no=slot_no,
                channel_id=channel_id,
                rank=rank,
                priority=priority,
                assignment_id=account.account_id,
                enodot_binding_id=account.binding_id,
                token_id=account.token_id,
                account_id=account.account_id,
                slot_state=SlotState.READY,
                funding_eligible=True,
                desired_schedulable=True,
                observed_schedulable=True,
                desired_cost_multiplier=slot_cost,
                observed_cost_multiplier=slot_cost,
                pin_confirmed=True,
                probe_confirmed=True,
            )
        )

    if applying_cost_plan is not None and set(
        applying_cost_plan.desired_slot_costs
    ) != set(ready_slot_nos):
        raise ProviderPoolShadowControllerError(
            "provider_shadow_controller_apply_in_progress_replan_required"
        )

    retry_value = (
        config.bootstrap_retry_reserve
        if config.retry_reserve_multiplier is None
        else config.retry_reserve_multiplier
    )
    realized_cost_value = _realized_cost_value(evidence, config=config)
    realized_cost_window = (
        f"{config.realized_cost_window}:{config.realized_cost_window_hours}h"
    )
    if applying_cost_plan is not None and latest is not None:
        semantic_cost_hash = latest.cost_input_hash
        cost_plan = applying_cost_plan
    else:
        semantic_cost_hash = cost_input_hash(
            model=config.model,
            ordered_channels=ranked.top,
            assignments=assignments,
            ready_slot_nos=ready_slot_nos,
            token_mix_version=config.token_mix_version,
            exchange_rate_version=config.exchange_rate_version,
            retry_reserve_window=config.retry_reserve_window,
            retry_reserve_value=retry_value,
            realized_cost_window=realized_cost_window,
            realized_cost_p95_value=realized_cost_value,
            margin_bps=config.margin_bps,
            headroom_bps=config.headroom_bps,
            quantization_version=config.quantization_version,
        )
        cost_plan = build_cost_plan(
            desired_slot_costs,
            retry_reserve_multiplier=config.retry_reserve_multiplier,
            bootstrap_retry_reserve=config.bootstrap_retry_reserve,
            realized_cost_p95_multiplier=realized_cost_value,
            margin_bps=config.margin_bps,
            headroom_bps=config.headroom_bps,
            previous_billing_floor=(
                Decimal("0") if active is None else active.billing_floor_multiplier
            ),
            conservative_fallback_cost=config.conservative_fallback_cost,
            quantum=config.quantum,
        )
    ordered_ids = tuple(row.channel_id for row in ranked.top)
    standby_ids = tuple(row.channel_id for row in ranked.standby)
    parent_routing = None if active is None else active.routing_generation
    parent_cost = None if active is None else active.cost_generation
    reused = bool(
        latest is not None
        and latest.parent_routing_generation == parent_routing
        and latest.parent_cost_generation == parent_cost
        and latest.desired_size == desired_size
        and latest.ordered_channel_ids == ordered_ids
        and latest.standby_channel_ids == standby_ids
        and latest.cost_input_hash == semantic_cost_hash
        and latest.cost_output_hash == cost_plan.cost_output_hash
        and latest.config_version == config.config_version
        and latest.generation_source == config.generation_source
    )
    if (
        latest is not None
        and (latest.generation_state != "planned" or latest.pricing_state != "planned")
        and not reused
    ):
        raise ProviderPoolShadowControllerError(
            "provider_shadow_controller_apply_in_progress_replan_required"
        )
    routing_generation = (
        latest.routing_generation
        if reused and latest is not None
        else evidence.maximum_routing_generation + 1
    )
    cost_generation = (
        latest.cost_generation
        if reused and latest is not None
        else evidence.maximum_cost_generation + 1
    )
    bundle = ProviderBundle(
        model=config.model,
        routing_generation=routing_generation,
        parent_routing_generation=parent_routing,
        cost_generation=cost_generation,
        parent_cost_generation=parent_cost,
        generation_source=config.generation_source,
        source_hash=(
            latest.source_hash
            if reused and latest is not None
            else evidence.source_hash
        ),
        ordered_channel_ids=ordered_ids,
        standby_channel_ids=standby_ids,
        desired_size=desired_size,
        slots=tuple(slots),
        cost_plan=cost_plan,
        cost_input_hash=semantic_cost_hash,
        token_mix_version=config.token_mix_version,
        exchange_rate_version=config.exchange_rate_version,
        config_version=config.config_version,
    )
    return (
        ProviderPoolShadowInput(
            bundle=bundle,
            current_routing=() if active is None else active.ordered_channel_ids,
            current_assignments={} if active is None else active.assignments,
            current_cost_input_hash=("0" * 64 if active is None else active.cost_input_hash),
        ),
        ranked,
        reused,
        pool_size_evidence,
    )


def _enforce_provider_cost_cap(
    ranked: RankedPool,
    *,
    config: ProviderPoolShadowControllerConfig,
    desired_size: int,
) -> RankedPool:
    maximum_cost = config.maximum_provider_cost_cny
    if maximum_cost is None:
        return ranked

    eligible: list[Candidate] = []
    excluded = list(ranked.excluded)
    for row in ranked.eligible:
        try:
            slot_cost_cny = normalize_slot_cost(
                effective_price_cny(row),
                fee_factor=config.fee_factor,
                quantum=config.quantum,
            )
        except ProviderPoolRankError:
            excluded.append((row.channel_id, "cost_cap_unverifiable"))
            continue
        if slot_cost_cny > maximum_cost:
            excluded.append((row.channel_id, "cost_cap_exceeded"))
            continue
        eligible.append(row)

    standby_end = desired_size + config.standby_count
    return RankedPool(
        top=tuple(eligible[:desired_size]),
        standby=tuple(eligible[desired_size:standby_end]),
        eligible=tuple(eligible),
        excluded=tuple(sorted(excluded)),
    )


def _slots_with_planning_hints(
    active_slots: Sequence[ProviderSlot],
    slot_hints: Mapping[int, ProviderSlot],
) -> tuple[ProviderSlot, ...]:
    by_slot = {slot.slot_no: slot for slot in active_slots}
    by_slot.update(slot_hints)
    return tuple(by_slot[slot_no] for slot_no in sorted(by_slot))


def _retain_unpublished_generation_candidates(
    ranked: RankedPool,
    latest: PlannedPoolProjection | None,
    *,
    desired_size: int,
) -> RankedPool:
    if (
        latest is None
        or latest.generation_state not in {"planned", "applying"}
        or latest.pricing_state not in {"planned", "applying", "confirmed"}
        or latest.desired_size != desired_size
    ):
        return ranked
    by_channel = {row.channel_id: row for row in ranked.eligible}
    required = latest.ordered_channel_ids + latest.standby_channel_ids
    if len(required) != len(set(required)) or any(
        channel_id not in by_channel for channel_id in required
    ):
        return ranked
    top = tuple(by_channel[channel_id] for channel_id in latest.ordered_channel_ids)
    standby = tuple(by_channel[channel_id] for channel_id in latest.standby_channel_ids)
    return RankedPool(
        top=top,
        standby=standby,
        eligible=ranked.eligible,
        excluded=ranked.excluded,
    )


def _retain_verified_slot_candidates(
    ranked: RankedPool,
    slot_hints: Mapping[int, ProviderSlot],
    *,
    desired_size: int,
) -> RankedPool:
    if not slot_hints:
        return ranked
    retained_channel_ids = {
        int(slot.channel_id)
        for slot_no, slot in slot_hints.items()
        if slot_no <= desired_size and slot.channel_id is not None
    }
    if len(retained_channel_ids) != len(
        [slot for slot_no, slot in slot_hints.items() if slot_no <= desired_size]
    ):
        raise ProviderPoolShadowControllerError(
            "provider_shadow_controller_slot_hint_channel_invalid"
        )
    eligible_channel_ids = {row.channel_id for row in ranked.eligible}
    if not retained_channel_ids.issubset(eligible_channel_ids):
        raise ProviderPoolShadowControllerError(
            "provider_shadow_controller_slot_hint_channel_ineligible"
        )
    selected_channel_ids = set(retained_channel_ids)
    for row in ranked.eligible:
        if len(selected_channel_ids) >= desired_size:
            break
        selected_channel_ids.add(row.channel_id)
    top = tuple(
        row for row in ranked.eligible if row.channel_id in selected_channel_ids
    )
    standby = tuple(
        row for row in ranked.eligible if row.channel_id not in selected_channel_ids
    )[: len(ranked.standby)]
    return RankedPool(
        top=top,
        standby=standby,
        eligible=ranked.eligible,
        excluded=ranked.excluded,
    )


def _applying_cost_plan(
    latest: PlannedPoolProjection | None,
    ranked: RankedPool,
    *,
    desired_size: int,
) -> CostPlan | None:
    if (
        latest is None
        or latest.generation_state != "applying"
        or latest.pricing_state not in {"applying", "confirmed"}
        or latest.desired_size != desired_size
        or latest.ordered_channel_ids
        != tuple(row.channel_id for row in ranked.top)
        or latest.standby_channel_ids
        != tuple(row.channel_id for row in ranked.standby)
    ):
        return None
    return latest.cost_plan


def _realized_cost_value(
    evidence: ProviderPoolPlanningEvidence,
    *,
    config: ProviderPoolShadowControllerConfig,
) -> Decimal | None:
    if config.realized_cost_p95_multiplier is not None:
        return config.realized_cost_p95_multiplier
    if evidence.realized_cost_sample_count >= config.minimum_cost_samples:
        if evidence.realized_cost_p95_multiplier is None:
            raise ProviderPoolShadowControllerError(
                "provider_shadow_controller_realized_cost_evidence_incomplete"
            )
        return evidence.realized_cost_p95_multiplier
    if evidence.active is not None:
        return evidence.active.realized_cost_p95_multiplier
    return None


class PostgresProviderPoolPlanningAdapter:
    """Read all planning evidence for one model from one PostgreSQL snapshot."""

    def __init__(self, connection_factory: Callable[[], Any]) -> None:
        if not callable(connection_factory):
            raise ProviderPoolShadowControllerError(
                "provider_shadow_controller_connection_factory_invalid"
            )
        self.connection_factory = connection_factory

    def load(
        self,
        model: str,
        *,
        now: datetime,
        observer_freshness_seconds: int,
        inventory_freshness_seconds: int,
        config_version: str,
        size_mode: str = "all_funded",
        realized_cost_window_hours: int = 24,
        realized_cost_match_tolerance_seconds: int = 5,
    ) -> ProviderPoolPlanningEvidence:
        now = _aware(now, "provider_shadow_controller_now_invalid")
        sizing_mode = str(size_mode or "").strip().casefold()
        if sizing_mode not in {"all_funded", "evidence_based"}:
            raise ProviderPoolShadowControllerError(
                "provider_shadow_controller_size_mode_invalid"
            )
        for name, value in (
            ("realized_cost_window_hours", realized_cost_window_hours),
            (
                "realized_cost_match_tolerance_seconds",
                realized_cost_match_tolerance_seconds,
            ),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ProviderPoolShadowControllerError(
                    f"provider_shadow_controller_{name}_invalid"
                )
        observer_cutoff = now - timedelta(seconds=observer_freshness_seconds)
        inventory_cutoff = now - timedelta(seconds=inventory_freshness_seconds)
        realized_cost_cutoff = now - timedelta(hours=realized_cost_window_hours)
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                cursor.execute(_LATEST_SCAN_SQL, (str(model),))
                scan = _one(cursor)
                if scan is None:
                    raise ProviderPoolShadowControllerError(
                        "provider_shadow_controller_snapshot_missing"
                    )
                cursor.execute(
                    _SNAPSHOT_CANDIDATE_COUNT_SQL,
                    (observer_cutoff, str(model), int(scan["id"])),
                )
                count_row = _one(cursor) or {}
                cursor.execute(
                    _SNAPSHOT_CANDIDATES_SQL,
                    (observer_cutoff, str(model), int(scan["id"])),
                )
                candidate_rows = _many(cursor)
                cursor.execute(_OWNED_RESOURCE_INVENTORY_SQL)
                owned_resource_rows = _many(cursor)
                inventory_sql = (
                    _INVENTORY_EVIDENCE_SQL
                    if sizing_mode == "evidence_based"
                    else _INVENTORY_SQL
                )
                cursor.execute(inventory_sql, (str(model),))
                inventory_rows = _many(cursor)
                cursor.execute(_QUARANTINES_SQL, (str(model), now))
                quarantine_rows = _many(cursor)
                cursor.execute(_ACTIVE_SQL, (str(model),))
                active_row = _one(cursor)
                slot_rows: list[dict[str, Any]] = []
                if active_row is not None:
                    cursor.execute(_ACTIVE_SLOTS_SQL, (str(model),))
                    slot_rows = _many(cursor)
                parent_routing = (
                    None if active_row is None else int(active_row["routing_generation"])
                )
                cursor.execute(_LATEST_PLANNED_SQL, (str(model), parent_routing))
                planned_row = _one(cursor)
                cursor.execute(_GENERATION_CURSOR_SQL, (str(model), str(model)))
                generation_row = _one(cursor) or {}
                pool_size_row: dict[str, Any] = {}
                if sizing_mode == "evidence_based":
                    cursor.execute(
                        _POOL_SIZE_EVIDENCE_SQL,
                        (str(model), now, inventory_cutoff),
                    )
                    pool_size_row = _one(cursor) or {}
                cursor.execute(_USAGE_LOGS_READABLE_SQL)
                usage_source = _one(cursor) or {}
                if usage_source.get("readable") is not True:
                    raise ProviderPoolShadowControllerError(
                        "provider_shadow_controller_realized_cost_source_unavailable"
                    )
                cursor.execute(
                    _REALIZED_COST_SQL,
                    (
                        realized_cost_cutoff,
                        realized_cost_match_tolerance_seconds,
                        str(model),
                    ),
                )
                realized_cost_row = _one(cursor) or {}
            connection.commit()
        except Exception:
            rollback = getattr(connection, "rollback", None)
            if callable(rollback):
                rollback()
            raise
        finally:
            close = getattr(connection, "close", None)
            if callable(close):
                close()

        snapshot, signal_owned_channel_ids, source_hash = _snapshot_from_rows(
            scan,
            candidate_rows,
            model=str(model),
            config_version=config_version,
            raw_candidate_count=int(count_row.get("candidate_count") or 0),
            raw_health_observer_count=int(
                count_row.get("health_observer_count") or 0
            ),
        )
        owned_resource_ids = _owned_resource_ids(owned_resource_rows)
        inventory = tuple(
            _inventory_from_row(row, model=str(model), freshness_cutoff=inventory_cutoff)
            for row in inventory_rows
        )
        quarantines = {
            int(row["channel_id"]): (
                datetime.max.replace(tzinfo=timezone.utc)
                if row.get("ends_at") is None
                else _aware(
                    row["ends_at"],
                    "provider_shadow_controller_quarantine_time_invalid",
                )
            )
            for row in quarantine_rows
        }
        active = (
            None
            if active_row is None
            else _active_from_rows(active_row, slot_rows)
        )
        planned = None if planned_row is None else _planned_from_row(planned_row)
        if sizing_mode == "evidence_based" and planned is not None:
            current_desired_size = max(1, planned.desired_size)
        else:
            current_desired_size = max(
                1,
                0 if active is None else active.desired_size,
                0 if planned is None else planned.desired_size,
            )
        qualified_count = len(_qualified_inventory(inventory, model=str(model)))
        liquidity_complete = bool(
            sizing_mode == "evidence_based"
            and pool_size_row.get("liquidity_runway_evidence_complete") is True
            and int(pool_size_row.get("liquidity_provider_count") or 0)
            == qualified_count
        )
        liquidity_sufficient = bool(
            liquidity_complete
            and pool_size_row.get("liquidity_runway_capacity_sufficient") is True
        )
        providers_needed = int(
            pool_size_row.get("providers_needed_for_24h_liquidity_runway") or 0
        )
        if not liquidity_complete or not liquidity_sufficient:
            providers_needed = max(current_desired_size, providers_needed)
        return ProviderPoolPlanningEvidence(
            snapshot=snapshot,
            source_hash=source_hash,
            inventory=inventory,
            owned_supplier_ids=owned_resource_ids["supplier"],
            owned_channel_ids=(
                owned_resource_ids["channel"] | frozenset(signal_owned_channel_ids)
            ),
            owned_listing_ids=owned_resource_ids["listing"],
            quarantines=quarantines,
            active=active,
            latest_planned=planned,
            maximum_routing_generation=int(
                generation_row.get("maximum_routing_generation") or 0
            ),
            maximum_cost_generation=int(
                generation_row.get("maximum_cost_generation") or 0
            ),
            realized_cost_sample_count=int(
                realized_cost_row.get("sample_count") or 0
            ),
            realized_cost_p95_multiplier=_optional_decimal(
                realized_cost_row.get("realized_cost_p95_multiplier")
            ),
            pool_size=ProviderPoolSizePlanningEvidence(
                observation_started_at=_aware(
                    pool_size_row.get("observation_started_at") or now,
                    "provider_shadow_controller_observation_start_invalid",
                ),
                last_shrink_at=(
                    None
                    if pool_size_row.get("last_shrink_at") is None
                    else _aware(
                        pool_size_row["last_shrink_at"],
                        "provider_shadow_controller_last_shrink_invalid",
                    )
                ),
                p99_concurrency=int(pool_size_row.get("p99_concurrency") or 0),
                p99_simultaneous_failed_slots=int(
                    pool_size_row.get("p99_simultaneous_failed_slots") or 0
                ),
                providers_needed_for_24h_liquidity_runway=providers_needed,
                liquidity_runway_evidence_complete=liquidity_complete,
                liquidity_runway_capacity_sufficient=liquidity_sufficient,
            ),
        )


def _qualified_inventory(
    inventory: Sequence[InventoryEntry],
    *,
    model: str,
) -> tuple[InventoryEntry, ...]:
    seen_merchants: set[int] = set()
    seen_accounts: set[int] = set()
    seen_tokens: set[int] = set()
    qualified: list[InventoryEntry] = []
    for item in inventory:
        if item.model != model:
            raise ProviderPoolShadowControllerError(
                "provider_shadow_controller_inventory_model_mismatch"
            )
        if (
            item.merchant_id in seen_merchants
            or item.account_id in seen_accounts
            or item.token_id in seen_tokens
        ):
            raise ProviderPoolShadowControllerError(
                "provider_shadow_controller_inventory_identity_duplicate"
            )
        seen_merchants.add(item.merchant_id)
        seen_accounts.add(item.account_id)
        seen_tokens.add(item.token_id)
        if (
            item.funding_eligible
            and item.liquidity_state
            in {
                LiquidityState.FUNDED,
                LiquidityState.REFILLING,
            }
            and item.binding_ready
            and item.inventory_state
            not in {InventoryState.RETIRED, InventoryState.REPAIRING}
        ):
            qualified.append(item)
    return tuple(
        sorted(
            qualified,
            key=lambda item: (-item.health_score, item.merchant_id, item.account_id),
        )
    )


def _assign_inventory_to_slots(
    current_slots: Sequence[ProviderSlot],
    inventory: Sequence[InventoryEntry],
    *,
    desired_size: int,
) -> Mapping[int, InventoryEntry]:
    by_account = {item.account_id: item for item in inventory}
    assigned: dict[int, InventoryEntry] = {}
    used: set[int] = set()
    for slot in sorted(current_slots, key=lambda item: item.slot_no):
        if slot.slot_no > desired_size or slot.account_id not in by_account:
            continue
        item = by_account[int(slot.account_id)]
        assigned[slot.slot_no] = item
        used.add(item.account_id)
    by_requested_slot: dict[int, InventoryEntry] = {}
    for item in inventory:
        requested = item.assigned_slot_no
        if requested is None or requested > desired_size or item.account_id in used:
            continue
        if requested in by_requested_slot:
            raise ProviderPoolShadowControllerError(
                "provider_shadow_controller_inventory_slot_duplicate"
            )
        by_requested_slot[requested] = item
    for slot_no in range(1, desired_size + 1):
        item = by_requested_slot.get(slot_no)
        if slot_no not in assigned and item is not None:
            assigned[slot_no] = item
            used.add(item.account_id)
    remaining = [item for item in inventory if item.account_id not in used]
    for slot_no in range(1, desired_size + 1):
        if slot_no not in assigned and remaining:
            item = remaining.pop(0)
            assigned[slot_no] = item
            used.add(item.account_id)
    return MappingProxyType(assigned)


def _vacant_slot(slot_no: int) -> ProviderSlot:
    return ProviderSlot(
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


def _snapshot_from_rows(
    scan: Mapping[str, Any],
    rows: Sequence[Mapping[str, Any]],
    *,
    model: str,
    config_version: str,
    raw_candidate_count: int | None = None,
    raw_health_observer_count: int | None = None,
) -> tuple[ObserverSnapshot, set[int], str]:
    if str(scan.get("status") or "") != "completed" or scan.get("completed_at") is None:
        raise ProviderPoolShadowControllerError(
            "provider_shadow_controller_snapshot_incomplete"
        )
    source_hash = str(scan.get("result_hash") or "").casefold()
    if not _sha256(source_hash):
        raise ProviderPoolShadowControllerError(
            "provider_shadow_controller_snapshot_source_hash_invalid"
        )
    expected_count = int(scan.get("candidate_count") or 0)
    observed_raw_count = len(rows) if raw_candidate_count is None else raw_candidate_count
    if expected_count != observed_raw_count:
        raise ProviderPoolShadowControllerError(
            "provider_shadow_controller_snapshot_candidate_count_mismatch"
        )
    raw_payload = scan.get("result_payload")
    if raw_payload is None:
        raise ProviderPoolShadowControllerError(
            "provider_shadow_controller_snapshot_payload_missing"
        )
    if isinstance(raw_payload, str):
        try:
            raw_payload = json.loads(raw_payload)
        except (TypeError, ValueError):
            raise ProviderPoolShadowControllerError(
                "provider_shadow_controller_snapshot_payload_invalid"
            ) from None
    if not isinstance(raw_payload, list) or len(raw_payload) != expected_count:
        raise ProviderPoolShadowControllerError(
            "provider_shadow_controller_snapshot_payload_invalid"
        )
    payload_bytes = json.dumps(
        raw_payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    computed_source_hash = hashlib.sha256(payload_bytes).hexdigest()
    if not hmac.compare_digest(computed_source_hash, source_hash):
        raise ProviderPoolShadowControllerError(
            "provider_shadow_controller_snapshot_result_hash_mismatch"
        )
    if int(scan.get("page_count") or 0) < 1:
        raise ProviderPoolShadowControllerError(
            "provider_shadow_controller_snapshot_pages_incomplete"
        )
    candidates: list[Candidate] = []
    owned_channel_ids: set[int] = set()
    for row in rows:
        candidate = candidate_from_mapping(
            {
                "channel_id": row.get("channel_id"),
                "model": row.get("model"),
                "price_key": row.get("price_key"),
                "final_multiplier": row.get("final_multiplier"),
                "realtime_ratio_cny": row.get("realtime_ratio_cny"),
                "exchange_rate": row.get("exchange_rate"),
                "supplier_id": row.get("supplier_id"),
                "listing_id": row.get("listing_id"),
                "health_state": row.get("health_state"),
                "available": row.get("available"),
                "last_success_at": row.get("last_success_at"),
                "latency_ewma_ms": row.get("latency_ewma_ms"),
                "observer_health_count": row.get("observer_health_count"),
                "observer_count": row.get("observer_count"),
                "observed_at": row.get("observed_at"),
            }
        )
        candidates.append(candidate)
        if row.get("is_owned") is True:
            owned_channel_ids.add(candidate.channel_id)
    health_observer_count = max(
        (candidate.observer_count for candidate in candidates),
        default=0,
    )
    if raw_health_observer_count is not None:
        if raw_health_observer_count < 0:
            raise ProviderPoolShadowControllerError(
                "provider_shadow_controller_snapshot_observer_count_invalid"
            )
        health_observer_count = max(health_observer_count, raw_health_observer_count)
    snapshot = ObserverSnapshot(
        model=model,
        observed_at=_aware(
            scan["completed_at"],
            "provider_shadow_controller_snapshot_time_invalid",
        ),
        status="completed",
        candidates=tuple(candidates),
        result_hash="0" * 64,
        candidate_count=len(candidates),
        pages_complete=True,
        config_version=config_version,
        sequence=int(scan["id"]),
        health_observer_count=health_observer_count,
    )
    snapshot = replace(snapshot, result_hash=snapshot_hash(snapshot))
    return snapshot, owned_channel_ids, source_hash


def _owned_resource_ids(
    rows: Sequence[Mapping[str, Any]],
) -> dict[str, frozenset[int]]:
    values: dict[str, set[int]] = {
        "supplier": set(),
        "channel": set(),
        "listing": set(),
    }
    for row in rows:
        resource_type = str(row.get("resource_type") or "")
        if resource_type not in values:
            raise ProviderPoolShadowControllerError(
                "provider_shadow_controller_owned_resource_type_invalid"
            )
        resource_id = int(row.get("resource_id") or 0)
        if resource_id < 1:
            raise ProviderPoolShadowControllerError(
                "provider_shadow_controller_owned_resource_id_invalid"
            )
        values[resource_type].add(resource_id)
    return {key: frozenset(resource_ids) for key, resource_ids in values.items()}


def _inventory_from_row(
    row: Mapping[str, Any],
    *,
    model: str,
    freshness_cutoff: datetime,
) -> InventoryEntry:
    financial_time = row.get("last_financial_observed_at")
    financial_fresh = financial_time is not None and _aware(
        financial_time,
        "provider_shadow_controller_inventory_time_invalid",
    ) >= freshness_cutoff
    roles = {str(value) for value in (row.get("roles") or ())}
    token_healthy = str(row.get("token_status") or "").casefold() in {
        "1",
        "active",
        "enabled",
    }
    session_healthy = str(row.get("merchant_status") or "") == "supplier_submitted"
    binding_ready = (
        str(row.get("binding_state") or "") == "bound"
        and token_healthy
        and session_healthy
        and "provider" in roles
    )
    egress_healthy = row.get("egress_healthy") is not False
    recent_success = (
        None
        if row.get("recent_provider_success_at") is None
        else _aware(
            row["recent_provider_success_at"],
            "provider_shadow_controller_inventory_success_time_invalid",
        )
    )
    return InventoryEntry(
        model=model,
        merchant_id=int(row["merchant_id"]),
        binding_id=int(row["enodot_binding_id"]),
        account_id=int(row["enodot_account_id"]),
        token_id=int(row["a6_token_id"]),
        inventory_state=InventoryState(str(row["inventory_state"])),
        liquidity_state=LiquidityState(str(row["liquidity_state"])),
        funding_eligible=bool(row.get("funding_eligible"))
        and financial_fresh
        and binding_ready,
        binding_ready=binding_ready,
        schedulable=bool(row.get("schedulable")),
        assigned_slot_no=(
            None
            if row.get("assigned_slot_no") is None
            else int(row["assigned_slot_no"])
        ),
        health_score=sum((session_healthy, token_healthy, egress_healthy)),
        user_balance_runway_seconds=max(
            0, int(row.get("user_balance_runway_seconds") or 0)
        ),
        withdrawable_units=max(0, int(row.get("withdrawable_units") or 0)),
        session_healthy=session_healthy,
        token_healthy=token_healthy,
        egress_healthy=egress_healthy,
        recent_provider_success_at=recent_success,
    )


def _active_from_rows(
    row: Mapping[str, Any],
    slots: Sequence[Mapping[str, Any]],
) -> ActivePoolProjection:
    parsed_slots = tuple(_slot_from_row(item) for item in slots)
    desired_size = int(row["desired_size"])
    if len(parsed_slots) != desired_size:
        raise ProviderPoolShadowControllerError(
            "provider_shadow_controller_active_slot_count_mismatch"
        )
    return ActivePoolProjection(
        routing_generation=int(row["routing_generation"]),
        cost_generation=int(row["cost_generation"]),
        desired_size=desired_size,
        ordered_channel_ids=tuple(int(value) for value in row.get("ordered_channel_ids") or ()),
        standby_channel_ids=tuple(int(value) for value in row.get("standby_channel_ids") or ()),
        slots=parsed_slots,
        cost_input_hash=str(row["cost_input_hash"]),
        billing_floor_multiplier=Decimal(str(row["billing_floor_multiplier"])),
        realized_cost_p95_multiplier=_optional_decimal(
            row.get("realized_cost_p95_multiplier")
        ),
        observed_billing_floor_multiplier=(
            None
            if row.get("observed_billing_floor_multiplier") is None
            else Decimal(str(row["observed_billing_floor_multiplier"]))
        ),
        observed_at=(
            None
            if row.get("observed_at") is None
            else _aware(
                row["observed_at"],
                "provider_shadow_controller_active_time_invalid",
            )
        ),
    )


def _slot_from_row(row: Mapping[str, Any]) -> ProviderSlot:
    account_id = None if row.get("enodot_account_id") is None else int(row["enodot_account_id"])
    return ProviderSlot(
        slot_no=int(row["slot_no"]),
        channel_id=_optional_int(row.get("desired_channel_id")),
        rank=_optional_int(row.get("desired_rank")),
        priority=_optional_int(row.get("desired_priority")),
        assignment_id=account_id,
        enodot_binding_id=_optional_int(row.get("enodot_binding_id")),
        token_id=_optional_int(row.get("a6_token_id")),
        account_id=account_id,
        slot_state=SlotState(str(row["slot_state"])),
        funding_eligible=bool(row.get("funding_eligible")),
        desired_schedulable=bool(row.get("desired_schedulable")),
        observed_schedulable=bool(row.get("observed_schedulable")),
        desired_cost_multiplier=_optional_decimal(row.get("desired_slot_cost_multiplier")),
        observed_cost_multiplier=_optional_decimal(row.get("observed_rate_multiplier")),
        confirmed_pin_binding_id=_optional_int(row.get("confirmed_pin_binding_id")),
        pin_confirmed=bool(row.get("pin_confirmed")),
        probe_confirmed=bool(row.get("probe_confirmed")),
        state_version=int(row.get("state_version") or 1),
    )


def _planned_from_row(row: Mapping[str, Any]) -> PlannedPoolProjection:
    return PlannedPoolProjection(
        routing_generation=int(row["routing_generation"]),
        cost_generation=int(row["cost_generation"]),
        parent_routing_generation=_optional_int(row.get("parent_routing_generation")),
        parent_cost_generation=_optional_int(row.get("parent_cost_generation")),
        desired_size=int(row["desired_size"]),
        ordered_channel_ids=tuple(int(value) for value in row.get("ordered_channel_ids") or ()),
        standby_channel_ids=tuple(int(value) for value in row.get("standby_channel_ids") or ()),
        cost_input_hash=str(row["cost_input_hash"]),
        cost_output_hash=str(row["cost_output_hash"]),
        config_version=str(row["config_version"]),
        source_hash=str(row["source_hash"]),
        generation_source=str(row["generation_source"]),
        generation_state=str(row.get("generation_state") or "planned"),
        pricing_state=str(row.get("pricing_state") or "planned"),
        cost_plan=_planned_cost_plan(row),
    )


def _planned_cost_plan(row: Mapping[str, Any]) -> CostPlan | None:
    raw_slot_costs = row.get("desired_slot_costs")
    if not isinstance(raw_slot_costs, Mapping):
        return None
    return CostPlan(
        desired_slot_costs={
            int(slot_no): Decimal(str(value))
            for slot_no, value in raw_slot_costs.items()
        },
        route_cost_multiplier=Decimal(str(row["route_cost_multiplier"])),
        retry_reserve_multiplier=Decimal(str(row["retry_reserve_multiplier"])),
        realized_cost_p95_multiplier=_optional_decimal(
            row.get("realized_cost_p95_multiplier")
        ),
        pool_cost_multiplier=Decimal(str(row["pool_cost_multiplier"])),
        billing_floor_multiplier=Decimal(str(row["billing_floor_multiplier"])),
        transition_floor_multiplier=Decimal(str(row["transition_floor_multiplier"])),
        margin_bps=int(row["margin_bps"]),
        headroom_bps=int(row["headroom_bps"]),
        quantum=DEFAULT_COST_QUANTUM,
        cost_output_hash=str(row["cost_output_hash"]),
    )


def _one(cursor: Any) -> dict[str, Any] | None:
    row = cursor.fetchone()
    return None if row is None else _mapping_row(cursor, row)


def _many(cursor: Any) -> list[dict[str, Any]]:
    return [_mapping_row(cursor, row) for row in cursor.fetchall()]


def _mapping_row(cursor: Any, row: Any) -> dict[str, Any]:
    if isinstance(row, Mapping):
        return dict(row)
    names = [column.name for column in cursor.description or ()]
    return dict(zip(names, row, strict=False))


def _optional_int(value: object) -> int | None:
    return None if value is None else int(value)


def _optional_decimal(value: object) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def _aware(value: datetime, code: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ProviderPoolShadowControllerError(code)
    return value.astimezone(timezone.utc)


def _sha256(value: object) -> bool:
    text = str(value)
    return len(text) == 64 and all(character in "0123456789abcdefABCDEF" for character in text)


_POOL_SIZE_EVIDENCE_SQL = """
WITH sizing_params AS (
    SELECT %s::text AS model,
           %s::timestamptz AS decision_at,
           %s::timestamptz AS inventory_cutoff
), generation_history AS (
    SELECT generation.generation, generation.desired_size, generation.planned_at,
           lag(generation.desired_size) OVER (
               ORDER BY generation.generation
           ) AS previous_desired_size
    FROM a6_trade.provider_pool_generations generation
    CROSS JOIN sizing_params params
    WHERE generation.model = params.model
), generation_facts AS (
    SELECT coalesce(min(history.planned_at), params.decision_at)
               AS observation_started_at,
           max(history.planned_at) FILTER (
               WHERE history.previous_desired_size IS NOT NULL
                 AND history.desired_size < history.previous_desired_size
           ) AS last_shrink_at
    FROM sizing_params params
    LEFT JOIN generation_history history ON true
    GROUP BY params.decision_at
), request_window AS (
    SELECT envelope.id, envelope.request_id, envelope.created_at,
           least(
               coalesce(envelope.completed_at, params.decision_at),
               params.decision_at
           ) AS completed_at
    FROM a6_trade.provider_request_envelopes envelope
    CROSS JOIN sizing_params params
    CROSS JOIN generation_facts facts
    WHERE envelope.model = params.model
      AND envelope.created_at >= greatest(
          facts.observation_started_at,
          params.decision_at - interval '14 days'
      )
      AND envelope.created_at <= params.decision_at
), request_events AS (
    SELECT request.created_at AS event_at, 1::integer AS delta
    FROM request_window request
    UNION ALL
    SELECT request.completed_at AS event_at, -1::integer AS delta
    FROM request_window request
    WHERE request.completed_at >= request.created_at
), event_deltas AS (
    SELECT event_at, sum(delta)::integer AS delta
    FROM request_events
    GROUP BY event_at
), concurrency_levels AS (
    SELECT greatest(
               sum(delta) OVER (ORDER BY event_at ROWS UNBOUNDED PRECEDING),
               0
           )::integer AS concurrency
    FROM event_deltas
), concurrency_evidence AS (
    SELECT coalesce(
               ceil(percentile_cont(0.99) WITHIN GROUP (ORDER BY concurrency)),
               0
           )::integer AS p99_concurrency
    FROM concurrency_levels
), failed_slots_by_request AS (
    SELECT request.request_id,
           count(DISTINCT attempt.slot_no) FILTER (
               WHERE attempt.state = 'failed'
           )::integer AS failed_slot_count
    FROM request_window request
    LEFT JOIN a6_trade.provider_attempts attempt
      ON attempt.request_envelope_id = request.id
     AND attempt.request_id = request.request_id
    GROUP BY request.request_id
), failure_evidence AS (
    SELECT coalesce(
               ceil(percentile_cont(0.99) WITHIN GROUP (
                   ORDER BY failed_slot_count
               )),
               0
           )::integer AS p99_simultaneous_failed_slots
    FROM failed_slots_by_request
), qualified_financial AS (
    SELECT inventory.merchant_id,
           financial.observed_at AS current_observed_at,
           financial.used_quota AS current_used_quota,
           greatest(coalesce(financial.user_balance_quota, 0), 0)::numeric
               + greatest(coalesce(financial.withdrawable_micros, 0), 0)::numeric
                   AS available_units,
           baseline.observed_at AS baseline_observed_at,
           baseline.used_quota AS baseline_used_quota
    FROM a6_trade.provider_inventory inventory
    CROSS JOIN sizing_params params
    JOIN a6_trade.a6_merchants merchant
      ON merchant.id = inventory.merchant_id
    JOIN a6_trade.a6_tokens token
      ON token.id = inventory.a6_token_id
    JOIN a6_trade.enodot_account_bindings binding
      ON binding.id = inventory.enodot_binding_id
     AND binding.a6_token_id = inventory.a6_token_id
     AND binding.enodot_account_id = inventory.enodot_account_id
    LEFT JOIN a6_trade.a6_financial_current financial
      ON financial.merchant_id = inventory.merchant_id
     AND financial.observed_at >= params.inventory_cutoff
     AND financial.observed_at <= params.decision_at
    LEFT JOIN LATERAL (
        SELECT snapshot.observed_at, snapshot.used_quota
        FROM a6_trade.a6_financial_snapshots snapshot
        WHERE snapshot.merchant_id = inventory.merchant_id
          AND snapshot.observed_at <= params.decision_at - interval '24 hours'
        ORDER BY snapshot.observed_at DESC
        LIMIT 1
    ) baseline ON true
    WHERE inventory.model = params.model
      AND inventory.funding_eligible
      AND inventory.liquidity_state IN ('FUNDED', 'REFILLING')
      AND inventory.inventory_state NOT IN ('REPAIRING', 'RETIRED')
      AND inventory.enodot_account_id IS NOT NULL
      AND binding.state = 'bound'
      AND token.status IN ('1', 'active', 'enabled')
      AND merchant.status = 'supplier_submitted'
      AND 'provider' = ANY(merchant.roles)
), liquidity_runways AS (
    SELECT merchant_id,
           CASE
               WHEN current_observed_at IS NULL
                 OR current_used_quota IS NULL
                 OR baseline_observed_at IS NULL
                 OR baseline_used_quota IS NULL
                 OR current_observed_at <= baseline_observed_at
               THEN NULL
               WHEN current_used_quota <= baseline_used_quota
               THEN 2147483647::bigint
               ELSE least(
                   2147483647::numeric,
                   greatest(
                       0::numeric,
                       floor(
                           available_units
                           * extract(epoch FROM (
                               current_observed_at - baseline_observed_at
                           ))::numeric
                           / (current_used_quota - baseline_used_quota)::numeric
                       )
                   )
               )::bigint
           END AS runway_seconds
    FROM qualified_financial
), liquidity_ranked AS (
    SELECT row_number() OVER (
               ORDER BY runway_seconds DESC, merchant_id
           )::integer AS provider_rank,
           sum(runway_seconds) OVER (
               ORDER BY runway_seconds DESC, merchant_id
               ROWS UNBOUNDED PRECEDING
           ) AS cumulative_runway_seconds
    FROM liquidity_runways
    WHERE runway_seconds IS NOT NULL
), liquidity_evidence AS (
    SELECT (SELECT count(*) FROM qualified_financial)::integer
               AS liquidity_provider_count,
           (
               (SELECT count(*) FROM qualified_financial) > 0
               AND (SELECT count(*) FROM liquidity_runways
                    WHERE runway_seconds IS NOT NULL)
                   = (SELECT count(*) FROM qualified_financial)
           ) AS liquidity_runway_evidence_complete,
           coalesce(
               (SELECT max(cumulative_runway_seconds) >= 86400
                FROM liquidity_ranked),
               false
           ) AS liquidity_runway_capacity_sufficient,
           coalesce(
               (SELECT min(provider_rank)
                FROM liquidity_ranked
                WHERE cumulative_runway_seconds >= 86400),
               (SELECT count(*)::integer FROM qualified_financial)
           )::integer AS providers_needed_for_24h_liquidity_runway
)
SELECT facts.observation_started_at, facts.last_shrink_at,
       concurrency.p99_concurrency,
       failure.p99_simultaneous_failed_slots,
       liquidity.liquidity_provider_count,
       liquidity.liquidity_runway_evidence_complete,
       liquidity.liquidity_runway_capacity_sufficient,
       liquidity.providers_needed_for_24h_liquidity_runway
FROM generation_facts facts
CROSS JOIN concurrency_evidence concurrency
CROSS JOIN failure_evidence failure
CROSS JOIN liquidity_evidence liquidity
"""


_USAGE_LOGS_READABLE_SQL = """
SELECT coalesce(
    to_regclass('public.usage_logs') IS NOT NULL
    AND has_schema_privilege('public', 'USAGE')
    AND has_table_privilege(to_regclass('public.usage_logs'), 'SELECT'),
    false
) AS readable
"""

_REALIZED_COST_SQL = """
WITH realized_params AS (
    SELECT %s::timestamptz AS since,
           make_interval(secs => %s) AS match_tolerance,
           %s::text AS model
), bound_usage AS (
    SELECT usage.id AS usage_log_id,
           token.merchant_id,
           binding.a6_token_id AS a6_token_ref_id,
           usage.model,
           usage.requested_model,
           usage.upstream_model,
           coalesce(usage.input_tokens, 0)
               + coalesce(usage.cache_read_tokens, 0)
               + coalesce(usage.cache_creation_tokens, 0) AS prompt_tokens,
           coalesce(usage.output_tokens, 0) AS completion_tokens,
           usage.total_cost::numeric AS successful_equivalent_cost,
           usage.created_at
    FROM public.usage_logs usage
    CROSS JOIN realized_params params
    JOIN a6_trade.enodot_account_bindings binding
      ON binding.enodot_account_id = usage.account_id
     AND binding.created_at <= usage.created_at
    JOIN a6_trade.a6_tokens token ON token.id = binding.a6_token_id
    WHERE usage.created_at >= params.since
      AND usage.total_cost IS NOT NULL
      AND usage.total_cost > 0
      AND (
          usage.model = params.model
          OR usage.requested_model = params.model
          OR usage.upstream_model = params.model
      )
), provider_events AS (
    SELECT event.id AS provider_event_id,
           event.merchant_id,
           event.a6_token_ref_id,
           event.model,
           event.prompt_tokens,
           event.completion_tokens,
           event.quota_micros,
           event.occurred_at,
           merchant.quota_per_unit
    FROM a6_trade.a6_provider_usage_events event
    CROSS JOIN realized_params params
    JOIN a6_trade.a6_merchants merchant ON merchant.id = event.merchant_id
    WHERE event.occurred_at >= params.since - params.match_tolerance
      AND event.model = params.model
      AND event.probe_run_id IS NULL
      AND event.result_class = '2'
      AND event.quota_micros >= 0
      AND merchant.quota_per_unit > 0
), candidate_pairs AS (
    SELECT usage.usage_log_id,
           event.provider_event_id,
           (event.quota_micros::numeric / event.quota_per_unit)
               / usage.successful_equivalent_cost AS realized_cost_multiplier,
           count(*) OVER (PARTITION BY usage.usage_log_id)
               AS usage_candidate_count,
           count(*) OVER (PARTITION BY event.provider_event_id)
               AS provider_event_candidate_count
    FROM bound_usage usage
    CROSS JOIN realized_params params
    JOIN provider_events event
      ON event.merchant_id = usage.merchant_id
     AND event.a6_token_ref_id = usage.a6_token_ref_id
     AND (
         event.model = usage.model
         OR event.model = usage.requested_model
         OR event.model = usage.upstream_model
     )
     AND event.occurred_at BETWEEN
         usage.created_at - params.match_tolerance
         AND usage.created_at + params.match_tolerance
     AND (
         event.prompt_tokens IS NULL
         OR event.prompt_tokens = usage.prompt_tokens
     )
     AND (
         event.completion_tokens IS NULL
         OR event.completion_tokens = usage.completion_tokens
     )
), unique_matches AS (
    SELECT realized_cost_multiplier
    FROM candidate_pairs
    WHERE usage_candidate_count = 1
      AND provider_event_candidate_count = 1
)
SELECT count(*)::bigint AS sample_count,
       percentile_cont(0.95) WITHIN GROUP (
           ORDER BY realized_cost_multiplier::double precision
       )::numeric(30, 12) AS realized_cost_p95_multiplier
FROM unique_matches
"""

_LATEST_SCAN_SQL = """
SELECT id, model, completed_at, page_count, candidate_count, result_hash,
       result_payload, status
FROM a6_trade.a6_market_scan_runs
WHERE model = %s
  AND status = 'completed'
  AND completed_at IS NOT NULL
ORDER BY started_at DESC, id DESC
LIMIT 1
"""

_SNAPSHOT_CANDIDATE_COUNT_SQL = """
SELECT jsonb_array_length(scan.result_payload)::integer AS candidate_count,
       coalesce(max(health.observer_count), 0)::integer AS health_observer_count
FROM a6_trade.a6_market_scan_runs scan
LEFT JOIN LATERAL jsonb_array_elements(scan.result_payload) payload(item) ON true
LEFT JOIN LATERAL (
    SELECT count(*)::integer AS observer_count
    FROM a6_trade.a6_channel_observer_health_current current
    WHERE current.model = scan.model
      AND current.channel_id = (payload.item->>'channel_id')::bigint
      AND current.observed_at >= %s
) health ON true
WHERE scan.model = %s AND scan.id = %s
GROUP BY scan.result_payload
"""

_LATEST_SUCCESS_AT_SQL = """
CASE
    WHEN business_success.last_success_at IS NULL
    THEN candidate.last_success_at
    WHEN candidate.last_success_at IS NULL
    THEN business_success.last_success_at
    ELSE greatest(
        business_success.last_success_at,
        candidate.last_success_at
    )
END
""".strip()

_SNAPSHOT_CANDIDATES_SQL = f"""
SELECT candidate.model, candidate.channel_id, candidate.listing_id,
       signal.supplier_id,
       coalesce(candidate.final_multiplier, candidate.realtime_ratio_usd) AS price_key,
       candidate.final_multiplier, candidate.realtime_ratio_cny,
       candidate.exchange_rate, candidate.available,
       coalesce(signal.is_owned, false) AS is_owned,
       candidate.health_state,
       {_LATEST_SUCCESS_AT_SQL} AS last_success_at,
       coalesce(
           business_latency.elapsed_ms,
           signal.recent_p50_ms
       ) AS latency_ewma_ms,
       scan.completed_at AS observed_at,
       coalesce(health.observer_count, 0) AS observer_count,
       coalesce(health.observer_health_count, 0) AS observer_health_count
FROM a6_trade.a6_market_scan_runs scan
CROSS JOIN LATERAL jsonb_array_elements(scan.result_payload) payload(item)
CROSS JOIN LATERAL (
    SELECT coalesce(nullif(payload.item->>'model', ''), scan.model) AS model,
           (payload.item->>'channel_id')::bigint AS channel_id,
           nullif(payload.item->>'listing_id', '')::bigint AS listing_id,
           nullif(payload.item->>'final_multiplier', '')::numeric AS final_multiplier,
           nullif(payload.item->>'realtime_ratio_usd', '')::numeric
               AS realtime_ratio_usd,
           nullif(payload.item->>'realtime_ratio_cny', '')::numeric
               AS realtime_ratio_cny,
           nullif(payload.item->>'exchange_rate', '')::numeric AS exchange_rate,
           coalesce(nullif(payload.item->>'available', '')::boolean, false)
               AS available,
           nullif(payload.item->>'health_state', '') AS health_state,
           CASE
               WHEN nullif(payload.item->>'last_success_at', '') IS NULL THEN NULL
               WHEN (payload.item->>'last_success_at')
                        ~ '^-?[0-9]+([.][0-9]+)?$'
               THEN to_timestamp(
                   CASE
                       WHEN abs((payload.item->>'last_success_at')::numeric)
                                >= 100000000000
                       THEN (payload.item->>'last_success_at')::numeric / 1000
                       ELSE (payload.item->>'last_success_at')::numeric
                   END
               )
               ELSE (payload.item->>'last_success_at')::timestamptz
           END AS last_success_at
) candidate
LEFT JOIN a6_trade.a6_market_channel_signals_current signal
  ON signal.model = candidate.model
 AND signal.channel_id = candidate.channel_id
LEFT JOIN LATERAL (
    SELECT event.occurred_at AS last_success_at
    FROM a6_trade.a6_provider_usage_events event
    WHERE event.model = candidate.model
      AND event.channel_id = candidate.channel_id
      AND event.result_class = '2'
      AND event.probe_run_id IS NULL
    ORDER BY event.occurred_at DESC, event.id DESC
    LIMIT 1
) business_success ON true
LEFT JOIN LATERAL (
    SELECT event.elapsed_ms::numeric AS elapsed_ms
    FROM a6_trade.a6_provider_usage_events event
    WHERE event.model = candidate.model
      AND event.channel_id = candidate.channel_id
      AND event.result_class = '2'
      AND event.probe_run_id IS NULL
      AND event.elapsed_ms IS NOT NULL
      AND event.elapsed_ms >= 0
    ORDER BY event.occurred_at DESC, event.id DESC
    LIMIT 1
) business_latency ON true
LEFT JOIN LATERAL (
    SELECT count(*)::integer AS observer_count,
           count(*) FILTER (
               WHERE current.available
                 AND current.health_state NOT IN ('cooling', 'unavailable')
           )::integer AS observer_health_count
    FROM a6_trade.a6_channel_observer_health_current current
    WHERE current.model = candidate.model
      AND current.channel_id = candidate.channel_id
      AND current.observed_at >= %s
) health ON true
WHERE scan.model = %s AND scan.id = %s
  AND NOT EXISTS (
      SELECT 1
      FROM a6_trade.a6_merchants owned_supplier
      WHERE owned_supplier.supplier_id = signal.supplier_id
  )
  AND NOT EXISTS (
      SELECT 1
      FROM a6_trade.a6_supplier_channels_current owned_channel
      WHERE owned_channel.a6_channel_id = candidate.channel_id
  )
  AND NOT EXISTS (
      SELECT 1
      FROM a6_trade.a6_supplier_listings_current owned_listing
      WHERE owned_listing.a6_listing_id = candidate.listing_id
  )
ORDER BY candidate.channel_id
"""

_OWNED_RESOURCE_INVENTORY_SQL = """
SELECT 'supplier'::text AS resource_type, merchant.supplier_id AS resource_id
FROM a6_trade.a6_merchants merchant
WHERE merchant.supplier_id IS NOT NULL
UNION ALL
SELECT 'channel'::text AS resource_type, channel.a6_channel_id AS resource_id
FROM a6_trade.a6_supplier_channels_current channel
UNION ALL
SELECT 'listing'::text AS resource_type, listing.a6_listing_id AS resource_id
FROM a6_trade.a6_supplier_listings_current listing
ORDER BY resource_type, resource_id
"""

_INVENTORY_SQL = """
SELECT inventory.merchant_id, inventory.enodot_binding_id,
       inventory.enodot_account_id, inventory.a6_token_id,
       inventory.inventory_state, inventory.liquidity_state,
       inventory.funding_eligible, inventory.schedulable,
       inventory.assigned_slot_no, inventory.last_financial_observed_at,
       binding.state AS binding_state, token.status AS token_status,
       merchant.status AS merchant_status, merchant.roles,
       greatest(coalesce(financial.withdrawable_micros, 0), 0)::bigint
           AS withdrawable_units,
       0::bigint AS user_balance_runway_seconds,
       true AS egress_healthy,
       NULL::timestamptz AS recent_provider_success_at
FROM a6_trade.provider_inventory inventory
JOIN a6_trade.a6_merchants merchant ON merchant.id = inventory.merchant_id
JOIN a6_trade.a6_tokens token ON token.id = inventory.a6_token_id
LEFT JOIN a6_trade.enodot_account_bindings binding
  ON binding.id = inventory.enodot_binding_id
 AND binding.a6_token_id = inventory.a6_token_id
 AND binding.enodot_account_id = inventory.enodot_account_id
LEFT JOIN a6_trade.a6_financial_current financial
  ON financial.merchant_id = inventory.merchant_id
WHERE inventory.model = %s
  AND inventory.enodot_account_id IS NOT NULL
  AND inventory.a6_token_id IS NOT NULL
ORDER BY inventory.merchant_id, inventory.enodot_account_id
"""

_INVENTORY_EVIDENCE_SQL = """
SELECT inventory.merchant_id, inventory.enodot_binding_id,
       inventory.enodot_account_id, inventory.a6_token_id,
       inventory.inventory_state, inventory.liquidity_state,
       inventory.funding_eligible, inventory.schedulable,
       inventory.assigned_slot_no, inventory.last_financial_observed_at,
       binding.state AS binding_state, token.status AS token_status,
       merchant.status AS merchant_status, merchant.roles,
       greatest(coalesce(financial.withdrawable_micros, 0), 0)::bigint
           AS withdrawable_units,
       CASE
           WHEN financial.observed_at IS NULL
             OR financial.used_quota IS NULL
             OR baseline.observed_at IS NULL
             OR baseline.used_quota IS NULL
             OR financial.observed_at <= baseline.observed_at
           THEN 0
           WHEN financial.used_quota <= baseline.used_quota
           THEN 2147483647
           ELSE least(
               2147483647::numeric,
               greatest(
                   0::numeric,
                   floor(
                       (
                           greatest(coalesce(financial.user_balance_quota, 0), 0)::numeric
                           + greatest(
                               coalesce(financial.withdrawable_micros, 0), 0
                           )::numeric
                       )
                       * extract(epoch FROM (
                           financial.observed_at - baseline.observed_at
                       ))::numeric
                       / (financial.used_quota - baseline.used_quota)::numeric
                   )
               )
           )::bigint
       END AS user_balance_runway_seconds,
       NOT EXISTS (
           SELECT 1
           FROM a6_trade.provider_quarantines quarantine
           WHERE quarantine.model = inventory.model
             AND quarantine.state IN ('active', 'probing')
             AND (quarantine.ends_at IS NULL OR quarantine.ends_at > now())
             AND (
                 quarantine.scope_type IN ('endpoint', 'egress', 'region')
                 OR (
                     quarantine.scope_type = 'account'
                     AND quarantine.scope_id = inventory.enodot_account_id::text
                 )
                 OR (
                     quarantine.scope_type = 'token'
                     AND quarantine.scope_id = inventory.a6_token_id::text
                 )
             )
       ) AS egress_healthy,
       recent_success.finished_at AS recent_provider_success_at
FROM a6_trade.provider_inventory inventory
JOIN a6_trade.a6_merchants merchant ON merchant.id = inventory.merchant_id
JOIN a6_trade.a6_tokens token ON token.id = inventory.a6_token_id
LEFT JOIN a6_trade.enodot_account_bindings binding
  ON binding.id = inventory.enodot_binding_id
 AND binding.a6_token_id = inventory.a6_token_id
 AND binding.enodot_account_id = inventory.enodot_account_id
LEFT JOIN a6_trade.a6_financial_current financial
  ON financial.merchant_id = inventory.merchant_id
LEFT JOIN LATERAL (
    SELECT snapshot.observed_at, snapshot.used_quota
    FROM a6_trade.a6_financial_snapshots snapshot
    WHERE snapshot.merchant_id = inventory.merchant_id
      AND financial.observed_at IS NOT NULL
      AND snapshot.observed_at <= financial.observed_at - interval '24 hours'
    ORDER BY snapshot.observed_at DESC
    LIMIT 1
) baseline ON true
LEFT JOIN LATERAL (
    SELECT attempt.finished_at
    FROM a6_trade.provider_attempts attempt
    WHERE attempt.model = inventory.model
      AND attempt.enodot_account_id = inventory.enodot_account_id
      AND attempt.state = 'won'
      AND attempt.finished_at IS NOT NULL
    ORDER BY attempt.finished_at DESC, attempt.id DESC
    LIMIT 1
) recent_success ON true
WHERE inventory.model = %s
  AND inventory.enodot_account_id IS NOT NULL
  AND inventory.a6_token_id IS NOT NULL
ORDER BY inventory.merchant_id, inventory.enodot_account_id
"""

_QUARANTINES_SQL = """
SELECT scope_id::bigint AS channel_id, ends_at
FROM a6_trade.provider_quarantines
WHERE model = %s AND scope_type = 'channel' AND state = 'active'
  AND (ends_at IS NULL OR ends_at > %s)
ORDER BY scope_id::bigint
"""

_ACTIVE_SQL = """
SELECT generation.generation AS routing_generation,
       generation.cost_generation, generation.desired_size,
       generation.ordered_channel_ids, generation.standby_channel_ids,
       generation.observed_at, cost.cost_input_hash,
       cost.billing_floor_multiplier, cost.observed_billing_floor_multiplier,
       cost.realized_cost_p95_multiplier
FROM a6_trade.provider_pool_generations generation
JOIN a6_trade.provider_cost_generations cost
  ON cost.id = generation.cost_generation_id
WHERE generation.model = %s
  AND generation.generation_state = 'active'
  AND cost.pricing_state = 'active'
"""

_ACTIVE_SLOTS_SQL = """
SELECT slot_no, enodot_binding_id, enodot_account_id, a6_token_id,
       desired_channel_id, desired_rank, desired_priority,
       desired_slot_cost_multiplier, observed_rate_multiplier,
       funding_eligible, slot_state, desired_schedulable,
       observed_schedulable, confirmed_pin_binding_id,
       pin_confirmed, probe_confirmed, state_version
FROM a6_trade.provider_pool_slots
WHERE model = %s AND valid_to IS NULL
ORDER BY slot_no
"""

_LATEST_PLANNED_SQL = """
SELECT generation.generation AS routing_generation,
       generation.cost_generation,
       generation.parent_generation AS parent_routing_generation,
       cost.parent_cost_generation, generation.desired_size,
       generation.ordered_channel_ids, generation.standby_channel_ids,
       cost.cost_input_hash, cost.cost_output_hash, generation.config_version,
       generation.source_hash, generation.generation_source,
       generation.generation_state, cost.pricing_state,
       cost.desired_slot_costs, cost.route_cost_multiplier,
       cost.retry_reserve_multiplier, cost.realized_cost_p95_multiplier,
       cost.pool_cost_multiplier, cost.billing_floor_multiplier,
       cost.transition_floor_multiplier, cost.margin_bps, cost.headroom_bps
FROM a6_trade.provider_pool_generations generation
JOIN a6_trade.provider_cost_generations cost
  ON cost.id = generation.cost_generation_id
WHERE generation.model = %s
  AND (
      (generation.generation_state = 'planned' AND cost.pricing_state = 'planned')
      OR (
          generation.generation_state = 'applying'
          AND cost.pricing_state IN ('applying', 'confirmed')
      )
  )
  AND generation.parent_generation IS NOT DISTINCT FROM %s
ORDER BY generation.generation DESC
LIMIT 1
"""

_GENERATION_CURSOR_SQL = """
SELECT coalesce((
           SELECT max(generation) FROM a6_trade.provider_pool_generations WHERE model = %s
       ), 0) AS maximum_routing_generation,
       coalesce((
           SELECT max(cost_generation) FROM a6_trade.provider_cost_generations WHERE model = %s
       ), 0) AS maximum_cost_generation
"""
