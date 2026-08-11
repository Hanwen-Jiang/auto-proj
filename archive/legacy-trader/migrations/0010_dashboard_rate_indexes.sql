CREATE INDEX IF NOT EXISTS a6_market_quotes_channel_model_time_idx
    ON a6_trade.a6_market_quotes (channel_id, model, observed_at DESC);
