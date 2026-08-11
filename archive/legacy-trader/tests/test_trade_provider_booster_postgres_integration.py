from __future__ import annotations

import os
import shutil
import socket
import subprocess
import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from types import SimpleNamespace
from typing import Any

import pytest

from a6api_auto.trade_channel_growth import PostgresChannelGrowthStore
from a6api_auto.trade_db import apply_migrations
from a6api_auto.trade_provider_binding import (
    PostgresProviderBindingStore,
    ProviderBindingError,
    ProviderBindingRequest,
)

psycopg = pytest.importorskip("psycopg")
dict_row = pytest.importorskip("psycopg.rows").dict_row


@pytest.fixture(scope="module")
def provider_booster_postgres(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    initdb = shutil.which("initdb")
    pg_ctl = shutil.which("pg_ctl")
    if initdb is None or pg_ctl is None:
        pytest.skip("PostgreSQL server binaries are not installed")
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("initdb does not run as root")

    root = tmp_path_factory.mktemp("provider-booster-postgres")
    data_dir = root / "pgdata"
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
        with psycopg.connect(dsn, row_factory=dict_row) as connection:
            applied = apply_migrations(connection)
            assert len(applied) >= 48
        yield dsn
    finally:
        subprocess.run(
            [pg_ctl, "-D", str(data_dir), "-m", "immediate", "-w", "stop"],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )


class _PausingCursor:
    def __init__(
        self,
        cursor: Any,
        *,
        marker: str,
        acquired: threading.Event,
        release: threading.Event,
    ) -> None:
        self._cursor = cursor
        self._marker = marker
        self._acquired = acquired
        self._release = release
        self._paused = False

    def __enter__(self) -> _PausingCursor:
        self._cursor.__enter__()
        return self

    def __exit__(self, *args: Any) -> Any:
        return self._cursor.__exit__(*args)

    def execute(self, query: Any, params: Any = None) -> _PausingCursor:
        self._cursor.execute(query, params)
        normalized = " ".join(str(query).split())
        if not self._paused and self._marker in normalized:
            self._paused = True
            self._acquired.set()
            if not self._release.wait(timeout=5):
                raise AssertionError("leader transaction was not released")
        return self

    def fetchone(self) -> Any:
        return self._cursor.fetchone()

    def fetchall(self) -> Any:
        return self._cursor.fetchall()

    @property
    def description(self) -> Any:
        return self._cursor.description

    @property
    def rowcount(self) -> int:
        return self._cursor.rowcount


class _PausingConnection:
    def __init__(
        self,
        connection: Any,
        *,
        marker: str,
        acquired: threading.Event,
        release: threading.Event,
    ) -> None:
        self._connection = connection
        self._marker = marker
        self._acquired = acquired
        self._release = release

    def cursor(self, *args: Any, **kwargs: Any) -> _PausingCursor:
        return _PausingCursor(
            self._connection.cursor(*args, **kwargs),
            marker=self._marker,
            acquired=self._acquired,
            release=self._release,
        )

    def commit(self) -> None:
        self._connection.commit()

    def rollback(self) -> None:
        self._connection.rollback()

    def close(self) -> None:
        self._connection.close()


@pytest.mark.parametrize("leader", ("growth", "provider"))
def test_growth_claim_and_provider_binding_are_mutually_exclusive_without_deadlock(
    provider_booster_postgres: str,
    leader: str,
) -> None:
    _reset_fixture(provider_booster_postgres)
    provider_request = ProviderBindingRequest(
        merchant_id=1,
        external_a6_token_id=14_020,
        proxy_id=5,
    )
    provider_context = PostgresProviderBindingStore(
        lambda: psycopg.connect(provider_booster_postgres, row_factory=dict_row)
    ).load_context(1, 14_020)

    growth_connection = _worker_connection(provider_booster_postgres)
    provider_connection = _worker_connection(provider_booster_postgres)
    leader_acquired = threading.Event()
    release_leader = threading.Event()
    if leader == "growth":
        growth_factory = partial(
            _PausingConnection,
            growth_connection,
            marker="FOR UPDATE OF token, booster",
            acquired=leader_acquired,
            release=release_leader,
        )
        provider_factory = partial(_same_connection, provider_connection)
        waiter_pid = provider_connection.info.backend_pid
    else:
        growth_factory = partial(_same_connection, growth_connection)
        provider_factory = partial(
            _PausingConnection,
            provider_connection,
            marker="SELECT id FROM a6_trade.a6_tokens",
            acquired=leader_acquired,
            release=release_leader,
        )
        waiter_pid = growth_connection.info.backend_pid

    growth_store = PostgresChannelGrowthStore(growth_factory)
    provider_store = PostgresProviderBindingStore(provider_factory)
    with ThreadPoolExecutor(max_workers=2) as executor:
        if leader == "growth":
            leader_future = executor.submit(_claim_growth, growth_store, "growth-first")
            assert leader_acquired.wait(timeout=3)
            waiter_future = executor.submit(
                _prepare_provider,
                provider_store,
                provider_context,
                provider_request,
                "provider-after-growth",
            )
        else:
            leader_future = executor.submit(
                _prepare_provider,
                provider_store,
                provider_context,
                provider_request,
                "provider-first",
            )
            assert leader_acquired.wait(timeout=3)
            waiter_future = executor.submit(_claim_growth, growth_store, "growth-after-provider")

        _wait_for_row_lock(provider_booster_postgres, waiter_pid)
        released_at = time.monotonic()
        release_leader.set()

        if leader == "growth":
            growth_claim = leader_future.result(timeout=3)
            with pytest.raises(
                ProviderBindingError,
                match="provider_binding_booster_action_running",
            ):
                waiter_future.result(timeout=3)
            assert growth_claim is not None
        else:
            preparation = leader_future.result(timeout=3)
            growth_claim = waiter_future.result(timeout=3)
            assert preparation.binding_id > 0
            assert growth_claim is None

    assert time.monotonic() - released_at < 3
    with psycopg.connect(
        provider_booster_postgres,
        autocommit=True,
        row_factory=dict_row,
    ) as connection:
        counts = connection.execute(
            """
            SELECT
                (SELECT count(*) FROM a6_trade.enodot_account_bindings) AS bindings,
                (SELECT count(*) FROM a6_trade.a6_channel_growth_actions) AS actions
            """
        ).fetchone()
    assert counts == (
        {"bindings": 0, "actions": 1}
        if leader == "growth"
        else {"bindings": 1, "actions": 0}
    )


def _claim_growth(store: PostgresChannelGrowthStore, suffix: str) -> Any:
    return store.claim_action(
        SimpleNamespace(
            merchant_id=1,
            listing_binding_id=30,
            model="gpt-5.6-sol",
            channel_id=1_120,
            listing_id=4_109,
        ),
        action_type="callback_readiness",
        health_state="cooling",
        idempotency_key=f"growth:{suffix}",
        booster_token_ref_id=10,
        request_count=1,
        estimated_cost_micros=100,
    )


def _prepare_provider(
    store: PostgresProviderBindingStore,
    context: Any,
    request: ProviderBindingRequest,
    suffix: str,
) -> Any:
    return store.prepare_binding(
        context,
        request,
        key_fingerprint="a" * 64,
        key_secret_ref="session:fixture#a6api_key",
        desired_state={"fixture": suffix},
        idempotency_key=f"bind-provider:{suffix}",
    )


def _worker_connection(dsn: str) -> Any:
    connection = psycopg.connect(dsn, row_factory=dict_row)
    connection.execute("SET lock_timeout = '4s'")
    connection.execute("SET statement_timeout = '8s'")
    connection.execute("SET deadlock_timeout = '100ms'")
    connection.commit()
    return connection


def _same_connection(connection: Any) -> Any:
    return connection


def _wait_for_row_lock(dsn: str, backend_pid: int) -> None:
    deadline = time.monotonic() + 3
    with psycopg.connect(dsn, autocommit=True, row_factory=dict_row) as connection:
        while time.monotonic() < deadline:
            activity = connection.execute(
                """
                SELECT wait_event_type, wait_event
                FROM pg_stat_activity
                WHERE pid = %s
                """,
                (backend_pid,),
            ).fetchone()
            if activity and activity["wait_event_type"] == "Lock":
                return
            time.sleep(0.01)
    raise AssertionError("competing transaction did not wait on the Token row lock")


def _reset_fixture(dsn: str) -> None:
    with psycopg.connect(dsn, autocommit=True) as connection:
        connection.execute("TRUNCATE a6_trade.a6_merchants CASCADE")
        connection.execute(
            """
            INSERT INTO a6_trade.a6_merchants (
                id, a6_user_id, supplier_id, session_ref, session_fingerprint,
                roles, status, proxy_lease_id
            ) VALUES (
                1, 9517, 298, 'session-fixture.json', repeat('a', 64),
                ARRAY['seller', 'provider']::text[], 'supplier_submitted', 'singbox:23084'
            );
            INSERT INTO a6_trade.a6_tokens (
                id, merchant_id, a6_token_id, token_name, status
            ) VALUES (10, 1, 14020, 'callback', '1');
            INSERT INTO a6_trade.a6_managed_channel_bindings (
                id, managed_uid, merchant_id, create_idempotency_key
            ) VALUES (20, 'channel-fixture', 1, 'channel-fixture-create');
            INSERT INTO a6_trade.a6_managed_listing_bindings (
                id, managed_uid, merchant_id, channel_binding_id, create_idempotency_key
            ) VALUES (30, 'listing-fixture', 1, 20, 'listing-fixture-create');
            INSERT INTO a6_trade.a6_channel_growth_policies (
                listing_binding_id, enabled
            ) VALUES (30, true);
            INSERT INTO a6_trade.a6_channel_booster_tokens (
                a6_token_ref_id, enabled
            ) VALUES (10, true);
            """
        )
