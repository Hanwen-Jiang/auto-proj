DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'a6trader') THEN
        GRANT DELETE ON a6_trade.a6_market_quotes TO a6trader;
    END IF;
END
$$;
