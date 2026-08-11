from __future__ import annotations

from contextlib import nullcontext
from decimal import Decimal
from pathlib import Path

import pytest

from a6api_auto.trade_provider_failure_repair import (
    ProviderFailureProbeRecovery,
    ProviderFailureRepairClaim,
    ProviderFailureRepairRun,
    ProviderFailureRepairTarget,
)
from a6api_auto.trade_provider_fast_block import ProviderFastBlockProjection
from a6api_auto.trade_provider_pool import ProviderSlot, SlotState
from a6api_auto.trade_provider_pool_apply import (
    ProviderPoolApplyPolicy,
    ProviderPoolApplyStage,
)
from a6api_auto.trade_provider_pool_reconciler import IdempotencyLedger
from a6api_auto.trade_provider_pool_shadow_controller import (
    ProviderPoolShadowControllerConfig,
)
from scripts import repair_a6_provider_failures as command

MODEL = "gpt-5.6-sol"
CONFIG_VERSION = "a6-provider-topn-failover-v2"
BILLING_SCOPE = "billing=gpt-5.6-sol/group-26/user-1/keys-53,54/name-ToA6API/platform-openai"
CONFIRMATION = (
    "a6-provider-pool:gpt-5.6-sol:a6-provider-topn-failover-v2:"
    "MEMBERSHIP_AND_COST:max-membership=1:"
    f"{BILLING_SCOPE}"
)


class Worker:
    def __init__(self, run: ProviderFailureRepairRun) -> None:
        self.run = run
        self.calls = 0

    def run_once(self) -> ProviderFailureRepairRun:
        self.calls += 1
        return self.run


def billing_target() -> command.apply_runtime.ProviderBillingTargetContract:
    return command.apply_runtime.ProviderBillingTargetContract(
        MODEL,
        26,
        1,
        (53, 54),
        "ToA6API",
        "openai",
    )


def policy() -> ProviderPoolApplyPolicy:
    return ProviderPoolApplyPolicy(
        ProviderPoolApplyStage.MEMBERSHIP_AND_COST,
        1,
        CONFIRMATION,
    )


def enabled_environment() -> dict[str, str]:
    return {
        "PROVIDER_FAILURE_REPAIR_ENABLED": "true",
        "PROVIDER_POOL_APPLY_STAGE": "MEMBERSHIP_AND_COST",
        "PROVIDER_POOL_MAX_MEMBERSHIP_CHANGES": "1",
        "PROVIDER_POOL_APPLY_CONFIRMATION": CONFIRMATION,
        "A6_TRADE_DATABASE_DSN": "postgresql://fixture/trade",
        "PROVIDER_DISPATCH_REDIS_URL": "redis://fixture/0",
        "PROVIDER_REQUEST_PLANE_URL": "http://127.0.0.1:48769",
    }


def test_default_off_and_enabled_dry_run_are_cold() -> None:
    calls: list[str] = []

    def worker_factory():
        calls.append("worker")
        raise AssertionError("worker factory must stay cold")

    disabled, disabled_exit = command.run_model(
        MODEL,
        enabled=False,
        apply=True,
        worker_factory=worker_factory,
    )
    dry_run, dry_run_exit = command.run_model(
        MODEL,
        enabled=True,
        apply=False,
        worker_factory=worker_factory,
    )

    assert (disabled["action"], disabled_exit) == ("disabled", 0)
    assert (dry_run["action"], dry_run_exit) == ("dry-run", 0)
    assert disabled["remote_write_count"] == 0
    assert dry_run["remote_write_count"] == 0
    assert calls == []


