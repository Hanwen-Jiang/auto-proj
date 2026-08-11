from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from a6api_auto.storage import write_private_json
from a6api_auto.trade_db import (
    UPSERT_MERCHANT_SQL,
    MerchantMetadata,
    MigrationError,
    apply_migrations,
    assert_metadata_is_secret_free,
    load_session_metadata,
    metadata_from_session,
    upsert_merchant_metadata,
)


class FakeCursor:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection
        self.result = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, query: str, params=None):
        self.connection.calls.append((query, params))
        normalized = " ".join(query.split())
        if normalized.startswith("SELECT 1 FROM pg_namespace"):
            self.result = (1,) if self.connection.schema_exists else None
        elif normalized.startswith("CREATE SCHEMA"):
            self.connection.schema_exists = True
        elif normalized.startswith("SELECT checksum FROM a6_trade.schema_migrations WHERE version"):
            checksum = self.connection.migrations.get(params[0])
            self.result = None if checksum is None else (checksum,)
        elif normalized.startswith("INSERT INTO a6_trade.schema_migrations"):
            version, _filename, checksum = params
            self.connection.migrations[version] = checksum
        elif normalized.startswith("INSERT INTO a6_trade.a6_merchants"):
            existing = self.connection.merchants.get(params[0])
            if existing is not None:
                if params[5] == "abandoned":
                    params = (
                        *params[:2],
                        existing[2] if existing[2] is not None else params[2],
                        existing[3] if existing[3] is not None else params[3],
                        *params[4:],
                    )
                operating_group = params[6] if params[6] != "standard" else existing[6]
                params = (*params[:6], operating_group, existing[7], *params[8:])
            self.connection.merchants[params[0]] = params
        return self

    def fetchone(self):
        return self.result


class FakeConnection:
    def __init__(self) -> None:
        self.calls = []
        self.migrations = {}
        self.merchants = {}
        self.commits = 0
        self.rollbacks = 0
        self.schema_exists = False

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def session_file(path: Path, number: int) -> Path:
    write_private_json(
        path,
        {
            "schema_version": 1,
            "origin": "https://fixture.invalid",
            "email": f"merchant-{number}@fixture.invalid",
            "username": f"merchant-{number}",
            "password": f"password-secret-{number}",
            "cookies": {"session": f"cookie-secret-{number}"},
            "a6api_key": f"sk-secret-{number}",
            "user_id": 1000 + number,
            "supplier_id": 2000 + number,
            "quota_per_unit": 500_000 + number,
            "status": "supplier_submitted",
        },
    )
    return path


def test_phase0_migration_contains_core_fact_and_control_tables() -> None:
    migration = Path("migrations/0001_a6_trade_phase0.sql").read_text()
    packaged = Path("src/a6api_auto/data/migrations/0001_a6_trade_phase0.sql").read_text()
    expected = {
        "a6_merchants",
        "a6_financial_current",
        "a6_financial_snapshots",
        "a6_earning_events",
        "a6_provider_usage_events",
        "a6_market_scan_runs",
        "a6_market_quotes",
        "a6_pin_bindings",
        "a6_pin_decisions",
        "trade_control_actions",
        "trade_control_action_attempts",
    }

    assert all(f"a6_trade.{table}" in migration for table in expected)
    assert "CREATE INDEX IF NOT EXISTS" in migration
    assert "CREATE UNIQUE INDEX IF NOT EXISTS a6_pin_bindings_one_current_uidx" in migration
    assert "a6_token_ref_id bigint REFERENCES a6_trade.a6_tokens(id)" in migration
    assert "external_a6_token_id bigint" in migration
    assert packaged == migration


def test_observer_payload_integrity_migration_is_packaged() -> None:
    migration = Path("migrations/0030_observer_scan_payload_integrity.sql").read_text()
    packaged = Path(
        "src/a6api_auto/data/migrations/0030_observer_scan_payload_integrity.sql"
    ).read_text()

    assert packaged == migration
    assert "ADD COLUMN IF NOT EXISTS result_payload jsonb" in migration
    assert "a6_market_scan_result_payload_array" in migration


def test_provider_pool_consistency_migration_is_packaged_and_composite() -> None:
    migration = Path("migrations/0031_provider_pool_consistency.sql").read_text()
    packaged = Path("src/a6api_auto/data/migrations/0031_provider_pool_consistency.sql").read_text()

    assert packaged == migration
    assert "provider_pool_generation_cost_identity_fk" in migration
    assert "provider_pool_slot_routing_identity_fk" in migration
    assert "provider_pool_slot_cost_identity_fk" in migration
    assert "provider_inventory_binding_identity_fk" in migration
    assert "provider_pool_slot_binding_identity_fk" in migration
    assert "provider_pool_slot_ready_binding_required" in migration
    assert "provider_pool_slot_vacant_bindings_empty" in migration
    assert "provider_pool_slot_pin_identity_fk" in migration
    assert "provider_pool_current_ready_account_idx" in migration
    assert "provider_pool_current_ready_token_idx" in migration
    assert "NOT VALID" in migration


def test_model_publication_persistence_migration_is_packaged() -> None:
    migration = Path("migrations/0032_model_publication_persistence.sql").read_text()
    packaged = Path(
        "src/a6api_auto/data/migrations/0032_model_publication_persistence.sql"
    ).read_text()

    assert packaged == migration
    assert "ADD COLUMN IF NOT EXISTS desired_at" in migration
    assert "state_version" in migration
    assert "model_publications_observed_at_idx" in migration


