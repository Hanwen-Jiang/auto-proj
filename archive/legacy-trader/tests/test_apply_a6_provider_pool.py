from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from a6api_auto.trade_provider_cost import PoolDiffKind, build_cost_plan
from a6api_auto.trade_provider_pool import ProviderSlot, SlotState
from a6api_auto.trade_provider_pool_apply import (
    PostgresProviderRemoteContextSource,
    ProviderPoolApplyError,
    ProviderPoolApplyPolicy,
    ProviderPoolApplyStage,
    ProviderRemoteContextRecord,
)
from a6api_auto.trade_provider_pool_reconciler import (
    ProviderPlatformBackoffActive,
    ProviderPoolReconcileError,
    ProviderReadbackRetryRequired,
    ReplanRequired,
)
from scripts import apply_a6_provider_pool as command

MODEL = "gpt-5.6-sol"
API_KEY = "sk-provider-fixture"
BILLING_SCOPE = "billing=gpt-5.6-sol/group-26/user-1/keys-53,54/name-ToA6API/platform-openai"


def apply_policy(
    stage: ProviderPoolApplyStage,
    *,
    max_membership_changes: int = 0,
) -> ProviderPoolApplyPolicy:
    pending = ProviderPoolApplyPolicy(stage, max_membership_changes, "")
    return ProviderPoolApplyPolicy(
        stage,
        max_membership_changes,
        pending.expected_confirmation(
            model=MODEL,
            config_version="a6-provider-topn-failover-v2",
            billing_contract=BILLING_SCOPE,
        ),
    )


def ready_slot(
    slot_no: int,
    *,
    channel_id: int,
    binding_id: int,
    token_id: int,
    account_id: int,
) -> ProviderSlot:
    return ProviderSlot(
        slot_no=slot_no,
        channel_id=channel_id,
        rank=slot_no,
        priority=slot_no + 1,
        assignment_id=account_id,
        enodot_binding_id=binding_id,
        token_id=token_id,
        account_id=account_id,
        slot_state=SlotState.READY,
        funding_eligible=True,
        desired_schedulable=True,
        observed_schedulable=True,
        desired_cost_multiplier=Decimal("1"),
        observed_cost_multiplier=Decimal("1"),
        confirmed_pin_binding_id=9000 + slot_no,
        pin_confirmed=True,
        probe_confirmed=True,
    )


class _Plan:
    def __init__(self, diff_kind: PoolDiffKind) -> None:
        self.diff_kind = diff_kind
        self.bundle = SimpleNamespace(
            model=MODEL,
            routing_generation=7,
            cost_generation=9,
            config_version="a6-provider-topn-failover-v2",
            cost_plan=SimpleNamespace(
                pool_cost_multiplier=Decimal("1"),
                billing_floor_multiplier=Decimal("1.2"),
                margin_bps=2_000,
            ),
            desired_size=2,
            ordered_channel_ids=(701, 702),
            slots=(
                ready_slot(1, channel_id=701, binding_id=31, token_id=11, account_id=21),
                ready_slot(2, channel_id=702, binding_id=32, token_id=12, account_id=22),
            ),
        )
        self.current_routing = (10,)
        self.current_assignments = {1: 21}
        self.current_slots = {1: "slot-fixture"}
        self.current_cost_input_hash = "a" * 64
        self.changed_slot_nos = (1,) if diff_kind is PoolDiffKind.MEMBERSHIP_AND_COST else ()
        self.active = SimpleNamespace(routing_generation=6, cost_generation=8)
        self.billing_finalize_required = False

    def summary(self):
        return {
            "success": True,
            "mode": "remote-apply",
            "model": MODEL,
            "diff_kind": self.diff_kind.value,
            "routing_generation": 7,
            "cost_generation": 9,
            "activated": False,
            "remote_write_count": 0,
        }


class _Planner:
    def __init__(self, plan: _Plan) -> None:
        self.value = plan
        self.calls = 0

    def plan(self):
        self.calls += 1
        return self.value


def test_command_is_default_off_before_database_or_secret_setup() -> None:
    result = command.run_model(MODEL, enabled=False, apply=True)

    assert result == {
        "success": True,
        "model": MODEL,
        "enabled": False,
        "apply": False,
        "action": "disabled",
        "activated": False,
        "remote_write_count": 0,
    }
    parsed = command.parser().parse_args(["--model", MODEL])
    assert parsed.apply is False
    assert parsed.provider_pool_reconcile_enabled is False
    assert parsed.maximum_provider_cost_cny == Decimal("0.3")
    assert command.resolve_enabled(False, environ={}) is False
    assert (
        command.resolve_enabled(
            False,
            environ={"PROVIDER_POOL_RECONCILE_ENABLED": "true"},
        )
        is True
    )


def test_main_default_off_does_not_require_database_or_runtime_ids(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        "sys.argv",
        ["apply_a6_provider_pool.py", "--model", MODEL, "--apply"],
    )
    for name in (
        "PROVIDER_POOL_RECONCILE_ENABLED",
        "A6_TRADE_DATABASE_DSN",
        "PROVIDER_POOL_BILLING_GROUP_ID",
        "PROVIDER_POOL_BILLING_USER_ID",
    ):
        monkeypatch.delenv(name, raising=False)

    command.main()

    assert json.loads(capsys.readouterr().out)["action"] == "disabled"


