"""Priority 1 economic source planning and request-plane projection.

PostgreSQL owns every input and generation. Redis contains only the canonical
active request-plane view and is repaired with an exact-readback CAS projector.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, InvalidOperation
from types import MappingProxyType
from typing import Any, Protocol

from .trade_profit_settings import A6_GROSS_LISTING_FACTOR, DEFAULT_PROFIT_ON_COST_BPS

ECONOMIC_COST_QUANTUM = Decimal("0.000001")
_ZERO_HASH = "0" * 64
_ELIGIBLE_STATES = frozenset({"ELIGIBLE", "THROTTLED"})
_SOURCE_KINDS = frozenset({"api", "account", "a6_gateway"})
_ELIGIBILITY_STATES = frozenset(
    {
        "DISCOVERED",
        "COST_UNKNOWN",
        "REVENUE_UNKNOWN",
        "UNHEALTHY",
        "UNPROFITABLE",
        "ELIGIBLE",
        "THROTTLED",
        "EXHAUSTED",
    }
)


class EconomicRoutingError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class EconomicRoutingConfig:
    enabled: bool = False
    plan_only: bool = True
    projection_apply_enabled: bool = False
    api_sources_enabled: bool = False
    account_sources_enabled: bool = False
    a6_gateway_enabled: bool = False
    minimum_profit_on_cost_bps: int = DEFAULT_PROFIT_ON_COST_BPS
    quota_snapshot_max_age_seconds: int = 60
    channel_revenue_max_age_seconds: int = 30
    health_max_age_seconds: int = 30
    recovery_confirmation_count: int = 2
    cost_quantum: Decimal = ECONOMIC_COST_QUANTUM
    config_version: str = "priority1-economic-v1"

    def __post_init__(self) -> None:
        for name in (
            "enabled",
            "plan_only",
            "projection_apply_enabled",
            "api_sources_enabled",
            "account_sources_enabled",
            "a6_gateway_enabled",
        ):
            if not isinstance(getattr(self, name), bool):
                raise EconomicRoutingError("economic_config_flag_invalid")
        if (
            isinstance(self.minimum_profit_on_cost_bps, bool)
            or not 0 <= self.minimum_profit_on_cost_bps <= 100_000
        ):
            raise EconomicRoutingError("economic_minimum_profit_invalid")
        for value in (
            self.quota_snapshot_max_age_seconds,
            self.channel_revenue_max_age_seconds,
            self.health_max_age_seconds,
        ):
            if isinstance(value, bool) or not 1 <= value <= 86_400:
                raise EconomicRoutingError("economic_freshness_invalid")
        if (
            isinstance(self.recovery_confirmation_count, bool)
            or not 1 <= self.recovery_confirmation_count <= 10
        ):
            raise EconomicRoutingError("economic_confirmation_count_invalid")
        quantum = _positive_decimal(self.cost_quantum, "economic_cost_quantum_invalid")
        version = _label(self.config_version, "economic_config_version_invalid", 128)
        object.__setattr__(self, "cost_quantum", quantum)
        object.__setattr__(self, "config_version", version)

    def source_kind_enabled(self, source_kind: str) -> bool:
        return {
            "api": self.api_sources_enabled,
            "account": self.account_sources_enabled,
            "a6_gateway": self.a6_gateway_enabled,
        }.get(source_kind, False)

    def document(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "plan_only": self.plan_only,
            "projection_apply_enabled": self.projection_apply_enabled,
            "api_sources_enabled": self.api_sources_enabled,
            "account_sources_enabled": self.account_sources_enabled,
            "a6_gateway_enabled": self.a6_gateway_enabled,
            "minimum_profit_on_cost_bps": self.minimum_profit_on_cost_bps,
            "quota_snapshot_max_age_seconds": self.quota_snapshot_max_age_seconds,
            "channel_revenue_max_age_seconds": self.channel_revenue_max_age_seconds,
            "health_max_age_seconds": self.health_max_age_seconds,
            "recovery_confirmation_count": self.recovery_confirmation_count,
            "cost_quantum": _decimal_text(self.cost_quantum),
            "config_version": self.config_version,
        }


@dataclass(frozen=True)
class EconomicSource:
    source_id: int
    source_key: str
    source_kind: str
    origin_class: str
    model: str
    endpoint_ref: str
    endpoint_fingerprint: str
    enodot_account_id: int | None = None
    health_state: str = "UNKNOWN"
    health_observed_at: datetime | None = None
    capacity_state: str = "UNKNOWN"
    capacity_observed_at: datetime | None = None
    capacity_epoch: str | None = None
    configured_reserve_a: Decimal = Decimal("0")
    enabled: bool = False
    config_version: str = "priority1-economic-v1"

    def __post_init__(self) -> None:
        source_id = _positive_int(self.source_id, "economic_source_id_invalid")
        kind = str(self.source_kind).strip().casefold()
        if kind not in _SOURCE_KINDS:
            raise EconomicRoutingError("economic_source_kind_invalid")
        account_id = _optional_positive_int(
            self.enodot_account_id, "economic_source_account_id_invalid"
        )
        if (kind in {"api", "account"}) != (account_id is not None):
            raise EconomicRoutingError("economic_source_account_shape_invalid")
        health = str(self.health_state).strip().upper()
        capacity = str(self.capacity_state).strip().upper()
        if health not in {"HEALTHY", "UNHEALTHY", "UNKNOWN"}:
            raise EconomicRoutingError("economic_source_health_invalid")
        if capacity not in {"AVAILABLE", "THROTTLED", "EXHAUSTED", "UNKNOWN"}:
            raise EconomicRoutingError("economic_source_capacity_invalid")
        health_at = _optional_aware_datetime(
            self.health_observed_at, "economic_source_health_time_invalid"
        )
        capacity_at = _optional_aware_datetime(
            self.capacity_observed_at, "economic_source_capacity_time_invalid"
        )
        fingerprint = str(self.endpoint_fingerprint).strip().casefold()
        if not _sha256(fingerprint):
            raise EconomicRoutingError("economic_source_endpoint_fingerprint_invalid")
        reserve = _nonnegative_decimal(self.configured_reserve_a, "economic_source_reserve_invalid")
        if not isinstance(self.enabled, bool):
            raise EconomicRoutingError("economic_source_enabled_invalid")
        object.__setattr__(self, "source_id", source_id)
        object.__setattr__(
            self,
            "source_key",
            _label(self.source_key, "economic_source_key_invalid", 256),
        )
        object.__setattr__(self, "source_kind", kind)
        object.__setattr__(
            self, "origin_class", _label(self.origin_class, "economic_origin_class_invalid", 128)
        )
        object.__setattr__(self, "model", _text(self.model, "economic_model_invalid", 256))
        object.__setattr__(
            self, "endpoint_ref", _text(self.endpoint_ref, "economic_endpoint_ref_invalid", 1024)
        )
        object.__setattr__(self, "endpoint_fingerprint", fingerprint)
        object.__setattr__(self, "enodot_account_id", account_id)
        object.__setattr__(self, "health_state", health)
        object.__setattr__(self, "health_observed_at", health_at)
        object.__setattr__(self, "capacity_state", capacity)
        object.__setattr__(self, "capacity_observed_at", capacity_at)
        object.__setattr__(
            self,
            "capacity_epoch",
            _optional_text(self.capacity_epoch, "economic_capacity_epoch_invalid", 256),
        )
        object.__setattr__(self, "configured_reserve_a", reserve)
        object.__setattr__(
            self,
            "config_version",
            _label(self.config_version, "economic_source_config_version_invalid", 128),
        )

    def input_document(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "source_key": self.source_key,
            "source_kind": self.source_kind,
            "origin_class": self.origin_class,
            "model": self.model,
            "endpoint_ref": self.endpoint_ref,
            "endpoint_fingerprint": self.endpoint_fingerprint,
            "enodot_account_id": self.enodot_account_id,
            "health_state": self.health_state,
            "health_observed_at": _datetime_text(self.health_observed_at),
            "capacity_state": self.capacity_state,
            "capacity_observed_at": _datetime_text(self.capacity_observed_at),
            "capacity_epoch": self.capacity_epoch,
            "configured_reserve_a": _decimal_text(self.configured_reserve_a),
            "enabled": self.enabled,
            "config_version": self.config_version,
        }


@dataclass(frozen=True)
class SourceCostContract:
    contract_id: int
    source_id: int
    cost_mode: str
    effective_at: datetime
    expires_at: datetime
    config_version: str
    source_hash: str
    contract_state: str = "active"
    declared_k_cny: Decimal | None = None
    cycle_cost_cny: Decimal | None = None
    currency: str = "CNY"
    cost_basis: str | None = None
    quota_basis: str | None = None

    def __post_init__(self) -> None:
        contract_id = _positive_int(self.contract_id, "economic_contract_id_invalid")
        source_id = _positive_int(self.source_id, "economic_contract_source_invalid")
        mode = str(self.cost_mode).strip().casefold()
        if mode not in {"declared_k", "weekly_amortized"}:
            raise EconomicRoutingError("economic_contract_mode_invalid")
        effective = _aware_datetime(self.effective_at, "economic_contract_time_invalid")
        expires = _aware_datetime(self.expires_at, "economic_contract_time_invalid")
        if effective >= expires:
            raise EconomicRoutingError("economic_contract_time_invalid")
        state = str(self.contract_state).strip().casefold()
        if state not in {"active", "expired", "disabled", "superseded"}:
            raise EconomicRoutingError("economic_contract_state_invalid")
        declared = _optional_positive_decimal(
            self.declared_k_cny, "economic_contract_declared_k_invalid"
        )
        cycle = _optional_positive_decimal(
            self.cycle_cost_cny, "economic_contract_cycle_cost_invalid"
        )
        cost_basis = _optional_text(self.cost_basis, "economic_contract_cost_basis_invalid", 64)
        quota_basis = _optional_text(self.quota_basis, "economic_contract_quota_basis_invalid", 64)
        if mode == "declared_k":
            if (
                declared is None
                or cycle is not None
                or cost_basis is not None
                or quota_basis is not None
            ):
                raise EconomicRoutingError("economic_contract_shape_invalid")
        elif (
            cycle is None
            or declared is not None
            or cost_basis != "weekly"
            or quota_basis not in {"full_cycle", "remaining_at_acquisition"}
        ):
            raise EconomicRoutingError("economic_contract_shape_invalid")
        source_hash = str(self.source_hash).strip().casefold()
        if not _sha256(source_hash):
            raise EconomicRoutingError("economic_contract_hash_invalid")
        if str(self.currency).strip().upper() != "CNY":
            raise EconomicRoutingError("economic_contract_currency_invalid")
        object.__setattr__(self, "contract_id", contract_id)
        object.__setattr__(self, "source_id", source_id)
        object.__setattr__(self, "cost_mode", mode)
        object.__setattr__(self, "effective_at", effective)
        object.__setattr__(self, "expires_at", expires)
        object.__setattr__(self, "contract_state", state)
        object.__setattr__(self, "declared_k_cny", declared)
        object.__setattr__(self, "cycle_cost_cny", cycle)
        object.__setattr__(self, "currency", "CNY")
        object.__setattr__(self, "cost_basis", cost_basis)
        object.__setattr__(self, "quota_basis", quota_basis)
        object.__setattr__(self, "source_hash", source_hash)
        object.__setattr__(
            self, "config_version", _label(self.config_version, "economic_contract_version_invalid")
        )

    def input_document(self) -> dict[str, Any]:
        return {
            "contract_id": self.contract_id,
            "source_id": self.source_id,
            "cost_mode": self.cost_mode,
            "declared_k_cny": _decimal_text(self.declared_k_cny),
            "cycle_cost_cny": _decimal_text(self.cycle_cost_cny),
            "currency": self.currency,
            "cost_basis": self.cost_basis,
            "quota_basis": self.quota_basis,
            "effective_at": _datetime_text(self.effective_at),
            "expires_at": _datetime_text(self.expires_at),
            "config_version": self.config_version,
            "source_hash": self.source_hash,
            "contract_state": self.contract_state,
        }


@dataclass(frozen=True)
class SourceQuotaSnapshot:
    snapshot_id: int
    source_id: int
    contract_id: int
    quota_epoch: str
    used_a: Decimal
    remaining_a: Decimal
    total_a: Decimal
    reset_at: datetime
    observed_at: datetime
    payload_hash: str
    snapshot_state: str = "VALID"

    def __post_init__(self) -> None:
        snapshot_id = _positive_int(self.snapshot_id, "economic_quota_snapshot_id_invalid")
        source_id = _positive_int(self.source_id, "economic_quota_source_invalid")
        contract_id = _positive_int(self.contract_id, "economic_quota_contract_invalid")
        used = _nonnegative_decimal(self.used_a, "economic_quota_used_invalid")
        remaining = _nonnegative_decimal(self.remaining_a, "economic_quota_remaining_invalid")
        total = _positive_decimal(self.total_a, "economic_quota_total_invalid")
        if total != used + remaining:
            raise EconomicRoutingError("economic_quota_total_drift")
        reset_at = _aware_datetime(self.reset_at, "economic_quota_reset_invalid")
        observed_at = _aware_datetime(self.observed_at, "economic_quota_observed_invalid")
        if reset_at <= observed_at:
            raise EconomicRoutingError("economic_quota_epoch_invalid")
        state = str(self.snapshot_state).strip().upper()
        if state not in {"VALID", "INVALID", "CONFLICT"}:
            raise EconomicRoutingError("economic_quota_state_invalid")
        payload_hash = str(self.payload_hash).strip().casefold()
        if not _sha256(payload_hash):
            raise EconomicRoutingError("economic_quota_hash_invalid")
        object.__setattr__(self, "snapshot_id", snapshot_id)
        object.__setattr__(self, "source_id", source_id)
        object.__setattr__(self, "contract_id", contract_id)
        object.__setattr__(
            self, "quota_epoch", _text(self.quota_epoch, "economic_quota_epoch_invalid", 256)
        )
        object.__setattr__(self, "used_a", used)
        object.__setattr__(self, "remaining_a", remaining)
        object.__setattr__(self, "total_a", total)
        object.__setattr__(self, "reset_at", reset_at)
        object.__setattr__(self, "observed_at", observed_at)
        object.__setattr__(self, "payload_hash", payload_hash)
        object.__setattr__(self, "snapshot_state", state)

    def input_document(self) -> dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "source_id": self.source_id,
            "contract_id": self.contract_id,
            "quota_epoch": self.quota_epoch,
            "used_a": _decimal_text(self.used_a),
            "remaining_a": _decimal_text(self.remaining_a),
            "total_a": _decimal_text(self.total_a),
            "reset_at": _datetime_text(self.reset_at),
            "observed_at": _datetime_text(self.observed_at),
            "payload_hash": self.payload_hash,
            "snapshot_state": self.snapshot_state,
        }


@dataclass(frozen=True)
class ChannelRevenueSnapshot:
    snapshot_id: int
    model: str
    channel_id: int
    revenue_epoch: str
    channel_net_k_cny: Decimal
    settlement_contract_ref: str
    observed_at: datetime
    payload_hash: str
    snapshot_state: str = "VALID"
    enodot_api_key_id: int | None = None
    sub2api_group_id: int | None = None

    def __post_init__(self) -> None:
        snapshot_id = _positive_int(self.snapshot_id, "economic_revenue_snapshot_id_invalid")
        channel_id = _positive_int(self.channel_id, "economic_channel_id_invalid")
        api_key_id = _optional_positive_int(
            self.enodot_api_key_id, "economic_revenue_api_key_invalid"
        )
        group_id = _optional_positive_int(self.sub2api_group_id, "economic_revenue_group_invalid")
        if api_key_id is None and group_id is None:
            raise EconomicRoutingError("economic_revenue_request_identity_missing")
        state = str(self.snapshot_state).strip().upper()
        if state not in {"VALID", "INVALID", "CONFLICT"}:
            raise EconomicRoutingError("economic_revenue_state_invalid")
        payload_hash = str(self.payload_hash).strip().casefold()
        if not _sha256(payload_hash):
            raise EconomicRoutingError("economic_revenue_hash_invalid")
        object.__setattr__(self, "snapshot_id", snapshot_id)
        object.__setattr__(self, "model", _text(self.model, "economic_model_invalid", 256))
        object.__setattr__(self, "channel_id", channel_id)
        object.__setattr__(
            self,
            "revenue_epoch",
            _text(self.revenue_epoch, "economic_revenue_epoch_invalid", 256),
        )
        object.__setattr__(
            self,
            "channel_net_k_cny",
            _positive_decimal(self.channel_net_k_cny, "economic_revenue_value_invalid"),
        )
        object.__setattr__(
            self,
            "settlement_contract_ref",
            _text(self.settlement_contract_ref, "economic_settlement_ref_invalid", 512),
        )
        object.__setattr__(
            self, "observed_at", _aware_datetime(self.observed_at, "economic_revenue_time_invalid")
        )
        object.__setattr__(self, "payload_hash", payload_hash)
        object.__setattr__(self, "snapshot_state", state)
        object.__setattr__(self, "enodot_api_key_id", api_key_id)
        object.__setattr__(self, "sub2api_group_id", group_id)

    def input_document(self) -> dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "model": self.model,
            "channel_id": self.channel_id,
            "enodot_api_key_id": self.enodot_api_key_id,
            "sub2api_group_id": self.sub2api_group_id,
            "revenue_epoch": self.revenue_epoch,
            "channel_net_k_cny": _decimal_text(self.channel_net_k_cny),
            "settlement_contract_ref": self.settlement_contract_ref,
            "observed_at": _datetime_text(self.observed_at),
            "payload_hash": self.payload_hash,
            "snapshot_state": self.snapshot_state,
        }


@dataclass(frozen=True)
class A6GatewayCostSnapshot:
    model: str
    routing_generation: int
    cost_generation: int
    source_k_cny: Decimal
    cost_output_hash: str
    ready_account_ids: tuple[int, ...]
    activated_at: datetime

    def __post_init__(self) -> None:
        routing = _positive_int(self.routing_generation, "economic_a6_routing_generation_invalid")
        cost = _positive_int(self.cost_generation, "economic_a6_cost_generation_invalid")
        accounts = tuple(
            sorted(
                {
                    _positive_int(value, "economic_a6_account_id_invalid")
                    for value in self.ready_account_ids
                }
            )
        )
        if not accounts:
            raise EconomicRoutingError("economic_a6_ready_set_empty")
        output_hash = str(self.cost_output_hash).strip().casefold()
        if not _sha256(output_hash):
            raise EconomicRoutingError("economic_a6_cost_hash_invalid")
        object.__setattr__(self, "model", _text(self.model, "economic_model_invalid", 256))
        object.__setattr__(self, "routing_generation", routing)
        object.__setattr__(self, "cost_generation", cost)
        object.__setattr__(
            self,
            "source_k_cny",
            _positive_decimal(self.source_k_cny, "economic_a6_cost_invalid"),
        )
        object.__setattr__(self, "cost_output_hash", output_hash)
        object.__setattr__(self, "ready_account_ids", accounts)
        object.__setattr__(
            self,
            "activated_at",
            _aware_datetime(self.activated_at, "economic_a6_activation_time_invalid"),
        )

    def input_document(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "routing_generation": self.routing_generation,
            "cost_generation": self.cost_generation,
            "source_k_cny": _decimal_text(self.source_k_cny),
            "cost_output_hash": self.cost_output_hash,
            "ready_account_ids": list(self.ready_account_ids),
            "activated_at": _datetime_text(self.activated_at),
        }


@dataclass(frozen=True)
class SourceCostResult:
    state: str
    reason_code: str
    source_k_cny: Decimal | None = None
    cost_contract_id: int | None = None
    quota_snapshot_id: int | None = None
    quota_epoch: str | None = None
    cost_snapshot_hash: str | None = None
    required_burn_a_per_second: Decimal = Decimal("0")
    a6_routing_generation: int | None = None
    a6_cost_generation: int | None = None


@dataclass(frozen=True)
class EconomicEligibility:
    model: str
    channel_id: int
    source_id: int
    source_kind: str
    eligibility_state: str
    reason_code: str
    revenue_snapshot_id: int
    evaluated_at: datetime
    enodot_account_id: int | None = None
    cost_contract_id: int | None = None
    quota_snapshot_id: int | None = None
    quota_epoch: str | None = None
    source_k_cny: Decimal | None = None
    channel_net_k_cny: Decimal | None = None
    minimum_channel_k_cny: Decimal | None = None
    profit_on_cost_bps: int | None = None
    cost_snapshot_hash: str | None = None
    revenue_snapshot_hash: str | None = None
    capacity_epoch: str | None = None
    required_burn_a_per_second: Decimal = Decimal("0")
    confirmation_count: int = 0
    a6_routing_generation: int | None = None
    a6_cost_generation: int | None = None
    enodot_api_key_id: int | None = None
    sub2api_group_id: int | None = None

    def __post_init__(self) -> None:
        state = str(self.eligibility_state).strip().upper()
        if state not in _ELIGIBILITY_STATES:
            raise EconomicRoutingError("economic_eligibility_state_invalid")
        if isinstance(self.confirmation_count, bool) or self.confirmation_count < 0:
            raise EconomicRoutingError("economic_confirmation_count_invalid")
        object.__setattr__(self, "eligibility_state", state)
        object.__setattr__(
            self,
            "required_burn_a_per_second",
            _nonnegative_decimal(self.required_burn_a_per_second, "economic_required_burn_invalid"),
        )

    @property
    def eligible(self) -> bool:
        return self.eligibility_state in _ELIGIBLE_STATES

    def semantic_document(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "channel_id": self.channel_id,
            "source_id": self.source_id,
            "source_kind": self.source_kind,
            "enodot_account_id": self.enodot_account_id,
            "cost_contract_id": self.cost_contract_id,
            "quota_snapshot_id": self.quota_snapshot_id,
            "revenue_snapshot_id": self.revenue_snapshot_id,
            "quota_epoch": self.quota_epoch,
            "source_k_cny": _decimal_text(self.source_k_cny),
            "channel_net_k_cny": _decimal_text(self.channel_net_k_cny),
            "minimum_channel_k_cny": _decimal_text(self.minimum_channel_k_cny),
            "profit_on_cost_bps": self.profit_on_cost_bps,
            "eligibility_state": self.eligibility_state,
            "reason_code": self.reason_code,
            "cost_snapshot_hash": self.cost_snapshot_hash,
            "revenue_snapshot_hash": self.revenue_snapshot_hash,
            "capacity_epoch": self.capacity_epoch,
            "required_burn_a_per_second": _decimal_text(self.required_burn_a_per_second),
            "confirmation_count": self.confirmation_count,
            "a6_routing_generation": self.a6_routing_generation,
            "a6_cost_generation": self.a6_cost_generation,
            "enodot_api_key_id": self.enodot_api_key_id,
            "sub2api_group_id": self.sub2api_group_id,
        }


@dataclass(frozen=True)
class EconomicPlan:
    model: str
    parent_generation: int | None
    input_hash: str
    output_hash: str
    evaluated_at: datetime
    config: EconomicRoutingConfig
    records: tuple[EconomicEligibility, ...]

    def __post_init__(self) -> None:
        if not _sha256(self.input_hash) or not _sha256(self.output_hash):
            raise EconomicRoutingError("economic_plan_hash_invalid")
        records = tuple(sorted(self.records, key=lambda item: (item.channel_id, item.source_id)))
        if len({(item.channel_id, item.source_id) for item in records}) != len(records):
            raise EconomicRoutingError("economic_plan_pair_duplicate")
        object.__setattr__(self, "records", records)


def ceil_to_economic_quantum(
    value: Decimal | str | int,
    quantum: Decimal | str = ECONOMIC_COST_QUANTUM,
) -> Decimal:
    amount = _nonnegative_decimal(value, "economic_cost_value_invalid")
    step = _positive_decimal(quantum, "economic_cost_quantum_invalid")
    return (amount / step).to_integral_value(rounding=ROUND_CEILING) * step


def minimum_channel_revenue(
    source_k_cny: Decimal | str | int,
    *,
    minimum_profit_on_cost_bps: int = DEFAULT_PROFIT_ON_COST_BPS,
    quantum: Decimal | str = ECONOMIC_COST_QUANTUM,
) -> Decimal:
    cost = _positive_decimal(source_k_cny, "economic_source_cost_invalid")
    if (
        isinstance(minimum_profit_on_cost_bps, bool)
        or not 0 <= minimum_profit_on_cost_bps <= 100_000
    ):
        raise EconomicRoutingError("economic_minimum_profit_invalid")
    factor = Decimal(1) + Decimal(minimum_profit_on_cost_bps) / Decimal(10_000)
    # A6 retains 20% of the operator's profit, so the channel gate must clear
    # (1 + bps) * 1.2. With the configured 6000 bps that is exactly 1.6 * 1.2
    # = 1.92 (the effective 9200 bps on cost).
    return ceil_to_economic_quantum(cost * factor * A6_GROSS_LISTING_FACTOR, quantum)


def weekly_amortized_source_cost(
    cycle_cost_cny: Decimal | str | int,
    used_a: Decimal | str | int,
    remaining_a: Decimal | str | int,
    *,
    quantum: Decimal | str = ECONOMIC_COST_QUANTUM,
) -> Decimal:
    cost = _positive_decimal(cycle_cost_cny, "economic_contract_cycle_cost_invalid")
    used = _nonnegative_decimal(used_a, "economic_quota_used_invalid")
    remaining = _nonnegative_decimal(remaining_a, "economic_quota_remaining_invalid")
    total = used + remaining
    if total <= 0:
        raise EconomicRoutingError("economic_quota_total_invalid")
    return ceil_to_economic_quantum(cost / total, quantum)


def derive_source_cost(
    source: EconomicSource,
    contract: SourceCostContract | None,
    quota: SourceQuotaSnapshot | None,
    a6_gateway: A6GatewayCostSnapshot | None,
    *,
    now: datetime,
    config: EconomicRoutingConfig,
) -> SourceCostResult:
    now = _aware_datetime(now, "economic_evaluation_time_invalid")
    if not config.enabled:
        return SourceCostResult("UNHEALTHY", "priority1_disabled")
    if not source.enabled:
        return SourceCostResult("UNHEALTHY", "source_disabled")
    if not config.source_kind_enabled(source.source_kind):
        return SourceCostResult("UNHEALTHY", f"{source.source_kind}_sources_disabled")

    if source.source_kind == "a6_gateway":
        if a6_gateway is None or a6_gateway.model != source.model:
            return SourceCostResult("COST_UNKNOWN", "a6_generation_missing")
        cost = ceil_to_economic_quantum(a6_gateway.source_k_cny, config.cost_quantum)
        cost_hash = _hash_document(
            {
                "source_id": source.source_id,
                "a6_gateway": a6_gateway.input_document(),
            }
        )
        return SourceCostResult(
            "KNOWN",
            "a6_active_generation",
            source_k_cny=cost,
            cost_snapshot_hash=cost_hash,
            a6_routing_generation=a6_gateway.routing_generation,
            a6_cost_generation=a6_gateway.cost_generation,
        )

    if contract is None or contract.source_id != source.source_id:
        return SourceCostResult("COST_UNKNOWN", "cost_contract_missing")
    if (
        contract.contract_state != "active"
        or now < contract.effective_at
        or now >= contract.expires_at
    ):
        return SourceCostResult("COST_UNKNOWN", "cost_contract_inactive")

    if source.source_kind == "api":
        if contract.cost_mode != "declared_k" or contract.declared_k_cny is None:
            return SourceCostResult("COST_UNKNOWN", "api_contract_mode_invalid")
        # The declared K is already in final landed CNY coordinates. No exchange,
        # platform, delta, Seller or 5/6 multiplier is applied here.
        return SourceCostResult(
            "KNOWN",
            "api_declared_k",
            source_k_cny=ceil_to_economic_quantum(contract.declared_k_cny, config.cost_quantum),
            cost_contract_id=contract.contract_id,
            cost_snapshot_hash=contract.source_hash,
        )

    if contract.cost_mode != "weekly_amortized" or contract.cycle_cost_cny is None:
        return SourceCostResult("COST_UNKNOWN", "account_contract_mode_invalid")
    if (
        quota is None
        or quota.source_id != source.source_id
        or quota.contract_id != contract.contract_id
    ):
        return SourceCostResult("COST_UNKNOWN", "quota_snapshot_missing")
    if quota.snapshot_state != "VALID":
        return SourceCostResult("COST_UNKNOWN", "quota_snapshot_invalid")
    if quota.total_a != quota.used_a + quota.remaining_a or quota.total_a <= 0:
        return SourceCostResult("COST_UNKNOWN", "quota_total_drift")
    if quota.observed_at > now or _age_seconds(now, quota.observed_at) > (
        config.quota_snapshot_max_age_seconds
    ):
        return SourceCostResult("COST_UNKNOWN", "quota_snapshot_stale")
    if quota.reset_at <= now:
        return SourceCostResult("COST_UNKNOWN", "quota_epoch_expired")

    cost = weekly_amortized_source_cost(
        contract.cycle_cost_cny,
        quota.used_a,
        quota.remaining_a,
        quantum=config.cost_quantum,
    )
    seconds_to_reset = max(Decimal(str((quota.reset_at - now).total_seconds())), Decimal(1))
    burn = max(quota.remaining_a - source.configured_reserve_a, Decimal(0)) / (seconds_to_reset)
    state = "EXHAUSTED" if quota.remaining_a <= source.configured_reserve_a else "KNOWN"
    reason = "quota_at_reserve" if state == "EXHAUSTED" else "weekly_amortized"
    return SourceCostResult(
        state,
        reason,
        source_k_cny=cost,
        cost_contract_id=contract.contract_id,
        quota_snapshot_id=quota.snapshot_id,
        quota_epoch=quota.quota_epoch,
        cost_snapshot_hash=quota.payload_hash,
        required_burn_a_per_second=burn,
    )


def evaluate_channel_source(
    source: EconomicSource,
    revenue: ChannelRevenueSnapshot,
    source_cost: SourceCostResult,
    *,
    now: datetime,
    config: EconomicRoutingConfig,
    previous: EconomicEligibility | None = None,
) -> EconomicEligibility:
    now = _aware_datetime(now, "economic_evaluation_time_invalid")
    common: dict[str, Any] = {
        "model": revenue.model,
        "channel_id": revenue.channel_id,
        "source_id": source.source_id,
        "source_kind": source.source_kind,
        "enodot_account_id": source.enodot_account_id,
        "cost_contract_id": source_cost.cost_contract_id,
        "quota_snapshot_id": source_cost.quota_snapshot_id,
        "revenue_snapshot_id": revenue.snapshot_id,
        "quota_epoch": source_cost.quota_epoch,
        "source_k_cny": source_cost.source_k_cny,
        "channel_net_k_cny": revenue.channel_net_k_cny,
        "cost_snapshot_hash": source_cost.cost_snapshot_hash,
        "revenue_snapshot_hash": revenue.payload_hash,
        "capacity_epoch": source.capacity_epoch,
        "required_burn_a_per_second": source_cost.required_burn_a_per_second,
        "a6_routing_generation": source_cost.a6_routing_generation,
        "a6_cost_generation": source_cost.a6_cost_generation,
        "enodot_api_key_id": revenue.enodot_api_key_id,
        "sub2api_group_id": revenue.sub2api_group_id,
        "evaluated_at": now,
    }

    def result(state: str, reason: str, **overrides: Any) -> EconomicEligibility:
        return EconomicEligibility(
            **common,
            eligibility_state=state,
            reason_code=reason,
            **overrides,
        )

    if source.model != revenue.model:
        raise EconomicRoutingError("economic_model_mismatch")
    if source_cost.state == "UNHEALTHY":
        return result("UNHEALTHY", source_cost.reason_code)
    if source_cost.state == "COST_UNKNOWN" or source_cost.source_k_cny is None:
        return result("COST_UNKNOWN", source_cost.reason_code)
    if source_cost.state == "EXHAUSTED":
        return result("EXHAUSTED", source_cost.reason_code)
    if revenue.snapshot_state != "VALID":
        return result("REVENUE_UNKNOWN", "channel_revenue_invalid")
    if revenue.observed_at > now or _age_seconds(now, revenue.observed_at) > (
        config.channel_revenue_max_age_seconds
    ):
        return result("REVENUE_UNKNOWN", "channel_revenue_stale")
    if (
        source.health_state != "HEALTHY"
        or source.health_observed_at is None
        or source.health_observed_at > now
        or _age_seconds(now, source.health_observed_at) > config.health_max_age_seconds
    ):
        return result("UNHEALTHY", "source_health_gate")
    if (
        source.capacity_state in {"UNKNOWN"}
        or source.capacity_observed_at is None
        or source.capacity_observed_at > now
        or _age_seconds(now, source.capacity_observed_at) > config.health_max_age_seconds
    ):
        return result("UNHEALTHY", "source_capacity_stale")
    if source.capacity_state == "EXHAUSTED":
        return result("EXHAUSTED", "source_capacity_exhausted")

    minimum = minimum_channel_revenue(
        source_cost.source_k_cny,
        minimum_profit_on_cost_bps=config.minimum_profit_on_cost_bps,
        quantum=config.cost_quantum,
    )
    profit_bps = int(
        (
            (revenue.channel_net_k_cny - source_cost.source_k_cny)
            / source_cost.source_k_cny
            * Decimal(10_000)
        ).to_integral_value(rounding=ROUND_FLOOR)
    )
    common["minimum_channel_k_cny"] = minimum
    common["profit_on_cost_bps"] = profit_bps
    if revenue.channel_net_k_cny < minimum:
        return result("UNPROFITABLE", "profit_gate_below_configured_minimum")

    target_state = "THROTTLED" if source.capacity_state == "THROTTLED" else "ELIGIBLE"
    confirmation = _next_confirmation_count(
        previous,
        source_cost_hash=source_cost.cost_snapshot_hash,
        revenue_hash=revenue.payload_hash,
        target_state=target_state,
        required=config.recovery_confirmation_count,
    )
    if previous is not None and previous.eligible:
        confirmation = config.recovery_confirmation_count
    if confirmation < config.recovery_confirmation_count:
        return result(
            "DISCOVERED",
            "eligible_confirmation_pending",
            confirmation_count=confirmation,
        )
    return result(
        target_state,
        "profit_gate_passed",
        confirmation_count=confirmation,
    )


def build_economic_plan(
    model: str,
    sources: Sequence[EconomicSource],
    contracts: Mapping[int, SourceCostContract],
    quotas: Mapping[int, SourceQuotaSnapshot],
    revenues: Sequence[ChannelRevenueSnapshot],
    *,
    a6_gateway: A6GatewayCostSnapshot | None,
    previous_records: Mapping[tuple[int, int], EconomicEligibility] | None,
    parent_generation: int | None,
    now: datetime,
    config: EconomicRoutingConfig,
) -> EconomicPlan:
    model = _text(model, "economic_model_invalid", 256)
    now = _aware_datetime(now, "economic_evaluation_time_invalid")
    selected_sources = tuple(
        sorted(
            (item for item in sources if item.model == model),
            key=lambda item: item.source_id,
        )
    )
    selected_revenues = tuple(
        sorted((item for item in revenues if item.model == model), key=lambda x: x.channel_id)
    )
    if len({item.source_id for item in selected_sources}) != len(selected_sources):
        raise EconomicRoutingError("economic_source_duplicate")
    if len({item.channel_id for item in selected_revenues}) != len(selected_revenues):
        raise EconomicRoutingError("economic_channel_revenue_ambiguous")
    previous = previous_records or {}
    source_costs = {
        source.source_id: derive_source_cost(
            source,
            contracts.get(source.source_id),
            quotas.get(source.source_id),
            a6_gateway,
            now=now,
            config=config,
        )
        for source in selected_sources
    }
    records = tuple(
        evaluate_channel_source(
            source,
            revenue,
            source_costs[source.source_id],
            now=now,
            config=config,
            previous=previous.get((revenue.channel_id, source.source_id)),
        )
        for revenue in selected_revenues
        for source in selected_sources
    )
    input_document = {
        "schema_version": 1,
        "model": model,
        "config": config.document(),
        "sources": [item.input_document() for item in selected_sources],
        "contracts": [
            contracts[item.source_id].input_document()
            for item in selected_sources
            if item.source_id in contracts
        ],
        "quotas": [
            quotas[item.source_id].input_document()
            for item in selected_sources
            if item.source_id in quotas
        ],
        "revenues": [item.input_document() for item in selected_revenues],
        "a6_gateway": None if a6_gateway is None else a6_gateway.input_document(),
    }
    output_document = {
        "schema_version": 1,
        "model": model,
        "records": [item.semantic_document() for item in records],
    }
    return EconomicPlan(
        model=model,
        parent_generation=parent_generation,
        input_hash=_hash_document(input_document),
        output_hash=_hash_document(output_document),
        evaluated_at=now,
        config=config,
        records=records,
    )


def _next_confirmation_count(
    previous: EconomicEligibility | None,
    *,
    source_cost_hash: str | None,
    revenue_hash: str,
    target_state: str,
    required: int,
) -> int:
    if previous is None:
        return 1
    consistent = (
        previous.cost_snapshot_hash == source_cost_hash
        and previous.revenue_snapshot_hash == revenue_hash
        and previous.eligibility_state in {"DISCOVERED", target_state}
    )
    return min(required, previous.confirmation_count + 1) if consistent else 1


@dataclass(frozen=True)
class ProjectedEconomicSource:
    source_id: int
    source_kind: str
    endpoint_fingerprint: str
    account_ids: tuple[int, ...]
    source_k_cny: Decimal
    channel_net_k_cny: Decimal
    minimum_channel_k_cny: Decimal
    profit_on_cost_bps: int
    required_burn_a_per_second: Decimal
    capacity_epoch: str | None
    cost_contract_id: int | None
    quota_epoch: str | None
    cost_snapshot_hash: str
    a6_routing_generation: int | None = None
    a6_cost_generation: int | None = None

    def __post_init__(self) -> None:
        source_id = _positive_int(self.source_id, "economic_projection_source_id_invalid")
        if self.source_kind not in _SOURCE_KINDS:
            raise EconomicRoutingError("economic_projection_source_kind_invalid")
        account_ids = tuple(
            sorted(
                {
                    _positive_int(value, "economic_projection_account_id_invalid")
                    for value in self.account_ids
                }
            )
        )
        if not account_ids:
            raise EconomicRoutingError("economic_projection_account_set_empty")
        fingerprint = str(self.endpoint_fingerprint).strip().casefold()
        cost_hash = str(self.cost_snapshot_hash).strip().casefold()
        if not _sha256(fingerprint) or not _sha256(cost_hash):
            raise EconomicRoutingError("economic_projection_hash_invalid")
        if isinstance(self.profit_on_cost_bps, bool):
            raise EconomicRoutingError("economic_projection_profit_invalid")
        profit_bps = _int(self.profit_on_cost_bps, "economic_projection_profit_invalid")
        object.__setattr__(self, "source_id", source_id)
        object.__setattr__(self, "account_ids", account_ids)
        object.__setattr__(self, "endpoint_fingerprint", fingerprint)
        object.__setattr__(self, "cost_snapshot_hash", cost_hash)
        object.__setattr__(self, "profit_on_cost_bps", profit_bps)
        for name in (
            "source_k_cny",
            "channel_net_k_cny",
            "minimum_channel_k_cny",
        ):
            object.__setattr__(
                self,
                name,
                _positive_decimal(getattr(self, name), "economic_projection_value_invalid"),
            )
        object.__setattr__(
            self,
            "required_burn_a_per_second",
            _nonnegative_decimal(
                self.required_burn_a_per_second, "economic_projection_burn_invalid"
            ),
        )

    def document(self, *, selection_rank: int) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "source_kind": self.source_kind,
            "priority": 1,
            "selection_rank": selection_rank,
            "endpoint_fingerprint": self.endpoint_fingerprint,
            "account_ids": list(self.account_ids),
            "source_k_cny": _decimal_text(self.source_k_cny),
            "channel_net_k_cny": _decimal_text(self.channel_net_k_cny),
            "minimum_channel_k_cny": _decimal_text(self.minimum_channel_k_cny),
            "profit_on_cost_bps": self.profit_on_cost_bps,
            "required_burn_a_per_second": _decimal_text(self.required_burn_a_per_second),
            "capacity_epoch": self.capacity_epoch,
            "cost_contract_id": self.cost_contract_id,
            "quota_epoch": self.quota_epoch,
            "cost_snapshot_hash": self.cost_snapshot_hash,
            "a6_routing_generation": self.a6_routing_generation,
            "a6_cost_generation": self.a6_cost_generation,
        }


def sort_projected_sources(
    sources: Iterable[ProjectedEconomicSource],
) -> tuple[ProjectedEconomicSource, ...]:
    return tuple(
        sorted(
            sources,
            key=lambda item: (
                0 if item.source_kind == "account" and item.required_burn_a_per_second > 0 else 1,
                -item.required_burn_a_per_second,
                item.source_k_cny,
                item.source_id,
            ),
        )
    )


@dataclass(frozen=True)
class EconomicChannelProjection:
    channel_id: int
    revenue_snapshot_id: int
    revenue_snapshot_hash: str
    enodot_api_key_id: int | None
    sub2api_group_id: int | None
    sources: tuple[ProjectedEconomicSource, ...]

    def __post_init__(self) -> None:
        channel_id = _positive_int(self.channel_id, "economic_projection_channel_invalid")
        revenue_id = _positive_int(
            self.revenue_snapshot_id, "economic_projection_revenue_snapshot_invalid"
        )
        revenue_hash = str(self.revenue_snapshot_hash).strip().casefold()
        if not _sha256(revenue_hash):
            raise EconomicRoutingError("economic_projection_revenue_hash_invalid")
        api_key = _optional_positive_int(
            self.enodot_api_key_id, "economic_projection_api_key_invalid"
        )
        group = _optional_positive_int(self.sub2api_group_id, "economic_projection_group_invalid")
        if api_key is None and group is None:
            raise EconomicRoutingError("economic_projection_request_identity_missing")
        sources = sort_projected_sources(self.sources)
        if len({item.source_id for item in sources}) != len(sources):
            raise EconomicRoutingError("economic_projection_source_duplicate")
        account_ids = [account_id for item in sources for account_id in item.account_ids]
        if len(set(account_ids)) != len(account_ids):
            raise EconomicRoutingError("economic_projection_account_duplicate")
        object.__setattr__(self, "channel_id", channel_id)
        object.__setattr__(self, "revenue_snapshot_id", revenue_id)
        object.__setattr__(self, "revenue_snapshot_hash", revenue_hash)
        object.__setattr__(self, "enodot_api_key_id", api_key)
        object.__setattr__(self, "sub2api_group_id", group)
        object.__setattr__(self, "sources", sources)

    def document(self) -> dict[str, Any]:
        sources = [
            item.document(selection_rank=index) for index, item in enumerate(self.sources, start=1)
        ]
        return {
            "channel_id": self.channel_id,
            "revenue_snapshot_id": self.revenue_snapshot_id,
            "revenue_snapshot_hash": self.revenue_snapshot_hash,
            "enodot_api_key_id": self.enodot_api_key_id,
            "sub2api_group_id": self.sub2api_group_id,
            "candidate_hash": _hash_document(sources),
            "sources": sources,
        }


@dataclass(frozen=True)
class EconomicProjectionBundle:
    model: str
    economic_generation: int
    input_hash: str
    output_hash: str
    evaluated_at: datetime
    channels: tuple[EconomicChannelProjection, ...]
    state: str = "active"
    schema_version: int = 1

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise EconomicRoutingError("economic_projection_schema_invalid")
        state = str(self.state).strip().casefold()
        if state not in {"active", "absent"}:
            raise EconomicRoutingError("economic_projection_state_invalid")
        generation = _positive_int(
            self.economic_generation, "economic_projection_generation_invalid"
        )
        input_hash = str(self.input_hash).strip().casefold()
        output_hash = str(self.output_hash).strip().casefold()
        if not _sha256(input_hash) or not _sha256(output_hash):
            raise EconomicRoutingError("economic_projection_hash_invalid")
        channels = tuple(sorted(self.channels, key=lambda item: item.channel_id))
        if len({item.channel_id for item in channels}) != len(channels):
            raise EconomicRoutingError("economic_projection_channel_duplicate")
        if state == "absent" and channels:
            raise EconomicRoutingError("economic_projection_tombstone_populated")
        object.__setattr__(self, "model", _text(self.model, "economic_model_invalid", 256))
        object.__setattr__(self, "economic_generation", generation)
        object.__setattr__(self, "input_hash", input_hash)
        object.__setattr__(self, "output_hash", output_hash)
        object.__setattr__(
            self,
            "evaluated_at",
            _aware_datetime(self.evaluated_at, "economic_projection_time_invalid"),
        )
        object.__setattr__(self, "channels", channels)
        object.__setattr__(self, "state", state)

    @classmethod
    def tombstone(
        cls,
        model: str,
        economic_generation: int,
        *,
        evaluated_at: datetime,
    ) -> EconomicProjectionBundle:
        return cls(
            model=model,
            economic_generation=economic_generation,
            input_hash=_ZERO_HASH,
            output_hash=_ZERO_HASH,
            evaluated_at=evaluated_at,
            channels=(),
            state="absent",
        )

    def document(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "state": self.state,
            "model": self.model,
            "economic_generation": self.economic_generation,
            "input_hash": self.input_hash,
            "output_hash": self.output_hash,
            "evaluated_at": _datetime_text(self.evaluated_at),
            "channels": [item.document() for item in self.channels],
        }

    def payload(self) -> str:
        return _canonical_json(self.document())

    def digest(self) -> str:
        return hashlib.sha256(self.payload().encode()).hexdigest()


@dataclass(frozen=True)
class FrozenEconomicCandidateSet:
    model: str
    channel_id: int
    economic_generation: int
    candidate_hash: str
    sources: tuple[ProjectedEconomicSource, ...]

    def __post_init__(self) -> None:
        model = _text(self.model, "economic_model_invalid", 256)
        channel_id = _positive_int(self.channel_id, "economic_request_channel_invalid")
        generation = _positive_int(self.economic_generation, "economic_request_generation_invalid")
        candidate_hash = str(self.candidate_hash).strip().casefold()
        if not _sha256(candidate_hash):
            raise EconomicRoutingError("economic_request_candidate_hash_invalid")
        sources = sort_projected_sources(self.sources)
        if len({item.source_id for item in sources}) != len(sources):
            raise EconomicRoutingError("economic_request_source_duplicate")
        expected = _hash_document(
            [item.document(selection_rank=index) for index, item in enumerate(sources, 1)]
        )
        if candidate_hash != expected:
            raise EconomicRoutingError("economic_request_candidate_hash_mismatch")
        object.__setattr__(self, "model", model)
        object.__setattr__(self, "channel_id", channel_id)
        object.__setattr__(self, "economic_generation", generation)
        object.__setattr__(self, "candidate_hash", candidate_hash)
        object.__setattr__(self, "sources", sources)

    def account_ids(self) -> frozenset[int]:
        return frozenset(account_id for source in self.sources for account_id in source.account_ids)

    def source_for_account(self, account_id: int) -> ProjectedEconomicSource | None:
        for source in self.sources:
            if account_id in source.account_ids:
                return source
        return None

    def select(
        self,
        *,
        attempted_source_ids: Iterable[int] = (),
        blocked_account_ids: Iterable[int] = (),
    ) -> ProjectedEconomicSource | None:
        attempted = set(attempted_source_ids)
        blocked = set(blocked_account_ids)
        for source in self.sources:
            if source.source_id in attempted:
                continue
            if any(account_id not in blocked for account_id in source.account_ids):
                return source
        return None


@dataclass(frozen=True)
class EconomicAttemptLedgerEntry:
    """One attempt's planned and realized economics in the common K_CNY frame."""

    attempt_id: str
    source_id: int
    source_k_cny: Decimal
    channel_net_k_cny: Decimal
    attempt_outcome: str
    planned_a: Decimal | None = None
    realized_a: Decimal | None = None
    realized_cost_cny: Decimal | None = None
    realized_revenue_cny: Decimal | None = None

    def __post_init__(self) -> None:
        outcome = str(self.attempt_outcome).strip().casefold()
        if outcome not in {"started", "succeeded", "failed", "won", "lost", "cancelled"}:
            raise EconomicRoutingError("economic_attempt_outcome_invalid")
        object.__setattr__(
            self,
            "attempt_id",
            _text(self.attempt_id, "economic_attempt_id_invalid", 256),
        )
        object.__setattr__(
            self,
            "source_id",
            _positive_int(self.source_id, "economic_attempt_source_invalid"),
        )
        object.__setattr__(
            self,
            "source_k_cny",
            _positive_decimal(self.source_k_cny, "economic_attempt_cost_rate_invalid"),
        )
        object.__setattr__(
            self,
            "channel_net_k_cny",
            _positive_decimal(self.channel_net_k_cny, "economic_attempt_revenue_rate_invalid"),
        )
        for name in (
            "planned_a",
            "realized_a",
            "realized_cost_cny",
            "realized_revenue_cny",
        ):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(
                    self,
                    name,
                    _nonnegative_decimal(value, "economic_attempt_value_invalid"),
                )
        object.__setattr__(self, "attempt_outcome", outcome)

    @property
    def planned_cost_cny(self) -> Decimal | None:
        if self.planned_a is None:
            return None
        return self.planned_a * self.source_k_cny

    @property
    def planned_revenue_cny(self) -> Decimal | None:
        if self.planned_a is None:
            return None
        return self.planned_a * self.channel_net_k_cny

    @property
    def planned_profit_cny(self) -> Decimal | None:
        if self.planned_cost_cny is None or self.planned_revenue_cny is None:
            return None
        return self.planned_revenue_cny - self.planned_cost_cny

    @property
    def realized_profit_cny(self) -> Decimal | None:
        if self.realized_cost_cny is None or self.realized_revenue_cny is None:
            return None
        return self.realized_revenue_cny - self.realized_cost_cny


