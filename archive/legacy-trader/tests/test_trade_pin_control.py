from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from a6api_auto.trade_pin_control import (
    ObserverQuote,
    PinAssignment,
    PinCandidate,
    PinControlError,
    ProviderPinExecutor,
    ProviderPinTarget,
    apply_pin_hysteresis,
    plan_provider_pins,
    weighted_pin_multiplier,
)

OWNED = {768, 832, 1087}


def target(merchant_id: int, token_id: int, account_id: int) -> ProviderPinTarget:
    return ProviderPinTarget(
        merchant_id=merchant_id,
        a6_user_id=9500 + merchant_id,
        token_id=token_id,
        token_ref_id=100 + merchant_id,
        enodot_account_id=account_id,
    )


def quote(
    observer: int,
    channel: int,
    multiplier: str,
    *,
    available: bool = True,
    success_rate: int | None = None,
    latency_ms: str | None = None,
    ttft_ms: str | None = None,
    health_state: str = "healthy",
    labels: tuple[str, ...] = (),
    failure_responsibility: str = "unknown",
    client_failures: int = 0,
    provider_failures: int = 0,
    platform_failures: int = 0,
    unknown_failures: int = 0,
    last_success_at: int | None = None,
):
    return ObserverQuote(
        observer_merchant_id=observer,
        channel_id=channel,
        listing_id=channel + 1000,
        model="gpt-5.6-sol",
        final_multiplier=Decimal(multiplier),
        input_price_micros=int(Decimal(multiplier) * 1_000_000),
        output_price_micros=int(Decimal(multiplier) * 8_000_000),
        available=available,
        recent_success_rate=success_rate,
        recent_p50_ms=Decimal(latency_ms) if latency_ms is not None else None,
        p50_ttft_ms=Decimal(ttft_ms) if ttft_ms is not None else None,
        health_state=health_state,
        smart_routing_labels=labels,
        last_failure_responsibility=failure_responsibility,
        recent_failure_count=(
            client_failures + provider_failures + platform_failures + unknown_failures
        ),
        recent_client_failure_count=client_failures,
        recent_provider_failure_count=provider_failures,
        recent_platform_failure_count=platform_failures,
        recent_unknown_failure_count=unknown_failures,
        last_success_at=last_success_at,
    )


def test_plan_requires_observer_consensus_excludes_owned_and_diversifies() -> None:
    plan = plan_provider_pins(
        [target(5, 14020, 45895), target(19, 14093, 45896)],
        [
            quote(1, 768, "0.01"),
            quote(2, 768, "0.01"),
            quote(1, 900, "0.02"),
            quote(2, 900, "0.04"),
            quote(3, 900, "0.03"),
            quote(1, 901, "0.04"),
            quote(2, 901, "0.04"),
            quote(1, 902, "0.01"),
        ],
        model="gpt-5.6-sol",
        owned_channel_ids=OWNED,
        min_observers=2,
        top_k=2,
    )

    assert [item.channel_id for item in plan.candidates] == [900, 901]
    assert [item.candidate.channel_id for item in plan.assignments] == [900, 901]
    assert plan.candidates[0].observer_count == 3
    assert plan.candidates[0].median_multiplier == Decimal("0.03")


def test_plan_rejects_no_cross_observer_candidate() -> None:
    with pytest.raises(PinControlError, match="pin_consensus_candidates_empty"):
        plan_provider_pins(
            [target(5, 14020, 45895)],
            [quote(1, 900, "0.02"), quote(2, 901, "0.03")],
            model="gpt-5.6-sol",
            owned_channel_ids=OWNED,
        )


def test_plan_rejects_only_candidate_above_absolute_multiplier_ceiling() -> None:
    with pytest.raises(
        PinControlError,
        match="pin_economic_or_health_gate_candidates_empty",
    ):
        plan_provider_pins(
            [target(5, 14020, 45895)],
            [quote(observer, 430, "0.72") for observer in (1, 2)],
            model="gpt-5.6-sol",
            owned_channel_ids=OWNED,
            maximum_multiplier="0.05",
        )

    plan = plan_provider_pins(
        [target(5, 14020, 45895)],
        [quote(observer, 431, "0.014") for observer in (1, 2)],
        model="gpt-5.6-sol",
        owned_channel_ids=OWNED,
        maximum_multiplier="0.05",
    )

    assert [candidate.channel_id for candidate in plan.candidates] == [431]


