-- PostgreSQL-authoritative receipts and capacity escrow for Provider regional
-- coordination. Regional Redis remains a projection; request-plane roles are
-- deliberately read-only and projector/subscriber roles cannot allocate quota.

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'enodot_binding_id_account_uq'
          AND conrelid = 'a6_trade.enodot_account_bindings'::regclass
    ) THEN
        ALTER TABLE a6_trade.enodot_account_bindings
            ADD CONSTRAINT enodot_binding_id_account_uq
            UNIQUE (id, enodot_account_id);
    END IF;
END
$$;

CREATE TABLE a6_trade.provider_region_projection_receipts (
    id bigserial PRIMARY KEY,
    region text NOT NULL,
    model text NOT NULL,
    projection_type text NOT NULL,
    routing_generation bigint NOT NULL,
    cost_generation bigint NOT NULL,
    source_hash char(64) NOT NULL,
    projection_digest char(64) NOT NULL,
    last_event_id bigint NOT NULL DEFAULT 0,
    subscriber_cursor bigint NOT NULL DEFAULT 0,
    subscriber_connected boolean NOT NULL DEFAULT false,
    redis_restart_epoch text NOT NULL,
    projection_state text NOT NULL DEFAULT 'projecting',
    receipt_version bigint NOT NULL DEFAULT 1,
    projected_at timestamptz NOT NULL,
    subscriber_verified_at timestamptz,
    verified_at timestamptz,
    last_error_class text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (region, model, projection_type),
    CONSTRAINT provider_region_projection_identity_valid CHECK (
        region ~ '^[a-z0-9][a-z0-9-]{0,31}$'
        AND model <> '' AND length(model) <= 128
        AND projection_type IN ('dispatch', 'fast_block')
        AND (
            (source_hash = repeat('0', 64)
             AND projection_type = 'dispatch'
             AND routing_generation >= 0 AND cost_generation >= 0)
            OR (source_hash <> repeat('0', 64)
                AND routing_generation > 0 AND cost_generation > 0)
        )
        AND receipt_version > 0
    ),
    CONSTRAINT provider_region_projection_hashes_valid CHECK (
        source_hash ~ '^[0-9a-f]{64}$'
        AND projection_digest ~ '^[0-9a-f]{64}$'
    ),
    CONSTRAINT provider_region_projection_cursor_valid CHECK (
        last_event_id >= 0
        AND subscriber_cursor >= 0
        AND subscriber_cursor <= last_event_id
    ),
    CONSTRAINT provider_region_projection_epoch_valid CHECK (
        redis_restart_epoch <> '' AND length(redis_restart_epoch) <= 128
    ),
    CONSTRAINT provider_region_projection_state_known CHECK (
        projection_state IN ('projecting', 'resyncing', 'ready', 'failed', 'stale')
    ),
    CONSTRAINT provider_region_projection_ready_complete CHECK (
        projection_state <> 'ready'
        OR (
            subscriber_connected
            AND subscriber_cursor = last_event_id
            AND subscriber_verified_at IS NOT NULL
            AND verified_at IS NOT NULL
            AND last_error_class IS NULL
        )
    ),
    CONSTRAINT provider_region_projection_times_valid CHECK (
        (subscriber_verified_at IS NULL OR subscriber_verified_at >= projected_at)
        AND (verified_at IS NULL OR verified_at >= projected_at)
    )
);

CREATE INDEX provider_region_projection_readiness_idx
    ON a6_trade.provider_region_projection_receipts (
        region, projection_state, model, projection_type
    );

