"""Internal publication control API storage and failure-driven wake-ups."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .trade_channel_failure import (
    FAILURE_CLASSIFIER_VERSION,
    classify_provider_attempt_failure,
)
from .trade_provider_fast_block import (
    ProviderFastBlockProjection,
    ProviderFastBlockProjectionResult,
)

UTC = timezone.utc

_SAFE_MODEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,95}$")
_SAFE_CODE = re.compile(r"^[a-z0-9][a-z0-9_.:-]{0,127}$")
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,191}$")
_FAILURE_OWNERS = frozenset(
    {"user", "client_transport", "provider", "platform_path", "internal", "unknown"}
)
_DRAINING_OWNERS = frozenset({"provider"})
_UPSTREAM_CLASSIFIER_VERSIONS = frozenset(
    {
        "failure-classifier-v1",
        FAILURE_CLASSIFIER_VERSION,
        "a6-provider-request-classifier-v1",
    }
)
_QUARANTINE_BACKOFF_SECONDS = (600, 1_800, 7_200, 21_600)
_FAST_BLOCK_KEY_PREFIX = "a6:provider:fast-block:v1"
_MAX_TRIGGER_BYTES = 4096
_SAFE_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class PublicationControlError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class PublicationWriteMetadata:
    idempotency_key: str
    expected_generation: int
    requested_by: str
    trace_id: str

    @classmethod
    def parse(cls, document: Mapping[str, Any]) -> PublicationWriteMetadata:
        return cls(
            idempotency_key=_identifier(
                document.get("idempotency_key"), "publication_idempotency_key_invalid"
            ),
            expected_generation=_positive_integer(
                document.get("expected_generation"), "publication_expected_generation_invalid"
            ),
            requested_by=_identifier(
                document.get("requested_by"), "publication_requested_by_invalid"
            ),
            trace_id=_identifier(document.get("trace_id"), "publication_trace_id_invalid"),
        )


@dataclass(frozen=True)
class ProviderAttemptFailureEvent:
    metadata: PublicationWriteMetadata
    request_id: str
    attempt_id: str
    attempt_no: int
    model: str
    routing_generation: int
    cost_generation: int
    slot_no: int
    enodot_account_id: int
    a6_token_id: int
    channel_id: int
    endpoint_host: str
    egress_id: str
    region: str
    failure_owner: str
    failure_owner_declared: str
    failure_code: str
    http_status: int | None
    transport_status: int | None
    terminal_event_seen: bool
    occurred_at: datetime
    classifier_version: str
    classifier_version_declared: str
    retry_next: bool
    fast_block_slot: bool
    control_action: str
    quarantine_scope: str | None
    channel_failure_streak_count: int = 0
    channel_failure_streak_threshold: int = 0
    channel_failure_threshold_reached: bool = False

    @classmethod
    def parse(cls, document: Mapping[str, Any]) -> ProviderAttemptFailureEvent:
        _reject_unknown(
            document,
            frozenset(
                {
                    "event_type",
                    "idempotency_key",
                    "expected_generation",
                    "requested_by",
                    "trace_id",
                    "request_id",
                    "attempt_id",
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
                    "region",
                    "failure_owner",
                    "failure_code",
                    "http_status",
                    "transport_status",
                    "terminal_event_seen",
                    "occurred_at",
                    "classifier_version",
                }
            ),
            "provider_failure_fields_invalid",
        )
        if document.get("event_type") != "provider_attempt_failed":
            raise PublicationControlError("provider_failure_event_type_invalid")
        metadata = PublicationWriteMetadata.parse(document)
        request_id = _identifier(document.get("request_id"), "provider_failure_request_id_invalid")
        attempt_no = _positive_integer(
            document.get("attempt_no"), "provider_failure_attempt_no_invalid"
        )
        model = _model(document.get("model"))
        routing_generation = _positive_integer(
            document.get("routing_generation"), "provider_failure_routing_generation_invalid"
        )
        if metadata.expected_generation != routing_generation:
            raise PublicationControlError("provider_failure_expected_generation_mismatch")
        owner = str(document.get("failure_owner") or "").strip()
        if owner not in _FAILURE_OWNERS:
            raise PublicationControlError("provider_failure_owner_invalid")
        classifier_version_declared = _identifier(
            document.get("classifier_version"), "provider_failure_classifier_invalid"
        )
        if classifier_version_declared not in _UPSTREAM_CLASSIFIER_VERSIONS:
            raise PublicationControlError("provider_failure_classifier_unsupported")
        terminal_event_seen = document.get("terminal_event_seen")
        if not isinstance(terminal_event_seen, bool):
            raise PublicationControlError("provider_failure_terminal_event_invalid")
        attempt_id_raw = document.get("attempt_id")
        attempt_id = (
            _identifier(attempt_id_raw, "provider_failure_attempt_id_invalid")
            if attempt_id_raw is not None
            else _derived_attempt_id(request_id, attempt_no)
        )
        failure_code = _code(document.get("failure_code"), "provider_failure_code_invalid")
        http_status = _optional_http_status(
            document.get("http_status"), "provider_failure_http_status_invalid"
        )
        transport_status = _optional_http_status(
            document.get("transport_status"), "provider_failure_transport_status_invalid"
        )
        classification = classify_provider_attempt_failure(
            failure_code,
            http_status=http_status if http_status is not None else transport_status,
        )
        if classification.owner != owner:
            raise PublicationControlError("provider_failure_owner_mismatch")
        return cls(
            metadata=metadata,
            request_id=request_id,
            attempt_id=attempt_id,
            attempt_no=attempt_no,
            model=model,
            routing_generation=routing_generation,
            cost_generation=_positive_integer(
                document.get("cost_generation"), "provider_failure_cost_generation_invalid"
            ),
            slot_no=_positive_integer(document.get("slot_no"), "provider_failure_slot_no_invalid"),
            enodot_account_id=_positive_integer(
                document.get("enodot_account_id"), "provider_failure_account_id_invalid"
            ),
            a6_token_id=_positive_integer(
                document.get("a6_token_id"), "provider_failure_token_id_invalid"
            ),
            channel_id=_positive_integer(
                document.get("channel_id"), "provider_failure_channel_id_invalid"
            ),
            endpoint_host=_hostname(document.get("endpoint_host")),
            egress_id=_identifier(
                document.get("egress_id") or "direct",
                "provider_failure_egress_invalid",
            ),
            region=_identifier(
                document.get("region") or "unknown",
                "provider_failure_region_invalid",
            ),
            failure_owner=classification.owner,
            failure_owner_declared=owner,
            failure_code=classification.code,
            http_status=http_status,
            transport_status=transport_status,
            terminal_event_seen=terminal_event_seen,
            occurred_at=_timestamp(document.get("occurred_at")),
            classifier_version=classification.classifier_version,
            classifier_version_declared=classifier_version_declared,
            retry_next=classification.retry_next,
            fast_block_slot=classification.fast_block_slot,
            control_action=classification.control_action,
            quarantine_scope=classification.quarantine_scope,
        )

    @property
    def drains_slot(self) -> bool:
        if not self.fast_block_slot or self.failure_owner not in _DRAINING_OWNERS:
            return False
        if self.quarantine_scope == "account" and self.control_action == "repair_liquidity":
            return True
        return self.quarantine_scope == "channel" and self.channel_failure_threshold_reached


@dataclass(frozen=True)
class PublicationRequestClaim:
    request_id: int
    idempotency_key: str
    model: str
    expected_generation: int
    expected_cost_generation: int | None
    request_source: str
    attempt_count: int


class AtomicPublicationTrigger:
    """Atomically replace a small PathChanged marker after the DB commit."""

    def __init__(self, path: Path) -> None:
        self.path = path.expanduser().resolve()

    def wake(self, *, model: str, request_id: int) -> None:
        payload = (
            json.dumps(
                {"model": _model(model), "request_id": int(request_id)},
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
            + b"\n"
        )
        if len(payload) > _MAX_TRIGGER_BYTES or request_id < 1:
            raise PublicationControlError("publication_trigger_payload_invalid")
        parent = self.path.parent
        temporary = parent / f".{self.path.name}.{os.getpid()}.tmp"
        try:
            parent.mkdir(mode=0o750, parents=True, exist_ok=True)
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
                0o600,
            )
            try:
                view = memoryview(payload)
                while view:
                    written = os.write(descriptor, view)
                    if written < 1:
                        raise OSError("publication trigger write made no progress")
                    view = view[written:]
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            os.replace(temporary, self.path)
            os.chmod(self.path, 0o600)
            directory = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except Exception as exc:
            with suppress(FileNotFoundError):
                temporary.unlink()
            raise PublicationControlError("publication_trigger_write_failed") from exc


class PostgresPublicationControl:
    """Persist internal write-API requests and expose bounded publication metrics."""

    def __init__(
        self,
        connection_factory: Callable[[], Any],
        *,
        trigger: AtomicPublicationTrigger | None = None,
        fast_block_projector: Any | None = None,
        running_reclaim_seconds: int = 1,
        fast_block_reclaim_seconds: int = 30,
        minimum_recovery_observers: int = 2,
        recovery_evidence_max_age_seconds: int = 120,
        provider_channel_failure_threshold: int = 3,
    ) -> None:
        if not callable(connection_factory):
            raise ValueError("publication_control_connection_factory_invalid")
        if isinstance(running_reclaim_seconds, bool) or running_reclaim_seconds < 1:
            raise ValueError("publication_request_reclaim_seconds_invalid")
        if fast_block_projector is not None and not callable(
            getattr(fast_block_projector, "apply", None)
        ):
            raise ValueError("provider_fast_block_projector_invalid")
        if isinstance(fast_block_reclaim_seconds, bool) or fast_block_reclaim_seconds < 1:
            raise ValueError("provider_fast_block_reclaim_seconds_invalid")
        if (
            isinstance(minimum_recovery_observers, bool)
            or not 2 <= minimum_recovery_observers <= 10
        ):
            raise ValueError("provider_recovery_observer_quorum_invalid")
        if (
            isinstance(recovery_evidence_max_age_seconds, bool)
            or not 30 <= recovery_evidence_max_age_seconds <= 3_600
        ):
            raise ValueError("provider_recovery_evidence_max_age_invalid")
        if (
            isinstance(provider_channel_failure_threshold, bool)
            or not 1 <= provider_channel_failure_threshold <= 100
        ):
            raise ValueError("provider_channel_failure_threshold_invalid")
        self.connection_factory = connection_factory
        self.trigger = trigger
        self.fast_block_projector = fast_block_projector
        self.running_reclaim_seconds = int(running_reclaim_seconds)
        self.fast_block_reclaim_seconds = int(fast_block_reclaim_seconds)
        self.minimum_recovery_observers = int(minimum_recovery_observers)
        self.recovery_evidence_max_age_seconds = int(recovery_evidence_max_age_seconds)
        self.provider_channel_failure_threshold = int(provider_channel_failure_threshold)

    def request_reconcile(self, model: str, document: Mapping[str, Any]) -> dict[str, Any]:
        model = _model(model)
        _reject_unknown(
            document,
            frozenset(
                {
                    "idempotency_key",
                    "expected_generation",
                    "requested_by",
                    "trace_id",
                    "reason_code",
                }
            ),
            "publication_request_fields_invalid",
        )
        metadata = PublicationWriteMetadata.parse(document)
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                generation, cost_generation = self._active_bundle(
                    cursor, model, expected_generation=metadata.expected_generation
                )
                row, deduplicated = self._enqueue(
                    cursor,
                    metadata=metadata,
                    model=model,
                    cost_generation=cost_generation,
                    request_source="manual",
                    payload={
                        "model": model,
                        "expected_generation": generation,
                        "expected_cost_generation": cost_generation,
                        "reason_code": _code(
                            document.get("reason_code") or "manual_reconcile",
                            "publication_reason_code_invalid",
                        ),
                    },
                )
                self._verify_request_readback(cursor, row)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            _close(connection)
        wake_scheduled = self._wake(model, int(row["id"]))
        return _request_result(row, deduplicated=deduplicated, wake_scheduled=wake_scheduled)

    def report_attempt_failure(self, document: Mapping[str, Any]) -> dict[str, Any]:
        event = ProviderAttemptFailureEvent.parse(document)
        connection = self.connection_factory()
        request_row: dict[str, Any]
        projection_row: dict[str, Any] | None = None
        quarantine_row: dict[str, Any] | None = None
        deduplicated = False
        try:
            with connection.cursor() as cursor:
                _generation, active_cost_generation = self._active_bundle(
                    cursor,
                    event.model,
                    expected_generation=event.routing_generation,
                )
                if active_cost_generation != event.cost_generation:
                    raise PublicationControlError("provider_failure_cost_generation_mismatch")
                slot_row = self._lock_failure_slot(cursor, event)
                attempt_row = self._record_attempt_failure(cursor, event)
                event, attempt_row = self._apply_channel_failure_streak(
                    cursor,
                    event,
                    attempt_row,
                    slot_row=slot_row,
                    threshold=self.provider_channel_failure_threshold,
                )
                observed_attempt_id = str(attempt_row["attempt_id"])
                if (
                    event.quarantine_scope == "account"
                    and event.control_action == "repair_liquidity"
                ):
                    self._record_liquidity_failure(cursor, event)
                if event.drains_slot:
                    projection_row, projection_deduplicated = self._enqueue_fast_block(
                        cursor,
                        event,
                        attempt_id=observed_attempt_id,
                    )
                    if event.quarantine_scope == "channel":
                        if projection_deduplicated:
                            quarantine_row = self._read_projection_quarantine(
                                cursor,
                                projection_row,
                                event,
                            )
                        else:
                            quarantine_row = self._upsert_quarantine(cursor, event)
                            projection_row = self._link_projection_quarantine(
                                cursor,
                                projection_row,
                                quarantine_row,
                            )
                drained = self._drain_slot(cursor, event) if event.drains_slot else False
                request_row, deduplicated = self._enqueue(
                    cursor,
                    metadata=event.metadata,
                    model=event.model,
                    cost_generation=event.cost_generation,
                    request_source="provider_failure",
                    payload=_failure_receipt_payload(event, observed_attempt_id),
                )
                if not event.drains_slot:
                    request_row = self._complete_failure_receipt(
                        cursor,
                        request_row,
                        deduplicated=deduplicated,
                    )
                self._verify_request_readback(cursor, request_row)
                self._verify_attempt_readback(cursor, event, attempt_row)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            _close(connection)
        wake_scheduled = (
            self._wake(event.model, int(request_row["id"])) if event.drains_slot else False
        )
        fast_block_projection = None
        if projection_row is not None:
            fast_block_projection = self._projection_result(projection_row)
            if self.fast_block_projector is not None:
                fast_block_projection = self.project_fast_block_event(int(projection_row["id"]))
        receipt = _request_result(
            request_row,
            deduplicated=deduplicated,
            wake_scheduled=wake_scheduled,
        )
        result: dict[str, Any] = {
            "accepted": True,
            "request_id": event.request_id,
            "attempt_id": observed_attempt_id,
            "model": event.model,
            "routing_generation": event.routing_generation,
            "cost_generation": event.cost_generation,
            "slot_no": event.slot_no,
            "slot_drained": drained,
            "publication_requested": event.drains_slot,
            "wake_scheduled": wake_scheduled,
            "failure_owner": event.failure_owner,
            "classifier_version": event.classifier_version,
            "control_action": event.control_action,
            "channel_failure_streak_count": event.channel_failure_streak_count,
            "channel_failure_streak_threshold": event.channel_failure_streak_threshold,
            "channel_failure_threshold_reached": event.channel_failure_threshold_reached,
            "quarantine": _quarantine_result(quarantine_row),
            "fast_block_projection": fast_block_projection,
            "failure_receipt": receipt,
        }
        if event.drains_slot:
            result["publication_request"] = receipt
        return result

    def begin_quarantine_probe(self, model: str, document: Mapping[str, Any]) -> dict[str, Any]:
        model = _model(model)
        _reject_unknown(
            document,
            frozenset(
                {
                    "idempotency_key",
                    "expected_generation",
                    "requested_by",
                    "trace_id",
                    "quarantine_id",
                }
            ),
            "provider_quarantine_probe_fields_invalid",
        )
        metadata = PublicationWriteMetadata.parse(document)
        quarantine_id = _positive_integer(
            document.get("quarantine_id"), "provider_quarantine_id_invalid"
        )
        probe_token = _derived_probe_token(model, quarantine_id, metadata.idempotency_key)
        connection = self.connection_factory()
        deduplicated = False
        try:
            with connection.cursor() as cursor:
                _generation, cost_generation = self._active_bundle(
                    cursor,
                    model,
                    expected_generation=metadata.expected_generation,
                )
                cursor.execute(
                    """
                    SELECT id, model, state, routing_generation, cost_generation,
                           next_probe_at
                    FROM a6_trade.provider_quarantines
                    WHERE id = %s AND model = %s
                    FOR UPDATE
                    """,
                    (quarantine_id, model),
                )
                quarantine = _row(cursor)
                if quarantine is None:
                    raise PublicationControlError("provider_quarantine_missing")
                if (
                    int(quarantine.get("routing_generation") or 0) != metadata.expected_generation
                    or int(quarantine.get("cost_generation") or 0) != cost_generation
                ):
                    raise PublicationControlError("provider_quarantine_generation_mismatch")
                cursor.execute(
                    """
                    SELECT id, idempotency_key, quarantine_id, model,
                           expected_generation, expected_cost_generation,
                           probe_token, status, requested_by, trace_id, claimed_at,
                           verified_at, observer_quorum_ref, pin_readback_ref,
                           real_probe_ref, evidence_hash, unblock_projection_id,
                           publication_request_id
                    FROM a6_trade.provider_quarantine_recoveries
                    WHERE quarantine_id = %s AND status = 'probing'
                    FOR SHARE
                    """,
                    (quarantine_id,),
                )
                active_claim = _row(cursor)
                if active_claim is not None and str(active_claim["idempotency_key"]) != (
                    f"quarantine-probe:{metadata.idempotency_key}"
                ):
                    raise PublicationControlError("provider_quarantine_probe_busy")
                cursor.execute(
                    """
                    INSERT INTO a6_trade.provider_quarantine_recoveries (
                        idempotency_key, quarantine_id, model,
                        expected_generation, expected_cost_generation,
                        probe_token, requested_by, trace_id
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (idempotency_key) DO NOTHING
                    RETURNING id, idempotency_key, quarantine_id, model,
                              expected_generation, expected_cost_generation,
                              probe_token, status, requested_by, trace_id, claimed_at,
                              verified_at, observer_quorum_ref, pin_readback_ref,
                              real_probe_ref, evidence_hash, unblock_projection_id,
                              publication_request_id
                    """,
                    (
                        f"quarantine-probe:{metadata.idempotency_key}",
                        quarantine_id,
                        model,
                        metadata.expected_generation,
                        cost_generation,
                        probe_token,
                        metadata.requested_by,
                        metadata.trace_id,
                    ),
                )
                recovery = _row(cursor)
                deduplicated = recovery is None
                if recovery is None:
                    cursor.execute(
                        """
                        SELECT id, idempotency_key, quarantine_id, model,
                               expected_generation, expected_cost_generation,
                               probe_token, status, requested_by, trace_id, claimed_at,
                               verified_at, observer_quorum_ref, pin_readback_ref,
                               real_probe_ref, evidence_hash, unblock_projection_id,
                               publication_request_id
                        FROM a6_trade.provider_quarantine_recoveries
                        WHERE idempotency_key = %s
                        FOR SHARE
                        """,
                        (f"quarantine-probe:{metadata.idempotency_key}",),
                    )
                    recovery = _row(cursor)
                if recovery is None or any(
                    recovery.get(name) != value
                    for name, value in {
                        "quarantine_id": quarantine_id,
                        "model": model,
                        "expected_generation": metadata.expected_generation,
                        "expected_cost_generation": cost_generation,
                        "probe_token": probe_token,
                        "requested_by": metadata.requested_by,
                        "trace_id": metadata.trace_id,
                    }.items()
                ):
                    raise PublicationControlError("provider_quarantine_probe_idempotency_conflict")
                if not deduplicated:
                    cursor.execute(
                        """
                        UPDATE a6_trade.provider_quarantines
                        SET state = 'probing', last_probe_at = now(),
                            last_probe_result = 'pending', updated_at = now()
                        WHERE id = %s AND state = 'active'
                          AND (next_probe_at IS NULL OR next_probe_at <= now())
                        RETURNING id, state
                        """,
                        (quarantine_id,),
                    )
                    probing = _row(cursor)
                    if probing is None or probing.get("state") != "probing":
                        raise PublicationControlError("provider_quarantine_probe_not_due")
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            _close(connection)
        return {
            "recovery_id": int(recovery["id"]),
            "quarantine_id": quarantine_id,
            "model": model,
            "routing_generation": metadata.expected_generation,
            "cost_generation": int(recovery["expected_cost_generation"]),
            "probe_token": probe_token,
            "status": str(recovery["status"]),
            "deduplicated": deduplicated,
        }

    def confirm_quarantine_recovery(
        self, model: str, document: Mapping[str, Any]
    ) -> dict[str, Any]:
        model = _model(model)
        _reject_unknown(
            document,
            frozenset(
                {
                    "idempotency_key",
                    "expected_generation",
                    "requested_by",
                    "trace_id",
                    "quarantine_id",
                    "probe_token",
                }
            ),
            "provider_quarantine_recovery_fields_invalid",
        )
        metadata = PublicationWriteMetadata.parse(document)
        quarantine_id = _positive_integer(
            document.get("quarantine_id"), "provider_quarantine_id_invalid"
        )
        probe_token = _identifier(
            document.get("probe_token"), "provider_quarantine_probe_token_invalid"
        )
        connection = self.connection_factory()
        deduplicated = False
        try:
            with connection.cursor() as cursor:
                _generation, cost_generation = self._active_bundle(
                    cursor,
                    model,
                    expected_generation=metadata.expected_generation,
                )
                cursor.execute(
                    """
                    SELECT recovery.id, recovery.idempotency_key,
                           recovery.quarantine_id, recovery.model,
                           recovery.expected_generation,
                           recovery.expected_cost_generation,
                           recovery.probe_token, recovery.status,
                           recovery.claimed_at, recovery.verified_at,
                           recovery.observer_quorum_ref,
                           recovery.pin_readback_ref, recovery.real_probe_ref,
                           recovery.evidence_hash,
                           recovery.confirmation_idempotency_key,
                           recovery.confirmation_requested_by,
                           recovery.confirmation_trace_id,
                           recovery.unblock_projection_id,
                           recovery.publication_request_id,
                           quarantine.state AS quarantine_state,
                           quarantine.scope_type, quarantine.scope_id,
                           quarantine.region, quarantine.slot_no,
                           quarantine.channel_id, quarantine.endpoint_host,
                           quarantine.egress_id, quarantine.routing_generation,
                           quarantine.cost_generation,
                           quarantine.last_failure_at,
                           quarantine.recovery_evidence
                    FROM a6_trade.provider_quarantine_recoveries recovery
                    JOIN a6_trade.provider_quarantines quarantine
                      ON quarantine.id = recovery.quarantine_id
                    WHERE recovery.quarantine_id = %s
                      AND recovery.probe_token = %s
                      AND recovery.model = %s
                    FOR UPDATE OF recovery, quarantine
                    """,
                    (quarantine_id, probe_token, model),
                )
                recovery = _row(cursor)
                if recovery is None:
                    raise PublicationControlError("provider_quarantine_probe_claim_missing")
                if (
                    int(recovery["expected_generation"]) != metadata.expected_generation
                    or int(recovery["expected_cost_generation"]) != cost_generation
                    or int(recovery.get("routing_generation") or 0) != metadata.expected_generation
                    or int(recovery.get("cost_generation") or 0) != cost_generation
                ):
                    raise PublicationControlError("provider_quarantine_generation_mismatch")
                if recovery["status"] == "verified":
                    deduplicated = True
                    _verify_recovery_replay(
                        recovery,
                        metadata=metadata,
                    )
                    projection_row = self._read_fast_block_row(
                        cursor, int(recovery["unblock_projection_id"])
                    )
                    request_row = self._read_request_row(
                        cursor, int(recovery["publication_request_id"])
                    )
                    slot_restored = True
                    evidence_result = _recovery_evidence_result(recovery)
                else:
                    if recovery["status"] != "probing" or recovery["quarantine_state"] != (
                        "probing"
                    ):
                        raise PublicationControlError("provider_quarantine_recovery_state_conflict")
                    verified_at = self._database_now(cursor)
                    claimed_at = _database_timestamp(
                        recovery.get("claimed_at"),
                        "provider_quarantine_claim_time_invalid",
                    )
                    last_failure_at = _database_timestamp(
                        recovery.get("last_failure_at"),
                        "provider_quarantine_failure_time_invalid",
                    )
                    if verified_at < max(claimed_at, last_failure_at):
                        raise PublicationControlError("provider_quarantine_evidence_clock_invalid")
                    slot = self._load_recovery_slot(cursor, recovery)
                    evidence = self._build_recovery_evidence(
                        cursor,
                        recovery,
                        slot,
                        verified_at=verified_at,
                        claimed_at=claimed_at,
                        last_failure_at=last_failure_at,
                    )
                    evidence_hash = hashlib.sha256(_json(evidence).encode()).hexdigest()
                    slot_restored = self._restore_quarantined_slot(
                        cursor,
                        slot,
                        verified_at=verified_at,
                    )
                    cursor.execute(
                        """
                        UPDATE a6_trade.provider_quarantines
                        SET state = 'closed', ends_at = %s,
                            recovery_verified_at = %s,
                            recovery_evidence = %s::jsonb,
                            last_probe_result = 'verified', updated_at = now()
                        WHERE id = %s AND state = 'probing'
                        RETURNING id, state
                        """,
                        (verified_at, verified_at, _json(evidence), quarantine_id),
                    )
                    closed = _row(cursor)
                    if closed is None or closed.get("state") != "closed":
                        raise PublicationControlError("provider_quarantine_close_cas_mismatch")
                    projection_row, projection_deduplicated = self._enqueue_fast_unblock(
                        cursor,
                        recovery,
                        metadata=metadata,
                        verified_at=verified_at,
                        evidence_hash=evidence_hash,
                    )
                    if projection_deduplicated:
                        raise PublicationControlError("provider_quarantine_unblock_state_conflict")
                    request_row, request_deduplicated = self._enqueue(
                        cursor,
                        metadata=metadata,
                        model=model,
                        cost_generation=cost_generation,
                        request_source="repair",
                        payload={
                            "model": model,
                            "expected_generation": metadata.expected_generation,
                            "expected_cost_generation": cost_generation,
                            "quarantine_id": quarantine_id,
                            "recovery_id": int(recovery["id"]),
                            "verified_at": evidence["verified_at"],
                            "evidence_hash": evidence_hash,
                        },
                    )
                    if request_deduplicated:
                        raise PublicationControlError("provider_quarantine_repair_state_conflict")
                    cursor.execute(
                        """
                        UPDATE a6_trade.provider_quarantine_recoveries
                        SET status = 'verified', verified_at = %s,
                            observer_quorum_ref = %s, pin_readback_ref = %s,
                            real_probe_ref = %s, evidence_hash = %s,
                            confirmation_idempotency_key = %s,
                            confirmation_requested_by = %s,
                            confirmation_trace_id = %s,
                            unblock_projection_id = %s,
                            publication_request_id = %s,
                            last_error_class = NULL, updated_at = now()
                        WHERE id = %s AND status = 'probing'
                        RETURNING id, status
                        """,
                        (
                            verified_at,
                            evidence["observer_quorum_ref"],
                            evidence["pin_readback_ref"],
                            evidence["real_probe_ref"],
                            evidence_hash,
                            metadata.idempotency_key,
                            metadata.requested_by,
                            metadata.trace_id,
                            int(projection_row["id"]),
                            int(request_row["id"]),
                            int(recovery["id"]),
                        ),
                    )
                    verified = _row(cursor)
                    if verified is None or verified.get("status") != "verified":
                        raise PublicationControlError("provider_quarantine_recovery_cas_mismatch")
                    self._verify_request_readback(cursor, request_row)
                    evidence_result = {
                        "verified_at": evidence["verified_at"],
                        "observer_quorum_ref": evidence["observer_quorum_ref"],
                        "pin_readback_ref": evidence["pin_readback_ref"],
                        "real_probe_ref": evidence["real_probe_ref"],
                        "evidence_hash": evidence_hash,
                    }
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            _close(connection)
        wake_scheduled = self._wake(model, int(request_row["id"]))
        projection_result = self._projection_result(projection_row)
        if self.fast_block_projector is not None:
            projection_result = self.project_fast_block_event(int(projection_row["id"]))
        return {
            "recovery_id": int(recovery["id"]),
            "quarantine_id": quarantine_id,
            "model": model,
            "status": "verified",
            "slot_restored": slot_restored,
            "deduplicated": deduplicated,
            "evidence": evidence_result,
            "fast_block_projection": projection_result,
            "publication_request": _request_result(
                request_row,
                deduplicated=deduplicated,
                wake_scheduled=wake_scheduled,
            ),
        }

    def project_fast_block_event(self, event_id: int) -> dict[str, Any]:
        if self.fast_block_projector is None:
            raise PublicationControlError("provider_fast_block_projector_unavailable")
        event_id = _positive_integer(event_id, "provider_fast_block_event_id_invalid")
        row = self._claim_fast_block(event_id=event_id, after_id=0)
        if row is None:
            return self._load_fast_block_result(event_id)
        return self._apply_claimed_fast_block(row)

    def project_pending_fast_blocks(
        self,
        *,
        limit: int = 100,
        stop_on_failure: bool = False,
    ) -> dict[str, Any]:
        if self.fast_block_projector is None:
            raise PublicationControlError("provider_fast_block_projector_unavailable")
        if isinstance(limit, bool) or not 1 <= limit <= 500:
            raise PublicationControlError("provider_fast_block_projection_limit_invalid")
        if not isinstance(stop_on_failure, bool):
            raise PublicationControlError("provider_fast_block_projection_stop_on_failure_invalid")
        after_id = 0
        events: list[dict[str, Any]] = []
        for _ in range(limit):
            row = self._claim_fast_block(event_id=None, after_id=after_id)
            if row is None:
                break
            after_id = int(row["id"])
            event = self._apply_claimed_fast_block(row)
            events.append(event)
            if stop_on_failure and event["projection_state"] == "failed":
                break
        return {
            "processed": len(events),
            "projected": sum(row["projection_state"] == "projected" for row in events),
            "superseded": sum(row["projection_state"] == "superseded" for row in events),
            "failed": sum(row["projection_state"] == "failed" for row in events),
            "events": events,
        }

    def rebuild_fast_block_cache(self, *, limit: int = 500) -> dict[str, Any]:
        """Rebuild Redis from the latest PostgreSQL event for each projection key."""

        if self.fast_block_projector is None or not callable(
            getattr(self.fast_block_projector, "rebuild", None)
        ):
            raise PublicationControlError("provider_fast_block_rebuilder_unavailable")
        if isinstance(limit, bool) or not 1 <= limit <= 500:
            raise PublicationControlError("provider_fast_block_rebuild_limit_invalid")
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    WITH latest AS (
                        SELECT DISTINCT ON (projection.projection_key)
                               projection.id
                        FROM a6_trade.provider_fast_block_outbox projection
                        ORDER BY projection.projection_key, projection.id DESC
                    )
                    SELECT projection.id, projection.idempotency_key,
                           projection.operation, projection.model,
                           projection.routing_generation,
                           projection.cost_generation, projection.slot_no,
                           projection.channel_id, projection.attempt_id,
                           projection.quarantine_id, projection.projection_key,
                           projection.payload, projection.projection_state,
                           projection.attempt_count,
                           projection.failure_occurred_at,
                           projection.recovery_verified_at,
                           projection.last_error_class,
                           quarantine.state AS quarantine_state
                    FROM latest
                    JOIN a6_trade.provider_fast_block_outbox projection
                      ON projection.id = latest.id
                    LEFT JOIN a6_trade.provider_quarantines quarantine
                      ON quarantine.id = projection.quarantine_id
                    ORDER BY projection.id
                    LIMIT %s
                    """,
                    (limit,),
                )
                rows = _rows(cursor)
        finally:
            _close(connection)

        events: list[dict[str, Any]] = []
        for row in rows:
            operation = str(row.get("operation") or "")
            quarantine_state = str(row.get("quarantine_state") or "")
            if (
                row.get("quarantine_id") is None
                or (operation == "block" and quarantine_state not in {"active", "probing"})
                or (operation == "unblock" and quarantine_state != "closed")
            ):
                raise PublicationControlError("provider_fast_block_rebuild_state_invalid")
            try:
                result: ProviderFastBlockProjectionResult = self.fast_block_projector.rebuild(
                    _fast_block_projection(row)
                )
            except Exception as exc:
                events.append(
                    {
                        "id": int(row["id"]),
                        "operation": operation,
                        "projection_key": str(row["projection_key"]),
                        "projection_state": "failed",
                        "changed": False,
                        "last_error_class": _error_class(exc),
                    }
                )
                continue
            events.append(
                {
                    "id": int(row["id"]),
                    "operation": operation,
                    "projection_key": str(row["projection_key"]),
                    "projection_state": result.state,
                    "changed": result.changed,
                    "last_error_class": None,
                }
            )
        return {
            "processed": len(events),
            "projected": sum(row["projection_state"] == "projected" for row in events),
            "superseded": sum(row["projection_state"] == "superseded" for row in events),
            "failed": sum(row["projection_state"] == "failed" for row in events),
            "changed": sum(bool(row["changed"]) for row in events),
            "events": events,
        }

    def _claim_fast_block(
        self,
        *,
        event_id: int | None,
        after_id: int,
    ) -> dict[str, Any] | None:
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    WITH candidate AS (
                        SELECT id
                        FROM a6_trade.provider_fast_block_outbox
                        WHERE id > %s
                          AND (%s::bigint IS NULL OR id = %s)
                          AND (
                              projection_state IN ('pending', 'failed')
                              OR (
                                  projection_state = 'running'
                                  AND updated_at <= now() - (%s * interval '1 second')
                              )
                          )
                        ORDER BY id
                        FOR UPDATE SKIP LOCKED
                        LIMIT 1
                    )
                    UPDATE a6_trade.provider_fast_block_outbox projection
                    SET projection_state = 'running',
                        attempt_count = projection.attempt_count + 1,
                        started_at = now(), projected_at = NULL,
                        last_error_class = NULL, updated_at = now()
                    FROM candidate
                    WHERE projection.id = candidate.id
                    RETURNING projection.id, projection.idempotency_key,
                              projection.operation, projection.model,
                              projection.routing_generation,
                              projection.cost_generation, projection.slot_no,
                              projection.channel_id, projection.attempt_id,
                              projection.quarantine_id, projection.projection_key,
                              projection.payload, projection.projection_state,
                              projection.attempt_count,
                              projection.failure_occurred_at,
                              projection.recovery_verified_at,
                              projection.last_error_class
                    """,
                    (
                        after_id,
                        event_id,
                        event_id,
                        self.fast_block_reclaim_seconds,
                    ),
                )
                row = _row(cursor)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            _close(connection)
        return row

    def _apply_claimed_fast_block(self, row: Mapping[str, Any]) -> dict[str, Any]:
        try:
            result: ProviderFastBlockProjectionResult = self.fast_block_projector.apply(
                _fast_block_projection(row)
            )
        except Exception as exc:
            return self._finish_fast_block(
                row,
                state="failed",
                error_class=_error_class(exc),
            )
        return self._finish_fast_block(row, state=result.state, error_class=None)

    def _finish_fast_block(
        self,
        claim: Mapping[str, Any],
        *,
        state: str,
        error_class: str | None,
    ) -> dict[str, Any]:
        if state not in {"projected", "superseded", "failed"}:
            raise PublicationControlError("provider_fast_block_finish_state_invalid")
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE a6_trade.provider_fast_block_outbox
                    SET projection_state = %s,
                        projected_at = CASE
                            WHEN %s IN ('projected', 'superseded') THEN now()
                            ELSE NULL
                        END,
                        last_error_class = %s,
                        updated_at = now()
                    WHERE id = %s AND projection_state = 'running'
                      AND attempt_count = %s
                    RETURNING id, idempotency_key, operation, model,
                              routing_generation, cost_generation, slot_no,
                              channel_id, attempt_id, quarantine_id,
                              projection_key, payload, projection_state,
                              attempt_count, failure_occurred_at,
                              recovery_verified_at, last_error_class
                    """,
                    (
                        state,
                        state,
                        error_class,
                        int(claim["id"]),
                        int(claim["attempt_count"]),
                    ),
                )
                row = _row(cursor)
                if row is None:
                    raise PublicationControlError("provider_fast_block_finish_cas_mismatch")
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            _close(connection)
        return self._projection_result(row)

    def _load_fast_block_result(self, event_id: int) -> dict[str, Any]:
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT id, idempotency_key, operation, model,
                           routing_generation, cost_generation, slot_no,
                           channel_id, attempt_id, quarantine_id,
                           projection_key, payload, projection_state,
                           attempt_count, failure_occurred_at,
                           recovery_verified_at, last_error_class
                    FROM a6_trade.provider_fast_block_outbox
                    WHERE id = %s
                    """,
                    (event_id,),
                )
                row = _row(cursor)
        finally:
            _close(connection)
        if row is None:
            raise PublicationControlError("provider_fast_block_event_missing")
        return self._projection_result(row)

    @staticmethod
    def _projection_result(row: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "id": int(row["id"]),
            "operation": str(row["operation"]),
            "projection_key": str(row["projection_key"]),
            "projection_state": str(row["projection_state"]),
            "attempt_count": int(row.get("attempt_count") or 0),
            "last_error_class": row.get("last_error_class"),
        }

    def claim_next(self, model: str) -> PublicationRequestClaim | None:
        model = _model(model)
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    WITH candidate AS (
                        SELECT id
                        FROM a6_trade.model_publication_reconcile_requests
                        WHERE model = %s
                          AND (
                              status IN ('pending', 'failed')
                              OR (
                                  status = 'running'
                                  AND updated_at <= now() - (%s * interval '1 second')
                              )
                          )
                        ORDER BY requested_at, id
                        FOR UPDATE SKIP LOCKED
                        LIMIT 1
                    )
                    UPDATE a6_trade.model_publication_reconcile_requests request
                    SET status = 'running', attempt_count = request.attempt_count + 1,
                        started_at = now(), completed_at = NULL,
                        last_error_class = NULL, updated_at = now()
                    FROM candidate
                    WHERE request.id = candidate.id
                    RETURNING request.id, request.idempotency_key, request.model,
                              request.expected_generation,
                              request.expected_cost_generation, request.request_source,
                              request.attempt_count
                    """,
                    (model, self.running_reclaim_seconds),
                )
                row = _row(cursor)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            _close(connection)
        if row is None:
            return None
        return PublicationRequestClaim(
            request_id=int(row["id"]),
            idempotency_key=str(row["idempotency_key"]),
            model=str(row["model"]),
            expected_generation=int(row["expected_generation"]),
            expected_cost_generation=(
                int(row["expected_cost_generation"])
                if row.get("expected_cost_generation") is not None
                else None
            ),
            request_source=str(row["request_source"]),
            attempt_count=int(row["attempt_count"]),
        )

    def complete(self, claim: PublicationRequestClaim, result: Mapping[str, Any]) -> None:
        self._finish(claim, status="completed", result=result, error_class=None)

    def fail(self, claim: PublicationRequestClaim, error_class: str) -> None:
        self._finish(
            claim,
            status="failed",
            result=None,
            error_class=_code(error_class, "publication_request_error_class_invalid"),
        )

    def _finish(
        self,
        claim: PublicationRequestClaim,
        *,
        status: str,
        result: Mapping[str, Any] | None,
        error_class: str | None,
    ) -> None:
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE a6_trade.model_publication_reconcile_requests
                    SET status = %s, result = %s::jsonb, last_error_class = %s,
                        completed_at = now(), updated_at = now()
                    WHERE id = %s AND status = 'running'
                      AND attempt_count = %s
                    RETURNING id, status
                    """,
                    (
                        status,
                        _json(dict(result)) if result is not None else None,
                        error_class,
                        claim.request_id,
                        claim.attempt_count,
                    ),
                )
                row = _row(cursor)
                if row is None or int(row["id"]) != claim.request_id:
                    raise PublicationControlError("publication_request_finish_cas_mismatch")
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            _close(connection)

    def render_metrics(self) -> str:
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT model, desired_state, observed_state, healthy_slots,
                           (
                               desired_state = observed_state
                               AND confirmed_at >= desired_at
                               AND (
                                   desired_state NOT IN ('published', 'degraded')
                                   OR last_real_probe_at >= desired_at
                               )
                           ) AS converged,
                           extract(epoch FROM (now() - desired_at))::double precision
                               AS desired_age_seconds,
                           CASE
                               WHEN confirmed_at IS NULL THEN NULL
                               ELSE extract(
                                   epoch FROM (confirmed_at - desired_at)
                               )::double precision
                           END AS transition_seconds
                    FROM a6_trade.model_publications
                    ORDER BY model
                    """
                )
                publications = _rows(cursor)
                cursor.execute(
                    """
                    SELECT model, status, count(*)::bigint AS request_count
                    FROM a6_trade.model_publication_reconcile_requests
                    GROUP BY model, status
                    ORDER BY model, status
                    """
                )
                requests = _rows(cursor)
                cursor.execute(
                    """
                    SELECT model, failure_owner,
                           failure_control_action AS control_action,
                           count(*)::bigint AS failure_count
                    FROM a6_trade.provider_attempts
                    WHERE state = 'failed'
                      AND failure_owner IS NOT NULL
                      AND failure_control_action IS NOT NULL
                    GROUP BY model, failure_owner, failure_control_action
                    ORDER BY model, failure_owner, failure_control_action
                    """
                )
                failures = _rows(cursor)
                cursor.execute(
                    """
                    SELECT model, scope_type, state,
                           count(*)::bigint AS quarantine_count
                    FROM a6_trade.provider_quarantines
                    GROUP BY model, scope_type, state
                    ORDER BY model, scope_type, state
                    """
                )
                quarantines = _rows(cursor)
                cursor.execute(
                    """
                    SELECT model, projection_state,
                           count(*)::bigint AS projection_count,
                           max(
                               CASE
                                   WHEN projection_state IN (
                                       'pending', 'running', 'failed'
                                   ) THEN extract(epoch FROM (now() - requested_at))
                                   ELSE 0
                               END
                           )::double precision AS oldest_age_seconds
                    FROM a6_trade.provider_fast_block_outbox
                    GROUP BY model, projection_state
                    ORDER BY model, projection_state
                    """
                )
                projections = _rows(cursor)
                cursor.execute(
                    """
                    WITH transitions AS (
                        SELECT model, request_source, status, result,
                               completed_at,
                               CASE request_source
                                   WHEN 'provider_failure' THEN
                                       (request_payload ->> 'occurred_at')::timestamptz
                                   WHEN 'repair' THEN
                                       (request_payload ->> 'verified_at')::timestamptz
                               END AS cause_at
                        FROM a6_trade.model_publication_reconcile_requests
                        WHERE request_source = 'repair'
                           OR (
                               request_source = 'provider_failure'
                               AND request_payload ->> 'fast_block_slot' = 'true'
                           )
                    )
                    SELECT model, request_source,
                           CASE
                               WHEN status = 'completed'
                                AND completed_at >= cause_at
                                AND (
                                    (
                                        request_source = 'provider_failure'
                                        AND result ->> 'desired_state' = 'unpublished'
                                        AND result ->> 'observed_state' = 'unpublished'
                                    )
                                    OR (
                                        request_source = 'repair'
                                        AND result ->> 'desired_state'
                                            IN ('published', 'degraded')
                                        AND result ->> 'observed_state' =
                                            result ->> 'desired_state'
                                    )
                                )
                               THEN 'confirmed'
                               ELSE 'pending'
                           END AS outcome,
                           max(
                               extract(
                                   epoch FROM (
                                       CASE
                                           WHEN status = 'completed'
                                            AND completed_at >= cause_at
                                           THEN completed_at
                                           ELSE now()
                                       END - cause_at
                                   )
                               )
                           )::double precision AS transition_seconds
                    FROM transitions
                    WHERE cause_at IS NOT NULL
                      AND NOT (
                          request_source = 'provider_failure'
                          AND status = 'completed'
                          AND COALESCE(result ->> 'desired_state', '') <> 'unpublished'
                      )
                    GROUP BY model, request_source, outcome
                    ORDER BY model, request_source, outcome
                    """
                )
                failure_transitions = _rows(cursor)
        finally:
            _close(connection)
        lines = [
            "# HELP a6_model_publication_state Current desired publication state.",
            "# TYPE a6_model_publication_state gauge",
        ]
        states = ("published", "degraded", "unpublished", "repairing")
        for row in publications:
            model = _metric_label(row.get("model"))
            desired = str(row.get("desired_state") or "")
            for state in states:
                lines.append(
                    f'a6_model_publication_state{{model="{model}",state="{state}"}} '
                    f"{1 if desired == state else 0}"
                )
            converged = bool(row.get("converged"))
            mismatch = max(float(row.get("desired_age_seconds") or 0), 0) if not converged else 0
            outcome = "confirmed" if converged else "pending"
            transition = row.get("transition_seconds")
            transition_value = (
                max(float(transition), 0)
                if transition is not None and outcome == "confirmed"
                else mismatch
            )
            lines.append(
                f'a6_model_publication_transition_seconds{{model="{model}",'
                f'to_state="{_metric_label(desired)}",outcome="{outcome}"}} '
                f"{transition_value:.6f}"
            )
            lines.append(
                f'a6_model_publication_desired_observed_mismatch_seconds{{model="{model}"}} '
                f"{mismatch:.6f}"
            )
            lines.append(
                f'a6_provider_pool_healthy_slots{{model="{model}"}} '
                f"{max(int(row.get('healthy_slots') or 0), 0)}"
            )
        lines.extend(
            (
                "# HELP a6_model_publication_reconcile_requests "
                "Durable reconcile requests by state.",
                "# TYPE a6_model_publication_reconcile_requests gauge",
            )
        )
        for row in requests:
            lines.append(
                "a6_model_publication_reconcile_requests"
                f'{{model="{_metric_label(row.get("model"))}",'
                f'status="{_metric_label(row.get("status"))}"}} '
                f"{max(int(row.get('request_count') or 0), 0)}"
            )
        lines.extend(
            (
                "# HELP a6_provider_failure_events Canonical failed attempts by owner and action.",
                "# TYPE a6_provider_failure_events gauge",
            )
        )
        for row in failures:
            lines.append(
                "a6_provider_failure_events"
                f'{{model="{_metric_label(row.get("model"))}",'
                f'owner="{_metric_label(row.get("failure_owner"))}",'
                f'action="{_metric_label(row.get("control_action"))}"}} '
                f"{max(int(row.get('failure_count') or 0), 0)}"
            )
        lines.extend(
            (
                "# HELP a6_provider_quarantines Durable quarantines by scope and state.",
                "# TYPE a6_provider_quarantines gauge",
            )
        )
        for row in quarantines:
            lines.append(
                "a6_provider_quarantines"
                f'{{model="{_metric_label(row.get("model"))}",'
                f'scope_type="{_metric_label(row.get("scope_type"))}",'
                f'state="{_metric_label(row.get("state"))}"}} '
                f"{max(int(row.get('quarantine_count') or 0), 0)}"
            )
        lines.extend(
            (
                "# HELP a6_provider_fast_block_projection_events Durable projection events.",
                "# TYPE a6_provider_fast_block_projection_events gauge",
                "# HELP a6_provider_fast_block_projection_oldest_age_seconds "
                "Oldest unconfirmed projection age.",
                "# TYPE a6_provider_fast_block_projection_oldest_age_seconds gauge",
            )
        )
        for row in projections:
            model = _metric_label(row.get("model"))
            state = _metric_label(row.get("projection_state"))
            lines.append(
                "a6_provider_fast_block_projection_events"
                f'{{model="{model}",state="{state}"}} '
                f"{max(int(row.get('projection_count') or 0), 0)}"
            )
            lines.append(
                "a6_provider_fast_block_projection_oldest_age_seconds"
                f'{{model="{model}",state="{state}"}} '
                f"{max(float(row.get('oldest_age_seconds') or 0), 0):.6f}"
            )
        lines.extend(
            (
                "# HELP a6_provider_publication_control_transition_seconds "
                "Failure-to-unpublish or repair-to-publish transition time.",
                "# TYPE a6_provider_publication_control_transition_seconds gauge",
            )
        )
        for row in failure_transitions:
            transition = (
                "unpublish" if row.get("request_source") == "provider_failure" else "publish"
            )
            lines.append(
                "a6_provider_publication_control_transition_seconds"
                f'{{model="{_metric_label(row.get("model"))}",'
                f'transition="{transition}",'
                f'outcome="{_metric_label(row.get("outcome"))}"}} '
                f"{max(float(row.get('transition_seconds') or 0), 0):.6f}"
            )
        return "\n".join(lines) + "\n"

    def _wake(self, model: str, request_id: int) -> bool:
        if self.trigger is None:
            return False
        try:
            self.trigger.wake(model=model, request_id=request_id)
        except PublicationControlError:
            return False
        return True

    @staticmethod
    def _active_bundle(cursor: Any, model: str, *, expected_generation: int) -> tuple[int, int]:
        cursor.execute(
            """
            SELECT generation.generation, generation.cost_generation
            FROM a6_trade.provider_pool_generations generation
            JOIN a6_trade.provider_cost_generations cost
              ON cost.id = generation.cost_generation_id
             AND cost.model = generation.model
             AND cost.cost_generation = generation.cost_generation
             AND cost.pricing_state = 'active'
            WHERE generation.model = %s
              AND generation.generation_state = 'active'
            FOR SHARE OF generation, cost
            """,
            (model,),
        )
        row = _row(cursor)
        if row is None:
            raise PublicationControlError("publication_active_bundle_missing")
        generation = int(row["generation"])
        if generation != expected_generation:
            raise PublicationControlError("publication_expected_generation_conflict")
        return generation, int(row["cost_generation"])

    @staticmethod
    def _enqueue(
        cursor: Any,
        *,
        metadata: PublicationWriteMetadata,
        model: str,
        cost_generation: int,
        request_source: str,
        payload: Mapping[str, Any],
    ) -> tuple[dict[str, Any], bool]:
        key = f"publication-request:{metadata.idempotency_key}"
        encoded = _json(dict(payload))
        cursor.execute(
            """
            INSERT INTO a6_trade.model_publication_reconcile_requests (
                idempotency_key, model, expected_generation,
                expected_cost_generation, request_source, requested_by,
                trace_id, request_payload
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb)
            ON CONFLICT (idempotency_key) DO NOTHING
            RETURNING id, idempotency_key, model, expected_generation,
                      expected_cost_generation, request_source, requested_by,
                      trace_id, request_payload, status, attempt_count, result
            """,
            (
                key,
                model,
                metadata.expected_generation,
                cost_generation,
                request_source,
                metadata.requested_by,
                metadata.trace_id,
                encoded,
            ),
        )
        row = _row(cursor)
        deduplicated = row is None
        if row is None:
            cursor.execute(
                """
                SELECT id, idempotency_key, model, expected_generation,
                       expected_cost_generation, request_source, requested_by,
                       trace_id, request_payload, status, attempt_count, result
                FROM a6_trade.model_publication_reconcile_requests
                WHERE idempotency_key = %s
                FOR SHARE
                """,
                (key,),
            )
            row = _row(cursor)
        if row is None:
            raise PublicationControlError("publication_request_readback_missing")
        expected = {
            "idempotency_key": key,
            "model": model,
            "expected_generation": metadata.expected_generation,
            "expected_cost_generation": cost_generation,
            "request_source": request_source,
            "requested_by": metadata.requested_by,
            "trace_id": metadata.trace_id,
            "request_payload": dict(payload),
        }
        actual = {name: _normalized_json(row.get(name)) for name in expected}
        if actual != expected:
            raise PublicationControlError("publication_idempotency_conflict")
        return row, deduplicated

    @staticmethod
    def _verify_request_readback(cursor: Any, expected: Mapping[str, Any]) -> None:
        cursor.execute(
            """
            SELECT id, idempotency_key, model, expected_generation,
                   expected_cost_generation, request_source, requested_by,
                   trace_id, request_payload, status, attempt_count, result
            FROM a6_trade.model_publication_reconcile_requests
            WHERE id = %s
            """,
            (int(expected["id"]),),
        )
        observed = _row(cursor)
        if observed is None or {key: _normalized_json(observed.get(key)) for key in expected} != {
            key: _normalized_json(value) for key, value in expected.items()
        }:
            raise PublicationControlError("publication_request_strict_readback_mismatch")

    @staticmethod
    def _complete_failure_receipt(
        cursor: Any,
        request: Mapping[str, Any],
        *,
        deduplicated: bool,
    ) -> dict[str, Any]:
        result = {
            "publication_requested": False,
            "slot_drained": False,
        }
        if deduplicated:
            if (
                request.get("status") != "completed"
                or int(request.get("attempt_count") or 0) != 0
                or _normalized_json(request.get("result")) != result
            ):
                raise PublicationControlError("provider_failure_receipt_state_conflict")
            return dict(request)
        encoded = _json(result)
        cursor.execute(
            """
            UPDATE a6_trade.model_publication_reconcile_requests receipt
            SET status = 'completed', result = %s::jsonb,
                last_error_class = NULL,
                completed_at = COALESCE(receipt.completed_at, now()),
                updated_at = now()
            WHERE receipt.id = %s
              AND receipt.status = 'pending'
              AND receipt.attempt_count = 0
              AND receipt.result IS NULL
            RETURNING id, idempotency_key, model, expected_generation,
                      expected_cost_generation, request_source, requested_by,
                      trace_id, request_payload, status, attempt_count, result
            """,
            (encoded, int(request["id"])),
        )
        row = _row(cursor)
        if row is None:
            raise PublicationControlError("provider_failure_receipt_state_conflict")
        return row

    @staticmethod
    def _enqueue_fast_block(
        cursor: Any,
        event: ProviderAttemptFailureEvent,
        *,
        attempt_id: str,
    ) -> tuple[dict[str, Any], bool]:
        idempotency_key = f"fast-block:{event.metadata.idempotency_key}"
        projection_key = _fast_block_key(event)
        payload = _fast_block_payload(event)
        cursor.execute(
            """
            INSERT INTO a6_trade.provider_fast_block_outbox (
                idempotency_key, operation, model, routing_generation,
                cost_generation, slot_no, channel_id, attempt_id,
                projection_key, payload, failure_occurred_at
            ) VALUES (
                %s, 'block', %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s
            )
            ON CONFLICT (idempotency_key) DO NOTHING
            RETURNING id, idempotency_key, operation, model, routing_generation,
                      cost_generation, slot_no, channel_id, attempt_id,
                      quarantine_id, projection_key, payload, projection_state,
                      attempt_count, failure_occurred_at, recovery_verified_at,
                      last_error_class
            """,
            (
                idempotency_key,
                event.model,
                event.routing_generation,
                event.cost_generation,
                event.slot_no,
                event.channel_id,
                attempt_id,
                projection_key,
                _json(payload),
                event.occurred_at,
            ),
        )
        row = _row(cursor)
        deduplicated = row is None
        if row is None:
            cursor.execute(
                """
                SELECT id, idempotency_key, operation, model, routing_generation,
                       cost_generation, slot_no, channel_id, attempt_id,
                       quarantine_id, projection_key, payload, projection_state,
                       attempt_count, failure_occurred_at, recovery_verified_at,
                       last_error_class
                FROM a6_trade.provider_fast_block_outbox
                WHERE idempotency_key = %s
                FOR SHARE
                """,
                (idempotency_key,),
            )
            row = _row(cursor)
        if row is None:
            raise PublicationControlError("provider_fast_block_readback_missing")
        checks = {
            "idempotency_key": idempotency_key,
            "operation": "block",
            "model": event.model,
            "routing_generation": event.routing_generation,
            "cost_generation": event.cost_generation,
            "slot_no": event.slot_no,
            "channel_id": event.channel_id,
            "attempt_id": attempt_id,
            "projection_key": projection_key,
            "payload": payload,
            "failure_occurred_at": event.occurred_at,
            "recovery_verified_at": None,
        }
        if any(
            _normalized_json(row.get(name)) != _normalized_json(value)
            for name, value in checks.items()
        ):
            raise PublicationControlError("provider_fast_block_idempotency_conflict")
        return row, deduplicated

    @staticmethod
    def _upsert_quarantine(
        cursor: Any,
        event: ProviderAttemptFailureEvent,
    ) -> dict[str, Any]:
        scope_type, scope_id, region = _quarantine_identity(event)
        evidence_hash = hashlib.sha256(_json(_fast_block_payload(event)).encode()).hexdigest()
        cursor.execute(
            """
            INSERT INTO a6_trade.provider_quarantines (
                model, scope_type, scope_id, region, reason_code,
                failure_count, starts_at, next_probe_at, state, evidence_hash,
                failure_owner, classifier_version, control_action,
                routing_generation, cost_generation, slot_no, channel_id,
                endpoint_host, egress_id, first_failure_at, last_failure_at,
                updated_at
            ) VALUES (
                %s, %s, %s, %s, %s,
                1, %s, %s, 'active', %s,
                %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, now()
            )
            ON CONFLICT (
                model, scope_type, scope_id, (COALESCE(region, ''))
            ) WHERE state IN ('active', 'probing')
            DO UPDATE SET
                reason_code = EXCLUDED.reason_code,
                failure_count = a6_trade.provider_quarantines.failure_count + 1,
                starts_at = LEAST(
                    a6_trade.provider_quarantines.starts_at,
                    EXCLUDED.starts_at
                ),
                next_probe_at = GREATEST(
                    a6_trade.provider_quarantines.last_failure_at,
                    EXCLUDED.last_failure_at
                ) + (
                    (ARRAY[600, 1800, 7200, 21600])[
                        LEAST(a6_trade.provider_quarantines.failure_count + 1, 4)
                    ] * interval '1 second'
                ),
                ends_at = NULL,
                state = 'active',
                evidence_hash = EXCLUDED.evidence_hash,
                failure_owner = EXCLUDED.failure_owner,
                classifier_version = EXCLUDED.classifier_version,
                control_action = EXCLUDED.control_action,
                routing_generation = EXCLUDED.routing_generation,
                cost_generation = EXCLUDED.cost_generation,
                slot_no = EXCLUDED.slot_no,
                channel_id = EXCLUDED.channel_id,
                endpoint_host = EXCLUDED.endpoint_host,
                egress_id = EXCLUDED.egress_id,
                last_failure_at = GREATEST(
                    a6_trade.provider_quarantines.last_failure_at,
                    EXCLUDED.last_failure_at
                ),
                recovery_verified_at = NULL,
                recovery_evidence = '{}'::jsonb,
                updated_at = now()
            RETURNING id, model, scope_type, scope_id, region, reason_code,
                      failure_count, state, next_probe_at, failure_owner,
                      classifier_version, control_action, routing_generation,
                      cost_generation, slot_no, channel_id, endpoint_host,
                      egress_id, first_failure_at, last_failure_at
            """,
            (
                event.model,
                scope_type,
                scope_id,
                region,
                event.failure_code,
                event.occurred_at,
                event.occurred_at + timedelta(seconds=_QUARANTINE_BACKOFF_SECONDS[0]),
                evidence_hash,
                event.failure_owner,
                event.classifier_version,
                event.control_action,
                event.routing_generation,
                event.cost_generation,
                event.slot_no,
                event.channel_id,
                event.endpoint_host,
                event.egress_id,
                event.occurred_at,
                event.occurred_at,
            ),
        )
        row = _row(cursor)
        if row is None:
            raise PublicationControlError("provider_quarantine_upsert_failed")
        checks = {
            "model": event.model,
            "scope_type": scope_type,
            "scope_id": scope_id,
            "region": region,
            "reason_code": event.failure_code,
            "state": "active",
            "failure_owner": event.failure_owner,
            "classifier_version": event.classifier_version,
            "control_action": event.control_action,
            "routing_generation": event.routing_generation,
            "cost_generation": event.cost_generation,
            "slot_no": event.slot_no,
            "channel_id": event.channel_id,
            "endpoint_host": event.endpoint_host,
            "egress_id": event.egress_id,
        }
        if any(row.get(name) != value for name, value in checks.items()):
            raise PublicationControlError("provider_quarantine_readback_mismatch")
        return row

    @staticmethod
    def _link_projection_quarantine(
        cursor: Any,
        projection: Mapping[str, Any],
        quarantine: Mapping[str, Any],
    ) -> dict[str, Any]:
        cursor.execute(
            """
            UPDATE a6_trade.provider_fast_block_outbox
            SET quarantine_id = %s, updated_at = now()
            WHERE id = %s AND quarantine_id IS NULL
            RETURNING id, idempotency_key, operation, model, routing_generation,
                      cost_generation, slot_no, channel_id, attempt_id,
                      quarantine_id, projection_key, payload, projection_state,
                      attempt_count, failure_occurred_at, recovery_verified_at,
                      last_error_class
            """,
            (int(quarantine["id"]), int(projection["id"])),
        )
        row = _row(cursor)
        if row is None or int(row.get("quarantine_id") or 0) != int(quarantine["id"]):
            raise PublicationControlError("provider_fast_block_quarantine_link_failed")
        return row

    @staticmethod
    def _read_projection_quarantine(
        cursor: Any,
        projection: Mapping[str, Any],
        event: ProviderAttemptFailureEvent,
    ) -> dict[str, Any]:
        quarantine_id = projection.get("quarantine_id")
        if quarantine_id is None:
            raise PublicationControlError("provider_fast_block_quarantine_missing")
        cursor.execute(
            """
            SELECT id, model, scope_type, scope_id, region, reason_code,
                   failure_count, state, next_probe_at, failure_owner,
                   classifier_version, control_action, routing_generation,
                   cost_generation, slot_no, channel_id, endpoint_host,
                   egress_id, first_failure_at, last_failure_at
            FROM a6_trade.provider_quarantines
            WHERE id = %s
            FOR SHARE
            """,
            (int(quarantine_id),),
        )
        row = _row(cursor)
        scope_type, scope_id, region = _quarantine_identity(event)
        if row is None or any(
            row.get(name) != value
            for name, value in {
                "id": int(quarantine_id),
                "model": event.model,
                "scope_type": scope_type,
                "scope_id": scope_id,
                "region": region,
                "reason_code": event.failure_code,
                "failure_owner": event.failure_owner,
                "classifier_version": event.classifier_version,
                "control_action": event.control_action,
            }.items()
        ):
            raise PublicationControlError("provider_quarantine_readback_mismatch")
        return row

    @staticmethod
    def _read_fast_block_row(cursor: Any, event_id: int) -> dict[str, Any]:
        cursor.execute(
            """
            SELECT id, idempotency_key, operation, model, routing_generation,
                   cost_generation, slot_no, channel_id, attempt_id,
                   quarantine_id, projection_key, payload, projection_state,
                   attempt_count, failure_occurred_at, recovery_verified_at,
                   last_error_class
            FROM a6_trade.provider_fast_block_outbox
            WHERE id = %s
            FOR SHARE
            """,
            (event_id,),
        )
        row = _row(cursor)
        if row is None:
            raise PublicationControlError("provider_fast_block_event_missing")
        return row

    @staticmethod
    def _read_request_row(cursor: Any, request_id: int) -> dict[str, Any]:
        cursor.execute(
            """
            SELECT id, idempotency_key, model, expected_generation,
                   expected_cost_generation, request_source, requested_by,
                   trace_id, request_payload, status, attempt_count, result
            FROM a6_trade.model_publication_reconcile_requests
            WHERE id = %s
            FOR SHARE
            """,
            (request_id,),
        )
        row = _row(cursor)
        if row is None:
            raise PublicationControlError("publication_request_readback_missing")
        return row

    @staticmethod
    def _database_now(cursor: Any) -> datetime:
        cursor.execute("SELECT clock_timestamp() AS verified_at")
        row = _row(cursor)
        if row is None:
            raise PublicationControlError("provider_quarantine_database_time_missing")
        return _database_timestamp(
            row.get("verified_at"),
            "provider_quarantine_database_time_invalid",
        )

    @staticmethod
    def _load_recovery_slot(
        cursor: Any,
        recovery: Mapping[str, Any],
    ) -> dict[str, Any]:
        cursor.execute(
            """
            SELECT slot.id, slot.state_version, slot.model,
                   slot.routing_generation, slot.cost_generation, slot.slot_no,
                   slot.enodot_binding_id, slot.enodot_account_id,
                   slot.a6_token_id, slot.desired_channel_id,
                   slot.confirmed_pin_binding_id, slot.slot_state,
                   slot.liquidity_state, slot.funding_eligible,
                   slot.desired_schedulable, slot.observed_schedulable,
                   slot.pin_confirmed, slot.probe_confirmed,
                   slot.desired_slot_cost_multiplier::text
                       AS desired_slot_cost_multiplier,
                   slot.observed_rate_multiplier::text
                       AS observed_rate_multiplier,
                   token.status AS token_status,
                   token.key_fingerprint
            FROM a6_trade.provider_pool_generations generation
            JOIN a6_trade.provider_pool_slots slot
              ON slot.generation_id = generation.id
             AND slot.model = generation.model
             AND slot.routing_generation = generation.generation
             AND slot.cost_generation = generation.cost_generation
            JOIN a6_trade.a6_tokens token ON token.id = slot.a6_token_id
            WHERE generation.model = %s
              AND generation.generation = %s
              AND generation.cost_generation = %s
              AND generation.generation_state = 'active'
              AND slot.valid_to IS NULL
              AND slot.slot_no = %s
              AND slot.desired_channel_id = %s
            FOR UPDATE OF slot
            """,
            (
                str(recovery["model"]),
                int(recovery["expected_generation"]),
                int(recovery["expected_cost_generation"]),
                int(recovery["slot_no"]),
                int(recovery["channel_id"]),
            ),
        )
        slot = _row(cursor)
        if slot is None:
            raise PublicationControlError("provider_quarantine_slot_not_recoverable")
        identities = (
            "id",
            "state_version",
            "enodot_binding_id",
            "enodot_account_id",
            "a6_token_id",
            "desired_channel_id",
            "confirmed_pin_binding_id",
        )
        try:
            for name in identities:
                _positive_integer(slot.get(name), "provider_quarantine_slot_identity_invalid")
        except PublicationControlError:
            raise
        fingerprint = str(slot.get("key_fingerprint") or "").strip().casefold()
        if (
            slot.get("slot_state") != "DRAINING"
            or bool(slot.get("desired_schedulable"))
            or bool(slot.get("observed_schedulable"))
            or slot.get("funding_eligible") is not True
            or slot.get("liquidity_state") != "FUNDED"
            or slot.get("desired_slot_cost_multiplier") != slot.get("observed_rate_multiplier")
            or str(slot.get("token_status") or "").strip().casefold()
            not in {"1", "active", "enabled"}
            or not _SAFE_SHA256.fullmatch(fingerprint)
        ):
            raise PublicationControlError("provider_quarantine_slot_not_recoverable")
        slot["key_fingerprint"] = fingerprint
        return slot

    def _build_recovery_evidence(
        self,
        cursor: Any,
        recovery: Mapping[str, Any],
        slot: Mapping[str, Any],
        *,
        verified_at: datetime,
        claimed_at: datetime,
        last_failure_at: datetime,
    ) -> dict[str, Any]:
        evidence_cutoff = max(
            claimed_at,
            last_failure_at,
            verified_at - timedelta(seconds=self.recovery_evidence_max_age_seconds),
        )
        observer = self._observer_recovery_evidence(
            cursor,
            model=str(recovery["model"]),
            channel_id=int(recovery["channel_id"]),
            evidence_cutoff=evidence_cutoff,
            verified_at=verified_at,
        )
        pin = self._pin_recovery_evidence(
            cursor,
            slot=slot,
            evidence_cutoff=evidence_cutoff,
            verified_at=verified_at,
        )
        probe = self._responses_probe_recovery_evidence(
            cursor,
            recovery=recovery,
            slot=slot,
            evidence_cutoff=evidence_cutoff,
            verified_at=verified_at,
        )
        observer_ref = _evidence_ref("observer-quorum", observer)
        pin_ref = _evidence_ref("pin-readback", pin, identity=int(pin["binding_id"]))
        probe_ref = _evidence_ref("responses-probe", probe)
        return {
            "schema_version": 1,
            "verified_at": _iso_timestamp(verified_at),
            "evidence_cutoff": _iso_timestamp(evidence_cutoff),
            "recovery_id": int(recovery["id"]),
            "quarantine_id": int(recovery["quarantine_id"]),
            "probe_token": str(recovery["probe_token"]),
            "model": str(recovery["model"]),
            "routing_generation": int(recovery["expected_generation"]),
            "cost_generation": int(recovery["expected_cost_generation"]),
            "slot_no": int(recovery["slot_no"]),
            "channel_id": int(recovery["channel_id"]),
            "observer_quorum_ref": observer_ref,
            "pin_readback_ref": pin_ref,
            "real_probe_ref": probe_ref,
            "observer_quorum": observer,
            "pin_readback": pin,
            "responses_probe": probe,
        }

    def _observer_recovery_evidence(
        self,
        cursor: Any,
        *,
        model: str,
        channel_id: int,
        evidence_cutoff: datetime,
        verified_at: datetime,
    ) -> dict[str, Any]:
        cursor.execute(
            """
            SELECT health.observer_merchant_id, health.scan_run_id,
                   health.listing_id, health.health_state, health.available,
                   health.observed_at, scan.completed_at, scan.http_status,
                   scan.page_count, scan.candidate_count, scan.result_hash,
                   scan.result_payload
            FROM a6_trade.a6_channel_observer_health_current health
            JOIN a6_trade.a6_market_scan_runs scan
              ON scan.id = health.scan_run_id
             AND scan.model = health.model
             AND scan.observer_merchant_id = health.observer_merchant_id
            JOIN a6_trade.a6_merchants merchant
              ON merchant.id = health.observer_merchant_id
            WHERE health.model = %s
              AND health.channel_id = %s
              AND health.available
              AND health.health_state NOT IN ('cooling', 'unavailable')
              AND health.observed_at >= %s
              AND health.observed_at <= %s
              AND scan.status = 'completed'
              AND scan.completed_at IS NOT NULL
              AND scan.completed_at >= %s
              AND scan.completed_at <= %s
              AND scan.http_status BETWEEN 200 AND 299
              AND scan.page_count >= 1
              AND scan.result_payload IS NOT NULL
              AND 'observer' = ANY(merchant.roles)
              AND merchant.status = 'supplier_submitted'
            ORDER BY health.observer_merchant_id, health.scan_run_id
            LIMIT %s
            """,
            (
                model,
                channel_id,
                evidence_cutoff,
                verified_at,
                evidence_cutoff,
                verified_at,
                10,
            ),
        )
        rows = _rows(cursor)
        facts: list[dict[str, Any]] = []
        observer_ids: set[int] = set()
        for row in rows:
            observer_id = _positive_integer(
                row.get("observer_merchant_id"),
                "provider_quarantine_observer_evidence_invalid",
            )
            scan_id = _positive_integer(
                row.get("scan_run_id"),
                "provider_quarantine_observer_evidence_invalid",
            )
            if observer_id in observer_ids:
                raise PublicationControlError("provider_quarantine_observer_evidence_invalid")
            payload = _normalized_json(row.get("result_payload"))
            if not isinstance(payload, list) or len(payload) != int(
                row.get("candidate_count") or -1
            ):
                raise PublicationControlError("provider_quarantine_observer_evidence_invalid")
            result_hash = str(row.get("result_hash") or "").strip().casefold()
            computed_hash = hashlib.sha256(_json(payload).encode()).hexdigest()
            if not _SAFE_SHA256.fullmatch(result_hash) or not hmac.compare_digest(
                computed_hash, result_hash
            ):
                raise PublicationControlError("provider_quarantine_observer_hash_mismatch")
            payload_index = _observer_payload_index(
                payload,
                model=model,
                channel_id=channel_id,
                listing_id=row.get("listing_id"),
                health_state=str(row.get("health_state") or ""),
            )
            observed_at = _database_timestamp(
                row.get("observed_at"),
                "provider_quarantine_observer_time_invalid",
            )
            completed_at = _database_timestamp(
                row.get("completed_at"),
                "provider_quarantine_observer_time_invalid",
            )
            observer_ids.add(observer_id)
            facts.append(
                {
                    "observer_merchant_id": observer_id,
                    "scan_run_id": scan_id,
                    "listing_id": (
                        int(row["listing_id"]) if row.get("listing_id") is not None else None
                    ),
                    "health_state": str(row["health_state"]),
                    "available": True,
                    "observed_at": _iso_timestamp(observed_at),
                    "completed_at": _iso_timestamp(completed_at),
                    "http_status": int(row["http_status"]),
                    "page_count": int(row["page_count"]),
                    "candidate_count": int(row["candidate_count"]),
                    "result_hash": result_hash,
                    "payload_index": payload_index,
                }
            )
        if len(observer_ids) < self.minimum_recovery_observers:
            raise PublicationControlError("provider_quarantine_observer_quorum_missing")
        return {
            "schema_version": 1,
            "required_observers": self.minimum_recovery_observers,
            "observer_count": len(observer_ids),
            "facts": facts,
        }

    @staticmethod
    def _pin_recovery_evidence(
        cursor: Any,
        *,
        slot: Mapping[str, Any],
        evidence_cutoff: datetime,
        verified_at: datetime,
    ) -> dict[str, Any]:
        cursor.execute(
            """
            SELECT binding.id, binding.a6_token_id, binding.model,
                   binding.channel_id, binding.fallback_to_smart_routing,
                   binding.valid_from, binding.valid_to, binding.confirmed_at,
                   binding.change_reason
            FROM a6_trade.a6_pin_bindings binding
            WHERE binding.id = %s
              AND binding.a6_token_id = %s
              AND binding.model = %s
              AND binding.channel_id = %s
              AND binding.valid_to IS NULL
            FOR SHARE OF binding
            """,
            (
                int(slot["confirmed_pin_binding_id"]),
                int(slot["a6_token_id"]),
                str(slot["model"]),
                int(slot["desired_channel_id"]),
            ),
        )
        binding = _row(cursor)
        if binding is None or binding.get("fallback_to_smart_routing") is not False:
            raise PublicationControlError("provider_quarantine_pin_evidence_missing")
        confirmed_at = _database_timestamp(
            binding.get("confirmed_at"),
            "provider_quarantine_pin_evidence_invalid",
        )
        if not evidence_cutoff <= confirmed_at <= verified_at:
            raise PublicationControlError("provider_quarantine_pin_evidence_stale")
        return {
            "schema_version": 1,
            "binding_id": int(binding["id"]),
            "a6_token_id": int(binding["a6_token_id"]),
            "model": str(binding["model"]),
            "channel_id": int(binding["channel_id"]),
            "fallback_to_smart_routing": False,
            "valid_from": _iso_timestamp(
                _database_timestamp(
                    binding.get("valid_from"),
                    "provider_quarantine_pin_evidence_invalid",
                )
            ),
            "confirmed_at": _iso_timestamp(confirmed_at),
            "change_reason": str(binding.get("change_reason") or ""),
        }

    @staticmethod
    def _responses_probe_recovery_evidence(
        cursor: Any,
        *,
        recovery: Mapping[str, Any],
        slot: Mapping[str, Any],
        evidence_cutoff: datetime,
        verified_at: datetime,
    ) -> dict[str, Any]:
        cursor.execute(
            """
            SELECT to_regclass('public.a6_provider_request_outcomes')::text
                       AS outcome_table,
                   to_regclass('public.a6_provider_request_attempts')::text
                       AS attempt_table
            """
        )
        tables = _row(cursor)
        if tables is None or not tables.get("outcome_table") or not tables.get("attempt_table"):
            raise PublicationControlError("provider_quarantine_probe_ledger_missing")
        cursor.execute(
            """
            SELECT outcome.request_id, outcome.model, outcome.endpoint_kind,
                   outcome.stream, outcome.attribution_enabled,
                   outcome.routing_generation, outcome.cost_generation,
                   outcome.logical_status, outcome.logical_outcome,
                   outcome.winning_attempt_id,
                   outcome.response_committed AS outcome_response_committed,
                   outcome.delivery_state AS outcome_delivery_state,
                   outcome.received_at AS outcome_received_at,
                   outcome.committed_at AS outcome_committed_at,
                   outcome.completed_at AS outcome_completed_at,
                   attempt.attempt_id, attempt.attempt_no, attempt.account_id,
                   attempt.attempt_state, attempt.upstream_http_status,
                   attempt.terminal_event_seen,
                   attempt.response_committed AS attempt_response_committed,
                   attempt.delivery_state AS attempt_delivery_state,
                   attempt.routing_generation AS attempt_routing_generation,
                   attempt.cost_generation AS attempt_cost_generation,
                   attempt.provider_slot_no, attempt.channel_id,
                   attempt.a6_token_id, attempt.a6_token_fingerprint,
                   attempt.started_at AS attempt_started_at,
                   attempt.finished_at AS attempt_finished_at
            FROM public.a6_provider_request_outcomes outcome
            JOIN public.a6_provider_request_attempts attempt
              ON attempt.request_id = outcome.request_id
             AND attempt.attempt_id = outcome.winning_attempt_id
            WHERE outcome.request_id = %s
            FOR SHARE OF outcome, attempt
            """,
            (str(recovery["probe_token"]),),
        )
        probe = _row(cursor)
        if probe is None:
            raise PublicationControlError("provider_quarantine_probe_evidence_missing")
        expected = {
            "request_id": str(recovery["probe_token"]),
            "model": str(recovery["model"]),
            "endpoint_kind": "/v1/responses",
            "stream": False,
            "attribution_enabled": True,
            "routing_generation": int(recovery["expected_generation"]),
            "cost_generation": int(recovery["expected_cost_generation"]),
            "logical_status": 200,
            "logical_outcome": "success",
            "outcome_response_committed": True,
            "outcome_delivery_state": "delivered",
            "attempt_state": "won",
            "terminal_event_seen": True,
            "attempt_response_committed": True,
            "attempt_delivery_state": "delivered",
            "attempt_routing_generation": int(recovery["expected_generation"]),
            "attempt_cost_generation": int(recovery["expected_cost_generation"]),
            "provider_slot_no": int(slot["slot_no"]),
            "channel_id": int(slot["desired_channel_id"]),
            "a6_token_id": int(slot["a6_token_id"]),
            "a6_token_fingerprint": str(slot["key_fingerprint"]),
            "account_id": int(slot["enodot_account_id"]),
        }
        if any(probe.get(name) != value for name, value in expected.items()):
            raise PublicationControlError("provider_quarantine_probe_evidence_mismatch")
        upstream_status = int(probe.get("upstream_http_status") or 0)
        if not 200 <= upstream_status <= 299:
            raise PublicationControlError("provider_quarantine_probe_evidence_mismatch")
        times: dict[str, datetime] = {}
        for name in (
            "outcome_received_at",
            "outcome_committed_at",
            "outcome_completed_at",
            "attempt_started_at",
            "attempt_finished_at",
        ):
            observed = _database_timestamp(
                probe.get(name),
                "provider_quarantine_probe_evidence_time_invalid",
            )
            if not evidence_cutoff <= observed <= verified_at:
                raise PublicationControlError("provider_quarantine_probe_evidence_stale")
            times[name] = observed
        return {
            "schema_version": 1,
            "request_id": str(probe["request_id"]),
            "attempt_id": str(probe["attempt_id"]),
            "attempt_no": int(probe["attempt_no"]),
            "model": str(probe["model"]),
            "endpoint_kind": str(probe["endpoint_kind"]),
            "routing_generation": int(probe["routing_generation"]),
            "cost_generation": int(probe["cost_generation"]),
            "provider_slot_no": int(probe["provider_slot_no"]),
            "channel_id": int(probe["channel_id"]),
            "a6_token_id": int(probe["a6_token_id"]),
            "a6_token_fingerprint": str(probe["a6_token_fingerprint"]),
            "account_id": int(probe["account_id"]),
            "logical_status": int(probe["logical_status"]),
            "logical_outcome": str(probe["logical_outcome"]),
            "upstream_http_status": upstream_status,
            "outcome_delivery_state": str(probe["outcome_delivery_state"]),
            "attempt_delivery_state": str(probe["attempt_delivery_state"]),
            **{name: _iso_timestamp(value) for name, value in times.items()},
        }

    @staticmethod
    def _restore_quarantined_slot(
        cursor: Any,
        slot: Mapping[str, Any],
        *,
        verified_at: datetime,
    ) -> bool:
        cursor.execute(
            """
            UPDATE a6_trade.provider_pool_slots slot
            SET desired_schedulable = true,
                observed_schedulable = true,
                slot_state = 'READY',
                pin_confirmed = true,
                probe_confirmed = true,
                state_version = slot.state_version + 1,
                last_success_at = %s,
                updated_at = now()
            WHERE slot.id = %s
              AND slot.state_version = %s
              AND slot.valid_to IS NULL
              AND slot.slot_state = 'DRAINING'
              AND NOT slot.desired_schedulable
              AND NOT slot.observed_schedulable
              AND slot.funding_eligible
              AND slot.liquidity_state = 'FUNDED'
              AND slot.confirmed_pin_binding_id = %s
              AND slot.observed_rate_multiplier = slot.desired_slot_cost_multiplier
            RETURNING slot.id, slot.slot_state, slot.desired_schedulable,
                      slot.observed_schedulable, slot.state_version
            """,
            (
                verified_at,
                int(slot["id"]),
                int(slot["state_version"]),
                int(slot["confirmed_pin_binding_id"]),
            ),
        )
        row = _row(cursor)
        if row is None:
            raise PublicationControlError("provider_quarantine_slot_not_recoverable")
        if (
            row.get("slot_state") != "READY"
            or row.get("desired_schedulable") is not True
            or row.get("observed_schedulable") is not True
        ):
            raise PublicationControlError("provider_quarantine_slot_readback_mismatch")
        return True

    @staticmethod
    def _enqueue_fast_unblock(
        cursor: Any,
        recovery: Mapping[str, Any],
        *,
        metadata: PublicationWriteMetadata,
        verified_at: datetime,
        evidence_hash: str,
    ) -> tuple[dict[str, Any], bool]:
        model = str(recovery["model"])
        routing_generation = int(recovery["expected_generation"])
        cost_generation = int(recovery["expected_cost_generation"])
        slot_no = int(recovery["slot_no"])
        channel_id = int(recovery["channel_id"])
        projection_key = _fast_block_key_parts(
            model,
            routing_generation,
            slot_no,
            channel_id,
        )
        idempotency_key = f"fast-unblock:{metadata.idempotency_key}"
        payload = {
            "quarantine_id": int(recovery["quarantine_id"]),
            "recovery_id": int(recovery["id"]),
            "evidence_hash": evidence_hash,
            "verified_at": verified_at.isoformat().replace("+00:00", "Z"),
        }
        cursor.execute(
            """
            INSERT INTO a6_trade.provider_fast_block_outbox (
                idempotency_key, operation, model, routing_generation,
                cost_generation, slot_no, channel_id, quarantine_id,
                projection_key, payload, recovery_verified_at
            ) VALUES (
                %s, 'unblock', %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s
            )
            ON CONFLICT (idempotency_key) DO NOTHING
            RETURNING id, idempotency_key, operation, model, routing_generation,
                      cost_generation, slot_no, channel_id, attempt_id,
                      quarantine_id, projection_key, payload, projection_state,
                      attempt_count, failure_occurred_at, recovery_verified_at,
                      last_error_class
            """,
            (
                idempotency_key,
                model,
                routing_generation,
                cost_generation,
                slot_no,
                channel_id,
                int(recovery["quarantine_id"]),
                projection_key,
                _json(payload),
                verified_at,
            ),
        )
        row = _row(cursor)
        deduplicated = row is None
        if row is None:
            cursor.execute(
                """
                SELECT id, idempotency_key, operation, model, routing_generation,
                       cost_generation, slot_no, channel_id, attempt_id,
                       quarantine_id, projection_key, payload, projection_state,
                       attempt_count, failure_occurred_at, recovery_verified_at,
                       last_error_class
                FROM a6_trade.provider_fast_block_outbox
                WHERE idempotency_key = %s
                FOR SHARE
                """,
                (idempotency_key,),
            )
            row = _row(cursor)
        checks = {
            "idempotency_key": idempotency_key,
            "operation": "unblock",
            "model": model,
            "routing_generation": routing_generation,
            "cost_generation": cost_generation,
            "slot_no": slot_no,
            "channel_id": channel_id,
            "attempt_id": None,
            "quarantine_id": int(recovery["quarantine_id"]),
            "projection_key": projection_key,
            "payload": payload,
            "failure_occurred_at": None,
            "recovery_verified_at": verified_at,
        }
        if row is None or any(
            _normalized_json(row.get(name)) != _normalized_json(value)
            for name, value in checks.items()
        ):
            raise PublicationControlError("provider_fast_block_idempotency_conflict")
        return row, deduplicated

    @staticmethod
    def _lock_failure_slot(cursor: Any, event: ProviderAttemptFailureEvent) -> dict[str, Any]:
        cursor.execute(
            """
            SELECT slot.id, slot.slot_state, slot.desired_schedulable,
                   slot.observed_schedulable, slot.state_version
                   , slot.pin_assignment_epoch_id
            FROM a6_trade.provider_pool_slots slot
            JOIN a6_trade.provider_pool_generations generation
              ON generation.id = slot.generation_id
             AND generation.model = slot.model
             AND generation.generation = slot.routing_generation
             AND generation.cost_generation = slot.cost_generation
             AND generation.generation_state = 'active'
            WHERE slot.model = %s
              AND slot.routing_generation = %s
              AND slot.slot_no = %s
              AND slot.enodot_account_id = %s
              AND slot.a6_token_id = %s
              AND slot.desired_channel_id = %s
              AND slot.valid_to IS NULL
            FOR UPDATE OF slot
            """,
            (
                event.model,
                event.routing_generation,
                event.slot_no,
                event.enodot_account_id,
                event.a6_token_id,
                event.channel_id,
            ),
        )
        row = _row(cursor)
        if row is None:
            raise PublicationControlError("provider_failure_slot_identity_mismatch")
        if int(row.get("pin_assignment_epoch_id") or 0) < 1:
            raise PublicationControlError("provider_failure_pin_epoch_missing")
        return row

    @staticmethod
    def _record_liquidity_failure(cursor: Any, event: ProviderAttemptFailureEvent) -> None:
        cursor.execute(
            """
            UPDATE a6_trade.enodot_account_bindings
            SET last_error_class = %s, last_error_at = %s, updated_at = now()
            WHERE enodot_account_id = %s AND a6_token_id = %s
              AND state = 'bound'
            """,
            (
                event.failure_code,
                event.occurred_at,
                event.enodot_account_id,
                event.a6_token_id,
            ),
        )
        if cursor.rowcount != 1:
            raise PublicationControlError("provider_liquidity_binding_record_mismatch")
        cursor.execute(
            """
            UPDATE a6_trade.provider_inventory
            SET inventory_state = 'REPAIRING', liquidity_state = 'UNFUNDED',
                funding_eligible = false, schedulable = false,
                state_version = state_version + 1,
                updated_at = now()
            WHERE model = %s AND enodot_account_id = %s AND a6_token_id = %s
            """,
            (event.model, event.enodot_account_id, event.a6_token_id),
        )
        if cursor.rowcount != 1:
            raise PublicationControlError("provider_liquidity_inventory_record_mismatch")

    @classmethod
    def _apply_channel_failure_streak(
        cls,
        cursor: Any,
        event: ProviderAttemptFailureEvent,
        attempt: Mapping[str, Any],
        *,
        slot_row: Mapping[str, Any],
        threshold: int,
    ) -> tuple[ProviderAttemptFailureEvent, dict[str, Any]]:
        if event.failure_owner != "provider" or event.quarantine_scope != "channel":
            return event, dict(attempt)
        stored_threshold = int(attempt.get("failure_streak_threshold") or 0)
        if stored_threshold > 0:
            return cls._event_from_streak_attempt(event, attempt), dict(attempt)

        streak_count = cls._consecutive_channel_failure_count(
            cursor,
            pin_assignment_epoch_id=int(slot_row["pin_assignment_epoch_id"]),
            limit=threshold,
        )
        threshold_reached = (
            event.failure_owner == "provider"
            and streak_count >= threshold
            and str(slot_row.get("slot_state") or "") == "READY"
        )
        control_action = "repin_immediately" if threshold_reached else event.control_action
        fast_block_slot = threshold_reached
        quarantine_scope = "channel"
        cursor.execute(
            """
            UPDATE a6_trade.provider_attempts
            SET failure_control_action = %s,
                failure_quarantine_scope = %s,
                failure_streak_count = %s,
                failure_streak_threshold = %s,
                failure_streak_triggered = %s
            WHERE id = %s
              AND failure_streak_threshold = 0
            RETURNING id, attempt_id, request_id, attempt_no, model,
                      routing_generation, cost_generation, slot_no,
                      enodot_account_id, a6_token_id, channel_id, endpoint_host,
                      egress_id, state, http_status, failure_owner, failure_code,
                      failure_owner_declared,
                      failure_classifier_version_declared,
                      failure_classifier_version, failure_control_action,
                      failure_quarantine_scope, failure_streak_count,
                      failure_streak_threshold, failure_streak_triggered,
                      failure_occurred_at, terminal_event_seen,
                      response_committed, transport_status
            """,
            (
                control_action,
                quarantine_scope,
                streak_count,
                threshold,
                threshold_reached,
                int(attempt["id"]),
            ),
        )
        updated = _row(cursor)
        if updated is None:
            cursor.execute(
                """
                SELECT id, attempt_id, request_id, attempt_no, model,
                       routing_generation, cost_generation, slot_no,
                       enodot_account_id, a6_token_id, channel_id, endpoint_host,
                       egress_id, state, http_status, failure_owner, failure_code,
                       failure_owner_declared,
                       failure_classifier_version_declared,
                       failure_classifier_version, failure_control_action,
                       failure_quarantine_scope, failure_streak_count,
                       failure_streak_threshold, failure_streak_triggered,
                       failure_occurred_at, terminal_event_seen,
                       response_committed, transport_status
                FROM a6_trade.provider_attempts
                WHERE id = %s
                """,
                (int(attempt["id"]),),
            )
            updated = _row(cursor)
        if updated is None or int(updated.get("failure_streak_threshold") or 0) != threshold:
            raise PublicationControlError("provider_failure_streak_readback_mismatch")
        effective = cls._event_from_streak_attempt(event, updated)
        if (
            effective.fast_block_slot != fast_block_slot
            or effective.control_action != control_action
            or effective.quarantine_scope != quarantine_scope
        ):
            raise PublicationControlError("provider_failure_streak_readback_mismatch")
        return effective, updated

    @staticmethod
    def _consecutive_channel_failure_count(
        cursor: Any,
        *,
        pin_assignment_epoch_id: int,
        limit: int,
    ) -> int:
        cursor.execute(
            """
            SELECT model, enodot_account_id, started_at
            FROM a6_trade.provider_pin_assignment_epochs
            WHERE id = %s
            """,
            (pin_assignment_epoch_id,),
        )
        epoch = _row(cursor)
        if epoch is None:
            raise PublicationControlError("provider_failure_pin_epoch_missing")
        streak_started_at = epoch["started_at"]
        cursor.execute("SELECT to_regclass('public.usage_logs') AS usage_logs")
        usage_relation = _row(cursor)
        if usage_relation is not None and usage_relation.get("usage_logs") is not None:
            cursor.execute(
                """
                SELECT max(created_at) AS latest_success_at
                FROM public.usage_logs
                WHERE account_id = %s AND model = %s
                  AND created_at >= %s
                """,
                (
                    int(epoch["enodot_account_id"]),
                    str(epoch["model"]),
                    epoch["started_at"],
                ),
            )
            success = _row(cursor)
            if success is not None and success.get("latest_success_at") is not None:
                streak_started_at = max(streak_started_at, success["latest_success_at"])
        cursor.execute(
            """
            SELECT state, failure_owner, failure_classifier_version,
                   failure_quarantine_scope
            FROM a6_trade.provider_attempts
            WHERE pin_assignment_epoch_id = %s
              AND finished_at > %s
              AND state IN ('failed', 'won', 'lost', 'cancelled')
              AND terminal_seq IS NOT NULL
              AND NOT EXISTS (
                  SELECT 1
                  FROM a6_trade.provider_quarantine_recoveries recovery
                  WHERE recovery.probe_token =
                        a6_trade.provider_attempts.request_id
              )
            ORDER BY terminal_seq DESC
            LIMIT %s
            """,
            (pin_assignment_epoch_id, streak_started_at, 10_000),
        )
        streak = 0
        for row in _rows(cursor):
            if row.get("state") == "won":
                break
            if (
                row.get("state") == "failed"
                and row.get("failure_owner") == "provider"
                and str(row.get("failure_classifier_version") or "")
                and row.get("failure_quarantine_scope") == "channel"
            ):
                streak += 1
                if streak >= limit:
                    break
        return streak

    @staticmethod
    def _event_from_streak_attempt(
        event: ProviderAttemptFailureEvent,
        attempt: Mapping[str, Any],
    ) -> ProviderAttemptFailureEvent:
        threshold = int(attempt.get("failure_streak_threshold") or 0)
        triggered = bool(attempt.get("failure_streak_triggered"))
        return replace(
            event,
            fast_block_slot=triggered,
            control_action=str(attempt.get("failure_control_action") or event.control_action),
            quarantine_scope=(
                str(attempt.get("failure_quarantine_scope"))
                if attempt.get("failure_quarantine_scope") is not None
                else None
            ),
            channel_failure_streak_count=int(attempt.get("failure_streak_count") or 0),
            channel_failure_streak_threshold=threshold,
            channel_failure_threshold_reached=triggered,
        )

    @staticmethod
    def _record_attempt_failure(cursor: Any, event: ProviderAttemptFailureEvent) -> dict[str, Any]:
        cursor.execute(
            """
            INSERT INTO a6_trade.provider_attempts (
                attempt_id, request_envelope_id, request_id, attempt_no,
                model, routing_generation, cost_generation, slot_no,
                enodot_account_id, a6_token_id, channel_id, endpoint_host,
                egress_id, state, http_status, failure_owner, failure_code,
                failure_owner_declared, failure_classifier_version_declared,
                failure_classifier_version, failure_control_action,
                failure_quarantine_scope, failure_streak_count,
                failure_streak_threshold, failure_streak_triggered,
                failure_occurred_at,
                terminal_event_seen, response_committed, actual_cost_micros,
                transport_status, delivery_state, started_at, finished_at
            )
            SELECT %s, envelope.id, %s, %s, %s, %s, %s, %s,
                   %s, %s, %s, %s, %s, 'failed', %s, %s, %s,
                   %s, %s, %s, %s, %s, 0, 0, false, %s,
                   %s, false, 0, %s, 'not_started', %s, %s
            FROM a6_trade.provider_request_envelopes envelope
            WHERE envelope.request_id = %s
              AND envelope.model = %s
              AND envelope.routing_generation = %s
              AND envelope.cost_generation = %s
            ON CONFLICT (request_id, attempt_no) DO UPDATE SET
                state = 'failed', http_status = EXCLUDED.http_status,
                failure_owner = EXCLUDED.failure_owner,
                failure_code = EXCLUDED.failure_code,
                failure_owner_declared = EXCLUDED.failure_owner_declared,
                failure_classifier_version_declared =
                    EXCLUDED.failure_classifier_version_declared,
                failure_classifier_version = EXCLUDED.failure_classifier_version,
                failure_control_action = CASE
                    WHEN a6_trade.provider_attempts.failure_streak_triggered
                    THEN a6_trade.provider_attempts.failure_control_action
                    ELSE EXCLUDED.failure_control_action
                END,
                failure_quarantine_scope = CASE
                    WHEN a6_trade.provider_attempts.failure_streak_triggered
                    THEN a6_trade.provider_attempts.failure_quarantine_scope
                    ELSE EXCLUDED.failure_quarantine_scope
                END,
                failure_occurred_at = EXCLUDED.failure_occurred_at,
                terminal_event_seen = EXCLUDED.terminal_event_seen,
                transport_status = EXCLUDED.transport_status,
                finished_at = COALESCE(a6_trade.provider_attempts.finished_at,
                                       EXCLUDED.finished_at)
            WHERE a6_trade.provider_attempts.model = EXCLUDED.model
              AND a6_trade.provider_attempts.routing_generation = EXCLUDED.routing_generation
              AND a6_trade.provider_attempts.cost_generation = EXCLUDED.cost_generation
              AND a6_trade.provider_attempts.slot_no = EXCLUDED.slot_no
              AND a6_trade.provider_attempts.enodot_account_id = EXCLUDED.enodot_account_id
              AND a6_trade.provider_attempts.a6_token_id = EXCLUDED.a6_token_id
              AND a6_trade.provider_attempts.channel_id = EXCLUDED.channel_id
              AND a6_trade.provider_attempts.endpoint_host = EXCLUDED.endpoint_host
              AND a6_trade.provider_attempts.egress_id = EXCLUDED.egress_id
              AND NOT a6_trade.provider_attempts.response_committed
              AND a6_trade.provider_attempts.state IN (
                  'created', 'dispatched', 'buffered', 'failed'
              )
              AND (
                  a6_trade.provider_attempts.failure_owner IS NULL
                  OR a6_trade.provider_attempts.failure_owner = EXCLUDED.failure_owner
              )
              AND (
                  a6_trade.provider_attempts.failure_code IS NULL
                  OR a6_trade.provider_attempts.failure_code = EXCLUDED.failure_code
              )
              AND (
                  a6_trade.provider_attempts.failure_owner_declared IS NULL
                  OR a6_trade.provider_attempts.failure_owner_declared =
                     EXCLUDED.failure_owner_declared
              )
              AND (
                  a6_trade.provider_attempts.failure_classifier_version_declared IS NULL
                  OR a6_trade.provider_attempts.failure_classifier_version_declared =
                     EXCLUDED.failure_classifier_version_declared
              )
              AND (
                  a6_trade.provider_attempts.failure_classifier_version IS NULL
                  OR a6_trade.provider_attempts.failure_classifier_version =
                     EXCLUDED.failure_classifier_version
              )
            RETURNING id, attempt_id, request_id, attempt_no, model,
                      routing_generation, cost_generation, slot_no,
                      enodot_account_id, a6_token_id, channel_id, endpoint_host,
                      egress_id, state, http_status, failure_owner, failure_code,
                      failure_owner_declared,
                      failure_classifier_version_declared,
                      failure_classifier_version, failure_control_action,
                      failure_quarantine_scope, failure_streak_count,
                      failure_streak_threshold, failure_streak_triggered,
                      failure_occurred_at,
                      terminal_event_seen, response_committed, transport_status
            """,
            (
                event.attempt_id,
                event.request_id,
                event.attempt_no,
                event.model,
                event.routing_generation,
                event.cost_generation,
                event.slot_no,
                event.enodot_account_id,
                event.a6_token_id,
                event.channel_id,
                event.endpoint_host,
                event.egress_id,
                event.http_status,
                event.failure_owner,
                event.failure_code,
                event.failure_owner_declared,
                event.classifier_version_declared,
                event.classifier_version,
                event.control_action,
                event.quarantine_scope or ("slot" if event.fast_block_slot else None),
                event.occurred_at,
                event.terminal_event_seen,
                event.transport_status,
                event.occurred_at,
                event.occurred_at,
                event.request_id,
                event.model,
                event.routing_generation,
                event.cost_generation,
            ),
        )
        row = _row(cursor)
        if row is None:
            raise PublicationControlError("provider_failure_attempt_conflict_or_envelope_missing")
        return row

    @staticmethod
    def _verify_attempt_readback(
        cursor: Any,
        event: ProviderAttemptFailureEvent,
        expected: Mapping[str, Any],
    ) -> None:
        cursor.execute(
            """
            SELECT id, attempt_id, request_id, attempt_no, model,
                   routing_generation, cost_generation, slot_no,
                   enodot_account_id, a6_token_id, channel_id, endpoint_host,
                   egress_id, state, http_status, failure_owner, failure_code,
                   failure_owner_declared,
                   failure_classifier_version_declared,
                   failure_classifier_version, failure_control_action,
                   failure_quarantine_scope, failure_streak_count,
                   failure_streak_threshold, failure_streak_triggered,
                   failure_occurred_at,
                   terminal_event_seen, response_committed, transport_status
            FROM a6_trade.provider_attempts
            WHERE id = %s
            """,
            (int(expected["id"]),),
        )
        row = _row(cursor)
        checks = {
            "request_id": event.request_id,
            "attempt_no": event.attempt_no,
            "model": event.model,
            "routing_generation": event.routing_generation,
            "cost_generation": event.cost_generation,
            "slot_no": event.slot_no,
            "enodot_account_id": event.enodot_account_id,
            "a6_token_id": event.a6_token_id,
            "channel_id": event.channel_id,
            "endpoint_host": event.endpoint_host,
            "egress_id": event.egress_id,
            "state": "failed",
            "http_status": event.http_status,
            "failure_owner": event.failure_owner,
            "failure_code": event.failure_code,
            "failure_owner_declared": event.failure_owner_declared,
            "failure_classifier_version_declared": event.classifier_version_declared,
            "failure_classifier_version": event.classifier_version,
            "failure_control_action": event.control_action,
            "failure_quarantine_scope": event.quarantine_scope
            or ("slot" if event.fast_block_slot else None),
            "failure_streak_count": event.channel_failure_streak_count,
            "failure_streak_threshold": event.channel_failure_streak_threshold,
            "failure_streak_triggered": event.channel_failure_threshold_reached,
            "failure_occurred_at": event.occurred_at,
            "terminal_event_seen": event.terminal_event_seen,
            "response_committed": False,
            "transport_status": event.transport_status,
        }
        if row is None or any(row.get(key) != value for key, value in checks.items()):
            raise PublicationControlError("provider_failure_attempt_readback_mismatch")

    @staticmethod
    def _drain_slot(cursor: Any, event: ProviderAttemptFailureEvent) -> bool:
        cursor.execute(
            """
            UPDATE a6_trade.provider_pool_slots slot
            SET desired_schedulable = false,
                observed_schedulable = false,
                slot_state = CASE
                    WHEN slot.slot_state = 'VACANT' THEN 'VACANT'
                    ELSE 'DRAINING'
                END,
                state_version = slot.state_version + CASE
                    WHEN slot.desired_schedulable
                      OR slot.observed_schedulable
                      OR slot.slot_state NOT IN ('DRAINING', 'VACANT')
                    THEN 1 ELSE 0
                END,
                updated_at = now()
            FROM a6_trade.provider_pool_generations generation
            WHERE generation.id = slot.generation_id
              AND generation.model = %s
              AND generation.generation = %s
              AND generation.generation_state = 'active'
              AND slot.valid_to IS NULL
              AND slot.model = generation.model
              AND slot.routing_generation = generation.generation
              AND slot.cost_generation = %s
              AND slot.slot_no = %s
              AND slot.enodot_account_id = %s
              AND slot.a6_token_id = %s
              AND slot.desired_channel_id = %s
            RETURNING slot.id, slot.slot_state, slot.desired_schedulable,
                      slot.observed_schedulable, slot.state_version
            """,
            (
                event.model,
                event.routing_generation,
                event.cost_generation,
                event.slot_no,
                event.enodot_account_id,
                event.a6_token_id,
                event.channel_id,
            ),
        )
        row = _row(cursor)
        if row is None:
            raise PublicationControlError("provider_failure_slot_identity_mismatch")
        if (
            row.get("slot_state") not in {"DRAINING", "VACANT"}
            or bool(row.get("desired_schedulable"))
            or bool(row.get("observed_schedulable"))
        ):
            raise PublicationControlError("provider_failure_slot_readback_mismatch")
        return row.get("slot_state") == "DRAINING"


