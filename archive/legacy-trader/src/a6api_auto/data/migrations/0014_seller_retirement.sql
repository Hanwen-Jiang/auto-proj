ALTER TABLE a6_trade.a6_merchants
    DROP CONSTRAINT IF EXISTS a6_merchants_roles_base_seller;
ALTER TABLE a6_trade.a6_merchants
    ADD CONSTRAINT a6_merchants_roles_operational_shape CHECK (
        roles = ARRAY['seller']::text[]
        OR roles = ARRAY['provider']::text[]
        OR roles = ARRAY['seller', 'provider']::text[]
        OR roles = ARRAY['seller', 'observer']::text[]
    );

CREATE TABLE IF NOT EXISTS a6_trade.a6_seller_retirement_plans (
    merchant_id bigint PRIMARY KEY REFERENCES a6_trade.a6_merchants(id) ON DELETE CASCADE,
    threshold_quota bigint NOT NULL,
    state text NOT NULL DEFAULT 'earning',
    transfer_intent_id bigint REFERENCES a6_trade.a6_balance_transfer_intents(id),
    external_a6_token_id bigint,
    enodot_account_id bigint,
    attempt_count integer NOT NULL DEFAULT 0,
    last_error_class text,
    retry_after timestamptz,
    last_started_at timestamptz,
    completed_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT a6_seller_retirement_threshold_positive CHECK (threshold_quota > 0),
    CONSTRAINT a6_seller_retirement_attempt_count_nonnegative CHECK (attempt_count >= 0),
    CONSTRAINT a6_seller_retirement_state_known CHECK (
        state IN (
            'earning', 'checking', 'transfer_unknown', 'transferred',
            'binding', 'taking_down', 'role_switch', 'completed'
        )
    ),
    CONSTRAINT a6_seller_retirement_completed_consistent CHECK (
        (state = 'completed') = (completed_at IS NOT NULL)
    )
);

CREATE INDEX IF NOT EXISTS a6_seller_retirement_state_retry_idx
    ON a6_trade.a6_seller_retirement_plans (state, retry_after, updated_at);

CREATE TABLE IF NOT EXISTS a6_trade.a6_seller_retirement_events (
    id bigserial PRIMARY KEY,
    merchant_id bigint NOT NULL REFERENCES a6_trade.a6_seller_retirement_plans(merchant_id)
        ON DELETE CASCADE,
    event_type text NOT NULL,
    from_state text,
    to_state text,
    details jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT a6_seller_retirement_event_type_nonempty CHECK (event_type <> ''),
    CONSTRAINT a6_seller_retirement_event_details_object CHECK (
        jsonb_typeof(details) = 'object'
    )
);

CREATE INDEX IF NOT EXISTS a6_seller_retirement_events_merchant_time_idx
    ON a6_trade.a6_seller_retirement_events (merchant_id, created_at DESC);
