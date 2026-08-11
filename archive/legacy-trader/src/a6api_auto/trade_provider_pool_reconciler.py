"""Remote saga for applying a confirmed Provider pool bundle.

The reconciler is dependency-injected. A6/Enodot adapters implement the small
control protocol, while tests and shadow mode can use a recording adapter. No
database transaction is held across a remote call; the generation store is
updated only after all readbacks succeed.
"""

from __future__ import annotations

import hashlib
import json
import math
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import AbstractContextManager, contextmanager, nullcontext
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
from enum import Enum
from types import MappingProxyType
from typing import Any, Protocol

from .trade_provider_cost import PoolDiffKind, RoutingView, classify_pool_diff
from .trade_provider_pool import (
    ProviderBundle,
    ProviderPoolStore,
    ProviderSlot,
    SlotState,
)

# Keep the long-lived remote-saga lease separate from the short transaction
# lock used by PostgresProviderPoolStore. Reusing the store namespace here
# would make store.plan() wait on the lease held by this same reconciler.
_PROVIDER_SAGA_LOCK_NAMESPACE = 0x41365051
# Shared with Sub2API's request-plane pre-send lease. Request nodes hold a
# shared lock only until upstream headers/a semantic WS frame are written;
# activation holds the exclusive side through Redis dispatch publication.
_PROVIDER_PRE_SEND_LOCK_NAMESPACE = 0x41365052
_REPLAN_PROBE_ERROR_CODES = frozenset(
    {
        "provider_chat_probe_response_invalid",
        "provider_chat_probe_transport_failed",
        "provider_probe_response_invalid",
        "provider_probe_stream_invalid",
        "provider_probe_transport_failed",
    }
)
_PIN_CHANNEL_ERROR_CODES = frozenset(
    {
        "channel_not_found",
        "channel_not_in_marketplace",
        "channel_not_marketplace",
        "model_not_available_in_channel",
        "supplier_channel_disabled",
        "user_channel_disabled",
    }
)
_PIN_PLATFORM_ERROR_CODES = frozenset(
    {
        "a6_request_deadline_exceeded",
        "a6_response_invalid",
        "a6_transport_failed",
        "provider_remote_pin_readback_mismatch",
    }
)
_PIN_CHANNEL_HTTP_STATUSES = frozenset({400, 404, 409, 410, 422})
_PROBE_REPLAN_SCOPES = frozenset(
    {"channel", "liquidity", "provider", "platform", "internal", "unknown"}
)


class ProviderPoolReconcileError(RuntimeError):
    def __init__(
        self,
        code: str,
        *,
        diagnostics: Mapping[str, Any] | None = None,
    ) -> None:
        self.code = code
        self.diagnostics = MappingProxyType(dict(diagnostics or {}))
        message = code
        if self.diagnostics:
            message = f"{code}: {_json(dict(self.diagnostics))}"
        super().__init__(message)


class ProviderReadbackRetryRequired(ProviderPoolReconcileError):
    """Keep the unpublished generation fail-closed and retry its readback in place."""

    def __init__(self, cause: BaseException) -> None:
        cause_code = str(getattr(cause, "code", "") or cause).split(":", 1)[0].strip()
        inherited = getattr(cause, "diagnostics", None)
        diagnostics = dict(inherited) if isinstance(inherited, Mapping) else {}
        diagnostics.setdefault("cause_code", cause_code or type(cause).__name__)
        super().__init__(
            "provider_reconcile_readback_retry_required",
            diagnostics=diagnostics,
        )


class ProviderRoundDeadlineExceeded(ProviderPoolReconcileError):
    """Yield an unfinished generation when its bounded remote-write round expires."""

    def __init__(self) -> None:
        super().__init__("provider_reconcile_round_deadline_exceeded")


class ReplanRequired(ProviderPoolReconcileError):
    def __init__(
        self,
        code: str,
        *,
        slot_no: int | None = None,
        channel_id: int | None = None,
        account_id: int | None = None,
        token_id: int | None = None,
        replan_scope: str = "unknown",
        probe_observation: Mapping[str, Any] | None = None,
        verified_slot_nos: Sequence[int] = (),
    ) -> None:
        super().__init__(code)
        for name, value in (
            ("slot_no", slot_no),
            ("channel_id", channel_id),
            ("account_id", account_id),
            ("token_id", token_id),
        ):
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value < 1
            ):
                raise ValueError(f"provider_replan_{name}_invalid")
        self.slot_no = slot_no
        self.channel_id = channel_id
        self.account_id = account_id
        self.token_id = token_id
        scope = str(replan_scope or "").strip().casefold()
        if scope not in _PROBE_REPLAN_SCOPES:
            raise ValueError("provider_replan_scope_invalid")
        self.replan_scope = scope
        self.probe_observation = MappingProxyType(
            dict(probe_observation or {})
        )
        verified: list[int] = []
        for value in verified_slot_nos:
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError("provider_replan_verified_slot_invalid")
            if value in verified:
                raise ValueError("provider_replan_verified_slot_duplicate")
            verified.append(value)
        self.verified_slot_nos = tuple(sorted(verified))


class ProviderPlatformBackoffActive(ProviderPoolReconcileError):
    """Durable platform backoff detected before generation or remote writes."""

    def __init__(self, observation: Mapping[str, Any]) -> None:
        super().__init__("provider_probe_backoff_active")
        if not isinstance(observation, Mapping):
            raise ValueError("provider_platform_backoff_observation_invalid")
        document = dict(observation)
        nested = document.get("document")
        if (
            document.get("confirmed") is not False
            or document.get("error_code") != "provider_probe_backoff_active"
            or document.get("failure_owner") != "platform"
            or not isinstance(nested, Mapping)
            or nested.get("request_sent") is not False
        ):
            raise ValueError("provider_platform_backoff_observation_invalid")
        self.observation = MappingProxyType(document)


@dataclass(frozen=True)
class ProviderProbeResult:
    """Redacted, responsibility-aware result of one real Provider probe."""

    confirmed: bool
    protocol: str
    http_status: int | None
    error_code: str | None
    failure_owner: str
    replan_scope: str
    document: Mapping[str, Any]

    def __post_init__(self) -> None:
        if not isinstance(self.confirmed, bool):
            raise ValueError("provider_probe_result_confirmed_invalid")
        protocol = str(self.protocol or "").strip().casefold()
        if protocol not in {"responses", "chat_completions", "unknown"}:
            raise ValueError("provider_probe_result_protocol_invalid")
        status = self.http_status
        if status is not None and (
            isinstance(status, bool) or not isinstance(status, int) or not 100 <= status <= 599
        ):
            raise ValueError("provider_probe_result_status_invalid")
        code = str(self.error_code or "").strip().casefold() or None
        owner = str(self.failure_owner or "").strip().casefold()
        if owner not in {
            "none",
            "channel",
            "liquidity",
            "provider",
            "platform",
            "internal",
            "unknown",
        }:
            raise ValueError("provider_probe_result_owner_invalid")
        scope = str(self.replan_scope or "").strip().casefold()
        if scope not in _PROBE_REPLAN_SCOPES | {"none"}:
            raise ValueError("provider_probe_result_scope_invalid")
        if self.confirmed:
            if status is None or not 200 <= status < 300 or code is not None:
                raise ValueError("provider_probe_result_success_invalid")
            if owner != "none" or scope != "none":
                raise ValueError("provider_probe_result_success_invalid")
        elif owner == "none" or scope == "none":
            raise ValueError("provider_probe_result_failure_invalid")
        if not isinstance(self.document, Mapping):
            raise ValueError("provider_probe_result_document_invalid")
        object.__setattr__(self, "protocol", protocol)
        object.__setattr__(self, "error_code", code)
        object.__setattr__(self, "failure_owner", owner)
        object.__setattr__(self, "replan_scope", scope)
        object.__setattr__(self, "document", MappingProxyType(dict(self.document)))

    def observation(self) -> dict[str, Any]:
        return {
            "confirmed": self.confirmed,
            "protocol": self.protocol,
            "http_status": self.http_status,
            "error_code": self.error_code,
            "failure_owner": self.failure_owner,
            "replan_scope": self.replan_scope,
            "document": dict(self.document),
        }


class ProviderActionLedgerError(ProviderPoolReconcileError):
    pass


class ProviderPoolRemote(Protocol):
    def platform_backoff(self, model: str) -> ProviderProbeResult | None: ...

    def stage_billing_floor(
        self, model: str, multiplier: Decimal, *, idempotency_key: str
    ) -> bool: ...

    def finalize_billing_floor(
        self, model: str, multiplier: Decimal, *, idempotency_key: str
    ) -> bool: ...

    def read_billing_floor(self, model: str) -> Decimal: ...

    def set_slot_cost(
        self, slot: ProviderSlot, multiplier: Decimal, *, idempotency_key: str
    ) -> bool: ...

    def drain_slot(self, slot: ProviderSlot, *, idempotency_key: str) -> bool: ...

    def repin_slot(self, slot: ProviderSlot, *, idempotency_key: str) -> bool: ...

    def probe_slot(
        self, slot: ProviderSlot, *, idempotency_key: str
    ) -> bool | ProviderProbeResult: ...

    def set_priority(self, slot: ProviderSlot, *, idempotency_key: str) -> bool: ...

    def set_assignment_metadata(
        self,
        bundle: ProviderBundle,
        slot: ProviderSlot,
        *,
        idempotency_key: str,
    ) -> bool: ...

    def set_schedulable(
        self, slot: ProviderSlot, enabled: bool, *, idempotency_key: str
    ) -> bool: ...

    def read_slot_schedulable(self, slot: ProviderSlot) -> bool: ...

    def readback(
        self,
        bundle: ProviderBundle,
        *,
        deferred_slot_nos: frozenset[int] = frozenset(),
    ) -> ProviderPoolReadback: ...


@dataclass(frozen=True)
class ProviderSlotReadback:
    slot_no: int
    account_id: int | None
    token_id: int | None
    channel_id: int | None
    priority: int | None
    rate_multiplier: Decimal | None
    schedulable: bool
    pin_binding_id: int | None
    pin_confirmed: bool
    probe_confirmed: bool
    assignment_metadata: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class ProviderPoolReadback:
    model: str
    billing_floor_multiplier: Decimal
    slots: Mapping[int, ProviderSlotReadback]


@dataclass(frozen=True)
class ProviderRequestPlaneCapability:
    """Runtime proof required before a Provider generation can mutate state."""

    model: str
    instance_id: str
    contract_version: str
    pre_send_advisory_lock_namespace: int
    paid_send_max_attempts: int
    http_pre_send_lease: bool
    sse_pre_send_lease: bool
    ws_per_billing_turn_budget: bool
    dispatch_lease_enabled: bool

    @classmethod
    def from_document(
        cls,
        document: object,
        *,
        expected_model: str,
    ) -> ProviderRequestPlaneCapability:
        if not isinstance(document, Mapping):
            raise ProviderPoolReconcileError(
                "provider_request_plane_capability_invalid"
            )
        try:
            capability = str(document["capability"])
            model = str(document["model"])
            instance_id = str(document["instance_id"]).strip()
            contract_version = str(document["contract_version"])
            namespace = document["pre_send_advisory_lock_namespace"]
            paid_send_max_attempts = document["paid_send_max_attempts"]
            http_pre_send_lease = document["http_pre_send_lease"]
            sse_pre_send_lease = document["sse_pre_send_lease"]
            ws_per_billing_turn_budget = document["ws_per_billing_turn_budget"]
            dispatch_lease_enabled = document["dispatch_lease_enabled"]
        except KeyError:
            raise ProviderPoolReconcileError(
                "provider_request_plane_capability_invalid"
            ) from None
        if (
            capability != "a6_provider_runtime"
            or model != str(expected_model)
            or not instance_id
            or contract_version != "1.0"
            or isinstance(namespace, bool)
            or not isinstance(namespace, int)
            or namespace != _PROVIDER_PRE_SEND_LOCK_NAMESPACE
            or isinstance(paid_send_max_attempts, bool)
            or not isinstance(paid_send_max_attempts, int)
            or paid_send_max_attempts != 1
            or http_pre_send_lease is not True
            or sse_pre_send_lease is not True
            or ws_per_billing_turn_budget is not True
            or dispatch_lease_enabled is not True
        ):
            raise ProviderPoolReconcileError(
                "provider_request_plane_capability_mismatch"
            )
        return cls(
            model=model,
            instance_id=instance_id,
            contract_version=contract_version,
            pre_send_advisory_lock_namespace=namespace,
            paid_send_max_attempts=paid_send_max_attempts,
            http_pre_send_lease=http_pre_send_lease,
            sse_pre_send_lease=sse_pre_send_lease,
            ws_per_billing_turn_budget=ws_per_billing_turn_budget,
            dispatch_lease_enabled=dispatch_lease_enabled,
        )


@dataclass(frozen=True)
class ProviderActionScope:
    config_version: str
    model: str
    routing_generation: int
    cost_generation: int

    def __post_init__(self) -> None:
        if not self.config_version or not self.model:
            raise ProviderActionLedgerError("provider_action_scope_invalid")
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 1
            for value in (self.routing_generation, self.cost_generation)
        ):
            raise ProviderActionLedgerError("provider_action_scope_invalid")


