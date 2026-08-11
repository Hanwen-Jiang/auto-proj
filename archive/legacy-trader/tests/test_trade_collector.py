import threading
from dataclasses import replace
from pathlib import Path

import pytest

from a6api_auto.errors import A6AutoError, A6HttpError
from a6api_auto.models import SessionRecord
from a6api_auto.trade_collector import A6CollectionService, A6ReadCollector, CollectorWatermarks


class FakeCollectorClient:
    def __init__(self) -> None:
        self.cookies = None
        self.log_window = None

    def import_cookies(self, cookies):
        self.cookies = dict(cookies)

    def self_user(self, user_id):
        assert user_id == 42
        return {"id": 42, "quota": 700, "used_quota": 800, "request_count": 9}

    def supplier_me(self, user_id):
        assert user_id == 42
        return {
            "id": 84,
            "name": "NexusAPI",
            "description": "多模型 API 中转服务",
            "markup_percent": 20,
        }

    def supplier_channels(self, user_id):
        assert user_id == 42
        return [
            {
                "id": 5,
                "supplier_id": 84,
                "name": "external",
                "nickname": "provider",
                "channel_type": 1,
                "status": 1,
                "availability": 1,
                "base_url": "https://must-not-be-collected.invalid",
                "key": "must-not-be-collected",
            }
        ]

    def supplier_listings(self, user_id, *, page, size, status=None):
        assert user_id == 42
        assert page == 1
        assert size == 100
        assert status is None
        return {
            "items": [
                {
                    "id": 50,
                    "channel_id": 5,
                    "own_model": "upstream-a",
                    "platform_model": "model-a",
                    "charge_type": "per_token",
                    "input_price_micros": 500_000,
                    "output_price_micros": 3_000_000,
                    "cache_read_price_micros": 50_000,
                    "cache_write_price_micros": 625_000,
                    "status": 2,
                    "availability": 1,
                    "visibility": 1,
                    "display_name": "Model A",
                    "description": "fixture listing",
                    "api_key": "must-not-be-collected",
                }
            ],
            "total": 1,
        }

    def supplier_earnings_summary(self, user_id):
        assert user_id == 42
        return {
            "today_calls": 3,
            "today_earned_micros": 400,
            "month_calls": 8,
            "month_earned_micros": 900,
        }

    def supplier_settlements_summary(self, user_id):
        assert user_id == 42
        return {
            "pending_micros": 10,
            "settled_micros": 20,
            "total_income_micros": 30,
            "week_new_micros": 5,
            "abnormal_count": 0,
        }

    def supplier_withdraw_summary(self, user_id):
        assert user_id == 42
        return {
            "balance_available_micros": 600,
            "withdrawing_micros": 70,
            "total_earned_micros": 1_000,
            "total_paid_micros": 330,
        }

    def supplier_earnings(self, user_id, *, page, size):
        assert user_id == 42
        assert page == 1
        assert size == 100
        return {
            "items": [
                {
                    "id": 12,
                    "log_id": 112,
                    "request_id": "earning-request",
                    "channel_id": 5,
                    "channel_name": "external",
                    "platform_model": "model-a",
                    "own_model": "upstream-a",
                    "prompt_tokens": 100,
                    "completion_tokens": 20,
                    "cache_tokens": 80,
                    "quota_micros": 120,
                    "earned_micros": 100,
                    "user_market_quota": 120,
                    "supplier_earn_quota": 100,
                    "markup_percent": 20,
                    "result": "success",
                    "use_time": 2,
                    "created_at": 1_700_000_000,
                },
                {"id": 10},
            ],
            "total": 2,
        }

    def user_logs(
        self,
        user_id,
        *,
        page,
        page_size,
        start_timestamp,
        end_timestamp,
    ):
        assert user_id == 42
        assert page == 1
        assert page_size == 100
        self.log_window = (start_timestamp, end_timestamp)
        return {
            "items": [
                {
                    "id": 22,
                    "request_id": "usage-request",
                    "token_id": 6,
                    "token_name": "provider",
                    "channel": 7,
                    "channel_name": "outside",
                    "model_name": "model-a",
                    "route_type": "pin",
                    "quota": 90,
                    "prompt_tokens": 50,
                    "completion_tokens": 5,
                    "use_time": 1,
                    "type": 2,
                    "created_at": 1_700_000_001,
                },
                {"id": 20},
            ],
            "total": 2,
        }

    def marketplace_pins(self, user_id):
        assert user_id == 42
        return [
            {
                "token_id": 6,
                "channel_id": 7,
                "model_name": "model-a",
                "status": "active",
                "fallback_to_smart_routing": False,
            }
        ]

    def list_tokens(self, user_id):
        assert user_id == 42
        return [
            {
                "id": 6,
                "name": "provider",
                "status": 1,
                "unlimited_quota": True,
                "expired_time": -1,
                "key": "must-not-be-collected",
            }
        ]


