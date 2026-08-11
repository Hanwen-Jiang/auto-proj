"""PostgreSQL contracts for Provider regional receipts and capacity escrow.

The module intentionally stops at the durable coordination boundary. Redis
projectors, subscribers and request-plane clients can use these contracts
without making a regional cache authoritative.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


class ProviderRegionError(RuntimeError):
    """Stable, secret-free regional coordination error."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_REGION_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
_PROJECTION_STATES = frozenset({"projecting", "resyncing", "ready", "failed", "stale"})
_CAPACITY_STATES = frozenset({"active", "draining", "disabled"})
_RECEIPT_STATES = frozenset({"resyncing", "ready", "failed", "stale"})
_CAPACITY_REGIONS = frozenset({"us", "hk"})
_TOMBSTONE_HASH = "0" * 64


@dataclass(frozen=True)
class ProviderRegionProjectionReceipt:
    region: str
    model: str
    projection_type: str
    routing_generation: int
    cost_generation: int
    source_hash: str
    projection_digest: str
    redis_restart_epoch: str
    projected_at: datetime
    last_event_id: int = 0
    subscriber_cursor: int = 0
    subscriber_connected: bool = False
    projection_state: str = "projecting"
    subscriber_verified_at: datetime | None = None
    verified_at: datetime | None = None
    last_error_class: str | None = None

    def __post_init__(self) -> None:
        region = _region(self.region, "provider_region_region_invalid")
        model = _nonempty(self.model, "provider_region_model_invalid", 128)
        projection_type = str(self.projection_type).strip().casefold()
        if projection_type not in {"dispatch", "fast_block"}:
            raise ProviderRegionError("provider_region_projection_type_invalid")
        source_hash = _hash(self.source_hash, "provider_region_source_hash_invalid")
        tombstone = projection_type == "dispatch" and source_hash == _TOMBSTONE_HASH
        generation_parser = _nonnegative_int if tombstone else _positive_int
        routing_generation = generation_parser(
            self.routing_generation,
            "provider_region_routing_generation_invalid",
        )
        cost_generation = generation_parser(
            self.cost_generation,
            "provider_region_cost_generation_invalid",
        )
        if source_hash == _TOMBSTONE_HASH and not tombstone:
            raise ProviderRegionError("provider_region_tombstone_projection_invalid")
        projection_digest = _hash(
            self.projection_digest,
            "provider_region_projection_digest_invalid",
        )
        epoch = _nonempty(self.redis_restart_epoch, "provider_region_epoch_invalid", 128)
        projected_at = _aware(self.projected_at, "provider_region_projected_at_invalid")
        last_event_id = _nonnegative_int(
            self.last_event_id,
            "provider_region_last_event_invalid",
        )
        subscriber_cursor = _nonnegative_int(
            self.subscriber_cursor,
            "provider_region_subscriber_cursor_invalid",
        )
        if subscriber_cursor > last_event_id:
            raise ProviderRegionError("provider_region_subscriber_cursor_ahead")
        if not isinstance(self.subscriber_connected, bool):
            raise ProviderRegionError("provider_region_subscriber_connected_invalid")
        state = str(self.projection_state).strip().casefold()
        if state not in _PROJECTION_STATES:
            raise ProviderRegionError("provider_region_projection_state_invalid")
        subscriber_verified_at = _optional_aware(
            self.subscriber_verified_at,
            "provider_region_subscriber_verified_at_invalid",
        )
        verified_at = _optional_aware(
            self.verified_at,
            "provider_region_verified_at_invalid",
        )
        if subscriber_verified_at is not None and subscriber_verified_at < projected_at:
            raise ProviderRegionError("provider_region_subscriber_verified_before_projection")
        if verified_at is not None and verified_at < projected_at:
            raise ProviderRegionError("provider_region_verified_before_projection")
        error = _optional_error(self.last_error_class)
        if state == "ready" and (
            not self.subscriber_connected
            or subscriber_cursor != last_event_id
            or subscriber_verified_at is None
            or verified_at is None
            or error is not None
        ):
            raise ProviderRegionError("provider_region_ready_incomplete")
        object.__setattr__(self, "region", region)
        object.__setattr__(self, "model", model)
        object.__setattr__(self, "projection_type", projection_type)
        object.__setattr__(self, "routing_generation", routing_generation)
        object.__setattr__(self, "cost_generation", cost_generation)
        object.__setattr__(self, "source_hash", source_hash)
        object.__setattr__(self, "projection_digest", projection_digest)
        object.__setattr__(self, "redis_restart_epoch", epoch)
        object.__setattr__(self, "projected_at", projected_at)
        object.__setattr__(self, "last_event_id", last_event_id)
        object.__setattr__(self, "subscriber_cursor", subscriber_cursor)
        object.__setattr__(self, "projection_state", state)
        object.__setattr__(self, "subscriber_verified_at", subscriber_verified_at)
        object.__setattr__(self, "verified_at", verified_at)
        object.__setattr__(self, "last_error_class", error)


@dataclass(frozen=True)
class ProviderRegionProjectionSubscriberAck:
    """Subscriber acknowledgement bound to one exact projected document."""

    region: str
    model: str
    projection_type: str
    routing_generation: int
    cost_generation: int
    source_hash: str
    projection_digest: str
    last_event_id: int
    redis_restart_epoch: str
    subscriber_cursor: int
    subscriber_verified_at: datetime

    def __post_init__(self) -> None:
        region = _region(self.region, "provider_region_region_invalid")
        model = _nonempty(self.model, "provider_region_model_invalid", 128)
        projection_type = str(self.projection_type).strip().casefold()
        if projection_type not in {"dispatch", "fast_block"}:
            raise ProviderRegionError("provider_region_projection_type_invalid")
        source_hash = _hash(self.source_hash, "provider_region_source_hash_invalid")
        tombstone = projection_type == "dispatch" and source_hash == _TOMBSTONE_HASH
        generation_parser = _nonnegative_int if tombstone else _positive_int
        routing_generation = generation_parser(
            self.routing_generation,
            "provider_region_routing_generation_invalid",
        )
        cost_generation = generation_parser(
            self.cost_generation,
            "provider_region_cost_generation_invalid",
        )
        if source_hash == _TOMBSTONE_HASH and not tombstone:
            raise ProviderRegionError("provider_region_tombstone_projection_invalid")
        projection_digest = _hash(
            self.projection_digest,
            "provider_region_projection_digest_invalid",
        )
        last_event_id = _nonnegative_int(
            self.last_event_id,
            "provider_region_last_event_invalid",
        )
        subscriber_cursor = _nonnegative_int(
            self.subscriber_cursor,
            "provider_region_subscriber_cursor_invalid",
        )
        if subscriber_cursor != last_event_id:
            raise ProviderRegionError("provider_region_subscriber_ack_cursor_mismatch")
        epoch = _nonempty(self.redis_restart_epoch, "provider_region_epoch_invalid", 128)
        subscriber_verified_at = _aware(
            self.subscriber_verified_at,
            "provider_region_subscriber_verified_at_invalid",
        )
        object.__setattr__(self, "region", region)
        object.__setattr__(self, "model", model)
        object.__setattr__(self, "projection_type", projection_type)
        object.__setattr__(self, "routing_generation", routing_generation)
        object.__setattr__(self, "cost_generation", cost_generation)
        object.__setattr__(self, "source_hash", source_hash)
        object.__setattr__(self, "projection_digest", projection_digest)
        object.__setattr__(self, "last_event_id", last_event_id)
        object.__setattr__(self, "redis_restart_epoch", epoch)
        object.__setattr__(self, "subscriber_cursor", subscriber_cursor)
        object.__setattr__(self, "subscriber_verified_at", subscriber_verified_at)


