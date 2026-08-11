"""Static contract between hand-written SQL and the migration DDL.

Every store in this codebase is unit-tested against an in-memory fake, so a column name
that does not exist is invisible until the statement runs in production. That is exactly
how ``trade_provider_topup`` shipped an INSERT naming ``error_class`` when
``a6_trade.trade_incidents`` only has ``incident_class NOT NULL`` -- the test suite was
fully green.

These tests parse the migrations and check the SQL text, so the mismatch fails locally.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

# Created inline by trade_db.apply_migrations before any migration can run, so it has no
# migration file of its own.
_BOOTSTRAP_TABLES = frozenset({"schema_migrations"})

_NON_COLUMN_KEYWORDS = frozenset(
    {"PRIMARY", "UNIQUE", "CHECK", "CONSTRAINT", "FOREIGN", "EXCLUDE", "LIKE"}
)


def _migration_columns() -> dict[str, set[str]]:
    """Replay CREATE TABLE / ADD COLUMN / DROP COLUMN across migrations in order."""

    columns: dict[str, set[str]] = {}
    for path in sorted(Path("migrations").glob("*.sql")):
        sql = path.read_text(encoding="utf-8")
        for match in re.finditer(
            r"CREATE TABLE (?:IF NOT EXISTS )?a6_trade\.(\w+)\s*\((.*?)\n\)\s*;",
            sql,
            re.S | re.I,
        ):
            table = match.group(1).lower()
            found: set[str] = set()
            for line in match.group(2).split("\n"):
                stripped = line.strip()
                if not stripped or stripped.startswith("--"):
                    continue
                first = stripped.split()[0].strip('",')
                if first.upper() in _NON_COLUMN_KEYWORDS:
                    continue
                if re.fullmatch(r"\w+", first):
                    found.add(first.lower())
            columns.setdefault(table, set()).update(found)
        for match in re.finditer(
            r"ALTER TABLE (?:IF EXISTS )?a6_trade\.(\w+)(.*?);", sql, re.S | re.I
        ):
            table = match.group(1).lower()
            body = match.group(2)
            for added in re.finditer(r"ADD COLUMN (?:IF NOT EXISTS )?(\w+)", body, re.I):
                columns.setdefault(table, set()).add(added.group(1).lower())
            for dropped in re.finditer(
                r"DROP COLUMN (?:IF EXISTS )?(\w+)", body, re.I
            ):
                columns.get(table, set()).discard(dropped.group(1).lower())
    return columns


def _insert_statements() -> list[tuple[str, str, str]]:
    """Return (module, table, column) for every column named in an INSERT column list."""

    triples: list[tuple[str, str, str]] = []
    for path in sorted(Path("src/a6api_auto").glob("*.py")):
        sql = path.read_text(encoding="utf-8")
        for match in re.finditer(r"INSERT INTO a6_trade\.(\w+)\s*\(([^)]*?)\)", sql, re.S):
            table = match.group(1).lower()
            for column in re.findall(r"\b(\w+)\b", match.group(2)):
                triples.append((path.name, table, column.lower()))
    return triples


def test_migrations_define_every_table_written_to() -> None:
    defined = set(_migration_columns()) | _BOOTSTRAP_TABLES
    written = {table for _module, table, _column in _insert_statements()}

    assert written - defined == set()


def test_every_inserted_column_exists_in_the_migration_ddl() -> None:
    columns = _migration_columns()
    mismatches = [
        f"{module}: a6_trade.{table}.{column}"
        for module, table, column in _insert_statements()
        if table not in _BOOTSTRAP_TABLES
        and column not in columns.get(table, set())
    ]

    assert mismatches == []


def test_the_parser_actually_found_the_schema() -> None:
    """Guard against the checks above passing because parsing silently produced nothing."""

    columns = _migration_columns()

    assert len(columns) >= 30
    assert "incident_class" in columns["trade_incidents"]
    assert "error_class" not in columns["trade_incidents"]
    assert "counterparty_class" in columns["a6_earning_events"]
    assert "suppressed_until" in columns["a6_channel_growth_policies"]
    assert len(_insert_statements()) >= 50


@pytest.mark.parametrize(
    ("table", "column"),
    [
        ("a6_earning_events", "counterparty_class"),
        ("a6_earning_events", "counterparty_token_ref_id"),
        ("a6_channel_growth_policies", "suppressed_until"),
        ("a6_channel_growth_policies", "suppression_reason"),
        ("a6_channel_growth_policies", "suppression_count"),
    ],
)
def test_ledger_and_suppression_columns_are_migrated(table: str, column: str) -> None:
    assert column in _migration_columns()[table]


def test_both_migration_directories_are_byte_identical() -> None:
    """`trade_db` picks whichever directory exists, so divergence is silent.

    `default_migration_directory()` prefers `<repo>/migrations` in a source checkout and
    falls back to the packaged `data/migrations`. Nothing verifies the two agree, so a
    migration added to only one of them passes every local test and then fails after
    deployment with a missing column.
    """

    source = Path("migrations")
    packaged = Path("src/a6api_auto/data/migrations")

    source_files = {path.name: path.read_bytes() for path in source.glob("*.sql")}
    packaged_files = {path.name: path.read_bytes() for path in packaged.glob("*.sql")}

    assert sorted(source_files) == sorted(packaged_files)
    mismatched = [name for name, body in source_files.items() if packaged_files[name] != body]
    assert mismatched == []
    assert len(source_files) >= 21
