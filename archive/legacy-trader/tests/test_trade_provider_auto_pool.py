from __future__ import annotations

from decimal import Decimal

import pytest

from a6api_auto.trade_provider_auto_pool import (
    AutoPoolMarketQuote,
    OwnedMarketplaceResources,
    ProviderAutoPoolError,
    ProviderAutoPoolTarget,
    marketplace_channel_snapshot,
    plan_provider_auto_pool,
    reconcile_provider_auto_pool,
)


def quote(
    observer: int,
    channel: int,
    ratio: str | None,
    *,
    ratio_usd: str | None = None,
    supplier: int | None = None,
    available: bool = True,
    health: str = "healthy",
) -> AutoPoolMarketQuote:
    return AutoPoolMarketQuote(
        observer_merchant_id=observer,
        channel_id=channel,
        model="gpt-5.6-sol",
        realtime_ratio_cny=Decimal(ratio) if ratio is not None else None,
        realtime_ratio_usd=(
            Decimal(ratio_usd)
            if ratio_usd is not None
            else Decimal(ratio) / Decimal("6.75")
            if ratio is not None
            else None
        ),
        available=available,
        health_state=health,
        supplier_id=supplier,
        last_success_at=1000 + observer,
        latency_ms=Decimal(100 + observer),
    )


def test_plan_uses_cny_quorum_median_and_fifth_channel_as_max_ratio() -> None:
    quotes = []
    for channel, ratio in enumerate(
        ("0.04", "0.05", "0.06", "0.07", "0.08", "0.09"),
        start=100,
    ):
        quotes.extend(
            (
                quote(1, channel, ratio, supplier=200 + channel),
                quote(2, channel, str(Decimal(ratio) + Decimal("0.002")), supplier=200 + channel),
            )
        )

    plan = plan_provider_auto_pool(quotes, model="gpt-5.6-sol")

    assert [item.channel_id for item in plan.selected] == [100, 101, 102, 103, 104]
    assert plan.max_ratio == Decimal("0.081")
    assert plan.max_ratio_usd == Decimal("0.012")
    assert plan.min_ratio == 0
    assert len(plan.source_hash) == 64


def test_plan_uses_cny_boundary_channels_matching_usd_snapshot() -> None:
    plan = plan_provider_auto_pool(
        [
            quote(1, 100, "0.04", ratio_usd="0.020", supplier=200),
            quote(2, 100, "0.04", ratio_usd="0.020", supplier=200),
            quote(1, 101, "0.05", ratio_usd="0.010", supplier=201),
            quote(2, 101, "0.05", ratio_usd="0.010", supplier=201),
        ],
        model="gpt-5.6-sol",
        target_channel_count=2,
    )

    assert [item.channel_id for item in plan.selected] == [100, 101]
    assert plan.max_ratio == Decimal("0.05")
    assert plan.max_ratio_usd == Decimal("0.010")


def test_runtime_failure_expands_to_the_next_distinct_cny_price_band() -> None:
    quotes = []
    for channel, ratio, ratio_usd in (
        (100, "0.04", "0.006"),
        (101, "0.05", "0.007"),
        (102, "0.06", "0.008"),
        (103, "0.08", "0.011"),
        (104, "0.09", "0.013"),
    ):
        quotes.extend(
            (
                quote(1, channel, ratio, ratio_usd=ratio_usd, supplier=200 + channel),
                quote(2, channel, ratio, ratio_usd=ratio_usd, supplier=200 + channel),
            )
        )

    plan = plan_provider_auto_pool(
        quotes,
        model="gpt-5.6-sol",
        expand_above_ratio=Decimal("0.06"),
    )

    assert [item.channel_id for item in plan.selected] == [100, 101, 102, 103]
    assert plan.max_ratio == Decimal("0.08")
    assert plan.max_ratio_usd == Decimal("0.011")


