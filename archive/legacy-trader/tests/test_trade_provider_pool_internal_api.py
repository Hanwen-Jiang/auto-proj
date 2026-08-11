from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from typing import Any

from a6api_auto.trade_dashboard import create_dashboard_app
from a6api_auto.trade_db import _migration_files
from a6api_auto.trade_repository import (
    _PROVIDER_POOL_READ_MODEL_QUERY,
    PostgresTradeRepository,
)

MODEL = "gpt-5.6-sol"


def provider_pool_fixture() -> dict[str, Any]:
    return {
        "model": MODEL,
        "generated_at": "2026-07-29T01:02:03Z",
        "routing": {
            "generation": 42,
            "parent_generation": 41,
            "state": "active",
            "source": "observer_snapshot",
            "source_hash": "a" * 64,
            "desired_size": 3,
            "topn_channel_ids": [1008, 1012],
            "standby_channel_ids": [1020],
            "observed_at": "2026-07-29T01:01:30Z",
            "planned_at": "2026-07-29T01:01:31Z",
            "activated_at": "2026-07-29T01:01:40Z",
            "updated_at": "2026-07-29T01:01:40Z",
            "config_version": "a6-provider-topn-failover-v2",
            "api_key": "ROUTING_SECRET_MUST_NOT_LEAK",
        },
        "cost": {
            "generation": 19,
            "parent_generation": 18,
            "state": "active",
            "source": "observer_snapshot",
            "input_hash": "b" * 64,
            "output_hash": "c" * 64,
            "token_mix_version": "mixed-token-v1",
            "exchange_rate_version": "usd-cny-v1",
            "quote_observed_at": "2026-07-29T01:01:30Z",
            "active_slot_nos": [1, 2],
            "route_cost_multiplier": "0.120000000000",
            "retry_reserve_multiplier": "0.030000000000",
            "realized_cost_p95_multiplier": "0.145000000000",
            "pool_cost_multiplier": "0.150000000000",
            "margin_bps": 1000,
            "headroom_bps": 500,
            "billing_floor_multiplier": "0.165000000000",
            "transition_floor_multiplier": "0.170000000000",
            "observed_billing_floor_multiplier": "0.165000000000",
            "planned_at": "2026-07-29T01:01:31Z",
            "confirmed_at": "2026-07-29T01:01:38Z",
            "activated_at": "2026-07-29T01:01:40Z",
            "updated_at": "2026-07-29T01:01:40Z",
            "config_version": "a6-provider-topn-failover-v2",
            "desired_slot_costs": {"secret": "COST_SECRET_MUST_NOT_LEAK"},
        },
        "slots": [
            {
                "slot_no": 2,
                "slot_name": "a6-provider-2",
                "routing_generation": 42,
                "cost_generation": 19,
                "state": "VACANT",
                "channel_id": None,
                "rank": None,
                "priority": None,
                "enodot_binding_id": None,
                "account_id": None,
                "token_id": None,
                "desired_cost_multiplier": None,
                "observed_cost_multiplier": None,
                "liquidity_state": "UNFUNDED",
                "funding_eligible": False,
                "desired_schedulable": False,
                "observed_schedulable": False,
                "confirmed_pin_binding_id": None,
                "pin_confirmed": False,
                "probe_confirmed": False,
                "state_version": 3,
                "valid_from": "2026-07-29T01:01:40Z",
                "updated_at": "2026-07-29T01:01:40Z",
            },
            {
                "slot_no": 1,
                "slot_name": "a6-provider-1",
                "routing_generation": 42,
                "cost_generation": 19,
                "state": "READY",
                "channel_id": 1008,
                "rank": 1,
                "priority": 11,
                "enodot_binding_id": 501,
                "account_id": 601,
                "token_id": 701,
                "desired_cost_multiplier": "0.120000000000",
                "observed_cost_multiplier": "0.120000000000",
                "liquidity_state": "FUNDED",
                "funding_eligible": True,
                "desired_schedulable": True,
                "observed_schedulable": True,
                "confirmed_pin_binding_id": 801,
                "pin_confirmed": True,
                "probe_confirmed": True,
                "state_version": 7,
                "last_success_at": "2026-07-29T01:01:59Z",
                "latency_ewma_ms": "381.250000",
                "valid_from": "2026-07-29T01:01:40Z",
                "updated_at": "2026-07-29T01:01:59Z",
                "session": "SLOT_SECRET_MUST_NOT_LEAK",
            },
        ],
        "publication": {
            "desired_state": "published",
            "observed_state": "published",
            "healthy_slots": 1,
            "min_healthy_slots": 1,
            "redundancy_target": 3,
            "reason_code": "healthy_slot_quorum",
            "desired_routing_generation": 42,
            "desired_cost_generation": 19,
            "scope": "a6-supplier:11117:listing:1397",
            "state_version": 9,
            "desired_at": "2026-07-29T01:01:41Z",
            "observed_at": "2026-07-29T01:01:50Z",
            "confirmed_at": "2026-07-29T01:01:50Z",
            "last_real_probe_at": "2026-07-29T01:01:49Z",
            "last_real_probe_request_id": "probe-001",
            "updated_at": "2026-07-29T01:01:50Z",
            "probe_body": "PUBLICATION_SECRET_MUST_NOT_LEAK",
        },
        "active_quarantines": [
            {
                "scope_type": "channel",
                "scope_id": "1012",
                "region": "us_cloud",
                "reason_code": "fixed_merchant_unavailable",
                "failure_count": 2,
                "starts_at": "2026-07-29T01:00:00Z",
                "next_probe_at": "2026-07-29T01:10:00Z",
                "updated_at": "2026-07-29T01:00:00Z",
                "evidence": "QUARANTINE_SECRET_MUST_NOT_LEAK",
            }
        ],
        "recent_failures": [
            {
                "request_id": "request-001",
                "attempt_id": "attempt-001",
                "attempt_no": 1,
                "routing_generation": 42,
                "cost_generation": 19,
                "slot_no": 1,
                "account_id": 601,
                "token_id": 701,
                "channel_id": 1008,
                "endpoint_host": "a6.a6api.com",
                "egress_id": "direct",
                "failure_owner": "provider",
                "failure_code": "fixed_merchant_unavailable",
                "http_status": 503,
                "transport_status": 503,
                "terminal_event_seen": False,
                "delivery_state": "not_started",
                "actual_cost_micros": 123,
                "occurred_at": "2026-07-29T01:01:58Z",
                "request_body": "REQUEST_SECRET_MUST_NOT_LEAK",
                "headers": {"Authorization": "Bearer SECRET_MUST_NOT_LEAK"},
            }
        ],
    }


