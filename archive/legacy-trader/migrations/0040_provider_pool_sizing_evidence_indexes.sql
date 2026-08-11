-- Bound evidence-based Provider sizing reads as the durable ledgers grow.
-- Migrations run transactionally, so these indexes deliberately avoid CONCURRENTLY.

CREATE INDEX IF NOT EXISTS provider_request_envelopes_model_created_idx
    ON a6_trade.provider_request_envelopes (model, created_at DESC)
    INCLUDE (id, request_id, completed_at);

CREATE INDEX IF NOT EXISTS a6_financial_snapshots_merchant_observed_idx
    ON a6_trade.a6_financial_snapshots (merchant_id, observed_at DESC)
    INCLUDE (used_quota);
