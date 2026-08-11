"""Durable Provider vacancy replacement coordination."""

from __future__ import annotations

import json
import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol


class ProviderMembershipRepairError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class ProviderMembershipRepairClaim:
    request_id: int
    claim_token: str
    model: str
    source_routing_generation: int
    source_cost_generation: int
    slot_no: int
    outgoing_merchant_id: int
    outgoing_enodot_account_id: int
    outgoing_a6_token_id: int
    reason_code: str
    attempt_count: int

    def __post_init__(self) -> None:
        identities = (
            self.request_id,
            self.source_routing_generation,
            self.source_cost_generation,
            self.slot_no,
            self.outgoing_merchant_id,
            self.outgoing_enodot_account_id,
            self.outgoing_a6_token_id,
            self.attempt_count,
        )
        if (
            any(isinstance(value, bool) or value < 1 for value in identities)
            or not self.claim_token
            or not self.model.strip()
            or self.reason_code
            not in {"provider_funding_contract_failed", "cashout_drain"}
        ):
            raise ProviderMembershipRepairError("provider_membership_repair_claim_invalid")


@dataclass(frozen=True)
class ProviderMembershipSlotState:
    routing_generation: int
    cost_generation: int
    slot_no: int
    slot_state: str
    merchant_id: int | None
    enodot_account_id: int | None
    a6_token_id: int | None
    route_mode: str | None
    observed_schedulable: bool
    auto_pool_generation_id: int | None
    auto_pool_generation_state: str | None
    auto_pool_account_state: str | None
    ban_confirmed: bool
    auto_pool_confirmed: bool
    cost_confirmed: bool
    schedulable_confirmed: bool

    @property
    def vacant(self) -> bool:
        return self.slot_state == "VACANT"

    @property
    def replacement_identity(self) -> tuple[int, int, int] | None:
        if self.vacant:
            return None
        values = (self.merchant_id, self.enodot_account_id, self.a6_token_id)
        if any(value is None or value < 1 for value in values):
            raise ProviderMembershipRepairError(
                "provider_membership_repair_slot_identity_invalid"
            )
        return int(values[0]), int(values[1]), int(values[2])

    @property
    def auto_pool_ready(self) -> bool:
        return bool(
            not self.vacant
            and self.slot_state == "READY"
            and self.route_mode == "auto_pool"
            and self.observed_schedulable
            and self.auto_pool_generation_id is not None
            and self.auto_pool_generation_state == "active"
            and self.auto_pool_account_state == "ready"
            and self.ban_confirmed
            and self.auto_pool_confirmed
            and self.cost_confirmed
            and self.schedulable_confirmed
        )


class ProviderMembershipWakeTrigger(Protocol):
    def wake(self, *, model: str, request_id: int) -> None: ...


class ProviderMembershipRepairStore(Protocol):
    def claim(self, *, worker_id: str) -> ProviderMembershipRepairClaim | None: ...

    def observe(
        self, claim: ProviderMembershipRepairClaim
    ) -> ProviderMembershipSlotState: ...

    def defer(
        self,
        claim: ProviderMembershipRepairClaim,
        *,
        state: ProviderMembershipSlotState | None,
        result: Mapping[str, Any],
        retry_after_seconds: int,
        error_class: str | None = None,
    ) -> None: ...

    def complete(
        self,
        claim: ProviderMembershipRepairClaim,
        *,
        state: ProviderMembershipSlotState,
        result: Mapping[str, Any],
    ) -> None: ...


