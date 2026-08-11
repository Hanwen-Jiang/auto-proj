from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from a6api_auto.trade_channel_growth import (
    _BOOSTER_CLAIM_GUARD_QUERY,
    _BOOSTER_CLAIM_LOCK_QUERY,
    _BOOSTERS_QUERY,
    _TARGETS_QUERY,
    CallbackRequestResult,
    CallbackUsage,
    ChannelGrowthError,
    ChannelGrowthRunner,
    GrowthActionClaim,
    GrowthBooster,
    GrowthTarget,
    PostgresChannelGrowthStore,
    _seller_readiness_url,
    callback_cost_micros,
)

UTC = timezone.utc


def test_growth_targets_prioritize_focus_group_within_health_state() -> None:
    normalized = " ".join(_TARGETS_QUERY.split())

    assert "CASE WHEN merchant.operating_group = %s THEN 0 ELSE 1 END" in normalized
    assert "inventory.status AS listing_status" in normalized
    assert "coalesce(action.error_class, '') <> 'callback_http_503'" not in normalized


def test_boosters_require_fresh_pin_eligible_user_balance() -> None:
    normalized = " ".join(_BOOSTERS_QUERY.split())

    assert "JOIN a6_trade.a6_financial_current financial" in normalized
    assert "financial.user_balance_quota >= merchant.quota_per_unit" in normalized
    assert "financial.observed_at >= now() - interval '10 minutes'" in normalized


def test_boosters_exclude_reserved_and_active_provider_tokens() -> None:
    normalized = " ".join(_BOOSTERS_QUERY.split())

    assert "provider_binding.a6_token_id = token.id" in normalized
    assert "provider_binding.state = 'bound'" not in normalized
    assert "provider_slot.a6_token_id = token.id" in normalized
    assert "provider_slot.valid_to IS NULL" in normalized
    assert "provider_slot.slot_state <> 'VACANT'" in normalized
    assert "provider_generation.generation_state = 'active'" in normalized


@pytest.mark.parametrize("eligible", (False, True))
def test_growth_claim_rechecks_and_locks_booster_provider_identity(eligible: bool) -> None:
    class Cursor:
        def __init__(self, connection):
            self.connection = connection
            self.result = None

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, query, params=None):
            normalized = " ".join(str(query).split())
            values = tuple(params or ())
            self.connection.calls.append((normalized, values))
            if "FOR UPDATE OF token, booster" in normalized:
                self.result = {"id": 71}
            elif normalized.startswith("SELECT token.id"):
                self.result = {"id": 71} if self.connection.eligible else None
            elif normalized.startswith("INSERT INTO a6_trade.a6_channel_growth_actions"):
                self.result = {"id": 81}
            else:
                self.result = None

        def fetchone(self):
            return self.result

    class Connection:
        def __init__(self):
            self.eligible = eligible
            self.calls = []
            self.commits = 0
            self.rollbacks = 0
            self.closes = 0

        def cursor(self):
            return Cursor(self)

        def commit(self):
            self.commits += 1

        def rollback(self):
            self.rollbacks += 1

        def close(self):
            self.closes += 1

    connection = Connection()
    claim = PostgresChannelGrowthStore(lambda: connection).claim_action(
        target(),
        action_type="callback_readiness",
        health_state="cooling",
        idempotency_key="growth:fixture",
        booster_token_ref_id=71,
        request_count=1,
        estimated_cost_micros=100,
    )

    lock = " ".join(_BOOSTER_CLAIM_LOCK_QUERY.split())
    guard = " ".join(_BOOSTER_CLAIM_GUARD_QUERY.split())
    assert "FOR UPDATE OF token, booster" in lock
    assert "FOR UPDATE" not in guard
    assert "provider_binding.a6_token_id = token.id" in guard
    assert connection.calls[0][1] == (71,)
    assert connection.calls[1][1] == (71,)
    if eligible:
        assert claim == GrowthActionClaim(81, "callback_readiness", "cooling")
        assert connection.commits == 1
        assert connection.rollbacks == 0
    else:
        assert claim is None
        assert connection.commits == 0
        assert connection.rollbacks == 1
        assert not any(
            query.startswith("INSERT INTO a6_trade.a6_channel_growth_actions")
            for query, _params in connection.calls
        )
    assert connection.closes == 1