def test_runtime_failure_stops_when_no_higher_band_exists_below_hard_cap() -> None:
    with pytest.raises(
        ProviderAutoPoolError,
        match="provider_auto_pool_expansion_exhausted",
    ):
        plan_provider_auto_pool(
            [
                quote(1, 100, "0.04", supplier=200),
                quote(2, 100, "0.04", supplier=200),
                quote(1, 101, "0.05", supplier=201),
                quote(2, 101, "0.05", supplier=201),
            ],
            model="gpt-5.6-sol",
            expand_above_ratio=Decimal("0.05"),
        )


def test_plan_excludes_owned_unhealthy_disputed_and_above_cap_channels() -> None:
    plan = plan_provider_auto_pool(
        [
            quote(1, 100, "0.04", supplier=355),
            quote(2, 100, "0.04", supplier=355),
            quote(1, 101, "0.05", supplier=400),
            quote(2, 101, "0.05", supplier=400, available=False),
            quote(3, 101, "0.05", supplier=400, available=False),
            quote(1, 102, "0.31", supplier=401),
            quote(2, 102, "0.31", supplier=401),
            quote(1, 103, "0.06", supplier=402),
            quote(2, 103, "0.062", supplier=402),
        ],
        model="gpt-5.6-sol",
        owned=OwnedMarketplaceResources(supplier_ids=frozenset({355})),
    )

    assert [item.channel_id for item in plan.selected] == [103]
    assert plan.max_ratio == Decimal("0.061")
    assert dict(plan.excluded) == {
        100: "owned",
        101: "observer_quorum",
        102: "above_hard_max",
    }


class FakeAutoPoolClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.rows = [
            {
                "channel_id": 1283,
                "listing_id": 4409,
                "supplier_id": 355,
                "user_channel_disabled": False,
                "route_status": "default",
            },
            {
                "channel_id": 1673,
                "listing_id": 5825,
                "supplier_id": 355,
                "user_channel_disabled": True,
                "route_status": "user_disabled",
            },
            {
                "channel_id": 622,
                "listing_id": 3000,
                "supplier_id": 444,
                "user_channel_disabled": False,
                "route_status": "default",
            },
        ]
        self.pins = [
            {"token_id": 20406, "model_name": "gpt-5.6-sol", "channel_id": 622}
        ]
        self.listings = [
            {"id": 4409, "status": 2},
            {"id": 5825, "status": 2},
            {"id": 5901, "status": 0},
        ]
        self.subscription = {
            "id": 26816,
            "user_id": 2292,
            "token_id": 20406,
            "model_name": "gpt-5.6-sol",
            "auto_pool_enabled": True,
            "min_ratio": "0",
            "max_ratio": "0.051",
            "channels": [{"channel_id": 622, "supplier_id": 444, "state": "active"}],
            "auto_channels": [
                {
                    "channel_id": 622,
                    "state": "auto_pool",
                    "realtime_ratio_cny": "0.05",
                    "realtime_ratio_usd": "0.007407407407",
                }
            ],
        }

    def marketplace_channels(self, user_id: int, **kwargs):
        self.calls.append(("channels", {"user_id": user_id, **kwargs}))
        page = kwargs["page"]
        rows = self.rows[(page - 1) * 2 : page * 2]
        return {"items": [dict(item) for item in rows], "total": len(self.rows)}

    def marketplace_disable_channel(self, user_id: int, **kwargs):
        self.calls.append(("disable", {"user_id": user_id, **kwargs}))
        for item in self.rows:
            if item["channel_id"] == kwargs["channel_id"]:
                item["user_channel_disabled"] = True
                item["route_status"] = "user_disabled"
        return {}

    def marketplace_pins(self, user_id: int):
        self.calls.append(("pins", {"user_id": user_id}))
        return [dict(item) for item in self.pins]

    def marketplace_unpin(self, user_id: int, **kwargs):
        self.calls.append(("unpin", {"user_id": user_id, **kwargs}))
        self.pins = []
        return {}

    def marketplace_subscriptions(self, user_id: int):
        self.calls.append(("subscriptions", {"user_id": user_id}))
        return [dict(self.subscription)]

    def marketplace_update_auto_pool(self, user_id: int, **kwargs):
        self.calls.append(("update", {"user_id": user_id, **kwargs}))
        self.subscription["max_ratio"] = str(kwargs["max_ratio"])
        return dict(self.subscription)

    def marketplace_create_auto_pool(self, user_id: int, **kwargs):
        raise AssertionError("existing subscription should be updated")

    def marketplace_subscription(self, user_id: int, **kwargs):
        self.calls.append(("subscription", {"user_id": user_id, **kwargs}))
        return dict(self.subscription)

    def supplier_listings(self, user_id: int, **kwargs):
        self.calls.append(("supplier_listings", {"user_id": user_id, **kwargs}))
        page = kwargs.get("page", 1)
        size = kwargs.get("size", 100)
        start = (page - 1) * size
        items = [dict(item) for item in self.listings[start : start + size]]
        return {"items": items, "total": len(self.listings)}

    def resubmit_supplier_listing(self, user_id: int, **kwargs):
        self.calls.append(("resubmit", {"user_id": user_id, **kwargs}))
        listing_id = kwargs["listing_id"]
        for item in self.listings:
            if item["id"] == listing_id:
                item["status"] = 2
        if listing_id == 5901 and not any(
            row.get("channel_id") == 1900 for row in self.rows
        ):
            self.rows.append(
                {
                    "channel_id": 1900,
                    "listing_id": 5901,
                    "supplier_id": 355,
                    "user_channel_disabled": False,
                    "route_status": "default",
                }
            )
        return {}


