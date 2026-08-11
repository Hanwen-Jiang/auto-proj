#!/usr/bin/env python3
"""Reconcile managed Seller listings against sub2api-account and provider schedulability.

Policy (per model):
- While no sub2api economic account is callable AND no provider is schedulable,
  every managed listing is taken down.
- When a callable sub2api account exists, a listing may be relisted only when
  its channel net K_CNY clears the account's profit gate
  (minimum_channel_k_cny = source_k_cny * (1 + bps) * 1.2 A6 gross factor).
- When the merchant's own provider is schedulable, its listing relists.

Channels without fresh revenue evidence stay down (no traffic -> no revenue ->
below the gate). Every mutation is an audited, idempotent control action with
remote readback, mirroring the emergency model pause machinery.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import time
from collections import defaultdict
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import ROUND_CEILING, Decimal
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row

from a6api_auto.client import A6ApiClient
from a6api_auto.storage import SessionStore, read_private_json
from a6api_auto.trade_economic_source import ECONOMIC_COST_QUANTUM
from a6api_auto.trade_profit_settings import (
    A6_GROSS_LISTING_FACTOR,
    DEFAULT_PROFIT_ON_COST_BPS,
    PostgresModelProfitSettingStore,
)

CONFIG_VERSION = "listing-schedulability-v1"
LOCK_FILE = Path("/var/lib/a6-trader/seller-control.lock")
_AWARE_NOW = datetime.now(timezone.utc)
_REVENUE_MAX_AGE_SECONDS = 600
_THROTTLED = frozenset({"AVAILABLE", "THROTTLED"})
# Account evidence freshness window. The economic-account sync runs every ~30s;
# the 3x margin absorbs a missed sync round without declaring every account
# uncallable (which would take down all live listings).
_ACCOUNT_EVIDENCE_MAX_AGE_SECONDS = 90
# Provisional weekly quota used to simulate the account cost basis when no
# usable quota snapshot exists (account at 0% usage / no other observable).
_SIMULATED_WEEKLY_QUOTA_A = Decimal("100")


class SchedulabilityError(RuntimeError):
    pass


@dataclass(frozen=True)
class Target:
    merchant_id: int
    a6_user_id: int
    session_ref: Path
    listing_id: int
    channel_id: int
    model: str
    output_price_micros: int


@dataclass(frozen=True)
class AccountEvidence:
    source_id: int
    enodot_account_id: int
    capacity_state: str
    health_state: str
    cost_mode: str | None
    cycle_cost_cny: Decimal | None
    declared_k_cny: Decimal | None
    used_a: Decimal | None
    remaining_a: Decimal | None

    def source_k_cny(self) -> Decimal | None:
        if self.cost_mode == "declared_k" and self.declared_k_cny is not None:
            return self.declared_k_cny
        if (
            self.cost_mode == "weekly_amortized"
            and self.cycle_cost_cny is not None
            and self.used_a is not None
            and self.remaining_a is not None
        ):
            total = self.used_a + self.remaining_a
            if total <= 0:
                return None
            return (self.cycle_cost_cny / total).quantize(
                ECONOMIC_COST_QUANTUM, rounding=ROUND_CEILING
            )
        if (
            self.cost_mode == "weekly_amortized"
            and self.cycle_cost_cny is not None
        ):
            # No usable quota snapshot (e.g. account at 0% usage with no other
            # observable): simulate with the provisional weekly quota of 100 A
            # so the account still gets a cost basis for the gate.
            return (self.cycle_cost_cny / _SIMULATED_WEEKLY_QUOTA_A).quantize(
                ECONOMIC_COST_QUANTUM, rounding=ROUND_CEILING
            )
        return None

    def callable(self, *, minimum_remaining_a: Decimal = Decimal("20")) -> bool:
        if self.health_state != "HEALTHY" or self.capacity_state not in _THROTTLED:
            return False
        if self.remaining_a is None or self.remaining_a < minimum_remaining_a:
            return False
        return self.source_k_cny() is not None


@dataclass(frozen=True)
class Decision:
    merchant_id: int
    listing_id: int
    action: str
    reason: str
    gate_k_cny: Decimal | None = None
    channel_net_k_cny: Decimal | None = None
    serving_account_ids: tuple[int, ...] = ()


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(
        description="Takedown or relist managed listings by account/provider schedulability"
    )
    command.add_argument("--model", default="gpt-5.6-sol")
    command.add_argument("--apply", action="store_true")
    command.add_argument("--timeout-seconds", type=float, default=20)
    command.add_argument("--attempts", type=int, default=3)
    command.add_argument("--revenue-max-age-seconds", type=int, default=_REVENUE_MAX_AGE_SECONDS)
    command.add_argument(
        "--minimum-remaining-a",
        type=float,
        default=20.0,
        help="sub2api account is callable only above this remaining weekly quota",
    )
    command.add_argument(
        "--provider-profit-percent",
        type=float,
        default=20.0,
        help="fallback Provider net-profit percent when the active cost "
        "generation has no margin_bps",
    )
    command.add_argument(
        "--fx-cny-per-usd",
        type=float,
        default=7.2,
        help="fallback CNY per USD when no fresh market pair rate exists",
    )
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


def load_accounts(connection: Any, model: str, *, now: datetime) -> tuple[AccountEvidence, ...]:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT s.source_id, s.enodot_account_id, s.capacity_state, s.health_state,
                   c.cost_mode, c.cycle_cost_cny, c.declared_k_cny,
                   q.used_a, q.remaining_a
            FROM a6_trade.sub2api_economic_sources s
            LEFT JOIN a6_trade.sub2api_source_cost_contracts c
              ON c.source_id = s.source_id AND c.contract_state = 'active'
            LEFT JOIN LATERAL (
                SELECT used_a, remaining_a
                FROM a6_trade.sub2api_source_quota_snapshots
                WHERE source_id = s.source_id AND snapshot_state = 'VALID'
                ORDER BY observed_at DESC, snapshot_id DESC
                LIMIT 1
            ) q ON true
            WHERE s.model = %s AND s.enabled
              AND s.health_observed_at IS NOT NULL
              AND s.health_observed_at >= %s
              AND s.capacity_observed_at IS NOT NULL
              AND s.capacity_observed_at >= %s
            ORDER BY s.source_id
            """,
            (
                model,
                now - timedelta(seconds=_ACCOUNT_EVIDENCE_MAX_AGE_SECONDS),
                now - timedelta(seconds=_ACCOUNT_EVIDENCE_MAX_AGE_SECONDS),
            ),
        )
        rows = cursor.fetchall()
    accounts: list[AccountEvidence] = []
    for row in rows:
        accounts.append(
            AccountEvidence(
                source_id=int(row["source_id"]),
                enodot_account_id=int(row["enodot_account_id"]),
                capacity_state=str(row["capacity_state"]),
                health_state=str(row["health_state"]),
                cost_mode=row["cost_mode"],
                cycle_cost_cny=(
                    Decimal(str(row["cycle_cost_cny"]))
                     if row["cycle_cost_cny"] is not None else None
                ),
                declared_k_cny=(
                    Decimal(str(row["declared_k_cny"]))
                     if row["declared_k_cny"] is not None else None
                ),
                used_a=Decimal(str(row["used_a"])) if row["used_a"] is not None else None,
                remaining_a=(
                    Decimal(str(row["remaining_a"])) if row["remaining_a"] is not None else None
                ),
            )
        )
    return tuple(accounts)


