from __future__ import annotations

import hashlib
import re
from contextlib import nullcontext
from dataclasses import replace
from decimal import Decimal

import pytest

from a6api_auto.enodot_admin import EnodotAdminClient
from a6api_auto.errors import A6ProbeTransportError
from a6api_auto.trade_provider_cost import build_cost_plan
from a6api_auto.trade_provider_pool import (
    InMemoryProviderPoolStore,
    ProviderBundle,
    ProviderSlot,
    SlotState,
)
from a6api_auto.trade_provider_pool_reconciler import (
    ProviderPoolReconciler,
    ProviderProbeResult,
    ReplanRequired,
)
from a6api_auto.trade_provider_pool_remote import (
    EnodotBillingFloorTarget,
    EnodotGroupUserBillingFloor,
    PostgresProviderPinBindingStore,
    PostgresProviderProbeHistory,
    ProviderPoolRemoteAdapter,
    ProviderPoolRemoteError,
    ProviderProbeProfile,
    ProviderRemoteSlotContext,
    build_provider_probe_profile,
)

MODEL = "gpt-5.6-sol"
PROBE_KEY = "sk-provider-probe-fixture"
PROBE_KEY_FINGERPRINT = hashlib.sha256(PROBE_KEY.encode()).hexdigest()
RESPONSES_PROFILE = ProviderProbeProfile(
    protocol="responses",
    input_text="Answer briefly: what is 12 plus 7?",
    max_output_tokens=8,
    delay_ms=0,
    request_id="fixture-response-request-1",
)
CHAT_PROFILE = ProviderProbeProfile(
    protocol="chat_completions",
    input_text="Reply briefly: which is larger, 14 or 9?",
    max_output_tokens=8,
    delay_ms=0,
    request_id="fixture-chat-request-1",
)


class PanelClient:
    def __init__(self) -> None:
        self.pins: dict[tuple[int, int, str], int] = {}
        self.calls = []

    def marketplace_pin(
        self,
        user_id,
        *,
        token_id,
        channel_id,
        model,
        fallback_to_smart_routing=False,
    ):
        self.calls.append(
            (
                "pin",
                user_id,
                token_id,
                channel_id,
                model,
                fallback_to_smart_routing,
            )
        )
        self.pins[(user_id, token_id, model)] = channel_id
        return {}

    def marketplace_pins(self, user_id):
        self.calls.append(("read", user_id))
        return [
            {
                "token_id": token_id,
                "model_name": model,
                "channel_id": channel_id,
            }
            for (pin_user_id, token_id, model), channel_id in self.pins.items()
            if pin_user_id == user_id
        ]


class ProbeClient:
    origin = "https://a6.a6api.com"
    proxy_url = ""

    def __init__(
        self,
        statuses=(200, 200, 200),
        *,
        chat_responses=((200, {"choices": [{"message": {"content": "."}}]}),) * 3,
    ) -> None:
        self.statuses = iter(statuses)
        self.chat_responses = iter(chat_responses)
        self.calls = []
        self.chat_calls = []

    def provider_responses_probe(
        self,
        api_key,
        model,
        *,
        max_output_tokens=16,
        timeout=24,
        connect_timeout=8,
        input_text=None,
        request_id=None,
    ):
        self.calls.append(
            (
                api_key,
                model,
                max_output_tokens,
                timeout,
                connect_timeout,
                input_text,
                request_id,
            )
        )
        return next(self.statuses), {"probe_event": "response.created"}

    def provider_chat_completions_probe(
        self,
        api_key,
        model,
        *,
        max_output_tokens=16,
        timeout=24,
        connect_timeout=8,
        input_text=None,
        request_id=None,
    ):
        self.chat_calls.append(
            (
                api_key,
                model,
                max_output_tokens,
                timeout,
                connect_timeout,
                input_text,
                request_id,
            )
        )
        return next(self.chat_responses)


