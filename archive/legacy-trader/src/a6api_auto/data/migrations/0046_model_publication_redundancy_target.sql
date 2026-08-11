-- Keep the publication floor separate from the desired healthy redundancy.
ALTER TABLE a6_trade.model_publications
    ADD COLUMN IF NOT EXISTS redundancy_target integer;

UPDATE a6_trade.model_publications
SET redundancy_target = GREATEST(min_healthy_slots, 3)
WHERE redundancy_target IS NULL
   OR redundancy_target < min_healthy_slots;

ALTER TABLE a6_trade.model_publications
    ALTER COLUMN redundancy_target SET DEFAULT 3,
    ALTER COLUMN redundancy_target SET NOT NULL;

ALTER TABLE a6_trade.model_publications
    DROP CONSTRAINT IF EXISTS provider_publication_slots_valid;
ALTER TABLE a6_trade.model_publications
    ADD CONSTRAINT provider_publication_slots_valid CHECK (
        healthy_slots >= 0
        AND min_healthy_slots >= 1
        AND redundancy_target >= min_healthy_slots
    );
