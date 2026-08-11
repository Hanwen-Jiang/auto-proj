-- Recovery probes must remain reachable after conservative callback growth budgets
-- are exhausted. Keep their accounting and cap separate from normal callback bursts.

ALTER TABLE a6_trade.a6_channel_growth_policies
    ADD COLUMN IF NOT EXISTS daily_readiness_budget_micros bigint;

UPDATE a6_trade.a6_channel_growth_policies
SET daily_readiness_budget_micros = 120000
WHERE daily_readiness_budget_micros IS NULL;

ALTER TABLE a6_trade.a6_channel_growth_policies
    ALTER COLUMN daily_readiness_budget_micros SET DEFAULT 120000,
    ALTER COLUMN daily_readiness_budget_micros SET NOT NULL;

ALTER TABLE a6_trade.a6_channel_growth_policies
    DROP CONSTRAINT IF EXISTS a6_channel_growth_readiness_budget_valid;
ALTER TABLE a6_trade.a6_channel_growth_policies
    ADD CONSTRAINT a6_channel_growth_readiness_budget_valid CHECK (
        daily_readiness_budget_micros >= 0
    );

ALTER TABLE a6_trade.a6_channel_booster_tokens
    ADD COLUMN IF NOT EXISTS daily_readiness_budget_micros bigint;

UPDATE a6_trade.a6_channel_booster_tokens
SET daily_readiness_budget_micros = 2000000
WHERE daily_readiness_budget_micros IS NULL;

ALTER TABLE a6_trade.a6_channel_booster_tokens
    ALTER COLUMN daily_readiness_budget_micros SET DEFAULT 2000000,
    ALTER COLUMN daily_readiness_budget_micros SET NOT NULL;

ALTER TABLE a6_trade.a6_channel_booster_tokens
    DROP CONSTRAINT IF EXISTS a6_channel_booster_readiness_budget_valid;
ALTER TABLE a6_trade.a6_channel_booster_tokens
    ADD CONSTRAINT a6_channel_booster_readiness_budget_valid CHECK (
        daily_readiness_budget_micros >= 0
    );