def _request_result(
    row: Mapping[str, Any], *, deduplicated: bool, wake_scheduled: bool
) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "idempotency_key": str(row["idempotency_key"]),
        "model": str(row["model"]),
        "expected_generation": int(row["expected_generation"]),
        "expected_cost_generation": int(row["expected_cost_generation"]),
        "request_source": str(row["request_source"]),
        "status": str(row["status"]),
        "attempt_count": int(row["attempt_count"]),
        "deduplicated": deduplicated,
        "wake_scheduled": wake_scheduled,
    }


def _failure_receipt_payload(
    event: ProviderAttemptFailureEvent, observed_attempt_id: str
) -> dict[str, Any]:
    return {
        "event_type": "provider_attempt_failed",
        "request_id": event.request_id,
        "attempt_id": observed_attempt_id,
        "attempt_no": event.attempt_no,
        "model": event.model,
        "routing_generation": event.routing_generation,
        "cost_generation": event.cost_generation,
        "slot_no": event.slot_no,
        "enodot_account_id": event.enodot_account_id,
        "a6_token_id": event.a6_token_id,
        "channel_id": event.channel_id,
        "endpoint_host": event.endpoint_host,
        "egress_id": event.egress_id,
        "region": event.region,
        "failure_owner": event.failure_owner,
        "failure_owner_declared": event.failure_owner_declared,
        "failure_code": event.failure_code,
        "http_status": event.http_status,
        "transport_status": event.transport_status,
        "terminal_event_seen": event.terminal_event_seen,
        "occurred_at": event.occurred_at.isoformat().replace("+00:00", "Z"),
        "classifier_version": event.classifier_version,
        "classifier_version_declared": event.classifier_version_declared,
        "retry_next": event.retry_next,
        "fast_block_slot": event.fast_block_slot,
        "control_action": event.control_action,
        "quarantine_scope": event.quarantine_scope,
        "channel_failure_streak_count": event.channel_failure_streak_count,
        "channel_failure_streak_threshold": event.channel_failure_streak_threshold,
        "channel_failure_threshold_reached": event.channel_failure_threshold_reached,
    }


