from __future__ import annotations

import json
import shutil
import socket
import subprocess
import time
from contextlib import suppress
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from a6api_auto.trade_provider_dispatch import (
    ProviderDispatchBundle,
    ProviderDispatchSlot,
    RedisProviderDispatchSink,
)
from a6api_auto.trade_provider_fast_block import (
    ProviderFastBlockProjection,
    RedisProviderFastBlockProjector,
)
from a6api_auto.trade_provider_regional_runtime import (
    ProviderFastBlockManifest,
    ProviderRegionalCapacityAllocation,
    ProviderRegionalRuntimeError,
    RedisProviderRegionalCapacityProjector,
    RedisProviderRegionalDispatchProjector,
    RedisProviderRegionalFastBlockProjector,
    load_redis_restart_epoch,
)

redis = pytest.importorskip("redis")


@pytest.fixture
def redis_client(tmp_path):
    executable = shutil.which("redis-server")
    if executable is None:
        pytest.skip("redis-server is not installed")
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    process = subprocess.Popen(
        [
            executable,
            "--port",
            str(port),
            "--bind",
            "127.0.0.1",
            "--protected-mode",
            "yes",
            "--save",
            "",
            "--appendonly",
            "no",
            "--dir",
            str(tmp_path),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    client = redis.Redis(host="127.0.0.1", port=port, decode_responses=True)
    try:
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            try:
                if client.ping():
                    break
            except redis.RedisError:
                time.sleep(0.02)
        else:
            pytest.fail("ephemeral redis-server did not start")
        yield client
    finally:
        with suppress(redis.RedisError):
            client.shutdown(nosave=True)
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.terminate()
            process.wait(timeout=3)


def bundle() -> ProviderDispatchBundle:
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
        payload={"failure_owner": "provider", "failure_code": "blocked"},
    )


def allocation() -> ProviderRegionalCapacityAllocation:
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
        effective_at=datetime(2026, 7, 30, tzinfo=timezone.utc),
    )


def test_real_redis_dispatch_and_fast_block_exact_meta(redis_client) -> None:
    epoch = load_redis_restart_epoch(redis_client)
    dispatch_sink = RedisProviderDispatchSink(redis_client)
    dispatch = RedisProviderRegionalDispatchProjector(
        redis_client,
        dispatch_sink,
        key_prefix="a6:provider:dispatch:v1",
    )
    dispatch.publish(bundle(), region="hk", redis_restart_epoch=epoch)
    dispatch_meta = json.loads(redis_client.get(dispatch.meta_key(bundle().model)))
    assert dispatch_meta["redis_restart_epoch"] == epoch
    assert dispatch_meta["projection_digest"] == bundle().digest()

    entry = fast_block()
    manifest = ProviderFastBlockManifest(
        model=entry.model,
        routing_generation=entry.routing_generation,
        cost_generation=entry.cost_generation,
        source_hash="b" * 64,
        last_event_id=entry.event_id,
        entries=(entry,),
    )
    base = "a6:provider:fast-block:v1"
    stale = f"{base}:{entry.model}:41:1:999"
    redis_client.sadd(f"{base}:{entry.model}:keys", stale)
    redis_client.set(stale, "stale")
    redis_client.set(f"{stale}:cursor", 1)
    fast = RedisProviderRegionalFastBlockProjector(
        redis_client,
        RedisProviderFastBlockProjector(redis_client),
    )
    fast.publish(manifest, region="hk", redis_restart_epoch=epoch)

    assert redis_client.smembers(fast.index_key(entry.model)) == {entry.projection_key}
    assert redis_client.get(stale) is None
    assert redis_client.get(f"{stale}:cursor") is None
    assert redis_client.get(fast.manifest_key(entry.model)) == manifest.payload()
    assert (
        json.loads(redis_client.get(fast.meta_key(entry.model)))["projection_digest"]
        == manifest.digest()
    )


