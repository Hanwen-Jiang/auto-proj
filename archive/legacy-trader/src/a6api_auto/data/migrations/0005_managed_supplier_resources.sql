CREATE TABLE IF NOT EXISTS a6_trade.a6_managed_channel_bindings (
    id bigserial PRIMARY KEY,
    managed_uid text NOT NULL UNIQUE,
    merchant_id bigint NOT NULL REFERENCES a6_trade.a6_merchants(id) ON DELETE CASCADE,
    a6_channel_id bigint,
    create_idempotency_key text NOT NULL UNIQUE,
    control_state text NOT NULL DEFAULT 'planned',
    desired_state jsonb NOT NULL DEFAULT '{}'::jsonb,
    observed_state jsonb,
    create_action_id bigint REFERENCES a6_trade.trade_control_actions(id),
    last_action_id bigint REFERENCES a6_trade.trade_control_actions(id),
    last_error_class text,
    first_attempted_at timestamptz,
    last_attempted_at timestamptz,
    last_observed_at timestamptz,
    verified_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT a6_managed_channels_uid_nonempty CHECK (
        managed_uid <> '' AND length(managed_uid) <= 256
    ),
    CONSTRAINT a6_managed_channels_idempotency_nonempty CHECK (
        create_idempotency_key <> '' AND length(create_idempotency_key) <= 512
    ),
    CONSTRAINT a6_managed_channels_external_id_positive CHECK (
        a6_channel_id IS NULL OR a6_channel_id > 0
    ),
    CONSTRAINT a6_managed_channels_control_state_known CHECK (
        control_state IN (
            'planned', 'creating', 'create_unknown', 'verifying',
            'verified', 'updating', 'update_unknown', 'error', 'retired'
        )
    ),
    CONSTRAINT a6_managed_channels_desired_object CHECK (
        jsonb_typeof(desired_state) = 'object'
    ),
    CONSTRAINT a6_managed_channels_observed_object CHECK (
        observed_state IS NULL OR jsonb_typeof(observed_state) = 'object'
    ),
    CONSTRAINT a6_managed_channels_verified_state_complete CHECK (
        control_state <> 'verified'
        OR (
            a6_channel_id IS NOT NULL
            AND create_action_id IS NOT NULL
            AND observed_state IS NOT NULL
            AND verified_at IS NOT NULL
        )
    ),
    CONSTRAINT a6_managed_channels_observation_time_consistent CHECK (
        last_observed_at IS NOT NULL OR observed_state IS NULL
    ),
    UNIQUE (merchant_id, id),
    UNIQUE (merchant_id, id, a6_channel_id),
    FOREIGN KEY (merchant_id, a6_channel_id)
        REFERENCES a6_trade.a6_supplier_channels_current (merchant_id, a6_channel_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS a6_managed_channels_external_uidx
    ON a6_trade.a6_managed_channel_bindings (a6_channel_id)
    WHERE a6_channel_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS a6_managed_channels_create_action_uidx
    ON a6_trade.a6_managed_channel_bindings (create_action_id)
    WHERE create_action_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS a6_managed_channels_merchant_state_idx
    ON a6_trade.a6_managed_channel_bindings (merchant_id, control_state, updated_at);
CREATE INDEX IF NOT EXISTS a6_managed_channels_last_action_idx
    ON a6_trade.a6_managed_channel_bindings (last_action_id)
    WHERE last_action_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS a6_trade.a6_managed_listing_bindings (
    id bigserial PRIMARY KEY,
    managed_uid text NOT NULL UNIQUE,
    merchant_id bigint NOT NULL REFERENCES a6_trade.a6_merchants(id) ON DELETE CASCADE,
    channel_binding_id bigint NOT NULL,
    a6_channel_id bigint,
    a6_listing_id bigint,
    create_idempotency_key text NOT NULL UNIQUE,
    control_state text NOT NULL DEFAULT 'planned',
    desired_state jsonb NOT NULL DEFAULT '{}'::jsonb,
    observed_state jsonb,
    create_action_id bigint REFERENCES a6_trade.trade_control_actions(id),
    last_action_id bigint REFERENCES a6_trade.trade_control_actions(id),
    last_error_class text,
    first_attempted_at timestamptz,
    last_attempted_at timestamptz,
    last_observed_at timestamptz,
    verified_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT a6_managed_listings_uid_nonempty CHECK (
        managed_uid <> '' AND length(managed_uid) <= 256
    ),
    CONSTRAINT a6_managed_listings_idempotency_nonempty CHECK (
        create_idempotency_key <> '' AND length(create_idempotency_key) <= 512
    ),
    CONSTRAINT a6_managed_listings_external_ids_positive CHECK (
        (a6_channel_id IS NULL OR a6_channel_id > 0)
        AND (a6_listing_id IS NULL OR a6_listing_id > 0)
    ),
    CONSTRAINT a6_managed_listings_control_state_known CHECK (
        control_state IN (
            'planned', 'creating', 'create_unknown', 'verifying', 'verified',
            'updating', 'update_unknown', 'taking_down', 'takedown_unknown',
            'resubmitting', 'resubmit_unknown', 'error', 'retired'
        )
    ),
    CONSTRAINT a6_managed_listings_desired_object CHECK (
        jsonb_typeof(desired_state) = 'object'
    ),
    CONSTRAINT a6_managed_listings_observed_object CHECK (
        observed_state IS NULL OR jsonb_typeof(observed_state) = 'object'
    ),
    CONSTRAINT a6_managed_listings_verified_state_complete CHECK (
        control_state <> 'verified'
        OR (
            a6_channel_id IS NOT NULL
            AND a6_listing_id IS NOT NULL
            AND create_action_id IS NOT NULL
            AND observed_state IS NOT NULL
            AND verified_at IS NOT NULL
        )
    ),
    CONSTRAINT a6_managed_listings_observation_time_consistent CHECK (
        last_observed_at IS NOT NULL OR observed_state IS NULL
    ),
    FOREIGN KEY (merchant_id, channel_binding_id)
        REFERENCES a6_trade.a6_managed_channel_bindings (merchant_id, id),
    FOREIGN KEY (merchant_id, channel_binding_id, a6_channel_id)
        REFERENCES a6_trade.a6_managed_channel_bindings (merchant_id, id, a6_channel_id),
    FOREIGN KEY (merchant_id, a6_listing_id)
        REFERENCES a6_trade.a6_supplier_listings_current (merchant_id, a6_listing_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS a6_managed_listings_external_uidx
    ON a6_trade.a6_managed_listing_bindings (a6_listing_id)
    WHERE a6_listing_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS a6_managed_listings_create_action_uidx
    ON a6_trade.a6_managed_listing_bindings (create_action_id)
    WHERE create_action_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS a6_managed_listings_channel_binding_idx
    ON a6_trade.a6_managed_listing_bindings (channel_binding_id, control_state);
CREATE INDEX IF NOT EXISTS a6_managed_listings_merchant_state_idx
    ON a6_trade.a6_managed_listing_bindings (merchant_id, control_state, updated_at);
CREATE INDEX IF NOT EXISTS a6_managed_listings_last_action_idx
    ON a6_trade.a6_managed_listing_bindings (last_action_id)
    WHERE last_action_id IS NOT NULL;