class ProviderModelLease(Protocol):
    def hold(self, model: str) -> AbstractContextManager[None]: ...


class ProviderActivationBarrier(Protocol):
    def hold(self, model: str) -> AbstractContextManager[None]: ...


class PostgresProviderModelLease:
    """Hold one session advisory lock across the complete remote saga."""

    def __init__(self, connection_factory: Callable[[], Any]) -> None:
        if not callable(connection_factory):
            raise ValueError("provider_model_lease_connection_factory_invalid")
        self.connection_factory = connection_factory

    @contextmanager
    def hold(self, model: str) -> Iterator[None]:
        model = str(model).strip()
        if not model:
            raise ProviderPoolReconcileError("provider_model_lease_model_invalid")
        connection = self.connection_factory()
        locked = False
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT pg_try_advisory_lock(%s, hashtext(%s))",
                    (_PROVIDER_SAGA_LOCK_NAMESPACE, model),
                )
                locked = _boolean_row(cursor)
            connection.commit()
            if not locked:
                raise ProviderPoolReconcileError("provider_model_lease_busy")
            yield
        finally:
            try:
                if locked:
                    with connection.cursor() as cursor:
                        cursor.execute(
                            "SELECT pg_advisory_unlock(%s, hashtext(%s))",
                            (_PROVIDER_SAGA_LOCK_NAMESPACE, model),
                        )
                        if not _boolean_row(cursor):
                            raise ProviderPoolReconcileError("provider_model_lease_release_failed")
                    connection.commit()
            finally:
                _close_connection(connection)


class PostgresProviderActivationBarrier:
    """Wait for pre-send readers, then fence activation and projection."""

    def __init__(self, connection_factory: Callable[[], Any]) -> None:
        if not callable(connection_factory):
            raise ValueError("provider_activation_barrier_connection_factory_invalid")
        self.connection_factory = connection_factory

    @contextmanager
    def hold(self, model: str) -> Iterator[None]:
        model = str(model).strip()
        if not model:
            raise ProviderPoolReconcileError("provider_activation_barrier_model_invalid")
        connection = self.connection_factory()
        locked = False
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT pg_advisory_lock(%s, hashtext(%s))",
                    (_PROVIDER_PRE_SEND_LOCK_NAMESPACE, model),
                )
                cursor.fetchone()
            connection.commit()
            locked = True
            yield
        finally:
            try:
                if locked:
                    with connection.cursor() as cursor:
                        cursor.execute(
                            "SELECT pg_advisory_unlock(%s, hashtext(%s))",
                            (_PROVIDER_PRE_SEND_LOCK_NAMESPACE, model),
                        )
                        if not _boolean_row(cursor):
                            raise ProviderPoolReconcileError(
                                "provider_activation_barrier_release_failed"
                            )
                    connection.commit()
            finally:
                _close_connection(connection)


@dataclass(frozen=True)
class ReconcileReport:
    model: str
    diff_kind: PoolDiffKind
    routing_generation: int
    cost_generation: int
    actions: tuple[str, ...]
    activated: bool
    dispatch_publication: object | None = None


class ActionClaimDisposition(str, Enum):
    CLAIMED = "claimed"
    COMPLETED = "completed"
    BUSY = "busy"


@dataclass(frozen=True)
class ProviderActionClaim:
    key: str
    disposition: ActionClaimDisposition
    action_id: int | None = None
    attempt_no: int | None = None


class ProviderActionLedger(Protocol):
    def key(
        self, bundle: ProviderBundle | ProviderActionScope, action: str, target: object
    ) -> str: ...

    def claim(
        self,
        bundle: ProviderBundle | ProviderActionScope,
        action: str,
        target: object,
    ) -> ProviderActionClaim: ...

    def complete(
        self,
        claim: ProviderActionClaim,
        *,
        observed_after: Mapping[str, Any],
        elapsed_ms: int,
    ) -> None: ...

    def fail(
        self,
        claim: ProviderActionClaim,
        *,
        error_class: str,
        elapsed_ms: int,
        observed_after: Mapping[str, Any] | None = None,
    ) -> None: ...


