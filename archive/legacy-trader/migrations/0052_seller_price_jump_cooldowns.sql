-- Hard guard for a single managed Listing whose newly calculated panel price
-- is at least twice its last successfully applied price.  The baseline remains
-- frozen for the lifetime of the cooldown; repeated high-cost Pin generations
-- only update latest_target_price_multiplier.

CREATE TABLE a6_trade.seller_price_jump_cooldowns (
    id bigserial PRIMARY KEY,
    model text NOT NULL,
    merchant_id bigint NOT NULL,
    entity_id text NOT NULL,
    baseline_price_multiplier numeric(30,12) NOT NULL,
    trigger_price_multiplier numeric(30,12) NOT NULL,
    latest_target_price_multiplier numeric(30,12) NOT NULL,
    threshold_ratio numeric(12,6) NOT NULL DEFAULT 2,
    trigger_snapshot_fingerprint char(64) NOT NULL,
    latest_snapshot_fingerprint char(64) NOT NULL,
    state text NOT NULL DEFAULT 'cooling',
    started_at timestamptz NOT NULL,
    cooling_until timestamptz NOT NULL,
    release_reason text,
    release_requested_at timestamptz,
    completed_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT seller_price_jump_state_known CHECK (
        state IN ('cooling', 'release_pending', 'completed')
    ),
    CONSTRAINT seller_price_jump_values_valid CHECK (
        baseline_price_multiplier > 0
        AND trigger_price_multiplier >= threshold_ratio * baseline_price_multiplier
        AND latest_target_price_multiplier > 0
        AND threshold_ratio >= 2
    ),
    CONSTRAINT seller_price_jump_hashes_valid CHECK (
        trigger_snapshot_fingerprint ~ '^[0-9a-f]{64}$'
        AND latest_snapshot_fingerprint ~ '^[0-9a-f]{64}$'
    ),
    CONSTRAINT seller_price_jump_window_valid CHECK (
        cooling_until >= started_at + interval '30 minutes'
        AND cooling_until <= started_at + interval '45 minutes'
    ),
    CONSTRAINT seller_price_jump_release_valid CHECK (
        (
            state = 'cooling'
            AND release_reason IS NULL
            AND release_requested_at IS NULL
            AND completed_at IS NULL
        )
        OR (
            state = 'release_pending'
            AND release_reason IN ('cost_recovered', 'cooldown_elapsed')
            AND release_requested_at IS NOT NULL
            AND completed_at IS NULL
        )
        OR (
            state = 'completed'
            AND release_reason IN ('cost_recovered', 'cooldown_elapsed')
            AND release_requested_at IS NOT NULL
            AND completed_at IS NOT NULL
        )
    )
);

CREATE UNIQUE INDEX seller_price_jump_one_active_idx
    ON a6_trade.seller_price_jump_cooldowns (model, entity_id)
    WHERE state IN ('cooling', 'release_pending');

CREATE INDEX seller_price_jump_due_idx
    ON a6_trade.seller_price_jump_cooldowns (cooling_until, id)
    WHERE state = 'cooling';

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'a6trader') THEN
        GRANT SELECT, INSERT, UPDATE
            ON a6_trade.seller_price_jump_cooldowns TO a6trader;
        GRANT USAGE, SELECT
            ON SEQUENCE a6_trade.seller_price_jump_cooldowns_id_seq TO a6trader;
    END IF;
END
$$;
