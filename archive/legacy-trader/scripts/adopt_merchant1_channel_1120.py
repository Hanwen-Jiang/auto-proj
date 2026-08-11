#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row

from a6api_auto.client import A6ApiClient
from a6api_auto.storage import SessionStore, read_private_json
from a6api_auto.trade_enodot_seller_resources import (
    PrivateFileManagedSellerSecretSink,
)
from a6api_auto.trade_managed_listing_store import PostgresManagedListingStore
from a6api_auto.trade_managed_listings import (
    ManagedListingController,
    ManagedListingSpec,
    ManagedMerchant,
    managed_marker,
    managed_resource_signature,
    stable_managed_uid,
)
from a6api_auto.trade_seller_orchestration import SellerPricing

CONFIG_VERSION = "merchant1-channel-1120-adoption-20260725-v1"
MODEL = "gpt-5.6-sol"
BASE_URL = "https://ai.enodot.shop/v1"
CHANNEL_LOGICAL_KEY = "seller-primary"
LISTING_DESCRIPTION = "Team 订阅来源，适合日常对话与代码任务"
SOURCE_FINGERPRINT = "92980d63e24290b2e14320eb093e2335db6fde57a30aa7536c284ef7fdd89940"


class AdoptionError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class PilotTarget:
    merchant_id: int = 1
    a6_user_id: int = 9645
    supplier_id: int = 308
    channel_id: int = 1120
    channel_binding_id: int = 1111
    approved_request_id: int = 1101
    pending_request_id: int = 1199
    session_fingerprint: str = (
        "1d5508fd887dc730eaceea2438bd0e136d7c90748406640f877c07d179f326ee"
    )
    inventory_source_hash: str = (
        "fcdf7821fd398da40bfd1b0c4d782fb79a8a4537a9ca2b82b95a787ca0634eb0"
    )
    source_fingerprint: str = SOURCE_FINGERPRINT

    @property
    def managed_uid(self) -> str:
        return stable_managed_uid("channel", self.merchant_id, CHANNEL_LOGICAL_KEY)


@dataclass(frozen=True)
class PinEvidence:
    pin_binding_id: int
    token_ref_id: int
    provider_merchant_id: int
    external_a6_token_id: int
    decision_id: int
    channel_id: int
    cost_multiplier: Decimal
    confirmed_at: datetime
    quote_id: int
    quote_scan_run_id: int
    quote_observed_at: datetime

    def public(self) -> dict[str, Any]:
        return {
            "pin_binding_id": self.pin_binding_id,
            "token_ref_id": self.token_ref_id,
            "provider_merchant_id": self.provider_merchant_id,
            "external_a6_token_id": self.external_a6_token_id,
            "decision_id": self.decision_id,
            "channel_id": self.channel_id,
            "cost_multiplier": str(self.cost_multiplier),
            "confirmed_at": self.confirmed_at.isoformat(),
            "quote_id": self.quote_id,
            "quote_scan_run_id": self.quote_scan_run_id,
            "quote_observed_at": self.quote_observed_at.isoformat(),
        }


@dataclass(frozen=True)
class DatabaseContext:
    merchant: Mapping[str, Any]
    seller: Mapping[str, Any]
    channel_binding: Mapping[str, Any]
    inventory: Mapping[str, Any]


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(
        description="Adopt approved Merchant 1 Channel 1120 and create its managed Listing"
    )
    command.add_argument("--apply", action="store_true", help="apply writes; default is dry-run")
    command.add_argument(
        "--proxy-map-file",
        type=Path,
        default=Path("/var/lib/a6-trader/secrets/proxy-map.json"),
    )
    command.add_argument(
        "--seller-key-dir",
        type=Path,
        default=Path("/var/lib/a6-trader/secrets/enodot-seller-keys"),
    )
    command.add_argument("--max-pin-age-seconds", type=int, default=900)
    return command


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _fingerprint(value: Any) -> str:
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _one(items: Sequence[Mapping[str, Any]], key: str, value: int, error: str) -> dict[str, Any]:
    matches = [dict(item) for item in items if _int(item.get(key)) == value]
    if len(matches) != 1:
        raise AdoptionError(error)
    return matches[0]


