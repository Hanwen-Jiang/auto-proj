-- One model-scoped profit-on-cost setting drives Seller pricing and the
-- Priority 1 economic eligibility gate.

CREATE TABLE IF NOT EXISTS a6_trade.model_profit_settings (
    model text PRIMARY KEY,
    minimum_profit_on_cost_bps integer NOT NULL DEFAULT 6000,
    updated_at timestamptz NOT NULL DEFAULT now(),
    updated_by text NOT NULL DEFAULT 'migration-0065',
    CONSTRAINT model_profit_settings_model_valid CHECK (
        model ~ '^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,95}$'
    ),
    CONSTRAINT model_profit_settings_bps_valid CHECK (
        minimum_profit_on_cost_bps BETWEEN 0 AND 100000
    ),
    CONSTRAINT model_profit_settings_actor_valid CHECK (
        btrim(updated_by) <> '' AND length(updated_by) <= 128
    )
);

ALTER TABLE a6_trade.sub2api_economic_generations
    ALTER COLUMN minimum_profit_on_cost_bps SET DEFAULT 6000;

INSERT INTO a6_trade.model_profit_settings (
    model, minimum_profit_on_cost_bps, updated_by
)
VALUES ('gpt-5.6-sol', 6000, 'migration-0065')
ON CONFLICT (model) DO NOTHING;

DROP TRIGGER IF EXISTS model_profit_settings_economic_notify
    ON a6_trade.model_profit_settings;
CREATE TRIGGER model_profit_settings_economic_notify
AFTER INSERT OR UPDATE OR DELETE ON a6_trade.model_profit_settings
FOR EACH ROW EXECUTE FUNCTION a6_trade.notify_economic_model_fact_changed();

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'a6trader') THEN
        GRANT SELECT, INSERT, UPDATE, DELETE ON
            a6_trade.model_profit_settings TO a6trader;
    END IF;
END
$$;