CREATE TABLE a6_trade.provider_region_capacity_allocations (
    id bigserial PRIMARY KEY,
    enodot_binding_id bigint NOT NULL,
    enodot_account_id bigint NOT NULL,
    region text NOT NULL,
    quota integer NOT NULL,
    account_concurrency integer NOT NULL,
    allocation_version bigint NOT NULL,
    allocation_key text NOT NULL,
    allocation_digest char(64) NOT NULL,
    source_hash char(64) NOT NULL,
    allocation_state text NOT NULL,
    drain_target_quota integer,
    effective_at timestamptz NOT NULL,
    drain_started_at timestamptz,
    valid_to timestamptz,
    reason text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT provider_region_capacity_binding_fk
        FOREIGN KEY (enodot_binding_id, enodot_account_id)
        REFERENCES a6_trade.enodot_account_bindings(id, enodot_account_id),
    CONSTRAINT provider_region_capacity_identity_uq
        UNIQUE (enodot_account_id, allocation_version, region),
    CONSTRAINT provider_region_capacity_idempotency_uq
        UNIQUE (enodot_account_id, allocation_key, region),
    CONSTRAINT provider_region_capacity_receipt_identity_uq
        UNIQUE (id, enodot_binding_id, enodot_account_id, region,
                allocation_version, quota),
    CONSTRAINT provider_region_capacity_identity_valid CHECK (
        enodot_binding_id > 0 AND enodot_account_id > 0
        AND region ~ '^[a-z0-9][a-z0-9-]{0,31}$'
        AND quota >= 0 AND account_concurrency >= 0
        AND quota <= account_concurrency
        AND allocation_version > 0
        AND allocation_key <> '' AND length(allocation_key) <= 160
        AND reason <> '' AND length(reason) <= 512
    ),
    CONSTRAINT provider_region_capacity_hashes_valid CHECK (
        allocation_digest ~ '^[0-9a-f]{64}$'
        AND source_hash ~ '^[0-9a-f]{64}$'
    ),
    CONSTRAINT provider_region_capacity_state_known CHECK (
        allocation_state IN ('active', 'draining', 'disabled')
    ),
    CONSTRAINT provider_region_capacity_state_complete CHECK (
        (allocation_state = 'active'
         AND quota > 0
         AND drain_target_quota IS NULL
         AND drain_started_at IS NULL)
        OR (allocation_state = 'disabled'
            AND quota = 0
            AND drain_target_quota IS NULL
            AND drain_started_at IS NULL)
        OR (allocation_state = 'draining'
            AND quota > 0
            AND drain_target_quota >= 0
            AND drain_target_quota < quota
            AND drain_started_at IS NOT NULL)
    ),
    CONSTRAINT provider_region_capacity_times_valid CHECK (
        (drain_started_at IS NULL OR drain_started_at >= effective_at)
        AND (valid_to IS NULL OR valid_to >= effective_at)
    )
);

CREATE UNIQUE INDEX provider_region_capacity_current_idx
    ON a6_trade.provider_region_capacity_allocations (enodot_account_id, region)
    WHERE valid_to IS NULL;

CREATE INDEX provider_region_capacity_version_idx
    ON a6_trade.provider_region_capacity_allocations (
        enodot_account_id, allocation_version, region
    ) WHERE valid_to IS NULL;

CREATE INDEX provider_region_capacity_drain_idx
    ON a6_trade.provider_region_capacity_allocations (drain_started_at, region)
    WHERE valid_to IS NULL AND allocation_state = 'draining';

