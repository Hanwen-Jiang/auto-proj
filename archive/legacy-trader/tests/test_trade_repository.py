from __future__ import annotations

from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from threading import Event, Lock
from typing import Any

import pytest

from a6api_auto.channel_policy import (
    FORBIDDEN_CHANNEL_NAME,
    GENERATED_CHANNEL_NAME_PATTERN,
    GENERATED_CHANNEL_NAME_SIGNAL_PATTERN,
)
from a6api_auto.trade_collector import (
    CollectionBatch,
    CollectorWatermarks,
    EarningEvent,
    FinancialSnapshot,
    PinSnapshot,
    ProviderUsageEvent,
    SupplierChannelSnapshot,
    SupplierListingSnapshot,
    SupplierProfileSnapshot,
    TokenSnapshot,
    provider_usage_identity,
    provider_usage_storage_key,
)
from a6api_auto.trade_repository import (
    _ENODOT_SALES_QUERY,
    _MANAGED_POSITION_PERFORMANCE_QUERY,
    _MERCHANT_QUERY,
    EnodotProviderCostLedger,
    PostgresTradeRepository,
    _coverage,
    _managed_position_document,
    _row,
    _rows,
    _since,
)

OBSERVED_AT = "2026-07-24T04:00:00Z"


def test_dashboard_cache_rejects_invalid_ttl() -> None:
    with pytest.raises(ValueError, match="dashboard_cache_ttl_invalid"):
        PostgresTradeRepository(lambda: object(), dashboard_cache_ttl_seconds=-1)


def test_economic_read_model_returns_runtime_profit_setting() -> None:
    row = {
        "model": "gpt-5.6-sol",
        "known": True,
        "generated_at": OBSERVED_AT,
        "profit_setting": {
            "minimum_profit_on_cost_bps": 6000,
            "updated_at": OBSERVED_AT,
            "updated_by": "dashboard",
        },
        "generation": None,
        "preview": None,
        "projection": None,
        "eligibility_states": {},
        "sources": [],
        "channels": [],
        "summary": {},
    }

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def execute(self, _query: str, params: tuple[object, ...]) -> None:
            assert params == ("gpt-5.6-sol",)

        def fetchone(self):
            return row

    class Connection:
        closed = False

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def cursor(self):
            return Cursor()

        def close(self) -> None:
            self.closed = True

    connection = Connection()
    result = PostgresTradeRepository(lambda: connection).get_economic_priority1(
        "gpt-5.6-sol"
    )

    assert result is not None
    assert result["profit_setting"]["minimum_profit_on_cost_bps"] == 6000
    assert connection.closed is True


def test_dashboard_merchant_cache_singleflights_parallel_readers(monkeypatch: Any) -> None:
    ticks = [100.0]
    repository = PostgresTradeRepository(
        lambda: object(),
        dashboard_cache_ttl_seconds=10,
        monotonic=lambda: ticks[0],
    )
    started = Event()
    release = Event()
    guard = Lock()
    calls = 0
    snapshot = (
        [{"merchant_id": 1}],
        EnodotProviderCostLedger(readable=True, by_merchant={}),
    )

    def load(window: str) -> tuple[list[dict[str, Any]], EnodotProviderCostLedger]:
        nonlocal calls
        assert window == "24h"
        with guard:
            calls += 1
        started.set()
        assert release.wait(timeout=2)
        return snapshot

    monkeypatch.setattr(repository, "_load_merchant_ledgers", load)
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(repository._get_merchant_ledgers, "24h")
        assert started.wait(timeout=2)
        second = executor.submit(repository._get_merchant_ledgers, "24h")
        release.set()
        assert first.result(timeout=2) is snapshot
        assert second.result(timeout=2) is snapshot

    assert calls == 1
    ticks[0] = 111.0
    assert repository._get_merchant_ledgers("24h") is snapshot
    assert calls == 2


def test_dashboard_market_cache_reuses_snapshot_until_ttl(monkeypatch: Any) -> None:
    ticks = [10.0]
    repository = PostgresTradeRepository(
        lambda: object(),
        dashboard_cache_ttl_seconds=10,
        monotonic=lambda: ticks[0],
    )
    calls: list[tuple[str, str]] = []
    snapshots = [{"generation": 1}, {"generation": 2}]

    def load(window: str, model: str) -> Mapping[str, Any]:
        calls.append((window, model))
        return snapshots[len(calls) - 1]

    monkeypatch.setattr(repository, "_load_market_traffic", load)
    assert repository.get_market_traffic("5m", "gpt-5.6-sol") is snapshots[0]
    ticks[0] = 19.9
    assert repository.get_market_traffic("5m", "gpt-5.6-sol") is snapshots[0]
    ticks[0] = 20.0
    assert repository.get_market_traffic("5m", "gpt-5.6-sol") is snapshots[1]
    assert calls == [("5m", "gpt-5.6-sol"), ("5m", "gpt-5.6-sol")]


def test_managed_positions_scope_current_nonretired_seller_bindings() -> None:
    query = " ".join(_MANAGED_POSITION_PERFORMANCE_QUERY.split())

    assert "FROM a6_trade.a6_managed_listing_bindings listing" in query
    assert "JOIN a6_trade.a6_managed_channel_bindings channel_binding" in query
    assert "channel_binding.id = listing.channel_binding_id" in query
    assert "listing.control_state <> 'retired'" in query
    assert "channel_binding.control_state <> 'retired'" in query
    assert "JOIN a6_trade.a6_merchants position_merchant" in query
    assert "position_merchant.status AS merchant_status" in query
    assert "position_merchant.roles AS merchant_roles" in query
    assert "position_merchant.status <> 'abandoned'" in query
    assert "position_merchant.roles @> ARRAY['seller']::text[]" in query
    assert "FROM a6_trade.a6_seller_retirement_plans retirement" in query
    assert "retirement.state = 'completed'" in query
    assert "LEFT JOIN LATERAL" in query
    assert "action.entity_type = 'a6_managed_listing'" in query
    assert "action.entity_id = listing.managed_uid" in query
    assert "ORDER BY action.completed_at DESC, action.id DESC LIMIT 1" in query
    assert "listing.a6_channel_id AS channel_id" in query
    assert "listing.control_state AS binding_control_state" in query
    assert "channel.a6_channel_id = listing.a6_channel_id" in query
    assert "LEFT JOIN a6_trade.a6_supplier_listings_current current_listing" in query
    assert "current_listing.platform_model AS listing_platform_model" in query
    assert "current_listing.status AS listing_status" in query
    assert "current_listing.availability AS listing_availability" in query
    assert "current_listing.visibility AS listing_visibility" in query
    assert "current_listing.observed_at >= position_merchant.last_poll_at" in query
    assert "current_listing.status = 2" not in query
    assert "current_listing.availability = 1" not in query
    assert "current_listing.visibility = 1" not in query
    assert "signal.model = %s" in query
    assert "signal.channel_id = listing.a6_channel_id" in query
    assert "channel.name IS NULL" in query
    assert "btrim(channel.name) <> %s" in query
    assert "btrim(channel.name) ~ %s" in query


@pytest.mark.parametrize(
    ("override", "blocked_reason"),
    (
        ({"merchant_status": "authenticated"}, "merchant_status_not_supplier_submitted"),
        ({"binding_control_state": "verifying"}, "binding_not_verified"),
        ({"listing_present": False}, "listing_missing"),
        ({"listing_channel_id": 701}, "listing_channel_mismatch"),
        ({"listing_platform_model": "gpt-5.6-terra"}, "listing_model_mismatch"),
        ({"listing_status": 0}, "listing_status_inactive"),
        ({"listing_availability": 0}, "listing_unavailable"),
        ({"listing_visibility": 0}, "listing_hidden"),
    ),
)
def test_managed_position_document_keeps_blocked_position_with_first_reason(
    override: Mapping[str, Any], blocked_reason: str
) -> None:
    row: dict[str, Any] = {
        "rank": 3,
        "merchant_id": 42,
        "planned_model": "gpt-5.6-sol",
        "merchant_status": "supplier_submitted",
        "merchant_roles": ["seller", "observer"],
        "binding_present": True,
        "binding_control_state": "verified",
        "channel_id": 700,
        "listing_id": 1700,
        "listing_present": True,
        "listing_channel_id": 700,
        "listing_platform_model": "gpt-5.6-sol",
        "listing_status": 2,
        "listing_availability": 1,
        "listing_visibility": 1,
        "seller_call_count": 4,
        "seller_income_micros": 120,
        "managed_key_call_count": 4,
        "managed_key_debit_micros": 80,
    }
    row.update(override)

    position = _managed_position_document(row, 300)

    assert position["operational_state"] == "blocked"
    assert position["blocked_reason"] == blocked_reason
    assert position["merchant_status"] == row["merchant_status"]
    assert position["binding_control_state"] == row["binding_control_state"]
    assert position["listing_status"] == row["listing_status"]


