from __future__ import annotations

import hashlib
import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from a6api_auto.trade_db import apply_migrations
from a6api_auto.trade_provider_fast_block import ProviderFastBlockProjectionResult
from a6api_auto.trade_provider_pool_remote import PostgresProviderPinBindingStore
from a6api_auto.trade_provider_replay import PostgresReplayStore, RequestEnvelope
from a6api_auto.trade_publication_control import (
    PostgresPublicationControl,
    PublicationControlError,
)

psycopg = pytest.importorskip("psycopg")
dict_row = pytest.importorskip("psycopg.rows").dict_row

UTC = timezone.utc


class RecordingRebuilder:
    def __init__(self) -> None:
        self.events = []

    def apply(self, projection: Any) -> ProviderFastBlockProjectionResult:
        return self.rebuild(projection)

    def rebuild(self, projection: Any) -> ProviderFastBlockProjectionResult:
        self.events.append(projection)
        return ProviderFastBlockProjectionResult(
            event_id=projection.event_id,
            projection_key=projection.projection_key,
            state="projected",
            changed=True,
        )


def test_postgres_failure_quarantine_projection_and_recovery_round_trip() -> None:
    dsn = os.environ.get("A6_TEST_POSTGRES_DSN", "").strip()
    if not dsn:
        pytest.skip("A6_TEST_POSTGRES_DSN is not configured")
    suffix = uuid.uuid4().hex[:12]
    model = f"phase7-{suffix}"
    request_id = f"phase7-request-{suffix}"
    session_ref = f"phase7-session-{suffix}"
    account_id = int(suffix, 16)
    channel_id = account_id + 1

    def connect() -> Any:
        return psycopg.connect(dsn, row_factory=dict_row)

    with connect() as connection:
        _ensure_replay_ledger_tables(connection)
        apply_migrations(connection, Path("migrations"))
    merchant_id: int | None = None
    observer_merchant_ids: list[int] = []
    probe_request_id: str | None = None
    try:
        merchant_id, token_id = _insert_active_bundle(
            connect,
            model=model,
            session_ref=session_ref,
            account_id=account_id,
            channel_id=channel_id,
        )
        now = datetime.now(UTC)
        PostgresReplayStore(connect).create_envelope(
            RequestEnvelope(
                request_id=request_id,
                model=model,
                protocol="responses",
                method="POST",
                path="/v1/responses",
                headers={"content-type": "application/json"},
                body=b'{"model":"phase7-fixture"}',
                created_at=now - timedelta(minutes=11),
                routing_generation=1,
                cost_generation=1,
                max_attempts=2,
                max_total_seconds=60,
                max_retry_cost_micros=100,
                trace_id=f"trace-{suffix}",
            )
        )
        control = PostgresPublicationControl(connect)
        failure = {
            "event_type": "provider_attempt_failed",
            "idempotency_key": f"failure-{suffix}",
            "expected_generation": 1,
            "requested_by": "sub2api-test",
            "trace_id": f"trace-{suffix}",
            "request_id": request_id,
            "attempt_id": f"attempt-{suffix}",
            "attempt_no": 1,
            "model": model,
            "routing_generation": 1,
            "cost_generation": 1,
            "slot_no": 1,
            "enodot_account_id": account_id,
            "a6_token_id": token_id,
            "channel_id": channel_id,
            "endpoint_host": "a6.a6api.com",
            "egress_id": "direct",
            "region": "test",
            "failure_owner": "provider",
            "failure_code": "fixed_merchant_unavailable",
            "http_status": 503,
            "terminal_event_seen": False,
            "occurred_at": (now - timedelta(minutes=11)).isoformat().replace("+00:00", "Z"),
            "classifier_version": "a6-provider-request-classifier-v1",
        }

        reported = control.report_attempt_failure(failure)
        replayed = control.report_attempt_failure(failure)
        assert reported["quarantine"]["failure_count"] == 1
        assert replayed["quarantine"]["failure_count"] == 1

        with connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE a6_trade.provider_fast_block_outbox
                SET projection_state = 'projected', projected_at = now()
                WHERE id = %s
                """,
                (reported["fast_block_projection"]["id"],),
            )
            connection.commit()
        rebuilder = RecordingRebuilder()
        rebuild = PostgresPublicationControl(
            connect,
            fast_block_projector=rebuilder,
        ).rebuild_fast_block_cache()
        assert rebuild["failed"] == 0
        assert any(
            event.operation == "block" and event.model == model for event in rebuilder.events
        )

        quarantine_id = reported["quarantine"]["id"]
        probe = control.begin_quarantine_probe(
            model,
            {
                "idempotency_key": f"probe-{suffix}",
                "expected_generation": 1,
                "requested_by": "reconciler-test",
                "trace_id": f"repair-{suffix}",
                "quarantine_id": quarantine_id,
            },
        )
        confirmation = {
            "idempotency_key": f"recover-{suffix}",
            "expected_generation": 1,
            "requested_by": "reconciler-test",
            "trace_id": f"repair-{suffix}",
            "quarantine_id": quarantine_id,
            "probe_token": probe["probe_token"],
        }

        with pytest.raises(
            PublicationControlError,
            match="provider_quarantine_observer_quorum_missing",
        ):
            control.confirm_quarantine_recovery(model, confirmation)
        _assert_recovery_pending(connect, quarantine_id=quarantine_id)

        observer_merchant_ids = _insert_observer_evidence(
            connect,
            model=model,
            channel_id=channel_id,
            suffix=suffix,
        )
        scan_id, original_hash = _corrupt_observer_hash(connect, model=model)
        with pytest.raises(
            PublicationControlError,
            match="provider_quarantine_observer_hash_mismatch",
        ):
            control.confirm_quarantine_recovery(model, confirmation)
        _assert_recovery_pending(connect, quarantine_id=quarantine_id)
        _restore_observer_hash(
            connect,
            scan_id=scan_id,
            result_hash=original_hash,
        )
        with pytest.raises(
            PublicationControlError,
            match="provider_quarantine_pin_evidence_stale",
        ):
            control.confirm_quarantine_recovery(model, confirmation)
        _assert_recovery_pending(connect, quarantine_id=quarantine_id)

        _refresh_pin_evidence(
            connect,
            model=model,
            token_id=token_id,
            channel_id=channel_id,
        )
        with pytest.raises(
            PublicationControlError,
            match="provider_quarantine_probe_evidence_missing",
        ):
            control.confirm_quarantine_recovery(model, confirmation)
        _assert_recovery_pending(connect, quarantine_id=quarantine_id)

        probe_request_id = str(probe["probe_token"])
        _insert_probe_evidence(
            connect,
            request_id=probe_request_id,
            model=model,
            account_id=account_id,
            token_id=token_id,
            channel_id=channel_id + 99,
        )
        with pytest.raises(
            PublicationControlError,
            match="provider_quarantine_probe_evidence_mismatch",
        ):
            control.confirm_quarantine_recovery(model, confirmation)
        _assert_recovery_pending(connect, quarantine_id=quarantine_id)
        _set_probe_channel(connect, request_id=probe_request_id, channel_id=channel_id)

        recovered = control.confirm_quarantine_recovery(model, confirmation)
        recovery_replay = control.confirm_quarantine_recovery(model, confirmation)

        assert recovered["slot_restored"] is True
        assert recovery_replay["deduplicated"] is True
        assert recovered["evidence"]["observer_quorum_ref"].startswith(
            "observer-quorum:v1:"
        )
        assert recovered["evidence"]["pin_readback_ref"].startswith("pin-readback:v1:")
        assert recovered["evidence"]["real_probe_ref"].startswith("responses-probe:v1:")
        with connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                    SELECT quarantine.state, quarantine.failure_count,
                           slot.slot_state, slot.desired_schedulable,
                           slot.observed_schedulable,
                           (
                               SELECT count(*)
                               FROM a6_trade.provider_fast_block_outbox projection
                               WHERE projection.quarantine_id = quarantine.id
                           ) AS projection_count,
                           (
                               SELECT count(*)
                               FROM a6_trade.model_publication_reconcile_requests request
                               WHERE request.model = quarantine.model
                                 AND request.request_source IN (
                                     'provider_failure', 'repair'
                                 )
                           ) AS request_count
                    FROM a6_trade.provider_quarantines quarantine
                    JOIN a6_trade.provider_pool_slots slot
                      ON slot.model = quarantine.model
                     AND slot.slot_no = quarantine.slot_no
                     AND slot.valid_to IS NULL
                    WHERE quarantine.id = %s
                    """,
                (quarantine_id,),
            )
            facts = cursor.fetchone()
            cursor.execute(
                """
                SELECT recovery.evidence_hash,
                       quarantine.recovery_evidence
                FROM a6_trade.provider_quarantine_recoveries recovery
                JOIN a6_trade.provider_quarantines quarantine
                  ON quarantine.id = recovery.quarantine_id
                WHERE recovery.quarantine_id = %s
                """,
                (quarantine_id,),
            )
            evidence_row = cursor.fetchone()
        assert facts == {
            "state": "closed",
            "failure_count": 1,
            "slot_state": "READY",
            "desired_schedulable": True,
            "observed_schedulable": True,
            "projection_count": 2,
            "request_count": 2,
        }
        canonical_evidence = json.dumps(
            evidence_row["recovery_evidence"],
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        assert hashlib.sha256(canonical_evidence).hexdigest() == evidence_row["evidence_hash"]
    finally:
        _cleanup(
            connect,
            model=model,
            session_ref=session_ref,
            merchant_id=merchant_id,
            observer_merchant_ids=observer_merchant_ids,
            probe_request_id=probe_request_id,
        )


def _insert_active_bundle(
    connect: Any,
    *,
    model: str,
    session_ref: str,
    account_id: int,
    channel_id: int,
) -> tuple[int, int]:
    with connect() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO a6_trade.a6_merchants (
                    session_ref, session_fingerprint, roles, status
                ) VALUES (%s, %s, ARRAY['provider'], 'active')
                RETURNING id
                """,
                (session_ref, "a" * 64),
            )
            merchant_id = int(cursor.fetchone()["id"])
            cursor.execute(
                """
                INSERT INTO a6_trade.a6_tokens (
                    merchant_id, a6_token_id, token_name, key_fingerprint, status
                ) VALUES (%s, %s, %s, %s, 'active')
                RETURNING id
                """,
                (merchant_id, account_id + 2, f"token-{model}", "e" * 64),
            )
            token_id = int(cursor.fetchone()["id"])
            cursor.execute(
                """
                INSERT INTO a6_trade.enodot_account_bindings (
                    a6_token_id, enodot_account_id, binding_uid, model_scope,
                    desired_priority, desired_concurrency, state
                ) VALUES (%s, %s, %s, ARRAY[%s], 2, 1, 'bound')
                RETURNING id
                """,
                (token_id, account_id, f"binding-{model}", model),
            )
            binding_id = int(cursor.fetchone()["id"])
            cursor.execute(
                """
                INSERT INTO a6_trade.a6_pin_bindings (
                    a6_token_id, model, channel_id, valid_from,
                    confirmed_at, change_reason
                ) VALUES (%s, %s, %s, now(), now(), 'phase7-test')
                RETURNING id
                """,
                (token_id, model, channel_id),
            )
            pin_id = int(cursor.fetchone()["id"])
            cursor.execute(
                """
                INSERT INTO a6_trade.provider_cost_generations (
                    model, cost_generation, cost_source,
                    cost_input_hash, cost_output_hash,
                    token_mix_version, exchange_rate_version,
                    active_slot_nos, desired_slot_costs,
                    route_cost_multiplier, retry_reserve_multiplier,
                    pool_cost_multiplier, billing_floor_multiplier,
                    transition_floor_multiplier,
                    observed_billing_floor_multiplier,
                    pricing_state, config_version, confirmed_at, activated_at
                ) VALUES (
                    %s, 1, 'phase7-test', %s, %s,
                    'phase7-token', 'phase7-fx', ARRAY[1], %s::jsonb,
                    1.25, 0.1, 1.25, 1.4, 1.4, 1.4,
                    'active', 'phase7-v1', now(), now()
                ) RETURNING id
                """,
                (model, "b" * 64, "c" * 64, '{"1":"1.25"}'),
            )
            cost_id = int(cursor.fetchone()["id"])
            cursor.execute(
                """
                INSERT INTO a6_trade.provider_pool_generations (
                    model, generation, generation_source, source_hash,
                    cost_generation_id, cost_generation, desired_size,
                    ordered_channel_ids, standby_channel_ids,
                    generation_state, observed_at, activated_at, config_version
                ) VALUES (
                    %s, 1, 'phase7-test', %s, %s, 1, 1,
                    ARRAY[%s]::bigint[], ARRAY[%s]::bigint[],
                    'active', now(), now(), 'phase7-v1'
                ) RETURNING id
                """,
                (model, "d" * 64, cost_id, channel_id, channel_id + 1),
            )
            generation_id = int(cursor.fetchone()["id"])
            cursor.execute(
                """
                INSERT INTO a6_trade.provider_pool_slots (
                    generation_id, model, routing_generation, slot_no,
                    slot_name, cost_generation_id, cost_generation,
                    enodot_binding_id, enodot_account_id, a6_token_id,
                    desired_channel_id, desired_rank, desired_priority,
                    desired_slot_cost_multiplier, observed_rate_multiplier,
                    liquidity_state, funding_eligible, slot_state,
                    desired_schedulable, observed_schedulable,
                    confirmed_pin_binding_id, pin_confirmed, probe_confirmed
                ) VALUES (
                    %s, %s, 1, 1, %s, %s, 1,
                    %s, %s, %s, %s, 1, 2, 1.25, 1.25,
                    'FUNDED', true, 'READY', true, true, %s, true, true
                )
                """,
                (
                    generation_id,
                    model,
                    f"slot-{model}",
                    cost_id,
                    binding_id,
                    account_id,
                    token_id,
                    channel_id,
                    pin_id,
                ),
            )
        connection.commit()
    return merchant_id, token_id


def _ensure_replay_ledger_tables(connection: Any) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS public.a6_provider_request_outcomes (
                request_id text PRIMARY KEY,
                api_key_id bigint NOT NULL,
                model text NOT NULL,
                endpoint_kind text NOT NULL,
                request_body_sha256 char(64) NOT NULL,
                request_body_bytes bigint NOT NULL,
                stream boolean NOT NULL,
                next_attempt_no bigint NOT NULL DEFAULT 0,
                logical_status integer,
                logical_outcome text NOT NULL DEFAULT 'in_progress',
                winning_attempt_id text,
                response_committed boolean NOT NULL DEFAULT false,
                delivery_state text NOT NULL DEFAULT 'not_started',
                received_at timestamptz NOT NULL,
                committed_at timestamptz,
                completed_at timestamptz,
                attribution_enabled boolean NOT NULL DEFAULT false,
                routing_generation bigint,
                cost_generation bigint,
                billing_disposition text NOT NULL DEFAULT 'legacy_untracked',
                created_at timestamptz NOT NULL DEFAULT now(),
                updated_at timestamptz NOT NULL DEFAULT now()
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS public.a6_provider_request_attempts (
                attempt_id text PRIMARY KEY,
                request_id text NOT NULL REFERENCES public.a6_provider_request_outcomes(
                    request_id
                ) ON DELETE CASCADE,
                attempt_no bigint NOT NULL,
                account_id bigint NOT NULL,
                attempt_state text NOT NULL DEFAULT 'started',
                upstream_http_status integer,
                terminal_event_seen boolean NOT NULL DEFAULT false,
                response_committed boolean NOT NULL DEFAULT false,
                delivery_state text NOT NULL DEFAULT 'not_started',
                routing_generation bigint,
                cost_generation bigint,
                provider_slot_no integer,
                channel_id bigint,
                a6_token_id bigint,
                a6_token_fingerprint text,
                planned_cost_micros bigint,
                started_at timestamptz NOT NULL,
                finished_at timestamptz,
                created_at timestamptz NOT NULL DEFAULT now(),
                updated_at timestamptz NOT NULL DEFAULT now()
            )
            """
        )
    connection.commit()


