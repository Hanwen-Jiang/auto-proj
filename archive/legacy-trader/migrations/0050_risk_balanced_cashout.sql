-- Risk-balanced treasury cashout state.  This migration is deliberately data-plane
-- inert: it records plans and durable money claims but enables no remote submission.

CREATE TABLE IF NOT EXISTS a6_trade.treasury_wallet_principals (
    id bigserial PRIMARY KEY,
    inventory_principal_key text NOT NULL UNIQUE,
    principal_type text NOT NULL,
    address_sha256 char(64) NOT NULL,
    address_masked varchar(96) NOT NULL,
    address_secret_ref text NOT NULL,
    enabled boolean NOT NULL DEFAULT false,
    principal_state text NOT NULL DEFAULT 'staged',
    state_version bigint NOT NULL DEFAULT 1,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT treasury_wallet_principal_type_known CHECK (
        principal_type IN ('evm', 'tron')
    ),
    CONSTRAINT treasury_wallet_principal_hash_valid CHECK (
        address_sha256 ~ '^[0-9a-f]{64}$'
    ),
    CONSTRAINT treasury_wallet_principal_state_known CHECK (
        principal_state IN ('staged', 'enabled', 'disabled', 'retired')
    ),
    CONSTRAINT treasury_wallet_principal_enabled_consistent CHECK (
        enabled = (principal_state = 'enabled')
    ),
    UNIQUE (principal_type, address_sha256)
);

CREATE TABLE IF NOT EXISTS a6_trade.treasury_wallet_routes (
    id bigserial PRIMARY KEY,
    wallet_principal_id bigint NOT NULL
        REFERENCES a6_trade.treasury_wallet_principals(id),
    inventory_route_key text NOT NULL,
    network varchar(32) NOT NULL,
    protocol varchar(32) NOT NULL,
    token_contract_sha256 char(64) NOT NULL,
    token_contract_display varchar(96) NOT NULL,
    a6_withdraw_account_id bigint,
    legacy_binding_id bigint
        REFERENCES a6_trade.a6_withdraw_account_bindings(id),
    route_state text NOT NULL DEFAULT 'staged',
    confirmation_probability numeric(12, 10) NOT NULL DEFAULT 1,
    latency_penalty_micros bigint NOT NULL DEFAULT 0,
    reconciliation_risk_micros bigint NOT NULL DEFAULT 0,
    last_preview_at timestamptz,
    last_confirmed_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT treasury_wallet_route_hash_valid CHECK (
        token_contract_sha256 ~ '^[0-9a-f]{64}$'
    ),
    CONSTRAINT treasury_wallet_route_state_known CHECK (
        route_state IN ('staged', 'healthy', 'degraded', 'disabled', 'retired')
    ),
    CONSTRAINT treasury_wallet_route_values_valid CHECK (
        confirmation_probability >= 0
        AND confirmation_probability <= 1
        AND latency_penalty_micros >= 0
        AND reconciliation_risk_micros >= 0
    ),
    UNIQUE (wallet_principal_id, network, token_contract_sha256),
    UNIQUE (wallet_principal_id, inventory_route_key)
);

