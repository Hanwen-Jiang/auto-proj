-- Event-driven A6 marketplace auto-pool routing. The CNY ratio is the
-- authoritative marketplace cap; the USD ratio is the same snapshot projected
-- into Enodot billing and Seller pricing.

CREATE TABLE IF NOT EXISTS a6_trade.provider_auto_pool_generations (
    id bigserial PRIMARY KEY,
    model text NOT NULL,
    auto_pool_generation bigint NOT NULL,
    parent_auto_pool_generation bigint,
    routing_generation bigint NOT NULL,
    cost_generation bigint NOT NULL,
    source_hash char(64) NOT NULL,
    observer_scan_ids bigint[] NOT NULL DEFAULT '{}',
    selected_channel_ids bigint[] NOT NULL DEFAULT '{}',
    max_ratio_cny numeric(30,12) NOT NULL,
    max_ratio_usd numeric(30,12) NOT NULL,
    target_channel_count integer NOT NULL,
    minimum_observers integer NOT NULL,
    hard_max_ratio_cny numeric(30,12) NOT NULL,
    trigger_reason text NOT NULL,
    generation_state text NOT NULL DEFAULT 'planned',
    observed_at timestamptz NOT NULL,
    planned_at timestamptz NOT NULL DEFAULT now(),
    activated_at timestamptz,
    completed_at timestamptz,
    config_version text NOT NULL,
    plan_payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (model, auto_pool_generation),
    CONSTRAINT provider_auto_pool_generation_identity_valid CHECK (
        btrim(model) <> ''
        AND auto_pool_generation > 0
        AND routing_generation > 0
        AND cost_generation > 0
        AND (
            parent_auto_pool_generation IS NULL
            OR parent_auto_pool_generation > 0
        )
        AND source_hash ~ '^[0-9a-fA-F]{64}$'
    ),
    CONSTRAINT provider_auto_pool_generation_ratio_valid CHECK (
        max_ratio_cny > 0
        AND max_ratio_usd > 0
        AND hard_max_ratio_cny > 0
        AND max_ratio_cny <= hard_max_ratio_cny
        AND target_channel_count > 0
        AND minimum_observers > 0
        AND cardinality(selected_channel_ids) > 0
    ),
    CONSTRAINT provider_auto_pool_generation_state_known CHECK (
        generation_state IN (
            'planned', 'applying', 'active', 'failed', 'superseded'
        )
    ),
    CONSTRAINT provider_auto_pool_generation_activation_valid CHECK (
        (generation_state = 'active' AND activated_at IS NOT NULL)
        OR generation_state <> 'active'
    ),
    CONSTRAINT provider_auto_pool_generation_payload_object CHECK (
        jsonb_typeof(plan_payload) = 'object'
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS provider_auto_pool_one_active_idx
    ON a6_trade.provider_auto_pool_generations (model)
    WHERE generation_state = 'active';

CREATE INDEX IF NOT EXISTS provider_auto_pool_generation_time_idx
    ON a6_trade.provider_auto_pool_generations (
        model, planned_at DESC, auto_pool_generation DESC
    );

CREATE TABLE IF NOT EXISTS a6_trade.provider_auto_pool_accounts (
    id bigserial PRIMARY KEY,
    generation_id bigint NOT NULL
        REFERENCES a6_trade.provider_auto_pool_generations(id) ON DELETE CASCADE,
    merchant_id bigint NOT NULL REFERENCES a6_trade.a6_merchants(id),
    token_ref_id bigint NOT NULL REFERENCES a6_trade.a6_tokens(id),
    external_token_id bigint NOT NULL,
    enodot_account_id bigint NOT NULL,
    slot_no integer NOT NULL,
    subscription_id bigint,
    account_state text NOT NULL DEFAULT 'pending',
    visible_owned_channel_ids bigint[] NOT NULL DEFAULT '{}',
    disabled_owned_channel_ids bigint[] NOT NULL DEFAULT '{}',
    absent_owned_channel_ids bigint[] NOT NULL DEFAULT '{}',
    ban_confirmed boolean NOT NULL DEFAULT false,
    unpin_confirmed boolean NOT NULL DEFAULT false,
    auto_pool_confirmed boolean NOT NULL DEFAULT false,
    cost_confirmed boolean NOT NULL DEFAULT false,
    schedulable_confirmed boolean NOT NULL DEFAULT false,
    observed_max_ratio_cny numeric(30,12),
    observed_max_ratio_usd numeric(30,12),
    readback_hash char(64),
    error_class text,
    remote_readback jsonb NOT NULL DEFAULT '{}'::jsonb,
    started_at timestamptz,
    completed_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (generation_id, merchant_id),
    UNIQUE (generation_id, token_ref_id),
    UNIQUE (generation_id, enodot_account_id),
    UNIQUE (generation_id, slot_no),
    CONSTRAINT provider_auto_pool_account_identity_valid CHECK (
        merchant_id > 0 AND token_ref_id > 0 AND external_token_id > 0
        AND enodot_account_id > 0 AND slot_no > 0
        AND (subscription_id IS NULL OR subscription_id > 0)
    ),
    CONSTRAINT provider_auto_pool_account_state_known CHECK (
        account_state IN (
            'pending', 'applying', 'ready', 'failed', 'unfunded', 'superseded'
        )
    ),
    CONSTRAINT provider_auto_pool_account_ratio_valid CHECK (
        (observed_max_ratio_cny IS NULL OR observed_max_ratio_cny > 0)
        AND (observed_max_ratio_usd IS NULL OR observed_max_ratio_usd > 0)
    ),
    CONSTRAINT provider_auto_pool_account_ready_contract CHECK (
        account_state <> 'ready' OR (
            subscription_id IS NOT NULL
            AND ban_confirmed AND unpin_confirmed AND auto_pool_confirmed
            AND cost_confirmed AND schedulable_confirmed
            AND observed_max_ratio_cny IS NOT NULL
            AND observed_max_ratio_usd IS NOT NULL
            AND readback_hash IS NOT NULL
            AND completed_at IS NOT NULL
            AND error_class IS NULL
        )
    ),
    CONSTRAINT provider_auto_pool_account_hash_valid CHECK (
        readback_hash IS NULL OR readback_hash ~ '^[0-9a-fA-F]{64}$'
    ),
    CONSTRAINT provider_auto_pool_account_readback_object CHECK (
        jsonb_typeof(remote_readback) = 'object'
    )
);

CREATE INDEX IF NOT EXISTS provider_auto_pool_account_state_idx
    ON a6_trade.provider_auto_pool_accounts (
        generation_id, account_state, slot_no
    );

ALTER TABLE a6_trade.provider_pool_slots
    ADD COLUMN IF NOT EXISTS route_mode text NOT NULL DEFAULT 'fixed_pin',
    ADD COLUMN IF NOT EXISTS auto_pool_generation_id bigint
        REFERENCES a6_trade.provider_auto_pool_generations(id),
    ADD COLUMN IF NOT EXISTS auto_pool_subscription_id bigint,
    ADD COLUMN IF NOT EXISTS auto_pool_confirmed boolean NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS auto_pool_max_ratio_cny numeric(30,12);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'provider_pool_slot_route_mode_known'
          AND conrelid = 'a6_trade.provider_pool_slots'::regclass
    ) THEN
        ALTER TABLE a6_trade.provider_pool_slots
            ADD CONSTRAINT provider_pool_slot_route_mode_known CHECK (
                route_mode IN ('fixed_pin', 'auto_pool')
            );
    END IF;
END
$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'provider_pool_slot_auto_pool_shape'
          AND conrelid = 'a6_trade.provider_pool_slots'::regclass
    ) THEN
        ALTER TABLE a6_trade.provider_pool_slots
            ADD CONSTRAINT provider_pool_slot_auto_pool_shape CHECK (
                (
                    route_mode = 'fixed_pin'
                    AND auto_pool_generation_id IS NULL
                    AND auto_pool_subscription_id IS NULL
                    AND NOT auto_pool_confirmed
                    AND auto_pool_max_ratio_cny IS NULL
                ) OR (
                    route_mode = 'auto_pool'
                    AND auto_pool_generation_id IS NOT NULL
                    AND auto_pool_subscription_id IS NOT NULL
                    AND auto_pool_subscription_id > 0
                    AND auto_pool_max_ratio_cny IS NOT NULL
                    AND auto_pool_max_ratio_cny > 0
                )
            );
    END IF;
