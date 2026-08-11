from __future__ import annotations

import json
import random
import threading
import time
from contextlib import contextmanager
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from a6api_auto.errors import A6AutoError
from a6api_auto.trade_pin_control import (
    ObserverQuote,
    PinAssignment,
    PinCandidate,
    PinPlan,
    ProviderPinTarget,
)
from a6api_auto.trade_pin_runner import (
    ObserverContext,
    ObserverScanResult,
    PersistedObserverSnapshot,
    PostgresPinAuditStore,
    ProviderPinContext,
    ProviderPinRunner,
    _balance_transfer_confirmed,
    _DeadlineBoundEnodotAdmin,
    _emergency_boost_workload,
    _emergency_pinned_channel_ids,
    _external_pin_failure_streak,
    _hash,
    _observed_current_operating_cost,
    _observer_backoff_seconds,
    _pin_failure_channel,
    _pin_failure_targets,
    _pin_remote_timeout,
    _pin_round_checkpoint,
    _pin_round_deadline,
    _pin_scan_worker_count,
    _provider_balance_ready,
    _provider_operating_channel_ids,
    _provider_planning_contexts,
    _provider_reconciliation_order,
    _reserved_candidate_channel_ids,
    _resolve_pin_failures,
    _scan_error_code,
    _SchedulablePoolGuard,
    _select_observer_round,
    _stabilize_provider_plan_assignments,
    _validated_probe_timing,
)


def test_pin_round_deadline_is_explicit_and_bounded(monkeypatch) -> None:
    now = [100.0]
    monkeypatch.setattr("a6api_auto.trade_pin_runner.time.monotonic", lambda: now[0])

    deadline = _pin_round_deadline(90)

    assert deadline == 190
    _pin_round_checkpoint(deadline)
    now[0] = 190
    with pytest.raises(A6AutoError, match="provider_pin_round_deadline_exceeded"):
        _pin_round_checkpoint(deadline)


@pytest.mark.parametrize("value", (True, 14.99, 301, float("inf"), float("nan")))
def test_pin_round_deadline_rejects_unbounded_budget(value) -> None:
    with pytest.raises(A6AutoError, match="provider_pin_round_time_budget_invalid"):
        _pin_round_deadline(value)


def test_pin_remote_timeout_shrinks_to_round_remaining(monkeypatch) -> None:
    now = [100.0]
    monkeypatch.setattr("a6api_auto.trade_pin_runner.time.monotonic", lambda: now[0])

    assert _pin_remote_timeout(104, 8) == 4
    now[0] = 103.75
    assert _pin_remote_timeout(104, 8) == pytest.approx(0.25)
    now[0] = 104
    with pytest.raises(A6AutoError, match="provider_pin_round_deadline_exceeded"):
        _pin_remote_timeout(104, 8)


def test_observer_only_scan_workers_do_not_queue_behind_write_side_limit() -> None:
    assert (
        _pin_scan_worker_count(
            provider_count=12,
            observer_count=14,
            observe_only=True,
        )
        == 14
    )
    assert (
        _pin_scan_worker_count(
            provider_count=12,
            observer_count=40,
            observe_only=True,
        )
        == 32
    )
    assert (
        _pin_scan_worker_count(
            provider_count=12,
            observer_count=14,
            observe_only=False,
        )
        == 8
    )


def test_probe_timing_accepts_slow_first_event_contract() -> None:
    assert _validated_probe_timing(12, 14) == (12.0, 14.0)


@pytest.mark.parametrize("timeout", (True, 0.99, 20.01, float("inf"), float("nan"), "12"))
def test_probe_timing_rejects_invalid_timeout(timeout) -> None:
    with pytest.raises(A6AutoError, match="provider_probe_timeout_invalid"):
        _validated_probe_timing(timeout, 14)


@pytest.mark.parametrize("budget", (True, 11.99, 30.01, float("inf"), float("nan"), "14"))
def test_probe_timing_rejects_invalid_round_budget(budget) -> None:
    with pytest.raises(A6AutoError, match="provider_probe_round_time_budget_invalid"):
        _validated_probe_timing(12, budget)


def test_pin_systemd_is_observer_driven_without_activation_probes() -> None:
    service = Path("deploy/systemd/a6-trader-pin.service").read_text()
    timer = Path("deploy/systemd/a6-trader-pin.timer").read_text()
    path = Path("deploy/systemd/a6-trader-pin.path").read_text()

    assert "--round-time-budget-seconds 120" in service
    assert "--enodot-admin-timeout-seconds 8" in service
    assert "--pin-probe-mode disabled" in service
    assert "--probe-budget 0" in service
    assert "--observer-snapshot-max-age-seconds 0" in service
    assert "--probe-failure-ttl-seconds 60" in service
    assert "--pin-failure-max-age-seconds 300" in service
    assert "--probe-timeout-seconds" not in service
    assert "--probe-round-time-budget-seconds" not in service
    assert "--pin-max-latency-ms 12000" in service
    assert "--minimum-schedulable-capacity 300" in service
    assert "--pin-min-switch-gain 0.05" in service
    assert "--pin-channel-failure-threshold 3" in service
    assert "phase1-provider-pin-v33-latest-commit-single-writer" in service
    assert "/venv/bin/python -m a6api_auto.cli trade-pin-providers-once" in service
    assert "--provider-initial-balance-ratio 0" in service
    assert "--apply" not in service
    assert "/usr/bin/flock" not in service
    assert "/var/lib/a6-trader/provider-control.lock" not in service
    assert "/var/lib/a6-trader/seller-control.lock" not in service
    assert "SuccessExitStatus=" not in service
    assert (
        "OnSuccess=a6-trader-provider-pool-apply.service "
        "a6-trader-seller-pilot.service"
    ) in service
    after = next(line for line in service.splitlines() if line.startswith("After="))
    assert "a6-trader-observer.service" not in after
    assert "TimeoutStartSec=150s" in service
    assert "OnUnitInactiveSec=15s" in timer
    assert "OnUnitActiveSec=" not in timer
    assert "PathModified=/run/sub2api-a6-trader/pin-request.failures" in path
    assert "PathChanged=/run/sub2api-a6-trader/pin-request" not in path


def test_provider_and_seller_control_planes_use_independent_locks() -> None:
    provider_writer_units = (
        "a6-trader-provider-pool-apply.service",
        "a6-trader-provider-failure-repair.service",
        "a6-trader-provider-liquidity.service",
    )
    for name in provider_writer_units:
        service = Path("deploy/systemd", name).read_text()
        assert "/var/lib/a6-trader/provider-control.lock" in service
        assert "/var/lib/a6-trader/seller-control.lock" not in service

    pin = Path("deploy/systemd/a6-trader-pin.service").read_text()
    assert "/var/lib/a6-trader/provider-control.lock" not in pin
    assert "/var/lib/a6-trader/seller-control.lock" not in pin

    seller = Path("deploy/systemd/a6-trader-seller-pilot.service").read_text()
    assert "/var/lib/a6-trader/seller-control.lock" in seller
    assert "/var/lib/a6-trader/provider-control.lock" not in seller
    assert "--max-workers 16" in seller
    assert "--remote-request-timeout-seconds 5" in seller
    assert "--round-time-budget-seconds 90" in seller
    assert "--listing-activation-stagger-seconds" not in seller
    assert "--nonblock" in seller
    assert "SuccessExitStatus=75" in seller
    assert "TimeoutStartSec=120s" in seller


def test_observer_systemd_runs_complete_pool_for_event_driven_auto_pool() -> None:
    service = Path("deploy/systemd/a6-trader-observer.service").read_text()
    timer = Path("deploy/systemd/a6-trader-observer.timer").read_text()
    target = Path("deploy/systemd/a6-trader-control.target").read_text()
    auto_pool = Path("deploy/systemd/a6-trader-provider-auto-pool.service").read_text()
    refresh_path = Path(
        "deploy/systemd/a6-trader-provider-auto-pool-refresh.path"
    ).read_text()

    assert "--all-observers" in service
    assert "--observer-round-size" not in service
    assert "--observer-window-seconds 4" in service
    assert "--client-timeout-seconds 3" in service
    assert "--round-time-budget-seconds 15" in service
    assert "OnSuccess=a6-trader-provider-auto-pool.service" in service
    assert "OnCalendar=*-*-* *:*:00" in timer
    assert "OnCalendar=*-*-* *:*:00,30" not in timer
    assert "Wants=a6-trader-observer.service" in target
    assert "Wants=a6-trader-observer.timer" not in target
    assert "Wants=a6-trader-pin.timer" not in target
    assert "Wants=a6-trader-pin.path" not in target
    assert "Wants=a6-trader-provider-pool-apply.timer" not in target
    assert "Wants=a6-trader-provider-auto-pool-refresh.path" in target
    assert "PathChanged=/run/a6-trader/provider-auto-pool-request" in refresh_path
    assert "Unit=a6-trader-observer.service" in refresh_path
    assert "PROVIDER_AUTO_POOL_ENABLED" in auto_pool
    assert "reconcile_a6_provider_auto_pool.py" in auto_pool
    assert "--hard-max-ratio-cny 0.3" in auto_pool
    assert "--minimum-ready-accounts 4" in auto_pool
    assert "/var/lib/a6-trader/provider-control.lock" in auto_pool
    assert "OnSuccess=a6-trader-seller-pilot.service" in auto_pool
    assert "a6-trader-provider-pool-shadow" not in auto_pool


def test_pin_failure_targets_map_enodot_account_to_confirmed_channel() -> None:
    contexts = (
        ProviderPinContext(1, 9001, 13001, 21, 45896, "a", 500_000, 500_000, 0, 1185),
        ProviderPinContext(2, 9002, 13002, 22, 45909, "b", 500_000, 500_000, 0, None),
    )

    targets = _pin_failure_targets(
        contexts,
        {45896: 101, 45909: 102, 49999: 103},
    )

    assert targets.matched_account_ids == (45896, 45909)
    assert targets.unmatched_account_ids == (49999,)


def test_pin_failure_targets_preserve_repeated_event_order() -> None:
    context = ProviderPinContext(
        1, 9001, 13001, 21, 45917, "a", 500_000, 500_000, 0, 1550
    )

    targets = _pin_failure_targets(
        (context,),
        ((45917, 101), (45917, 102), (45917, 103)),
    )

    assert [(item.enodot_account_id, event_ns) for item, event_ns in targets.matched] == [
        (45917, 101),
        (45917, 102),
        (45917, 103),
    ]


def test_external_pin_failure_streak_requires_three_consecutive_new_events() -> None:
    first_two = _external_pin_failure_streak(
        (102, 101), latest_success_ns=None, threshold=3, inserted=True
    )
    third = _external_pin_failure_streak(
        (103, 102, 101), latest_success_ns=None, threshold=3, inserted=True
    )
    duplicate_third = _external_pin_failure_streak(
        (103, 102, 101), latest_success_ns=None, threshold=3, inserted=False
    )

    assert first_two.count == 2
    assert first_two.triggered is False
    assert third.count == 3
    assert third.triggered is True
    assert duplicate_third.count == 3
    assert duplicate_third.triggered is False
    assert duplicate_third.duplicate is True


def test_external_pin_failure_streak_resets_after_success_between_failures() -> None:
    streak = _external_pin_failure_streak(
        (300, 200, 100),
        latest_success_ns=150,
        threshold=3,
        inserted=True,
    )

    assert streak.count == 2
    assert streak.triggered is False
    assert streak.success_reset is True


def test_external_failure_is_attributed_to_pin_active_at_event_time() -> None:
    subject = replace(probe_context(), confirmed_pin_channel_id=815)
    seen = []
    store = SimpleNamespace(
        pin_channel_at=lambda token_ref_id, model, event_ns: seen.append(
            (token_ref_id, model, event_ns)
        )
        or 622
    )

    channel_id = _pin_failure_channel(
        store,
        subject,
        model="gpt-5.6-sol",
        event_at_unix_nano=123456789,
    )

    assert channel_id == 622
    assert seen == [(subject.token_ref_id, "gpt-5.6-sol", 123456789)]