def test_execute_returns_before_database_setup_when_disabled_or_without_apply() -> None:
    calls: list[object] = []

    def connector(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("database connector must stay cold")

    apply_args = command.parser().parse_args(["--model", MODEL, "--apply"])
    disabled, disabled_exit = command.execute(
        apply_args,
        environ={},
        connector=connector,
    )
    dry_args = command.parser().parse_args(["--model", MODEL])
    dry_run, dry_run_exit = command.execute(
        dry_args,
        environ={"PROVIDER_FAILURE_REPAIR_ENABLED": "true"},
        connector=connector,
    )

    assert (disabled["action"], disabled_exit) == ("disabled", 0)
    assert (dry_run["action"], dry_run_exit) == ("dry-run", 0)
    assert calls == []


def test_missing_apply_policy_blocks_before_worker_or_database_claim(monkeypatch) -> None:
    calls: list[str] = []

    def fail_build_worker(**kwargs):
        calls.append("worker")
        raise AssertionError("worker must not be built")

    monkeypatch.setattr(command, "build_worker", fail_build_worker)
    args = command.parser().parse_args(
        [
            "--model",
            MODEL,
            "--apply",
            "--billing-group-id",
            "26",
            "--billing-user-id",
            "1",
            "--billing-api-key-ids",
            "53,54",
            "--billing-group-name",
            "ToA6API",
            "--billing-platform",
            "openai",
        ]
    )

    with pytest.raises(
        command.ProviderFailureRepairCommandError,
        match="provider_failure_repair_membership_stage_required",
    ):
        command.execute(
            args,
            environ={"PROVIDER_FAILURE_REPAIR_ENABLED": "true"},
            connector=lambda *_args, **_kwargs: calls.append("database"),
        )

    assert calls == []


def test_main_reports_enabled_apply_configuration_errors_without_zero_write_claim(
    capsys,
) -> None:
    exit_code = command.main(
        [
            "--model",
            MODEL,
            "--apply",
            "--billing-group-id",
            "26",
            "--billing-user-id",
            "1",
            "--billing-api-key-ids",
            "53,54",
            "--billing-group-name",
            "ToA6API",
            "--billing-platform",
            "openai",
        ],
        environ={"PROVIDER_FAILURE_REPAIR_ENABLED": "true"},
        connector=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("configuration failure must precede database access")
        ),
    )

    document = command.json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert document["enabled"] is True
    assert document["apply"] is True
    assert document["remote_write_count"] is None
    assert document["error_class"] == "provider_failure_repair_membership_stage_required"