def test_enabled_dry_run_builds_plan_without_remote_factory() -> None:
    planner = _Planner(_Plan(PoolDiffKind.COST_ONLY))

    result = command.run_model(
        MODEL,
        enabled=True,
        apply=False,
        planner=planner,
        reconciler_factory=lambda _plan: pytest.fail("remote factory must stay cold"),
        apply_policy=apply_policy(ProviderPoolApplyStage.COST_ONLY),
        billing_contract=BILLING_SCOPE,
    )

    assert result["action"] == "planned"
    assert result["activated"] is False
    assert result["remote_write_count"] == 0
    assert planner.calls == 1


def test_platform_backoff_is_a_clean_apply_noop() -> None:
    class Reconciler:
        def reconcile(self, *_args, **_kwargs):
            raise ProviderPlatformBackoffActive(
                {
                    "confirmed": False,
                    "protocol": "responses",
                    "http_status": None,
                    "error_code": "provider_probe_backoff_active",
                    "failure_owner": "platform",
                    "replan_scope": "platform",
                    "document": {
                        "request_sent": False,
                        "retry_after_ms": 31_000,
                        "source_action_id": 77,
                    },
                }
            )

    planner = _Planner(_Plan(PoolDiffKind.COST_ONLY))
    result = command.run_model(
        MODEL,
        enabled=True,
        apply=True,
        required_ready_slots=2,
        planner=planner,
        reconciler_factory=lambda _plan: Reconciler(),
        apply_policy=apply_policy(ProviderPoolApplyStage.COST_ONLY),
        billing_contract=BILLING_SCOPE,
    )

    assert result["action"] == "platform-backoff"
    assert result["activated"] is False
    assert result["persisted"] is False
    assert result["remote_write_count"] == 0
    assert result["request_sent"] is False
    assert result["retry_after_ms"] == 31_000


def test_readback_retry_keeps_apply_generation_without_reporting_failure() -> None:
    class Reconciler:
        def reconcile(self, *_args, **_kwargs):
            raise ProviderReadbackRetryRequired(
                ProviderPoolReconcileError(
                    "provider_reconcile_slot_readback_mismatch",
                    diagnostics={"slot_no": 2, "mismatch_fields": ["schedulable"]},
                )
            )

    result = command.run_model(
        MODEL,
        enabled=True,
        apply=True,
        required_ready_slots=2,
        planner=_Planner(_Plan(PoolDiffKind.COST_ONLY)),
        reconciler_factory=lambda _plan: Reconciler(),
        apply_policy=apply_policy(ProviderPoolApplyStage.COST_ONLY),
        billing_contract=BILLING_SCOPE,
    )

    assert result["action"] == "readback-retry"
    assert result["persisted"] is False
    assert result["activated"] is False
    assert result["retry_error"] == "provider_reconcile_readback_retry_required"
    assert result["readback_diagnostics"]["slot_no"] == 2


def test_dry_run_resolves_exact_billing_confirmation_scope_without_remote_read() -> None:
    target = command.resolve_billing_target(
        MODEL,
        group_id=26,
        user_id=1,
        api_key_ids="54,53",
        group_name="ToA6API",
        platform="OpenAI",
        required=False,
        environ={},
    )

    assert target is not None
    assert target.confirmation_scope == BILLING_SCOPE
    result = command.run_model(
        MODEL,
        enabled=True,
        apply=False,
        planner=_Planner(_Plan(PoolDiffKind.MEMBERSHIP_AND_COST)),
        apply_policy=command.ProviderPoolApplyPolicy(
            command.ProviderPoolApplyStage.MEMBERSHIP_AND_COST,
            1,
            "",
        ),
        billing_contract=target.confirmation_scope,
    )
    assert result["required_confirmation"] == (
        "a6-provider-pool:gpt-5.6-sol:a6-provider-topn-failover-v2:"
        "MEMBERSHIP_AND_COST:max-membership=1:"
        f"{BILLING_SCOPE}"
    )
    assert result["remote_write_count"] == 0


def test_unscoped_dry_run_keeps_billing_context_cold() -> None:
    target = command.resolve_billing_target(
        MODEL,
        group_id=None,
        user_id=None,
        api_key_ids=None,
        group_name=None,
        platform=None,
        required=False,
        environ={},
    )

    assert target is None


def test_partially_scoped_dry_run_fails_closed() -> None:
    with pytest.raises(
        command.ProviderPoolApplyCommandError,
        match="provider_apply_billing_target_missing",
    ):
        command.resolve_billing_target(
            MODEL,
            group_id=26,
            user_id=None,
            api_key_ids=None,
            group_name=None,
            platform=None,
            required=False,
            environ={},
        )


def test_enabled_noop_skips_remote_factory_even_with_apply() -> None:
    projected = []

    class Projector:
        def reconcile(self, model, **kwargs):
            projected.append((model, kwargs))
            return SimpleNamespace(
                key="dispatch-key",
                digest="f" * 64,
                changed=False,
                routing_generation=6,
                cost_generation=8,
                state="active",
            )

    result = command.run_model(
        MODEL,
        enabled=True,
        apply=True,
        required_ready_slots=2,
        planner=_Planner(_Plan(PoolDiffKind.NOOP)),
        reconciler_factory=lambda _plan: pytest.fail("remote factory must stay cold"),
        dispatch_projector=Projector(),
        apply_policy=apply_policy(ProviderPoolApplyStage.COST_ONLY),
        billing_contract=BILLING_SCOPE,
    )

    assert result["action"] == "noop"
    assert result["activated"] is False
    assert result["dispatch_published"] is True
    assert result["dispatch_changed"] is False
    assert projected == [
        (
            MODEL,
            {
                "expected_routing_generation": 6,
                "expected_cost_generation": 8,
            },
        )
    ]


