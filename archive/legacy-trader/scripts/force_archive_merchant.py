#!/usr/bin/env python3
from __future__ import annotations

import argparse
import fcntl
import json
import os
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row

from a6api_auto.client import A6ApiClient
from a6api_auto.storage import SessionStore, read_private_json

CONFIG_VERSION = "force-observer-archive-20260727-v1"
LOCK_FILE = Path("/var/lib/a6-trader/seller-control.lock")


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(
        description="Take down one merchant's Listings and archive it as observer-only"
    )
    command.add_argument("--merchant", type=int, required=True)
    command.add_argument("--expected-name", required=True)
    command.add_argument(
        "--proxy-map-file",
        type=Path,
        default=Path("/var/lib/a6-trader/secrets/proxy-map.json"),
    )
    command.add_argument("--apply", action="store_true")
    return command


@contextmanager
def seller_lock() -> Iterator[None]:
    descriptor = os.open(LOCK_FILE, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def subject(connection: Any, merchant_id: int) -> dict[str, Any]:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT m.id, m.a6_user_id, m.supplier_id, m.session_ref,
                   m.supplier_name, m.roles, m.role_revision,
                   round(
                       coalesce(f.withdrawable_micros, 0)::numeric
                       * 1000000 / m.quota_per_unit
                   )::bigint AS withdrawable_micros,
                   round(
                       coalesce(f.user_balance_quota, 0)::numeric
                       * 1000000 / m.quota_per_unit
                   )::bigint AS user_balance_micros,
                   p.state AS retirement_state, p.archive_mode
            FROM a6_trade.a6_merchants m
            LEFT JOIN a6_trade.a6_financial_current f ON f.merchant_id = m.id
            LEFT JOIN a6_trade.a6_seller_retirement_plans p ON p.merchant_id = m.id
            WHERE m.id = %s
            """,
            (merchant_id,),
        )
        row = cursor.fetchone()
    if row is None:
        raise RuntimeError("force_archive_merchant_missing")
    return dict(row)


def listings(client: A6ApiClient, user_id: int) -> list[dict[str, Any]]:
    by_id: dict[int, dict[str, Any]] = {}
    for page in range(1, 101):
        response = client.supplier_listings(user_id, page=page, size=100)
        items = response.get("items")
        if not isinstance(items, list):
            raise RuntimeError("force_archive_listing_inventory_invalid")
        for item in items:
            if not isinstance(item, Mapping) or int(item.get("id") or 0) < 1:
                raise RuntimeError("force_archive_listing_inventory_invalid")
            by_id[int(item["id"])] = dict(item)
        total = int(response.get("total") or 0)
        if not items or len(items) < 100 or (total and len(by_id) >= total):
            break
    else:
        raise RuntimeError("force_archive_listing_inventory_incomplete")
    return [by_id[item_id] for item_id in sorted(by_id)]


def public_listing(item: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: item.get(key)
        for key in (
            "id",
            "channel_id",
            "platform_model",
            "status",
            "availability",
            "visibility",
        )
    }


def authenticated_client(
    row: Mapping[str, Any], proxy_by_user_id: Mapping[int, str]
) -> A6ApiClient:
    user_id = int(row["a6_user_id"])
    record = SessionStore(Path(str(row["session_ref"]))).load()
    if (
        record.user_id != user_id
        or record.supplier_id != int(row["supplier_id"])
        or not record.cookies
        or user_id not in proxy_by_user_id
    ):
        raise RuntimeError("force_archive_session_mismatch")
    client = A6ApiClient(record.origin, proxy_url=proxy_by_user_id[user_id])
    client.import_cookies(record.cookies)
    return client


def prepare_action(
    connection: Any,
    row: Mapping[str, Any],
    remote_listings: list[dict[str, Any]],
) -> tuple[int, str]:
    merchant_id = int(row["id"])
    observed_before = {
        "roles": list(row["roles"]),
        "withdrawable_micros": int(row["withdrawable_micros"]),
        "user_balance_micros": int(row["user_balance_micros"]),
        "listings": [public_listing(item) for item in remote_listings],
    }
    desired = {
        "roles": ["observer"],
        "listing_status": 0,
        "forfeit_account_value": True,
    }
    with connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO a6_trade.trade_control_actions (
                idempotency_key, action_type, entity_type, entity_id,
                desired_state, observed_before, status, config_version,
                requested_by, started_at
            ) VALUES (
                %s, 'force_archive_merchant', 'a6_merchant', %s,
                %s::jsonb, %s::jsonb, 'started', %s,
                'force-archive-merchant', now()
            )
            ON CONFLICT (idempotency_key) DO UPDATE SET
                observed_before = CASE
                    WHEN trade_control_actions.status = 'completed'
                    THEN trade_control_actions.observed_before
                    ELSE EXCLUDED.observed_before
                END,
                status = CASE
                    WHEN trade_control_actions.status = 'completed' THEN 'completed'
                    ELSE 'started'
                END,
                error_class = CASE
                    WHEN trade_control_actions.status = 'completed'
                    THEN trade_control_actions.error_class ELSE NULL
                END,
                started_at = CASE
                    WHEN trade_control_actions.status = 'completed'
                    THEN trade_control_actions.started_at ELSE now()
                END,
                completed_at = CASE
                    WHEN trade_control_actions.status = 'completed'
                    THEN trade_control_actions.completed_at ELSE NULL
                END,
                updated_at = now()
            RETURNING id, status
            """,
            (
                f"force-archive:merchant:{merchant_id}:{CONFIG_VERSION}",
                str(merchant_id),
                json.dumps(desired, sort_keys=True),
                json.dumps(observed_before, sort_keys=True),
                CONFIG_VERSION,
            ),
        )
        action = cursor.fetchone()
    connection.commit()
    return int(action["id"]), str(action["status"])