def aggregate_request_economics(
    attempts: Iterable[EconomicAttemptLedgerEntry],
) -> dict[str, Decimal | int | bool]:
    records = tuple(attempts)
    if len({item.attempt_id for item in records}) != len(records):
        raise EconomicRoutingError("economic_attempt_id_duplicate")
    if len({item.source_id for item in records}) != len(records):
        raise EconomicRoutingError("economic_attempt_source_duplicate")
    realized = tuple(
        item
        for item in records
        if item.realized_cost_cny is not None and item.realized_revenue_cny is not None
    )
    total_cost = sum((item.realized_cost_cny for item in realized), Decimal(0))
    total_revenue = sum((item.realized_revenue_cny for item in realized), Decimal(0))
    total_profit = total_revenue - total_cost
    return {
        "attempt_count": len(records),
        "realized_attempt_count": len(realized),
        "realized_cost_cny": total_cost,
        "realized_revenue_cny": total_revenue,
        "realized_profit_cny": total_profit,
        "realized_profit_negative": total_profit < 0,
    }


def freeze_economic_candidates(
    bundle: EconomicProjectionBundle,
    *,
    api_key_id: int,
    group_id: int | None,
    request_origin_fingerprint: str | None = None,
    visited_source_fingerprints: Iterable[str] = (),
    economic_hop_count: int = 0,
) -> FrozenEconomicCandidateSet | None:
    if bundle.state != "active":
        return None
    api_key_id = _positive_int(api_key_id, "economic_request_api_key_invalid")
    group_id = _optional_positive_int(group_id, "economic_request_group_invalid")
    if isinstance(economic_hop_count, bool) or not 0 <= economic_hop_count <= 1:
        raise EconomicRoutingError("economic_hop_count_invalid")
    if economic_hop_count >= 1:
        return None
    origin = None
    if request_origin_fingerprint is not None:
        origin = str(request_origin_fingerprint).strip().casefold()
        if not _sha256(origin):
            raise EconomicRoutingError("economic_request_origin_invalid")
    visited = {str(value).strip().casefold() for value in visited_source_fingerprints}
    if any(not _sha256(value) for value in visited):
        raise EconomicRoutingError("economic_visited_source_invalid")
    by_key = [item for item in bundle.channels if item.enodot_api_key_id == api_key_id]
    candidates = by_key
    if not candidates and group_id is not None:
        candidates = [item for item in bundle.channels if item.sub2api_group_id == group_id]
    if not candidates:
        return None
    if len(candidates) != 1:
        raise EconomicRoutingError("economic_request_channel_ambiguous")
    channel = candidates[0]
    sources = tuple(
        item
        for item in channel.sources
        if item.endpoint_fingerprint != origin and item.endpoint_fingerprint not in visited
    )
    document = [item.document(selection_rank=index) for index, item in enumerate(sources, 1)]
    return FrozenEconomicCandidateSet(
        model=bundle.model,
        channel_id=channel.channel_id,
        economic_generation=bundle.economic_generation,
        candidate_hash=_hash_document(document),
        sources=sources,
    )


