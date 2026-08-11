from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timedelta, timezone

import pytest

from a6api_auto.trade_provider_region import (
    ProviderRegionCapacityPlan,
    ProviderRegionCapacityReceipt,
    ProviderRegionCapacityTarget,
    ProviderRegionError,
    ProviderRegionProjectionReceipt,
    ProviderRegionProjectionSubscriberAck,
    validate_capacity_receipt_transition,
    validate_capacity_transition,
    validate_projection_subscriber_ack,
    validate_projection_transition,
)

UTC = timezone.utc
NOW = datetime(2026, 7, 30, 6, tzinfo=UTC)
HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64


def _projection(**changes: object) -> ProviderRegionProjectionReceipt:
    values: dict[str, object] = {
        "region": "us",
        "model": "gpt-5.6-sol",
        "projection_type": "dispatch",
        "routing_generation": 1,
        "cost_generation": 1,
        "source_hash": HASH_A,
        "projection_digest": HASH_B,
        "redis_restart_epoch": "epoch-1",
        "projected_at": NOW,
        "last_event_id": 7,
        "subscriber_cursor": 7,
        "subscriber_connected": True,
        "projection_state": "ready",
        "subscriber_verified_at": NOW,
        "verified_at": NOW,
    }
    values.update(changes)
    return ProviderRegionProjectionReceipt(**values)  # type: ignore[arg-type]


def _projection_row(
    receipt: ProviderRegionProjectionReceipt,
    *,
    receipt_version: int,
) -> dict[str, object]:
    return {**asdict(receipt), "receipt_version": receipt_version, "id": 1}


def _projection_ack(
    receipt: ProviderRegionProjectionReceipt,
    *,
    verified_at: datetime,
) -> ProviderRegionProjectionSubscriberAck:
    return ProviderRegionProjectionSubscriberAck(
        region=receipt.region,
        model=receipt.model,
        projection_type=receipt.projection_type,
        routing_generation=receipt.routing_generation,
        cost_generation=receipt.cost_generation,
        source_hash=receipt.source_hash,
        projection_digest=receipt.projection_digest,
        last_event_id=receipt.last_event_id,
        redis_restart_epoch=receipt.redis_restart_epoch,
        subscriber_cursor=receipt.last_event_id,
        subscriber_verified_at=verified_at,
    )


def _capacity_plan(
    *targets: ProviderRegionCapacityTarget,
    concurrency: int = 10,
    key: str = "allocation-1",
) -> ProviderRegionCapacityPlan:
    return ProviderRegionCapacityPlan(
        enodot_binding_id=31,
        enodot_account_id=41,
        account_concurrency=concurrency,
        allocation_key=key,
        source_hash=HASH_A,
        reason="regional escrow test",
        effective_at=NOW,
        targets=targets,
    )


def _capacity_receipt(**changes: object) -> ProviderRegionCapacityReceipt:
    values: dict[str, object] = {
        "enodot_binding_id": 31,
        "enodot_account_id": 41,
        "region": "us",
        "allocation_version": 2,
        "quota": 8,
        "occupancy": 4,
        "occupancy_digest": HASH_A,
        "lease_cursor": 11,
        "redis_restart_epoch": "epoch-1",
        "projected_at": NOW,
        "receipt_state": "ready",
        "verified_at": NOW,
    }
    values.update(changes)
    return ProviderRegionCapacityReceipt(**values)  # type: ignore[arg-type]


def _capacity_receipt_row(
    receipt: ProviderRegionCapacityReceipt,
    *,
    receipt_version: int,
) -> dict[str, object]:
    return {**asdict(receipt), "receipt_version": receipt_version, "id": 1}


def test_projection_receipt_requires_initial_resync_and_exact_cas() -> None:
    with pytest.raises(
        ProviderRegionError,
        match="provider_region_receipt_initial_resync_required",
    ):
        validate_projection_transition(None, _projection(), expected_receipt_version=0)

    initial = _projection(
        subscriber_cursor=0,
        subscriber_connected=False,
        projection_state="resyncing",
        subscriber_verified_at=None,
        verified_at=None,
    )
    assert validate_projection_transition(None, initial, expected_receipt_version=0) == "insert"
    with pytest.raises(ProviderRegionError, match="provider_region_receipt_cas_mismatch"):
        validate_projection_transition(None, initial, expected_receipt_version=1)