def record() -> SessionRecord:
    return SessionRecord(
        schema_version=1,
        origin="https://a6api.example",
        email="private@example.test",
        username="private-user",
        password="private-password",
        user_id=42,
        cookies={"session": "private-cookie"},
        status="supplier_submitted",
        supplier_id=84,
    )


def test_read_collector_normalizes_snapshot_and_incremental_events() -> None:
    client = FakeCollectorClient()
    collector = A6ReadCollector(client, now=lambda: 1_700_003_600)

    batch = collector.collect(record(), CollectorWatermarks(earning_id=10, provider_log_id=20))

    assert client.cookies == {"session": "private-cookie"}
    assert batch.snapshot.merchant_user_id == 42
    assert batch.snapshot.quota_per_unit == 500_000
    assert batch.snapshot.withdrawable_micros == 600
    assert batch.snapshot.today_earned_micros == 400
    assert batch.snapshot.settlement_total_income_micros == 30
    assert batch.supplier_profile is not None
    assert batch.supplier_profile.supplier_id == 84
    assert batch.supplier_profile.name == "NexusAPI"
    assert batch.supplier_profile.description == "多模型 API 中转服务"
    assert batch.channels[0].a6_channel_id == 5
    assert batch.channels[0].supplier_id == 84
    assert batch.listings[0].a6_listing_id == 50
    assert batch.listings[0].input_price_micros == 500_000
    assert batch.listings[0].status == 2
    assert [event.earning_id for event in batch.earnings] == [12]
    assert batch.earnings[0].earned_micros == 100
    assert [event.log_id for event in batch.provider_usage] == [22]
    assert batch.provider_usage[0].channel_id == 7
    assert batch.pins[0].channel_id == 7
    assert batch.tokens[0].token_id == 6
    assert batch.tokens[0].token_name == "provider"
    assert "must-not-be-collected" not in str(batch)
    assert batch.next_watermarks == CollectorWatermarks(earning_id=12, provider_log_id=20)
    assert client.log_window == (1_700_000_000, 1_700_003_600)


def test_collection_summary_excludes_session_secrets() -> None:
    batch = A6ReadCollector(FakeCollectorClient(), now=lambda: 1_700_003_600).collect(record())

    rendered = str(batch.summary())

    assert "private@example.test" not in rendered
    assert "private-user" not in rendered
    assert "private-password" not in rendered
    assert "private-cookie" not in rendered
    assert batch.summary()["channels"] == 1
    assert batch.summary()["listings"] == 1


def test_supplier_listing_inventory_reads_all_pages_and_deduplicates_by_id() -> None:
    pages = []

    class PagedListingsClient:
        def supplier_listings(self, user_id, *, page, size, status=None):
            assert user_id == 42
            assert size == 2
            assert status is None
            pages.append(page)
            if page == 1:
                return {
                    "items": [
                        {"id": 2, "channel_id": 5, "platform_model": "model-b"},
                        {"id": 1, "channel_id": 5, "platform_model": "model-a"},
                    ],
                    "total": 3,
                }
            return {
                "items": [{"id": 3, "channel_id": 6, "platform_model": "model-c"}],
                "total": 3,
            }

    listings = A6ReadCollector(
        PagedListingsClient(),
        max_pages=3,
        page_size=2,
    )._collect_listings(42, "2026-07-24T04:00:00Z")

    assert pages == [1, 2]
    assert [listing.a6_listing_id for listing in listings] == [1, 2, 3]