def test_failure_event_output_channels_use_event_time_pin_bindings() -> None:
    subject = replace(probe_context(), confirmed_pin_channel_id=815)
    targets = _pin_failure_targets(
        (subject,),
        ((subject.enodot_account_id, 123456789),),
    )
    store = SimpleNamespace(pin_channel_at=lambda *_args: 622)

    resolved = _resolve_pin_failures(store, targets, model="gpt-5.6-sol")

    assert [(item.context.enodot_account_id, item.channel_id) for item in resolved] == [
        (subject.enodot_account_id, 622)
    ]


def test_external_pin_failure_is_persisted_as_idempotent_streak_event() -> None:
    class ExternalFailureCursor(RecordingCursor):
        def execute(self, query, params=None):
            self.connection.calls.append((query, params))
            normalized = " ".join(query.split())
            if normalized.startswith("INSERT INTO a6_trade.trade_control_actions"):
                self.result = {"id": 91}
            elif "FROM public.usage_logs" in normalized:
                self.result = {"latest_success_at": None, "latest_success_ns": None}
            elif "AS event_ns" in normalized:
                self.result = [
                    {"event_ns": 123456789},
                    {"event_ns": 123456788},
                    {"event_ns": 123456787},
                ]
            elif normalized.startswith("UPDATE a6_trade.trade_control_actions"):
                self.result = {"id": 91}
            else:
                self.result = None
            return self

        def fetchone(self):
            return self.result[0] if isinstance(self.result, list) else self.result

        def fetchall(self):
            if isinstance(self.result, list):
                return self.result
            return [] if self.result is None else [self.result]

    class ExternalFailureConnection(RecordingConnection):
        def cursor(self):
            return ExternalFailureCursor(self)

    connection = ExternalFailureConnection()
    store = PostgresPinAuditStore(lambda: connection)
    context = ProviderPinContext(
        1,
        9001,
        13001,
        21,
        45896,
        "a",
        500_000,
        500_000,
        0,
        1185,
    )

    result = store.record_external_pin_failure(
        context,
        model="gpt-5.6-sol",
        channel_id=1185,
        event_at_unix_nano=123456789,
        failure_ttl_seconds=45,
        failure_threshold=3,
        config_version="fixture-v1",
    )

    query, params = connection.calls[0]
    assert "ON CONFLICT (idempotency_key) DO NOTHING" in query
    assert "provider_pin_channel_failure" in query
    assert params[0] == (
        "provider-channel-failure:45896:21:gpt-5.6-sol:1185:123456789"
    )
    assert params[1] == "21"
    assert json.loads(params[2])["request_kind"] == "sub2api_traffic_failure"
    assert json.loads(params[3])["source"] == "sub2api_failure_journal"
    event_query, event_params = next(
        (query, params) for query, params in connection.calls if "AS event_ns" in query
    )
    assert "enodot_account_id" in event_query
    assert event_params[:4] == ("21", "45896", "gpt-5.6-sol", "1185")
    assert result.count == 3
    assert result.threshold == 3
    assert result.triggered is True
    assert connection.commits == 1
    assert connection.rollbacks == 0
    assert connection.closed is True


def test_emergency_pin_detection_uses_any_observer_red_or_yellow_signal() -> None:
    quotes = (
        ObserverQuote(
            observer_merchant_id=1,
            channel_id=1170,
            listing_id=4170,
            model="gpt-5.6-sol",
            final_multiplier=Decimal("0.04385"),
            input_price_micros=43_850,
            output_price_micros=263_100,
            health_state="healthy",
        ),
        ObserverQuote(
            observer_merchant_id=2,
            channel_id=1170,
            listing_id=4170,
            model="gpt-5.6-sol",
            final_multiplier=Decimal("0.04385"),
            input_price_micros=43_850,
            output_price_micros=263_100,
            health_state="degraded",
            recent_success_rate=7200,
        ),
        ObserverQuote(
            observer_merchant_id=3,
            channel_id=1305,
            listing_id=4305,
            model="gpt-5.6-sol",
            final_multiplier=Decimal("0.04404"),
            input_price_micros=44_040,
            output_price_micros=264_240,
            health_state="degraded",
        ),
    )

    assert _emergency_pinned_channel_ids(quotes, {1170}) == frozenset({1170})
    assert _emergency_pinned_channel_ids(quotes, {9999}) == frozenset()


def test_current_operating_cost_uses_consensus_max_across_pinned_channels() -> None:
    quotes = tuple(
        quote
        for observer, channel_id, multiplier in (
            (1, 1170, "0.011"),
            (2, 1170, "0.013"),
            (1, 1305, "0.019"),
            (2, 1305, "0.021"),
            (1, 1400, "0.90"),
        )
        for quote in (
            ObserverQuote(
                observer_merchant_id=observer,
                channel_id=channel_id,
                listing_id=channel_id + 3000,
                model="gpt-5.6-sol",
                final_multiplier=Decimal(multiplier),
                input_price_micros=1,
                output_price_micros=1,
            ),
        )
    )

    assert _observed_current_operating_cost(
        quotes,
        {1170, 1305, 1400},
        min_observers=2,
    ) == Decimal("0.020")


def test_zero_balance_pin_stays_in_cost_set_until_real_insufficient_fact() -> None:
    funded = replace(
        probe_context(),
        merchant_id=1,
        confirmed_pin_channel_id=1170,
        user_balance_quota=500_000,
        quota_per_unit=500_000,
    )
    drained = replace(
        probe_context(),
        merchant_id=2,
        confirmed_pin_channel_id=430,
        user_balance_quota=0,
        withdrawable_quota=0,
        quota_per_unit=500_000,
    )

    assert _provider_operating_channel_ids((funded, drained)) == frozenset({430, 1170})


def test_current_operating_cost_channel_set_keeps_positive_balance_confirmed_pin() -> None:
    provider = replace(
        probe_context(),
        merchant_id=2,
        confirmed_pin_channel_id=430,
        user_balance_quota=1,
        withdrawable_quota=0,
        quota_per_unit=500_000,
    )

    assert _provider_operating_channel_ids((provider,)) == frozenset({430})


def test_emergency_boost_workload_always_contains_short_medium_and_long() -> None:
    workloads = _emergency_boost_workload(5, rng=random.Random(7))

    assert len(workloads) == 5
    assert {item.profile for item in workloads} == {"short", "medium", "long"}
    assert all(16 <= item.max_output_tokens <= 512 for item in workloads)
    assert min(len(item.input_text) for item in workloads) >= 80
    assert max(len(item.input_text) for item in workloads) >= 1_000


class MarketplaceClient:
    def __init__(self, items):
        self.items = items

    def marketplace_channels(self, *_args, **_kwargs):
        return {"items": self.items, "total": len(self.items)}


def test_observer_scan_excludes_nested_cooldown_and_uses_live_ratio() -> None:
    client = MarketplaceClient(
        [
            {
                "channel_id": 1009,
                "listing_id": 3343,
                "supplier_id": 275,
                "model_name": "gpt-5.6-sol",
                "input_price_micros": 14_400,
                "output_price_micros": 86_400,
                "realtime_ratio_usd": 0.00288,
                "route_status": "default",
                "sample_count": 14,
                "recent_success_rate": 0,
                "circuit_status": {
                    "state": "cooling",
                    "scope": "listing",
                    "recovery_mode": "natural",
                    "cooling_until": 1_784_966_529,
                    "last_cooling_at": 1_784_964_817,
                    "last_probe_at": 1_784_964_900,
                    "next_probe_at": 1_784_965_000,
                    "probe_status": "idle",
                    "probe_success_required": 2,
                    "probe_success_streak": 0,
                    "last_error_kind": "merchant_quota_insufficient",
                },
                "cooldown_status": {
                    "state": "cooling",
                    "remaining_seconds": 1712,
                    "expected_recovery_at": 1_784_966_529,
                    "next_probe_at": 1_784_965_000,
                    "sources": ["circuit"],
                },
            },
            {
                "channel_id": 627,
                "listing_id": 1735,
                "supplier_id": 103,
                "model_name": "gpt-5.6-sol",
                "input_price_micros": 57_600,
                "output_price_micros": 345_600,
                "realtime_ratio_usd": 0.01152,
                "realtime_ratio_cny": 0.078014,
                "exchange_rate": 6.77205,
                "route_status": "default",
                "sample_count": 100,
                "recent_success_rate": 4500,
                "last_success_at": 1_784_964_817,
                "last_failure_at": 1_784_964_700,
                "smart_routing_status": "labeled",
                "smart_routing_labels": ["cheap"],
                "last_sample_at": 1_784_964_817,
                "recent_p50_ms": 812.5,
                "p50_ttft_ms": 155.25,
            },
        ]
    )
    runner = object.__new__(ProviderPinRunner)
    runner._observer_client = lambda _user_id: client  # type: ignore[method-assign]

    quotes, pages = runner._scan_observer(
        ObserverContext(merchant_id=1, a6_user_id=9645, token_id=13868, session_ref="x"),
        model="gpt-5.6-sol",
        page_size=100,
    )

    by_channel = {quote.channel_id: quote for quote in quotes}
    assert pages == 1
    assert by_channel[1009].available is False
    assert by_channel[1009].circuit_state == "cooling"
    assert by_channel[1009].circuit_scope == "listing"
    assert by_channel[1009].circuit_recovery_mode == "natural"
    assert by_channel[1009].circuit_cooling_until == 1_784_966_529
    assert by_channel[1009].circuit_last_cooling_at == 1_784_964_817
    assert by_channel[1009].circuit_last_probe_at == 1_784_964_900
    assert by_channel[1009].circuit_next_probe_at == 1_784_965_000
    assert by_channel[1009].circuit_probe_success_required == 2
    assert by_channel[1009].circuit_probe_success_streak == 0
    assert by_channel[1009].cooldown_expected_recovery_at == 1_784_966_529
    assert by_channel[1009].cooldown_next_probe_at == 1_784_965_000
    assert by_channel[1009].cooldown_sources == ["circuit"]
    assert by_channel[1009].health_state == "cooling"
    assert by_channel[1009].final_multiplier == Decimal("0.00288")
    assert by_channel[627].available is True
    assert by_channel[627].final_multiplier == Decimal("0.01152")
    assert by_channel[627].realtime_ratio_usd == Decimal("0.01152")
    assert by_channel[627].realtime_ratio_cny == Decimal("0.078014")
    assert by_channel[627].exchange_rate == Decimal("6.77205")
    assert by_channel[627].last_success_at == 1_784_964_817
    assert by_channel[627].health_state == "idle"
    assert by_channel[627].smart_routing_labels == ("cheap",)
    assert by_channel[627].recent_p50_ms == Decimal("812.5")
    assert by_channel[627].p50_ttft_ms == Decimal("155.25")


def test_observer_scan_prefers_explicit_final_multiplier() -> None:
    client = MarketplaceClient(
        [
            {
                "channel_id": 444,
                "listing_id": 1406,
                "model_name": "gpt-5.6-sol",
                "input_price_micros": 44_118,
                "output_price_micros": 264_708,
                "final_multiplier": "0.009",
                "realtime_ratio_usd": "0.0088236",
                "route_status": "default",
            }
        ]
    )
    runner = object.__new__(ProviderPinRunner)
    runner._observer_client = lambda _user_id: client  # type: ignore[method-assign]

    quotes, _ = runner._scan_observer(
        ObserverContext(merchant_id=1, a6_user_id=9645, token_id=13868, session_ref="x"),
        model="gpt-5.6-sol",
        page_size=100,
    )

    assert quotes[0].final_multiplier == Decimal("0.009")
    assert quotes[0].realtime_ratio_usd == Decimal("0.0088236")


