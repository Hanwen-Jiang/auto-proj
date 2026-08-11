from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import shadow_reconcile_a6_provider_pool as command
from scripts.audit_a6_provider_shadow_journal import audit_journal_lines
from scripts.shadow_reconcile_a6_provider_pool import (
    ProviderPoolShadowCommandError,
    _document,
    append_shadow_audit_record,
    run,
)
from tests.test_audit_a6_provider_shadow_journal import shadow_message


def test_shadow_timer_reschedules_after_long_run_or_timer_restart() -> None:
    timer = Path("deploy/systemd/a6-trader-provider-pool-shadow.timer").read_text()

    assert "OnActiveSec=5s" in timer
    assert "OnUnitInactiveSec=30s" in timer
    assert "OnUnitActiveSec=" not in timer


def test_observer_round_scans_every_configured_observer_without_a_fixed_cap() -> None:
    service = Path("deploy/systemd/a6-trader-observer.service").read_text()
    timer = Path("deploy/systemd/a6-trader-observer.timer").read_text()
    target = Path("deploy/systemd/a6-trader-control.target").read_text()

    assert "--min-observers 2" in service
    assert "--all-observers" in service
    assert "--observer-round-size" not in service
    assert "--top-k 12" in service
    assert "OnSuccess=a6-trader-provider-auto-pool.service" in service
    assert "TimeoutStartSec=30s" in service
    assert "OnCalendar=*-*-* *:*:00" in timer
    assert "OnCalendar=*-*-* *:*:00,30" not in timer
    assert "OnUnitActiveSec=" not in timer
    assert "Wants=a6-trader-observer.timer" not in target
    assert "Wants=a6-trader-provider-pool-shadow" not in target


def test_shadow_does_not_wait_for_an_in_progress_observer_job() -> None:
    service = Path("deploy/systemd/a6-trader-provider-pool-shadow.service").read_text()

    assert "After=network-online.target postgresql.service" in service
    assert (
        "After=network-online.target postgresql.service a6-trader-observer.service" not in service
    )


def test_provider_inventory_freshness_default_matches_production_services() -> None:
    args = command.parser().parse_args(["--model", "gpt-5.6-sol"])

    assert args.inventory_freshness_seconds == 900
    assert args.maximum_provider_cost_cny == Decimal("0.3")
    assert args.transient_retry_attempts == 3
    assert args.transient_retry_delay_seconds == 3
    for service_path in (
        "deploy/systemd/a6-trader-provider-pool-shadow.service",
        "deploy/systemd/a6-trader-provider-pool-apply.service",
        "deploy/systemd/a6-trader-provider-failure-repair.service",
    ):
        service = Path(service_path).read_text()
        assert "--inventory-freshness-seconds 900" in service
        assert "--inventory-freshness-seconds 360" not in service


def test_provider_services_use_the_reviewed_all_funded_pool_limit() -> None:
    for service_path in (
        "deploy/systemd/a6-trader-provider-pool-shadow.service",
        "deploy/systemd/a6-trader-provider-pool-apply.service",
        "deploy/systemd/a6-trader-provider-failure-repair.service",
    ):
        service = Path(service_path).read_text()
        assert "--initial-max-active-slots 12" in service
        assert "--standby-count 0" in service
        assert "--standby-count 12" not in service
        assert "--margin-bps 2000" in service
        if "provider-pool" in service_path:
            assert "--maximum-provider-cost-cny 0.3" in service
        assert "a6-provider-topn-failover-v4-all-funded-dynamic-ready" in service

    shadow = Path("deploy/systemd/a6-trader-provider-pool-shadow.service").read_text()
    apply = Path("deploy/systemd/a6-trader-provider-pool-apply.service").read_text()
    assert "--minimum-active-slots 4" in shadow
    assert "--minimum-active-slots 4" in apply
    assert "--required-ready-slots" not in apply


def test_shadow_service_persists_dedicated_audit_evidence() -> None:
    service = Path("deploy/systemd/a6-trader-provider-pool-shadow.service").read_text()
    rotation = Path("deploy/logrotate/a6-trader-provider-pool-shadow").read_text()

    assert "--audit-log-file /var/lib/a6-trader/provider-pool-shadow-observations.jsonl" in service
    assert "ReadWritePaths=/var/lib/a6-trader" in service
    assert "/var/lib/a6-trader/provider-pool-shadow-observations.jsonl" in rotation
    assert "create 0600 a6trader a6trader" in rotation
    assert "rotate 7" in rotation
    assert "copytruncate" not in rotation
    assert "--transient-retry-attempts 3" in service
    assert "--transient-retry-delay-seconds 3" in service


