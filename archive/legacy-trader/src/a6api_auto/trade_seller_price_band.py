from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal, InvalidOperation, localcontext
from typing import Any, Protocol

from .errors import A6AutoError
from .trade_managed_listings import (
    SUPPORTED_MODEL,
    is_source_only_listing_description,
    managed_marker,
    stable_managed_uid,
)
from .trade_seller_orchestration import (
    GPT_5_6_SOL_OFFICIAL_PRICES,
    SellerMerchantResourceResult,
    SellerPricing,
    SellerResourceBatchRequest,
    SellerResourceOrchestrator,
)

_SAFE_AUDIT_LABEL = re.compile(r"^[a-z0-9][a-z0-9:._-]{0,127}$")
_NINETEEN_TIER_NAMES = (
    *("floor",) * 4,
    *("value",) * 4,
    *("balanced",) * 4,
    *("quality",) * 3,
    *("profit_floor",) * 2,
    "profit_mid",
    "ceiling",
)
_PRICE_BAND_STRATEGY = "market_channel_density_log_histogram_v3"
_FALLBACK_PRICE_BAND_STRATEGY = "segmented_panel_log_density_v2"
_MAX_MANAGED_DELTA = Decimal("0.5")
_DELTA_QUANTUM = Decimal("0.000001")
_PROFIT_COMPARISON_EPSILON = Decimal("1e-24")
_MINIMUM_MARGIN = Decimal("0.20")
ROLLOUT_BATCH_CONFIRMATION = "CONFIRM_CORRELATED_ACCOUNT_BATCH_RISK"
_DEFAULT_MARKET_DENSITY_BUCKET_COUNT = 8
_DEFAULT_MARKET_DENSITY_COVERAGE_RATIO = Decimal("0.20")
_DEFAULT_MARKET_DENSITY_MIN_CHANNELS = 2
_PRICE_JUMP_THRESHOLD_RATIO = Decimal("2")
_PRICE_JUMP_MULTIPLIER_QUANTUM = Decimal("0.000000000001")
_PRICE_JUMP_COOLDOWN_MIN_SECONDS = 30 * 60
_PRICE_JUMP_COOLDOWN_MAX_SECONDS = 45 * 60
_PRICE_BAND_DENSITY = (
    ("low", Decimal("0.55"), Decimal("0.30")),
    ("middle", Decimal("0.30"), Decimal("0.40")),
    ("high", Decimal("0.15"), Decimal("0.30")),
)


