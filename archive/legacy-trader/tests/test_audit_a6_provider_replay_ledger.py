from __future__ import annotations

import copy
import hashlib
import io
import json
from decimal import Decimal
from typing import Any

import psycopg
import pytest

from scripts import audit_a6_provider_replay_ledger as audit

REQUEST_ID = "hk-shadow-hotpath-20260729T120000Z-001"
TOKEN_FINGERPRINTS = ("1" * 64, "2" * 64, "3" * 64)


def _candidate(index: int) -> dict[str, Any]:
    candidate = {
        "account_id": 100 + index,
        "priority": index,
        "routing_generation": 42,
        "cost_generation": 19,
        "provider_slot_no": index,
        "channel_id": 200 + index,
        "a6_token_id": 300 + index,
        "token_fingerprint": TOKEN_FINGERPRINTS[index - 1],
        "planned_cost_micros": 10 + index,
    }
    assignment_payload = {key: value for key, value in candidate.items() if key != "priority"}
    candidate["assignment_fingerprint"] = audit._sha256_json(assignment_payload)
    return candidate


def _ledger_fixture(candidate_count: int = 3) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    candidates = [_candidate(index) for index in range(1, candidate_count + 1)]
    outcome = {
        "request_id": REQUEST_ID,
        "next_attempt_no": candidate_count,
        "stream": False,
        "logical_status": 200,
        "logical_outcome": "success",
        "winning_attempt_id": "attempt-2",
        "response_committed": True,
        "delivery_state": "delivered",
        "committed_at": "2026-07-29T12:00:01Z",
        "completed_at": "2026-07-29T12:00:02Z",
        "attribution_enabled": True,
        "routing_generation": 42,
        "cost_generation": 19,
        "prepared_attempts_enabled": True,
        "dispatch_owner_present": True,
        "dispatch_owner_claimed_at": "2026-07-29T12:00:00Z",
        "dispatch_owner_heartbeat_at": "2026-07-29T12:00:02Z",
        "dispatch_owner_time_valid": True,
        "billing_disposition": "billed",
        "billed_micros": 20,
        "billing_recorded_at": "2026-07-29T12:00:02Z",
        "candidate_snapshot_enabled": True,
        "candidate_set_hash": audit._sha256_json(candidates),
        "candidate_count": candidate_count,
        "candidate_assignments": candidates,
        "billing_class": "success",
        "transport_status": 200,
        "billing_fact_count": 1,
        "winner_billing_fact_count": 1,
    }
    attempts: list[dict[str, Any]] = []
    for index, candidate in enumerate(candidates, start=1):
        attempts.append(
            {
                "request_id": REQUEST_ID,
                "attempt_id": f"attempt-{index}",
                "attempt_no": index,
                "account_id": candidate["account_id"],
                "attempt_state": "abandoned",
                "upstream_http_status": None,
                "upstream_request_id_absent": True,
                "failure_owner": None,
                "failure_code": "candidate_not_dispatched",
                "retry_decision": "not_dispatched",
                "terminal_event_seen": False,
                "response_committed": False,
                "delivery_state": "not_started",
                "buffered_bytes": 0,
                "spilled": False,
                "started_at": "2026-07-29T12:00:00Z",
                "prepared_at": "2026-07-29T12:00:00Z",
                "finished_at": "2026-07-29T12:00:02Z",
                "routing_generation": candidate["routing_generation"],
                "cost_generation": candidate["cost_generation"],
                "provider_slot_no": candidate["provider_slot_no"],
                "channel_id": candidate["channel_id"],
                "a6_token_id": candidate["a6_token_id"],
                "a6_token_fingerprint": candidate["token_fingerprint"],
                "planned_cost_micros": candidate["planned_cost_micros"],
                "realized_cost_micros": None,
                "cost_recorded_at": None,
                "assignment_fingerprint": candidate["assignment_fingerprint"],
                "cost_observation_state": "unavailable",
                "observed_input_tokens": None,
                "observed_output_tokens": None,
                "observed_cache_creation_tokens": None,
                "observed_cache_read_tokens": None,
            }
        )
    attempts[0].update(
        {
            "attempt_state": "failed",
            "upstream_http_status": 503,
            "failure_owner": "provider",
            "failure_code": "synthetic_provider_failure",
            "retry_decision": "retry_next_candidate",
        }
    )
    attempts[1].update(
        {
            "attempt_state": "won",
            "upstream_http_status": 200,
            "terminal_event_seen": False,
            "response_committed": True,
            "delivery_state": "delivered",
            "buffered_bytes": 128,
            "realized_cost_micros": 12,
            "cost_recorded_at": "2026-07-29T12:00:02Z",
            "cost_observation_state": "valued",
            "observed_input_tokens": 10,
            "observed_output_tokens": 5,
            "observed_cache_creation_tokens": 0,
            "observed_cache_read_tokens": 0,
            "failure_code": None,
            "retry_decision": None,
        }
    )
    return [outcome], attempts