def test_plan_fails_closed_above_protected_billing_floor() -> None:
    with pytest.raises(
        PinControlError,
        match="pin_economic_or_health_gate_candidates_empty",
    ):
        plan_provider_pins(
            [target(5, 14020, 45895)],
            [quote(observer, 430, "0.024") for observer in (1, 2)],
            model="gpt-5.6-sol",
            owned_channel_ids=OWNED,
            maximum_multiplier="0.05",
            protected_billing_floor_multiplier="0.020",
        )


def test_plan_enforces_relative_current_cost_ceiling_in_same_unit() -> None:
    quotes = (
        [quote(observer, 430, "0.016") for observer in (1, 2)]
        + [quote(observer, 431, "0.014") for observer in (1, 2)]
    )

    plan = plan_provider_pins(
        [target(5, 14020, 45895)],
        quotes,
        model="gpt-5.6-sol",
        owned_channel_ids=OWNED,
        maximum_multiplier="0.05",
        protected_billing_floor_multiplier="0.018",
        current_operating_cost_multiplier="0.010",
        maximum_relative_cost_ratio="1.5",
    )

    assert [candidate.channel_id for candidate in plan.candidates] == [431]
    assert plan.absolute_cost_ceiling_multiplier == Decimal("0.05")
    assert plan.protected_billing_floor_multiplier == Decimal("0.018")
    assert plan.current_operating_cost_multiplier == Decimal("0.010")
    assert plan.relative_cost_ceiling_multiplier == Decimal("0.0150")
    assert plan.effective_cost_ceiling_multiplier == Decimal("0.0150")


@pytest.mark.parametrize("maximum", [True, "0", "-0.01", "nan", "inf"])
def test_plan_validates_absolute_multiplier_ceiling(maximum: object) -> None:
    with pytest.raises(PinControlError, match="pin_maximum_multiplier_invalid"):
        plan_provider_pins(
            [target(5, 14020, 45895)],
            [quote(observer, 431, "0.014") for observer in (1, 2)],
            model="gpt-5.6-sol",
            owned_channel_ids=OWNED,
            maximum_multiplier=maximum,
        )


@pytest.mark.parametrize("ratio", [True, "0", "0.99", "nan", "inf"])
def test_plan_validates_relative_cost_ratio(ratio: object) -> None:
    with pytest.raises(PinControlError, match="pin_maximum_relative_cost_ratio_invalid"):
        plan_provider_pins(
            [target(5, 14020, 45895)],
            [quote(observer, 431, "0.014") for observer in (1, 2)],
            model="gpt-5.6-sol",
            owned_channel_ids=OWNED,
            maximum_relative_cost_ratio=ratio,
        )


def test_plan_prioritizes_lower_price_over_success_rate_and_latency() -> None:
    plan = plan_provider_pins(
        [target(5, 14020, 45895)],
        [
            quote(
                observer,
                900,
                "0.040",
                success_rate=5000,
                latency_ms="20000",
                health_state="healthy",
                labels=("slow",),
            )
            for observer in (1, 2, 3)
        ]
        + [
            quote(
                observer,
                901,
                "0.041",
                success_rate=10000,
                latency_ms="2000",
                ttft_ms="600",
                health_state="healthy",
                labels=("fast", "stable"),
            )
            for observer in (1, 2, 3)
        ],
        model="gpt-5.6-sol",
        owned_channel_ids=OWNED,
        top_k=2,
    )

    assert [item.channel_id for item in plan.candidates] == [900, 901]
    cheaper = plan.candidates[0]
    assert cheaper.median_success_rate == 5000
    assert cheaper.median_latency_ms == Decimal("20000")
    assert cheaper.health_state == "healthy"
    assert cheaper.success_score == 0
    assert cheaper.composite_score == 0


def test_plan_excludes_channels_without_ready_health_state() -> None:
    plan = plan_provider_pins(
        [target(5, 14020, 45895)],
        [
            quote(observer, channel, multiplier, health_state=health_state)
            for observer in (1, 2, 3)
            for channel, multiplier, health_state in (
                (900, "0.010", "degraded"),
                (901, "0.011", "unlabeled"),
                (902, "0.012", "unknown"),
                (903, "0.013", "healthy"),
            )
        ],
        model="gpt-5.6-sol",
        owned_channel_ids=OWNED,
        top_k=4,
    )

    assert [item.channel_id for item in plan.candidates] == [903]
    assert [item.candidate.channel_id for item in plan.assignments] == [903]


