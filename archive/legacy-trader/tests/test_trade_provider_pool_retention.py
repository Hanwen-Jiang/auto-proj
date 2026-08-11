from __future__ import annotations

import copy
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from a6api_auto.trade_provider_pool_retention import (
    PostgresProviderGenerationRetention,
    ProviderGenerationRetentionConfig,
    ProviderGenerationRetentionError,
)
from scripts.cleanup_a6_provider_generations import parser, run

NOW = datetime(2026, 7, 29, 2, 0, tzinfo=timezone.utc)
OLD = NOW - timedelta(days=181)
RECENT = NOW - timedelta(days=179)


def pair(
    routing_generation: int,
    *,
    routing_state: str = "superseded",
    cost_state: str = "superseded",
    routing_updated_at: datetime = OLD,
    cost_updated_at: datetime = OLD,
    slot_valid_to: datetime | None = OLD,
    shared_cost_generation: bool = False,
    shared_cost_slot: bool = False,
) -> dict[str, Any]:
    return {
        "model": "gpt-5.6-sol",
        "routing_generation_id": 200 + routing_generation,
        "routing_generation": routing_generation,
        "cost_generation_id": 100 + routing_generation,
        "cost_generation": routing_generation,
        "routing_state": routing_state,
        "cost_state": cost_state,
        "routing_updated_at": routing_updated_at,
        "cost_updated_at": cost_updated_at,
        "slots": [
            {
                "id": 300 + routing_generation,
                "generation_id": 200 + routing_generation,
                "cost_generation_id": 100 + routing_generation,
                "slot_no": 1,
                "valid_to": slot_valid_to,
            }
        ],
        "shared_cost_generation": shared_cost_generation,
        "shared_cost_slot": shared_cost_slot,
        "routing_deleted": False,
        "cost_deleted": False,
    }