def planned_provider_pool_fixture() -> dict[str, Any]:
    fixture = provider_pool_fixture()
    fixture.update(
        {
            "routing": None,
            "cost": None,
            "slots": [],
            # A historical publication row must not be exposed as active state.
            "publication": fixture["publication"],
            "preview": {
                "kind": "planned",
                "routing": {
                    **fixture["routing"],
                    "generation": 43,
                    "parent_generation": None,
                    "state": "planned",
                    "activated_at": None,
                    "updated_at": "2026-07-29T02:01:40Z",
                    "preview_secret": "PREVIEW_ROUTING_SECRET_MUST_NOT_LEAK",
                },
                "cost": {
                    **fixture["cost"],
                    "generation": 20,
                    "parent_generation": None,
                    "state": "planned",
                    "active_slot_nos": [1],
                    "confirmed_at": None,
                    "activated_at": None,
                    "updated_at": "2026-07-29T02:01:40Z",
                    "preview_secret": "PREVIEW_COST_SECRET_MUST_NOT_LEAK",
                },
                "slots": [
                    {
                        "slot_no": 2,
                        "slot_name": "a6-provider-2",
                        "routing_generation": 43,
                        "cost_generation": 20,
                        "state": "VACANT",
                        "channel_id": None,
                        "rank": None,
                        "desired_cost_multiplier": None,
                        "updated_at": "2026-07-29T02:01:40Z",
                    },
                    {
                        "slot_no": 1,
                        "slot_name": "a6-provider-1",
                        "routing_generation": 43,
                        "cost_generation": 20,
                        "state": "READY",
                        "channel_id": 1008,
                        "rank": 1,
                        "desired_cost_multiplier": "0.120000000000",
                        "updated_at": "2026-07-29T02:01:40Z",
                        "preview_secret": "PREVIEW_SLOT_SECRET_MUST_NOT_LEAK",
                    },
                ],
            },
        }
    )
    return fixture


