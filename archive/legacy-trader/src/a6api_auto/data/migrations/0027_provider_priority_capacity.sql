-- Provider pool priorities use priority_offset + rank. Keep priority 1 available
-- for the platform's reserved/default route and allow the documented 12-slot pool.

DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'enodot_binding_priority_range'
          AND conrelid = 'a6_trade.enodot_account_bindings'::regclass
    ) THEN
        ALTER TABLE a6_trade.enodot_account_bindings
            DROP CONSTRAINT enodot_binding_priority_range;
    END IF;
END
$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'enodot_binding_priority_range'
          AND conrelid = 'a6_trade.enodot_account_bindings'::regclass
    ) THEN
        ALTER TABLE a6_trade.enodot_account_bindings
            ADD CONSTRAINT enodot_binding_priority_range CHECK (
                desired_priority BETWEEN 0 AND 32767
            );
    END IF;
END
$$;
