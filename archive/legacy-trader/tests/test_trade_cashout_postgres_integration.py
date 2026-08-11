from __future__ import annotations

import hashlib
import os
import shutil
import socket
import subprocess
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from threading import Barrier
from typing import Any

import psycopg
import pytest
from psycopg import sql
from psycopg.rows import dict_row

from a6api_auto.trade_cashout_orchestrator import (
    MICROS_PER_USD,
    A6CryptoCashoutReconciler,
    CashoutError,
    CashoutFinancialSnapshot,
    CashoutPlanner,
    CashoutPolicy,
    CashoutReconciliation,
    CashoutRiskEvidence,
    CashoutRoutePreview,
    CashoutSource,
    PostgresCashoutStore,
)
from a6api_auto.trade_db import apply_migrations
from a6api_auto.trade_provider_liquidity import (
    PostgresProviderLiquidityStore,
    ProviderLiquidityCandidate,
)
from a6api_auto.trade_provider_topup import (
    LiveBalance,
    PostgresProviderTopupStore,
    ProviderTopupContext,
    ProviderTopupError,
    ProviderTopupOutcome,
)

NOW = datetime.now(UTC)
UNIT = 500_000


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _connect(dsn: str) -> psycopg.Connection[Any]:
    return psycopg.connect(dsn, row_factory=dict_row)


