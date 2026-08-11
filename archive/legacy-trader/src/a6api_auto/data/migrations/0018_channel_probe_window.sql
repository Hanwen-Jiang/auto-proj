ALTER TABLE a6_trade.a6_market_channel_signals_current
    ADD COLUMN IF NOT EXISTS circuit_next_probe_at timestamptz,
    ADD COLUMN IF NOT EXISTS cooldown_next_probe_at timestamptz;

CREATE INDEX IF NOT EXISTS a6_market_signals_probe_window_idx
    ON a6_trade.a6_market_channel_signals_current (
        circuit_next_probe_at, channel_id
    )
    WHERE is_owned AND circuit_next_probe_at IS NOT NULL;