def _fast_block_key(event: ProviderAttemptFailureEvent) -> str:
    return _fast_block_key_parts(
        event.model,
        event.routing_generation,
        event.slot_no,
        event.channel_id,
    )


def _fast_block_key_parts(
    model: str,
    routing_generation: int,
    slot_no: int,
    channel_id: int,
) -> str:
    return f"{_FAST_BLOCK_KEY_PREFIX}:{model}:{routing_generation}:{slot_no}:{channel_id}"


def _fast_block_payload(event: ProviderAttemptFailureEvent) -> dict[str, Any]:
    scope_type, scope_id, region = _quarantine_identity(event)
    return {
        "failure_owner": event.failure_owner,
        "failure_code": event.failure_code,
        "classifier_version": event.classifier_version,
        "control_action": event.control_action,
        "quarantine_scope_type": scope_type,
        "quarantine_scope_id": scope_id,
        "region": region,
        "endpoint_host": event.endpoint_host,
        "egress_id": event.egress_id,
        "occurred_at": event.occurred_at.isoformat().replace("+00:00", "Z"),
    }


def _quarantine_identity(event: ProviderAttemptFailureEvent) -> tuple[str, str, str | None]:
    scope_type = event.quarantine_scope or "slot"
    if scope_type == "channel":
        scope_id = str(event.channel_id)
    elif scope_type == "token":
        scope_id = str(event.a6_token_id)
    elif scope_type == "endpoint":
        scope_id = f"{event.endpoint_host}|{event.egress_id}"
    elif scope_type == "egress":
        scope_id = event.egress_id
    elif scope_type == "account":
        scope_id = str(event.enodot_account_id)
    elif scope_type == "region":
        scope_id = event.region
    else:
        scope_type = "slot"
        scope_id = f"{event.routing_generation}:{event.slot_no}:{event.channel_id}"
    region = event.region if scope_type in {"endpoint", "egress", "region"} else None
    return scope_type, scope_id, region