class StaleActionCursor:
    def __init__(self, connection) -> None:
        self.connection = connection
        self.rowcount = 2

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, query, params):
        self.connection.query = " ".join(str(query).split())
        self.connection.params = tuple(params)


class StaleActionConnection:
    def __init__(self) -> None:
        self.query = ""
        self.params = ()
        self.commits = 0
        self.rollbacks = 0
        self.closes = 0

    def cursor(self):
        return StaleActionCursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closes += 1


def test_growth_store_recovers_stale_running_actions_without_counting_a_failure() -> None:
    connection = StaleActionConnection()

    recovered = PostgresChannelGrowthStore(
        lambda: connection
    ).recover_stale_actions(max_age_seconds=600)

    assert recovered == 2
    assert "SET state = 'skipped'" in connection.query
    assert "channel_growth_stale_action_recovered" in connection.query
    assert "WHERE state = 'running'" in connection.query
    assert connection.params == (600,)
    assert connection.commits == 1
    assert connection.rollbacks == 0
    assert connection.closes == 1


def test_growth_store_rejects_invalid_stale_action_age() -> None:
    for value in (True, 59, 86_401):
        with pytest.raises(
            ChannelGrowthError,
            match="channel_growth_stale_action_age_invalid",
        ):
            PostgresChannelGrowthStore(lambda: None).recover_stale_actions(
                max_age_seconds=value
            )


def target(**changes) -> GrowthTarget:
    now = datetime.now(UTC)
    value = GrowthTarget(
        listing_binding_id=1,
        managed_uid="a6-managed:listing:m1:fixture",
        merchant_id=1,
        a6_user_id=9645,
        supplier_id=308,
        roles=("seller", "observer"),
        session_ref="session-fixture.json",
        enodot_user_id=17,
        enodot_managed_uid="a6trader:seller:merchant-1",
        enodot_key_secret_ref="enodot-seller:sha256:" + "a" * 64,
        model="gpt-5.6-sol",
        channel_id=1120,
        listing_id=4109,
        input_price_micros=100_000,
        output_price_micros=600_000,
        cache_read_price_micros=10_000,
        cache_write_price_micros=100_000,
        listing_status=2,
        available=False,
        route_status="default",
        circuit_state="cooling",
        circuit_probe_status="idle",
        circuit_last_error_kind="merchant_quota_insufficient",
        circuit_probe_success_required=2,
        circuit_probe_success_streak=0,
        circuit_scope="listing",
        circuit_recovery_mode="natural",
        circuit_cooling_until=now + timedelta(seconds=120),
        circuit_last_cooling_at=now,
        circuit_last_probe_at=None,
        circuit_next_probe_at=None,
        cooldown_state="cooling",
        cooldown_remaining_seconds=120,
        cooldown_expected_recovery_at=now + timedelta(seconds=120),
        cooldown_next_probe_at=None,
        smart_routing_status="labeled",
        smart_routing_labels=("fast",),
        last_success_at=now - timedelta(minutes=5),
        last_failure_at=now,
        last_failure_code="merchant_quota_insufficient",
        sample_count=57,
        recent_success_rate=5789,
        recent_p50_ms=Decimal("800"),
        p50_ttft_ms=Decimal("150"),
        signal_observed_at=now,
        health_observer_count=3,
        health_confirmation_count=3,
        recycle_enabled=True,
        callback_enabled=True,
        action_cooldown_seconds=1800,
        callback_cooldown_seconds=600,
        callback_requests=15,
        callback_concurrency=5,
        daily_budget_micros=250_000,
        daily_spent_micros=0,
        daily_readiness_budget_micros=120_000,
        daily_readiness_spent_micros=0,
        working_balance_low_usd=Decimal("0.25"),
        working_balance_target_usd=Decimal("1"),
        degraded_success_rate=8000,
        idle_seconds=1800,
        minimum_health_observers=2,
        maximum_consecutive_failures=2,
        consecutive_failures=0,
        last_recycle_at=None,
        recycle_next_allowed_at=None,
        last_callback_at=None,
        callback_next_allowed_at=None,
        last_readiness_at=None,
        readiness_next_allowed_at=None,
    )
    return replace(value, **changes)