class PostgresProviderMembershipRepairStore:
    def __init__(
        self,
        connection_factory: Callable[[], Any],
        *,
        stale_claim_seconds: int = 120,
    ) -> None:
        if not callable(connection_factory):
            raise ValueError("provider_membership_repair_connection_factory_invalid")
        if (
            isinstance(stale_claim_seconds, bool)
            or not 5 <= stale_claim_seconds <= 86_400
        ):
            raise ValueError("provider_membership_repair_stale_claim_invalid")
        self.connection_factory = connection_factory
        self.stale_claim_seconds = stale_claim_seconds

    def claim(self, *, worker_id: str) -> ProviderMembershipRepairClaim | None:
        owner = str(worker_id or "").strip()
        if not owner:
            raise ProviderMembershipRepairError(
                "provider_membership_repair_worker_invalid"
            )
        claim_token = secrets.token_hex(24)
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    WITH candidate AS (
                        SELECT request.id
                        FROM a6_trade.provider_membership_repair_requests request
                        WHERE (
                            request.status IN ('pending', 'retry')
                            AND request.next_attempt_at <= now()
                        ) OR (
                            request.status = 'running'
                            AND request.claimed_at < now() - (%s * interval '1 second')
                        )
                        ORDER BY request.next_attempt_at, request.id
                        FOR UPDATE SKIP LOCKED
                        LIMIT 1
                    )
                    UPDATE a6_trade.provider_membership_repair_requests request
                    SET status = 'running', claim_token = %s, claimed_by = %s,
                        claimed_at = now(), attempt_count = request.attempt_count + 1,
                        completed_at = NULL, updated_at = now()
                    FROM candidate
                    WHERE request.id = candidate.id
                    RETURNING request.*
                    """,
                    (self.stale_claim_seconds, claim_token, owner),
                )
                row = _row(cursor)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            _close(connection)
        return None if row is None else _claim(row)

    def observe(
        self, claim: ProviderMembershipRepairClaim
    ) -> ProviderMembershipSlotState:
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT generation.generation AS routing_generation,
                           generation.cost_generation, slot.slot_no,
                           slot.slot_state, merchant.id AS merchant_id,
                           slot.enodot_account_id, slot.a6_token_id,
                           slot.route_mode, slot.observed_schedulable,
                           slot.auto_pool_generation_id,
                           auto_generation.generation_state
                               AS auto_pool_generation_state,
                           auto_account.account_state AS auto_pool_account_state,
                           coalesce(auto_account.ban_confirmed, false)
                               AS ban_confirmed,
                           coalesce(auto_account.auto_pool_confirmed, false)
                               AS auto_pool_confirmed,
                           coalesce(auto_account.cost_confirmed, false)
                               AS cost_confirmed,
                           coalesce(auto_account.schedulable_confirmed, false)
                               AS schedulable_confirmed
                    FROM a6_trade.provider_pool_generations generation
                    JOIN a6_trade.provider_pool_slots slot
                      ON slot.generation_id = generation.id
                     AND slot.model = generation.model
                     AND slot.routing_generation = generation.generation
                     AND slot.valid_to IS NULL
                    LEFT JOIN a6_trade.a6_tokens token ON token.id = slot.a6_token_id
                    LEFT JOIN a6_trade.a6_merchants merchant
                      ON merchant.id = token.merchant_id
                    LEFT JOIN a6_trade.provider_auto_pool_generations auto_generation
                      ON auto_generation.id = slot.auto_pool_generation_id
                     AND auto_generation.model = generation.model
                     AND auto_generation.routing_generation = generation.generation
                     AND auto_generation.cost_generation = generation.cost_generation
                    LEFT JOIN a6_trade.provider_auto_pool_accounts auto_account
                      ON auto_account.generation_id = auto_generation.id
                     AND auto_account.slot_no = slot.slot_no
                     AND auto_account.token_ref_id = slot.a6_token_id
                     AND auto_account.enodot_account_id = slot.enodot_account_id
                    WHERE generation.model = %s
                      AND generation.generation_state = 'active'
                      AND slot.slot_no = %s
                    """,
                    (claim.model, claim.slot_no),
                )
                row = _row(cursor)
        finally:
            _close(connection)
        if row is None:
            raise ProviderMembershipRepairError(
                "provider_membership_repair_active_slot_missing"
            )
        return _slot_state(row)

    def defer(
        self,
        claim: ProviderMembershipRepairClaim,
        *,
        state: ProviderMembershipSlotState | None,
        result: Mapping[str, Any],
        retry_after_seconds: int,
        error_class: str | None = None,
    ) -> None:
        if isinstance(retry_after_seconds, bool) or not 1 <= retry_after_seconds <= 3600:
            raise ProviderMembershipRepairError(
                "provider_membership_repair_retry_delay_invalid"
            )
        target = _target_values(state)
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT request.id
                    FROM a6_trade.provider_membership_repair_requests request
                    JOIN a6_trade.provider_pool_generations generation
                      ON generation.model = request.model
                     AND generation.generation_state = 'active'
                    JOIN a6_trade.provider_pool_slots slot
                      ON slot.generation_id = generation.id
                     AND slot.model = generation.model
                     AND slot.routing_generation = generation.generation
                     AND slot.cost_generation = generation.cost_generation
                     AND slot.slot_no = request.slot_no
                     AND slot.valid_to IS NULL
                    JOIN a6_trade.a6_tokens token ON token.id = slot.a6_token_id
                    JOIN a6_trade.a6_merchants merchant
                      ON merchant.id = token.merchant_id
                    JOIN a6_trade.provider_auto_pool_generations auto_generation
                      ON auto_generation.id = slot.auto_pool_generation_id
                     AND auto_generation.model = generation.model
                     AND auto_generation.routing_generation = generation.generation
                     AND auto_generation.cost_generation = generation.cost_generation
                     AND auto_generation.generation_state = 'active'
                    JOIN a6_trade.provider_auto_pool_accounts auto_account
                      ON auto_account.generation_id = auto_generation.id
                     AND auto_account.merchant_id = merchant.id
                     AND auto_account.token_ref_id = slot.a6_token_id
                     AND auto_account.enodot_account_id = slot.enodot_account_id
                     AND auto_account.slot_no = slot.slot_no
                    WHERE request.id = %s AND request.status = 'running'
                      AND request.claim_token = %s
                      AND generation.generation = %s
                      AND generation.cost_generation = %s
                      AND generation.generation > request.source_routing_generation
                      AND generation.cost_generation >= request.source_cost_generation
                      AND slot.slot_state = 'READY'
                      AND slot.route_mode = 'auto_pool'
                      AND slot.desired_schedulable AND slot.observed_schedulable
                      AND slot.auto_pool_confirmed
                      AND slot.auto_pool_subscription_id IS NOT NULL
                      AND merchant.id = %s AND slot.enodot_account_id = %s
                      AND slot.a6_token_id = %s
                      AND merchant.id <> request.outgoing_merchant_id
                      AND slot.enodot_account_id
                          <> request.outgoing_enodot_account_id
                      AND slot.a6_token_id <> request.outgoing_a6_token_id
                      AND auto_account.subscription_id =
                          slot.auto_pool_subscription_id
                      AND auto_account.account_state = 'ready'
                      AND auto_account.ban_confirmed
                      AND auto_account.unpin_confirmed
                      AND auto_account.auto_pool_confirmed
                      AND auto_account.cost_confirmed
                      AND auto_account.schedulable_confirmed
                    FOR UPDATE OF request, generation, slot,
                                  auto_generation, auto_account
                    """,
                    (
                        claim.request_id,
                        claim.claim_token,
                        state.routing_generation,
                        state.cost_generation,
                        state.merchant_id,
                        state.enodot_account_id,
                        state.a6_token_id,
                    ),
                )
                if _row(cursor) is None:
                    raise ProviderMembershipRepairError(
                        "provider_membership_repair_completion_state_changed"
                    )
                cursor.execute(
                    """
                    UPDATE a6_trade.provider_membership_repair_requests
                    SET status = 'retry', claim_token = NULL, claimed_by = NULL,
                        claimed_at = NULL,
                        next_attempt_at = now() + (%s * interval '1 second'),
                        target_routing_generation = %s,
                        target_cost_generation = %s,
                        replacement_merchant_id = %s,
                        replacement_enodot_account_id = %s,
                        replacement_a6_token_id = %s,
                        last_error_class = %s, result = %s::jsonb,
                        updated_at = now()
                    WHERE id = %s AND status = 'running' AND claim_token = %s
                    RETURNING id
                    """,
                    (
                        retry_after_seconds,
                        *target,
                        _error(error_class),
                        _json(result),
                        claim.request_id,
                        claim.claim_token,
                    ),
                )
                if _row(cursor) is None:
                    raise ProviderMembershipRepairError(
                        "provider_membership_repair_defer_cas_mismatch"
                    )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            _close(connection)

    def complete(
        self,
        claim: ProviderMembershipRepairClaim,
        *,
        state: ProviderMembershipSlotState,
        result: Mapping[str, Any],
    ) -> None:
        target = _target_values(state)
        if any(value is None for value in target):
            raise ProviderMembershipRepairError(
                "provider_membership_repair_completion_target_missing"
            )
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE a6_trade.provider_membership_repair_requests
                    SET status = 'completed', claim_token = NULL, claimed_by = NULL,
                        claimed_at = NULL, target_routing_generation = %s,
                        target_cost_generation = %s,
                        replacement_merchant_id = %s,
                        replacement_enodot_account_id = %s,
                        replacement_a6_token_id = %s,
                        completed_at = now(), last_error_class = NULL,
                        result = %s::jsonb, updated_at = now()
                    WHERE id = %s AND status = 'running' AND claim_token = %s
                    RETURNING id
                    """,
                    (
                        *target,
                        _json(result),
                        claim.request_id,
                        claim.claim_token,
                    ),
                )
                if _row(cursor) is None:
                    raise ProviderMembershipRepairError(
                        "provider_membership_repair_complete_cas_mismatch"
                    )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            _close(connection)


