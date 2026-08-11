-- P0-A ledger truth: label every A6 seller earning with the funding source of the
-- request that produced it, so circular booster ("抖加") money is never counted as
-- external revenue.
--
-- P0-B decay stop: replace the one-way `enabled=false` growth kill switch with an
-- expiring suppression, so nothing can permanently disable a growth policy.

ALTER TABLE a6_trade.a6_earning_events
    ADD COLUMN IF NOT EXISTS counterparty_class text NOT NULL DEFAULT 'unknown',
    ADD COLUMN IF NOT EXISTS counterparty_token_ref_id bigint,
    ADD COLUMN IF NOT EXISTS classified_at timestamptz;

ALTER TABLE a6_trade.a6_earning_events
    DROP CONSTRAINT IF EXISTS a6_earning_events_counterparty_class_check;
ALTER TABLE a6_trade.a6_earning_events
    ADD CONSTRAINT a6_earning_events_counterparty_class_check CHECK (
        counterparty_class IN ('external', 'own_boost', 'own_pin', 'unknown')
    );

-- Revenue aggregation always filters on counterparty_class.
CREATE INDEX IF NOT EXISTS a6_earning_events_counterparty_time_idx
    ON a6_trade.a6_earning_events (counterparty_class, occurred_at DESC);

-- The incremental classifier only scans rows it has not resolved yet.
CREATE INDEX IF NOT EXISTS a6_earning_events_unclassified_idx
    ON a6_trade.a6_earning_events (occurred_at)
    WHERE counterparty_class = 'unknown';

-- request_id is the join key on both sides; provider events are looked up by it.
CREATE INDEX IF NOT EXISTS a6_provider_usage_events_request_id_idx
    ON a6_trade.a6_provider_usage_events (request_id)
    WHERE request_id IS NOT NULL;

-- Backfill. Every historical counterparty has already been collected, so an earning
-- with no matching provider request is external by elimination.
--
-- Measured at 2026-07-26T11:20Z before this ran:
--   30680 earnings, 100% carry request_id
--    1919 match one of our own provider requests, all 11 via `callback` booster tokens
--       0 match a non-booster provider token (the Pin own-channel exclusion has held)
--   own_boost earned  3,897,294 quota  ($7.79)
--   external earned  15,642,203 quota  ($31.28)
--   booster spend     6,152,145 quota  ($12.30)  -> net boost loss $4.51
WITH counterparty AS (
    SELECT
        p.request_id,
        max(p.a6_token_ref_id) AS token_ref_id,
        bool_or(booster.a6_token_ref_id IS NOT NULL) AS is_booster
    FROM a6_trade.a6_provider_usage_events p
    LEFT JOIN a6_trade.a6_channel_booster_tokens booster
        ON booster.a6_token_ref_id = p.a6_token_ref_id
    WHERE p.request_id IS NOT NULL
    GROUP BY p.request_id
)
UPDATE a6_trade.a6_earning_events e
SET counterparty_class = CASE
        WHEN c.request_id IS NULL THEN 'external'
        WHEN c.is_booster THEN 'own_boost'
        ELSE 'own_pin'
    END,
    counterparty_token_ref_id = c.token_ref_id,
    classified_at = now()
FROM (
    SELECT e2.id, c2.request_id, c2.token_ref_id, c2.is_booster
    FROM a6_trade.a6_earning_events e2
    LEFT JOIN counterparty c2 ON c2.request_id = e2.request_id
) c
WHERE e.id = c.id
  AND e.counterparty_class = 'unknown';

-- Expiring suppression replaces the permanent `enabled=false` written by retirement.
ALTER TABLE a6_trade.a6_channel_growth_policies
    ADD COLUMN IF NOT EXISTS suppressed_until timestamptz,
    ADD COLUMN IF NOT EXISTS suppression_reason text,
    ADD COLUMN IF NOT EXISTS suppression_count integer NOT NULL DEFAULT 0;

CREATE INDEX IF NOT EXISTS a6_channel_growth_policies_suppressed_idx
    ON a6_trade.a6_channel_growth_policies (suppressed_until)
    WHERE suppressed_until IS NOT NULL;

-- Record which policies retirement had permanently disabled, so the operator-run
-- reset command can tell them apart from deliberate manual disables. This does not
-- re-enable anything: turning growth back on is an explicit, phased decision.
UPDATE a6_trade.a6_channel_growth_policies policy
SET suppression_reason = 'retirement_legacy_disable',
    suppressed_until = now()
FROM a6_trade.a6_managed_listing_bindings listing
JOIN a6_trade.a6_seller_retirement_plans retirement
  ON retirement.merchant_id = listing.merchant_id
WHERE policy.listing_binding_id = listing.id
  AND retirement.completed_at IS NOT NULL
  AND policy.enabled = false
  AND policy.suppression_reason IS NULL;
