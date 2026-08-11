from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from a6api_auto.trade_market_history_retention import (
    MarketHistoryArchiveFile,
    MarketHistoryExport,
    MarketHistoryObject,
    MarketHistoryRetentionConfig,
    MarketHistoryRetentionError,
    PostgresMarketHistoryRetention,
)
from scripts.cleanup_a6_market_history import parser, run

NOW = datetime(2026, 8, 2, 3, 0, tzinfo=timezone.utc)


def candidate(scan_run_id: int, *, hours_old: int = 96) -> dict[str, Any]:
    return {
        "scan_run_id": scan_run_id,
        "started_at": NOW - timedelta(hours=hours_old),
        "candidate_count": 291,
        "payload_bytes": 42_000,
    }


class FakeCursor:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection
        self.rows: list[dict[str, Any]] = []
        self.description = None
        self.rowcount = -1

    def __enter__(self) -> FakeCursor:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, query: str, params: Any = None) -> None:
        normalized = " ".join(query.split())
        self.connection.database.queries.append((normalized, params))
        self.rowcount = -1
        if (
            normalized.startswith("SET TRANSACTION")
            or normalized.startswith("SET LOCAL")
            or normalized.startswith("SELECT pg_advisory_xact_lock")
        ):
            self.rows = []
            return
        if normalized.startswith("SELECT scan.id AS scan_run_id"):
            self.rows = list(self.connection.batch)
            return
        if normalized.startswith("SELECT count(*)::bigint AS quote_count"):
            self.rows = [
                {
                    "quote_count": (
                        0 if self.connection.quotes_deleted else self.connection.quote_count
                    )
                }
            ]
            return
        if normalized.startswith("WITH deleted_quotes AS"):
            self.connection.quotes_deleted = True
            self.rows = [{"deleted_quote_count": self.connection.quote_count}]
            return
        if normalized.startswith("DELETE FROM a6_trade.a6_market_scan_runs"):
            ids = list(params[0])
            if self.connection.database.delete_mismatch and ids:
                ids = ids[:-1]
            self.connection.deleted = True
            self.rows = [{"id": item} for item in ids]
            self.rowcount = len(ids)
            return
        if normalized.startswith("INSERT INTO a6_trade.a6_market_history_archives"):
            self.connection.database.receipts += 1
            self.rows = []
            return
        raise AssertionError(f"unexpected query: {normalized}")

    def fetchall(self) -> list[dict[str, Any]]:
        rows = self.rows
        self.rows = []
        return rows

    def fetchone(self) -> dict[str, Any] | None:
        if not self.rows:
            return None
        return self.rows.pop(0)


class FakeConnection:
    def __init__(
        self,
        database: FakeDatabase,
        batch: list[dict[str, Any]],
        quote_count: int,
    ) -> None:
        self.database = database
        self.batch = batch
        self.quote_count = quote_count
        self.deleted = False
        self.quotes_deleted = False

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)

    def commit(self) -> None:
        self.database.commits += 1

    def rollback(self) -> None:
        self.database.rollbacks += 1

    def close(self) -> None:
        self.database.closes += 1


class FakeDatabase:
    def __init__(
        self,
        batches: list[list[dict[str, Any]]],
        quote_counts: list[int],
        *,
        delete_mismatch: bool = False,
    ) -> None:
        self.batches = batches
        self.quote_counts = quote_counts
        self.delete_mismatch = delete_mismatch
        self.connections = 0
        self.commits = 0
        self.rollbacks = 0
        self.closes = 0
        self.receipts = 0
        self.queries: list[tuple[str, Any]] = []

    def connection(self) -> FakeConnection:
        index = self.connections
        self.connections += 1
        return FakeConnection(
            self,
            self.batches[index] if index < len(self.batches) else [],
            self.quote_counts[index] if index < len(self.quote_counts) else 0,
        )


class FakeExporter:
    def export(
        self,
        _connection: Any,
        scan_run_ids: list[int],
        *,
        quote_count: int,
        directory: Path,
    ) -> MarketHistoryExport:
        return MarketHistoryExport(
            scan_runs=self._file(
                directory / "a6_market_scan_runs.csv.gz",
                b"scan-archive",
                len(scan_run_ids),
            ),
            quotes=self._file(
                directory / "a6_market_quotes.csv.gz",
                b"quote-archive",
                quote_count,
            ),
        )

    @staticmethod
    def _file(path: Path, payload: bytes, row_count: int) -> MarketHistoryArchiveFile:
        path.write_bytes(payload)
        return MarketHistoryArchiveFile(
            path=path,
            row_count=row_count,
            sha256=hashlib.sha256(payload).hexdigest(),
            size_bytes=len(payload),
        )