def test_equal_price_uses_latest_success_then_latency_and_ignores_success_rate() -> None:
    plan = plan_provider_pins(
        [target(5, 14020, 45895)],
        [
            quote(
                observer,
                900,
                "0.040",
                success_rate=10000,
                latency_ms="1000",
                last_success_at=100,
            )
            for observer in (1, 2, 3)
        ]
        + [
            quote(
                observer,
                901,
                "0.040",
                success_rate=100,
                latency_ms="9000",
                last_success_at=200,
            )
            for observer in (1, 2, 3)
        ]
        + [
            quote(
                observer,
                902,
                "0.040",
                success_rate=9000,
                latency_ms="2000",
                last_success_at=200,
            )
            for observer in (1, 2, 3)
        ],
        model="gpt-5.6-sol",
        owned_channel_ids=OWNED,
        top_k=3,
    )

    assert [item.channel_id for item in plan.candidates] == [902, 901, 900]
    assert [item.latest_success_at for item in plan.candidates] == [200, 200, 100]
    assert all(item.success_score == 0 for item in plan.candidates)


def test_equal_tuple_uses_channel_id_and_places_missing_signals_last() -> None:
    quotes = []
    for observer in (1, 2):
        quotes.extend(
            (
                replace(
                    quote(
                        observer,
                        902,
                        "0.040",
                        latency_ms="1000",
                        last_success_at=200,
                    ),
                    input_price_micros=1,
                    output_price_micros=1,
                ),
                replace(
                    quote(
                        observer,
                        901,
                        "0.040",
                        latency_ms="1000",
                        last_success_at=200,
                    ),
                    input_price_micros=999_999,
                    output_price_micros=9_999_999,
                ),
                quote(observer, 903, "0.040", latency_ms="1"),
                quote(observer, 904, "0.040", last_success_at=200),
            )
        )

    plan = plan_provider_pins(
        [target(5, 14020, 45895)],
        quotes,
        model="gpt-5.6-sol",
        owned_channel_ids=OWNED,
        top_k=4,
        maximum_latency_ms=None,
    )

    assert [item.channel_id for item in plan.candidates] == [901, 902, 904, 903]


def test_nonzero_score_weights_do_not_override_strict_tuple_order() -> None:
    plan = plan_provider_pins(
        [target(5, 14020, 45895)],
        [
            quote(
                observer,
                900,
                "0.040",
                latency_ms="20000",
                last_success_at=100,
            )
            for observer in (1, 2)
        ]
        + [
            quote(
                observer,
                901,
                "0.041",
                latency_ms="10",
                last_success_at=200,
            )
            for observer in (1, 2)
        ],
        model="gpt-5.6-sol",
        owned_channel_ids=OWNED,
        top_k=2,
        price_weight="0.05",
        success_weight="0.45",
        latency_weight="0.50",
        maximum_latency_ms=None,
    )

    assert [item.channel_id for item in plan.candidates] == [900, 901]
    assert plan.candidates[0].composite_score > plan.candidates[1].composite_score


def test_plan_uses_slower_of_p50_and_ttft_for_hard_latency_gate() -> None:
    plan = plan_provider_pins(
        [target(5, 14020, 45895)],
        [
            quote(
                observer,
                900,
                "0.010",
                success_rate=9900,
                latency_ms="2000",
                ttft_ms="12001",
                health_state="healthy",
            )
            for observer in (1, 2, 3)
        ]
        + [
            quote(
                observer,
                901,
                "0.011",
                success_rate=9900,
                latency_ms="6000",
                ttft_ms="4000",
                health_state="healthy",
            )
            for observer in (1, 2, 3)
        ],
        model="gpt-5.6-sol",
        owned_channel_ids=OWNED,
        minimum_success_rate=9000,
        maximum_latency_ms="10000",
    )

    assert [item.channel_id for item in plan.ranked_candidates] == [901]
    assert plan.candidates[0].effective_latency_ms == Decimal("6000")


def test_plan_does_not_apply_success_rate_hard_gate() -> None:
    plan = plan_provider_pins(
        [target(5, 14020, 45895)],
        [
            quote(
                observer,
                900,
                "0.010",
                success_rate=100,
                latency_ms="2000",
                ttft_ms="1000",
                health_state="healthy",
                failure_responsibility="provider",
                provider_failures=2,
            )
            for observer in (1, 2, 3)
        ],
        model="gpt-5.6-sol",
        owned_channel_ids=OWNED,
        minimum_success_rate=9000,
        maximum_latency_ms="10000",
    )

    assert plan.candidates[0].channel_id == 900
    assert plan.candidates[0].median_success_rate == 100
    assert plan.candidates[0].success_score == 0