def complete_archive(
    connection: Any,
    row: Mapping[str, Any],
    action_id: int,
    remote_listings: list[dict[str, Any]],
) -> None:
    merchant_id = int(row["id"])
    roles_before = tuple(str(role) for role in row["roles"])
    listing_ids = [int(item["id"]) for item in remote_listings]
    observed_after = {
        "roles": ["observer"],
        "listings": [public_listing(item) for item in remote_listings],
        "forfeited_withdrawable_micros": int(row["withdrawable_micros"]),
        "forfeited_user_balance_micros": int(row["user_balance_micros"]),
    }
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT roles FROM a6_trade.a6_merchants WHERE id = %s FOR UPDATE",
            (merchant_id,),
        )
        locked = cursor.fetchone()
        if locked is None or tuple(str(role) for role in locked["roles"]) != roles_before:
            raise RuntimeError("force_archive_role_precondition_changed")
        cursor.execute(
            """
            UPDATE a6_trade.a6_merchants
            SET roles = ARRAY['observer']::text[],
                role_revision = role_revision + 1,
                updated_at = now()
            WHERE id = %s
            """,
            (merchant_id,),
        )
        for item in remote_listings:
            cursor.execute(
                """
                UPDATE a6_trade.a6_supplier_listings_current
                SET status = %s, availability = %s, visibility = %s,
                    observed_at = now(), updated_at = now()
                WHERE merchant_id = %s AND a6_listing_id = %s
                """,
                (
                    int(item.get("status") or 0),
                    int(item.get("availability") or 0),
                    int(item.get("visibility") or 0),
                    merchant_id,
                    int(item["id"]),
                ),
            )
        cursor.execute(
            """
            UPDATE a6_trade.a6_channel_growth_policies policy
            SET enabled = false, recycle_enabled = false,
                callback_enabled = false, updated_at = now()
            FROM a6_trade.a6_managed_listing_bindings binding
            WHERE policy.listing_binding_id = binding.id
              AND binding.merchant_id = %s
            """,
            (merchant_id,),
        )
        cursor.execute(
            """
            UPDATE a6_trade.a6_managed_listing_bindings
            SET control_state = 'retired', last_error_class = NULL, updated_at = now()
            WHERE merchant_id = %s AND control_state <> 'retired'
            """,
            (merchant_id,),
        )
        cursor.execute(
            """
            UPDATE a6_trade.a6_managed_channel_bindings
            SET control_state = 'retired', last_error_class = NULL, updated_at = now()
            WHERE merchant_id = %s AND control_state <> 'retired'
            """,
            (merchant_id,),
        )
        cursor.execute(
            """
            UPDATE a6_trade.a6_seller_retirement_plans
            SET state = 'completed', archive_mode = true, completed_at = now(),
                retry_after = NULL, last_error_class = NULL, updated_at = now()
            WHERE merchant_id = %s
            """,
            (merchant_id,),
        )
        if cursor.rowcount != 1:
            raise RuntimeError("force_archive_plan_missing")
        cursor.execute(
            """
            INSERT INTO a6_trade.a6_seller_retirement_events (
                merchant_id, event_type, from_state, to_state, details
            ) VALUES (%s, 'force_archive_completed', %s, 'completed', %s::jsonb)
            """,
            (
                merchant_id,
                str(row.get("retirement_state") or ""),
                json.dumps(
                    {
                        "roles_before": list(roles_before),
                        "roles_after": ["observer"],
                        "listing_ids": listing_ids,
                        "forfeited_withdrawable_micros": int(row["withdrawable_micros"]),
                        "forfeited_user_balance_micros": int(row["user_balance_micros"]),
                    },
                    sort_keys=True,
                ),
            ),
        )
        cursor.execute(
            """
            UPDATE a6_trade.trade_control_actions
            SET observed_after = %s::jsonb, status = 'completed',
                error_class = NULL, completed_at = now(), updated_at = now()
            WHERE id = %s
            """,
            (json.dumps(observed_after, sort_keys=True), action_id),
        )
    connection.commit()


