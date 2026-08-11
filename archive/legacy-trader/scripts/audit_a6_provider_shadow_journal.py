#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import ROUND_CEILING, Decimal, DecimalException
from pathlib import Path
from typing import Any, TextIO

from a6api_auto.trade_provider_inventory import next_desired_pool_size
from a6api_auto.trade_provider_pool_shadow_evidence import (
    SHADOW_EVIDENCE_SCHEMA,
    canonical_sha256,
    identity_digest,
)
from a6api_auto.trade_provider_pool_size_evidence import POOL_SIZE_EVIDENCE_SCHEMA

_FAILURE_MARKERS = (
    "failed with result",
    "failed to start",
    "main process exited",
    "start operation timed out",
)
_MAX_RETRY_ATTEMPTS = 10


class ShadowJournalAuditError(RuntimeError):
    pass


@dataclass(frozen=True)
class ShadowObservation:
    line_no: int
    observed_at: datetime
    generation: int
    cost_generation: int
    snapshot_sequence: int
    inventory_changed_count: int
    top_identity_digest: str
    standby_identity_digest: str
    semantic_input_hash: str
    semantic_output_hash: str
    pool_size_input_hash: str
    pool_size_output_hash: str


def audit_journal_lines(
    lines: Iterable[str],
    *,
    model: str,
    minimum_window_seconds: float = 24 * 60 * 60,
    maximum_gap_seconds: float = 120,
    require_inventory_projection: bool = True,
    not_before: datetime | None = None,
) -> dict[str, Any]:
    model = str(model or "").strip()
    if not model:
        raise ShadowJournalAuditError("shadow_journal_model_invalid")
    if minimum_window_seconds <= 0 or maximum_gap_seconds <= 0:
        raise ShadowJournalAuditError("shadow_journal_window_invalid")
    if not isinstance(require_inventory_projection, bool):
        raise ShadowJournalAuditError("shadow_journal_inventory_mode_invalid")
    explicit_not_before = _optional_aware_utc(
        not_before,
        "shadow_journal_not_before_invalid",
    )

    observations: list[ShadowObservation] = []
    service_failures: list[tuple[datetime, str]] = []
    errors: list[str] = []
    contract_not_before = explicit_not_before
    boundary_source = "explicit" if explicit_not_before is not None else None
    ignored_pre_boundary_records = 0
    for line_no, raw_line in enumerate(lines, start=1):
        line = str(raw_line).strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            errors.append(f"line_{line_no}:journal_json_invalid")
            continue
        if not isinstance(record, Mapping):
            errors.append(f"line_{line_no}:journal_record_invalid")
            continue
        timestamp = _journal_timestamp(record, line_no, errors)
        message = record.get("MESSAGE")
        if timestamp is None or not isinstance(message, str):
            if timestamp is not None:
                errors.append(f"line_{line_no}:journal_message_invalid")
            continue
        lowered = message.casefold()
        if any(marker in lowered for marker in _FAILURE_MARKERS):
            service_failures.append((timestamp, message[:256]))
        try:
            payload = json.loads(message)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, Mapping):
            continue
        if payload.get("mode") != "shadow" or payload.get("model") != model:
            continue
        if explicit_not_before is not None and timestamp < explicit_not_before:
            ignored_pre_boundary_records += 1
            continue
        retry_audit_present = "attempt_count" in payload
        if contract_not_before is None:
            if not retry_audit_present:
                ignored_pre_boundary_records += 1
                continue
            contract_not_before = timestamp
            boundary_source = "retry_contract"
        prefix = f"line_{line_no}"
        _validate_retry_audit_contract(payload, prefix, errors)
        _validate_shadow_result(payload, prefix, errors)
        if payload.get("success") is not True:
            # Failure summaries intentionally contain no generation/evidence
            # identity. The failed run is decisive without success-only noise.
            continue
        generation = _nonnegative_integer(payload.get("routing_generation"))
        cost_generation = _nonnegative_integer(payload.get("cost_generation"))
        snapshot_sequence = _nonnegative_integer(payload.get("snapshot_sequence"))
        if not generation or not cost_generation or not snapshot_sequence:
            errors.append(f"{prefix}:shadow_identity_invalid")
            continue
        evidence_identity = _validate_shadow_evidence(payload, prefix, errors)
        if evidence_identity is None:
            continue
        pool_size_identity = _validate_pool_size_evidence(payload, prefix, errors)
        if pool_size_identity is None:
            continue
        inventory_changed_count = 0
        inventory = payload.get("inventory_projection")
        if require_inventory_projection:
            if not isinstance(inventory, Mapping) or inventory.get("enabled") is False:
                errors.append(f"{prefix}:inventory_projection_missing")
                continue
            inventory_changed_count = _nonnegative_integer(inventory.get("changed_count"))
            if inventory_changed_count is None:
                errors.append(f"{prefix}:inventory_projection_invalid")
                continue
        observations.append(
            ShadowObservation(
                line_no,
                timestamp,
                generation,
                cost_generation,
                snapshot_sequence,
                inventory_changed_count,
                evidence_identity["top_identity_digest"],
                evidence_identity["standby_identity_digest"],
                evidence_identity["semantic_input_hash"],
                evidence_identity["semantic_output_hash"],
                pool_size_identity["input_hash"],
                pool_size_identity["output_hash"],
            )
        )

    if len(observations) < 2:
        if contract_not_before is None:
            errors.append("shadow_retry_audit_contract_missing")
        errors.append("shadow_observation_count_insufficient")
        return _report(
            observations,
            errors,
            [],
            minimum_window_seconds,
            maximum_gap_seconds,
            contract_not_before=contract_not_before,
            boundary_source=boundary_source,
            ignored_pre_boundary_records=ignored_pre_boundary_records,
        )

    observations.sort(key=lambda item: item.observed_at)
    first = observations[0]
    last = observations[-1]
    relevant_failures = [
        message for timestamp, message in service_failures if first.observed_at <= timestamp
    ]
    if relevant_failures:
        errors.append("shadow_service_failure_observed")
    span_seconds = (last.observed_at - first.observed_at).total_seconds()
    if span_seconds < minimum_window_seconds:
        errors.append("shadow_window_too_short")
    semantic_outputs: dict[str, str] = {}
    pool_size_outputs: dict[str, str] = {}
    for observation in observations:
        previous_output = semantic_outputs.setdefault(
            observation.semantic_input_hash,
            observation.semantic_output_hash,
        )
        if previous_output != observation.semantic_output_hash:
            errors.append(f"line_{observation.line_no}:shadow_semantic_output_unstable")
        previous_size_output = pool_size_outputs.setdefault(
            observation.pool_size_input_hash,
            observation.pool_size_output_hash,
        )
        if previous_size_output != observation.pool_size_output_hash:
            errors.append(f"line_{observation.line_no}:pool_size_semantic_output_unstable")
    for previous, current in zip(observations, observations[1:], strict=False):
        gap = (current.observed_at - previous.observed_at).total_seconds()
        if gap <= 0:
            errors.append("shadow_timestamp_not_monotonic")
        if gap > maximum_gap_seconds:
            errors.append("shadow_observation_gap_exceeded")
        if current.generation < previous.generation:
            errors.append("shadow_routing_generation_regressed")
        if current.cost_generation < previous.cost_generation:
            errors.append("shadow_cost_generation_regressed")
        if current.snapshot_sequence < previous.snapshot_sequence:
            errors.append("shadow_snapshot_sequence_regressed")
    return _report(
        observations,
        errors,
        relevant_failures,
        minimum_window_seconds,
        maximum_gap_seconds,
        contract_not_before=contract_not_before,
        boundary_source=boundary_source,
        ignored_pre_boundary_records=ignored_pre_boundary_records,
    )