def test_balance_transfer_is_confirmed_when_concurrent_spend_hides_balance_gain() -> None:
    assert _balance_transfer_confirmed(
        user_balance_before=342_394,
        withdrawable_before=1_759_019,
        user_balance_after=658_768,
        withdrawable_after=1_259_019,
        amount=500_000,
    )


def test_balance_transfer_requires_a_full_credit_or_withdrawable_debit() -> None:
    assert not _balance_transfer_confirmed(
        user_balance_before=342_394,
        withdrawable_before=1_759_019,
        user_balance_after=658_768,
        withdrawable_after=1_759_019,
        amount=500_000,
    )


def test_any_positive_balance_provider_reserves_its_candidate() -> None:
    contexts = {
        5: SimpleNamespace(
            quota_per_unit=500_000,
            user_balance_quota=1_888,
            withdrawable_quota=261_686,
        ),
        6: SimpleNamespace(
            quota_per_unit=500_000,
            user_balance_quota=454_612,
            withdrawable_quota=0,
        ),
        7: SimpleNamespace(
            quota_per_unit=500_000,
            user_balance_quota=979_313,
            withdrawable_quota=1_406_604,
        ),
    }
    assignments = tuple(
        SimpleNamespace(
            target=SimpleNamespace(merchant_id=merchant_id),
            candidate=SimpleNamespace(channel_id=channel_id),
        )
        for merchant_id, channel_id in ((5, 706), (6, 1170), (7, 1269))
    )
    plan = SimpleNamespace(assignments=assignments)

    assert _provider_balance_ready(contexts[5])
    assert _provider_balance_ready(contexts[6])
    assert _provider_balance_ready(contexts[7])
    assert _reserved_candidate_channel_ids(plan, contexts, 7) == frozenset({706, 1170})
    assert _reserved_candidate_channel_ids(plan, contexts, 5) == frozenset({1170, 1269})


def test_all_positive_balance_providers_enter_planning_pool() -> None:
    contexts = (
        ProviderPinContext(5, 9_005, 14_005, 25, 45_895, "m5", 500_000, 1_888, 0),
        ProviderPinContext(6, 9_006, 14_006, 26, 45_896, "m6", 500_000, 395_336, 0),
        ProviderPinContext(7, 9_007, 14_007, 27, 45_897, "m7", 500_000, 979_313, 0),
    )

    planning = _provider_planning_contexts(contexts)

    assert [context.merchant_id for context in planning] == [5, 6, 7]


def test_plan_stabilization_preserves_current_owners_for_same_channel_set() -> None:
    contexts = {
        5: ProviderPinContext(5, 9_005, 14_005, 25, 45_895, "m5", 500_000, 500_000, 0, 384, 80),
        6: ProviderPinContext(6, 9_006, 14_006, 26, 45_896, "m6", 500_000, 500_000, 0, 227, 75),
    }
    candidates = (probe_candidate(227, "0.01392"), probe_candidate(384, "0.0144"))
    targets = tuple(
        ProviderPinTarget(
            context.merchant_id,
            context.a6_user_id,
            context.token_id,
            context.token_ref_id,
            context.enodot_account_id,
        )
        for context in contexts.values()
    )
    plan = PinPlan(
        model="gpt-5.6-sol",
        assignments=tuple(
            PinAssignment(target, candidate)
            for target, candidate in zip(targets, candidates, strict=True)
        ),
        candidates=candidates,
        ranked_candidates=candidates,
        owned_channel_ids=frozenset(),
    )

    stabilized = _stabilize_provider_plan_assignments(plan, contexts)

    assert [item.candidate.channel_id for item in stabilized.assignments] == [384, 227]
    assert _reserved_candidate_channel_ids(stabilized, contexts, 5) == frozenset({227})
    assert _reserved_candidate_channel_ids(stabilized, contexts, 6) == frozenset({384})


def test_duplicate_primary_candidate_is_not_reserved_against_its_current_provider() -> None:
    contexts = {
        5: ProviderPinContext(5, 9_005, 14_005, 25, 45_895, "m5", 500_000, 500_000, 0),
        6: ProviderPinContext(6, 9_006, 14_006, 26, 45_896, "m6", 500_000, 500_000, 0),
        7: ProviderPinContext(7, 9_007, 14_007, 27, 45_897, "m7", 500_000, 500_000, 0),
    }
    assignments = tuple(
        SimpleNamespace(
            target=SimpleNamespace(merchant_id=merchant_id),
            candidate=SimpleNamespace(channel_id=channel_id),
        )
        for merchant_id, channel_id in ((5, 649), (6, 649), (7, 1242))
    )
    plan = SimpleNamespace(assignments=assignments)

    assert _reserved_candidate_channel_ids(plan, contexts, 5) == frozenset({1242})
    assert _reserved_candidate_channel_ids(plan, contexts, 6) == frozenset({1242})
    assert _reserved_candidate_channel_ids(plan, contexts, 7) == frozenset({649})


def test_confirmed_provider_pin_continues_while_user_balance_is_positive() -> None:
    context = ProviderPinContext(
        3,
        9_003,
        14_027,
        23,
        45_907,
        "m3",
        500_000,
        499_344,
        4_064,
        362,
    )

    assert _provider_balance_ready(context)
    assert _provider_balance_ready(context, user_balance_quota=125_000)
    assert _provider_balance_ready(context, user_balance_quota=124_999)
    assert _provider_balance_ready(context, user_balance_quota=1)
    assert _provider_balance_ready(context, user_balance_quota=0)
    assert _provider_balance_ready(
        context,
        user_balance_quota=499_344,
        has_confirmed_pin=False,
    )
    assert _provider_balance_ready(
        context,
        user_balance_quota=374_999,
        has_confirmed_pin=False,
    )


def test_confirmed_low_balance_provider_remains_in_planning_pool() -> None:
    contexts = (
        ProviderPinContext(3, 9_003, 14_027, 23, 45_907, "m3", 500_000, 499_344, 4_064, 362),
        ProviderPinContext(7, 9_007, 14_006, 27, 45_897, "m7", 500_000, 979_313, 0),
    )

    planning = _provider_planning_contexts(contexts)

    assert [context.merchant_id for context in planning] == [3, 7]


def test_temporary_provider_uses_exclusive_reserve_floor() -> None:
    context = ProviderPinContext(
        2,
        9_002,
        13_841,
        2,
        45_908,
        "m2",
        500_000,
        121_397,
        0,
        temporary_provider=True,
        minimum_balance_quota=25_000,
        desired_priority=1,
        desired_concurrency=15,
    )

    assert _provider_balance_ready(context) is True
    assert _provider_balance_ready(context, user_balance_quota=25_001) is True
    assert _provider_balance_ready(context, user_balance_quota=25_000) is False
    assert (
        _provider_balance_ready(
            context,
            user_balance_quota=24_999,
            withdrawable_quota=500_000,
        )
        is False
    )


def test_pin_round_skips_three_residual_providers_without_partial_failure(monkeypatch) -> None:
    class ResidualPlanningStore(SnapshotReuseStore):
        def provider_contexts(self, _model):
            funded = tuple(
                ProviderPinContext(
                    merchant_id=merchant_id,
                    a6_user_id=9_000 + merchant_id,
                    token_id=14_000 + merchant_id,
                    token_ref_id=100 + merchant_id,
                    enodot_account_id=45_000 + merchant_id,
                    session_ref=f"m{merchant_id}",
                    quota_per_unit=500_000,
                    user_balance_quota=500_000,
                    withdrawable_quota=0,
                    confirmed_pin_channel_id=700 + merchant_id,
                )
                for merchant_id in range(1, 11)
            )
            residual = tuple(
                ProviderPinContext(
                    merchant_id=merchant_id,
                    a6_user_id=9_000 + merchant_id,
                    token_id=14_000 + merchant_id,
                    token_ref_id=100 + merchant_id,
                    enodot_account_id=45_000 + merchant_id,
                    session_ref=f"m{merchant_id}",
                    quota_per_unit=500_000,
                    user_balance_quota=25_000,
                    withdrawable_quota=0,
                    confirmed_pin_channel_id=700 + merchant_id,
                    temporary_provider=True,
                    minimum_balance_quota=25_000,
                )
                for merchant_id in range(11, 14)
            )
            return funded + residual

    candidate = probe_candidate(1550, "0.008")

    def plan(targets, _quotes, **_kwargs):
        assert [target.merchant_id for target in targets] == list(range(1, 11))
        return PinPlan(
            model="gpt-5.6-sol",
            assignments=tuple(PinAssignment(target, candidate) for target in targets),
            candidates=(candidate,),
            ranked_candidates=(candidate,),
            owned_channel_ids=frozenset(),
        )

    monkeypatch.setattr("a6api_auto.trade_pin_runner.plan_provider_pins", plan)
    runner = object.__new__(ProviderPinRunner)
    runner.store = ResidualPlanningStore()
    runner.enodot_admin = SimpleNamespace()

    result = runner.run(
        model="gpt-5.6-sol",
        min_observers=2,
        observer_snapshot_max_age_seconds=0,
        pin_probe_mode="disabled",
        probe_budget=0,
    )

    assert result["success"] is True
    assert result["providers"] == 13
    assert len([item for item in result["assignments"] if item["action"] == "pin"]) == 10
    skipped = [item for item in result["assignments"] if item["action"] == "skipped"]
    assert [item["merchant_id"] for item in skipped] == [11, 12, 13]
    assert {item["skip_reason"] for item in skipped} == {
        "residual_provider_below_reserve"
    }
    assert not any(item["action"] == "error" for item in result["assignments"])

def test_provider_reconciliation_brings_funded_account_online_first() -> None:
    contexts = (
        ProviderPinContext(5, 9_005, 14_005, 25, 45_895, "m5", 500_000, 0, 0),
        ProviderPinContext(19, 9_019, 14_019, 39, 45_896, "m19", 500_000, 500_000, 0),
        ProviderPinContext(7, 9_007, 14_007, 27, 45_897, "m7", 500_000, 1, 0),
    )

    ordered = _provider_reconciliation_order(contexts)

    assert [context.merchant_id for context in ordered] == [5, 7, 19]


def test_observer_backoff_is_exponential_and_capped() -> None:
    assert [_observer_backoff_seconds(value) for value in range(1, 8)] == [
        15,
        30,
        60,
        120,
        240,
        300,
        300,
    ]
    assert _scan_error_code(A6AutoError("a6_transport_failed")) == ("a6_transport_failed")


class ObserverBackoffStore:
    def provider_contexts(self, model):
        assert model == "gpt-5.6-sol"
        return (SimpleNamespace(),)

    def observer_contexts(self):
        return (SimpleNamespace(), SimpleNamespace(), SimpleNamespace())

    def observer_scan_backoff(self, _model):
        return {
            "consecutive_failed_rounds": 3,
            "retry_after": "2026-07-25 22:45:00+00",
            "remaining_seconds": 52,
            "last_error_class": "a6_transport_failed",
        }


def test_provider_pin_round_short_circuits_during_observer_backoff() -> None:
    runner = object.__new__(ProviderPinRunner)
    runner.store = ObserverBackoffStore()

    result = runner.run(model="gpt-5.6-sol", min_observers=2)

    assert result["success"] is True
    assert result["mode"] == "observer_backoff"
    assert result["remaining_seconds"] == 52
    assert result["assignments"] == []


def test_all_observers_mode_has_no_round_size_cap() -> None:
    runner = object.__new__(ProviderPinRunner)
    store = ObserverBackoffStore()
    store.observer_contexts = lambda: tuple(SimpleNamespace() for _ in range(100))
    runner.store = store

    result = runner.run(
        model="gpt-5.6-sol",
        min_observers=2,
        observer_round_size=None,
    )

    assert result["observers"] == 100


