-- Durable hand-off from request-plane failure classification to the remote
-- Provider replacement saga.  The queue stores identities and hashes only;
-- request bodies, upstream bodies, credentials, and API keys are excluded.

CREATE TABLE a6_trade.provider_failure_repair_requests (
    id bigserial PRIMARY KEY,
    attempt_id text NOT NULL UNIQUE
        REFERENCES a6_trade.provider_attempts(attempt_id),
    quarantine_id bigint NOT NULL
        REFERENCES a6_trade.provider_quarantines(id),
    control_action text NOT NULL,
    config_version text NOT NULL,
    model text NOT NULL,
    routing_generation bigint NOT NULL,
    cost_generation bigint NOT NULL,
    slot_no integer NOT NULL,
    enodot_binding_id bigint NOT NULL,
    enodot_account_id bigint NOT NULL,
    a6_token_id bigint NOT NULL,
    failed_channel_id bigint NOT NULL,
    desired_rank integer,
    desired_priority integer,
    desired_slot_cost_multiplier numeric(30,12),
    observed_rate_multiplier numeric(30,12),
    funding_eligible boolean NOT NULL,
    confirmed_pin_binding_id bigint,
    pin_confirmed boolean NOT NULL,
    probe_confirmed boolean NOT NULL,
    slot_state_version bigint NOT NULL,
    target_routing_generation bigint,
    target_cost_generation bigint,
    target_channel_id bigint,
    target_account_id bigint,
    target_token_id bigint,
    target_source_hash char(64),
    status text NOT NULL DEFAULT 'pending',
    attempt_count integer NOT NULL DEFAULT 0,
    claim_token text,
    claimed_by text,
    claimed_at timestamptz,
    next_attempt_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz,
    last_error_class text,
    result jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT provider_failure_repair_action_known CHECK (
        control_action IN ('repin_immediately', 'probe_then_repin')
    ),
    CONSTRAINT provider_failure_repair_status_known CHECK (
        status IN ('pending', 'running', 'completed', 'failed', 'superseded')
    ),
    CONSTRAINT provider_failure_repair_identity_positive CHECK (
        routing_generation > 0 AND cost_generation > 0 AND slot_no > 0
        AND enodot_binding_id > 0 AND enodot_account_id > 0
        AND a6_token_id > 0 AND failed_channel_id > 0
        AND slot_state_version > 0 AND attempt_count >= 0
    ),
    CONSTRAINT provider_failure_repair_target_complete CHECK (
        (
            target_routing_generation IS NULL
            AND target_cost_generation IS NULL
            AND target_channel_id IS NULL
            AND target_account_id IS NULL
            AND target_token_id IS NULL
            AND target_source_hash IS NULL
        )
        OR (
            target_routing_generation > 0
            AND target_cost_generation > 0
            AND target_channel_id > 0
            AND target_account_id > 0
            AND target_token_id > 0
            AND target_source_hash ~ '^[0-9a-f]{64}$'
        )
    ),
    CONSTRAINT provider_failure_repair_claim_complete CHECK (
        (status = 'running' AND claim_token IS NOT NULL
            AND claimed_by IS NOT NULL AND claimed_at IS NOT NULL)
        OR (status <> 'running' AND claim_token IS NULL
            AND claimed_by IS NULL AND claimed_at IS NULL)
    ),
    CONSTRAINT provider_failure_repair_completion_complete CHECK (
        (status = 'completed' AND completed_at IS NOT NULL)
        OR (status <> 'completed' AND completed_at IS NULL)
    ),
    CONSTRAINT provider_failure_repair_result_object CHECK (
        jsonb_typeof(result) = 'object'
    )
);

CREATE INDEX provider_failure_repair_claim_idx
    ON a6_trade.provider_failure_repair_requests (
        next_attempt_at, id
    )
    WHERE status IN ('pending', 'running');

CREATE UNIQUE INDEX provider_failure_repair_one_open_slot_idx
    ON a6_trade.provider_failure_repair_requests (
        model, routing_generation, slot_no
    )
    WHERE status IN ('pending', 'running');

CREATE INDEX provider_failure_repair_scope_idx
    ON a6_trade.provider_failure_repair_requests (
        model, routing_generation, slot_no, id DESC
    );

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'a6trader') THEN
        GRANT SELECT, INSERT, UPDATE
            ON a6_trade.provider_failure_repair_requests TO a6trader;
        GRANT USAGE, SELECT
            ON SEQUENCE a6_trade.provider_failure_repair_requests_id_seq TO a6trader;
    END IF;
END
$$;
