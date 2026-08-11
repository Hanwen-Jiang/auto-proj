from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime, timezone
from io import BytesIO
from typing import Any

import pytest

from a6api_auto.trade_dashboard import create_dashboard_app
from a6api_auto.trade_repository import PostgresTradeRepository

UTC = timezone.utc
MODEL = "gpt-5.6-sol"
HASH = "a" * 64


class FakeRepository:
    def __init__(self) -> None:
        self.inventory: Mapping[str, Any] | None = provider_inventory_fixture()
        self.error: Exception | None = None
        self.calls: list[str] = []

    def get_provider_inventory(self, model: str) -> Mapping[str, Any] | None:
        self.calls.append(model)
        if self.error is not None:
            raise self.error
        return self.inventory


def provider_inventory_fixture() -> dict[str, Any]:
    return {
        "model": MODEL,
        "generated_at": "2026-07-29T05:00:00Z",
        "session": "SESSION_MUST_NOT_LEAK",
        "items": [
            {
                "merchant_id": 11,
                "enodot_binding_id": 111,
                "account_id": 211,
                "token_id": 311,
                "inventory_state": "READY",
                "liquidity_state": "FUNDED",
                "funding_eligible": True,
                "schedulable": True,
                "binding_ready": True,
                "balance_units": 3,
                "financial_source_hash": HASH,
                "assigned_slot_no": 1,
                "assignment": {
                    "slot_no": 1,
                    "found": True,
                    "identity_matches": True,
                    "state": "READY",
                    "routing_generation": 42,
                    "cost_generation": 19,
                    "channel_id": 1008,
                    "priority": 2,
                    "observed_schedulable": True,
                    "api_key": "KEY_MUST_NOT_LEAK",
                },
                "state_version": 7,
                "last_financial_observed_at": "2026-07-29T04:59:00Z",
                "updated_at": "2026-07-29T04:59:30Z",
                "password": "PASSWORD_MUST_NOT_LEAK",
            },
            {
                "merchant_id": 12,
                "enodot_binding_id": 112,
                "account_id": 212,
                "token_id": 312,
                "inventory_state": "WARM_STANDBY",
                "liquidity_state": "FUNDED",
                "funding_eligible": True,
                "schedulable": False,
                "binding_ready": True,
                "balance_units": 2,
                "financial_source_hash": "b" * 64,
                "assigned_slot_no": None,
                "assignment": None,
                "state_version": 3,
            },
            {
                "merchant_id": 13,
                "inventory_state": "REPAIRING",
                "liquidity_state": "UNFUNDED",
                "funding_eligible": False,
                "schedulable": False,
                "binding_ready": False,
                "assigned_slot_no": 3,
                "assignment": {
                    "slot_no": 3,
                    "found": False,
                    "identity_matches": False,
                    "state": None,
                    "observed_schedulable": False,
                },
                "state_version": 2,
            },
        ],
    }


def request(app: Any, path: str) -> tuple[str, dict[str, str], bytes]:
    captured: dict[str, Any] = {}

    def start_response(status: str, headers: list[tuple[str, str]]) -> None:
        captured["status"] = status
        captured["headers"] = dict(headers)

    body = b"".join(
        app(
            {
                "REQUEST_METHOD": "GET",
                "PATH_INFO": path,
                "QUERY_STRING": "",
                "wsgi.input": BytesIO(b""),
            },
            start_response,
        )
    )
    return captured["status"], captured["headers"], body


def test_provider_inventory_api_returns_sanitized_readiness_and_assignments() -> None:
    repository = FakeRepository()

    status, headers, body = request(
        create_dashboard_app(repository), f"/internal/provider-inventory/{MODEL}"
    )

    assert status == "200 OK"
    assert headers["Cache-Control"] == "no-store, private"
    assert headers["Pragma"] == "no-cache"
    payload = json.loads(body)
    assert payload["model"] == MODEL
    assert payload["items"][0] == {
        "merchant_id": 11,
        "enodot_binding_id": 111,
        "account_id": 211,
        "token_id": 311,
        "inventory_state": "READY",
        "liquidity_state": "FUNDED",
        "funding_eligible": True,
        "schedulable": True,
        "binding_ready": True,
        "balance_units": 3,
        "financial_source_hash": HASH,
        "assigned_slot_no": 1,
        "assignment": {
            "slot_no": 1,
            "found": True,
            "identity_matches": True,
            "state": "READY",
            "routing_generation": 42,
            "cost_generation": 19,
            "channel_id": 1008,
            "priority": 2,
            "observed_schedulable": True,
        },
        "assignment_consistent": True,
        "state_version": 7,
        "last_financial_observed_at": "2026-07-29T04:59:00Z",
        "updated_at": "2026-07-29T04:59:30Z",
    }
    assert payload["items"][2]["assignment_consistent"] is False
    assert payload["summary"] == {
        "inventory_count": 3,
        "ready_count": 1,
        "warm_standby_count": 1,
        "repairing_count": 1,
        "retired_count": 0,
        "funded_count": 2,
        "schedulable_count": 1,
        "assigned_count": 2,
        "inconsistent_assignment_count": 1,
    }
    assert repository.calls == [MODEL]
    for secret in (
        b"SESSION_MUST_NOT_LEAK",
        b"KEY_MUST_NOT_LEAK",
        b"PASSWORD_MUST_NOT_LEAK",
    ):
        assert secret not in body


