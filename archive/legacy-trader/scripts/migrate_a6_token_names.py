#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row

from a6api_auto.client import A6ApiClient
from a6api_auto.models import SessionRecord
from a6api_auto.storage import SessionStore, read_private_json
from a6api_auto.token_name_migration import (
    PostgresTokenNameDatabase,
    PrivateTokenNameJournal,
    TokenNameMigrationError,
    execute_token_name_migration,
    plan_token_name_migration,
)
from a6api_auto.token_names import is_identity_linked_token_name

APPLY_CONFIRMATION = "APPLY-A6-TOKEN-RENAME"


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(
        description="Rename identity-linked A6 token labels without rotating token keys",
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
    )
    command.add_argument("--database-dsn-env", default="A6_TRADE_DATABASE_DSN")
    command.add_argument("--journal-file", type=Path)
    command.add_argument(
        "--all-token-names",
        action="store_true",
        help="Rename every session token, including already-anonymous legacy names",
    )
    command.add_argument(
        "--allow-direct-missing-proxy",
        action="store_true",
        help="Use a direct A6 control connection when a registered user has no proxy mapping",
    )
    command.add_argument("--apply", action="store_true")
    command.add_argument("--confirm", default="")
    return command


def _proxy_map(path: Path) -> dict[int, str]:
    payload = read_private_json(path)
    try:
        values = {int(user_id): str(proxy).strip() for user_id, proxy in payload.items()}
    except (TypeError, ValueError):
        raise TokenNameMigrationError("token_name_migration_proxy_map_invalid") from None
    if not values or any(user_id < 1 or not proxy for user_id, proxy in values.items()):
        raise TokenNameMigrationError("token_name_migration_proxy_map_invalid")
    return values


def _authenticated_client(
    record: SessionRecord,
    proxy_by_user_id: dict[int, str],
    *,
    allow_direct_missing_proxy: bool = False,
) -> A6ApiClient:
    user_id = int(record.user_id or 0)
    proxy = proxy_by_user_id.get(user_id)
    if user_id < 1 or (not proxy and not allow_direct_missing_proxy):
        raise TokenNameMigrationError("token_name_migration_proxy_mapping_missing")
    client = A6ApiClient(record.origin, proxy_url=proxy or "")
    client.import_cookies(record.cookies)
    current = client.self_user(user_id) if record.cookies else None
    if current is None:
        current = client.login(record.username, record.password)
        if current is None and record.username != record.email:
            current = client.login(record.email, record.password)
    if int((current or {}).get("id") or 0) != user_id:
        raise TokenNameMigrationError("token_name_migration_session_identity_mismatch")
    return client


def _safe_error(exc: Exception) -> str:
    if isinstance(exc, (TokenNameMigrationError,)):
        return str(exc)[:160]
    return "token_name_migration_preflight_failed"


def run(args: argparse.Namespace) -> dict[str, Any]:
    if args.apply and args.confirm != APPLY_CONFIRMATION:
        raise TokenNameMigrationError("token_name_migration_confirmation_required")
    dsn = os.environ.get(args.database_dsn_env, "").strip()
    if not dsn:
        raise TokenNameMigrationError("token_name_migration_database_dsn_missing")
    sessions_dir = args.sessions_dir.expanduser().resolve()
    if not sessions_dir.is_dir():
        raise TokenNameMigrationError("token_name_migration_sessions_directory_missing")
    proxy_by_user_id = _proxy_map(args.proxy_map_file)

    def connection_factory() -> Any:
        return psycopg.connect(dsn, row_factory=dict_row)

    database = PostgresTokenNameDatabase(connection_factory)
    planned: list[tuple[Any, Any]] = []
    blocked: list[dict[str, Any]] = []
    linked_count = 0
    selected_count = 0
    pending_session_count = 0
    for path in sorted(sessions_dir.glob("session-*.json"), key=str):
        record = SessionStore(path).load()
        if not record.user_id:
            pending_session_count += 1
            continue
        identity_linked = is_identity_linked_token_name(record.token_name)
        if identity_linked:
            linked_count += 1
        if not args.all_token_names and not identity_linked:
            continue
        selected_count += 1
        try:
            remote = _authenticated_client(
                record,
                proxy_by_user_id,
                allow_direct_missing_proxy=args.allow_direct_missing_proxy,
            )
            candidate = plan_token_name_migration(path, database, remote)
            planned.append((candidate, remote))
        except Exception as exc:
            blocked.append({"session_file": path.name, "error": _safe_error(exc)})

    result: dict[str, Any] = {
        "mode": "apply" if args.apply else "dry-run",
        "selection": "all" if args.all_token_names else "identity-linked",
        "selected_count": selected_count,
        "pending_session_count": pending_session_count,
        "identity_linked_count": linked_count,
        "planned_count": len(planned),
        "blocked_count": len(blocked),
        "blocked": blocked,
        "tokens": [candidate.public_summary(state="planned") for candidate, _ in planned],
    }
    if not args.apply:
        return result
    if blocked:
        raise TokenNameMigrationError("token_name_migration_preflight_blocked")
    journal_path = args.journal_file or sessions_dir.parent / "token-name-migration.journal.jsonl"
    journal = PrivateTokenNameJournal(journal_path)
    completed: list[dict[str, Any]] = []
    for candidate, remote in planned:
        completed.append(
            execute_token_name_migration(
                candidate,
                database,
                remote,
                apply=True,
                journal=journal,
            )
        )
    result["tokens"] = completed
    result["completed_count"] = len(completed)
    result["journal_file"] = str(journal_path.expanduser().resolve())
    return result


def main() -> None:
    print(json.dumps(run(parser().parse_args()), ensure_ascii=True, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