def load_schedulable_provider_merchants(connection: Any, model: str) -> frozenset[int]:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT DISTINCT merchant_id
            FROM a6_trade.provider_inventory
            WHERE model = %s AND schedulable = true
              AND inventory_state NOT IN ('RETIRED', 'QUARANTINED')
            """,
            (model,),
        )
        return frozenset(int(row["merchant_id"]) for row in cursor.fetchall())


def load_provider_cost(
    connection: Any,
    model: str,
    *,
    market_fx: Decimal | None,
    fallback_fx: Decimal,
) -> tuple[Decimal | None, int | None]:
    """Cheapest callable Provider upstream cost in CNY per 1K tokens.

    Uses the active provider cost generation's realized P95 cost (USD) converted
    with the market pair rate, together with that generation's configured
    margin_bps (the Provider profit gate is config-driven, not hardcoded).
    Returns (None, None) when no active generation or no realized cost exists
    (the Provider path is then dormant).
    """
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT cost.realized_cost_p95_multiplier, cost.margin_bps
            FROM a6_trade.provider_cost_generations cost
            JOIN a6_trade.provider_pool_generations generation
              ON generation.cost_generation_id = cost.id
             AND generation.model = cost.model
            WHERE cost.model = %s
              AND cost.pricing_state = 'active'
              AND generation.generation_state = 'active'
            ORDER BY cost.activated_at DESC NULLS LAST, cost.id DESC
            LIMIT 1
            """,
            (model,),
        )
        row = cursor.fetchall()
    if not row:
        return None, None
    raw = row[0]
    cost_value = raw["realized_cost_p95_multiplier"] if isinstance(raw, Mapping) else raw[0]
    margin_value = raw["margin_bps"] if isinstance(raw, Mapping) else raw[1]
    if cost_value is None:
        return None, None
    cost = Decimal(str(cost_value))
    fx = market_fx if market_fx is not None else fallback_fx
    return (
        (cost * fx).quantize(ECONOMIC_COST_QUANTUM, rounding=ROUND_CEILING),
        None if margin_value is None else int(margin_value),
    )