def vacant_document() -> dict:
    return {
        "model": "gpt-5.6-sol",
        "routing_generation": 1,
        "parent_routing_generation": None,
        "cost_generation": 1,
        "parent_cost_generation": None,
        "generation_source": "observer-shadow",
        "source_hash": "a" * 64,
        "cost_input_hash": "b" * 64,
        "ordered_channel_ids": [],
        "standby_channel_ids": [],
        "desired_size": 1,
        "slots": [
            {
                "slot_no": 1,
                "channel_id": None,
                "rank": None,
                "priority": None,
                "assignment_id": None,
                "token_id": None,
                "account_id": None,
                "slot_state": "VACANT",
                "funding_eligible": False,
                "desired_schedulable": False,
                "observed_schedulable": False,
                "pin_confirmed": False,
                "probe_confirmed": False,
            }
        ],
        "cost_plan": {
            "desired_slot_costs": {},
            "retry_reserve_multiplier": "0",
            "bootstrap_retry_reserve": "0",
            "realized_cost_p95_multiplier": None,
            "previous_billing_floor": "1.25",
        },
    }


def test_shadow_command_defaults_to_validation_only() -> None:
    result = run(vacant_document(), persist=False)

    assert result == {
        "success": True,
        "mode": "shadow",
        "model": "gpt-5.6-sol",
        "diff_kind": "COST_ONLY",
        "routing_generation": 1,
        "cost_generation": 1,
        "persisted": False,
        "activated": False,
        "remote_write_count": 0,
    }


def test_shadow_command_persist_requires_database_factory() -> None:
    with pytest.raises(
        ProviderPoolShadowCommandError,
        match="provider_shadow_connection_factory_missing",
    ):
        run(vacant_document(), persist=True)


def test_shadow_postgres_factory_bounds_database_work(monkeypatch) -> None:
    sentinel = object()
    calls: list[tuple[str, dict[str, object]]] = []

    def connect(dsn: str, **kwargs: object) -> object:
        calls.append((dsn, kwargs))
        return sentinel

    monkeypatch.setattr(command.psycopg, "connect", connect)

    assert command._postgres_connection_factory("dbname=fixture")() is sentinel
    assert calls == [
        (
            "dbname=fixture",
            {
                "row_factory": command.dict_row,
                "application_name": "a6-provider-pool-shadow",
                "connect_timeout": 5,
                "options": (
                    "-c statement_timeout=45000 "
                    "-c lock_timeout=5000 "
                    "-c idle_in_transaction_session_timeout=50000"
                ),
            },
        )
    ]


def test_shadow_postgres_factory_rejects_empty_dsn() -> None:
    with pytest.raises(
        ProviderPoolShadowCommandError,
        match="provider_shadow_database_dsn_missing",
    ):
        command._postgres_connection_factory("  ")


def test_shadow_command_reads_object_document(tmp_path) -> None:
    path = tmp_path / "shadow.json"
    path.write_text(json.dumps(vacant_document()), encoding="utf-8")

    assert _document(path) == vacant_document()

    path.write_text("[]", encoding="utf-8")
    with pytest.raises(ProviderPoolShadowCommandError, match="document_invalid"):
        _document(path)


def test_shadow_audit_log_is_private_and_machine_verifiable(tmp_path) -> None:
    path = tmp_path / "shadow-observations.jsonl"
    started = datetime(2026, 7, 29, 10, 32, 40, tzinfo=timezone.utc)

    for index in range(2):
        generation = 1120 + index
        append_shadow_audit_record(
            path,
            json.loads(
                shadow_message(
                    generation,
                    sequence=105_991 + index,
                )
            ),
            observed_at=started + timedelta(seconds=30 * index),
        )

    assert path.stat().st_mode & 0o777 == 0o600
    report = audit_journal_lines(
        path.read_text(encoding="ascii").splitlines(),
        model="gpt-5.6-sol",
        minimum_window_seconds=30,
        maximum_gap_seconds=31,
    )
    assert report["success"] is True
    assert report["observation_count"] == 2
    assert report["window_start"] == started.isoformat()


def test_shadow_audit_log_rejects_unsafe_paths_and_files(tmp_path) -> None:
    with pytest.raises(
        ProviderPoolShadowCommandError,
        match="provider_shadow_audit_path_invalid",
    ):
        append_shadow_audit_record(Path("relative.jsonl"), {})

    path = tmp_path / "wide.jsonl"
    path.write_text("", encoding="ascii")
    path.chmod(0o644)
    with pytest.raises(
        ProviderPoolShadowCommandError,
        match="provider_shadow_audit_write_failed",
    ):
        append_shadow_audit_record(path, {})

    target = tmp_path / "target.jsonl"
    target.write_text("", encoding="ascii")
    target.chmod(0o600)
    symlink = tmp_path / "symlink.jsonl"
    symlink.symlink_to(target)
    with pytest.raises(
        ProviderPoolShadowCommandError,
        match="provider_shadow_audit_write_failed",
    ):
        append_shadow_audit_record(symlink, {})

    hardlink = tmp_path / "hardlink.jsonl"
    hardlink.hardlink_to(target)
    with pytest.raises(
        ProviderPoolShadowCommandError,
        match="provider_shadow_audit_write_failed",
    ):
        append_shadow_audit_record(hardlink, {})