@dataclass(frozen=True)
class EconomicProjectionPublishResult:
    key: str
    digest: str
    economic_generation: int
    state: str
    changed: bool


class EconomicProjectionSink(Protocol):
    def publish(self, bundle: EconomicProjectionBundle) -> EconomicProjectionPublishResult: ...


class RedisEconomicProjectionSink:
    def __init__(
        self,
        client: Any,
        *,
        key_prefix: str = "a6:economic:priority1:v1",
        notification_channel: str = "a6:economic:priority1:changed:v1",
    ) -> None:
        if not callable(getattr(client, "eval", None)) or not callable(
            getattr(client, "get", None)
        ):
            raise EconomicRoutingError("economic_redis_client_invalid")
        self.client = client
        self.key_prefix = _redis_name(key_prefix, "economic_redis_key_prefix_invalid")
        self.notification_channel = _redis_name(
            notification_channel, "economic_redis_channel_invalid"
        )

    def key(self, model: str) -> str:
        return f"{self.key_prefix}:{_text(model, 'economic_model_invalid', 256)}"

    def publish(self, bundle: EconomicProjectionBundle) -> EconomicProjectionPublishResult:
        if not isinstance(bundle, EconomicProjectionBundle):
            raise EconomicRoutingError("economic_projection_bundle_invalid")
        payload = bundle.payload()
        digest = bundle.digest()
        notification = _canonical_json(
            {
                "schema_version": 1,
                "state": bundle.state,
                "model": bundle.model,
                "economic_generation": bundle.economic_generation,
                "digest": digest,
            }
        )
        result = self.client.eval(
            _ATOMIC_ECONOMIC_PUBLISH_LUA,
            1,
            self.key(bundle.model),
            payload,
            bundle.schema_version,
            bundle.model,
            bundle.economic_generation,
            bundle.state,
            self.notification_channel,
            notification,
        )
        try:
            status = int(result)
        except (TypeError, ValueError):
            raise EconomicRoutingError("economic_redis_result_invalid") from None
        if status == -1:
            raise EconomicRoutingError("economic_redis_stale_generation")
        if status == -2:
            raise EconomicRoutingError("economic_redis_existing_projection_invalid")
        if status not in {0, 1}:
            raise EconomicRoutingError("economic_redis_result_invalid")
        observed = self.client.get(self.key(bundle.model))
        if isinstance(observed, bytes):
            try:
                observed = observed.decode()
            except UnicodeDecodeError:
                observed = None
        if observed != payload:
            raise EconomicRoutingError("economic_redis_readback_mismatch")
        return EconomicProjectionPublishResult(
            key=self.key(bundle.model),
            digest=digest,
            economic_generation=bundle.economic_generation,
            state=bundle.state,
            changed=status == 1,
        )