def test_managed_position_document_preserves_active_position_metrics() -> None:
    position = _managed_position_document(
        {
            "rank": 1,
            "merchant_id": 7,
            "planned_model": "gpt-5.6-sol",
            "merchant_status": "supplier_submitted",
            "merchant_roles": ["seller"],
            "binding_present": True,
            "binding_control_state": "verified",
            "channel_id": 702,
            "listing_id": 1702,
            "listing_present": True,
            "listing_channel_id": 702,
            "listing_platform_model": "gpt-5.6-sol",
            "listing_status": 2,
            "listing_availability": 1,
            "listing_visibility": 1,
            "seller_call_count": 4,
            "seller_income_micros": 120,
            "managed_key_call_count": 4,
            "managed_key_debit_micros": 80,
        },
        300,
    )

    assert position["operational_state"] == "active"
    assert position["blocked_reason"] == ""
    assert position["delta_sample_status"] == "matched_count"
    assert position["seller_income_per_call_micros"] == 30
    assert position["estimated_contribution_micros"] == 40


def test_managed_position_document_uses_one_effective_failure_responsibility() -> None:
    position = _managed_position_document(
        {
            "merchant_status": "supplier_submitted",
            "merchant_roles": ["seller"],
            "binding_present": True,
            "binding_control_state": "verified",
            "channel_id": 1637,
            "listing_id": 5714,
            "listing_present": True,
            "listing_channel_id": 1637,
            "planned_model": "gpt-5.6-sol",
            "listing_platform_model": "gpt-5.6-sol",
            "listing_status": 2,
            "listing_availability": 1,
            "listing_visibility": 1,
            "market_available": True,
            "health_state": "degraded",
            "smart_routing_status": "labeled",
            "smart_routing_labels": ["cheap"],
            "sample_count": 11,
            "recent_success_rate": 7272,
            "last_success_at": datetime(2026, 7, 30, 19, 19, 11, tzinfo=UTC),
            "last_failure_at": datetime(2026, 7, 30, 19, 19, 49, tzinfo=UTC),
            "last_failure_code": "do_request_failed",
            "last_failure_responsibility": "unknown",
        },
        300,
        now=datetime(2026, 7, 30, 19, 20, tzinfo=UTC),
    )

    assert position["health_state"] == "degraded"
    assert position["health_reason"] == "current_provider_failure"
    assert position["last_failure_responsibility"] == "provider"


def test_enodot_sales_matches_only_normalized_models_in_adjacent_time_buckets() -> None:
    query = " ".join(_ENODOT_SALES_QUERY.split())

    assert "usage_match_keys AS MATERIALIZED" in query
    assert "SELECT DISTINCT value::text AS model" in query
    assert "VALUES (usage.model), (usage.requested_model), (usage.upstream_model)" in query
    assert "VALUES (-1::bigint), (0::bigint), (1::bigint)" in query
    assert "event.model = usage.candidate_model" in query
    assert "event.match_bucket = usage.match_bucket" in query
    assert "event.occurred_at BETWEEN usage.created_at - params.match_tolerance" in query


def test_tuple_rows_support_psycopg_column_metadata() -> None:
    class Column:
        def __init__(self, name: str) -> None:
            self.name = name

    class Cursor:
        description = (Column("merchant_id"), Column("amount"))

        @staticmethod
        def fetchall() -> list[tuple[int, int]]:
            return [(1, 20), (2, 30)]

        @staticmethod
        def fetchone() -> tuple[int, int]:
            return (1, 20)

    assert _rows(Cursor()) == [
        {"merchant_id": 1, "amount": 20},
        {"merchant_id": 2, "amount": 30},
    ]
    assert _row(Cursor()) == {"merchant_id": 1, "amount": 20}


def test_dashboard_reads_only_the_requested_merchant_session_binding() -> None:
    expected = {
        "merchant_id": 7,
        "a6_user_id": 1007,
        "supplier_id": 2007,
        "session_ref": "/private/session-7.json",
        "session_fingerprint": "a" * 64,
    }

    class Cursor:
        def __init__(self) -> None:
            self.query = ""
            self.params: tuple[Any, ...] = ()

        def __enter__(self) -> Cursor:
            return self

        def __exit__(self, *_args: Any) -> bool:
            return False

        def execute(self, query: str, params: Any = None) -> None:
            self.query = query
            self.params = tuple(params or ())

        def fetchone(self) -> Mapping[str, Any]:
            return expected

    class Connection:
        def __init__(self) -> None:
            self.cursor_instance = Cursor()
            self.closed = False

        def cursor(self) -> Cursor:
            return self.cursor_instance

        def close(self) -> None:
            self.closed = True

    connection = Connection()
    repository = PostgresTradeRepository(lambda: connection)

    result = repository.get_merchant_session_binding(7)

    assert result == expected
    assert connection.cursor_instance.params == (7,)
    normalized = " ".join(connection.cursor_instance.query.split())
    assert "WHERE id = %s AND status <> 'abandoned'" in normalized
    assert "password" not in normalized
    assert "cookies" not in normalized
    assert connection.closed is True


def test_dashboard_merchant_query_excludes_abandoned_session_tombstones() -> None:
    normalized = " ".join(_MERCHANT_QUERY.split())

    assert "WHERE m.status <> 'abandoned' ORDER BY m.id" in normalized


def test_dashboard_merchant_channel_counts_use_current_active_seller_scope() -> None:
    normalized = " ".join(_MERCHANT_QUERY.split())

    assert "active_seller_channels AS" in normalized
    assert "seller.status = 'supplier_submitted'" in normalized
    assert "seller.roles @> ARRAY['seller']::text[]" in normalized
    assert "seller.last_poll_at IS NOT NULL" in normalized
    assert "channel.observed_at = seller.last_poll_at" in normalized
    assert "channel.status = 1" in normalized
    assert "channel.availability = 1" in normalized
    assert "channel.channel_type = 1" in normalized
    assert "btrim(channel.name) <> policy.forbidden_name" in normalized
    assert "btrim(channel.name) ~ policy.generated_name_pattern" in normalized
    assert "btrim(channel.name) ~ policy.generated_name_signal_pattern" in normalized
    assert "completed_retirement.state = 'completed'" in normalized
    assert normalized.count("FROM active_seller_channels c") == 3
    assert "c.observed_at = f.observed_at" not in normalized


def test_dashboard_merchant_query_binds_shared_channel_name_policy() -> None:
    captured: dict[str, Any] = {}

    class Cursor:
        description = ()

        def __enter__(self) -> Cursor:
            return self

        def __exit__(self, *_args: Any) -> bool:
            return False

        def execute(self, query: str, params: Any = None) -> None:
            captured.setdefault("calls", []).append((query, tuple(params or ())))

        def fetchall(self) -> list[Any]:
            return []

        def fetchone(self) -> Mapping[str, Any]:
            return {"readable": False}

    class Connection:
        def cursor(self) -> Cursor:
            return Cursor()

        def close(self) -> None:
            pass

    repository = PostgresTradeRepository(
        Connection,
        now=lambda: datetime(2026, 7, 31, tzinfo=UTC),
        dashboard_cache_ttl_seconds=0,
    )

    repository._load_merchant_ledgers("1h")

    merchant_query, merchant_params = captured["calls"][0]
    assert merchant_query is _MERCHANT_QUERY
    assert len(merchant_params) == _MERCHANT_QUERY.count("%s")
    assert merchant_params[-3:] == (
        FORBIDDEN_CHANNEL_NAME,
        GENERATED_CHANNEL_NAME_PATTERN,
        GENERATED_CHANNEL_NAME_SIGNAL_PATTERN,
    )


