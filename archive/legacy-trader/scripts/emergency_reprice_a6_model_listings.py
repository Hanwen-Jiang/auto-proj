#!/usr/bin/env python3
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row

from a6api_auto.client import A6ApiClient
from a6api_auto.storage import SessionStore, read_private_json
from a6api_auto.trade_seller_orchestration import GPT_5_6_SOL_OFFICIAL_PRICES

CONFIG_VERSION = "emergency-weighted-pin-margin-reprice-v3"
LOCK_FILE = Path("/var/lib/a6-trader/seller-control.lock")
SUPPORTED_MODEL = "gpt-5.6-sol"
MINIMUM_MARGIN = Decimal("0.20")
DEFAULT_CONVERSION_FACTOR = Decimal("0.833333333333")

_PRICE_FIELDS = (
    "input_price_micros",
    "output_price_micros",
    "cache_read_price_micros",
    "cache_write_price_micros",
)
_PRICE_KEYS = ("input", "output", "cache_read", "cache_write")


@dataclass(frozen=True)
class Target:
    merchant_id: int
    a6_user_id: int
    session_ref: Path
    managed_uid: str
    listing_id: int
    channel_id: int
    model: str
    observed_delta: Decimal | None
    local_prices: tuple[int, int, int, int]

    @property
    def effective_delta(self) -> Decimal:
        return self.observed_delta if self.observed_delta is not None else Decimal("0")


@dataclass(frozen=True)
class PricePlan:
    cost_multiplier: Decimal
    landed_cost_multiplier: Decimal
    margin: Decimal
    conversion_factor: Decimal
    headroom: Decimal
    delta: Decimal
    minimum_final_multiplier: Decimal
    minimum_panel_multiplier: Decimal
    panel_multiplier: Decimal
    final_multiplier: Decimal
    prices: tuple[int, int, int, int]

    @property
    def required_final_revenue_multiplier(self) -> Decimal:
        return (
            (Decimal("1") + self.margin)
            * self.landed_cost_multiplier
            * (Decimal("1") + self.headroom)
        )

    @property
    def cost_markup(self) -> Decimal:
        return self.final_multiplier / self.landed_cost_multiplier - Decimal("1")

    def profit_gate(self) -> dict[str, Any]:
        return {
            "formula": (
                "final=(1+delta)*P >= (1+margin)*(weighted_pin_cost/conversion_factor)*(1+headroom)"
            ),
            "final_revenue_multiplier": decimal_text(self.final_multiplier),
            "all_landed_cost_multiplier": decimal_text(self.landed_cost_multiplier),
            "required_final_revenue_multiplier": decimal_text(
                self.required_final_revenue_multiplier
            ),
            "cost_markup": decimal_text(self.cost_markup),
            "passed": self.final_multiplier >= self.required_final_revenue_multiplier,
        }

    def public(self) -> dict[str, Any]:
        return {
            "cost_multiplier": decimal_text(self.cost_multiplier),
            "landed_cost_multiplier": decimal_text(self.landed_cost_multiplier),
            "margin": decimal_text(self.margin),
            "conversion_factor": decimal_text(self.conversion_factor),
            "headroom": decimal_text(self.headroom),
            "delta": decimal_text(self.delta),
            "minimum_final_multiplier": decimal_text(self.minimum_final_multiplier),
            "minimum_panel_multiplier": decimal_text(self.minimum_panel_multiplier),
            "price_multiplier": decimal_text(self.panel_multiplier),
            "final_multiplier": decimal_text(self.final_multiplier),
            "prices_micros": dict(zip(_PRICE_KEYS, self.prices, strict=True)),
            "profit_gate": self.profit_gate(),
        }


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(
        description=(
            "Raise every managed, down A6 Seller listing for gpt-5.6-sol to a "
            "confirmed landed-cost margin floor"
        )
    )
    command.add_argument("--model", default=SUPPORTED_MODEL)
    command.add_argument(
        "--cost-multiplier",
        required=True,
        help="Confirmed upstream K in the pricing contract coordinate",
    )
    command.add_argument("--margin", default=decimal_text(MINIMUM_MARGIN))
    command.add_argument(
        "--conversion-factor",
        default=decimal_text(DEFAULT_CONVERSION_FACTOR),
    )
    command.add_argument("--headroom", default="0")
    command.add_argument("--apply", action="store_true")
    command.add_argument("--timeout-seconds", type=float, default=15)
    command.add_argument("--readback-attempts", type=int, default=5)
    command.add_argument("--readback-interval-seconds", type=float, default=0.5)
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


