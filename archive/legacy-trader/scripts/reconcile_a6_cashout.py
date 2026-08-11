#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import socket
import sys
from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime, timezone
from importlib.resources import files
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row

from a6api_auto.client import A6ApiClient
from a6api_auto.storage import read_private_json
from a6api_auto.trade_authenticated_client import authenticated_a6_client
from a6api_auto.trade_cashout_accounts import (
    PostgresTreasuryAccountStore,
    TreasuryAccountError,
    TreasuryAccountReconciler,
)
from a6api_auto.trade_cashout_inventory import (
    CashoutInventoryError,
    load_wallet_inventory,
)
from a6api_auto.trade_cashout_orchestrator import (
    A6CryptoCashoutReconciler,
    A6CryptoCashoutRemote,
    CashoutApplyExecutor,
    CashoutError,
    CashoutOrchestrator,
    CashoutPlanner,
    CashoutPolicy,
    CashoutReconciliationWorker,
    PostgresCashoutStore,
)

ENABLE_ENV = "TREASURY_CASHOUT_ENABLED"
STANDARD_APPLY_ENV = "TREASURY_STANDARD_WITHDRAW_APPLY_ENABLED"
CALLBACK_APPLY_ENV = "TREASURY_CALLBACK_SWEEP_APPLY_ENABLED"
WALLET_ACCOUNT_APPLY_ENV = "TREASURY_WALLET_ACCOUNT_APPLY_ENABLED"
APPLY_CONFIRMATION = "a6-risk-balanced-cashout-v2"
INVENTORY_CONFIRMATION = "a6-usdt-wallet-inventory-v1"
DEFAULT_INVENTORY = Path(
    str(files("a6api_auto").joinpath("data/usdt_wallet_inventory.json"))
)


class CashoutCommandError(RuntimeError):
    pass


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(
        description="Plan or reconcile risk-balanced A6 treasury cashout intents",
    )
    command.add_argument(
        "--stage",
        choices=("all", "accounts", "reconcile", "plan"),
        default="all",
    )
    command.add_argument("--max-candidates", type=int, default=1_000)
    command.add_argument("--financial-snapshot-ttl-seconds", type=int, default=5)
    command.add_argument("--route-readback-ttl-seconds", type=int, default=86_400)
    command.add_argument("--reconcile-limit", type=int, default=100)
    command.add_argument("--reconcile-lease-seconds", type=int, default=60)
    command.add_argument("--reconcile-retry-seconds", type=int, default=300)
    command.add_argument("--client-timeout-seconds", type=float, default=10)
    command.add_argument("--wallet-inventory-file", type=Path, default=DEFAULT_INVENTORY)
    command.add_argument(
        "--proxy-map-file",
        type=Path,
        default=Path("/var/lib/a6-trader/secrets/proxy-map.json"),
    )
    command.add_argument("--sync-wallet-inventory", action="store_true")
    command.add_argument("--activate-wallets", action="store_true")
    command.add_argument("--assign-wallets", action="store_true")
    command.add_argument("--inventory-confirmation", default="")
    command.add_argument("--apply-wallet-accounts", action="store_true")
    command.add_argument("--account-max-workers", type=int, default=8)
    command.add_argument("--account-max-creates", type=int, default=24)
    command.add_argument("--apply", action="store_true")
    command.add_argument("--apply-confirmation", default="")
    command.add_argument("--compact-output", action="store_true")
    return command


def authorize(
    *,
    enabled: bool,
    apply: bool,
    apply_confirmation: str,
    standard_apply_enabled: bool,
    callback_apply_enabled: bool,
) -> dict[str, object] | None:
    if not enabled:
        return {
            "success": True,
            "enabled": False,
            "mode": "disabled",
            "remote_write_count": 0,
            "plans": [],
        }
    if apply and apply_confirmation != APPLY_CONFIRMATION:
        raise CashoutCommandError("cashout_apply_confirmation_invalid")
    if apply and not (standard_apply_enabled or callback_apply_enabled):
        raise CashoutCommandError("cashout_apply_methods_disabled")
    if not apply and (standard_apply_enabled or callback_apply_enabled):
        # Method gates may remain staged in the environment. Without --apply this
        # invocation is still plan-only and performs no remote write.
        return None
    return None