def _int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 0
    return parsed if parsed > 0 else 0


def _source_bundle(
    client: A6ApiClient,
    target: PilotTarget,
) -> dict[str, dict[str, Any]]:
    channels = client.supplier_channels(target.a6_user_id)
    requests = client.supplier_requests(target.a6_user_id)
    return {
        "channel": _one(channels, "id", target.channel_id, "adoption_channel_not_unique"),
        "approved_request": _one(
            requests,
            "id",
            target.approved_request_id,
            "adoption_approved_request_not_unique",
        ),
        "pending_request": _one(
            requests,
            "id",
            target.pending_request_id,
            "adoption_pending_request_not_unique",
        ),
    }


def _stable_post_bundle(
    bundle: Mapping[str, Mapping[str, Any]],
    *,
    target: PilotTarget,
    description: str,
    key_last4: str,
) -> dict[str, Any]:
    channel = bundle["channel"]
    approved = bundle["approved_request"]
    pending = bundle["pending_request"]
    expected_channel = {
        "id": target.channel_id,
        "binding_id": target.channel_binding_id,
        "supplier_id": target.supplier_id,
        "name": channel.get("name"),
        "nickname": channel.get("nickname"),
        "channel_type": 1,
        "status": 1,
        "availability": 1,
        "models": MODEL,
        "description": description,
    }
    expected_request = {
        "id": target.approved_request_id,
        "native_channel_id": target.channel_id,
        "supplier_id": target.supplier_id,
        "name": approved.get("name"),
        "nickname": approved.get("nickname"),
        "channel_type": 1,
        "status": 1,
        "base_url": BASE_URL,
        "models": MODEL,
        "test_model": MODEL,
        "description": description,
        "api_key_last4": key_last4,
    }
    guard = {
        "id": target.pending_request_id,
        "native_channel_id": 0,
        "supplier_id": target.supplier_id,
        "name": pending.get("name"),
        "status": 0,
    }
    ownership = {
        "channel": expected_channel,
        "request": expected_request,
        "request_guards": [guard],
    }
    ownership["fingerprint"] = _fingerprint(ownership)
    return ownership


def _matches(document: Mapping[str, Any], expected: Mapping[str, Any]) -> bool:
    return all(document.get(key) == value for key, value in expected.items())


def _verify_post_bundle(
    bundle: Mapping[str, Mapping[str, Any]],
    ownership: Mapping[str, Any],
) -> None:
    channel = ownership.get("channel")
    request = ownership.get("request")
    guards = ownership.get("request_guards")
    if (
        not isinstance(channel, Mapping)
        or not isinstance(request, Mapping)
        or not isinstance(guards, list)
        or len(guards) != 1
        or not isinstance(guards[0], Mapping)
        or not _matches(bundle["channel"], channel)
        or not _matches(bundle["approved_request"], request)
        or not _matches(bundle["pending_request"], guards[0])
    ):
        raise AdoptionError("adoption_remote_readback_mismatch")
    fingerprint = ownership.get("fingerprint")
    unsigned = {
        "channel": dict(channel),
        "request": dict(request),
        "request_guards": [dict(guards[0])],
    }
    if fingerprint != _fingerprint(unsigned):
        raise AdoptionError("adoption_ownership_fingerprint_invalid")


def _all_listings(client: A6ApiClient, user_id: int) -> list[dict[str, Any]]:
    response = client.supplier_listings(user_id, page=1, size=1000)
    items = response.get("items")
    if not isinstance(items, list):
        raise AdoptionError("adoption_listing_inventory_invalid")
    return [dict(item) for item in items if isinstance(item, Mapping)]


