"""Provider routing diff and monotonic protected-cost planning."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal, InvalidOperation
from enum import Enum
from types import MappingProxyType
from typing import Any

# Enodot persists account and billing multipliers as DECIMAL(10,4). Planning
# at a finer precision lets PostgreSQL round a remote write below the protected
# cost, so every Provider cost and floor must be rounded up to the remote step.
DEFAULT_COST_QUANTUM = Decimal("0.0001")


class ProviderCostError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class PoolDiffKind(str, Enum):
    NOOP = "NOOP"
    COST_ONLY = "COST_ONLY"
    PRIORITY_AND_COST = "PRIORITY_AND_COST"
    MEMBERSHIP_AND_COST = "MEMBERSHIP_AND_COST"
    LIQUIDITY_REPLACEMENT = "LIQUIDITY_REPLACEMENT"


@dataclass(frozen=True)
class RoutingView:
    ordered_channel_ids: tuple[int, ...]
    assignments: Mapping[int, str | int | None]
    cost_input_hash: str

    def __post_init__(self) -> None:
        channels = tuple(
            _positive_int(item, "provider_routing_channel_invalid")
            for item in self.ordered_channel_ids
        )
        if len(channels) != len(set(channels)):
            raise ProviderCostError("provider_routing_channel_duplicate")
        assignments: dict[int, str | int | None] = {}
        for raw_slot, value in self.assignments.items():
            slot = _positive_int(raw_slot, "provider_routing_slot_invalid")
            assignments[slot] = value
        if not _sha256(self.cost_input_hash):
            raise ProviderCostError("provider_routing_cost_hash_invalid")
        object.__setattr__(self, "ordered_channel_ids", channels)
        object.__setattr__(self, "assignments", MappingProxyType(assignments))


@dataclass(frozen=True)
class CostPlan:
    desired_slot_costs: Mapping[int, Decimal]
    route_cost_multiplier: Decimal
    retry_reserve_multiplier: Decimal
    realized_cost_p95_multiplier: Decimal | None
    pool_cost_multiplier: Decimal
    billing_floor_multiplier: Decimal
    transition_floor_multiplier: Decimal
    margin_bps: int
    headroom_bps: int
    quantum: Decimal
    cost_output_hash: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "desired_slot_costs",
            MappingProxyType(dict(sorted(self.desired_slot_costs.items()))),
        )

    def public(self) -> dict[str, Any]:
        return {
            "desired_slot_costs": {
                str(slot): format(value, "f") for slot, value in self.desired_slot_costs.items()
            },
            "route_cost_multiplier": format(self.route_cost_multiplier, "f"),
            "retry_reserve_multiplier": format(self.retry_reserve_multiplier, "f"),
            "realized_cost_p95_multiplier": (
                None
                if self.realized_cost_p95_multiplier is None
                else format(self.realized_cost_p95_multiplier, "f")
            ),
            "pool_cost_multiplier": format(self.pool_cost_multiplier, "f"),
            "billing_floor_multiplier": format(self.billing_floor_multiplier, "f"),
            "transition_floor_multiplier": format(self.transition_floor_multiplier, "f"),
            "margin_bps": self.margin_bps,
            "headroom_bps": self.headroom_bps,
            "quantum": format(self.quantum, "f"),
            "cost_output_hash": self.cost_output_hash,
        }


def classify_pool_diff(current: RoutingView, desired: RoutingView) -> PoolDiffKind:
    """Classify the three-dimensional routing/assignment/cost diff."""

    old = current.ordered_channel_ids
    new = desired.ordered_channel_ids
    if len(old) != len(new) or set(old) != set(new):
        return PoolDiffKind.MEMBERSHIP_AND_COST
    assignment_changed = any(
        current.assignments.get(slot) != desired.assignments.get(slot)
        for slot in set(current.assignments) | set(desired.assignments)
    )
    if assignment_changed:
        return PoolDiffKind.LIQUIDITY_REPLACEMENT
    if old == new:
        if current.cost_input_hash == desired.cost_input_hash:
            return PoolDiffKind.NOOP
        return PoolDiffKind.COST_ONLY
    return PoolDiffKind.PRIORITY_AND_COST


def ceil_to_step(value: Decimal | str | int, step: Decimal | str = DEFAULT_COST_QUANTUM) -> Decimal:
    amount = _nonnegative_decimal(value, "provider_cost_value_invalid")
    quantum = _positive_decimal(step, "provider_cost_quantum_invalid")
    return (amount / quantum).to_integral_value(rounding=ROUND_CEILING) * quantum


def normalize_slot_cost(
    channel_cost: Decimal | str | int,
    *,
    exchange_factor: Decimal | str | int = Decimal("1"),
    fee_factor: Decimal | str | int = Decimal("1"),
    quantum: Decimal | str = DEFAULT_COST_QUANTUM,
) -> Decimal:
    cost = _nonnegative_decimal(channel_cost, "provider_channel_cost_invalid")
    exchange = _positive_decimal(exchange_factor, "provider_exchange_factor_invalid")
    fee = _positive_decimal(fee_factor, "provider_fee_factor_invalid")
    return ceil_to_step(cost * exchange * fee, quantum)


def build_cost_plan(
    desired_slot_costs: Mapping[int, Decimal | str | int],
    *,
    retry_reserve_multiplier: Decimal | str | int | None,
    bootstrap_retry_reserve: Decimal | str | int,
    realized_cost_p95_multiplier: Decimal | str | int | None,
    margin_bps: int = 1000,
    headroom_bps: int = 500,
    previous_billing_floor: Decimal | str | int = Decimal("0"),
    conservative_fallback_cost: Decimal | str | int = Decimal("0"),
    quantum: Decimal | str = DEFAULT_COST_QUANTUM,
) -> CostPlan:
    """Build a deterministic floor that covers any READY slot and retry reserve.

    An empty READY set retains the previous confirmed billing floor.  It is therefore
    suitable for an unpublished generation, but callers must not dispatch it.
    """

    if isinstance(margin_bps, bool) or not 0 <= margin_bps <= 100_000:
        raise ProviderCostError("provider_cost_margin_invalid")
    if isinstance(headroom_bps, bool) or not 0 <= headroom_bps <= 100_000:
        raise ProviderCostError("provider_cost_headroom_invalid")
    step = _positive_decimal(quantum, "provider_cost_quantum_invalid")
    slot_costs: dict[int, Decimal] = {}
    for raw_slot, raw_cost in desired_slot_costs.items():
        slot = _positive_int(raw_slot, "provider_cost_slot_invalid")
        if slot in slot_costs:
            raise ProviderCostError("provider_cost_slot_duplicate")
        slot_costs[slot] = ceil_to_step(raw_cost, step)

    bootstrap = _nonnegative_decimal(
        bootstrap_retry_reserve, "provider_cost_bootstrap_retry_invalid"
    )
    retry = (
        bootstrap
        if retry_reserve_multiplier is None
        else _nonnegative_decimal(
            retry_reserve_multiplier, "provider_cost_retry_reserve_invalid"
        )
    )
    realized = (
        None
        if realized_cost_p95_multiplier is None
        else _nonnegative_decimal(
            realized_cost_p95_multiplier, "provider_cost_realized_invalid"
        )
    )
    previous_floor = _nonnegative_decimal(
        previous_billing_floor, "provider_cost_previous_floor_invalid"
    )
    fallback = _nonnegative_decimal(
        conservative_fallback_cost, "provider_cost_fallback_invalid"
    )

    route = max(slot_costs.values(), default=Decimal("0"))
    route = max(route, fallback if slot_costs else Decimal("0"))
    protected = route + retry
    if realized is not None:
        protected = max(protected, realized)
    protected = ceil_to_step(protected, step)
    margin_factor = Decimal(1) + Decimal(margin_bps) / Decimal(10_000)
    headroom_factor = Decimal(1) + Decimal(headroom_bps) / Decimal(10_000)
    computed_floor = ceil_to_step(protected * margin_factor * headroom_factor, step)
    billing_floor = computed_floor if slot_costs else max(previous_floor, computed_floor)
    transition_floor = max(previous_floor, billing_floor)

    semantic = {
        "desired_slot_costs": {
            str(slot): format(value, "f") for slot, value in sorted(slot_costs.items())
        },
        "route_cost_multiplier": format(route, "f"),
        "retry_reserve_multiplier": format(retry, "f"),
        "realized_cost_p95_multiplier": None if realized is None else format(realized, "f"),
        "pool_cost_multiplier": format(protected, "f"),
        "billing_floor_multiplier": format(billing_floor, "f"),
        "transition_floor_multiplier": format(transition_floor, "f"),
        "margin_bps": margin_bps,
        "headroom_bps": headroom_bps,
        "quantum": format(step, "f"),
    }
    output_hash = hashlib.sha256(
        json.dumps(semantic, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return CostPlan(
        desired_slot_costs=slot_costs,
        route_cost_multiplier=route,
        retry_reserve_multiplier=retry,
        realized_cost_p95_multiplier=realized,
        pool_cost_multiplier=protected,
        billing_floor_multiplier=billing_floor,
        transition_floor_multiplier=transition_floor,
        margin_bps=margin_bps,
        headroom_bps=headroom_bps,
        quantum=step,
        cost_output_hash=output_hash,
    )


def _positive_int(value: object, code: str) -> int:
    if isinstance(value, bool):
        raise ProviderCostError(code)
    try:
        result = int(value)
    except (TypeError, ValueError):
        raise ProviderCostError(code) from None
    if result < 1:
        raise ProviderCostError(code)
    return result


def _decimal(value: object, code: str) -> Decimal:
    if isinstance(value, bool):
        raise ProviderCostError(code)
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ProviderCostError(code) from None
    if not result.is_finite():
        raise ProviderCostError(code)
    return result


def _nonnegative_decimal(value: object, code: str) -> Decimal:
    result = _decimal(value, code)
    if result < 0:
        raise ProviderCostError(code)
    return result


def _positive_decimal(value: object, code: str) -> Decimal:
    result = _decimal(value, code)
    if result <= 0:
        raise ProviderCostError(code)
    return result


def _sha256(value: object) -> bool:
    text = str(value)
    return len(text) == 64 and all(char in "0123456789abcdefABCDEF" for char in text)