class ProviderMembershipRepairConsumer:
    def __init__(
        self,
        store: ProviderMembershipRepairStore,
        *,
        worker_id: str,
        membership_apply_trigger: ProviderMembershipWakeTrigger,
        auto_pool_trigger: ProviderMembershipWakeTrigger,
    ) -> None:
        owner = str(worker_id or "").strip()
        if not owner:
            raise ProviderMembershipRepairError(
                "provider_membership_repair_worker_invalid"
            )
        self.store = store
        self.worker_id = owner
        self.membership_apply_trigger = membership_apply_trigger
        self.auto_pool_trigger = auto_pool_trigger

    def run_once(self) -> dict[str, Any]:
        claim = self.store.claim(worker_id=self.worker_id)
        if claim is None:
            return {"status": "idle"}
        state: ProviderMembershipSlotState | None = None
        try:
            state = self.store.observe(claim)
            identity = state.replacement_identity
            outgoing = (
                claim.outgoing_merchant_id,
                claim.outgoing_enodot_account_id,
                claim.outgoing_a6_token_id,
            )
            if identity is not None and any(
                current == previous
                for current, previous in zip(identity, outgoing, strict=True)
            ):
                raise ProviderMembershipRepairError(
                    "provider_membership_repair_outgoing_identity_still_active"
                )
            if state.vacant:
                self.membership_apply_trigger.wake(
                    model=claim.model,
                    request_id=claim.request_id,
                )
                result = _result(claim, state, "membership_apply_requested")
                self.store.defer(
                    claim,
                    state=None,
                    result=result,
                    retry_after_seconds=5,
                )
                return {"status": "waiting", **result}
            if (
                state.routing_generation <= claim.source_routing_generation
                or state.cost_generation < claim.source_cost_generation
            ):
                raise ProviderMembershipRepairError(
                    "provider_membership_repair_generation_not_advanced"
                )
            if not state.auto_pool_ready:
                self.auto_pool_trigger.wake(
                    model=claim.model,
                    request_id=claim.request_id,
                )
                result = _result(claim, state, "auto_pool_preflight_requested")
                self.store.defer(
                    claim,
                    state=state,
                    result=result,
                    retry_after_seconds=5,
                )
                return {"status": "waiting", **result}
            result = _result(claim, state, "replacement_active")
            self.store.complete(claim, state=state, result=result)
            return {"status": "completed", **result}
        except Exception as exc:
            code = _error(getattr(exc, "code", None) or type(exc).__name__)
            result = {
                "request_id": claim.request_id,
                "model": claim.model,
                "slot_no": claim.slot_no,
                "action": "retry",
                "error_class": code,
            }
            self.store.defer(
                claim,
                state=state,
                result=result,
                retry_after_seconds=_retry_delay(claim.attempt_count),
                error_class=code,
            )
            return {"status": "retry", **result}

    def drain(self, *, limit: int = 100) -> dict[str, int]:
        if isinstance(limit, bool) or not 1 <= limit <= 1000:
            raise ValueError("provider_membership_repair_drain_limit_invalid")
        counts = {"processed": 0, "completed": 0, "waiting": 0, "retry": 0}
        for _ in range(limit):
            result = self.run_once()
            if result["status"] == "idle":
                break
            counts["processed"] += 1
            counts[str(result["status"])] += 1
            if result["status"] == "retry":
                break
        return counts