def test_enabled_apply_passes_the_frozen_current_projection_to_reconciler() -> None:
    plan = _Plan(PoolDiffKind.MEMBERSHIP_AND_COST)
    observed = []
    order = []

    class Reconciler:
        def reconcile(self, bundle, **kwargs):
            order.append("activate")
            observed.append((bundle, kwargs))
            return SimpleNamespace(
                model=MODEL,
                diff_kind=PoolDiffKind.MEMBERSHIP_AND_COST,
                routing_generation=7,
                cost_generation=9,
                activated=True,
                actions=("stage_billing_floor", "repin_slot"),
            )

    class Projector:
        def reconcile(self, model, **kwargs):
            order.append("dispatch")
            assert model == MODEL
            assert kwargs == {
                "expected_routing_generation": 7,
                "expected_cost_generation": 9,
            }
            return SimpleNamespace(
                key="dispatch-key",
                digest="f" * 64,
                changed=True,
                routing_generation=7,
                cost_generation=9,
                state="active",
            )

    result = command.run_model(
        MODEL,
        enabled=True,
        apply=True,
        required_ready_slots=2,
        planner=_Planner(plan),
        reconciler_factory=lambda received: (
            Reconciler() if received is plan else pytest.fail("plan identity changed")
        ),
        dispatch_projector=Projector(),
        apply_policy=apply_policy(
            ProviderPoolApplyStage.MEMBERSHIP_AND_COST,
            max_membership_changes=1,
        ),
        billing_contract=BILLING_SCOPE,
    )

    assert result["action"] == "activated"
    assert result["persisted"] is True
    assert result["remote_write_count"] == 2
    assert result["dispatch_published"] is True
    assert result["dispatch_changed"] is True
    assert order == ["activate", "dispatch"]
    assert observed == [
        (
            plan.bundle,
                {
                    "current_routing": (10,),
                    "current_assignments": {1: 21},
                    "current_slots": {1: "slot-fixture"},
                    "current_cost_input_hash": "a" * 64,
                    "verified_slot_nos": (),
                },
            )
        ]


def test_apply_automatically_replans_the_same_slot_after_bad_pin() -> None:
    plan = _Plan(PoolDiffKind.MEMBERSHIP_AND_COST)
    events = []

    class FailoverPlanner(_Planner):
        def __init__(self, value):
            super().__init__(value)
            self.rejected = []
            self.verified = []

        def reject_channel(self, channel_id):
            self.rejected.append(channel_id)

        def retain_verified_slots(self, received_plan, slot_nos):
            self.verified.append((received_plan, tuple(slot_nos)))

    planner = FailoverPlanner(plan)
    attempts = 0

    class Reconciler:
        def reconcile(self, bundle, **_kwargs):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise ReplanRequired(
                    "provider_reconcile_probe_failed",
                    slot_no=1,
                    channel_id=701,
                    replan_scope="channel",
                    verified_slot_nos=(2,),
                )
            return SimpleNamespace(
                model=MODEL,
                diff_kind=PoolDiffKind.MEMBERSHIP_AND_COST,
                routing_generation=bundle.routing_generation,
                cost_generation=bundle.cost_generation,
                activated=True,
                actions=("repin_slot", "probe_slot"),
            )

        def fail_applying(self, bundle, *, reason):
            events.append(("failed", bundle.routing_generation, reason))

    class Projector:
        def reconcile(self, model, **_kwargs):
            return SimpleNamespace(
                key="dispatch-key",
                digest="f" * 64,
                changed=True,
                routing_generation=7,
                cost_generation=9,
                state="active",
            )

    result = command.run_model(
        MODEL,
        enabled=True,
        apply=True,
        required_ready_slots=2,
        planner=planner,
        reconciler_factory=lambda _plan: Reconciler(),
        dispatch_projector=Projector(),
        apply_policy=apply_policy(
            ProviderPoolApplyStage.MEMBERSHIP_AND_COST,
            max_membership_changes=1,
        ),
        billing_contract=BILLING_SCOPE,
        pin_failover_attempts=12,
    )

    assert result["action"] == "activated"
    assert result["pin_failover_count"] == 1
    assert result["failed_pins"] == [{"slot_no": 1, "channel_id": 701}]
    assert planner.calls == 2
    assert planner.rejected == [701]
    assert planner.verified == [(plan, (2,))]
    assert events == [("failed", 7, "provider_reconcile_probe_failed")]