def _fast_block_projection(row: Mapping[str, Any]) -> ProviderFastBlockProjection:
    payload = _normalized_json(row.get("payload"))
    if not isinstance(payload, Mapping):
        raise PublicationControlError("provider_fast_block_payload_invalid")
    return ProviderFastBlockProjection(
        event_id=int(row["id"]),
        operation=str(row["operation"]),
        model=str(row["model"]),
        routing_generation=int(row["routing_generation"]),
        cost_generation=int(row["cost_generation"]),
        slot_no=int(row["slot_no"]),
        channel_id=int(row["channel_id"]),
        projection_key=str(row["projection_key"]),
        payload=dict(payload),
    )


def _quarantine_result(row: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "id": int(row["id"]),
        "scope_type": str(row["scope_type"]),
        "scope_id": str(row["scope_id"]),
        "region": row.get("region"),
        "state": str(row["state"]),
        "failure_count": int(row["failure_count"]),
        "next_probe_at": _iso_or_none(row.get("next_probe_at")),
    }


def _verify_recovery_replay(
    recovery: Mapping[str, Any],
    *,
    metadata: PublicationWriteMetadata,
) -> None:
    checks = {
        "confirmation_idempotency_key": metadata.idempotency_key,
        "confirmation_requested_by": metadata.requested_by,
        "confirmation_trace_id": metadata.trace_id,
        "quarantine_state": "closed",
    }
    if any(recovery.get(name) != value for name, value in checks.items()) or any(
        recovery.get(name) is None
        for name in (
            "verified_at",
            "observer_quorum_ref",
            "pin_readback_ref",
            "real_probe_ref",
            "evidence_hash",
            "unblock_projection_id",
            "publication_request_id",
        )
    ):
        raise PublicationControlError("provider_quarantine_recovery_idempotency_conflict")