def test_model_publication_scope_migration_upgrades_0032_and_is_packaged() -> None:
    migration = Path("migrations/0033_model_publication_scope.sql").read_text()
    packaged = Path("src/a6api_auto/data/migrations/0033_model_publication_scope.sql").read_text()

    assert packaged == migration
    assert "ADD COLUMN IF NOT EXISTS publication_scope text" in migration
    assert "ADD COLUMN IF NOT EXISTS desired_cost_generation bigint" in migration
    assert "model_publications_scope_nonempty" in migration
    assert "model_publications_cost_generation_positive" in migration


def test_model_publication_redundancy_target_migration_is_packaged_and_constrained() -> None:
    migration = Path("migrations/0046_model_publication_redundancy_target.sql").read_text()
    packaged = Path(
        "src/a6api_auto/data/migrations/0046_model_publication_redundancy_target.sql"
    ).read_text()

    assert packaged == migration
    assert "ADD COLUMN IF NOT EXISTS redundancy_target integer" in migration
    assert "SET redundancy_target = GREATEST(min_healthy_slots, 3)" in migration
    assert "ALTER COLUMN redundancy_target SET DEFAULT 3" in migration
    assert "redundancy_target >= min_healthy_slots" in migration


def test_channel_readiness_budget_migration_is_packaged_and_constrained() -> None:
    migration = Path("migrations/0047_channel_readiness_budget.sql").read_text()
    packaged = Path("src/a6api_auto/data/migrations/0047_channel_readiness_budget.sql").read_text()

    assert packaged == migration
    assert "daily_readiness_budget_micros = 120000" in migration
    assert "daily_readiness_budget_micros = 2000000" in migration
    assert "a6_channel_growth_readiness_budget_valid" in migration
    assert "a6_channel_booster_readiness_budget_valid" in migration


def test_provider_realized_cost_match_index_is_packaged_and_scoped() -> None:
    migration = Path("migrations/0038_provider_realized_cost_match_index.sql").read_text()
    packaged = Path(
        "src/a6api_auto/data/migrations/0038_provider_realized_cost_match_index.sql"
    ).read_text()

    assert packaged == migration
    assert "a6_provider_usage_events_realized_cost_match_idx" in migration
    assert "a6_token_ref_id" in migration
    assert "merchant_id" in migration
    assert "model" in migration
    assert "occurred_at DESC" in migration
    assert "probe_run_id IS NULL" in migration
    assert "result_class = '2'" in migration


def test_provider_pool_sizing_indexes_are_packaged_and_transactional() -> None:
    migration = Path("migrations/0040_provider_pool_sizing_evidence_indexes.sql").read_text()
    packaged = Path(
        "src/a6api_auto/data/migrations/0040_provider_pool_sizing_evidence_indexes.sql"
    ).read_text()

    assert packaged == migration
    assert "provider_request_envelopes_model_created_idx" in migration
    assert "(model, created_at DESC)" in migration
    assert "a6_financial_snapshots_merchant_observed_idx" in migration
    assert "(merchant_id, observed_at DESC)" in migration
    assert "CREATE INDEX CONCURRENTLY" not in migration


def test_sub2api_provider_dispatch_grant_is_packaged_and_read_only() -> None:
    migration = Path("migrations/0041_sub2api_provider_dispatch_read_privileges.sql").read_text()
    packaged = Path(
        "src/a6api_auto/data/migrations/0041_sub2api_provider_dispatch_read_privileges.sql"
    ).read_text()

    assert packaged == migration
    assert "ARRAY['sub2api_cm', 'sub2api']" in migration
    assert "GRANT USAGE ON SCHEMA a6_trade" in migration
    for table in (
        "provider_pool_generations",
        "provider_cost_generations",
        "provider_pool_slots",
        "a6_tokens",
        "enodot_account_bindings",
        "a6_pin_bindings",
        "provider_fast_block_outbox",
        "provider_quarantines",
    ):
        assert f"a6_trade.{table}" in migration
    assert "GRANT SELECT ON TABLE" in migration


def test_provider_auto_pool_migration_is_packaged_with_ban_activation_gate() -> None:
    migration = Path("migrations/0066_provider_auto_pool.sql").read_text()
    packaged = Path(
        "src/a6api_auto/data/migrations/0066_provider_auto_pool.sql"
    ).read_text()

    assert migration == packaged
    assert "provider_auto_pool_generations" in migration
    assert "provider_auto_pool_accounts" in migration
    assert "max_ratio_cny" in migration
    assert "max_ratio_usd" in migration
    assert "ban_confirmed AND unpin_confirmed AND auto_pool_confirmed" in migration
    assert "route_mode IN ('fixed_pin', 'auto_pool')" in migration
    assert "INSERT INTO" not in migration
    assert "UPDATE a6_trade" not in migration
    assert "DELETE FROM" not in migration


def test_provider_auto_pool_runtime_failure_migration_is_packaged_and_route_aware() -> None:
    migration = Path(
        "migrations/0067_provider_auto_pool_runtime_failure.sql"
    ).read_text()
    packaged = Path(
        "src/a6api_auto/data/migrations/0067_provider_auto_pool_runtime_failure.sql"
    ).read_text()

    assert migration == packaged
    assert "'refresh_auto_pool'" in migration
    assert "slot.route_mode = 'auto_pool'" in migration
    assert "failure_code = 'insufficient_quota'" in migration
    assert "SET slot_state = 'REPAIRING'" in migration
    assert "inventory_state = 'REPAIRING'" in migration
    assert "WHEN block_action = 'block_liquidity' THEN 'UNFUNDED'" in migration
    assert "pg_notify(" in migration
    assert "a6_provider_runtime_failure" in migration


