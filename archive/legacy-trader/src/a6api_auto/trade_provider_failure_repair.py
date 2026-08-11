"""Durable worker for failure-driven Provider pool repair.

The request plane records the failure and drains the authoritative slot in one
database transaction.  This worker then closes the remote side of that gap:
it verifies the Enodot account is unschedulable, optionally runs a bounded
probe, and delegates replacement activation to ``ProviderPoolReconciler``.
"""

from __future__ import annotations

import json
import re
import secrets
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Protocol

from .trade_provider_cost import PoolDiffKind
from .trade_provider_pool import ProviderSlot, SlotState
from .trade_provider_pool_apply import ProviderPoolApplyPlan
from .trade_provider_pool_reconciler import (
    ActionClaimDisposition,
    IdempotencyLedger,
    ProviderActionLedger,
    ProviderActionScope,
    ProviderModelLease,
    ProviderPoolReconciler,
)

_REPAIR_ACTIONS = frozenset({"repin_immediately", "probe_then_repin"})
_SAFE_WORKER_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,190}$")


class ProviderFailureRepairError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class ProviderFailureRepairTarget:
    routing_generation: int
    cost_generation: int
    slot_no: int
    channel_id: int
    account_id: int
    token_id: int
    source_hash: str

    def __post_init__(self) -> None:
        for value in (
            self.routing_generation,
            self.cost_generation,
            self.slot_no,
            self.channel_id,
            self.account_id,
            self.token_id,
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ProviderFailureRepairError("provider_failure_repair_target_invalid")
        if not re.fullmatch(r"[0-9a-f]{64}", self.source_hash):
            raise ProviderFailureRepairError("provider_failure_repair_target_hash_invalid")


@dataclass(frozen=True)
class ProviderFailureRepairClaim:
    request_id: int
    claim_token: str
    worker_id: str
    attempt_count: int
    attempt_id: str
    quarantine_id: int
    control_action: str
    config_version: str
    model: str
    routing_generation: int
    cost_generation: int
    current_slot: ProviderSlot
    target: ProviderFailureRepairTarget | None = None

    def __post_init__(self) -> None:
        identities = (
            self.request_id,
            self.attempt_count,
            self.quarantine_id,
            self.routing_generation,
            self.cost_generation,
        )
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 1
            for value in identities
        ):
            raise ProviderFailureRepairError("provider_failure_repair_claim_invalid")
        if self.control_action not in _REPAIR_ACTIONS:
            raise ProviderFailureRepairError("provider_failure_repair_action_invalid")
        if (
            not self.claim_token
            or not _SAFE_WORKER_ID.fullmatch(self.worker_id)
            or not self.attempt_id
            or not self.config_version
            or not self.model
        ):
            raise ProviderFailureRepairError("provider_failure_repair_claim_invalid")
        slot = self.current_slot
        if (
            slot.slot_no < 1
            or slot.slot_state is SlotState.VACANT
            or slot.account_id is None
            or slot.token_id is None
            or slot.channel_id is None
        ):
            raise ProviderFailureRepairError("provider_failure_repair_slot_invalid")

    @property
    def scope(self) -> ProviderActionScope:
        return ProviderActionScope(
            self.config_version,
            self.model,
            self.routing_generation,
            self.cost_generation,
        )


@dataclass(frozen=True)
class ProviderFailureRepairRun:
    status: str
    request_id: int | None = None
    control_action: str | None = None
    attempt_count: int = 0
    quick_probe_count: int = 0
    target: ProviderFailureRepairTarget | None = None
    error_class: str | None = None


@dataclass(frozen=True)
class ProviderFailureProbeRecovery:
    quick_probe_count: int
    unblock_projection_id: int
    deduplicated: bool = False

    def __post_init__(self) -> None:
        if (
            isinstance(self.quick_probe_count, bool)
            or not isinstance(self.quick_probe_count, int)
            or not 1 <= self.quick_probe_count <= 3
            or isinstance(self.unblock_projection_id, bool)
            or not isinstance(self.unblock_projection_id, int)
            or self.unblock_projection_id < 1
            or not isinstance(self.deduplicated, bool)
        ):
            raise ProviderFailureRepairError(
                "provider_failure_probe_recovery_invalid"
            )


class ProviderFailureRepairRemote(Protocol):
    def drain_slot(self, slot: ProviderSlot, *, idempotency_key: str) -> bool: ...

    def probe_slot(self, slot: ProviderSlot, *, idempotency_key: str) -> bool: ...

    def set_schedulable(
        self,
        slot: ProviderSlot,
        enabled: bool,
        *,
        idempotency_key: str,
    ) -> bool: ...

    def read_slot_schedulable(self, slot: ProviderSlot) -> bool: ...


class ProviderFailureRepairStore(Protocol):
    def claim_next(self, *, worker_id: str) -> ProviderFailureRepairClaim | None: ...

    def bind_target(
        self,
        claim: ProviderFailureRepairClaim,
        target: ProviderFailureRepairTarget,
    ) -> ProviderFailureRepairTarget: ...

    def read_active_target(
        self,
        claim: ProviderFailureRepairClaim,
    ) -> ProviderFailureRepairTarget | None: ...

    def read_probe_recovery(
        self,
        claim: ProviderFailureRepairClaim,
    ) -> ProviderFailureProbeRecovery | None: ...

    def complete_probe_recovery(
        self,
        claim: ProviderFailureRepairClaim,
        *,
        quick_probe_count: int,
    ) -> ProviderFailureProbeRecovery: ...

    def complete(
        self,
        claim: ProviderFailureRepairClaim,
        *,
        result: Mapping[str, Any],
    ) -> None: ...

    def fail(
        self,
        claim: ProviderFailureRepairClaim,
        *,
        error_class: str,
        retry_after_seconds: int,
        terminal: bool,
    ) -> None: ...


class ProviderFailureRepairPlanner(Protocol):
    def plan(self, claim: ProviderFailureRepairClaim) -> ProviderPoolApplyPlan: ...


class ProviderFailureReconcilerFactory(Protocol):
    def __call__(
        self,
        plan: ProviderPoolApplyPlan,
        ledger: ProviderActionLedger,
    ) -> ProviderPoolReconciler: ...


@dataclass(frozen=True)
class ProviderFailureRepairRuntime:
    remote: ProviderFailureRepairRemote
    reconciler_factory: ProviderFailureReconcilerFactory

    def __post_init__(self) -> None:
        remote_methods = (
            "drain_slot",
            "probe_slot",
            "set_schedulable",
            "read_slot_schedulable",
        )
        if any(not callable(getattr(self.remote, method, None)) for method in remote_methods):
            raise ProviderFailureRepairError("provider_failure_repair_remote_invalid")
        if not callable(self.reconciler_factory):
            raise ProviderFailureRepairError("provider_failure_repair_reconciler_factory_invalid")


class ProviderFailureRepairRuntimeFactory(Protocol):
    def __call__(
        self,
        claim: ProviderFailureRepairClaim,
        plan: ProviderPoolApplyPlan,
        ledger: ProviderActionLedger,
    ) -> ProviderFailureRepairRuntime: ...


class ProviderFailureProbeRemoteFactory(Protocol):
    def __call__(
        self,
        claim: ProviderFailureRepairClaim,
    ) -> ProviderFailureRepairRemote: ...


class ProviderFailureFastBlockReplayer(Protocol):
    def __call__(self) -> Mapping[str, Any]: ...


