from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from a6api_auto.trade_provider_runtime_failure import (
    ProviderRuntimeFailureClaim,
    ProviderRuntimeFailureConsumer,
)
from a6api_auto.trade_publication_control import PublicationControlError

UTC = timezone.utc


def claim(**overrides: Any) -> ProviderRuntimeFailureClaim:
    values = {
        "event_id": 17,
        "claim_token": "claim-17",
        "source_error_log_id": 39163,
        "upstream_ordinal": 2,
        "source_request_id": "source-request",
        "model": "gpt-5.6-sol",
        "failure_code": "fixed_merchant_unavailable",
        "http_status": 503,
        "occurred_at": datetime(2026, 8, 4, 3, 2, 29, tzinfo=UTC),
        "routing_generation": 7389,
        "cost_generation": 7389,
        "slot_no": 2,
        "enodot_account_id": 45916,
        "a6_token_id": 22746,
        "channel_id": 1320,
        "pin_assignment_epoch_id": 261,
        "route_mode": "fixed_pin",
        "auto_pool_generation_id": None,
        "auto_pool_subscription_id": None,
        "consecutive_failure_count": 3,
        "block_action": "block_channel",
        "attempt_count": 1,
    }
    values.update(overrides)
    return ProviderRuntimeFailureClaim(**values)


class Store:
    def __init__(self, item: ProviderRuntimeFailureClaim | None) -> None:
        self.item = item
        self.envelopes: list[int] = []
        self.completed: list[dict[str, Any]] = []
        self.superseded: list[str] = []
        self.retries: list[tuple[str, int]] = []

    def claim(self, *, worker_id: str) -> ProviderRuntimeFailureClaim | None:
        assert worker_id == "worker-1"
        item, self.item = self.item, None
        return item

    def ensure_envelope(self, item: ProviderRuntimeFailureClaim) -> None:
        self.envelopes.append(item.event_id)

    def complete(
        self,
        _item: ProviderRuntimeFailureClaim,
        *,
        result: dict[str, Any],
    ) -> None:
        self.completed.append(result)

    def supersede(self, _item: ProviderRuntimeFailureClaim, *, error_class: str) -> None:
        self.superseded.append(error_class)

    def retry(
        self,
        _item: ProviderRuntimeFailureClaim,
        *,
        error_class: str,
        retry_after_seconds: int,
    ) -> None:
        self.retries.append((error_class, retry_after_seconds))


class Control:
    def __init__(self, result: dict[str, Any] | Exception) -> None:
        self.result = result
        self.documents: list[dict[str, Any]] = []

    def report_attempt_failure(self, document: dict[str, Any]) -> dict[str, Any]:
        self.documents.append(document)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class Trigger:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    def wake(self, *, model: str, request_id: int) -> None:
        self.calls.append((model, request_id))


def test_runtime_failure_consumer_reports_canonical_fixed_merchant_failure() -> None:
    store = Store(claim())
    control = Control(
        {
            "attempt_id": "a6-runtime-attempt:39163:2",
            "slot_drained": True,
            "control_action": "repin_immediately",
            "channel_failure_streak_count": 3,
        }
    )
    consumer = ProviderRuntimeFailureConsumer(  # type: ignore[arg-type]
        store,
        control,
        worker_id="worker-1",
    )

    result = consumer.run_once()

    assert result["status"] == "completed"
    assert store.envelopes == [17]
    assert store.completed[0]["block_action"] == "block_channel"
    document = control.documents[0]
    assert document["request_id"] == "a6-runtime:39163:2"
    assert document["failure_owner"] == "provider"
    assert document["failure_code"] == "fixed_merchant_unavailable"
    assert document["terminal_event_seen"] is True


def test_runtime_failure_consumer_supersedes_stale_generation() -> None:
    store = Store(claim())
    control = Control(PublicationControlError("publication_expected_generation_conflict"))
    consumer = ProviderRuntimeFailureConsumer(  # type: ignore[arg-type]
        store,
        control,
        worker_id="worker-1",
    )

    result = consumer.run_once()

    assert result["status"] == "superseded"
    assert store.superseded == ["publication_expected_generation_conflict"]
    assert store.retries == []


def test_runtime_failure_consumer_retries_transient_control_error() -> None:
    store = Store(claim(failure_code="insufficient_quota", block_action="block_liquidity"))
    control = Control(PublicationControlError("publication_trigger_write_failed"))
    consumer = ProviderRuntimeFailureConsumer(  # type: ignore[arg-type]
        store,
        control,
        worker_id="worker-1",
    )

    result = consumer.run_once()

    assert result["status"] == "retry"
    assert store.retries == [("publication_trigger_write_failed", 5)]


def test_auto_pool_failure_wakes_observer_refresh_without_old_pin_control() -> None:
    store = Store(
        claim(
            channel_id=None,
            pin_assignment_epoch_id=None,
            route_mode="auto_pool",
            auto_pool_generation_id=8012,
            auto_pool_subscription_id=26816,
            consecutive_failure_count=1,
            block_action="refresh_auto_pool",
        )
    )
    control = Control(AssertionError("fixed Pin control must not run"))
    refresh = Trigger()
    consumer = ProviderRuntimeFailureConsumer(  # type: ignore[arg-type]
        store,
        control,
        worker_id="worker-1",
        auto_pool_trigger=refresh,
    )

    result = consumer.run_once()

    assert result["status"] == "completed"
    assert result["control_action"] == "observe_then_refresh_auto_pool"
    assert refresh.calls == [("gpt-5.6-sol", 17)]
    assert control.documents == []
    assert store.envelopes == []


def test_auto_pool_quota_failure_wakes_existing_liquidity_chain() -> None:
    store = Store(
        claim(
            failure_code="insufficient_quota",
            channel_id=None,
            pin_assignment_epoch_id=None,
            route_mode="auto_pool",
            auto_pool_generation_id=8012,
            auto_pool_subscription_id=26816,
            consecutive_failure_count=1,
            block_action="block_liquidity",
        )
    )
    liquidity = Trigger()
    consumer = ProviderRuntimeFailureConsumer(  # type: ignore[arg-type]
        store,
        Control(AssertionError("fixed Pin control must not run")),
        worker_id="worker-1",
        liquidity_trigger=liquidity,
    )

    result = consumer.run_once()

    assert result["status"] == "completed"
    assert result["control_action"] == "repair_liquidity"
    assert liquidity.calls == [("gpt-5.6-sol", 17)]
