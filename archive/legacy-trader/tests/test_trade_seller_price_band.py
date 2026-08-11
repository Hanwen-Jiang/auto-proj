from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

import a6api_auto.trade_seller_price_band as price_band_module
from a6api_auto.trade_seller_orchestration import (
    GPT_5_6_SOL_OFFICIAL_PRICES,
    SellerMerchantResourceResult,
)
from a6api_auto.trade_seller_price_band import (
    ROLLOUT_BATCH_CONFIRMATION,
    PostgresSellerPriceBandStore,
    SellerAppliedPriceState,
    SellerMarketQuoteEvidence,
    SellerPinEvidence,
    SellerPriceAuditRef,
    SellerPriceBandError,
    SellerPriceBandOrchestrator,
    SellerPriceBandRequest,
    SellerPriceBandSnapshot,
    SellerPriceJumpControl,
    SellerProviderExclusion,
    build_seller_price_band_plan,
)

NOW = datetime(2026, 7, 25, 8, 0, tzinfo=timezone.utc)


def pin(
    *,
    binding_id: int = 101,
    token_ref_id: int = 201,
    channel_id: int = 301,
    ratio: Decimal | None = Decimal("0.10"),
    usage: int = 0,
) -> SellerPinEvidence:
    return SellerPinEvidence(
        binding_id=binding_id,
        token_ref_id=token_ref_id,
        model="gpt-5.6-sol",
        channel_id=channel_id,
        fallback_to_smart_routing=False,
        valid_from=NOW - timedelta(hours=2),
        valid_to=None,
        confirmed_at=NOW - timedelta(hours=1),
        decision_id=401 + binding_id,
        decision_token_ref_id=token_ref_id,
        decision_model="gpt-5.6-sol",
        selected_channel_id=channel_id,
        decision_status="completed",
        quote_id=501 + binding_id,
        scan_run_id=601 + binding_id,
        quote_channel_id=channel_id,
        quote_model="gpt-5.6-sol",
        realtime_ratio_usd=ratio,
        quote_is_owned=False,
        quote_observed_at=NOW - timedelta(seconds=15),
        scan_status="completed",
        owned_channel=False,
        usage_demand_units=usage,
    )


def snapshot(*pins: SellerPinEvidence) -> SellerPriceBandSnapshot:
    return SellerPriceBandSnapshot(
        model="gpt-5.6-sol",
        pins=tuple(pins or (pin(),)),
        evaluated_at=NOW,
    )


def market_quote(
    channel_id: int,
    ratio: str,
    *,
    observers: int = 3,
    age_seconds: int = 10,
    owned: bool = False,
) -> SellerMarketQuoteEvidence:
    return SellerMarketQuoteEvidence(
        channel_id=channel_id,
        realtime_ratio_usd=Decimal(ratio),
        observer_count=observers,
        observed_at=NOW - timedelta(seconds=age_seconds),
        is_owned=owned,
    )


def request() -> SellerPriceBandRequest:
    return SellerPriceBandRequest(
        merchant_ids=tuple(range(19, 0, -1)),
        group_id=26,
        listing_descriptions={
            merchant_id: f"Team 席位 {merchant_id} 来源" for merchant_id in range(1, 20)
        },
    )


def final_revenue_floor(plan) -> Decimal:
    return (
        (Decimal("1") + plan.request.margin)
        * plan.cost_multiplier
        / plan.request.conversion_factor
        * (Decimal("1") + plan.request.headroom)
    )


def successful_result(merchant_id: int) -> SellerMerchantResourceResult:
    return SellerMerchantResourceResult(
        merchant_id=merchant_id,
        a6_user_id=9_000 + merchant_id,
        roles=("seller", "observer") if merchant_id == 1 else ("seller",),
        seller={
            "action": "noop",
            "state": "bound",
            "verified": True,
            "applied": False,
        },
        channel={"action": "noop", "state": "verified", "applied": False},
        listing={"action": "update", "state": "verified", "applied": True},
    )


def test_audit_marks_confirmed_delta_with_blocked_listing_as_failed() -> None:
    result = SellerMerchantResourceResult(
        merchant_id=4,
        a6_user_id=7775,
        roles=("seller", "provider"),
        seller={
            "action": "update_delta",
            "state": "bound",
            "verified": True,
            "applied": True,
        },
        channel={"action": "blocked", "state": "blocked", "applied": False},
        listing={"action": "blocked", "state": "blocked", "applied": False},
        error_class="a6_transport_failed",
    )

    status, error, _observed = price_band_module._audit_outcome(result)

    assert status == "failed"
    assert error == "a6_transport_failed"


@pytest.mark.parametrize(
    ("seller", "channel", "listing"),
    (
        (
            {
                "action": "update_delta",
                "state": "update_unknown",
                "verified": False,
                "applied": True,
            },
            {"action": "blocked", "state": "blocked", "applied": False},
            {"action": "blocked", "state": "blocked", "applied": False},
        ),
        (
            {"action": "noop", "state": "bound", "verified": True, "applied": False},
            {"action": "noop", "state": "verified", "applied": False},
            {"action": "update", "state": "update_unknown", "applied": True},
        ),
    ),
)
def test_audit_keeps_unconfirmed_remote_mutation_unknown(seller, channel, listing) -> None:
    result = SellerMerchantResourceResult(
        merchant_id=4,
        a6_user_id=7775,
        roles=("seller", "provider"),
        seller=seller,
        channel=channel,
        listing=listing,
        error_class="a6_transport_failed",
    )

    status, error, _observed = price_band_module._audit_outcome(result)

    assert status == "unknown"
    assert error == "a6_transport_failed"


def test_nineteen_assignments_cover_segmented_dense_low_price_band() -> None:
    plan = build_seller_price_band_plan(request(), snapshot(), now=NOW)

    assert [item.merchant_id for item in plan.assignments] == list(range(1, 20))
    assert plan.public()["tier_counts"] == {
        "balanced": 4,
        "ceiling": 1,
        "floor": 4,
        "profit_floor": 2,
        "profit_mid": 1,
        "quality": 3,
        "value": 4,
    }
    assert plan.public()["price_zone_counts"] == {
        "high": 3,
        "low": 10,
        "middle": 6,
    }
    distribution = plan.public()["distribution"]
    assert distribution["strategy"] == "market_channel_density_log_histogram_v3"
    assert distribution["mode"] == "fallback_segmented_density"
    assert distribution["eligible_market_channel_count"] == 0
    assert distribution["fallback"] == {
        "reason": "insufficient_market_channels",
        "strategy": "segmented_panel_log_density_v2",
        "segments": [
            {"zone": "low", "point_share": "0.55", "log_span_share": "0.30"},
            {
                "zone": "middle",
                "point_share": "0.30",
                "log_span_share": "0.40",
            },
            {"zone": "high", "point_share": "0.15", "log_span_share": "0.30"},
        ],
    }
    finals = [item.pricing.final_multiplier for item in plan.assignments]
    panels = [item.pricing.price_multiplier for item in plan.assignments]
    deltas = [item.pricing.delta for item in plan.assignments]
    profit_floor = final_revenue_floor(plan)
    assert finals[0] == profit_floor
    assert finals[-1] == Decimal("1")
    assert all(left < right for left, right in zip(panels, panels[1:], strict=False))
    assert all(value <= Decimal("1") for value in panels)
    assert all(value >= profit_floor for value in finals)
    assert deltas[0] == Decimal("0.500000")
    assert sum(value > 0 for value in deltas) == 6
    assert deltas[6:] == [Decimal("0")] * 13
    assert all(left >= right for left, right in zip(deltas, deltas[1:], strict=False))
    assert panels[0] == profit_floor / Decimal("1.5")
    assert all(
        abs(final - profit_floor) < Decimal("0.000001")
        for final, delta in zip(finals, deltas, strict=True)
        if delta > 0
    )
    assert all(
        abs(final - panel) < Decimal("1e-27")
        for panel, final, delta in zip(panels, finals, deltas, strict=True)
        if delta == 0
    )
    positions = [item.log_position for item in plan.assignments]
    positions_by_zone = {
        zone: [item.log_position for item in plan.assignments if item.price_zone == zone]
        for zone in ("low", "middle", "high")
    }
    steps_by_zone = {
        zone: [right - left for left, right in zip(values, values[1:], strict=False)]
        for zone, values in positions_by_zone.items()
    }
    low_steps = steps_by_zone["low"]
    middle_steps = steps_by_zone["middle"]
    high_steps = steps_by_zone["high"]
    assert positions[0] == Decimal("0")
    assert positions[-1] == Decimal("1")
    assert positions_by_zone["low"][-1] < Decimal("0.30")
    assert Decimal("0.30") < positions_by_zone["middle"][0]
    assert positions_by_zone["middle"][-1] < Decimal("0.70")
    assert Decimal("0.70") < positions_by_zone["high"][0]
    assert all(max(steps) - min(steps) < Decimal("1e-25") for steps in steps_by_zone.values())
    assert max(low_steps) < min(middle_steps)
    assert max(middle_steps) < min(high_steps)
    assert panels[1] - panels[0] < panels[-1] - panels[-2]
    assert len({item.listing_managed_uid for item in plan.assignments}) == 19
    price_points = [item.pricing.prices for item in plan.assignments]
    assert len(set(price_points)) == 19
    assert all(
        all(left < right for left, right in zip(component, component[1:], strict=False))
        for component in zip(*price_points, strict=True)
    )
    assert plan.public()["coverage"] == {
        "minimum_final_multiplier": format(finals[0], "f"),
        "maximum_final_multiplier": "1",
        "final_range_ratio": format(finals[-1] / finals[0], "f"),
        "minimum_panel_multiplier": format(panels[0], "f"),
        "maximum_panel_multiplier": format(panels[-1].normalize(), "f"),
        "delta_zero_panel_multiplier": format(profit_floor, "f"),
        "positive_delta_points": 6,
        "zero_delta_points": 13,
        "distinct_price_points": 19,
        "listing_targets": 19,
    }

    table = plan.public()["assignments"]
    assert table[0]["merchant_id"] == 1
    assert table[0]["target_final_multiplier"] == format(finals[0], "f")
    assert table[0]["a6_panel_multiplier"] == format(panels[0], "f")
    assert table[0]["delta"] == "0.500000"


