CREATE INDEX IF NOT EXISTS trade_control_actions_seller_plan_latest_idx
    ON a6_trade.trade_control_actions (
        (desired_state ->> 'model'),
        (desired_state ->> 'merchant_id'),
        completed_at DESC,
        id DESC
    )
    WHERE action_type = 'apply_seller_price_band'
      AND status = 'completed';