def test_projection_receipt_is_monotonic_and_rejects_same_generation_digest_conflict() -> None:
    current_receipt = _projection()
    current = _projection_row(current_receipt, receipt_version=4)

    assert (
        validate_projection_transition(
            current,
            current_receipt,
            expected_receipt_version=4,
        )
        == "dedupe"
    )
    with pytest.raises(ProviderRegionError, match="provider_region_receipt_cas_mismatch"):
        validate_projection_transition(current, current_receipt, expected_receipt_version=3)
    with pytest.raises(ProviderRegionError, match="provider_region_receipt_stale_generation"):
        validate_projection_transition(
            {**current, "routing_generation": 2},
            _projection(),
            expected_receipt_version=4,
        )
    with pytest.raises(ProviderRegionError, match="provider_region_receipt_digest_conflict"):
        validate_projection_transition(
            current,
            _projection(projection_digest=HASH_C),
            expected_receipt_version=4,
        )

    routing_only_advance = _projection(
        routing_generation=2,
        cost_generation=1,
        source_hash=HASH_C,
        projection_digest=HASH_C,
        subscriber_cursor=0,
        subscriber_connected=False,
        projection_state="projecting",
        subscriber_verified_at=None,
        verified_at=None,
        projected_at=NOW + timedelta(seconds=1),
    )
    assert (
        validate_projection_transition(
            current,
            routing_only_advance,
            expected_receipt_version=4,
        )
        == "advance"
    )

    with pytest.raises(
        ProviderRegionError,
        match="provider_region_reprojection_ack_reset_required",
    ):
        validate_projection_transition(
            current,
            _projection(
                routing_generation=2,
                source_hash=HASH_C,
                projection_digest=HASH_C,
                projection_state="projecting",
                subscriber_verified_at=None,
                verified_at=None,
                projected_at=NOW + timedelta(seconds=1),
            ),
            expected_receipt_version=4,
        )


def test_fast_block_digest_may_change_only_with_a_new_event() -> None:
    current_receipt = _projection(projection_type="fast_block")
    current = _projection_row(current_receipt, receipt_version=2)

    with pytest.raises(ProviderRegionError, match="provider_region_receipt_digest_conflict"):
        validate_projection_transition(
            current,
            _projection(projection_type="fast_block", projection_digest=HASH_C),
            expected_receipt_version=2,
        )

    reprojection = _projection(
        projection_type="fast_block",
        projection_digest=HASH_C,
        last_event_id=8,
        subscriber_cursor=0,
        subscriber_connected=False,
        projection_state="projecting",
        projected_at=NOW + timedelta(seconds=1),
        subscriber_verified_at=None,
        verified_at=None,
    )
    assert (
        validate_projection_transition(
            current,
            reprojection,
            expected_receipt_version=2,
        )
        == "advance"
    )


def test_projection_restart_epoch_resets_cursor_before_readiness() -> None:
    current = _projection_row(_projection(), receipt_version=4)

    with pytest.raises(ProviderRegionError, match="provider_region_restart_resync_required"):
        validate_projection_transition(
            current,
            _projection(redis_restart_epoch="epoch-2"),
            expected_receipt_version=4,
        )

    resync = _projection(
        redis_restart_epoch="epoch-2",
        subscriber_cursor=0,
        subscriber_connected=False,
        projection_state="resyncing",
        projected_at=NOW + timedelta(seconds=1),
        subscriber_verified_at=None,
        verified_at=None,
    )
    assert validate_projection_transition(current, resync, expected_receipt_version=4) == "advance"

    resync_row = _projection_row(resync, receipt_version=5)
    subscriber_verified_at = NOW + timedelta(seconds=2)
    ack = _projection_ack(resync, verified_at=subscriber_verified_at)
    assert (
        validate_projection_subscriber_ack(
            resync_row,
            ack,
            expected_receipt_version=5,
        )
        == "ack"
    )
    acked_row = {
        **resync_row,
        "subscriber_cursor": 7,
        "subscriber_connected": True,
        "subscriber_verified_at": subscriber_verified_at,
        "receipt_version": 6,
    }
    ready = _projection(
        redis_restart_epoch="epoch-2",
        projected_at=NOW + timedelta(seconds=1),
        subscriber_verified_at=subscriber_verified_at,
        verified_at=NOW + timedelta(seconds=3),
    )
    assert (
        validate_projection_transition(acked_row, ready, expected_receipt_version=6) == "advance"
    )


