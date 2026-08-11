from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from typing import Any, Protocol

from .trade_provider_binding import (
    ProviderBindingContext,
    ProviderBindingError,
    ProviderBindingStore,
)

_SAFE_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")


class ProviderRebalanceError(RuntimeError):
    """Stable provider-rebalance error that excludes remote account credentials."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class ProviderRetirementRequest:
    merchant_id: int
    external_a6_token_id: int
    enodot_account_id: int
    group_id: int = 26
    config_version: str = "phase1-provider-rebalance-m19-m7-v1"
    requested_by: str = "a6-trader-provider-rebalance"

    def __post_init__(self) -> None:
        for value, code in (
            (self.merchant_id, "provider_retirement_merchant_id_invalid"),
            (self.external_a6_token_id, "provider_retirement_token_id_invalid"),
            (self.enodot_account_id, "provider_retirement_account_id_invalid"),
            (self.group_id, "provider_retirement_group_id_invalid"),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ProviderRebalanceError(code)
        for value, code in (
            (self.config_version, "provider_retirement_config_version_invalid"),
            (self.requested_by, "provider_retirement_requested_by_invalid"),
        ):
            if not _SAFE_LABEL.fullmatch(str(value)):
                raise ProviderRebalanceError(code)

    @property
    def binding_uid(self) -> str:
        return f"a6:merchant-{self.merchant_id}:token-{self.external_a6_token_id}"

    @property
    def idempotency_key(self) -> str:
        return (
            f"retire-provider:{self.enodot_account_id}:group-{self.group_id}:{self.config_version}"
        )


@dataclass(frozen=True)
class ProviderRetirementBinding:
    binding_id: int
    merchant_id: int
    external_a6_token_id: int
    binding_uid: str
    enodot_account_id: int
    state: str


@dataclass(frozen=True)
class ProviderRetirementPreparation:
    binding_id: int
    action_id: int
    attempt_no: int


@dataclass(frozen=True)
class ProviderRetirementResult:
    mode: str
    account_id: int
    binding_uid: str
    action: str
    changed_fields: tuple[str, ...]
    binding_state_before: str
    binding_state_after: str
    applied: bool
    verified: bool

    def summary(self) -> dict[str, Any]:
        return {
            "success": True,
            "mode": self.mode,
            "account_id": self.account_id,
            "binding_uid": self.binding_uid,
            "action": self.action,
            "changed_fields": list(self.changed_fields),
            "binding_state_before": self.binding_state_before,
            "binding_state_after": self.binding_state_after,
            "applied": self.applied,
            "verified": self.verified,
        }


class ProviderRetirementStore(Protocol):
    def load_binding(
        self,
        request: ProviderRetirementRequest,
    ) -> ProviderRetirementBinding: ...

    def prepare_retirement(
        self,
        binding: ProviderRetirementBinding,
        request: ProviderRetirementRequest,
        *,
        desired_state: Mapping[str, Any],
        observed_before: Mapping[str, Any],
    ) -> ProviderRetirementPreparation: ...

    def complete_retirement(
        self,
        preparation: ProviderRetirementPreparation,
        request: ProviderRetirementRequest,
        *,
        observed_after: Mapping[str, Any],
        elapsed_ms: int,
    ) -> None: ...

    def fail_retirement(
        self,
        preparation: ProviderRetirementPreparation,
        *,
        error_class: str,
        error_fingerprint: str,
        elapsed_ms: int,
    ) -> None: ...


class EnodotRetirementAdmin(Protocol):
    def get_account(self, account_id: int) -> Mapping[str, Any]: ...

    def update_account(
        self,
        account_id: int,
        payload: Mapping[str, Any],
    ) -> Mapping[str, Any]: ...

    def set_schedulable(
        self,
        account_id: int,
        schedulable: bool,
    ) -> Mapping[str, Any]: ...


class ProviderAccountRetirementExecutor:
    def __init__(
        self,
        store: ProviderRetirementStore,
        admin: EnodotRetirementAdmin,
        *,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.store = store
        self.admin = admin
        self.monotonic = monotonic

    def execute(
        self,
        request: ProviderRetirementRequest,
        *,
        apply: bool = False,
    ) -> ProviderRetirementResult:
        if not isinstance(apply, bool):
            raise ProviderRebalanceError("provider_retirement_apply_invalid")
        started = self.monotonic()
        preparation: ProviderRetirementPreparation | None = None
        try:
            binding = self.store.load_binding(request)
            before = _account_snapshot(self.admin.get_account(request.enodot_account_id), request)
            desired_groups = tuple(
                group_id for group_id in before["group_ids"] if group_id != request.group_id
            )
            changed_fields = tuple(
                field
                for required, field in (
                    (before["schedulable"] is not False, "schedulable"),
                    (request.group_id in before["group_ids"], "group_ids"),
                )
                if required
            )
            binding_change = binding.state != "retired"
            action = "update" if changed_fields or binding_change else "noop"
            if not apply:
                return ProviderRetirementResult(
                    mode="dry-run",
                    account_id=request.enodot_account_id,
                    binding_uid=request.binding_uid,
                    action=action,
                    changed_fields=changed_fields,
                    binding_state_before=binding.state,
                    binding_state_after="retired",
                    applied=False,
                    verified=False,
                )
            if action == "noop":
                return ProviderRetirementResult(
                    mode="applied",
                    account_id=request.enodot_account_id,
                    binding_uid=request.binding_uid,
                    action="noop",
                    changed_fields=(),
                    binding_state_before=binding.state,
                    binding_state_after=binding.state,
                    applied=False,
                    verified=True,
                )

            desired_state = {
                "account_id": request.enodot_account_id,
                "binding_uid": request.binding_uid,
                "excluded_group_id": request.group_id,
                "group_ids": list(desired_groups),
                "schedulable": False,
                "binding_state": "retired",
            }
            preparation = self.store.prepare_retirement(
                binding,
                request,
                desired_state=desired_state,
                observed_before=before,
            )

            # Stop scheduling before changing group membership so a partially completed
            # retry never sends new traffic through the retiring Provider.
            if before["schedulable"] is not False:
                self.admin.set_schedulable(request.enodot_account_id, False)
                current = _account_snapshot(
                    self.admin.get_account(request.enodot_account_id),
                    request,
                )
                if current["schedulable"] is not False:
                    raise ProviderRebalanceError("provider_retirement_schedulable_readback_failed")
            else:
                current = before

            current_groups = tuple(current["group_ids"])
            if request.group_id in current_groups:
                self.admin.update_account(
                    request.enodot_account_id,
                    {
                        "group_ids": [
                            group_id for group_id in current_groups if group_id != request.group_id
                        ]
                    },
                )
            after = _account_snapshot(
                self.admin.get_account(request.enodot_account_id),
                request,
            )
            if after["schedulable"] is not False:
                raise ProviderRebalanceError("provider_retirement_schedulable_readback_failed")
            if request.group_id in after["group_ids"]:
                raise ProviderRebalanceError("provider_retirement_group_readback_failed")

            self.store.complete_retirement(
                preparation,
                request,
                observed_after={**after, "binding_state": "retired"},
                elapsed_ms=_elapsed_ms(started, self.monotonic()),
            )
            preparation = None
            verified_binding = self.store.load_binding(request)
            if verified_binding.state != "retired":
                raise ProviderRebalanceError("provider_retirement_binding_readback_failed")
            return ProviderRetirementResult(
                mode="applied",
                account_id=request.enodot_account_id,
                binding_uid=request.binding_uid,
                action=action,
                changed_fields=changed_fields,
                binding_state_before=binding.state,
                binding_state_after=verified_binding.state,
                applied=True,
                verified=True,
            )
        except Exception as exc:
            code = _error_code(exc)
            if preparation is not None:
                self.store.fail_retirement(
                    preparation,
                    error_class=code,
                    error_fingerprint=hashlib.sha256(code.encode()).hexdigest(),
                    elapsed_ms=_elapsed_ms(started, self.monotonic()),
                )
            raise ProviderRebalanceError(code) from None


class PostgresProviderRetirementStore:
    def __init__(self, connection_factory: Callable[[], Any]) -> None:
        self.connection_factory = connection_factory

    def load_binding(
        self,
        request: ProviderRetirementRequest,
    ) -> ProviderRetirementBinding:
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(_RETIREMENT_BINDING_QUERY, (request.enodot_account_id,))
                row = _row(cursor)
            return _binding_from_row(row, request)
        finally:
            _close(connection)

    def prepare_retirement(
        self,
        binding: ProviderRetirementBinding,
        request: ProviderRetirementRequest,
        *,
        desired_state: Mapping[str, Any],
        observed_before: Mapping[str, Any],
    ) -> ProviderRetirementPreparation:
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    _RETIREMENT_BINDING_QUERY + " FOR UPDATE",
                    (request.enodot_account_id,),
                )
                locked = _binding_from_row(_row(cursor), request)
                if locked != binding:
                    raise ProviderRebalanceError("provider_retirement_binding_changed")
                cursor.execute(
                    """
                    INSERT INTO a6_trade.trade_control_actions (
                        idempotency_key, action_type, entity_type, entity_id,
                        desired_state, observed_before, status, config_version,
                        requested_by, started_at
                    ) VALUES (
                        %s, 'retire_provider_account', 'enodot_account', %s,
                        %s::jsonb, %s::jsonb, 'started', %s, %s, now()
                    )
                    ON CONFLICT (idempotency_key) DO UPDATE SET
                        desired_state = EXCLUDED.desired_state,
                        observed_before = EXCLUDED.observed_before,
                        status = 'started', error_class = NULL,
                        started_at = now(), completed_at = NULL, updated_at = now()
                    RETURNING id
                    """,
                    (
                        request.idempotency_key,
                        str(request.enodot_account_id),
                        _json(desired_state),
                        _json(observed_before),
                        request.config_version,
                        request.requested_by,
                    ),
                )
                action = _row(cursor)
                if action is None:
                    raise ProviderRebalanceError("provider_retirement_audit_write_failed")
                action_id = int(action["id"])
                cursor.execute(
                    """
                    SELECT coalesce(max(attempt_no), 0) + 1 AS attempt_no
                    FROM a6_trade.trade_control_action_attempts
                    WHERE action_id = %s
                    """,
                    (action_id,),
                )
                attempt = _row(cursor)
                attempt_no = int((attempt or {}).get("attempt_no") or 1)
                cursor.execute(
                    """
                    INSERT INTO a6_trade.trade_control_action_attempts (
                        action_id, attempt_no, status, observed_before, started_at
                    ) VALUES (%s, %s, 'started', %s::jsonb, now())
                    """,
                    (action_id, attempt_no, _json(observed_before)),
                )
            connection.commit()
            return ProviderRetirementPreparation(binding.binding_id, action_id, attempt_no)
        except Exception:
            connection.rollback()
            raise
        finally:
            _close(connection)

    def complete_retirement(
        self,
        preparation: ProviderRetirementPreparation,
        request: ProviderRetirementRequest,
        *,
        observed_after: Mapping[str, Any],
        elapsed_ms: int,
    ) -> None:
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE a6_trade.enodot_account_bindings
                    SET state = 'retired', cooldown_until = NULL,
                        last_error_class = NULL, updated_at = now()
                    WHERE id = %s AND binding_uid = %s AND enodot_account_id = %s
                    RETURNING id
                    """,
                    (
                        preparation.binding_id,
                        request.binding_uid,
                        request.enodot_account_id,
                    ),
                )
                if cursor.fetchone() is None:
                    raise ProviderRebalanceError("provider_retirement_binding_update_failed")
                cursor.execute(
                    """
                    UPDATE a6_trade.trade_control_actions
                    SET observed_after = %s::jsonb, status = 'completed',
                        error_class = NULL, completed_at = now(), elapsed_ms = %s,
                        updated_at = now()
                    WHERE id = %s
                    """,
                    (_json(observed_after), elapsed_ms, preparation.action_id),
                )
                cursor.execute(
                    """
                    UPDATE a6_trade.trade_control_action_attempts
                    SET observed_after = %s::jsonb, status = 'completed',
                        completed_at = now(), elapsed_ms = %s
                    WHERE action_id = %s AND attempt_no = %s
                    """,
                    (
                        _json(observed_after),
                        elapsed_ms,
                        preparation.action_id,
                        preparation.attempt_no,
                    ),
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            _close(connection)

    def fail_retirement(
        self,
        preparation: ProviderRetirementPreparation,
        *,
        error_class: str,
        error_fingerprint: str,
        elapsed_ms: int,
    ) -> None:
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE a6_trade.trade_control_actions
                    SET status = 'failed', error_class = %s,
                        completed_at = now(), elapsed_ms = %s, updated_at = now()
                    WHERE id = %s
                    """,
                    (error_class, elapsed_ms, preparation.action_id),
                )
                cursor.execute(
                    """
                    UPDATE a6_trade.trade_control_action_attempts
                    SET status = 'failed', error_class = %s, error_fingerprint = %s,
                        completed_at = now(), elapsed_ms = %s
                    WHERE action_id = %s AND attempt_no = %s
                    """,
                    (
                        error_class,
                        error_fingerprint,
                        elapsed_ms,
                        preparation.action_id,
                        preparation.attempt_no,
                    ),
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            _close(connection)


class ProjectedProviderRoleStore:
    """Expose the post-switch role during a binding dry-run without persisting it."""

    def __init__(self, delegate: ProviderBindingStore, merchant_id: int) -> None:
        self.delegate = delegate
        self.merchant_id = merchant_id

    def load_context(
        self,
        merchant_id: int,
        external_a6_token_id: int,
    ) -> ProviderBindingContext:
        context = self.delegate.load_context(merchant_id, external_a6_token_id)
        if context.merchant_id != self.merchant_id:
            raise ProviderBindingError("provider_binding_projection_merchant_mismatch")
        if context.roles not in {("seller",), ("seller", "provider")}:
            raise ProviderBindingError("provider_binding_projection_role_invalid")
        return replace(context, roles=("seller", "provider"))

    def prepare_binding(self, *_args: Any, **_kwargs: Any) -> Any:
        raise ProviderBindingError("provider_binding_projection_write_forbidden")

    def complete_binding(self, *_args: Any, **_kwargs: Any) -> None:
        raise ProviderBindingError("provider_binding_projection_write_forbidden")

    def fail_binding(self, *_args: Any, **_kwargs: Any) -> None:
        raise ProviderBindingError("provider_binding_projection_write_forbidden")


def load_merchant_roles(
    connection_factory: Callable[[], Any],
    merchant_ids: tuple[int, ...],
) -> dict[int, tuple[str, ...]]:
    connection = connection_factory()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT id, roles
                FROM a6_trade.a6_merchants
                WHERE id = ANY(%s)
                """,
                (list(merchant_ids),),
            )
            rows = _rows(cursor)
        result = {int(row["id"]): tuple(str(role) for role in row["roles"]) for row in rows}
        if set(result) != set(merchant_ids):
            raise ProviderRebalanceError("provider_rebalance_merchant_missing")
        return result
    finally:
        _close(connection)


