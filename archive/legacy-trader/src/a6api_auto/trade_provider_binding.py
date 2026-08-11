from __future__ import annotations

import hashlib
import json
import math
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import MappingProxyType
from typing import Any, Protocol
from urllib.parse import urlsplit

from .models import SessionRecord
from .storage import SessionStore

_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_SAFE_MODEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,95}$")
_ACTIVE_TOKEN_STATES = frozenset({"1", "active", "enabled"})
_PROVIDER_STATUS = "supplier_submitted"
_A6_UPSTREAM_BASE_URL = "https://a6.a6api.com/v1"
_A6_RELAY_ORIGIN = "http://127.0.0.1:8767"
MAX_PROVIDER_PRIORITY = 32767


class ProviderBindingError(RuntimeError):
    """Stable Provider binding error that never retains secret-bearing responses."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class ProviderBindingRequest:
    merchant_id: int
    external_a6_token_id: int
    proxy_id: int
    group_ids: tuple[int, ...] = (26,)
    model_scope: tuple[str, ...] = ("gpt-5.6-sol",)
    base_url: str = _A6_UPSTREAM_BASE_URL
    use_upstream_relay: bool = True
    model_mapping: Mapping[str, str] = field(default_factory=lambda: {"gpt-5.6-sol": "gpt-5.6-sol"})
    pool_mode: bool = True
    pool_mode_retry_count: int = 0
    pool_mode_retry_status_codes: tuple[int, ...] = ()
    concurrency: int = 100
    priority: int = 1
    rate_multiplier: float = 1.0
    config_version: str = "phase1-provider-binding-v1"
    requested_by: str = "a6-trader"

    def __post_init__(self) -> None:
        for value, code in (
            (self.merchant_id, "provider_binding_merchant_id_invalid"),
            (self.external_a6_token_id, "provider_binding_token_id_invalid"),
            (self.proxy_id, "provider_binding_proxy_id_invalid"),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ProviderBindingError(code)
        if not self.group_ids or any(
            isinstance(value, bool) or not isinstance(value, int) or value < 1
            for value in self.group_ids
        ):
            raise ProviderBindingError("provider_binding_group_ids_invalid")
        if len(set(self.group_ids)) != len(self.group_ids):
            raise ProviderBindingError("provider_binding_group_ids_invalid")
        if not self.model_scope or any(
            not _SAFE_MODEL.fullmatch(str(model)) for model in self.model_scope
        ):
            raise ProviderBindingError("provider_binding_model_scope_invalid")
        normalized_mapping = {str(key): str(value) for key, value in self.model_mapping.items()}
        if not normalized_mapping or any(
            not _SAFE_MODEL.fullmatch(key) or not _SAFE_MODEL.fullmatch(value)
            for key, value in normalized_mapping.items()
        ):
            raise ProviderBindingError("provider_binding_model_mapping_invalid")
        base_url = str(self.base_url).strip().rstrip("/")
        parsed = urlsplit(base_url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.query:
            raise ProviderBindingError("provider_binding_base_url_invalid")
        if not isinstance(self.use_upstream_relay, bool):
            raise ProviderBindingError("provider_binding_relay_mode_invalid")
        if self.use_upstream_relay and base_url != _A6_UPSTREAM_BASE_URL:
            raise ProviderBindingError("provider_binding_relay_upstream_invalid")
        if not isinstance(self.pool_mode, bool):
            raise ProviderBindingError("provider_binding_pool_mode_invalid")
        if (
            isinstance(self.pool_mode_retry_count, bool)
            or not 0 <= self.pool_mode_retry_count <= 10
        ):
            raise ProviderBindingError("provider_binding_pool_mode_retry_count_invalid")
        if any(
            isinstance(value, bool) or not isinstance(value, int) or not 100 <= value <= 599
            for value in self.pool_mode_retry_status_codes
        ):
            raise ProviderBindingError("provider_binding_pool_mode_retry_status_codes_invalid")
        if isinstance(self.concurrency, bool) or self.concurrency < 1:
            raise ProviderBindingError("provider_binding_concurrency_invalid")
        if isinstance(self.priority, bool) or not 1 <= self.priority <= MAX_PROVIDER_PRIORITY:
            raise ProviderBindingError("provider_binding_priority_invalid")
        if isinstance(self.rate_multiplier, bool):
            raise ProviderBindingError("provider_binding_rate_multiplier_invalid")
        try:
            multiplier = float(self.rate_multiplier)
        except (TypeError, ValueError):
            raise ProviderBindingError("provider_binding_rate_multiplier_invalid") from None
        if not math.isfinite(multiplier) or multiplier <= 0:
            raise ProviderBindingError("provider_binding_rate_multiplier_invalid")
        if not _SAFE_NAME.fullmatch(self.config_version):
            raise ProviderBindingError("provider_binding_config_version_invalid")
        if not _SAFE_NAME.fullmatch(self.requested_by):
            raise ProviderBindingError("provider_binding_requested_by_invalid")
        object.__setattr__(self, "base_url", base_url)
        object.__setattr__(self, "group_ids", tuple(dict.fromkeys(self.group_ids)))
        object.__setattr__(self, "model_scope", tuple(dict.fromkeys(self.model_scope)))
        object.__setattr__(self, "model_mapping", MappingProxyType(normalized_mapping))
        object.__setattr__(
            self,
            "pool_mode_retry_status_codes",
            tuple(dict.fromkeys(self.pool_mode_retry_status_codes)),
        )
        object.__setattr__(self, "rate_multiplier", multiplier)

    @property
    def binding_uid(self) -> str:
        return f"a6:merchant-{self.merchant_id}:token-{self.external_a6_token_id}"

    @property
    def account_name(self) -> str:
        return f"a6-provider-merchant-{self.merchant_id}-token-{self.external_a6_token_id}"


@dataclass(frozen=True)
class ProviderBindingContext:
    merchant_id: int
    a6_user_id: int
    supplier_id: int
    merchant_status: str
    roles: tuple[str, ...]
    session_ref: str
    proxy_lease_id: str
    token_row_id: int
    external_a6_token_id: int
    token_name: str
    token_status: str
    key_fingerprint: str = ""
    key_secret_ref: str = ""


@dataclass(frozen=True)
class BindingPreparation:
    binding_id: int
    action_id: int
    attempt_no: int | None
    existing_account_id: int | None
    existing_state: str
    audit_completed: bool = False


@dataclass(frozen=True)
class ProviderBindingResult:
    mode: str
    merchant_id: int
    external_a6_token_id: int
    binding_uid: str
    action: str
    applied: bool
    account_id: int | None
    session_updated: bool
    verified: bool

    def summary(self) -> dict[str, Any]:
        return {
            "success": True,
            "mode": self.mode,
            "merchant_id": self.merchant_id,
            "external_a6_token_id": self.external_a6_token_id,
            "binding_uid": self.binding_uid,
            "action": self.action,
            "applied": self.applied,
            "account_id": self.account_id,
            "session_updated": self.session_updated,
            "verified": self.verified,
        }


class ProviderBindingStore(Protocol):
    def load_context(
        self,
        merchant_id: int,
        external_a6_token_id: int,
    ) -> ProviderBindingContext: ...

    def prepare_binding(
        self,
        context: ProviderBindingContext,
        request: ProviderBindingRequest,
        *,
        key_fingerprint: str,
        key_secret_ref: str,
        desired_state: Mapping[str, Any],
        idempotency_key: str,
    ) -> BindingPreparation: ...

    def complete_binding(
        self,
        preparation: BindingPreparation,
        request: ProviderBindingRequest,
        *,
        account_id: int,
        observed_after: Mapping[str, Any],
        elapsed_ms: int,
    ) -> None: ...

    def fail_binding(
        self,
        preparation: BindingPreparation,
        request: ProviderBindingRequest,
        *,
        error_class: str,
        error_fingerprint: str,
        elapsed_ms: int,
    ) -> None: ...


class EnodotProviderAdmin(Protocol):
    def request(self, method: str, path: str, **kwargs: Any) -> Any: ...

    def reconcile_a6_provider_account(self, *, apply: bool = False, **provider: Any) -> Any: ...

    def create_account(self, payload: Mapping[str, Any]) -> Mapping[str, Any]: ...

    def update_account(
        self,
        account_id: int,
        payload: Mapping[str, Any],
    ) -> Mapping[str, Any]: ...

    def get_account(self, account_id: int) -> Mapping[str, Any]: ...

    def find_account_by_binding_uid(self, binding_uid: str) -> Mapping[str, Any] | None: ...

    def set_schedulable(self, account_id: int, schedulable: bool) -> Mapping[str, Any]: ...


KeyRevealer = Callable[[ProviderBindingContext, SessionRecord], str]
SessionStoreFactory = Callable[[Path], SessionStore]


class ProviderBindingExecutor:
    def __init__(
        self,
        store: ProviderBindingStore,
        admin: EnodotProviderAdmin,
        *,
        key_revealer: KeyRevealer | None = None,
        session_store_factory: SessionStoreFactory = SessionStore,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.store = store
        self.admin = admin
        self.key_revealer = key_revealer
        self.session_store_factory = session_store_factory
        self.monotonic = monotonic

    def execute(
        self,
        request: ProviderBindingRequest,
        *,
        apply: bool = False,
    ) -> ProviderBindingResult:
        if not isinstance(apply, bool):
            raise ProviderBindingError("provider_binding_apply_invalid")
        preparation: BindingPreparation | None = None
        started = self.monotonic()
        try:
            context = self.store.load_context(
                request.merchant_id,
                request.external_a6_token_id,
            )
            _validate_context(context, request)
            session_store, record = self._load_session(context)
            key, key_fingerprint, needs_session_save = self._resolve_key(context, record)
            secret_ref = f"{context.session_ref}#a6api_key"
            desired_state = _desired_audit_state(context, request, key_fingerprint)
            idempotency_key = _idempotency_key(request, key_fingerprint)

            if apply:
                preparation = self.store.prepare_binding(
                    context,
                    request,
                    key_fingerprint=key_fingerprint,
                    key_secret_ref=secret_ref,
                    desired_state=desired_state,
                    idempotency_key=idempotency_key,
                )
                if needs_session_save:
                    self._save_revealed_key(
                        session_store,
                        record,
                        context,
                        key,
                        key_fingerprint,
                    )

            _validate_proxy(self.admin, context, request.proxy_id)
            provider = _provider_kwargs(context, request, key, key_fingerprint)
            plan = self.admin.reconcile_a6_provider_account(apply=False, **provider)
            action = str(getattr(plan, "action", ""))
            if action not in {"create", "update", "noop"}:
                raise ProviderBindingError("provider_binding_reconcile_result_invalid")
            account_id = _optional_positive_int(getattr(plan, "account_id", None))

            if not apply:
                return ProviderBindingResult(
                    mode="dry-run",
                    merchant_id=request.merchant_id,
                    external_a6_token_id=request.external_a6_token_id,
                    binding_uid=request.binding_uid,
                    action=action,
                    applied=False,
                    account_id=account_id,
                    session_updated=False,
                    verified=False,
                )

            if action == "create":
                created = self.admin.create_account(
                    _create_payload(context, request, key, key_fingerprint)
                )
                account_id = _positive_int(
                    created.get("id"),
                    "provider_binding_created_account_id_invalid",
                )
            elif action == "update":
                account_id = _positive_int(
                    account_id,
                    "provider_binding_existing_account_id_missing",
                )
                before_update = self.admin.get_account(account_id)
                if before_update.get("schedulable") is not False:
                    self.admin.set_schedulable(account_id, False)
                reconciled = self.admin.reconcile_a6_provider_account(apply=True, **provider)
                updated_account_id = _positive_int(
                    getattr(reconciled, "account_id", None),
                    "provider_binding_updated_account_id_invalid",
                )
                if updated_account_id != account_id:
                    raise ProviderBindingError("provider_binding_updated_account_id_mismatch")
            elif account_id is None:
                raise ProviderBindingError("provider_binding_existing_account_id_missing")

            observed = self.admin.get_account(account_id)
            if observed.get("schedulable") is not False:
                self.admin.set_schedulable(account_id, False)
                observed = self.admin.get_account(account_id)
            if list(observed.get("group_ids") or []) != list(request.group_ids):
                self.admin.update_account(
                    account_id,
                    {"group_ids": list(request.group_ids)},
                )
                observed = self.admin.get_account(account_id)
            snapshot = _verify_readback(observed, context, request, key_fingerprint, account_id)
            by_uid = self.admin.find_account_by_binding_uid(request.binding_uid)
            if by_uid is None or _optional_positive_int(by_uid.get("id")) != account_id:
                raise ProviderBindingError("provider_binding_uid_readback_mismatch")
            elapsed_ms = _elapsed_ms(started, self.monotonic())
            self.store.complete_binding(
                preparation,
                request,
                account_id=account_id,
                observed_after=snapshot,
                elapsed_ms=elapsed_ms,
            )
            return ProviderBindingResult(
                mode="applied",
                merchant_id=request.merchant_id,
                external_a6_token_id=request.external_a6_token_id,
                binding_uid=request.binding_uid,
                action=action,
                applied=action != "noop",
                account_id=account_id,
                session_updated=needs_session_save,
                verified=True,
            )
        except Exception as exc:
            code = _error_code(exc)
            if preparation is not None:
                self.store.fail_binding(
                    preparation,
                    request,
                    error_class=code,
                    error_fingerprint=hashlib.sha256(code.encode()).hexdigest(),
                    elapsed_ms=_elapsed_ms(started, self.monotonic()),
                )
            raise ProviderBindingError(code) from None

    def _load_session(
        self,
        context: ProviderBindingContext,
    ) -> tuple[SessionStore, SessionRecord]:
        store = self.session_store_factory(Path(context.session_ref))
        record = store.load()
        if record.status != _PROVIDER_STATUS:
            raise ProviderBindingError("provider_binding_session_status_invalid")
        if record.user_id != context.a6_user_id:
            raise ProviderBindingError("provider_binding_session_user_mismatch")
        if record.supplier_id != context.supplier_id:
            raise ProviderBindingError("provider_binding_session_supplier_mismatch")
        return store, record

    def _resolve_key(
        self,
        context: ProviderBindingContext,
        record: SessionRecord,
    ) -> tuple[str, str, bool]:
        key = str(record.a6api_key or "").strip()
        needs_save = False
        if not key:
            if self.key_revealer is None:
                raise ProviderBindingError("provider_binding_key_revealer_required")
            key = str(self.key_revealer(context, record) or "").strip()
            needs_save = True
        key = _normalize_key(key)
        fingerprint = hashlib.sha256(key.encode()).hexdigest()
        if context.key_fingerprint and context.key_fingerprint != fingerprint:
            raise ProviderBindingError("provider_binding_key_fingerprint_mismatch")
        if record.a6api_key and record.token_name != context.token_name:
            raise ProviderBindingError("provider_binding_session_token_name_mismatch")
        return key, fingerprint, needs_save

    @staticmethod
    def _save_revealed_key(
        store: SessionStore,
        record: SessionRecord,
        context: ProviderBindingContext,
        key: str,
        fingerprint: str,
    ) -> None:
        store.save(replace(record, a6api_key=key, token_name=context.token_name))
        saved = store.load()
        saved_fingerprint = hashlib.sha256(str(saved.a6api_key).encode()).hexdigest()
        if saved.token_name != context.token_name or saved_fingerprint != fingerprint:
            raise ProviderBindingError("provider_binding_session_key_write_unconfirmed")


class PostgresProviderBindingStore:
    def __init__(self, connection_factory: Callable[[], Any]) -> None:
        self.connection_factory = connection_factory

    def load_context(self, merchant_id: int, external_a6_token_id: int) -> ProviderBindingContext:
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(_CONTEXT_QUERY, (merchant_id, external_a6_token_id))
                row = _row(cursor)
            if row is None:
                raise ProviderBindingError("provider_binding_context_missing")
            return _context_from_row(row)
        finally:
            _close(connection)

    def prepare_binding(
        self,
        context: ProviderBindingContext,
        request: ProviderBindingRequest,
        *,
        key_fingerprint: str,
        key_secret_ref: str,
        desired_state: Mapping[str, Any],
        idempotency_key: str,
    ) -> BindingPreparation:
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    _TOKEN_LOCK_QUERY,
                    (request.merchant_id, request.external_a6_token_id),
                )
                if _row(cursor) is None:
                    raise ProviderBindingError("provider_binding_context_missing")
                # Growth claims lock the Token before inserting an action whose
                # merchant FK takes KEY SHARE. Keep the same Token -> Merchant
                # order here and use SHARE because this transaction never writes
                # the Merchant row.
                cursor.execute(
                    _CONTEXT_QUERY + " FOR SHARE OF m",
                    (request.merchant_id, request.external_a6_token_id),
                )
                locked_row = _row(cursor)
                if locked_row is None:
                    raise ProviderBindingError("provider_binding_context_missing")
                locked = _context_from_row(locked_row)
                _validate_context(locked, request)
                if locked != context:
                    raise ProviderBindingError("provider_binding_context_changed")
                cursor.execute(
                    """
                    SELECT id
                    FROM a6_trade.a6_channel_growth_actions
                    WHERE booster_token_ref_id = %s
                      AND state = 'running'
                    ORDER BY id
                    LIMIT 1
                    FOR UPDATE
                    """,
                    (context.token_row_id,),
                )
                if _row(cursor) is not None:
                    raise ProviderBindingError("provider_binding_booster_action_running")
                cursor.execute(
                    """
                    UPDATE a6_trade.a6_tokens
                    SET key_fingerprint = %s, key_secret_ref = %s,
                        last_reconciled_at = now(), updated_at = now()
                    WHERE id = %s
                    """,
                    (key_fingerprint, key_secret_ref, context.token_row_id),
                )
                cursor.execute(
                    """
                    INSERT INTO a6_trade.enodot_account_bindings (
                        a6_token_id, binding_uid, model_scope,
                        desired_priority, desired_concurrency, state
                    ) VALUES (%s, %s, %s::text[], %s, %s, 'planned')
                    ON CONFLICT (binding_uid) DO UPDATE SET
                        model_scope = EXCLUDED.model_scope,
                        desired_priority = EXCLUDED.desired_priority,
                        desired_concurrency = EXCLUDED.desired_concurrency,
                        state = CASE
                            WHEN a6_trade.enodot_account_bindings.enodot_account_id IS NULL
                            THEN 'planned'
                            ELSE a6_trade.enodot_account_bindings.state
                        END,
                        updated_at = now()
                    WHERE a6_trade.enodot_account_bindings.a6_token_id = EXCLUDED.a6_token_id
                    RETURNING id, enodot_account_id, state
                    """,
                    (
                        context.token_row_id,
                        request.binding_uid,
                        list(request.model_scope),
                        request.priority,
                        request.concurrency,
                    ),
                )
                binding = _row(cursor)
                if binding is None:
                    raise ProviderBindingError("provider_binding_uid_conflict")
                observed_before = {
                    "binding_id": int(binding["id"]),
                    "enodot_account_id": binding.get("enodot_account_id"),
                    "state": str(binding.get("state") or "planned"),
                }
                cursor.execute(
                    """
                    INSERT INTO a6_trade.trade_control_actions (
                        idempotency_key, action_type, entity_type, entity_id,
                        desired_state, observed_before, status,
                        config_version, requested_by, started_at
                    ) VALUES (
                        %s, 'bind_provider_account', 'a6_token', %s,
                        %s::jsonb, %s::jsonb, 'started', %s, %s, now()
                    )
                    ON CONFLICT (idempotency_key) DO UPDATE SET
                        desired_state = EXCLUDED.desired_state,
                        updated_at = now()
                    RETURNING id, status
                    """,
                    (
                        idempotency_key,
                        str(request.external_a6_token_id),
                        _json(desired_state),
                        _json(observed_before),
                        request.config_version,
                        request.requested_by,
                    ),
                )
                action = _row(cursor)
                if action is None:
                    raise ProviderBindingError("provider_binding_audit_write_failed")
                action_id = int(action["id"])
                audit_completed = str(action.get("status")) == "completed"
                attempt_no: int | None = None
                if not audit_completed:
                    cursor.execute(
                        """
                        UPDATE a6_trade.trade_control_actions
                        SET status = 'started', error_class = NULL,
                            started_at = coalesce(started_at, now()), updated_at = now()
                        WHERE id = %s
                        """,
                        (action_id,),
                    )
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
            return BindingPreparation(
                binding_id=int(binding["id"]),
                action_id=action_id,
                attempt_no=attempt_no,
                existing_account_id=_optional_positive_int(binding.get("enodot_account_id")),
                existing_state=str(binding.get("state") or "planned"),
                audit_completed=audit_completed,
            )
        except Exception:
            connection.rollback()
            raise
        finally:
            _close(connection)

    def complete_binding(
        self,
        preparation: BindingPreparation,
        request: ProviderBindingRequest,
        *,
        account_id: int,
        observed_after: Mapping[str, Any],
        elapsed_ms: int,
    ) -> None:
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE a6_trade.enodot_account_bindings
                    SET enodot_account_id = %s, state = 'bound',
                        last_error_class = NULL, last_error_at = NULL, updated_at = now()
                    WHERE id = %s AND binding_uid = %s
                    """,
                    (account_id, preparation.binding_id, request.binding_uid),
                )
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
                if preparation.attempt_no is not None:
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

    def fail_binding(
        self,
        preparation: BindingPreparation,
        request: ProviderBindingRequest,
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
                    UPDATE a6_trade.enodot_account_bindings
                    SET state = 'error', last_error_class = %s,
                        last_error_at = now(), updated_at = now()
                    WHERE id = %s AND binding_uid = %s
                    """,
                    (error_class, preparation.binding_id, request.binding_uid),
                )
                cursor.execute(
                    """
                    UPDATE a6_trade.trade_control_actions
                    SET status = 'failed', error_class = %s,
                        completed_at = now(), elapsed_ms = %s, updated_at = now()
                    WHERE id = %s
                    """,
                    (error_class, elapsed_ms, preparation.action_id),
                )
                if preparation.attempt_no is not None:
                    cursor.execute(
                        """
                        UPDATE a6_trade.trade_control_action_attempts
                        SET status = 'failed', error_class = %s,
                            error_fingerprint = %s, completed_at = now(), elapsed_ms = %s
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


_CONTEXT_QUERY = """
    SELECT m.id AS merchant_id, m.a6_user_id, m.supplier_id,
           m.status AS merchant_status, m.roles, m.session_ref, m.proxy_lease_id,
           t.id AS token_row_id, t.a6_token_id AS external_a6_token_id,
           t.token_name, t.status AS token_status,
           coalesce(t.key_fingerprint, '') AS key_fingerprint,
           coalesce(t.key_secret_ref, '') AS key_secret_ref
    FROM a6_trade.a6_merchants m
    JOIN a6_trade.a6_tokens t ON t.merchant_id = m.id
    WHERE m.id = %s AND t.a6_token_id = %s
"""


_TOKEN_LOCK_QUERY = """
    SELECT id
    FROM a6_trade.a6_tokens
    WHERE merchant_id = %s AND a6_token_id = %s
    FOR UPDATE
"""


def _context_from_row(row: Mapping[str, Any]) -> ProviderBindingContext:
    try:
        return ProviderBindingContext(
            merchant_id=int(row["merchant_id"]),
            a6_user_id=int(row["a6_user_id"]),
            supplier_id=int(row["supplier_id"]),
            merchant_status=str(row["merchant_status"]),
            roles=tuple(str(value) for value in row["roles"]),
            session_ref=str(row["session_ref"]),
            proxy_lease_id=str(row.get("proxy_lease_id") or ""),
            token_row_id=int(row["token_row_id"]),
            external_a6_token_id=int(row["external_a6_token_id"]),
            token_name=str(row["token_name"]),
            token_status=str(row["token_status"]),
            key_fingerprint=str(row.get("key_fingerprint") or ""),
            key_secret_ref=str(row.get("key_secret_ref") or ""),
        )
    except (KeyError, TypeError, ValueError):
        raise ProviderBindingError("provider_binding_context_invalid") from None


def _validate_context(
    context: ProviderBindingContext,
    request: ProviderBindingRequest,
) -> None:
    if context.merchant_id != request.merchant_id:
        raise ProviderBindingError("provider_binding_merchant_mismatch")
    if context.external_a6_token_id != request.external_a6_token_id:
        raise ProviderBindingError("provider_binding_token_mismatch")
    if context.merchant_status != _PROVIDER_STATUS:
        raise ProviderBindingError("provider_binding_merchant_status_invalid")
    if tuple(context.roles) not in {("seller", "provider"), ("provider",)}:
        raise ProviderBindingError("provider_binding_role_invalid")
    if context.a6_user_id < 1 or context.supplier_id < 1 or context.token_row_id < 1:
        raise ProviderBindingError("provider_binding_identity_invalid")
    if context.token_status.casefold() not in _ACTIVE_TOKEN_STATES:
        raise ProviderBindingError("provider_binding_token_status_invalid")
    if not context.token_name:
        raise ProviderBindingError("provider_binding_token_name_invalid")
    if not context.session_ref:
        raise ProviderBindingError("provider_binding_session_ref_missing")
    if not re.fullmatch(r"singbox:\d+", context.proxy_lease_id):
        raise ProviderBindingError("provider_binding_proxy_lease_invalid")


def _validate_proxy(
    admin: EnodotProviderAdmin,
    context: ProviderBindingContext,
    proxy_id: int,
) -> None:
    inventory = admin.request("GET", "admin/proxies/all")
    if not isinstance(inventory, Sequence) or isinstance(inventory, (str, bytes)):
        raise ProviderBindingError("provider_binding_proxy_inventory_invalid")
    matches = [
        item for item in inventory if isinstance(item, Mapping) and item.get("id") == proxy_id
    ]
    if len(matches) != 1:
        raise ProviderBindingError("provider_binding_proxy_missing")
    proxy = matches[0]
    if str(proxy.get("status") or "").casefold() != "active":
        raise ProviderBindingError("provider_binding_proxy_inactive")
    expected_port = int(context.proxy_lease_id.split(":", 1)[1])
    if _optional_positive_int(proxy.get("port")) != expected_port:
        raise ProviderBindingError("provider_binding_proxy_lease_mismatch")


def _normalize_key(key: str) -> str:
    normalized = key if key.startswith("sk-") else f"sk-{key}"
    if len(normalized) < 20 or any(character.isspace() for character in normalized):
        raise ProviderBindingError("provider_binding_key_invalid")
    return normalized


def _provider_extra(
    context: ProviderBindingContext,
    request: ProviderBindingRequest,
    key_fingerprint: str,
) -> dict[str, Any]:
    return {
        "managed_by": "a6-trader",
        "a6_provider": True,
        "a6_binding_uid": request.binding_uid,
        "a6_api_key_fingerprint": key_fingerprint,
        "a6_merchant_id": context.merchant_id,
        "a6_user_id": context.a6_user_id,
        "a6_supplier_id": context.supplier_id,
        "external_a6_token_id": context.external_a6_token_id,
    }


def _provider_kwargs(
    context: ProviderBindingContext,
    request: ProviderBindingRequest,
    key: str,
    key_fingerprint: str,
) -> dict[str, Any]:
    return {
        "binding_uid": request.binding_uid,
        "name": request.account_name,
        "api_key": key,
        "proxy_id": _account_proxy_id(request),
        "base_url": _account_base_url(context, request),
        "model_mapping": dict(request.model_mapping),
        "pool_mode": request.pool_mode,
        "pool_mode_retry_count": request.pool_mode_retry_count,
        "pool_mode_retry_status_codes": list(request.pool_mode_retry_status_codes),
        "concurrency": request.concurrency,
        "priority": request.priority,
        "rate_multiplier": request.rate_multiplier,
        "group_ids": list(request.group_ids),
        "extra": _provider_extra(context, request, key_fingerprint),
    }


def _create_payload(
    context: ProviderBindingContext,
    request: ProviderBindingRequest,
    key: str,
    key_fingerprint: str,
) -> dict[str, Any]:
    return {
        "name": request.account_name,
        "platform": "openai",
        "type": "apikey",
        "credentials": {
            "api_key": key,
            "base_url": _account_base_url(context, request),
            "model_mapping": dict(request.model_mapping),
            "pool_mode": request.pool_mode,
            "pool_mode_retry_count": request.pool_mode_retry_count,
            "pool_mode_retry_status_codes": list(request.pool_mode_retry_status_codes),
        },
        "extra": _provider_extra(context, request, key_fingerprint),
        "proxy_id": _account_proxy_id(request),
        # Keep a newly-created account unroutable until disabled is confirmed.
        "group_ids": [],
        "concurrency": request.concurrency,
        "priority": request.priority,
        "rate_multiplier": request.rate_multiplier,
        "schedulable": False,
    }


def _desired_audit_state(
    context: ProviderBindingContext,
    request: ProviderBindingRequest,
    key_fingerprint: str,
) -> dict[str, Any]:
    return {
        "binding_uid": request.binding_uid,
        "merchant_id": context.merchant_id,
        "external_a6_token_id": context.external_a6_token_id,
        "key_fingerprint": key_fingerprint,
        "proxy_id": _account_proxy_id(request),
        "egress_proxy_id": request.proxy_id,
        "group_ids": list(request.group_ids),
        "model_scope": list(request.model_scope),
        "base_url": _account_base_url(context, request),
        "upstream_relay": request.use_upstream_relay,
        "model_mapping": dict(request.model_mapping),
        "pool_mode": request.pool_mode,
        "pool_mode_retry_count": request.pool_mode_retry_count,
        "pool_mode_retry_status_codes": list(request.pool_mode_retry_status_codes),
        "concurrency": request.concurrency,
        "priority": request.priority,
        "rate_multiplier": request.rate_multiplier,
        "schedulable": False,
    }


def _verify_readback(
    account: Mapping[str, Any],
    context: ProviderBindingContext,
    request: ProviderBindingRequest,
    key_fingerprint: str,
    account_id: int,
) -> dict[str, Any]:
    account_proxy_id = _account_proxy_id(request)
    account_base_url = _account_base_url(context, request)
    checks = (
        (_optional_positive_int(account.get("id")) == account_id, "account_id"),
        (account.get("name") == request.account_name, "name"),
        (account.get("platform") == "openai", "platform"),
        (account.get("type") == "apikey", "type"),
        (_optional_positive_int(account.get("proxy_id")) == account_proxy_id, "proxy"),
        (list(account.get("group_ids") or []) == list(request.group_ids), "group_ids"),
        (account.get("concurrency") == request.concurrency, "concurrency"),
        (account.get("priority") == request.priority, "priority"),
        (float(account.get("rate_multiplier") or 0) == request.rate_multiplier, "rate_multiplier"),
        (account.get("schedulable") is False, "schedulable"),
    )
    for matched, field_name in checks:
        if not matched:
            raise ProviderBindingError(f"provider_binding_readback_{field_name}_mismatch")
    credentials = account.get("credentials")
    if not isinstance(credentials, Mapping):
        raise ProviderBindingError("provider_binding_readback_credentials_invalid")
    if str(credentials.get("base_url") or "").rstrip("/") != account_base_url:
        raise ProviderBindingError("provider_binding_readback_base_url_mismatch")
    if dict(credentials.get("model_mapping") or {}) != dict(request.model_mapping):
        raise ProviderBindingError("provider_binding_readback_model_mapping_mismatch")
    if credentials.get("pool_mode") is not request.pool_mode:
        raise ProviderBindingError("provider_binding_readback_pool_mode_mismatch")
    if credentials.get("pool_mode_retry_count") != request.pool_mode_retry_count:
        raise ProviderBindingError("provider_binding_readback_pool_mode_retry_count_mismatch")
    if list(credentials.get("pool_mode_retry_status_codes") or []) != list(
        request.pool_mode_retry_status_codes
    ):
        raise ProviderBindingError(
            "provider_binding_readback_pool_mode_retry_status_codes_mismatch"
        )
    extra = account.get("extra")
    if not isinstance(extra, Mapping):
        raise ProviderBindingError("provider_binding_readback_extra_invalid")
    expected_extra = _provider_extra(context, request, key_fingerprint)
    if any(extra.get(key) != value for key, value in expected_extra.items()):
        raise ProviderBindingError("provider_binding_readback_extra_mismatch")
    return {
        "account_id": account_id,
        "binding_uid": request.binding_uid,
        "key_fingerprint": key_fingerprint,
        "proxy_id": account_proxy_id,
        "egress_proxy_id": request.proxy_id,
        "group_ids": list(request.group_ids),
        "base_url": account_base_url,
        "upstream_relay": request.use_upstream_relay,
        "model_mapping": dict(request.model_mapping),
        "pool_mode": request.pool_mode,
        "pool_mode_retry_count": request.pool_mode_retry_count,
        "pool_mode_retry_status_codes": list(request.pool_mode_retry_status_codes),
        "concurrency": request.concurrency,
        "priority": request.priority,
        "rate_multiplier": request.rate_multiplier,
        "schedulable": False,
        "verified": True,
    }


def _idempotency_key(request: ProviderBindingRequest, key_fingerprint: str) -> str:
    return f"bind-provider:{request.binding_uid}:{key_fingerprint[:16]}:{request.config_version}"


def _account_base_url(
    context: ProviderBindingContext,
    request: ProviderBindingRequest,
) -> str:
    if not request.use_upstream_relay:
        return request.base_url
    return f"{_A6_RELAY_ORIGIN}/a6-user/{context.a6_user_id}/v1"


def _account_proxy_id(request: ProviderBindingRequest) -> int | None:
    # request.proxy_id identifies the Trader control-plane session used for key reveal.
    # Provider runtime traffic reaches the local relay directly and has no account proxy.
    return None if request.use_upstream_relay else request.proxy_id


def _error_code(exc: Exception) -> str:
    if isinstance(exc, ProviderBindingError):
        return exc.code
    candidate = str(getattr(exc, "code", "") or "")
    if _SAFE_NAME.fullmatch(candidate):
        return candidate
    name = re.sub(r"(?<!^)(?=[A-Z])", "_", type(exc).__name__).lower()
    return f"provider_binding_{name}"


def _row(cursor: Any) -> dict[str, Any] | None:
    raw = cursor.fetchone()
    if raw is None:
        return None
    if isinstance(raw, Mapping):
        return dict(raw)
    names = [getattr(column, "name", column[0]) for column in cursor.description]
    return dict(zip(names, raw, strict=True))


def _positive_int(value: Any, error: str) -> int:
    result = _optional_positive_int(value)
    if result is None:
        raise ProviderBindingError(error)
    return result


def _optional_positive_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = int(value)
    except (TypeError, ValueError):
        return None
    return result if result > 0 else None


def _json(value: Mapping[str, Any]) -> str:
    return json.dumps(dict(value), ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _elapsed_ms(started: float, finished: float) -> int:
    return max(0, int((finished - started) * 1000))


def _close(connection: Any) -> None:
    close = getattr(connection, "close", None)
    if callable(close):
        close()