def decimal_text(value: Decimal) -> str:
    return format(value, "f")


def decimal_value(
    value: Any,
    code: str,
    *,
    minimum: Decimal,
    maximum: Decimal | None = None,
) -> Decimal:
    if isinstance(value, bool):
        raise RuntimeError(code)
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise RuntimeError(code) from None
    if not result.is_finite() or result < minimum or (maximum is not None and result > maximum):
        raise RuntimeError(code)
    return result


def required_nonnegative_int(item: Mapping[str, Any], field: str) -> int:
    value = item.get(field)
    if isinstance(value, bool):
        raise RuntimeError(f"emergency_reprice_{field}_invalid")
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise RuntimeError(f"emergency_reprice_{field}_invalid") from None
    if parsed < 0 or str(value).strip() != str(parsed):
        raise RuntimeError(f"emergency_reprice_{field}_invalid")
    return parsed


def optional_int(item: Mapping[str, Any], field: str) -> int | None:
    value = item.get(field)
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise RuntimeError(f"emergency_reprice_{field}_invalid")
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise RuntimeError(f"emergency_reprice_{field}_invalid") from None
    if str(value).strip() != str(parsed):
        raise RuntimeError(f"emergency_reprice_{field}_invalid")
    return parsed


def limited_text(value: Any, limit: int) -> str:
    return str(value or "")[:limit]