_ATOMIC_ECONOMIC_PUBLISH_LUA = r"""
local current = redis.call('GET', KEYS[1])
if current == ARGV[1] then
    return 0
end
if current then
    local ok, decoded = pcall(cjson.decode, current)
    if not ok
       or tonumber(decoded.schema_version) ~= tonumber(ARGV[2])
       or tostring(decoded.model) ~= ARGV[3]
       or tonumber(decoded.economic_generation) == nil then
        return -2
    end
    local current_generation = tonumber(decoded.economic_generation)
    local desired_generation = tonumber(ARGV[4])
    if current_generation >= desired_generation then
        return -1
    end
end
redis.call('SET', KEYS[1], ARGV[1])
redis.call('PUBLISH', ARGV[6], ARGV[7])
return 1
"""


@dataclass(frozen=True)
class EconomicGenerationActivation:
    model: str
    economic_generation: int
    generation_id: int
    input_hash: str
    output_hash: str
    changed: bool
    plan_only: bool
    activated_at: datetime


def build_projection_bundle(
    plan: EconomicPlan,
    activation: EconomicGenerationActivation,
    sources: Sequence[EconomicSource],
    *,
    a6_gateway: A6GatewayCostSnapshot | None,
) -> EconomicProjectionBundle:
    source_map = {item.source_id: item for item in sources}
    grouped: dict[int, list[ProjectedEconomicSource]] = {}
    channel_identity: dict[int, tuple[int, str, int | None, int | None]] = {}
    for record in plan.records:
        if not record.eligible:
            continue
        source = source_map.get(record.source_id)
        if source is None:
            raise EconomicRoutingError("economic_projection_source_missing")
        account_ids: tuple[int, ...]
        if source.source_kind == "a6_gateway":
            if (
                a6_gateway is None
                or record.a6_routing_generation != a6_gateway.routing_generation
                or record.a6_cost_generation != a6_gateway.cost_generation
            ):
                raise EconomicRoutingError("economic_projection_a6_generation_mismatch")
            account_ids = a6_gateway.ready_account_ids
        else:
            if source.enodot_account_id is None:
                raise EconomicRoutingError("economic_projection_account_missing")
            account_ids = (source.enodot_account_id,)
        if (
            record.source_k_cny is None
            or record.channel_net_k_cny is None
            or record.minimum_channel_k_cny is None
            or record.profit_on_cost_bps is None
            or record.cost_snapshot_hash is None
            or record.revenue_snapshot_hash is None
        ):
            raise EconomicRoutingError("economic_projection_eligibility_incomplete")
        grouped.setdefault(record.channel_id, []).append(
            ProjectedEconomicSource(
                source_id=record.source_id,
                source_kind=record.source_kind,
                endpoint_fingerprint=source.endpoint_fingerprint,
                account_ids=account_ids,
                source_k_cny=record.source_k_cny,
                channel_net_k_cny=record.channel_net_k_cny,
                minimum_channel_k_cny=record.minimum_channel_k_cny,
                profit_on_cost_bps=record.profit_on_cost_bps,
                required_burn_a_per_second=record.required_burn_a_per_second,
                capacity_epoch=record.capacity_epoch,
                cost_contract_id=record.cost_contract_id,
                quota_epoch=record.quota_epoch,
                cost_snapshot_hash=record.cost_snapshot_hash,
                a6_routing_generation=record.a6_routing_generation,
                a6_cost_generation=record.a6_cost_generation,
            )
        )
        identity = (
            record.revenue_snapshot_id,
            record.revenue_snapshot_hash,
            record.enodot_api_key_id,
            record.sub2api_group_id,
        )
        if (
            record.channel_id in channel_identity
            and channel_identity[record.channel_id] != identity
        ):
            raise EconomicRoutingError("economic_projection_revenue_identity_mismatch")
        channel_identity[record.channel_id] = identity
    channels = tuple(
        EconomicChannelProjection(
            channel_id=channel_id,
            revenue_snapshot_id=channel_identity[channel_id][0],
            revenue_snapshot_hash=channel_identity[channel_id][1],
            enodot_api_key_id=channel_identity[channel_id][2],
            sub2api_group_id=channel_identity[channel_id][3],
            sources=tuple(items),
        )
        for channel_id, items in sorted(grouped.items())
    )
    return EconomicProjectionBundle(
        model=plan.model,
        economic_generation=activation.economic_generation,
        input_hash=plan.input_hash,
        output_hash=plan.output_hash,
        evaluated_at=plan.evaluated_at,
        channels=channels,
    )