@dataclass(frozen=True)
class ProviderRegionCapacityTarget:
    region: str
    quota: int
    allocation_state: str
    drain_target_quota: int | None = None

    def __post_init__(self) -> None:
        region = _region(self.region, "provider_region_capacity_region_invalid")
        quota = _nonnegative_int(self.quota, "provider_region_capacity_quota_invalid")
        state = str(self.allocation_state).strip().casefold()
        if state not in _CAPACITY_STATES:
            raise ProviderRegionError("provider_region_capacity_state_invalid")
        drain_target = self.drain_target_quota
        if state == "active":
            if quota < 1 or drain_target is not None:
                raise ProviderRegionError("provider_region_capacity_active_invalid")
        elif state == "disabled":
            if quota != 0 or drain_target is not None:
                raise ProviderRegionError("provider_region_capacity_disabled_invalid")
        else:
            if quota < 1 or drain_target is None:
                raise ProviderRegionError("provider_region_capacity_draining_invalid")
            drain_target = _nonnegative_int(
                drain_target,
                "provider_region_capacity_drain_target_invalid",
            )
            if drain_target >= quota:
                raise ProviderRegionError("provider_region_capacity_drain_target_invalid")
        object.__setattr__(self, "region", region)
        object.__setattr__(self, "quota", quota)
        object.__setattr__(self, "allocation_state", state)
        object.__setattr__(self, "drain_target_quota", drain_target)


