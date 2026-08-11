-- A Provider that cannot satisfy its funding contract leaves duty immediately.
-- Keep the logical slot VACANT and hand replacement planning to a durable queue.

ALTER TABLE a6_trade.provider_fund_reserves
    DROP CONSTRAINT IF EXISTS provider_fund_reserve_basis_known;

ALTER TABLE a6_trade.provider_fund_reserves
    ADD CONSTRAINT provider_fund_reserve_basis_known CHECK (
        reserve_basis IN (
            'configured_minimum',
            'immediate_refill',
            'pending_topup',
            'cashout_drain',
            'provider_retirement'
        )
    );

CREATE TABLE IF NOT EXISTS a6_trade.provider_membership_repair_requests (
    id bigserial PRIMARY KEY,
    model text NOT NULL,
    source_routing_generation bigint NOT NULL,
    source_cost_generation bigint NOT NULL,
    slot_no integer NOT NULL,
    outgoing_merchant_id bigint NOT NULL
        REFERENCES a6_trade.a6_merchants(id),
    outgoing_enodot_account_id bigint NOT NULL,
    outgoing_a6_token_id bigint NOT NULL,
    reason_code text NOT NULL,
    status text NOT NULL DEFAULT 'pending',
    attempt_count integer NOT NULL DEFAULT 0,
    claim_token text,
    claimed_by text,
    claimed_at timestamptz,
    next_attempt_at timestamptz NOT NULL DEFAULT now(),
    target_routing_generation bigint,
    target_cost_generation bigint,
    replacement_merchant_id bigint REFERENCES a6_trade.a6_merchants(id),
    replacement_enodot_account_id bigint,
    replacement_a6_token_id bigint,
    completed_at timestamptz,
    last_error_class text,
    result jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT provider_membership_repair_identity_positive CHECK (
        source_routing_generation > 0 AND source_cost_generation > 0
        AND slot_no > 0 AND outgoing_merchant_id > 0
        AND outgoing_enodot_account_id > 0 AND outgoing_a6_token_id > 0
        AND attempt_count >= 0
    ),
    CONSTRAINT provider_membership_repair_reason_known CHECK (
        reason_code IN ('provider_funding_contract_failed', 'cashout_drain')
    ),
    CONSTRAINT provider_membership_repair_status_known CHECK (
        status IN ('pending', 'running', 'retry', 'completed', 'failed', 'superseded')
    ),
    CONSTRAINT provider_membership_repair_claim_consistent CHECK (
        (
            status = 'running'
            AND claim_token IS NOT NULL
            AND claimed_by IS NOT NULL
            AND claimed_at IS NOT NULL
        ) OR (
            status <> 'running'
            AND claim_token IS NULL
            AND claimed_by IS NULL
            AND claimed_at IS NULL
        )
    ),
    CONSTRAINT provider_membership_repair_target_consistent CHECK (
        (
            target_routing_generation IS NULL
            AND target_cost_generation IS NULL
            AND replacement_merchant_id IS NULL
            AND replacement_enodot_account_id IS NULL
            AND replacement_a6_token_id IS NULL
        ) OR (
            target_routing_generation > 0
            AND target_cost_generation > 0
            AND replacement_merchant_id > 0
            AND replacement_enodot_account_id > 0
            AND replacement_a6_token_id > 0
        )
    ),
    CONSTRAINT provider_membership_repair_completion_consistent CHECK (
        (status IN ('completed', 'failed', 'superseded') AND completed_at IS NOT NULL)
        OR (status NOT IN ('completed', 'failed', 'superseded') AND completed_at IS NULL)
    ),
    CONSTRAINT provider_membership_repair_result_object CHECK (
        jsonb_typeof(result) = 'object'
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS provider_membership_repair_one_open_slot_idx
    ON a6_trade.provider_membership_repair_requests (model, slot_no)
    WHERE status IN ('pending', 'running', 'retry');

CREATE INDEX IF NOT EXISTS provider_membership_repair_claim_idx
    ON a6_trade.provider_membership_repair_requests (next_attempt_at, id)
    WHERE status IN ('pending', 'retry');

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'a6trader') THEN
        GRANT SELECT, INSERT, UPDATE
            ON a6_trade.provider_membership_repair_requests TO a6trader;
        GRANT USAGE, SELECT
            ON SEQUENCE a6_trade.provider_membership_repair_requests_id_seq TO a6trader;
    END IF;
END
$$;