class FakeObjectStore:
    def __init__(self, *, corrupt_head: bool = False) -> None:
        self.corrupt_head = corrupt_head
        self.objects: dict[str, tuple[bytes, str]] = {}

    @property
    def identity(self) -> dict[str, str]:
        return {
            "backend": "s3",
            "bucket": "test-archive",
            "endpoint_sha256": "e" * 64,
        }

    def put_file(
        self,
        key: str,
        path: Path,
        *,
        sha256: str,
        content_type: str,
        content_encoding: str | None = None,
    ) -> MarketHistoryObject:
        del content_type, content_encoding
        payload = path.read_bytes()
        self.objects[key] = (payload, sha256)
        return self.head(key)

    def head(self, key: str) -> MarketHistoryObject:
        payload, sha256 = self.objects[key]
        return MarketHistoryObject(
            key=key,
            size_bytes=len(payload),
            sha256="0" * 64 if self.corrupt_head else sha256,
        )


def archiver(
    database: FakeDatabase,
    store: FakeObjectStore,
    tmp_path: Path,
) -> PostgresMarketHistoryRetention:
    return PostgresMarketHistoryRetention(
        database.connection,
        archive_store=store,
        exporter=FakeExporter(),
        staging_directory=tmp_path,
    )


def test_dry_run_is_read_only_and_reports_one_batch() -> None:
    database = FakeDatabase([[candidate(11), candidate(12)]], [73])
    result = PostgresMarketHistoryRetention(database.connection).cleanup(
        MarketHistoryRetentionConfig(retention_hours=72, batch_size=2),
        now=NOW,
    )

    assert result.applied is False
    assert result.eligible_quote_count == 73
    assert result.deleted_scan_runs == 0
    assert result.has_more is True
    assert database.commits == 1
    assert database.rollbacks == 0
    queries = [query for query, _ in database.queries]
    assert any(query.startswith("SET TRANSACTION") for query in queries)
    assert not any(query.startswith("DELETE FROM") for query in queries)


def test_apply_archives_verifies_receipt_then_deletes(tmp_path: Path) -> None:
    database = FakeDatabase([[candidate(21), candidate(22)]], [64])
    store = FakeObjectStore()
    result = archiver(database, store, tmp_path).cleanup(
        MarketHistoryRetentionConfig(retention_hours=72, batch_size=5),
        apply=True,
        now=NOW,
    )

    assert result.applied is True
    assert result.deleted_scan_runs == 2
    assert result.deleted_quotes == 64
    assert result.deleted_payload_bytes == 84_000
    assert result.archived_bytes > 0
    assert result.archive_prefix is not None
    assert database.commits == 1
    assert database.receipts == 1
    manifest_key = f"{result.archive_prefix}/manifest.json"
    manifest = json.loads(store.objects[manifest_key][0])
    assert manifest["scan_run_ids"] == [21, 22]
    assert manifest["scan_run_count"] == 2
    assert manifest["quote_count"] == 64
    queries = [query for query, _ in database.queries]
    candidate_query = next(
        query for query in queries if query.startswith("SELECT scan.id AS scan_run_id")
    )
    assert "scan.status <> 'running'" in candidate_query
    assert "FOR UPDATE OF scan SKIP LOCKED" in candidate_query
    assert any(query.startswith("WITH deleted_quotes AS") for query in queries)
    assert any(query.startswith("INSERT INTO") for query in queries)


def test_apply_requires_external_archive_before_opening_database() -> None:
    database = FakeDatabase([[candidate(25)]], [10])

    with pytest.raises(MarketHistoryRetentionError, match="archive_store_required"):
        PostgresMarketHistoryRetention(database.connection).cleanup(
            MarketHistoryRetentionConfig(),
            apply=True,
            now=NOW,
        )

    assert database.connections == 0


def test_remote_hash_mismatch_rolls_back_before_any_delete(tmp_path: Path) -> None:
    database = FakeDatabase([[candidate(27)]], [12])
    store = FakeObjectStore(corrupt_head=True)

    with pytest.raises(MarketHistoryRetentionError, match="archive_sha256_mismatch"):
        archiver(database, store, tmp_path).cleanup(
            MarketHistoryRetentionConfig(),
            apply=True,
            now=NOW,
        )

    assert database.commits == 0
    assert database.rollbacks == 1
    assert database.receipts == 0
    assert not any(query.startswith("DELETE FROM") for query, _ in database.queries)


