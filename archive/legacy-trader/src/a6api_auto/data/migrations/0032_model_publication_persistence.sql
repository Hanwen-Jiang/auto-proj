-- Extend the compact publication projection with timestamps and a monotonic
-- version.  Desired/observed state remains in model_publications; the extra
-- columns make remote listing/probe confirmation auditable without duplicating
-- the market facts.

ALTER TABLE a6_trade.model_publications
    ADD COLUMN IF NOT EXISTS desired_at timestamptz NOT NULL DEFAULT now(),
    ADD COLUMN IF NOT EXISTS action_started_at timestamptz,
    ADD COLUMN IF NOT EXISTS confirmed_at timestamptz,
    ADD COLUMN IF NOT EXISTS last_real_probe_at timestamptz,
    ADD COLUMN IF NOT EXISTS last_real_probe_request_id text,
    ADD COLUMN IF NOT EXISTS state_version bigint NOT NULL DEFAULT 1;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'model_publications_state_version_positive'
          AND conrelid = 'a6_trade.model_publications'::regclass
    ) THEN
        ALTER TABLE a6_trade.model_publications
            ADD CONSTRAINT model_publications_state_version_positive
            CHECK (state_version > 0);
    END IF;
END
$$;

CREATE INDEX IF NOT EXISTS model_publications_observed_at_idx
    ON a6_trade.model_publications (observed_at DESC);

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'a6trader') THEN
        GRANT SELECT, INSERT, UPDATE ON a6_trade.model_publications TO a6trader;
    END IF;
END
$$;
