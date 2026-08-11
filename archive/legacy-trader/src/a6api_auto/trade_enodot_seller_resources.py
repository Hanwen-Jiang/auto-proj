from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Protocol

from .storage import ensure_private_directory, read_private_json, write_private_json

_MANAGED_UID = re.compile(r"^[a-z0-9][a-z0-9:._-]{0,255}$")
_FINGERPRINT = re.compile(r"^[0-9a-f]{64}$")
_SECRET_REF = re.compile(r"^enodot-seller:sha256:([0-9a-f]{64})$")
_SELLER_STATUS = "supplier_submitted"
_SELLER_DUTIES = frozenset({"provider", "observer"})
_INITIAL_WORKING_CREDIT_USD = Decimal("999999999999.00000000")


class ManagedSellerError(RuntimeError):
    """Stable error code that never retains Enodot credentials or response bodies."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class ManagedSellerRequest:
    merchant_id: int
    group_id: int
    delta: Decimal | float | str = Decimal("0")
    config_version: str = "phase1-enodot-managed-seller-v1"
    requested_by: str = "a6-trader"

    def __post_init__(self) -> None:
        merchant_id = _positive_int(self.merchant_id, "managed_seller_merchant_id_invalid")
        group_id = _positive_int(self.group_id, "managed_seller_group_id_invalid")
        delta = _delta(self.delta)
        if not _MANAGED_UID.fullmatch(str(self.config_version)):
            raise ManagedSellerError("managed_seller_config_version_invalid")
        if not _MANAGED_UID.fullmatch(str(self.requested_by)):
            raise ManagedSellerError("managed_seller_requested_by_invalid")
        object.__setattr__(self, "merchant_id", merchant_id)
        object.__setattr__(self, "group_id", group_id)
        object.__setattr__(self, "delta", delta)

    @property
    def managed_uid(self) -> str:
        return f"a6trader:seller:merchant-{self.merchant_id}"

    @property
    def idempotency_key(self) -> str:
        digest = hashlib.sha256(
            f"{self.managed_uid}\n{self.group_id}\n{self.config_version}".encode()
        ).hexdigest()
        return f"a6trader-seller-{digest[:40]}"

    @property
    def delta_idempotency_key(self) -> str:
        digest = hashlib.sha256(
            (
                f"{self.managed_uid}\n{self.group_id}\n"
                f"{_decimal_text(self.delta)}\n{self.config_version}"
            ).encode()
        ).hexdigest()
        return f"a6trader-seller-delta-{digest[:40]}"


@dataclass(frozen=True)
class ManagedSellerContext:
    merchant_id: int
    a6_user_id: int | None
    supplier_id: int | None
    merchant_status: str
    roles: tuple[str, ...]
    session_ref: str


@dataclass(frozen=True)
class ManagedSellerBinding:
    binding_id: int
    merchant_id: int
    managed_uid: str
    group_id: int
    desired_delta: Decimal
    control_state: str
    enodot_user_id: int | None = None
    enodot_api_key_id: int | None = None
    api_key_fingerprint: str = ""
    key_secret_ref: str = ""


@dataclass(frozen=True)
class ManagedSellerClaim:
    binding: ManagedSellerBinding
    action_id: int
    attempt_no: int


@dataclass(frozen=True)
class ManagedSellerRemote:
    managed_uid: str
    user_id: int
    api_key_id: int
    group_id: int
    delta: Decimal
    initial_working_credit_usd: Decimal
    status: str
    api_key: str = field(default="", repr=False, compare=False)

    def audit_snapshot(self) -> dict[str, Any]:
        return {
            "managed_uid": self.managed_uid,
            "enodot_user_id": self.user_id,
            "enodot_api_key_id": self.api_key_id,
            "group_id": self.group_id,
            "delta": _decimal_text(self.delta),
            "initial_working_credit_usd": _decimal_text(
                self.initial_working_credit_usd
            ),
            "status": self.status,
        }


class _ManagedSellerCapabilityToken:
    __slots__ = ()


@dataclass(frozen=True)
class ManagedSellerResult:
    mode: str
    merchant_id: int
    managed_uid: str
    action: str
    applied: bool
    verified: bool
    state: str
    enodot_user_id: int | None = None
    enodot_api_key_id: int | None = None
    delta: Decimal | None = None
    error_class: str | None = None
    preflight_verified: bool = False

    def public(self) -> dict[str, Any]:
        return {
            "success": self.error_class is None,
            "mode": self.mode,
            "merchant_id": self.merchant_id,
            "managed_uid": self.managed_uid,
            "action": self.action,
            "applied": self.applied,
            "verified": self.verified,
            "state": self.state,
            "enodot_user_id": self.enodot_user_id,
            "enodot_api_key_id": self.enodot_api_key_id,
            "delta": _decimal_text(self.delta) if self.delta is not None else None,
            "error_class": self.error_class,
        }


class ManagedSellerStore(Protocol):
    def list_seller_ids(self) -> tuple[int, ...]: ...

    def list_bound_seller_ids(self) -> tuple[int, ...]: ...

    def load_context(self, merchant_id: int) -> ManagedSellerContext: ...

    def get_binding(self, merchant_id: int) -> ManagedSellerBinding | None: ...

    def claim_create(
        self,
        context: ManagedSellerContext,
        request: ManagedSellerRequest,
        desired_state: Mapping[str, Any],
    ) -> ManagedSellerClaim | None: ...

    def claim_update(
        self,
        binding: ManagedSellerBinding,
        request: ManagedSellerRequest,
        desired_state: Mapping[str, Any],
    ) -> ManagedSellerClaim | None: ...

    def mark_unknown(
        self,
        claim: ManagedSellerClaim,
        *,
        state: str,
        error_class: str,
    ) -> None: ...

    def save_secret_receipt(
        self,
        binding: ManagedSellerBinding,
        *,
        api_key_fingerprint: str,
        key_secret_ref: str,
    ) -> ManagedSellerBinding: ...

    def mark_bound(
        self,
        claim: ManagedSellerClaim,
        remote: ManagedSellerRemote,
        *,
        api_key_fingerprint: str,
        key_secret_ref: str,
    ) -> ManagedSellerBinding: ...


class EnodotManagedSellerAdmin(Protocol):
    def get_managed_seller_capability(self) -> Mapping[str, Any]: ...

    def get_managed_seller(self, managed_uid: str) -> Mapping[str, Any] | None: ...

    def create_managed_seller(
        self,
        *,
        managed_uid: str,
        group_id: int,
        delta: Decimal,
        idempotency_key: str,
    ) -> Mapping[str, Any]: ...

    def set_managed_seller_delta(
        self,
        managed_uid: str,
        delta: Decimal,
        *,
        idempotency_key: str,
    ) -> Mapping[str, Any]: ...


class ManagedSellerSecretSink(Protocol):
    def store_api_key(self, managed_uid: str, api_key: str) -> tuple[str, str]: ...

    def read_api_key(self, managed_uid: str, secret_ref: str) -> str: ...

    def get_api_key_fingerprint(self, secret_ref: str) -> str: ...


class PrivateFileManagedSellerSecretSink:
    """Owner-only immutable files for Enodot API keys created by the controller."""

    def __init__(self, root: Path) -> None:
        # Keep construction read-only so a CLI dry-run does not create state.
        self.root = root.expanduser().resolve()

    def __repr__(self) -> str:
        return f"{type(self).__name__}(root={str(self.root)!r})"

    def store_api_key(self, managed_uid: str, api_key: str) -> tuple[str, str]:
        if not _MANAGED_UID.fullmatch(str(managed_uid)):
            raise ManagedSellerError("managed_seller_secret_uid_invalid")
        key = _secret_value(api_key)
        uid_digest = hashlib.sha256(managed_uid.encode()).hexdigest()
        secret_ref = f"enodot-seller:sha256:{uid_digest}"
        ensure_private_directory(self.root)
        path = self.root / f"{uid_digest}.json"
        expected = {
            "schema_version": 1,
            "managed_uid": managed_uid,
            "api_key": key,
        }
        try:
            if path.exists():
                current = read_private_json(path)
                if current != expected:
                    raise ManagedSellerError("managed_seller_secret_conflict")
            else:
                write_private_json(path, expected)
            confirmed = read_private_json(path)
        except ManagedSellerError:
            raise
        except Exception:
            raise ManagedSellerError("managed_seller_secret_write_failed") from None
        if confirmed != expected:
            raise ManagedSellerError("managed_seller_secret_write_unconfirmed")
        return secret_ref, hashlib.sha256(key.encode()).hexdigest()

    def read_api_key(self, managed_uid: str, secret_ref: str) -> str:
        if not _MANAGED_UID.fullmatch(str(managed_uid)):
            raise ManagedSellerError("managed_seller_secret_uid_invalid")
        match = _SECRET_REF.fullmatch(str(secret_ref))
        expected_digest = hashlib.sha256(managed_uid.encode()).hexdigest()
        if match is None or match.group(1) != expected_digest:
            raise ManagedSellerError("managed_seller_secret_identity_mismatch")
        try:
            value = read_private_json(self.root / f"{expected_digest}.json")
            stored_uid = str(value.get("managed_uid") or "")
            key = _secret_value(value.get("api_key"))
        except ManagedSellerError:
            raise
        except Exception:
            raise ManagedSellerError("managed_seller_secret_read_failed") from None
        if stored_uid != managed_uid:
            raise ManagedSellerError("managed_seller_secret_identity_mismatch")
        return key

    def get_api_key_fingerprint(self, secret_ref: str) -> str:
        match = _SECRET_REF.fullmatch(str(secret_ref))
        if match is None:
            raise ManagedSellerError("managed_seller_secret_ref_invalid")
        path = self.root / f"{match.group(1)}.json"
        try:
            value = read_private_json(path)
            managed_uid = str(value.get("managed_uid") or "")
            key = _secret_value(value.get("api_key"))
        except ManagedSellerError:
            raise
        except Exception:
            raise ManagedSellerError("managed_seller_secret_read_failed") from None
        if hashlib.sha256(managed_uid.encode()).hexdigest() != match.group(1):
            raise ManagedSellerError("managed_seller_secret_identity_mismatch")
        return hashlib.sha256(key.encode()).hexdigest()


class PostgresManagedSellerStore:
    def __init__(self, connection_factory: Callable[[], Any]) -> None:
        self.connection_factory = connection_factory

    def list_seller_ids(self) -> tuple[int, ...]:
        return self._list_seller_ids(_SELLERS_QUERY)

    def list_bound_seller_ids(self) -> tuple[int, ...]:
        return self._list_seller_ids(_BOUND_SELLERS_QUERY)

    def _list_seller_ids(self, query: str) -> tuple[int, ...]:
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(query)
                rows = cursor.fetchall()
            merchant_ids = sorted(
                _positive_int(
                    row["merchant_id"] if isinstance(row, Mapping) else row[0],
                    "managed_seller_result_invalid",
                )
                for row in rows
            )
            if len(merchant_ids) != len(set(merchant_ids)):
                raise ManagedSellerError("managed_seller_result_invalid")
            return tuple(merchant_ids)
        finally:
            _close(connection)

    def load_context(self, merchant_id: int) -> ManagedSellerContext:
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(_SELLER_CONTEXT_QUERY, (merchant_id,))
                row = _row(cursor)
            if row is None:
                raise ManagedSellerError("managed_seller_context_missing")
            return _context_from_row(row)
        finally:
            _close(connection)

    def get_binding(self, merchant_id: int) -> ManagedSellerBinding | None:
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(_SELLER_BINDING_QUERY, (merchant_id,))
                row = _row(cursor)
            return _binding_from_row(row) if row is not None else None
        finally:
            _close(connection)

    def claim_create(
        self,
        context: ManagedSellerContext,
        request: ManagedSellerRequest,
        desired_state: Mapping[str, Any],
    ) -> ManagedSellerClaim | None:
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(_SELLER_CONTEXT_QUERY + " FOR UPDATE", (request.merchant_id,))
                row = _row(cursor)
                if row is None:
                    raise ManagedSellerError("managed_seller_context_missing")
                locked = _context_from_row(row)
                _validate_context(locked, request)
                if locked != context:
                    raise ManagedSellerError("managed_seller_context_changed")
                cursor.execute(_SELLER_BINDING_QUERY + " FOR UPDATE", (request.merchant_id,))
                existing_row = _row(cursor)
                existing = _binding_from_row(existing_row) if existing_row is not None else None
                if existing is not None:
                    _validate_binding(existing, request)
                    if existing.control_state not in {"planned", "error"}:
                        connection.rollback()
                        return None
                observed_before = _binding_snapshot(existing)
                cursor.execute(
                    """
                    INSERT INTO a6_trade.trade_control_actions (
                        idempotency_key, action_type, entity_type, entity_id,
                        desired_state, observed_before, status,
                        config_version, requested_by, started_at
                    ) VALUES (
                        %s, 'create_enodot_seller_resource', 'merchant', %s,
                        %s::jsonb, %s::jsonb, 'started', %s, %s, now()
                    )
                    ON CONFLICT (idempotency_key) DO NOTHING
                    RETURNING id
                    """,
                    (
                        request.idempotency_key,
                        str(request.merchant_id),
                        _json(desired_state),
                        _json(observed_before),
                        request.config_version,
                        request.requested_by,
                    ),
                )
                action = _row(cursor)
                if action is None:
                    connection.rollback()
                    return None
                action_id = _positive_int(action["id"], "managed_seller_action_invalid")
                cursor.execute(
                    """
                    INSERT INTO a6_trade.enodot_managed_seller_resources (
                        merchant_id, managed_uid, group_id, desired_delta,
                        create_idempotency_key, control_state, desired_state,
                        create_action_id, last_action_id,
                        first_attempted_at, last_attempted_at
                    ) VALUES (
                        %s, %s, %s, %s, %s, 'creating', %s::jsonb,
                        %s, %s, now(), now()
                    )
                    ON CONFLICT (merchant_id) DO UPDATE SET
                        group_id = EXCLUDED.group_id,
                        desired_delta = EXCLUDED.desired_delta,
                        desired_state = EXCLUDED.desired_state,
                        control_state = 'creating',
                        create_action_id = coalesce(
                            a6_trade.enodot_managed_seller_resources.create_action_id,
                            EXCLUDED.create_action_id
                        ),
                        last_action_id = EXCLUDED.last_action_id,
                        first_attempted_at = coalesce(
                            a6_trade.enodot_managed_seller_resources.first_attempted_at,
                            now()
                        ),
                        last_attempted_at = now(),
                        last_error_class = NULL,
                        updated_at = now()
                    WHERE a6_trade.enodot_managed_seller_resources.managed_uid =
                              EXCLUDED.managed_uid
                      AND a6_trade.enodot_managed_seller_resources.control_state
                              IN ('planned', 'error')
                    RETURNING *
                    """,
                    (
                        request.merchant_id,
                        request.managed_uid,
                        request.group_id,
                        request.delta,
                        request.idempotency_key,
                        _json(desired_state),
                        action_id,
                        action_id,
                    ),
                )
                claimed_row = _row(cursor)
                if claimed_row is None:
                    raise ManagedSellerError("managed_seller_create_claim_conflict")
                attempt_no = self._start_attempt(cursor, action_id, observed_before)
            connection.commit()
            return ManagedSellerClaim(
                _binding_from_row(claimed_row),
                action_id=action_id,
                attempt_no=attempt_no,
            )
        except Exception:
            connection.rollback()
            raise
        finally:
            _close(connection)

    def claim_update(
        self,
        binding: ManagedSellerBinding,
        request: ManagedSellerRequest,
        desired_state: Mapping[str, Any],
    ) -> ManagedSellerClaim | None:
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(_SELLER_BINDING_QUERY + " FOR UPDATE", (request.merchant_id,))
                row = _row(cursor)
                if row is None:
                    raise ManagedSellerError("managed_seller_binding_missing")
                locked = _binding_from_row(row)
                _validate_binding(locked, request)
                if locked != binding:
                    raise ManagedSellerError("managed_seller_binding_changed")
                if locked.control_state in {"creating", "updating"}:
                    connection.rollback()
                    return None
                observed_before = _binding_snapshot(locked)
                cursor.execute(
                    """
                    INSERT INTO a6_trade.trade_control_actions (
                        idempotency_key, action_type, entity_type, entity_id,
                        desired_state, observed_before, status,
                        config_version, requested_by, started_at
                    ) VALUES (
                        %s, 'update_enodot_seller_resource', 'merchant', %s,
                        %s::jsonb, %s::jsonb, 'started', %s, %s, now()
                    )
                    ON CONFLICT (idempotency_key) DO UPDATE SET
                        desired_state = EXCLUDED.desired_state,
                        observed_before = EXCLUDED.observed_before,
                        status = 'started', error_class = NULL,
                        started_at = now(), completed_at = NULL,
                        elapsed_ms = NULL, updated_at = now()
                    RETURNING id
                    """,
                    (
                        request.delta_idempotency_key,
                        str(request.merchant_id),
                        _json(desired_state),
                        _json(observed_before),
                        request.config_version,
                        request.requested_by,
                    ),
                )
                action = _row(cursor)
                if action is None:
                    raise ManagedSellerError("managed_seller_audit_write_failed")
                action_id = _positive_int(action["id"], "managed_seller_action_invalid")
                cursor.execute(
                    """
                    UPDATE a6_trade.enodot_managed_seller_resources
                    SET desired_delta = %s, desired_state = %s::jsonb,
                        control_state = 'updating', last_action_id = %s,
                        last_attempted_at = now(), last_error_class = NULL,
                        updated_at = now()
                    WHERE id = %s AND merchant_id = %s AND managed_uid = %s
                    RETURNING *
                    """,
                    (
                        request.delta,
                        _json(desired_state),
                        action_id,
                        binding.binding_id,
                        request.merchant_id,
                        request.managed_uid,
                    ),
                )
                claimed_row = _row(cursor)
                if claimed_row is None:
                    raise ManagedSellerError("managed_seller_update_claim_conflict")
                attempt_no = self._start_attempt(cursor, action_id, observed_before)
            connection.commit()
            return ManagedSellerClaim(
                _binding_from_row(claimed_row),
                action_id=action_id,
                attempt_no=attempt_no,
            )
        except Exception:
            connection.rollback()
            raise
        finally:
            _close(connection)

    @staticmethod
    def _start_attempt(
        cursor: Any,
        action_id: int,
        observed_before: Mapping[str, Any],
    ) -> int:
        cursor.execute(
            """
            SELECT coalesce(max(attempt_no), 0) + 1 AS attempt_no
            FROM a6_trade.trade_control_action_attempts
            WHERE action_id = %s
            """,
            (action_id,),
        )
        row = _row(cursor)
        attempt_no = _positive_int(
            (row or {}).get("attempt_no"),
            "managed_seller_attempt_invalid",
        )
        cursor.execute(
            """
            INSERT INTO a6_trade.trade_control_action_attempts (
                action_id, attempt_no, status, observed_before, started_at
            ) VALUES (%s, %s, 'started', %s::jsonb, now())
            """,
            (action_id, attempt_no, _json(observed_before)),
        )
        return attempt_no

    def mark_unknown(
        self,
        claim: ManagedSellerClaim,
        *,
        state: str,
        error_class: str,
    ) -> None:
        if state not in {"create_unknown", "update_unknown"}:
            raise ManagedSellerError("managed_seller_unknown_state_invalid")
        if not _MANAGED_UID.fullmatch(str(error_class)):
            raise ManagedSellerError("managed_seller_error_class_invalid")
        connection = self.connection_factory()
        try:
            fingerprint = hashlib.sha256(error_class.encode()).hexdigest()
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE a6_trade.enodot_managed_seller_resources
                    SET control_state = %s, last_error_class = %s,
                        last_attempted_at = now(), updated_at = now()
                    WHERE id = %s AND last_action_id = %s
                    """,
                    (state, error_class, claim.binding.binding_id, claim.action_id),
                )
                cursor.execute(
                    """
                    UPDATE a6_trade.trade_control_actions
                    SET status = 'unknown', error_class = %s,
                        completed_at = now(), updated_at = now()
                    WHERE id = %s
                    """,
                    (error_class, claim.action_id),
                )
                cursor.execute(
                    """
                    UPDATE a6_trade.trade_control_action_attempts
                    SET status = 'unknown', error_class = %s,
                        error_fingerprint = %s, completed_at = now()
                    WHERE action_id = %s AND attempt_no = %s
                    """,
                    (error_class, fingerprint, claim.action_id, claim.attempt_no),
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            _close(connection)

    def save_secret_receipt(
        self,
        binding: ManagedSellerBinding,
        *,
        api_key_fingerprint: str,
        key_secret_ref: str,
    ) -> ManagedSellerBinding:
        if not _FINGERPRINT.fullmatch(str(api_key_fingerprint)) or not key_secret_ref:
            raise ManagedSellerError("managed_seller_secret_receipt_invalid")
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE a6_trade.enodot_managed_seller_resources
                    SET api_key_fingerprint = %s, key_secret_ref = %s,
                        updated_at = now()
                    WHERE id = %s AND merchant_id = %s AND managed_uid = %s
                      AND (api_key_fingerprint IS NULL OR api_key_fingerprint = %s)
                      AND (key_secret_ref IS NULL OR key_secret_ref = %s)
                    RETURNING *
                    """,
                    (
                        api_key_fingerprint,
                        key_secret_ref,
                        binding.binding_id,
                        binding.merchant_id,
                        binding.managed_uid,
                        api_key_fingerprint,
                        key_secret_ref,
                    ),
                )
                row = _row(cursor)
                if row is None:
                    raise ManagedSellerError("managed_seller_secret_receipt_conflict")
            connection.commit()
            return _binding_from_row(row)
        except Exception:
            connection.rollback()
            raise
        finally:
            _close(connection)

    def mark_bound(
        self,
        claim: ManagedSellerClaim,
        remote: ManagedSellerRemote,
        *,
        api_key_fingerprint: str,
        key_secret_ref: str,
    ) -> ManagedSellerBinding:
        _validate_secret_receipt_fields(api_key_fingerprint, key_secret_ref)
        observed = remote.audit_snapshot()
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE a6_trade.enodot_managed_seller_resources
                    SET enodot_user_id = %s, enodot_api_key_id = %s,
                        api_key_fingerprint = %s, key_secret_ref = %s,
                        observed_delta = %s, control_state = 'bound',
                        observed_state = %s::jsonb, last_error_class = NULL,
                        last_observed_at = now(), verified_at = now(),
                        updated_at = now()
                    WHERE id = %s AND merchant_id = %s AND managed_uid = %s
                      AND group_id = %s AND last_action_id = %s
                    RETURNING *
                    """,
                    (
                        remote.user_id,
                        remote.api_key_id,
                        api_key_fingerprint,
                        key_secret_ref,
                        remote.delta,
                        _json(observed),
                        claim.binding.binding_id,
                        claim.binding.merchant_id,
                        remote.managed_uid,
                        remote.group_id,
                        claim.action_id,
                    ),
                )
                row = _row(cursor)
                if row is None:
                    raise ManagedSellerError("managed_seller_bound_write_conflict")
                cursor.execute(
                    """
                    UPDATE a6_trade.trade_control_actions
                    SET observed_after = %s::jsonb, status = 'completed',
                        error_class = NULL, completed_at = now(), updated_at = now()
                    WHERE id = %s
                    """,
                    (_json(observed), claim.action_id),
                )
                cursor.execute(
                    """
                    UPDATE a6_trade.trade_control_action_attempts
                    SET observed_after = %s::jsonb, status = 'completed',
                        error_class = NULL, completed_at = now()
                    WHERE action_id = %s AND attempt_no = %s
                    """,
                    (_json(observed), claim.action_id, claim.attempt_no),
                )
            connection.commit()
            return _binding_from_row(row)
        except Exception:
            connection.rollback()
            raise
        finally:
            _close(connection)