@dataclass
class FakeDatabase:
    pairs: list[dict[str, Any]]
    archives: list[dict[str, Any]] = field(default_factory=list)
    connections: list[FakeConnection] = field(default_factory=list)
    fail_cost_delete: bool = False

    def connection(self) -> FakeConnection:
        connection = FakeConnection(self)
        self.connections.append(connection)
        return connection

    def snapshot(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        return copy.deepcopy((self.pairs, self.archives))

    def restore(
        self,
        snapshot: tuple[list[dict[str, Any]], list[dict[str, Any]]],
    ) -> None:
        self.pairs, self.archives = copy.deepcopy(snapshot)


class FakeConnection:
    def __init__(self, database: FakeDatabase) -> None:
        self.database = database
        self.calls: list[tuple[str, Any]] = []
        self.commits = 0
        self.rollbacks = 0
        self.closed = False
        self.snapshot = database.snapshot()

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1
        self.database.restore(self.snapshot)

    def close(self) -> None:
        self.closed = True


class FakeCursor:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection
        self.result: list[dict[str, Any]] = []
        self.rowcount = -1
        self.description = None

    def __enter__(self) -> FakeCursor:
        return self

    def __exit__(self, *_args: object) -> bool:
        return False

    def execute(self, query: str, params: Any = None) -> FakeCursor:
        normalized = " ".join(query.split())
        self.connection.calls.append((normalized, params))
        self.result = []
        self.rowcount = -1
        database = self.connection.database
        if normalized.startswith("SET TRANSACTION ISOLATION LEVEL"):
            return self
        if normalized.startswith("SELECT pg_advisory_xact_lock"):
            return self
        if normalized.startswith("SELECT generation.id AS routing_generation_id"):
            model, routing_cutoff, cost_cutoff, limit = params
            eligible = [
                item
                for item in database.pairs
                if not item["routing_deleted"]
                and not item["cost_deleted"]
                and item["model"] == model
                and item["routing_state"] == "superseded"
                and item["cost_state"] == "superseded"
                and item["routing_updated_at"] < routing_cutoff
                and item["cost_updated_at"] < cost_cutoff
                and all(slot["valid_to"] is not None for slot in item["slots"])
                and not item["shared_cost_generation"]
                and not item["shared_cost_slot"]
            ]
            eligible.sort(
                key=lambda item: (
                    max(item["routing_updated_at"], item["cost_updated_at"]),
                    item["routing_generation"],
                    item["cost_generation"],
                )
            )
            self.result = [candidate_row(item) for item in eligible[:limit]]
            self.rowcount = len(self.result)
            return self
        if normalized.startswith(
            "INSERT INTO a6_trade.provider_generation_archives"
        ):
            (
                cutoff,
                routing_id,
                cost_id,
                model,
                routing_generation,
                cost_generation,
                routing_cutoff,
                cost_cutoff,
            ) = params
            item = next(
                (
                    current
                    for current in database.pairs
                    if current["routing_generation_id"] == routing_id
                    and current["cost_generation_id"] == cost_id
                    and current["model"] == model
                    and current["routing_generation"] == routing_generation
                    and current["cost_generation"] == cost_generation
                    and current["routing_state"] == "superseded"
                    and current["cost_state"] == "superseded"
                    and current["routing_updated_at"] < routing_cutoff
                    and current["cost_updated_at"] < cost_cutoff
                    and all(slot["valid_to"] is not None for slot in current["slots"])
                    and not current["shared_cost_generation"]
                    and not current["shared_cost_slot"]
                ),
                None,
            )
            if item is not None:
                archive = {
                    "routing_generation_id": routing_id,
                    "cost_generation_id": cost_id,
                    "slot_snapshot": copy.deepcopy(item["slots"]),
                    "slot_count": len(item["slots"]),
                    "retention_cutoff": cutoff,
                }
                database.archives.append(archive)
                self.result = [{"slot_count": len(item["slots"])}]
                self.rowcount = 1
            else:
                self.rowcount = 0
            return self
        if normalized.startswith("DELETE FROM a6_trade.provider_pool_slots"):
            item = find_pair(database, routing_id=params[0])
            self.rowcount = len(item["slots"])
            item["slots"] = []
            return self
        if normalized.startswith("DELETE FROM a6_trade.provider_pool_generations"):
            routing_id, model, routing_generation, cost_id, cost_generation = params
            item = find_pair(database, routing_id=routing_id)
            matches = (
                item["model"] == model
                and item["routing_generation"] == routing_generation
                and item["cost_generation_id"] == cost_id
                and item["cost_generation"] == cost_generation
                and item["routing_state"] == "superseded"
                and not item["routing_deleted"]
            )
            self.rowcount = int(matches)
            if matches:
                item["routing_deleted"] = True
            return self
        if normalized.startswith("DELETE FROM a6_trade.provider_cost_generations"):
            cost_id, model, cost_generation = params
            item = find_pair(database, cost_id=cost_id)
            matches = (
                not database.fail_cost_delete
                and item["model"] == model
                and item["cost_generation"] == cost_generation
                and item["cost_state"] == "superseded"
                and item["routing_deleted"]
                and not item["slots"]
                and not item["shared_cost_generation"]
                and not item["shared_cost_slot"]
                and not item["cost_deleted"]
            )
            self.rowcount = int(matches)
            if matches:
                item["cost_deleted"] = True
            return self
        raise AssertionError(f"unexpected SQL: {normalized}")

    def fetchall(self) -> list[dict[str, Any]]:
        return self.result

    def fetchone(self) -> dict[str, Any] | None:
        return self.result[0] if self.result else None


def find_pair(
    database: FakeDatabase,
    *,
    routing_id: int | None = None,
    cost_id: int | None = None,
) -> dict[str, Any]:
    return next(
        item
        for item in database.pairs
        if (routing_id is None or item["routing_generation_id"] == routing_id)
        and (cost_id is None or item["cost_generation_id"] == cost_id)
    )


def candidate_row(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "model": item["model"],
        "routing_generation_id": item["routing_generation_id"],
        "routing_generation": item["routing_generation"],
        "cost_generation_id": item["cost_generation_id"],
        "cost_generation": item["cost_generation"],
        "routing_updated_at": item["routing_updated_at"],
        "cost_updated_at": item["cost_updated_at"],
        "slot_count": len(item["slots"]),
    }


def cleanup(
    database: FakeDatabase,
    *,
    apply: bool,
    batch_size: int = 100,
):
    return PostgresProviderGenerationRetention(database.connection).cleanup(
        ProviderGenerationRetentionConfig(
            model="gpt-5.6-sol",
            retention_days=180,
            batch_size=batch_size,
        ),
        apply=apply,
        now=NOW,
    )


def test_dry_run_only_reports_expired_unreferenced_superseded_pairs() -> None:
    database = FakeDatabase(
        [
            pair(1),
            pair(2, routing_updated_at=RECENT),
            pair(3, routing_state="active", cost_state="active"),
            pair(4, routing_state="planned", cost_state="planned"),
            pair(5, routing_state="applying", cost_state="confirmed"),
            pair(6, cost_state="active"),
            pair(7, slot_valid_to=None),
            pair(8, shared_cost_generation=True),
            pair(9, shared_cost_slot=True),
        ]
    )
    before = database.snapshot()

    result = cleanup(database, apply=False)

    assert [item.routing_generation for item in result.candidates] == [1]
    assert result.archived_pairs == 0
    assert database.snapshot() == before
    connection = database.connections[0]
    assert connection.commits == 1
    assert connection.rollbacks == 0
    assert connection.closed is True
    statements = [query for query, _ in connection.calls]
    assert any("REPEATABLE READ READ ONLY" in query for query in statements)
    assert not any("INSERT INTO" in query or "DELETE FROM" in query for query in statements)


def test_apply_archives_snapshot_and_deletes_pair_in_dependency_order() -> None:
    database = FakeDatabase([pair(11)])

    result = cleanup(database, apply=True)

    assert result.archived_pairs == 1
    assert result.deleted_slots == 1
    assert result.deleted_routing_generations == 1
    assert result.deleted_cost_generations == 1
    assert database.archives[0]["slot_count"] == 1
    assert database.pairs[0]["slots"] == []
    assert database.pairs[0]["routing_deleted"] is True
    assert database.pairs[0]["cost_deleted"] is True
    statements = [query for query, _ in database.connections[0].calls]
    assert statements[0].startswith("SELECT pg_advisory_xact_lock")
    assert [
        next(index for index, query in enumerate(statements) if marker in query)
        for marker in (
            "INSERT INTO a6_trade.provider_generation_archives",
            "DELETE FROM a6_trade.provider_pool_slots",
            "DELETE FROM a6_trade.provider_pool_generations",
            "DELETE FROM a6_trade.provider_cost_generations",
        )
    ] == sorted(
        next(index for index, query in enumerate(statements) if marker in query)
        for marker in (
            "INSERT INTO a6_trade.provider_generation_archives",
            "DELETE FROM a6_trade.provider_pool_slots",
            "DELETE FROM a6_trade.provider_pool_generations",
            "DELETE FROM a6_trade.provider_cost_generations",
        )
    )


def test_cost_delete_mismatch_rolls_back_archive_slots_and_routing() -> None:
    database = FakeDatabase([pair(21)], fail_cost_delete=True)
    before = database.snapshot()

    with pytest.raises(
        ProviderGenerationRetentionError,
        match="provider_generation_archive_cost_delete_mismatch",
    ):
        cleanup(database, apply=True)

    assert database.snapshot() == before
    connection = database.connections[0]
    assert connection.commits == 0
    assert connection.rollbacks == 1
    assert connection.closed is True


def test_batch_size_limits_locked_and_archived_pairs() -> None:
    database = FakeDatabase([pair(31), pair(32), pair(33)])

    result = cleanup(database, apply=True, batch_size=2)

    assert [item.routing_generation for item in result.candidates] == [31, 32]
    assert result.archived_pairs == 2
    assert database.pairs[2]["routing_deleted"] is False
    candidate_query = next(
        query
        for query, _ in database.connections[0].calls
        if query.startswith("SELECT generation.id AS routing_generation_id")
    )
    assert "FOR UPDATE OF generation, cost SKIP LOCKED" in candidate_query


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"model": ""}, "model_invalid"),
        ({"model": "m", "retention_days": 0}, "days_invalid"),
        ({"model": "m", "batch_size": 0}, "batch_size_invalid"),
        ({"model": "m", "batch_size": 1001}, "batch_size_invalid"),
    ],
)
def test_retention_config_rejects_unbounded_inputs(
    kwargs: dict[str, Any],
    message: str,
) -> None:
    with pytest.raises(ProviderGenerationRetentionError, match=message):
        ProviderGenerationRetentionConfig(**kwargs)


