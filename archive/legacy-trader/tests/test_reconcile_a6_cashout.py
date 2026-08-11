from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / "scripts" / "reconcile_a6_cashout.py"
SPEC = importlib.util.spec_from_file_location("reconcile_a6_cashout", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


def test_cashout_command_is_default_off_and_apply_needs_all_gates() -> None:
    args = module.parser().parse_args([])

    assert args.apply is False
    assert args.sync_wallet_inventory is False
    assert args.activate_wallets is False
    assert args.assign_wallets is False
    assert args.apply_confirmation == ""
    assert (
        module.authorize(
            enabled=False,
            apply=True,
            apply_confirmation="",
            standard_apply_enabled=True,
            callback_apply_enabled=False,
        )["mode"]
        == "disabled"
    )
    with pytest.raises(module.CashoutCommandError, match="cashout_apply_confirmation_invalid"):
        module.authorize(
            enabled=True,
            apply=True,
            apply_confirmation="wrong",
            standard_apply_enabled=True,
            callback_apply_enabled=False,
        )
    with pytest.raises(module.CashoutCommandError, match="cashout_apply_methods_disabled"):
        module.authorize(
            enabled=True,
            apply=True,
            apply_confirmation=module.APPLY_CONFIRMATION,
            standard_apply_enabled=False,
            callback_apply_enabled=False,
        )


def test_disabled_command_returns_before_database_or_secret_reads(monkeypatch) -> None:
    monkeypatch.delenv("A6_TRADE_DATABASE_DSN", raising=False)
    monkeypatch.delenv(module.ENABLE_ENV, raising=False)

    result = module.run(module.parser().parse_args(["--apply"]))

    assert result == {
        "success": True,
        "enabled": False,
        "mode": "disabled",
        "remote_write_count": 0,
        "plans": [],
    }


def test_apply_off_still_reconciles_existing_crypto_intents(monkeypatch) -> None:
    monkeypatch.setenv(module.ENABLE_ENV, "1")
    monkeypatch.setenv("A6_TRADE_DATABASE_DSN", "fixture-dsn")
    monkeypatch.delenv(module.STANDARD_APPLY_ENV, raising=False)
    monkeypatch.delenv(module.CALLBACK_APPLY_ENV, raising=False)
    calls = []

    class Store:
        def __init__(self, connection_factory, **kwargs):
            calls.append(("store", connection_factory, kwargs))

        def load_candidates(self, _now, *, policy, limit):
            calls.append(("candidates", policy, limit))
            return ()

    class Worker:
        def __init__(self, store, reconciler, **kwargs):
            calls.append(("worker", store, reconciler, kwargs))

        def run_once(self, *, max_intents):
            calls.append(("reconcile", max_intents))
            return {
                "success": True,
                "claimed_count": 1,
                "confirmed_count": 1,
                "pending_count": 0,
                "unknown_count": 0,
                "failed_final_count": 0,
                "remote_write_count": 0,
            }

    monkeypatch.setattr(module, "PostgresCashoutStore", Store)
    monkeypatch.setattr(module, "CashoutReconciliationWorker", Worker)

    result = module.run(module.parser().parse_args([]))

    assert result["mode"] == "plan-only"
    assert result["remote_write_count"] == 0
    assert result["reconciliation"]["confirmed_count"] == 1
    assert ("reconcile", 100) in calls


def test_systemd_schedule_applies_after_fresh_collector_with_confirmation() -> None:
    root = Path(__file__).parents[1]
    service = (root / "deploy/systemd/a6-trader-cashout.service").read_text()
    timer = (root / "deploy/systemd/a6-trader-cashout.timer").read_text()
    drop_in = (root / "deploy/systemd/a6-trader-collector.service.d/cashout.conf").read_text()

    assert "TREASURY_CASHOUT_ENABLED" in service
    assert "--stage plan" in service
    assert "--apply --apply-confirmation a6-risk-balanced-cashout-v2" in service
    assert "treasury-cashout.lock" in service
    assert "OnUnitInactiveSec=5min" in timer
    assert "OnSuccess=a6-trader-cashout.service" in drop_in
