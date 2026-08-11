from __future__ import annotations

import copy
import json
import os
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any

import pytest

from a6api_auto.trade_provider_replay import (
    AttemptResult,
    AttemptState,
    DispatchCandidate,
    PostgresReplayStore,
    ProviderAttempt,
    ReliableResponseBuffer,
    ReplayCoordinator,
    ReplayError,
    RequestEnvelope,
    SSETerminalValidator,
)


def envelope(**kwargs) -> RequestEnvelope:
    values = {
        "request_id": "req-1",
        "model": "gpt-5.6-sol",
        "protocol": "responses",
        "method": "POST",
        "path": "/v1/responses",
        "headers": {"Authorization": "redacted", "Content-Length": "3"},
        "body": b"abc",
        "created_at": datetime.now(timezone.utc),
        "routing_generation": 1,
        "cost_generation": 1,
    }
    values.update(kwargs)
    return RequestEnvelope(**values)


def candidate(slot: int, priority: int) -> DispatchCandidate:
    return DispatchCandidate(slot, 100 + slot, 200 + slot, 300 + slot, priority)


@dataclass
class FakeReplayDatabase:
    envelopes: dict[str, dict[str, Any]] = field(default_factory=dict)
    attempts: dict[str, dict[str, Any]] = field(default_factory=dict)
    connections: list[FakeReplayConnection] = field(default_factory=list)
    fail_winner_attempt_update: bool = False

    def connection(self) -> FakeReplayConnection:
        connection = FakeReplayConnection(self)
        self.connections.append(connection)
        return connection

    def snapshot(self) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
        return copy.deepcopy((self.envelopes, self.attempts))

    def restore(
        self,
        snapshot: tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]],
    ) -> None:
        self.envelopes, self.attempts = copy.deepcopy(snapshot)


class FakeReplayConnection:
    def __init__(self, database: FakeReplayDatabase) -> None:
        self.database = database
        self.calls: list[tuple[str, Any]] = []
        self.commits = 0
        self.rollbacks = 0
        self.closed = False
        self._snapshot = database.snapshot()

    def cursor(self) -> FakeReplayCursor:
        return FakeReplayCursor(self)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1
        self.database.restore(self._snapshot)

    def close(self) -> None:
        self.closed = True