@pytest.fixture(scope="module")
def cashout_postgres(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    initdb = shutil.which("initdb")
    pg_ctl = shutil.which("pg_ctl")
    if initdb is None or pg_ctl is None:
        pytest.skip("PostgreSQL server binaries are not installed")
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("initdb does not run as root")

    root = tmp_path_factory.mktemp("cashout-postgres")
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
            assert "0050" in applied
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
def database(cashout_postgres: str) -> Iterator[str]:
    _truncate_business_tables(cashout_postgres)
    yield cashout_postgres
    _truncate_business_tables(cashout_postgres)


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


def _insert_merchant(
    dsn: str,
    seed: int,
    *,
    financial_at: datetime | None = None,
    withdrawable_micros: int = 55 * MICROS_PER_USD,
    financial_source_hash: str | None = None,
) -> int:
    with _connect(dsn) as connection:
        row = connection.execute(
            """
            INSERT INTO a6_trade.a6_merchants (
                a6_user_id, supplier_id, session_ref, session_fingerprint,
                roles, status, quota_per_unit, created_at
            ) VALUES (%s, %s, %s, %s, ARRAY['seller'], 'active', %s, %s)
            RETURNING id
            """,
            (
                10_000 + seed,
                20_000 + seed,
                f"session://cashout-fixture/{seed}",
                _digest(f"session:{seed}"),
                UNIT,
                NOW - timedelta(days=10),
            ),
        ).fetchone()
        assert row is not None
        merchant_id = int(row["id"])
        if financial_at is not None:
            connection.execute(
                """
                INSERT INTO a6_trade.a6_financial_current (
                    merchant_id, withdrawable_micros, observed_at, source_hash
                ) VALUES (%s, %s, %s, %s)
                """,
                (
                    merchant_id,
                    withdrawable_micros,
                    financial_at,
                    financial_source_hash or _digest(f"financial:{seed}"),
                ),
            )
        return merchant_id


def _insert_route(dsn: str, seed: int) -> tuple[int, int]:
    with _connect(dsn) as connection:
        principal = connection.execute(
            """
            INSERT INTO a6_trade.treasury_wallet_principals (
                inventory_principal_key, principal_type, address_sha256,
                address_masked, address_secret_ref, enabled, principal_state
            ) VALUES (%s, 'evm', %s, %s, %s, true, 'enabled')
            RETURNING id
            """,
            (
                f"principal-{seed}",
                _digest(f"address:{seed}"),
                f"0x{seed:04x}...fixture",
                f"secret://wallet/{seed}",
            ),
        ).fetchone()
        assert principal is not None
        principal_id = int(principal["id"])
        route = connection.execute(
            """
            INSERT INTO a6_trade.treasury_wallet_routes (
                wallet_principal_id, inventory_route_key, network, protocol,
                token_contract_sha256, token_contract_display, route_state
            ) VALUES (%s, %s, 'BSC', 'BEP20', %s, %s, 'healthy')
            RETURNING id
            """,
            (
                principal_id,
                f"route-{seed}",
                _digest(f"contract:{seed}"),
                f"contract-{seed}",
            ),
        ).fetchone()
        assert route is not None
        return principal_id, int(route["id"])


def _crypto_plan(
    merchant_id: int,
    principal_id: int,
    route_id: int,
    seed: int,
    *,
    observed_at: datetime = NOW,
):
    financial = CashoutFinancialSnapshot(
        merchant_id=merchant_id,
        withdrawable_micros=55 * MICROS_PER_USD,
        provider_reserve_micros=0,
        local_reserved_other_micros=0,
        balance_source_hash=_digest(f"balance:{seed}"),
        observed_at=observed_at,
        quota_per_unit=UNIT,
    )
    preview = CashoutRoutePreview(
        wallet_route_id=route_id,
        wallet_principal_id=principal_id,
        remote_withdraw_account_id=30_000 + seed,
        network="BSC",
        gross_micros=financial.cashout_eligible_micros,
        fee_micros=3 * MICROS_PER_USD,
        net_micros=52 * MICROS_PER_USD,
        confirmation_probability=Decimal("1"),
        preview_source_hash=_digest(f"preview:{seed}"),
    )
    return CashoutPlanner().plan(
        CashoutSource(
            financial=financial,
            risk=CashoutRiskEvidence(
                delta_effective=Decimal("0.5"),
                merchant_age_hours=Decimal("240"),
                hours_since_last_confirmed_cashout=Decimal("48"),
                earn_rate_p25_micros_per_hour=MICROS_PER_USD,
                hard_risk=True,
                fixed_probability=Decimal("0.01"),
                sample_count=120,
            ),
            routes=(preview,),
            decision_epoch=1_000 + seed,
        )
    )


def _callback_plan(merchant_id: int, receiver_id: int, seed: int):
    return CashoutPlanner().plan(
        CashoutSource(
            financial=CashoutFinancialSnapshot(
                merchant_id=merchant_id,
                withdrawable_micros=5 * MICROS_PER_USD,
                provider_reserve_micros=0,
                local_reserved_other_micros=0,
                balance_source_hash=_digest(f"callback-balance:{seed}"),
                observed_at=NOW,
                quota_per_unit=UNIT,
            ),
            risk=CashoutRiskEvidence(
                delta_effective=Decimal("0.5"),
                merchant_age_hours=Decimal("240"),
                hours_since_last_confirmed_cashout=Decimal("48"),
                earn_rate_p25_micros_per_hour=MICROS_PER_USD,
                hard_risk=True,
                fixed_probability=Decimal("0.4"),
                sample_count=120,
            ),
            callback_available=True,
            callback_sender_ref="sg-cloud-fixture",
            callback_receiver_merchant_id=receiver_id,
            callback_receiver_model="gpt-5.6-sol",
            callback_receiver_listing_id=40_000 + seed,
            decision_epoch=2_000 + seed,
        )
    )


def test_migration_pair_is_identical_and_empty_database_includes_0050(
    cashout_postgres: str,
) -> None:
    root = Path(__file__).resolve().parents[1]
    source = (root / "migrations/0050_risk_balanced_cashout.sql").read_bytes()
    packaged = (root / "src/a6api_auto/data/migrations/0050_risk_balanced_cashout.sql").read_bytes()

    assert source == packaged
    with _connect(cashout_postgres) as connection:
        versions = connection.execute(
            "SELECT version FROM a6_trade.schema_migrations ORDER BY version"
        ).fetchall()
        expected = [
            path.name.split("_", 1)[0]
            for path in sorted((root / "migrations").glob("[0-9][0-9][0-9][0-9]_*.sql"))
        ]
        assert [row["version"] for row in versions] == expected
        assert "0050" in expected


def test_topup_and_cashout_compete_for_one_shared_fund_reservation(
    database: str,
) -> None:
    merchant_id = _insert_merchant(
        database,
        1,
        financial_at=NOW,
        financial_source_hash=_digest("balance:1"),
    )
    principal_id, route_id = _insert_route(database, 1)
    plan = _crypto_plan(merchant_id, principal_id, route_id, 1)
    cashout = PostgresCashoutStore(
        lambda: _connect(database),
        financial_snapshot_ttl_seconds=300,
    )
    topup = PostgresProviderTopupStore(lambda: _connect(database))
    context = ProviderTopupContext(
        merchant_id=merchant_id,
        a6_user_id=10_001,
        session_ref="session://cashout-fixture/1",
        quota_per_unit=UNIT,
    )
    barrier = Barrier(2)

    def create_cashout() -> tuple[str, str]:
        barrier.wait()
        try:
            cashout.create_intent(plan)
        except CashoutError as exc:
            return "cashout", exc.code
        return "cashout", "ok"

    def create_topup() -> tuple[str, str]:
        barrier.wait()
        try:
            topup.begin_transfer(
                context,
                LiveBalance(user_balance=0, withdrawable=5 * MICROS_PER_USD),
                UNIT,
                {"enabled": True, "net_micros": UNIT, "fee_micros": 0},
                idempotency_key="provider-topup:fixture:1",
            )
        except ProviderTopupError as exc:
            return "topup", str(exc)
        return "topup", "ok"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = dict(executor.map(lambda function: function(), (create_cashout, create_topup)))

    assert list(outcomes.values()).count("ok") == 1
    assert set(outcomes.values()) <= {
        "ok",
        "cashout_funds_already_reserved",
        "provider_topup_funds_already_reserved",
    }
    with _connect(database) as connection:
        claims = connection.execute(
            """
            SELECT owner_type, count(*) AS claim_count
            FROM a6_trade.merchant_fund_reservations
            WHERE merchant_id = %s AND reservation_state = 'active'
            GROUP BY owner_type
            """,
            (merchant_id,),
        ).fetchall()
        assert len(claims) == 1
        assert int(claims[0]["claim_count"]) == 1
        assert claims[0]["owner_type"] in {"provider_topup", "crypto_cashout"}


def test_concurrent_same_cashout_intent_is_exactly_once(database: str) -> None:
    merchant_id = _insert_merchant(
        database,
        2,
        financial_at=NOW,
        financial_source_hash=_digest("balance:2"),
    )
    principal_id, route_id = _insert_route(database, 2)
    plan = _crypto_plan(merchant_id, principal_id, route_id, 2)
    store = PostgresCashoutStore(
        lambda: _connect(database),
        financial_snapshot_ttl_seconds=300,
    )
    barrier = Barrier(2)

    def create() -> int:
        barrier.wait()
        return store.create_intent(plan).intent_id

    with ThreadPoolExecutor(max_workers=2) as executor:
        intent_ids = tuple(executor.map(lambda _index: create(), range(2)))

    assert intent_ids[0] == intent_ids[1]
    with _connect(database) as connection:
        counts = connection.execute(
            """
            SELECT
                (SELECT count(*) FROM a6_trade.a6_withdrawal_intents) AS intents,
                (SELECT count(*) FROM a6_trade.merchant_fund_reservations) AS claims,
                (SELECT count(*) FROM a6_trade.a6_cashout_risk_snapshots) AS risks
            """
        ).fetchone()
        assert counts == {"intents": 1, "claims": 1, "risks": 1}


def test_unknown_intent_uses_expiring_lease_and_is_never_resubmitted(
    database: str,
) -> None:
    merchant_id = _insert_merchant(
        database,
        3,
        financial_at=NOW,
        financial_source_hash=_digest("balance:3"),
    )
    principal_id, route_id = _insert_route(database, 3)
    plan = _crypto_plan(merchant_id, principal_id, route_id, 3)
    store = PostgresCashoutStore(
        lambda: _connect(database),
        financial_snapshot_ttl_seconds=300,
    )
    intent = store.create_intent(plan)
    store.begin_submit(intent.intent_id)
    store.record_submit_result(
        intent.intent_id,
        accepted=False,
        remote_request_id=None,
    )

    first = store.claim_reconciliation("worker-one", lease_seconds=60)
    assert first is not None
    assert first.intent_id == intent.intent_id
    assert store.claim_reconciliation("worker-two", lease_seconds=60) is None
    with pytest.raises(CashoutError, match="cashout_intent_not_submittable"):
        store.begin_submit(intent.intent_id)

    with _connect(database) as connection:
        connection.execute(
            """
            UPDATE a6_trade.a6_withdrawal_intents
            SET reconcile_lease_until = now() - interval '1 second'
            WHERE id = %s
            """,
            (intent.intent_id,),
        )
    with pytest.raises(CashoutError, match="cashout_reconcile_claim_lost"):
        store.complete_reconciliation(
            intent.intent_id,
            "worker-one",
            CashoutReconciliation(
                outcome="unknown",
                confirmation_type="remote_status",
                source_hash=_digest("expired-worker-result"),
                observed_at=NOW,
            ),
        )
    reclaimed = store.claim_reconciliation("worker-two", lease_seconds=60)
    assert reclaimed is not None
    assert reclaimed.intent_id == intent.intent_id
    with _connect(database) as connection:
        row = connection.execute(
            """
            SELECT intent_state, reconcile_owner, submit_attempted_at
            FROM a6_trade.a6_withdrawal_intents WHERE id = %s
            """,
            (intent.intent_id,),
        ).fetchone()
        assert row is not None
        assert row["intent_state"] == "RECONCILING"
        assert row["reconcile_owner"] == "worker-two"
        assert row["submit_attempted_at"] is not None


def test_crypto_reconciliation_context_and_terminal_status_close_original_intent(
    database: str,
) -> None:
    seed = 13
    merchant_id = _insert_merchant(
        database,
        seed,
        financial_at=NOW,
        financial_source_hash=_digest(f"balance:{seed}"),
    )
    principal_id, route_id = _insert_route(database, seed)
    store = PostgresCashoutStore(
        lambda: _connect(database),
        financial_snapshot_ttl_seconds=300,
    )
    intent = store.create_intent(
        _crypto_plan(merchant_id, principal_id, route_id, seed)
    )
    store.begin_submit(intent.intent_id)
    store.record_submit_result(
        intent.intent_id,
        accepted=True,
        remote_request_id="request-13",
        remote_withdrawal_id="withdrawal-13",
        confirmation_deadline_at=NOW + timedelta(hours=24),
    )
    claimed = store.claim_reconciliation(
        "crypto-worker",
        method="crypto",
        retry_interval_seconds=300,
    )
    assert claimed is not None
    context = store.load_reconciliation_context(claimed.intent_id)

    assert context.intent.intent_id == intent.intent_id
    assert context.intent.remote_withdrawal_id == "withdrawal-13"
    assert context.merchant.a6_user_id == 10_000 + seed
    assert context.remote_withdraw_account_id == 30_000 + seed
    assert context.baseline_withdrawable_micros == 55 * MICROS_PER_USD

    class Remote:
        def supplier_withdraw_status(self, user_id, withdrawal_id):
            assert user_id == 10_000 + seed
            assert withdrawal_id == "withdrawal-13"
            return {
                "id": withdrawal_id,
                "status": "completed",
                "amount_micros": 55 * MICROS_PER_USD,
                "fee_micros": 3 * MICROS_PER_USD,
                "net_micros": 52 * MICROS_PER_USD,
                "txid": "chain-13",
            }

        def supplier_withdrawals(self, *_args, **_kwargs):
            raise AssertionError("status ID match must not scan the withdrawal list twice")

        def supplier_withdraw_summary(self, user_id):
            assert user_id == 10_000 + seed
            return {
                "balance_available_micros": 0,
                "withdrawing_micros": 0,
                "total_paid_micros": 52 * MICROS_PER_USD,
            }

    result = A6CryptoCashoutReconciler(
        lambda _merchant: Remote(),
        clock=lambda: NOW + timedelta(minutes=1),
    ).reconcile(context)
    store.complete_reconciliation(intent.intent_id, "crypto-worker", result)

    with _connect(database) as connection:
        row = connection.execute(
            """
            SELECT intent.intent_state, intent.actual_fee_micros,
                   intent.actual_net_micros, intent.chain_txid,
                   reservation.reservation_state,
                   (SELECT count(*)
                    FROM a6_trade.a6_cashout_ledger_entries ledger
                    WHERE ledger.withdrawal_intent_id = intent.id) AS ledger_count
            FROM a6_trade.a6_withdrawal_intents intent
            JOIN a6_trade.merchant_fund_reservations reservation
              ON reservation.id = intent.fund_reservation_id
            WHERE intent.id = %s
            """,
            (intent.intent_id,),
        ).fetchone()
    assert row == {
        "intent_state": "CONFIRMED",
        "actual_fee_micros": 3 * MICROS_PER_USD,
        "actual_net_micros": 52 * MICROS_PER_USD,
        "chain_txid": "chain-13",
        "reservation_state": "released",
        "ledger_count": 3,
    }
    assert store.load_candidates(
        NOW + timedelta(minutes=1),
        policy=CashoutPolicy(
            financial_snapshot_ttl_seconds=300,
            route_readback_ttl_seconds=300,
        ),
    ) == ()


def test_pending_reconciliation_polls_before_deadline_and_throttles_hot_retry(
    database: str,
) -> None:
    seed = 14
    merchant_id = _insert_merchant(
        database,
        seed,
        financial_at=NOW,
        financial_source_hash=_digest(f"balance:{seed}"),
    )
    principal_id, route_id = _insert_route(database, seed)
    store = PostgresCashoutStore(
        lambda: _connect(database),
        financial_snapshot_ttl_seconds=300,
    )
    intent = store.create_intent(
        _crypto_plan(merchant_id, principal_id, route_id, seed)
    )
    store.begin_submit(intent.intent_id)
    store.record_submit_result(
        intent.intent_id,
        accepted=True,
        remote_request_id="request-14",
        remote_withdrawal_id="withdrawal-14",
        confirmation_deadline_at=NOW + timedelta(hours=24),
    )

    claimed = store.claim_reconciliation(
        "poll-worker",
        method="crypto",
        retry_interval_seconds=300,
    )
    assert claimed is not None
    store.complete_reconciliation(
        intent.intent_id,
        "poll-worker",
        CashoutReconciliation(
            outcome="pending",
            confirmation_type="remote_status",
            source_hash=_digest("pending-before-deadline"),
            observed_at=NOW,
            remote_status="processing",
            remote_withdrawal_id="withdrawal-14",
        ),
    )
    assert (
        store.claim_reconciliation(
            "hot-worker",
            method="crypto",
            retry_interval_seconds=300,
        )
        is None
    )

    with _connect(database) as connection:
        connection.execute(
            """
            UPDATE a6_trade.a6_withdrawal_intents
            SET last_reconciled_at = now() - interval '301 seconds'
            WHERE id = %s
            """,
            (intent.intent_id,),
        )
    retry = store.claim_reconciliation(
        "retry-worker",
        method="crypto",
        retry_interval_seconds=300,
    )
    assert retry is not None
    assert retry.intent_id == intent.intent_id


def test_confirmed_callback_releases_claim_and_writes_balanced_three_leg_ledger(
    database: str,
) -> None:
    sender_id = _insert_merchant(
        database,
        4,
        financial_at=NOW,
        withdrawable_micros=5 * MICROS_PER_USD,
        financial_source_hash=_digest("callback-balance:4"),
    )
    receiver_id = _insert_merchant(database, 5)
    plan = _callback_plan(sender_id, receiver_id, 4)
    assert plan.method == "callback"
    assert plan.action == "execute"
    store = PostgresCashoutStore(
        lambda: _connect(database),
        financial_snapshot_ttl_seconds=300,
    )
    intent = store.create_intent(plan)
    store.begin_submit(intent.intent_id)
    store.record_submit_result(
        intent.intent_id,
        accepted=False,
        remote_request_id=None,
    )
    claimed = store.claim_reconciliation("callback-worker")
    assert claimed is not None

    with _connect(database) as connection:
        connection.execute(
            """
            INSERT INTO a6_trade.a6_cashout_ledger_entries (
                withdrawal_intent_id, merchant_id, leg_type,
                amount_micros, source_hash, occurred_at
            ) VALUES
                (%s, %s, 'callback_receiver_credit', 700000, %s, %s),
                (%s, %s, 'callback_receiver_credit', 900000, %s, %s)
            """,
            (
                intent.intent_id,
                receiver_id,
                _digest("recent-receiver-credit"),
                NOW - timedelta(days=2),
                intent.intent_id,
                receiver_id,
                _digest("old-receiver-credit"),
                NOW - timedelta(days=8),
            ),
        )

    source_hash = _digest("callback-confirmed")
    result = CashoutReconciliation(
        outcome="confirmed",
        confirmation_type="callback_closed",
        source_hash=source_hash,
        observed_at=NOW,
        observed_gross_micros=5 * MICROS_PER_USD,
        observed_fee_micros=833_333,
        observed_net_micros=4_166_667,
        sender_debit_micros=5 * MICROS_PER_USD,
        callback_fee_micros=833_333,
        receiver_credit_micros=4_166_667,
    )
    store.complete_reconciliation(intent.intent_id, "callback-worker", result)
    store.complete_reconciliation(intent.intent_id, "callback-worker", result)

    with _connect(database) as connection:
        intent_row = connection.execute(
            """
            SELECT intent_state, actual_fee_micros, actual_net_micros
            FROM a6_trade.a6_withdrawal_intents WHERE id = %s
            """,
            (intent.intent_id,),
        ).fetchone()
        reservation = connection.execute(
            """
            SELECT reservation_state, released_at
            FROM a6_trade.merchant_fund_reservations WHERE id = %s
            """,
            (intent.fund_reservation_id,),
        ).fetchone()
        callback = connection.execute(
            """
            SELECT accounting_state, sender_debit_micros,
                   callback_fee_micros, receiver_credit_micros
            FROM a6_trade.a6_callback_sweep_intents
            WHERE withdrawal_intent_id = %s
            """,
            (intent.intent_id,),
        ).fetchone()
        ledger = connection.execute(
            """
            SELECT merchant_id, leg_type, amount_micros
            FROM a6_trade.a6_cashout_ledger_entries
            WHERE withdrawal_intent_id = %s AND source_hash = %s
            ORDER BY leg_type
            """,
            (intent.intent_id, source_hash),
        ).fetchall()
        receiver_position = connection.execute(
            """
            SELECT rolling_7d_callback_receiver_credit_micros
            FROM a6_trade.merchant_cashout_positions WHERE merchant_id = %s
            """,
            (receiver_id,),
        ).fetchone()

    assert intent_row == {
        "intent_state": "CONFIRMED",
        "actual_fee_micros": 833_333,
        "actual_net_micros": 4_166_667,
    }
    assert reservation is not None
    assert reservation["reservation_state"] == "released"
    assert reservation["released_at"] is not None
    assert callback == {
        "accounting_state": "confirmed",
        "sender_debit_micros": 5 * MICROS_PER_USD,
        "callback_fee_micros": 833_333,
        "receiver_credit_micros": 4_166_667,
    }
    assert ledger == [
        {
            "merchant_id": receiver_id,
            "leg_type": "callback_receiver_credit",
            "amount_micros": 4_166_667,
        },
        {
            "merchant_id": sender_id,
            "leg_type": "callback_sweep_fee",
            "amount_micros": 833_333,
        },
        {
            "merchant_id": sender_id,
            "leg_type": "callback_sweep_gross",
            "amount_micros": 5 * MICROS_PER_USD,
        },
    ]
    assert receiver_position == {"rolling_7d_callback_receiver_credit_micros": 4_866_667}


def test_authoritative_no_effect_releases_claim(database: str) -> None:
    merchant_id = _insert_merchant(
        database,
        6,
        financial_at=NOW,
        financial_source_hash=_digest("balance:6"),
    )
    principal_id, route_id = _insert_route(database, 6)
    plan = _crypto_plan(merchant_id, principal_id, route_id, 6)
    store = PostgresCashoutStore(
        lambda: _connect(database),
        financial_snapshot_ttl_seconds=300,
    )
    intent = store.create_intent(plan)
    store.begin_submit(intent.intent_id)
    store.record_submit_result(
        intent.intent_id,
        accepted=False,
        remote_request_id=None,
    )
    assert store.claim_reconciliation("failure-worker") is not None
    store.complete_reconciliation(
        intent.intent_id,
        "failure-worker",
        CashoutReconciliation(
            outcome="failed_final",
            confirmation_type="authoritative_no_effect",
            source_hash=_digest("authoritative-no-effect"),
            observed_at=NOW,
            authoritative_no_effect=True,
            failure_class="remote_rejected_before_effect",
        ),
    )

    with _connect(database) as connection:
        row = connection.execute(
            """
            SELECT intent.intent_state, intent.authoritative_no_effect_at,
                   reservation.reservation_state, reservation.released_at
            FROM a6_trade.a6_withdrawal_intents intent
            JOIN a6_trade.merchant_fund_reservations reservation
              ON reservation.id = intent.fund_reservation_id
            WHERE intent.id = %s
            """,
            (intent.intent_id,),
        ).fetchone()
    assert row is not None
    assert row["intent_state"] == "FAILED_FINAL"
    assert row["authoritative_no_effect_at"] == NOW
    assert row["reservation_state"] == "released"
    assert row["released_at"] is not None


def test_create_intent_rejects_stale_authoritative_financial_snapshot(
    database: str,
) -> None:
    observed_at = NOW - timedelta(minutes=10)
    merchant_id = _insert_merchant(
        database,
        11,
        financial_at=observed_at,
        financial_source_hash=_digest("balance:11"),
    )
    principal_id, route_id = _insert_route(database, 11)
    plan = _crypto_plan(
        merchant_id,
        principal_id,
        route_id,
        11,
        observed_at=observed_at,
    )
    store = PostgresCashoutStore(
        lambda: _connect(database),
        financial_snapshot_ttl_seconds=300,
    )

    with pytest.raises(CashoutError, match="cashout_financial_snapshot_stale"):
        store.create_intent(plan)

    with _connect(database) as connection:
        counts = connection.execute(
            """
            SELECT
                (SELECT count(*) FROM a6_trade.a6_withdrawal_intents) AS intents,
                (SELECT count(*) FROM a6_trade.merchant_fund_reservations) AS claims
            """
        ).fetchone()
    assert counts == {"intents": 0, "claims": 0}


def test_candidate_loader_enforces_freshness_and_merchant_route_binding(
    database: str,
) -> None:
    first_id = _insert_merchant(database, 7, financial_at=NOW)
    second_id = _insert_merchant(database, 8, financial_at=NOW)
    stale_reserve_id = _insert_merchant(database, 9, financial_at=NOW)
    _insert_merchant(database, 10, financial_at=NOW - timedelta(seconds=31))
    principal_id, route_id = _insert_route(database, 7)

    with _connect(database) as connection:
        for merchant_id, seed, remote_id in (
            (first_id, 7, 70_007),
            (second_id, 8, 80_008),
        ):
            connection.execute(
                """
                INSERT INTO a6_trade.merchant_treasury_assignments (
                    merchant_id, wallet_principal_id, assignment_state,
                    config_version, assignment_score
                ) VALUES (%s, %s, 'active', 'fixture-v1', %s)
                """,
                (merchant_id, principal_id, _digest(f"assignment:{seed}")),
            )
            connection.execute(
                """
                INSERT INTO a6_trade.merchant_treasury_route_bindings (
                    merchant_id, wallet_route_id, a6_withdraw_account_id,
                    binding_state, remote_account_source_hash, last_readback_at
                ) VALUES (%s, %s, %s, 'healthy', %s, %s)
                """,
                (
                    merchant_id,
                    route_id,
                    remote_id,
                    _digest(f"binding:{seed}"),
                    NOW,
                ),
            )
        connection.execute(
            """
            INSERT INTO a6_trade.provider_inventory (
                model, merchant_id, inventory_state
            ) VALUES ('gpt-5.6-sol', %s, 'READY')
            """,
            (stale_reserve_id,),
        )
        connection.execute(
            """
            INSERT INTO a6_trade.provider_fund_reserves (
                model, merchant_id, reserved_micros, reserve_basis,
                user_balance_micros, withdrawable_micros, quota_per_unit,
                balance_source_hash, observed_at
            ) VALUES (
                'gpt-5.6-sol', %s, 1000000, 'configured_minimum',
                1000000, 55000000, %s, %s, %s
            )
            """,
            (
                stale_reserve_id,
                UNIT,
                _digest("stale-provider-reserve"),
                NOW - timedelta(seconds=31),
            ),
        )

    store = PostgresCashoutStore(lambda: _connect(database))
    candidates = store.load_candidates(
        NOW,
        policy=CashoutPolicy(
            financial_snapshot_ttl_seconds=30,
            route_readback_ttl_seconds=60,
        ),
    )

    assert [candidate.context.merchant_id for candidate in candidates] == [
        first_id,
        second_id,
    ]
    remote_accounts = {
        candidate.context.merchant_id: candidate.source.routes[0].remote_withdraw_account_id
        for candidate in candidates
    }
    assert remote_accounts == {first_id: 70_007, second_id: 80_008}
    assert all(candidate.source.routes[0].wallet_route_id == route_id for candidate in candidates)
    assert all(candidate.source.routes[0].authoritative is False for candidate in candidates)


def test_provider_drain_resumes_the_same_intent_and_fund_reservation(
    database: str,
) -> None:
    seed = 12
    balance_hash = _digest(f"balance:{seed}")
    merchant_id = _insert_merchant(
        database,
        seed,
        financial_at=NOW,
        financial_source_hash=balance_hash,
    )
    principal_id, route_id = _insert_route(database, seed)
    waiting_plan = replace(
        _crypto_plan(merchant_id, principal_id, route_id, seed),
        action="awaiting_provider_drain",
        decision_reason="provider_drain_required",
    )
    store = PostgresCashoutStore(
        lambda: _connect(database),
        financial_snapshot_ttl_seconds=300,
    )
    waiting = store.create_intent(waiting_plan)
    assert waiting.state == "AWAITING_PROVIDER_DRAIN"
    account_id = 90_000 + seed
    token_id = 91_000 + seed

    with _connect(database) as connection:
        connection.execute(
            """
            INSERT INTO a6_trade.merchant_treasury_assignments (
                merchant_id, wallet_principal_id, assignment_state,
                config_version, assignment_score
            ) VALUES (%s, %s, 'active', 'fixture-v1', %s)
            """,
            (merchant_id, principal_id, _digest("resume-assignment")),
        )
        connection.execute(
            """
            INSERT INTO a6_trade.merchant_treasury_route_bindings (
                merchant_id, wallet_route_id, a6_withdraw_account_id,
                binding_state, remote_account_source_hash, last_readback_at
            ) VALUES (%s, %s, %s, 'healthy', %s, %s)
            """,
            (
                merchant_id,
                route_id,
                30_000 + seed,
                _digest("resume-binding"),
                NOW,
            ),
        )
        cost = connection.execute(
            """
            INSERT INTO a6_trade.provider_cost_generations (
                model, cost_generation, cost_source,
                cost_input_hash, cost_output_hash,
                token_mix_version, exchange_rate_version,
                active_slot_nos, desired_slot_costs,
                route_cost_multiplier, retry_reserve_multiplier,
                pool_cost_multiplier, billing_floor_multiplier,
                transition_floor_multiplier,
                observed_billing_floor_multiplier,
                pricing_state, config_version, confirmed_at, activated_at
            ) VALUES (
                'gpt-5.6-sol', 1, 'cashout-drain-fixture', %s, %s,
                'fixture-token', 'fixture-fx', ARRAY[1], '{"1":"1"}'::jsonb,
                1, 0, 1, 1, 1, 1,
                'active', 'fixture-v1', now(), now()
            ) RETURNING id
            """,
            (_digest("drain-cost-input"), _digest("drain-cost-output")),
        ).fetchone()
        assert cost is not None
        generation = connection.execute(
            """
            INSERT INTO a6_trade.provider_pool_generations (
                model, generation, generation_source, source_hash,
                cost_generation_id, cost_generation, desired_size,
                ordered_channel_ids, standby_channel_ids,
                generation_state, observed_at, activated_at, config_version
            ) VALUES (
                'gpt-5.6-sol', 1, 'cashout-drain-fixture', %s,
                %s, 1, 1, ARRAY[1]::bigint[], '{}'::bigint[],
                'active', now(), now(), 'fixture-v1'
            ) RETURNING id
            """,
            (_digest("drain-generation"), int(cost["id"])),
        ).fetchone()
        assert generation is not None
        connection.execute(
            """
            INSERT INTO a6_trade.provider_inventory (
                model, merchant_id, enodot_account_id, a6_token_id,
                inventory_state, liquidity_state, funding_eligible,
                schedulable, balance_units, assigned_slot_no
            ) VALUES (
                'gpt-5.6-sol', %s, %s, %s,
                'REPAIRING', 'FUNDED', true, false, %s, 1
            )
            """,
            (merchant_id, account_id, token_id, 2 * UNIT),
        )
        connection.execute(
            """
            INSERT INTO a6_trade.provider_pool_slots (
                generation_id, model, routing_generation, slot_no,
                slot_name, cost_generation_id, cost_generation,
                enodot_account_id, a6_token_id, liquidity_state,
                funding_eligible, slot_state,
                desired_schedulable, observed_schedulable
            ) VALUES (
                %s, 'gpt-5.6-sol', 1, 1,
                'cashout-drain-fixture', %s, 1,
                %s, %s, 'FUNDED', true, 'REPAIRING', false, false
            )
            """,
            (int(generation["id"]), int(cost["id"]), account_id, token_id),
        )
        connection.execute(
            """
            INSERT INTO a6_trade.provider_fund_reserves (
                model, merchant_id, reserved_micros, reserve_basis,
                user_balance_micros, withdrawable_micros, quota_per_unit,
                balance_source_hash, observed_at
            ) VALUES (
                'gpt-5.6-sol', %s, 0, 'cashout_drain',
                %s, %s, %s, %s, %s
            )
            """,
            (
                merchant_id,
                2 * UNIT,
                55 * MICROS_PER_USD,
                UNIT,
                _digest("drain-reserve"),
                NOW,
            ),
        )

    draining_candidate = store.load_candidates(
        NOW,
        policy=CashoutPolicy(
            financial_snapshot_ttl_seconds=300,
            route_readback_ttl_seconds=300,
        ),
    )[0]
    assert draining_candidate.source.provider_duty_state == "READY"
    assert draining_candidate.source.provider_drain_confirmed is False
    draining_route = replace(
        draining_candidate.source.routes[0],
        fee_micros=3 * MICROS_PER_USD,
        net_micros=52 * MICROS_PER_USD,
        preview_source_hash=_digest("draining-preview"),
        authoritative=True,
    )
    draining_plan = CashoutPlanner().plan(
        replace(
            draining_candidate.source,
            routes=(draining_route,),
            existing_cycle=waiting_plan.cycle,
        )
    )
    assert draining_plan.action == "awaiting_provider_drain"
    assert store.create_intent(draining_plan).intent_id == waiting.intent_id

    with _connect(database) as connection:
        connection.execute(
            """
            UPDATE a6_trade.provider_pool_slots
            SET valid_to = %s
            WHERE model = 'gpt-5.6-sol'
              AND enodot_account_id = %s AND a6_token_id = %s
              AND valid_to IS NULL
            """,
            (NOW, account_id, token_id),
        )

    candidate = store.load_candidates(
        NOW,
        policy=CashoutPolicy(
            financial_snapshot_ttl_seconds=300,
            route_readback_ttl_seconds=300,
        ),
    )[0]
    assert candidate.source.resumable_intent_id == waiting.intent_id
    assert candidate.source.funds_claimed is False
    assert candidate.source.financial.local_reserved_other_micros == 0
    assert candidate.source.provider_drain_confirmed is True
    authoritative_route = replace(
        candidate.source.routes[0],
        fee_micros=3 * MICROS_PER_USD,
        net_micros=52 * MICROS_PER_USD,
        preview_source_hash=_digest("resume-preview"),
        authoritative=True,
    )
    resumed_plan = CashoutPlanner().plan(
        replace(
            candidate.source,
            routes=(authoritative_route,),
            existing_cycle=waiting_plan.cycle,
        )
    )
    assert resumed_plan.action == "execute"
    assert resumed_plan.resume_intent_id == waiting.intent_id

    resumed = store.create_intent(resumed_plan)

    assert resumed.intent_id == waiting.intent_id
    assert resumed.fund_reservation_id == waiting.fund_reservation_id
    assert resumed.state == "PREVIEWED"
    with _connect(database) as connection:
        counts = connection.execute(
            """
            SELECT
                (SELECT count(*) FROM a6_trade.a6_withdrawal_intents) AS intents,
                (SELECT count(*) FROM a6_trade.merchant_fund_reservations) AS claims,
                (SELECT reserved_micros
                 FROM a6_trade.merchant_fund_reservations
                 WHERE id = %s) AS reserved_micros
            """,
            (waiting.fund_reservation_id,),
        ).fetchone()
    assert counts == {
        "intents": 1,
        "claims": 1,
        "reserved_micros": 55 * MICROS_PER_USD,
    }


def test_cashout_drain_projection_sets_provider_reserve_to_zero(
    database: str,
) -> None:
    seed = 15
    merchant_id = _insert_merchant(database, seed)
    account_id = 50_000 + seed
    token_id = 60_000 + seed
    with _connect(database) as connection:
        connection.execute(
            """
            INSERT INTO a6_trade.provider_inventory (
                model, merchant_id, enodot_account_id, a6_token_id,
                inventory_state, liquidity_state, funding_eligible,
                schedulable, balance_units, assigned_slot_no
            ) VALUES (
                'gpt-5.6-sol', %s, %s, %s,
                'READY', 'FUNDED', true, true, %s, 1
            )
            """,
            (merchant_id, account_id, token_id, 2 * UNIT),
        )
    context = ProviderTopupContext(
        merchant_id=merchant_id,
        a6_user_id=10_000 + seed,
        session_ref=f"session://cashout-fixture/{seed}",
        quota_per_unit=UNIT,
    )
    candidate = ProviderLiquidityCandidate(
        model="gpt-5.6-sol",
        context=context,
        enodot_account_id=account_id,
        a6_token_id=token_id,
        inventory_state="READY",
        liquidity_state="FUNDED",
        schedulable=True,
        assigned_slot_no=None,
        cashout_drain_requested=True,
    )
    outcome = ProviderTopupOutcome(
        merchant_id=merchant_id,
        action="noop",
        applied=False,
        user_balance_before=2 * UNIT,
        user_balance_after=2 * UNIT,
        withdrawable_before=55 * MICROS_PER_USD,
        funding_eligible=True,
        replacement_required=True,
        liquidity_state="FUNDED",
    )

    projected = PostgresProviderLiquidityStore(lambda: _connect(database)).project(
        candidate,
        outcome,
        hard_blocked=True,
        replacement_required=True,
        observed_at=NOW,
        event_key="cashout-drain-projection-fixture",
        refill_target_units=Decimal("2"),
        cashout_drain_requested=True,
    )

    with _connect(database) as connection:
        row = connection.execute(
            """
            SELECT inventory.inventory_state, inventory.schedulable,
                   reserve.reserved_micros, reserve.reserve_basis
            FROM a6_trade.provider_inventory inventory
            JOIN a6_trade.provider_fund_reserves reserve
              ON reserve.model = inventory.model
             AND reserve.merchant_id = inventory.merchant_id
            WHERE inventory.model = 'gpt-5.6-sol'
              AND inventory.merchant_id = %s
            """,
            (merchant_id,),
        ).fetchone()
    assert projected["provider_reserve_micros"] == 0
    assert projected["reserve_basis"] == "cashout_drain"
    assert row == {
        "inventory_state": "RETIRED",
        "schedulable": False,
        "reserved_micros": 0,
        "reserve_basis": "cashout_drain",
    }