def _recovery_evidence_result(recovery: Mapping[str, Any]) -> dict[str, Any]:
    evidence_hash = str(recovery.get("evidence_hash") or "").strip().casefold()
    if not _SAFE_SHA256.fullmatch(evidence_hash):
        raise PublicationControlError("provider_quarantine_recovery_evidence_invalid")
    return {
        "verified_at": _iso_timestamp(
            _database_timestamp(
                recovery.get("verified_at"),
                "provider_quarantine_recovery_evidence_invalid",
            )
        ),
        "observer_quorum_ref": _identifier(
            recovery.get("observer_quorum_ref"),
            "provider_quarantine_recovery_evidence_invalid",
        ),
        "pin_readback_ref": _identifier(
            recovery.get("pin_readback_ref"),
            "provider_quarantine_recovery_evidence_invalid",
        ),
        "real_probe_ref": _identifier(
            recovery.get("real_probe_ref"),
            "provider_quarantine_recovery_evidence_invalid",
        ),
        "evidence_hash": evidence_hash,
    }


def _evidence_ref(
    kind: str,
    evidence: Mapping[str, Any],
    *,
    identity: int | None = None,
) -> str:
    digest = hashlib.sha256(_json(dict(evidence)).encode()).hexdigest()
    prefix = f"{kind}:v1"
    return f"{prefix}:{identity}:{digest}" if identity is not None else f"{prefix}:{digest}"


