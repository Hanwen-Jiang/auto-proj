BEGIN;

DO $$
DECLARE
    eligible_count integer;
    funded_trial_count integer;
BEGIN
    SELECT count(*) INTO eligible_count
    FROM a6_trade.a6_merchants merchant
    JOIN a6_trade.a6_seller_retirement_plans retirement
      ON retirement.merchant_id = merchant.id
    WHERE merchant.id IN (2, 3, 9, 10, 12, 15)
      AND merchant.roles = ARRAY['observer']::text[]
      AND merchant.status = 'supplier_submitted'
      AND retirement.archive_mode
      AND retirement.state = 'completed'
      AND EXISTS (
          SELECT 1
          FROM a6_trade.a6_tokens token
          JOIN a6_trade.enodot_account_bindings binding
            ON binding.a6_token_id = token.id
          WHERE token.merchant_id = merchant.id
            AND token.status IN ('1', 'active', 'enabled')
            AND binding.state = 'bound'
            AND binding.enodot_account_id IS NOT NULL
      );
    IF eligible_count <> 6 THEN
        RAISE EXCEPTION 'residual_provider_rollout_precondition_failed:%', eligible_count;
    END IF;

    SELECT count(*) INTO funded_trial_count
    FROM a6_trade.a6_merchants merchant
    JOIN a6_trade.a6_financial_current financial ON financial.merchant_id = merchant.id
    WHERE merchant.id IN (2, 3, 9)
      AND financial.user_balance_quota > (merchant.quota_per_unit + 19) / 20;
    IF funded_trial_count <> 3 THEN
        RAISE EXCEPTION 'residual_provider_rollout_balance_failed:%', funded_trial_count;
    END IF;
END
$$;

INSERT INTO a6_trade.residual_provider_policies (
    model, merchant_id, enabled, state, reserve_balance_quota,
    desired_priority, desired_concurrency, last_balance_quota
)
SELECT
    'gpt-5.6-sol', merchant.id, true,
    CASE WHEN merchant.id IN (2, 3, 9) THEN 'active' ELSE 'candidate' END,
    (merchant.quota_per_unit + 19) / 20,
    1, 15, financial.user_balance_quota
FROM a6_trade.a6_merchants merchant
JOIN a6_trade.a6_financial_current financial ON financial.merchant_id = merchant.id
WHERE merchant.id IN (2, 3, 9, 10, 12, 15)
ON CONFLICT (model, merchant_id) DO UPDATE SET
    enabled = true,
    state = CASE
        WHEN residual_provider_policies.state = 'drained'
        THEN residual_provider_policies.state
        ELSE EXCLUDED.state
    END,
    reserve_balance_quota = EXCLUDED.reserve_balance_quota,
    desired_priority = EXCLUDED.desired_priority,
    desired_concurrency = EXCLUDED.desired_concurrency,
    last_balance_quota = EXCLUDED.last_balance_quota,
    last_error_class = NULL,
    updated_at = now();

UPDATE a6_trade.a6_merchants merchant
SET roles = ARRAY['provider', 'observer']::text[],
    role_revision = role_revision + 1,
    updated_at = now()
FROM a6_trade.residual_provider_policies policy
WHERE policy.model = 'gpt-5.6-sol'
  AND policy.merchant_id = merchant.id
  AND policy.enabled
  AND policy.state = 'active'
  AND merchant.roles = ARRAY['observer']::text[];

UPDATE a6_trade.enodot_account_bindings binding
SET desired_priority = policy.desired_priority,
    desired_concurrency = policy.desired_concurrency,
    last_error_class = NULL,
    updated_at = now()
FROM a6_trade.a6_tokens token
JOIN a6_trade.residual_provider_policies policy
  ON policy.merchant_id = token.merchant_id
 AND policy.model = 'gpt-5.6-sol'
 AND policy.enabled
 AND policy.state = 'active'
WHERE binding.a6_token_id = token.id
  AND binding.state = 'bound';

INSERT INTO a6_trade.provider_inventory (
    model, merchant_id, enodot_binding_id, enodot_account_id, a6_token_id,
    inventory_state, liquidity_state, funding_eligible, schedulable,
    balance_units, financial_source_hash, assigned_slot_no,
    last_financial_observed_at
)
SELECT
    policy.model, merchant.id, binding.id, binding.enodot_account_id, token.id,
    'WARM_STANDBY', 'FUNDED', true, false,
    financial.user_balance_quota, financial.source_hash, NULL,
    financial.observed_at
