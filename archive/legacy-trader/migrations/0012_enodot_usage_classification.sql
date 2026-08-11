CREATE TABLE IF NOT EXISTS a6_trade.enodot_usage_classification_rules (
    id bigserial PRIMARY KEY,
    rule_uid text NOT NULL UNIQUE,
    enodot_user_id bigint,
    enodot_api_key_id bigint,
    classification text NOT NULL,
    valid_from timestamptz NOT NULL,
    valid_to timestamptz,
    reason text NOT NULL,
    created_by text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT enodot_usage_rule_selector_present CHECK (
        enodot_user_id IS NOT NULL OR enodot_api_key_id IS NOT NULL
    ),
    CONSTRAINT enodot_usage_rule_ids_positive CHECK (
        (enodot_user_id IS NULL OR enodot_user_id > 0)
        AND (enodot_api_key_id IS NULL OR enodot_api_key_id > 0)
    ),
    CONSTRAINT enodot_usage_rule_classification_known CHECK (
        classification IN ('owner_activation', 'internal_operation')
    ),
    CONSTRAINT enodot_usage_rule_window_valid CHECK (
        valid_to IS NULL OR valid_to > valid_from
    ),
    CONSTRAINT enodot_usage_rule_audit_nonempty CHECK (
        rule_uid <> '' AND reason <> '' AND created_by <> ''
    )
);

CREATE INDEX IF NOT EXISTS enodot_usage_rules_key_time_idx
    ON a6_trade.enodot_usage_classification_rules (
        enodot_api_key_id, valid_from, valid_to
    )
    WHERE enodot_api_key_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS enodot_usage_rules_user_time_idx
    ON a6_trade.enodot_usage_classification_rules (
        enodot_user_id, valid_from, valid_to
    )
    WHERE enodot_user_id IS NOT NULL;

-- User-declared owner activation, verified read-only against Enodot user/key metadata
-- before this migration was authored.
INSERT INTO a6_trade.enodot_usage_classification_rules (
    rule_uid, enodot_user_id, enodot_api_key_id, classification,
    valid_from, reason, created_by
)
VALUES (
    'owner-activation:admin:to-a6-api:20260725T095900Z',
    1,
    53,
    'owner_activation',
    timestamptz '2026-07-25 09:59:00+00',
    'Owner used an internal key to activate a merchant outside the imported session set',
    'user_declared_20260725'
)
ON CONFLICT (rule_uid) DO NOTHING;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'a6trader') THEN
        GRANT SELECT, INSERT, UPDATE, DELETE
            ON a6_trade.enodot_usage_classification_rules TO a6trader;
        GRANT USAGE, SELECT, UPDATE
            ON SEQUENCE a6_trade.enodot_usage_classification_rules_id_seq TO a6trader;
    END IF;
END
$$;
