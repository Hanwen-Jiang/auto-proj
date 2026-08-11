CREATE SCHEMA IF NOT EXISTS a6_trade;

CREATE TABLE IF NOT EXISTS a6_trade.a6_merchants (
    id bigserial PRIMARY KEY,
    a6_user_id bigint,
    supplier_id bigint,
    session_ref text NOT NULL UNIQUE,
    session_fingerprint char(64) NOT NULL,
    roles text[] NOT NULL DEFAULT ARRAY['unassigned']::text[],
    status text NOT NULL DEFAULT 'unknown',
    proxy_lease_id text,
    proxy_fingerprint varchar(64),
    proxy_metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    last_auth_ok_at timestamptz,
    last_poll_at timestamptz,
    poll_error_count integer NOT NULL DEFAULT 0,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT a6_merchants_roles_known CHECK (
        roles <@ ARRAY['unassigned', 'seller', 'provider', 'observer', 'probe']::text[]
    ),
    CONSTRAINT a6_merchants_poll_errors_nonnegative CHECK (poll_error_count >= 0),
    CONSTRAINT a6_merchants_proxy_metadata_object CHECK (
        jsonb_typeof(proxy_metadata) = 'object'
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS a6_merchants_a6_user_id_uidx
    ON a6_trade.a6_merchants (a6_user_id)
    WHERE a6_user_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS a6_merchants_supplier_id_uidx
    ON a6_trade.a6_merchants (supplier_id)
    WHERE supplier_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS a6_merchants_status_idx
    ON a6_trade.a6_merchants (status);
CREATE INDEX IF NOT EXISTS a6_merchants_roles_gin_idx
    ON a6_trade.a6_merchants USING gin (roles);
CREATE INDEX IF NOT EXISTS a6_merchants_proxy_fingerprint_idx
    ON a6_trade.a6_merchants (proxy_fingerprint)
    WHERE proxy_fingerprint IS NOT NULL;

CREATE TABLE IF NOT EXISTS a6_trade.a6_tokens (
    id bigserial PRIMARY KEY,
    merchant_id bigint NOT NULL REFERENCES a6_trade.a6_merchants(id),
    a6_token_id bigint NOT NULL,
    token_name text NOT NULL,
    key_fingerprint varchar(64),
    key_secret_ref text,
    status text NOT NULL DEFAULT 'unknown',
    created_by_controller boolean NOT NULL DEFAULT false,
    last_reconciled_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (merchant_id, a6_token_id),
    UNIQUE (merchant_id, token_name)
);

CREATE INDEX IF NOT EXISTS a6_tokens_status_idx
    ON a6_trade.a6_tokens (status);

CREATE TABLE IF NOT EXISTS a6_trade.enodot_account_bindings (
    id bigserial PRIMARY KEY,
    a6_token_id bigint NOT NULL REFERENCES a6_trade.a6_tokens(id),
    enodot_account_id bigint,
    binding_uid text NOT NULL UNIQUE,
    model_scope text[] NOT NULL DEFAULT '{}'::text[],
    desired_priority smallint NOT NULL DEFAULT 1,
    desired_concurrency integer NOT NULL DEFAULT 1,
    state text NOT NULL DEFAULT 'planned',
    cooldown_until timestamptz,
    last_error_class text,
    last_error_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT enodot_binding_priority_range CHECK (desired_priority BETWEEN 0 AND 2),
    CONSTRAINT enodot_binding_concurrency_positive CHECK (desired_concurrency >= 0),
    UNIQUE (a6_token_id, enodot_account_id)
);

CREATE INDEX IF NOT EXISTS enodot_account_bindings_state_idx
    ON a6_trade.enodot_account_bindings (state, cooldown_until);
CREATE UNIQUE INDEX IF NOT EXISTS enodot_account_bindings_account_idx
    ON a6_trade.enodot_account_bindings (enodot_account_id)
    WHERE enodot_account_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS a6_trade.a6_financial_current (
    merchant_id bigint PRIMARY KEY REFERENCES a6_trade.a6_merchants(id),
    user_balance_quota bigint,
    used_quota bigint,
    request_count bigint,
    withdrawable_micros bigint,
    withdrawing_micros bigint,
    lifetime_earned_micros bigint,
    paid_micros bigint,
    today_earned_micros bigint,
    month_earned_micros bigint,
    today_calls bigint,
    month_calls bigint,
    settlement_pending_micros bigint,
    settlement_settled_micros bigint,
    settlement_total_income_micros bigint,
    settlement_week_new_micros bigint,
    settlement_abnormal_count integer,
    markup_percent integer,
    observed_at timestamptz NOT NULL,
    source_hash char(64) NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS a6_financial_current_observed_at_idx
    ON a6_trade.a6_financial_current (observed_at);

CREATE TABLE IF NOT EXISTS a6_trade.a6_financial_snapshots (
    id bigserial PRIMARY KEY,
    merchant_id bigint NOT NULL REFERENCES a6_trade.a6_merchants(id),
    observed_bucket_5m timestamptz NOT NULL,
    observed_at timestamptz NOT NULL,
    user_balance_quota bigint,
    used_quota bigint,
    request_count bigint,
    withdrawable_micros bigint,
    withdrawing_micros bigint,
    lifetime_earned_micros bigint,
    paid_micros bigint,
    today_earned_micros bigint,
    month_earned_micros bigint,
    today_calls bigint,
    month_calls bigint,
    settlement_pending_micros bigint,
    settlement_settled_micros bigint,
    settlement_total_income_micros bigint,
    settlement_week_new_micros bigint,
    settlement_abnormal_count integer,
    markup_percent integer,
    snapshot_reason text NOT NULL DEFAULT 'changed',
    source_status text NOT NULL DEFAULT 'ok',
    source_hash char(64) NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (merchant_id, observed_bucket_5m)
);

CREATE INDEX IF NOT EXISTS a6_financial_snapshots_observed_at_idx
    ON a6_trade.a6_financial_snapshots (observed_at DESC);

CREATE TABLE IF NOT EXISTS a6_trade.a6_earning_events (
    id bigserial PRIMARY KEY,
    merchant_id bigint NOT NULL REFERENCES a6_trade.a6_merchants(id),
    a6_earning_id text NOT NULL,
    log_id text,
    request_id text,
    channel_id bigint,
    listing_id bigint,
    model text NOT NULL,
    own_model text,
    prompt_tokens bigint,
    completion_tokens bigint,
    cache_read_tokens bigint,
    cache_write_tokens bigint,
    quota_micros bigint,
    earned_micros bigint NOT NULL,
    market_quota_micros bigint,
    supplier_earn_micros bigint,
    markup_percent numeric(12, 6),
    channel_multiplier numeric(24, 12),
    final_multiplier numeric(24, 12),
    result_class text,
    elapsed_ms integer,
    occurred_at timestamptz NOT NULL,
    source_hash char(64),
    ingested_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (merchant_id, a6_earning_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS a6_earning_events_request_uidx
    ON a6_trade.a6_earning_events (merchant_id, request_id)
    WHERE request_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS a6_earning_events_time_idx
    ON a6_trade.a6_earning_events (occurred_at DESC);
CREATE INDEX IF NOT EXISTS a6_earning_events_model_time_idx
    ON a6_trade.a6_earning_events (model, occurred_at DESC);

CREATE TABLE IF NOT EXISTS a6_trade.a6_provider_usage_events (
    id bigserial PRIMARY KEY,
    merchant_id bigint NOT NULL REFERENCES a6_trade.a6_merchants(id),
    a6_log_id text NOT NULL,
    request_id text,
    a6_token_ref_id bigint REFERENCES a6_trade.a6_tokens(id),
    external_a6_token_id bigint,
    model text NOT NULL,
    channel_id bigint,
    quota_micros bigint NOT NULL,
    prompt_tokens bigint,
    completion_tokens bigint,
    cache_read_tokens bigint,
    cache_write_tokens bigint,
    route_type text,
    result_class text,
    http_status integer,
    elapsed_ms integer,
    occurred_at timestamptz NOT NULL,
    probe_run_id bigint,
    source_hash char(64),
    ingested_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (merchant_id, a6_log_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS a6_provider_usage_events_request_uidx
    ON a6_trade.a6_provider_usage_events (merchant_id, request_id)
    WHERE request_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS a6_provider_usage_events_time_idx
    ON a6_trade.a6_provider_usage_events (occurred_at DESC);
CREATE INDEX IF NOT EXISTS a6_provider_usage_events_channel_time_idx
    ON a6_trade.a6_provider_usage_events (channel_id, occurred_at DESC);
CREATE INDEX IF NOT EXISTS a6_provider_usage_events_token_ref_time_idx
    ON a6_trade.a6_provider_usage_events (a6_token_ref_id, occurred_at DESC)
    WHERE a6_token_ref_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS a6_provider_usage_events_external_token_time_idx
    ON a6_trade.a6_provider_usage_events (external_a6_token_id, occurred_at DESC)
    WHERE external_a6_token_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS a6_trade.a6_settlement_events (
    id bigserial PRIMARY KEY,
    merchant_id bigint NOT NULL REFERENCES a6_trade.a6_merchants(id),
    a6_settlement_id text NOT NULL,
    amount_micros bigint NOT NULL,
    status text NOT NULL,
    occurred_at timestamptz NOT NULL,
    source_hash char(64),
    ingested_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (merchant_id, a6_settlement_id)
);

CREATE INDEX IF NOT EXISTS a6_settlement_events_time_idx
    ON a6_trade.a6_settlement_events (occurred_at DESC);

CREATE TABLE IF NOT EXISTS a6_trade.a6_withdrawal_events (
    id bigserial PRIMARY KEY,
    merchant_id bigint NOT NULL REFERENCES a6_trade.a6_merchants(id),
    a6_withdrawal_id text NOT NULL,
    amount_micros bigint NOT NULL,
    fee_micros bigint,
    status text NOT NULL,
    requested_at timestamptz NOT NULL,
    completed_at timestamptz,
    source_hash char(64),
    ingested_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (merchant_id, a6_withdrawal_id)
);

CREATE INDEX IF NOT EXISTS a6_withdrawal_events_requested_at_idx
    ON a6_trade.a6_withdrawal_events (requested_at DESC);

CREATE TABLE IF NOT EXISTS a6_trade.a6_balance_transfer_intents (
    id bigserial PRIMARY KEY,
    merchant_id bigint NOT NULL REFERENCES a6_trade.a6_merchants(id),
    idempotency_key text NOT NULL UNIQUE,
    amount_micros bigint NOT NULL,
    preview jsonb NOT NULL DEFAULT '{}'::jsonb,
    state text NOT NULL DEFAULT 'planned',
    requested_by text NOT NULL,
    requested_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT a6_balance_transfer_amount_positive CHECK (amount_micros > 0)
);

CREATE INDEX IF NOT EXISTS a6_balance_transfer_intents_state_idx
    ON a6_trade.a6_balance_transfer_intents (state, requested_at);

CREATE TABLE IF NOT EXISTS a6_trade.a6_balance_transfer_confirmations (
    id bigserial PRIMARY KEY,
    intent_id bigint NOT NULL REFERENCES a6_trade.a6_balance_transfer_intents(id),
    status text NOT NULL,
    balance_before_micros bigint,
    balance_after_micros bigint,
    merchant_available_before_micros bigint,
    merchant_available_after_micros bigint,
    observed_at timestamptz NOT NULL,
    evidence_hash char(64),
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS a6_balance_transfer_confirmations_intent_idx
    ON a6_trade.a6_balance_transfer_confirmations (intent_id, observed_at DESC);

CREATE TABLE IF NOT EXISTS a6_trade.a6_market_scan_runs (
    id bigserial PRIMARY KEY,
    observer_merchant_id bigint NOT NULL REFERENCES a6_trade.a6_merchants(id),
    proxy_lease_id text,
    model text NOT NULL,
    started_at timestamptz NOT NULL,
    completed_at timestamptz,
    page_count integer NOT NULL DEFAULT 0,
    candidate_count integer NOT NULL DEFAULT 0,
    http_status integer,
    elapsed_ms integer,
    result_hash char(64),
    status text NOT NULL DEFAULT 'running',
    error_class text,
    raw_artifact_ref text,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT a6_market_scan_counts_nonnegative CHECK (
        page_count >= 0 AND candidate_count >= 0
    )
);

CREATE INDEX IF NOT EXISTS a6_market_scan_runs_model_time_idx
    ON a6_trade.a6_market_scan_runs (model, started_at DESC);
CREATE INDEX IF NOT EXISTS a6_market_scan_runs_observer_time_idx
    ON a6_trade.a6_market_scan_runs (observer_merchant_id, started_at DESC);

CREATE TABLE IF NOT EXISTS a6_trade.a6_market_quotes (
    id bigserial PRIMARY KEY,
    scan_run_id bigint NOT NULL REFERENCES a6_trade.a6_market_scan_runs(id) ON DELETE CASCADE,
    supplier_id bigint,
    channel_id bigint NOT NULL,
    listing_id bigint,
    model text NOT NULL,
    input_price numeric(30, 12),
    output_price numeric(30, 12),
    cache_read_price numeric(30, 12),
    cache_write_price numeric(30, 12),
    final_multiplier numeric(24, 12),
    route_status text,
    last_success_at timestamptz,
    circuit_state text,
    market_rank integer,
    is_owned boolean NOT NULL DEFAULT false,
    observed_at timestamptz NOT NULL,
    source_hash char(64),
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT a6_market_quote_rank_positive CHECK (market_rank IS NULL OR market_rank > 0)
);

CREATE UNIQUE INDEX IF NOT EXISTS a6_market_quotes_identity_uidx
    ON a6_trade.a6_market_quotes (
        scan_run_id,
        channel_id,
        COALESCE(listing_id, -1),
        model
    );
CREATE INDEX IF NOT EXISTS a6_market_quotes_model_rank_idx
    ON a6_trade.a6_market_quotes (model, market_rank, observed_at DESC);
CREATE INDEX IF NOT EXISTS a6_market_quotes_owned_idx
    ON a6_trade.a6_market_quotes (is_owned, observed_at DESC)
    WHERE is_owned;
CREATE INDEX IF NOT EXISTS a6_market_quotes_owned_supplier_idx
    ON a6_trade.a6_market_quotes (supplier_id, observed_at DESC)
    WHERE is_owned;

CREATE TABLE IF NOT EXISTS a6_trade.a6_pin_decisions (
    id bigserial PRIMARY KEY,
    a6_token_id bigint NOT NULL REFERENCES a6_trade.a6_tokens(id),
    model text NOT NULL,
    current_channel_id bigint,
    selected_channel_id bigint,
    candidate_set jsonb NOT NULL DEFAULT '[]'::jsonb,
    exclusion_reasons jsonb NOT NULL DEFAULT '{}'::jsonb,
    expected_cost_multiplier numeric(24, 12),
    expected_switch_gain numeric(24, 12),
    hysteresis_threshold numeric(24, 12),
    config_version text NOT NULL,
    decision_status text NOT NULL DEFAULT 'suggested',
    decided_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT a6_pin_decision_candidates_array CHECK (
        jsonb_typeof(candidate_set) = 'array'
    ),
    CONSTRAINT a6_pin_decision_exclusions_object CHECK (
        jsonb_typeof(exclusion_reasons) = 'object'
    )
);

CREATE INDEX IF NOT EXISTS a6_pin_decisions_token_model_time_idx
    ON a6_trade.a6_pin_decisions (a6_token_id, model, decided_at DESC);

CREATE TABLE IF NOT EXISTS a6_trade.a6_pin_bindings (
    id bigserial PRIMARY KEY,
    a6_token_id bigint NOT NULL REFERENCES a6_trade.a6_tokens(id),
    model text NOT NULL,
    channel_id bigint NOT NULL,
    fallback_to_smart_routing boolean NOT NULL DEFAULT false,
    valid_from timestamptz NOT NULL,
    valid_to timestamptz,
    decision_id bigint REFERENCES a6_trade.a6_pin_decisions(id),
    confirmed_at timestamptz,
    change_reason text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT a6_pin_binding_valid_window CHECK (
        valid_to IS NULL OR valid_to > valid_from
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS a6_pin_bindings_one_current_uidx
    ON a6_trade.a6_pin_bindings (a6_token_id, model)
    WHERE valid_to IS NULL;
CREATE INDEX IF NOT EXISTS a6_pin_bindings_history_idx
    ON a6_trade.a6_pin_bindings (a6_token_id, model, valid_from DESC);
CREATE INDEX IF NOT EXISTS a6_pin_bindings_channel_idx
    ON a6_trade.a6_pin_bindings (channel_id, valid_from DESC);

CREATE TABLE IF NOT EXISTS a6_trade.trade_control_actions (
    id bigserial PRIMARY KEY,
    idempotency_key text NOT NULL UNIQUE,
    action_type text NOT NULL,
    entity_type text NOT NULL,
    entity_id text NOT NULL,
    desired_state jsonb NOT NULL,
    observed_before jsonb,
    observed_after jsonb,
    status text NOT NULL DEFAULT 'suggested',
    error_class text,
    config_version text NOT NULL,
    requested_by text NOT NULL,
    requested_at timestamptz NOT NULL DEFAULT now(),
    started_at timestamptz,
    completed_at timestamptz,
    elapsed_ms integer,
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT trade_control_action_desired_object CHECK (
        jsonb_typeof(desired_state) = 'object'
    )
);

CREATE INDEX IF NOT EXISTS trade_control_actions_status_idx
    ON a6_trade.trade_control_actions (status, requested_at);
CREATE INDEX IF NOT EXISTS trade_control_actions_entity_idx
    ON a6_trade.trade_control_actions (entity_type, entity_id, requested_at DESC);

CREATE TABLE IF NOT EXISTS a6_trade.trade_control_action_attempts (
    id bigserial PRIMARY KEY,
    action_id bigint NOT NULL REFERENCES a6_trade.trade_control_actions(id),
    attempt_no integer NOT NULL,
    status text NOT NULL,
    observed_before jsonb,
    observed_after jsonb,
    error_class text,
    error_fingerprint char(64),
    started_at timestamptz NOT NULL,
    completed_at timestamptz,
    elapsed_ms integer,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT trade_control_attempt_number_positive CHECK (attempt_no > 0),
    UNIQUE (action_id, attempt_no)
);

CREATE INDEX IF NOT EXISTS trade_control_action_attempts_status_idx
    ON a6_trade.trade_control_action_attempts (status, started_at DESC);

CREATE TABLE IF NOT EXISTS a6_trade.trade_incidents (
    id bigserial PRIMARY KEY,
    incident_key text NOT NULL UNIQUE,
    incident_class text NOT NULL,
    entity_type text NOT NULL,
    entity_id text NOT NULL,
    severity text NOT NULL,
    status text NOT NULL DEFAULT 'open',
    details jsonb NOT NULL DEFAULT '{}'::jsonb,
    first_seen_at timestamptz NOT NULL,
    last_seen_at timestamptz NOT NULL,
    occurrence_count integer NOT NULL DEFAULT 1,
    resolved_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT trade_incident_occurrence_positive CHECK (occurrence_count > 0)
);

CREATE INDEX IF NOT EXISTS trade_incidents_status_idx
    ON a6_trade.trade_incidents (status, severity, last_seen_at DESC);
CREATE INDEX IF NOT EXISTS trade_incidents_entity_time_idx
    ON a6_trade.trade_incidents (entity_type, entity_id, last_seen_at DESC);

CREATE TABLE IF NOT EXISTS a6_trade.trade_probe_runs (
    id bigserial PRIMARY KEY,
    merchant_id bigint NOT NULL REFERENCES a6_trade.a6_merchants(id),
    a6_token_id bigint REFERENCES a6_trade.a6_tokens(id),
    channel_id bigint,
    model text NOT NULL,
    concurrency integer NOT NULL,
    request_target integer NOT NULL,
    request_success integer NOT NULL DEFAULT 0,
    request_failed integer NOT NULL DEFAULT 0,
    cost_micros bigint NOT NULL DEFAULT 0,
    status text NOT NULL DEFAULT 'planned',
    started_at timestamptz,
    completed_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT trade_probe_limits_positive CHECK (
        concurrency > 0 AND request_target > 0
    ),
    CONSTRAINT trade_probe_counts_nonnegative CHECK (
        request_success >= 0 AND request_failed >= 0 AND cost_micros >= 0
    )
);

CREATE INDEX IF NOT EXISTS trade_probe_runs_channel_time_idx
    ON a6_trade.trade_probe_runs (channel_id, created_at DESC);

CREATE TABLE IF NOT EXISTS a6_trade.trade_job_heartbeats (
    job_name text PRIMARY KEY,
    worker_id text NOT NULL,
    status text NOT NULL,
    watermark jsonb NOT NULL DEFAULT '{}'::jsonb,
    last_success_at timestamptz,
    heartbeat_at timestamptz NOT NULL,
    error_class text,
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS trade_job_heartbeats_freshness_idx
    ON a6_trade.trade_job_heartbeats (heartbeat_at);