def test_active_cooling_channel_waits_then_probes_before_any_recycle() -> None:
    now = datetime.now(UTC)
    subject = target()
    assert ChannelGrowthRunner._action_for(subject, "cooling", now) == (
        None,
        "awaiting_circuit_recovery_window",
    )
    due = replace(
        subject,
        circuit_cooling_until=now - timedelta(seconds=10),
        cooldown_expected_recovery_at=now - timedelta(seconds=10),
        cooldown_remaining_seconds=0,
    )
    assert ChannelGrowthRunner._action_for(due, "cooling", now) == (
        "callback_readiness",
        "",
    )
    held = replace(subject, last_recycle_at=now - timedelta(seconds=30))
    assert ChannelGrowthRunner._action_for(held, "cooling", now) == (
        None,
        "awaiting_circuit_recovery_window",
    )
    recovered = replace(
        held,
        circuit_cooling_until=now - timedelta(seconds=10),
        cooldown_expected_recovery_at=now - timedelta(seconds=10),
        signal_observed_at=now - timedelta(seconds=130),
    )
    assert ChannelGrowthRunner._action_for(recovered, "cooling", now) == (
        "callback_readiness",
        "",
    )
    probed = replace(
        recovered,
        last_readiness_at=now - timedelta(seconds=15),
        readiness_next_allowed_at=now + timedelta(seconds=45),
    )
    assert ChannelGrowthRunner._action_for(probed, "cooling", now) == (
        None,
        "readiness_next_allowed",
    )


def test_recovery_readiness_respects_action_specific_next_allowed_at() -> None:
    now = datetime.now(UTC)
    subject = target(
        last_recycle_at=now - timedelta(minutes=1),
        circuit_cooling_until=now - timedelta(minutes=1),
        cooldown_expected_recovery_at=now - timedelta(minutes=1),
        cooldown_remaining_seconds=0,
        readiness_next_allowed_at=now + timedelta(seconds=7),
    )
    assert ChannelGrowthRunner._action_for(subject, "cooling", now) == (
        None,
        "readiness_next_allowed",
    )


def test_probe_recovery_respects_readiness_next_allowed_at() -> None:
    now = datetime.now(UTC)
    subject = target(
        circuit_recovery_mode="probe",
        last_recycle_at=now - timedelta(minutes=1),
        recycle_next_allowed_at=now + timedelta(minutes=29),
        circuit_next_probe_at=now - timedelta(seconds=2),
        cooldown_next_probe_at=now - timedelta(seconds=2),
        readiness_next_allowed_at=now + timedelta(minutes=30),
    )

    assert ChannelGrowthRunner._action_for(subject, "cooling", now) == (
        None,
        "readiness_next_allowed",
    )


def test_probe_recovery_runs_readiness_when_probe_window_opens() -> None:
    now = datetime.now(UTC)
    initial = target(
        circuit_recovery_mode="probe",
        circuit_last_error_kind="upstream_5xx",
        circuit_next_probe_at=now + timedelta(seconds=20),
        cooldown_next_probe_at=now + timedelta(seconds=20),
    )
    assert ChannelGrowthRunner._action_for(initial, "cooling", now) == (
        None,
        "awaiting_circuit_probe_window",
    )
    waiting = replace(
        initial,
        last_recycle_at=now - timedelta(seconds=30),
        recycle_next_allowed_at=now + timedelta(seconds=1770),
    )
    assert ChannelGrowthRunner._action_for(waiting, "cooling", now) == (
        None,
        "awaiting_circuit_probe_window",
    )
    ready = replace(
        waiting,
        circuit_next_probe_at=now - timedelta(seconds=2),
        cooldown_next_probe_at=now - timedelta(seconds=2),
    )
    assert ChannelGrowthRunner._action_for(ready, "cooling", now) == (
        "callback_readiness",
        "",
    )
    running = replace(ready, circuit_probe_status="running")
    assert ChannelGrowthRunner._action_for(running, "cooling", now) == (
        None,
        "platform_probe_running",
    )
    completing = replace(ready, circuit_probe_success_streak=2)
    assert ChannelGrowthRunner._action_for(completing, "cooling", now) == (
        None,
        "awaiting_circuit_probe_completion",
    )