def test_quota_per_unit_migration_is_packaged_and_constrained() -> None:
    migration = Path("migrations/0002_quota_per_unit.sql").read_text()
    packaged = Path("src/a6api_auto/data/migrations/0002_quota_per_unit.sql").read_text()

    assert "ADD COLUMN IF NOT EXISTS quota_per_unit bigint" in migration
    assert "CHECK (quota_per_unit > 0)" in migration
    assert packaged == migration


def test_crypto_withdraw_account_migration_is_packaged_and_unique() -> None:
    migration = Path("migrations/0003_crypto_withdraw_accounts.sql").read_text()
    packaged = Path("src/a6api_auto/data/migrations/0003_crypto_withdraw_accounts.sql").read_text()

    assert "merchant_id bigint NOT NULL UNIQUE" in migration
    assert "address_sha256 char(64) NOT NULL UNIQUE" in migration
    assert "a6_withdraw_account_bindings_a6_account_uidx" in migration
    assert "existing_unmanaged" in migration
    assert packaged == migration


def test_supplier_inventory_migration_is_packaged_with_current_row_identity() -> None:
    migration = Path("migrations/0004_supplier_inventory.sql").read_text()
    packaged = Path("src/a6api_auto/data/migrations/0004_supplier_inventory.sql").read_text()

    assert packaged == migration
    assert "CREATE TABLE IF NOT EXISTS a6_trade.a6_supplier_channels_current" in migration
    assert "CREATE TABLE IF NOT EXISTS a6_trade.a6_supplier_listings_current" in migration
    assert (
        migration.count(
            "merchant_id bigint NOT NULL REFERENCES a6_trade.a6_merchants(id) ON DELETE CASCADE"
        )
        == 2
    )
    assert "UNIQUE (merchant_id, a6_channel_id)" in migration
    assert "UNIQUE (merchant_id, a6_listing_id)" in migration


def test_supplier_inventory_migration_preserves_prices_and_ownership_state() -> None:
    migration = Path("migrations/0004_supplier_inventory.sql").read_text()
    price_fields = (
        "input_price_micros",
        "output_price_micros",
        "cache_read_price_micros",
        "cache_write_price_micros",
    )

    assert all(f"{field} bigint NOT NULL DEFAULT 0" in migration for field in price_fields)
    assert all(f"{field} >= 0" in migration for field in price_fields)
    assert "managed_state text NOT NULL DEFAULT 'existing_unmanaged'" in migration
    assert "managed_state IN ('existing_unmanaged', 'managed')" in migration
    assert "a6_supplier_channels_supplier_idx" in migration
    assert "a6_supplier_channels_availability_idx" in migration
    assert "a6_supplier_listings_channel_idx" in migration
    assert "a6_supplier_listings_model_visibility_idx" in migration
    assert "a6_supplier_listings_managed_state_idx" in migration


def test_supplier_profile_cache_migration_is_packaged_and_bounded() -> None:
    migration = Path("migrations/0023_supplier_profile_cache.sql").read_text()
    packaged = Path("src/a6api_auto/data/migrations/0023_supplier_profile_cache.sql").read_text()

    assert packaged == migration
    assert "ADD COLUMN IF NOT EXISTS supplier_name text" in migration
    assert "ADD COLUMN IF NOT EXISTS supplier_description text" in migration
    assert "ADD COLUMN IF NOT EXISTS supplier_profile_observed_at timestamptz" in migration
    assert "char_length(supplier_name) <= 256" in migration
    assert "char_length(supplier_description) <= 2000" in migration


def test_supplier_inventory_migration_contains_all_collector_snapshot_columns() -> None:
    migration = Path("migrations/0004_supplier_inventory.sql").read_text()
    channel_columns = {
        "merchant_id",
        "a6_channel_id",
        "supplier_id",
        "name",
        "nickname",
        "channel_type",
        "status",
        "availability",
        "observed_at",
        "source_hash",
        "created_at",
        "updated_at",
    }
    listing_columns = {
        "merchant_id",
        "a6_listing_id",
        "a6_channel_id",
        "own_model",
        "platform_model",
        "charge_type",
        "input_price_micros",
        "output_price_micros",
        "cache_read_price_micros",
        "cache_write_price_micros",
        "status",
        "availability",
        "visibility",
        "display_name",
        "description",
        "managed_state",
        "observed_at",
        "source_hash",
        "created_at",
        "updated_at",
    }

    channel_definition, listing_definition = migration.split(
        "CREATE TABLE IF NOT EXISTS a6_trade.a6_supplier_listings_current",
        maxsplit=1,
    )
    assert all(column in channel_definition for column in channel_columns)
    assert all(column in listing_definition for column in listing_columns)


def test_enodot_managed_seller_resources_migration_is_packaged_and_isolated() -> None:
    migration = Path("migrations/0006_enodot_managed_seller_resources.sql").read_text()
    packaged = Path(
        "src/a6api_auto/data/migrations/0006_enodot_managed_seller_resources.sql"
    ).read_text()

    assert packaged == migration
    assert "CREATE TABLE IF NOT EXISTS a6_trade.enodot_managed_seller_resources" in migration
    assert "merchant_id bigint NOT NULL UNIQUE" in migration
    assert "managed_uid text NOT NULL UNIQUE" in migration
    assert "enodot_user_id bigint" in migration
    assert "enodot_api_key_id bigint" in migration
    assert "api_key_fingerprint char(64)" in migration
    assert "desired_delta BETWEEN 0 AND 0.5" in migration
    assert "enodot_managed_seller_user_uidx" in migration
    assert "enodot_managed_seller_key_uidx" in migration
    assert "enodot_account_bindings" not in migration
    assert "password" not in migration
    assert "api_key text" not in migration