def _load_database_context(connection: Any, target: PilotTarget) -> DatabaseContext:
    with connection.cursor() as cursor:
        cursor.execute("SELECT * FROM a6_trade.a6_merchants WHERE id = %s", (target.merchant_id,))
        merchant = cursor.fetchone()
        cursor.execute(
            "SELECT * FROM a6_trade.enodot_managed_seller_resources WHERE merchant_id = %s",
            (target.merchant_id,),
        )
        seller = cursor.fetchone()
        cursor.execute(
            """
            SELECT * FROM a6_trade.a6_managed_channel_bindings
            WHERE managed_uid = %s
            """,
            (target.managed_uid,),
        )
        binding = cursor.fetchone()
        cursor.execute(
            """
            SELECT * FROM a6_trade.a6_supplier_channels_current
            WHERE merchant_id = %s AND a6_channel_id = %s
            """,
            (target.merchant_id, target.channel_id),
        )
        inventory = cursor.fetchone()
    if any(item is None for item in (merchant, seller, binding, inventory)):
        raise AdoptionError("adoption_database_context_missing")
    return DatabaseContext(merchant, seller, binding, inventory)


def _validate_database_context(context: DatabaseContext, target: PilotTarget) -> None:
    merchant = context.merchant
    seller = context.seller
    binding = context.channel_binding
    inventory = context.inventory
    if (
        _int(merchant.get("a6_user_id")) != target.a6_user_id
        or _int(merchant.get("supplier_id")) != target.supplier_id
        or tuple(merchant.get("roles") or ()) != ("seller", "observer")
        or merchant.get("status") != "supplier_submitted"
        or str(merchant.get("session_fingerprint")) != target.session_fingerprint
        or _int(seller.get("merchant_id")) != target.merchant_id
        or seller.get("control_state") != "bound"
        or _int(seller.get("group_id")) != 26
        or Decimal(str(seller.get("observed_delta"))) != Decimal("0.5")
        or not seller.get("api_key_fingerprint")
        or not seller.get("key_secret_ref")
        or _int(binding.get("merchant_id")) != target.merchant_id
        or binding.get("managed_uid") != target.managed_uid
        or _int(inventory.get("a6_channel_id")) != target.channel_id
        or _int(inventory.get("supplier_id")) != target.supplier_id
        or _int(inventory.get("status")) != 1
        or _int(inventory.get("availability")) != 1
    ):
        raise AdoptionError("adoption_database_fingerprint_mismatch")
    already_adopted = (
        binding.get("control_state") == "verified"
        and _int(binding.get("a6_channel_id")) == target.channel_id
    )
    if not already_adopted and (
        binding.get("control_state") not in {"verifying", "updating", "update_unknown"}
        or _int(binding.get("a6_channel_id")) != 0
        or str(inventory.get("source_hash")) != target.inventory_source_hash
    ):
        raise AdoptionError("adoption_binding_state_mismatch")
    desired = binding.get("desired_state")
    remote = desired.get("remote_channel") if isinstance(desired, Mapping) else None
    if (
        not isinstance(remote, Mapping)
        or remote.get("base_url") != BASE_URL
        or remote.get("models") != MODEL
        or remote.get("api_key_sha256") != str(seller.get("api_key_fingerprint"))
    ):
        raise AdoptionError("adoption_managed_spec_mismatch")


