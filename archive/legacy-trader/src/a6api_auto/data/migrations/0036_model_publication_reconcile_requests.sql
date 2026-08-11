-- Durable wake-up queue for failure-driven and operator-requested publication
-- reconciliation.  The request payload is control metadata only; request bodies,
-- credentials and raw upstream errors are deliberately excluded.

CREATE TABLE IF NOT EXISTS a6_trade.model_publication_reconcile_requests (
    id bigserial PRIMARY KEY,
    idempotency_key text NOT NULL UNIQUE,
    model text NOT NULL,
    expected_generation bigint NOT NULL,
    expected_cost_generation bigint,
    request_source text NOT NULL,
    requested_by text NOT NULL,
    trace_id text NOT NULL,
    request_payload jsonb NOT NULL,
    status text NOT NULL DEFAULT 'pending',
    attempt_count integer NOT NULL DEFAULT 0,
    last_error_class text,
    result jsonb,
    requested_at timestamptz NOT NULL DEFAULT now(),
    started_at timestamptz,
    completed_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT model_publication_reconcile_generation_positive CHECK (
        expected_generation > 0
        AND (expected_cost_generation IS NULL OR expected_cost_generation > 0)
    ),
    CONSTRAINT model_publication_reconcile_source_known CHECK (
        request_source IN ('provider_failure', 'manual', 'repair')
    ),
    CONSTRAINT model_publication_reconcile_status_known CHECK (
        status IN ('pending', 'running', 'completed', 'failed')
    ),
    CONSTRAINT model_publication_reconcile_attempt_nonnegative CHECK (
        attempt_count >= 0
    ),
    CONSTRAINT model_publication_reconcile_payload_object CHECK (
        jsonb_typeof(request_payload) = 'object'
    )
);

CREATE INDEX IF NOT EXISTS model_publication_reconcile_pending_idx
    ON a6_trade.model_publication_reconcile_requests (
        model, status, requested_at, id
    )
    WHERE status IN ('pending', 'running', 'failed');

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'a6trader') THEN
        GRANT SELECT, INSERT, UPDATE
            ON a6_trade.model_publication_reconcile_requests TO a6trader;
        GRANT USAGE, SELECT
            ON SEQUENCE a6_trade.model_publication_reconcile_requests_id_seq TO a6trader;
    END IF;
END
$$;
