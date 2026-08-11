-- Bind real-probe evidence to the exact managed-listing set reconciled for a model.

ALTER TABLE a6_trade.model_publications
    ADD COLUMN IF NOT EXISTS publication_scope text,
    ADD COLUMN IF NOT EXISTS desired_cost_generation bigint;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'model_publications_scope_nonempty'
          AND conrelid = 'a6_trade.model_publications'::regclass
    ) THEN
        ALTER TABLE a6_trade.model_publications
            ADD CONSTRAINT model_publications_scope_nonempty
            CHECK (publication_scope IS NULL OR publication_scope <> '');
    END IF;
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'model_publications_cost_generation_positive'
          AND conrelid = 'a6_trade.model_publications'::regclass
    ) THEN
        ALTER TABLE a6_trade.model_publications
            ADD CONSTRAINT model_publications_cost_generation_positive
            CHECK (desired_cost_generation IS NULL OR desired_cost_generation > 0);
    END IF;
END
$$;