def test_live_market_density_allocates_listing_slots_by_channel_count() -> None:
    density_snapshot = replace(
        snapshot(),
        market_quotes=(
            *(market_quote(900 + index, "0.10") for index in range(8)),
            *(market_quote(920 + index, "0.17") for index in range(2)),
            market_quote(930, "0.50"),
        ),
    )

    plan = build_seller_price_band_plan(request(), density_snapshot, now=NOW)
    distribution = plan.public()["distribution"]

    assert distribution["mode"] == "live_market_density"
    assert distribution["eligible_market_channel_count"] == 11
    assert distribution["allocatable_market_channel_count"] == 11
    assert distribution["coverage_slots"] == 4
    assert distribution["density_slots"] == 15
    assert [item["market_channel_count"] for item in distribution["buckets"]] == [
        8,
        2,
        0,
        0,
        0,
        1,
        0,
        0,
    ]
    assert [item["listing_count"] for item in distribution["buckets"]] == [
        12,
        3,
        1,
        0,
        0,
        2,
        0,
        1,
    ]
    assert sum(item["listing_count"] for item in distribution["buckets"]) == 19
    assert plan.assignments[0].log_position == Decimal("0")
    assert plan.assignments[-1].log_position == Decimal("1")
    assert all(
        left.pricing.price_multiplier < right.pricing.price_multiplier
        for left, right in zip(plan.assignments, plan.assignments[1:], strict=False)
    )
    assert all(
        item.pricing.final_multiplier
        >= Decimal("1.2") * plan.cost_multiplier / plan.request.conversion_factor
        for item in plan.assignments
    )


def test_single_merchant_pilot_uses_max_delta_at_the_profit_floor() -> None:
    single = replace(
        request(),
        merchant_ids=(1,),
        listing_descriptions={1: "Team 订阅来源"},
    )

    plan = build_seller_price_band_plan(single, snapshot(), now=NOW)

    assert plan.assignments[0].tier == "floor"
    assert plan.assignments[0].price_zone == "low"
    assert plan.assignments[0].pricing.delta == Decimal("0.500000")


def test_maximum_band_utilization_leaves_room_below_the_hard_ceiling() -> None:
    limited = replace(
        request(),
        maximum_price_multiplier=Decimal("0.4"),
        maximum_final_multiplier=Decimal("0.4"),
        maximum_band_utilization=Decimal("0.75"),
    )

    plan = build_seller_price_band_plan(limited, snapshot(), now=NOW)

    assert plan.request.maximum_final_multiplier == Decimal("0.4")
    assert plan.public()["maximum_band_utilization"] == "0.75"
    assert plan.assignments[-1].pricing.final_multiplier == Decimal("0.300")
    assert plan.public()["coverage"]["maximum_final_multiplier"] == "0.300"
    assert all(
        item.pricing.final_multiplier
        >= Decimal("1.2") * plan.cost_multiplier / plan.request.conversion_factor
        for item in plan.assignments
    )


def test_panel_and_final_caps_are_checked_in_their_own_coordinates() -> None:
    limited = replace(
        request(),
        maximum_price_multiplier=Decimal("0.12"),
        maximum_final_multiplier=Decimal("0.18"),
    )

    plan = build_seller_price_band_plan(limited, snapshot(), now=NOW)

    profit_floor = final_revenue_floor(plan)
    assert plan.assignments[0].pricing.price_multiplier == profit_floor / Decimal("1.5")
    assert plan.assignments[0].pricing.delta == Decimal("0.500000")
    assert plan.assignments[-1].pricing.price_multiplier == Decimal("0.12")
    assert plan.assignments[-1].pricing.delta == Decimal("0.200001")
    assert all(
        item.pricing.price_multiplier <= limited.maximum_price_multiplier
        for item in plan.assignments
    )
    assert all(
        item.pricing.final_multiplier <= limited.maximum_final_multiplier
        for item in plan.assignments
    )
    assert all(
        profit_floor <= item.pricing.final_multiplier < profit_floor + Decimal("0.000001")
        for item in plan.assignments
    )


def test_prices_use_live_sol_create_payload_and_readback_units() -> None:
    plan = build_seller_price_band_plan(request(), snapshot(), now=NOW)
    assignment = plan.assignments[0]
    input_price = assignment.pricing.prices[0]
    expected = (
        input_price,
        input_price * 6,
        (input_price + 5) // 10,
        (input_price * 5 + 2) // 4,
    )

    assert GPT_5_6_SOL_OFFICIAL_PRICES == (
        5_000_000,
        30_000_000,
        500_000,
        6_250_000,
    )
    assert assignment.pricing.prices == expected


def test_cost_multiplier_uses_one_hour_official_demand_weights_and_equal_fallback() -> None:
    weighted = build_seller_price_band_plan(
        request(),
        snapshot(
            pin(binding_id=1, token_ref_id=11, channel_id=21, ratio=Decimal("0.1"), usage=1),
            pin(binding_id=2, token_ref_id=12, channel_id=22, ratio=Decimal("0.3"), usage=3),
        ),
        now=NOW,
    )
    equal = build_seller_price_band_plan(
        request(),
        snapshot(
            pin(binding_id=1, token_ref_id=11, channel_id=21, ratio=Decimal("0.1")),
            pin(binding_id=2, token_ref_id=12, channel_id=22, ratio=Decimal("0.3")),
        ),
        now=NOW,
    )

    assert weighted.cost_multiplier == Decimal("0.25")
    assert weighted.weight_mode == "official_demand_1h"
    assert equal.cost_multiplier == Decimal("0.2")
    assert equal.weight_mode == "equal_fallback"


def test_delta_floor_exactly_preserves_twenty_percent_profit() -> None:
    pilot_request = replace(
        request(),
        merchant_ids=(1,),
        listing_descriptions={1: "Team 满血池来源"},
    )
    weighted_snapshot = SellerPriceBandSnapshot(
        model="gpt-5.6-sol",
        pins=(
            pin(binding_id=1, token_ref_id=11, channel_id=21, ratio=Decimal("0.04385"), usage=6),
            pin(binding_id=2, token_ref_id=12, channel_id=22, ratio=Decimal("0.04385"), usage=2),
            pin(binding_id=3, token_ref_id=13, channel_id=23, ratio=Decimal("0.04404"), usage=2),
        ),
        evaluated_at=NOW,
    )

    plan = build_seller_price_band_plan(pilot_request, weighted_snapshot, now=NOW)
    pricing = plan.assignments[0].pricing

    assert plan.cost_multiplier == Decimal("0.043888")
    assert pricing.delta == Decimal("0.500000")
    assert pricing.price_multiplier.quantize(Decimal("0.00000001")) == Decimal("0.04213248")
    assert pricing.final_multiplier.quantize(Decimal("0.00000001")) == Decimal("0.06319872")
    assert pricing.market_multiplier.quantize(Decimal("0.00000001")) == Decimal("0.05055898")
    assert pricing.final_multiplier >= Decimal("1.2") * pricing.landed_cost_multiplier


def test_delta_decays_to_zero_while_final_profit_stays_at_the_floor() -> None:
    plan = build_seller_price_band_plan(request(), snapshot(), now=NOW)
    profit_floor = final_revenue_floor(plan)
    floor_band = [
        item for item in plan.assignments if item.pricing.price_multiplier <= profit_floor
    ]
    profit_band = [
        item for item in plan.assignments if item.pricing.price_multiplier > profit_floor
    ]

    assert floor_band
    assert floor_band[0].pricing.delta == Decimal("0.500000")
    assert all(item.pricing.delta > 0 for item in floor_band[:-1])
    assert all(item.pricing.delta == 0 for item in profit_band)
    assert all(item.pricing.final_multiplier >= profit_floor for item in plan.assignments)
    assert all(
        left.pricing.delta >= right.pricing.delta
        for left, right in zip(plan.assignments, plan.assignments[1:], strict=False)
    )


def test_isolated_market_low_is_guarded_between_top1_and_top2_near_top2() -> None:
    guarded_snapshot = replace(
        snapshot(pin(ratio=Decimal("0.010"))),
        market_quotes=(
            market_quote(901, "0.010"),
            market_quote(902, "0.020"),
            market_quote(903, "0.021"),
        ),
    )

    plan = build_seller_price_band_plan(request(), guarded_snapshot, now=NOW)
    guard = plan.price_floor_guard

    assert guard.status == "isolated_low_price_guard"
    assert guard.activated is True
    assert guard.top1_gap_ratio == Decimal("1")
    assert guard.neighbor_count == 1
    assert guard.guarded_market_multiplier == Decimal("0.01750")
    assert plan.assignments[0].pricing.market_multiplier == Decimal("0.01750")
    assert plan.assignments[0].pricing.delta == Decimal("0")
    assert (
        plan.assignments[0].pricing.final_multiplier
        >= Decimal("0.012") / plan.request.conversion_factor
    )


