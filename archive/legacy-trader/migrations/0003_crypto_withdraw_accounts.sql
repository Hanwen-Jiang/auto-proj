CREATE TABLE IF NOT EXISTS a6_trade.a6_withdraw_account_bindings (
    id bigserial PRIMARY KEY,
    merchant_id bigint NOT NULL UNIQUE REFERENCES a6_trade.a6_merchants(id),
    network varchar(32) NOT NULL,
    address_sha256 char(64) NOT NULL UNIQUE,
    address_masked varchar(96) NOT NULL,
    address_secret_ref text NOT NULL,
    stable_remark varchar(128) NOT NULL UNIQUE,
    a6_withdraw_account_id bigint,
    source varchar(16) NOT NULL,
    state varchar(24) NOT NULL DEFAULT 'reserved',
    last_error_class text,
    reserved_at timestamptz NOT NULL DEFAULT now(),
    reconciled_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT a6_withdraw_address_sha256_format CHECK (
        address_sha256 ~ '^[0-9a-f]{64}$'
    ),
    CONSTRAINT a6_withdraw_source_known CHECK (source IN ('pool', 'existing')),
    CONSTRAINT a6_withdraw_binding_state_known CHECK (
        state IN (
            'reserved', 'creating', 'create_unknown',
            'bound', 'existing_unmanaged', 'error'
        )
    ),
    CONSTRAINT a6_withdraw_binding_account_state_consistent CHECK (
        (state IN ('bound', 'existing_unmanaged')) = (a6_withdraw_account_id IS NOT NULL)
    ),
    CONSTRAINT a6_withdraw_binding_source_state_consistent CHECK (
        (source = 'existing' AND state = 'existing_unmanaged')
        OR (source = 'pool' AND state <> 'existing_unmanaged')
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS a6_withdraw_account_bindings_a6_account_uidx
    ON a6_trade.a6_withdraw_account_bindings (a6_withdraw_account_id)
    WHERE a6_withdraw_account_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS a6_withdraw_account_bindings_state_idx
    ON a6_trade.a6_withdraw_account_bindings (state, updated_at);
