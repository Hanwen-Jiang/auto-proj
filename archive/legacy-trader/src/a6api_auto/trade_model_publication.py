"""Model publication state derived from the confirmed Provider pool."""

from __future__ import annotations

import hashlib
import json
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager, contextmanager, nullcontext
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any, Protocol

from .channel_policy import (
    FORBIDDEN_CHANNEL_NAME,
    GENERATED_CHANNEL_NAME_PATTERN,
    GENERATED_CHANNEL_NAME_SIGNAL_PATTERN,
)


class PublicationError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class PublicationState(str, Enum):
    PUBLISHED = "published"
    DEGRADED = "degraded"
    UNPUBLISHED = "unpublished"
    REPAIRING = "repairing"


@dataclass(frozen=True)
class PublicationDecision:
    model: str
    desired_state: PublicationState
    healthy_slots: int
    minimum_healthy_slots: int
    redundancy_target: int
    reason_code: str
    generation: int | None = None
    cost_generation: int | None = None


@dataclass(frozen=True)
class PublicationRecord:
    model: str
    desired_state: PublicationState
    observed_state: PublicationState
    healthy_slots: int
    minimum_healthy_slots: int
    redundancy_target: int
    reason_code: str
    generation: int | None = None
    cost_generation: int | None = None
    state_version: int = 1
    publication_scope: str | None = None
    action_started_at: datetime | None = None
    confirmed_at: datetime | None = None
    last_real_probe_at: datetime | None = None
    last_real_probe_request_id: str | None = None


class ListingPublicationState(str, Enum):
    PUBLISHED = "published"
    UNPUBLISHED = "unpublished"
    MIXED = "mixed"


class PublicationActionDisposition(str, Enum):
    CLAIMED = "claimed"
    COMPLETED = "completed"
    BUSY = "busy"


@dataclass(frozen=True)
class PublicationProbeResult:
    request_id: str | None = None


@dataclass(frozen=True)
class PublicationActionClaim:
    idempotency_key: str
    disposition: PublicationActionDisposition
    action_id: int | None = None
    attempt_no: int | None = None


@dataclass(frozen=True)
class PublicationReconcileReport:
    record: PublicationRecord
    target_listing_state: ListingPublicationState
    observed_listing_state: ListingPublicationState
    action: str
    action_disposition: str
    remote_write_performed: bool
    probe_request_id: str | None = None


@dataclass(frozen=True)
class ManagedPublicationListing:
    merchant_id: int
    a6_user_id: int
    session_ref: str
    managed_uid: str
    listing_id: int
    channel_id: int
    model: str

    def __post_init__(self) -> None:
        if (
            min(
                self.merchant_id,
                self.a6_user_id,
                self.listing_id,
                self.channel_id,
            )
            < 1
        ):
            raise PublicationError("publication_managed_listing_identity_invalid")
        if not self.session_ref or not self.managed_uid or not self.model:
            raise PublicationError("publication_managed_listing_identity_invalid")


@dataclass(frozen=True)
class SellerListingCostEvidence:
    """Confirmed Seller price contract used by the publication cost guard."""

    listing: ManagedPublicationListing
    price_multiplier: Decimal | str | float
    delta: Decimal | str | float
    conversion_factor: Decimal | str | float
    margin: Decimal | str | float
    headroom: Decimal | str | float
    price_plan_active: bool
    remote_price_readback_confirmed: bool
    seller_binding_verified: bool = False
    collector_fresh: bool = False
    session_healthy: bool = False
    channel_healthy: bool = False
    recent_external_success_at: datetime | None = None
    recent_external_success_latency_ms: int | None = None

    def __post_init__(self) -> None:
        values = {
            "price_multiplier": self.price_multiplier,
            "delta": self.delta,
            "conversion_factor": self.conversion_factor,
            "margin": self.margin,
            "headroom": self.headroom,
        }
        parsed: dict[str, Decimal] = {}
        for name, value in values.items():
            parsed[name] = _guard_decimal(value, f"publication_cost_guard_{name}_invalid")
        if parsed["price_multiplier"] < 0 or parsed["conversion_factor"] <= 0:
            raise PublicationError("publication_cost_guard_price_contract_invalid")
        if parsed["delta"] < 0 or parsed["margin"] < 0 or parsed["headroom"] < 0:
            raise PublicationError("publication_cost_guard_price_contract_invalid")
        if parsed["conversion_factor"] > 1:
            raise PublicationError("publication_cost_guard_price_contract_invalid")
        confirmations = (
            self.price_plan_active,
            self.remote_price_readback_confirmed,
            self.seller_binding_verified,
            self.collector_fresh,
            self.session_healthy,
            self.channel_healthy,
        )
        if any(not isinstance(value, bool) for value in confirmations):
            raise PublicationError("publication_cost_guard_confirmation_invalid")
        if self.recent_external_success_at is not None and (
            not isinstance(self.recent_external_success_at, datetime)
            or self.recent_external_success_at.tzinfo is None
        ):
            raise PublicationError("publication_cost_guard_success_evidence_invalid")
        latency = self.recent_external_success_latency_ms
        if latency is not None and (
            isinstance(latency, bool) or not isinstance(latency, int) or latency < 0
        ):
            raise PublicationError("publication_cost_guard_success_evidence_invalid")
        for name, value in parsed.items():
            object.__setattr__(self, name, value)

    @property
    def provider_cost_ceiling(self) -> Decimal:
        return seller_listing_provider_cost_ceiling(
            price_multiplier=self.price_multiplier,
            delta=self.delta,
            conversion_factor=self.conversion_factor,
            margin=self.margin,
            headroom=self.headroom,
        )


@dataclass(frozen=True)
class SellerListingCostTarget:
    listing: ManagedPublicationListing
    provider_cost_ceiling: Decimal
    desired_state: ListingPublicationState
    reason_code: str
    recent_external_success_at: datetime | None = None
    health_score: int = 0
    operational_latency_ms: int | None = None


@dataclass(frozen=True)
class SellerCostGuardPlan:
    model: str
    protected_provider_cost: Decimal
    cost_generation: int
    cost_generation_active: bool
    targets: tuple[SellerListingCostTarget, ...]
    schedulable_sources: bool = True

    @property
    def any_publishable(self) -> bool:
        return any(item.desired_state is ListingPublicationState.PUBLISHED for item in self.targets)

    @property
    def maximum_provider_cost_ceiling(self) -> Decimal:
        return max(item.provider_cost_ceiling for item in self.targets)

    @property
    def publishable_listing_count(self) -> int:
        return sum(item.desired_state is ListingPublicationState.PUBLISHED for item in self.targets)

    def target_for(self, listing: ManagedPublicationListing) -> ListingPublicationState:
        identity = (listing.a6_user_id, listing.listing_id)
        for target in self.targets:
            if (target.listing.a6_user_id, target.listing.listing_id) == identity:
                return target.desired_state
        raise PublicationError("publication_cost_guard_listing_missing")


def seller_listing_provider_cost_ceiling(
    *,
    price_multiplier: Decimal | str | float,
    delta: Decimal | str | float,
    conversion_factor: Decimal | str | float,
    margin: Decimal | str | float,
    headroom: Decimal | str | float,
) -> Decimal:
    """Invert SellerPricing's profit-floor formula to recover the maximum safe K."""

    price = _guard_decimal(price_multiplier, "publication_cost_guard_price_multiplier_invalid")
    delta_value = _guard_decimal(delta, "publication_cost_guard_delta_invalid")
    conversion = _guard_decimal(
        conversion_factor, "publication_cost_guard_conversion_factor_invalid"
    )
    margin_value = _guard_decimal(margin, "publication_cost_guard_margin_invalid")
    headroom_value = _guard_decimal(headroom, "publication_cost_guard_headroom_invalid")
    if (
        price < 0
        or delta_value < 0
        or conversion <= 0
        or conversion > 1
        or margin_value < 0
        or headroom_value < 0
    ):
        raise PublicationError("publication_cost_guard_price_contract_invalid")
    return (
        price
        * (Decimal(1) + delta_value)
        * conversion
        / ((Decimal(1) + margin_value) * (Decimal(1) + headroom_value))
    )


