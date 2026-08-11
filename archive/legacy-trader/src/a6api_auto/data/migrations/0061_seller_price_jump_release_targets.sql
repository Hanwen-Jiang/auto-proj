-- Persist the complete offline reprice target so elapsed cooldowns can be
-- released even while the current Provider pricing snapshot is incomplete.

ALTER TABLE a6_trade.seller_price_jump_cooldowns
    ADD COLUMN latest_target_cost_multiplier numeric(30,12),
    ADD COLUMN latest_target_delta numeric(8,6);

UPDATE a6_trade.seller_price_jump_cooldowns cooldown
SET latest_target_delta = coalesce(seller.observed_delta, seller.desired_delta, 0)
FROM a6_trade.enodot_managed_seller_resources seller
WHERE seller.merchant_id = cooldown.merchant_id;

UPDATE a6_trade.seller_price_jump_cooldowns
SET latest_target_delta = coalesce(latest_target_delta, 0),
    latest_target_cost_multiplier = greatest(
        0.000000000001::numeric,
        round(
            latest_target_price_multiplier
            * (1 + coalesce(latest_target_delta, 0))
            * 0.833333333333::numeric
            / 1.20::numeric
            * 0.999999::numeric,
            12
        )
    );

ALTER TABLE a6_trade.seller_price_jump_cooldowns
    ALTER COLUMN latest_target_cost_multiplier SET NOT NULL,
    ALTER COLUMN latest_target_delta SET NOT NULL,
    ADD CONSTRAINT seller_price_jump_release_target_valid CHECK (
        latest_target_cost_multiplier > 0
        AND latest_target_delta BETWEEN 0 AND 0.5
    );