def load_market_fx(connection: Any, model: str, *, now: datetime) -> Decimal | None:
    """Per-window market pair rate (CNY per USD) from the latest market quotes."""
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT avg(realtime_ratio_cny / nullif(realtime_ratio_usd, 0)) AS fx
            FROM a6_trade.a6_market_quotes
            WHERE model = %s
              AND observed_at >= %s
              AND realtime_ratio_usd > 0
              AND realtime_ratio_cny > 0
            """,
            (model, now - timedelta(seconds=_REVENUE_MAX_AGE_SECONDS)),
        )
        rows = cursor.fetchall()
    if not rows:
        return None
    raw = rows[0]
    value = raw["fx"] if isinstance(raw, Mapping) else raw[0]
    if value is None:
        return None
    return Decimal(str(value))


def load_channel_market_quotes(
    connection: Any,
    model: str,
    *,
    now: datetime,
    max_age_seconds: int = _REVENUE_MAX_AGE_SECONDS,
) -> dict[int, Decimal]:
    """Latest market quote (realtime_ratio_cny) per channel.

    The marketplace trades channels at the observed quote, NOT the seller-side
    listing price (the seller ask can differ from the effective price by
    several times). All profit gates must compare against this quote.
    """
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT DISTINCT ON (channel_id) channel_id, realtime_ratio_cny
            FROM a6_trade.a6_market_quotes
            WHERE model = %s
              AND observed_at >= %s
              AND realtime_ratio_cny IS NOT NULL
            ORDER BY channel_id, observed_at DESC
            """,
            (model, now - timedelta(seconds=max_age_seconds)),
        )
        rows = cursor.fetchall()
    result: dict[int, Decimal] = {}
    for row in rows:
        if isinstance(row, Mapping):
            channel_id = int(row["channel_id"])
            value = row["realtime_ratio_cny"]
        else:
            channel_id = int(row[0])
            value = row[1]
        if value is not None:
            result[channel_id] = Decimal(str(value))
    return result


