"""PostgreSQL persistence for Provider routing/cost generation bundles."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Protocol

from .trade_provider_cost import DEFAULT_COST_QUANTUM, CostPlan
from .trade_provider_inventory import project_active_provider_inventory
from .trade_provider_pool import (
    GenerationState,
    PricingState,
    ProviderActivationRollback,
    ProviderBundle,
    ProviderPoolError,
    ProviderSlot,
    SlotState,
)


@dataclass(frozen=True)
class ActiveBundleState:
    model: str
    routing_generation: int
    cost_generation: int
    source_hash: str
    cost_input_hash: str
    cost_output_hash: str
    desired_size: int


@dataclass(frozen=True)
class PlannedBundleState:
    routing_generation_id: int
    cost_generation_id: int
    routing_generation: int
    cost_generation: int


class ApplyingPoolProjection(Protocol):
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
    generation_state: str
    pricing_state: str


class PostgresProviderPoolStore:
    """Write desired state with short transactions and explicit generation CAS."""

    def __init__(
        self,
        connection_factory: Callable[[], Any],
        *,
        max_apply_age_seconds: int = 900,
    ) -> None:
        if (
            isinstance(max_apply_age_seconds, bool)
            or not isinstance(max_apply_age_seconds, int)
            or not 1 <= max_apply_age_seconds <= 86_400
        ):
            raise ValueError("provider_apply_max_age_invalid")
        self.connection_factory = connection_factory
        self.max_apply_age_seconds = max_apply_age_seconds

    def active(self, model: str) -> ActiveBundleState | None:
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT generation.model, generation.generation,
                           generation.cost_generation, generation.source_hash,
                           cost.cost_input_hash, cost.cost_output_hash,
                           generation.desired_size
                    FROM a6_trade.provider_pool_generations generation
                    JOIN a6_trade.provider_cost_generations cost
                      ON cost.id = generation.cost_generation_id
                    WHERE generation.model = %s
                      AND generation.generation_state = 'active'
                      AND cost.pricing_state = 'active'
                    """,
                    (str(model),),
                )
                row = _row(cursor)
            return None if row is None else _active_state(row)
        finally:
            _close(connection)

    def load_bundle(
        self,
        model: str,
        *,
        routing_generation: int,
        cost_generation: int,
    ) -> ProviderBundle:
        """Load the exact persisted bundle used for remote compensation."""

        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT generation.model, generation.generation,
                           generation.parent_generation,
                           generation.generation_source, generation.source_hash,
                           generation.cost_generation, generation.desired_size,
                           generation.ordered_channel_ids,
                           generation.standby_channel_ids,
                           generation.generation_state,
                           generation.config_version,
                           cost.parent_cost_generation, cost.cost_input_hash,
                           cost.cost_output_hash, cost.token_mix_version,
                           cost.exchange_rate_version, cost.desired_slot_costs,
                           cost.route_cost_multiplier,
                           cost.retry_reserve_multiplier,
                           cost.realized_cost_p95_multiplier,
                           cost.pool_cost_multiplier,
                           cost.billing_floor_multiplier,
                           cost.transition_floor_multiplier,
                           cost.margin_bps, cost.headroom_bps,
                           cost.pricing_state
                    FROM a6_trade.provider_pool_generations generation
                    JOIN a6_trade.provider_cost_generations cost
                      ON cost.id = generation.cost_generation_id
                     AND cost.model = generation.model
                     AND cost.cost_generation = generation.cost_generation
                    WHERE generation.model = %s
                      AND generation.generation = %s
                      AND generation.cost_generation = %s
                    """,
                    (str(model), int(routing_generation), int(cost_generation)),
                )
                generation = _row(cursor)
                if generation is None:
                    raise ProviderPoolError("provider_bundle_not_found")
                cursor.execute(
                    """
                    SELECT slot_no, enodot_binding_id, enodot_account_id,
                           a6_token_id, desired_channel_id, desired_rank,
                           desired_priority, desired_slot_cost_multiplier,
                           observed_rate_multiplier, funding_eligible,
                           slot_state, desired_schedulable,
                           observed_schedulable, confirmed_pin_binding_id,
                           pin_confirmed, probe_confirmed, state_version
                    FROM a6_trade.provider_pool_slots
                    WHERE model = %s AND routing_generation = %s
                      AND valid_to IS NULL
                    ORDER BY slot_no
                    """,
                    (str(model), int(routing_generation)),
                )
                slots = _rows(cursor)
            return _bundle_from_rows(generation, slots)
        finally:
            _close(connection)

    def plan(
        self,
        bundle: ProviderBundle,
        *,
        expected_routing_generation: int | None,
        expected_cost_generation: int | None,
        cost_input_hash: str | None = None,
        token_mix_version: str | None = None,
        exchange_rate_version: str | None = None,
    ) -> PlannedBundleState:
        if bundle.parent_routing_generation != expected_routing_generation:
            raise ProviderPoolError("provider_bundle_parent_routing_mismatch")
        if bundle.parent_cost_generation != expected_cost_generation:
            raise ProviderPoolError("provider_bundle_parent_cost_mismatch")
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                _lock_model(cursor, bundle.model)
                current = _active_row(cursor, bundle.model, for_update=True)
                _check_active_cas(
                    current,
                    expected_routing_generation=expected_routing_generation,
                    expected_cost_generation=expected_cost_generation,
                )
                cursor.execute(
                    """
                    SELECT generation.id AS routing_generation_id,
                           generation.cost_generation_id,
                           generation.parent_generation,
                           generation.generation_source,
                           generation.source_hash,
                           generation.cost_generation,
                           generation.desired_size,
                           generation.ordered_channel_ids,
                           generation.standby_channel_ids,
                           generation.config_version,
                           cost.parent_cost_generation,
                           cost.cost_input_hash,
                           cost.cost_output_hash,
                           cost.config_version AS cost_config_version
                    FROM a6_trade.provider_pool_generations generation
                    JOIN a6_trade.provider_cost_generations cost
                      ON cost.id = generation.cost_generation_id
                    WHERE generation.model = %s AND generation.generation = %s
                    """,
                    (bundle.model, bundle.routing_generation),
                )
                existing = _row(cursor)
                semantic_cost_hash = cost_input_hash or bundle.effective_cost_input_hash
                if existing is not None:
                    if (
                        str(existing["source_hash"]) != bundle.source_hash
                        or int(existing["cost_generation"]) != bundle.cost_generation
                        or _optional_int(existing["parent_generation"])
                        != bundle.parent_routing_generation
                        or _optional_int(existing["parent_cost_generation"])
                        != bundle.parent_cost_generation
                        or str(existing["generation_source"]) != bundle.generation_source
                        or int(existing["desired_size"]) != bundle.desired_size
                        or tuple(existing["ordered_channel_ids"] or ())
                        != bundle.ordered_channel_ids
                        or tuple(existing["standby_channel_ids"] or ())
                        != bundle.standby_channel_ids
                        or str(existing["config_version"]) != bundle.config_version
                        or str(existing["cost_config_version"]) != bundle.config_version
                        or str(existing["cost_input_hash"]) != semantic_cost_hash
                        or str(existing["cost_output_hash"]) != bundle.cost_plan.cost_output_hash
                    ):
                        raise ProviderPoolError("provider_bundle_generation_conflict")
                    connection.commit()
                    return PlannedBundleState(
                        int(existing["routing_generation_id"]),
                        int(existing["cost_generation_id"]),
                        bundle.routing_generation,
                        bundle.cost_generation,
                    )

                _supersede_stale_plans(cursor, bundle.model)
                cursor.execute(
                    """
                    INSERT INTO a6_trade.provider_cost_generations (
                        model, cost_generation, parent_cost_generation, cost_source,
                        cost_input_hash, cost_output_hash,
                        token_mix_version, exchange_rate_version,
                        active_slot_nos, desired_slot_costs,
                        route_cost_multiplier, retry_reserve_multiplier,
                        realized_cost_p95_multiplier, pool_cost_multiplier,
                        margin_bps, headroom_bps, billing_floor_multiplier,
                        transition_floor_multiplier, pricing_state, config_version
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s::jsonb, %s, %s, %s, %s, %s, %s, %s, %s,
                        'planned', %s
                    )
                    RETURNING id
                    """,
                    (
                        bundle.model,
                        bundle.cost_generation,
                        expected_cost_generation,
                        bundle.generation_source,
                        semantic_cost_hash,
                        bundle.cost_plan.cost_output_hash,
                        token_mix_version or bundle.token_mix_version,
                        exchange_rate_version or bundle.exchange_rate_version,
                        list(bundle.cost_plan.desired_slot_costs),
                        _json(
                            {
                                str(slot): format(value, "f")
                                for slot, value in bundle.cost_plan.desired_slot_costs.items()
                            }
                        ),
                        bundle.cost_plan.route_cost_multiplier,
                        bundle.cost_plan.retry_reserve_multiplier,
                        bundle.cost_plan.realized_cost_p95_multiplier,
                        bundle.cost_plan.pool_cost_multiplier,
                        bundle.cost_plan.margin_bps,
                        bundle.cost_plan.headroom_bps,
                        bundle.cost_plan.billing_floor_multiplier,
                        bundle.cost_plan.transition_floor_multiplier,
                        bundle.config_version,
                    ),
                )
                cost_row = _row(cursor)
                if cost_row is None:
                    raise ProviderPoolError("provider_cost_generation_write_failed")
                cost_id = int(cost_row["id"])
                cursor.execute(
                    """
                    INSERT INTO a6_trade.provider_pool_generations (
                        model, generation, parent_generation, generation_source,
                        source_hash, cost_generation_id, cost_generation,
                        desired_size, ordered_channel_ids, standby_channel_ids,
                        generation_state, config_version
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, 'planned', %s
                    )
                    RETURNING id
                    """,
                    (
                        bundle.model,
                        bundle.routing_generation,
                        expected_routing_generation,
                        bundle.generation_source,
                        bundle.source_hash,
                        cost_id,
                        bundle.cost_generation,
                        bundle.desired_size,
                        list(bundle.ordered_channel_ids),
                        list(bundle.standby_channel_ids),
                        bundle.config_version,
                    ),
                )
                generation_row = _row(cursor)
                if generation_row is None:
                    raise ProviderPoolError("provider_routing_generation_write_failed")
            connection.commit()
            return PlannedBundleState(
                int(generation_row["id"]),
                cost_id,
                bundle.routing_generation,
                bundle.cost_generation,
            )
        except Exception:
            connection.rollback()
            raise
        finally:
            _close(connection)

    def mark_applying(self, bundle: ProviderBundle) -> None:
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                _lock_model(cursor, bundle.model)
                plan_document = _bundle_plan_document(bundle)
                plan_key = _applying_plan_key(
                    bundle.model,
                    bundle.routing_generation,
                    bundle.cost_generation,
                )
                cursor.execute(
                    """
                    INSERT INTO a6_trade.trade_control_actions (
                        idempotency_key, action_type, entity_type, entity_id,
                        desired_state, observed_after, status, config_version,
                        requested_by, started_at, completed_at
                    ) VALUES (
                        %s, 'provider_pool_generation_plan',
                        'provider_pool_generation', %s,
                        %s::jsonb, %s::jsonb, 'completed', %s,
                        'provider-pool-apply', now(), now()
                    )
                    ON CONFLICT (idempotency_key) DO NOTHING
                    RETURNING desired_state, config_version
                    """,
                    (
                        plan_key,
                        f"{bundle.model}:{bundle.routing_generation}:{bundle.cost_generation}",
                        json.dumps(plan_document, ensure_ascii=True, sort_keys=True),
                        json.dumps(plan_document, ensure_ascii=True, sort_keys=True),
                        bundle.config_version,
                    ),
                )
                persisted_plan = _row(cursor)
                if persisted_plan is None:
                    cursor.execute(
                        """
                        SELECT desired_state, config_version
                        FROM a6_trade.trade_control_actions
                        WHERE idempotency_key = %s
                          AND action_type = 'provider_pool_generation_plan'
                          AND status = 'completed'
                        FOR UPDATE
                        """,
                        (plan_key,),
                    )
                    persisted_plan = _row(cursor)
                if (
                    persisted_plan is None
                    or str(persisted_plan.get("config_version") or "")
                    != bundle.config_version
                    or _json_object(persisted_plan.get("desired_state")) != plan_document
                ):
                    raise ProviderPoolError("provider_applying_plan_conflict")
                cursor.execute(
                    """
                    UPDATE a6_trade.provider_cost_generations
                    SET pricing_state = CASE
                            WHEN pricing_state = 'planned' THEN 'applying'
                            ELSE pricing_state
                        END,
                        updated_at = now()
                    WHERE model = %s AND cost_generation = %s
                      AND parent_cost_generation IS NOT DISTINCT FROM %s
                      AND (
                          pricing_state IN ('applying', 'confirmed')
                          OR (
                              pricing_state = 'planned'
                              AND planned_at >= now() - (%s * interval '1 second')
                          )
                      )
                    """,
                    (
                        bundle.model,
                        bundle.cost_generation,
                        bundle.parent_cost_generation,
                        self.max_apply_age_seconds,
                    ),
                )
                if cursor.rowcount != 1:
                    raise ProviderPoolError("provider_cost_generation_apply_cas_mismatch")
                cursor.execute(
                    """
                    UPDATE a6_trade.provider_pool_generations
                    SET generation_state = 'applying', updated_at = now()
                    WHERE model = %s AND generation = %s
                      AND parent_generation IS NOT DISTINCT FROM %s
                      AND (
                          generation_state = 'applying'
                          OR (
                              generation_state = 'planned'
                              AND planned_at >= now() - (%s * interval '1 second')
                          )
                      )
                    """,
                    (
                        bundle.model,
                        bundle.routing_generation,
                        bundle.parent_routing_generation,
                        self.max_apply_age_seconds,
                    ),
                )
                if cursor.rowcount != 1:
                    raise ProviderPoolError("provider_routing_generation_apply_cas_mismatch")
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            _close(connection)

    def load_applying_bundle(
        self,
        model: str,
        *,
        routing_generation: int,
        cost_generation: int,
    ) -> ProviderBundle | None:
        """Load the immutable bundle captured before the first remote mutation."""

        plan_key = _applying_plan_key(model, routing_generation, cost_generation)
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT action.desired_state, action.config_version,
                           generation.generation_state, cost.pricing_state
                    FROM a6_trade.trade_control_actions action
                    JOIN a6_trade.provider_pool_generations generation
                      ON generation.model = %s AND generation.generation = %s
                    JOIN a6_trade.provider_cost_generations cost
                      ON cost.id = generation.cost_generation_id
                     AND cost.cost_generation = %s
                    WHERE action.idempotency_key = %s
                      AND action.action_type = 'provider_pool_generation_plan'
                      AND action.status = 'completed'
                      AND generation.generation_state = 'applying'
                      AND cost.pricing_state IN ('applying', 'confirmed')
                    """,
                    (model, routing_generation, cost_generation, plan_key),
                )
                row = _row(cursor)
            if row is None:
                return None
            bundle = _bundle_from_plan_document(_json_object(row.get("desired_state")))
            if (
                bundle.model != model
                or bundle.routing_generation != routing_generation
                or bundle.cost_generation != cost_generation
                or bundle.config_version != str(row.get("config_version") or "")
            ):
                raise ProviderPoolError("provider_applying_plan_identity_mismatch")
            return bundle
        finally:
            _close(connection)

    def fail_applying(self, bundle: ProviderBundle, *, reason: str) -> None:
        """CAS-fail one unpublished Pin plan so the controller can replan it."""

        self._fail_applying_state(
            model=bundle.model,
            routing_generation=bundle.routing_generation,
            cost_generation=bundle.cost_generation,
            parent_routing_generation=bundle.parent_routing_generation,
            parent_cost_generation=bundle.parent_cost_generation,
            generation_source=bundle.generation_source,
            source_hash=bundle.source_hash,
            desired_size=bundle.desired_size,
            ordered_channel_ids=bundle.ordered_channel_ids,
            standby_channel_ids=bundle.standby_channel_ids,
            config_version=bundle.config_version,
            cost_input_hash=bundle.effective_cost_input_hash,
            cost_output_hash=bundle.cost_plan.cost_output_hash,
            expected_pricing_state=None,
            reason=reason,
        )

    def fail_applying_projection(
        self,
        model: str,
        projection: ApplyingPoolProjection,
        *,
        reason: str,
    ) -> None:
        """Fail the exact persisted applying projection when live evidence invalidates it."""

        try:
            generation_state = str(projection.generation_state)
            pricing_state = str(projection.pricing_state)
            if generation_state != "applying" or pricing_state not in {
                "applying",
                "confirmed",
            }:
                raise ProviderPoolError(
                    "provider_generation_failure_projection_invalid"
                )
            self._fail_applying_state(
                model=str(model),
                routing_generation=int(projection.routing_generation),
                cost_generation=int(projection.cost_generation),
                parent_routing_generation=projection.parent_routing_generation,
                parent_cost_generation=projection.parent_cost_generation,
                generation_source=str(projection.generation_source),
                source_hash=str(projection.source_hash),
                desired_size=int(projection.desired_size),
                ordered_channel_ids=tuple(
                    int(value)
                    for value in projection.ordered_channel_ids
                ),
                standby_channel_ids=tuple(
                    int(value)
                    for value in projection.standby_channel_ids
                ),
                config_version=str(projection.config_version),
                cost_input_hash=str(projection.cost_input_hash),
                cost_output_hash=str(projection.cost_output_hash),
                expected_pricing_state=pricing_state,
                reason=reason,
            )
        except (AttributeError, TypeError, ValueError):
            raise ProviderPoolError(
                "provider_generation_failure_projection_invalid"
            ) from None

    def _fail_applying_state(
        self,
        *,
        model: str,
        routing_generation: int,
        cost_generation: int,
        parent_routing_generation: int | None,
        parent_cost_generation: int | None,
        generation_source: str,
        source_hash: str,
        desired_size: int,
        ordered_channel_ids: tuple[int, ...],
        standby_channel_ids: tuple[int, ...],
        config_version: str,
        cost_input_hash: str,
        cost_output_hash: str,
        expected_pricing_state: str | None,
        reason: str,
    ) -> None:
        reason = str(reason or "").strip()
        if not reason:
            raise ProviderPoolError("provider_generation_failure_reason_invalid")
        if expected_pricing_state is not None and expected_pricing_state not in {
            "applying",
            "confirmed",
        }:
            raise ProviderPoolError("provider_generation_failure_projection_invalid")
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                _lock_model(cursor, model)
                current = _active_row(cursor, model, for_update=True)
                _check_active_cas(
                    current,
                    expected_routing_generation=parent_routing_generation,
                    expected_cost_generation=parent_cost_generation,
                )
                cursor.execute(
                    """
                    SELECT generation.id AS generation_id,
                           generation.cost_generation_id,
                           generation.parent_generation,
                           generation.generation_source,
                           generation.source_hash,
                           generation.cost_generation,
                           generation.desired_size,
                           generation.ordered_channel_ids,
                           generation.standby_channel_ids,
                           generation.generation_state,
                           generation.config_version AS routing_config_version,
                           cost.parent_cost_generation,
                           cost.cost_source,
                           cost.cost_input_hash,
                           cost.cost_output_hash,
                           cost.pricing_state,
                           cost.config_version AS cost_config_version
                    FROM a6_trade.provider_pool_generations generation
                    JOIN a6_trade.provider_cost_generations cost
                      ON cost.id = generation.cost_generation_id
                     AND cost.model = generation.model
                     AND cost.cost_generation = generation.cost_generation
                    WHERE generation.model = %s AND generation.generation = %s
                    FOR UPDATE OF generation, cost
                    """,
                    (model, routing_generation),
                )
                candidate = _row(cursor)
                observed_pricing_state = (
                    str(candidate["pricing_state"])
                    if candidate is not None
                    else ""
                )
                if candidate is None or any(
                    (
                        _optional_strict_int(candidate["parent_generation"])
                        != parent_routing_generation,
                        str(candidate["generation_source"]) != generation_source,
                        str(candidate["source_hash"]) != source_hash,
                        _strict_int(candidate["cost_generation"])
                        != cost_generation,
                        _strict_int(candidate["desired_size"]) != desired_size,
                        tuple(int(value) for value in candidate["ordered_channel_ids"])
                        != ordered_channel_ids,
                        tuple(int(value) for value in candidate["standby_channel_ids"])
                        != standby_channel_ids,
                        str(candidate["generation_state"]) != "applying",
                        str(candidate["routing_config_version"]) != config_version,
                        _optional_strict_int(candidate["parent_cost_generation"])
                        != parent_cost_generation,
                        str(candidate["cost_source"]) != generation_source,
                        str(candidate["cost_input_hash"]) != cost_input_hash,
                        str(candidate["cost_output_hash"]) != cost_output_hash,
                        observed_pricing_state not in {"applying", "confirmed"},
                        expected_pricing_state is not None
                        and observed_pricing_state != expected_pricing_state,
                        str(candidate["cost_config_version"]) != config_version,
                    )
                ):
                    raise ProviderPoolError(
                        "provider_generation_failure_bundle_mismatch"
                    )
                cursor.execute(
                    """
                    SELECT count(*) AS slot_count
                    FROM a6_trade.provider_pool_slots
                    WHERE model = %s AND routing_generation = %s
                      AND valid_to IS NULL
                    """,
                    (model, routing_generation),
                )
                slots = _row(cursor) or {}
                if _strict_int(slots.get("slot_count")) != 0:
                    raise ProviderPoolError(
                        "provider_generation_failure_slots_present"
                    )
                cursor.execute(
                    """
                    UPDATE a6_trade.provider_cost_generations
                    SET pricing_state = 'failed', updated_at = now()
                    WHERE id = %s AND pricing_state = %s
                    """,
                    (
                        _strict_int(candidate["cost_generation_id"]),
                        observed_pricing_state,
                    ),
                )
                if cursor.rowcount != 1:
                    raise ProviderPoolError(
                        "provider_cost_generation_failure_cas_mismatch"
                    )
                cursor.execute(
                    """
                    UPDATE a6_trade.provider_pool_generations
                    SET generation_state = 'failed', updated_at = now()
                    WHERE id = %s AND generation_state = 'applying'
                    """,
                    (_strict_int(candidate["generation_id"]),),
                )
                if cursor.rowcount != 1:
                    raise ProviderPoolError(
                        "provider_routing_generation_failure_cas_mismatch"
                    )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            _close(connection)

    def confirm_cost(
        self,
        bundle: ProviderBundle,
        *,
        observed_output_hash: str,
        observed_billing_floor: Decimal,
    ) -> None:
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE a6_trade.provider_cost_generations
                    SET pricing_state = 'confirmed',
                        observed_billing_floor_multiplier = %s,
                        confirmed_at = now(), updated_at = now()
                    WHERE model = %s AND cost_generation = %s
                      AND parent_cost_generation IS NOT DISTINCT FROM %s
                      AND pricing_state IN ('applying', 'confirmed')
                      AND cost_output_hash = %s
                      AND billing_floor_multiplier <= %s
                    """,
                    (
                        observed_billing_floor,
                        bundle.model,
                        bundle.cost_generation,
                        bundle.parent_cost_generation,
                        observed_output_hash,
                        observed_billing_floor,
                    ),
                )
                if cursor.rowcount != 1:
                    raise ProviderPoolError("provider_cost_generation_confirm_cas_mismatch")
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            _close(connection)

    def activate(
        self,
        bundle: ProviderBundle,
        *,
        deferred_slot_nos: Sequence[int] = (),
    ) -> ActiveBundleState:
        """Atomically replace current slots and activate routing plus cost.

        ``deferred_slot_nos`` are changed identities prepared for auto-pool
        takeover: they are written directly as fail-closed REPAIRING slots with
        no Pin binding (Ban + auto-pool routing never requires a Pin).
        """

        deferred = _slot_no_set(deferred_slot_nos, "provider_auto_pool_defer_slot_invalid")
        bundle_slot_nos = {item.slot_no for item in bundle.slots}
        if any(slot_no not in bundle_slot_nos for slot_no in deferred):
            raise ProviderPoolError("provider_auto_pool_defer_slot_invalid")
        non_vacant_deferred = {
            slot_no
            for slot_no in deferred
            if next(
                (
                    item
                    for item in bundle.slots
                    if item.slot_no == slot_no and item.slot_state is not SlotState.VACANT
                ),
                None,
            )
            is not None
        }
        if any(slot.slot_state not in {SlotState.READY, SlotState.VACANT} for slot in bundle.slots):
            raise ProviderPoolError("provider_bundle_slots_unverified")
        if any(
            slot.slot_state is SlotState.READY
            and slot.slot_no not in deferred
            and (slot.enodot_binding_id is None or slot.confirmed_pin_binding_id is None)
            for slot in bundle.slots
        ):
            raise ProviderPoolError("provider_bundle_binding_readback_missing")

        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                _lock_model(cursor, bundle.model)
                current = _active_row(cursor, bundle.model, for_update=True)
                _check_active_cas(
                    current,
                    expected_routing_generation=bundle.parent_routing_generation,
                    expected_cost_generation=bundle.parent_cost_generation,
                )
                cursor.execute(
                    """
                    SELECT generation.id AS generation_id,
                           generation.cost_generation_id,
                           generation.model AS routing_model,
                           generation.generation AS routing_generation,
                           generation.parent_generation,
                           generation.generation_source,
                           generation.source_hash,
                           generation.cost_generation AS routing_cost_generation,
                           generation.desired_size,
                           generation.ordered_channel_ids,
                           generation.standby_channel_ids,
                           generation.config_version AS routing_config_version,
                           cost.model AS cost_model,
                           cost.cost_generation,
                           cost.parent_cost_generation,
                           cost.cost_source,
                           cost.cost_input_hash,
                           cost.cost_output_hash,
                           cost.token_mix_version,
                           cost.exchange_rate_version,
                           cost.active_slot_nos,
                           cost.desired_slot_costs,
                           cost.route_cost_multiplier,
                           cost.retry_reserve_multiplier,
                           cost.realized_cost_p95_multiplier,
                           cost.pool_cost_multiplier,
                           cost.margin_bps,
                           cost.headroom_bps,
                           cost.billing_floor_multiplier,
                           cost.transition_floor_multiplier,
                           cost.observed_billing_floor_multiplier,
                           cost.config_version AS cost_config_version
                    FROM a6_trade.provider_pool_generations generation
                    JOIN a6_trade.provider_cost_generations cost
                      ON cost.id = generation.cost_generation_id
                    WHERE generation.model = %s AND generation.generation = %s
                      AND cost.model = generation.model
                      AND generation.generation_state = 'applying'
                      AND cost.pricing_state = 'confirmed'
                    FOR UPDATE OF generation, cost
                    """,
                    (bundle.model, bundle.routing_generation),
                )
                planned = _row(cursor)
                if planned is None:
                    raise ProviderPoolError("provider_bundle_not_confirmed")
                _check_activation_bundle(planned, bundle)
                if current is not None:
                    cursor.execute(
                        """
                        UPDATE a6_trade.provider_pool_generations
                        SET generation_state = 'superseded', updated_at = now()
                        WHERE model = %s AND generation = %s
                          AND generation_state = 'active'
                        """,
                        (bundle.model, bundle.parent_routing_generation),
                    )
                    if cursor.rowcount != 1:
                        raise ProviderPoolError("provider_old_routing_supersede_failed")
                    cursor.execute(
                        """
                        UPDATE a6_trade.provider_cost_generations
                        SET pricing_state = 'superseded', updated_at = now()
                        WHERE model = %s AND cost_generation = %s
                          AND pricing_state = 'active'
                        """,
                        (bundle.model, bundle.parent_cost_generation),
                    )
                    if cursor.rowcount != 1:
                        raise ProviderPoolError("provider_old_cost_supersede_failed")
                epoch_ids = {
                    slot.slot_no: (
                        None
                        if slot.slot_no in non_vacant_deferred
                        else _prepare_pin_assignment_epoch(cursor, bundle, slot)
                    )
                    for slot in bundle.slots
                }
                cursor.execute(
                    """
                    UPDATE a6_trade.provider_pool_slots
                    SET valid_to = now(), updated_at = now()
                    WHERE model = %s AND valid_to IS NULL
                    """,
                    (bundle.model,),
                )
                for slot in bundle.slots:
                    deferred_slot = slot.slot_no in non_vacant_deferred
                    cursor.execute(
                        """
                        INSERT INTO a6_trade.provider_pool_slots AS existing (
                            generation_id, model, routing_generation,
                            slot_no, slot_name, cost_generation_id, cost_generation,
                            enodot_binding_id, enodot_account_id, a6_token_id,
                            pin_assignment_epoch_id,
                            desired_channel_id, desired_rank, desired_priority,
                            desired_slot_cost_multiplier, observed_rate_multiplier,
                            liquidity_state, funding_eligible, slot_state,
                            desired_schedulable, observed_schedulable,
                            confirmed_pin_binding_id,
                            pin_confirmed, probe_confirmed, state_version
                        ) VALUES (
                            %s, %s, %s, %s, %s, %s, %s,
                            %s, %s, %s, %s, %s, %s, %s, %s,
                            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                        )
                        ON CONFLICT (model, routing_generation, slot_no) DO UPDATE
                        SET generation_id = EXCLUDED.generation_id,
                            slot_name = EXCLUDED.slot_name,
                            cost_generation_id = EXCLUDED.cost_generation_id,
                            cost_generation = EXCLUDED.cost_generation,
                            enodot_binding_id = EXCLUDED.enodot_binding_id,
                            enodot_account_id = EXCLUDED.enodot_account_id,
                            a6_token_id = EXCLUDED.a6_token_id,
                            pin_assignment_epoch_id = EXCLUDED.pin_assignment_epoch_id,
                            desired_channel_id = EXCLUDED.desired_channel_id,
                            desired_rank = EXCLUDED.desired_rank,
                            desired_priority = EXCLUDED.desired_priority,
                            desired_slot_cost_multiplier =
                                EXCLUDED.desired_slot_cost_multiplier,
                            observed_rate_multiplier = EXCLUDED.observed_rate_multiplier,
                            liquidity_state = EXCLUDED.liquidity_state,
                            funding_eligible = EXCLUDED.funding_eligible,
                            slot_state = EXCLUDED.slot_state,
                            desired_schedulable = EXCLUDED.desired_schedulable,
                            observed_schedulable = EXCLUDED.observed_schedulable,
                            confirmed_pin_binding_id = EXCLUDED.confirmed_pin_binding_id,
                            pin_confirmed = EXCLUDED.pin_confirmed,
                            probe_confirmed = EXCLUDED.probe_confirmed,
                            state_version = EXCLUDED.state_version,
                            valid_from = now(), valid_to = NULL, updated_at = now()
                        WHERE existing.valid_to IS NOT NULL
                          AND existing.generation_id = EXCLUDED.generation_id
                          AND existing.cost_generation_id = EXCLUDED.cost_generation_id
                        """,
                        (
                            int(planned["generation_id"]),
                            bundle.model,
                            bundle.routing_generation,
                            slot.slot_no,
                            slot.slot_name,
                            int(planned["cost_generation_id"]),
                            bundle.cost_generation,
                            slot.enodot_binding_id,
                            slot.account_id,
                            slot.token_id,
                            epoch_ids[slot.slot_no],
                            slot.channel_id,
                            slot.rank,
                            slot.priority,
                            slot.desired_cost_multiplier,
                            slot.observed_cost_multiplier,
                            "FUNDED" if slot.funding_eligible else "UNFUNDED",
                            slot.funding_eligible,
                            "REPAIRING" if deferred_slot else slot.slot_state.value,
                            False if deferred_slot else slot.desired_schedulable,
                            False if deferred_slot else slot.observed_schedulable,
                            None if deferred_slot else slot.confirmed_pin_binding_id,
                            False if deferred_slot else slot.pin_confirmed,
                            False if deferred_slot else slot.probe_confirmed,
                            slot.state_version,
                        ),
                    )
                    if cursor.rowcount != 1:
                        raise ProviderPoolError("provider_slot_activate_failed")
                ready_slot_count = len(bundle.ready_slots) - len(non_vacant_deferred)
                _project_active_provider_pin_facts(
                    cursor,
                    model=bundle.model,
                    routing_generation=bundle.routing_generation,
                    config_version=bundle.config_version,
                    expected_ready_count=ready_slot_count,
                )
                project_active_provider_inventory(
                    cursor,
                    bundle.model,
                    routing_generation=bundle.routing_generation,
                    expected_ready_count=ready_slot_count,
                )
                cursor.execute(
                    """
                    UPDATE a6_trade.provider_cost_generations
                    SET pricing_state = 'active', activated_at = now(), updated_at = now()
                    WHERE id = %s AND pricing_state = 'confirmed'
                    """,
                    (int(planned["cost_generation_id"]),),
                )
                if cursor.rowcount != 1:
                    raise ProviderPoolError("provider_new_cost_activate_failed")
                cursor.execute(
                    """
                    UPDATE a6_trade.provider_pool_generations
                    SET generation_state = 'active', activated_at = now(), updated_at = now()
                    WHERE id = %s AND generation_state = 'applying'
                    """,
                    (int(planned["generation_id"]),),
                )
                if cursor.rowcount != 1:
                    raise ProviderPoolError("provider_new_routing_activate_failed")
            connection.commit()
            return ActiveBundleState(
                bundle.model,
                bundle.routing_generation,
                bundle.cost_generation,
                bundle.source_hash,
                str(planned["cost_input_hash"]),
                str(planned["cost_output_hash"]),
                bundle.desired_size,
            )
        except Exception:
            connection.rollback()
            raise
        finally:
            _close(connection)

    def hold_membership_for_auto_pool(
        self,
        bundle: ProviderBundle,
        *,
        slot_nos: Sequence[int],
    ) -> int:
        """Keep changed identities out of dispatch until auto-pool readbacks pass."""

        held = tuple(sorted({_strict_int(value) for value in slot_nos}))
        if not held or len(held) != len(tuple(slot_nos)) or any(value < 1 for value in held):
            raise ProviderPoolError("provider_auto_pool_hold_slot_invalid")
        desired = {slot.slot_no: slot for slot in bundle.slots}
        if not set(held).issubset(desired):
            raise ProviderPoolError("provider_auto_pool_hold_slot_invalid")
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                _lock_model(cursor, bundle.model)
                current = _active_row(cursor, bundle.model, for_update=True)
                if current is None or (
                    _strict_int(current["generation"]) != bundle.routing_generation
                    or _strict_int(current["cost_generation"]) != bundle.cost_generation
                ):
                    raise ProviderPoolError(
                        "provider_auto_pool_hold_generation_mismatch"
                    )
                for slot_no in held:
                    slot = desired[slot_no]
                    if (
                        slot.slot_state is not SlotState.READY
                        or slot.enodot_binding_id is None
                        or slot.account_id is None
                        or slot.token_id is None
                    ):
                        raise ProviderPoolError(
                            "provider_auto_pool_hold_slot_invalid"
                        )
                    cursor.execute(
                        """
                        UPDATE a6_trade.provider_pool_slots
                        SET slot_state = 'REPAIRING',
                            desired_schedulable = false,
                            observed_schedulable = false,
                            state_version = state_version + 1,
                            updated_at = now()
                        WHERE model = %s AND routing_generation = %s
                          AND cost_generation = %s AND slot_no = %s
                          AND enodot_binding_id = %s
                          AND enodot_account_id = %s AND a6_token_id = %s
                          AND valid_to IS NULL AND slot_state = 'READY'
                          AND desired_schedulable AND observed_schedulable
                        """,
                        (
                            bundle.model,
                            bundle.routing_generation,
                            bundle.cost_generation,
                            slot_no,
                            slot.enodot_binding_id,
                            slot.account_id,
                            slot.token_id,
                        ),
                    )
                    if cursor.rowcount != 1:
                        raise ProviderPoolError(
                            "provider_auto_pool_hold_slot_conflict"
                        )
                    cursor.execute(
                        """
                        UPDATE a6_trade.provider_inventory
                        SET inventory_state = 'REPAIRING', schedulable = false,
                            assigned_slot_no = %s,
                            state_version = state_version + 1,
                            updated_at = now()
                        WHERE model = %s AND enodot_binding_id = %s
                          AND enodot_account_id = %s AND a6_token_id = %s
                        """,
                        (
                            slot_no,
                            bundle.model,
                            slot.enodot_binding_id,
                            slot.account_id,
                            slot.token_id,
                        ),
                    )
                    if cursor.rowcount != 1:
                        raise ProviderPoolError(
                            "provider_auto_pool_hold_inventory_conflict"
                        )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            _close(connection)
        return len(held)

    def rollback_activation(self, bundle: ProviderBundle) -> ProviderActivationRollback:
        """Restore the activation parent with an exact generation CAS."""

        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                _lock_model(cursor, bundle.model)
                current = _active_row(cursor, bundle.model, for_update=True)
                candidate = _rollback_generation_row(
                    cursor,
                    bundle.model,
                    bundle.routing_generation,
                    bundle.cost_generation,
                )
                if candidate is None or (
                    _optional_strict_int(candidate["parent_generation"])
                    != bundle.parent_routing_generation
                    or _optional_strict_int(candidate["parent_cost_generation"])
                    != bundle.parent_cost_generation
                ):
                    raise ProviderPoolError("provider_bundle_rollback_cas_mismatch")

                parent_pair: tuple[int, int] | None
                if bundle.parent_routing_generation is None:
                    if bundle.parent_cost_generation is not None:
                        raise ProviderPoolError("provider_bundle_rollback_cas_mismatch")
                    parent_pair = None
                else:
                    if bundle.parent_cost_generation is None:
                        raise ProviderPoolError("provider_bundle_rollback_cas_mismatch")
                    parent_pair = (
                        bundle.parent_routing_generation,
                        bundle.parent_cost_generation,
                    )
                candidate_pair = (bundle.routing_generation, bundle.cost_generation)
                current_pair = (
                    None
                    if current is None
                    else (
                        _strict_int(current["generation"]),
                        _strict_int(current["cost_generation"]),
                    )
                )

                parent = None
                if parent_pair is not None:
                    parent = _rollback_generation_row(
                        cursor,
                        bundle.model,
                        parent_pair[0],
                        parent_pair[1],
                    )
                    if parent is None:
                        raise ProviderPoolError("provider_bundle_rollback_cas_mismatch")

                if current_pair == candidate_pair:
                    if (
                        str(candidate["generation_state"]) != "active"
                        or str(candidate["pricing_state"]) != "active"
                    ):
                        raise ProviderPoolError("provider_bundle_rollback_cas_mismatch")
                    if parent is not None and (
                        str(parent["generation_state"]) != "superseded"
                        or str(parent["pricing_state"]) != "superseded"
                    ):
                        raise ProviderPoolError("provider_bundle_rollback_cas_mismatch")

                    cursor.execute(
                        """
                        UPDATE a6_trade.provider_pin_assignment_epochs epoch
                        SET ended_routing_generation = %s,
                            ended_cost_generation = %s,
                            ended_at = now(), updated_at = now()
                        WHERE epoch.id IN (
                            SELECT pin_assignment_epoch_id
                            FROM a6_trade.provider_pool_slots
                            WHERE model = %s AND routing_generation = %s
                              AND pin_assignment_epoch_id IS NOT NULL
                        )
                          AND epoch.id NOT IN (
                            SELECT pin_assignment_epoch_id
                            FROM a6_trade.provider_pool_slots
                            WHERE model = %s
                              AND routing_generation IS NOT DISTINCT FROM %s
                              AND pin_assignment_epoch_id IS NOT NULL
                        )
                          AND epoch.ended_at IS NULL
                        """,
                        (
                            bundle.routing_generation,
                            bundle.cost_generation,
                            bundle.model,
                            bundle.routing_generation,
                            bundle.model,
                            bundle.parent_routing_generation,
                        ),
                    )
                    if parent is not None:
                        cursor.execute(
                            """
                            UPDATE a6_trade.provider_pin_assignment_epochs epoch
                            SET ended_routing_generation = NULL,
                                ended_cost_generation = NULL,
                                ended_at = NULL, updated_at = now()
                            WHERE epoch.id IN (
                                SELECT pin_assignment_epoch_id
                                FROM a6_trade.provider_pool_slots
                                WHERE model = %s AND routing_generation = %s
                                  AND pin_assignment_epoch_id IS NOT NULL
                            )
                              AND epoch.id NOT IN (
                                SELECT pin_assignment_epoch_id
                                FROM a6_trade.provider_pool_slots
                                WHERE model = %s AND routing_generation = %s
                                  AND pin_assignment_epoch_id IS NOT NULL
                            )
                              AND epoch.ended_routing_generation = %s
                              AND epoch.ended_cost_generation = %s
                              AND epoch.ended_at IS NOT NULL
                            """,
                            (
                                bundle.model,
                                bundle.parent_routing_generation,
                                bundle.model,
                                bundle.routing_generation,
                                bundle.routing_generation,
                                bundle.cost_generation,
                            ),
                        )

                    cursor.execute(
                        """
                        UPDATE a6_trade.provider_pool_generations
                        SET generation_state = 'applying', activated_at = NULL,
                            updated_at = now()
                        WHERE id = %s AND generation_state = 'active'
                        """,
                        (_strict_int(candidate["generation_id"]),),
                    )
                    if cursor.rowcount != 1:
                        raise ProviderPoolError("provider_new_routing_rollback_failed")
                    cursor.execute(
                        """
                        UPDATE a6_trade.provider_cost_generations
                        SET pricing_state = 'confirmed', activated_at = NULL,
                            updated_at = now()
                        WHERE id = %s AND pricing_state = 'active'
                        """,
                        (_strict_int(candidate["cost_generation_id"]),),
                    )
                    if cursor.rowcount != 1:
                        raise ProviderPoolError("provider_new_cost_rollback_failed")
                    cursor.execute(
                        """
                        UPDATE a6_trade.provider_pool_slots
                        SET valid_to = now(), updated_at = now()
                        WHERE model = %s AND routing_generation = %s
                          AND valid_to IS NULL
                        """,
                        (bundle.model, bundle.routing_generation),
                    )
                    if cursor.rowcount != _strict_int(candidate["desired_size"]):
                        raise ProviderPoolError("provider_new_slots_rollback_failed")

                    if parent is not None:
                        cursor.execute(
                            """
                            UPDATE a6_trade.provider_pool_slots
                            SET valid_to = NULL, updated_at = now()
                            WHERE model = %s AND routing_generation = %s
                              AND valid_to IS NOT NULL
                            """,
                            (bundle.model, bundle.parent_routing_generation),
                        )
                        if cursor.rowcount != _strict_int(parent["desired_size"]):
                            raise ProviderPoolError("provider_parent_slots_restore_failed")
                        cursor.execute(
                            """
                            UPDATE a6_trade.provider_cost_generations
                            SET pricing_state = 'active', updated_at = now()
                            WHERE id = %s AND pricing_state = 'superseded'
                            """,
                            (_strict_int(parent["cost_generation_id"]),),
                        )
                        if cursor.rowcount != 1:
                            raise ProviderPoolError("provider_parent_cost_restore_failed")
                        cursor.execute(
                            """
                            UPDATE a6_trade.provider_pool_generations
                            SET generation_state = 'active', updated_at = now()
                            WHERE id = %s AND generation_state = 'superseded'
                            """,
                            (_strict_int(parent["generation_id"]),),
                        )
                        if cursor.rowcount != 1:
                            raise ProviderPoolError("provider_parent_routing_restore_failed")
                elif current_pair == parent_pair and (
                    parent_pair is not None or current_pair is None
                ):
                    if (
                        str(candidate["generation_state"]) != "applying"
                        or str(candidate["pricing_state"]) != "confirmed"
                    ):
                        raise ProviderPoolError("provider_bundle_rollback_cas_mismatch")
                    if parent is not None and (
                        str(parent["generation_state"]) != "active"
                        or str(parent["pricing_state"]) != "active"
                    ):
                        raise ProviderPoolError("provider_bundle_rollback_cas_mismatch")
                else:
                    raise ProviderPoolError("provider_bundle_rollback_cas_mismatch")

                restored_size = 0 if parent is None else _strict_int(parent["desired_size"])
                _check_rollback_slot_windows(
                    cursor,
                    model=bundle.model,
                    rolled_back_generation=bundle.routing_generation,
                    restored_generation=bundle.parent_routing_generation,
                    restored_size=restored_size,
                )
                _project_active_provider_pin_facts(
                    cursor,
                    model=bundle.model,
                    routing_generation=bundle.parent_routing_generation,
                    config_version=(
                        bundle.config_version
                        if parent is None
                        else str(parent["config_version"])
                    ),
                    expected_ready_count=(0 if parent is None else None),
                )
                project_active_provider_inventory(
                    cursor,
                    bundle.model,
                    routing_generation=bundle.parent_routing_generation,
                    expected_ready_count=(0 if parent is None else None),
                )
            connection.commit()
            return ProviderActivationRollback(
                model=bundle.model,
                rolled_back_routing_generation=bundle.routing_generation,
                rolled_back_cost_generation=bundle.cost_generation,
                restored_routing_generation=bundle.parent_routing_generation,
                restored_cost_generation=bundle.parent_cost_generation,
            )
        except Exception:
            connection.rollback()
            raise
        finally:
            _close(connection)

    def record_billing_floor(
        self,
        model: str,
        *,
        cost_generation: int,
        observed_billing_floor: Decimal,
    ) -> None:
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                _lock_model(cursor, str(model))
                cursor.execute(
                    """
                    UPDATE a6_trade.provider_cost_generations cost
                    SET observed_billing_floor_multiplier = %s, updated_at = now()
                    WHERE cost.model = %s AND cost.cost_generation = %s
                      AND cost.pricing_state = 'active'
                      AND cost.billing_floor_multiplier <= %s
                      AND EXISTS (
                          SELECT 1
                          FROM a6_trade.provider_pool_generations generation
                          WHERE generation.model = cost.model
                            AND generation.cost_generation_id = cost.id
                            AND generation.generation_state = 'active'
                      )
                    """,
                    (
                        observed_billing_floor,
                        str(model),
                        int(cost_generation),
                        observed_billing_floor,
                    ),
                )
                if cursor.rowcount != 1:
                    raise ProviderPoolError("provider_bundle_billing_record_cas_mismatch")
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            _close(connection)


def _project_active_provider_pin_facts(
    cursor: Any,
    *,
    model: str,
    routing_generation: int | None,
    config_version: str,
    expected_ready_count: int | None,
) -> None:
    """Project exact active-slot Pin readback into the shared pricing facts."""

    if routing_generation is None:
        ready_rows: list[dict[str, Any]] = []
    else:
        cursor.execute(
            """
            SELECT slot_no, a6_token_id, desired_channel_id,
                   observed_rate_multiplier, confirmed_pin_binding_id
            FROM a6_trade.provider_pool_slots
            WHERE model = %s AND routing_generation = %s
              AND valid_to IS NULL AND slot_state = 'READY'
              AND pin_confirmed
            ORDER BY slot_no
            FOR SHARE
            """,
            (model, routing_generation),
        )
        ready_rows = _rows(cursor)
    if expected_ready_count is not None and len(ready_rows) != expected_ready_count:
        raise ProviderPoolError("provider_pin_projection_ready_count_mismatch")

    active_token_ids: list[int] = []
    seen_tokens: set[int] = set()
    for row in ready_rows:
        token_id = _strict_int(row["a6_token_id"])
        channel_id = _strict_int(row["desired_channel_id"])
        remote_binding_id = _strict_int(row["confirmed_pin_binding_id"])
        if token_id in seen_tokens:
            raise ProviderPoolError("provider_pin_projection_token_duplicate")
        seen_tokens.add(token_id)
        active_token_ids.append(token_id)
        cursor.execute(
            """
            SELECT binding.id, binding.channel_id, binding.fallback_to_smart_routing,
                   binding.confirmed_at, binding.decision_id,
                   decision.a6_token_id AS decision_token_id,
                   decision.model AS decision_model,
                   decision.selected_channel_id,
                   decision.decision_status
            FROM a6_trade.a6_pin_bindings binding
            LEFT JOIN a6_trade.a6_pin_decisions decision
              ON decision.id = binding.decision_id
            WHERE binding.a6_token_id = %s AND binding.model = %s
              AND binding.valid_to IS NULL
            ORDER BY binding.valid_from DESC, binding.id DESC
            LIMIT 1
            FOR UPDATE OF binding
            """,
            (token_id, model),
        )
        current = _row(cursor)
        complete = bool(
            current is not None
            and _strict_int(current["channel_id"]) == channel_id
            and current.get("fallback_to_smart_routing") is False
            and current.get("confirmed_at") is not None
            and _optional_strict_int(current.get("decision_id")) is not None
            and _optional_strict_int(current.get("decision_token_id")) == token_id
            and str(current.get("decision_model") or "") == model
            and _optional_strict_int(current.get("selected_channel_id")) == channel_id
            and str(current.get("decision_status") or "") == "completed"
        )
        if complete:
            continue

        cursor.execute(
            """
            INSERT INTO a6_trade.a6_pin_decisions (
                a6_token_id, model, current_channel_id, selected_channel_id,
                candidate_set, exclusion_reasons, expected_cost_multiplier,
                config_version, decision_status, decided_at
            ) VALUES (
                %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s,
                %s, 'completed', now()
            )
            RETURNING id
            """,
            (
                token_id,
                model,
                None if current is None else _strict_int(current["channel_id"]),
                channel_id,
                json.dumps(
                    [
                        {
                            "channel_id": channel_id,
                            "remote_pin_binding_id": remote_binding_id,
                            "routing_generation": routing_generation,
                            "source": "provider_apply_exact_readback",
                        }
                    ],
                    ensure_ascii=True,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                json.dumps(
                    {
                        "remote_pin_binding_id": remote_binding_id,
                        "routing_generation": routing_generation,
                        "source": "provider_apply_exact_readback",
                    },
                    ensure_ascii=True,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                row.get("observed_rate_multiplier"),
                config_version,
            ),
        )
        decision = _row(cursor)
        if decision is None:
            raise ProviderPoolError("provider_pin_projection_decision_insert_failed")
        decision_id = _strict_int(decision["id"])

        if current is not None and _strict_int(current["channel_id"]) != channel_id:
            cursor.execute(
                """
                UPDATE a6_trade.a6_pin_bindings
                SET valid_to = now()
                WHERE id = %s AND valid_to IS NULL
                """,
                (_strict_int(current["id"]),),
            )
            if cursor.rowcount != 1:
                raise ProviderPoolError("provider_pin_projection_binding_close_failed")
            current = None

        if current is None:
            cursor.execute(
                """
                INSERT INTO a6_trade.a6_pin_bindings (
                    a6_token_id, model, channel_id, fallback_to_smart_routing,
                    valid_from, confirmed_at, change_reason, decision_id
                ) VALUES (%s, %s, %s, false, now(), now(), %s, %s)
                """,
                (
                    token_id,
                    model,
                    channel_id,
                    "provider_apply_exact_readback",
                    decision_id,
                ),
            )
        else:
            cursor.execute(
                """
                UPDATE a6_trade.a6_pin_bindings
                SET fallback_to_smart_routing = false,
                    confirmed_at = now(), change_reason = %s, decision_id = %s
                WHERE id = %s AND valid_to IS NULL AND channel_id = %s
                """,
                (
                    "provider_apply_exact_readback",
                    decision_id,
                    _strict_int(current["id"]),
                    channel_id,
                ),
            )
            if cursor.rowcount != 1:
                raise ProviderPoolError("provider_pin_projection_binding_update_failed")

    cursor.execute(
        """
        UPDATE a6_trade.a6_pin_bindings binding
        SET valid_to = now()
        WHERE binding.model = %s AND binding.valid_to IS NULL
          AND EXISTS (
              SELECT 1
              FROM a6_trade.a6_tokens token
              JOIN a6_trade.a6_merchants merchant ON merchant.id = token.merchant_id
              WHERE token.id = binding.a6_token_id
                AND merchant.roles @> ARRAY['provider']::text[]
          )
          AND NOT (binding.a6_token_id = ANY(%s::bigint[]))
        """,
        (model, active_token_ids),
    )


def _applying_plan_key(model: str, routing_generation: int, cost_generation: int) -> str:
    return f"provider-generation-plan:{model}:{routing_generation}:{cost_generation}"


def _bundle_plan_document(bundle: ProviderBundle) -> dict[str, Any]:
    return {
        "schema": "a6-provider-applying-plan-v1",
        "model": bundle.model,
        "routing_generation": bundle.routing_generation,
        "parent_routing_generation": bundle.parent_routing_generation,
        "cost_generation": bundle.cost_generation,
        "parent_cost_generation": bundle.parent_cost_generation,
        "generation_source": bundle.generation_source,
        "source_hash": bundle.source_hash,
        "ordered_channel_ids": list(bundle.ordered_channel_ids),
        "standby_channel_ids": list(bundle.standby_channel_ids),
        "desired_size": bundle.desired_size,
        "cost_input_hash": bundle.cost_input_hash,
        "token_mix_version": bundle.token_mix_version,
        "exchange_rate_version": bundle.exchange_rate_version,
        "config_version": bundle.config_version,
        "cost_plan": bundle.cost_plan.public(),
        "slots": [
            {
                "slot_no": slot.slot_no,
                "channel_id": slot.channel_id,
                "rank": slot.rank,
                "priority": slot.priority,
                "assignment_id": slot.assignment_id,
                "enodot_binding_id": slot.enodot_binding_id,
                "token_id": slot.token_id,
                "account_id": slot.account_id,
                "slot_state": slot.slot_state.value,
                "funding_eligible": slot.funding_eligible,
                "desired_schedulable": slot.desired_schedulable,
                "observed_schedulable": slot.observed_schedulable,
                "desired_cost_multiplier": (
                    None
                    if slot.desired_cost_multiplier is None
                    else format(slot.desired_cost_multiplier, "f")
                ),
                "observed_cost_multiplier": (
                    None
                    if slot.observed_cost_multiplier is None
                    else format(slot.observed_cost_multiplier, "f")
                ),
                "confirmed_pin_binding_id": slot.confirmed_pin_binding_id,
                "pin_confirmed": slot.pin_confirmed,
                "probe_confirmed": slot.probe_confirmed,
                "state_version": slot.state_version,
            }
            for slot in bundle.slots
        ],
    }


def _bundle_from_plan_document(document: Mapping[str, Any]) -> ProviderBundle:
    if document.get("schema") != "a6-provider-applying-plan-v1":
        raise ProviderPoolError("provider_applying_plan_schema_invalid")
    raw_cost = document.get("cost_plan")
    raw_slots = document.get("slots")
    if not isinstance(raw_cost, Mapping) or not isinstance(raw_slots, list):
        raise ProviderPoolError("provider_applying_plan_document_invalid")
    raw_slot_costs = raw_cost.get("desired_slot_costs")
    if not isinstance(raw_slot_costs, Mapping):
        raise ProviderPoolError("provider_applying_plan_cost_invalid")
    cost_plan = CostPlan(
        desired_slot_costs={
            _strict_int(slot_no): _strict_decimal(value)
            for slot_no, value in raw_slot_costs.items()
        },
        route_cost_multiplier=_strict_decimal(raw_cost.get("route_cost_multiplier")),
        retry_reserve_multiplier=_strict_decimal(
            raw_cost.get("retry_reserve_multiplier")
        ),
        realized_cost_p95_multiplier=_optional_strict_decimal(
            raw_cost.get("realized_cost_p95_multiplier")
        ),
        pool_cost_multiplier=_strict_decimal(raw_cost.get("pool_cost_multiplier")),
        billing_floor_multiplier=_strict_decimal(
            raw_cost.get("billing_floor_multiplier")
        ),
        transition_floor_multiplier=_strict_decimal(
            raw_cost.get("transition_floor_multiplier")
        ),
        margin_bps=_strict_int(raw_cost.get("margin_bps")),
        headroom_bps=_strict_int(raw_cost.get("headroom_bps")),
        quantum=_strict_decimal(raw_cost.get("quantum")),
        cost_output_hash=str(raw_cost.get("cost_output_hash") or ""),
    )
    slots: list[ProviderSlot] = []
    for raw_slot in raw_slots:
        if not isinstance(raw_slot, Mapping):
            raise ProviderPoolError("provider_applying_plan_slot_invalid")
        slots.append(
            ProviderSlot(
                slot_no=_strict_int(raw_slot.get("slot_no")),
                channel_id=_optional_strict_int(raw_slot.get("channel_id")),
                rank=_optional_strict_int(raw_slot.get("rank")),
                priority=_optional_strict_int(raw_slot.get("priority")),
                assignment_id=raw_slot.get("assignment_id"),
                enodot_binding_id=_optional_strict_int(
                    raw_slot.get("enodot_binding_id")
                ),
                token_id=_optional_strict_int(raw_slot.get("token_id")),
                account_id=_optional_strict_int(raw_slot.get("account_id")),
                slot_state=SlotState(str(raw_slot.get("slot_state") or "")),
                funding_eligible=bool(raw_slot.get("funding_eligible")),
                desired_schedulable=bool(raw_slot.get("desired_schedulable")),
                observed_schedulable=bool(raw_slot.get("observed_schedulable")),
                desired_cost_multiplier=_optional_strict_decimal(
                    raw_slot.get("desired_cost_multiplier")
                ),
                observed_cost_multiplier=_optional_strict_decimal(
                    raw_slot.get("observed_cost_multiplier")
                ),
                confirmed_pin_binding_id=_optional_strict_int(
                    raw_slot.get("confirmed_pin_binding_id")
                ),
                pin_confirmed=bool(raw_slot.get("pin_confirmed")),
                probe_confirmed=bool(raw_slot.get("probe_confirmed")),
                state_version=_strict_int(raw_slot.get("state_version")),
            )
        )
    return ProviderBundle(
        model=str(document.get("model") or ""),
        routing_generation=_strict_int(document.get("routing_generation")),
        parent_routing_generation=_optional_strict_int(
            document.get("parent_routing_generation")
        ),
        cost_generation=_strict_int(document.get("cost_generation")),
        parent_cost_generation=_optional_strict_int(
            document.get("parent_cost_generation")
        ),
        generation_source=str(document.get("generation_source") or ""),
        source_hash=str(document.get("source_hash") or ""),
        ordered_channel_ids=tuple(
            _strict_int(value) for value in document.get("ordered_channel_ids") or ()
        ),
        standby_channel_ids=tuple(
            _strict_int(value) for value in document.get("standby_channel_ids") or ()
        ),
        desired_size=_strict_int(document.get("desired_size")),
        slots=tuple(slots),
        cost_plan=cost_plan,
        cost_input_hash=(
            None
            if document.get("cost_input_hash") is None
            else str(document.get("cost_input_hash"))
        ),
        token_mix_version=str(document.get("token_mix_version") or ""),
        exchange_rate_version=str(document.get("exchange_rate_version") or ""),
        config_version=str(document.get("config_version") or ""),
    )


def _json_object(value: object) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            raise ProviderPoolError("provider_applying_plan_document_invalid") from None
    if not isinstance(value, Mapping):
        raise ProviderPoolError("provider_applying_plan_document_invalid")
    return dict(value)


def _prepare_pin_assignment_epoch(
    cursor: Any,
    bundle: ProviderBundle,
    slot: ProviderSlot,
) -> int | None:
    cursor.execute(
        """
        SELECT slot.pin_assignment_epoch_id, slot.enodot_account_id,
               slot.a6_token_id, slot.desired_channel_id,
               epoch.enodot_account_id AS epoch_account_id,
               epoch.a6_token_id AS epoch_token_id,
               epoch.channel_id AS epoch_channel_id,
               epoch.ended_at AS epoch_ended_at
        FROM a6_trade.provider_pool_slots slot
        LEFT JOIN a6_trade.provider_pin_assignment_epochs epoch
          ON epoch.id = slot.pin_assignment_epoch_id
        WHERE slot.model = %s AND slot.slot_no = %s AND slot.valid_to IS NULL
        FOR UPDATE OF slot
        """,
        (bundle.model, slot.slot_no),
    )
    current = _row(cursor)
    current_epoch_id = (
        None if current is None else _optional_strict_int(current["pin_assignment_epoch_id"])
    )
    desired_identity = (
        None
        if slot.slot_state is SlotState.VACANT
        else (slot.account_id, slot.token_id, slot.channel_id)
    )
    current_identity = (
        None
        if current is None
        else (
            _optional_strict_int(current["enodot_account_id"]),
            _optional_strict_int(current["a6_token_id"]),
            _optional_strict_int(current["desired_channel_id"]),
        )
    )
    if desired_identity is not None and any(value is None for value in desired_identity):
        raise ProviderPoolError("provider_pin_epoch_desired_identity_missing")
    if current_identity == desired_identity and desired_identity is not None:
        if (
            current_epoch_id is None
            or current.get("epoch_ended_at") is not None
            or (
                _optional_strict_int(current.get("epoch_account_id")),
                _optional_strict_int(current.get("epoch_token_id")),
                _optional_strict_int(current.get("epoch_channel_id")),
            )
            != desired_identity
        ):
            raise ProviderPoolError("provider_pin_epoch_current_mismatch")
        return current_epoch_id

    if current_epoch_id is not None:
        cursor.execute(
            """
            UPDATE a6_trade.provider_pin_assignment_epochs
            SET ended_routing_generation = %s, ended_cost_generation = %s,
                ended_at = now(), updated_at = now()
            WHERE id = %s AND ended_at IS NULL
            """,
            (bundle.routing_generation, bundle.cost_generation, current_epoch_id),
        )
        if cursor.rowcount != 1:
            raise ProviderPoolError("provider_pin_epoch_close_failed")

    if desired_identity is None:
        return None
    cursor.execute(
        """
        INSERT INTO a6_trade.provider_pin_assignment_epochs (
            model, slot_no, enodot_account_id, a6_token_id, channel_id,
            started_routing_generation, started_cost_generation
        ) VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (model, slot_no, started_routing_generation) DO UPDATE
        SET ended_routing_generation = NULL,
            ended_cost_generation = NULL,
            ended_at = NULL,
            updated_at = now()
        WHERE provider_pin_assignment_epochs.enodot_account_id =
                  EXCLUDED.enodot_account_id
          AND provider_pin_assignment_epochs.a6_token_id = EXCLUDED.a6_token_id
          AND provider_pin_assignment_epochs.channel_id = EXCLUDED.channel_id
          AND provider_pin_assignment_epochs.started_cost_generation =
                  EXCLUDED.started_cost_generation
          AND provider_pin_assignment_epochs.ended_routing_generation =
                  EXCLUDED.started_routing_generation
          AND provider_pin_assignment_epochs.ended_cost_generation =
                  EXCLUDED.started_cost_generation
          AND provider_pin_assignment_epochs.ended_at IS NOT NULL
        RETURNING id
        """,
        (
            bundle.model,
            slot.slot_no,
            desired_identity[0],
            desired_identity[1],
            desired_identity[2],
            bundle.routing_generation,
            bundle.cost_generation,
        ),
    )
    row = _row(cursor)
    if row is None:
        raise ProviderPoolError("provider_pin_epoch_create_failed")
    return _strict_int(row["id"])


def _active_row(cursor: Any, model: str, *, for_update: bool) -> dict[str, Any] | None:
    lock = " FOR UPDATE OF generation, cost" if for_update else ""
    cursor.execute(
        f"""
        SELECT generation.generation, generation.cost_generation
        FROM a6_trade.provider_pool_generations generation
        JOIN a6_trade.provider_cost_generations cost
          ON cost.id = generation.cost_generation_id
        WHERE generation.model = %s
          AND generation.generation_state = 'active'
          AND cost.pricing_state = 'active'{lock}
        """,
        (model,),
    )
    return _row(cursor)


def _rollback_generation_row(
    cursor: Any,
    model: str,
    routing_generation: int,
    cost_generation: int,
) -> dict[str, Any] | None:
    cursor.execute(
        """
        SELECT generation.id AS generation_id,
               cost.id AS cost_generation_id,
               generation.parent_generation,
               cost.parent_cost_generation,
               generation.generation_state,
               cost.pricing_state,
               generation.desired_size,
               generation.config_version
        FROM a6_trade.provider_pool_generations generation
        JOIN a6_trade.provider_cost_generations cost
          ON cost.id = generation.cost_generation_id
         AND cost.model = generation.model
         AND cost.cost_generation = generation.cost_generation
        WHERE generation.model = %s AND generation.generation = %s
          AND generation.cost_generation = %s
        FOR UPDATE OF generation, cost
        """,
        (str(model), int(routing_generation), int(cost_generation)),
    )
    return _row(cursor)


def _check_rollback_slot_windows(
    cursor: Any,
    *,
    model: str,
    rolled_back_generation: int,
    restored_generation: int | None,
    restored_size: int,
) -> None:
    cursor.execute(
        """
        SELECT count(*) AS current_slot_count,
               count(*) FILTER (WHERE routing_generation = %s)
                   AS rolled_back_slot_count,
               count(*) FILTER (
                   WHERE routing_generation IS NOT DISTINCT FROM %s
               ) AS restored_slot_count
        FROM a6_trade.provider_pool_slots
        WHERE model = %s AND valid_to IS NULL
        """,
        (int(rolled_back_generation), restored_generation, str(model)),
    )
    row = _row(cursor)
    if row is None or (
        _strict_int(row["current_slot_count"]) != restored_size
        or _strict_int(row["rolled_back_slot_count"]) != 0
        or _strict_int(row["restored_slot_count"]) != restored_size
    ):
        raise ProviderPoolError("provider_bundle_rollback_slot_window_mismatch")


def _supersede_stale_plans(cursor: Any, model: str) -> None:
    cursor.execute(
        """
        WITH stale AS (
            SELECT generation.id AS generation_id, cost.id AS cost_id
            FROM a6_trade.provider_pool_generations generation
            JOIN a6_trade.provider_cost_generations cost
              ON cost.id = generation.cost_generation_id
            WHERE generation.model = %s
              AND generation.generation_state = 'planned'
              AND cost.pricing_state = 'planned'
            FOR UPDATE OF generation, cost
        ), superseded_routing AS (
            UPDATE a6_trade.provider_pool_generations generation
            SET generation_state = 'superseded', updated_at = now()
            FROM stale
            WHERE generation.id = stale.generation_id
            RETURNING stale.cost_id
        ), superseded_cost AS (
            UPDATE a6_trade.provider_cost_generations cost
            SET pricing_state = 'superseded', updated_at = now()
            WHERE cost.id IN (SELECT cost_id FROM stale)
            RETURNING cost.id
        )
        SELECT (SELECT count(*) FROM superseded_routing) AS routing_count,
               (SELECT count(*) FROM superseded_cost) AS cost_count
        """,
        (str(model),),
    )
    result = _row(cursor)
    if result is None or int(result["routing_count"]) != int(result["cost_count"]):
        raise ProviderPoolError("provider_planned_generation_supersede_mismatch")


def _check_active_cas(
    current: Mapping[str, Any] | None,
    *,
    expected_routing_generation: int | None,
    expected_cost_generation: int | None,
) -> None:
    routing = None if current is None else int(current["generation"])
    cost = None if current is None else int(current["cost_generation"])
    if routing != expected_routing_generation or cost != expected_cost_generation:
        raise ProviderPoolError("provider_bundle_active_cas_mismatch")


def _check_activation_bundle(planned: Mapping[str, Any], bundle: ProviderBundle) -> None:
    """Reject a confirmed row whose persisted semantics drifted from the bundle."""

    plan = bundle.cost_plan
    try:
        desired_slot_costs = planned["desired_slot_costs"]
        if not isinstance(desired_slot_costs, Mapping):
            raise TypeError
        actual_slot_costs = {
            _strict_int(slot_no): _strict_decimal(value)
            for slot_no, value in desired_slot_costs.items()
        }
        actual = {
            "routing_model": str(planned["routing_model"]),
            "routing_generation": _strict_int(planned["routing_generation"]),
            "parent_generation": _optional_strict_int(planned["parent_generation"]),
            "generation_source": str(planned["generation_source"]),
            "source_hash": str(planned["source_hash"]),
            "routing_cost_generation": _strict_int(planned["routing_cost_generation"]),
            "desired_size": _strict_int(planned["desired_size"]),
            "ordered_channel_ids": tuple(
                _strict_int(value) for value in (planned["ordered_channel_ids"] or ())
            ),
            "standby_channel_ids": tuple(
                _strict_int(value) for value in (planned["standby_channel_ids"] or ())
            ),
            "routing_config_version": str(planned["routing_config_version"]),
            "cost_model": str(planned["cost_model"]),
            "cost_generation": _strict_int(planned["cost_generation"]),
            "parent_cost_generation": _optional_strict_int(planned["parent_cost_generation"]),
            "cost_source": str(planned["cost_source"]),
            "cost_input_hash": str(planned["cost_input_hash"]),
            "cost_output_hash": str(planned["cost_output_hash"]),
            "token_mix_version": str(planned["token_mix_version"]),
            "exchange_rate_version": str(planned["exchange_rate_version"]),
            "active_slot_nos": tuple(
                _strict_int(value) for value in (planned["active_slot_nos"] or ())
            ),
            "desired_slot_costs": actual_slot_costs,
            "route_cost_multiplier": _strict_decimal(planned["route_cost_multiplier"]),
            "retry_reserve_multiplier": _strict_decimal(planned["retry_reserve_multiplier"]),
            "realized_cost_p95_multiplier": _optional_strict_decimal(
                planned["realized_cost_p95_multiplier"]
            ),
            "pool_cost_multiplier": _strict_decimal(planned["pool_cost_multiplier"]),
            "margin_bps": _strict_int(planned["margin_bps"]),
            "headroom_bps": _strict_int(planned["headroom_bps"]),
            "billing_floor_multiplier": _strict_decimal(planned["billing_floor_multiplier"]),
            "transition_floor_multiplier": _strict_decimal(planned["transition_floor_multiplier"]),
            "cost_config_version": str(planned["cost_config_version"]),
        }
        observed_floor = _strict_decimal(planned["observed_billing_floor_multiplier"])
    except (KeyError, TypeError, ValueError, ArithmeticError):
        raise ProviderPoolError("provider_bundle_activation_generation_conflict") from None

    expected = {
        "routing_model": bundle.model,
        "routing_generation": bundle.routing_generation,
        "parent_generation": bundle.parent_routing_generation,
        "generation_source": bundle.generation_source,
        "source_hash": bundle.source_hash,
        "routing_cost_generation": bundle.cost_generation,
        "desired_size": bundle.desired_size,
        "ordered_channel_ids": bundle.ordered_channel_ids,
        "standby_channel_ids": bundle.standby_channel_ids,
        "routing_config_version": bundle.config_version,
        "cost_model": bundle.model,
        "cost_generation": bundle.cost_generation,
        "parent_cost_generation": bundle.parent_cost_generation,
        "cost_source": bundle.generation_source,
        "cost_input_hash": bundle.effective_cost_input_hash,
        "cost_output_hash": plan.cost_output_hash,
        "token_mix_version": bundle.token_mix_version,
        "exchange_rate_version": bundle.exchange_rate_version,
        "active_slot_nos": tuple(plan.desired_slot_costs),
        "desired_slot_costs": dict(plan.desired_slot_costs),
        "route_cost_multiplier": plan.route_cost_multiplier,
        "retry_reserve_multiplier": plan.retry_reserve_multiplier,
        "realized_cost_p95_multiplier": plan.realized_cost_p95_multiplier,
        "pool_cost_multiplier": plan.pool_cost_multiplier,
        "margin_bps": plan.margin_bps,
        "headroom_bps": plan.headroom_bps,
        "billing_floor_multiplier": plan.billing_floor_multiplier,
        "transition_floor_multiplier": plan.transition_floor_multiplier,
        "cost_config_version": bundle.config_version,
    }
    if actual != expected or observed_floor < plan.transition_floor_multiplier:
        raise ProviderPoolError("provider_bundle_activation_generation_conflict")


def _optional_int(value: object) -> int | None:
    return None if value is None else int(value)


def _slot_no_set(values: Sequence[int], code: str) -> frozenset[int]:
    result = tuple(_strict_int(value) for value in values)
    if len(result) != len(set(result)) or any(value < 1 for value in result):
        raise ProviderPoolError(code)
    return frozenset(result)


def _strict_int(value: object) -> int:
    if isinstance(value, bool):
        raise TypeError
    return int(value)  # type: ignore[arg-type]


def _optional_strict_int(value: object) -> int | None:
    return None if value is None else _strict_int(value)


def _strict_decimal(value: object) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise TypeError
    result = Decimal(str(value))
    if not result.is_finite():
        raise ValueError
    return result


def _optional_strict_decimal(value: object) -> Decimal | None:
    return None if value is None else _strict_decimal(value)


def _bundle_from_rows(
    generation: Mapping[str, Any],
    slot_rows: list[dict[str, Any]],
) -> ProviderBundle:
    raw_costs = generation.get("desired_slot_costs")
    if isinstance(raw_costs, str):
        raw_costs = json.loads(raw_costs)
    if not isinstance(raw_costs, Mapping):
        raise ProviderPoolError("provider_bundle_cost_document_invalid")
    desired_slot_costs = {
        _strict_int(slot_no): _strict_decimal(value)
        for slot_no, value in raw_costs.items()
    }
    cost_plan = CostPlan(
        desired_slot_costs=desired_slot_costs,
        route_cost_multiplier=_strict_decimal(generation["route_cost_multiplier"]),
        retry_reserve_multiplier=_strict_decimal(
            generation["retry_reserve_multiplier"]
        ),
        realized_cost_p95_multiplier=_optional_strict_decimal(
            generation.get("realized_cost_p95_multiplier")
        ),
        pool_cost_multiplier=_strict_decimal(generation["pool_cost_multiplier"]),
        billing_floor_multiplier=_strict_decimal(
            generation["billing_floor_multiplier"]
        ),
        transition_floor_multiplier=_strict_decimal(
            generation["transition_floor_multiplier"]
        ),
        margin_bps=_strict_int(generation["margin_bps"]),
        headroom_bps=_strict_int(generation["headroom_bps"]),
        quantum=DEFAULT_COST_QUANTUM,
        cost_output_hash=str(generation["cost_output_hash"]),
    )
    slots: list[ProviderSlot] = []
    for row in slot_rows:
        account_id = _optional_strict_int(row.get("enodot_account_id"))
        slots.append(
            ProviderSlot(
                slot_no=_strict_int(row["slot_no"]),
                channel_id=_optional_strict_int(row.get("desired_channel_id")),
                rank=_optional_strict_int(row.get("desired_rank")),
                priority=_optional_strict_int(row.get("desired_priority")),
                assignment_id=account_id,
                enodot_binding_id=_optional_strict_int(row.get("enodot_binding_id")),
                token_id=_optional_strict_int(row.get("a6_token_id")),
                account_id=account_id,
                slot_state=SlotState(str(row["slot_state"])),
                funding_eligible=bool(row.get("funding_eligible")),
                desired_schedulable=bool(row.get("desired_schedulable")),
                observed_schedulable=bool(row.get("observed_schedulable")),
                desired_cost_multiplier=_optional_strict_decimal(
                    row.get("desired_slot_cost_multiplier")
                ),
                observed_cost_multiplier=_optional_strict_decimal(
                    row.get("observed_rate_multiplier")
                ),
                confirmed_pin_binding_id=_optional_strict_int(
                    row.get("confirmed_pin_binding_id")
                ),
                pin_confirmed=bool(row.get("pin_confirmed")),
                probe_confirmed=bool(row.get("probe_confirmed")),
                state_version=_strict_int(row.get("state_version") or 1),
            )
        )
    return ProviderBundle(
        model=str(generation["model"]),
        routing_generation=_strict_int(generation["generation"]),
        parent_routing_generation=_optional_strict_int(
            generation.get("parent_generation")
        ),
        cost_generation=_strict_int(generation["cost_generation"]),
        parent_cost_generation=_optional_strict_int(
            generation.get("parent_cost_generation")
        ),
        generation_source=str(generation["generation_source"]),
        source_hash=str(generation["source_hash"]),
        ordered_channel_ids=tuple(
            _strict_int(value) for value in generation.get("ordered_channel_ids") or ()
        ),
        standby_channel_ids=tuple(
            _strict_int(value) for value in generation.get("standby_channel_ids") or ()
        ),
        desired_size=_strict_int(generation["desired_size"]),
        slots=tuple(slots),
        cost_plan=cost_plan,
        cost_input_hash=str(generation["cost_input_hash"]),
        token_mix_version=str(generation["token_mix_version"]),
        exchange_rate_version=str(generation["exchange_rate_version"]),
        generation_state=GenerationState(str(generation["generation_state"])),
        pricing_state=PricingState(str(generation["pricing_state"])),
        config_version=str(generation["config_version"]),
    )


def _active_state(row: Mapping[str, Any]) -> ActiveBundleState:
    return ActiveBundleState(
        model=str(row["model"]),
        routing_generation=int(row["generation"]),
        cost_generation=int(row["cost_generation"]),
        source_hash=str(row["source_hash"]),
        cost_input_hash=str(row["cost_input_hash"]),
        cost_output_hash=str(row["cost_output_hash"]),
        desired_size=int(row["desired_size"]),
    )


def _lock_model(cursor: Any, model: str) -> None:
    cursor.execute(
        "SELECT pg_advisory_xact_lock(%s, hashtext(%s))",
        (0x41365050, str(model)),
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


def _json(value: Mapping[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _close(connection: Any) -> None:
    close = getattr(connection, "close", None)
    if callable(close):
        close()