def _assert_recovery_pending(connect: Any, *, quarantine_id: int) -> None:
    with connect() as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT quarantine.state, slot.slot_state,
                   slot.desired_schedulable, slot.observed_schedulable,
                   (
                       SELECT count(*)
                       FROM a6_trade.provider_fast_block_outbox projection
                       WHERE projection.quarantine_id = quarantine.id
                         AND projection.operation = 'unblock'
                   ) AS unblock_count,
                   (
                       SELECT count(*)
                       FROM a6_trade.model_publication_reconcile_requests request
                       WHERE request.model = quarantine.model
                         AND request.request_source = 'repair'
                   ) AS repair_count
            FROM a6_trade.provider_quarantines quarantine
            JOIN a6_trade.provider_pool_slots slot
              ON slot.model = quarantine.model
             AND slot.slot_no = quarantine.slot_no
             AND slot.valid_to IS NULL
            WHERE quarantine.id = %s
            """,
            (quarantine_id,),
        )
        facts = cursor.fetchone()
    assert facts == {
        "state": "probing",
        "slot_state": "DRAINING",
        "desired_schedulable": False,
        "observed_schedulable": False,
        "unblock_count": 0,
        "repair_count": 0,
    }


def _insert_observer_evidence(
    connect: Any,
    *,
    model: str,
    channel_id: int,
    suffix: str,
) -> list[int]:
    payload = [
        {
            "available": True,
            "channel_id": channel_id,
            "health_state": "healthy",
            "model": model,
        }
    ]
    encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    result_hash = hashlib.sha256(encoded.encode()).hexdigest()
    merchant_ids: list[int] = []
    with connect() as connection:
        with connection.cursor() as cursor:
            for index in range(2):
                cursor.execute(
                    """
                    INSERT INTO a6_trade.a6_merchants (
                        session_ref, session_fingerprint, roles, status
                    ) VALUES (%s, %s, ARRAY['observer'], 'supplier_submitted')
                    RETURNING id
                    """,
                    (f"phase7-observer-{suffix}-{index}", f"{index + 1}" * 64),
                )
                merchant_id = int(cursor.fetchone()["id"])
                merchant_ids.append(merchant_id)
                cursor.execute(
                    """
                    INSERT INTO a6_trade.a6_market_scan_runs (
                        observer_merchant_id, model, started_at, completed_at,
                        page_count, candidate_count, http_status, result_hash,
                        result_payload, status
                    ) VALUES (
                        %s, %s, clock_timestamp(), clock_timestamp(),
                        1, 1, 200, %s, %s::jsonb, 'completed'
                    ) RETURNING id, completed_at
                    """,
                    (merchant_id, model, result_hash, encoded),
                )
                scan = cursor.fetchone()
                cursor.execute(
                    """
                    INSERT INTO a6_trade.a6_channel_observer_health_current (
                        model, channel_id, observer_merchant_id,
                        health_state, available, observed_at, scan_run_id
                    ) VALUES (%s, %s, %s, 'healthy', true, %s, %s)
                    """,
                    (model, channel_id, merchant_id, scan["completed_at"], scan["id"]),
                )
        connection.commit()
    return merchant_ids


def _refresh_pin_evidence(
    connect: Any,
    *,
    model: str,
    token_id: int,
    channel_id: int,
) -> None:
    binding_id = PostgresProviderPinBindingStore(connect).confirm(
        token_id,
        model,
        channel_id,
        change_reason="phase7-recovery-readback",
    )
    assert binding_id > 0


def _corrupt_observer_hash(connect: Any, *, model: str) -> tuple[int, str]:
    with connect() as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT id, result_hash
            FROM a6_trade.a6_market_scan_runs
            WHERE model = %s
            ORDER BY id
            LIMIT 1
            """,
            (model,),
        )
        row = cursor.fetchone()
        cursor.execute(
            "UPDATE a6_trade.a6_market_scan_runs SET result_hash = %s WHERE id = %s",
            ("0" * 64, row["id"]),
        )
        connection.commit()
    return int(row["id"]), str(row["result_hash"])