class IdempotencyLedger:
    """Process-local implementation of the Provider action ledger contract."""

    def __init__(self) -> None:
        self.completed: set[str] = set()
        self.in_progress: set[str] = set()

    def key(
        self,
        bundle: ProviderBundle | ProviderActionScope,
        action: str,
        target: object,
    ) -> str:
        document = {
            "config_version": bundle.config_version,
            "model": bundle.model,
            "routing_generation": bundle.routing_generation,
            "cost_generation": bundle.cost_generation,
            "action": action,
            "target": target,
        }
        digest = hashlib.sha256(
            json.dumps(document, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        scope_digest = hashlib.sha256(
            f"{bundle.config_version}\0{bundle.model}".encode()
        ).hexdigest()
        return (
            f"a6pp:{scope_digest[:16]}:r{bundle.routing_generation}:"
            f"c{bundle.cost_generation}:{action}:t{digest[:24]}"
        )

    def claim(
        self,
        bundle: ProviderBundle | ProviderActionScope,
        action: str,
        target: object,
    ) -> ProviderActionClaim:
        key = self.key(bundle, action, target)
        if key in self.completed:
            return ProviderActionClaim(key, ActionClaimDisposition.COMPLETED)
        if key in self.in_progress:
            return ProviderActionClaim(key, ActionClaimDisposition.BUSY)
        self.in_progress.add(key)
        return ProviderActionClaim(key, ActionClaimDisposition.CLAIMED)

    def complete(
        self,
        claim: ProviderActionClaim,
        *,
        observed_after: Mapping[str, Any],
        elapsed_ms: int,
    ) -> None:
        del observed_after, elapsed_ms
        if claim.disposition is not ActionClaimDisposition.CLAIMED:
            raise ProviderActionLedgerError("provider_action_ledger_claim_invalid")
        self.in_progress.discard(claim.key)
        self.completed.add(claim.key)

    def fail(
        self,
        claim: ProviderActionClaim,
        *,
        error_class: str,
        elapsed_ms: int,
        observed_after: Mapping[str, Any] | None = None,
    ) -> None:
        del error_class, elapsed_ms, observed_after
        if claim.disposition is ActionClaimDisposition.CLAIMED:
            self.in_progress.discard(claim.key)


class PostgresProviderActionLedger:
    """Persist Provider saga checkpoints in the shared trade control-action tables."""

    def __init__(
        self,
        connection_factory: Callable[[], Any],
        *,
        requested_by: str = "provider_pool_reconciler",
        stale_claim_seconds: int = 300,
    ) -> None:
        if not str(requested_by).strip():
            raise ValueError("provider_action_ledger_requested_by_invalid")
        if isinstance(stale_claim_seconds, bool) or stale_claim_seconds < 1:
            raise ValueError("provider_action_ledger_stale_claim_invalid")
        self.connection_factory = connection_factory
        self.requested_by = str(requested_by).strip()
        self.stale_claim_seconds = int(stale_claim_seconds)

    def key(
        self,
        bundle: ProviderBundle | ProviderActionScope,
        action: str,
        target: object,
    ) -> str:
        return IdempotencyLedger().key(bundle, action, target)

    def claim(
        self,
        bundle: ProviderBundle | ProviderActionScope,
        action: str,
        target: object,
    ) -> ProviderActionClaim:
        key = self.key(bundle, action, target)
        desired = _action_document(bundle, action, target)
        action_type = f"provider_pool_{action}"
        entity_type = "provider_model" if target == bundle.model else "provider_slot"
        entity_id = _action_entity_id(bundle, target)
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                # Idempotency-key formatting is an implementation detail. Reuse an
                # older release's checkpoint when its complete semantic scope is
                # identical, so a key-format change cannot replay remote writes.
                cursor.execute(
                    """
                    SELECT id, idempotency_key, status, desired_state,
                           status NOT IN ('started', 'applying')
                           OR updated_at <= now() - (%s * interval '1 second')
                               AS claimable
                    FROM a6_trade.trade_control_actions
                    WHERE action_type = %s
                      AND entity_type = %s
                      AND entity_id = %s
                      AND config_version = %s
                      AND desired_state = %s::jsonb
                    ORDER BY CASE
                                 WHEN status = 'completed' THEN 0
                                 WHEN status IN ('started', 'applying') THEN 1
                                 ELSE 2
                             END,
                             id
                    LIMIT 1
                    FOR UPDATE
                    """,
                    (
                        self.stale_claim_seconds,
                        action_type,
                        entity_type,
                        entity_id,
                        bundle.config_version,
                        _json(desired),
                    ),
                )
                row = _mapping_row(cursor)
                inserted = False
                if row is None:
                    cursor.execute(
                        """
                        INSERT INTO a6_trade.trade_control_actions (
                            idempotency_key, action_type, entity_type, entity_id,
                            desired_state, status, config_version, requested_by, started_at
                        ) VALUES (
                            %s, %s, %s, %s, %s::jsonb,
                            'started', %s, %s, now()
                        )
                        ON CONFLICT (idempotency_key) DO NOTHING
                        RETURNING id, idempotency_key, status, desired_state
                        """,
                        (
                            key,
                            action_type,
                            entity_type,
                            entity_id,
                            _json(desired),
                            bundle.config_version,
                            self.requested_by,
                        ),
                    )
                    row = _mapping_row(cursor)
                    inserted = row is not None
                if row is None:
                    # A concurrent claimant may have inserted the current key
                    # after the semantic lookup and before our INSERT.
                    cursor.execute(
                        """
                        SELECT id, idempotency_key, status, desired_state,
                               status NOT IN ('started', 'applying')
                               OR updated_at <= now() - (%s * interval '1 second')
                                   AS claimable
                        FROM a6_trade.trade_control_actions
                        WHERE idempotency_key = %s
                        FOR UPDATE
                        """,
                        (self.stale_claim_seconds, key),
                    )
                    row = _mapping_row(cursor)
                if row is None:
                    raise ProviderActionLedgerError("provider_action_ledger_claim_missing")
                if _normalized_json(row.get("desired_state")) != desired:
                    raise ProviderActionLedgerError("provider_action_ledger_desired_conflict")
                persisted_key = str(row.get("idempotency_key") or "").strip()
                if not persisted_key:
                    raise ProviderActionLedgerError("provider_action_ledger_key_missing")
                key = persisted_key
                if not inserted:
                    if str(row.get("status")) == "completed":
                        connection.commit()
                        return ProviderActionClaim(
                            key,
                            ActionClaimDisposition.COMPLETED,
                            action_id=int(row["id"]),
                        )
                    if not bool(row.get("claimable")):
                        connection.commit()
                        return ProviderActionClaim(
                            key,
                            ActionClaimDisposition.BUSY,
                            action_id=int(row["id"]),
                        )
                    stale_error = "provider_action_ledger_stale_reclaimed"
                    cursor.execute(
                        """
                        UPDATE a6_trade.trade_control_action_attempts
                        SET status = 'failed', error_class = %s,
                            error_fingerprint = %s, completed_at = now(),
                            elapsed_ms = greatest(
                                (extract(epoch FROM (now() - started_at)) * 1000)::bigint,
                                0
                            )
                        WHERE action_id = %s AND status = 'started'
                        """,
                        (
                            stale_error,
                            hashlib.sha256(stale_error.encode()).hexdigest(),
                            int(row["id"]),
                        ),
                    )
                    cursor.execute(
                        """
                        UPDATE a6_trade.trade_control_actions
                        SET status = 'started', error_class = NULL,
                            started_at = now(), completed_at = NULL,
                            elapsed_ms = NULL, updated_at = now()
                        WHERE id = %s
                        """,
                        (int(row["id"]),),
                    )
                action_id = int(row["id"])
                cursor.execute(
                    """
                    SELECT coalesce(max(attempt_no), 0) + 1 AS attempt_no
                    FROM a6_trade.trade_control_action_attempts
                    WHERE action_id = %s
                    """,
                    (action_id,),
                )
                attempt = _mapping_row(cursor)
                attempt_no = int((attempt or {}).get("attempt_no") or 1)
                cursor.execute(
                    """
                    INSERT INTO a6_trade.trade_control_action_attempts (
                        action_id, attempt_no, status, started_at
                    ) VALUES (%s, %s, 'started', now())
                    """,
                    (action_id, attempt_no),
                )
            connection.commit()
            return ProviderActionClaim(
                key,
                ActionClaimDisposition.CLAIMED,
                action_id=action_id,
                attempt_no=attempt_no,
            )
        except Exception:
            connection.rollback()
            raise
        finally:
            _close_connection(connection)

    def complete(
        self,
        claim: ProviderActionClaim,
        *,
        observed_after: Mapping[str, Any],
        elapsed_ms: int,
    ) -> None:
        self._finish(
            claim,
            status="completed",
            observed_after=observed_after,
            error_class=None,
            elapsed_ms=elapsed_ms,
        )

    def fail(
        self,
        claim: ProviderActionClaim,
        *,
        error_class: str,
        elapsed_ms: int,
        observed_after: Mapping[str, Any] | None = None,
    ) -> None:
        self._finish(
            claim,
            status="failed",
            observed_after=observed_after,
            error_class=error_class,
            elapsed_ms=elapsed_ms,
        )

    def _finish(
        self,
        claim: ProviderActionClaim,
        *,
        status: str,
        observed_after: Mapping[str, Any] | None,
        error_class: str | None,
        elapsed_ms: int,
    ) -> None:
        if (
            claim.disposition is not ActionClaimDisposition.CLAIMED
            or claim.action_id is None
            or claim.attempt_no is None
        ):
            raise ProviderActionLedgerError("provider_action_ledger_claim_invalid")
        elapsed_ms = max(int(elapsed_ms), 0)
        fingerprint = (
            hashlib.sha256(str(error_class).encode()).hexdigest()
            if error_class is not None
            else None
        )
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE a6_trade.trade_control_actions
                    SET observed_after = %s::jsonb, status = %s, error_class = %s,
                        completed_at = now(), elapsed_ms = %s, updated_at = now()
                    WHERE id = %s AND status = 'started'
                    """,
                    (
                        _json(observed_after) if observed_after is not None else None,
                        status,
                        error_class,
                        elapsed_ms,
                        claim.action_id,
                    ),
                )
                if cursor.rowcount != 1:
                    raise ProviderActionLedgerError("provider_action_ledger_finish_cas_mismatch")
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
                        fingerprint,
                        elapsed_ms,
                        claim.action_id,
                        claim.attempt_no,
                    ),
                )
                if cursor.rowcount != 1:
                    raise ProviderActionLedgerError(
                        "provider_action_ledger_attempt_finish_cas_mismatch"
                    )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            _close_connection(connection)


class ProviderPoolReconciler:
    def __init__(
        self,
        store: ProviderPoolStore,
        remote: ProviderPoolRemote,
        *,
        ledger: ProviderActionLedger | None = None,
        model_lease: ProviderModelLease | None = None,
        activation_barrier: ProviderActivationBarrier | None = None,
        activation_publisher: Callable[[str, int, int], object] | None = None,
        activation_compensator: Callable[[str, int, int], object] | None = None,
        request_plane_preflight: Callable[[str], object] | None = None,
        require_request_plane_preflight: bool = False,
        require_remote_readback: bool = False,
        activation_probe_required: bool = True,
        defer_membership_to_auto_pool: bool = False,
        remote_max_workers: int = 1,
        round_deadline: float | None = None,
    ) -> None:
        if not isinstance(require_remote_readback, bool):
            raise ValueError("provider_reconcile_readback_mode_invalid")
        if not isinstance(require_request_plane_preflight, bool):
            raise ValueError("provider_request_plane_preflight_mode_invalid")
        if not isinstance(activation_probe_required, bool):
            raise ValueError("provider_reconcile_probe_mode_invalid")
        if not isinstance(defer_membership_to_auto_pool, bool):
            raise ValueError("provider_reconcile_auto_pool_defer_mode_invalid")
        if (
            isinstance(remote_max_workers, bool)
            or not isinstance(remote_max_workers, int)
            or not 1 <= remote_max_workers <= 32
        ):
            raise ValueError("provider_reconcile_remote_workers_invalid")
        if round_deadline is not None and (
            isinstance(round_deadline, bool)
            or not isinstance(round_deadline, (int, float))
            or not math.isfinite(float(round_deadline))
        ):
            raise ValueError("provider_reconcile_round_deadline_invalid")
        self.store = store
        self.remote = remote
        self.ledger = ledger or IdempotencyLedger()
        self.model_lease = model_lease
        if activation_publisher is not None and activation_barrier is None:
            raise ValueError("provider_activation_publisher_barrier_missing")
        if activation_compensator is not None and activation_publisher is None:
            raise ValueError("provider_activation_compensator_publisher_missing")
        if activation_publisher is not None and activation_compensator is None:
            raise ValueError("provider_activation_compensator_missing")
        if defer_membership_to_auto_pool and (
            activation_barrier is None
            or activation_publisher is None
            or not callable(getattr(store, "hold_membership_for_auto_pool", None))
        ):
            raise ValueError("provider_reconcile_auto_pool_defer_contract_missing")
        if require_request_plane_preflight and request_plane_preflight is None:
            raise ValueError("provider_request_plane_preflight_missing")
        if request_plane_preflight is not None and not callable(request_plane_preflight):
            raise ValueError("provider_request_plane_preflight_invalid")
        self.activation_barrier = activation_barrier
        self.activation_publisher = activation_publisher
        self.activation_compensator = activation_compensator
        self.request_plane_preflight = request_plane_preflight
        self.require_request_plane_preflight = require_request_plane_preflight
        self.require_remote_readback = require_remote_readback
        self.activation_probe_required = activation_probe_required
        self.defer_membership_to_auto_pool = defer_membership_to_auto_pool
        self.remote_max_workers = remote_max_workers
        self.round_deadline = None if round_deadline is None else float(round_deadline)
        self._deadline_suspension_depth = 0

    def reconcile(
        self,
        bundle: ProviderBundle,
        *,
        current_routing: Sequence[int] = (),
        current_assignments: Mapping[int, str | int | None] | None = None,
        current_slots: Mapping[int, ProviderSlot] | None = None,
        current_cost_input_hash: str = "0" * 64,
        verified_slot_nos: Sequence[int] = (),
    ) -> ReconcileReport:
        def execute() -> ReconcileReport:
            parent = self._parent_bundle(bundle)
            self._remote_mutation_started = False
            try:
                return self._reconcile(
                    bundle,
                    current_routing=current_routing,
                    current_assignments=current_assignments,
                    current_slots=current_slots,
                    current_cost_input_hash=current_cost_input_hash,
                    verified_slot_nos=verified_slot_nos,
                )
            except ProviderReadbackRetryRequired:
                # The remote mutation ledger is idempotent and the candidate is still
                # fail-closed. Retrying this generation is safer and much cheaper than
                # rewriting the parent after a read-only timeout or readback mismatch.
                raise
            except ReplanRequired as exc:
                if self._remote_mutation_started and exc.replan_scope in {
                    "channel",
                    "liquidity",
                    "provider",
                }:
                    try:
                        with self._suspend_round_deadline():
                            self._restore_parent_after_failure(
                                bundle,
                                parent,
                                current_slots=current_slots,
                                cause=exc,
                            )
                    except ProviderPoolReconcileError as compensation_error:
                        if (
                            compensation_error.code
                            != "provider_reconcile_parent_compensation_failed"
                        ):
                            raise
                    if parent is not None:
                        raise ReplanRequired(
                            exc.code,
                            slot_no=exc.slot_no,
                            channel_id=exc.channel_id,
                            account_id=exc.account_id,
                            token_id=exc.token_id,
                            replan_scope=exc.replan_scope,
                            probe_observation=exc.probe_observation,
                            verified_slot_nos=(),
                        ) from exc
                raise
            except BaseException as exc:
                if self._remote_mutation_started:
                    with self._suspend_round_deadline():
                        self._restore_parent_after_failure(
                            bundle,
                            parent,
                            current_slots=current_slots,
                            cause=exc,
                        )
                raise
            finally:
                self._remote_mutation_started = False

        if self.model_lease is None:
            return execute()
        with self.model_lease.hold(bundle.model):
            return execute()

    def _parent_bundle(self, bundle: ProviderBundle) -> ProviderBundle | None:
        if bundle.parent_routing_generation is None:
            if bundle.parent_cost_generation is not None:
                raise ProviderPoolReconcileError(
                    "provider_reconcile_parent_generation_mismatch"
                )
            return None
        active_reader = getattr(self.store, "active", None)
        active = active_reader(bundle.model) if callable(active_reader) else None
        if isinstance(active, ProviderBundle):
            parent = active
        else:
            loader = getattr(self.store, "load_bundle", None)
            if not callable(loader):
                if self.require_remote_readback:
                    raise ProviderPoolReconcileError(
                        "provider_reconcile_parent_bundle_loader_missing"
                    )
                return None
            parent = loader(
                bundle.model,
                routing_generation=bundle.parent_routing_generation,
                cost_generation=bundle.parent_cost_generation,
            )
        if (
            not isinstance(parent, ProviderBundle)
            or parent.model != bundle.model
            or parent.routing_generation != bundle.parent_routing_generation
            or parent.cost_generation != bundle.parent_cost_generation
        ):
            raise ProviderPoolReconcileError(
                "provider_reconcile_parent_bundle_mismatch"
            )
        return parent

    def fail_applying(self, bundle: ProviderBundle, *, reason: str) -> None:
        """CAS-fail an unpublished generation before selecting another Pin."""

        reason = str(reason or "").strip()
        if not reason:
            raise ProviderPoolReconcileError("provider_reconcile_replan_reason_invalid")
        fail = getattr(self.store, "fail_applying", None)
        if not callable(fail):
            raise ProviderPoolReconcileError(
                "provider_reconcile_fail_applying_store_missing"
            )
        if self.model_lease is None:
            fail(bundle, reason=reason)
            return
        with self.model_lease.hold(bundle.model):
            fail(bundle, reason=reason)

    def _reconcile(
        self,
        bundle: ProviderBundle,
        *,
        current_routing: Sequence[int],
        current_assignments: Mapping[int, str | int | None] | None,
        current_slots: Mapping[int, ProviderSlot] | None,
        current_cost_input_hash: str,
        verified_slot_nos: Sequence[int],
    ) -> ReconcileReport:
        self._checkpoint()
        current_slot_map = _current_slot_map(current_slots)
        if current_assignments is None and current_slots is not None:
            current_assignments = {
                slot_no: slot.assignment_id for slot_no, slot in current_slot_map.items()
            }
        current = RoutingView(
            tuple(current_routing), current_assignments or {}, current_cost_input_hash
        )
        desired = RoutingView(
            bundle.ordered_channel_ids,
            {slot.slot_no: slot.assignment_id for slot in bundle.slots},
            bundle.effective_cost_input_hash,
        )
        diff = classify_pool_diff(current, desired)
        if diff is PoolDiffKind.NOOP:
            return ReconcileReport(
                bundle.model,
                diff,
                bundle.routing_generation,
                bundle.cost_generation,
                (),
                False,
            )
        if (
            diff
            in {
                PoolDiffKind.LIQUIDITY_REPLACEMENT,
                PoolDiffKind.MEMBERSHIP_AND_COST,
            }
            and current_slots is None
            and any(value is not None for value in current.assignments.values())
        ):
            raise ProviderPoolReconcileError("provider_reconcile_current_slots_required")
        if self.require_remote_readback and (
            not callable(getattr(self.remote, "readback", None))
            or not callable(getattr(self.remote, "read_slot_schedulable", None))
        ):
            raise ProviderPoolReconcileError("provider_reconcile_remote_readback_required")
        self._confirm_platform_backoff_clear(bundle.model)
        self._confirm_request_plane_capability(bundle.model)
        self.store.plan(
            bundle,
            expected_routing_generation=bundle.parent_routing_generation,
            expected_cost_generation=bundle.parent_cost_generation,
        )
        self.store.mark_applying(bundle)
        actions: list[str] = []
        desired_slot_map = {slot.slot_no: slot for slot in bundle.slots}
        verified_probe_slot_nos = _verified_probe_slot_nos(
            desired_slot_map,
            verified_slot_nos,
        )
        changed_slot_nos = _changed_slot_nos(current_slot_map, desired_slot_map)
        if current_slots is None:
            changed_slot_nos = {
                slot.slot_no for slot in bundle.slots if slot.slot_state is not SlotState.VACANT
            }
        deferred_slot_nos = (
            frozenset(
                slot_no
                for slot_no in changed_slot_nos
                if slot_no in desired_slot_map
                and desired_slot_map[slot_no].slot_state is not SlotState.VACANT
            )
            if self.defer_membership_to_auto_pool
            and diff
            in {
                PoolDiffKind.LIQUIDITY_REPLACEMENT,
                PoolDiffKind.MEMBERSHIP_AND_COST,
            }
            else frozenset()
        )
        transition_slots = _transition_remote_slots(
            current_slot_map,
            desired_slot_map,
            changed_slot_nos,
        )
        self._remote_mutation_started = True
        self._hold_all_schedulable_disabled(bundle, transition_slots, actions)
        if diff in {
            PoolDiffKind.LIQUIDITY_REPLACEMENT,
            PoolDiffKind.MEMBERSHIP_AND_COST,
        }:
            drained_identities: set[tuple[int, int]] = set()
            drain_work: list[tuple[str, object, Callable[[str], object]]] = []
            for slot_no in sorted(changed_slot_nos):
                previous = current_slot_map.get(slot_no)
                desired_slot = desired_slot_map.get(slot_no)
                for drain_candidate in (previous, desired_slot):
                    if drain_candidate is None or drain_candidate.slot_state is SlotState.VACANT:
                        continue
                    target = _drain_action_target(drain_candidate)
                    remote_identity = (target[1], target[2])
                    if remote_identity in drained_identities:
                        continue
                    drain_work.append(
                        (
                            "drain_slot",
                            target,
                            lambda key, slot=drain_candidate: self.remote.drain_slot(
                                slot, idempotency_key=key
                            ),
                        )
                    )
                    drained_identities.add(remote_identity)
            self._run_once_actions(bundle, drain_work, actions)
        self._once(
            bundle,
            "stage_billing_floor",
            bundle.model,
            lambda key: self.remote.stage_billing_floor(
                bundle.model,
                bundle.cost_plan.transition_floor_multiplier,
                idempotency_key=key,
            ),
            actions,
        )
        cost_work: list[tuple[str, object, Callable[[str], object]]] = []
        for slot in bundle.slots:
            if slot.slot_state is SlotState.VACANT:
                continue
            cost = bundle.cost_plan.desired_slot_costs.get(slot.slot_no)
            if cost is None:
                raise ProviderPoolReconcileError("provider_reconcile_slot_cost_missing")
            previous = current_slot_map.get(slot.slot_no)
            if (
                current_slots is not None
                and previous is not None
                and previous.observed_cost_multiplier == cost
                and slot.slot_no not in changed_slot_nos
            ):
                continue
            cost_work.append(
                (
                    "set_slot_cost",
                    slot.slot_no,
                    lambda key, slot=slot, cost=cost: self.remote.set_slot_cost(
                        slot, cost, idempotency_key=key
                    ),
                )
            )
        self._run_once_actions(bundle, cost_work, actions)

        if diff in {
            PoolDiffKind.LIQUIDITY_REPLACEMENT,
            PoolDiffKind.MEMBERSHIP_AND_COST,
        }:
            for slot_no in sorted(changed_slot_nos):
                slot = desired_slot_map.get(slot_no)
                if slot is None or slot.slot_state is SlotState.VACANT:
                    continue
                if slot_no in deferred_slot_nos:
                    continue

                def repin(key: str, slot: ProviderSlot = slot) -> bool:
                    return self._repin_slot_or_replan(
                        slot,
                        idempotency_key=key,
                        operation="pin",
                    )

                try:
                    self._once(
                        bundle,
                        "repin_slot",
                        slot.slot_no,
                        repin,
                        actions,
                    )
                except ReplanRequired as exc:
                    raise ReplanRequired(
                        exc.code,
                        slot_no=exc.slot_no,
                        channel_id=exc.channel_id,
                        account_id=exc.account_id,
                        token_id=exc.token_id,
                        replan_scope=exc.replan_scope,
                        probe_observation=exc.probe_observation,
                        verified_slot_nos=tuple(sorted(verified_probe_slot_nos)),
                    ) from exc

                if not self.activation_probe_required:
                    continue
                if slot.slot_no in verified_probe_slot_nos:
                    continue

                def probe(
                    key: str, slot: ProviderSlot = slot
                ) -> ProviderProbeResult:
                    try:
                        raw_result = self.remote.probe_slot(slot, idempotency_key=key)
                    except Exception as exc:
                        error_code = str(getattr(exc, "code", "") or exc).strip()
                        if error_code not in _REPLAN_PROBE_ERROR_CODES:
                            raise
                        observation = _probe_exception_observation(error_code, exc)
                        raise ReplanRequired(
                            "provider_reconcile_probe_failed",
                            slot_no=slot.slot_no,
                            channel_id=slot.channel_id,
                            account_id=slot.account_id,
                            token_id=slot.token_id,
                            replan_scope=str(observation["replan_scope"]),
                            probe_observation=observation,
                        ) from exc
                    result = _normalize_probe_result(raw_result)
                    if not result.confirmed:
                        raise ReplanRequired(
                            "provider_reconcile_probe_failed",
                            slot_no=slot.slot_no,
                            channel_id=slot.channel_id,
                            account_id=slot.account_id,
                            token_id=slot.token_id,
                            replan_scope=result.replan_scope,
                            probe_observation=result.observation(),
                        )
                    return result

                try:
                    self._once(
                        bundle,
                        "probe_slot",
                        slot.slot_no,
                        probe,
                        actions,
                    )
                except ReplanRequired as exc:
                    raise ReplanRequired(
                        exc.code,
                        slot_no=exc.slot_no,
                        channel_id=exc.channel_id,
                        account_id=exc.account_id,
                        token_id=exc.token_id,
                        replan_scope=exc.replan_scope,
                        probe_observation=exc.probe_observation,
                        verified_slot_nos=tuple(sorted(verified_probe_slot_nos)),
                    ) from exc
                verified_probe_slot_nos.add(slot.slot_no)

        if diff in {
            PoolDiffKind.PRIORITY_AND_COST,
            PoolDiffKind.MEMBERSHIP_AND_COST,
            PoolDiffKind.LIQUIDITY_REPLACEMENT,
        }:
            priority_slots: list[ProviderSlot] = []
            for slot in bundle.slots:
                if slot.slot_state is SlotState.VACANT:
                    continue
                previous = current_slot_map.get(slot.slot_no)
                if (
                    current_slots is not None
                    and previous is not None
                    and previous.priority == slot.priority
                    and slot.slot_no not in changed_slot_nos
                ):
                    continue
                priority_slots.append(slot)

            # Enodot exposes only single-account updates. Move every participant in a
            # multi-slot reorder into a disjoint interval before assigning final ranks,
            # so neither an intermediate response nor a resumed saga observes duplicate
            # production priorities.
            if len(priority_slots) > 1:
                stage_priority_work = [
                    (
                        "stage_priority",
                        (staged_slot.slot_no, staged_slot.priority),
                        lambda key, slot=staged_slot: self.remote.set_priority(
                            slot, idempotency_key=key
                        ),
                    )
                    for staged_slot in _temporary_priority_slots(bundle, priority_slots)
                ]
                self._run_once_actions(bundle, stage_priority_work, actions)

            self._run_once_actions(
                bundle,
                [
                    (
                        "set_priority",
                        slot.slot_no,
                        lambda key, slot=slot: self.remote.set_priority(
                            slot, idempotency_key=key
                        ),
                    )
                    for slot in priority_slots
                ],
                actions,
            )

        self._run_once_actions(
            bundle,
            [
                (
                    "set_assignment_metadata",
                    (slot.slot_no, slot.account_id, slot.token_id, slot.channel_id),
                    lambda key, slot=slot: self.remote.set_assignment_metadata(
                        bundle,
                        slot,
                        idempotency_key=key,
                    ),
                )
                for slot in bundle.ready_slots
                if slot.slot_no not in deferred_slot_nos
            ],
            actions,
        )

        pre_activation_schedulable = {
            # Changed slots must stay closed. An unchanged slot may already be
            # fail-closed from a previous interrupted attempt; either boolean is
            # safe here because every READY slot is strictly enabled after dispatch.
            slot.slot_no: False if slot.slot_no in changed_slot_nos else None
            for slot in bundle.ready_slots
        }
        try:
            (
                observed_output_hash,
                observed_billing_floor,
                prepared_readback_bundle,
            ) = self._confirm_remote(
                bundle,
                expected_schedulable=pre_activation_schedulable,
                deferred_slot_nos=deferred_slot_nos,
            )
        except (ProviderReadbackRetryRequired, ProviderRoundDeadlineExceeded):
            raise
        except ReplanRequired:
            raise
        except BaseException as exc:
            try:
                self._repair_pre_activation_readback(bundle, exc, actions)
            except ReplanRequired:
                raise
            except BaseException as repair_error:
                raise ProviderReadbackRetryRequired(repair_error) from repair_error
            raise ProviderReadbackRetryRequired(exc) from exc
        self.store.confirm_cost(
            bundle,
            observed_output_hash=observed_output_hash,
            observed_billing_floor=observed_billing_floor,
        )
        activation_bundle = _with_observed_schedulable(
            prepared_readback_bundle,
            enabled=True,
        )
        dispatch_publication: object | None = None
        activation_started = False
        activation_compensated = False
        barrier = (
            nullcontext()
            if self.activation_barrier is None
            else self.activation_barrier.hold(activation_bundle.model)
        )
        try:
            with barrier:
                try:
                    self.store.activate(
                        activation_bundle,
                        deferred_slot_nos=tuple(sorted(deferred_slot_nos)),
                    )
                    activation_started = True
                    if deferred_slot_nos:
                        actions.append("defer_membership_to_auto_pool")
                    if self.activation_publisher is not None:
                        dispatch_publication = self.activation_publisher(
                            activation_bundle.model,
                            activation_bundle.routing_generation,
                            activation_bundle.cost_generation,
                        )
                        _assert_activation_publication(
                            dispatch_publication,
                            activation_bundle,
                        )
                    self._run_slot_actions(
                        tuple(
                            slot
                            for slot in activation_bundle.ready_slots
                            if slot.slot_no not in deferred_slot_nos
                        ),
                        lambda slot, local_actions: self._set_schedulable_verified(
                            activation_bundle,
                            slot,
                            True,
                            action="enable_schedulable_after_dispatch",
                            actions=local_actions,
                        ),
                        actions,
                    )
                    # Zero-Probe activation already has one full pre-publication
                    # readback plus an exact per-slot enable readback. Repeating the
                    # full panel scan here only turns a transient GET into rollback.
                    if self.activation_probe_required:
                        self._confirm_remote(
                            activation_bundle,
                            expected_schedulable={
                                slot.slot_no: slot.slot_no not in deferred_slot_nos
                                for slot in activation_bundle.ready_slots
                            },
                            deferred_slot_nos=deferred_slot_nos,
                        )
                except BaseException as exc:
                    activation_compensated = True
                    with self._suspend_round_deadline():
                        self._compensate_activation(
                            activation_bundle,
                            transition_slots,
                            activation_started=activation_started,
                            cause=exc,
                        )
                    raise
        except BaseException as exc:
            if not activation_compensated:
                with self._suspend_round_deadline():
                    self._compensate_activation(
                        activation_bundle,
                        transition_slots,
                        activation_started=activation_started,
                        cause=exc,
                    )
            raise
        self._finalize_billing_floor(
            activation_bundle,
            ProviderActionScope(
                activation_bundle.config_version,
                activation_bundle.model,
                activation_bundle.routing_generation,
                activation_bundle.cost_generation,
            ),
            actions,
            recover_checkpoint=False,
        )
        return ReconcileReport(
            bundle.model,
            diff,
            bundle.routing_generation,
            bundle.cost_generation,
            tuple(actions),
            True,
            dispatch_publication,
        )

    def _repair_pre_activation_readback(
        self,
        bundle: ProviderBundle,
        cause: BaseException,
        actions: list[str],
    ) -> bool:
        """Repair all fields identified by one exact pre-activation readback."""

        if not isinstance(cause, ProviderPoolReconcileError) or cause.code not in {
            "provider_reconcile_slot_readback_mismatch",
            "provider_reconcile_assignment_metadata_readback_mismatch",
        }:
            return False
        diagnostics = cause.diagnostics
        if (
            diagnostics.get("model") != bundle.model
            or diagnostics.get("routing_generation") != bundle.routing_generation
            or diagnostics.get("cost_generation") != bundle.cost_generation
        ):
            raise ProviderPoolReconcileError(
                "provider_reconcile_readback_repair_identity_mismatch"
            )
        raw_mismatches = diagnostics.get("slot_mismatches")
        if raw_mismatches is None:
            mismatches: tuple[Mapping[str, Any], ...] = (diagnostics,)
        elif isinstance(raw_mismatches, Sequence) and not isinstance(
            raw_mismatches,
            (str, bytes),
        ):
            mismatches = tuple(
                mismatch
                for mismatch in raw_mismatches
                if isinstance(mismatch, Mapping)
            )
            if len(mismatches) != len(raw_mismatches) or not mismatches:
                raise ProviderPoolReconcileError(
                    "provider_reconcile_readback_repair_diagnostics_invalid"
                )
        else:
            raise ProviderPoolReconcileError(
                "provider_reconcile_readback_repair_diagnostics_invalid"
            )
        repair_attempt = time.time_ns()
        repaired = False
        for mismatch in mismatches:
            if cause.code == "provider_reconcile_slot_readback_mismatch":
                slot_repaired = self._repair_pre_activation_slot_readback(
                    bundle,
                    mismatch,
                    repair_attempt=repair_attempt,
                    actions=actions,
                )
            else:
                slot_repaired = self._repair_pre_activation_assignment_metadata(
                    bundle,
                    mismatch,
                    repair_attempt=repair_attempt,
                    actions=actions,
                )
            repaired = slot_repaired or repaired
        return repaired

    def _repair_pre_activation_assignment_metadata(
        self,
        bundle: ProviderBundle,
        diagnostics: Mapping[str, Any],
        *,
        repair_attempt: int,
        actions: list[str],
    ) -> bool:
        if (
            diagnostics.get("model") != bundle.model
            or diagnostics.get("routing_generation") != bundle.routing_generation
            or diagnostics.get("cost_generation") != bundle.cost_generation
        ):
            raise ProviderPoolReconcileError(
                "provider_reconcile_readback_repair_identity_mismatch"
            )
        slot_no = diagnostics.get("slot_no")
        if isinstance(slot_no, bool) or not isinstance(slot_no, int):
            raise ProviderPoolReconcileError(
                "provider_reconcile_readback_repair_slot_invalid"
            )
        slot = next(
            (candidate for candidate in bundle.ready_slots if candidate.slot_no == slot_no),
            None,
        )
        raw_fields = diagnostics.get("mismatch_fields")
        observed = diagnostics.get("observed")
        if (
            slot is None
            or not isinstance(raw_fields, list)
            or not raw_fields
            or not isinstance(observed, Mapping)
        ):
            raise ProviderPoolReconcileError(
                "provider_reconcile_readback_repair_diagnostics_invalid"
            )
        fields = tuple(sorted({str(field) for field in raw_fields}))
        if not fields:
            raise ProviderPoolReconcileError(
                "provider_reconcile_readback_repair_diagnostics_invalid"
            )

        self._set_schedulable_verified(
            bundle,
            slot,
            False,
            action="repair_readback_schedulable",
            actions=actions,
        )
        self._once(
            bundle,
            "repair_readback_assignment_metadata",
            (
                slot.slot_no,
                slot.account_id,
                slot.token_id,
                slot.channel_id,
                repair_attempt,
                fields,
            ),
            lambda key: self.remote.set_assignment_metadata(
                bundle,
                slot,
                idempotency_key=key,
            ),
            actions,
        )
        return True

    def _repair_pre_activation_slot_readback(
        self,
        bundle: ProviderBundle,
        diagnostics: Mapping[str, Any],
        *,
        repair_attempt: int,
        actions: list[str],
    ) -> bool:
        if (
            diagnostics.get("model") != bundle.model
            or diagnostics.get("routing_generation") != bundle.routing_generation
            or diagnostics.get("cost_generation") != bundle.cost_generation
        ):
            raise ProviderPoolReconcileError(
                "provider_reconcile_readback_repair_identity_mismatch"
            )
        slot_no = diagnostics.get("slot_no")
        if isinstance(slot_no, bool) or not isinstance(slot_no, int):
            raise ProviderPoolReconcileError(
                "provider_reconcile_readback_repair_slot_invalid"
            )
        slot = next(
            (candidate for candidate in bundle.ready_slots if candidate.slot_no == slot_no),
            None,
        )
        if slot is None:
            raise ProviderPoolReconcileError(
                "provider_reconcile_readback_repair_slot_invalid"
            )
        raw_fields = diagnostics.get("mismatch_fields")
        observed = diagnostics.get("observed")
        if not isinstance(raw_fields, list) or not isinstance(observed, Mapping):
            raise ProviderPoolReconcileError(
                "provider_reconcile_readback_repair_diagnostics_invalid"
            )
        fields = {str(field) for field in raw_fields}
        repairable = fields & {
            "token_id",
            "channel_id",
            "priority",
            "rate_multiplier",
            "schedulable",
            "pin_binding_id",
            "pin_confirmed",
        }
        if not repairable:
            return False

        self._set_schedulable_verified(
            bundle,
            slot,
            False,
            action="repair_readback_schedulable",
            actions=actions,
        )
        if repairable & {
            "token_id",
            "channel_id",
            "pin_binding_id",
            "pin_confirmed",
        }:
            self._once(
                bundle,
                "repair_readback_pin",
                (
                    slot.slot_no,
                    slot.account_id,
                    slot.token_id,
                    slot.channel_id,
                    repair_attempt,
                    observed.get("token_id"),
                    observed.get("channel_id"),
                ),
                lambda key: self._repin_slot_or_replan(
                    slot,
                    idempotency_key=key,
                    operation="repair_readback_pin",
                ),
                actions,
            )
        if "rate_multiplier" in repairable:
            desired_cost = bundle.cost_plan.desired_slot_costs.get(slot.slot_no)
            if desired_cost is None:
                raise ProviderPoolReconcileError(
                    "provider_reconcile_slot_cost_missing"
                )
            self._once(
                bundle,
                "repair_readback_cost",
                (
                    slot.slot_no,
                    repair_attempt,
                    format(desired_cost, "f"),
                    observed.get("rate_multiplier"),
                ),
                lambda key: self.remote.set_slot_cost(
                    slot,
                    desired_cost,
                    idempotency_key=key,
                ),
                actions,
            )
        if "priority" in repairable:
            self._once(
                bundle,
                "repair_readback_priority",
                (
                    slot.slot_no,
                    repair_attempt,
                    slot.priority,
                    observed.get("priority"),
                ),
                lambda key: self.remote.set_priority(slot, idempotency_key=key),
                actions,
            )
        return True

    def finalize_active_billing(
        self,
        bundle: ProviderBundle,
        *,
        routing_generation: int,
        cost_generation: int,
    ) -> tuple[str, ...]:
        """Resume the post-activation price-lowering checkpoint for a NOOP plan."""

        actions: list[str] = []
        scope = ProviderActionScope(
            bundle.config_version,
            bundle.model,
            routing_generation,
            cost_generation,
        )
        self._finalize_billing_floor(
            bundle,
            scope,
            actions,
            recover_checkpoint=True,
        )
        return tuple(actions)

    def _confirm_request_plane_capability(
        self,
        model: str,
    ) -> ProviderRequestPlaneCapability | None:
        if self.request_plane_preflight is None:
            if self.require_request_plane_preflight:
                raise ProviderPoolReconcileError(
                    "provider_request_plane_preflight_required"
                )
            return None
        try:
            document = self.request_plane_preflight(model)
        except ProviderPoolReconcileError:
            raise
        except Exception as exc:
            raise ProviderPoolReconcileError(
                "provider_request_plane_preflight_failed"
            ) from exc
        return ProviderRequestPlaneCapability.from_document(
            document,
            expected_model=model,
        )

    def _confirm_platform_backoff_clear(self, model: str) -> None:
        if not self.activation_probe_required:
            return
        checker = getattr(self.remote, "platform_backoff", None)
        if not callable(checker):
            return
        try:
            result = checker(model)
        except ProviderPoolReconcileError:
            raise
        except Exception as exc:
            raise ProviderPoolReconcileError(
                "provider_platform_backoff_preflight_failed"
            ) from exc
        if result is None:
            return
        if not isinstance(result, ProviderProbeResult):
            raise ProviderPoolReconcileError(
                "provider_platform_backoff_preflight_invalid"
            )
        if result.confirmed:
            return
        if (
            result.error_code != "provider_probe_backoff_active"
            or result.failure_owner != "platform"
            or result.replan_scope != "platform"
            or result.document.get("request_sent") is not False
        ):
            raise ProviderPoolReconcileError(
                "provider_platform_backoff_preflight_invalid"
            )
        raise ProviderPlatformBackoffActive(result.observation())

    def _hold_all_schedulable_disabled(
        self,
        bundle: ProviderBundle,
        slots: Sequence[ProviderSlot],
        actions: list[str],
    ) -> None:
        try:
            self._run_slot_actions(
                slots,
                lambda slot, local_actions: self._set_schedulable_verified(
                    bundle,
                    slot,
                    False,
                    action="hold_schedulable_disabled",
                    actions=local_actions,
                ),
                actions,
            )
        except BaseException as exc:
            compensation_errors = self._force_all_schedulable_disabled(bundle, slots)
            if compensation_errors:
                raise ProviderPoolReconcileError(
                    "provider_reconcile_remote_disable_compensation_failed"
                ) from exc
            raise

    def _set_schedulable_verified(
        self,
        bundle: ProviderBundle | ProviderActionScope,
        slot: ProviderSlot,
        enabled: bool,
        *,
        action: str,
        actions: list[str],
    ) -> None:
        target = (*_drain_action_target(slot), enabled)
        reader = getattr(self.remote, "read_slot_schedulable", None)
        if not callable(reader):
            self._once(
                bundle,
                action,
                target,
                lambda key: self.remote.set_schedulable(
                    slot,
                    enabled,
                    idempotency_key=key,
                ),
                actions,
            )
            return
        observed = self._read_schedulable(slot)
        if observed is enabled:
            self._complete_from_readback(
                bundle,
                action,
                target,
                observed_after={
                    "verified": True,
                    "recovered_from_readback": True,
                    "schedulable": enabled,
                },
            )
            return

        claim = self.ledger.claim(bundle, action, target)
        if claim.disposition is ActionClaimDisposition.BUSY:
            raise ProviderPoolReconcileError("provider_reconcile_action_in_progress")
        started = time.monotonic()
        try:
            changed = self.remote.set_schedulable(
                slot,
                enabled,
                idempotency_key=claim.key,
            )
            if self._read_schedulable(slot) is not enabled:
                raise ProviderPoolReconcileError(
                    "provider_reconcile_schedulable_readback_mismatch"
                )
        except BaseException as exc:
            if claim.disposition is ActionClaimDisposition.CLAIMED:
                self.ledger.fail(
                    claim,
                    error_class=f"provider_reconcile_{action}_failed",
                    elapsed_ms=_elapsed_ms(started),
                )
            if isinstance(exc, ProviderPoolReconcileError):
                raise
            raise ProviderPoolReconcileError(
                f"provider_reconcile_{action}_failed"
            ) from exc
        if claim.disposition is ActionClaimDisposition.CLAIMED:
            self.ledger.complete(
                claim,
                observed_after={
                    "verified": True,
                    "action": action,
                    "target": _json_value(target),
                    "schedulable": enabled,
                },
                elapsed_ms=_elapsed_ms(started),
            )
        if changed is not False:
            actions.append(action)

    def _read_schedulable(self, slot: ProviderSlot) -> bool:
        try:
            observed = self.remote.read_slot_schedulable(slot)
        except ProviderRoundDeadlineExceeded:
            raise
        except Exception as exc:
            raise ProviderPoolReconcileError(
                "provider_reconcile_schedulable_readback_failed"
            ) from exc
        if not isinstance(observed, bool):
            raise ProviderPoolReconcileError(
                "provider_reconcile_schedulable_readback_invalid"
            )
        return observed

    def _force_all_schedulable_disabled(
        self,
        bundle: ProviderBundle,
        slots: Sequence[ProviderSlot],
    ) -> tuple[BaseException, ...]:
        errors: list[BaseException] = []
        for slot in slots:
            target = (*_drain_action_target(slot), False)
            try:
                self.remote.set_schedulable(
                    slot,
                    False,
                    idempotency_key=self.ledger.key(
                        bundle,
                        "compensate_schedulable_disabled",
                        target,
                    ),
                )
                reader = getattr(self.remote, "read_slot_schedulable", None)
                if callable(reader) and self._read_schedulable(slot) is not False:
                    raise ProviderPoolReconcileError(
                        "provider_reconcile_schedulable_readback_mismatch"
                    )
            except BaseException as exc:
                errors.append(exc)
        return tuple(errors)

    def _compensate_activation(
        self,
        bundle: ProviderBundle,
        slots: Sequence[ProviderSlot],
        *,
        activation_started: bool,
        cause: BaseException,
    ) -> None:
        errors: list[BaseException] = list(
            self._force_all_schedulable_disabled(bundle, slots)
        )
        database_restored = not activation_started
        if activation_started:
            rollback = getattr(self.store, "rollback_activation", None)
            if not callable(rollback):
                errors.append(
                    ProviderPoolReconcileError(
                        "provider_reconcile_activation_rollback_missing"
                    )
                )
            else:
                try:
                    rollback(bundle)
                    database_restored = True
                except BaseException as exc:
                    errors.append(exc)
        if (
            activation_started
            and database_restored
            and self.activation_publisher is not None
            and self.activation_compensator is not None
        ):
            try:
                restored = self.activation_compensator(
                    bundle.model,
                    bundle.routing_generation,
                    bundle.cost_generation,
                )
                _assert_compensation_publication(restored, bundle)
            except BaseException as exc:
                errors.append(exc)
        if errors:
            raise ProviderPoolReconcileError(
                "provider_reconcile_activation_compensation_failed"
            ) from cause

    def _restore_parent_after_failure(
        self,
        candidate: ProviderBundle,
        parent: ProviderBundle | None,
        *,
        current_slots: Mapping[int, ProviderSlot] | None,
        cause: BaseException,
    ) -> None:
        if parent is None:
            return
        current_slot_map = _current_slot_map(current_slots)
        desired_slot_map = {slot.slot_no: slot for slot in candidate.slots}
        changed_slot_nos = _changed_slot_nos(current_slot_map, desired_slot_map)
        if current_slots is None:
            changed_slot_nos = {
                slot.slot_no
                for slot in candidate.slots
                if slot.slot_state is not SlotState.VACANT
            }
        transition_slots = _transition_remote_slots(
            current_slot_map,
            desired_slot_map,
            changed_slot_nos,
        )
        disable_errors = self._force_all_schedulable_disabled(
            candidate,
            transition_slots,
        )
        if disable_errors:
            raise ProviderPoolReconcileError(
                "provider_reconcile_parent_fail_closed_failed"
            ) from cause
        scope = ProviderActionScope(
            candidate.config_version,
            candidate.model,
            candidate.routing_generation,
            candidate.cost_generation,
        )
        actions: list[str] = []
        parent_identity = (parent.routing_generation, parent.cost_generation)
        # A later candidate repair can drift a field after an earlier compensation
        # checkpoint completed. Each compensation pass must therefore reassert state.
        compensation_attempt = time.time_ns()
        try:
            safe_floor = max(
                candidate.cost_plan.transition_floor_multiplier,
                parent.cost_plan.transition_floor_multiplier,
            )
            self._once(
                scope,
                "restore_parent_stage_billing_floor",
                (
                    *parent_identity,
                    compensation_attempt,
                    format(safe_floor, "f"),
                ),
                lambda key: self.remote.stage_billing_floor(
                    parent.model,
                    safe_floor,
                    idempotency_key=key,
                ),
                actions,
            )
            if self.remote.read_billing_floor(parent.model) < safe_floor:
                raise ProviderPoolReconcileError(
                    "provider_reconcile_parent_billing_readback_mismatch"
                )

            for slot in parent.ready_slots:
                cost = parent.cost_plan.desired_slot_costs.get(slot.slot_no)
                if cost is None:
                    raise ProviderPoolReconcileError(
                        "provider_reconcile_parent_slot_cost_missing"
                    )
                self._once(
                    scope,
                    "restore_parent_slot_cost",
                    (
                        *parent_identity,
                        compensation_attempt,
                        slot.slot_no,
                        format(cost, "f"),
                    ),
                    lambda key, slot=slot, cost=cost: self.remote.set_slot_cost(
                        slot,
                        cost,
                        idempotency_key=key,
                    ),
                    actions,
                )

            for slot in parent.ready_slots:
                if slot.slot_no not in changed_slot_nos:
                    continue
                self._once(
                    scope,
                    "restore_parent_pin",
                    (
                        *parent_identity,
                        compensation_attempt,
                        slot.slot_no,
                        slot.channel_id,
                    ),
                    lambda key, slot=slot: self.remote.repin_slot(
                        slot,
                        idempotency_key=key,
                    ),
                    actions,
                )

                if self.activation_probe_required:

                    def recovery_probe(
                        key: str,
                        slot: ProviderSlot = slot,
                    ) -> ProviderProbeResult:
                        method = getattr(self.remote, "probe_slot_for_recovery", None)
                        raw = (
                            method(slot, idempotency_key=key)
                            if callable(method)
                            else self.remote.probe_slot(slot, idempotency_key=key)
                        )
                        result = _normalize_probe_result(raw)
                        if not result.confirmed:
                            raise ProviderPoolReconcileError(
                                "provider_reconcile_parent_probe_failed"
                            )
                        return result

                    self._once(
                        scope,
                        "restore_parent_probe",
                        (
                            *parent_identity,
                            compensation_attempt,
                            slot.slot_no,
                            slot.channel_id,
                        ),
                        recovery_probe,
                        actions,
                    )

            for slot in parent.ready_slots:
                self._once(
                    scope,
                    "restore_parent_priority",
                    (
                        *parent_identity,
                        compensation_attempt,
                        slot.slot_no,
                        slot.priority,
                    ),
                    lambda key, slot=slot: self.remote.set_priority(
                        slot,
                        idempotency_key=key,
                    ),
                    actions,
                )
                self._once(
                    scope,
                    "restore_parent_assignment_metadata",
                    (
                        *parent_identity,
                        compensation_attempt,
                        slot.slot_no,
                        slot.account_id,
                        slot.token_id,
                    ),
                    lambda key, slot=slot: self.remote.set_assignment_metadata(
                        parent,
                        slot,
                        idempotency_key=key,
                    ),
                    actions,
                )

            for slot in parent.ready_slots:
                if slot.slot_no in changed_slot_nos:
                    continue
                self._set_schedulable_verified(
                    scope,
                    slot,
                    True,
                    action="restore_parent_schedulable",
                    actions=actions,
                )
            self._confirm_remote(
                parent,
                expected_schedulable={
                    slot.slot_no: slot.slot_no not in changed_slot_nos
                    for slot in parent.ready_slots
                },
            )
            for slot in parent.ready_slots:
                self._set_schedulable_verified(
                    scope,
                    slot,
                    True,
                    action="restore_parent_schedulable",
                    actions=actions,
                )
            self._confirm_remote(parent, expected_schedulable=True)

            final_floor = parent.cost_plan.billing_floor_multiplier
            self._once(
                scope,
                "restore_parent_billing_floor",
                (
                    *parent_identity,
                    compensation_attempt,
                    format(final_floor, "f"),
                ),
                lambda key: self.remote.finalize_billing_floor(
                    parent.model,
                    final_floor,
                    idempotency_key=key,
                ),
                actions,
            )
            if self.remote.read_billing_floor(parent.model) != final_floor:
                raise ProviderPoolReconcileError(
                    "provider_reconcile_parent_billing_readback_mismatch"
                )
        except BaseException as compensation_error:
            fail_closed_errors = self._force_all_schedulable_disabled(
                candidate,
                transition_slots,
            )
            if fail_closed_errors:
                raise ProviderPoolReconcileError(
                    "provider_reconcile_parent_fail_closed_failed"
                ) from compensation_error
            raise ProviderPoolReconcileError(
                "provider_reconcile_parent_compensation_failed"
            ) from compensation_error

    def _finalize_billing_floor(
        self,
        bundle: ProviderBundle,
        scope: ProviderActionScope,
        actions: list[str],
        *,
        recover_checkpoint: bool,
    ) -> None:
        desired = bundle.cost_plan.billing_floor_multiplier
        target = (bundle.model, format(desired, "f"))
        observed = self.remote.read_billing_floor(bundle.model)
        if observed < desired:
            raise ProviderPoolReconcileError("provider_reconcile_billing_final_readback_too_low")
        if observed != desired:
            self._once(
                scope,
                "finalize_billing_floor",
                target,
                lambda key: self.remote.finalize_billing_floor(
                    bundle.model,
                    desired,
                    idempotency_key=key,
                ),
                actions,
            )
            observed = self.remote.read_billing_floor(bundle.model)
            if observed != desired:
                raise ProviderPoolReconcileError(
                    "provider_reconcile_billing_final_readback_mismatch"
                )
        elif recover_checkpoint:
            self._complete_from_readback(
                scope,
                "finalize_billing_floor",
                target,
                observed_after={
                    "verified": True,
                    "recovered_from_readback": True,
                    "billing_floor_multiplier": format(observed, "f"),
                },
            )
        recorder = getattr(self.store, "record_billing_floor", None)
        if callable(recorder):
            recorder(
                bundle.model,
                cost_generation=scope.cost_generation,
                observed_billing_floor=observed,
            )

    def _complete_from_readback(
        self,
        scope: ProviderActionScope,
        action: str,
        target: object,
        *,
        observed_after: Mapping[str, Any],
    ) -> None:
        claim = self.ledger.claim(scope, action, target)
        if claim.disposition is ActionClaimDisposition.COMPLETED:
            return
        if claim.disposition is ActionClaimDisposition.BUSY:
            raise ProviderPoolReconcileError("provider_reconcile_action_in_progress")
        self.ledger.complete(claim, observed_after=observed_after, elapsed_ms=0)

    def _repin_slot_or_replan(
        self,
        slot: ProviderSlot,
        *,
        idempotency_key: str,
        operation: str,
    ) -> bool:
        try:
            return self.remote.repin_slot(slot, idempotency_key=idempotency_key)
        except ReplanRequired:
            raise
        except Exception as exc:
            scope, observation = _pin_exception_observation(exc, operation=operation)
            raise ReplanRequired(
                "provider_reconcile_pin_failed",
                slot_no=slot.slot_no,
                channel_id=slot.channel_id,
                account_id=slot.account_id,
                token_id=slot.token_id,
                replan_scope=scope,
                probe_observation=observation,
            ) from exc

    def _confirm_remote(
        self,
        bundle: ProviderBundle,
        *,
        expected_schedulable: bool | Mapping[int, bool | None],
        deferred_slot_nos: frozenset[int] = frozenset(),
    ) -> tuple[str, Decimal, ProviderBundle]:
        readback = getattr(self.remote, "readback", None)
        if not callable(readback):
            return (
                bundle.cost_plan.cost_output_hash,
                bundle.cost_plan.transition_floor_multiplier,
                bundle,
            )
        observed = readback(bundle, deferred_slot_nos=deferred_slot_nos)
        activation_bundle = _assert_remote_readback(
            bundle,
            observed,
            expected_schedulable=expected_schedulable,
            require_probe_confirmation=self.activation_probe_required,
            deferred_slot_nos=deferred_slot_nos,
        )
        return (
            bundle.cost_plan.cost_output_hash,
            observed.billing_floor_multiplier,
            activation_bundle,
        )

    def _checkpoint(self) -> None:
        if (
            self._deadline_suspension_depth == 0
            and self.round_deadline is not None
            and time.monotonic() >= self.round_deadline
        ):
            raise ProviderRoundDeadlineExceeded()

    @contextmanager
    def _suspend_round_deadline(self) -> Iterator[None]:
        self._deadline_suspension_depth += 1
        try:
            yield
        finally:
            self._deadline_suspension_depth -= 1

    def _run_once_actions(
        self,
        bundle: ProviderBundle | ProviderActionScope,
        work: Sequence[tuple[str, object, Callable[[str], object]]],
        actions: list[str],
    ) -> None:
        if not work:
            return
        self._checkpoint()
        if self.remote_max_workers == 1 or len(work) == 1:
            for action, target, call in work:
                self._once(bundle, action, target, call, actions)
            self._checkpoint()
            return

        def execute(
            item: tuple[str, object, Callable[[str], object]],
        ) -> tuple[str, ...]:
            local_actions: list[str] = []
            action, target, call = item
            self._once(bundle, action, target, call, local_actions)
            return tuple(local_actions)

        with ThreadPoolExecutor(
            max_workers=min(self.remote_max_workers, len(work)),
            thread_name_prefix="a6-provider-remote",
        ) as executor:
            futures = [executor.submit(execute, item) for item in work]
            for future in futures:
                actions.extend(future.result())
        self._checkpoint()

    def _run_slot_actions(
        self,
        slots: Sequence[ProviderSlot],
        call: Callable[[ProviderSlot, list[str]], None],
        actions: list[str],
    ) -> None:
        if not slots:
            return
        self._checkpoint()
        if self.remote_max_workers == 1 or len(slots) == 1:
            for slot in slots:
                call(slot, actions)
            self._checkpoint()
            return

        def execute(slot: ProviderSlot) -> tuple[str, ...]:
            local_actions: list[str] = []
            call(slot, local_actions)
            return tuple(local_actions)

        with ThreadPoolExecutor(
            max_workers=min(self.remote_max_workers, len(slots)),
            thread_name_prefix="a6-provider-slot",
        ) as executor:
            futures = [executor.submit(execute, slot) for slot in slots]
            for future in futures:
                actions.extend(future.result())
        self._checkpoint()

    def _once(
        self,
        bundle: ProviderBundle | ProviderActionScope,
        action: str,
        target: object,
        call: Any,
        actions: list[str],
    ) -> None:
        self._checkpoint()
        claim = self.ledger.claim(bundle, action, target)
        if claim.disposition is ActionClaimDisposition.COMPLETED:
            return
        if claim.disposition is ActionClaimDisposition.BUSY:
            raise ProviderPoolReconcileError("provider_reconcile_action_in_progress")
        started = time.monotonic()
        try:
            changed = call(claim.key)
        except ProviderRoundDeadlineExceeded as exc:
            self.ledger.fail(
                claim,
                error_class=exc.code,
                elapsed_ms=_elapsed_ms(started),
            )
            raise
        except ReplanRequired as exc:
            self.ledger.fail(
                claim,
                error_class=exc.code,
                elapsed_ms=_elapsed_ms(started),
                observed_after={
                    "verified": False,
                    "action": action,
                    "target": _json_value(target),
                    "probe": dict(exc.probe_observation),
                },
            )
            raise
        except Exception as exc:
            error_code = f"provider_reconcile_{action}_failed"
            self.ledger.fail(
                claim,
                error_class=error_code,
                elapsed_ms=_elapsed_ms(started),
            )
            raise ProviderPoolReconcileError(error_code) from exc
        observed_after: dict[str, Any] = {
            "verified": True,
            "action": action,
            "target": _json_value(target),
        }
        if isinstance(changed, ProviderProbeResult):
            observed_after["probe"] = changed.observation()
        self.ledger.complete(
            claim,
            observed_after=observed_after,
            elapsed_ms=_elapsed_ms(started),
        )
        if changed is not False:
            actions.append(action)


def _current_slot_map(
    current_slots: Mapping[int, ProviderSlot] | None,
) -> dict[int, ProviderSlot]:
    if current_slots is None:
        return {}
    result: dict[int, ProviderSlot] = {}
    for raw_slot_no, slot in current_slots.items():
        if isinstance(raw_slot_no, bool):
            raise ProviderPoolReconcileError("provider_reconcile_current_slot_invalid")
        try:
            slot_no = int(raw_slot_no)
        except (TypeError, ValueError):
            raise ProviderPoolReconcileError("provider_reconcile_current_slot_invalid") from None
        if slot_no < 1 or slot.slot_no != slot_no or slot_no in result:
            raise ProviderPoolReconcileError("provider_reconcile_current_slot_invalid")
        result[slot_no] = slot
    return result


def _related_remote_slots(
    current: Mapping[int, ProviderSlot],
    desired: Mapping[int, ProviderSlot],
) -> tuple[ProviderSlot, ...]:
    result: list[ProviderSlot] = []
    seen: set[tuple[int, int]] = set()
    for slot_map in (current, desired):
        for slot in sorted(slot_map.values(), key=lambda value: value.slot_no):
            if slot.slot_state is SlotState.VACANT:
                continue
            if slot.account_id is None or slot.token_id is None:
                raise ProviderPoolReconcileError(
                    "provider_reconcile_schedulable_identity_missing"
                )
            identity = (slot.account_id, slot.token_id)
            if identity in seen:
                continue
            seen.add(identity)
            result.append(slot)
    return tuple(result)


def _transition_remote_slots(
    current: Mapping[int, ProviderSlot],
    desired: Mapping[int, ProviderSlot],
    changed_slot_nos: set[int],
) -> tuple[ProviderSlot, ...]:
    return _related_remote_slots(
        {slot_no: slot for slot_no, slot in current.items() if slot_no in changed_slot_nos},
        {slot_no: slot for slot_no, slot in desired.items() if slot_no in changed_slot_nos},
    )


def _changed_slot_nos(
    current: Mapping[int, ProviderSlot], desired: Mapping[int, ProviderSlot]
) -> set[int]:
    changed: set[int] = set()
    for slot_no in set(current) | set(desired):
        previous = current.get(slot_no)
        target = desired.get(slot_no)
        previous_identity = (
            None
            if previous is None or previous.slot_state is SlotState.VACANT
            else (
                previous.assignment_id,
                previous.token_id,
                previous.account_id,
                previous.channel_id,
            )
        )
        target_identity = (
            None
            if target is None or target.slot_state is SlotState.VACANT
            else (
                target.assignment_id,
                target.token_id,
                target.account_id,
                target.channel_id,
            )
        )
        if previous_identity != target_identity:
            changed.add(slot_no)
    return changed


def _verified_probe_slot_nos(
    desired: Mapping[int, ProviderSlot],
    values: Sequence[int],
) -> set[int]:
    verified: set[int] = set()
    for value in values:
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or value < 1
            or value in verified
        ):
            raise ProviderPoolReconcileError("provider_reconcile_verified_slot_invalid")
        slot = desired.get(value)
        if slot is None or slot.slot_state is not SlotState.READY:
            raise ProviderPoolReconcileError("provider_reconcile_verified_slot_invalid")
        verified.add(value)
    return verified


def _normalize_probe_result(value: object) -> ProviderProbeResult:
    if isinstance(value, ProviderProbeResult):
        return value
    if value is True:
        return ProviderProbeResult(
            confirmed=True,
            protocol="unknown",
            http_status=200,
            error_code=None,
            failure_owner="none",
            replan_scope="none",
            document={"compatibility_result": True},
        )
    if value is False:
        return ProviderProbeResult(
            confirmed=False,
            protocol="unknown",
            http_status=None,
            error_code="provider_probe_unconfirmed",
            failure_owner="unknown",
            replan_scope="unknown",
            document={},
        )
    raise ProviderPoolReconcileError("provider_reconcile_probe_result_invalid")


def _probe_exception_observation(
    error_code: str,
    exc: Exception | None = None,
) -> dict[str, Any]:
    code = str(error_code or "provider_probe_failed").strip().casefold()
    owner = "platform" if "transport" in code else "unknown"
    scope = "platform" if owner == "platform" else "unknown"
    raw_diagnostics = getattr(exc, "diagnostics", None)
    diagnostics = dict(raw_diagnostics) if isinstance(raw_diagnostics, Mapping) else {}
    protocol = str(diagnostics.get("protocol") or "").strip().casefold()
    if protocol not in {"responses", "chat_completions"}:
        protocol = "chat_completions" if "chat" in code else "responses"
    raw_status = diagnostics.get("http_status")
    try:
        http_status = int(raw_status) if raw_status is not None else None
    except (TypeError, ValueError):
        http_status = None
    return ProviderProbeResult(
        confirmed=False,
        protocol=protocol,
        http_status=http_status,
        error_code=code,
        failure_owner=owner,
        replan_scope=scope,
        document={"transport": diagnostics} if diagnostics else {},
    ).observation()


def _pin_exception_observation(
    exc: Exception,
    *,
    operation: str,
) -> tuple[str, dict[str, Any]]:
    raw_error_code = str(getattr(exc, "code", "") or exc).strip().casefold()
    error_code = (
        raw_error_code
        if raw_error_code
        and len(raw_error_code) <= 128
        and all(character.isalnum() or character in "_.-" for character in raw_error_code)
        else "provider_pin_failed"
    )
    raw_provider_code = getattr(exc, "provider_error_code", None)
    provider_error_code = (
        str(raw_provider_code).strip().casefold()
        if raw_provider_code is not None
        else None
    )
    if provider_error_code not in _PIN_CHANNEL_ERROR_CODES:
        provider_error_code = (
            provider_error_code
            if provider_error_code
            and len(provider_error_code) <= 128
            and all(
                character.isalnum() or character in "_.-"
                for character in provider_error_code
            )
            else None
        )
    raw_status = getattr(exc, "status_code", None)
    try:
        http_status = int(raw_status) if raw_status is not None else None
    except (TypeError, ValueError):
        http_status = None
    if provider_error_code in _PIN_CHANNEL_ERROR_CODES or (
        error_code == "marketplace_pin_failed"
        and http_status in _PIN_CHANNEL_HTTP_STATUSES
    ):
        scope = "channel"
    elif (
        error_code in _PIN_PLATFORM_ERROR_CODES
        or http_status == 429
        or (http_status is not None and 500 <= http_status <= 599)
    ):
        scope = "platform"
    else:
        scope = "internal"
    document: dict[str, Any] = {
        "operation": str(operation),
        "request_sent": error_code != "provider_remote_repin_requires_drained_slot",
    }
    if provider_error_code is not None:
        document["provider_error_code"] = provider_error_code
    if (
        provider_error_code is None
        and error_code == "marketplace_pin_failed"
        and http_status in _PIN_CHANNEL_HTTP_STATUSES
    ):
        document["classification_source"] = "nontransient_pin_http_status"
    return scope, {
        "confirmed": False,
        "protocol": "unknown",
        "http_status": http_status,
        "error_code": error_code,
        "provider_error_code": provider_error_code,
        "failure_owner": scope,
        "replan_scope": scope,
        "document": document,
    }


def _drain_action_target(slot: ProviderSlot) -> tuple[int, int, int]:
    if slot.slot_state is SlotState.VACANT or slot.account_id is None or slot.token_id is None:
        raise ProviderPoolReconcileError("provider_reconcile_drain_identity_missing")
    return (slot.slot_no, slot.account_id, slot.token_id)


def _temporary_priority_slots(
    bundle: ProviderBundle,
    participants: Sequence[ProviderSlot],
) -> tuple[ProviderSlot, ...]:
    active = tuple(slot for slot in bundle.slots if slot.slot_state is not SlotState.VACANT)
    active_count = len(active)
    if active_count < 2:
        raise ProviderPoolReconcileError("provider_reconcile_priority_stage_invalid")

    offsets: set[int] = set()
    ranks: set[int] = set()
    final_priorities: set[int] = set()
    for slot in active:
        if slot.rank is None or slot.priority is None:
            raise ProviderPoolReconcileError("provider_reconcile_priority_stage_invalid")
        offsets.add(slot.priority - slot.rank)
        ranks.add(slot.rank)
        final_priorities.add(slot.priority)
    if (
        len(offsets) != 1
        or next(iter(offsets)) < 0
        or len(ranks) != active_count
        or len(final_priorities) != active_count
    ):
        raise ProviderPoolReconcileError("provider_reconcile_priority_stage_invalid")

    interval_base = max(final_priorities)
    staged: list[ProviderSlot] = []
    staged_priorities: set[int] = set()
    for slot in sorted(participants, key=lambda value: value.slot_no):
        # Sparse ranks (vacant slots between READY identities) make the old
        # active_count-based interval collide with final priorities. Anchor the
        # temporary interval strictly above every final priority instead; each
        # slot_no contributes a unique value inside the interval.
        temporary_priority = interval_base + slot.slot_no
        if (
            temporary_priority > 32767
            or temporary_priority in final_priorities
            or temporary_priority in staged_priorities
        ):
            raise ProviderPoolReconcileError("provider_reconcile_priority_stage_invalid")
        staged_priorities.add(temporary_priority)
        staged.append(replace(slot, priority=temporary_priority))
    return tuple(staged)


def _assert_remote_readback(
    bundle: ProviderBundle,
    observed: object,
    *,
    expected_schedulable: bool | Mapping[int, bool | None],
    require_probe_confirmation: bool = True,
    deferred_slot_nos: frozenset[int] = frozenset(),
) -> ProviderBundle:
    if not isinstance(require_probe_confirmation, bool):
        raise ProviderPoolReconcileError("provider_reconcile_probe_mode_invalid")
    expected_schedulable_by_slot = _expected_schedulable_by_slot(
        bundle,
        expected_schedulable,
    )
    if not isinstance(observed, ProviderPoolReadback):
        raise ProviderPoolReconcileError("provider_reconcile_remote_readback_invalid")
    if str(observed.model) != bundle.model:
        raise ProviderPoolReconcileError("provider_reconcile_readback_model_mismatch")
    if isinstance(observed.billing_floor_multiplier, bool):
        raise ProviderPoolReconcileError("provider_reconcile_billing_readback_invalid")
    try:
        billing_floor = Decimal(observed.billing_floor_multiplier)
    except (InvalidOperation, TypeError, ValueError):
        raise ProviderPoolReconcileError("provider_reconcile_billing_readback_invalid") from None
    if (
        not billing_floor.is_finite()
        or billing_floor < bundle.cost_plan.transition_floor_multiplier
    ):
        raise ProviderPoolReconcileError("provider_reconcile_billing_readback_mismatch")

    if not isinstance(observed.slots, Mapping):
        raise ProviderPoolReconcileError("provider_reconcile_slot_readback_invalid")
    readback_slots: dict[int, ProviderSlotReadback] = {}
    for raw_slot_no, value in observed.slots.items():
        if isinstance(raw_slot_no, bool) or not isinstance(value, ProviderSlotReadback):
            raise ProviderPoolReconcileError("provider_reconcile_slot_readback_invalid")
        try:
            slot_no = int(raw_slot_no)
        except (TypeError, ValueError):
            raise ProviderPoolReconcileError("provider_reconcile_slot_readback_invalid") from None
        if (
            slot_no < 1
            or isinstance(value.slot_no, bool)
            or not isinstance(value.slot_no, int)
            or value.slot_no != slot_no
            or slot_no in readback_slots
        ):
            raise ProviderPoolReconcileError("provider_reconcile_slot_readback_invalid")
        readback_slots[slot_no] = value

    seen_accounts: set[int] = set()
    seen_tokens: set[int] = set()
    desired_by_slot = {slot.slot_no: slot for slot in bundle.slots}
    for slot_no, value in readback_slots.items():
        desired = desired_by_slot.get(slot_no)
        if desired is None or desired.slot_state is SlotState.VACANT:
            if value.schedulable:
                raise ProviderPoolReconcileError("provider_reconcile_vacant_readback_schedulable")
            continue
        if value.account_id in seen_accounts or value.token_id in seen_tokens:
            raise ProviderPoolReconcileError("provider_reconcile_slot_readback_identity_duplicate")
        if value.account_id is not None:
            seen_accounts.add(value.account_id)
        if value.token_id is not None:
            seen_tokens.add(value.token_id)

    slot_mismatches: list[dict[str, Any]] = []
    metadata_mismatches: list[dict[str, Any]] = []
    for slot in bundle.slots:
        if slot.slot_state is SlotState.VACANT:
            continue
        value = readback_slots.get(slot.slot_no)
        if value is None:
            raise ProviderPoolReconcileError("provider_reconcile_slot_readback_missing")
        expected_cost = bundle.cost_plan.desired_slot_costs.get(slot.slot_no)
        if expected_cost is None:
            raise ProviderPoolReconcileError("provider_reconcile_slot_cost_missing")
        diagnostics = _slot_readback_diagnostics(
            bundle,
            slot,
            value,
            expected_cost=expected_cost,
            expected_schedulable=expected_schedulable_by_slot[slot.slot_no],
            require_probe_confirmation=require_probe_confirmation,
            deferred_slot_nos=deferred_slot_nos,
        )
        if diagnostics["mismatch_fields"]:
            slot_mismatches.append(diagnostics)
        try:
            if slot.slot_no in deferred_slot_nos:
                continue
            _assert_assignment_metadata(bundle, slot, value.assignment_metadata)
        except ProviderPoolReconcileError as exc:
            if exc.code != "provider_reconcile_assignment_metadata_readback_mismatch":
                raise
            metadata_mismatches.append(dict(exc.diagnostics))
    if slot_mismatches:
        combined = dict(slot_mismatches[0])
        combined["mismatch_slot_nos"] = [
            mismatch["slot_no"] for mismatch in slot_mismatches
        ]
        combined["slot_mismatches"] = slot_mismatches
        raise ProviderPoolReconcileError(
            "provider_reconcile_slot_readback_mismatch",
            diagnostics=combined,
        )
    if metadata_mismatches:
        combined = dict(metadata_mismatches[0])
        combined["mismatch_slot_nos"] = [
            mismatch["slot_no"] for mismatch in metadata_mismatches
        ]
        combined["slot_mismatches"] = metadata_mismatches
        raise ProviderPoolReconcileError(
            "provider_reconcile_assignment_metadata_readback_mismatch",
            diagnostics=combined,
        )
    return replace(
        bundle,
        slots=tuple(
            slot
            if slot.slot_state is SlotState.VACANT
            else replace(
                slot,
                # READY is the prospective active-state record. The actual remote
                # account remains disabled until DB/Redis publication is complete.
                # Deferred identities keep their planned READY shape here (the
                # dataclass contract requires Pin confirmation on READY); the
                # deferred-aware store activation writes them directly as
                # fail-closed REPAIRING with no Pin fields.
                observed_schedulable=True,
                observed_cost_multiplier=readback_slots[slot.slot_no].rate_multiplier,
                # Deferred identities are never pinned remotely; the readback
                # gate above verified the remote has no Pin. The planned READY
                # shape is preserved here because the dataclass contract and the
                # deferred-aware store activation both require it (the store
                # writes deferred slots as fail-closed REPAIRING with no Pin).
                confirmed_pin_binding_id=(
                    slot.confirmed_pin_binding_id
                    if slot.slot_no in deferred_slot_nos
                    else readback_slots[slot.slot_no].pin_binding_id
                ),
                pin_confirmed=(
                    slot.pin_confirmed
                    if slot.slot_no in deferred_slot_nos
                    else readback_slots[slot.slot_no].pin_confirmed
                ),
                # The persisted field predates Observer-driven activation. In
                # disabled mode it means the configured activation gate passed;
                # exact Pin readback is that gate and no network Probe was sent.
                probe_confirmed=(
                    slot.probe_confirmed
                    if slot.slot_no in deferred_slot_nos
                    else (
                        readback_slots[slot.slot_no].probe_confirmed
                        if require_probe_confirmation
                        else readback_slots[slot.slot_no].pin_confirmed
                    )
                ),
            )
            for slot in bundle.slots
        ),
    )


def _slot_readback_diagnostics(
    bundle: ProviderBundle,
    slot: ProviderSlot,
    observed: ProviderSlotReadback,
    *,
    expected_cost: Decimal,
    expected_schedulable: bool | None,
    require_probe_confirmation: bool,
    deferred_slot_nos: frozenset[int] = frozenset(),
) -> dict[str, Any]:
    deferred = slot.slot_no in deferred_slot_nos
    expected = {
        "account_id": slot.account_id,
        "token_id": None if deferred else slot.token_id,
        "channel_id": None if deferred else slot.channel_id,
        "priority": slot.priority,
        "rate_multiplier": format(expected_cost, "f"),
        "schedulable": (
            expected_schedulable if expected_schedulable is not None else "either_boolean"
        ),
        "pin_binding_id": "none" if deferred else "positive_int",
        "pin_confirmed": not deferred,
        "probe_confirmed": (
            "not_required" if not require_probe_confirmation else (not deferred)
        ),
    }
    actual = {
        "account_id": observed.account_id,
        "token_id": observed.token_id,
        "channel_id": observed.channel_id,
        "priority": observed.priority,
        "rate_multiplier": (
            format(observed.rate_multiplier, "f")
            if isinstance(observed.rate_multiplier, Decimal)
            else observed.rate_multiplier
        ),
        "schedulable": observed.schedulable,
        "pin_binding_id": observed.pin_binding_id,
        "pin_confirmed": observed.pin_confirmed,
        "probe_confirmed": observed.probe_confirmed,
    }
    mismatches: list[str] = []
    for field in ("account_id", "token_id", "channel_id", "priority"):
        value = actual[field]
        expected_value = expected[field]
        if expected_value is None:
            if value is not None:
                mismatches.append(field)
        elif type(value) is not int or value < 1 or value != expected_value:
            mismatches.append(field)
    if observed.rate_multiplier != expected_cost:
        mismatches.append("rate_multiplier")
    if expected_schedulable is not None and observed.schedulable is not expected_schedulable:
        mismatches.append("schedulable")
    if deferred:
        if observed.pin_binding_id is not None:
            mismatches.append("pin_binding_id")
        if observed.pin_confirmed is not False:
            mismatches.append("pin_confirmed")
    else:
        if type(observed.pin_binding_id) is not int or observed.pin_binding_id < 1:
            mismatches.append("pin_binding_id")
        if observed.pin_confirmed is not True:
            mismatches.append("pin_confirmed")
    if require_probe_confirmation and not deferred and observed.probe_confirmed is not True:
        mismatches.append("probe_confirmed")
    return {
        "model": bundle.model,
        "routing_generation": bundle.routing_generation,
        "cost_generation": bundle.cost_generation,
        "slot_no": slot.slot_no,
        "mismatch_fields": mismatches,
        "expected": expected,
        "observed": actual,
    }


def _expected_schedulable_by_slot(
    bundle: ProviderBundle,
    expected: bool | Mapping[int, bool | None],
) -> dict[int, bool | None]:
    ready_slot_nos = {slot.slot_no for slot in bundle.ready_slots}
    if isinstance(expected, bool):
        return {slot_no: expected for slot_no in ready_slot_nos}
    if not isinstance(expected, Mapping):
        raise ProviderPoolReconcileError(
            "provider_reconcile_expected_schedulable_invalid"
        )
    normalized: dict[int, bool | None] = {}
    for slot_no, enabled in expected.items():
        if (
            isinstance(slot_no, bool)
            or not isinstance(slot_no, int)
            or (enabled is not None and not isinstance(enabled, bool))
            or slot_no in normalized
        ):
            raise ProviderPoolReconcileError(
                "provider_reconcile_expected_schedulable_invalid"
            )
        normalized[slot_no] = enabled
    if set(normalized) != ready_slot_nos:
        raise ProviderPoolReconcileError(
            "provider_reconcile_expected_schedulable_invalid"
        )
    return normalized


def _with_observed_schedulable(
    bundle: ProviderBundle,
    *,
    enabled: bool,
) -> ProviderBundle:
    if not isinstance(enabled, bool):
        raise ProviderPoolReconcileError(
            "provider_reconcile_expected_schedulable_invalid"
        )
    return replace(
        bundle,
        slots=tuple(
            slot
            if slot.slot_state is SlotState.VACANT
            else replace(slot, observed_schedulable=enabled)
            for slot in bundle.slots
        ),
    )


def _assert_activation_publication(
    publication: object,
    bundle: ProviderBundle,
) -> None:
    key, digest, changed, routing, cost, state = _publication_fields(publication)
    if (
        not key
        or not _sha256_digest(digest)
        or not isinstance(changed, bool)
        or routing != bundle.routing_generation
        or cost != bundle.cost_generation
        or state != "active"
    ):
        raise ProviderPoolReconcileError(
            "provider_reconcile_dispatch_publication_mismatch"
        )


def _assert_compensation_publication(
    publication: object,
    bundle: ProviderBundle,
) -> None:
    key, digest, changed, routing, cost, state = _publication_fields(publication)
    expected_state = "active" if bundle.parent_routing_generation is not None else "absent"
    expected_routing = bundle.parent_routing_generation or 0
    expected_cost = bundle.parent_cost_generation or 0
    if (
        not key
        or not _sha256_digest(digest)
        or not isinstance(changed, bool)
        or routing != expected_routing
        or cost != expected_cost
        or state != expected_state
    ):
        raise ProviderPoolReconcileError(
            "provider_reconcile_dispatch_compensation_mismatch"
        )


def _publication_fields(
    publication: object,
) -> tuple[str, str, object, int, int, str]:
    try:
        key = str(publication.key)
        digest = str(publication.digest).casefold()
        changed = publication.changed
        routing = publication.routing_generation
        cost = publication.cost_generation
        state = str(publication.state)
    except AttributeError:
        raise ProviderPoolReconcileError(
            "provider_reconcile_dispatch_publication_invalid"
        ) from None
    if (
        isinstance(routing, bool)
        or not isinstance(routing, int)
        or routing < 0
        or isinstance(cost, bool)
        or not isinstance(cost, int)
        or cost < 0
    ):
        raise ProviderPoolReconcileError(
            "provider_reconcile_dispatch_publication_invalid"
        )
    return key, digest, changed, routing, cost, state


def _sha256_digest(value: object) -> bool:
    text = str(value)
    return len(text) == 64 and all(
        character in "0123456789abcdef" for character in text
    )


def _assert_assignment_metadata(
    bundle: ProviderBundle,
    slot: ProviderSlot,
    observed: object,
) -> None:
    if not isinstance(observed, Mapping):
        raise ProviderPoolReconcileError(
            "provider_reconcile_assignment_metadata_readback_invalid",
            diagnostics={
                "routing_generation": bundle.routing_generation,
                "cost_generation": bundle.cost_generation,
                "slot_no": slot.slot_no,
                "observed_type": type(observed).__name__,
            },
        )
    planned_cost = bundle.cost_plan.desired_slot_costs.get(slot.slot_no)
    if planned_cost is None:
        raise ProviderPoolReconcileError("provider_reconcile_slot_cost_missing")
    expected = {
        "a6_provider": True,
        "a6_provider_routing_generation": bundle.routing_generation,
        "a6_provider_cost_generation": bundle.cost_generation,
        "a6_provider_slot_no": slot.slot_no,
        "a6_provider_token_ref_id": slot.token_id,
        "a6_provider_channel_id": slot.channel_id,
        "a6_provider_rank": slot.rank,
        "a6_provider_cost_multiplier": format(planned_cost, "f"),
        "a6_provider_source_hash": bundle.source_hash,
        "a6_provider_cost_input_hash": bundle.effective_cost_input_hash,
        "a6_provider_cost_output_hash": bundle.cost_plan.cost_output_hash,
    }
    mismatches = [
        key
        for key, value in expected.items()
        if key not in observed
        or type(observed[key]) is not type(value)
        or observed[key] != value
    ]
    fingerprint = observed.get("a6_api_key_fingerprint")
    external_token_id = observed.get("external_a6_token_id")
    fingerprint_valid = (
        isinstance(fingerprint, str)
        and len(fingerprint) == 64
        and fingerprint == fingerprint.casefold()
        and all(character in "0123456789abcdef" for character in fingerprint)
    )
    external_token_valid = (
        not isinstance(external_token_id, bool)
        and isinstance(external_token_id, int)
        and external_token_id >= 1
    )
    if not fingerprint_valid:
        mismatches.append("a6_api_key_fingerprint")
    if not external_token_valid:
        mismatches.append("external_a6_token_id")
    if mismatches:
        observed_values = {key: observed.get(key) for key in expected}
        observed_values["external_a6_token_id"] = external_token_id
        observed_values["a6_api_key_fingerprint_valid"] = fingerprint_valid
        raise ProviderPoolReconcileError(
            "provider_reconcile_assignment_metadata_readback_mismatch",
            diagnostics={
                "model": bundle.model,
                "routing_generation": bundle.routing_generation,
                "cost_generation": bundle.cost_generation,
                "slot_no": slot.slot_no,
                "mismatch_fields": mismatches,
                "expected": expected,
                "observed": observed_values,
            },
        )


def _boolean_row(cursor: Any) -> bool:
    row = cursor.fetchone()
    if isinstance(row, Mapping):
        if len(row) != 1:
            raise ProviderPoolReconcileError("provider_model_lease_readback_invalid")
        value = next(iter(row.values()))
    elif isinstance(row, (tuple, list)) and len(row) == 1:
        value = row[0]
    else:
        raise ProviderPoolReconcileError("provider_model_lease_readback_invalid")
    if not isinstance(value, bool):
        raise ProviderPoolReconcileError("provider_model_lease_readback_invalid")
    return value


def _action_document(
    bundle: ProviderBundle | ProviderActionScope, action: str, target: object
) -> dict[str, Any]:
    return {
        "model": bundle.model,
        "routing_generation": bundle.routing_generation,
        "cost_generation": bundle.cost_generation,
        "action": str(action),
        "target": _json_value(target),
    }


def _action_entity_id(bundle: ProviderBundle | ProviderActionScope, target: object) -> str:
    if target == bundle.model:
        return bundle.model
    return f"{bundle.model}:{_json(target)}"


def _json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _json_value(value: object) -> Any:
    return json.loads(_json(value))


def _normalized_json(value: object) -> Any:
    if isinstance(value, str):
        return json.loads(value)
    return _json_value(value)


def _mapping_row(cursor: Any) -> dict[str, Any] | None:
    row = cursor.fetchone()
    if row is None:
        return None
    if isinstance(row, Mapping):
        return dict(row)
    names = [column.name for column in cursor.description or ()]
    if not names:
        raise ProviderActionLedgerError("provider_action_ledger_row_invalid")
    return dict(zip(names, row, strict=False))


def _close_connection(connection: Any) -> None:
    close = getattr(connection, "close", None)
    if callable(close):
        close()


def _elapsed_ms(started: float) -> int:
    return max(int((time.monotonic() - started) * 1000), 0)