@dataclass(frozen=True)
class ProviderRegionCapacityPlan:
    enodot_binding_id: int
    enodot_account_id: int
    account_concurrency: int
    allocation_key: str
    source_hash: str
    reason: str
    effective_at: datetime
    targets: tuple[ProviderRegionCapacityTarget, ...]

    def __post_init__(self) -> None:
        binding_id = _positive_int(
            self.enodot_binding_id,
            "provider_region_capacity_binding_invalid",
        )
        account_id = _positive_int(
            self.enodot_account_id,
            "provider_region_capacity_account_invalid",
        )
        concurrency = _nonnegative_int(
            self.account_concurrency,
            "provider_region_capacity_concurrency_invalid",
        )
        key = _nonempty(self.allocation_key, "provider_region_capacity_key_invalid", 160)
        source_hash = _hash(
            self.source_hash,
            "provider_region_capacity_source_hash_invalid",
        )
        reason = _nonempty(self.reason, "provider_region_capacity_reason_invalid", 512)
        effective_at = _aware(
            self.effective_at,
            "provider_region_capacity_effective_at_invalid",
        )
        targets = tuple(self.targets)
        if not targets or any(
            not isinstance(item, ProviderRegionCapacityTarget) for item in targets
        ):
            raise ProviderRegionError("provider_region_capacity_targets_invalid")
        if len({item.region for item in targets}) != len(targets):
            raise ProviderRegionError("provider_region_capacity_region_duplicate")
        if {item.region for item in targets} != _CAPACITY_REGIONS:
            raise ProviderRegionError("provider_region_capacity_region_set_invalid")
        if sum(item.quota for item in targets) > concurrency:
            raise ProviderRegionError("provider_region_capacity_overallocated")
        object.__setattr__(self, "account_concurrency", concurrency)
        object.__setattr__(self, "enodot_binding_id", binding_id)
        object.__setattr__(self, "enodot_account_id", account_id)
        object.__setattr__(self, "allocation_key", key)
        object.__setattr__(self, "source_hash", source_hash)
        object.__setattr__(self, "reason", reason)
        object.__setattr__(self, "effective_at", effective_at)
        object.__setattr__(self, "targets", tuple(sorted(targets, key=lambda item: item.region)))

    @property
    def allocation_digest(self) -> str:
        document = {
            "account_id": self.enodot_account_id,
            "account_concurrency": self.account_concurrency,
            "binding_id": self.enodot_binding_id,
            "reason": self.reason,
            "source_hash": self.source_hash,
            "targets": [
                {
                    "drain_target_quota": item.drain_target_quota,
                    "region": item.region,
                    "quota": item.quota,
                    "state": item.allocation_state,
                }
                for item in self.targets
            ],
        }
        return hashlib.sha256(
            json.dumps(document, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()


@dataclass(frozen=True)
class ProviderRegionCapacityReceipt:
    enodot_binding_id: int
    enodot_account_id: int
    region: str
    allocation_version: int
    quota: int
    occupancy: int
    occupancy_digest: str
    lease_cursor: int
    redis_restart_epoch: str
    projected_at: datetime
    receipt_state: str = "resyncing"
    verified_at: datetime | None = None
    last_error_class: str | None = None

    def __post_init__(self) -> None:
        binding_id = _positive_int(
            self.enodot_binding_id,
            "provider_region_capacity_binding_invalid",
        )
        account_id = _positive_int(
            self.enodot_account_id,
            "provider_region_capacity_account_invalid",
        )
        region = _region(self.region, "provider_region_capacity_region_invalid")
        allocation_version = _positive_int(
            self.allocation_version,
            "provider_region_capacity_version_invalid",
        )
        quota = _nonnegative_int(self.quota, "provider_region_capacity_quota_invalid")
        occupancy = _nonnegative_int(
            self.occupancy,
            "provider_region_capacity_occupancy_invalid",
        )
        if occupancy > quota:
            raise ProviderRegionError("provider_region_capacity_occupancy_over_quota")
        digest = _hash(
            self.occupancy_digest,
            "provider_region_capacity_occupancy_digest_invalid",
        )
        cursor = _nonnegative_int(
            self.lease_cursor,
            "provider_region_capacity_lease_cursor_invalid",
        )
        epoch = _nonempty(self.redis_restart_epoch, "provider_region_epoch_invalid", 128)
        projected_at = _aware(
            self.projected_at,
            "provider_region_capacity_projected_at_invalid",
        )
        state = str(self.receipt_state).strip().casefold()
        if state not in _RECEIPT_STATES:
            raise ProviderRegionError("provider_region_capacity_receipt_state_invalid")
        verified_at = _optional_aware(
            self.verified_at,
            "provider_region_capacity_verified_at_invalid",
        )
        if verified_at is not None and verified_at < projected_at:
            raise ProviderRegionError("provider_region_capacity_verified_before_projection")
        error = _optional_error(self.last_error_class)
        if state == "ready" and (verified_at is None or error is not None):
            raise ProviderRegionError("provider_region_capacity_ready_incomplete")
        object.__setattr__(self, "region", region)
        object.__setattr__(self, "enodot_binding_id", binding_id)
        object.__setattr__(self, "enodot_account_id", account_id)
        object.__setattr__(self, "allocation_version", allocation_version)
        object.__setattr__(self, "quota", quota)
        object.__setattr__(self, "occupancy", occupancy)
        object.__setattr__(self, "occupancy_digest", digest)
        object.__setattr__(self, "lease_cursor", cursor)
        object.__setattr__(self, "redis_restart_epoch", epoch)
        object.__setattr__(self, "projected_at", projected_at)
        object.__setattr__(self, "receipt_state", state)
        object.__setattr__(self, "verified_at", verified_at)
        object.__setattr__(self, "last_error_class", error)


@dataclass(frozen=True)
class ProviderRegionProjectionWriteResult:
    receipt_version: int
    deduplicated: bool


@dataclass(frozen=True)
class ProviderRegionCapacityWriteResult:
    allocation_version: int
    allocation_digest: str
    deduplicated: bool


@dataclass(frozen=True)
class ProviderRegionCapacityReceiptWriteResult:
    receipt_version: int
    deduplicated: bool


def validate_projection_transition(
    current: Mapping[str, Any] | None,
    desired: ProviderRegionProjectionReceipt,
    *,
    expected_receipt_version: int,
) -> str:
    """Validate a receipt CAS and return ``insert``, ``advance`` or ``dedupe``."""

    expected = _nonnegative_int(
        expected_receipt_version,
        "provider_region_receipt_expected_version_invalid",
    )
    if current is None:
        if expected != 0:
            raise ProviderRegionError("provider_region_receipt_cas_mismatch")
        if desired.projection_state == "ready":
            raise ProviderRegionError("provider_region_receipt_initial_resync_required")
        return "insert"
    version = _positive_int(
        current.get("receipt_version"),
        "provider_region_receipt_current_version_invalid",
    )
    if expected != version:
        raise ProviderRegionError("provider_region_receipt_cas_mismatch")
    if (
        _region(current.get("region"), "provider_region_current_region_invalid")
        != desired.region
        or _nonempty(
            current.get("model"),
            "provider_region_current_model_invalid",
            128,
        )
        != desired.model
        or str(current.get("projection_type") or "").strip().casefold()
        != desired.projection_type
    ):
        raise ProviderRegionError("provider_region_receipt_identity_mismatch")
    current_source = str(current.get("source_hash") or "").strip().casefold()
    current_tombstone = (
        desired.projection_type == "dispatch" and current_source == _TOMBSTONE_HASH
    )
    generation_parser = _nonnegative_int if current_tombstone else _positive_int
    current_routing = generation_parser(
        current.get("routing_generation"),
        "provider_region_receipt_current_generation_invalid",
    )
    current_cost = generation_parser(
        current.get("cost_generation"),
        "provider_region_receipt_current_cost_generation_invalid",
    )
    current_event = _nonnegative_int(
        current.get("last_event_id"),
        "provider_region_receipt_current_event_invalid",
    )
    current_cursor = _nonnegative_int(
        current.get("subscriber_cursor"),
        "provider_region_receipt_current_cursor_invalid",
    )
    if current_cursor > current_event:
        raise ProviderRegionError("provider_region_receipt_current_cursor_invalid")
    if desired.routing_generation < current_routing or desired.cost_generation < current_cost:
        raise ProviderRegionError("provider_region_receipt_stale_generation")
    if desired.last_event_id < current_event:
        raise ProviderRegionError("provider_region_receipt_stale_cursor")
    same_generation = (
        desired.routing_generation == current_routing
        and desired.cost_generation == current_cost
    )
    current_digest = str(current.get("projection_digest") or "").strip().casefold()
    same_event = desired.last_event_id == current_event
    tombstone_transition = (
        desired.projection_type == "dispatch"
        and desired.source_hash == _TOMBSTONE_HASH
        and current_source != _TOMBSTONE_HASH
    )
    if same_generation and desired.source_hash != current_source and not tombstone_transition:
        raise ProviderRegionError("provider_region_receipt_digest_conflict")
    if same_generation and (
        desired.projection_type == "dispatch" or same_event
    ) and desired.projection_digest != current_digest and not tombstone_transition:
        raise ProviderRegionError("provider_region_receipt_digest_conflict")

    current_epoch = _nonempty(
        current.get("redis_restart_epoch"),
        "provider_region_current_epoch_invalid",
        128,
    )
    current_projected_at = _aware(
        current.get("projected_at"),
        "provider_region_current_projected_at_invalid",
    )
    current_state = str(current.get("projection_state") or "").strip().casefold()
    if current_state not in _PROJECTION_STATES:
        raise ProviderRegionError("provider_region_projection_state_invalid")
    epoch_changed = desired.redis_restart_epoch != current_epoch
    projection_changed = (
        not same_generation
        or desired.source_hash != current_source
        or desired.projection_digest != current_digest
        or desired.last_event_id != current_event
        or epoch_changed
    )
    reprojection_started = desired.projection_state in {"projecting", "resyncing"} and (
        projection_changed
        or current_state not in {"projecting", "resyncing"}
        or desired.projected_at > current_projected_at
    )
    if reprojection_started:
        if (
            (epoch_changed and desired.projection_state != "resyncing")
            or desired.subscriber_connected
            or desired.subscriber_cursor != 0
            or desired.subscriber_verified_at is not None
            or desired.verified_at is not None
        ):
            raise ProviderRegionError(
                "provider_region_restart_resync_required"
                if epoch_changed
                else "provider_region_reprojection_ack_reset_required"
            )
    elif epoch_changed:
        raise ProviderRegionError("provider_region_restart_resync_required")
    elif desired.subscriber_cursor < current_cursor:
        raise ProviderRegionError("provider_region_receipt_stale_cursor")

    generation_changed = not same_generation
    if generation_changed and desired.projection_state == "ready":
        raise ProviderRegionError("provider_region_generation_resync_required")

    if desired.projected_at < current_projected_at:
        raise ProviderRegionError("provider_region_receipt_stale_timestamp")
    if (
        same_generation
        and desired.last_event_id == current_event
        and desired.subscriber_cursor == current_cursor
        and desired.source_hash == current_source
        and desired.projection_digest == current_digest
        and desired.redis_restart_epoch == current_epoch
        and desired.projection_state == str(current.get("projection_state") or "")
        and desired.subscriber_connected == bool(current.get("subscriber_connected"))
        and desired.projected_at == current_projected_at
        and desired.subscriber_verified_at
        == _optional_aware(
            current.get("subscriber_verified_at"),
            "provider_region_current_subscriber_verified_at_invalid",
        )
        and desired.verified_at
        == _optional_aware(
            current.get("verified_at"),
            "provider_region_current_verified_at_invalid",
        )
        and desired.last_error_class == _optional_error(current.get("last_error_class"))
    ):
        return "dedupe"
    return "advance"


def validate_projection_subscriber_ack(
    current: Mapping[str, Any] | None,
    ack: ProviderRegionProjectionSubscriberAck,
    *,
    expected_receipt_version: int,
) -> str:
    """Validate a subscriber compare-ACK without granting projector ownership."""

    expected = _positive_int(
        expected_receipt_version,
        "provider_region_receipt_expected_version_invalid",
    )
    if current is None:
        raise ProviderRegionError("provider_region_receipt_missing")
    version = _positive_int(
        current.get("receipt_version"),
        "provider_region_receipt_current_version_invalid",
    )
    if expected != version:
        raise ProviderRegionError("provider_region_receipt_cas_mismatch")
    exact = (
        _region(current.get("region"), "provider_region_current_region_invalid") == ack.region
        and _nonempty(current.get("model"), "provider_region_current_model_invalid", 128)
        == ack.model
        and str(current.get("projection_type") or "").strip().casefold()
        == ack.projection_type
        and int(current.get("routing_generation")) == ack.routing_generation
        and int(current.get("cost_generation")) == ack.cost_generation
        and str(current.get("source_hash") or "").strip().casefold() == ack.source_hash
        and str(current.get("projection_digest") or "").strip().casefold()
        == ack.projection_digest
        and int(current.get("last_event_id")) == ack.last_event_id
        and _nonempty(
            current.get("redis_restart_epoch"),
            "provider_region_current_epoch_invalid",
            128,
        )
        == ack.redis_restart_epoch
    )
    if not exact:
        raise ProviderRegionError("provider_region_subscriber_ack_projection_mismatch")
    state = str(current.get("projection_state") or "").strip().casefold()
    if state not in {"projecting", "resyncing", "ready"}:
        raise ProviderRegionError("provider_region_subscriber_ack_state_invalid")
    if current.get("last_error_class") is not None:
        raise ProviderRegionError("provider_region_subscriber_ack_projection_not_clean")
    projected_at = _aware(
        current.get("projected_at"),
        "provider_region_current_projected_at_invalid",
    )
    if ack.subscriber_verified_at < projected_at:
        raise ProviderRegionError("provider_region_subscriber_verified_before_projection")
    current_verified = _optional_aware(
        current.get("subscriber_verified_at"),
        "provider_region_current_subscriber_verified_at_invalid",
    )
    if (
        bool(current.get("subscriber_connected"))
        and int(current.get("subscriber_cursor")) == ack.subscriber_cursor
        and current_verified == ack.subscriber_verified_at
    ):
        return "dedupe"
    if state == "ready":
        raise ProviderRegionError("provider_region_subscriber_ack_ready_conflict")
    return "ack"


def validate_capacity_transition(
    current: Sequence[Mapping[str, Any]],
    desired: ProviderRegionCapacityPlan,
    *,
    current_receipts: Mapping[str, Mapping[str, Any]] | None = None,
) -> None:
    """Enforce drain-before-transfer against a locked current snapshot."""

    current_receipts = current_receipts or {}
    current_by_region = {str(row.get("region")): row for row in current}
    if len(current_by_region) != len(current):
        raise ProviderRegionError("provider_region_capacity_current_duplicate")
    if current:
        versions = {int(row.get("allocation_version") or 0) for row in current}
        if len(versions) != 1 or not next(iter(versions)) > 0:
            raise ProviderRegionError("provider_region_capacity_current_version_invalid")
        concurrency_values = {
            _nonnegative_int(
                row.get("account_concurrency"),
                "provider_region_capacity_current_concurrency_invalid",
            )
            for row in current
        }
        if concurrency_values != {desired.account_concurrency}:
            raise ProviderRegionError("provider_region_capacity_concurrency_cas_mismatch")
    desired_by_region = {item.region: item for item in desired.targets}
    if current and set(current_by_region) != set(desired_by_region):
        raise ProviderRegionError("provider_region_capacity_region_set_changed")
    for region, row in current_by_region.items():
        old_quota = _nonnegative_int(
            row.get("quota"),
            "provider_region_capacity_current_quota_invalid",
        )
        target = desired_by_region[region]
        if target.quota < old_quota:
            if str(row.get("allocation_state") or "") != "draining":
                raise ProviderRegionError("provider_region_capacity_drain_required")
            drain_target = _nonnegative_int(
                row.get("drain_target_quota"),
                "provider_region_capacity_current_drain_target_invalid",
            )
            if drain_target != target.quota:
                raise ProviderRegionError("provider_region_capacity_drain_target_mismatch")
            receipt = current_receipts.get(region)
            if receipt is None or str(receipt.get("receipt_state") or "") != "ready":
                raise ProviderRegionError("provider_region_capacity_occupancy_unverified")
            if int(receipt.get("allocation_version") or 0) != int(
                row.get("allocation_version") or 0
            ):
                raise ProviderRegionError("provider_region_capacity_receipt_stale")
            if _nonnegative_int(
                receipt.get("quota"),
                "provider_region_capacity_receipt_quota_invalid",
            ) != old_quota:
                raise ProviderRegionError("provider_region_capacity_receipt_stale")
            occupancy = _nonnegative_int(
                receipt.get("occupancy"),
                "provider_region_capacity_receipt_occupancy_invalid",
            )
            if occupancy > target.quota:
                raise ProviderRegionError("provider_region_capacity_drain_not_complete")
            drain_started_at = row.get("drain_started_at")
            verified_at = receipt.get("verified_at")
            if drain_started_at is None or verified_at is None:
                raise ProviderRegionError("provider_region_capacity_occupancy_unverified")
            if _aware(
                verified_at,
                "provider_region_capacity_receipt_verified_at_invalid",
            ) < _aware(
                drain_started_at,
                "provider_region_capacity_drain_started_at_invalid",
            ):
                raise ProviderRegionError("provider_region_capacity_receipt_before_drain")
        elif target.quota == old_quota and str(row.get("allocation_state") or "") == "draining":
            if target.allocation_state != "draining":
                raise ProviderRegionError("provider_region_capacity_drain_not_complete")
        elif target.quota > old_quota and str(row.get("allocation_state") or "") == "draining":
            raise ProviderRegionError("provider_region_capacity_drain_in_progress")


class PostgresProviderRegionStore:
    """Durable CAS repository for regional receipts and capacity allocation."""

    def __init__(self, connection_factory: Callable[[], Any]) -> None:
        if not callable(connection_factory):
            raise ProviderRegionError("provider_region_connection_factory_invalid")
        self.connection_factory = connection_factory

    def record_projection_receipt(
        self,
        receipt: ProviderRegionProjectionReceipt,
        *,
        expected_receipt_version: int,
    ) -> ProviderRegionProjectionWriteResult:
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                    (f"a6-provider-region-receipt:{receipt.region}:{receipt.model}:{receipt.projection_type}",),
                )
                cursor.execute(
                    _CANONICAL_GENERATION_SQL,
                    (
                        receipt.model,
                        receipt.routing_generation,
                        receipt.cost_generation,
                        receipt.model,
                        receipt.routing_generation,
                        receipt.model,
                        receipt.cost_generation,
                        receipt.model,
                    ),
                )
                canonical = _row(cursor)
                if canonical is None:
                    raise ProviderRegionError("provider_region_canonical_generation_missing")
                canonical_source = str(canonical.get("source_hash") or "").strip().casefold()
                if (
                    canonical_source == _TOMBSTONE_HASH
                    and receipt.projection_type != "dispatch"
                ):
                    raise ProviderRegionError("provider_region_tombstone_projection_invalid")
                if canonical_source != receipt.source_hash:
                    raise ProviderRegionError("provider_region_source_hash_mismatch")
                cursor.execute(
                    _CURRENT_PROJECTION_RECEIPT_SQL,
                    (receipt.region, receipt.model, receipt.projection_type),
                )
                current = _row(cursor)
                action = validate_projection_transition(
                    current,
                    receipt,
                    expected_receipt_version=expected_receipt_version,
                )
                if action == "dedupe":
                    version = int(current["receipt_version"])
                    connection.commit()
                    return ProviderRegionProjectionWriteResult(version, True)
                if action == "insert":
                    cursor.execute(
                        _INSERT_PROJECTION_RECEIPT_SQL,
                        _projection_params(receipt, 1),
                    )
                    version = 1
                else:
                    version = int(current["receipt_version"]) + 1
                    cursor.execute(
                        _UPDATE_PROJECTION_RECEIPT_SQL,
                        _projection_update_params(receipt, version)
                        + (current["id"], expected_receipt_version),
                    )
                    if int(getattr(cursor, "rowcount", 0) or 0) != 1:
                        raise ProviderRegionError("provider_region_receipt_cas_mismatch")
            connection.commit()
            return ProviderRegionProjectionWriteResult(version, False)
        except Exception:
            _rollback(connection)
            raise
        finally:
            _close(connection)

    def ack_projection_receipt(
        self,
        ack: ProviderRegionProjectionSubscriberAck,
        *,
        expected_receipt_version: int,
    ) -> ProviderRegionProjectionWriteResult:
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                    (
                        f"a6-provider-region-receipt:{ack.region}:{ack.model}:"
                        f"{ack.projection_type}",
                    ),
                )
                cursor.execute(
                    _CURRENT_PROJECTION_RECEIPT_SQL,
                    (ack.region, ack.model, ack.projection_type),
                )
                current = _row(cursor)
                action = validate_projection_subscriber_ack(
                    current,
                    ack,
                    expected_receipt_version=expected_receipt_version,
                )
                if action == "dedupe":
                    connection.commit()
                    return ProviderRegionProjectionWriteResult(
                        int(current["receipt_version"]),
                        True,
                    )
                version = int(current["receipt_version"]) + 1
                cursor.execute(
                    _ACK_PROJECTION_RECEIPT_SQL,
                    (
                        ack.subscriber_cursor,
                        ack.subscriber_verified_at,
                        version,
                        current["id"],
                        expected_receipt_version,
                        current["region"],
                        current["model"],
                        current["projection_type"],
                        current["routing_generation"],
                        current["cost_generation"],
                        current["source_hash"],
                        current["projection_digest"],
                        current["last_event_id"],
                        current["redis_restart_epoch"],
                        current["projection_state"],
                        current["projected_at"],
                        current.get("verified_at"),
                    ),
                )
                if int(getattr(cursor, "rowcount", 0) or 0) != 1:
                    raise ProviderRegionError("provider_region_receipt_cas_mismatch")
            connection.commit()
            return ProviderRegionProjectionWriteResult(version, False)
        except Exception:
            _rollback(connection)
            raise
        finally:
            _close(connection)

    def set_capacity_allocations(
        self,
        plan: ProviderRegionCapacityPlan,
        *,
        expected_allocation_version: int,
    ) -> ProviderRegionCapacityWriteResult:
        expected = _nonnegative_int(
            expected_allocation_version,
            "provider_region_capacity_expected_version_invalid",
        )
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                    (f"a6-provider-region-capacity:{plan.enodot_account_id}",),
                )
                cursor.execute(
                    _BINDING_CAPACITY_SQL,
                    (plan.enodot_binding_id, plan.enodot_account_id),
                )
                binding = _row(cursor)
                if (
                    binding is None
                    or int(binding.get("desired_concurrency") or 0)
                    != plan.account_concurrency
                ):
                    raise ProviderRegionError("provider_region_capacity_concurrency_mismatch")
                cursor.execute(
                    _CAPACITY_BY_KEY_SQL,
                    (plan.enodot_account_id, plan.allocation_key),
                )
                by_key = _rows(cursor)
                if by_key:
                    if any(
                        str(row.get("allocation_digest") or "").strip()
                        != plan.allocation_digest
                        for row in by_key
                    ):
                        raise ProviderRegionError("provider_region_capacity_idempotency_conflict")
                    versions = {
                        _positive_int(
                            row.get("allocation_version"),
                            "provider_region_capacity_idempotency_version_invalid",
                        )
                        for row in by_key
                    }
                    regions = {
                        _region(
                            row.get("region"),
                            "provider_region_capacity_idempotency_region_invalid",
                        )
                        for row in by_key
                    }
                    if len(versions) != 1 or regions != {
                        target.region for target in plan.targets
                    }:
                        raise ProviderRegionError("provider_region_capacity_idempotency_conflict")
                    version = next(iter(versions))
                    connection.commit()
                    return ProviderRegionCapacityWriteResult(version, plan.allocation_digest, True)
                cursor.execute(_CURRENT_CAPACITY_SQL, (plan.enodot_account_id,))
                current = _rows(cursor)
                current_version = 0 if not current else int(current[0]["allocation_version"])
                if current and any(
                    int(row["allocation_version"]) != current_version for row in current
                ):
                    raise ProviderRegionError("provider_region_capacity_current_version_invalid")
                if expected != current_version:
                    raise ProviderRegionError("provider_region_capacity_cas_mismatch")
                receipts: dict[str, Mapping[str, Any]] = {}
                if current:
                    cursor.execute(_CURRENT_CAPACITY_RECEIPTS_SQL, (plan.enodot_account_id,))
                    receipts = {str(row["region"]): row for row in _rows(cursor)}
                validate_capacity_transition(current, plan, current_receipts=receipts)
                version = current_version + 1
                if current:
                    cursor.execute(
                        _CLOSE_CAPACITY_SQL,
                        (plan.effective_at, plan.enodot_account_id, current_version),
                    )
                    if int(getattr(cursor, "rowcount", 0) or 0) != len(current):
                        raise ProviderRegionError("provider_region_capacity_cas_mismatch")
                for target in plan.targets:
                    old = next(
                        (row for row in current if row["region"] == target.region),
                        None,
                    )
                    drain_started = None
                    if target.allocation_state == "draining":
                        drain_started = (
                            old.get("drain_started_at") if old is not None else None
                        ) or plan.effective_at
                    cursor.execute(
                        _INSERT_CAPACITY_SQL,
                        (
                            plan.enodot_binding_id,
                            plan.enodot_account_id,
                            target.region,
                            target.quota,
                            plan.account_concurrency,
                            version,
                            plan.allocation_key,
                            plan.allocation_digest,
                            plan.source_hash,
                            target.allocation_state,
                            target.drain_target_quota,
                            plan.effective_at,
                            drain_started,
                            plan.reason,
                        ),
                    )
            connection.commit()
            return ProviderRegionCapacityWriteResult(version, plan.allocation_digest, False)
        except Exception:
            _rollback(connection)
            raise
        finally:
            _close(connection)

    def record_capacity_receipt(
        self,
        receipt: ProviderRegionCapacityReceipt,
        *,
        expected_receipt_version: int,
    ) -> ProviderRegionCapacityReceiptWriteResult:
        if receipt.receipt_state != "resyncing" or receipt.verified_at is not None:
            raise ProviderRegionError("provider_region_capacity_projector_state_invalid")
        expected = _nonnegative_int(
            expected_receipt_version,
            "provider_region_capacity_expected_receipt_version_invalid",
        )
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                    (f"a6-provider-region-capacity-receipt:{receipt.enodot_account_id}:{receipt.region}",),
                )
                cursor.execute(
                    _CURRENT_CAPACITY_ROW_SQL,
                    (receipt.enodot_account_id, receipt.region),
                )
                allocation = _row(cursor)
                if allocation is None:
                    raise ProviderRegionError("provider_region_capacity_allocation_missing")
                if (
                    int(allocation["enodot_binding_id"]) != receipt.enodot_binding_id
                    or int(allocation["allocation_version"]) != receipt.allocation_version
                    or int(allocation["quota"]) != receipt.quota
                ):
                    raise ProviderRegionError("provider_region_capacity_receipt_identity_mismatch")
                cursor.execute(
                    _CURRENT_CAPACITY_RECEIPT_ROW_SQL,
                    (receipt.enodot_account_id, receipt.region),
                )
                current = _row(cursor)
                action = validate_capacity_receipt_transition(
                    current,
                    receipt,
                    expected_receipt_version=expected,
                )
                if action == "dedupe":
                    version = int(current["receipt_version"])
                    connection.commit()
                    return ProviderRegionCapacityReceiptWriteResult(version, True)
                version = 1 if current is None else int(current["receipt_version"]) + 1
                if current is None:
                    cursor.execute(
                        _INSERT_CAPACITY_RECEIPT_SQL,
                        _capacity_receipt_params(receipt, version, allocation),
                    )
                else:
                    cursor.execute(
                        _UPDATE_CAPACITY_RECEIPT_SQL,
                        _capacity_receipt_update_params(receipt, version, allocation)
                        + (current["id"], expected),
                    )
                    if int(getattr(cursor, "rowcount", 0) or 0) != 1:
                        raise ProviderRegionError("provider_region_capacity_receipt_cas_mismatch")
            connection.commit()
            return ProviderRegionCapacityReceiptWriteResult(version, False)
        except Exception:
            _rollback(connection)
            raise
        finally:
            _close(connection)

    def ack_capacity_receipt(
        self,
        receipt: ProviderRegionCapacityReceipt,
        *,
        expected_receipt_version: int,
    ) -> ProviderRegionCapacityReceiptWriteResult:
        if receipt.receipt_state != "ready" or receipt.verified_at is None:
            raise ProviderRegionError("provider_region_capacity_subscriber_ack_invalid")
        expected = _positive_int(
            expected_receipt_version,
            "provider_region_capacity_expected_receipt_version_invalid",
        )
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                    (
                        f"a6-provider-region-capacity-receipt:"
                        f"{receipt.enodot_account_id}:{receipt.region}",
                    ),
                )
                cursor.execute(
                    _CURRENT_CAPACITY_ROW_SQL,
                    (receipt.enodot_account_id, receipt.region),
                )
                allocation = _row(cursor)
                if allocation is None:
                    raise ProviderRegionError("provider_region_capacity_allocation_missing")
                cursor.execute(
                    _CURRENT_CAPACITY_RECEIPT_ROW_SQL,
                    (receipt.enodot_account_id, receipt.region),
                )
                current = _row(cursor)
                if current is None or not _capacity_ack_matches_current(current, receipt):
                    raise ProviderRegionError(
                        "provider_region_capacity_subscriber_ack_mismatch"
                    )
                action = validate_capacity_receipt_transition(
                    current,
                    receipt,
                    expected_receipt_version=expected,
                )
                if action == "dedupe":
                    connection.commit()
                    return ProviderRegionCapacityReceiptWriteResult(
                        int(current["receipt_version"]),
                        True,
                    )
                if str(current.get("receipt_state") or "").strip().casefold() == "ready":
                    raise ProviderRegionError(
                        "provider_region_capacity_subscriber_ack_ready_conflict"
                    )
                version = int(current["receipt_version"]) + 1
                cursor.execute(
                    _ACK_CAPACITY_RECEIPT_SQL,
                    (
                        version,
                        receipt.verified_at,
                        current["id"],
                        expected,
                        current["allocation_id"],
                        current["enodot_binding_id"],
                        current["enodot_account_id"],
                        current["region"],
                        current["allocation_version"],
                        current["quota"],
                        current["occupancy"],
                        current["occupancy_digest"],
                        current["lease_cursor"],
                        current["redis_restart_epoch"],
                        current["projected_at"],
                    ),
                )
                if int(getattr(cursor, "rowcount", 0) or 0) != 1:
                    raise ProviderRegionError("provider_region_capacity_receipt_cas_mismatch")
            connection.commit()
            return ProviderRegionCapacityReceiptWriteResult(version, False)
        except Exception:
            _rollback(connection)
            raise
        finally:
            _close(connection)


