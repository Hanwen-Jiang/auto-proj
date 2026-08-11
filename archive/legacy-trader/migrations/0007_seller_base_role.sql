ALTER TABLE a6_trade.a6_merchants
    ADD COLUMN IF NOT EXISTS role_revision bigint NOT NULL DEFAULT 0;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM a6_trade.a6_merchants
        WHERE cardinality(
            ARRAY(
                SELECT role
                FROM unnest(roles) AS role
                WHERE role IN ('provider', 'observer', 'probe')
            )
        ) > 1
    ) THEN
        RAISE EXCEPTION 'a6_merchant_duty_roles_conflict';
    END IF;
END
$$;

UPDATE a6_trade.a6_merchants
SET roles = ARRAY['seller']::text[] || CASE
        WHEN roles @> ARRAY['provider']::text[] THEN ARRAY['provider']::text[]
        WHEN roles @> ARRAY['observer']::text[] THEN ARRAY['observer']::text[]
        WHEN roles @> ARRAY['probe']::text[] THEN ARRAY['probe']::text[]
        ELSE ARRAY[]::text[]
    END,
    role_revision = role_revision + 1,
    updated_at = now()
WHERE roles IS DISTINCT FROM (
    ARRAY['seller']::text[] || CASE
        WHEN roles @> ARRAY['provider']::text[] THEN ARRAY['provider']::text[]
        WHEN roles @> ARRAY['observer']::text[] THEN ARRAY['observer']::text[]
        WHEN roles @> ARRAY['probe']::text[] THEN ARRAY['probe']::text[]
        ELSE ARRAY[]::text[]
    END
);

ALTER TABLE a6_trade.a6_merchants
    ALTER COLUMN roles SET DEFAULT ARRAY['seller']::text[];

ALTER TABLE a6_trade.a6_merchants
    DROP CONSTRAINT IF EXISTS a6_merchants_roles_base_seller;
ALTER TABLE a6_trade.a6_merchants
    ADD CONSTRAINT a6_merchants_roles_base_seller CHECK (
        roles[1] = 'seller'
        AND cardinality(roles) BETWEEN 1 AND 2
        AND roles <@ ARRAY['seller', 'provider', 'observer', 'probe']::text[]
        AND (
            cardinality(roles) = 1
            OR roles[2] IN ('provider', 'observer', 'probe')
        )
    );

ALTER TABLE a6_trade.a6_merchants
    DROP CONSTRAINT IF EXISTS a6_merchants_role_revision_nonnegative;
ALTER TABLE a6_trade.a6_merchants
    ADD CONSTRAINT a6_merchants_role_revision_nonnegative CHECK (role_revision >= 0);