FROM a6_trade.residual_provider_policies policy
JOIN a6_trade.a6_merchants merchant ON merchant.id = policy.merchant_id
JOIN a6_trade.a6_financial_current financial ON financial.merchant_id = merchant.id
JOIN a6_trade.a6_tokens token ON token.merchant_id = merchant.id
JOIN a6_trade.enodot_account_bindings binding
  ON binding.a6_token_id = token.id
 AND binding.state = 'bound'
WHERE policy.model = 'gpt-5.6-sol'
  AND policy.enabled
  AND policy.state = 'active'
ON CONFLICT (model, merchant_id) DO UPDATE SET
    enodot_binding_id = EXCLUDED.enodot_binding_id,
    enodot_account_id = EXCLUDED.enodot_account_id,
    a6_token_id = EXCLUDED.a6_token_id,
    inventory_state = EXCLUDED.inventory_state,
    liquidity_state = EXCLUDED.liquidity_state,
    funding_eligible = EXCLUDED.funding_eligible,
    schedulable = EXCLUDED.schedulable,
    balance_units = EXCLUDED.balance_units,
    financial_source_hash = EXCLUDED.financial_source_hash,
    assigned_slot_no = NULL,
    state_version = provider_inventory.state_version + 1,
    last_financial_observed_at = EXCLUDED.last_financial_observed_at,
    updated_at = now();

UPDATE a6_trade.residual_provider_policies
SET activated_at = coalesce(activated_at, now()), updated_at = now()
WHERE model = 'gpt-5.6-sol' AND enabled AND state = 'active';

INSERT INTO a6_trade.residual_provider_events (
    model, merchant_id, event_type, balance_quota,
    reserve_balance_quota, details
)
SELECT
    policy.model, policy.merchant_id,
    CASE WHEN policy.state = 'active' THEN 'activated' ELSE 'candidate_registered' END,
    policy.last_balance_quota, policy.reserve_balance_quota,
    jsonb_build_object(
        'stage', CASE WHEN policy.state = 'active' THEN 1 ELSE 2 END,
        'desired_priority', policy.desired_priority,
        'desired_concurrency', policy.desired_concurrency
    )
FROM a6_trade.residual_provider_policies policy
WHERE policy.model = 'gpt-5.6-sol'
  AND policy.merchant_id IN (2, 3, 9, 10, 12, 15)
  AND NOT EXISTS (
      SELECT 1
      FROM a6_trade.residual_provider_events event
      WHERE event.model = policy.model
        AND event.merchant_id = policy.merchant_id
        AND event.event_type = CASE
            WHEN policy.state = 'active' THEN 'activated'
            ELSE 'candidate_registered'
        END
  );

INSERT INTO a6_trade.trade_control_actions (
    idempotency_key, action_type, entity_type, entity_id,
    desired_state, observed_before, observed_after, status,
    config_version, requested_by, started_at, completed_at
)
SELECT
    'residual-provider:merchant:' || policy.merchant_id || ':activate:v1',
    'activate_residual_provider', 'merchant', policy.merchant_id::text,
    jsonb_build_object(
        'roles', ARRAY['provider', 'observer']::text[],
        'reserve_balance_quota', policy.reserve_balance_quota,
        'desired_concurrency', policy.desired_concurrency
    ),
    jsonb_build_object('roles', ARRAY['observer']::text[]),
    jsonb_build_object(
        'roles', ARRAY['provider', 'observer']::text[],
        'schedulable', false
    ),
    'completed', 'residual-provider-v1', 'a6-trader-residual-provider',
    now(), now()
FROM a6_trade.residual_provider_policies policy
WHERE policy.model = 'gpt-5.6-sol'
  AND policy.enabled
  AND policy.state = 'active'
ON CONFLICT (idempotency_key) DO NOTHING;

DO $$
DECLARE
    active_count integer;
    candidate_count integer;
BEGIN
    SELECT count(*) FILTER (WHERE state = 'active'),
           count(*) FILTER (WHERE state = 'candidate')
      INTO active_count, candidate_count
    FROM a6_trade.residual_provider_policies
    WHERE model = 'gpt-5.6-sol'
      AND merchant_id IN (2, 3, 9, 10, 12, 15);
    IF active_count <> 3 OR candidate_count <> 3 THEN
        RAISE EXCEPTION 'residual_provider_rollout_state_failed:%:%',
            active_count, candidate_count;
    END IF;
END
$$;

COMMIT;
