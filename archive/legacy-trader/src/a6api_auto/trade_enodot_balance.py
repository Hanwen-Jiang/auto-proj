from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

from .enodot_admin import EnodotAdminError
from .errors import A6AutoError

USD_QUANTUM = Decimal("0.00000001")
MAX_BALANCE_USD = Decimal("1000000000000.00000000")
BALANCE_LOCK_NAMESPACE = 0x41364252
DEFAULT_CONFIG_VERSION = "managed-seller-balance-recovery-20260725-v1"
DEFAULT_REQUESTED_BY = "restore-enodot-user-balance"


class EnodotBalanceRecoveryError(A6AutoError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class EnodotBalanceRecoveryRequest:
    user_id: int
    expected_balance_usd: Decimal | str | float
    target_balance_usd: Decimal | str | float = Decimal("1.00000000")
    config_version: str = DEFAULT_CONFIG_VERSION
    requested_by: str = DEFAULT_REQUESTED_BY
    notes: str = "A6 Trader managed Seller working-credit loss recovery"

    def __post_init__(self) -> None:
        user_id = _positive_int(self.user_id, "enodot_balance_user_id_invalid")
        expected = _money(self.expected_balance_usd, "enodot_balance_expected_invalid")
        target = _money(self.target_balance_usd, "enodot_balance_target_invalid")
        config_version = str(self.config_version or "").strip()
        requested_by = str(self.requested_by or "").strip()
        notes = str(self.notes or "").strip()
        if target <= expected:
            raise EnodotBalanceRecoveryError("enodot_balance_target_not_above_expected")
        if not config_version or len(config_version) > 256:
            raise EnodotBalanceRecoveryError("enodot_balance_config_version_invalid")
        if not requested_by or len(requested_by) > 256:
            raise EnodotBalanceRecoveryError("enodot_balance_requested_by_invalid")
        if len(notes) > 1024:
            raise EnodotBalanceRecoveryError("enodot_balance_notes_invalid")
        object.__setattr__(self, "user_id", user_id)
        object.__setattr__(self, "expected_balance_usd", expected)
        object.__setattr__(self, "target_balance_usd", target)
        object.__setattr__(self, "config_version", config_version)
        object.__setattr__(self, "requested_by", requested_by)
        object.__setattr__(self, "notes", notes)

    @property
    def amount_usd(self) -> Decimal:
        return self.target_balance_usd - self.expected_balance_usd

    @property
    def idempotency_key(self) -> str:
        identity = (
            f"{self.user_id}:{_money_text(self.expected_balance_usd)}:"
            f"{_money_text(self.target_balance_usd)}:{self.config_version}"
        )
        digest = hashlib.sha256(identity.encode()).hexdigest()[:32]
        return f"a6trader-balance:{self.user_id}:{digest}"

    def desired_state(self) -> dict[str, Any]:
        return {
            "endpoint": f"/api/v1/admin/users/{self.user_id}/balance",
            "method": "POST",
            "operation": "add",
            "user_id": self.user_id,
            "expected_balance_usd": _money_text(self.expected_balance_usd),
            "amount_usd": _money_text(self.amount_usd),
            "target_balance_usd": _money_text(self.target_balance_usd),
        }


@dataclass(frozen=True)
class EnodotBalanceAuditPreparation:
    action_id: int
    attempt_no: int | None
    audit_completed: bool = False


@dataclass(frozen=True)
class EnodotBalanceRecoveryResult:
    user_id: int
    action: str
    applied: bool
    verified: bool
    balance_before_usd: Decimal
    balance_after_usd: Decimal
    amount_usd: Decimal
    action_id: int | None = None
    recovered_after_transport_error: bool = False

    def public(self) -> dict[str, Any]:
        return {
            "success": True,
            "user_id": self.user_id,
            "action": self.action,
            "applied": self.applied,
            "verified": self.verified,
            "balance_before_usd": _money_text(self.balance_before_usd),
            "balance_after_usd": _money_text(self.balance_after_usd),
            "amount_usd": _money_text(self.amount_usd),
            "action_id": self.action_id,
            "recovered_after_transport_error": self.recovered_after_transport_error,
        }


class EnodotBalanceAdmin(Protocol):
    def get_user(self, user_id: int) -> Mapping[str, Any]: ...

    def add_user_balance(
        self,
        user_id: int,
        amount: Any,
        *,
        idempotency_key: str,
        notes: str = "",
    ) -> Mapping[str, Any]: ...


class EnodotBalanceAuditStore(Protocol):
    def lock(self, user_id: int) -> Any: ...

    def start(
        self,
        request: EnodotBalanceRecoveryRequest,
        observed_before: Mapping[str, Any],
    ) -> EnodotBalanceAuditPreparation: ...

    def complete(
        self,
        preparation: EnodotBalanceAuditPreparation,
        observed_after: Mapping[str, Any],
        *,
        elapsed_ms: int,
    ) -> None: ...

    def finish_error(
        self,
        preparation: EnodotBalanceAuditPreparation,
        observed_after: Mapping[str, Any] | None,
        *,
        error_class: str,
        elapsed_ms: int,
        unknown: bool,
    ) -> None: ...


class EnodotBalanceRecoveryService:
    def __init__(self, store: EnodotBalanceAuditStore, admin: EnodotBalanceAdmin) -> None:
        self.store = store
        self.admin = admin

    def execute(
        self,
        request: EnodotBalanceRecoveryRequest,
        *,
        apply: bool = False,
    ) -> EnodotBalanceRecoveryResult:
        if not isinstance(apply, bool):
            raise EnodotBalanceRecoveryError("enodot_balance_apply_invalid")
        lock = self.store.lock(request.user_id) if apply else nullcontext()
        with lock:
            before = _user_snapshot(self.admin.get_user(request.user_id), request.user_id)
            before_balance = _money(before["balance_usd"], "enodot_balance_read_invalid")
            if not apply:
                if before_balance == request.target_balance_usd:
                    return _result(request, "noop", False, True, before_balance, None)
                _require_precondition(request, before_balance)
                return _result(request, "add", False, False, before_balance, None)

            preparation = self.store.start(request, before)
            if preparation.audit_completed:
                observed = _user_snapshot(self.admin.get_user(request.user_id), request.user_id)
                observed_balance = _money(
                    observed["balance_usd"],
                    "enodot_balance_read_invalid",
                )
                if observed_balance != request.target_balance_usd:
                    raise EnodotBalanceRecoveryError("enodot_balance_completed_action_drift")
                return _result(
                    request,
                    "replay",
                    False,
                    True,
                    before_balance,
                    preparation.action_id,
                    balance_after=observed_balance,
                )

            if before_balance == request.target_balance_usd:
                self.store.complete(preparation, before, elapsed_ms=0)
                return _result(
                    request,
                    "noop",
                    False,
                    True,
                    before_balance,
                    preparation.action_id,
                )
            try:
                _require_precondition(request, before_balance)
            except EnodotBalanceRecoveryError as exc:
                self.store.finish_error(
                    preparation,
                    before,
                    error_class=exc.code,
                    elapsed_ms=0,
                    unknown=False,
                )
                raise

            started = time.monotonic()
            try:
                self.admin.add_user_balance(
                    request.user_id,
                    request.amount_usd,
                    idempotency_key=request.idempotency_key,
                    notes=request.notes,
                )
                after = _user_snapshot(self.admin.get_user(request.user_id), request.user_id)
            except Exception as exc:
                return self._resolve_mutation_error(
                    request,
                    preparation,
                    before_balance,
                    started,
                    exc,
                )

            after_balance = _money(after["balance_usd"], "enodot_balance_read_invalid")
            elapsed_ms = _elapsed_ms(started)
            if after_balance != request.target_balance_usd:
                error_class = "enodot_balance_readback_mismatch"
                self.store.finish_error(
                    preparation,
                    after,
                    error_class=error_class,
                    elapsed_ms=elapsed_ms,
                    unknown=after_balance != request.expected_balance_usd,
                )
                raise EnodotBalanceRecoveryError(error_class)
            self.store.complete(preparation, after, elapsed_ms=elapsed_ms)
            return _result(
                request,
                "add",
                True,
                True,
                before_balance,
                preparation.action_id,
                balance_after=after_balance,
            )

    def _resolve_mutation_error(
        self,
        request: EnodotBalanceRecoveryRequest,
        preparation: EnodotBalanceAuditPreparation,
        before_balance: Decimal,
        started: float,
        cause: Exception,
    ) -> EnodotBalanceRecoveryResult:
        error_class = _error_class(cause)
        elapsed_ms = _elapsed_ms(started)
        try:
            after = _user_snapshot(self.admin.get_user(request.user_id), request.user_id)
            after_balance = _money(after["balance_usd"], "enodot_balance_read_invalid")
        except Exception:
            self.store.finish_error(
                preparation,
                None,
                error_class=error_class,
                elapsed_ms=elapsed_ms,
                unknown=True,
            )
            raise EnodotBalanceRecoveryError(error_class) from None
        if after_balance == request.target_balance_usd:
            self.store.complete(preparation, after, elapsed_ms=elapsed_ms)
            return _result(
                request,
                "add",
                True,
                True,
                before_balance,
                preparation.action_id,
                balance_after=after_balance,
                recovered_after_transport_error=True,
            )
        self.store.finish_error(
            preparation,
            after,
            error_class=error_class,
            elapsed_ms=elapsed_ms,
            unknown=after_balance != request.expected_balance_usd,
        )
        raise EnodotBalanceRecoveryError(error_class) from None


class PostgresEnodotBalanceAuditStore:
    def __init__(self, connection_factory: Callable[[], Any]) -> None:
        self.connection_factory = connection_factory

    @contextmanager
    def lock(self, user_id: int) -> Iterator[None]:
        user_id = _positive_int(user_id, "enodot_balance_user_id_invalid")
        connection = self.connection_factory()
        locked = False
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT pg_advisory_lock(%s, %s)",
                    (BALANCE_LOCK_NAMESPACE, user_id),
                )
            connection.commit()
            locked = True
        except Exception:
            _close(connection)
            raise EnodotBalanceRecoveryError("enodot_balance_audit_lock_failed") from None
        try:
            yield
        finally:
            if locked:
                try:
                    with connection.cursor() as cursor:
                        cursor.execute(
                            "SELECT pg_advisory_unlock(%s, %s)",
                            (BALANCE_LOCK_NAMESPACE, user_id),
                        )
                    connection.commit()
                except Exception:
                    connection.rollback()
            _close(connection)

    def start(
        self,
        request: EnodotBalanceRecoveryRequest,
        observed_before: Mapping[str, Any],
    ) -> EnodotBalanceAuditPreparation:
        connection = self.connection_factory()
        desired = request.desired_state()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO a6_trade.trade_control_actions (
                        idempotency_key, action_type, entity_type, entity_id,
                        desired_state, observed_before, status, config_version,
                        requested_by, started_at
                    ) VALUES (
                        %s, 'restore_enodot_user_balance', 'enodot_user', %s,
                        %s::jsonb, %s::jsonb, 'started', %s, %s, now()
                    )
                    ON CONFLICT (idempotency_key) DO UPDATE SET updated_at = now()
                    RETURNING id, status, desired_state
                    """,
                    (
                        request.idempotency_key,
                        str(request.user_id),
                        _json(desired),
                        _json(observed_before),
                        request.config_version,
                        request.requested_by,
                    ),
                )
                action = _required_row(cursor, "enodot_balance_audit_start_failed")
                action_id = _positive_int(
                    action.get("id"),
                    "enodot_balance_audit_start_failed",
                )
                if dict(action.get("desired_state") or {}) != desired:
                    raise EnodotBalanceRecoveryError("enodot_balance_audit_conflict")
                if str(action.get("status")) == "completed":
                    connection.commit()
                    return EnodotBalanceAuditPreparation(action_id, None, True)
                cursor.execute(
                    """
                    UPDATE a6_trade.trade_control_actions
                    SET status = 'started', error_class = NULL,
                        started_at = coalesce(started_at, now()), completed_at = NULL,
                        elapsed_ms = NULL, updated_at = now()
                    WHERE id = %s
                    """,
                    (action_id,),
                )
                cursor.execute(
                    """
                    SELECT coalesce(max(attempt_no), 0) + 1 AS attempt_no
                    FROM a6_trade.trade_control_action_attempts WHERE action_id = %s
                    """,
                    (action_id,),
                )
                attempt = _required_row(cursor, "enodot_balance_audit_start_failed")
                attempt_no = _positive_int(
                    attempt.get("attempt_no"),
                    "enodot_balance_audit_start_failed",
                )
                cursor.execute(
                    """
                    INSERT INTO a6_trade.trade_control_action_attempts (
                        action_id, attempt_no, status, observed_before, started_at
                    ) VALUES (%s, %s, 'started', %s::jsonb, now())
                    """,
                    (action_id, attempt_no, _json(observed_before)),
                )
            connection.commit()
            return EnodotBalanceAuditPreparation(action_id, attempt_no)
        except EnodotBalanceRecoveryError:
            connection.rollback()
            raise
        except Exception:
            connection.rollback()
            raise EnodotBalanceRecoveryError("enodot_balance_audit_start_failed") from None
        finally:
            _close(connection)

    def complete(
        self,
        preparation: EnodotBalanceAuditPreparation,
        observed_after: Mapping[str, Any],
        *,
        elapsed_ms: int,
    ) -> None:
        self._finish(
            preparation,
            observed_after,
            status="completed",
            error_class=None,
            elapsed_ms=elapsed_ms,
        )

    def finish_error(
        self,
        preparation: EnodotBalanceAuditPreparation,
        observed_after: Mapping[str, Any] | None,
        *,
        error_class: str,
        elapsed_ms: int,
        unknown: bool,
    ) -> None:
        self._finish(
            preparation,
            observed_after,
            status="unknown" if unknown else "failed",
            error_class=error_class,
            elapsed_ms=elapsed_ms,
        )

    def _finish(
        self,
        preparation: EnodotBalanceAuditPreparation,
        observed_after: Mapping[str, Any] | None,
        *,
        status: str,
        error_class: str | None,
        elapsed_ms: int,
    ) -> None:
        if preparation.audit_completed or preparation.attempt_no is None:
            return
        elapsed_ms = max(int(elapsed_ms), 0)
        fingerprint = hashlib.sha256(str(error_class or "").encode()).hexdigest()
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE a6_trade.trade_control_actions
                    SET observed_after = %s::jsonb, status = %s, error_class = %s,
                        completed_at = now(), elapsed_ms = %s, updated_at = now()
                    WHERE id = %s AND status IN ('started', 'unknown')
                    """,
                    (
                        _json(observed_after) if observed_after is not None else None,
                        status,
                        error_class,
                        elapsed_ms,
                        preparation.action_id,
                    ),
                )
                cursor.execute(
                    """
                    UPDATE a6_trade.trade_control_action_attempts
                    SET observed_after = %s::jsonb, status = %s, error_class = %s,
                        error_fingerprint = %s, completed_at = now(), elapsed_ms = %s
                    WHERE action_id = %s AND attempt_no = %s AND status = 'started'
                    """,
                    (
                        _json(observed_after) if observed_after is not None else None,
                        status,
                        error_class,
                        fingerprint if error_class else None,
                        elapsed_ms,
                        preparation.action_id,
                        preparation.attempt_no,
                    ),
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise EnodotBalanceRecoveryError("enodot_balance_audit_finish_failed") from None
        finally:
            _close(connection)


def _require_precondition(
    request: EnodotBalanceRecoveryRequest,
    observed_balance: Decimal,
) -> None:
    if observed_balance != request.expected_balance_usd:
        raise EnodotBalanceRecoveryError("enodot_balance_precondition_failed")


def _user_snapshot(user: Mapping[str, Any], expected_user_id: int) -> dict[str, Any]:
    if not isinstance(user, Mapping):
        raise EnodotBalanceRecoveryError("enodot_balance_read_invalid")
    user_id = _positive_int(user.get("id"), "enodot_balance_read_invalid")
    if user_id != expected_user_id:
        raise EnodotBalanceRecoveryError("enodot_balance_read_invalid")
    balance = _money(user.get("balance"), "enodot_balance_read_invalid")
    return {
        "user_id": user_id,
        "balance_usd": _money_text(balance),
        "status": str(user.get("status") or ""),
    }


def _result(
    request: EnodotBalanceRecoveryRequest,
    action: str,
    applied: bool,
    verified: bool,
    before: Decimal,
    action_id: int | None,
    *,
    balance_after: Decimal | None = None,
    recovered_after_transport_error: bool = False,
) -> EnodotBalanceRecoveryResult:
    return EnodotBalanceRecoveryResult(
        user_id=request.user_id,
        action=action,
        applied=applied,
        verified=verified,
        balance_before_usd=before,
        balance_after_usd=balance_after if balance_after is not None else before,
        amount_usd=request.amount_usd if action == "add" else Decimal("0.00000000"),
        action_id=action_id,
        recovered_after_transport_error=recovered_after_transport_error,
    )


def _money(value: Any, error: str) -> Decimal:
    if value is None or isinstance(value, bool):
        raise EnodotBalanceRecoveryError(error)
    try:
        amount = Decimal(str(value))
        normalized = amount.quantize(USD_QUANTUM)
    except (InvalidOperation, TypeError, ValueError):
        raise EnodotBalanceRecoveryError(error) from None
    if not amount.is_finite() or amount != normalized or abs(amount) > MAX_BALANCE_USD:
        raise EnodotBalanceRecoveryError(error)
    return normalized


def _money_text(value: Decimal) -> str:
    return format(value.quantize(USD_QUANTUM), ".8f")


def _positive_int(value: Any, error: str) -> int:
    if value is None or isinstance(value, bool):
        raise EnodotBalanceRecoveryError(error)
    try:
        result = int(value)
    except (TypeError, ValueError):
        raise EnodotBalanceRecoveryError(error) from None
    if result < 1:
        raise EnodotBalanceRecoveryError(error)
    return result


def _elapsed_ms(started: float) -> int:
    return max(round((time.monotonic() - started) * 1000), 0)


def _error_class(exc: Exception) -> str:
    if isinstance(exc, EnodotBalanceRecoveryError):
        return exc.code
    if isinstance(exc, EnodotAdminError):
        return exc.code
    candidate = str(getattr(exc, "code", "") or "")
    if re.fullmatch(r"[a-z0-9_.:-]{1,96}", candidate):
        return candidate
    name = re.sub(r"(?<!^)(?=[A-Z])", "_", type(exc).__name__).lower()
    return f"enodot_balance_{name}"


def _required_row(cursor: Any, error: str) -> dict[str, Any]:
    raw = cursor.fetchone()
    if raw is None:
        raise EnodotBalanceRecoveryError(error)
    if isinstance(raw, Mapping):
        return dict(raw)
    names = [getattr(column, "name", column[0]) for column in cursor.description]
    return dict(zip(names, raw, strict=True))


def _json(value: Mapping[str, Any]) -> str:
    return json.dumps(dict(value), ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _close(connection: Any) -> None:
    close = getattr(connection, "close", None)
    if callable(close):
        close()
