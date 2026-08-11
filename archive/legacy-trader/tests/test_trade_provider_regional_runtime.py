from __future__ import annotations

import json
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from a6api_auto.trade_provider_dispatch import ProviderDispatchBundle, ProviderDispatchSlot
from a6api_auto.trade_provider_fast_block import ProviderFastBlockProjection
from a6api_auto.trade_provider_regional_runtime import (
    ProviderCapacityRedisProjection,
    ProviderCapacityRedisSnapshot,
    ProviderFastBlockManifest,
    ProviderRegionalCapacityAllocation,
    ProviderRegionalCapacityState,
    ProviderRegionalNotificationConsumer,
    ProviderRegionalProjectionRuntime,
    ProviderRegionalRuntimeError,
    load_redis_restart_epoch,
)

NOW = datetime(2026, 7, 30, tzinfo=timezone.utc)


def dispatch_bundle() -> ProviderDispatchBundle:
    return ProviderDispatchBundle(
        model="gpt-5.6-sol",
        routing_generation=42,
        cost_generation=19,
        source_hash="b" * 64,
        cost_input_hash="c" * 64,
        cost_output_hash="d" * 64,
        ready_slots=(
            ProviderDispatchSlot(
                slot_no=1,
                account_id=51,
                token_ref_id=41,
                external_token_id=14093,
                channel_id=1008,
                rank=1,
                priority=2,
                cost_multiplier=Decimal("1.25"),
            ),
        ),
    )


def fast_block() -> ProviderFastBlockProjection:
    return ProviderFastBlockProjection(
        event_id=7,
        operation="block",
        model="gpt-5.6-sol",
        routing_generation=42,
        cost_generation=19,
        slot_no=1,
        channel_id=1008,
        projection_key="a6:provider:fast-block:v1:gpt-5.6-sol:42:1:1008",
        payload={
            "failure_owner": "provider",
            "failure_code": "fixed_merchant_unavailable",
        },
    )


def test_fast_block_manifest_has_cross_language_golden_bytes_and_digest() -> None:
    manifest = ProviderFastBlockManifest(
        model="gpt-5.6-sol",
        routing_generation=42,
        cost_generation=19,
        source_hash="b" * 64,
        last_event_id=9,
        entries=(fast_block(),),
    )

    assert manifest.payload() == (
        '{"cost_generation":19,"events":[{"event_id":7,"operation":"block",'
        '"payload_sha256":"5be47d5e3568f0e714187b4b44a969f5e9199568e68f83138225522cfedb5f4d",'
        '"projection_key":"a6:provider:fast-block:v1:gpt-5.6-sol:42:1:1008"}],'
        '"last_event_id":9,"model":"gpt-5.6-sol","routing_generation":42,'
        '"schema_version":1,"source_hash":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb'
        'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"}'
    )
    assert manifest.digest() == "8584c8fe4604928702a8c85aef2b911dd7b55ad00d7c959cd65f35ad5ca45059"


class InfoRedis:
    def __init__(self, info):
        self.value = info
        self.calls = []

    def info(self, section):
        self.calls.append(section)
        return self.value


def test_restart_epoch_comes_only_from_live_info_server() -> None:
    client = InfoRedis({"run_id": "a" * 40})
    assert load_redis_restart_epoch(client) == "a" * 40
    assert client.calls == ["server"]

    with pytest.raises(ProviderRegionalRuntimeError, match="redis_epoch_invalid"):
        load_redis_restart_epoch(InfoRedis({"run_id": ""}))
    with pytest.raises(ProviderRegionalRuntimeError, match="redis_info_invalid"):
        load_redis_restart_epoch(InfoRedis("run_id:a"))


class RecordingRuntime:
    def __init__(self) -> None:
        self.calls = []

    def reconcile_capacity(self, *, region, account_id):
        self.calls.append(("account", region, account_id))
        return {"success": True, "ready": True}

    def reconcile_capacities(self, *, region):
        self.calls.append(("region", region))
        return {"success": True, "ready": True}

    def reconcile(self, model, *, region):
        self.calls.append(("model", region, model))
        return {"success": True, "ready": True}


