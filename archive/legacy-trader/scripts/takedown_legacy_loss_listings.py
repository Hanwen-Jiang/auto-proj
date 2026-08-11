#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row

from a6api_auto.client import A6ApiClient
from a6api_auto.storage import SessionStore, read_private_json

CONFIG_VERSION = "legacy-shared-key-stop-loss-20260725-v1"


@dataclass(frozen=True)
class LegacyTarget:
    merchant_id: int
    listing_id: int
    channel_id: int
    model: str
    prices: tuple[int, int, int, int]


TARGETS = (
    LegacyTarget(4, 2597, 768, "gpt-5.6-luna", (80_000, 480_000, 8_000, 100_000)),
    LegacyTarget(5, 3593, 1087, "gpt-5.6-sol", (350_000, 2_100_000, 35_000, 437_500)),
    LegacyTarget(5, 3595, 1087, "gpt-5.6-luna", (200_000, 1_200_000, 20_000, 250_000)),
    LegacyTarget(7, 2799, 832, "gpt-5.5", (2_200_000, 13_200_000, 220_000, 0)),
    LegacyTarget(19, 3835, 1158, "gpt-5.5", (450_000, 2_700_000, 45_000, 0)),
)


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(description="Takedown known loss-making legacy Listings")
    command.add_argument("--apply", action="store_true")
    command.add_argument(
        "--proxy-map-file",
        type=Path,
        default=Path("/var/lib/a6-trader/secrets/proxy-map.json"),
    )
    return command


def _listing(client: A6ApiClient, user_id: int, listing_id: int) -> dict[str, Any]:
    response = client.supplier_listings(user_id, page=1, size=1000)
    matches = [
        dict(item)
        for item in response.get("items") or []
        if int(item.get("id") or 0) == listing_id
    ]
    if len(matches) != 1:
        raise RuntimeError("legacy_stop_loss_listing_readback_not_unique")
    return matches[0]


def _public_listing(item: Mapping[str, Any]) -> dict[str, Any]:
    allowed = {
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
    }
    return {key: item.get(key) for key in sorted(allowed)}


def _required_int(item: Mapping[str, Any], key: str) -> int:
    value = item.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise RuntimeError(f"legacy_stop_loss_{key}_invalid")
    return value


def _active(item: Mapping[str, Any]) -> bool:
    return all(
        _required_int(item, key) == expected
        for key, expected in (
            ("status", 2),
            ("availability", 1),
            ("visibility", 1),
        )
    )


def _down(item: Mapping[str, Any]) -> bool:
    return _required_int(item, "status") == 0


def _matches_target(item: Mapping[str, Any], target: LegacyTarget) -> bool:
    observed_prices = tuple(
        _required_int(item, field)
        for field in (
            "input_price_micros",
            "output_price_micros",
            "cache_read_price_micros",
            "cache_write_price_micros",
        )
    )
    return all(
        (
            _required_int(item, "id") == target.listing_id,
            _required_int(item, "channel_id") == target.channel_id,
            item.get("own_model") == target.model,
            item.get("platform_model") == target.model,
            observed_prices == target.prices,
        )
    )