def _observer_payload_index(
    payload: list[Any],
    *,
    model: str,
    channel_id: int,
    listing_id: object,
    health_state: str,
) -> int:
    expected_listing = int(listing_id) if listing_id is not None else None
    for index, raw in enumerate(payload):
        if not isinstance(raw, Mapping):
            continue
        try:
            observed_channel = _positive_integer(
                raw.get("channel_id"),
                "provider_quarantine_observer_evidence_invalid",
            )
            observed_listing = (
                _positive_integer(
                    raw.get("listing_id"),
                    "provider_quarantine_observer_evidence_invalid",
                )
                if raw.get("listing_id") is not None
                else None
            )
        except PublicationControlError:
            continue
        if (
            str(raw.get("model") or "") == model
            and observed_channel == channel_id
            and (expected_listing is None or observed_listing == expected_listing)
            and raw.get("available") is True
            and str(raw.get("health_state") or "") == health_state
        ):
            return index
    raise PublicationControlError("provider_quarantine_observer_payload_mismatch")


def _database_timestamp(value: object, code: str) -> datetime:
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, str) and len(value) <= 64:
        try:
            result = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            raise PublicationControlError(code) from None
    else:
        raise PublicationControlError(code)
    if result.tzinfo is None:
        raise PublicationControlError(code)
    return result.astimezone(UTC)


