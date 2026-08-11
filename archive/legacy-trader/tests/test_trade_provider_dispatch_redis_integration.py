from __future__ import annotations

import json
import shutil
import socket
import subprocess
import time
from contextlib import suppress
from dataclasses import replace
from decimal import Decimal

import pytest

from a6api_auto.trade_provider_dispatch import (
    ProviderDispatchBundle,
    ProviderDispatchError,
    ProviderDispatchSlot,
    RedisProviderDispatchSink,
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


def active_bundle() -> ProviderDispatchBundle:
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


def test_real_redis_repairs_corruption_and_enforces_tombstone_order(redis_client) -> None:
    bundle = active_bundle()
    sink = RedisProviderDispatchSink(redis_client)

    first = sink.publish(bundle)
    redis_client.set(sink.key(bundle.model), "{corrupt")
    repaired = sink.publish(bundle)
    tombstone = ProviderDispatchBundle.tombstone(
        bundle.model,
        routing_generation=bundle.routing_generation,
        cost_generation=bundle.cost_generation,
    )
    removed = sink.publish(tombstone)
    with pytest.raises(
        ProviderDispatchError,
        match="provider_dispatch_redis_stale_generation",
    ):
        sink.publish(bundle)
    newer = replace(
        bundle,
        routing_generation=43,
        cost_generation=20,
        source_hash="e" * 64,
    )
    restored = sink.publish(newer)

    stored = json.loads(redis_client.get(sink.key(bundle.model)))
    assert first.changed is True
    assert repaired.changed is True
    assert removed.changed is True
    assert restored.changed is True
    assert stored["state"] == "active"
    assert (stored["routing_generation"], stored["cost_generation"]) == (43, 20)
