#!/usr/bin/env python3
"""Audit strict A-to-B replay manifests against the durable Sub2API ledger."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import shlex
import sys
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

MANIFEST_COLUMNS = (
    "sample",
    "attempt",
    "request_id",
    "routes",
    "body_hash_equal",
    "dispatch_ms",
    "http_status",
    "total_seconds",
    "response_bytes",
    "winner_count",
    "a_leak_count",
    "accepted",
    "reason",
)

READ_ONLY_TRANSACTION_SQL = "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"

OUTCOME_QUERY = """
SELECT
    outcome.request_id,
    outcome.next_attempt_no,
    outcome.stream,
    outcome.logical_status,
    outcome.logical_outcome,
    outcome.winning_attempt_id,
    outcome.response_committed,
    outcome.delivery_state,
    outcome.committed_at,
    outcome.completed_at,
    outcome.attribution_enabled,
    outcome.routing_generation,
    outcome.cost_generation,
    outcome.prepared_attempts_enabled,
    NULLIF(btrim(outcome.dispatch_owner_id), '') IS NOT NULL AS dispatch_owner_present,
    outcome.dispatch_owner_claimed_at,
    outcome.dispatch_owner_heartbeat_at,
    outcome.dispatch_owner_heartbeat_at >= outcome.dispatch_owner_claimed_at
        AS dispatch_owner_time_valid,
    outcome.billing_disposition,
    outcome.billed_micros,
    outcome.billing_recorded_at,
    outcome.candidate_snapshot_enabled,
    outcome.candidate_set_hash,
    outcome.candidate_count,
    outcome.candidate_assignments,
    outcome.billing_class,
    outcome.transport_status,
    (
        SELECT COUNT(*)
        FROM usage_logs AS usage
        WHERE usage.request_id = outcome.request_id
          AND usage.api_key_id = outcome.api_key_id
    ) AS billing_fact_count,
    (
        SELECT COUNT(*)
        FROM usage_logs AS usage
        JOIN a6_provider_request_attempts AS winner
          ON winner.request_id = outcome.request_id
         AND winner.attempt_id = outcome.winning_attempt_id
        WHERE usage.request_id = outcome.request_id
          AND usage.api_key_id = outcome.api_key_id
          AND usage.account_id = winner.account_id
    ) AS winner_billing_fact_count
FROM a6_provider_request_outcomes AS outcome
WHERE outcome.request_id = ANY(%s::text[])
ORDER BY outcome.request_id
"""

ATTEMPT_QUERY = """
SELECT
    attempt.request_id,
    attempt.attempt_id,
    attempt.attempt_no,
    attempt.account_id,
    attempt.attempt_state,
    attempt.upstream_http_status,
    attempt.upstream_request_id IS NULL AS upstream_request_id_absent,
    attempt.failure_owner,
    attempt.failure_code,
    attempt.retry_decision,
    attempt.terminal_event_seen,
    attempt.response_committed,
    attempt.delivery_state,
    attempt.buffered_bytes,
    attempt.spilled,
    attempt.started_at,
    attempt.prepared_at,
    attempt.finished_at,
    attempt.routing_generation,
    attempt.cost_generation,
    attempt.provider_slot_no,
    attempt.channel_id,
    attempt.a6_token_id,
    attempt.a6_token_fingerprint,
    attempt.planned_cost_micros,
    attempt.realized_cost_micros,
    attempt.cost_recorded_at,
    attempt.assignment_fingerprint,
    attempt.cost_observation_state,
    attempt.observed_input_tokens,
    attempt.observed_output_tokens,
    attempt.observed_cache_creation_tokens,
    attempt.observed_cache_read_tokens
