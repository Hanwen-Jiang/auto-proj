#!/usr/bin/env python3
"""Provision a dedicated, persistent A6 transfer-asset key pool."""

from __future__ import annotations

import argparse
import concurrent.futures
import fcntl
import hashlib
import json
import os
import sys
import time
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from a6api_auto.client import A6ApiClient
from a6api_auto.storage import SessionStore
from a6api_auto.token_names import token_name_corpus

SCHEMA_VERSION = 1
PURPOSE = "a6_transfer_asset_key_pool"
DUTY = "transfer_asset"
UNCALLABLE_ROUTE_STATUSES = {
    "down",
    "retired",
    "supplier_disabled",
    "unavailable",
    "user_disabled",
}


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(
        description="Provision one dedicated transfer-asset Token/Key per A6 session user."
    )
    command.add_argument("--session-dir", type=Path, required=True)
    command.add_argument("--proxy-map-file", type=Path, required=True)
    command.add_argument("--output", type=Path, required=True)
    command.add_argument("--model", default="gpt-5.6-sol")
    command.add_argument("--channel-id", type=int)
    command.add_argument("--user-id", type=int, action="append", default=[])
    command.add_argument("--workers", type=int, default=8)
    command.add_argument("--attempts", type=int, default=3)
    command.add_argument("--retry-delay", type=float, default=2.0)
    command.add_argument("--timeout", type=float, default=20.0)
    return command


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def compact_error(error: BaseException, limit: int = 240) -> str:
    value = " ".join(str(error).split()) or type(error).__name__
    return value if len(value) <= limit else value[: limit - 3] + "..."


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def ensure_private_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)