def targets(connection: Any, model: str) -> tuple[Target, ...]:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT binding.merchant_id, merchant.a6_user_id,
                   merchant.session_ref, binding.managed_uid,
                   listing.a6_listing_id, listing.a6_channel_id,
                   listing.platform_model,
                   CASE WHEN seller.control_state = 'bound'
                        THEN seller.observed_delta END AS observed_delta,
                   listing.input_price_micros, listing.output_price_micros,
                   listing.cache_read_price_micros,
                   listing.cache_write_price_micros
            FROM a6_trade.a6_managed_listing_bindings binding
            JOIN a6_trade.a6_supplier_listings_current listing
              ON listing.merchant_id = binding.merchant_id
             AND listing.a6_listing_id = binding.a6_listing_id
             AND listing.a6_channel_id = binding.a6_channel_id
            JOIN a6_trade.a6_merchants merchant
              ON merchant.id = binding.merchant_id
            LEFT JOIN a6_trade.enodot_managed_seller_resources seller
              ON seller.merchant_id = binding.merchant_id
             AND seller.control_state <> 'retired'
            WHERE listing.platform_model = %s
              AND listing.status = 0
              AND binding.control_state <> 'retired'
              AND merchant.a6_user_id IS NOT NULL
              AND merchant.status = 'supplier_submitted'
              AND merchant.roles @> ARRAY['seller']::text[]
            ORDER BY merchant.a6_user_id, listing.a6_listing_id
            """,
            (model,),
        )
        rows = cursor.fetchall()
    selected = []
    for row in rows:
        raw_delta = row.get("observed_delta")
        delta = (
            None
            if raw_delta is None
            else decimal_value(
                raw_delta,
                "emergency_reprice_observed_delta_invalid",
                minimum=Decimal("0"),
                maximum=Decimal("0.5"),
            )
        )
        selected.append(
            Target(
                merchant_id=int(row["merchant_id"]),
                a6_user_id=int(row["a6_user_id"]),
                session_ref=Path(str(row["session_ref"])),
                managed_uid=str(row["managed_uid"]),
                listing_id=int(row["a6_listing_id"]),
                channel_id=int(row["a6_channel_id"]),
                model=str(row["platform_model"]),
                observed_delta=delta,
                local_prices=tuple(required_nonnegative_int(row, field) for field in _PRICE_FIELDS),
            )
        )
    return tuple(selected)


def client_for(
    target: Target,
    proxy_by_user_id: Mapping[int, str],
    *,
    timeout: float,
) -> A6ApiClient:
    record = SessionStore(target.session_ref).load()
    proxy_url = str(proxy_by_user_id.get(target.a6_user_id) or "").strip()
    if record.user_id != target.a6_user_id or not record.cookies or not proxy_url:
        raise RuntimeError("emergency_reprice_session_invalid")
    client = A6ApiClient(record.origin, proxy_url=proxy_url, timeout=timeout)
    client.import_cookies(record.cookies)
    return client


def listing_inventory(client: A6ApiClient, user_id: int) -> dict[int, dict[str, Any]]:
    result: dict[int, dict[str, Any]] = {}
    for page in range(1, 101):
        document = client.supplier_listings(user_id, page=page, size=100)
        items = document.get("items")
        if not isinstance(items, list):
            raise RuntimeError("emergency_reprice_listing_inventory_invalid")
        for item in items:
            if not isinstance(item, Mapping) or int(item.get("id") or 0) < 1:
                raise RuntimeError("emergency_reprice_listing_inventory_invalid")
            result[int(item["id"])] = dict(item)
        total = int(document.get("total") or 0)
        if not items or len(items) < 100 or (total and len(result) >= total):
            return result
    raise RuntimeError("emergency_reprice_listing_inventory_incomplete")


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
            *_PRICE_FIELDS,
        )
    }


def normalized_listing(target: Target, item: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "merchant_user_id": target.a6_user_id,
        "a6_listing_id": required_nonnegative_int(item, "id"),
        "a6_channel_id": required_nonnegative_int(item, "channel_id"),
        "own_model": limited_text(item.get("own_model"), 256),
        "platform_model": limited_text(item.get("platform_model"), 256),
        "charge_type": limited_text(item.get("charge_type"), 64),
        **dict(zip(_PRICE_FIELDS, listing_prices(item), strict=True)),
        "status": optional_int(item, "status"),
        "availability": optional_int(item, "availability"),
        "visibility": optional_int(item, "visibility"),
        "display_name": limited_text(item.get("display_name"), 256),
        "description": limited_text(item.get("description"), 2000),
    }


def listing_source_hash(target: Target, item: Mapping[str, Any]) -> str:
    payload = normalized_listing(target, item)
    encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def audit_observation(
    target: Target,
    after: Mapping[str, Any],
    *,
    action: str,
    activation_recovery: Mapping[str, Any] | None = None,
    error_code: str | None = None,
) -> dict[str, Any]:
    readback = public_listing(after)
    listing_observation = {
        "action": action,
        "state": "verified" if error_code is None else "error",
        "applied": action != "noop",
        "external_id": target.listing_id,
        "error_class": error_code,
        "readback": readback,
    }
    if activation_recovery is not None:
        listing_observation["activation_recovery"] = dict(activation_recovery)
    return {
        "merchant_id": target.merchant_id,
        "a6_user_id": target.a6_user_id,
        "readiness": "ready",
        "seller": {
            "action": "noop",
            "state": "bound" if target.observed_delta is not None else "unverified",
            "verified": target.observed_delta is not None,
            "applied": False,
        },
        "channel": {"action": "noop", "state": "verified", "applied": False},
        "listing": listing_observation,
        "error_class": error_code,
    }


def validate_listing_identity(item: Mapping[str, Any], target: Target) -> None:
    if (
        int(item.get("id") or 0) != target.listing_id
        or int(item.get("channel_id") or 0) != target.channel_id
        or str(item.get("platform_model") or "") != target.model
    ):
        raise RuntimeError("emergency_reprice_listing_identity_mismatch")


def listing_is_down(item: Mapping[str, Any]) -> bool:
    return all(optional_int(item, field) == 0 for field in ("status", "availability", "visibility"))


def activation_signal(item: Mapping[str, Any]) -> bool:
    for field in ("status", "availability", "visibility"):
        if field in item and item.get(field) not in {None, ""} and optional_int(item, field) != 0:
            return True
    return False


def validate_down_listing(item: Mapping[str, Any], target: Target) -> None:
    validate_listing_identity(item, target)
    if not listing_is_down(item):
        raise RuntimeError("emergency_reprice_listing_not_down")


def recover_unexpected_activation(
    client: A6ApiClient,
    target: Target,
    observed_activation: Mapping[str, Any],
    *,
    source: str,
    attempts: int,
    interval_seconds: float,
) -> tuple[dict[str, Any] | None, dict[str, Any], Exception | None]:
    evidence: dict[str, Any] = {
        "detected": True,
        "source": source,
        "observed_activation": public_listing(observed_activation),
        "takedown_attempts": 0,
        "takedown_errors": [],
        "final_down_confirmed": False,
        "final_readback": None,
    }
    last_error: Exception = RuntimeError("emergency_reprice_activation_recovery_failed")
    for index in range(attempts):
        evidence["takedown_attempts"] = int(evidence["takedown_attempts"]) + 1
        try:
            client.takedown_supplier_listing(target.a6_user_id, target.listing_id)
        except Exception as exc:
            evidence["takedown_errors"].append(error_class(exc))
            last_error = exc
        try:
            inventory = listing_inventory(client, target.a6_user_id)
            after = inventory.get(target.listing_id)
            if after is None:
                raise RuntimeError("emergency_reprice_listing_missing")
            validate_listing_identity(after, target)
            evidence["final_readback"] = public_listing(after)
            if listing_is_down(after):
                evidence["final_down_confirmed"] = True
                return after, evidence, None
            last_error = RuntimeError("emergency_reprice_activation_still_live")
        except Exception as exc:
            last_error = exc
        if index + 1 < attempts and interval_seconds:
            time.sleep(interval_seconds)
    return None, evidence, last_error


def prices_from_input(input_price: int) -> tuple[int, int, int, int]:
    if isinstance(input_price, bool) or not isinstance(input_price, int) or input_price < 0:
        raise RuntimeError("emergency_reprice_input_price_invalid")
    return (
        input_price,
        input_price * 6,
        (input_price + 5) // 10,
        (input_price * 5 + 2) // 4,
    )


def minimum_normalized_input(current_prices: Sequence[int]) -> int:
    if len(current_prices) != 4:
        raise RuntimeError("emergency_reprice_prices_invalid")
    current = tuple(int(value) for value in current_prices)
    if any(value < 0 for value in current):
        raise RuntimeError("emergency_reprice_prices_invalid")
    input_price, output_price, cache_read_price, cache_write_price = current
    return max(
        input_price,
        (output_price + 5) // 6,
        max(0, cache_read_price * 10 - 5),
        max(0, (cache_write_price * 4 - 2 + 4) // 5),
    )


def build_price_plan(
    current_prices: Sequence[int],
    *,
    cost_multiplier: Decimal,
    delta: Decimal,
    margin: Decimal = MINIMUM_MARGIN,
    conversion_factor: Decimal = DEFAULT_CONVERSION_FACTOR,
    headroom: Decimal = Decimal("0"),
) -> PricePlan:
    cost = decimal_value(
        cost_multiplier,
        "emergency_reprice_cost_multiplier_invalid",
        minimum=Decimal("0.000000000001"),
    )
    effective_delta = decimal_value(
        delta,
        "emergency_reprice_delta_invalid",
        minimum=Decimal("0"),
        maximum=Decimal("0.5"),
    )
    selected_margin = decimal_value(
        margin,
        "emergency_reprice_margin_invalid",
        minimum=MINIMUM_MARGIN,
        maximum=Decimal("10"),
    )
    conversion = decimal_value(
        conversion_factor,
        "emergency_reprice_conversion_factor_invalid",
        minimum=Decimal("0.000000000001"),
        maximum=Decimal("1"),
    )
    selected_headroom = decimal_value(
        headroom,
        "emergency_reprice_headroom_invalid",
        minimum=Decimal("0"),
        maximum=Decimal("10"),
    )
    landed_cost = cost / conversion
    required_final_revenue = (
        (Decimal("1") + selected_margin) * landed_cost * (Decimal("1") + selected_headroom)
    )
    minimum_final = required_final_revenue
    minimum_panel = minimum_final / (Decimal("1") + effective_delta)
    base_input = GPT_5_6_SOL_OFFICIAL_PRICES[0]
    floor_input = int(
        (Decimal(base_input) * minimum_panel).to_integral_value(rounding=ROUND_CEILING)
    )
    target_input = max(floor_input, minimum_normalized_input(current_prices))
    prices = prices_from_input(target_input)
    current = tuple(int(value) for value in current_prices)
    if any(after < before for before, after in zip(current, prices, strict=True)):
        raise RuntimeError("emergency_reprice_price_decrease_detected")
    panel = Decimal(target_input) / Decimal(base_input)
    final = (Decimal("1") + effective_delta) * panel
    if final < required_final_revenue:
        raise RuntimeError("emergency_reprice_margin_floor_violated")
    return PricePlan(
        cost_multiplier=cost,
        landed_cost_multiplier=landed_cost,
        margin=selected_margin,
        conversion_factor=conversion,
        headroom=selected_headroom,
        delta=effective_delta,
        minimum_final_multiplier=minimum_final,
        minimum_panel_multiplier=minimum_panel,
        panel_multiplier=panel,
        final_multiplier=final,
        prices=prices,
    )


def listing_prices(item: Mapping[str, Any]) -> tuple[int, int, int, int]:
    return tuple(required_nonnegative_int(item, field) for field in _PRICE_FIELDS)


def update_payload(item: Mapping[str, Any], plan: PricePlan) -> dict[str, Any]:
    charge_type = item.get("charge_type")
    if not isinstance(charge_type, str) or not charge_type.strip():
        raise RuntimeError("emergency_reprice_charge_type_invalid")
    return {
        "charge_type": charge_type,
        **dict(zip(_PRICE_FIELDS, plan.prices, strict=True)),
        "display_name": str(item.get("display_name") or ""),
        "description": str(item.get("description") or ""),
    }


def desired_state(target: Target, plan: PricePlan) -> dict[str, Any]:
    pricing = plan.public()
    return {
        "model": target.model,
        "merchant_id": target.merchant_id,
        "listing_managed_uid": target.managed_uid,
        "listing_id": target.listing_id,
        "channel_id": target.channel_id,
        "cost_multiplier": decimal_text(plan.cost_multiplier),
        "margin": decimal_text(plan.margin),
        "conversion_factor": decimal_text(plan.conversion_factor),
        "headroom": decimal_text(plan.headroom),
        "profit_gate": plan.profit_gate(),
        "target_final_multiplier": decimal_text(plan.final_multiplier),
        "minimum_final_multiplier": decimal_text(plan.minimum_final_multiplier),
        "a6_panel_multiplier": decimal_text(plan.panel_multiplier),
        "delta": decimal_text(plan.delta),
        "delta_source": (
            "remote_observed" if target.observed_delta is not None else "conservative_zero"
        ),
        "prices_micros": dict(zip(_PRICE_KEYS, plan.prices, strict=True)),
        "pricing": pricing,
        "reason": "emergency_weighted_pin_margin_floor",
    }


def _transaction(connection: Any, operation: Any) -> Any:
    try:
        result = operation()
        connection.commit()
        return result
    except Exception:
        connection.rollback()
        raise


def prepare_action(
    connection: Any,
    target: Target,
    before: Mapping[str, Any],
    plan: PricePlan,
    *,
    config_version: str,
) -> tuple[int, int]:
    desired = desired_state(target, plan)
    digest = hashlib.sha256(
        json.dumps(desired, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    key = f"emergency-reprice:{target.model}:{target.listing_id}:{digest[:32]}"

    def write() -> tuple[int, int]:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO a6_trade.trade_control_actions (
                    idempotency_key, action_type, entity_type, entity_id,
                    desired_state, observed_before, status, config_version,
                    requested_by, started_at
                ) VALUES (
                    %s, 'apply_seller_price_band', 'a6_managed_listing', %s,
                    %s::jsonb, %s::jsonb, 'started', %s,
                    'emergency-reprice-a6-model-listings', now()
                )
                ON CONFLICT (idempotency_key) DO UPDATE SET
                    desired_state = EXCLUDED.desired_state,
                    observed_before = EXCLUDED.observed_before,
                    status = 'started', error_class = NULL,
                    started_at = now(), completed_at = NULL, elapsed_ms = NULL,
                    updated_at = now()
                RETURNING id
                """,
                (
                    key,
                    target.managed_uid,
                    json.dumps(desired, sort_keys=True),
                    json.dumps(public_listing(before), sort_keys=True),
                    config_version,
                ),
            )
            action_id = int(cursor.fetchone()["id"])
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
        return action_id, attempt_no

    return _transaction(connection, write)