def test_apply_quota_probe_replaces_provider_without_rejecting_channel() -> None:
    plan = _Plan(PoolDiffKind.MEMBERSHIP_AND_COST)
    failed_generations = []
    handled = []

    class Planner(_Planner):
        def __init__(self, value):
            super().__init__(value)
            self.rejected_providers = []

        def reject_channel(self, channel_id):
            pytest.fail(f"quota failure rejected channel {channel_id}")

        def reject_provider(self, account_id, token_id):
            self.rejected_providers.append((account_id, token_id))

        def retain_verified_slots(self, _plan, _slot_nos):
            return None

    planner = Planner(plan)
    attempts = 0

    class Reconciler:
        def reconcile(self, bundle, **_kwargs):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise ReplanRequired(
                    "provider_reconcile_probe_failed",
                    slot_no=1,
                    channel_id=701,
                    account_id=21,
                    token_id=11,
                    replan_scope="liquidity",
                    probe_observation={
                        "http_status": 403,
                        "error_code": "insufficient_quota",
                        "replan_scope": "liquidity",
                    },
                )
            return SimpleNamespace(
                model=MODEL,
                diff_kind=PoolDiffKind.MEMBERSHIP_AND_COST,
                routing_generation=bundle.routing_generation,
                cost_generation=bundle.cost_generation,
                activated=True,
                actions=("repin_slot", "probe_slot"),
            )

        def fail_applying(self, bundle, *, reason):
            failed_generations.append((bundle.routing_generation, reason))

    class Projector:
        def reconcile(self, _model, **_kwargs):
            return SimpleNamespace(
                key="dispatch-key",
                digest="f" * 64,
                changed=True,
                routing_generation=7,
                cost_generation=9,
                state="active",
            )

    result = command.run_model(
        MODEL,
        enabled=True,
        apply=True,
        required_ready_slots=2,
        planner=planner,
        reconciler_factory=lambda _plan: Reconciler(),
        dispatch_projector=Projector(),
        apply_policy=apply_policy(
            ProviderPoolApplyStage.MEMBERSHIP_AND_COST,
            max_membership_changes=1,
        ),
        billing_contract=BILLING_SCOPE,
        probe_failure_handler=lambda received_plan, failure: handled.append(
            (received_plan, failure)
        ),
    )

    assert result["action"] == "activated"
    assert result["pin_failover_count"] == 0
    assert result["provider_failover_count"] == 1
    assert result["failed_providers"][0]["probe"]["error_code"] == (
        "insufficient_quota"
    )
    assert planner.rejected_providers == [(21, 11)]
    assert len(handled) == 1
    assert failed_generations == [(7, "provider_reconcile_probe_failed")]


def test_apply_platform_failure_keeps_generation_for_next_timer_run() -> None:
    plan = _Plan(PoolDiffKind.MEMBERSHIP_AND_COST)
    failed_generations = []
    persisted_failures = []

    class Planner(_Planner):
        def __init__(self, value):
            super().__init__(value)
            self.rejected_providers = []

        def reject_provider(self, account_id, token_id):
            self.rejected_providers.append((account_id, token_id))

        def retain_verified_slots(self, _plan, _slot_nos):
            return None

    planner = Planner(plan)
    attempts = 0

    class Reconciler:
        def reconcile(self, bundle, **_kwargs):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise ReplanRequired(
                    "provider_reconcile_probe_failed",
                    slot_no=2,
                    channel_id=702,
                    account_id=22,
                    token_id=12,
                    replan_scope="platform",
                    probe_observation={
                        "error_code": "provider_probe_transport_failed",
                        "replan_scope": "platform",
                    },
                )
            if attempts == 2:
                raise ProviderPlatformBackoffActive(
                    {
                        "confirmed": False,
                        "protocol": "responses",
                        "http_status": None,
                        "error_code": "provider_probe_backoff_active",
                        "failure_owner": "platform",
                        "replan_scope": "platform",
                        "document": {
                            "request_sent": False,
                            "retry_after_ms": 1_500,
                            "source_action_id": 77,
                        },
                    }
                )
            return SimpleNamespace(
                model=MODEL,
                diff_kind=PoolDiffKind.MEMBERSHIP_AND_COST,
                routing_generation=bundle.routing_generation,
                cost_generation=bundle.cost_generation,
                activated=True,
                actions=("repin_slot", "probe_slot"),
            )

        def fail_applying(self, bundle, *, reason):
            failed_generations.append((bundle.routing_generation, reason))

    waits = []
    result = command.run_model(
        MODEL,
        enabled=True,
        apply=True,
        required_ready_slots=2,
        planner=planner,
        reconciler_factory=lambda _plan: Reconciler(),
        apply_policy=apply_policy(
            ProviderPoolApplyStage.MEMBERSHIP_AND_COST,
            max_membership_changes=1,
        ),
        billing_contract=BILLING_SCOPE,
        probe_failure_handler=lambda received_plan, failure: persisted_failures.append(
            (received_plan, failure)
        ),
        platform_backoff_waiter=waits.append,
    )

    assert result["action"] == "apply-retry"
    assert result["activated"] is False
    assert result["persisted"] is False
    assert result["retry_scope"] == "platform"
    assert result["retry_error"] == "provider_reconcile_probe_failed"
    assert result["provider_failover_count"] == 0
    assert result["failed_providers"] == []
    assert planner.rejected_providers == []
    assert persisted_failures == []
    assert failed_generations == []
    assert attempts == 1
    assert waits == []


def test_apply_platform_failure_does_not_reject_provider_below_slot_floor() -> None:
    full = _Plan(PoolDiffKind.MEMBERSHIP_AND_COST)
    full.bundle.desired_size = 2
    reduced = _Plan(PoolDiffKind.MEMBERSHIP_AND_COST)
    reduced.bundle.desired_size = 1
    reduced.bundle.ordered_channel_ids = (701,)
    reduced.bundle.slots = (reduced.bundle.slots[0],)
    attempts = 0

    class Planner:
        def __init__(self):
            self.rejected = set()
            self.restored = []

        def plan(self):
            return reduced if self.rejected else full

        def reject_provider(self, account_id, token_id):
            self.rejected.add((account_id, token_id))

        def restore_provider(self, account_id, token_id):
            self.rejected.discard((account_id, token_id))
            self.restored.append((account_id, token_id))

        def retain_verified_slots(self, _plan, _slot_nos):
            return None

    planner = Planner()

    class Reconciler:
        def reconcile(self, bundle, **_kwargs):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise ReplanRequired(
                    "provider_reconcile_probe_failed",
                    slot_no=2,
                    channel_id=702,
                    account_id=22,
                    token_id=12,
                    replan_scope="platform",
                    probe_observation={
                        "error_code": "provider_probe_transport_failed",
                        "replan_scope": "platform",
                    },
                )
            return SimpleNamespace(
                model=MODEL,
                diff_kind=PoolDiffKind.MEMBERSHIP_AND_COST,
                routing_generation=bundle.routing_generation,
                cost_generation=bundle.cost_generation,
                activated=True,
                actions=("repin_slot", "probe_slot"),
            )

        def fail_applying(self, _bundle, *, reason):
            assert reason == "provider_reconcile_probe_failed"

    result = command.run_model(
        MODEL,
        enabled=True,
        apply=True,
        minimum_ready_slots=2,
        planner=planner,
        reconciler_factory=lambda _plan: Reconciler(),
        apply_policy=apply_policy(
            ProviderPoolApplyStage.MEMBERSHIP_AND_COST,
            max_membership_changes=1,
        ),
        billing_contract=BILLING_SCOPE,
    )

    assert result["action"] == "apply-retry"
    assert result["retry_scope"] == "platform"
    assert result["provider_failover_count"] == 0
    assert planner.rejected == set()
    assert planner.restored == []
    assert attempts == 1