def test_reconcile_bans_every_visible_owned_channel_before_auto_pool_update() -> None:
    client = FakeAutoPoolClient()
    target = ProviderAutoPoolTarget(2292, 20406, "gpt-5.6-sol")
    plan = plan_provider_auto_pool(
        [quote(1, 622, "0.05", supplier=444), quote(2, 622, "0.052", supplier=444)],
        model=target.model,
    )
    owned = OwnedMarketplaceResources(
        supplier_ids=frozenset({355}),
        channel_ids=frozenset({1283, 1673}),
    )

    result = reconcile_provider_auto_pool(
        client,
        target,
        plan,
        owned,
        page_size=2,
    )

    assert result.disabled_channel_ids == (1283,)
    assert result.confirmed_disabled_channel_ids == (1283, 1673)
    assert result.visible_owned_channel_ids == (1283, 1673)
    assert result.absent_owned_channel_ids == ()
    assert result.unpinned is True
    names = [name for name, _payload in client.calls]
    assert names.index("disable") < names.index("unpin") < names.index("update")
    channel_calls = [payload for name, payload in client.calls if name == "channels"]
    assert all(payload["include_user_disabled"] is True for payload in channel_calls)
    assert all(payload["include_disabled"] is True for payload in channel_calls)
    assert all(payload["exclude_unavailable"] is False for payload in channel_calls)
    assert all(payload["route_status"] == "all" for payload in channel_calls)
    assert all(payload["include_seed"] is True for payload in channel_calls)


def test_marketplace_snapshot_rejects_duplicate_rows_across_pages() -> None:
    client = FakeAutoPoolClient()
    client.rows = [client.rows[0], client.rows[0]]

    with pytest.raises(
        ProviderAutoPoolError,
        match="provider_auto_pool_marketplace_pagination_unstable",
    ):
        marketplace_channel_snapshot(
            client,
            ProviderAutoPoolTarget(2292, 20406, "gpt-5.6-sol"),
            page_size=1,
        )


def test_reconcile_stops_before_subscription_when_ban_readback_is_incomplete() -> None:
    class BrokenReadbackClient(FakeAutoPoolClient):
        def marketplace_disable_channel(self, user_id: int, **kwargs):
            self.calls.append(("disable", {"user_id": user_id, **kwargs}))
            return {}

    client = BrokenReadbackClient()
    target = ProviderAutoPoolTarget(2292, 20406, "gpt-5.6-sol")
    plan = plan_provider_auto_pool(
        [quote(1, 622, "0.05", supplier=444), quote(2, 622, "0.052", supplier=444)],
        model=target.model,
    )

    with pytest.raises(
        ProviderAutoPoolError,
        match="provider_auto_pool_owned_channel_readback_failed",
    ):
        reconcile_provider_auto_pool(
            client,
            target,
            plan,
            OwnedMarketplaceResources(supplier_ids=frozenset({355})),
            page_size=2,
        )

    assert not any(name in {"unpin", "update"} for name, _payload in client.calls)