class EnodotAdmin:
    def __init__(self) -> None:
        self.accounts = {
            21: {
                "id": 21,
                "status": "active",
                "priority": 1,
                "rate_multiplier": 1.0,
                "schedulable": False,
                "extra": {"unrelated": "preserved"},
            }
        }
        self.group_rates = {26: {501: 1.0}}
        self.calls = []
        self.ignored_account_fields: set[str] = set()

    def get_account(self, account_id):
        self.calls.append(("get_account", account_id))
        return dict(self.accounts[account_id])

    def update_account(self, account_id, payload, *, idempotency_key=""):
        self.calls.append(("update_account", account_id, dict(payload), idempotency_key))
        self.accounts[account_id].update(
            {
                key: value
                for key, value in payload.items()
                if key not in self.ignored_account_fields
            }
        )
        return dict(self.accounts[account_id])

    def set_schedulable(self, account_id, schedulable, *, idempotency_key=""):
        self.calls.append(("schedulable", account_id, schedulable, idempotency_key))
        self.accounts[account_id]["schedulable"] = schedulable
        return dict(self.accounts[account_id])

    def get_group_rate_multipliers(self, group_id):
        self.calls.append(("get_group_rates", group_id))
        return [
            {
                "user_id": user_id,
                "rate_multiplier": multiplier,
                "visible_to_user": False,
            }
            for user_id, multiplier in sorted(self.group_rates[group_id].items())
        ]

    def merge_group_rate_multiplier(
        self,
        group_id,
        user_id,
        rate_multiplier,
        *,
        lock,
        visible_to_user=False,
        idempotency_key="",
    ):
        self.calls.append(
            (
                "merge_group_rate",
                group_id,
                user_id,
                rate_multiplier,
                lock,
                visible_to_user,
                idempotency_key,
            )
        )
        self.group_rates[group_id][user_id] = rate_multiplier
        return self.get_group_rate_multipliers(group_id)


class PinStore:
    def __init__(self) -> None:
        self.bindings = {}
        self.calls = []

    def confirm(self, token_ref_id, model, channel_id, *, change_reason):
        self.calls.append(("confirm", token_ref_id, model, channel_id, change_reason))
        self.bindings[(token_ref_id, model, channel_id)] = 801
        return 801

    def current(self, token_ref_id, model, channel_id):
        self.calls.append(("current", token_ref_id, model, channel_id))
        return self.bindings.get((token_ref_id, model, channel_id))


def provider_bundle() -> ProviderBundle:
    plan = build_cost_plan(
        {1: "1"},
        retry_reserve_multiplier="0",
        bootstrap_retry_reserve="0",
        realized_cost_p95_multiplier=None,
    )
    return ProviderBundle(
        model=MODEL,
        routing_generation=1,
        parent_routing_generation=None,
        cost_generation=1,
        parent_cost_generation=None,
        generation_source="bootstrap",
        source_hash="a" * 64,
        ordered_channel_ids=(701,),
        standby_channel_ids=(),
        desired_size=1,
        slots=(
            ProviderSlot(
                slot_no=1,
                channel_id=701,
                rank=1,
                priority=2,
                assignment_id="inventory-1",
                enodot_binding_id=31,
                token_id=11,
                account_id=21,
                slot_state=SlotState.READY,
                funding_eligible=True,
                desired_schedulable=True,
                observed_schedulable=True,
                desired_cost_multiplier=Decimal("1"),
                observed_cost_multiplier=Decimal("1"),
                confirmed_pin_binding_id=700,
                pin_confirmed=True,
                probe_confirmed=True,
            ),
        ),
        cost_plan=plan,
    )


def remote_fixture(
    *,
    panel=None,
    probe=None,
    admin=None,
    profile=RESPONSES_PROFILE,
    history=None,
    activation_probe_mode="required",
    readback_max_attempts=1,
    pre_enable_guard=lambda _context: None,
):
    panel = panel or PanelClient()
    probe = probe or ProbeClient()
    admin = admin or EnodotAdmin()
    pin_store = PinStore()
    billing = EnodotGroupUserBillingFloor(
        admin,
        {MODEL: EnodotBillingFloorTarget(26, 501)},
        group_lock=lambda _group_id: nullcontext(),
    )
    context = ProviderRemoteSlotContext(
        token_ref_id=11,
        external_token_id=10_011,
        merchant_id=1,
        a6_user_id=9_001,
        enodot_binding_id=31,
        enodot_account_id=21,
        key_fingerprint=PROBE_KEY_FINGERPRINT,
        panel_client=panel,
        probe_api_key=PROBE_KEY,
    )
    remote = ProviderPoolRemoteAdapter(
        MODEL,
        admin,
        billing,
        pin_store,
        probe,
        (context,),
        pre_enable_guard=pre_enable_guard,
        probe_timeout_seconds=4,
        probe_connect_timeout_seconds=3,
        probe_max_output_tokens=8,
        probe_history=history,
        activation_probe_mode=activation_probe_mode,
        readback_max_attempts=readback_max_attempts,
        probe_profile_factory=lambda _maximum: profile,
        sleep=lambda _seconds: None,
    )
    return remote, panel, probe, admin, pin_store, context


