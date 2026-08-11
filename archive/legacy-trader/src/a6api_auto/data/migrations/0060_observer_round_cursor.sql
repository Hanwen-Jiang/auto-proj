CREATE TABLE IF NOT EXISTS a6_trade.a6_market_observer_round_cursors (
    model text PRIMARY KEY,
    next_round_index bigint NOT NULL DEFAULT 0,
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT a6_market_observer_round_cursor_nonnegative CHECK (
        next_round_index >= 0
    )
);

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'a6trader') THEN
        GRANT SELECT, INSERT, UPDATE
            ON a6_trade.a6_market_observer_round_cursors TO a6trader;
    END IF;
END
$$;