CREATE TABLE a6_trade.provider_region_capacity_receipts (
    id bigserial PRIMARY KEY,
    allocation_id bigint NOT NULL,
    enodot_binding_id bigint NOT NULL,
    enodot_account_id bigint NOT NULL,
    region text NOT NULL,
    allocation_version bigint NOT NULL,
    quota integer NOT NULL,
    occupancy integer NOT NULL,
    occupancy_digest char(64) NOT NULL,
    lease_cursor bigint NOT NULL,
    redis_restart_epoch text NOT NULL,
    receipt_state text NOT NULL DEFAULT 'resyncing',
    receipt_version bigint NOT NULL DEFAULT 1,
    projected_at timestamptz NOT NULL,
    verified_at timestamptz,
    last_error_class text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (enodot_account_id, region),
    CONSTRAINT provider_region_capacity_receipt_allocation_fk
        FOREIGN KEY (
            allocation_id, enodot_binding_id, enodot_account_id, region,
            allocation_version, quota
        ) REFERENCES a6_trade.provider_region_capacity_allocations (
            id, enodot_binding_id, enodot_account_id, region,
            allocation_version, quota
        ),
    CONSTRAINT provider_region_capacity_receipt_identity_valid CHECK (
        allocation_id > 0 AND enodot_binding_id > 0 AND enodot_account_id > 0
        AND region ~ '^[a-z0-9][a-z0-9-]{0,31}$'
        AND allocation_version > 0
        AND quota >= 0 AND occupancy >= 0 AND occupancy <= quota
        AND lease_cursor >= 0 AND receipt_version > 0
    ),
    CONSTRAINT provider_region_capacity_receipt_digest_valid CHECK (
        occupancy_digest ~ '^[0-9a-f]{64}$'
    ),
    CONSTRAINT provider_region_capacity_receipt_epoch_valid CHECK (
        redis_restart_epoch <> '' AND length(redis_restart_epoch) <= 128
    ),
    CONSTRAINT provider_region_capacity_receipt_state_known CHECK (
        receipt_state IN ('resyncing', 'ready', 'failed', 'stale')
    ),
    CONSTRAINT provider_region_capacity_receipt_ready_complete CHECK (
        receipt_state <> 'ready'
        OR (verified_at IS NOT NULL AND last_error_class IS NULL)
    ),
    CONSTRAINT provider_region_capacity_receipt_times_valid CHECK (
        verified_at IS NULL OR verified_at >= projected_at
    )
);

CREATE INDEX provider_region_capacity_receipt_readiness_idx
    ON a6_trade.provider_region_capacity_receipts (
        region, receipt_state, enodot_account_id
    );

CREATE FUNCTION a6_trade.provider_region_projection_receipt_guard()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    same_generation boolean;
    projection_changed boolean;
    reprojection_started boolean;
    last_routing_generation bigint;
    last_cost_generation bigint;
