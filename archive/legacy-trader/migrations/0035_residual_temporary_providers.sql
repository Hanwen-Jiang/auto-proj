-- Retired Observer accounts may spend a bounded remainder of their A6 user balance as
-- temporary Providers.  This is an explicit, per-merchant policy: the normal Provider
-- liquidity floor stays unchanged, and reaching the reserve line retires the duty again.

CREATE TABLE IF NOT EXISTS a6_trade.residual_provider_policies (
    model text NOT NULL,
    merchant_id bigint NOT NULL REFERENCES a6_trade.a6_merchants(id),
    enabled boolean NOT NULL DEFAULT true,
    state text NOT NULL DEFAULT 'candidate',
    reserve_balance_quota bigint NOT NULL,
    desired_priority smallint NOT NULL DEFAULT 1,
    desired_concurrency integer NOT NULL DEFAULT 15,
    last_balance_quota bigint,
    activated_at timestamptz,
    drained_at timestamptz,
    last_error_class text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (model, merchant_id),
    CONSTRAINT residual_provider_model_nonempty CHECK (btrim(model) <> ''),
    CONSTRAINT residual_provider_state_known CHECK (
        state IN ('candidate', 'active', 'drained', 'error', 'disabled')
    ),
    CONSTRAINT residual_provider_reserve_nonnegative CHECK (reserve_balance_quota >= 0),
    CONSTRAINT residual_provider_balance_nonnegative CHECK (
        last_balance_quota IS NULL OR last_balance_quota >= 0
    ),
    CONSTRAINT residual_provider_priority_valid CHECK (
        desired_priority BETWEEN 1 AND 32767
    ),
    CONSTRAINT residual_provider_concurrency_valid CHECK (
        desired_concurrency BETWEEN 1 AND 1000
    )
);

CREATE INDEX IF NOT EXISTS residual_provider_active_idx
    ON a6_trade.residual_provider_policies (model, state, updated_at)
    WHERE enabled AND state IN ('candidate', 'active');

CREATE TABLE IF NOT EXISTS a6_trade.residual_provider_events (
    id bigserial PRIMARY KEY,
    model text NOT NULL,
    merchant_id bigint NOT NULL,
    event_type text NOT NULL,
    balance_quota bigint,
    reserve_balance_quota bigint NOT NULL,
    details jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT residual_provider_event_policy_fk
        FOREIGN KEY (model, merchant_id)
        REFERENCES a6_trade.residual_provider_policies(model, merchant_id),
    CONSTRAINT residual_provider_event_balance_nonnegative CHECK (
        balance_quota IS NULL OR balance_quota >= 0
    ),
    CONSTRAINT residual_provider_event_reserve_nonnegative CHECK (
        reserve_balance_quota >= 0
    ),
    CONSTRAINT residual_provider_event_details_object CHECK (
        jsonb_typeof(details) = 'object'
    )
);

CREATE INDEX IF NOT EXISTS residual_provider_events_merchant_time_idx
    ON a6_trade.residual_provider_events (merchant_id, created_at DESC, id DESC);

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'a6trader') THEN
        GRANT SELECT, INSERT, UPDATE, DELETE
            ON a6_trade.residual_provider_policies,
               a6_trade.residual_provider_events TO a6trader;
        GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA a6_trade TO a6trader;
    END IF;
END
$$;