class ManagedSellerService:
    def __init__(
        self,
        store: ManagedSellerStore,
        admin: EnodotManagedSellerAdmin,
        secret_sink: ManagedSellerSecretSink,
    ) -> None:
        self.store = store
        self.admin = admin
        self.secret_sink = secret_sink
        self.__capability_token = _ManagedSellerCapabilityToken()

    def preflight_contract(self) -> object:
        try:
            document = self.admin.get_managed_seller_capability()
        except Exception:
            raise ManagedSellerError("managed_seller_capability_unavailable") from None
        _parse_capability(document)
        return self.__capability_token

    def execute(
        self,
        request: ManagedSellerRequest,
        *,
        apply: bool = False,
        capability: object | None = None,
    ) -> ManagedSellerResult:
        if not isinstance(apply, bool):
            raise ManagedSellerError("managed_seller_apply_invalid")
        if capability is None:
            capability = self.preflight_contract()
        if capability is not self.__capability_token:
            raise ManagedSellerError("managed_seller_capability_invalid")
        context = self.store.load_context(request.merchant_id)
        _validate_context(context, request)
        binding = self.store.get_binding(request.merchant_id)
        if binding is not None:
            _validate_binding(binding, request)
        remote = _read_remote(self.admin, request.managed_uid)
        desired = _desired_state(context, request)

        if remote is None:
            if binding is not None and (
                binding.control_state == "bound"
                or binding.enodot_user_id is not None
                or binding.enodot_api_key_id is not None
            ):
                raise ManagedSellerError("managed_seller_bound_readback_missing")
            return self._create(request, context, binding, desired, apply=apply)
        _verify_remote_identity(remote, request, binding)
        if binding is None:
            raise ManagedSellerError("managed_seller_remote_untracked")
        if binding.control_state == "create_unknown" and (
            not binding.key_secret_ref or not binding.api_key_fingerprint
        ):
            raise ManagedSellerError("managed_seller_create_secret_lost")
        binding = self._ensure_secret_receipt(binding, remote)
        fingerprint = _binding_fingerprint(binding, self.secret_sink)
        if remote.delta != request.delta:
            return self._update_delta(
                request,
                binding,
                remote,
                desired,
                fingerprint,
                apply=apply,
            )
        if binding.control_state != "bound":
            if not apply:
                return _result(
                    request,
                    remote,
                    "reconcile",
                    False,
                    False,
                    binding.control_state,
                    mode="dry-run",
                )
            claim = self.store.claim_update(binding, request, desired)
            if claim is None:
                raise ManagedSellerError("managed_seller_update_already_claimed")
            bound = self.store.mark_bound(
                claim,
                remote,
                api_key_fingerprint=fingerprint,
                key_secret_ref=binding.key_secret_ref,
            )
            return _result(
                request,
                remote,
                "reconcile",
                True,
                True,
                bound.control_state,
                mode="applied",
            )
        return _result(
            request,
            remote,
            "noop",
            False,
            True,
            "bound",
            mode="applied" if apply else "dry-run",
        )

    def _create(
        self,
        request: ManagedSellerRequest,
        context: ManagedSellerContext,
        binding: ManagedSellerBinding | None,
        desired: Mapping[str, Any],
        *,
        apply: bool,
    ) -> ManagedSellerResult:
        if binding is not None and binding.control_state in {"creating", "create_unknown"}:
            return ManagedSellerResult(
                mode="applied" if apply else "dry-run",
                merchant_id=request.merchant_id,
                managed_uid=request.managed_uid,
                action="reconcile_pending",
                applied=False,
                verified=False,
                state=binding.control_state,
                enodot_user_id=binding.enodot_user_id,
                enodot_api_key_id=binding.enodot_api_key_id,
                delta=binding.desired_delta,
            )
        if not apply:
            return ManagedSellerResult(
                mode="dry-run",
                merchant_id=request.merchant_id,
                managed_uid=request.managed_uid,
                action="create",
                applied=False,
                verified=False,
                state=binding.control_state if binding else "planned",
                delta=request.delta,
            )
        claim = self.store.claim_create(context, request, desired)
        if claim is None:
            raise ManagedSellerError("managed_seller_create_already_claimed")
        try:
            document = self.admin.create_managed_seller(
                managed_uid=request.managed_uid,
                group_id=request.group_id,
                delta=request.delta,
                idempotency_key=request.idempotency_key,
            )
            created = _parse_remote(document, require_api_key=True)
        except Exception as exc:
            code = _error_code(exc)
            self.store.mark_unknown(claim, state="create_unknown", error_class=code)
            raise ManagedSellerError("managed_seller_create_result_unknown") from None
        try:
            _verify_remote_identity(created, request, claim.binding)
        except Exception as exc:
            self.store.mark_unknown(
                claim,
                state="create_unknown",
                error_class=_error_code(exc),
            )
            raise ManagedSellerError("managed_seller_create_result_unknown") from None
        try:
            secret_ref, fingerprint = self.secret_sink.store_api_key(
                request.managed_uid,
                created.api_key,
            )
            _validate_secret_receipt(secret_ref, fingerprint, created.api_key)
            saved = self.store.save_secret_receipt(
                claim.binding,
                api_key_fingerprint=fingerprint,
                key_secret_ref=secret_ref,
            )
        except Exception as exc:
            self.store.mark_unknown(
                claim,
                state="create_unknown",
                error_class=_error_code(exc),
            )
            raise ManagedSellerError("managed_seller_create_result_unknown") from None
        claim = ManagedSellerClaim(saved, claim.action_id, claim.attempt_no)
        try:
            observed = _read_remote(self.admin, request.managed_uid)
            if observed is None:
                raise ManagedSellerError("managed_seller_create_readback_missing")
            _verify_remote_identity(observed, request, claim.binding)
            if observed.delta != request.delta:
                raise ManagedSellerError("managed_seller_create_delta_mismatch")
        except Exception as exc:
            self.store.mark_unknown(
                claim,
                state="create_unknown",
                error_class=_error_code(exc),
            )
            raise ManagedSellerError("managed_seller_create_result_unknown") from None
        bound = self.store.mark_bound(
            claim,
            observed,
            api_key_fingerprint=fingerprint,
            key_secret_ref=secret_ref,
        )
        return _result(
            request,
            observed,
            "create",
            True,
            True,
            bound.control_state,
            mode="applied",
        )

    def _ensure_secret_receipt(
        self,
        binding: ManagedSellerBinding,
        remote: ManagedSellerRemote,
    ) -> ManagedSellerBinding:
        if binding.key_secret_ref and binding.api_key_fingerprint:
            return binding
        raise ManagedSellerError("managed_seller_secret_receipt_missing")

    def _update_delta(
        self,
        request: ManagedSellerRequest,
        binding: ManagedSellerBinding,
        remote: ManagedSellerRemote,
        desired: Mapping[str, Any],
        fingerprint: str,
        *,
        apply: bool,
    ) -> ManagedSellerResult:
        if not apply:
            return _result(
                request,
                remote,
                "update_delta",
                False,
                False,
                binding.control_state,
                mode="dry-run",
            )
        claim = self.store.claim_update(binding, request, desired)
        if claim is None:
            raise ManagedSellerError("managed_seller_update_already_claimed")
        try:
            self.admin.set_managed_seller_delta(
                request.managed_uid,
                request.delta,
                idempotency_key=request.delta_idempotency_key,
            )
        except Exception as exc:
            self.store.mark_unknown(
                claim,
                state="update_unknown",
                error_class=_error_code(exc),
            )
            raise ManagedSellerError("managed_seller_update_result_unknown") from None
        observed = _read_remote(self.admin, request.managed_uid)
        if observed is None or observed.delta != request.delta:
            self.store.mark_unknown(
                claim,
                state="update_unknown",
                error_class="managed_seller_update_readback_mismatch",
            )
            raise ManagedSellerError("managed_seller_update_result_unknown")
        _verify_remote_identity(observed, request, binding)
        bound = self.store.mark_bound(
            claim,
            observed,
            api_key_fingerprint=fingerprint,
            key_secret_ref=binding.key_secret_ref,
        )
        return _result(
            request,
            observed,
            "update_delta",
            True,
            True,
            bound.control_state,
            mode="applied",
        )