class FakeRepository:
    def __init__(self) -> None:
        self.provider_pool_calls: list[str] = []
        self.provider_pool: Mapping[str, Any] | None = provider_pool_fixture()
        self.provider_pool_error: Exception | None = None

    def get_provider_pool(self, model: str) -> Mapping[str, Any] | None:
        self.provider_pool_calls.append(model)
        if self.provider_pool_error is not None:
            raise self.provider_pool_error
        return self.provider_pool

    def health(self) -> Mapping[str, Any]:
        return {"status": "ok"}

    def get_overview(self, _window: str) -> Mapping[str, Any]:
        return {}

    def get_merchants(self, _window: str) -> Sequence[Mapping[str, Any]]:
        return []

    def get_market_traffic(self, _window: str, _model: str) -> Mapping[str, Any]:
        return {}

    def get_merchant_session_binding(self, _merchant_id: int) -> Mapping[str, Any] | None:
        return None


def request(app: Any, path: str, *, method: str = "GET") -> tuple[str, dict[str, str], bytes]:
    captured: dict[str, Any] = {}

    def start_response(status: str, headers: list[tuple[str, str]]) -> None:
        captured["status"] = status
        captured["headers"] = dict(headers)

    body = b"".join(
        app(
            {
                "REQUEST_METHOD": method,
                "PATH_INFO": path,
                "QUERY_STRING": "",
                "SERVER_NAME": "localhost",
                "SERVER_PORT": "80",
                "wsgi.url_scheme": "http",
                "CONTENT_TYPE": "",
                "CONTENT_LENGTH": "0",
                "wsgi.input": BytesIO(),
            },
            start_response,
        )
    )
    return captured["status"], captured["headers"], body


def test_provider_pool_internal_api_returns_allowlisted_control_snapshot() -> None:
    repository = FakeRepository()
    app = create_dashboard_app(repository)

    status, headers, body = request(app, f"/internal/provider-pools/{MODEL}")

    payload = json.loads(body)
    assert status == "200 OK"
    assert headers["Cache-Control"] == "no-store, private"
    assert headers["Pragma"] == "no-cache"
    assert repository.provider_pool_calls == [MODEL]
    assert payload["routing"]["generation"] == 42
    assert payload["routing"]["topn_channel_ids"] == [1008, 1012]
    assert payload["cost"]["generation"] == 19
    assert payload["cost"]["route_cost_multiplier"] == "0.120000000000"
    assert [slot["slot_no"] for slot in payload["slots"]] == [1, 2]
    assert payload["slots"][0]["state"] == "READY"
    assert payload["slots"][1]["state"] == "VACANT"
    assert payload["publication"]["converged"] is True
    assert payload["publication"]["redundancy_target"] == 3
    assert payload["active_quarantines"][0]["scope_id"] == "1012"
    assert payload["recent_failures"][0] == {
        "request_id": "request-001",
        "attempt_id": "attempt-001",
        "attempt_no": 1,
        "routing_generation": 42,
        "cost_generation": 19,
        "slot_no": 1,
        "account_id": 601,
        "token_id": 701,
        "channel_id": 1008,
        "endpoint_host": "a6.a6api.com",
        "egress_id": "direct",
        "failure_owner": "provider",
        "failure_code": "fixed_merchant_unavailable",
        "http_status": 503,
        "transport_status": 503,
        "terminal_event_seen": False,
        "delivery_state": "not_started",
        "actual_cost_micros": 123,
        "occurred_at": "2026-07-29T01:01:58Z",
    }
    assert payload["summary"] == {
        "topn_count": 2,
        "standby_count": 1,
        "slot_count": 2,
        "ready_slot_count": 1,
        "vacant_slot_count": 1,
        "schedulable_slot_count": 1,
        "active_quarantine_count": 1,
        "recent_failure_count": 1,
    }
    assert b"MUST_NOT_LEAK" not in body
    assert b"Authorization" not in body


def test_publication_projection_rejects_stale_confirmation_and_probe_evidence() -> None:
    repository = FakeRepository()
    stale = provider_pool_fixture()
    stale["publication"] = {
        **stale["publication"],
        "desired_at": "2026-07-29T01:02:00Z",
        "confirmed_at": "2026-07-29T01:01:50Z",
        "last_real_probe_at": "2026-07-29T01:01:49Z",
    }
    repository.provider_pool = stale

    status, _, body = request(
        create_dashboard_app(repository),
        f"/internal/provider-pools/{MODEL}",
    )

    assert status == "200 OK"
    assert json.loads(body)["publication"]["converged"] is False


