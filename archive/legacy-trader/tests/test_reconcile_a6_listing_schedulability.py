from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from scripts.reconcile_a6_listing_schedulability import (
    AccountEvidence,
    Target,
    decide,
    load_accounts,
)

NOW = datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc)

NET_MULTIPLIER = Decimal("1.6")
GROSS_MULTIPLIER = Decimal("1.92")
PROVIDER_GATE_MULTIPLIER = Decimal("1.44")


def target(
    merchant_id: int = 1,
    listing_id: int = 100,
    channel_id: int = 50,
    output_price_micros: int = 235_446,
) -> Target:
    return Target(
        merchant_id=merchant_id,
        a6_user_id=merchant_id + 1000,
        session_ref=Path(f"/sessions/merchant-{merchant_id}"),
        listing_id=listing_id,
        channel_id=channel_id,
        model="gpt-5.6-sol",
        output_price_micros=output_price_micros,
    )


def account(
    *,
    source_id: int = 14,
    enodot_account_id: int = 45931,
    cost_mode: str = "weekly_amortized",
    cycle_cost_cny: Decimal | None = Decimal("25"),
    declared_k_cny: Decimal | None = None,
    used_a: Decimal | None = Decimal("203.87"),
    remaining_a: Decimal | None = Decimal("0"),
    capacity_state: str = "AVAILABLE",
    health_state: str = "HEALTHY",
) -> AccountEvidence:
    return AccountEvidence(
        source_id=source_id,
        enodot_account_id=enodot_account_id,
        capacity_state=capacity_state,
        health_state=health_state,
        cost_mode=cost_mode,
        cycle_cost_cny=cycle_cost_cny,
        declared_k_cny=declared_k_cny,
        used_a=used_a,
        remaining_a=remaining_a,
    )


def decide_with(
    target_item: Target,
    *,
    is_live: bool,
    channel_net_k_cny: Decimal | None,
    channel_quote_cny: Decimal | None = None,
    schedulable_provider_merchants: frozenset[int] = frozenset(),
    accounts: tuple[AccountEvidence, ...] = (),
    provider_cost_cny_per_1k: Decimal | None = None,
    minimum_remaining_a: Decimal = Decimal("20"),
    banned_channel_ids: frozenset[int] = frozenset(),
) -> object:
    return decide(
        target_item,
        is_live=is_live,
        channel_net_k_cny=channel_net_k_cny,
        channel_quote_cny=channel_quote_cny,
        schedulable_provider_merchants=schedulable_provider_merchants,
        accounts=accounts,
        gross_gate_multiplier=GROSS_MULTIPLIER,
        provider_cost_cny_per_1k=provider_cost_cny_per_1k,
        provider_gate_multiplier=PROVIDER_GATE_MULTIPLIER,
        minimum_remaining_a=minimum_remaining_a,
        banned_channel_ids=banned_channel_ids,
    )


def test_callable_account_requires_health_capacity_cost_and_remaining_quota() -> None:
    exhausted = account(capacity_state="EXHAUSTED")
    assert not exhausted.callable()
    available = account(remaining_a=Decimal("30"))
    assert available.callable()
    assert available.source_k_cny() == Decimal("0.106897")
    unknown_cost = account(used_a=None, remaining_a=None)
    assert not unknown_cost.callable()
    nearly_empty = account(remaining_a=Decimal("5"))
    assert not nearly_empty.callable()


def test_gate_for_account_45931_is_0_235446() -> None:
    # Cost 25 CNY amortized over the consumed 203.87K units -> k 0.122628,
    # net gate 0.122628 * 1.6 = 0.196205, gross gate 0.196205 * 1.2 = 0.235446.
    available = account()
    assert available.source_k_cny() == Decimal("0.122628")
    assert available.source_k_cny() * NET_MULTIPLIER == Decimal("0.1962048")
    assert available.source_k_cny() * GROSS_MULTIPLIER == Decimal("0.23544576")


def test_no_callable_source_takes_down_live_listing_and_keeps_down_listing() -> None:
    only = (account(capacity_state="EXHAUSTED", remaining_a=Decimal("0")),)
    live = decide_with(
        target(),
        is_live=True,
        channel_net_k_cny=Decimal("1"),
        accounts=only,
    )
    assert live.action == "takedown"
    assert live.reason == "no_callable_source"
    down = decide_with(
        target(),
        is_live=False,
        channel_net_k_cny=Decimal("1"),
        accounts=only,
    )
    assert down.action == "keep"


def test_auto_pool_banned_channel_is_always_kept_regardless_of_gate() -> None:
    only = (account(capacity_state="EXHAUSTED", remaining_a=Decimal("0")),)
    # banned 渠道即使无 callable source、价格低于门禁也保持现状（auto-pool 接管）
    for is_live in (True, False):
        decision = decide_with(
            target(),
            is_live=is_live,
            channel_net_k_cny=Decimal("1"),
            channel_quote_cny=Decimal("0.0001"),
            accounts=only,
            banned_channel_ids=frozenset({50}),
        )
        assert decision.action == "keep"
        assert decision.reason == "auto_pool_banned"
    # 非 banned 渠道不受影响
    normal = decide_with(
        target(),
        is_live=True,
        channel_net_k_cny=Decimal("1"),
        channel_quote_cny=Decimal("0.0001"),
        accounts=only,
        banned_channel_ids=frozenset({51}),
    )
    assert normal.action == "takedown"


