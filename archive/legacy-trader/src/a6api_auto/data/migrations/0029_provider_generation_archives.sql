-- Archive expired Provider routing/cost bundles before removing live-table history.
-- Only superseded/superseded pairs are eligible for this retention path.

CREATE TABLE IF NOT EXISTS a6_trade.provider_generation_archives (
    id bigserial PRIMARY KEY,
    model text NOT NULL,
    routing_generation bigint NOT NULL,
    cost_generation bigint NOT NULL,
    routing_generation_id bigint NOT NULL,
    cost_generation_id bigint NOT NULL,
    source_hash char(64) NOT NULL,
    cost_input_hash char(64) NOT NULL,
    cost_output_hash char(64) NOT NULL,
    generation_state text NOT NULL,
    pricing_state text NOT NULL,
    routing_updated_at timestamptz NOT NULL,
    cost_updated_at timestamptz NOT NULL,
    routing_snapshot jsonb NOT NULL,
    cost_snapshot jsonb NOT NULL,
    slot_snapshot jsonb NOT NULL DEFAULT '[]'::jsonb,
    slot_count integer NOT NULL DEFAULT 0,
    retention_cutoff timestamptz NOT NULL,
    archive_reason text NOT NULL DEFAULT 'retention_expired',
    archived_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (model, routing_generation, cost_generation),
    CONSTRAINT provider_generation_archive_ids_positive CHECK (
        routing_generation > 0 AND cost_generation > 0
        AND routing_generation_id > 0 AND cost_generation_id > 0
    ),
    CONSTRAINT provider_generation_archive_hashes_valid CHECK (
        source_hash ~ '^[0-9a-fA-F]{64}$'
        AND cost_input_hash ~ '^[0-9a-fA-F]{64}$'
        AND cost_output_hash ~ '^[0-9a-fA-F]{64}$'
    ),
    CONSTRAINT provider_generation_archive_states_fixed CHECK (
        generation_state = 'superseded' AND pricing_state = 'superseded'
    ),
    CONSTRAINT provider_generation_archive_reason_known CHECK (
        archive_reason = 'retention_expired'
    ),
    CONSTRAINT provider_generation_archive_snapshots_valid CHECK (
        jsonb_typeof(routing_snapshot) = 'object'
        AND jsonb_typeof(cost_snapshot) = 'object'
        AND jsonb_typeof(slot_snapshot) = 'array'
    ),
    CONSTRAINT provider_generation_archive_slot_count_valid CHECK (
        slot_count >= 0 AND jsonb_array_length(slot_snapshot) = slot_count
    )
);

CREATE INDEX IF NOT EXISTS provider_generation_archives_model_time_idx
    ON a6_trade.provider_generation_archives (model, archived_at DESC);

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'a6trader') THEN
        GRANT SELECT, INSERT
            ON a6_trade.provider_generation_archives TO a6trader;
        GRANT USAGE, SELECT
            ON SEQUENCE a6_trade.provider_generation_archives_id_seq TO a6trader;
    END IF;
END
$$;