def test_supplier_listing_inventory_rejects_truncated_pagination() -> None:
    class TruncatedListingsClient:
        def supplier_listings(self, _user_id, *, page, size, status=None):
            assert size == 2
            assert status is None
            return {
                "items": [
                    {"id": page * 10 + 1, "channel_id": 5},
                    {"id": page * 10 + 2, "channel_id": 5},
                ],
                "total": 5,
            }

    collector = A6ReadCollector(TruncatedListingsClient(), max_pages=2, page_size=2)

    with pytest.raises(A6AutoError, match="supplier_listings_inventory_incomplete"):
        collector._collect_listings(42, "2026-07-24T04:00:00Z")


def test_provider_usage_parses_reused_log_id_below_legacy_watermark() -> None:
    class ReusedIdClient(FakeCollectorClient):
        def user_logs(
            self,
            user_id,
            *,
            page,
            page_size,
            start_timestamp,
            end_timestamp,
        ):
            assert user_id == 42
            assert page == 1
            return {
                "items": [
                    {
                        "id": 1,
                        "request_id": "new-request-with-reused-id",
                        "token_id": 6,
                        "channel": 7,
                        "model_name": "model-a",
                        "quota": 91,
                        "prompt_tokens": 51,
                        "completion_tokens": 6,
                        "use_time": 1,
                        "type": 2,
                        "created_at": end_timestamp - 10,
                    },
                    {
                        "id": 86,
                        "request_id": "old-request",
                        "token_id": 6,
                        "channel": 7,
                        "model_name": "model-a",
                        "quota": 90,
                        "prompt_tokens": 50,
                        "completion_tokens": 5,
                        "use_time": 1,
                        "type": 2,
                        "created_at": end_timestamp - 20,
                    },
                ],
                "total": 2,
            }

    batch = A6ReadCollector(ReusedIdClient(), now=lambda: 1_700_003_600).collect(
        record(),
        CollectorWatermarks(provider_log_id=86),
    )

    assert [(event.log_id, event.request_id) for event in batch.provider_usage] == [
        (1, "new-request-with-reused-id"),
        (86, "old-request"),
    ]
    assert batch.next_watermarks.provider_log_id == 86


def test_provider_usage_full_page_continues_without_watermark_short_circuit() -> None:
    pages = []

    class PagedClient:
        def user_logs(
            self,
            _user_id,
            *,
            page,
            page_size,
            start_timestamp,
            end_timestamp,
        ):
            pages.append(page)
            assert page_size == 2
            if page == 1:
                return {
                    "items": [
                        {"id": 2, "request_id": "request-2", "created_at": end_timestamp - 2},
                        {"id": 1, "request_id": "request-1", "created_at": end_timestamp - 3},
                    ],
                    "total": 3,
                }
            return {
                "items": [
                    {"id": 1, "request_id": "new-reused-request", "created_at": end_timestamp - 1}
                ],
                "total": 3,
            }

    events = A6ReadCollector(PagedClient(), max_pages=3, page_size=2)._collect_provider_usage(
        42,
        86,
        end_timestamp=1_700_003_600,
    )

    assert pages == [1, 2]
    assert {event.request_id for event in events} == {
        "request-1",
        "request-2",
        "new-reused-request",
    }


def test_session_record_fixture_has_no_filesystem_dependency(tmp_path: Path) -> None:
    assert not list(tmp_path.iterdir())
    assert record().user_id == 42


def test_collection_service_records_structured_failure(monkeypatch) -> None:
    failures: list[tuple[str, str]] = []

    class Sink:
        def persist_collection(self, _batch):
            raise AssertionError("unexpected persist")

        def record_collection_failure(self, session_ref, error_class):
            failures.append((session_ref, error_class))

    monkeypatch.setattr(
        "a6api_auto.trade_collector.SessionStore.load",
        lambda _store: record(),
    )
    service = A6CollectionService(
        client_factory=lambda _record: (_ for _ in ()).throw(A6AutoError("collector_timeout")),
        watermark_loader=lambda _user_id: CollectorWatermarks(),
        sink=Sink(),
        sleep=lambda _seconds: None,
    )

    completed, collected_failures = service.collect_paths((Path("session-fixture.json"),))

    assert completed == 0
    assert [(item.session_ref, item.error_class) for item in collected_failures] == [
        ("session-fixture.json", "collector_timeout")
    ]
    assert failures == [("session-fixture.json", "collector_timeout")]