def collection_batch(*, observed_at: str = OBSERVED_AT) -> CollectionBatch:
    return CollectionBatch(
        snapshot=FinancialSnapshot(
            merchant_user_id=42,
            supplier_id=84,
            quota_per_unit=500_000,
            user_balance_quota=700,
            used_quota=300,
            request_count=9,
            withdrawable_micros=600,
            withdrawing_micros=70,
            lifetime_earned_micros=1_000,
            paid_micros=330,
            today_earned_micros=400,
            month_earned_micros=900,
            today_calls=3,
            month_calls=8,
            settlement_pending_micros=10,
            settlement_settled_micros=20,
            settlement_total_income_micros=30,
            settlement_week_new_micros=5,
            settlement_abnormal_count=0,
            markup_percent=20,
            observed_at=observed_at,
        ),
        channels=(
            SupplierChannelSnapshot(
                merchant_user_id=42,
                a6_channel_id=5,
                supplier_id=84,
                name="external",
                nickname="provider",
                channel_type=1,
                status=1,
                availability=1,
                observed_at=observed_at,
                source_hash="c" * 64,
            ),
        ),
        listings=(
            SupplierListingSnapshot(
                merchant_user_id=42,
                a6_listing_id=50,
                a6_channel_id=5,
                own_model="upstream-a",
                platform_model="model-a",
                charge_type="per_token",
                input_price_micros=500_000,
                output_price_micros=3_000_000,
                cache_read_price_micros=50_000,
                cache_write_price_micros=625_000,
                status=2,
                availability=1,
                visibility=1,
                display_name="Model A",
                description="fixture listing",
                observed_at=observed_at,
                source_hash="d" * 64,
            ),
        ),
        tokens=(
            TokenSnapshot(
                merchant_user_id=42,
                token_id=6,
                token_name="provider",
                status="1",
                unlimited_quota=True,
                expired_time=-1,
                observed_at=observed_at,
            ),
        ),
        earnings=(
            EarningEvent(
                merchant_user_id=42,
                earning_id=12,
                log_id=112,
                request_id="earning-request",
                channel_id=5,
                channel_name="external",
                platform_model="model-a",
                own_model="upstream-a",
                prompt_tokens=100,
                completion_tokens=20,
                cache_tokens=80,
                quota_micros=120,
                earned_micros=100,
                market_quota_micros=120,
                supplier_earn_micros=100,
                markup_percent=20,
                result="success",
                use_time_seconds=2,
                created_at=1_700_000_000,
            ),
        ),
        provider_usage=(
            ProviderUsageEvent(
                merchant_user_id=42,
                log_id=22,
                request_id="usage-request",
                token_id=6,
                token_name="provider",
                channel_id=7,
                channel_name="outside",
                model_name="model-a",
                route_type="pin",
                quota=90,
                prompt_tokens=50,
                completion_tokens=5,
                use_time_seconds=1,
                result_type=2,
                created_at=1_700_000_001,
            ),
        ),
        pins=(
            PinSnapshot(
                merchant_user_id=42,
                token_id=6,
                channel_id=7,
                model_name="model-a",
                status="active",
                fallback_to_smart_routing=False,
                observed_at=observed_at,
            ),
        ),
        next_watermarks=CollectorWatermarks(earning_id=12, provider_log_id=22),
        supplier_profile=SupplierProfileSnapshot(
            merchant_user_id=42,
            supplier_id=84,
            name="NexusAPI",
            description="多模型 API 中转服务",
            observed_at=observed_at,
        ),
    )


class RecordingCursor:
    def __init__(self, connection: RecordingConnection) -> None:
        self.connection = connection
        self.one: dict[str, Any] | None = None
        self.many: list[dict[str, Any]] = []

    def __enter__(self) -> RecordingCursor:
        return self

    def __exit__(self, *_args: Any) -> bool:
        return False

    def execute(self, query: str, params: Any = None) -> RecordingCursor:
        values = tuple(params or ())
        assert query.count("%s") == len(values), "SQL placeholder/parameter mismatch"
        self.connection.calls.append((query, values))
        normalized = " ".join(query.split())
        self.one = None
        self.many = []
        if normalized.startswith(
            (
                "SELECT id, last_poll_at FROM a6_trade.a6_merchants",
                "UPDATE a6_trade.a6_merchants SET poll_error_count",
            )
        ):
            self.one = (
                {"id": 10, "last_poll_at": self.connection.last_poll_at}
                if self.connection.merchant_exists
                else None
            )
        elif normalized.startswith(
            (
                "INSERT INTO a6_trade.a6_tokens",
                "SELECT id FROM a6_trade.a6_tokens",
            )
        ):
            self.one = {"id": 20}
        return self

    def fetchone(self) -> dict[str, Any] | None:
        return self.one

    def fetchall(self) -> list[dict[str, Any]]:
        return self.many


class RecordingConnection:
    def __init__(
        self,
        *,
        merchant_exists: bool = True,
        last_poll_at: datetime | None = None,
    ) -> None:
        self.merchant_exists = merchant_exists
        self.last_poll_at = last_poll_at
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.commits = 0
        self.rollbacks = 0
        self.closes = 0

    def cursor(self) -> RecordingCursor:
        return RecordingCursor(self)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        self.closes += 1


def test_persist_collection_sql_contract_is_complete_and_retry_safe() -> None:
    connection = RecordingConnection()
    repository = PostgresTradeRepository(lambda: connection)

    repository.persist_collection(collection_batch())
    repository.persist_collection(collection_batch())

    assert connection.commits == 2
    assert connection.rollbacks == 0
    assert connection.closes == 2
    queries = [" ".join(query.split()) for query, _ in connection.calls]
    financial = next(query for query in queries if "a6_financial_current" in query)
    snapshot = next(query for query in queries if "a6_financial_snapshots" in query)
    earning = next(query for query in queries if "INSERT INTO a6_trade.a6_earning_events" in query)
    usage = next(
        query for query in queries if "INSERT INTO a6_trade.a6_provider_usage_events" in query
    )
    pin_insert = next(query for query in queries if "INSERT INTO a6_trade.a6_pin_bindings" in query)
    pin_confirmation = next(
        query
        for query in queries
        if "UPDATE a6_trade.a6_pin_bindings" in query and "SET confirmed_at" in query
    )
    channel_upsert = next(
        query for query in queries if "INSERT INTO a6_trade.a6_supplier_channels_current" in query
    )
    listing_upsert = next(
        query for query in queries if "INSERT INTO a6_trade.a6_supplier_listings_current" in query
    )
    channel_delete = next(
        query for query in queries if "DELETE FROM a6_trade.a6_supplier_channels_current" in query
    )
    listing_delete = next(
        query for query in queries if "DELETE FROM a6_trade.a6_supplier_listings_current" in query
    )
    profile_update = next(
        query
        for query in queries
        if "supplier_profile_observed_at" in query and "UPDATE a6_trade.a6_merchants" in query
    )

    assert "a6_financial_current.observed_at <= EXCLUDED.observed_at" in financial
    assert "a6_financial_snapshots.observed_at <= EXCLUDED.observed_at" in snapshot
    assert "ON CONFLICT DO NOTHING" in earning
    assert "ON CONFLICT DO NOTHING" in usage
    assert "a6_supplier_channels_current.observed_at <= EXCLUDED.observed_at" in channel_upsert
    assert "a6_supplier_listings_current.observed_at <= EXCLUDED.observed_at" in listing_upsert
    assert "supplier_name = %s" in profile_update
    assert "supplier_description = %s" in profile_update
    assert "supplier_profile_observed_at <= %s" in profile_update
    assert "managed_state" not in listing_upsert
    assert "a6_managed_channel_bindings" in channel_delete
    assert "a6_managed_listing_bindings" in listing_delete
    assert "current_channel.observed_at <= %s" in channel_delete
    assert "current_listing.observed_at <= %s" in listing_delete
    assert "AND channel_id = %s" not in pin_insert
    assert "WHERE a6_token_id = %s AND model = %s AND valid_to IS NULL" in pin_insert
    assert "decision.selected_channel_id = %s" in pin_insert
    assert "decision.decision_status = 'completed'" in pin_insert
    assert "decision.decided_at <= %s" in pin_insert
    assert "decision_id = coalesce(decision_id" in pin_confirmation
    assert "decision.selected_channel_id = %s" in pin_confirmation
    assert "decision.decision_status = 'completed'" in pin_confirmation
    assert "decision.decided_at <= %s" in pin_confirmation
    assert "confirmed_at IS NULL OR decision_id IS NULL" in pin_confirmation
    assert any(
        "UPDATE a6_trade.trade_incidents SET status = 'resolved'" in query for query in queries
    )