def test_provider_schedulable_relists_merchant_regardless_of_gate() -> None:
    only = (account(remaining_a=Decimal("30")),)
    decision = decide_with(
        target(merchant_id=9),
        is_live=False,
        channel_net_k_cny=Decimal("0.01"),
        schedulable_provider_merchants=frozenset({9}),
        accounts=only,
    )
    assert decision.action == "relist"
    assert decision.reason == "provider_schedulable"


def test_sub2api_gate_relists_by_listing_price_only() -> None:
    only = (account(remaining_a=Decimal("30")),)
    k_cny = only[0].source_k_cny()
    assert k_cny is not None
    price_gate_cny = k_cny * GROSS_MULTIPLIER
    blocked = decide_with(
        target(),
        is_live=True,
        channel_net_k_cny=None,
        channel_quote_cny=price_gate_cny - Decimal("0.000001"),
        accounts=only,
    )
    assert blocked.action == "takedown"
    assert blocked.reason == "sub2api_price_gate_blocked"
    allowed = decide_with(
        target(),
        is_live=False,
        channel_net_k_cny=None,
        channel_quote_cny=price_gate_cny,
        accounts=only,
    )
    assert allowed.action == "relist"
    assert allowed.reason == "sub2api_price_gate_passed"
    no_quote = decide_with(
        target(),
        is_live=False,
        channel_net_k_cny=None,
        channel_quote_cny=None,
        accounts=only,
    )
    assert no_quote.action == "keep"
    assert no_quote.reason == "no_market_quote"


def test_provider_profit_gate_relists_when_price_covers_provider_cost() -> None:
    only = (account(remaining_a=Decimal("30")),)
    provider_cost = Decimal("0.0715")
    decision = decide_with(
        target(),
        is_live=False,
        channel_net_k_cny=None,
        channel_quote_cny=provider_cost * PROVIDER_GATE_MULTIPLIER,
        schedulable_provider_merchants=frozenset({5}),
        accounts=only,
        provider_cost_cny_per_1k=provider_cost,
    )
    assert decision.action == "relist"
    assert decision.reason == "provider_profit_gate_passed"
    assert decision.gate_k_cny == provider_cost * PROVIDER_GATE_MULTIPLIER


def test_provider_gate_blocks_when_price_below_provider_cost_floor() -> None:
    provider_cost = Decimal("0.0715")
    decision = decide_with(
        target(),
        is_live=True,
        channel_net_k_cny=None,
        channel_quote_cny=provider_cost * PROVIDER_GATE_MULTIPLIER - Decimal("0.000001"),
        schedulable_provider_merchants=frozenset({5}),
        accounts=(),
        provider_cost_cny_per_1k=provider_cost,
    )
    assert decision.action == "takedown"
    assert decision.reason == "provider_profit_gate_blocked"


def test_cheapest_callable_account_gate_is_used() -> None:
    cheap = account(
        source_id=10,
        enodot_account_id=45927,
        cycle_cost_cny=Decimal("14"),
        used_a=Decimal("230"),
        remaining_a=Decimal("20"),
    )
    expensive = account(remaining_a=Decimal("30"))
    accounts = (expensive, cheap)
    cheap_k = Decimal("14") / Decimal("250")
    decision = decide_with(
        target(),
        is_live=False,
        channel_net_k_cny=None,
        channel_quote_cny=cheap_k * GROSS_MULTIPLIER,
        accounts=accounts,
    )
    assert decision.action == "relist"
    assert decision.reason == "sub2api_price_gate_passed"


class FakeCursor:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows

    def __enter__(self) -> FakeCursor:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def execute(self, sql: str, params: tuple[object, ...] | None = None) -> None:
        del sql, params

    def fetchall(self) -> list[dict[str, object]]:
        return self.rows


class FakeConnection:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows
        self.closed = False

    def cursor(self) -> FakeCursor:
        return FakeCursor(self.rows)

    def close(self) -> None:
        self.closed = True


def test_load_accounts_skips_stale_or_disabled_sources() -> None:
    rows = [
        {
            "source_id": 1,
            "enodot_account_id": 45918,
            "capacity_state": "EXHAUSTED",
            "health_state": "HEALTHY",
            "cost_mode": "weekly_amortized",
            "cycle_cost_cny": Decimal("9"),
            "declared_k_cny": None,
            "used_a": Decimal("100"),
            "remaining_a": Decimal("0"),
        },
        {
            "source_id": 14,
            "enodot_account_id": 45931,
            "capacity_state": "AVAILABLE",
            "health_state": "HEALTHY",
            "cost_mode": "weekly_amortized",
            "cycle_cost_cny": Decimal("25"),
            "declared_k_cny": None,
            "used_a": Decimal("4"),
            "remaining_a": Decimal("96"),
        },
    ]
    connection = FakeConnection(rows)
    accounts = load_accounts(connection, "gpt-5.6-sol", now=NOW)
    assert [item.enodot_account_id for item in accounts] == [45918, 45931]
    assert [item.callable() for item in accounts] == [False, True]