def test_collection_service_skips_pending_supplier_application(monkeypatch) -> None:
    class PendingSupplierClient:
        def import_cookies(self, _cookies):
            pass

        def self_user(self, _user_id):
            return {"id": 42}

        def supplier_me(self, _user_id):
            raise A6AutoError("supplier_self_failed")

        def supplier_requests(self, _user_id):
            return [{"id": 1267, "status": 0}]

    class Sink:
        def persist_collection(self, _batch):
            raise AssertionError("pending supplier must not be persisted")

        def record_collection_failure(self, *_args):
            raise AssertionError("pending supplier must not be recorded as failed")

    monkeypatch.setattr(
        "a6api_auto.trade_collector.SessionStore.load",
        lambda _store: record(),
    )
    service = A6CollectionService(
        client_factory=lambda _record: PendingSupplierClient(),
        watermark_loader=lambda _user_id: CollectorWatermarks(),
        sink=Sink(),
    )

    completed, failures = service.collect_paths((Path("session-pending.json"),))

    assert completed == 0
    assert failures == ()


def test_collection_service_keeps_approved_supplier_failure(monkeypatch) -> None:
    recorded = []

    class ApprovedSupplierClient:
        def import_cookies(self, _cookies):
            pass

        def self_user(self, _user_id):
            return {"id": 42}

        def supplier_me(self, _user_id):
            raise A6AutoError("supplier_self_failed")

        def supplier_requests(self, _user_id):
            return [{"id": 1209, "status": 1}]

    class Sink:
        def persist_collection(self, _batch):
            raise AssertionError("failed supplier must not be persisted")

        def record_collection_failure(self, session_ref, error_class):
            recorded.append((session_ref, error_class))

    monkeypatch.setattr(
        "a6api_auto.trade_collector.SessionStore.load",
        lambda _store: record(),
    )
    service = A6CollectionService(
        client_factory=lambda _record: ApprovedSupplierClient(),
        watermark_loader=lambda _user_id: CollectorWatermarks(),
        sink=Sink(),
    )

    completed, failures = service.collect_paths((Path("session-approved.json"),))

    assert completed == 0
    assert [(item.session_ref, item.error_class) for item in failures] == [
        ("session-approved.json", "supplier_self_failed")
    ]
    assert recorded == [("session-approved.json", "supplier_self_failed")]


def test_collection_service_retries_one_transport_failure(monkeypatch) -> None:
    attempts = 0
    persisted = []

    class Sink:
        def persist_collection(self, batch):
            persisted.append(batch)

        def record_collection_failure(self, *_args):
            raise AssertionError("recovered transport failure must not be recorded")

    class Collector:
        def __init__(self, _client, *, now):
            self.now = now

        def collect(self, _record, _watermarks):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise A6AutoError("a6_transport_failed")
            return "batch"

    monkeypatch.setattr(
        "a6api_auto.trade_collector.SessionStore.load",
        lambda _store: record(),
    )
    monkeypatch.setattr("a6api_auto.trade_collector.A6ReadCollector", Collector)
    service = A6CollectionService(
        client_factory=lambda _record: object(),
        watermark_loader=lambda _user_id: CollectorWatermarks(),
        sink=Sink(),
        sleep=lambda _seconds: None,
    )

    completed, failures = service.collect_paths((Path("session-fixture.json"),))

    assert completed == 1
    assert failures == ()
    assert attempts == 2
    assert persisted == ["batch"]


@pytest.mark.parametrize("status", [429, 503])
def test_collection_service_retries_transient_get_without_duplicate_persist(
    monkeypatch,
    status,
) -> None:
    attempts = 0
    persisted = []
    sleeps = []

    class Sink:
        def persist_collection(self, batch):
            persisted.append(batch)

        def record_collection_failure(self, *_args):
            raise AssertionError("recovered transient failure must not be recorded")

    class Collector:
        def __init__(self, _client, *, now):
            pass

        def collect(self, _record, _watermarks):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise A6HttpError(
                    "supplier_withdraw_summary_failed",
                    status_code=status,
                )
            return "batch"

    monkeypatch.setattr(
        "a6api_auto.trade_collector.SessionStore.load",
        lambda _store: record(),
    )
    monkeypatch.setattr("a6api_auto.trade_collector.A6ReadCollector", Collector)
    service = A6CollectionService(
        client_factory=lambda _record: object(),
        watermark_loader=lambda _user_id: CollectorWatermarks(),
        sink=Sink(),
        sleep=sleeps.append,
    )

    completed, failures = service.collect_paths((Path("session-fixture.json"),))

    assert completed == 1
    assert failures == ()
    assert attempts == 2
    assert sleeps == [1]
    assert persisted == ["batch"]