def test_remote_readback_retries_only_the_failed_slot_transport() -> None:
    class FlakyPanel(PanelClient):
        proxy_url = "http://proxy.example.invalid"

        def __init__(self) -> None:
            super().__init__()
            self.read_attempts = 0

        def marketplace_pins(self, user_id):
            self.read_attempts += 1
            if self.read_attempts == 1:
                raise RuntimeError("a6_transport_failed")
            return super().marketplace_pins(user_id)

    panel = FlakyPanel()
    remote, _panel, _probe, _admin, pin_store, context = remote_fixture(
        panel=panel,
        activation_probe_mode="disabled",
        readback_max_attempts=2,
    )
    panel.pins[(context.a6_user_id, context.external_token_id, MODEL)] = 701
    pin_store.bindings[(context.token_ref_id, MODEL, 701)] = 801

    readback = remote.readback(provider_bundle())

    assert readback.slots[1].channel_id == 701
    assert panel.read_attempts == 2


def test_remote_readback_transport_error_contains_secret_free_slot_diagnostics() -> None:
    class FailingPanel(PanelClient):
        proxy_url = "http://proxy.example.invalid"

        def marketplace_pins(self, user_id):
            del user_id
            raise RuntimeError("a6_transport_failed")

    remote, *_rest = remote_fixture(
        panel=FailingPanel(),
        activation_probe_mode="disabled",
        readback_max_attempts=2,
    )

    with pytest.raises(ProviderPoolRemoteError) as raised:
        remote.readback(provider_bundle())

    assert raised.value.code == "a6_transport_failed"
    assert raised.value.diagnostics == {
        "a6_user_id": 9001,
        "account_id": 21,
        "attempts": 2,
        "cause_code": "a6_transport_failed",
        "channel_id": 701,
        "elapsed_ms": raised.value.diagnostics["elapsed_ms"],
        "phase": "a6_marketplace_pins",
        "phase_elapsed_ms": raised.value.diagnostics["phase_elapsed_ms"],
        "proxy_configured": True,
        "slot_no": 1,
        "token_ref_id": 11,
    }
    assert "proxy.example" not in str(raised.value.diagnostics)


def test_remote_adapter_completes_strict_saga_with_external_token_and_readbacks() -> None:
    remote, panel, probe, admin, pin_store, _context = remote_fixture()
    store = InMemoryProviderPoolStore()

    report = ProviderPoolReconciler(
        store,
        remote,
        require_remote_readback=True,
    ).reconcile(provider_bundle())

    assert report.activated is True
    assert report.actions == (
        "stage_billing_floor",
        "repin_slot",
        "probe_slot",
        "set_priority",
        "set_assignment_metadata",
        "enable_schedulable_after_dispatch",
    )
    pin_call = next(call for call in panel.calls if call[0] == "pin")
    assert pin_call[2:] == (10_011, 701, MODEL, False)
    assert pin_store.calls[0][1:4] == (11, MODEL, 701)
    assert [call[0] for call in pin_store.calls] == ["confirm", "current", "current"]
    assert len(probe.calls) == 1
    assert probe.calls == [
        (
            "sk-provider-probe-fixture",
            MODEL,
            8,
            4.0,
            3.0,
            RESPONSES_PROFILE.input_text,
            RESPONSES_PROFILE.request_id,
        )
    ]
    assert probe.chat_calls == []
    assert admin.accounts[21] == {
        "id": 21,
        "status": "active",
        "priority": 2,
        "rate_multiplier": 1.0,
        "schedulable": True,
        "extra": {
            "unrelated": "preserved",
            "a6_provider": True,
            "a6_api_key_fingerprint": PROBE_KEY_FINGERPRINT,
            "a6_provider_routing_generation": 1,
            "a6_provider_cost_generation": 1,
            "a6_provider_slot_no": 1,
            "a6_provider_token_ref_id": 11,
            "a6_provider_channel_id": 701,
            "external_a6_token_id": 10_011,
            "a6_provider_rank": 1,
            "a6_provider_cost_multiplier": "1",
            "a6_provider_source_hash": "a" * 64,
            "a6_provider_cost_input_hash": "a" * 64,
            "a6_provider_cost_output_hash": provider_bundle().cost_plan.cost_output_hash,
        },
    }
    assert Decimal(str(admin.group_rates[26][501])) == (
        provider_bundle().cost_plan.transition_floor_multiplier
    )
    active = store.active(MODEL)
    assert active is not None
    assert active.slots[0].confirmed_pin_binding_id == 801
    action_keys = [
        call[-1]
        for call in admin.calls
        if call[0] in {"merge_group_rate", "update_account", "schedulable"}
    ]
    action_keys.append(pin_store.calls[0][-1])
    assert len(action_keys) == 5
    assert len(set(action_keys)) == 5
    assert all(
        len(key) <= 128 and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]*", key)
        for key in action_keys
    )


