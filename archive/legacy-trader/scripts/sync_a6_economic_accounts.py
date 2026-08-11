#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row

from a6api_auto.enodot_admin import EnodotAdminClient
from a6api_auto.trade_economic_accounts import (
    EconomicAccountSyncConfig,
    PostgresEconomicAccountStore,
    sync_owned_accounts,
)
from a6api_auto.trade_economic_source import EconomicRoutingError

_TRUE = frozenset({"1", "true", "yes", "on"})
_FALSE = frozenset({"", "0", "false", "no", "off"})


def _env_bool(name: str, default: bool) -> bool:
    if name not in os.environ:
        return default
    value = str(os.environ.get(name, "")).strip().casefold()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    raise EconomicRoutingError(f"economic_account_env_flag_invalid:{name}")


def _private_text(path: Path) -> str:
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError:
        raise EconomicRoutingError("economic_account_admin_key_unreadable") from None
    if not value:
        raise EconomicRoutingError("economic_account_admin_key_invalid")
    return value


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(
        description="Discover and cost owned weekly accounts for Priority 1 routing"
    )
    command.add_argument("--enabled", action=argparse.BooleanOptionalAction, default=None)
    command.add_argument(
        "--enodot-url",
        default=os.environ.get("ECONOMIC_ACCOUNT_ENODOT_URL", "https://ai.enodot.shop"),
    )
    command.add_argument(
        "--admin-key-file",
        type=Path,
        default=Path("/var/lib/a6-trader/secrets/enodot-admin-key.secret"),
    )
    command.add_argument("--database-dsn-env", default="A6_TRADE_DATABASE_DSN")
    command.add_argument("--model", default=os.environ.get("ECONOMIC_ACCOUNT_MODEL", "gpt-5.6-sol"))
    command.add_argument(
        "--account-name", default=os.environ.get("ECONOMIC_ACCOUNT_NAME", "gpt-plus")
    )
    command.add_argument(
        "--group-id",
        type=int,
        default=int(os.environ.get("ECONOMIC_ACCOUNT_GROUP_ID", "26")),
    )
    command.add_argument(
        "--provisional-total-a",
        type=Decimal,
        default=Decimal(os.environ.get("ECONOMIC_ACCOUNT_PROVISIONAL_TOTAL_A", "100")),
    )
    command.add_argument(
        "--minimum-total-a",
        type=Decimal,
        default=Decimal(os.environ.get("ECONOMIC_ACCOUNT_MINIMUM_TOTAL_A", "50")),
    )
    command.add_argument(
        "--maximum-total-a",
        type=Decimal,
        default=Decimal(os.environ.get("ECONOMIC_ACCOUNT_MAXIMUM_TOTAL_A", "300")),
    )
    command.add_argument(
        "--usage-workers",
        type=int,
        default=int(os.environ.get("ECONOMIC_ACCOUNT_USAGE_WORKERS", "4")),
    )
    return command


def run(args: argparse.Namespace) -> dict[str, Any]:
    enabled = (
        _env_bool("ECONOMIC_ACCOUNT_SYNC_ENABLED", True)
        if args.enabled is None
        else bool(args.enabled)
    )
    if not enabled:
        return {"success": True, "enabled": False, "action": "disabled"}
    dsn = str(os.environ.get(args.database_dsn_env, "")).strip()
    if not dsn:
        raise EconomicRoutingError("economic_account_database_dsn_missing")
    admin_key = _private_text(args.admin_key_file)
    config = EconomicAccountSyncConfig(
        model=args.model,
        account_name=args.account_name,
        group_id=args.group_id,
        provisional_total_a=args.provisional_total_a,
        minimum_total_a=args.minimum_total_a,
        maximum_total_a=args.maximum_total_a,
        usage_workers=args.usage_workers,
    )

    def connection_factory() -> Any:
        return psycopg.connect(dsn, row_factory=dict_row)

    def admin_factory() -> EnodotAdminClient:
        return EnodotAdminClient(args.enodot_url, admin_key)

    return sync_owned_accounts(
        admin_factory(),
        PostgresEconomicAccountStore(connection_factory),
        config=config,
        usage_client_factory=admin_factory,
    )


def main() -> None:
    result = run(parser().parse_args())
    print(json.dumps(result, ensure_ascii=True, sort_keys=True, default=str))
    if not result.get("success"):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