def main() -> None:
    args = parser().parse_args()
    if args.merchant < 1 or not args.expected_name.strip():
        raise RuntimeError("force_archive_arguments_invalid")
    dsn = os.environ.get("A6_TRADE_DATABASE_DSN", "").strip()
    if not dsn:
        raise RuntimeError("force_archive_database_dsn_missing")
    proxy_by_user_id = {
        int(key): str(value)
        for key, value in read_private_json(args.proxy_map_file).items()
    }
    connection = psycopg.connect(dsn, row_factory=dict_row)
    try:
        with seller_lock():
            row = subject(connection, args.merchant)
            connection.commit()
            if str(row["supplier_name"]) != args.expected_name:
                raise RuntimeError("force_archive_name_precondition_failed")
            roles = tuple(str(role) for role in row["roles"])
            if roles == ("observer",) and row.get("retirement_state") == "completed":
                print(json.dumps({"success": True, "action": "already_archived"}))
                return
            if "seller" not in roles:
                raise RuntimeError("force_archive_role_precondition_failed")
            client = authenticated_client(row, proxy_by_user_id)
            before = listings(client, int(row["a6_user_id"]))
            unexpected = [item for item in before if int(item.get("status") or 0) not in {0, 2}]
            if unexpected:
                raise RuntimeError("force_archive_listing_state_unexpected")
            active = [item for item in before if int(item.get("status") or 0) == 2]
            preview = {
                "merchant_id": args.merchant,
                "supplier_name": row["supplier_name"],
                "roles_before": list(roles),
                "roles_after": ["observer"],
                "withdrawable_micros_forfeited": int(row["withdrawable_micros"]),
                "user_balance_micros_forfeited": int(row["user_balance_micros"]),
                "active_listing_ids": [int(item["id"]) for item in active],
            }
            if not args.apply:
                print(json.dumps({"success": True, "mode": "dry-run", **preview}))
                return
            action_id, prior_status = prepare_action(connection, row, before)
            if prior_status == "completed":
                raise RuntimeError("force_archive_completed_action_state_mismatch")
            for item in active:
                client.takedown_supplier_listing(int(row["a6_user_id"]), int(item["id"]))
            after = listings(client, int(row["a6_user_id"]))
            if any(int(item.get("status") or 0) != 0 for item in after):
                raise RuntimeError("force_archive_takedown_readback_failed")
            complete_archive(connection, row, action_id, after)
            print(json.dumps({"success": True, "mode": "applied", **preview}))
    finally:
        connection.close()


if __name__ == "__main__":
    main()