class _PostgresEconomicInputStore:
    """Read one repeatable-read snapshot of every economic input."""

    def __init__(self, connection_factory: Callable[[], Any]) -> None:
        if not callable(connection_factory):
            raise EconomicRoutingError("economic_connection_factory_invalid")
        self.connection_factory = connection_factory

    def load_plan_inputs(
        self,
        model: str,
    ) -> tuple[
        tuple[EconomicSource, ...],
        Mapping[int, SourceCostContract],
        Mapping[int, SourceQuotaSnapshot],
        tuple[ChannelRevenueSnapshot, ...],
        A6GatewayCostSnapshot | None,
        Mapping[tuple[int, int], EconomicEligibility],
        int | None,
    ]:
        model = _text(model, "economic_model_invalid", 256)
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                cursor.execute(_ACTIVE_ECONOMIC_GENERATION_SQL, (model,))
                active_row = _row(cursor)
                parent_generation = (
                    None
                    if active_row is None
                    else _positive_int(
                        active_row.get("economic_generation"),
                        "economic_generation_invalid",
                    )
                )
                cursor.execute(_ECONOMIC_SOURCES_SQL, (model,))
                sources = tuple(_source_from_row(row) for row in _rows(cursor))
                cursor.execute(_ACTIVE_COST_CONTRACTS_SQL, (model,))
                contracts = {
                    item.source_id: item
                    for item in (_contract_from_row(row) for row in _rows(cursor))
                }
                cursor.execute(_LATEST_QUOTAS_SQL, (model,))
                quotas = {
                    item.source_id: item for item in (_quota_from_row(row) for row in _rows(cursor))
                }
                cursor.execute(_LATEST_REVENUES_SQL, (model,))
                revenues = tuple(_revenue_from_row(row) for row in _rows(cursor))
                cursor.execute(_ACTIVE_A6_GATEWAY_SQL, (model,))
                gateway_row = _row(cursor)
                gateway = _gateway_from_row(gateway_row) if gateway_row is not None else None
                previous: dict[tuple[int, int], EconomicEligibility] = {}
                if active_row is not None:
                    cursor.execute(
                        _ACTIVE_ELIGIBILITY_SQL,
                        (active_row["generation_id"],),
                    )
                    for row in _rows(cursor):
                        item = _eligibility_from_row(row)
                        previous[(item.channel_id, item.source_id)] = item
            connection.commit()
            return (
                sources,
                MappingProxyType(contracts),
                MappingProxyType(quotas),
                revenues,
                gateway,
                MappingProxyType(previous),
                parent_generation,
            )
        except Exception:
            connection.rollback()
            raise
        finally:
            _close(connection)