class ObserveOnlyStore:
    def __init__(self) -> None:
        self.scans = []
        self.next_observer_round = 0

    def provider_contexts(self, _model):
        return (
            ProviderPinContext(
                4,
                7_775,
                12_509,
                16,
                45_909,
                "m4",
                500_000,
                1_000_000,
                0,
            ),
        )

    def observer_contexts(self):
        return (
            ObserverContext(1, 9_001, 13_001, "observer-1"),
            ObserverContext(2, 9_002, 13_002, "observer-2"),
        )

    def observer_scan_backoff(self, _model):
        return None

    def claim_observer_round(self, _model):
        claimed = self.next_observer_round
        self.next_observer_round += 1
        return claimed

    def owned_channel_ids(self):
        return (1120,)

    def negative_probe_channel_ids(self, *_args, **_kwargs):
        return ()

    def record_scan(self, merchant_id, *_args, **_kwargs):
        self.scans.append(merchant_id)

    def record_observer_round(self, *_args, **_kwargs):
        return 0


class SnapshotReuseStore(ObserveOnlyStore):
    def latest_observer_snapshot(self, _model, **_kwargs):
        now = time.time()
        return PersistedObserverSnapshot(
            quotes=(),
            scans=(
                {"merchant_id": 1, "source": "persisted_observer_snapshot"},
                {"merchant_id": 2, "source": "persisted_observer_snapshot"},
            ),
            latest_completed_at_epoch=now,
            oldest_completed_at_epoch=now,
        )

    def recent_failure_summaries(self, *_args, **_kwargs):
        return {}


def test_pin_reuses_completed_observer_snapshot_without_rescanning() -> None:
    runner = object.__new__(ProviderPinRunner)
    runner.store = SnapshotReuseStore()
    runner._client = lambda _user_id: SimpleNamespace(
        supplier_channels=lambda _a6_user_id: []
    )
    runner._scan_observer_observed = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("duplicate observer scan")
    )

    result = runner.run(
        model="gpt-5.6-sol",
        min_observers=2,
        observer_snapshot_max_age_seconds=0,
        pin_probe_mode="disabled",
        probe_budget=0,
    )

    assert result["success"] is False
    assert result["scan_errors"] == ["pin_consensus_candidates_empty"]
    assert result["observer_snapshot_reused"] is True
    assert len(result["scans"]) == 2


class SharedObserverBackoffStore(ObserveOnlyStore):
    def __init__(self, *, active_backoff=None, observer_count=4) -> None:
        super().__init__()
        self.active_backoff = dict(active_backoff) if active_backoff is not None else None
        self.observers = tuple(
            ObserverContext(
                merchant_id=merchant_id,
                a6_user_id=9_000 + merchant_id,
                token_id=13_000 + merchant_id,
                session_ref=f"observer-{merchant_id}",
            )
            for merchant_id in range(1, observer_count + 1)
        )
        self.backoff_reads = 0
        self.round_updates = []
        self.scan_failures = []

    def observer_contexts(self):
        return self.observers

    def observer_scan_backoff(self, _model):
        self.backoff_reads += 1
        return dict(self.active_backoff) if self.active_backoff is not None else None

    def record_observer_scan_failure(self, merchant_id, _model, **kwargs):
        self.scan_failures.append((merchant_id, kwargs["error_class"]))

    def record_observer_round(self, model, *, quorum_met, error_class):
        self.round_updates.append((model, quorum_met, error_class))
        if quorum_met:
            self.active_backoff = None
            return 0
        failures = int((self.active_backoff or {}).get("consecutive_failed_rounds") or 0) + 1
        delay = _observer_backoff_seconds(failures)
        self.active_backoff = {
            "consecutive_failed_rounds": failures,
            "retry_after": "fixture-retry-after",
            "remaining_seconds": delay,
            "last_error_class": error_class,
        }
        return delay


def _active_observer_backoff(failures=3):
    return {
        "consecutive_failed_rounds": failures,
        "retry_after": "2026-07-25 22:45:00+00",
        "remaining_seconds": _observer_backoff_seconds(failures),
        "last_error_class": "a6_transport_failed",
    }


def test_observe_only_round_skips_provider_reads_and_reconciliation() -> None:
    runner = object.__new__(ProviderPinRunner)
    runner.store = ObserveOnlyStore()
    runner._client = lambda _user_id: (_ for _ in ()).throw(AssertionError("provider read"))
    runner._scan_observer_observed = lambda *_args, **_kwargs: ObserverScanResult(
        (),
        1,
        1_000.0,
        1_000.1,
        100,
    )

    result = runner.run(
        model="gpt-5.6-sol",
        min_observers=2,
        observer_round_size=2,
        observer_window_seconds=0.001,
        observe_only=True,
    )

    assert result["success"] is True
    assert result["mode"] == "observer_only"
    assert result["quote_count"] == 0
    assert result["assignments"] == []
    assert runner.store.scans == [1, 2]


def test_observer_client_reuses_refreshed_cookie_without_live_auth(monkeypatch) -> None:
    imported_cookies = []

    class FakeClient:
        def __init__(self, origin, *, proxy_url, timeout):
            self.origin = origin
            self.proxy_url = proxy_url
            self.timeout = timeout

        def import_cookies(self, cookies):
            imported_cookies.append(dict(cookies))

    monkeypatch.setattr("a6api_auto.trade_pin_runner.A6ApiClient", FakeClient)
    runner = object.__new__(ProviderPinRunner)
    runner._records = {
        101: SimpleNamespace(
            origin="https://a6api.com",
            user_id=101,
            cookies={"session": "verified-cookie"},
        )
    }
    runner.proxy_by_user_id = {101: "http://127.0.0.1:20001"}
    runner.client_timeout = 3.0

    client = runner._observer_client(101)

    assert client.origin == "https://a6api.com"
    assert client.proxy_url == "http://127.0.0.1:20001"
    assert client.timeout == 3.0
    assert imported_cookies == [{"session": "verified-cookie"}]


def test_observe_only_settles_completed_quorum_at_round_deadline(monkeypatch) -> None:
    store = ObserveOnlyStore()
    runner = object.__new__(ProviderPinRunner)
    runner.store = store
    runner._client = lambda _user_id: (_ for _ in ()).throw(AssertionError("provider read"))

    def scan(observer, **_kwargs):
        if observer.merchant_id == 2:
            time.sleep(0.08)
        return ObserverScanResult((), 1, time.time(), time.time(), 1)

    runner._scan_observer_observed = scan
    monkeypatch.setattr(
        "a6api_auto.trade_pin_runner._pin_round_deadline",
        lambda _seconds: time.monotonic() + 0.03,
    )

    result = runner.run(
        model="gpt-5.6-sol",
        min_observers=1,
        observer_round_size=2,
        observer_window_seconds=0.001,
        observe_only=True,
    )

    assert result["success"] is True
    assert result["mode"] == "observer_only"
    assert result["observer_deadline_settled"] is True
    assert result["observer_timed_out_merchant_ids"] == [2]
    assert result["scan_errors"] == [
        "observer:2:provider_pin_round_deadline_exceeded"
    ]
    assert store.scans == [1]


def test_observe_only_commits_each_completed_scan_while_other_scan_is_running() -> None:
    store = ObserveOnlyStore()
    first_committed = threading.Event()
    original_record_scan = store.record_scan

    def record_scan(merchant_id, *args, **kwargs):
        original_record_scan(merchant_id, *args, **kwargs)
        if merchant_id == 1:
            first_committed.set()

    store.record_scan = record_scan
    runner = object.__new__(ProviderPinRunner)
    runner.store = store
    runner._client = lambda _user_id: (_ for _ in ()).throw(AssertionError("provider read"))

    def scan(observer, **_kwargs):
        if observer.merchant_id == 2:
            assert first_committed.wait(1)
        return ObserverScanResult((), 1, 1_000.0, 1_000.1, 100)

    runner._scan_observer_observed = scan

    result = runner.run(
        model="gpt-5.6-sol",
        min_observers=2,
        observer_round_size=2,
        observer_window_seconds=0.001,
        observe_only=True,
    )

    assert result["success"] is True
    assert store.scans == [1, 2]


def test_observe_only_bypasses_existing_shared_backoff() -> None:
    store = SharedObserverBackoffStore(active_backoff=_active_observer_backoff())
    runner = object.__new__(ProviderPinRunner)
    runner.store = store
    runner._client = lambda _user_id: (_ for _ in ()).throw(AssertionError("provider read"))
    runner._scan_observer_observed = lambda *_args, **_kwargs: ObserverScanResult(
        (),
        1,
        1_000.0,
        1_000.1,
        100,
    )

    result = runner.run(
        model="gpt-5.6-sol",
        min_observers=2,
        observer_round_size=2,
        observer_window_seconds=0.001,
        observe_only=True,
    )

    assert result["mode"] == "observer_only"
    assert store.backoff_reads == 0
    assert store.round_updates == []
    assert store.active_backoff == _active_observer_backoff()
    assert len(store.scans) == 2


def test_observe_only_quorum_failure_does_not_advance_backoff_and_rotates() -> None:
    store = SharedObserverBackoffStore(observer_count=4)
    runner = object.__new__(ProviderPinRunner)
    runner.store = store
    runner._client = lambda _user_id: (_ for _ in ()).throw(AssertionError("provider read"))
    scanned = []

    def failed_scan(observer, **_kwargs):
        scanned.append(observer.merchant_id)
        raise A6AutoError("a6_transport_failed")

    runner._scan_observer_observed = failed_scan
    first = runner.run(
        model="gpt-5.6-sol",
        min_observers=2,
        observer_round_size=2,
        observer_window_seconds=0.001,
        observe_only=True,
    )
    second = runner.run(
        model="gpt-5.6-sol",
        min_observers=2,
        observer_round_size=2,
        observer_window_seconds=0.001,
        observe_only=True,
    )

    assert first["mode"] == second["mode"] == "observer_quorum_failed"
    assert first["backoff_seconds"] == second["backoff_seconds"] == 0
    assert scanned == [1, 2, 3, 4]
    assert store.backoff_reads == 0
    assert store.round_updates == []
    assert store.active_backoff is None


def test_observer_systemd_uses_bounded_get_budget_and_durable_cursor_version() -> None:
    service = Path("deploy/systemd/a6-trader-observer.service").read_text()

    assert "/venv/bin/python -m a6api_auto.cli trade-pin-providers-once" in service
    assert "/venv/bin/a6api-auto" not in service
    assert "--client-timeout-seconds 3" in service
    assert "phase1-observer-v8-live-commit-30s" in service


def test_apply_quorum_failure_advances_and_then_obeys_shared_backoff() -> None:
    store = SharedObserverBackoffStore(observer_count=2)
    runner = object.__new__(ProviderPinRunner)
    runner.store = store
    runner._client = lambda _user_id: SimpleNamespace(supplier_channels=lambda _a6_user_id: [])
    scanned = []

    def failed_scan(observer, **_kwargs):
        scanned.append(observer.merchant_id)
        raise A6AutoError("a6_transport_failed")

    runner._scan_observer_observed = failed_scan
    first = runner.run(
        model="gpt-5.6-sol",
        min_observers=2,
        observer_round_size=2,
        observer_window_seconds=0.001,
        apply=True,
    )
    second = runner.run(
        model="gpt-5.6-sol",
        min_observers=2,
        observer_round_size=2,
        observer_window_seconds=0.001,
        apply=True,
    )

    assert first["mode"] == "observer_quorum_failed"
    assert first["backoff_seconds"] == 15
    assert second["mode"] == "observer_backoff"
    assert second["consecutive_failed_rounds"] == 1
    assert scanned == [1, 2]
    assert store.backoff_reads == 2
    assert store.round_updates == [("gpt-5.6-sol", False, "a6_transport_failed")]