def _load_pin_evidence(
    connection: Any,
    *,
    max_age_seconds: int,
) -> PinEvidence:
    if max_age_seconds < 60:
        raise AdoptionError("adoption_pin_max_age_invalid")
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT b.id AS pin_binding_id, b.a6_token_id AS token_ref_id,
                   t.merchant_id AS provider_merchant_id,
                   t.a6_token_id AS external_a6_token_id,
                   b.decision_id, b.channel_id, b.confirmed_at,
                   d.expected_cost_multiplier, d.decision_status,
                   q.id AS quote_id, q.scan_run_id AS quote_scan_run_id,
                   q.final_multiplier AS quote_multiplier,
                   q.observed_at AS quote_observed_at
            FROM a6_trade.a6_pin_bindings b
            JOIN a6_trade.a6_tokens t ON t.id = b.a6_token_id
            JOIN a6_trade.a6_merchants m ON m.id = t.merchant_id
            JOIN a6_trade.a6_pin_decisions d ON d.id = b.decision_id
            JOIN LATERAL (
                SELECT q.* FROM a6_trade.a6_market_quotes q
                WHERE q.channel_id = b.channel_id AND q.model = b.model
                ORDER BY q.observed_at DESC, q.id DESC LIMIT 1
            ) q ON true
            WHERE b.valid_to IS NULL AND b.model = %s
              AND d.decision_status = 'completed'
              AND d.selected_channel_id = b.channel_id
              AND 'provider' = ANY(m.roles)
            ORDER BY b.confirmed_at DESC, b.id DESC
            LIMIT 1
            """,
            (MODEL,),
        )
        row = cursor.fetchone()
    if row is None:
        raise AdoptionError("adoption_current_provider_pin_missing")
    k = Decimal(str(row.get("expected_cost_multiplier")))
    quote_k = Decimal(str(row.get("quote_multiplier")))
    confirmed = row.get("confirmed_at")
    quote_at = row.get("quote_observed_at")
    now = datetime.now(timezone.utc)
    if (
        k <= 0
        or k != quote_k
        or not isinstance(confirmed, datetime)
        or not isinstance(quote_at, datetime)
        or (now - confirmed).total_seconds() > max_age_seconds
        or (now - quote_at).total_seconds() > max_age_seconds
    ):
        raise AdoptionError("adoption_provider_pin_fingerprint_invalid")
    return PinEvidence(
        pin_binding_id=_int(row.get("pin_binding_id")),
        token_ref_id=_int(row.get("token_ref_id")),
        provider_merchant_id=_int(row.get("provider_merchant_id")),
        external_a6_token_id=_int(row.get("external_a6_token_id")),
        decision_id=_int(row.get("decision_id")),
        channel_id=_int(row.get("channel_id")),
        cost_multiplier=k,
        confirmed_at=confirmed,
        quote_id=_int(row.get("quote_id")),
        quote_scan_run_id=_int(row.get("quote_scan_run_id")),
        quote_observed_at=quote_at,
    )


def _pricing(pin: PinEvidence) -> SellerPricing:
    return SellerPricing(
        cost_multiplier=pin.cost_multiplier,
        delta=Decimal("0.5"),
        margin=Decimal("0.2"),
        conversion_factor=Decimal("0.833333333333"),
        headroom=Decimal("0.02"),
    )


def _action_key(target: PilotTarget) -> str:
    return (
        f"managed-channel-adopt:{target.managed_uid}:{target.channel_id}:"
        f"{target.source_fingerprint[:20]}"
    )


def _claim_adoption(
    connection: Any,
    target: PilotTarget,
    desired: Mapping[str, Any],
    before: Mapping[str, Any],
) -> tuple[int, int]:
    key = _action_key(target)
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT * FROM a6_trade.a6_managed_channel_bindings
            WHERE managed_uid = %s FOR UPDATE
            """,
            (target.managed_uid,),
        )
        binding = cursor.fetchone()
        if (
            binding is None
            or _int(binding.get("merchant_id")) != target.merchant_id
            or _int(binding.get("a6_channel_id")) != 0
            or binding.get("control_state") not in {"verifying", "updating", "update_unknown"}
        ):
            raise AdoptionError("adoption_claim_binding_conflict")
        cursor.execute(
            """
            INSERT INTO a6_trade.trade_control_actions (
                idempotency_key, action_type, entity_type, entity_id,
                desired_state, observed_before, status, config_version,
                requested_by, started_at
            ) VALUES (
                %s, 'adopt_managed_channel', 'a6_managed_channel', %s,
                %s::jsonb, %s::jsonb, 'started', %s,
                'adopt-merchant1-channel-1120', now()
            )
            ON CONFLICT (idempotency_key) DO UPDATE SET
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
            RETURNING id, status, desired_state
            """,
            (key, target.managed_uid, _json(desired), _json(before), CONFIG_VERSION),
        )
        action = cursor.fetchone()
        if action is None or action.get("desired_state") != desired:
            raise AdoptionError("adoption_action_fingerprint_conflict")
        if action.get("status") == "completed":
            raise AdoptionError("adoption_action_already_completed_binding_mismatch")
        action_id = _int(action.get("id"))
        cursor.execute(
            """
            SELECT coalesce(max(attempt_no), 0) + 1 AS attempt_no
            FROM a6_trade.trade_control_action_attempts WHERE action_id = %s
            """,
            (action_id,),
        )
        attempt_no = _int(cursor.fetchone().get("attempt_no"))
        cursor.execute(
            """
            INSERT INTO a6_trade.trade_control_action_attempts (
                action_id, attempt_no, status, observed_before, started_at
            ) VALUES (%s, %s, 'started', %s::jsonb, now())
            """,
            (action_id, attempt_no, _json(before)),
        )
        cursor.execute(
            """
            UPDATE a6_trade.a6_managed_channel_bindings
            SET control_state = 'updating', last_action_id = %s,
                last_attempted_at = now(), updated_at = now()
            WHERE managed_uid = %s
            """,
            (action_id, target.managed_uid),
        )
    connection.commit()
    return action_id, attempt_no