def _read_remote(
    admin: EnodotManagedSellerAdmin,
    managed_uid: str,
) -> ManagedSellerRemote | None:
    try:
        document = admin.get_managed_seller(managed_uid)
    except Exception as exc:
        raise ManagedSellerError(_error_code(exc)) from None
    return _parse_remote(document) if document is not None else None


def _parse_remote(
    document: Mapping[str, Any],
    *,
    require_api_key: bool = False,
) -> ManagedSellerRemote:
    if not isinstance(document, Mapping):
        raise ManagedSellerError("managed_seller_response_invalid")
    try:
        managed_uid = str(document["managed_uid"])
        user_id = _positive_int(document["user_id"], "managed_seller_response_invalid")
        api_key_id = _positive_int(document["api_key_id"], "managed_seller_response_invalid")
        group_id = _positive_int(document["group_id"], "managed_seller_response_invalid")
        delta = _delta(document["delta"])
        initial_working_credit_usd = _exact_initial_working_credit(
            document["initial_working_credit_usd"]
        )
        status = str(document["status"]).strip()
    except (KeyError, TypeError, ValueError, InvalidOperation):
        raise ManagedSellerError("managed_seller_response_invalid") from None
    if not _MANAGED_UID.fullmatch(managed_uid) or not status or len(status) > 64:
        raise ManagedSellerError("managed_seller_response_invalid")
    api_key = str(document.get("api_key") or "").strip()
    if require_api_key and not api_key:
        raise ManagedSellerError("managed_seller_create_key_missing")
    return ManagedSellerRemote(
        managed_uid=managed_uid,
        user_id=user_id,
        api_key_id=api_key_id,
        group_id=group_id,
        delta=delta,
        initial_working_credit_usd=initial_working_credit_usd,
        status=status,
        api_key=api_key,
    )


