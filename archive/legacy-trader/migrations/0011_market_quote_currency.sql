ALTER TABLE a6_trade.a6_market_quotes
    ADD COLUMN IF NOT EXISTS realtime_ratio_usd numeric(24, 12),
    ADD COLUMN IF NOT EXISTS realtime_ratio_cny numeric(24, 12),
    ADD COLUMN IF NOT EXISTS exchange_rate numeric(24, 12);

UPDATE a6_trade.a6_market_quotes
SET realtime_ratio_usd = final_multiplier
WHERE realtime_ratio_usd IS NULL
  AND final_multiplier IS NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'a6_market_quotes_currency_values_positive'
          AND conrelid = 'a6_trade.a6_market_quotes'::regclass
    ) THEN
        ALTER TABLE a6_trade.a6_market_quotes
            ADD CONSTRAINT a6_market_quotes_currency_values_positive CHECK (
                (realtime_ratio_usd IS NULL OR realtime_ratio_usd > 0)
                AND (realtime_ratio_cny IS NULL OR realtime_ratio_cny > 0)
                AND (exchange_rate IS NULL OR exchange_rate > 0)
            );
    END IF;
END
$$;