def _manifest_text(
    *,
    accepted: int = 1,
    dispatch_ms: tuple[str, ...] | None = None,
) -> str:
    if dispatch_ms is None:
        dispatch_ms = tuple("1.25" for _ in range(accepted))
    assert len(dispatch_ms) == accepted
    rows = ["\t".join(audit.MANIFEST_COLUMNS)]
    for index in range(1, accepted + 1):
        rows.append(
            "\t".join(
                (
                    str(index),
                    str(index),
                    f"hk-shadow-hotpath-20260729T120000Z-{index:03d}",
                    "a,b",
                    "true",
                    dispatch_ms[index - 1],
                    "200",
                    "0.250",
                    "128",
                    "1",
                    "0",
                    "true",
                    "accepted",
                )
            )
        )
    return "\n".join(rows) + "\n"


def _manifest() -> audit.ManifestAudit:
    return audit.parse_manifest(io.StringIO(_manifest_text()), expected_samples=1)


def test_manifest_parser_accepts_only_strict_contiguous_samples() -> None:
    rows = _manifest_text(accepted=1).splitlines()
    rows.append(
        "\t".join(
            (
                "",
                "2",
                "hk-shadow-hotpath-20260729T120000Z-002",
                "a",
                "false",
                "",
                "503",
                "0.100",
                "20",
                "0",
                "0",
                "false",
                "not_strict_a_b",
            )
        )
    )
    rows.append(
        "\t".join(
            (
                "2",
                "3",
                "hk-shadow-hotpath-20260729T120000Z-003",
                "a,b",
                "true",
                "2.50",
                "200",
                "0.500",
                "256",
                "1",
                "0",
                "true",
                "accepted",
            )
        )
    )

    manifest = audit.parse_manifest(io.StringIO("\n".join(rows) + "\n"), expected_samples=2)

    assert manifest.total_rows == 3
    assert manifest.request_ids == (
        "hk-shadow-hotpath-20260729T120000Z-001",
        "hk-shadow-hotpath-20260729T120000Z-003",
    )


@pytest.mark.parametrize(
    ("column", "value", "code"),
    (
        ("routes", "b,a", "manifest_routes_not_strict_a_b"),
        ("body_hash_equal", "false", "manifest_body_hash_mismatch"),
        ("winner_count", "2", "manifest_winner_count_invalid"),
        ("a_leak_count", "1", "manifest_a_content_leak"),
    ),
)
def test_manifest_parser_rejects_non_strict_accepted_rows(
    column: str, value: str, code: str
) -> None:
    rows = list(csv_row.split("\t") for csv_row in _manifest_text().splitlines())
    rows[1][audit.MANIFEST_COLUMNS.index(column)] = value
    payload = "\n".join("\t".join(row) for row in rows) + "\n"

    with pytest.raises(audit.ReplayLedgerAuditError) as raised:
        audit.parse_manifest(io.StringIO(payload), expected_samples=1)

    assert raised.value.code == code


def test_manifest_parser_rejects_wrong_expected_sample_count() -> None:
    with pytest.raises(audit.ReplayLedgerAuditError) as raised:
        audit.parse_manifest(io.StringIO(_manifest_text()), expected_samples=2)

    assert raised.value.code == "manifest_accepted_sample_count_mismatch"


def test_manifest_sha256_binds_the_exact_persisted_bytes() -> None:
    payload = _manifest_text()
    expected_sha256 = hashlib.sha256(payload.encode("ascii")).hexdigest()

    manifest = audit.parse_manifest(
        io.StringIO(payload),
        expected_samples=1,
        expected_sha256=expected_sha256,
    )

    assert manifest.sha256 == expected_sha256
    tampered = payload.replace("1.25", "1.26", 1)
    with pytest.raises(audit.ReplayLedgerAuditError) as raised:
        audit.parse_manifest(
            io.StringIO(tampered),
            expected_samples=1,
            expected_sha256=expected_sha256,
        )
    assert raised.value.code == "manifest_sha256_mismatch"


