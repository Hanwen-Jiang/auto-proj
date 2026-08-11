from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from a6api_auto.trade_provider_cost import build_cost_plan
from a6api_auto.trade_provider_pool_shadow_evidence import (
    SHADOW_EVIDENCE_SCHEMA,
    canonical_sha256,
    identity_digest,
)
from a6api_auto.trade_provider_pool_size_evidence import build_pool_size_evidence
from scripts.audit_a6_provider_shadow_journal import audit_journal_lines

MODEL = "gpt-5.6-sol"


def journal_line(timestamp: datetime, message: str) -> str:
    return json.dumps(
        {
            "__REALTIME_TIMESTAMP": str(int(timestamp.timestamp() * 1_000_000)),
            "MESSAGE": message,
        }
    )


def shadow_message(
    generation: int,
    *,
    sequence: int,
    remote_writes: int = 0,
    inventory_changed: int = 0,
) -> str:
    top = [{"channel_id": 10, "supplier_id": 210, "listing_id": 110}]
    standby = [{"channel_id": 20, "supplier_id": 220, "listing_id": 120}]
    owned = {"supplier_ids": [901], "channel_ids": [902], "listing_ids": [903]}
    cost_input = {
        "model": MODEL,
        "ordered_channels": [{"channel_id": 10, "price": "0.4", "final_multiplier": "0.4"}],
        "assignments": [[1, 1001]],
        "ready_slot_nos": [1],
        "token_mix_version": "bootstrap-v1",
        "exchange_rate_version": "identity-v1",
        "retry_reserve_window": "bootstrap",
        "retry_reserve_value": "0.2",
        "realized_cost_window": "provider-usage-match-v1:24h",
        "realized_cost_p95_value": "0.55",
        "margin_bps": 1000,
        "headroom_bps": 500,
        "quantization_version": "decimal-ceiling-v1",
    }
    cost_plan = build_cost_plan(
        {1: Decimal("0.4")},
        retry_reserve_multiplier=Decimal("0.2"),
        bootstrap_retry_reserve=Decimal("0.2"),
        realized_cost_p95_multiplier=Decimal("0.55"),
        margin_bps=1000,
        headroom_bps=500,
    )
    cost_output = cost_plan.public()
    cost_output.pop("cost_output_hash")
    semantic_input = {
        "model": MODEL,
        "config_version": "a6-provider-topn-failover-v2",
        "cost_input_hash": canonical_sha256(cost_input),
        "previous_billing_floor_multiplier": "0",
        "conservative_fallback_cost": "0",
        "quantum": cost_output["quantum"],
    }
    decision_at = datetime(2026, 7, 29, 3, 32, tzinfo=timezone.utc)
    pool_size_evidence = build_pool_size_evidence(
        size_mode="all_funded",
        current_desired_size=1,
        funded_eligible_provider_count=1,
        decision_at=decision_at,
        observation_started_at=decision_at - timedelta(days=14),
        last_shrink_at=None,
        p99_concurrency=1,
        safe_concurrency_per_slot=1,
        target_utilization="0.7",
        p99_simultaneous_failed_slots=0,
        providers_needed_for_24h_liquidity_runway=1,
        liquidity_runway_evidence_complete=True,
        liquidity_runway_capacity_sufficient=True,
        minimum_active_slots=3,
        initial_max_active_slots=12,
        minimum_observation=timedelta(days=14),
        minimum_shrink_interval=timedelta(hours=72),
        desired_size=1,
    )
    return json.dumps(
        {
            "mode": "shadow",
            "model": MODEL,
            "success": True,
            "attempt_count": 1,
            "activated": False,
            "routing_generation": generation,
            "cost_generation": generation,
            "snapshot_sequence": sequence,
            "snapshot_candidate_count": 2,
            "eligible_count": 2,
            "top_count": 1,
            "standby_count": 1,
            "desired_size": 1,
            "plan_write_count": 1,
            "remote_write_count": remote_writes,
            "inventory_projection": {
                "canonical_count": 5,
                "funded_count": 2,
                "changed_count": inventory_changed,
                "retired_count": 0,
            },
            "shadow_evidence": {
                "schema": SHADOW_EVIDENCE_SCHEMA,
                "snapshot": {
                    "status": "completed",
                    "pages_complete": True,
                    "candidate_count": 2,
                    "result_hash": "a" * 64,
                    "result_hash_verified": True,
                    "source_hash": "b" * 64,
                },
                "selection": {
                    "top": top,
                    "standby": standby,
                    "top_identity_digest": identity_digest("top", top),
                    "standby_identity_digest": identity_digest("standby", standby),
                    "owned": {**owned, "identity_digest": canonical_sha256(owned)},
                    "top_owned_overlap_count": 0,
                    "standby_owned_overlap_count": 0,
                },
                "cost": {
                    "input": cost_input,
                    "input_hash": canonical_sha256(cost_input),
                    "output": cost_output,
                    "output_hash": cost_plan.cost_output_hash,
                    "coverage": {
                        "minimum_realized_cost_samples": 100,
                        "realized_cost_sample_count": 100,
                        "observed_realized_cost_p95_multiplier": "0.55",
                        "applied_realized_cost_p95_multiplier": "0.55",
                        "actual_cost_coverage_state": "covered",
                        "route_plus_retry_covered": True,
                        "realized_cost_covered": True,
                        "protected_floor_covered": True,
                    },
                },
                "semantic_stability": {
                    "input": semantic_input,
                    "input_hash": canonical_sha256(semantic_input),
                    "output_hash": cost_plan.cost_output_hash,
                },
            },
            "pool_size_evidence": pool_size_evidence,
        }
    )