def test_reconcile_stops_when_known_owned_channel_is_missing_from_full_snapshot() -> None:
    client = FakeAutoPoolClient()
    target = ProviderAutoPoolTarget(2292, 20406, "gpt-5.6-sol")
    plan = plan_provider_auto_pool(
        [quote(1, 622, "0.05", supplier=444), quote(2, 622, "0.052", supplier=444)],
        model=target.model,
    )

    with pytest.raises(
        ProviderAutoPoolError,
        match="provider_auto_pool_owned_resource_not_visible",
    ):
        reconcile_provider_auto_pool(
            client,
            target,
            plan,
            OwnedMarketplaceResources(channel_ids=frozenset({1283, 1900})),
            page_size=2,
        )

    assert not any(name in {"disable", "unpin", "update"} for name, _payload in client.calls)


def test_reconcile_relists_down_owned_channels_before_ban_when_mapping_provided() -> None:
    client = FakeAutoPoolClient()
    target = ProviderAutoPoolTarget(2292, 20406, "gpt-5.6-sol")
    plan = plan_provider_auto_pool(
        [quote(1, 622, "0.05", supplier=444), quote(2, 622, "0.052", supplier=444)],
        model=target.model,
    )
    owned = OwnedMarketplaceResources(
        supplier_ids=frozenset({355}),
        channel_ids=frozenset({1283, 1673, 1900}),
        listing_ids=frozenset({4409, 5825, 5901}),
    )
    mappings = {1283: 4409, 1673: 5825, 1900: 5901}

    result = reconcile_provider_auto_pool(
        client,
        target,
        plan,
        owned,
        page_size=2,
        owned_listings_by_channel=mappings,
    )

    # 下架的 5901 被重新上架，随后所有 owned 渠道被 ban 并确认
    names = [name for name, _payload in client.calls]
    assert "resubmit" in names
    resubmit = [payload for name, payload in client.calls if name == "resubmit"]
    assert [payload["listing_id"] for payload in resubmit] == [5901]
    assert result.disabled_channel_ids == (1283, 1900)
    assert result.confirmed_disabled_channel_ids == (1283, 1673, 1900)
    assert names.index("resubmit") < names.index("disable")
    assert names.index("resubmit") < names.index("unpin") < names.index("update")


def test_reconcile_relist_readback_failure_aborts_before_ban() -> None:
    class StuckResubmitClient(FakeAutoPoolClient):
        def resubmit_supplier_listing(self, user_id: int, **kwargs):
            self.calls.append(("resubmit", {"user_id": user_id, **kwargs}))
            return {}

    client = StuckResubmitClient()
    target = ProviderAutoPoolTarget(2292, 20406, "gpt-5.6-sol")
    plan = plan_provider_auto_pool(
        [quote(1, 622, "0.05", supplier=444), quote(2, 622, "0.052", supplier=444)],
        model=target.model,
    )
    owned = OwnedMarketplaceResources(
        channel_ids=frozenset({1283, 1900}),
    )

    with pytest.raises(
        ProviderAutoPoolError,
        match="provider_auto_pool_owned_relist_readback_failed",
    ):
        reconcile_provider_auto_pool(
            client,
            target,
            plan,
            owned,
            page_size=2,
            owned_listings_by_channel={1900: 5901},
        )

    assert not any(name in {"disable", "unpin", "update"} for name, _payload in client.calls)


def test_reconcile_relist_requires_mapping_for_missing_owned_channel() -> None:
    client = FakeAutoPoolClient()
    target = ProviderAutoPoolTarget(2292, 20406, "gpt-5.6-sol")
    plan = plan_provider_auto_pool(
        [quote(1, 622, "0.05", supplier=444), quote(2, 622, "0.052", supplier=444)],
        model=target.model,
    )
    owned = OwnedMarketplaceResources(channel_ids=frozenset({1283, 1900}))

    # 映射缺失（1900 无对应 listing）→ 中止且不做任何变更
    with pytest.raises(
        ProviderAutoPoolError,
        match="provider_auto_pool_owned_relist_mapping_missing",
    ):
        reconcile_provider_auto_pool(
            client,
            target,
            plan,
            owned,
            page_size=2,
            owned_listings_by_channel={1283: 4409},
        )

    assert not any(name in {"resubmit", "disable", "unpin", "update"} for name, _payload in client.calls)


