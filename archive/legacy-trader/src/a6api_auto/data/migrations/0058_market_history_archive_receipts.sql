CREATE TABLE IF NOT EXISTS a6_trade.a6_market_history_archives (
    id bigserial PRIMARY KEY,
    archive_format text NOT NULL,
    archive_prefix text NOT NULL UNIQUE,
    store_backend text NOT NULL,
    store_bucket text NOT NULL,
    store_endpoint_hash char(64) NOT NULL,
    retention_cutoff timestamptz NOT NULL,
    oldest_started_at timestamptz NOT NULL,
    newest_started_at timestamptz NOT NULL,
    scan_run_ids bigint[] NOT NULL,
    scan_run_count integer NOT NULL,
    quote_count bigint NOT NULL,
    scan_object_key text NOT NULL,
    scan_object_sha256 char(64) NOT NULL,
    scan_object_bytes bigint NOT NULL,
    quote_object_key text NOT NULL,
    quote_object_sha256 char(64) NOT NULL,
    quote_object_bytes bigint NOT NULL,
    manifest_object_key text NOT NULL,
    manifest_object_sha256 char(64) NOT NULL,
    manifest_object_bytes bigint NOT NULL,
    archived_at timestamptz NOT NULL,
    deleted_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT a6_market_history_archives_counts_positive CHECK (
        scan_run_count > 0
        AND quote_count >= 0
        AND scan_object_bytes > 0
        AND quote_object_bytes > 0
        AND manifest_object_bytes > 0
        AND cardinality(scan_run_ids) = scan_run_count
    ),
    CONSTRAINT a6_market_history_archives_time_order CHECK (
        oldest_started_at <= newest_started_at
        AND newest_started_at < retention_cutoff
        AND archived_at <= deleted_at
    )
);

CREATE INDEX IF NOT EXISTS a6_market_history_archives_deleted_idx
    ON a6_trade.a6_market_history_archives (deleted_at DESC, id DESC);

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'a6trader') THEN
        GRANT SELECT, INSERT ON a6_trade.a6_market_history_archives TO a6trader;
        GRANT USAGE, SELECT ON SEQUENCE a6_trade.a6_market_history_archives_id_seq
            TO a6trader;
    END IF;
END
$$;
