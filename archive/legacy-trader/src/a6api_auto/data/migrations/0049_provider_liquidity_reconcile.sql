-- Provider liquidity uses the same per-merchant claim boundary as future cashout
-- workers.  A transfer with an unknown remote result keeps its claim active until
-- balance-effect reconciliation proves the outcome; it is never submitted again.

CREATE TABLE IF NOT EXISTS a6_trade.merchant_fund_reservations (
    id bigserial PRIMARY KEY,
    merchant_id bigint NOT NULL REFERENCES a6_trade.a6_merchants(id),
    owner_type text NOT NULL,
    owner_intent_id bigint NOT NULL,
    reserved_micros bigint NOT NULL,
    reservation_state text NOT NULL DEFAULT 'active',
    balance_source_hash char(64) NOT NULL,
    expires_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    released_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT merchant_fund_reservation_owner_known CHECK (
        owner_type IN ('provider_topup', 'crypto_cashout', 'callback_cashout')
    ),
    CONSTRAINT merchant_fund_reservation_amount_positive CHECK (
        reserved_micros > 0
    ),
    CONSTRAINT merchant_fund_reservation_state_known CHECK (
        reservation_state IN ('active', 'released', 'cancelled')
    ),
    CONSTRAINT merchant_fund_reservation_hash_valid CHECK (
        balance_source_hash ~ '^[0-9a-f]{64}$'
    ),
    CONSTRAINT merchant_fund_reservation_release_complete CHECK (
        (reservation_state = 'active' AND released_at IS NULL)
        OR (reservation_state <> 'active' AND released_at IS NOT NULL)
    ),
    UNIQUE (owner_type, owner_intent_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS merchant_fund_reservation_one_active_idx
    ON a6_trade.merchant_fund_reservations (merchant_id)
    WHERE reservation_state = 'active';

CREATE INDEX IF NOT EXISTS merchant_fund_reservation_history_idx
    ON a6_trade.merchant_fund_reservations (merchant_id, created_at DESC, id DESC);

ALTER TABLE a6_trade.a6_balance_transfer_intents
    ADD COLUMN IF NOT EXISTS fund_reservation_id bigint;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'a6_balance_transfer_fund_reservation_fk'
          AND conrelid = 'a6_trade.a6_balance_transfer_intents'::regclass
    ) THEN
        ALTER TABLE a6_trade.a6_balance_transfer_intents
            ADD CONSTRAINT a6_balance_transfer_fund_reservation_fk
            FOREIGN KEY (fund_reservation_id)
            REFERENCES a6_trade.merchant_fund_reservations(id);
    END IF;
END
$$;

CREATE UNIQUE INDEX IF NOT EXISTS a6_balance_transfer_reservation_uidx
    ON a6_trade.a6_balance_transfer_intents (fund_reservation_id)
    WHERE fund_reservation_id IS NOT NULL;

-- This is the long-lived operating reserve subtracted by cashout planning.  It is
-- separate from the unique active action claim above: Provider top-up consumes its
-- own reserve, while cashout must leave it untouched.
CREATE TABLE IF NOT EXISTS a6_trade.provider_fund_reserves (
    model text NOT NULL,
    merchant_id bigint NOT NULL REFERENCES a6_trade.a6_merchants(id),
    reserved_micros bigint NOT NULL,
    reserve_basis text NOT NULL,
    user_balance_micros bigint NOT NULL,
    withdrawable_micros bigint NOT NULL,
    quota_per_unit bigint NOT NULL,
    pending_transfer_intent_id bigint
        REFERENCES a6_trade.a6_balance_transfer_intents(id),
    balance_source_hash char(64) NOT NULL,
    observed_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (model, merchant_id),
    CONSTRAINT provider_fund_reserve_values_valid CHECK (
        reserved_micros >= 0
        AND user_balance_micros >= 0
        AND withdrawable_micros >= 0
        AND quota_per_unit > 0
    ),
    CONSTRAINT provider_fund_reserve_basis_known CHECK (
        reserve_basis IN (
            'configured_minimum', 'immediate_refill', 'pending_topup'
        )
    ),
    CONSTRAINT provider_fund_reserve_hash_valid CHECK (
        balance_source_hash ~ '^[0-9a-f]{64}$'
    )
);

CREATE INDEX IF NOT EXISTS provider_fund_reserve_observed_idx
    ON a6_trade.provider_fund_reserves (observed_at, model, merchant_id);

CREATE TABLE IF NOT EXISTS a6_trade.provider_liquidity_events (
    id bigserial PRIMARY KEY,
    idempotency_key text NOT NULL UNIQUE,
    model text NOT NULL,
    merchant_id bigint NOT NULL REFERENCES a6_trade.a6_merchants(id),
    enodot_account_id bigint,
    a6_token_id bigint,
    slot_no integer,
    action text NOT NULL,
    liquidity_state text NOT NULL,
    funding_eligible boolean NOT NULL,
    hard_blocked boolean NOT NULL,
    replacement_required boolean NOT NULL,
    transfer_intent_id bigint
        REFERENCES a6_trade.a6_balance_transfer_intents(id),
    fund_reservation_id bigint
        REFERENCES a6_trade.merchant_fund_reservations(id),
    transfer_amount_micros bigint NOT NULL DEFAULT 0,
    user_balance_before_micros bigint NOT NULL,
    user_balance_after_micros bigint,
    withdrawable_before_micros bigint NOT NULL,
    provider_reserve_micros bigint NOT NULL,
    error_class text,
    evidence jsonb NOT NULL DEFAULT '{}'::jsonb,
    evidence_hash char(64) NOT NULL,
    observed_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT provider_liquidity_event_state_known CHECK (
        liquidity_state IN (
            'FUNDED', 'REFILLING', 'UNFUNDED', 'TRANSFER_UNKNOWN', 'UNKNOWN'
        )
    ),
    CONSTRAINT provider_liquidity_event_values_valid CHECK (
        (slot_no IS NULL OR slot_no > 0)
        AND transfer_amount_micros >= 0
        AND user_balance_before_micros >= 0
        AND (user_balance_after_micros IS NULL OR user_balance_after_micros >= 0)
        AND withdrawable_before_micros >= 0
        AND provider_reserve_micros >= 0
    ),
    CONSTRAINT provider_liquidity_event_evidence_object CHECK (
        jsonb_typeof(evidence) = 'object'
    ),
    CONSTRAINT provider_liquidity_event_hash_valid CHECK (
        evidence_hash ~ '^[0-9a-f]{64}$'
    )
);

CREATE INDEX IF NOT EXISTS provider_liquidity_event_scope_idx
    ON a6_trade.provider_liquidity_events (
        model, merchant_id, observed_at DESC, id DESC
    );

CREATE INDEX IF NOT EXISTS provider_liquidity_replacement_idx
    ON a6_trade.provider_liquidity_events (
        model, observed_at DESC, id DESC
    )
    WHERE replacement_required;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'a6trader') THEN
        GRANT SELECT, INSERT, UPDATE
            ON a6_trade.merchant_fund_reservations,
               a6_trade.provider_fund_reserves,
               a6_trade.provider_liquidity_events TO a6trader;
        GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA a6_trade TO a6trader;
    END IF;
END
$$;
