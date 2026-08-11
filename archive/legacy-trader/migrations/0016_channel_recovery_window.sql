ALTER TABLE a6_trade.a6_market_channel_signals_current
    ADD COLUMN IF NOT EXISTS circuit_scope text NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS circuit_recovery_mode text NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS circuit_cooling_until timestamptz,
    ADD COLUMN IF NOT EXISTS circuit_last_cooling_at timestamptz,
    ADD COLUMN IF NOT EXISTS circuit_last_probe_at timestamptz,
    ADD COLUMN IF NOT EXISTS circuit_probe_success_streak integer,
    ADD COLUMN IF NOT EXISTS cooldown_expected_recovery_at timestamptz,
    ADD COLUMN IF NOT EXISTS cooldown_sources jsonb NOT NULL DEFAULT '[]'::jsonb;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'a6_market_signal_recovery_values_valid'
          AND conrelid = 'a6_trade.a6_market_channel_signals_current'::regclass
    ) THEN
        ALTER TABLE a6_trade.a6_market_channel_signals_current
            ADD CONSTRAINT a6_market_signal_recovery_values_valid CHECK (
                (
                    circuit_probe_success_streak IS NULL
                    OR circuit_probe_success_streak >= 0
                )
                AND jsonb_typeof(cooldown_sources) = 'array'
            );
    END IF;
END
$$;

CREATE INDEX IF NOT EXISTS a6_market_signals_recovery_window_idx
    ON a6_trade.a6_market_channel_signals_current (
        circuit_cooling_until, channel_id
    )
    WHERE is_owned AND circuit_cooling_until IS NOT NULL;