def assert_role_switch_readback(
    roles: Mapping[int, tuple[str, ...]],
    *,
    outgoing_merchant_id: int,
    incoming_merchant_id: int,
) -> None:
    if roles.get(outgoing_merchant_id) != ("seller",):
        raise ProviderRebalanceError("provider_rebalance_outgoing_role_readback_failed")
    if roles.get(incoming_merchant_id) != ("seller", "provider"):
        raise ProviderRebalanceError("provider_rebalance_incoming_role_readback_failed")


_RETIREMENT_BINDING_QUERY = """
    SELECT b.id AS binding_id, b.binding_uid, b.enodot_account_id, b.state,
           m.id AS merchant_id, t.a6_token_id AS external_a6_token_id
    FROM a6_trade.enodot_account_bindings b
    JOIN a6_trade.a6_tokens t ON t.id = b.a6_token_id
    JOIN a6_trade.a6_merchants m ON m.id = t.merchant_id
    WHERE b.enodot_account_id = %s
"""


def _binding_from_row(
    row: Mapping[str, Any] | None,
    request: ProviderRetirementRequest,
) -> ProviderRetirementBinding:
    if row is None:
        raise ProviderRebalanceError("provider_retirement_binding_missing")
    try:
        binding = ProviderRetirementBinding(
            binding_id=int(row["binding_id"]),
            merchant_id=int(row["merchant_id"]),
            external_a6_token_id=int(row["external_a6_token_id"]),
            binding_uid=str(row["binding_uid"]),
            enodot_account_id=int(row["enodot_account_id"]),
            state=str(row["state"]),
        )
    except (KeyError, TypeError, ValueError):
        raise ProviderRebalanceError("provider_retirement_binding_invalid") from None
    if (
        binding.merchant_id != request.merchant_id
        or binding.external_a6_token_id != request.external_a6_token_id
        or binding.binding_uid != request.binding_uid
        or binding.enodot_account_id != request.enodot_account_id
    ):
        raise ProviderRebalanceError("provider_retirement_binding_mismatch")
    if binding.state not in {"bound", "retired"}:
        raise ProviderRebalanceError("provider_retirement_binding_state_invalid")
    return binding