def test_provider_client_failure_is_contained_inside_owned_channel_future() -> None:
    store = SharedObserverBackoffStore(observer_count=2)
    store.recent_failure_summaries = lambda *_args, **_kwargs: {}
    runner = object.__new__(ProviderPinRunner)
    runner.store = store
    runner._client = lambda _user_id: (_ for _ in ()).throw(
        A6AutoError("pin_a6_transport_failed")
    )
    runner._scan_observer_observed = lambda *_args, **_kwargs: ObserverScanResult(
        (),
        1,
        1_000.0,
        1_000.1,
        100,
    )

    result = runner.run(
        model="gpt-5.6-sol",
        min_observers=2,
        observer_round_size=2,
        observer_window_seconds=0.001,
    )

    assert result["success"] is False
    assert result["scan_errors"] == ["pin_consensus_candidates_empty"]
    assert store.scans == [1, 2]


def test_observe_only_and_apply_runners_do_not_mutate_each_others_backoff() -> None:
    initial = _active_observer_backoff(2)
    store = SharedObserverBackoffStore(active_backoff=initial, observer_count=2)
    observer_runner = object.__new__(ProviderPinRunner)
    observer_runner.store = store
    observer_runner._client = lambda _user_id: (_ for _ in ()).throw(
        AssertionError("provider read")
    )
    observer_runner._scan_observer_observed = lambda *_args, **_kwargs: ObserverScanResult(
        (),
        1,
        1_000.0,
        1_000.1,
        100,
    )
    apply_runner = object.__new__(ProviderPinRunner)
    apply_runner.store = store
    apply_runner._scan_observer_observed = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("apply runner scanned during backoff")
    )

    observed = observer_runner.run(
        model="gpt-5.6-sol",
        min_observers=2,
        observer_round_size=2,
        observer_window_seconds=0.001,
        observe_only=True,
    )
    applied = apply_runner.run(
        model="gpt-5.6-sol",
        min_observers=2,
        observer_round_size=2,
        observer_window_seconds=0.001,
        apply=True,
    )

    assert observed["mode"] == "observer_only"
    assert applied["mode"] == "observer_backoff"
    assert store.active_backoff == initial
    assert store.backoff_reads == 1
    assert store.round_updates == []


def test_observer_pool_rotates_three_accounts_per_round() -> None:
    observers = tuple(
        ObserverContext(
            merchant_id=merchant_id,
            a6_user_id=9_000 + merchant_id,
            token_id=13_000 + merchant_id,
            session_ref=f"session-{merchant_id}.json",
        )
        for merchant_id in range(1, 13)
    )

    rounds = [
        _select_observer_round(observers, round_size=3, round_index=index) for index in range(5)
    ]

    assert [[item.merchant_id for item in selected] for selected in rounds] == [
        [1, 2, 3],
        [4, 5, 6],
        [7, 8, 9],
        [10, 11, 12],
        [1, 2, 3],
    ]


def test_observer_scan_result_captures_request_time(monkeypatch) -> None:
    runner = object.__new__(ProviderPinRunner)
    runner._scan_observer = lambda *_args, **_kwargs: ((), 1)  # type: ignore[method-assign]
    wall_times = iter((1_000.25, 1_001.75))
    monotonic_times = iter((50.0, 51.25))
    monkeypatch.setattr("a6api_auto.trade_pin_runner.time.time", lambda: next(wall_times))
    monkeypatch.setattr("a6api_auto.trade_pin_runner.time.monotonic", lambda: next(monotonic_times))

    scan = runner._scan_observer_observed(
        ObserverContext(merchant_id=1, a6_user_id=9645, token_id=13868, session_ref="x"),
        model="gpt-5.6-sol",
        page_size=1000,
    )

    assert scan.started_at_epoch == 1_000.25
    assert scan.completed_at_epoch == 1_001.75
    assert scan.elapsed_ms == 1_250
    assert scan.page_count == 1


class RecordingCursor:
    def __init__(self, connection):
        self.connection = connection
        self.result = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, query, params=None):
        self.connection.calls.append((query, params))
        if "INSERT INTO a6_trade.a6_market_scan_runs" in query:
            self.result = {"id": 71}
        return self

    def executemany(self, query, params_seq):
        for params in params_seq:
            self.execute(query, params)
        return self

    def fetchone(self):
        return self.result

    def fetchall(self):
        return [] if self.result is None else [self.result]


class RecordingConnection:
    def __init__(self):
        self.calls = []
        self.commits = 0
        self.rollbacks = 0
        self.closed = False

    def cursor(self):
        return RecordingCursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed = True


class SnapshotCursor(RecordingCursor):
    def execute(self, query, params=None):
        self.connection.calls.append((query, params))
        self.result = self.connection.row
        return self


class SnapshotConnection(RecordingConnection):
    def __init__(self, row):
        super().__init__()
        self.row = row

    def cursor(self):
        return SnapshotCursor(self)


def test_latest_observer_snapshot_rehydrates_hash_verified_quotes() -> None:
    payload = [
        {
            "channel_id": 1170,
            "listing_id": 4011,
            "model": "gpt-5.6-sol",
            "input_price_micros": 69_326,
            "output_price_micros": 415_956,
            "final_multiplier": "0.0138652",
            "available": True,
            "route_status": "default",
            "health_state": "healthy",
            "smart_routing_labels": ["cheap"],
        }
    ]
    connection = SnapshotConnection(
        {
            "scan_run_id": 71,
            "observer_merchant_id": 9,
            "started_at_epoch": 1_000.25,
            "completed_at_epoch": 1_001.75,
            "page_count": 1,
            "candidate_count": 1,
            "elapsed_ms": 1_250,
            "result_hash": _hash(payload),
            "result_payload": payload,
        }
    )

    snapshot = PostgresPinAuditStore(lambda: connection).latest_observer_snapshot(
        "gpt-5.6-sol",
        observer_merchant_ids=(9,),
        max_age_seconds=0,
    )

    assert len(snapshot.scans) == 1
    assert snapshot.scans[0]["source"] == "persisted_observer_snapshot"
    assert len(snapshot.quotes) == 1
    quote = snapshot.quotes[0]
    assert quote.observer_merchant_id == 9
    assert quote.channel_id == 1170
    assert quote.health_state == "healthy"
    assert quote.smart_routing_labels == ("cheap",)
    assert connection.calls[0][1] == ("gpt-5.6-sol", [9], 0, 0)
    assert connection.closed is True


class ResidualDrainCursor(RecordingCursor):
    def execute(self, query, params=None):
        super().execute(query, params)
        if "SELECT state, reserve_balance_quota" in query:
            self.result = {"state": "active", "reserve_balance_quota": 25_000}
        return self


class ResidualDrainConnection(RecordingConnection):
    def cursor(self):
        return ResidualDrainCursor(self)


def test_residual_provider_promotion_is_bounded_and_skip_locked() -> None:
    connection = RecordingConnection()
    store = PostgresPinAuditStore(lambda: connection)

    assert (
        store.promote_residual_provider_candidates(
            "gpt-5.6-sol",
            maximum_active=3,
        )
        == ()
    )

    queries = [" ".join(query.split()) for query, _params in connection.calls]
    assert any("pg_advisory_xact_lock" in query for query in queries)
    candidate_query = next(query for query in queries if "state = 'candidate'" in query)
    assert "financial.user_balance_quota > policy.reserve_balance_quota" in candidate_query
    assert "FOR UPDATE OF policy, merchant SKIP LOCKED" in candidate_query
    assert "LIMIT %s" in candidate_query
    assert connection.commits == 1
    assert connection.rollbacks == 0


def test_observer_contexts_accept_normal_authenticated_observers() -> None:
    connection = RecordingConnection()

    assert PostgresPinAuditStore(lambda: connection).observer_contexts() == ()

    query = " ".join(connection.calls[0][0].split())
    assert "'observer' = ANY(m.roles)" in query
    assert "m.status IN ('authenticated', 'supplier_submitted')" in query


def test_residual_provider_drain_returns_role_to_observer_and_audits() -> None:
    connection = ResidualDrainConnection()
    store = PostgresPinAuditStore(lambda: connection)
    context = ProviderPinContext(
        2,
        9_002,
        13_841,
        2,
        45_908,
        "m2",
        500_000,
        25_000,
        0,
        temporary_provider=True,
        minimum_balance_quota=25_000,
        desired_priority=1,
        desired_concurrency=15,
    )

    assert store.drain_residual_provider(
        context,
        model="gpt-5.6-sol",
        balance_quota=25_000,
        error_class="residual_provider_reserve_reached",
    )

    queries = [" ".join(query.split()) for query, _params in connection.calls]
    assert any("SET roles = ARRAY['observer']::text[]" in query for query in queries)
    assert any("SET state = 'drained'" in query for query in queries)
    assert any("inventory_state = 'RETIRED'" in query for query in queries)
    assert any("event_type, balance_quota" in query for query in queries)
    assert connection.commits == 1
    assert connection.rollbacks == 0
    assert connection.closed is True


def test_record_scan_persists_observer_request_timestamps_and_currency() -> None:
    connection = RecordingConnection()
    store = PostgresPinAuditStore(lambda: connection)
    quote = ObserverQuote(
        observer_merchant_id=9,
        channel_id=1170,
        listing_id=4011,
        model="gpt-5.6-sol",
        final_multiplier=Decimal("0.0138652"),
        input_price_micros=69_326,
        output_price_micros=415_956,
        realtime_ratio_usd=Decimal("0.0138652"),
        realtime_ratio_cny=Decimal("0.09389790744"),
        exchange_rate=Decimal("6.7722"),
        last_success_at=995,
        circuit_probe_success_streak=1,
        circuit_scope="listing",
        circuit_recovery_mode="natural",
        circuit_cooling_until=1_005,
        circuit_last_cooling_at=990,
        circuit_last_probe_at=998,
        circuit_next_probe_at=1_002,
        cooldown_expected_recovery_at=1_005,
        cooldown_next_probe_at=1_002,
        cooldown_sources=("circuit",),
    )

    store.record_scan(
        9,
        "gpt-5.6-sol",
        (quote,),
        page_count=1,
        owned_channel_ids=frozenset(),
        started_at_epoch=1_000.25,
        completed_at_epoch=1_001.75,
        elapsed_ms=1_250,
    )

    scan_query, scan_params = connection.calls[0]
    signal_query, signal_params = connection.calls[1]
    quote_query, quote_params = connection.calls[2]
    assert "to_timestamp(%s)" in scan_query
    assert scan_params[2:4] == (1_000.25, 1_001.75)
    assert scan_params[6] == 1_250
    assert "a6_market_channel_signals_current" in signal_query
    signal_payload = json.loads(signal_params[0])
    assert signal_params[1:] == (9, 9, 9, 9, 71, 9, 71)
    assert len(signal_payload) == 1
    assert {
        key: signal_payload[0][key]
        for key in (
            "model",
            "channel_id",
            "listing_id",
            "realtime_ratio_usd",
            "realtime_ratio_cny",
            "health_state",
            "smart_routing_labels",
            "observed_at",
        )
    } == {
        "model": "gpt-5.6-sol",
        "channel_id": 1170,
        "listing_id": 4011,
        "realtime_ratio_usd": "0.0138652",
        "realtime_ratio_cny": "0.09389790744",
        "health_state": "unknown",
        "smart_routing_labels": [],
        "observed_at": 1_001.75,
    }
    assert signal_payload[0]["health_evidence"]["circuit_state"] == ""
    assert signal_payload[0]["circuit_probe_success_streak"] == 1
    assert signal_payload[0]["circuit_scope"] == "listing"
    assert signal_payload[0]["circuit_recovery_mode"] == "natural"
    assert signal_payload[0]["circuit_cooling_until"] == 1_005
    assert signal_payload[0]["circuit_next_probe_at"] == 1_002
    assert signal_payload[0]["cooldown_expected_recovery_at"] == 1_005
    assert signal_payload[0]["cooldown_next_probe_at"] == 1_002
    assert signal_payload[0]["cooldown_sources"] == ["circuit"]
    assert "realtime_ratio_usd, realtime_ratio_cny, exchange_rate" in quote_query
    assert quote_params[8:11] == (
        Decimal("0.0138652"),
        Decimal("0.09389790744"),
        Decimal("6.7722"),
    )
    assert quote_params[-2] == 1_001.75
    assert connection.commits == 1
    assert connection.rollbacks == 0
    assert connection.closed is True