def _mark_unknown(
    connection: Any,
    target: PilotTarget,
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
            SET status = 'unknown', error_class = %s, elapsed_ms = %s, updated_at = now()
            WHERE id = %s
            """,
            (error_class, elapsed_ms, action_id),
        )
        cursor.execute(
            """
            UPDATE a6_trade.trade_control_action_attempts
            SET status = 'unknown', error_class = %s, error_fingerprint = %s,
                completed_at = now(), elapsed_ms = %s
            WHERE action_id = %s AND attempt_no = %s
            """,
            (error_class, fingerprint, elapsed_ms, action_id, attempt_no),
        )
        cursor.execute(
            """
            UPDATE a6_trade.a6_managed_channel_bindings
            SET control_state = 'update_unknown', last_error_class = %s, updated_at = now()
            WHERE managed_uid = %s AND last_action_id = %s
            """,
            (error_class, target.managed_uid, action_id),
        )
    connection.commit()


def _complete_adoption(
    connection: Any,
    target: PilotTarget,
    action_id: int,
    attempt_no: int,
    ownership: Mapping[str, Any],
    observed: Mapping[str, Any],
    elapsed_ms: int,
) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT * FROM a6_trade.a6_managed_channel_bindings
            WHERE managed_uid = %s FOR UPDATE
            """,
            (target.managed_uid,),
        )
        binding = cursor.fetchone()
        if (
            binding is None
            or _int(binding.get("merchant_id")) != target.merchant_id
            or _int(binding.get("a6_channel_id")) != 0
            or _int(binding.get("last_action_id")) != action_id
            or binding.get("control_state") not in {"updating", "update_unknown"}
        ):
            raise AdoptionError("adoption_complete_binding_conflict")
        desired = dict(binding.get("desired_state") or {})
        desired["adoption"] = dict(ownership)
        old_create_action = _int(binding.get("create_action_id"))
        cursor.execute(
            """
            UPDATE a6_trade.a6_managed_channel_bindings
            SET a6_channel_id = %s, control_state = 'verified',
                desired_state = %s::jsonb, observed_state = %s::jsonb,
                create_action_id = %s, last_action_id = %s,
                last_error_class = NULL, last_observed_at = now(),
                verified_at = now(), updated_at = now()
            WHERE managed_uid = %s
            """,
            (
                target.channel_id,
                _json(desired),
                _json(observed),
                action_id,
                action_id,
                target.managed_uid,
            ),
        )
        cursor.execute(
            """
            UPDATE a6_trade.trade_control_actions
            SET observed_after = %s::jsonb, status = 'completed', error_class = NULL,
                completed_at = now(), elapsed_ms = %s, updated_at = now()
            WHERE id = %s
            """,
            (_json(observed), elapsed_ms, action_id),
        )
        cursor.execute(
            """
            UPDATE a6_trade.trade_control_action_attempts
            SET observed_after = %s::jsonb, status = 'completed', error_class = NULL,
                completed_at = now(), elapsed_ms = %s
            WHERE action_id = %s AND attempt_no = %s
            """,
            (_json(observed), elapsed_ms, action_id, attempt_no),
        )
        if old_create_action and old_create_action != action_id:
            cursor.execute(
                """
                UPDATE a6_trade.trade_control_actions
                SET status = 'superseded',
                    error_class = 'managed_channel_create_superseded_by_adoption',
                    completed_at = now(), updated_at = now()
                WHERE id = %s AND status <> 'completed'
                """,
                (old_create_action,),
            )
            cursor.execute(
                """
                UPDATE a6_trade.trade_control_action_attempts
                SET status = 'superseded',
                    error_class = 'managed_channel_create_superseded_by_adoption',
                    completed_at = now()
                WHERE action_id = %s AND completed_at IS NULL
                """,
                (old_create_action,),
            )
    connection.commit()