def _source_from_row(row: Mapping[str, Any]) -> EconomicSource:
    return EconomicSource(
        source_id=row.get("source_id"),
        source_key=row.get("source_key"),
        source_kind=row.get("source_kind"),
        origin_class=row.get("origin_class"),
        model=row.get("model"),
        endpoint_ref=row.get("endpoint_ref"),
        endpoint_fingerprint=row.get("endpoint_fingerprint"),
        enodot_account_id=row.get("enodot_account_id"),
        health_state=row.get("health_state"),
        health_observed_at=row.get("health_observed_at"),
        capacity_state=row.get("capacity_state"),
        capacity_observed_at=row.get("capacity_observed_at"),
        capacity_epoch=row.get("capacity_epoch"),
        configured_reserve_a=row.get("configured_reserve_a"),
        enabled=row.get("enabled"),
        config_version=row.get("config_version"),
    )


def _contract_from_row(row: Mapping[str, Any]) -> SourceCostContract:
    return SourceCostContract(
        contract_id=row.get("contract_id"),
        source_id=row.get("source_id"),
        cost_mode=row.get("cost_mode"),
        declared_k_cny=row.get("declared_k_cny"),
        cycle_cost_cny=row.get("cycle_cost_cny"),
        currency=row.get("currency"),
        cost_basis=row.get("cost_basis"),
        quota_basis=row.get("quota_basis"),
        effective_at=row.get("effective_at"),
        expires_at=row.get("expires_at"),
        config_version=row.get("config_version"),
        source_hash=row.get("source_hash"),
        contract_state=row.get("contract_state"),
    )


def _quota_from_row(row: Mapping[str, Any]) -> SourceQuotaSnapshot:
    return SourceQuotaSnapshot(
        snapshot_id=row.get("snapshot_id"),
        source_id=row.get("source_id"),
        contract_id=row.get("contract_id"),
        quota_epoch=row.get("quota_epoch"),
        used_a=row.get("used_a"),
        remaining_a=row.get("remaining_a"),
        total_a=row.get("total_a"),
        reset_at=row.get("reset_at"),
        observed_at=row.get("observed_at"),
        payload_hash=row.get("payload_hash"),
        snapshot_state=row.get("snapshot_state"),
    )


def _revenue_from_row(row: Mapping[str, Any]) -> ChannelRevenueSnapshot:
    return ChannelRevenueSnapshot(
        snapshot_id=row.get("snapshot_id"),
        model=row.get("model"),
        channel_id=row.get("channel_id"),
        enodot_api_key_id=row.get("enodot_api_key_id"),
        sub2api_group_id=row.get("sub2api_group_id"),
        revenue_epoch=row.get("revenue_epoch"),
        channel_net_k_cny=row.get("channel_net_k_cny"),
        settlement_contract_ref=row.get("settlement_contract_ref"),
        observed_at=row.get("observed_at"),
        payload_hash=row.get("payload_hash"),
        snapshot_state=row.get("snapshot_state"),
    )


def _gateway_from_row(row: Mapping[str, Any]) -> A6GatewayCostSnapshot | None:
    account_ids = _positive_int_tuple(row.get("ready_account_ids"))
    source_k = _optional_positive_decimal(row.get("source_k_cny"), "economic_a6_cost_invalid")
    if not account_ids or source_k is None:
        return None
    return A6GatewayCostSnapshot(
        model=row.get("model"),
        routing_generation=row.get("routing_generation"),
        cost_generation=row.get("cost_generation"),
        source_k_cny=source_k,
        cost_output_hash=row.get("cost_output_hash"),
        ready_account_ids=account_ids,
        activated_at=row.get("activated_at"),
    )


def _eligibility_from_row(row: Mapping[str, Any]) -> EconomicEligibility:
    return EconomicEligibility(
        model=str(row.get("model") or ""),
        channel_id=_positive_int(row.get("channel_id"), "economic_channel_id_invalid"),
        source_id=_positive_int(row.get("source_id"), "economic_source_id_invalid"),
        source_kind=str(row.get("source_kind") or ""),
        enodot_account_id=_optional_positive_int(
            row.get("enodot_account_id"), "economic_source_account_id_invalid"
        ),
        cost_contract_id=_optional_positive_int(
            row.get("cost_contract_id"), "economic_contract_id_invalid"
        ),
        quota_snapshot_id=_optional_positive_int(
            row.get("quota_snapshot_id"), "economic_quota_snapshot_id_invalid"
        ),
        revenue_snapshot_id=_positive_int(
            row.get("revenue_snapshot_id"), "economic_revenue_snapshot_id_invalid"
        ),
        quota_epoch=_optional_text(row.get("quota_epoch"), "economic_quota_epoch_invalid"),
        source_k_cny=_optional_positive_decimal(
            row.get("source_k_cny"), "economic_source_cost_invalid"
        ),
        channel_net_k_cny=_optional_positive_decimal(
            row.get("channel_net_k_cny"), "economic_revenue_value_invalid"
        ),
        minimum_channel_k_cny=_optional_positive_decimal(
            row.get("minimum_channel_k_cny"), "economic_minimum_revenue_invalid"
        ),
        profit_on_cost_bps=_optional_int(
            row.get("profit_on_cost_bps"), "economic_profit_bps_invalid"
        ),
        eligibility_state=str(row.get("eligibility_state") or ""),
        reason_code=str(row.get("reason_code") or ""),
        cost_snapshot_hash=_optional_hash(row.get("cost_snapshot_hash")),
        revenue_snapshot_hash=_optional_hash(row.get("revenue_snapshot_hash")),
        capacity_epoch=_optional_text(row.get("capacity_epoch"), "economic_capacity_epoch_invalid"),
        required_burn_a_per_second=_nonnegative_decimal(
            row.get("required_burn_a_per_second") or 0,
            "economic_required_burn_invalid",
        ),
        confirmation_count=_nonnegative_int(
            row.get("confirmation_count"), "economic_confirmation_count_invalid"
        ),
        a6_routing_generation=_optional_positive_int(
            row.get("a6_routing_generation"),
            "economic_a6_routing_generation_invalid",
        ),
        a6_cost_generation=_optional_positive_int(
            row.get("a6_cost_generation"), "economic_a6_cost_generation_invalid"
        ),
        enodot_api_key_id=_optional_positive_int(
            row.get("enodot_api_key_id"), "economic_revenue_api_key_invalid"
        ),
        sub2api_group_id=_optional_positive_int(
            row.get("sub2api_group_id"), "economic_revenue_group_invalid"
        ),
        evaluated_at=_aware_datetime(row.get("evaluated_at"), "economic_evaluation_time_invalid"),
    )


def _projected_source_from_row(
    row: Mapping[str, Any], account_ids: tuple[int, ...]
) -> ProjectedEconomicSource:
    return ProjectedEconomicSource(
        source_id=_positive_int(row.get("source_id"), "economic_source_id_invalid"),
        source_kind=str(row.get("source_kind") or ""),
        endpoint_fingerprint=str(row.get("endpoint_fingerprint") or ""),
        account_ids=account_ids,
        source_k_cny=row.get("source_k_cny"),
        channel_net_k_cny=row.get("channel_net_k_cny"),
        minimum_channel_k_cny=row.get("minimum_channel_k_cny"),
        profit_on_cost_bps=_int(row.get("profit_on_cost_bps"), "economic_profit_bps_invalid"),
        required_burn_a_per_second=row.get("required_burn_a_per_second") or 0,
        capacity_epoch=row.get("capacity_epoch"),
        cost_contract_id=_optional_positive_int(
            row.get("cost_contract_id"), "economic_contract_id_invalid"
        ),
        quota_epoch=row.get("quota_epoch"),
        cost_snapshot_hash=str(row.get("cost_snapshot_hash") or ""),
        a6_routing_generation=_optional_positive_int(
            row.get("a6_routing_generation"),
            "economic_a6_routing_generation_invalid",
        ),
        a6_cost_generation=_optional_positive_int(
            row.get("a6_cost_generation"), "economic_a6_cost_generation_invalid"
        ),
    )


def _eligibility_insert_params(
    record: EconomicEligibility,
    generation_id: int,
    economic_generation: int,
) -> tuple[Any, ...]:
    return (
        generation_id,
        record.model,
        record.channel_id,
        record.source_id,
        economic_generation,
        record.source_kind,
        record.enodot_account_id,
        record.cost_contract_id,
        record.quota_snapshot_id,
        record.revenue_snapshot_id,
        record.quota_epoch,
        record.source_k_cny,
        record.channel_net_k_cny,
        record.minimum_channel_k_cny,
        record.profit_on_cost_bps,
        record.eligibility_state,
        record.reason_code,
        record.cost_snapshot_hash,
        record.revenue_snapshot_hash,
        record.capacity_epoch,
        record.required_burn_a_per_second,
        record.confirmation_count,
        record.a6_routing_generation,
        record.a6_cost_generation,
        record.evaluated_at,
    )


_ACTIVE_ECONOMIC_GENERATION_SQL = """
SELECT generation_id, model, economic_generation, input_hash, output_hash,
       plan_only, minimum_profit_on_cost_bps, config_version,
       evaluated_at, activated_at
FROM a6_trade.sub2api_economic_generations
WHERE model = %s AND generation_state = 'active'
"""


_ACTIVE_ECONOMIC_GENERATION_FOR_UPDATE_SQL = (
    _ACTIVE_ECONOMIC_GENERATION_SQL.rstrip() + "\nFOR UPDATE\n"
)


_ECONOMIC_SOURCES_SQL = """
SELECT source_id, source_key, enodot_account_id, source_kind, origin_class,
       model, endpoint_ref, endpoint_fingerprint, health_state,
       health_observed_at, capacity_state, capacity_observed_at,
       capacity_epoch, configured_reserve_a, enabled, config_version
FROM a6_trade.sub2api_economic_sources
WHERE model = %s
ORDER BY source_id
"""