def test_provider_context_only_trusts_exact_completed_pin_decision() -> None:
    connection = RecordingConnection()

    assert PostgresPinAuditStore(lambda: connection).provider_contexts("gpt-5.6-sol") == ()

    query, params = next(
        (" ".join(query.split()), params)
        for query, params in connection.calls
        if "confirmed_pin_channel_id" in query
    )
    assert "JOIN a6_trade.a6_pin_decisions pin_decision" in query
    assert "pin_decision.a6_token_id = current_pin.a6_token_id" in query
    assert "pin_decision.model = current_pin.model" in query
    assert "pin_decision.selected_channel_id = current_pin.channel_id" in query
    assert "pin_decision.decision_status = 'completed'" in query
    assert "LEFT JOIN a6_trade.residual_provider_policies residual" in query
    assert "residual.state = 'active'" in query
    assert params == ("gpt-5.6-sol", "gpt-5.6-sol")


def test_record_scan_retains_current_pin_outside_the_price_top_ten() -> None:
    connection = RecordingConnection()
    store = PostgresPinAuditStore(lambda: connection)
    quotes = tuple(
        ObserverQuote(
            observer_merchant_id=9,
            channel_id=channel_id,
            listing_id=4_000 + channel_id,
            model="gpt-5.6-sol",
            final_multiplier=Decimal(channel_id) / Decimal(1_000),
            input_price_micros=channel_id * 1_000,
            output_price_micros=channel_id * 6_000,
            realtime_ratio_usd=Decimal(channel_id) / Decimal(1_000),
        )
        for channel_id in range(1, 12)
    )

    store.record_scan(
        9,
        "gpt-5.6-sol",
        quotes,
        page_count=1,
        owned_channel_ids=frozenset(),
        pinned_channel_ids=frozenset({11}),
        completed_at_epoch=1_001.75,
    )

    persisted_channel_ids = {
        params[2]
        for query, params in connection.calls
        if "INSERT INTO a6_trade.a6_market_quotes" in query
    }
    assert persisted_channel_ids == set(range(1, 12))


def test_pin_completion_attaches_decision_to_existing_same_channel_binding() -> None:
    connection = RecordingConnection()
    store = PostgresPinAuditStore(lambda: connection)
    assignment = PinAssignment(
        ProviderPinTarget(
            merchant_id=4,
            a6_user_id=7775,
            token_id=12509,
            token_ref_id=16,
            enodot_account_id=45909,
        ),
        PinCandidate(
            channel_id=815,
            listing_id=2740,
            model="gpt-5.6-sol",
            observer_count=3,
            median_multiplier=Decimal("0.0059852"),
            median_input_price_micros=29926,
            median_output_price_micros=179554,
        ),
        switch_reason="switch_unpinned",
    )

    store.complete("pin-decision:3698", assignment, {"channel_id": 815})

    queries = [(" ".join(query.split()), params) for query, params in connection.calls]
    binding_update = next(
        (query, params)
        for query, params in queries
        if "SET decision_id = %s" in query and "UPDATE a6_trade.a6_pin_bindings" in query
    )
    assert "confirmed_at = coalesce(confirmed_at, now())" in binding_update[0]
    assert "channel_id = %s AND valid_to IS NULL" in binding_update[0]
    assert binding_update[1] == (3698, "switch_unpinned", 16, "gpt-5.6-sol", 815)
    assert connection.commits == 1
    assert connection.rollbacks == 0


class ProbeStore:
    def __init__(self, *, fresh: bool = False, lock_acquired: bool = True) -> None:
        self.fresh = fresh
        self.lock_acquired = lock_acquired
        self.audit = []

    @contextmanager
    def provider_probe_lock(self, token_ref_id):
        self.audit.append(("lock", token_ref_id))
        yield self.lock_acquired

    def recent_provider_probe_failed(self, *_args, **_kwargs):
        return False

    def probe_health_fresh(self, *_args, **_kwargs):
        return self.fresh

    def prepare(self, assignment, *, config_version):
        self.audit.append(("prepare", assignment.candidate.channel_id, config_version))
        return "pin-decision:1"

    def complete(self, action_ref, assignment, observed_pin):
        self.audit.append(
            ("complete", action_ref, assignment.candidate.channel_id, observed_pin["channel_id"])
        )

    def fail(self, action_ref, *, error_class):
        self.audit.append(("fail", action_ref, error_class))

    def record_probe(self, context, **kwargs):
        self.audit.append(
            (
                "probe",
                context.token_ref_id,
                kwargs["channel_id"],
                kwargs["result"],
            )
        )


class ProbeAdmin:
    def __init__(self, *, status="error", schedulable=False) -> None:
        self.account = {"id": 45895, "status": status, "schedulable": schedulable}
        self.calls = []

    def get_account(self, account_id):
        self.calls.append(("get", account_id))
        return dict(self.account)

    def update_account(self, account_id, payload):
        self.calls.append(("update", account_id, dict(payload)))
        self.account.update(payload)
        return dict(self.account)

    def set_schedulable(self, account_id, schedulable):
        self.calls.append(("schedulable", account_id, schedulable))
        self.account["schedulable"] = schedulable
        return dict(self.account)


def test_provider_account_restore_failure_reestablishes_isolation() -> None:
    class LostEnableResponseAdmin(ProbeAdmin):
        def set_schedulable(self, account_id, schedulable):
            result = super().set_schedulable(account_id, schedulable)
            if schedulable:
                raise A6AutoError("enodot_transport_failed")
            return result

    runner = object.__new__(ProviderPinRunner)
    runner.enodot_admin = LostEnableResponseAdmin(status="active", schedulable=False)

    with pytest.raises(A6AutoError, match="enodot_transport_failed"):
        runner._restore_provider_account(
            45_895,
            {"id": 45_895, "status": "active", "schedulable": False},
        )

    assert runner.enodot_admin.account["schedulable"] is False
    assert runner.enodot_admin.calls[-1] == ("schedulable", 45_895, False)


class PoolAdmin:
    def __init__(self, account_ids):
        self.accounts = {
            account_id: {
                "id": account_id,
                "status": "active",
                "schedulable": True,
                "concurrency": 100,
            }
            for account_id in account_ids
        }
        self.calls = []

    def get_account(self, account_id):
        self.calls.append(("get", account_id))
        return dict(self.accounts[account_id])

    def set_schedulable(self, account_id, schedulable):
        self.calls.append(("schedulable", account_id, schedulable))
        self.accounts[account_id]["schedulable"] = schedulable
        return dict(self.accounts[account_id])


def test_schedulable_pool_guard_never_disables_last_required_capacity() -> None:
    admin = PoolAdmin((45_895, 45_896, 45_897))
    guard = _SchedulablePoolGuard(
        admin,
        (45_895, 45_896, 45_897),
        minimum_capacity=100,
    )

    assert guard.disable(45_895).capacity_after == 200
    assert guard.disable(45_896).capacity_after == 100
    last = guard.disable(45_897)

    assert last.changed is False
    assert last.retained_for_capacity is True
    assert last.capacity_before == 100
    assert last.capacity_after == 100
    assert admin.accounts[45_897]["schedulable"] is True
    assert guard.capacity() == 100

    forced = guard.disable(45_897, force=True)

    assert forced.changed is True
    assert forced.retained_for_capacity is False
    assert forced.capacity_after == 0
    assert admin.accounts[45_897]["schedulable"] is False
    assert [call for call in admin.calls if call[0] == "get"] == [
        ("get", 45_895),
        ("get", 45_896),
        ("get", 45_897),
    ]


def test_schedulable_pool_guard_observes_external_disable_before_next_decision() -> None:
    admin = PoolAdmin((45_895, 45_896, 45_897))
    guard = _SchedulablePoolGuard(
        admin,
        (45_895, 45_896, 45_897),
        minimum_capacity=200,
    )
    assert guard.capacity() == 300

    disabled = admin.set_schedulable(45_895, False)
    guard.observe(45_895, disabled)
    decision = guard.disable(45_896)

    assert decision.changed is False
    assert decision.retained_for_capacity is True
    assert decision.capacity_before == 200
    assert decision.capacity_after == 200
    assert admin.accounts[45_896]["schedulable"] is True


def test_provider_restore_updates_loaded_pool_capacity_snapshot() -> None:
    admin = PoolAdmin((45_895, 45_896, 45_897))
    guard = _SchedulablePoolGuard(
        admin,
        (45_895, 45_896, 45_897),
        minimum_capacity=200,
    )
    assert guard.capacity() == 300
    disabled = admin.set_schedulable(45_895, False)
    guard.observe(45_895, disabled)
    assert guard.capacity() == 200

    runner = object.__new__(ProviderPinRunner)
    runner.enodot_admin = admin
    runner._restore_provider_account(
        45_895,
        disabled,
        pool_guard=guard,
    )

    assert guard.capacity() == 300
    decision = guard.disable(45_896)
    assert decision.changed is True
    assert decision.retained_for_capacity is False
    assert decision.capacity_before == 300
    assert decision.capacity_after == 200


def test_deadline_bound_admin_narrows_every_pool_request(monkeypatch) -> None:
    class DeadlinePoolAdmin(PoolAdmin):
        def __init__(self, account_ids):
            super().__init__(account_ids)
            self.timeout_budgets = []

        @contextmanager
        def request_timeout(self, timeout):
            self.timeout_budgets.append(timeout)
            yield

    now = [100.0]
    monkeypatch.setattr("a6api_auto.trade_pin_runner.time.monotonic", lambda: now[0])
    admin = DeadlinePoolAdmin((45_895, 45_896, 45_897))
    bounded = _DeadlineBoundEnodotAdmin(admin, 104)
    guard = _SchedulablePoolGuard(
        bounded,
        (45_895, 45_896, 45_897),
        minimum_capacity=100,
    )

    assert guard.disable(45_895).capacity_after == 200
    now[0] = 103.5
    assert guard.disable(45_896).capacity_after == 100
    assert guard.capacity() == 100

    assert admin.timeout_budgets == [4, 4, 4, 4, pytest.approx(0.5)]
    assert [call for call in admin.calls if call[0] == "get"] == [
        ("get", 45_895),
        ("get", 45_896),
        ("get", 45_897),
    ]


class ProbeA6:
    def __init__(self, channel_id: int, probe_responses=()) -> None:
        self.channel_id = channel_id
        self.probe_responses = iter(probe_responses)
        self.calls = []

    def marketplace_pins(self, user_id):
        self.calls.append(("pins", user_id, self.channel_id))
        return [
            {
                "token_id": 14020,
                "model_name": "gpt-5.6-sol",
                "channel_id": self.channel_id,
            }
        ]

    def marketplace_pin(self, user_id, **kwargs):
        self.calls.append(("pin", user_id, kwargs["channel_id"]))
        self.channel_id = kwargs["channel_id"]
        return {"channel_id": self.channel_id}

    def provider_responses_probe(self, api_key, model, **kwargs):
        self.calls.append(("probe", api_key, model, kwargs))
        response = next(self.probe_responses)
        if isinstance(response, Exception):
            raise response
        return response