def run(args: argparse.Namespace) -> dict[str, Any]:
    enabled = _boolean_env(ENABLE_ENV)
    standard_apply_enabled = _boolean_env(STANDARD_APPLY_ENV)
    callback_apply_enabled = _boolean_env(CALLBACK_APPLY_ENV)
    wallet_account_apply_enabled = _boolean_env(WALLET_ACCOUNT_APPLY_ENV)
    disabled = authorize(
        enabled=enabled,
        apply=bool(args.apply),
        apply_confirmation=str(args.apply_confirmation or ""),
        standard_apply_enabled=standard_apply_enabled,
        callback_apply_enabled=callback_apply_enabled,
    )
    if disabled is not None:
        return disabled
    if args.activate_wallets and (
        not args.sync_wallet_inventory or args.inventory_confirmation != INVENTORY_CONFIRMATION
    ):
        raise CashoutCommandError("cashout_wallet_activation_confirmation_invalid")
    if callback_apply_enabled and args.apply:
        raise CashoutCommandError("cashout_callback_submitter_not_configured")
    if args.apply_wallet_accounts and not wallet_account_apply_enabled:
        raise CashoutCommandError("cashout_wallet_account_apply_disabled")
    if args.apply_wallet_accounts and args.stage not in {"all", "accounts"}:
        raise CashoutCommandError("cashout_wallet_account_stage_invalid")

    dsn = os.environ.get("A6_TRADE_DATABASE_DSN", "").strip()
    if not dsn:
        raise CashoutCommandError("cashout_database_dsn_missing")

    def connection_factory():
        return psycopg.connect(dsn, row_factory=dict_row)

    policy = CashoutPolicy(
        financial_snapshot_ttl_seconds=args.financial_snapshot_ttl_seconds,
        route_readback_ttl_seconds=args.route_readback_ttl_seconds,
        standard_withdraw_apply_enabled=bool(args.apply and standard_apply_enabled),
        callback_sweep_apply_enabled=bool(args.apply and callback_apply_enabled),
    )
    store = PostgresCashoutStore(
        connection_factory,
        financial_snapshot_ttl_seconds=policy.financial_snapshot_ttl_seconds,
    )
    proxy_map_cache: dict[int, str] | None = None

    def client_factory(context):
        nonlocal proxy_map_cache
        if proxy_map_cache is None:
            proxy_map_cache = _proxy_map(args.proxy_map_file)
        proxy_url = str(proxy_map_cache.get(context.a6_user_id) or "").strip()
        if not proxy_url:
            raise CashoutCommandError("cashout_proxy_missing")
        client, _record = authenticated_a6_client(
            context.session_ref,
            expected_user_id=context.a6_user_id,
            proxy_url=proxy_url,
            client_factory=lambda record, fixed_proxy: A6ApiClient(
                record.origin,
                proxy_url=fixed_proxy,
                timeout=args.client_timeout_seconds,
            ),
        )
        return client

    reconciliation: Mapping[str, Any] = {
        "success": True,
        "claimed_count": 0,
        "remote_write_count": 0,
    }
    if args.stage in {"all", "reconcile"}:
        reconciliation = CashoutReconciliationWorker(
            store,
            A6CryptoCashoutReconciler(client_factory),
            worker_id=f"{socket.gethostname()}:{os.getpid()}",
            lease_seconds=args.reconcile_lease_seconds,
            retry_interval_seconds=args.reconcile_retry_seconds,
        ).run_once(max_intents=args.reconcile_limit)
    if args.stage == "reconcile":
        return {
            "success": reconciliation.get("success") is True,
            "enabled": True,
            "stage": "reconcile",
            "reconciliation": dict(reconciliation),
            "remote_write_count": 0,
        }

    inventory_result: Mapping[str, Any] | None = None
    inventory = None
    if args.sync_wallet_inventory:
        inventory = load_wallet_inventory(args.wallet_inventory_file)
        inventory_result = store.sync_wallet_inventory(
            inventory,
            activate=bool(args.activate_wallets),
        )

    account_result: Mapping[str, Any] | None = None
    assignments: dict[int, int] = {}
    account_stage_requested = args.stage == "accounts" or (
        args.stage == "all"
        and (args.sync_wallet_inventory or args.assign_wallets or args.apply_wallet_accounts)
    )
    if account_stage_requested:
        if inventory is None:
            inventory = load_wallet_inventory(args.wallet_inventory_file)
        account_store = PostgresTreasuryAccountStore(connection_factory)
        released_assignments = account_store.release_ineligible_assignments()
        if args.assign_wallets:
            assignments = dict(
                store.assign_wallet_principals(
                    account_store.list_assignable_merchant_ids(),
                    config_version=policy.config_version,
                )
            )
        account_result = TreasuryAccountReconciler(
            account_store,
            client_factory,
            max_workers=args.account_max_workers,
        ).run_once(
            inventory,
            apply=bool(args.apply_wallet_accounts),
            max_creates=args.account_max_creates,
        )
    if args.stage == "accounts":
        success = bool(account_result and account_result.get("success") is True)
        return {
            "success": success,
            "enabled": True,
            "stage": "accounts",
            "inventory": dict(inventory_result or {}),
            "wallet_assignments": len(assignments),
            "released_assignments": released_assignments,
            "accounts": dict(account_result or {}),
            "remote_write_count": int(
                (account_result or {}).get("remote_write_count") or 0
            ),
        }

    now = datetime.now(timezone.utc)
    candidates = store.load_candidates(now, policy=policy, limit=args.max_candidates)
    if args.assign_wallets and candidates:
        assignments = dict(
            store.assign_wallet_principals(
                [candidate.context.merchant_id for candidate in candidates],
                config_version=policy.config_version,
            )
        )
        candidates = store.load_candidates(now, policy=policy, limit=args.max_candidates)

    planner = CashoutPlanner(policy)
    crypto_remote = None
    if args.apply and standard_apply_enabled:
        crypto_remote = A6CryptoCashoutRemote(client_factory, policy=policy)
        refreshed = []
        for candidate in candidates:
            local_plan = planner.plan(candidate.source)
            if local_plan.method != "crypto":
                refreshed.append(candidate)
                continue
            try:
                refreshed.append(crypto_remote.preview_candidate(candidate))
            except Exception:
                refreshed.append(
                    replace(candidate, source=replace(candidate.source, routes=()))
                )
        candidates = tuple(refreshed)

    executor = (
        CashoutApplyExecutor(
            store,
            crypto_remote=crypto_remote,
            policy=policy,
            clock=lambda: datetime.now(timezone.utc),
        )
        if args.apply
        else None
    )
    result = CashoutOrchestrator(
        planner,
        store,
        executor,
        enabled=True,
        plan_only=not args.apply,
    ).run_once(tuple(candidate.source for candidate in candidates))
    result["inventory"] = dict(inventory_result or {})
    result["wallet_assignments"] = len(assignments)
    result["reconciliation"] = reconciliation
    result["accounts"] = dict(account_result or {})
    result["stage"] = "plan" if args.stage == "plan" else "all"
    return result


def _boolean_env(name: str) -> bool:
    value = os.environ.get(name, "").strip().lower()
    if value in {"", "0", "false", "no"}:
        return False
    if value in {"1", "true", "yes"}:
        return True
    raise CashoutCommandError(f"cashout_boolean_env_invalid:{name.lower()}")


def _proxy_map(path: Path) -> dict[int, str]:
    try:
        document = read_private_json(path)
        result = {int(key): str(value).strip() for key, value in document.items()}
    except (OSError, TypeError, ValueError):
        raise CashoutCommandError("cashout_proxy_map_invalid") from None
    if not result or any(key < 1 or not value for key, value in result.items()):
        raise CashoutCommandError("cashout_proxy_map_invalid")
    return result


def main() -> None:
    args = parser().parse_args()
    try:
        result = run(args)
    except (
        CashoutCommandError,
        CashoutError,
        CashoutInventoryError,
        TreasuryAccountError,
    ) as exc:
        error = str(getattr(exc, "code", "") or exc)
        print(json.dumps({"success": False, "error": error}), file=sys.stderr)
        raise SystemExit(1) from None
    except Exception:
        print(
            json.dumps({"success": False, "error": "cashout_reconcile_failed"}),
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