def _claim(row: Mapping[str, Any]) -> ProviderMembershipRepairClaim:
    return ProviderMembershipRepairClaim(
        request_id=int(row["id"]),
        claim_token=str(row["claim_token"]),
        model=str(row["model"]),
        source_routing_generation=int(row["source_routing_generation"]),
        source_cost_generation=int(row["source_cost_generation"]),
        slot_no=int(row["slot_no"]),
        outgoing_merchant_id=int(row["outgoing_merchant_id"]),
        outgoing_enodot_account_id=int(row["outgoing_enodot_account_id"]),
        outgoing_a6_token_id=int(row["outgoing_a6_token_id"]),
        reason_code=str(row["reason_code"]),
        attempt_count=int(row["attempt_count"]),
    )


def _slot_state(row: Mapping[str, Any]) -> ProviderMembershipSlotState:
    return ProviderMembershipSlotState(
        routing_generation=int(row["routing_generation"]),
        cost_generation=int(row["cost_generation"]),
        slot_no=int(row["slot_no"]),
        slot_state=str(row["slot_state"]),
        merchant_id=_optional_int(row.get("merchant_id")),
        enodot_account_id=_optional_int(row.get("enodot_account_id")),
        a6_token_id=_optional_int(row.get("a6_token_id")),
        route_mode=_optional_text(row.get("route_mode")),
        observed_schedulable=bool(row.get("observed_schedulable")),
        auto_pool_generation_id=_optional_int(row.get("auto_pool_generation_id")),
        auto_pool_generation_state=_optional_text(
            row.get("auto_pool_generation_state")
        ),
        auto_pool_account_state=_optional_text(row.get("auto_pool_account_state")),
        ban_confirmed=bool(row.get("ban_confirmed")),
        auto_pool_confirmed=bool(row.get("auto_pool_confirmed")),
        cost_confirmed=bool(row.get("cost_confirmed")),
        schedulable_confirmed=bool(row.get("schedulable_confirmed")),
    )