BEGIN
    IF NEW.source_hash = repeat('0', 64) THEN
        SELECT coalesce(
                   max(generation) FILTER (WHERE activated_at IS NOT NULL), 0
               )
        INTO last_routing_generation
        FROM a6_trade.provider_pool_generations
        WHERE model = NEW.model;
        SELECT coalesce(
                   max(cost_generation) FILTER (WHERE activated_at IS NOT NULL), 0
               )
        INTO last_cost_generation
        FROM a6_trade.provider_cost_generations
        WHERE model = NEW.model;
        IF NEW.projection_type <> 'dispatch'
           OR EXISTS (
               SELECT 1
               FROM a6_trade.provider_pool_generations generation
               WHERE generation.model = NEW.model
                 AND generation.generation_state = 'active'
           )
           OR NEW.routing_generation <> last_routing_generation
           OR NEW.cost_generation <> last_cost_generation THEN
            RAISE EXCEPTION 'provider_region_canonical_tombstone_mismatch'
                USING ERRCODE = '23503';
        END IF;
    ELSIF NOT EXISTS (
            SELECT 1
            FROM a6_trade.provider_pool_generations generation
            JOIN a6_trade.provider_cost_generations cost
              ON cost.model = generation.model
             AND cost.cost_generation = generation.cost_generation
            WHERE generation.model = NEW.model
              AND generation.generation = NEW.routing_generation
              AND generation.cost_generation = NEW.cost_generation
              AND generation.source_hash = NEW.source_hash
              AND generation.generation_state = 'active'
              AND cost.pricing_state = 'active'
        ) THEN
            RAISE EXCEPTION 'provider_region_canonical_generation_missing'
                USING ERRCODE = '23503';
    END IF;

    IF TG_OP = 'INSERT' THEN
        IF NEW.receipt_version <> 1 OR NEW.projection_state = 'ready' THEN
            RAISE EXCEPTION 'provider_region_receipt_initial_resync_required'
                USING ERRCODE = '23514';
        END IF;
        RETURN NEW;
    END IF;

    IF (NEW.id, NEW.region, NEW.model, NEW.projection_type, NEW.created_at)
       IS DISTINCT FROM
       (OLD.id, OLD.region, OLD.model, OLD.projection_type, OLD.created_at) THEN
        RAISE EXCEPTION 'provider_region_receipt_identity_immutable'
            USING ERRCODE = '23514';
    END IF;
    IF NEW.receipt_version <> OLD.receipt_version + 1 THEN
        RAISE EXCEPTION 'provider_region_receipt_cas_mismatch'
            USING ERRCODE = '23514';
    END IF;
    IF NEW.routing_generation < OLD.routing_generation
       OR NEW.cost_generation < OLD.cost_generation THEN
        RAISE EXCEPTION 'provider_region_receipt_stale_generation'
            USING ERRCODE = '23514';
    END IF;
    IF NEW.last_event_id < OLD.last_event_id THEN
        RAISE EXCEPTION 'provider_region_receipt_stale_cursor'
            USING ERRCODE = '23514';
    END IF;

    same_generation := NEW.routing_generation = OLD.routing_generation
        AND NEW.cost_generation = OLD.cost_generation;
    IF same_generation
       AND NEW.source_hash IS DISTINCT FROM OLD.source_hash
       AND NOT (
           NEW.projection_type = 'dispatch'
           AND NEW.source_hash = repeat('0', 64)
           AND OLD.source_hash <> repeat('0', 64)
       ) THEN
        RAISE EXCEPTION 'provider_region_receipt_digest_conflict'
            USING ERRCODE = '23514';
    END IF;
    IF same_generation
       AND (NEW.projection_type = 'dispatch' OR NEW.last_event_id = OLD.last_event_id)
       AND NEW.projection_digest IS DISTINCT FROM OLD.projection_digest
       AND NOT (
           NEW.projection_type = 'dispatch'
           AND NEW.source_hash = repeat('0', 64)
           AND OLD.source_hash <> repeat('0', 64)
       ) THEN
        RAISE EXCEPTION 'provider_region_receipt_digest_conflict'
            USING ERRCODE = '23514';
    END IF;

    projection_changed := (NEW.routing_generation, NEW.cost_generation,
        NEW.source_hash, NEW.projection_digest, NEW.last_event_id,
        NEW.redis_restart_epoch) IS DISTINCT FROM
        (OLD.routing_generation, OLD.cost_generation, OLD.source_hash,
         OLD.projection_digest, OLD.last_event_id, OLD.redis_restart_epoch);
    reprojection_started := NEW.projection_state IN ('projecting', 'resyncing')
        AND (
            projection_changed
            OR OLD.projection_state NOT IN ('projecting', 'resyncing')
            OR NEW.projected_at > OLD.projected_at
        );
    IF reprojection_started THEN
        NEW.subscriber_cursor := 0;
        NEW.subscriber_connected := false;
        NEW.subscriber_verified_at := NULL;
        NEW.verified_at := NULL;
    END IF;

    IF NEW.redis_restart_epoch IS DISTINCT FROM OLD.redis_restart_epoch THEN
        IF NEW.projection_state <> 'resyncing'
           OR NEW.subscriber_connected
           OR NEW.subscriber_cursor <> 0
           OR NEW.subscriber_verified_at IS NOT NULL
           OR NEW.verified_at IS NOT NULL THEN
            RAISE EXCEPTION 'provider_region_restart_resync_required'
                USING ERRCODE = '23514';
        END IF;
    ELSIF NOT reprojection_started
          AND NEW.subscriber_cursor < OLD.subscriber_cursor THEN
        RAISE EXCEPTION 'provider_region_receipt_stale_cursor'
            USING ERRCODE = '23514';
    END IF;
    IF NOT same_generation AND NEW.projection_state = 'ready' THEN
        RAISE EXCEPTION 'provider_region_generation_resync_required'
            USING ERRCODE = '23514';
    END IF;
    IF NEW.projected_at < OLD.projected_at THEN
        RAISE EXCEPTION 'provider_region_receipt_stale_timestamp'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER provider_region_projection_receipt_guard