FROM a6_provider_request_attempts AS attempt
WHERE attempt.request_id = ANY(%s::text[])
ORDER BY attempt.request_id, attempt.attempt_no
"""

_HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_MAX_AUDIT_REQUESTS = 10_000
_MAX_MANIFEST_ROWS = 100_000
_TERMINAL_ATTEMPT_STATES = {
    "failed",
    "won",
    "superseded_success",
    "abandoned",
}
_TERMINAL_COST_STATES = {"usage_observed", "valued", "unavailable"}
_DATABASE_ENV_FIELDS = (
    ("DATABASE_HOST", "host"),
    ("DATABASE_PORT", "port"),
    ("DATABASE_USER", "user"),
    ("DATABASE_PASSWORD", "password"),
    ("DATABASE_DBNAME", "dbname"),
    ("DATABASE_SSLMODE", "sslmode"),
)
_MAX_DATABASE_ENV_FILE_BYTES = 64 * 1024


class ReplayLedgerAuditError(RuntimeError):
    def __init__(self, code: str, *, line: int | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.line = line


@dataclass(frozen=True)
class ManifestSample:
    sample: int
    attempt: int
    request_id: str
    dispatch_ms: Decimal


@dataclass(frozen=True)
class ManifestAudit:
    total_rows: int
    expected_samples: int
    accepted: tuple[ManifestSample, ...]
    sha256: str
    dispatch_p50_ms: Decimal
    dispatch_p95_ms: Decimal
    required_p95_ms: Decimal | None = None

    @property
    def request_ids(self) -> tuple[str, ...]:
        return tuple(sample.request_id for sample in self.accepted)


@dataclass(frozen=True)
class CandidateAssignment:
    account_id: int
    priority: int
    routing_generation: int
    cost_generation: int
    provider_slot_no: int
    channel_id: int
    a6_token_id: int
    token_fingerprint: str
    planned_cost_micros: int | None
    assignment_fingerprint: str

    def assignment_payload(self) -> dict[str, Any]:
        payload = {
            "account_id": self.account_id,
            "routing_generation": self.routing_generation,
            "cost_generation": self.cost_generation,
            "provider_slot_no": self.provider_slot_no,
            "channel_id": self.channel_id,
            "a6_token_id": self.a6_token_id,
            "token_fingerprint": self.token_fingerprint,
        }
        if self.planned_cost_micros is not None:
            payload["planned_cost_micros"] = self.planned_cost_micros
        return payload

    def candidate_payload(self) -> dict[str, Any]:
        payload = {
            "account_id": self.account_id,
            "priority": self.priority,
            "routing_generation": self.routing_generation,
            "cost_generation": self.cost_generation,
            "provider_slot_no": self.provider_slot_no,
            "channel_id": self.channel_id,
            "a6_token_id": self.a6_token_id,
            "token_fingerprint": self.token_fingerprint,
        }
        if self.planned_cost_micros is not None:
            payload["planned_cost_micros"] = self.planned_cost_micros
        payload["assignment_fingerprint"] = self.assignment_fingerprint
        return payload


def parse_manifest(
    lines: Iterable[str],
    *,
    expected_samples: int,
    expected_sha256: str | None = None,
    require_p95_ms: Decimal | None = None,
) -> ManifestAudit:
    if expected_samples < 1 or expected_samples > _MAX_AUDIT_REQUESTS:
        raise ReplayLedgerAuditError("manifest_expected_samples_invalid")
    if expected_sha256 is not None and not _valid_hash(expected_sha256):
        raise ReplayLedgerAuditError("manifest_expected_sha256_invalid")
    if require_p95_ms is not None and (
        not isinstance(require_p95_ms, Decimal)
        or not require_p95_ms.is_finite()
        or require_p95_ms <= 0
    ):
        raise ReplayLedgerAuditError("manifest_dispatch_p95_threshold_invalid")

    digest = hashlib.sha256()

    def hashing_lines() -> Iterable[str]:
        for line in lines:
            try:
                encoded = line.encode("ascii", "strict")
            except (AttributeError, UnicodeEncodeError):
                raise ReplayLedgerAuditError("manifest_encoding_invalid") from None
            digest.update(encoded)
            yield line

    reader = csv.reader(hashing_lines(), delimiter="\t", strict=True)
    try:
        header = next(reader)
    except StopIteration:
        raise ReplayLedgerAuditError("manifest_empty") from None
    except csv.Error:
        raise ReplayLedgerAuditError("manifest_tsv_invalid", line=1) from None
    if tuple(header) != MANIFEST_COLUMNS:
        raise ReplayLedgerAuditError("manifest_header_invalid", line=1)

    accepted: list[ManifestSample] = []
    seen_request_ids: set[str] = set()
    total_rows = 0
    try:
        for line_no, values in enumerate(reader, start=2):
            total_rows += 1
            if total_rows > _MAX_MANIFEST_ROWS:
                raise ReplayLedgerAuditError("manifest_row_limit_exceeded", line=line_no)
            if len(values) != len(MANIFEST_COLUMNS):
                raise ReplayLedgerAuditError("manifest_row_width_invalid", line=line_no)
            row = dict(zip(MANIFEST_COLUMNS, values, strict=True))
            attempt = _positive_manifest_integer(row["attempt"], line_no)
            if attempt != total_rows:
                raise ReplayLedgerAuditError("manifest_attempt_sequence_invalid", line=line_no)
            request_id = row["request_id"].strip()
            if not request_id or request_id in seen_request_ids:
                raise ReplayLedgerAuditError("manifest_request_id_invalid", line=line_no)
            seen_request_ids.add(request_id)
            if row["accepted"] not in {"true", "false"}:
                raise ReplayLedgerAuditError("manifest_accepted_flag_invalid", line=line_no)
            if row["body_hash_equal"] not in {"true", "false"}:
                raise ReplayLedgerAuditError("manifest_body_hash_flag_invalid", line=line_no)
            is_accepted = row["accepted"] == "true"
            if is_accepted != (row["reason"] == "accepted"):
                raise ReplayLedgerAuditError("manifest_acceptance_reason_invalid", line=line_no)
            if not is_accepted:
                if row["sample"]:
                    raise ReplayLedgerAuditError("manifest_rejected_sample_invalid", line=line_no)
                continue

            sample = _positive_manifest_integer(row["sample"], line_no)
            if row["routes"] != "a,b":
                raise ReplayLedgerAuditError("manifest_routes_not_strict_a_b", line=line_no)
            if row["body_hash_equal"] != "true":
                raise ReplayLedgerAuditError("manifest_body_hash_mismatch", line=line_no)
            if _manifest_integer(row["http_status"], line_no) != 200:
                raise ReplayLedgerAuditError("manifest_http_status_invalid", line=line_no)
            if _manifest_integer(row["winner_count"], line_no) != 1:
                raise ReplayLedgerAuditError("manifest_winner_count_invalid", line=line_no)
            if _manifest_integer(row["a_leak_count"], line_no) != 0:
                raise ReplayLedgerAuditError("manifest_a_content_leak", line=line_no)
            dispatch_ms = _manifest_decimal(row["dispatch_ms"], line_no)
            if dispatch_ms < 0:
                raise ReplayLedgerAuditError("manifest_dispatch_time_invalid", line=line_no)
            if _manifest_decimal(row["total_seconds"], line_no) < 0:
                raise ReplayLedgerAuditError("manifest_total_time_invalid", line=line_no)
            if _manifest_integer(row["response_bytes"], line_no) < 0:
                raise ReplayLedgerAuditError("manifest_response_size_invalid", line=line_no)
            accepted.append(ManifestSample(sample, attempt, request_id, dispatch_ms))
    except csv.Error:
        raise ReplayLedgerAuditError("manifest_tsv_invalid", line=reader.line_num) from None

    if [sample.sample for sample in accepted] != list(range(1, len(accepted) + 1)):
        raise ReplayLedgerAuditError("manifest_sample_sequence_invalid")
    if len(accepted) != expected_samples:
        raise ReplayLedgerAuditError("manifest_accepted_sample_count_mismatch")
    manifest_sha256 = digest.hexdigest()
    if expected_sha256 is not None and manifest_sha256 != expected_sha256:
        raise ReplayLedgerAuditError("manifest_sha256_mismatch")
    dispatch_values = sorted(sample.dispatch_ms for sample in accepted)
    return ManifestAudit(
        total_rows,
        expected_samples,
        tuple(accepted),
        manifest_sha256,
        _nearest_rank(dispatch_values, numerator=50, denominator=100),
        _nearest_rank(dispatch_values, numerator=95, denominator=100),
        require_p95_ms,
    )


def _nearest_rank(
    ordered_values: Sequence[Decimal],
    *,
    numerator: int,
    denominator: int,
) -> Decimal:
    if not ordered_values or numerator < 1 or denominator < numerator:
        raise ReplayLedgerAuditError("manifest_percentile_input_invalid")
    index = max(0, (numerator * len(ordered_values) + denominator - 1) // denominator - 1)
    return ordered_values[index]


def _manifest_integer(value: str, line_no: int) -> int:
    if not value or value.strip() != value or not re.fullmatch(r"0|[1-9][0-9]*", value):
        raise ReplayLedgerAuditError("manifest_integer_invalid", line=line_no)
    return int(value)


def _positive_manifest_integer(value: str, line_no: int) -> int:
    parsed = _manifest_integer(value, line_no)
    if parsed < 1:
        raise ReplayLedgerAuditError("manifest_positive_integer_invalid", line=line_no)
    return parsed


def _manifest_decimal(value: str, line_no: int) -> Decimal:
    if not value or value.strip() != value:
        raise ReplayLedgerAuditError("manifest_decimal_invalid", line=line_no)
    try:
        parsed = Decimal(value)
    except InvalidOperation:
        raise ReplayLedgerAuditError("manifest_decimal_invalid", line=line_no) from None
    if not parsed.is_finite():
        raise ReplayLedgerAuditError("manifest_decimal_invalid", line=line_no)
    return parsed


def fetch_ledger_rows(
    connection: Any,
    request_ids: Sequence[str],
) -> tuple[list[Mapping[str, Any]], list[Mapping[str, Any]]]:
    bounded_ids = list(request_ids)
    if not bounded_ids or len(bounded_ids) > _MAX_AUDIT_REQUESTS:
        raise ReplayLedgerAuditError("ledger_request_id_count_invalid")
    try:
        with connection.cursor() as cursor:
            cursor.execute(READ_ONLY_TRANSACTION_SQL)
            cursor.execute(OUTCOME_QUERY, (bounded_ids,))
            outcomes = list(cursor.fetchall())
            cursor.execute(ATTEMPT_QUERY, (bounded_ids,))
            attempts = list(cursor.fetchall())
        return outcomes, attempts
    finally:
        connection.rollback()


def audit_request_ledger(
    request_id: str,
    outcome_rows: Sequence[Mapping[str, Any]],
    attempt_rows: Sequence[Mapping[str, Any]],
) -> tuple[str, ...]:
    failures: set[str] = set()
    if len(outcome_rows) != 1:
        failures.add("outcome_row_count_invalid")
        return tuple(sorted(failures))
    outcome = outcome_rows[0]

    if (
        outcome.get("logical_outcome") != "success"
        or _integer(outcome.get("logical_status")) != 200
        or _integer(outcome.get("transport_status")) != 200
        or outcome.get("billing_class") != "success"
    ):
        failures.add("outcome_success_terminal_invalid")
    if outcome.get("delivery_state") != "delivered" or outcome.get("completed_at") is None:
        failures.add("outcome_delivery_terminal_invalid")
    winner_id = _nonempty_string(outcome.get("winning_attempt_id"))
    if (
        winner_id is None
        or outcome.get("response_committed") is not True
        or outcome.get("committed_at") is None
    ):
        failures.add("outcome_winner_commit_invalid")

    routing_generation = _positive_integer(outcome.get("routing_generation"))
    cost_generation = _positive_integer(outcome.get("cost_generation"))
    if outcome.get("attribution_enabled") is not True:
        failures.add("outcome_attribution_disabled")
    if routing_generation is None or cost_generation is None:
        failures.add("outcome_generation_invalid")
    if (
        outcome.get("prepared_attempts_enabled") is not True
        or outcome.get("dispatch_owner_present") is not True
        or outcome.get("dispatch_owner_claimed_at") is None
        or outcome.get("dispatch_owner_heartbeat_at") is None
        or outcome.get("dispatch_owner_time_valid") is not True
    ):
        failures.add("dispatch_owner_fencing_invalid")

    candidates = _audit_candidate_snapshot(outcome, failures)
    candidate_count = _positive_integer(outcome.get("candidate_count"))
    if candidate_count is None or candidate_count < 2:
        failures.add("candidate_count_invalid")
    if _integer(outcome.get("next_attempt_no")) != candidate_count:
        failures.add("prepared_attempt_count_invalid")

    if not _billing_terminal_valid(outcome):
        failures.add("winner_billing_terminal_invalid")
    if _integer(outcome.get("billing_fact_count")) != 1:
        failures.add("billing_fact_count_invalid")
    if _integer(outcome.get("winner_billing_fact_count")) != 1:
        failures.add("billing_fact_winner_mismatch")

    attempts = sorted(attempt_rows, key=lambda row: _sort_integer(row.get("attempt_no")))
    if candidate_count is not None and len(attempts) != candidate_count:
        failures.add("prepared_attempt_count_invalid")
    attempt_numbers = [_positive_integer(row.get("attempt_no")) for row in attempts]
    if attempt_numbers != list(range(1, len(attempts) + 1)):
        failures.add("attempt_number_sequence_invalid")
    attempt_ids = [_nonempty_string(row.get("attempt_id")) for row in attempts]
    if None in attempt_ids or len(set(attempt_ids)) != len(attempt_ids):
        failures.add("attempt_identity_invalid")

    if candidates is not None and len(candidates) == len(attempts):
        for candidate, attempt in zip(candidates, attempts, strict=True):
            _audit_attempt_attribution(
                attempt,
                candidate,
                routing_generation,
                cost_generation,
                failures,
            )

    for attempt in attempts:
        if attempt.get("prepared_at") is None or attempt.get("started_at") is None:
            failures.add("attempt_not_prepared")
        if attempt.get("attempt_state") not in _TERMINAL_ATTEMPT_STATES:
            failures.add("attempt_not_terminal")
        if attempt.get("finished_at") is None:
            failures.add("attempt_finish_missing")

    committed = [attempt for attempt in attempts if attempt.get("response_committed") is True]
    winners = [attempt for attempt in attempts if attempt.get("attempt_state") == "won"]
    if len(committed) != 1 or len(winners) != 1:
        failures.add("winner_attempt_count_invalid")
    elif committed[0].get("attempt_id") != winner_id or winners[0].get("attempt_id") != winner_id:
        failures.add("winner_attempt_identity_mismatch")

    if len(attempts) < 2:
        failures.add("dispatch_sequence_invalid")
    else:
        failed_attempt, winning_attempt = attempts[0], attempts[1]
        if (
            failed_attempt.get("attempt_state") != "failed"
            or failed_attempt.get("response_committed") is not False
            or winning_attempt.get("attempt_state") != "won"
            or _integer(winning_attempt.get("upstream_http_status")) != 200
            or not isinstance(outcome.get("stream"), bool)
            or winning_attempt.get("terminal_event_seen") != outcome.get("stream")
            or winning_attempt.get("response_committed") is not True
            or winning_attempt.get("delivery_state") != "delivered"
            or winning_attempt.get("attempt_id") != winner_id
        ):
            failures.add("dispatch_sequence_invalid")
        _audit_failed_attempt_cost(failed_attempt, failures)
        _audit_winner_cost(winning_attempt, failures)
        if failed_attempt.get("failure_owner") != "provider":
            failures.add("failed_attempt_owner_invalid")
        if not _nonempty_string(failed_attempt.get("failure_code")) or not _nonempty_string(
            failed_attempt.get("retry_decision")
        ):
            failures.add("failed_attempt_reason_incomplete")

    for attempt in attempts[2:]:
        _audit_abandoned_attempt(attempt, failures)
    return tuple(sorted(failures))


def _audit_candidate_snapshot(
    outcome: Mapping[str, Any],
    failures: set[str],
) -> list[CandidateAssignment] | None:
    if outcome.get("candidate_snapshot_enabled") is not True:
        failures.add("candidate_snapshot_disabled")
        return None
    raw_assignments = outcome.get("candidate_assignments")
    if not isinstance(raw_assignments, list):
        failures.add("candidate_snapshot_invalid")
        return None
    parsed: list[CandidateAssignment] = []
    for raw in raw_assignments:
        candidate = _parse_candidate(raw)
        if candidate is None:
            failures.add("candidate_snapshot_invalid")
            return None
        parsed.append(candidate)
    candidate_count = _positive_integer(outcome.get("candidate_count"))
    if candidate_count != len(parsed):
        failures.add("candidate_snapshot_invalid")
    canonical = sorted(
        parsed,
        key=lambda item: (item.priority, item.provider_slot_no, item.account_id),
    )
    if parsed != canonical:
        failures.add("candidate_snapshot_order_invalid")
    payload = [candidate.candidate_payload() for candidate in canonical]
    candidate_hash = _sha256_json(payload)
    if outcome.get("candidate_set_hash") != candidate_hash:
        failures.add("candidate_snapshot_hash_invalid")
    return parsed


def _parse_candidate(raw: object) -> CandidateAssignment | None:
    if not isinstance(raw, Mapping):
        return None
    planned_cost_micros = None
    if raw.get("planned_cost_micros") is not None:
        planned_cost_micros = _nonnegative_integer(raw.get("planned_cost_micros"))
        if planned_cost_micros is None:
            return None
    candidate = CandidateAssignment(
        account_id=_positive_integer(raw.get("account_id")) or 0,
        priority=_positive_integer(raw.get("priority")) or 0,
        routing_generation=_positive_integer(raw.get("routing_generation")) or 0,
        cost_generation=_positive_integer(raw.get("cost_generation")) or 0,
        provider_slot_no=_positive_integer(raw.get("provider_slot_no")) or 0,
        channel_id=_positive_integer(raw.get("channel_id")) or 0,
        a6_token_id=_positive_integer(raw.get("a6_token_id")) or 0,
        token_fingerprint=str(raw.get("token_fingerprint") or ""),
        planned_cost_micros=planned_cost_micros,
        assignment_fingerprint=str(raw.get("assignment_fingerprint") or ""),
    )
    if (
        min(
            candidate.account_id,
            candidate.priority,
            candidate.routing_generation,
            candidate.cost_generation,
            candidate.provider_slot_no,
            candidate.channel_id,
            candidate.a6_token_id,
        )
        < 1
        or not _valid_hash(candidate.token_fingerprint)
        or not _valid_hash(candidate.assignment_fingerprint)
        or candidate.assignment_fingerprint != _sha256_json(candidate.assignment_payload())
    ):
        return None
    return candidate


def _audit_attempt_attribution(
    attempt: Mapping[str, Any],
    candidate: CandidateAssignment,
    routing_generation: int | None,
    cost_generation: int | None,
    failures: set[str],
) -> None:
    if (
        _positive_integer(attempt.get("account_id")) != candidate.account_id
        or _positive_integer(attempt.get("routing_generation")) != routing_generation
        or _positive_integer(attempt.get("routing_generation")) != candidate.routing_generation
        or _positive_integer(attempt.get("cost_generation")) != cost_generation
        or _positive_integer(attempt.get("cost_generation")) != candidate.cost_generation
        or _positive_integer(attempt.get("provider_slot_no")) != candidate.provider_slot_no
        or _positive_integer(attempt.get("channel_id")) != candidate.channel_id
        or _positive_integer(attempt.get("a6_token_id")) != candidate.a6_token_id
        or attempt.get("a6_token_fingerprint") != candidate.token_fingerprint
        or not _planned_cost_matches(
            attempt.get("planned_cost_micros"), candidate.planned_cost_micros
        )
        or attempt.get("assignment_fingerprint") != candidate.assignment_fingerprint
    ):
        failures.add("attempt_attribution_mismatch")


def _audit_failed_attempt_cost(attempt: Mapping[str, Any], failures: set[str]) -> None:
    state = attempt.get("cost_observation_state")
    if state not in _TERMINAL_COST_STATES:
        failures.add("failed_attempt_cost_observation_invalid")
        return
    invalid = (
        (
            state == "usage_observed"
            and (
                not _usage_complete(attempt)
                or attempt.get("realized_cost_micros") is not None
                or attempt.get("cost_recorded_at") is not None
            )
        )
        or (
            state == "valued"
            and (
                not _usage_complete(attempt)
                or _nonnegative_integer(attempt.get("realized_cost_micros")) is None
                or attempt.get("cost_recorded_at") is None
            )
        )
        or (
            state == "unavailable"
            and any(
                attempt.get(field) is not None
                for field in (
                    "observed_input_tokens",
                    "observed_output_tokens",
                    "observed_cache_creation_tokens",
                    "observed_cache_read_tokens",
                    "realized_cost_micros",
                    "cost_recorded_at",
                )
            )
        )
    )
    if invalid:
        failures.add("failed_attempt_cost_observation_invalid")


def _audit_winner_cost(attempt: Mapping[str, Any], failures: set[str]) -> None:
    if (
        attempt.get("cost_observation_state") != "valued"
        or not _usage_complete(attempt)
        or _nonnegative_integer(attempt.get("realized_cost_micros")) is None
        or attempt.get("cost_recorded_at") is None
    ):
        failures.add("winner_cost_terminal_invalid")


def _audit_abandoned_attempt(attempt: Mapping[str, Any], failures: set[str]) -> None:
    if (
        attempt.get("attempt_state") != "abandoned"
        or attempt.get("response_committed") is not False
        or attempt.get("delivery_state") != "not_started"
        or attempt.get("upstream_http_status") is not None
        or attempt.get("upstream_request_id_absent") is not True
        or attempt.get("failure_owner") is not None
        or attempt.get("failure_code") != "candidate_not_dispatched"
        or attempt.get("retry_decision") != "not_dispatched"
        or attempt.get("terminal_event_seen") is not False
        or _integer(attempt.get("buffered_bytes")) != 0
        or attempt.get("spilled") is not False
        or attempt.get("cost_observation_state") != "unavailable"
        or any(
            attempt.get(field) is not None
            for field in (
                "observed_input_tokens",
                "observed_output_tokens",
                "observed_cache_creation_tokens",
                "observed_cache_read_tokens",
            )
        )
        or attempt.get("realized_cost_micros") is not None
        or attempt.get("cost_recorded_at") is not None
    ):
        failures.add("prepared_candidate_terminal_invalid")


def _billing_terminal_valid(outcome: Mapping[str, Any]) -> bool:
    disposition = outcome.get("billing_disposition")
    recorded = outcome.get("billing_recorded_at") is not None
    if disposition == "billed":
        return recorded and _nonnegative_integer(outcome.get("billed_micros")) is not None
    if disposition == "not_billable":
        return recorded and outcome.get("billed_micros") is None
    return False


def _usage_complete(attempt: Mapping[str, Any]) -> bool:
    return all(
        _nonnegative_integer(attempt.get(field)) is not None
        for field in (
            "observed_input_tokens",
            "observed_output_tokens",
            "observed_cache_creation_tokens",
            "observed_cache_read_tokens",
        )
    )


def _planned_cost_matches(actual: object, expected: int | None) -> bool:
    if expected is None:
        return actual is None
    return _nonnegative_integer(actual) == expected


def build_report(
    manifest: ManifestAudit,
    outcome_rows: Sequence[Mapping[str, Any]],
    attempt_rows: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    request_id_set = set(manifest.request_ids)
    outcomes_by_request: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    attempts_by_request: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    unexpected_rows = 0
    for row in outcome_rows:
        request_id = _nonempty_string(row.get("request_id"))
        if request_id in request_id_set:
            outcomes_by_request[request_id].append(row)
        else:
            unexpected_rows += 1
    for row in attempt_rows:
        request_id = _nonempty_string(row.get("request_id"))
        if request_id in request_id_set:
            attempts_by_request[request_id].append(row)
        else:
            unexpected_rows += 1

    failures: list[dict[str, Any]] = []
    failure_counts: Counter[str] = Counter()
    for request_id in manifest.request_ids:
        codes = audit_request_ledger(
            request_id,
            outcomes_by_request[request_id],
            attempts_by_request[request_id],
        )
        if codes:
            failures.append({"request_id": request_id, "codes": list(codes)})
            failure_counts.update(codes)

    billing_fact_rows = sum(
        max(_nonnegative_integer(row.get("billing_fact_count")) or 0, 0)
        for row in outcome_rows
        if _nonempty_string(row.get("request_id")) in request_id_set
    )
    global_errors = ["ledger_unexpected_request_id"] if unexpected_rows else []
    if (
        manifest.required_p95_ms is not None
        and manifest.dispatch_p95_ms >= manifest.required_p95_ms
    ):
        global_errors.append("manifest_dispatch_p95_threshold_not_met")
    return {
        "success": not failures and not global_errors,
        "contract": "a6-provider-replay-ledger-v192",
        "manifest": {
            "total_rows": manifest.total_rows,
            "strict_accepted_samples": len(manifest.accepted),
            "expected_samples": manifest.expected_samples,
            "sha256": manifest.sha256,
            "dispatch_ms": {
                "p50": str(manifest.dispatch_p50_ms),
                "p95": str(manifest.dispatch_p95_ms),
                "required_p95_less_than": (
                    None
                    if manifest.required_p95_ms is None
                    else str(manifest.required_p95_ms)
                ),
            },
        },
        "ledger": {
            "audited_requests": len(manifest.accepted),
            "passed_requests": len(manifest.accepted) - len(failures),
            "failed_requests": len(failures),
            "outcome_rows": len(outcome_rows),
            "attempt_rows": len(attempt_rows),
            "billing_fact_rows": billing_fact_rows,
        },
        "errors": global_errors,
        "failure_counts": dict(sorted(failure_counts.items())),
        "failures": failures,
    }


def _sha256_json(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("ascii")
    return hashlib.sha256(payload).hexdigest()


def _integer(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return None


def _positive_integer(value: object) -> int | None:
    parsed = _integer(value)
    return parsed if parsed is not None and parsed > 0 else None


def _nonnegative_integer(value: object) -> int | None:
    parsed = _integer(value)
    return parsed if parsed is not None and parsed >= 0 else None


def _sort_integer(value: object) -> int:
    parsed = _integer(value)
    return parsed if parsed is not None else sys.maxsize


def _nonempty_string(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return value


def _valid_hash(value: object) -> bool:
    return isinstance(value, str) and _HASH_PATTERN.fullmatch(value) is not None


def _error_report(code: str, *, line: int | None = None) -> dict[str, Any]:
    report: dict[str, Any] = {
        "success": False,
        "contract": "a6-provider-replay-ledger-v192",
        "error": code,
    }
    if line is not None:
        report["line"] = line
    return report


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(description=__doc__)
    command.add_argument("--manifest", type=Path, required=True)
    command.add_argument("--expected-samples", type=int, required=True)
    command.add_argument("--expected-manifest-sha256")
    command.add_argument(
        "--require-p95-ms",
        type=Decimal,
        help="require accepted strict A-to-B dispatch P95 to be strictly below this value",
    )
    database = command.add_mutually_exclusive_group()
    database.add_argument("--database-url")
    database.add_argument("--database-url-file", type=Path)
    database.add_argument(
        "--database-env-file",
        type=Path,
        help="systemd EnvironmentFile containing split Sub2API DATABASE_* fields",
    )
    command.add_argument("--database-url-env", default="SUB2API_DATABASE_URL")
    command.add_argument("--connect-timeout-seconds", type=int, default=10)
    return command


def _quote_conninfo_value(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _database_conninfo_from_env_file(path: Path) -> str:
    try:
        if path.stat().st_size > _MAX_DATABASE_ENV_FILE_BYTES:
            raise ReplayLedgerAuditError("database_env_file_invalid")
        payload = path.read_text(encoding="utf-8")
    except ReplayLedgerAuditError:
        raise
    except (OSError, UnicodeError):
        raise ReplayLedgerAuditError("database_env_file_read_failed") from None

    values: dict[str, str] = {}
    expected = {name for name, _ in _DATABASE_ENV_FIELDS}
    for raw_line in payload.splitlines():
        line = raw_line.strip()
        if not line or line.startswith(("#", ";")):
            continue
        name, separator, raw_value = line.partition("=")
        name = name.strip()
        if not separator or name not in expected:
            continue
        try:
            parts = shlex.split(raw_value.strip(), comments=False, posix=True)
        except ValueError:
            raise ReplayLedgerAuditError("database_env_file_invalid") from None
        values[name] = " ".join(parts)

    if any(not values.get(name) for name, _ in _DATABASE_ENV_FIELDS):
        raise ReplayLedgerAuditError("database_env_file_invalid")
    if not values["DATABASE_PORT"].isdigit():
        raise ReplayLedgerAuditError("database_env_file_invalid")
    return " ".join(
        f"{keyword}={_quote_conninfo_value(values[name])}"
        for name, keyword in _DATABASE_ENV_FIELDS
    )


def _database_url(args: argparse.Namespace) -> str:
    if args.database_url:
        value = args.database_url
    elif args.database_url_file:
        try:
            value = args.database_url_file.read_text(encoding="utf-8")
        except OSError:
            raise ReplayLedgerAuditError("database_url_file_read_failed") from None
    elif args.database_env_file:
        return _database_conninfo_from_env_file(args.database_env_file)
    else:
        value = os.environ.get(args.database_url_env, "")
    value = str(value).strip()
    if not value:
        raise ReplayLedgerAuditError("database_url_missing")
    return value


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.connect_timeout_seconds < 1:
        print(json.dumps(_error_report("database_connect_timeout_invalid"), sort_keys=True))
        return 2
    try:
        with args.manifest.open(encoding="ascii", newline="") as source:
            manifest = parse_manifest(
                source,
                expected_samples=args.expected_samples,
                expected_sha256=args.expected_manifest_sha256,
                require_p95_ms=args.require_p95_ms,
            )
    except ReplayLedgerAuditError as exc:
        print(json.dumps(_error_report(exc.code, line=exc.line), sort_keys=True))
        return 2
    except (OSError, UnicodeError):
        print(json.dumps(_error_report("manifest_read_failed"), sort_keys=True))
        return 2

    try:
        database_url = _database_url(args)
    except ReplayLedgerAuditError as exc:
        print(json.dumps(_error_report(exc.code), sort_keys=True))
        return 2

    try:
        import psycopg
        from psycopg.rows import dict_row

        connection = psycopg.connect(
            database_url,
            row_factory=dict_row,
            connect_timeout=args.connect_timeout_seconds,
        )
    except Exception:
        print(json.dumps(_error_report("database_connect_failed"), sort_keys=True))
        return 3
    try:
        outcomes, attempts = fetch_ledger_rows(connection, manifest.request_ids)
        report = build_report(manifest, outcomes, attempts)
    except Exception:
        print(json.dumps(_error_report("database_query_failed"), sort_keys=True))
        return 3
    finally:
        with suppress(Exception):
            connection.close()
    print(json.dumps(report, ensure_ascii=True, separators=(",", ":"), sort_keys=True))
    return 0 if report["success"] else 4


if __name__ == "__main__":
    sys.exit(main())
