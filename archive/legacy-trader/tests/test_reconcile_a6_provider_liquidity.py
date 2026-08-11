from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / "scripts" / "reconcile_a6_provider_liquidity.py"
SPEC = importlib.util.spec_from_file_location("reconcile_a6_provider_liquidity", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


def test_apply_is_default_off_and_requires_exact_confirmation() -> None:
    args = module.parser().parse_args([])
    assert args.apply is False
    assert args.apply_confirmation == ""
    assert args.model == "gpt-5.6-sol"

    with pytest.raises(module.ProviderLiquidityCommandError):
        module.authorize(enabled=True, apply=True, confirmation="wrong")
    assert (
        module.authorize(
            enabled=True,
            apply=True,
            confirmation=module.APPLY_CONFIRMATION,
        )
        is None
    )


def test_now_is_timezone_aware_on_the_production_python_runtime() -> None:
    assert module._now().utcoffset() is not None


def test_disabled_policy_returns_before_runtime_dependencies() -> None:
    result = module.authorize(enabled=False, apply=True, confirmation="")

    assert result == {
        "success": True,
        "enabled": False,
        "mode": "disabled",
        "action": "disabled",
        "remote_write_count_min": 0,
    }


def test_systemd_unit_has_two_apply_gates_and_shared_provider_lock() -> None:
    root = Path(__file__).parents[1]
    service = (
        root / "deploy/systemd/a6-trader-provider-liquidity.service"
    ).read_text(encoding="utf-8")
    timer = (
        root / "deploy/systemd/a6-trader-provider-liquidity.timer"
    ).read_text(encoding="utf-8")
    path = (
        root / "deploy/systemd/a6-trader-provider-liquidity.path"
    ).read_text(encoding="utf-8")

    assert "PROVIDER_LIQUIDITY_RECONCILE_ENABLED" in service
    assert "--apply-confirmation a6-provider-liquidity-v1" in service
    assert "/var/lib/a6-trader/provider-control.lock" in service
    assert "/var/lib/a6-trader/seller-control.lock" not in service
    assert "--apply" in service
    assert "OnSuccess=a6-trader-observer.service" in service
    assert "PathChanged=/run/a6-trader/provider-liquidity-request" in path
    assert "Unit=a6-trader-provider-liquidity.service" in path
    assert "OnUnitInactiveSec=15s" in timer
    assert "RandomizedDelaySec=0" in timer


def test_migration_pairs_match_and_define_shared_reservation_contract() -> None:
    root = Path(__file__).parents[1]
    migration = root / "migrations/0049_provider_liquidity_reconcile.sql"
    packaged = (
        root
        / "src/a6api_auto/data/migrations/0049_provider_liquidity_reconcile.sql"
    )
    sql = migration.read_text(encoding="utf-8")

    assert sql == packaged.read_text(encoding="utf-8")
    assert "merchant_fund_reservations" in sql
    assert "merchant_fund_reservation_one_active_idx" in sql
    assert "WHERE reservation_state = 'active'" in sql
    assert "provider_fund_reserves" in sql
    assert "provider_liquidity_events" in sql
    assert "fund_reservation_id" in sql