def test_empty_supplier_inventory_still_cleans_current_rows() -> None:
    batch = replace(collection_batch(), channels=(), listings=())
    connection = RecordingConnection()

    PostgresTradeRepository(lambda: connection).persist_collection(batch)

    calls = [(" ".join(query.split()), params) for query, params in connection.calls]
    channel_delete = next(
        call for call in calls if "DELETE FROM a6_trade.a6_supplier_channels_current" in call[0]
    )
    listing_delete = next(
        call for call in calls if "DELETE FROM a6_trade.a6_supplier_listings_current" in call[0]
    )
    assert channel_delete[1][2] == []
    assert listing_delete[1][2] == []


def test_older_collection_does_not_resurrect_current_supplier_inventory() -> None:
    connection = RecordingConnection(
        last_poll_at=datetime(2026, 7, 24, 5, 0, tzinfo=UTC),
    )

    PostgresTradeRepository(lambda: connection).persist_collection(collection_batch())

    queries = [" ".join(query.split()) for query, _ in connection.calls]
    assert not any("a6_supplier_channels_current" in query for query in queries)
    assert not any("a6_supplier_listings_current" in query for query in queries)
    assert any("a6_financial_current" in query for query in queries)
    assert connection.commits == 1


def test_reused_provider_log_id_uses_request_identity_and_inserts_new_request() -> None:
    original = collection_batch().provider_usage[0]
    old_event = replace(original, log_id=1, request_id="old-request")
    new_event = replace(original, log_id=1, request_id="new-request", created_at=1_700_000_002)
    batch = replace(collection_batch(), provider_usage=(old_event, new_event))
    connection = RecordingConnection()

    PostgresTradeRepository(lambda: connection).persist_collection(batch)

    usage_calls = [
        (query, params)
        for query, params in connection.calls
        if "INSERT INTO a6_trade.a6_provider_usage_events" in query
    ]
    assert len(usage_calls) == 2
    keys = [params[1] for _, params in usage_calls]
    assert keys[0].startswith("v2:request:")
    assert keys[1].startswith("v2:request:")
    assert keys[0] != keys[1]
    assert all(key != "1" for key in keys)
    assert [params[2] for _, params in usage_calls] == ["old-request", "new-request"]
    assert all("WHERE NOT EXISTS" in query for query, _ in usage_calls)

    # Model the two existing uniqueness constraints: the legacy numeric ID blocks
    # neither v2 key, while request_id suppresses only the already-seen request.
    stored_keys = {"1"}
    stored_requests = {"old-request"}
    inserted_requests = []
    for _, params in usage_calls:
        storage_key, request_id = params[1], params[2]
        if storage_key in stored_keys or request_id in stored_requests:
            continue
        stored_keys.add(storage_key)
        stored_requests.add(request_id)
        inserted_requests.append(request_id)
    assert inserted_requests == ["new-request"]


def test_provider_usage_fingerprint_is_stable_when_window_log_id_changes() -> None:
    event = replace(collection_batch().provider_usage[0], request_id="", log_id=1)
    renumbered = replace(event, log_id=86)

    assert provider_usage_identity(event) == provider_usage_identity(renumbered)
    assert provider_usage_storage_key(event) == provider_usage_storage_key(renumbered)


def test_persist_collection_rolls_back_when_session_metadata_was_not_imported() -> None:
    connection = RecordingConnection(merchant_exists=False)
    repository = PostgresTradeRepository(lambda: connection)

    with pytest.raises(LookupError, match="collector_merchant_not_imported"):
        repository.persist_collection(collection_batch())

    assert connection.commits == 0
    assert connection.rollbacks == 1
    assert connection.closes == 1


def test_collection_failure_updates_counter_and_secret_free_incident() -> None:
    connection = RecordingConnection()
    repository = PostgresTradeRepository(lambda: connection)

    repository.record_collection_failure("session-ref", "Timeout with private detail")

    assert connection.commits == 1
    assert connection.rollbacks == 0
    calls = [(" ".join(query.split()), params) for query, params in connection.calls]
    incident = next(call for call in calls if "INSERT INTO a6_trade.trade_incidents" in call[0])
    assert incident[1] == ("collector:10:collector_error", "collector_error", "10")
    assert "private detail" not in repr(calls)


class DashboardCursor:
    def __init__(self, connection: DashboardConnection) -> None:
        self.connection = connection
        self.one: dict[str, Any] | None = None
        self.many: list[dict[str, Any]] = []

    def __enter__(self) -> DashboardCursor:
        return self

    def __exit__(self, *_args: Any) -> bool:
        return False

    def execute(self, query: str, params: Any = None) -> DashboardCursor:
        values = tuple(params or ())
        assert query.count("%s") == len(values), "SQL placeholder/parameter mismatch"
        self.connection.calls.append((query, values))
        normalized = " ".join(query.split())
        self.one = None
        self.many = []
        if "managed_internal_debits AS" in normalized:
            self.many = [dict(row) for row in self.connection.managed_internal_debit_rows]
        elif "unique_matches AS" in normalized:
            self.many = [
                {
                    "merchant_id": self.connection.merchant_row["merchant_id"],
                    "external_enodot_revenue_micros": self.connection.sales_micros,
                    "external_enodot_request_count": self.connection.sales_requests,
                    "confirmed_external_enodot_revenue_micros": (
                        self.connection.confirmed_sales_micros
                    ),
                    "confirmed_external_enodot_request_count": (
                        self.connection.confirmed_sales_requests
                    ),
                    "unattributed_external_enodot_revenue_micros": (
                        self.connection.unattributed_sales_micros
                    ),
                    "unattributed_external_enodot_request_count": (
                        self.connection.unattributed_sales_requests
                    ),
                    "external_provider_cost_micros": (
                        self.connection.external_provider_cost_micros
                    ),
                    "external_provider_event_count": (
                        self.connection.external_provider_event_count
                    ),
                    "managed_internal_provider_cost_micros": (
                        self.connection.managed_internal_provider_cost_micros
                    ),
                    "managed_internal_provider_event_count": (
                        self.connection.managed_internal_provider_event_count
                    ),
                    "owner_internal_debit_micros": (self.connection.owner_internal_debit_micros),
                    "owner_internal_request_count": (self.connection.owner_internal_request_count),
                    "owner_internal_provider_cost_micros": (
                        self.connection.owner_internal_provider_cost_micros
                    ),
                    "owner_internal_provider_event_count": (
                        self.connection.owner_internal_provider_event_count
                    ),
                }
            ]
        elif normalized.startswith("WITH money AS"):
            self.many = [dict(self.connection.merchant_row)]
        elif normalized.startswith("WITH stock_params AS"):
            self.one = dict(self.connection.stock_window)
        elif "has_table_privilege" in normalized:
            self.one = {"readable": self.connection.usage_logs_readable}
        elif "AS merchant_count" in normalized and "FROM a6_trade.a6_merchants" in normalized:
            self.one = dict(self.connection.health_row)
        return self

    def fetchone(self) -> dict[str, Any] | None:
        return self.one

    def fetchall(self) -> list[dict[str, Any]]:
        return self.many