def test_seller_base_role_migration_is_packaged_and_supports_duty_switching() -> None:
    migration = Path("migrations/0007_seller_base_role.sql").read_text()
    packaged = Path("src/a6api_auto/data/migrations/0007_seller_base_role.sql").read_text()

    assert packaged == migration
    assert "ADD COLUMN IF NOT EXISTS role_revision" in migration
    assert "ALTER COLUMN roles SET DEFAULT ARRAY['seller']::text[]" in migration
    assert "roles[1] = 'seller'" in migration
    assert "cardinality(roles) BETWEEN 1 AND 2" in migration
    assert "roles[2] IN ('provider', 'observer', 'probe')" in migration
    assert "'provider', 'observer', 'probe'" in migration
    assert "a6_merchants_role_revision_nonnegative" in migration


def test_probe_role_removal_migration_is_packaged_and_preserves_other_duties() -> None:
    migration = Path("migrations/0009_remove_probe_merchant_role.sql").read_text()
    packaged = Path(
        "src/a6api_auto/data/migrations/0009_remove_probe_merchant_role.sql"
    ).read_text()

    assert packaged == migration
    assert "WHERE roles @> ARRAY['probe']::text[]" in migration
    assert "WHEN roles @> ARRAY['provider']::text[]" in migration
    assert "WHEN roles @> ARRAY['observer']::text[]" in migration
    assert "roles <@ ARRAY['seller', 'provider', 'observer']::text[]" in migration
    assert "roles[2] IN ('provider', 'observer')" in migration
    assert "role_revision = role_revision + 1" in migration


def test_runtime_privilege_migration_is_packaged_and_role_guarded() -> None:
    migration = Path("migrations/0008_runtime_role_privileges.sql").read_text()
    packaged = Path("src/a6api_auto/data/migrations/0008_runtime_role_privileges.sql").read_text()

    assert packaged == migration
    assert "FROM pg_roles WHERE rolname = 'a6trader'" in migration
    assert "ON ALL TABLES IN SCHEMA a6_trade TO a6trader" in migration
    assert "ON ALL SEQUENCES IN SCHEMA a6_trade TO a6trader" in migration
    assert "ALTER DEFAULT PRIVILEGES IN SCHEMA a6_trade" in migration


def test_market_quote_currency_migration_is_packaged_and_constrained() -> None:
    migration = Path("migrations/0011_market_quote_currency.sql").read_text()
    packaged = Path("src/a6api_auto/data/migrations/0011_market_quote_currency.sql").read_text()

    assert packaged == migration
    assert "ADD COLUMN IF NOT EXISTS realtime_ratio_usd numeric(24, 12)" in migration
    assert "ADD COLUMN IF NOT EXISTS realtime_ratio_cny numeric(24, 12)" in migration
    assert "ADD COLUMN IF NOT EXISTS exchange_rate numeric(24, 12)" in migration
    assert "SET realtime_ratio_usd = final_multiplier" in migration
    assert "a6_market_quotes_currency_values_positive" in migration


def test_enodot_usage_classification_migration_is_packaged_and_audited() -> None:
    migration = Path("migrations/0012_enodot_usage_classification.sql").read_text()
    packaged = Path(
        "src/a6api_auto/data/migrations/0012_enodot_usage_classification.sql"
    ).read_text()

    assert packaged == migration
    assert "CREATE TABLE IF NOT EXISTS a6_trade.enodot_usage_classification_rules" in migration
    assert "classification IN ('owner_activation', 'internal_operation')" in migration
    assert "valid_to IS NULL OR valid_to > valid_from" in migration
    assert "enodot_user_id, enodot_api_key_id" in migration
    assert "'owner_activation'" in migration
    assert "timestamptz '2026-07-25 09:59:00+00'" in migration
    assert "'user_declared_20260725'" in migration
    assert "request_id" not in migration


def test_admin_internal_operation_key_migration_is_packaged_and_audited() -> None:
    migration = Path("migrations/0048_admin_internal_operation_key.sql").read_text()
    packaged = Path(
        "src/a6api_auto/data/migrations/0048_admin_internal_operation_key.sql"
    ).read_text()

    assert packaged == migration
    assert "internal-operation:admin:funsionapi:20260717T201954Z" in migration
    assert "'internal_operation'" in migration
    assert "2026-07-17 20:19:54.970329+00" in migration


def test_apply_migrations_skips_schema_create_when_schema_exists(tmp_path: Path) -> None:
    migration_dir = tmp_path / "migrations"
    migration_dir.mkdir()
    (migration_dir / "0001_fixture.sql").write_text("SELECT 1;\n")
    connection = FakeConnection()
    connection.schema_exists = True

    assert apply_migrations(connection, migration_dir) == ("0001",)
    assert not any("CREATE SCHEMA" in query for query, _params in connection.calls)