def validate_capacity_receipt_transition(
    current: Mapping[str, Any] | None,
    desired: ProviderRegionCapacityReceipt,
    *,
    expected_receipt_version: int,
) -> str:
    if current is None:
        if expected_receipt_version != 0:
            raise ProviderRegionError("provider_region_capacity_receipt_cas_mismatch")
        if desired.receipt_state != "resyncing":
            raise ProviderRegionError("provider_region_capacity_initial_resync_required")
        return "insert"
    version = _positive_int(
        current.get("receipt_version"),
        "provider_region_capacity_current_receipt_version_invalid",
    )
    if expected_receipt_version != version:
        raise ProviderRegionError("provider_region_capacity_receipt_cas_mismatch")
    current_binding = _positive_int(
        current.get("enodot_binding_id"),
        "provider_region_capacity_current_binding_invalid",
    )
    current_account = _positive_int(
        current.get("enodot_account_id"),
        "provider_region_capacity_current_account_invalid",
    )
    current_region = _region(
        current.get("region"),
        "provider_region_capacity_current_region_invalid",
    )
    if (
        desired.enodot_binding_id != current_binding
        or desired.enodot_account_id != current_account
        or desired.region != current_region
    ):
        raise ProviderRegionError("provider_region_capacity_receipt_identity_mismatch")
    current_allocation_version = _positive_int(
        current.get("allocation_version"),
        "provider_region_capacity_current_allocation_version_invalid",
    )
    if desired.allocation_version < current_allocation_version:
        raise ProviderRegionError("provider_region_capacity_stale_allocation")

    current_epoch = _nonempty(
        current.get("redis_restart_epoch"),
        "provider_region_current_epoch_invalid",
        128,
    )
    epoch_changed = desired.redis_restart_epoch != current_epoch
    if epoch_changed:
        if desired.receipt_state != "resyncing" or desired.verified_at is not None:
            raise ProviderRegionError("provider_region_capacity_restart_resync_required")
        if desired.lease_cursor != 0:
            raise ProviderRegionError("provider_region_capacity_restart_cursor_invalid")
    else:
        current_cursor = _nonnegative_int(
            current.get("lease_cursor"),
            "provider_region_capacity_current_cursor_invalid",
        )
        if desired.lease_cursor < current_cursor:
            raise ProviderRegionError("provider_region_capacity_stale_cursor")
        if (
            desired.lease_cursor == current_cursor
            and desired.allocation_version == current_allocation_version
            and (
                desired.occupancy != int(current.get("occupancy") or 0)
                or desired.occupancy_digest
                != str(current.get("occupancy_digest") or "").strip().casefold()
            )
        ):
            raise ProviderRegionError("provider_region_capacity_digest_conflict")

    current_projected_at = _aware(
        current.get("projected_at"),
        "provider_region_capacity_current_projected_at_invalid",
    )
    if desired.projected_at < current_projected_at:
        raise ProviderRegionError("provider_region_capacity_receipt_stale_timestamp")
    if (
        desired.enodot_binding_id == current_binding
        and desired.enodot_account_id == current_account
        and desired.region == current_region
        and desired.allocation_version == current_allocation_version
        and desired.quota
        == _nonnegative_int(
            current.get("quota"),
            "provider_region_capacity_current_quota_invalid",
        )
        and desired.redis_restart_epoch == current_epoch
        and desired.lease_cursor == int(current.get("lease_cursor") or 0)
        and desired.occupancy == int(current.get("occupancy") or 0)
        and desired.occupancy_digest
        == str(current.get("occupancy_digest") or "").strip().casefold()
        and desired.receipt_state == str(current.get("receipt_state") or "")
        and desired.projected_at == current_projected_at
        and desired.verified_at
        == _optional_aware(
            current.get("verified_at"),
            "provider_region_capacity_current_verified_at_invalid",
        )
        and desired.last_error_class == _optional_error(current.get("last_error_class"))
    ):
        return "dedupe"
    return "advance"