def test_exact_policy_builds_failure_repair_worker_without_connecting_early(
    monkeypatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_build_worker(**kwargs):
        captured.update(kwargs)
        return Worker(ProviderFailureRepairRun("idle"))

    monkeypatch.setattr(command, "build_worker", fake_build_worker)
    args = command.parser().parse_args(
        [
            "--model",
            MODEL,
            "--apply",
            "--billing-group-id",
            "26",
            "--billing-user-id",
            "1",
            "--billing-api-key-ids",
            "53,54",
            "--billing-group-name",
            "ToA6API",
            "--billing-platform",
            "openai",
        ]
    )
    connector_calls: list[object] = []

    result, exit_code = command.execute(
        args,
        environ=enabled_environment(),
        connector=lambda *args, **kwargs: connector_calls.append((args, kwargs)),
    )

    assert (result["action"], exit_code) == ("idle", 0)
    assert connector_calls == []
    assert captured["model"] == MODEL
    assert captured["redis_url"] == "redis://fixture/0"
    config = captured["config"]
    assert isinstance(config, ProviderPoolShadowControllerConfig)
    assert config.generation_source == "failure_repair"


def test_worker_construction_keeps_remote_secrets_cold(monkeypatch) -> None:
    secret_reads: list[Path] = []

    def read_secret(path: Path) -> str:
        secret_reads.append(path)
        raise AssertionError("runtime secret must be read only after a claimed remote action")

    monkeypatch.setattr(command.apply_runtime, "_private_text", read_secret)
    worker = command.build_worker(
        model=MODEL,
        config=ProviderPoolShadowControllerConfig(
            model=MODEL,
            config_version=CONFIG_VERSION,
            generation_source="failure_repair",
        ),
        apply_policy=policy(),
        billing_target=billing_target(),
        connection_factory=lambda: (_ for _ in ()).throw(
            AssertionError("database connection must stay lazy")
        ),
        redis_url="redis://fixture/0",
        redis_key_prefix="a6:provider:dispatch:v1",
        redis_notification_channel="a6:provider:dispatch:changed:v1",
        request_plane_url="http://127.0.0.1:48769",
        sessions_dir=Path("/secrets/sessions"),
        proxy_map_file=Path("/secrets/proxy-map.json"),
        enodot_url="https://ai.enodot.shop",
        enodot_admin_key_file=Path("/secrets/enodot-admin-key.secret"),
        worker_id="repair-fixture-1",
    )

    assert isinstance(worker, command.ProviderFailureRepairWorker)
    assert worker.controller.runtime_factory is not None
    assert worker.fast_block_replayer is not None
    assert secret_reads == []


def test_build_worker_quick_probe_uses_only_current_slot_and_keeps_replacement_cold(
    monkeypatch,
) -> None:
    current_slot = ProviderSlot(
        slot_no=1,
        channel_id=1008,
        rank=1,
        priority=2,
        assignment_id=31,
        enodot_binding_id=31,
        token_id=41,
        account_id=51,
        slot_state=SlotState.DRAINING,
        funding_eligible=True,
        desired_schedulable=False,
        observed_schedulable=False,
        desired_cost_multiplier=Decimal("1"),
        observed_cost_multiplier=Decimal("1"),
        confirmed_pin_binding_id=61,
        pin_confirmed=True,
        probe_confirmed=True,
        state_version=7,
    )
    repair_claim = ProviderFailureRepairClaim(
        request_id=71,
        claim_token="claim-token",
        worker_id="repair-fixture-1",
        attempt_count=1,
        attempt_id="attempt-71",
        quarantine_id=81,
        control_action="probe_then_repin",
        config_version=CONFIG_VERSION,
        model=MODEL,
        routing_generation=1,
        cost_generation=1,
        current_slot=current_slot,
    )
    calls: list[object] = []

    class Store:
        def __init__(self):
            self.claim = repair_claim

        def claim_next(self, *, worker_id):
            assert worker_id == "repair-fixture-1"
            value, self.claim = self.claim, None
            return value

        def read_probe_recovery(self, _claim):
            return None

        def read_active_target(self, _claim):
            return None

        def complete_probe_recovery(self, _claim, *, quick_probe_count):
            calls.append(("complete-probe-recovery", quick_probe_count))
            return ProviderFailureProbeRecovery(quick_probe_count, 91)

        def bind_target(self, *_args):
            raise AssertionError("replacement target must stay cold")

        def complete(self, *_args, **_kwargs):
            raise AssertionError("replacement completion must stay cold")

        def fail(self, *_args, **_kwargs):
            raise AssertionError("quick recovery must not fail")

    class Lease:
        def hold(self, model):
            assert model == MODEL
            return nullcontext()

    class ForbiddenPlanner:
        def plan(self, *_args, **_kwargs):
            raise AssertionError("replacement planner must stay cold")

    class ContextSource:
        def __init__(self, _connection_factory):
            pass

        def load(self, model, slots, *, desired_token_ids):
            calls.append(("context", model, tuple(slots), desired_token_ids))
            return ("current-record",)

    class Remote:
        def __init__(self):
            self.schedulable = True

        def drain_slot(self, slot, *, idempotency_key):
            assert slot is current_slot
            assert idempotency_key
            self.schedulable = False
            calls.append(("drain", slot.channel_id))
            return True

        def probe_slot(self, slot, *, idempotency_key):
            assert slot is current_slot
            assert idempotency_key
            calls.append(("probe", slot.channel_id))
            return True

        def set_schedulable(self, slot, enabled, *, idempotency_key):
            assert slot is current_slot
            assert enabled is True
            assert idempotency_key
            self.schedulable = True
            calls.append(("restore", slot.channel_id))
            return True

        def read_slot_schedulable(self, slot):
            assert slot is current_slot
            return self.schedulable

    store = Store()
    remote = Remote()

    def replay_fast_blocks():
        calls.append(("project-pending",))
        return {"processed": 0, "projected": 0, "superseded": 0, "failed": 0}

    monkeypatch.setattr(command, "PostgresProviderFailureRepairStore", lambda *_a, **_k: store)
    monkeypatch.setattr(
        command,
        "PostgresProviderActionLedger",
        lambda *_a, **_k: IdempotencyLedger(),
    )
    monkeypatch.setattr(command, "PostgresProviderModelLease", lambda *_a, **_k: Lease())
    monkeypatch.setattr(command, "ProviderPoolApplyPlanner", lambda *_a, **_k: ForbiddenPlanner())
    monkeypatch.setattr(command, "PostgresProviderRemoteContextSource", ContextSource)
    monkeypatch.setattr(command, "EnodotAdminClient", lambda *_a, **_k: "admin")
    monkeypatch.setattr(command.apply_runtime, "_private_text", lambda _path: "admin-key")
    monkeypatch.setattr(command.apply_runtime, "_proxy_map", lambda _path: {51: "proxy"})
    monkeypatch.setattr(
        command,
        "build_fast_block_replayer",
        lambda *_a, **_k: replay_fast_blocks,
    )

    def build_remote_for_slots(
        model,
        slots,
        desired_token_ids,
        records,
        **kwargs,
    ):
        calls.append(
            (
                "build-remote",
                model,
                tuple(slots),
                desired_token_ids,
                tuple(records),
                kwargs["billing_target"],
            )
        )
        return remote

    monkeypatch.setattr(
        command.apply_runtime,
        "build_remote_adapter_for_slots",
        build_remote_for_slots,
    )
    monkeypatch.setattr(
        command.apply_runtime,
        "build_dispatch_projector",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("dispatch projector must stay cold")
        ),
    )

    worker = command.build_worker(
        model=MODEL,
        config=ProviderPoolShadowControllerConfig(
            model=MODEL,
            config_version=CONFIG_VERSION,
            generation_source="failure_repair",
        ),
        apply_policy=policy(),
        billing_target=billing_target(),
        connection_factory=lambda: (_ for _ in ()).throw(
            AssertionError("patched dependencies own all database access")
        ),
        redis_url="redis://fixture/0",
        redis_key_prefix="a6:provider:dispatch:v1",
        redis_notification_channel="a6:provider:dispatch:changed:v1",
        request_plane_url="http://127.0.0.1:48769",
        sessions_dir=Path("/secrets/sessions"),
        proxy_map_file=Path("/secrets/proxy-map.json"),
        enodot_url="https://ai.enodot.shop",
        enodot_admin_key_file=Path("/secrets/enodot-admin-key.secret"),
        worker_id="repair-fixture-1",
    )

    result = worker.run_once()

    assert result.status == "completed"
    assert result.quick_probe_count == 1
    context_call = next(call for call in calls if call[0] == "context")
    assert context_call[1:] == (MODEL, (current_slot,), frozenset({41}))
    build_call = next(call for call in calls if call[0] == "build-remote")
    assert build_call[1:5] == (
        MODEL,
        (current_slot,),
        frozenset({41}),
        ("current-record",),
    )
    assert [call[0] for call in calls if isinstance(call, tuple)] == [
        "project-pending",
        "context",
        "build-remote",
        "drain",
        "probe",
        "restore",
        "complete-probe-recovery",
        "project-pending",
    ]


