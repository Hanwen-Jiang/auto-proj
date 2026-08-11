from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

from .client import A6ApiClient
from .models import SessionRecord
from .storage import SessionStore, private_file_lock, read_private_json, write_private_json

UTC = timezone.utc
_SAFE_ERROR = re.compile(r"^[a-z][a-z0-9_]{0,95}$")


class MerchantRecoveryError(RuntimeError):
    """Stable merchant-recovery failure without session or proxy material."""


class Connection(Protocol):
    def cursor(self) -> Any: ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...


@dataclass(frozen=True)
class ProxyRepairTarget:
    merchant_id: int
    a6_user_id: int
    supplier_id: int
    session_ref: str
    poll_error_count: int
    incident_id: int
    incident_key: str
    occurrence_count: int


@dataclass(frozen=True)
class ProxyVerification:
    proxy: str
    elapsed_ms: int


def proxy_fingerprint(proxy: str) -> str:
    return hashlib.sha256(proxy.encode()).hexdigest()


def proxy_reservation_fingerprint(proxy: str) -> str:
    return proxy_fingerprint(proxy)[:12]


def proxy_lease_id(proxy: str) -> str:
    port = urlsplit(proxy).port
    if port is None:
        raise MerchantRecoveryError("merchant_recovery_proxy_url_invalid")
    return f"singbox:{port}"