BEFORE INSERT OR UPDATE ON a6_trade.provider_region_projection_receipts
FOR EACH ROW EXECUTE FUNCTION a6_trade.provider_region_projection_receipt_guard();

CREATE FUNCTION a6_trade.provider_region_capacity_receipt_guard()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    allocation_is_current boolean;
    projection_changed boolean;
BEGIN
    SELECT allocation.valid_to IS NULL
    INTO allocation_is_current
    FROM a6_trade.provider_region_capacity_allocations allocation
    WHERE allocation.id = NEW.allocation_id;
    IF allocation_is_current IS DISTINCT FROM true THEN
        RAISE EXCEPTION 'provider_region_capacity_receipt_allocation_stale'
            USING ERRCODE = '23514';
    END IF;

    IF TG_OP = 'INSERT' THEN
        IF current_user = 'a6_provider_subscriber' THEN
            RAISE EXCEPTION 'provider_region_capacity_subscriber_insert_forbidden'
                USING ERRCODE = '42501';
        END IF;
        IF NEW.receipt_version <> 1 OR NEW.receipt_state <> 'resyncing' THEN
            RAISE EXCEPTION 'provider_region_capacity_initial_resync_required'
                USING ERRCODE = '23514';
        END IF;
        RETURN NEW;
    END IF;

    IF (NEW.id, NEW.enodot_binding_id, NEW.enodot_account_id, NEW.region, NEW.created_at)
       IS DISTINCT FROM
       (OLD.id, OLD.enodot_binding_id, OLD.enodot_account_id, OLD.region, OLD.created_at) THEN
        RAISE EXCEPTION 'provider_region_capacity_receipt_identity_immutable'
            USING ERRCODE = '23514';
    END IF;
    IF NEW.receipt_version <> OLD.receipt_version + 1 THEN
        RAISE EXCEPTION 'provider_region_capacity_receipt_cas_mismatch'
            USING ERRCODE = '23514';
    END IF;
    IF NEW.allocation_version < OLD.allocation_version THEN
        RAISE EXCEPTION 'provider_region_capacity_stale_allocation'
            USING ERRCODE = '23514';
    END IF;

    projection_changed := (NEW.allocation_id, NEW.allocation_version, NEW.quota,
        NEW.occupancy, NEW.occupancy_digest, NEW.lease_cursor,
        NEW.redis_restart_epoch, NEW.projected_at, NEW.last_error_class)
        IS DISTINCT FROM
        (OLD.allocation_id, OLD.allocation_version, OLD.quota, OLD.occupancy,
         OLD.occupancy_digest, OLD.lease_cursor, OLD.redis_restart_epoch,
         OLD.projected_at, OLD.last_error_class);
    IF projection_changed THEN
        NEW.receipt_state := 'resyncing';
        NEW.verified_at := NULL;
    END IF;

    IF current_user = 'a6_provider_projector'
       AND (NEW.receipt_state <> 'resyncing' OR NEW.verified_at IS NOT NULL) THEN
        RAISE EXCEPTION 'provider_region_capacity_projector_state_forbidden'
            USING ERRCODE = '42501';
    END IF;
    IF current_user = 'a6_provider_subscriber'
       AND (
           projection_changed
           OR NEW.receipt_state <> 'ready'
           OR NEW.verified_at IS NULL
       ) THEN
        RAISE EXCEPTION 'provider_region_capacity_subscriber_ack_invalid'
            USING ERRCODE = '42501';
    END IF;

    IF NEW.redis_restart_epoch IS DISTINCT FROM OLD.redis_restart_epoch THEN
        IF NEW.receipt_state <> 'resyncing'
           OR NEW.lease_cursor <> 0
           OR NEW.verified_at IS NOT NULL THEN
            RAISE EXCEPTION 'provider_region_capacity_restart_resync_required'
                USING ERRCODE = '23514';
        END IF;
    ELSIF NEW.lease_cursor < OLD.lease_cursor THEN
        RAISE EXCEPTION 'provider_region_capacity_stale_cursor'
            USING ERRCODE = '23514';
    ELSIF NEW.lease_cursor = OLD.lease_cursor
          AND NEW.allocation_version = OLD.allocation_version
          AND (NEW.occupancy, NEW.occupancy_digest)
              IS DISTINCT FROM (OLD.occupancy, OLD.occupancy_digest) THEN
        RAISE EXCEPTION 'provider_region_capacity_digest_conflict'
            USING ERRCODE = '23514';
    END IF;
    IF NEW.projected_at < OLD.projected_at THEN
        RAISE EXCEPTION 'provider_region_capacity_receipt_stale_timestamp'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER provider_region_capacity_receipt_guard
