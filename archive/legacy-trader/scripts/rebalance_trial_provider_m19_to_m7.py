#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import stat
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row

from a6api_auto.client import A6ApiClient
from a6api_auto.enodot_admin import EnodotAdminClient
from a6api_auto.storage import read_private_json
from a6api_auto.trade_provider_binding import (
    PostgresProviderBindingStore,
    ProviderBindingError,
    ProviderBindingExecutor,
    ProviderBindingRequest,
)
from a6api_auto.trade_provider_rebalance import (
    PostgresProviderRetirementStore,
    ProjectedProviderRoleStore,
    ProviderAccountRetirementExecutor,
    ProviderRebalanceError,
    ProviderRetirementRequest,
    assert_role_switch_readback,
    load_merchant_roles,
)
from a6api_auto.trade_roles import (
    RoleAssignment,
    RoleAssignmentError,
    assign_merchant_roles,
)

OUTGOING_MERCHANT_ID = 19
OUTGOING_TOKEN_ID = 14093
OUTGOING_ACCOUNT_ID = 45896
INCOMING_MERCHANT_ID = 7
INCOMING_TOKEN_ID = 12465
INCOMING_PROXY_ID = 7
PROVIDER_GROUP_ID = 26
CONFIG_VERSION = "phase1-provider-rebalance-m19-m7-v1"
REQUESTED_BY = "a6-trader-provider-rebalance"
ROLE_ASSIGNMENTS = (
    RoleAssignment(INCOMING_MERCHANT_ID, "provider"),
    RoleAssignment(OUTGOING_MERCHANT_ID, "seller"),
)
EXPECTED_PREVIOUS_ROLES = {
    OUTGOING_MERCHANT_ID: (("seller", "provider"), ("seller",)),
    INCOMING_MERCHANT_ID: (("seller",), ("seller", "provider")),
}


def _private_text(path: Path, error: str) -> str:
    resolved = path.expanduser().resolve()
    try:
        info = resolved.stat()
        value = resolved.read_text(encoding="utf-8").strip()
    except OSError:
        raise ProviderRebalanceError(error) from None
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or not value:
        raise ProviderRebalanceError(error)
    return value


def _proxy_map(path: Path) -> dict[int, str]:
    try:
        payload = read_private_json(path)
        result = {int(user_id): str(proxy) for user_id, proxy in payload.items()}
    except (TypeError, ValueError):
        raise ProviderRebalanceError("provider_rebalance_proxy_map_invalid") from None
    if not result or any(user_id < 1 or not proxy for user_id, proxy in result.items()):
        raise ProviderRebalanceError("provider_rebalance_proxy_map_invalid")
    return result


def _key_revealer(proxy_by_user_id: Mapping[int, str]):
    def reveal(context, record) -> str:
        proxy_url = proxy_by_user_id.get(context.a6_user_id)
        if not proxy_url:
            raise ProviderBindingError("provider_binding_proxy_mapping_missing")
        client = A6ApiClient(record.origin, proxy_url=proxy_url)
        client.import_cookies(record.cookies)
        matches = [
            item
            for item in client.list_tokens(context.a6_user_id)
            if int(item.get("id") or 0) == context.external_a6_token_id
        ]
        if len(matches) != 1 or str(matches[0].get("name") or "") != context.token_name:
            raise ProviderBindingError("provider_binding_token_readback_mismatch")
        status, document = client._request(
            "POST",
            f"/api/token/{context.external_a6_token_id}/key",
            user_id=context.a6_user_id,
            referer="/console/token",
        )
        data = document.get("data")
        key = str(data.get("key") or "").strip() if isinstance(data, Mapping) else ""
        if status != 200 or document.get("success") is not True or not key:
            raise ProviderBindingError("provider_binding_key_reveal_failed")
        return key

    return reveal


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(
        description="Retire merchant 19 Provider and idempotently promote merchant 7",
    )
    command.add_argument("--apply", action="store_true", help="apply writes; default is dry-run")
    command.add_argument(
        "--stage",
        choices=("all", "retire-old", "switch-roles", "bind-new"),
        default="all",
        help="apply or inspect one resumable stage",
    )
    command.add_argument("--priority", type=int, choices=(1, 2), default=1)
    command.add_argument("--enodot-url", default="https://ai.enodot.shop")
    command.add_argument(
        "--admin-key-file",
        type=Path,
        default=Path("/var/lib/a6-trader/secrets/enodot-admin-key.secret"),
    )
    command.add_argument(
        "--proxy-map-file",
        type=Path,
        default=Path("/var/lib/a6-trader/secrets/proxy-map.json"),
    )
    return command


