from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from .errors import A6AutoError
from .storage import private_file_lock, read_private_json, write_private_json
from .token_names import generate_anonymous_token_name, is_anonymous_token_name

UTC = timezone.utc


class TokenNameMigrationError(A6AutoError):
    pass


@dataclass(frozen=True)
class DatabaseTokenIdentity:
    merchant_id: int
    token_ref_id: int
    external_token_id: int
    token_name: str


@dataclass(frozen=True)
class TokenNameMigrationCandidate:
    session_path: Path
    user_id: int
    merchant_id: int
    token_ref_id: int
    external_token_id: int
    old_name: str
    new_name: str

    def public_summary(self, *, state: str) -> dict[str, Any]:
        return {
            "state": state,
            "merchant_id": self.merchant_id,
            "a6_user_id": self.user_id,
            "a6_token_id": self.external_token_id,
            "old_name": self.old_name,
            "new_name": self.new_name,
            "session_file": self.session_path.name,
        }


class TokenNameDatabase(Protocol):
    def resolve(
        self,
        session_ref: str,
        user_id: int,
        token_name: str,
    ) -> DatabaseTokenIdentity | None: ...

    def current_name(self, candidate: TokenNameMigrationCandidate) -> str | None: ...

    def compare_and_swap_name(
        self,
        candidate: TokenNameMigrationCandidate,
        expected_name: str,
        new_name: str,
    ) -> bool: ...


class TokenNameRemote(Protocol):
    def get_token(self, user_id: int, token_id: int) -> dict[str, Any]: ...

    def list_tokens(self, user_id: int) -> list[dict[str, Any]]: ...

    def rename_token(self, user_id: int, token_id: int, new_name: str) -> dict[str, Any]: ...