def complete_action(
    connection: Any,
    target: Target,
    action_id: int,
    attempt_no: int,
    after: Mapping[str, Any],
    elapsed_ms: int,
    *,
    operation: str,
    activation_recovery: Mapping[str, Any] | None = None,
) -> None:
    normalized = normalized_listing(target, after)
    observed = json.dumps(
        audit_observation(
            target,
            after,
            action=operation,
            activation_recovery=activation_recovery,
        ),
        sort_keys=True,
    )
    source_hash = listing_source_hash(target, after)

    def write() -> None:
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
            if cursor.rowcount != 1:
                raise RuntimeError("emergency_reprice_action_missing")
            cursor.execute(
                """
                UPDATE a6_trade.trade_control_action_attempts
                SET observed_after = %s::jsonb, status = 'completed',
                    error_class = NULL, completed_at = now(), elapsed_ms = %s
                WHERE action_id = %s AND attempt_no = %s
                """,
                (observed, elapsed_ms, action_id, attempt_no),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("emergency_reprice_attempt_missing")
            cursor.execute(
                """
                UPDATE a6_trade.a6_supplier_listings_current
                SET own_model = %s, platform_model = %s, charge_type = %s,
                    input_price_micros = %s, output_price_micros = %s,
                    cache_read_price_micros = %s, cache_write_price_micros = %s,
                    status = %s, availability = %s, visibility = %s,
                    display_name = %s, description = %s,
                    source_hash = %s, observed_at = now(), updated_at = now()
                WHERE merchant_id = %s AND a6_listing_id = %s
                  AND a6_channel_id = %s
                """,
                (
                    normalized["own_model"],
                    normalized["platform_model"],
                    normalized["charge_type"],
                    *(normalized[field] for field in _PRICE_FIELDS),
                    normalized["status"],
                    normalized["availability"],
                    normalized["visibility"],
                    normalized["display_name"],
                    normalized["description"],
                    source_hash,
                    target.merchant_id,
                    target.listing_id,
                    target.channel_id,
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("emergency_reprice_local_readback_missing")

    _transaction(connection, write)


def fail_action(
    connection: Any,
    action_id: int,
    attempt_no: int,
    error_code: str,
    elapsed_ms: int,
    *,
    target: Target | None = None,
    after: Mapping[str, Any] | None = None,
    activation_recovery: Mapping[str, Any] | None = None,
) -> None:
    fingerprint = hashlib.sha256(error_code.encode()).hexdigest()
    observed_after = None
    if target is not None and after is not None:
        observed_after = json.dumps(
            audit_observation(
                target,
                after,
                action="activation_recovery_failed",
                activation_recovery=activation_recovery,
                error_code=error_code,
            ),
            sort_keys=True,
        )

    def write() -> None:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE a6_trade.trade_control_actions
                SET observed_after = %s::jsonb, status = 'failed',
                    error_class = %s, completed_at = now(),
                    elapsed_ms = %s, updated_at = now()
                WHERE id = %s
                """,
                (observed_after, error_code, elapsed_ms, action_id),
            )
            cursor.execute(
                """
                UPDATE a6_trade.trade_control_action_attempts
                SET observed_after = %s::jsonb, status = 'failed',
                    error_class = %s, error_fingerprint = %s,
                    completed_at = now(), elapsed_ms = %s
                WHERE action_id = %s AND attempt_no = %s
                """,
                (
                    observed_after,
                    error_code,
                    fingerprint,
                    elapsed_ms,
                    action_id,
                    attempt_no,
                ),
            )

    _transaction(connection, write)


def error_class(exc: Exception) -> str:
    value = str(exc).strip()
    if value and value.replace("_", "").isalnum():
        return value[:200]
    return "emergency_reprice_remote_failed"


def process_target(
    connection: Any,
    target: Target,
    client: A6ApiClient,
    before: Mapping[str, Any],
    *,
    cost_multiplier: Decimal,
    margin: Decimal,
    conversion_factor: Decimal,
    headroom: Decimal,
    config_version: str,
    readback_attempts: int,
    readback_interval_seconds: float,
) -> dict[str, Any]:
    validate_down_listing(before, target)
    before_prices = listing_prices(before)
    plan = build_price_plan(
        before_prices,
        cost_multiplier=cost_multiplier,
        delta=target.effective_delta,
        margin=margin,
        conversion_factor=conversion_factor,
        headroom=headroom,
    )
    action_id, attempt_no = prepare_action(
        connection,
        target,
        before,
        plan,
        config_version=config_version,
    )
    started = time.monotonic()
    if before_prices == plan.prices:
        complete_action(
            connection,
            target,
            action_id,
            attempt_no,
            before,
            0,
            operation="noop",
        )
        return {
            "merchant_id": target.merchant_id,
            "listing_id": target.listing_id,
            "action": "noop",
            "confirmed": True,
            "delta": decimal_text(plan.delta),
            "prices_micros": list(plan.prices),
        }

    mutation_error: Exception | None = None
    mutation_response: Mapping[str, Any] | None = None
    activation_recovery: dict[str, Any] | None = None
    last_observed: Mapping[str, Any] = before
    try:
        response = client.update_supplier_listing(
            target.a6_user_id,
            target.listing_id,
            update_payload(before, plan),
        )
        if isinstance(response, Mapping):
            mutation_response = response
    except Exception as exc:
        mutation_error = exc

    last_error: Exception | None = mutation_error
    if mutation_response is not None and activation_signal(mutation_response):
        recovered, activation_recovery, recovery_error = recover_unexpected_activation(
            client,
            target,
            mutation_response,
            source="put_response",
            attempts=readback_attempts,
            interval_seconds=readback_interval_seconds,
        )
        if recovered is not None:
            last_observed = recovered
            if listing_prices(recovered) == plan.prices:
                elapsed_ms = max(0, round((time.monotonic() - started) * 1000))
                complete_action(
                    connection,
                    target,
                    action_id,
                    attempt_no,
                    recovered,
                    elapsed_ms,
                    operation="update_then_takedown",
                    activation_recovery=activation_recovery,
                )
                return {
                    "merchant_id": target.merchant_id,
                    "listing_id": target.listing_id,
                    "action": "reprice_and_takedown",
                    "confirmed": True,
                    "activation_recovered": True,
                    "delta": decimal_text(plan.delta),
                    "prices_micros": list(plan.prices),
                }
            last_error = RuntimeError("emergency_reprice_readback_mismatch")
        else:
            final_readback = activation_recovery.get("final_readback")
            if isinstance(final_readback, Mapping):
                last_observed = final_readback
            last_error = recovery_error

    if activation_recovery is not None:
        assert last_error is not None
        elapsed_ms = max(0, round((time.monotonic() - started) * 1000))
        code = error_class(last_error)
        fail_action(
            connection,
            action_id,
            attempt_no,
            code,
            elapsed_ms,
            target=target,
            after=last_observed,
            activation_recovery=activation_recovery,
        )
        return {
            "merchant_id": target.merchant_id,
            "listing_id": target.listing_id,
            "action": "error",
            "confirmed": False,
            "activation_recovered": bool(activation_recovery.get("final_down_confirmed")),
            "error_class": code,
        }

    for index in range(readback_attempts):
        try:
            inventory = listing_inventory(client, target.a6_user_id)
            after = inventory.get(target.listing_id)
            if after is None:
                raise RuntimeError("emergency_reprice_listing_missing")
            validate_listing_identity(after, target)
            last_observed = after
            if activation_signal(after) or not listing_is_down(after):
                recovered, activation_recovery, recovery_error = recover_unexpected_activation(
                    client,
                    target,
                    after,
                    source="get_readback",
                    attempts=readback_attempts,
                    interval_seconds=readback_interval_seconds,
                )
                if recovered is None:
                    final_readback = activation_recovery.get("final_readback")
                    if isinstance(final_readback, Mapping):
                        last_observed = final_readback
                    raise recovery_error or RuntimeError(
                        "emergency_reprice_activation_recovery_failed"
                    )
                after = recovered
                last_observed = recovered
            if listing_prices(after) != plan.prices:
                raise RuntimeError("emergency_reprice_readback_mismatch")
            elapsed_ms = max(0, round((time.monotonic() - started) * 1000))
            complete_action(
                connection,
                target,
                action_id,
                attempt_no,
                after,
                elapsed_ms,
                operation=("update_then_takedown" if activation_recovery is not None else "update"),
                activation_recovery=activation_recovery,
            )
            return {
                "merchant_id": target.merchant_id,
                "listing_id": target.listing_id,
                "action": (
                    "reprice_and_takedown" if activation_recovery is not None else "reprice"
                ),
                "confirmed": True,
                "activation_recovered": activation_recovery is not None,
                "delta": decimal_text(plan.delta),
                "prices_micros": list(plan.prices),
            }
        except Exception as exc:
            last_error = exc
            if activation_recovery is not None:
                break
            if index + 1 < readback_attempts and readback_interval_seconds:
                time.sleep(readback_interval_seconds)

    assert last_error is not None
    elapsed_ms = max(0, round((time.monotonic() - started) * 1000))
    code = error_class(last_error)
    fail_action(
        connection,
        action_id,
        attempt_no,
        code,
        elapsed_ms,
        target=target if activation_recovery is not None else None,
        after=last_observed if activation_recovery is not None else None,
        activation_recovery=activation_recovery,
    )
    return {
        "merchant_id": target.merchant_id,
        "listing_id": target.listing_id,
        "action": "error",
        "confirmed": False,
        "activation_recovered": bool(
            activation_recovery and activation_recovery.get("final_down_confirmed")
        ),
        "error_class": code,
    }


def run_targets(
    connection: Any,
    selected: Sequence[Target],
    proxy_by_user_id: Mapping[int, str],
    *,
    timeout: float,
    cost_multiplier: Decimal,
    margin: Decimal,
    conversion_factor: Decimal,
    headroom: Decimal,
    config_version: str,
    readback_attempts: int,
    readback_interval_seconds: float,
) -> list[dict[str, Any]]:
    clients: dict[int, A6ApiClient] = {}
    results: list[dict[str, Any]] = []
    for target in selected:
        try:
            client = clients.get(target.a6_user_id)
            if client is None:
                client = client_for(target, proxy_by_user_id, timeout=timeout)
                clients[target.a6_user_id] = client
            inventory = listing_inventory(client, target.a6_user_id)
            before = inventory.get(target.listing_id)
            if before is None:
                raise RuntimeError("emergency_reprice_listing_missing")
            results.append(
                process_target(
                    connection,
                    target,
                    client,
                    before,
                    cost_multiplier=cost_multiplier,
                    margin=margin,
                    conversion_factor=conversion_factor,
                    headroom=headroom,
                    config_version=config_version,
                    readback_attempts=readback_attempts,
                    readback_interval_seconds=readback_interval_seconds,
                )
            )
        except Exception as exc:
            connection.rollback()
            results.append(
                {
                    "merchant_id": target.merchant_id,
                    "listing_id": target.listing_id,
                    "action": "error",
                    "confirmed": False,
                    "error_class": error_class(exc),
                }
            )
    return results


def _validated_arguments(
    args: argparse.Namespace,
) -> tuple[Decimal, Decimal, Decimal, Decimal]:
    if (
        args.model.strip() != SUPPORTED_MODEL
        or args.timeout_seconds <= 0
        or args.readback_attempts < 1
        or args.readback_interval_seconds < 0
        or not str(args.config_version).strip()
    ):
        raise RuntimeError("emergency_reprice_arguments_invalid")
    return (
        decimal_value(
            args.cost_multiplier,
            "emergency_reprice_cost_multiplier_invalid",
            minimum=Decimal("0.000000000001"),
        ),
        decimal_value(
            args.margin,
            "emergency_reprice_margin_invalid",
            minimum=MINIMUM_MARGIN,
            maximum=Decimal("10"),
        ),
        decimal_value(
            args.conversion_factor,
            "emergency_reprice_conversion_factor_invalid",
            minimum=Decimal("0.000000000001"),
            maximum=Decimal("1"),
        ),
        decimal_value(
            args.headroom,
            "emergency_reprice_headroom_invalid",
            minimum=Decimal("0"),
            maximum=Decimal("10"),
        ),
    )


def main() -> None:
    args = parser().parse_args()
    cost, margin, conversion, headroom = _validated_arguments(args)
    dsn = os.environ.get("A6_TRADE_DATABASE_DSN", "").strip()
    if not dsn:
        raise RuntimeError("emergency_reprice_database_dsn_missing")
    connection = psycopg.connect(dsn, row_factory=dict_row)
    try:
        with seller_lock():
            selected = targets(connection, args.model.strip())
            planned = [
                {
                    "merchant_id": target.merchant_id,
                    "listing_id": target.listing_id,
                    "delta": decimal_text(target.effective_delta),
                    "delta_source": (
                        "remote_observed"
                        if target.observed_delta is not None
                        else "conservative_zero"
                    ),
                    "before_prices_micros": list(target.local_prices),
                    "target": build_price_plan(
                        target.local_prices,
                        cost_multiplier=cost,
                        delta=target.effective_delta,
                        margin=margin,
                        conversion_factor=conversion,
                        headroom=headroom,
                    ),
                }
                for target in selected
            ]
            for item, target in zip(planned, selected, strict=True):
                plan = item.pop("target")
                item["desired_state"] = desired_state(target, plan)
            preview = {
                "model": args.model.strip(),
                "cost_multiplier": decimal_text(cost),
                "margin": decimal_text(margin),
                "conversion_factor": decimal_text(conversion),
                "headroom": decimal_text(headroom),
                "target_listing_count": len(selected),
                "target_user_count": len({item.a6_user_id for item in selected}),
            }
            if not args.apply:
                print(
                    json.dumps(
                        {"success": True, "mode": "dry-run", **preview, "targets": planned},
                        ensure_ascii=True,
                        separators=(",", ":"),
                    )
                )
                return
            proxy_by_user_id = {
                int(key): str(value).strip()
                for key, value in read_private_json(args.proxy_map_file).items()
            }
            results = run_targets(
                connection,
                selected,
                proxy_by_user_id,
                timeout=args.timeout_seconds,
                cost_multiplier=cost,
                margin=margin,
                conversion_factor=conversion,
                headroom=headroom,
                config_version=args.config_version,
                readback_attempts=args.readback_attempts,
                readback_interval_seconds=args.readback_interval_seconds,
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
                        "targets": planned,
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