def test_dispatch_p95_gate_is_strict_at_threshold_boundary() -> None:
    outcomes, attempts = _ledger_fixture()
    below = audit.parse_manifest(
        io.StringIO(_manifest_text(dispatch_ms=("249.999",))),
        expected_samples=1,
        require_p95_ms=Decimal("250"),
    )
    boundary = audit.parse_manifest(
        io.StringIO(_manifest_text(dispatch_ms=("250.000",))),
        expected_samples=1,
        require_p95_ms=Decimal("250"),
    )

    below_report = audit.build_report(below, outcomes, attempts)
    boundary_report = audit.build_report(boundary, outcomes, attempts)

    assert below_report["success"] is True
    assert below_report["manifest"]["dispatch_ms"] == {
        "p50": "249.999",
        "p95": "249.999",
        "required_p95_less_than": "250",
    }
    assert boundary_report["success"] is False
    assert boundary_report["errors"] == ["manifest_dispatch_p95_threshold_not_met"]


def test_dispatch_percentiles_use_the_benchmark_nearest_rank_contract() -> None:
    manifest = audit.parse_manifest(
        io.StringIO(
            _manifest_text(
                accepted=20,
                dispatch_ms=tuple(str(value) for value in range(1, 21)),
            )
        ),
        expected_samples=20,
        require_p95_ms=Decimal("20"),
    )

    assert manifest.dispatch_p50_ms == Decimal("10")
    assert manifest.dispatch_p95_ms == Decimal("19")


def test_dispatch_percentile_gate_rejects_missing_accepted_samples() -> None:
    header_only = "\t".join(audit.MANIFEST_COLUMNS) + "\n"

    with pytest.raises(audit.ReplayLedgerAuditError) as raised:
        audit.parse_manifest(
            io.StringIO(header_only),
            expected_samples=1,
            require_p95_ms=Decimal("250"),
        )

    assert raised.value.code == "manifest_accepted_sample_count_mismatch"


def test_cli_rejects_tampered_manifest_before_database_access(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    original = _manifest_text()
    manifest_path = tmp_path / "manifest.tsv"
    manifest_path.write_text(original.replace("1.25", "1.26", 1), encoding="ascii")
    expected_sha256 = hashlib.sha256(original.encode("ascii")).hexdigest()

    def unexpected_connect(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("database access must follow manifest provenance checks")

    monkeypatch.setattr(psycopg, "connect", unexpected_connect)
    exit_code = audit.main(
        [
            "--manifest",
            str(manifest_path),
            "--expected-samples",
            "1",
            "--expected-manifest-sha256",
            expected_sha256,
            "--database-url",
            "postgresql://unused",
        ]
    )

    assert exit_code == 2
    assert json.loads(capsys.readouterr().out)["error"] == "manifest_sha256_mismatch"


def test_complete_a_failed_b_won_ledger_passes() -> None:
    outcomes, attempts = _ledger_fixture()

    report = audit.build_report(_manifest(), outcomes, attempts)

    assert report["success"] is True
    assert report["ledger"] == {
        "audited_requests": 1,
        "passed_requests": 1,
        "failed_requests": 0,
        "outcome_rows": 1,
        "attempt_rows": 3,
        "billing_fact_rows": 1,
    }
    assert report["failures"] == []


@pytest.mark.parametrize(
    ("mutate", "code"),
    (
        (
            lambda outcomes, attempts: attempts[2].update(
                {"attempt_state": "won", "response_committed": True}
            ),
            "winner_attempt_count_invalid",
        ),
        (
            lambda outcomes, attempts: attempts[0].update({"attempt_state": "won"}),
            "dispatch_sequence_invalid",
        ),
        (
            lambda outcomes, attempts: attempts[2].update({"attempt_state": "started"}),
            "prepared_candidate_terminal_invalid",
        ),
        (
            lambda outcomes, attempts: attempts[0].update({"cost_observation_state": "pending"}),
            "failed_attempt_cost_observation_invalid",
        ),
        (
            lambda outcomes, attempts: attempts[1].update(
                {
                    "cost_observation_state": "usage_observed",
                    "realized_cost_micros": None,
                    "cost_recorded_at": None,
                }
            ),
            "winner_cost_terminal_invalid",
        ),
        (
            lambda outcomes, attempts: outcomes[0].update({"billing_fact_count": 2}),
            "billing_fact_count_invalid",
        ),
        (
            lambda outcomes, attempts: outcomes[0].update({"dispatch_owner_time_valid": False}),
            "dispatch_owner_fencing_invalid",
        ),
    ),
)
def test_ledger_invariant_failures_are_machine_readable(mutate: Any, code: str) -> None:
    outcomes, attempts = _ledger_fixture()
    mutate(outcomes, attempts)

    report = audit.build_report(_manifest(), outcomes, attempts)

    assert report["success"] is False
    assert code in report["failures"][0]["codes"]


class _FakeCursor:
    def __init__(self, connection: _FakeConnection) -> None:
        self.connection = connection
        self.rows: list[dict[str, Any]] = []

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, query: str, params: object = None) -> None:
        self.connection.statements.append((query, params))
        if "FROM a6_provider_request_outcomes" in query:
            self.rows = self.connection.outcomes
        elif "FROM a6_provider_request_attempts AS attempt" in query:
            self.rows = self.connection.attempts
        else:
            self.rows = []

    def fetchall(self) -> list[dict[str, Any]]:
        return self.rows


class _FakeConnection:
    def __init__(self, outcomes: list[dict[str, Any]], attempts: list[dict[str, Any]]) -> None:
        self.outcomes = outcomes
        self.attempts = attempts
        self.statements: list[tuple[str, object]] = []
        self.rollback_count = 0
        self.closed = False

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self)

    def rollback(self) -> None:
        self.rollback_count += 1

    def close(self) -> None:
        self.closed = True