class DashboardConnection:
    def __init__(
        self,
        *,
        usage_logs_readable: bool,
        sales_micros: int = 0,
        sales_requests: int = 0,
        binding_count: int = 0,
        unattributed_sales_micros: int = 0,
        unattributed_sales_requests: int = 0,
    ) -> None:
        self.usage_logs_readable = usage_logs_readable
        self.sales_micros = sales_micros
        self.sales_requests = sales_requests
        self.unattributed_sales_micros = unattributed_sales_micros
        self.unattributed_sales_requests = unattributed_sales_requests
        self.confirmed_sales_micros = sales_micros - unattributed_sales_micros
        self.confirmed_sales_requests = sales_requests - unattributed_sales_requests
        self.external_provider_cost_micros = (
            90 if binding_count and self.confirmed_sales_requests else 0
        )
        self.external_provider_event_count = (
            1 if binding_count and self.confirmed_sales_requests else 0
        )
        self.managed_internal_provider_cost_micros = 0
        self.managed_internal_provider_event_count = 0
        self.owner_internal_debit_micros = 0
        self.owner_internal_request_count = 0
        self.owner_internal_provider_cost_micros = 0
        self.owner_internal_provider_event_count = 0
        self.managed_internal_debit_rows: list[dict[str, Any]] = []
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.merchant_row = {
            "merchant_id": 10,
            "display_name": "merchant-0010",
            "roles": ["provider"],
            "session_status": "supplier_submitted",
            "last_observed_at": datetime(2026, 7, 24, 4, 58, tzinfo=UTC),
            "poll_error_count": 0,
            "provider_cost_micros": 90,
            "confirmed_provider_cost_micros": 90 if binding_count else 0,
            "unattributed_provider_cost_micros": 0 if binding_count else 90,
            "provider_event_count": 1,
            "unattributed_provider_event_count": 0 if binding_count else 1,
            "seller_income_micros": 100,
            "external_earned_micros": 100,
            "own_boost_earned_micros": 0,
            "own_pin_earned_micros": 0,
            "unclassified_earned_micros": 0,
            "earning_event_count": 1,
            "unclassified_earning_event_count": 0,
            "boost_gross_cost_micros": 0,
            "boost_event_count": 0,
            "boost_net_loss_micros": 0,
            "probe_gross_cost_micros": 0,
            "probe_recovery_micros": 0,
            "probe_loss_micros": 0,
            "pin_probe_cost_micros": 0,
            "pin_probe_event_count": 0,
            "withdrawal_fee_micros": 0,
            "enodot_account_count": binding_count,
        }
        self.health_row = {
            "merchant_count": 1,
            "last_success_at": self.merchant_row["last_observed_at"],
            "error_count": 0,
        }
        self.stock_window = {
            f"{name}_{field}": 0
            for name in ("user_balance", "withdrawable", "withdrawing")
            for field in ("start", "current", "min", "max")
        }

    def cursor(self) -> DashboardCursor:
        return DashboardCursor(self)

    def close(self) -> None:
        pass


def test_merchants_mark_profit_unavailable_when_usage_logs_is_not_readable() -> None:
    now = datetime(2026, 7, 24, 5, 0, tzinfo=UTC)
    connection = DashboardConnection(usage_logs_readable=False)
    repository = PostgresTradeRepository(lambda: connection, now=lambda: now)

    merchants = repository.get_merchants("1h")

    assert merchants[0]["enodot_sales_micros"] == 0
    assert merchants[0]["sales_status"] == "unavailable"
    assert merchants[0]["profit_status"] == "unavailable"
    assert merchants[0]["actual_profit_micros"] is None
    assert merchants[0]["honest_profit_micros"] is None
    assert merchants[0]["unattributed_provider_cost_micros"] == 90
    assert merchants[0]["session_status"] == "healthy"
    assert not any("FROM public.usage_logs" in query for query, _ in connection.calls)
    merchant_query, merchant_params = connection.calls[0]
    expected_since = datetime(2026, 7, 24, 4, 0, tzinfo=UTC)
    assert merchant_params == (
        1_000_000,
        *(expected_since,) * 7,
        FORBIDDEN_CHANNEL_NAME,
        GENERATED_CHANNEL_NAME_PATTERN,
        GENERATED_CHANNEL_NAME_SIGNAL_PATTERN,
    )
    assert "FROM a6_trade.a6_supplier_listings_current l" in merchant_query
    assert "FROM a6_trade.a6_managed_listing_bindings b" in merchant_query
    assert "b.control_state = 'verified'" in merchant_query
    assert "b.managed_uid <> ''" in merchant_query
    assert "b.verified_at IS NOT NULL" in merchant_query
    assert "WHERE EXISTS" in merchant_query
    assert "coalesce(inventory.active_listing_count, 0)" in merchant_query
    assert "existing_unmanaged_listing_count" in merchant_query
    assert "managed_active_listing_count" in merchant_query
    assert "u.probe_run_id IS NULL" in merchant_query
    assert "b.created_at <= u.occurred_at" in merchant_query
    assert "probe_recovery_micros" in merchant_query
    assert "action.action_type = 'provider_pin_probe'" in merchant_query
    assert "action_candidate_count = 1 AND event_candidate_count = 1" in merchant_query
    assert "probe.provider_event_id = u.id" in merchant_query
    assert "pin_probe_cost_micros" in merchant_query
    assert "i.status = 'open'" in merchant_query


def test_merchants_keep_pre_binding_provider_cost_pending() -> None:
    now = datetime(2026, 7, 24, 5, 0, tzinfo=UTC)
    connection = DashboardConnection(usage_logs_readable=True)
    repository = PostgresTradeRepository(lambda: connection, now=lambda: now)

    merchant = repository.get_merchants("1h")[0]

    assert merchant["sales_status"] == "pending"
    assert merchant["profit_status"] == "partial"
    assert merchant["confirmed_provider_cost_micros"] == 0
    assert merchant["unattributed_provider_cost_micros"] == 90
    assert merchant["actual_profit_micros"] is None
    assert merchant["honest_profit_micros"] is None


def test_merchants_merge_enodot_sales_when_usage_logs_is_readable() -> None:
    now = datetime(2026, 7, 24, 5, 0, tzinfo=UTC)
    connection = DashboardConnection(
        usage_logs_readable=True,
        sales_micros=400,
        sales_requests=4,
        binding_count=1,
    )
    repository = PostgresTradeRepository(lambda: connection, now=lambda: now)

    merchants = repository.get_merchants("1h")

    assert merchants[0]["enodot_sales_micros"] == 400
    assert merchants[0]["external_enodot_revenue_micros"] == 400
    assert merchants[0]["external_enodot_request_count"] == 4
    assert merchants[0]["confirmed_external_enodot_revenue_micros"] == 400
    assert merchants[0]["confirmed_enodot_sales_micros"] == 400
    assert merchants[0]["unattributed_enodot_sales_micros"] == 0
    assert merchants[0]["actual_profit_micros"] == 410
    assert merchants[0]["confirmed_provider_cost_micros"] == 90
    assert merchants[0]["external_provider_cost_micros"] == 90
    assert merchants[0]["managed_internal_provider_cost_micros"] == 0
    assert merchants[0]["unattributed_provider_cost_micros"] == 0
    assert merchants[0]["sales_status"] == "confirmed"
    assert merchants[0]["profit_status"] == "confirmed"
    assert merchants[0]["profit_margin"] == Decimal("1.025")
    sales_calls = [call for call in connection.calls if "unique_matches AS" in call[0]]
    assert sales_calls == [
        (
            sales_calls[0][0],
            (1_000_000, datetime(2026, 7, 24, 4, 0, tzinfo=UTC)),
        )
    ]
    assert "b.created_at <= u.created_at" in sales_calls[0][0]
    assert "sum(actual_cost)" in sales_calls[0][0]
    assert "sum(u.total_cost)" not in sales_calls[0][0]


def test_enodot_sales_use_per_key_delta_adjusted_actual_cost() -> None:
    connection = DashboardConnection(
        usage_logs_readable=True,
        sales_micros=150,
        sales_requests=1,
        binding_count=1,
    )
    repository = PostgresTradeRepository(
        lambda: connection,
        now=lambda: datetime(2026, 7, 24, 5, 0, tzinfo=UTC),
    )

    merchant = repository.get_merchants("1h")[0]
    sales_query = next(query for query, _params in connection.calls if "unique_matches AS" in query)

    # With delta=0.5 a base total_cost=0.0001 becomes actual_cost=0.00015.
    assert merchant["enodot_sales_micros"] == 150
    assert merchant["actual_profit_micros"] == 160
    assert "sum(actual_cost)" in sales_query
    assert "sum(u.total_cost)" not in sales_query
    assert "managed.api_key_id = u.api_key_id" in sales_query
    assert "event.probe_run_id IS NULL" in sales_query