def test_apply_hard_blocks_a_generation_below_the_minimum_ready_slot_floor() -> None:
    with pytest.raises(
        command.ProviderPoolApplyCommandError,
        match="provider_apply_required_ready_slots_unavailable",
    ):
        command.run_model(
            MODEL,
            enabled=True,
            apply=True,
            minimum_ready_slots=4,
            planner=_Planner(_Plan(PoolDiffKind.NOOP)),
            reconciler_factory=lambda _plan: pytest.fail("remote factory must stay cold"),
            apply_policy=apply_policy(ProviderPoolApplyStage.COST_ONLY),
            billing_contract=BILLING_SCOPE,
        )


def test_apply_pin_failover_stops_when_candidate_budget_is_exhausted() -> None:
    plan = _Plan(PoolDiffKind.MEMBERSHIP_AND_COST)
    failed = []

    class Planner(_Planner):
        def reject_channel(self, _channel_id):
            return None

    class Reconciler:
        def reconcile(self, _bundle, **_kwargs):
            raise ReplanRequired(
                "provider_reconcile_probe_failed",
                slot_no=1,
                channel_id=701,
                replan_scope="channel",
            )

        def fail_applying(self, _bundle, *, reason):
            assert reason == "provider_reconcile_probe_failed"
            failed.append(reason)

    with pytest.raises(
        command.ProviderPoolApplyCommandError,
        match="provider_apply_pin_candidates_exhausted",
    ):
        command.run_model(
            MODEL,
            enabled=True,
            apply=True,
            required_ready_slots=2,
            planner=Planner(plan),
            reconciler_factory=lambda _plan: Reconciler(),
            apply_policy=apply_policy(
                ProviderPoolApplyStage.MEMBERSHIP_AND_COST,
                max_membership_changes=1,
            ),
            billing_contract=BILLING_SCOPE,
            pin_failover_attempts=1,
        )

    assert failed == ["provider_reconcile_probe_failed"]


@pytest.mark.parametrize(
    ("stage", "diff_kind", "limit", "error"),
    (
        (
            ProviderPoolApplyStage.COST_ONLY,
            PoolDiffKind.PRIORITY_AND_COST,
            0,
            "provider_apply_diff_not_authorized",
        ),
        (
            ProviderPoolApplyStage.PRIORITY_AND_COST,
            PoolDiffKind.MEMBERSHIP_AND_COST,
            0,
            "provider_apply_diff_not_authorized",
        ),
        (
            ProviderPoolApplyStage.MEMBERSHIP_AND_COST,
            PoolDiffKind.MEMBERSHIP_AND_COST,
            0,
            "provider_apply_membership_limit_required",
        ),
    ),
)
def test_apply_rollout_stage_blocks_unapproved_remote_factory(
    stage, diff_kind, limit, error
) -> None:
    plan = _Plan(diff_kind)
    with pytest.raises(ProviderPoolApplyError, match=error):
        command.run_model(
            MODEL,
            enabled=True,
            apply=True,
            required_ready_slots=2,
            planner=_Planner(plan),
            reconciler_factory=lambda _plan: pytest.fail("remote factory must stay cold"),
            apply_policy=apply_policy(stage, max_membership_changes=limit),
            billing_contract=BILLING_SCOPE,
        )


def test_apply_rollout_confirmation_is_bound_to_billing_scope() -> None:
    with pytest.raises(
        ProviderPoolApplyError,
        match="provider_apply_confirmation_mismatch",
    ):
        command.run_model(
            MODEL,
            enabled=True,
            apply=True,
            required_ready_slots=2,
            planner=_Planner(_Plan(PoolDiffKind.COST_ONLY)),
            reconciler_factory=lambda _plan: pytest.fail("remote factory must stay cold"),
            apply_policy=apply_policy(ProviderPoolApplyStage.COST_ONLY),
            billing_contract="billing=wrong-target",
        )


def test_apply_defaults_ready_slot_guard_to_planned_desired_size() -> None:
    result = command.run_model(
        MODEL,
        enabled=True,
        apply=True,
        planner=_Planner(_Plan(PoolDiffKind.NOOP)),
        reconciler_factory=lambda _plan: pytest.fail("remote factory must stay cold"),
        apply_policy=apply_policy(ProviderPoolApplyStage.COST_ONLY),
        billing_contract=BILLING_SCOPE,
    )

    assert result["action"] == "noop"