def test_plan_does_not_apply_success_hard_gate_to_client_failures() -> None:
    plan = plan_provider_pins(
        [target(5, 14020, 45895)],
        [
            quote(
                observer,
                900,
                "0.010",
                success_rate=7300,
                latency_ms="2000",
                health_state="healthy",
                failure_responsibility="client",
                client_failures=3,
            )
            for observer in (1, 2, 3)
        ],
        model="gpt-5.6-sol",
        owned_channel_ids=OWNED,
        minimum_success_rate=9000,
        maximum_latency_ms="10000",
    )

    assert plan.candidates[0].channel_id == 900
    assert plan.candidates[0].success_score == 0
    assert plan.candidates[0].recent_client_failure_count == 3


def test_latency_score_winsorizes_single_extreme_outlier() -> None:
    latencies = (1000, 2000, 3000, 4000, 5000, 6000, 7000, 8000, 10000, 120000)
    plan = plan_provider_pins(
        [target(5, 14020, 45895)],
        [
            quote(
                observer,
                900 + index,
                "0.010",
                success_rate=10000,
                latency_ms=str(latency),
                ttft_ms=str(latency // 2),
                health_state="healthy",
            )
            for index, latency in enumerate(latencies)
            for observer in (1, 2)
        ],
        model="gpt-5.6-sol",
        owned_channel_ids=OWNED,
        top_k=10,
    )

    by_channel = {item.channel_id: item for item in plan.ranked_candidates}
    assert by_channel[908].latency_score > Decimal("0.4")
    assert by_channel[909].latency_score == Decimal(1)


def test_pin_hysteresis_holds_then_switches_by_priority_after_minimum_age() -> None:
    current = PinCandidate(
        900, 1900, "gpt-5.6-sol", 3, Decimal("0.011"), 11, 66, composite_score=Decimal("0.20")
    )
    proposed = PinCandidate(
        901, 1901, "gpt-5.6-sol", 3, Decimal("0.010"), 10, 60, composite_score=Decimal("0.10")
    )

    held = apply_pin_hysteresis(
        900,
        proposed,
        (proposed, current),
        current_age_seconds=20,
        minimum_hold_seconds=60,
        minimum_switch_gain="0.05",
    )
    switched = apply_pin_hysteresis(
        900,
        proposed,
        (proposed, current),
        current_age_seconds=60,
        minimum_hold_seconds=60,
        minimum_switch_gain="0.05",
    )

    assert held.action == "hold_minimum_duration"
    assert held.candidate.channel_id == 900
    assert held.hold_remaining_seconds == 40
    assert held.expected_switch_gain == Decimal("0.09090909090909090909090909091")
    assert switched.action == "switch_priority"
    assert switched.candidate.channel_id == 901


def test_pin_hysteresis_ignores_composite_gain_but_emergency_bypasses_hold() -> None:
    current = PinCandidate(
        900, 1900, "gpt-5.6-sol", 3, Decimal("0.011"), 11, 66, composite_score=Decimal("0.13")
    )
    proposed = PinCandidate(
        901, 1901, "gpt-5.6-sol", 3, Decimal("0.010"), 10, 60, composite_score=Decimal("0.10")
    )

    held = apply_pin_hysteresis(
        900,
        proposed,
        (proposed, current),
        current_age_seconds=120,
        minimum_hold_seconds=60,
        minimum_switch_gain="0.99",
    )
    bypassed = apply_pin_hysteresis(
        900,
        proposed,
        (proposed, current),
        current_age_seconds=1,
        minimum_hold_seconds=60,
        minimum_switch_gain="0.05",
        bypass=True,
    )

    assert held.action == "switch_priority"
    assert held.candidate.channel_id == 901
    assert bypassed.action == "switch_bypass"
    assert bypassed.candidate.channel_id == 901


def test_plan_excludes_cooling_quotes_and_validates_weights() -> None:
    with pytest.raises(PinControlError, match="pin_consensus_candidates_empty"):
        plan_provider_pins(
            [target(5, 14020, 45895)],
            [quote(observer, 900, "0.02", health_state="cooling") for observer in (1, 2)],
            model="gpt-5.6-sol",
            owned_channel_ids=OWNED,
        )

    with pytest.raises(PinControlError, match="pin_weights_invalid"):
        plan_provider_pins(
            [target(5, 14020, 45895)],
            [quote(observer, 901, "0.03") for observer in (1, 2)],
            model="gpt-5.6-sol",
            owned_channel_ids=OWNED,
            price_weight="0.8",
            success_weight="0.15",
            latency_weight="0.25",
        )


def test_weighted_multiplier_uses_one_hour_usage_and_equal_fallback() -> None:
    plan = plan_provider_pins(
        [target(5, 14020, 45895), target(19, 14093, 45896)],
        [
            quote(1, 900, "0.02"),
            quote(2, 900, "0.02"),
            quote(1, 901, "0.04"),
            quote(2, 901, "0.04"),
        ],
        model="gpt-5.6-sol",
        owned_channel_ids=OWNED,
        top_k=2,
    )

    assert weighted_pin_multiplier(plan.assignments, {900: 3, 901: 1}) == Decimal("0.025")
    assert weighted_pin_multiplier(plan.assignments, {}) == Decimal("0.03")


class FakeA6:
    def __init__(self, observed_channel: int) -> None:
        self.observed_channel = observed_channel
        self.calls = []

    def marketplace_pin(self, user_id, **kwargs):
        self.calls.append(("pin", user_id, kwargs))
        return {}

    def marketplace_pins(self, user_id):
        self.calls.append(("read", user_id))
        return [
            {
                "token_id": 14020,
                "model_name": "gpt-5.6-sol",
                "channel_id": self.observed_channel,
            }
        ]


class FakeAdmin:
    def __init__(self) -> None:
        self.calls = []

    def set_schedulable(self, account_id, schedulable):
        self.calls.append((account_id, schedulable))
        return {"id": account_id, "schedulable": schedulable}


class FakeAudit:
    def __init__(self) -> None:
        self.calls = []

    def prepare(self, assignment, *, config_version):
        self.calls.append(("prepare", assignment.candidate.channel_id, config_version))
        return "action-1"

    def complete(self, action_ref, assignment, observed_pin):
        self.calls.append(("complete", action_ref, observed_pin["channel_id"]))

    def fail(self, action_ref, *, error_class):
        self.calls.append(("fail", action_ref, error_class))


def assignment(channel_id: int = 900) -> PinAssignment:
    return PinAssignment(
        target=target(5, 14020, 45895),
        candidate=PinCandidate(
            channel_id=channel_id,
            listing_id=1900,
            model="gpt-5.6-sol",
            observer_count=2,
            median_multiplier=Decimal("0.03"),
            median_input_price_micros=30_000,
            median_output_price_micros=240_000,
        ),
    )


def test_executor_predrains_writes_reads_back_and_stays_drained_for_probe() -> None:
    a6 = FakeA6(900)
    admin = FakeAdmin()
    audit = FakeAudit()

    result = ProviderPinExecutor(a6, admin, audit_store=audit).execute(
        assignment(),
        owned_channel_ids=OWNED,
        config_version="trial-v1",
        apply=True,
    )

    assert result.confirmed is True
    assert admin.calls == [(45895, False)]
    assert [call[0] for call in a6.calls] == ["pin", "read"]
    assert audit.calls == [
        ("prepare", 900, "trial-v1"),
        ("complete", "action-1", 900),
    ]


def test_executor_keeps_disabled_when_readback_is_owned() -> None:
    a6 = FakeA6(768)
    admin = FakeAdmin()
    audit = FakeAudit()

    with pytest.raises(PinControlError, match="pin_readback_owned_channel"):
        ProviderPinExecutor(a6, admin, audit_store=audit).execute(
            assignment(),
            owned_channel_ids=OWNED,
            config_version="trial-v1",
            apply=True,
        )

    assert admin.calls == [(45895, False)]
    assert audit.calls[-1] == ("fail", "action-1", "pin_readback_owned_channel")


def test_executor_dry_run_has_no_side_effects_and_rejects_owned_target() -> None:
    a6 = FakeA6(900)
    admin = FakeAdmin()
    result = ProviderPinExecutor(a6, admin).execute(
        assignment(),
        owned_channel_ids=OWNED,
        config_version="trial-v1",
    )
    assert result.applied is False
    assert a6.calls == []
    assert admin.calls == []

    with pytest.raises(PinControlError, match="pin_owned_channel_selected"):
        ProviderPinExecutor(a6, admin).execute(
            assignment(768),
            owned_channel_ids=OWNED,
            config_version="trial-v1",
        )