class FakeReplayCursor:
    def __init__(self, connection: FakeReplayConnection) -> None:
        self.connection = connection
        self.result: dict[str, Any] | None = None
        self.rowcount = -1

    def __enter__(self) -> FakeReplayCursor:
        return self

    def __exit__(self, *_args: object) -> bool:
        return False

    def execute(self, query: str, params: Any = None) -> FakeReplayCursor:
        normalized = " ".join(query.split())
        self.connection.calls.append((normalized, params))
        self.result = None
        self.rowcount = -1

        if normalized.startswith("INSERT INTO a6_trade.provider_request_envelopes"):
            self._create_envelope(params)
            return self
        if normalized.startswith("INSERT INTO a6_trade.provider_attempts"):
            self._record_attempt(params)
            return self
        if normalized.startswith("SELECT EXISTS ( SELECT 1 FROM a6_trade.provider_request"):
            request_id, attempt_id = params
            self.result = {
                "envelope_exists": request_id in self.connection.database.envelopes,
                "attempt_exists": attempt_id in self.connection.database.attempts,
            }
            return self
        if normalized.startswith("SELECT envelope.id, envelope.winning_attempt_id"):
            attempt_id, request_id = params
            envelope_row = self.connection.database.envelopes.get(request_id)
            if envelope_row is not None:
                attempt_row = self.connection.database.attempts.get(attempt_id)
                candidate_exists = bool(
                    attempt_row is not None
                    and attempt_row["request_envelope_id"] == envelope_row["id"]
                    and attempt_row["request_id"] == request_id
                    and attempt_row["state"] in {"dispatched", "buffered", "won"}
                )
                self.result = {
                    "id": envelope_row["id"],
                    "winning_attempt_id": envelope_row["winning_attempt_id"],
                    "candidate_exists": candidate_exists,
                }
            return self
        if normalized.startswith("UPDATE a6_trade.provider_request_envelopes"):
            attempt_id, request_id = params
            envelope_row = self.connection.database.envelopes.get(request_id)
            if (
                envelope_row is not None
                and envelope_row["winning_attempt_id"] is None
                and not envelope_row["response_committed"]
            ):
                envelope_row.update(
                    winning_attempt_id=attempt_id,
                    response_committed=True,
                    logical_outcome="success",
                )
                self.rowcount = 1
            else:
                self.rowcount = 0
            return self
        if normalized.startswith("UPDATE a6_trade.provider_attempts"):
            request_id, attempt_id = params
            attempt_row = self.connection.database.attempts.get(attempt_id)
            if (
                not self.connection.database.fail_winner_attempt_update
                and attempt_row is not None
                and attempt_row["request_id"] == request_id
                and attempt_row["state"] in {"dispatched", "buffered", "won"}
            ):
                attempt_row.update(state="won", response_committed=True)
                self.rowcount = 1
            else:
                self.rowcount = 0
            return self
        if normalized.startswith("SELECT winning_attempt_id"):
            request_id = params[0]
            envelope_row = self.connection.database.envelopes.get(request_id)
            if envelope_row is not None:
                self.result = {"winning_attempt_id": envelope_row["winning_attempt_id"]}
            return self
        raise AssertionError(f"unexpected SQL: {normalized}")

    def fetchone(self) -> dict[str, Any] | None:
        return self.result

    def _create_envelope(self, params: tuple[Any, ...]) -> None:
        database = self.connection.database
        request_id = params[0]
        proposed = {
            "id": len(database.envelopes) + 1,
            "request_id": request_id,
            "trace_id": params[1],
            "model": params[2],
            "protocol": params[3],
            "method": params[4],
            "path": params[5],
            "headers": json.loads(params[6]),
            "request_body": params[7],
            "body_sha256": params[8],
            "routing_generation": params[9],
            "cost_generation": params[10],
            "max_attempts": params[11],
            "max_total_seconds": params[12],
            "max_retry_cost_micros": params[13],
            "created_at": params[14],
            "winning_attempt_id": None,
            "response_committed": False,
            "logical_outcome": None,
        }
        existing = database.envelopes.get(request_id)
        if existing is None:
            database.envelopes[request_id] = proposed
            self.result = {"id": proposed["id"]}
            self.rowcount = 1
            return
        comparable = set(proposed) - {
            "id",
            "winning_attempt_id",
            "response_committed",
            "logical_outcome",
        }
        if all(existing[key] == proposed[key] for key in comparable):
            self.result = {"id": existing["id"]}
            self.rowcount = 1
        else:
            self.rowcount = 0

    def _record_attempt(self, params: tuple[Any, ...]) -> None:
        database = self.connection.database
        request_id = params[1]
        envelope_row = database.envelopes.get(request_id)
        if envelope_row is None:
            self.rowcount = 0
            return
        proposed = {
            "attempt_id": params[0],
            "request_envelope_id": envelope_row["id"],
            "request_id": request_id,
            "attempt_no": params[2],
            "model": envelope_row["model"],
            "routing_generation": params[3],
            "cost_generation": params[4],
            "slot_no": params[5],
            "enodot_account_id": params[6],
            "a6_token_id": params[7],
            "channel_id": params[8],
            "endpoint_host": params[9],
            "egress_id": params[10],
            "state": params[11],
            "http_status": params[12],
            "failure_owner": params[13],
            "failure_code": params[14],
            "terminal_event_seen": params[15],
            "response_committed": params[16],
            "response_body_sha256": params[17],
            "actual_cost_micros": params[18],
            "transport_status": params[19],
            "delivery_state": params[20],
        }
        existing = database.attempts.get(proposed["attempt_id"])
        if existing is None:
            database.attempts[proposed["attempt_id"]] = proposed
            self.rowcount = 1
            return
        identity_fields = {
            "request_envelope_id",
            "request_id",
            "attempt_no",
            "model",
            "routing_generation",
            "cost_generation",
            "slot_no",
            "enodot_account_id",
            "a6_token_id",
            "channel_id",
            "endpoint_host",
            "egress_id",
        }
        transition_allowed = existing["state"] == proposed["state"] or (
            existing["state"] == "created"
            or existing["state"] == "dispatched"
            and proposed["state"] in {"buffered", "failed", "won", "lost", "cancelled"}
            or existing["state"] == "buffered"
            and proposed["state"] in {"failed", "won", "lost", "cancelled"}
        )
        commit_monotonic = not existing["response_committed"] or proposed["response_committed"]
        if (
            all(existing[field] == proposed[field] for field in identity_fields)
            and transition_allowed
            and commit_monotonic
        ):
            existing.update(proposed)
            self.rowcount = 1
        else:
            self.rowcount = 0