def test_complete_shadow_window_is_machine_verifiable() -> None:
    started = datetime(2026, 7, 29, 3, 32, tzinfo=timezone.utc)
    lines = [
        journal_line(
            started + timedelta(seconds=offset),
            shadow_message(
                679 + index,
                sequence=94_284 + index,
                inventory_changed=5 if index == 0 else 0,
            ),
        )
        for index, offset in enumerate((0, 30, 60, 90, 120))
    ]

    report = audit_journal_lines(
        lines,
        model=MODEL,
        minimum_window_seconds=120,
        maximum_gap_seconds=31,
    )

    assert report["success"] is True
    assert report["observation_count"] == 5
    assert report["window_seconds"] == 120
    assert report["maximum_observed_gap_seconds"] == 30
    assert report["maximum_observed_gap_start"] == started.isoformat()
    assert report["maximum_observed_gap_end"] == (started + timedelta(seconds=30)).isoformat()
    assert report["inventory_changed_total"] == 5
    assert report["first_routing_generation"] == 679
    assert report["last_routing_generation"] == 683


def test_retry_audit_contract_sets_default_window_boundary() -> None:
    started = datetime(2026, 7, 29, 3, 32, tzinfo=timezone.utc)
    legacy = json.loads(shadow_message(678, sequence=94_283))
    del legacy["attempt_count"]
    legacy_failure = {
        "success": False,
        "mode": "shadow",
        "model": MODEL,
        "activated": False,
        "plan_write_count": 0,
        "remote_write_count": 0,
        "failure_type": "ProviderPoolRankError",
    }

    report = audit_journal_lines(
        [
            journal_line(started, json.dumps(legacy)),
            journal_line(
                started + timedelta(seconds=10),
                json.dumps(legacy_failure),
            ),
            journal_line(
                started + timedelta(seconds=30),
                shadow_message(679, sequence=94_284),
            ),
            journal_line(
                started + timedelta(seconds=60),
                shadow_message(680, sequence=94_285),
            ),
        ],
        model=MODEL,
        minimum_window_seconds=30,
        maximum_gap_seconds=31,
    )

    assert report["success"] is True
    assert report["retry_audit_contract"] == "a6-provider-shadow-retry-audit-v1"
    assert report["window_boundary_source"] == "retry_contract"
    assert report["window_not_before"] == (started + timedelta(seconds=30)).isoformat()
    assert report["window_start"] == (started + timedelta(seconds=30)).isoformat()
    assert report["ignored_pre_boundary_record_count"] == 2
    assert report["observation_count"] == 2


def test_explicit_window_boundary_rejects_legacy_records_after_boundary() -> None:
    started = datetime(2026, 7, 29, 3, 32, tzinfo=timezone.utc)
    legacy = json.loads(shadow_message(679, sequence=94_284))
    del legacy["attempt_count"]
    boundary = started + timedelta(seconds=15)

    report = audit_journal_lines(
        [
            journal_line(started, json.dumps(legacy)),
            journal_line(started + timedelta(seconds=30), json.dumps(legacy)),
            journal_line(
                started + timedelta(seconds=60),
                shadow_message(680, sequence=94_285),
            ),
        ],
        model=MODEL,
        minimum_window_seconds=30,
        maximum_gap_seconds=31,
        not_before=boundary,
    )

    assert report["success"] is False
    assert "line_2:shadow_attempt_count_invalid" in report["errors"]
    assert report["window_boundary_source"] == "explicit"
    assert report["window_not_before"] == boundary.isoformat()
    assert report["ignored_pre_boundary_record_count"] == 1