def normalize_proxy_url(value: object) -> str:
    if not isinstance(value, str):
        raise MerchantRecoveryError("merchant_recovery_proxy_url_invalid")
    proxy = value.strip()
    try:
        parsed = urlsplit(proxy)
        port = parsed.port
    except ValueError as exc:
        raise MerchantRecoveryError("merchant_recovery_proxy_url_invalid") from exc
    if (
        parsed.scheme not in {"http", "https", "socks5", "socks5h"}
        or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
        or port is None
        or not 1 <= port <= 65535
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise MerchantRecoveryError("merchant_recovery_proxy_url_invalid")
    return proxy


def load_proxy_map(path: Path) -> dict[int, str]:
    try:
        payload = read_private_json(path)
        result = {
            int(key): normalize_proxy_url(value)
            for key, value in payload.items()
        }
    except (TypeError, ValueError, MerchantRecoveryError) as exc:
        raise MerchantRecoveryError("merchant_recovery_proxy_map_invalid") from exc
    if any(user_id < 1 for user_id in result) or len(set(result.values())) != len(result):
        raise MerchantRecoveryError("merchant_recovery_proxy_map_invalid")
    return result


def load_proxy_pool(paths: Sequence[Path]) -> tuple[str, ...]:
    proxies: list[str] = []
    for path in paths:
        resolved = path.expanduser().resolve()
        try:
            info = resolved.stat()
            payload = json.loads(resolved.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise MerchantRecoveryError("merchant_recovery_proxy_pool_unreadable") from exc
        if info.st_mode & 0o077 or not isinstance(payload, list):
            raise MerchantRecoveryError("merchant_recovery_proxy_pool_invalid")
        try:
            proxies.extend(normalize_proxy_url(value) for value in payload)
        except MerchantRecoveryError as exc:
            raise MerchantRecoveryError("merchant_recovery_proxy_pool_invalid") from exc
    normalized = tuple(dict.fromkeys(proxies))
    if not normalized:
        raise MerchantRecoveryError("merchant_recovery_proxy_pool_empty")
    return normalized


def load_reserved_proxy_fingerprints(paths: Sequence[Path]) -> frozenset[str]:
    reserved: set[str] = set()
    for path in paths:
        resolved = path.expanduser().resolve()
        if not resolved.exists():
            continue
        try:
            payload = read_private_json(resolved)
        except Exception as exc:
            raise MerchantRecoveryError("merchant_recovery_reservations_invalid") from exc
        values = payload.get("reservations")
        if not isinstance(values, Mapping):
            raise MerchantRecoveryError("merchant_recovery_reservations_invalid")
        for value in values:
            fingerprint = str(value or "")
            if not re.fullmatch(r"[0-9a-f]{12}", fingerprint):
                raise MerchantRecoveryError("merchant_recovery_reservations_invalid")
            reserved.add(fingerprint)
    return frozenset(reserved)


class A6ProxyVerifier:
    def __init__(
        self,
        *,
        required_successes: int = 2,
        timeout_seconds: float = 6,
        client_factory: Callable[[SessionRecord, str, float], Any] | None = None,
    ) -> None:
        if (
            isinstance(required_successes, bool)
            or not 1 <= required_successes <= 5
            or isinstance(timeout_seconds, bool)
            or not 1 <= timeout_seconds <= 30
        ):
            raise MerchantRecoveryError("merchant_recovery_verifier_config_invalid")
        self.required_successes = required_successes
        self.timeout_seconds = float(timeout_seconds)
        self.client_factory = client_factory or (
            lambda record, proxy, timeout: A6ApiClient(
                record.origin,
                proxy_url=proxy,
                timeout=timeout,
            )
        )

    def verify(self, record: SessionRecord, proxy: str) -> ProxyVerification | None:
        if record.user_id is None or record.supplier_id is None or not record.cookies:
            return None
        started = time.monotonic()
        try:
            for _attempt in range(self.required_successes):
                client = self.client_factory(record, proxy, self.timeout_seconds)
                client.import_cookies(record.cookies)
                user = client.self_user(int(record.user_id))
                if not isinstance(user, Mapping) or _positive_int(user.get("id")) != record.user_id:
                    return None
                supplier = client.supplier_me(int(record.user_id))
                if (
                    not isinstance(supplier, Mapping)
                    or _positive_int(supplier.get("id")) != record.supplier_id
                ):
                    return None
        except Exception:
            return None
        return ProxyVerification(
            proxy=proxy,
            elapsed_ms=max(0, round((time.monotonic() - started) * 1000)),
        )


class PostgresMerchantRecoveryStore:
    def __init__(self, connection: Connection) -> None:
        self.connection = connection

    def proxy_repair_targets(
        self,
        *,
        failure_threshold: int,
        stale_seconds: int,
        repair_cooldown_seconds: int,
        limit: int,
    ) -> list[ProxyRepairTarget]:
        try:
            with self.connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT m.id AS merchant_id, m.a6_user_id, m.supplier_id,
                           m.session_ref, m.poll_error_count,
                           incident.id AS incident_id, incident.incident_key,
                           incident.occurrence_count
                    FROM a6_trade.a6_merchants m
                    JOIN LATERAL (
                        SELECT candidate.id, candidate.incident_key,
                               candidate.occurrence_count, candidate.last_seen_at
                        FROM a6_trade.trade_incidents candidate
                        WHERE candidate.entity_type = 'merchant'
                          AND candidate.entity_id = m.id::text
                          AND candidate.status = 'open'
                          AND candidate.incident_class IN (
                              'a6_transport_failed',
                              'supplier_self_failed'
                          )
                          AND (
                              (
                                  candidate.incident_key =
                                      'collector:' || m.id::text || ':' ||
                                      candidate.incident_class
                                  AND (
                                      m.poll_error_count >= %s
                                      OR m.last_poll_at IS NULL
                                      OR m.last_poll_at <
                                          now() - (%s * interval '1 second')
                                  )
                              )
                              OR (
                                  candidate.incident_key =
                                      'session-refresh:' || m.id::text || ':' ||
                                      candidate.incident_class
                                  AND candidate.occurrence_count >= %s
                              )
                          )
                        ORDER BY candidate.last_seen_at DESC, candidate.id DESC
                        LIMIT 1
                    ) incident ON TRUE
                    WHERE m.status = 'supplier_submitted'
                      AND m.a6_user_id IS NOT NULL
                      AND m.supplier_id IS NOT NULL
                      AND NOT EXISTS (
                          SELECT 1
                          FROM a6_trade.trade_control_actions action
                          WHERE action.action_type = 'repair_merchant_proxy'
                            AND action.entity_type = 'a6_merchant'
                            AND action.entity_id = m.id::text
                            AND action.status = 'completed'
                            AND action.completed_at >=
                                now() - (%s * interval '1 second')
                      )
                    ORDER BY m.last_poll_at NULLS FIRST,
                             m.poll_error_count DESC,
                             incident.last_seen_at
                    LIMIT %s
                    """,
                    (
                        failure_threshold,
                        stale_seconds,
                        failure_threshold,
                        repair_cooldown_seconds,
                        limit,
                    ),
                )
                rows = _rows(cursor)
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        return [
            ProxyRepairTarget(
                merchant_id=int(row["merchant_id"]),
                a6_user_id=int(row["a6_user_id"]),
                supplier_id=int(row["supplier_id"]),
                session_ref=str(row["session_ref"]),
                poll_error_count=int(row.get("poll_error_count") or 0),
                incident_id=int(row["incident_id"]),
                incident_key=str(row["incident_key"]),
                occurrence_count=int(row.get("occurrence_count") or 0),
            )
            for row in rows
        ]

    def record_session_refresh_failure(self, session_ref: str, error_class: str) -> bool:
        normalized = _error_code(MerchantRecoveryError(error_class))
        try:
            with self.connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT id
                    FROM a6_trade.a6_merchants
                    WHERE session_ref = %s
                      AND status NOT IN ('allocated', 'abandoned')
                      AND a6_user_id IS NOT NULL
                    FOR UPDATE
                    """,
                    (session_ref,),
                )
                merchant = _row(cursor)
                if merchant is None:
                    self.connection.rollback()
                    return False
                merchant_id = int(merchant["id"])
                incident_key = f"session-refresh:{merchant_id}:{normalized}"
                cursor.execute(
                    """
                    INSERT INTO a6_trade.trade_incidents (
                        incident_key, incident_class, entity_type, entity_id,
                        severity, status, first_seen_at, last_seen_at
                    ) VALUES (%s, %s, 'merchant', %s, 'warning', 'open', now(), now())
                    ON CONFLICT (incident_key) DO UPDATE SET
                        status = 'open',
                        last_seen_at = now(),
                        occurrence_count = CASE
                            WHEN a6_trade.trade_incidents.status = 'resolved' THEN 1
                            ELSE a6_trade.trade_incidents.occurrence_count + 1
                        END,
                        resolved_at = NULL,
                        updated_at = now()
                    """,
                    (incident_key, normalized, str(merchant_id)),
                )
            self.connection.commit()
            return True
        except Exception:
            self.connection.rollback()
            raise

    def resolve_session_refresh_failures(self, session_ref: str) -> int:
        try:
            with self.connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE a6_trade.trade_incidents incident
                    SET status = 'resolved', resolved_at = now(), updated_at = now()
                    FROM a6_trade.a6_merchants merchant
                    WHERE merchant.session_ref = %s
                      AND incident.entity_type = 'merchant'
                      AND incident.entity_id = merchant.id::text
                      AND incident.incident_key LIKE
                          'session-refresh:' || merchant.id::text || ':%%'
                      AND incident.status = 'open'
                    RETURNING incident.id
                    """,
                    (session_ref,),
                )
                resolved = len(cursor.fetchall())
            self.connection.commit()
            return resolved
        except Exception:
            self.connection.rollback()
            raise

    def never_activated_archive_candidates(
        self,
        *,
        minimum_age_seconds: int,
        minimum_errors: int,
        limit: int,
    ) -> list[int]:
        try:
            with self.connection.cursor() as cursor:
                cursor.execute(
                    _NEVER_ACTIVATED_ARCHIVE_QUERY + " ORDER BY p.requested_at, m.id LIMIT %s",
                    (minimum_age_seconds, minimum_errors, limit),
                )
                rows = _rows(cursor)
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        return [int(row["merchant_id"]) for row in rows]

    def complete_never_activated_archive(
        self,
        merchant_id: int,
        *,
        minimum_age_seconds: int,
        minimum_errors: int,
    ) -> bool:
        try:
            with self.connection.cursor() as cursor:
                cursor.execute(
                    _NEVER_ACTIVATED_ARCHIVE_QUERY
                    + " AND m.id = %s FOR UPDATE OF m, p",
                    (minimum_age_seconds, minimum_errors, merchant_id),
                )
                row = _row(cursor)
                if row is None:
                    self.connection.rollback()
                    return False
                roles_before = [str(role) for role in row["roles"]]
                cursor.execute(
                    """
                    UPDATE a6_trade.a6_merchants
                    SET roles = ARRAY['observer']::text[],
                        role_revision = role_revision + 1,
                        poll_error_count = 0,
                        updated_at = now()
                    WHERE id = %s
                    """,
                    (merchant_id,),
                )
                cursor.execute(
                    """
                    UPDATE a6_trade.a6_seller_retirement_plans
                    SET state = 'completed', completed_at = now(),
                        last_error_class = NULL, retry_after = NULL,
                        updated_at = now()
                    WHERE merchant_id = %s AND state = 'earning' AND archive_mode
                    """,
                    (merchant_id,),
                )
                if int(getattr(cursor, "rowcount", 0) or 0) != 1:
                    raise MerchantRecoveryError("merchant_recovery_archive_state_changed")
                details = {
                    "roles_before": roles_before,
                    "roles_after": ["observer"],
                    "supplier_activated": False,
                    "remote_inventory_empty": True,
                }
                cursor.execute(
                    """
                    INSERT INTO a6_trade.a6_seller_retirement_events (
                        merchant_id, event_type, from_state, to_state, details
                    ) VALUES (
                        %s, 'safe_archive_completed_without_supplier_activation',
                        'earning', 'completed', %s::jsonb
                    )
                    """,
                    (merchant_id, _json(details)),
                )
                desired = {
                    "merchant_id": merchant_id,
                    "roles": ["observer"],
                    "operation": "complete_never_activated_archive",
                }
                cursor.execute(
                    """
                    INSERT INTO a6_trade.trade_control_actions (
                        idempotency_key, action_type, entity_type, entity_id,
                        desired_state, observed_before, observed_after, status,
                        config_version, requested_by, started_at, completed_at
                    ) VALUES (
                        %s, 'retire_seller_role', 'a6_merchant', %s,
                        %s::jsonb, %s::jsonb, %s::jsonb, 'completed',
                        'merchant-recovery-v1', 'a6-merchant-recovery', now(), now()
                    ) ON CONFLICT (idempotency_key) DO NOTHING
                    """,
                    (
                        f"merchant-recovery:archive:{merchant_id}:v1",
                        str(merchant_id),
                        _json(desired),
                        _json({"roles": roles_before}),
                        _json({"roles": ["observer"]}),
                    ),
                )
                cursor.execute(
                    """
                    UPDATE a6_trade.trade_incidents
                    SET status = 'resolved', resolved_at = now(), updated_at = now()
                    WHERE entity_type = 'merchant' AND entity_id = %s
                      AND status = 'open'
                    """,
                    (str(merchant_id),),
                )
            self.connection.commit()
            return True
        except Exception:
            self.connection.rollback()
            raise

    def resolve_terminal_archive_incidents(self) -> int:
        try:
            with self.connection.cursor() as cursor:
                cursor.execute(
                    """
                    WITH terminal AS (
                        SELECT m.id
                        FROM a6_trade.a6_merchants m
                        JOIN a6_trade.a6_seller_retirement_plans p
                          ON p.merchant_id = m.id
                        WHERE p.state = 'completed' AND p.archive_mode
                          AND m.roles = ARRAY['observer']::text[]
                    ), reset AS (
                        UPDATE a6_trade.a6_merchants m
                        SET poll_error_count = 0, updated_at = now()
                        FROM terminal
                        WHERE m.id = terminal.id AND m.poll_error_count <> 0
                        RETURNING m.id
                    )
                    UPDATE a6_trade.trade_incidents incident
                    SET status = 'resolved', resolved_at = now(), updated_at = now()
                    FROM terminal
                    WHERE incident.entity_type = 'merchant'
                      AND incident.entity_id = terminal.id::text
                      AND incident.status = 'open'
                    RETURNING incident.id
                    """
                )
                resolved = len(cursor.fetchall())
            self.connection.commit()
            return resolved
        except Exception:
            self.connection.rollback()
            raise

    def apply_proxy_repair(
        self,
        target: ProxyRepairTarget,
        *,
        proxy_map_file: Path,
        expected_proxy: str,
        selected: ProxyVerification,
    ) -> bool:
        lock_path = proxy_map_file.with_name(f".{proxy_map_file.name}.lock")
        original_payload: dict[str, Any] | None = None
        map_written = False
        try:
            with self.connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT m.id, m.a6_user_id, m.poll_error_count,
                           m.proxy_lease_id, incident.status AS incident_status,
                           incident.incident_key
                    FROM a6_trade.a6_merchants m
                    JOIN a6_trade.trade_incidents incident ON incident.id = %s
                    WHERE m.id = %s AND m.a6_user_id = %s
                      AND incident.entity_type = 'merchant'
                      AND incident.entity_id = m.id::text
                    FOR UPDATE OF m, incident
                    """,
                    (target.incident_id, target.merchant_id, target.a6_user_id),
                )
                current = _row(cursor)
                if (
                    current is None
                    or str(current.get("incident_status") or "") != "open"
                    or str(current.get("incident_key") or "") != target.incident_key
                    or (
                        target.incident_key.startswith("collector:")
                        and int(current.get("poll_error_count") or 0) == 0
                    )
                ):
                    self.connection.rollback()
                    return False
                with private_file_lock(lock_path):
                    original_payload = read_private_json(proxy_map_file)
                    key = str(target.a6_user_id)
                    if original_payload.get(key) != expected_proxy:
                        raise MerchantRecoveryError("merchant_recovery_proxy_map_changed")
                    if any(
                        other_key != key and value == selected.proxy
                        for other_key, value in original_payload.items()
                    ):
                        raise MerchantRecoveryError("merchant_recovery_proxy_candidate_claimed")
                    _private_backup(proxy_map_file, target.merchant_id)
                    replacement = dict(original_payload)
                    replacement[key] = selected.proxy
                    write_private_json(proxy_map_file, replacement)
                    map_written = True

                    fingerprint = proxy_fingerprint(selected.proxy)
                    lease_id = proxy_lease_id(selected.proxy)
                    metadata = {
                        "pool": "fixed-map",
                        "lease_id": lease_id,
                        "provider": "singbox",
                        "proxy_fingerprint": fingerprint,
                    }
                    cursor.execute(
                        """
                        UPDATE a6_trade.a6_merchants
                        SET proxy_lease_id = %s, proxy_fingerprint = %s,
                            proxy_metadata = %s::jsonb, updated_at = now()
                        WHERE id = %s AND a6_user_id = %s
                        """,
                        (
                            lease_id,
                            fingerprint,
                            _json(metadata),
                            target.merchant_id,
                            target.a6_user_id,
                        ),
                    )
                    before = {
                        "proxy_lease_id": proxy_lease_id(expected_proxy),
                        "poll_error_count": int(current.get("poll_error_count") or 0),
                        "incident_id": target.incident_id,
                    }
                    after = {
                        "proxy_lease_id": lease_id,
                        "candidate_verification_ms": selected.elapsed_ms,
                    }
                    desired = {
                        "merchant_id": target.merchant_id,
                        "proxy_lease_id": lease_id,
                        "operation": "replace_failed_fixed_proxy",
                    }
                    cursor.execute(
                        """
                        INSERT INTO a6_trade.trade_control_actions (
                            idempotency_key, action_type, entity_type, entity_id,
                            desired_state, observed_before, observed_after, status,
                            config_version, requested_by, started_at, completed_at
                        ) VALUES (
                            %s, 'repair_merchant_proxy', 'a6_merchant', %s,
                            %s::jsonb, %s::jsonb, %s::jsonb, 'completed',
                            'merchant-recovery-v1', 'a6-merchant-recovery', now(), now()
                        ) ON CONFLICT (idempotency_key) DO NOTHING
                        """,
                        (
                            (
                                f"merchant-recovery:proxy:{target.merchant_id}:"
                                f"incident:{target.incident_id}:"
                                f"occurrence:{target.occurrence_count}:v1"
                            ),
                            str(target.merchant_id),
                            _json(desired),
                            _json(before),
                            _json(after),
                        ),
                    )
                    self.connection.commit()
            return True
        except Exception:
            self.connection.rollback()
            if map_written and original_payload is not None:
                with private_file_lock(lock_path):
                    write_private_json(proxy_map_file, original_payload)
            raise