def _prepare_action(
    connection: Any,
    merchant_id: int,
    listing_id: int,
    before: Any,
) -> tuple[int, str, int | None]:
    key = f"legacy-stop-loss:{merchant_id}:{listing_id}:{CONFIG_VERSION}"
    desired = {
        "expected_status": 0,
        "reason": "legacy_enodot_cost_contract_missing",
    }
    with connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO a6_trade.trade_control_actions (
                idempotency_key, action_type, entity_type, entity_id,
                desired_state, observed_before, status, config_version,
                requested_by, started_at
            ) VALUES (
                %s, 'legacy_stop_loss_takedown', 'a6_supplier_listing', %s,
                %s::jsonb, %s::jsonb, 'started', %s,
                'takedown-legacy-loss-listings', now()
            )
            ON CONFLICT (idempotency_key) DO UPDATE SET
                observed_before = CASE
                    WHEN trade_control_actions.status = 'completed'
                    THEN trade_control_actions.observed_before
                    ELSE EXCLUDED.observed_before
                END,
                status = trade_control_actions.status,
                updated_at = now()
            RETURNING id, status
            """,
            (
                key,
                f"merchant:{merchant_id}:listing:{listing_id}",
                json.dumps(desired, sort_keys=True),
                json.dumps(before, sort_keys=True),
                CONFIG_VERSION,
            ),
        )
        action = cursor.fetchone()
        action_id = int(action["id"])
        prior_status = str(action["status"])
        attempt_no = None
        if prior_status != "completed":
            cursor.execute(
                """
                UPDATE a6_trade.trade_control_actions
                SET status = 'started', error_class = NULL,
                    started_at = now(), completed_at = NULL, elapsed_ms = NULL,
                    updated_at = now()
                WHERE id = %s
                """,
                (action_id,),
            )
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
                (action_id, attempt_no, json.dumps(before, sort_keys=True)),
            )
    connection.commit()
    return action_id, prior_status, attempt_no


def _complete(
    connection: Any,
    action_id: int,
    merchant_id: int,
    listing_id: int,
    after: Mapping[str, Any],
    elapsed_ms: int,
    attempt_no: int | None,
) -> None:
    payload = json.dumps(_public_listing(after), sort_keys=True)
    with connection.cursor() as cursor:
        cursor.execute(
            """
            UPDATE a6_trade.trade_control_actions
            SET observed_after = %s::jsonb, status = 'completed',
                error_class = NULL, completed_at = now(), elapsed_ms = %s,
                updated_at = now()
            WHERE id = %s
            """,
            (payload, elapsed_ms, action_id),
        )
        if attempt_no is not None:
            cursor.execute(
                """
                UPDATE a6_trade.trade_control_action_attempts
                SET observed_after = %s::jsonb, status = 'completed',
                    error_class = NULL, completed_at = now(), elapsed_ms = %s
                WHERE action_id = %s AND attempt_no = %s
                """,
                (payload, elapsed_ms, action_id, attempt_no),
            )
        cursor.execute(
            """
            UPDATE a6_trade.a6_supplier_listings_current
            SET status = %s, availability = %s, visibility = %s,
                observed_at = now(), updated_at = now()
            WHERE merchant_id = %s AND a6_listing_id = %s
            """,
            (
                _required_int(after, "status"),
                _required_int(after, "availability"),
                _required_int(after, "visibility"),
                merchant_id,
                listing_id,
            ),
        )
    connection.commit()


def _fail(
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


def main() -> None:
    args = parser().parse_args()
    dsn = os.environ.get("A6_TRADE_DATABASE_DSN", "").strip()
    if not dsn:
        raise RuntimeError("legacy_stop_loss_database_dsn_missing")
    proxy_by_user_id = {
        int(key): str(value) for key, value in read_private_json(args.proxy_map_file).items()
    }
    connection = psycopg.connect(dsn, row_factory=dict_row)
    results = []
    try:
        for target in TARGETS:
            merchant_id = target.merchant_id
            listing_id = target.listing_id
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT m.a6_user_id, m.session_ref, l.platform_model,
                           l.status, l.availability, l.visibility, l.managed_state
                    FROM a6_trade.a6_merchants m
                    JOIN a6_trade.a6_supplier_listings_current l ON l.merchant_id = m.id
                    WHERE m.id = %s AND l.a6_listing_id = %s
                    """,
                    (merchant_id, listing_id),
                )
                context = cursor.fetchone()
            if context is None or context["managed_state"] != "existing_unmanaged":
                raise RuntimeError("legacy_stop_loss_target_invalid")
            record = SessionStore(Path(context["session_ref"])).load()
            user_id = int(context["a6_user_id"])
            if record.user_id != user_id or user_id not in proxy_by_user_id:
                raise RuntimeError("legacy_stop_loss_session_invalid")
            client = A6ApiClient(record.origin, proxy_url=proxy_by_user_id[user_id])
            client.import_cookies(record.cookies)
            before = _listing(client, user_id, listing_id)
            if not _matches_target(before, target):
                raise RuntimeError("legacy_stop_loss_remote_fingerprint_mismatch")
            if _down(before):
                if args.apply:
                    action_id, _prior_status, attempt_no = _prepare_action(
                        connection,
                        merchant_id,
                        listing_id,
                        _public_listing(before),
                    )
                    _complete(
                        connection,
                        action_id,
                        merchant_id,
                        listing_id,
                        before,
                        0,
                        attempt_no,
                    )
                results.append(
                    {
                        "merchant_id": merchant_id,
                        "listing_id": listing_id,
                        "action": "noop",
                        "before": _public_listing(before),
                    }
                )
                continue
            if not _active(before):
                raise RuntimeError("legacy_stop_loss_remote_state_invalid")
            if not args.apply:
                results.append(
                    {
                        "merchant_id": merchant_id,
                        "listing_id": listing_id,
                        "action": "takedown",
                        "before": _public_listing(before),
                    }
                )
                continue
            action_id, prior_status, attempt_no = _prepare_action(
                connection,
                merchant_id,
                listing_id,
                _public_listing(before),
            )
            if prior_status == "completed" or attempt_no is None:
                raise RuntimeError("legacy_stop_loss_completed_action_remote_active")
            started = time.monotonic()
            try:
                client.takedown_supplier_listing(user_id, listing_id)
                after = _listing(client, user_id, listing_id)
                if not _down(after):
                    raise RuntimeError("legacy_stop_loss_readback_mismatch")
                elapsed_ms = max(0, round((time.monotonic() - started) * 1000))
                _complete(
                    connection,
                    action_id,
                    merchant_id,
                    listing_id,
                    after,
                    elapsed_ms,
                    attempt_no,
                )
                results.append(
                    {
                        "merchant_id": merchant_id,
                        "listing_id": listing_id,
                        "action": "takedown",
                        "confirmed": True,
                    }
                )
            except Exception as exc:
                elapsed_ms = max(0, round((time.monotonic() - started) * 1000))
                error_class = (
                    str(exc)
                    if str(exc).replace("_", "").isalnum()
                    else "legacy_stop_loss_failed"
                )
                _fail(connection, action_id, attempt_no, error_class, elapsed_ms)
                results.append(
                    {
                        "merchant_id": merchant_id,
                        "listing_id": listing_id,
                        "action": "error",
                        "error_class": error_class,
                    }
                )
        success = not any(item["action"] == "error" for item in results)
        print(json.dumps({"success": success, "apply": args.apply, "results": results}, indent=2))
        if not success:
            raise SystemExit(2)
    finally:
        connection.close()


if __name__ == "__main__":
    main()
