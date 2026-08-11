-- Keep automatic callback growth as a small recovery aid. Large manual retirement
-- bursts are a separate operator action and must not become the automatic default.

ALTER TABLE a6_trade.a6_channel_growth_policies
    ALTER COLUMN callback_cooldown_seconds SET DEFAULT 3600,
    ALTER COLUMN callback_requests SET DEFAULT 3,
    ALTER COLUMN callback_concurrency SET DEFAULT 1,
    ALTER COLUMN daily_budget_micros SET DEFAULT 10000;

ALTER TABLE a6_trade.a6_channel_booster_tokens
    ALTER COLUMN daily_budget_micros SET DEFAULT 25000,
    ALTER COLUMN maximum_burst_requests SET DEFAULT 3;

UPDATE a6_trade.a6_channel_growth_policies
SET callback_cooldown_seconds = 3600,
    callback_requests = 3,
    callback_concurrency = 1,
    daily_budget_micros = 10000,
    updated_at = now()
WHERE enabled AND callback_enabled;

UPDATE a6_trade.a6_channel_booster_tokens
SET daily_budget_micros = 25000,
    maximum_burst_requests = 3,
    updated_at = now()
WHERE enabled;