def _exact_initial_working_credit(value: Any) -> Decimal:
    if isinstance(value, bool):
        raise ManagedSellerError("managed_seller_response_invalid")
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ManagedSellerError("managed_seller_response_invalid") from None
    if not amount.is_finite() or amount != _INITIAL_WORKING_CREDIT_USD:
        raise ManagedSellerError("managed_seller_response_invalid")
    return _INITIAL_WORKING_CREDIT_USD


def _parse_capability(document: Mapping[str, Any]) -> None:
    if not isinstance(document, Mapping):
        raise ManagedSellerError("managed_seller_capability_invalid")
    expected = {
        "capability": "a6_trader_managed_sellers",
        "contract_version": "1.0",
        "create_method": "PUT",
        "create_secret_delivery": "first_response_only",
        "replay_returns_api_key": False,
        "read_returns_api_key": False,
        "delta_min": 0,
        "delta_max": 0.5,
        "rate_multiplier_base": 1,
    }
    if dict(document) != expected:
        raise ManagedSellerError("managed_seller_capability_invalid")


def _validate_context(
    context: ManagedSellerContext,
    request: ManagedSellerRequest,
) -> None:
    roles = tuple(context.roles)
    valid_roles = roles == ("seller",) or (
        len(roles) == 2 and roles[0] == "seller" and roles[1] in _SELLER_DUTIES
    )
    if (
        context.merchant_id != request.merchant_id
        or context.a6_user_id is None
        or context.a6_user_id < 1
        or context.supplier_id is None
        or context.supplier_id < 1
        or context.merchant_status != _SELLER_STATUS
        or not valid_roles
        or not context.session_ref
    ):
        raise ManagedSellerError("managed_seller_context_invalid")


