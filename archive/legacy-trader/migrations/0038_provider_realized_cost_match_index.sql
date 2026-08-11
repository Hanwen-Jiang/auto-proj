CREATE INDEX IF NOT EXISTS a6_provider_usage_events_realized_cost_match_idx
    ON a6_trade.a6_provider_usage_events (
        a6_token_ref_id,
        merchant_id,
        model,
        occurred_at DESC
    )
    INCLUDE (id, quota_micros, prompt_tokens, completion_tokens)
    WHERE a6_token_ref_id IS NOT NULL
      AND probe_run_id IS NULL
      AND result_class = '2';
