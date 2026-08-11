-- Preserve the exact canonical Observer payload used to derive result_hash.
-- Shadow planning must reject completed scans that predate this column or have
-- a payload/hash mismatch instead of treating a locally rebuilt projection as proof.

ALTER TABLE a6_trade.a6_market_scan_runs
    ADD COLUMN IF NOT EXISTS result_payload jsonb;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'a6_market_scan_result_payload_array'
          AND conrelid = 'a6_trade.a6_market_scan_runs'::regclass
    ) THEN
        ALTER TABLE a6_trade.a6_market_scan_runs
            ADD CONSTRAINT a6_market_scan_result_payload_array CHECK (
                result_payload IS NULL OR jsonb_typeof(result_payload) = 'array'
            );
    END IF;
END
$$;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'a6trader') THEN
        GRANT SELECT, INSERT, UPDATE ON a6_trade.a6_market_scan_runs TO a6trader;
    END IF;
END
$$;