def _target_values(
    state: ProviderMembershipSlotState | None,
) -> tuple[int | None, int | None, int | None, int | None, int | None]:
    if state is None or state.replacement_identity is None:
        return (None, None, None, None, None)
    merchant_id, account_id, token_id = state.replacement_identity
    return (
        state.routing_generation,
        state.cost_generation,
        merchant_id,
        account_id,
        token_id,
    )


def _result(
    claim: ProviderMembershipRepairClaim,
    state: ProviderMembershipSlotState,
    action: str,
) -> dict[str, Any]:
    identity = state.replacement_identity
    return {
        "request_id": claim.request_id,
        "model": claim.model,
        "slot_no": claim.slot_no,
        "action": action,
        "routing_generation": state.routing_generation,
        "cost_generation": state.cost_generation,
        "slot_state": state.slot_state,
        "route_mode": state.route_mode,
        "replacement_merchant_id": None if identity is None else identity[0],
        "replacement_enodot_account_id": None if identity is None else identity[1],
        "replacement_a6_token_id": None if identity is None else identity[2],
        "auto_pool_ready": state.auto_pool_ready,
    }


def _retry_delay(attempt_count: int) -> int:
    return min(300, max(5, 2 ** min(max(1, attempt_count), 8)))


def _optional_int(value: object) -> int | None:
    return None if value is None else int(value)


def _optional_text(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


def _error(value: object) -> str:
    return str(value or "provider_membership_repair_unknown").strip().casefold()[:160]


def _json(value: Mapping[str, Any]) -> str:
    return json.dumps(dict(value), ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _row(cursor: Any) -> dict[str, Any] | None:
    value = cursor.fetchone()
    if value is None:
        return None
    if isinstance(value, Mapping):
        return dict(value)
    names = [column.name for column in cursor.description or ()]
    return dict(zip(names, value, strict=False))


def _close(connection: Any) -> None:
    close = getattr(connection, "close", None)
    if callable(close):
        close()