def plan_seller_cost_guard(
    model: str,
    *,
    protected_provider_cost: Decimal | str | float,
    cost_generation: int,
    cost_generation_active: bool,
    listings: Sequence[SellerListingCostEvidence],
    schedulable_sources: bool = True,
) -> SellerCostGuardPlan:
    model = _model(model)
    protected_cost = _guard_decimal(
        protected_provider_cost, "publication_cost_guard_provider_cost_invalid"
    )
    if protected_cost < 0:
        raise PublicationError("publication_cost_guard_provider_cost_invalid")
    if (
        isinstance(cost_generation, bool)
        or not isinstance(cost_generation, int)
        or cost_generation < 1
    ):
        raise PublicationError("publication_cost_guard_generation_invalid")
    if not isinstance(cost_generation_active, bool) or not isinstance(
        schedulable_sources, bool
    ):
        raise PublicationError("publication_cost_guard_generation_state_invalid")
    evidence = tuple(listings)
    if not evidence or any(item.listing.model != model for item in evidence):
        raise PublicationError("publication_cost_guard_listings_invalid")
    identities = {(item.listing.a6_user_id, item.listing.listing_id) for item in evidence}
    if len(identities) != len(evidence):
        raise PublicationError("publication_cost_guard_listings_invalid")
    targets = []
    for item in evidence:
        ceiling = item.provider_cost_ceiling
        if not schedulable_sources:
            state = ListingPublicationState.UNPUBLISHED
            reason = "no_callable_source"
        elif protected_cost > ceiling:
            state = ListingPublicationState.UNPUBLISHED
            reason = "provider_cost_above_listing_ceiling"
        elif not cost_generation_active:
            state = ListingPublicationState.UNPUBLISHED
            reason = "provider_cost_generation_not_active"
        elif not item.price_plan_active:
            state = ListingPublicationState.UNPUBLISHED
            reason = "seller_price_plan_not_active"
        elif not item.remote_price_readback_confirmed:
            state = ListingPublicationState.UNPUBLISHED
            reason = "seller_price_readback_unconfirmed"
        elif not item.seller_binding_verified:
            state = ListingPublicationState.UNPUBLISHED
            reason = "seller_binding_unverified"
        elif not item.collector_fresh:
            state = ListingPublicationState.UNPUBLISHED
            reason = "seller_collector_stale"
        elif not item.session_healthy:
            state = ListingPublicationState.UNPUBLISHED
            reason = "seller_session_unhealthy"
        elif not item.channel_healthy:
            state = ListingPublicationState.UNPUBLISHED
            reason = "seller_channel_unhealthy"
        elif item.recent_external_success_at is None:
            state = ListingPublicationState.UNPUBLISHED
            reason = "seller_external_success_missing"
        else:
            state = ListingPublicationState.PUBLISHED
            reason = "seller_cost_guard_satisfied"
        targets.append(
            SellerListingCostTarget(
                item.listing,
                ceiling,
                state,
                reason,
                recent_external_success_at=item.recent_external_success_at,
                health_score=sum((item.session_healthy, item.channel_healthy)),
                operational_latency_ms=item.recent_external_success_latency_ms,
            )
        )
    return SellerCostGuardPlan(
        model=model,
        protected_provider_cost=protected_cost,
        cost_generation=cost_generation,
        cost_generation_active=cost_generation_active,
        targets=tuple(targets),
        schedulable_sources=schedulable_sources,
    )


def apply_seller_cost_guard(
    decision: PublicationDecision, plan: SellerCostGuardPlan
) -> PublicationDecision:
    if decision.model != plan.model or decision.cost_generation != plan.cost_generation:
        raise PublicationError("publication_cost_guard_generation_mismatch")
    if decision.desired_state not in {PublicationState.PUBLISHED, PublicationState.DEGRADED}:
        return decision
    if plan.any_publishable:
        return decision
    reason = (
        "provider_cost_above_all_listing_ceilings"
        if plan.protected_provider_cost > plan.maximum_provider_cost_ceiling
        else "seller_price_recovery_unconfirmed"
    )
    return PublicationDecision(
        model=decision.model,
        desired_state=PublicationState.UNPUBLISHED,
        healthy_slots=decision.healthy_slots,
        minimum_healthy_slots=decision.minimum_healthy_slots,
        redundancy_target=decision.redundancy_target,
        reason_code=reason,
        generation=decision.generation,
        cost_generation=decision.cost_generation,
    )


def decide_publication(
    model: str,
    *,
    healthy_slots: int,
    minimum_healthy_slots: int = 1,
    redundancy_target: int = 3,
    generation: int | None,
    cost_generation: int | None = None,
    bundle_confirmed: bool,
    controls_fresh: bool = True,
) -> PublicationDecision:
    """Return the state that can be safely exposed to the request dispatcher."""

    if not str(model).strip():
        raise PublicationError("publication_model_invalid")
    if isinstance(healthy_slots, bool) or healthy_slots < 0:
        raise PublicationError("publication_healthy_slots_invalid")
    if isinstance(minimum_healthy_slots, bool) or minimum_healthy_slots < 1:
        raise PublicationError("publication_minimum_slots_invalid")
    if (
        isinstance(redundancy_target, bool)
        or not isinstance(redundancy_target, int)
        or redundancy_target < minimum_healthy_slots
    ):
        raise PublicationError("publication_redundancy_target_invalid")
    if generation is not None and (
        isinstance(generation, bool) or not isinstance(generation, int) or generation < 1
    ):
        raise PublicationError("publication_generation_invalid")
    if cost_generation is not None and (
        isinstance(cost_generation, bool)
        or not isinstance(cost_generation, int)
        or cost_generation < 1
    ):
        raise PublicationError("publication_cost_generation_invalid")
    if healthy_slots == 0:
        state = PublicationState.UNPUBLISHED
        reason = "no_healthy_slots"
    elif healthy_slots < minimum_healthy_slots:
        state = PublicationState.UNPUBLISHED
        reason = "below_publish_floor"
    elif not bundle_confirmed:
        state = PublicationState.REPAIRING
        reason = "bundle_not_confirmed"
    elif not controls_fresh:
        state = PublicationState.REPAIRING
        reason = "control_evidence_stale"
    elif healthy_slots < redundancy_target:
        state = PublicationState.DEGRADED
        reason = "below_redundancy_target"
    else:
        state = PublicationState.PUBLISHED
        reason = "healthy"
    return PublicationDecision(
        model=str(model),
        desired_state=state,
        healthy_slots=healthy_slots,
        minimum_healthy_slots=minimum_healthy_slots,
        redundancy_target=redundancy_target,
        reason_code=reason,
        generation=generation,
        cost_generation=cost_generation,
    )


class PublicationStore(Protocol):
    def read(self, model: str) -> PublicationRecord | None: ...

    def write(self, decision: PublicationDecision) -> PublicationRecord: ...


class ObservablePublicationStore(PublicationStore, Protocol):
    def observe(self, model: str, state: PublicationState) -> PublicationRecord: ...

    def confirm(
        self,
        model: str,
        state: PublicationState,
        *,
        expected_state_version: int,
        real_probe_confirmed: bool = False,
        probe_request_id: str | None = None,
    ) -> PublicationRecord: ...

    def mark_action_started(
        self, model: str, *, expected_state_version: int
    ) -> PublicationRecord: ...

    def bind_scope(
        self,
        model: str,
        publication_scope: str,
        *,
        expected_state_version: int,
    ) -> PublicationRecord: ...


class InMemoryPublicationStore:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._records: dict[str, PublicationRecord] = {}

    def read(self, model: str) -> PublicationRecord | None:
        with self._lock:
            return self._records.get(str(model))

    def write(self, decision: PublicationDecision) -> PublicationRecord:
        with self._lock:
            previous = self._records.get(decision.model)
            record = PublicationRecord(
                model=decision.model,
                desired_state=decision.desired_state,
                observed_state=(
                    previous.observed_state
                    if previous is not None
                    else PublicationState.UNPUBLISHED
                ),
                healthy_slots=decision.healthy_slots,
                minimum_healthy_slots=decision.minimum_healthy_slots,
                redundancy_target=decision.redundancy_target,
                reason_code=decision.reason_code,
                generation=decision.generation,
                cost_generation=decision.cost_generation,
                state_version=(previous.state_version + 1 if previous is not None else 1),
                publication_scope=(previous.publication_scope if previous is not None else None),
                action_started_at=None,
                confirmed_at=previous.confirmed_at if previous is not None else None,
                last_real_probe_at=None,
                last_real_probe_request_id=None,
            )
            self._records[decision.model] = record
            return record

    def bind_scope(
        self,
        model: str,
        publication_scope: str,
        *,
        expected_state_version: int,
    ) -> PublicationRecord:
        with self._lock:
            current = self._records.get(str(model))
            if current is None:
                raise PublicationError("publication_record_missing")
            if current.state_version != expected_state_version:
                raise PublicationError("publication_scope_cas_mismatch")
            publication_scope = _required_text(publication_scope, "publication_scope_invalid")
            if current.publication_scope == publication_scope:
                return current
            updated = PublicationRecord(
                model=current.model,
                desired_state=current.desired_state,
                observed_state=current.observed_state,
                healthy_slots=current.healthy_slots,
                minimum_healthy_slots=current.minimum_healthy_slots,
                redundancy_target=current.redundancy_target,
                reason_code=current.reason_code,
                generation=current.generation,
                cost_generation=current.cost_generation,
                state_version=current.state_version + 1,
                publication_scope=publication_scope,
                action_started_at=None,
                confirmed_at=current.confirmed_at,
                last_real_probe_at=None,
                last_real_probe_request_id=None,
            )
            self._records[current.model] = updated
            return updated

    def mark_action_started(self, model: str, *, expected_state_version: int) -> PublicationRecord:
        with self._lock:
            current = self._records.get(str(model))
            if current is None:
                raise PublicationError("publication_record_missing")
            if current.state_version != expected_state_version:
                raise PublicationError("publication_action_start_cas_mismatch")
            updated = PublicationRecord(
                model=current.model,
                desired_state=current.desired_state,
                observed_state=current.observed_state,
                healthy_slots=current.healthy_slots,
                minimum_healthy_slots=current.minimum_healthy_slots,
                redundancy_target=current.redundancy_target,
                reason_code=current.reason_code,
                generation=current.generation,
                cost_generation=current.cost_generation,
                state_version=current.state_version + 1,
                publication_scope=current.publication_scope,
                action_started_at=datetime.now().astimezone(),
                confirmed_at=current.confirmed_at,
                last_real_probe_at=current.last_real_probe_at,
                last_real_probe_request_id=current.last_real_probe_request_id,
            )
            self._records[current.model] = updated
            return updated

    def observe(self, model: str, state: PublicationState) -> PublicationRecord:
        with self._lock:
            current = self._records.get(str(model))
            if current is None:
                raise PublicationError("publication_record_missing")
            return self.confirm(
                model,
                state,
                expected_state_version=current.state_version,
            )

    def confirm(
        self,
        model: str,
        state: PublicationState,
        *,
        expected_state_version: int,
        real_probe_confirmed: bool = False,
        probe_request_id: str | None = None,
    ) -> PublicationRecord:
        with self._lock:
            current = self._records.get(str(model))
            if current is None:
                raise PublicationError("publication_record_missing")
            if current.state_version != expected_state_version:
                raise PublicationError("publication_confirmation_cas_mismatch")
            state = _state(state, "publication_observed_state_invalid")
            now = datetime.now().astimezone()
            updated = PublicationRecord(
                model=current.model,
                desired_state=current.desired_state,
                observed_state=state,
                healthy_slots=current.healthy_slots,
                minimum_healthy_slots=current.minimum_healthy_slots,
                redundancy_target=current.redundancy_target,
                reason_code=current.reason_code,
                generation=current.generation,
                cost_generation=current.cost_generation,
                state_version=current.state_version + 1,
                publication_scope=current.publication_scope,
                action_started_at=current.action_started_at,
                confirmed_at=now,
                last_real_probe_at=(now if real_probe_confirmed else current.last_real_probe_at),
                last_real_probe_request_id=(
                    probe_request_id if real_probe_confirmed else current.last_real_probe_request_id
                ),
            )
            self._records[current.model] = updated
            return updated


