"""Protocol-neutral request replay primitives for the Sub2API request plane.

The production Sub2API middleware can adapt these objects to Responses, Chat
Completions, or WebSocket handlers.  No upstream error body is copied into the
public outcome; only a normalized failure code is retained.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Protocol


class ReplayError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class AttemptState(str, Enum):
    CREATED = "created"
    DISPATCHED = "dispatched"
    BUFFERED = "buffered"
    FAILED = "failed"
    WON = "won"
    LOST = "lost"
    CANCELLED = "cancelled"


_DELIVERY_STATES = frozenset(
    {"not_started", "stream_open", "delivered", "delivery_aborted", "client_gone"}
)
_FAILURE_OWNERS = frozenset(
    {"user", "client_transport", "provider", "platform_path", "internal", "unknown"}
)


@dataclass(frozen=True)
class RequestEnvelope:
    request_id: str
    model: str
    protocol: str
    method: str
    path: str
    headers: Mapping[str, str]
    body: bytes
    created_at: datetime
    routing_generation: int
    cost_generation: int
    max_attempts: int = 0
    max_total_seconds: float = 600.0
    max_retry_cost_micros: int | None = None
    trace_id: str = ""

    def __post_init__(self) -> None:
        if not str(self.request_id).strip() or not str(self.model).strip():
            raise ReplayError("replay_envelope_identity_invalid")
        if not isinstance(self.body, bytes):
            raise ReplayError("replay_envelope_body_invalid")
        if not isinstance(self.headers, Mapping):
            raise ReplayError("replay_envelope_headers_invalid")
        if isinstance(self.routing_generation, bool) or self.routing_generation < 1:
            raise ReplayError("replay_envelope_routing_generation_invalid")
        if isinstance(self.cost_generation, bool) or self.cost_generation < 1:
            raise ReplayError("replay_envelope_cost_generation_invalid")
        if isinstance(self.max_attempts, bool) or self.max_attempts < 0:
            raise ReplayError("replay_envelope_max_attempts_invalid")
        if self.max_total_seconds <= 0:
            raise ReplayError("replay_envelope_deadline_invalid")
        if self.max_retry_cost_micros is not None and (
            isinstance(self.max_retry_cost_micros, bool) or self.max_retry_cost_micros < 0
        ):
            raise ReplayError("replay_envelope_retry_budget_invalid")
        if not isinstance(self.created_at, datetime) or self.created_at.tzinfo is None:
            raise ReplayError("replay_envelope_created_at_invalid")
        normalized_headers = {
            str(name).strip().casefold(): str(value)
            for name, value in self.headers.items()
            if str(name).strip().casefold()
            not in {
                "authorization",
                "content-length",
                "connection",
                "cookie",
                "host",
                "proxy-authorization",
                "set-cookie",
            }
        }
        object.__setattr__(self, "headers", normalized_headers)

    @property
    def body_sha256(self) -> str:
        return hashlib.sha256(self.body).hexdigest()

    @property
    def envelope_id(self) -> str:
        return f"{self.request_id}:{self.body_sha256[:16]}"


@dataclass(frozen=True)
class DispatchCandidate:
    slot_no: int
    account_id: int
    token_id: int
    channel_id: int
    priority: int
    endpoint_host: str = "a6.a6api.com"
    egress_id: str = "direct"

    def __post_init__(self) -> None:
        for name in ("slot_no", "account_id", "token_id", "channel_id", "priority"):
            value = getattr(self, name)
            if isinstance(value, bool) or value < 1:
                raise ReplayError(f"replay_candidate_{name}_invalid")
        if not str(self.endpoint_host).strip():
            raise ReplayError("replay_candidate_endpoint_invalid")


@dataclass(frozen=True)
class AttemptResult:
    status_code: int
    body: bytes = b""
    success: bool = False
    terminal_event_seen: bool = False
    failure_owner: str = "unknown"
    failure_code: str = ""
    retryable: bool = True
    actual_cost_micros: int = 0
    transport_status: int | None = None
    delivery_state: str = "not_started"

    def __post_init__(self) -> None:
        if isinstance(self.status_code, bool) or not 100 <= self.status_code <= 599:
            raise ReplayError("replay_attempt_status_invalid")
        if not isinstance(self.body, bytes):
            raise ReplayError("replay_attempt_body_invalid")
        if isinstance(self.actual_cost_micros, bool) or self.actual_cost_micros < 0:
            raise ReplayError("replay_attempt_cost_invalid")
        if self.success and not 200 <= self.status_code < 300:
            raise ReplayError("replay_attempt_success_status_invalid")
        if self.failure_owner not in _FAILURE_OWNERS:
            raise ReplayError("replay_attempt_failure_owner_invalid")
        if self.transport_status is not None and (
            isinstance(self.transport_status, bool) or not 100 <= self.transport_status <= 599
        ):
            raise ReplayError("replay_attempt_transport_status_invalid")
        if self.delivery_state not in _DELIVERY_STATES:
            raise ReplayError("replay_attempt_delivery_state_invalid")


@dataclass(frozen=True)
class ProviderAttempt:
    attempt_id: str
    request_id: str
    attempt_no: int
    candidate: DispatchCandidate
    routing_generation: int
    cost_generation: int
    state: AttemptState = AttemptState.CREATED
    status_code: int | None = None
    failure_owner: str | None = None
    failure_code: str | None = None
    terminal_event_seen: bool = False
    response_committed: bool = False
    body_sha256: str | None = None
    actual_cost_micros: int = 0
    transport_status: int | None = None
    delivery_state: str = "not_started"

    def __post_init__(self) -> None:
        if not str(self.attempt_id).strip() or not str(self.request_id).strip():
            raise ReplayError("replay_attempt_identity_invalid")
        if isinstance(self.attempt_no, bool) or self.attempt_no < 1:
            raise ReplayError("replay_attempt_number_invalid")
        if isinstance(self.routing_generation, bool) or self.routing_generation < 1:
            raise ReplayError("replay_attempt_routing_generation_invalid")
        if isinstance(self.cost_generation, bool) or self.cost_generation < 1:
            raise ReplayError("replay_attempt_cost_generation_invalid")
        if not isinstance(self.state, AttemptState):
            raise ReplayError("replay_attempt_state_invalid")
        if self.status_code is not None and (
            isinstance(self.status_code, bool) or not 100 <= self.status_code <= 599
        ):
            raise ReplayError("replay_attempt_status_invalid")
        if self.failure_owner is not None and self.failure_owner not in _FAILURE_OWNERS:
            raise ReplayError("replay_attempt_failure_owner_invalid")
        if isinstance(self.actual_cost_micros, bool) or self.actual_cost_micros < 0:
            raise ReplayError("replay_attempt_cost_invalid")
        if self.transport_status is not None and (
            isinstance(self.transport_status, bool) or not 100 <= self.transport_status <= 599
        ):
            raise ReplayError("replay_attempt_transport_status_invalid")
        if self.delivery_state not in _DELIVERY_STATES:
            raise ReplayError("replay_attempt_delivery_state_invalid")
        if self.body_sha256 is not None and (
            len(self.body_sha256) != 64
            or any(character not in "0123456789abcdefABCDEF" for character in self.body_sha256)
        ):
            raise ReplayError("replay_attempt_response_hash_invalid")
        if self.response_committed and self.state is not AttemptState.WON:
            raise ReplayError("replay_attempt_commit_state_invalid")


@dataclass(frozen=True)
class ReplayOutcome:
    request_id: str
    logical_outcome: str
    winning_attempt_id: str | None
    attempts: tuple[ProviderAttempt, ...]
    response: AttemptResult | None
    synthetic_response: Mapping[str, Any] | None = None


class AttemptRecorder(Protocol):
    def record(self, attempt: ProviderAttempt) -> None: ...


class WinnerCAS:
    """Process-local winner CAS; a database implementation can mirror this contract."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._winners: dict[str, str] = {}

    def claim(self, request_id: str, attempt_id: str) -> bool:
        with self._lock:
            current = self._winners.get(request_id)
            if current is None:
                self._winners[request_id] = attempt_id
                return True
            return current == attempt_id

    def winner(self, request_id: str) -> str | None:
        with self._lock:
            return self._winners.get(request_id)


