from __future__ import annotations

import json
import shutil
import socket
import subprocess
import time
from contextlib import suppress

import pytest

from a6api_auto.trade_provider_fast_block import (
    ProviderFastBlockError,
    ProviderFastBlockProjection,
    RedisProviderFastBlockProjector,
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


def projection(event_id: int, operation: str) -> ProviderFastBlockProjection:
    return ProviderFastBlockProjection(
        event_id=event_id,
        operation=operation,
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


def test_real_redis_block_unblock_and_stale_replay(redis_client) -> None:
    projector = RedisProviderFastBlockProjector(
        redis_client,
        block_ttl_seconds=600,
        cursor_ttl_seconds=1_800,
    )
    key = projector.key("gpt-5.6-sol", 42, 1, 1008)

    blocked = projector.apply(projection(1, "block"))
    assert blocked.changed is True
    assert json.loads(redis_client.get(key))["state"] == "blocked"
    assert redis_client.ttl(key) > 0

    unblocked = projector.apply(projection(2, "unblock"))
    assert unblocked.changed is True
    assert redis_client.get(key) is None

    stale = projector.apply(projection(1, "block"))
    assert stale.state == "superseded"
    assert redis_client.get(key) is None

    restored = projector.apply(projection(3, "block"))
    assert restored.changed is True
    redis_client.set(key, "{corrupt")
    repaired = projector.apply(projection(3, "block"))
    stored = json.loads(redis_client.get(key))

    assert repaired.changed is True
    assert stored["event_id"] == 3
    assert stored["routing_generation"] == 42

    redis_client.set(f"{key}:cursor", "corrupt")
    with pytest.raises(ProviderFastBlockError, match="redis_result_invalid"):
        projector.apply(projection(4, "block"))


def test_real_redis_rebuild_recovers_flush_and_corrupt_cursor(redis_client) -> None:
    projector = RedisProviderFastBlockProjector(
        redis_client,
        block_ttl_seconds=600,
        cursor_ttl_seconds=1_800,
    )
    key = projector.key("gpt-5.6-sol", 42, 1, 1008)
    latest = projection(7, "block")

    projector.apply(latest)
    redis_client.flushdb()
    rebuilt = projector.rebuild(latest)

    assert rebuilt.changed is True
    assert json.loads(redis_client.get(key))["event_id"] == 7
    assert redis_client.get(f"{key}:cursor") == "7"

    redis_client.set(f"{key}:cursor", "corrupt")
    repaired = projector.rebuild(latest)
    repeated = projector.rebuild(latest)

    assert repaired.state == "projected"
    assert redis_client.get(f"{key}:cursor") == "7"
    assert repeated.changed is False


def test_real_redis_rebuild_never_applies_older_event_over_newer_cursor(redis_client) -> None:
    projector = RedisProviderFastBlockProjector(
        redis_client,
        block_ttl_seconds=600,
        cursor_ttl_seconds=1_800,
    )
    key = projector.key("gpt-5.6-sol", 42, 1, 1008)

    projector.apply(projection(8, "unblock"))
    projector.apply(projection(9, "block"))
    stale = projector.rebuild(projection(8, "unblock"))

    assert stale.state == "superseded"
    assert json.loads(redis_client.get(key))["event_id"] == 9
    assert redis_client.get(f"{key}:cursor") == "9"