@pytest.mark.parametrize(
    "failure",
    [
        A6HttpError("supplier_listings_failed", status_code=403),
        A6AutoError("supplier_self_failed"),
        A6AutoError("supplier_listings_failed"),
    ],
)
def test_collection_service_does_not_retry_nontransient_read_failure(
    monkeypatch,
    failure,
) -> None:
    attempts = 0
    recorded = []

    class Sink:
        def persist_collection(self, _batch):
            raise AssertionError("failed collection must not be persisted")

        def record_collection_failure(self, session_ref, error_class):
            recorded.append((session_ref, error_class))

    class Collector:
        def __init__(self, _client, *, now):
            pass

        def collect(self, _record, _watermarks):
            nonlocal attempts
            attempts += 1
            raise failure

    monkeypatch.setattr(
        "a6api_auto.trade_collector.SessionStore.load",
        lambda _store: record(),
    )
    monkeypatch.setattr("a6api_auto.trade_collector.A6ReadCollector", Collector)
    service = A6CollectionService(
        client_factory=lambda _record: object(),
        watermark_loader=lambda _user_id: CollectorWatermarks(),
        sink=Sink(),
        sleep=lambda _seconds: (_ for _ in ()).throw(
            AssertionError("nontransient failure must not sleep")
        ),
    )

    completed, failures = service.collect_paths((Path("session-fixture.json"),))

    assert completed == 0
    assert [item.error_class for item in failures] == [str(failure)]
    assert attempts == 1
    assert recorded == [("session-fixture.json", str(failure))]


def test_collection_service_refreshes_one_expired_session_without_spending_transport_retry(
    monkeypatch,
) -> None:
    attempts = 0
    refreshed = []
    persisted = []

    class Sink:
        def persist_collection(self, batch):
            persisted.append(batch)

        def record_collection_failure(self, *_args):
            raise AssertionError("recovered session must not be recorded as failed")

    class Collector:
        def __init__(self, client, *, now):
            self.client = client

        def collect(self, item, _watermarks):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise A6AutoError("collector_session_invalid")
            assert item.cookies == {"session": "fresh-cookie"}
            return "batch"

    stored = {"record": record()}
    monkeypatch.setattr(
        "a6api_auto.trade_collector.SessionStore.load",
        lambda _store: stored["record"],
    )
    monkeypatch.setattr("a6api_auto.trade_collector.A6ReadCollector", Collector)

    def refresh(store, current, client):
        refreshed.append((store.path.name, current.user_id, client))
        stored["record"] = replace(current, cookies={"session": "fresh-cookie"})

    service = A6CollectionService(
        client_factory=lambda item: item.cookies.get("session", "missing"),
        watermark_loader=lambda _user_id: CollectorWatermarks(),
        sink=Sink(),
        transport_retries=0,
        session_refresher=refresh,
    )

    completed, failures = service.collect_paths((Path("session-expired.json"),))

    assert completed == 1
    assert failures == ()
    assert attempts == 2
    assert refreshed == [("session-expired.json", 42, "private-cookie")]
    assert persisted == ["batch"]


def test_collection_service_rejects_refreshed_session_identity_change(monkeypatch) -> None:
    recorded = []

    class Sink:
        def persist_collection(self, _batch):
            raise AssertionError("invalid refreshed session must not be persisted")

        def record_collection_failure(self, session_ref, error_class):
            recorded.append((session_ref, error_class))

    class Collector:
        def __init__(self, _client, *, now):
            pass

        def collect(self, _record, _watermarks):
            raise A6AutoError("collector_session_invalid")

    stored = {"record": record()}
    monkeypatch.setattr(
        "a6api_auto.trade_collector.SessionStore.load",
        lambda _store: stored["record"],
    )
    monkeypatch.setattr("a6api_auto.trade_collector.A6ReadCollector", Collector)

    def refresh(_store, current, _client):
        stored["record"] = replace(current, user_id=99)

    service = A6CollectionService(
        client_factory=lambda _record: object(),
        watermark_loader=lambda _user_id: CollectorWatermarks(),
        sink=Sink(),
        session_refresher=refresh,
    )

    completed, failures = service.collect_paths((Path("session-expired.json"),))

    assert completed == 0
    assert [(item.session_ref, item.error_class) for item in failures] == [
        ("session-expired.json", "collector_session_refresh_invalid")
    ]
    assert recorded == [
        ("session-expired.json", "collector_session_refresh_invalid")
    ]