def test_shadow_audit_log_rolls_back_partial_write(tmp_path, monkeypatch) -> None:
    path = tmp_path / "shadow-observations.jsonl"
    append_shadow_audit_record(path, {"result": "complete"})
    complete_prefix = path.read_bytes()
    real_write = command.os.write

    def partial_write_then_fail(descriptor, payload) -> int:
        real_write(descriptor, payload[:17])
        raise OSError("fixture filesystem full")

    monkeypatch.setattr(command.os, "write", partial_write_then_fail)
    with pytest.raises(
        ProviderPoolShadowCommandError,
        match="provider_shadow_audit_write_failed",
    ):
        append_shadow_audit_record(path, {"result": "interrupted"})

    assert path.read_bytes() == complete_prefix
    monkeypatch.setattr(command.os, "write", real_write)
    append_shadow_audit_record(path, {"result": "next"})
    envelopes = [json.loads(line) for line in path.read_text().splitlines()]
    assert [json.loads(item["MESSAGE"])["result"] for item in envelopes] == [
        "complete",
        "next",
    ]


def test_shadow_cli_audits_early_database_configuration_failure(tmp_path, monkeypatch) -> None:
    path = tmp_path / "shadow-observations.jsonl"
    monkeypatch.delenv("A6_TRADE_DATABASE_DSN", raising=False)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "shadow_reconcile_a6_provider_pool.py",
            "--model",
            "gpt-5.6-sol",
            "--persist",
            "--audit-log-file",
            str(path),
        ],
    )

    with pytest.raises(
        ProviderPoolShadowCommandError,
        match="provider_shadow_database_dsn_missing",
    ):
        command.main()

    envelope = json.loads(path.read_text(encoding="ascii"))
    failure = json.loads(envelope["MESSAGE"])
    assert failure == {
        "success": False,
        "mode": "shadow",
        "model": "gpt-5.6-sol",
        "activated": False,
        "plan_write_count": 0,
        "remote_write_count": 0,
        "failure_type": "ProviderPoolShadowCommandError",
        "failure_code": "provider_shadow_database_dsn_missing",
        "attempt_count": 1,
    }


def test_shadow_cli_success_audits_retry_attempt_count(tmp_path, monkeypatch) -> None:
    path = tmp_path / "shadow-observations.jsonl"
    monkeypatch.setenv("A6_TRADE_DATABASE_DSN", "dbname=fixture")
    monkeypatch.setenv("PROVIDER_INVENTORY_RECONCILE_ENABLED", "false")
    monkeypatch.setattr(
        command,
        "_postgres_connection_factory",
        lambda _dsn: lambda: object(),
    )
    monkeypatch.setattr(
        command,
        "run_model_with_retry",
        lambda *_args, **_kwargs: (
            {
                "success": True,
                "mode": "shadow",
                "model": "gpt-5.6-sol",
                "activated": False,
                "plan_write_count": 1,
                "remote_write_count": 0,
            },
            3,
        ),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "shadow_reconcile_a6_provider_pool.py",
            "--model",
            "gpt-5.6-sol",
            "--persist",
            "--audit-log-file",
            str(path),
        ],
    )

    command.main()

    envelope = json.loads(path.read_text(encoding="ascii"))
    success = json.loads(envelope["MESSAGE"])
    assert success["success"] is True
    assert success["attempt_count"] == 3
    assert "failure_type" not in success
    assert "failure_code" not in success