def test_guarded_floor_uses_partial_delta_to_hold_twenty_percent_profit() -> None:
    guarded_request = replace(request(), conversion_factor=Decimal("1"))
    guarded_snapshot = replace(
        snapshot(pin(ratio=Decimal("0.010"))),
        market_quotes=(
            market_quote(901, "0.008"),
            market_quote(902, "0.012"),
            market_quote(903, "0.0121"),
        ),
    )

    plan = build_seller_price_band_plan(guarded_request, guarded_snapshot, now=NOW)
    pricing = plan.assignments[0].pricing

    assert plan.price_floor_guard.guarded_market_multiplier == Decimal("0.01100")
    assert pricing.price_multiplier == Decimal("0.01100")
    assert pricing.delta == Decimal("0.090910")
    assert pricing.final_multiplier >= Decimal("0.012")
    assert pricing.final_multiplier < Decimal("0.0120001")


def test_dense_low_price_cluster_keeps_profit_floor_without_guard_markup() -> None:
    dense_snapshot = replace(
        snapshot(pin(ratio=Decimal("0.010"))),
        market_quotes=(
            market_quote(901, "0.0100"),
            market_quote(902, "0.0101"),
            market_quote(903, "0.0102"),
        ),
    )

    plan = build_seller_price_band_plan(request(), dense_snapshot, now=NOW)

    assert plan.price_floor_guard.status == "dense_low_price_cluster"
    assert plan.price_floor_guard.activated is False
    assert plan.price_floor_guard.neighbor_count == 3
    profit_floor = final_revenue_floor(plan)
    assert plan.assignments[0].pricing.final_multiplier == profit_floor
    assert plan.assignments[0].pricing.price_multiplier == profit_floor / Decimal("1.5")
    assert plan.assignments[0].pricing.delta == Decimal("0.500000")


def test_request_rejects_sub_twenty_percent_margin() -> None:
    with pytest.raises(SellerPriceBandError, match="seller_price_band_margin_invalid"):
        replace(request(), margin=Decimal("0.199999"))


def test_audit_desired_state_proves_weighted_pin_profit_gate() -> None:
    plan = build_seller_price_band_plan(request(), snapshot(), now=NOW)
    desired = plan.audit_desired_state(plan.assignments[0])
    gate = desired["profit_gate"]

    assert "settlement_fee_rate" not in desired
    assert gate["passed"] is True
    assert Decimal(gate["final_revenue_multiplier"]) >= Decimal(
        gate["required_final_revenue_multiplier"]
    )
    assert Decimal(gate["cost_markup"]) >= Decimal("0.20")


@pytest.mark.parametrize(
    "broken,error",
    [
        (lambda value: replace(value, owned_channel=True), "seller_price_band_owned_pin"),
        (
            lambda value: replace(value, quote_is_owned=True),
            "seller_price_band_owned_pin",
        ),
        (
            lambda value: replace(value, decision_status="applying"),
            "seller_price_band_decision_incomplete",
        ),
        (
            lambda value: replace(value, realtime_ratio_usd=None),
            "seller_price_band_usd_quote_missing",
        ),
        (
            lambda value: replace(value, quote_observed_at=NOW - timedelta(seconds=301)),
            "seller_price_band_quote_stale",
        ),
        (
            lambda value: replace(value, quote_observed_at=NOW + timedelta(microseconds=1)),
            "seller_price_band_quote_future",
        ),
        (
            lambda value: replace(value, fallback_to_smart_routing=True),
            "seller_price_band_smart_fallback",
        ),
        (
            lambda value: replace(value, scan_status="running"),
            "seller_price_band_scan_incomplete",
        ),
    ],
)
def test_snapshot_evidence_failures_close_the_entire_plan(broken, error) -> None:
    with pytest.raises(SellerPriceBandError, match=error):
        build_seller_price_band_plan(request(), snapshot(broken(pin())), now=NOW)


def test_snapshot_accepts_binding_that_was_current_at_evaluation_anchor() -> None:
    historical = replace(pin(), valid_to=NOW + timedelta(microseconds=1))

    plan = build_seller_price_band_plan(request(), snapshot(historical), now=NOW)

    assert plan.cost_multiplier == Decimal("0.10")


def test_snapshot_rejects_binding_that_ended_at_evaluation_anchor() -> None:
    historical = replace(pin(), valid_to=NOW)

    with pytest.raises(
        SellerPriceBandError,
        match="seller_price_band_binding_not_current",
    ):
        build_seller_price_band_plan(request(), snapshot(historical), now=NOW)


def test_active_provider_projection_replaces_legacy_pin_decision_gate() -> None:
    projected = replace(
        pin(),
        valid_to=NOW - timedelta(minutes=5),
        decision_id=None,
        decision_token_ref_id=None,
        decision_model=None,
        selected_channel_id=None,
        decision_status=None,
        provider_generation_id=7001,
        provider_routing_generation=7319,
        provider_cost_generation=7319,
        provider_generation_state="active",
        provider_generation_activated_at=NOW - timedelta(minutes=30),
        provider_slot_no=4,
        provider_slot_state="READY",
        provider_slot_token_ref_id=201,
        provider_slot_channel_id=301,
        provider_confirmed_pin_binding_id=101,
        provider_pin_confirmed=True,
        provider_probe_confirmed=True,
        provider_desired_schedulable=True,
        provider_observed_schedulable=True,
    )

    plan = build_seller_price_band_plan(request(), snapshot(projected), now=NOW)

    assert plan.cost_multiplier == Decimal("0.10")
    assert plan.snapshot.pins[0].provider_routing_generation == 7319


def test_active_provider_projection_identity_mismatch_fails_closed() -> None:
    projected = replace(
        pin(),
        provider_generation_id=7001,
        provider_routing_generation=7319,
        provider_cost_generation=7319,
        provider_generation_state="active",
        provider_generation_activated_at=NOW - timedelta(minutes=30),
        provider_slot_no=4,
        provider_slot_state="READY",
        provider_slot_token_ref_id=201,
        provider_slot_channel_id=301,
        provider_confirmed_pin_binding_id=999,
        provider_pin_confirmed=True,
        provider_probe_confirmed=True,
        provider_desired_schedulable=True,
        provider_observed_schedulable=True,
    )

    with pytest.raises(
        SellerPriceBandError,
        match="seller_price_band_provider_projection_incomplete",
    ):
        build_seller_price_band_plan(request(), snapshot(projected), now=NOW)


def test_active_auto_pool_projection_uses_generation_usd_cap_as_cost() -> None:
    projected = replace(
        pin(ratio=Decimal("0.012"), usage=75),
        channel_id=None,
        valid_from=NOW - timedelta(minutes=10),
        confirmed_at=NOW - timedelta(minutes=11),
        decision_id=None,
        decision_token_ref_id=None,
        decision_model=None,
        selected_channel_id=None,
        decision_status=None,
        quote_id=None,
        scan_run_id=None,
        quote_channel_id=None,
        quote_model=None,
        quote_is_owned=False,
        quote_observed_at=NOW - timedelta(hours=2),
        scan_status=None,
        provider_generation_id=7001,
        provider_routing_generation=7319,
        provider_cost_generation=7319,
        provider_generation_state="active",
        provider_generation_activated_at=NOW - timedelta(hours=1),
        provider_slot_no=4,
        provider_slot_state="READY",
        provider_slot_token_ref_id=201,
        provider_slot_channel_id=None,
        provider_confirmed_pin_binding_id=None,
        provider_pin_confirmed=False,
        provider_probe_confirmed=False,
        provider_desired_schedulable=True,
        provider_observed_schedulable=True,
        provider_route_mode="auto_pool",
        provider_auto_pool_generation_id=8001,
        provider_auto_pool_subscription_id=26816,
        provider_auto_pool_confirmed=True,
        provider_auto_pool_generation_state="active",
        provider_auto_pool_activated_at=NOW - timedelta(minutes=10),
        provider_auto_pool_observed_at=NOW - timedelta(minutes=12),
    )

    plan = build_seller_price_band_plan(request(), snapshot(projected), now=NOW)

    assert plan.cost_multiplier == Decimal("0.012")
    assert plan.weight_mode == "official_demand_1h"
    assert plan.snapshot.pins[0].provider_route_mode == "auto_pool"


def test_auto_pool_projection_requires_confirmed_owned_channel_ban() -> None:
    projected = replace(
        pin(ratio=Decimal("0.012")),
        channel_id=None,
        valid_from=NOW - timedelta(minutes=10),
        confirmed_at=NOW - timedelta(minutes=11),
        decision_id=None,
        decision_token_ref_id=None,
        decision_model=None,
        selected_channel_id=None,
        decision_status=None,
        quote_id=None,
        scan_run_id=None,
        quote_channel_id=None,
        quote_model=None,
        quote_is_owned=False,
        scan_status=None,
        provider_generation_id=7001,
        provider_routing_generation=7319,
        provider_cost_generation=7319,
        provider_generation_state="active",
        provider_generation_activated_at=NOW - timedelta(hours=1),
        provider_slot_no=4,
        provider_slot_state="READY",
        provider_slot_token_ref_id=201,
        provider_slot_channel_id=None,
        provider_confirmed_pin_binding_id=None,
        provider_pin_confirmed=False,
        provider_probe_confirmed=False,
        provider_desired_schedulable=True,
        provider_observed_schedulable=True,
        provider_route_mode="auto_pool",
        provider_auto_pool_generation_id=8001,
        provider_auto_pool_subscription_id=26816,
        provider_auto_pool_confirmed=False,
        provider_auto_pool_generation_state="active",
        provider_auto_pool_activated_at=NOW - timedelta(minutes=10),
        provider_auto_pool_observed_at=NOW - timedelta(minutes=12),
    )

    with pytest.raises(
        SellerPriceBandError,
        match="seller_price_band_provider_projection_incomplete",
    ):
        build_seller_price_band_plan(request(), snapshot(projected), now=NOW)