def test_reconcile_relist_without_mapping_keeps_strict_visibility_failure() -> None:
    client = FakeAutoPoolClient()
    target = ProviderAutoPoolTarget(2292, 20406, "gpt-5.6-sol")
    plan = plan_provider_auto_pool(
        [quote(1, 622, "0.05", supplier=444), quote(2, 622, "0.052", supplier=444)],
        model=target.model,
    )
    owned = OwnedMarketplaceResources(channel_ids=frozenset({1283, 1900}))

    with pytest.raises(
        ProviderAutoPoolError,
        match="provider_auto_pool_owned_resource_not_visible",
    ):
        reconcile_provider_auto_pool(
            client,
            target,
            plan,
            owned,
            page_size=2,
        )

    assert not any(name in {"resubmit", "disable", "unpin", "update"} for name, _payload in client.calls)


def test_reconcile_rejects_owned_channel_in_effective_auto_pool() -> None:
    client = FakeAutoPoolClient()
    client.subscription["auto_channels"] = [
        {
            "channel_id": 1283,
            "state": "auto_pool",
            "realtime_ratio_cny": "0.05",
        }
    ]
    target = ProviderAutoPoolTarget(2292, 20406, "gpt-5.6-sol")
    plan = plan_provider_auto_pool(
        [quote(1, 622, "0.05", supplier=444), quote(2, 622, "0.052", supplier=444)],
        model=target.model,
    )

    with pytest.raises(
        ProviderAutoPoolError,
        match="provider_auto_pool_owned_channel_selected",
    ):
        reconcile_provider_auto_pool(
            client,
            target,
            plan,
            OwnedMarketplaceResources(
                supplier_ids=frozenset({355}),
                channel_ids=frozenset({1283, 1673}),
            ),
            page_size=2,
        )


@pytest.mark.parametrize(
    "marker",
    (
        {"state": "auto_pool"},
        {"pool_source": "auto"},
        {"ratio_status": "matched"},
    ),
)
def test_reconcile_accepts_marketplace_effective_auto_pool_markers(marker) -> None:
    client = FakeAutoPoolClient()
    client.subscription["channels"] = []
    client.subscription["auto_channels"] = [
        {
            "channel_id": 622,
            "realtime_ratio_cny": "0.05",
            **marker,
        }
    ]
    target = ProviderAutoPoolTarget(2292, 20406, "gpt-5.6-sol")
    plan = plan_provider_auto_pool(
        [quote(1, 622, "0.05", supplier=444), quote(2, 622, "0.052", supplier=444)],
        model=target.model,
    )

    result = reconcile_provider_auto_pool(
        client,
        target,
        plan,
        OwnedMarketplaceResources(
            supplier_ids=frozenset({355}),
            channel_ids=frozenset({1283, 1673}),
        ),
        page_size=2,
    )

    assert result.subscription_id == 26816


def test_auto_pool_store_cas_and_runner_lease_are_wired() -> None:
    import inspect

    from a6api_auto.trade_provider_auto_pool_store import (
        PostgresProviderAutoPoolStore,
    )
    from scripts.reconcile_a6_provider_auto_pool import run

    store_source = inspect.getsource(PostgresProviderAutoPoolStore)
    assert "provider_auto_pool_account_cost_generation_mismatch" in store_source
    assert "max_ratio_cny = %s" in store_source
    assert "max_ratio_usd = %s" in store_source

    runner_source = inspect.getsource(run)
    assert "PostgresProviderModelLease" in runner_source
    assert ".hold(model)" in runner_source
    assert "store.begin_generation(" in runner_source
    assert "store.activate(" in runner_source
    begin = runner_source.index("PostgresProviderModelLease")
    activate = runner_source.index("store.activate(")
    assert begin < activate
