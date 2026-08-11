from __future__ import annotations

import json
import stat
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Any

import pytest

from a6api_auto.trade_dashboard import create_dashboard_app
from a6api_auto.trade_db import _migration_files
from a6api_auto.trade_provider_fast_block import ProviderFastBlockProjectionResult
from a6api_auto.trade_publication_control import (
    AtomicPublicationTrigger,
    PostgresPublicationControl,
    ProviderAttemptFailureEvent,
    PublicationControlError,
)

UTC = timezone.utc
MODEL = "gpt-5.6-sol"


def failure_document(**overrides: Any) -> dict[str, Any]:
    document = {
        "event_type": "provider_attempt_failed",
        "idempotency_key": "req-42-attempt-1-failure",
        "expected_generation": 42,
        "requested_by": "sub2api-us",
        "trace_id": "trace-42",
        "request_id": "req-42",
        "attempt_id": "attempt-42-1",
        "attempt_no": 1,
        "model": MODEL,
        "routing_generation": 42,
        "cost_generation": 19,
        "slot_no": 1,
        "enodot_account_id": 7001,
        "a6_token_id": 8001,
        "channel_id": 1008,
        "endpoint_host": "a6.a6api.com",
        "egress_id": "direct",
        "failure_owner": "provider",
        "failure_code": "fixed_merchant_unavailable",
        "http_status": 503,
        "terminal_event_seen": False,
        "occurred_at": "2026-07-29T02:03:04Z",
        "classifier_version": "failure-classifier-v1",
    }
    document.update(overrides)
    return document


def numbered_failure(number: int, **overrides: Any) -> dict[str, Any]:
    return failure_document(
        idempotency_key=f"req-{number}-attempt-1-failure",
        trace_id=f"trace-{number}",
        request_id=f"req-{number}",
        attempt_id=f"attempt-{number}-1",
        occurred_at=f"2026-07-29T02:03:{number:02d}Z",
        **overrides,
    )