CREATE UNIQUE INDEX IF NOT EXISTS treasury_wallet_route_legacy_binding_uidx
    ON a6_trade.treasury_wallet_routes (legacy_binding_id)
    WHERE legacy_binding_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS a6_trade.merchant_treasury_assignments (
    id bigserial PRIMARY KEY,
    merchant_id bigint NOT NULL REFERENCES a6_trade.a6_merchants(id),
    wallet_principal_id bigint NOT NULL
        REFERENCES a6_trade.treasury_wallet_principals(id),
    assignment_state text NOT NULL DEFAULT 'active',
    config_version text NOT NULL,
    assignment_score char(64) NOT NULL,
    assigned_at timestamptz NOT NULL DEFAULT now(),
    released_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT merchant_treasury_assignment_state_known CHECK (
        assignment_state IN ('active', 'released', 'retired')
    ),
    CONSTRAINT merchant_treasury_assignment_score_valid CHECK (
        assignment_score ~ '^[0-9a-f]{64}$'
    ),
    CONSTRAINT merchant_treasury_assignment_release_consistent CHECK (
        (assignment_state = 'active' AND released_at IS NULL)
        OR (assignment_state <> 'active' AND released_at IS NOT NULL)
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS merchant_treasury_one_active_idx
    ON a6_trade.merchant_treasury_assignments (merchant_id)
    WHERE assignment_state = 'active';

CREATE INDEX IF NOT EXISTS merchant_treasury_principal_active_idx
    ON a6_trade.merchant_treasury_assignments (
        wallet_principal_id, assigned_at, merchant_id
    ) WHERE assignment_state = 'active';

-- A withdrawal account belongs to one A6 merchant even when several merchants share
-- the same destination principal. Keep that identity below the principal route instead
-- of treating treasury_wallet_routes.a6_withdraw_account_id as global.
CREATE TABLE IF NOT EXISTS a6_trade.merchant_treasury_route_bindings (
    id bigserial PRIMARY KEY,
    merchant_id bigint NOT NULL REFERENCES a6_trade.a6_merchants(id),
    wallet_route_id bigint NOT NULL REFERENCES a6_trade.treasury_wallet_routes(id),
    a6_withdraw_account_id bigint,
    binding_state text NOT NULL DEFAULT 'staged',
    remote_account_source_hash char(64),
    last_readback_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT merchant_treasury_route_binding_state_known CHECK (
        binding_state IN ('staged', 'healthy', 'degraded', 'disabled', 'retired')
    ),
    CONSTRAINT merchant_treasury_route_binding_remote_valid CHECK (
        (a6_withdraw_account_id IS NULL AND binding_state = 'staged')
        OR (a6_withdraw_account_id IS NOT NULL AND a6_withdraw_account_id > 0)
    ),
    CONSTRAINT merchant_treasury_route_binding_hash_valid CHECK (
        remote_account_source_hash IS NULL
        OR remote_account_source_hash ~ '^[0-9a-f]{64}$'
    ),
    UNIQUE (merchant_id, wallet_route_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS merchant_treasury_remote_account_uidx
    ON a6_trade.merchant_treasury_route_bindings (
        merchant_id, a6_withdraw_account_id
    ) WHERE a6_withdraw_account_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS merchant_treasury_route_healthy_idx
    ON a6_trade.merchant_treasury_route_bindings (
        merchant_id, wallet_route_id, last_readback_at DESC
    ) WHERE binding_state = 'healthy';

CREATE TABLE IF NOT EXISTS a6_trade.a6_cashout_decision_cycles (
    id bigserial PRIMARY KEY,
    merchant_id bigint NOT NULL REFERENCES a6_trade.a6_merchants(id),
    decision_epoch bigint NOT NULL,
    config_version text NOT NULL,
    risk_tier text NOT NULL,
    hard_risk boolean NOT NULL DEFAULT false,
    standard_target_micros bigint NOT NULL,
    target_jitter_micros bigint NOT NULL,
    balance_source_hash char(64) NOT NULL,
    preview_contract_hash char(64) NOT NULL,
    cycle_state text NOT NULL DEFAULT 'active',
    opened_at timestamptz NOT NULL DEFAULT now(),
    closed_at timestamptz,
    close_reason text,
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT a6_cashout_cycle_risk_tier_known CHECK (
        risk_tier IN ('high', 'medium', 'low')
    ),
    CONSTRAINT a6_cashout_cycle_target_valid CHECK (
        standard_target_micros >= 50000000
        AND abs(target_jitter_micros) <= 2500000
    ),
    CONSTRAINT a6_cashout_cycle_hashes_valid CHECK (
        balance_source_hash ~ '^[0-9a-f]{64}$'
        AND preview_contract_hash ~ '^[0-9a-f]{64}$'
    ),
    CONSTRAINT a6_cashout_cycle_state_known CHECK (
        cycle_state IN ('active', 'closed', 'expired')
    ),
    CONSTRAINT a6_cashout_cycle_close_consistent CHECK (
        (cycle_state = 'active' AND closed_at IS NULL)
        OR (cycle_state <> 'active' AND closed_at IS NOT NULL)
    ),
    UNIQUE (merchant_id, decision_epoch, config_version)
);

CREATE UNIQUE INDEX IF NOT EXISTS a6_cashout_cycle_one_active_idx
    ON a6_trade.a6_cashout_decision_cycles (merchant_id)
    WHERE cycle_state = 'active';

CREATE TABLE IF NOT EXISTS a6_trade.a6_cashout_risk_snapshots (
    id bigserial PRIMARY KEY,
    decision_cycle_id bigint NOT NULL
        REFERENCES a6_trade.a6_cashout_decision_cycles(id),
    merchant_id bigint NOT NULL REFERENCES a6_trade.a6_merchants(id),
    delta_effective numeric(12, 10) NOT NULL,
    merchant_age_hours numeric(20, 6) NOT NULL,
    hours_since_last_confirmed_cashout numeric(20, 6),
    cashout_eligible_micros bigint NOT NULL,
    earn_rate_p25_micros_per_hour bigint NOT NULL,
    session_refresh_failures integer NOT NULL,
    withdraw_preview_failures integer NOT NULL,
    risk_probability numeric(16, 14) NOT NULL,
    risk_horizon_hours numeric(20, 6) NOT NULL,
    risk_model_version text NOT NULL,
    training_window_days integer NOT NULL,
    sample_count bigint NOT NULL,
    evidence jsonb NOT NULL DEFAULT '{}'::jsonb,
    evidence_hash char(64) NOT NULL,
    observed_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT a6_cashout_risk_values_valid CHECK (
        delta_effective >= 0 AND delta_effective <= 0.5
        AND merchant_age_hours >= 0
        AND (
            hours_since_last_confirmed_cashout IS NULL
            OR hours_since_last_confirmed_cashout >= 0
        )
        AND cashout_eligible_micros >= 0
        AND earn_rate_p25_micros_per_hour >= 0
        AND session_refresh_failures >= 0
        AND withdraw_preview_failures >= 0
        AND risk_probability >= 0 AND risk_probability <= 1
        AND risk_horizon_hours >= 0
        AND training_window_days > 0
        AND sample_count >= 0
    ),
    CONSTRAINT a6_cashout_risk_evidence_valid CHECK (
        jsonb_typeof(evidence) = 'object'
        AND evidence_hash ~ '^[0-9a-f]{64}$'
    )
);

CREATE INDEX IF NOT EXISTS a6_cashout_risk_merchant_idx
    ON a6_trade.a6_cashout_risk_snapshots (
        merchant_id, observed_at DESC, id DESC
    );

CREATE TABLE IF NOT EXISTS a6_trade.merchant_cashout_positions (
    merchant_id bigint PRIMARY KEY REFERENCES a6_trade.a6_merchants(id),
    source_funds_accrued_micros bigint NOT NULL DEFAULT 0,
    confirmed_source_debit_micros bigint NOT NULL DEFAULT 0,
    confirmed_crypto_net_micros bigint NOT NULL DEFAULT 0,
    rolling_7d_callback_receiver_credit_micros bigint NOT NULL DEFAULT 0,
    skipped_rounds integer NOT NULL DEFAULT 0,
    oldest_residual_at timestamptz,
    last_confirmed_cashout_at timestamptz,
    last_confirmed_callback_at timestamptz,
    source_hash char(64) NOT NULL,
    observed_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT merchant_cashout_position_values_valid CHECK (
        source_funds_accrued_micros >= 0
        AND confirmed_source_debit_micros >= 0
        AND confirmed_crypto_net_micros >= 0
        AND rolling_7d_callback_receiver_credit_micros >= 0
        AND skipped_rounds >= 0
    ),
    CONSTRAINT merchant_cashout_position_hash_valid CHECK (
        source_hash ~ '^[0-9a-f]{64}$'
    )
);

CREATE TABLE IF NOT EXISTS a6_trade.a6_withdrawal_intents (
    id bigserial PRIMARY KEY,
    decision_cycle_id bigint NOT NULL
        REFERENCES a6_trade.a6_cashout_decision_cycles(id),
    risk_snapshot_id bigint NOT NULL
        REFERENCES a6_trade.a6_cashout_risk_snapshots(id),
    merchant_id bigint NOT NULL REFERENCES a6_trade.a6_merchants(id),
    method text NOT NULL,
    gross_micros bigint NOT NULL,
    reserved_micros bigint NOT NULL,
    fund_reservation_id bigint
        REFERENCES a6_trade.merchant_fund_reservations(id),
    provider_reserve_micros bigint NOT NULL,
    cashout_eligible_micros bigint NOT NULL,
    expected_fee_micros bigint NOT NULL,
    expected_net_micros bigint NOT NULL,
    actual_fee_micros bigint,
    actual_net_micros bigint,
    risk_probability numeric(16, 14) NOT NULL,
    risk_horizon_hours numeric(20, 6) NOT NULL,
    risk_model_version text NOT NULL,
    risk_tier text NOT NULL,
    decision_reason text NOT NULL,
    cashout_coverage_before numeric(20, 12) NOT NULL,
    risk_exposure_before_micros bigint NOT NULL,
    wallet_route_id bigint REFERENCES a6_trade.treasury_wallet_routes(id),
    remote_withdraw_account_id bigint,
    idempotency_key char(64) NOT NULL UNIQUE,
    intent_state text NOT NULL DEFAULT 'PLANNED',
    balance_source_hash char(64) NOT NULL,
    config_version text NOT NULL,
    remote_request_id text,
    remote_withdrawal_id text,
    chain_txid text,
    submit_attempted_at timestamptz,
    submitted_at timestamptz,
    confirmation_deadline_at timestamptz,
    confirmed_at timestamptz,
    reconcile_owner text,
    reconcile_lease_until timestamptz,
    last_reconciled_at timestamptz,
    authoritative_no_effect_at timestamptz,
    failure_class text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT a6_withdrawal_intent_method_known CHECK (
        method IN ('crypto', 'callback')
    ),
    CONSTRAINT a6_withdrawal_intent_values_valid CHECK (
        gross_micros > 0
        AND reserved_micros = gross_micros
        AND provider_reserve_micros >= 0
        AND cashout_eligible_micros >= gross_micros
        AND expected_fee_micros >= 0
        AND expected_net_micros >= 0
        AND (actual_fee_micros IS NULL OR actual_fee_micros >= 0)
        AND (actual_net_micros IS NULL OR actual_net_micros >= 0)
        AND risk_probability >= 0 AND risk_probability <= 1
        AND risk_horizon_hours >= 0
        AND cashout_coverage_before >= 0
        AND risk_exposure_before_micros >= 0
    ),
    CONSTRAINT a6_withdrawal_intent_route_consistent CHECK (
        (method = 'crypto' AND wallet_route_id IS NOT NULL)
        OR (method = 'callback' AND wallet_route_id IS NULL)
    ),
    CONSTRAINT a6_withdrawal_intent_risk_tier_known CHECK (
        risk_tier IN ('high', 'medium', 'low')
    ),
    CONSTRAINT a6_withdrawal_intent_state_known CHECK (
        intent_state IN (
            'PLANNED', 'AWAITING_PROVIDER_DRAIN', 'PREVIEWED',
            'SUBMITTED', 'PENDING', 'UNKNOWN', 'RECONCILING',
            'CONFIRMED', 'FAILED_FINAL', 'EXPIRED'
        )
    ),
    CONSTRAINT a6_withdrawal_intent_hash_valid CHECK (
        idempotency_key ~ '^[0-9a-f]{64}$'
        AND balance_source_hash ~ '^[0-9a-f]{64}$'
    ),
    CONSTRAINT a6_withdrawal_intent_terminal_consistent CHECK (
        (intent_state = 'CONFIRMED' AND confirmed_at IS NOT NULL)
        OR (intent_state <> 'CONFIRMED')
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS a6_withdrawal_intent_one_nonterminal_idx
    ON a6_trade.a6_withdrawal_intents (merchant_id)
    WHERE intent_state IN (
        'PLANNED', 'AWAITING_PROVIDER_DRAIN', 'PREVIEWED',
        'SUBMITTED', 'PENDING', 'UNKNOWN', 'RECONCILING'
    );

CREATE UNIQUE INDEX IF NOT EXISTS a6_withdrawal_intent_reservation_uidx
    ON a6_trade.a6_withdrawal_intents (fund_reservation_id)
    WHERE fund_reservation_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS a6_withdrawal_intent_reconcile_idx
    ON a6_trade.a6_withdrawal_intents (
        intent_state, reconcile_lease_until, updated_at, id
    ) WHERE intent_state IN ('SUBMITTED', 'PENDING', 'UNKNOWN', 'RECONCILING');

CREATE TABLE IF NOT EXISTS a6_trade.a6_withdrawal_confirmations (
    id bigserial PRIMARY KEY,
    withdrawal_intent_id bigint NOT NULL
        REFERENCES a6_trade.a6_withdrawal_intents(id),
    confirmation_type text NOT NULL,
    remote_withdrawal_id text,
    chain_txid text,
    observed_gross_micros bigint,
    observed_fee_micros bigint,
    observed_net_micros bigint,
    remote_status text,
    source_hash char(64) NOT NULL,
    evidence jsonb NOT NULL DEFAULT '{}'::jsonb,
    observed_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT a6_withdrawal_confirmation_type_known CHECK (
        confirmation_type IN (
            'preview', 'accepted', 'remote_status', 'balance_effect',
            'chain', 'callback_sender_debit', 'callback_receiver_credit',
            'callback_closed', 'authoritative_no_effect'
        )
    ),
    CONSTRAINT a6_withdrawal_confirmation_values_valid CHECK (
        (observed_gross_micros IS NULL OR observed_gross_micros >= 0)
        AND (observed_fee_micros IS NULL OR observed_fee_micros >= 0)
        AND (observed_net_micros IS NULL OR observed_net_micros >= 0)
    ),
    CONSTRAINT a6_withdrawal_confirmation_evidence_valid CHECK (
        source_hash ~ '^[0-9a-f]{64}$'
        AND jsonb_typeof(evidence) = 'object'
    ),
    UNIQUE (withdrawal_intent_id, confirmation_type, source_hash)
);

CREATE TABLE IF NOT EXISTS a6_trade.a6_callback_sweep_intents (
    withdrawal_intent_id bigint PRIMARY KEY
        REFERENCES a6_trade.a6_withdrawal_intents(id),
    sg_cloud_sender_ref text NOT NULL,
    receiver_merchant_id bigint NOT NULL REFERENCES a6_trade.a6_merchants(id),
    receiver_model text NOT NULL,
    receiver_listing_id bigint NOT NULL,
    callback_request_id text UNIQUE,
    max_spend_micros bigint NOT NULL,
    sender_debit_micros bigint,
    callback_fee_micros bigint,
    receiver_credit_micros bigint,
    accounting_tolerance_micros bigint NOT NULL DEFAULT 1,
    accounting_state text NOT NULL DEFAULT 'planned',
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT a6_callback_sweep_values_valid CHECK (
        max_spend_micros > 0
        AND (sender_debit_micros IS NULL OR sender_debit_micros >= 0)
        AND (callback_fee_micros IS NULL OR callback_fee_micros >= 0)
        AND (receiver_credit_micros IS NULL OR receiver_credit_micros >= 0)
        AND accounting_tolerance_micros >= 0
    ),
    CONSTRAINT a6_callback_sweep_state_known CHECK (
        accounting_state IN (
            'planned', 'submitted', 'partial', 'unknown', 'confirmed', 'failed_final'
        )
    ),
    CONSTRAINT a6_callback_sweep_closure_valid CHECK (
        accounting_state <> 'confirmed'
        OR (
            sender_debit_micros IS NOT NULL
            AND callback_fee_micros IS NOT NULL
            AND receiver_credit_micros IS NOT NULL
            AND abs(
                sender_debit_micros
                - callback_fee_micros
                - receiver_credit_micros
            ) <= accounting_tolerance_micros
        )
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS a6_callback_receiver_nonterminal_idx
    ON a6_trade.a6_callback_sweep_intents (receiver_merchant_id)
    WHERE accounting_state IN ('planned', 'submitted', 'partial', 'unknown');

CREATE TABLE IF NOT EXISTS a6_trade.a6_cashout_ledger_entries (
    id bigserial PRIMARY KEY,
    withdrawal_intent_id bigint NOT NULL
        REFERENCES a6_trade.a6_withdrawal_intents(id),
    merchant_id bigint NOT NULL REFERENCES a6_trade.a6_merchants(id),
    leg_type text NOT NULL,
    amount_micros bigint NOT NULL,
    source_hash char(64) NOT NULL,
    occurred_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT a6_cashout_ledger_leg_known CHECK (
        leg_type IN (
            'crypto_withdraw_gross', 'crypto_withdraw_fee',
            'crypto_withdraw_net', 'callback_sweep_gross',
            'callback_sweep_fee', 'callback_receiver_credit'
        )
    ),
    CONSTRAINT a6_cashout_ledger_values_valid CHECK (
        amount_micros >= 0 AND source_hash ~ '^[0-9a-f]{64}$'
    ),
    UNIQUE (withdrawal_intent_id, leg_type, source_hash)
);

CREATE TABLE IF NOT EXISTS a6_trade.treasury_legacy_binding_migrations (
    legacy_binding_id bigint PRIMARY KEY
        REFERENCES a6_trade.a6_withdraw_account_bindings(id),
    merchant_id bigint NOT NULL REFERENCES a6_trade.a6_merchants(id),
    wallet_principal_id bigint NOT NULL
        REFERENCES a6_trade.treasury_wallet_principals(id),
    wallet_route_id bigint NOT NULL REFERENCES a6_trade.treasury_wallet_routes(id),
    assignment_id bigint NOT NULL
        REFERENCES a6_trade.merchant_treasury_assignments(id),
    source_hash char(64) NOT NULL,
    migrated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT treasury_legacy_binding_hash_valid CHECK (
        source_hash ~ '^[0-9a-f]{64}$'
    )
);

-- Preserve old bindings as staged principals/routes.  Enabling them remains a separate
-- read-back operation, so applying this migration cannot submit a withdrawal.
WITH legacy AS (
    SELECT binding.*,
           CASE
               WHEN upper(binding.network) IN ('TRX', 'TRC20') THEN 'tron'
               ELSE 'evm'
           END AS principal_type
    FROM a6_trade.a6_withdraw_account_bindings binding
), inserted AS (
    INSERT INTO a6_trade.treasury_wallet_principals (
        inventory_principal_key, principal_type, address_sha256,
        address_masked, address_secret_ref, enabled, principal_state
    )
    SELECT 'legacy-binding-' || legacy.id::text, legacy.principal_type,
           legacy.address_sha256, legacy.address_masked,
           legacy.address_secret_ref, false, 'staged'
    FROM legacy
    ON CONFLICT (principal_type, address_sha256) DO NOTHING
    RETURNING id
)
SELECT count(*) FROM inserted;

WITH legacy_route AS (
    SELECT binding.*,
           principal.id AS wallet_principal_id,
           CASE
               WHEN upper(binding.network) IN ('BSC', 'BEP20') THEN 'BSC'
               WHEN upper(binding.network) IN ('ETH', 'ERC20') THEN 'ETH'
               WHEN upper(binding.network) IN ('POL', 'POLYGON') THEN 'POL'
               WHEN upper(binding.network) IN ('TRX', 'TRC20') THEN 'TRX'
               ELSE upper(binding.network)
           END AS normalized_network,
           CASE
               WHEN upper(binding.network) IN ('BSC', 'BEP20') THEN 'BEP20'
               WHEN upper(binding.network) IN ('ETH', 'ERC20') THEN 'ERC20'
               WHEN upper(binding.network) IN ('POL', 'POLYGON') THEN 'Polygon'
               WHEN upper(binding.network) IN ('TRX', 'TRC20') THEN 'TRC20'
               ELSE upper(binding.network)
           END AS protocol,
           CASE
               WHEN upper(binding.network) IN ('BSC', 'BEP20')
                   THEN '8b9c9e0160f6b631330153427ee1609fd59d5db3ee924e6c1bfa940b467516f4'
               WHEN upper(binding.network) IN ('ETH', 'ERC20')
                   THEN '63cbd95b98536412d438ab18ed85c35e326b10e413d89033ba7143f1744abecf'
               WHEN upper(binding.network) IN ('POL', 'POLYGON')
                   THEN '58899898d7e52be2cf681d9208c874c281d00d6d9fca33c6dc7640657269a081'
               WHEN upper(binding.network) IN ('TRX', 'TRC20')
                   THEN 'fb2e4998929bc2df4bf6a158d887c5040987f3d71e661928c40f80dce3306ede'
               ELSE md5(upper(binding.network)) || md5('USDT:' || upper(binding.network))
           END AS contract_hash,
           CASE
               WHEN upper(binding.network) IN ('BSC', 'BEP20')
                   THEN '0x55d398326f99059ff775485246999027b3197955'
               WHEN upper(binding.network) IN ('ETH', 'ERC20')
                   THEN '0xdac17f958d2ee523a2206206994597c13d831ec7'
               WHEN upper(binding.network) IN ('POL', 'POLYGON')
                   THEN '0xc2132d05d31c914a87c6611c10748aeb04b58e8f'
               WHEN upper(binding.network) IN ('TRX', 'TRC20')
                   THEN 'TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t'
               ELSE 'legacy-usdt-' || lower(binding.network)
           END AS contract_display
    FROM a6_trade.a6_withdraw_account_bindings binding
    JOIN a6_trade.treasury_wallet_principals principal
      ON principal.address_sha256 = binding.address_sha256
), inserted AS (
    INSERT INTO a6_trade.treasury_wallet_routes (
        wallet_principal_id, inventory_route_key, network, protocol,
        token_contract_sha256, token_contract_display,
        a6_withdraw_account_id, legacy_binding_id, route_state
    )
    SELECT wallet_principal_id, 'legacy-binding-' || id::text,
           normalized_network, protocol, contract_hash, contract_display,
           a6_withdraw_account_id, id, 'staged'
    FROM legacy_route
    ON CONFLICT (wallet_principal_id, network, token_contract_sha256) DO NOTHING
    RETURNING id
)
SELECT count(*) FROM inserted;

WITH legacy_assignment AS (
    SELECT binding.id AS legacy_binding_id, binding.merchant_id,
           principal.id AS wallet_principal_id,
           binding.address_sha256 AS assignment_score
    FROM a6_trade.a6_withdraw_account_bindings binding
    JOIN a6_trade.treasury_wallet_principals principal
      ON principal.address_sha256 = binding.address_sha256
), inserted AS (
    INSERT INTO a6_trade.merchant_treasury_assignments (
        merchant_id, wallet_principal_id, assignment_state,
        config_version, assignment_score
    )
    SELECT merchant_id, wallet_principal_id, 'active',
           'legacy-withdraw-binding-v1', assignment_score
    FROM legacy_assignment
    ON CONFLICT (merchant_id) WHERE assignment_state = 'active' DO NOTHING
    RETURNING id
)
SELECT count(*) FROM inserted;

INSERT INTO a6_trade.merchant_treasury_route_bindings (
    merchant_id, wallet_route_id, a6_withdraw_account_id,
    binding_state, remote_account_source_hash, last_readback_at
)
SELECT binding.merchant_id, route.id, binding.a6_withdraw_account_id,
       CASE
           WHEN binding.a6_withdraw_account_id IS NULL THEN 'staged'
           ELSE 'healthy'
       END,
       md5(binding.id::text || ':' || binding.address_sha256)
       || md5(binding.network || ':' || binding.merchant_id::text),
       CASE
           WHEN binding.a6_withdraw_account_id IS NULL THEN NULL
           ELSE coalesce(binding.reconciled_at, binding.updated_at)
       END
FROM a6_trade.a6_withdraw_account_bindings binding
JOIN a6_trade.treasury_wallet_routes route
  ON route.legacy_binding_id = binding.id
ON CONFLICT (merchant_id, wallet_route_id) DO NOTHING;

INSERT INTO a6_trade.treasury_legacy_binding_migrations (
    legacy_binding_id, merchant_id, wallet_principal_id,
    wallet_route_id, assignment_id, source_hash
)
SELECT binding.id, binding.merchant_id, principal.id, route.id, assignment.id,
       md5(binding.id::text || ':' || binding.address_sha256)
       || md5(binding.network || ':' || binding.merchant_id::text)
FROM a6_trade.a6_withdraw_account_bindings binding
JOIN a6_trade.treasury_wallet_principals principal
  ON principal.address_sha256 = binding.address_sha256
JOIN a6_trade.treasury_wallet_routes route
  ON route.legacy_binding_id = binding.id
JOIN a6_trade.merchant_treasury_assignments assignment
  ON assignment.merchant_id = binding.merchant_id
 AND assignment.wallet_principal_id = principal.id
 AND assignment.assignment_state = 'active'
ON CONFLICT (legacy_binding_id) DO NOTHING;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'a6trader') THEN
        GRANT SELECT, INSERT, UPDATE
            ON a6_trade.treasury_wallet_principals,
               a6_trade.treasury_wallet_routes,
               a6_trade.merchant_treasury_assignments,
               a6_trade.merchant_treasury_route_bindings,
               a6_trade.a6_cashout_decision_cycles,
               a6_trade.a6_cashout_risk_snapshots,
               a6_trade.merchant_cashout_positions,
               a6_trade.a6_withdrawal_intents,
               a6_trade.a6_withdrawal_confirmations,
               a6_trade.a6_callback_sweep_intents,
               a6_trade.a6_cashout_ledger_entries,
               a6_trade.treasury_legacy_binding_migrations TO a6trader;
        GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA a6_trade TO a6trader;
    END IF;
END
$$;