def _validate_binding(
    binding: ManagedSellerBinding,
    request: ManagedSellerRequest,
) -> None:
    if (
        binding.merchant_id != request.merchant_id
        or binding.managed_uid != request.managed_uid
        or binding.group_id != request.group_id
    ):
        raise ManagedSellerError("managed_seller_binding_conflict")


def _verify_remote_identity(
    remote: ManagedSellerRemote,
    request: ManagedSellerRequest,
    binding: ManagedSellerBinding | None,
) -> None:
    if remote.managed_uid != request.managed_uid or remote.group_id != request.group_id:
        raise ManagedSellerError("managed_seller_remote_identity_mismatch")
    if binding is None:
        return
    if binding.enodot_user_id is not None and binding.enodot_user_id != remote.user_id:
        raise ManagedSellerError("managed_seller_remote_user_mismatch")
    if binding.enodot_api_key_id is not None and binding.enodot_api_key_id != remote.api_key_id:
        raise ManagedSellerError("managed_seller_remote_key_mismatch")


def _binding_fingerprint(
    binding: ManagedSellerBinding,
    secret_sink: ManagedSellerSecretSink,
) -> str:
    if not binding.key_secret_ref or not _FINGERPRINT.fullmatch(binding.api_key_fingerprint):
        raise ManagedSellerError("managed_seller_secret_receipt_missing")
    observed = str(secret_sink.get_api_key_fingerprint(binding.key_secret_ref) or "")
    if observed != binding.api_key_fingerprint:
        raise ManagedSellerError("managed_seller_secret_fingerprint_mismatch")
    return observed