def test_observer_driven_activation_pins_and_reads_back_without_any_probe() -> None:
    class ForbiddenHistory:
        def reusable_result(self, *_args):
            raise AssertionError("probe history must stay cold")

        def platform_backoff(self, *_args):
            raise AssertionError("probe backoff must stay cold")

    remote, _panel, probe, _admin, _pin_store, _context = remote_fixture(
        history=ForbiddenHistory(),
        activation_probe_mode="disabled",
    )
    store = InMemoryProviderPoolStore()

    report = ProviderPoolReconciler(
        store,
        remote,
        require_remote_readback=True,
        activation_probe_required=False,
    ).reconcile(provider_bundle())

    assert report.activated is True
    assert "repin_slot" in report.actions
    assert "probe_slot" not in report.actions
    assert probe.calls == []
    assert probe.chat_calls == []
    active = store.active(MODEL)
    assert active is not None
    assert active.slots[0].pin_confirmed is True
    assert active.slots[0].probe_confirmed is True
    raw_readback = remote.readback(provider_bundle())
    assert raw_readback.slots[1].pin_confirmed is True
    assert raw_readback.slots[1].probe_confirmed is False
    assert probe.calls == []
    assert probe.chat_calls == []


def test_remote_readback_preserves_mismatched_metadata_for_slot_diagnostics() -> None:
    remote, _panel, _probe, admin, _pin_store, _context = remote_fixture(
        activation_probe_mode="disabled",
    )
    desired = provider_bundle()
    slot = desired.slots[0]
    admin.accounts[21].update(
        {
            "priority": slot.priority,
            "rate_multiplier": float(slot.desired_cost_multiplier),
            "schedulable": False,
        }
    )
    remote.set_assignment_metadata(desired, slot, idempotency_key="fixture-metadata")
    admin.accounts[21]["extra"]["a6_provider_routing_generation"] = (
        desired.routing_generation - 1
    )

    readback = remote.readback(desired)

    assert (
        readback.slots[1].assignment_metadata["a6_provider_routing_generation"]
        == desired.routing_generation - 1
    )


def test_remote_adapter_reads_exact_schedulable_boolean_for_failure_repair() -> None:
    remote, _panel, _probe, admin, _pin_store, _context = remote_fixture()
    provider_slot = provider_bundle().slots[0]

    assert remote.read_slot_schedulable(provider_slot) is False
    admin.accounts[21]["schedulable"] = True
    assert remote.read_slot_schedulable(provider_slot) is True
    admin.accounts[21]["schedulable"] = "true"
    with pytest.raises(
        ProviderPoolRemoteError,
        match="provider_remote_schedulable_readback_invalid",
    ):
        remote.read_slot_schedulable(provider_slot)


def test_remote_adapter_runs_owned_channel_ban_before_every_enable_readback() -> None:
    events = []

    def guard(context) -> None:
        events.append(("ban", context.external_token_id))

    remote, _panel, _probe, admin, _pin_store, _context = remote_fixture(
        pre_enable_guard=guard,
    )
    provider_slot = provider_bundle().slots[0]

    assert remote.set_schedulable(
        provider_slot,
        True,
        idempotency_key="enable-after-ban",
    ) is True
    events.extend(
        ("schedulable", call[2])
        for call in admin.calls
        if call[0] == "schedulable"
    )

    assert events == [("ban", 10_011), ("schedulable", True)]

    assert remote.set_schedulable(
        provider_slot,
        True,
        idempotency_key="verify-existing-after-ban",
    ) is False
    assert events == [
        ("ban", 10_011),
        ("schedulable", True),
        ("ban", 10_011),
    ]