def _account_snapshot(
    account: Mapping[str, Any],
    request: ProviderRetirementRequest,
) -> dict[str, Any]:
    if not isinstance(account, Mapping):
        raise ProviderRebalanceError("provider_retirement_account_invalid")
    extra = account.get("extra")
    if not isinstance(extra, Mapping):
        raise ProviderRebalanceError("provider_retirement_account_identity_missing")
    try:
        account_id = int(account.get("id"))
        group_ids = tuple(int(group_id) for group_id in (account.get("group_ids") or []))
    except (TypeError, ValueError):
        raise ProviderRebalanceError("provider_retirement_account_invalid") from None
    schedulable = account.get("schedulable")
    if account_id != request.enodot_account_id:
        raise ProviderRebalanceError("provider_retirement_account_id_mismatch")
    if extra.get("a6_binding_uid") != request.binding_uid:
        raise ProviderRebalanceError("provider_retirement_account_identity_mismatch")
    if not isinstance(schedulable, bool):
        raise ProviderRebalanceError("provider_retirement_schedulable_invalid")
    if len(group_ids) != len(set(group_ids)) or any(group_id < 1 for group_id in group_ids):
        raise ProviderRebalanceError("provider_retirement_group_ids_invalid")
    return {
        "account_id": account_id,
        "binding_uid": request.binding_uid,
        "group_ids": list(group_ids),
        "schedulable": schedulable,
        "status": str(account.get("status") or ""),
    }


