"""Durable bridge from Sub2API runtime failures to Provider control."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from .trade_channel_failure import (
    FAILURE_CLASSIFIER_VERSION,
    classify_provider_attempt_failure,
)
from .trade_publication_control import PostgresPublicationControl, PublicationControlError


class ProviderRuntimeFailureError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class ProviderRuntimeFailureClaim:
    event_id: int
    claim_token: str
    source_error_log_id: int
    upstream_ordinal: int
    source_request_id: str | None
    model: str
    failure_code: str
    http_status: int | None
    occurred_at: datetime
    routing_generation: int
    cost_generation: int
    slot_no: int
    enodot_account_id: int
    a6_token_id: int
    channel_id: int | None
    pin_assignment_epoch_id: int | None
    route_mode: str
    auto_pool_generation_id: int | None
    auto_pool_subscription_id: int | None
    consecutive_failure_count: int
    block_action: str
    attempt_count: int

    @property
    def request_id(self) -> str:
        return f"a6-runtime:{self.source_error_log_id}:{self.upstream_ordinal}"

    @property
    def attempt_id(self) -> str:
        return f"a6-runtime-attempt:{self.source_error_log_id}:{self.upstream_ordinal}"


class ProviderRuntimeWakeTrigger(Protocol):
    def wake(self, *, model: str, request_id: int) -> None: ...


class PostgresProviderRuntimeFailureStore:
    def __init__(
        self,
        connection_factory: Callable[[], Any],
        *,
        stale_claim_seconds: int = 60,
    ) -> None:
        if not callable(connection_factory):
            raise ValueError("provider_runtime_failure_connection_factory_invalid")
        if isinstance(stale_claim_seconds, bool) or stale_claim_seconds < 5:
            raise ValueError("provider_runtime_failure_stale_claim_invalid")
        self.connection_factory = connection_factory
        self.stale_claim_seconds = int(stale_claim_seconds)

    def claim(self, *, worker_id: str) -> ProviderRuntimeFailureClaim | None:
        worker_id = _identifier(worker_id, "provider_runtime_failure_worker_id_invalid")
        claim_token = uuid.uuid4().hex
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    WITH candidate AS (
                        SELECT event.id
                        FROM a6_trade.provider_runtime_failure_events AS event
                        WHERE event.control_required
                          AND (
                              (
                                  event.processing_state IN ('pending', 'retry')
                                  AND event.next_attempt_at <= now()
                              ) OR (
                                  event.processing_state = 'running'
                                  AND event.claimed_at <= now()
                                      - (%s * interval '1 second')
                              )
                          )
                          AND NOT EXISTS (
                              SELECT 1
                              FROM a6_trade.provider_runtime_failure_events AS earlier
                              WHERE earlier.model = event.model
                                AND earlier.enodot_account_id =
                                    event.enodot_account_id
                                AND earlier.route_mode = event.route_mode
                                AND (
                                    (
                                        event.route_mode = 'fixed_pin'
                                        AND earlier.pin_assignment_epoch_id =
                                            event.pin_assignment_epoch_id
                                    ) OR (
                                        event.route_mode = 'auto_pool'
                                        AND earlier.auto_pool_generation_id =
                                            event.auto_pool_generation_id
                                    )
                                )
                                AND earlier.control_required
                                AND (earlier.occurred_at, earlier.id)
                                    < (event.occurred_at, event.id)
                                AND earlier.processing_state NOT IN (
                                    'completed', 'ignored', 'superseded', 'failed'
                                )
                          )
                        ORDER BY event.occurred_at, event.id
                        FOR UPDATE SKIP LOCKED
                        LIMIT 1
                    )
                    UPDATE a6_trade.provider_runtime_failure_events AS event
                    SET processing_state = 'running',
                        attempt_count = event.attempt_count + 1,
                        claim_token = %s,
                        claimed_by = %s,
                        claimed_at = now(),
                        last_error_class = NULL,
                        updated_at = now()
                    FROM candidate
                    WHERE event.id = candidate.id
                    RETURNING event.id, event.claim_token,
                              event.source_error_log_id, event.upstream_ordinal,
                              event.source_request_id, event.model,
                              event.failure_code, event.http_status,
                              event.occurred_at, event.routing_generation,
                              event.cost_generation, event.slot_no,
                              event.enodot_account_id, event.a6_token_id,
                              event.channel_id, event.pin_assignment_epoch_id,
                              event.route_mode, event.auto_pool_generation_id,
                              event.auto_pool_subscription_id,
                              event.consecutive_failure_count,
                              event.block_action, event.attempt_count
                    """,
                    (self.stale_claim_seconds, claim_token, worker_id),
                )
                row = _row(cursor)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            _close(connection)
        return _claim(row) if row is not None else None

    def ensure_envelope(self, claim: ProviderRuntimeFailureClaim) -> None:
        body = b""
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO a6_trade.provider_request_envelopes (
                        request_id, trace_id, model, protocol, method, path,
                        headers, request_body, body_sha256,
                        routing_generation, cost_generation,
                        max_attempts, max_total_seconds, response_committed
                    ) VALUES (
                        %s, %s, %s, 'responses', 'POST', '/v1/responses',
                        '{}'::jsonb, %s, %s, %s, %s, 1, 30, false
                    )
                    ON CONFLICT (request_id) DO NOTHING
                    """,
                    (
                        claim.request_id,
                        claim.request_id,
                        claim.model,
                        body,
                        hashlib.sha256(body).hexdigest(),
                        claim.routing_generation,
                        claim.cost_generation,
                    ),
                )
                cursor.execute(
                    """
                    SELECT model, routing_generation, cost_generation,
                           response_committed
                    FROM a6_trade.provider_request_envelopes
                    WHERE request_id = %s
                    """,
                    (claim.request_id,),
                )
                observed = _row(cursor)
                if observed is None or (
                    str(observed.get("model")) != claim.model
                    or int(observed.get("routing_generation") or 0)
                    != claim.routing_generation
                    or int(observed.get("cost_generation") or 0)
                    != claim.cost_generation
                    or bool(observed.get("response_committed"))
                ):
                    raise ProviderRuntimeFailureError(
                        "provider_runtime_failure_envelope_readback_mismatch"
                    )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            _close(connection)

    def complete(
        self,
        claim: ProviderRuntimeFailureClaim,
        *,
        result: Mapping[str, Any],
    ) -> None:
        self._finish(claim, state="completed", result=result, error_class=None)

    def supersede(self, claim: ProviderRuntimeFailureClaim, *, error_class: str) -> None:
        self._finish(
            claim,
            state="superseded",
            result={"reason": _error_code(error_class)},
            error_class=error_class,
        )

    def retry(
        self,
        claim: ProviderRuntimeFailureClaim,
        *,
        error_class: str,
        retry_after_seconds: int,
    ) -> None:
        error_class = _error_code(error_class)
        retry_after_seconds = max(1, min(int(retry_after_seconds), 300))
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE a6_trade.provider_runtime_failure_events
                    SET processing_state = 'retry', claim_token = NULL,
                        claimed_by = NULL, claimed_at = NULL,
                        next_attempt_at = now() + (%s * interval '1 second'),
                        last_error_class = %s, updated_at = now()
                    WHERE id = %s AND processing_state = 'running'
                      AND claim_token = %s
                    RETURNING id
                    """,
                    (
                        retry_after_seconds,
                        error_class,
                        claim.event_id,
                        claim.claim_token,
                    ),
                )
                if _row(cursor) is None:
                    raise ProviderRuntimeFailureError(
                        "provider_runtime_failure_retry_cas_mismatch"
                    )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            _close(connection)

    def _finish(
        self,
        claim: ProviderRuntimeFailureClaim,
        *,
        state: str,
        result: Mapping[str, Any],
        error_class: str | None,
    ) -> None:
        if state not in {"completed", "superseded", "failed"}:
            raise ValueError("provider_runtime_failure_finish_state_invalid")
        encoded = json.dumps(dict(result), ensure_ascii=True, sort_keys=True)
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE a6_trade.provider_runtime_failure_events
                    SET processing_state = %s, claim_token = NULL,
                        claimed_by = NULL, claimed_at = NULL,
                        completed_at = now(), last_error_class = %s,
                        result = %s::jsonb, updated_at = now()
                    WHERE id = %s AND processing_state = 'running'
                      AND claim_token = %s
                    RETURNING id
                    """,
                    (
                        state,
                        _error_code(error_class) if error_class else None,
                        encoded,
                        claim.event_id,
                        claim.claim_token,
                    ),
                )
                if _row(cursor) is None:
                    raise ProviderRuntimeFailureError(
                        "provider_runtime_failure_finish_cas_mismatch"
                    )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            _close(connection)


class ProviderRuntimeFailureConsumer:
    _SUPERSEDED_ERRORS = frozenset(
        {
            "publication_expected_generation_conflict",
            "provider_failure_cost_generation_mismatch",
            "provider_failure_slot_identity_mismatch",
            "provider_failure_pin_epoch_missing",
        }
    )

    def __init__(
        self,
        store: PostgresProviderRuntimeFailureStore,
        control: PostgresPublicationControl,
        *,
        worker_id: str,
        auto_pool_trigger: ProviderRuntimeWakeTrigger | None = None,
        liquidity_trigger: ProviderRuntimeWakeTrigger | None = None,
    ) -> None:
        self.store = store
        self.control = control
        self.auto_pool_trigger = auto_pool_trigger
        self.liquidity_trigger = liquidity_trigger
        self.worker_id = _identifier(
            worker_id, "provider_runtime_failure_worker_id_invalid"
        )

    def run_once(self) -> dict[str, Any]:
        claim = self.store.claim(worker_id=self.worker_id)
        if claim is None:
            return {"status": "idle"}
        try:
            if claim.route_mode == "auto_pool":
                return self._handle_auto_pool(claim)
            self.store.ensure_envelope(claim)
            classification = classify_provider_attempt_failure(
                claim.failure_code,
                http_status=claim.http_status,
            )
            report = self.control.report_attempt_failure(
                {
                    "event_type": "provider_attempt_failed",
                    "idempotency_key": claim.request_id,
                    "expected_generation": claim.routing_generation,
                    "requested_by": "runtime-failure-bridge",
                    "trace_id": claim.request_id,
                    "request_id": claim.request_id,
                    "attempt_id": claim.attempt_id,
                    "attempt_no": 1,
                    "model": claim.model,
                    "routing_generation": claim.routing_generation,
                    "cost_generation": claim.cost_generation,
                    "slot_no": claim.slot_no,
                    "enodot_account_id": claim.enodot_account_id,
                    "a6_token_id": claim.a6_token_id,
                    "channel_id": claim.channel_id,
                    "endpoint_host": "a6.a6api.com",
                    "egress_id": "production-request-plane",
                    "region": "us_cloud",
                    "failure_owner": classification.owner,
                    "failure_code": classification.code,
                    "http_status": claim.http_status,
                    "terminal_event_seen": True,
                    "occurred_at": claim.occurred_at.isoformat(),
                    "classifier_version": FAILURE_CLASSIFIER_VERSION,
                }
            )
            result = {
                "attempt_id": str(report["attempt_id"]),
                "slot_drained": bool(report["slot_drained"]),
                "control_action": str(report["control_action"]),
                "channel_failure_streak_count": int(
                    report.get("channel_failure_streak_count") or 0
                ),
                "block_action": claim.block_action,
            }
            self.store.complete(claim, result=result)
            return {"status": "completed", "event_id": claim.event_id, **result}
        except PublicationControlError as exc:
            if exc.code in self._SUPERSEDED_ERRORS:
                self.store.supersede(claim, error_class=exc.code)
                return {
                    "status": "superseded",
                    "event_id": claim.event_id,
                    "error_class": exc.code,
                }
            self.store.retry(
                claim,
                error_class=exc.code,
                retry_after_seconds=_retry_delay(claim.attempt_count),
            )
            return {
                "status": "retry",
                "event_id": claim.event_id,
                "error_class": exc.code,
            }
        except Exception as exc:
            error_class = _error_code(getattr(exc, "code", None) or type(exc).__name__)
            self.store.retry(
                claim,
                error_class=error_class,
                retry_after_seconds=_retry_delay(claim.attempt_count),
            )
            return {
                "status": "retry",
                "event_id": claim.event_id,
                "error_class": error_class,
            }

    def _handle_auto_pool(
        self,
        claim: ProviderRuntimeFailureClaim,
    ) -> dict[str, Any]:
        if claim.block_action == "refresh_auto_pool":
            trigger = self.auto_pool_trigger
            control_action = "observe_then_refresh_auto_pool"
        elif claim.block_action == "block_liquidity":
            trigger = self.liquidity_trigger
            control_action = "repair_liquidity"
        else:
            raise ProviderRuntimeFailureError(
                "provider_runtime_failure_auto_pool_action_invalid"
            )
        if trigger is None:
            raise ProviderRuntimeFailureError(
                "provider_runtime_failure_trigger_missing"
            )
        trigger.wake(model=claim.model, request_id=claim.event_id)
        result = {
            "attempt_id": claim.attempt_id,
            "slot_drained": True,
            "control_action": control_action,
            "channel_failure_streak_count": claim.consecutive_failure_count,
            "block_action": claim.block_action,
            "route_mode": claim.route_mode,
            "auto_pool_generation_id": claim.auto_pool_generation_id,
            "auto_pool_subscription_id": claim.auto_pool_subscription_id,
        }
        self.store.complete(claim, result=result)
        return {"status": "completed", "event_id": claim.event_id, **result}

    def drain(self, *, limit: int = 100) -> dict[str, Any]:
        if isinstance(limit, bool) or limit < 1:
            raise ValueError("provider_runtime_failure_drain_limit_invalid")
        results: list[dict[str, Any]] = []
        for _ in range(limit):
            result = self.run_once()
            if result["status"] == "idle":
                break
            results.append(result)
            if result["status"] == "retry":
                break
        return {
            "processed": len(results),
            "completed": sum(item["status"] == "completed" for item in results),
            "superseded": sum(item["status"] == "superseded" for item in results),
            "retry": sum(item["status"] == "retry" for item in results),
        }


def _claim(row: Mapping[str, Any]) -> ProviderRuntimeFailureClaim:
    return ProviderRuntimeFailureClaim(
        event_id=int(row["id"]),
        claim_token=str(row["claim_token"]),
        source_error_log_id=int(row["source_error_log_id"]),
        upstream_ordinal=int(row["upstream_ordinal"]),
        source_request_id=(
            str(row["source_request_id"])
            if row.get("source_request_id") is not None
            else None
        ),
        model=str(row["model"]),
        failure_code=str(row["failure_code"]),
        http_status=int(row["http_status"]) if row.get("http_status") is not None else None,
        occurred_at=row["occurred_at"],
        routing_generation=int(row["routing_generation"]),
        cost_generation=int(row["cost_generation"]),
        slot_no=int(row["slot_no"]),
        enodot_account_id=int(row["enodot_account_id"]),
        a6_token_id=int(row["a6_token_id"]),
        channel_id=(
            int(row["channel_id"]) if row.get("channel_id") is not None else None
        ),
        pin_assignment_epoch_id=(
            int(row["pin_assignment_epoch_id"])
            if row.get("pin_assignment_epoch_id") is not None
            else None
        ),
        route_mode=str(row.get("route_mode") or "fixed_pin"),
        auto_pool_generation_id=(
            int(row["auto_pool_generation_id"])
            if row.get("auto_pool_generation_id") is not None
            else None
        ),
        auto_pool_subscription_id=(
            int(row["auto_pool_subscription_id"])
            if row.get("auto_pool_subscription_id") is not None
            else None
        ),
        consecutive_failure_count=int(row["consecutive_failure_count"]),
        block_action=str(row["block_action"]),
        attempt_count=int(row["attempt_count"]),
    )


def _row(cursor: Any) -> dict[str, Any] | None:
    raw = cursor.fetchone()
    if raw is None:
        return None
    if isinstance(raw, Mapping):
        return dict(raw)
    description = getattr(cursor, "description", None) or ()
    return {str(item[0]): value for item, value in zip(description, raw, strict=False)}


def _close(connection: Any) -> None:
    close = getattr(connection, "close", None)
    if callable(close):
        close()


def _identifier(value: object, code: str) -> str:
    text = str(value or "").strip()
    if not text or len(text) > 192:
        raise ValueError(code)
    return text


def _error_code(value: object) -> str:
    text = str(value or "provider_runtime_failure_unknown").strip().casefold()
    normalized = "_".join(part for part in text.replace("-", "_").split("_") if part)
    return normalized[:160] or "provider_runtime_failure_unknown"


def _retry_delay(attempt_count: int) -> int:
    return min(5 * (2 ** max(0, min(attempt_count - 1, 5))), 120)
