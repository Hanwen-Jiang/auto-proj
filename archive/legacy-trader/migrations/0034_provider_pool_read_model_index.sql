-- Keep the bounded Provider-pool failure read model index-backed as the
-- durable attempt ledger grows.  The expression matches the API ordering.

CREATE INDEX IF NOT EXISTS provider_attempt_failed_model_time_idx
    ON a6_trade.provider_attempts (
        model,
        (coalesce(finished_at, started_at)) DESC,
        id DESC
    )
    WHERE state = 'failed';
