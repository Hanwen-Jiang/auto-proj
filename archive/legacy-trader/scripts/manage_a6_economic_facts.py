#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row

from a6api_auto.trade_economic_facts import EconomicFactStore
from a6api_auto.trade_economic_source import EconomicRoutingError


def _timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise argparse.ArgumentTypeError("timestamp must be ISO-8601 with timezone") from None
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("timestamp must include timezone")
    return parsed


def _json_value(value: str, *, expected: type) -> Any:
    raw = value
    if value.startswith("@"):
        raw = Path(value[1:]).read_text(encoding="utf-8")
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        raise EconomicRoutingError("economic_cli_json_invalid") from None
    if not isinstance(parsed, expected):
        raise EconomicRoutingError("economic_cli_json_shape_invalid")
    return parsed


def _document(value: Any) -> dict[str, Any]:
    if hasattr(value, "input_document"):
        return value.input_document()
    return dict(value)


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(
        description="Record structured Priority 1 economic routing facts"
    )
    command.add_argument("--database-dsn-env", default="A6_TRADE_DATABASE_DSN")
    actions = command.add_subparsers(dest="action", required=True)

    source = actions.add_parser("source-upsert")
    source.add_argument("--source-key", required=True)
    source.add_argument("--source-kind", choices=("api", "account", "a6_gateway"), required=True)
    source.add_argument("--origin-class", required=True)
    source.add_argument("--model", required=True)
    source.add_argument("--endpoint-ref", required=True)
    source.add_argument("--endpoint-fingerprint")
    source.add_argument("--enodot-account-id", type=int)
    source.add_argument("--configured-reserve-a", type=Decimal, default=Decimal(0))
    source.add_argument("--enabled", action=argparse.BooleanOptionalAction, default=False)
    source.add_argument("--config-version", default="priority1-economic-v1")
    source.add_argument("--metadata-json", default="{}")

    state = actions.add_parser("source-state")
    state.add_argument("--source-id", type=int, required=True)
    state.add_argument("--health-state", choices=("HEALTHY", "UNHEALTHY", "UNKNOWN"), required=True)
    state.add_argument(
        "--capacity-state",
        choices=("AVAILABLE", "THROTTLED", "EXHAUSTED", "UNKNOWN"),
        required=True,
    )
    state.add_argument("--observed-at", type=_timestamp, required=True)
    state.add_argument("--capacity-epoch")
    state.add_argument("--enabled", action=argparse.BooleanOptionalAction, default=None)

    contract = actions.add_parser("contract")
    contract.add_argument("--source-id", type=int, required=True)
    contract.add_argument("--cost-mode", choices=("declared_k", "weekly_amortized"), required=True)
    contract.add_argument("--declared-k-cny", type=Decimal)
    contract.add_argument("--cycle-cost-cny", type=Decimal)
    contract.add_argument("--cost-basis")
    contract.add_argument("--quota-basis")
    contract.add_argument("--effective-at", type=_timestamp, required=True)
    contract.add_argument("--expires-at", type=_timestamp, required=True)
    contract.add_argument("--config-version", required=True)
    contract.add_argument("--payload-json", default="{}")

    quota = actions.add_parser("quota")
    quota.add_argument("--source-id", type=int, required=True)
    quota.add_argument("--contract-id", type=int, required=True)
    quota.add_argument("--quota-epoch", required=True)
    quota.add_argument("--used-a", type=Decimal, required=True)
    quota.add_argument("--remaining-a", type=Decimal, required=True)
    quota.add_argument("--total-a", type=Decimal, required=True)
    quota.add_argument("--reset-at", type=_timestamp, required=True)
    quota.add_argument("--observed-at", type=_timestamp, required=True)
    quota.add_argument("--payload-json", default="{}")
    quota.add_argument("--allow-total-change", action="store_true")

    revenue = actions.add_parser("revenue")
    revenue.add_argument("--model", required=True)
    revenue.add_argument("--channel-id", type=int, required=True)
    revenue.add_argument("--api-key-id", type=int)
    revenue.add_argument("--group-id", type=int)
    revenue.add_argument("--revenue-epoch", required=True)
    revenue.add_argument("--channel-net-k-cny", type=Decimal, required=True)
    revenue.add_argument("--settlement-contract-ref", required=True)
    revenue.add_argument("--listing-refs-json", default="[]")
    revenue.add_argument("--observed-at", type=_timestamp, required=True)
    revenue.add_argument("--payload-json", default="{}")
    return command


def run(args: argparse.Namespace, store: EconomicFactStore) -> Any:
    if args.action == "source-upsert":
        return store.upsert_source(
            source_key=args.source_key,
            source_kind=args.source_kind,
            origin_class=args.origin_class,
            model=args.model,
            endpoint_ref=args.endpoint_ref,
            endpoint_fingerprint=args.endpoint_fingerprint,
            enodot_account_id=args.enodot_account_id,
            configured_reserve_a=args.configured_reserve_a,
            enabled=args.enabled,
            config_version=args.config_version,
            metadata=_json_value(args.metadata_json, expected=dict),
        )
    if args.action == "source-state":
        return store.update_source_state(
            args.source_id,
            health_state=args.health_state,
            capacity_state=args.capacity_state,
            observed_at=args.observed_at,
            capacity_epoch=args.capacity_epoch,
            enabled=args.enabled,
        )
    if args.action == "contract":
        return store.record_cost_contract(
            source_id=args.source_id,
            cost_mode=args.cost_mode,
            declared_k_cny=args.declared_k_cny,
            cycle_cost_cny=args.cycle_cost_cny,
            cost_basis=args.cost_basis,
            quota_basis=args.quota_basis,
            effective_at=args.effective_at,
            expires_at=args.expires_at,
            config_version=args.config_version,
            contract_payload=_json_value(args.payload_json, expected=dict),
        )
    if args.action == "quota":
        return store.record_quota_snapshot(
            source_id=args.source_id,
            contract_id=args.contract_id,
            quota_epoch=args.quota_epoch,
            used_a=args.used_a,
            remaining_a=args.remaining_a,
            total_a=args.total_a,
            reset_at=args.reset_at,
            observed_at=args.observed_at,
            source_payload=_json_value(args.payload_json, expected=dict),
            allow_total_change=args.allow_total_change,
        )
    if args.action == "revenue":
        return store.record_channel_revenue(
            model=args.model,
            channel_id=args.channel_id,
            enodot_api_key_id=args.api_key_id,
            sub2api_group_id=args.group_id,
            revenue_epoch=args.revenue_epoch,
            channel_net_k_cny=args.channel_net_k_cny,
            settlement_contract_ref=args.settlement_contract_ref,
            controlled_listing_refs=_json_value(args.listing_refs_json, expected=list),
            observed_at=args.observed_at,
            source_payload=_json_value(args.payload_json, expected=dict),
        )
    raise EconomicRoutingError("economic_cli_action_invalid")


def main() -> None:
    args = parser().parse_args()
    dsn = str(os.environ.get(args.database_dsn_env, "")).strip()
    if not dsn:
        raise EconomicRoutingError("economic_database_dsn_missing")

    def connection_factory() -> Any:
        return psycopg.connect(dsn, row_factory=dict_row)

    result = run(args, EconomicFactStore(connection_factory))
    print(json.dumps(_document(result), ensure_ascii=True, sort_keys=True, default=str))


if __name__ == "__main__":
    main()
