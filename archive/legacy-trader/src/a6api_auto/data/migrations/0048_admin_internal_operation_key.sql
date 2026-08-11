-- The admin-owned funsionapi key drives operator traffic. Without an explicit
-- rule, its usage is reported as external revenue and creates a false profit.
INSERT INTO a6_trade.enodot_usage_classification_rules (
    rule_uid, enodot_user_id, enodot_api_key_id, classification,
    valid_from, reason, created_by
)
VALUES (
    'internal-operation:admin:funsionapi:20260717T201954Z',
    1,
    54,
    'internal_operation',
    timestamptz '2026-07-17 20:19:54.970329+00',
    'Admin-owned funsionapi key is used for operator traffic, not customer revenue',
    'production_audit_20260731'
)
ON CONFLICT (rule_uid) DO NOTHING;