def _projection_params(receipt: ProviderRegionProjectionReceipt, version: int) -> tuple[Any, ...]:
    return (
        receipt.region,
        receipt.model,
        receipt.projection_type,
        receipt.routing_generation,
        receipt.cost_generation,
        receipt.source_hash,
        receipt.projection_digest,
        receipt.last_event_id,
        receipt.redis_restart_epoch,
        receipt.projection_state,
        version,
        receipt.projected_at,
        receipt.verified_at,
        receipt.last_error_class,
    )


def _projection_update_params(
    receipt: ProviderRegionProjectionReceipt,
    version: int,
) -> tuple[Any, ...]:
    return _projection_params(receipt, version)[3:]


def _capacity_receipt_params(
    receipt: ProviderRegionCapacityReceipt,
    version: int,
    allocation: Mapping[str, Any],
) -> tuple[Any, ...]:
    return (
        allocation["id"],
        receipt.enodot_binding_id,
        receipt.enodot_account_id,
        receipt.region,
        receipt.allocation_version,
        receipt.quota,
        receipt.occupancy,
        receipt.occupancy_digest,
        receipt.lease_cursor,
        receipt.redis_restart_epoch,
        version,
        receipt.projected_at,
        receipt.last_error_class,
    )


def _capacity_receipt_update_params(
    receipt: ProviderRegionCapacityReceipt,
    version: int,
    allocation: Mapping[str, Any],
) -> tuple[Any, ...]:
    return (
        allocation["id"],
        receipt.allocation_version,
        receipt.quota,
        receipt.occupancy,
        receipt.occupancy_digest,
        receipt.lease_cursor,
        receipt.redis_restart_epoch,
        version,
        receipt.projected_at,
        receipt.last_error_class,
    )


