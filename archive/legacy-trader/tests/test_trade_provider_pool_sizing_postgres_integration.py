from __future__ import annotations

import os
import shutil
import socket
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg
import pytest

from a6api_auto.trade_provider_pool_shadow_controller import (
    _LATEST_SUCCESS_AT_SQL,
    _POOL_SIZE_EVIDENCE_SQL,
)

MODEL = "gpt-5.6-sol"


@contextmanager
def temporary_postgres(tmp_path: Path) -> Iterator[psycopg.Connection]:
    initdb = shutil.which("initdb")
    pg_ctl = shutil.which("pg_ctl")
    if initdb is None or pg_ctl is None:
        pytest.skip("PostgreSQL server binaries are not installed")
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("initdb does not run as root")

    data_dir = tmp_path / "pgdata"
    subprocess.run(
        [
            initdb,
            "-D",
            str(data_dir),
            "--auth=trust",
            "--no-locale",
            "--encoding=UTF8",
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    subprocess.run(
        [
            pg_ctl,
            "-D",
            str(data_dir),
            "-o",
            f"-F -h 127.0.0.1 -p {port}",
            "-l",
            str(tmp_path / "postgres.log"),
            "-w",
            "start",
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    connection: psycopg.Connection | None = None
    try:
        connection = psycopg.connect(
            dbname="postgres",
            host="127.0.0.1",
            port=port,
            autocommit=True,
        )
        yield connection
    finally:
        if connection is not None:
            connection.close()
        subprocess.run(
            [pg_ctl, "-D", str(data_dir), "-m", "immediate", "-w", "stop"],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )


def test_latest_success_merge_uses_latest_nonnull_source(tmp_path: Path) -> None:
    reference = datetime(2026, 7, 30, 8, tzinfo=timezone.utc)
    cases = (
        (
            "observer_newer",
            reference - timedelta(minutes=2),
            reference - timedelta(minutes=1),
            reference - timedelta(minutes=1),
        ),
        (
            "business_newer",
            reference - timedelta(minutes=1),
            reference - timedelta(minutes=2),
            reference - timedelta(minutes=1),
        ),
        ("business_null", None, reference, reference),
        ("observer_null", reference, None, reference),
        ("both_null", None, None, None),
    )
    with temporary_postgres(tmp_path) as connection:
        for scenario, business_success_at, observer_success_at, expected in cases:
            row = connection.execute(
                f"""
                SELECT {_LATEST_SUCCESS_AT_SQL}
                FROM (
                    SELECT %s::timestamptz AS last_success_at
                ) business_success
                CROSS JOIN (
                    SELECT %s::timestamptz AS last_success_at
                ) candidate
                """,
                (business_success_at, observer_success_at),
            ).fetchone()

            assert row == (expected,), scenario


def test_sizing_query_executes_and_uses_bounded_evidence_indexes(
    tmp_path: Path,
) -> None:
    decision_at = datetime(2026, 7, 30, 8, tzinfo=timezone.utc)
    with temporary_postgres(tmp_path) as connection:
        connection.execute(_MINIMUM_SCHEMA_SQL)
        connection.execute(
            Path("migrations/0040_provider_pool_sizing_evidence_indexes.sql").read_text()
        )
        _insert_fixture(connection, decision_at=decision_at)

        row = connection.execute(
            _POOL_SIZE_EVIDENCE_SQL,
            (MODEL, decision_at, decision_at - timedelta(minutes=5)),
        ).fetchone()

        assert row is not None
        assert row[0] == decision_at - timedelta(days=30)
        assert row[1] == decision_at - timedelta(hours=1)
        assert row[2] == 1
        assert row[3] == 1
        assert row[4] == 2
        assert row[5] is True
        assert row[6] is True
        assert row[7] == 1

        connection.execute("SET enable_seqscan = off")
        plan_rows = connection.execute(
            f"EXPLAIN (COSTS OFF) {_POOL_SIZE_EVIDENCE_SQL}",
            (MODEL, decision_at, decision_at - timedelta(minutes=5)),
        ).fetchall()
        plan = "\n".join(str(item[0]) for item in plan_rows)

        assert "provider_request_envelopes_model_created_idx" in plan
        assert "a6_financial_snapshots_merchant_observed_idx" in plan


def _insert_fixture(
    connection: psycopg.Connection,
    *,
    decision_at: datetime,
) -> None:
    connection.execute(
        """
        INSERT INTO a6_trade.provider_pool_generations
            (model, generation, desired_size, planned_at)
        VALUES (%s, 1, 5, %s), (%s, 2, 4, %s)
        """,
        (
            MODEL,
            decision_at - timedelta(days=30),
            MODEL,
            decision_at - timedelta(hours=1),
        ),
    )
    connection.execute(
        """
        INSERT INTO a6_trade.provider_request_envelopes
            (id, request_id, model, created_at, completed_at)
        VALUES
            (1, 'old-request', %s, %s, %s),
            (2, 'recent-request', %s, %s, %s)
        """,
        (
            MODEL,
            decision_at - timedelta(days=20),
            decision_at - timedelta(days=20, minutes=-10),
            MODEL,
            decision_at - timedelta(minutes=20),
            decision_at - timedelta(minutes=10),
        ),
    )
    connection.execute(
        """
        INSERT INTO a6_trade.provider_attempts
            (request_envelope_id, request_id, slot_no, state)
        SELECT 1, 'old-request', slot_no, 'failed'
        FROM generate_series(1, 10) AS slot_no
        UNION ALL
        SELECT 2, 'recent-request', 1, 'failed'
        """
    )
    connection.execute(
        """
        INSERT INTO a6_trade.a6_merchants (id, status, roles)
        VALUES
            (1, 'supplier_submitted', ARRAY['provider']::text[]),
            (2, 'supplier_submitted', ARRAY['provider']::text[]),
            (3, 'supplier_submitted', ARRAY['provider']::text[]);
        INSERT INTO a6_trade.a6_tokens (id, status)
        VALUES (11, 'active'), (12, 'active'), (13, 'active');
        INSERT INTO a6_trade.enodot_account_bindings
            (id, a6_token_id, enodot_account_id, state)
        VALUES
            (21, 11, 31, 'bound'),
            (22, 12, 32, 'bound'),
            (23, 13, 33, 'bound')
        """
    )
    connection.execute(
        """
        INSERT INTO a6_trade.provider_inventory
            (model, merchant_id, a6_token_id, enodot_binding_id,
             enodot_account_id, funding_eligible, liquidity_state, inventory_state)
        VALUES
            (%s, 1, 11, 21, 31, true, 'FUNDED', 'READY'),
            (%s, 2, 12, 22, 32, true, 'REFILLING', 'WARM_STANDBY'),
            (%s, 3, 13, 23, 33, true, 'TRANSFER_UNKNOWN', 'WARM_STANDBY')
        """,
        (MODEL, MODEL, MODEL),
    )
    connection.execute(
        """
        INSERT INTO a6_trade.a6_financial_current
            (merchant_id, observed_at, used_quota,
             user_balance_quota, withdrawable_micros)
        VALUES
            (1, %s, 100000, 200000, 0),
            (2, %s, 100000, 200000, 0),
            (3, %s, 100000, 200000, 0)
        """,
        (decision_at, decision_at, decision_at),
    )
    connection.execute(
        """
        INSERT INTO a6_trade.a6_financial_snapshots
            (merchant_id, observed_at, used_quota)
        VALUES (1, %s, 0), (2, %s, 0), (3, %s, 0)
        """,
        (
            decision_at - timedelta(hours=24),
            decision_at - timedelta(hours=24),
            decision_at - timedelta(hours=24),
        ),
    )


_MINIMUM_SCHEMA_SQL = """
CREATE SCHEMA a6_trade;

CREATE TABLE a6_trade.provider_pool_generations (
    model text NOT NULL,
    generation bigint NOT NULL,
    desired_size integer NOT NULL,
    planned_at timestamptz NOT NULL
);

CREATE TABLE a6_trade.provider_request_envelopes (
    id bigint PRIMARY KEY,
    request_id text NOT NULL UNIQUE,
    model text NOT NULL,
    created_at timestamptz NOT NULL,
    completed_at timestamptz
);

CREATE TABLE a6_trade.provider_attempts (
    request_envelope_id bigint NOT NULL,
    request_id text NOT NULL,
    slot_no integer NOT NULL,
    state text NOT NULL
);

CREATE INDEX provider_attempts_request_idx
    ON a6_trade.provider_attempts (request_envelope_id, request_id);

CREATE TABLE a6_trade.a6_merchants (
    id bigint PRIMARY KEY,
    status text NOT NULL,
    roles text[] NOT NULL
);

CREATE TABLE a6_trade.a6_tokens (
    id bigint PRIMARY KEY,
    status text NOT NULL
);

CREATE TABLE a6_trade.enodot_account_bindings (
    id bigint PRIMARY KEY,
    a6_token_id bigint NOT NULL,
    enodot_account_id bigint NOT NULL,
    state text NOT NULL
);

CREATE TABLE a6_trade.provider_inventory (
    model text NOT NULL,
    merchant_id bigint NOT NULL,
    a6_token_id bigint NOT NULL,
    enodot_binding_id bigint NOT NULL,
    enodot_account_id bigint NOT NULL,
    funding_eligible boolean NOT NULL,
    liquidity_state text NOT NULL,
    inventory_state text NOT NULL
);

CREATE TABLE a6_trade.a6_financial_current (
    merchant_id bigint PRIMARY KEY,
    observed_at timestamptz NOT NULL,
    used_quota bigint,
    user_balance_quota bigint,
    withdrawable_micros bigint
);

CREATE TABLE a6_trade.a6_financial_snapshots (
    merchant_id bigint NOT NULL,
    observed_at timestamptz NOT NULL,
    used_quota bigint
);
"""
