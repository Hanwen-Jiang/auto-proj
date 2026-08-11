#!/usr/bin/env python3
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import time
from collections import defaultdict
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row

from a6api_auto.client import A6ApiClient
from a6api_auto.storage import SessionStore, read_private_json

CONFIG_VERSION = "emergency-no-loss-model-pause-v1"
LOCK_FILE = Path("/var/lib/a6-trader/seller-control.lock")


@dataclass(frozen=True)
class Target:
    merchant_id: int
    a6_user_id: int
    session_ref: Path
    listing_id: int
    channel_id: int
    model: str


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(
        description="Pause every active managed A6 Seller listing for one model"
    )
    command.add_argument("--model", default="gpt-5.6-sol")
    command.add_argument("--apply", action="store_true")
    command.add_argument("--timeout-seconds", type=float, default=15)
    command.add_argument("--attempts", type=int, default=3)
    command.add_argument(
        "--proxy-map-file",
        type=Path,
        default=Path("/var/lib/a6-trader/secrets/proxy-map.json"),
    )
    command.add_argument("--config-version", default=CONFIG_VERSION)
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


def targets(connection: Any, model: str) -> tuple[Target, ...]:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT binding.merchant_id, merchant.a6_user_id,
                   merchant.session_ref, listing.a6_listing_id,
                   listing.a6_channel_id, listing.platform_model
            FROM a6_trade.a6_managed_listing_bindings binding
            JOIN a6_trade.a6_supplier_listings_current listing
              ON listing.merchant_id = binding.merchant_id
             AND listing.a6_listing_id = binding.a6_listing_id
             AND listing.a6_channel_id = binding.a6_channel_id
            JOIN a6_trade.a6_merchants merchant
              ON merchant.id = binding.merchant_id
            WHERE listing.platform_model = %s
              AND listing.status = 2
              AND listing.availability = 1
              AND listing.visibility = 1
              AND binding.control_state <> 'retired'
              AND merchant.a6_user_id IS NOT NULL
              AND merchant.status = 'supplier_submitted'
              AND merchant.roles @> ARRAY['seller']::text[]
            ORDER BY merchant.a6_user_id, listing.a6_listing_id
            """,
            (model,),
        )
        rows = cursor.fetchall()
    return tuple(
        Target(
            merchant_id=int(row["merchant_id"]),
            a6_user_id=int(row["a6_user_id"]),
            session_ref=Path(str(row["session_ref"])),
            listing_id=int(row["a6_listing_id"]),
            channel_id=int(row["a6_channel_id"]),
            model=str(row["platform_model"]),
        )
        for row in rows
    )


def client_for(
    target: Target,
    proxy_by_user_id: Mapping[int, str],
    *,
    timeout: float,
) -> A6ApiClient:
    record = SessionStore(target.session_ref).load()
    proxy_url = str(proxy_by_user_id.get(target.a6_user_id) or "").strip()
    if record.user_id != target.a6_user_id or not record.cookies or not proxy_url:
        raise RuntimeError("emergency_pause_session_invalid")
    client = A6ApiClient(record.origin, proxy_url=proxy_url, timeout=timeout)
    client.import_cookies(record.cookies)
    return client


def listing_inventory(client: A6ApiClient, user_id: int) -> dict[int, dict[str, Any]]:
    result: dict[int, dict[str, Any]] = {}
    for page in range(1, 101):
        document = client.supplier_listings(user_id, page=page, size=100)
        items = document.get("items")
        if not isinstance(items, list):
            raise RuntimeError("emergency_pause_listing_inventory_invalid")
        for item in items:
            if not isinstance(item, Mapping) or int(item.get("id") or 0) < 1:
                raise RuntimeError("emergency_pause_listing_inventory_invalid")
            result[int(item["id"])] = dict(item)
        total = int(document.get("total") or 0)
        if not items or len(items) < 100 or (total and len(result) >= total):
            return result
    raise RuntimeError("emergency_pause_listing_inventory_incomplete")


def public_listing(item: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: item.get(key)
        for key in (
            "id",
            "channel_id",
            "own_model",
            "platform_model",
            "status",
            "availability",
            "visibility",
            "input_price_micros",
            "output_price_micros",
            "cache_read_price_micros",
            "cache_write_price_micros",
        )
    }


def validate_listing(item: Mapping[str, Any], target: Target) -> None:
    if (
        int(item.get("id") or 0) != target.listing_id
        or int(item.get("channel_id") or 0) != target.channel_id
        or str(item.get("platform_model") or "") != target.model
    ):
        raise RuntimeError("emergency_pause_listing_identity_mismatch")
    if int(item.get("status") or 0) not in {0, 2}:
        raise RuntimeError("emergency_pause_listing_state_unknown")


def is_down(item: Mapping[str, Any]) -> bool:
    try:
        return all(int(item.get(field)) == 0 for field in ("status", "availability", "visibility"))
    except (TypeError, ValueError):
        return False


def prepare_action(
    connection: Any,
    target: Target,
    before: Mapping[str, Any],
    *,
    config_version: str,
) -> tuple[int, int]:
    key = f"emergency-model-pause:{target.model}:{target.merchant_id}:{target.listing_id}"
    desired = {
        "model": target.model,
        "merchant_id": target.merchant_id,
        "listing_id": target.listing_id,
        "channel_id": target.channel_id,
        "status": 0,
        "reason": "cost_band_ceiling_exceeded",
    }
    with connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO a6_trade.trade_control_actions (
                idempotency_key, action_type, entity_type, entity_id,
                desired_state, observed_before, status, config_version,
                requested_by, started_at
            ) VALUES (
                %s, 'emergency_model_pause', 'a6_supplier_listing', %s,
                %s::jsonb, %s::jsonb, 'started', %s,
                'emergency-pause-a6-model-listings', now()
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
                key,
                f"merchant:{target.merchant_id}:listing:{target.listing_id}",
                json.dumps(desired, sort_keys=True),
                json.dumps(public_listing(before), sort_keys=True),
                config_version,
            ),
        )
        action = cursor.fetchone()
        action_id = int(action["id"])
        cursor.execute(
            """
            SELECT coalesce(max(attempt_no), 0) + 1 AS attempt_no
            FROM a6_trade.trade_control_action_attempts
            WHERE action_id = %s
            """,
            (action_id,),
        )
        attempt_no = int(cursor.fetchone()["attempt_no"])
        cursor.execute(
            """
            INSERT INTO a6_trade.trade_control_action_attempts (
                action_id, attempt_no, status, observed_before, started_at
            ) VALUES (%s, %s, 'started', %s::jsonb, now())
            """,
            (
                action_id,
                attempt_no,
                json.dumps(public_listing(before), sort_keys=True),
            ),
        )
    connection.commit()
    return action_id, attempt_no


def complete_action(
    connection: Any,
    target: Target,
    action_id: int,
    attempt_no: int,
    after: Mapping[str, Any],
    elapsed_ms: int,
) -> None:
    observed = json.dumps(public_listing(after), sort_keys=True)
    with connection.cursor() as cursor:
        cursor.execute(
            """
            UPDATE a6_trade.trade_control_actions
            SET observed_after = %s::jsonb, status = 'completed',
                error_class = NULL, completed_at = now(), elapsed_ms = %s,
                updated_at = now()
            WHERE id = %s
            """,
            (observed, elapsed_ms, action_id),
        )
        cursor.execute(
            """
            UPDATE a6_trade.trade_control_action_attempts
            SET observed_after = %s::jsonb, status = 'completed',
                error_class = NULL, completed_at = now(), elapsed_ms = %s
            WHERE action_id = %s AND attempt_no = %s
            """,
            (observed, elapsed_ms, action_id, attempt_no),
        )
        cursor.execute(
            """
            UPDATE a6_trade.a6_supplier_listings_current
            SET status = 0, availability = 0, visibility = 0,
                observed_at = now(), updated_at = now()
            WHERE merchant_id = %s AND a6_listing_id = %s
              AND a6_channel_id = %s
            """,
            (target.merchant_id, target.listing_id, target.channel_id),
        )
        if cursor.rowcount != 1:
            raise RuntimeError("emergency_pause_local_readback_missing")
    connection.commit()


def fail_action(
    connection: Any,
    action_id: int,
    attempt_no: int,
    error_class: str,
    elapsed_ms: int,
) -> None:
    fingerprint = hashlib.sha256(error_class.encode()).hexdigest()
    with connection.cursor() as cursor:
        cursor.execute(
            """
            UPDATE a6_trade.trade_control_actions
            SET status = 'failed', error_class = %s, completed_at = now(),
                elapsed_ms = %s, updated_at = now()
            WHERE id = %s
            """,
            (error_class, elapsed_ms, action_id),
        )
        cursor.execute(
            """
            UPDATE a6_trade.trade_control_action_attempts
            SET status = 'failed', error_class = %s, error_fingerprint = %s,
                completed_at = now(), elapsed_ms = %s
            WHERE action_id = %s AND attempt_no = %s
            """,
            (error_class, fingerprint, elapsed_ms, action_id, attempt_no),
        )
    connection.commit()


def error_class(exc: Exception) -> str:
    value = str(exc).strip()
    if value and value.replace("_", "").isalnum():
        return value[:200]
    return "emergency_pause_remote_failed"


def process_target(
    connection: Any,
    target: Target,
    client: A6ApiClient,
    before: Mapping[str, Any],
    *,
    config_version: str,
    attempts: int,
) -> dict[str, Any]:
    validate_listing(before, target)
    if is_down(before):
        return {
            "merchant_id": target.merchant_id,
            "listing_id": target.listing_id,
            "action": "noop",
            "confirmed": True,
        }
    action_id, attempt_no = prepare_action(
        connection,
        target,
        before,
        config_version=config_version,
    )
    started = time.monotonic()
    last_error: Exception | None = None
    for index in range(attempts):
        try:
            if index == 0:
                client.takedown_supplier_listing(target.a6_user_id, target.listing_id)
            inventory = listing_inventory(client, target.a6_user_id)
            after = inventory.get(target.listing_id)
            if after is None:
                raise RuntimeError("emergency_pause_listing_missing")
            validate_listing(after, target)
            if not is_down(after):
                if index + 1 < attempts:
                    client.takedown_supplier_listing(target.a6_user_id, target.listing_id)
                    continue
                raise RuntimeError("emergency_pause_readback_mismatch")
            elapsed_ms = max(0, round((time.monotonic() - started) * 1000))
            complete_action(connection, target, action_id, attempt_no, after, elapsed_ms)
            return {
                "merchant_id": target.merchant_id,
                "listing_id": target.listing_id,
                "action": "takedown",
                "confirmed": True,
            }
        except Exception as exc:
            last_error = exc
            if index + 1 < attempts:
                time.sleep(0.5 * (index + 1))
    assert last_error is not None
    elapsed_ms = max(0, round((time.monotonic() - started) * 1000))
    code = error_class(last_error)
    fail_action(connection, action_id, attempt_no, code, elapsed_ms)
    return {
        "merchant_id": target.merchant_id,
        "listing_id": target.listing_id,
        "action": "error",
        "confirmed": False,
        "error_class": code,
    }


def main() -> None:
    args = parser().parse_args()
    if not args.model.strip() or args.timeout_seconds <= 0 or args.attempts < 1:
        raise RuntimeError("emergency_pause_arguments_invalid")
    dsn = os.environ.get("A6_TRADE_DATABASE_DSN", "").strip()
    if not dsn:
        raise RuntimeError("emergency_pause_database_dsn_missing")
    proxy_by_user_id = {
        int(key): str(value).strip()
        for key, value in read_private_json(args.proxy_map_file).items()
    }
    connection = psycopg.connect(dsn, row_factory=dict_row)
    try:
        with seller_lock():
            selected = targets(connection, args.model.strip())
            preview = {
                "model": args.model.strip(),
                "target_listing_count": len(selected),
                "target_user_count": len({item.a6_user_id for item in selected}),
            }
            if not args.apply:
                print(json.dumps({"success": True, "mode": "dry-run", **preview}))
                return
            by_user: dict[int, list[Target]] = defaultdict(list)
            for target in selected:
                by_user[target.a6_user_id].append(target)
            results: list[dict[str, Any]] = []
            for user_targets in by_user.values():
                representative = user_targets[0]
                try:
                    client = client_for(
                        representative,
                        proxy_by_user_id,
                        timeout=args.timeout_seconds,
                    )
                    inventory = listing_inventory(client, representative.a6_user_id)
                    for target in user_targets:
                        before = inventory.get(target.listing_id)
                        if before is None:
                            raise RuntimeError("emergency_pause_listing_missing")
                        result = process_target(
                            connection,
                            target,
                            client,
                            before,
                            config_version=args.config_version,
                            attempts=args.attempts,
                        )
                        results.append(result)
                        if result["confirmed"]:
                            inventory[target.listing_id] = {
                                **before,
                                "status": 0,
                                "availability": 0,
                                "visibility": 0,
                            }
                except Exception as exc:
                    code = error_class(exc)
                    results.extend(
                        {
                            "merchant_id": target.merchant_id,
                            "listing_id": target.listing_id,
                            "action": "error",
                            "confirmed": False,
                            "error_class": code,
                        }
                        for target in user_targets
                        if not any(
                            item["merchant_id"] == target.merchant_id
                            and item["listing_id"] == target.listing_id
                            for item in results
                        )
                    )
            failed = [item for item in results if not item["confirmed"]]
            print(
                json.dumps(
                    {
                        "success": not failed,
                        "mode": "applied",
                        **preview,
                        "confirmed_count": len(results) - len(failed),
                        "failed_count": len(failed),
                        "results": results,
                    },
                    ensure_ascii=True,
                    separators=(",", ":"),
                )
            )
            if failed:
                raise SystemExit(2)
    finally:
        connection.close()


if __name__ == "__main__":
    main()
