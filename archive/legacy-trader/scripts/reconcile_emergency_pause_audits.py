#!/usr/bin/env python3
# ruff: noqa: I001
from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
from collections import defaultdict
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row

from a6api_auto.client import A6ApiClient
from a6api_auto.storage import SessionStore, read_private_json
try:
    from scripts.emergency_pause_a6_model_listings import (
        Target,
        listing_inventory,
        public_listing,
        seller_lock,
        validate_listing,
    )
except ModuleNotFoundError:
    from emergency_pause_a6_model_listings import (
        Target,
        listing_inventory,
        public_listing,
        seller_lock,
        validate_listing,
    )

CONFIG_VERSION = "emergency-pause-audit-readback-v1"


def _exact_integer(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def remote_confirmed_down(item: Mapping[str, Any]) -> bool:
    return all(
        _exact_integer(item.get(field)) == 0
        for field in ("status", "availability", "visibility")
    )


def failed_targets(connection: Any, model: str) -> tuple[tuple[int, Target], ...]:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT action.id AS action_id, binding.merchant_id, merchant.a6_user_id,
                   merchant.session_ref, listing.a6_listing_id,
                   listing.a6_channel_id, listing.platform_model
            FROM a6_trade.trade_control_actions action
            JOIN a6_trade.a6_managed_listing_bindings binding
              ON action.entity_id = concat(
                  'merchant:', binding.merchant_id,
                  ':listing:', binding.a6_listing_id
              )
            JOIN a6_trade.a6_supplier_listings_current listing
              ON listing.merchant_id = binding.merchant_id
             AND listing.a6_listing_id = binding.a6_listing_id
             AND listing.a6_channel_id = binding.a6_channel_id
            JOIN a6_trade.a6_merchants merchant
              ON merchant.id = binding.merchant_id
            WHERE action.action_type = 'emergency_model_pause'
              AND action.entity_type = 'a6_supplier_listing'
              AND action.status = 'failed'
              AND action.desired_state ->> 'model' = %s
              AND listing.platform_model = %s
              AND action.desired_state ->> 'reason' = 'cost_band_ceiling_exceeded'
              AND binding.control_state <> 'retired'
              AND merchant.a6_user_id IS NOT NULL
              AND merchant.roles @> ARRAY['seller']::text[]
            ORDER BY merchant.a6_user_id, listing.a6_listing_id
            """,
            (model, model),
        )
        rows = cursor.fetchall()
    return tuple(
        (
            int(row["action_id"]),
            Target(
                merchant_id=int(row["merchant_id"]),
                a6_user_id=int(row["a6_user_id"]),
                session_ref=Path(str(row["session_ref"])),
                listing_id=int(row["a6_listing_id"]),
                channel_id=int(row["a6_channel_id"]),
                model=str(row["platform_model"]),
            ),
        )
        for row in rows
    )


def reconcile_confirmed_down(
    connection: Any,
    *,
    action_id: int,
    target: Target,
    remote: Mapping[str, Any],
    config_version: str = CONFIG_VERSION,
) -> None:
    validate_listing(remote, target)
    if not remote_confirmed_down(remote):
        raise RuntimeError("emergency_pause_reconcile_not_confirmed_down")
    observed = public_listing(remote)
    observed_json = json.dumps(observed, ensure_ascii=True, sort_keys=True)
    source_hash = hashlib.sha256(observed_json.encode()).hexdigest()
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT id, status, action_type, entity_type, entity_id, desired_state
            FROM a6_trade.trade_control_actions
            WHERE id = %s
            FOR UPDATE
            """,
            (action_id,),
        )
        action = cursor.fetchone()
        expected_entity = f"merchant:{target.merchant_id}:listing:{target.listing_id}"
        if (
            action is None
            or _exact_integer(action.get("id")) != action_id
            or action["status"] != "failed"
            or action["action_type"] != "emergency_model_pause"
            or action["entity_type"] != "a6_supplier_listing"
            or action["entity_id"] != expected_entity
        ):
            raise RuntimeError("emergency_pause_reconcile_action_conflict")
        desired = action.get("desired_state")
        if isinstance(desired, str):
            desired = json.loads(desired)
        if (
            not isinstance(desired, Mapping)
            or desired.get("model") != target.model
            or _exact_integer(desired.get("merchant_id")) != target.merchant_id
            or _exact_integer(desired.get("listing_id")) != target.listing_id
            or _exact_integer(desired.get("channel_id")) != target.channel_id
            or _exact_integer(desired.get("status")) != 0
            or desired.get("reason") != "cost_band_ceiling_exceeded"
        ):
            raise RuntimeError("emergency_pause_reconcile_action_conflict")
        cursor.execute(
            """
            SELECT coalesce(max(attempt_no), 0) + 1 AS attempt_no
            FROM a6_trade.trade_control_action_attempts
            WHERE action_id = %s
            """,
            (action_id,),
        )
        attempt_no = int(cursor.fetchone()["attempt_no"])
        reconciliation = json.dumps(
            {
                "source": "remote_supplier_listing_readback",
                "config_version": config_version,
                "listing": observed,
            },
            ensure_ascii=True,
            sort_keys=True,
        )
        cursor.execute(
            """
            UPDATE a6_trade.trade_control_actions
            SET observed_after = %s::jsonb, status = 'completed',
                error_class = NULL, completed_at = now(), elapsed_ms = 0,
                updated_at = now()
            WHERE id = %s AND status = 'failed'
              AND action_type = 'emergency_model_pause'
              AND entity_type = 'a6_supplier_listing'
              AND entity_id = %s
            """,
            (observed_json, action_id, expected_entity),
        )
        if cursor.rowcount != 1:
            raise RuntimeError("emergency_pause_reconcile_action_cas_mismatch")
        cursor.execute(
            """
            INSERT INTO a6_trade.trade_control_action_attempts (
                action_id, attempt_no, status, observed_before, observed_after,
                started_at, completed_at, elapsed_ms
            ) VALUES (
                %s, %s, 'completed', %s::jsonb, %s::jsonb,
                now(), now(), 0
            )
            """,
            (action_id, attempt_no, reconciliation, observed_json),
        )
        cursor.execute(
            """
            UPDATE a6_trade.a6_supplier_listings_current
            SET status = 0, availability = 0, visibility = 0,
                observed_at = now(), source_hash = %s, updated_at = now()
            WHERE merchant_id = %s AND a6_listing_id = %s
              AND a6_channel_id = %s AND platform_model = %s
            """,
            (
                source_hash,
                target.merchant_id,
                target.listing_id,
                target.channel_id,
                target.model,
            ),
        )
        if cursor.rowcount != 1:
            raise RuntimeError("emergency_pause_reconcile_inventory_missing")
    connection.commit()