def _validate_secret_receipt(secret_ref: str, fingerprint: str, api_key: str) -> None:
    expected = hashlib.sha256(api_key.encode()).hexdigest()
    if not secret_ref or not _FINGERPRINT.fullmatch(fingerprint) or fingerprint != expected:
        raise ManagedSellerError("managed_seller_secret_write_unconfirmed")


def _validate_secret_receipt_fields(fingerprint: str, secret_ref: str) -> None:
    if not _FINGERPRINT.fullmatch(str(fingerprint)) or not str(secret_ref):
        raise ManagedSellerError("managed_seller_secret_receipt_invalid")


def _desired_state(
    context: ManagedSellerContext,
    request: ManagedSellerRequest,
) -> dict[str, Any]:
    return {
        "managed_uid": request.managed_uid,
        "merchant_id": context.merchant_id,
        "a6_user_id": context.a6_user_id,
        "supplier_id": context.supplier_id,
        "group_id": request.group_id,
        "delta": _decimal_text(request.delta),
    }


def _result(
    request: ManagedSellerRequest,
    remote: ManagedSellerRemote,
    action: str,
    applied: bool,
    verified: bool,
    state: str,
    *,
    mode: str,
) -> ManagedSellerResult:
    return ManagedSellerResult(
        mode=mode,
        merchant_id=request.merchant_id,
        managed_uid=request.managed_uid,
        action=action,
        applied=applied,
        verified=verified,
        state=state,
        enodot_user_id=remote.user_id,
        enodot_api_key_id=remote.api_key_id,
        delta=remote.delta,
        preflight_verified=True,
    )