BEFORE INSERT OR UPDATE ON a6_trade.provider_region_capacity_receipts
FOR EACH ROW EXECUTE FUNCTION a6_trade.provider_region_capacity_receipt_guard();

CREATE FUNCTION a6_trade.assert_provider_region_capacity(account_id bigint)
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, a6_trade
AS $$
DECLARE
    canonical_concurrency integer;
    current_quota bigint;
    version_count bigint;
    binding_count bigint;
    region_count bigint;
    regions_known boolean;
    concurrency_matches boolean;
BEGIN
    PERFORM pg_advisory_xact_lock(
        hashtextextended('a6-provider-region-capacity:' || account_id::text, 0)
    );
    SELECT binding.desired_concurrency
    INTO canonical_concurrency
    FROM a6_trade.enodot_account_bindings binding
    WHERE binding.enodot_account_id = account_id
    FOR SHARE;

    SELECT coalesce(sum(allocation.quota), 0),
           count(DISTINCT allocation.allocation_version),
           count(DISTINCT allocation.enodot_binding_id),
           count(DISTINCT allocation.region),
           coalesce(bool_and(allocation.region IN ('us', 'hk')), true),
           coalesce(bool_and(allocation.account_concurrency = canonical_concurrency), true)
    INTO current_quota, version_count, binding_count, region_count,
         regions_known, concurrency_matches
    FROM a6_trade.provider_region_capacity_allocations allocation
    WHERE allocation.enodot_account_id = account_id
      AND allocation.valid_to IS NULL;

    IF current_quota = 0 AND version_count = 0 THEN
        RETURN;
    END IF;
    IF canonical_concurrency IS NULL THEN
        RAISE EXCEPTION 'provider_region_capacity_binding_missing'
            USING ERRCODE = '23503';
    END IF;
    IF current_quota > canonical_concurrency THEN
        RAISE EXCEPTION 'provider_region_capacity_overallocated'
            USING ERRCODE = '23514';
    END IF;
    IF version_count <> 1 OR binding_count <> 1 OR region_count <> 2
       OR NOT regions_known OR NOT concurrency_matches THEN
        RAISE EXCEPTION 'provider_region_capacity_current_version_invalid'
            USING ERRCODE = '23514';
    END IF;
END
$$;

CREATE FUNCTION a6_trade.provider_region_capacity_allocation_guard()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, a6_trade
AS $$
BEGIN
    PERFORM a6_trade.assert_provider_region_capacity(
        CASE WHEN TG_OP = 'DELETE' THEN OLD.enodot_account_id
             ELSE NEW.enodot_account_id END
    );
    RETURN NULL;
END
$$;

CREATE CONSTRAINT TRIGGER provider_region_capacity_allocation_guard
AFTER INSERT OR UPDATE OR DELETE
ON a6_trade.provider_region_capacity_allocations
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION a6_trade.provider_region_capacity_allocation_guard();

CREATE FUNCTION a6_trade.provider_region_binding_capacity_guard()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, a6_trade
AS $$
BEGIN
    IF OLD.enodot_account_id IS NOT NULL THEN
        PERFORM a6_trade.assert_provider_region_capacity(OLD.enodot_account_id);
    END IF;
    IF NEW.enodot_account_id IS NOT NULL
       AND NEW.enodot_account_id IS DISTINCT FROM OLD.enodot_account_id THEN
        PERFORM a6_trade.assert_provider_region_capacity(NEW.enodot_account_id);
    END IF;
    RETURN NULL;