def test_probe_preserves_quota_failure_and_assigns_liquidity_scope() -> None:
    class QuotaProbe(ProbeClient):
        def provider_responses_probe(self, *args, **kwargs):
            self.calls.append((args, kwargs))
            return 403, {
                "error": {
                    "type": "insufficient_quota",
                    "code": "insufficient_quota",
                    "reason": "current account quota is insufficient",
                    "request_id": "probe-request-1",
                    "secret": "must-not-persist",
                }
            }

        def provider_chat_completions_probe(self, *args, **kwargs):
            raise AssertionError("chat probe must not run after quota failure")

    remote, *_rest = remote_fixture(probe=QuotaProbe())

    result = remote.probe_slot(provider_bundle().slots[0], idempotency_key="probe-1")

    assert result.confirmed is False
    assert result.http_status == 403
    assert result.error_code == "insufficient_quota"
    assert result.failure_owner == "liquidity"
    assert result.replan_scope == "liquidity"
    assert result.document["response"] == {
        "error": {
            "type": "insufficient_quota",
            "code": "insufficient_quota",
            "reason": "current account quota is insufficient",
            "request_id": "probe-request-1",
        }
    }
    assert result.document["profile"]["prompt_fingerprint"] == (
        RESPONSES_PROFILE.prompt_fingerprint
    )
    assert "secret" not in str(result.document)


def test_successful_direct_probe_is_reused_by_activation_readbacks() -> None:
    probe = ProbeClient((200, 503))
    remote, *_rest = remote_fixture(probe=probe)
    store = InMemoryProviderPoolStore()

    report = ProviderPoolReconciler(
        store,
        remote,
        require_remote_readback=True,
    ).reconcile(provider_bundle())

    assert report.activated is True
    assert len(probe.calls) == 1
    assert len(probe.chat_calls) == 0
    assert store.active(MODEL) is not None


def test_random_probe_profiles_vary_without_exceeding_budget() -> None:
    profiles = tuple(build_provider_probe_profile(16) for _ in range(64))

    assert len({profile.request_id for profile in profiles}) == len(profiles)
    assert len({profile.prompt_fingerprint for profile in profiles}) >= 60
    assert len({profile.delay_ms for profile in profiles}) >= 8
    assert {profile.protocol for profile in profiles} == {
        "responses",
        "chat_completions",
    }
    assert all(1 <= profile.max_output_tokens <= 16 for profile in profiles)
    assert all(250 <= profile.delay_ms <= 2_750 for profile in profiles)
    assert all(8 <= len(profile.input_text) <= 48 for profile in profiles)
    assert all(profile.input_text.endswith("?") for profile in profiles)


def test_probe_transport_failure_persists_profile_and_curl_stage() -> None:
    class TransportProbe(ProbeClient):
        def provider_responses_probe(self, *args, **kwargs):
            raise A6ProbeTransportError(
                "provider_probe_transport_failed",
                {
                    "protocol": "responses",
                    "request_id": kwargs["request_id"],
                    "stage": "tls_handshake",
                    "curl_code": 28,
                    "http_status": None,
                    "elapsed_ms": 8002,
                    "exception_type": "RequestException",
                    "detail": "SSL connection timeout",
                },
            )

    remote, *_rest = remote_fixture(probe=TransportProbe())

    result = remote.probe_slot(provider_bundle().slots[0], idempotency_key="probe-1")

    assert result.confirmed is False
    assert result.failure_owner == "platform"
    assert result.replan_scope == "platform"
    assert result.document["profile"]["prompt_fingerprint"] == (
        RESPONSES_PROFILE.prompt_fingerprint
    )
    assert result.document["transport"]["curl_code"] == 28
    assert result.document["transport"]["stage"] == "tls_handshake"


def test_unknown_500_is_platform_failure_with_request_id() -> None:
    class UnknownErrorProbe(ProbeClient):
        def provider_responses_probe(self, *args, **kwargs):
            return 500, {
                "error": {
                    "type": "unknown_api_error",
                    "code": "unknown_api_error",
                    "reason": "request handling failed",
                    "request_id": "upstream-request-1",
                    "secret": "must-not-persist",
                }
            }

    remote, *_rest = remote_fixture(probe=UnknownErrorProbe())

    result = remote.probe_slot(provider_bundle().slots[0], idempotency_key="probe-1")

    assert result.http_status == 500
    assert result.error_code == "unknown_api_error"
    assert result.failure_owner == "platform"
    assert result.replan_scope == "platform"
    assert result.document["response"]["error"]["request_id"] == "upstream-request-1"
    assert "secret" not in str(result.document)