def probe_context(*, confirmed_channel_id: int | None = 1185) -> ProviderPinContext:
    return ProviderPinContext(
        merchant_id=5,
        a6_user_id=9682,
        token_id=14020,
        token_ref_id=25,
        enodot_account_id=45895,
        session_ref="merchant-5.json",
        quota_per_unit=500_000,
        user_balance_quota=500_000,
        withdrawable_quota=0,
        confirmed_pin_channel_id=confirmed_channel_id,
        confirmed_pin_age_seconds=120 if confirmed_channel_id is not None else None,
    )


def probe_candidate(channel_id: int, multiplier: str) -> PinCandidate:
    return PinCandidate(
        channel_id=channel_id,
        listing_id=channel_id + 3000,
        model="gpt-5.6-sol",
        observer_count=3,
        median_multiplier=Decimal(multiplier),
        median_input_price_micros=10_000,
        median_output_price_micros=60_000,
    )


def probe_assignment(candidate: PinCandidate) -> PinAssignment:
    context = probe_context()
    return PinAssignment(
        ProviderPinTarget(
            merchant_id=context.merchant_id,
            a6_user_id=context.a6_user_id,
            token_id=context.token_id,
            token_ref_id=context.token_ref_id,
            enodot_account_id=context.enodot_account_id,
        ),
        candidate,
    )


def probe_runner(store: ProbeStore, admin: ProbeAdmin) -> ProviderPinRunner:
    runner = object.__new__(ProviderPinRunner)
    runner.store = store
    runner.enodot_admin = admin
    runner._records = {9682: SimpleNamespace(a6api_key="sk-provider-session-secret")}
    return runner


def test_observer_activation_pins_and_restores_without_probe_request() -> None:
    selected = probe_candidate(622, "0.01")
    store = ProbeStore()
    admin = ProbeAdmin(status="active", schedulable=True)
    a6 = ProbeA6(1185)
    runner = probe_runner(store, admin)

    result, remaining = runner._reconcile_provider_pin(
        probe_context(),
        a6,
        probe_assignment(selected),
        candidates=(selected,),
        owned_channel_ids=set(),
        negative_channel_ids={1185},
        model="gpt-5.6-sol",
        config_version="observer-driven-no-probe-v1",
        apply=True,
        probe_budget_remaining=0,
        probe_failure_ttl_seconds=45,
        probe_health_ttl_seconds=300,
        probe_max_output_tokens=16,
        probe_timeout_seconds=6,
        probe_round_deadline=time.monotonic() + 8,
        balance_transferred=False,
        pin_probe_mode="disabled",
        force_predrain_channel_ids=frozenset({1185}),
    )

    assert result["action"] == "pin"
    assert result["selected_channel_id"] == 622
    assert result["observer_confirmed"] is True
    assert result["probe_confirmed"] is False
    assert result["readiness_state"] == "READY_UNPROVEN"
    assert result["probe_attempts"] == []
    assert remaining == 0
    assert all(call[0] != "probe" for call in a6.calls)
    assert a6.channel_id == 622
    assert admin.account["schedulable"] is True


def test_probe_402_marks_negative_and_retries_next_top_k_before_recovery() -> None:
    first = probe_candidate(1185, "0.01")
    second = probe_candidate(622, "0.02")
    store = ProbeStore()
    admin = ProbeAdmin(status="error", schedulable=False)
    a6 = ProbeA6(
        1185,
        (
            (402, {"error": {"code": "fixed_merchant_unavailable", "secret": "x"}}),
            (200, {"probe_event": "response.created"}),
        ),
    )
    negative = set()
    runner = probe_runner(store, admin)

    result, remaining = runner._reconcile_provider_pin(
        probe_context(),
        a6,
        probe_assignment(first),
        candidates=(first, second),
        owned_channel_ids={1120},
        negative_channel_ids=negative,
        model="gpt-5.6-sol",
        config_version="probe-hotfix-v1",
        apply=True,
        probe_budget_remaining=4,
        probe_failure_ttl_seconds=45,
        probe_health_ttl_seconds=300,
        probe_max_output_tokens=16,
        probe_timeout_seconds=6,
        probe_round_deadline=time.monotonic() + 8,
        balance_transferred=False,
    )

    assert result["action"] == "pin"
    assert result["selected_channel_id"] == 622
    assert result["probe_confirmed"] is True
    assert [item["channel_id"] for item in result["probe_attempts"]] == [1185, 622]
    assert result["probe_attempts"][0]["error_class"] == "fixed_merchant_unavailable"
    assert "secret" not in str(result)
    assert remaining == 2
    assert negative == {1185}
    assert a6.channel_id == 622
    assert [call[0] for call in a6.calls].count("probe") == 2
    assert admin.account == {"id": 45895, "status": "active", "schedulable": True}
    assert ("update", 45895, {"status": "active"}) in admin.calls
    first_enable = admin.calls.index(("schedulable", 45895, True))
    assert all(call != ("schedulable", 45895, True) for call in admin.calls[:first_enable])
    probe_audits = [item for item in store.audit if item[0] == "probe"]
    assert [item[2] for item in probe_audits] == [1185, 622]
    assert probe_audits[0][3].error_class == "fixed_merchant_unavailable"


def test_unaudited_live_pin_is_reconfirmed_even_when_channel_is_unchanged() -> None:
    candidate = probe_candidate(1185, "0.01")
    store = ProbeStore(fresh=True)
    admin = ProbeAdmin(status="active", schedulable=True)
    a6 = ProbeA6(1185, ((200, {"probe_event": "response.created"}),))
    runner = probe_runner(store, admin)

    result, remaining = runner._reconcile_provider_pin(
        probe_context(confirmed_channel_id=None),
        a6,
        probe_assignment(candidate),
        candidates=(candidate,),
        owned_channel_ids={1120},
        negative_channel_ids=set(),
        model="gpt-5.6-sol",
        config_version="audit-reconfirm-v1",
        apply=True,
        probe_budget_remaining=2,
        probe_failure_ttl_seconds=45,
        probe_health_ttl_seconds=300,
        probe_max_output_tokens=16,
        probe_timeout_seconds=6,
        probe_round_deadline=time.monotonic() + 8,
        balance_transferred=False,
    )

    assert result["action"] == "pin"
    assert result["selected_channel_id"] == 1185
    assert result["confirmed_pin_drift"] is True
    assert result["probe_confirmed"] is True
    assert remaining == 1
    assert ("pin", 9682, 1185) in a6.calls
    assert ("prepare", 1185, "audit-reconfirm-v1") in store.audit
    assert ("complete", "pin-decision:1", 1185, 1185) in store.audit


def test_red_or_yellow_pin_switch_runs_normal_emergency_boost(monkeypatch) -> None:
    selected = probe_candidate(622, "0.02")
    store = ProbeStore()
    admin = ProbeAdmin(status="active", schedulable=True)
    a6 = ProbeA6(1185, ((200, {"probe_event": "response.created"}),))
    runner = probe_runner(store, admin)
    boost_calls = []

    def fake_boost(context, **kwargs):
        boost_calls.append((context.merchant_id, kwargs))
        return {
            "triggered": True,
            "request_count": 4,
            "success_count": 4,
        }

    monkeypatch.setattr(runner, "_run_emergency_boost", fake_boost)

    result, remaining = runner._reconcile_provider_pin(
        probe_context(),
        a6,
        probe_assignment(selected),
        candidates=(selected,),
        owned_channel_ids={1120},
        negative_channel_ids=set(),
        model="gpt-5.6-sol",
        config_version="health-switch-v1",
        apply=True,
        probe_budget_remaining=2,
        probe_failure_ttl_seconds=45,
        probe_health_ttl_seconds=300,
        probe_max_output_tokens=16,
        probe_timeout_seconds=6,
        probe_round_deadline=time.monotonic() + 8,
        balance_transferred=False,
        emergency_channel_ids=frozenset({1185}),
    )

    assert result["action"] == "pin"
    assert result["selected_channel_id"] == 622
    assert result["emergency_channel_detected"] is True
    assert result["emergency_boost"]["request_count"] == 4
    assert remaining == 1
    assert boost_calls == [
        (
            5,
            {
                "model": "gpt-5.6-sol",
                "source_channel_id": 1185,
                "channel_id": 622,
                "minimum_requests": 3,
                "maximum_requests": 5,
                "timeout_seconds": 12,
                "cooldown_seconds": 60,
                "config_version": "health-switch-v1",
            },
        )
    ]


def test_probe_transport_failure_isolates_current_provider() -> None:
    first = probe_candidate(1185, "0.01")
    reserved = probe_candidate(622, "0.02")
    second = probe_candidate(641, "0.03")
    store = ProbeStore()
    admin = ProbeAdmin(status="active", schedulable=True)
    a6 = ProbeA6(
        1185,
        (
            A6AutoError("provider_probe_transport_failed"),
            (200, {"probe_event": "response.created"}),
        ),
    )
    negative = set()
    runner = probe_runner(store, admin)

    result, remaining = runner._reconcile_provider_pin(
        probe_context(),
        a6,
        probe_assignment(first),
        candidates=(first, reserved, second),
        owned_channel_ids={1120},
        negative_channel_ids=negative,
        model="gpt-5.6-sol",
        config_version="probe-hotfix-v1",
        apply=True,
        probe_budget_remaining=4,
        probe_failure_ttl_seconds=45,
        probe_health_ttl_seconds=300,
        probe_max_output_tokens=16,
        probe_timeout_seconds=6,
        probe_round_deadline=time.monotonic() + 8,
        balance_transferred=False,
        reserved_channel_ids=frozenset({622}),
    )

    assert result["action"] == "probe_pending"
    assert result["applied"] is False
    assert result["selected_channel_id"] == 1185
    assert result["probe_pending"] is True
    assert [item["channel_id"] for item in result["probe_attempts"]] == [1185]
    assert result["probe_attempts"][0]["error_class"] == "provider_probe_transport_failed"
    assert remaining == 3
    assert negative == set()
    assert a6.channel_id == 1185
    assert ("schedulable", 45895, False) in admin.calls
    assert admin.account["schedulable"] is False


def test_new_pin_transport_failure_stays_drained_after_pin_readback() -> None:
    selected = probe_candidate(622, "0.02")
    store = ProbeStore()
    admin = ProbeAdmin(status="active", schedulable=True)
    a6 = ProbeA6(
        1185,
        (A6AutoError("provider_probe_transport_failed"),),
    )
    runner = probe_runner(store, admin)

    result, remaining = runner._reconcile_provider_pin(
        probe_context(),
        a6,
        probe_assignment(selected),
        candidates=(selected,),
        owned_channel_ids=set(),
        negative_channel_ids=set(),
        model="gpt-5.6-sol",
        config_version="predrain-probe-v1",
        apply=True,
        probe_budget_remaining=1,
        probe_failure_ttl_seconds=45,
        probe_health_ttl_seconds=300,
        probe_max_output_tokens=16,
        probe_timeout_seconds=6,
        probe_round_deadline=time.monotonic() + 8,
        balance_transferred=False,
    )

    assert result["action"] == "pin_pending_probe"
    assert result["confirmed"] is True
    assert result["probe_confirmed"] is False
    assert remaining == 0
    assert a6.channel_id == 622
    assert admin.account["schedulable"] is False
    assert admin.calls == [
        ("get", 45895),
        ("schedulable", 45895, False),
    ]


