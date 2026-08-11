from __future__ import annotations

from typing import Any

import pytest

from a6api_auto.trade_provider_fast_block import (
    ProviderFastBlockError,
    ProviderFastBlockProjection,
    RedisProviderFastBlockProjector,
)


class RedisClient:
    def __init__(self, result: int = 1) -> None:
        self.result = result
        self.calls: list[tuple[Any, ...]] = []

    def eval(self, *args: Any) -> int:
        self.calls.append(args)
        return self.result


def projection(*, event_id: int = 7, operation: str = "block") -> ProviderFastBlockProjection:
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


def test_projector_uses_full_slot_identity_and_monotonic_event_id() -> None:
    client = RedisClient()
    projector = RedisProviderFastBlockProjector(client)

    result = projector.apply(projection())

    assert result.state == "projected"
    assert result.changed is True
    call = client.calls[0]
    assert call[1] == 2
    assert call[2] == "a6:provider:fast-block:v1:gpt-5.6-sol:42:1:1008"
    assert call[3].endswith(":cursor")
    assert call[4] == 7
    assert call[5] == "block"


def test_projector_reports_superseded_without_treating_it_as_failure() -> None:
    client = RedisClient(-1)
    result = RedisProviderFastBlockProjector(client).apply(projection())

    assert result.state == "superseded"
    assert result.changed is False


def test_rebuild_uses_recovery_script_and_preserves_projection_identity() -> None:
    client = RedisClient()
    projector = RedisProviderFastBlockProjector(client)

    result = projector.rebuild(projection())

    assert result.state == "projected"
    assert result.changed is True
    assert client.calls[0][4:6] == (7, "block")
    assert "current_id ~= nil and current_id > event_id" in client.calls[0][0]


def test_projector_rejects_projection_key_drift() -> None:
    client = RedisClient()
    projector = RedisProviderFastBlockProjector(client)
    invalid = ProviderFastBlockProjection(
        **{
            **projection().__dict__,
            "projection_key": "a6:provider:fast-block:v1:gpt-5.6-sol:41:1:1008",
        }
    )

    with pytest.raises(ProviderFastBlockError, match="projection_key_mismatch"):
        projector.apply(invalid)
