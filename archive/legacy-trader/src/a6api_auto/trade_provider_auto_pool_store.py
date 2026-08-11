"""PostgreSQL state transitions for Provider marketplace auto-pool routing."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from .trade_provider_auto_pool import (
    AutoPoolPlan,
    OwnedMarketplaceResources,
    ProviderAutoPoolError,
)


@dataclass(frozen=True)
class ProviderAutoPoolAccount:
    merchant_id: int
    a6_user_id: int
    session_ref: str
    token_ref_id: int
    external_token_id: int
    enodot_account_id: int
    slot_no: int
    routing_generation: int
    cost_generation: int


@dataclass(frozen=True)
class ProviderAutoPoolGeneration:
    generation_id: int
    generation: int
    parent_generation: int | None
    model: str
    routing_generation: int
    cost_generation: int
    source_hash: str
    reused_active: bool = False
    reused_inflight: bool = False


@dataclass(frozen=True)
class ProviderAutoPoolRuntimeContext:
    generation_id: int
    generation: int
    max_ratio_cny: Decimal
    max_ratio_usd: Decimal
    failed_account_count: int
    unfunded_account_count: int


def provider_auto_pool_execution_hash(
    plan: AutoPoolPlan,
    owned: OwnedMarketplaceResources,
    accounts: Sequence[ProviderAutoPoolAccount],
) -> str:
    document = {
        "algorithm": "provider-auto-pool-execution-v1",
        "plan_source_hash": plan.source_hash,
        "owned": {
            "supplier_ids": sorted(owned.supplier_ids),
            "channel_ids": sorted(owned.channel_ids),
            "listing_ids": sorted(owned.listing_ids),
        },
        "accounts": [
            {
                "merchant_id": item.merchant_id,
                "a6_user_id": item.a6_user_id,
                "token_ref_id": item.token_ref_id,
                "external_token_id": item.external_token_id,
                "enodot_account_id": item.enodot_account_id,
                "slot_no": item.slot_no,
                "routing_generation": item.routing_generation,
                "cost_generation": item.cost_generation,
            }
            for item in sorted(accounts, key=lambda value: value.slot_no)
        ],
    }
    return _hash(document)


class PostgresProviderAutoPoolStore:
    def __init__(
        self,
        connection_factory: Callable[[], Any],
        *,
        stale_claim_seconds: int = 120,
    ) -> None:
        if not callable(connection_factory):
            raise ValueError("provider_auto_pool_connection_factory_invalid")
        if (
            isinstance(stale_claim_seconds, bool)
            or not 5 <= stale_claim_seconds <= 3600
        ):
            raise ValueError("provider_auto_pool_stale_claim_invalid")
        self.connection_factory = connection_factory
        self.stale_claim_seconds = stale_claim_seconds

    def load_accounts(self, model: str) -> tuple[ProviderAutoPoolAccount, ...]:
        model = _model(model)
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT merchant.id AS merchant_id, merchant.a6_user_id,
                           merchant.session_ref, token.id AS token_ref_id,
                           token.a6_token_id AS external_token_id,
                           slot.enodot_account_id, slot.slot_no,
                           generation.generation AS routing_generation,
                           generation.cost_generation
                    FROM a6_trade.provider_pool_generations generation
                    JOIN a6_trade.provider_pool_slots slot
                      ON slot.generation_id = generation.id
                     AND slot.model = generation.model
                     AND slot.routing_generation = generation.generation
                     AND slot.valid_to IS NULL
                    JOIN a6_trade.a6_tokens token ON token.id = slot.a6_token_id
                    JOIN a6_trade.a6_merchants merchant ON merchant.id = token.merchant_id
                    JOIN a6_trade.enodot_account_bindings binding
                      ON binding.id = slot.enodot_binding_id
                     AND binding.a6_token_id = token.id
                     AND binding.enodot_account_id = slot.enodot_account_id
                    WHERE generation.model = %s
                      AND generation.generation_state = 'active'
                      AND slot.slot_state <> 'VACANT'
                      AND slot.liquidity_state = 'FUNDED'
                      AND slot.funding_eligible
                      AND slot.enodot_account_id IS NOT NULL
                      AND merchant.a6_user_id IS NOT NULL
                      AND merchant.status = 'supplier_submitted'
                      AND 'provider' = ANY(merchant.roles)
                      AND token.status IN ('1', 'active', 'enabled')
                      AND binding.state = 'bound'
                    ORDER BY slot.slot_no
                    """,
                    (model,),
                )
                rows = _rows(cursor)
        finally:
            _close(connection)
        accounts = tuple(_account(row) for row in rows)
        if not accounts:
            raise ProviderAutoPoolError("provider_auto_pool_accounts_empty")
        if len({item.slot_no for item in accounts}) != len(accounts) or len(
            {item.enodot_account_id for item in accounts}
        ) != len(accounts):
            raise ProviderAutoPoolError("provider_auto_pool_account_identity_conflict")
        routing = {item.routing_generation for item in accounts}
        costs = {item.cost_generation for item in accounts}
        if len(routing) != 1 or len(costs) != 1:
            raise ProviderAutoPoolError("provider_auto_pool_generation_identity_conflict")
        return accounts

    def active_runtime_context(
        self,
        model: str,
    ) -> ProviderAutoPoolRuntimeContext | None:
        model = _model(model)
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT generation.id,
                           generation.auto_pool_generation,
                           generation.max_ratio_cny,
                           generation.max_ratio_usd,
                           count(account.id) FILTER (
                               WHERE account.account_state = 'failed'
                           ) AS failed_account_count,
                           count(account.id) FILTER (
                               WHERE account.account_state = 'unfunded'
                           ) AS unfunded_account_count
                    FROM a6_trade.provider_auto_pool_generations generation
                    LEFT JOIN a6_trade.provider_auto_pool_accounts account
                      ON account.generation_id = generation.id
                    WHERE generation.model = %s
                      AND generation.generation_state = 'active'
                    GROUP BY generation.id
                    """,
                    (model,),
                )
                row = _row(cursor)
        finally:
            _close(connection)
        if row is None:
            return None
        return ProviderAutoPoolRuntimeContext(
            generation_id=_positive(row.get("id")),
            generation=_positive(row.get("auto_pool_generation")),
            max_ratio_cny=_positive_decimal(
                row.get("max_ratio_cny"),
                "provider_auto_pool_runtime_ratio_invalid",
            ),
            max_ratio_usd=_positive_decimal(
                row.get("max_ratio_usd"),
                "provider_auto_pool_runtime_ratio_invalid",
            ),
            failed_account_count=_nonnegative(row.get("failed_account_count")),
            unfunded_account_count=_nonnegative(row.get("unfunded_account_count")),
        )

    def owned_resources(
        self,
        model: str,
        merchant_id: int | None = None,
    ) -> OwnedMarketplaceResources:
        model = _model(model)
        if merchant_id is not None:
            merchant_id = _positive(merchant_id)
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                if merchant_id is None:
                    cursor.execute(
                        """
                        SELECT supplier_id
                        FROM a6_trade.a6_merchants
                        WHERE supplier_id IS NOT NULL AND supplier_id > 0
                        ORDER BY supplier_id
                        """
                    )
                else:
                    cursor.execute(
                        """
                        SELECT supplier_id
                        FROM a6_trade.a6_merchants
                        WHERE id = %s AND supplier_id IS NOT NULL AND supplier_id > 0
                        """,
                        (merchant_id,),
                    )
                supplier_ids = frozenset(int(row["supplier_id"]) for row in _rows(cursor))
                if merchant_id is None:
                    cursor.execute(
                        """
                        WITH controlled AS (
                            SELECT binding.a6_channel_id, binding.a6_listing_id
                            FROM a6_trade.a6_managed_listing_bindings binding
                            WHERE binding.a6_channel_id IS NOT NULL
                              AND binding.a6_listing_id IS NOT NULL
                              AND coalesce(
                                  binding.observed_state->>'platform_model',
                                  binding.observed_state->>'own_model',
                                  binding.desired_state #>> '{remote_listing,platform_model}',
                                  binding.desired_state #>> '{remote_listing,own_model}'
                              ) = %s
                        ), current_listings AS (
                            SELECT listing.a6_channel_id, listing.a6_listing_id
                            FROM a6_trade.a6_supplier_listings_current listing
                            WHERE listing.platform_model = %s OR listing.own_model = %s
                        )
                        SELECT a6_channel_id, a6_listing_id FROM controlled
                        UNION
                        SELECT a6_channel_id, a6_listing_id FROM current_listings
                        """,
                        (model, model, model),
                    )
                else:
                    cursor.execute(
                        """
                        WITH controlled AS (
                            SELECT binding.a6_channel_id, binding.a6_listing_id
                            FROM a6_trade.a6_managed_listing_bindings binding
                            WHERE binding.merchant_id = %s
                              AND binding.a6_channel_id IS NOT NULL
                              AND binding.a6_listing_id IS NOT NULL
                              AND coalesce(
                                  binding.observed_state->>'platform_model',
                                  binding.observed_state->>'own_model',
                                  binding.desired_state #>> '{remote_listing,platform_model}',
                                  binding.desired_state #>> '{remote_listing,own_model}'
                              ) = %s
                        ), current_listings AS (
                            SELECT listing.a6_channel_id, listing.a6_listing_id
                            FROM a6_trade.a6_supplier_listings_current listing
                            WHERE listing.merchant_id = %s
                              AND (listing.platform_model = %s OR listing.own_model = %s)
                        )
                        SELECT a6_channel_id, a6_listing_id FROM controlled
                        UNION
                        SELECT a6_channel_id, a6_listing_id FROM current_listings
                        """,
                        (merchant_id, model, merchant_id, model, model),
                    )
                resources = _rows(cursor)
        finally:
            _close(connection)
        return OwnedMarketplaceResources(
            supplier_ids=supplier_ids,
            channel_ids=frozenset(int(row["a6_channel_id"]) for row in resources),
            listing_ids=frozenset(int(row["a6_listing_id"]) for row in resources),
        )

    def owned_listings_by_channel(
        self,
        model: str,
        merchant_id: int | None = None,
    ) -> dict[int, int]:
        model = _model(model)
        if merchant_id is not None:
            merchant_id = _positive(merchant_id)
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                if merchant_id is None:
                    cursor.execute(
                        """
                        SELECT binding.a6_channel_id, binding.a6_listing_id
                        FROM a6_trade.a6_managed_listing_bindings binding
                        WHERE binding.a6_channel_id IS NOT NULL
                          AND binding.a6_listing_id IS NOT NULL
                          AND binding.control_state <> 'retired'
                          AND coalesce(
                              binding.observed_state->>'platform_model',
                              binding.observed_state->>'own_model',
                              binding.desired_state #>> '{remote_listing,platform_model}',
                              binding.desired_state #>> '{remote_listing,own_model}'
                          ) = %s
                        ORDER BY binding.a6_channel_id
                        """,
                        (model,),
                    )
                else:
                    cursor.execute(
                        """
                        SELECT binding.a6_channel_id, binding.a6_listing_id
                        FROM a6_trade.a6_managed_listing_bindings binding
                        WHERE binding.merchant_id = %s
                          AND binding.a6_channel_id IS NOT NULL
                          AND binding.a6_listing_id IS NOT NULL
                          AND binding.control_state <> 'retired'
                          AND coalesce(
                              binding.observed_state->>'platform_model',
                              binding.observed_state->>'own_model',
                              binding.desired_state #>> '{remote_listing,platform_model}',
                              binding.desired_state #>> '{remote_listing,own_model}'
                          ) = %s
                        ORDER BY binding.a6_channel_id
                        """,
                        (merchant_id, model),
                    )
                result: dict[int, int] = {}
                for row in _rows(cursor):
                    channel_id = int(row["a6_channel_id"])
                    listing_id = int(row["a6_listing_id"])
                    if channel_id in result and result[channel_id] != listing_id:
                        raise ProviderAutoPoolError(
                            "provider_auto_pool_owned_channel_listing_ambiguous"
                        )
                    result[channel_id] = listing_id
                return result
        finally:
            _close(connection)

    def begin_generation(
        self,
        plan: AutoPoolPlan,
        *,
        accounts: Sequence[ProviderAutoPoolAccount],
        observer_scan_ids: Sequence[int],
        observed_at: datetime,
        trigger_reason: str,
        config_version: str,
        source_hash: str,
        minimum_observers: int,
        target_channel_count: int,
        hard_max_ratio_cny: Decimal,
    ) -> ProviderAutoPoolGeneration:
        normalized_accounts = tuple(sorted(accounts, key=lambda item: item.slot_no))
        if not normalized_accounts:
            raise ProviderAutoPoolError("provider_auto_pool_accounts_empty")
        routing_generation = _one(
            item.routing_generation for item in normalized_accounts
        )
        cost_generation = _one(item.cost_generation for item in normalized_accounts)
        trigger_reason = _text(trigger_reason, "provider_auto_pool_trigger_invalid")
        config_version = _text(config_version, "provider_auto_pool_config_invalid")
        source_hash = _sha256(source_hash)
        scan_ids = tuple(sorted({_positive(value) for value in observer_scan_ids}))
        if not scan_ids or observed_at.tzinfo is None:
            raise ProviderAutoPoolError("provider_auto_pool_observer_evidence_invalid")
        payload = _plan_payload(plan)
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT pg_advisory_xact_lock(%s, hashtext(%s))",
                    (0x41364150, plan.model),
                )
                cursor.execute(
                    """
                    SELECT generation.id, generation.auto_pool_generation,
                           generation.parent_auto_pool_generation,
                           generation.routing_generation,
                           generation.cost_generation,
                           generation.source_hash,
                           generation.config_version,
                           generation.minimum_observers,
                           generation.target_channel_count,
                           generation.hard_max_ratio_cny,
                           count(account.id) AS account_count,
                           count(account.id) FILTER (
                               WHERE account.account_state = 'ready'
                           ) AS ready_count
                    FROM a6_trade.provider_auto_pool_generations generation
                    LEFT JOIN a6_trade.provider_auto_pool_accounts account
                      ON account.generation_id = generation.id
                    WHERE generation.model = %s
                      AND generation.generation_state = 'active'
                    GROUP BY generation.id
                    """,
                    (plan.model,),
                )
                active = _row(cursor)
                if (
                    active is not None
                    and str(active["source_hash"]) == source_hash
                    and int(active["routing_generation"]) == routing_generation
                    and int(active["cost_generation"]) == cost_generation
                    and int(active["account_count"]) == len(normalized_accounts)
                    and int(active["ready_count"]) == len(normalized_accounts)
                    and str(active["config_version"]) == config_version
                    and int(active["minimum_observers"]) == minimum_observers
                    and int(active["target_channel_count"]) == target_channel_count
                    and Decimal(str(active["hard_max_ratio_cny"]))
                    == hard_max_ratio_cny
                ):
                    connection.commit()
                    return ProviderAutoPoolGeneration(
                        generation_id=int(active["id"]),
                        generation=int(active["auto_pool_generation"]),
                        parent_generation=_optional_int(
                            active.get("parent_auto_pool_generation")
                        ),
                        model=plan.model,
                        routing_generation=routing_generation,
                        cost_generation=cost_generation,
                        source_hash=source_hash,
                        reused_active=True,
                    )
                cursor.execute(
                    """
                    SELECT generation.id, generation.auto_pool_generation,
                           generation.parent_auto_pool_generation,
                           generation.routing_generation,
                           generation.cost_generation,
                           generation.source_hash,
                           generation.generation_state,
                           generation.config_version,
                           generation.minimum_observers,
                           generation.target_channel_count,
                           generation.hard_max_ratio_cny,
                           count(account.id) AS account_count,
                           count(account.id) FILTER (
                               WHERE account.account_state IN ('unfunded', 'superseded')
                           ) AS blocked_count
                    FROM a6_trade.provider_auto_pool_generations generation
                    LEFT JOIN a6_trade.provider_auto_pool_accounts account
                      ON account.generation_id = generation.id
                    WHERE generation.model = %s
                      AND generation.source_hash = %s
                      AND generation.routing_generation = %s
                      AND generation.cost_generation = %s
                      AND generation.generation_state IN ('planned', 'applying')
                    GROUP BY generation.id
                    ORDER BY generation.auto_pool_generation DESC
                    LIMIT 1
                    """,
                    (plan.model, source_hash, routing_generation, cost_generation),
                )
                inflight = _row(cursor)
                if (
                    inflight is not None
                    and int(inflight["account_count"]) == len(normalized_accounts)
                    and int(inflight["blocked_count"]) == 0
                    and str(inflight["config_version"]) == config_version
                    and int(inflight["minimum_observers"]) == minimum_observers
                    and int(inflight["target_channel_count"]) == target_channel_count
                    and Decimal(str(inflight["hard_max_ratio_cny"]))
                    == hard_max_ratio_cny
                ):
                    if str(inflight["generation_state"]) == "planned":
                        cursor.execute(
                            """
                            UPDATE a6_trade.provider_auto_pool_generations
                            SET generation_state = 'applying', updated_at = now()
                            WHERE id = %s AND generation_state = 'planned'
                            """,
                            (int(inflight["id"]),),
                        )
                        if cursor.rowcount != 1:
                            raise ProviderAutoPoolError(
                                "provider_auto_pool_generation_resume_conflict"
                            )
                    connection.commit()
                    return ProviderAutoPoolGeneration(
                        generation_id=int(inflight["id"]),
                        generation=int(inflight["auto_pool_generation"]),
                        parent_generation=_optional_int(
                            inflight.get("parent_auto_pool_generation")
                        ),
                        model=plan.model,
                        routing_generation=routing_generation,
                        cost_generation=cost_generation,
                        source_hash=source_hash,
                        reused_inflight=True,
                    )
                cursor.execute(
                    """
                    SELECT coalesce(max(auto_pool_generation), 0) + 1 AS next_generation
                    FROM a6_trade.provider_auto_pool_generations
                    WHERE model = %s
                    """,
                    (plan.model,),
                )
                next_generation = int((_row(cursor) or {})["next_generation"])
                parent_generation = (
                    int(active["auto_pool_generation"]) if active is not None else None
                )
                cursor.execute(
                    """
                    UPDATE a6_trade.provider_auto_pool_generations
                    SET generation_state = 'failed', completed_at = now(), updated_at = now()
                    WHERE model = %s AND generation_state IN ('planned', 'applying')
                    """,
                    (plan.model,),
                )
                cursor.execute(
                    """
                    INSERT INTO a6_trade.provider_auto_pool_generations (
                        model, auto_pool_generation, parent_auto_pool_generation,
                        routing_generation, cost_generation, source_hash,
                        observer_scan_ids, selected_channel_ids,
                        max_ratio_cny, max_ratio_usd, target_channel_count,
                        minimum_observers, hard_max_ratio_cny, trigger_reason,
                        generation_state, observed_at, config_version, plan_payload
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s, 'applying', %s, %s, %s::jsonb
                    ) RETURNING id
                    """,
                    (
                        plan.model,
                        next_generation,
                        parent_generation,
                        routing_generation,
                        cost_generation,
                        source_hash,
                        list(scan_ids),
                        [item.channel_id for item in plan.selected],
                        plan.max_ratio,
                        plan.max_ratio_usd,
                        target_channel_count,
                        minimum_observers,
                        hard_max_ratio_cny,
                        trigger_reason,
                        observed_at,
                        config_version,
                        json.dumps(payload, ensure_ascii=True, sort_keys=True),
                    ),
                )
                generation_id = int((_row(cursor) or {})["id"])
                for item in normalized_accounts:
                    cursor.execute(
                        """
                        INSERT INTO a6_trade.provider_auto_pool_accounts (
                            generation_id, merchant_id, token_ref_id,
                            external_token_id, enodot_account_id, slot_no
                        ) VALUES (%s, %s, %s, %s, %s, %s)
                        """,
                        (
                            generation_id,
                            item.merchant_id,
                            item.token_ref_id,
                            item.external_token_id,
                            item.enodot_account_id,
                            item.slot_no,
                        ),
                    )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            _close(connection)
        return ProviderAutoPoolGeneration(
            generation_id=generation_id,
            generation=next_generation,
            parent_generation=parent_generation,
            model=plan.model,
            routing_generation=routing_generation,
            cost_generation=cost_generation,
            source_hash=source_hash,
        )

    def claim_accounts(
        self,
        generation: ProviderAutoPoolGeneration,
        accounts: Sequence[ProviderAutoPoolAccount],
        *,
        claim_token: str,
        claimed_by: str,
    ) -> None:
        normalized = tuple(sorted(accounts, key=lambda item: item.slot_no))
        token = _text(claim_token, "provider_auto_pool_claim_token_invalid")
        owner = _text(claimed_by, "provider_auto_pool_claim_owner_invalid")
        if not normalized or len(token) > 128 or len(owner) > 160:
            raise ProviderAutoPoolError("provider_auto_pool_account_claim_invalid")
        expected = {
            (
                account.merchant_id,
                account.token_ref_id,
                account.enodot_account_id,
                account.slot_no,
            )
            for account in normalized
        }
        if len(expected) != len(normalized) or any(
            account.routing_generation != generation.routing_generation
            or account.cost_generation != generation.cost_generation
            for account in normalized
        ):
            raise ProviderAutoPoolError("provider_auto_pool_account_claim_invalid")
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT generation_state, routing_generation, cost_generation
                    FROM a6_trade.provider_auto_pool_generations
                    WHERE id = %s AND model = %s
                    FOR UPDATE
                    """,
                    (generation.generation_id, generation.model),
                )
                generation_row = _row(cursor)
                expected_state = "active" if generation.reused_active else "applying"
                if generation_row is None or (
                    str(generation_row["generation_state"]) != expected_state
                    or int(generation_row["routing_generation"])
                    != generation.routing_generation
                    or int(generation_row["cost_generation"])
                    != generation.cost_generation
                ):
                    raise ProviderAutoPoolError(
                        "provider_auto_pool_generation_claim_conflict"
                    )
                cursor.execute(
                    """
                    SELECT merchant_id, token_ref_id, enodot_account_id, slot_no,
                           account_state,
                           (
                               claim_token IS NULL OR claim_token = %s
                               OR claimed_at < now() - (%s * interval '1 second')
                           ) AS claimable
                    FROM a6_trade.provider_auto_pool_accounts
                    WHERE generation_id = %s
                    ORDER BY slot_no
                    FOR UPDATE
                    """,
                    (token, self.stale_claim_seconds, generation.generation_id),
                )
                rows = _rows(cursor)
                observed = {
                    (
                        int(row["merchant_id"]),
                        int(row["token_ref_id"]),
                        int(row["enodot_account_id"]),
                        int(row["slot_no"]),
                    )
                    for row in rows
                }
                if observed != expected or any(
                    str(row["account_state"])
                    not in {"pending", "applying", "ready", "failed"}
                    for row in rows
                ):
                    raise ProviderAutoPoolError(
                        "provider_auto_pool_account_claim_identity_conflict"
                    )
                if any(not bool(row["claimable"]) for row in rows):
                    raise ProviderAutoPoolError(
                        "provider_auto_pool_account_claim_busy"
                    )
                cursor.execute(
                    """
                    UPDATE a6_trade.provider_auto_pool_accounts
                    SET account_state = 'applying', started_at = coalesce(started_at, now()),
                        claim_token = %s, claimed_by = %s, claimed_at = now(),
                        subscription_id = NULL,
                        visible_owned_channel_ids = '{}',
                        disabled_owned_channel_ids = '{}',
                        absent_owned_channel_ids = '{}',
                        ban_confirmed = false, unpin_confirmed = false,
                        auto_pool_confirmed = false, cost_confirmed = false,
                        schedulable_confirmed = false,
                        observed_max_ratio_cny = NULL,
                        observed_max_ratio_usd = NULL,
                        readback_hash = NULL, remote_readback = '{}'::jsonb,
                        completed_at = NULL, error_class = NULL, updated_at = now()
                    WHERE generation_id = %s
                      AND account_state IN ('pending', 'applying', 'ready', 'failed')
                      AND (
                          claim_token IS NULL OR claim_token = %s
                          OR claimed_at < now() - (%s * interval '1 second')
                      )
                    """,
                    (
                        token,
                        owner,
                        generation.generation_id,
                        token,
                        self.stale_claim_seconds,
                    ),
                )
                if cursor.rowcount != len(normalized):
                    raise ProviderAutoPoolError("provider_auto_pool_account_claim_conflict")
                cursor.execute(
                    """
                    UPDATE a6_trade.provider_pool_slots slot
                    SET slot_state = 'REPAIRING', desired_schedulable = false,
                        observed_schedulable = false,
                        state_version = slot.state_version + 1, updated_at = now()
                    FROM a6_trade.provider_auto_pool_accounts account
                    WHERE account.generation_id = %s
                      AND account.claim_token = %s
                      AND slot.model = %s AND slot.routing_generation = %s
                      AND slot.slot_no = account.slot_no
                      AND slot.enodot_account_id = account.enodot_account_id
                      AND slot.a6_token_id = account.token_ref_id
                      AND slot.valid_to IS NULL
                    """,
                    (
                        generation.generation_id,
                        token,
                        _model_from_generation(generation),
                        generation.routing_generation,
                    ),
                )
                if cursor.rowcount != len(normalized):
                    raise ProviderAutoPoolError("provider_auto_pool_slot_pause_conflict")
                cursor.execute(
                    """
                    UPDATE a6_trade.provider_inventory inventory
                    SET inventory_state = 'REPAIRING', schedulable = false,
                        assigned_slot_no = account.slot_no,
                        state_version = inventory.state_version + 1,
                        updated_at = now()
                    FROM a6_trade.provider_auto_pool_accounts account
                    WHERE account.generation_id = %s
                      AND account.claim_token = %s
                      AND inventory.model = %s
                      AND inventory.enodot_account_id = account.enodot_account_id
                      AND inventory.a6_token_id = account.token_ref_id
                    """,
                    (
                        generation.generation_id,
                        token,
                        _model_from_generation(generation),
                    ),
                )
                if cursor.rowcount != len(normalized):
                    raise ProviderAutoPoolError(
                        "provider_auto_pool_inventory_pause_conflict"
                    )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            _close(connection)

    def record_prepared(
        self,
        generation: ProviderAutoPoolGeneration,
        account: ProviderAutoPoolAccount,
        *,
        claim_token: str,
        subscription_id: int,
        visible_owned_channel_ids: Sequence[int],
        disabled_owned_channel_ids: Sequence[int],
        max_ratio_cny: Decimal,
        max_ratio_usd: Decimal,
        readback: Mapping[str, Any],
    ) -> None:
        token = _text(claim_token, "provider_auto_pool_claim_token_invalid")
        encoded = json.dumps(dict(readback), ensure_ascii=True, sort_keys=True)
        digest = hashlib.sha256(encoded.encode()).hexdigest()
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE a6_trade.provider_auto_pool_accounts
                    SET subscription_id = %s,
                        visible_owned_channel_ids = %s,
                        disabled_owned_channel_ids = %s,
                        absent_owned_channel_ids = '{}',
                        ban_confirmed = true, unpin_confirmed = true,
                        auto_pool_confirmed = true, cost_confirmed = true,
                        observed_max_ratio_cny = %s,
                        observed_max_ratio_usd = %s,
                        readback_hash = %s, remote_readback = %s::jsonb,
                        error_class = NULL, updated_at = now()
                    WHERE generation_id = %s AND merchant_id = %s
                      AND account_state = 'applying'
                      AND claim_token = %s
                      AND EXISTS (
                          SELECT 1
                          FROM a6_trade.provider_auto_pool_generations generation
                          WHERE generation.id = a6_trade.provider_auto_pool_accounts.generation_id
                            AND generation.max_ratio_cny = %s
                            AND generation.max_ratio_usd = %s
                      )
                    """,
                    (
                        _positive(subscription_id),
                        sorted({_positive(value) for value in visible_owned_channel_ids}),
                        sorted({_positive(value) for value in disabled_owned_channel_ids}),
                        max_ratio_cny,
                        max_ratio_usd,
                        digest,
                        encoded,
                        generation.generation_id,
                        account.merchant_id,
                        token,
                        max_ratio_cny,
                        max_ratio_usd,
                    ),
                )
                if cursor.rowcount != 1:
                    cursor.execute(
                        """
                        SELECT max_ratio_cny, max_ratio_usd
                        FROM a6_trade.provider_auto_pool_generations
                        WHERE id = %s
                        """,
                        (generation.generation_id,),
                    )
                    generation_row = _row(cursor)
                    if generation_row is not None and (
                        Decimal(str(generation_row["max_ratio_cny"])) != max_ratio_cny
                        or Decimal(str(generation_row["max_ratio_usd"])) != max_ratio_usd
                    ):
                        raise ProviderAutoPoolError(
                            "provider_auto_pool_account_cost_generation_mismatch"
                        )
                    raise ProviderAutoPoolError(
                        "provider_auto_pool_account_prepare_conflict"
                    )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            _close(connection)

    def record_ready(
        self,
        generation: ProviderAutoPoolGeneration,
        account: ProviderAutoPoolAccount,
        *,
        claim_token: str,
    ) -> None:
        token = _text(claim_token, "provider_auto_pool_claim_token_invalid")
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE a6_trade.provider_auto_pool_accounts
                    SET account_state = 'ready', schedulable_confirmed = true,
                        completed_at = now(), error_class = NULL, updated_at = now()
                    WHERE generation_id = %s AND merchant_id = %s
                      AND account_state = 'applying'
                      AND claim_token = %s
                      AND ban_confirmed AND unpin_confirmed
                      AND auto_pool_confirmed AND cost_confirmed
                    """,
                    (generation.generation_id, account.merchant_id, token),
                )
                if cursor.rowcount != 1:
                    raise ProviderAutoPoolError("provider_auto_pool_account_ready_conflict")
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            _close(connection)

    def record_failed(
        self,
        generation: ProviderAutoPoolGeneration,
        account: ProviderAutoPoolAccount,
        *,
        claim_token: str,
        error_class: str,
    ) -> None:
        token = _text(claim_token, "provider_auto_pool_claim_token_invalid")
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE a6_trade.provider_auto_pool_accounts
                    SET account_state = 'failed', schedulable_confirmed = false,
                        error_class = %s, completed_at = now(), updated_at = now()
                    WHERE generation_id = %s AND merchant_id = %s
                      AND account_state IN ('pending', 'applying', 'ready', 'failed')
                      AND claim_token = %s
                    """,
                    (
                        _text(error_class, "provider_auto_pool_error_class_invalid")[:160],
                        generation.generation_id,
                        account.merchant_id,
                        token,
                    ),
                )
                if cursor.rowcount != 1:
                    raise ProviderAutoPoolError(
                        "provider_auto_pool_account_failure_conflict"
                    )
                cursor.execute(
                    """
                    UPDATE a6_trade.provider_pool_slots
                    SET slot_state = 'REPAIRING', desired_schedulable = false,
                        observed_schedulable = false,
                        state_version = state_version + 1, updated_at = now()
                    WHERE model = %s AND routing_generation = %s
                      AND slot_no = %s AND enodot_account_id = %s
                      AND a6_token_id = %s AND valid_to IS NULL
                    """,
                    (
                        _model_from_generation(generation),
                        generation.routing_generation,
                        account.slot_no,
                        account.enodot_account_id,
                        account.token_ref_id,
                    ),
                )
                cursor.execute(
                    """
                    UPDATE a6_trade.provider_inventory
                    SET inventory_state = 'REPAIRING', schedulable = false,
                        state_version = state_version + 1, updated_at = now()
                    WHERE model = %s AND enodot_account_id = %s
                      AND a6_token_id = %s
                    """,
                    (
                        _model_from_generation(generation),
                        account.enodot_account_id,
                        account.token_ref_id,
                    ),
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            _close(connection)

    def activate(
        self,
        model: str,
        generation: ProviderAutoPoolGeneration,
        *,
        minimum_ready: int,
    ) -> int:
        model = _model(model)
        if isinstance(minimum_ready, bool) or minimum_ready < 1:
            raise ProviderAutoPoolError("provider_auto_pool_minimum_ready_invalid")
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT pg_advisory_xact_lock(%s, hashtext(%s))",
                    (0x41364150, model),
                )
                cursor.execute(
                    """
                    SELECT generation_state, routing_generation, cost_generation,
                           max_ratio_cny, max_ratio_usd
                    FROM a6_trade.provider_auto_pool_generations
                    WHERE id = %s AND model = %s
                    FOR UPDATE
                    """,
                    (generation.generation_id, model),
                )
                row = _row(cursor)
                expected_state = "active" if generation.reused_active else "applying"
                if row is None or str(row["generation_state"]) != expected_state:
                    raise ProviderAutoPoolError("provider_auto_pool_activation_conflict")
                if (
                    int(row["routing_generation"]) != generation.routing_generation
                    or int(row["cost_generation"]) != generation.cost_generation
                ):
                    raise ProviderAutoPoolError("provider_auto_pool_parent_generation_changed")
                cursor.execute(
                    """
                    SELECT count(*) AS account_count,
                           count(*) FILTER (
                               WHERE account_state = 'ready'
                           ) AS ready_count
                    FROM a6_trade.provider_auto_pool_accounts
                    WHERE generation_id = %s AND account_state = 'ready'
                    """,
                    (generation.generation_id,),
                )
                account_counts = _row(cursor) or {}
                account_count = int(account_counts["account_count"])
                ready_count = int(account_counts["ready_count"])
                if ready_count < minimum_ready:
                    raise ProviderAutoPoolError("provider_auto_pool_minimum_ready_not_met")
                cursor.execute(
                    """
                    SELECT generation, cost_generation
                    FROM a6_trade.provider_pool_generations
                    WHERE model = %s AND generation_state = 'active'
                    FOR UPDATE
                    """,
                    (model,),
                )
                active = _row(cursor)
                if active is None or (
                    int(active["generation"]) != generation.routing_generation
                    or int(active["cost_generation"]) != generation.cost_generation
                ):
                    raise ProviderAutoPoolError("provider_auto_pool_parent_generation_changed")
                cursor.execute(
                    """
                    UPDATE a6_trade.provider_pin_assignment_epochs epoch
                    SET ended_routing_generation = %s, ended_cost_generation = %s,
                        ended_at = now(), updated_at = now()
                    FROM a6_trade.provider_pool_slots slot
                    JOIN a6_trade.provider_auto_pool_accounts account
                      ON account.enodot_account_id = slot.enodot_account_id
                     AND account.token_ref_id = slot.a6_token_id
                     AND account.slot_no = slot.slot_no
                    WHERE account.generation_id = %s
                      AND account.account_state = 'ready'
                      AND slot.model = %s AND slot.routing_generation = %s
                      AND slot.valid_to IS NULL
                      AND epoch.id = slot.pin_assignment_epoch_id
                      AND epoch.ended_at IS NULL
                    """,
                    (
                        generation.routing_generation,
                        generation.cost_generation,
                        generation.generation_id,
                        model,
                        generation.routing_generation,
                    ),
                )
                cursor.execute(
                    """
                    UPDATE a6_trade.a6_pin_bindings pin
                    SET valid_to = now()
                    FROM a6_trade.provider_auto_pool_accounts account
                    WHERE account.generation_id = %s
                      AND account.account_state = 'ready'
                      AND pin.a6_token_id = account.token_ref_id
                      AND pin.model = %s AND pin.valid_to IS NULL
                    """,
                    (generation.generation_id, model),
                )
                cursor.execute(
                    """
                    UPDATE a6_trade.provider_pool_slots slot
                    SET route_mode = 'auto_pool', desired_channel_id = NULL,
                        desired_slot_cost_multiplier = %s,
                        observed_rate_multiplier = %s,
                        slot_state = 'READY', desired_schedulable = true,
                        observed_schedulable = true,
                        confirmed_pin_binding_id = NULL, pin_confirmed = false,
                        probe_confirmed = false, pin_assignment_epoch_id = NULL,
                        auto_pool_generation_id = %s,
                        auto_pool_subscription_id = account.subscription_id,
                        auto_pool_confirmed = true,
                        auto_pool_max_ratio_cny = %s,
                        state_version = state_version + 1, updated_at = now()
                    FROM a6_trade.provider_auto_pool_accounts account
                    WHERE account.generation_id = %s
                      AND account.account_state = 'ready'
                      AND slot.model = %s AND slot.routing_generation = %s
                      AND slot.valid_to IS NULL
                      AND slot.slot_no = account.slot_no
                      AND slot.enodot_account_id = account.enodot_account_id
                      AND slot.a6_token_id = account.token_ref_id
                    """,
                    (
                        row["max_ratio_usd"],
                        row["max_ratio_usd"],
                        generation.generation_id,
                        row["max_ratio_cny"],
                        generation.generation_id,
                        model,
                        generation.routing_generation,
                    ),
                )
                if cursor.rowcount != ready_count:
                    raise ProviderAutoPoolError("provider_auto_pool_slot_activation_conflict")
                cursor.execute(
                    """
                    UPDATE a6_trade.provider_inventory inventory
                    SET inventory_state = 'READY', schedulable = true,
                        assigned_slot_no = slot.slot_no,
                        state_version = inventory.state_version + 1,
                        updated_at = now()
                    FROM a6_trade.provider_pool_slots slot
                    WHERE slot.model = %s AND slot.routing_generation = %s
                      AND slot.valid_to IS NULL AND slot.route_mode = 'auto_pool'
                      AND slot.slot_state = 'READY' AND slot.observed_schedulable
                      AND inventory.model = slot.model
                      AND inventory.enodot_account_id = slot.enodot_account_id
                      AND inventory.a6_token_id = slot.a6_token_id
                    """,
                    (model, generation.routing_generation),
                )
                cursor.execute(
                    """
                    UPDATE a6_trade.provider_auto_pool_accounts
                    SET claim_token = NULL, claimed_by = NULL, claimed_at = NULL,
                        updated_at = now()
                    WHERE generation_id = %s AND claim_token IS NOT NULL
                    """,
                    (generation.generation_id,),
                )
                if cursor.rowcount != account_count:
                    raise ProviderAutoPoolError(
                        "provider_auto_pool_account_claim_release_conflict"
                    )
                if not generation.reused_active:
                    cursor.execute(
                        """
                        UPDATE a6_trade.provider_auto_pool_generations
                        SET generation_state = 'superseded', completed_at = now(),
                            updated_at = now()
                        WHERE model = %s AND generation_state = 'active' AND id <> %s
                        """,
                        (model, generation.generation_id),
                    )
                    cursor.execute(
                        """
                        UPDATE a6_trade.provider_auto_pool_generations
                        SET generation_state = 'active', activated_at = now(),
                            completed_at = now(), updated_at = now()
                        WHERE id = %s AND generation_state = 'applying'
                        """,
                        (generation.generation_id,),
                    )
                    if cursor.rowcount != 1:
                        raise ProviderAutoPoolError(
                            "provider_auto_pool_activation_conflict"
                        )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            _close(connection)
        return ready_count

    def fail_generation(
        self,
        generation: ProviderAutoPoolGeneration,
        *,
        error_class: str,
    ) -> None:
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                if generation.reused_active:
                    cursor.execute(
                        """
                        UPDATE a6_trade.provider_auto_pool_generations
                        SET updated_at = now()
                        WHERE id = %s AND generation_state = 'active'
                        """,
                        (generation.generation_id,),
                    )
                else:
                    cursor.execute(
                        """
                        UPDATE a6_trade.provider_auto_pool_generations
                        SET generation_state = 'failed', completed_at = now(),
                            updated_at = now()
                        WHERE id = %s
                          AND generation_state IN ('planned', 'applying')
                        """,
                        (generation.generation_id,),
                    )
                cursor.execute(
                    """
                    UPDATE a6_trade.provider_auto_pool_accounts
                    SET account_state = 'failed', schedulable_confirmed = false,
                        error_class = coalesce(error_class, %s),
                        completed_at = coalesce(completed_at, now()),
                        claim_token = NULL, claimed_by = NULL, claimed_at = NULL,
                        updated_at = now()
                    WHERE generation_id = %s
                      AND account_state IN ('pending', 'applying', 'ready')
                    """,
                    (
                        _text(error_class, "provider_auto_pool_error_class_invalid")[:160],
                        generation.generation_id,
                    ),
                )
                cursor.execute(
                    """
                    UPDATE a6_trade.provider_auto_pool_accounts
                    SET claim_token = NULL, claimed_by = NULL, claimed_at = NULL,
                        updated_at = now()
                    WHERE generation_id = %s AND claim_token IS NOT NULL
                    """,
                    (generation.generation_id,),
                )
                cursor.execute(
                    """
                    UPDATE a6_trade.provider_pool_slots slot
                    SET slot_state = 'REPAIRING', desired_schedulable = false,
                        observed_schedulable = false,
                        state_version = state_version + 1, updated_at = now()
                    FROM a6_trade.provider_auto_pool_accounts account
                    WHERE account.generation_id = %s
                      AND account.account_state = 'failed'
                      AND slot.model = %s
                      AND slot.routing_generation = %s
                      AND slot.valid_to IS NULL
                      AND slot.slot_no = account.slot_no
                      AND slot.enodot_account_id = account.enodot_account_id
                      AND slot.a6_token_id = account.token_ref_id
                    """,
                    (
                        generation.generation_id,
                        _model_from_generation(generation),
                        generation.routing_generation,
                    ),
                )
                cursor.execute(
                    """
                    UPDATE a6_trade.provider_inventory inventory
                    SET inventory_state = 'REPAIRING', schedulable = false,
                        state_version = inventory.state_version + 1,
                        updated_at = now()
                    FROM a6_trade.provider_auto_pool_accounts account
                    WHERE account.generation_id = %s
                      AND account.account_state = 'failed'
                      AND inventory.model = %s
                      AND inventory.enodot_account_id = account.enodot_account_id
                      AND inventory.a6_token_id = account.token_ref_id
                    """,
                    (generation.generation_id, _model_from_generation(generation)),
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            _close(connection)


def _plan_payload(plan: AutoPoolPlan) -> dict[str, Any]:
    return {
        "model": plan.model,
        "min_ratio_cny": str(plan.min_ratio),
        "max_ratio_cny": str(plan.max_ratio),
        "max_ratio_usd": str(plan.max_ratio_usd),
        "source_hash": plan.source_hash,
        "selected": [
            {
                "channel_id": item.channel_id,
                "supplier_id": item.supplier_id,
                "listing_id": item.listing_id,
                "ratio_cny": str(item.ratio_cny),
                "ratio_usd": str(item.ratio_usd),
                "observer_ids": list(item.observer_ids),
            }
            for item in plan.selected
        ],
        "excluded": [list(item) for item in plan.excluded],
    }


def _account(row: Mapping[str, Any]) -> ProviderAutoPoolAccount:
    session_ref = str(row.get("session_ref") or "").strip()
    if not session_ref:
        raise ProviderAutoPoolError("provider_auto_pool_session_ref_invalid")
    return ProviderAutoPoolAccount(
        merchant_id=_positive(row.get("merchant_id")),
        a6_user_id=_positive(row.get("a6_user_id")),
        session_ref=session_ref,
        token_ref_id=_positive(row.get("token_ref_id")),
        external_token_id=_positive(row.get("external_token_id")),
        enodot_account_id=_positive(row.get("enodot_account_id")),
        slot_no=_positive(row.get("slot_no")),
        routing_generation=_positive(row.get("routing_generation")),
        cost_generation=_positive(row.get("cost_generation")),
    )


def _model_from_generation(generation: ProviderAutoPoolGeneration) -> str:
    return _model(generation.model)


def _model(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        raise ProviderAutoPoolError("provider_auto_pool_model_invalid")
    return text


def _text(value: object, code: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ProviderAutoPoolError(code)
    return text


def _positive(value: object) -> int:
    if value in (None, "") or isinstance(value, bool):
        raise ProviderAutoPoolError("provider_auto_pool_identity_invalid")
    try:
        result = int(value)
    except (TypeError, ValueError):
        raise ProviderAutoPoolError("provider_auto_pool_identity_invalid") from None
    if result < 1:
        raise ProviderAutoPoolError("provider_auto_pool_identity_invalid")
    return result


def _nonnegative(value: object) -> int:
    if value in (None, "") or isinstance(value, bool):
        raise ProviderAutoPoolError("provider_auto_pool_count_invalid")
    try:
        result = int(value)
    except (TypeError, ValueError):
        raise ProviderAutoPoolError("provider_auto_pool_count_invalid") from None
    if result < 0:
        raise ProviderAutoPoolError("provider_auto_pool_count_invalid")
    return result


def _positive_decimal(value: object, code: str) -> Decimal:
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ProviderAutoPoolError(code) from None
    if not result.is_finite() or result <= 0:
        raise ProviderAutoPoolError(code)
    return result


def _optional_int(value: object) -> int | None:
    return None if value is None else int(value)


def _one(values: Sequence[int] | Any) -> int:
    unique = {int(value) for value in values}
    if len(unique) != 1:
        raise ProviderAutoPoolError("provider_auto_pool_generation_identity_conflict")
    return next(iter(unique))


def _sha256(value: object) -> str:
    text = str(value or "").strip().casefold()
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise ProviderAutoPoolError("provider_auto_pool_source_hash_invalid")
    return text


def _hash(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def _row(cursor: Any) -> dict[str, Any] | None:
    raw = cursor.fetchone()
    if raw is None:
        return None
    if isinstance(raw, Mapping):
        return dict(raw)
    description = getattr(cursor, "description", None) or ()
    return {str(item[0]): value for item, value in zip(description, raw, strict=False)}


def _rows(cursor: Any) -> list[dict[str, Any]]:
    raw = cursor.fetchall()
    if not raw:
        return []
    if isinstance(raw[0], Mapping):
        return [dict(item) for item in raw]
    description = getattr(cursor, "description", None) or ()
    return [
        {str(item[0]): value for item, value in zip(description, row, strict=False)}
        for row in raw
    ]


def _close(connection: Any) -> None:
    close = getattr(connection, "close", None)
    if callable(close):
        close()
