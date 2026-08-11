-- A Channel failure streak belongs to one successful Pin assignment, not to a
-- routing/cost generation. Terminal sequence allocation is serialized by the
-- epoch row so restarts and concurrent reporters preserve exact ordering.

CREATE TABLE IF NOT EXISTS a6_trade.provider_pin_assignment_epochs (
    id bigserial PRIMARY KEY,
    model text NOT NULL,
    slot_no integer NOT NULL,
    enodot_account_id bigint NOT NULL,
    a6_token_id bigint NOT NULL,
    channel_id bigint NOT NULL,
    started_routing_generation bigint NOT NULL,
    started_cost_generation bigint NOT NULL,
    ended_routing_generation bigint,
    ended_cost_generation bigint,
    last_terminal_seq bigint NOT NULL DEFAULT 0,
    started_at timestamptz NOT NULL DEFAULT now(),
    ended_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT provider_pin_epoch_identity_positive CHECK (
        slot_no > 0 AND enodot_account_id > 0 AND a6_token_id > 0
        AND channel_id > 0 AND started_routing_generation > 0
        AND started_cost_generation > 0 AND last_terminal_seq >= 0
        AND (ended_routing_generation IS NULL OR ended_routing_generation > 0)
        AND (ended_cost_generation IS NULL OR ended_cost_generation > 0)
    ),
    CONSTRAINT provider_pin_epoch_end_complete CHECK (
        (ended_at IS NULL AND ended_routing_generation IS NULL
            AND ended_cost_generation IS NULL)
        OR
        (ended_at IS NOT NULL AND ended_routing_generation IS NOT NULL
            AND ended_cost_generation IS NOT NULL)
    ),
    UNIQUE (model, slot_no, started_routing_generation)
);

CREATE UNIQUE INDEX IF NOT EXISTS provider_pin_epoch_current_idx
    ON a6_trade.provider_pin_assignment_epochs (model, slot_no)
    WHERE ended_at IS NULL;

ALTER TABLE a6_trade.provider_pool_slots
    ADD COLUMN IF NOT EXISTS pin_assignment_epoch_id bigint
        REFERENCES a6_trade.provider_pin_assignment_epochs(id);

INSERT INTO a6_trade.provider_pin_assignment_epochs (
    model, slot_no, enodot_account_id, a6_token_id, channel_id,
    started_routing_generation, started_cost_generation,
    ended_routing_generation, ended_cost_generation, started_at, ended_at
)
SELECT slot.model, slot.slot_no, slot.enodot_account_id, slot.a6_token_id,
       slot.desired_channel_id, slot.routing_generation, slot.cost_generation,
       CASE WHEN slot.valid_to IS NULL THEN NULL ELSE slot.routing_generation END,
       CASE WHEN slot.valid_to IS NULL THEN NULL ELSE slot.cost_generation END,
       slot.valid_from, slot.valid_to
FROM a6_trade.provider_pool_slots slot
WHERE slot.slot_state <> 'VACANT'
  AND slot.enodot_account_id IS NOT NULL
  AND slot.a6_token_id IS NOT NULL
  AND slot.desired_channel_id IS NOT NULL
  AND slot.pin_assignment_epoch_id IS NULL
ON CONFLICT (model, slot_no, started_routing_generation) DO NOTHING;

UPDATE a6_trade.provider_pool_slots slot
SET pin_assignment_epoch_id = epoch.id
FROM a6_trade.provider_pin_assignment_epochs epoch
WHERE slot.pin_assignment_epoch_id IS NULL
  AND epoch.model = slot.model
  AND epoch.slot_no = slot.slot_no
  AND epoch.started_routing_generation = slot.routing_generation;

ALTER TABLE a6_trade.provider_attempts
    ADD COLUMN IF NOT EXISTS pin_assignment_epoch_id bigint
        REFERENCES a6_trade.provider_pin_assignment_epochs(id),
    ADD COLUMN IF NOT EXISTS terminal_seq bigint;

UPDATE a6_trade.provider_attempts attempt
SET pin_assignment_epoch_id = slot.pin_assignment_epoch_id
FROM a6_trade.provider_pool_slots slot
WHERE attempt.pin_assignment_epoch_id IS NULL
  AND slot.model = attempt.model
  AND slot.routing_generation = attempt.routing_generation
  AND slot.slot_no = attempt.slot_no
  AND slot.enodot_account_id = attempt.enodot_account_id
  AND slot.a6_token_id = attempt.a6_token_id
  AND slot.desired_channel_id = attempt.channel_id;

WITH ranked AS (
    SELECT id,
           row_number() OVER (
               PARTITION BY pin_assignment_epoch_id
               ORDER BY COALESCE(finished_at, started_at), id
           ) AS terminal_seq
    FROM a6_trade.provider_attempts
    WHERE pin_assignment_epoch_id IS NOT NULL
      AND state IN ('failed', 'won', 'lost', 'cancelled')
      AND finished_at IS NOT NULL
)
UPDATE a6_trade.provider_attempts attempt
SET terminal_seq = ranked.terminal_seq
FROM ranked
WHERE attempt.id = ranked.id
  AND attempt.terminal_seq IS NULL;