def test_apply_allows_vacant_slots_when_the_ready_floor_is_met() -> None:
    plan = _Plan(PoolDiffKind.NOOP)
    plan.bundle.slots = (
        plan.bundle.slots[0],
        SimpleNamespace(slot_state=SlotState.VACANT),
    )

    result = command.run_model(
        MODEL,
        enabled=True,
        apply=True,
        minimum_ready_slots=1,
        planner=_Planner(plan),
        reconciler_factory=lambda _plan: pytest.fail("remote factory must stay cold"),
        apply_policy=apply_policy(ProviderPoolApplyStage.COST_ONLY),
        billing_contract=BILLING_SCOPE,
    )

    assert result["action"] == "noop"


def test_apply_allows_all_slots_to_be_vacant_when_no_capped_candidate_exists() -> None:
    plan = _Plan(PoolDiffKind.NOOP)
    plan.bundle.slots = (
        SimpleNamespace(slot_state=SlotState.VACANT),
        SimpleNamespace(slot_state=SlotState.VACANT),
    )

    result = command.run_model(
        MODEL,
        enabled=True,
        apply=True,
        minimum_ready_slots=0,
        planner=_Planner(plan),
        reconciler_factory=lambda _plan: pytest.fail("remote factory must stay cold"),
        apply_policy=apply_policy(ProviderPoolApplyStage.COST_ONLY),
        billing_contract=BILLING_SCOPE,
    )

    assert result["action"] == "noop"


def test_apply_blocks_a_vacant_ready_set_before_remote_writes() -> None:
    plan = _Plan(PoolDiffKind.COST_ONLY)
    plan.bundle.slots = (
        plan.bundle.slots[0],
        SimpleNamespace(slot_state=SlotState.VACANT),
    )

    with pytest.raises(
        command.ProviderPoolApplyCommandError,
        match="provider_apply_required_ready_slots_unavailable",
    ):
        command.run_model(
            MODEL,
            enabled=True,
            apply=True,
            required_ready_slots=2,
            planner=_Planner(plan),
            reconciler_factory=lambda _plan: pytest.fail("remote factory must stay cold"),
            apply_policy=apply_policy(ProviderPoolApplyStage.COST_ONLY),
            billing_contract=BILLING_SCOPE,
        )


@pytest.mark.parametrize(
    ("margin_bps", "billing_floor"),
    (
        (1_999, Decimal("1.2")),
        (2_000, Decimal("1.199999")),
    ),
)
def test_apply_blocks_a_plan_below_the_provider_profit_floor(
    margin_bps: int,
    billing_floor: Decimal,
) -> None:
    plan = _Plan(PoolDiffKind.COST_ONLY)
    plan.bundle.cost_plan.margin_bps = margin_bps
    plan.bundle.cost_plan.billing_floor_multiplier = billing_floor

    with pytest.raises(
        command.ProviderPoolApplyCommandError,
        match="provider_apply_minimum_profit_not_met",
    ):
        command.run_model(
            MODEL,
            enabled=True,
            apply=True,
            required_ready_slots=2,
            planner=_Planner(plan),
            reconciler_factory=lambda _plan: pytest.fail("remote factory must stay cold"),
            apply_policy=apply_policy(ProviderPoolApplyStage.COST_ONLY),
            billing_contract=BILLING_SCOPE,
        )


def test_apply_policy_resolves_only_explicit_stage_limit_and_confirmation() -> None:
    resolved = command.resolve_apply_policy(
        None,
        None,
        None,
        environ={
            "PROVIDER_POOL_APPLY_STAGE": "membership_and_cost",
            "PROVIDER_POOL_MAX_MEMBERSHIP_CHANGES": "1",
            "PROVIDER_POOL_APPLY_CONFIRMATION": "confirmed-scope",
        },
    )

    assert resolved == ProviderPoolApplyPolicy(
        ProviderPoolApplyStage.MEMBERSHIP_AND_COST,
        1,
        "confirmed-scope",
    )


def context_row(
    *,
    token_id: int,
    binding_id: int,
    account_id: int,
    desired: bool,
) -> dict:
    return {
        "token_ref_id": token_id,
        "external_token_id": 10_000 + token_id,
        "token_status": "active" if desired else "disabled",
        "key_fingerprint": hashlib.sha256(API_KEY.encode()).hexdigest(),
        "key_secret_ref": f"/sessions/merchant-{token_id}.json#a6api_key",
        "merchant_id": token_id,
        "a6_user_id": 20_000 + token_id,
        "session_ref": f"/sessions/merchant-{token_id}.json",
        "merchant_status": "supplier_submitted" if desired else "retired",
        "roles": ["provider"] if desired else ["seller"],
        "enodot_binding_id": binding_id,
        "enodot_account_id": account_id,
        "binding_state": "bound" if desired else "retired",
        "model_scope": [MODEL],
    }


class _Cursor:
    def __init__(self, database):
        self.database = database
        self.description = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, query, params):
        self.database.calls.append((" ".join(query.split()), params))

    def fetchall(self):
        return self.database.rows


class _Connection:
    def __init__(self, database):
        self.database = database
        self.closed = False

    def cursor(self):
        return _Cursor(self.database)

    def close(self):
        self.closed = True


class _Database:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []
        self.connections = []

    def connect(self):
        connection = _Connection(self)
        self.connections.append(connection)
        return connection


