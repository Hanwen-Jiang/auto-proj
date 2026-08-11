ALTER TABLE a6_trade.a6_merchants
    ADD COLUMN IF NOT EXISTS supplier_name text NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS supplier_description text NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS supplier_profile_observed_at timestamptz;

ALTER TABLE a6_trade.a6_merchants
    DROP CONSTRAINT IF EXISTS a6_merchants_supplier_name_length;
ALTER TABLE a6_trade.a6_merchants
    ADD CONSTRAINT a6_merchants_supplier_name_length
    CHECK (char_length(supplier_name) <= 256);

ALTER TABLE a6_trade.a6_merchants
    DROP CONSTRAINT IF EXISTS a6_merchants_supplier_description_length;
ALTER TABLE a6_trade.a6_merchants
    ADD CONSTRAINT a6_merchants_supplier_description_length
    CHECK (char_length(supplier_description) <= 2000);