class PostgresTokenNameDatabase:
    def __init__(self, connection_factory: Callable[[], Any]) -> None:
        self.connection_factory = connection_factory

    def resolve(
        self,
        session_ref: str,
        user_id: int,
        token_name: str,
    ) -> DatabaseTokenIdentity | None:
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT merchant.id AS merchant_id, token.id AS token_ref_id,
                           token.a6_token_id AS external_token_id,
                           token.token_name
                    FROM a6_trade.a6_merchants merchant
                    JOIN a6_trade.a6_tokens token ON token.merchant_id = merchant.id
                    WHERE merchant.session_ref = %s
                      AND merchant.a6_user_id = %s
                      AND token.token_name = %s
                    """,
                    (session_ref, user_id, token_name),
                )
                row = cursor.fetchone()
        finally:
            connection.close()
        if row is None:
            return None
        return DatabaseTokenIdentity(
            merchant_id=int(row["merchant_id"]),
            token_ref_id=int(row["token_ref_id"]),
            external_token_id=int(row["external_token_id"]),
            token_name=str(row["token_name"]),
        )

    def current_name(self, candidate: TokenNameMigrationCandidate) -> str | None:
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT token_name
                    FROM a6_trade.a6_tokens
                    WHERE id = %s AND merchant_id = %s AND a6_token_id = %s
                    """,
                    (
                        candidate.token_ref_id,
                        candidate.merchant_id,
                        candidate.external_token_id,
                    ),
                )
                row = cursor.fetchone()
        finally:
            connection.close()
        return None if row is None else str(row["token_name"])

    def compare_and_swap_name(
        self,
        candidate: TokenNameMigrationCandidate,
        expected_name: str,
        new_name: str,
    ) -> bool:
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE a6_trade.a6_tokens
                    SET token_name = %s, updated_at = now()
                    WHERE id = %s AND merchant_id = %s AND a6_token_id = %s
                      AND token_name = %s
                    RETURNING id
                    """,
                    (
                        new_name,
                        candidate.token_ref_id,
                        candidate.merchant_id,
                        candidate.external_token_id,
                        expected_name,
                    ),
                )
                changed = cursor.fetchone() is not None
            if changed:
                connection.commit()
            else:
                connection.rollback()
            return changed
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()


class PrivateTokenNameJournal:
    def __init__(self, path: Path) -> None:
        self.path = path.expanduser().resolve()

    def append(self, event: str, candidate: TokenNameMigrationCandidate) -> None:
        payload = candidate.public_summary(state=event)
        payload["recorded_at"] = _utc_now()
        encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True).encode() + b"\n"
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.path.parent, 0o700)
        descriptor = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.fchmod(descriptor, 0o600)
            os.write(descriptor, encoded)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def plan_token_name_migration(
    session_path: Path,
    database: TokenNameDatabase,
    remote: TokenNameRemote,
    *,
    name_factory: Callable[[], str] = generate_anonymous_token_name,
) -> TokenNameMigrationCandidate:
    path = session_path.expanduser().resolve()
    session = read_private_json(path)
    old_name = str(session.get("token_name") or "")
    user_id = _positive_integer(session.get("user_id"))
    if user_id is None:
        raise TokenNameMigrationError("token_name_migration_session_identity_missing")
    identity = database.resolve(str(path), user_id, old_name)
    if identity is None:
        raise TokenNameMigrationError("token_name_migration_database_identity_missing")
    live = remote.get_token(user_id, identity.external_token_id)
    if (
        _positive_integer(live.get("id")) != identity.external_token_id
        or str(live.get("name") or "") != old_name
    ):
        raise TokenNameMigrationError("token_name_migration_live_identity_mismatch")
    existing_names = {str(item.get("name") or "") for item in remote.list_tokens(user_id)}
    for _attempt in range(32):
        new_name = name_factory()
        if is_anonymous_token_name(new_name) and new_name not in existing_names:
            break
    else:
        raise TokenNameMigrationError("token_name_migration_name_allocation_failed")
    return TokenNameMigrationCandidate(
        session_path=path,
        user_id=user_id,
        merchant_id=identity.merchant_id,
        token_ref_id=identity.token_ref_id,
        external_token_id=identity.external_token_id,
        old_name=old_name,
        new_name=new_name,
    )


def execute_token_name_migration(
    candidate: TokenNameMigrationCandidate,
    database: TokenNameDatabase,
    remote: TokenNameRemote,
    *,
    apply: bool = False,
    journal: PrivateTokenNameJournal | None = None,
) -> dict[str, Any]:
    if not apply:
        return candidate.public_summary(state="planned")
    if journal is None:
        raise TokenNameMigrationError("token_name_migration_journal_required")
    journal.append("started", candidate)
    try:
        remote.rename_token(candidate.user_id, candidate.external_token_id, candidate.new_name)
        _verify_remote_name(remote, candidate, candidate.new_name)
        journal.append("live_renamed", candidate)

        _compare_and_swap_session(candidate, candidate.old_name, candidate.new_name)
        journal.append("session_saved", candidate)

        if not database.compare_and_swap_name(
            candidate,
            candidate.old_name,
            candidate.new_name,
        ):
            raise TokenNameMigrationError("token_name_migration_database_cas_conflict")
        journal.append("completed", candidate)
        return candidate.public_summary(state="completed")
    except Exception as exc:
        rollback_errors = _rollback(candidate, database, remote)
        if rollback_errors:
            raise TokenNameMigrationError("token_name_migration_rollback_failed") from exc
        try:
            journal.append("rolled_back", candidate)
        except Exception:
            raise TokenNameMigrationError("token_name_migration_journal_failed") from exc
        if isinstance(exc, A6AutoError):
            raise
        raise TokenNameMigrationError("token_name_migration_failed") from exc


def _rollback(
    candidate: TokenNameMigrationCandidate,
    database: TokenNameDatabase,
    remote: TokenNameRemote,
) -> list[str]:
    errors: list[str] = []
    try:
        database_name = database.current_name(candidate)
        rollback_failed = (
            database_name == candidate.new_name
            and not database.compare_and_swap_name(
                candidate,
                candidate.new_name,
                candidate.old_name,
            )
        )
        if rollback_failed or database_name not in {candidate.old_name, candidate.new_name}:
            errors.append("database")
    except Exception:
        errors.append("database")
    try:
        _restore_session(candidate)
    except Exception:
        errors.append("session")
    try:
        observed = remote.get_token(candidate.user_id, candidate.external_token_id)
        current_name = str(observed.get("name") or "")
        if current_name == candidate.new_name:
            remote.rename_token(candidate.user_id, candidate.external_token_id, candidate.old_name)
        elif current_name != candidate.old_name:
            raise TokenNameMigrationError("token_name_migration_live_rollback_conflict")
        _verify_remote_name(remote, candidate, candidate.old_name)
    except Exception:
        errors.append("live")
    return errors


def _compare_and_swap_session(
    candidate: TokenNameMigrationCandidate,
    expected_name: str,
    new_name: str,
) -> None:
    lock_path = candidate.session_path.with_name(f".{candidate.session_path.name}.token-name.lock")
    with private_file_lock(lock_path):
        session = read_private_json(candidate.session_path)
        if (
            _positive_integer(session.get("user_id")) != candidate.user_id
            or str(session.get("token_name") or "") != expected_name
        ):
            raise TokenNameMigrationError("token_name_migration_session_cas_conflict")
        session["token_name"] = new_name
        session["updated_at"] = _utc_now()
        write_private_json(candidate.session_path, session)


def _restore_session(candidate: TokenNameMigrationCandidate) -> None:
    current = read_private_json(candidate.session_path)
    current_name = str(current.get("token_name") or "")
    if current_name == candidate.old_name:
        return
    if current_name != candidate.new_name:
        raise TokenNameMigrationError("token_name_migration_session_rollback_conflict")
    _compare_and_swap_session(candidate, candidate.new_name, candidate.old_name)


def _verify_remote_name(
    remote: TokenNameRemote,
    candidate: TokenNameMigrationCandidate,
    expected_name: str,
) -> None:
    observed = remote.get_token(candidate.user_id, candidate.external_token_id)
    if (
        _positive_integer(observed.get("id")) != candidate.external_token_id
        or str(observed.get("name") or "") != expected_name
    ):
        raise TokenNameMigrationError("token_name_migration_live_readback_mismatch")


def _positive_integer(value: object) -> int | None:
    if value in (None, "") or isinstance(value, bool):
        return None
    try:
        result = int(value)
    except (TypeError, ValueError):
        return None
    return result if result > 0 else None


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")