def test_managed_supplier_resources_migration_is_packaged_and_idempotent() -> None:
    migration = Path("migrations/0005_managed_supplier_resources.sql").read_text()
    packaged = Path(
        "src/a6api_auto/data/migrations/0005_managed_supplier_resources.sql"
    ).read_text()

    assert packaged == migration
    assert "CREATE TABLE IF NOT EXISTS a6_trade.a6_managed_channel_bindings" in migration
    assert "CREATE TABLE IF NOT EXISTS a6_trade.a6_managed_listing_bindings" in migration
    assert migration.count("managed_uid text NOT NULL UNIQUE") == 2
    assert migration.count("create_idempotency_key text NOT NULL UNIQUE") == 2
    assert (
        migration.count("create_action_id bigint REFERENCES a6_trade.trade_control_actions(id)")
        == 2
    )
    assert (
        migration.count("last_action_id bigint REFERENCES a6_trade.trade_control_actions(id)") == 2
    )


def test_managed_supplier_resources_require_readback_before_verified_state() -> None:
    migration = Path("migrations/0005_managed_supplier_resources.sql").read_text()

    assert "a6_managed_channels_verified_state_complete" in migration
    assert "a6_managed_listings_verified_state_complete" in migration
    assert migration.count("observed_state IS NOT NULL") >= 2
    assert migration.count("verified_at IS NOT NULL") >= 2
    assert migration.count("create_action_id IS NOT NULL") >= 4
    assert "control_state <> 'verified'" in migration
    assert "'create_unknown'" in migration
    assert "'update_unknown'" in migration
    assert "'takedown_unknown'" in migration
    assert "'resubmit_unknown'" in migration
    assert migration.count("jsonb_typeof(desired_state) = 'object'") == 2
    assert migration.count("jsonb_typeof(observed_state) = 'object'") == 2


def test_managed_supplier_resources_cannot_claim_unobserved_or_foreign_inventory() -> None:
    migration = Path("migrations/0005_managed_supplier_resources.sql").read_text()

    assert "REFERENCES a6_trade.a6_supplier_channels_current" in migration
    assert "REFERENCES a6_trade.a6_supplier_listings_current" in migration
    assert "FOREIGN KEY (merchant_id, channel_binding_id)" in migration
    assert "FOREIGN KEY (merchant_id, channel_binding_id, a6_channel_id)" in migration
    assert "a6_managed_channels_external_uidx" in migration
    assert "a6_managed_channels_create_action_uidx" in migration
    assert "a6_managed_listings_external_uidx" in migration
    assert "a6_managed_listings_create_action_uidx" in migration
    assert "WHERE a6_channel_id IS NOT NULL" in migration
    assert "WHERE a6_listing_id IS NOT NULL" in migration
    assert "a6_managed_channels_merchant_state_idx" in migration
    assert "a6_managed_listings_channel_binding_idx" in migration
    assert "a6_managed_listings_merchant_state_idx" in migration


def test_managed_supplier_resources_retain_control_timestamps() -> None:
    migration = Path("migrations/0005_managed_supplier_resources.sql").read_text()
    timestamps = (
        "first_attempted_at",
        "last_attempted_at",
        "last_observed_at",
        "verified_at",
        "created_at",
        "updated_at",
    )

    assert all(migration.count(field) >= 2 for field in timestamps)
    assert migration.count("last_error_class text") == 2


def test_apply_migrations_records_checksum_and_skips_second_run(tmp_path: Path) -> None:
    migration_dir = tmp_path / "migrations"
    migration_dir.mkdir()
    sql = "CREATE TABLE IF NOT EXISTS a6_trade.fixture (id bigint PRIMARY KEY);\n"
    (migration_dir / "0001_fixture.sql").write_text(sql)
    connection = FakeConnection()

    assert apply_migrations(connection, migration_dir) == ("0001",)
    first_sql_execution_count = sum(query == sql for query, _ in connection.calls)
    assert apply_migrations(connection, migration_dir) == ()

    assert first_sql_execution_count == 1
    assert sum(query == sql for query, _ in connection.calls) == 1
    assert connection.migrations["0001"] == hashlib.sha256(sql.encode()).hexdigest()
    assert connection.commits == 2
    assert connection.rollbacks == 0


def test_apply_migrations_rejects_changed_applied_sql(tmp_path: Path) -> None:
    migration_dir = tmp_path / "migrations"
    migration_dir.mkdir()
    migration = migration_dir / "0001_fixture.sql"
    migration.write_text("SELECT 1;\n")
    connection = FakeConnection()
    apply_migrations(connection, migration_dir)
    migration.write_text("SELECT 2;\n")

    with pytest.raises(MigrationError, match="migration_checksum_mismatch:0001"):
        apply_migrations(connection, migration_dir)

    assert connection.rollbacks == 1


def test_session_import_extracts_only_approved_metadata(tmp_path: Path) -> None:
    path = session_file(tmp_path / "private" / "session.json", 1)
    metadata = metadata_from_session(
        path,
        roles=("seller",),
        proxy_metadata={
            "lease_id": "lease-1",
            "proxy_fingerprint": "proxy-fingerprint-1",
            "region": "us-west",
        },
    )

    assert metadata.user_id == 1001
    assert metadata.supplier_id == 2001
    assert metadata.quota_per_unit == 500_001
    assert metadata.status == "supplier_submitted"
    assert metadata.operating_group == "standard"
    assert metadata.roles == ("seller",)
    assert metadata.proxy_lease_id == "lease-1"
    assert metadata.proxy_fingerprint == "proxy-fingerprint-1"
    assert len(metadata.session_fingerprint) == 64
    assert_metadata_is_secret_free(metadata)

    serialized = repr(metadata.__dict__)
    assert "merchant-1@fixture.invalid" not in serialized
    assert "password-secret-1" not in serialized
    assert "cookie-secret-1" not in serialized
    assert "sk-secret-1" not in serialized