_ACTIVE_COST_CONTRACTS_SQL = """
SELECT contract.contract_id, contract.source_id, contract.cost_mode,
       contract.cycle_cost_cny, contract.declared_k_cny, contract.currency,
       contract.cost_basis, contract.quota_basis, contract.effective_at,
       contract.expires_at, contract.config_version, contract.source_hash,
       contract.contract_state
FROM a6_trade.sub2api_source_cost_contracts contract
JOIN a6_trade.sub2api_economic_sources source
  ON source.source_id = contract.source_id
WHERE source.model = %s AND contract.contract_state = 'active'
ORDER BY contract.source_id
"""


_LATEST_QUOTAS_SQL = """
SELECT DISTINCT ON (snapshot.source_id)
       snapshot.snapshot_id, snapshot.source_id, snapshot.contract_id,
       snapshot.quota_epoch, snapshot.used_a, snapshot.remaining_a,
       snapshot.total_a, snapshot.reset_at, snapshot.observed_at,
       snapshot.payload_hash, snapshot.snapshot_state
FROM a6_trade.sub2api_source_quota_snapshots snapshot
JOIN a6_trade.sub2api_economic_sources source
  ON source.source_id = snapshot.source_id
WHERE source.model = %s
ORDER BY snapshot.source_id, snapshot.observed_at DESC, snapshot.snapshot_id DESC
"""


_LATEST_REVENUES_SQL = """
SELECT DISTINCT ON (channel_id)
       snapshot_id, model, channel_id, enodot_api_key_id, sub2api_group_id,
       revenue_epoch, channel_net_k_cny, settlement_contract_ref,
       observed_at, payload_hash, snapshot_state
FROM a6_trade.sub2api_channel_revenue_snapshots
WHERE model = %s
ORDER BY channel_id, observed_at DESC, snapshot_id DESC
"""


_ACTIVE_A6_GATEWAY_SQL = """
SELECT generation.model,
       generation.generation AS routing_generation,
       generation.cost_generation,
       cost.pool_cost_multiplier AS source_k_cny,
       cost.cost_output_hash,
       greatest(generation.activated_at, cost.activated_at) AS activated_at,
       array_agg(DISTINCT slot.enodot_account_id ORDER BY slot.enodot_account_id)
           AS ready_account_ids
FROM a6_trade.provider_pool_generations generation
JOIN a6_trade.provider_cost_generations cost
  ON cost.id = generation.cost_generation_id
 AND cost.model = generation.model
 AND cost.cost_generation = generation.cost_generation
JOIN a6_trade.provider_pool_slots slot
  ON slot.generation_id = generation.id
 AND slot.model = generation.model
 AND slot.routing_generation = generation.generation
 AND slot.cost_generation = generation.cost_generation
 AND slot.valid_to IS NULL
JOIN a6_trade.a6_tokens token ON token.id = slot.a6_token_id
JOIN a6_trade.enodot_account_bindings binding
  ON binding.id = slot.enodot_binding_id
 AND binding.a6_token_id = slot.a6_token_id
 AND binding.enodot_account_id = slot.enodot_account_id
JOIN a6_trade.a6_pin_bindings pin
  ON pin.id = slot.confirmed_pin_binding_id
 AND pin.model = slot.model
 AND pin.a6_token_id = slot.a6_token_id
 AND pin.channel_id = slot.desired_channel_id
 AND pin.valid_to IS NULL
 AND pin.confirmed_at IS NOT NULL
WHERE generation.model = %s
  AND generation.generation_state = 'active'
  AND cost.pricing_state = 'active'
  AND cost.pool_cost_multiplier > 0
  AND slot.slot_state = 'READY'
  AND slot.funding_eligible
  AND slot.desired_schedulable AND slot.observed_schedulable
  AND slot.pin_confirmed AND slot.probe_confirmed
  AND slot.desired_priority >= 2
  AND slot.desired_priority = slot.desired_rank + 1
  AND slot.desired_slot_cost_multiplier = slot.observed_rate_multiplier
  AND slot.liquidity_state IN ('FUNDED', 'REFILLING', 'TRANSFER_UNKNOWN')
  AND lower(token.status) IN ('1', 'active', 'enabled')
  AND lower(binding.state) = 'bound'
GROUP BY generation.model, generation.generation, generation.cost_generation,
         cost.pool_cost_multiplier, cost.cost_output_hash,
         generation.activated_at, cost.activated_at
"""


_ACTIVE_ELIGIBILITY_SQL = """
SELECT eligibility.*, revenue.enodot_api_key_id, revenue.sub2api_group_id
FROM a6_trade.sub2api_channel_source_eligibility eligibility
JOIN a6_trade.sub2api_channel_revenue_snapshots revenue
  ON revenue.snapshot_id = eligibility.revenue_snapshot_id
WHERE eligibility.generation_id = %s
ORDER BY eligibility.channel_id, eligibility.source_id
"""


_NEXT_ECONOMIC_GENERATION_SQL = """
SELECT coalesce(max(economic_generation), 0) + 1 AS economic_generation
FROM a6_trade.sub2api_economic_generations
WHERE model = %s
"""


_INSERT_ECONOMIC_GENERATION_SQL = """
INSERT INTO a6_trade.sub2api_economic_generations (
    model, economic_generation, parent_generation, input_hash, output_hash,
    generation_state, plan_only, minimum_profit_on_cost_bps,
    quota_snapshot_max_age_seconds, channel_revenue_max_age_seconds,
    health_max_age_seconds, config_version, evaluated_at, activated_at
) VALUES (
    %s, %s, %s, %s, %s, 'active', %s, %s, %s, %s, %s, %s, %s, %s
)
RETURNING generation_id
"""


_INSERT_ECONOMIC_ELIGIBILITY_SQL = """
INSERT INTO a6_trade.sub2api_channel_source_eligibility (
    generation_id, model, channel_id, source_id, economic_generation,
    source_kind, enodot_account_id, cost_contract_id, quota_snapshot_id,
    revenue_snapshot_id, quota_epoch, source_k_cny, channel_net_k_cny,
    minimum_channel_k_cny, profit_on_cost_bps, eligibility_state, reason_code,
    cost_snapshot_hash, revenue_snapshot_hash, capacity_epoch,
    required_burn_a_per_second, confirmation_count,
    a6_routing_generation, a6_cost_generation, evaluated_at
) VALUES (
    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
    %s, %s, %s, %s, %s
)
"""


_ACTIVE_PROJECTABLE_ELIGIBILITY_SQL = """
SELECT eligibility.channel_id, eligibility.source_id, eligibility.source_kind,
       eligibility.enodot_account_id, eligibility.cost_contract_id,
       eligibility.quota_epoch, eligibility.source_k_cny,
       eligibility.channel_net_k_cny, eligibility.minimum_channel_k_cny,
       eligibility.profit_on_cost_bps, eligibility.cost_snapshot_hash,
       eligibility.capacity_epoch, eligibility.required_burn_a_per_second,
       eligibility.a6_routing_generation, eligibility.a6_cost_generation,
       eligibility.revenue_snapshot_id,
       source.endpoint_fingerprint,
       revenue.payload_hash AS revenue_snapshot_hash,
       revenue.enodot_api_key_id, revenue.sub2api_group_id
FROM a6_trade.sub2api_channel_source_eligibility eligibility
JOIN a6_trade.sub2api_economic_sources source
  ON source.source_id = eligibility.source_id
JOIN a6_trade.sub2api_channel_revenue_snapshots revenue
  ON revenue.snapshot_id = eligibility.revenue_snapshot_id
WHERE eligibility.generation_id = %s
  AND eligibility.eligibility_state IN ('ELIGIBLE', 'THROTTLED')
ORDER BY eligibility.channel_id,
         CASE WHEN eligibility.source_kind = 'account'
                   AND eligibility.required_burn_a_per_second > 0 THEN 0 ELSE 1 END,
         eligibility.required_burn_a_per_second DESC,
         eligibility.source_k_cny,
         eligibility.source_id
"""


_A6_READY_ACCOUNT_IDS_SQL = """
SELECT array_agg(slot.enodot_account_id ORDER BY slot.desired_priority, slot.slot_no)
           AS ready_account_ids
FROM a6_trade.provider_pool_slots slot
JOIN a6_trade.a6_tokens token ON token.id = slot.a6_token_id
JOIN a6_trade.enodot_account_bindings binding
  ON binding.id = slot.enodot_binding_id
 AND binding.a6_token_id = slot.a6_token_id
 AND binding.enodot_account_id = slot.enodot_account_id
JOIN a6_trade.a6_pin_bindings pin
  ON pin.id = slot.confirmed_pin_binding_id
 AND pin.model = slot.model
 AND pin.a6_token_id = slot.a6_token_id
 AND pin.channel_id = slot.desired_channel_id
 AND pin.valid_to IS NULL
 AND pin.confirmed_at IS NOT NULL
WHERE slot.model = %s AND slot.routing_generation = %s AND slot.cost_generation = %s
  AND slot.valid_to IS NULL AND slot.slot_state = 'READY'
  AND slot.funding_eligible
  AND slot.desired_schedulable AND slot.observed_schedulable
  AND slot.pin_confirmed AND slot.probe_confirmed
  AND slot.desired_priority >= 2
  AND slot.desired_priority = slot.desired_rank + 1
  AND slot.desired_slot_cost_multiplier = slot.observed_rate_multiplier
  AND slot.liquidity_state IN ('FUNDED', 'REFILLING', 'TRANSFER_UNKNOWN')
  AND lower(token.status) IN ('1', 'active', 'enabled')
  AND lower(binding.state) = 'bound'
"""


_UPSERT_ECONOMIC_PROJECTION_RECEIPT_SQL = """
INSERT INTO a6_trade.sub2api_economic_projection_receipts (
    model, economic_generation, projection_digest, redis_key,
    projection_state, projected_at, last_error_class, updated_at
) VALUES (%s, %s, %s, %s, %s, %s, %s, now())
ON CONFLICT (model) DO UPDATE
SET economic_generation = EXCLUDED.economic_generation,
    projection_digest = EXCLUDED.projection_digest,
    redis_key = EXCLUDED.redis_key,
    projection_state = EXCLUDED.projection_state,
    projected_at = EXCLUDED.projected_at,
    last_error_class = EXCLUDED.last_error_class,
    updated_at = now()
WHERE a6_trade.sub2api_economic_projection_receipts.economic_generation
      <= EXCLUDED.economic_generation
"""