class PostgresPublicationStore:
    """Durable desired/observed publication state for a single model.

    The transaction is deliberately short.  Remote listing or real-probe calls
    happen outside this store; callers persist the observed result afterwards.
    ``pg_advisory_xact_lock`` serializes writers for the same model across
    reconciler instances while leaving request-plane reads non-blocking.
    """

    def __init__(self, connection_factory: Any) -> None:
        if not callable(connection_factory):
            raise ValueError("publication_connection_factory_invalid")
        self.connection_factory = connection_factory

    def read(self, model: str) -> PublicationRecord | None:
        model = _model(model)
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT model, desired_state, observed_state, healthy_slots,
                           min_healthy_slots, redundancy_target, reason_code,
                           desired_generation,
                           desired_cost_generation,
                           publication_scope, state_version, action_started_at,
                           confirmed_at, last_real_probe_at,
                           last_real_probe_request_id
                    FROM a6_trade.model_publications
                    WHERE model = %s
                    """,
                    (model,),
                )
                row = _row(cursor)
            return None if row is None else _record_from_row(row)
        finally:
            _close(connection)

    def write(self, decision: PublicationDecision) -> PublicationRecord:
        model = _model(decision.model)
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT pg_advisory_xact_lock(%s, hashtext(%s))",
                    (0x41365055, model),
                )
                cursor.execute(
                    """
                    INSERT INTO a6_trade.model_publications (
                        model, desired_state, observed_state, healthy_slots,
                        min_healthy_slots, redundancy_target, reason_code,
                        desired_generation,
                        desired_cost_generation,
                        observed_at, updated_at
                    ) VALUES (
                        %s, %s, 'unpublished', %s, %s, %s, %s, %s, %s, now(), now()
                    )
                    ON CONFLICT (model) DO UPDATE SET
                        desired_state = EXCLUDED.desired_state,
                        healthy_slots = EXCLUDED.healthy_slots,
                        min_healthy_slots = EXCLUDED.min_healthy_slots,
                        redundancy_target = EXCLUDED.redundancy_target,
                        reason_code = EXCLUDED.reason_code,
                        desired_generation = EXCLUDED.desired_generation,
                        desired_cost_generation = EXCLUDED.desired_cost_generation,
                        desired_at = now(),
                        action_started_at = NULL,
                        last_real_probe_at = NULL,
                        last_real_probe_request_id = NULL,
                        state_version = a6_trade.model_publications.state_version + 1,
                        updated_at = now()
                    RETURNING model, desired_state, observed_state, healthy_slots,
                              min_healthy_slots, redundancy_target, reason_code,
                              desired_generation,
                              desired_cost_generation,
                              publication_scope, state_version, action_started_at,
                              confirmed_at, last_real_probe_at,
                              last_real_probe_request_id
                    """,
                    (
                        model,
                        decision.desired_state.value,
                        decision.healthy_slots,
                        decision.minimum_healthy_slots,
                        decision.redundancy_target,
                        decision.reason_code,
                        decision.generation,
                        decision.cost_generation,
                    ),
                )
                row = _row(cursor)
                if row is None:
                    raise PublicationError("publication_write_readback_missing")
            connection.commit()
            return _record_from_row(row)
        except Exception:
            connection.rollback()
            raise
        finally:
            _close(connection)

    def bind_scope(
        self,
        model: str,
        publication_scope: str,
        *,
        expected_state_version: int,
    ) -> PublicationRecord:
        model = _model(model)
        publication_scope = _required_text(publication_scope, "publication_scope_invalid")
        if (
            isinstance(expected_state_version, bool)
            or not isinstance(expected_state_version, int)
            or expected_state_version < 1
        ):
            raise PublicationError("publication_state_version_invalid")
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE a6_trade.model_publications
                    SET publication_scope = %s,
                        action_started_at = NULL,
                        last_real_probe_at = NULL,
                        last_real_probe_request_id = NULL,
                        state_version = state_version + 1,
                        updated_at = now()
                    WHERE model = %s AND state_version = %s
                      AND publication_scope IS DISTINCT FROM %s
                    RETURNING model, desired_state, observed_state, healthy_slots,
                              min_healthy_slots, redundancy_target, reason_code,
                              desired_generation,
                              desired_cost_generation,
                              publication_scope, state_version, action_started_at,
                              confirmed_at, last_real_probe_at,
                              last_real_probe_request_id
                    """,
                    (
                        publication_scope,
                        model,
                        expected_state_version,
                        publication_scope,
                    ),
                )
                row = _row(cursor)
                if row is None:
                    cursor.execute(
                        """
                        SELECT model, desired_state, observed_state, healthy_slots,
                               min_healthy_slots, redundancy_target, reason_code,
                               desired_generation,
                               desired_cost_generation,
                               publication_scope, state_version, action_started_at,
                               confirmed_at, last_real_probe_at,
                               last_real_probe_request_id
                        FROM a6_trade.model_publications
                        WHERE model = %s AND state_version = %s
                          AND publication_scope = %s
                        """,
                        (model, expected_state_version, publication_scope),
                    )
                    row = _row(cursor)
                if row is None:
                    raise PublicationError("publication_scope_cas_mismatch")
            connection.commit()
            return _record_from_row(row)
        except Exception:
            connection.rollback()
            raise
        finally:
            _close(connection)

    def mark_action_started(self, model: str, *, expected_state_version: int) -> PublicationRecord:
        model = _model(model)
        if (
            isinstance(expected_state_version, bool)
            or not isinstance(expected_state_version, int)
            or expected_state_version < 1
        ):
            raise PublicationError("publication_state_version_invalid")
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE a6_trade.model_publications
                    SET action_started_at = now(),
                        state_version = state_version + 1,
                        updated_at = now()
                    WHERE model = %s AND state_version = %s
                    RETURNING model, desired_state, observed_state, healthy_slots,
                              min_healthy_slots, redundancy_target, reason_code,
                              desired_generation,
                              desired_cost_generation,
                              publication_scope, state_version, action_started_at, confirmed_at,
                              last_real_probe_at, last_real_probe_request_id
                    """,
                    (model, expected_state_version),
                )
                row = _row(cursor)
                if row is None:
                    raise PublicationError("publication_action_start_cas_mismatch")
            connection.commit()
            return _record_from_row(row)
        except Exception:
            connection.rollback()
            raise
        finally:
            _close(connection)

    def observe(self, model: str, state: PublicationState) -> PublicationRecord:
        model = _model(model)
        current = self.read(model)
        if current is None:
            raise PublicationError("publication_record_missing")
        return self.confirm(
            model,
            state,
            expected_state_version=current.state_version,
        )

    def confirm(
        self,
        model: str,
        state: PublicationState,
        *,
        expected_state_version: int,
        real_probe_confirmed: bool = False,
        probe_request_id: str | None = None,
    ) -> PublicationRecord:
        model = _model(model)
        state = _state(state, "publication_observed_state_invalid")
        if (
            isinstance(expected_state_version, bool)
            or not isinstance(expected_state_version, int)
            or expected_state_version < 1
        ):
            raise PublicationError("publication_state_version_invalid")
        if not isinstance(real_probe_confirmed, bool):
            raise PublicationError("publication_probe_confirmation_invalid")
        probe_request_id = _optional_text(probe_request_id)
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE a6_trade.model_publications
                    SET observed_state = %s,
                        observed_at = now(),
                        confirmed_at = now(),
                        last_real_probe_at = CASE
                            WHEN %s THEN now()
                            ELSE last_real_probe_at
                        END,
                        last_real_probe_request_id = CASE
                            WHEN %s THEN %s
                            ELSE last_real_probe_request_id
                        END,
                        state_version = state_version + 1,
                        updated_at = now()
                    WHERE model = %s AND state_version = %s
                    RETURNING model, desired_state, observed_state, healthy_slots,
                              min_healthy_slots, redundancy_target, reason_code,
                              desired_generation,
                              desired_cost_generation,
                              publication_scope, state_version, action_started_at, confirmed_at,
                              last_real_probe_at,
                              last_real_probe_request_id
                    """,
                    (
                        state.value,
                        real_probe_confirmed,
                        real_probe_confirmed,
                        probe_request_id,
                        model,
                        expected_state_version,
                    ),
                )
                row = _row(cursor)
                if row is None:
                    raise PublicationError("publication_confirmation_cas_mismatch")
            connection.commit()
            return _record_from_row(row)
        except Exception:
            connection.rollback()
            raise
        finally:
            _close(connection)