def test_context_source_validates_desired_but_keeps_outgoing_drain_identity() -> None:
    database = _Database(
        [
            context_row(token_id=11, binding_id=31, account_id=21, desired=True),
            context_row(token_id=12, binding_id=32, account_id=22, desired=False),
        ]
    )
    desired = ready_slot(1, channel_id=701, binding_id=31, token_id=11, account_id=21)
    outgoing = ready_slot(1, channel_id=601, binding_id=32, token_id=12, account_id=22)

    records = PostgresProviderRemoteContextSource(database.connect).load(
        MODEL,
        (outgoing, desired),
        desired_token_ids=frozenset({11}),
    )

    assert [record.token_ref_id for record in records] == [11, 12]
    assert records[1].merchant_status == "retired"
    query, params = database.calls[0]
    assert "token.id = ANY(%s)" in query
    assert "binding.id = ANY(%s)" in query
    assert params == ([11, 12], [31, 32])
    assert database.connections[0].closed is True


def test_context_source_rejects_nonprovider_desired_identity() -> None:
    row = context_row(token_id=11, binding_id=31, account_id=21, desired=True)
    row["roles"] = ["seller"]
    database = _Database([row])
    desired = ready_slot(1, channel_id=701, binding_id=31, token_id=11, account_id=21)

    with pytest.raises(ProviderPoolApplyError, match="provider_apply_context_role_invalid"):
        PostgresProviderRemoteContextSource(database.connect).load(
            MODEL,
            (desired,),
            desired_token_ids=frozenset({11}),
        )


def test_context_source_skips_database_when_every_slot_is_vacant() -> None:
    database = _Database([])
    vacant = ProviderSlot(
        slot_no=1,
        channel_id=None,
        rank=None,
        priority=None,
        assignment_id=None,
        enodot_binding_id=None,
        token_id=None,
        account_id=None,
        slot_state=SlotState.VACANT,
        funding_eligible=False,
        desired_schedulable=False,
    )

    records = PostgresProviderRemoteContextSource(database.connect).load(
        MODEL,
        (vacant,),
        desired_token_ids=frozenset(),
    )

    assert records == ()
    assert database.connections == []


class _Client:
    instances = []

    def __init__(self, origin, *, proxy_url="", timeout=30):
        self.origin = origin.rstrip("/")
        self.proxy_url = proxy_url
        self.timeout = timeout
        self.login_calls = 0
        self.__class__.instances.append(self)

    def self_user(self, _user_id):
        return None

    def login(self, username, _password):
        self.login_calls += 1
        return {"id": 20_011, "username": username}

    def import_cookies(self, _cookies):
        return None

    def export_cookies(self):
        return {"session": "refreshed"}

    def marketplace_pins(self, _user_id):
        return []


class _SessionStore:
    saved = []

    def __init__(self, path):
        self.path = path

    def load(self):
        return SimpleNamespace(
            user_id=20_011,
            origin="https://a6api.com",
            a6api_key=API_KEY,
            status="supplier_submitted",
            cookies={"session": "expired"},
            username="merchant-11",
            email="merchant-11@example.test",
            password="fixture-password",
            supplier_id=1,
            request_id=1,
            updated_at="fixture",
        )

    def save(self, record):
        self.saved.append(record)


def test_panel_session_is_proxied_and_lazy_while_probe_client_is_direct(
    monkeypatch, tmp_path
) -> None:
    _Client.instances = []
    _SessionStore.saved = []
    monkeypatch.setattr(command, "A6ApiClient", _Client)
    monkeypatch.setattr(command, "SessionStore", _SessionStore)
    session_path = tmp_path / "merchant-11.json"
    context = ProviderRemoteContextRecord(
        token_ref_id=11,
        external_token_id=10_011,
        merchant_id=11,
        a6_user_id=20_011,
        session_ref=str(session_path),
        merchant_status="supplier_submitted",
        roles=("provider",),
        token_status="active",
        key_fingerprint=hashlib.sha256(API_KEY.encode()).hexdigest(),
        key_secret_ref=f"{session_path}#a6api_key",
        enodot_binding_id=31,
        enodot_account_id=21,
        binding_state="bound",
        model_scope=(MODEL,),
    )

    slot_context = command._slot_context(
        context,
        sessions_dir=tmp_path,
        proxy_by_user_id={20_011: "http://panel-proxy"},
    )
    direct = command._direct_probe_client(4)

    panel = slot_context.panel_client.client
    assert panel.proxy_url == "http://panel-proxy"
    assert panel.login_calls == 0
    assert direct.origin == "https://a6.a6api.com"
    assert direct.proxy_url == ""
    assert direct is not panel
    slot_context.panel_client.marketplace_pins(20_011)
    assert panel.login_calls == 1
    assert len(_SessionStore.saved) == 1