def test_provider_pool_internal_api_projects_auto_pool_route_evidence() -> None:
    repository = FakeRepository()
    raw = provider_pool_fixture()
    raw["auto_pool"] = {
        "generation_id": 8001,
        "generation": 12,
        "state": "active",
        "selected_channel_ids": [1841, 1548, 1869, 1541, 706],
        "max_ratio_cny": "0.080000000000",
        "max_ratio_usd": "0.011847765000",
        "target_channel_count": 5,
        "ready_account_count": 1,
        "account_count": 1,
        "owned_channel_ban_confirmed": True,
        "observed_at": "2026-08-04T03:00:00Z",
        "activated_at": "2026-08-04T03:00:10Z",
        "updated_at": "2026-08-04T03:00:10Z",
        "config_version": "provider-auto-pool-v1-owned-channel-hard-ban",
        "remote_readback": "REMOTE_SECRET_MUST_NOT_LEAK",
    }
    ready = next(item for item in raw["slots"] if item["state"] == "READY")
    ready.update(
        {
            "channel_id": None,
            "confirmed_pin_binding_id": None,
            "pin_confirmed": False,
            "probe_confirmed": False,
            "route_mode": "auto_pool",
            "auto_pool_generation_id": 8001,
            "auto_pool_generation": 12,
            "auto_pool_subscription_id": 26816,
            "auto_pool_confirmed": True,
            "auto_pool_max_ratio_cny": "0.080000000000",
            "owned_channel_ban_confirmed": True,
            "auto_pool_unpin_confirmed": True,
            "auto_pool_account_state": "ready",
        }
    )
    repository.provider_pool = raw

    status, _, body = request(
        create_dashboard_app(repository),
        f"/internal/provider-pools/{MODEL}",
    )

    payload = json.loads(body)
    assert status == "200 OK"
    assert payload["auto_pool"] == {
        "generation_id": 8001,
        "generation": 12,
        "state": "active",
        "selected_channel_ids": [1841, 1548, 1869, 1541, 706],
        "max_ratio_cny": "0.080000000000",
        "max_ratio_usd": "0.011847765000",
        "target_channel_count": 5,
        "ready_account_count": 1,
        "account_count": 1,
        "owned_channel_ban_confirmed": True,
        "observed_at": "2026-08-04T03:00:00Z",
        "activated_at": "2026-08-04T03:00:10Z",
        "updated_at": "2026-08-04T03:00:10Z",
        "config_version": "provider-auto-pool-v1-owned-channel-hard-ban",
    }
    slot = payload["slots"][0]
    assert slot["route_mode"] == "auto_pool"
    assert slot["channel_id"] is None
    assert slot["auto_pool_subscription_id"] == 26816
    assert slot["owned_channel_ban_confirmed"] is True
    assert slot["observed_schedulable"] is True
    assert b"REMOTE_SECRET_MUST_NOT_LEAK" not in body


def test_provider_pool_internal_api_keeps_planned_preview_separate_from_active_state() -> None:
    repository = FakeRepository()
    repository.provider_pool = planned_provider_pool_fixture()

    status, _, body = request(
        create_dashboard_app(repository),
        f"/internal/provider-pools/{MODEL}",
    )

    payload = json.loads(body)
    assert status == "200 OK"
    assert payload["routing"] is None
    assert payload["cost"] is None
    assert payload["slots"] == []
    assert payload["publication"] is None
    assert payload["summary"]["slot_count"] == 0
    assert payload["preview"]["kind"] == "planned"
    assert payload["preview"]["routing"]["generation"] == 43
    assert payload["preview"]["routing"]["state"] == "planned"
    assert payload["preview"]["cost"]["generation"] == 20
    assert payload["preview"]["cost"]["state"] == "planned"
    assert [slot["state"] for slot in payload["preview"]["slots"]] == ["READY", "VACANT"]
    assert payload["preview"]["summary"] == {
        "topn_count": 2,
        "standby_count": 1,
        "slot_count": 2,
        "ready_slot_count": 1,
        "vacant_slot_count": 1,
    }
    assert b"PREVIEW_" not in body
    assert b"MUST_NOT_LEAK" not in body


def test_provider_pool_internal_api_rejects_noncanonical_model_paths() -> None:
    repository = FakeRepository()
    app = create_dashboard_app(repository)

    for path in (
        "/internal/provider-pools/",
        "/internal/provider-pools/bad model",
        "/internal/provider-pools/../gpt-5.6-sol",
        "/internal/provider-pools/gpt-5.6-sol/extra",
        f"/internal/provider-pools/{'a' * 97}",
    ):
        status, _, body = request(app, path)
        assert status == "404 Not Found"
        assert json.loads(body) == {"error": {"code": "not_found"}}
    assert repository.provider_pool_calls == []