class Cursor:
    def __init__(self, database: Database) -> None:
        self.database = database
        self.result: dict[str, Any] | None = None
        self.results: list[dict[str, Any]] = []
        self.description = None
        self.rowcount = 0

    def __enter__(self) -> Cursor:
        return self

    def __exit__(self, *_args: Any) -> bool:
        return False

    def execute(self, query: str, params: Any = None) -> None:
        sql = " ".join(query.split())
        values = tuple(params or ())
        self.database.queries.append((sql, values))
        self.result = None
        self.results = []
        self.rowcount = 0
        if sql.startswith("SELECT generation.generation"):
            model = values[0]
            if model == MODEL:
                self.result = {"generation": 42, "cost_generation": 19}
        elif sql.startswith("SELECT slot.id, slot.slot_state"):
            identity = (values[0], values[1], values[2], values[3], values[4], values[5])
            expected = (
                self.database.slot_identity[0],
                self.database.slot_identity[1],
                self.database.slot_identity[3],
                self.database.slot_identity[4],
                self.database.slot_identity[5],
                self.database.slot_identity[6],
            )
            if identity == expected:
                self.result = dict(self.database.slot)
        elif sql.startswith("SELECT model, enodot_account_id, started_at"):
            self.result = {
                "model": MODEL,
                "enodot_account_id": self.database.slot_identity[4],
                "started_at": datetime(2026, 7, 29, 2, 0, tzinfo=UTC),
            }
        elif sql.startswith("SELECT to_regclass('public.usage_logs')"):
            self.result = {"usage_logs": None}
        elif sql.startswith("INSERT INTO a6_trade.provider_attempts"):
            if not self.database.envelope_exists:
                return
            key = (str(values[1]), int(values[2]))
            row = self.database.attempts.get(key)
            expected = {
                "attempt_id": str(values[0]),
                "request_id": str(values[1]),
                "attempt_no": int(values[2]),
                "model": str(values[3]),
                "routing_generation": int(values[4]),
                "cost_generation": int(values[5]),
                "slot_no": int(values[6]),
                "enodot_account_id": int(values[7]),
                "a6_token_id": int(values[8]),
                "channel_id": int(values[9]),
                "endpoint_host": str(values[10]),
                "egress_id": str(values[11]),
                "state": "failed",
                "http_status": values[12],
                "failure_owner": str(values[13]),
                "failure_code": str(values[14]),
                "failure_owner_declared": str(values[15]),
                "failure_classifier_version_declared": str(values[16]),
                "failure_classifier_version": str(values[17]),
                "failure_control_action": str(values[18]),
                "failure_quarantine_scope": values[19],
                "failure_streak_count": 0,
                "failure_streak_threshold": 0,
                "failure_streak_triggered": False,
                "failure_occurred_at": values[20],
                "terminal_event_seen": bool(values[21]),
                "response_committed": False,
                "transport_status": values[22],
                "finished_at": values[24],
                "pin_assignment_epoch_id": 1,
                "terminal_seq": len(self.database.attempts) + 1,
            }
            if row is None:
                row = {"id": len(self.database.attempts) + 1, **expected}
                self.database.attempts[key] = row
            else:
                if row.get("failure_streak_triggered"):
                    expected["failure_control_action"] = row["failure_control_action"]
                    expected["failure_quarantine_scope"] = row["failure_quarantine_scope"]
                expected["pin_assignment_epoch_id"] = row["pin_assignment_epoch_id"]
                expected["terminal_seq"] = row["terminal_seq"]
                for name in (
                    "failure_streak_count",
                    "failure_streak_threshold",
                    "failure_streak_triggered",
                ):
                    expected[name] = row.get(name)
                if any(
                    row.get(name) != value
                    for name, value in expected.items()
                    if name != "attempt_id"
                ):
                    return
            self.result = dict(row)
            self.rowcount = 1
        elif sql.startswith("SELECT state, failure_owner, failure_classifier_version"):
            matching = [
                row
                for row in self.database.attempts.values()
                if row.get("pin_assignment_epoch_id") == int(values[0])
                and row["state"] in {"failed", "won", "lost", "cancelled"}
                and row.get("terminal_seq") is not None
            ]
            matching.sort(key=lambda row: row["terminal_seq"], reverse=True)
            self.results = [dict(row) for row in matching[: int(values[-1])]]
        elif sql.startswith("UPDATE a6_trade.provider_attempts"):
            row = next(
                (item for item in self.database.attempts.values() if item["id"] == int(values[5])),
                None,
            )
            if row is not None and int(row.get("failure_streak_threshold") or 0) == 0:
                row["failure_control_action"] = str(values[0])
                row["failure_quarantine_scope"] = values[1]
                row["failure_streak_count"] = int(values[2])
                row["failure_streak_threshold"] = int(values[3])
                row["failure_streak_triggered"] = bool(values[4])
                self.result = dict(row)
                self.rowcount = 1
        elif sql.startswith("SELECT id, attempt_id, request_id"):
            attempt_id = int(values[0])
            self.result = next(
                (dict(row) for row in self.database.attempts.values() if row["id"] == attempt_id),
                None,
            )
        elif sql.startswith("INSERT INTO a6_trade.provider_fast_block_outbox"):
            key = str(values[0])
            row = self.database.projections.get(key)
            if row is None:
                row = {
                    "id": len(self.database.projections) + 1,
                    "idempotency_key": key,
                    "operation": "block",
                    "model": str(values[1]),
                    "routing_generation": int(values[2]),
                    "cost_generation": int(values[3]),
                    "slot_no": int(values[4]),
                    "channel_id": int(values[5]),
                    "attempt_id": str(values[6]),
                    "quarantine_id": None,
                    "projection_key": str(values[7]),
                    "payload": json.loads(values[8]),
                    "projection_state": "pending",
                    "attempt_count": 0,
                    "failure_occurred_at": values[9],
                    "recovery_verified_at": None,
                    "last_error_class": None,
                }
                self.database.projections[key] = row
                self.result = dict(row)
                self.rowcount = 1
        elif sql.startswith("SELECT id, idempotency_key, operation, model") and (
            "FROM a6_trade.provider_fast_block_outbox" in sql
        ):
            if "WHERE idempotency_key" in sql:
                row = self.database.projections.get(str(values[0]))
            else:
                row = next(
                    (
                        item
                        for item in self.database.projections.values()
                        if item["id"] == int(values[0])
                    ),
                    None,
                )
            self.result = dict(row) if row is not None else None
        elif sql.startswith("INSERT INTO a6_trade.provider_quarantines"):
            identity = (str(values[0]), str(values[1]), str(values[2]), values[3])
            row = self.database.quarantines.get(identity)
            if row is None:
                row = {
                    "id": len(self.database.quarantines) + 1,
                    "model": identity[0],
                    "scope_type": identity[1],
                    "scope_id": identity[2],
                    "region": identity[3],
                    "reason_code": str(values[4]),
                    "failure_count": 1,
                    "state": "active",
                    "next_probe_at": values[6],
                    "failure_owner": str(values[8]),
                    "classifier_version": str(values[9]),
                    "control_action": str(values[10]),
                    "routing_generation": int(values[11]),
                    "cost_generation": int(values[12]),
                    "slot_no": int(values[13]),
                    "channel_id": int(values[14]),
                    "endpoint_host": str(values[15]),
                    "egress_id": str(values[16]),
                    "first_failure_at": values[17],
                    "last_failure_at": values[18],
                }
                self.database.quarantines[identity] = row
            else:
                row["failure_count"] += 1
                row["reason_code"] = str(values[4])
                row["state"] = "active"
                row["last_failure_at"] = max(row["last_failure_at"], values[18])
            self.result = dict(row)
            self.rowcount = 1
        elif sql.startswith("UPDATE a6_trade.provider_fast_block_outbox SET quarantine_id"):
            quarantine_id = int(values[0])
            projection_id = int(values[1])
            row = next(
                (
                    item
                    for item in self.database.projections.values()
                    if item["id"] == projection_id
                ),
                None,
            )
            if row is not None and row["quarantine_id"] is None:
                row["quarantine_id"] = quarantine_id
                self.result = dict(row)
                self.rowcount = 1
        elif sql.startswith("SELECT id, model, scope_type, scope_id"):
            quarantine_id = int(values[0])
            row = next(
                (
                    item
                    for item in self.database.quarantines.values()
                    if item["id"] == quarantine_id
                ),
                None,
            )
            self.result = dict(row) if row is not None else None
        elif sql.startswith("UPDATE a6_trade.provider_pool_slots"):
            identity = (
                values[0],
                values[1],
                values[2],
                values[3],
                values[4],
                values[5],
                values[6],
            )
            if identity != self.database.slot_identity:
                return
            changed = (
                self.database.slot["desired_schedulable"]
                or self.database.slot["observed_schedulable"]
                or self.database.slot["slot_state"] not in {"DRAINING", "VACANT"}
            )
            self.database.slot["desired_schedulable"] = False
            self.database.slot["observed_schedulable"] = False
            self.database.slot["slot_state"] = "DRAINING"
            self.database.slot["state_version"] += int(changed)
            self.result = dict(self.database.slot)
            self.rowcount = 1
        elif sql.startswith("INSERT INTO a6_trade.model_publication_reconcile_requests"):
            key = str(values[0])
            row = self.database.requests.get(key)
            if row is None:
                row = {
                    "id": len(self.database.requests) + 1,
                    "idempotency_key": key,
                    "model": str(values[1]),
                    "expected_generation": int(values[2]),
                    "expected_cost_generation": int(values[3]),
                    "request_source": str(values[4]),
                    "requested_by": str(values[5]),
                    "trace_id": str(values[6]),
                    "request_payload": json.loads(values[7]),
                    "status": "pending",
                    "attempt_count": 0,
                }
                self.database.requests[key] = row
                self.result = dict(row)
                self.rowcount = 1
        elif sql.startswith("SELECT id, idempotency_key, model"):
            if "WHERE idempotency_key" in sql:
                row = self.database.requests.get(str(values[0]))
            else:
                row = next(
                    (
                        item
                        for item in self.database.requests.values()
                        if item["id"] == int(values[0])
                    ),
                    None,
                )
            self.result = dict(row) if row is not None else None
        elif sql.startswith("UPDATE a6_trade.model_publication_reconcile_requests receipt"):
            result = json.loads(values[0])
            request_id = int(values[1])
            row = next(
                (item for item in self.database.requests.values() if item["id"] == request_id),
                None,
            )
            if (
                row is not None
                and row["status"] == "pending"
                and row["attempt_count"] == 0
                and row.get("result") is None
            ):
                row["status"] = "completed"
                row["result"] = result
                self.result = dict(row)
                self.rowcount = 1
        elif sql.startswith("WITH candidate AS"):
            if "provider_fast_block_outbox" in sql:
                after_id = int(values[0])
                event_id = int(values[1]) if values[1] is not None else None
                candidates = [
                    row
                    for row in self.database.projections.values()
                    if row["id"] > after_id
                    and (event_id is None or row["id"] == event_id)
                    and row["projection_state"] in {"pending", "failed", "running"}
                ]
                if candidates:
                    row = sorted(candidates, key=lambda item: item["id"])[0]
                    row["projection_state"] = "running"
                    row["attempt_count"] += 1
                    self.result = dict(row)
                    self.rowcount = 1
            else:
                candidates = [
                    row
                    for row in self.database.requests.values()
                    if row["model"] == values[0] and row["status"] != "completed"
                ]
                if candidates:
                    row = sorted(candidates, key=lambda item: item["id"])[0]
                    row["status"] = "running"
                    row["attempt_count"] += 1
                    self.result = dict(row)
                    self.rowcount = 1
        elif sql.startswith("UPDATE a6_trade.provider_fast_block_outbox SET projection_state"):
            state = str(values[0])
            event_id = int(values[3])
            attempt_count = int(values[4])
            row = next(
                (item for item in self.database.projections.values() if item["id"] == event_id),
                None,
            )
            if (
                row is not None
                and row["projection_state"] == "running"
                and row["attempt_count"] == attempt_count
            ):
                row["projection_state"] = state
                row["last_error_class"] = values[2]
                self.result = dict(row)
                self.rowcount = 1
        elif sql.startswith("UPDATE a6_trade.model_publication_reconcile_requests SET status"):
            request_id = int(values[3])
            row = next(
                (item for item in self.database.requests.values() if item["id"] == request_id),
                None,
            )
            if (
                row is not None
                and row["status"] == "running"
                and row["attempt_count"] == int(values[4])
            ):
                row["status"] = str(values[0])
                row["result"] = json.loads(values[1]) if values[1] else None
                row["last_error_class"] = values[2]
                self.result = {"id": row["id"], "status": row["status"]}
                self.rowcount = 1
        elif sql.startswith("SELECT model, desired_state, observed_state"):
            self.results = list(self.database.publications)
        elif sql.startswith("SELECT model, status, count(*)"):
            counts: dict[tuple[str, str], int] = {}
            for row in self.database.requests.values():
                key = (row["model"], row["status"])
                counts[key] = counts.get(key, 0) + 1
            self.results = [
                {"model": model, "status": status, "request_count": count}
                for (model, status), count in sorted(counts.items())
            ]
        elif sql.startswith("SELECT model, failure_owner"):
            counts: dict[tuple[str, str, str], int] = {}
            for row in self.database.attempts.values():
                key = (
                    str(row["model"]),
                    str(row["failure_owner"]),
                    str(row["failure_control_action"]),
                )
                counts[key] = counts.get(key, 0) + 1
            self.results = [
                {
                    "model": model,
                    "failure_owner": owner,
                    "control_action": action,
                    "failure_count": count,
                }
                for (model, owner, action), count in sorted(counts.items())
            ]
        elif sql.startswith("SELECT model, scope_type, state"):
            counts: dict[tuple[str, str, str], int] = {}
            for row in self.database.quarantines.values():
                key = (str(row["model"]), str(row["scope_type"]), str(row["state"]))
                counts[key] = counts.get(key, 0) + 1
            self.results = [
                {
                    "model": model,
                    "scope_type": scope,
                    "state": state,
                    "quarantine_count": count,
                }
                for (model, scope, state), count in sorted(counts.items())
            ]
        elif sql.startswith("SELECT model, projection_state"):
            counts: dict[tuple[str, str], int] = {}
            for row in self.database.projections.values():
                key = (str(row["model"]), str(row["projection_state"]))
                counts[key] = counts.get(key, 0) + 1
            self.results = [
                {
                    "model": model,
                    "projection_state": state,
                    "projection_count": count,
                    "oldest_age_seconds": 1.25,
                }
                for (model, state), count in sorted(counts.items())
            ]

    def fetchone(self) -> dict[str, Any] | None:
        return self.result

    def fetchall(self) -> list[dict[str, Any]]:
        return self.results


