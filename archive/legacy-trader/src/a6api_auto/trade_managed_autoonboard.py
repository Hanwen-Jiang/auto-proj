from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Protocol

from .channel_policy import (
    FORBIDDEN_CHANNEL_NAME,
    GENERATED_CHANNEL_NAME_PATTERN,
    GENERATED_CHANNEL_NAME_SIGNAL_PATTERN,
)
from .errors import A6AutoError
from .mail import QQMailbox
from .merchant_policy import FOCUS_OPERATING_GROUP
from .models import SessionRecord
from .storage import SessionStore
from .templates import choose_account_template
from .trade_managed_listings import (
    SUPPORTED_MODEL,
    ExistingChannelSelection,
    ManagedListingController,
    ManagedListingError,
    ManagedListingSpec,
    ManagedListingStore,
    ManagedMerchant,
    is_source_only_listing_description,
    stable_managed_uid,
)
from .trade_seller_orchestration import GPT_5_6_SOL_OFFICIAL_PRICES
from .workflow import Workflow

DEFAULT_INVENTORY_MAX_AGE_SECONDS = 15 * 60
DEFAULT_TARGET_LIMIT = 100
DEFAULT_SEED_PRICES = GPT_5_6_SOL_OFFICIAL_PRICES
PRICE_SEED_FOLLOWER_STRATEGY = "managed_listing_price_seed_follower_v1"
_AUDIT_LABEL = re.compile(r"^[a-z0-9][a-z0-9:._-]{0,127}$")
_KNOWN_CHANNEL_BINDING_STATES = frozenset(
    {
        "planned",
        "creating",
        "create_unknown",
        "verifying",
        "verified",
        "updating",
        "update_unknown",
        "error",
        "retired",
    }
)
_ACTIONABLE_CHANNEL_BINDING_STATES = frozenset(
    {"verified", "updating", "update_unknown"}
)