class PostgresEconomicStore(_PostgresEconomicInputStore):
    """Activate complete generations and rebuild their request-plane projection."""

    def build_plan(
        self,
        model: str,
        *,
        config: EconomicRoutingConfig,
        now: datetime | None = None,
    ) -> tuple[EconomicPlan, tuple[EconomicSource, ...], A6GatewayCostSnapshot | None]:
        evaluated_at = _aware_datetime(
            now or datetime.now(timezone.utc), "economic_evaluation_time_invalid"
        )
        (
            sources,
            contracts,
            quotas,
            revenues,
            gateway,
            previous,
            parent,
        ) = self.load_plan_inputs(model)
        plan = build_economic_plan(
            model,
            sources,
            contracts,
            quotas,
            revenues,
            a6_gateway=gateway,
            previous_records=previous,
            parent_generation=parent,
            now=evaluated_at,
            config=config,
        )
        return plan, sources, gateway

    def activate(self, plan: EconomicPlan) -> EconomicGenerationActivation:
        if not isinstance(plan, EconomicPlan):
            raise EconomicRoutingError("economic_plan_invalid")
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                    (f"a6-economic:{plan.model}",),
                )
                cursor.execute(_ACTIVE_ECONOMIC_GENERATION_FOR_UPDATE_SQL, (plan.model,))
                active = _row(cursor)
                active_generation = (
                    None
                    if active is None
                    else _positive_int(
                        active.get("economic_generation"),
                        "economic_generation_invalid",
                    )
                )
                if active_generation != plan.parent_generation:
                    raise EconomicRoutingError("economic_plan_parent_superseded")
                if active is not None and (
                    str(active.get("input_hash") or "").casefold() == plan.input_hash
                    and str(active.get("output_hash") or "").casefold() == plan.output_hash
                    and active.get("plan_only") is plan.config.plan_only
                    and int(active.get("minimum_profit_on_cost_bps") or -1)
                    == plan.config.minimum_profit_on_cost_bps
                    and str(active.get("config_version") or "") == plan.config.config_version
                ):
                    connection.commit()
                    return EconomicGenerationActivation(
                        model=plan.model,
                        economic_generation=active_generation or 0,
                        generation_id=_positive_int(
                            active.get("generation_id"), "economic_generation_id_invalid"
                        ),
                        input_hash=plan.input_hash,
                        output_hash=plan.output_hash,
                        changed=False,
                        plan_only=plan.config.plan_only,
                        activated_at=_aware_datetime(
                            active.get("activated_at"), "economic_generation_time_invalid"
                        ),
                    )
                cursor.execute(_NEXT_ECONOMIC_GENERATION_SQL, (plan.model,))
                next_row = _required_row(cursor, "economic_generation_allocate_failed")
                generation = _positive_int(
                    next_row.get("economic_generation"), "economic_generation_invalid"
                )
                if active is not None:
                    cursor.execute(
                        """
                        UPDATE a6_trade.sub2api_economic_generations
                        SET generation_state = 'superseded'
                        WHERE generation_id = %s AND generation_state = 'active'
                        """,
                        (active["generation_id"],),
                    )
                    if cursor.rowcount != 1:
                        raise EconomicRoutingError("economic_generation_supersede_failed")
                cursor.execute(
                    _INSERT_ECONOMIC_GENERATION_SQL,
                    (
                        plan.model,
                        generation,
                        plan.parent_generation,
                        plan.input_hash,
                        plan.output_hash,
                        plan.config.plan_only,
                        plan.config.minimum_profit_on_cost_bps,
                        plan.config.quota_snapshot_max_age_seconds,
                        plan.config.channel_revenue_max_age_seconds,
                        plan.config.health_max_age_seconds,
                        plan.config.config_version,
                        plan.evaluated_at,
                        plan.evaluated_at,
                    ),
                )
                inserted = _required_row(cursor, "economic_generation_insert_failed")
                generation_id = _positive_int(
                    inserted.get("generation_id"), "economic_generation_id_invalid"
                )
                for record in plan.records:
                    cursor.execute(
                        _INSERT_ECONOMIC_ELIGIBILITY_SQL,
                        _eligibility_insert_params(record, generation_id, generation),
                    )
                cursor.execute(
                    "SELECT pg_notify(%s, %s)",
                    (
                        "a6_economic_generation_activated",
                        _canonical_json(
                            {
                                "model": plan.model,
                                "economic_generation": generation,
                                "output_hash": plan.output_hash,
                                "plan_only": plan.config.plan_only,
                            }
                        ),
                    ),
                )
            connection.commit()
            return EconomicGenerationActivation(
                model=plan.model,
                economic_generation=generation,
                generation_id=generation_id,
                input_hash=plan.input_hash,
                output_hash=plan.output_hash,
                changed=True,
                plan_only=plan.config.plan_only,
                activated_at=plan.evaluated_at,
            )
        except Exception:
            connection.rollback()
            raise
        finally:
            _close(connection)

    def load_active_projection(self, model: str) -> tuple[EconomicProjectionBundle, bool] | None:
        model = _text(model, "economic_model_invalid", 256)
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                cursor.execute(_ACTIVE_ECONOMIC_GENERATION_SQL, (model,))
                active = _row(cursor)
                if active is None:
                    connection.commit()
                    return None
                cursor.execute(_ACTIVE_PROJECTABLE_ELIGIBILITY_SQL, (active["generation_id"],))
                rows = _rows(cursor)
                gateway_accounts: dict[tuple[int, int], tuple[int, ...]] = {}
                for row in rows:
                    if row.get("source_kind") != "a6_gateway":
                        continue
                    key = (
                        _positive_int(
                            row.get("a6_routing_generation"),
                            "economic_a6_routing_generation_invalid",
                        ),
                        _positive_int(
                            row.get("a6_cost_generation"),
                            "economic_a6_cost_generation_invalid",
                        ),
                    )
                    if key not in gateway_accounts:
                        cursor.execute(
                            _A6_READY_ACCOUNT_IDS_SQL,
                            (model, key[0], key[1]),
                        )
                        ready = _row(cursor)
                        account_values = () if ready is None else ready.get("ready_account_ids")
                        gateway_accounts[key] = _positive_int_tuple(account_values)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            _close(connection)

        grouped: dict[int, list[ProjectedEconomicSource]] = {}
        channel_identity: dict[int, tuple[int, str, int | None, int | None]] = {}
        for row in rows:
            source_kind = str(row.get("source_kind") or "")
            if source_kind == "a6_gateway":
                account_ids = gateway_accounts[
                    (int(row["a6_routing_generation"]), int(row["a6_cost_generation"]))
                ]
            else:
                account_ids = (
                    _positive_int(
                        row.get("enodot_account_id"),
                        "economic_projection_account_missing",
                    ),
                )
            projected = _projected_source_from_row(row, account_ids)
            channel_id = _positive_int(row.get("channel_id"), "economic_channel_id_invalid")
            grouped.setdefault(channel_id, []).append(projected)
            identity = (
                _positive_int(
                    row.get("revenue_snapshot_id"),
                    "economic_revenue_snapshot_id_invalid",
                ),
                str(row.get("revenue_snapshot_hash") or "").casefold(),
                _optional_positive_int(
                    row.get("enodot_api_key_id"), "economic_revenue_api_key_invalid"
                ),
                _optional_positive_int(
                    row.get("sub2api_group_id"), "economic_revenue_group_invalid"
                ),
            )
            if channel_id in channel_identity and channel_identity[channel_id] != identity:
                raise EconomicRoutingError("economic_projection_revenue_identity_mismatch")
            channel_identity[channel_id] = identity
        channels = tuple(
            EconomicChannelProjection(
                channel_id=channel_id,
                revenue_snapshot_id=channel_identity[channel_id][0],
                revenue_snapshot_hash=channel_identity[channel_id][1],
                enodot_api_key_id=channel_identity[channel_id][2],
                sub2api_group_id=channel_identity[channel_id][3],
                sources=tuple(items),
            )
            for channel_id, items in sorted(grouped.items())
        )
        bundle = EconomicProjectionBundle(
            model=model,
            economic_generation=_positive_int(
                active.get("economic_generation"), "economic_generation_invalid"
            ),
            input_hash=str(active.get("input_hash") or ""),
            output_hash=str(active.get("output_hash") or ""),
            evaluated_at=_aware_datetime(
                active.get("evaluated_at"), "economic_generation_time_invalid"
            ),
            channels=channels,
        )
        return bundle, bool(active.get("plan_only"))

    def record_projection_receipt(
        self,
        bundle: EconomicProjectionBundle,
        *,
        redis_key: str,
        projection_state: str,
        last_error_class: str | None = None,
        projected_at: datetime | None = None,
    ) -> None:
        state = str(projection_state).strip().casefold()
        if state not in {"plan_only", "projected", "withdrawn", "error"}:
            raise EconomicRoutingError("economic_projection_receipt_state_invalid")
        error = _optional_text(last_error_class, "economic_projection_error_class_invalid", 256)
        observed_at = (
            None
            if projected_at is None
            else _aware_datetime(projected_at, "economic_projection_time_invalid")
        )
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    _UPSERT_ECONOMIC_PROJECTION_RECEIPT_SQL,
                    (
                        bundle.model,
                        bundle.economic_generation,
                        bundle.digest(),
                        redis_key,
                        state,
                        observed_at,
                        error,
                    ),
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            _close(connection)


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )


def _hash_document(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode()).hexdigest()


def _sha256(value: object) -> bool:
    text = str(value or "")
    return len(text) == 64 and all(character in "0123456789abcdefABCDEF" for character in text)


def _text(value: object, code: str, maximum: int = 512) -> str:
    text = str(value or "").strip()
    if not text or len(text) > maximum or any(ord(character) < 0x20 for character in text):
        raise EconomicRoutingError(code)
    return text


def _optional_text(value: object, code: str, maximum: int = 512) -> str | None:
    if value in (None, ""):
        return None
    return _text(value, code, maximum)


def _label(value: object, code: str, maximum: int = 128) -> str:
    text = _text(value, code, maximum)
    if not text[0].isalnum() or any(
        not (character.isalnum() or character in "._:-") for character in text
    ):
        raise EconomicRoutingError(code)
    return text


def _redis_name(value: object, code: str) -> str:
    text = _text(value, code, 512)
    if any(character.isspace() for character in text):
        raise EconomicRoutingError(code)
    return text


def _decimal(value: object, code: str) -> Decimal:
    if isinstance(value, bool):
        raise EconomicRoutingError(code)
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise EconomicRoutingError(code) from None
    if not result.is_finite():
        raise EconomicRoutingError(code)
    return result


def _positive_decimal(value: object, code: str) -> Decimal:
    result = _decimal(value, code)
    if result <= 0:
        raise EconomicRoutingError(code)
    return result


def _optional_positive_decimal(value: object, code: str) -> Decimal | None:
    if value in (None, ""):
        return None
    return _positive_decimal(value, code)


def _nonnegative_decimal(value: object, code: str) -> Decimal:
    result = _decimal(value, code)
    if result < 0:
        raise EconomicRoutingError(code)
    return result


def _decimal_text(value: Decimal | None) -> str | None:
    return None if value is None else format(value, "f")


def _int(value: object, code: str) -> int:
    if isinstance(value, bool):
        raise EconomicRoutingError(code)
    try:
        return int(value)
    except (TypeError, ValueError):
        raise EconomicRoutingError(code) from None


def _optional_int(value: object, code: str) -> int | None:
    if value in (None, ""):
        return None
    return _int(value, code)


def _positive_int(value: object, code: str) -> int:
    result = _int(value, code)
    if result < 1:
        raise EconomicRoutingError(code)
    return result


def _optional_positive_int(value: object, code: str) -> int | None:
    if value in (None, ""):
        return None
    return _positive_int(value, code)


def _nonnegative_int(value: object, code: str) -> int:
    result = _int(value, code)
    if result < 0:
        raise EconomicRoutingError(code)
    return result


def _positive_int_tuple(value: object) -> tuple[int, ...]:
    if value in (None, "", (), []):
        return ()
    values: Iterable[object]
    if isinstance(value, str):
        stripped = value.strip().removeprefix("{").removesuffix("}")
        values = () if not stripped else stripped.split(",")
    elif isinstance(value, Iterable):
        values = value
    else:
        raise EconomicRoutingError("economic_account_set_invalid")
    return tuple(
        sorted({_positive_int(item, "economic_projection_account_id_invalid") for item in values})
    )


def _optional_hash(value: object) -> str | None:
    if value in (None, ""):
        return None
    text = str(value).strip().casefold()
    if not _sha256(text):
        raise EconomicRoutingError("economic_hash_invalid")
    return text


def _aware_datetime(value: object, code: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise EconomicRoutingError(code)
    return value.astimezone(timezone.utc)


def _optional_aware_datetime(value: object, code: str) -> datetime | None:
    if value is None:
        return None
    return _aware_datetime(value, code)


def _datetime_text(value: datetime | None) -> str | None:
    if value is None:
        return None
    return _aware_datetime(value, "economic_datetime_invalid").isoformat()


def _age_seconds(now: datetime, observed_at: datetime) -> Decimal:
    return Decimal(str((now - observed_at).total_seconds()))


def _row(cursor: Any) -> dict[str, Any] | None:
    value = cursor.fetchone()
    if value is None:
        return None
    if isinstance(value, Mapping):
        return dict(value)
    names = [column.name for column in cursor.description or ()]
    return dict(zip(names, value, strict=False))


def _required_row(cursor: Any, code: str) -> dict[str, Any]:
    value = _row(cursor)
    if value is None:
        raise EconomicRoutingError(code)
    return value


def _rows(cursor: Any) -> list[dict[str, Any]]:
    values = cursor.fetchall()
    if not values:
        return []
    if isinstance(values[0], Mapping):
        return [dict(value) for value in values]
    names = [column.name for column in cursor.description or ()]
    return [dict(zip(names, value, strict=False)) for value in values]


def _close(connection: Any) -> None:
    close = getattr(connection, "close", None)
    if callable(close):
        close()
