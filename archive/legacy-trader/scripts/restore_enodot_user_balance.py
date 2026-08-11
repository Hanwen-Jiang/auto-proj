#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import stat
import sys
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

from a6api_auto.enodot_admin import EnodotAdminClient
from a6api_auto.trade_enodot_balance import (
    DEFAULT_CONFIG_VERSION,
    EnodotBalanceRecoveryError,
    EnodotBalanceRecoveryRequest,
    EnodotBalanceRecoveryService,
    PostgresEnodotBalanceAuditStore,
)


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(
        description="Restore an Enodot managed Seller to an exact working balance"
    )
    command.add_argument(
        "--apply",
        action="store_true",
        help="apply the credit; default is dry-run",
    )
    command.add_argument("--user-id", type=int, default=17)
    command.add_argument("--expected-balance", default="-5.37927575")
    command.add_argument("--target-balance", default="1.00000000")
    command.add_argument("--enodot-url", default="https://ai.enodot.shop")
    command.add_argument(
        "--admin-key-file",
        type=Path,
        default=Path("/var/lib/a6-trader/secrets/enodot-admin-key.secret"),
    )
    command.add_argument(
        "--admin-key-env",
        default="",
        help="environment containing the Admin key; takes precedence over --admin-key-file",
    )
    command.add_argument(
        "--database-dsn-env",
        default="A6_TRADE_DATABASE_DSN",
        help="environment containing the a6_trade PostgreSQL DSN",
    )
    command.add_argument("--config-version", default=DEFAULT_CONFIG_VERSION)
    command.add_argument("--requested-by", default="restore-enodot-user-balance")
    command.add_argument(
        "--notes",
        default="Restore managed Seller 17 after confirmed legacy-route upstream loss",
    )
    return command


def _private_text(path: Path) -> str:
    resolved = path.expanduser().resolve()
    try:
        info = resolved.stat()
        value = resolved.read_text(encoding="utf-8").strip()
    except OSError:
        raise EnodotBalanceRecoveryError("enodot_balance_admin_key_unreadable") from None
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or not value:
        raise EnodotBalanceRecoveryError("enodot_balance_admin_key_permissions")
    return value


def _admin_key(args: argparse.Namespace) -> str:
    env_name = str(args.admin_key_env or "").strip()
    if env_name:
        value = os.environ.get(env_name, "").strip()
        if not value:
            raise EnodotBalanceRecoveryError("enodot_balance_admin_key_environment_empty")
        return value
    return _private_text(args.admin_key_file)


def main() -> None:
    args = parser().parse_args()
    dsn = os.environ.get(args.database_dsn_env, "").strip()
    if args.apply and not dsn:
        raise EnodotBalanceRecoveryError("enodot_balance_database_dsn_missing")
    request = EnodotBalanceRecoveryRequest(
        user_id=args.user_id,
        expected_balance_usd=args.expected_balance,
        target_balance_usd=args.target_balance,
        config_version=args.config_version,
        requested_by=args.requested_by,
        notes=args.notes,
    )

    def connection_factory():
        return psycopg.connect(dsn, row_factory=dict_row)

    store = PostgresEnodotBalanceAuditStore(connection_factory)
    service = EnodotBalanceRecoveryService(
        store,
        EnodotAdminClient(args.enodot_url, _admin_key(args)),
    )
    result = service.execute(request, apply=args.apply)
    output = result.public()
    output["mode"] = "apply" if args.apply else "dry-run"
    output["idempotency_key"] = request.idempotency_key
    print(json.dumps(output, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    try:
        main()
    except EnodotBalanceRecoveryError as exc:
        print(json.dumps({"success": False, "error": exc.code}), file=sys.stderr)
        raise SystemExit(1) from None