def _switch_roles(connection_factory, *, apply: bool) -> dict[str, Any]:
    connection = connection_factory()
    try:
        result = assign_merchant_roles(
            connection,
            ROLE_ASSIGNMENTS,
            apply=apply,
            config_version=CONFIG_VERSION,
            requested_by=REQUESTED_BY,
            expected_previous_roles=EXPECTED_PREVIOUS_ROLES,
        )
    finally:
        connection.close()
    roles = load_merchant_roles(
        connection_factory,
        (OUTGOING_MERCHANT_ID, INCOMING_MERCHANT_ID),
    )
    if apply:
        assert_role_switch_readback(
            roles,
            outgoing_merchant_id=OUTGOING_MERCHANT_ID,
            incoming_merchant_id=INCOMING_MERCHANT_ID,
        )
    summary = result.summary()
    summary["observed_roles"] = {str(key): list(value) for key, value in sorted(roles.items())}
    summary["desired_roles"] = {
        str(OUTGOING_MERCHANT_ID): ["seller"],
        str(INCOMING_MERCHANT_ID): ["seller", "provider"],
    }
    summary["verified"] = apply
    return summary


def _require_retired(result: Mapping[str, Any]) -> None:
    if result.get("action") != "noop" or result.get("binding_state_before") != "retired":
        raise ProviderRebalanceError("provider_rebalance_retirement_prerequisite_missing")


def _require_switched_roles(connection_factory) -> None:
    roles = load_merchant_roles(
        connection_factory,
        (OUTGOING_MERCHANT_ID, INCOMING_MERCHANT_ID),
    )
    assert_role_switch_readback(
        roles,
        outgoing_merchant_id=OUTGOING_MERCHANT_ID,
        incoming_merchant_id=INCOMING_MERCHANT_ID,
    )


def main() -> None:
    args = parser().parse_args()
    dsn = os.environ.get("A6_TRADE_DATABASE_DSN", "").strip()
    if not dsn:
        raise ProviderRebalanceError("provider_rebalance_database_dsn_missing")
    admin_key = _private_text(
        args.admin_key_file,
        "provider_rebalance_admin_key_invalid",
    )
    proxy_by_user_id = _proxy_map(args.proxy_map_file) if args.stage in {"all", "bind-new"} else {}

    def connection_factory():
        return psycopg.connect(dsn, row_factory=dict_row)

    admin = EnodotAdminClient(args.enodot_url, admin_key)
    retirement_request = ProviderRetirementRequest(
        merchant_id=OUTGOING_MERCHANT_ID,
        external_a6_token_id=OUTGOING_TOKEN_ID,
        enodot_account_id=OUTGOING_ACCOUNT_ID,
        group_id=PROVIDER_GROUP_ID,
        config_version=CONFIG_VERSION,
        requested_by=REQUESTED_BY,
    )
    retirement = ProviderAccountRetirementExecutor(
        PostgresProviderRetirementStore(connection_factory),
        admin,
    )
    binding_store = PostgresProviderBindingStore(connection_factory)
    binding_request = ProviderBindingRequest(
        merchant_id=INCOMING_MERCHANT_ID,
        external_a6_token_id=INCOMING_TOKEN_ID,
        proxy_id=INCOMING_PROXY_ID,
        group_ids=(PROVIDER_GROUP_ID,),
        concurrency=100,
        priority=args.priority,
        config_version=CONFIG_VERSION,
        requested_by=REQUESTED_BY,
    )
    stages: dict[str, Any] = {}

    if args.stage in {"all", "retire-old"}:
        stages["retire-old"] = retirement.execute(
            retirement_request,
            apply=args.apply,
        ).summary()

    if args.stage in {"all", "switch-roles"}:
        if args.apply and args.stage == "switch-roles":
            _require_retired(retirement.execute(retirement_request).summary())
        stages["switch-roles"] = _switch_roles(connection_factory, apply=args.apply)

    if args.stage in {"all", "bind-new"}:
        if args.apply:
            if args.stage == "bind-new":
                _require_retired(retirement.execute(retirement_request).summary())
            _require_switched_roles(connection_factory)
            binding_executor = ProviderBindingExecutor(
                binding_store,
                admin,
                key_revealer=_key_revealer(proxy_by_user_id),
            )
        else:
            binding_executor = ProviderBindingExecutor(
                ProjectedProviderRoleStore(binding_store, INCOMING_MERCHANT_ID),
                admin,
                key_revealer=_key_revealer(proxy_by_user_id),
            )
        stages["bind-new"] = binding_executor.execute(
            binding_request,
            apply=args.apply,
        ).summary()

    print(
        json.dumps(
            {
                "success": True,
                "mode": "applied" if args.apply else "dry-run",
                "selected_stage": args.stage,
                "stages": stages,
            },
            ensure_ascii=True,
            indent=2,
        )
    )


if __name__ == "__main__":
    try:
        main()
    except (ProviderBindingError, ProviderRebalanceError, RoleAssignmentError) as exc:
        code = str(getattr(exc, "code", "") or exc)
        print(json.dumps({"success": False, "error": code}), file=sys.stderr)
        raise SystemExit(1) from None