class PostgresProviderFailureRepairStore:
    """Materialize repairable failures and claim them with SKIP LOCKED."""

    def __init__(
        self,
        connection_factory: Callable[[], Any],
        *,
        stale_claim_seconds: int = 300,
        materialize_limit: int = 100,
    ) -> None:
        if not callable(connection_factory):
            raise ValueError("provider_failure_repair_connection_factory_invalid")
        if isinstance(stale_claim_seconds, bool) or not 1 <= stale_claim_seconds <= 86_400:
            raise ValueError("provider_failure_repair_stale_claim_invalid")
        if isinstance(materialize_limit, bool) or not 1 <= materialize_limit <= 1_000:
            raise ValueError("provider_failure_repair_materialize_limit_invalid")
        self.connection_factory = connection_factory
        self.stale_claim_seconds = int(stale_claim_seconds)
        self.materialize_limit = int(materialize_limit)

    def claim_next(self, *, worker_id: str) -> ProviderFailureRepairClaim | None:
        worker_id = _worker_id(worker_id)
        claim_token = secrets.token_hex(24)
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                self._supersede_stale(cursor)
                self._materialize(cursor)
                cursor.execute(
                    """
                    WITH candidate AS (
                        SELECT request.id
                        FROM a6_trade.provider_failure_repair_requests request
                        WHERE (
                                (
                                    request.status = 'pending'
                                    AND request.next_attempt_at <= now()
                                )
                                OR (
                                    request.status = 'running'
                                    AND request.claimed_at <= now()
                                        - (%s * interval '1 second')
                                )
                              )
                          AND (
                                EXISTS (
                                    SELECT 1
                                    FROM a6_trade.provider_pool_generations generation
                                    WHERE generation.model = request.model
                                      AND generation.generation = request.routing_generation
                                      AND generation.cost_generation = request.cost_generation
                                      AND generation.generation_state = 'active'
                                )
                                OR EXISTS (
                                    SELECT 1
                                    FROM a6_trade.provider_pool_generations generation
                                    JOIN a6_trade.provider_cost_generations cost
                                      ON cost.id = generation.cost_generation_id
                                     AND cost.model = request.model
                                     AND cost.cost_generation = request.target_cost_generation
                                     AND cost.pricing_state = 'active'
                                    JOIN a6_trade.provider_pool_slots slot
                                      ON slot.generation_id = generation.id
                                     AND slot.model = request.model
                                     AND slot.slot_no = request.slot_no
                                     AND slot.routing_generation = request.target_routing_generation
                                     AND slot.cost_generation = request.target_cost_generation
                                     AND slot.desired_channel_id = request.target_channel_id
                                     AND slot.enodot_account_id = request.target_account_id
                                     AND slot.a6_token_id = request.target_token_id
                                     AND slot.slot_state = 'READY'
                                     AND slot.funding_eligible
                                     AND slot.desired_schedulable
                                     AND slot.observed_schedulable
                                     AND slot.pin_confirmed
                                     AND slot.probe_confirmed
                                     AND slot.confirmed_pin_binding_id IS NOT NULL
                                     AND slot.valid_to IS NULL
                                    WHERE generation.model = request.model
                                      AND generation.generation = request.target_routing_generation
                                      AND generation.cost_generation =
                                          request.target_cost_generation
                                      AND generation.generation_state = 'active'
                                      AND generation.config_version = request.config_version
                                      AND generation.source_hash = request.target_source_hash
                                )
                              )
                        ORDER BY request.next_attempt_at, request.id
                        FOR UPDATE OF request SKIP LOCKED
                        LIMIT 1
                    )
                    UPDATE a6_trade.provider_failure_repair_requests request
                    SET status = 'running', claim_token = %s, claimed_by = %s,
                        claimed_at = now(), attempt_count = request.attempt_count + 1,
                        last_error_class = NULL, updated_at = now()
                    FROM candidate
                    WHERE request.id = candidate.id
                    RETURNING request.*
                    """,
                    (self.stale_claim_seconds, claim_token, worker_id),
                )
                row = _row(cursor)
            connection.commit()
            return None if row is None else _claim(row)
        except Exception:
            connection.rollback()
            raise
        finally:
            _close(connection)

    def bind_target(
        self,
        claim: ProviderFailureRepairClaim,
        target: ProviderFailureRepairTarget,
    ) -> ProviderFailureRepairTarget:
        if not isinstance(target, ProviderFailureRepairTarget):
            raise ProviderFailureRepairError("provider_failure_repair_target_invalid")
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE a6_trade.provider_failure_repair_requests
                    SET target_routing_generation = COALESCE(
                            target_routing_generation, %s
                        ),
                        target_cost_generation = COALESCE(target_cost_generation, %s),
                        target_channel_id = COALESCE(target_channel_id, %s),
                        target_account_id = COALESCE(target_account_id, %s),
                        target_token_id = COALESCE(target_token_id, %s),
                        target_source_hash = COALESCE(target_source_hash, %s),
                        updated_at = now()
                    WHERE id = %s AND status = 'running' AND claim_token = %s
                      AND (
                            target_routing_generation IS NULL
                            OR (
                                target_routing_generation = %s
                                AND target_cost_generation = %s
                                AND target_channel_id = %s
                                AND target_account_id = %s
                                AND target_token_id = %s
                                AND target_source_hash = %s
                            )
                          )
                    RETURNING target_routing_generation, target_cost_generation,
                              slot_no, target_channel_id, target_account_id,
                              target_token_id, target_source_hash
                    """,
                    (
                        target.routing_generation,
                        target.cost_generation,
                        target.channel_id,
                        target.account_id,
                        target.token_id,
                        target.source_hash,
                        claim.request_id,
                        claim.claim_token,
                        target.routing_generation,
                        target.cost_generation,
                        target.channel_id,
                        target.account_id,
                        target.token_id,
                        target.source_hash,
                    ),
                )
                row = _row(cursor)
                if row is None:
                    raise ProviderFailureRepairError(
                        "provider_failure_repair_target_bind_cas_mismatch"
                    )
            connection.commit()
            return _target(row)
        except Exception:
            connection.rollback()
            raise
        finally:
            _close(connection)

    def read_active_target(
        self,
        claim: ProviderFailureRepairClaim,
    ) -> ProviderFailureRepairTarget | None:
        if claim.target is None:
            return None
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT request.target_routing_generation,
                           request.target_cost_generation, request.slot_no,
                           request.target_channel_id, request.target_account_id,
                           request.target_token_id, request.target_source_hash
                    FROM a6_trade.provider_failure_repair_requests request
                    JOIN a6_trade.provider_pool_generations generation
                      ON generation.model = request.model
                     AND generation.generation = request.target_routing_generation
                     AND generation.cost_generation = request.target_cost_generation
                     AND generation.generation_state = 'active'
                     AND generation.config_version = request.config_version
                     AND generation.source_hash = request.target_source_hash
                    JOIN a6_trade.provider_cost_generations cost
                      ON cost.id = generation.cost_generation_id
                     AND cost.model = request.model
                     AND cost.cost_generation = request.target_cost_generation
                     AND cost.pricing_state = 'active'
                    JOIN a6_trade.provider_pool_slots slot
                      ON slot.generation_id = generation.id
                     AND slot.model = request.model
                     AND slot.slot_no = request.slot_no
                     AND slot.routing_generation = request.target_routing_generation
                     AND slot.cost_generation = request.target_cost_generation
                     AND slot.desired_channel_id = request.target_channel_id
                     AND slot.enodot_account_id = request.target_account_id
                     AND slot.a6_token_id = request.target_token_id
                     AND slot.slot_state = 'READY'
                     AND slot.funding_eligible
                     AND slot.desired_schedulable
                     AND slot.observed_schedulable
                     AND slot.pin_confirmed
                     AND slot.probe_confirmed
                     AND slot.confirmed_pin_binding_id IS NOT NULL
                     AND slot.valid_to IS NULL
                    WHERE request.id = %s AND request.status = 'running'
                      AND request.claim_token = %s
                    """,
                    (claim.request_id, claim.claim_token),
                )
                row = _row(cursor)
            connection.commit()
            return None if row is None else _target(row)
        except Exception:
            connection.rollback()
            raise
        finally:
            _close(connection)

    def read_probe_recovery(
        self,
        claim: ProviderFailureRepairClaim,
    ) -> ProviderFailureProbeRecovery | None:
        if claim.control_action != "probe_then_repin":
            return None
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                recovery = self._read_probe_recovery_row(cursor, claim)
            connection.commit()
            return recovery
        except Exception:
            connection.rollback()
            raise
        finally:
            _close(connection)

    def complete_probe_recovery(
        self,
        claim: ProviderFailureRepairClaim,
        *,
        quick_probe_count: int,
    ) -> ProviderFailureProbeRecovery:
        if claim.control_action != "probe_then_repin" or claim.target is not None:
            raise ProviderFailureRepairError(
                "provider_failure_probe_recovery_claim_invalid"
            )
        quick_probe_count = _bounded_integer(
            quick_probe_count,
            minimum=1,
            maximum=3,
            code="provider_failure_probe_recovery_count_invalid",
        )
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                recovered = self._read_probe_recovery_row(cursor, claim)
                if recovered is not None:
                    connection.commit()
                    return ProviderFailureProbeRecovery(
                        recovered.quick_probe_count,
                        recovered.unblock_projection_id,
                        deduplicated=True,
                    )

                cursor.execute(
                    """
                    SELECT request.id AS request_id,
                           quarantine.id AS quarantine_id,
                           quarantine.state AS quarantine_state,
                           slot.id AS slot_id, slot.state_version,
                           slot.slot_state, slot.desired_schedulable,
                           slot.observed_schedulable
                    FROM a6_trade.provider_failure_repair_requests request
                    JOIN a6_trade.provider_quarantines quarantine
                      ON quarantine.id = request.quarantine_id
                     AND quarantine.model = request.model
                     AND quarantine.routing_generation = request.routing_generation
                     AND quarantine.cost_generation = request.cost_generation
                     AND quarantine.slot_no = request.slot_no
                     AND quarantine.channel_id = request.failed_channel_id
                     AND quarantine.control_action = request.control_action
                    JOIN a6_trade.provider_pool_generations generation
                      ON generation.model = request.model
                     AND generation.generation = request.routing_generation
                     AND generation.cost_generation = request.cost_generation
                     AND generation.generation_state = 'active'
                     AND generation.config_version = request.config_version
                    JOIN a6_trade.provider_pool_slots slot
                      ON slot.generation_id = generation.id
                     AND slot.model = request.model
                     AND slot.routing_generation = request.routing_generation
                     AND slot.cost_generation = request.cost_generation
                     AND slot.slot_no = request.slot_no
                     AND slot.enodot_binding_id = request.enodot_binding_id
                     AND slot.enodot_account_id = request.enodot_account_id
                     AND slot.a6_token_id = request.a6_token_id
                     AND slot.desired_channel_id = request.failed_channel_id
                     AND slot.state_version = request.slot_state_version
                     AND slot.valid_to IS NULL
                    WHERE request.id = %s
                      AND request.status = 'running'
                      AND request.claim_token = %s
                      AND request.control_action = 'probe_then_repin'
                      AND request.target_routing_generation IS NULL
                      AND request.target_cost_generation IS NULL
                      AND request.target_channel_id IS NULL
                      AND request.target_account_id IS NULL
                      AND request.target_token_id IS NULL
                      AND request.target_source_hash IS NULL
                    FOR UPDATE OF request, quarantine, slot
                    """,
                    (claim.request_id, claim.claim_token),
                )
                locked = _row(cursor)
                if (
                    locked is None
                    or locked.get("quarantine_state") != "active"
                    or locked.get("slot_state") != "DRAINING"
                    or locked.get("desired_schedulable") is not False
                    or locked.get("observed_schedulable") is not False
                ):
                    raise ProviderFailureRepairError(
                        "provider_failure_probe_recovery_state_conflict"
                    )

                cursor.execute(
                    """
                    UPDATE a6_trade.provider_quarantines
                    SET state = 'probing', last_probe_at = now(),
                        last_probe_result = 'pending', updated_at = now()
                    WHERE id = %s AND state = 'active'
                      AND model = %s
                      AND routing_generation = %s
                      AND cost_generation = %s
                      AND slot_no = %s
                      AND channel_id = %s
                      AND control_action = 'probe_then_repin'
                    RETURNING id, state
                    """,
                    (
                        claim.quarantine_id,
                        claim.model,
                        claim.routing_generation,
                        claim.cost_generation,
                        claim.current_slot.slot_no,
                        claim.current_slot.channel_id,
                    ),
                )
                probing = _row(cursor)
                if probing is None or probing.get("state") != "probing":
                    raise ProviderFailureRepairError(
                        "provider_failure_probe_recovery_probe_cas_mismatch"
                    )

                cursor.execute(
                    """
                    UPDATE a6_trade.provider_pool_slots slot
                    SET desired_schedulable = true,
                        observed_schedulable = true,
                        slot_state = 'READY',
                        pin_confirmed = true,
                        probe_confirmed = true,
                        state_version = slot.state_version + 1,
                        last_success_at = now(), updated_at = now()
                    WHERE slot.id = %s
                      AND slot.state_version = %s
                      AND slot.valid_to IS NULL
                      AND slot.slot_state = 'DRAINING'
                      AND NOT slot.desired_schedulable
                      AND NOT slot.observed_schedulable
                      AND slot.funding_eligible
                      AND slot.liquidity_state = 'FUNDED'
                      AND slot.confirmed_pin_binding_id = %s
                      AND slot.pin_confirmed
                      AND slot.observed_rate_multiplier =
                          slot.desired_slot_cost_multiplier
                    RETURNING slot.id, slot.slot_state,
                              slot.desired_schedulable,
                              slot.observed_schedulable, slot.state_version
                    """,
                    (
                        int(locked["slot_id"]),
                        claim.current_slot.state_version,
                        claim.current_slot.confirmed_pin_binding_id,
                    ),
                )
                restored = _row(cursor)
                if (
                    restored is None
                    or restored.get("slot_state") != "READY"
                    or restored.get("desired_schedulable") is not True
                    or restored.get("observed_schedulable") is not True
                ):
                    raise ProviderFailureRepairError(
                        "provider_failure_probe_recovery_slot_cas_mismatch"
                    )

                evidence = _probe_recovery_evidence(claim, quick_probe_count)
                cursor.execute(
                    """
                    UPDATE a6_trade.provider_quarantines
                    SET state = 'closed', ends_at = now(),
                        recovery_verified_at = now(),
                        recovery_evidence = %s::jsonb,
                        last_probe_result = 'verified', updated_at = now()
                    WHERE id = %s AND state = 'probing'
                    RETURNING id, state
                    """,
                    (_json(evidence), claim.quarantine_id),
                )
                closed = _row(cursor)
                if closed is None or closed.get("state") != "closed":
                    raise ProviderFailureRepairError(
                        "provider_failure_probe_recovery_close_cas_mismatch"
                    )

                projection = self._upsert_probe_unblock(
                    cursor,
                    claim,
                    quick_probe_count=quick_probe_count,
                )
                result = _probe_recovery_result(
                    claim,
                    quick_probe_count=quick_probe_count,
                    unblock_projection_id=int(projection["id"]),
                )
                self._complete_row(cursor, claim, result)
                completed = self._read_probe_recovery_row(cursor, claim)
                if completed is None:
                    raise ProviderFailureRepairError(
                        "provider_failure_probe_recovery_readback_mismatch"
                    )
            connection.commit()
            return completed
        except Exception:
            connection.rollback()
            raise
        finally:
            _close(connection)

    @staticmethod
    def _upsert_probe_unblock(
        cursor: Any,
        claim: ProviderFailureRepairClaim,
        *,
        quick_probe_count: int,
    ) -> dict[str, Any]:
        idempotency_key = _probe_unblock_idempotency_key(claim)
        projection_key = _fast_block_projection_key(claim)
        payload = _probe_unblock_payload(claim, quick_probe_count)
        cursor.execute(
            """
            INSERT INTO a6_trade.provider_fast_block_outbox (
                idempotency_key, operation, model, routing_generation,
                cost_generation, slot_no, channel_id, quarantine_id,
                projection_key, payload, recovery_verified_at
            ) VALUES (
                %s, 'unblock', %s, %s, %s, %s, %s, %s, %s, %s::jsonb, now()
            )
            ON CONFLICT (idempotency_key) DO NOTHING
            RETURNING id, idempotency_key, operation, model,
                      routing_generation, cost_generation, slot_no, channel_id,
                      attempt_id, quarantine_id, projection_key, payload,
                      projection_state, failure_occurred_at,
                      recovery_verified_at
            """,
            (
                idempotency_key,
                claim.model,
                claim.routing_generation,
                claim.cost_generation,
                claim.current_slot.slot_no,
                claim.current_slot.channel_id,
                claim.quarantine_id,
                projection_key,
                _json(payload),
            ),
        )
        row = _row(cursor)
        if row is None:
            cursor.execute(
                """
                SELECT id, idempotency_key, operation, model,
                       routing_generation, cost_generation, slot_no, channel_id,
                       attempt_id, quarantine_id, projection_key, payload,
                       projection_state, failure_occurred_at,
                       recovery_verified_at
                FROM a6_trade.provider_fast_block_outbox
                WHERE idempotency_key = %s
                FOR SHARE
                """,
                (idempotency_key,),
            )
            row = _row(cursor)
        expected = {
            "idempotency_key": idempotency_key,
            "operation": "unblock",
            "model": claim.model,
            "routing_generation": claim.routing_generation,
            "cost_generation": claim.cost_generation,
            "slot_no": claim.current_slot.slot_no,
            "channel_id": claim.current_slot.channel_id,
            "attempt_id": None,
            "quarantine_id": claim.quarantine_id,
            "projection_key": projection_key,
            "payload": payload,
            "failure_occurred_at": None,
        }
        if row is None or any(
            _normalized_json(row.get(name)) != _normalized_json(value)
            for name, value in expected.items()
        ):
            raise ProviderFailureRepairError(
                "provider_failure_probe_recovery_unblock_conflict"
            )
        return row

    @staticmethod
    def _read_probe_recovery_row(
        cursor: Any,
        claim: ProviderFailureRepairClaim,
    ) -> ProviderFailureProbeRecovery | None:
        cursor.execute(
            """
            SELECT request.result,
                   projection.id AS unblock_projection_id
            FROM a6_trade.provider_failure_repair_requests request
            JOIN a6_trade.provider_quarantines quarantine
              ON quarantine.id = request.quarantine_id
             AND quarantine.model = request.model
             AND quarantine.routing_generation = request.routing_generation
             AND quarantine.cost_generation = request.cost_generation
             AND quarantine.slot_no = request.slot_no
             AND quarantine.channel_id = request.failed_channel_id
             AND quarantine.control_action = request.control_action
             AND quarantine.state = 'closed'
            JOIN a6_trade.provider_pool_generations generation
              ON generation.model = request.model
             AND generation.generation = request.routing_generation
             AND generation.cost_generation = request.cost_generation
             AND generation.generation_state = 'active'
             AND generation.config_version = request.config_version
            JOIN a6_trade.provider_pool_slots slot
              ON slot.generation_id = generation.id
             AND slot.model = request.model
             AND slot.routing_generation = request.routing_generation
             AND slot.cost_generation = request.cost_generation
             AND slot.slot_no = request.slot_no
             AND slot.enodot_binding_id = request.enodot_binding_id
             AND slot.enodot_account_id = request.enodot_account_id
             AND slot.a6_token_id = request.a6_token_id
             AND slot.desired_channel_id = request.failed_channel_id
             AND slot.slot_state = 'READY'
             AND slot.desired_schedulable
             AND slot.observed_schedulable
             AND slot.pin_confirmed
             AND slot.probe_confirmed
             AND slot.confirmed_pin_binding_id = request.confirmed_pin_binding_id
             AND slot.valid_to IS NULL
            JOIN a6_trade.provider_fast_block_outbox projection
              ON projection.idempotency_key = %s
             AND projection.operation = 'unblock'
             AND projection.model = request.model
             AND projection.routing_generation = request.routing_generation
             AND projection.cost_generation = request.cost_generation
             AND projection.slot_no = request.slot_no
             AND projection.channel_id = request.failed_channel_id
             AND projection.quarantine_id = request.quarantine_id
             AND projection.projection_key = %s
             AND projection.attempt_id IS NULL
             AND projection.failure_occurred_at IS NULL
             AND projection.recovery_verified_at IS NOT NULL
            WHERE request.id = %s
              AND request.status = 'completed'
              AND request.control_action = 'probe_then_repin'
              AND request.target_routing_generation IS NULL
              AND request.target_cost_generation IS NULL
              AND request.target_channel_id IS NULL
              AND request.target_account_id IS NULL
              AND request.target_token_id IS NULL
              AND request.target_source_hash IS NULL
            """,
            (
                _probe_unblock_idempotency_key(claim),
                _fast_block_projection_key(claim),
                claim.request_id,
            ),
        )
        row = _row(cursor)
        if row is None:
            return None
        result = _normalized_json(row.get("result"))
        if not isinstance(result, Mapping):
            raise ProviderFailureRepairError(
                "provider_failure_probe_recovery_result_invalid"
            )
        quick_probe_count = _verify_probe_recovery_result(claim, result)
        projection_id = row.get("unblock_projection_id")
        if (
            isinstance(projection_id, bool)
            or not isinstance(projection_id, int)
            or projection_id < 1
        ):
            raise ProviderFailureRepairError(
                "provider_failure_probe_recovery_result_invalid"
            )
        if result.get("unblock_projection_id") != projection_id:
            raise ProviderFailureRepairError(
                "provider_failure_probe_recovery_result_invalid"
            )
        return ProviderFailureProbeRecovery(quick_probe_count, projection_id)

    def complete(
        self,
        claim: ProviderFailureRepairClaim,
        *,
        result: Mapping[str, Any],
    ) -> None:
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                self._complete_row(cursor, claim, result)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            _close(connection)

    def fail(
        self,
        claim: ProviderFailureRepairClaim,
        *,
        error_class: str,
        retry_after_seconds: int,
        terminal: bool,
    ) -> None:
        if not isinstance(terminal, bool):
            raise ProviderFailureRepairError("provider_failure_repair_terminal_invalid")
        retry_after_seconds = _bounded_integer(
            retry_after_seconds,
            minimum=1,
            maximum=86_400,
            code="provider_failure_repair_retry_delay_invalid",
        )
        error_class = _error_code(error_class)
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE a6_trade.provider_failure_repair_requests
                    SET status = %s, claim_token = NULL, claimed_by = NULL,
                        claimed_at = NULL,
                        next_attempt_at = CASE
                            WHEN %s THEN next_attempt_at
                            ELSE now() + (%s * interval '1 second')
                        END,
                        last_error_class = %s, updated_at = now()
                    WHERE id = %s AND status = 'running' AND claim_token = %s
                    RETURNING id
                    """,
                    (
                        "failed" if terminal else "pending",
                        terminal,
                        retry_after_seconds,
                        error_class,
                        claim.request_id,
                        claim.claim_token,
                    ),
                )
                if _row(cursor) is None:
                    raise ProviderFailureRepairError("provider_failure_repair_fail_cas_mismatch")
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            _close(connection)

    def _materialize(self, cursor: Any) -> None:
        cursor.execute(
            """
            INSERT INTO a6_trade.provider_failure_repair_requests (
                attempt_id, quarantine_id, control_action, config_version,
                model, routing_generation, cost_generation, slot_no,
                enodot_binding_id, enodot_account_id, a6_token_id,
                failed_channel_id, desired_rank, desired_priority,
                desired_slot_cost_multiplier, observed_rate_multiplier,
                funding_eligible, confirmed_pin_binding_id,
                pin_confirmed, probe_confirmed, slot_state_version
            )
            SELECT attempt.attempt_id, projection.quarantine_id,
                   attempt.failure_control_action, generation.config_version,
                   attempt.model, attempt.routing_generation,
                   attempt.cost_generation, attempt.slot_no,
                   slot.enodot_binding_id, attempt.enodot_account_id,
                   attempt.a6_token_id, attempt.channel_id,
                   slot.desired_rank, slot.desired_priority,
                   slot.desired_slot_cost_multiplier,
                   slot.observed_rate_multiplier, slot.funding_eligible,
                   slot.confirmed_pin_binding_id, slot.pin_confirmed,
                   slot.probe_confirmed, slot.state_version
            FROM a6_trade.provider_attempts attempt
            JOIN a6_trade.provider_fast_block_outbox projection
              ON projection.attempt_id = attempt.attempt_id
             AND projection.operation = 'block'
             AND projection.quarantine_id IS NOT NULL
            JOIN a6_trade.provider_pool_generations generation
              ON generation.model = attempt.model
             AND generation.generation = attempt.routing_generation
             AND generation.cost_generation = attempt.cost_generation
             AND generation.generation_state = 'active'
            JOIN a6_trade.provider_pool_slots slot
              ON slot.generation_id = generation.id
             AND slot.model = attempt.model
             AND slot.routing_generation = attempt.routing_generation
             AND slot.cost_generation = attempt.cost_generation
             AND slot.slot_no = attempt.slot_no
             AND slot.enodot_account_id = attempt.enodot_account_id
             AND slot.a6_token_id = attempt.a6_token_id
             AND slot.desired_channel_id = attempt.channel_id
             AND slot.valid_to IS NULL
             AND slot.slot_state = 'DRAINING'
            WHERE attempt.state = 'failed'
              AND NOT attempt.response_committed
              AND attempt.failure_control_action IN (
                    'repin_immediately', 'probe_then_repin'
                  )
              AND NOT EXISTS (
                    SELECT 1
                    FROM a6_trade.provider_failure_repair_requests request
                    WHERE request.attempt_id = attempt.attempt_id
                  )
            ORDER BY attempt.failure_occurred_at, attempt.id
            LIMIT %s
            ON CONFLICT DO NOTHING
            """,
            (self.materialize_limit,),
        )

    def _supersede_stale(self, cursor: Any) -> None:
        cursor.execute(
            """
            UPDATE a6_trade.provider_failure_repair_requests request
            SET status = 'superseded', claim_token = NULL, claimed_by = NULL,
                claimed_at = NULL,
                last_error_class = 'provider_failure_repair_generation_superseded',
                updated_at = now()
            WHERE (
                    request.status = 'pending'
                    OR (
                        request.status = 'running'
                        AND request.claimed_at <= now()
                            - (%s * interval '1 second')
                    )
                  )
              AND NOT EXISTS (
                    SELECT 1
                    FROM a6_trade.provider_pool_generations generation
                    WHERE generation.model = request.model
                      AND generation.generation = request.routing_generation
                      AND generation.cost_generation = request.cost_generation
                      AND generation.generation_state = 'active'
                  )
              AND NOT EXISTS (
                    SELECT 1
                    FROM a6_trade.provider_pool_generations generation
                    JOIN a6_trade.provider_cost_generations cost
                      ON cost.id = generation.cost_generation_id
                     AND cost.model = request.model
                     AND cost.cost_generation = request.target_cost_generation
                     AND cost.pricing_state = 'active'
                    JOIN a6_trade.provider_pool_slots slot
                      ON slot.generation_id = generation.id
                     AND slot.model = request.model
                     AND slot.slot_no = request.slot_no
                     AND slot.routing_generation = request.target_routing_generation
                     AND slot.cost_generation = request.target_cost_generation
                     AND slot.desired_channel_id = request.target_channel_id
                     AND slot.enodot_account_id = request.target_account_id
                     AND slot.a6_token_id = request.target_token_id
                     AND slot.slot_state = 'READY'
                     AND slot.funding_eligible
                     AND slot.desired_schedulable
                     AND slot.observed_schedulable
                     AND slot.pin_confirmed
                     AND slot.probe_confirmed
                     AND slot.confirmed_pin_binding_id IS NOT NULL
                     AND slot.valid_to IS NULL
                    WHERE generation.model = request.model
                      AND generation.generation = request.target_routing_generation
                      AND generation.cost_generation = request.target_cost_generation
                      AND generation.generation_state = 'active'
                      AND generation.config_version = request.config_version
                      AND generation.source_hash = request.target_source_hash
                  )
            """,
            (self.stale_claim_seconds,),
        )

    @staticmethod
    def _complete_row(
        cursor: Any,
        claim: ProviderFailureRepairClaim,
        result: Mapping[str, Any],
    ) -> None:
        cursor.execute(
            """
            UPDATE a6_trade.provider_failure_repair_requests
            SET status = 'completed', result = %s::jsonb,
                completed_at = now(), claim_token = NULL,
                claimed_by = NULL, claimed_at = NULL,
                last_error_class = NULL, updated_at = now()
            WHERE id = %s AND status = 'running' AND claim_token = %s
            RETURNING id
            """,
            (_json(result), claim.request_id, claim.claim_token),
        )
        if _row(cursor) is None:
            raise ProviderFailureRepairError("provider_failure_repair_complete_cas_mismatch")


class ProviderFailureRepairController:
    """Execute one claimed request without holding a database transaction remotely."""

    def __init__(
        self,
        store: ProviderFailureRepairStore,
        remote: ProviderFailureRepairRemote | None,
        planner: ProviderFailureRepairPlanner,
        reconciler_factory: ProviderFailureReconcilerFactory | None,
        *,
        model_lease: ProviderModelLease,
        runtime_factory: ProviderFailureRepairRuntimeFactory | None = None,
        probe_remote_factory: ProviderFailureProbeRemoteFactory | None = None,
        ledger: ProviderActionLedger | None = None,
        activation_publisher: Callable[[str, int, int], object] | None = None,
        quick_probe_attempts: int = 2,
        quick_probe_enabled: bool = True,
        max_repair_attempts: int = 8,
        retry_after_seconds: int = 30,
    ) -> None:
        self.store = store
        self.remote = remote
        self.planner = planner
        self.reconciler_factory = reconciler_factory
        self.runtime_factory = runtime_factory
        self.probe_remote_factory = probe_remote_factory
        self.model_lease = model_lease
        self.ledger = ledger or IdempotencyLedger()
        self.activation_publisher = activation_publisher
        if not isinstance(quick_probe_enabled, bool):
            raise ValueError("provider_failure_repair_probe_mode_invalid")
        self.quick_probe_enabled = quick_probe_enabled
        self.quick_probe_attempts = _bounded_integer(
            quick_probe_attempts,
            minimum=1,
            maximum=3,
            code="provider_failure_repair_probe_budget_invalid",
        )
        self.max_repair_attempts = _bounded_integer(
            max_repair_attempts,
            minimum=1,
            maximum=100,
            code="provider_failure_repair_attempt_budget_invalid",
        )
        self.retry_after_seconds = _bounded_integer(
            retry_after_seconds,
            minimum=1,
            maximum=86_400,
            code="provider_failure_repair_retry_delay_invalid",
        )
        dependencies = (
            (
                store,
                (
                    "bind_target",
                    "read_active_target",
                    "read_probe_recovery",
                    "complete_probe_recovery",
                    "complete",
                    "fail",
                ),
                "store",
            ),
            (planner, ("plan",), "planner"),
            (model_lease, ("hold",), "model_lease"),
        )
        for dependency, methods, code in dependencies:
            if any(not callable(getattr(dependency, method, None)) for method in methods):
                raise ValueError(f"provider_failure_repair_{code}_invalid")
        if activation_publisher is not None and not callable(activation_publisher):
            raise ValueError("provider_failure_repair_activation_publisher_invalid")
        static_remote_methods = (
            "drain_slot",
            "probe_slot",
            "set_schedulable",
            "read_slot_schedulable",
        )
        static_runtime_valid = all(
            callable(getattr(remote, method, None)) for method in static_remote_methods
        ) and callable(reconciler_factory)
        dynamic_runtime_valid = callable(runtime_factory) and callable(
            probe_remote_factory
        )
        if callable(runtime_factory) != callable(probe_remote_factory):
            raise ValueError(
                "provider_failure_repair_dynamic_runtime_configuration_invalid"
            )
        if static_runtime_valid == dynamic_runtime_valid:
            raise ValueError("provider_failure_repair_runtime_configuration_invalid")

    def execute(self, claim: ProviderFailureRepairClaim) -> ProviderFailureRepairRun:
        if not isinstance(claim, ProviderFailureRepairClaim):
            raise ProviderFailureRepairError("provider_failure_repair_claim_invalid")
        try:
            with self.model_lease.hold(claim.model):
                return self._execute_locked(claim)
        except Exception as exc:
            error_class = _exception_code(exc)
            terminal = claim.attempt_count >= self.max_repair_attempts
            self.store.fail(
                claim,
                error_class=error_class,
                retry_after_seconds=self.retry_after_seconds,
                terminal=terminal,
            )
            return ProviderFailureRepairRun(
                "failed" if terminal else "pending",
                claim.request_id,
                claim.control_action,
                claim.attempt_count,
                0,
                error_class=error_class,
            )

    def _execute_locked(
        self,
        claim: ProviderFailureRepairClaim,
    ) -> ProviderFailureRepairRun:
        probe_recovery = self.store.read_probe_recovery(claim)
        if probe_recovery is not None:
            return ProviderFailureRepairRun(
                "completed",
                claim.request_id,
                claim.control_action,
                claim.attempt_count,
                probe_recovery.quick_probe_count,
            )

        recovered = self.store.read_active_target(claim)
        if recovered is not None:
            if self.activation_publisher is not None:
                self.activation_publisher(
                    claim.model,
                    recovered.routing_generation,
                    recovered.cost_generation,
                )
            self.store.complete(
                claim,
                result=_completion_result(recovered, actions=(), recovered=True),
            )
            return ProviderFailureRepairRun(
                "completed",
                claim.request_id,
                claim.control_action,
                claim.attempt_count,
                target=recovered,
            )

        probe_count = 0
        healthy = False
        if (
            self.quick_probe_enabled
            and claim.control_action == "probe_then_repin"
            and claim.target is None
        ):
            probe_remote = self._probe_remote(claim)
            self._drain_remote(claim, probe_remote)
            for probe_no in range(1, self.quick_probe_attempts + 1):
                probe_count += 1
                healthy = self._probe_once(claim, probe_remote, probe_no)
                if healthy:
                    break

        if healthy:
            self._remote_action(
                claim,
                "failure_restore_schedulable",
                (claim.request_id, _slot_identity(claim.current_slot)),
                lambda key: probe_remote.set_schedulable(
                    claim.current_slot,
                    True,
                    idempotency_key=key,
                ),
            )
            if probe_remote.read_slot_schedulable(claim.current_slot) is not True:
                raise ProviderFailureRepairError(
                    "provider_failure_repair_remote_restore_readback_mismatch"
                )
            recovery = self.store.complete_probe_recovery(
                claim,
                quick_probe_count=probe_count,
            )
            return ProviderFailureRepairRun(
                "completed",
                claim.request_id,
                claim.control_action,
                claim.attempt_count,
                recovery.quick_probe_count,
            )

        # Replacement availability must not gate recovery of a healthy original
        # assignment. Plan, validate and persist a target only after probes fail.
        plan = self.planner.plan(claim)
        target = _validate_plan(claim, plan)
        bound = self.store.bind_target(claim, target)
        if bound != target:
            raise ProviderFailureRepairError("provider_failure_repair_target_readback_mismatch")

        runtime = self._runtime(claim, plan)
        remote = runtime.remote
        self._drain_remote(claim, remote)

        reconciler = runtime.reconciler_factory(plan, self.ledger)
        if not isinstance(reconciler, ProviderPoolReconciler):
            raise ProviderFailureRepairError("provider_failure_repair_reconciler_invalid")
        if reconciler.remote is not remote:
            raise ProviderFailureRepairError("provider_failure_repair_remote_identity_mismatch")
        if reconciler.ledger is not self.ledger:
            raise ProviderFailureRepairError("provider_failure_repair_ledger_identity_mismatch")
        if reconciler.model_lease is not None:
            raise ProviderFailureRepairError("provider_failure_repair_nested_model_lease_invalid")
        if not reconciler.require_remote_readback:
            raise ProviderFailureRepairError("provider_failure_repair_strict_readback_required")
        report = reconciler.reconcile(
            plan.bundle,
            current_routing=plan.current_routing,
            current_assignments=plan.current_assignments,
            current_slots=plan.current_slots,
            current_cost_input_hash=plan.current_cost_input_hash,
        )
        if (
            not report.activated
            or report.routing_generation != target.routing_generation
            or report.cost_generation != target.cost_generation
        ):
            raise ProviderFailureRepairError("provider_failure_repair_activation_mismatch")
        result = _completion_result(target, actions=report.actions, recovered=False)
        self.store.complete(claim, result=result)
        return ProviderFailureRepairRun(
            "completed",
            claim.request_id,
            claim.control_action,
            claim.attempt_count,
            probe_count,
            target,
        )

    def _probe_remote(
        self,
        claim: ProviderFailureRepairClaim,
    ) -> ProviderFailureRepairRemote:
        if self.probe_remote_factory is not None:
            remote = self.probe_remote_factory(claim)
        else:
            remote = self.remote
        methods = (
            "drain_slot",
            "probe_slot",
            "set_schedulable",
            "read_slot_schedulable",
        )
        if remote is None or any(
            not callable(getattr(remote, method, None)) for method in methods
        ):
            raise ProviderFailureRepairError(
                "provider_failure_repair_probe_remote_invalid"
            )
        return remote

    def _drain_remote(
        self,
        claim: ProviderFailureRepairClaim,
        remote: ProviderFailureRepairRemote,
    ) -> None:
        self._remote_action(
            claim,
            "failure_drain_slot",
            (claim.request_id, claim.attempt_count, _slot_identity(claim.current_slot)),
            lambda key: remote.drain_slot(
                claim.current_slot,
                idempotency_key=key,
            ),
        )
        if remote.read_slot_schedulable(claim.current_slot) is not False:
            raise ProviderFailureRepairError(
                "provider_failure_repair_remote_drain_readback_mismatch"
            )

    def _runtime(
        self,
        claim: ProviderFailureRepairClaim,
        plan: ProviderPoolApplyPlan,
    ) -> ProviderFailureRepairRuntime:
        if self.runtime_factory is not None:
            runtime = self.runtime_factory(claim, plan, self.ledger)
            if not isinstance(runtime, ProviderFailureRepairRuntime):
                raise ProviderFailureRepairError("provider_failure_repair_runtime_invalid")
            return runtime
        if self.remote is None or self.reconciler_factory is None:
            raise ProviderFailureRepairError("provider_failure_repair_runtime_invalid")
        return ProviderFailureRepairRuntime(self.remote, self.reconciler_factory)

    def _probe_once(
        self,
        claim: ProviderFailureRepairClaim,
        remote: ProviderFailureRepairRemote,
        probe_no: int,
    ) -> bool:
        result = False

        def probe(key: str) -> bool:
            nonlocal result
            result = bool(remote.probe_slot(claim.current_slot, idempotency_key=key))
            return result

        completed = self._remote_action(
            claim,
            "failure_quick_probe",
            (claim.request_id, claim.attempt_count, probe_no, _slot_identity(claim.current_slot)),
            probe,
            observed={"probe_no": probe_no},
        )
        return result if completed else False

    def _remote_action(
        self,
        claim: ProviderFailureRepairClaim,
        action: str,
        target: object,
        call: Callable[[str], bool],
        *,
        observed: Mapping[str, Any] | None = None,
    ) -> bool:
        action_claim = self.ledger.claim(claim.scope, action, target)
        if action_claim.disposition is ActionClaimDisposition.COMPLETED:
            return False
        if action_claim.disposition is ActionClaimDisposition.BUSY:
            raise ProviderFailureRepairError("provider_failure_repair_action_busy")
        started = time.monotonic()
        try:
            changed = call(action_claim.key)
        except Exception as exc:
            error_class = f"provider_failure_repair_{action}_failed"
            self.ledger.fail(
                action_claim,
                error_class=error_class,
                elapsed_ms=_elapsed_ms(started),
            )
            raise ProviderFailureRepairError(error_class) from exc
        self.ledger.complete(
            action_claim,
            observed_after={
                "verified": True,
                "changed": changed is not False,
                **dict(observed or {}),
            },
            elapsed_ms=_elapsed_ms(started),
        )
        return True


class ProviderFailureRepairWorker:
    def __init__(
        self,
        store: ProviderFailureRepairStore,
        controller: ProviderFailureRepairController,
        *,
        worker_id: str,
        fast_block_replayer: ProviderFailureFastBlockReplayer | None = None,
    ) -> None:
        self.store = store
        self.controller = controller
        self.worker_id = _worker_id(worker_id)
        self.fast_block_replayer = fast_block_replayer
        if controller.store is not store:
            raise ValueError("provider_failure_repair_worker_store_mismatch")
        if fast_block_replayer is not None and not callable(fast_block_replayer):
            raise ValueError("provider_failure_repair_fast_block_replayer_invalid")

    def run_once(self) -> ProviderFailureRepairRun:
        if self.fast_block_replayer is not None:
            self.fast_block_replayer()
        claim = self.store.claim_next(worker_id=self.worker_id)
        if claim is None:
            return ProviderFailureRepairRun("idle")
        run = self.controller.execute(claim)
        if self.fast_block_replayer is not None and run.status == "completed":
            self.fast_block_replayer()
        return run


def _validate_plan(
    claim: ProviderFailureRepairClaim,
    plan: ProviderPoolApplyPlan,
) -> ProviderFailureRepairTarget:
    if not isinstance(plan, ProviderPoolApplyPlan):
        raise ProviderFailureRepairError("provider_failure_repair_plan_invalid")
    bundle = plan.bundle
    if (
        bundle.model != claim.model
        or bundle.parent_routing_generation != claim.routing_generation
        or bundle.parent_cost_generation != claim.cost_generation
        or plan.active is None
        or plan.active.routing_generation != claim.routing_generation
        or plan.active.cost_generation != claim.cost_generation
    ):
        raise ProviderFailureRepairError("provider_failure_repair_plan_generation_mismatch")
    if plan.diff_kind not in {
        PoolDiffKind.MEMBERSHIP_AND_COST,
        PoolDiffKind.LIQUIDITY_REPLACEMENT,
    }:
        raise ProviderFailureRepairError("provider_failure_repair_plan_diff_invalid")
    if claim.current_slot.slot_no not in plan.changed_slot_nos:
        raise ProviderFailureRepairError("provider_failure_repair_slot_unchanged")
    current_slot = plan.current_slots.get(claim.current_slot.slot_no)
    if current_slot is None or _slot_identity(current_slot) != _slot_identity(claim.current_slot):
        raise ProviderFailureRepairError("provider_failure_repair_current_slot_mismatch")
    target_slot = next(
        (slot for slot in bundle.slots if slot.slot_no == claim.current_slot.slot_no),
        None,
    )
    if (
        target_slot is None
        or target_slot.slot_state is not SlotState.READY
        or target_slot.channel_id is None
        or target_slot.account_id is None
        or target_slot.token_id is None
        or target_slot.channel_id == claim.current_slot.channel_id
    ):
        raise ProviderFailureRepairError("provider_failure_repair_replacement_missing")
    target = ProviderFailureRepairTarget(
        bundle.routing_generation,
        bundle.cost_generation,
        target_slot.slot_no,
        target_slot.channel_id,
        target_slot.account_id,
        target_slot.token_id,
        bundle.source_hash,
    )
    if claim.target is not None and claim.target != target:
        raise ProviderFailureRepairError("provider_failure_repair_target_drift")
    return target


def _claim(row: Mapping[str, Any]) -> ProviderFailureRepairClaim:
    target = None
    if row.get("target_routing_generation") is not None:
        target = _target(row)
    slot = ProviderSlot(
        slot_no=int(row["slot_no"]),
        channel_id=int(row["failed_channel_id"]),
        rank=_optional_int(row.get("desired_rank")),
        priority=_optional_int(row.get("desired_priority")),
        assignment_id=int(row["enodot_binding_id"]),
        enodot_binding_id=int(row["enodot_binding_id"]),
        token_id=int(row["a6_token_id"]),
        account_id=int(row["enodot_account_id"]),
        slot_state=SlotState.DRAINING,
        funding_eligible=bool(row.get("funding_eligible")),
        desired_schedulable=False,
        observed_schedulable=False,
        desired_cost_multiplier=_optional_decimal(row.get("desired_slot_cost_multiplier")),
        observed_cost_multiplier=_optional_decimal(row.get("observed_rate_multiplier")),
        confirmed_pin_binding_id=_optional_int(row.get("confirmed_pin_binding_id")),
        pin_confirmed=bool(row.get("pin_confirmed")),
        probe_confirmed=bool(row.get("probe_confirmed")),
        state_version=int(row["slot_state_version"]),
    )
    return ProviderFailureRepairClaim(
        request_id=int(row["id"]),
        claim_token=str(row["claim_token"]),
        worker_id=str(row["claimed_by"]),
        attempt_count=int(row["attempt_count"]),
        attempt_id=str(row["attempt_id"]),
        quarantine_id=int(row["quarantine_id"]),
        control_action=str(row["control_action"]),
        config_version=str(row["config_version"]),
        model=str(row["model"]),
        routing_generation=int(row["routing_generation"]),
        cost_generation=int(row["cost_generation"]),
        current_slot=slot,
        target=target,
    )


def _target(row: Mapping[str, Any]) -> ProviderFailureRepairTarget:
    return ProviderFailureRepairTarget(
        int(row["target_routing_generation"]),
        int(row["target_cost_generation"]),
        int(row["slot_no"]),
        int(row["target_channel_id"]),
        int(row["target_account_id"]),
        int(row["target_token_id"]),
        str(row["target_source_hash"]),
    )


def _completion_result(
    target: ProviderFailureRepairTarget,
    *,
    actions: tuple[str, ...],
    recovered: bool,
) -> dict[str, Any]:
    return {
        "routing_generation": target.routing_generation,
        "cost_generation": target.cost_generation,
        "slot_no": target.slot_no,
        "channel_id": target.channel_id,
        "account_id": target.account_id,
        "token_id": target.token_id,
        "source_hash": target.source_hash,
        "actions": list(actions),
        "recovered_from_active_readback": recovered,
    }


def _probe_recovery_evidence(
    claim: ProviderFailureRepairClaim,
    quick_probe_count: int,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "recovery_kind": "failure_quick_probe",
        "repair_request_id": claim.request_id,
        "attempt_id": claim.attempt_id,
        "routing_generation": claim.routing_generation,
        "cost_generation": claim.cost_generation,
        "slot_no": claim.current_slot.slot_no,
        "channel_id": claim.current_slot.channel_id,
        "account_id": claim.current_slot.account_id,
        "token_id": claim.current_slot.token_id,
        "confirmed_pin_binding_id": claim.current_slot.confirmed_pin_binding_id,
        "quick_probe_count": quick_probe_count,
        "original_assignment_preserved": True,
    }


def _probe_unblock_payload(
    claim: ProviderFailureRepairClaim,
    quick_probe_count: int,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "recovery_kind": "failure_quick_probe",
        "repair_request_id": claim.request_id,
        "attempt_id": claim.attempt_id,
        "quarantine_id": claim.quarantine_id,
        "quick_probe_count": quick_probe_count,
    }


def _probe_recovery_result(
    claim: ProviderFailureRepairClaim,
    *,
    quick_probe_count: int,
    unblock_projection_id: int,
) -> dict[str, Any]:
    return {
        "outcome": "probe_recovered",
        "routing_generation": claim.routing_generation,
        "cost_generation": claim.cost_generation,
        "slot_no": claim.current_slot.slot_no,
        "channel_id": claim.current_slot.channel_id,
        "account_id": claim.current_slot.account_id,
        "token_id": claim.current_slot.token_id,
        "confirmed_pin_binding_id": claim.current_slot.confirmed_pin_binding_id,
        "quick_probe_count": quick_probe_count,
        "unblock_projection_id": unblock_projection_id,
        "original_assignment_preserved": True,
        "replacement_written": False,
    }


def _verify_probe_recovery_result(
    claim: ProviderFailureRepairClaim,
    result: Mapping[str, Any],
) -> int:
    quick_probe_count = result.get("quick_probe_count")
    if (
        isinstance(quick_probe_count, bool)
        or not isinstance(quick_probe_count, int)
        or not 1 <= quick_probe_count <= 3
    ):
        raise ProviderFailureRepairError(
            "provider_failure_probe_recovery_result_invalid"
        )
    unblock_projection_id = result.get("unblock_projection_id")
    if (
        isinstance(unblock_projection_id, bool)
        or not isinstance(unblock_projection_id, int)
        or unblock_projection_id < 1
    ):
        raise ProviderFailureRepairError(
            "provider_failure_probe_recovery_result_invalid"
        )
    expected = _probe_recovery_result(
        claim,
        quick_probe_count=quick_probe_count,
        unblock_projection_id=unblock_projection_id,
    )
    if dict(result) != expected:
        raise ProviderFailureRepairError(
            "provider_failure_probe_recovery_result_invalid"
        )
    return quick_probe_count


def _probe_unblock_idempotency_key(claim: ProviderFailureRepairClaim) -> str:
    return f"fast-unblock:failure-repair:{claim.request_id}"


def _fast_block_projection_key(claim: ProviderFailureRepairClaim) -> str:
    return (
        f"a6:provider:fast-block:v1:{claim.model}:{claim.routing_generation}:"
        f"{claim.current_slot.slot_no}:{claim.current_slot.channel_id}"
    )


def _slot_identity(slot: ProviderSlot) -> tuple[int, int, int, int]:
    if slot.channel_id is None or slot.account_id is None or slot.token_id is None:
        raise ProviderFailureRepairError("provider_failure_repair_slot_invalid")
    return (slot.slot_no, slot.account_id, slot.token_id, slot.channel_id)


def _worker_id(value: object) -> str:
    worker_id = str(value or "").strip()
    if not _SAFE_WORKER_ID.fullmatch(worker_id):
        raise ProviderFailureRepairError("provider_failure_repair_worker_id_invalid")
    return worker_id


def _bounded_integer(value: object, *, minimum: int, maximum: int, code: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(code)
    return value


def _optional_int(value: object) -> int | None:
    return None if value is None else int(value)


def _optional_decimal(value: object) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def _error_code(value: object) -> str:
    code = str(value or "").strip().casefold().replace("-", "_")
    code = re.sub(r"[^a-z0-9_.:]", "_", code)[:128]
    if not code or not code[0].isalnum():
        raise ProviderFailureRepairError("provider_failure_repair_error_class_invalid")
    return code


def _exception_code(exc: Exception) -> str:
    value = getattr(exc, "code", None)
    if isinstance(value, str) and value:
        return _error_code(value)
    return _error_code(type(exc).__name__)


def _elapsed_ms(started: float) -> int:
    return max(int((time.monotonic() - started) * 1000), 0)


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _normalized_json(value: object) -> object:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    if isinstance(value, Mapping):
        return {str(key): _normalized_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalized_json(item) for item in value]
    return value


def _row(cursor: Any) -> dict[str, Any] | None:
    value = cursor.fetchone()
    if value is None:
        return None
    if isinstance(value, Mapping):
        return dict(value)
    description = getattr(cursor, "description", None)
    if not description:
        raise ProviderFailureRepairError("provider_failure_repair_row_mapping_missing")
    return {str(column[0]): item for column, item in zip(description, value, strict=True)}


def _close(connection: Any) -> None:
    close = getattr(connection, "close", None)
    if callable(close):
        close()