class PublicationReconciler:
    def __init__(self, store: PublicationStore) -> None:
        self.store = store

    def reconcile(self, decision: PublicationDecision) -> PublicationRecord:
        current = self.store.read(decision.model)
        if (
            current is not None
            and current.desired_state is decision.desired_state
            and current.healthy_slots == decision.healthy_slots
            and current.minimum_healthy_slots == decision.minimum_healthy_slots
            and current.redundancy_target == decision.redundancy_target
            and current.generation == decision.generation
            and current.cost_generation == decision.cost_generation
            and current.reason_code == decision.reason_code
        ):
            return current
        return self.store.write(decision)


class PostgresPublicationDiscovery:
    """Discover managed listings and confirmed pool evidence for one model."""

    def __init__(self, connection_factory: Callable[[], Any]) -> None:
        if not callable(connection_factory):
            raise ValueError("publication_discovery_connection_factory_invalid")
        self.connection_factory = connection_factory

    def listings(self, model: str) -> tuple[ManagedPublicationListing, ...]:
        model = _model(model)
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT binding.merchant_id, merchant.a6_user_id,
                           merchant.session_ref, binding.managed_uid,
                           listing.a6_listing_id AS listing_id,
                           listing.a6_channel_id AS channel_id,
                           listing.platform_model AS model
                    FROM a6_trade.a6_managed_listing_bindings binding
                    JOIN a6_trade.a6_supplier_listings_current listing
                      ON listing.merchant_id = binding.merchant_id
                     AND listing.a6_listing_id = binding.a6_listing_id
                     AND listing.a6_channel_id = binding.a6_channel_id
                    JOIN a6_trade.a6_supplier_channels_current channel
                      ON channel.merchant_id = listing.merchant_id
                     AND channel.a6_channel_id = listing.a6_channel_id
                    JOIN a6_trade.a6_merchants merchant
                      ON merchant.id = binding.merchant_id
                    WHERE listing.platform_model = %s
                      AND btrim(channel.name) <> %s
                      AND NOT (
                          btrim(channel.name) ~ %s
                          AND btrim(channel.name) ~ %s
                      )
                      AND binding.a6_listing_id IS NOT NULL
                      AND binding.control_state <> 'retired'
                      AND binding.managed_uid <> ''
                      AND merchant.a6_user_id IS NOT NULL
                      AND merchant.status = 'supplier_submitted'
                      AND merchant.roles @> ARRAY['seller']::text[]
                    ORDER BY merchant.a6_user_id, listing.a6_listing_id
                    """,
                    (
                        model,
                        FORBIDDEN_CHANNEL_NAME,
                        GENERATED_CHANNEL_NAME_PATTERN,
                        GENERATED_CHANNEL_NAME_SIGNAL_PATTERN,
                    ),
                )
                rows = _rows(cursor)
        finally:
            _close(connection)
        result = tuple(
            ManagedPublicationListing(
                merchant_id=int(row["merchant_id"]),
                a6_user_id=int(row["a6_user_id"]),
                session_ref=_required_text(
                    row.get("session_ref"), "publication_session_ref_missing"
                ),
                managed_uid=_required_text(
                    row.get("managed_uid"), "publication_managed_uid_missing"
                ),
                listing_id=int(row["listing_id"]),
                channel_id=int(row["channel_id"]),
                model=_model(row.get("model")),
            )
            for row in rows
        )
        if not result:
            raise PublicationError("publication_managed_listings_missing")
        identities = {(item.a6_user_id, item.listing_id) for item in result}
        if len(identities) != len(result) or any(item.model != model for item in result):
            raise PublicationError("publication_managed_listings_ambiguous")
        return result

    def seller_cost_guard_plan(
        self,
        model: str,
        listings: Sequence[ManagedPublicationListing],
        *,
        collector_freshness_seconds: int = 900,
        success_lookback_seconds: int = 86400,
    ) -> SellerCostGuardPlan:
        """Load the active Provider cost and latest remotely verified Seller prices."""

        model = _model(model)
        managed = tuple(listings)
        if not managed or any(item.model != model for item in managed):
            raise PublicationError("publication_cost_guard_listings_invalid")
        for value, code in (
            (collector_freshness_seconds, "publication_collector_freshness_invalid"),
            (success_lookback_seconds, "publication_success_lookback_invalid"),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise PublicationError(code)
        entity_ids = [item.managed_uid for item in managed]
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT cost.cost_generation, cost.pool_cost_multiplier,
                           cost.pricing_state, binding.managed_uid,
                           coalesce(
                               action.desired_state ->> 'a6_panel_multiplier',
                               (listing.input_price_micros::numeric / 5000000)::text
                           ) AS price_multiplier,
                           coalesce(
                               action.desired_state ->> 'delta',
                               seller.observed_delta::text,
                               '0'
                           ) AS delta,
                           coalesce(
                               action.desired_state ->> 'conversion_factor',
                               '0.833333333333'
                           ) AS conversion_factor,
                           coalesce(action.desired_state ->> 'margin', '0.20')
                               AS margin,
                           coalesce(action.desired_state ->> 'headroom', '0')
                               AS headroom,
                           action.status = 'completed' AS price_plan_active,
                           action.completed_at IS NOT NULL
                           AND coalesce(action.observed_after #>> '{listing,state}', '')
                               = 'verified'
                           AND coalesce(action.observed_after #>> '{listing,error_class}', '')
                               = ''
                           AND listing.input_price_micros =
                               (action.desired_state #>> '{prices_micros,input}')::bigint
                           AND listing.output_price_micros =
                               (action.desired_state #>> '{prices_micros,output}')::bigint
                           AND listing.cache_read_price_micros =
                               (action.desired_state #>> '{prices_micros,cache_read}')::bigint
                           AND listing.cache_write_price_micros =
                               (action.desired_state #>> '{prices_micros,cache_write}')::bigint
                           AND seller.observed_delta =
                               (action.desired_state ->> 'delta')::numeric
                           AND (
                               pause.completed_at IS NULL
                               OR action.completed_at > pause.completed_at
                           )
                               AS remote_price_readback_confirmed,
                           binding.control_state <> 'retired'
                           AND binding.a6_listing_id = listing.a6_listing_id
                           AND binding.a6_channel_id = listing.a6_channel_id
                               AS seller_binding_verified,
                           merchant.last_poll_at IS NOT NULL
                           AND merchant.last_poll_at >=
                               now() - (%s * interval '1 second')
                           AND listing.observed_at = merchant.last_poll_at
                           AND channel.observed_at = merchant.last_poll_at
                               AS collector_fresh,
                           merchant.status = 'supplier_submitted'
                           AND merchant.roles @> ARRAY['seller']::text[]
                           AND merchant.poll_error_count = 0
                           AND merchant.last_auth_ok_at IS NOT NULL
                               AS session_healthy,
                           channel.status = 1 AND channel.availability = 1
                               AS channel_healthy,
                           recent_success.occurred_at AS recent_external_success_at,
                           recent_success.elapsed_ms
                               AS recent_external_success_latency_ms
                    FROM a6_trade.provider_cost_generations cost
                    CROSS JOIN unnest(%s::text[]) AS requested(managed_uid)
                    JOIN a6_trade.a6_managed_listing_bindings binding
                      ON binding.managed_uid = requested.managed_uid
                    JOIN a6_trade.a6_supplier_listings_current listing
                      ON listing.merchant_id = binding.merchant_id
                     AND listing.a6_listing_id = binding.a6_listing_id
                    JOIN a6_trade.a6_supplier_channels_current channel
                      ON channel.merchant_id = binding.merchant_id
                     AND channel.a6_channel_id = binding.a6_channel_id
                    JOIN a6_trade.a6_merchants merchant
                      ON merchant.id = binding.merchant_id
                    LEFT JOIN a6_trade.enodot_managed_seller_resources seller
                      ON seller.merchant_id = binding.merchant_id
                    LEFT JOIN LATERAL (
                        SELECT candidate.desired_state, candidate.observed_after,
                               candidate.status, candidate.completed_at
                        FROM a6_trade.trade_control_actions candidate
                        WHERE candidate.action_type = 'apply_seller_price_band'
                          AND candidate.entity_type = 'a6_managed_listing'
                          AND candidate.entity_id = binding.managed_uid
                          AND candidate.status = 'completed'
                          AND candidate.desired_state ->> 'model' = %s
                        ORDER BY candidate.completed_at DESC, candidate.id DESC
                        LIMIT 1
                    ) action ON true
                    LEFT JOIN LATERAL (
                        SELECT candidate.completed_at
                        FROM a6_trade.trade_control_actions candidate
                        WHERE candidate.action_type = 'emergency_model_pause'
                          AND candidate.entity_type = 'a6_supplier_listing'
                          AND candidate.entity_id = concat(
                              'merchant:', binding.merchant_id,
                              ':listing:', binding.a6_listing_id
                          )
                          AND candidate.status = 'completed'
                          AND candidate.desired_state ->> 'model' = %s
                        ORDER BY candidate.completed_at DESC, candidate.id DESC
                        LIMIT 1
                    ) pause ON true
                    LEFT JOIN LATERAL (
                        SELECT earning.occurred_at, earning.elapsed_ms
                        FROM a6_trade.a6_earning_events earning
                        WHERE earning.merchant_id = binding.merchant_id
                          AND earning.channel_id = binding.a6_channel_id
                          AND earning.model = %s
                          AND earning.counterparty_class = 'external'
                          AND earning.occurred_at >=
                              now() - (%s * interval '1 second')
                        ORDER BY earning.occurred_at DESC, earning.id DESC
                        LIMIT 1
                    ) recent_success ON true
                    WHERE cost.model = %s
                      AND cost.pricing_state = 'active'
                    ORDER BY binding.managed_uid
                    """,
                    (
                        collector_freshness_seconds,
                        entity_ids,
                        model,
                        model,
                        model,
                        success_lookback_seconds,
                        model,
                    ),
                )
                rows = _rows(cursor)
        finally:
            _close(connection)
        if len(rows) != len(managed):
            raise PublicationError("publication_cost_guard_evidence_missing")
        by_uid = {item.managed_uid: item for item in managed}
        if len(by_uid) != len(managed):
            raise PublicationError("publication_cost_guard_listings_invalid")
        generations = {
            (
                int(row["cost_generation"]),
                str(row.get("pricing_state") or ""),
                _guard_decimal(
                    row.get("pool_cost_multiplier"),
                    "publication_cost_guard_provider_cost_invalid",
                ),
            )
            for row in rows
        }
        if len(generations) != 1:
            raise PublicationError("publication_cost_guard_generation_ambiguous")
        cost_generation, pricing_state, protected_cost = generations.pop()
        evidence = []
        for row in rows:
            uid = str(row.get("managed_uid") or "")
            listing = by_uid.get(uid)
            if listing is None:
                raise PublicationError("publication_cost_guard_evidence_ambiguous")
            try:
                evidence.append(
                    SellerListingCostEvidence(
                        listing=listing,
                        price_multiplier=row.get("price_multiplier"),
                        delta=row.get("delta"),
                        conversion_factor=row.get("conversion_factor"),
                        margin=row.get("margin"),
                        headroom=row.get("headroom"),
                        price_plan_active=bool(row.get("price_plan_active")),
                        remote_price_readback_confirmed=bool(
                            row.get("remote_price_readback_confirmed")
                        ),
                        seller_binding_verified=bool(row.get("seller_binding_verified")),
                        collector_fresh=bool(row.get("collector_fresh")),
                        session_healthy=bool(row.get("session_healthy")),
                        channel_healthy=bool(row.get("channel_healthy")),
                        recent_external_success_at=row.get("recent_external_success_at"),
                        recent_external_success_latency_ms=(
                            None
                            if row.get("recent_external_success_latency_ms") is None
                            else int(row["recent_external_success_latency_ms"])
                        ),
                    )
                )
            except PublicationError as exc:
                raise PublicationError("publication_cost_guard_evidence_invalid") from exc
        schedulable_sources = self.load_schedulable_sources(model)
        return plan_seller_cost_guard(
            model,
            protected_provider_cost=protected_cost,
            cost_generation=cost_generation,
            cost_generation_active=(pricing_state == "active"),
            listings=evidence,
            schedulable_sources=schedulable_sources,
        )

    def load_schedulable_sources(self, model: str) -> bool:
        """True when any callable economic account or schedulable provider exists.

        Mirrors the listing-schedulability duty: an account must be enabled,
        healthy, have capacity (AVAILABLE/THROTTLED) and a known cost to be
        callable; a provider must be schedulable and not retired/quarantined.
        """

        normalized = _model(model)
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT EXISTS (
                        SELECT 1
                        FROM a6_trade.sub2api_economic_sources s
                        JOIN a6_trade.sub2api_source_cost_contracts c
                          ON c.source_id = s.source_id AND c.contract_state = 'active'
                        WHERE s.model = %s AND s.enabled
                          AND s.health_state = 'HEALTHY'
                          AND s.capacity_state IN ('AVAILABLE', 'THROTTLED')
                          AND (
                              (c.cost_mode = 'declared_k'
                               AND c.declared_k_cny IS NOT NULL AND c.declared_k_cny > 0)
                              OR c.cost_mode = 'weekly_amortized'
                          )
                    ) AS account_callable,
                    EXISTS (
                        SELECT 1
                        FROM a6_trade.provider_inventory
                        WHERE model = %s AND schedulable = true
                          AND inventory_state NOT IN ('RETIRED', 'QUARANTINED')
                    ) AS provider_callable
                    """,
                    (normalized, normalized),
                )
                row = cursor.fetchone()
        finally:
            _close(connection)
        if row is None:
            raise PublicationError("publication_cost_guard_schedulability_missing")
        if isinstance(row, Mapping):
            return bool(row.get("account_callable")) or bool(row.get("provider_callable"))
        return bool(row[0]) or bool(row[1])

    def decision(
        self,
        model: str,
        *,
        minimum_healthy_slots: int = 1,
        redundancy_target: int = 3,
        control_max_age_seconds: int = 120,
        minimum_health_observers: int = 2,
    ) -> PublicationDecision:
        model = _model(model)
        if (
            isinstance(control_max_age_seconds, bool)
            or not isinstance(control_max_age_seconds, int)
            or control_max_age_seconds < 1
        ):
            raise PublicationError("publication_control_max_age_invalid")
        if (
            isinstance(minimum_health_observers, bool)
            or not isinstance(minimum_health_observers, int)
            or not 1 <= minimum_health_observers <= 10
        ):
            raise PublicationError("publication_minimum_health_observers_invalid")
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT generation.generation,
                           generation.cost_generation,
                           cost.pricing_state,
                           coalesce(bool_and(
                               slot.id IS NULL
                               OR slot.valid_to IS NOT NULL
                               OR slot.slot_state <> 'READY'
                               OR health.fresh_available_observers >= %s
                           ), true) AS controls_fresh,
                           count(slot.id) FILTER (
                               WHERE slot.valid_to IS NULL
                                 AND slot.routing_generation = generation.generation
                                 AND slot.cost_generation = generation.cost_generation
                                 AND slot.slot_state = 'READY'
                                 AND slot.funding_eligible
                                 AND slot.desired_schedulable
                                 AND slot.observed_schedulable
                                 AND slot.pin_confirmed
                                 AND slot.probe_confirmed
                           )::integer AS healthy_slots
                    FROM a6_trade.provider_pool_generations generation
                    JOIN a6_trade.provider_cost_generations cost
                      ON cost.id = generation.cost_generation_id
                     AND cost.model = generation.model
                     AND cost.cost_generation = generation.cost_generation
                    LEFT JOIN a6_trade.provider_pool_slots slot
                      ON slot.generation_id = generation.id
                     AND slot.model = generation.model
                    LEFT JOIN LATERAL (
                        SELECT count(DISTINCT observer.observer_merchant_id)::integer
                                   AS fresh_available_observers
                        FROM a6_trade.a6_channel_observer_health_current observer
                        WHERE observer.model = generation.model
                          AND observer.channel_id = slot.desired_channel_id
                          AND observer.available
                          AND observer.observed_at >=
                              now() - (%s * interval '1 second')
                    ) health ON true
                    WHERE generation.model = %s
                      AND generation.generation_state = 'active'
                    GROUP BY generation.generation, generation.cost_generation,
                             cost.pricing_state
                    """,
                    (
                        minimum_health_observers,
                        control_max_age_seconds,
                        model,
                    ),
                )
                row = _row(cursor)
        finally:
            _close(connection)
        if row is None:
            return decide_publication(
                model,
                healthy_slots=0,
                minimum_healthy_slots=minimum_healthy_slots,
                redundancy_target=redundancy_target,
                generation=None,
                cost_generation=None,
                bundle_confirmed=False,
            )
        return decide_publication(
            model,
            healthy_slots=int(row.get("healthy_slots") or 0),
            minimum_healthy_slots=minimum_healthy_slots,
            redundancy_target=redundancy_target,
            generation=int(row["generation"]),
            cost_generation=int(row["cost_generation"]),
            bundle_confirmed=(str(row.get("pricing_state") or "") == "active"),
            controls_fresh=bool(row.get("controls_fresh")),
        )