def _listing_spec(pricing: SellerPricing, pin: PinEvidence) -> ManagedListingSpec:
    input_price = pricing.prices[0]
    # A6 persists the other gpt-5.6-sol prices from the rounded input price.
    output_price = input_price * 6
    cache_read_price = (input_price + 9) // 10
    cache_write_price = (input_price * 5 + 3) // 4
    return ManagedListingSpec(
        logical_key=MODEL,
        input_price_micros=input_price,
        output_price_micros=output_price,
        cache_read_price_micros=cache_read_price,
        cache_write_price_micros=cache_write_price,
        display_name=MODEL,
        description=LISTING_DESCRIPTION,
        own_model=MODEL,
        platform_model=MODEL,
    )


def _listing_spec_from_binding(binding: Any) -> ManagedListingSpec:
    desired = binding.desired_state
    remote = desired.get("remote_listing") if isinstance(desired, Mapping) else None
    if not isinstance(remote, Mapping):
        raise AdoptionError("adoption_listing_recovery_spec_missing")
    try:
        return ManagedListingSpec(
            logical_key=MODEL,
            input_price_micros=int(remote["input_price_micros"]),
            output_price_micros=int(remote["output_price_micros"]),
            cache_read_price_micros=int(remote["cache_read_price_micros"]),
            cache_write_price_micros=int(remote["cache_write_price_micros"]),
            display_name=MODEL,
            description=str(remote.get("description") or LISTING_DESCRIPTION),
            own_model=str(remote["own_model"]),
            platform_model=str(remote["platform_model"]),
        )
    except (KeyError, TypeError, ValueError):
        raise AdoptionError("adoption_listing_recovery_spec_invalid") from None