def _restore_observer_hash(
    connect: Any,
    *,
    scan_id: int,
    result_hash: str,
) -> None:
    with connect() as connection, connection.cursor() as cursor:
        cursor.execute(
            "UPDATE a6_trade.a6_market_scan_runs SET result_hash = %s WHERE id = %s",
            (result_hash, scan_id),
        )
        connection.commit()


def _insert_probe_evidence(
    connect: Any,
    *,
    request_id: str,
    model: str,
    account_id: int,
    token_id: int,
    channel_id: int,
) -> None:
    attempt_id = f"{request_id}-attempt-1"
    with connect() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO public.a6_provider_request_outcomes (
                    request_id, api_key_id, model, endpoint_kind,
                    request_body_sha256, request_body_bytes, stream,
                    next_attempt_no, logical_outcome, response_committed,
                    delivery_state, received_at, attribution_enabled,
                    routing_generation, cost_generation, billing_disposition
                ) VALUES (
                    %s, 1, %s, '/v1/responses', %s, 32, false,
                    1, 'in_progress', false, 'not_started', clock_timestamp(),
                    true, 1, 1, 'unresolved'
                )
                """,
                (request_id, model, "f" * 64),
            )
            cursor.execute(
                """
                INSERT INTO public.a6_provider_request_attempts (
                    attempt_id, request_id, attempt_no, account_id,
                    attempt_state, upstream_http_status, terminal_event_seen,
                    response_committed, delivery_state, routing_generation,
                    cost_generation, provider_slot_no, channel_id,
                    a6_token_id, a6_token_fingerprint, planned_cost_micros,
                    started_at, finished_at
                ) VALUES (
                    %s, %s, 1, %s, 'won', 200, true, true, 'delivered',
                    1, 1, 1, %s, %s, %s, 0,
                    clock_timestamp(), clock_timestamp()
                )
                """,
                (attempt_id, request_id, account_id, channel_id, token_id, "e" * 64),
            )
            cursor.execute(
                """
                UPDATE public.a6_provider_request_outcomes
                SET logical_status = 200, logical_outcome = 'success',
                    winning_attempt_id = %s, response_committed = true,
                    delivery_state = 'delivered',
                    committed_at = clock_timestamp(),
                    completed_at = clock_timestamp(), updated_at = clock_timestamp()
                WHERE request_id = %s
                """,
                (attempt_id, request_id),
            )
        connection.commit()


def _set_probe_channel(connect: Any, *, request_id: str, channel_id: int) -> None:
    with connect() as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            UPDATE public.a6_provider_request_attempts
            SET channel_id = %s, updated_at = clock_timestamp()
            WHERE request_id = %s
            """,
            (channel_id, request_id),
        )
        assert cursor.rowcount == 1
        connection.commit()