END
$$;

CREATE CONSTRAINT TRIGGER provider_region_binding_capacity_guard
AFTER UPDATE ON a6_trade.enodot_account_bindings
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION a6_trade.provider_region_binding_capacity_guard();

REVOKE ALL ON FUNCTION a6_trade.assert_provider_region_capacity(bigint) FROM PUBLIC;
REVOKE ALL ON FUNCTION a6_trade.provider_region_capacity_allocation_guard() FROM PUBLIC;
REVOKE ALL ON FUNCTION a6_trade.provider_region_binding_capacity_guard() FROM PUBLIC;
REVOKE ALL ON FUNCTION a6_trade.provider_region_projection_receipt_guard() FROM PUBLIC;
REVOKE ALL ON FUNCTION a6_trade.provider_region_capacity_receipt_guard() FROM PUBLIC;

REVOKE ALL ON TABLE a6_trade.provider_region_projection_receipts,
    a6_trade.provider_region_capacity_allocations,
    a6_trade.provider_region_capacity_receipts FROM PUBLIC;
REVOKE ALL ON SEQUENCE a6_trade.provider_region_projection_receipts_id_seq,
    a6_trade.provider_region_capacity_allocations_id_seq,
    a6_trade.provider_region_capacity_receipts_id_seq FROM PUBLIC;

DO $$
DECLARE
    request_plane_role text;
BEGIN
    FOREACH request_plane_role IN ARRAY ARRAY['sub2api_cm', 'sub2api']
    LOOP
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = request_plane_role) THEN
            EXECUTE format(
                'REVOKE ALL ON TABLE '
                'a6_trade.provider_region_projection_receipts, '
                'a6_trade.provider_region_capacity_allocations, '
                'a6_trade.provider_region_capacity_receipts FROM %I',
                request_plane_role
            );
            EXECUTE format('GRANT USAGE ON SCHEMA a6_trade TO %I', request_plane_role);
            EXECUTE format(
                'GRANT SELECT ON TABLE '
                'a6_trade.provider_region_projection_receipts, '
                'a6_trade.provider_region_capacity_allocations, '
                'a6_trade.provider_region_capacity_receipts TO %I',
                request_plane_role
            );
        END IF;
    END LOOP;
END
$$;

DO $$
DECLARE
    projection_role text;