def test_database_reads_are_bounded_and_read_only() -> None:
    outcomes, attempts = _ledger_fixture()
    connection = _FakeConnection(outcomes, attempts)

    actual_outcomes, actual_attempts = audit.fetch_ledger_rows(connection, [REQUEST_ID])

    assert actual_outcomes == outcomes
    assert actual_attempts == attempts
    assert connection.statements[0][0] == audit.READ_ONLY_TRANSACTION_SQL
    assert len(connection.statements) == 3
    assert connection.statements[1][1] == ([REQUEST_ID],)
    assert connection.statements[2][1] == ([REQUEST_ID],)
    assert connection.rollback_count == 1


def test_json_report_excludes_dsn_tokens_keys_and_fingerprints() -> None:
    outcomes, attempts = _ledger_fixture()
    report = audit.build_report(_manifest(), outcomes, attempts)

    rendered = json.dumps(report, sort_keys=True)

    for secret in (
        "postgresql://user:password@host/database",
        "Bearer TOKEN",
        "api_key",
        *TOKEN_FINGERPRINTS,
        outcomes[0]["candidate_set_hash"],
        attempts[0]["assignment_fingerprint"],
    ):
        assert secret not in rendered


def test_duplicate_or_missing_outcome_fails_closed() -> None:
    outcomes, attempts = _ledger_fixture()

    duplicate = audit.build_report(_manifest(), outcomes * 2, attempts)
    missing = audit.build_report(_manifest(), [], attempts)

    assert duplicate["failures"][0]["codes"] == ["outcome_row_count_invalid"]
    assert missing["failures"][0]["codes"] == ["outcome_row_count_invalid"]


def test_candidate_snapshot_hash_and_attempt_attribution_are_verified() -> None:
    outcomes, attempts = _ledger_fixture()
    tampered_hash_outcomes = copy.deepcopy(outcomes)
    tampered_hash_outcomes[0]["candidate_set_hash"] = "f" * 64
    tampered_attempts = copy.deepcopy(attempts)
    tampered_attempts[0]["channel_id"] += 1

    hash_report = audit.build_report(_manifest(), tampered_hash_outcomes, attempts)
    attribution_report = audit.build_report(_manifest(), outcomes, tampered_attempts)

    assert "candidate_snapshot_hash_invalid" in hash_report["failures"][0]["codes"]
    assert "attempt_attribution_mismatch" in attribution_report["failures"][0]["codes"]


def test_planned_cost_is_optional_but_must_match_attempt() -> None:
    outcomes, attempts = _ledger_fixture()
    candidate = outcomes[0]["candidate_assignments"][0]
    candidate.pop("planned_cost_micros")
    assignment_payload = {
        key: value
        for key, value in candidate.items()
        if key not in {"priority", "assignment_fingerprint"}
    }
    candidate["assignment_fingerprint"] = audit._sha256_json(assignment_payload)
    outcomes[0]["candidate_set_hash"] = audit._sha256_json(outcomes[0]["candidate_assignments"])
    attempts[0]["planned_cost_micros"] = None
    attempts[0]["assignment_fingerprint"] = candidate["assignment_fingerprint"]

    valid = audit.build_report(_manifest(), outcomes, attempts)
    attempts[0]["planned_cost_micros"] = 99
    mismatched = audit.build_report(_manifest(), outcomes, attempts)

    assert valid["success"] is True
    assert "attempt_attribution_mismatch" in mismatched["failures"][0]["codes"]