class PublicationRemote(Protocol):
    publication_scope: str

    def read_listing(self, model: str) -> ListingPublicationState: ...

    def set_listing_state(
        self,
        model: str,
        state: ListingPublicationState,
        *,
        idempotency_key: str,
    ) -> None: ...

    def probe(self, model: str) -> PublicationProbeResult: ...


class PublicationModelLease(Protocol):
    def hold(self, model: str) -> AbstractContextManager[None]: ...


class PostgresPublicationModelLease:
    """Serialize remote publication sagas with a session advisory lock."""

    def __init__(self, connection_factory: Callable[[], Any]) -> None:
        if not callable(connection_factory):
            raise ValueError("publication_model_lease_connection_factory_invalid")
        self.connection_factory = connection_factory

    @contextmanager
    def hold(self, model: str):
        model = _model(model)
        connection = self.connection_factory()
        locked = False
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT pg_try_advisory_lock(%s, hashtext(%s))",
                    (0x41365056, model),
                )
                locked = _boolean_row(cursor)
            connection.commit()
            if not locked:
                raise PublicationError("publication_model_lease_busy")
            yield
        finally:
            try:
                if locked:
                    with connection.cursor() as cursor:
                        cursor.execute(
                            "SELECT pg_advisory_unlock(%s, hashtext(%s))",
                            (0x41365056, model),
                        )
                        if not _boolean_row(cursor):
                            raise PublicationError("publication_model_lease_release_failed")
                    connection.commit()
            finally:
                _close(connection)