def _public_bundle(bundle: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    return {
        "source_fingerprint": _fingerprint(bundle),
        "channel": {
            key: bundle["channel"].get(key)
            for key in (
                "id",
                "binding_id",
                "supplier_id",
                "name",
                "status",
                "availability",
                "models",
                "description",
            )
        },
        "approved_request": {
            key: bundle["approved_request"].get(key)
            for key in (
                "id",
                "native_channel_id",
                "supplier_id",
                "status",
                "base_url",
                "models",
                "test_model",
                "description",
                "api_key_last4",
            )
        },
        "pending_request": {
            key: bundle["pending_request"].get(key)
            for key in ("id", "native_channel_id", "supplier_id", "name", "status")
        },
    }


def main() -> None:
    args = parser().parse_args()
    dsn = os.environ.get("A6_TRADE_DATABASE_DSN", "").strip()
    if not dsn:
        raise AdoptionError("adoption_database_dsn_missing")
    target = PilotTarget()
    connection = psycopg.connect(dsn, row_factory=dict_row)
    try:
        context = _load_database_context(connection, target)
        _validate_database_context(context, target)
        pin = _load_pin_evidence(connection, max_age_seconds=args.max_pin_age_seconds)
        pricing = _pricing(pin)
        secret_sink = PrivateFileManagedSellerSecretSink(args.seller_key_dir)
        seller = context.seller
        api_key = secret_sink.read_api_key(
            str(seller["managed_uid"]),
            str(seller["key_secret_ref"]),
        )
        if hashlib.sha256(api_key.encode()).hexdigest() != str(seller["api_key_fingerprint"]):
            raise AdoptionError("adoption_seller_key_fingerprint_mismatch")
        session = SessionStore(Path(context.merchant["session_ref"])).load()
        if (
            session.user_id != target.a6_user_id
            or session.supplier_id != target.supplier_id
            or session.status != "supplier_submitted"
        ):
            raise AdoptionError("adoption_session_identity_mismatch")
        proxy_map = read_private_json(args.proxy_map_file)
        proxy_url = proxy_map.get(str(target.a6_user_id), proxy_map.get(target.a6_user_id))
        if not isinstance(proxy_url, str) or not proxy_url:
            raise AdoptionError("adoption_proxy_mapping_missing")
        client = A6ApiClient(session.origin, proxy_url=proxy_url)
        client.import_cookies(session.cookies)
        bundle = _source_bundle(client, target)
        description = managed_resource_signature("channel", target.managed_uid)
        ownership = _stable_post_bundle(
            bundle,
            target=target,
            description=description,
            key_last4=api_key[-4:],
        )
        binding = context.channel_binding
        already_adopted = (
            binding.get("control_state") == "verified"
            and _int(binding.get("a6_channel_id")) == target.channel_id
        )
        remote_mode = "source"
        if already_adopted:
            persisted = (binding.get("desired_state") or {}).get("adoption")
            if persisted != ownership:
                raise AdoptionError("adoption_persisted_ownership_mismatch")
            _verify_post_bundle(bundle, ownership)
            remote_mode = "adopted"
        elif _fingerprint(bundle) != target.source_fingerprint:
            try:
                _verify_post_bundle(bundle, ownership)
            except AdoptionError:
                raise AdoptionError("adoption_source_fingerprint_mismatch") from None
            remote_mode = "patched_uncommitted"
        channel_listings = [
            item for item in _all_listings(client, target.a6_user_id)
            if _int(item.get("channel_id")) == target.channel_id
        ]
        if not already_adopted and channel_listings:
            raise AdoptionError("adoption_channel_not_empty")
        result: dict[str, Any] = {
            "success": True,
            "mode": "applied" if args.apply else "dry-run",
            "merchant_id": target.merchant_id,
            "channel_id": target.channel_id,
            "channel_binding_id": target.channel_binding_id,
            "remote_mode": remote_mode,
            "channel": _public_bundle(bundle),
            "pin": pin.public(),
            "pricing": pricing.public(),
            "planned_patch": {
                "base_url": BASE_URL,
                "models": MODEL,
                "test_model": MODEL,
                "description": description,
                "api_key_sha256": str(seller["api_key_fingerprint"]),
            },
            "listing": {
                "action": "reconcile" if already_adopted else "create_after_adoption",
                "model": MODEL,
                "prices_micros": pricing.public()["prices_micros"],
            },
        }
        if not args.apply:
            print(json.dumps(result, ensure_ascii=True, indent=2))
            return
        if not already_adopted:
            action_desired = {
                "target": {
                    "merchant_id": target.merchant_id,
                    "a6_user_id": target.a6_user_id,
                    "supplier_id": target.supplier_id,
                    "channel_id": target.channel_id,
                    "channel_binding_id": target.channel_binding_id,
                    "approved_request_id": target.approved_request_id,
                    "pending_request_id": target.pending_request_id,
                },
                "source_fingerprint": target.source_fingerprint,
                "ownership": ownership,
                "api_key_sha256": str(seller["api_key_fingerprint"]),
            }
            started = time.monotonic()
            action_id, attempt_no = _claim_adoption(
                connection,
                target,
                action_desired,
                _public_bundle(bundle),
            )
            try:
                fresh = _source_bundle(client, target)
                if _fingerprint(fresh) == target.source_fingerprint:
                    client.update_supplier_channel(
                        target.a6_user_id,
                        target.channel_binding_id,
                        {
                            "base_url": BASE_URL,
                            "api_key": api_key,
                            "models": MODEL,
                            "test_model": MODEL,
                            "description": description,
                        },
                    )
                    fresh = _source_bundle(client, target)
                _verify_post_bundle(fresh, ownership)
                elapsed_ms = int((time.monotonic() - started) * 1000)
                _complete_adoption(
                    connection,
                    target,
                    action_id,
                    attempt_no,
                    ownership,
                    _public_bundle(fresh),
                    elapsed_ms,
                )
                result["channel_action_id"] = action_id
                result["channel"] = _public_bundle(fresh)
            except Exception as exc:
                elapsed_ms = int((time.monotonic() - started) * 1000)
                code = exc.code if isinstance(exc, AdoptionError) else type(exc).__name__
                _mark_unknown(connection, target, action_id, attempt_no, str(code), elapsed_ms)
                raise
        latest_pin = _load_pin_evidence(connection, max_age_seconds=args.max_pin_age_seconds)
        latest_pricing = _pricing(latest_pin)
        listing_store = PostgresManagedListingStore(
            lambda: psycopg.connect(dsn, row_factory=dict_row),
            config_version=CONFIG_VERSION,
            requested_by="adopt-merchant1-channel-1120",
        )
        controller = ManagedListingController(listing_store, lambda _merchant: client)
        merchant = ManagedMerchant(
            merchant_id=target.merchant_id,
            a6_user_id=target.a6_user_id,
            supplier_id=target.supplier_id,
            roles=("seller", "observer"),
        )
        listing_uid = stable_managed_uid(
            "listing",
            target.merchant_id,
            f"{MODEL}:{managed_marker(target.managed_uid)}",
        )
        listing_binding = listing_store.get_listing(listing_uid)
        recovered = None
        if (
            listing_binding is not None
            and listing_binding.a6_listing_id is None
            and listing_binding.control_state in {"creating", "create_unknown", "verifying"}
        ):
            recovered = controller.ensure_listing(
                merchant,
                target.managed_uid,
                _listing_spec_from_binding(listing_binding),
                apply=True,
            )
            if recovered.state != "verified" or not recovered.external_id:
                raise AdoptionError("adoption_listing_recovery_pending")
            listing_binding = listing_store.get_listing(listing_uid)
        if (
            listing_binding is not None
            and listing_binding.control_state == "verified"
            and listing_binding.a6_listing_id
        ):
            listing = controller.update_listing(
                merchant,
                listing_uid,
                _listing_spec(latest_pricing, latest_pin),
                apply=True,
            )
        else:
            listing = controller.ensure_listing(
                merchant,
                target.managed_uid,
                _listing_spec(latest_pricing, latest_pin),
                apply=True,
            )
        result["pin"] = latest_pin.public()
        result["pricing"] = latest_pricing.public()
        result["listing"] = listing.public()
        if recovered is not None:
            result["listing_recovery"] = recovered.public()
        result["listing_readback"] = [
            item
            for item in _all_listings(client, target.a6_user_id)
            if _int(item.get("id")) == _int(listing.external_id)
        ]
        if len(result["listing_readback"]) != 1:
            raise AdoptionError("adoption_listing_final_readback_failed")
        print(json.dumps(result, ensure_ascii=True, indent=2))
    finally:
        connection.close()


if __name__ == "__main__":
    try:
        main()
    except AdoptionError as exc:
        print(json.dumps({"success": False, "error": exc.code}), file=sys.stderr)
        raise SystemExit(1) from None