def test_legacy_scan_without_payload_is_counted_as_zero_bytes() -> None:
    legacy = candidate(23)
    legacy["payload_bytes"] = None
    database = FakeDatabase([[legacy]], [12])

    result = PostgresMarketHistoryRetention(database.connection).cleanup(
        MarketHistoryRetentionConfig(),
        now=NOW,
    )

    assert result.candidates[0].payload_bytes == 0
    assert result.summary()["eligible_payload_bytes"] == 0


def test_apply_rolls_back_if_the_locked_parent_set_changes(tmp_path: Path) -> None:
    database = FakeDatabase(
        [[candidate(31), candidate(32)]],
        [48],
        delete_mismatch=True,
    )

    with pytest.raises(
        MarketHistoryRetentionError,
        match="market_history_retention_scan_delete_mismatch",
    ):
        archiver(database, FakeObjectStore(), tmp_path).cleanup(
            MarketHistoryRetentionConfig(),
            apply=True,
            now=NOW,
        )

    assert database.commits == 0
    assert database.rollbacks == 1
    assert database.receipts == 0


def test_cleanup_rejects_naive_clock_before_opening_database() -> None:
    database = FakeDatabase([], [])

    with pytest.raises(MarketHistoryRetentionError, match="datetime_invalid"):
        PostgresMarketHistoryRetention(database.connection).cleanup(
            MarketHistoryRetentionConfig(),
            now=datetime(2026, 8, 2, 3, 0),
        )

    assert database.connections == 0


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"retention_hours": 0}, "hours_invalid"),
        ({"retention_hours": 24 * 367}, "hours_invalid"),
        ({"batch_size": 0}, "batch_size_invalid"),
        ({"batch_size": 10_001}, "batch_size_invalid"),
    ],
)
def test_config_rejects_unbounded_values(
    kwargs: dict[str, Any],
    message: str,
) -> None:
    with pytest.raises(MarketHistoryRetentionError, match=message):
        MarketHistoryRetentionConfig(**kwargs)


def test_script_commits_multiple_bounded_batches(tmp_path: Path) -> None:
    database = FakeDatabase(
        [
            [candidate(41), candidate(42)],
            [candidate(43)],
        ],
        [60, 29],
    )
    store = FakeObjectStore()

    result = run(
        apply=True,
        retention_hours=72,
        batch_size=2,
        max_batches=4,
        connection_factory=database.connection,
        now=NOW,
        archive_store=store,
        exporter=FakeExporter(),
        staging_directory=str(tmp_path),
    )

    assert result["batches_run"] == 2
    assert result["deleted_scan_runs"] == 3
    assert result["deleted_quotes"] == 89
    assert result["archived_bytes"] > 0
    assert len(result["archive_prefixes"]) == 2
    assert result["has_more"] is False
    assert database.commits == 2
    assert database.receipts == 2


def test_script_defaults_to_a_bounded_dry_run() -> None:
    args = parser().parse_args([])
    assert args.apply is False
    assert args.retention_hours == 72
    assert args.batch_size == 500
    assert args.max_batches == 4


def test_migration_and_systemd_units_are_packaged_and_archive_gated() -> None:
    migration = Path("migrations/0055_market_history_retention.sql").read_text()
    packaged = Path("src/a6api_auto/data/migrations/0055_market_history_retention.sql").read_text()
    service = Path("deploy/systemd/a6-trader-market-history-cleanup.service").read_text()
    timer = Path("deploy/systemd/a6-trader-market-history-cleanup.timer").read_text()
    receipt_migration = Path(
        "migrations/0058_market_history_archive_receipts.sql"
    ).read_text()
    packaged_receipt_migration = Path(
        "src/a6api_auto/data/migrations/0058_market_history_archive_receipts.sql"
    ).read_text()

    assert packaged == migration
    assert packaged_receipt_migration == receipt_migration
    assert "a6_market_scan_runs_retention_idx" in migration
    assert "a6_market_history_archives" in receipt_migration
    assert "archive_prefix text NOT NULL UNIQUE" in receipt_migration
    assert "--retention-hours 72" in service
    assert "--batch-size 500" in service
    assert "--max-batches 4" in service
    assert "EnvironmentFile=/etc/a6-market-archive.env" in service
    assert "ReadWritePaths=/www/a6-market-archive-staging" in service
    assert "--apply" in service
    assert "OnCalendar=hourly" in timer
