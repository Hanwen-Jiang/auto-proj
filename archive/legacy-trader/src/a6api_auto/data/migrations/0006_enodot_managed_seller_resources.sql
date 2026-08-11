CREATE TABLE IF NOT EXISTS a6_trade.enodot_managed_seller_resources (
    id bigserial PRIMARY KEY,
    merchant_id bigint NOT NULL UNIQUE
        REFERENCES a6_trade.a6_merchants(id) ON DELETE CASCADE,
    managed_uid text NOT NULL UNIQUE,
    enodot_user_id bigint,
    enodot_api_key_id bigint,
    api_key_fingerprint char(64),
    key_secret_ref text,
    group_id bigint NOT NULL,
    desired_delta numeric(8, 6) NOT NULL,
    observed_delta numeric(8, 6),
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
    CONSTRAINT enodot_managed_seller_uid_nonempty CHECK (
        managed_uid <> '' AND length(managed_uid) <= 256
    ),
    CONSTRAINT enodot_managed_seller_external_ids_positive CHECK (
        (enodot_user_id IS NULL OR enodot_user_id > 0)
        AND (enodot_api_key_id IS NULL OR enodot_api_key_id > 0)
        AND group_id > 0
    ),
    CONSTRAINT enodot_managed_seller_delta_range CHECK (
        desired_delta BETWEEN 0 AND 0.5
        AND (observed_delta IS NULL OR observed_delta BETWEEN 0 AND 0.5)
    ),
    CONSTRAINT enodot_managed_seller_fingerprint_shape CHECK (
        api_key_fingerprint IS NULL
        OR api_key_fingerprint ~ '^[0-9a-f]{64}$'
    ),
    CONSTRAINT enodot_managed_seller_idempotency_nonempty CHECK (
        create_idempotency_key <> '' AND length(create_idempotency_key) <= 128
    ),
    CONSTRAINT enodot_managed_seller_state_known CHECK (
        control_state IN (
            'planned', 'creating', 'create_unknown', 'verifying',
            'bound', 'updating', 'update_unknown', 'error', 'retired'
        )
    ),
    CONSTRAINT enodot_managed_seller_desired_object CHECK (
        jsonb_typeof(desired_state) = 'object'
    ),
    CONSTRAINT enodot_managed_seller_observed_object CHECK (
        observed_state IS NULL OR jsonb_typeof(observed_state) = 'object'
    ),
    CONSTRAINT enodot_managed_seller_observation_time_consistent CHECK (
        last_observed_at IS NOT NULL OR observed_state IS NULL
    ),
    CONSTRAINT enodot_managed_seller_bound_complete CHECK (
        control_state <> 'bound'
        OR (
            enodot_user_id IS NOT NULL
            AND enodot_api_key_id IS NOT NULL
            AND api_key_fingerprint IS NOT NULL
            AND key_secret_ref IS NOT NULL
            AND observed_state IS NOT NULL
            AND observed_delta IS NOT NULL
            AND verified_at IS NOT NULL
        )
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS enodot_managed_seller_user_uidx
    ON a6_trade.enodot_managed_seller_resources (enodot_user_id)
    WHERE enodot_user_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS enodot_managed_seller_key_uidx
    ON a6_trade.enodot_managed_seller_resources (enodot_api_key_id)
    WHERE enodot_api_key_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS enodot_managed_seller_create_action_uidx
    ON a6_trade.enodot_managed_seller_resources (create_action_id)
    WHERE create_action_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS enodot_managed_seller_state_idx
    ON a6_trade.enodot_managed_seller_resources (control_state, updated_at);
CREATE INDEX IF NOT EXISTS enodot_managed_seller_last_action_idx
    ON a6_trade.enodot_managed_seller_resources (last_action_id)
    WHERE last_action_id IS NOT NULL;