def _error_code(exc: Exception) -> str:
    if isinstance(exc, ProviderRebalanceError):
        return exc.code
    candidate = str(getattr(exc, "code", "") or "")
    if _SAFE_LABEL.fullmatch(candidate):
        return candidate
    name = re.sub(r"(?<!^)(?=[A-Z])", "_", type(exc).__name__).lower()
    return f"provider_retirement_{name}"


def _row(cursor: Any) -> dict[str, Any] | None:
    raw = cursor.fetchone()
    if raw is None:
        return None
    if isinstance(raw, Mapping):
        return dict(raw)
    names = [getattr(column, "name", column[0]) for column in cursor.description]
    return dict(zip(names, raw, strict=True))


def _rows(cursor: Any) -> list[dict[str, Any]]:
    raw = cursor.fetchall()
    if not raw:
        return []
    if isinstance(raw[0], Mapping):
        return [dict(row) for row in raw]
    names = [getattr(column, "name", column[0]) for column in cursor.description]
    return [dict(zip(names, row, strict=True)) for row in raw]


def _json(value: Mapping[str, Any]) -> str:
    return json.dumps(dict(value), ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _elapsed_ms(started: float, finished: float) -> int:
    return max(0, int((finished - started) * 1000))


def _close(connection: Any) -> None:
    close = getattr(connection, "close", None)
    if callable(close):
        close()