def _capacity_ack_matches_current(
    current: Mapping[str, Any],
    receipt: ProviderRegionCapacityReceipt,
) -> bool:
    return (
        int(current.get("enodot_binding_id") or 0) == receipt.enodot_binding_id
        and int(current.get("enodot_account_id") or 0) == receipt.enodot_account_id
        and str(current.get("region") or "").strip().casefold() == receipt.region
        and int(current.get("allocation_version") or 0) == receipt.allocation_version
        and int(current.get("quota") or 0) == receipt.quota
        and int(current.get("occupancy") or 0) == receipt.occupancy
        and str(current.get("occupancy_digest") or "").strip().casefold()
        == receipt.occupancy_digest
        and int(current.get("lease_cursor") or 0) == receipt.lease_cursor
        and str(current.get("redis_restart_epoch") or "")
        == receipt.redis_restart_epoch
        and str(current.get("receipt_state") or "").strip().casefold()
        in {"resyncing", "ready"}
        and current.get("last_error_class") is None
        and _aware(
            current.get("projected_at"),
            "provider_region_capacity_current_projected_at_invalid",
        )
        == receipt.projected_at
    )


def _positive_int(value: object, code: str) -> int:
    if isinstance(value, bool):
        raise ProviderRegionError(code)
    try:
        result = int(value)
    except (TypeError, ValueError):
        raise ProviderRegionError(code) from None
    if result < 1:
        raise ProviderRegionError(code)
    return result