class ManagedAutoOnboardError(A6AutoError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class ManagedAutoOnboardPricePlan:
    source_action_id: int
    source_entity_id: str
    rank: int
    tier: str
    price_zone: str
    log_position: str
    delta: str
    target_final_multiplier: str
    panel_multiplier: str

    def __post_init__(self) -> None:
        if (
            isinstance(self.source_action_id, bool)
            or not isinstance(self.source_action_id, int)
            or self.source_action_id < 1
            or not str(self.source_entity_id).strip()
            or isinstance(self.rank, bool)
            or not isinstance(self.rank, int)
            or self.rank < 1
            or not str(self.tier).strip()
            or self.price_zone not in {"low", "middle", "high"}
        ):
            raise ManagedAutoOnboardError("managed_autoonboard_listing_seed_plan_invalid")
        for value in (
            self.log_position,
            self.delta,
            self.target_final_multiplier,
            self.panel_multiplier,
        ):
            try:
                parsed = Decimal(str(value))
            except (InvalidOperation, ValueError):
                raise ManagedAutoOnboardError(
                    "managed_autoonboard_listing_seed_plan_invalid"
                ) from None
            if not parsed.is_finite() or parsed < 0:
                raise ManagedAutoOnboardError(
                    "managed_autoonboard_listing_seed_plan_invalid"
                )


@dataclass(frozen=True)
class ManagedAutoOnboardTarget:
    merchant_id: int
    a6_user_id: int
    supplier_id: int
    roles: tuple[str, ...]
    session_ref: str
    a6_channel_id: int
    channel_managed_uid: str | None = None
    channel_control_state: str | None = None
    listing_managed_uid: str | None = None
    a6_listing_id: int | None = None
    listing_status: int | None = None
    listing_seed_prices: tuple[int, int, int, int] | None = None
    listing_seed_source: str | None = None
    listing_seed_plan: ManagedAutoOnboardPricePlan | None = None

    def __post_init__(self) -> None:
        for value in (
            self.merchant_id,
            self.a6_user_id,
            self.supplier_id,
            self.a6_channel_id,
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ManagedAutoOnboardError("managed_autoonboard_target_identity_invalid")
        roles = tuple(str(role) for role in self.roles)
        valid_roles = roles == ("seller",) or (
            len(roles) == 2 and roles[0] == "seller" and roles[1] in {"provider", "observer"}
        )
        if not valid_roles:
            raise ManagedAutoOnboardError("managed_autoonboard_target_roles_invalid")
        session_ref = str(self.session_ref).strip()
        if not session_ref:
            raise ManagedAutoOnboardError("managed_autoonboard_target_session_invalid")
        managed_uid = str(self.channel_managed_uid or "").strip() or None
        control_state = str(self.channel_control_state or "").strip() or None
        if (managed_uid is None) != (control_state is None):
            raise ManagedAutoOnboardError("managed_autoonboard_channel_binding_invalid")
        if (
            control_state is not None
            and control_state not in _KNOWN_CHANNEL_BINDING_STATES
        ):
            raise ManagedAutoOnboardError("managed_autoonboard_channel_binding_invalid")
        listing_uid = str(self.listing_managed_uid or "").strip() or None
        listing_identity = (self.a6_listing_id, self.listing_status)
        if listing_uid is None:
            if listing_identity != (None, None):
                raise ManagedAutoOnboardError("managed_autoonboard_listing_binding_invalid")
        elif (
            isinstance(self.a6_listing_id, bool)
            or not isinstance(self.a6_listing_id, int)
            or self.a6_listing_id < 1
            or isinstance(self.listing_status, bool)
            or not isinstance(self.listing_status, int)
            or self.listing_status not in {0, 2}
        ):
            raise ManagedAutoOnboardError("managed_autoonboard_listing_binding_invalid")
        seed_prices = self.listing_seed_prices
        seed_source = str(self.listing_seed_source or "").strip() or None
        if seed_prices is not None:
            seed_prices = tuple(seed_prices)
            if len(seed_prices) != 4 or any(
                isinstance(value, bool) or not isinstance(value, int) or value < 1
                for value in seed_prices
            ):
                raise ManagedAutoOnboardError("managed_autoonboard_listing_seed_invalid")
        if (seed_prices is None) != (seed_source is None):
            raise ManagedAutoOnboardError("managed_autoonboard_listing_seed_invalid")
        if seed_source not in {None, "merchant_managed_listing", "fleet_managed_listing"}:
            raise ManagedAutoOnboardError("managed_autoonboard_listing_seed_invalid")
        if self.listing_seed_plan is not None and seed_prices is None:
            raise ManagedAutoOnboardError("managed_autoonboard_listing_seed_plan_invalid")
        object.__setattr__(self, "roles", roles)
        object.__setattr__(self, "session_ref", session_ref)
        object.__setattr__(self, "channel_managed_uid", managed_uid)
        object.__setattr__(self, "channel_control_state", control_state)
        object.__setattr__(self, "listing_managed_uid", listing_uid)
        object.__setattr__(self, "listing_seed_prices", seed_prices)
        object.__setattr__(self, "listing_seed_source", seed_source)


@dataclass(frozen=True)
class ManagedAutoOnboardRequest:
    model: str = SUPPORTED_MODEL
    seed_prices: tuple[int, int, int, int] = DEFAULT_SEED_PRICES
    inventory_max_age_seconds: int = DEFAULT_INVENTORY_MAX_AGE_SECONDS
    target_limit: int = DEFAULT_TARGET_LIMIT
    require_confirmed_price_seed: bool = False

    def __post_init__(self) -> None:
        model = str(self.model).strip()
        if model != SUPPORTED_MODEL:
            raise ManagedAutoOnboardError("managed_autoonboard_model_unsupported")
        prices = tuple(self.seed_prices)
        if len(prices) != 4 or any(
            isinstance(value, bool) or not isinstance(value, int) or value < 1
            for value in prices
        ):
            raise ManagedAutoOnboardError("managed_autoonboard_seed_prices_invalid")
        if (
            isinstance(self.inventory_max_age_seconds, bool)
            or not 60 <= self.inventory_max_age_seconds <= 86_400
        ):
            raise ManagedAutoOnboardError("managed_autoonboard_inventory_age_invalid")
        if isinstance(self.target_limit, bool) or not 1 <= self.target_limit <= 1000:
            raise ManagedAutoOnboardError("managed_autoonboard_target_limit_invalid")
        if not isinstance(self.require_confirmed_price_seed, bool):
            raise ManagedAutoOnboardError(
                "managed_autoonboard_price_seed_policy_invalid"
            )
        object.__setattr__(self, "model", model)
        object.__setattr__(self, "seed_prices", prices)


@dataclass(frozen=True)
class ManagedAutoOnboardItemResult:
    merchant_id: int
    a6_user_id: int
    a6_channel_id: int
    channel: Mapping[str, Any]
    listing: Mapping[str, Any]
    listing_seed_source: str
    error_class: str | None = None

    def public(self) -> dict[str, Any]:
        return {
            "merchant_id": self.merchant_id,
            "a6_user_id": self.a6_user_id,
            "a6_channel_id": self.a6_channel_id,
            "channel": dict(self.channel),
            "listing": dict(self.listing),
            "listing_seed_source": self.listing_seed_source,
            "error_class": self.error_class,
        }


@dataclass(frozen=True)
class ManagedAutoOnboardBatchResult:
    apply: bool
    model: str
    targets: tuple[ManagedAutoOnboardItemResult, ...]

    def summary(self) -> dict[str, Any]:
        failed = sum(item.error_class is not None for item in self.targets)
        verified = sum(
            item.error_class is None and item.listing.get("state") == "verified"
            for item in self.targets
        )
        return {
            "success": failed == 0,
            "mode": "applied" if self.apply else "dry-run",
            "operation": "auto_onboard_seller_channels",
            "model": self.model,
            "target_count": len(self.targets),
            "verified_count": verified,
            "planned_count": sum(
                item.error_class is None and item.listing.get("state") == "planned"
                for item in self.targets
            ),
            "failed": failed,
            "targets": [item.public() for item in self.targets],
        }


class ManagedAutoOnboardDiscovery(Protocol):
    def list_targets(
        self,
        *,
        model: str,
        inventory_max_age_seconds: int,
        limit: int,
    ) -> tuple[ManagedAutoOnboardTarget, ...]: ...


class PostgresManagedAutoOnboardDiscovery:
    """Find missing Listings and existing Listings that have not joined the price band."""

    def __init__(self, connection_factory: Callable[[], Any]) -> None:
        if not callable(connection_factory):
            raise ManagedAutoOnboardError("managed_autoonboard_connection_factory_invalid")
        self.connection_factory = connection_factory

    def list_targets(
        self,
        *,
        model: str,
        inventory_max_age_seconds: int,
        limit: int,
    ) -> tuple[ManagedAutoOnboardTarget, ...]:
        request = ManagedAutoOnboardRequest(
            model=model,
            inventory_max_age_seconds=inventory_max_age_seconds,
            target_limit=limit,
        )
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    WITH latest_canonical_price_actions AS MATERIALIZED (
                        SELECT DISTINCT ON (action.entity_id)
                               action.id,
                               action.entity_id,
                               action.completed_at,
                               action.desired_state
                        FROM a6_trade.trade_control_actions action
                        WHERE action.action_type = 'apply_seller_price_band'
                          AND action.entity_type = 'a6_managed_listing'
                          AND action.status = 'completed'
                          AND action.completed_at IS NOT NULL
                          AND action.desired_state ->> 'model' = %s
                          AND coalesce(
                              action.desired_state ->> 'pricing_strategy', ''
                          ) <> 'managed_listing_price_seed_follower_v1'
                        ORDER BY action.entity_id,
                                 action.completed_at DESC,
                                 action.id DESC
                    ),
                    confirmed_price_actions AS MATERIALIZED (
                        SELECT action.id AS source_action_id,
                               action.entity_id AS source_entity_id,
                               action.completed_at,
                               seed_binding.merchant_id,
                               (action.desired_state #>>
                                   '{prices_micros,input}')::bigint
                                   AS input_price_micros,
                               (action.desired_state #>>
                                   '{prices_micros,output}')::bigint
                                   AS output_price_micros,
                               (action.desired_state #>>
                                   '{prices_micros,cache_read}')::bigint
                                   AS cache_read_price_micros,
                               (action.desired_state #>>
                                   '{prices_micros,cache_write}')::bigint
                                   AS cache_write_price_micros,
                               (action.desired_state ->> 'rank')::integer AS rank,
                               action.desired_state ->> 'tier' AS tier,
                               action.desired_state ->> 'price_zone' AS price_zone,
                               action.desired_state ->> 'log_position' AS log_position,
                               action.desired_state ->> 'delta' AS delta,
                               action.desired_state ->> 'target_final_multiplier'
                                   AS target_final_multiplier,
                               action.desired_state ->> 'a6_panel_multiplier'
                                   AS panel_multiplier
                        FROM latest_canonical_price_actions action
                        JOIN a6_trade.a6_managed_listing_bindings seed_binding
                          ON seed_binding.managed_uid = action.entity_id
                         AND seed_binding.control_state = 'verified'
                        JOIN a6_trade.a6_managed_channel_bindings seed_channel_binding
                          ON seed_channel_binding.id = seed_binding.channel_binding_id
                         AND seed_channel_binding.control_state = 'verified'
                        JOIN a6_trade.a6_supplier_listings_current seed_listing
                          ON seed_listing.merchant_id = seed_binding.merchant_id
                         AND seed_listing.a6_listing_id = seed_binding.a6_listing_id
                        JOIN a6_trade.a6_merchants seed_merchant
                          ON seed_merchant.id = seed_binding.merchant_id
                        WHERE action.desired_state #>> '{prices_micros,input}'
                              ~ '^[1-9][0-9]*$'
                          AND action.desired_state #>> '{prices_micros,output}'
                              ~ '^[1-9][0-9]*$'
                          AND action.desired_state #>> '{prices_micros,cache_read}'
                              ~ '^[1-9][0-9]*$'
                          AND action.desired_state #>> '{prices_micros,cache_write}'
                              ~ '^[1-9][0-9]*$'
                          AND action.desired_state ->> 'rank' ~ '^[1-9][0-9]*$'
                          AND action.desired_state ->> 'tier' <> ''
                          AND action.desired_state ->> 'price_zone'
                              IN ('low', 'middle', 'high')
                          AND action.desired_state ->> 'log_position'
                              ~ '^[0-9]+([.][0-9]+)?$'
                          AND action.desired_state ->> 'delta'
                              ~ '^[0-9]+([.][0-9]+)?$'
                          AND action.desired_state ->> 'target_final_multiplier'
                              ~ '^[0-9]+([.][0-9]+)?$'
                          AND action.desired_state ->> 'a6_panel_multiplier'
                              ~ '^[0-9]+([.][0-9]+)?$'
                          AND seed_listing.platform_model = %s
                          AND seed_listing.input_price_micros =
                              (action.desired_state #>>
                                  '{prices_micros,input}')::bigint
                          AND seed_listing.output_price_micros =
                              (action.desired_state #>>
                                  '{prices_micros,output}')::bigint
                          AND seed_listing.cache_read_price_micros =
                              (action.desired_state #>>
                                  '{prices_micros,cache_read}')::bigint
                          AND seed_listing.cache_write_price_micros =
                              (action.desired_state #>>
                                  '{prices_micros,cache_write}')::bigint
                          AND seed_merchant.status = 'supplier_submitted'
                          AND seed_merchant.roles @> ARRAY['seller']::text[]
                          AND seed_merchant.poll_error_count = 0
                          AND seed_merchant.last_poll_at IS NOT NULL
                          AND seed_listing.observed_at >=
                              now() - (%s * interval '1 second')
                          AND NOT EXISTS (
                              SELECT 1
                              FROM a6_trade.a6_seller_retirement_plans seed_retirement
                              WHERE seed_retirement.merchant_id = seed_merchant.id
                                AND seed_retirement.completed_at IS NOT NULL
                          )
                    ),
                    confirmed_follower_actions AS MATERIALIZED (
                        SELECT action.id,
                               action.entity_id,
                               action.completed_at,
                               action.desired_state ->> 'source_price_action_id'
                                   AS source_price_action_id,
                               coalesce(
                                   action.desired_state ->> 'expected_listing_status',
                                   action.observed_after ->> 'listing_status'
                               )
                                   AS expected_listing_status,
                               action.desired_state #>> '{prices_micros,input}'
                                   AS input_price_micros,
                               action.desired_state #>> '{prices_micros,output}'
                                   AS output_price_micros,
                               action.desired_state #>> '{prices_micros,cache_read}'
                                   AS cache_read_price_micros,
                               action.desired_state #>> '{prices_micros,cache_write}'
                                   AS cache_write_price_micros
                        FROM a6_trade.trade_control_actions action
                        WHERE action.action_type = 'apply_seller_price_band'
                          AND action.entity_type = 'a6_managed_listing'
                          AND action.status = 'completed'
                          AND action.completed_at IS NOT NULL
                          AND action.desired_state ->> 'model' = %s
                          AND action.desired_state ->> 'pricing_strategy' =
                              'managed_listing_price_seed_follower_v1'
                    )
                    SELECT merchant.id AS merchant_id,
                           merchant.a6_user_id,
                           merchant.supplier_id,
                           merchant.roles,
                           merchant.session_ref,
                           channel.a6_channel_id,
                           channel_binding.managed_uid AS channel_managed_uid,
                           channel_binding.control_state AS channel_control_state,
                           target_listing.managed_uid AS listing_managed_uid,
                           target_listing.a6_listing_id,
                           target_listing.listing_status,
                           price_seed.input_price_micros AS seed_input_price_micros,
                           price_seed.output_price_micros AS seed_output_price_micros,
                           price_seed.cache_read_price_micros AS seed_cache_read_price_micros,
                           price_seed.cache_write_price_micros AS seed_cache_write_price_micros,
                           price_seed.seed_source AS listing_seed_source,
                           price_seed.source_action_id AS seed_source_action_id,
                           price_seed.source_entity_id AS seed_source_entity_id,
                           price_seed.rank AS seed_rank,
                           price_seed.tier AS seed_tier,
                           price_seed.price_zone AS seed_price_zone,
                           price_seed.log_position AS seed_log_position,
                           price_seed.delta AS seed_delta,
                           price_seed.target_final_multiplier
                               AS seed_target_final_multiplier,
                           price_seed.panel_multiplier AS seed_panel_multiplier
                    FROM a6_trade.a6_merchants merchant
                    JOIN a6_trade.a6_supplier_channels_current channel
                      ON channel.merchant_id = merchant.id
                    LEFT JOIN a6_trade.a6_managed_channel_bindings channel_binding
                     ON channel_binding.merchant_id = merchant.id
                     AND channel_binding.a6_channel_id = channel.a6_channel_id
                    LEFT JOIN LATERAL (
                        SELECT listing_binding.managed_uid,
                               listing_binding.a6_listing_id,
                               current_listing.status AS listing_status,
                               current_listing.input_price_micros,
                               current_listing.output_price_micros,
                               current_listing.cache_read_price_micros,
                               current_listing.cache_write_price_micros,
                               current_listing.observed_at
                        FROM a6_trade.a6_managed_listing_bindings listing_binding
                        JOIN a6_trade.a6_supplier_listings_current current_listing
                          ON current_listing.merchant_id = listing_binding.merchant_id
                         AND current_listing.a6_listing_id = listing_binding.a6_listing_id
                        WHERE listing_binding.merchant_id = merchant.id
                          AND listing_binding.channel_binding_id = channel_binding.id
                          AND listing_binding.control_state = 'verified'
                          AND current_listing.platform_model = %s
                          AND current_listing.status IN (0, 2)
                          AND current_listing.observed_at = merchant.last_poll_at
                        ORDER BY listing_binding.id DESC
                        LIMIT 1
                    ) target_listing ON true
                    LEFT JOIN LATERAL (
                        SELECT follower.*
                        FROM confirmed_follower_actions follower
                        WHERE follower.entity_id = target_listing.managed_uid
                          AND follower.expected_listing_status =
                              target_listing.listing_status::text
                          AND ROW(
                              follower.input_price_micros,
                              follower.output_price_micros,
                              follower.cache_read_price_micros,
                              follower.cache_write_price_micros
                          ) = ROW(
                              target_listing.input_price_micros::text,
                              target_listing.output_price_micros::text,
                              target_listing.cache_read_price_micros::text,
                              target_listing.cache_write_price_micros::text
                          )
                        ORDER BY follower.completed_at DESC,
                                 follower.id DESC
                        LIMIT 1
                    ) sticky_follower ON true
                    LEFT JOIN LATERAL (
                        SELECT seed.input_price_micros,
                               seed.output_price_micros,
                               seed.cache_read_price_micros,
                               seed.cache_write_price_micros,
                               CASE
                                   WHEN seed.source_entity_id = target_listing.managed_uid
                                   THEN 'merchant_managed_listing'
                                   ELSE 'fleet_managed_listing'
                               END AS seed_source,
                               seed.source_action_id,
                               seed.source_entity_id,
                               seed.rank,
                               seed.tier,
                               seed.price_zone,
                               seed.log_position,
                               seed.delta,
                               seed.target_final_multiplier,
                               seed.panel_multiplier
                        FROM confirmed_price_actions seed
                        WHERE seed.source_entity_id = target_listing.managed_uid
                           OR seed.price_zone = 'low'
                        ORDER BY (
                                     seed.source_entity_id = target_listing.managed_uid
                                 ) DESC,
                                 CASE WHEN seed.source_entity_id = target_listing.managed_uid
                                      THEN seed.completed_at END DESC NULLS LAST,
                                 CASE WHEN seed.source_entity_id = target_listing.managed_uid
                                      THEN seed.source_action_id END DESC NULLS LAST,
                                 (
                                     seed.source_entity_id IS DISTINCT FROM
                                         target_listing.managed_uid
                                     AND seed.price_zone = 'low'
                                     AND seed.source_action_id::text =
                                         sticky_follower.source_price_action_id
                                     AND ROW(
                                         seed.input_price_micros::text,
                                         seed.output_price_micros::text,
                                         seed.cache_read_price_micros::text,
                                         seed.cache_write_price_micros::text
                                     ) = ROW(
                                         sticky_follower.input_price_micros,
                                         sticky_follower.output_price_micros,
                                         sticky_follower.cache_read_price_micros,
                                         sticky_follower.cache_write_price_micros
                                     )
                                 ) DESC,
                                 md5(concat(
                                     channel.a6_channel_id, ':', seed.source_entity_id
                                 )),
                                 seed.source_action_id
                        LIMIT 1
                    ) price_seed ON true
                    LEFT JOIN LATERAL (
                        SELECT true AS source_covered,
                               follower_action.completed_at >=
                                   target_listing.observed_at AS fresh_write
                        FROM confirmed_follower_actions follower_action
                        WHERE follower_action.entity_id =
                              target_listing.managed_uid
                          AND follower_action.source_price_action_id =
                              price_seed.source_action_id::text
                          AND follower_action.expected_listing_status =
                              target_listing.listing_status::text
                          AND ROW(
                              follower_action.input_price_micros,
                              follower_action.output_price_micros,
                              follower_action.cache_read_price_micros,
                              follower_action.cache_write_price_micros
                          ) = ROW(
                              price_seed.input_price_micros::text,
                              price_seed.output_price_micros::text,
                              price_seed.cache_read_price_micros::text,
                              price_seed.cache_write_price_micros::text
                          )
                        ORDER BY follower_action.completed_at DESC,
                                 follower_action.id DESC
                        LIMIT 1
                    ) follower_coverage ON true
                    WHERE merchant.status = 'supplier_submitted'
                      AND merchant.roles @> ARRAY['seller']::text[]
                      AND merchant.a6_user_id IS NOT NULL
                      AND merchant.supplier_id IS NOT NULL
                      AND merchant.session_ref <> ''
                      AND merchant.poll_error_count = 0
                      AND merchant.last_poll_at IS NOT NULL
                      AND channel.status = 1
                      AND channel.availability = 1
                      AND channel.channel_type = 1
                      AND channel.name <> %s
                      AND NOT (channel.name ~ %s AND channel.name ~ %s)
                      AND (
                          channel.supplier_id IS NULL
                          OR channel.supplier_id = merchant.supplier_id
                      )
                      AND channel.observed_at = merchant.last_poll_at
                      AND channel.observed_at >= now() - (%s * interval '1 second')
                      AND NOT EXISTS (
                          SELECT 1
                          FROM a6_trade.a6_seller_retirement_plans retirement
                          WHERE retirement.merchant_id = merchant.id
                            AND retirement.completed_at IS NOT NULL
                      )
                      AND (
                          target_listing.managed_uid IS NULL
                          OR (
                              price_seed.source_action_id IS NOT NULL
                              AND (
                                  (
                                      ROW(
                                          target_listing.input_price_micros,
                                          target_listing.output_price_micros,
                                          target_listing.cache_read_price_micros,
                                          target_listing.cache_write_price_micros
                                      ) IS DISTINCT FROM ROW(
                                          price_seed.input_price_micros,
                                          price_seed.output_price_micros,
                                          price_seed.cache_read_price_micros,
                                          price_seed.cache_write_price_micros
                                      )
                                      AND follower_coverage.fresh_write IS NOT TRUE
                                  )
                                  OR (
                                      ROW(
                                          target_listing.input_price_micros,
                                          target_listing.output_price_micros,
                                          target_listing.cache_read_price_micros,
                                          target_listing.cache_write_price_micros
                                      ) IS NOT DISTINCT FROM ROW(
                                          price_seed.input_price_micros,
                                          price_seed.output_price_micros,
                                          price_seed.cache_read_price_micros,
                                          price_seed.cache_write_price_micros
                                      )
                                      AND target_listing.managed_uid IS DISTINCT FROM
                                          price_seed.source_entity_id
                                      AND follower_coverage.source_covered IS NOT TRUE
                                  )
                              )
                          )
                      )
                    ORDER BY (merchant.operating_group = %s) DESC,
                             merchant.id,
                             channel.a6_channel_id
                    LIMIT %s
                    """,
                    (
                        request.model,
                        request.model,
                        request.inventory_max_age_seconds,
                        request.model,
                        request.model,
                        FORBIDDEN_CHANNEL_NAME,
                        GENERATED_CHANNEL_NAME_PATTERN,
                        GENERATED_CHANNEL_NAME_SIGNAL_PATTERN,
                        request.inventory_max_age_seconds,
                        FOCUS_OPERATING_GROUP,
                        request.target_limit,
                    ),
                )
                rows = _rows(cursor)
        except ManagedAutoOnboardError:
            raise
        except Exception as exc:
            raise ManagedAutoOnboardError("managed_autoonboard_discovery_failed") from exc
        finally:
            _close(connection)
        targets = tuple(_target(row) for row in rows)
        identities = {(item.merchant_id, item.a6_channel_id) for item in targets}
        if len(identities) != len(targets):
            raise ManagedAutoOnboardError("managed_autoonboard_targets_duplicate")
        return targets


class ManagedAutoOnboardPriceRecorder(Protocol):
    def confirm(
        self,
        target: ManagedAutoOnboardTarget,
        *,
        listing_managed_uid: str,
        model: str,
    ) -> bool: ...


class PostgresManagedAutoOnboardPriceRecorder:
    """Persist inherited price-band evidence only after current inventory confirms it."""

    def __init__(
        self,
        connection_factory: Callable[[], Any],
        *,
        config_version: str,
        requested_by: str = "trade-auto-onboard-seller-channels",
    ) -> None:
        if not callable(connection_factory):
            raise ManagedAutoOnboardError("managed_autoonboard_connection_factory_invalid")
        self.connection_factory = connection_factory
        self.config_version = _audit_label(
            config_version,
            "managed_autoonboard_config_version_invalid",
        )
        self.requested_by = _audit_label(
            requested_by,
            "managed_autoonboard_requested_by_invalid",
        )

    def confirm(
        self,
        target: ManagedAutoOnboardTarget,
        *,
        listing_managed_uid: str,
        model: str,
    ) -> bool:
        plan = target.listing_seed_plan
        prices = target.listing_seed_prices
        if plan is None or prices is None:
            return False
        if model != SUPPORTED_MODEL or not str(listing_managed_uid).strip():
            raise ManagedAutoOnboardError("managed_autoonboard_price_confirmation_invalid")
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT binding.merchant_id,
                           binding.a6_listing_id,
                           binding.control_state,
                           current_listing.status AS listing_status,
                           current_listing.input_price_micros,
                           current_listing.output_price_micros,
                           current_listing.cache_read_price_micros,
                           current_listing.cache_write_price_micros
                    FROM a6_trade.a6_managed_listing_bindings binding
                    JOIN a6_trade.a6_supplier_listings_current current_listing
                      ON current_listing.merchant_id = binding.merchant_id
                     AND current_listing.a6_listing_id = binding.a6_listing_id
                    WHERE binding.managed_uid = %s
                    FOR UPDATE OF binding
                    """,
                    (listing_managed_uid,),
                )
                row = _one_row(cursor)
                if row is None:
                    raise ManagedAutoOnboardError(
                        "managed_autoonboard_price_confirmation_missing"
                    )
                observed_prices = _prices(row)
                if (
                    int(row.get("merchant_id") or 0) != target.merchant_id
                    or int(row.get("a6_listing_id") or 0) < 1
                    or row.get("control_state") != "verified"
                    or observed_prices != prices
                ):
                    raise ManagedAutoOnboardError(
                        "managed_autoonboard_price_confirmation_changed"
                    )
                observed_status = int(row.get("listing_status"))
                if (
                    target.listing_status is not None
                    and observed_status != target.listing_status
                ):
                    raise ManagedAutoOnboardError(
                        "managed_autoonboard_listing_activation_changed"
                    )
                desired = _price_confirmation_desired(
                    target,
                    listing_managed_uid=listing_managed_uid,
                    model=model,
                    expected_listing_status=observed_status,
                )
                digest = hashlib.sha256(
                    json.dumps(desired, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest()
                idempotency_key = f"managed-listing-price-seed:{digest[:48]}"
                observation = {
                    "merchant_id": target.merchant_id,
                    "a6_listing_id": int(row["a6_listing_id"]),
                    "listing_status": observed_status,
                    "prices_micros": {
                        key: value
                        for key, value in zip(
                            ("input", "output", "cache_read", "cache_write"),
                            prices,
                            strict=True,
                        )
                    },
                }
                cursor.execute(
                    """
                    INSERT INTO a6_trade.trade_control_actions (
                        idempotency_key, action_type, entity_type, entity_id,
                        desired_state, observed_before, observed_after, status,
                        config_version, requested_by, started_at, completed_at
                    ) VALUES (
                        %s, 'apply_seller_price_band', 'a6_managed_listing', %s,
                        %s::jsonb, %s::jsonb, %s::jsonb, 'completed',
                        %s, %s, now(), now()
                    )
                    ON CONFLICT (idempotency_key) DO NOTHING
                    RETURNING id
                    """,
                    (
                        idempotency_key,
                        listing_managed_uid,
                        _json(desired),
                        _json(observation),
                        _json(observation),
                        self.config_version,
                        self.requested_by,
                    ),
                )
                action = _one_row(cursor)
                if action is None:
                    cursor.execute(
                        """
                        SELECT desired_state, observed_after, status
                        FROM a6_trade.trade_control_actions
                        WHERE idempotency_key = %s
                        """,
                        (idempotency_key,),
                    )
                    existing = _one_row(cursor)
                    if (
                        existing is None
                        or existing.get("status") != "completed"
                        or dict(existing.get("desired_state") or {}) != desired
                        or dict(existing.get("observed_after") or {}) != observation
                    ):
                        raise ManagedAutoOnboardError(
                            "managed_autoonboard_price_confirmation_conflict"
                        )
                    connection.rollback()
                    return False
                action_id = int(action.get("id") or 0)
                if action_id < 1:
                    raise ManagedAutoOnboardError(
                        "managed_autoonboard_price_confirmation_invalid"
                    )
                cursor.execute(
                    """
                    INSERT INTO a6_trade.trade_control_action_attempts (
                        action_id, attempt_no, status, observed_before,
                        observed_after, started_at, completed_at
                    ) VALUES (%s, 1, 'completed', %s::jsonb, %s::jsonb, now(), now())
                    """,
                    (action_id, _json(observation), _json(observation)),
                )
            connection.commit()
            return True
        except ManagedAutoOnboardError:
            connection.rollback()
            raise
        except Exception as exc:
            connection.rollback()
            raise ManagedAutoOnboardError(
                "managed_autoonboard_price_confirmation_failed"
            ) from exc
        finally:
            _close(connection)


SessionRefresher = Callable[[SessionStore, SessionRecord, Any], None]
ClientFactory = Callable[[ManagedAutoOnboardTarget, SessionRecord], Any]
ControllerFactory = Callable[[ManagedListingStore, Any], ManagedListingController]


class ManagedChannelAutoOnboarder:
    def __init__(
        self,
        discovery: ManagedAutoOnboardDiscovery,
        listing_store: ManagedListingStore,
        client_factory: ClientFactory,
        *,
        sessions_dir: Path,
        session_refresher: SessionRefresher | None = None,
        controller_factory: ControllerFactory | None = None,
        price_recorder: ManagedAutoOnboardPriceRecorder | None = None,
    ) -> None:
        self.discovery = discovery
        self.listing_store = listing_store
        self.client_factory = client_factory
        self.sessions_dir = sessions_dir.expanduser().resolve()
        self.session_refresher = session_refresher or _refresh_session
        self.price_recorder = price_recorder
        self.controller_factory = controller_factory or (
            lambda store, client: ManagedListingController(store, lambda _merchant: client)
        )

    def execute(
        self,
        request: ManagedAutoOnboardRequest,
        *,
        apply: bool = False,
    ) -> ManagedAutoOnboardBatchResult:
        if not isinstance(apply, bool):
            raise ManagedAutoOnboardError("managed_autoonboard_apply_invalid")
        targets = self.discovery.list_targets(
            model=request.model,
            inventory_max_age_seconds=request.inventory_max_age_seconds,
            limit=request.target_limit,
        )
        results = tuple(self._reconcile_one(target, request, apply=apply) for target in targets)
        return ManagedAutoOnboardBatchResult(apply, request.model, results)

    def _reconcile_one(
        self,
        target: ManagedAutoOnboardTarget,
        request: ManagedAutoOnboardRequest,
        *,
        apply: bool,
    ) -> ManagedAutoOnboardItemResult:
        channel_uid = target.channel_managed_uid or stable_managed_uid(
            "channel",
            target.merchant_id,
            auto_channel_logical_key(target.a6_channel_id),
        )
        channel_result: dict[str, Any] = _blocked_decision(
            target,
            "channel",
            channel_uid,
            "managed_autoonboard_not_started",
        )
        listing_result: dict[str, Any] = _blocked_decision(
            target,
            "listing",
            "",
            "managed_autoonboard_channel_pending",
        )
        seed_source = "default_seed"
        try:
            session_store = self._session_store(target)
            record = session_store.load()
            _validate_session(target, record)
            client = self.client_factory(target, record)
            self.session_refresher(session_store, record, client)
            _validate_session(target, record)
            merchant = ManagedMerchant(
                merchant_id=target.merchant_id,
                a6_user_id=target.a6_user_id,
                supplier_id=target.supplier_id,
                roles=target.roles,
            )
            controller = self.controller_factory(self.listing_store, client)
            selection = controller.select_existing_channel(
                merchant,
                model=request.model,
                preferred_channel_id=target.a6_channel_id,
            )
            listing_spec, seed_source = _listing_spec(
                target,
                record,
                selection,
                request,
                prefer_seed_prices=target.listing_managed_uid is not None,
            )
            if (
                target.channel_managed_uid is None
                or target.channel_control_state
                not in _ACTIONABLE_CHANNEL_BINDING_STATES
            ):
                decision = controller.ensure_existing_channel(
                    merchant,
                    auto_channel_logical_key(target.a6_channel_id),
                    selection,
                    model=request.model,
                    managed_uid=target.channel_managed_uid,
                    apply=apply,
                )
                channel_result = decision.public()
                channel_uid = decision.managed_uid
            else:
                current_models = _channel_models(selection.channel)
                if (
                    target.channel_control_state == "verified"
                    and request.model in current_models
                ):
                    channel_result = {
                        "merchant_id": target.merchant_id,
                        "resource_kind": "channel",
                        "managed_uid": channel_uid,
                        "action": "already_managed",
                        "state": "verified",
                        "applied": False,
                        "external_id": target.a6_channel_id,
                        "error_class": None,
                    }
                else:
                    desired_models = tuple(dict.fromkeys((*current_models, request.model)))
                    decision = controller.update_channel(
                        merchant,
                        channel_uid,
                        {"models": ",".join(desired_models)},
                        apply=apply,
                    )
                    channel_result = decision.public()
            if apply and channel_result.get("state") != "verified":
                error = str(
                    channel_result.get("error_class")
                    or "managed_autoonboard_channel_not_verified"
                )
                listing_result = _blocked_decision(target, "listing", "", error)
                return _item_result(
                    target,
                    channel_result,
                    listing_result,
                    seed_source,
                    error,
                )
            if target.listing_managed_uid is not None:
                selected_listing_id = _positive_int(
                    (selection.listing or {}).get("id"),
                    "managed_autoonboard_listing_selection_invalid",
                )
                if selected_listing_id != target.a6_listing_id:
                    raise ManagedAutoOnboardError(
                        "managed_autoonboard_listing_selection_changed"
                    )
                listing = controller.update_listing(
                    merchant,
                    target.listing_managed_uid,
                    listing_spec,
                    expected_status=target.listing_status,
                    apply=apply,
                )
            else:
                try:
                    listing = controller.ensure_listing_on_existing_channel(
                        merchant,
                        channel_uid,
                        selection,
                        listing_spec,
                        apply=apply,
                    )
                except ManagedListingError as exc:
                    if selection.listing is not None or exc.code != "managed_listing_not_verified":
                        raise
                    listing = controller.ensure_listing(
                        merchant,
                        channel_uid,
                        listing_spec,
                        apply=apply,
                    )
            listing_result = listing.public()
            error = _decision_error(listing_result, apply=apply)
            if (
                apply
                and error is None
                and self.price_recorder is not None
                and target.listing_seed_plan is not None
            ):
                self.price_recorder.confirm(
                    target,
                    listing_managed_uid=str(listing_result.get("managed_uid") or ""),
                    model=request.model,
                )
            return _item_result(
                target,
                channel_result,
                listing_result,
                seed_source,
                error,
            )
        except Exception as exc:
            error = _error_code(exc)
            if channel_result.get("error_class") == "managed_autoonboard_not_started":
                channel_result = _blocked_decision(target, "channel", channel_uid, error)
            if not listing_result.get("managed_uid"):
                listing_result = _blocked_decision(target, "listing", "", error)
            return _item_result(
                target,
                channel_result,
                listing_result,
                seed_source,
                error,
            )

    def _session_store(self, target: ManagedAutoOnboardTarget) -> SessionStore:
        path = Path(target.session_ref).expanduser().resolve()
        try:
            path.relative_to(self.sessions_dir)
        except ValueError as exc:
            raise ManagedAutoOnboardError("managed_autoonboard_session_outside_root") from exc
        if not path.is_file():
            raise ManagedAutoOnboardError("managed_autoonboard_session_missing")
        return SessionStore(path)


def auto_channel_logical_key(a6_channel_id: int) -> str:
    if isinstance(a6_channel_id, bool) or not isinstance(a6_channel_id, int) or a6_channel_id < 1:
        raise ManagedAutoOnboardError("managed_autoonboard_channel_id_invalid")
    return f"auto-existing-channel:{a6_channel_id}"


def _listing_spec(
    target: ManagedAutoOnboardTarget,
    record: SessionRecord,
    selection: ExistingChannelSelection,
    request: ManagedAutoOnboardRequest,
    *,
    prefer_seed_prices: bool = False,
) -> tuple[ManagedListingSpec, str]:
    template = choose_account_template(record.template_id, a6_user_id=target.a6_user_id)
    description = template.model_description
    prices = request.seed_prices
    source = "default_seed"
    if target.listing_seed_prices is not None:
        prices = target.listing_seed_prices
        source = str(target.listing_seed_source)
    elif request.require_confirmed_price_seed:
        raise ManagedAutoOnboardError("managed_autoonboard_confirmed_price_seed_missing")
    existing = selection.listing
    if existing is not None:
        existing_description = str(existing.get("description") or "").strip()
        if is_source_only_listing_description(existing_description):
            description = existing_description
        candidate = _prices(existing)
        if candidate is not None and not (
            prefer_seed_prices and target.listing_seed_prices is not None
        ):
            prices = candidate
            source = "existing_listing"
    return (
        ManagedListingSpec(
            logical_key=request.model,
            input_price_micros=prices[0],
            output_price_micros=prices[1],
            cache_read_price_micros=prices[2],
            cache_write_price_micros=prices[3],
            display_name=request.model,
            description=description,
            own_model=request.model,
            platform_model=request.model,
        ),
        source,
    )


def _prices(value: Mapping[str, Any]) -> tuple[int, int, int, int] | None:
    result: list[int] = []
    for key in (
        "input_price_micros",
        "output_price_micros",
        "cache_read_price_micros",
        "cache_write_price_micros",
    ):
        raw = value.get(key)
        if isinstance(raw, bool):
            return None
        try:
            parsed = int(raw)
        except (TypeError, ValueError):
            return None
        if parsed < 1:
            return None
        result.append(parsed)
    return (result[0], result[1], result[2], result[3])


def _channel_models(value: Mapping[str, Any]) -> tuple[str, ...]:
    raw = value.get("models")
    if isinstance(raw, str):
        values = raw.split(",")
    elif isinstance(raw, (list, tuple)):
        values = raw
    else:
        values = ()
    return tuple(dict.fromkeys(str(item).strip() for item in values if str(item).strip()))


def _refresh_session(store: SessionStore, record: SessionRecord, client: Any) -> None:
    Workflow(client, QQMailbox(), store).ensure_authenticated(record)


def _validate_session(target: ManagedAutoOnboardTarget, record: SessionRecord) -> None:
    if (
        record.user_id != target.a6_user_id
        or record.supplier_id != target.supplier_id
        or record.status != "supplier_submitted"
    ):
        raise ManagedAutoOnboardError("managed_autoonboard_session_identity_mismatch")


def _target(row: Mapping[str, Any]) -> ManagedAutoOnboardTarget:
    roles = row.get("roles")
    if not isinstance(roles, (list, tuple)):
        raise ManagedAutoOnboardError("managed_autoonboard_target_roles_invalid")
    try:
        return ManagedAutoOnboardTarget(
            merchant_id=int(row["merchant_id"]),
            a6_user_id=int(row["a6_user_id"]),
            supplier_id=int(row["supplier_id"]),
            roles=tuple(str(role) for role in roles),
            session_ref=str(row["session_ref"]),
            a6_channel_id=int(row["a6_channel_id"]),
            channel_managed_uid=row.get("channel_managed_uid"),
            channel_control_state=row.get("channel_control_state"),
            listing_managed_uid=row.get("listing_managed_uid"),
            a6_listing_id=(
                int(row["a6_listing_id"])
                if row.get("a6_listing_id") is not None
                else None
            ),
            listing_status=(
                int(row["listing_status"])
                if row.get("listing_status") is not None
                else None
            ),
            listing_seed_prices=_seed_prices(row),
            listing_seed_source=row.get("listing_seed_source"),
            listing_seed_plan=_seed_plan(row),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ManagedAutoOnboardError("managed_autoonboard_target_invalid") from exc


def _seed_prices(row: Mapping[str, Any]) -> tuple[int, int, int, int] | None:
    keys = (
        "seed_input_price_micros",
        "seed_output_price_micros",
        "seed_cache_read_price_micros",
        "seed_cache_write_price_micros",
    )
    values = tuple(row.get(key) for key in keys)
    if values == (None, None, None, None):
        return None
    if any(value is None for value in values):
        raise ManagedAutoOnboardError("managed_autoonboard_listing_seed_invalid")
    try:
        return (int(values[0]), int(values[1]), int(values[2]), int(values[3]))
    except (TypeError, ValueError) as exc:
        raise ManagedAutoOnboardError("managed_autoonboard_listing_seed_invalid") from exc


def _seed_plan(row: Mapping[str, Any]) -> ManagedAutoOnboardPricePlan | None:
    source_action_id = row.get("seed_source_action_id")
    if source_action_id is None:
        return None
    try:
        return ManagedAutoOnboardPricePlan(
            source_action_id=int(source_action_id),
            source_entity_id=str(row["seed_source_entity_id"]),
            rank=int(row["seed_rank"]),
            tier=str(row["seed_tier"]),
            price_zone=str(row["seed_price_zone"]),
            log_position=str(row["seed_log_position"]),
            delta=str(row["seed_delta"]),
            target_final_multiplier=str(row["seed_target_final_multiplier"]),
            panel_multiplier=str(row["seed_panel_multiplier"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ManagedAutoOnboardError(
            "managed_autoonboard_listing_seed_plan_invalid"
        ) from exc


def _rows(cursor: Any) -> list[dict[str, Any]]:
    values = cursor.fetchall()
    if not values:
        return []
    if isinstance(values[0], Mapping):
        return [dict(value) for value in values]
    names = [getattr(column, "name", column[0]) for column in cursor.description]
    return [dict(zip(names, value, strict=True)) for value in values]


def _one_row(cursor: Any) -> dict[str, Any] | None:
    value = cursor.fetchone()
    if value is None:
        return None
    if isinstance(value, Mapping):
        return dict(value)
    names = [getattr(column, "name", column[0]) for column in cursor.description]
    return dict(zip(names, value, strict=True))


def _price_confirmation_desired(
    target: ManagedAutoOnboardTarget,
    *,
    listing_managed_uid: str,
    model: str,
    expected_listing_status: int,
) -> dict[str, Any]:
    plan = target.listing_seed_plan
    prices = target.listing_seed_prices
    if plan is None or prices is None or target.listing_seed_source is None:
        raise ManagedAutoOnboardError("managed_autoonboard_price_confirmation_invalid")
    return {
        "merchant_id": target.merchant_id,
        "rank": plan.rank,
        "tier": plan.tier,
        "price_zone": plan.price_zone,
        "log_position": plan.log_position,
        "listing_managed_uid": listing_managed_uid,
        "target_final_multiplier": plan.target_final_multiplier,
        "a6_panel_multiplier": plan.panel_multiplier,
        "delta": plan.delta,
        "prices_micros": {
            key: value
            for key, value in zip(
                ("input", "output", "cache_read", "cache_write"),
                prices,
                strict=True,
            )
        },
        "model": model,
        "pricing_strategy": PRICE_SEED_FOLLOWER_STRATEGY,
        "price_seed_source": target.listing_seed_source,
        "expected_listing_status": expected_listing_status,
        "source_price_action_id": plan.source_action_id,
        "source_listing_managed_uid": plan.source_entity_id,
    }


def _positive_int(value: Any, code: str) -> int:
    if isinstance(value, bool):
        raise ManagedAutoOnboardError(code)
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise ManagedAutoOnboardError(code) from None
    if parsed < 1:
        raise ManagedAutoOnboardError(code)
    return parsed


def _audit_label(value: Any, code: str) -> str:
    normalized = str(value or "").strip()
    if not _AUDIT_LABEL.fullmatch(normalized):
        raise ManagedAutoOnboardError(code)
    return normalized


def _json(value: Mapping[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _item_result(
    target: ManagedAutoOnboardTarget,
    channel: Mapping[str, Any],
    listing: Mapping[str, Any],
    seed_source: str,
    error: str | None,
) -> ManagedAutoOnboardItemResult:
    return ManagedAutoOnboardItemResult(
        merchant_id=target.merchant_id,
        a6_user_id=target.a6_user_id,
        a6_channel_id=target.a6_channel_id,
        channel=dict(channel),
        listing=dict(listing),
        listing_seed_source=seed_source,
        error_class=error,
    )


def _blocked_decision(
    target: ManagedAutoOnboardTarget,
    resource_kind: str,
    managed_uid: str,
    error: str,
) -> dict[str, Any]:
    return {
        "merchant_id": target.merchant_id,
        "resource_kind": resource_kind,
        "managed_uid": managed_uid,
        "action": "blocked",
        "state": "error",
        "applied": False,
        "external_id": None,
        "error_class": error,
    }


def _decision_error(decision: Mapping[str, Any], *, apply: bool) -> str | None:
    if decision.get("error_class"):
        return str(decision["error_class"])
    if apply and decision.get("state") != "verified":
        return "managed_autoonboard_listing_not_verified"
    return None


def _error_code(exc: Exception) -> str:
    code = getattr(exc, "code", None)
    if isinstance(code, str) and code:
        return code
    value = str(exc).strip()
    return value or type(exc).__name__


def _close(connection: Any) -> None:
    close = getattr(connection, "close", None)
    if callable(close):
        close()