def test_missing_pin_fails_closed() -> None:
    empty = SellerPriceBandSnapshot("gpt-5.6-sol", (), NOW)

    with pytest.raises(SellerPriceBandError, match="seller_price_band_pin_missing"):
        build_seller_price_band_plan(request(), empty, now=NOW)


def test_bound_provider_without_current_pin_is_reported_at_zero_weight() -> None:
    excluded = SellerProviderExclusion(
        merchant_id=19,
        token_ref_id=1901,
        external_token_id=14093,
        enodot_account_id=45896,
        reason="provider_balance_insufficient",
    )
    current = SellerPriceBandSnapshot(
        "gpt-5.6-sol",
        (pin(token_ref_id=501, channel_id=790),),
        NOW,
        (excluded,),
    )

    plan = build_seller_price_band_plan(request(), current, now=NOW)

    assert plan.cost_multiplier == Decimal("0.10")
    assert plan.public()["excluded_providers"] == [
        {
            "merchant_id": 19,
            "token_ref_id": 1901,
            "external_token_id": 14093,
            "enodot_account_id": 45896,
            "reason": "provider_balance_insufficient",
            "usage_quota_micros": 0,
        }
    ]


def test_provider_exclusion_row_uses_the_exclusion_quota_field() -> None:
    excluded = price_band_module._provider_exclusion_from_row(
        {
            "merchant_id": 19,
            "token_ref_id": 1901,
            "external_token_id": 14093,
            "enodot_account_id": 45916,
            "exclusion_reason": "current_pin_missing",
        }
    )

    assert excluded == SellerProviderExclusion(
        merchant_id=19,
        token_ref_id=1901,
        external_token_id=14093,
        enodot_account_id=45916,
        reason="current_pin_missing",
        usage_quota_micros=0,
    )


def test_snapshot_sql_uses_only_active_provider_generation_slots() -> None:
    pin_query = " ".join(price_band_module._SNAPSHOT_QUERY.split())
    auto_pool_query = " ".join(price_band_module._AUTO_POOL_SNAPSHOT_QUERY.split())
    market_query = " ".join(price_band_module._MARKET_FLOOR_QUERY.split())
    exclusion_query = " ".join(price_band_module._PROVIDER_EXCLUSIONS_QUERY.split())

    assert "JOIN a6_trade.a6_tokens token ON token.id = b.a6_token_id" in pin_query
    assert "FROM a6_trade.provider_pool_generations pool_generation" in pin_query
    assert "JOIN a6_trade.provider_pool_slots pool_slot" in pin_query
    assert "b.id = pool_slot.confirmed_pin_binding_id" in pin_query
    assert "pool_generation.generation_state = 'active'" in pin_query
    assert "pool_slot.slot_state = 'READY'" in pin_query
    assert "pool_slot.desired_schedulable" in pin_query
    assert "pool_slot.observed_schedulable" in pin_query
    assert "pool_slot.pin_confirmed" in pin_query
    assert "pool_slot.probe_confirmed" in pin_query
    assert "JOIN a6_trade.a6_merchants merchant" in pin_query
    assert "merchant.roles @> ARRAY['provider']::text[]" in pin_query
    assert "account.state = 'bound'" in pin_query
    assert "account.enodot_account_id IS NOT NULL" in pin_query
    assert "b.confirmed_at IS NOT NULL" in pin_query
    assert "provider_user_balance_insufficient_unfunded" in pin_query
    assert "coalesce(financial.withdrawable_micros, 0) < merchant.quota_per_unit" in pin_query
    assert "account.cooldown_until <= %s" in pin_query
    assert "pool_slot.valid_from <= %s" in pin_query
    assert "(pool_slot.valid_to IS NULL OR pool_slot.valid_to > %s)" in pin_query
    assert "b.valid_from <= %s" not in pin_query
    assert "FROM a6_trade.a6_market_quotes q" in pin_query
    assert "q.observed_at <= %s" in pin_query
    assert "q.created_at <= %s - interval '2 seconds'" in pin_query
    assert "scan.id = q.scan_run_id" in pin_query
    assert "coalesce(u.prompt_tokens, 0)::numeric * 5000000" in pin_query
    assert "coalesce(u.completion_tokens, 0)::numeric * 30000000" in pin_query
    assert "coalesce(u.cache_read_tokens, 0)::numeric * 500000" in pin_query
    assert "coalesce(u.cache_write_tokens, 0)::numeric * 6250000" in pin_query
    assert "sum(u.quota_micros)" not in pin_query
    assert "merchant.roles @> ARRAY['provider']::text[]" in exclusion_query
    assert "nullif(account.last_error_class, '') IS NOT NULL" in exclusion_query
    assert "current_pin.valid_from <= %s" in exclusion_query
    assert "(current_pin.valid_to IS NULL OR current_pin.valid_to > %s)" in exclusion_query
    assert "current_pin.confirmed_at IS NOT NULL" in exclusion_query
    assert "provider_user_balance_insufficient_unfunded" in exclusion_query
    assert "financial.withdrawable_micros" in exclusion_query
    assert "count(DISTINCT scan.observer_merchant_id)" in market_query
    assert "q.realtime_ratio_usd IS NOT NULL" in market_query
    assert "q.is_owned = false" in market_query
    assert "q.created_at <= %s - interval '2 seconds'" in market_query
    assert "scan.status = 'completed'" in market_query
    assert "pool_slot.route_mode = 'auto_pool'" in auto_pool_query
    assert "auto_generation.generation_state = 'active'" in auto_pool_query
    assert "auto_account.account_state = 'ready'" in auto_pool_query
    assert "auto_account.ban_confirmed" in auto_pool_query
    assert "auto_account.unpin_confirmed" in auto_pool_query
    assert "auto_account.auto_pool_confirmed" in auto_pool_query
    assert "auto_account.cost_confirmed" in auto_pool_query
    assert "auto_account.schedulable_confirmed" in auto_pool_query
    assert "auto_generation.max_ratio_usd AS realtime_ratio_usd" in auto_pool_query
    assert "pin.channel_id IS NULL AND auto_pool.subscription_id IS NULL" in exclusion_query


def _single_high_ceiling_request() -> SellerPriceBandRequest:
    return replace(
        request(),
        merchant_ids=(1,),
        listing_descriptions={1: "Team 订阅来源"},
        maximum_price_multiplier=Decimal("10"),
        maximum_final_multiplier=Decimal("10"),
        maximum_band_utilization=Decimal("0.75"),
    )


def _applied_price_state(plan, price_multiplier: Decimal) -> SellerAppliedPriceState:
    assignment = plan.assignments[0]
    return SellerAppliedPriceState(
        merchant_id=assignment.merchant_id,
        entity_id=assignment.listing_managed_uid,
        action_id=7001,
        config_version=plan.request.config_version,
        cost_multiplier=plan.cost_multiplier,
        final_multiplier=assignment.pricing.final_multiplier,
        price_multiplier=price_multiplier,
        delta=assignment.pricing.delta,
        completed_at=NOW - timedelta(minutes=5),
    )


def test_four_x_pin_cost_still_builds_theoretical_safe_price_for_jump_control() -> None:
    plan = build_seller_price_band_plan(
        _single_high_ceiling_request(),
        snapshot(pin(ratio=Decimal("4.0"))),
        now=NOW,
    )

    assignment = plan.assignments[0]
    assert plan.cost_multiplier == Decimal("4.0")
    assert assignment.pricing.price_multiplier <= Decimal("7.5")
    assert assignment.pricing.final_multiplier <= Decimal("7.5")
    assert plan.public()["profit_gate"]["passed"] is True


def test_price_jump_at_exactly_twice_previous_price_enters_30_to_45_minute_cooling() -> None:
    plan = build_seller_price_band_plan(
        _single_high_ceiling_request(),
        snapshot(pin(ratio=Decimal("0.20"))),
        now=NOW,
    )
    target = plan.assignments[0].pricing.price_multiplier

    control = price_band_module._price_jump_controls(
        plan,
        (_applied_price_state(plan, target / Decimal("2")),),
        {},
        now=NOW,
    )[0]

    assert control.action == "takedown"
    assert control.state == "cooling"
    assert control.target_price_multiplier == target
    assert control.baseline_price_multiplier == target / Decimal("2")
    assert control.cooling_until is not None
    assert 30 * 60 <= (control.cooling_until - NOW).total_seconds() <= 45 * 60


def test_price_jump_uses_lowest_controlled_listing_as_collective_baseline() -> None:
    plan = build_seller_price_band_plan(
        _single_high_ceiling_request(),
        snapshot(pin(ratio=Decimal("0.20"))),
        now=NOW,
    )
    target = plan.assignments[0].pricing.price_multiplier
    canonical = _applied_price_state(plan, target / Decimal("1.5"))
    lowest_controlled = target / Decimal("2")

    control = price_band_module._price_jump_controls(
        plan,
        (canonical,),
        {},
        controlled_baselines={1: lowest_controlled},
        now=NOW,
    )[0]

    assert control.action == "takedown"
    assert control.state == "cooling"
    assert control.baseline_price_multiplier == lowest_controlled


def test_price_jump_below_twice_previous_price_remains_immediately_applicable() -> None:
    plan = build_seller_price_band_plan(
        _single_high_ceiling_request(),
        snapshot(pin(ratio=Decimal("0.20"))),
        now=NOW,
    )
    target = plan.assignments[0].pricing.price_multiplier

    control = price_band_module._price_jump_controls(
        plan,
        (_applied_price_state(plan, target / Decimal("1.99")),),
        {},
        now=NOW,
    )[0]

    assert control.action == "apply"
    assert control.state == "normal"


def test_price_jump_persistence_uses_postgres_numeric_scale() -> None:
    assert price_band_module._price_jump_persisted_multiplier(
        Decimal("0.0851851553680696171312855287")
    ) == Decimal("0.085185155368")
    assert price_band_module._price_jump_persisted_multiplier(
        Decimal("0.9797334262169878983646542238")
    ) == Decimal("0.979733426217")