def test_failed_legacy_recycle_cooldown_does_not_block_readiness() -> None:
    now = datetime.now(UTC)
    subject = target(
        consecutive_failures=2,
        last_recycle_at=None,
        recycle_next_allowed_at=now + timedelta(seconds=30),
    )

    assert ChannelGrowthRunner._action_for(subject, "unavailable", now) == (
        "callback_readiness",
        "",
    )


def test_unlabeled_channel_selects_callback_burst() -> None:
    assert ChannelGrowthRunner._action_for(
        target(available=True, circuit_state="", cooldown_state=""),
        "unlabeled",
        datetime.now(UTC),
    ) == ("callback_burst", "")


def test_inactive_listing_uses_readiness_even_after_health_recovers() -> None:
    assert ChannelGrowthRunner._action_for(
        target(
            listing_status=0,
            available=True,
            circuit_state="",
            cooldown_state="",
        ),
        "healthy",
        datetime.now(UTC),
    ) == ("callback_readiness", "")


def test_unknown_cooling_failure_never_triggers_destructive_recycle() -> None:
    assert ChannelGrowthRunner._action_for(
        target(
            consecutive_failures=2,
            last_failure_code="unknown_error",
        ),
        "cooling",
        datetime.now(UTC),
    ) == (None, "awaiting_circuit_recovery_window")


def test_confirmed_unavailable_uses_non_destructive_readiness() -> None:
    now = datetime.now(UTC)
    subject = target(
        consecutive_failures=2,
        recycle_next_allowed_at=now - timedelta(seconds=1),
    )
    assert ChannelGrowthRunner._action_for(subject, "unavailable", now) == (
        "callback_readiness",
        "",
    )


def test_confirmed_unavailable_ignores_recycle_cooldown() -> None:
    now = datetime.now(UTC)
    subject = target(
        consecutive_failures=2,
        recycle_next_allowed_at=now + timedelta(seconds=30),
    )
    assert ChannelGrowthRunner._action_for(subject, "unavailable", now) == (
        "callback_readiness",
        "",
    )


def test_growth_action_waits_for_two_observer_health_confirmations() -> None:
    subject = target(health_confirmation_count=1)
    assert ChannelGrowthRunner._action_for(subject, "cooling", datetime.now(UTC)) == (
        None,
        "awaiting_observer_quorum",
    )


def test_growth_run_recovers_stale_actions_only_in_apply_mode() -> None:
    recovered = []
    runner = object.__new__(ChannelGrowthRunner)
    runner.store = SimpleNamespace(
        recover_stale_actions=lambda *, max_age_seconds: recovered.append(
            max_age_seconds
        )
        or 2,
        targets=lambda _model, *, signal_max_age_seconds: (),
    )

    preview = runner.run(model="gpt-5.6-sol", apply=False)
    applied = runner.run(model="gpt-5.6-sol", apply=True)

    assert recovered == [600]
    assert preview["held"] == {}
    assert applied["held"] == {"stale_action_recovered": 2}


def test_budget_blocked_head_does_not_starve_later_recovery_target() -> None:
    first = target(merchant_id=9170)
    second = target(merchant_id=9469)
    runner = object.__new__(ChannelGrowthRunner)
    runner.store = SimpleNamespace(
        targets=lambda _model, *, signal_max_age_seconds: (first, second)
    )
    runner._action_for = lambda _target, _health, _now: ("callback_readiness", "")
    visited = []

    def boost(targets, subject, health_state, *, held, action_type):
        visited.append(subject.merchant_id)
        if subject.merchant_id == first.merchant_id:
            return {
                "success": True,
                "mode": "held",
                "model": subject.model,
                "target_count": len(targets),
                "held": {
                    **held,
                    "callback_budget_or_booster_unavailable": 1,
                },
                "maximum_actions_per_run": 1,
            }
        return {
            "success": True,
            "mode": "completed",
            "model": subject.model,
            "target_count": len(targets),
            "merchant_id": subject.merchant_id,
            "health_state": health_state,
            "action_type": action_type,
            "held": held,
            "maximum_actions_per_run": 1,
        }

    runner._boost = boost

    result = runner.run(model="gpt-5.6-sol", apply=True)

    assert visited == [9170, 9469]
    assert result["mode"] == "completed"
    assert result["merchant_id"] == 9469
    assert result["held"] == {"callback_budget_or_booster_unavailable": 1}