def test_dispatch_tombstone_supports_zero_cursor_and_one_active_to_absent_transition() -> None:
    initial = _projection(
        routing_generation=0,
        cost_generation=0,
        source_hash="0" * 64,
        projection_digest=HASH_C,
        last_event_id=0,
        subscriber_cursor=0,
        subscriber_connected=False,
        projection_state="resyncing",
        subscriber_verified_at=None,
        verified_at=None,
    )
    assert validate_projection_transition(None, initial, expected_receipt_version=0) == "insert"

    active = _projection()
    current = _projection_row(active, receipt_version=3)
    tombstone = _projection(
        source_hash="0" * 64,
        projection_digest=HASH_C,
        subscriber_cursor=0,
        subscriber_connected=False,
        projection_state="resyncing",
        subscriber_verified_at=None,
        verified_at=None,
        projected_at=NOW + timedelta(seconds=1),
    )
    assert (
        validate_projection_transition(current, tombstone, expected_receipt_version=3) == "advance"
    )
    with pytest.raises(ProviderRegionError, match="provider_region_receipt_digest_conflict"):
        validate_projection_transition(
            _projection_row(tombstone, receipt_version=4),
            _projection(
                source_hash="0" * 64,
                projection_digest=HASH_B,
                subscriber_connected=False,
                projection_state="resyncing",
                subscriber_verified_at=None,
                verified_at=None,
                projected_at=NOW + timedelta(seconds=2),
            ),
            expected_receipt_version=4,
        )


def test_receipt_heartbeat_advances_without_cursor_or_digest_change() -> None:
    current_projection = _projection()
    projection_heartbeat = _projection(
        projected_at=NOW + timedelta(seconds=1),
        subscriber_verified_at=NOW + timedelta(seconds=1),
        verified_at=NOW + timedelta(seconds=1),
    )
    assert (
        validate_projection_transition(
            _projection_row(current_projection, receipt_version=4),
            projection_heartbeat,
            expected_receipt_version=4,
        )
        == "advance"
    )

    current_capacity = _capacity_receipt()
    capacity_heartbeat = _capacity_receipt(
        projected_at=NOW + timedelta(seconds=1),
        verified_at=NOW + timedelta(seconds=1),
    )
    assert (
        validate_capacity_receipt_transition(
            _capacity_receipt_row(current_capacity, receipt_version=4),
            capacity_heartbeat,
            expected_receipt_version=4,
        )
        == "advance"
    )


def test_capacity_plan_enforces_sum_and_represents_disabled_zero_concurrency() -> None:
    disabled = _capacity_plan(
        ProviderRegionCapacityTarget("hk", 0, "disabled"),
        ProviderRegionCapacityTarget("us", 0, "disabled"),
        concurrency=0,
    )
    assert disabled.account_concurrency == 0
    assert len(disabled.allocation_digest) == 64

    with pytest.raises(ProviderRegionError, match="provider_region_capacity_overallocated"):
        _capacity_plan(
            ProviderRegionCapacityTarget("hk", 4, "active"),
            ProviderRegionCapacityTarget("us", 7, "active"),
        )
    with pytest.raises(ProviderRegionError, match="provider_region_capacity_region_set_invalid"):
        _capacity_plan(ProviderRegionCapacityTarget("us", 10, "active"))