class Connection:
    def __init__(self, database: Database) -> None:
        self.database = database

    def cursor(self) -> Cursor:
        return Cursor(self.database)

    def commit(self) -> None:
        self.database.commits += 1
        self.database.events.append("commit")

    def rollback(self) -> None:
        self.database.rollbacks += 1

    def close(self) -> None:
        self.database.closes += 1


class Database:
    def __init__(self) -> None:
        self.envelope_exists = True
        self.attempts: dict[tuple[str, int], dict[str, Any]] = {}
        self.projections: dict[str, dict[str, Any]] = {}
        self.quarantines: dict[tuple[str, str, str, object], dict[str, Any]] = {}
        self.requests: dict[str, dict[str, Any]] = {}
        self.slot_identity = (MODEL, 42, 19, 1, 7001, 8001, 1008)
        self.slot = {
            "id": 4,
            "slot_state": "READY",
            "desired_schedulable": True,
            "observed_schedulable": True,
            "state_version": 7,
            "pin_assignment_epoch_id": 1,
        }
        self.publications = [
            {
                "model": MODEL,
                "desired_state": "unpublished",
                "observed_state": "published",
                "healthy_slots": 0,
                "converged": False,
                "desired_age_seconds": 3.5,
                "transition_seconds": None,
            }
        ]
        self.queries: list[tuple[str, tuple[Any, ...]]] = []
        self.events: list[str] = []
        self.commits = 0
        self.rollbacks = 0
        self.closes = 0

    def connect(self) -> Connection:
        return Connection(self)