def test_enodot_sales_query_uniquely_matches_external_revenue_to_provider_cost() -> None:
    connection = DashboardConnection(
        usage_logs_readable=True,
        sales_micros=150,
        sales_requests=1,
        binding_count=1,
    )
    PostgresTradeRepository(
        lambda: connection,
        now=lambda: datetime(2026, 7, 24, 5, 0, tzinfo=UTC),
    ).get_merchants("1h")

    query = next(query for query, _params in connection.calls if "unique_matches AS" in query)

    assert "b.enodot_account_id = u.account_id" in query
    assert "b.created_at <= u.created_at" in query
    assert "sum(actual_cost)" in query
    assert "WHEN managed.api_key_id IS NOT NULL THEN 'managed_internal'" in query
    assert "event.a6_token_ref_id = usage.a6_token_ref_id" in query
    assert "event.model = usage.candidate_model" in query
    assert "event.match_bucket = usage.match_bucket" in query
    assert "event.prompt_tokens = usage.prompt_tokens" in query
    assert "event.completion_tokens = usage.completion_tokens" in query
    assert "usage.created_at - params.match_tolerance" in query
    assert "usage_candidate_count = 1" in query
    assert "provider_event_candidate_count = 1" in query
    assert "event.probe_run_id IS NULL" in query
    assert "event.request_id" not in query
    assert "u.request_id" not in query


def test_uncollected_provider_cost_keeps_enodot_sales_out_of_actual_profit() -> None:
    connection = DashboardConnection(
        usage_logs_readable=True,
        sales_micros=6_208_343,
        sales_requests=85,
        binding_count=1,
        unattributed_sales_micros=6_208_343,
        unattributed_sales_requests=85,
    )
    connection.merchant_row.update(
        provider_cost_micros=0,
        confirmed_provider_cost_micros=0,
        unattributed_provider_cost_micros=0,
        provider_event_count=0,
        unattributed_provider_event_count=0,
        seller_income_micros=0,
    )
    repository = PostgresTradeRepository(
        lambda: connection,
        now=lambda: datetime(2026, 7, 24, 5, 0, tzinfo=UTC),
    )

    merchant = repository.get_merchants("1h")[0]
    overview = repository.get_overview("1h")

    assert merchant["enodot_sales_micros"] == 6_208_343
    assert merchant["confirmed_enodot_sales_micros"] == 0
    assert merchant["unattributed_enodot_sales_micros"] == 6_208_343
    assert merchant["unattributed_enodot_sales_request_count"] == 85
    assert merchant["profit_status"] == "partial"
    assert merchant["actual_profit_micros"] is None
    assert merchant["honest_profit_micros"] is None
    assert merchant["profit_margin"] is None
    assert overview["flows"]["actual_profit_micros"] is None
    assert overview["flows"]["profit_status"] == "partial"
    assert overview["flows"]["unattributed_enodot_sales_micros"] == 6_208_343
    assert overview["flows"]["unattributed_enodot_sales_request_count"] == 85


def test_partially_attributed_sales_are_not_reported_as_actual_profit() -> None:
    connection = DashboardConnection(
        usage_logs_readable=True,
        sales_micros=1_000,
        sales_requests=2,
        binding_count=1,
        unattributed_sales_micros=400,
        unattributed_sales_requests=1,
    )
    connection.merchant_row.update(
        provider_cost_micros=200,
        confirmed_provider_cost_micros=200,
        unattributed_provider_cost_micros=0,
        provider_event_count=1,
        unattributed_provider_event_count=0,
        seller_income_micros=0,
    )
    connection.external_provider_cost_micros = 200
    connection.external_provider_event_count = 1
    repository = PostgresTradeRepository(
        lambda: connection,
        now=lambda: datetime(2026, 7, 24, 5, 0, tzinfo=UTC),
    )

    merchant = repository.get_merchants("1h")[0]

    assert merchant["profit_status"] == "partial"
    assert merchant["actual_profit_micros"] is None
    assert merchant["honest_profit_micros"] is None
    assert merchant["confirmed_enodot_sales_request_count"] == 1
    assert merchant["unattributed_enodot_sales_request_count"] == 1


def test_managed_internal_debit_is_reported_but_not_counted_as_external_cost() -> None:
    connection = DashboardConnection(usage_logs_readable=True)
    connection.merchant_row.update(
        roles=["seller"],
        provider_cost_micros=0,
        confirmed_provider_cost_micros=0,
        unattributed_provider_cost_micros=0,
        provider_event_count=0,
        unattributed_provider_event_count=0,
        seller_income_micros=241_494,
    )
    connection.managed_internal_debit_rows = [
        {
            "merchant_id": 10,
            "managed_internal_debit_micros": 25_089_294,
            "managed_internal_request_count": 216,
        },
    ]
    repository = PostgresTradeRepository(
        lambda: connection,
        now=lambda: datetime(2026, 7, 25, 8, 0, tzinfo=UTC),
    )

    merchant = repository.get_merchants("24h")[0]
    overview = repository.get_overview("24h")

    assert merchant["enodot_provider_cost_micros"] == 25_089_294
    assert merchant["managed_internal_debit_micros"] == 25_089_294
    assert merchant["managed_internal_request_count"] == 216
    assert merchant["unattributed_enodot_provider_cost_micros"] == 0
    assert merchant["unattributed_enodot_provider_event_count"] == 0
    assert merchant["profit_status"] == "confirmed"
    assert merchant["actual_profit_micros"] == 241_494
    assert merchant["honest_profit_micros"] == 241_494
    assert overview["flows"]["managed_internal_debit_micros"] == 25_089_294
    assert overview["flows"]["managed_internal_request_count"] == 216
    assert overview["flows"]["profit_status"] == "confirmed"
    assert overview["flows"]["actual_profit_micros"] == 241_494
    cost_query = next(
        query for query, _params in connection.calls if "managed_internal_debits AS" in query
    )
    assert "managed.enodot_api_key_id = u.api_key_id" in cost_query
    assert "legacy_candidate_matches" not in cost_query


def test_managed_seller_key_debit_does_not_reduce_operating_profit() -> None:
    connection = DashboardConnection(usage_logs_readable=True)
    connection.merchant_row.update(
        roles=["seller"],
        provider_cost_micros=0,
        confirmed_provider_cost_micros=0,
        unattributed_provider_cost_micros=0,
        provider_event_count=0,
        unattributed_provider_event_count=0,
        seller_income_micros=1_000,
    )
    connection.managed_internal_debit_rows = [
        {
            "merchant_id": 10,
            "managed_internal_debit_micros": 400,
            "managed_internal_request_count": 1,
        }
    ]
    repository = PostgresTradeRepository(
        lambda: connection,
        now=lambda: datetime(2026, 7, 25, 8, 0, tzinfo=UTC),
    )

    merchant = repository.get_merchants("1h")[0]

    assert merchant["profit_status"] == "confirmed"
    assert merchant["actual_profit_micros"] == 1_000
    assert merchant["honest_profit_micros"] == 1_000
    assert merchant["managed_internal_debit_micros"] == 400
    assert merchant["confirmed_enodot_provider_cost_micros"] == 400


def test_managed_internal_request_subtracts_real_a6_cost_exactly_once() -> None:
    connection = DashboardConnection(usage_logs_readable=True, binding_count=1)
    connection.merchant_row.update(
        roles=["seller", "provider"],
        provider_cost_micros=50,
        provider_event_count=1,
        seller_income_micros=1_000,
    )
    connection.managed_internal_provider_cost_micros = 50
    connection.managed_internal_provider_event_count = 1
    connection.managed_internal_debit_rows = [
        {
            "merchant_id": 10,
            "managed_internal_debit_micros": 400,
            "managed_internal_request_count": 1,
        }
    ]
    repository = PostgresTradeRepository(
        lambda: connection,
        now=lambda: datetime(2026, 7, 25, 8, 0, tzinfo=UTC),
    )

    merchant = repository.get_merchants("1h")[0]

    assert merchant["managed_internal_debit_micros"] == 400
    assert merchant["managed_internal_provider_cost_micros"] == 50
    assert merchant["confirmed_provider_cost_micros"] == 50
    assert merchant["unattributed_provider_cost_micros"] == 0
    assert merchant["actual_profit_micros"] == 950
    assert merchant["honest_profit_micros"] == 950
    assert merchant["profit_status"] == "confirmed"