class PublicationActionLedger(Protocol):
    def claim(
        self,
        decision: PublicationDecision,
        target: ListingPublicationState,
        *,
        publication_scope: str = "model",
        reopen_completed: bool = False,
        recover_in_progress: bool = False,
    ) -> PublicationActionClaim: ...

    def complete(
        self,
        claim: PublicationActionClaim,
        *,
        observed_after: Mapping[str, Any],
        elapsed_ms: int,
    ) -> None: ...

    def fail(
        self,
        claim: PublicationActionClaim,
        *,
        error_class: str,
        elapsed_ms: int,
    ) -> None: ...


class InMemoryPublicationActionLedger:
    def __init__(self) -> None:
        self.completed: set[str] = set()
        self.in_progress: set[str] = set()

    def claim(
        self,
        decision: PublicationDecision,
        target: ListingPublicationState,
        *,
        publication_scope: str = "model",
        reopen_completed: bool = False,
        recover_in_progress: bool = False,
    ) -> PublicationActionClaim:
        key = _publication_action_key(decision, target, publication_scope)
        if key in self.in_progress:
            if not recover_in_progress:
                return PublicationActionClaim(key, PublicationActionDisposition.BUSY)
            self.in_progress.discard(key)
        if key in self.completed and not reopen_completed:
            return PublicationActionClaim(key, PublicationActionDisposition.COMPLETED)
        self.completed.discard(key)
        self.in_progress.add(key)
        return PublicationActionClaim(key, PublicationActionDisposition.CLAIMED)

    def complete(
        self,
        claim: PublicationActionClaim,
        *,
        observed_after: Mapping[str, Any],
        elapsed_ms: int,
    ) -> None:
        del observed_after, elapsed_ms
        if claim.disposition is not PublicationActionDisposition.CLAIMED:
            raise PublicationError("publication_action_claim_invalid")
        self.in_progress.discard(claim.idempotency_key)
        self.completed.add(claim.idempotency_key)

    def fail(
        self,
        claim: PublicationActionClaim,
        *,
        error_class: str,
        elapsed_ms: int,
    ) -> None:
        del error_class, elapsed_ms
        if claim.disposition is PublicationActionDisposition.CLAIMED:
            self.in_progress.discard(claim.idempotency_key)


class PostgresPublicationActionLedger:
    """Idempotent publication actions backed by the shared control ledger."""

    def __init__(
        self,
        connection_factory: Callable[[], Any],
        *,
        config_version: str,
        requested_by: str = "model_publication_reconciler",
        stale_claim_seconds: int = 300,
    ) -> None:
        if not callable(connection_factory):
            raise ValueError("publication_action_connection_factory_invalid")
        self.connection_factory = connection_factory
        self.config_version = _required_text(
            config_version, "publication_action_config_version_invalid"
        )
        self.requested_by = _required_text(requested_by, "publication_action_requested_by_invalid")
        if isinstance(stale_claim_seconds, bool) or stale_claim_seconds < 1:
            raise ValueError("publication_action_stale_claim_invalid")
        self.stale_claim_seconds = int(stale_claim_seconds)

    def claim(
        self,
        decision: PublicationDecision,
        target: ListingPublicationState,
        *,
        publication_scope: str = "model",
        reopen_completed: bool = False,
        recover_in_progress: bool = False,
    ) -> PublicationActionClaim:
        if not isinstance(recover_in_progress, bool):
            raise PublicationError("publication_action_recovery_mode_invalid")
        key = _publication_action_key(decision, target, publication_scope)
        desired = _publication_action_document(decision, target, publication_scope)
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO a6_trade.trade_control_actions (
                        idempotency_key, action_type, entity_type, entity_id,
                        desired_state, status, config_version, requested_by, started_at
                    ) VALUES (%s, %s, 'provider_model', %s, %s::jsonb,
                              'started', %s, %s, now())
                    ON CONFLICT (idempotency_key) DO NOTHING
                    RETURNING id, status, desired_state
                    """,
                    (
                        key,
                        "publish_model"
                        if target is ListingPublicationState.PUBLISHED
                        else "unpublish_model",
                        decision.model,
                        _json(desired),
                        self.config_version,
                        self.requested_by,
                    ),
                )
                row = _row(cursor)
                if row is None:
                    cursor.execute(
                        """
                        SELECT id, status, desired_state,
                               %s
                               OR status NOT IN ('started', 'applying')
                               OR updated_at <= now() - (%s * interval '1 second')
                                   AS claimable
                        FROM a6_trade.trade_control_actions
                        WHERE idempotency_key = %s
                        FOR UPDATE
                        """,
                        (recover_in_progress, self.stale_claim_seconds, key),
                    )
                    row = _row(cursor)
                    if row is None:
                        raise PublicationError("publication_action_claim_missing")
                    if _normalized_json(row.get("desired_state")) != desired:
                        raise PublicationError("publication_action_desired_conflict")
                    status = str(row.get("status") or "")
                    if status == "completed" and not reopen_completed:
                        connection.commit()
                        return PublicationActionClaim(
                            key,
                            PublicationActionDisposition.COMPLETED,
                            action_id=int(row["id"]),
                        )
                    if not bool(row.get("claimable")):
                        connection.commit()
                        return PublicationActionClaim(
                            key,
                            PublicationActionDisposition.BUSY,
                            action_id=int(row["id"]),
                        )
                    cursor.execute(
                        """
                        UPDATE a6_trade.trade_control_actions
                        SET status = 'started', error_class = NULL,
                            started_at = now(), completed_at = NULL,
                            elapsed_ms = NULL, updated_at = now()
                        WHERE id = %s
                        """,
                        (int(row["id"]),),
                    )
                elif _normalized_json(row.get("desired_state")) != desired:
                    raise PublicationError("publication_action_desired_conflict")
                action_id = int(row["id"])
                cursor.execute(
                    """
                    SELECT coalesce(max(attempt_no), 0) + 1 AS attempt_no
                    FROM a6_trade.trade_control_action_attempts
                    WHERE action_id = %s
                    """,
                    (action_id,),
                )
                attempt = _row(cursor)
                attempt_no = int((attempt or {}).get("attempt_no") or 1)
                cursor.execute(
                    """
                    INSERT INTO a6_trade.trade_control_action_attempts (
                        action_id, attempt_no, status, started_at
                    ) VALUES (%s, %s, 'started', now())
                    """,
                    (action_id, attempt_no),
                )
            connection.commit()
            return PublicationActionClaim(
                key,
                PublicationActionDisposition.CLAIMED,
                action_id=action_id,
                attempt_no=attempt_no,
            )
        except Exception:
            connection.rollback()
            raise
        finally:
            _close(connection)

    def complete(
        self,
        claim: PublicationActionClaim,
        *,
        observed_after: Mapping[str, Any],
        elapsed_ms: int,
    ) -> None:
        self._finish(
            claim,
            status="completed",
            observed_after=observed_after,
            error_class=None,
            elapsed_ms=elapsed_ms,
        )

    def fail(
        self,
        claim: PublicationActionClaim,
        *,
        error_class: str,
        elapsed_ms: int,
    ) -> None:
        self._finish(
            claim,
            status="failed",
            observed_after=None,
            error_class=_required_text(error_class, "publication_action_error_class_invalid"),
            elapsed_ms=elapsed_ms,
        )

    def _finish(
        self,
        claim: PublicationActionClaim,
        *,
        status: str,
        observed_after: Mapping[str, Any] | None,
        error_class: str | None,
        elapsed_ms: int,
    ) -> None:
        if (
            claim.disposition is not PublicationActionDisposition.CLAIMED
            or claim.action_id is None
            or claim.attempt_no is None
        ):
            raise PublicationError("publication_action_claim_invalid")
        elapsed_ms = max(int(elapsed_ms), 0)
        fingerprint = (
            hashlib.sha256(error_class.encode()).hexdigest() if error_class is not None else None
        )
        observed_json = _json(observed_after) if observed_after is not None else None
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE a6_trade.trade_control_actions
                    SET observed_after = %s::jsonb, status = %s, error_class = %s,
                        completed_at = now(), elapsed_ms = %s, updated_at = now()
                    WHERE id = %s AND status = 'started'
                    """,
                    (observed_json, status, error_class, elapsed_ms, claim.action_id),
                )
                if cursor.rowcount != 1:
                    raise PublicationError("publication_action_finish_cas_mismatch")
                cursor.execute(
                    """
                    UPDATE a6_trade.trade_control_action_attempts
                    SET observed_after = %s::jsonb, status = %s, error_class = %s,
                        error_fingerprint = %s, completed_at = now(), elapsed_ms = %s
                    WHERE action_id = %s AND attempt_no = %s AND status = 'started'
                    """,
                    (
                        observed_json,
                        status,
                        error_class,
                        fingerprint,
                        elapsed_ms,
                        claim.action_id,
                        claim.attempt_no,
                    ),
                )
                if cursor.rowcount != 1:
                    raise PublicationError("publication_action_attempt_finish_cas_mismatch")
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            _close(connection)


class A6PublicationClient(Protocol):
    def supplier_listings(
        self, user_id: int, *, page: int, size: int, status: int | None = None
    ) -> Mapping[str, Any]: ...

    def takedown_supplier_listing(self, user_id: int, listing_id: int) -> object: ...

    def resubmit_supplier_listing(self, user_id: int, listing_id: int) -> object: ...


class DirectA6PublicationProbeClient(Protocol):
    proxy_url: str

    def provider_responses_probe(
        self,
        api_key: str,
        model: str,
        *,
        max_output_tokens: int,
        timeout: float,
    ) -> tuple[int, Mapping[str, Any]]: ...


