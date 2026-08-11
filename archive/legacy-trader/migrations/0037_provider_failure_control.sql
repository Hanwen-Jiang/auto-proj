-- Failure-control persistence for canonical classification, quarantine recovery,
-- and PostgreSQL-authoritative fast-block projection. Raw upstream bodies,
-- request payloads, credentials, and API keys are deliberately excluded.

ALTER TABLE a6_trade.provider_attempts
    ADD COLUMN IF NOT EXISTS failure_owner_declared text,
    ADD COLUMN IF NOT EXISTS failure_classifier_version_declared text,
    ADD COLUMN IF NOT EXISTS failure_classifier_version text,
    ADD COLUMN IF NOT EXISTS failure_control_action text,
    ADD COLUMN IF NOT EXISTS failure_quarantine_scope text,
    ADD COLUMN IF NOT EXISTS failure_occurred_at timestamptz;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'provider_attempt_failure_action_known'
          AND conrelid = 'a6_trade.provider_attempts'::regclass
    ) THEN
        ALTER TABLE a6_trade.provider_attempts
            ADD CONSTRAINT provider_attempt_failure_action_known CHECK (
                failure_control_action IS NULL
                OR failure_control_action IN (
                    'none', 'repin_immediately', 'probe_then_repin',
                    'isolate_path', 'repair_internal', 'open_incident'
                )
            );
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'provider_attempt_failure_scope_known'
          AND conrelid = 'a6_trade.provider_attempts'::regclass
    ) THEN
        ALTER TABLE a6_trade.provider_attempts
            ADD CONSTRAINT provider_attempt_failure_scope_known CHECK (
                failure_quarantine_scope IS NULL
                OR failure_quarantine_scope IN (
                    'slot', 'channel', 'token', 'endpoint',
                    'egress', 'account', 'region'
                )
            );
    END IF;
END
$$;

ALTER TABLE a6_trade.provider_quarantines
    ADD COLUMN IF NOT EXISTS failure_owner text,
    ADD COLUMN IF NOT EXISTS classifier_version text,
    ADD COLUMN IF NOT EXISTS control_action text,
    ADD COLUMN IF NOT EXISTS routing_generation bigint,
    ADD COLUMN IF NOT EXISTS cost_generation bigint,
    ADD COLUMN IF NOT EXISTS slot_no integer,
    ADD COLUMN IF NOT EXISTS channel_id bigint,
    ADD COLUMN IF NOT EXISTS endpoint_host text,
    ADD COLUMN IF NOT EXISTS egress_id text,
    ADD COLUMN IF NOT EXISTS first_failure_at timestamptz,
    ADD COLUMN IF NOT EXISTS last_failure_at timestamptz,
    ADD COLUMN IF NOT EXISTS last_probe_at timestamptz,
    ADD COLUMN IF NOT EXISTS last_probe_result text,
    ADD COLUMN IF NOT EXISTS recovery_verified_at timestamptz,
    ADD COLUMN IF NOT EXISTS recovery_evidence jsonb NOT NULL DEFAULT '{}'::jsonb;

UPDATE a6_trade.provider_quarantines
SET first_failure_at = COALESCE(first_failure_at, starts_at),
    last_failure_at = COALESCE(last_failure_at, starts_at)
WHERE first_failure_at IS NULL OR last_failure_at IS NULL;

ALTER TABLE a6_trade.provider_quarantines
    ALTER COLUMN first_failure_at SET NOT NULL,
    ALTER COLUMN last_failure_at SET NOT NULL;

ALTER TABLE a6_trade.provider_quarantines
    DROP CONSTRAINT IF EXISTS provider_quarantine_scope_known,
    DROP CONSTRAINT IF EXISTS provider_quarantine_state_known;

ALTER TABLE a6_trade.provider_quarantines
    ADD CONSTRAINT provider_quarantine_scope_known CHECK (
        scope_type IN (
            'slot', 'channel', 'token', 'endpoint',
            'egress', 'account', 'region'
        )
    ),
    ADD CONSTRAINT provider_quarantine_state_known CHECK (
        state IN ('active', 'probing', 'closed')
    ),
    ADD CONSTRAINT provider_quarantine_owner_known CHECK (
        failure_owner IS NULL
        OR failure_owner IN (
            'user', 'client_transport', 'provider',
            'platform_path', 'internal', 'unknown'
        )
    ),
    ADD CONSTRAINT provider_quarantine_action_known CHECK (
        control_action IS NULL
        OR control_action IN (
            'none', 'repin_immediately', 'probe_then_repin',
            'isolate_path', 'repair_internal', 'open_incident'
        )
    ),
    ADD CONSTRAINT provider_quarantine_generation_positive CHECK (
        (routing_generation IS NULL OR routing_generation > 0)
        AND (cost_generation IS NULL OR cost_generation > 0)
        AND (slot_no IS NULL OR slot_no > 0)
        AND (channel_id IS NULL OR channel_id > 0)
    ),
    ADD CONSTRAINT provider_quarantine_recovery_evidence_object CHECK (
        jsonb_typeof(recovery_evidence) = 'object'
    );

DROP INDEX IF EXISTS a6_trade.provider_quarantine_active_idx;

CREATE UNIQUE INDEX provider_quarantine_active_idx
    ON a6_trade.provider_quarantines (
        model, scope_type, scope_id, COALESCE(region, '')
    )
    WHERE state IN ('active', 'probing');

CREATE INDEX provider_quarantine_probe_idx
    ON a6_trade.provider_quarantines (next_probe_at, id)
    WHERE state IN ('active', 'probing');

