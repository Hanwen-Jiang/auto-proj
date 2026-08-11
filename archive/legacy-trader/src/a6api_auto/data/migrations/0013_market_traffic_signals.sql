CREATE TABLE IF NOT EXISTS a6_trade.a6_market_channel_signals_current (
    model text NOT NULL,
    channel_id bigint NOT NULL,
    listing_id bigint,
    supplier_id bigint,
    realtime_ratio_usd numeric(24, 12),
    realtime_ratio_cny numeric(24, 12),
    final_multiplier numeric(24, 12),
    market_rank integer,
    available boolean NOT NULL DEFAULT true,
    is_owned boolean NOT NULL DEFAULT false,
    last_success_at timestamptz,
    first_observed_at timestamptz NOT NULL,
    last_observed_at timestamptz NOT NULL,
    observer_merchant_id bigint NOT NULL
        REFERENCES a6_trade.a6_merchants(id),
    scan_run_id bigint NOT NULL
        REFERENCES a6_trade.a6_market_scan_runs(id),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (model, channel_id),
    CONSTRAINT a6_market_signal_ids_positive CHECK (
        channel_id > 0
        AND (listing_id IS NULL OR listing_id > 0)
        AND (supplier_id IS NULL OR supplier_id > 0)
    ),
    CONSTRAINT a6_market_signal_values_positive CHECK (
        (realtime_ratio_usd IS NULL OR realtime_ratio_usd > 0)
        AND (realtime_ratio_cny IS NULL OR realtime_ratio_cny > 0)
        AND (final_multiplier IS NULL OR final_multiplier > 0)
        AND (market_rank IS NULL OR market_rank > 0)
    ),
    CONSTRAINT a6_market_signal_time_order CHECK (
        first_observed_at <= last_observed_at
        AND (
            last_success_at IS NULL
            OR last_success_at <= last_observed_at + interval '5 minutes'
        )
    )
);

CREATE INDEX IF NOT EXISTS a6_market_signals_model_price_idx
    ON a6_trade.a6_market_channel_signals_current (
        model, realtime_ratio_cny, channel_id
    );
CREATE INDEX IF NOT EXISTS a6_market_signals_model_observed_idx
    ON a6_trade.a6_market_channel_signals_current (
        model, last_observed_at DESC
    );

CREATE TABLE IF NOT EXISTS a6_trade.a6_market_traffic_events (
    id bigserial PRIMARY KEY,
    model text NOT NULL,
    channel_id bigint NOT NULL,
    listing_id bigint,
    supplier_id bigint,
    success_at timestamptz NOT NULL,
    first_observed_at timestamptz NOT NULL,
    last_observed_at timestamptz NOT NULL,
    realtime_ratio_usd numeric(24, 12),
    realtime_ratio_cny numeric(24, 12),
    final_multiplier numeric(24, 12),
    market_rank integer,
    is_owned boolean NOT NULL DEFAULT false,
    observer_ids bigint[] NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (model, channel_id, success_at),
    CONSTRAINT a6_market_traffic_ids_positive CHECK (
        channel_id > 0
        AND (listing_id IS NULL OR listing_id > 0)
        AND (supplier_id IS NULL OR supplier_id > 0)
    ),
    CONSTRAINT a6_market_traffic_values_positive CHECK (
        (realtime_ratio_usd IS NULL OR realtime_ratio_usd > 0)
        AND (realtime_ratio_cny IS NULL OR realtime_ratio_cny > 0)
        AND (final_multiplier IS NULL OR final_multiplier > 0)
        AND (market_rank IS NULL OR market_rank > 0)
    ),
    CONSTRAINT a6_market_traffic_observers_present CHECK (
        cardinality(observer_ids) > 0
        AND array_position(observer_ids, NULL) IS NULL
    ),
    CONSTRAINT a6_market_traffic_time_order CHECK (
        success_at <= first_observed_at + interval '5 minutes'
        AND first_observed_at <= last_observed_at
    )
);

CREATE INDEX IF NOT EXISTS a6_market_traffic_model_observed_idx
    ON a6_trade.a6_market_traffic_events (
        model, first_observed_at DESC
    );
CREATE INDEX IF NOT EXISTS a6_market_traffic_model_price_idx
    ON a6_trade.a6_market_traffic_events (
        model, realtime_ratio_cny, first_observed_at DESC
    );
CREATE INDEX IF NOT EXISTS a6_market_traffic_owned_idx
    ON a6_trade.a6_market_traffic_events (
        model, first_observed_at DESC
    )
    WHERE is_owned;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'a6trader') THEN
        GRANT SELECT, INSERT, UPDATE, DELETE
            ON a6_trade.a6_market_channel_signals_current TO a6trader;
        GRANT SELECT, INSERT, UPDATE, DELETE
            ON a6_trade.a6_market_traffic_events TO a6trader;
        GRANT USAGE, SELECT, UPDATE
            ON SEQUENCE a6_trade.a6_market_traffic_events_id_seq TO a6trader;
    END IF;
END
$$;

