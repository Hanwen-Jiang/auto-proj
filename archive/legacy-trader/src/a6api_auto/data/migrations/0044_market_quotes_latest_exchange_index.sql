CREATE INDEX IF NOT EXISTS a6_market_quotes_latest_exchange_idx
    ON a6_trade.a6_market_quotes (observed_at DESC, id DESC)
    WHERE exchange_rate IS NOT NULL
       OR (
           realtime_ratio_cny IS NOT NULL
           AND coalesce(realtime_ratio_usd, final_multiplier) IS NOT NULL
       );