class MerchantRecoveryController:
    def __init__(
        self,
        store: PostgresMerchantRecoveryStore,
        *,
        sessions_dir: Path,
        proxy_map_file: Path,
        proxy_pool_files: Sequence[Path],
        reservation_files: Sequence[Path] = (),
        verifier: A6ProxyVerifier | Any | None = None,
    ) -> None:
        self.store = store
        self.sessions_dir = sessions_dir.expanduser().resolve()
        self.proxy_map_file = proxy_map_file.expanduser().resolve()
        self.proxy_pool_files = tuple(proxy_pool_files)
        self.reservation_files = tuple(reservation_files)
        self.verifier = verifier or A6ProxyVerifier()

    def run_once(
        self,
        *,
        apply: bool,
        failure_threshold: int = 3,
        stale_seconds: int = 1800,
        repair_cooldown_seconds: int = 3600,
        candidate_limit: int = 16,
        max_proxy_repairs: int = 1,
        archive_minimum_age_seconds: int = 1800,
        archive_minimum_errors: int = 3,
        max_archives: int = 10,
    ) -> dict[str, Any]:
        _validate_limits(
            failure_threshold=failure_threshold,
            stale_seconds=stale_seconds,
            repair_cooldown_seconds=repair_cooldown_seconds,
            candidate_limit=candidate_limit,
            max_proxy_repairs=max_proxy_repairs,
            archive_minimum_age_seconds=archive_minimum_age_seconds,
            archive_minimum_errors=archive_minimum_errors,
            max_archives=max_archives,
        )
        terminal_incidents_resolved = (
            self.store.resolve_terminal_archive_incidents() if apply else 0
        )
        archive_candidates = self.store.never_activated_archive_candidates(
            minimum_age_seconds=archive_minimum_age_seconds,
            minimum_errors=archive_minimum_errors,
            limit=max_archives,
        )
        archived: list[int] = []
        if apply:
            for merchant_id in archive_candidates:
                if self.store.complete_never_activated_archive(
                    merchant_id,
                    minimum_age_seconds=archive_minimum_age_seconds,
                    minimum_errors=archive_minimum_errors,
                ):
                    archived.append(merchant_id)

        proxy_map = load_proxy_map(self.proxy_map_file)
        pool = load_proxy_pool(self.proxy_pool_files)
        reserved = load_reserved_proxy_fingerprints(self.reservation_files)
        targets = self.store.proxy_repair_targets(
            failure_threshold=failure_threshold,
            stale_seconds=stale_seconds,
            repair_cooldown_seconds=repair_cooldown_seconds,
            limit=max_proxy_repairs,
        )
        used = set(proxy_map.values())
        proxy_results: list[dict[str, Any]] = []
        for target in targets:
            try:
                record = self._record(target)
                current_proxy = proxy_map.get(target.a6_user_id)
                if current_proxy and self.verifier.verify(record, current_proxy) is not None:
                    proxy_results.append(
                        {
                            "merchant_id": target.merchant_id,
                            "action": "current_proxy_recovered",
                            "applied": False,
                        }
                    )
                    continue
                candidates = [
                    proxy
                    for proxy in pool
                    if proxy not in used
                    and proxy_reservation_fingerprint(proxy) not in reserved
                ]
                candidates.sort(key=_proxy_sort_key)
                selected: ProxyVerification | None = None
                for candidate in candidates[:candidate_limit]:
                    selected = self.verifier.verify(record, candidate)
                    if selected is not None:
                        break
                if selected is None:
                    proxy_results.append(
                        {
                            "merchant_id": target.merchant_id,
                            "action": "deferred_no_healthy_proxy",
                            "applied": False,
                        }
                    )
                    continue
                changed = False
                if apply:
                    if current_proxy is None:
                        raise MerchantRecoveryError("merchant_recovery_current_proxy_missing")
                    changed = self.store.apply_proxy_repair(
                        target,
                        proxy_map_file=self.proxy_map_file,
                        expected_proxy=current_proxy,
                        selected=selected,
                    )
                    if changed:
                        used.discard(current_proxy)
                        used.add(selected.proxy)
                        proxy_map[target.a6_user_id] = selected.proxy
                proxy_results.append(
                    {
                        "merchant_id": target.merchant_id,
                        "action": "proxy_replaced" if changed else "proxy_replacement_planned",
                        "applied": changed,
                        "verification_ms": selected.elapsed_ms,
                        "new_lease_id": proxy_lease_id(selected.proxy),
                    }
                )
            except Exception as exc:
                proxy_results.append(
                    {
                        "merchant_id": target.merchant_id,
                        "action": "repair_failed",
                        "applied": False,
                        "error_class": _error_code(exc),
                    }
                )
        return {
            "success": not any(item["action"] == "repair_failed" for item in proxy_results),
            "mode": "applied" if apply else "dry-run",
            "terminal_incidents_resolved": terminal_incidents_resolved,
            "archive_candidate_count": len(archive_candidates),
            "archived_count": len(archived),
            "archived_merchant_ids": archived,
            "proxy_target_count": len(targets),
            "proxy_repair_count": sum(item.get("applied") is True for item in proxy_results),
            "proxy_results": proxy_results,
        }

    def _record(self, target: ProxyRepairTarget) -> SessionRecord:
        basename = Path(target.session_ref).name
        path = (self.sessions_dir / basename).resolve()
        if not basename or path.parent != self.sessions_dir or not path.is_file():
            raise MerchantRecoveryError("merchant_recovery_session_unavailable")
        record = SessionStore(path).load()
        if (
            record.status != "supplier_submitted"
            or record.user_id != target.a6_user_id
            or record.supplier_id != target.supplier_id
            or not record.cookies
        ):
            raise MerchantRecoveryError("merchant_recovery_session_mismatch")
        return record