def test_callback_cost_uses_cache_specific_listing_prices() -> None:
    usage = CallbackUsage(
        input_tokens=10_000,
        output_tokens=100,
        cached_tokens=8_000,
        cache_creation_tokens=1_000,
    )
    # 1k uncached input + 8k cache read + 1k cache write + 100 output.
    assert callback_cost_micros(usage, target()) == 340


def test_manual_session_without_upstream_uses_managed_seller_readiness_url() -> None:
    assert (
        _seller_readiness_url(SimpleNamespace(upstream_base_url=""))
        == "https://ai.enodot.shop/v1"
    )
    assert (
        _seller_readiness_url(SimpleNamespace(upstream_base_url="https://seller.example/v1"))
        == "https://seller.example/v1"
    )


class GrowthStore:
    def __init__(self) -> None:
        self.failed = None
        self.completed = None
        self.claimed = None
        self.booster = GrowthBooster(
            token_ref_id=5,
            merchant_id=5,
            a6_user_id=5005,
            token_id=14530,
            token_name="callback",
            session_ref="session-booster.json",
            daily_budget_micros=250_000,
            daily_spent_micros=0,
            daily_readiness_budget_micros=2_000_000,
            daily_readiness_spent_micros=0,
            maximum_burst_requests=15,
        )

    def boosters(self, _target_merchant_id):
        return (self.booster,)

    def claim_action(self, *_args, **kwargs):
        self.claimed = kwargs
        return GrowthActionClaim(2, kwargs["action_type"], "cooling")

    def fail_action(self, claim, **kwargs):
        self.failed = claim, kwargs

    def complete_action(self, claim, **kwargs):
        self.completed = claim, kwargs


def test_failed_recycle_retries_once_with_a_distinct_key_after_30_seconds() -> None:
    subject = target(consecutive_failures=1)
    store = GrowthStore()
    runner = object.__new__(ChannelGrowthRunner)
    runner.store = store
    runner.random_uniform = lambda _minimum, _maximum: 1.25
    runner.sleep = lambda _seconds: None
    runner._ensure_working_balance = lambda *_args: (Decimal("1"), Decimal("1"))
    runner._listing_operation = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        RuntimeError("a6_transport_failed")
    )
    runner._best_effort_reactivate = lambda _target: True

    result = runner._recycle(
        (subject,),
        subject,
        "cooling",
        min_delay=0.8,
        max_delay=2.0,
        held={},
    )

    assert result["mode"] == "failed"
    assert result["error_class"] == "a6_transport_failed"
    assert store.claimed is not None
    assert store.claimed["idempotency_key"].endswith(":retry:1")
    assert store.failed is not None
    _claim, failure = store.failed
    assert failure["next_allowed_seconds"] == 30
    assert failure["details"]["retry_delay_seconds"] == 30
    assert failure["details"]["final_listing_active"] is True


class BalanceAdmin:
    def __init__(self, balance="1") -> None:
        self.balance = Decimal(balance)
        self.credits = []

    def get_user(self, _user_id):
        return {"balance": str(self.balance)}

    def add_user_balance(self, user_id, amount, **kwargs):
        self.credits.append((user_id, amount, kwargs))
        self.balance += amount
        return {"balance": str(self.balance)}