BEGIN
    projection_role := 'a6_provider_projector';
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = projection_role) THEN
        EXECUTE format(
            'REVOKE ALL ON TABLE '
            'a6_trade.provider_region_projection_receipts, '
            'a6_trade.provider_region_capacity_allocations, '
            'a6_trade.provider_region_capacity_receipts FROM %I',
            projection_role
        );
        EXECUTE format('GRANT USAGE ON SCHEMA a6_trade TO %I', projection_role);
        EXECUTE format(
            'GRANT SELECT ON TABLE '
            'a6_trade.provider_region_projection_receipts, '
            'a6_trade.provider_region_capacity_allocations, '
            'a6_trade.provider_region_capacity_receipts, '
            'a6_trade.provider_pool_generations, '
            'a6_trade.provider_cost_generations, '
            'a6_trade.provider_pool_slots, '
            'a6_trade.a6_tokens, '
            'a6_trade.enodot_account_bindings, '
            'a6_trade.a6_pin_bindings, '
            'a6_trade.provider_fast_block_outbox, '
            'a6_trade.provider_quarantines TO %I',
            projection_role
        );
        EXECUTE format(
            'GRANT INSERT (region, model, projection_type, routing_generation, '
            'cost_generation, source_hash, projection_digest, last_event_id, '
            'redis_restart_epoch, projection_state, receipt_version, projected_at, '
            'verified_at, last_error_class) ON '
            'a6_trade.provider_region_projection_receipts TO %I',
            projection_role
        );
        EXECUTE format(
            'GRANT UPDATE (routing_generation, cost_generation, source_hash, '
            'projection_digest, last_event_id, redis_restart_epoch, projection_state, '
            'receipt_version, projected_at, verified_at, last_error_class, updated_at) '
            'ON a6_trade.provider_region_projection_receipts TO %I',
            projection_role
        );
        EXECUTE format(
            'GRANT INSERT (allocation_id, enodot_binding_id, enodot_account_id, '
            'region, allocation_version, quota, occupancy, occupancy_digest, '
            'lease_cursor, redis_restart_epoch, receipt_version, projected_at, '
            'last_error_class) ON a6_trade.provider_region_capacity_receipts TO %I',
            projection_role
        );
        EXECUTE format(
            'GRANT UPDATE (allocation_id, allocation_version, quota, occupancy, '
            'occupancy_digest, lease_cursor, redis_restart_epoch, receipt_version, '
            'projected_at, last_error_class, updated_at) ON '
            'a6_trade.provider_region_capacity_receipts TO %I',
            projection_role
        );
        EXECUTE format(
            'GRANT USAGE, SELECT ON SEQUENCE '
            'a6_trade.provider_region_projection_receipts_id_seq, '
            'a6_trade.provider_region_capacity_receipts_id_seq TO %I',
            projection_role
        );
    END IF;

    projection_role := 'a6_provider_subscriber';
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = projection_role) THEN
        EXECUTE format(
            'REVOKE ALL ON TABLE '
            'a6_trade.provider_region_projection_receipts, '
            'a6_trade.provider_region_capacity_allocations, '
            'a6_trade.provider_region_capacity_receipts FROM %I',
            projection_role
        );
        EXECUTE format('GRANT USAGE ON SCHEMA a6_trade TO %I', projection_role);
        EXECUTE format(
            'GRANT SELECT ON TABLE '
            'a6_trade.provider_region_projection_receipts, '
            'a6_trade.provider_region_capacity_allocations, '
            'a6_trade.provider_region_capacity_receipts, '
            'a6_trade.provider_pool_generations, '
            'a6_trade.provider_cost_generations, '
            'a6_trade.provider_pool_slots, '
            'a6_trade.a6_tokens, '
            'a6_trade.enodot_account_bindings, '
            'a6_trade.a6_pin_bindings, '
            'a6_trade.provider_fast_block_outbox, '
            'a6_trade.provider_quarantines TO %I',
            projection_role
        );
        EXECUTE format(
            'GRANT UPDATE (subscriber_cursor, subscriber_connected, '
            'receipt_version, subscriber_verified_at, updated_at) ON '
            'a6_trade.provider_region_projection_receipts TO %I',
            projection_role
        );
        EXECUTE format(
            'GRANT UPDATE (receipt_state, receipt_version, verified_at, updated_at) '
            'ON a6_trade.provider_region_capacity_receipts TO %I',
            projection_role
        );
    END IF;
END
$$;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'a6trader') THEN
        REVOKE ALL ON TABLE a6_trade.provider_region_projection_receipts,
            a6_trade.provider_region_capacity_allocations,
            a6_trade.provider_region_capacity_receipts FROM a6trader;
        REVOKE ALL ON SEQUENCE a6_trade.provider_region_projection_receipts_id_seq,
            a6_trade.provider_region_capacity_allocations_id_seq,
            a6_trade.provider_region_capacity_receipts_id_seq FROM a6trader;
        GRANT SELECT ON a6_trade.provider_region_projection_receipts,
            a6_trade.provider_region_capacity_allocations,
            a6_trade.provider_region_capacity_receipts TO a6trader;
        GRANT INSERT ON a6_trade.provider_region_capacity_allocations TO a6trader;
        GRANT UPDATE (valid_to, updated_at)
            ON a6_trade.provider_region_capacity_allocations TO a6trader;
        GRANT USAGE, SELECT
            ON SEQUENCE a6_trade.provider_region_capacity_allocations_id_seq TO a6trader;
    END IF;
END
$$;
