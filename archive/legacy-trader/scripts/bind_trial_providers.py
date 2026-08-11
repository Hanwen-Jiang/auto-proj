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

TRIAL_PROVIDERS = (
    ProviderBindingRequest(
        merchant_id=4,
        external_a6_token_id=12509,
        proxy_id=19,
        concurrency=100,
    ),
    ProviderBindingRequest(
        merchant_id=5,
        external_a6_token_id=14020,
        proxy_id=5,
        concurrency=100,
    ),
    ProviderBindingRequest(
        merchant_id=19,
        external_a6_token_id=14093,
        proxy_id=18,
        concurrency=100,
    ),
)


def _private_text(path: Path, error: str) -> str:
    resolved = path.expanduser().resolve()
    try:
        info = resolved.stat()
        value = resolved.read_text(encoding="utf-8").strip()
    except OSError:
        raise ProviderBindingError(error) from None
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or not value:
        raise ProviderBindingError(error)
    return value


def _proxy_map(path: Path) -> dict[int, str]:
    try:
        payload = read_private_json(path)
        result = {int(user_id): str(proxy) for user_id, proxy in payload.items()}
    except (TypeError, ValueError):
        raise ProviderBindingError("provider_binding_proxy_map_invalid") from None
    if not result or any(user_id < 1 or not proxy for user_id, proxy in result.items()):
        raise ProviderBindingError("provider_binding_proxy_map_invalid")
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
    command = argparse.ArgumentParser(description="Reconcile the Phase 1 trial Providers")
    command.add_argument("--apply", action="store_true", help="apply writes; default is dry-run")
    command.add_argument(
        "--merchant",
        action="append",
        type=int,
        choices=tuple(request.merchant_id for request in TRIAL_PROVIDERS),
        help="reconcile only the selected merchant; repeat to select more than one",
    )
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


def main() -> None:
    args = parser().parse_args()
    dsn = os.environ.get("A6_TRADE_DATABASE_DSN", "").strip()
    if not dsn:
        raise ProviderBindingError("provider_binding_database_dsn_missing")
    proxy_by_user_id = _proxy_map(args.proxy_map_file)
    admin_key = _private_text(args.admin_key_file, "provider_binding_admin_key_invalid")

    def connection_factory():
        return psycopg.connect(dsn, row_factory=dict_row)

    executor = ProviderBindingExecutor(
        PostgresProviderBindingStore(connection_factory),
        EnodotAdminClient(args.enodot_url, admin_key),
        key_revealer=_key_revealer(proxy_by_user_id),
    )
    selected_merchants = set(args.merchant or ())
    requests = tuple(
        request
        for request in TRIAL_PROVIDERS
        if not selected_merchants or request.merchant_id in selected_merchants
    )
    results = [request_result.summary() for request_result in (
        executor.execute(request, apply=args.apply) for request in requests
    )]
    print(json.dumps({"success": True, "results": results}, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    try:
        main()
    except ProviderBindingError as exc:
        print(json.dumps({"success": False, "error": exc.code}), file=sys.stderr)
        raise SystemExit(1) from None