def provider_attempt(
    *,
    request_id: str = "req-1",
    attempt_no: int = 1,
    slot_no: int = 1,
    state: AttemptState = AttemptState.DISPATCHED,
    **kwargs: Any,
) -> ProviderAttempt:
    values = {
        "attempt_id": f"{request_id}:attempt-{attempt_no}",
        "request_id": request_id,
        "attempt_no": attempt_no,
        "candidate": candidate(slot_no, slot_no),
        "routing_generation": 1,
        "cost_generation": 1,
        "state": state,
    }
    values.update(kwargs)
    return ProviderAttempt(**values)


def test_replay_hides_failed_attempt_and_commits_one_winner() -> None:
    calls = []

    def dispatch(_envelope, selected, _attempt_no):
        calls.append(selected.slot_no)
        if selected.slot_no == 1:
            return AttemptResult(
                status_code=503,
                failure_owner="provider",
                failure_code="fixed_merchant_unavailable",
            )
        return AttemptResult(status_code=200, body=b"winner", success=True)

    outcome = ReplayCoordinator().run(envelope(), (candidate(1, 1), candidate(2, 2)), dispatch)

    assert calls == [1, 2]
    assert outcome.logical_outcome == "success"
    assert outcome.winning_attempt_id == "req-1:attempt-2"
    assert len([item for item in outcome.attempts if item.response_committed]) == 1
    assert outcome.attempts[0].failure_code == "fixed_merchant_unavailable"


def test_zero_retry_cost_budget_is_disabled_and_does_not_reject_success() -> None:
    outcome = ReplayCoordinator().run(
        envelope(max_retry_cost_micros=0),
        (candidate(1, 1),),
        lambda *_: AttemptResult(
            status_code=200,
            body=b"winner",
            success=True,
            actual_cost_micros=10,
        ),
    )

    assert outcome.logical_outcome == "success"


def test_retry_cost_budget_stops_after_failure_without_erasing_transport_facts() -> None:
    calls: list[int] = []

    def dispatch(_envelope, selected, _attempt_no):
        calls.append(selected.slot_no)
        return AttemptResult(
            status_code=503,
            failure_owner="provider",
            failure_code="fixed_merchant_unavailable",
            actual_cost_micros=5,
            transport_status=524,
            delivery_state="delivery_aborted",
        )

    outcome = ReplayCoordinator().run(
        envelope(max_retry_cost_micros=5),
        (candidate(1, 1), candidate(2, 2)),
        dispatch,
    )

    assert calls == [1]
    assert outcome.attempts[0].failure_code == "fixed_merchant_unavailable"
    assert outcome.attempts[0].transport_status == 524
    assert outcome.attempts[0].delivery_state == "delivery_aborted"


def test_postgres_replay_store_persists_frozen_budget_and_attempt_facts() -> None:
    database = FakeReplayDatabase()
    store = PostgresReplayStore(database.connection)
    frozen = envelope(max_retry_cost_micros=1234, max_total_seconds=90.5)

    envelope_id = store.create_envelope(frozen)
    store.record(
        provider_attempt(
            state=AttemptState.FAILED,
            status_code=503,
            failure_owner="provider",
            failure_code="fixed_merchant_unavailable",
            actual_cost_micros=37,
            transport_status=524,
            delivery_state="delivery_aborted",
        )
    )

    assert envelope_id == 1
    assert database.envelopes["req-1"]["max_retry_cost_micros"] == 1234
    assert database.envelopes["req-1"]["created_at"] == frozen.created_at
    persisted = database.attempts["req-1:attempt-1"]
    assert persisted["transport_status"] == 524
    assert persisted["delivery_state"] == "delivery_aborted"
    assert all(connection.commits == 1 for connection in database.connections)
    assert all(connection.closed for connection in database.connections)