class Trigger:
    def __init__(self, database: Database) -> None:
        self.database = database
        self.calls: list[tuple[str, int]] = []

    def wake(self, *, model: str, request_id: int) -> None:
        assert self.database.events[-1] == "commit"
        self.database.events.append("wake")
        self.calls.append((model, request_id))


class FailOnceProjector:
    def __init__(self) -> None:
        self.calls = 0

    def apply(self, projection: Any) -> ProviderFastBlockProjectionResult:
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("redis fixture outage")
        return ProviderFastBlockProjectionResult(
            event_id=projection.event_id,
            projection_key=projection.projection_key,
            state="projected",
            changed=True,
        )


def test_failure_parser_requires_direct_endpoint_and_matching_generation() -> None:
    event = ProviderAttemptFailureEvent.parse(failure_document())
    assert event.drains_slot is False
    assert event.failure_owner == "provider"
    assert event.control_action == "none"
    assert event.quarantine_scope == "channel"
    assert event.occurred_at == datetime(2026, 7, 29, 2, 3, 4, tzinfo=UTC)

    request_classifier = ProviderAttemptFailureEvent.parse(
        failure_document(classifier_version="a6-provider-request-classifier-v1")
    )
    assert request_classifier.classifier_version == "failure-classifier-v3"
    assert request_classifier.classifier_version_declared == "a6-provider-request-classifier-v1"

    with pytest.raises(PublicationControlError, match="provider_failure_endpoint_host_invalid"):
        ProviderAttemptFailureEvent.parse(failure_document(endpoint_host="api.a6api.com"))
    with pytest.raises(
        PublicationControlError,
        match="provider_failure_expected_generation_mismatch",
    ):
        ProviderAttemptFailureEvent.parse(failure_document(expected_generation=41))
    with pytest.raises(PublicationControlError, match="provider_failure_fields_invalid"):
        ProviderAttemptFailureEvent.parse(
            failure_document(raw_error_body="SECRET_MUST_NOT_BE_ACCEPTED")
        )
    with pytest.raises(PublicationControlError, match="provider_failure_owner_mismatch"):
        ProviderAttemptFailureEvent.parse(failure_document(failure_owner="user"))
    with pytest.raises(PublicationControlError, match="provider_failure_classifier_unsupported"):
        ProviderAttemptFailureEvent.parse(failure_document(classifier_version="unknown-v9"))