def _positive_int(value: Any, code: str) -> int:
    if isinstance(value, bool):
        raise ManagedSellerError(code)
    try:
        result = int(value)
    except (TypeError, ValueError):
        raise ManagedSellerError(code) from None
    if result < 1:
        raise ManagedSellerError(code)
    return result


def _secret_value(value: Any) -> str:
    result = str(value or "").strip()
    if len(result) < 16 or len(result) > 4096 or any(character.isspace() for character in result):
        raise ManagedSellerError("managed_seller_secret_invalid")
    return result


def _delta(value: Any) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float) and not math.isfinite(value):
        raise ManagedSellerError("managed_seller_delta_invalid")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ManagedSellerError("managed_seller_delta_invalid") from None
    if not result.is_finite() or result < 0 or result > Decimal("0.5"):
        raise ManagedSellerError("managed_seller_delta_invalid")
    return result.quantize(Decimal("0.000001"))


def _decimal_text(value: Decimal) -> str:
    return format(value, "f")


def _error_code(exc: Exception) -> str:
    code = str(getattr(exc, "code", "") or "").strip()
    if _MANAGED_UID.fullmatch(code):
        return code
    name = type(exc).__name__.lower()
    return f"managed_seller_{name}"


_SELLER_CONTEXT_QUERY = """
    SELECT id AS merchant_id, a6_user_id, supplier_id,
           status AS merchant_status, roles, session_ref
    FROM a6_trade.a6_merchants
    WHERE id = %s
"""

