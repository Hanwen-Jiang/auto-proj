-- A6 Provider TopN/failover desired state and request-attempt facts.
-- PostgreSQL is authoritative; Redis/request-plane caches are projections.

CREATE TABLE IF NOT EXISTS a6_trade.provider_cost_generations (
    id bigserial PRIMARY KEY,
    model text NOT NULL,
    cost_generation bigint NOT NULL,
    parent_cost_generation bigint,
    cost_source text NOT NULL,
    cost_input_hash char(64) NOT NULL,
    cost_output_hash char(64) NOT NULL,
    token_mix_version text NOT NULL,
    exchange_rate_version text NOT NULL,
    quote_observed_at timestamptz,
    active_slot_nos integer[] NOT NULL DEFAULT '{}',
    desired_slot_costs jsonb NOT NULL DEFAULT '{}'::jsonb,
    route_cost_multiplier numeric(30,12) NOT NULL DEFAULT 0,
    retry_reserve_multiplier numeric(30,12) NOT NULL DEFAULT 0,
    realized_cost_p95_multiplier numeric(30,12),
    pool_cost_multiplier numeric(30,12) NOT NULL DEFAULT 0,
    margin_bps integer NOT NULL DEFAULT 1000,
    headroom_bps integer NOT NULL DEFAULT 500,
    billing_floor_multiplier numeric(30,12) NOT NULL DEFAULT 0,
    transition_floor_multiplier numeric(30,12) NOT NULL DEFAULT 0,
    observed_billing_floor_multiplier numeric(30,12),
    pricing_state text NOT NULL DEFAULT 'planned',
    config_version text NOT NULL,
    planned_at timestamptz NOT NULL DEFAULT now(),
    confirmed_at timestamptz,
    activated_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (model, cost_generation),
    CONSTRAINT provider_cost_generation_positive CHECK (cost_generation > 0),
    CONSTRAINT provider_cost_hashes_valid CHECK (
        cost_input_hash ~ '^[0-9a-fA-F]{64}$' AND cost_output_hash ~ '^[0-9a-fA-F]{64}$'
    ),
    CONSTRAINT provider_cost_state_known CHECK (
        pricing_state IN ('planned', 'applying', 'confirmed', 'active', 'failed', 'superseded')
    ),
    CONSTRAINT provider_cost_values_nonnegative CHECK (
        route_cost_multiplier >= 0 AND retry_reserve_multiplier >= 0
        AND pool_cost_multiplier >= 0 AND billing_floor_multiplier >= 0
        AND transition_floor_multiplier >= 0
        AND (realized_cost_p95_multiplier IS NULL OR realized_cost_p95_multiplier >= 0)
    ),
    CONSTRAINT provider_cost_inputs_objects CHECK (
        jsonb_typeof(desired_slot_costs) = 'object'
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS provider_cost_one_active_idx
    ON a6_trade.provider_cost_generations (model)
    WHERE pricing_state = 'active';

CREATE TABLE IF NOT EXISTS a6_trade.provider_pool_generations (
    id bigserial PRIMARY KEY,
    model text NOT NULL,
    generation bigint NOT NULL,
    parent_generation bigint,
    generation_source text NOT NULL,
    source_hash char(64) NOT NULL,
    cost_generation_id bigint NOT NULL
        REFERENCES a6_trade.provider_cost_generations(id),
    cost_generation bigint NOT NULL,
    desired_size integer NOT NULL,
    ordered_channel_ids bigint[] NOT NULL DEFAULT '{}',
    standby_channel_ids bigint[] NOT NULL DEFAULT '{}',
    generation_state text NOT NULL DEFAULT 'planned',
    observed_at timestamptz,
    planned_at timestamptz NOT NULL DEFAULT now(),
    activated_at timestamptz,
    config_version text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (model, generation),
    CONSTRAINT provider_pool_generation_positive CHECK (generation > 0),
    CONSTRAINT provider_pool_generation_size_valid CHECK (desired_size > 0),
    CONSTRAINT provider_pool_generation_hash_valid CHECK (source_hash ~ '^[0-9a-fA-F]{64}$'),
    CONSTRAINT provider_pool_generation_state_known CHECK (
        generation_state IN ('planned', 'applying', 'active', 'failed', 'superseded')
    ),
    CONSTRAINT provider_pool_generation_cost_pair_fk
        FOREIGN KEY (model, cost_generation)
        REFERENCES a6_trade.provider_cost_generations(model, cost_generation)
);

CREATE UNIQUE INDEX IF NOT EXISTS provider_pool_one_active_idx
    ON a6_trade.provider_pool_generations (model)
    WHERE generation_state = 'active';

CREATE TABLE IF NOT EXISTS a6_trade.provider_pool_slots (
    id bigserial PRIMARY KEY,
    generation_id bigint NOT NULL REFERENCES a6_trade.provider_pool_generations(id),
    model text NOT NULL,
    routing_generation bigint NOT NULL,
    slot_no integer NOT NULL,
    slot_name text NOT NULL,
    cost_generation_id bigint NOT NULL REFERENCES a6_trade.provider_cost_generations(id),
    cost_generation bigint NOT NULL,
    enodot_binding_id bigint,
    enodot_account_id bigint,
    a6_token_id bigint,
    desired_channel_id bigint,
    desired_rank integer,
    desired_priority integer,
    desired_slot_cost_multiplier numeric(30,12),
    observed_rate_multiplier numeric(30,12),
    liquidity_state text NOT NULL DEFAULT 'FUNDED',
    funding_eligible boolean NOT NULL DEFAULT false,
    financial_source_hash char(64),
    pending_transfer_intent_id bigint,
    slot_state text NOT NULL DEFAULT 'PROVISIONING',
    desired_schedulable boolean NOT NULL DEFAULT false,
    observed_schedulable boolean NOT NULL DEFAULT false,
    confirmed_pin_binding_id bigint,
    pin_confirmed boolean NOT NULL DEFAULT false,
    probe_confirmed boolean NOT NULL DEFAULT false,
    state_version bigint NOT NULL DEFAULT 1,
    last_success_at timestamptz,
    latency_ewma_ms numeric(30,6),
    valid_from timestamptz NOT NULL DEFAULT now(),
    valid_to timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT provider_pool_slot_identity UNIQUE (model, routing_generation, slot_no),
    CONSTRAINT provider_pool_slot_positive CHECK (
        slot_no > 0 AND routing_generation > 0 AND cost_generation > 0
    ),
    CONSTRAINT provider_pool_slot_state_known CHECK (
        slot_state IN ('PROVISIONING', 'DRAINING', 'REPINNING', 'PROBING',
                       'READY', 'REPAIRING', 'QUARANTINED', 'VACANT')
    ),
    CONSTRAINT provider_pool_slot_liquidity_known CHECK (
        liquidity_state IN ('FUNDED', 'REFILLING', 'UNFUNDED', 'TRANSFER_UNKNOWN', 'UNKNOWN')
    ),
    CONSTRAINT provider_pool_slot_ready_contract CHECK (
        slot_state <> 'READY' OR (
            enodot_account_id IS NOT NULL AND a6_token_id IS NOT NULL
            AND desired_channel_id IS NOT NULL AND desired_priority IS NOT NULL
            AND desired_slot_cost_multiplier IS NOT NULL
            AND observed_rate_multiplier = desired_slot_cost_multiplier
            AND funding_eligible AND desired_schedulable AND observed_schedulable
            AND pin_confirmed AND probe_confirmed
        )
    ),
    CONSTRAINT provider_pool_slot_vacant_contract CHECK (
        slot_state <> 'VACANT' OR (
            enodot_account_id IS NULL AND a6_token_id IS NULL
            AND desired_channel_id IS NULL AND NOT desired_schedulable
            AND NOT observed_schedulable AND NOT funding_eligible
        )
    ),
    CONSTRAINT provider_pool_slot_cost_nonnegative CHECK (
        (desired_slot_cost_multiplier IS NULL OR desired_slot_cost_multiplier >= 0)
        AND (observed_rate_multiplier IS NULL OR observed_rate_multiplier >= 0)
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS provider_pool_current_slot_idx
    ON a6_trade.provider_pool_slots (model, slot_no)
    WHERE valid_to IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS provider_pool_ready_channel_idx
    ON a6_trade.provider_pool_slots (model, desired_channel_id)
    WHERE valid_to IS NULL AND slot_state = 'READY' AND desired_channel_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS provider_pool_dispatch_idx
    ON a6_trade.provider_pool_slots (model, desired_priority, slot_no)
    WHERE valid_to IS NULL AND slot_state = 'READY' AND observed_schedulable;

CREATE TABLE IF NOT EXISTS a6_trade.provider_inventory (
    id bigserial PRIMARY KEY,
    model text NOT NULL,
    merchant_id bigint NOT NULL REFERENCES a6_trade.a6_merchants(id),
    enodot_binding_id bigint,
    enodot_account_id bigint,
    a6_token_id bigint,
    inventory_state text NOT NULL DEFAULT 'WARM_STANDBY',
    liquidity_state text NOT NULL DEFAULT 'UNKNOWN',
    funding_eligible boolean NOT NULL DEFAULT false,
    schedulable boolean NOT NULL DEFAULT false,
    balance_units bigint,
    financial_source_hash char(64),
    assigned_slot_no integer,
    state_version bigint NOT NULL DEFAULT 1,
    last_financial_observed_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (model, merchant_id),
    CONSTRAINT provider_inventory_state_known CHECK (
        inventory_state IN ('READY', 'WARM_STANDBY', 'VACANT', 'REPAIRING', 'RETIRED')
    ),
    CONSTRAINT provider_inventory_liquidity_known CHECK (
        liquidity_state IN ('FUNDED', 'REFILLING', 'UNFUNDED', 'TRANSFER_UNKNOWN', 'UNKNOWN')
    ),
    CONSTRAINT provider_inventory_balance_nonnegative CHECK (balance_units IS NULL OR balance_units >= 0)
);

CREATE INDEX IF NOT EXISTS provider_inventory_assignment_idx
    ON a6_trade.provider_inventory (model, inventory_state, funding_eligible, updated_at DESC);

CREATE TABLE IF NOT EXISTS a6_trade.provider_quarantines (
    id bigserial PRIMARY KEY,
    model text NOT NULL,
    scope_type text NOT NULL,
    scope_id text NOT NULL,
    region text,
    reason_code text NOT NULL,
    failure_count integer NOT NULL DEFAULT 1,
    starts_at timestamptz NOT NULL DEFAULT now(),
    next_probe_at timestamptz,
    ends_at timestamptz,
    state text NOT NULL DEFAULT 'active',
    evidence_hash char(64),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT provider_quarantine_scope_known CHECK (
        scope_type IN ('channel', 'token', 'endpoint', 'egress', 'account', 'region')
    ),
    CONSTRAINT provider_quarantine_state_known CHECK (state IN ('active', 'closed')),
    CONSTRAINT provider_quarantine_failure_positive CHECK (failure_count > 0)
);

CREATE UNIQUE INDEX IF NOT EXISTS provider_quarantine_active_idx
    ON a6_trade.provider_quarantines (model, scope_type, scope_id, COALESCE(region, ''))
    WHERE state = 'active';

CREATE TABLE IF NOT EXISTS a6_trade.provider_request_envelopes (
    id bigserial PRIMARY KEY,
    request_id text NOT NULL UNIQUE,
    trace_id text,
    model text NOT NULL,
    protocol text NOT NULL,
    method text NOT NULL,
    path text NOT NULL,
    headers jsonb NOT NULL DEFAULT '{}'::jsonb,
    request_body bytea NOT NULL,
    body_sha256 char(64) NOT NULL,
    routing_generation bigint NOT NULL,
    cost_generation bigint NOT NULL,
    max_attempts integer NOT NULL DEFAULT 0,
    max_total_seconds numeric(20,6) NOT NULL DEFAULT 600,
    max_retry_cost_micros bigint,
    response_committed boolean NOT NULL DEFAULT false,
    winning_attempt_id text,
    logical_outcome text,
    created_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz,
    UNIQUE (id, request_id),
    CONSTRAINT provider_envelope_body_hash_valid CHECK (body_sha256 ~ '^[0-9a-fA-F]{64}$'),
    CONSTRAINT provider_envelope_attempts_valid CHECK (
        max_attempts >= 0 AND max_total_seconds > 0
        AND (max_retry_cost_micros IS NULL OR max_retry_cost_micros >= 0)
    ),
    CONSTRAINT provider_envelope_headers_object CHECK (jsonb_typeof(headers) = 'object')
);

CREATE TABLE IF NOT EXISTS a6_trade.provider_attempts (
    id bigserial PRIMARY KEY,
    attempt_id text NOT NULL UNIQUE,
    request_envelope_id bigint NOT NULL,
    request_id text NOT NULL,
    attempt_no integer NOT NULL,
    model text NOT NULL,
    routing_generation bigint NOT NULL,
    cost_generation bigint NOT NULL,
    slot_no integer NOT NULL,
    enodot_account_id bigint,
    a6_token_id bigint,
    channel_id bigint,
    endpoint_host text NOT NULL DEFAULT 'a6.a6api.com',
    egress_id text NOT NULL DEFAULT 'direct',
    state text NOT NULL DEFAULT 'created',
    http_status integer,
    failure_owner text,
    failure_code text,
    terminal_event_seen boolean NOT NULL DEFAULT false,
    response_committed boolean NOT NULL DEFAULT false,
    response_body_sha256 char(64),
    actual_cost_micros bigint NOT NULL DEFAULT 0,
    transport_status integer,
    delivery_state text NOT NULL DEFAULT 'not_started',
    started_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    UNIQUE (request_id, attempt_no),
    UNIQUE (request_id, slot_no),
    UNIQUE (attempt_id, request_id),
    CONSTRAINT provider_attempt_envelope_fk
        FOREIGN KEY (request_envelope_id, request_id)
        REFERENCES a6_trade.provider_request_envelopes(id, request_id)
        ON DELETE CASCADE,
    CONSTRAINT provider_attempt_state_known CHECK (
        state IN ('created', 'dispatched', 'buffered', 'failed', 'won', 'lost', 'cancelled')
    ),
    CONSTRAINT provider_attempt_cost_nonnegative CHECK (actual_cost_micros >= 0),
    CONSTRAINT provider_attempt_identity_positive CHECK (
        attempt_no > 0 AND routing_generation > 0 AND cost_generation > 0 AND slot_no > 0
    ),
    CONSTRAINT provider_attempt_status_valid CHECK (
        http_status IS NULL OR http_status BETWEEN 100 AND 599
    ),
    CONSTRAINT provider_attempt_transport_status_valid CHECK (
        transport_status IS NULL OR transport_status BETWEEN 100 AND 599
    ),
    CONSTRAINT provider_attempt_failure_owner_known CHECK (
        failure_owner IS NULL
        OR failure_owner IN (
            'user', 'client_transport', 'provider',
            'platform_path', 'internal', 'unknown'
        )
    ),
    CONSTRAINT provider_attempt_delivery_state_known CHECK (
        delivery_state IN (
            'not_started', 'stream_open', 'delivered',
            'delivery_aborted', 'client_gone'
        )
    ),
    CONSTRAINT provider_attempt_commit_state_valid CHECK (
        NOT response_committed OR state = 'won'
    ),
    CONSTRAINT provider_attempt_response_hash_valid CHECK (
        response_body_sha256 IS NULL
        OR response_body_sha256 ~ '^[0-9a-fA-F]{64}$'
    )
);

ALTER TABLE a6_trade.provider_request_envelopes
    ADD CONSTRAINT provider_envelope_winning_attempt_fk
    FOREIGN KEY (winning_attempt_id, request_id)
    REFERENCES a6_trade.provider_attempts(attempt_id, request_id)
    DEFERRABLE INITIALLY DEFERRED;

CREATE UNIQUE INDEX IF NOT EXISTS provider_attempt_one_winner_idx
    ON a6_trade.provider_attempts (request_id)
    WHERE state = 'won' AND response_committed;
CREATE INDEX IF NOT EXISTS provider_attempt_request_idx
    ON a6_trade.provider_attempts (request_id, attempt_no);

CREATE TABLE IF NOT EXISTS a6_trade.model_publications (
    model text PRIMARY KEY,
    desired_state text NOT NULL DEFAULT 'unpublished',
    observed_state text NOT NULL DEFAULT 'unpublished',
    healthy_slots integer NOT NULL DEFAULT 0,
    min_healthy_slots integer NOT NULL DEFAULT 1,
    reason_code text,
    desired_generation bigint,
    observed_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT provider_publication_state_known CHECK (
        desired_state IN ('published', 'degraded', 'unpublished', 'repairing')
        AND observed_state IN ('published', 'degraded', 'unpublished', 'repairing')
    ),
    CONSTRAINT provider_publication_slots_valid CHECK (
        healthy_slots >= 0 AND min_healthy_slots >= 1
    )
);

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'a6trader') THEN
        GRANT SELECT, INSERT, UPDATE, DELETE
            ON a6_trade.provider_cost_generations,
               a6_trade.provider_pool_generations,
               a6_trade.provider_pool_slots,
               a6_trade.provider_inventory,
               a6_trade.provider_quarantines,
               a6_trade.provider_request_envelopes,
               a6_trade.provider_attempts,
               a6_trade.model_publications TO a6trader;
        GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA a6_trade TO a6trader;
    END IF;
END
$$;