def test_atomic_trigger_replaces_private_marker(tmp_path: Path) -> None:
    target = tmp_path / "runtime" / "publication-request"
    trigger = AtomicPublicationTrigger(target)

    trigger.wake(model=MODEL, request_id=7)
    first = target.read_text(encoding="utf-8")
    trigger.wake(model=MODEL, request_id=8)

    assert json.loads(first) == {"model": MODEL, "request_id": 7}
    assert json.loads(target.read_text(encoding="utf-8"))["request_id"] == 8
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert list(target.parent.glob(".*.tmp")) == []


def test_provider_failure_commits_attempt_drain_and_request_before_wake() -> None:
    database = Database()
    trigger = Trigger(database)
    control = PostgresPublicationControl(database.connect, trigger=trigger)  # type: ignore[arg-type]

    first = control.report_attempt_failure(numbered_failure(1))
    second = control.report_attempt_failure(numbered_failure(2))
    result = control.report_attempt_failure(numbered_failure(3))

    assert first["slot_drained"] is False
    assert first["channel_failure_streak_count"] == 1
    assert second["slot_drained"] is False
    assert second["channel_failure_streak_count"] == 2
    assert result["accepted"] is True
    assert result["slot_drained"] is True
    assert result["publication_requested"] is True
    assert result["wake_scheduled"] is True
    assert result["failure_owner"] == "provider"
    assert result["control_action"] == "repin_immediately"
    assert result["channel_failure_streak_count"] == 3
    assert result["channel_failure_streak_threshold"] == 3
    assert result["channel_failure_threshold_reached"] is True
    assert result["quarantine"]["scope_type"] == "channel"
    assert result["quarantine"]["failure_count"] == 1
    assert result["fast_block_projection"]["projection_state"] == "pending"
    assert database.slot == {
        "id": 4,
        "slot_state": "DRAINING",
        "desired_schedulable": False,
        "observed_schedulable": False,
        "state_version": 8,
        "pin_assignment_epoch_id": 1,
    }
    assert database.events == ["commit", "commit", "commit", "wake"]
    assert trigger.calls == [(MODEL, 3)]
    assert database.rollbacks == 0
    rendered = "\n".join(query for query, _ in database.queries)
    assert "NOT a6_trade.provider_attempts.response_committed" in rendered
    assert "provider_quarantine_recoveries recovery" in rendered
    assert "recovery.probe_token = a6_trade.provider_attempts.request_id" in rendered
    assert "slot.valid_to IS NULL" in rendered
    assert "publication_request_strict_readback_mismatch" not in rendered