def test_pending_fast_block_replayer_escalates_failed_projection_for_timer_retry() -> None:
    class Control:
        def __init__(self):
            self.calls = 0

        def project_pending_fast_blocks(self, *, limit, stop_on_failure):
            assert limit == 500
            assert stop_on_failure is True
            self.calls += 1
            return {
                "processed": 1,
                "projected": 0 if self.calls == 1 else 1,
                "superseded": 0,
                "failed": 1 if self.calls == 1 else 0,
                "events": [],
            }

    control = Control()
    replayer = command._PendingFastBlockReplayer(control)

    with pytest.raises(
        command.ProviderFailureRepairCommandError,
        match="provider_failure_repair_fast_block_projection_failed",
    ):
        replayer()

    assert replayer()["projected"] == 1
    assert control.calls == 2


def test_fast_block_replayer_builder_projects_quick_recovery_unblock_to_redis(
    monkeypatch,
) -> None:
    import redis

    key = f"a6:provider:fast-block:v1:{MODEL}:1:1:1008"

    class RedisClient:
        def __init__(self):
            self.values = {key: "blocked"}

        def eval(self, _script, key_count, block_key, cursor_key, *args):
            assert key_count == 2
            event_id, operation = int(args[0]), args[1]
            assert event_id == 91
            assert operation == "unblock"
            changed = int(self.values.pop(block_key, None) is not None)
            self.values[cursor_key] = str(event_id)
            return changed

    client = RedisClient()
    monkeypatch.setattr(redis.Redis, "from_url", lambda *_a, **_k: client)
    observed: dict[str, object] = {}

    class Control:
        def __init__(self, _connection_factory, *, fast_block_projector):
            self.projector = fast_block_projector

        def project_pending_fast_blocks(self, *, limit, stop_on_failure):
            assert limit == 500
            assert stop_on_failure is True
            projection = ProviderFastBlockProjection(
                event_id=91,
                operation="unblock",
                model=MODEL,
                routing_generation=1,
                cost_generation=1,
                slot_no=1,
                channel_id=1008,
                projection_key=key,
                payload={
                    "recovery_kind": "failure_quick_probe",
                    "repair_request_id": 71,
                },
            )
            result = self.projector.apply(projection)
            observed["state"] = result.state
            observed["changed"] = result.changed
            return {
                "processed": 1,
                "projected": int(result.state == "projected"),
                "superseded": int(result.state == "superseded"),
                "failed": 0,
                "events": [],
            }

    monkeypatch.setattr(command, "PostgresPublicationControl", Control)

    replayer = command.build_fast_block_replayer(
        lambda: None,
        redis_url="redis://fixture/0",
    )
    result = replayer()

    assert result["projected"] == 1
    assert observed == {"state": "projected", "changed": True}
    assert key not in client.values
    assert client.values[f"{key}:cursor"] == "91"


