ALTER TABLE a6_trade.a6_market_channel_signals_current
    ADD COLUMN IF NOT EXISTS route_status text NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS circuit_state text NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS circuit_probe_status text NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS circuit_last_error_kind text NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS circuit_probe_success_required integer,
    ADD COLUMN IF NOT EXISTS cooldown_state text NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS cooldown_label text NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS cooldown_remaining_seconds integer,
    ADD COLUMN IF NOT EXISTS capability_cooldowns jsonb NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS smart_routing_status text NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS smart_routing_labels text[] NOT NULL DEFAULT '{}'::text[],
    ADD COLUMN IF NOT EXISTS listing_availability jsonb,
    ADD COLUMN IF NOT EXISTS last_failure_at timestamptz,
    ADD COLUMN IF NOT EXISTS last_failure_code text NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS last_sample_at timestamptz,
    ADD COLUMN IF NOT EXISTS sample_count integer,
    ADD COLUMN IF NOT EXISTS recent_success_rate integer,
    ADD COLUMN IF NOT EXISTS success_buckets jsonb NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS recent_p50_ms numeric(16, 3),
    ADD COLUMN IF NOT EXISTS p50_ttft_ms numeric(16, 3),
    ADD COLUMN IF NOT EXISTS health_state text NOT NULL DEFAULT 'unknown';

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'a6_market_signal_health_values_valid'
          AND conrelid = 'a6_trade.a6_market_channel_signals_current'::regclass
    ) THEN
        ALTER TABLE a6_trade.a6_market_channel_signals_current
            ADD CONSTRAINT a6_market_signal_health_values_valid CHECK (
                (circuit_probe_success_required IS NULL OR circuit_probe_success_required >= 0)
                AND (cooldown_remaining_seconds IS NULL OR cooldown_remaining_seconds >= 0)
                AND (sample_count IS NULL OR sample_count >= 0)
                AND (recent_success_rate IS NULL OR recent_success_rate BETWEEN 0 AND 10000)
                AND (recent_p50_ms IS NULL OR recent_p50_ms >= 0)
                AND (p50_ttft_ms IS NULL OR p50_ttft_ms >= 0)
                AND health_state IN (
                    'cooling', 'unavailable', 'degraded', 'unlabeled',
                    'idle', 'healthy', 'unknown'
                )
                AND jsonb_typeof(capability_cooldowns) IN ('object', 'array')
                AND jsonb_typeof(success_buckets) IN ('object', 'array')
            );
    END IF;
END
$$;

CREATE INDEX IF NOT EXISTS a6_market_signals_owned_health_idx
    ON a6_trade.a6_market_channel_signals_current (
        health_state, last_observed_at DESC, channel_id
    )
    WHERE is_owned;

CREATE TABLE IF NOT EXISTS a6_trade.a6_channel_health_events (
    id bigserial PRIMARY KEY,
    model text NOT NULL,
    channel_id bigint NOT NULL,
    listing_id bigint,
    observer_merchant_id bigint NOT NULL
        REFERENCES a6_trade.a6_merchants(id),
    previous_state text NOT NULL,
    current_state text NOT NULL,
    evidence jsonb NOT NULL DEFAULT '{}'::jsonb,
    observed_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT a6_channel_health_event_ids_positive CHECK (
        channel_id > 0 AND (listing_id IS NULL OR listing_id > 0)
    ),
    CONSTRAINT a6_channel_health_event_states_known CHECK (
        previous_state IN (
            'cooling', 'unavailable', 'degraded', 'unlabeled',
            'idle', 'healthy', 'unknown'
        )
        AND current_state IN (
            'cooling', 'unavailable', 'degraded', 'unlabeled',
            'idle', 'healthy', 'unknown'
        )
    ),
    CONSTRAINT a6_channel_health_event_evidence_object CHECK (
        jsonb_typeof(evidence) = 'object'
    )
);

CREATE INDEX IF NOT EXISTS a6_channel_health_events_channel_time_idx
    ON a6_trade.a6_channel_health_events (model, channel_id, observed_at DESC);
CREATE INDEX IF NOT EXISTS a6_channel_health_events_state_time_idx
    ON a6_trade.a6_channel_health_events (current_state, observed_at DESC);

CREATE TABLE IF NOT EXISTS a6_trade.a6_channel_growth_policies (
    listing_binding_id bigint PRIMARY KEY
        REFERENCES a6_trade.a6_managed_listing_bindings(id) ON DELETE CASCADE,
    enabled boolean NOT NULL DEFAULT false,
    recycle_enabled boolean NOT NULL DEFAULT true,
    callback_enabled boolean NOT NULL DEFAULT true,
    action_cooldown_seconds integer NOT NULL DEFAULT 1800,
    callback_cooldown_seconds integer NOT NULL DEFAULT 600,
    callback_requests integer NOT NULL DEFAULT 15,
    callback_concurrency integer NOT NULL DEFAULT 5,
    daily_budget_micros bigint NOT NULL DEFAULT 250000,
    working_balance_low_usd numeric(20, 8) NOT NULL DEFAULT 0.25000000,
    working_balance_target_usd numeric(20, 8) NOT NULL DEFAULT 1.00000000,
    degraded_success_rate integer NOT NULL DEFAULT 8000,
    idle_seconds integer NOT NULL DEFAULT 1800,
    maximum_consecutive_failures integer NOT NULL DEFAULT 2,
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT a6_channel_growth_policy_values_valid CHECK (
        action_cooldown_seconds >= 60
        AND callback_cooldown_seconds >= 60
        AND callback_requests BETWEEN 1 AND 100
        AND callback_concurrency BETWEEN 1 AND callback_requests
        AND daily_budget_micros >= 0
        AND working_balance_low_usd >= 0
        AND working_balance_target_usd >= working_balance_low_usd
        AND degraded_success_rate BETWEEN 0 AND 10000
        AND idle_seconds >= 60
        AND maximum_consecutive_failures BETWEEN 1 AND 20
    )
);

