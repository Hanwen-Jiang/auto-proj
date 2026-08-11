from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Protocol

from .merchant_policy import DEFAULT_OPERATING_GROUP, normalize_operating_group
from .models import DEFAULT_QUOTA_PER_UNIT
from .storage import read_private_json

MIGRATION_NAME = re.compile(r"^(?P<version>\d{4})_[a-z0-9_]+\.sql$")
KNOWN_ROLES = frozenset({"unassigned", "seller", "provider", "observer"})
DUTY_ROLES = frozenset({"provider", "observer"})
ALLOWED_PROXY_METADATA = frozenset(
    {
        "country",
        "egress_ip_fingerprint",
        "lease_id",
        "pool",
        "provider",
        "proxy_fingerprint",
        "proxy_id",
        "region",
    }
)
FORBIDDEN_SESSION_FIELDS = frozenset(
    {
        "a6api_key",
        "api_key",
        "cookie",
        "cookies",
        "email",
        "key",
        "password",
        "secret",
        "token",
        "upstream_api_key",
    }
)


def _safe_proxy_metadata(value: Mapping[str, Any] | None) -> dict[str, str]:
    if not value:
        return {}
    unknown = set(value) - ALLOWED_PROXY_METADATA
    if unknown:
        raise ValueError("proxy_metadata_contains_unapproved_fields")
    result = {key: str(item) for key, item in value.items() if item is not None and str(item)}
    lowered = " ".join(result.values()).lower()
    if "://" in lowered or "password" in lowered or "@" in lowered:
        raise ValueError("proxy_metadata_contains_secret_material")
    return result


class Cursor(Protocol):
    def execute(self, query: str, params: Any = None) -> Any: ...

    def fetchone(self) -> Any: ...


class Connection(Protocol):
    def cursor(self) -> Any: ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...


class MigrationError(RuntimeError):
    pass


@dataclass(frozen=True)
class MerchantMetadata:
    session_ref: str
    session_fingerprint: str
    user_id: int | None
    supplier_id: int | None
    status: str
    quota_per_unit: int = DEFAULT_QUOTA_PER_UNIT
    operating_group: str = DEFAULT_OPERATING_GROUP
    roles: tuple[str, ...] = ("seller",)
    proxy_lease_id: str | None = None
    proxy_fingerprint: str | None = None
    proxy_metadata: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        requested_roles = tuple(str(role) for role in self.roles)
        if not requested_roles or len(set(requested_roles)) != len(requested_roles):
            raise ValueError("merchant_roles_invalid")
        requested_duties = set(requested_roles) & DUTY_ROLES
        if requested_roles in {("seller",), ("unassigned",)}:
            normalized_roles = ("seller",)
        elif requested_roles in {("provider",), ("provider", "observer")}:
            normalized_roles = requested_roles
        elif requested_roles == ("observer",):
            normalized_roles = ("seller", requested_roles[0])
        elif (
            len(requested_roles) == 2
            and requested_roles[0] == "seller"
            and requested_roles[1] in DUTY_ROLES
        ):
            normalized_roles = requested_roles
        else:
            raise ValueError("merchant_roles_invalid")
        if not self.session_ref:
            raise ValueError("session_ref_required")
        if not re.fullmatch(r"[0-9a-f]{64}", self.session_fingerprint):
            raise ValueError("session_fingerprint_invalid")
        if not self.status:
            raise ValueError("merchant_status_required")
        operating_group = normalize_operating_group(self.operating_group)
        if (
            isinstance(self.quota_per_unit, bool)
            or not isinstance(self.quota_per_unit, int)
            or self.quota_per_unit < 1
        ):
            raise ValueError("quota_per_unit_invalid")
        if len(requested_duties) > 1 and requested_roles != ("provider", "observer"):
            raise ValueError("merchant_roles_invalid")
        safe_proxy = _safe_proxy_metadata(self.proxy_metadata)
        object.__setattr__(self, "roles", normalized_roles)
        object.__setattr__(self, "operating_group", operating_group)
        object.__setattr__(self, "proxy_metadata", MappingProxyType(safe_proxy))


def connect_postgres(dsn: str, **kwargs: Any) -> Connection:
    """Create a psycopg3 connection without making psycopg an import-time dependency."""
    import psycopg

    return psycopg.connect(dsn, **kwargs)