def run(
    connection: Any,
    candidates: Sequence[tuple[int, Target]],
    proxy_by_user_id: Mapping[int, str],
    *,
    timeout: float,
    apply: bool,
) -> dict[str, Any]:
    grouped: dict[int, list[tuple[int, Target]]] = defaultdict(list)
    for candidate in candidates:
        grouped[candidate[1].a6_user_id].append(candidate)
    results = []
    for user_id, user_targets in grouped.items():
        representative = user_targets[0][1]
        try:
            record = SessionStore(representative.session_ref).load()
            proxy = str(proxy_by_user_id.get(user_id) or "").strip()
            if record.user_id != user_id or not record.cookies or not proxy:
                raise RuntimeError("emergency_pause_reconcile_session_invalid")
            client = A6ApiClient(record.origin, proxy_url=proxy, timeout=timeout)
            client.import_cookies(record.cookies)
            inventory = listing_inventory(client, user_id)
            for action_id, target in user_targets:
                remote = inventory.get(target.listing_id)
                if remote is None:
                    raise RuntimeError("emergency_pause_reconcile_listing_missing")
                validate_listing(remote, target)
                confirmed = remote_confirmed_down(remote)
                if confirmed and apply:
                    reconcile_confirmed_down(
                        connection,
                        action_id=action_id,
                        target=target,
                        remote=remote,
                    )
                results.append(
                    {
                        "action_id": action_id,
                        "merchant_id": target.merchant_id,
                        "listing_id": target.listing_id,
                        "confirmed_down": confirmed,
                        "action": "reconciled" if confirmed and apply else "observed",
                    }
                )
        except Exception as exc:
            connection.rollback()
            code = str(exc) if str(exc).replace("_", "").isalnum() else type(exc).__name__
            known = {(item["action_id"], item["listing_id"]) for item in results}
            results.extend(
                {
                    "action_id": action_id,
                    "merchant_id": target.merchant_id,
                    "listing_id": target.listing_id,
                    "confirmed_down": False,
                    "action": "error",
                    "error_class": code,
                }
                for action_id, target in user_targets
                if (action_id, target.listing_id) not in known
            )
    confirmed = sum(item["confirmed_down"] for item in results)
    return {
        "success": all(item["action"] != "error" for item in results),
        "mode": "applied" if apply else "dry-run",
        "failed_audit_count": len(candidates),
        "remote_confirmed_down_count": confirmed,
        "remaining_failed_count": len(candidates) - (confirmed if apply else 0),
        "results": results,
    }


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(
        description="Reconcile failed emergency pause audits from remote Listing readback"
    )
    command.add_argument("--model", default="gpt-5.6-sol")
    command.add_argument("--apply", action="store_true")
    command.add_argument("--timeout-seconds", type=float, default=15)
    command.add_argument(
        "--proxy-map-file",
        type=Path,
        default=Path("/var/lib/a6-trader/secrets/proxy-map.json"),
    )
    return command


def main() -> None:
    args = parser().parse_args()
    if not args.model.strip() or args.timeout_seconds <= 0:
        raise RuntimeError("emergency_pause_reconcile_arguments_invalid")
    dsn = str(os.environ.get("A6_TRADE_DATABASE_DSN") or "").strip()
    if not dsn:
        raise RuntimeError("emergency_pause_reconcile_database_dsn_missing")
    proxy_file = args.proxy_map_file.expanduser().resolve()
    if not stat.S_ISREG(proxy_file.stat().st_mode) or proxy_file.stat().st_mode & 0o077:
        raise RuntimeError("emergency_pause_reconcile_proxy_map_invalid")
    proxy_map = {
        int(key): str(value).strip() for key, value in read_private_json(proxy_file).items()
    }
    connection = psycopg.connect(dsn, row_factory=dict_row)
    try:
        with seller_lock():
            result = run(
                connection,
                failed_targets(connection, args.model.strip()),
                proxy_map,
                timeout=args.timeout_seconds,
                apply=args.apply,
            )
        print(json.dumps(result, ensure_ascii=True, separators=(",", ":")))
        if not result["success"]:
            raise SystemExit(2)
    finally:
        connection.close()


if __name__ == "__main__":
    main()