def test_failure_replay_does_not_increment_quarantine_or_duplicate_outbox() -> None:
    database = Database()
    control = PostgresPublicationControl(  # type: ignore[arg-type]
        database.connect,
        provider_channel_failure_threshold=1,
    )

    first = control.report_attempt_failure(failure_document())
    replay = control.report_attempt_failure(failure_document())

    assert first["quarantine"]["failure_count"] == 1
    assert replay["quarantine"]["failure_count"] == 1
    assert len(database.quarantines) == 1
    assert len(database.projections) == 1


def test_unknown_transport_failure_does_not_count_or_switch_pin() -> None:
    database = Database()
    control = PostgresPublicationControl(database.connect)  # type: ignore[arg-type]

    result = control.report_attempt_failure(
        failure_document(
            failure_owner="unknown",
            failure_code="upstream_failover",
            http_status=None,
            transport_status=None,
        )
    )

    assert result["failure_owner"] == "unknown"
    assert result["control_action"] == "open_incident"
    assert result["channel_failure_streak_count"] == 0
    assert result["slot_drained"] is False
    assert result["fast_block_projection"] is None
    assert result["quarantine"] is None


def test_failed_fast_block_projection_remains_durable_and_replays() -> None:
    database = Database()
    projector = FailOnceProjector()
    control = PostgresPublicationControl(
        database.connect,  # type: ignore[arg-type]
        fast_block_projector=projector,
        provider_channel_failure_threshold=1,
    )

    reported = control.report_attempt_failure(failure_document())
    replayed = control.project_pending_fast_blocks()

    assert reported["fast_block_projection"]["projection_state"] == "failed"
    assert reported["fast_block_projection"]["attempt_count"] == 1
    assert replayed["processed"] == 1
    assert replayed["projected"] == 1
    projection = next(iter(database.projections.values()))
    assert projection["projection_state"] == "projected"
    assert projection["attempt_count"] == 2


def test_pending_fast_block_projection_stops_after_first_failure_when_requested() -> None:
    database = Database()
    control = PostgresPublicationControl(  # type: ignore[arg-type]
        database.connect,
        provider_channel_failure_threshold=1,
    )
    control.report_attempt_failure(failure_document())
    first = next(iter(database.projections.values()))
    second = {
        **first,
        "id": 2,
        "idempotency_key": "req-42-attempt-2-failure:fast-block",
        "attempt_id": "attempt-42-2",
        "projection_state": "pending",
        "attempt_count": 0,
        "last_error_class": None,
    }
    database.projections[second["idempotency_key"]] = second
    projector = FailOnceProjector()
    control.fast_block_projector = projector

    with pytest.raises(
        PublicationControlError,
        match="provider_fast_block_projection_stop_on_failure_invalid",
    ):
        control.project_pending_fast_blocks(stop_on_failure=1)  # type: ignore[arg-type]

    failed = control.project_pending_fast_blocks(limit=500, stop_on_failure=True)

    assert failed["processed"] == 1
    assert failed["failed"] == 1
    assert projector.calls == 1
    assert first["projection_state"] == "failed"
    assert first["attempt_count"] == 1
    assert second["projection_state"] == "pending"
    assert second["attempt_count"] == 0

    replayed = control.project_pending_fast_blocks(limit=500, stop_on_failure=True)

    assert replayed["processed"] == 2
    assert replayed["projected"] == 2
    assert projector.calls == 3
    assert first["projection_state"] == "projected"
    assert first["attempt_count"] == 2
    assert second["projection_state"] == "projected"
    assert second["attempt_count"] == 1


def test_pending_fast_block_projection_keeps_batching_after_failure_by_default() -> None:
    database = Database()
    control = PostgresPublicationControl(  # type: ignore[arg-type]
        database.connect,
        provider_channel_failure_threshold=1,
    )
    control.report_attempt_failure(failure_document())
    first = next(iter(database.projections.values()))
    second = {
        **first,
        "id": 2,
        "idempotency_key": "req-42-attempt-2-failure:fast-block",
        "attempt_id": "attempt-42-2",
        "projection_state": "pending",
        "attempt_count": 0,
        "last_error_class": None,
    }
    database.projections[second["idempotency_key"]] = second
    projector = FailOnceProjector()
    control.fast_block_projector = projector

    projected = control.project_pending_fast_blocks(limit=500)

    assert projected["processed"] == 2
    assert projected["failed"] == 1
    assert projected["projected"] == 1
    assert projector.calls == 2
    assert first["projection_state"] == "failed"
    assert second["projection_state"] == "projected"