def test_upsert_preserves_existing_proxy_lease_when_import_has_no_proxy_metadata() -> None:
    normalized = " ".join(UPSERT_MERCHANT_SQL.split())

    assert (
        "proxy_lease_id = coalesce( EXCLUDED.proxy_lease_id, a6_trade.a6_merchants.proxy_lease_id )"
    ) in normalized
    assert "WHEN EXCLUDED.proxy_lease_id IS NULL" in normalized
    assert "THEN a6_trade.a6_merchants.proxy_metadata" in normalized
    assert "WHEN EXCLUDED.operating_group <> 'standard'" in normalized


def test_session_import_persists_focus_operating_group(tmp_path: Path) -> None:
    path = session_file(tmp_path / "private" / "session.json", 1)
    payload = json.loads(path.read_text())
    payload["operating_group"] = "重点组"
    write_private_json(path, payload)

    metadata = metadata_from_session(path)
    connection = FakeConnection()
    upsert_merchant_metadata(connection, (metadata,))

    assert metadata.operating_group == "重点组"
    assert connection.merchants[str(path.resolve())][6] == "重点组"


def test_session_fingerprint_tracks_cookie_but_not_other_secret_values(tmp_path: Path) -> None:
    path = session_file(tmp_path / "private" / "session.json", 1)
    before = metadata_from_session(path)
    payload = {
        "schema_version": 1,
        "origin": "https://fixture.invalid",
        "email": "different@fixture.invalid",
        "username": "different",
        "password": "changed-password",
        "cookies": {"session": "cookie-secret-1"},
        "a6api_key": "sk-rotated",
        "user_id": 1001,
        "supplier_id": 2001,
        "status": "supplier_submitted",
    }
    write_private_json(path, payload)

    assert metadata_from_session(path).session_fingerprint == before.session_fingerprint
    payload["cookies"] = {"session": "rotated-cookie"}
    write_private_json(path, payload)
    assert metadata_from_session(path).session_fingerprint != before.session_fingerprint


def test_proxy_metadata_rejects_urls_credentials_and_unknown_fields(tmp_path: Path) -> None:
    path = session_file(tmp_path / "private" / "session.json", 1)

    with pytest.raises(ValueError, match="proxy_metadata_contains_secret_material"):
        metadata_from_session(
            path,
            proxy_metadata={"provider": "socks5://user:password@proxy.invalid:1080"},
        )
    with pytest.raises(ValueError, match="proxy_metadata_contains_unapproved_fields"):
        metadata_from_session(path, proxy_metadata={"proxy_url": "fixture"})


def test_load_and_upsert_nineteen_sessions_is_idempotent_and_secret_free(
    tmp_path: Path,
) -> None:
    paths = [
        session_file(tmp_path / "private" / f"session-{index}.json", index) for index in range(19)
    ]
    duplicate_paths = [*paths, paths[0]]
    roles = {str(path.resolve()): ("seller",) for path in paths}
    proxy = {
        str(path.resolve()): {
            "lease_id": f"lease-{index}",
            "proxy_fingerprint": f"proxy-fingerprint-{index}",
            "pool": "phase0",
        }
        for index, path in enumerate(paths)
    }
    metadata = load_session_metadata(
        duplicate_paths,
        roles_by_ref=roles,
        proxy_metadata_by_ref=proxy,
    )
    connection = FakeConnection()

    assert len(metadata) == 19
    assert upsert_merchant_metadata(connection, metadata) == 19
    assert upsert_merchant_metadata(connection, reversed(metadata)) == 19
    assert len(connection.merchants) == 19
    assert connection.commits == 2
    assert connection.rollbacks == 0

    parameters = repr(tuple(connection.merchants.values()))
    assert "@fixture.invalid" not in parameters
    assert "password-secret" not in parameters
    assert "cookie-secret" not in parameters
    assert "sk-secret" not in parameters
    assert all("ON CONFLICT (session_ref) DO UPDATE" in query for query, _ in connection.calls)
    assert all("roles = a6_trade.a6_merchants.roles" in query for query, _ in connection.calls)


def test_abandoned_session_tombstones_existing_merchant_without_dropping_identity(
    tmp_path: Path,
) -> None:
    path = session_file(tmp_path / "private" / "session.json", 1)
    connection = FakeConnection()
    upsert_merchant_metadata(connection, (metadata_from_session(path),))

    payload = json.loads(path.read_text())
    payload.update(status="abandoned", user_id=None, supplier_id=None)
    write_private_json(path, payload)
    tombstones = load_session_metadata((path,))
    upsert_merchant_metadata(connection, tombstones)

    assert len(tombstones) == 1
    assert tombstones[0].status == "abandoned"
    persisted = connection.merchants[str(path.resolve())]
    assert persisted[2] == 1001
    assert persisted[3] == 2001
    assert persisted[5] == "abandoned"
    normalized = " ".join(UPSERT_MERCHANT_SQL.split())
    assert "WHEN EXCLUDED.status = 'abandoned'" in normalized
    assert "coalesce(a6_trade.a6_merchants.a6_user_id, EXCLUDED.a6_user_id)" in normalized
    assert "coalesce(a6_trade.a6_merchants.supplier_id, EXCLUDED.supplier_id)" in normalized


def test_merchant_metadata_rejects_unknown_roles() -> None:
    with pytest.raises(ValueError, match="merchant_roles_invalid"):
        MerchantMetadata(
            session_ref="session:1",
            session_fingerprint="a" * 64,
            user_id=1,
            supplier_id=2,
            status="active",
            roles=("owner",),
        )


