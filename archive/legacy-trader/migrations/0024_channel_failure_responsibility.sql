CREATE TABLE IF NOT EXISTS a6_trade.a6_channel_failure_events (
    id bigserial PRIMARY KEY,
    model text NOT NULL,
    channel_id bigint NOT NULL,
    listing_id bigint,
    supplier_id bigint,
    failure_at timestamptz NOT NULL,
    failure_code text NOT NULL,
    responsibility text NOT NULL,
    first_observed_at timestamptz NOT NULL,
    last_observed_at timestamptz NOT NULL,
    observer_ids bigint[] NOT NULL DEFAULT '{}'::bigint[],
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT a6_channel_failure_event_ids_positive CHECK (
        channel_id > 0
        AND (listing_id IS NULL OR listing_id > 0)
        AND (supplier_id IS NULL OR supplier_id > 0)
    ),
    CONSTRAINT a6_channel_failure_event_code_bounded CHECK (
        length(failure_code) BETWEEN 1 AND 128
    ),
    CONSTRAINT a6_channel_failure_event_responsibility_known CHECK (
        responsibility IN ('client', 'provider', 'platform', 'unknown')
    ),
    CONSTRAINT a6_channel_failure_event_observation_order CHECK (
        first_observed_at <= last_observed_at
    ),
    UNIQUE (model, channel_id, failure_at, failure_code)
);

CREATE INDEX IF NOT EXISTS a6_channel_failure_events_recent_idx
    ON a6_trade.a6_channel_failure_events (model, channel_id, failure_at DESC);
CREATE INDEX IF NOT EXISTS a6_channel_failure_events_responsibility_idx
    ON a6_trade.a6_channel_failure_events (model, responsibility, failure_at DESC);
CREATE INDEX IF NOT EXISTS a6_channel_failure_events_retention_idx
    ON a6_trade.a6_channel_failure_events (failure_at);

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'a6trader') THEN
        GRANT SELECT, INSERT, UPDATE, DELETE
            ON a6_trade.a6_channel_failure_events TO a6trader;
        GRANT USAGE, SELECT, UPDATE
            ON SEQUENCE a6_trade.a6_channel_failure_events_id_seq TO a6trader;
    END IF;
END
$$;