def load_channel_revenue(
    connection: Any,
    model: str,
    *,
    max_age_seconds: int,
    now: datetime,
) -> dict[int, Decimal]:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT DISTINCT ON (channel_id) channel_id, channel_net_k_cny
            FROM a6_trade.sub2api_channel_revenue_snapshots
            WHERE model = %s AND snapshot_state = 'VALID'
              AND observed_at >= %s
            ORDER BY channel_id, observed_at DESC, snapshot_id DESC
            """,
            (model, now - timedelta(seconds=max_age_seconds)),
        )
        return {
            int(row["channel_id"]): Decimal(str(row["channel_net_k_cny"]))
            for row in cursor.fetchall()
        }


def load_targets(
    connection: Any,
    model: str,
    *,
    revenue_by_channel: Mapping[int, Decimal],
    schedulable_provider_merchants: frozenset[int],
    accounts: Sequence[AccountEvidence],
    now: datetime,
) -> tuple[tuple[Target, ...], dict[tuple[int, int], bool]]:
    del revenue_by_channel, schedulable_provider_merchants, accounts, now
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT binding.merchant_id, merchant.a6_user_id,
                   merchant.session_ref, listing.a6_listing_id,
                   listing.a6_channel_id, listing.platform_model,
                   listing.status, listing.output_price_micros
            FROM a6_trade.a6_managed_listing_bindings binding
            JOIN a6_trade.a6_supplier_listings_current listing
              ON listing.merchant_id = binding.merchant_id
             AND listing.a6_listing_id = binding.a6_listing_id
             AND listing.a6_channel_id = binding.a6_channel_id
            JOIN a6_trade.a6_merchants merchant
              ON merchant.id = binding.merchant_id
            WHERE listing.platform_model = %s
              AND binding.control_state <> 'retired'
              AND merchant.a6_user_id IS NOT NULL
              AND merchant.status = 'supplier_submitted'
              AND merchant.roles @> ARRAY['seller']::text[]
            ORDER BY merchant.a6_user_id, listing.a6_listing_id
            """,
            (model,),
        )
        rows = cursor.fetchall()
    targets: list[Target] = []
    live_by_listing: dict[tuple[int, int], bool] = {}
    for row in rows:
        merchant_id = int(row["merchant_id"])
        listing_id = int(row["a6_listing_id"])
        target = Target(
            merchant_id=merchant_id,
            a6_user_id=int(row["a6_user_id"]),
            session_ref=Path(str(row["session_ref"])),
            listing_id=listing_id,
            channel_id=int(row["a6_channel_id"]),
            model=str(row["platform_model"]),
            output_price_micros=int(row["output_price_micros"] or 0),
        )
        targets.append(target)
        live_by_listing[(merchant_id, listing_id)] = int(row["status"] or 0) == 2
    return tuple(targets), live_by_listing