@pytest.mark.parametrize(
    "roles",
    [
        ("seller", "observer", "probe"),
        ("seller", "seller"),
        ("observer", "seller"),
        ("unassigned", "provider"),
    ],
)
def test_merchant_metadata_rejects_noncanonical_roles(roles: tuple[str, ...]) -> None:
    with pytest.raises(ValueError, match="merchant_roles_invalid"):
        MerchantMetadata(
            session_ref="session:1",
            session_fingerprint="a" * 64,
            user_id=1,
            supplier_id=2,
            status="active",
            roles=roles,
        )


def test_session_metadata_rejects_boolean_quota_per_unit(tmp_path: Path) -> None:
    path = session_file(tmp_path / "private" / "session.json", 1)
    payload = json.loads(path.read_text())
    payload["quota_per_unit"] = True
    write_private_json(path, payload)

    with pytest.raises(ValueError, match="quota_per_unit_invalid"):
        metadata_from_session(path)


def test_session_reimport_preserves_previously_assigned_roles(tmp_path: Path) -> None:
    path = session_file(tmp_path / "private" / "session.json", 1)
    assigned = metadata_from_session(path, roles=("provider",))
    default_import = metadata_from_session(path)
    connection = FakeConnection()

    upsert_merchant_metadata(connection, (assigned,))
    upsert_merchant_metadata(connection, (default_import,))

    persisted = connection.merchants[str(path.resolve())]
    assert persisted[7] == ["provider"]
    assert persisted[4] == 500_001
    upsert_sql = next(
        query for query, _ in connection.calls if "INSERT INTO a6_trade.a6_merchants" in query
    )
    assert "roles = a6_trade.a6_merchants.roles" in upsert_sql


def test_provider_only_metadata_is_preserved_for_retired_seller() -> None:
    metadata = MerchantMetadata(
        session_ref="session:retired-provider",
        session_fingerprint="a" * 64,
        user_id=1,
        supplier_id=2,
        status="supplier_submitted",
        roles=("provider",),
    )

    assert metadata.roles == ("provider",)


def test_provider_observer_metadata_is_preserved_for_retired_seller() -> None:
    metadata = MerchantMetadata(
        session_ref="session:retired-provider-observer",
        session_fingerprint="a" * 64,
        user_id=1,
        supplier_id=2,
        status="supplier_submitted",
        roles=("provider", "observer"),
    )

    assert metadata.roles == ("provider", "observer")


def test_focus_operating_group_migration_is_packaged() -> None:
    migration = Path("migrations/0043_focus_operating_group.sql").read_text()
    packaged = Path("src/a6api_auto/data/migrations/0043_focus_operating_group.sql").read_text()

    assert packaged == migration
    assert "operating_group text NOT NULL DEFAULT 'standard'" in migration
    assert "a6_merchants_operating_group_idx" in migration


def test_seller_retirement_migration_is_packaged_and_allows_provider_only() -> None:
    migration = Path("migrations/0014_seller_retirement.sql").read_text()
    packaged = Path("src/a6api_auto/data/migrations/0014_seller_retirement.sql").read_text()

    assert packaged == migration
    assert "roles = ARRAY['provider']::text[]" in migration
    assert "a6_seller_retirement_plans" in migration
    assert "a6_seller_retirement_events" in migration


def test_retired_provider_observer_migration_is_packaged_and_disables_growth() -> None:
    migration = Path("migrations/0020_retired_provider_observer.sql").read_text()
    packaged = Path("src/a6api_auto/data/migrations/0020_retired_provider_observer.sql").read_text()

    assert packaged == migration
    assert "roles = ARRAY['provider', 'observer']::text[]" in migration
    assert "retired_observer_duty_added" in migration
    assert "a6_channel_growth_policies" in migration
    assert "callback_enabled = false" in migration


def test_channel_growth_autoonboard_migration_is_packaged_and_role_scoped() -> None:
    migration = Path("migrations/0025_channel_growth_autoonboard.sql").read_text()
    packaged = Path(
        "src/a6api_auto/data/migrations/0025_channel_growth_autoonboard.sql"
    ).read_text()

    assert packaged == migration
    assert "a6_managed_listing_growth_policy_sync" in migration
    assert "a6_merchant_growth_duties_sync" in migration
    assert "degraded_success_rate" in migration
    assert "8000" in migration
    assert "merchant.roles @> ARRAY['seller']::text[]" in migration
    assert "merchant.roles @> ARRAY['provider']::text[]" in migration


def test_provider_topn_migration_is_packaged_with_generation_and_slot_contracts() -> None:
    migration = Path("migrations/0026_provider_topn_failover.sql").read_text()
    packaged = Path("src/a6api_auto/data/migrations/0026_provider_topn_failover.sql").read_text()

    assert packaged == migration
    assert "a6_trade.provider_cost_generations" in migration
    assert "a6_trade.provider_pool_generations" in migration
    assert "a6_trade.provider_pool_slots" in migration
    assert "a6_trade.provider_request_envelopes" in migration
    assert "a6_trade.provider_attempts" in migration
    assert "a6_trade.model_publications" in migration
    assert "provider_cost_one_active_idx" in migration
    assert "provider_pool_one_active_idx" in migration
    assert "provider_pool_current_slot_idx" in migration
    assert "provider_attempt_one_winner_idx" in migration
    assert "liquidity_state text NOT NULL DEFAULT 'FUNDED'" in migration
    assert "endpoint_host text NOT NULL DEFAULT 'a6.a6api.com'" in migration
    assert "max_retry_cost_micros bigint" in migration
    assert "UNIQUE (request_id, slot_no)" in migration
    assert "UNIQUE (attempt_id, request_id)" in migration
    assert "FOREIGN KEY (request_envelope_id, request_id)" in migration
    assert "FOREIGN KEY (winning_attempt_id, request_id)" in migration
    assert "DEFERRABLE INITIALLY DEFERRED" in migration
    assert "provider_attempt_response_hash_valid" in migration