INSERT INTO a6_trade.a6_channel_growth_policies (listing_binding_id)
SELECT id
FROM a6_trade.a6_managed_listing_bindings
WHERE control_state = 'verified' AND a6_listing_id IS NOT NULL
ON CONFLICT (listing_binding_id) DO NOTHING;

CREATE TABLE IF NOT EXISTS a6_trade.a6_channel_booster_tokens (
    a6_token_ref_id bigint PRIMARY KEY
        REFERENCES a6_trade.a6_tokens(id) ON DELETE CASCADE,
    enabled boolean NOT NULL DEFAULT true,
    daily_budget_micros bigint NOT NULL DEFAULT 250000,
    maximum_burst_requests integer NOT NULL DEFAULT 15,
    last_used_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT a6_channel_booster_values_valid CHECK (
        daily_budget_micros >= 0
        AND maximum_burst_requests BETWEEN 1 AND 100
    )
);

INSERT INTO a6_trade.a6_channel_booster_tokens (a6_token_ref_id)
SELECT id
FROM a6_trade.a6_tokens
WHERE lower(token_name) = 'callback'
ON CONFLICT (a6_token_ref_id) DO NOTHING;

CREATE TABLE IF NOT EXISTS a6_trade.a6_channel_growth_actions (
    id bigserial PRIMARY KEY,
    idempotency_key text NOT NULL UNIQUE,
    merchant_id bigint NOT NULL
        REFERENCES a6_trade.a6_merchants(id),
    listing_binding_id bigint NOT NULL
        REFERENCES a6_trade.a6_managed_listing_bindings(id),
    model text NOT NULL,
    channel_id bigint NOT NULL,
    listing_id bigint NOT NULL,
    health_state text NOT NULL,
    action_type text NOT NULL,
    state text NOT NULL DEFAULT 'running',
    booster_token_ref_id bigint
        REFERENCES a6_trade.a6_tokens(id),
    request_count integer NOT NULL DEFAULT 0,
    success_count integer NOT NULL DEFAULT 0,
    failure_count integer NOT NULL DEFAULT 0,
    estimated_cost_micros bigint NOT NULL DEFAULT 0,
    actual_cost_micros bigint NOT NULL DEFAULT 0,
    balance_before_usd numeric(20, 8),
    balance_after_usd numeric(20, 8),
    details jsonb NOT NULL DEFAULT '{}'::jsonb,
    error_class text,
    started_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz,
    next_allowed_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT a6_channel_growth_action_ids_positive CHECK (
        channel_id > 0 AND listing_id > 0
    ),
    CONSTRAINT a6_channel_growth_action_health_known CHECK (
        health_state IN (
            'cooling', 'unavailable', 'degraded', 'unlabeled',
            'idle', 'healthy', 'unknown'
        )
    ),
    CONSTRAINT a6_channel_growth_action_type_known CHECK (
        action_type IN (
            'listing_recycle', 'callback_readiness', 'callback_burst'
        )
    ),
    CONSTRAINT a6_channel_growth_action_state_known CHECK (
        state IN ('running', 'completed', 'failed', 'skipped')
    ),
    CONSTRAINT a6_channel_growth_action_counts_valid CHECK (
        request_count >= 0
        AND success_count >= 0
        AND failure_count >= 0
        AND success_count + failure_count <= request_count
        AND estimated_cost_micros >= 0
        AND actual_cost_micros >= 0
    ),
    CONSTRAINT a6_channel_growth_action_details_object CHECK (
        jsonb_typeof(details) = 'object'
    )
);

CREATE INDEX IF NOT EXISTS a6_channel_growth_actions_listing_time_idx
    ON a6_trade.a6_channel_growth_actions (listing_binding_id, started_at DESC);
CREATE INDEX IF NOT EXISTS a6_channel_growth_actions_state_time_idx
    ON a6_trade.a6_channel_growth_actions (state, started_at DESC);
CREATE INDEX IF NOT EXISTS a6_channel_growth_actions_booster_time_idx
    ON a6_trade.a6_channel_growth_actions (booster_token_ref_id, started_at DESC)
    WHERE booster_token_ref_id IS NOT NULL;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'a6trader') THEN
        GRANT SELECT, INSERT, UPDATE, DELETE
            ON a6_trade.a6_channel_health_events TO a6trader;
        GRANT SELECT, INSERT, UPDATE, DELETE
            ON a6_trade.a6_channel_growth_policies TO a6trader;
        GRANT SELECT, INSERT, UPDATE, DELETE
            ON a6_trade.a6_channel_booster_tokens TO a6trader;
        GRANT SELECT, INSERT, UPDATE, DELETE
            ON a6_trade.a6_channel_growth_actions TO a6trader;
        GRANT USAGE, SELECT, UPDATE
            ON SEQUENCE a6_trade.a6_channel_health_events_id_seq TO a6trader;
        GRANT USAGE, SELECT, UPDATE
            ON SEQUENCE a6_trade.a6_channel_growth_actions_id_seq TO a6trader;
    END IF;
END
$$;