def decide(
    target: Target,
    *,
    is_live: bool,
    channel_net_k_cny: Decimal | None,
    channel_quote_cny: Decimal | None,
    schedulable_provider_merchants: frozenset[int],
    accounts: Sequence[AccountEvidence],
    gross_gate_multiplier: Decimal,
    provider_cost_cny_per_1k: Decimal | None,
    provider_gate_multiplier: Decimal,
    minimum_remaining_a: Decimal = Decimal("20"),
    banned_channel_ids: frozenset[int] = frozenset(),
) -> Decision:
    # 已被 auto-pool ban（user_channel_disabled）的 owned 渠道由 auto-pool 接管：
    # 必须保持上架+banned 状态以维持市场审计可见性，上下架均不得干预。
    if target.channel_id in banned_channel_ids:
        return Decision(
            target.merchant_id,
            target.listing_id,
            "keep",
            "auto_pool_banned",
        )
    if target.merchant_id in schedulable_provider_merchants:
        return Decision(
            target.merchant_id,
            target.listing_id,
            "relist" if not is_live else "keep",
            "provider_schedulable",
        )
    callable_accounts = tuple(
        account
        for account in accounts
        if account.callable(minimum_remaining_a=minimum_remaining_a)
    )
    provider_callable = bool(schedulable_provider_merchants)
    if not callable_accounts and not provider_callable:
        return Decision(
            target.merchant_id,
            target.listing_id,
            "takedown" if is_live else "keep",
            "no_callable_source",
        )
    # The marketplace trades channels at the observed quote, not the seller-side
    # listing price. Every profit gate therefore compares the channel quote
    # (realtime_ratio_cny) against the account floor; without a fresh quote the
    # channel stays down (no observable price -> cannot prove profitability).
    if channel_quote_cny is None:
        return Decision(
            target.merchant_id,
            target.listing_id,
            "takedown" if is_live else "keep",
            "no_market_quote",
        )
    # Provider path: the cheapest callable Provider account's gate is the
    # provider upstream cost x (1 + profit%) x 1/c; 20% net profit is the
    # default (1.44x) and is configurable.
    if (
        provider_callable
        and provider_cost_cny_per_1k is not None
        and channel_quote_cny
        >= provider_cost_cny_per_1k * provider_gate_multiplier
    ):
        return Decision(
            target.merchant_id,
            target.listing_id,
            "relist" if not is_live else "keep",
            "provider_profit_gate_passed",
            gate_k_cny=provider_cost_cny_per_1k * provider_gate_multiplier,
            channel_net_k_cny=channel_net_k_cny,
        )
    # Sub2api path: the cheapest callable account's LISTING gate decides
    # relisting. A channel relists when its observed quote clears that account's
    # cost+profit floor (quote >= k * gross_multiplier, e.g. 45933:
    # k x 1.92).
    if callable_accounts:
        cheapest = min(
            callable_accounts,
            key=lambda item: (
                item.source_k_cny() if item.source_k_cny() is not None else Decimal("Infinity"),
                item.source_id,
            ),
        )
        k_cny = cheapest.source_k_cny()
        assert k_cny is not None
        price_gate_cny = k_cny * gross_gate_multiplier
        if channel_quote_cny >= price_gate_cny:
            return Decision(
                target.merchant_id,
                target.listing_id,
                "relist" if not is_live else "keep",
                "sub2api_price_gate_passed",
                gate_k_cny=price_gate_cny,
                channel_net_k_cny=channel_net_k_cny,
            )
        return Decision(
            target.merchant_id,
            target.listing_id,
            "takedown" if is_live else "keep",
            "sub2api_price_gate_blocked",
            gate_k_cny=price_gate_cny,
            channel_net_k_cny=channel_net_k_cny,
        )
    return Decision(
        target.merchant_id,
        target.listing_id,
        "takedown" if is_live else "keep",
        "provider_profit_gate_blocked",
        channel_net_k_cny=channel_net_k_cny,
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
        raise SchedulabilityError("schedulability_session_invalid")
    client = A6ApiClient(record.origin, proxy_url=proxy_url, timeout=timeout)
    client.import_cookies(record.cookies)
    return client


def listing_inventory(client: A6ApiClient, user_id: int) -> dict[int, dict[str, Any]]:
    result: dict[int, dict[str, Any]] = {}
    for page in range(1, 101):
        document = client.supplier_listings(user_id, page=page, size=100)
        items = document.get("items")
        if not isinstance(items, list):
            raise SchedulabilityError("schedulability_listing_inventory_invalid")
        for item in items:
            if not isinstance(item, Mapping) or int(item.get("id") or 0) < 1:
                raise SchedulabilityError("schedulability_listing_inventory_invalid")
            result[int(item["id"])] = dict(item)
        total = int(document.get("total") or 0)
        if not items or len(items) < 100 or (total and len(result) >= total):
            return result
    raise SchedulabilityError("schedulability_listing_inventory_incomplete")


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
        raise SchedulabilityError("schedulability_listing_identity_mismatch")
    if int(item.get("status") or 0) not in {0, 2}:
        raise SchedulabilityError("schedulability_listing_state_unknown")


def is_down(item: Mapping[str, Any]) -> bool:
    return int(item.get("status") or 0) == 0


def is_live(item: Mapping[str, Any]) -> bool:
    return int(item.get("status") or 0) == 2


def prepare_action(
    connection: Any,
    target: Target,
    decision: Decision,
    before: Mapping[str, Any],
    *,
    config_version: str,
) -> tuple[int, int]:
    key = f"schedulability:{target.model}:{target.merchant_id}:{target.listing_id}"
    desired = {
        "model": target.model,
        "merchant_id": target.merchant_id,
        "listing_id": target.listing_id,
        "channel_id": target.channel_id,
        "action": decision.action,
        "reason": decision.reason,
        "serving_account_ids": list(decision.serving_account_ids),
        "gate_k_cny": None if decision.gate_k_cny is None else format(decision.gate_k_cny, "f"),
        "channel_net_k_cny": (
            None if decision.channel_net_k_cny is None else format(decision.channel_net_k_cny, "f")
        ),
        "status": 2 if decision.action == "relist" else 0,
    }
    with connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO a6_trade.trade_control_actions (
                idempotency_key, action_type, entity_type, entity_id,
                desired_state, observed_before, status, config_version,
                requested_by, started_at
            ) VALUES (
                %s, 'schedulability_listing_action', 'a6_supplier_listing', %s,
                %s::jsonb, %s::jsonb, 'started', %s,
                'reconcile-a6-listing-schedulability', now()
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
            SET status = %s, availability = %s, visibility = %s,
                observed_at = now(), updated_at = now()
            WHERE merchant_id = %s AND a6_listing_id = %s
              AND a6_channel_id = %s
            """,
            (
                int(after.get("status") or 0),
                int(after.get("availability") or 0),
                int(after.get("visibility") or 0),
                target.merchant_id,
                target.listing_id,
                target.channel_id,
            ),
        )
        if cursor.rowcount != 1:
            raise SchedulabilityError("schedulability_local_readback_missing")
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
    return "schedulability_remote_failed"


def load_banned_channel_ids(connection: Any, model: str) -> frozenset[int]:
    """auto-pool 已 ban 的 owned 渠道（user_channel_disabled，需保持上架+禁用）。"""
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT DISTINCT disabled_channel
            FROM a6_trade.provider_auto_pool_accounts account
            JOIN a6_trade.provider_auto_pool_generations generation
              ON generation.id = account.generation_id
            CROSS JOIN LATERAL unnest(account.disabled_owned_channel_ids)
              AS disabled_channel
            WHERE generation.model = %s
              AND generation.generation_state IN ('applying', 'active')
              AND account.account_state IN ('applying', 'ready')
            """,
            (model,),
        )
        return frozenset(int(row["disabled_channel"]) for row in cursor.fetchall())


def mutate(
    connection: Any,
    target: Target,
    client: A6ApiClient,
    decision: Decision,
    before: Mapping[str, Any],
    *,
    config_version: str,
    attempts: int,
) -> dict[str, Any]:
    validate_listing(before, target)
    if decision.action == "keep" or (
        decision.action == "takedown" and is_down(before)
    ) or (decision.action == "relist" and is_live(before)):
        return {
            "merchant_id": target.merchant_id,
            "listing_id": target.listing_id,
            "action": decision.action,
            "reason": decision.reason,
            "serving_account_ids": list(decision.serving_account_ids),
            "mutated": False,
            "confirmed": True,
        }
    action_id, attempt_no = prepare_action(
        connection,
        target,
        decision,
        before,
        config_version=config_version,
    )
    started = time.monotonic()
    last_error: Exception | None = None
    for index in range(attempts):
        try:
            if index == 0:
                if decision.action == "takedown":
                    client.takedown_supplier_listing(target.a6_user_id, target.listing_id)
                else:
                    client.resubmit_supplier_listing(target.a6_user_id, target.listing_id)
            inventory = listing_inventory(client, target.a6_user_id)
            after = inventory.get(target.listing_id)
            if after is None:
                raise SchedulabilityError("schedulability_listing_missing")
            validate_listing(after, target)
            reached = (
                is_down(after) if decision.action == "takedown" else is_live(after)
            )
            if not reached:
                if index + 1 < attempts:
                    if decision.action == "takedown":
                        client.takedown_supplier_listing(target.a6_user_id, target.listing_id)
                    else:
                        client.resubmit_supplier_listing(target.a6_user_id, target.listing_id)
                    continue
                raise SchedulabilityError("schedulability_readback_mismatch")
            elapsed_ms = max(0, round((time.monotonic() - started) * 1000))
            complete_action(connection, target, action_id, attempt_no, after, elapsed_ms)
            return {
                "merchant_id": target.merchant_id,
                "listing_id": target.listing_id,
                "action": decision.action,
                "reason": decision.reason,
                "serving_account_ids": list(decision.serving_account_ids),
                "mutated": True,
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
        "action": decision.action,
        "reason": decision.reason,
        "mutated": False,
        "confirmed": False,
        "error_class": code,
    }


def main() -> None:
    args = parser().parse_args()
    if not args.model.strip() or args.timeout_seconds <= 0 or args.attempts < 1:
        raise SchedulabilityError("schedulability_arguments_invalid")
    dsn = os.environ.get("A6_TRADE_DATABASE_DSN", "").strip()
    if not dsn:
        raise SchedulabilityError("schedulability_database_dsn_missing")
    now = _AWARE_NOW
    proxy_by_user_id = {
        int(key): str(value).strip()
        for key, value in read_private_json(args.proxy_map_file).items()
    }
    connection = psycopg.connect(dsn, row_factory=dict_row)
    try:
        with seller_lock():
            model = args.model.strip()
            now = _AWARE_NOW
            market_fx = load_market_fx(connection, model, now=now)
            runtime_setting = PostgresModelProfitSettingStore(
                lambda: psycopg.connect(dsn, row_factory=dict_row)
            ).get(model)
            if runtime_setting is not None:
                net_gate_multiplier = runtime_setting.net_multiplier
                gross_gate_multiplier = runtime_setting.gross_listing_multiplier
            else:
                default_net = Decimal(1) + Decimal(DEFAULT_PROFIT_ON_COST_BPS) / Decimal(10000)
                net_gate_multiplier = default_net
                gross_gate_multiplier = default_net * A6_GROSS_LISTING_FACTOR
            provider_gate_multiplier = (
                Decimal(1) + Decimal(args.provider_profit_percent) / Decimal(100)
            ) * A6_GROSS_LISTING_FACTOR
            accounts = load_accounts(connection, model, now=now)
            schedulable_providers = load_schedulable_provider_merchants(connection, model)
            provider_cost, provider_margin_bps = load_provider_cost(
                connection,
                model,
                market_fx=market_fx,
                fallback_fx=Decimal(str(args.fx_cny_per_usd)),
            )
            if provider_margin_bps is not None:
                provider_gate_multiplier = (
                    Decimal(1) + Decimal(provider_margin_bps) / Decimal(10000)
                ) * A6_GROSS_LISTING_FACTOR
            else:
                provider_gate_multiplier = (
                    Decimal(1) + Decimal(args.provider_profit_percent) / Decimal(100)
                ) * A6_GROSS_LISTING_FACTOR
            revenue_by_channel = load_channel_revenue(
                connection,
                model,
                max_age_seconds=args.revenue_max_age_seconds,
                now=now,
            )
            quote_by_channel = load_channel_market_quotes(
                connection,
                model,
                now=now,
                max_age_seconds=args.revenue_max_age_seconds,
            )
            targets, live_by_listing = load_targets(
                connection,
                model,
                revenue_by_channel=revenue_by_channel,
                schedulable_provider_merchants=schedulable_providers,
                accounts=accounts,
                now=now,
            )
            banned_channel_ids = load_banned_channel_ids(connection, model)
            decisions = [
                decide(
                    target,
                    is_live=live_by_listing.get((target.merchant_id, target.listing_id), False),
                    channel_net_k_cny=revenue_by_channel.get(target.channel_id),
                    channel_quote_cny=quote_by_channel.get(target.channel_id),
                    schedulable_provider_merchants=schedulable_providers,
                    accounts=accounts,
                    gross_gate_multiplier=gross_gate_multiplier,
                    provider_cost_cny_per_1k=provider_cost,
                    provider_gate_multiplier=provider_gate_multiplier,
                    minimum_remaining_a=Decimal(str(args.minimum_remaining_a)),
                    banned_channel_ids=banned_channel_ids,
                )
                for target in targets
            ]
            preview = {
                "model": model,
                "target_listing_count": len(targets),
                "callable_account_count": sum(
                    1 for item in accounts if item.callable(
                        minimum_remaining_a=Decimal(str(args.minimum_remaining_a))
                    )
                ),
                "banned_channel_count": len(banned_channel_ids),
                "cheapest_callable_k_cny": (
                    format(
                        min(
                            (item.source_k_cny(), item.source_id)
                            for item in accounts
                            if item.callable(
                                minimum_remaining_a=Decimal(str(args.minimum_remaining_a))
                            )
                            and item.source_k_cny() is not None
                        )[0],
                        "f",
                    )
                    if any(
                        item.callable(
                            minimum_remaining_a=Decimal(str(args.minimum_remaining_a))
                        )
                        and item.source_k_cny() is not None
                        for item in accounts
                    )
                    else None
                ),
                "sub2api_price_gate_micros": (
                    format(
                        min(
                            (item.source_k_cny(), item.source_id)
                            for item in accounts
                            if item.callable(
                                minimum_remaining_a=Decimal(str(args.minimum_remaining_a))
                            )
                            and item.source_k_cny() is not None
                        )[0]
                        * gross_gate_multiplier
                        * Decimal("1000000"),
                        "f",
                    )
                    if any(
                        item.callable(
                            minimum_remaining_a=Decimal(str(args.minimum_remaining_a))
                        )
                        and item.source_k_cny() is not None
                        for item in accounts
                    )
                    else None
                ),
                "schedulable_provider_merchant_count": len(schedulable_providers),
                "revenue_channel_count": len(revenue_by_channel),
                "provider_cost_cny_per_1k": (
                    format(provider_cost, "f") if provider_cost is not None else None
                ),
                "market_fx_cny_per_usd": (
                    format(market_fx, "f") if market_fx is not None else None
                ),
                "sub2api_gate_multipliers": {
                    "net": format(net_gate_multiplier, "f"),
                    "gross": format(gross_gate_multiplier, "f"),
                    "source": "model_profit_settings"
                    if runtime_setting is not None
                    else "default",
                },
                "provider_gate_multiplier": format(provider_gate_multiplier, "f"),
                "would_takedown": sum(
                    1 for item in decisions if item.action == "takedown"
                ),
                "would_relist": sum(1 for item in decisions if item.action == "relist"),
                "would_keep": sum(1 for item in decisions if item.action == "keep"),
            }
            if not args.apply:
                print(json.dumps({"success": True, "mode": "dry-run", **preview}))
                return
            by_user: dict[int, list[tuple[Target, Decision]]] = defaultdict(list)
            for target, decision in zip(targets, decisions, strict=True):
                if decision.action != "keep":
                    by_user[target.a6_user_id].append((target, decision))
            # Listings already in their desired state ("keep") need no client or
            # remote inventory; only mutated listings cost network round trips, so
            # a fully-quiescent reconcile finishes in milliseconds and does not
            # starve the other seller-lock holders.
            results: list[dict[str, Any]] = [
                {
                    "merchant_id": target.merchant_id,
                    "listing_id": target.listing_id,
                    "action": "keep",
                    "reason": decision.reason,
                    "serving_account_ids": list(decision.serving_account_ids),
                    "mutated": False,
                    "confirmed": True,
                }
                for target, decision in zip(targets, decisions, strict=True)
                if decision.action == "keep"
            ]
            for user_targets in by_user.values():
                representative = user_targets[0][0]
                try:
                    client = client_for(
                        representative,
                        proxy_by_user_id,
                        timeout=args.timeout_seconds,
                    )
                    inventory = listing_inventory(client, representative.a6_user_id)
                    for target, decision in user_targets:
                        before = inventory.get(target.listing_id)
                        if before is None:
                            raise SchedulabilityError("schedulability_listing_missing")
                        result = mutate(
                            connection,
                            target,
                            client,
                            decision,
                            before,
                            config_version=args.config_version,
                            attempts=args.attempts,
                        )
                        results.append(result)
                        if result.get("mutated"):
                            status = 2 if decision.action == "relist" else 0
                            inventory[target.listing_id] = {
                                **before,
                                "status": status,
                                "availability": status,
                                "visibility": status,
                            }
                except Exception as exc:
                    code = error_class(exc)
                    results.extend(
                        {
                            "merchant_id": target.merchant_id,
                            "listing_id": target.listing_id,
                            "action": decision.action,
                            "reason": decision.reason,
                            "mutated": False,
                            "confirmed": False,
                            "error_class": code,
                        }
                        for target, decision in user_targets
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
