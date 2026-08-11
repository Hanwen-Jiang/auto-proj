-- Priority 1 economic source routing. PostgreSQL owns contracts, snapshots,
-- eligibility generations and request economics; Redis is a projection only.

CREATE TABLE IF NOT EXISTS a6_trade.sub2api_economic_sources (
    source_id bigserial PRIMARY KEY,
    source_key text NOT NULL UNIQUE,
    enodot_account_id bigint,
    source_kind text NOT NULL,
    origin_class text NOT NULL,
    model text NOT NULL,
    endpoint_ref text NOT NULL,
    endpoint_fingerprint char(64) NOT NULL,
    health_state text NOT NULL DEFAULT 'UNKNOWN',
    health_observed_at timestamptz,
    capacity_state text NOT NULL DEFAULT 'UNKNOWN',
    capacity_observed_at timestamptz,
    capacity_epoch text,
    configured_reserve_a numeric(30,12) NOT NULL DEFAULT 0,
    enabled boolean NOT NULL DEFAULT false,
    config_version text NOT NULL,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT sub2api_economic_source_key_valid CHECK (
        btrim(source_key) <> '' AND length(source_key) <= 256
    ),
    CONSTRAINT sub2api_economic_source_kind_known CHECK (
        source_kind IN ('api', 'account', 'a6_gateway')
    ),
    CONSTRAINT sub2api_economic_source_origin_valid CHECK (
        btrim(origin_class) <> '' AND length(origin_class) <= 128
    ),
    CONSTRAINT sub2api_economic_source_identity_valid CHECK (
        btrim(model) <> ''
        AND btrim(endpoint_ref) <> ''
        AND endpoint_fingerprint ~ '^[0-9a-fA-F]{64}$'
        AND (enodot_account_id IS NULL OR enodot_account_id > 0)
        AND (
            (source_kind IN ('api', 'account') AND enodot_account_id IS NOT NULL)
            OR (source_kind = 'a6_gateway' AND enodot_account_id IS NULL)
        )
    ),
    CONSTRAINT sub2api_economic_source_health_known CHECK (
        health_state IN ('HEALTHY', 'UNHEALTHY', 'UNKNOWN')
    ),
    CONSTRAINT sub2api_economic_source_capacity_known CHECK (
        capacity_state IN ('AVAILABLE', 'THROTTLED', 'EXHAUSTED', 'UNKNOWN')
    ),
    CONSTRAINT sub2api_economic_source_capacity_valid CHECK (
        configured_reserve_a >= 0
        AND (capacity_epoch IS NULL OR length(capacity_epoch) <= 256)
    ),
    CONSTRAINT sub2api_economic_source_metadata_object CHECK (
        jsonb_typeof(metadata) = 'object'
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS sub2api_economic_source_account_model_uidx
    ON a6_trade.sub2api_economic_sources (enodot_account_id, model)
    WHERE enodot_account_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS sub2api_economic_source_a6_gateway_uidx
    ON a6_trade.sub2api_economic_sources (model)
    WHERE source_kind = 'a6_gateway';
CREATE INDEX IF NOT EXISTS sub2api_economic_source_enabled_idx
    ON a6_trade.sub2api_economic_sources (model, enabled, source_kind, source_id);

CREATE TABLE IF NOT EXISTS a6_trade.sub2api_source_cost_contracts (
    contract_id bigserial PRIMARY KEY,
    source_id bigint NOT NULL
        REFERENCES a6_trade.sub2api_economic_sources(source_id),
    cost_mode text NOT NULL,
    cycle_cost_cny numeric(30,12),
    declared_k_cny numeric(30,12),
    currency char(3) NOT NULL DEFAULT 'CNY',
    cost_basis text,
    quota_basis text,
    effective_at timestamptz NOT NULL,
    expires_at timestamptz NOT NULL,
    config_version text NOT NULL,
    source_hash char(64) NOT NULL,
    contract_state text NOT NULL DEFAULT 'active',
    contract_payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT sub2api_source_cost_mode_known CHECK (
        cost_mode IN ('declared_k', 'weekly_amortized')
    ),
    CONSTRAINT sub2api_source_cost_contract_shape CHECK (
        currency = 'CNY'
        AND effective_at < expires_at
        AND source_hash ~ '^[0-9a-fA-F]{64}$'
        AND (
            (
                cost_mode = 'declared_k'
                AND declared_k_cny IS NOT NULL AND declared_k_cny > 0
                AND cycle_cost_cny IS NULL
                AND cost_basis IS NULL AND quota_basis IS NULL
            )
            OR (
                cost_mode = 'weekly_amortized'
                AND cycle_cost_cny IS NOT NULL AND cycle_cost_cny > 0
                AND declared_k_cny IS NULL
                AND cost_basis = 'weekly'
                AND quota_basis IN ('full_cycle', 'remaining_at_acquisition')
            )
        )
    ),
    CONSTRAINT sub2api_source_cost_contract_state_known CHECK (
        contract_state IN ('active', 'expired', 'disabled', 'superseded')
    ),
    CONSTRAINT sub2api_source_cost_contract_payload_object CHECK (
        jsonb_typeof(contract_payload) = 'object'
    ),
    UNIQUE (source_id, config_version)
);

CREATE UNIQUE INDEX IF NOT EXISTS sub2api_source_cost_one_active_idx
    ON a6_trade.sub2api_source_cost_contracts (source_id)
    WHERE contract_state = 'active';
CREATE INDEX IF NOT EXISTS sub2api_source_cost_effective_idx
    ON a6_trade.sub2api_source_cost_contracts (
        source_id, effective_at DESC, expires_at DESC
    );

CREATE TABLE IF NOT EXISTS a6_trade.sub2api_source_quota_snapshots (
    snapshot_id bigserial PRIMARY KEY,
    source_id bigint NOT NULL
        REFERENCES a6_trade.sub2api_economic_sources(source_id),
    contract_id bigint NOT NULL
        REFERENCES a6_trade.sub2api_source_cost_contracts(contract_id),
    quota_epoch text NOT NULL,
    used_a numeric(30,12) NOT NULL,
    remaining_a numeric(30,12) NOT NULL,
    total_a numeric(30,12) NOT NULL,
    reset_at timestamptz NOT NULL,
    observed_at timestamptz NOT NULL,
    payload_hash char(64) NOT NULL,
    snapshot_state text NOT NULL DEFAULT 'VALID',
    source_payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT sub2api_source_quota_values_valid CHECK (
        btrim(quota_epoch) <> '' AND length(quota_epoch) <= 256
        AND used_a >= 0 AND remaining_a >= 0 AND total_a > 0
        AND total_a = used_a + remaining_a
        AND reset_at > observed_at
        AND payload_hash ~ '^[0-9a-fA-F]{64}$'
    ),
    CONSTRAINT sub2api_source_quota_state_known CHECK (
        snapshot_state IN ('VALID', 'INVALID', 'CONFLICT')
    ),
    CONSTRAINT sub2api_source_quota_payload_object CHECK (
        jsonb_typeof(source_payload) = 'object'
    ),
    UNIQUE (source_id, quota_epoch, observed_at, payload_hash)
);

CREATE INDEX IF NOT EXISTS sub2api_source_quota_latest_idx
    ON a6_trade.sub2api_source_quota_snapshots (
        source_id, observed_at DESC, snapshot_id DESC
    );
CREATE INDEX IF NOT EXISTS sub2api_source_quota_epoch_idx
    ON a6_trade.sub2api_source_quota_snapshots (
        source_id, quota_epoch, observed_at DESC
    );

CREATE TABLE IF NOT EXISTS a6_trade.sub2api_channel_revenue_snapshots (
    snapshot_id bigserial PRIMARY KEY,
    model text NOT NULL,
    channel_id bigint NOT NULL,
    enodot_api_key_id bigint,
    sub2api_group_id bigint,
    revenue_epoch text NOT NULL,
    channel_net_k_cny numeric(30,12) NOT NULL,
    settlement_contract_ref text NOT NULL,
    controlled_listing_refs jsonb NOT NULL DEFAULT '[]'::jsonb,
    observed_at timestamptz NOT NULL,
    payload_hash char(64) NOT NULL,
    snapshot_state text NOT NULL DEFAULT 'VALID',
    source_payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT sub2api_channel_revenue_identity_valid CHECK (
        btrim(model) <> '' AND channel_id > 0
        AND (enodot_api_key_id IS NULL OR enodot_api_key_id > 0)
        AND (sub2api_group_id IS NULL OR sub2api_group_id > 0)
        AND (enodot_api_key_id IS NOT NULL OR sub2api_group_id IS NOT NULL)
        AND btrim(revenue_epoch) <> '' AND length(revenue_epoch) <= 256
    ),
    CONSTRAINT sub2api_channel_revenue_value_valid CHECK (
        channel_net_k_cny > 0
        AND btrim(settlement_contract_ref) <> ''
        AND payload_hash ~ '^[0-9a-fA-F]{64}$'
    ),
    CONSTRAINT sub2api_channel_revenue_state_known CHECK (
        snapshot_state IN ('VALID', 'INVALID', 'CONFLICT')
    ),
    CONSTRAINT sub2api_channel_revenue_payload_shape CHECK (
        jsonb_typeof(controlled_listing_refs) = 'array'
        AND jsonb_typeof(source_payload) = 'object'
    ),
    UNIQUE (model, channel_id, revenue_epoch, observed_at, payload_hash)
);

CREATE INDEX IF NOT EXISTS sub2api_channel_revenue_latest_idx
    ON a6_trade.sub2api_channel_revenue_snapshots (
        model, channel_id, observed_at DESC, snapshot_id DESC
    );
CREATE INDEX IF NOT EXISTS sub2api_channel_revenue_api_key_idx
    ON a6_trade.sub2api_channel_revenue_snapshots (
        enodot_api_key_id, model, observed_at DESC
    ) WHERE enodot_api_key_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS sub2api_channel_revenue_group_idx
    ON a6_trade.sub2api_channel_revenue_snapshots (
        sub2api_group_id, model, observed_at DESC
    ) WHERE sub2api_group_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS a6_trade.sub2api_economic_generations (
    generation_id bigserial PRIMARY KEY,
    model text NOT NULL,
    economic_generation bigint NOT NULL,
    parent_generation bigint,
    input_hash char(64) NOT NULL,
    output_hash char(64) NOT NULL,
    generation_state text NOT NULL DEFAULT 'planned',
    plan_only boolean NOT NULL DEFAULT true,
    minimum_profit_on_cost_bps integer NOT NULL DEFAULT 2000,
    quota_snapshot_max_age_seconds integer NOT NULL DEFAULT 60,
    channel_revenue_max_age_seconds integer NOT NULL DEFAULT 30,
    health_max_age_seconds integer NOT NULL DEFAULT 30,
    config_version text NOT NULL,
    evaluated_at timestamptz NOT NULL,
    activated_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT sub2api_economic_generation_identity_valid CHECK (
        btrim(model) <> '' AND economic_generation > 0
        AND (parent_generation IS NULL OR parent_generation > 0)
        AND input_hash ~ '^[0-9a-fA-F]{64}$'
        AND output_hash ~ '^[0-9a-fA-F]{64}$'
    ),
    CONSTRAINT sub2api_economic_generation_state_known CHECK (
        generation_state IN ('planned', 'active', 'failed', 'superseded')
    ),
    CONSTRAINT sub2api_economic_generation_config_valid CHECK (
        minimum_profit_on_cost_bps BETWEEN 0 AND 100000
        AND quota_snapshot_max_age_seconds BETWEEN 1 AND 86400
        AND channel_revenue_max_age_seconds BETWEEN 1 AND 86400
        AND health_max_age_seconds BETWEEN 1 AND 86400
    ),
    CONSTRAINT sub2api_economic_generation_activation_valid CHECK (
        (generation_state = 'active' AND activated_at IS NOT NULL)
        OR (generation_state <> 'active')
    ),
    UNIQUE (model, economic_generation)
);

CREATE UNIQUE INDEX IF NOT EXISTS sub2api_economic_one_active_idx
    ON a6_trade.sub2api_economic_generations (model)
    WHERE generation_state = 'active';

CREATE TABLE IF NOT EXISTS a6_trade.sub2api_channel_source_eligibility (
    eligibility_id bigserial PRIMARY KEY,
    generation_id bigint NOT NULL
        REFERENCES a6_trade.sub2api_economic_generations(generation_id)
        ON DELETE CASCADE,
    model text NOT NULL,
    channel_id bigint NOT NULL,
    source_id bigint NOT NULL
        REFERENCES a6_trade.sub2api_economic_sources(source_id),
    economic_generation bigint NOT NULL,
    source_kind text NOT NULL,
    enodot_account_id bigint,
    cost_contract_id bigint
        REFERENCES a6_trade.sub2api_source_cost_contracts(contract_id),
    quota_snapshot_id bigint
        REFERENCES a6_trade.sub2api_source_quota_snapshots(snapshot_id),
    revenue_snapshot_id bigint NOT NULL
        REFERENCES a6_trade.sub2api_channel_revenue_snapshots(snapshot_id),
    quota_epoch text,
    source_k_cny numeric(30,12),
    channel_net_k_cny numeric(30,12),
    minimum_channel_k_cny numeric(30,12),
    profit_on_cost_bps integer,
    eligibility_state text NOT NULL,
    reason_code text NOT NULL,
    cost_snapshot_hash char(64),
    revenue_snapshot_hash char(64),
    capacity_epoch text,
    required_burn_a_per_second numeric(30,12) NOT NULL DEFAULT 0,
    confirmation_count integer NOT NULL DEFAULT 0,
    a6_routing_generation bigint,
    a6_cost_generation bigint,
    evaluated_at timestamptz NOT NULL,
    CONSTRAINT sub2api_channel_source_identity_valid CHECK (
        btrim(model) <> '' AND channel_id > 0 AND economic_generation > 0
        AND source_kind IN ('api', 'account', 'a6_gateway')
        AND (enodot_account_id IS NULL OR enodot_account_id > 0)
    ),
    CONSTRAINT sub2api_channel_source_state_known CHECK (
        eligibility_state IN (
            'DISCOVERED', 'COST_UNKNOWN', 'REVENUE_UNKNOWN', 'UNHEALTHY',
            'UNPROFITABLE', 'ELIGIBLE', 'THROTTLED', 'EXHAUSTED'
        )
    ),
    CONSTRAINT sub2api_channel_source_values_valid CHECK (
        (source_k_cny IS NULL OR source_k_cny > 0)
        AND (channel_net_k_cny IS NULL OR channel_net_k_cny > 0)
        AND (minimum_channel_k_cny IS NULL OR minimum_channel_k_cny > 0)
        AND required_burn_a_per_second >= 0
        AND confirmation_count >= 0
        AND (a6_routing_generation IS NULL OR a6_routing_generation > 0)
        AND (a6_cost_generation IS NULL OR a6_cost_generation > 0)
    ),
    CONSTRAINT sub2api_channel_source_eligible_complete CHECK (
        eligibility_state NOT IN ('ELIGIBLE', 'THROTTLED')
        OR (
            source_k_cny IS NOT NULL
            AND channel_net_k_cny IS NOT NULL
            AND minimum_channel_k_cny IS NOT NULL
            AND profit_on_cost_bps IS NOT NULL
            AND cost_snapshot_hash ~ '^[0-9a-fA-F]{64}$'
            AND revenue_snapshot_hash ~ '^[0-9a-fA-F]{64}$'
        )
    ),
    UNIQUE (model, economic_generation, channel_id, source_id),
    UNIQUE (generation_id, channel_id, source_id)
);

CREATE INDEX IF NOT EXISTS sub2api_channel_source_active_lookup_idx
    ON a6_trade.sub2api_channel_source_eligibility (
        model, economic_generation, channel_id, eligibility_state, source_k_cny, source_id
    );
CREATE INDEX IF NOT EXISTS sub2api_channel_source_account_idx
    ON a6_trade.sub2api_channel_source_eligibility (
        enodot_account_id, model, economic_generation
    ) WHERE enodot_account_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS a6_trade.sub2api_economic_projection_receipts (
    model text PRIMARY KEY,
    economic_generation bigint NOT NULL,
    projection_digest char(64) NOT NULL,
    redis_key text NOT NULL,
    projection_state text NOT NULL,
    projected_at timestamptz,
    last_error_class text,
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT sub2api_economic_projection_identity_valid CHECK (
        btrim(model) <> '' AND economic_generation > 0
        AND projection_digest ~ '^[0-9a-fA-F]{64}$'
        AND btrim(redis_key) <> ''
    ),
    CONSTRAINT sub2api_economic_projection_state_known CHECK (
        projection_state IN ('plan_only', 'projected', 'withdrawn', 'error')
    )
);

CREATE TABLE IF NOT EXISTS a6_trade.sub2api_economic_request_envelopes (
    envelope_id bigserial PRIMARY KEY,
    request_id text NOT NULL UNIQUE,
    api_key_id bigint NOT NULL,
    group_id bigint,
    model text NOT NULL,
    channel_id bigint,
    economic_generation bigint NOT NULL,
    candidate_hash char(64) NOT NULL,
    candidate_sources jsonb NOT NULL DEFAULT '[]'::jsonb,
    economic_hop_count integer NOT NULL DEFAULT 0,
    visited_source_fingerprints jsonb NOT NULL DEFAULT '[]'::jsonb,
    planned_a numeric(30,12),
    logical_outcome text,
    created_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz,
    UNIQUE (envelope_id, request_id),
    CONSTRAINT sub2api_economic_envelope_identity_valid CHECK (
        btrim(request_id) <> '' AND api_key_id > 0
        AND (group_id IS NULL OR group_id > 0)
        AND btrim(model) <> ''
        AND (channel_id IS NULL OR channel_id > 0)
        AND economic_generation > 0
        AND candidate_hash ~ '^[0-9a-fA-F]{64}$'
        AND economic_hop_count BETWEEN 0 AND 1
        AND (planned_a IS NULL OR planned_a >= 0)
    ),
    CONSTRAINT sub2api_economic_envelope_json_shape CHECK (
        jsonb_typeof(candidate_sources) = 'array'
        AND jsonb_typeof(visited_source_fingerprints) = 'array'
    )
);

CREATE TABLE IF NOT EXISTS a6_trade.sub2api_economic_attempts (
    attempt_id text PRIMARY KEY,
    envelope_id bigint NOT NULL,
    request_id text NOT NULL,
    attempt_no integer NOT NULL,
    model text NOT NULL,
    channel_id bigint NOT NULL,
    source_id bigint NOT NULL
        REFERENCES a6_trade.sub2api_economic_sources(source_id),
    enodot_account_id bigint NOT NULL,
    source_kind text NOT NULL,
    economic_generation bigint NOT NULL,
    cost_contract_id bigint
        REFERENCES a6_trade.sub2api_source_cost_contracts(contract_id),
    quota_epoch text,
    source_k_cny numeric(30,12) NOT NULL,
    channel_net_k_cny numeric(30,12) NOT NULL,
    minimum_channel_k_cny numeric(30,12) NOT NULL,
    planned_a numeric(30,12),
    realized_a numeric(30,12),
    planned_cost_cny numeric(30,12),
    realized_cost_cny numeric(30,12),
    planned_revenue_cny numeric(30,12),
    realized_revenue_cny numeric(30,12),
    planned_profit_cny numeric(30,12),
    realized_profit_cny numeric(30,12),
    profit_on_cost_bps integer NOT NULL,
    eligibility_reason text NOT NULL,
    attempt_outcome text NOT NULL DEFAULT 'started',
    started_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    CONSTRAINT sub2api_economic_attempt_envelope_fk
        FOREIGN KEY (envelope_id, request_id)
        REFERENCES a6_trade.sub2api_economic_request_envelopes(envelope_id, request_id)
        ON DELETE CASCADE,
    CONSTRAINT sub2api_economic_attempt_identity_valid CHECK (
        btrim(attempt_id) <> '' AND attempt_no > 0
        AND btrim(model) <> '' AND channel_id > 0
        AND source_kind IN ('api', 'account', 'a6_gateway')
        AND enodot_account_id > 0
        AND economic_generation > 0
    ),
    CONSTRAINT sub2api_economic_attempt_values_valid CHECK (
        source_k_cny > 0 AND channel_net_k_cny > 0 AND minimum_channel_k_cny > 0
        AND (planned_a IS NULL OR planned_a >= 0)
        AND (realized_a IS NULL OR realized_a >= 0)
        AND (planned_cost_cny IS NULL OR planned_cost_cny >= 0)
        AND (realized_cost_cny IS NULL OR realized_cost_cny >= 0)
        AND (planned_revenue_cny IS NULL OR planned_revenue_cny >= 0)
        AND (realized_revenue_cny IS NULL OR realized_revenue_cny >= 0)
    ),
    CONSTRAINT sub2api_economic_attempt_outcome_known CHECK (
        attempt_outcome IN (
            'started', 'succeeded', 'failed', 'won', 'lost', 'cancelled'
        )
    ),
    UNIQUE (request_id, attempt_no),
    UNIQUE (request_id, enodot_account_id)
);

CREATE INDEX IF NOT EXISTS sub2api_economic_attempt_request_idx
    ON a6_trade.sub2api_economic_attempts (request_id, attempt_no);
CREATE INDEX IF NOT EXISTS sub2api_economic_attempt_source_time_idx
    ON a6_trade.sub2api_economic_attempts (source_id, started_at DESC);
CREATE INDEX IF NOT EXISTS sub2api_economic_attempt_negative_profit_idx
    ON a6_trade.sub2api_economic_attempts (finished_at DESC)
    WHERE realized_profit_cny < 0;

-- Every committed economic fact wakes the projector. PostgreSQL delivers
-- NOTIFY only after commit, while the 15-second timer remains the loss repair.
CREATE OR REPLACE FUNCTION a6_trade.notify_economic_model_fact_changed()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, a6_trade
AS $$
DECLARE
    fact_model text;
BEGIN
    IF TG_OP = 'DELETE' THEN
        fact_model := OLD.model;
    ELSE
        fact_model := NEW.model;
    END IF;
    IF fact_model IS NOT NULL AND btrim(fact_model) <> '' THEN
        PERFORM pg_notify(
            'a6_economic_fact_changed',
            json_build_object(
                'model', fact_model,
                'event', TG_TABLE_NAME,
                'operation', lower(TG_OP)
            )::text
        );
    END IF;
    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION a6_trade.notify_economic_source_fact_changed()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, a6_trade
AS $$
DECLARE
    fact_model text;
    fact_source_id bigint;
BEGIN
    IF TG_OP = 'DELETE' THEN
        fact_source_id := OLD.source_id;
    ELSE
        fact_source_id := NEW.source_id;
    END IF;
    SELECT source.model
    INTO fact_model
    FROM a6_trade.sub2api_economic_sources source
    WHERE source.source_id = fact_source_id;
    IF fact_model IS NOT NULL AND btrim(fact_model) <> '' THEN
        PERFORM pg_notify(
            'a6_economic_fact_changed',
            json_build_object(
                'model', fact_model,
                'event', TG_TABLE_NAME,
                'operation', lower(TG_OP),
                'identity', fact_source_id
            )::text
        );
    END IF;
    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS sub2api_economic_sources_notify
    ON a6_trade.sub2api_economic_sources;
CREATE TRIGGER sub2api_economic_sources_notify
AFTER INSERT OR UPDATE OR DELETE ON a6_trade.sub2api_economic_sources
FOR EACH ROW EXECUTE FUNCTION a6_trade.notify_economic_model_fact_changed();

DROP TRIGGER IF EXISTS sub2api_source_cost_contracts_notify
    ON a6_trade.sub2api_source_cost_contracts;
CREATE TRIGGER sub2api_source_cost_contracts_notify
AFTER INSERT OR UPDATE OR DELETE ON a6_trade.sub2api_source_cost_contracts
FOR EACH ROW EXECUTE FUNCTION a6_trade.notify_economic_source_fact_changed();

DROP TRIGGER IF EXISTS sub2api_source_quota_snapshots_notify
    ON a6_trade.sub2api_source_quota_snapshots;
CREATE TRIGGER sub2api_source_quota_snapshots_notify
AFTER INSERT OR UPDATE OR DELETE ON a6_trade.sub2api_source_quota_snapshots
FOR EACH ROW EXECUTE FUNCTION a6_trade.notify_economic_source_fact_changed();

DROP TRIGGER IF EXISTS sub2api_channel_revenue_snapshots_notify
    ON a6_trade.sub2api_channel_revenue_snapshots;
CREATE TRIGGER sub2api_channel_revenue_snapshots_notify
AFTER INSERT OR UPDATE OR DELETE ON a6_trade.sub2api_channel_revenue_snapshots
FOR EACH ROW EXECUTE FUNCTION a6_trade.notify_economic_model_fact_changed();

DROP TRIGGER IF EXISTS sub2api_economic_attempts_notify
    ON a6_trade.sub2api_economic_attempts;
CREATE TRIGGER sub2api_economic_attempts_notify
AFTER INSERT OR UPDATE OR DELETE ON a6_trade.sub2api_economic_attempts
FOR EACH ROW EXECUTE FUNCTION a6_trade.notify_economic_model_fact_changed();

DROP TRIGGER IF EXISTS provider_pool_generations_economic_notify
    ON a6_trade.provider_pool_generations;
CREATE TRIGGER provider_pool_generations_economic_notify
AFTER INSERT OR UPDATE OR DELETE ON a6_trade.provider_pool_generations
FOR EACH ROW EXECUTE FUNCTION a6_trade.notify_economic_model_fact_changed();

DROP TRIGGER IF EXISTS provider_cost_generations_economic_notify
    ON a6_trade.provider_cost_generations;
CREATE TRIGGER provider_cost_generations_economic_notify
AFTER INSERT OR UPDATE OR DELETE ON a6_trade.provider_cost_generations
FOR EACH ROW EXECUTE FUNCTION a6_trade.notify_economic_model_fact_changed();

DROP TRIGGER IF EXISTS provider_pool_slots_economic_notify
    ON a6_trade.provider_pool_slots;
CREATE TRIGGER provider_pool_slots_economic_notify
AFTER INSERT OR UPDATE OR DELETE ON a6_trade.provider_pool_slots
FOR EACH ROW EXECUTE FUNCTION a6_trade.notify_economic_model_fact_changed();

DO $$
DECLARE
    request_plane_role text;
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'a6trader') THEN
        GRANT SELECT, INSERT, UPDATE, DELETE ON
            a6_trade.sub2api_economic_sources,
            a6_trade.sub2api_source_cost_contracts,
            a6_trade.sub2api_source_quota_snapshots,
            a6_trade.sub2api_channel_revenue_snapshots,
            a6_trade.sub2api_economic_generations,
            a6_trade.sub2api_channel_source_eligibility,
            a6_trade.sub2api_economic_projection_receipts,
            a6_trade.sub2api_economic_request_envelopes,
            a6_trade.sub2api_economic_attempts TO a6trader;
        GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA a6_trade TO a6trader;
    END IF;

    FOREACH request_plane_role IN ARRAY ARRAY['sub2api_cm', 'sub2api']
    LOOP
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = request_plane_role) THEN
            EXECUTE format('GRANT USAGE ON SCHEMA a6_trade TO %I', request_plane_role);
            EXECUTE format(
                'GRANT SELECT ON TABLE '
                'a6_trade.sub2api_economic_sources, '
                'a6_trade.sub2api_source_cost_contracts, '
                'a6_trade.sub2api_source_quota_snapshots, '
                'a6_trade.sub2api_channel_revenue_snapshots, '
                'a6_trade.sub2api_economic_generations, '
                'a6_trade.sub2api_channel_source_eligibility, '
                'a6_trade.sub2api_economic_projection_receipts TO %I',
                request_plane_role
            );
            EXECUTE format(
                'GRANT SELECT, INSERT, UPDATE ON TABLE '
                'a6_trade.sub2api_economic_request_envelopes, '
                'a6_trade.sub2api_economic_attempts TO %I',
                request_plane_role
            );
            EXECUTE format(
                'GRANT USAGE, SELECT ON SEQUENCE '
                'a6_trade.sub2api_economic_request_envelopes_envelope_id_seq TO %I',
                request_plane_role
            );
        END IF;
    END LOOP;
END
$$;
