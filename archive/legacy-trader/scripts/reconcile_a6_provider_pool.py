#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import re
import stat
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from a6api_auto.client import A6ApiClient
from a6api_auto.enodot_admin import EnodotAdminClient
from a6api_auto.errors import A6AutoError
from a6api_auto.storage import SessionStore, read_private_json

_RELAY_ORIGIN = "http://127.0.0.1:8767"
_MANAGED_PROVIDER_NAME = re.compile(
    r"^a6-provider-merchant-(?P<merchant_id>[1-9][0-9]*)-token-"
    r"(?P<external_token_id>[1-9][0-9]*)$"
)


def _fixed_relay_origin(value: object) -> str:
    """Keep Provider accounts on the local relay owned by the service unit."""

    candidate = str(value or "").strip()
    try:
        parsed = urlsplit(candidate.rstrip("/"))
    except ValueError:
        raise A6AutoError("provider_pool_relay_origin_invalid") from None
    try:
        port = parsed.port
    except ValueError:
        port = -1
    if (
        parsed.scheme.casefold() != "http"
        or parsed.hostname != "127.0.0.1"
        or port != 8767
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise A6AutoError("provider_pool_relay_origin_invalid")
    return _RELAY_ORIGIN


def _private_text(path: Path) -> str:
    resolved = path.expanduser().resolve()
    try:
        info = resolved.stat()
        value = resolved.read_text(encoding="utf-8").strip()
    except OSError:
        raise A6AutoError("provider_pool_admin_key_unreadable") from None
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or not value:
        raise A6AutoError("provider_pool_admin_key_invalid")
    return value


def _provider_update(
    account: Mapping[str, Any],
    *,
    api_key: str,
    relay_origin: str,
) -> tuple[dict[str, Any], str]:
    relay_origin = _fixed_relay_origin(relay_origin)
    account_id = int(account.get("id") or 0)
    name = str(account.get("name") or "")
    extra = account.get("extra")
    extra = extra if isinstance(extra, Mapping) else {}
    user_id = int(extra.get("a6_user_id") or 0)
    credentials = account.get("credentials")
    credentials = dict(credentials) if isinstance(credentials, Mapping) else {}
    if account_id < 1 or not name.startswith("a6-provider-") or user_id < 1:
        raise A6AutoError("provider_pool_account_invalid")
    api_key = str(api_key).strip()
    if not api_key:
        raise A6AutoError("provider_pool_api_key_missing")

    desired_base_url = f"{relay_origin.rstrip('/')}/a6-user/{user_id}/v1"
    desired_credentials = {
        **credentials,
        "api_key": api_key,
        "base_url": desired_base_url,
        "pool_mode": True,
        "pool_mode_retry_count": 0,
        "pool_mode_retry_status_codes": [],
    }
    fingerprint = hashlib.sha256(api_key.encode()).hexdigest()
    desired_extra = {
        **extra,
        "a6_provider": True,
        "a6_api_key_fingerprint": fingerprint,
    }
    update: dict[str, Any] = {}
    credentials_changed = (
        credentials.get("base_url") != desired_base_url
        or credentials.get("pool_mode") is not True
        or credentials.get("pool_mode_retry_count") != 0
        or list(credentials.get("pool_mode_retry_status_codes") or []) != []
        or extra.get("a6_api_key_fingerprint") != fingerprint
    )
    if credentials_changed:
        update["credentials"] = desired_credentials
    if (
        extra.get("a6_provider") is not True
        or extra.get("a6_api_key_fingerprint") != fingerprint
    ):
        update["extra"] = desired_extra
    if account.get("proxy_id") is not None:
        update["proxy_id"] = 0
    return update, desired_base_url


def _managed_provider_accounts(accounts: object) -> list[Mapping[str, Any]]:
    if not isinstance(accounts, list):
        raise A6AutoError("provider_pool_accounts_invalid")
    selected: list[Mapping[str, Any]] = []
    account_ids: set[int] = set()
    binding_uids: set[str] = set()
    identities: set[tuple[int, int]] = set()
    for raw in accounts:
        if not isinstance(raw, Mapping):
            raise A6AutoError("provider_pool_account_invalid")
        name = str(raw.get("name") or "").strip()
        match = _MANAGED_PROVIDER_NAME.fullmatch(name)
        if match is None:
            continue
        extra = raw.get("extra")
        if not isinstance(extra, Mapping) or extra.get("managed_by") != "a6-trader":
            raise A6AutoError("provider_pool_account_identity_invalid")
        try:
            account_id = int(raw.get("id") or 0)
            merchant_id = int(extra.get("a6_merchant_id") or 0)
            external_token_id = int(extra.get("external_a6_token_id") or 0)
        except (TypeError, ValueError):
            raise A6AutoError("provider_pool_account_identity_invalid") from None
        identity = (merchant_id, external_token_id)
        binding_uid = str(extra.get("a6_binding_uid") or "").strip()
        if (
            account_id < 1
            or merchant_id != int(match.group("merchant_id"))
            or external_token_id != int(match.group("external_token_id"))
            or binding_uid != f"a6:merchant-{merchant_id}:token-{external_token_id}"
            or account_id in account_ids
            or binding_uid in binding_uids
            or identity in identities
        ):
            raise A6AutoError("provider_pool_account_identity_invalid")
        account_ids.add(account_id)
        binding_uids.add(binding_uid)
        identities.add(identity)
        selected.append(raw)
    return sorted(selected, key=lambda item: int(item["id"]))


def _proxy_map(path: Path) -> dict[int, str]:
    try:
        values = read_private_json(path)
        result = {int(user_id): str(proxy).strip() for user_id, proxy in values.items()}
    except (TypeError, ValueError):
        raise A6AutoError("provider_pool_proxy_map_invalid") from None
    if not result or any(user_id < 1 or not proxy for user_id, proxy in result.items()):
        raise A6AutoError("provider_pool_proxy_map_invalid")
    return result


def _session_records(path: Path) -> dict[int, Any]:
    records = {}
    for session_path in sorted(path.expanduser().resolve().glob("*.json")):
        record = SessionStore(session_path).load()
        if record.user_id:
            records[int(record.user_id)] = record
    if not records:
        raise A6AutoError("provider_pool_sessions_empty")
    return records


def _reveal_api_key(
    account: Mapping[str, Any],
    *,
    records: Mapping[int, Any],
    proxies: Mapping[int, str],
) -> str:
    extra = account.get("extra")
    extra = extra if isinstance(extra, Mapping) else {}
    user_id = int(extra.get("a6_user_id") or 0)
    token_id = int(extra.get("external_a6_token_id") or 0)
    record = records.get(user_id)
    proxy = proxies.get(user_id)
    if record is None or not proxy or token_id < 1:
        raise A6AutoError("provider_pool_key_source_missing")
    session_key = str(getattr(record, "a6api_key", "") or "").strip()
    expected_fingerprint = str(extra.get("a6_api_key_fingerprint") or "")
    if (
        session_key
        and expected_fingerprint
        and hashlib.sha256(session_key.encode()).hexdigest() == expected_fingerprint
    ):
        return session_key
    client = A6ApiClient(record.origin, proxy_url=proxy)
    client.import_cookies(record.cookies)
    status, document = client._request(
        "POST",
        f"/api/token/{token_id}/key",
        user_id=user_id,
        referer="/console/token",
    )
    data = document.get("data") if isinstance(document, Mapping) else None
    api_key = str(data.get("key") or "").strip() if isinstance(data, Mapping) else ""
    if status != 200 or document.get("success") is not True or not api_key:
        raise A6AutoError("provider_pool_key_reveal_failed")
    return api_key


def _verify(account: Mapping[str, Any], *, desired_base_url: str) -> None:
    credentials = account.get("credentials")
    credentials = credentials if isinstance(credentials, Mapping) else {}
    extra = account.get("extra")
    extra = extra if isinstance(extra, Mapping) else {}
    if (
        credentials.get("base_url") != desired_base_url
        or credentials.get("pool_mode") is not True
        or credentials.get("pool_mode_retry_count") != 0
        or list(credentials.get("pool_mode_retry_status_codes") or []) != []
        or extra.get("a6_provider") is not True
        or account.get("proxy_id") is not None
    ):
        raise A6AutoError("provider_pool_readback_mismatch")


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(description="Reconcile all managed A6 Provider pool fields")
    command.add_argument("--apply", action="store_true")
    command.add_argument("--enodot-url", default="https://ai.enodot.shop")
    command.add_argument(
        "--relay-origin",
        default=_RELAY_ORIGIN,
        help="reserved compatibility option; only the local relay origin is accepted",
    )
    command.add_argument(
        "--sessions-dir",
        type=Path,
        default=Path("/var/lib/a6-trader/secrets/sessions"),
    )
    command.add_argument(
        "--proxy-map-file",
        type=Path,
        default=Path("/var/lib/a6-trader/secrets/proxy-map.json"),
        help="Trader control-plane session proxies used only to reveal A6 keys",
    )
    command.add_argument(
        "--admin-key-file",
        type=Path,
        default=Path("/var/lib/a6-trader/secrets/enodot-admin-key.secret"),
    )
    return command


def main() -> None:
    args = parser().parse_args()
    client = EnodotAdminClient(args.enodot_url, _private_text(args.admin_key_file))
    records = _session_records(args.sessions_dir)
    proxies = _proxy_map(args.proxy_map_file)
    accounts = _managed_provider_accounts(
        client._list_accounts_raw(page=1, page_size=200, filters=None)["items"]
    )
    if not accounts:
        raise A6AutoError("provider_pool_accounts_empty")

    planned = []
    for account in accounts:
        api_key = _reveal_api_key(account, records=records, proxies=proxies)
        update, desired_base_url = _provider_update(
            account,
            api_key=api_key,
            relay_origin=args.relay_origin,
        )
        planned.append((account, update, desired_base_url))

    results = []
    for account, update, desired_base_url in planned:
        account_id = int(account["id"])
        if args.apply and update:
            client.update_account(account_id, update)
        readback = client.get_account(account_id) if args.apply else account
        if args.apply:
            _verify(readback, desired_base_url=desired_base_url)
        results.append(
            {
                "account_id": account_id,
                "name": account["name"],
                "action": "update" if update else "noop",
                "applied": bool(args.apply and update),
                "base_url": desired_base_url,
                "pool_mode": True,
                "pool_mode_retry_count": 0,
                "pool_mode_retry_status_codes": [],
                "proxy_id": None,
            }
        )
    print(
        json.dumps(
            {"success": True, "apply": args.apply, "count": len(results), "results": results},
            ensure_ascii=True,
            separators=(",", ":"),
        )
    )


if __name__ == "__main__":
    try:
        main()
    except A6AutoError as exc:
        print(json.dumps({"success": False, "error": str(exc)}), file=sys.stderr)
        raise SystemExit(1) from None