def test_authenticated_failure_api_drives_durable_publication_e2e() -> None:
    database = Database()
    trigger = Trigger(database)
    control = PostgresPublicationControl(  # type: ignore[arg-type]
        database.connect,
        trigger=trigger,
        provider_channel_failure_threshold=1,
    )
    token = "fixture-internal-token-32-characters"
    app = create_dashboard_app(
        object(),  # type: ignore[arg-type]
        publication_control=control,
        internal_api_token=token,
    )
    raw = json.dumps(failure_document(), separators=(",", ":")).encode()
    captured: dict[str, Any] = {}

    def start_response(status: str, headers: list[tuple[str, str]]) -> None:
        captured["status"] = status
        captured["headers"] = dict(headers)

    body = b"".join(
        app(
            {
                "REQUEST_METHOD": "POST",
                "PATH_INFO": "/internal/provider-attempt-failures",
                "CONTENT_TYPE": "application/json",
                "CONTENT_LENGTH": str(len(raw)),
                "HTTP_AUTHORIZATION": f"Bearer {token}",
                "wsgi.input": BytesIO(raw),
            },
            start_response,
        )
    )

    payload = json.loads(body)
    assert captured["status"] == "202 Accepted"
    assert captured["headers"]["Cache-Control"] == "no-store, private"
    assert payload["slot_drained"] is True
    assert payload["publication_request"]["status"] == "pending"
    assert database.events == ["commit", "wake"]


def test_non_provider_failure_has_completed_durable_receipt_without_publication() -> None:
    database = Database()
    trigger = Trigger(database)
    control = PostgresPublicationControl(database.connect, trigger=trigger)  # type: ignore[arg-type]

    document = failure_document(
        failure_owner="internal",
        failure_code="provider_key_unauthorized",
        http_status=401,
    )
    result = control.report_attempt_failure(document)
    replay = control.report_attempt_failure(document)

    assert result["slot_drained"] is False
    assert result["publication_requested"] is False
    assert result["failure_receipt"]["status"] == "completed"
    assert result["failure_receipt"]["deduplicated"] is False
    assert replay["failure_receipt"]["deduplicated"] is True
    assert database.slot["slot_state"] == "READY"
    assert len(database.requests) == 1
    receipt = next(iter(database.requests.values()))
    assert receipt["status"] == "completed"
    assert receipt["result"] == {
        "publication_requested": False,
        "slot_drained": False,
    }
    assert control.claim_next(MODEL) is None
    assert trigger.calls == []


def test_non_provider_failure_rejects_same_idempotency_key_with_payload_drift() -> None:
    database = Database()
    control = PostgresPublicationControl(database.connect)  # type: ignore[arg-type]
    document = failure_document(
        failure_owner="client_transport",
        failure_code="client_disconnected",
        http_status=None,
        transport_status=499,
    )

    control.report_attempt_failure(document)

    with pytest.raises(
        PublicationControlError,
        match="provider_failure_attempt_conflict_or_envelope_missing",
    ):
        control.report_attempt_failure(
            {
                **document,
                "classifier_version": "a6-provider-request-classifier-v1",
            }
        )


def test_manual_reconcile_is_idempotent_and_rejects_payload_drift() -> None:
    database = Database()
    trigger = Trigger(database)
    control = PostgresPublicationControl(database.connect, trigger=trigger)  # type: ignore[arg-type]
    document = {
        "idempotency_key": "manual-42",
        "expected_generation": 42,
        "requested_by": "operator",
        "trace_id": "trace-manual-42",
        "reason_code": "slo_drill",
    }

    first = control.request_reconcile(MODEL, document)
    replay = control.request_reconcile(MODEL, document)

    assert first["deduplicated"] is False
    assert replay["deduplicated"] is True
    assert first["id"] == replay["id"]
    assert trigger.calls == [(MODEL, 1), (MODEL, 1)]
    with pytest.raises(PublicationControlError, match="publication_idempotency_conflict"):
        control.request_reconcile(MODEL, {**document, "reason_code": "different"})


