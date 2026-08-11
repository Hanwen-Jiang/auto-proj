-- Bridge production Sub2API failures into the durable Provider control plane.
-- The trigger performs only bounded local writes. Remote Pin/top-up work is
-- resumed by the listener after commit.

CREATE TABLE IF NOT EXISTS a6_trade.provider_runtime_failure_events (
    id bigserial PRIMARY KEY,
    source_error_log_id bigint NOT NULL,
    upstream_ordinal integer NOT NULL,
    source_request_id text,
    model text NOT NULL,
    failure_code text NOT NULL,
    http_status integer,
    occurred_at timestamptz NOT NULL,
    routing_generation bigint NOT NULL,
    cost_generation bigint NOT NULL,
    slot_no integer NOT NULL,
    enodot_account_id bigint NOT NULL,
    a6_token_id bigint NOT NULL,
    channel_id bigint NOT NULL,
    pin_assignment_epoch_id bigint NOT NULL,
    consecutive_failure_count integer NOT NULL DEFAULT 1,
    control_required boolean NOT NULL DEFAULT true,
    block_action text NOT NULL DEFAULT 'none',
    processing_state text NOT NULL DEFAULT 'pending',
    attempt_count integer NOT NULL DEFAULT 0,
    claim_token text,
    claimed_by text,
    claimed_at timestamptz,
    next_attempt_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz,
    last_error_class text,
    result jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (source_error_log_id, upstream_ordinal),
    CONSTRAINT provider_runtime_failure_identity_positive CHECK (
        source_error_log_id > 0 AND upstream_ordinal > 0
        AND routing_generation > 0 AND cost_generation > 0
        AND slot_no > 0 AND enodot_account_id > 0
        AND a6_token_id > 0 AND channel_id > 0
        AND pin_assignment_epoch_id > 0
        AND consecutive_failure_count > 0 AND attempt_count >= 0
    ),
    CONSTRAINT provider_runtime_failure_http_status_valid CHECK (
        http_status IS NULL OR http_status BETWEEN 100 AND 599
    ),
    CONSTRAINT provider_runtime_failure_action_known CHECK (
        block_action IN ('none', 'block_liquidity', 'block_channel')
    ),
    CONSTRAINT provider_runtime_failure_state_known CHECK (
        processing_state IN (
            'pending', 'running', 'retry', 'completed',
            'ignored', 'superseded', 'failed'
        )
    ),
    CONSTRAINT provider_runtime_failure_claim_complete CHECK (
        (
            processing_state = 'running'
            AND claim_token IS NOT NULL
            AND claimed_by IS NOT NULL
            AND claimed_at IS NOT NULL
        ) OR (
            processing_state <> 'running'
            AND claim_token IS NULL
            AND claimed_by IS NULL
            AND claimed_at IS NULL
        )
    ),
    CONSTRAINT provider_runtime_failure_completion_complete CHECK (
        (processing_state IN ('completed', 'ignored', 'superseded', 'failed'))
            = (completed_at IS NOT NULL)
    ),
    CONSTRAINT provider_runtime_failure_result_object CHECK (
        jsonb_typeof(result) = 'object'
    )
);

CREATE INDEX IF NOT EXISTS provider_runtime_failure_claim_idx
    ON a6_trade.provider_runtime_failure_events (
        next_attempt_at, occurred_at, id
    )
    WHERE processing_state IN ('pending', 'retry', 'running');

CREATE INDEX IF NOT EXISTS provider_runtime_failure_epoch_idx
    ON a6_trade.provider_runtime_failure_events (
        pin_assignment_epoch_id, occurred_at DESC, id DESC
    );

CREATE INDEX IF NOT EXISTS provider_pool_runtime_account_idx
    ON a6_trade.provider_pool_slots (model, enodot_account_id)
    WHERE valid_to IS NULL AND enodot_account_id IS NOT NULL;

CREATE OR REPLACE FUNCTION a6_trade.try_parse_jsonb(value text)
RETURNS jsonb
LANGUAGE plpgsql
IMMUTABLE
STRICT
AS $$
BEGIN
    RETURN value::jsonb;
EXCEPTION WHEN invalid_text_representation THEN
    RETURN NULL;
END
$$;

CREATE OR REPLACE FUNCTION a6_trade.capture_provider_runtime_failures()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, a6_trade
AS $$
#variable_conflict use_variable
DECLARE
    source_row jsonb := to_jsonb(NEW);
    upstream_event jsonb;
    detail jsonb;
    slot record;
    upstream_ordinal integer;
    source_error_log_id bigint;
    source_account_id bigint;
    event_model text;
    failure_code text;
    status_text text;
    event_status integer;
    event_occurred_at timestamptz;
    latest_success_at timestamptz;
    streak_started_at timestamptz;
    prior_block_exists boolean;
    streak_count integer;
    control_required boolean;
    block_action text;
    processing_state text;
    inserted_event_id bigint;
    account_changed_id bigint;