def test_price_jump_cooling_freezes_baseline_and_releases_on_recovery_or_expiry() -> None:
    request_value = _single_high_ceiling_request()
    high = build_seller_price_band_plan(
        request_value,
        snapshot(pin(ratio=Decimal("0.20"))),
        now=NOW,
    )
    baseline = Decimal("0.09")
    cooling = price_band_module._price_jump_controls(
        high,
        (_applied_price_state(high, baseline),),
        {},
        now=NOW,
    )[0]
    cooling = replace(cooling, cooldown_id=41)
    assert cooling.target_price_multiplier >= baseline * 2

    higher = build_seller_price_band_plan(
        request_value,
        snapshot(pin(ratio=Decimal("0.21"))),
        now=NOW,
    )
    still_cooling = price_band_module._price_jump_controls(
        higher,
        (_applied_price_state(higher, baseline),),
        {cooling.entity_id: cooling},
        now=NOW + timedelta(minutes=1),
    )[0]
    assert still_cooling.action == "takedown"
    assert still_cooling.baseline_price_multiplier == baseline

    recovered = build_seller_price_band_plan(
        request_value,
        snapshot(pin(ratio=Decimal("0.10"))),
        now=NOW,
    )
    recovery_release = price_band_module._price_jump_controls(
        recovered,
        (_applied_price_state(recovered, baseline),),
        {cooling.entity_id: cooling},
        now=NOW + timedelta(minutes=2),
    )[0]
    assert recovery_release.action == "release"
    assert recovery_release.release_reason == "cost_recovered"

    expiry_release = price_band_module._price_jump_controls(
        higher,
        (_applied_price_state(higher, baseline),),
        {cooling.entity_id: cooling},
        now=cooling.cooling_until,
    )[0]
    assert expiry_release.action == "release"
    assert expiry_release.release_reason == "cooldown_elapsed"


def test_cost_recovery_release_reverts_to_cooling_if_pin_cost_rebounds_before_apply() -> None:
    request_value = _single_high_ceiling_request()
    high = build_seller_price_band_plan(
        request_value,
        snapshot(pin(ratio=Decimal("0.20"))),
        now=NOW,
    )
    low = build_seller_price_band_plan(
        request_value,
        snapshot(pin(ratio=Decimal("0.10"))),
        now=NOW,
    )
    baseline = Decimal("0.09")
    cooling = replace(
        price_band_module._price_jump_controls(
            high,
            (_applied_price_state(high, baseline),),
            {},
            now=NOW,
        )[0],
        cooldown_id=42,
    )
    pending_release = price_band_module._price_jump_controls(
        low,
        (_applied_price_state(low, baseline),),
        {cooling.entity_id: cooling},
        now=NOW + timedelta(minutes=1),
    )[0]
    assert pending_release.state == "release_pending"

    rebound = price_band_module._price_jump_controls(
        high,
        (_applied_price_state(high, baseline),),
        {cooling.entity_id: pending_release},
        now=NOW + timedelta(minutes=2),
    )[0]

    assert rebound.state == "cooling"
    assert rebound.action == "takedown"
    assert rebound.release_reason is None
    assert rebound.baseline_price_multiplier == baseline


class FakeStore:
    def __init__(self, snapshots, *, states=(), elapsed_releases=()):
        self.snapshots = list(snapshots)
        self.states = tuple(states)
        self.elapsed_releases = tuple(elapsed_releases)
        self.loads = 0
        self.claims = 0
        self.claimed_ids = []
        self.finishes = 0
        self.failures = []
        self.rollout_checks = []
        self.evaluated_ats = []
        self.control_reconciles = []
        self.completed_releases = []
        self.completed_elapsed_releases = []

    def assert_rollout_ready(self, merchant_id, *, evaluated_at):
        self.rollout_checks.append((merchant_id, evaluated_at))

    def load_snapshot(
        self,
        _model,
        *,
        evaluated_at,
        usage_window_seconds,
        quote_max_age_seconds,
    ):
        assert evaluated_at.tzinfo is not None
        assert usage_window_seconds == 3600
        assert quote_max_age_seconds == 300
        self.evaluated_ats.append(evaluated_at)
        value = self.snapshots[min(self.loads, len(self.snapshots) - 1)]
        self.loads += 1
        return value

    def load_applied_states(self, _plan):
        return self.states

    def prepare_elapsed_price_jump_releases(self, _request, *, now):
        assert now.tzinfo is not None
        return self.elapsed_releases

    def complete_elapsed_price_jump_release(
        self,
        _request,
        control,
        pricing,
        result,
    ):
        self.completed_elapsed_releases.append((control, pricing, result))

    def reconcile_price_jump_controls(self, plan, states, *, now, apply):
        self.control_reconciles.append(apply)
        return price_band_module._price_jump_controls(plan, states, {}, now=now)

    def complete_price_jump_releases(self, _plan, _controls, completed_merchant_ids):
        self.completed_releases.append(completed_merchant_ids)

    def claim_plan(self, plan, ready_merchant_ids):
        self.claims += 1
        self.claimed_ids.append(tuple(ready_merchant_ids))
        return tuple(
            SellerPriceAuditRef(
                merchant_id=merchant_id,
                entity_id=plan.assignment(merchant_id).listing_managed_uid,
                idempotency_key=plan.audit_idempotency_key(plan.assignment(merchant_id)),
                action_id=1_000 + merchant_id,
                claimed=True,
                replayed=False,
                status="started",
            )
            for merchant_id in ready_merchant_ids
        )

    def finish_plan(self, plan, claims, results):
        self.finishes += 1
        assert set(results) == {item.merchant_id for item in plan.assignments}
        return tuple(replace(item, status="completed") for item in claims)

    def fail_plan(self, plan, claims, *, error_class):
        self.failures.append((plan.fingerprint, tuple(claims), error_class))
        return tuple(replace(item, status="failed") for item in claims)


class FakeResources:
    def __init__(self):
        self.calls = 0
        self.requests = {}
        self.request_batches = []

    def execute_priced(self, requests, *, apply=False, before_apply=None):
        self.calls += 1
        if apply and before_apply is not None:
            before_apply(tuple(requests))
        self.requests = dict(requests)
        self.request_batches.append(dict(requests))
        return tuple(successful_result(merchant_id) for merchant_id in requests)


def test_apply_failure_after_claim_closes_the_audit_batch_immediately() -> None:
    current = snapshot()
    store = FakeStore([current, current])

    class FailingResources(FakeResources):
        def execute_priced(self, requests, *, apply=False, before_apply=None):
            assert apply is True
            assert before_apply is not None
            before_apply(tuple(requests))
            raise RuntimeError("a6_transport_failed")

    with pytest.raises(RuntimeError, match="a6_transport_failed"):
        SellerPriceBandOrchestrator(
            store,
            FailingResources(),
            clock=lambda: NOW,
        ).execute(
            request(),
            apply=True,
            rollout_batch_limit=19,
            rollout_confirmation=ROLLOUT_BATCH_CONFIRMATION,
        )

    assert store.claims == 1
    assert store.finishes == 0
    assert len(store.failures) == 1
    assert store.failures[0][0] == build_seller_price_band_plan(
        request(), current, now=NOW
    ).fingerprint
    assert store.failures[0][2] == "a6_transport_failed"
    assert all(item.status == "started" for item in store.failures[0][1])


def test_elapsed_price_jump_release_runs_before_invalid_provider_snapshot() -> None:
    invalid = SellerPriceBandSnapshot(
        model="gpt-5.6-sol",
        pins=(),
        evaluated_at=NOW,
    )
    release = SellerPriceJumpControl(
        cooldown_id=41,
        merchant_id=1,
        entity_id="a6-managed:listing:m1:release",
        action="release",
        state="release_pending",
        baseline_price_multiplier=Decimal("0.01"),
        target_price_multiplier=Decimal("0.03"),
        target_cost_multiplier=Decimal("0.03"),
        target_delta=Decimal("0.5"),
        started_at=NOW - timedelta(minutes=45),
        cooling_until=NOW - timedelta(minutes=1),
        release_reason="cooldown_elapsed",
    )
    store = FakeStore([invalid], elapsed_releases=(release,))
    resources = FakeResources()

    with pytest.raises(SellerPriceBandError, match="seller_price_band_pin_missing"):
        SellerPriceBandOrchestrator(
            store,
            resources,
            clock=lambda: NOW,
        ).execute(
            request(),
            apply=True,
            rollout_batch_limit=19,
            rollout_confirmation=ROLLOUT_BATCH_CONFIRMATION,
        )

    assert len(store.completed_elapsed_releases) == 1
    release_request = resources.request_batches[0][1]
    assert release_request.listing_mutation == "release"
    assert release_request.listing_scope == "all_controlled"
    assert release_request.pricing.price_multiplier == Decimal("0.03")
    assert release_request.pricing.delta == Decimal("0.5")


def test_dry_run_has_one_fact_read_and_zero_audit_or_resource_writes() -> None:
    store = FakeStore([snapshot()])
    resources = FakeResources()
    result = SellerPriceBandOrchestrator(
        store,
        resources,
        clock=lambda: NOW,
    ).execute(request())

    assert result.summary()["mode"] == "dry-run"
    assert store.loads == 1
    assert store.claims == 0
    assert store.finishes == 0
    assert all(item.action_id is None and item.status == "planned" for item in result.audits)
    assert all(item.channel_strategy == "adopt_existing" for item in resources.requests.values())