def test_cleanup_requires_an_aware_clock_before_opening_database() -> None:
    database = FakeDatabase([pair(41)])

    with pytest.raises(ProviderGenerationRetentionError, match="datetime_invalid"):
        PostgresProviderGenerationRetention(database.connection).cleanup(
            ProviderGenerationRetentionConfig(model="gpt-5.6-sol"),
            now=datetime(2026, 7, 29, 2, 0),
        )

    assert database.connections == []


def test_cleanup_cli_defaults_to_dry_run_and_requires_explicit_apply() -> None:
    args = parser().parse_args(["--model", "gpt-5.6-sol"])
    assert args.apply is False
    assert args.retention_days == 180
    assert args.batch_size == 100

    database = FakeDatabase([pair(51)])
    dry_run = run(
        "gpt-5.6-sol",
        apply=False,
        connection_factory=database.connection,
    )
    assert dry_run["mode"] == "dry-run"
    assert dry_run["archived_pairs"] == 0


def test_generation_archive_migration_is_packaged_and_state_scoped() -> None:
    migration = Path("migrations/0029_provider_generation_archives.sql").read_text(
        encoding="utf-8"
    )
    packaged = Path(
        "src/a6api_auto/data/migrations/0029_provider_generation_archives.sql",
    ).read_text(encoding="utf-8")

    assert packaged == migration
    assert "a6_trade.provider_generation_archives" in migration
    assert "generation_state = 'superseded'" in migration
    assert "pricing_state = 'superseded'" in migration
    assert "routing_snapshot jsonb NOT NULL" in migration
    assert "cost_snapshot jsonb NOT NULL" in migration
    assert "slot_snapshot jsonb NOT NULL" in migration

    source = Path("src/a6api_auto/trade_provider_pool_retention.py").read_text(
        encoding="utf-8"
    )
    assert "to_jsonb(routing)" in source