def test_durable_request_can_be_reclaimed_and_completed_after_interruption() -> None:
    database = Database()
    control = PostgresPublicationControl(database.connect, running_reclaim_seconds=1)
    control.request_reconcile(
        MODEL,
        {
            "idempotency_key": "restart-42",
            "expected_generation": 42,
            "requested_by": "sub2api-us",
            "trace_id": "trace-restart",
        },
    )
    abandoned = control.claim_next(MODEL)
    recovered = control.claim_next(MODEL)
    assert abandoned is not None
    assert recovered is not None
    assert recovered.request_id == abandoned.request_id
    assert recovered.attempt_count == 2

    control.complete(recovered, {"observed_state": "unpublished"})
    assert control.claim_next(MODEL) is None
    request = next(iter(database.requests.values()))
    assert request["status"] == "completed"
    assert request["result"] == {"observed_state": "unpublished"}
    rendered = "\n".join(query for query, _ in database.queries)
    assert "FOR UPDATE SKIP LOCKED" in rendered
    assert "interval '1 second'" in rendered


def test_metrics_expose_state_transition_mismatch_and_queue_depth() -> None:
    database = Database()
    control = PostgresPublicationControl(
        database.connect,
        provider_channel_failure_threshold=1,
    )
    control.request_reconcile(
        MODEL,
        {
            "idempotency_key": "metric-42",
            "expected_generation": 42,
            "requested_by": "operator",
            "trace_id": "trace-metric",
        },
    )
    control.report_attempt_failure(failure_document())

    metrics = control.render_metrics()

    assert f'a6_model_publication_state{{model="{MODEL}",state="unpublished"}} 1' in metrics
    assert (
        f'a6_model_publication_transition_seconds{{model="{MODEL}",'
        'to_state="unpublished",outcome="pending"} 3.500000'
    ) in metrics
    mismatch_metric = (
        f'a6_model_publication_desired_observed_mismatch_seconds{{model="{MODEL}"}} 3.500000'
    )
    assert mismatch_metric in metrics
    assert f'a6_provider_pool_healthy_slots{{model="{MODEL}"}} 0' in metrics
    assert (
        f'a6_model_publication_reconcile_requests{{model="{MODEL}",status="pending"}} 2' in metrics
    )
    assert (
        f'a6_provider_failure_events{{model="{MODEL}",owner="provider",'
        'action="repin_immediately"} 1'
    ) in metrics
    assert (
        f'a6_provider_quarantines{{model="{MODEL}",scope_type="channel",state="active"}} 1'
        in metrics
    )
    assert (
        f'a6_provider_fast_block_projection_events{{model="{MODEL}",state="pending"}} 1' in metrics
    )


def test_publication_reconcile_request_migration_is_packaged_and_discovered() -> None:
    source = Path("migrations/0036_model_publication_reconcile_requests.sql")
    packaged = Path("src/a6api_auto/data/migrations/0036_model_publication_reconcile_requests.sql")
    migration = source.read_text(encoding="utf-8")

    assert packaged.read_text(encoding="utf-8") == migration
    assert "idempotency_key text NOT NULL UNIQUE" in migration
    assert "status IN ('pending', 'running', 'completed', 'failed')" in migration
    assert dict(_migration_files(Path("migrations")))["0036"] == source


def test_provider_failure_control_migration_is_packaged_and_discovered() -> None:
    source = Path("migrations/0037_provider_failure_control.sql")
    packaged = Path("src/a6api_auto/data/migrations/0037_provider_failure_control.sql")
    migration = source.read_text(encoding="utf-8")

    assert packaged.read_text(encoding="utf-8") == migration
    assert "provider_fast_block_outbox" in migration
    assert "state IN ('active', 'probing', 'closed')" in migration
    assert "provider_quarantine_recoveries" in migration
    assert "observer_quorum_ref ~ '^observer-quorum:v1:" in migration
    assert "GRANT SELECT ON public.a6_provider_request_outcomes" in migration
    assert dict(_migration_files(Path("migrations")))["0037"] == source


def test_recovery_rejects_client_supplied_evidence_references_before_database_access() -> None:
    database = Database()
    control = PostgresPublicationControl(database.connect)  # type: ignore[arg-type]

    with pytest.raises(
        PublicationControlError,
        match="provider_quarantine_recovery_fields_invalid",
    ):
        control.confirm_quarantine_recovery(
            MODEL,
            {
                "idempotency_key": "recover-42",
                "expected_generation": 42,
                "requested_by": "reconciler",
                "trace_id": "trace-recover-42",
                "quarantine_id": 1,
                "probe_token": "probe-token-42",
                "observer_quorum_ref": "caller-controlled",
            },
        )

    assert database.queries == []