def test_shadow_model_retries_only_transient_snapshot_failures(monkeypatch) -> None:
    calls = 0
    sleeps: list[float] = []

    def run_model(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls < 3:
            raise command.ProviderPoolRankError("provider_snapshot_observer_quorum_insufficient")
        return {"success": True}

    monkeypatch.setattr(command, "run_model", run_model)

    result, attempt_count = command.run_model_with_retry(
        "gpt-5.6-sol",
        persist=True,
        connection_factory=lambda: object(),
        retry_attempts=3,
        retry_delay_seconds=2.5,
        sleep=sleeps.append,
    )

    assert result == {"success": True}
    assert attempt_count == 3
    assert calls == 3
    assert sleeps == [2.5, 2.5]


def test_shadow_transient_retry_allowlist_is_narrow_and_explicit() -> None:
    assert (
        frozenset(
            {
                "provider_snapshot_stale",
                "provider_snapshot_observer_quorum_insufficient",
                "provider_snapshot_out_of_order",
                "provider_shadow_controller_snapshot_missing",
                "provider_shadow_controller_snapshot_incomplete",
                "provider_shadow_controller_snapshot_candidate_count_mismatch",
            }
        )
        == command._TRANSIENT_SHADOW_FAILURE_CODES
    )


@pytest.mark.parametrize(
    ("error_type", "failure_code"),
    (
        (command.ProviderPoolRankError, "provider_snapshot_hash_mismatch"),
        (
            command.ProviderPoolShadowControllerError,
            "provider_shadow_controller_snapshot_source_hash_invalid",
        ),
        (
            command.ProviderPoolShadowControllerError,
            "provider_shadow_controller_snapshot_payload_invalid",
        ),
        (
            command.ProviderPoolShadowControllerError,
            "provider_shadow_controller_snapshot_result_hash_mismatch",
        ),
        (
            command.ProviderPoolShadowControllerError,
            "provider_shadow_controller_snapshot_pages_incomplete",
        ),
    ),
)
def test_shadow_model_does_not_retry_integrity_failures(
    monkeypatch,
    error_type,
    failure_code,
) -> None:
    calls = 0

    def run_model(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise error_type(failure_code)

    monkeypatch.setattr(command, "run_model", run_model)

    with pytest.raises(error_type, match=failure_code):
        command.run_model_with_retry(
            "gpt-5.6-sol",
            persist=True,
            connection_factory=lambda: object(),
            sleep=lambda _seconds: pytest.fail("unexpected retry"),
        )

    assert calls == 1


def test_shadow_model_retry_exhaustion_preserves_failure_evidence(monkeypatch) -> None:
    def run_model(*_args, **_kwargs):
        raise command.ProviderPoolRankError("provider_snapshot_stale")

    monkeypatch.setattr(command, "run_model", run_model)

    with pytest.raises(command.ProviderPoolShadowRetryExhausted) as caught:
        command.run_model_with_retry(
            "gpt-5.6-sol",
            persist=True,
            connection_factory=lambda: object(),
            retry_attempts=2,
            retry_delay_seconds=0,
            sleep=lambda _seconds: None,
        )

    assert command.shadow_failure_details(caught.value) == (
        "ProviderPoolRankError",
        "provider_snapshot_stale",
        2,
    )


def test_model_persist_refreshes_inventory_before_planning(monkeypatch) -> None:
    calls: list[str] = []

    class Projector:
        def reconcile(self, model: str):
            calls.append(model)
            return SimpleNamespace(
                summary=lambda: {
                    "model": model,
                    "canonical_count": 5,
                    "funded_count": 1,
                    "changed_count": 5,
                    "retired_count": 6,
                }
            )

    class Controller:
        def __init__(self, _source, _config, *, store):
            assert store is not None

        def run(self, *, now, persist):
            assert now is not None
            assert persist is True
            return SimpleNamespace(
                summary=lambda: {
                    "success": True,
                    "model": "gpt-5.6-sol",
                    "remote_write_count": 0,
                }
            )

    monkeypatch.setattr(
        command,
        "PostgresProviderPoolPlanningAdapter",
        lambda _factory: object(),
    )
    monkeypatch.setattr(
        command,
        "PostgresProviderPoolPlanStore",
        lambda _factory: object(),
    )
    monkeypatch.setattr(command, "SingleModelProviderPoolShadowController", Controller)

    result = command.run_model(
        "gpt-5.6-sol",
        persist=True,
        connection_factory=lambda: object(),
        inventory_projector=Projector(),
        project_inventory=True,
    )

    assert calls == ["gpt-5.6-sol"]
    assert result["inventory_projection"] == {
        "model": "gpt-5.6-sol",
        "canonical_count": 5,
        "funded_count": 1,
        "changed_count": 5,
        "retired_count": 6,
    }


def test_inventory_projection_flag_is_strict_and_default_off() -> None:
    assert command.resolve_inventory_projection_enabled(environ={}) is False
    assert (
        command.resolve_inventory_projection_enabled(
            environ={"PROVIDER_INVENTORY_RECONCILE_ENABLED": "true"}
        )
        is True
    )
    with pytest.raises(
        ProviderPoolShadowCommandError,
        match="provider_inventory_flag_invalid",
    ):
        command.resolve_inventory_projection_enabled(
            environ={"PROVIDER_INVENTORY_RECONCILE_ENABLED": "sometimes"}
        )