class A6SupplierListingPublicationRemote:
    """Primitive for one A6 supplier listing; compose it for model publication."""

    def __init__(
        self,
        listing_client: A6PublicationClient,
        *,
        probe_client: DirectA6PublicationProbeClient,
        user_id: int,
        listing_id: int,
        probe_api_key: str,
        page_size: int = 100,
        max_pages: int = 100,
        probe_timeout: float = 6,
        probe_max_output_tokens: int = 16,
    ) -> None:
        if user_id < 1 or listing_id < 1:
            raise ValueError("publication_listing_identity_invalid")
        if page_size < 1 or max_pages < 1:
            raise ValueError("publication_listing_pagination_invalid")
        if listing_client is probe_client:
            raise ValueError("publication_probe_client_not_independent")
        if not hasattr(probe_client, "proxy_url"):
            raise ValueError("publication_probe_directness_unverifiable")
        if str(probe_client.proxy_url or "").strip():
            raise ValueError("publication_probe_proxy_forbidden")
        self.listing_client = listing_client
        self.probe_client = probe_client
        self.user_id = int(user_id)
        self.listing_id = int(listing_id)
        self.publication_scope = f"a6-supplier:{self.user_id}:listing:{self.listing_id}"
        self.probe_api_key = _required_text(probe_api_key, "publication_probe_api_key_invalid")
        self.page_size = int(page_size)
        self.max_pages = int(max_pages)
        self.probe_timeout = float(probe_timeout)
        self.probe_max_output_tokens = int(probe_max_output_tokens)

    def read_listing(self, model: str) -> ListingPublicationState:
        model = _model(model)
        for page in range(1, self.max_pages + 1):
            document = self.listing_client.supplier_listings(
                self.user_id, page=page, size=self.page_size
            )
            items = document.get("items")
            if not isinstance(items, list):
                raise PublicationError("publication_listing_readback_invalid")
            for item in items:
                if (
                    not isinstance(item, Mapping)
                    or _positive_int(item.get("id")) != self.listing_id
                ):
                    continue
                remote_models = {
                    str(item.get(key) or "").strip()
                    for key in ("own_model", "platform_model")
                    if str(item.get(key) or "").strip()
                }
                if remote_models and model not in remote_models:
                    raise PublicationError("publication_listing_model_mismatch")
                status = _wire_text(item.get("status"))
                if status == "2" and all(
                    _wire_text(item.get(field)) == "1" for field in ("availability", "visibility")
                ):
                    return ListingPublicationState.PUBLISHED
                if status == "0":
                    return ListingPublicationState.UNPUBLISHED
                raise PublicationError("publication_listing_state_unknown")
            if len(items) < self.page_size:
                break
        raise PublicationError("publication_listing_missing")

    def set_listing_state(
        self,
        model: str,
        state: ListingPublicationState,
        *,
        idempotency_key: str,
    ) -> None:
        _model(model)
        _required_text(idempotency_key, "publication_idempotency_key_invalid")
        if state is ListingPublicationState.PUBLISHED:
            self.listing_client.resubmit_supplier_listing(self.user_id, self.listing_id)
        elif state is ListingPublicationState.UNPUBLISHED:
            self.listing_client.takedown_supplier_listing(self.user_id, self.listing_id)
        else:
            raise PublicationError("publication_listing_target_invalid")

    def probe(self, model: str) -> PublicationProbeResult:
        if str(self.probe_client.proxy_url or "").strip():
            raise PublicationError("publication_probe_proxy_forbidden")
        status, document = self.probe_client.provider_responses_probe(
            self.probe_api_key,
            _model(model),
            max_output_tokens=self.probe_max_output_tokens,
            timeout=self.probe_timeout,
        )
        if not 200 <= int(status) < 300 or document.get("probe_event") != "response.created":
            raise PublicationError("publication_real_probe_failed")
        request_id = _optional_text(document.get("request_id") or document.get("response_id"))
        return PublicationProbeResult(request_id=request_id)


class CompositePublicationRemote:
    """Fan out one model publication target to every managed seller listing."""

    def __init__(
        self,
        remotes: Sequence[PublicationRemote],
        *,
        probe_remote: PublicationRemote,
    ) -> None:
        self.remotes = tuple(remotes)
        if not self.remotes:
            raise ValueError("publication_remote_set_empty")
        scopes = tuple(
            _required_text(remote.publication_scope, "publication_scope_invalid")
            for remote in self.remotes
        )
        if len(set(scopes)) != len(scopes):
            raise ValueError("publication_remote_scope_duplicate")
        if not any(remote is probe_remote for remote in self.remotes):
            raise ValueError("publication_probe_remote_not_managed")
        self.probe_remote = probe_remote
        scope_hash = hashlib.sha256(_json(sorted(scopes)).encode()).hexdigest()
        self.publication_scope = f"a6-model-listings:{scope_hash[:32]}"

    def read_listing(self, model: str) -> ListingPublicationState:
        states = {remote.read_listing(model) for remote in self.remotes}
        if states == {ListingPublicationState.PUBLISHED}:
            return ListingPublicationState.PUBLISHED
        if states == {ListingPublicationState.UNPUBLISHED}:
            return ListingPublicationState.UNPUBLISHED
        return ListingPublicationState.MIXED

    def set_listing_state(
        self,
        model: str,
        state: ListingPublicationState,
        *,
        idempotency_key: str,
    ) -> None:
        if state not in {
            ListingPublicationState.PUBLISHED,
            ListingPublicationState.UNPUBLISHED,
        }:
            raise PublicationError("publication_listing_target_invalid")
        for remote in self.remotes:
            if remote.read_listing(model) is state:
                continue
            remote.set_listing_state(
                model,
                state,
                idempotency_key=f"{idempotency_key}:{_scope_suffix(remote.publication_scope)}",
            )

    def probe(self, model: str) -> PublicationProbeResult:
        return self.probe_remote.probe(model)


class SellerCostGuardPublicationRemote:
    """Expose individualized Listing states as one converged model-level target."""

    def __init__(
        self,
        remotes: Mapping[tuple[int, int], PublicationRemote],
        *,
        plan: SellerCostGuardPlan,
        probe_remote: PublicationRemote,
    ) -> None:
        self.plan = plan
        self.remotes = dict(remotes)
        self.targets = {
            (target.listing.a6_user_id, target.listing.listing_id): target
            for target in plan.targets
        }
        if set(self.remotes) != set(self.targets):
            raise ValueError("publication_cost_guard_remote_set_mismatch")
        if not any(
            target.desired_state is ListingPublicationState.PUBLISHED
            and self.remotes[identity] is probe_remote
            for identity, target in self.targets.items()
        ):
            raise ValueError("publication_cost_guard_probe_remote_not_publishable")
        self.probe_remote = probe_remote
        scope_document = [
            {
                "scope": remote.publication_scope,
                "target": self.targets[identity].desired_state.value,
            }
            for identity, remote in sorted(self.remotes.items())
        ]
        scope_hash = hashlib.sha256(_json(scope_document).encode()).hexdigest()
        self.publication_scope = f"a6-cost-guarded-listings:{scope_hash[:32]}"

    def _desired_states(
        self, model_target: ListingPublicationState
    ) -> dict[tuple[int, int], ListingPublicationState]:
        if model_target is ListingPublicationState.UNPUBLISHED:
            return {identity: ListingPublicationState.UNPUBLISHED for identity in self.remotes}
        if model_target is not ListingPublicationState.PUBLISHED:
            raise PublicationError("publication_listing_target_invalid")
        return {identity: target.desired_state for identity, target in self.targets.items()}

    def read_listing(self, model: str) -> ListingPublicationState:
        model = _model(model)
        desired = self._desired_states(ListingPublicationState.PUBLISHED)
        states = {identity: remote.read_listing(model) for identity, remote in self.remotes.items()}
        if all(states[identity] is target for identity, target in desired.items()):
            return ListingPublicationState.PUBLISHED
        if all(state is ListingPublicationState.UNPUBLISHED for state in states.values()):
            return ListingPublicationState.UNPUBLISHED
        return ListingPublicationState.MIXED

    def set_listing_state(
        self,
        model: str,
        state: ListingPublicationState,
        *,
        idempotency_key: str,
    ) -> None:
        desired = self._desired_states(state)
        for identity, remote in self.remotes.items():
            target = desired[identity]
            if remote.read_listing(model) is target:
                continue
            remote.set_listing_state(
                model,
                target,
                idempotency_key=f"{idempotency_key}:{_scope_suffix(remote.publication_scope)}",
            )

    def probe(self, model: str) -> PublicationProbeResult:
        return self.probe_remote.probe(model)