def write_private_json(path: Path, document: Mapping[str, Any]) -> None:
    ensure_private_directory(path.parent)
    payload = (
        json.dumps(document, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        remaining = memoryview(payload)
        while remaining:
            written = os.write(descriptor, remaining)
            if written < 1:
                raise OSError("transfer_asset_manifest_write_stalled")
            remaining = remaining[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def load_proxy_map(path: Path) -> dict[int, str]:
    document = load_json(path)
    if not isinstance(document, dict):
        raise RuntimeError("transfer_asset_proxy_map_invalid")
    result: dict[int, str] = {}
    for raw_user_id, raw_proxy in document.items():
        try:
            user_id = int(raw_user_id)
        except (TypeError, ValueError):
            continue
        proxy = str(raw_proxy or "").strip()
        if user_id > 0 and proxy:
            result[user_id] = proxy
    return result


def session_specs(
    directory: Path,
    proxies: Mapping[int, str],
    selected_user_ids: set[int],
) -> dict[int, dict[str, Any]]:
    result: dict[int, dict[str, Any]] = {}
    for path in sorted(directory.glob("session-*.json")):
        try:
            record = SessionStore(path).load()
            user_id = int(record.user_id or 0)
        except Exception:
            continue
        if user_id < 1 or (selected_user_ids and user_id not in selected_user_ids):
            continue
        if user_id in result:
            raise RuntimeError(f"transfer_asset_duplicate_session_user:{user_id}")
        proxy = str(proxies.get(user_id) or "").strip()
        result[user_id] = {
            "user_id": user_id,
            "session_file": str(path.resolve()),
            "proxy": proxy,
        }
    if selected_user_ids - set(result):
        missing = ",".join(str(value) for value in sorted(selected_user_ids - set(result)))
        raise RuntimeError(f"transfer_asset_session_missing:{missing}")
    if not result:
        raise RuntimeError("transfer_asset_sessions_empty")
    return result


def new_manifest(*, session_dir: Path, model: str) -> dict[str, Any]:
    now = utc_now()
    return {
        "schema_version": SCHEMA_VERSION,
        "purpose": PURPOSE,
        "duty": DUTY,
        "model": model,
        "session_dir": str(session_dir.resolve()),
        "created_at": now,
        "updated_at": now,
        "assets": {},
        "accounts": [],
    }


def load_manifest(path: Path, *, session_dir: Path, model: str) -> dict[str, Any]:
    if not path.exists():
        return new_manifest(session_dir=session_dir, model=model)
    document = load_json(path)
    if (
        not isinstance(document, dict)
        or document.get("schema_version") != SCHEMA_VERSION
        or document.get("purpose") != PURPOSE
        or document.get("duty") != DUTY
        or document.get("model") != model
        or not isinstance(document.get("assets"), dict)
    ):
        raise RuntimeError("transfer_asset_manifest_invalid")
    return document


def render_accounts(document: dict[str, Any]) -> None:
    accounts: list[dict[str, Any]] = []
    for raw_user_id, raw_asset in sorted(
        document["assets"].items(), key=lambda item: int(item[0])
    ):
        if not isinstance(raw_asset, dict) or raw_asset.get("state") != "ready":
            continue
        api_key = str(raw_asset.get("api_key") or "").strip()
        token_id = int(raw_asset.get("token_id") or 0)
        if not api_key.startswith("sk-") or token_id < 1:
            continue
        user_id = int(raw_user_id)
        accounts.append(
            {
                "name": f"user-{user_id}",
                "new_api_user": user_id,
                "token_id": token_id,
                "token_name": raw_asset["token_name"],
                "session_file": raw_asset["session_file"],
                "api_key": api_key,
                "proxy": raw_asset["proxy"],
                "duty": DUTY,
            }
        )
    document["accounts"] = accounts
    document["updated_at"] = utc_now()


def select_token_name(
    user_id: int,
    *,
    reserved_names: set[str],
    remote_names: set[str],
) -> str:
    corpus = token_name_corpus()
    digest = hashlib.sha256(f"{PURPOSE}:{user_id}".encode()).digest()
    start = int.from_bytes(digest[:8], "big") % len(corpus)
    for offset in range(len(corpus)):
        candidate = corpus[(start + offset) % len(corpus)]
        if candidate not in reserved_names and candidate not in remote_names:
            return candidate
    raise RuntimeError(f"transfer_asset_token_name_exhausted:{user_id}")


def client_for(spec: Mapping[str, Any], timeout: float) -> tuple[int, A6ApiClient]:
    record = SessionStore(Path(str(spec["session_file"]))).load()
    user_id = int(record.user_id or 0)
    if user_id != int(spec["user_id"]):
        raise RuntimeError("transfer_asset_session_identity_changed")
    client = A6ApiClient(record.origin, proxy_url=str(spec["proxy"]), timeout=timeout)
    client.import_cookies(record.cookies)
    return user_id, client


def retry(
    operation: Callable[[], Any],
    *,
    attempts: int,
    retry_delay: float,
) -> Any:
    last_error: BaseException | None = None
    for attempt in range(1, attempts + 1):
        try:
            return operation()
        except Exception as error:  # noqa: BLE001 - retry is account-scoped.
            last_error = error
            if attempt < attempts:
                time.sleep(retry_delay * attempt)
    assert last_error is not None
    raise last_error


def discover_remote_names(
    spec: Mapping[str, Any],
    *,
    timeout: float,
    attempts: int,
    retry_delay: float,
) -> set[str]:
    user_id, client = client_for(spec, timeout)
    tokens = retry(
        lambda: client.list_tokens(user_id),
        attempts=attempts,
        retry_delay=retry_delay,
    )
    return {str(item.get("name") or "").strip() for item in tokens}


def channel_readback(
    client: A6ApiClient,
    user_id: int,
    *,
    token_id: int,
    model: str,
    channel_id: int,
) -> dict[str, Any]:
    page = 1
    seen = 0
    total: int | None = None
    while page <= 100:
        data = client.marketplace_channels(
            user_id,
            token_id=token_id,
            model=model,
            page=page,
            page_size=100,
            route_status="all",
            exclude_unavailable=False,
            include_seed=True,
            include_disabled=True,
            include_user_disabled=True,
        )
        items = data.get("items")
        if not isinstance(items, list):
            raise RuntimeError("transfer_asset_channel_items_invalid")
        for item in items:
            if isinstance(item, Mapping) and int(item.get("channel_id") or 0) == channel_id:
                route_status = str(item.get("route_status") or "").strip().casefold()
                if (
                    item.get("listing_availability") != 1
                    or item.get("supplier_channel_disabled") is True
                    or item.get("user_channel_disabled") is True
                    or route_status in UNCALLABLE_ROUTE_STATUSES
                ):
                    raise RuntimeError("transfer_asset_channel_not_callable")
                return dict(item)
        if total is None:
            total = int(data.get("total") or len(items))
        seen += len(items)
        if seen >= total or not items:
            break
        page += 1
    raise RuntimeError("transfer_asset_channel_missing")


def provision_asset(
    spec: Mapping[str, Any],
    asset: Mapping[str, Any],
    *,
    model: str,
    channel_id: int | None,
    timeout: float,
    attempts: int,
    retry_delay: float,
) -> dict[str, Any]:
    def operation() -> dict[str, Any]:
        user_id, client = client_for(spec, timeout)
        token_name = str(asset["token_name"])
        key = client.ensure_one_key(user_id, token_name)
        tokens = [
            item
            for item in client.list_tokens(user_id)
            if str(item.get("name") or "") == token_name
        ]
        if not tokens:
            raise RuntimeError("transfer_asset_token_readback_missing")
        target = max(tokens, key=lambda item: int(item.get("id") or 0))
        token_id = int(target.get("id") or 0)
        observed = client.get_token(user_id, token_id)
        if (
            token_id < 1
            or int(observed.get("id") or 0) != token_id
            or str(observed.get("name") or "") != token_name
            or int(observed.get("status") or 0) != 1
            or not key.startswith("sk-")
        ):
            raise RuntimeError("transfer_asset_token_readback_mismatch")
        pin: dict[str, Any] | None = None
        channel: dict[str, Any] | None = None
        if channel_id is not None:
            channel = channel_readback(
                client,
                user_id,
                token_id=token_id,
                model=model,
                channel_id=channel_id,
            )
            client.marketplace_pin(
                user_id,
                token_id=token_id,
                channel_id=channel_id,
                model=model,
                fallback_to_smart_routing=False,
            )
            matches = [
                item
                for item in client.marketplace_pins(user_id)
                if int(item.get("token_id") or 0) == token_id
                and str(item.get("model_name") or "") == model
            ]
            if len(matches) != 1:
                raise RuntimeError("transfer_asset_pin_readback_missing")
            pin = matches[0]
            if (
                int(pin.get("channel_id") or 0) != channel_id
                or str(pin.get("status") or "") != "active"
                or pin.get("fallback_to_smart_routing") is not False
            ):
                raise RuntimeError("transfer_asset_pin_readback_mismatch")
        now = utc_now()
        return {
            **dict(asset),
            "state": "ready",
            "token_id": token_id,
            "api_key": key,
            "key_fingerprint": hashlib.sha256(key.encode()).hexdigest(),
            "remote_status": int(observed.get("status") or 0),
            "pin": (
                None
                if pin is None
                else {
                    "channel_id": int(pin["channel_id"]),
                    "model_name": str(pin["model_name"]),
                    "status": str(pin["status"]),
                    "fallback_to_smart_routing": bool(
                        pin.get("fallback_to_smart_routing")
                    ),
                    "listing_id": int((channel or {}).get("listing_id") or 0),
                }
            ),
            "last_error": None,
            "ready_at": str(asset.get("ready_at") or now),
            "updated_at": now,
        }

    return retry(operation, attempts=attempts, retry_delay=retry_delay)


def validate_args(args: argparse.Namespace) -> None:
    if (
        not args.session_dir.is_dir()
        or not args.proxy_map_file.is_file()
        or not args.model.strip()
        or (args.channel_id is not None and args.channel_id < 1)
        or not 1 <= args.workers <= 32
        or not 1 <= args.attempts <= 10
        or args.retry_delay < 0
        or args.timeout <= 0
        or any(user_id < 1 for user_id in args.user_id)
    ):
        raise RuntimeError("transfer_asset_arguments_invalid")


def main() -> int:
    args = parser().parse_args()
    validate_args(args)
    output = args.output.expanduser().resolve()
    ensure_private_directory(output.parent)
    lock_path = output.with_name(output.name + ".lock")
    lock_descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(lock_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as error:
        os.close(lock_descriptor)
        raise RuntimeError("transfer_asset_manifest_locked") from error

    try:
        proxies = load_proxy_map(args.proxy_map_file)
        specs = session_specs(args.session_dir, proxies, set(args.user_id))
        document = load_manifest(output, session_dir=args.session_dir, model=args.model)
        assets: dict[str, Any] = document["assets"]
        reserved_names = {
            str(asset.get("token_name") or "")
            for asset in assets.values()
            if isinstance(asset, dict) and asset.get("token_name")
        }

        unplanned = {
            user_id: spec for user_id, spec in specs.items() if str(user_id) not in assets
        }
        remote_names: dict[int, set[str]] = {}
        planning_failures: dict[int, str] = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as executor:
            futures = {
                executor.submit(
                    discover_remote_names,
                    spec,
                    timeout=args.timeout,
                    attempts=args.attempts,
                    retry_delay=args.retry_delay,
                ): user_id
                for user_id, spec in unplanned.items()
            }
            for future in concurrent.futures.as_completed(futures):
                user_id = futures[future]
                try:
                    remote_names[user_id] = future.result()
                except Exception as error:  # noqa: BLE001 - continue other accounts.
                    planning_failures[user_id] = compact_error(error)

        for user_id in sorted(unplanned):
            if user_id in planning_failures:
                continue
            spec = specs[user_id]
            token_name = select_token_name(
                user_id,
                reserved_names=reserved_names,
                remote_names=remote_names[user_id],
            )
            reserved_names.add(token_name)
            now = utc_now()
            assets[str(user_id)] = {
                "user_id": user_id,
                "state": "planned",
                "duty": DUTY,
                "model": args.model,
                "token_name": token_name,
                "session_file": spec["session_file"],
                "proxy": spec["proxy"],
                "planned_at": now,
                "updated_at": now,
            }
        render_accounts(document)
        write_private_json(output, document)

        failures: dict[int, str] = dict(planning_failures)
        completed = 0
        targets = {
            user_id: (specs[user_id], dict(assets[str(user_id)]))
            for user_id in specs
            if str(user_id) in assets
        }
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as executor:
            futures = {
                executor.submit(
                    provision_asset,
                    spec,
                    asset,
                    model=args.model,
                    channel_id=args.channel_id,
                    timeout=args.timeout,
                    attempts=args.attempts,
                    retry_delay=args.retry_delay,
                ): user_id
                for user_id, (spec, asset) in targets.items()
            }
            for future in concurrent.futures.as_completed(futures):
                user_id = futures[future]
                try:
                    assets[str(user_id)] = future.result()
                    completed += 1
                    print(
                        f"asset_ready user_id={user_id} "
                        f"token_id={assets[str(user_id)]['token_id']} "
                        f"token_name={assets[str(user_id)]['token_name']!r}",
                        flush=True,
                    )
                except Exception as error:  # noqa: BLE001 - persist partial progress.
                    message = compact_error(error)
                    failures[user_id] = message
                    current = dict(assets[str(user_id)])
                    current["last_error"] = message
                    current["updated_at"] = utc_now()
                    assets[str(user_id)] = current
                    print(
                        f"asset_failed user_id={user_id} detail={message}",
                        file=sys.stderr,
                        flush=True,
                    )
                render_accounts(document)
                write_private_json(output, document)

        selected_ready = sum(
            1
            for user_id in specs
            if isinstance(assets.get(str(user_id)), dict)
            and assets[str(user_id)].get("state") == "ready"
        )
        summary = {
            "purpose": PURPOSE,
            "selected_users": len(specs),
            "ready": selected_ready,
            "completed_this_run": completed,
            "failed": len(failures),
            "failure_users": sorted(failures),
            "output": str(output),
        }
        print(json.dumps(summary, ensure_ascii=True, sort_keys=True))
        return 0 if selected_ready == len(specs) and not failures else 2
    finally:
        fcntl.flock(lock_descriptor, fcntl.LOCK_UN)
        os.close(lock_descriptor)


if __name__ == "__main__":
    raise SystemExit(main())