CREATE TABLE a6_trade.provider_fast_block_outbox (
    id bigserial PRIMARY KEY,
    idempotency_key text NOT NULL UNIQUE,
    operation text NOT NULL,
    model text NOT NULL,
    routing_generation bigint NOT NULL,
    cost_generation bigint NOT NULL,
    slot_no integer NOT NULL,
    channel_id bigint NOT NULL,
    attempt_id text REFERENCES a6_trade.provider_attempts(attempt_id),
    quarantine_id bigint REFERENCES a6_trade.provider_quarantines(id),
    projection_key text NOT NULL,
    payload jsonb NOT NULL,
    projection_state text NOT NULL DEFAULT 'pending',
    attempt_count integer NOT NULL DEFAULT 0,
    failure_occurred_at timestamptz,
    recovery_verified_at timestamptz,
    requested_at timestamptz NOT NULL DEFAULT now(),
    started_at timestamptz,
    projected_at timestamptz,
    last_error_class text,
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT provider_fast_block_operation_known CHECK (
        operation IN ('block', 'unblock')
    ),
    CONSTRAINT provider_fast_block_projection_state_known CHECK (
        projection_state IN ('pending', 'running', 'projected', 'failed', 'superseded')
    ),
    CONSTRAINT provider_fast_block_identity_positive CHECK (
        routing_generation > 0 AND cost_generation > 0
        AND slot_no > 0 AND channel_id > 0 AND attempt_count >= 0
    ),
    CONSTRAINT provider_fast_block_payload_object CHECK (
        jsonb_typeof(payload) = 'object'
    ),
    CONSTRAINT provider_fast_block_timestamps_valid CHECK (
        (operation = 'block' AND attempt_id IS NOT NULL
            AND failure_occurred_at IS NOT NULL)
        OR (operation = 'unblock' AND recovery_verified_at IS NOT NULL)
    )
);

CREATE INDEX provider_fast_block_pending_idx
    ON a6_trade.provider_fast_block_outbox (
        projection_state, requested_at, id
    )
    WHERE projection_state IN ('pending', 'running', 'failed');

CREATE INDEX provider_fast_block_scope_idx
    ON a6_trade.provider_fast_block_outbox (
        model, routing_generation, slot_no, channel_id, id DESC
    );

CREATE TABLE a6_trade.provider_quarantine_recoveries (
    id bigserial PRIMARY KEY,
    idempotency_key text NOT NULL UNIQUE,
    quarantine_id bigint NOT NULL REFERENCES a6_trade.provider_quarantines(id),
    model text NOT NULL,
    expected_generation bigint NOT NULL,
    expected_cost_generation bigint NOT NULL,
    probe_token text NOT NULL UNIQUE,
    status text NOT NULL DEFAULT 'probing',
    requested_by text NOT NULL,
    trace_id text NOT NULL,
    confirmation_idempotency_key text UNIQUE,
    confirmation_requested_by text,
    confirmation_trace_id text,
    claimed_at timestamptz NOT NULL DEFAULT now(),
    verified_at timestamptz,
    observer_quorum_ref text,
    pin_readback_ref text,
    real_probe_ref text,
    evidence_hash char(64),
    unblock_projection_id bigint REFERENCES a6_trade.provider_fast_block_outbox(id),
    publication_request_id bigint
        REFERENCES a6_trade.model_publication_reconcile_requests(id),
    last_error_class text,
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT provider_quarantine_recovery_status_known CHECK (
        status IN ('probing', 'verified', 'failed')
    ),
    CONSTRAINT provider_quarantine_recovery_generation_positive CHECK (
        expected_generation > 0 AND expected_cost_generation > 0
    ),
    CONSTRAINT provider_quarantine_recovery_evidence_complete CHECK (
        status <> 'verified'
        OR (
            verified_at IS NOT NULL
            AND observer_quorum_ref ~ '^observer-quorum:v1:[0-9a-f]{64}$'
            AND pin_readback_ref ~ '^pin-readback:v1:[1-9][0-9]*:[0-9a-f]{64}$'
            AND real_probe_ref ~ '^responses-probe:v1:[0-9a-f]{64}$'
            AND evidence_hash ~ '^[0-9a-f]{64}$'
            AND confirmation_idempotency_key IS NOT NULL
            AND confirmation_requested_by IS NOT NULL
            AND confirmation_trace_id IS NOT NULL
            AND unblock_projection_id IS NOT NULL
            AND publication_request_id IS NOT NULL
        )
    )
);

CREATE UNIQUE INDEX provider_quarantine_one_probe_idx
    ON a6_trade.provider_quarantine_recoveries (quarantine_id)
    WHERE status = 'probing';

CREATE INDEX provider_quarantine_recovery_verified_idx
    ON a6_trade.provider_quarantine_recoveries (model, verified_at DESC)
    WHERE status = 'verified';

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'a6trader') THEN
        GRANT SELECT, INSERT, UPDATE
            ON a6_trade.provider_attempts,
               a6_trade.provider_quarantines,
               a6_trade.provider_fast_block_outbox,
               a6_trade.provider_quarantine_recoveries TO a6trader;
        GRANT USAGE, SELECT
            ON SEQUENCE a6_trade.provider_fast_block_outbox_id_seq,
                        a6_trade.provider_quarantine_recoveries_id_seq TO a6trader;
        IF to_regclass('public.a6_provider_request_outcomes') IS NOT NULL THEN
            GRANT SELECT ON public.a6_provider_request_outcomes TO a6trader;
        END IF;
        IF to_regclass('public.a6_provider_request_attempts') IS NOT NULL THEN
            GRANT SELECT ON public.a6_provider_request_attempts TO a6trader;
        END IF;
    END IF;
END
$$;