class PublicationController:
    """Converge one desired publication record without holding a remote-call DB tx."""

    def __init__(
        self,
        store: ObservablePublicationStore,
        remote: PublicationRemote,
        *,
        ledger: PublicationActionLedger | None = None,
        model_lease: PublicationModelLease | None = None,
    ) -> None:
        self.store = store
        self.remote = remote
        self.publication_scope = _required_text(
            remote.publication_scope, "publication_scope_invalid"
        )
        self.ledger = ledger or InMemoryPublicationActionLedger()
        self.model_lease = model_lease
        self.desired = PublicationReconciler(store)

    def reconcile(self, decision: PublicationDecision) -> PublicationReconcileReport:
        lease = (
            self.model_lease.hold(decision.model) if self.model_lease is not None else nullcontext()
        )
        with lease:
            return self._reconcile_locked(decision)

    def _reconcile_locked(self, decision: PublicationDecision) -> PublicationReconcileReport:
        record = self.desired.reconcile(decision)
        if record.publication_scope != self.publication_scope:
            record = self.store.bind_scope(
                decision.model,
                self.publication_scope,
                expected_state_version=record.state_version,
            )
        target = _target_listing_state(decision.desired_state)
        action = "publish" if target is ListingPublicationState.PUBLISHED else "unpublish"
        observed_before = self.remote.read_listing(decision.model)
        probe_already_confirmed = (
            target is ListingPublicationState.UNPUBLISHED or record.last_real_probe_at is not None
        )
        if (
            observed_before is target
            and record.observed_state is decision.desired_state
            and probe_already_confirmed
        ):
            return PublicationReconcileReport(
                record=record,
                target_listing_state=target,
                observed_listing_state=observed_before,
                action=action,
                action_disposition="noop",
                remote_write_performed=False,
                probe_request_id=record.last_real_probe_request_id,
            )

        claim = self.ledger.claim(
            decision,
            target,
            publication_scope=self.publication_scope,
            reopen_completed=observed_before is not target,
            # The session advisory lease proves that no earlier controller is
            # still executing this model's remote saga.  A surviving started
            # ledger row therefore belongs to a terminated process and can be
            # reclaimed immediately instead of waiting beyond the 5/10s SLO.
            recover_in_progress=self.model_lease is not None,
        )
        if claim.disposition is PublicationActionDisposition.BUSY:
            return PublicationReconcileReport(
                record=record,
                target_listing_state=target,
                observed_listing_state=observed_before,
                action=action,
                action_disposition=claim.disposition.value,
                remote_write_performed=False,
            )

        started = time.monotonic()
        remote_write = False
        ledger_finished = claim.disposition is PublicationActionDisposition.COMPLETED
        probe = PublicationProbeResult()
        try:
            if claim.disposition is PublicationActionDisposition.CLAIMED:
                record = self.store.mark_action_started(
                    decision.model,
                    expected_state_version=record.state_version,
                )
            if target is ListingPublicationState.PUBLISHED:
                probe = self.remote.probe(decision.model)
            if observed_before is not target:
                if claim.disposition is PublicationActionDisposition.COMPLETED:
                    raise PublicationError("publication_completed_action_remote_drift")
                self.remote.set_listing_state(
                    decision.model,
                    target,
                    idempotency_key=claim.idempotency_key,
                )
                remote_write = True
            observed_after = self.remote.read_listing(decision.model)
            if observed_after is not target:
                raise PublicationError("publication_listing_readback_mismatch")
            elapsed_ms = round((time.monotonic() - started) * 1000)
            if claim.disposition is PublicationActionDisposition.CLAIMED:
                self.ledger.complete(
                    claim,
                    observed_after={
                        "listing_state": observed_after.value,
                        "probe_request_id": probe.request_id,
                    },
                    elapsed_ms=elapsed_ms,
                )
                ledger_finished = True
            confirmed = self.store.confirm(
                decision.model,
                decision.desired_state,
                expected_state_version=record.state_version,
                real_probe_confirmed=(target is ListingPublicationState.PUBLISHED),
                probe_request_id=probe.request_id,
            )
            return PublicationReconcileReport(
                record=confirmed,
                target_listing_state=target,
                observed_listing_state=observed_after,
                action=action,
                action_disposition=claim.disposition.value,
                remote_write_performed=remote_write,
                probe_request_id=probe.request_id,
            )
        except Exception as exc:
            if claim.disposition is PublicationActionDisposition.CLAIMED and not ledger_finished:
                self.ledger.fail(
                    claim,
                    error_class=_error_code(exc),
                    elapsed_ms=round((time.monotonic() - started) * 1000),
                )
            if isinstance(exc, PublicationError):
                raise
            raise PublicationError(_error_code(exc)) from exc


def _model(value: object) -> str:
    model = str(value or "").strip()
    if not model:
        raise PublicationError("publication_model_invalid")
    return model


def _guard_decimal(value: object, code: str) -> Decimal:
    if isinstance(value, bool):
        raise PublicationError(code)
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise PublicationError(code) from None
    if not parsed.is_finite():
        raise PublicationError(code)
    return parsed


def _state(value: object, error: str) -> PublicationState:
    try:
        return value if isinstance(value, PublicationState) else PublicationState(str(value))
    except ValueError:
        raise PublicationError(error) from None


def _target_listing_state(state: PublicationState) -> ListingPublicationState:
    if state in {PublicationState.PUBLISHED, PublicationState.DEGRADED}:
        return ListingPublicationState.PUBLISHED
    return ListingPublicationState.UNPUBLISHED


def _publication_action_document(
    decision: PublicationDecision,
    target: ListingPublicationState,
    publication_scope: str,
) -> dict[str, Any]:
    return {
        "model": decision.model,
        "publication_scope": _required_text(publication_scope, "publication_scope_invalid"),
        "target_listing_state": target.value,
        "desired_state": decision.desired_state.value,
        "generation": decision.generation,
        "cost_generation": decision.cost_generation,
        "healthy_slots": decision.healthy_slots,
        "minimum_healthy_slots": decision.minimum_healthy_slots,
        "redundancy_target": decision.redundancy_target,
        "reason_code": decision.reason_code,
    }


def _publication_action_key(
    decision: PublicationDecision,
    target: ListingPublicationState,
    publication_scope: str,
) -> str:
    document = _publication_action_document(decision, target, publication_scope)
    digest = hashlib.sha256(_json(document).encode()).hexdigest()
    generation = decision.generation if decision.generation is not None else "none"
    return f"model-publication:{decision.model}:g{generation}:{target.value}:{digest[:24]}"


def _scope_suffix(publication_scope: str) -> str:
    return hashlib.sha256(
        _required_text(publication_scope, "publication_scope_invalid").encode()
    ).hexdigest()[:16]


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _normalized_json(value: object) -> object:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return value
    if isinstance(value, Mapping):
        return {str(key): _normalized_json(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_normalized_json(item) for item in value]
    return value


def _required_text(value: object, error: str) -> str:
    result = str(value or "").strip()
    if not result:
        raise ValueError(error)
    return result


def _optional_text(value: object) -> str | None:
    result = str(value or "").strip()
    return result or None


def _wire_text(value: object) -> str:
    return "" if value is None else str(value).strip()


def _positive_int(value: object) -> int | None:
    try:
        result = int(value)
    except (TypeError, ValueError):
        return None
    return result if result > 0 else None


def _error_code(exc: Exception) -> str:
    code = getattr(exc, "code", None)
    return str(code or exc.__class__.__name__).strip() or "publication_remote_failed"


def _record_from_row(row: Mapping[str, Any]) -> PublicationRecord:
    try:
        desired = _state(row.get("desired_state"), "publication_desired_state_invalid")
        observed = _state(row.get("observed_state"), "publication_observed_state_invalid")
        healthy = int(row["healthy_slots"])
        minimum = int(row["min_healthy_slots"])
        redundancy_target = int(row["redundancy_target"])
    except (KeyError, TypeError, ValueError):
        raise PublicationError("publication_row_invalid") from None
    if healthy < 0 or minimum < 1 or redundancy_target < minimum:
        raise PublicationError("publication_row_invalid")
    generation = row.get("desired_generation")
    if generation is not None:
        try:
            generation = int(generation)
        except (TypeError, ValueError):
            raise PublicationError("publication_row_invalid") from None
        if generation < 1:
            raise PublicationError("publication_row_invalid")
    cost_generation = row.get("desired_cost_generation")
    if cost_generation is not None:
        try:
            cost_generation = int(cost_generation)
        except (TypeError, ValueError):
            raise PublicationError("publication_row_invalid") from None
        if cost_generation < 1:
            raise PublicationError("publication_row_invalid")
    try:
        state_version = int(row.get("state_version", 1))
    except (TypeError, ValueError):
        raise PublicationError("publication_row_invalid") from None
    if state_version < 1:
        raise PublicationError("publication_row_invalid")
    return PublicationRecord(
        model=_model(row.get("model")),
        desired_state=desired,
        observed_state=observed,
        healthy_slots=healthy,
        minimum_healthy_slots=minimum,
        redundancy_target=redundancy_target,
        reason_code=str(row.get("reason_code") or ""),
        generation=generation,
        cost_generation=cost_generation,
        state_version=state_version,
        publication_scope=_optional_text(row.get("publication_scope")),
        action_started_at=row.get("action_started_at"),
        confirmed_at=row.get("confirmed_at"),
        last_real_probe_at=row.get("last_real_probe_at"),
        last_real_probe_request_id=_optional_text(row.get("last_real_probe_request_id")),
    )


def _row(cursor: Any) -> dict[str, Any] | None:
    row = cursor.fetchone()
    if row is None:
        return None
    if isinstance(row, Mapping):
        return dict(row)
    names = [column.name for column in cursor.description or ()]
    return dict(zip(names, row, strict=False))


def _rows(cursor: Any) -> list[dict[str, Any]]:
    rows = cursor.fetchall()
    if not rows:
        return []
    if isinstance(rows[0], Mapping):
        return [dict(row) for row in rows]
    names = [column.name for column in cursor.description or ()]
    return [dict(zip(names, row, strict=False)) for row in rows]


def _boolean_row(cursor: Any) -> bool:
    row = cursor.fetchone()
    if isinstance(row, Mapping):
        value = next(iter(row.values()), False)
    elif isinstance(row, (tuple, list)):
        value = row[0] if row else False
    else:
        value = row
    return bool(value)


def _close(connection: Any) -> None:
    close = getattr(connection, "close", None)
    if callable(close):
        close()