def test_collection_service_collects_with_bounded_workers_and_stable_order(monkeypatch) -> None:
    active = 0
    max_active = 0
    barrier = threading.Barrier(2)
    persisted = []

    class Sink:
        def persist_collection(self, batch):
            persisted.append(batch)

        def record_collection_failure(self, *_args):
            raise AssertionError("unexpected collection failure")

    class Collector:
        def __init__(self, client, *, now):
            self.client = client

        def collect(self, _record, _watermarks):
            nonlocal active, max_active
            with lock:
                active += 1
                max_active = max(max_active, active)
            barrier.wait(timeout=2)
            with lock:
                active -= 1
            return self.client

    lock = threading.Lock()
    records = {
        "one.json": replace(record(), user_id=41),
        "two.json": replace(record(), user_id=42),
    }
    monkeypatch.setattr(
        "a6api_auto.trade_collector.SessionStore.load",
        lambda store: records[store.path.name],
    )
    monkeypatch.setattr("a6api_auto.trade_collector.A6ReadCollector", Collector)
    service = A6CollectionService(
        client_factory=lambda item: int(item.user_id),
        watermark_loader=lambda _user_id: CollectorWatermarks(),
        sink=Sink(),
        max_workers=2,
    )

    completed, failures = service.collect_paths((Path("one.json"), Path("two.json")))

    assert completed == 2
    assert failures == ()
    assert max_active == 2
    assert sorted(persisted) == [41, 42]


@pytest.mark.parametrize(
    ("failure_stage", "expected_error"),
    [
        ("persist", "collector_persist_failed"),
        ("factory", "collector_internal_error"),
    ],
)
def test_collection_service_contains_worker_failures_and_continues(
    monkeypatch,
    failure_stage,
    expected_error,
) -> None:
    records = {
        "one.json": replace(record(), user_id=41),
        "two.json": replace(record(), user_id=42),
    }
    persisted = []
    recorded_failures = []

    class Sink:
        def persist_collection(self, batch):
            if failure_stage == "persist" and batch == 41:
                raise RuntimeError("database fixture detail")
            persisted.append(batch)

        def record_collection_failure(self, session_ref, error_class):
            recorded_failures.append((session_ref, error_class))

    class Collector:
        def __init__(self, client, *, now):
            self.client = client

        def collect(self, _record, _watermarks):
            return self.client

    def client_factory(item):
        if failure_stage == "factory" and item.user_id == 41:
            raise RuntimeError("local fixture detail")
        return int(item.user_id)

    monkeypatch.setattr(
        "a6api_auto.trade_collector.SessionStore.load",
        lambda store: records[store.path.name],
    )
    monkeypatch.setattr("a6api_auto.trade_collector.A6ReadCollector", Collector)
    service = A6CollectionService(
        client_factory=client_factory,
        watermark_loader=lambda _user_id: CollectorWatermarks(),
        sink=Sink(),
        max_workers=2,
    )

    completed, failures = service.collect_paths((Path("one.json"), Path("two.json")))

    assert completed == 1
    assert [(item.session_ref, item.error_class) for item in failures] == [
        ("one.json", expected_error)
    ]
    assert recorded_failures == [("one.json", expected_error)]
    assert persisted == [42]


@pytest.mark.parametrize("workers", [0, 17, True])
def test_collection_service_rejects_invalid_worker_count(workers) -> None:
    with pytest.raises(ValueError, match="collector max workers invalid"):
        A6CollectionService(
            client_factory=lambda _record: object(),
            watermark_loader=lambda _user_id: CollectorWatermarks(),
            sink=object(),
            max_workers=workers,
        )