def default_migration_directory() -> Path:
    source_directory = Path(__file__).resolve().parents[2] / "migrations"
    if source_directory.is_dir():
        return source_directory
    return Path(__file__).with_name("data") / "migrations"


def _migration_files(directory: Path) -> list[tuple[str, Path]]:
    migrations: list[tuple[str, Path]] = []
    for path in directory.glob("*.sql"):
        match = MIGRATION_NAME.fullmatch(path.name)
        if match:
            migrations.append((match.group("version"), path))
    migrations.sort(key=lambda item: item[0])
    if len({version for version, _ in migrations}) != len(migrations):
        raise MigrationError("duplicate_migration_version")
    return migrations


def apply_migrations(
    connection: Connection,
    migration_directory: Path | None = None,
) -> tuple[str, ...]:
    """Apply versioned SQL once and reject edits to an already-applied migration."""
    directory = (migration_directory or default_migration_directory()).resolve()
    if not directory.is_dir():
        raise MigrationError("migration_directory_missing")

    applied: list[str] = []
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT 1 FROM pg_namespace WHERE nspname = %s",
                ("a6_trade",),
            )
            if cursor.fetchone() is None:
                cursor.execute("CREATE SCHEMA a6_trade")
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS a6_trade.schema_migrations (
                    version text PRIMARY KEY,
                    filename text NOT NULL,
                    checksum char(64) NOT NULL,
                    applied_at timestamptz NOT NULL DEFAULT now()
                )
                """
            )
            for version, path in _migration_files(directory):
                sql = path.read_text(encoding="utf-8")
                checksum = hashlib.sha256(sql.encode()).hexdigest()
                cursor.execute(
                    "SELECT checksum FROM a6_trade.schema_migrations WHERE version = %s",
                    (version,),
                )
                existing = cursor.fetchone()
                if existing is not None:
                    existing_checksum = (
                        existing[0] if not isinstance(existing, Mapping) else existing["checksum"]
                    )
                    if str(existing_checksum) != checksum:
                        raise MigrationError(f"migration_checksum_mismatch:{version}")
                    continue
                cursor.execute(sql)
                cursor.execute(
                    """
                    INSERT INTO a6_trade.schema_migrations (version, filename, checksum)
                    VALUES (%s, %s, %s)
                    """,
                    (version, path.name, checksum),
                )
                applied.append(version)
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    return tuple(applied)


def _integer_or_none(value: Any) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ValueError("metadata_integer_invalid")
    return int(value)


def _session_fingerprint(session_ref: str, session: Mapping[str, Any]) -> str:
    raw_cookies = session.get("cookies")
    session_cookie = raw_cookies.get("session") if isinstance(raw_cookies, Mapping) else None
    identity = {
        "origin": session.get("origin"),
        "session_ref": session_ref,
        "session_cookie_hash": (
            hashlib.sha256(str(session_cookie).encode()).hexdigest() if session_cookie else None
        ),
        "supplier_id": session.get("supplier_id"),
        "user_id": session.get("user_id"),
    }
    encoded = json.dumps(identity, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def metadata_from_session(
    path: Path,
    *,
    session_ref: str | None = None,
    roles: Sequence[str] = ("seller",),
    proxy_metadata: Mapping[str, Any] | None = None,
) -> MerchantMetadata:
    """Read a private session and return only values approved for PostgreSQL."""
    resolved = path.expanduser().resolve()
    session = read_private_json(resolved)
    reference = session_ref or str(resolved)
    safe_proxy = _safe_proxy_metadata(proxy_metadata)
    proxy_lease_id = safe_proxy.get("lease_id")
    proxy_fingerprint = safe_proxy.get("proxy_fingerprint")
    normalized_roles = tuple(dict.fromkeys(str(role) for role in roles))
    quota_per_unit = session.get("quota_per_unit")
    if quota_per_unit in (None, ""):
        quota_per_unit = DEFAULT_QUOTA_PER_UNIT
    return MerchantMetadata(
        session_ref=reference,
        session_fingerprint=_session_fingerprint(reference, session),
        user_id=_integer_or_none(session.get("user_id")),
        supplier_id=_integer_or_none(session.get("supplier_id")),
        status=str(session.get("status") or "unknown"),
        quota_per_unit=quota_per_unit,
        operating_group=session.get("operating_group", DEFAULT_OPERATING_GROUP),
        roles=normalized_roles,
        proxy_lease_id=proxy_lease_id,
        proxy_fingerprint=proxy_fingerprint,
        proxy_metadata=safe_proxy,
    )


def load_session_metadata(
    paths: Iterable[Path],
    *,
    roles_by_ref: Mapping[str, Sequence[str]] | None = None,
    proxy_metadata_by_ref: Mapping[str, Mapping[str, Any]] | None = None,
) -> tuple[MerchantMetadata, ...]:
    """Build a deterministic, duplicate-free metadata set from session files.

    Terminal session records are intentionally included. Importing an
    ``abandoned`` record is the tombstone that retires a previously imported
    merchant without deleting its database row or referenced audit history.
    """
    roles_by_ref = roles_by_ref or {}
    proxy_metadata_by_ref = proxy_metadata_by_ref or {}
    rows: list[MerchantMetadata] = []
    seen: set[str] = set()
    for path in sorted((item.expanduser().resolve() for item in paths), key=str):
        reference = str(path)
        if reference in seen:
            continue
        seen.add(reference)
        metadata = metadata_from_session(
            path,
            session_ref=reference,
            roles=roles_by_ref.get(reference, ("seller",)),
            proxy_metadata=proxy_metadata_by_ref.get(reference),
        )
        rows.append(metadata)
    return tuple(rows)


UPSERT_MERCHANT_SQL = """
    INSERT INTO a6_trade.a6_merchants (
        session_ref,
        session_fingerprint,
        a6_user_id,
        supplier_id,
        quota_per_unit,
        status,
        operating_group,
        roles,
        proxy_lease_id,
        proxy_fingerprint,
        proxy_metadata
    )
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s::text[], %s, %s, %s::jsonb)
    ON CONFLICT (session_ref) DO UPDATE SET
        session_fingerprint = EXCLUDED.session_fingerprint,
        a6_user_id = CASE
            WHEN EXCLUDED.status = 'abandoned'
            THEN coalesce(a6_trade.a6_merchants.a6_user_id, EXCLUDED.a6_user_id)
            ELSE EXCLUDED.a6_user_id
        END,
        supplier_id = CASE
            WHEN EXCLUDED.status = 'abandoned'
            THEN coalesce(a6_trade.a6_merchants.supplier_id, EXCLUDED.supplier_id)
            ELSE EXCLUDED.supplier_id
        END,
        quota_per_unit = EXCLUDED.quota_per_unit,
        status = EXCLUDED.status,
        operating_group = CASE
            WHEN EXCLUDED.operating_group <> 'standard'
            THEN EXCLUDED.operating_group
            ELSE a6_trade.a6_merchants.operating_group
        END,
        roles = a6_trade.a6_merchants.roles,
        proxy_lease_id = coalesce(
            EXCLUDED.proxy_lease_id,
            a6_trade.a6_merchants.proxy_lease_id
        ),
        proxy_fingerprint = coalesce(
            EXCLUDED.proxy_fingerprint,
            a6_trade.a6_merchants.proxy_fingerprint
        ),
        proxy_metadata = CASE
            WHEN EXCLUDED.proxy_lease_id IS NULL
            THEN a6_trade.a6_merchants.proxy_metadata
            ELSE EXCLUDED.proxy_metadata
        END,
        updated_at = now()
"""


def upsert_merchant_metadata(
    connection: Connection,
    merchants: Iterable[MerchantMetadata],
) -> int:
    """Idempotently import merchant metadata using psycopg3 cursor semantics."""
    rows = tuple(merchants)
    try:
        with connection.cursor() as cursor:
            for merchant in rows:
                params = (
                    merchant.session_ref,
                    merchant.session_fingerprint,
                    merchant.user_id,
                    merchant.supplier_id,
                    merchant.quota_per_unit,
                    merchant.status,
                    merchant.operating_group,
                    list(merchant.roles),
                    merchant.proxy_lease_id,
                    merchant.proxy_fingerprint,
                    json.dumps(dict(merchant.proxy_metadata), ensure_ascii=True, sort_keys=True),
                )
                cursor.execute(UPSERT_MERCHANT_SQL, params)
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    return len(rows)


def assert_metadata_is_secret_free(metadata: MerchantMetadata) -> None:
    """Guard serialization boundaries used by future CLI and collector integrations."""
    payload = metadata.__dict__
    if set(payload) & FORBIDDEN_SESSION_FIELDS:
        raise ValueError("merchant_metadata_contains_secret_field")