def test_provider_inventory_api_rejects_noncanonical_paths_without_reading() -> None:
    repository = FakeRepository()
    app = create_dashboard_app(repository)

    for path in (
        "/internal/provider-inventory/",
        "/internal/provider-inventory/bad model",
        "/internal/provider-inventory/../gpt-5.6-sol",
        "/internal/provider-inventory/gpt-5.6-sol/extra",
        f"/internal/provider-inventory/{'a' * 97}",
    ):
        status, _, body = request(app, path)
        assert status == "404 Not Found"
        assert json.loads(body) == {"error": {"code": "not_found"}}
    assert repository.calls == []


def test_provider_inventory_api_has_stable_not_found_and_failure_responses() -> None:
    repository = FakeRepository()
    repository.inventory = None
    app = create_dashboard_app(repository)

    status, headers, body = request(app, f"/internal/provider-inventory/{MODEL}")

    assert status == "404 Not Found"
    assert headers["Cache-Control"] == "no-store, private"
    assert json.loads(body) == {"error": {"code": "provider_inventory_not_found"}}

    repository.error = RuntimeError("DATABASE_SECRET_MUST_NOT_LEAK")
    status, _, body = request(app, f"/internal/provider-inventory/{MODEL}")

    assert status == "503 Service Unavailable"
    assert json.loads(body) == {"error": {"code": "provider_inventory_unavailable"}}
    assert b"MUST_NOT_LEAK" not in body


def test_provider_inventory_api_fails_closed_on_assignment_slot_mismatch() -> None:
    repository = FakeRepository()
    inventory = provider_inventory_fixture()
    inventory["items"][0]["assignment"]["slot_no"] = 2
    repository.inventory = inventory

    status, _, body = request(
        create_dashboard_app(repository), f"/internal/provider-inventory/{MODEL}"
    )

    assert status == "503 Service Unavailable"
    assert json.loads(body) == {"error": {"code": "provider_inventory_unavailable"}}


class ReadModelCursor:
    def __init__(self, row: Mapping[str, Any]) -> None:
        self.row = row
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    def __enter__(self) -> ReadModelCursor:
        return self

    def __exit__(self, *_args: Any) -> bool:
        return False

    def execute(self, query: str, params: Any = None) -> None:
        self.calls.append((query, tuple(params or ())))

    def fetchone(self) -> Mapping[str, Any]:
        return self.row


class ReadModelConnection:
    def __init__(self, row: Mapping[str, Any]) -> None:
        self.cursor_instance = ReadModelCursor(row)
        self.closed = False

    def cursor(self) -> ReadModelCursor:
        return self.cursor_instance

    def close(self) -> None:
        self.closed = True


def test_repository_reads_provider_inventory_as_one_bounded_snapshot() -> None:
    row = {
        "model": MODEL,
        "known": True,
        "generated_at": datetime(2026, 7, 29, 5, 0, tzinfo=UTC),
        "items": [{"merchant_id": 11, "inventory_state": "READY"}],
    }
    connection = ReadModelConnection(row)
    repository = PostgresTradeRepository(lambda: connection)

    result = repository.get_provider_inventory(MODEL)

    assert result == {
        "model": MODEL,
        "generated_at": row["generated_at"],
        "items": row["items"],
    }
    assert connection.closed is True
    assert len(connection.cursor_instance.calls) == 1
    query, params = connection.cursor_instance.calls[0]
    assert params == (MODEL,)
    assert "clock_timestamp() AS generated_at" in query
    assert "LIMIT 500" in query
    assert "slot.valid_to IS NULL" in query
    assert "slot.enodot_binding_id = inventory.enodot_binding_id" in query
    assert "slot.enodot_account_id = inventory.enodot_account_id" in query
    assert "slot.a6_token_id = inventory.a6_token_id" in query
    assert "WHEN slot.slot_state = 'READY' THEN 'READY'" in query
    assert "THEN slot.observed_schedulable" in query
    assert "coalesce(slot.slot_no, inventory.assigned_slot_no)" in query
    assert "IS NOT DISTINCT FROM" in query
    assert "session" not in query.casefold()
    assert "api_key" not in query.casefold()


def test_repository_returns_none_for_unknown_provider_inventory_model() -> None:
    connection = ReadModelConnection(
        {
            "model": MODEL,
            "known": False,
            "generated_at": datetime(2026, 7, 29, 5, 0, tzinfo=UTC),
            "items": [],
        }
    )

    assert PostgresTradeRepository(lambda: connection).get_provider_inventory(MODEL) is None
    assert connection.closed is True


def test_repository_rejects_invalid_provider_inventory_model_before_connecting() -> None:
    repository = PostgresTradeRepository(
        lambda: pytest.fail("connection must stay cold for invalid model")
    )

    with pytest.raises(ValueError, match="provider_inventory_model_invalid"):
        repository.get_provider_inventory(" ")
