-- Keep Listing health management attached to the Seller lifecycle.  The original
-- 0015 backfill only saw Listings that existed on the day it ran, so every later
-- Seller was silently omitted from callback growth.

CREATE OR REPLACE FUNCTION a6_trade.sync_listing_growth_policy()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    seller_active boolean := false;
BEGIN
    SELECT merchant.roles @> ARRAY['seller']::text[]
    INTO seller_active
    FROM a6_trade.a6_merchants merchant
    WHERE merchant.id = NEW.merchant_id;

    IF NEW.control_state = 'verified'
       AND NEW.a6_listing_id IS NOT NULL
       AND seller_active THEN
        INSERT INTO a6_trade.a6_channel_growth_policies (
            listing_binding_id, enabled, recycle_enabled, callback_enabled,
            degraded_success_rate
        ) VALUES (NEW.id, true, true, true, 8000)
        ON CONFLICT (listing_binding_id) DO NOTHING;
    ELSIF NEW.control_state = 'retired' THEN
        UPDATE a6_trade.a6_channel_growth_policies
        SET enabled = false,
            recycle_enabled = false,
            callback_enabled = false,
            updated_at = now()
        WHERE listing_binding_id = NEW.id;
    END IF;
    RETURN NEW;
END
$$;

DROP TRIGGER IF EXISTS a6_managed_listing_growth_policy_sync
    ON a6_trade.a6_managed_listing_bindings;
CREATE TRIGGER a6_managed_listing_growth_policy_sync
AFTER INSERT OR UPDATE OF control_state, a6_listing_id
ON a6_trade.a6_managed_listing_bindings
FOR EACH ROW
EXECUTE FUNCTION a6_trade.sync_listing_growth_policy();

-- Backfill all current Seller Listings and explicitly repair the four pilot policies
-- whose callback flag was left off by the retirement-era rollout.
INSERT INTO a6_trade.a6_channel_growth_policies (
    listing_binding_id, enabled, recycle_enabled, callback_enabled,
    degraded_success_rate
)
SELECT listing.id, true, true, true, 8000
FROM a6_trade.a6_managed_listing_bindings listing
JOIN a6_trade.a6_merchants merchant ON merchant.id = listing.merchant_id
WHERE listing.control_state = 'verified'
  AND listing.a6_listing_id IS NOT NULL
  AND merchant.roles @> ARRAY['seller']::text[]
ON CONFLICT (listing_binding_id) DO UPDATE
SET enabled = true,
    recycle_enabled = true,
    callback_enabled = true,
    degraded_success_rate = 8000,
    suppressed_until = NULL,
    suppression_reason = NULL,
    updated_at = now();

-- A retired or force-archived merchant is no longer a Seller target.  Its A6 account
-- may still supply Provider/Observer duty and a callback Token, but its old Listing
-- must not be recycled or boosted.
UPDATE a6_trade.a6_channel_growth_policies policy
SET enabled = false,
    recycle_enabled = false,
    callback_enabled = false,
    updated_at = now()
FROM a6_trade.a6_managed_listing_bindings listing
JOIN a6_trade.a6_merchants merchant ON merchant.id = listing.merchant_id
WHERE policy.listing_binding_id = listing.id
  AND (
      listing.control_state = 'retired'
      OR NOT (merchant.roles @> ARRAY['seller']::text[])
  );

CREATE OR REPLACE FUNCTION a6_trade.sync_merchant_growth_duties()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.roles @> ARRAY['seller']::text[] THEN
        INSERT INTO a6_trade.a6_channel_growth_policies (
            listing_binding_id, enabled, recycle_enabled, callback_enabled,
            degraded_success_rate
        )
        SELECT listing.id, true, true, true, 8000
        FROM a6_trade.a6_managed_listing_bindings listing
        WHERE listing.merchant_id = NEW.id
          AND listing.control_state = 'verified'
          AND listing.a6_listing_id IS NOT NULL
        ON CONFLICT (listing_binding_id) DO NOTHING;
    ELSE
        UPDATE a6_trade.a6_channel_growth_policies policy
        SET enabled = false,
            recycle_enabled = false,
            callback_enabled = false,
            updated_at = now()
        FROM a6_trade.a6_managed_listing_bindings listing
        WHERE policy.listing_binding_id = listing.id
          AND listing.merchant_id = NEW.id;
    END IF;

    IF NEW.roles @> ARRAY['provider']::text[] THEN
        INSERT INTO a6_trade.a6_channel_booster_tokens (
            a6_token_ref_id, enabled
        )
        SELECT token.id, true
        FROM a6_trade.a6_tokens token
        WHERE token.merchant_id = NEW.id
          AND lower(token.token_name) = 'callback'
          AND token.status IN ('1', 'active', 'enabled')
        ON CONFLICT (a6_token_ref_id) DO UPDATE
        SET enabled = true, updated_at = now();
    ELSE
        UPDATE a6_trade.a6_channel_booster_tokens booster
        SET enabled = false, updated_at = now()
        FROM a6_trade.a6_tokens token
        WHERE booster.a6_token_ref_id = token.id
          AND token.merchant_id = NEW.id;
    END IF;
    RETURN NEW;
END
$$;

DROP TRIGGER IF EXISTS a6_merchant_growth_duties_sync
    ON a6_trade.a6_merchants;
CREATE TRIGGER a6_merchant_growth_duties_sync
AFTER UPDATE OF roles
ON a6_trade.a6_merchants
FOR EACH ROW
WHEN (OLD.roles IS DISTINCT FROM NEW.roles)
EXECUTE FUNCTION a6_trade.sync_merchant_growth_duties();

-- Current pilot: M5 and M6 are the two Provider-duty accounts.  The target merchant
-- exclusion in the runner prevents a Token from ever boosting its own Listing.
INSERT INTO a6_trade.a6_channel_booster_tokens (a6_token_ref_id, enabled)
SELECT token.id, true
FROM a6_trade.a6_tokens token
JOIN a6_trade.a6_merchants merchant ON merchant.id = token.merchant_id
WHERE lower(token.token_name) = 'callback'
  AND token.status IN ('1', 'active', 'enabled')
  AND merchant.roles @> ARRAY['provider']::text[]
ON CONFLICT (a6_token_ref_id) DO UPDATE
SET enabled = true, updated_at = now();