_SELLERS_QUERY = """
    SELECT merchant.id AS merchant_id
    FROM a6_trade.a6_merchants merchant
    WHERE merchant.roles[1] = 'seller'
      AND merchant.last_poll_at IS NOT NULL
      AND NOT EXISTS (
          SELECT 1
          FROM a6_trade.a6_seller_retirement_plans retirement
          WHERE retirement.merchant_id = merchant.id
            AND retirement.state = 'completed'
      )
    ORDER BY merchant.id
"""

_BOUND_SELLERS_QUERY = """
    SELECT merchant.id AS merchant_id
    FROM a6_trade.a6_merchants merchant
    JOIN a6_trade.enodot_managed_seller_resources seller
      ON seller.merchant_id = merchant.id
     AND seller.control_state = 'bound'
    WHERE merchant.roles[1] = 'seller'
      AND merchant.last_poll_at IS NOT NULL
      AND NOT EXISTS (
          SELECT 1
          FROM a6_trade.a6_seller_retirement_plans retirement
          WHERE retirement.merchant_id = merchant.id
            AND retirement.state = 'completed'
      )
    ORDER BY merchant.id
"""

_SELLER_BINDING_QUERY = """
    SELECT id, merchant_id, managed_uid, group_id, desired_delta,
           control_state, enodot_user_id, enodot_api_key_id,
           coalesce(api_key_fingerprint, '') AS api_key_fingerprint,
           coalesce(key_secret_ref, '') AS key_secret_ref
    FROM a6_trade.enodot_managed_seller_resources
    WHERE merchant_id = %s
"""


def _context_from_row(row: Mapping[str, Any]) -> ManagedSellerContext:
    try:
        return ManagedSellerContext(
            merchant_id=int(row["merchant_id"]),
            a6_user_id=_context_identity(row["a6_user_id"]),
            supplier_id=_context_identity(row["supplier_id"]),
            merchant_status=str(row["merchant_status"]),
            roles=tuple(str(role) for role in row["roles"]),
            session_ref=str(row["session_ref"]),
        )
    except (KeyError, TypeError, ValueError):
        raise ManagedSellerError("managed_seller_context_invalid") from None


def _binding_from_row(row: Mapping[str, Any]) -> ManagedSellerBinding:
    try:
        return ManagedSellerBinding(
            binding_id=int(row["id"]),
            merchant_id=int(row["merchant_id"]),
            managed_uid=str(row["managed_uid"]),
            group_id=int(row["group_id"]),
            desired_delta=_delta(row["desired_delta"]),
            control_state=str(row["control_state"]),
            enodot_user_id=_optional_positive_int(row.get("enodot_user_id")),
            enodot_api_key_id=_optional_positive_int(row.get("enodot_api_key_id")),
            api_key_fingerprint=str(row.get("api_key_fingerprint") or ""),
            key_secret_ref=str(row.get("key_secret_ref") or ""),
        )
    except (KeyError, TypeError, ValueError, InvalidOperation):
        raise ManagedSellerError("managed_seller_binding_invalid") from None


def _binding_snapshot(binding: ManagedSellerBinding | None) -> dict[str, Any]:
    if binding is None:
        return {}
    return {
        "binding_id": binding.binding_id,
        "merchant_id": binding.merchant_id,
        "managed_uid": binding.managed_uid,
        "group_id": binding.group_id,
        "desired_delta": _decimal_text(binding.desired_delta),
        "control_state": binding.control_state,
        "enodot_user_id": binding.enodot_user_id,
        "enodot_api_key_id": binding.enodot_api_key_id,
        "api_key_fingerprint": binding.api_key_fingerprint or None,
        "key_secret_ref": binding.key_secret_ref or None,
    }


def _row(cursor: Any) -> dict[str, Any] | None:
    raw = cursor.fetchone()
    if raw is None:
        return None
    if isinstance(raw, Mapping):
        return dict(raw)
    names = [getattr(column, "name", column[0]) for column in cursor.description]
    return dict(zip(names, raw, strict=True))


def _optional_positive_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = int(value)
    except (TypeError, ValueError):
        return None
    return result if result > 0 else None


def _context_identity(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ManagedSellerError("managed_seller_context_invalid")
    try:
        result = int(value)
    except (TypeError, ValueError):
        raise ManagedSellerError("managed_seller_context_invalid") from None
    if result < 1:
        raise ManagedSellerError("managed_seller_context_invalid")
    return result


def _json(value: Mapping[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _close(connection: Any) -> None:
    close = getattr(connection, "close", None)
    if callable(close):
        close()
