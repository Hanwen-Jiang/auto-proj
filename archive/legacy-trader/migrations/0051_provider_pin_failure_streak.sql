-- A Provider Pin changes channel only after a durable streak of canonical
-- channel-owned failures.  Zero threshold means the attempt has not yet been
-- evaluated by the authoritative failure controller.

ALTER TABLE a6_trade.provider_attempts
    ADD COLUMN IF NOT EXISTS failure_streak_count integer NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS failure_streak_threshold integer NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS failure_streak_triggered boolean NOT NULL DEFAULT false;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'provider_attempt_failure_streak_valid'
          AND conrelid = 'a6_trade.provider_attempts'::regclass
    ) THEN
        ALTER TABLE a6_trade.provider_attempts
            ADD CONSTRAINT provider_attempt_failure_streak_valid CHECK (
                failure_streak_count >= 0
                AND failure_streak_threshold BETWEEN 0 AND 100
                AND (
                    failure_streak_threshold > 0
                    OR (
                        failure_streak_count = 0
                        AND NOT failure_streak_triggered
                    )
                )
                AND (
                    NOT failure_streak_triggered
                    OR (
                        failure_owner = 'provider'
                        AND failure_control_action = 'repin_immediately'
                        AND failure_quarantine_scope = 'channel'
                        AND failure_streak_count >= failure_streak_threshold
                    )
                )
            );
    END IF;
END
$$;

CREATE INDEX IF NOT EXISTS provider_attempt_pin_terminal_streak_idx
    ON a6_trade.provider_attempts (
        model, routing_generation, slot_no, enodot_account_id,
        a6_token_id, channel_id, finished_at DESC, id DESC
    )
    WHERE state IN ('failed', 'won', 'lost', 'cancelled')
      AND finished_at IS NOT NULL;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'a6trader') THEN
        GRANT SELECT, INSERT, UPDATE ON a6_trade.provider_attempts TO a6trader;
    END IF;
END
$$;
