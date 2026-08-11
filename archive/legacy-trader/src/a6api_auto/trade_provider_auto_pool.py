"""Event-driven Provider auto-pool planning and remote reconciliation."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from statistics import median
from typing import Any, Protocol


class ProviderAutoPoolError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


_READY_HEALTH_STATES = frozenset({"healthy", "idle"})
_USER_DISABLED_ROUTE_STATUS = "user_disabled"


@dataclass(frozen=True)
class AutoPoolMarketQuote:
    observer_merchant_id: int
    channel_id: int
    model: str
    realtime_ratio_cny: Decimal | None
    realtime_ratio_usd: Decimal | None
    available: bool
    health_state: str
    supplier_id: int | None = None
    listing_id: int | None = None
    last_success_at: int | None = None
    latency_ms: Decimal | None = None

    def __post_init__(self) -> None:
        for value in (self.observer_merchant_id, self.channel_id):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ProviderAutoPoolError("provider_auto_pool_quote_identity_invalid")
        if not str(self.model or "").strip() or not isinstance(self.available, bool):
            raise ProviderAutoPoolError("provider_auto_pool_quote_invalid")
        for value in (self.supplier_id, self.listing_id, self.last_success_at):
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value < 1
            ):
                raise ProviderAutoPoolError("provider_auto_pool_quote_identity_invalid")
        for value in (
            self.realtime_ratio_cny,
            self.realtime_ratio_usd,
            self.latency_ms,
        ):
            if value is not None and (
                not isinstance(value, Decimal) or not value.is_finite() or value < 0
            ):
                raise ProviderAutoPoolError("provider_auto_pool_quote_decimal_invalid")


@dataclass(frozen=True)
class AutoPoolCandidate:
    channel_id: int
    supplier_id: int | None
    listing_id: int | None
    ratio_cny: Decimal
    ratio_usd: Decimal
    observer_ids: tuple[int, ...]
    last_success_at: int | None
    latency_ms: Decimal | None


@dataclass(frozen=True)
class AutoPoolPlan:
    model: str
    min_ratio: Decimal
    max_ratio: Decimal
    max_ratio_usd: Decimal
    selected: tuple[AutoPoolCandidate, ...]
    eligible: tuple[AutoPoolCandidate, ...]
    excluded: tuple[tuple[int, str], ...]
    source_hash: str


@dataclass(frozen=True)
class OwnedMarketplaceResources:
    supplier_ids: frozenset[int] = frozenset()
    channel_ids: frozenset[int] = frozenset()
    listing_ids: frozenset[int] = frozenset()

    def __post_init__(self) -> None:
        for values in (self.supplier_ids, self.channel_ids, self.listing_ids):
            if any(isinstance(value, bool) or value < 1 for value in values):
                raise ProviderAutoPoolError("provider_auto_pool_owned_identity_invalid")

    def owns(self, item: Mapping[str, Any]) -> bool:
        return (
            _positive_int(item.get("supplier_id")) in self.supplier_ids
            or _positive_int(item.get("channel_id") or item.get("id")) in self.channel_ids
            or _positive_int(item.get("listing_id")) in self.listing_ids
        )


_EMPTY_OWNED_RESOURCES = OwnedMarketplaceResources()


@dataclass(frozen=True)
class ProviderAutoPoolTarget:
    a6_user_id: int
    token_id: int
    model: str

    def __post_init__(self) -> None:
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 1
            for value in (self.a6_user_id, self.token_id)
        ) or not str(self.model or "").strip():
            raise ProviderAutoPoolError("provider_auto_pool_target_invalid")


@dataclass(frozen=True)
class ProviderAutoPoolRemoteResult:
    subscription_id: int
    disabled_channel_ids: tuple[int, ...]
    confirmed_disabled_channel_ids: tuple[int, ...]
    visible_owned_channel_ids: tuple[int, ...]
    absent_owned_channel_ids: tuple[int, ...]
    unpinned: bool
    max_ratio: Decimal
    subscription: Mapping[str, Any]


class ProviderAutoPoolClient(Protocol):
    def marketplace_channels(self, user_id: int, **kwargs: Any) -> Mapping[str, Any]: ...

    def marketplace_disable_channel(self, user_id: int, **kwargs: Any) -> Mapping[str, Any]: ...

    def marketplace_pins(self, user_id: int) -> list[dict[str, Any]]: ...

    def marketplace_unpin(self, user_id: int, **kwargs: Any) -> Mapping[str, Any]: ...

    def marketplace_subscriptions(self, user_id: int) -> list[dict[str, Any]]: ...

    def marketplace_subscription(self, user_id: int, **kwargs: Any) -> Mapping[str, Any]: ...

    def marketplace_create_auto_pool(self, user_id: int, **kwargs: Any) -> Mapping[str, Any]: ...

    def marketplace_update_auto_pool(self, user_id: int, **kwargs: Any) -> Mapping[str, Any]: ...

    def supplier_listings(self, user_id: int, **kwargs: Any) -> Mapping[str, Any]: ...

    def resubmit_supplier_listing(self, user_id: int, listing_id: int) -> Mapping[str, Any]: ...


@dataclass(frozen=True)
class ProviderOwnedChannelBanResult:
    newly_disabled_channel_ids: tuple[int, ...]
    confirmed_disabled_channel_ids: tuple[int, ...]
    snapshot: tuple[dict[str, Any], ...]


def plan_provider_auto_pool(
    quotes: Sequence[AutoPoolMarketQuote],
    *,
    model: str,
    owned: OwnedMarketplaceResources = _EMPTY_OWNED_RESOURCES,
    minimum_observers: int = 2,
    target_channel_count: int = 5,
    hard_max_ratio: Decimal = Decimal("0.3"),
    expand_above_ratio: Decimal | None = None,
) -> AutoPoolPlan:
    model = str(model or "").strip()
    hard_max_ratio = _decimal(hard_max_ratio, "provider_auto_pool_hard_max_invalid")
    if expand_above_ratio is not None:
        expand_above_ratio = _decimal(
            expand_above_ratio,
            "provider_auto_pool_expand_ratio_invalid",
        )
    if (
        not model
        or isinstance(minimum_observers, bool)
        or minimum_observers < 1
        or isinstance(target_channel_count, bool)
        or target_channel_count < 1
        or hard_max_ratio <= 0
        or (
            expand_above_ratio is not None
            and expand_above_ratio >= hard_max_ratio
        )
    ):
        raise ProviderAutoPoolError("provider_auto_pool_plan_config_invalid")

    grouped: dict[int, dict[int, AutoPoolMarketQuote]] = {}
    for quote in quotes:
        if not isinstance(quote, AutoPoolMarketQuote):
            raise ProviderAutoPoolError("provider_auto_pool_quote_invalid")
        if quote.model != model:
            continue
        grouped.setdefault(quote.channel_id, {})[quote.observer_merchant_id] = quote

    eligible: list[AutoPoolCandidate] = []
    excluded: list[tuple[int, str]] = []
    for channel_id, by_observer in grouped.items():
        observations = tuple(by_observer.values())
        representative = min(observations, key=lambda item: item.observer_merchant_id)
        if (
            channel_id in owned.channel_ids
            or representative.supplier_id in owned.supplier_ids
            or representative.listing_id in owned.listing_ids
        ):
            excluded.append((channel_id, "owned"))
            continue
        healthy = tuple(
            item
            for item in observations
            if item.available
            and item.health_state.strip().casefold() in _READY_HEALTH_STATES
            and item.realtime_ratio_cny is not None
            and item.realtime_ratio_cny > 0
            and item.realtime_ratio_usd is not None
            and item.realtime_ratio_usd > 0
        )
        if len(healthy) < minimum_observers:
            excluded.append((channel_id, "observer_quorum"))
            continue
        if len(healthy) <= len(observations) - len(healthy):
            excluded.append((channel_id, "health_conflict"))
            continue
        supplier_ids = {item.supplier_id for item in healthy if item.supplier_id is not None}
        if len(supplier_ids) > 1:
            excluded.append((channel_id, "supplier_conflict"))
            continue
        ratio = median(item.realtime_ratio_cny for item in healthy)
        ratio_usd = median(item.realtime_ratio_usd for item in healthy)
        if ratio > hard_max_ratio:
            excluded.append((channel_id, "above_hard_max"))
            continue
        successful = [item.last_success_at for item in healthy if item.last_success_at is not None]
        latencies = [item.latency_ms for item in healthy if item.latency_ms is not None]
        eligible.append(
            AutoPoolCandidate(
                channel_id=channel_id,
                supplier_id=next(iter(supplier_ids), representative.supplier_id),
                listing_id=representative.listing_id,
                ratio_cny=ratio,
                ratio_usd=ratio_usd,
                observer_ids=tuple(sorted(item.observer_merchant_id for item in healthy)),
                last_success_at=max(successful, default=None),
                latency_ms=median(latencies) if latencies else None,
            )
        )

    eligible.sort(key=_candidate_sort_key)
    if expand_above_ratio is None:
        selected = tuple(eligible[:target_channel_count])
    else:
        boundary_index = next(
            (
                index
                for index, candidate in enumerate(eligible)
                if candidate.ratio_cny > expand_above_ratio
            ),
            None,
        )
        if boundary_index is None:
            raise ProviderAutoPoolError("provider_auto_pool_expansion_exhausted")
        selected = tuple(eligible[: boundary_index + 1])
    if not selected:
        raise ProviderAutoPoolError("provider_auto_pool_no_eligible_channels")
    boundary = selected[-1]
    max_ratio = boundary.ratio_cny
    max_ratio_usd = boundary.ratio_usd
    document = {
        "algorithm": "observer-cny-top-range-v1",
        "model": model,
        "minimum_observers": minimum_observers,
        "target_channel_count": target_channel_count,
        "hard_max_ratio": _decimal_text(hard_max_ratio),
        "boundary_mode": (
            "initial_top_n" if expand_above_ratio is None else "strict_next_price_band"
        ),
        "expand_above_ratio": (
            _decimal_text(expand_above_ratio)
            if expand_above_ratio is not None
            else None
        ),
        "owned_supplier_ids": sorted(owned.supplier_ids),
        "owned_channel_ids": sorted(owned.channel_ids),
        "owned_listing_ids": sorted(owned.listing_ids),
        "selected": [
            {
                "channel_id": item.channel_id,
                "supplier_id": item.supplier_id,
                "listing_id": item.listing_id,
                "ratio_cny": _decimal_text(item.ratio_cny),
                "ratio_usd": _decimal_text(item.ratio_usd),
                "observer_ids": list(item.observer_ids),
            }
            for item in selected
        ],
    }
    source_hash = hashlib.sha256(
        json.dumps(document, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return AutoPoolPlan(
        model=model,
        min_ratio=Decimal("0"),
        max_ratio=max_ratio,
        max_ratio_usd=max_ratio_usd,
        selected=selected,
        eligible=tuple(eligible),
        excluded=tuple(sorted(excluded)),
        source_hash=source_hash,
    )


def reconcile_provider_auto_pool(
    client: ProviderAutoPoolClient,
    target: ProviderAutoPoolTarget,
    plan: AutoPoolPlan,
    owned: OwnedMarketplaceResources,
    *,
    page_size: int = 100,
    owned_listings_by_channel: Mapping[int, int] | None = None,
) -> ProviderAutoPoolRemoteResult:
    if plan.model != target.model:
        raise ProviderAutoPoolError("provider_auto_pool_plan_model_mismatch")

    ban = reconcile_provider_owned_channel_ban(
        client,
        target,
        owned,
        page_size=page_size,
        owned_listings_by_channel=owned_listings_by_channel,
    )
    banned_owned = _owned_channels(ban.snapshot, owned)

    pins = client.marketplace_pins(target.a6_user_id)
    matching_pins = [item for item in pins if _matches_token_model(item, target)]
    if len(matching_pins) > 1:
        raise ProviderAutoPoolError("provider_auto_pool_duplicate_pins")
    if matching_pins:
        client.marketplace_unpin(
            target.a6_user_id,
            token_id=target.token_id,
            model=target.model,
        )
        observed_pins = client.marketplace_pins(target.a6_user_id)
        if any(_matches_token_model(item, target) for item in observed_pins):
            raise ProviderAutoPoolError("provider_auto_pool_unpin_readback_failed")

    subscriptions = [
        item
        for item in client.marketplace_subscriptions(target.a6_user_id)
        if _matches_token_model(item, target)
    ]
    if len(subscriptions) > 1:
        raise ProviderAutoPoolError("provider_auto_pool_duplicate_subscriptions")
    if subscriptions:
        subscription_id = _required_id(
            subscriptions[0].get("id"),
            "provider_auto_pool_subscription_identity_invalid",
        )
        client.marketplace_update_auto_pool(
            target.a6_user_id,
            subscription_id=subscription_id,
            min_ratio=plan.min_ratio,
            max_ratio=plan.max_ratio,
        )
    else:
        created = client.marketplace_create_auto_pool(
            target.a6_user_id,
            token_id=target.token_id,
            model=target.model,
            min_ratio=plan.min_ratio,
            max_ratio=plan.max_ratio,
        )
        subscription_id = _required_id(
            created.get("id"),
            "provider_auto_pool_subscription_identity_invalid",
        )

    subscription = dict(
        client.marketplace_subscription(
            target.a6_user_id,
            subscription_id=subscription_id,
        )
    )
    excluded_channel_ids = frozenset({*owned.channel_ids, *banned_owned})
    _verify_subscription(
        subscription,
        target=target,
        plan=plan,
        excluded_channel_ids=excluded_channel_ids,
    )

    final_snapshot = marketplace_channel_snapshot(client, target, page_size=page_size)
    _verify_known_owned_visible(final_snapshot, owned)
    final_owned = _owned_channels(final_snapshot, owned)
    if any(not _user_disabled(item) for item in final_owned.values()):
        raise ProviderAutoPoolError("provider_auto_pool_owned_channel_readback_failed")
    return ProviderAutoPoolRemoteResult(
        subscription_id=subscription_id,
        disabled_channel_ids=ban.newly_disabled_channel_ids,
        confirmed_disabled_channel_ids=tuple(sorted(final_owned)),
        visible_owned_channel_ids=tuple(sorted(final_owned)),
        absent_owned_channel_ids=(),
        unpinned=bool(matching_pins),
        max_ratio=plan.max_ratio,
        subscription=subscription,
    )


def reconcile_provider_owned_channel_ban(
    client: ProviderAutoPoolClient,
    target: ProviderAutoPoolTarget,
    owned: OwnedMarketplaceResources,
    *,
    page_size: int = 100,
    owned_listings_by_channel: Mapping[int, int] | None = None,
) -> ProviderOwnedChannelBanResult:
    before = marketplace_channel_snapshot(client, target, page_size=page_size)
    missing = owned.channel_ids - {
        _required_id(
            item.get("channel_id") or item.get("id"),
            "provider_auto_pool_marketplace_channel_invalid",
        )
        for item in before
    }
    if missing:
        # 下架（status=0）的 listing 不出现在市场搜索中，因此 owned 渠道必须
        # 先临时恢复上架才可执行并审计禁用（ban）。上架后保持上架+banned：
        # 实证表明 DOWN+banned 会再次从市场搜索中消失，导致审计无法通过。
        if owned_listings_by_channel is None:
            raise ProviderAutoPoolError("provider_auto_pool_owned_resource_not_visible")
        _relist_owned_channels(
            client,
            target,
            missing,
            owned_listings_by_channel,
        )
        before = marketplace_channel_snapshot(client, target, page_size=page_size)
    _verify_known_owned_visible(before, owned)
    visible_owned = _owned_channels(before, owned)
    newly_disabled: list[int] = []
    for channel_id, item in visible_owned.items():
        if _user_disabled(item):
            continue
        client.marketplace_disable_channel(
            target.a6_user_id,
            channel_id=channel_id,
            model=target.model,
        )
        newly_disabled.append(channel_id)

    snapshot = marketplace_channel_snapshot(client, target, page_size=page_size)
    _verify_known_owned_visible(snapshot, owned)
    confirmed_owned = _owned_channels(snapshot, owned)
    if any(not _user_disabled(item) for item in confirmed_owned.values()):
        raise ProviderAutoPoolError("provider_auto_pool_owned_channel_readback_failed")
    if set(visible_owned) - set(confirmed_owned):
        raise ProviderAutoPoolError("provider_auto_pool_marketplace_snapshot_unstable")
    return ProviderOwnedChannelBanResult(
        newly_disabled_channel_ids=tuple(sorted(newly_disabled)),
        confirmed_disabled_channel_ids=tuple(sorted(confirmed_owned)),
        snapshot=tuple(dict(item) for item in snapshot),
    )


def _relist_owned_channels(
    client: ProviderAutoPoolClient,
    target: ProviderAutoPoolTarget,
    missing_channel_ids: set[int],
    owned_listings_by_channel: Mapping[int, int],
) -> None:
    for channel_id in sorted(missing_channel_ids):
        listing_id = owned_listings_by_channel.get(channel_id)
        if listing_id is None:
            raise ProviderAutoPoolError("provider_auto_pool_owned_relist_mapping_missing")
        client.resubmit_supplier_listing(
            target.a6_user_id,
            listing_id=listing_id,
        )
    observed = _supplier_listing_state_by_id(client, target.a6_user_id)
    still_down = [
        (channel_id, owned_listings_by_channel[channel_id])
        for channel_id in sorted(missing_channel_ids)
        if int(observed.get(owned_listings_by_channel[channel_id], {}).get("status") or 0) != 2
    ]
    if still_down:
        raise ProviderAutoPoolError("provider_auto_pool_owned_relist_readback_failed")


def _supplier_listing_state_by_id(
    client: ProviderAutoPoolClient,
    user_id: int,
) -> dict[int, Mapping[str, Any]]:
    result: dict[int, Mapping[str, Any]] = {}
    for page in range(1, 101):
        document = client.supplier_listings(user_id, page=page, size=100)
        items = document.get("items")
        if not isinstance(items, list):
            raise ProviderAutoPoolError("provider_auto_pool_supplier_page_invalid")
        for raw in items:
            if not isinstance(raw, Mapping):
                raise ProviderAutoPoolError("provider_auto_pool_supplier_page_invalid")
            item = dict(raw)
            listing_id = _required_id(
                item.get("id"),
                "provider_auto_pool_supplier_listing_invalid",
            )
            result[listing_id] = item
        total = _nonnegative_int(document.get("total"))
        if not items or len(items) < 100 or (total and len(result) >= total):
            return result
    raise ProviderAutoPoolError("provider_auto_pool_supplier_page_limit")


def marketplace_channel_snapshot(
    client: ProviderAutoPoolClient,
    target: ProviderAutoPoolTarget,
    *,
    page_size: int = 100,
    maximum_pages: int = 100,
) -> tuple[dict[str, Any], ...]:
    if (
        isinstance(page_size, bool)
        or not 1 <= page_size <= 1000
        or isinstance(maximum_pages, bool)
        or not 1 <= maximum_pages <= 1000
    ):
        raise ProviderAutoPoolError("provider_auto_pool_pagination_invalid")
    rows: list[dict[str, Any]] = []
    seen_ids: set[int] = set()
    total: int | None = None
    for page in range(1, maximum_pages + 1):
        payload = client.marketplace_channels(
            target.a6_user_id,
            token_id=target.token_id,
            model=target.model,
            page=page,
            page_size=page_size,
            sort="input_asc",
            route_status="all",
            exclude_unavailable=False,
            include_seed=True,
            include_disabled=True,
            include_user_disabled=True,
        )
        items = payload.get("items")
        if not isinstance(items, list):
            raise ProviderAutoPoolError("provider_auto_pool_marketplace_page_invalid")
        if total is None:
            total = _nonnegative_int(payload.get("total"))
        for raw in items:
            if not isinstance(raw, Mapping):
                raise ProviderAutoPoolError("provider_auto_pool_marketplace_page_invalid")
            item = dict(raw)
            channel_id = _required_id(
                item.get("channel_id") or item.get("id"),
                "provider_auto_pool_marketplace_channel_invalid",
            )
            if channel_id in seen_ids:
                raise ProviderAutoPoolError("provider_auto_pool_marketplace_pagination_unstable")
            seen_ids.add(channel_id)
            rows.append(item)
        if total is not None and len(rows) >= total:
            if len(rows) != total:
                raise ProviderAutoPoolError("provider_auto_pool_marketplace_total_mismatch")
            return tuple(rows)
        if len(items) < page_size:
            if total is not None and len(rows) != total:
                raise ProviderAutoPoolError("provider_auto_pool_marketplace_total_mismatch")
            return tuple(rows)
    raise ProviderAutoPoolError("provider_auto_pool_marketplace_page_limit")


def _owned_channels(
    rows: Sequence[Mapping[str, Any]],
    owned: OwnedMarketplaceResources,
) -> dict[int, Mapping[str, Any]]:
    return {
        _required_id(
            item.get("channel_id") or item.get("id"),
            "provider_auto_pool_marketplace_channel_invalid",
        ): item
        for item in rows
        if owned.owns(item)
    }


def _verify_known_owned_visible(
    rows: Sequence[Mapping[str, Any]],
    owned: OwnedMarketplaceResources,
) -> None:
    observed_channel_ids = {
        _required_id(
            item.get("channel_id") or item.get("id"),
            "provider_auto_pool_marketplace_channel_invalid",
        )
        for item in rows
    }
    observed_listing_ids = {
        value
        for item in rows
        if (value := _positive_int(item.get("listing_id"))) is not None
    }
    if (
        not owned.channel_ids.issubset(observed_channel_ids)
        or not owned.listing_ids.issubset(observed_listing_ids)
    ):
        raise ProviderAutoPoolError("provider_auto_pool_owned_resource_not_visible")


def _user_disabled(item: Mapping[str, Any]) -> bool:
    return item.get("user_channel_disabled") is True and str(
        item.get("route_status") or ""
    ).strip().casefold() == _USER_DISABLED_ROUTE_STATUS


def _matches_token_model(item: Mapping[str, Any], target: ProviderAutoPoolTarget) -> bool:
    return (
        _positive_int(item.get("token_id")) == target.token_id
        and str(item.get("model_name") or item.get("model") or "").strip() == target.model
    )


def _verify_subscription(
    subscription: Mapping[str, Any],
    *,
    target: ProviderAutoPoolTarget,
    plan: AutoPoolPlan,
    excluded_channel_ids: frozenset[int],
) -> None:
    if (
        _positive_int(subscription.get("user_id")) not in {None, target.a6_user_id}
        or _positive_int(subscription.get("token_id")) != target.token_id
        or str(subscription.get("model_name") or "").strip() != target.model
        or subscription.get("auto_pool_enabled") is not True
        or _optional_decimal(subscription.get("min_ratio")) != plan.min_ratio
        or _optional_decimal(subscription.get("max_ratio")) != plan.max_ratio
    ):
        raise ProviderAutoPoolError("provider_auto_pool_subscription_readback_failed")
    channels = subscription.get("channels") or []
    auto_channels = subscription.get("auto_channels")
    if not isinstance(channels, list) or not isinstance(auto_channels, list):
        raise ProviderAutoPoolError("provider_auto_pool_subscription_readback_failed")
    if any(
        isinstance(item, Mapping)
        and str(item.get("state") or "").strip().casefold() == "active"
        and _nonnegative_int(item.get("removed_at")) in {None, 0}
        and _positive_int(item.get("channel_id")) in excluded_channel_ids
        for item in channels
    ):
        raise ProviderAutoPoolError("provider_auto_pool_owned_channel_selected")
    if not auto_channels:
        raise ProviderAutoPoolError("provider_auto_pool_effective_pool_empty")
    for item in auto_channels:
        if not isinstance(item, Mapping):
            raise ProviderAutoPoolError("provider_auto_pool_subscription_readback_failed")
        channel_id = _required_id(
            item.get("channel_id"),
            "provider_auto_pool_subscription_readback_failed",
        )
        ratio_cny = _optional_decimal(item.get("realtime_ratio_cny"))
        if channel_id in excluded_channel_ids:
            raise ProviderAutoPoolError("provider_auto_pool_owned_channel_selected")
        if (
            not _is_effective_auto_pool_channel(item)
            or ratio_cny is None
            or ratio_cny < plan.min_ratio
            or ratio_cny > plan.max_ratio
        ):
            raise ProviderAutoPoolError("provider_auto_pool_subscription_readback_failed")


def _is_effective_auto_pool_channel(item: Mapping[str, Any]) -> bool:
    return (
        str(item.get("state") or "").strip().casefold() == "auto_pool"
        or str(item.get("pool_source") or "").strip().casefold() == "auto"
        or str(item.get("ratio_status") or "").strip().casefold() == "matched"
    )


def _candidate_sort_key(item: AutoPoolCandidate) -> tuple[Any, ...]:
    return (
        item.ratio_cny,
        1 if item.last_success_at is None else 0,
        -(item.last_success_at or 0),
        1 if item.latency_ms is None else 0,
        item.latency_ms or Decimal("0"),
        item.channel_id,
    )


def _decimal(value: object, code: str) -> Decimal:
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ProviderAutoPoolError(code) from None
    if not result.is_finite() or result < 0:
        raise ProviderAutoPoolError(code)
    return result


def _optional_decimal(value: object) -> Decimal | None:
    if value in (None, "") or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return result if result.is_finite() and result >= 0 else None


def _decimal_text(value: Decimal) -> str:
    return format(value, "f")


def _positive_int(value: object) -> int | None:
    if value in (None, "") or isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _required_id(value: object, code: str) -> int:
    parsed = _positive_int(value)
    if parsed is None:
        raise ProviderAutoPoolError(code)
    return parsed


def _nonnegative_int(value: object) -> int | None:
    if value in (None, "") or isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None