BEGIN
    IF source_row->'upstream_errors' IS NULL
       OR jsonb_typeof(source_row->'upstream_errors') <> 'array' THEN
        RETURN NEW;
    END IF;
    source_error_log_id := NULLIF(source_row->>'id', '')::bigint;
    event_model := COALESCE(
        NULLIF(btrim(source_row->>'requested_model'), ''),
        NULLIF(btrim(source_row->>'model'), '')
    );
    IF source_error_log_id IS NULL OR event_model IS NULL THEN
        RETURN NEW;
    END IF;

    FOR upstream_event, upstream_ordinal IN
        SELECT item.value, item.ordinality::integer
        FROM jsonb_array_elements(source_row->'upstream_errors')
             WITH ORDINALITY AS item(value, ordinality)
    LOOP
        IF COALESCE(upstream_event->>'account_id', '') !~ '^[1-9][0-9]*$' THEN
            CONTINUE;
        END IF;
        source_account_id := (upstream_event->>'account_id')::bigint;
        detail := CASE
            WHEN jsonb_typeof(upstream_event->'detail') = 'object'
            THEN upstream_event->'detail'
            ELSE a6_trade.try_parse_jsonb(upstream_event->>'detail')
        END;
        failure_code := lower(regexp_replace(
            COALESCE(
                NULLIF(detail #>> '{error,code}', ''),
                NULLIF(detail #>> '{error,type}', ''),
                NULLIF(upstream_event->>'reason', '')
            ),
            '[^a-zA-Z0-9_.:-]+', '_', 'g'
        ));
        IF failure_code IS NULL
           OR failure_code NOT IN ('insufficient_quota', 'fixed_merchant_unavailable') THEN
            CONTINUE;
        END IF;

        SELECT generation.generation, generation.cost_generation,
               provider_slot.slot_no, provider_slot.enodot_account_id,
               provider_slot.a6_token_id,
               provider_slot.desired_channel_id AS channel_id,
               provider_slot.pin_assignment_epoch_id,
               epoch.started_at AS epoch_started_at
        INTO slot
        FROM a6_trade.provider_pool_generations AS generation
        JOIN a6_trade.provider_pool_slots AS provider_slot
          ON provider_slot.generation_id = generation.id
         AND provider_slot.valid_to IS NULL
        JOIN a6_trade.provider_pin_assignment_epochs AS epoch
          ON epoch.id = provider_slot.pin_assignment_epoch_id
        WHERE generation.model = event_model
          AND generation.generation_state = 'active'
          AND provider_slot.model = generation.model
          AND provider_slot.enodot_account_id = source_account_id
          AND provider_slot.a6_token_id IS NOT NULL
          AND provider_slot.desired_channel_id IS NOT NULL
        ORDER BY provider_slot.slot_no
        LIMIT 1;
        IF NOT FOUND THEN
            CONTINUE;
        END IF;

        -- Serialize sequence allocation and streak decisions for this Pin epoch.
        PERFORM 1
        FROM a6_trade.provider_pin_assignment_epochs
        WHERE id = slot.pin_assignment_epoch_id
        FOR UPDATE;

        status_text := upstream_event->>'upstream_status_code';
        event_status := CASE
            WHEN COALESCE(status_text, '') ~ '^[1-5][0-9][0-9]$'
            THEN status_text::integer
            ELSE NULL
        END;
        event_occurred_at := CASE
            WHEN COALESCE(upstream_event->>'at_unix_ms', '') ~ '^[0-9]{13}$'
            THEN to_timestamp((upstream_event->>'at_unix_ms')::numeric / 1000)
            ELSE COALESCE((source_row->>'created_at')::timestamptz, now())
        END;

        SELECT max(usage.created_at)
        INTO latest_success_at
        FROM public.usage_logs AS usage
        WHERE usage.account_id = source_account_id
          AND usage.model = event_model
          AND usage.created_at >= slot.epoch_started_at;
        streak_started_at := greatest(
            slot.epoch_started_at,
            COALESCE(latest_success_at, slot.epoch_started_at)
        );

        SELECT EXISTS (
            SELECT 1
            FROM a6_trade.provider_runtime_failure_events AS prior
            WHERE prior.pin_assignment_epoch_id = slot.pin_assignment_epoch_id
              AND prior.occurred_at > streak_started_at
              AND prior.block_action IN ('block_liquidity', 'block_channel')
        ) INTO prior_block_exists;

        IF failure_code = 'insufficient_quota' THEN
            streak_count := 1;
            control_required := NOT prior_block_exists;
            block_action := CASE
                WHEN control_required THEN 'block_liquidity'
                ELSE 'none'
            END;
        ELSE
            SELECT count(*) + 1
            INTO streak_count
            FROM a6_trade.provider_runtime_failure_events AS prior
            WHERE prior.pin_assignment_epoch_id = slot.pin_assignment_epoch_id
              AND prior.failure_code = 'fixed_merchant_unavailable'
              AND prior.occurred_at > streak_started_at;
            control_required := NOT prior_block_exists;
            block_action := CASE
                WHEN control_required AND streak_count >= 3 THEN 'block_channel'
                ELSE 'none'
            END;
        END IF;
        processing_state := CASE
            WHEN control_required THEN 'pending'
            ELSE 'ignored'
        END;

        INSERT INTO a6_trade.provider_runtime_failure_events (
            source_error_log_id, upstream_ordinal, source_request_id,
            model, failure_code, http_status, occurred_at,
            routing_generation, cost_generation, slot_no,
            enodot_account_id, a6_token_id, channel_id,
            pin_assignment_epoch_id, consecutive_failure_count,
            control_required, block_action, processing_state,
            completed_at, result
        ) VALUES (
            source_error_log_id, upstream_ordinal,
            NULLIF(source_row->>'request_id', ''),
            event_model, failure_code, event_status, event_occurred_at,
            slot.generation, slot.cost_generation, slot.slot_no,
            slot.enodot_account_id, slot.a6_token_id, slot.channel_id,
            slot.pin_assignment_epoch_id, streak_count,
            control_required, block_action, processing_state,
            CASE WHEN processing_state = 'ignored' THEN now() ELSE NULL END,
            CASE WHEN processing_state = 'ignored'
                 THEN jsonb_build_object('reason', 'epoch_already_blocked')
                 ELSE '{}'::jsonb END
        )
        ON CONFLICT DO NOTHING
        RETURNING id INTO inserted_event_id;
        IF inserted_event_id IS NULL THEN
            CONTINUE;
        END IF;

        IF block_action IN ('block_liquidity', 'block_channel') THEN
            account_changed_id := NULL;
            UPDATE public.accounts AS account
            SET schedulable = false,
                error_message = 'a6 runtime block: ' || failure_code,
                updated_at = now()
            WHERE account.id = source_account_id
              AND account.deleted_at IS NULL
              AND account.schedulable IS TRUE
            RETURNING account.id INTO account_changed_id;

            IF account_changed_id IS NOT NULL THEN
                INSERT INTO public.scheduler_outbox (
                    event_type, account_id, group_id, payload, dedup_key
                ) VALUES (
                    'account_changed', account_changed_id, NULL,
                    jsonb_build_object(
                        'source', 'a6_provider_runtime_failure',
                        'failure_code', failure_code,
                        'event_id', inserted_event_id
                    ),
                    'a6-runtime-block:' || account_changed_id::text || ':'
                        || slot.pin_assignment_epoch_id::text
                )
                ON CONFLICT DO NOTHING;
            END IF;

        END IF;

        IF block_action = 'block_liquidity' THEN
            UPDATE a6_trade.provider_inventory AS inventory
            SET inventory_state = 'REPAIRING',
                liquidity_state = 'UNFUNDED',
                funding_eligible = false,
                schedulable = false,
                state_version = state_version + 1,
                updated_at = now()
            WHERE inventory.model = event_model
              AND inventory.enodot_account_id = source_account_id
              AND inventory.a6_token_id = slot.a6_token_id;
        END IF;

        PERFORM pg_notify(
            'a6_provider_runtime_failure',
            jsonb_build_object(
                'event_id', inserted_event_id,
                'model', event_model,
                'block_action', block_action
            )::text
        );
    END LOOP;
    RETURN NEW;
EXCEPTION WHEN OTHERS THEN
    -- Request serving must not be coupled to control-plane observability errors.
    RAISE WARNING 'a6 provider runtime failure bridge skipped log %: % %',
        source_row->>'id', SQLSTATE, SQLERRM;
    RETURN NEW;
END
$$;

DO $$
BEGIN
    IF to_regclass('public.ops_error_logs') IS NOT NULL
       AND to_regclass('public.accounts') IS NOT NULL
       AND to_regclass('public.scheduler_outbox') IS NOT NULL
       AND to_regclass('public.usage_logs') IS NOT NULL THEN
        DROP TRIGGER IF EXISTS a6_provider_runtime_failure_bridge
            ON public.ops_error_logs;
        CREATE TRIGGER a6_provider_runtime_failure_bridge
        AFTER INSERT ON public.ops_error_logs
        FOR EACH ROW
        EXECUTE FUNCTION a6_trade.capture_provider_runtime_failures();
    END IF;
END
$$;

REVOKE ALL ON FUNCTION a6_trade.try_parse_jsonb(text) FROM PUBLIC;
REVOKE ALL ON FUNCTION a6_trade.capture_provider_runtime_failures() FROM PUBLIC;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'a6trader') THEN
        GRANT SELECT, INSERT, UPDATE
            ON a6_trade.provider_runtime_failure_events TO a6trader;
        GRANT USAGE, SELECT
            ON SEQUENCE a6_trade.provider_runtime_failure_events_id_seq TO a6trader;
    END IF;
END
$$;