class PostgresReplayStore:
    """Persist envelopes/attempts and perform the production winner CAS."""

    def __init__(self, connection_factory: Callable[[], Any]) -> None:
        self.connection_factory = connection_factory

    def create_envelope(self, envelope: RequestEnvelope) -> int:
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO a6_trade.provider_request_envelopes (
                        request_id, trace_id, model, protocol, method, path,
                        headers, request_body, body_sha256,
                        routing_generation, cost_generation,
                        max_attempts, max_total_seconds, max_retry_cost_micros,
                        created_at
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s,
                        %s::jsonb, %s, %s, %s, %s, %s, %s, %s, %s
                    )
                    ON CONFLICT (request_id) DO UPDATE SET
                        request_id = EXCLUDED.request_id
                    WHERE a6_trade.provider_request_envelopes.trace_id IS NOT DISTINCT FROM
                              EXCLUDED.trace_id
                      AND a6_trade.provider_request_envelopes.model = EXCLUDED.model
                      AND a6_trade.provider_request_envelopes.protocol = EXCLUDED.protocol
                      AND a6_trade.provider_request_envelopes.method = EXCLUDED.method
                      AND a6_trade.provider_request_envelopes.path = EXCLUDED.path
                      AND a6_trade.provider_request_envelopes.headers = EXCLUDED.headers
                      AND a6_trade.provider_request_envelopes.request_body = EXCLUDED.request_body
                      AND a6_trade.provider_request_envelopes.body_sha256 = EXCLUDED.body_sha256
                      AND a6_trade.provider_request_envelopes.routing_generation =
                          EXCLUDED.routing_generation
                      AND a6_trade.provider_request_envelopes.cost_generation =
                          EXCLUDED.cost_generation
                      AND a6_trade.provider_request_envelopes.max_attempts =
                          EXCLUDED.max_attempts
                      AND a6_trade.provider_request_envelopes.max_total_seconds =
                          EXCLUDED.max_total_seconds
                      AND a6_trade.provider_request_envelopes.max_retry_cost_micros
                          IS NOT DISTINCT FROM EXCLUDED.max_retry_cost_micros
                      AND a6_trade.provider_request_envelopes.created_at = EXCLUDED.created_at
                    RETURNING id
                    """,
                    (
                        envelope.request_id,
                        envelope.trace_id or None,
                        envelope.model,
                        envelope.protocol,
                        envelope.method,
                        envelope.path,
                        json.dumps(
                            dict(envelope.headers),
                            ensure_ascii=True,
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                        envelope.body,
                        envelope.body_sha256,
                        envelope.routing_generation,
                        envelope.cost_generation,
                        envelope.max_attempts,
                        envelope.max_total_seconds,
                        envelope.max_retry_cost_micros,
                        envelope.created_at,
                    ),
                )
                row = cursor.fetchone()
                if row is None:
                    raise ReplayError("replay_envelope_conflict")
                envelope_id = int(row["id"] if isinstance(row, Mapping) else row[0])
            connection.commit()
            return envelope_id
        except Exception:
            connection.rollback()
            raise
        finally:
            _close_connection(connection)

    def record(self, attempt: ProviderAttempt) -> None:
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO a6_trade.provider_attempts (
                        attempt_id, request_envelope_id, request_id, attempt_no,
                        model, routing_generation, cost_generation,
                        slot_no, enodot_account_id, a6_token_id, channel_id,
                        endpoint_host, egress_id, state, http_status,
                        failure_owner, failure_code, terminal_event_seen,
                        response_committed, response_body_sha256, actual_cost_micros,
                        transport_status, delivery_state, finished_at
                    )
                    SELECT
                        %s, envelope.id, %s, %s, envelope.model, %s, %s,
                        %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s, %s, %s,
                        CASE WHEN %s IN ('failed', 'won', 'lost', 'cancelled')
                             THEN now() ELSE NULL END
                    FROM a6_trade.provider_request_envelopes envelope
                    WHERE envelope.request_id = %s
                    ON CONFLICT (attempt_id) DO UPDATE SET
                        state = EXCLUDED.state,
                        http_status = EXCLUDED.http_status,
                        failure_owner = EXCLUDED.failure_owner,
                        failure_code = EXCLUDED.failure_code,
                        terminal_event_seen = EXCLUDED.terminal_event_seen,
                        response_committed = EXCLUDED.response_committed,
                        response_body_sha256 = EXCLUDED.response_body_sha256,
                        actual_cost_micros = EXCLUDED.actual_cost_micros,
                        transport_status = EXCLUDED.transport_status,
                        delivery_state = EXCLUDED.delivery_state,
                        finished_at = EXCLUDED.finished_at
                    WHERE a6_trade.provider_attempts.request_id = EXCLUDED.request_id
                      AND a6_trade.provider_attempts.request_envelope_id =
                          EXCLUDED.request_envelope_id
                      AND a6_trade.provider_attempts.attempt_no = EXCLUDED.attempt_no
                      AND a6_trade.provider_attempts.model = EXCLUDED.model
                      AND a6_trade.provider_attempts.routing_generation =
                          EXCLUDED.routing_generation
                      AND a6_trade.provider_attempts.cost_generation =
                          EXCLUDED.cost_generation
                      AND a6_trade.provider_attempts.slot_no = EXCLUDED.slot_no
                      AND a6_trade.provider_attempts.enodot_account_id =
                          EXCLUDED.enodot_account_id
                      AND a6_trade.provider_attempts.a6_token_id = EXCLUDED.a6_token_id
                      AND a6_trade.provider_attempts.channel_id = EXCLUDED.channel_id
                      AND a6_trade.provider_attempts.endpoint_host = EXCLUDED.endpoint_host
                      AND a6_trade.provider_attempts.egress_id = EXCLUDED.egress_id
                      AND (
                          a6_trade.provider_attempts.state = EXCLUDED.state
                          OR a6_trade.provider_attempts.state = 'created'
                          OR (
                              a6_trade.provider_attempts.state = 'dispatched'
                              AND EXCLUDED.state IN (
                                  'buffered', 'failed', 'won', 'lost', 'cancelled'
                              )
                          )
                          OR (
                              a6_trade.provider_attempts.state = 'buffered'
                              AND EXCLUDED.state IN ('failed', 'won', 'lost', 'cancelled')
                          )
                      )
                      AND (
                          NOT a6_trade.provider_attempts.response_committed
                          OR EXCLUDED.response_committed
                      )
                    """,
                    (
                        attempt.attempt_id,
                        attempt.request_id,
                        attempt.attempt_no,
                        attempt.routing_generation,
                        attempt.cost_generation,
                        attempt.candidate.slot_no,
                        attempt.candidate.account_id,
                        attempt.candidate.token_id,
                        attempt.candidate.channel_id,
                        attempt.candidate.endpoint_host,
                        attempt.candidate.egress_id,
                        attempt.state.value,
                        attempt.status_code,
                        attempt.failure_owner,
                        attempt.failure_code,
                        attempt.terminal_event_seen,
                        attempt.response_committed,
                        attempt.body_sha256,
                        attempt.actual_cost_micros,
                        attempt.transport_status,
                        attempt.delivery_state,
                        attempt.state.value,
                        attempt.request_id,
                    ),
                )
                if cursor.rowcount != 1:
                    cursor.execute(
                        """
                        SELECT
                            EXISTS (
                                SELECT 1
                                FROM a6_trade.provider_request_envelopes
                                WHERE request_id = %s
                            ) AS envelope_exists,
                            EXISTS (
                                SELECT 1
                                FROM a6_trade.provider_attempts
                                WHERE attempt_id = %s
                            ) AS attempt_exists
                        """,
                        (attempt.request_id, attempt.attempt_id),
                    )
                    row = cursor.fetchone()
                    envelope_exists = (
                        row.get("envelope_exists") if isinstance(row, Mapping) else row[0]
                    )
                    if not envelope_exists:
                        raise ReplayError("replay_attempt_envelope_missing")
                    raise ReplayError("replay_attempt_conflict")
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            _close_connection(connection)

    def claim(self, request_id: str, attempt_id: str) -> bool:
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT envelope.id, envelope.winning_attempt_id,
                           EXISTS (
                               SELECT 1
                               FROM a6_trade.provider_attempts attempt
                               WHERE attempt.request_envelope_id = envelope.id
                                 AND attempt.request_id = envelope.request_id
                                 AND attempt.attempt_id = %s
                                 AND attempt.state IN ('dispatched', 'buffered', 'won')
                           ) AS candidate_exists
                    FROM a6_trade.provider_request_envelopes envelope
                    WHERE envelope.request_id = %s
                    FOR UPDATE
                    """,
                    (attempt_id, request_id),
                )
                row = cursor.fetchone()
                if row is None:
                    connection.commit()
                    return False
                current = row.get("winning_attempt_id") if isinstance(row, Mapping) else row[1]
                candidate_exists = (
                    row.get("candidate_exists") if isinstance(row, Mapping) else row[2]
                )
                if not candidate_exists:
                    connection.commit()
                    return False
                if current is not None:
                    connection.commit()
                    return str(current) == str(attempt_id)
                cursor.execute(
                    """
                    UPDATE a6_trade.provider_request_envelopes
                    SET winning_attempt_id = %s,
                        response_committed = true,
                        logical_outcome = 'success',
                        completed_at = now()
                    WHERE request_id = %s
                      AND winning_attempt_id IS NULL
                      AND response_committed = false
                    """,
                    (attempt_id, request_id),
                )
                if cursor.rowcount != 1:
                    raise ReplayError("replay_winner_envelope_cas_mismatch")
                cursor.execute(
                    """
                    UPDATE a6_trade.provider_attempts
                    SET state = 'won', response_committed = true,
                        finished_at = COALESCE(finished_at, now())
                    WHERE request_id = %s AND attempt_id = %s
                      AND state IN ('dispatched', 'buffered', 'won')
                    """,
                    (request_id, attempt_id),
                )
                if cursor.rowcount != 1:
                    raise ReplayError("replay_winner_attempt_cas_mismatch")
            connection.commit()
            return True
        except Exception:
            connection.rollback()
            raise
        finally:
            _close_connection(connection)

    def winner(self, request_id: str) -> str | None:
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT winning_attempt_id
                    FROM a6_trade.provider_request_envelopes
                    WHERE request_id = %s
                    """,
                    (request_id,),
                )
                row = cursor.fetchone()
            if row is None:
                return None
            value = row.get("winning_attempt_id") if isinstance(row, Mapping) else row[0]
            return str(value) if value else None
        finally:
            _close_connection(connection)


