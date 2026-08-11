-- Make the durable Sub2API runtime-failure bridge route-mode aware. Fixed Pin
-- keeps its consecutive-three Channel rule. Auto-pool has no Pin epoch or
-- fixed Channel identity, so the first upstream failure pauses the account and
-- requests a fresh Observer -> auto-pool generation. Quota exhaustion keeps
-- the existing liquidity control action.

ALTER TABLE a6_trade.provider_runtime_failure_events
    ALTER COLUMN channel_id DROP NOT NULL,
    ALTER COLUMN pin_assignment_epoch_id DROP NOT NULL,
    ADD COLUMN IF NOT EXISTS route_mode text NOT NULL DEFAULT 'fixed_pin',
    ADD COLUMN IF NOT EXISTS auto_pool_generation_id bigint
        REFERENCES a6_trade.provider_auto_pool_generations(id),
    ADD COLUMN IF NOT EXISTS auto_pool_subscription_id bigint;

ALTER TABLE a6_trade.provider_runtime_failure_events
    DROP CONSTRAINT IF EXISTS provider_runtime_failure_identity_positive,
    DROP CONSTRAINT IF EXISTS provider_runtime_failure_action_known;

ALTER TABLE a6_trade.provider_runtime_failure_events
    ADD CONSTRAINT provider_runtime_failure_identity_positive CHECK (
        source_error_log_id > 0 AND upstream_ordinal > 0
        AND routing_generation > 0 AND cost_generation > 0
        AND slot_no > 0 AND enodot_account_id > 0 AND a6_token_id > 0
        AND consecutive_failure_count > 0 AND attempt_count >= 0
        AND (
            (
                route_mode = 'fixed_pin'
                AND channel_id > 0 AND pin_assignment_epoch_id > 0
                AND auto_pool_generation_id IS NULL
                AND auto_pool_subscription_id IS NULL
            ) OR (
                route_mode = 'auto_pool'
                AND channel_id IS NULL AND pin_assignment_epoch_id IS NULL
                AND auto_pool_generation_id > 0
                AND auto_pool_subscription_id > 0
            )
        )
    ),
    ADD CONSTRAINT provider_runtime_failure_action_known CHECK (
        block_action IN (
            'none', 'block_liquidity', 'block_channel', 'refresh_auto_pool'
        )
    );