def test_owner_activation_is_not_external_revenue_but_keeps_real_provider_cost() -> None:
    connection = DashboardConnection(usage_logs_readable=True, binding_count=1)
    connection.merchant_row.update(
        roles=["seller", "provider"],
        provider_cost_micros=398,
        provider_event_count=1,
        seller_income_micros=0,
    )
    connection.owner_internal_debit_micros = 27_593
    connection.owner_internal_request_count = 1
    connection.owner_internal_provider_cost_micros = 398
    connection.owner_internal_provider_event_count = 1
    repository = PostgresTradeRepository(
        lambda: connection,
        now=lambda: datetime(2026, 7, 25, 10, 0, tzinfo=UTC),
    )

    merchant = repository.get_merchants("1h")[0]
    query = next(query for query, _params in connection.calls if "unique_matches AS" in query)

    assert merchant["external_enodot_revenue_micros"] == 0
    assert merchant["external_enodot_request_count"] == 0
    assert merchant["owner_internal_debit_micros"] == 27_593
    assert merchant["owner_internal_request_count"] == 1
    assert merchant["owner_internal_provider_cost_micros"] == 398
    assert merchant["owner_internal_provider_event_count"] == 1
    assert merchant["confirmed_provider_cost_micros"] == 398
    assert merchant["unattributed_provider_cost_micros"] == 0
    assert merchant["actual_profit_micros"] == -398
    assert merchant["honest_profit_micros"] == -398
    assert merchant["profit_status"] == "confirmed"
    assert "a6_trade.enodot_usage_classification_rules" in query
    assert "rule.enodot_user_id = u.user_id" in query
    assert "rule.enodot_api_key_id = u.api_key_id" in query
    assert "rule.valid_from <= u.created_at" in query
    assert "owner_activation" in query


def test_operating_profit_charges_all_known_cost_and_net_probe_loss() -> None:
    now = datetime(2026, 7, 24, 5, 0, tzinfo=UTC)
    connection = DashboardConnection(
        usage_logs_readable=True,
        sales_micros=1_000,
        sales_requests=1,
        binding_count=1,
    )
    connection.merchant_row.update(
        provider_cost_micros=300,
        confirmed_provider_cost_micros=200,
        unattributed_provider_cost_micros=100,
        provider_event_count=2,
        unattributed_provider_event_count=1,
        seller_income_micros=0,
        probe_gross_cost_micros=300,
        probe_recovery_micros=60,
        probe_loss_micros=240,
        withdrawal_fee_micros=10,
    )
    connection.external_provider_cost_micros = 200
    connection.external_provider_event_count = 1
    repository = PostgresTradeRepository(lambda: connection, now=lambda: now)

    merchant = repository.get_merchants("1h")[0]
    overview = repository.get_overview("1h")

    assert merchant["actual_profit_micros"] == 450
    assert merchant["honest_profit_micros"] == 450
    assert merchant["profit_status"] == "confirmed"
    assert merchant["profit_margin"] == Decimal("0.45")
    assert overview["flows"]["actual_profit_micros"] == 450
    assert overview["flows"]["honest_profit_micros"] == 450
    assert overview["flows"]["confirmed_provider_cost_micros"] == 200
    assert overview["flows"]["unattributed_provider_cost_micros"] == 100
    assert overview["flows"]["probe_gross_cost_micros"] == 300
    assert overview["flows"]["probe_recovery_micros"] == 60
    assert overview["flows"]["probe_loss_micros"] == 240
    assert overview["flows"]["withdrawal_fee_micros"] == 10
    assert overview["flows"]["profit_status"] == "confirmed"


def test_pin_health_probe_is_separate_from_customer_provider_cost() -> None:
    connection = DashboardConnection(
        usage_logs_readable=True,
        sales_micros=1_000,
        sales_requests=1,
        binding_count=1,
    )
    connection.merchant_row.update(
        provider_cost_micros=100,
        provider_event_count=1,
        seller_income_micros=0,
        probe_gross_cost_micros=20,
        probe_recovery_micros=0,
        probe_loss_micros=20,
        pin_probe_cost_micros=20,
        pin_probe_event_count=1,
    )
    connection.external_provider_cost_micros = 100
    connection.external_provider_event_count = 1
    repository = PostgresTradeRepository(
        lambda: connection,
        now=lambda: datetime(2026, 7, 25, 10, 0, tzinfo=UTC),
    )

    merchant = repository.get_merchants("1h")[0]

    assert merchant["provider_cost_micros"] == 100
    assert merchant["confirmed_provider_cost_micros"] == 100
    assert merchant["pin_probe_cost_micros"] == 20
    assert merchant["pin_probe_event_count"] == 1
    assert merchant["probe_gross_cost_micros"] == 20
    assert merchant["probe_loss_micros"] == 20
    assert merchant["actual_profit_micros"] == 880
    assert merchant["profit_status"] == "confirmed"


def test_seller_only_profit_does_not_require_enodot_binding() -> None:
    now = datetime(2026, 7, 24, 5, 0, tzinfo=UTC)
    connection = DashboardConnection(usage_logs_readable=True)
    connection.merchant_row.update(
        roles=["seller"],
        provider_cost_micros=0,
        confirmed_provider_cost_micros=0,
        unattributed_provider_cost_micros=0,
        provider_event_count=0,
        unattributed_provider_event_count=0,
    )
    repository = PostgresTradeRepository(lambda: connection, now=lambda: now)

    merchant = repository.get_merchants("1h")[0]

    assert merchant["sales_status"] == "not_applicable"
    assert merchant["profit_status"] == "confirmed"
    assert merchant["actual_profit_micros"] == 100
    assert merchant["profit_margin"] is None


def test_rejected_supplier_does_not_keep_dashboard_in_error_state() -> None:
    now = datetime(2026, 7, 24, 5, 0, tzinfo=UTC)
    connection = DashboardConnection(usage_logs_readable=True)
    connection.merchant_row.update(
        roles=["seller"],
        session_status="supplier_rejected",
        poll_error_count=12,
    )
    repository = PostgresTradeRepository(lambda: connection, now=lambda: now)

    merchant = repository.get_merchants("1h")[0]

    assert merchant["status"] == "pending"


def test_imported_but_uncollected_merchants_are_not_reported_healthy_or_fresh() -> None:
    now = datetime(2026, 7, 24, 5, 0, tzinfo=UTC)
    connection = DashboardConnection(usage_logs_readable=False)
    connection.health_row.update(merchant_count=19, last_success_at=None)
    connection.merchant_row["last_observed_at"] = None
    repository = PostgresTradeRepository(lambda: connection, now=lambda: now)

    health = repository.health()
    overview = repository.get_overview("1h")

    assert health["status"] == "degraded"
    assert health["last_success_at"] is None
    assert overview["freshness"]["status"] == "unknown"
    assert overview["freshness"]["last_observed_at"] is None


def test_health_only_counts_collector_eligible_supplier_sessions() -> None:
    connection = DashboardConnection(usage_logs_readable=False)
    repository = PostgresTradeRepository(lambda: connection)

    repository.health()

    query = next(
        query
        for query, _params in connection.calls
        if "count(*) FILTER" in query
    )
    assert query.count("WHERE status = 'supplier_submitted'") == 3


def test_overview_preserves_delayed_freshness_state() -> None:
    now = datetime(2026, 7, 24, 5, 0, tzinfo=UTC)
    connection = DashboardConnection(usage_logs_readable=False)
    connection.merchant_row["last_observed_at"] = datetime(2026, 7, 24, 4, 40, tzinfo=UTC)
    repository = PostgresTradeRepository(lambda: connection, now=lambda: now)

    overview = repository.get_overview("1h")

    assert overview["freshness"]["status"] == "delayed"


def test_overview_uses_complete_per_merchant_stock_baseline() -> None:
    now = datetime(2026, 7, 24, 5, 0, tzinfo=UTC)
    since = datetime(2026, 7, 24, 4, 0, tzinfo=UTC)
    connection = DashboardConnection(usage_logs_readable=False)
    connection.stock_window.update(
        user_balance_start=350,
        user_balance_current=360,
        user_balance_min=330,
        user_balance_max=380,
        withdrawable_start=80,
        withdrawable_current=70,
        withdrawable_min=60,
        withdrawable_max=90,
    )
    repository = PostgresTradeRepository(lambda: connection, now=lambda: now)

    overview = repository.get_overview("1h")

    assert overview["stocks"]["user_balance"] == {
        "start_micros": 350,
        "current_micros": 360,
        "change_micros": 10,
        "min_micros": 330,
        "max_micros": 380,
    }
    stock_call = next(call for call in connection.calls if "WITH stock_params AS" in call[0])
    assert stock_call[1] == (since, now, 1_000_000)
    assert "GROUP BY observed_bucket_5m" not in stock_call[0]
    assert "CASE WHEN s.observed_at <= p.since" in stock_call[0]