UPDATE a6_trade.provider_pin_assignment_epochs epoch
SET last_terminal_seq = terminal.maximum_terminal_seq,
    updated_at = now()
FROM (
    SELECT pin_assignment_epoch_id, max(terminal_seq) AS maximum_terminal_seq
    FROM a6_trade.provider_attempts
    WHERE pin_assignment_epoch_id IS NOT NULL AND terminal_seq IS NOT NULL
    GROUP BY pin_assignment_epoch_id
) terminal
WHERE epoch.id = terminal.pin_assignment_epoch_id
  AND epoch.last_terminal_seq < terminal.maximum_terminal_seq;

CREATE OR REPLACE FUNCTION a6_trade.assign_provider_attempt_epoch_terminal_seq()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    next_terminal_seq bigint;
BEGIN
    IF NEW.pin_assignment_epoch_id IS NULL
       AND NEW.enodot_account_id IS NOT NULL
       AND NEW.a6_token_id IS NOT NULL
       AND NEW.channel_id IS NOT NULL THEN
        SELECT slot.pin_assignment_epoch_id
        INTO NEW.pin_assignment_epoch_id
        FROM a6_trade.provider_pool_slots slot
        WHERE slot.model = NEW.model
          AND slot.routing_generation = NEW.routing_generation
          AND slot.slot_no = NEW.slot_no
          AND slot.enodot_account_id = NEW.enodot_account_id
          AND slot.a6_token_id = NEW.a6_token_id
          AND slot.desired_channel_id = NEW.channel_id;
    END IF;

    IF NEW.state IN ('failed', 'won', 'lost', 'cancelled')
       AND NEW.finished_at IS NOT NULL
       AND NEW.terminal_seq IS NULL THEN
        IF NEW.pin_assignment_epoch_id IS NULL THEN
            RAISE EXCEPTION 'provider_attempt_pin_assignment_epoch_missing';
        END IF;
        UPDATE a6_trade.provider_pin_assignment_epochs
        SET last_terminal_seq = last_terminal_seq + 1, updated_at = now()
        WHERE id = NEW.pin_assignment_epoch_id
        RETURNING last_terminal_seq INTO next_terminal_seq;
        IF next_terminal_seq IS NULL THEN
            RAISE EXCEPTION 'provider_attempt_pin_assignment_epoch_invalid';
        END IF;
        NEW.terminal_seq := next_terminal_seq;
    END IF;
    RETURN NEW;
END
$$;

DROP TRIGGER IF EXISTS provider_attempt_assign_epoch_terminal_seq
    ON a6_trade.provider_attempts;
CREATE TRIGGER provider_attempt_assign_epoch_terminal_seq
BEFORE INSERT OR UPDATE OF state, finished_at
ON a6_trade.provider_attempts
FOR EACH ROW
EXECUTE FUNCTION a6_trade.assign_provider_attempt_epoch_terminal_seq();

DROP INDEX IF EXISTS a6_trade.provider_attempt_pin_terminal_streak_idx;
CREATE INDEX provider_attempt_pin_terminal_streak_idx
    ON a6_trade.provider_attempts (
        pin_assignment_epoch_id, terminal_seq DESC
    )
    WHERE pin_assignment_epoch_id IS NOT NULL
      AND terminal_seq IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS provider_attempt_epoch_terminal_seq_idx
    ON a6_trade.provider_attempts (pin_assignment_epoch_id, terminal_seq)
    WHERE pin_assignment_epoch_id IS NOT NULL AND terminal_seq IS NOT NULL;

ALTER TABLE a6_trade.provider_attempts
    DROP CONSTRAINT IF EXISTS provider_attempt_failure_action_known;
ALTER TABLE a6_trade.provider_attempts
    ADD CONSTRAINT provider_attempt_failure_action_known CHECK (
        failure_control_action IS NULL
        OR failure_control_action IN (
            'none', 'repin_immediately', 'probe_then_repin',
            'repair_liquidity', 'isolate_path', 'repair_internal', 'open_incident'
        )
    );

ALTER TABLE a6_trade.provider_quarantines
    DROP CONSTRAINT IF EXISTS provider_quarantine_action_known;
ALTER TABLE a6_trade.provider_quarantines
    ADD CONSTRAINT provider_quarantine_action_known CHECK (
        control_action IS NULL
        OR control_action IN (
            'none', 'repin_immediately', 'probe_then_repin',
            'repair_liquidity', 'isolate_path', 'repair_internal', 'open_incident'
        )
    );

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'a6trader') THEN
        GRANT SELECT, INSERT, UPDATE
            ON a6_trade.provider_pin_assignment_epochs TO a6trader;
        GRANT USAGE, SELECT
            ON SEQUENCE a6_trade.provider_pin_assignment_epochs_id_seq TO a6trader;
    END IF;
END
$$;