class SellerPriceBandError(A6AutoError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class SellerPinEvidence:
    binding_id: int
    token_ref_id: int
    model: str
    channel_id: int | None
    fallback_to_smart_routing: bool
    valid_from: datetime
    valid_to: datetime | None
    confirmed_at: datetime | None
    decision_id: int | None
    decision_token_ref_id: int | None
    decision_model: str | None
    selected_channel_id: int | None
    decision_status: str | None
    quote_id: int | None
    scan_run_id: int | None
    quote_channel_id: int | None
    quote_model: str | None
    realtime_ratio_usd: Decimal | None
    quote_is_owned: bool | None
    quote_observed_at: datetime | None
    scan_status: str | None
    owned_channel: bool
    usage_demand_units: int
    provider_generation_id: int | None = None
    provider_routing_generation: int | None = None
    provider_cost_generation: int | None = None
    provider_generation_state: str | None = None
    provider_generation_activated_at: datetime | None = None
    provider_slot_no: int | None = None
    provider_slot_state: str | None = None
    provider_slot_token_ref_id: int | None = None
    provider_slot_channel_id: int | None = None
    provider_confirmed_pin_binding_id: int | None = None
    provider_pin_confirmed: bool | None = None
    provider_probe_confirmed: bool | None = None
    provider_desired_schedulable: bool | None = None
    provider_observed_schedulable: bool | None = None
    provider_route_mode: str | None = None
    provider_auto_pool_generation_id: int | None = None
    provider_auto_pool_subscription_id: int | None = None
    provider_auto_pool_confirmed: bool | None = None
    provider_auto_pool_generation_state: str | None = None
    provider_auto_pool_activated_at: datetime | None = None
    provider_auto_pool_observed_at: datetime | None = None

    def fingerprint_payload(self) -> dict[str, Any]:
        # Late-arriving Observer rows can have a different quote/scan identity while
        # describing the same pinned channel at the same economic rate. Keep those
        # record IDs and timestamps in audit_payload(), but fingerprint only fields
        # that can change ownership, routing, or the resulting price plan.
        return {
            "binding_id": self.binding_id,
            "token_ref_id": self.token_ref_id,
            "model": self.model,
            "channel_id": self.channel_id,
            "fallback_to_smart_routing": self.fallback_to_smart_routing,
            "confirmed": self.confirmed_at is not None,
            "decision_id": self.decision_id,
            "decision_token_ref_id": self.decision_token_ref_id,
            "decision_model": self.decision_model,
            "selected_channel_id": self.selected_channel_id,
            "decision_status": self.decision_status,
            "quote_channel_id": self.quote_channel_id,
            "quote_model": self.quote_model,
            "realtime_ratio_usd": _decimal_text(self.realtime_ratio_usd),
            "quote_is_owned": self.quote_is_owned,
            "scan_status": self.scan_status,
            "owned_channel": self.owned_channel,
            "provider_generation_id": self.provider_generation_id,
            "provider_routing_generation": self.provider_routing_generation,
            "provider_cost_generation": self.provider_cost_generation,
            "provider_generation_state": self.provider_generation_state,
            "provider_slot_no": self.provider_slot_no,
            "provider_slot_state": self.provider_slot_state,
            "provider_slot_token_ref_id": self.provider_slot_token_ref_id,
            "provider_slot_channel_id": self.provider_slot_channel_id,
            "provider_confirmed_pin_binding_id": self.provider_confirmed_pin_binding_id,
            "provider_pin_confirmed": self.provider_pin_confirmed,
            "provider_probe_confirmed": self.provider_probe_confirmed,
            "provider_desired_schedulable": self.provider_desired_schedulable,
            "provider_observed_schedulable": self.provider_observed_schedulable,
            "provider_route_mode": self.provider_route_mode,
            "provider_auto_pool_generation_id": self.provider_auto_pool_generation_id,
            "provider_auto_pool_subscription_id": self.provider_auto_pool_subscription_id,
            "provider_auto_pool_confirmed": self.provider_auto_pool_confirmed,
            "provider_auto_pool_generation_state": self.provider_auto_pool_generation_state,
            "provider_auto_pool_activated_at": _datetime_text(
                self.provider_auto_pool_activated_at
            ),
            "provider_auto_pool_observed_at": _datetime_text(
                self.provider_auto_pool_observed_at
            ),
        }

    def audit_payload(self) -> dict[str, Any]:
        return {
            "binding_id": self.binding_id,
            "token_ref_id": self.token_ref_id,
            "decision_id": self.decision_id,
            "quote_id": self.quote_id,
            "scan_run_id": self.scan_run_id,
            "channel_id": self.channel_id,
            "model": self.model,
            "realtime_ratio_usd": _decimal_text(self.realtime_ratio_usd),
            "quote_observed_at": _datetime_text(self.quote_observed_at),
            "usage_demand_units": self.usage_demand_units,
            "provider_routing_generation": self.provider_routing_generation,
            "provider_cost_generation": self.provider_cost_generation,
            "provider_generation_activated_at": _datetime_text(
                self.provider_generation_activated_at
            ),
            "provider_slot_no": self.provider_slot_no,
            "provider_route_mode": self.provider_route_mode,
            "provider_auto_pool_generation_id": self.provider_auto_pool_generation_id,
            "provider_auto_pool_subscription_id": self.provider_auto_pool_subscription_id,
            "provider_auto_pool_activated_at": _datetime_text(
                self.provider_auto_pool_activated_at
            ),
            "provider_auto_pool_observed_at": _datetime_text(
                self.provider_auto_pool_observed_at
            ),
        }


@dataclass(frozen=True)
class SellerPriceBandSnapshot:
    model: str
    pins: tuple[SellerPinEvidence, ...]
    evaluated_at: datetime
    excluded_providers: tuple[SellerProviderExclusion, ...] = ()
    market_quotes: tuple[SellerMarketQuoteEvidence, ...] = ()

    @property
    def fingerprint(self) -> str:
        return _hash_document(
            {
                "model": self.model,
                "pins": [
                    pin.fingerprint_payload()
                    for pin in sorted(
                        self.pins,
                        key=lambda item: (item.token_ref_id, item.binding_id),
                    )
                ],
                "excluded_providers": [
                    item.public()
                    for item in sorted(
                        self.excluded_providers,
                        key=lambda item: (item.merchant_id, item.token_ref_id),
                    )
                ],
                "market_quotes": [
                    item.fingerprint_payload()
                    for item in sorted(
                        self.market_quotes,
                        key=lambda item: item.channel_id,
                    )
                ],
            }
        )


@dataclass(frozen=True)
class SellerMarketQuoteEvidence:
    channel_id: int
    realtime_ratio_usd: Decimal
    observer_count: int
    observed_at: datetime
    is_owned: bool = False

    def fingerprint_payload(self) -> dict[str, Any]:
        # Observer rounds can finish after Seller preflight starts while carrying an
        # observed_at that is still inside the anchored snapshot window. Timestamp
        # churn alone does not alter the plan; price, quorum, and ownership still do.
        return {
            "channel_id": self.channel_id,
            "realtime_ratio_usd": _decimal_text(self.realtime_ratio_usd),
            "observer_count": self.observer_count,
            "is_owned": self.is_owned,
        }


@dataclass(frozen=True)
class SellerPriceFloorGuard:
    status: str
    activated: bool
    top1_channel_id: int | None
    top2_channel_id: int | None
    top1_market_multiplier: Decimal | None
    top2_market_multiplier: Decimal | None
    top1_gap_ratio: Decimal | None
    neighbor_count: int
    profit_floor_market_multiplier: Decimal
    guarded_market_multiplier: Decimal | None
    protected_panel_multiplier: Decimal
    protected_final_multiplier: Decimal

    def public(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "activated": self.activated,
            "top1_channel_id": self.top1_channel_id,
            "top2_channel_id": self.top2_channel_id,
            "top1_market_multiplier": _decimal_text(self.top1_market_multiplier),
            "top2_market_multiplier": _decimal_text(self.top2_market_multiplier),
            "top1_gap_ratio": _decimal_text(self.top1_gap_ratio),
            "neighbor_count": self.neighbor_count,
            "profit_floor_market_multiplier": _decimal_text(self.profit_floor_market_multiplier),
            "guarded_market_multiplier": _decimal_text(self.guarded_market_multiplier),
            "protected_panel_multiplier": _decimal_text(self.protected_panel_multiplier),
            "protected_final_multiplier": _decimal_text(self.protected_final_multiplier),
        }


@dataclass(frozen=True)
class SellerPriceDensityBucket:
    index: int
    lower_log_position: Decimal
    upper_log_position: Decimal
    lower_panel_multiplier: Decimal
    upper_panel_multiplier: Decimal
    lower_market_multiplier: Decimal
    upper_market_multiplier: Decimal
    market_channel_count: int
    listing_count: int

    def public(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "lower_log_position": _decimal_text(self.lower_log_position),
            "upper_log_position": _decimal_text(self.upper_log_position),
            "lower_panel_multiplier": _decimal_text(self.lower_panel_multiplier),
            "upper_panel_multiplier": _decimal_text(self.upper_panel_multiplier),
            "lower_market_multiplier": _decimal_text(self.lower_market_multiplier),
            "upper_market_multiplier": _decimal_text(self.upper_market_multiplier),
            "market_channel_count": self.market_channel_count,
            "listing_count": self.listing_count,
        }


@dataclass(frozen=True)
class SellerPriceDistribution:
    mode: str
    eligible_market_channel_count: int
    allocatable_market_channel_count: int
    in_band_market_channel_count: int
    below_floor_market_channel_count: int
    above_ceiling_market_channel_count: int
    coverage_slots: int
    density_slots: int
    buckets: tuple[SellerPriceDensityBucket, ...] = ()
    fallback_reason: str | None = None

    def public(self) -> dict[str, Any]:
        document: dict[str, Any] = {
            "strategy": _PRICE_BAND_STRATEGY,
            "mode": self.mode,
            "coordinate": "panel_multiplier_log",
            "eligible_market_channel_count": self.eligible_market_channel_count,
            "allocatable_market_channel_count": self.allocatable_market_channel_count,
            "in_band_market_channel_count": self.in_band_market_channel_count,
            "below_floor_market_channel_count": self.below_floor_market_channel_count,
            "above_ceiling_market_channel_count": self.above_ceiling_market_channel_count,
            "coverage_slots": self.coverage_slots,
            "density_slots": self.density_slots,
            "buckets": [item.public() for item in self.buckets],
        }
        if self.fallback_reason is not None:
            document["fallback"] = {
                "reason": self.fallback_reason,
                "strategy": _FALLBACK_PRICE_BAND_STRATEGY,
                "segments": _fallback_price_band_segments(),
            }
        return document


@dataclass(frozen=True)
class SellerProviderExclusion:
    merchant_id: int
    token_ref_id: int
    external_token_id: int
    enodot_account_id: int
    reason: str
    usage_quota_micros: int = 0

    def public(self) -> dict[str, Any]:
        return {
            "merchant_id": self.merchant_id,
            "token_ref_id": self.token_ref_id,
            "external_token_id": self.external_token_id,
            "enodot_account_id": self.enodot_account_id,
            "reason": self.reason,
            "usage_quota_micros": self.usage_quota_micros,
        }


@dataclass(frozen=True)
class SellerAppliedPriceState:
    merchant_id: int
    entity_id: str
    action_id: int
    config_version: str
    cost_multiplier: Decimal
    final_multiplier: Decimal
    price_multiplier: Decimal
    delta: Decimal
    completed_at: datetime


@dataclass(frozen=True)
class SellerPriceTransition:
    action: str
    previous_cost_multiplier: Decimal | None
    decrease_ratio: Decimal | None
    affected_merchant_ids: tuple[int, ...]
    prior_state_count: int

    def public(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "previous_cost_multiplier": _decimal_text(self.previous_cost_multiplier),
            "decrease_ratio": _decimal_text(self.decrease_ratio),
            "affected_merchant_ids": list(self.affected_merchant_ids),
            "prior_state_count": self.prior_state_count,
        }


@dataclass(frozen=True)
class SellerPriceJumpControl:
    merchant_id: int
    entity_id: str
    action: str
    state: str
    target_price_multiplier: Decimal
    target_cost_multiplier: Decimal
    target_delta: Decimal
    baseline_price_multiplier: Decimal | None = None
    threshold_ratio: Decimal = _PRICE_JUMP_THRESHOLD_RATIO
    cooldown_id: int | None = None
    started_at: datetime | None = None
    cooling_until: datetime | None = None
    release_reason: str | None = None

    def __post_init__(self) -> None:
        if (
            isinstance(self.merchant_id, bool)
            or self.merchant_id < 1
            or not self.entity_id
            or self.action not in {"apply", "takedown", "release"}
            or self.state not in {"normal", "cooling", "release_pending"}
        ):
            raise SellerPriceBandError("seller_price_jump_control_invalid")
        target = _positive_decimal(
            self.target_price_multiplier,
            "seller_price_jump_target_invalid",
        )
        target_cost = _positive_decimal(
            self.target_cost_multiplier,
            "seller_price_jump_target_cost_invalid",
        )
        target_delta = _bounded_decimal(
            self.target_delta,
            minimum=Decimal("0"),
            maximum=Decimal("0.5"),
            code="seller_price_jump_target_delta_invalid",
        )
        baseline = (
            _positive_decimal(
                self.baseline_price_multiplier,
                "seller_price_jump_baseline_invalid",
            )
            if self.baseline_price_multiplier is not None
            else None
        )
        threshold = _bounded_decimal(
            self.threshold_ratio,
            minimum=Decimal("2"),
            maximum=Decimal("10"),
            code="seller_price_jump_threshold_invalid",
        )
        if self.state == "normal":
            if self.action != "apply" or self.cooldown_id is not None:
                raise SellerPriceBandError("seller_price_jump_control_invalid")
        elif (
            baseline is None
            or self.started_at is None
            or self.cooling_until is None
            or self.started_at.tzinfo is None
            or self.cooling_until.tzinfo is None
            or self.cooling_until <= self.started_at
        ):
            raise SellerPriceBandError("seller_price_jump_control_invalid")
        if self.state == "cooling" and self.action != "takedown":
            raise SellerPriceBandError("seller_price_jump_control_invalid")
        if self.state == "release_pending" and self.action != "release":
            raise SellerPriceBandError("seller_price_jump_control_invalid")
        object.__setattr__(self, "target_price_multiplier", target)
        object.__setattr__(self, "target_cost_multiplier", target_cost)
        object.__setattr__(self, "target_delta", target_delta)
        object.__setattr__(self, "baseline_price_multiplier", baseline)
        object.__setattr__(self, "threshold_ratio", threshold)

    def public(self) -> dict[str, Any]:
        return {
            "cooldown_id": self.cooldown_id,
            "action": self.action,
            "state": self.state,
            "baseline_price_multiplier": _decimal_text(self.baseline_price_multiplier),
            "target_price_multiplier": _decimal_text(self.target_price_multiplier),
            "target_cost_multiplier": _decimal_text(self.target_cost_multiplier),
            "target_delta": _decimal_text(self.target_delta),
            "threshold_ratio": _decimal_text(self.threshold_ratio),
            "started_at": _datetime_text(self.started_at),
            "cooling_until": _datetime_text(self.cooling_until),
            "release_reason": self.release_reason,
        }


@dataclass(frozen=True)
class SellerPriceBandRequest:
    merchant_ids: tuple[int, ...]
    group_id: int
    listing_descriptions: Mapping[int, str]
    existing_channel_ids: Mapping[int, int] | None = None
    model: str = SUPPORTED_MODEL
    margin: Decimal | str | float = _MINIMUM_MARGIN
    conversion_factor: Decimal | str | float = Decimal("0.833333333333")
    headroom: Decimal | str | float = Decimal("0")
    maximum_price_multiplier: Decimal | str | float = Decimal("1")
    maximum_final_multiplier: Decimal | str | float = Decimal("1")
    maximum_band_utilization: Decimal | str | float = Decimal("1")
    decrease_hysteresis: Decimal | str | float = Decimal("0.02")
    minimum_update_interval_seconds: int = 30
    quote_max_age_seconds: int = 300
    usage_window_seconds: int = 3600
    market_floor_enabled: bool = True
    market_floor_gap_ratio: Decimal | str | float = Decimal("0.10")
    market_floor_neighbor_ratio: Decimal | str | float = Decimal("0.03")
    market_floor_min_neighbors: int = 2
    market_floor_min_observers: int = 2
    market_floor_position: Decimal | str | float = Decimal("0.75")
    market_density_bucket_count: int = _DEFAULT_MARKET_DENSITY_BUCKET_COUNT
    market_density_coverage_ratio: Decimal | str | float = _DEFAULT_MARKET_DENSITY_COVERAGE_RATIO
    market_density_min_channels: int = _DEFAULT_MARKET_DENSITY_MIN_CHANNELS
    seller_base_url: str = "https://ai.enodot.shop/v1"
    channel_logical_key: str = "seller-primary"
    config_version: str = "phase3-seller-price-band-v2"
    requested_by: str = "trade-apply-seller-price-band"
    contact_info: str = "a6-trader"

    def __post_init__(self) -> None:
        raw_ids = tuple(self.merchant_ids)
        if (
            not raw_ids
            or any(
                isinstance(item, bool) or not isinstance(item, int) or item < 1 for item in raw_ids
            )
            or len(set(raw_ids)) != len(raw_ids)
        ):
            raise SellerPriceBandError("seller_price_band_merchants_invalid")
        if (
            isinstance(self.group_id, bool)
            or not isinstance(self.group_id, int)
            or self.group_id < 1
        ):
            raise SellerPriceBandError("seller_price_band_group_id_invalid")
        if self.model != SUPPORTED_MODEL:
            raise SellerPriceBandError("seller_price_band_model_unsupported")
        raw_channel_ids = dict(self.existing_channel_ids or {})
        channel_ids: dict[int, int] = {}
        for raw_merchant_id, raw_channel_id in raw_channel_ids.items():
            if (
                isinstance(raw_merchant_id, bool)
                or not isinstance(raw_merchant_id, int)
                or raw_merchant_id not in raw_ids
                or isinstance(raw_channel_id, bool)
                or not isinstance(raw_channel_id, int)
                or raw_channel_id < 1
            ):
                raise SellerPriceBandError("seller_price_band_channel_map_invalid")
            channel_ids[raw_merchant_id] = raw_channel_id
        raw_descriptions = dict(self.listing_descriptions or {})
        if set(raw_descriptions) != set(raw_ids):
            raise SellerPriceBandError("seller_price_band_description_map_invalid")
        descriptions: dict[int, str] = {}
        for raw_merchant_id, raw_description in raw_descriptions.items():
            if isinstance(raw_merchant_id, bool) or not isinstance(raw_merchant_id, int):
                raise SellerPriceBandError("seller_price_band_description_map_invalid")
            description = str(raw_description).strip()
            if not is_source_only_listing_description(description):
                raise SellerPriceBandError("seller_price_band_description_map_invalid")
            descriptions[raw_merchant_id] = description
        margin = _bounded_decimal(
            self.margin,
            minimum=_MINIMUM_MARGIN,
            maximum=Decimal("10"),
            code="seller_price_band_margin_invalid",
        )
        conversion = _bounded_decimal(
            self.conversion_factor,
            minimum=Decimal("0.000000000001"),
            maximum=Decimal("1"),
            code="seller_price_band_conversion_invalid",
        )
        headroom = _bounded_decimal(
            self.headroom,
            minimum=Decimal("0"),
            maximum=Decimal("1"),
            code="seller_price_band_headroom_invalid",
        )
        maximum = _bounded_decimal(
            self.maximum_price_multiplier,
            minimum=Decimal("0.000000000001"),
            maximum=Decimal("100"),
            code="seller_price_band_maximum_invalid",
        )
        maximum_final = _bounded_decimal(
            self.maximum_final_multiplier,
            minimum=Decimal("0.000000000001"),
            maximum=Decimal("100"),
            code="seller_price_band_final_maximum_invalid",
        )
        maximum_band_utilization = _bounded_decimal(
            self.maximum_band_utilization,
            minimum=Decimal("0.01"),
            maximum=Decimal("1"),
            code="seller_price_band_maximum_utilization_invalid",
        )
        hysteresis = _bounded_decimal(
            self.decrease_hysteresis,
            minimum=Decimal("0.02"),
            maximum=Decimal("1"),
            code="seller_price_band_hysteresis_invalid",
        )
        for value, code in (
            (self.quote_max_age_seconds, "seller_price_band_quote_age_invalid"),
            (self.usage_window_seconds, "seller_price_band_usage_window_invalid"),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise SellerPriceBandError(code)
        if not isinstance(self.market_floor_enabled, bool):
            raise SellerPriceBandError("seller_price_band_market_floor_enabled_invalid")
        market_floor_gap = _bounded_decimal(
            self.market_floor_gap_ratio,
            minimum=Decimal("0.01"),
            maximum=Decimal("1"),
            code="seller_price_band_market_floor_gap_invalid",
        )
        market_floor_neighbor = _bounded_decimal(
            self.market_floor_neighbor_ratio,
            minimum=Decimal("0.001"),
            maximum=Decimal("0.25"),
            code="seller_price_band_market_floor_neighbor_invalid",
        )
        market_floor_position = _bounded_decimal(
            self.market_floor_position,
            minimum=Decimal("0.5"),
            maximum=Decimal("0.95"),
            code="seller_price_band_market_floor_position_invalid",
        )
        market_density_coverage = _bounded_decimal(
            self.market_density_coverage_ratio,
            minimum=Decimal("0.10"),
            maximum=Decimal("0.50"),
            code="seller_price_band_market_density_coverage_invalid",
        )
        for value, code in (
            (
                self.market_floor_min_neighbors,
                "seller_price_band_market_floor_neighbors_invalid",
            ),
            (
                self.market_floor_min_observers,
                "seller_price_band_market_floor_observers_invalid",
            ),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or not 2 <= value <= 64:
                raise SellerPriceBandError(code)
        if (
            isinstance(self.market_density_bucket_count, bool)
            or not isinstance(self.market_density_bucket_count, int)
            or not 3 <= self.market_density_bucket_count <= 32
        ):
            raise SellerPriceBandError("seller_price_band_market_density_buckets_invalid")
        if (
            isinstance(self.market_density_min_channels, bool)
            or not isinstance(self.market_density_min_channels, int)
            or not 2 <= self.market_density_min_channels <= 256
        ):
            raise SellerPriceBandError("seller_price_band_market_density_channels_invalid")
        if (
            isinstance(self.minimum_update_interval_seconds, bool)
            or not isinstance(self.minimum_update_interval_seconds, int)
            or self.minimum_update_interval_seconds < 30
        ):
            raise SellerPriceBandError("seller_price_band_update_interval_invalid")
        for value, code in (
            (self.config_version, "seller_price_band_config_version_invalid"),
            (self.requested_by, "seller_price_band_requested_by_invalid"),
        ):
            if not _SAFE_AUDIT_LABEL.fullmatch(str(value)):
                raise SellerPriceBandError(code)
        if not str(self.seller_base_url).strip():
            raise SellerPriceBandError("seller_price_band_base_url_invalid")
        if not str(self.channel_logical_key).strip():
            raise SellerPriceBandError("seller_price_band_channel_key_invalid")
        if not str(self.contact_info).strip():
            raise SellerPriceBandError("seller_price_band_contact_invalid")
        object.__setattr__(self, "merchant_ids", tuple(sorted(raw_ids)))
        object.__setattr__(self, "existing_channel_ids", dict(sorted(channel_ids.items())))
        object.__setattr__(
            self,
            "listing_descriptions",
            dict(sorted(descriptions.items())),
        )
        object.__setattr__(self, "margin", margin)
        object.__setattr__(self, "conversion_factor", conversion)
        object.__setattr__(self, "headroom", headroom)
        object.__setattr__(self, "maximum_price_multiplier", maximum)
        object.__setattr__(self, "maximum_final_multiplier", maximum_final)
        object.__setattr__(
            self,
            "maximum_band_utilization",
            maximum_band_utilization,
        )
        object.__setattr__(self, "decrease_hysteresis", hysteresis)
        object.__setattr__(self, "market_floor_gap_ratio", market_floor_gap)
        object.__setattr__(self, "market_floor_neighbor_ratio", market_floor_neighbor)
        object.__setattr__(self, "market_floor_position", market_floor_position)
        object.__setattr__(self, "market_density_coverage_ratio", market_density_coverage)


@dataclass(frozen=True)
class SellerPriceAssignment:
    merchant_id: int
    rank: int
    tier: str
    price_zone: str
    log_position: Decimal
    pricing: SellerPricing
    listing_managed_uid: str

    def plan_payload(self) -> dict[str, Any]:
        return {
            "merchant_id": self.merchant_id,
            "rank": self.rank,
            "tier": self.tier,
            "price_zone": self.price_zone,
            "log_position": _decimal_text(self.log_position),
            "listing_managed_uid": self.listing_managed_uid,
            "target_final_multiplier": _decimal_text(self.pricing.final_multiplier),
            "a6_panel_multiplier": _decimal_text(self.pricing.price_multiplier),
            "delta": _decimal_text(self.pricing.delta),
            "prices_micros": self.pricing.public()["prices_micros"],
            "pricing": self.pricing.public(),
        }


@dataclass(frozen=True)
class SellerPriceBandPlan:
    request: SellerPriceBandRequest
    snapshot: SellerPriceBandSnapshot
    cost_multiplier: Decimal
    weight_mode: str
    price_floor_guard: SellerPriceFloorGuard
    distribution: SellerPriceDistribution
    assignments: tuple[SellerPriceAssignment, ...]
    transition: SellerPriceTransition | None = None
    price_jump_controls: tuple[SellerPriceJumpControl, ...] = ()

    @property
    def fingerprint(self) -> str:
        return _hash_document(
            {
                "config_version": self.request.config_version,
                "model": self.request.model,
                "group_id": self.request.group_id,
                "existing_channel_ids": dict(self.request.existing_channel_ids or {}),
                "margin": _decimal_text(self.request.margin),
                "conversion_factor": _decimal_text(self.request.conversion_factor),
                "headroom": _decimal_text(self.request.headroom),
                "maximum_price_multiplier": _decimal_text(self.request.maximum_price_multiplier),
                "maximum_final_multiplier": _decimal_text(self.request.maximum_final_multiplier),
                "maximum_band_utilization": _decimal_text(self.request.maximum_band_utilization),
                "decrease_hysteresis": _decimal_text(self.request.decrease_hysteresis),
                "minimum_update_interval_seconds": (self.request.minimum_update_interval_seconds),
                "market_floor": {
                    "enabled": self.request.market_floor_enabled,
                    "gap_ratio": _decimal_text(self.request.market_floor_gap_ratio),
                    "neighbor_ratio": _decimal_text(self.request.market_floor_neighbor_ratio),
                    "min_neighbors": self.request.market_floor_min_neighbors,
                    "min_observers": self.request.market_floor_min_observers,
                    "position": _decimal_text(self.request.market_floor_position),
                    "result": self.price_floor_guard.public(),
                },
                "market_density": {
                    "bucket_count": self.request.market_density_bucket_count,
                    "coverage_ratio": _decimal_text(self.request.market_density_coverage_ratio),
                    "min_channels": self.request.market_density_min_channels,
                },
                "distribution": self.distribution.public(),
                "official_prices_micros": list(GPT_5_6_SOL_OFFICIAL_PRICES),
                "snapshot_fingerprint": self.snapshot.fingerprint,
                "cost_multiplier": _decimal_text(self.cost_multiplier),
                "weight_mode": self.weight_mode,
                "assignments": [item.plan_payload() for item in self.assignments],
            }
        )

    def assignment(self, merchant_id: int) -> SellerPriceAssignment:
        for item in self.assignments:
            if item.merchant_id == merchant_id:
                return item
        raise SellerPriceBandError("seller_price_band_assignment_missing")

    def price_jump_control(self, merchant_id: int) -> SellerPriceJumpControl:
        for item in self.price_jump_controls:
            if item.merchant_id == merchant_id:
                return item
        raise SellerPriceBandError("seller_price_jump_control_missing")

    def audit_idempotency_key(self, assignment: SellerPriceAssignment) -> str:
        digest = hashlib.sha256(
            (
                f"{assignment.listing_managed_uid}\n{self.fingerprint}\n"
                f"{self.request.config_version}"
            ).encode()
        ).hexdigest()
        return f"seller-price-band:{digest[:48]}"

    def audit_desired_state(self, assignment: SellerPriceAssignment) -> dict[str, Any]:
        profit_gate = _profit_gate_payload(
            self.request,
            self.cost_multiplier,
            assignment.pricing.final_multiplier,
        )
        return {
            **assignment.plan_payload(),
            "model": self.request.model,
            "group_id": self.request.group_id,
            "existing_channel_id": (self.request.existing_channel_ids or {}).get(
                assignment.merchant_id
            ),
            "cost_multiplier": _decimal_text(self.cost_multiplier),
            "margin": _decimal_text(self.request.margin),
            "conversion_factor": _decimal_text(self.request.conversion_factor),
            "headroom": _decimal_text(self.request.headroom),
            "profit_gate": profit_gate,
            "maximum_price_multiplier": _decimal_text(self.request.maximum_price_multiplier),
            "maximum_final_multiplier": _decimal_text(self.request.maximum_final_multiplier),
            "maximum_band_utilization": _decimal_text(self.request.maximum_band_utilization),
            "decrease_hysteresis": _decimal_text(self.request.decrease_hysteresis),
            "minimum_update_interval_seconds": (self.request.minimum_update_interval_seconds),
            "market_floor": {
                "enabled": self.request.market_floor_enabled,
                "gap_ratio": _decimal_text(self.request.market_floor_gap_ratio),
                "neighbor_ratio": _decimal_text(self.request.market_floor_neighbor_ratio),
                "min_neighbors": self.request.market_floor_min_neighbors,
                "min_observers": self.request.market_floor_min_observers,
                "position": _decimal_text(self.request.market_floor_position),
                "result": self.price_floor_guard.public(),
            },
            "market_density": {
                "bucket_count": self.request.market_density_bucket_count,
                "coverage_ratio": _decimal_text(self.request.market_density_coverage_ratio),
                "min_channels": self.request.market_density_min_channels,
            },
            "distribution": self.distribution.public(),
            "official_prices_micros": {
                key: value
                for key, value in zip(
                    ("input", "output", "cache_read", "cache_write"),
                    GPT_5_6_SOL_OFFICIAL_PRICES,
                    strict=True,
                )
            },
            "weight_mode": self.weight_mode,
            "pin_evidence": [pin.audit_payload() for pin in self.snapshot.pins],
            "excluded_providers": [item.public() for item in self.snapshot.excluded_providers],
            "snapshot_fingerprint": self.snapshot.fingerprint,
            "plan_fingerprint": self.fingerprint,
            "config_version": self.request.config_version,
        }

    def public(self) -> dict[str, Any]:
        finals = [item.pricing.final_multiplier for item in self.assignments]
        panels = [item.pricing.price_multiplier for item in self.assignments]
        profit_floor_final = _profit_floor_final(
            self.request,
            self.cost_multiplier,
        )
        positive_delta_points = sum(item.pricing.delta > 0 for item in self.assignments)
        return {
            "fingerprint": self.fingerprint,
            "snapshot_fingerprint": self.snapshot.fingerprint,
            "model": self.request.model,
            "merchant_count": len(self.assignments),
            "existing_channel_ids": dict(self.request.existing_channel_ids or {}),
            "cost_multiplier": _decimal_text(self.cost_multiplier),
            "weight_mode": self.weight_mode,
            "margin": _decimal_text(self.request.margin),
            "conversion_factor": _decimal_text(self.request.conversion_factor),
            "headroom": _decimal_text(self.request.headroom),
            "maximum_price_multiplier": _decimal_text(self.request.maximum_price_multiplier),
            "maximum_final_multiplier": _decimal_text(self.request.maximum_final_multiplier),
            "maximum_band_utilization": _decimal_text(self.request.maximum_band_utilization),
            "decrease_hysteresis": _decimal_text(self.request.decrease_hysteresis),
            "minimum_update_interval_seconds": (self.request.minimum_update_interval_seconds),
            "market_floor": {
                "enabled": self.request.market_floor_enabled,
                "gap_ratio": _decimal_text(self.request.market_floor_gap_ratio),
                "neighbor_ratio": _decimal_text(self.request.market_floor_neighbor_ratio),
                "min_neighbors": self.request.market_floor_min_neighbors,
                "min_observers": self.request.market_floor_min_observers,
                "position": _decimal_text(self.request.market_floor_position),
                "result": self.price_floor_guard.public(),
            },
            "formula": (
                "a=weighted_pin_cost; d=1+delta; P=a*x; final=d*P; "
                "final_floor=(1+margin)*(a/c)*(1+headroom); "
                "x=final_floor/(a*d)"
            ),
            "profit_gate": _profit_gate_payload(
                self.request,
                self.cost_multiplier,
                min(finals),
            ),
            "market_density": {
                "bucket_count": self.request.market_density_bucket_count,
                "coverage_ratio": _decimal_text(self.request.market_density_coverage_ratio),
                "min_channels": self.request.market_density_min_channels,
            },
            "distribution": self.distribution.public(),
            "coverage": {
                "minimum_final_multiplier": _decimal_text(min(finals)),
                "maximum_final_multiplier": _decimal_text(max(finals)),
                "final_range_ratio": _decimal_text(max(finals) / min(finals)),
                "minimum_panel_multiplier": _decimal_text(min(panels)),
                "maximum_panel_multiplier": _decimal_text(max(panels)),
                "delta_zero_panel_multiplier": _decimal_text(profit_floor_final),
                "positive_delta_points": positive_delta_points,
                "zero_delta_points": len(self.assignments) - positive_delta_points,
                "distinct_price_points": len({item.pricing.prices for item in self.assignments}),
                "listing_targets": len(self.assignments),
            },
            "tier_counts": dict(sorted(Counter(item.tier for item in self.assignments).items())),
            "price_zone_counts": dict(
                sorted(Counter(item.price_zone for item in self.assignments).items())
            ),
            "pins": [pin.audit_payload() for pin in self.snapshot.pins],
            "excluded_providers": [item.public() for item in self.snapshot.excluded_providers],
            "assignments": [item.plan_payload() for item in self.assignments],
            "transition": self.transition.public() if self.transition else None,
            "price_jump_hard_rule": {
                "threshold_ratio": _decimal_text(_PRICE_JUMP_THRESHOLD_RATIO),
                "cooldown_min_seconds": _PRICE_JUMP_COOLDOWN_MIN_SECONDS,
                "cooldown_max_seconds": _PRICE_JUMP_COOLDOWN_MAX_SECONDS,
                "controls": {
                    str(item.merchant_id): item.public() for item in self.price_jump_controls
                },
            },
        }


@dataclass(frozen=True)
class SellerPriceAuditRef:
    merchant_id: int
    entity_id: str
    idempotency_key: str
    action_id: int | None
    claimed: bool
    replayed: bool
    status: str
    attempt_no: int | None = None

    def public(self) -> dict[str, Any]:
        return {
            "idempotency_key": self.idempotency_key,
            "action_id": self.action_id,
            "attempt_no": self.attempt_no,
            "claimed": self.claimed,
            "replayed": self.replayed,
            "status": self.status,
        }


@dataclass(frozen=True)
class SellerPriceBandResult:
    apply: bool
    plan: SellerPriceBandPlan
    merchants: tuple[SellerMerchantResourceResult, ...]
    audits: tuple[SellerPriceAuditRef, ...]

    def summary(self) -> dict[str, Any]:
        result_by_merchant = {item.merchant_id: item for item in self.merchants}
        audit_by_merchant = {item.merchant_id: item for item in self.audits}
        merchant_documents: list[dict[str, Any]] = []
        for assignment in self.plan.assignments:
            resource = result_by_merchant[assignment.merchant_id]
            audit = audit_by_merchant[assignment.merchant_id]
            document = resource.public()
            document["price_band"] = assignment.plan_payload()
            document["price_jump_control"] = self.plan.price_jump_control(
                assignment.merchant_id
            ).public()
            document["price_band_audit"] = audit.public()
            merchant_documents.append(document)
        not_ready = sum(item["readiness"] == "not_ready" for item in merchant_documents)
        failures = sum(item["error_class"] is not None for item in merchant_documents)
        actionable_blocked = sum(
            decision.get("action") == "blocked"
            for item in merchant_documents
            if item["readiness"] != "not_ready"
            for decision in (item["channel"], item["listing"])
        )
        pending = sum(
            decision.get("action") == "reconcile_pending" or bool(decision.get("error_class"))
            for item in merchant_documents
            for decision in (item["seller"], item["channel"], item["listing"])
        )
        blockers = [
            {
                "merchant_id": item["merchant_id"],
                "code": "seller_not_ready",
                "merchant_status": item["seller"].get("merchant_status"),
            }
            for item in merchant_documents
            if item["readiness"] == "not_ready"
        ]
        return {
            "success": failures == 0
            and pending == 0
            and (not self.apply or actionable_blocked == 0),
            "mode": "applied" if self.apply else "dry-run",
            "model": self.plan.request.model,
            "merchant_count": len(merchant_documents),
            "ready_merchant_count": len(merchant_documents) - not_ready,
            "not_ready_merchant_count": not_ready,
            "blocker_count": len(blockers),
            "blockers": blockers,
            "plan": self.plan.public(),
            "merchants": merchant_documents,
        }


class SellerPriceBandStore(Protocol):
    def assert_rollout_ready(
        self,
        merchant_id: int,
        *,
        evaluated_at: datetime,
    ) -> None: ...

    def load_snapshot(
        self,
        model: str,
        *,
        evaluated_at: datetime,
        usage_window_seconds: int,
        quote_max_age_seconds: int,
    ) -> SellerPriceBandSnapshot: ...

    def load_applied_states(
        self,
        plan: SellerPriceBandPlan,
    ) -> tuple[SellerAppliedPriceState, ...]: ...

    def prepare_elapsed_price_jump_releases(
        self,
        request: SellerPriceBandRequest,
        *,
        now: datetime,
    ) -> tuple[SellerPriceJumpControl, ...]: ...

    def complete_elapsed_price_jump_release(
        self,
        request: SellerPriceBandRequest,
        control: SellerPriceJumpControl,
        pricing: SellerPricing,
        result: SellerMerchantResourceResult,
    ) -> None: ...

    def reconcile_price_jump_controls(
        self,
        plan: SellerPriceBandPlan,
        states: tuple[SellerAppliedPriceState, ...],
        *,
        now: datetime,
        apply: bool,
    ) -> tuple[SellerPriceJumpControl, ...]: ...

    def complete_price_jump_releases(
        self,
        plan: SellerPriceBandPlan,
        controls: tuple[SellerPriceJumpControl, ...],
        completed_merchant_ids: tuple[int, ...],
    ) -> None: ...

    def claim_plan(
        self,
        plan: SellerPriceBandPlan,
        ready_merchant_ids: tuple[int, ...],
    ) -> tuple[SellerPriceAuditRef, ...]: ...

    def finish_plan(
        self,
        plan: SellerPriceBandPlan,
        claims: tuple[SellerPriceAuditRef, ...],
        results: Mapping[int, SellerMerchantResourceResult],
    ) -> tuple[SellerPriceAuditRef, ...]: ...

    def fail_plan(
        self,
        plan: SellerPriceBandPlan,
        claims: tuple[SellerPriceAuditRef, ...],
        *,
        error_class: str,
    ) -> tuple[SellerPriceAuditRef, ...]: ...


class SellerPriceBandOrchestrator:
    def __init__(
        self,
        store: SellerPriceBandStore,
        resources: SellerResourceOrchestrator,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.store = store
        self.resources = resources
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def execute(
        self,
        request: SellerPriceBandRequest,
        *,
        apply: bool = False,
        rollout_batch_limit: int = 1,
        rollout_confirmation: str = "",
    ) -> SellerPriceBandResult:
        if not isinstance(apply, bool):
            raise SellerPriceBandError("seller_price_band_apply_invalid")
        if (
            isinstance(rollout_batch_limit, bool)
            or not isinstance(rollout_batch_limit, int)
            or rollout_batch_limit < 1
        ):
            raise SellerPriceBandError("seller_price_band_rollout_limit_invalid")
        if (
            apply
            and len(request.merchant_ids) > 1
            and (
                len(request.merchant_ids) > rollout_batch_limit
                or rollout_confirmation != ROLLOUT_BATCH_CONFIRMATION
            )
        ):
            raise SellerPriceBandError("seller_price_band_rollout_guard")
        evaluated_at = _aware_datetime(self.clock(), "seller_price_band_clock_invalid")
        if apply:
            self._release_elapsed_price_jumps(request, now=evaluated_at)
        if apply and len(request.merchant_ids) == 1:
            self.store.assert_rollout_ready(
                request.merchant_ids[0],
                evaluated_at=evaluated_at,
            )
        snapshot = self.store.load_snapshot(
            request.model,
            evaluated_at=evaluated_at,
            usage_window_seconds=request.usage_window_seconds,
            quote_max_age_seconds=request.quote_max_age_seconds,
        )
        plan = self._build_controlled_plan(
            request,
            snapshot,
            now=evaluated_at,
            persist_controls=False,
        )
        mutation_policies = _price_band_mutation_policies(plan)
        _raise_if_fully_held(plan, mutation_policies, apply=apply)

        def requests_for(
            current_plan: SellerPriceBandPlan,
            policies: Mapping[int, str],
        ) -> dict[int, SellerResourceBatchRequest]:
            return {
                assignment.merchant_id: SellerResourceBatchRequest(
                    merchant_ids=(assignment.merchant_id,),
                    group_id=request.group_id,
                    model=request.model,
                    pricing=assignment.pricing,
                    listing_description=request.listing_descriptions[assignment.merchant_id],
                    listing_mutation=policies[assignment.merchant_id],
                    listing_scope="all_controlled",
                    seller_base_url=request.seller_base_url,
                    channel_logical_key=request.channel_logical_key,
                    channel_strategy="adopt_existing",
                    existing_channel_id=(request.existing_channel_ids or {}).get(
                        assignment.merchant_id
                    ),
                    config_version=request.config_version,
                    requested_by=request.requested_by,
                    contact_info=request.contact_info,
                )
                for assignment in current_plan.assignments
            }

        resource_requests = requests_for(plan, mutation_policies)
        claims: tuple[SellerPriceAuditRef, ...] = ()

        def before_apply(ready_ids: tuple[int, ...]) -> Mapping[int, str]:
            nonlocal claims, mutation_policies, plan
            # Remote preflight is deliberately sequential and can span several live Pin rounds.
            # Reprice from fresh evidence at the mutation barrier instead of applying a stale plan.
            refreshed_at = _aware_datetime(self.clock(), "seller_price_band_clock_invalid")
            refreshed = self.store.load_snapshot(
                request.model,
                evaluated_at=refreshed_at,
                usage_window_seconds=request.usage_window_seconds,
                quote_max_age_seconds=request.quote_max_age_seconds,
            )
            refreshed_plan = self._build_controlled_plan(
                request,
                refreshed,
                now=refreshed_at,
                persist_controls=True,
            )
            plan = refreshed_plan
            mutation_policies = _price_band_mutation_policies(plan)
            _raise_if_fully_held(plan, mutation_policies, apply=True)
            resource_requests.clear()
            resource_requests.update(requests_for(plan, mutation_policies))
            claimable_ids = tuple(
                merchant_id
                for merchant_id in ready_ids
                if mutation_policies[merchant_id] in {"apply", "release"}
            )
            claims = self.store.claim_plan(plan, claimable_ids)
            return {merchant_id: mutation_policies[merchant_id] for merchant_id in ready_ids}

        try:
            merchants = self.resources.execute_priced(
                resource_requests,
                apply=apply,
                before_apply=before_apply,
            )
        except Exception as exc:
            if apply and claims:
                claims = self.store.fail_plan(
                    plan,
                    claims,
                    error_class=_safe_error(
                        str(getattr(exc, "code", "") or str(exc) or type(exc).__name__)
                    ),
                )
            raise
        if apply:
            results_by_merchant = {item.merchant_id: item for item in merchants}
            claims = self.store.finish_plan(
                plan,
                claims,
                results_by_merchant,
            )
            completed_releases = tuple(
                control.merchant_id
                for control in plan.price_jump_controls
                if control.action == "release"
                and control.merchant_id in results_by_merchant
                and _audit_outcome(results_by_merchant[control.merchant_id])[0] == "completed"
            )
            self.store.complete_price_jump_releases(
                plan,
                plan.price_jump_controls,
                completed_releases,
            )
        audits_by_merchant = {item.merchant_id: item for item in claims}
        audits = tuple(
            audits_by_merchant.get(assignment.merchant_id)
            or SellerPriceAuditRef(
                merchant_id=assignment.merchant_id,
                entity_id=assignment.listing_managed_uid,
                idempotency_key=plan.audit_idempotency_key(assignment),
                action_id=None,
                claimed=False,
                replayed=False,
                status=_unclaimed_price_audit_status(
                    apply=apply,
                    mutation=mutation_policies[assignment.merchant_id],
                ),
            )
            for assignment in plan.assignments
        )
        return SellerPriceBandResult(apply, plan, merchants, audits)

    def _release_elapsed_price_jumps(
        self,
        request: SellerPriceBandRequest,
        *,
        now: datetime,
    ) -> None:
        controls = self.store.prepare_elapsed_price_jump_releases(request, now=now)
        incomplete = False
        for control in controls:
            pricing = _elapsed_price_jump_release_pricing(request, control)
            resource_request = SellerResourceBatchRequest(
                merchant_ids=(control.merchant_id,),
                group_id=request.group_id,
                model=request.model,
                pricing=pricing,
                listing_description=request.listing_descriptions[control.merchant_id],
                listing_mutation="release",
                listing_scope="all_controlled",
                seller_base_url=request.seller_base_url,
                channel_logical_key=request.channel_logical_key,
                channel_strategy="adopt_existing",
                existing_channel_id=(request.existing_channel_ids or {}).get(control.merchant_id),
                config_version=request.config_version,
                requested_by=request.requested_by,
                contact_info=request.contact_info,
            )
            try:
                results = self.resources.execute_priced(
                    {control.merchant_id: resource_request},
                    apply=True,
                )
            except A6AutoError:
                incomplete = True
                continue
            result = next(
                (item for item in results if item.merchant_id == control.merchant_id),
                None,
            )
            if result is None or _audit_outcome(result)[0] != "completed":
                incomplete = True
                continue
            self.store.complete_elapsed_price_jump_release(
                request,
                control,
                pricing,
                result,
            )
        if incomplete:
            raise SellerPriceBandError("seller_price_jump_elapsed_release_incomplete")

    def _build_controlled_plan(
        self,
        request: SellerPriceBandRequest,
        snapshot: SellerPriceBandSnapshot,
        *,
        now: datetime,
        persist_controls: bool,
    ) -> SellerPriceBandPlan:
        plan = build_seller_price_band_plan(request, snapshot, now=now)
        states = self.store.load_applied_states(plan)
        transition = _assess_transition(
            request,
            plan,
            states,
            now=now,
        )
        controls = self.store.reconcile_price_jump_controls(
            plan,
            states,
            now=now,
            apply=persist_controls,
        )
        return replace(
            plan,
            transition=transition,
            price_jump_controls=controls,
        )


def _price_band_mutation_policies(plan: SellerPriceBandPlan) -> dict[int, str]:
    held = plan.transition is not None and plan.transition.action in {
        "hold_hysteresis",
        "hold_minimum_interval",
    }
    return {
        control.merchant_id: ("hold" if held and control.action == "apply" else control.action)
        for control in plan.price_jump_controls
    }


def _elapsed_price_jump_release_pricing(
    request: SellerPriceBandRequest,
    control: SellerPriceJumpControl,
) -> SellerPricing:
    return SellerPricing(
        cost_multiplier=control.target_cost_multiplier,
        delta=control.target_delta,
        margin=request.margin,
        conversion_factor=request.conversion_factor,
        headroom=request.headroom,
        maximum_price_multiplier=max(
            request.maximum_price_multiplier,
            control.target_price_multiplier,
        ),
        price_multiplier=control.target_price_multiplier,
    )


def _raise_if_fully_held(
    plan: SellerPriceBandPlan,
    policies: Mapping[int, str],
    *,
    apply: bool,
) -> None:
    if not apply or any(action != "hold" for action in policies.values()):
        return
    transition = plan.transition
    if transition is None:
        raise SellerPriceBandError("seller_price_band_transition_missing")
    if transition.action == "hold_hysteresis":
        raise SellerPriceBandError("seller_price_band_hysteresis_hold")
    if transition.action == "hold_minimum_interval":
        raise SellerPriceBandError("seller_price_band_minimum_interval_hold")
    raise SellerPriceBandError("seller_price_band_mutation_policy_invalid")


def _unclaimed_price_audit_status(*, apply: bool, mutation: str) -> str:
    if not apply:
        return "planned"
    if mutation == "takedown":
        return "skipped_price_jump_cooling"
    if mutation == "hold":
        return "skipped_transition_hold"
    return "skipped_not_ready"


def build_seller_price_band_plan(
    request: SellerPriceBandRequest,
    snapshot: SellerPriceBandSnapshot,
    *,
    now: datetime,
) -> SellerPriceBandPlan:
    now = _aware_datetime(now, "seller_price_band_clock_invalid")
    cost_multiplier, weight_mode = _validate_snapshot(request, snapshot, now=now)
    tier_names = _tier_names(len(request.merchant_ids))
    profit_floor_final = _profit_floor_final(request, cost_multiplier)
    eligible_market_quotes = _eligible_market_quotes(request, snapshot, now=now)
    price_floor_guard = _price_floor_guard(
        request,
        eligible_market_quotes,
        cost_multiplier=cost_multiplier,
    )
    protected_panel = price_floor_guard.protected_panel_multiplier
    maximum_price = request.maximum_price_multiplier * request.maximum_band_utilization
    maximum_final = request.maximum_final_multiplier * request.maximum_band_utilization
    maximum_panel = min(maximum_price, maximum_final)
    if (
        price_floor_guard.protected_final_multiplier > maximum_final
        or protected_panel > maximum_price
    ):
        raise SellerPriceBandError("seller_price_band_price_infeasible")
    placements, distribution = _market_density_geometric_targets(
        request,
        eligible_market_quotes,
        protected_panel,
        maximum_panel,
        len(request.merchant_ids),
    )
    assignments: list[SellerPriceAssignment] = []
    for rank, (merchant_id, tier, placement) in enumerate(
        zip(request.merchant_ids, tier_names, placements, strict=True),
        start=1,
    ):
        price_zone, log_position, price_multiplier = placement
        delta = _delta_for_panel_multiplier(
            price_multiplier,
            profit_floor_final=profit_floor_final,
        )
        try:
            pricing = SellerPricing(
                cost_multiplier=cost_multiplier,
                delta=delta,
                margin=request.margin,
                conversion_factor=request.conversion_factor,
                headroom=Decimal("0"),
                maximum_price_multiplier=request.maximum_price_multiplier,
                price_multiplier=price_multiplier,
            )
            gate = _profit_gate_payload(
                request,
                cost_multiplier,
                pricing.final_multiplier,
            )
            if not gate["passed"]:
                raise SellerPriceBandError("seller_price_band_profit_gate_violated")
        except Exception as exc:
            code = str(getattr(exc, "code", "") or "")
            if code == "seller_price_infeasible":
                raise SellerPriceBandError("seller_price_band_price_infeasible") from None
            raise
        channel_uid = stable_managed_uid("channel", merchant_id, request.channel_logical_key)
        listing_uid = stable_managed_uid(
            "listing",
            merchant_id,
            f"{request.model}:{managed_marker(channel_uid)}",
        )
        assignments.append(
            SellerPriceAssignment(
                merchant_id=merchant_id,
                rank=rank,
                tier=tier,
                price_zone=price_zone,
                log_position=log_position,
                pricing=pricing,
                listing_managed_uid=listing_uid,
            )
        )
    if len({item.listing_managed_uid for item in assignments}) != len(assignments):
        raise SellerPriceBandError("seller_price_band_listing_target_conflict")
    if protected_panel != maximum_panel and len(
        {item.pricing.prices for item in assignments}
    ) != len(assignments):
        raise SellerPriceBandError("seller_price_band_distinct_prices_infeasible")
    return SellerPriceBandPlan(
        request=request,
        snapshot=snapshot,
        cost_multiplier=cost_multiplier,
        weight_mode=weight_mode,
        price_floor_guard=price_floor_guard,
        distribution=distribution,
        assignments=tuple(assignments),
    )


def _eligible_market_quotes(
    request: SellerPriceBandRequest,
    snapshot: SellerPriceBandSnapshot,
    *,
    now: datetime,
) -> tuple[SellerMarketQuoteEvidence, ...]:
    eligible: list[SellerMarketQuoteEvidence] = []
    seen_channels: set[int] = set()
    for quote in snapshot.market_quotes:
        if (
            isinstance(quote.channel_id, bool)
            or not isinstance(quote.channel_id, int)
            or quote.channel_id < 1
            or quote.channel_id in seen_channels
            or isinstance(quote.observer_count, bool)
            or not isinstance(quote.observer_count, int)
            or quote.observer_count < 0
        ):
            raise SellerPriceBandError("seller_price_band_market_quote_invalid")
        seen_channels.add(quote.channel_id)
        ratio = _optional_positive_decimal(quote.realtime_ratio_usd)
        observed_at = _aware_datetime(
            quote.observed_at,
            "seller_price_band_market_quote_time_invalid",
        )
        if observed_at > now:
            raise SellerPriceBandError("seller_price_band_market_quote_future")
        if (
            ratio is None
            or quote.is_owned
            or quote.observer_count < request.market_floor_min_observers
            or (now - observed_at).total_seconds() > request.quote_max_age_seconds
        ):
            continue
        eligible.append(quote)
    eligible.sort(key=lambda item: (item.realtime_ratio_usd, item.channel_id))
    return tuple(eligible)


def _price_floor_guard(
    request: SellerPriceBandRequest,
    eligible: tuple[SellerMarketQuoteEvidence, ...],
    *,
    cost_multiplier: Decimal,
) -> SellerPriceFloorGuard:
    profit_final = _profit_floor_final(request, cost_multiplier)
    minimum_panel = profit_final / (Decimal("1") + _MAX_MANAGED_DELTA)
    profit_market = minimum_panel / request.conversion_factor

    def guarded_final(panel_multiplier: Decimal) -> Decimal:
        delta = _delta_for_panel_multiplier(
            panel_multiplier,
            profit_floor_final=profit_final,
        )
        return (Decimal("1") + delta) * panel_multiplier

    if not request.market_floor_enabled:
        return SellerPriceFloorGuard(
            status="disabled",
            activated=False,
            top1_channel_id=None,
            top2_channel_id=None,
            top1_market_multiplier=None,
            top2_market_multiplier=None,
            top1_gap_ratio=None,
            neighbor_count=0,
            profit_floor_market_multiplier=profit_market,
            guarded_market_multiplier=None,
            protected_panel_multiplier=minimum_panel,
            protected_final_multiplier=guarded_final(minimum_panel),
        )

    if len(eligible) < 2:
        return SellerPriceFloorGuard(
            status="insufficient_market_evidence",
            activated=False,
            top1_channel_id=eligible[0].channel_id if eligible else None,
            top2_channel_id=None,
            top1_market_multiplier=(eligible[0].realtime_ratio_usd if eligible else None),
            top2_market_multiplier=None,
            top1_gap_ratio=None,
            neighbor_count=len(eligible),
            profit_floor_market_multiplier=profit_market,
            guarded_market_multiplier=None,
            protected_panel_multiplier=minimum_panel,
            protected_final_multiplier=guarded_final(minimum_panel),
        )

    top1, top2 = eligible[:2]
    gap_ratio = (top2.realtime_ratio_usd - top1.realtime_ratio_usd) / top1.realtime_ratio_usd
    neighbor_limit = top1.realtime_ratio_usd * (Decimal("1") + request.market_floor_neighbor_ratio)
    neighbor_count = sum(item.realtime_ratio_usd <= neighbor_limit for item in eligible)
    isolated = (
        gap_ratio >= request.market_floor_gap_ratio
        or neighbor_count < request.market_floor_min_neighbors
    )
    if not isolated:
        return SellerPriceFloorGuard(
            status="dense_low_price_cluster",
            activated=False,
            top1_channel_id=top1.channel_id,
            top2_channel_id=top2.channel_id,
            top1_market_multiplier=top1.realtime_ratio_usd,
            top2_market_multiplier=top2.realtime_ratio_usd,
            top1_gap_ratio=gap_ratio,
            neighbor_count=neighbor_count,
            profit_floor_market_multiplier=profit_market,
            guarded_market_multiplier=None,
            protected_panel_multiplier=minimum_panel,
            protected_final_multiplier=guarded_final(minimum_panel),
        )

    guarded_market = (
        top1.realtime_ratio_usd
        + (top2.realtime_ratio_usd - top1.realtime_ratio_usd) * request.market_floor_position
    )
    guarded_panel = guarded_market * request.conversion_factor
    protected_panel = max(minimum_panel, guarded_panel)
    return SellerPriceFloorGuard(
        status="isolated_low_price_guard",
        activated=protected_panel > minimum_panel,
        top1_channel_id=top1.channel_id,
        top2_channel_id=top2.channel_id,
        top1_market_multiplier=top1.realtime_ratio_usd,
        top2_market_multiplier=top2.realtime_ratio_usd,
        top1_gap_ratio=gap_ratio,
        neighbor_count=neighbor_count,
        profit_floor_market_multiplier=profit_market,
        guarded_market_multiplier=guarded_market,
        protected_panel_multiplier=protected_panel,
        protected_final_multiplier=guarded_final(protected_panel),
    )


def _tier_names(count: int) -> tuple[str, ...]:
    if count < 1:
        raise SellerPriceBandError("seller_price_band_merchants_invalid")
    if count == 1:
        return ("floor",)
    names: list[str] = []
    for index in range(count):
        if count == 19:
            tier = _NINETEEN_TIER_NAMES[index]
        else:
            tier_index = min(18, (index * 19) // count)
            tier = _NINETEEN_TIER_NAMES[tier_index]
        names.append(tier)
    return tuple(names)


def _delta_for_panel_multiplier(
    panel_multiplier: Decimal,
    *,
    profit_floor_final: Decimal,
) -> Decimal:
    if panel_multiplier <= 0 or profit_floor_final <= 0:
        raise SellerPriceBandError("seller_price_band_price_infeasible")
    if panel_multiplier >= profit_floor_final:
        return Decimal("0")
    raw_delta = profit_floor_final / panel_multiplier - Decimal("1")
    return min(_MAX_MANAGED_DELTA, raw_delta).quantize(
        _DELTA_QUANTUM,
        rounding=ROUND_CEILING,
    )


def _profit_floor_final(
    request: SellerPriceBandRequest,
    cost_multiplier: Decimal,
) -> Decimal:
    landed_cost = cost_multiplier / request.conversion_factor
    return (Decimal("1") + request.margin) * landed_cost * (Decimal("1") + request.headroom)


def _profit_gate_payload(
    request: SellerPriceBandRequest,
    cost_multiplier: Decimal,
    final_revenue_multiplier: Decimal,
) -> dict[str, Any]:
    all_landed_cost = cost_multiplier / request.conversion_factor
    required_final_revenue = (
        (Decimal("1") + request.margin) * all_landed_cost * (Decimal("1") + request.headroom)
    )
    return {
        "formula": (
            "final=(1+delta)*P >= (1+margin)*(weighted_pin_cost/conversion_factor)*(1+headroom)"
        ),
        "final_revenue_multiplier": _decimal_text(final_revenue_multiplier),
        "all_landed_cost_multiplier": _decimal_text(all_landed_cost),
        "required_final_revenue_multiplier": _decimal_text(required_final_revenue),
        "cost_markup": _decimal_text(final_revenue_multiplier / all_landed_cost - Decimal("1")),
        "passed": (final_revenue_multiplier + _PROFIT_COMPARISON_EPSILON >= required_final_revenue),
    }


def _market_density_geometric_targets(
    request: SellerPriceBandRequest,
    eligible_quotes: tuple[SellerMarketQuoteEvidence, ...],
    minimum: Decimal,
    maximum: Decimal,
    count: int,
) -> tuple[
    tuple[tuple[str, Decimal, Decimal], ...],
    SellerPriceDistribution,
]:
    if count < 1 or minimum <= 0 or maximum < minimum:
        raise SellerPriceBandError("seller_price_band_price_infeasible")
    if count == 1 or minimum == maximum:
        return _fallback_market_density_targets(
            request,
            eligible_quotes,
            minimum,
            maximum,
            count,
            reason="collapsed_price_band",
        )
    if len(eligible_quotes) < request.market_density_min_channels:
        return _fallback_market_density_targets(
            request,
            eligible_quotes,
            minimum,
            maximum,
            count,
            reason="insufficient_market_channels",
        )

    bucket_count = min(request.market_density_bucket_count, count)
    market_counts, below_floor, in_band, above_ceiling = _market_density_counts(
        request,
        eligible_quotes,
        minimum,
        maximum,
        bucket_count,
    )
    allocatable = below_floor + in_band
    if allocatable < request.market_density_min_channels:
        return _fallback_market_density_targets(
            request,
            eligible_quotes,
            minimum,
            maximum,
            count,
            reason="insufficient_in_band_market_channels",
        )

    requested_coverage = int(
        (Decimal(count) * request.market_density_coverage_ratio).to_integral_value(
            rounding=ROUND_CEILING
        )
    )
    coverage_slots = min(bucket_count, count, max(2, requested_coverage))
    density_slots = count - coverage_slots
    listing_counts = [0] * bucket_count
    for index in _coverage_bucket_indexes(bucket_count, coverage_slots):
        listing_counts[index] += 1

    if density_slots:
        allocated = 0
        remainders: list[tuple[int, int, int]] = []
        for index, market_count in enumerate(market_counts):
            scaled = density_slots * market_count
            base = scaled // allocatable
            listing_counts[index] += base
            allocated += base
            remainders.append((scaled % allocatable, market_count, index))
        for _remainder, _market_count, index in sorted(
            remainders,
            key=lambda item: (-item[0], -item[1], item[2]),
        )[: density_slots - allocated]:
            listing_counts[index] += 1

    positions: list[tuple[str, Decimal, Decimal]] = []
    buckets: list[SellerPriceDensityBucket] = []
    with localcontext() as context:
        context.prec = 50
        logarithmic_span = (maximum / minimum).ln()
        bucket_width = Decimal("1") / Decimal(bucket_count)
        for index, (market_count, listing_count) in enumerate(
            zip(market_counts, listing_counts, strict=True)
        ):
            lower_position = Decimal(index) * bucket_width
            upper_position = Decimal(index + 1) * bucket_width
            lower_panel = (
                minimum if index == 0 else minimum * (logarithmic_span * lower_position).exp()
            )
            upper_panel = (
                maximum
                if index == bucket_count - 1
                else minimum * (logarithmic_span * upper_position).exp()
            )
            buckets.append(
                SellerPriceDensityBucket(
                    index=index,
                    lower_log_position=lower_position,
                    upper_log_position=upper_position,
                    lower_panel_multiplier=lower_panel,
                    upper_panel_multiplier=upper_panel,
                    lower_market_multiplier=lower_panel / request.conversion_factor,
                    upper_market_multiplier=upper_panel / request.conversion_factor,
                    market_channel_count=market_count,
                    listing_count=listing_count,
                )
            )
            for local_index in range(listing_count):
                if index == 0:
                    position = lower_position + bucket_width * Decimal(local_index) / Decimal(
                        listing_count
                    )
                elif index == bucket_count - 1:
                    position = lower_position + bucket_width * Decimal(local_index + 1) / Decimal(
                        listing_count
                    )
                else:
                    position = lower_position + bucket_width * Decimal(
                        2 * local_index + 1
                    ) / Decimal(2 * listing_count)
                price_multiplier = (
                    minimum
                    if position == 0
                    else maximum
                    if position == 1
                    else minimum * (logarithmic_span * position).exp()
                )
                positions.append((_price_zone_for_position(position), position, price_multiplier))

    positions.sort(key=lambda item: item[1])
    if (
        len(positions) != count
        or positions[0][1] != 0
        or positions[-1][1] != 1
        or any(left[1] >= right[1] for left, right in zip(positions, positions[1:], strict=False))
    ):
        raise SellerPriceBandError("seller_price_band_density_allocation_invalid")
    distribution = SellerPriceDistribution(
        mode="live_market_density",
        eligible_market_channel_count=len(eligible_quotes),
        allocatable_market_channel_count=allocatable,
        in_band_market_channel_count=in_band,
        below_floor_market_channel_count=below_floor,
        above_ceiling_market_channel_count=above_ceiling,
        coverage_slots=coverage_slots,
        density_slots=density_slots,
        buckets=tuple(buckets),
    )
    return tuple(positions), distribution


def _fallback_market_density_targets(
    request: SellerPriceBandRequest,
    eligible_quotes: tuple[SellerMarketQuoteEvidence, ...],
    minimum: Decimal,
    maximum: Decimal,
    count: int,
    *,
    reason: str,
) -> tuple[
    tuple[tuple[str, Decimal, Decimal], ...],
    SellerPriceDistribution,
]:
    below_floor = 0
    in_band = 0
    above_ceiling = 0
    for quote in eligible_quotes:
        panel_multiplier = quote.realtime_ratio_usd * request.conversion_factor
        if panel_multiplier < minimum:
            below_floor += 1
        elif panel_multiplier > maximum:
            above_ceiling += 1
        else:
            in_band += 1
    distribution = SellerPriceDistribution(
        mode="fallback_segmented_density",
        eligible_market_channel_count=len(eligible_quotes),
        allocatable_market_channel_count=below_floor + in_band,
        in_band_market_channel_count=in_band,
        below_floor_market_channel_count=below_floor,
        above_ceiling_market_channel_count=above_ceiling,
        coverage_slots=0,
        density_slots=0,
        fallback_reason=reason,
    )
    return _segmented_geometric_targets(minimum, maximum, count), distribution


def _market_density_counts(
    request: SellerPriceBandRequest,
    eligible_quotes: tuple[SellerMarketQuoteEvidence, ...],
    minimum: Decimal,
    maximum: Decimal,
    bucket_count: int,
) -> tuple[list[int], int, int, int]:
    market_counts = [0] * bucket_count
    below_floor = 0
    in_band = 0
    above_ceiling = 0
    with localcontext() as context:
        context.prec = 50
        logarithmic_span = (maximum / minimum).ln()
        for quote in eligible_quotes:
            panel_multiplier = quote.realtime_ratio_usd * request.conversion_factor
            if panel_multiplier < minimum:
                below_floor += 1
                bucket_index = 0
            elif panel_multiplier > maximum:
                above_ceiling += 1
                continue
            else:
                in_band += 1
                position = (panel_multiplier / minimum).ln() / logarithmic_span
                bucket_index = min(bucket_count - 1, int(position * bucket_count))
            market_counts[bucket_index] += 1
    return market_counts, below_floor, in_band, above_ceiling


def _coverage_bucket_indexes(bucket_count: int, coverage_slots: int) -> tuple[int, ...]:
    if coverage_slots < 1 or coverage_slots > bucket_count:
        raise SellerPriceBandError("seller_price_band_density_allocation_invalid")
    if coverage_slots == 1:
        return (0,)
    denominator = coverage_slots - 1
    indexes = tuple(
        (index * (bucket_count - 1) + denominator // 2) // denominator
        for index in range(coverage_slots)
    )
    if len(set(indexes)) != coverage_slots:
        raise SellerPriceBandError("seller_price_band_density_allocation_invalid")
    return indexes


def _price_zone_for_position(position: Decimal) -> str:
    if position < Decimal("0.30"):
        return "low"
    if position < Decimal("0.70"):
        return "middle"
    return "high"


def _segmented_geometric_targets(
    minimum: Decimal,
    maximum: Decimal,
    count: int,
) -> tuple[tuple[str, Decimal, Decimal], ...]:
    if count < 1 or minimum <= 0 or maximum < minimum:
        raise SellerPriceBandError("seller_price_band_price_infeasible")
    positions = _segmented_log_positions(count)
    if count == 1 or minimum == maximum:
        return tuple((zone, position, minimum) for zone, position in positions)
    with localcontext() as context:
        context.prec = 50
        logarithmic_span = (maximum / minimum).ln()
        targets = tuple(
            (
                zone,
                position,
                minimum
                if index == 0
                else maximum
                if index == count - 1
                else minimum * (logarithmic_span * position).exp(),
            )
            for index, (zone, position) in enumerate(positions)
        )
    return targets


def _segmented_log_positions(count: int) -> tuple[tuple[str, Decimal], ...]:
    if count < 1:
        raise SellerPriceBandError("seller_price_band_merchants_invalid")
    if count == 1:
        return (("low", Decimal("0")),)
    positions: list[tuple[str, Decimal]] = []
    for index in range(count):
        quantile = Decimal(index) / Decimal(count - 1)
        point_start = Decimal("0")
        span_start = Decimal("0")
        for segment_index, (zone, point_share, span_share) in enumerate(_PRICE_BAND_DENSITY):
            point_end = point_start + point_share
            is_last = segment_index == len(_PRICE_BAND_DENSITY) - 1
            if quantile <= point_end or is_last:
                local_quantile = (quantile - point_start) / point_share
                position = span_start + local_quantile * span_share
                positions.append((zone, min(Decimal("1"), max(Decimal("0"), position))))
                break
            point_start = point_end
            span_start += span_share
    return tuple(positions)


def _fallback_price_band_segments() -> list[dict[str, str]]:
    return [
        {
            "zone": zone,
            "point_share": _decimal_text(point_share),
            "log_span_share": _decimal_text(span_share),
        }
        for zone, point_share, span_share in _PRICE_BAND_DENSITY
    ]


class PostgresSellerPriceBandStore:
    def __init__(
        self,
        connection_factory: Callable[[], Any],
        *,
        stale_claim_seconds: int = 3_600,
    ) -> None:
        if not callable(connection_factory):
            raise SellerPriceBandError("seller_price_band_connection_factory_invalid")
        if (
            isinstance(stale_claim_seconds, bool)
            or not isinstance(stale_claim_seconds, int)
            or not 1 <= stale_claim_seconds <= 86_400
        ):
            raise SellerPriceBandError("seller_price_band_stale_claim_invalid")
        self.connection_factory = connection_factory
        self.stale_claim_seconds = stale_claim_seconds

    def assert_rollout_ready(
        self,
        merchant_id: int,
        *,
        evaluated_at: datetime,
    ) -> None:
        merchant_id = _positive_int(
            merchant_id,
            "seller_price_band_rollout_merchant_invalid",
        )
        evaluated_at = _aware_datetime(
            evaluated_at,
            "seller_price_band_clock_invalid",
        )
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT desired_state, completed_at
                    FROM a6_trade.trade_control_actions
                    WHERE action_type = 'apply_seller_price_band'
                      AND entity_type = 'a6_managed_listing'
                      AND status = 'completed'
                    ORDER BY completed_at DESC, id DESC
                    LIMIT 1
                    """
                )
                latest = _row(cursor)
                if latest is None:
                    return
                desired = latest.get("desired_state")
                if isinstance(desired, str):
                    try:
                        desired = json.loads(desired)
                    except json.JSONDecodeError:
                        desired = None
                if not isinstance(desired, Mapping):
                    raise SellerPriceBandError("seller_price_band_rollout_evidence_invalid")
                previous_merchant_id = _positive_int(
                    desired.get("merchant_id"),
                    "seller_price_band_rollout_evidence_invalid",
                )
                if previous_merchant_id == merchant_id:
                    return
                latest_completed_at = _aware_datetime(
                    latest.get("completed_at"),
                    "seller_price_band_rollout_evidence_invalid",
                )
                cursor.execute(
                    """
                    SELECT desired_state, completed_at
                    FROM a6_trade.trade_control_actions
                    WHERE action_type = 'verify_seller_rollout'
                      AND entity_type = 'a6_merchant'
                      AND entity_id = %s
                      AND status = 'completed'
                    ORDER BY completed_at DESC, id DESC
                    LIMIT 1
                    """,
                    (str(previous_merchant_id),),
                )
                evidence_row = _row(cursor)
            if evidence_row is None:
                raise SellerPriceBandError("seller_price_band_rollout_observation_required")
            evidence_completed_at = _aware_datetime(
                evidence_row.get("completed_at"),
                "seller_price_band_rollout_evidence_invalid",
            )
            if evidence_completed_at < latest_completed_at:
                raise SellerPriceBandError("seller_price_band_rollout_observation_required")
            evidence = evidence_row.get("desired_state")
            if isinstance(evidence, str):
                try:
                    evidence = json.loads(evidence)
                except json.JSONDecodeError:
                    evidence = None
            required = (
                "supplier_approved",
                "listing_verified",
                "listing_test_passed",
                "profit_attribution_observed",
            )
            if not isinstance(evidence, Mapping) or any(
                evidence.get(key) is not True for key in required
            ):
                raise SellerPriceBandError("seller_price_band_rollout_evidence_invalid")
            window_ended_at = _aware_datetime(
                evidence.get("observation_window_ended_at"),
                "seller_price_band_rollout_evidence_invalid",
            )
            if window_ended_at > evaluated_at:
                raise SellerPriceBandError("seller_price_band_rollout_observation_required")
        finally:
            _close(connection)

    def load_snapshot(
        self,
        model: str,
        *,
        evaluated_at: datetime,
        usage_window_seconds: int,
        quote_max_age_seconds: int,
    ) -> SellerPriceBandSnapshot:
        evaluated_at = _aware_datetime(evaluated_at, "seller_price_band_clock_invalid")
        usage_since = evaluated_at - timedelta(seconds=usage_window_seconds)
        quote_since = evaluated_at - timedelta(seconds=quote_max_age_seconds)
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    _SNAPSHOT_QUERY,
                    (
                        evaluated_at,
                        evaluated_at,
                        usage_since,
                        evaluated_at,
                        model,
                        evaluated_at,
                        evaluated_at,
                        evaluated_at,
                        evaluated_at,
                    ),
                )
                pin_rows = _rows(cursor)
                cursor.execute(
                    _AUTO_POOL_SNAPSHOT_QUERY,
                    (
                        usage_since,
                        evaluated_at,
                        model,
                        evaluated_at,
                        evaluated_at,
                        evaluated_at,
                        evaluated_at,
                        evaluated_at,
                    ),
                )
                pin_rows.extend(_rows(cursor))
                cursor.execute(
                    _PROVIDER_EXCLUSIONS_QUERY,
                    (
                        evaluated_at,
                        model,
                        evaluated_at,
                        evaluated_at,
                        model,
                        evaluated_at,
                        evaluated_at,
                        evaluated_at,
                        evaluated_at,
                    ),
                )
                exclusion_rows = _rows(cursor)
                cursor.execute(
                    _MARKET_FLOOR_QUERY,
                    (model, quote_since, evaluated_at, evaluated_at),
                )
                market_rows = _rows(cursor)
            return SellerPriceBandSnapshot(
                model=model,
                pins=tuple(_pin_from_row(row) for row in pin_rows),
                evaluated_at=evaluated_at,
                excluded_providers=tuple(
                    _provider_exclusion_from_row(row) for row in exclusion_rows
                ),
                market_quotes=tuple(_market_quote_from_row(row) for row in market_rows),
            )
        finally:
            _close(connection)

    def load_applied_states(
        self,
        plan: SellerPriceBandPlan,
    ) -> tuple[SellerAppliedPriceState, ...]:
        entity_ids = [item.listing_managed_uid for item in plan.assignments]
        assignment_by_entity = {item.listing_managed_uid: item for item in plan.assignments}
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT DISTINCT ON (entity_id)
                           id, entity_id, desired_state, config_version, completed_at
                    FROM a6_trade.trade_control_actions
                    WHERE action_type = 'apply_seller_price_band'
                      AND entity_type = 'a6_managed_listing'
                      AND entity_id = ANY(%s)
                      AND status = 'completed'
                    ORDER BY entity_id, completed_at DESC, id DESC
                    """,
                    (entity_ids,),
                )
                rows = _rows(cursor)
            return tuple(_applied_state_from_row(row, assignment_by_entity) for row in rows)
        finally:
            _close(connection)

    def prepare_elapsed_price_jump_releases(
        self,
        request: SellerPriceBandRequest,
        *,
        now: datetime,
    ) -> tuple[SellerPriceJumpControl, ...]:
        now = _aware_datetime(now, "seller_price_band_clock_invalid")
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT id
                    FROM a6_trade.seller_price_jump_cooldowns
                    WHERE model = %s
                      AND merchant_id = ANY(%s)
                      AND cooling_until <= %s
                      AND (
                          state = 'cooling'
                          OR (
                              state = 'release_pending'
                              AND release_reason IN ('cost_recovered', 'cooldown_elapsed')
                          )
                      )
                    ORDER BY cooling_until, id
                    FOR UPDATE
                    """,
                    (request.model, list(request.merchant_ids), now),
                )
                due_ids = [
                    _positive_int(row.get("id"), "seller_price_jump_row_invalid")
                    for row in _rows(cursor)
                ]
                if not due_ids:
                    connection.commit()
                    return ()
                cursor.execute(
                    """
                    UPDATE a6_trade.seller_price_jump_cooldowns
                    SET state = 'release_pending',
                        release_reason = 'cooldown_elapsed',
                        release_requested_at = COALESCE(release_requested_at, %s),
                        updated_at = now()
                    WHERE id = ANY(%s)
                      AND model = %s
                      AND cooling_until <= %s
                      AND state IN ('cooling', 'release_pending')
                    RETURNING id, model, merchant_id, entity_id,
                              baseline_price_multiplier,
                              trigger_price_multiplier,
                              latest_target_price_multiplier,
                              latest_target_cost_multiplier,
                              latest_target_delta, threshold_ratio,
                              trigger_snapshot_fingerprint,
                              latest_snapshot_fingerprint, state,
                              started_at, cooling_until, release_reason,
                              release_requested_at
                    """,
                    (now, due_ids, request.model, now),
                )
                controls = tuple(
                    _price_jump_control_from_row(row)
                    for row in sorted(_rows(cursor), key=lambda item: int(item["id"]))
                )
                if len(controls) != len(due_ids):
                    raise SellerPriceBandError("seller_price_jump_prepare_conflict")
            connection.commit()
            return controls
        except Exception:
            connection.rollback()
            raise
        finally:
            _close(connection)

    def complete_elapsed_price_jump_release(
        self,
        request: SellerPriceBandRequest,
        control: SellerPriceJumpControl,
        pricing: SellerPricing,
        result: SellerMerchantResourceResult,
    ) -> None:
        if (
            control.cooldown_id is None
            or control.action != "release"
            or control.state != "release_pending"
            or control.release_reason != "cooldown_elapsed"
            or result.merchant_id != control.merchant_id
            or _audit_outcome(result)[0] != "completed"
        ):
            raise SellerPriceBandError("seller_price_jump_completion_invalid")
        desired_state = {
            "merchant_id": control.merchant_id,
            "listing_managed_uid": control.entity_id,
            "model": request.model,
            "cost_multiplier": _decimal_text(control.target_cost_multiplier),
            "target_final_multiplier": _decimal_text(pricing.final_multiplier),
            "a6_panel_multiplier": _decimal_text(control.target_price_multiplier),
            "delta": _decimal_text(control.target_delta),
            "prices_micros": pricing.public()["prices_micros"],
            "price_jump_cooldown_id": control.cooldown_id,
            "price_jump_release_reason": control.release_reason,
            "config_version": request.config_version,
        }
        observed_after = result.public()
        idempotency_key = f"seller-price-jump-release:{control.cooldown_id}"
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                    (f"seller-price-jump:{control.entity_id}",),
                )
                cursor.execute(
                    """
                    INSERT INTO a6_trade.trade_control_actions (
                        idempotency_key, action_type, entity_type, entity_id,
                        desired_state, observed_after, status, config_version,
                        requested_by, started_at, completed_at, elapsed_ms
                    ) VALUES (
                        %s, 'apply_seller_price_band', 'a6_managed_listing', %s,
                        %s::jsonb, %s::jsonb, 'completed', %s, %s,
                        now(), now(), 0
                    )
                    RETURNING id
                    """,
                    (
                        idempotency_key,
                        control.entity_id,
                        _json(desired_state),
                        _json(observed_after),
                        request.config_version,
                        request.requested_by,
                    ),
                )
                action_id = _positive_int(
                    (_row(cursor) or {}).get("id"),
                    "seller_price_jump_completion_conflict",
                )
                cursor.execute(
                    """
                    INSERT INTO a6_trade.trade_control_action_attempts (
                        action_id, attempt_no, status, observed_after,
                        started_at, completed_at, elapsed_ms
                    ) VALUES (%s, 1, 'completed', %s::jsonb, now(), now(), 0)
                    """,
                    (action_id, _json(observed_after)),
                )
                cursor.execute(
                    """
                    UPDATE a6_trade.seller_price_jump_cooldowns
                    SET state = 'completed', completed_at = now(), updated_at = now()
                    WHERE id = %s
                      AND model = %s
                      AND merchant_id = %s
                      AND entity_id = %s
                      AND state = 'release_pending'
                      AND release_reason = 'cooldown_elapsed'
                    RETURNING id
                    """,
                    (
                        control.cooldown_id,
                        request.model,
                        control.merchant_id,
                        control.entity_id,
                    ),
                )
                if _row(cursor) is None:
                    raise SellerPriceBandError("seller_price_jump_completion_conflict")
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            _close(connection)

    def reconcile_price_jump_controls(
        self,
        plan: SellerPriceBandPlan,
        states: tuple[SellerAppliedPriceState, ...],
        *,
        now: datetime,
        apply: bool,
    ) -> tuple[SellerPriceJumpControl, ...]:
        if not isinstance(apply, bool):
            raise SellerPriceBandError("seller_price_jump_apply_invalid")
        now = _aware_datetime(now, "seller_price_band_clock_invalid")
        entity_ids = [item.listing_managed_uid for item in plan.assignments]
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                if apply:
                    for entity_id in sorted(entity_ids):
                        cursor.execute(
                            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                            (f"seller-price-jump:{entity_id}",),
                        )
                query = """
                    SELECT id, model, merchant_id, entity_id,
                           baseline_price_multiplier, trigger_price_multiplier,
                           latest_target_price_multiplier,
                           latest_target_cost_multiplier,
                           latest_target_delta, threshold_ratio,
                           trigger_snapshot_fingerprint, latest_snapshot_fingerprint,
                           state, started_at, cooling_until, release_reason,
                           release_requested_at
                    FROM a6_trade.seller_price_jump_cooldowns
                    WHERE model = %s
                      AND entity_id = ANY(%s)
                      AND state IN ('cooling', 'release_pending')
                    ORDER BY entity_id
                """
                if apply:
                    query += " FOR UPDATE"
                cursor.execute(query, (plan.request.model, entity_ids))
                active = {
                    str(row["entity_id"]): _price_jump_control_from_row(row)
                    for row in _rows(cursor)
                }
                cursor.execute(
                    """
                    SELECT binding.merchant_id,
                           min(
                               listing.input_price_micros::numeric / %s::numeric
                           ) AS baseline_price_multiplier
                    FROM a6_trade.a6_managed_listing_bindings binding
                    JOIN a6_trade.a6_supplier_listings_current listing
                      ON listing.merchant_id = binding.merchant_id
                     AND listing.a6_listing_id = binding.a6_listing_id
                     AND listing.a6_channel_id = binding.a6_channel_id
                    WHERE binding.merchant_id = ANY(%s)
                      AND binding.control_state <> 'retired'
                      AND listing.platform_model = %s
                      AND listing.status IN (0, 2)
                      AND listing.input_price_micros > 0
                    GROUP BY binding.merchant_id
                    """,
                    (
                        GPT_5_6_SOL_OFFICIAL_PRICES[0],
                        [item.merchant_id for item in plan.assignments],
                        plan.request.model,
                    ),
                )
                controlled_baselines = {
                    int(row["merchant_id"]): _positive_decimal(
                        row.get("baseline_price_multiplier"),
                        "seller_price_jump_baseline_invalid",
                    )
                    for row in _rows(cursor)
                }
                controls = _price_jump_controls(
                    plan,
                    states,
                    active,
                    controlled_baselines=controlled_baselines,
                    now=now,
                )
                if not apply:
                    connection.commit()
                    return controls

                persisted: list[SellerPriceJumpControl] = []
                for control in controls:
                    if control.state == "normal":
                        persisted.append(control)
                        continue
                    persisted_target = _price_jump_persisted_multiplier(
                        control.target_price_multiplier
                    )
                    persisted_cost = _price_jump_persisted_multiplier(
                        control.target_cost_multiplier
                    )
                    persisted_delta = control.target_delta.quantize(_DELTA_QUANTUM)
                    if control.cooldown_id is None:
                        persisted_baseline = _price_jump_persisted_multiplier(
                            control.baseline_price_multiplier
                        )
                        cursor.execute(
                            """
                            INSERT INTO a6_trade.seller_price_jump_cooldowns (
                                model, merchant_id, entity_id,
                                baseline_price_multiplier,
                                trigger_price_multiplier,
                                latest_target_price_multiplier,
                                latest_target_cost_multiplier,
                                latest_target_delta, threshold_ratio,
                                trigger_snapshot_fingerprint,
                                latest_snapshot_fingerprint, state,
                                started_at, cooling_until, release_reason,
                                release_requested_at, updated_at
                            ) VALUES (
                                %s, %s, %s, %s, %s, %s, %s, %s, %s,
                                %s, %s, 'cooling', %s, %s, NULL, NULL, now()
                            )
                            RETURNING id, model, merchant_id, entity_id,
                                      baseline_price_multiplier,
                                      trigger_price_multiplier,
                                      latest_target_price_multiplier,
                                      latest_target_cost_multiplier,
                                      latest_target_delta, threshold_ratio,
                                      trigger_snapshot_fingerprint,
                                      latest_snapshot_fingerprint, state,
                                      started_at, cooling_until, release_reason,
                                      release_requested_at
                            """,
                            (
                                plan.request.model,
                                control.merchant_id,
                                control.entity_id,
                                persisted_baseline,
                                persisted_target,
                                persisted_target,
                                persisted_cost,
                                persisted_delta,
                                control.threshold_ratio,
                                plan.snapshot.fingerprint,
                                plan.snapshot.fingerprint,
                                control.started_at,
                                control.cooling_until,
                            ),
                        )
                    else:
                        cursor.execute(
                            """
                            UPDATE a6_trade.seller_price_jump_cooldowns
                            SET latest_target_price_multiplier = %s,
                                latest_target_cost_multiplier = %s,
                                latest_target_delta = %s,
                                latest_snapshot_fingerprint = %s,
                                state = %s,
                                release_reason = %s,
                                release_requested_at = CASE
                                    WHEN %s = 'release_pending'
                                    THEN COALESCE(release_requested_at, %s)
                                    ELSE NULL
                                END,
                                updated_at = now()
                            WHERE id = %s
                              AND model = %s
                              AND merchant_id = %s
                              AND entity_id = %s
                              AND state IN ('cooling', 'release_pending')
                            RETURNING id, model, merchant_id, entity_id,
                                      baseline_price_multiplier,
                                      trigger_price_multiplier,
                                      latest_target_price_multiplier,
                                      latest_target_cost_multiplier,
                                      latest_target_delta, threshold_ratio,
                                      trigger_snapshot_fingerprint,
                                      latest_snapshot_fingerprint, state,
                                      started_at, cooling_until, release_reason,
                                      release_requested_at
                            """,
                            (
                                persisted_target,
                                persisted_cost,
                                persisted_delta,
                                plan.snapshot.fingerprint,
                                control.state,
                                control.release_reason,
                                control.state,
                                now,
                                control.cooldown_id,
                                plan.request.model,
                                control.merchant_id,
                                control.entity_id,
                            ),
                        )
                    row = _row(cursor)
                    if row is None:
                        raise SellerPriceBandError("seller_price_jump_persist_conflict")
                    observed = _price_jump_control_from_row(row)
                    if observed.merchant_id != control.merchant_id:
                        raise SellerPriceBandError("seller_price_jump_readback_merchant_mismatch")
                    if observed.entity_id != control.entity_id:
                        raise SellerPriceBandError("seller_price_jump_readback_entity_mismatch")
                    if observed.state != control.state:
                        raise SellerPriceBandError("seller_price_jump_readback_state_mismatch")
                    if observed.target_price_multiplier != persisted_target:
                        raise SellerPriceBandError("seller_price_jump_readback_target_mismatch")
                    if observed.target_cost_multiplier != persisted_cost:
                        raise SellerPriceBandError("seller_price_jump_readback_cost_mismatch")
                    if observed.target_delta != persisted_delta:
                        raise SellerPriceBandError("seller_price_jump_readback_delta_mismatch")
                    persisted.append(observed)
            connection.commit()
            return tuple(persisted)
        except Exception:
            connection.rollback()
            raise
        finally:
            _close(connection)

    def complete_price_jump_releases(
        self,
        plan: SellerPriceBandPlan,
        controls: tuple[SellerPriceJumpControl, ...],
        completed_merchant_ids: tuple[int, ...],
    ) -> None:
        completed = frozenset(completed_merchant_ids)
        if any(
            isinstance(item, bool) or not isinstance(item, int) or item < 1 for item in completed
        ):
            raise SellerPriceBandError("seller_price_jump_completion_invalid")
        releases = tuple(
            item for item in controls if item.action == "release" and item.merchant_id in completed
        )
        if not releases:
            return
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                for control in releases:
                    if control.cooldown_id is None:
                        raise SellerPriceBandError("seller_price_jump_completion_invalid")
                    cursor.execute(
                        """
                        UPDATE a6_trade.seller_price_jump_cooldowns
                        SET state = 'completed', completed_at = now(), updated_at = now()
                        WHERE id = %s
                          AND model = %s
                          AND merchant_id = %s
                          AND entity_id = %s
                          AND state = 'release_pending'
                        RETURNING id
                        """,
                        (
                            control.cooldown_id,
                            plan.request.model,
                            control.merchant_id,
                            control.entity_id,
                        ),
                    )
                    if _row(cursor) is None:
                        raise SellerPriceBandError("seller_price_jump_completion_conflict")
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            _close(connection)

    def claim_plan(
        self,
        plan: SellerPriceBandPlan,
        ready_merchant_ids: tuple[int, ...],
    ) -> tuple[SellerPriceAuditRef, ...]:
        ready = tuple(dict.fromkeys(ready_merchant_ids))
        if len(ready) != len(ready_merchant_ids):
            raise SellerPriceBandError("seller_price_band_ready_merchants_invalid")
        assignments = tuple(plan.assignment(merchant_id) for merchant_id in ready)
        if not assignments:
            return ()
        desired_by_key = {
            plan.audit_idempotency_key(item): plan.audit_desired_state(item) for item in assignments
        }
        assignment_by_key = {plan.audit_idempotency_key(item): item for item in assignments}
        keys = list(desired_by_key)
        entity_ids = [item.listing_managed_uid for item in assignments]
        attempt_observation = {
            "snapshot_fingerprint": plan.snapshot.fingerprint,
            "plan_fingerprint": plan.fingerprint,
        }
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                # Serialize even the first claim, when no action row exists to lock yet.
                for entity_id in sorted(set(entity_ids)):
                    cursor.execute(
                        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                        (f"seller-price-band:{entity_id}",),
                    )
                cursor.execute(
                    """
                    SELECT idempotency_key, id, action_type, entity_type, entity_id,
                           desired_state, status, config_version, requested_by,
                           updated_at <= now() - (%s * interval '1 second') AS stale
                    FROM a6_trade.trade_control_actions
                    WHERE idempotency_key = ANY(%s)
                    ORDER BY id
                    FOR UPDATE
                    """,
                    (self.stale_claim_seconds, keys),
                )
                existing = {row["idempotency_key"]: row for row in _rows(cursor)}
                cursor.execute(
                    """
                    SELECT id, idempotency_key, entity_id, status,
                           updated_at <= now() - (%s * interval '1 second') AS stale
                    FROM a6_trade.trade_control_actions
                    WHERE action_type = 'apply_seller_price_band'
                      AND entity_type = 'a6_managed_listing'
                      AND entity_id = ANY(%s)
                      AND status IN ('started', 'unknown')
                      AND NOT (idempotency_key = ANY(%s))
                    ORDER BY id
                    FOR UPDATE
                    """,
                    (self.stale_claim_seconds, entity_ids, keys),
                )
                pending = _rows(cursor)
                if any(not bool(row.get("stale")) for row in pending):
                    raise SellerPriceBandError("seller_price_band_action_pending")
                for row in pending:
                    self._supersede_stale_action(cursor, row, plan)
                refs: dict[int, SellerPriceAuditRef] = {}
                for key in keys:
                    assignment = assignment_by_key[key]
                    desired = desired_by_key[key]
                    row = existing.get(key)
                    if row is not None:
                        if not _existing_action_identity_matches(
                            row,
                            assignment,
                            desired,
                            plan,
                        ):
                            raise SellerPriceBandError("seller_price_band_audit_conflict")
                        action_id = _positive_int(
                            row.get("id"),
                            "seller_price_band_audit_invalid",
                        )
                        action_status = str(row.get("status") or "")
                        if action_status == "completed":
                            refs[assignment.merchant_id] = SellerPriceAuditRef(
                                assignment.merchant_id,
                                assignment.listing_managed_uid,
                                key,
                                action_id,
                                False,
                                True,
                                "completed",
                            )
                            continue
                        if action_status in {"started", "unknown"} and not bool(row.get("stale")):
                            raise SellerPriceBandError("seller_price_band_action_pending")
                        if action_status not in {
                            "started",
                            "unknown",
                            "failed",
                            "superseded",
                        }:
                            raise SellerPriceBandError("seller_price_band_audit_conflict")
                        attempt_no = self._restart_action(
                            cursor,
                            action_id=action_id,
                            action_status=action_status,
                            attempt_observation=attempt_observation,
                        )
                        refs[assignment.merchant_id] = SellerPriceAuditRef(
                            assignment.merchant_id,
                            assignment.listing_managed_uid,
                            key,
                            action_id,
                            True,
                            True,
                            "started",
                            attempt_no,
                        )
                        continue
                    cursor.execute(
                        """
                        INSERT INTO a6_trade.trade_control_actions (
                            idempotency_key, action_type, entity_type, entity_id,
                            desired_state, observed_before, status,
                            config_version, requested_by, started_at
                        ) VALUES (
                            %s, 'apply_seller_price_band', 'a6_managed_listing', %s,
                            %s::jsonb, %s::jsonb, 'started', %s, %s, now()
                        )
                        ON CONFLICT (idempotency_key) DO NOTHING
                        RETURNING id
                        """,
                        (
                            key,
                            assignment.listing_managed_uid,
                            _json(desired),
                            _json(attempt_observation),
                            plan.request.config_version,
                            plan.request.requested_by,
                        ),
                    )
                    inserted = _row(cursor)
                    if inserted is None:
                        raise SellerPriceBandError("seller_price_band_audit_claim_conflict")
                    action_id = _positive_int(inserted.get("id"), "seller_price_band_audit_invalid")
                    cursor.execute(
                        """
                        INSERT INTO a6_trade.trade_control_action_attempts (
                            action_id, attempt_no, status, observed_before, started_at
                        ) VALUES (%s, 1, 'started', %s::jsonb, now())
                        """,
                        (
                            action_id,
                            _json(attempt_observation),
                        ),
                    )
                    refs[assignment.merchant_id] = SellerPriceAuditRef(
                        assignment.merchant_id,
                        assignment.listing_managed_uid,
                        key,
                        action_id,
                        True,
                        False,
                        "started",
                        1,
                    )
            connection.commit()
            return tuple(refs[item.merchant_id] for item in assignments)
        except Exception:
            connection.rollback()
            raise
        finally:
            _close(connection)

    @staticmethod
    def _supersede_stale_action(
        cursor: Any,
        row: Mapping[str, Any],
        plan: SellerPriceBandPlan,
    ) -> None:
        action_id = _positive_int(row.get("id"), "seller_price_band_audit_invalid")
        error_class = "seller_price_band_stale_plan_superseded"
        fingerprint = hashlib.sha256(error_class.encode()).hexdigest()
        cursor.execute(
            """
            UPDATE a6_trade.trade_control_action_attempts
            SET status = 'superseded', error_class = %s,
                error_fingerprint = %s,
                completed_at = coalesce(completed_at, now()),
                elapsed_ms = coalesce(
                    elapsed_ms,
                    least(
                        2147483647::numeric,
                        greatest(
                            0::numeric,
                            round(extract(epoch FROM (now() - started_at)) * 1000)
                        )
                    )::integer
                )
            WHERE id = (
                SELECT id
                FROM a6_trade.trade_control_action_attempts
                WHERE action_id = %s AND status IN ('started', 'unknown')
                ORDER BY attempt_no DESC
                LIMIT 1
            )
            RETURNING id
            """,
            (error_class, fingerprint, action_id),
        )
        if _row(cursor) is None:
            raise SellerPriceBandError("seller_price_band_audit_recovery_conflict")
        cursor.execute(
            """
            UPDATE a6_trade.trade_control_actions
            SET status = 'superseded', error_class = %s,
                completed_at = coalesce(completed_at, now()),
                elapsed_ms = coalesce(
                    elapsed_ms,
                    least(
                        2147483647::numeric,
                        greatest(
                            0::numeric,
                            round(extract(epoch FROM (now() - started_at)) * 1000)
                        )
                    )::integer
                ),
                observed_after = coalesce(
                    observed_after,
                    %s::jsonb
                ),
                updated_at = now()
            WHERE id = %s AND status IN ('started', 'unknown')
            RETURNING id
            """,
            (
                error_class,
                _json(
                    {
                        "recovery": "superseded_after_remote_preflight",
                        "replacement_plan_fingerprint": plan.fingerprint,
                    }
                ),
                action_id,
            ),
        )
        if _row(cursor) is None:
            raise SellerPriceBandError("seller_price_band_audit_recovery_conflict")

    @staticmethod
    def _restart_action(
        cursor: Any,
        *,
        action_id: int,
        action_status: str,
        attempt_observation: Mapping[str, Any],
    ) -> int:
        if action_status in {"started", "unknown"}:
            error_class = "seller_price_band_stale_claim_recovered"
            fingerprint = hashlib.sha256(error_class.encode()).hexdigest()
            cursor.execute(
                """
                UPDATE a6_trade.trade_control_action_attempts
                SET status = 'unknown',
                    error_class = coalesce(error_class, %s),
                    error_fingerprint = coalesce(error_fingerprint, %s),
                    completed_at = coalesce(completed_at, now()),
                    elapsed_ms = coalesce(
                        elapsed_ms,
                        least(
                            2147483647::numeric,
                            greatest(
                                0::numeric,
                                round(
                                    extract(epoch FROM (now() - started_at)) * 1000
                                )
                            )
                        )::integer
                    )
                WHERE id = (
                    SELECT id
                    FROM a6_trade.trade_control_action_attempts
                    WHERE action_id = %s AND status IN ('started', 'unknown')
                    ORDER BY attempt_no DESC
                    LIMIT 1
                )
                RETURNING id
                """,
                (error_class, fingerprint, action_id),
            )
            if _row(cursor) is None:
                raise SellerPriceBandError("seller_price_band_audit_recovery_conflict")
        cursor.execute(
            """
            UPDATE a6_trade.trade_control_actions
            SET observed_after = NULL, status = 'started', error_class = NULL,
                started_at = now(), completed_at = NULL, elapsed_ms = NULL,
                updated_at = now()
            WHERE id = %s AND status = %s
            RETURNING id
            """,
            (action_id, action_status),
        )
        if _row(cursor) is None:
            raise SellerPriceBandError("seller_price_band_audit_recovery_conflict")
        cursor.execute(
            """
            SELECT coalesce(max(attempt_no), 0) + 1 AS attempt_no
            FROM a6_trade.trade_control_action_attempts
            WHERE action_id = %s
            """,
            (action_id,),
        )
        attempt_no = _positive_int(
            (_row(cursor) or {}).get("attempt_no"),
            "seller_price_band_audit_invalid",
        )
        cursor.execute(
            """
            INSERT INTO a6_trade.trade_control_action_attempts (
                action_id, attempt_no, status, observed_before, started_at
            ) VALUES (%s, %s, 'started', %s::jsonb, now())
            """,
            (action_id, attempt_no, _json(attempt_observation)),
        )
        return attempt_no

    def finish_plan(
        self,
        plan: SellerPriceBandPlan,
        claims: tuple[SellerPriceAuditRef, ...],
        results: Mapping[int, SellerMerchantResourceResult],
    ) -> tuple[SellerPriceAuditRef, ...]:
        outcomes: dict[int, tuple[str, str | None, dict[str, Any]]] = {}
        for claim in claims:
            result = results.get(claim.merchant_id)
            if result is None:
                raise SellerPriceBandError("seller_price_band_result_missing")
            outcomes[claim.merchant_id] = _audit_outcome(result)
        claimed = tuple(item for item in claims if item.claimed)
        if not claimed:
            return claims
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                for claim in claimed:
                    status, error_class, observed_after = outcomes[claim.merchant_id]
                    if claim.attempt_no is None:
                        raise SellerPriceBandError("seller_price_band_audit_finish_conflict")
                    cursor.execute(
                        """
                        UPDATE a6_trade.trade_control_actions
                        SET observed_after = %s::jsonb, status = %s,
                            error_class = %s, completed_at = now(),
                            elapsed_ms = greatest(
                                0,
                                round(extract(epoch FROM (now() - started_at)) * 1000)::integer
                            ),
                            updated_at = now()
                        WHERE id = %s AND idempotency_key = %s AND status = 'started'
                        RETURNING id
                        """,
                        (
                            _json(observed_after),
                            status,
                            error_class,
                            claim.action_id,
                            claim.idempotency_key,
                        ),
                    )
                    if _row(cursor) is None:
                        raise SellerPriceBandError("seller_price_band_audit_finish_conflict")
                    cursor.execute(
                        """
                        UPDATE a6_trade.trade_control_action_attempts
                        SET observed_after = %s::jsonb, status = %s,
                            error_class = %s, error_fingerprint = %s,
                            completed_at = now(),
                            elapsed_ms = greatest(
                                0,
                                round(extract(epoch FROM (now() - started_at)) * 1000)::integer
                            )
                        WHERE action_id = %s AND attempt_no = %s AND status = 'started'
                        RETURNING id
                        """,
                        (
                            _json(observed_after),
                            status,
                            error_class,
                            (
                                hashlib.sha256(error_class.encode()).hexdigest()
                                if error_class is not None
                                else None
                            ),
                            claim.action_id,
                            claim.attempt_no,
                        ),
                    )
                    if _row(cursor) is None:
                        raise SellerPriceBandError("seller_price_band_audit_finish_conflict")
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            _close(connection)
        return tuple(
            replace(item, status=outcomes[item.merchant_id][0]) if item.claimed else item
            for item in claims
        )

    def fail_plan(
        self,
        plan: SellerPriceBandPlan,
        claims: tuple[SellerPriceAuditRef, ...],
        *,
        error_class: str,
    ) -> tuple[SellerPriceAuditRef, ...]:
        error_class = _safe_error(error_class)
        claimed = tuple(item for item in claims if item.claimed)
        if not claimed:
            return claims
        observed_after = _json(
            {
                "outcome": "interrupted",
                "remote_state": "unconfirmed",
                "retry": "fresh_remote_preflight",
                "plan_fingerprint": plan.fingerprint,
                "error_class": error_class,
            }
        )
        fingerprint = hashlib.sha256(error_class.encode()).hexdigest()
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                for claim in claimed:
                    if claim.action_id is None or claim.attempt_no is None:
                        raise SellerPriceBandError("seller_price_band_audit_fail_conflict")
                    cursor.execute(
                        """
                        UPDATE a6_trade.trade_control_actions
                        SET observed_after = %s::jsonb, status = 'failed',
                            error_class = %s, completed_at = now(),
                            elapsed_ms = greatest(
                                0,
                                round(extract(epoch FROM (now() - started_at)) * 1000)::integer
                            ),
                            updated_at = now()
                        WHERE id = %s AND idempotency_key = %s AND status = 'started'
                        RETURNING id
                        """,
                        (
                            observed_after,
                            error_class,
                            claim.action_id,
                            claim.idempotency_key,
                        ),
                    )
                    if _row(cursor) is None:
                        raise SellerPriceBandError("seller_price_band_audit_fail_conflict")
                    cursor.execute(
                        """
                        UPDATE a6_trade.trade_control_action_attempts
                        SET observed_after = %s::jsonb, status = 'failed',
                            error_class = %s, error_fingerprint = %s,
                            completed_at = now(),
                            elapsed_ms = greatest(
                                0,
                                round(extract(epoch FROM (now() - started_at)) * 1000)::integer
                            )
                        WHERE action_id = %s AND attempt_no = %s AND status = 'started'
                        RETURNING id
                        """,
                        (
                            observed_after,
                            error_class,
                            fingerprint,
                            claim.action_id,
                            claim.attempt_no,
                        ),
                    )
                    if _row(cursor) is None:
                        raise SellerPriceBandError("seller_price_band_audit_fail_conflict")
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            _close(connection)
        return tuple(replace(item, status="failed") if item.claimed else item for item in claims)


def _validate_snapshot(
    request: SellerPriceBandRequest,
    snapshot: SellerPriceBandSnapshot,
    *,
    now: datetime,
) -> tuple[Decimal, str]:
    if snapshot.model != request.model:
        raise SellerPriceBandError("seller_price_band_snapshot_model_mismatch")
    if not snapshot.pins:
        raise SellerPriceBandError("seller_price_band_pin_missing")
    binding_ids: set[tuple[str, int]] = set()
    token_ids: set[int] = set()
    weighted_cost = Decimal("0")
    total_weight = 0
    equal_cost = Decimal("0")
    for pin in snapshot.pins:
        route_mode = str(
            pin.provider_route_mode
            or ("fixed_pin" if pin.provider_generation_id is not None else "legacy")
        ).strip().casefold()
        binding_identity = (route_mode, pin.binding_id)
        if binding_identity in binding_ids or pin.token_ref_id in token_ids:
            raise SellerPriceBandError("seller_price_band_pin_duplicate")
        binding_ids.add(binding_identity)
        token_ids.add(pin.token_ref_id)
        if pin.model != request.model:
            raise SellerPriceBandError("seller_price_band_pin_model_mismatch")
        valid_from = _aware_datetime(pin.valid_from, "seller_price_band_binding_time_invalid")
        provider_projection_present = any(
            value is not None
            for value in (
                pin.provider_generation_id,
                pin.provider_routing_generation,
                pin.provider_cost_generation,
                pin.provider_generation_state,
                pin.provider_generation_activated_at,
                pin.provider_slot_no,
                pin.provider_slot_state,
                pin.provider_slot_token_ref_id,
                pin.provider_slot_channel_id,
                pin.provider_confirmed_pin_binding_id,
                pin.provider_pin_confirmed,
                pin.provider_probe_confirmed,
                pin.provider_desired_schedulable,
                pin.provider_observed_schedulable,
                pin.provider_route_mode,
                pin.provider_auto_pool_generation_id,
                pin.provider_auto_pool_subscription_id,
                pin.provider_auto_pool_confirmed,
                pin.provider_auto_pool_generation_state,
                pin.provider_auto_pool_activated_at,
                pin.provider_auto_pool_observed_at,
            )
        )
        provider_projection_common = (
            provider_projection_present
            and pin.provider_generation_id is not None
            and pin.provider_routing_generation is not None
            and pin.provider_cost_generation is not None
            and pin.provider_generation_state == "active"
            and pin.provider_generation_activated_at is not None
            and pin.provider_generation_activated_at <= now
            and pin.provider_slot_no is not None
            and pin.provider_slot_state == "READY"
            and pin.provider_slot_token_ref_id == pin.token_ref_id
            and pin.provider_desired_schedulable is True
            and pin.provider_observed_schedulable is True
        )
        fixed_projection_confirmed = (
            provider_projection_common
            and route_mode == "fixed_pin"
            and pin.provider_slot_channel_id == pin.channel_id
            and pin.provider_confirmed_pin_binding_id == pin.binding_id
            and pin.provider_pin_confirmed is True
            and pin.provider_probe_confirmed is True
            and pin.provider_auto_pool_generation_id is None
            and pin.provider_auto_pool_subscription_id is None
            and pin.provider_auto_pool_confirmed is not True
        )
        auto_pool_projection_confirmed = (
            provider_projection_common
            and route_mode == "auto_pool"
            and pin.channel_id is None
            and pin.provider_slot_channel_id is None
            and pin.provider_confirmed_pin_binding_id is None
            and pin.provider_pin_confirmed is False
            and pin.provider_auto_pool_generation_id is not None
            and pin.provider_auto_pool_subscription_id is not None
            and pin.provider_auto_pool_confirmed is True
            and pin.provider_auto_pool_generation_state == "active"
            and pin.provider_auto_pool_activated_at is not None
            and pin.provider_auto_pool_activated_at <= now
            and pin.provider_auto_pool_observed_at is not None
            and pin.provider_auto_pool_observed_at <= pin.provider_auto_pool_activated_at
        )
        provider_projection_confirmed = (
            fixed_projection_confirmed or auto_pool_projection_confirmed
        )
        if provider_projection_present and not provider_projection_confirmed:
            raise SellerPriceBandError("seller_price_band_provider_projection_incomplete")
        evidence_anchor = (
            pin.provider_auto_pool_activated_at
            if auto_pool_projection_confirmed
            else pin.provider_generation_activated_at
            if fixed_projection_confirmed
            else now
        )
        assert evidence_anchor is not None
        if valid_from > evidence_anchor:
            raise SellerPriceBandError("seller_price_band_binding_time_invalid")
        if pin.valid_to is not None:
            valid_to = _aware_datetime(pin.valid_to, "seller_price_band_binding_time_invalid")
            if valid_to <= evidence_anchor:
                raise SellerPriceBandError("seller_price_band_binding_not_current")
        if pin.confirmed_at is None:
            raise SellerPriceBandError("seller_price_band_binding_unconfirmed")
        confirmed_at = _aware_datetime(pin.confirmed_at, "seller_price_band_binding_time_invalid")
        if confirmed_at > evidence_anchor:
            raise SellerPriceBandError("seller_price_band_binding_time_invalid")
        if pin.fallback_to_smart_routing is not False:
            raise SellerPriceBandError("seller_price_band_smart_fallback")
        if auto_pool_projection_confirmed:
            if any(
                value is not None
                for value in (
                    pin.decision_id,
                    pin.decision_token_ref_id,
                    pin.decision_model,
                    pin.selected_channel_id,
                    pin.quote_id,
                    pin.scan_run_id,
                    pin.quote_channel_id,
                    pin.quote_model,
                )
            ):
                raise SellerPriceBandError("seller_price_band_auto_pool_identity_mismatch")
            if pin.owned_channel or pin.quote_is_owned is not False:
                raise SellerPriceBandError("seller_price_band_owned_pin")
            ratio = _optional_positive_decimal(pin.realtime_ratio_usd)
            if ratio is None:
                raise SellerPriceBandError("seller_price_band_usd_quote_missing")
            if (
                isinstance(pin.usage_demand_units, bool)
                or not isinstance(pin.usage_demand_units, int)
                or pin.usage_demand_units < 0
            ):
                raise SellerPriceBandError("seller_price_band_usage_invalid")
            equal_cost += ratio
            if pin.usage_demand_units > 0:
                weighted_cost += ratio * pin.usage_demand_units
                total_weight += pin.usage_demand_units
            continue
        legacy_decision_confirmed = (
            pin.decision_id is not None
            and pin.decision_token_ref_id == pin.token_ref_id
            and pin.decision_model == request.model
            and pin.selected_channel_id == pin.channel_id
            and pin.decision_status == "completed"
        )
        if not fixed_projection_confirmed and not legacy_decision_confirmed:
            raise SellerPriceBandError("seller_price_band_decision_incomplete")
        if pin.owned_channel or pin.quote_is_owned is True:
            raise SellerPriceBandError("seller_price_band_owned_pin")
        if pin.quote_is_owned is not False:
            raise SellerPriceBandError("seller_price_band_quote_ownership_unknown")
        if (
            pin.quote_id is None
            or pin.scan_run_id is None
            or pin.quote_channel_id != pin.channel_id
            or pin.quote_model != request.model
        ):
            raise SellerPriceBandError("seller_price_band_quote_mismatch")
        if pin.scan_status != "completed":
            raise SellerPriceBandError("seller_price_band_scan_incomplete")
        ratio = _optional_positive_decimal(pin.realtime_ratio_usd)
        if ratio is None:
            raise SellerPriceBandError("seller_price_band_usd_quote_missing")
        if pin.quote_observed_at is None:
            raise SellerPriceBandError("seller_price_band_usd_quote_missing")
        quote_at = _aware_datetime(pin.quote_observed_at, "seller_price_band_quote_time_invalid")
        if quote_at > now:
            raise SellerPriceBandError("seller_price_band_quote_future")
        if (now - quote_at).total_seconds() > request.quote_max_age_seconds:
            raise SellerPriceBandError("seller_price_band_quote_stale")
        if (
            isinstance(pin.usage_demand_units, bool)
            or not isinstance(pin.usage_demand_units, int)
            or pin.usage_demand_units < 0
        ):
            raise SellerPriceBandError("seller_price_band_usage_invalid")
        equal_cost += ratio
        if pin.usage_demand_units > 0:
            weighted_cost += ratio * pin.usage_demand_units
            total_weight += pin.usage_demand_units
    if total_weight > 0:
        return weighted_cost / Decimal(total_weight), "official_demand_1h"
    return equal_cost / Decimal(len(snapshot.pins)), "equal_fallback"


def _price_jump_controls(
    plan: SellerPriceBandPlan,
    states: tuple[SellerAppliedPriceState, ...],
    active: Mapping[str, SellerPriceJumpControl],
    *,
    controlled_baselines: Mapping[int, Decimal] | None = None,
    now: datetime,
) -> tuple[SellerPriceJumpControl, ...]:
    now = _aware_datetime(now, "seller_price_band_clock_invalid")
    state_by_merchant = {item.merchant_id: item for item in states}
    baseline_by_merchant = dict(controlled_baselines or {})
    controls: list[SellerPriceJumpControl] = []
    for assignment in plan.assignments:
        existing = active.get(assignment.listing_managed_uid)
        target = assignment.pricing.price_multiplier
        target_cost = assignment.pricing.cost_multiplier
        target_delta = assignment.pricing.delta
        if existing is None:
            prior = state_by_merchant.get(assignment.merchant_id)
            baselines = tuple(
                value
                for value in (
                    prior.price_multiplier if prior is not None else None,
                    baseline_by_merchant.get(assignment.merchant_id),
                )
                if value is not None
            )
            baseline = min(baselines) if baselines else None
            if baseline is None or target < baseline * _PRICE_JUMP_THRESHOLD_RATIO:
                controls.append(
                    SellerPriceJumpControl(
                        merchant_id=assignment.merchant_id,
                        entity_id=assignment.listing_managed_uid,
                        action="apply",
                        state="normal",
                        target_price_multiplier=target,
                        target_cost_multiplier=target_cost,
                        target_delta=target_delta,
                    )
                )
                continue
            duration = _price_jump_cooldown_seconds(
                assignment.listing_managed_uid,
                plan.snapshot.fingerprint,
            )
            controls.append(
                SellerPriceJumpControl(
                    merchant_id=assignment.merchant_id,
                    entity_id=assignment.listing_managed_uid,
                    action="takedown",
                    state="cooling",
                    baseline_price_multiplier=baseline,
                    target_price_multiplier=target,
                    target_cost_multiplier=target_cost,
                    target_delta=target_delta,
                    started_at=now,
                    cooling_until=now + timedelta(seconds=duration),
                )
            )
            continue

        if (
            existing.entity_id != assignment.listing_managed_uid
            or existing.merchant_id != assignment.merchant_id
            or existing.baseline_price_multiplier is None
            or existing.started_at is None
            or existing.cooling_until is None
        ):
            raise SellerPriceBandError("seller_price_jump_control_conflict")
        release_reason = existing.release_reason
        state = existing.state
        action = existing.action
        if state == "cooling":
            if target < existing.baseline_price_multiplier * existing.threshold_ratio:
                state = "release_pending"
                action = "release"
                release_reason = "cost_recovered"
            elif now >= existing.cooling_until:
                state = "release_pending"
                action = "release"
                release_reason = "cooldown_elapsed"
            else:
                action = "takedown"
        elif state == "release_pending":
            if (
                release_reason == "cost_recovered"
                and now < existing.cooling_until
                and target >= existing.baseline_price_multiplier * existing.threshold_ratio
            ):
                state = "cooling"
                action = "takedown"
                release_reason = None
            else:
                action = "release"
        controls.append(
            SellerPriceJumpControl(
                merchant_id=assignment.merchant_id,
                entity_id=assignment.listing_managed_uid,
                action=action,
                state=state,
                baseline_price_multiplier=existing.baseline_price_multiplier,
                target_price_multiplier=target,
                target_cost_multiplier=target_cost,
                target_delta=target_delta,
                threshold_ratio=existing.threshold_ratio,
                cooldown_id=existing.cooldown_id,
                started_at=existing.started_at,
                cooling_until=existing.cooling_until,
                release_reason=release_reason,
            )
        )
    return tuple(controls)


def _price_jump_cooldown_seconds(entity_id: str, snapshot_fingerprint: str) -> int:
    digest = hashlib.sha256(f"{entity_id}\n{snapshot_fingerprint}".encode()).digest()
    spread = _PRICE_JUMP_COOLDOWN_MAX_SECONDS - _PRICE_JUMP_COOLDOWN_MIN_SECONDS
    return _PRICE_JUMP_COOLDOWN_MIN_SECONDS + int.from_bytes(digest[:4], "big") % (spread + 1)


def _price_jump_persisted_multiplier(value: object) -> Decimal:
    return _positive_decimal(value, "seller_price_jump_target_invalid").quantize(
        _PRICE_JUMP_MULTIPLIER_QUANTUM,
        rounding=ROUND_HALF_UP,
    )


def _assess_transition(
    request: SellerPriceBandRequest,
    plan: SellerPriceBandPlan,
    states: tuple[SellerAppliedPriceState, ...],
    *,
    now: datetime,
) -> SellerPriceTransition:
    if not states:
        return SellerPriceTransition("initial", None, None, (), 0)
    assignments = {item.merchant_id: item for item in plan.assignments}
    completed_at_by_action: dict[int, datetime] = {}
    for state in states:
        assignment = assignments.get(state.merchant_id)
        if assignment is None or assignment.listing_managed_uid != state.entity_id:
            raise SellerPriceBandError("seller_price_band_previous_state_conflict")
        completed_at = _aware_datetime(
            state.completed_at,
            "seller_price_band_previous_state_invalid",
        )
        if completed_at > now:
            raise SellerPriceBandError("seller_price_band_previous_state_invalid")
        completed_at_by_action[state.action_id] = completed_at

    # A remote batch can succeed for only part of the merchants. The latest
    # completed action identifies that batch's cost; older costs are the rows
    # that still need reconciliation, not conflicting ownership evidence.
    latest_state = max(
        states,
        key=lambda item: (completed_at_by_action[item.action_id], item.action_id),
    )
    previous_cost = latest_state.cost_multiplier
    previous_config_version = latest_state.config_version
    current_cost_states = tuple(
        item
        for item in states
        if item.cost_multiplier == previous_cost and item.config_version == previous_config_version
    )
    if len(current_cost_states) != len(states):
        return SellerPriceTransition(
            "reconcile_partial",
            previous_cost,
            None,
            (),
            len(current_cost_states),
        )
    if len(states) < len(assignments):
        return SellerPriceTransition(
            "reconcile_partial",
            previous_cost,
            None,
            (),
            len(states),
        )
    if previous_config_version != request.config_version:
        return SellerPriceTransition(
            "reconfigure",
            previous_cost,
            None,
            tuple(sorted(assignments)),
            len(states),
        )
    if plan.cost_multiplier < previous_cost:
        decrease = (previous_cost - plan.cost_multiplier) / previous_cost
        if decrease < request.decrease_hysteresis:
            return SellerPriceTransition(
                "hold_hysteresis",
                previous_cost,
                decrease,
                (),
                len(states),
            )
        latest = max(item.completed_at for item in states)
        if (now - latest).total_seconds() < request.minimum_update_interval_seconds:
            return SellerPriceTransition(
                "hold_minimum_interval",
                previous_cost,
                decrease,
                (),
                len(states),
            )
        return SellerPriceTransition(
            "reprice",
            previous_cost,
            decrease,
            (),
            len(states),
        )
    if plan.cost_multiplier > previous_cost:
        hard_floor = _profit_floor_final(request, plan.cost_multiplier)
        affected = tuple(
            sorted(
                state.merchant_id
                for state in states
                if state.final_multiplier < hard_floor
                or assignments[state.merchant_id].pricing.final_multiplier < hard_floor
            )
        )
        return SellerPriceTransition(
            "reprice",
            previous_cost,
            None,
            affected,
            len(states),
        )
    return SellerPriceTransition(
        "reconcile",
        previous_cost,
        Decimal("0"),
        (),
        len(states),
    )


def _applied_state_from_row(
    row: Mapping[str, Any],
    assignments: Mapping[str, SellerPriceAssignment],
) -> SellerAppliedPriceState:
    desired = row.get("desired_state")
    if isinstance(desired, str):
        try:
            desired = json.loads(desired)
        except json.JSONDecodeError:
            desired = None
    entity_id = str(row.get("entity_id") or "")
    assignment = assignments.get(entity_id)
    if not isinstance(desired, Mapping) or assignment is None:
        raise SellerPriceBandError("seller_price_band_previous_state_invalid")
    cost = _optional_positive_decimal(desired.get("cost_multiplier"))
    final = _optional_positive_decimal(desired.get("target_final_multiplier"))
    panel = _optional_positive_decimal(desired.get("a6_panel_multiplier"))
    delta = _optional_decimal(desired.get("delta"))
    config_version = str(row.get("config_version") or "")
    if (
        cost is None
        or final is None
        or panel is None
        or delta is None
        or not _SAFE_AUDIT_LABEL.fullmatch(config_version)
    ):
        raise SellerPriceBandError("seller_price_band_previous_state_invalid")
    return SellerAppliedPriceState(
        merchant_id=assignment.merchant_id,
        entity_id=entity_id,
        action_id=_positive_int(row.get("id"), "seller_price_band_previous_state_invalid"),
        config_version=config_version,
        cost_multiplier=cost,
        final_multiplier=final,
        price_multiplier=panel,
        delta=delta,
        completed_at=_aware_datetime(
            row.get("completed_at"),
            "seller_price_band_previous_state_invalid",
        ),
    )


def _price_jump_control_from_row(row: Mapping[str, Any]) -> SellerPriceJumpControl:
    state = str(row.get("state") or "")
    if state not in {"cooling", "release_pending"}:
        raise SellerPriceBandError("seller_price_jump_row_invalid")
    return SellerPriceJumpControl(
        cooldown_id=_positive_int(row.get("id"), "seller_price_jump_row_invalid"),
        merchant_id=_positive_int(row.get("merchant_id"), "seller_price_jump_row_invalid"),
        entity_id=str(row.get("entity_id") or ""),
        action="takedown" if state == "cooling" else "release",
        state=state,
        baseline_price_multiplier=_positive_decimal(
            row.get("baseline_price_multiplier"),
            "seller_price_jump_row_invalid",
        ),
        target_price_multiplier=_positive_decimal(
            row.get("latest_target_price_multiplier"),
            "seller_price_jump_row_invalid",
        ),
        target_cost_multiplier=_positive_decimal(
            row.get("latest_target_cost_multiplier"),
            "seller_price_jump_row_invalid",
        ),
        target_delta=_bounded_decimal(
            row.get("latest_target_delta"),
            minimum=Decimal("0"),
            maximum=Decimal("0.5"),
            code="seller_price_jump_row_invalid",
        ),
        threshold_ratio=_positive_decimal(
            row.get("threshold_ratio"),
            "seller_price_jump_row_invalid",
        ),
        started_at=_aware_datetime(row.get("started_at"), "seller_price_jump_row_invalid"),
        cooling_until=_aware_datetime(
            row.get("cooling_until"),
            "seller_price_jump_row_invalid",
        ),
        release_reason=(
            str(row.get("release_reason")) if row.get("release_reason") is not None else None
        ),
    )


def _audit_outcome(
    result: SellerMerchantResourceResult,
) -> tuple[str, str | None, dict[str, Any]]:
    observed = result.public()
    decisions = (result.seller, result.channel, result.listing)
    listing_verified = (
        result.error_class is None
        and result.listing.get("state") == "verified"
        and result.listing.get("action") != "blocked"
        and not result.listing.get("error_class")
    )
    if listing_verified:
        return "completed", None, observed
    error = result.error_class or next(
        (str(item.get("error_class")) for item in decisions if item.get("error_class")),
        "seller_price_band_apply_unconfirmed",
    )
    uncertain = any(_decision_result_uncertain(item) for item in decisions)
    return ("unknown" if uncertain else "failed"), _safe_error(error), observed


def _decision_result_uncertain(decision: Mapping[str, Any]) -> bool:
    state = str(decision.get("state") or "")
    action = str(decision.get("action") or "")
    if state == "unknown" or state.endswith("_unknown"):
        return True
    if action == "reconcile_pending" or action.endswith("_unknown"):
        return True
    if not decision.get("applied"):
        return False
    if "verified" in decision:
        return decision.get("verified") is not True
    return state not in {"bound", "verified"}


def _existing_action_matches(
    row: Mapping[str, Any],
    assignment: SellerPriceAssignment,
    desired: Mapping[str, Any],
    plan: SellerPriceBandPlan,
) -> bool:
    return row.get("status") == "completed" and _existing_action_identity_matches(
        row,
        assignment,
        desired,
        plan,
    )


def _existing_action_identity_matches(
    row: Mapping[str, Any],
    assignment: SellerPriceAssignment,
    desired: Mapping[str, Any],
    plan: SellerPriceBandPlan,
) -> bool:
    stored_desired = row.get("desired_state")
    if isinstance(stored_desired, str):
        try:
            stored_desired = json.loads(stored_desired)
        except json.JSONDecodeError:
            return False
    return (
        row.get("action_type") == "apply_seller_price_band"
        and row.get("entity_type") == "a6_managed_listing"
        and row.get("entity_id") == assignment.listing_managed_uid
        and _audit_desired_matches(stored_desired, desired)
        and row.get("config_version") == plan.request.config_version
        and row.get("requested_by") == plan.request.requested_by
    )


def _audit_desired_matches(
    stored: Any,
    desired: Mapping[str, Any],
) -> bool:
    if not isinstance(stored, Mapping):
        return False
    stored_stable = dict(stored)
    desired_stable = dict(desired)
    stored_stable.pop("pin_evidence", None)
    desired_stable.pop("pin_evidence", None)
    return stored_stable == desired_stable


def _pin_from_row(row: Mapping[str, Any]) -> SellerPinEvidence:
    try:
        return SellerPinEvidence(
            binding_id=_positive_int(row.get("binding_id"), "seller_price_band_snapshot_invalid"),
            token_ref_id=_positive_int(
                row.get("token_ref_id"), "seller_price_band_snapshot_invalid"
            ),
            model=str(row.get("model") or ""),
            channel_id=_optional_positive_int(row.get("channel_id")),
            fallback_to_smart_routing=row.get("fallback_to_smart_routing"),
            valid_from=_aware_datetime(row.get("valid_from"), "seller_price_band_snapshot_invalid"),
            valid_to=_optional_datetime(row.get("valid_to"), "seller_price_band_snapshot_invalid"),
            confirmed_at=_optional_datetime(
                row.get("confirmed_at"), "seller_price_band_snapshot_invalid"
            ),
            decision_id=_optional_positive_int(row.get("decision_id")),
            decision_token_ref_id=_optional_positive_int(row.get("decision_token_ref_id")),
            decision_model=(
                str(row.get("decision_model")) if row.get("decision_model") is not None else None
            ),
            selected_channel_id=_optional_positive_int(row.get("selected_channel_id")),
            decision_status=(
                str(row.get("decision_status")) if row.get("decision_status") is not None else None
            ),
            quote_id=_optional_positive_int(row.get("quote_id")),
            scan_run_id=_optional_positive_int(row.get("scan_run_id")),
            quote_channel_id=_optional_positive_int(row.get("quote_channel_id")),
            quote_model=(
                str(row.get("quote_model")) if row.get("quote_model") is not None else None
            ),
            realtime_ratio_usd=_optional_decimal(row.get("realtime_ratio_usd")),
            quote_is_owned=row.get("quote_is_owned"),
            quote_observed_at=_optional_datetime(
                row.get("quote_observed_at"), "seller_price_band_snapshot_invalid"
            ),
            scan_status=(
                str(row.get("scan_status")) if row.get("scan_status") is not None else None
            ),
            owned_channel=row.get("owned_channel"),
            usage_demand_units=int(row.get("usage_demand_units") or 0),
            provider_generation_id=_optional_positive_int(
                row.get("provider_generation_id")
            ),
            provider_routing_generation=_optional_positive_int(
                row.get("provider_routing_generation")
            ),
            provider_cost_generation=_optional_positive_int(
                row.get("provider_cost_generation")
            ),
            provider_generation_state=(
                str(row.get("provider_generation_state"))
                if row.get("provider_generation_state") is not None
                else None
            ),
            provider_generation_activated_at=_optional_datetime(
                row.get("provider_generation_activated_at"),
                "seller_price_band_snapshot_invalid",
            ),
            provider_slot_no=_optional_positive_int(row.get("provider_slot_no")),
            provider_slot_state=(
                str(row.get("provider_slot_state"))
                if row.get("provider_slot_state") is not None
                else None
            ),
            provider_slot_token_ref_id=_optional_positive_int(
                row.get("provider_slot_token_ref_id")
            ),
            provider_slot_channel_id=_optional_positive_int(
                row.get("provider_slot_channel_id")
            ),
            provider_confirmed_pin_binding_id=_optional_positive_int(
                row.get("provider_confirmed_pin_binding_id")
            ),
            provider_pin_confirmed=row.get("provider_pin_confirmed"),
            provider_probe_confirmed=row.get("provider_probe_confirmed"),
            provider_desired_schedulable=row.get("provider_desired_schedulable"),
            provider_observed_schedulable=row.get("provider_observed_schedulable"),
            provider_route_mode=(
                str(row.get("provider_route_mode"))
                if row.get("provider_route_mode") is not None
                else None
            ),
            provider_auto_pool_generation_id=_optional_positive_int(
                row.get("provider_auto_pool_generation_id")
            ),
            provider_auto_pool_subscription_id=_optional_positive_int(
                row.get("provider_auto_pool_subscription_id")
            ),
            provider_auto_pool_confirmed=row.get("provider_auto_pool_confirmed"),
            provider_auto_pool_generation_state=(
                str(row.get("provider_auto_pool_generation_state"))
                if row.get("provider_auto_pool_generation_state") is not None
                else None
            ),
            provider_auto_pool_activated_at=_optional_datetime(
                row.get("provider_auto_pool_activated_at"),
                "seller_price_band_snapshot_invalid",
            ),
            provider_auto_pool_observed_at=_optional_datetime(
                row.get("provider_auto_pool_observed_at"),
                "seller_price_band_snapshot_invalid",
            ),
        )
    except (TypeError, ValueError, InvalidOperation):
        raise SellerPriceBandError("seller_price_band_snapshot_invalid") from None


def _market_quote_from_row(row: Mapping[str, Any]) -> SellerMarketQuoteEvidence:
    try:
        ratio = _optional_positive_decimal(row.get("realtime_ratio_usd"))
        if ratio is None:
            raise ValueError
        return SellerMarketQuoteEvidence(
            channel_id=_positive_int(
                row.get("channel_id"),
                "seller_price_band_market_quote_invalid",
            ),
            realtime_ratio_usd=ratio,
            observer_count=_positive_int(
                row.get("observer_count"),
                "seller_price_band_market_quote_invalid",
            ),
            observed_at=_aware_datetime(
                row.get("observed_at"),
                "seller_price_band_market_quote_time_invalid",
            ),
            is_owned=bool(row.get("is_owned")),
        )
    except (TypeError, ValueError, InvalidOperation):
        raise SellerPriceBandError("seller_price_band_market_quote_invalid") from None


def _provider_exclusion_from_row(
    row: Mapping[str, Any],
) -> SellerProviderExclusion:
    try:
        reason = str(row.get("exclusion_reason") or "current_pin_missing").strip()
        if not reason or not reason.replace("_", "").isalnum():
            reason = "current_pin_missing"
        return SellerProviderExclusion(
            merchant_id=_positive_int(
                row.get("merchant_id"),
                "seller_price_band_provider_exclusion_invalid",
            ),
            token_ref_id=_positive_int(
                row.get("token_ref_id"),
                "seller_price_band_provider_exclusion_invalid",
            ),
            external_token_id=_positive_int(
                row.get("external_token_id"),
                "seller_price_band_provider_exclusion_invalid",
            ),
            enodot_account_id=_positive_int(
                row.get("enodot_account_id"),
                "seller_price_band_provider_exclusion_invalid",
            ),
            reason=reason,
            usage_quota_micros=0,
        )
    except (TypeError, ValueError):
        raise SellerPriceBandError("seller_price_band_provider_exclusion_invalid") from None


def _rows(cursor: Any) -> list[dict[str, Any]]:
    raw = cursor.fetchall()
    if not raw:
        return []
    if isinstance(raw[0], Mapping):
        return [dict(item) for item in raw]
    names = [getattr(column, "name", column[0]) for column in cursor.description]
    return [dict(zip(names, item, strict=True)) for item in raw]


def _row(cursor: Any) -> dict[str, Any] | None:
    raw = cursor.fetchone()
    if raw is None:
        return None
    if isinstance(raw, Mapping):
        return dict(raw)
    names = [getattr(column, "name", column[0]) for column in cursor.description]
    return dict(zip(names, raw, strict=True))


def _positive_int(value: Any, code: str) -> int:
    if isinstance(value, bool):
        raise SellerPriceBandError(code)
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise SellerPriceBandError(code) from None
    if parsed < 1:
        raise SellerPriceBandError(code)
    return parsed


def _optional_positive_int(value: Any) -> int | None:
    if value is None:
        return None
    return _positive_int(value, "seller_price_band_snapshot_invalid")


def _bounded_decimal(
    value: Any,
    *,
    minimum: Decimal,
    maximum: Decimal,
    code: str,
) -> Decimal:
    parsed = _optional_decimal(value)
    if parsed is None or parsed < minimum or parsed > maximum:
        raise SellerPriceBandError(code)
    return parsed


def _optional_decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _optional_positive_decimal(value: Any) -> Decimal | None:
    parsed = _optional_decimal(value)
    return parsed if parsed is not None and parsed > 0 else None


def _positive_decimal(value: Any, code: str) -> Decimal:
    parsed = _optional_positive_decimal(value)
    if parsed is None:
        raise SellerPriceBandError(code)
    return parsed


def _aware_datetime(value: Any, code: str) -> datetime:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.strip())
        except ValueError:
            raise SellerPriceBandError(code) from None
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise SellerPriceBandError(code)
    return value.astimezone(timezone.utc)


def _optional_datetime(value: Any, code: str) -> datetime | None:
    return None if value is None else _aware_datetime(value, code)


def _datetime_text(value: datetime | None) -> str | None:
    if value is None:
        return None
    return _aware_datetime(value, "seller_price_band_snapshot_invalid").isoformat(
        timespec="microseconds"
    )


def _decimal_text(value: Decimal | None) -> str | None:
    return None if value is None else format(value, "f")


def _hash_document(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def _json(value: Mapping[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _safe_error(value: str) -> str:
    normalized = str(value or "").strip()
    return (
        normalized
        if normalized and normalized.replace("_", "").isalnum()
        else "seller_price_band_apply_unconfirmed"
    )


def _close(connection: Any) -> None:
    close = getattr(connection, "close", None)
    if callable(close):
        close()


_SNAPSHOT_QUERY = """
    SELECT b.id AS binding_id,
           b.a6_token_id AS token_ref_id,
           b.model,
           b.channel_id,
           b.fallback_to_smart_routing,
           b.valid_from,
           b.valid_to,
           b.confirmed_at,
           b.decision_id,
           pool_generation.id AS provider_generation_id,
           pool_generation.generation AS provider_routing_generation,
           pool_generation.cost_generation AS provider_cost_generation,
           pool_generation.generation_state AS provider_generation_state,
           pool_generation.activated_at AS provider_generation_activated_at,
           pool_slot.slot_no AS provider_slot_no,
           pool_slot.slot_state AS provider_slot_state,
           pool_slot.a6_token_id AS provider_slot_token_ref_id,
           pool_slot.desired_channel_id AS provider_slot_channel_id,
           pool_slot.confirmed_pin_binding_id AS provider_confirmed_pin_binding_id,
           pool_slot.pin_confirmed AS provider_pin_confirmed,
           pool_slot.probe_confirmed AS provider_probe_confirmed,
           pool_slot.desired_schedulable AS provider_desired_schedulable,
           pool_slot.observed_schedulable AS provider_observed_schedulable,
           pool_slot.route_mode AS provider_route_mode,
           pool_slot.auto_pool_generation_id AS provider_auto_pool_generation_id,
           pool_slot.auto_pool_subscription_id AS provider_auto_pool_subscription_id,
           pool_slot.auto_pool_confirmed AS provider_auto_pool_confirmed,
           NULL::text AS provider_auto_pool_generation_state,
           NULL::timestamptz AS provider_auto_pool_activated_at,
           NULL::timestamptz AS provider_auto_pool_observed_at,
           d.a6_token_id AS decision_token_ref_id,
           d.model AS decision_model,
           d.selected_channel_id,
           d.decision_status,
           quote.quote_id,
           quote.scan_run_id,
           quote.quote_channel_id,
           quote.quote_model,
           quote.realtime_ratio_usd,
           quote.quote_is_owned,
           quote.quote_observed_at,
           quote.scan_status,
           EXISTS (
               SELECT 1
               FROM a6_trade.a6_supplier_channels_current owned
               WHERE owned.a6_channel_id = b.channel_id
           ) AS owned_channel,
           coalesce(usage.usage_demand_units, 0)::numeric AS usage_demand_units
    FROM a6_trade.provider_pool_generations pool_generation
    JOIN a6_trade.provider_pool_slots pool_slot
      ON pool_slot.generation_id = pool_generation.id
     AND pool_slot.model = pool_generation.model
     AND pool_slot.routing_generation = pool_generation.generation
    JOIN a6_trade.a6_pin_bindings b
      ON b.id = pool_slot.confirmed_pin_binding_id
     AND b.a6_token_id = pool_slot.a6_token_id
     AND b.model = pool_slot.model
     AND b.channel_id = pool_slot.desired_channel_id
    JOIN a6_trade.a6_tokens token ON token.id = b.a6_token_id
    JOIN a6_trade.a6_merchants merchant ON merchant.id = token.merchant_id
    LEFT JOIN a6_trade.a6_financial_current financial
      ON financial.merchant_id = merchant.id
    LEFT JOIN a6_trade.a6_pin_decisions d ON d.id = b.decision_id
    LEFT JOIN LATERAL (
        SELECT q.id AS quote_id,
               q.scan_run_id,
               q.channel_id AS quote_channel_id,
               q.model AS quote_model,
               q.realtime_ratio_usd,
               q.is_owned AS quote_is_owned,
               q.observed_at AS quote_observed_at,
               scan.status AS scan_status
        FROM a6_trade.a6_market_quotes q
        JOIN a6_trade.a6_market_scan_runs scan ON scan.id = q.scan_run_id
        WHERE q.channel_id = b.channel_id
          AND q.model = b.model
          AND q.observed_at <= %s
          AND q.created_at <= %s - interval '2 seconds'
        ORDER BY q.observed_at DESC, q.id DESC
        LIMIT 1
    ) quote ON true
    LEFT JOIN LATERAL (
        SELECT sum(
                   coalesce(u.prompt_tokens, 0)::numeric * 5000000
                 + coalesce(u.completion_tokens, 0)::numeric * 30000000
                 + coalesce(u.cache_read_tokens, 0)::numeric * 500000
                 + coalesce(u.cache_write_tokens, 0)::numeric * 6250000
               ) AS usage_demand_units
        FROM a6_trade.a6_provider_usage_events u
        WHERE u.a6_token_ref_id = b.a6_token_id
          AND u.model = b.model
          AND u.channel_id = b.channel_id
          AND u.probe_run_id IS NULL
          AND u.occurred_at >= %s
          AND u.occurred_at <= %s
    ) usage ON true
    WHERE pool_generation.model = %s
      AND pool_generation.generation_state = 'active'
      AND pool_generation.activated_at IS NOT NULL
      AND pool_generation.activated_at <= %s
      AND pool_slot.valid_from <= %s
      AND (pool_slot.valid_to IS NULL OR pool_slot.valid_to > %s)
      AND pool_slot.slot_state = 'READY'
      AND pool_slot.route_mode = 'fixed_pin'
      AND pool_slot.desired_schedulable
      AND pool_slot.observed_schedulable
      AND pool_slot.pin_confirmed
      AND pool_slot.probe_confirmed
      AND b.confirmed_at IS NOT NULL
      AND b.confirmed_at <= pool_generation.activated_at
      AND merchant.roles @> ARRAY['provider']::text[]
      AND EXISTS (
          SELECT 1
          FROM a6_trade.enodot_account_bindings account
          WHERE account.a6_token_id = token.id
            AND account.state = 'bound'
            AND account.enodot_account_id IS NOT NULL
            AND (
                nullif(account.last_error_class, '') IS NULL
                OR account.last_error_class IN (
                    'provider_user_balance_below_one_usd',
                    'provider_user_balance_exhausted',
                    'account_quota_insufficient',
                    'insufficient_quota',
                    'provider_user_balance_insufficient_unfunded'
                )
            )
            AND (
                account.cooldown_until IS NULL
                OR account.cooldown_until <= %s
            )
      )
      AND NOT (
          EXISTS (
              SELECT 1
              FROM a6_trade.enodot_account_bindings account
              WHERE account.a6_token_id = token.id
                AND account.state = 'bound'
                AND account.last_error_class IN (
                    'account_quota_insufficient',
                    'insufficient_quota',
                    'provider_user_balance_insufficient_unfunded'
                )
          )
          AND coalesce(financial.withdrawable_micros, 0) < merchant.quota_per_unit
      )
    ORDER BY pool_slot.slot_no
"""


_AUTO_POOL_SNAPSHOT_QUERY = """
    SELECT auto_account.id AS binding_id,
           pool_slot.a6_token_id AS token_ref_id,
           pool_slot.model,
           NULL::bigint AS channel_id,
           false AS fallback_to_smart_routing,
           auto_generation.activated_at AS valid_from,
           NULL::timestamptz AS valid_to,
           auto_account.completed_at AS confirmed_at,
           NULL::bigint AS decision_id,
           pool_generation.id AS provider_generation_id,
           pool_generation.generation AS provider_routing_generation,
           pool_generation.cost_generation AS provider_cost_generation,
           pool_generation.generation_state AS provider_generation_state,
           pool_generation.activated_at AS provider_generation_activated_at,
           pool_slot.slot_no AS provider_slot_no,
           pool_slot.slot_state AS provider_slot_state,
           pool_slot.a6_token_id AS provider_slot_token_ref_id,
           pool_slot.desired_channel_id AS provider_slot_channel_id,
           pool_slot.confirmed_pin_binding_id AS provider_confirmed_pin_binding_id,
           pool_slot.pin_confirmed AS provider_pin_confirmed,
           pool_slot.probe_confirmed AS provider_probe_confirmed,
           pool_slot.desired_schedulable AS provider_desired_schedulable,
           pool_slot.observed_schedulable AS provider_observed_schedulable,
           pool_slot.route_mode AS provider_route_mode,
           auto_generation.id AS provider_auto_pool_generation_id,
           auto_account.subscription_id AS provider_auto_pool_subscription_id,
           pool_slot.auto_pool_confirmed AS provider_auto_pool_confirmed,
           auto_generation.generation_state AS provider_auto_pool_generation_state,
           auto_generation.activated_at AS provider_auto_pool_activated_at,
           auto_generation.observed_at AS provider_auto_pool_observed_at,
           NULL::bigint AS decision_token_ref_id,
           NULL::text AS decision_model,
           NULL::bigint AS selected_channel_id,
           NULL::text AS decision_status,
           NULL::bigint AS quote_id,
           NULL::bigint AS scan_run_id,
           NULL::bigint AS quote_channel_id,
           NULL::text AS quote_model,
           auto_generation.max_ratio_usd AS realtime_ratio_usd,
           false AS quote_is_owned,
           auto_generation.observed_at AS quote_observed_at,
           NULL::text AS scan_status,
           false AS owned_channel,
           coalesce(usage.usage_demand_units, 0)::numeric AS usage_demand_units
    FROM a6_trade.provider_pool_generations pool_generation
    JOIN a6_trade.provider_pool_slots pool_slot
      ON pool_slot.generation_id = pool_generation.id
     AND pool_slot.model = pool_generation.model
     AND pool_slot.routing_generation = pool_generation.generation
    JOIN a6_trade.provider_auto_pool_generations auto_generation
      ON auto_generation.id = pool_slot.auto_pool_generation_id
     AND auto_generation.model = pool_slot.model
     AND auto_generation.routing_generation = pool_slot.routing_generation
     AND auto_generation.cost_generation = pool_generation.cost_generation
    JOIN a6_trade.provider_auto_pool_accounts auto_account
      ON auto_account.generation_id = auto_generation.id
     AND auto_account.token_ref_id = pool_slot.a6_token_id
     AND auto_account.enodot_account_id = pool_slot.enodot_account_id
     AND auto_account.slot_no = pool_slot.slot_no
     AND auto_account.subscription_id = pool_slot.auto_pool_subscription_id
    JOIN a6_trade.a6_tokens token ON token.id = pool_slot.a6_token_id
    JOIN a6_trade.a6_merchants merchant ON merchant.id = token.merchant_id
    LEFT JOIN a6_trade.a6_financial_current financial
      ON financial.merchant_id = merchant.id
    LEFT JOIN LATERAL (
        SELECT sum(
                   coalesce(u.prompt_tokens, 0)::numeric * 5000000
                 + coalesce(u.completion_tokens, 0)::numeric * 30000000
                 + coalesce(u.cache_read_tokens, 0)::numeric * 500000
                 + coalesce(u.cache_write_tokens, 0)::numeric * 6250000
               ) AS usage_demand_units
        FROM a6_trade.a6_provider_usage_events u
        WHERE u.a6_token_ref_id = pool_slot.a6_token_id
          AND u.model = pool_slot.model
          AND u.probe_run_id IS NULL
          AND u.occurred_at >= %s
          AND u.occurred_at <= %s
    ) usage ON true
    WHERE pool_generation.model = %s
      AND pool_generation.generation_state = 'active'
      AND pool_generation.activated_at IS NOT NULL
      AND pool_generation.activated_at <= %s
      AND pool_slot.valid_from <= %s
      AND (pool_slot.valid_to IS NULL OR pool_slot.valid_to > %s)
      AND pool_slot.slot_state = 'READY'
      AND pool_slot.route_mode = 'auto_pool'
      AND pool_slot.desired_channel_id IS NULL
      AND pool_slot.confirmed_pin_binding_id IS NULL
      AND NOT pool_slot.pin_confirmed
      AND pool_slot.auto_pool_confirmed
      AND pool_slot.desired_schedulable
      AND pool_slot.observed_schedulable
      AND auto_generation.generation_state = 'active'
      AND auto_generation.activated_at IS NOT NULL
      AND auto_generation.activated_at <= %s
      AND auto_account.account_state = 'ready'
      AND auto_account.ban_confirmed
      AND auto_account.unpin_confirmed
      AND auto_account.auto_pool_confirmed
      AND auto_account.cost_confirmed
      AND auto_account.schedulable_confirmed
      AND auto_account.observed_max_ratio_usd = auto_generation.max_ratio_usd
      AND merchant.roles @> ARRAY['provider']::text[]
      AND EXISTS (
          SELECT 1
          FROM a6_trade.enodot_account_bindings account
          WHERE account.a6_token_id = token.id
            AND account.state = 'bound'
            AND account.enodot_account_id = pool_slot.enodot_account_id
            AND (
                nullif(account.last_error_class, '') IS NULL
                OR account.last_error_class IN (
                    'provider_user_balance_below_one_usd',
                    'provider_user_balance_exhausted',
                    'account_quota_insufficient',
                    'insufficient_quota',
                    'provider_user_balance_insufficient_unfunded'
                )
            )
            AND (account.cooldown_until IS NULL OR account.cooldown_until <= %s)
      )
      AND NOT (
          EXISTS (
              SELECT 1
              FROM a6_trade.enodot_account_bindings account
              WHERE account.a6_token_id = token.id
                AND account.state = 'bound'
                AND account.last_error_class IN (
                    'account_quota_insufficient',
                    'insufficient_quota',
                    'provider_user_balance_insufficient_unfunded'
                )
          )
          AND coalesce(financial.withdrawable_micros, 0) < merchant.quota_per_unit
      )
    ORDER BY pool_slot.slot_no
"""


_MARKET_FLOOR_QUERY = """
    SELECT q.channel_id,
           CAST(
               percentile_cont(0.5) WITHIN GROUP (
                   ORDER BY q.realtime_ratio_usd
               ) AS numeric
           ) AS realtime_ratio_usd,
           count(DISTINCT scan.observer_merchant_id)::integer AS observer_count,
           max(q.observed_at) AS observed_at,
           bool_or(q.is_owned) AS is_owned
    FROM a6_trade.a6_market_quotes q
    JOIN a6_trade.a6_market_scan_runs scan ON scan.id = q.scan_run_id
    WHERE q.model = %s
      AND q.observed_at >= %s
      AND q.observed_at <= %s
      AND q.created_at <= %s - interval '2 seconds'
      AND q.realtime_ratio_usd IS NOT NULL
      AND q.is_owned = false
      AND scan.status = 'completed'
    GROUP BY q.channel_id
    ORDER BY realtime_ratio_usd, q.channel_id
    LIMIT 256
"""


_PROVIDER_EXCLUSIONS_QUERY = """
    SELECT merchant.id AS merchant_id,
           token.id AS token_ref_id,
           token.a6_token_id AS external_token_id,
           account.enodot_account_id,
           CASE
               WHEN nullif(account.last_error_class, '') IS NOT NULL
                 AND account.last_error_class NOT IN (
                     'provider_user_balance_below_one_usd',
                     'provider_user_balance_exhausted',
                     'account_quota_insufficient',
                     'insufficient_quota',
                     'provider_user_balance_insufficient_unfunded'
                 )
               THEN account.last_error_class
               WHEN account.cooldown_until IS NOT NULL
                 AND account.cooldown_until > %s
               THEN 'provider_account_cooldown'
               WHEN pin.channel_id IS NULL AND auto_pool.subscription_id IS NULL
               THEN 'current_pin_missing'
               ELSE 'provider_user_balance_insufficient_unfunded'
           END AS exclusion_reason
    FROM a6_trade.a6_merchants merchant
    JOIN a6_trade.a6_tokens token ON token.merchant_id = merchant.id
    LEFT JOIN a6_trade.a6_financial_current financial
      ON financial.merchant_id = merchant.id
    JOIN LATERAL (
        SELECT binding.enodot_account_id, binding.last_error_class,
               binding.cooldown_until
        FROM a6_trade.enodot_account_bindings binding
        WHERE binding.a6_token_id = token.id
          AND binding.state = 'bound'
          AND binding.enodot_account_id IS NOT NULL
        ORDER BY binding.id DESC
        LIMIT 1
    ) account ON true
    LEFT JOIN LATERAL (
        SELECT current_pin.channel_id
        FROM a6_trade.a6_pin_bindings current_pin
        WHERE current_pin.a6_token_id = token.id
          AND current_pin.model = %s
          AND current_pin.valid_from <= %s
          AND (current_pin.valid_to IS NULL OR current_pin.valid_to > %s)
          AND current_pin.confirmed_at IS NOT NULL
        ORDER BY current_pin.valid_from DESC
        LIMIT 1
    ) pin ON true
    LEFT JOIN LATERAL (
        SELECT auto_account.subscription_id
        FROM a6_trade.provider_pool_generations pool_generation
        JOIN a6_trade.provider_pool_slots pool_slot
          ON pool_slot.generation_id = pool_generation.id
         AND pool_slot.model = pool_generation.model
         AND pool_slot.routing_generation = pool_generation.generation
        JOIN a6_trade.provider_auto_pool_generations auto_generation
          ON auto_generation.id = pool_slot.auto_pool_generation_id
         AND auto_generation.generation_state = 'active'
        JOIN a6_trade.provider_auto_pool_accounts auto_account
          ON auto_account.generation_id = auto_generation.id
         AND auto_account.token_ref_id = pool_slot.a6_token_id
         AND auto_account.enodot_account_id = pool_slot.enodot_account_id
         AND auto_account.slot_no = pool_slot.slot_no
         AND auto_account.subscription_id = pool_slot.auto_pool_subscription_id
        WHERE pool_generation.model = %s
          AND pool_generation.generation_state = 'active'
          AND pool_generation.activated_at <= %s
          AND pool_slot.a6_token_id = token.id
          AND pool_slot.valid_from <= %s
          AND (pool_slot.valid_to IS NULL OR pool_slot.valid_to > %s)
          AND pool_slot.route_mode = 'auto_pool'
          AND pool_slot.slot_state = 'READY'
          AND pool_slot.auto_pool_confirmed
          AND pool_slot.desired_schedulable
          AND pool_slot.observed_schedulable
          AND auto_account.account_state = 'ready'
        ORDER BY auto_generation.activated_at DESC
        LIMIT 1
    ) auto_pool ON true
    WHERE merchant.roles @> ARRAY['provider']::text[]
      AND (
          (
              nullif(account.last_error_class, '') IS NOT NULL
              AND account.last_error_class NOT IN (
                  'provider_user_balance_below_one_usd',
                  'provider_user_balance_exhausted',
                  'account_quota_insufficient',
                  'insufficient_quota',
                  'provider_user_balance_insufficient_unfunded'
              )
          )
          OR (
              account.cooldown_until IS NOT NULL
              AND account.cooldown_until > %s
          )
          OR (pin.channel_id IS NULL AND auto_pool.subscription_id IS NULL)
          OR (
              account.last_error_class IN (
                  'account_quota_insufficient',
                  'insufficient_quota',
                  'provider_user_balance_insufficient_unfunded'
              )
              AND coalesce(financial.withdrawable_micros, 0)
                  < merchant.quota_per_unit
          )
      )
    ORDER BY merchant.id, token.id
"""
