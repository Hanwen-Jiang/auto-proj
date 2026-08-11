ALTER TABLE a6_trade.a6_merchants
    DROP CONSTRAINT IF EXISTS a6_merchants_roles_operational_shape;
ALTER TABLE a6_trade.a6_merchants
    ADD CONSTRAINT a6_merchants_roles_operational_shape CHECK (
        roles = ARRAY['seller']::text[]
        OR roles = ARRAY['provider']::text[]
        OR roles = ARRAY['observer']::text[]
        OR roles = ARRAY['provider', 'observer']::text[]
        OR roles = ARRAY['seller', 'provider']::text[]
        OR roles = ARRAY['seller', 'observer']::text[]
    );

ALTER TABLE a6_trade.a6_seller_retirement_plans
    ADD COLUMN IF NOT EXISTS archive_mode boolean NOT NULL DEFAULT false;
ALTER TABLE a6_trade.a6_seller_retirement_plans
    ADD COLUMN IF NOT EXISTS requested_at timestamptz;

ALTER TABLE a6_trade.a6_seller_retirement_plans
    DROP CONSTRAINT IF EXISTS a6_seller_retirement_state_known;
ALTER TABLE a6_trade.a6_seller_retirement_plans
    ADD CONSTRAINT a6_seller_retirement_state_known CHECK (
        state IN (
            'earning', 'checking', 'transfer_unknown', 'transferred',
            'binding', 'taking_down', 'role_switch', 'draining', 'completed'
        )
    );

CREATE INDEX IF NOT EXISTS a6_seller_retirement_archive_progress_idx
    ON a6_trade.a6_seller_retirement_plans (
        archive_mode, state, retry_after, updated_at
    )
    WHERE archive_mode;