def test_postgres_replay_store_rejects_envelope_and_attempt_identity_reuse() -> None:
    database = FakeReplayDatabase()
    store = PostgresReplayStore(database.connection)
    frozen = envelope()
    store.create_envelope(frozen)
    assert store.create_envelope(frozen) == 1

    with pytest.raises(ReplayError, match="replay_envelope_conflict"):
        store.create_envelope(replace(frozen, path="/v1/chat/completions"))

    original = provider_attempt()
    store.record(original)
    conflicting_candidate = replace(original.candidate, endpoint_host="other.fixture")
    with pytest.raises(ReplayError, match="replay_attempt_conflict"):
        store.record(replace(original, candidate=conflicting_candidate))

    assert database.attempts[original.attempt_id]["endpoint_host"] == "a6.a6api.com"
    assert database.connections[-1].rollbacks == 1


def test_postgres_replay_store_reports_missing_envelope() -> None:
    database = FakeReplayDatabase()

    with pytest.raises(ReplayError, match="replay_attempt_envelope_missing"):
        PostgresReplayStore(database.connection).record(provider_attempt())

    assert database.connections[-1].rollbacks == 1
    assert database.attempts == {}


def test_postgres_replay_winner_claim_is_atomic_and_idempotent() -> None:
    database = FakeReplayDatabase()
    store = PostgresReplayStore(database.connection)
    store.create_envelope(envelope())

    first = provider_attempt()
    second = provider_attempt(attempt_no=2, slot_no=2)
    assert store.claim("req-1", first.attempt_id) is False
    store.record(first)
    store.record(second)

    assert store.claim("req-1", first.attempt_id) is True
    assert database.envelopes["req-1"]["winning_attempt_id"] == first.attempt_id
    assert database.envelopes["req-1"]["response_committed"] is True
    assert database.attempts[first.attempt_id]["state"] == "won"
    assert database.attempts[first.attempt_id]["response_committed"] is True
    assert store.claim("req-1", first.attempt_id) is True
    assert store.claim("req-1", second.attempt_id) is False
    assert store.winner("req-1") == first.attempt_id


def test_postgres_replay_winner_claim_rolls_back_envelope_when_attempt_update_fails() -> None:
    database = FakeReplayDatabase()
    store = PostgresReplayStore(database.connection)
    store.create_envelope(envelope())
    attempt = provider_attempt()
    store.record(attempt)
    database.fail_winner_attempt_update = True

    with pytest.raises(ReplayError, match="replay_winner_attempt_cas_mismatch"):
        store.claim("req-1", attempt.attempt_id)

    assert database.envelopes["req-1"]["winning_attempt_id"] is None
    assert database.envelopes["req-1"]["response_committed"] is False
    assert database.attempts[attempt.attempt_id]["state"] == "dispatched"
    assert database.connections[-1].commits == 0
    assert database.connections[-1].rollbacks == 1


def test_request_envelope_does_not_persist_credential_headers() -> None:
    value = envelope(
        headers={
            "Authorization": "Bearer secret",
            "Cookie": "session=secret",
            "Content-Type": "application/json",
        }
    )

    assert value.headers == {"content-type": "application/json"}


def test_replay_client_cancel_finalizes_without_dispatch() -> None:
    outcome = ReplayCoordinator().run(
        envelope(),
        (candidate(1, 1),),
        lambda *_: (_ for _ in ()).throw(AssertionError("must not dispatch")),
        client_cancelled=lambda: True,
    )
    assert outcome.logical_outcome == "client_cancelled_done"
    assert outcome.synthetic_response["status_code"] == 200


def test_sse_requires_exact_terminal_event() -> None:
    valid = (
        b"event: response.output_text.delta\n"
        b"data: {\"type\":\"response.output_text.delta\"}\n\n"
        b"event: response.completed\n"
        b"data: {\"type\":\"response.completed\"}\n\n"
    )
    assert SSETerminalValidator.validate(valid).valid is True
    assert SSETerminalValidator.validate(valid + b"event: response.failed\n\n").reason == (
        "events_after_terminal"
    )
    assert (
        SSETerminalValidator.validate(
            valid.replace(b"response.completed", b"response.failed")
        ).valid
        is False
    )


def test_response_buffer_spills_with_private_permissions(tmp_path) -> None:
    with ReliableResponseBuffer(directory=tmp_path, max_memory_bytes=2, max_bytes=10) as buffer:
        buffer.append(b"ab")
        buffer.append(b"cd")
        assert buffer.spilled is True
        assert buffer.read() == b"abcd"
        path = next(tmp_path.glob("*.spool"))
        assert os.stat(path).st_mode & 0o777 == 0o600
    assert not list(tmp_path.glob("*.spool"))
