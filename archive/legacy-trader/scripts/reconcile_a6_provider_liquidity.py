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
from a6api_auto.storage import read_private_json
from a6api_auto.trade_provider_liquidity import (
    PostgresProviderLiquidityStore,
    ProviderLiquidityError,
    ProviderLiquidityHardLineGuard,
    ProviderLiquidityReconciler,
)
from a6api_auto.trade_provider_topup import (
    A6ProviderTopupRemote,
    PostgresProviderTopupStore,
    ProviderTopupContext,
    ProviderTopupError,
    ProviderTopupService,
    authenticated_provider_topup_client,
)

APPLY_CONFIRMATION = "a6-provider-liquidity-v1"
ENABLE_ENV = "PROVIDER_LIQUIDITY_RECONCILE_ENABLED"


class ProviderLiquidityCommandError(RuntimeError):
    pass


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(
        description="Reconcile Provider hard-line liquidity and integer-unit top-ups",
    )
    command.add_argument("--model", default="gpt-5.6-sol")
    command.add_argument("--max-providers", type=int, default=100)
    command.add_argument(
        "--proxy-map-file",
        type=Path,
        default=Path("/var/lib/a6-trader/secrets/proxy-map.json"),
    )
    command.add_argument(
        "--enodot-admin-key-file",
        type=Path,
        default=Path("/var/lib/a6-trader/secrets/enodot-admin-key.secret"),
    )
    command.add_argument("--enodot-url", default="https://ai.enodot.shop")
    command.add_argument("--compact-output", action="store_true")
    command.add_argument("--apply", action="store_true")
    command.add_argument("--apply-confirmation", default="")
    return command


def authorize(*, enabled: bool, apply: bool, confirmation: str) -> dict[str, object] | None:
    if not enabled:
        return {
            "success": True,
            "enabled": False,
            "mode": "disabled",
            "action": "disabled",
            "remote_write_count_min": 0,
        }
    if apply and confirmation != APPLY_CONFIRMATION:
        raise ProviderLiquidityCommandError(
            "provider_liquidity_apply_confirmation_invalid"
        )
    return None


def _enabled() -> bool:
    value = os.environ.get(ENABLE_ENV, "").strip().lower()
    if value in {"", "0", "false", "no"}:
        return False
    if value in {"1", "true", "yes"}:
        return True
    raise ProviderLiquidityCommandError("provider_liquidity_enable_env_invalid")


def _database_dsn() -> str:
    dsn = os.environ.get("A6_TRADE_DATABASE_DSN", "").strip()
    if not dsn:
        raise ProviderLiquidityCommandError("provider_liquidity_database_dsn_missing")
    return dsn


def _private_text(path: Path) -> str:
    resolved = path.expanduser().resolve()
    try:
        info = resolved.stat()
        value = resolved.read_text(encoding="utf-8").strip()
    except OSError:
        raise ProviderLiquidityCommandError(
            "provider_liquidity_admin_key_invalid"
        ) from None
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or not value:
        raise ProviderLiquidityCommandError("provider_liquidity_admin_key_invalid")
    return value


def _proxy_map(path: Path) -> dict[int, str]:
    try:
        payload = read_private_json(path)
        result = {int(key): str(value).strip() for key, value in payload.items()}
    except (TypeError, ValueError):
        raise ProviderLiquidityCommandError(
            "provider_liquidity_proxy_map_invalid"
        ) from None
    if not result or any(key < 1 or not value for key, value in result.items()):
        raise ProviderLiquidityCommandError("provider_liquidity_proxy_map_invalid")
    return result


def run(args: argparse.Namespace) -> dict[str, object]:
    policy = authorize(
        enabled=_enabled(),
        apply=bool(args.apply),
        confirmation=str(args.apply_confirmation or ""),
    )
    if policy is not None:
        return policy
    dsn = _database_dsn()
    proxy_by_user_id = _proxy_map(args.proxy_map_file)
    admin_key = _private_text(args.enodot_admin_key_file)

    def connection_factory():
        return psycopg.connect(dsn, row_factory=dict_row)

    liquidity_store = PostgresProviderLiquidityStore(connection_factory)
    candidates = liquidity_store.candidates(args.model, limit=args.max_providers)
    guard = ProviderLiquidityHardLineGuard(
        candidates,
        EnodotAdminClient(args.enodot_url, admin_key),
        apply=bool(args.apply),
    )
    proxy_map = dict(proxy_by_user_id)

    def client_factory(context: ProviderTopupContext):
        return authenticated_provider_topup_client(
            context,
            proxy_by_user_id=proxy_map,
        )

    topup = ProviderTopupService(
        PostgresProviderTopupStore(connection_factory),
        A6ProviderTopupRemote(client_factory),
        clock=lambda: _now(),
        apply=bool(args.apply),
        strict_liquidity=True,
    )
    return ProviderLiquidityReconciler(
        liquidity_store,
        topup,
        guard,
        model=args.model,
        apply=bool(args.apply),
        clock=_now,
        max_providers=args.max_providers,
    ).run_once(candidates)


def _now():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc)


def main() -> None:
    args = parser().parse_args()
    try:
        result = run(args)
    except (ProviderLiquidityCommandError, ProviderLiquidityError, ProviderTopupError) as exc:
        code = str(getattr(exc, "code", "") or exc)
        print(json.dumps({"success": False, "error": code}), file=sys.stderr)
        raise SystemExit(1) from None
    except Exception:
        print(
            json.dumps(
                {"success": False, "error": "provider_liquidity_reconcile_failed"}
            ),
            file=sys.stderr,
        )
        raise SystemExit(1) from None
    print(
        json.dumps(
            result,
            ensure_ascii=True,
            separators=(",", ":") if args.compact_output else None,
            indent=None if args.compact_output else 2,
        )
    )
    if result.get("success") is not True:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
