CREATE INDEX IF NOT EXISTS a6_market_scan_runs_retention_idx
    ON a6_trade.a6_market_scan_runs (started_at, id);

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'a6trader') THEN
        GRANT DELETE ON a6_trade.a6_market_scan_runs TO a6trader;
    END IF;
END
$$;