def test_unhealthy_provider_transport_failure_retries_next_top_k() -> None:
    first = probe_candidate(1185, "0.01")
    second = probe_candidate(641, "0.03")
    store = ProbeStore()
    admin = ProbeAdmin(status="error", schedulable=False)
    a6 = ProbeA6(
        1185,
        (
            A6AutoError("provider_probe_transport_failed"),
            (200, {"probe_event": "response.created"}),
        ),
    )
    runner = probe_runner(store, admin)

    result, remaining = runner._reconcile_provider_pin(
        probe_context(),
        a6,
        probe_assignment(first),
        candidates=(first, second),
        owned_channel_ids={1120},
        negative_channel_ids=set(),
        model="gpt-5.6-sol",
        config_version="probe-hotfix-v1",
        apply=True,
        probe_budget_remaining=4,
        probe_failure_ttl_seconds=45,
        probe_health_ttl_seconds=300,
        probe_max_output_tokens=16,
        probe_timeout_seconds=6,
        probe_round_deadline=time.monotonic() + 8,
        balance_transferred=False,
    )

    assert result["action"] == "pin"
    assert result["selected_channel_id"] == 641
    assert [item["channel_id"] for item in result["probe_attempts"]] == [1185, 641]
    assert remaining == 2
    assert admin.account["schedulable"] is True


def test_fresh_noop_does_not_write_schedulable_or_spend_probe() -> None:
    candidate = probe_candidate(622, "0.02")
    store = ProbeStore(fresh=True)
    admin = ProbeAdmin(status="active", schedulable=True)
    a6 = ProbeA6(622)
    runner = probe_runner(store, admin)

    result, remaining = runner._reconcile_provider_pin(
        probe_context(confirmed_channel_id=622),
        a6,
        probe_assignment(candidate),
        candidates=(candidate,),
        owned_channel_ids={1120},
        negative_channel_ids=set(),
        model="gpt-5.6-sol",
        config_version="probe-hotfix-v1",
        apply=True,
        probe_budget_remaining=4,
        probe_failure_ttl_seconds=45,
        probe_health_ttl_seconds=300,
        probe_max_output_tokens=16,
        probe_timeout_seconds=6,
        probe_round_deadline=time.monotonic() + 8,
        balance_transferred=False,
    )

    assert result["action"] == "noop"
    assert result["applied"] is False
    assert result["probe_health_fresh"] is True
    assert remaining == 4
    assert admin.calls == [("get", 45895)]
    assert not any(call[0] == "probe" for call in a6.calls)


def test_probe_transport_timeout_is_capped_by_probe_round_remaining() -> None:
    candidate = probe_candidate(1185, "0.01")
    store = ProbeStore()
    admin = ProbeAdmin(status="active", schedulable=True)
    a6 = ProbeA6(1185, ((200, {"probe_event": "response.created"}),))
    runner = probe_runner(store, admin)
    probe_deadline = time.monotonic() + 1

    result, remaining = runner._reconcile_provider_pin(
        probe_context(),
        a6,
        probe_assignment(candidate),
        candidates=(candidate,),
        owned_channel_ids=set(),
        negative_channel_ids=set(),
        model="gpt-5.6-sol",
        config_version="probe-deadline-v1",
        apply=True,
        probe_budget_remaining=1,
        probe_failure_ttl_seconds=45,
        probe_health_ttl_seconds=300,
        probe_max_output_tokens=16,
        probe_timeout_seconds=6,
        probe_round_deadline=probe_deadline,
        balance_transferred=False,
    )

    probe_call = next(call for call in a6.calls if call[0] == "probe")
    assert result["probe_confirmed"] is True
    assert remaining == 0
    assert 0 < probe_call[3]["timeout"] <= 1


def test_healthy_current_pin_is_held_during_minimum_duration() -> None:
    current = replace(probe_candidate(1185, "0.011"), composite_score=Decimal("0.20"))
    proposed = replace(probe_candidate(622, "0.010"), composite_score=Decimal("0.10"))
    context = replace(
        probe_context(),
        confirmed_pin_channel_id=1185,
        confirmed_pin_age_seconds=20,
    )
    store = ProbeStore(fresh=True)
    admin = ProbeAdmin(status="active", schedulable=True)
    a6 = ProbeA6(1185)
    runner = probe_runner(store, admin)

    result, remaining = runner._reconcile_provider_pin(
        context,
        a6,
        probe_assignment(proposed),
        candidates=(proposed, current),
        ranked_candidates=(proposed, current),
        owned_channel_ids=set(),
        negative_channel_ids=set(),
        model="gpt-5.6-sol",
        config_version="health-hysteresis-v1",
        apply=True,
        probe_budget_remaining=4,
        probe_failure_ttl_seconds=45,
        probe_health_ttl_seconds=300,
        probe_max_output_tokens=16,
        probe_timeout_seconds=6,
        probe_round_deadline=time.monotonic() + 8,
        balance_transferred=False,
        pin_minimum_hold_seconds=60,
        pin_minimum_switch_gain="0.05",
    )

    assert result["action"] == "hold_minimum_duration"
    assert result["current_channel_id"] == 1185
    assert result["proposed_channel_id"] == 622
    assert result["selected_channel_id"] == 1185
    assert result["hold_remaining_seconds"] == 40
    assert result["expected_switch_gain"] == "0.09090909090909090909090909091"
    assert remaining == 4
    assert not any(call[0] == "pin" for call in a6.calls)


def test_live_pin_drift_bypasses_minimum_hold_and_restores_plan() -> None:
    persisted = replace(probe_candidate(1185, "0.011"), composite_score=Decimal("0.20"))
    drifted = replace(probe_candidate(790, "0.070"), composite_score=Decimal("0.80"))
    proposed = replace(probe_candidate(622, "0.010"), composite_score=Decimal("0.10"))
    context = replace(
        probe_context(),
        confirmed_pin_channel_id=1185,
        confirmed_pin_age_seconds=20,
    )
    store = ProbeStore()
    admin = ProbeAdmin(status="active", schedulable=True)
    a6 = ProbeA6(790, ((200, {"probe_event": "response.created"}),))
    runner = probe_runner(store, admin)

    result, remaining = runner._reconcile_provider_pin(
        context,
        a6,
        probe_assignment(proposed),
        candidates=(proposed, drifted, persisted),
        ranked_candidates=(proposed, persisted, drifted),
        owned_channel_ids=set(),
        negative_channel_ids=set(),
        model="gpt-5.6-sol",
        config_version="pin-drift-recovery-v1",
        apply=True,
        probe_budget_remaining=4,
        probe_failure_ttl_seconds=45,
        probe_health_ttl_seconds=300,
        probe_max_output_tokens=16,
        probe_timeout_seconds=6,
        probe_round_deadline=time.monotonic() + 8,
        balance_transferred=False,
        pin_minimum_hold_seconds=60,
        pin_minimum_switch_gain="0.05",
    )

    assert result["action"] == "pin"
    assert result["selected_channel_id"] == 622
    assert result["switch_policy"] == "switch_bypass"
    assert result["confirmed_pin_drift"] is True
    assert a6.channel_id == 622
    assert remaining == 3


def test_failed_probe_keeps_enodot_account_unschedulable() -> None:
    candidate = probe_candidate(1185, "0.01")
    store = ProbeStore()
    admin = ProbeAdmin(status="active", schedulable=True)
    a6 = ProbeA6(
        1185,
        ((402, {"error": {"code": "fixed_merchant_unavailable"}}),),
    )
    negative = set()
    runner = probe_runner(store, admin)

    result, remaining = runner._reconcile_provider_pin(
        probe_context(),
        a6,
        probe_assignment(candidate),
        candidates=(candidate,),
        owned_channel_ids=set(),
        negative_channel_ids=negative,
        model="gpt-5.6-sol",
        config_version="probe-hotfix-v1",
        apply=True,
        probe_budget_remaining=1,
        probe_failure_ttl_seconds=45,
        probe_health_ttl_seconds=300,
        probe_max_output_tokens=16,
        probe_timeout_seconds=6,
        probe_round_deadline=time.monotonic() + 8,
        balance_transferred=False,
    )

    assert result["action"] == "error"
    assert result["error_class"] == "fixed_merchant_unavailable"
    assert remaining == 0
    assert negative == {1185}
    assert admin.account["schedulable"] is False
    assert ("schedulable", 45895, True) not in admin.calls


def test_failed_probe_force_isolates_last_schedulable_provider_capacity() -> None:
    candidate = probe_candidate(1185, "0.01")
    store = ProbeStore()
    admin = ProbeAdmin(status="active", schedulable=True)
    admin.account["concurrency"] = 100
    guard = _SchedulablePoolGuard(admin, (45895,), minimum_capacity=100)
    a6 = ProbeA6(
        1185,
        ((402, {"error": {"code": "fixed_merchant_unavailable"}}),),
    )
    runner = probe_runner(store, admin)

    result, remaining = runner._reconcile_provider_pin(
        probe_context(),
        a6,
        probe_assignment(candidate),
        candidates=(candidate,),
        owned_channel_ids=set(),
        negative_channel_ids=set(),
        model="gpt-5.6-sol",
        config_version="pool-capacity-guard-v1",
        apply=True,
        probe_budget_remaining=1,
        probe_failure_ttl_seconds=45,
        probe_health_ttl_seconds=300,
        probe_max_output_tokens=16,
        probe_timeout_seconds=6,
        probe_round_deadline=time.monotonic() + 8,
        balance_transferred=False,
        pool_guard=guard,
    )

    assert result["action"] == "error"
    assert result["retained_for_pool_capacity"] is False
    assert result["pool_capacity_before"] == 100
    assert result["pool_capacity_after"] == 0
    assert remaining == 0
    assert admin.account["schedulable"] is False
    assert ("schedulable", 45895, False) in admin.calls


def test_three_failure_channel_switch_forces_predrain_past_capacity_guard() -> None:
    current = probe_candidate(1185, "0.02")
    proposed = probe_candidate(622, "0.01")
    store = ProbeStore()
    admin = ProbeAdmin(status="active", schedulable=True)
    admin.account["concurrency"] = 100
    guard = _SchedulablePoolGuard(admin, (45895,), minimum_capacity=100)
    a6 = ProbeA6(1185, ((200, {"probe_event": "response.created"}),))
    runner = probe_runner(store, admin)

    result, remaining = runner._reconcile_provider_pin(
        replace(
            probe_context(),
            confirmed_pin_channel_id=1185,
            confirmed_pin_age_seconds=10,
        ),
        a6,
        probe_assignment(proposed),
        candidates=(proposed, current),
        owned_channel_ids=set(),
        negative_channel_ids={1185},
        model="gpt-5.6-sol",
        config_version="three-failure-force-predrain-v1",
        apply=True,
        probe_budget_remaining=1,
        probe_failure_ttl_seconds=45,
        probe_health_ttl_seconds=300,
        probe_max_output_tokens=16,
        probe_timeout_seconds=6,
        probe_round_deadline=time.monotonic() + 8,
        balance_transferred=False,
        force_predrain_channel_ids=frozenset({1185}),
        pool_guard=guard,
    )

    assert result["action"] == "pin"
    assert result["selected_channel_id"] == 622
    assert result["probe_confirmed"] is True
    assert remaining == 0
    assert ("schedulable", 45895, False) in admin.calls
    assert admin.account["schedulable"] is True


def test_probe_lock_busy_has_no_pin_or_admin_side_effects() -> None:
    candidate = probe_candidate(622, "0.02")
    store = ProbeStore(lock_acquired=False)
    admin = ProbeAdmin(status="error", schedulable=False)
    a6 = ProbeA6(622)
    runner = probe_runner(store, admin)

    result, remaining = runner._reconcile_provider_pin(
        probe_context(),
        a6,
        probe_assignment(candidate),
        candidates=(candidate,),
        owned_channel_ids=set(),
        negative_channel_ids=set(),
        model="gpt-5.6-sol",
        config_version="probe-hotfix-v1",
        apply=True,
        probe_budget_remaining=4,
        probe_failure_ttl_seconds=45,
        probe_health_ttl_seconds=300,
        probe_max_output_tokens=16,
        probe_timeout_seconds=6,
        probe_round_deadline=time.monotonic() + 8,
        balance_transferred=False,
    )

    assert result["error_class"] == "provider_probe_lock_busy"
    assert remaining == 4
    assert a6.calls == []
    assert admin.calls == []