def test_failed_retry_audit_requires_type_code_and_attempt_count() -> None:
    started = datetime(2026, 7, 29, 3, 32, tzinfo=timezone.utc)
    failure = json.dumps(
        {
            "success": False,
            "mode": "shadow",
            "model": MODEL,
            "activated": False,
            "plan_write_count": 0,
            "remote_write_count": 0,
            "attempt_count": 3,
        }
    )

    report = audit_journal_lines(
        [
            journal_line(started, shadow_message(679, sequence=94_284)),
            journal_line(started + timedelta(seconds=30), failure),
            journal_line(
                started + timedelta(seconds=60),
                shadow_message(680, sequence=94_285),
            ),
        ],
        model=MODEL,
        minimum_window_seconds=60,
        maximum_gap_seconds=61,
    )

    assert report["success"] is False
    assert "line_2:shadow_failure_type_invalid" in report["errors"]
    assert "line_2:shadow_failure_code_invalid" in report["errors"]
    assert "line_2:shadow_run_failed" in report["errors"]


def test_remote_write_and_service_timeout_fail_the_window() -> None:
    started = datetime(2026, 7, 29, 3, 32, tzinfo=timezone.utc)
    lines = [
        journal_line(started, shadow_message(679, sequence=94_284)),
        journal_line(
            started + timedelta(seconds=30),
            "a6-trader-provider-pool-shadow.service: start operation timed out. Terminating.",
        ),
        journal_line(
            started + timedelta(seconds=60),
            shadow_message(680, sequence=94_300, remote_writes=1),
        ),
    ]

    report = audit_journal_lines(
        lines,
        model=MODEL,
        minimum_window_seconds=60,
        maximum_gap_seconds=61,
    )

    assert report["success"] is False
    assert "line_3:shadow_remote_write_observed" in report["errors"]
    assert "shadow_service_failure_observed" in report["errors"]


def test_generation_regression_gap_and_short_window_are_reported() -> None:
    started = datetime(2026, 7, 29, 3, 32, tzinfo=timezone.utc)
    lines = [
        journal_line(started, shadow_message(680, sequence=94_300)),
        journal_line(
            started + timedelta(seconds=90),
            shadow_message(679, sequence=94_299),
        ),
    ]

    report = audit_journal_lines(
        lines,
        model=MODEL,
        minimum_window_seconds=120,
        maximum_gap_seconds=60,
    )

    assert report["success"] is False
    assert "shadow_window_too_short" in report["errors"]
    assert "shadow_observation_gap_exceeded" in report["errors"]
    assert "shadow_routing_generation_regressed" in report["errors"]
    assert "shadow_cost_generation_regressed" in report["errors"]
    assert "shadow_snapshot_sequence_regressed" in report["errors"]


def test_inventory_projection_is_required_after_v62() -> None:
    started = datetime(2026, 7, 29, 3, 32, tzinfo=timezone.utc)
    payload = json.loads(shadow_message(679, sequence=94_284))
    del payload["inventory_projection"]
    lines = [
        journal_line(started, json.dumps(payload)),
        journal_line(
            started + timedelta(seconds=30),
            shadow_message(680, sequence=94_300),
        ),
    ]

    report = audit_journal_lines(
        lines,
        model=MODEL,
        minimum_window_seconds=30,
        maximum_gap_seconds=31,
    )

    assert report["success"] is False
    assert "line_1:inventory_projection_missing" in report["errors"]
    assert "shadow_observation_count_insufficient" in report["errors"]