@pytest.mark.parametrize(
    ("run", "expected_action", "expected_exit"),
    [
        (ProviderFailureRepairRun("idle"), "idle", 0),
        (ProviderFailureRepairRun("completed", request_id=71), "completed", 0),
        (
            ProviderFailureRepairRun(
                "pending",
                request_id=71,
                error_class="provider_failure_repair_probe_failed",
            ),
            "pending",
            75,
        ),
        (
            ProviderFailureRepairRun(
                "failed",
                request_id=71,
                error_class="provider_failure_repair_probe_failed",
            ),
            "failed",
            1,
        ),
    ],
)
def test_worker_status_maps_to_process_exit_code(run, expected_action, expected_exit) -> None:
    worker = Worker(run)

    result, exit_code = command.run_model(
        MODEL,
        enabled=True,
        apply=True,
        worker_factory=lambda: worker,
    )

    assert result["action"] == expected_action
    assert exit_code == expected_exit
    assert worker.calls == 1


def test_completed_result_includes_the_frozen_target() -> None:
    target = ProviderFailureRepairTarget(3, 4, 1, 1012, 51, 41, "b" * 64)

    result, exit_code = command.run_model(
        MODEL,
        enabled=True,
        apply=True,
        worker_factory=lambda: Worker(
            ProviderFailureRepairRun(
                "completed",
                request_id=71,
                control_action="repin_immediately",
                attempt_count=1,
                quick_probe_count=0,
                target=target,
            )
        ),
    )

    assert exit_code == 0
    assert result["target"] == {
        "routing_generation": 3,
        "cost_generation": 4,
        "slot_no": 1,
        "channel_id": 1012,
        "account_id": 51,
        "token_id": 41,
        "source_hash": "b" * 64,
    }


def test_systemd_repair_runner_is_default_off_serialized_and_rapid() -> None:
    root = Path(__file__).resolve().parents[1]
    service = (root / "deploy/systemd/a6-trader-provider-failure-repair.service").read_text()
    timer = (root / "deploy/systemd/a6-trader-provider-failure-repair.timer").read_text()

    assert "PROVIDER_FAILURE_REPAIR_ENABLED" in service
    assert "PROVIDER_POOL_APPLY_STAGE" in service
    assert "PROVIDER_POOL_MAX_MEMBERSHIP_CHANGES" in service
    assert "PROVIDER_POOL_APPLY_CONFIRMATION" in service
    assert "--apply" in service
    assert "--provider-failure-repair-enabled" not in service
    assert "repair_a6_provider_failures.py" in service
    assert "--billing-api-key-ids 53,54" in service
    assert "PROVIDER_REQUEST_PLANE_URL=http://127.0.0.1:48769" in service
    assert "--initial-max-active-slots 12" in service
    assert "--margin-bps 2000" in service
    assert "--pin-probe-mode disabled" in service
    assert "a6-provider-topn-failover-v4-all-funded-dynamic-ready" in service
    assert "provider-control.lock" in service
    assert "seller-control.lock" not in service
    assert "--conflict-exit-code 75" in service
    assert "SuccessExitStatus=75" in service
    assert "NoNewPrivileges=true" in service
    assert "ProtectSystem=strict" in service
    assert "ReadWritePaths=/var/lib/a6-trader" in service
    assert "OnUnitInactiveSec=15s" in timer
    assert "RandomizedDelaySec=4s" in timer
    assert "Persistent=true" not in timer