def test_explicit_existing_channel_map_is_assigned_per_seller_without_creation() -> None:
    store = FakeStore([snapshot()])
    resources = FakeResources()
    mapped = replace(
        request(),
        merchant_ids=(3, 1, 2),
        existing_channel_ids={3: 833, 1: 831},
        listing_descriptions={
            1: "Team 订阅来源",
            2: "K12 配额来源",
            3: "bugteam 共享池来源",
        },
    )

    result = SellerPriceBandOrchestrator(
        store,
        resources,
        clock=lambda: NOW,
    ).execute(mapped)

    assert result.plan.public()["existing_channel_ids"] == {1: 831, 3: 833}
    assert {
        merchant_id: item.existing_channel_id for merchant_id, item in resources.requests.items()
    } == {1: 831, 2: None, 3: 833}
    assert {
        merchant_id: item.listing_description for merchant_id, item in resources.requests.items()
    } == {
        1: "Team 订阅来源",
        2: "K12 配额来源",
        3: "bugteam 共享池来源",
    }
    assert all(item.channel_strategy == "adopt_existing" for item in resources.requests.values())
    assert all(
        item.merchant_ids == (merchant_id,) for merchant_id, item in resources.requests.items()
    )


@pytest.mark.parametrize(
    "channel_map",
    (
        {3: 833},
        {1: 0},
        {True: 831},
        {1: True},
    ),
)
def test_existing_channel_map_rejects_unknown_or_invalid_ids(channel_map) -> None:
    with pytest.raises(SellerPriceBandError, match="seller_price_band_channel_map_invalid"):
        replace(request(), merchant_ids=(1, 2), existing_channel_ids=channel_map)


@pytest.mark.parametrize(
    "description_map",
    (
        {},
        {1: "Team 订阅来源"},
        {1: "Team 订阅来源", 2: ""},
        {1: "Team 订阅来源", 2: "x" * 501},
        {1: "Team 订阅来源", 2: "Pro 订阅来源，面向复杂分析任务"},
    ),
)
def test_listing_description_map_requires_one_bounded_value_per_merchant(
    description_map,
) -> None:
    with pytest.raises(
        SellerPriceBandError,
        match="seller_price_band_description_map_invalid",
    ):
        replace(
            request(),
            merchant_ids=(1, 2),
            listing_descriptions=description_map,
        )


def test_snapshot_change_after_resource_preflight_reprices_before_audit_claim() -> None:
    first = snapshot(pin(ratio=Decimal("0.10")))
    changed = snapshot(pin(ratio=Decimal("0.11")))
    store = FakeStore([first, changed])
    resources = FakeResources()

    result = SellerPriceBandOrchestrator(
        store,
        resources,
        clock=lambda: NOW,
    ).execute(
        request(),
        apply=True,
        rollout_batch_limit=19,
        rollout_confirmation=ROLLOUT_BATCH_CONFIRMATION,
    )

    assert store.loads == 2
    assert store.claims == 1
    assert store.finishes == 1
    assert result.plan.cost_multiplier == Decimal("0.11")
    assert all(
        item.pricing.cost_multiplier == Decimal("0.11") for item in resources.requests.values()
    )
    assert all(item.status == "completed" for item in result.audits)


def test_invalid_refreshed_snapshot_after_preflight_still_prevents_audit_claim() -> None:
    first = snapshot(pin(ratio=Decimal("0.10")))
    missing = SellerPriceBandSnapshot(
        model="gpt-5.6-sol",
        pins=(),
        evaluated_at=NOW,
    )
    store = FakeStore([first, missing])
    resources = FakeResources()

    with pytest.raises(SellerPriceBandError, match="seller_price_band_pin_missing"):
        SellerPriceBandOrchestrator(
            store,
            resources,
            clock=lambda: NOW,
        ).execute(
            request(),
            apply=True,
            rollout_batch_limit=19,
            rollout_confirmation=ROLLOUT_BATCH_CONFIRMATION,
        )

    assert store.loads == 2
    assert store.claims == 0
    assert store.finishes == 0


def test_snapshot_fingerprint_ignores_late_equivalent_quote_identity() -> None:
    first_pin = pin(ratio=Decimal("0.10"), usage=17)
    late_equivalent = replace(
        first_pin,
        quote_id=first_pin.quote_id + 1,
        scan_run_id=first_pin.scan_run_id + 1,
        quote_observed_at=first_pin.quote_observed_at + timedelta(seconds=8),
        usage_demand_units=18,
    )

    assert snapshot(first_pin).fingerprint == snapshot(late_equivalent).fingerprint
    assert (
        snapshot(first_pin).fingerprint
        != snapshot(replace(late_equivalent, realtime_ratio_usd=Decimal("0.11"))).fingerprint
    )


def test_snapshot_fingerprint_ignores_late_equivalent_market_quote_timestamp() -> None:
    first_quote = market_quote(901, "0.010", age_seconds=15)
    late_equivalent = replace(
        first_quote,
        observed_at=first_quote.observed_at + timedelta(seconds=8),
    )
    first = replace(snapshot(), market_quotes=(first_quote,))
    late = replace(snapshot(), market_quotes=(late_equivalent,))

    assert first.fingerprint == late.fingerprint
    assert (
        first.fingerprint
        != replace(
            late,
            market_quotes=(replace(late_equivalent, realtime_ratio_usd=Decimal("0.011")),),
        ).fingerprint
    )


def test_audit_replay_ignores_late_equivalent_pin_evidence_identity() -> None:
    first_pin = pin(ratio=Decimal("0.10"), usage=17)
    late_equivalent = replace(
        first_pin,
        quote_id=first_pin.quote_id + 1,
        scan_run_id=first_pin.scan_run_id + 1,
        quote_observed_at=first_pin.quote_observed_at + timedelta(seconds=8),
        usage_demand_units=18,
    )
    first_plan = build_seller_price_band_plan(request(), snapshot(first_pin), now=NOW)
    replay_plan = build_seller_price_band_plan(
        request(),
        snapshot(late_equivalent),
        now=NOW,
    )
    first_assignment = first_plan.assignments[0]
    replay_assignment = replay_plan.assignments[0]
    first_desired = first_plan.audit_desired_state(first_assignment)
    replay_desired = replay_plan.audit_desired_state(replay_assignment)
    row = {
        "action_type": "apply_seller_price_band",
        "entity_type": "a6_managed_listing",
        "entity_id": first_assignment.listing_managed_uid,
        "desired_state": first_desired,
        "status": "completed",
        "config_version": first_plan.request.config_version,
        "requested_by": first_plan.request.requested_by,
    }

    assert first_plan.fingerprint == replay_plan.fingerprint
    assert first_desired != replay_desired
    assert price_band_module._existing_action_matches(
        row,
        replay_assignment,
        replay_desired,
        replay_plan,
    )


def test_apply_claims_and_finishes_one_audit_per_listing() -> None:
    current = snapshot()
    store = FakeStore([current, current])
    result = SellerPriceBandOrchestrator(
        store,
        FakeResources(),
        clock=lambda: NOW,
    ).execute(
        request(),
        apply=True,
        rollout_batch_limit=19,
        rollout_confirmation=ROLLOUT_BATCH_CONFIRMATION,
    )

    assert result.summary()["success"] is True
    assert store.loads == 2
    assert store.claims == 1
    assert store.finishes == 1
    assert len(result.audits) == 19
    assert all(item.action_id and item.status == "completed" for item in result.audits)


def test_price_jump_cooling_takes_listing_down_without_claiming_high_price_as_applied() -> None:
    request_value = _single_high_ceiling_request()
    current = snapshot(pin(ratio=Decimal("0.20")))
    initial_plan = build_seller_price_band_plan(request_value, current, now=NOW)
    target = initial_plan.assignments[0].pricing.price_multiplier
    prior = _applied_price_state(initial_plan, target / Decimal("2"))
    store = FakeStore([current, current], states=(prior,))
    resources = FakeResources()

    result = SellerPriceBandOrchestrator(
        store,
        resources,
        clock=lambda: NOW,
    ).execute(request_value, apply=True)

    assert store.claimed_ids == [()]
    assert resources.requests[1].listing_mutation == "takedown"
    assert result.plan.price_jump_control(1).action == "takedown"
    assert result.audits[0].status == "skipped_price_jump_cooling"


def test_apply_recheck_uses_a_fresh_mutation_barrier_snapshot() -> None:
    current = snapshot()
    store = FakeStore([current, current])
    clock_values = iter((NOW, NOW + timedelta(seconds=30)))

    SellerPriceBandOrchestrator(
        store,
        FakeResources(),
        clock=lambda: next(clock_values),
    ).execute(
        request(),
        apply=True,
        rollout_batch_limit=19,
        rollout_confirmation=ROLLOUT_BATCH_CONFIRMATION,
    )

    assert store.evaluated_ats == [NOW, NOW + timedelta(seconds=30)]


def test_partial_applied_band_is_reconciled_before_hysteresis() -> None:
    current = snapshot()
    plan = build_seller_price_band_plan(request(), current, now=NOW)
    first = plan.assignments[0]
    state = SellerAppliedPriceState(
        merchant_id=first.merchant_id,
        entity_id=first.listing_managed_uid,
        action_id=1,
        config_version=plan.request.config_version,
        cost_multiplier=plan.cost_multiplier * Decimal("1.01"),
        final_multiplier=first.pricing.final_multiplier,
        price_multiplier=first.pricing.price_multiplier,
        delta=first.pricing.delta,
        completed_at=NOW,
    )

    transition = price_band_module._assess_transition(
        request(),
        plan,
        (state,),
        now=NOW,
    )

    assert transition.action == "reconcile_partial"
    assert transition.prior_state_count == 1