CREATE INDEX IF NOT EXISTS provider_runtime_failure_auto_pool_idx
    ON a6_trade.provider_runtime_failure_events (
        auto_pool_generation_id, enodot_account_id, occurred_at DESC, id DESC
    )
    WHERE route_mode = 'auto_pool';

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
                NULLIF(upstream_event->>'reason', ''),
                'upstream_runtime_error'
            ),
            '[^a-zA-Z0-9_.:-]+', '_', 'g'
        ));

        SELECT generation.generation, generation.cost_generation,
               provider_slot.slot_no, provider_slot.enodot_account_id,
               provider_slot.a6_token_id,
               provider_slot.desired_channel_id AS channel_id,
               provider_slot.pin_assignment_epoch_id,
               epoch.started_at AS epoch_started_at,
               provider_slot.route_mode,
               provider_slot.auto_pool_generation_id,
               provider_slot.auto_pool_subscription_id
        INTO slot
        FROM a6_trade.provider_pool_generations AS generation
        JOIN a6_trade.provider_pool_slots AS provider_slot
          ON provider_slot.generation_id = generation.id
         AND provider_slot.valid_to IS NULL
        LEFT JOIN a6_trade.provider_pin_assignment_epochs AS epoch
          ON epoch.id = provider_slot.pin_assignment_epoch_id
        LEFT JOIN a6_trade.provider_auto_pool_generations AS auto_generation
          ON auto_generation.id = provider_slot.auto_pool_generation_id
        WHERE generation.model = event_model
          AND generation.generation_state = 'active'
          AND provider_slot.model = generation.model
          AND provider_slot.enodot_account_id = source_account_id
          AND provider_slot.a6_token_id IS NOT NULL
          AND (
              (
                  provider_slot.route_mode = 'fixed_pin'
                  AND provider_slot.desired_channel_id IS NOT NULL
                  AND provider_slot.pin_assignment_epoch_id IS NOT NULL
              ) OR (
                  provider_slot.route_mode = 'auto_pool'
                  AND provider_slot.auto_pool_generation_id IS NOT NULL
                  AND provider_slot.auto_pool_subscription_id IS NOT NULL
                  AND auto_generation.generation_state = 'active'
              )
          )
        ORDER BY provider_slot.slot_no
        LIMIT 1;
        IF NOT FOUND THEN
            CONTINUE;
        END IF;
        IF slot.route_mode = 'fixed_pin'
           AND failure_code NOT IN (
               'insufficient_quota', 'fixed_merchant_unavailable'
           ) THEN
            CONTINUE;
        END IF;

        IF slot.route_mode = 'fixed_pin' THEN
            PERFORM 1
            FROM a6_trade.provider_pin_assignment_epochs
            WHERE id = slot.pin_assignment_epoch_id
            FOR UPDATE;
        ELSE
            PERFORM 1
            FROM a6_trade.provider_auto_pool_generations
            WHERE id = slot.auto_pool_generation_id
            FOR UPDATE;
        END IF;

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

        SELECT EXISTS (
            SELECT 1
            FROM a6_trade.provider_runtime_failure_events AS prior
            WHERE prior.model = event_model
              AND prior.enodot_account_id = source_account_id
              AND prior.block_action IN (
                  'block_liquidity', 'block_channel', 'refresh_auto_pool'
              )
              AND (
                  (
                      slot.route_mode = 'fixed_pin'
                      AND prior.route_mode = 'fixed_pin'
                      AND prior.pin_assignment_epoch_id = slot.pin_assignment_epoch_id
                  ) OR (
                      slot.route_mode = 'auto_pool'
                      AND prior.route_mode = 'auto_pool'
                      AND prior.auto_pool_generation_id = slot.auto_pool_generation_id
                  )
              )
        ) INTO prior_block_exists;

        IF slot.route_mode = 'auto_pool' THEN
            streak_count := 1;
            control_required := NOT prior_block_exists;
            block_action := CASE
                WHEN NOT control_required THEN 'none'
                WHEN failure_code = 'insufficient_quota' THEN 'block_liquidity'
                ELSE 'refresh_auto_pool'
            END;
        ELSE
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
            pin_assignment_epoch_id, route_mode,
            auto_pool_generation_id, auto_pool_subscription_id,
            consecutive_failure_count, control_required, block_action,
            processing_state, completed_at, result
        ) VALUES (
            source_error_log_id, upstream_ordinal,
            NULLIF(source_row->>'request_id', ''),
            event_model, failure_code, event_status, event_occurred_at,
            slot.generation, slot.cost_generation, slot.slot_no,
            slot.enodot_account_id, slot.a6_token_id, slot.channel_id,
            slot.pin_assignment_epoch_id, slot.route_mode,
            slot.auto_pool_generation_id, slot.auto_pool_subscription_id,
            streak_count, control_required, block_action, processing_state,
            CASE WHEN processing_state = 'ignored' THEN now() ELSE NULL END,
            CASE WHEN processing_state = 'ignored'
                 THEN jsonb_build_object('reason', 'route_generation_already_blocked')
                 ELSE '{}'::jsonb END
        )
        ON CONFLICT DO NOTHING
        RETURNING id INTO inserted_event_id;
        IF inserted_event_id IS NULL THEN
            CONTINUE;
        END IF;

        IF block_action IN (
            'block_liquidity', 'block_channel', 'refresh_auto_pool'
        ) THEN
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
                        'route_mode', slot.route_mode,
                        'event_id', inserted_event_id
                    ),
                    'a6-runtime-block:' || account_changed_id::text || ':'
                        || COALESCE(
                            slot.pin_assignment_epoch_id,
                            slot.auto_pool_generation_id
                        )::text
                )
                ON CONFLICT DO NOTHING;
            END IF;

            UPDATE a6_trade.provider_pool_slots AS provider_slot
            SET slot_state = 'REPAIRING',
                liquidity_state = CASE
                    WHEN block_action = 'block_liquidity' THEN 'UNFUNDED'
                    ELSE provider_slot.liquidity_state
                END,
                funding_eligible = CASE
                    WHEN block_action = 'block_liquidity' THEN false
                    ELSE provider_slot.funding_eligible
                END,
                desired_schedulable = false,
                observed_schedulable = false,
                state_version = state_version + 1,
                updated_at = now()
            WHERE provider_slot.model = event_model
              AND provider_slot.routing_generation = slot.generation
              AND provider_slot.slot_no = slot.slot_no
              AND provider_slot.enodot_account_id = source_account_id
              AND provider_slot.valid_to IS NULL;

            UPDATE a6_trade.provider_inventory AS inventory
            SET inventory_state = 'REPAIRING',
                liquidity_state = CASE
                    WHEN block_action = 'block_liquidity' THEN 'UNFUNDED'
                    ELSE inventory.liquidity_state
                END,
                funding_eligible = CASE
                    WHEN block_action = 'block_liquidity' THEN false
                    ELSE inventory.funding_eligible
                END,
                schedulable = false,
                state_version = state_version + 1,
                updated_at = now()
            WHERE inventory.model = event_model
              AND inventory.enodot_account_id = source_account_id
              AND inventory.a6_token_id = slot.a6_token_id;

            IF slot.route_mode = 'auto_pool' THEN
                UPDATE a6_trade.provider_auto_pool_accounts AS auto_account
                SET account_state = CASE
                        WHEN block_action = 'block_liquidity' THEN 'unfunded'
                        ELSE 'failed'
                    END,
                    schedulable_confirmed = false,
                    error_class = failure_code,
                    completed_at = now(),
                    updated_at = now()
                WHERE auto_account.generation_id = slot.auto_pool_generation_id
                  AND auto_account.enodot_account_id = source_account_id
                  AND auto_account.account_state = 'ready';
            END IF;
        END IF;

        PERFORM pg_notify(
            'a6_provider_runtime_failure',
            jsonb_build_object(
                'event_id', inserted_event_id,
                'model', event_model,
                'route_mode', slot.route_mode,
                'block_action', block_action
            )::text
        );
    END LOOP;
    RETURN NEW;
EXCEPTION WHEN OTHERS THEN
    RAISE WARNING 'a6 provider runtime failure bridge skipped log %: % %',
        source_row->>'id', SQLSTATE, SQLERRM;
    RETURN NEW;
END
$$;

REVOKE ALL ON FUNCTION a6_trade.capture_provider_runtime_failures() FROM PUBLIC;