def _iso_timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _derived_probe_token(model: str, quarantine_id: int, idempotency_key: str) -> str:
    digest = hashlib.sha256(f"{model}:{quarantine_id}:{idempotency_key}".encode()).hexdigest()
    return f"probe-{digest[:40]}"


def _error_class(exc: Exception) -> str:
    code = str(getattr(exc, "code", "") or "").strip().casefold()
    if _SAFE_CODE.fullmatch(code):
        return code
    return type(exc).__name__.casefold()[:128]


def _iso_or_none(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    return str(value)


def _derived_attempt_id(request_id: str, attempt_no: int) -> str:
    digest = hashlib.sha256(f"{request_id}:{attempt_no}".encode()).hexdigest()
    return f"failure-{digest[:32]}"


def _reject_unknown(document: Mapping[str, Any], allowed: frozenset[str], code: str) -> None:
    if any(not isinstance(key, str) or key not in allowed for key in document):
        raise PublicationControlError(code)


def _model(value: object) -> str:
    result = str(value or "").strip()
    if not _SAFE_MODEL.fullmatch(result):
        raise PublicationControlError("publication_model_invalid")
    return result


def _identifier(value: object, code: str) -> str:
    result = str(value or "").strip()
    if not _SAFE_ID.fullmatch(result):
        raise PublicationControlError(code)
    return result


def _code(value: object, code: str) -> str:
    result = str(value or "").strip().casefold().replace("-", "_")
    if not _SAFE_CODE.fullmatch(result):
        raise PublicationControlError(code)
    return result


def _hostname(value: object) -> str:
    result = str(value or "").strip().lower()
    if result != "a6.a6api.com":
        raise PublicationControlError("provider_failure_endpoint_host_invalid")
    return result


def _positive_integer(value: object, code: str) -> int:
    if isinstance(value, bool):
        raise PublicationControlError(code)
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError):
        raise PublicationControlError(code) from None
    if not 1 <= result <= 9_223_372_036_854_775_807:
        raise PublicationControlError(code)
    return result