@pytest.mark.parametrize(
    ("tamper", "expected_error"),
    (
        ("schema", "line_1:shadow_evidence_schema_invalid"),
        ("snapshot_incomplete", "line_1:shadow_snapshot_incomplete"),
        ("snapshot_hash", "line_1:shadow_snapshot_result_hash_invalid"),
        ("snapshot_unverified", "line_1:shadow_snapshot_result_hash_unverified"),
        ("source_hash", "line_1:shadow_source_hash_invalid"),
        ("top_digest", "line_1:shadow_top_identity_digest_mismatch"),
        ("owned_overlap", "line_1:shadow_top_owned_overlap"),
        ("cost_input", "line_1:shadow_cost_input_hash_mismatch"),
        ("cost_output", "line_1:shadow_cost_output_hash_mismatch"),
        ("coverage", "line_1:shadow_actual_cost_coverage_unproven"),
        (
            "protected_flag",
            "line_1:shadow_protected_floor_coverage_mismatch",
        ),
        ("semantic_input", "line_1:shadow_semantic_input_hash_mismatch"),
        ("semantic_output", "line_1:shadow_semantic_output_hash_mismatch"),
    ),
)
def test_shadow_evidence_tampering_has_stable_error_codes(
    tamper: str,
    expected_error: str,
) -> None:
    started = datetime(2026, 7, 29, 3, 32, tzinfo=timezone.utc)
    payload = json.loads(shadow_message(679, sequence=94_284))
    evidence = payload["shadow_evidence"]
    if tamper == "schema":
        evidence["schema"] = "future"
    elif tamper == "snapshot_incomplete":
        evidence["snapshot"]["pages_complete"] = False
    elif tamper == "snapshot_hash":
        evidence["snapshot"]["result_hash"] = "not-a-hash"
    elif tamper == "snapshot_unverified":
        evidence["snapshot"]["result_hash_verified"] = False
    elif tamper == "source_hash":
        evidence["snapshot"]["source_hash"] = "not-a-hash"
    elif tamper == "top_digest":
        evidence["selection"]["top_identity_digest"] = "f" * 64
    elif tamper == "owned_overlap":
        owned = evidence["selection"]["owned"]
        owned["channel_ids"] = [10]
        owned["identity_digest"] = canonical_sha256(
            {
                "supplier_ids": owned["supplier_ids"],
                "channel_ids": owned["channel_ids"],
                "listing_ids": owned["listing_ids"],
            }
        )
        evidence["selection"]["top_owned_overlap_count"] = 1
    elif tamper == "cost_input":
        evidence["cost"]["input"]["ordered_channels"][0]["price"] = "0.5"
    elif tamper == "cost_output":
        evidence["cost"]["output"]["pool_cost_multiplier"] = "0.7"
    elif tamper == "coverage":
        coverage = evidence["cost"]["coverage"]
        coverage["realized_cost_sample_count"] = 99
        coverage["actual_cost_coverage_state"] = "insufficient_or_uncovered"
        coverage["realized_cost_covered"] = False
    elif tamper == "protected_flag":
        evidence["cost"]["coverage"]["protected_floor_covered"] = False
    elif tamper == "semantic_input":
        evidence["semantic_stability"]["input"]["config_version"] = "changed"
    elif tamper == "semantic_output":
        evidence["semantic_stability"]["output_hash"] = "f" * 64
    else:  # pragma: no cover - exhaustive fixture guard
        raise AssertionError(tamper)

    report = audit_journal_lines(
        [
            journal_line(started, json.dumps(payload)),
            journal_line(
                started + timedelta(seconds=30),
                shadow_message(680, sequence=94_285),
            ),
        ],
        model=MODEL,
        minimum_window_seconds=30,
        maximum_gap_seconds=31,
    )

    assert report["success"] is False
    assert expected_error in report["errors"]


@pytest.mark.parametrize(
    ("tamper", "expected_error"),
    (
        ("schema", "line_1:pool_size_evidence_schema_invalid"),
        ("input_hash", "line_1:pool_size_input_hash_mismatch"),
        ("output_hash", "line_1:pool_size_output_hash_mismatch"),
        ("decision", "line_1:pool_size_decision_mismatch"),
        ("liquidity", "line_1:pool_size_input_contract_invalid"),
        ("guard_window", "line_1:pool_size_input_contract_invalid"),
    ),
)
def test_pool_size_evidence_tampering_has_stable_error_codes(
    tamper: str,
    expected_error: str,
) -> None:
    started = datetime(2026, 7, 29, 3, 32, tzinfo=timezone.utc)
    payload = json.loads(shadow_message(679, sequence=94_284))
    sizing = payload["pool_size_evidence"]
    if tamper == "schema":
        sizing["schema"] = "future"
    elif tamper == "input_hash":
        sizing["input"]["p99_concurrency"] = 2
    elif tamper == "output_hash":
        sizing["output_hash"] = "f" * 64
    elif tamper == "decision":
        sizing["output"] = {"desired_size": 2, "action": "expand"}
        sizing["output_hash"] = canonical_sha256(sizing["output"])
        payload["desired_size"] = 2
    elif tamper == "liquidity":
        sizing["input"]["liquidity_runway_evidence_complete"] = False
        sizing["input"]["liquidity_runway_capacity_sufficient"] = False
        sizing["input"]["providers_needed_for_24h_liquidity_runway"] = 0
        sizing["input_hash"] = canonical_sha256(sizing["input"])
    elif tamper == "guard_window":
        sizing["input"]["minimum_observation_seconds"] = 1
        sizing["input_hash"] = canonical_sha256(sizing["input"])
    else:  # pragma: no cover - exhaustive fixture guard
        raise AssertionError(tamper)

    report = audit_journal_lines(
        [
            journal_line(started, json.dumps(payload)),
            journal_line(
                started + timedelta(seconds=30),
                shadow_message(680, sequence=94_285),
            ),
        ],
        model=MODEL,
        minimum_window_seconds=30,
        maximum_gap_seconds=31,
    )

    assert report["success"] is False
    assert expected_error in report["errors"]


