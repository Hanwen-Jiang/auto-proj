-- The Sub2API request plane resolves Provider dispatch leases from PostgreSQL
-- when the Redis projection is absent or needs an authoritative watermark.
-- Keep this runtime grant read-only and limited to the tables referenced by
-- account_repo_a6_provider_dispatch.go.

DO $$
DECLARE
    request_plane_role text;
BEGIN
    FOREACH request_plane_role IN ARRAY ARRAY['sub2api_cm', 'sub2api']
    LOOP
        IF EXISTS (
            SELECT 1 FROM pg_roles WHERE rolname = request_plane_role
        ) THEN
            EXECUTE format(
                'GRANT USAGE ON SCHEMA a6_trade TO %I',
                request_plane_role
            );
            EXECUTE format(
                'GRANT SELECT ON TABLE '
                'a6_trade.provider_pool_generations, '
                'a6_trade.provider_cost_generations, '
                'a6_trade.provider_pool_slots, '
                'a6_trade.a6_tokens, '
                'a6_trade.enodot_account_bindings, '
                'a6_trade.a6_pin_bindings, '
                'a6_trade.provider_fast_block_outbox, '
                'a6_trade.provider_quarantines TO %I',
                request_plane_role
            );
        END IF;
    END LOOP;
END
$$;
