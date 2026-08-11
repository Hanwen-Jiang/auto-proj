CREATE TABLE IF NOT EXISTS a6_trade.a6_market_observer_scan_backoff (
    model text PRIMARY KEY,
    consecutive_failed_rounds integer NOT NULL DEFAULT 0,
    retry_after timestamptz,
    last_error_class text,
    last_failure_at timestamptz,
    last_success_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT a6_market_observer_backoff_failures_nonnegative CHECK (
        consecutive_failed_rounds >= 0
    )
);

CREATE INDEX IF NOT EXISTS a6_market_observer_backoff_retry_idx
    ON a6_trade.a6_market_observer_scan_backoff (retry_after)
    WHERE retry_after IS NOT NULL;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'a6trader') THEN
        GRANT SELECT, INSERT, UPDATE, DELETE
            ON a6_trade.a6_market_observer_scan_backoff TO a6trader;
    END IF;
END
$$;