def test_probe_history_reuses_success_and_backs_off_platform_failures() -> None:
    history = PostgresProviderProbeHistory(lambda: None)
    success = history._decision(
        (
            {
                "id": 10,
                "idempotency_key": "probe-success",
                "status": "completed",
                "age_seconds": 3,
                "observed_after": {
                    "probe": {
                        "confirmed": True,
                        "protocol": "responses",
                        "failure_owner": "none",
                    }
                },
            },
        )
    )
    backed_off = history._decision(
        (
            {
                "id": 12,
                "idempotency_key": "probe-failure-2",
                "status": "failed",
                "age_seconds": 0,
                "observed_after": {
                    "probe": {
                        "confirmed": False,
                        "protocol": "chat_completions",
                        "error_code": "provider_probe_transport_failed",
                        "failure_owner": "platform",
                    }
                },
            },
            {
                "id": 11,
                "idempotency_key": "probe-failure-1",
                "status": "failed",
                "age_seconds": 100,
                "observed_after": {
                    "probe": {
                        "confirmed": False,
                        "protocol": "responses",
                        "error_code": "unknown_api_error",
                        "failure_owner": "platform",
                    }
                },
            },
        )
    )

    assert isinstance(success, ProviderProbeResult)
    assert success.confirmed is True
    assert success.document["request_sent"] is False
    assert isinstance(backed_off, ProviderProbeResult)
    assert backed_off.error_code == "provider_probe_backoff_active"
    assert backed_off.document["request_sent"] is False
    assert backed_off.document["consecutive_platform_failures"] == 2
    assert 135_000 <= backed_off.document["retry_after_ms"] <= 225_000


@pytest.mark.parametrize(
    "chat_response",
    (
        (503, {"choices": [{"message": {"content": "."}}]}),
        (200, {"choices": []}),
        (200, {}),
    ),
)
def test_chat_probe_failure_keeps_generation_inactive(
    chat_response,
) -> None:
    probe = ProbeClient(
        chat_responses=(chat_response,)
    )
    remote, *_rest = remote_fixture(probe=probe, profile=CHAT_PROFILE)
    store = InMemoryProviderPoolStore()

    with pytest.raises(
        ReplanRequired,
        match="provider_reconcile_probe_failed",
    ):
        ProviderPoolReconciler(
            store,
            remote,
            require_remote_readback=True,
        ).reconcile(provider_bundle())

    assert len(probe.calls) == 0
    assert len(probe.chat_calls) == 1
    assert store.active(MODEL) is None


def test_vacant_only_bundle_needs_no_secret_slot_context() -> None:
    _remote, _panel, probe, admin, pin_store, _context = remote_fixture()
    billing = EnodotGroupUserBillingFloor(
        admin,
        {MODEL: EnodotBillingFloorTarget(26, 501)},
        group_lock=lambda _group_id: nullcontext(),
    )
    remote = ProviderPoolRemoteAdapter(
        MODEL,
        admin,
        billing,
        pin_store,
        probe,
        (),
        pre_enable_guard=lambda _context: None,
    )
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
    cost = build_cost_plan(
        {},
        retry_reserve_multiplier=None,
        bootstrap_retry_reserve=Decimal("0.2"),
        realized_cost_p95_multiplier=None,
    )
    desired = replace(
        provider_bundle(),
        ordered_channel_ids=(),
        desired_size=1,
        slots=(vacant,),
        cost_plan=cost,
    )
    store = InMemoryProviderPoolStore()

    report = ProviderPoolReconciler(
        store,
        remote,
        require_remote_readback=True,
    ).reconcile(desired)

    assert report.activated is True
    assert report.actions == ("finalize_billing_floor",)
    assert probe.calls == []
    assert store.active(MODEL).slots[0].slot_state is SlotState.VACANT


def test_slot_cost_write_fails_closed_when_separate_get_keeps_old_value() -> None:
    admin = EnodotAdmin()
    admin.ignored_account_fields.add("rate_multiplier")
    remote, *_rest = remote_fixture(admin=admin)

    with pytest.raises(
        ProviderPoolRemoteError,
        match="provider_remote_slot_cost_readback_mismatch",
    ):
        remote.set_slot_cost(
            provider_bundle().slots[0],
            Decimal("1.25"),
            idempotency_key="fixture-cost",
        )


