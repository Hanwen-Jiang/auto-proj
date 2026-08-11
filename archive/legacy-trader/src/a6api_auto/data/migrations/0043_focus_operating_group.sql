ALTER TABLE a6_trade.a6_merchants
    ADD COLUMN IF NOT EXISTS operating_group text NOT NULL DEFAULT 'standard';

ALTER TABLE a6_trade.a6_merchants
    DROP CONSTRAINT IF EXISTS a6_merchants_operating_group_valid;
ALTER TABLE a6_trade.a6_merchants
    ADD CONSTRAINT a6_merchants_operating_group_valid CHECK (
        char_length(operating_group) BETWEEN 1 AND 64
        AND operating_group !~ '[[:cntrl:]]'
    );

CREATE INDEX IF NOT EXISTS a6_merchants_operating_group_idx
    ON a6_trade.a6_merchants (operating_group, id)
    WHERE operating_group <> 'standard';