def capacity_notice(*, origin: str = "request_plane") -> str:
    return json.dumps(
        {
            "schema_version": 1,
            "hint": "exact_reread",
            "projection_type": "capacity",
            "origin": origin,
            "region": "hk",
            "account_id": 51,
            "allocation_version": 3,
            "lease_cursor": 8,
            "state": "ready",
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def test_capacity_notification_uses_immediate_account_refresh() -> None:
    runtime = RecordingRuntime()
    consumer = ProviderRegionalNotificationConsumer(runtime)  # type: ignore[arg-type]

    started = time.monotonic()
    result = consumer.handle(
        "a6:provider:capacity:changed:v1",
        capacity_notice(),
        model="gpt-5.6-sol",
        region="hk",
    )

    assert time.monotonic() - started < 0.250
    assert result["ready"] is True
    assert runtime.calls == [("account", "hk", 51)]


def test_notifications_are_hints_and_malformed_data_fails_closed() -> None:
    runtime = RecordingRuntime()
    consumer = ProviderRegionalNotificationConsumer(runtime)  # type: ignore[arg-type]

    ignored = consumer.handle(
        "a6:provider:capacity:changed:v1",
        capacity_notice(origin="projector"),
        model="gpt-5.6-sol",
        region="hk",
    )
    assert ignored["action"] == "projector-hint-observed"
    assert runtime.calls == []

    consumer.handle(
        "a6:provider:capacity:changed:v1",
        "3:8:ready",
        model="gpt-5.6-sol",
        region="hk",
    )
    assert runtime.calls == [("region", "hk")]

    with pytest.raises(ProviderRegionalRuntimeError, match="notification_invalid"):
        consumer.handle(
            "a6:provider:capacity:changed:v1",
            "not-a-capacity-hint",
            model="gpt-5.6-sol",
            region="hk",
        )


def test_dispatch_notification_triggers_canonical_reread_not_payload_use() -> None:
    runtime = RecordingRuntime()
    consumer = ProviderRegionalNotificationConsumer(runtime)  # type: ignore[arg-type]
    notice = json.dumps(
        {
            "schema_version": 1,
            "model": "gpt-5.6-sol",
            "digest": "payload-is-not-authority",
        }
    )

    consumer.handle(
        "a6:provider:dispatch:changed:v1",
        notice,
        model="gpt-5.6-sol",
        region="us",
    )

    assert runtime.calls == [("model", "us", "gpt-5.6-sol")]


class RuntimeRedis:
    def __init__(self, *epochs: str) -> None:
        self.epochs = list(epochs)

    def info(self, section):
        assert section == "server"
        value = self.epochs.pop(0) if len(self.epochs) > 1 else self.epochs[0]
        return {"run_id": value}


class DispatchSource:
    def __init__(self, value):
        self.value = value

    def load_active(self, _model):
        return self.value


class RegionalSource:
    def __init__(self, manifest):
        self.manifest = manifest
        self.receipts = {}

    def load_projection_receipt(self, region, model, projection_type):
        row = self.receipts.get((region, model, projection_type))
        return None if row is None else dict(row)

    def load_fast_block_manifest(self, _bundle):
        return self.manifest

    def load_capacity_states(self, _region):
        return ()

    def load_capacity_state(self, _region, _account_id):
        return None


class ProjectionStore:
    def __init__(self, source: RegionalSource) -> None:
        self.source = source
        self.receipts = []

    def record_projection_receipt(self, receipt, *, expected_receipt_version):
        self.receipts.append(receipt)
        key = (receipt.region, receipt.model, receipt.projection_type)
        current = self.source.receipts.get(key)
        version = expected_receipt_version + 1
        row = {
            **({} if current is None else current),
            **receipt.__dict__,
            "receipt_version": version,
            "id": 1,
        }
        self.source.receipts[key] = row
        return SimpleNamespace(receipt_version=version)


class ProjectionSink:
    def __init__(self) -> None:
        self.calls = []

    def publish(self, value, **kwargs):
        self.calls.append((value, kwargs))

    def verify(self, value, **kwargs):
        assert (value, kwargs) in self.calls


def acknowledge_all(source: RegionalSource) -> None:
    for row in source.receipts.values():
        row["subscriber_cursor"] = row["last_event_id"]
        row["subscriber_connected"] = True
        row["subscriber_verified_at"] = row["projected_at"] + timedelta(microseconds=1)
        row["receipt_version"] += 1


def runtime_fixture(*epochs: str):
    bundle = dispatch_bundle()
    manifest = ProviderFastBlockManifest(
        model=bundle.model,
        routing_generation=bundle.routing_generation,
        cost_generation=bundle.cost_generation,
        source_hash=bundle.source_hash,
        last_event_id=0,
        entries=(),
    )
    source = RegionalSource(manifest)
    store = ProjectionStore(source)
    dispatch = ProjectionSink()
    fast = ProjectionSink()
    runtime = ProviderRegionalProjectionRuntime(
        redis_client=RuntimeRedis(*epochs),
        dispatch_source=DispatchSource(bundle),
        regional_source=source,
        dispatch_projector=dispatch,  # type: ignore[arg-type]
        fast_block_projector=fast,  # type: ignore[arg-type]
        capacity_projector=SimpleNamespace(),  # type: ignore[arg-type]
        projection_store=store,
        capacity_store=SimpleNamespace(),  # type: ignore[arg-type]
        clock=lambda: NOW,
    )
    return runtime, source, store, dispatch, fast


def test_runtime_records_nonready_receipts_before_exact_projection() -> None:
    runtime, source, store, dispatch, fast = runtime_fixture("epoch-1")

    initial = runtime.reconcile("gpt-5.6-sol", region="hk")

    assert initial["ready"] is False
    assert [receipt.projection_state for receipt in store.receipts] == [
        "resyncing",
        "resyncing",
    ]
    assert all(receipt.redis_restart_epoch == "epoch-1" for receipt in store.receipts)
    assert len(dispatch.calls) == 1
    assert len(fast.calls) == 1

    still_waiting = runtime.reconcile("gpt-5.6-sol", region="hk")
    assert still_waiting["ready"] is False
    assert len(store.receipts) == 2

    acknowledge_all(source)
    finalized = runtime.reconcile("gpt-5.6-sol", region="hk")
    assert finalized["ready"] is True
    assert [receipt.projection_state for receipt in store.receipts] == [
        "resyncing",
        "resyncing",
        "ready",
        "ready",
    ]
    assert all(receipt.subscriber_connected is False for receipt in store.receipts[:2])

    steady = runtime.reconcile("gpt-5.6-sol", region="hk")
    assert steady["ready"] is True
    assert steady["dispatch"]["action"] == "verified"
    assert steady["fast_block"]["action"] == "verified"
    assert len(store.receipts) == 4


def test_runtime_epoch_change_stops_before_ready_receipt() -> None:
    runtime, _source, store, _dispatch, _fast = runtime_fixture("epoch-1", "epoch-2")

    with pytest.raises(ProviderRegionalRuntimeError, match="redis_epoch_changed"):
        runtime.reconcile("gpt-5.6-sol", region="hk")

    assert [receipt.projection_state for receipt in store.receipts] == ["resyncing"]


def capacity_allocation() -> ProviderRegionalCapacityAllocation:
    return ProviderRegionalCapacityAllocation(
        allocation_id=11,
        enodot_binding_id=31,
        enodot_account_id=51,
        region="hk",
        quota=2,
        account_concurrency=10,
        allocation_version=3,
        allocation_digest="a" * 64,
        source_hash="b" * 64,
        allocation_state="active",
        drain_target_quota=None,
        effective_at=NOW,
    )


class CapacitySource:
    def __init__(self, allocation: ProviderRegionalCapacityAllocation) -> None:
        self.allocation = allocation
        self.receipt = None

    def load_capacity_state(self, region, account_id):
        assert (region, account_id) == (
            self.allocation.region,
            self.allocation.enodot_account_id,
        )
        receipt = None if self.receipt is None else dict(self.receipt)
        return ProviderRegionalCapacityState(self.allocation, receipt)

    def load_capacity_states(self, region):
        assert region == self.allocation.region
        return (self.load_capacity_state(region, self.allocation.enodot_account_id),)


class CapacityStore:
    def __init__(self, source: CapacitySource) -> None:
        self.source = source
        self.receipts = []

    def record_capacity_receipt(self, receipt, *, expected_receipt_version):
        current_version = (
            0
            if self.source.receipt is None
            else int(self.source.receipt["receipt_version"])
        )
        assert expected_receipt_version == current_version
        self.receipts.append(receipt)
        version = current_version + 1
        self.source.receipt = {**receipt.__dict__, "receipt_version": version}
        return SimpleNamespace(receipt_version=version)

    def acknowledge(self, verified_at: datetime) -> None:
        assert self.source.receipt is not None
        self.source.receipt.update(
            receipt_state="ready",
            receipt_version=int(self.source.receipt["receipt_version"]) + 1,
            verified_at=verified_at,
        )


class CapacityProjector:
    def __init__(self, *, epoch_changed: bool, restart_reconciled: bool) -> None:
        self.first_epoch_changed = epoch_changed
        self.restart_reconciled = restart_reconciled
        self.projection = None
        self.begin_calls = 0
        self.seal_calls = 0
        self.finalize_calls = 0
        self.verify_calls = 0

    def read(self, _allocation, *, redis_restart_epoch):
        assert redis_restart_epoch == "epoch-1"
        return self.projection

    def begin(self, allocation, *, redis_restart_epoch):
        assert redis_restart_epoch == "epoch-1"
        self.begin_calls += 1
        snapshot = ProviderCapacityRedisSnapshot(
            redis_restart_epoch=redis_restart_epoch,
            lease_cursor=0,
            occupancy=0,
            occupancy_digest=(
                "0edb284731a0b513246c742000c83ba8461eb484f8aa1cb4949a97d286c684ac"
            ),
            leases=(),
            identities=(),
            epoch_changed=self.first_epoch_changed and self.begin_calls == 1,
            restart_reconciled=self.restart_reconciled,
        )
        old_version = 0 if self.projection is None else self.projection.receipt_version
        self.projection = ProviderCapacityRedisProjection(
            snapshot=snapshot,
            state="resyncing",
            receipt_version=old_version,
            receipt_dirty=True,
        )
        return snapshot

    def seal(self, _allocation, snapshot, *, receipt_version):
        assert self.projection is not None and self.projection.receipt_dirty
        assert receipt_version > self.projection.receipt_version
        self.seal_calls += 1
        self.projection = ProviderCapacityRedisProjection(
            snapshot=snapshot,
            state="resyncing",
            receipt_version=receipt_version,
            receipt_dirty=False,
        )

    def finalize(self, allocation, snapshot, *, receipt_version):
        assert self.projection is not None
        assert self.projection.state == "resyncing"
        assert not self.projection.receipt_dirty
        assert snapshot.restart_reconciled
        assert receipt_version == self.projection.receipt_version + 1
        self.finalize_calls += 1
        self.projection = ProviderCapacityRedisProjection(
            snapshot=snapshot,
            state=(
                "ready"
                if allocation.allocation_state == "active"
                else allocation.allocation_state
            ),
            receipt_version=receipt_version,
            receipt_dirty=False,
        )

    def verify(self, _allocation, snapshot, *, receipt_version, final_state):
        assert self.projection == ProviderCapacityRedisProjection(
            snapshot=snapshot,
            state=final_state,
            receipt_version=receipt_version,
            receipt_dirty=False,
        )
        self.verify_calls += 1

    def mark_restart_reconciled(self) -> None:
        self.restart_reconciled = True
        assert self.projection is not None
        self.projection = replace(
            self.projection,
            snapshot=replace(self.projection.snapshot, restart_reconciled=True),
        )


def capacity_runtime_fixture(
    *,
    epoch_changed: bool = False,
    restart_reconciled: bool = True,
    clock=lambda: NOW,
):
    allocation = capacity_allocation()
    source = CapacitySource(allocation)
    store = CapacityStore(source)
    projector = CapacityProjector(
        epoch_changed=epoch_changed,
        restart_reconciled=restart_reconciled,
    )
    runtime = ProviderRegionalProjectionRuntime(
        redis_client=RuntimeRedis("epoch-1"),
        dispatch_source=SimpleNamespace(),  # type: ignore[arg-type]
        regional_source=source,  # type: ignore[arg-type]
        dispatch_projector=SimpleNamespace(),  # type: ignore[arg-type]
        fast_block_projector=SimpleNamespace(),  # type: ignore[arg-type]
        capacity_projector=projector,  # type: ignore[arg-type]
        projection_store=SimpleNamespace(),  # type: ignore[arg-type]
        capacity_store=store,
        restart_grace_seconds=60,
        clock=clock,
    )
    return runtime, source, store, projector


def test_capacity_runtime_seals_waits_for_ack_then_finalizes_once() -> None:
    runtime, source, store, projector = capacity_runtime_fixture()

    initial = runtime.reconcile_capacity(region="hk", account_id=51)
    assert initial["ready"] is False
    assert initial["capacity"][0]["action"] == "sealed"
    assert [receipt.receipt_state for receipt in store.receipts] == ["resyncing"]
    assert projector.projection.state == "resyncing"
    assert projector.projection.receipt_version == 1

    waiting = runtime.reconcile_capacity(region="hk", account_id=51)
    assert waiting["capacity"][0]["action"] == "awaiting-subscriber"
    assert len(store.receipts) == 1
    assert projector.seal_calls == 1

    store.acknowledge(source.receipt["projected_at"] + timedelta(microseconds=1))
    finalized = runtime.reconcile_capacity(region="hk", account_id=51)
    assert finalized["ready"] is True
    assert finalized["capacity"][0]["action"] == "finalized"
    assert projector.finalize_calls == 1
    assert projector.projection.receipt_version == 2

    steady = runtime.reconcile_capacity(region="hk", account_id=51)
    assert steady["ready"] is True
    assert steady["capacity"][0]["action"] == "verified"
    assert len(store.receipts) == 1
    assert projector.seal_calls == 1
    assert projector.finalize_calls == 1


def test_capacity_restart_grace_never_trusts_empty_snapshot_without_reconcile() -> None:
    now = [NOW]
    runtime, source, store, projector = capacity_runtime_fixture(
        epoch_changed=True,
        restart_reconciled=False,
        clock=lambda: now[0],
    )

    initial = runtime.reconcile_capacity(region="hk", account_id=51)
    assert initial["ready"] is False
    assert store.receipts[0].last_error_class == "redis_restart_grace"

    now[0] = NOW + timedelta(seconds=59)
    grace = runtime.reconcile_capacity(region="hk", account_id=51)
    assert grace["capacity"][0]["action"] == "restart-grace"
    assert len(store.receipts) == 1

    now[0] = NOW + timedelta(minutes=10)
    unreconciled = runtime.reconcile_capacity(region="hk", account_id=51)
    assert unreconciled["capacity"][0]["action"] == "awaiting-durable-reconcile"
    assert unreconciled["ready"] is False
    assert len(store.receipts) == 1

    projector.mark_restart_reconciled()
    cleared = runtime.reconcile_capacity(region="hk", account_id=51)
    assert cleared["capacity"][0]["action"] == "sealed"
    assert len(store.receipts) == 2
    assert store.receipts[-1].last_error_class is None
    assert projector.projection.receipt_version == 2

    store.acknowledge(source.receipt["projected_at"] + timedelta(microseconds=1))
    ready = runtime.reconcile_capacity(region="hk", account_id=51)
    assert ready["ready"] is True
    assert projector.projection.receipt_version == 3


class PollPubSub:
    def __init__(self) -> None:
        self.polls = 0

    def subscribe(self, *_channels):
        return None

    def get_message(self, *, timeout):
        assert timeout == 0.05
        self.polls += 1
        if self.polls == 1:
            return None
        raise KeyboardInterrupt

    def close(self):
        return None


class PollRedis:
    def __init__(self, pubsub: PollPubSub) -> None:
        self.value = pubsub

    def pubsub(self, *, ignore_subscribe_messages):
        assert ignore_subscribe_messages is True
        return self.value


def test_watch_polls_projector_after_ack_without_a_second_hint() -> None:
    runtime = RecordingRuntime()
    pubsub = PollPubSub()
    consumer = ProviderRegionalNotificationConsumer(runtime)  # type: ignore[arg-type]

    with pytest.raises(KeyboardInterrupt):
        consumer.run_forever(
            PollRedis(pubsub),
            model="gpt-5.6-sol",
            region="hk",
            poll_seconds=0.05,
        )

    assert runtime.calls == [
        ("model", "hk", "gpt-5.6-sol"),
        ("model", "hk", "gpt-5.6-sol"),
    ]
