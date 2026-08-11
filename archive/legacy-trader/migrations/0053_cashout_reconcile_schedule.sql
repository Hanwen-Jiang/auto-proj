CREATE INDEX IF NOT EXISTS a6_withdrawal_intent_reconcile_due_idx
    ON a6_trade.a6_withdrawal_intents (
        method,
        intent_state,
        last_reconciled_at NULLS FIRST,
        reconcile_lease_until,
        id
    )
    WHERE intent_state IN ('SUBMITTED', 'PENDING', 'UNKNOWN', 'RECONCILING');