def test_latest_partial_band_supersedes_older_cost_without_conflict() -> None:
    current = snapshot()
    plan = build_seller_price_band_plan(request(), current, now=NOW)
    latest_cost = plan.cost_multiplier * Decimal("0.95")
    older_cost = plan.cost_multiplier * Decimal("1.05")
    states = tuple(
        SellerAppliedPriceState(
            merchant_id=item.merchant_id,
            entity_id=item.listing_managed_uid,
            action_id=2_000 + index,
            config_version=plan.request.config_version,
            cost_multiplier=latest_cost if index < 15 else older_cost,
            final_multiplier=item.pricing.final_multiplier,
            price_multiplier=item.pricing.price_multiplier,
            delta=item.pricing.delta,
            completed_at=NOW if index < 15 else NOW - timedelta(minutes=1),
        )
        for index, item in enumerate(plan.assignments[:16])
    )

    transition = price_band_module._assess_transition(
        request(),
        plan,
        states,
        now=NOW,
    )

    assert transition.action == "reconcile_partial"
    assert transition.previous_cost_multiplier == latest_cost
    assert transition.prior_state_count == 15


def test_cost_rise_reprices_low_listings_instead_of_requiring_takedown() -> None:
    current = snapshot()
    plan = build_seller_price_band_plan(request(), current, now=NOW)
    states = tuple(
        SellerAppliedPriceState(
            merchant_id=item.merchant_id,
            entity_id=item.listing_managed_uid,
            action_id=index,
            config_version=plan.request.config_version,
            cost_multiplier=plan.cost_multiplier * Decimal("0.9"),
            final_multiplier=Decimal("0.001"),
            price_multiplier=Decimal("0.001"),
            delta=item.pricing.delta,
            completed_at=NOW,
        )
        for index, item in enumerate(plan.assignments, start=1)
    )

    transition = price_band_module._assess_transition(
        request(),
        plan,
        states,
        now=NOW,
    )

    assert transition.action == "reprice"
    assert transition.affected_merchant_ids == tuple(request().merchant_ids)


def test_config_version_change_reconfigures_all_listings_before_hysteresis() -> None:
    current = snapshot()
    changed = replace(request(), config_version="phase3-seller-price-band-v3")
    plan = build_seller_price_band_plan(changed, current, now=NOW)
    states = tuple(
        SellerAppliedPriceState(
            merchant_id=item.merchant_id,
            entity_id=item.listing_managed_uid,
            action_id=index,
            config_version="phase3-seller-price-band-v2",
            cost_multiplier=plan.cost_multiplier * Decimal("1.001"),
            final_multiplier=item.pricing.final_multiplier,
            price_multiplier=item.pricing.price_multiplier,
            delta=item.pricing.delta,
            completed_at=NOW,
        )
        for index, item in enumerate(plan.assignments, start=1)
    )

    transition = price_band_module._assess_transition(
        changed,
        plan,
        states,
        now=NOW,
    )

    assert transition.action == "reconfigure"
    assert transition.affected_merchant_ids == tuple(changed.merchant_ids)


def test_partial_config_rollout_is_reconciled_before_reconfigure() -> None:
    current = snapshot()
    changed = replace(request(), config_version="phase3-seller-price-band-v3")
    plan = build_seller_price_band_plan(changed, current, now=NOW)
    states = tuple(
        SellerAppliedPriceState(
            merchant_id=item.merchant_id,
            entity_id=item.listing_managed_uid,
            action_id=index,
            config_version=(
                changed.config_version if index <= 15 else "phase3-seller-price-band-v2"
            ),
            cost_multiplier=plan.cost_multiplier,
            final_multiplier=item.pricing.final_multiplier,
            price_multiplier=item.pricing.price_multiplier,
            delta=item.pricing.delta,
            completed_at=NOW if index <= 15 else NOW - timedelta(minutes=1),
        )
        for index, item in enumerate(plan.assignments, start=1)
    )

    transition = price_band_module._assess_transition(
        changed,
        plan,
        states,
        now=NOW,
    )

    assert transition.action == "reconcile_partial"
    assert transition.prior_state_count == 15


def test_apply_defaults_to_single_merchant_rollout_guard() -> None:
    current = snapshot()
    orchestrator = SellerPriceBandOrchestrator(
        FakeStore([current, current]),
        FakeResources(),
        clock=lambda: NOW,
    )

    with pytest.raises(SellerPriceBandError, match="seller_price_band_rollout_guard"):
        orchestrator.execute(request(), apply=True)

    single = replace(
        request(),
        merchant_ids=(1,),
        listing_descriptions={1: "Team 订阅来源"},
    )
    single_store = FakeStore([current, current])
    result = SellerPriceBandOrchestrator(
        single_store,
        FakeResources(),
        clock=lambda: NOW,
    ).execute(single, apply=True)
    assert result.summary()["success"] is True
    assert single_store.rollout_checks == [(1, NOW)]


class RolloutEvidenceCursor:
    def __init__(self, connection) -> None:
        self.connection = connection
        self.result = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, query, params=None):
        normalized = " ".join(str(query).split())
        values = tuple(params or ())
        assert query.count("%s") == len(values)
        self.connection.calls.append((normalized, values))
        if "action_type = 'apply_seller_price_band'" in normalized:
            self.result = self.connection.latest_rollout
        elif "action_type = 'verify_seller_rollout'" in normalized:
            self.result = self.connection.rollout_evidence
            if (
                self.result is not None
                and "completed_at >= %s" in normalized
                and self.result["completed_at"] < values[1]
            ):
                self.result = None
        else:
            raise AssertionError(f"unexpected query: {normalized}")

    def fetchone(self):
        return self.result


class RolloutEvidenceConnection:
    def __init__(self, latest_rollout=None, rollout_evidence=None) -> None:
        self.latest_rollout = latest_rollout
        self.rollout_evidence = rollout_evidence
        self.calls = []
        self.closes = 0

    def cursor(self):
        return RolloutEvidenceCursor(self)

    def close(self):
        self.closes += 1


def completed_rollout(merchant_id: int, *, completed_at=NOW - timedelta(hours=1)):
    return {
        "desired_state": {"merchant_id": merchant_id},
        "completed_at": completed_at,
    }


def completed_rollout_evidence(
    *,
    window_ended_at=NOW - timedelta(minutes=5),
    completed_at=NOW - timedelta(minutes=4),
    **changes,
):
    desired_state = {
        "supplier_approved": True,
        "listing_verified": True,
        "listing_test_passed": True,
        "profit_attribution_observed": True,
        "observation_window_ended_at": window_ended_at,
    }
    desired_state.update(changes)
    return {"desired_state": desired_state, "completed_at": completed_at}


@pytest.mark.parametrize(
    ("latest_rollout", "merchant_id"),
    [(None, 7), (completed_rollout(7), 7)],
)
def test_postgres_rollout_guard_allows_first_or_same_merchant(
    latest_rollout,
    merchant_id,
) -> None:
    connection = RolloutEvidenceConnection(latest_rollout)

    PostgresSellerPriceBandStore(lambda: connection).assert_rollout_ready(
        merchant_id,
        evaluated_at=NOW,
    )

    assert len(connection.calls) == 1
    assert "status = 'completed'" in connection.calls[0][0]
    assert connection.calls[0][1] == ()
    assert connection.closes == 1


def test_postgres_rollout_guard_queries_completed_evidence_for_previous_merchant() -> None:
    latest_rollout = completed_rollout(7)
    connection = RolloutEvidenceConnection(
        latest_rollout,
        completed_rollout_evidence(),
    )

    PostgresSellerPriceBandStore(lambda: connection).assert_rollout_ready(
        8,
        evaluated_at=NOW,
    )

    assert len(connection.calls) == 2
    evidence_query, evidence_params = connection.calls[1]
    assert "action_type = 'verify_seller_rollout'" in evidence_query
    assert "entity_type = 'a6_merchant'" in evidence_query
    assert "status = 'completed'" in evidence_query
    assert "ORDER BY completed_at DESC, id DESC" in evidence_query
    assert evidence_params == ("7",)
    assert connection.closes == 1


@pytest.mark.parametrize(
    ("evidence", "error"),
    [
        (None, "seller_price_band_rollout_observation_required"),
        (
            completed_rollout_evidence(listing_test_passed=False),
            "seller_price_band_rollout_evidence_invalid",
        ),
        (
            completed_rollout_evidence(window_ended_at=NOW + timedelta(seconds=1)),
            "seller_price_band_rollout_observation_required",
        ),
    ],
)
def test_postgres_rollout_guard_rejects_missing_incomplete_or_open_evidence(
    evidence,
    error,
) -> None:
    connection = RolloutEvidenceConnection(completed_rollout(7), evidence)

    with pytest.raises(SellerPriceBandError, match=error):
        PostgresSellerPriceBandStore(lambda: connection).assert_rollout_ready(
            8,
            evaluated_at=NOW,
        )

    assert connection.closes == 1


def test_postgres_rollout_guard_accepts_jsonb_iso_observation_window() -> None:
    connection = RolloutEvidenceConnection(
        completed_rollout(7),
        completed_rollout_evidence(window_ended_at=(NOW - timedelta(minutes=5)).isoformat()),
    )

    PostgresSellerPriceBandStore(lambda: connection).assert_rollout_ready(
        8,
        evaluated_at=NOW,
    )

    assert connection.closes == 1


def test_postgres_rollout_guard_rejects_evidence_older_than_latest_rollout() -> None:
    connection = RolloutEvidenceConnection(
        completed_rollout(7, completed_at=NOW - timedelta(minutes=30)),
        completed_rollout_evidence(
            window_ended_at=NOW - timedelta(hours=2),
            completed_at=NOW - timedelta(hours=2),
        ),
    )

    with pytest.raises(
        SellerPriceBandError,
        match="seller_price_band_rollout_observation_required",
    ):
        PostgresSellerPriceBandStore(lambda: connection).assert_rollout_ready(
            8,
            evaluated_at=NOW,
        )

    assert connection.closes == 1