class ReplayCoordinator:
    def __init__(
        self,
        *,
        winner_cas: WinnerCAS | None = None,
        recorder: AttemptRecorder | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.winner_cas = winner_cas or WinnerCAS()
        self.recorder = recorder
        self.clock = clock

    def run(
        self,
        envelope: RequestEnvelope,
        candidates: Sequence[DispatchCandidate],
        dispatch: Callable[[RequestEnvelope, DispatchCandidate, int], AttemptResult],
        *,
        client_cancelled: Callable[[], bool] | None = None,
    ) -> ReplayOutcome:
        """Run attempts against a frozen candidate set and commit at most one winner."""

        ordered = tuple(sorted(candidates, key=lambda item: (item.priority, item.slot_no)))
        if len({candidate.slot_no for candidate in ordered}) != len(ordered):
            raise ReplayError("replay_candidate_slot_duplicate")
        limit = (
            len(ordered)
            if envelope.max_attempts == 0
            else min(envelope.max_attempts, len(ordered))
        )
        started = self.clock()
        attempts: list[ProviderAttempt] = []
        retry_cost = 0
        for attempt_no, candidate in enumerate(ordered[:limit], start=1):
            if self.clock() - started >= envelope.max_total_seconds:
                break
            if client_cancelled is not None and client_cancelled():
                synthetic = SyntheticResponseAdapter.client_cancelled()
                return ReplayOutcome(
                    envelope.request_id,
                    "client_cancelled_done",
                    None,
                    tuple(attempts),
                    None,
                    synthetic,
                )
            attempt = ProviderAttempt(
                attempt_id=f"{envelope.request_id}:attempt-{attempt_no}",
                request_id=envelope.request_id,
                attempt_no=attempt_no,
                candidate=candidate,
                routing_generation=envelope.routing_generation,
                cost_generation=envelope.cost_generation,
                state=AttemptState.DISPATCHED,
            )
            self._record(attempt)
            try:
                result = dispatch(envelope, candidate, attempt_no)
                if not isinstance(result, AttemptResult):
                    raise ReplayError("replay_dispatch_result_invalid")
            except Exception as exc:
                result = AttemptResult(
                    status_code=599,
                    failure_owner="internal",
                    failure_code=_exception_code(exc),
                    retryable=True,
                )
            successful = result.success and 200 <= result.status_code < 300
            if successful and self.winner_cas.claim(envelope.request_id, attempt.attempt_id):
                winning = replace(
                    attempt,
                    state=AttemptState.WON,
                    status_code=result.status_code,
                    terminal_event_seen=result.terminal_event_seen,
                    response_committed=True,
                    body_sha256=hashlib.sha256(result.body).hexdigest(),
                    actual_cost_micros=result.actual_cost_micros,
                    transport_status=result.transport_status,
                    delivery_state=result.delivery_state,
                )
                self._record(winning)
                return ReplayOutcome(
                    envelope.request_id,
                    "success",
                    winning.attempt_id,
                    tuple(attempts + [winning]),
                    result,
                    None,
                )

            failed = replace(
                attempt,
                state=AttemptState.FAILED if not successful else AttemptState.LOST,
                status_code=result.status_code,
                failure_owner=result.failure_owner,
                failure_code=result.failure_code or "upstream_error",
                terminal_event_seen=result.terminal_event_seen,
                body_sha256=hashlib.sha256(result.body).hexdigest() if result.body else None,
                actual_cost_micros=result.actual_cost_micros,
                transport_status=result.transport_status,
                delivery_state=result.delivery_state,
            )
            attempts.append(failed)
            self._record(failed)
            if not successful:
                retry_cost += result.actual_cost_micros
            retry_budget_exhausted = (
                not successful
                and envelope.max_retry_cost_micros not in (None, 0)
                and retry_cost >= envelope.max_retry_cost_micros
            )
            if successful or not result.retryable or result.failure_owner == "client":
                break
            if retry_budget_exhausted:
                break

        return ReplayOutcome(
            envelope.request_id,
            "maintenance_done",
            None,
            tuple(attempts),
            None,
            SyntheticResponseAdapter.maintenance_done(),
        )

    def _record(self, attempt: ProviderAttempt) -> None:
        if self.recorder is not None:
            self.recorder.record(attempt)


class ReliableResponseBuffer:
    """Bounded response spool with restrictive permissions and deterministic cleanup."""

    def __init__(
        self,
        *,
        directory: Path,
        max_memory_bytes: int = 8 * 1024 * 1024,
        max_bytes: int = 128 * 1024 * 1024,
    ) -> None:
        if max_memory_bytes < 0 or max_bytes < 1 or max_memory_bytes > max_bytes:
            raise ReplayError("replay_buffer_limits_invalid")
        self.directory = directory.expanduser().resolve()
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.directory, 0o700)
        self.max_memory_bytes = max_memory_bytes
        self.max_bytes = max_bytes
        self._memory = bytearray()
        self._file_path: Path | None = None
        self._file = None
        self._size = 0
        self._closed = False

    @property
    def size(self) -> int:
        return self._size

    @property
    def spilled(self) -> bool:
        return self._file is not None

    def append(self, chunk: bytes) -> None:
        if self._closed:
            raise ReplayError("replay_buffer_closed")
        if not isinstance(chunk, bytes):
            raise ReplayError("replay_buffer_chunk_invalid")
        if self._size + len(chunk) > self.max_bytes:
            raise ReplayError("replay_buffer_limit_exceeded")
        if self._file is None and self._size + len(chunk) <= self.max_memory_bytes:
            self._memory.extend(chunk)
        else:
            self._ensure_file()
            self._file.write(chunk)
            self._file.flush()
        self._size += len(chunk)

    def read(self) -> bytes:
        if self._file is None:
            return bytes(self._memory)
        self._file.flush()
        with self._file_path.open("rb") as handle:
            return handle.read()

    def close(self, *, delete: bool = True) -> None:
        if self._closed:
            return
        self._closed = True
        if self._file is not None:
            self._file.close()
            self._file = None
        if delete and self._file_path is not None:
            with suppress(FileNotFoundError):
                self._file_path.unlink()
            self._file_path = None

    def __enter__(self) -> ReliableResponseBuffer:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _ensure_file(self) -> None:
        if self._file is not None:
            return
        for _ in range(10):
            candidate = self.directory / f"replay-{uuid.uuid4().hex}.spool"
            try:
                descriptor = os.open(candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                continue
            self._file_path = candidate
            self._file = os.fdopen(descriptor, "wb")
            if self._memory:
                self._file.write(self._memory)
                self._memory.clear()
            return
        raise ReplayError("replay_buffer_spool_create_failed")


@dataclass(frozen=True)
class SSEValidation:
    valid: bool
    terminal_event_seen: bool
    event_count: int
    reason: str = ""


class SSETerminalValidator:
    TERMINAL_EVENT = "response.completed"

    @classmethod
    def validate(cls, payload: bytes | str | Iterable[bytes | str]) -> SSEValidation:
        if isinstance(payload, (bytes, str)):
            chunks: Iterable[bytes | str] = (payload,)
        else:
            chunks = payload
        raw = b"".join(
            chunk if isinstance(chunk, bytes) else str(chunk).encode() for chunk in chunks
        )
        events = _parse_sse_events(raw)
        if not events:
            return SSEValidation(False, False, 0, "empty")
        terminal_indexes = [
            index for index, event in enumerate(events) if event[0] == cls.TERMINAL_EVENT
        ]
        if len(terminal_indexes) != 1:
            return SSEValidation(False, bool(terminal_indexes), len(events), "terminal_count")
        terminal_index = terminal_indexes[0]
        if terminal_index != len(events) - 1:
            return SSEValidation(False, True, len(events), "events_after_terminal")
        data = events[terminal_index][1]
        if data:
            try:
                document = json.loads(data)
            except json.JSONDecodeError:
                return SSEValidation(False, True, len(events), "terminal_json")
            if (
                isinstance(document, Mapping)
                and document.get("type") not in {None, cls.TERMINAL_EVENT}
            ):
                return SSEValidation(False, True, len(events), "terminal_type")
        return SSEValidation(True, True, len(events))


class SyntheticResponseAdapter:
    """Stable HTTP-200 protocol envelopes for logical non-success outcomes."""

    @staticmethod
    def user_fault(code: str, message: str = "Request rejected") -> dict[str, Any]:
        return {
            "status_code": 200,
            "logical_outcome": "user_fault_done",
            "body": {
                "error": {
                    "type": "invalid_request_error",
                    "code": str(code),
                    "message": message,
                }
            },
        }

    @staticmethod
    def maintenance_done(reason: str = "provider_pool_temporarily_unavailable") -> dict[str, Any]:
        return {
            "status_code": 200,
            "logical_outcome": "maintenance_done",
            "body": {
                "error": {
                    "type": "server_error",
                    "code": str(reason),
                    "message": "Provider pool temporarily unavailable",
                }
            },
        }

    @staticmethod
    def client_cancelled() -> dict[str, Any]:
        return {
            "status_code": 200,
            "logical_outcome": "client_cancelled_done",
            "body": {"type": "response.completed", "status": "cancelled"},
            "transport_status": 499,
            "delivery_state": "client_gone",
        }


def _parse_sse_events(raw: bytes) -> list[tuple[str, str]]:
    events: list[tuple[str, str]] = []
    for block in raw.replace(b"\r\n", b"\n").split(b"\n\n"):
        if not block.strip() or block.lstrip().startswith(b":"):
            continue
        event_name = "message"
        data_lines: list[str] = []
        for line in block.split(b"\n"):
            if line.startswith(b"event:"):
                event_name = line[6:].strip().decode("utf-8", errors="replace")
            elif line.startswith(b"data:"):
                data_lines.append(line[5:].lstrip().decode("utf-8", errors="replace"))
        events.append((event_name, "\n".join(data_lines)))
    return events


def _exception_code(exc: Exception) -> str:
    text = str(exc).strip().casefold().replace(" ", "_")
    return text[:128] or exc.__class__.__name__.casefold()


def _close_connection(connection: Any) -> None:
    close = getattr(connection, "close", None)
    if callable(close):
        close()