def test_stale_action_retry_reads_remote_effect_before_reissuing_mutation() -> None:
    remote, panel, _probe, admin, pin_store, _context = remote_fixture()
    slot = provider_bundle().slots[0]
    desired_floor = provider_bundle().cost_plan.transition_floor_multiplier
    admin.group_rates[26][501] = float(desired_floor)
    admin.accounts[21].update(
        {
            "priority": slot.priority,
            "rate_multiplier": float(slot.desired_cost_multiplier),
            "schedulable": False,
        }
    )
    panel.pins[(9_001, 10_011, MODEL)] = 701
    desired_metadata = {
        "a6_provider": True,
        "a6_api_key_fingerprint": PROBE_KEY_FINGERPRINT,
        "a6_provider_routing_generation": 1,
        "a6_provider_cost_generation": 1,
        "a6_provider_slot_no": 1,
        "a6_provider_token_ref_id": 11,
        "a6_provider_channel_id": 701,
        "external_a6_token_id": 10_011,
        "a6_provider_rank": 1,
        "a6_provider_cost_multiplier": "1",
        "a6_provider_source_hash": "a" * 64,
        "a6_provider_cost_input_hash": "a" * 64,
        "a6_provider_cost_output_hash": provider_bundle().cost_plan.cost_output_hash,
    }
    admin.accounts[21]["extra"].update(desired_metadata)

    assert (
        remote.stage_billing_floor(
            MODEL,
            desired_floor,
            idempotency_key="retry-floor",
        )
        is False
    )
    assert (
        remote.set_slot_cost(
            slot,
            slot.desired_cost_multiplier,
            idempotency_key="retry-cost",
        )
        is False
    )
    assert remote.drain_slot(slot, idempotency_key="retry-drain") is False
    assert remote.repin_slot(slot, idempotency_key="retry-pin") is False
    assert remote.set_priority(slot, idempotency_key="retry-priority") is False
    assert (
        remote.set_assignment_metadata(
            provider_bundle(),
            slot,
            idempotency_key="retry-assignment-metadata",
        )
        is False
    )
    assert (
        remote.set_schedulable(slot, False, idempotency_key="retry-schedulable")
        is False
    )

    assert not any(
        call[0] in {"merge_group_rate", "update_account", "schedulable"}
        for call in admin.calls
    )
    assert not any(call[0] == "pin" for call in panel.calls)
    assert pin_store.calls[0][0] == "confirm"


def test_assignment_metadata_write_requires_separate_strict_readback() -> None:
    admin = EnodotAdmin()
    admin.ignored_account_fields.add("extra")
    remote, *_rest = remote_fixture(admin=admin)

    with pytest.raises(
        ProviderPoolRemoteError,
        match="provider_remote_assignment_metadata_readback_mismatch",
    ):
        remote.set_assignment_metadata(
            provider_bundle(),
            provider_bundle().slots[0],
            idempotency_key="fixture-assignment",
        )


def test_assignment_metadata_refreshes_generation_without_replacing_unrelated_extra() -> None:
    remote, _panel, _probe, admin, _pin_store, _context = remote_fixture()
    first = provider_bundle()
    second = replace(
        first,
        routing_generation=2,
        parent_routing_generation=1,
        cost_generation=2,
        parent_cost_generation=1,
    )

    assert (
        remote.set_assignment_metadata(
            first,
            first.slots[0],
            idempotency_key="fixture-assignment-1",
        )
        is True
    )
    assert (
        remote.set_assignment_metadata(
            second,
            second.slots[0],
            idempotency_key="fixture-assignment-2",
        )
        is True
    )

    updates = [call for call in admin.calls if call[0] == "update_account"]
    assert len(updates) == 2
    assert admin.accounts[21]["extra"]["unrelated"] == "preserved"
    assert admin.accounts[21]["extra"]["a6_provider_routing_generation"] == 2
    assert admin.accounts[21]["extra"]["a6_provider_cost_generation"] == 2


def test_remote_context_rejects_key_fingerprint_that_does_not_match_secret() -> None:
    _remote, panel, _probe, _admin, _pin_store, context = remote_fixture()

    with pytest.raises(
        ProviderPoolRemoteError,
        match="provider_remote_context_probe_key_fingerprint_mismatch",
    ):
        replace(context, key_fingerprint="0" * 64, panel_client=panel)


def test_repin_requires_enodot_drain_and_does_not_touch_a6_when_still_schedulable() -> None:
    remote, panel, *_rest = remote_fixture()
    remote.enodot_admin.accounts[21]["schedulable"] = True

    with pytest.raises(
        ProviderPoolRemoteError,
        match="provider_remote_repin_requires_drained_slot",
    ):
        remote.repin_slot(provider_bundle().slots[0], idempotency_key="fixture-pin")

    assert panel.calls == []


def test_remote_context_hides_probe_key_and_direct_probe_is_mandatory() -> None:
    _remote, _panel, _probe, admin, pin_store, context = remote_fixture()
    assert "sk-provider-probe-fixture" not in repr(context)
    proxied_probe = ProbeClient()
    proxied_probe.proxy_url = "http://control-plane-proxy"
    billing = EnodotGroupUserBillingFloor(
        admin,
        {MODEL: EnodotBillingFloorTarget(26, 501)},
        group_lock=lambda _group_id: nullcontext(),
    )

    with pytest.raises(
        ProviderPoolRemoteError,
        match="provider_remote_probe_client_not_direct",
    ):
        ProviderPoolRemoteAdapter(
            MODEL,
            admin,
            billing,
            pin_store,
            proxied_probe,
            (context,),
            pre_enable_guard=lambda _context: None,
        )