def single_merchant_plan():
    single = replace(
        request(),
        merchant_ids=(1,),
        listing_descriptions={1: "Team 席位 1 来源"},
    )
    return build_seller_price_band_plan(single, snapshot(), now=NOW)


def price_band_action_row(
    plan,
    *,
    status: str,
    stale: bool,
    action_id: int = 71,
):
    assignment = plan.assignments[0]
    return {
        "idempotency_key": plan.audit_idempotency_key(assignment),
        "id": action_id,
        "action_type": "apply_seller_price_band",
        "entity_type": "a6_managed_listing",
        "entity_id": assignment.listing_managed_uid,
        "desired_state": plan.audit_desired_state(assignment),
        "status": status,
        "config_version": plan.request.config_version,
        "requested_by": plan.request.requested_by,
        "stale": stale,
    }


class PriceBandLedgerCursor:
    def __init__(self, connection) -> None:
        self.connection = connection
        self.result = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, query, params=None):
        normalized = " ".join(str(query).split())
        values = tuple(params or ())
        assert query.count("%s") == len(values)
        self.connection.calls.append((normalized, values))
        if "pg_advisory_xact_lock" in normalized:
            self.result = None
        elif (
            "SELECT idempotency_key, id, action_type" in normalized
            and "WHERE idempotency_key = ANY" in normalized
        ):
            self.result = list(self.connection.existing)
        elif "AND NOT (idempotency_key = ANY" in normalized:
            self.result = list(self.connection.pending)
        elif "SET status = 'superseded'" in normalized or "SET status = 'unknown'" in normalized:
            self.result = {"id": self.connection.recovery_row_id}
        elif "SET observed_after = NULL, status = 'started'" in normalized:
            self.result = {"id": values[0]}
        elif "SELECT coalesce(max(attempt_no), 0) + 1" in normalized:
            self.result = {"attempt_no": self.connection.next_attempt_no}
        elif "INSERT INTO a6_trade.trade_control_actions" in normalized:
            self.result = {"id": self.connection.new_action_id}
        elif "INSERT INTO a6_trade.trade_control_action_attempts" in normalized:
            self.result = None
        elif (
            "UPDATE a6_trade.trade_control_actions" in normalized
            and "SET observed_after = %s::jsonb" in normalized
        ):
            self.result = {"id": values[-2]}
        elif (
            "UPDATE a6_trade.trade_control_action_attempts" in normalized
            and "SET observed_after = %s::jsonb" in normalized
        ):
            self.result = {"id": self.connection.recovery_row_id}
        else:
            raise AssertionError(f"unexpected query: {normalized}")

    def fetchall(self):
        if self.result is None:
            return []
        if isinstance(self.result, list):
            return self.result
        return [self.result]

    def fetchone(self):
        if isinstance(self.result, list):
            return self.result[0] if self.result else None
        return self.result


class PriceBandLedgerConnection:
    def __init__(
        self,
        *,
        existing=(),
        pending=(),
        new_action_id: int = 81,
        next_attempt_no: int = 2,
    ) -> None:
        self.existing = tuple(existing)
        self.pending = tuple(pending)
        self.new_action_id = new_action_id
        self.next_attempt_no = next_attempt_no
        self.recovery_row_id = 91
        self.calls = []
        self.commits = 0
        self.rollbacks = 0
        self.closes = 0

    def cursor(self):
        return PriceBandLedgerCursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closes += 1


@pytest.mark.parametrize("stale_claim_seconds", (True, 0, 86_401, 1.5))
def test_postgres_price_band_store_rejects_invalid_stale_claim_ttl(
    stale_claim_seconds,
) -> None:
    with pytest.raises(SellerPriceBandError, match="seller_price_band_stale_claim_invalid"):
        PostgresSellerPriceBandStore(
            lambda: None,
            stale_claim_seconds=stale_claim_seconds,
        )


@pytest.mark.parametrize("source", ("exact", "prior"))
@pytest.mark.parametrize("status", ("started", "unknown"))
def test_price_band_fresh_pending_action_remains_exclusive(source, status) -> None:
    plan = single_merchant_plan()
    exact = price_band_action_row(plan, status=status, stale=False)
    prior = {
        "id": 72,
        "idempotency_key": "seller-price-band:prior",
        "entity_id": plan.assignments[0].listing_managed_uid,
        "status": status,
        "stale": False,
    }
    connection = PriceBandLedgerConnection(
        existing=(exact,) if source == "exact" else (),
        pending=(prior,) if source == "prior" else (),
    )

    with pytest.raises(SellerPriceBandError, match="seller_price_band_action_pending"):
        PostgresSellerPriceBandStore(lambda: connection).claim_plan(plan, (1,))

    assert connection.commits == 0
    assert connection.rollbacks == 1
    assert not any("INSERT INTO" in query for query, _params in connection.calls)
    assert not any("SET status = 'superseded'" in query for query, _params in connection.calls)


def test_price_band_completed_exact_action_is_replayed_without_new_attempt() -> None:
    plan = single_merchant_plan()
    connection = PriceBandLedgerConnection(
        existing=(price_band_action_row(plan, status="completed", stale=True),),
    )

    claims = PostgresSellerPriceBandStore(lambda: connection).claim_plan(plan, (1,))

    assert claims[0].action_id == 71
    assert claims[0].attempt_no is None
    assert claims[0].claimed is False
    assert claims[0].replayed is True
    assert claims[0].status == "completed"
    assert not any("INSERT INTO" in query for query, _params in connection.calls)
    assert connection.commits == 1


@pytest.mark.parametrize("status", ("started", "unknown"))
def test_price_band_stale_exact_claim_restarts_same_action_with_next_attempt(status) -> None:
    plan = single_merchant_plan()
    connection = PriceBandLedgerConnection(
        existing=(price_band_action_row(plan, status=status, stale=True),),
        next_attempt_no=2,
    )

    claims = PostgresSellerPriceBandStore(lambda: connection).claim_plan(plan, (1,))

    assert len(claims) == 1
    assert claims[0].action_id == 71
    assert claims[0].attempt_no == 2
    assert claims[0].claimed is True
    assert claims[0].replayed is True
    assert connection.commits == 1
    assert any("SET status = 'unknown'" in query for query, _params in connection.calls)
    attempt_insert = next(
        params
        for query, params in connection.calls
        if "INSERT INTO a6_trade.trade_control_action_attempts" in query
    )
    assert attempt_insert[:2] == (71, 2)


def test_price_band_stale_prior_plan_is_superseded_before_new_claim() -> None:
    plan = single_merchant_plan()
    prior = {
        "id": 72,
        "idempotency_key": "seller-price-band:prior",
        "entity_id": plan.assignments[0].listing_managed_uid,
        "status": "started",
        "stale": True,
    }
    connection = PriceBandLedgerConnection(pending=(prior,), new_action_id=82)

    claims = PostgresSellerPriceBandStore(lambda: connection).claim_plan(plan, (1,))

    assert claims[0].action_id == 82
    assert claims[0].attempt_no == 1
    assert claims[0].replayed is False
    queries = [query for query, _params in connection.calls]
    supersede_index = next(
        index for index, query in enumerate(queries) if "SET status = 'superseded'" in query
    )
    insert_index = next(
        index
        for index, query in enumerate(queries)
        if "INSERT INTO a6_trade.trade_control_actions" in query
    )
    assert supersede_index < insert_index
    action_supersede = next(
        params
        for query, params in connection.calls
        if "UPDATE a6_trade.trade_control_actions" in query and "SET status = 'superseded'" in query
    )
    assert "superseded_after_remote_preflight" in action_supersede[1]


def test_price_band_recovered_attempt_is_finished_by_attempt_number() -> None:
    plan = single_merchant_plan()
    claim_connection = PriceBandLedgerConnection(
        existing=(price_band_action_row(plan, status="started", stale=True),),
        next_attempt_no=2,
    )
    finish_connection = PriceBandLedgerConnection()
    connections = iter((claim_connection, finish_connection))
    store = PostgresSellerPriceBandStore(lambda: next(connections))

    claims = store.claim_plan(plan, (1,))
    finished = store.finish_plan(plan, claims, {1: successful_result(1)})

    assert finished[0].status == "completed"
    attempt_finish = next(
        params
        for query, params in finish_connection.calls
        if "UPDATE a6_trade.trade_control_action_attempts" in query
    )
    assert attempt_finish[-2:] == (71, 2)
    assert finish_connection.commits == 1


def test_price_band_failed_round_closes_action_and_attempt_for_immediate_retry() -> None:
    plan = single_merchant_plan()
    claim_connection = PriceBandLedgerConnection(new_action_id=82)
    fail_connection = PriceBandLedgerConnection()
    connections = iter((claim_connection, fail_connection))
    store = PostgresSellerPriceBandStore(lambda: next(connections))

    claims = store.claim_plan(plan, (1,))
    failed = store.fail_plan(plan, claims, error_class="a6_transport_failed")

    assert failed[0].status == "failed"
    action_update = next(
        (query, params)
        for query, params in fail_connection.calls
        if "UPDATE a6_trade.trade_control_actions" in query
    )
    attempt_update = next(
        (query, params)
        for query, params in fail_connection.calls
        if "UPDATE a6_trade.trade_control_action_attempts" in query
    )
    assert "status = 'failed'" in action_update[0]
    assert "remote_state" in action_update[1][0]
    assert action_update[1][1:] == ("a6_transport_failed", 82, claims[0].idempotency_key)
    assert "status = 'failed'" in attempt_update[0]
    assert attempt_update[1][-2:] == (82, 1)
    assert fail_connection.commits == 1
    assert fail_connection.rollbacks == 0