def test_outgoing_drain_context_does_not_load_retired_session_or_proxy(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr(
        command,
        "SessionStore",
        lambda _path: pytest.fail("outgoing drain must not load a session"),
    )
    context = ProviderRemoteContextRecord(
        token_ref_id=11,
        external_token_id=10_011,
        merchant_id=11,
        a6_user_id=20_011,
        session_ref=str(tmp_path / "missing-retired-session.json"),
        merchant_status="retired",
        roles=("seller",),
        token_status="disabled",
        key_fingerprint=hashlib.sha256(API_KEY.encode()).hexdigest(),
        key_secret_ref="retired-secret-ref",
        enodot_binding_id=31,
        enodot_account_id=21,
        binding_state="retired",
        model_scope=(MODEL,),
    )

    slot_context = command._slot_context(
        context,
        sessions_dir=tmp_path,
        proxy_by_user_id={},
        require_secret=False,
    )

    assert slot_context.panel_client is None
    assert slot_context.probe_api_key == ""


class _BillingAdmin:
    def __init__(self, api_key_ids=(53, 54)) -> None:
        self.api_key_ids = api_key_ids

    def get_user(self, user_id):
        return {"id": user_id, "status": "active"}

    def request(self, method, path):
        assert (method, path) == ("GET", "admin/users/1/api-keys")
        return {
            "items": [
                {
                    "id": key_id,
                    "user_id": 1,
                    "group_id": 26,
                    "status": "active",
                    "group": {
                        "id": 26,
                        "name": "ToA6API",
                        "platform": "openai",
                        "status": "active",
                    },
                }
                for key_id in self.api_key_ids
            ]
        }


def test_billing_target_contract_reads_back_complete_affected_key_scope() -> None:
    target = command.ProviderBillingTargetContract(
        MODEL,
        26,
        1,
        (54, 53),
        "ToA6API",
        "OpenAI",
    )

    command._validate_billing_target(_BillingAdmin(), target)

    assert target.affected_api_key_ids == (53, 54)
    assert target.confirmation_scope == BILLING_SCOPE


def test_billing_target_contract_fails_when_group_gains_an_unreviewed_key() -> None:
    target = command.ProviderBillingTargetContract(
        MODEL,
        26,
        1,
        (53, 54),
        "ToA6API",
        "openai",
    )

    with pytest.raises(
        ProviderPoolApplyError,
        match="provider_apply_billing_key_scope_mismatch",
    ):
        command._validate_billing_target(_BillingAdmin((53, 54, 55)), target)


def test_systemd_controller_stays_default_off_and_bounds_remote_mutations() -> None:
    root = Path(__file__).resolve().parents[1]
    service = (root / "deploy/systemd/a6-trader-provider-pool-apply.service").read_text()
    timer = (root / "deploy/systemd/a6-trader-provider-pool-apply.timer").read_text()

    assert "PROVIDER_POOL_RECONCILE_ENABLED" in service
    assert "PROVIDER_POOL_APPLY_STAGE" in service
    assert "PROVIDER_POOL_MAX_MEMBERSHIP_CHANGES" in service
    assert "PROVIDER_POOL_APPLY_CONFIRMATION" in service
    assert "--provider-pool-reconcile-enabled" not in service
    assert "apply_a6_provider_pool.py" in service
    assert "--apply" in service
    assert "provider-control.lock" in service
    assert "seller-control.lock" not in service
    assert "--nonblock" in service
    assert "--conflict-exit-code 75" in service
    assert "SuccessExitStatus=75" in service
    assert "--proxy-map-file" in service
    assert "--enodot-admin-key-file" in service
    assert "--billing-group-id 26" in service
    assert "--billing-user-id 1" in service
    assert "--billing-api-key-ids 53,54" in service
    assert "PROVIDER_REQUEST_PLANE_URL=http://127.0.0.1:48769" in service
    assert "--required-ready-slots" not in service
    assert "--initial-max-active-slots 12" in service
    assert "--observer-freshness-seconds 120" in service
    assert "--pin-failover-attempts 30" in service
    assert "--pin-probe-mode disabled" in service
    assert "a6-trader-provider-pool-shadow.service" not in service
    assert "--remote-max-workers 6" in service
    assert "--remote-request-timeout-seconds 5" in service
    assert "--round-time-budget-seconds 60" in service
    assert "TimeoutStartSec=90s" in service
    assert "--margin-bps 2000" in service
    assert "--maximum-provider-cost-cny 0.3" in service
    assert "ReadWritePaths=/var/lib/a6-trader" in service
    assert "OnUnitInactiveSec=15s" in timer
    assert "OnUnitActiveSec=30s" not in timer


def test_apply_main_refreshes_inventory_without_shadow(monkeypatch, capsys) -> None:
    events = []

    class Projector:
        def __init__(self, connection_factory):
            assert callable(connection_factory)

        def reconcile(self, model):
            events.append(("inventory", model))

    monkeypatch.setattr(command, "PostgresProviderInventoryProjector", Projector)
    monkeypatch.setattr(command.psycopg, "connect", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(
        command,
        "build_dispatch_projector",
        lambda *_args, **_kwargs: events.append(("dispatch", MODEL)) or object(),
    )
    monkeypatch.setattr(command, "_runtime_text", lambda *_args, **_kwargs: "http://request-plane")
    monkeypatch.setattr(command, "resolve_billing_target", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(command, "ProviderPoolApplyPlanner", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(command, "resolve_apply_policy", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(
        command,
        "run_model",
        lambda *_args, **_kwargs: {"success": True, "events": list(events)},
    )
    monkeypatch.setenv("A6_TRADE_DATABASE_DSN", "postgresql://fixture")
    monkeypatch.setattr(
        "sys.argv",
        [
            "apply_a6_provider_pool.py",
            "--model",
            MODEL,
            "--apply",
            "--provider-pool-reconcile-enabled",
        ],
    )

    command.main()

    assert events[0] == ("inventory", MODEL)
    assert events[1] == ("dispatch", MODEL)
    assert json.loads(capsys.readouterr().out)["success"] is True


def test_apply_script_uses_the_canonical_cost_builder_contract() -> None:
    plan = build_cost_plan(
        {1: Decimal("1")},
        retry_reserve_multiplier=Decimal("0"),
        bootstrap_retry_reserve=Decimal("0"),
        realized_cost_p95_multiplier=None,
    )
    assert plan.transition_floor_multiplier >= plan.billing_floor_multiplier


def test_provider_apply_uses_the_provider_cost_contract_default_buffer() -> None:
    assert command.parser().parse_args(["--model", MODEL]).margin_bps == 2000