def test_observation_without_pool_size_evidence_is_excluded() -> None:
    started = datetime(2026, 7, 29, 3, 32, tzinfo=timezone.utc)
    payload = json.loads(shadow_message(679, sequence=94_284))
    del payload["pool_size_evidence"]

    report = audit_journal_lines(
        [journal_line(started, json.dumps(payload))],
        model=MODEL,
        minimum_window_seconds=30,
        maximum_gap_seconds=31,
    )

    assert "line_1:pool_size_evidence_missing" in report["errors"]
    assert report["observation_count"] == 0


def test_failed_run_does_not_report_success_identity_errors() -> None:
    started = datetime(2026, 7, 29, 3, 32, tzinfo=timezone.utc)
    failure = json.dumps(
        {
            "success": False,
            "mode": "shadow",
            "model": MODEL,
            "activated": False,
            "plan_write_count": 0,
            "remote_write_count": 0,
            "failure_type": "ConnectionTimeout",
            "failure_code": "ConnectionTimeout",
            "attempt_count": 1,
        }
    )

    report = audit_journal_lines(
        [
            journal_line(started, shadow_message(679, sequence=94_284)),
            journal_line(started + timedelta(seconds=30), failure),
            journal_line(
                started + timedelta(seconds=60),
                shadow_message(680, sequence=94_285),
            ),
        ],
        model=MODEL,
        minimum_window_seconds=60,
        maximum_gap_seconds=61,
    )

    assert "line_2:shadow_run_failed" in report["errors"]
    assert "line_2:shadow_identity_invalid" not in report["errors"]
    assert "line_2:shadow_evidence_missing" not in report["errors"]
    assert report["observation_count"] == 2


def test_legacy_observation_does_not_enter_v1_evidence_window() -> None:
    started = datetime(2026, 7, 29, 3, 32, tzinfo=timezone.utc)
    payload = json.loads(shadow_message(679, sequence=94_284))
    del payload["shadow_evidence"]

    report = audit_journal_lines(
        [journal_line(started, json.dumps(payload))],
        model=MODEL,
        minimum_window_seconds=30,
        maximum_gap_seconds=31,
    )

    assert "line_1:shadow_evidence_missing" in report["errors"]
    assert report["observation_count"] == 0


def test_unbounded_timestamp_and_decimal_evidence_return_codes_instead_of_crashing() -> None:
    payload = json.loads(shadow_message(679, sequence=94_284))
    cost = payload["shadow_evidence"]["cost"]
    semantic = payload["shadow_evidence"]["semantic_stability"]
    cost["output"]["quantum"] = "1e-999999999"
    cost["output_hash"] = canonical_sha256(cost["output"])
    semantic["input"]["quantum"] = "1e-999999999"
    semantic["input_hash"] = canonical_sha256(semantic["input"])
    semantic["output_hash"] = cost["output_hash"]

    report = audit_journal_lines(
        [
            json.dumps(
                {
                    "__REALTIME_TIMESTAMP": "9" * 100,
                    "MESSAGE": shadow_message(679, sequence=94_284),
                }
            ),
            journal_line(
                datetime(2026, 7, 29, 3, 32, tzinfo=timezone.utc),
                json.dumps(payload),
            ),
        ],
        model=MODEL,
        minimum_window_seconds=30,
        maximum_gap_seconds=31,
    )

    assert "line_1:journal_timestamp_invalid" in report["errors"]
    assert "line_2:shadow_cost_output_contract_invalid" in report["errors"]
