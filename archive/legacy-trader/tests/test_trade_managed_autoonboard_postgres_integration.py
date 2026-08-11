from __future__ import annotations

import os
import shutil
import socket
import subprocess
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from a6api_auto.trade_managed_autoonboard import PostgresManagedAutoOnboardDiscovery

psycopg = pytest.importorskip("psycopg")
dict_row = pytest.importorskip("psycopg.rows").dict_row

MODEL = "gpt-5.6-sol"
PRICES_A = (620_000, 3_720_000, 62_000, 775_000)
PRICES_B = (630_000, 3_780_000, 63_000, 787_500)
PRICES_C = (640_000, 3_840_000, 64_000, 800_000)


@pytest.fixture(scope="module")
def postgres_dsn(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    initdb = shutil.which("initdb")
    pg_ctl = shutil.which("pg_ctl")
    if initdb is None or pg_ctl is None:
        pytest.skip("PostgreSQL server binaries are not installed")
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("initdb does not run as root")

    root = tmp_path_factory.mktemp("managed-autoonboard-postgres")
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
    try:
        yield f"dbname=postgres host=127.0.0.1 port={port}"
    finally:
        subprocess.run(
            [pg_ctl, "-D", str(data_dir), "-m", "immediate", "-w", "stop"],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )


def test_same_price_canonical_rollover_records_one_new_follower(
    postgres_dsn: str,
) -> None:
    observed_at = datetime.now(timezone.utc) - timedelta(seconds=10)
    with _connect(postgres_dsn) as connection:
        _reset_fixture(connection)
        _insert_inventory(connection, observed_at=observed_at, target_prices=PRICES_A)
        _insert_canonical_action(
            connection,
            action_id=101,
            prices=PRICES_A,
            completed_at=observed_at - timedelta(seconds=50),
        )
        _insert_canonical_action(
            connection,
            action_id=102,
            prices=PRICES_A,
            completed_at=observed_at - timedelta(seconds=40),
        )
        _insert_follower_action(
            connection,
            action_id=201,
            source_action_id=101,
            prices=PRICES_A,
            completed_at=observed_at - timedelta(seconds=20),
        )

    targets = _discover(postgres_dsn)

    assert [target.listing_managed_uid for target in targets] == ["listing-target"]
    assert targets[0].listing_seed_plan is not None
    assert targets[0].listing_seed_plan.source_action_id == 102

    follower_completed_at = observed_at + timedelta(seconds=5)
    with _connect(postgres_dsn) as connection:
        _insert_follower_action(
            connection,
            action_id=202,
            source_action_id=102,
            prices=PRICES_A,
            completed_at=follower_completed_at,
        )

    assert _discover(postgres_dsn) == ()

    collector_observed_at = follower_completed_at + timedelta(seconds=1)
    with _connect(postgres_dsn) as connection:
        connection.execute(
            "UPDATE a6_trade.a6_merchants SET last_poll_at = %s WHERE id = 1",
            (collector_observed_at,),
        )
        connection.execute(
            """UPDATE a6_trade.a6_supplier_channels_current
               SET observed_at = %s WHERE merchant_id = 1""",
            (collector_observed_at,),
        )
        connection.execute(
            """UPDATE a6_trade.a6_supplier_listings_current
               SET observed_at = %s WHERE merchant_id = 1""",
            (collector_observed_at,),
        )

    assert _discover(postgres_dsn) == ()


def test_unprojected_latest_canonical_does_not_reactivate_older_seed(
    postgres_dsn: str,
) -> None:
    observed_at = datetime.now(timezone.utc) - timedelta(seconds=10)
    with _connect(postgres_dsn) as connection:
        _reset_fixture(connection)
        _insert_inventory(connection, observed_at=observed_at, target_prices=PRICES_C)
        _insert_canonical_action(
            connection,
            action_id=101,
            prices=PRICES_A,
            completed_at=observed_at - timedelta(seconds=50),
        )
        _insert_canonical_action(
            connection,
            action_id=102,
            prices=PRICES_B,
            completed_at=observed_at - timedelta(seconds=40),
        )

    assert _discover(postgres_dsn) == ()


def test_new_lower_hash_fleet_source_does_not_replace_valid_sticky_source(
    postgres_dsn: str,
) -> None:
    observed_at = datetime.now(timezone.utc) - timedelta(seconds=10)
    with _connect(postgres_dsn) as connection:
        _reset_fixture(connection)
        _insert_fleet_sticky_inventory(connection, observed_at=observed_at)
        _insert_canonical_action(
            connection,
            action_id=101,
            entity_id="listing-source-old",
            prices=PRICES_A,
            completed_at=observed_at - timedelta(seconds=50),
        )
        _insert_canonical_action(
            connection,
            action_id=103,
            entity_id="listing-source-new",
            prices=PRICES_B,
            completed_at=observed_at - timedelta(seconds=40),
        )
        _insert_follower_action(
            connection,
            action_id=201,
            entity_id="listing-target",
            source_action_id=101,
            prices=PRICES_A,
            completed_at=observed_at - timedelta(seconds=5),
        )
        lower_hash = connection.execute(
            """SELECT md5('202:listing-source-new') <
                      md5('202:listing-source-old') AS lower_hash"""
        ).fetchone()

    assert lower_hash == {"lower_hash": True}
    assert _discover(postgres_dsn) == ()


def test_newer_incompatible_follower_does_not_shadow_current_sticky_source(
    postgres_dsn: str,
) -> None:
    observed_at = datetime.now(timezone.utc) - timedelta(seconds=10)
    with _connect(postgres_dsn) as connection:
        _reset_fixture(connection)
        _insert_fleet_sticky_inventory(connection, observed_at=observed_at)
        _insert_canonical_action(
            connection,
            action_id=101,
            entity_id="listing-source-old",
            prices=PRICES_A,
            completed_at=observed_at - timedelta(seconds=50),
        )
        _insert_canonical_action(
            connection,
            action_id=103,
            entity_id="listing-source-new",
            prices=PRICES_B,
            completed_at=observed_at - timedelta(seconds=40),
        )
        _insert_follower_action(
            connection,
            action_id=201,
            entity_id="listing-target",
            source_action_id=101,
            prices=PRICES_A,
            completed_at=observed_at - timedelta(seconds=20),
        )
        _insert_follower_action(
            connection,
            action_id=202,
            entity_id="listing-target",
            source_action_id=103,
            prices=PRICES_B,
            completed_at=observed_at - timedelta(seconds=5),
        )

    assert _discover(postgres_dsn) == ()


def test_legacy_created_listing_uses_observed_status_for_sticky_source(
    postgres_dsn: str,
) -> None:
    observed_at = datetime.now(timezone.utc) - timedelta(seconds=10)
    with _connect(postgres_dsn) as connection:
        _reset_fixture(connection)
        _insert_fleet_sticky_inventory(connection, observed_at=observed_at)
        _insert_canonical_action(
            connection,
            action_id=101,
            entity_id="listing-source-old",
            prices=PRICES_A,
            completed_at=observed_at - timedelta(seconds=50),
        )
        _insert_canonical_action(
            connection,
            action_id=103,
            entity_id="listing-source-new",
            prices=PRICES_B,
            completed_at=observed_at - timedelta(seconds=40),
        )
        _insert_follower_action(
            connection,
            action_id=201,
            entity_id="listing-target",
            source_action_id=101,
            prices=PRICES_A,
            completed_at=observed_at - timedelta(seconds=5),
            expected_listing_status=None,
            observed_listing_status=2,
        )

    assert _discover(postgres_dsn) == ()


def test_invalid_sticky_fleet_source_falls_back_to_hash_source(
    postgres_dsn: str,
) -> None:
    observed_at = datetime.now(timezone.utc) - timedelta(seconds=10)
    with _connect(postgres_dsn) as connection:
        _reset_fixture(connection)
        _insert_fleet_sticky_inventory(connection, observed_at=observed_at)
        _insert_canonical_action(
            connection,
            action_id=101,
            entity_id="listing-source-old",
            prices=PRICES_A,
            completed_at=observed_at - timedelta(seconds=50),
        )
        _insert_canonical_action(
            connection,
            action_id=103,
            entity_id="listing-source-new",
            prices=PRICES_B,
            completed_at=observed_at - timedelta(seconds=40),
        )
        _insert_follower_action(
            connection,
            action_id=201,
            entity_id="listing-target",
            source_action_id=101,
            prices=PRICES_A,
            completed_at=observed_at - timedelta(seconds=5),
        )
        connection.execute(
            "UPDATE a6_trade.a6_merchants SET poll_error_count = 1 WHERE id = 1"
        )

    targets = _discover(postgres_dsn)

    assert [target.listing_managed_uid for target in targets] == ["listing-target"]
    assert targets[0].listing_seed_plan is not None
    assert targets[0].listing_seed_plan.source_action_id == 103
    assert targets[0].listing_seed_prices == PRICES_B


def _connect(dsn: str) -> Any:
    return psycopg.connect(dsn, autocommit=True, row_factory=dict_row)


def _discover(dsn: str) -> tuple[Any, ...]:
    discovery = PostgresManagedAutoOnboardDiscovery(lambda: _connect(dsn))
    return discovery.list_targets(
        model=MODEL,
        inventory_max_age_seconds=900,
        limit=100,
    )


def _reset_fixture(connection: Any) -> None:
    connection.execute(_MINIMUM_SCHEMA_SQL)
    connection.execute(
        """
        TRUNCATE a6_trade.trade_control_actions,
                 a6_trade.a6_managed_listing_bindings,
                 a6_trade.a6_managed_channel_bindings,
                 a6_trade.a6_supplier_listings_current,
                 a6_trade.a6_supplier_channels_current,
                 a6_trade.a6_seller_retirement_plans,
                 a6_trade.a6_merchants
        """
    )


def _insert_inventory(
    connection: Any,
    *,
    observed_at: datetime,
    target_prices: tuple[int, int, int, int],
) -> None:
    connection.execute(
        """
        INSERT INTO a6_trade.a6_merchants
            (id, a6_user_id, supplier_id, roles, status, session_ref,
             poll_error_count, last_poll_at, operating_group)
        VALUES (1, 1001, 2001, ARRAY['seller']::text[], 'supplier_submitted',
                '/sessions/merchant-1.json', 0, %s, 'standard')
        """,
        (observed_at,),
    )
    connection.execute(
        """
        INSERT INTO a6_trade.a6_supplier_channels_current
            (merchant_id, a6_channel_id, supplier_id, name, channel_type,
             status, availability, observed_at)
        VALUES
            (1, 101, 2001, 'source-channel', 1, 1, 1, %s),
            (1, 102, 2001, 'target-channel', 1, 1, 1, %s)
        """,
        (observed_at, observed_at),
    )
    connection.execute(
        """
        INSERT INTO a6_trade.a6_managed_channel_bindings
            (id, managed_uid, merchant_id, a6_channel_id, control_state)
        VALUES
            (11, 'channel-source', 1, 101, 'verified'),
            (12, 'channel-target', 1, 102, 'verified')
        """
    )
    connection.execute(
        """
        INSERT INTO a6_trade.a6_supplier_listings_current
            (merchant_id, a6_listing_id, a6_channel_id, platform_model,
             input_price_micros, output_price_micros,
             cache_read_price_micros, cache_write_price_micros,
             status, observed_at)
        VALUES
            (1, 1001, 101, %s, %s, %s, %s, %s, 2, %s),
            (1, 1002, 102, %s, %s, %s, %s, %s, 2, %s)
        """,
        (MODEL, *PRICES_A, observed_at, MODEL, *target_prices, observed_at),
    )
    connection.execute(
        """
        INSERT INTO a6_trade.a6_managed_listing_bindings
            (id, managed_uid, merchant_id, channel_binding_id,
             a6_listing_id, control_state)
        VALUES
            (21, 'listing-source', 1, 11, 1001, 'verified'),
            (22, 'listing-target', 1, 12, 1002, 'verified')
        """
    )


def _insert_fleet_sticky_inventory(
    connection: Any,
    *,
    observed_at: datetime,
) -> None:
    connection.execute(
        """
        INSERT INTO a6_trade.a6_merchants
            (id, a6_user_id, supplier_id, roles, status, session_ref,
             poll_error_count, last_poll_at, operating_group)
        VALUES
            (1, 1001, 2001, ARRAY['seller']::text[], 'supplier_submitted',
             '/sessions/source-old.json', 0, %s, 'standard'),
            (2, 1002, 2002, ARRAY['seller']::text[], 'supplier_submitted',
             '/sessions/target.json', 0, %s, 'standard'),
            (3, 1003, 2003, ARRAY['seller']::text[], 'supplier_submitted',
             '/sessions/source-new.json', 0, %s, 'standard')
        """,
        (observed_at, observed_at, observed_at),
    )
    connection.execute(
        """
        INSERT INTO a6_trade.a6_supplier_channels_current
            (merchant_id, a6_channel_id, supplier_id, name, channel_type,
             status, availability, observed_at)
        VALUES
            (1, 101, 2001, 'source-old', 1, 1, 1, %s),
            (2, 202, 2002, 'target-channel', 1, 1, 1, %s),
            (3, 301, 2003, 'source-new', 1, 1, 1, %s)
        """,
        (observed_at, observed_at, observed_at),
    )
    connection.execute(
        """
        INSERT INTO a6_trade.a6_managed_channel_bindings
            (id, managed_uid, merchant_id, a6_channel_id, control_state)
        VALUES
            (11, 'channel-source-old', 1, 101, 'verified'),
            (22, 'channel-target', 2, 202, 'verified'),
            (31, 'channel-source-new', 3, 301, 'verified')
        """
    )
    connection.execute(
        """
        INSERT INTO a6_trade.a6_supplier_listings_current
            (merchant_id, a6_listing_id, a6_channel_id, platform_model,
             input_price_micros, output_price_micros,
             cache_read_price_micros, cache_write_price_micros,
             status, observed_at)
        VALUES
            (1, 1001, 101, %s, %s, %s, %s, %s, 2, %s),
            (2, 2001, 202, %s, %s, %s, %s, %s, 2, %s),
            (3, 3001, 301, %s, %s, %s, %s, %s, 2, %s)
        """,
        (
            MODEL,
            *PRICES_A,
            observed_at,
            MODEL,
            *PRICES_A,
            observed_at,
            MODEL,
            *PRICES_B,
            observed_at,
        ),
    )
    connection.execute(
        """
        INSERT INTO a6_trade.a6_managed_listing_bindings
            (id, managed_uid, merchant_id, channel_binding_id,
             a6_listing_id, control_state)
        VALUES
            (21, 'listing-source-old', 1, 11, 1001, 'verified'),
            (42, 'listing-target', 2, 22, 2001, 'verified'),
            (61, 'listing-source-new', 3, 31, 3001, 'verified')
        """
    )


def _insert_canonical_action(
    connection: Any,
    *,
    action_id: int,
    entity_id: str = "listing-source",
    prices: tuple[int, int, int, int],
    completed_at: datetime,
) -> None:
    connection.execute(
        """
        INSERT INTO a6_trade.trade_control_actions
            (id, action_type, entity_type, entity_id, desired_state,
             status, completed_at)
        VALUES (%s, 'apply_seller_price_band', 'a6_managed_listing',
                %s, %s::jsonb, 'completed', %s)
        """,
        (action_id, entity_id, _canonical_state(prices), completed_at),
    )


def _insert_follower_action(
    connection: Any,
    *,
    action_id: int,
    entity_id: str = "listing-target",
    source_action_id: int,
    prices: tuple[int, int, int, int],
    completed_at: datetime,
    expected_listing_status: int | None = 2,
    observed_listing_status: int | None = None,
) -> None:
    connection.execute(
        """
        INSERT INTO a6_trade.trade_control_actions
            (id, action_type, entity_type, entity_id, desired_state, observed_after,
             status, completed_at)
        VALUES (%s, 'apply_seller_price_band', 'a6_managed_listing',
                %s, %s::jsonb, %s::jsonb, 'completed', %s)
        """,
        (
            action_id,
            entity_id,
            psycopg.types.json.Jsonb(
                {
                    "model": MODEL,
                    "pricing_strategy": "managed_listing_price_seed_follower_v1",
                    "source_price_action_id": source_action_id,
                    "expected_listing_status": expected_listing_status,
                    "prices_micros": _prices_state(prices),
                }
            ),
            psycopg.types.json.Jsonb(
                {"listing_status": observed_listing_status}
                if observed_listing_status is not None
                else {}
            ),
            completed_at,
        ),
    )


def _canonical_state(prices: tuple[int, int, int, int]) -> Any:
    return psycopg.types.json.Jsonb(
        {
            "model": MODEL,
            "pricing_strategy": "seller_price_band_v2",
            "prices_micros": _prices_state(prices),
            "rank": 1,
            "tier": "floor",
            "price_zone": "low",
            "log_position": "0.1",
            "delta": "0.1",
            "target_final_multiplier": "0.15",
            "a6_panel_multiplier": "0.1",
        }
    )


def _prices_state(prices: tuple[int, int, int, int]) -> dict[str, int]:
    return dict(zip(("input", "output", "cache_read", "cache_write"), prices, strict=True))


_MINIMUM_SCHEMA_SQL = """
CREATE SCHEMA IF NOT EXISTS a6_trade;

CREATE TABLE IF NOT EXISTS a6_trade.a6_merchants (
    id bigint PRIMARY KEY,
    a6_user_id bigint,
    supplier_id bigint,
    roles text[] NOT NULL,
    status text NOT NULL,
    session_ref text NOT NULL,
    poll_error_count integer NOT NULL,
    last_poll_at timestamptz,
    operating_group text NOT NULL
);

CREATE TABLE IF NOT EXISTS a6_trade.a6_supplier_channels_current (
    merchant_id bigint NOT NULL,
    a6_channel_id bigint NOT NULL,
    supplier_id bigint,
    name text NOT NULL,
    channel_type integer,
    status integer,
    availability integer,
    observed_at timestamptz NOT NULL,
    PRIMARY KEY (merchant_id, a6_channel_id)
);

CREATE TABLE IF NOT EXISTS a6_trade.a6_supplier_listings_current (
    merchant_id bigint NOT NULL,
    a6_listing_id bigint NOT NULL,
    a6_channel_id bigint NOT NULL,
    platform_model text NOT NULL,
    input_price_micros bigint NOT NULL,
    output_price_micros bigint NOT NULL,
    cache_read_price_micros bigint NOT NULL,
    cache_write_price_micros bigint NOT NULL,
    status integer,
    observed_at timestamptz NOT NULL,
    PRIMARY KEY (merchant_id, a6_listing_id)
);

CREATE TABLE IF NOT EXISTS a6_trade.a6_managed_channel_bindings (
    id bigint PRIMARY KEY,
    managed_uid text NOT NULL UNIQUE,
    merchant_id bigint NOT NULL,
    a6_channel_id bigint,
    control_state text NOT NULL
);

CREATE TABLE IF NOT EXISTS a6_trade.a6_managed_listing_bindings (
    id bigint PRIMARY KEY,
    managed_uid text NOT NULL UNIQUE,
    merchant_id bigint NOT NULL,
    channel_binding_id bigint NOT NULL,
    a6_listing_id bigint,
    control_state text NOT NULL
);

CREATE TABLE IF NOT EXISTS a6_trade.trade_control_actions (
    id bigint PRIMARY KEY,
    action_type text NOT NULL,
    entity_type text NOT NULL,
    entity_id text NOT NULL,
    desired_state jsonb NOT NULL,
    observed_after jsonb,
    status text NOT NULL,
    completed_at timestamptz
);

CREATE TABLE IF NOT EXISTS a6_trade.a6_seller_retirement_plans (
    merchant_id bigint PRIMARY KEY,
    completed_at timestamptz
)
"""