def test_provider_priority_capacity_migration_is_packaged_and_expands_range() -> None:
    migration = Path("migrations/0027_provider_priority_capacity.sql").read_text()
    packaged = Path(
        "src/a6api_auto/data/migrations/0027_provider_priority_capacity.sql"
    ).read_text()

    assert packaged == migration
    assert "DROP CONSTRAINT enodot_binding_priority_range" in migration
    assert "desired_priority BETWEEN 0 AND 32767" in migration


def test_residual_temporary_provider_migration_is_packaged() -> None:
    migration = Path("migrations/0035_residual_temporary_providers.sql").read_text()
    packaged = Path(
        "src/a6api_auto/data/migrations/0035_residual_temporary_providers.sql"
    ).read_text()

    assert migration == packaged
    assert "residual_provider_policies" in migration
    assert "reserve_balance_quota" in migration
    assert "residual_provider_events" in migration


def test_latest_exchange_index_migration_is_packaged() -> None:
    migration = Path("migrations/0044_market_quotes_latest_exchange_index.sql").read_text()
    packaged = Path(
        "src/a6api_auto/data/migrations/0044_market_quotes_latest_exchange_index.sql"
    ).read_text()

    assert migration == packaged
    assert "a6_market_quotes_latest_exchange_idx" in migration
    assert "observed_at DESC, id DESC" in migration


def test_seller_plan_latest_index_migration_is_packaged() -> None:
    migration = Path("migrations/0045_seller_plan_latest_index.sql").read_text()
    packaged = Path("src/a6api_auto/data/migrations/0045_seller_plan_latest_index.sql").read_text()

    assert migration == packaged
    assert "trade_control_actions_seller_plan_latest_idx" in migration
    assert "(desired_state ->> 'model')" in migration
    assert "(desired_state ->> 'merchant_id')" in migration


def test_provider_regional_coordination_migration_is_packaged_and_least_privileged() -> None:
    migration = Path("migrations/0042_provider_regional_coordination.sql").read_text()
    packaged = Path(
        "src/a6api_auto/data/migrations/0042_provider_regional_coordination.sql"
    ).read_text()

    assert packaged == migration
    for table in (
        "provider_region_projection_receipts",
        "provider_region_capacity_allocations",
        "provider_region_capacity_receipts",
    ):
        assert f"a6_trade.{table}" in migration
    assert "provider_region_projection_receipt_guard" in migration
    assert "provider_region_capacity_receipt_guard" in migration
    assert "provider_region_capacity_allocation_guard" in migration
    assert "provider_region_binding_capacity_guard" in migration
    assert "DEFERRABLE INITIALLY DEFERRED" in migration
    assert "ARRAY['sub2api_cm', 'sub2api']" in migration
    assert "a6_provider_projector" in migration
    assert "a6_provider_subscriber" in migration
    assert "REVOKE ALL ON TABLE" in migration
    assert "GRANT SELECT ON TABLE" in migration
    assert "GRANT UPDATE (subscriber_cursor, subscriber_connected" in migration
    assert "GRANT UPDATE (valid_to, updated_at)" in migration


def test_provider_pin_failure_streak_migration_is_packaged_and_constrained() -> None:
    migration = Path("migrations/0051_provider_pin_failure_streak.sql").read_text()
    packaged = Path(
        "src/a6api_auto/data/migrations/0051_provider_pin_failure_streak.sql"
    ).read_text()

    assert packaged == migration
    assert "failure_streak_count integer NOT NULL DEFAULT 0" in migration
    assert "failure_streak_triggered boolean NOT NULL DEFAULT false" in migration
    assert "provider_attempt_pin_terminal_streak_idx" in migration


def test_seller_price_jump_cooldown_migration_is_packaged_and_bounded() -> None:
    migration = Path("migrations/0052_seller_price_jump_cooldowns.sql").read_text()
    packaged = Path(
        "src/a6api_auto/data/migrations/0052_seller_price_jump_cooldowns.sql"
    ).read_text()

    assert packaged == migration
    assert "seller_price_jump_cooldowns" in migration
    assert "trigger_price_multiplier >= threshold_ratio * baseline_price_multiplier" in migration
    assert "cooling_until >= started_at + interval '30 minutes'" in migration
    assert "cooling_until <= started_at + interval '45 minutes'" in migration
    assert "WHERE state IN ('cooling', 'release_pending')" in migration


def test_model_profit_setting_migration_is_packaged_and_notifies_reconciler() -> None:
    migration = Path("migrations/0065_model_profit_settings.sql").read_text()
    packaged = Path(
        "src/a6api_auto/data/migrations/0065_model_profit_settings.sql"
    ).read_text()

    assert packaged == migration
    assert "a6_trade.model_profit_settings" in migration
    assert "minimum_profit_on_cost_bps integer NOT NULL DEFAULT 6000" in migration
    assert "VALUES ('gpt-5.6-sol', 6000" in migration
    assert "notify_economic_model_fact_changed" in migration
    assert "GRANT SELECT, INSERT, UPDATE, DELETE" in migration
