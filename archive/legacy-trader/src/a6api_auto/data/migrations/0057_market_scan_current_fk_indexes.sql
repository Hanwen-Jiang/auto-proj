CREATE INDEX IF NOT EXISTS a6_market_signals_current_scan_run_idx
    ON a6_trade.a6_market_channel_signals_current (scan_run_id);

CREATE INDEX IF NOT EXISTS a6_channel_observer_health_current_scan_run_idx
    ON a6_trade.a6_channel_observer_health_current (scan_run_id);