def _nonnegative_int(value: object, code: str) -> int:
    if isinstance(value, bool):
        raise ProviderRegionError(code)
    try:
        result = int(value)
    except (TypeError, ValueError):
        raise ProviderRegionError(code) from None
    if result < 0:
        raise ProviderRegionError(code)
    return result


def _region(value: object, code: str) -> str:
    result = str(value or "").strip().casefold()
    if not _REGION_RE.fullmatch(result):
        raise ProviderRegionError(code)
    return result


def _nonempty(value: object, code: str, maximum: int) -> str:
    result = str(value or "").strip()
    if not result or len(result) > maximum or any(ord(char) < 0x20 for char in result):
        raise ProviderRegionError(code)
    return result


def _hash(value: object, code: str) -> str:
    result = str(value or "").strip().casefold()
    if not _HASH_RE.fullmatch(result):
        raise ProviderRegionError(code)
    return result


def _aware(value: object, code: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ProviderRegionError(code)
    return value.astimezone(timezone.utc)


def _optional_aware(value: object, code: str) -> datetime | None:
    return None if value is None else _aware(value, code)


def _optional_error(value: object) -> str | None:
    if value is None:
        return None
    result = str(value).strip()
    if not result or len(result) > 160 or any(ord(char) < 0x20 for char in result):
        raise ProviderRegionError("provider_region_error_class_invalid")
    return result


def _row(cursor: Any) -> dict[str, Any] | None:
    value = cursor.fetchone()
    if value is None:
        return None
    if isinstance(value, Mapping):
        return dict(value)
    names = [column.name for column in cursor.description or ()]
    return dict(zip(names, value, strict=False))


def _rows(cursor: Any) -> list[dict[str, Any]]:
    values = cursor.fetchall()
    if not values:
        return []
    if isinstance(values[0], Mapping):
        return [dict(value) for value in values]
    names = [column.name for column in cursor.description or ()]
    return [dict(zip(names, value, strict=False)) for value in values]


def _rollback(connection: Any) -> None:
    rollback = getattr(connection, "rollback", None)
    if callable(rollback):
        rollback()


def _close(connection: Any) -> None:
    close = getattr(connection, "close", None)
    if callable(close):
        close()


_CANONICAL_GENERATION_SQL = """
WITH active AS (
    SELECT generation.source_hash
    FROM a6_trade.provider_pool_generations generation
    JOIN a6_trade.provider_cost_generations cost
      ON cost.model = generation.model
     AND cost.cost_generation = generation.cost_generation
    WHERE generation.model = %s
      AND generation.generation = %s
      AND generation.cost_generation = %s
      AND generation.generation_state = 'active'
      AND cost.pricing_state = 'active'
), absent AS (
    SELECT repeat('0', 64)::text AS source_hash
    WHERE NOT EXISTS (
        SELECT 1
        FROM a6_trade.provider_pool_generations generation
        WHERE generation.model = %s
          AND generation.generation_state = 'active'
    )
      AND %s = (
          SELECT coalesce(max(generation) FILTER (WHERE activated_at IS NOT NULL), 0)
          FROM a6_trade.provider_pool_generations
          WHERE model = %s
      )
      AND %s = (
          SELECT coalesce(max(cost_generation) FILTER (WHERE activated_at IS NOT NULL), 0)
          FROM a6_trade.provider_cost_generations
          WHERE model = %s
      )
)
SELECT source_hash FROM active
UNION ALL
SELECT source_hash FROM absent
"""

_CURRENT_PROJECTION_RECEIPT_SQL = """
SELECT *
FROM a6_trade.provider_region_projection_receipts
WHERE region = %s AND model = %s AND projection_type = %s
FOR UPDATE
"""

_INSERT_PROJECTION_RECEIPT_SQL = """
INSERT INTO a6_trade.provider_region_projection_receipts (
    region, model, projection_type, routing_generation, cost_generation,
    source_hash, projection_digest, last_event_id, redis_restart_epoch,
    projection_state, receipt_version, projected_at, verified_at, last_error_class
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
"""

_UPDATE_PROJECTION_RECEIPT_SQL = """
UPDATE a6_trade.provider_region_projection_receipts
SET routing_generation = %s, cost_generation = %s, source_hash = %s,
    projection_digest = %s, last_event_id = %s, redis_restart_epoch = %s,
    projection_state = %s, receipt_version = %s, projected_at = %s,
    verified_at = %s, last_error_class = %s, updated_at = now()
WHERE id = %s AND receipt_version = %s
"""

_ACK_PROJECTION_RECEIPT_SQL = """
UPDATE a6_trade.provider_region_projection_receipts
SET subscriber_cursor = %s, subscriber_connected = true,
    subscriber_verified_at = %s, receipt_version = %s, updated_at = now()
WHERE id = %s AND receipt_version = %s
  AND region = %s AND model = %s AND projection_type = %s
  AND routing_generation = %s AND cost_generation = %s
  AND source_hash = %s AND projection_digest = %s AND last_event_id = %s
  AND redis_restart_epoch = %s AND projection_state = %s
  AND projected_at = %s AND verified_at IS NOT DISTINCT FROM %s
  AND last_error_class IS NULL
"""

_BINDING_CAPACITY_SQL = """
SELECT id, enodot_account_id, desired_concurrency
FROM a6_trade.enodot_account_bindings
WHERE id = %s AND enodot_account_id = %s
FOR SHARE
"""

_CAPACITY_BY_KEY_SQL = """
SELECT allocation_version, allocation_digest, region
FROM a6_trade.provider_region_capacity_allocations
WHERE enodot_account_id = %s AND allocation_key = %s
ORDER BY allocation_version, region
"""

_CURRENT_CAPACITY_SQL = """
SELECT *
FROM a6_trade.provider_region_capacity_allocations
WHERE enodot_account_id = %s AND valid_to IS NULL
ORDER BY region
FOR UPDATE
"""

_CURRENT_CAPACITY_RECEIPTS_SQL = """
SELECT *
FROM a6_trade.provider_region_capacity_receipts
WHERE enodot_account_id = %s
"""

_CLOSE_CAPACITY_SQL = """
UPDATE a6_trade.provider_region_capacity_allocations
SET valid_to = %s, updated_at = now()
WHERE enodot_account_id = %s AND allocation_version = %s AND valid_to IS NULL
"""

_INSERT_CAPACITY_SQL = """
INSERT INTO a6_trade.provider_region_capacity_allocations (
    enodot_binding_id, enodot_account_id, region, quota, account_concurrency,
    allocation_version, allocation_key, allocation_digest, source_hash,
    allocation_state, drain_target_quota, effective_at, drain_started_at,
    reason
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
"""

_CURRENT_CAPACITY_ROW_SQL = """
SELECT *
FROM a6_trade.provider_region_capacity_allocations
WHERE enodot_account_id = %s AND region = %s AND valid_to IS NULL
"""

_CURRENT_CAPACITY_RECEIPT_ROW_SQL = """
SELECT *
FROM a6_trade.provider_region_capacity_receipts
WHERE enodot_account_id = %s AND region = %s
FOR UPDATE
"""

_INSERT_CAPACITY_RECEIPT_SQL = """
INSERT INTO a6_trade.provider_region_capacity_receipts (
    allocation_id, enodot_binding_id, enodot_account_id, region,
    allocation_version, quota, occupancy, occupancy_digest, lease_cursor,
    redis_restart_epoch, receipt_version, projected_at, last_error_class
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
"""

_UPDATE_CAPACITY_RECEIPT_SQL = """
UPDATE a6_trade.provider_region_capacity_receipts
SET allocation_id = %s, allocation_version = %s, quota = %s,
    occupancy = %s, occupancy_digest = %s, lease_cursor = %s,
    redis_restart_epoch = %s, receipt_version = %s, projected_at = %s,
    last_error_class = %s, updated_at = now()
WHERE id = %s AND receipt_version = %s
"""

_ACK_CAPACITY_RECEIPT_SQL = """
UPDATE a6_trade.provider_region_capacity_receipts
SET receipt_state = 'ready', receipt_version = %s, verified_at = %s,
    updated_at = now()
WHERE id = %s AND receipt_version = %s
  AND allocation_id = %s AND enodot_binding_id = %s
  AND enodot_account_id = %s AND region = %s
  AND allocation_version = %s AND quota = %s AND occupancy = %s
  AND occupancy_digest = %s AND lease_cursor = %s
  AND redis_restart_epoch = %s AND projected_at = %s
  AND receipt_state = 'resyncing' AND verified_at IS NULL
  AND last_error_class IS NULL
"""
