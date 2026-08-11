from __future__ import annotations

import os
import shutil
import socket
import subprocess
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from a6api_auto.trade_db import apply_migrations
from a6api_auto.trade_model_publication import (
    PostgresPublicationStore,
    PublicationState,
    decide_publication,
)
from a6api_auto.trade_provider_region import (
    PostgresProviderRegionStore,
    ProviderRegionCapacityPlan,
    ProviderRegionCapacityReceipt,
    ProviderRegionCapacityTarget,
    ProviderRegionError,
    ProviderRegionProjectionReceipt,
    ProviderRegionProjectionSubscriberAck,
)

psycopg = pytest.importorskip("psycopg")
dict_row = pytest.importorskip("psycopg.rows").dict_row

UTC = timezone.utc
NOW = datetime(2026, 7, 30, 7, tzinfo=UTC)
HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64


@pytest.fixture(scope="module")
def regional_postgres(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    initdb = shutil.which("initdb")
    pg_ctl = shutil.which("pg_ctl")
    if initdb is None or pg_ctl is None:
        pytest.skip("PostgreSQL server binaries are not installed")
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("initdb does not run as root")

    data_dir = tmp_path_factory.mktemp("provider-region-pg") / "data"
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
            str(data_dir.parent / "postgres.log"),
            "-w",
            "start",
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    dsn = f"dbname=postgres host=127.0.0.1 port={port}"
    try:
        with psycopg.connect(dsn) as connection:
            for role in (
                "a6trader",
                "sub2api",
                "sub2api_cm",
                "a6_provider_projector",
                "a6_provider_subscriber",
            ):
                connection.execute(f"CREATE ROLE {role} NOLOGIN")
            connection.commit()
            applied = apply_migrations(connection, Path("migrations"))
            assert "0048" in applied
            assert tuple(sorted(applied)) == applied
        yield dsn
    finally:
        subprocess.run(
            [pg_ctl, "-D", str(data_dir), "-m", "immediate", "-w", "stop"],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )


def _connect(dsn: str) -> Any:
    return psycopg.connect(dsn, row_factory=dict_row)


def _store(dsn: str) -> PostgresProviderRegionStore:
    return PostgresProviderRegionStore(lambda: _connect(dsn))


def _role_store(dsn: str, role: str) -> PostgresProviderRegionStore:
    def connect() -> Any:
        connection = _connect(dsn)
        connection.execute(f"SET ROLE {role}")
        return connection

    return PostgresProviderRegionStore(connect)


def test_model_publication_thresholds_round_trip_in_postgres(
    regional_postgres: str,
) -> None:
    store = PostgresPublicationStore(lambda: _connect(regional_postgres))
    decision = decide_publication(
        "publication-threshold-fixture",
        healthy_slots=1,
        minimum_healthy_slots=1,
        redundancy_target=3,
        generation=1,
        cost_generation=1,
        bundle_confirmed=True,
    )

    written = store.write(decision)
    read_back = store.read(decision.model)

    assert written.desired_state is PublicationState.DEGRADED
    assert written.minimum_healthy_slots == 1
    assert written.redundancy_target == 3
    assert read_back == written


def _insert_provider_fixture(
    dsn: str,
    *,
    model: str,
    account_id: int,
    concurrency: int,
) -> int:
    with _connect(dsn) as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO a6_trade.a6_merchants (
                session_ref, session_fingerprint, roles, status
            ) VALUES (%s, %s, ARRAY['provider'], 'active')
            RETURNING id
            """,
            (f"session-{account_id}", HASH_A),
        )
        merchant_id = int(cursor.fetchone()["id"])
        cursor.execute(
            """
            INSERT INTO a6_trade.a6_tokens (
                merchant_id, a6_token_id, token_name, key_fingerprint, status
            ) VALUES (%s, %s, %s, %s, 'active')
            RETURNING id
            """,
            (merchant_id, account_id + 100, f"token-{account_id}", HASH_B),
        )
        token_id = int(cursor.fetchone()["id"])
        cursor.execute(
            """
            INSERT INTO a6_trade.enodot_account_bindings (
                a6_token_id, enodot_account_id, binding_uid, model_scope,
                desired_priority, desired_concurrency, state
            ) VALUES (%s, %s, %s, ARRAY[%s], 2, %s, 'bound')
            RETURNING id
            """,
            (token_id, account_id, f"binding-{account_id}", model, concurrency),
        )
        binding_id = int(cursor.fetchone()["id"])
        cursor.execute(
            """
            INSERT INTO a6_trade.provider_cost_generations (
                model, cost_generation, cost_source, cost_input_hash,
                cost_output_hash, token_mix_version, exchange_rate_version,
                pricing_state, config_version
            ) VALUES (%s, 1, 'regional-test', %s, %s, 'mix-1', 'fx-1',
                      'active', 'regional-test-v1')
            RETURNING id
            """,
            (model, HASH_A, HASH_B),
        )
        cost_generation_id = int(cursor.fetchone()["id"])
        cursor.execute(
            """
            INSERT INTO a6_trade.provider_pool_generations (
                model, generation, generation_source, source_hash,
                cost_generation_id, cost_generation, desired_size,
                generation_state, config_version
            ) VALUES (%s, 1, 'regional-test', %s, %s, 1, 1,
                      'active', 'regional-test-v1')
            """,
            (model, HASH_A, cost_generation_id),
        )
    return binding_id


def _projection(
    *,
    model: str,
    state: str,
    cursor: int,
    epoch: str = "epoch-1",
    digest: str = HASH_B,
    projected_at: datetime = NOW,
    subscriber_verified_at: datetime | None = None,
    verified_at: datetime | None = None,
) -> ProviderRegionProjectionReceipt:
    ready = state == "ready"
    return ProviderRegionProjectionReceipt(
        region="us",
        model=model,
        projection_type="dispatch",
        routing_generation=1,
        cost_generation=1,
        source_hash=HASH_A,
        projection_digest=digest,
        last_event_id=9,
        subscriber_cursor=cursor,
        subscriber_connected=ready,
        redis_restart_epoch=epoch,
        projection_state=state,
        projected_at=projected_at,
        subscriber_verified_at=(
            subscriber_verified_at if subscriber_verified_at is not None else projected_at
        )
        if ready
        else None,
        verified_at=(verified_at if verified_at is not None else projected_at) if ready else None,
    )


def _projection_ack(
    receipt: ProviderRegionProjectionReceipt,
    *,
    verified_at: datetime,
) -> ProviderRegionProjectionSubscriberAck:
    return ProviderRegionProjectionSubscriberAck(
        region=receipt.region,
        model=receipt.model,
        projection_type=receipt.projection_type,
        routing_generation=receipt.routing_generation,
        cost_generation=receipt.cost_generation,
        source_hash=receipt.source_hash,
        projection_digest=receipt.projection_digest,
        last_event_id=receipt.last_event_id,
        redis_restart_epoch=receipt.redis_restart_epoch,
        subscriber_cursor=receipt.last_event_id,
        subscriber_verified_at=verified_at,
    )


def _capacity_plan(
    *,
    binding_id: int,
    account_id: int,
    key: str,
    effective_at: datetime,
    us: ProviderRegionCapacityTarget,
    hk: ProviderRegionCapacityTarget,
) -> ProviderRegionCapacityPlan:
    return ProviderRegionCapacityPlan(
        enodot_binding_id=binding_id,
        enodot_account_id=account_id,
        account_concurrency=10,
        allocation_key=key,
        source_hash=HASH_A,
        reason="regional capacity integration",
        effective_at=effective_at,
        targets=(us, hk),
    )


def test_projection_receipt_cas_digest_and_restart_round_trip(
    regional_postgres: str,
) -> None:
    model = "regional-projection-test"
    _insert_provider_fixture(
        regional_postgres,
        model=model,
        account_id=81001,
        concurrency=10,
    )
    store = _store(regional_postgres)

    initial_receipt = _projection(model=model, state="resyncing", cursor=0)
    initial = store.record_projection_receipt(
        initial_receipt,
        expected_receipt_version=0,
    )
    assert initial.receipt_version == 1
    acked = store.ack_projection_receipt(
        _projection_ack(initial_receipt, verified_at=NOW + timedelta(seconds=1)),
        expected_receipt_version=1,
    )
    assert acked.receipt_version == 2
    ready = store.record_projection_receipt(
        _projection(
            model=model,
            state="ready",
            cursor=9,
            projected_at=NOW,
            subscriber_verified_at=NOW + timedelta(seconds=1),
            verified_at=NOW + timedelta(seconds=2),
        ),
        expected_receipt_version=2,
    )
    assert ready.receipt_version == 3

    with pytest.raises(ProviderRegionError, match="provider_region_receipt_digest_conflict"):
        store.record_projection_receipt(
            _projection(
                model=model,
                state="ready",
                cursor=9,
                digest=HASH_C,
                projected_at=NOW,
                subscriber_verified_at=NOW + timedelta(seconds=1),
                verified_at=NOW + timedelta(seconds=3),
            ),
            expected_receipt_version=3,
        )
    with pytest.raises(ProviderRegionError, match="provider_region_restart_resync_required"):
        store.record_projection_receipt(
            _projection(
                model=model,
                state="ready",
                cursor=9,
                epoch="epoch-2",
                projected_at=NOW,
                subscriber_verified_at=NOW + timedelta(seconds=1),
                verified_at=NOW + timedelta(seconds=3),
            ),
            expected_receipt_version=3,
        )

    restarted = store.record_projection_receipt(
        _projection(
            model=model,
            state="resyncing",
            cursor=0,
            epoch="epoch-2",
            projected_at=NOW + timedelta(seconds=2),
        ),
        expected_receipt_version=3,
    )
    assert restarted.receipt_version == 4
    restarted_receipt = _projection(
        model=model,
        state="resyncing",
        cursor=0,
        epoch="epoch-2",
        projected_at=NOW + timedelta(seconds=2),
    )
    restarted_ack = store.ack_projection_receipt(
        _projection_ack(restarted_receipt, verified_at=NOW + timedelta(seconds=3)),
        expected_receipt_version=4,
    )
    assert restarted_ack.receipt_version == 5
    recovered = store.record_projection_receipt(
        _projection(
            model=model,
            state="ready",
            cursor=9,
            epoch="epoch-2",
            projected_at=NOW + timedelta(seconds=2),
            subscriber_verified_at=NOW + timedelta(seconds=3),
            verified_at=NOW + timedelta(seconds=4),
        ),
        expected_receipt_version=5,
    )
    assert recovered.receipt_version == 6

    with (
        pytest.raises(psycopg.errors.CheckViolation, match="digest_conflict"),
        _connect(regional_postgres) as connection,
    ):
        connection.execute(
            """
            UPDATE a6_trade.provider_region_projection_receipts
            SET projection_digest = %s, receipt_version = receipt_version + 1
            WHERE region = 'us' AND model = %s AND projection_type = 'dispatch'
            """,
            (HASH_C, model),
        )

    with _connect(regional_postgres) as connection:
        row = connection.execute(
            """
            SELECT receipt_version, projection_digest, redis_restart_epoch,
                   subscriber_cursor, projection_state
            FROM a6_trade.provider_region_projection_receipts
            WHERE region = 'us' AND model = %s AND projection_type = 'dispatch'
            """,
            (model,),
        ).fetchone()
    assert row == {
        "receipt_version": 6,
        "projection_digest": HASH_B,
        "redis_restart_epoch": "epoch-2",
        "subscriber_cursor": 9,
        "projection_state": "ready",
    }


def test_dispatch_absent_tombstone_round_trips_after_active_generation_closes(
    regional_postgres: str,
) -> None:
    model = "regional-tombstone-test"
    _insert_provider_fixture(
        regional_postgres,
        model=model,
        account_id=81501,
        concurrency=10,
    )
    with _connect(regional_postgres) as connection:
        connection.execute(
            """
            UPDATE a6_trade.provider_pool_generations
            SET generation_state = 'superseded', activated_at = %s
            WHERE model = %s
            """,
            (NOW, model),
        )
        connection.execute(
            """
            UPDATE a6_trade.provider_cost_generations
            SET pricing_state = 'superseded', activated_at = %s
            WHERE model = %s
            """,
            (NOW, model),
        )

    store = _store(regional_postgres)
    tombstone = ProviderRegionProjectionReceipt(
        region="us",
        model=model,
        projection_type="dispatch",
        routing_generation=1,
        cost_generation=1,
        source_hash="0" * 64,
        projection_digest=HASH_C,
        redis_restart_epoch="epoch-1",
        projected_at=NOW + timedelta(seconds=1),
        projection_state="resyncing",
    )
    first = store.record_projection_receipt(tombstone, expected_receipt_version=0)
    assert first.receipt_version == 1
    verified_at = NOW + timedelta(seconds=2)
    acked = store.ack_projection_receipt(
        _projection_ack(tombstone, verified_at=verified_at),
        expected_receipt_version=first.receipt_version,
    )
    assert acked.receipt_version == 2
    ready = ProviderRegionProjectionReceipt(
        **{
            **tombstone.__dict__,
            "subscriber_cursor": 0,
            "subscriber_connected": True,
            "projection_state": "ready",
            "subscriber_verified_at": verified_at,
            "verified_at": NOW + timedelta(seconds=3),
        }
    )
    assert (
        store.record_projection_receipt(
            ready,
            expected_receipt_version=acked.receipt_version,
        ).receipt_version
        == 3
    )


def test_capacity_escrow_drain_before_transfer_and_database_sum_guard(
    regional_postgres: str,
) -> None:
    account_id = 82001
    binding_id = _insert_provider_fixture(
        regional_postgres,
        model="regional-capacity-test",
        account_id=account_id,
        concurrency=10,
    )
    store = _store(regional_postgres)
    initial_plan = _capacity_plan(
        binding_id=binding_id,
        account_id=account_id,
        key="capacity-v1",
        effective_at=NOW,
        us=ProviderRegionCapacityTarget("us", 8, "active"),
        hk=ProviderRegionCapacityTarget("hk", 2, "active"),
    )
    initial = store.set_capacity_allocations(initial_plan, expected_allocation_version=0)
    assert initial.allocation_version == 1

    with (
        pytest.raises(psycopg.errors.CheckViolation, match="current_version_invalid"),
        _connect(regional_postgres) as connection,
    ):
        connection.execute(
            """
            DELETE FROM a6_trade.provider_region_capacity_allocations
            WHERE enodot_account_id = %s AND region = 'hk' AND valid_to IS NULL
            """,
            (account_id,),
        )

    transfer_plan = _capacity_plan(
        binding_id=binding_id,
        account_id=account_id,
        key="capacity-v3",
        effective_at=NOW + timedelta(seconds=4),
        us=ProviderRegionCapacityTarget("us", 5, "active"),
        hk=ProviderRegionCapacityTarget("hk", 5, "active"),
    )
    with pytest.raises(ProviderRegionError, match="provider_region_capacity_drain_required"):
        store.set_capacity_allocations(transfer_plan, expected_allocation_version=1)

    drain_plan = _capacity_plan(
        binding_id=binding_id,
        account_id=account_id,
        key="capacity-v2",
        effective_at=NOW + timedelta(seconds=1),
        us=ProviderRegionCapacityTarget("us", 8, "draining", 5),
        hk=ProviderRegionCapacityTarget("hk", 2, "active"),
    )
    draining = store.set_capacity_allocations(drain_plan, expected_allocation_version=1)
    assert draining.allocation_version == 2

    receipt = ProviderRegionCapacityReceipt(
        enodot_binding_id=binding_id,
        enodot_account_id=account_id,
        region="us",
        allocation_version=2,
        quota=8,
        occupancy=5,
        occupancy_digest=HASH_A,
        lease_cursor=0,
        redis_restart_epoch="epoch-1",
        projected_at=NOW + timedelta(seconds=2),
        receipt_state="resyncing",
    )
    assert store.record_capacity_receipt(receipt, expected_receipt_version=0).receipt_version == 1
    verified_receipt = ProviderRegionCapacityReceipt(
        **{
            **receipt.__dict__,
            "receipt_state": "ready",
            "verified_at": NOW + timedelta(seconds=3),
        }
    )
    assert (
        store.ack_capacity_receipt(
            verified_receipt,
            expected_receipt_version=1,
        ).receipt_version
        == 2
    )

    transferred = store.set_capacity_allocations(
        transfer_plan,
        expected_allocation_version=2,
    )
    assert transferred.allocation_version == 3
    with _connect(regional_postgres) as connection:
        rows = connection.execute(
            """
            SELECT region, quota, allocation_version
            FROM a6_trade.provider_region_capacity_allocations
            WHERE enodot_account_id = %s AND valid_to IS NULL
            ORDER BY region
            """,
            (account_id,),
        ).fetchall()
    assert rows == [
        {"region": "hk", "quota": 5, "allocation_version": 3},
        {"region": "us", "quota": 5, "allocation_version": 3},
    ]

    with (
        pytest.raises(psycopg.errors.CheckViolation, match="overallocated"),
        _connect(regional_postgres) as connection,
    ):
        connection.execute(
            """
            UPDATE a6_trade.provider_region_capacity_allocations
            SET quota = 6
            WHERE enodot_account_id = %s AND region = 'hk' AND valid_to IS NULL
            """,
            (account_id,),
        )

    with (
        pytest.raises(psycopg.errors.CheckViolation, match="overallocated"),
        _connect(regional_postgres) as connection,
    ):
        connection.execute(
            """
            UPDATE a6_trade.enodot_account_bindings
            SET desired_concurrency = 9
            WHERE id = %s
            """,
            (binding_id,),
        )


def test_projector_bootstrap_and_external_subscriber_ack_under_real_roles(
    regional_postgres: str,
) -> None:
    model = "regional-role-handshake-test"
    account_id = 82501
    binding_id = _insert_provider_fixture(
        regional_postgres,
        model=model,
        account_id=account_id,
        concurrency=10,
    )
    _store(regional_postgres).set_capacity_allocations(
        _capacity_plan(
            binding_id=binding_id,
            account_id=account_id,
            key="capacity-role-v1",
            effective_at=NOW,
            us=ProviderRegionCapacityTarget("us", 8, "active"),
            hk=ProviderRegionCapacityTarget("hk", 2, "active"),
        ),
        expected_allocation_version=0,
    )
    projector = _role_store(regional_postgres, "a6_provider_projector")
    subscriber = _role_store(regional_postgres, "a6_provider_subscriber")

    projection = _projection(model=model, state="resyncing", cursor=0)
    projected = projector.record_projection_receipt(
        projection,
        expected_receipt_version=0,
    )
    assert projected.receipt_version == 1

    capacity = ProviderRegionCapacityReceipt(
        enodot_binding_id=binding_id,
        enodot_account_id=account_id,
        region="us",
        allocation_version=1,
        quota=8,
        occupancy=0,
        occupancy_digest=HASH_A,
        lease_cursor=0,
        redis_restart_epoch="epoch-1",
        projected_at=NOW,
        receipt_state="resyncing",
    )
    bootstrapped = projector.record_capacity_receipt(
        capacity,
        expected_receipt_version=0,
    )
    assert bootstrapped.receipt_version == 1

    with pytest.raises(psycopg.errors.CheckViolation, match="ready_complete"):
        projector.record_projection_receipt(
            _projection(
                model=model,
                state="ready",
                cursor=9,
                projected_at=NOW,
                subscriber_verified_at=NOW + timedelta(seconds=1),
                verified_at=NOW + timedelta(seconds=2),
            ),
            expected_receipt_version=1,
        )
    with pytest.raises(
        ProviderRegionError,
        match="provider_region_capacity_projector_state_invalid",
    ):
        projector.record_capacity_receipt(
            ProviderRegionCapacityReceipt(
                **{
                    **capacity.__dict__,
                    "receipt_state": "ready",
                    "verified_at": NOW + timedelta(seconds=1),
                }
            ),
            expected_receipt_version=1,
        )

    projection_ack = subscriber.ack_projection_receipt(
        _projection_ack(projection, verified_at=NOW + timedelta(seconds=1)),
        expected_receipt_version=1,
    )
    assert projection_ack.receipt_version == 2
    capacity_ack = subscriber.ack_capacity_receipt(
        ProviderRegionCapacityReceipt(
            **{
                **capacity.__dict__,
                "receipt_state": "ready",
                "verified_at": NOW + timedelta(seconds=1),
            }
        ),
        expected_receipt_version=1,
    )
    assert capacity_ack.receipt_version == 2

    finalized = projector.record_projection_receipt(
        _projection(
            model=model,
            state="ready",
            cursor=9,
            projected_at=NOW,
            subscriber_verified_at=NOW + timedelta(seconds=1),
            verified_at=NOW + timedelta(seconds=2),
        ),
        expected_receipt_version=2,
    )
    assert finalized.receipt_version == 3

    for role, statement, value in (
        (
            "a6_provider_projector",
            "UPDATE a6_trade.provider_region_projection_receipts "
            "SET subscriber_connected = true WHERE model = %s",
            model,
        ),
        (
            "a6_provider_projector",
            "UPDATE a6_trade.provider_region_capacity_receipts "
            "SET receipt_state = 'ready' WHERE enodot_account_id = %s",
            account_id,
        ),
        (
            "a6_provider_subscriber",
            "UPDATE a6_trade.provider_region_projection_receipts "
            "SET projection_state = 'ready' WHERE model = %s",
            model,
        ),
        (
            "a6_provider_subscriber",
            "UPDATE a6_trade.provider_region_capacity_receipts "
            "SET occupancy_digest = %s WHERE enodot_account_id = 82501",
            HASH_C,
        ),
    ):
        with (
            pytest.raises(psycopg.errors.InsufficientPrivilege),
            _connect(regional_postgres) as connection,
        ):
            connection.execute(f"SET ROLE {role}")
            connection.execute(statement, (value,))

    with _connect(regional_postgres) as connection:
        projection_row = connection.execute(
            """
            SELECT projection_state, subscriber_connected, subscriber_cursor,
                   receipt_version
            FROM a6_trade.provider_region_projection_receipts
            WHERE model = %s
            """,
            (model,),
        ).fetchone()
        capacity_row = connection.execute(
            """
            SELECT receipt_state, occupancy, receipt_version
            FROM a6_trade.provider_region_capacity_receipts
            WHERE enodot_account_id = %s AND region = 'us'
            """,
            (account_id,),
        ).fetchone()
    assert projection_row == {
        "projection_state": "ready",
        "subscriber_connected": True,
        "subscriber_cursor": 9,
        "receipt_version": 3,
    }
    assert capacity_row == {
        "receipt_state": "ready",
        "occupancy": 0,
        "receipt_version": 2,
    }


def test_regional_roles_have_only_their_coordination_privileges(
    regional_postgres: str,
) -> None:
    tables = {
        "projection": "a6_trade.provider_region_projection_receipts",
        "allocation": "a6_trade.provider_region_capacity_allocations",
        "capacity_receipt": "a6_trade.provider_region_capacity_receipts",
    }
    with _connect(regional_postgres) as connection:
        for role in ("sub2api", "sub2api_cm"):
            for table in tables.values():
                assert _table_privilege(connection, role, table, "SELECT")
                assert not _table_privilege(connection, role, table, "INSERT")
                assert not _table_privilege(connection, role, table, "UPDATE")
                assert not _table_privilege(connection, role, table, "DELETE")

        assert not _table_privilege(
            connection,
            "a6_provider_projector",
            tables["projection"],
            "INSERT",
        )
        assert _column_privilege(
            connection,
            "a6_provider_projector",
            tables["projection"],
            "projection_state",
            "INSERT",
        )
        assert not _table_privilege(
            connection,
            "a6_provider_subscriber",
            tables["capacity_receipt"],
            "UPDATE",
        )
        assert not _column_privilege(
            connection,
            "a6_provider_subscriber",
            tables["capacity_receipt"],
            "occupancy_digest",
            "UPDATE",
        )
        assert not _column_privilege(
            connection,
            "a6_provider_subscriber",
            tables["capacity_receipt"],
            "region",
            "UPDATE",
        )
        for canonical in (
            "a6_trade.provider_pool_generations",
            "a6_trade.provider_cost_generations",
            "a6_trade.provider_pool_slots",
            "a6_trade.provider_fast_block_outbox",
            "a6_trade.provider_quarantines",
        ):
            assert _table_privilege(
                connection,
                "a6_provider_subscriber",
                canonical,
                "SELECT",
            )
        assert not _table_privilege(
            connection,
            "a6_provider_projector",
            tables["allocation"],
            "INSERT",
        )
        assert not _table_privilege(
            connection,
            "a6_provider_projector",
            tables["capacity_receipt"],
            "INSERT",
        )
        assert _column_privilege(
            connection,
            "a6_provider_subscriber",
            tables["projection"],
            "subscriber_cursor",
            "UPDATE",
        )
        assert not _column_privilege(
            connection,
            "a6_provider_projector",
            tables["projection"],
            "subscriber_cursor",
            "UPDATE",
        )
        assert not _column_privilege(
            connection,
            "a6_provider_subscriber",
            tables["projection"],
            "projection_digest",
            "UPDATE",
        )
        assert not _table_privilege(
            connection,
            "a6_provider_subscriber",
            tables["capacity_receipt"],
            "INSERT",
        )
        assert _column_privilege(
            connection,
            "a6_provider_projector",
            tables["capacity_receipt"],
            "occupancy_digest",
            "UPDATE",
        )
        assert not _column_privilege(
            connection,
            "a6_provider_projector",
            tables["capacity_receipt"],
            "receipt_state",
            "UPDATE",
        )
        assert _column_privilege(
            connection,
            "a6_provider_subscriber",
            tables["capacity_receipt"],
            "receipt_state",
            "UPDATE",
        )
        assert not _table_privilege(
            connection,
            "a6_provider_subscriber",
            tables["allocation"],
            "INSERT",
        )
        assert _table_privilege(connection, "a6trader", tables["allocation"], "INSERT")
        assert _column_privilege(
            connection,
            "a6trader",
            tables["allocation"],
            "valid_to",
            "UPDATE",
        )
        assert not _column_privilege(
            connection,
            "a6trader",
            tables["allocation"],
            "quota",
            "UPDATE",
        )
        assert not _table_privilege(connection, "a6trader", tables["allocation"], "DELETE")
        assert not _table_privilege(connection, "a6trader", tables["projection"], "INSERT")
        assert not _table_privilege(
            connection,
            "a6trader",
            tables["capacity_receipt"],
            "UPDATE",
        )


def _table_privilege(
    connection: Any,
    role: str,
    table: str,
    privilege: str,
) -> bool:
    row = connection.execute(
        "SELECT has_table_privilege(%s, %s, %s) AS allowed",
        (role, table, privilege),
    ).fetchone()
    return bool(row["allowed"])


def _column_privilege(
    connection: Any,
    role: str,
    table: str,
    column: str,
    privilege: str,
) -> bool:
    row = connection.execute(
        "SELECT has_column_privilege(%s, %s, %s, %s) AS allowed",
        (role, table, column, privilege),
    ).fetchone()
    return bool(row["allowed"])