def test_capacity_transfer_requires_prior_drain_and_verified_occupancy() -> None:
    active = (
        {
            "region": "hk",
            "quota": 2,
            "account_concurrency": 10,
            "allocation_version": 1,
            "allocation_state": "active",
            "drain_target_quota": None,
            "drain_started_at": None,
        },
        {
            "region": "us",
            "quota": 8,
            "account_concurrency": 10,
            "allocation_version": 1,
            "allocation_state": "active",
            "drain_target_quota": None,
            "drain_started_at": None,
        },
    )
    transferred = _capacity_plan(
        ProviderRegionCapacityTarget("hk", 5, "active"),
        ProviderRegionCapacityTarget("us", 5, "active"),
    )
    with pytest.raises(ProviderRegionError, match="provider_region_capacity_drain_required"):
        validate_capacity_transition(active, transferred)

    draining_plan = _capacity_plan(
        ProviderRegionCapacityTarget("hk", 2, "active"),
        ProviderRegionCapacityTarget("us", 8, "draining", 5),
    )
    validate_capacity_transition(active, draining_plan)

    draining = (
        {**active[0], "allocation_version": 2},
        {
            **active[1],
            "allocation_version": 2,
            "allocation_state": "draining",
            "drain_target_quota": 5,
            "drain_started_at": NOW,
        },
    )
    receipt = {
        "region": "us",
        "quota": 8,
        "allocation_version": 2,
        "occupancy": 6,
        "receipt_state": "ready",
        "verified_at": NOW + timedelta(seconds=1),
    }
    with pytest.raises(ProviderRegionError, match="provider_region_capacity_drain_not_complete"):
        validate_capacity_transition(draining, transferred, current_receipts={"us": receipt})

    receipt["occupancy"] = 5
    validate_capacity_transition(draining, transferred, current_receipts={"us": receipt})


def test_capacity_receipt_restart_and_allocation_transitions_are_monotonic() -> None:
    initial = _capacity_receipt(receipt_state="resyncing", verified_at=None)
    assert (
        validate_capacity_receipt_transition(
            None,
            initial,
            expected_receipt_version=0,
        )
        == "insert"
    )
    with pytest.raises(
        ProviderRegionError,
        match="provider_region_capacity_initial_resync_required",
    ):
        validate_capacity_receipt_transition(
            None,
            _capacity_receipt(),
            expected_receipt_version=0,
        )

    current_receipt = _capacity_receipt()
    current = _capacity_receipt_row(current_receipt, receipt_version=3)
    with pytest.raises(ProviderRegionError, match="provider_region_capacity_digest_conflict"):
        validate_capacity_receipt_transition(
            current,
            _capacity_receipt(occupancy_digest=HASH_B),
            expected_receipt_version=3,
        )

    next_allocation = _capacity_receipt(
        allocation_version=3,
        quota=5,
        occupancy_digest=HASH_B,
        projected_at=NOW + timedelta(seconds=1),
        verified_at=NOW + timedelta(seconds=1),
    )
    assert (
        validate_capacity_receipt_transition(
            current,
            next_allocation,
            expected_receipt_version=3,
        )
        == "advance"
    )

    with pytest.raises(
        ProviderRegionError,
        match="provider_region_capacity_restart_resync_required",
    ):
        validate_capacity_receipt_transition(
            current,
            _capacity_receipt(redis_restart_epoch="epoch-2"),
            expected_receipt_version=3,
        )
    with pytest.raises(
        ProviderRegionError,
        match="provider_region_capacity_restart_cursor_invalid",
    ):
        validate_capacity_receipt_transition(
            current,
            _capacity_receipt(
                redis_restart_epoch="epoch-2",
                receipt_state="resyncing",
                verified_at=None,
            ),
            expected_receipt_version=3,
        )

    restart = _capacity_receipt(
        redis_restart_epoch="epoch-2",
        lease_cursor=0,
        receipt_state="resyncing",
        projected_at=NOW + timedelta(seconds=1),
        verified_at=None,
    )
    assert (
        validate_capacity_receipt_transition(
            current,
            restart,
            expected_receipt_version=3,
        )
        == "advance"
    )
