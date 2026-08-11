UPDATE a6_trade.a6_merchants
SET roles = ARRAY['seller']::text[] || CASE
        WHEN roles @> ARRAY['provider']::text[] THEN ARRAY['provider']::text[]
        WHEN roles @> ARRAY['observer']::text[] THEN ARRAY['observer']::text[]
        ELSE ARRAY[]::text[]
    END,
    role_revision = role_revision + 1,
    updated_at = now()
WHERE roles @> ARRAY['probe']::text[];

ALTER TABLE a6_trade.a6_merchants
    DROP CONSTRAINT IF EXISTS a6_merchants_roles_base_seller;
ALTER TABLE a6_trade.a6_merchants
    ADD CONSTRAINT a6_merchants_roles_base_seller CHECK (
        roles[1] = 'seller'
        AND cardinality(roles) BETWEEN 1 AND 2
        AND roles <@ ARRAY['seller', 'provider', 'observer']::text[]
        AND (
            cardinality(roles) = 1
            OR roles[2] IN ('provider', 'observer')
        )
    );