class Response:
    status_code = 200

    def __init__(self, data):
        self.data = data

    def json(self):
        return {"code": 0, "message": "success", "data": self.data}


class Session:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def request(self, **kwargs):
        self.calls.append(kwargs)
        return next(self.responses)


def test_enodot_account_mutations_forward_action_idempotency_keys() -> None:
    account = {"id": 21, "schedulable": False, "priority": 2}
    session = Session((Response(account), Response(account)))
    client = EnodotAdminClient(
        "https://ai.enodot.shop",
        "admin-fixture",
        session=session,
    )

    client.update_account(21, {"priority": 2}, idempotency_key="provider-priority-21")
    client.set_schedulable(21, False, idempotency_key="provider-drain-21")

    assert [call["headers"]["Idempotency-Key"] for call in session.calls] == [
        "provider-priority-21",
        "provider-drain-21",
    ]
    assert session.calls[0]["json"] == {"priority": 2}
    assert session.calls[1]["json"] == {"schedulable": False}


def test_enodot_group_rate_merge_forwards_billing_action_idempotency_key() -> None:
    before = [{"user_id": 501, "rate_multiplier": 1.0, "visible_to_user": False}]
    after = [{"user_id": 501, "rate_multiplier": 1.2, "visible_to_user": False}]
    session = Session((Response(before), Response([]), Response(after)))
    client = EnodotAdminClient(
        "https://ai.enodot.shop",
        "admin-fixture",
        session=session,
    )

    observed = client.merge_group_rate_multiplier(
        26,
        501,
        1.2,
        lock=lambda _group_id: nullcontext(),
        idempotency_key="provider-billing-generation-1",
    )

    assert observed == after
    assert session.calls[1]["method"] == "PUT"
    assert session.calls[1]["json"] == {
        "entries": [
            {
                "user_id": 501,
                "rate_multiplier": 1.2,
                "visible_to_user": False,
            }
        ]
    }
    assert (
        session.calls[1]["headers"]["Idempotency-Key"]
        == "provider-billing-generation-1"
    )


class Cursor:
    def __init__(self, database):
        self.database = database
        self.result = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, query, params):
        sql = " ".join(query.split())
        self.database.queries.append((sql, params))
        if "FROM a6_trade.a6_tokens" in sql:
            self.result = {"id": 11}
        elif "INSERT INTO a6_trade.a6_pin_bindings" in sql:
            self.result = None if self.database.pin_binding_exists else {"id": 801}
            self.database.pin_binding_exists = True
        elif (
            "SET confirmed_at = now(), change_reason = %s" in sql
            or "SELECT id FROM a6_trade.a6_pin_bindings" in sql
        ):
            self.result = {"id": 801}
        else:
            self.result = None

    def fetchone(self):
        result = self.result
        self.result = None
        return result


class Connection:
    def __init__(self, database):
        self.database = database

    def cursor(self):
        return Cursor(self.database)

    def commit(self):
        self.database.commits += 1

    def rollback(self):
        self.database.rollbacks += 1

    def close(self):
        self.database.closes += 1


class Database:
    def __init__(self, *, pin_binding_exists=False):
        self.queries = []
        self.commits = 0
        self.rollbacks = 0
        self.closes = 0
        self.pin_binding_exists = pin_binding_exists

    def connect(self):
        return Connection(self)


def test_postgres_pin_store_confirms_internal_token_identity_and_reads_exact_binding() -> None:
    database = Database(pin_binding_exists=True)
    store = PostgresProviderPinBindingStore(database.connect)

    assert store.confirm(11, MODEL, 701, change_reason="provider-generation-1") == 801
    assert store.current(11, MODEL, 701) == 801

    rendered = "\n".join(query for query, _params in database.queries)
    assert "SELECT id FROM a6_trade.a6_tokens WHERE id = %s FOR UPDATE" in rendered
    assert "INSERT INTO a6_trade.a6_pin_bindings" in rendered
    assert "SET confirmed_at = now(), change_reason = %s" in rendered
    assert "coalesce(confirmed_at, now())" not in rendered
    assert "valid_to IS NULL AND confirmed_at IS NOT NULL" in rendered
    assert database.commits == 1
    assert database.rollbacks == 0
    assert database.closes == 2