def test_provider_pool_internal_api_has_stable_not_found_and_failure_responses() -> None:
    repository = FakeRepository()
    repository.provider_pool = None
    app = create_dashboard_app(repository)

    status, headers, body = request(app, f"/internal/provider-pools/{MODEL}")

    assert status == "404 Not Found"
    assert headers["Cache-Control"] == "no-store, private"
    assert json.loads(body) == {"error": {"code": "provider_pool_not_found"}}

    repository.provider_pool_error = RuntimeError("DATABASE_SECRET_MUST_NOT_LEAK")
    status, _, body = request(app, f"/internal/provider-pools/{MODEL}")

    assert status == "503 Service Unavailable"
    assert json.loads(body) == {"error": {"code": "provider_pool_unavailable"}}
    assert b"MUST_NOT_LEAK" not in body


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


def test_repository_reads_provider_pool_as_one_bounded_database_snapshot() -> None:
    row = {
        "model": MODEL,
        "known": True,
        "generated_at": datetime(2026, 7, 29, 1, 2, 3, tzinfo=UTC),
        "routing": {"generation": 42},
        "cost": {"generation": 19},
        "slots": [{"slot_no": 1}],
        "publication": {"desired_state": "published"},
        "planned_routing": {"generation": 43, "state": "planned"},
        "planned_cost": {"generation": 20, "state": "planned"},
        "planned_slots": [{"slot_no": 1, "state": "READY"}],
        "active_quarantines": [],
        "recent_failures": [{"attempt_id": "attempt-001"}],
    }
    connection = ReadModelConnection(row)
    repository = PostgresTradeRepository(lambda: connection)

    result = repository.get_provider_pool(MODEL)

    assert result == {
        "model": MODEL,
        "generated_at": row["generated_at"],
        "routing": row["routing"],
        "cost": row["cost"],
        "auto_pool": None,
        "slots": row["slots"],
        "publication": row["publication"],
        "preview": {
            "kind": "planned",
            "routing": row["planned_routing"],
            "cost": row["planned_cost"],
            "slots": row["planned_slots"],
        },
        "active_quarantines": [],
        "recent_failures": row["recent_failures"],
        "recent_generation_failure_count": 0,
    }
    assert connection.closed is True
    assert len(connection.cursor_instance.calls) == 1
    query, params = connection.cursor_instance.calls[0]
    assert query == _PROVIDER_POOL_READ_MODEL_QUERY
    assert params == (MODEL,)
    assert query.count("%s") == 1
    normalized = " ".join(query.split())
    assert "generation.generation_state = 'active'" in normalized
    assert "cost.pricing_state = 'active'" in normalized
    assert "generation.generation_state = 'planned'" in normalized
    assert "cost.pricing_state = 'planned'" in normalized
    assert "AS planned_routing" in normalized
    assert "AS planned_cost" in normalized
    assert "AS planned_slots" in normalized
    assert "active_auto_pool AS" in normalized
    assert "AS auto_pool" in normalized
    assert "slot.route_mode" in normalized
    assert "slot.auto_pool_subscription_id" in normalized
    assert "generate_series( 1, least(planned.desired_size, 100) )" in normalized
    assert "array_position(planned.active_slot_nos, series.slot_no)" in normalized
    assert "slot.valid_to IS NULL" in normalized
    assert "WHERE EXISTS (SELECT 1 FROM active_bundle)" in normalized
    assert "publication.desired_cost_generation" in normalized
    assert "publication.redundancy_target" in normalized
    assert "attempt.state = 'failed'" in normalized
    assert "LIMIT 20" in normalized
    assert "request_body" not in normalized
    assert "headers" not in normalized


def test_repository_returns_none_for_unknown_provider_pool() -> None:
    connection = ReadModelConnection(
        {
            "model": MODEL,
            "known": False,
            "generated_at": datetime(2026, 7, 29, 1, 2, 3, tzinfo=UTC),
        }
    )

    assert PostgresTradeRepository(lambda: connection).get_provider_pool(MODEL) is None
    assert connection.closed is True


def test_provider_pool_read_model_index_is_packaged_and_discovered() -> None:
    source = Path("migrations/0034_provider_pool_read_model_index.sql")
    packaged = Path(
        "src/a6api_auto/data/migrations/0034_provider_pool_read_model_index.sql"
    )
    migration = source.read_text(encoding="utf-8")

    assert packaged.read_text(encoding="utf-8") == migration
    assert "provider_attempt_failed_model_time_idx" in migration
    assert "(coalesce(finished_at, started_at)) DESC" in migration
    assert "WHERE state = 'failed'" in migration
    discovered = dict(_migration_files(Path("migrations")))
    assert discovered["0034"] == source