_NEVER_ACTIVATED_ARCHIVE_QUERY = """
SELECT m.id AS merchant_id, m.roles
FROM a6_trade.a6_merchants m
JOIN a6_trade.a6_seller_retirement_plans p ON p.merchant_id = m.id
WHERE p.archive_mode AND p.state = 'earning'
  AND p.requested_at IS NOT NULL
  AND p.requested_at <= now() - (%s * interval '1 second')
  AND m.status IN ('authenticated', 'supplier_rejected')
  AND m.supplier_id IS NULL
  AND m.roles IN (
      ARRAY['seller']::text[],
      ARRAY['seller', 'observer']::text[]
  )
  AND m.poll_error_count >= %s
  AND NOT EXISTS (
      SELECT 1 FROM a6_trade.a6_supplier_channels_current channel
      WHERE channel.merchant_id = m.id
  )
  AND NOT EXISTS (
      SELECT 1 FROM a6_trade.a6_supplier_listings_current listing
      WHERE listing.merchant_id = m.id
  )
  AND NOT EXISTS (
      SELECT 1
      FROM a6_trade.a6_tokens token
      JOIN a6_trade.enodot_account_bindings binding
        ON binding.a6_token_id = token.id
      WHERE token.merchant_id = m.id
  )
  AND NOT EXISTS (
      SELECT 1 FROM a6_trade.a6_financial_current financial
      WHERE financial.merchant_id = m.id
        AND (
            coalesce(financial.user_balance_quota, 0) > 0
            OR coalesce(financial.withdrawable_micros, 0) > 0
            OR coalesce(financial.withdrawing_micros, 0) > 0
        )
  )
"""


