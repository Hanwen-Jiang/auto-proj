ALTER TABLE a6_trade.a6_merchants
    ADD COLUMN IF NOT EXISTS quota_per_unit bigint NOT NULL DEFAULT 500000;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_catalog.pg_constraint
        WHERE conrelid = 'a6_trade.a6_merchants'::regclass
          AND conname = 'a6_merchants_quota_per_unit_positive'
    ) THEN
        ALTER TABLE a6_trade.a6_merchants
            ADD CONSTRAINT a6_merchants_quota_per_unit_positive
            CHECK (quota_per_unit > 0);
    END IF;
END
$$;
