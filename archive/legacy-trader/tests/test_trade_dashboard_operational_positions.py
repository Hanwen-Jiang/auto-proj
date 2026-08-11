from __future__ import annotations

import os
import shutil
import socket
import subprocess
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone

import pytest

from a6api_auto.trade_repository import PostgresTradeRepository

psycopg = pytest.importorskip("psycopg")
dict_row = pytest.importorskip("psycopg.rows").dict_row

UTC = timezone.utc
NOW = datetime(2026, 7, 31, 1, 0, tzinfo=UTC)
MODEL = "gpt-5.6-sol"


@pytest.fixture(scope="module")
def operational_positions_postgres(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[str]:
    initdb = shutil.which("initdb")
    pg_ctl = shutil.which("pg_ctl")
    if initdb is None or pg_ctl is None:
        pytest.skip("PostgreSQL server binaries are not installed")
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("initdb does not run as root")

    data_dir = tmp_path_factory.mktemp("dashboard-operational-positions-pg") / "data"
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
        with psycopg.connect(dsn, autocommit=True) as connection:
            connection.execute(_MINIMUM_SCHEMA_SQL)
            _insert_scope_matrix(connection)
        yield dsn
    finally:
        subprocess.run(
            [pg_ctl, "-D", str(data_dir), "-m", "immediate", "-w", "stop"],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )


def test_operational_positions_are_current_managed_listings_not_historical_plans(
    operational_positions_postgres: str,
) -> None:
    repository = PostgresTradeRepository(
        lambda: psycopg.connect(operational_positions_postgres, row_factory=dict_row),
        now=lambda: NOW,
        dashboard_cache_ttl_seconds=0,
    )

    snapshot = repository.get_market_traffic("5m", MODEL)
    positions = {int(item["merchant_id"]): item for item in snapshot["managed_positions"]}

    # Current non-retired bindings are the set truth. A completed price plan is optional.
    assert set(positions) == {1, 2, 9, 10, 11, 12, 13, 17, 18}
    assert positions[1]["operational_state"] == "active"
    assert positions[1]["rank"] == 0
    assert positions[1]["channel_id"] == 501
    assert positions[1]["listing_id"] == 1001

    # A current managed Listing remains visible while down so the row explains the outage.
    assert positions[2]["operational_state"] == "blocked"
    assert positions[2]["blocked_reason"] == "listing_status_inactive"
    assert positions[2]["listing_status"] == 0
    assert positions[2]["listing_availability"] == 0
    assert positions[2]["listing_visibility"] == 0

    # A newer completed plan for another managed UID must not replace this Listing's plan.
    assert positions[9]["rank"] == 9
    assert positions[9]["operational_state"] == "active"

    # Bound rows retained from an older collector snapshot are not current inventory facts.
    assert positions[10]["operational_state"] == "blocked"
    assert positions[10]["blocked_reason"] == "listing_missing"
    assert positions[10]["listing_present"] is False

    # Only terminal statuses leave the set; in-progress states remain explicit diagnostics.
    assert positions[11]["operational_state"] == "blocked"
    assert positions[11]["blocked_reason"] == "merchant_status_not_supplier_submitted"
    assert positions[12]["operational_state"] == "blocked"
    assert positions[12]["blocked_reason"] == "binding_not_verified"
    assert positions[13]["operational_state"] == "active"

    # Forbidden and generated names stay out; ordinary ten-character names remain managed.
    assert {14, 15, 16}.isdisjoint(positions)
    assert positions[17]["channel_name"] == "easygarden"
    assert positions[18]["channel_name"] == "exampleapi"

    # Each excluded fixture has a physical current row or a completed plan, but no current
    # managed Seller position: retired Listing, retired parent Channel, abandoned merchant,
    # missing Seller role, completed retirement, and entirely unmanaged Listing respectively.
    assert {3, 4, 5, 6, 7, 8}.isdisjoint(positions)


def _insert_scope_matrix(connection: object) -> None:
    connection.execute(
        """
        INSERT INTO a6_trade.a6_merchants (id, status, roles, quota_per_unit, last_poll_at)
        VALUES
            (1,  'supplier_submitted', ARRAY['seller'],   500000, %s),
            (2,  'supplier_submitted', ARRAY['seller'],   500000, %s),
            (3,  'supplier_submitted', ARRAY['seller'],   500000, %s),
            (4,  'supplier_submitted', ARRAY['seller'],   500000, %s),
            (5,  'abandoned',          ARRAY['seller'],   500000, %s),
            (6,  'supplier_submitted', ARRAY['observer'], 500000, %s),
            (7,  'supplier_submitted', ARRAY['seller'],   500000, %s),
            (8,  'supplier_submitted', ARRAY['seller'],   500000, %s),
            (9,  'supplier_submitted', ARRAY['seller'],   500000, %s),
            (10, 'supplier_submitted', ARRAY['seller'],   500000, %s),
            (11, 'authenticated',      ARRAY['seller'],   500000, %s),
            (12, 'supplier_submitted', ARRAY['seller'],   500000, %s),
            (13, 'supplier_submitted', ARRAY['seller'],   500000, %s),
            (14, 'supplier_submitted', ARRAY['seller'],   500000, %s),
            (15, 'supplier_submitted', ARRAY['seller'],   500000, %s),
            (16, 'supplier_submitted', ARRAY['seller'],   500000, %s),
            (17, 'supplier_submitted', ARRAY['seller'],   500000, %s),
            (18, 'supplier_submitted', ARRAY['seller'],   500000, %s)
        """,
        (NOW,) * 18,
    )
    connection.execute(
        """
        INSERT INTO a6_trade.a6_seller_retirement_plans
            (merchant_id, state, completed_at)
        VALUES
            (7, 'completed', %s),
            (13, 'draining', NULL)
        """,
        (NOW - timedelta(minutes=5),),
    )
    connection.execute(
        """
        INSERT INTO a6_trade.a6_supplier_channels_current
            (merchant_id, a6_channel_id, name, nickname, observed_at)
        SELECT merchant_id,
               500 + merchant_id,
               CASE merchant_id
                   WHEN 14 THEN '正常API中转站'
                   WHEN 15 THEN 'eqgb5qrv35'
                   WHEN 16 THEN 'expdpxroye'
                   WHEN 17 THEN 'easygarden'
                   WHEN 18 THEN 'exampleapi'
                   ELSE 'channel-' || merchant_id
               END,
               'nickname-' || merchant_id,
               CASE WHEN merchant_id = 10 THEN %s ELSE %s END
        FROM generate_series(1, 18) merchant_id
        """,
        (NOW - timedelta(hours=1), NOW),
    )
    connection.execute(
        """
        INSERT INTO a6_trade.a6_supplier_listings_current (
            merchant_id, a6_listing_id, a6_channel_id, platform_model,
            status, availability, visibility, observed_at
        )
        SELECT merchant_id,
               1000 + merchant_id,
               500 + merchant_id,
               %s,
               CASE WHEN merchant_id = 2 THEN 0 ELSE 2 END,
               CASE WHEN merchant_id = 2 THEN 0 ELSE 1 END,
               CASE WHEN merchant_id = 2 THEN 0 ELSE 1 END,
               CASE WHEN merchant_id = 10 THEN %s ELSE %s END
        FROM generate_series(1, 18) merchant_id
        """,
        (MODEL, NOW - timedelta(hours=1), NOW),
    )
    connection.execute(
        """
        INSERT INTO a6_trade.a6_managed_channel_bindings (
            id, managed_uid, merchant_id, a6_channel_id, control_state
        )
        SELECT 100 + merchant_id,
               'channel-m' || merchant_id,
               merchant_id,
               500 + merchant_id,
               CASE WHEN merchant_id = 4 THEN 'retired' ELSE 'verified' END
        FROM generate_series(1, 18) merchant_id
        WHERE merchant_id <> 8
        """
    )
    connection.execute(
        """
        INSERT INTO a6_trade.a6_managed_listing_bindings (
            id, managed_uid, merchant_id, channel_binding_id,
            a6_channel_id, a6_listing_id, control_state
        )
        SELECT 200 + merchant_id,
               'listing-m' || merchant_id,
               merchant_id,
               100 + merchant_id,
               500 + merchant_id,
               1000 + merchant_id,
               CASE
                   WHEN merchant_id = 3 THEN 'retired'
                   WHEN merchant_id = 12 THEN 'verifying'
                   ELSE 'verified'
               END
        FROM generate_series(1, 18) merchant_id
        WHERE merchant_id <> 8
        """
    )

    for merchant_id in (2, 3, 4, 5, 6, 7, 10, 11, 12, 13):
        _insert_price_plan(
            connection,
            action_id=merchant_id,
            merchant_id=merchant_id,
            managed_uid=f"listing-m{merchant_id}",
            rank=merchant_id,
            completed_at=NOW - timedelta(minutes=merchant_id),
        )
    _insert_price_plan(
        connection,
        action_id=80,
        merchant_id=8,
        managed_uid="unmanaged-stale-plan",
        rank=80,
        completed_at=NOW - timedelta(minutes=1),
    )
    _insert_price_plan(
        connection,
        action_id=90,
        merchant_id=9,
        managed_uid="listing-m9",
        rank=9,
        completed_at=NOW - timedelta(hours=2),
    )
    _insert_price_plan(
        connection,
        action_id=91,
        merchant_id=9,
        managed_uid="retired-old-listing-m9",
        rank=99,
        completed_at=NOW - timedelta(minutes=1),
    )


def _insert_price_plan(
    connection: object,
    *,
    action_id: int,
    merchant_id: int,
    managed_uid: str,
    rank: int,
    completed_at: datetime,
) -> None:
    connection.execute(
        """
        INSERT INTO a6_trade.trade_control_actions (
            id, action_type, entity_type, entity_id, desired_state,
            status, completed_at
        ) VALUES (
            %s,
            'apply_seller_price_band',
            'a6_managed_listing',
            %s,
            jsonb_build_object(
                'merchant_id', %s::bigint,
                'listing_managed_uid', %s::text,
                'model', %s::text,
                'rank', %s::integer,
                'tier', 'fixture',
                'price_zone', 'middle',
                'delta', '0.100000',
                'target_final_multiplier', '0.060000',
                'a6_panel_multiplier', '0.055000'
            ),
            'completed',
            %s
        )
        """,
        (
            action_id,
            managed_uid,
            merchant_id,
            managed_uid,
            MODEL,
            rank,
            completed_at,
        ),
    )


_MINIMUM_SCHEMA_SQL = """
CREATE SCHEMA a6_trade;

CREATE TABLE a6_trade.a6_merchants (
    id bigint PRIMARY KEY,
    status text NOT NULL,
    roles text[] NOT NULL,
    quota_per_unit bigint NOT NULL,
    last_poll_at timestamptz
);

CREATE TABLE a6_trade.a6_seller_retirement_plans (
    merchant_id bigint PRIMARY KEY,
    state text NOT NULL,
    completed_at timestamptz
);

CREATE TABLE a6_trade.a6_supplier_channels_current (
    merchant_id bigint NOT NULL,
    a6_channel_id bigint NOT NULL,
    name text NOT NULL,
    nickname text NOT NULL,
    observed_at timestamptz NOT NULL,
    PRIMARY KEY (merchant_id, a6_channel_id)
);

CREATE TABLE a6_trade.a6_supplier_listings_current (
    merchant_id bigint NOT NULL,
    a6_listing_id bigint NOT NULL,
    a6_channel_id bigint NOT NULL,
    platform_model text NOT NULL,
    status integer,
    availability integer,
    visibility integer,
    observed_at timestamptz NOT NULL,
    PRIMARY KEY (merchant_id, a6_listing_id)
);

CREATE TABLE a6_trade.a6_managed_channel_bindings (
    id bigint PRIMARY KEY,
    managed_uid text NOT NULL,
    merchant_id bigint NOT NULL,
    a6_channel_id bigint,
    control_state text NOT NULL
);

CREATE TABLE a6_trade.a6_managed_listing_bindings (
    id bigint PRIMARY KEY,
    managed_uid text NOT NULL,
    merchant_id bigint NOT NULL,
    channel_binding_id bigint NOT NULL,
    a6_channel_id bigint,
    a6_listing_id bigint,
    control_state text NOT NULL
);

CREATE TABLE a6_trade.a6_channel_growth_policies (
    listing_binding_id bigint PRIMARY KEY,
    degraded_success_rate integer NOT NULL DEFAULT 8000,
    idle_seconds integer NOT NULL DEFAULT 1800
);

CREATE TABLE a6_trade.trade_control_actions (
    id bigint PRIMARY KEY,
    action_type text NOT NULL,
    entity_type text NOT NULL,
    entity_id text NOT NULL,
    desired_state jsonb NOT NULL,
    status text NOT NULL,
    completed_at timestamptz
);

CREATE TABLE a6_trade.a6_earning_events (
    merchant_id bigint NOT NULL,
    channel_id bigint,
    listing_id bigint,
    earned_micros bigint NOT NULL,
    occurred_at timestamptz NOT NULL,
    model text NOT NULL
);

CREATE TABLE a6_trade.enodot_managed_seller_resources (
    merchant_id bigint NOT NULL,
    enodot_api_key_id bigint
);

CREATE TABLE public.usage_logs (
    api_key_id bigint,
    actual_cost numeric NOT NULL,
    created_at timestamptz NOT NULL,
    model text,
    requested_model text,
    upstream_model text
);

CREATE TABLE a6_trade.a6_market_channel_signals_current (
    model text NOT NULL,
    channel_id bigint NOT NULL,
    listing_id bigint,
    supplier_id bigint,
    realtime_ratio_cny numeric,
    available boolean NOT NULL DEFAULT true,
    market_rank integer,
    health_state text,
    route_status text,
    circuit_state text,
    circuit_cooling_until timestamptz,
    cooldown_state text,
    cooldown_remaining_seconds integer,
    cooldown_expected_recovery_at timestamptz,
    smart_routing_status text,
    sample_count integer,
    recent_success_rate integer,
    recent_p50_ms numeric,
    p50_ttft_ms numeric,
    smart_routing_labels text[],
    last_observed_at timestamptz NOT NULL,
    last_success_at timestamptz,
    last_failure_at timestamptz,
    last_failure_code text,
    PRIMARY KEY (model, channel_id)
);

CREATE TABLE a6_trade.a6_market_traffic_events (
    id bigint PRIMARY KEY,
    model text NOT NULL,
    channel_id bigint NOT NULL,
    listing_id bigint,
    success_at timestamptz NOT NULL,
    first_observed_at timestamptz NOT NULL,
    realtime_ratio_cny numeric,
    is_owned boolean NOT NULL,
    observer_ids bigint[] NOT NULL
);

CREATE TABLE a6_trade.a6_channel_failure_events (
    model text NOT NULL,
    channel_id bigint NOT NULL,
    failure_code text NOT NULL,
    responsibility text NOT NULL,
    failure_at timestamptz NOT NULL
);

CREATE TABLE a6_trade.a6_market_observer_scan_backoff (
    model text PRIMARY KEY,
    consecutive_failed_rounds integer NOT NULL,
    retry_after timestamptz,
    last_error_class text,
    last_failure_at timestamptz,
    last_success_at timestamptz
);
"""