class BoosterClient:
    def __init__(self) -> None:
        self.pin = None

    def ensure_one_key(self, *_args):
        return "booster-key"

    def marketplace_pins(self, *_args):
        if self.pin is None:
            return []
        return [
            {
                "token_id": 14530,
                "model_name": "gpt-5.6-sol",
                "channel_id": self.pin,
                "status": "active",
            }
        ]

    def marketplace_pin(self, _user_id, *, channel_id, **_kwargs):
        self.pin = channel_id
        return {"success": True}

    def marketplace_unpin(self, *_args, **_kwargs):
        self.pin = None
        return {"success": True}


class Readiness503Transport:
    def __init__(self) -> None:
        self.calls = 0
        self.batch_calls = 0

    def request(self, **_kwargs):
        self.calls += 1
        if self.calls == 1:
            return CallbackRequestResult(
                True,
                200,
                5,
                usage=CallbackUsage(100, 1, 90, 0),
            )
        return CallbackRequestResult(
            False,
            503,
            7,
            error_class="callback_http_503",
        )

    def batch(self, **_kwargs):
        self.batch_calls += 1
        raise AssertionError("batch must wait for A6 readiness")


class SuccessfulReadinessTransport(Readiness503Transport):
    def request(self, **_kwargs):
        self.calls += 1
        return CallbackRequestResult(
            True,
            200,
            5,
            usage=CallbackUsage(100, 1, 90, 0),
        )


def attach_listing_state(runner, subject, *, initial_status: int = 0):
    state = {"status": initial_status}

    def operation(_merchant, _managed_uid, *, operation):
        state["status"] = 0 if operation == "takedown" else 2
        return {"state": "verified", "operation": operation}

    def live(_target):
        return {
            "id": subject.listing_id,
            "status": state["status"],
            "input_price_micros": subject.input_price_micros,
            "output_price_micros": subject.output_price_micros,
            "cache_read_price_micros": subject.cache_read_price_micros,
            "cache_write_price_micros": subject.cache_write_price_micros,
        }

    runner._listing_operation = operation
    runner._live_listing = live
    runner._listing_status = lambda _target: state["status"]
    return state


def test_recycle_preserves_zero_status_and_completes_isolation() -> None:
    subject = target()
    store = GrowthStore()
    runner = object.__new__(ChannelGrowthRunner)
    runner.store = store
    listing = attach_listing_state(runner, subject, initial_status=2)

    result = runner._recycle(
        (subject,),
        subject,
        "cooling",
        min_delay=0.8,
        max_delay=2.0,
        held={},
    )

    assert result["success"] is True
    assert result["mode"] == "completed"
    assert listing["status"] == 0
    assert store.failed is None
    assert store.completed is not None
    _claim, completion = store.completed
    assert completion["details"]["status_before"] == 2
    assert completion["details"]["final_status"] == 0
    assert completion["details"]["price_preserved"] is True


def test_a6_readiness_503_retries_within_one_minute_without_sending_burst() -> None:
    now = datetime.now(UTC)
    subject = target(
        circuit_cooling_until=now - timedelta(minutes=1),
        cooldown_expected_recovery_at=now - timedelta(minutes=1),
        cooldown_remaining_seconds=0,
    )
    store = GrowthStore()
    transport = Readiness503Transport()
    runner = object.__new__(ChannelGrowthRunner)
    runner.store = store
    runner.usage_samples = (CallbackUsage(100, 1, 90, 0),)
    runner.seller_secret_sink = SimpleNamespace(read_api_key=lambda *_args: "seller-key")
    runner.callback_transport = transport
    runner.callback_gateway_url = "https://a6.a6api.com"
    runner.proxy_by_user_id = {5005: "http://proxy.invalid"}
    runner.enodot_admin = BalanceAdmin()
    runner.sleep = lambda _seconds: None
    runner._record = lambda _user_id: SimpleNamespace(  # type: ignore[method-assign]
        upstream_base_url="https://seller.invalid"
    )
    booster_client = BoosterClient()
    runner.a6_client = lambda _user_id: booster_client  # type: ignore[method-assign]
    listing = attach_listing_state(runner, subject)

    result = runner._boost(
        (subject,),
        subject,
        "cooling",
        held={},
        action_type="callback_readiness",
    )

    assert result["success"] is False
    assert result["error_class"] == "callback_http_503"
    assert transport.batch_calls == 0
    assert booster_client.pin is None
    assert store.failed is not None
    _claim, failure = store.failed
    assert failure["error_class"] == "callback_http_503"
    assert failure["success_count"] == 0
    assert failure["failure_count"] == 1
    assert failure["next_allowed_seconds"] == 60
    assert failure["details"]["remaining_requests"] == 1
    assert failure["details"]["pin_restored"] is True
    assert failure["details"]["callback_concurrency"] == 2
    assert failure["details"]["callback_batch_concurrency"] == 1
    assert failure["details"]["listing_reactivated_for_probe"] is True
    assert failure["details"]["listing_reisolated"] is True
    assert listing["status"] == 0