END
$$;

ALTER TABLE a6_trade.provider_pool_slots
    DROP CONSTRAINT IF EXISTS provider_pool_slot_ready_pin_required;

ALTER TABLE a6_trade.provider_pool_slots
    DROP CONSTRAINT IF EXISTS provider_pool_slot_ready_contract;

ALTER TABLE a6_trade.provider_pool_slots
    ADD CONSTRAINT provider_pool_slot_ready_contract CHECK (
        slot_state <> 'READY' OR (
            enodot_account_id IS NOT NULL AND a6_token_id IS NOT NULL
            AND desired_rank IS NOT NULL AND desired_priority IS NOT NULL
            AND desired_slot_cost_multiplier IS NOT NULL
            AND observed_rate_multiplier = desired_slot_cost_multiplier
            AND funding_eligible AND desired_schedulable AND observed_schedulable
            AND (
                (
                    route_mode = 'fixed_pin'
                    AND desired_channel_id IS NOT NULL
                    AND confirmed_pin_binding_id IS NOT NULL
                    AND pin_confirmed AND probe_confirmed
                ) OR (
                    route_mode = 'auto_pool'
                    AND desired_channel_id IS NULL
                    AND confirmed_pin_binding_id IS NULL
                    AND NOT pin_confirmed
                    AND auto_pool_confirmed
                )
            )
        )
    );

DROP INDEX IF EXISTS a6_trade.provider_pool_ready_channel_idx;
CREATE UNIQUE INDEX provider_pool_ready_channel_idx
    ON a6_trade.provider_pool_slots (model, desired_channel_id)
    WHERE valid_to IS NULL
      AND slot_state = 'READY'
      AND route_mode = 'fixed_pin'
      AND desired_channel_id IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS provider_pool_current_auto_subscription_idx
    ON a6_trade.provider_pool_slots (model, auto_pool_subscription_id)
    WHERE valid_to IS NULL
      AND route_mode = 'auto_pool'
      AND auto_pool_subscription_id IS NOT NULL;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'a6trader') THEN
        GRANT SELECT, INSERT, UPDATE
            ON a6_trade.provider_auto_pool_generations,
               a6_trade.provider_auto_pool_accounts TO a6trader;
        GRANT USAGE, SELECT
            ON SEQUENCE a6_trade.provider_auto_pool_generations_id_seq,
                        a6_trade.provider_auto_pool_accounts_id_seq TO a6trader;
    END IF;
END
$$;