def _cleanup(
    connect: Any,
    *,
    model: str,
    session_ref: str,
    merchant_id: int | None,
    observer_merchant_ids: list[int],
    probe_request_id: str | None,
) -> None:
    with connect() as connection:
        with connection.cursor() as cursor:
            if probe_request_id is not None:
                cursor.execute(
                    "DELETE FROM public.a6_provider_request_attempts WHERE request_id = %s",
                    (probe_request_id,),
                )
                cursor.execute(
                    "DELETE FROM public.a6_provider_request_outcomes WHERE request_id = %s",
                    (probe_request_id,),
                )
            cursor.execute(
                "DELETE FROM a6_trade.a6_channel_observer_health_current WHERE model = %s",
                (model,),
            )
            cursor.execute(
                "DELETE FROM a6_trade.a6_market_scan_runs WHERE model = %s",
                (model,),
            )
            for table in (
                "provider_quarantine_recoveries",
                "provider_fast_block_outbox",
                "provider_quarantines",
                "model_publication_reconcile_requests",
                "provider_attempts",
                "provider_request_envelopes",
                "provider_pool_slots",
                "provider_pool_generations",
                "provider_cost_generations",
            ):
                cursor.execute(f"DELETE FROM a6_trade.{table} WHERE model = %s", (model,))
            cursor.execute("DELETE FROM a6_trade.a6_pin_bindings WHERE model = %s", (model,))
            cursor.execute(
                "DELETE FROM a6_trade.enodot_account_bindings WHERE %s = ANY(model_scope)",
                (model,),
            )
            if merchant_id is not None:
                cursor.execute(
                    "DELETE FROM a6_trade.a6_tokens WHERE merchant_id = %s",
                    (merchant_id,),
                )
            if observer_merchant_ids:
                cursor.execute(
                    "DELETE FROM a6_trade.a6_merchants WHERE id = ANY(%s)",
                    (observer_merchant_ids,),
                )
            cursor.execute(
                "DELETE FROM a6_trade.a6_merchants WHERE session_ref = %s",
                (session_ref,),
            )
        connection.commit()