def test_probe_recovery_sends_required_canaries_serially_without_batch() -> None:
    now = datetime.now(UTC)
    subject = target(
        circuit_recovery_mode="probe",
        circuit_next_probe_at=now - timedelta(minutes=1),
        cooldown_next_probe_at=now - timedelta(minutes=1),
        daily_spent_micros=250_000,
    )
    store = GrowthStore()
    store.booster = replace(store.booster, daily_spent_micros=250_000)
    transport = SuccessfulReadinessTransport()
    sleeps = []
    runner = object.__new__(ChannelGrowthRunner)
    runner.store = store
    runner.usage_samples = (CallbackUsage(100, 1, 90, 0),)
    runner.seller_secret_sink = SimpleNamespace(read_api_key=lambda *_args: "seller-key")
    runner.callback_transport = transport
    runner.callback_gateway_url = "https://a6.a6api.com"
    runner.proxy_by_user_id = {5005: "http://proxy.invalid"}
    admin = BalanceAdmin("-0.948743")
    runner.enodot_admin = admin
    runner.sleep = sleeps.append
    runner._record = lambda _user_id: SimpleNamespace(  # type: ignore[method-assign]
        upstream_base_url="https://seller.invalid"
    )
    booster_client = BoosterClient()
    runner.a6_client = lambda _user_id: booster_client  # type: ignore[method-assign]
    listing = attach_listing_state(runner, subject)

    result = runner._boost(
        (subject,),
        subject,
        "cooling",
        held={},
        action_type="callback_readiness",
    )

    assert result["success"] is True
    assert result["action_type"] == "callback_readiness"
    assert transport.calls == 3  # Public readiness plus two A6 circuit probes.
    assert transport.batch_calls == 0
    assert sleeps == [1.0]
    assert booster_client.pin is None
    assert store.completed is not None
    _claim, completion = store.completed
    assert completion["request_count"] == 2
    assert completion["success_count"] == 2
    assert completion["failure_count"] == 0
    assert completion["next_allowed_seconds"] == 10
    assert completion["balance_before_usd"] == Decimal("-0.94874300")
    assert completion["balance_after_usd"] == Decimal("1.00000000")
    assert completion["details"]["balance_credit_applied"] is True
    assert completion["details"]["balance_credit_usd"] == "1.94874300"
    assert completion["details"]["listing_reactivated_for_probe"] is True
    assert completion["details"]["listing_reisolated"] is False
    assert completion["details"]["budget_scope"] == "readiness"
    assert listing["status"] == 2
    assert len(admin.credits) == 1
    assert admin.credits[0][2]["idempotency_key"] == "channel-growth-balance:2"


def test_growth_targets_survive_retirement_and_expiring_suppression() -> None:
    """Growth must not decay to zero on its own.

    Three separate one-way gates removed listings from health management with no path
    back: `policy.enabled=false` written by retirement, `retirement.completed_at IS
    NULL` in the target query, and permanently disabled callbacks. Suppression is now
    time-bounded and retirement no longer excludes a merchant.
    """

    source = Path("src/a6api_auto/trade_channel_growth.py").read_text(encoding="utf-8")

    assert "AND retirement.completed_at IS NULL" not in source
    assert "a6_trade.a6_seller_retirement_plans" not in source
    assert "policy.suppressed_until IS NULL OR policy.suppressed_until <= now()" in source
