"""PostgreSQL integration evidence for the Provider auto-pool recovery task.

Covers the full 0001-0069 migration chain, READY slot contract shapes, auto-pool
generation begin/claim/prepare/ready/activate/fail CAS semantics, stale claim
recovery, slot/inventory projections, membership completion CAS and the
model-scoped session lease plus activation barrier.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
from collections.abc import Callable, Iterator
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import psycopg
import pytest
from psycopg import sql
from psycopg.rows import dict_row

from a6api_auto.trade_db import apply_migrations
from a6api_auto.trade_provider_auto_pool import (
    AutoPoolCandidate,
    AutoPoolPlan,
    ProviderAutoPoolError,
)
from a6api_auto.trade_provider_auto_pool_store import (
    PostgresProviderAutoPoolStore,
    ProviderAutoPoolAccount,
)
from a6api_auto.trade_provider_membership_repair import (
    PostgresProviderMembershipRepairStore,
    ProviderMembershipRepairClaim,
    ProviderMembershipRepairError,
)
from a6api_auto.trade_provider_pool import (
    CostPlan,
    GenerationState,
    PricingState,
    ProviderBundle,
    ProviderPoolError,
    ProviderSlot,
    SlotState,
)
from a6api_auto.trade_provider_pool_reconciler import (
    PostgresProviderActivationBarrier,
    PostgresProviderModelLease,
    ProviderPoolReconcileError,
)
from a6api_auto.trade_provider_pool_store import PostgresProviderPoolStore

UTC = timezone.utc
MODEL = "gpt-5.6-sol"
SOURCE_HASH = "a" * 64
NOW = datetime(2026, 8, 4, 8, 30, tzinfo=UTC)


def _connect(dsn: str) -> psycopg.Connection:
    return psycopg.connect(dsn, row_factory=dict_row)


@pytest.fixture(scope="module")
def auto_pool_postgres(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    initdb = shutil.which("initdb")
    pg_ctl = shutil.which("pg_ctl")
    if initdb is None or pg_ctl is None:
        pytest.skip("PostgreSQL server binaries are not installed")
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("initdb does not run as root")

    root = tmp_path_factory.mktemp("auto-pool-postgres")
    data_dir = root / "data"
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
            str(root / "postgres.log"),
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
        with _connect(dsn) as connection:
            applied = apply_migrations(connection, Path("migrations"))
            expected = tuple(
                path.name.split("_", 1)[0]
                for path in sorted(Path("migrations").glob("[0-9][0-9][0-9][0-9]_*.sql"))
            )
            assert applied == expected
            assert "0069" in applied
            assert apply_migrations(connection, Path("migrations")) == ()
        yield dsn
    finally:
        subprocess.run(
            [pg_ctl, "-D", str(data_dir), "-m", "immediate", "-w", "stop"],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )


@pytest.fixture
def database(auto_pool_postgres: str) -> Iterator[str]:
    _truncate_business_tables(auto_pool_postgres)
    yield auto_pool_postgres
    _truncate_business_tables(auto_pool_postgres)


def _truncate_business_tables(dsn: str) -> None:
    with _connect(dsn) as connection:
        rows = connection.execute(
            """
            SELECT tablename
            FROM pg_tables
            WHERE schemaname = 'a6_trade'
              AND tablename <> 'schema_migrations'
            ORDER BY tablename
            """
        ).fetchall()
        identifiers = [sql.Identifier("a6_trade", str(row["tablename"])) for row in rows]
        if identifiers:
            connection.execute(
                sql.SQL("TRUNCATE {} RESTART IDENTITY CASCADE").format(
                    sql.SQL(", ").join(identifiers)
                )
            )


def _connection_factory(dsn: str) -> Callable[[], psycopg.Connection]:
    return lambda: _connect(dsn)


def _store(dsn: str) -> PostgresProviderAutoPoolStore:
    return PostgresProviderAutoPoolStore(
        _connection_factory(dsn),
        stale_claim_seconds=5,
    )


def _merchant(
    dsn: str,
    merchant_id: int,
    *,
    user_id: int | None = None,
    supplier_id: int | None = None,
    status: str = "supplier_submitted",
    roles: tuple[str, ...] = ("provider",),
) -> None:
    session_ref = f"session-{merchant_id}"
    with _connect(dsn) as connection:
        connection.execute(
            """
            INSERT INTO a6_trade.a6_merchants (
                id, a6_user_id, supplier_id, session_ref, session_fingerprint,
                roles, status
            ) VALUES (%s, %s, %s, %s, %s, %s::text[], %s)
            """,
            (
                merchant_id,
                user_id,
                supplier_id,
                session_ref,
                "f" * 64,
                list(roles),
                status,
            ),
        )


def _token(
    dsn: str,
    token_id: int,
    merchant_id: int,
    *,
    a6_token_id: int | None = None,
    status: str = "active",
) -> None:
    with _connect(dsn) as connection:
        connection.execute(
            """
            INSERT INTO a6_trade.a6_tokens (
                id, merchant_id, a6_token_id, token_name, status,
                created_by_controller
            ) VALUES (%s, %s, %s, %s, %s, true)
            """,
            (
                token_id,
                merchant_id,
                token_id if a6_token_id is None else a6_token_id,
                f"provider-token-{token_id}",
                status,
            ),
        )


def _binding(
    dsn: str,
    binding_id: int,
    token_id: int,
    account_id: int,
    *,
    state: str = "bound",
) -> None:
    with _connect(dsn) as connection:
        connection.execute(
            """
            INSERT INTO a6_trade.enodot_account_bindings (
                id, a6_token_id, enodot_account_id, binding_uid, state
            ) VALUES (%s, %s, %s, %s, %s)
            """,
            (binding_id, token_id, account_id, f"binding-{binding_id}", state),
        )


def _pin_binding(
    dsn: str,
    pin_binding_id: int,
    token_id: int,
    channel_id: int,
) -> None:
    with _connect(dsn) as connection:
        connection.execute(
            """
            INSERT INTO a6_trade.a6_pin_bindings (
                id, a6_token_id, model, channel_id, valid_from, change_reason
            ) VALUES (%s, %s, %s, %s, %s, 'integration-seed')
            """,
            (pin_binding_id, token_id, MODEL, channel_id, NOW),
        )


def _inventory(
    dsn: str,
    merchant_id: int,
    token_id: int,
    binding_id: int,
    account_id: int,
    *,
    inventory_state: str = "READY",
    schedulable: bool = True,
) -> None:
    with _connect(dsn) as connection:
        connection.execute(
            """
            INSERT INTO a6_trade.provider_inventory (
                model, merchant_id, enodot_binding_id, enodot_account_id,
                a6_token_id, inventory_state, liquidity_state, funding_eligible,
                schedulable, state_version
            ) VALUES (%s, %s, %s, %s, %s, %s, 'FUNDED', true, %s, 1)
            """,
            (
                MODEL,
                merchant_id,
                binding_id,
                account_id,
                token_id,
                inventory_state,
                schedulable,
            ),
        )


def _cost_generation(
    dsn: str,
    cost_generation: int,
    *,
    pricing_state: str = "active",
) -> int:
    with _connect(dsn) as connection:
        row = connection.execute(
            """
            INSERT INTO a6_trade.provider_cost_generations (
                model, cost_generation, cost_source, cost_input_hash,
                cost_output_hash, token_mix_version, exchange_rate_version,
                desired_slot_costs, route_cost_multiplier,
                retry_reserve_multiplier, pool_cost_multiplier,
                billing_floor_multiplier, transition_floor_multiplier,
                pricing_state, config_version
            ) VALUES (
                %s, %s, 'test', %s, %s, 'test-v1', 'test-v1',
                '{}'::jsonb, 1, 0, 1, 1, 1, %s, 'auto-pool-test-v1'
            ) RETURNING id
            """,
            (
                MODEL,
                cost_generation,
                SOURCE_HASH,
                SOURCE_HASH,
                pricing_state,
            ),
        ).fetchone()
        return int(row["id"])


def _routing_generation(
    dsn: str,
    routing_generation: int,
    cost_generation: int,
    cost_generation_id: int,
    *,
    generation_state: str = "active",
) -> int:
    with _connect(dsn) as connection:
        row = connection.execute(
            """
            INSERT INTO a6_trade.provider_pool_generations (
                model, generation, generation_source, source_hash,
                cost_generation_id, cost_generation, desired_size,
                ordered_channel_ids, generation_state, observed_at,
                config_version
            ) VALUES (
                %s, %s, 'test', %s, %s, %s, 2, ARRAY[%s, %s]::bigint[],
                %s, %s, 'auto-pool-test-v1'
            ) RETURNING id
            """,
            (
                MODEL,
                routing_generation,
                SOURCE_HASH,
                cost_generation_id,
                cost_generation,
                1001,
                1002,
                generation_state,
                NOW,
            ),
        ).fetchone()
        return int(row["id"])


def _slot(
    dsn: str,
    generation_id: int,
    routing_generation: int,
    cost_generation: int,
    cost_generation_id: int,
    *,
    slot_no: int,
    binding_id: int,
    account_id: int,
    token_id: int,
    channel_id: int | None,
    slot_state: str,
    multiplier: Decimal | None = None,
    desired_schedulable: bool = False,
    observed_schedulable: bool = False,
    route_mode: str = "fixed_pin",
    pin_confirmed: bool = False,
    probe_confirmed: bool = False,
    confirmed_pin_binding_id: int | None = None,
    desired_priority: int | None = None,
    desired_rank: int | None = None,
    auto_pool_generation_id: int | None = None,
    auto_pool_subscription_id: int | None = None,
    auto_pool_confirmed: bool = False,
    auto_pool_max_ratio_cny: Decimal | None = None,
    pin_assignment_epoch_id: int | None = None,
) -> None:
    with _connect(dsn) as connection:
        connection.execute(
            """
            INSERT INTO a6_trade.provider_pool_slots (
                generation_id, model, routing_generation, slot_no, slot_name,
                cost_generation_id, cost_generation, enodot_binding_id,
                enodot_account_id, a6_token_id, desired_channel_id,
                desired_rank, desired_priority, desired_slot_cost_multiplier,
                observed_rate_multiplier, liquidity_state, funding_eligible,
                slot_state, desired_schedulable, observed_schedulable,
                confirmed_pin_binding_id, pin_confirmed, probe_confirmed,
                state_version, pin_assignment_epoch_id, route_mode,
                auto_pool_generation_id, auto_pool_subscription_id,
                auto_pool_confirmed, auto_pool_max_ratio_cny
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                'FUNDED', true, %s, %s, %s, %s, %s, %s, 1, %s, %s, %s, %s,
                %s, %s
            )
            """,
            (
                generation_id,
                MODEL,
                routing_generation,
                slot_no,
                f"a6-provider-{slot_no}",
                cost_generation_id,
                cost_generation,
                binding_id,
                account_id,
                token_id,
                channel_id,
                desired_rank,
                desired_priority,
                multiplier,
                multiplier,
                slot_state,
                desired_schedulable,
                observed_schedulable,
                confirmed_pin_binding_id,
                pin_confirmed,
                probe_confirmed,
                pin_assignment_epoch_id,
                route_mode,
                auto_pool_generation_id,
                auto_pool_subscription_id,
                auto_pool_confirmed,
                auto_pool_max_ratio_cny,
            ),
        )


def _auto_generation(
    dsn: str,
    *,
    auto_pool_generation: int,
    routing_generation: int,
    cost_generation: int,
    generation_state: str = "applying",
    source_hash: str = SOURCE_HASH,
    max_ratio_cny: str = "0.2",
    max_ratio_usd: str = "0.18",
) -> int:
    with _connect(dsn) as connection:
        row = connection.execute(
            """
            INSERT INTO a6_trade.provider_auto_pool_generations (
                model, auto_pool_generation, routing_generation,
                cost_generation, source_hash, observer_scan_ids,
                selected_channel_ids, max_ratio_cny, max_ratio_usd,
                target_channel_count, minimum_observers, hard_max_ratio_cny,
                trigger_reason, generation_state, observed_at, config_version,
                plan_payload, activated_at
            ) VALUES (
                %s, %s, %s, %s, %s, ARRAY[1]::bigint[], ARRAY[1001]::bigint[],
                %s, %s, 2, 2, '0.3', 'test', %s, %s, 'auto-pool-test-v1',
                '{}'::jsonb,
                CASE WHEN %s = 'active' THEN %s ELSE NULL END
            ) RETURNING id
            """,
            (
                MODEL,
                auto_pool_generation,
                routing_generation,
                cost_generation,
                source_hash,
                max_ratio_cny,
                max_ratio_usd,
                generation_state,
                NOW,
                generation_state,
                NOW,
            ),
        ).fetchone()
        return int(row["id"])


def _auto_account(
    dsn: str,
    generation_id: int,
    merchant_id: int,
    token_id: int,
    account_id: int,
    *,
    slot_no: int,
    account_state: str = "pending",
    subscription_id: int | None = None,
    ban_confirmed: bool = False,
    unpin_confirmed: bool = False,
    auto_pool_confirmed: bool = False,
    cost_confirmed: bool = False,
    schedulable_confirmed: bool = False,
    observed_max_ratio_cny: str | None = None,
    observed_max_ratio_usd: str | None = None,
    readback_hash: str | None = None,
    completed_at: datetime | None = None,
) -> None:
    with _connect(dsn) as connection:
        connection.execute(
            """
            INSERT INTO a6_trade.provider_auto_pool_accounts (
                generation_id, merchant_id, token_ref_id, external_token_id,
                enodot_account_id, slot_no, subscription_id, account_state,
                visible_owned_channel_ids, disabled_owned_channel_ids,
                absent_owned_channel_ids, ban_confirmed, unpin_confirmed,
                auto_pool_confirmed, cost_confirmed, schedulable_confirmed,
                observed_max_ratio_cny, observed_max_ratio_usd, readback_hash,
                completed_at, remote_readback
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, '{}', '{}', '{}', %s, %s,
                %s, %s, %s, %s, %s, %s, %s, '{}'::jsonb
            )
            """,
            (
                generation_id,
                merchant_id,
                token_id,
                token_id,
                account_id,
                slot_no,
                subscription_id,
                account_state,
                ban_confirmed,
                unpin_confirmed,
                auto_pool_confirmed,
                cost_confirmed,
                schedulable_confirmed,
                observed_max_ratio_cny,
                observed_max_ratio_usd,
                readback_hash,
                completed_at,
            ),
        )


def _account(
    merchant_id: int,
    token_id: int,
    account_id: int,
    slot_no: int,
    routing_generation: int,
    cost_generation: int,
) -> ProviderAutoPoolAccount:
    return ProviderAutoPoolAccount(
        merchant_id=merchant_id,
        a6_user_id=merchant_id * 10,
        session_ref=f"/var/lib/a6-trader/secrets/sessions/session-{merchant_id}",
        token_ref_id=token_id,
        external_token_id=token_id,
        enodot_account_id=account_id,
        slot_no=slot_no,
        routing_generation=routing_generation,
        cost_generation=cost_generation,
    )


def _plan(
    *,
    max_ratio: str = "0.2",
    max_ratio_usd: str = "0.18",
    source_hash: str = SOURCE_HASH,
) -> AutoPoolPlan:
    candidate = AutoPoolCandidate(
        channel_id=1001,
        supplier_id=2001,
        listing_id=3001,
        ratio_cny=Decimal("0.18"),
        ratio_usd=Decimal("0.16"),
        observer_ids=(1, 2),
        last_success_at=None,
        latency_ms=None,
    )
    return AutoPoolPlan(
        model=MODEL,
        min_ratio=Decimal("0.1"),
        max_ratio=Decimal(max_ratio),
        max_ratio_usd=Decimal(max_ratio_usd),
        selected=(candidate,),
        eligible=(candidate,),
        excluded=(),
        source_hash=source_hash,
    )


def _seed_active_routing(
    dsn: str,
    routing_generation: int = 1,
    cost_generation: int = 1,
) -> tuple[int, int]:
    cost_id = _cost_generation(dsn, cost_generation)
    routing_id = _routing_generation(
        dsn, routing_generation, cost_generation, cost_id
    )
    return routing_id, cost_id


def _seed_provider_pair(dsn: str, slot_no: int = 1) -> tuple[int, int, int]:
    merchant_id = slot_no
    token_id = slot_no * 100
    account_id = slot_no * 1000
    binding_id = slot_no * 10_000
    _merchant(dsn, merchant_id, user_id=merchant_id * 10)
    _token(dsn, token_id, merchant_id)
    _binding(dsn, binding_id, token_id, account_id)
    return merchant_id, token_id, account_id


def _seed_ready_fixed_slot(
    dsn: str,
    *,
    routing_generation: int = 1,
    cost_generation: int = 1,
    slot_no: int = 1,
) -> tuple[int, int, int]:
    merchant_id, token_id, account_id = _seed_provider_pair(dsn, slot_no)
    _inventory(dsn, merchant_id, token_id, slot_no * 10_000, account_id)
    _pin_binding(dsn, 9000 + slot_no, token_id, 1000 + slot_no)
    routing_id, cost_id = _seed_active_routing(dsn, routing_generation, cost_generation)
    _slot(
        dsn,
        routing_id,
        routing_generation,
        cost_generation,
        cost_id,
        slot_no=slot_no,
        binding_id=slot_no * 10_000,
        account_id=account_id,
        token_id=token_id,
        channel_id=1000 + slot_no,
        slot_state="READY",
        multiplier=Decimal("0.9"),
        desired_schedulable=True,
        observed_schedulable=True,
        desired_priority=1,
        desired_rank=1,
        pin_confirmed=True,
        probe_confirmed=True,
        confirmed_pin_binding_id=9000 + slot_no,
    )
    return merchant_id, token_id, account_id


def test_migration_chain_applies_through_0069_and_removes_legacy_pin_contract(
    auto_pool_postgres: str,
) -> None:
    with _connect(auto_pool_postgres) as connection:
        constraints = {
            str(row["conname"])
            for row in connection.execute(
                "SELECT conname FROM pg_constraint"
            ).fetchall()
        }
        assert "provider_pool_slot_ready_pin_required" not in constraints
        assert "provider_pool_slot_ready_contract" in constraints
        assert "provider_pool_slot_auto_pool_shape" in constraints
        assert "provider_auto_pool_account_claim_shape" in constraints
        assert "provider_auto_pool_account_ready_contract" in constraints
        assert "provider_auto_pool_generation_parent_fk" in constraints
        assert "provider_pool_generation_model_routing_cost_uq" in constraints
        assert "provider_membership_repair_completion_consistent" in constraints
        ledger = connection.execute(
            "SELECT version FROM a6_trade.schema_migrations ORDER BY version DESC LIMIT 1"
        ).fetchone()
        assert ledger["version"] == "0069"


def test_fixed_pin_and_auto_pool_ready_shapes_satisfy_slot_contract(
    database: str,
) -> None:
    routing_id, cost_id = _seed_active_routing(database)
    merchant_id, token_id, account_id = _seed_provider_pair(database, slot_no=1)
    _inventory(database, merchant_id, token_id, 10_000, account_id)
    _pin_binding(database, 9001, token_id, 1001)
    _slot(
        database,
        routing_id,
        1,
        1,
        cost_id,
        slot_no=1,
        binding_id=10_000,
        account_id=account_id,
        token_id=token_id,
        channel_id=1001,
        slot_state="READY",
        multiplier=Decimal("0.9"),
        desired_schedulable=True,
        observed_schedulable=True,
        desired_priority=1,
        desired_rank=1,
        pin_confirmed=True,
        probe_confirmed=True,
        confirmed_pin_binding_id=9001,
    )
    merchant_id, token_id, account_id = _seed_provider_pair(database, slot_no=2)
    _inventory(database, merchant_id, token_id, 20_000, account_id)
    auto_id = _auto_generation(
        database,
        auto_pool_generation=1,
        routing_generation=1,
        cost_generation=1,
        generation_state="active",
    )
    _auto_account(
        database,
        auto_id,
        merchant_id,
        token_id,
        account_id,
        slot_no=2,
        account_state="ready",
        subscription_id=5000,
        ban_confirmed=True,
        unpin_confirmed=True,
        auto_pool_confirmed=True,
        cost_confirmed=True,
        schedulable_confirmed=True,
        observed_max_ratio_cny="0.2",
        observed_max_ratio_usd="0.18",
        readback_hash="b" * 64,
        completed_at=NOW,
    )
    _slot(
        database,
        routing_id,
        1,
        1,
        cost_id,
        slot_no=2,
        binding_id=20_000,
        account_id=account_id,
        token_id=token_id,
        channel_id=None,
        slot_state="READY",
        multiplier=Decimal("0.18"),
        desired_schedulable=True,
        observed_schedulable=True,
        desired_priority=1,
        desired_rank=1,
        route_mode="auto_pool",
        auto_pool_generation_id=auto_id,
        auto_pool_subscription_id=5000,
        auto_pool_confirmed=True,
        auto_pool_max_ratio_cny=Decimal("0.2"),
    )
    with _connect(database) as connection:
        rows = connection.execute(
            "SELECT slot_no, slot_state, route_mode FROM a6_trade.provider_pool_slots"
        ).fetchall()
        assert {(int(row["slot_no"]), row["slot_state"], row["route_mode"]) for row in rows} == {
            (1, "READY", "fixed_pin"),
            (2, "READY", "auto_pool"),
        }


def test_begin_generation_creates_applying_then_resumes_inflight_and_reuses_active(
    database: str,
) -> None:
    for slot_no in (1, 2):
        merchant_id, token_id, account_id = _seed_provider_pair(database, slot_no)
        _inventory(database, merchant_id, token_id, slot_no * 10_000, account_id)
    routing_id, cost_id = _seed_active_routing(database)
    for slot_no in (1, 2):
        _slot(
            database,
            routing_id,
            1,
            1,
            cost_id,
            slot_no=slot_no,
            binding_id=slot_no * 10_000,
            account_id=slot_no * 1000,
            token_id=slot_no * 100,
            channel_id=None,
            slot_state="PROVISIONING",
            multiplier=None,
            desired_rank=1,
            desired_priority=1,
        )
    store = _store(database)
    accounts = (
        _account(1, 100, 1000, 1, routing_generation=1, cost_generation=1),
        _account(2, 200, 2000, 2, routing_generation=1, cost_generation=1),
    )
    plan = _plan()

    first = store.begin_generation(
        plan,
        accounts=accounts,
        observer_scan_ids=(1, 2),
        observed_at=NOW,
        trigger_reason="observer_success",
        config_version="auto-pool-test-v1",
        source_hash=SOURCE_HASH,
        minimum_observers=2,
        target_channel_count=2,
        hard_max_ratio_cny=Decimal("0.3"),
    )
    assert not first.reused_active and not first.reused_inflight
    assert first.generation == 1

    resumed = store.begin_generation(
        plan,
        accounts=accounts,
        observer_scan_ids=(1, 2),
        observed_at=NOW,
        trigger_reason="observer_success",
        config_version="auto-pool-test-v1",
        source_hash=SOURCE_HASH,
        minimum_observers=2,
        target_channel_count=2,
        hard_max_ratio_cny=Decimal("0.3"),
    )
    assert resumed.generation_id == first.generation_id
    assert resumed.reused_inflight
    with _connect(database) as connection:
        rows = connection.execute(
            "SELECT generation_state FROM a6_trade.provider_auto_pool_generations"
        ).fetchall()
        assert [row["generation_state"] for row in rows] == ["applying"]


def test_active_generation_refresh_keeps_single_active_row(database: str) -> None:
    for slot_no in (1, 2):
        merchant_id, token_id, account_id = _seed_provider_pair(database, slot_no)
        _inventory(database, merchant_id, token_id, slot_no * 10_000, account_id)
    routing_id, cost_id = _seed_active_routing(database)
    for slot_no in (1, 2):
        _slot(
            database,
            routing_id,
            1,
            1,
            cost_id,
            slot_no=slot_no,
            binding_id=slot_no * 10_000,
            account_id=slot_no * 1000,
            token_id=slot_no * 100,
            channel_id=None,
            slot_state="PROVISIONING",
            multiplier=None,
            desired_rank=1,
            desired_priority=1,
        )
    store = _store(database)
    accounts = (
        _account(1, 100, 1000, 1, routing_generation=1, cost_generation=1),
        _account(2, 200, 2000, 2, routing_generation=1, cost_generation=1),
    )
    plan = _plan()
    generation = store.begin_generation(
        plan,
        accounts=accounts,
        observer_scan_ids=(1, 2),
        observed_at=NOW,
        trigger_reason="observer_success",
        config_version="auto-pool-test-v1",
        source_hash=SOURCE_HASH,
        minimum_observers=2,
        target_channel_count=2,
        hard_max_ratio_cny=Decimal("0.3"),
    )
    store.claim_accounts(
        generation,
        accounts,
        claim_token="token-active",
        claimed_by="worker-1",
    )
    for account in accounts:
        store.record_prepared(
            generation,
            account,
            claim_token="token-active",
            subscription_id=5000 + account.slot_no,
            visible_owned_channel_ids=(),
            disabled_owned_channel_ids=(),
            max_ratio_cny=Decimal("0.2"),
            max_ratio_usd=Decimal("0.18"),
            readback={"ok": True},
        )
        store.record_ready(generation, account, claim_token="token-active")
    store.activate(MODEL, generation, minimum_ready=2)

    refreshed = store.begin_generation(
        plan,
        accounts=accounts,
        observer_scan_ids=(1, 2),
        observed_at=NOW,
        trigger_reason="observer_success",
        config_version="auto-pool-test-v1",
        source_hash=SOURCE_HASH,
        minimum_observers=2,
        target_channel_count=2,
        hard_max_ratio_cny=Decimal("0.3"),
    )
    assert refreshed.reused_active
    with _connect(database) as connection:
        rows = connection.execute(
            """
            SELECT generation_state, count(*) AS total
            FROM a6_trade.provider_auto_pool_generations
            GROUP BY generation_state
            """
        ).fetchall()
        states = {row["generation_state"]: int(row["total"]) for row in rows}
        assert states["active"] == 1
        assert "applying" not in states


def test_claim_busy_and_wrong_token_cas_failures(database: str) -> None:
    for slot_no in (1, 2):
        merchant_id, token_id, account_id = _seed_provider_pair(database, slot_no)
        _inventory(database, merchant_id, token_id, slot_no * 10_000, account_id)
    routing_id, cost_id = _seed_active_routing(database)
    for slot_no in (1, 2):
        _slot(
            database,
            routing_id,
            1,
            1,
            cost_id,
            slot_no=slot_no,
            binding_id=slot_no * 10_000,
            account_id=slot_no * 1000,
            token_id=slot_no * 100,
            channel_id=None,
            slot_state="PROVISIONING",
            multiplier=None,
            desired_rank=1,
            desired_priority=1,
        )
    store = _store(database)
    accounts = (
        _account(1, 100, 1000, 1, routing_generation=1, cost_generation=1),
        _account(2, 200, 2000, 2, routing_generation=1, cost_generation=1),
    )
    plan = _plan()
    generation = store.begin_generation(
        plan,
        accounts=accounts,
        observer_scan_ids=(1, 2),
        observed_at=NOW,
        trigger_reason="observer_success",
        config_version="auto-pool-test-v1",
        source_hash=SOURCE_HASH,
        minimum_observers=2,
        target_channel_count=2,
        hard_max_ratio_cny=Decimal("0.3"),
    )
    store.claim_accounts(
        generation,
        accounts,
        claim_token="owner-token",
        claimed_by="worker-a",
    )
    with pytest.raises(ProviderAutoPoolError, match="provider_auto_pool_account_claim_busy"):
        store.claim_accounts(
            generation,
            accounts,
            claim_token="other-token",
            claimed_by="worker-b",
        )
    store.claim_accounts(
        generation,
        accounts,
        claim_token="owner-token",
        claimed_by="worker-a",
    )
    with pytest.raises(
        ProviderAutoPoolError, match="provider_auto_pool_account_prepare_conflict"
    ):
        store.record_prepared(
            generation,
            accounts[0],
            claim_token="wrong-token",
            subscription_id=5001,
            visible_owned_channel_ids=(),
            disabled_owned_channel_ids=(),
            max_ratio_cny=Decimal("0.2"),
            max_ratio_usd=Decimal("0.18"),
            readback={"ok": True},
        )
    with pytest.raises(
        ProviderAutoPoolError, match="provider_auto_pool_account_ready_conflict"
    ):
        store.record_ready(generation, accounts[0], claim_token="wrong-token")
    with pytest.raises(
        ProviderAutoPoolError, match="provider_auto_pool_account_failure_conflict"
    ):
        store.record_failed(
            generation,
            accounts[0],
            claim_token="wrong-token",
            error_class="boom",
        )


def test_stale_claim_is_reclaimed_after_timeout(database: str) -> None:
    for slot_no in (1, 2):
        merchant_id, token_id, account_id = _seed_provider_pair(database, slot_no)
        _inventory(database, merchant_id, token_id, slot_no * 10_000, account_id)
    routing_id, cost_id = _seed_active_routing(database)
    for slot_no in (1, 2):
        _slot(
            database,
            routing_id,
            1,
            1,
            cost_id,
            slot_no=slot_no,
            binding_id=slot_no * 10_000,
            account_id=slot_no * 1000,
            token_id=slot_no * 100,
            channel_id=None,
            slot_state="PROVISIONING",
            multiplier=None,
            desired_rank=1,
            desired_priority=1,
        )
    store = _store(database)
    accounts = (
        _account(1, 100, 1000, 1, routing_generation=1, cost_generation=1),
        _account(2, 200, 2000, 2, routing_generation=1, cost_generation=1),
    )
    plan = _plan()
    generation = store.begin_generation(
        plan,
        accounts=accounts,
        observer_scan_ids=(1, 2),
        observed_at=NOW,
        trigger_reason="observer_success",
        config_version="auto-pool-test-v1",
        source_hash=SOURCE_HASH,
        minimum_observers=2,
        target_channel_count=2,
        hard_max_ratio_cny=Decimal("0.3"),
    )
    store.claim_accounts(
        generation,
        accounts,
        claim_token="stale-token",
        claimed_by="dead-worker",
    )
    with _connect(database) as connection:
        connection.execute(
            """
            UPDATE a6_trade.provider_auto_pool_accounts
            SET claimed_at = now() - interval '30 seconds'
            """
        )
    store.claim_accounts(
        generation,
        accounts,
        claim_token="fresh-token",
        claimed_by="live-worker",
    )
    with _connect(database) as connection:
        rows = connection.execute(
            """
            SELECT claim_token, claimed_by FROM a6_trade.provider_auto_pool_accounts
            ORDER BY slot_no
            """
        ).fetchall()
        assert {row["claim_token"] for row in rows} == {"fresh-token"}
        assert {row["claimed_by"] for row in rows} == {"live-worker"}


def test_claim_pause_prepare_ready_activate_projection_chain(database: str) -> None:
    for slot_no in (1, 2):
        merchant_id, token_id, account_id = _seed_provider_pair(database, slot_no)
        _inventory(database, merchant_id, token_id, slot_no * 10_000, account_id)
    routing_id, cost_id = _seed_active_routing(database)
    for slot_no in (1, 2):
        _slot(
            database,
            routing_id,
            1,
            1,
            cost_id,
            slot_no=slot_no,
            binding_id=slot_no * 10_000,
            account_id=slot_no * 1000,
            token_id=slot_no * 100,
            channel_id=None,
            slot_state="PROVISIONING",
            multiplier=None,
            desired_rank=1,
            desired_priority=1,
        )
    store = _store(database)
    accounts = (
        _account(1, 100, 1000, 1, routing_generation=1, cost_generation=1),
        _account(2, 200, 2000, 2, routing_generation=1, cost_generation=1),
    )
    plan = _plan()
    generation = store.begin_generation(
        plan,
        accounts=accounts,
        observer_scan_ids=(1, 2),
        observed_at=NOW,
        trigger_reason="observer_success",
        config_version="auto-pool-test-v1",
        source_hash=SOURCE_HASH,
        minimum_observers=2,
        target_channel_count=2,
        hard_max_ratio_cny=Decimal("0.3"),
    )
    store.claim_accounts(
        generation,
        accounts,
        claim_token="token-1",
        claimed_by="worker-1",
    )
    with _connect(database) as connection:
        rows = connection.execute(
            """
            SELECT slot_no, slot_state, desired_schedulable, observed_schedulable
            FROM a6_trade.provider_pool_slots ORDER BY slot_no
            """
        ).fetchall()
        assert all(
            row["slot_state"] == "REPAIRING"
            and not row["desired_schedulable"]
            and not row["observed_schedulable"]
            for row in rows
        )
        inventory_rows = connection.execute(
            """
            SELECT inventory_state, schedulable
            FROM a6_trade.provider_inventory ORDER BY merchant_id
            """
        ).fetchall()
        assert all(
            row["inventory_state"] == "REPAIRING" and not row["schedulable"]
            for row in inventory_rows
        )
        account_rows = connection.execute(
            """
            SELECT account_state, claim_token, ban_confirmed, unpin_confirmed
            FROM a6_trade.provider_auto_pool_accounts ORDER BY slot_no
            """
        ).fetchall()
        assert all(row["account_state"] == "applying" for row in account_rows)
        assert all(row["claim_token"] == "token-1" for row in account_rows)

    for account in accounts:
        store.record_prepared(
            generation,
            account,
            claim_token="token-1",
            subscription_id=5000 + account.slot_no,
            visible_owned_channel_ids=(900 + account.slot_no,),
            disabled_owned_channel_ids=(700 + account.slot_no,),
            max_ratio_cny=Decimal("0.2"),
            max_ratio_usd=Decimal("0.18"),
            readback={"subscription_id": 5000 + account.slot_no, "ok": True},
        )
    with _connect(database) as connection:
        rows = connection.execute(
            """
            SELECT slot_no, subscription_id, ban_confirmed, unpin_confirmed,
                   auto_pool_confirmed, cost_confirmed, schedulable_confirmed,
                   observed_max_ratio_cny, observed_max_ratio_usd,
                   readback_hash, account_state
            FROM a6_trade.provider_auto_pool_accounts ORDER BY slot_no
            """
        ).fetchall()
    for row in rows:
            assert int(row["subscription_id"]) == 5000 + row["slot_no"]
            assert row["ban_confirmed"] and row["unpin_confirmed"]
            assert row["auto_pool_confirmed"] and row["cost_confirmed"]
            assert not row["schedulable_confirmed"]
            assert row["observed_max_ratio_cny"] is not None
            assert row["observed_max_ratio_usd"] is not None
            assert row["readback_hash"] is not None
            assert row["account_state"] == "applying"

    for account in accounts:
        store.record_ready(generation, account, claim_token="token-1")
    with _connect(database) as connection:
        rows = connection.execute(
            """
            SELECT account_state, schedulable_confirmed, completed_at
            FROM a6_trade.provider_auto_pool_accounts ORDER BY slot_no
            """
        ).fetchall()
        assert all(
            row["account_state"] == "ready"
            and row["schedulable_confirmed"]
            and row["completed_at"] is not None
            for row in rows
        )

    store.activate(MODEL, generation, minimum_ready=2)
    with _connect(database) as connection:
        rows = connection.execute(
            """
            SELECT slot_no, slot_state, route_mode, desired_schedulable,
                   observed_schedulable, desired_channel_id,
                   confirmed_pin_binding_id, pin_confirmed, auto_pool_confirmed,
                   auto_pool_subscription_id, auto_pool_max_ratio_cny,
                   observed_rate_multiplier, desired_slot_cost_multiplier
            FROM a6_trade.provider_pool_slots ORDER BY slot_no
            """
        ).fetchall()
        assert len(rows) == 2
        for row in rows:
            assert row["slot_state"] == "READY"
            assert row["route_mode"] == "auto_pool"
            assert row["desired_schedulable"] and row["observed_schedulable"]
            assert row["desired_channel_id"] is None
            assert row["confirmed_pin_binding_id"] is None
            assert not row["pin_confirmed"]
            assert row["auto_pool_confirmed"]
            assert int(row["auto_pool_subscription_id"]) == 5000 + row["slot_no"]
            assert row["auto_pool_max_ratio_cny"] is not None
            assert row["observed_rate_multiplier"] == row["desired_slot_cost_multiplier"]
            assert Decimal(str(row["desired_slot_cost_multiplier"])) == Decimal("0.18")
        inventory_rows = connection.execute(
            """
            SELECT inventory_state, schedulable, assigned_slot_no
            FROM a6_trade.provider_inventory ORDER BY merchant_id
            """
        ).fetchall()
        assert all(
            row["inventory_state"] == "READY" and row["schedulable"]
            for row in inventory_rows
        )
        generation_rows = connection.execute(
            "SELECT generation_state, activated_at FROM a6_trade.provider_auto_pool_generations"
        ).fetchall()
        assert len(generation_rows) == 1
        assert generation_rows[0]["generation_state"] == "active"
        assert generation_rows[0]["activated_at"] is not None
        account_rows = connection.execute(
            """
            SELECT account_state, claim_token, claimed_by, claimed_at
            FROM a6_trade.provider_auto_pool_accounts ORDER BY slot_no
            """
        ).fetchall()
        assert all(
            row["account_state"] == "ready"
            and row["claim_token"] is None
            and row["claimed_by"] is None
            and row["claimed_at"] is None
            for row in account_rows
        )


def test_record_failed_and_fail_generation_projections(database: str) -> None:
    for slot_no in (1, 2):
        merchant_id, token_id, account_id = _seed_provider_pair(database, slot_no)
        _inventory(database, merchant_id, token_id, slot_no * 10_000, account_id)
    routing_id, cost_id = _seed_active_routing(database)
    for slot_no in (1, 2):
        _slot(
            database,
            routing_id,
            1,
            1,
            cost_id,
            slot_no=slot_no,
            binding_id=slot_no * 10_000,
            account_id=slot_no * 1000,
            token_id=slot_no * 100,
            channel_id=None,
            slot_state="PROVISIONING",
            multiplier=None,
            desired_rank=1,
            desired_priority=1,
        )
    store = _store(database)
    accounts = (
        _account(1, 100, 1000, 1, routing_generation=1, cost_generation=1),
        _account(2, 200, 2000, 2, routing_generation=1, cost_generation=1),
    )
    plan = _plan()
    generation = store.begin_generation(
        plan,
        accounts=accounts,
        observer_scan_ids=(1, 2),
        observed_at=NOW,
        trigger_reason="observer_success",
        config_version="auto-pool-test-v1",
        source_hash=SOURCE_HASH,
        minimum_observers=2,
        target_channel_count=2,
        hard_max_ratio_cny=Decimal("0.3"),
    )
    store.claim_accounts(
        generation,
        accounts,
        claim_token="token-f",
        claimed_by="worker-f",
    )
    store.record_failed(
        generation,
        accounts[0],
        claim_token="token-f",
        error_class="remote_timeout",
    )
    with _connect(database) as connection:
        failed = connection.execute(
            """
            SELECT a.slot_no, a.account_state, a.error_class, a.completed_at,
                   s.slot_state, s.desired_schedulable, i.inventory_state, i.schedulable
            FROM a6_trade.provider_auto_pool_accounts a
            JOIN a6_trade.provider_pool_slots s
              ON s.model = %s AND s.slot_no = a.slot_no
              AND s.routing_generation = 1 AND s.valid_to IS NULL
            JOIN a6_trade.provider_inventory i
              ON i.model = %s AND i.merchant_id = a.merchant_id
            WHERE a.generation_id = %s AND a.slot_no = 1
            """,
            (MODEL, MODEL, generation.generation_id),
        ).fetchone()
        assert failed["account_state"] == "failed"
        assert failed["error_class"] == "remote_timeout"
        assert failed["completed_at"] is not None
        assert failed["slot_state"] == "REPAIRING"
        assert not failed["desired_schedulable"]
        assert failed["inventory_state"] == "REPAIRING"
        assert not failed["schedulable"]

    store.fail_generation(generation, error_class="minimum_ready_missed")
    with _connect(database) as connection:
        generation_row = connection.execute(
            """
            SELECT generation_state, completed_at
            FROM a6_trade.provider_auto_pool_generations WHERE id = %s
            """,
            (generation.generation_id,),
        ).fetchone()
        assert generation_row["generation_state"] == "failed"
        assert generation_row["completed_at"] is not None
        account_rows = connection.execute(
            """
            SELECT account_state, claim_token FROM a6_trade.provider_auto_pool_accounts
            ORDER BY slot_no
            """
        ).fetchall()
        assert all(row["account_state"] == "failed" for row in account_rows)
        assert all(row["claim_token"] is None for row in account_rows)


def test_auto_pool_full_chain_requires_zero_pin_data(database: str) -> None:
    for slot_no in (1, 2):
        merchant_id, token_id, account_id = _seed_provider_pair(database, slot_no)
        _inventory(database, merchant_id, token_id, slot_no * 10_000, account_id)
    routing_id, cost_id = _seed_active_routing(database)
    for slot_no in (1, 2):
        _slot(
            database,
            routing_id,
            1,
            1,
            cost_id,
            slot_no=slot_no,
            binding_id=slot_no * 10_000,
            account_id=slot_no * 1000,
            token_id=slot_no * 100,
            channel_id=None,
            slot_state="PROVISIONING",
            multiplier=None,
            desired_rank=1,
            desired_priority=1,
        )
    with _connect(database) as connection:
        assert connection.execute(
            "SELECT count(*) FROM a6_trade.a6_pin_bindings"
        ).fetchone()["count"] == 0
    store = _store(database)
    accounts = (
        _account(1, 100, 1000, 1, routing_generation=1, cost_generation=1),
        _account(2, 200, 2000, 2, routing_generation=1, cost_generation=1),
    )
    plan = _plan()
    generation = store.begin_generation(
        plan,
        accounts=accounts,
        observer_scan_ids=(1, 2),
        observed_at=NOW,
        trigger_reason="observer_success",
        config_version="auto-pool-test-v1",
        source_hash=SOURCE_HASH,
        minimum_observers=2,
        target_channel_count=2,
        hard_max_ratio_cny=Decimal("0.3"),
    )
    store.claim_accounts(
        generation,
        accounts,
        claim_token="token-nopin",
        claimed_by="worker-1",
    )
    for account in accounts:
        store.record_prepared(
            generation,
            account,
            claim_token="token-nopin",
            subscription_id=5000 + account.slot_no,
            visible_owned_channel_ids=(900 + account.slot_no,),
            disabled_owned_channel_ids=(700 + account.slot_no,),
            max_ratio_cny=Decimal("0.2"),
            max_ratio_usd=Decimal("0.18"),
            readback={"ok": True},
        )
        store.record_ready(generation, account, claim_token="token-nopin")
    store.activate(MODEL, generation, minimum_ready=2)
    with _connect(database) as connection:
        assert connection.execute(
            "SELECT count(*) FROM a6_trade.a6_pin_bindings"
        ).fetchone()["count"] == 0
        rows = connection.execute(
            """
            SELECT slot_state, route_mode, confirmed_pin_binding_id, pin_confirmed
            FROM a6_trade.provider_pool_slots ORDER BY slot_no
            """
        ).fetchall()
        assert all(
            row["slot_state"] == "READY"
            and row["route_mode"] == "auto_pool"
            and row["confirmed_pin_binding_id"] is None
            and not row["pin_confirmed"]
            for row in rows
        )


def test_record_prepared_rejects_cost_mismatching_generation_ratio(
    database: str,
) -> None:
    for slot_no in (1, 2):
        merchant_id, token_id, account_id = _seed_provider_pair(database, slot_no)
        _inventory(database, merchant_id, token_id, slot_no * 10_000, account_id)
    routing_id, cost_id = _seed_active_routing(database)
    for slot_no in (1, 2):
        _slot(
            database,
            routing_id,
            1,
            1,
            cost_id,
            slot_no=slot_no,
            binding_id=slot_no * 10_000,
            account_id=slot_no * 1000,
            token_id=slot_no * 100,
            channel_id=None,
            slot_state="PROVISIONING",
            multiplier=None,
            desired_rank=1,
            desired_priority=1,
        )
    store = _store(database)
    accounts = (
        _account(1, 100, 1000, 1, routing_generation=1, cost_generation=1),
        _account(2, 200, 2000, 2, routing_generation=1, cost_generation=1),
    )
    plan = _plan()
    generation = store.begin_generation(
        plan,
        accounts=accounts,
        observer_scan_ids=(1, 2),
        observed_at=NOW,
        trigger_reason="observer_success",
        config_version="auto-pool-test-v1",
        source_hash=SOURCE_HASH,
        minimum_observers=2,
        target_channel_count=2,
        hard_max_ratio_cny=Decimal("0.3"),
    )
    store.claim_accounts(
        generation,
        accounts,
        claim_token="token-ratio",
        claimed_by="worker-1",
    )
    with pytest.raises(
        ProviderAutoPoolError,
        match="provider_auto_pool_account_cost_generation_mismatch",
    ):
        store.record_prepared(
            generation,
            accounts[0],
            claim_token="token-ratio",
            subscription_id=5001,
            visible_owned_channel_ids=(),
            disabled_owned_channel_ids=(),
            max_ratio_cny=Decimal("0.25"),
            max_ratio_usd=Decimal("0.23"),
            readback={"ok": True},
        )
    with _connect(database) as connection:
        rows = connection.execute(
            """
            SELECT account_state, subscription_id, cost_confirmed
            FROM a6_trade.provider_auto_pool_accounts ORDER BY slot_no
            """
        ).fetchall()
        assert all(
            row["account_state"] == "applying"
            and row["subscription_id"] is None
            and not row["cost_confirmed"]
            for row in rows
        )
    store.record_prepared(
        generation,
        accounts[0],
        claim_token="token-ratio",
        subscription_id=5001,
        visible_owned_channel_ids=(),
        disabled_owned_channel_ids=(),
        max_ratio_cny=Decimal("0.2"),
        max_ratio_usd=Decimal("0.18"),
        readback={"ok": True},
    )


def test_membership_repair_completion_cas_against_database(database: str) -> None:
    outgoing = (15, 150, 1500)
    _merchant(database, outgoing[0])
    _token(database, outgoing[2], outgoing[0], status="retired")
    merchant_id, token_id, account_id = 16, 1600, 160
    _merchant(database, merchant_id)
    _token(database, token_id, merchant_id)
    _binding(database, 16_000, token_id, account_id)
    _inventory(
        database,
        merchant_id,
        token_id,
        16_000,
        account_id,
        inventory_state="REPAIRING",
        schedulable=False,
    )
    cost_id = _cost_generation(database, 101)
    routing_id = _routing_generation(
        database, 101, 101, cost_id, generation_state="active"
    )
    auto_id = _auto_generation(
        database,
        auto_pool_generation=1,
        routing_generation=101,
        cost_generation=101,
        generation_state="active",
    )
    _auto_account(
        database,
        auto_id,
        merchant_id,
        token_id,
        account_id,
        slot_no=4,
        account_state="ready",
        subscription_id=7000,
        ban_confirmed=True,
        unpin_confirmed=True,
        auto_pool_confirmed=True,
        cost_confirmed=True,
        schedulable_confirmed=True,
        observed_max_ratio_cny="0.2",
        observed_max_ratio_usd="0.18",
        readback_hash="c" * 64,
        completed_at=NOW,
    )
    _slot(
        database,
        routing_id,
        101,
        101,
        cost_id,
        slot_no=4,
        binding_id=16_000,
        account_id=account_id,
        token_id=token_id,
        channel_id=None,
        slot_state="READY",
        multiplier=Decimal("0.18"),
        desired_schedulable=True,
        observed_schedulable=True,
        desired_priority=1,
        desired_rank=1,
        route_mode="auto_pool",
        auto_pool_generation_id=auto_id,
        auto_pool_subscription_id=7000,
        auto_pool_confirmed=True,
        auto_pool_max_ratio_cny=Decimal("0.2"),
    )
    with _connect(database) as connection:
        connection.execute(
            """
            INSERT INTO a6_trade.provider_membership_repair_requests (
                model, source_routing_generation, source_cost_generation,
                slot_no, outgoing_merchant_id, outgoing_enodot_account_id,
                outgoing_a6_token_id, reason_code, status, attempt_count
            ) VALUES (%s, 100, 100, 4, %s, %s, %s,
                      'provider_funding_contract_failed', 'pending', 0)
            """,
            (MODEL, *outgoing),
        )
    repair_store = PostgresProviderMembershipRepairStore(_connection_factory(database))
    claim = repair_store.claim(worker_id="worker-1")
    assert claim is not None
    assert claim.model == MODEL
    assert claim.slot_no == 4

    state = repair_store.observe(claim)
    assert state.routing_generation == 101
    assert state.auto_pool_ready
    assert state.replacement_identity == (merchant_id, account_id, token_id)

    repair_store.complete(claim, state=state, result={"action": "replacement_active"})
    with _connect(database) as connection:
        row = connection.execute(
            """
            SELECT status, claim_token, target_routing_generation,
                   target_cost_generation, replacement_merchant_id,
                   replacement_enodot_account_id, replacement_a6_token_id,
                   completed_at
            FROM a6_trade.provider_membership_repair_requests WHERE id = %s
            """,
            (claim.request_id,),
        ).fetchone()
        assert row["status"] == "completed"
        assert row["claim_token"] is None
        assert int(row["target_routing_generation"]) == 101
        assert int(row["target_cost_generation"]) == 101
        assert int(row["replacement_merchant_id"]) == merchant_id
        assert int(row["replacement_enodot_account_id"]) == account_id
        assert int(row["replacement_a6_token_id"]) == token_id
        assert row["completed_at"] is not None


def test_membership_complete_rejects_stale_claim_token(database: str) -> None:
    _merchant(database, 15)
    _token(database, 1500, 15, status="retired")
    merchant_id, token_id, account_id = 16, 1600, 160
    _merchant(database, merchant_id)
    _token(database, token_id, merchant_id)
    _binding(database, 16_000, token_id, account_id)
    _inventory(
        database,
        merchant_id,
        token_id,
        16_000,
        account_id,
        inventory_state="REPAIRING",
        schedulable=False,
    )
    cost_id = _cost_generation(database, 101)
    routing_id = _routing_generation(
        database, 101, 101, cost_id, generation_state="active"
    )
    auto_id = _auto_generation(
        database,
        auto_pool_generation=1,
        routing_generation=101,
        cost_generation=101,
        generation_state="active",
    )
    _auto_account(
        database,
        auto_id,
        merchant_id,
        token_id,
        account_id,
        slot_no=4,
        account_state="ready",
        subscription_id=7000,
        ban_confirmed=True,
        unpin_confirmed=True,
        auto_pool_confirmed=True,
        cost_confirmed=True,
        schedulable_confirmed=True,
        observed_max_ratio_cny="0.2",
        observed_max_ratio_usd="0.18",
        readback_hash="c" * 64,
        completed_at=NOW,
    )
    _slot(
        database,
        routing_id,
        101,
        101,
        cost_id,
        slot_no=4,
        binding_id=16_000,
        account_id=account_id,
        token_id=token_id,
        channel_id=None,
        slot_state="READY",
        multiplier=Decimal("0.18"),
        desired_schedulable=True,
        observed_schedulable=True,
        desired_priority=1,
        desired_rank=1,
        route_mode="auto_pool",
        auto_pool_generation_id=auto_id,
        auto_pool_subscription_id=7000,
        auto_pool_confirmed=True,
        auto_pool_max_ratio_cny=Decimal("0.2"),
    )
    with _connect(database) as connection:
        connection.execute(
            """
            INSERT INTO a6_trade.provider_membership_repair_requests (
                model, source_routing_generation, source_cost_generation,
                slot_no, outgoing_merchant_id, outgoing_enodot_account_id,
                outgoing_a6_token_id, reason_code, status, attempt_count,
                claim_token, claimed_by, claimed_at
            ) VALUES (
                %s, 100, 100, 4, 15, 150, 1500,
                'provider_funding_contract_failed', 'running', 1,
                'real-token', 'worker-1', now()
            )
            """,
            (MODEL,),
        )
    repair_store = PostgresProviderMembershipRepairStore(_connection_factory(database))
    claim = ProviderMembershipRepairClaim(
        request_id=1,
        claim_token="wrong-token",
        model=MODEL,
        source_routing_generation=100,
        source_cost_generation=100,
        slot_no=4,
        outgoing_merchant_id=15,
        outgoing_enodot_account_id=150,
        outgoing_a6_token_id=1500,
        reason_code="provider_funding_contract_failed",
        attempt_count=2,
    )
    state = repair_store.observe(claim)
    with pytest.raises(
        ProviderMembershipRepairError,
        match="provider_membership_repair_complete_cas_mismatch",
    ):
        repair_store.complete(claim, state=state, result={"action": "replacement_active"})


def test_model_lease_serializes_same_model_and_allows_other_models(
    auto_pool_postgres: str,
) -> None:
    lease_a = PostgresProviderModelLease(lambda: _connect(auto_pool_postgres))
    lease_b = PostgresProviderModelLease(lambda: _connect(auto_pool_postgres))
    lease_c = PostgresProviderModelLease(lambda: _connect(auto_pool_postgres))

    with lease_a.hold(MODEL):
        with pytest.raises(
            ProviderPoolReconcileError, match="provider_model_lease_busy"
        ), lease_b.hold(MODEL):
            pytest.fail("second same-model executor must not hold the lease")
        with lease_c.hold("gpt-5.6-other"):
            pass
    with lease_b.hold(MODEL):
        pass


def test_model_lease_released_when_session_closes(auto_pool_postgres: str) -> None:
    def factory() -> psycopg.Connection:
        return _connect(auto_pool_postgres)

    first = factory()
    try:
        with PostgresProviderModelLease(lambda: first).hold(MODEL):
            pass
    finally:
        first.close()
    with PostgresProviderModelLease(factory).hold(MODEL):
        pass


def test_activation_barrier_blocks_until_release(auto_pool_postgres: str) -> None:
    holder = _connect(auto_pool_postgres)
    contender = _connect(auto_pool_postgres)
    try:
        with PostgresProviderActivationBarrier(lambda: holder).hold(MODEL):
            row = contender.execute(
                """
                SELECT pg_try_advisory_lock(%s, hashtext(%s))
                """,
                (0x41365052, MODEL),
            ).fetchone()
            assert row["pg_try_advisory_lock"] is False
        row = contender.execute(
            """
            SELECT pg_try_advisory_lock(%s, hashtext(%s))
            """,
            (0x41365052, MODEL),
        ).fetchone()
        assert row["pg_try_advisory_lock"] is True
        contender.execute(
            """
            SELECT pg_advisory_unlock(%s, hashtext(%s))
            """,
            (0x41365052, MODEL),
        )
    finally:
        holder.close()
        contender.close()


def test_deferred_activate_writes_changed_slot_repairing_without_pin(
    database: str,
) -> None:
    routing_id, cost_id = _seed_active_routing(database)
    merchant_id, token_id, account_id = _seed_provider_pair(database, slot_no=1)
    _inventory(database, merchant_id, token_id, 10_000, account_id)
    merchant_id, token_id, account_id = _seed_provider_pair(database, slot_no=2)
    _inventory(database, merchant_id, token_id, 20_000, account_id)
    _pin_binding(database, 9002, token_id, 1002)
    with _connect(database) as connection:
        epoch_row = connection.execute(
            """
            INSERT INTO a6_trade.provider_pin_assignment_epochs (
                model, slot_no, enodot_account_id, a6_token_id, channel_id,
                started_routing_generation, started_cost_generation
            ) VALUES (%s, 2, 2000, 200, 1002, 1, 1) RETURNING id
            """,
            (MODEL,),
        ).fetchone()
        slot_2_epoch_id = int(epoch_row["id"])
    for slot_no in (1, 2):
        _slot(
            database,
            routing_id,
            1,
            1,
            cost_id,
            slot_no=slot_no,
            binding_id=slot_no * 10_000,
            account_id=slot_no * 1000,
            token_id=slot_no * 100,
            channel_id=1000 + slot_no,
            slot_state="REPAIRING" if slot_no == 1 else "READY",
            multiplier=Decimal("0.9"),
            desired_schedulable=slot_no == 2,
            observed_schedulable=slot_no == 2,
            desired_priority=1,
            desired_rank=1,
            pin_confirmed=slot_no == 2,
            probe_confirmed=slot_no == 2,
            confirmed_pin_binding_id=9002 if slot_no == 2 else None,
            pin_assignment_epoch_id=slot_2_epoch_id if slot_no == 2 else None,
        )
    cost_plan = CostPlan(
        desired_slot_costs={1: Decimal("0.9"), 2: Decimal("0.9")},
        route_cost_multiplier=Decimal("1"),
        retry_reserve_multiplier=Decimal("0"),
        realized_cost_p95_multiplier=None,
        pool_cost_multiplier=Decimal("1"),
        billing_floor_multiplier=Decimal("1"),
        transition_floor_multiplier=Decimal("1"),
        margin_bps=1000,
        headroom_bps=500,
        quantum=Decimal("0.01"),
        cost_output_hash=SOURCE_HASH,
    )

    def slot_for(slot_no: int) -> ProviderSlot:
        return ProviderSlot(
            slot_no=slot_no,
            channel_id=1000 + slot_no,
            rank=1,
            priority=1,
            assignment_id=f"assignment-{slot_no}",
            enodot_binding_id=slot_no * 10_000,
            token_id=slot_no * 100,
            account_id=slot_no * 1000,
            slot_state=SlotState.READY,
            funding_eligible=True,
            desired_schedulable=True,
            observed_schedulable=True,
            desired_cost_multiplier=Decimal("0.9"),
            observed_cost_multiplier=Decimal("0.9"),
            confirmed_pin_binding_id=9002,
            pin_confirmed=True,
            probe_confirmed=True,
        )

    bundle = ProviderBundle(
        model=MODEL,
        routing_generation=2,
        parent_routing_generation=1,
        cost_generation=2,
        parent_cost_generation=1,
        generation_source="test",
        source_hash=SOURCE_HASH,
        ordered_channel_ids=(1001, 1002),
        standby_channel_ids=(),
        desired_size=2,
        slots=(slot_for(1), slot_for(2)),
        cost_plan=cost_plan,
        cost_input_hash=SOURCE_HASH,
        generation_state=GenerationState.ACTIVE,
        pricing_state=PricingState.ACTIVE,
        config_version="auto-pool-test-v1",
    )
    store = PostgresProviderPoolStore(_connection_factory(database))
    store.plan(bundle, expected_routing_generation=1, expected_cost_generation=1)
    store.mark_applying(bundle)
    store.confirm_cost(
        bundle,
        observed_output_hash=bundle.cost_plan.cost_output_hash,
        observed_billing_floor=bundle.cost_plan.transition_floor_multiplier,
    )
    store.activate(bundle, deferred_slot_nos=(1,))
    with _connect(database) as connection:
        rows = connection.execute(
            """
            SELECT slot_no, slot_state, route_mode, desired_schedulable,
                   observed_schedulable, confirmed_pin_binding_id,
                   pin_confirmed, probe_confirmed
            FROM a6_trade.provider_pool_slots
            WHERE valid_to IS NULL
            ORDER BY slot_no
            """
        ).fetchall()

        deferred_row = rows[0]
        assert deferred_row["slot_state"] == "REPAIRING"
        assert not deferred_row["desired_schedulable"]
        assert not deferred_row["observed_schedulable"]
        assert deferred_row["confirmed_pin_binding_id"] is None
        assert not deferred_row["pin_confirmed"]
        assert not deferred_row["probe_confirmed"]
        ready_row = rows[1]
        assert ready_row["slot_state"] == "READY"
        assert ready_row["confirmed_pin_binding_id"] == 9002
        assert ready_row["pin_confirmed"]
        assert connection.execute(
            "SELECT count(*) FROM a6_trade.a6_pin_bindings"
        ).fetchone()["count"] == 1


def test_hold_membership_for_auto_pool_pauses_only_changed_slots(
    database: str,
) -> None:
    routing_id, cost_id = _seed_active_routing(database)
    for slot_no in (1, 2):
        merchant_id, token_id, account_id = _seed_provider_pair(database, slot_no)
        _inventory(database, merchant_id, token_id, slot_no * 10_000, account_id)
        _pin_binding(database, 9000 + slot_no, token_id, 1000 + slot_no)
        _slot(
            database,
            routing_id,
            1,
            1,
            cost_id,
            slot_no=slot_no,
            binding_id=slot_no * 10_000,
            account_id=account_id,
            token_id=token_id,
            channel_id=1000 + slot_no,
            slot_state="READY",
            multiplier=Decimal("0.9"),
            desired_schedulable=True,
            observed_schedulable=True,
            desired_priority=1,
            desired_rank=1,
            pin_confirmed=True,
            probe_confirmed=True,
            confirmed_pin_binding_id=9000 + slot_no,
        )
    cost_plan = CostPlan(
        desired_slot_costs={1: Decimal("0.9"), 2: Decimal("0.9")},
        route_cost_multiplier=Decimal("1"),
        retry_reserve_multiplier=Decimal("0"),
        realized_cost_p95_multiplier=None,
        pool_cost_multiplier=Decimal("1"),
        billing_floor_multiplier=Decimal("1"),
        transition_floor_multiplier=Decimal("1"),
        margin_bps=1000,
        headroom_bps=500,
        quantum=Decimal("0.01"),
        cost_output_hash=SOURCE_HASH,
    )

    def slot_for(slot_no: int) -> ProviderSlot:
        return ProviderSlot(
            slot_no=slot_no,
            channel_id=1000 + slot_no,
            rank=1,
            priority=1,
            assignment_id=f"assignment-{slot_no}",
            enodot_binding_id=slot_no * 10_000,
            token_id=slot_no * 100,
            account_id=slot_no * 1000,
            slot_state=SlotState.READY,
            funding_eligible=True,
            desired_schedulable=True,
            observed_schedulable=True,
            desired_cost_multiplier=Decimal("0.9"),
            observed_cost_multiplier=Decimal("0.9"),
            confirmed_pin_binding_id=9000 + slot_no,
            pin_confirmed=True,
            probe_confirmed=True,
        )

    bundle = ProviderBundle(
        model=MODEL,
        routing_generation=1,
        parent_routing_generation=None,
        cost_generation=1,
        parent_cost_generation=None,
        generation_source="test",
        source_hash=SOURCE_HASH,
        ordered_channel_ids=(1001, 1002),
        standby_channel_ids=(),
        desired_size=2,
        slots=(slot_for(1), slot_for(2)),
        cost_plan=cost_plan,
        cost_input_hash=SOURCE_HASH,
        generation_state=GenerationState.ACTIVE,
        pricing_state=PricingState.ACTIVE,
        config_version="auto-pool-test-v1",
    )
    store = PostgresProviderPoolStore(_connection_factory(database))
    held = store.hold_membership_for_auto_pool(bundle, slot_nos=(1,))
    assert held == 1
    with _connect(database) as connection:
        slot_rows = connection.execute(
            """
            SELECT slot_no, slot_state, desired_schedulable, observed_schedulable,
                   state_version
            FROM a6_trade.provider_pool_slots ORDER BY slot_no
            """
        ).fetchall()
        assert slot_rows[0]["slot_state"] == "REPAIRING"
        assert not slot_rows[0]["desired_schedulable"]
        assert not slot_rows[0]["observed_schedulable"]
        assert int(slot_rows[0]["state_version"]) == 2
        assert slot_rows[1]["slot_state"] == "READY"
        assert slot_rows[1]["desired_schedulable"]
        inventory_rows = connection.execute(
            """
            SELECT merchant_id, inventory_state, schedulable, assigned_slot_no
            FROM a6_trade.provider_inventory ORDER BY merchant_id
            """
        ).fetchall()
        assert inventory_rows[0]["inventory_state"] == "REPAIRING"
        assert not inventory_rows[0]["schedulable"]
        assert int(inventory_rows[0]["assigned_slot_no"]) == 1
        assert inventory_rows[1]["inventory_state"] == "READY"


def test_hold_membership_for_auto_pool_rejects_wrong_generation(
    database: str,
) -> None:
    merchant_id, token_id, account_id = _seed_ready_fixed_slot(database)
    cost_plan = CostPlan(
        desired_slot_costs={1: Decimal("0.9")},
        route_cost_multiplier=Decimal("1"),
        retry_reserve_multiplier=Decimal("0"),
        realized_cost_p95_multiplier=None,
        pool_cost_multiplier=Decimal("1"),
        billing_floor_multiplier=Decimal("1"),
        transition_floor_multiplier=Decimal("1"),
        margin_bps=1000,
        headroom_bps=500,
        quantum=Decimal("0.01"),
        cost_output_hash=SOURCE_HASH,
    )
    slot = ProviderSlot(
        slot_no=1,
        channel_id=1001,
        rank=1,
        priority=1,
        assignment_id="assignment-1",
        enodot_binding_id=10_000,
        token_id=100,
        account_id=1000,
        slot_state=SlotState.READY,
        funding_eligible=True,
        desired_schedulable=True,
        observed_schedulable=True,
        desired_cost_multiplier=Decimal("0.9"),
        observed_cost_multiplier=Decimal("0.9"),
        confirmed_pin_binding_id=9001,
        pin_confirmed=True,
        probe_confirmed=True,
    )
    bundle = ProviderBundle(
        model=MODEL,
        routing_generation=2,
        parent_routing_generation=None,
        cost_generation=2,
        parent_cost_generation=None,
        generation_source="test",
        source_hash=SOURCE_HASH,
        ordered_channel_ids=(1001,),
        standby_channel_ids=(),
        desired_size=1,
        slots=(slot,),
        cost_plan=cost_plan,
        cost_input_hash=SOURCE_HASH,
        generation_state=GenerationState.ACTIVE,
        pricing_state=PricingState.ACTIVE,
        config_version="auto-pool-test-v1",
    )
    store = PostgresProviderPoolStore(_connection_factory(database))
    with pytest.raises(
        ProviderPoolError, match="provider_auto_pool_hold_generation_mismatch"
    ):
        store.hold_membership_for_auto_pool(bundle, slot_nos=(1,))