@pytest.mark.parametrize(
    ("window", "expected"),
    [
        ("5m", datetime(2026, 7, 24, 4, 55, tzinfo=UTC)),
        ("mtd", datetime(2026, 7, 1, 0, 0, tzinfo=UTC)),
        ("all", datetime(1970, 1, 1, tzinfo=UTC)),
    ],
)
def test_window_start_is_deterministic(window: str, expected: datetime) -> None:
    assert _since(window, datetime(2026, 7, 24, 5, 0, tzinfo=UTC)) == expected


def test_phase0_migration_indexes_repository_join_paths() -> None:
    migration = Path("migrations/0001_a6_trade_phase0.sql").read_text(encoding="utf-8")

    assert "CREATE UNIQUE INDEX IF NOT EXISTS enodot_account_bindings_account_idx" in migration
    assert "a6_market_quotes_owned_supplier_idx" in migration
    assert "trade_incidents_entity_time_idx" in migration


def test_booster_earnings_are_not_revenue_and_their_net_loss_is_charged() -> None:
    """抖加 money returning to us is a loss, not income.

    Measured on 2026-07-26, booster traffic cost 6,152,145 quota and returned
    3,897,294 quota of "earnings" -- the A6 platform cut makes self-buying strictly
    lossy. Crediting the return as seller income is what made a $4.51 loss read as a
    profit, so the ledger must exclude it and charge the net separately.
    """

    connection = DashboardConnection(
        usage_logs_readable=True,
        sales_micros=1_000,
        sales_requests=1,
        binding_count=1,
    )
    connection.merchant_row.update(
        provider_cost_micros=100,
        confirmed_provider_cost_micros=100,
        unattributed_provider_cost_micros=0,
        provider_event_count=1,
        unattributed_provider_event_count=0,
        # Only the external slice reaches seller_income_micros.
        seller_income_micros=300,
        external_earned_micros=300,
        own_boost_earned_micros=800,
        earning_event_count=2,
        unclassified_earning_event_count=0,
        boost_gross_cost_micros=1_000,
        boost_event_count=5,
        boost_net_loss_micros=200,
    )
    connection.external_provider_cost_micros = 100
    connection.external_provider_event_count = 1
    repository = PostgresTradeRepository(
        lambda: connection,
        now=lambda: datetime(2026, 7, 24, 5, 0, tzinfo=UTC),
    )

    merchant = repository.get_merchants("1h")[0]

    assert merchant["attribution_coverage"] == Decimal("1.0000")
    # 1000 confirmed sales - 100 confirmed cost + 300 external earnings - 200 boost net
    assert merchant["honest_profit_micros"] == 1_000
    assert merchant["seller_income_micros"] == 300
    assert merchant["own_boost_earned_micros"] == 800
    assert merchant["boost_net_loss_micros"] == 200
    sales_query = next(query for query, _ in connection.calls if "unique_matches AS" in query)
    merchant_query = next(query for query, _ in connection.calls if "boost_flow AS" in query)
    assert "booster_event.a6_token_ref_id = event.a6_token_ref_id" not in sales_query
    assert "booster_event.a6_token_ref_id = u.a6_token_ref_id" in merchant_query
    assert "provider_binding.a6_token_id = u.a6_token_ref_id" in merchant_query
    assert "provider_binding.created_at <= u.occurred_at" in merchant_query


def test_bound_provider_identity_takes_precedence_over_stale_booster_marker() -> None:
    normalized = " ".join(_MERCHANT_QUERY.split())
    boost_flow = normalized.split("), boost_flow AS (", 1)[1].split(
        "), provider_flow AS (", 1
    )[0]
    provider_flow = normalized.split("), provider_flow AS (", 1)[1].split(
        "), transfer_flow AS (", 1
    )[0]

    assert "JOIN a6_trade.a6_channel_booster_tokens booster" in boost_flow
    assert "provider_binding.a6_token_id = u.a6_token_ref_id" in boost_flow
    assert "provider_binding.created_at <= u.occurred_at" in boost_flow
    assert "booster_event.a6_token_ref_id = u.a6_token_ref_id" in provider_flow
    assert "AND NOT EXISTS ( SELECT 1 FROM a6_trade.enodot_account_bindings" in provider_flow
    assert "provider_binding.a6_token_id = u.a6_token_ref_id" in provider_flow
    assert "provider_binding.created_at <= u.occurred_at" in provider_flow


def test_profit_is_withheld_when_revenue_coverage_is_below_the_floor() -> None:
    """Unsettled revenue can still change profit, while known A6 cost is charged fully."""

    connection = DashboardConnection(
        usage_logs_readable=True,
        sales_micros=10_000,
        sales_requests=10,
        binding_count=1,
    )
    connection.merchant_row.update(
        provider_cost_micros=1_000,
        confirmed_provider_cost_micros=200,
        unattributed_provider_cost_micros=800,
        provider_event_count=10,
        unattributed_provider_event_count=8,
        seller_income_micros=0,
        external_earned_micros=0,
        earning_event_count=10,
        unclassified_earning_event_count=8,
    )
    connection.external_provider_cost_micros = 200
    connection.external_provider_event_count = 2
    repository = PostgresTradeRepository(
        lambda: connection,
        now=lambda: datetime(2026, 7, 24, 5, 0, tzinfo=UTC),
    )

    merchant = repository.get_merchants("1h")[0]
    overview = repository.get_overview("1h")

    assert merchant["attribution_coverage"] == Decimal("0.7333")
    assert merchant["honest_profit_micros"] is None
    assert overview["flows"]["honest_profit_micros"] is None


def test_coverage_counts_every_ledger_side_and_reports_no_evidence_as_none() -> None:
    assert _coverage() is None
    assert _coverage((0, 0)) is None
    assert _coverage((4, 0), (6, 0)) == Decimal("1.0000")
    # Unattributed sales alone are enough to pull coverage under the floor.
    assert _coverage((85, 85), (0, 0), (1, 0)) == Decimal("0.0116")
    # Unknown can never exceed total, so coverage stays in [0, 1].
    assert _coverage((2, 5)) == Decimal("0.0000")


def test_classify_earning_counterparties_rejects_out_of_range_bounds() -> None:
    repository = PostgresTradeRepository(lambda: DashboardConnection(usage_logs_readable=True))

    with pytest.raises(ValueError):
        repository.classify_earning_counterparties(settle_seconds=-1)
    with pytest.raises(ValueError):
        repository.classify_earning_counterparties(limit=0)


def test_classify_earning_counterparties_accepts_dict_rows() -> None:
    captured: dict[str, str] = {}

    class Cursor:
        def __enter__(self) -> Cursor:
            return self

        def __exit__(self, *_args: Any) -> bool:
            return False

        def execute(self, query: str, params: Any = None) -> None:
            assert params == (900, 20_000)
            captured["query"] = " ".join(query.split())

        def fetchall(self) -> list[dict[str, str]]:
            return [
                {"counterparty_class": "external"},
                {"counterparty_class": "own_boost"},
                {"counterparty_class": "external"},
            ]

    class Connection:
        def __init__(self) -> None:
            self.committed = False
            self.closed = False

        def cursor(self) -> Cursor:
            return Cursor()

        def commit(self) -> None:
            self.committed = True

        def rollback(self) -> None:
            raise AssertionError("rollback must not run")

        def close(self) -> None:
            self.closed = True

    connection = Connection()
    repository = PostgresTradeRepository(lambda: connection)

    result = repository.classify_earning_counterparties()

    assert result == {
        "external": 2,
        "own_boost": 1,
        "own_pin": 0,
        "classified": 3,
    }
    assert connection.committed is True
    assert connection.closed is True
    assert "booster.a6_token_ref_id IS NOT NULL AND NOT EXISTS" in captured["query"]
    assert "provider_binding.a6_token_id = provider.a6_token_ref_id" in captured["query"]
    assert "provider_binding.created_at <= provider.occurred_at" in captured["query"]


def test_counterparty_migration_labels_booster_funded_earnings() -> None:
    migration = Path("migrations/0021_earning_counterparty_class.sql").read_text(encoding="utf-8")

    assert "counterparty_class" in migration
    assert "a6_channel_booster_tokens" in migration
    assert "'own_boost'" in migration
    assert "'own_pin'" in migration
    # Suppression must be time-bounded, never a permanent disable.
    assert "suppressed_until" in migration
    assert "SET enabled = false" not in migration