def test_unexpected_database_request_id_fails_without_echoing_it() -> None:
    outcomes, attempts = _ledger_fixture()
    unexpected = copy.deepcopy(outcomes[0])
    unexpected["request_id"] = "TOKEN-SHOULD-NOT-BE-ECHOED"

    report = audit.build_report(_manifest(), outcomes + [unexpected], attempts)

    assert report["success"] is False
    assert report["errors"] == ["ledger_unexpected_request_id"]
    assert "TOKEN-SHOULD-NOT-BE-ECHOED" not in json.dumps(report)


def test_cli_returns_nonzero_for_invariant_failure_and_redacts_database_url(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    manifest_path = tmp_path / "manifest.tsv"
    manifest_path.write_text(_manifest_text(), encoding="ascii")
    outcomes, attempts = _ledger_fixture()
    outcomes[0]["billing_fact_count"] = 2
    connection = _FakeConnection(outcomes, attempts)
    database_url = "postgresql://user:SECRET@HOST/database"
    monkeypatch.setattr(psycopg, "connect", lambda *_args, **_kwargs: connection)

    exit_code = audit.main(
        [
            "--manifest",
            str(manifest_path),
            "--expected-samples",
            "1",
            "--database-url",
            database_url,
        ]
    )

    rendered = capsys.readouterr().out
    assert exit_code == 4
    assert json.loads(rendered)["success"] is False
    assert database_url not in rendered
    assert "SECRET" not in rendered
    assert connection.closed is True


def test_database_env_file_builds_quoted_conninfo_without_reporting_it(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    manifest_path = tmp_path / "manifest.tsv"
    manifest_path.write_text(_manifest_text(), encoding="ascii")
    env_path = tmp_path / "sub2api.env"
    env_path.write_text(
        "\n".join(
            (
                "DATABASE_HOST=127.0.0.1",
                "DATABASE_PORT=5432",
                "DATABASE_USER=sub2api",
                "DATABASE_PASSWORD=\"secret with 'quote and \\\\ slash\"",
                "DATABASE_DBNAME=sub2api_codexmanager",
                "DATABASE_SSLMODE=disable",
                "UNRELATED_TOKEN=must-not-be-read",
            )
        ),
        encoding="utf-8",
    )
    outcomes, attempts = _ledger_fixture()
    connection = _FakeConnection(outcomes, attempts)
    captured_conninfo: list[str] = []

    def connect(conninfo: str, **_kwargs: Any) -> _FakeConnection:
        captured_conninfo.append(conninfo)
        return connection

    monkeypatch.setattr(psycopg, "connect", connect)

    exit_code = audit.main(
        [
            "--manifest",
            str(manifest_path),
            "--expected-samples",
            "1",
            "--database-env-file",
            str(env_path),
        ]
    )

    rendered = capsys.readouterr().out
    assert exit_code == 0
    expected_password = "secret with 'quote and \\ slash"
    assert captured_conninfo == [
        "host='127.0.0.1' port='5432' user='sub2api' "
        f"password={audit._quote_conninfo_value(expected_password)} "
        "dbname='sub2api_codexmanager' sslmode='disable'"
    ]
    assert "secret" not in rendered
    assert "UNRELATED_TOKEN" not in rendered


@pytest.mark.parametrize(
    "payload",
    (
        "DATABASE_HOST=127.0.0.1\n",
        "\n".join(
            (
                "DATABASE_HOST=127.0.0.1",
                "DATABASE_PORT=not-a-port",
                "DATABASE_USER=sub2api",
                "DATABASE_PASSWORD=secret",
                "DATABASE_DBNAME=sub2api",
                "DATABASE_SSLMODE=disable",
            )
        ),
    ),
)
def test_database_env_file_rejects_incomplete_or_invalid_contract(
    tmp_path: Any,
    payload: str,
) -> None:
    env_path = tmp_path / "sub2api.env"
    env_path.write_text(payload, encoding="utf-8")

    with pytest.raises(audit.ReplayLedgerAuditError) as raised:
        audit._database_conninfo_from_env_file(env_path)

    assert raised.value.code == "database_env_file_invalid"