def _optional_http_status(value: object, code: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise PublicationControlError(code)
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError):
        raise PublicationControlError(code) from None
    if not 100 <= result <= 599:
        raise PublicationControlError(code)
    return result


def _timestamp(value: object) -> datetime:
    if not isinstance(value, str) or len(value) > 40:
        raise PublicationControlError("provider_failure_occurred_at_invalid")
    normalized = value.strip().replace("Z", "+00:00")
    try:
        result = datetime.fromisoformat(normalized)
    except ValueError:
        raise PublicationControlError("provider_failure_occurred_at_invalid") from None
    if result.tzinfo is None:
        raise PublicationControlError("provider_failure_occurred_at_invalid")
    return result.astimezone(UTC)


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _normalized_json(value: object) -> object:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return value
    if isinstance(value, Mapping):
        return {str(key): _normalized_json(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_normalized_json(item) for item in value]
    return value


def _row(cursor: Any) -> dict[str, Any] | None:
    row = cursor.fetchone()
    if row is None:
        return None
    if isinstance(row, Mapping):
        return dict(row)
    names = [column.name for column in cursor.description or ()]
    return dict(zip(names, row, strict=False))


def _rows(cursor: Any) -> list[dict[str, Any]]:
    rows = cursor.fetchall()
    if not rows:
        return []
    if isinstance(rows[0], Mapping):
        return [dict(row) for row in rows]
    names = [column.name for column in cursor.description or ()]
    return [dict(zip(names, row, strict=False)) for row in rows]


def _metric_label(value: object) -> str:
    return str(value or "").replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _close(connection: Any) -> None:
    close = getattr(connection, "close", None)
    if callable(close):
        close()