def lease_identity(*, request_id: str = "request-a") -> str:
    return json.dumps(
        {
            "schema_version": 1,
            "lease_id": "lease-a",
            "request_id": request_id,
            "attempt_id": "request-a",
            "dispatch_owner_id": "hk:00000000-0000-0000-0000-000000000001",
            "account_id": 51,
            "region": "hk",
            "allocation_version": 3,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def test_real_redis_capacity_requires_seal_ack_and_finalize(redis_client) -> None:
    projector = RedisProviderRegionalCapacityProjector(
        redis_client,
        lease_ttl_seconds=60,
    )
    epoch = load_redis_restart_epoch(redis_client)
    current = allocation()
    first = projector.begin(current, redis_restart_epoch=epoch)
    assert first.epoch_changed is True
    assert first.restart_reconciled is False
    assert first.occupancy == 0
    projector.seal(current, first, receipt_version=1)

    leases_key, meta_key = projector.keys("hk", 51)
    identities_key = projector.identities_key("hk", 51)
    assert redis_client.hget(meta_key, "state") == "resyncing"
    assert redis_client.hget(meta_key, "receipt_dirty") == "0"
    assert redis_client.hget(meta_key, "receipt_version") == "1"
    with pytest.raises(ProviderRegionalRuntimeError, match="restart_reconcile_pending"):
        projector.finalize(current, first, receipt_version=2)

    redis_client.hset(meta_key, "reconciled_restart_epoch", epoch)
    acknowledged = projector.read(current, redis_restart_epoch=epoch)
    assert acknowledged is not None
    assert acknowledged.snapshot.restart_reconciled is True
    projector.finalize(current, acknowledged.snapshot, receipt_version=2)
    assert redis_client.hget(meta_key, "state") == "ready"
    assert redis_client.hget(meta_key, "receipt_version") == "2"

    redis_client.zadd(leases_key, {"request-a": 1})
    redis_client.hset(identities_key, "request-a", lease_identity())
    redis_client.hset(
        meta_key,
        mapping={"lease_cursor": 1, "occupancy": 1, "receipt_dirty": 1},
    )

    dirty = projector.read(current, redis_restart_epoch=epoch)
    assert dirty is not None
    assert dirty.receipt_dirty is True
    second = projector.begin(current, redis_restart_epoch=epoch)
    assert second.epoch_changed is False
    assert second.restart_reconciled is True
    assert second.leases == (("request-a", "1"),)
    assert second.occupancy == 1
    assert (
        second.occupancy_digest
        == "c502da02824b491aaf3ffaecd4ae8e502ccf1d011494ae3a6fcac431349bab4b"
    )
    projector.seal(current, second, receipt_version=3)
    sealed = projector.read(current, redis_restart_epoch=epoch)
    assert sealed is not None
    projector.finalize(current, sealed.snapshot, receipt_version=4)
    meta = redis_client.hgetall(meta_key)
    assert meta["state"] == "ready"
    assert meta["receipt_version"] == "4"
    assert meta["receipt_dirty"] == "0"
    assert meta["occupancy_digest"] == second.occupancy_digest
    assert redis_client.zscore(leases_key, "request-a") == 1.0
    assert projector.read(current, redis_restart_epoch=epoch).snapshot == second


def test_real_redis_capacity_seal_and_finalize_reject_exact_snapshot_conflicts(
    redis_client,
) -> None:
    projector = RedisProviderRegionalCapacityProjector(redis_client, lease_ttl_seconds=60)
    epoch = load_redis_restart_epoch(redis_client)
    current = allocation()
    leases_key, meta_key = projector.keys("hk", 51)
    identities_key = projector.identities_key("hk", 51)

    projector.begin(current, redis_restart_epoch=epoch)
    redis_client.hset(meta_key, "reconciled_restart_epoch", epoch)
    redis_client.zadd(leases_key, {"request-a": 1})
    redis_client.hset(identities_key, "request-a", lease_identity())
    redis_client.hset(meta_key, mapping={"lease_cursor": 1, "occupancy": 1})
    snapshot = projector.begin(current, redis_restart_epoch=epoch)

    redis_client.hset(
        identities_key,
        "request-a",
        lease_identity(request_id="changed-after-begin"),
    )
    with pytest.raises(ProviderRegionalRuntimeError, match="concurrent_change"):
        projector.seal(current, snapshot, receipt_version=1)

    redis_client.hset(identities_key, "request-a", dict(snapshot.identities)["request-a"])
    projector.seal(current, snapshot, receipt_version=1)
    sealed = projector.read(current, redis_restart_epoch=epoch)
    assert sealed is not None
    redis_client.zadd(leases_key, {"request-a": 2})
    with pytest.raises(ProviderRegionalRuntimeError, match="concurrent_change"):
        projector.finalize(current, sealed.snapshot, receipt_version=2)


def test_real_redis_capacity_identity_collection_fails_closed(redis_client) -> None:
    projector = RedisProviderRegionalCapacityProjector(redis_client, lease_ttl_seconds=60)
    epoch = load_redis_restart_epoch(redis_client)
    current = allocation()
    leases_key, meta_key = projector.keys("hk", 51)
    identities_key = projector.identities_key("hk", 51)

    projector.begin(current, redis_restart_epoch=epoch)
    redis_client.zadd(leases_key, {"untracked-mutation": 2})
    with pytest.raises(ProviderRegionalRuntimeError, match="metadata_corrupt"):
        projector.begin(current, redis_restart_epoch=epoch)

    redis_client.zrem(leases_key, "untracked-mutation")
    redis_client.zadd(leases_key, {"request-a": 1})
    redis_client.hset(identities_key, "request-a", "not-json")
    redis_client.hset(meta_key, "occupancy", 1)
    with pytest.raises(ProviderRegionalRuntimeError, match="metadata_corrupt"):
        projector.begin(current, redis_restart_epoch=epoch)

    wrong_region = json.loads(lease_identity())
    wrong_region["region"] = "us"
    redis_client.hset(
        identities_key,
        "request-a",
        json.dumps(wrong_region, sort_keys=True, separators=(",", ":")),
    )
    with pytest.raises(ProviderRegionalRuntimeError, match="metadata_corrupt"):
        projector.begin(current, redis_restart_epoch=epoch)

    wrong_owner = json.loads(lease_identity())
    wrong_owner["dispatch_owner_id"] = "hk:NOT-A-CANONICAL-UUID"
    redis_client.hset(
        identities_key,
        "request-a",
        json.dumps(wrong_owner, sort_keys=True, separators=(",", ":")),
    )
    with pytest.raises(ProviderRegionalRuntimeError, match="metadata_corrupt"):
        projector.begin(current, redis_restart_epoch=epoch)
