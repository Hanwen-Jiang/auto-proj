CREATE TABLE IF NOT EXISTS a6_trade.a6_supplier_channels_current (
    merchant_id bigint NOT NULL REFERENCES a6_trade.a6_merchants(id) ON DELETE CASCADE,
    a6_channel_id bigint NOT NULL,
    supplier_id bigint,
    name text NOT NULL DEFAULT '',
    nickname text NOT NULL DEFAULT '',
    channel_type integer,
    status integer,
    availability integer,
    observed_at timestamptz NOT NULL,
    source_hash char(64) NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT a6_supplier_channels_external_id_positive CHECK (a6_channel_id > 0),
    UNIQUE (merchant_id, a6_channel_id)
);

CREATE INDEX IF NOT EXISTS a6_supplier_channels_supplier_idx
    ON a6_trade.a6_supplier_channels_current (supplier_id, a6_channel_id)
    WHERE supplier_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS a6_supplier_channels_availability_idx
    ON a6_trade.a6_supplier_channels_current (merchant_id, availability, status);

CREATE TABLE IF NOT EXISTS a6_trade.a6_supplier_listings_current (
    merchant_id bigint NOT NULL REFERENCES a6_trade.a6_merchants(id) ON DELETE CASCADE,
    a6_listing_id bigint NOT NULL,
    a6_channel_id bigint NOT NULL,
    own_model text NOT NULL DEFAULT '',
    platform_model text NOT NULL DEFAULT '',
    charge_type text NOT NULL DEFAULT 'unknown',
    input_price_micros bigint NOT NULL DEFAULT 0,
    output_price_micros bigint NOT NULL DEFAULT 0,
    cache_read_price_micros bigint NOT NULL DEFAULT 0,
    cache_write_price_micros bigint NOT NULL DEFAULT 0,
    status integer,
    availability integer,
    visibility integer,
    display_name text NOT NULL DEFAULT '',
    description text NOT NULL DEFAULT '',
    managed_state text NOT NULL DEFAULT 'existing_unmanaged',
    observed_at timestamptz NOT NULL,
    source_hash char(64) NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT a6_supplier_listings_prices_nonnegative CHECK (
        input_price_micros >= 0
        AND output_price_micros >= 0
        AND cache_read_price_micros >= 0
        AND cache_write_price_micros >= 0
    ),
    CONSTRAINT a6_supplier_listings_managed_state_known CHECK (
        managed_state IN ('existing_unmanaged', 'managed')
    ),
    CONSTRAINT a6_supplier_listings_external_ids_positive CHECK (
        a6_listing_id > 0 AND a6_channel_id > 0
    ),
    UNIQUE (merchant_id, a6_listing_id)
);

CREATE INDEX IF NOT EXISTS a6_supplier_listings_channel_idx
    ON a6_trade.a6_supplier_listings_current (merchant_id, a6_channel_id);
CREATE INDEX IF NOT EXISTS a6_supplier_listings_model_visibility_idx
    ON a6_trade.a6_supplier_listings_current (
        platform_model,
        availability,
        visibility
    );
CREATE INDEX IF NOT EXISTS a6_supplier_listings_managed_state_idx
    ON a6_trade.a6_supplier_listings_current (merchant_id, managed_state, observed_at DESC);
