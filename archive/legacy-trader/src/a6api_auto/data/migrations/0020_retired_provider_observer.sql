ALTER TABLE a6_trade.a6_merchants
    DROP CONSTRAINT IF EXISTS a6_merchants_roles_operational_shape;
ALTER TABLE a6_trade.a6_merchants
    ADD CONSTRAINT a6_merchants_roles_operational_shape CHECK (
        roles = ARRAY['seller']::text[]
        OR roles = ARRAY['provider']::text[]
        OR roles = ARRAY['provider', 'observer']::text[]
        OR roles = ARRAY['seller', 'provider']::text[]
        OR roles = ARRAY['seller', 'observer']::text[]
    );

UPDATE a6_trade.a6_merchants merchant
SET roles = ARRAY['provider', 'observer']::text[],
    role_revision = role_revision + 1,
    updated_at = now()
FROM a6_trade.a6_seller_retirement_plans retirement
WHERE retirement.merchant_id = merchant.id
  AND retirement.completed_at IS NOT NULL
  AND merchant.roles = ARRAY['provider']::text[];

INSERT INTO a6_trade.a6_seller_retirement_events (
    merchant_id, event_type, from_state, to_state, details
)
SELECT
    retirement.merchant_id,
    'retired_observer_duty_added',
    'completed',
    'completed',
    jsonb_build_object(
        'roles_before', ARRAY['provider']::text[],
        'roles_after', ARRAY['provider', 'observer']::text[]
    )
FROM a6_trade.a6_seller_retirement_plans retirement
WHERE retirement.completed_at IS NOT NULL
  AND NOT EXISTS (
      SELECT 1
      FROM a6_trade.a6_seller_retirement_events event
      WHERE event.merchant_id = retirement.merchant_id
        AND event.event_type = 'retired_observer_duty_added'
  );

UPDATE a6_trade.a6_channel_growth_policies policy
SET enabled = false,
    callback_enabled = false,
    updated_at = now()
FROM a6_trade.a6_managed_listing_bindings listing
JOIN a6_trade.a6_seller_retirement_plans retirement
  ON retirement.merchant_id = listing.merchant_id
WHERE policy.listing_binding_id = listing.id
  AND retirement.completed_at IS NOT NULL;
