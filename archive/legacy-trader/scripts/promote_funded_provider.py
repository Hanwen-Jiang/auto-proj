#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import stat
import sys
from collections.abc import Mapping
from pathlib import Path

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
    ProjectedProviderRoleStore,
    load_merchant_roles,
)
from a6api_auto.trade_roles import (
    RoleAssignment,
    RoleAssignmentError,
    assign_merchant_roles,
)

PROVIDER_GROUP_ID = 26
CONFIG_VERSION = "a6-provider-funded-promotion-v1"
REQUESTED_BY = "a6-trader-provider-promotion"


class ProviderPromotionError(RuntimeError):
    pass


def _private_text(path: Path, error: str) -> str:
    resolved = path.expanduser().resolve()
    try:
        info = resolved.stat()
        value = resolved.read_text(encoding="utf-8").strip()
    except OSError:
        raise ProviderPromotionError(error) from None
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or not value:
        raise ProviderPromotionError(error)
    return value


def _proxy_map(path: Path) -> dict[int, str]:
    try:
        payload = read_private_json(path)
        result = {int(user_id): str(proxy) for user_id, proxy in payload.items()}
    except (TypeError, ValueError):
        raise ProviderPromotionError("provider_promotion_proxy_map_invalid") from None
    if not result or any(user_id < 1 or not proxy for user_id, proxy in result.items()):
        raise ProviderPromotionError("provider_promotion_proxy_map_invalid")
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
        description="Promote one funded Seller to an idempotent pool-mode A6 Provider",
    )
    command.add_argument("--merchant", type=int, required=True)
    command.add_argument("--token", type=int, required=True, help="external A6 token id")
    command.add_argument(
        "--proxy-id",
        type=int,
        required=True,
        help="active Enodot proxy whose port matches the merchant session lease",
    )
    command.add_argument("--apply", action="store_true", help="apply writes; default is dry-run")
    command.add_argument(
        "--stage",
        choices=("all", "role", "binding"),
        default="all",
        help="run one resumable stage or the complete promotion",
    )
    command.add_argument("--priority", type=int, choices=(1, 2), default=1)
    command.add_argument("--concurrency", type=int, default=100)
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


def _assign_provider_role(connection_factory, merchant_id: int, *, apply: bool):
    connection = connection_factory()
    try:
        return assign_merchant_roles(
            connection,
            (RoleAssignment(merchant_id, "provider"),),
            apply=apply,
            config_version=CONFIG_VERSION,
            requested_by=REQUESTED_BY,
            expected_previous_roles={
                merchant_id: (("seller",), ("seller", "provider")),
            },
        ).summary()
    finally:
        connection.close()


def _require_provider_role(connection_factory, merchant_id: int) -> None:
    roles = load_merchant_roles(connection_factory, (merchant_id,)).get(merchant_id)
    if roles not in {("seller", "provider"), ("provider",)}:
        raise ProviderPromotionError("provider_promotion_role_missing")


def main() -> None:
    args = parser().parse_args()
    if args.merchant <= 0 or args.token <= 0 or args.proxy_id <= 0:
        raise ProviderPromotionError("provider_promotion_identity_invalid")
    if not 1 <= args.concurrency <= 1_000_000:
        raise ProviderPromotionError("provider_promotion_concurrency_invalid")
    dsn = os.environ.get("A6_TRADE_DATABASE_DSN", "").strip()
    if not dsn:
        raise ProviderPromotionError("provider_promotion_database_dsn_missing")
    admin_key = _private_text(args.admin_key_file, "provider_promotion_admin_key_invalid")
    proxy_by_user_id = _proxy_map(args.proxy_map_file) if args.stage in {"all", "binding"} else {}

    def connection_factory():
        return psycopg.connect(dsn, row_factory=dict_row)

    stages: dict[str, object] = {}
    if args.stage in {"all", "role"}:
        stages["role"] = _assign_provider_role(
            connection_factory,
            args.merchant,
            apply=args.apply,
        )

    if args.stage in {"all", "binding"}:
        binding_store = PostgresProviderBindingStore(connection_factory)
        if args.apply:
            _require_provider_role(connection_factory, args.merchant)
        else:
            binding_store = ProjectedProviderRoleStore(binding_store, args.merchant)
        executor = ProviderBindingExecutor(
            binding_store,
            EnodotAdminClient(args.enodot_url, admin_key),
            key_revealer=_key_revealer(proxy_by_user_id),
        )
        stages["binding"] = executor.execute(
            ProviderBindingRequest(
                merchant_id=args.merchant,
                external_a6_token_id=args.token,
                proxy_id=args.proxy_id,
                group_ids=(PROVIDER_GROUP_ID,),
                concurrency=args.concurrency,
                priority=args.priority,
                config_version=CONFIG_VERSION,
                requested_by=REQUESTED_BY,
            ),
            apply=args.apply,
        ).summary()

    print(
        json.dumps(
            {
                "success": True,
                "mode": "applied" if args.apply else "dry-run",
                "merchant_id": args.merchant,
                "stages": stages,
            },
            ensure_ascii=True,
            indent=2,
        )
    )


if __name__ == "__main__":
    try:
        main()
    except (ProviderBindingError, ProviderPromotionError, RoleAssignmentError) as exc:
        code = str(getattr(exc, "code", "") or exc)
        print(json.dumps({"success": False, "error": code}), file=sys.stderr)
        raise SystemExit(1) from None
