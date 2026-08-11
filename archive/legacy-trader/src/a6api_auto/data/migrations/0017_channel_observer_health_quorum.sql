CREATE TABLE IF NOT EXISTS a6_trade.a6_channel_observer_health_current (
    model text NOT NULL,
    channel_id bigint NOT NULL,
    listing_id bigint,
    observer_merchant_id bigint NOT NULL
        REFERENCES a6_trade.a6_merchants(id),
    health_state text NOT NULL,
    available boolean NOT NULL,
    evidence jsonb NOT NULL DEFAULT '{}'::jsonb,
    observed_at timestamptz NOT NULL,
    scan_run_id bigint NOT NULL
        REFERENCES a6_trade.a6_market_scan_runs(id),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (model, channel_id, observer_merchant_id),
    CONSTRAINT a6_channel_observer_health_ids_positive CHECK (
        channel_id > 0 AND (listing_id IS NULL OR listing_id > 0)
    ),
    CONSTRAINT a6_channel_observer_health_state_known CHECK (
        health_state IN (
            'cooling', 'unavailable', 'degraded', 'unlabeled',
            'idle', 'healthy', 'unknown'
        )
    ),
    CONSTRAINT a6_channel_observer_health_evidence_object CHECK (
        jsonb_typeof(evidence) = 'object'
    )
);

CREATE INDEX IF NOT EXISTS a6_channel_observer_health_quorum_idx
    ON a6_trade.a6_channel_observer_health_current (
        model, channel_id, health_state, observed_at DESC
    );

ALTER TABLE a6_trade.a6_channel_growth_policies
    ADD COLUMN IF NOT EXISTS minimum_health_observers integer NOT NULL DEFAULT 2;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'a6_channel_growth_minimum_health_observers_valid'
          AND conrelid = 'a6_trade.a6_channel_growth_policies'::regclass
    ) THEN
        ALTER TABLE a6_trade.a6_channel_growth_policies
            ADD CONSTRAINT a6_channel_growth_minimum_health_observers_valid CHECK (
                minimum_health_observers BETWEEN 1 AND 10
            );
    END IF;
END
$$;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'a6trader') THEN
        GRANT SELECT, INSERT, UPDATE, DELETE
            ON a6_trade.a6_channel_observer_health_current TO a6trader;
    END IF;
END
$$;
