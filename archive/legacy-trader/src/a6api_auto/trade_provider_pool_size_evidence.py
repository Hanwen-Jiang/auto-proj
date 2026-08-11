"""Replayable evidence for Provider pool-size decisions."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from .trade_provider_inventory import ProviderInventoryError, next_desired_pool_size
from .trade_provider_pool_shadow_evidence import canonical_sha256

POOL_SIZE_EVIDENCE_SCHEMA = "a6-provider-pool-size-evidence-v1"


def build_pool_size_evidence(
    *,
    size_mode: str,
    current_desired_size: int,
    funded_eligible_provider_count: int,
    decision_at: datetime,
    observation_started_at: datetime,
    last_shrink_at: datetime | None,
    p99_concurrency: int,
    safe_concurrency_per_slot: int,
    target_utilization: Decimal | str | int,
    p99_simultaneous_failed_slots: int,
    providers_needed_for_24h_liquidity_runway: int,
    liquidity_runway_evidence_complete: bool,
    liquidity_runway_capacity_sufficient: bool,
    minimum_active_slots: int,
    initial_max_active_slots: int,
    minimum_observation: timedelta,
    minimum_shrink_interval: timedelta,
    desired_size: int,
) -> dict[str, Any]:
    """Build the strict document consumed by the shadow journal auditor."""

    if not isinstance(liquidity_runway_evidence_complete, bool) or not isinstance(
        liquidity_runway_capacity_sufficient, bool
    ):
        raise ProviderInventoryError(
            "provider_inventory_liquidity_runway_evidence_invalid"
        )
    if liquidity_runway_capacity_sufficient and not liquidity_runway_evidence_complete:
        raise ProviderInventoryError(
            "provider_inventory_liquidity_runway_evidence_invalid"
        )
    if (
        (
            not liquidity_runway_evidence_complete
            or not liquidity_runway_capacity_sufficient
        )
        and providers_needed_for_24h_liquidity_runway < current_desired_size
    ):
        raise ProviderInventoryError(
            "provider_inventory_liquidity_runway_evidence_not_conservative"
        )
    decision_time = _aware(decision_at, "provider_inventory_now_invalid")
    observation_start = _aware(
        observation_started_at,
        "provider_inventory_observation_start_invalid",
    )
    previous_shrink = (
        None
        if last_shrink_at is None
        else _aware(last_shrink_at, "provider_inventory_last_shrink_invalid")
    )
    utilization = Decimal(str(target_utilization))
    minimum_observation_seconds = _whole_seconds(
        minimum_observation,
        "provider_inventory_shrink_window_invalid",
        allow_zero=True,
    )
    minimum_shrink_interval_seconds = _whole_seconds(
        minimum_shrink_interval,
        "provider_inventory_shrink_window_invalid",
        allow_zero=False,
    )
    expected = next_desired_pool_size(
        size_mode=size_mode,
        current_desired_size=current_desired_size,
        funded_eligible_provider_count=funded_eligible_provider_count,
        now=decision_time,
        observation_started_at=observation_start,
        last_shrink_at=previous_shrink,
        p99_concurrency=p99_concurrency,
        safe_concurrency_per_slot=safe_concurrency_per_slot,
        target_utilization=utilization,
        p99_simultaneous_failed_slots=p99_simultaneous_failed_slots,
        providers_needed_for_24h_liquidity_runway=(
            providers_needed_for_24h_liquidity_runway
        ),
        minimum_active_slots=minimum_active_slots,
        initial_max_active_slots=initial_max_active_slots,
        minimum_observation=minimum_observation,
        minimum_shrink_interval=minimum_shrink_interval,
    )
    if isinstance(desired_size, bool) or desired_size != expected:
        raise ProviderInventoryError("provider_inventory_pool_size_decision_mismatch")

    input_document = {
        "size_mode": str(size_mode).strip().casefold(),
        "current_desired_size": current_desired_size,
        "funded_eligible_provider_count": funded_eligible_provider_count,
        "decision_at": decision_time.isoformat(),
        "observation_started_at": observation_start.isoformat(),
        "last_shrink_at": (
            None if previous_shrink is None else previous_shrink.isoformat()
        ),
        "p99_concurrency": p99_concurrency,
        "safe_concurrency_per_slot": safe_concurrency_per_slot,
        "target_utilization": format(utilization, "f"),
        "p99_simultaneous_failed_slots": p99_simultaneous_failed_slots,
        "providers_needed_for_24h_liquidity_runway": (
            providers_needed_for_24h_liquidity_runway
        ),
        "liquidity_runway_evidence_complete": (
            liquidity_runway_evidence_complete
        ),
        "liquidity_runway_capacity_sufficient": (
            liquidity_runway_capacity_sufficient
        ),
        "minimum_active_slots": minimum_active_slots,
        "initial_max_active_slots": initial_max_active_slots,
        "minimum_observation_seconds": minimum_observation_seconds,
        "minimum_shrink_interval_seconds": minimum_shrink_interval_seconds,
    }
    output_document = {
        "desired_size": desired_size,
        "action": (
            "expand"
            if desired_size > current_desired_size
            else "shrink"
            if desired_size < current_desired_size
            else "hold"
        ),
    }
    return {
        "schema": POOL_SIZE_EVIDENCE_SCHEMA,
        "input": input_document,
        "input_hash": canonical_sha256(input_document),
        "output": output_document,
        "output_hash": canonical_sha256(output_document),
    }


def _aware(value: datetime, code: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ProviderInventoryError(code)
    return value.astimezone(timezone.utc)


def _whole_seconds(value: timedelta, code: str, *, allow_zero: bool) -> int:
    if not isinstance(value, timedelta):
        raise ProviderInventoryError(code)
    seconds = value.total_seconds()
    if seconds < 0 or (not allow_zero and seconds <= 0) or not seconds.is_integer():
        raise ProviderInventoryError(code)
    return int(seconds)