def _journal_timestamp(
    record: Mapping[str, Any],
    line_no: int,
    errors: list[str],
) -> datetime | None:
    raw = record.get("__REALTIME_TIMESTAMP")
    try:
        micros = int(str(raw))
    except (TypeError, ValueError):
        errors.append(f"line_{line_no}:journal_timestamp_invalid")
        return None
    if micros < 1:
        errors.append(f"line_{line_no}:journal_timestamp_invalid")
        return None
    try:
        return datetime.fromtimestamp(micros / 1_000_000, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        errors.append(f"line_{line_no}:journal_timestamp_invalid")
        return None


def _validate_shadow_result(
    payload: Mapping[str, Any],
    prefix: str,
    errors: list[str],
) -> None:
    if payload.get("success") is not True:
        errors.append(f"{prefix}:shadow_run_failed")
    if payload.get("activated") is not False:
        errors.append(f"{prefix}:shadow_activation_observed")
    if _nonnegative_integer(payload.get("remote_write_count")) != 0:
        errors.append(f"{prefix}:shadow_remote_write_observed")
    if _nonnegative_integer(payload.get("plan_write_count")) not in {0, 1}:
        errors.append(f"{prefix}:shadow_plan_write_count_invalid")


def _validate_retry_audit_contract(
    payload: Mapping[str, Any],
    prefix: str,
    errors: list[str],
) -> None:
    attempt_count = _json_positive_integer(payload.get("attempt_count"))
    if attempt_count is None or attempt_count > _MAX_RETRY_ATTEMPTS:
        errors.append(f"{prefix}:shadow_attempt_count_invalid")
    success = payload.get("success")
    if success is True:
        if "failure_code" in payload or "failure_type" in payload:
            errors.append(f"{prefix}:shadow_success_failure_audit_present")
        return
    if not _audit_identifier(payload.get("failure_type")):
        errors.append(f"{prefix}:shadow_failure_type_invalid")
    if not _audit_identifier(payload.get("failure_code")):
        errors.append(f"{prefix}:shadow_failure_code_invalid")


def _validate_shadow_evidence(
    payload: Mapping[str, Any],
    prefix: str,
    errors: list[str],
) -> dict[str, str] | None:
    initial_error_count = len(errors)
    evidence = payload.get("shadow_evidence")
    if not isinstance(evidence, Mapping):
        errors.append(f"{prefix}:shadow_evidence_missing")
        return None
    if set(evidence) != {
        "schema",
        "snapshot",
        "selection",
        "cost",
        "semantic_stability",
    }:
        errors.append(f"{prefix}:shadow_evidence_contract_invalid")
    if evidence.get("schema") != SHADOW_EVIDENCE_SCHEMA:
        errors.append(f"{prefix}:shadow_evidence_schema_invalid")

    snapshot = evidence.get("snapshot")
    if not isinstance(snapshot, Mapping):
        errors.append(f"{prefix}:shadow_snapshot_evidence_invalid")
    else:
        _validate_snapshot_evidence(snapshot, payload, prefix, errors)

    selection = evidence.get("selection")
    top: list[dict[str, int | None]] | None = None
    standby: list[dict[str, int | None]] | None = None
    top_digest = ""
    standby_digest = ""
    if not isinstance(selection, Mapping):
        errors.append(f"{prefix}:shadow_selection_evidence_invalid")
    else:
        top = _identity_rows(selection.get("top"))
        standby = _identity_rows(selection.get("standby"))
        if top is None:
            errors.append(f"{prefix}:shadow_top_identity_invalid")
        if standby is None:
            errors.append(f"{prefix}:shadow_standby_identity_invalid")
        if top is not None and standby is not None:
            top_digest, standby_digest = _validate_selection_evidence(
                selection,
                top,
                standby,
                snapshot if isinstance(snapshot, Mapping) else {},
                payload,
                prefix,
                errors,
            )

    cost = evidence.get("cost")
    semantic = evidence.get("semantic_stability")
    cost_input_hash_value = ""
    cost_output_hash_value = ""
    if not isinstance(cost, Mapping):
        errors.append(f"{prefix}:shadow_cost_evidence_invalid")
    else:
        cost_input_hash_value, cost_output_hash_value = _validate_cost_evidence(
            cost,
            semantic,
            payload,
            top or [],
            prefix,
            errors,
        )

    semantic_input_hash = ""
    semantic_output_hash = ""
    if not isinstance(semantic, Mapping):
        errors.append(f"{prefix}:shadow_semantic_evidence_invalid")
    else:
        semantic_input = semantic.get("input")
        if set(semantic) != {"input", "input_hash", "output_hash"}:
            errors.append(f"{prefix}:shadow_semantic_evidence_invalid")
        if not isinstance(semantic_input, Mapping):
            errors.append(f"{prefix}:shadow_semantic_input_invalid")
        else:
            if set(semantic_input) != {
                "model",
                "config_version",
                "cost_input_hash",
                "previous_billing_floor_multiplier",
                "conservative_fallback_cost",
                "quantum",
            }:
                errors.append(f"{prefix}:shadow_semantic_input_contract_invalid")
            semantic_input_hash = str(semantic.get("input_hash") or "")
            expected_semantic_hash = canonical_sha256(dict(semantic_input))
            if not _is_sha256(semantic_input_hash) or semantic_input_hash != expected_semantic_hash:
                errors.append(f"{prefix}:shadow_semantic_input_hash_mismatch")
            if semantic_input.get("model") != payload.get("model"):
                errors.append(f"{prefix}:shadow_semantic_input_contract_invalid")
            if semantic_input.get("cost_input_hash") != cost_input_hash_value:
                errors.append(f"{prefix}:shadow_semantic_input_contract_invalid")
            semantic_output_hash = str(semantic.get("output_hash") or "")
            if (
                not _is_sha256(semantic_output_hash)
                or semantic_output_hash != cost_output_hash_value
            ):
                errors.append(f"{prefix}:shadow_semantic_output_hash_mismatch")

    if len(errors) != initial_error_count:
        return None
    return {
        "top_identity_digest": top_digest,
        "standby_identity_digest": standby_digest,
        "semantic_input_hash": semantic_input_hash,
        "semantic_output_hash": semantic_output_hash,
    }


def _validate_pool_size_evidence(
    payload: Mapping[str, Any],
    prefix: str,
    errors: list[str],
) -> dict[str, str] | None:
    initial_error_count = len(errors)
    evidence = payload.get("pool_size_evidence")
    if not isinstance(evidence, Mapping):
        errors.append(f"{prefix}:pool_size_evidence_missing")
        return None
    if set(evidence) != {"schema", "input", "input_hash", "output", "output_hash"}:
        errors.append(f"{prefix}:pool_size_evidence_contract_invalid")
    if evidence.get("schema") != POOL_SIZE_EVIDENCE_SCHEMA:
        errors.append(f"{prefix}:pool_size_evidence_schema_invalid")

    input_value = evidence.get("input")
    input_hash = str(evidence.get("input_hash") or "")
    if not isinstance(input_value, Mapping):
        errors.append(f"{prefix}:pool_size_input_invalid")
        return None
    if set(input_value) != {
        "size_mode",
        "current_desired_size",
        "funded_eligible_provider_count",
        "decision_at",
        "observation_started_at",
        "last_shrink_at",
        "p99_concurrency",
        "safe_concurrency_per_slot",
        "target_utilization",
        "p99_simultaneous_failed_slots",
        "providers_needed_for_24h_liquidity_runway",
        "liquidity_runway_evidence_complete",
        "liquidity_runway_capacity_sufficient",
        "minimum_active_slots",
        "initial_max_active_slots",
        "minimum_observation_seconds",
        "minimum_shrink_interval_seconds",
    }:
        errors.append(f"{prefix}:pool_size_input_contract_invalid")
    if not _is_sha256(input_hash) or input_hash != canonical_sha256(dict(input_value)):
        errors.append(f"{prefix}:pool_size_input_hash_mismatch")

    mode = input_value.get("size_mode")
    current_size = _json_positive_integer(input_value.get("current_desired_size"))
    funded_count = _json_nonnegative_integer(input_value.get("funded_eligible_provider_count"))
    decision_at = _aware_iso_value(input_value.get("decision_at"))
    observation_started_at = _aware_iso_value(input_value.get("observation_started_at"))
    raw_last_shrink = input_value.get("last_shrink_at")
    last_shrink_at = None if raw_last_shrink is None else _aware_iso_value(raw_last_shrink)
    p99_concurrency = _json_nonnegative_integer(input_value.get("p99_concurrency"))
    safe_concurrency = _json_positive_integer(input_value.get("safe_concurrency_per_slot"))
    utilization = _positive_decimal_value(input_value.get("target_utilization"))
    p99_failed_slots = _json_nonnegative_integer(input_value.get("p99_simultaneous_failed_slots"))
    liquidity_needed = _json_nonnegative_integer(
        input_value.get("providers_needed_for_24h_liquidity_runway")
    )
    liquidity_complete = input_value.get("liquidity_runway_evidence_complete")
    liquidity_sufficient = input_value.get("liquidity_runway_capacity_sufficient")
    minimum_slots = _json_positive_integer(input_value.get("minimum_active_slots"))
    maximum_slots = _json_positive_integer(input_value.get("initial_max_active_slots"))
    minimum_observation_seconds = _json_positive_integer(
        input_value.get("minimum_observation_seconds")
    )
    minimum_shrink_interval_seconds = _json_positive_integer(
        input_value.get("minimum_shrink_interval_seconds")
    )
    contract_valid = bool(
        mode in {"all_funded", "evidence_based"}
        and current_size is not None
        and funded_count is not None
        and decision_at is not None
        and observation_started_at is not None
        and (raw_last_shrink is None or last_shrink_at is not None)
        and p99_concurrency is not None
        and safe_concurrency is not None
        and utilization is not None
        and utilization <= 1
        and p99_failed_slots is not None
        and liquidity_needed is not None
        and isinstance(liquidity_complete, bool)
        and isinstance(liquidity_sufficient, bool)
        and minimum_slots is not None
        and maximum_slots is not None
        and minimum_slots <= maximum_slots
        and minimum_observation_seconds is not None
        and minimum_observation_seconds >= 14 * 24 * 60 * 60
        and minimum_shrink_interval_seconds is not None
        and minimum_shrink_interval_seconds >= 72 * 60 * 60
    )
    if contract_valid:
        contract_valid = bool(
            observation_started_at <= decision_at
            and (last_shrink_at is None or observation_started_at <= last_shrink_at <= decision_at)
            and (not liquidity_sufficient or liquidity_complete)
            and (liquidity_complete and liquidity_sufficient or liquidity_needed >= current_size)
        )
    if not contract_valid:
        errors.append(f"{prefix}:pool_size_input_contract_invalid")

    output_value = evidence.get("output")
    output_hash = str(evidence.get("output_hash") or "")
    desired_size: int | None = None
    if not isinstance(output_value, Mapping) or set(output_value) != {
        "desired_size",
        "action",
    }:
        errors.append(f"{prefix}:pool_size_output_contract_invalid")
    else:
        desired_size = _json_positive_integer(output_value.get("desired_size"))
        if desired_size is None or output_value.get("action") not in {
            "expand",
            "hold",
            "shrink",
        }:
            errors.append(f"{prefix}:pool_size_output_contract_invalid")
        if (
            _json_positive_integer(payload.get("desired_size")) != desired_size
            or current_size is None
            or output_value.get("action")
            != (
                "expand"
                if desired_size is not None and desired_size > current_size
                else "shrink"
                if desired_size is not None and desired_size < current_size
                else "hold"
            )
        ):
            errors.append(f"{prefix}:pool_size_output_contract_invalid")
        if not _is_sha256(output_hash) or output_hash != canonical_sha256(dict(output_value)):
            errors.append(f"{prefix}:pool_size_output_hash_mismatch")

    if contract_valid and desired_size is not None:
        try:
            expected = next_desired_pool_size(
                size_mode=str(mode),
                current_desired_size=current_size,
                funded_eligible_provider_count=funded_count,
                now=decision_at,
                observation_started_at=observation_started_at,
                last_shrink_at=last_shrink_at,
                p99_concurrency=p99_concurrency,
                safe_concurrency_per_slot=safe_concurrency,
                target_utilization=utilization,
                p99_simultaneous_failed_slots=p99_failed_slots,
                providers_needed_for_24h_liquidity_runway=liquidity_needed,
                minimum_active_slots=minimum_slots,
                initial_max_active_slots=maximum_slots,
                minimum_observation=timedelta(seconds=minimum_observation_seconds),
                minimum_shrink_interval=timedelta(seconds=minimum_shrink_interval_seconds),
            )
        except (ArithmeticError, TypeError, ValueError):
            errors.append(f"{prefix}:pool_size_decision_contract_invalid")
        else:
            if desired_size != expected:
                errors.append(f"{prefix}:pool_size_decision_mismatch")

    if len(errors) != initial_error_count:
        return None
    return {"input_hash": input_hash, "output_hash": output_hash}


def _validate_snapshot_evidence(
    snapshot: Mapping[str, Any],
    payload: Mapping[str, Any],
    prefix: str,
    errors: list[str],
) -> None:
    if set(snapshot) != {
        "status",
        "pages_complete",
        "candidate_count",
        "result_hash",
        "result_hash_verified",
        "source_hash",
    }:
        errors.append(f"{prefix}:shadow_snapshot_evidence_invalid")
    if snapshot.get("status") != "completed" or snapshot.get("pages_complete") is not True:
        errors.append(f"{prefix}:shadow_snapshot_incomplete")
    candidate_count = _json_nonnegative_integer(snapshot.get("candidate_count"))
    summary_count = _json_nonnegative_integer(payload.get("snapshot_candidate_count"))
    if candidate_count is None or summary_count is None or candidate_count != summary_count:
        errors.append(f"{prefix}:shadow_snapshot_candidate_count_mismatch")
    if not _is_sha256(snapshot.get("result_hash")):
        errors.append(f"{prefix}:shadow_snapshot_result_hash_invalid")
    if snapshot.get("result_hash_verified") is not True:
        errors.append(f"{prefix}:shadow_snapshot_result_hash_unverified")
    if not _is_sha256(snapshot.get("source_hash")):
        errors.append(f"{prefix}:shadow_source_hash_invalid")


def _validate_selection_evidence(
    selection: Mapping[str, Any],
    top: list[dict[str, int | None]],
    standby: list[dict[str, int | None]],
    snapshot: Mapping[str, Any],
    payload: Mapping[str, Any],
    prefix: str,
    errors: list[str],
) -> tuple[str, str]:
    if set(selection) != {
        "top",
        "standby",
        "top_identity_digest",
        "standby_identity_digest",
        "owned",
        "top_owned_overlap_count",
        "standby_owned_overlap_count",
    }:
        errors.append(f"{prefix}:shadow_selection_evidence_invalid")
    top_digest = str(selection.get("top_identity_digest") or "")
    standby_digest = str(selection.get("standby_identity_digest") or "")
    if not _is_sha256(top_digest) or top_digest != identity_digest("top", top):
        errors.append(f"{prefix}:shadow_top_identity_digest_mismatch")
    if not _is_sha256(standby_digest) or standby_digest != identity_digest("standby", standby):
        errors.append(f"{prefix}:shadow_standby_identity_digest_mismatch")

    top_ids = [identity["channel_id"] for identity in top]
    standby_ids = [identity["channel_id"] for identity in standby]
    if (
        len(top_ids) != len(set(top_ids))
        or len(standby_ids) != len(set(standby_ids))
        or set(top_ids) & set(standby_ids)
    ):
        errors.append(f"{prefix}:shadow_selection_identity_duplicate")
    if _json_nonnegative_integer(payload.get("top_count")) != len(top):
        errors.append(f"{prefix}:shadow_top_count_mismatch")
    if _json_nonnegative_integer(payload.get("standby_count")) != len(standby):
        errors.append(f"{prefix}:shadow_standby_count_mismatch")
    candidate_count = _json_nonnegative_integer(snapshot.get("candidate_count"))
    eligible_count = _json_nonnegative_integer(payload.get("eligible_count"))
    if (
        candidate_count is None
        or eligible_count is None
        or eligible_count > candidate_count
        or len(top) + len(standby) > eligible_count
    ):
        errors.append(f"{prefix}:shadow_selection_count_invalid")

    owned = selection.get("owned")
    if not isinstance(owned, Mapping):
        errors.append(f"{prefix}:shadow_owned_identity_invalid")
        return top_digest, standby_digest
    if set(owned) != {
        "supplier_ids",
        "channel_ids",
        "listing_ids",
        "identity_digest",
    }:
        errors.append(f"{prefix}:shadow_owned_identity_invalid")
        return top_digest, standby_digest
    owned_ids: dict[str, list[int]] = {}
    for name in ("supplier_ids", "channel_ids", "listing_ids"):
        values = _positive_integer_list(owned.get(name))
        if values is None or values != sorted(set(values)):
            errors.append(f"{prefix}:shadow_owned_identity_invalid")
            return top_digest, standby_digest
        owned_ids[name] = values
    expected_owned_digest = canonical_sha256(owned_ids)
    if owned.get("identity_digest") != expected_owned_digest:
        errors.append(f"{prefix}:shadow_owned_identity_digest_mismatch")

    top_overlap = _owned_overlap(top, owned_ids)
    standby_overlap = _owned_overlap(standby, owned_ids)
    if _json_nonnegative_integer(selection.get("top_owned_overlap_count")) != top_overlap:
        errors.append(f"{prefix}:shadow_top_owned_overlap_count_mismatch")
    if _json_nonnegative_integer(selection.get("standby_owned_overlap_count")) != standby_overlap:
        errors.append(f"{prefix}:shadow_standby_owned_overlap_count_mismatch")
    if top_overlap:
        errors.append(f"{prefix}:shadow_top_owned_overlap")
    if standby_overlap:
        errors.append(f"{prefix}:shadow_standby_owned_overlap")
    return top_digest, standby_digest


def _validate_cost_evidence(
    cost: Mapping[str, Any],
    semantic: object,
    payload: Mapping[str, Any],
    top: list[dict[str, int | None]],
    prefix: str,
    errors: list[str],
) -> tuple[str, str]:
    if set(cost) != {"input", "input_hash", "output", "output_hash", "coverage"}:
        errors.append(f"{prefix}:shadow_cost_evidence_invalid")
    cost_input = cost.get("input")
    input_hash = str(cost.get("input_hash") or "")
    if not isinstance(cost_input, Mapping):
        errors.append(f"{prefix}:shadow_cost_input_invalid")
    else:
        expected_input_hash = canonical_sha256(dict(cost_input))
        if not _is_sha256(input_hash) or input_hash != expected_input_hash:
            errors.append(f"{prefix}:shadow_cost_input_hash_mismatch")
        _validate_cost_input_contract(cost_input, payload, top, prefix, errors)

    cost_output = cost.get("output")
    output_hash = str(cost.get("output_hash") or "")
    if not isinstance(cost_output, Mapping):
        errors.append(f"{prefix}:shadow_cost_output_invalid")
    else:
        expected_output_hash = canonical_sha256(dict(cost_output))
        if not _is_sha256(output_hash) or output_hash != expected_output_hash:
            errors.append(f"{prefix}:shadow_cost_output_hash_mismatch")

    if (
        isinstance(cost_input, Mapping)
        and isinstance(cost_output, Mapping)
        and isinstance(semantic, Mapping)
        and isinstance(semantic.get("input"), Mapping)
    ):
        _validate_cost_plan_contract(
            cost_input,
            cost_output,
            cost.get("coverage"),
            semantic["input"],
            prefix,
            errors,
        )
    return input_hash, output_hash


def _validate_cost_input_contract(
    cost_input: Mapping[str, Any],
    payload: Mapping[str, Any],
    top: list[dict[str, int | None]],
    prefix: str,
    errors: list[str],
) -> None:
    valid = set(cost_input) == {
        "model",
        "ordered_channels",
        "assignments",
        "ready_slot_nos",
        "token_mix_version",
        "exchange_rate_version",
        "retry_reserve_window",
        "retry_reserve_value",
        "realized_cost_window",
        "realized_cost_p95_value",
        "margin_bps",
        "headroom_bps",
        "quantization_version",
    }
    valid = valid and cost_input.get("model") == payload.get("model")
    ordered = cost_input.get("ordered_channels")
    ordered_ids: list[int] = []
    if isinstance(ordered, list):
        for row in ordered:
            if not isinstance(row, Mapping) or set(row) != {
                "channel_id",
                "price",
                "final_multiplier",
            }:
                valid = False
                break
            channel_id = _json_positive_integer(row.get("channel_id"))
            if channel_id is None:
                valid = False
                break
            ordered_ids.append(channel_id)
            if _decimal_value(row.get("price")) is None:
                valid = False
            if (
                row.get("final_multiplier") is not None
                and _decimal_value(row.get("final_multiplier")) is None
            ):
                valid = False
    else:
        valid = False
    if ordered_ids != [identity["channel_id"] for identity in top]:
        valid = False

    assignments = cost_input.get("assignments")
    assignment_slots: list[int] = []
    if isinstance(assignments, list):
        for item in assignments:
            if not isinstance(item, list) or len(item) != 2:
                valid = False
                break
            slot = _json_positive_integer(item[0])
            assignment = item[1]
            if slot is None or not _assignment_valid(assignment):
                valid = False
                break
            assignment_slots.append(slot)
    else:
        valid = False
    if assignment_slots != sorted(set(assignment_slots)):
        valid = False
    ready_slots = _positive_integer_list(cost_input.get("ready_slot_nos"))
    if (
        ready_slots is None
        or ready_slots != sorted(set(ready_slots))
        or not set(ready_slots).issubset(assignment_slots)
    ):
        valid = False
    for name in (
        "token_mix_version",
        "exchange_rate_version",
        "retry_reserve_window",
        "realized_cost_window",
        "quantization_version",
    ):
        if not isinstance(cost_input.get(name), str) or not cost_input[name]:
            valid = False
    for name in ("retry_reserve_value",):
        if _decimal_value(cost_input.get(name)) is None:
            valid = False
    if (
        cost_input.get("realized_cost_p95_value") is not None
        and _decimal_value(cost_input.get("realized_cost_p95_value")) is None
    ):
        valid = False
    for name in ("margin_bps", "headroom_bps"):
        basis_points = _json_nonnegative_integer(cost_input.get(name))
        if basis_points is None or basis_points > 100_000:
            valid = False
    if not valid:
        errors.append(f"{prefix}:shadow_cost_input_contract_invalid")


def _validate_cost_plan_contract(
    cost_input: Mapping[str, Any],
    output: Mapping[str, Any],
    coverage_value: object,
    semantic_input: Mapping[str, Any],
    prefix: str,
    errors: list[str],
) -> None:
    required_output = {
        "desired_slot_costs",
        "route_cost_multiplier",
        "retry_reserve_multiplier",
        "realized_cost_p95_multiplier",
        "pool_cost_multiplier",
        "billing_floor_multiplier",
        "transition_floor_multiplier",
        "margin_bps",
        "headroom_bps",
        "quantum",
    }
    if set(output) != required_output:
        errors.append(f"{prefix}:shadow_cost_output_contract_invalid")
        return
    slot_costs = _slot_costs(output.get("desired_slot_costs"))
    route = _decimal_value(output.get("route_cost_multiplier"))
    retry = _decimal_value(output.get("retry_reserve_multiplier"))
    realized = _optional_decimal_value(output.get("realized_cost_p95_multiplier"))
    protected = _decimal_value(output.get("pool_cost_multiplier"))
    billing = _decimal_value(output.get("billing_floor_multiplier"))
    transition = _decimal_value(output.get("transition_floor_multiplier"))
    quantum = _positive_decimal_value(output.get("quantum"))
    previous_floor = _decimal_value(semantic_input.get("previous_billing_floor_multiplier"))
    fallback = _decimal_value(semantic_input.get("conservative_fallback_cost"))
    margin_bps = _json_nonnegative_integer(output.get("margin_bps"))
    headroom_bps = _json_nonnegative_integer(output.get("headroom_bps"))
    values = (
        slot_costs,
        route,
        retry,
        protected,
        billing,
        transition,
        quantum,
        previous_floor,
        fallback,
        margin_bps,
        headroom_bps,
    )
    if any(value is None for value in values):
        errors.append(f"{prefix}:shadow_cost_output_contract_invalid")
        return
    assert slot_costs is not None
    assert route is not None and retry is not None and protected is not None
    assert billing is not None and transition is not None and quantum is not None
    assert previous_floor is not None and fallback is not None
    assert margin_bps is not None and headroom_bps is not None
    if realized is _INVALID_DECIMAL or margin_bps > 100_000 or headroom_bps > 100_000:
        errors.append(f"{prefix}:shadow_cost_output_contract_invalid")
        return

    ready_slots = _positive_integer_list(cost_input.get("ready_slot_nos"))
    try:
        expected_route = max(max(slot_costs.values()), fallback) if slot_costs else Decimal("0")
        expected_protected = _ceil_to_step(route + retry, quantum)
        if isinstance(realized, Decimal):
            expected_protected = max(expected_protected, _ceil_to_step(realized, quantum))
        margin_factor = Decimal(1) + Decimal(margin_bps) / Decimal(10_000)
        headroom_factor = Decimal(1) + Decimal(headroom_bps) / Decimal(10_000)
        computed_floor = _ceil_to_step(
            protected * margin_factor * headroom_factor,
            quantum,
        )
        expected_billing = computed_floor if slot_costs else max(previous_floor, computed_floor)
        expected_transition = max(previous_floor, expected_billing)
        slot_costs_quantized = all(
            value == _ceil_to_step(value, quantum) for value in slot_costs.values()
        )
    except DecimalException:
        errors.append(f"{prefix}:shadow_cost_output_contract_invalid")
        return
    if (
        ready_slots is None
        or set(ready_slots) != set(slot_costs)
        or not slot_costs_quantized
        or route != expected_route
        or protected != expected_protected
        or billing != expected_billing
        or transition != expected_transition
        or retry != _decimal_value(cost_input.get("retry_reserve_value"))
        or realized != _optional_decimal_value(cost_input.get("realized_cost_p95_value"))
        or margin_bps != _json_nonnegative_integer(cost_input.get("margin_bps"))
        or headroom_bps != _json_nonnegative_integer(cost_input.get("headroom_bps"))
        or quantum != _positive_decimal_value(semantic_input.get("quantum"))
    ):
        errors.append(f"{prefix}:shadow_cost_output_contract_invalid")
        return
    _validate_cost_coverage(
        coverage_value,
        protected=protected,
        route=route,
        retry=retry,
        applied_realized=realized if isinstance(realized, Decimal) else None,
        billing=billing,
        transition=transition,
        previous_floor=previous_floor,
        prefix=prefix,
        errors=errors,
    )


def _validate_cost_coverage(
    coverage_value: object,
    *,
    protected: Decimal,
    route: Decimal,
    retry: Decimal,
    applied_realized: Decimal | None,
    billing: Decimal,
    transition: Decimal,
    previous_floor: Decimal,
    prefix: str,
    errors: list[str],
) -> None:
    if not isinstance(coverage_value, Mapping):
        errors.append(f"{prefix}:shadow_cost_coverage_invalid")
        return
    if set(coverage_value) != {
        "minimum_realized_cost_samples",
        "realized_cost_sample_count",
        "observed_realized_cost_p95_multiplier",
        "applied_realized_cost_p95_multiplier",
        "actual_cost_coverage_state",
        "route_plus_retry_covered",
        "realized_cost_covered",
        "protected_floor_covered",
    }:
        errors.append(f"{prefix}:shadow_cost_coverage_invalid")
        return
    minimum_samples = _json_positive_integer(coverage_value.get("minimum_realized_cost_samples"))
    sample_count = _json_nonnegative_integer(coverage_value.get("realized_cost_sample_count"))
    observed_realized = _optional_decimal_value(
        coverage_value.get("observed_realized_cost_p95_multiplier")
    )
    coverage_applied = _optional_decimal_value(
        coverage_value.get("applied_realized_cost_p95_multiplier")
    )
    if (
        minimum_samples is None
        or sample_count is None
        or observed_realized is _INVALID_DECIMAL
        or coverage_applied is _INVALID_DECIMAL
        or coverage_applied != applied_realized
    ):
        errors.append(f"{prefix}:shadow_cost_coverage_invalid")
        return
    actual_covered = bool(
        sample_count >= minimum_samples
        and isinstance(observed_realized, Decimal)
        and protected >= observed_realized
    )
    route_covered = protected >= route + retry
    protected_floor_covered = transition >= billing and transition >= previous_floor
    expected_state = "covered" if actual_covered else "insufficient_or_uncovered"
    if (
        coverage_value.get("actual_cost_coverage_state") != expected_state
        or coverage_value.get("realized_cost_covered") is not actual_covered
    ):
        errors.append(f"{prefix}:shadow_actual_cost_coverage_mismatch")
    if coverage_value.get("route_plus_retry_covered") is not route_covered:
        errors.append(f"{prefix}:shadow_route_retry_coverage_mismatch")
    if coverage_value.get("protected_floor_covered") is not protected_floor_covered:
        errors.append(f"{prefix}:shadow_protected_floor_coverage_mismatch")
    if not route_covered:
        errors.append(f"{prefix}:shadow_route_retry_uncovered")
    if not protected_floor_covered:
        errors.append(f"{prefix}:shadow_protected_floor_uncovered")
    if not actual_covered:
        errors.append(f"{prefix}:shadow_actual_cost_coverage_unproven")


_INVALID_DECIMAL = object()


def _identity_rows(value: object) -> list[dict[str, int | None]] | None:
    if not isinstance(value, list):
        return None
    identities: list[dict[str, int | None]] = []
    for item in value:
        if not isinstance(item, Mapping) or set(item) != {
            "channel_id",
            "supplier_id",
            "listing_id",
        }:
            return None
        channel_id = _json_positive_integer(item.get("channel_id"))
        supplier_id = _optional_positive_integer(item.get("supplier_id"))
        listing_id = _optional_positive_integer(item.get("listing_id"))
        if channel_id is None or supplier_id is _INVALID_DECIMAL or listing_id is _INVALID_DECIMAL:
            return None
        identities.append(
            {
                "channel_id": channel_id,
                "supplier_id": supplier_id,
                "listing_id": listing_id,
            }
        )
    return identities


def _owned_overlap(
    identities: list[dict[str, int | None]],
    owned: Mapping[str, list[int]],
) -> int:
    suppliers = set(owned["supplier_ids"])
    channels = set(owned["channel_ids"])
    listings = set(owned["listing_ids"])
    return sum(
        identity["channel_id"] in channels
        or identity["supplier_id"] in suppliers
        or identity["listing_id"] in listings
        for identity in identities
    )


def _positive_integer_list(value: object) -> list[int] | None:
    if not isinstance(value, list):
        return None
    result: list[int] = []
    for item in value:
        parsed = _json_positive_integer(item)
        if parsed is None:
            return None
        result.append(parsed)
    return result


def _slot_costs(value: object) -> dict[int, Decimal] | None:
    if not isinstance(value, Mapping):
        return None
    result: dict[int, Decimal] = {}
    for raw_slot, raw_cost in value.items():
        if not isinstance(raw_slot, str) or not raw_slot.isdecimal():
            return None
        slot = int(raw_slot)
        cost = _decimal_value(raw_cost)
        if slot < 1 or slot in result or cost is None:
            return None
        result[slot] = cost
    return result


def _assignment_valid(value: object) -> bool:
    if value is None:
        return True
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return value > 0
    return isinstance(value, str) and bool(value.strip())


def _optional_positive_integer(value: object) -> int | None | object:
    if value is None:
        return None
    parsed = _json_positive_integer(value)
    return _INVALID_DECIMAL if parsed is None else parsed


def _json_positive_integer(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return None
    return value


def _json_nonnegative_integer(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _decimal_value(value: object) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
    except (DecimalException, ValueError):
        return None
    return result if result.is_finite() and result >= 0 else None


def _optional_decimal_value(value: object) -> Decimal | None | object:
    if value is None:
        return None
    parsed = _decimal_value(value)
    return _INVALID_DECIMAL if parsed is None else parsed


def _positive_decimal_value(value: object) -> Decimal | None:
    parsed = _decimal_value(value)
    if parsed is None or parsed <= 0:
        return None
    return parsed


def _aware_iso_value(value: object) -> datetime | None:
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    try:
        return parsed.astimezone(timezone.utc)
    except (OverflowError, ValueError):
        return None


def _optional_aware_utc(value: datetime | None, code: str) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ShadowJournalAuditError(code)
    try:
        return value.astimezone(timezone.utc)
    except (OverflowError, ValueError):
        raise ShadowJournalAuditError(code) from None


def _audit_identifier(value: object) -> bool:
    return (
        isinstance(value, str)
        and 1 <= len(value) <= 128
        and all(
            character.isascii() and (character.isalnum() or character == "_") for character in value
        )
    )


def _ceil_to_step(value: Decimal, step: Decimal) -> Decimal:
    return (value / step).to_integral_value(rounding=ROUND_CEILING) * step


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(char in "0123456789abcdef" for char in value)
    )


def _nonnegative_integer(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _report(
    observations: list[ShadowObservation],
    errors: list[str],
    service_failures: list[str],
    minimum_window_seconds: float,
    maximum_gap_seconds: float,
    *,
    contract_not_before: datetime | None,
    boundary_source: str | None,
    ignored_pre_boundary_records: int,
) -> dict[str, Any]:
    unique_errors = sorted(set(errors))
    report: dict[str, Any] = {
        "success": not unique_errors,
        "observation_count": len(observations),
        "required_window_seconds": minimum_window_seconds,
        "maximum_allowed_gap_seconds": maximum_gap_seconds,
        "retry_audit_contract": "a6-provider-shadow-retry-audit-v1",
        "window_boundary_source": boundary_source,
        "window_not_before": (
            None if contract_not_before is None else contract_not_before.isoformat()
        ),
        "ignored_pre_boundary_record_count": ignored_pre_boundary_records,
        "errors": unique_errors,
        "service_failures": service_failures,
    }
    if observations:
        ordered = sorted(observations, key=lambda item: item.observed_at)
        gaps = [
            (
                (current.observed_at - previous.observed_at).total_seconds(),
                previous.observed_at,
                current.observed_at,
            )
            for previous, current in zip(ordered, ordered[1:], strict=False)
        ]
        maximum_gap = max(gaps, default=None, key=lambda item: item[0])
        report.update(
            {
                "window_start": ordered[0].observed_at.isoformat(),
                "window_end": ordered[-1].observed_at.isoformat(),
                "window_seconds": (
                    ordered[-1].observed_at - ordered[0].observed_at
                ).total_seconds(),
                "maximum_observed_gap_seconds": (0 if maximum_gap is None else maximum_gap[0]),
                "maximum_observed_gap_start": (
                    None if maximum_gap is None else maximum_gap[1].isoformat()
                ),
                "maximum_observed_gap_end": (
                    None if maximum_gap is None else maximum_gap[2].isoformat()
                ),
                "first_routing_generation": ordered[0].generation,
                "last_routing_generation": ordered[-1].generation,
                "first_cost_generation": ordered[0].cost_generation,
                "last_cost_generation": ordered[-1].cost_generation,
                "first_snapshot_sequence": ordered[0].snapshot_sequence,
                "last_snapshot_sequence": ordered[-1].snapshot_sequence,
                "first_top_identity_digest": ordered[0].top_identity_digest,
                "last_top_identity_digest": ordered[-1].top_identity_digest,
                "first_standby_identity_digest": ordered[0].standby_identity_digest,
                "last_standby_identity_digest": ordered[-1].standby_identity_digest,
                "semantic_input_count": len({item.semantic_input_hash for item in ordered}),
                "semantic_output_count": len({item.semantic_output_hash for item in ordered}),
                "pool_size_input_count": len({item.pool_size_input_hash for item in ordered}),
                "pool_size_output_count": len({item.pool_size_output_hash for item in ordered}),
                "inventory_changed_total": sum(item.inventory_changed_count for item in ordered),
            }
        )
    return report


def _not_before_argument(value: str) -> datetime:
    text = str(value or "").strip()
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise argparse.ArgumentTypeError("not-before must be an ISO-8601 timestamp") from None
    try:
        result = _optional_aware_utc(parsed, "shadow_journal_not_before_invalid")
    except ShadowJournalAuditError:
        raise argparse.ArgumentTypeError("not-before must include a UTC offset") from None
    assert result is not None
    return result


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(
        description="Audit a journalctl -o json shadow-controller window"
    )
    command.add_argument("--model", required=True)
    command.add_argument("--journal-file", type=Path)
    command.add_argument("--minimum-hours", type=float, default=24)
    command.add_argument("--maximum-gap-seconds", type=float, default=120)
    command.add_argument(
        "--not-before",
        type=_not_before_argument,
        help=(
            "ignore earlier records; by default the first record carrying the "
            "retry-audit contract is the boundary"
        ),
    )
    command.add_argument(
        "--allow-missing-inventory-projection",
        action="store_true",
    )
    return command


def _input_lines(path: Path | None) -> tuple[Iterable[str], TextIO | None]:
    if path is None:
        return sys.stdin, None
    handle = path.expanduser().resolve().open("r", encoding="utf-8")
    return handle, handle


def main() -> None:
    args = parser().parse_args()
    lines, handle = _input_lines(args.journal_file)
    try:
        report = audit_journal_lines(
            lines,
            model=args.model,
            minimum_window_seconds=args.minimum_hours * 60 * 60,
            maximum_gap_seconds=args.maximum_gap_seconds,
            require_inventory_projection=not args.allow_missing_inventory_projection,
            not_before=args.not_before,
        )
    except (OSError, ShadowJournalAuditError) as exc:
        report = {"success": False, "errors": [str(exc)]}
    finally:
        if handle is not None:
            handle.close()
    print(json.dumps(report, ensure_ascii=True, sort_keys=True))
    if not report.get("success"):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
