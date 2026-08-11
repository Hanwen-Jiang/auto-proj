-- Durable membership wakeups and duty-scoped liquidity tokens.

CREATE OR REPLACE FUNCTION a6_trade.notify_provider_membership_repair()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.status IN ('pending', 'retry') THEN
        PERFORM pg_notify(
            'a6_provider_membership_repair',
            json_build_object(
                'request_id', NEW.id,
                'model', NEW.model,
                'slot_no', NEW.slot_no
            )::text
        );
    END IF;
    RETURN NEW;
END
$$;

DROP TRIGGER IF EXISTS provider_membership_repair_notify
    ON a6_trade.provider_membership_repair_requests;
CREATE TRIGGER provider_membership_repair_notify
AFTER INSERT OR UPDATE OF status, next_attempt_at
ON a6_trade.provider_membership_repair_requests
FOR EACH ROW
EXECUTE FUNCTION a6_trade.notify_provider_membership_repair();

CREATE UNIQUE INDEX IF NOT EXISTS a6_tokens_id_merchant_uidx
    ON a6_trade.a6_tokens (id, merchant_id);

ALTER TABLE a6_trade.provider_auto_pool_accounts
    ADD COLUMN IF NOT EXISTS claim_token text,
    ADD COLUMN IF NOT EXISTS claimed_by text,
    ADD COLUMN IF NOT EXISTS claimed_at timestamptz;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'provider_auto_pool_account_claim_shape'
          AND conrelid = 'a6_trade.provider_auto_pool_accounts'::regclass
    ) THEN
        ALTER TABLE a6_trade.provider_auto_pool_accounts
            ADD CONSTRAINT provider_auto_pool_account_claim_shape CHECK (
                (
                    claim_token IS NULL
                    AND claimed_by IS NULL
                    AND claimed_at IS NULL
                ) OR (
                    btrim(claim_token) <> ''
                    AND btrim(claimed_by) <> ''
                    AND claimed_at IS NOT NULL
                )
            );
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'provider_auto_pool_account_token_merchant_fk'
          AND conrelid = 'a6_trade.provider_auto_pool_accounts'::regclass
    ) THEN
        ALTER TABLE a6_trade.provider_auto_pool_accounts
            ADD CONSTRAINT provider_auto_pool_account_token_merchant_fk
            FOREIGN KEY (token_ref_id, merchant_id)
            REFERENCES a6_trade.a6_tokens(id, merchant_id);
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'provider_pool_generation_model_routing_cost_uq'
          AND conrelid = 'a6_trade.provider_pool_generations'::regclass
    ) THEN
        ALTER TABLE a6_trade.provider_pool_generations
            ADD CONSTRAINT provider_pool_generation_model_routing_cost_uq
            UNIQUE (model, generation, cost_generation);
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'provider_auto_pool_generation_parent_fk'
          AND conrelid = 'a6_trade.provider_auto_pool_generations'::regclass
    ) THEN
        ALTER TABLE a6_trade.provider_auto_pool_generations
            ADD CONSTRAINT provider_auto_pool_generation_parent_fk
            FOREIGN KEY (model, routing_generation, cost_generation)
            REFERENCES a6_trade.provider_pool_generations(
                model, generation, cost_generation
            );
    END IF;
END
$$;

CREATE TABLE IF NOT EXISTS a6_trade.a6_token_duties (
    token_ref_id bigint PRIMARY KEY,
    merchant_id bigint NOT NULL REFERENCES a6_trade.a6_merchants(id) ON DELETE CASCADE,
    duty text NOT NULL,
    model text NOT NULL,
    duty_state text NOT NULL DEFAULT 'provisioned',
    created_by_controller boolean NOT NULL DEFAULT true,
    last_reconciled_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (token_ref_id, merchant_id),
    UNIQUE (token_ref_id, model),
    CONSTRAINT a6_token_duty_token_merchant_fk FOREIGN KEY (token_ref_id, merchant_id)
        REFERENCES a6_trade.a6_tokens(id, merchant_id) ON DELETE CASCADE,
    CONSTRAINT a6_token_duty_known CHECK (duty IN ('liquidity')),
    CONSTRAINT a6_token_duty_model_nonempty CHECK (btrim(model) <> ''),
    CONSTRAINT a6_token_duty_state_known CHECK (
        duty_state IN ('provisioned', 'ready', 'blocked', 'retired')
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS a6_token_duty_one_live_scope_idx
    ON a6_trade.a6_token_duties (merchant_id, duty, model)
    WHERE duty_state IN ('provisioned', 'ready', 'blocked');

CREATE TABLE IF NOT EXISTS a6_trade.a6_liquidity_token_routes (
    token_ref_id bigint NOT NULL,
    model text NOT NULL,
    receiver_merchant_id bigint REFERENCES a6_trade.a6_merchants(id),
    receiver_listing_id bigint,
    receiver_channel_id bigint,
    route_state text NOT NULL DEFAULT 'unassigned',
    pin_confirmed boolean NOT NULL DEFAULT false,
    remote_readback jsonb NOT NULL DEFAULT '{}'::jsonb,
    last_error_class text,
    observed_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (token_ref_id, model),
    CONSTRAINT a6_liquidity_route_duty_model_fk FOREIGN KEY (token_ref_id, model)
        REFERENCES a6_trade.a6_token_duties(token_ref_id, model) ON DELETE CASCADE,
    CONSTRAINT a6_liquidity_route_model_nonempty CHECK (btrim(model) <> ''),
    CONSTRAINT a6_liquidity_route_state_known CHECK (
        route_state IN ('unassigned', 'ready', 'blocked')
    ),
    CONSTRAINT a6_liquidity_route_target_consistent CHECK (
        (
            route_state = 'unassigned'
            AND receiver_merchant_id IS NULL
            AND receiver_listing_id IS NULL
            AND receiver_channel_id IS NULL
            AND NOT pin_confirmed
        ) OR (
            route_state IN ('ready', 'blocked')
            AND receiver_merchant_id > 0
            AND receiver_listing_id > 0
            AND receiver_channel_id > 0
            AND (route_state <> 'ready' OR pin_confirmed)
        )
    ),
    CONSTRAINT a6_liquidity_route_readback_object CHECK (
        jsonb_typeof(remote_readback) = 'object'
    )
);

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'a6trader') THEN
        GRANT SELECT, INSERT, UPDATE
            ON a6_trade.a6_token_duties,
               a6_trade.a6_liquidity_token_routes TO a6trader;
    END IF;
END
$$;