def _validate_limits(**values: int) -> None:
    bounds = {
        "failure_threshold": (2, 1000),
        "stale_seconds": (600, 86400),
        "repair_cooldown_seconds": (300, 86400),
        "candidate_limit": (1, 100),
        "max_proxy_repairs": (1, 20),
        "archive_minimum_age_seconds": (300, 604800),
        "archive_minimum_errors": (1, 1000),
        "max_archives": (1, 100),
    }
    for name, value in values.items():
        lower, upper = bounds[name]
        if isinstance(value, bool) or not isinstance(value, int) or not lower <= value <= upper:
            raise MerchantRecoveryError("merchant_recovery_limits_invalid")


def _private_backup(path: Path, merchant_id: int) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    backup = path.with_name(f"{path.name}.bak-{stamp}-merchant-{merchant_id}")
    shutil.copy2(path, backup)
    os.chmod(backup, 0o600)
    return backup


def _proxy_sort_key(proxy: str) -> tuple[int, str]:
    return (int(urlsplit(proxy).port or 65536), proxy)


def _positive_int(value: object) -> int:
    if isinstance(value, bool):
        return 0
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 0
    return parsed if parsed > 0 else 0


def _json(value: Mapping[str, Any]) -> str:
    return json.dumps(dict(value), ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _row(cursor: Any) -> dict[str, Any] | None:
    value = cursor.fetchone()
    if value is None:
        return None
    if isinstance(value, Mapping):
        return dict(value)
    names = [getattr(column, "name", column[0]) for column in cursor.description]
    return dict(zip(names, value, strict=True))


def _rows(cursor: Any) -> list[dict[str, Any]]:
    values = cursor.fetchall()
    if not values:
        return []
    if isinstance(values[0], Mapping):
        return [dict(value) for value in values]
    names = [getattr(column, "name", column[0]) for column in cursor.description]
    return [dict(zip(names, value, strict=True)) for value in values]


def _error_code(exc: BaseException) -> str:
    value = str(exc or "").strip().lower()
    return value if _SAFE_ERROR.fullmatch(value) else "merchant_recovery_internal_error"
