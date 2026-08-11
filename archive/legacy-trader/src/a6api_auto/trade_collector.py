from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable, Iterable, Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from .client import A6ApiClient
from .errors import A6AutoError, A6HttpError
from .models import SessionRecord
from .storage import SessionStore

UTC = timezone.utc


def _utc_iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, UTC).isoformat().replace("+00:00", "Z")


def _integer(value: Any, *, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _optional_integer(value: Any) -> int | None:
    if value in (None, "") or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _text(value: Any, *, limit: int = 500) -> str:
    text = str(value or "")
    return text[:limit]


def _source_hash(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


@dataclass(frozen=True)
class CollectorWatermarks:
    earning_id: int = 0
    provider_log_id: int = 0


@dataclass(frozen=True)
class FinancialSnapshot:
    merchant_user_id: int
    supplier_id: int
    quota_per_unit: int
    user_balance_quota: int
    used_quota: int
    request_count: int
    withdrawable_micros: int
    withdrawing_micros: int
    lifetime_earned_micros: int
    paid_micros: int
    today_earned_micros: int
    month_earned_micros: int
    today_calls: int
    month_calls: int
    settlement_pending_micros: int
    settlement_settled_micros: int
    settlement_total_income_micros: int
    settlement_week_new_micros: int
    settlement_abnormal_count: int
    markup_percent: int
    observed_at: str


@dataclass(frozen=True)
class SupplierProfileSnapshot:
    merchant_user_id: int
    supplier_id: int
    name: str
    description: str
    observed_at: str


@dataclass(frozen=True)
class SupplierChannelSnapshot:
    merchant_user_id: int
    a6_channel_id: int
    supplier_id: int | None
    name: str
    nickname: str
    channel_type: int | None
    status: int | None
    availability: int | None
    observed_at: str
    source_hash: str


@dataclass(frozen=True)
class SupplierListingSnapshot:
    merchant_user_id: int
    a6_listing_id: int
    a6_channel_id: int
    own_model: str
    platform_model: str
    charge_type: str
    input_price_micros: int
    output_price_micros: int
    cache_read_price_micros: int
    cache_write_price_micros: int
    status: int | None
    availability: int | None
    visibility: int | None
    display_name: str
    description: str
    observed_at: str
    source_hash: str


@dataclass(frozen=True)
class EarningEvent:
    merchant_user_id: int
    earning_id: int
    log_id: int | None
    request_id: str
    channel_id: int | None
    channel_name: str
    platform_model: str
    own_model: str
    prompt_tokens: int
    completion_tokens: int
    cache_tokens: int
    quota_micros: int
    earned_micros: int
    market_quota_micros: int
    supplier_earn_micros: int
    markup_percent: int
    result: str
    use_time_seconds: int
    created_at: int


@dataclass(frozen=True)
class ProviderUsageEvent:
    merchant_user_id: int
    log_id: int
    request_id: str
    token_id: int | None
    token_name: str
    channel_id: int | None
    channel_name: str
    model_name: str
    route_type: str
    quota: int
    prompt_tokens: int
    completion_tokens: int
    use_time_seconds: int
    result_type: int
    created_at: int


def provider_usage_fingerprint(event: ProviderUsageEvent) -> str:
    """Hash fields that remain stable when A6 renumbers window-relative log IDs."""
    stable = {
        "merchant_user_id": event.merchant_user_id,
        "token_id": event.token_id,
        "channel_id": event.channel_id,
        "model_name": event.model_name,
        "route_type": event.route_type,
        "quota": event.quota,
        "prompt_tokens": event.prompt_tokens,
        "completion_tokens": event.completion_tokens,
        "use_time_seconds": event.use_time_seconds,
        "result_type": event.result_type,
        "created_at": event.created_at,
    }
    encoded = json.dumps(stable, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def provider_usage_identity(event: ProviderUsageEvent) -> str:
    if event.request_id:
        digest = hashlib.sha256(event.request_id.encode()).hexdigest()
        return f"request:{digest}"
    return f"fingerprint:{provider_usage_fingerprint(event)}"


def provider_usage_storage_key(event: ProviderUsageEvent) -> str:
    return f"v2:{provider_usage_identity(event)}"


@dataclass(frozen=True)
class PinSnapshot:
    merchant_user_id: int
    token_id: int | None
    channel_id: int | None
    model_name: str
    status: str
    fallback_to_smart_routing: bool
    observed_at: str


@dataclass(frozen=True)
class TokenSnapshot:
    merchant_user_id: int
    token_id: int
    token_name: str
    status: str
    unlimited_quota: bool
    expired_time: int | None
    observed_at: str


@dataclass(frozen=True)
class CollectionBatch:
    snapshot: FinancialSnapshot
    tokens: tuple[TokenSnapshot, ...]
    earnings: tuple[EarningEvent, ...]
    provider_usage: tuple[ProviderUsageEvent, ...]
    pins: tuple[PinSnapshot, ...]
    next_watermarks: CollectorWatermarks
    channels: tuple[SupplierChannelSnapshot, ...] = ()
    listings: tuple[SupplierListingSnapshot, ...] = ()
    supplier_profile: SupplierProfileSnapshot | None = None

    def summary(self) -> dict[str, Any]:
        return {
            "merchant_user_id": self.snapshot.merchant_user_id,
            "snapshot": asdict(self.snapshot),
            "channels": len(self.channels),
            "listings": len(self.listings),
            "tokens": len(self.tokens),
            "earning_events": len(self.earnings),
            "provider_usage_events": len(self.provider_usage),
            "pins": len(self.pins),
            "next_watermarks": asdict(self.next_watermarks),
        }


@dataclass(frozen=True)
class CollectionFailure:
    session_ref: str
    error_class: str


class CollectionSink(Protocol):
    def persist_collection(self, batch: CollectionBatch) -> None: ...

    def record_collection_failure(self, session_ref: str, error_class: str) -> None: ...


SessionRefresher = Callable[[SessionStore, SessionRecord, A6ApiClient], None]


class A6ReadCollector:
    def __init__(
        self,
        client: A6ApiClient,
        *,
        now: Callable[[], float] = time.time,
        max_pages: int = 5,
        page_size: int = 100,
        provider_log_lookback_seconds: int = 3600,
    ) -> None:
        if max_pages < 1 or page_size < 1 or provider_log_lookback_seconds < 60:
            raise ValueError("invalid collector limits")
        self.client = client
        self.now = now
        self.max_pages = max_pages
        self.page_size = page_size
        self.provider_log_lookback_seconds = provider_log_lookback_seconds

    def collect(
        self,
        record: SessionRecord,
        watermarks: CollectorWatermarks | None = None,
    ) -> CollectionBatch:
        if not record.user_id:
            raise A6AutoError("collector_user_id_missing")
        watermarks = watermarks or CollectorWatermarks()
        user_id = int(record.user_id)
        self.client.import_cookies(record.cookies)
        user = self.client.self_user(user_id)
        if user is None:
            raise A6AutoError("collector_session_invalid")

        try:
            supplier = self.client.supplier_me(user_id)
        except A6AutoError as exc:
            if str(exc) != "supplier_self_failed" or not self._supplier_application_pending(
                user_id
            ):
                raise
            raise A6AutoError("collector_supplier_pending") from None
        earnings_summary = self.client.supplier_earnings_summary(user_id)
        settlements_summary = self.client.supplier_settlements_summary(user_id)
        withdraw_summary = self.client.supplier_withdraw_summary(user_id)
        observed_epoch = self.now()
        observed_at = _utc_iso(observed_epoch)
        supplier_profile = _supplier_profile_snapshot(user_id, supplier, observed_at)

        snapshot = FinancialSnapshot(
            merchant_user_id=user_id,
            supplier_id=_integer(supplier.get("id")),
            quota_per_unit=record.quota_per_unit,
            user_balance_quota=_integer(user.get("quota")),
            used_quota=_integer(user.get("used_quota")),
            request_count=_integer(user.get("request_count")),
            withdrawable_micros=_integer(withdraw_summary.get("balance_available_micros")),
            withdrawing_micros=_integer(withdraw_summary.get("withdrawing_micros")),
            lifetime_earned_micros=_integer(withdraw_summary.get("total_earned_micros")),
            paid_micros=_integer(withdraw_summary.get("total_paid_micros")),
            today_earned_micros=_integer(earnings_summary.get("today_earned_micros")),
            month_earned_micros=_integer(earnings_summary.get("month_earned_micros")),
            today_calls=_integer(earnings_summary.get("today_calls")),
            month_calls=_integer(earnings_summary.get("month_calls")),
            settlement_pending_micros=_integer(settlements_summary.get("pending_micros")),
            settlement_settled_micros=_integer(settlements_summary.get("settled_micros")),
            settlement_total_income_micros=_integer(
                settlements_summary.get("total_income_micros")
            ),
            settlement_week_new_micros=_integer(settlements_summary.get("week_new_micros")),
            settlement_abnormal_count=_integer(settlements_summary.get("abnormal_count")),
            markup_percent=_integer(supplier.get("markup_percent")),
            observed_at=observed_at,
        )

        channels_by_id: dict[int, SupplierChannelSnapshot] = {}
        for item in self.client.supplier_channels(user_id):
            if _integer(item.get("id")) < 1:
                raise A6AutoError("supplier_channels_inventory_invalid")
            channel = _supplier_channel_snapshot(user_id, item, observed_at)
            channels_by_id[channel.a6_channel_id] = channel
        channels = tuple(channels_by_id[channel_id] for channel_id in sorted(channels_by_id))
        listings = self._collect_listings(user_id, observed_at)
        earnings = self._collect_earnings(user_id, watermarks.earning_id)
        provider_usage = self._collect_provider_usage(
            user_id,
            watermarks.provider_log_id,
            end_timestamp=int(observed_epoch),
        )
        tokens = tuple(
            _token_snapshot(user_id, item, observed_at)
            for item in self.client.list_tokens(user_id)
            if _integer(item.get("id")) > 0
        )
        pins = tuple(
            _pin_snapshot(user_id, item, observed_at)
            for item in self.client.marketplace_pins(user_id)
        )
        next_watermarks = CollectorWatermarks(
            earning_id=max((event.earning_id for event in earnings), default=watermarks.earning_id),
            provider_log_id=watermarks.provider_log_id,
        )
        return CollectionBatch(
            snapshot=snapshot,
            channels=channels,
            listings=listings,
            tokens=tokens,
            earnings=tuple(sorted(earnings, key=lambda event: event.earning_id)),
            provider_usage=tuple(sorted(provider_usage, key=lambda event: event.log_id)),
            pins=pins,
            next_watermarks=next_watermarks,
            supplier_profile=supplier_profile,
        )

    def _supplier_application_pending(self, user_id: int) -> bool:
        try:
            requests = self.client.supplier_requests(user_id)
        except A6AutoError:
            return False
        if not requests:
            return False
        latest = max(requests, key=lambda item: _integer(item.get("id")))
        raw_status = latest.get("status")
        return raw_status == 0 or str(raw_status).strip().lower() in {
            "pending",
            "reviewing",
        }

    def _collect_listings(
        self,
        user_id: int,
        observed_at: str,
    ) -> tuple[SupplierListingSnapshot, ...]:
        by_id: dict[int, SupplierListingSnapshot] = {}
        expected_total: int | None = None
        complete = False
        for page in range(1, self.max_pages + 1):
            data = self.client.supplier_listings(user_id, page=page, size=self.page_size)
            items = data["items"]
            total = _optional_integer(data.get("total"))
            if total is not None:
                if total < 0 or (expected_total is not None and total != expected_total):
                    raise A6AutoError("supplier_listings_inventory_inconsistent")
                expected_total = total
            for item in items:
                listing_id = _integer(item.get("id"))
                if listing_id < 1:
                    raise A6AutoError("supplier_listings_inventory_invalid")
                listing = _supplier_listing_snapshot(
                    user_id,
                    item,
                    observed_at,
                )
                if listing.a6_channel_id < 1:
                    raise A6AutoError("supplier_listings_inventory_invalid")
                by_id[listing_id] = listing
            if expected_total is not None and len(by_id) > expected_total:
                raise A6AutoError("supplier_listings_inventory_inconsistent")
            if expected_total is not None and len(by_id) == expected_total:
                complete = True
                break
            if not items or len(items) < self.page_size:
                complete = expected_total is None or len(by_id) >= expected_total
                break
        if not complete:
            raise A6AutoError("supplier_listings_inventory_incomplete")
        return tuple(by_id[listing_id] for listing_id in sorted(by_id))

    def _collect_earnings(self, user_id: int, watermark: int) -> list[EarningEvent]:
        events: list[EarningEvent] = []
        for page in range(1, self.max_pages + 1):
            data = self.client.supplier_earnings(user_id, page=page, size=self.page_size)
            items = data["items"]
            if not items:
                break
            stop = False
            for item in items:
                event_id = _integer(item.get("id"))
                if event_id <= watermark:
                    stop = True
                    continue
                if event_id > 0:
                    events.append(_earning_event(user_id, item))
            if stop or len(items) < self.page_size:
                break
        return events

    def _collect_provider_usage(
        self,
        user_id: int,
        _watermark: int,
        *,
        end_timestamp: int,
    ) -> list[ProviderUsageEvent]:
        events: list[ProviderUsageEvent] = []
        seen: set[str] = set()
        start_timestamp = end_timestamp - self.provider_log_lookback_seconds
        for page in range(1, self.max_pages + 1):
            data = self.client.user_logs(
                user_id,
                page=page,
                page_size=self.page_size,
                start_timestamp=start_timestamp,
                end_timestamp=end_timestamp,
            )
            items = data["items"]
            if not items:
                break
            for item in items:
                if not isinstance(item, Mapping):
                    continue
                event = _provider_usage_event(user_id, item)
                if not event.request_id and event.created_at <= 0:
                    continue
                identity = provider_usage_identity(event)
                if identity not in seen:
                    events.append(event)
                    seen.add(identity)
            if len(items) < self.page_size:
                break
        return events


class A6CollectionService:
    def __init__(
        self,
        *,
        client_factory: Callable[[SessionRecord], A6ApiClient],
        watermark_loader: Callable[[int], CollectorWatermarks],
        sink: CollectionSink,
        now: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        transport_retries: int = 1,
        max_workers: int = 1,
        session_refresher: SessionRefresher | None = None,
    ) -> None:
        if transport_retries < 0 or transport_retries > 3:
            raise ValueError("collector transport retries invalid")
        if isinstance(max_workers, bool) or not 1 <= max_workers <= 16:
            raise ValueError("collector max workers invalid")
        self.client_factory = client_factory
        self.watermark_loader = watermark_loader
        self.sink = sink
        self.now = now
        self.sleep = sleep
        self.transport_retries = transport_retries
        self.max_workers = max_workers
        self.session_refresher = session_refresher

    def collect_paths(
        self,
        session_paths: Iterable[Path],
    ) -> tuple[int, tuple[CollectionFailure, ...]]:
        paths = tuple(session_paths)
        if self.max_workers == 1 or len(paths) < 2:
            outcomes = tuple(self._collect_path(path) for path in paths)
        else:
            with ThreadPoolExecutor(
                max_workers=min(self.max_workers, len(paths)),
                thread_name_prefix="a6-collector",
            ) as executor:
                outcomes = tuple(executor.map(self._collect_path, paths))
        completed = sum(outcome is True for outcome in outcomes)
        failures = tuple(
            outcome for outcome in outcomes if isinstance(outcome, CollectionFailure)
        )
        return completed, failures

    def _collect_path(self, path: Path) -> bool | CollectionFailure | None:
        try:
            store = SessionStore(path)
            record = store.load()
            if record.status != "supplier_submitted" or not record.user_id:
                return None
            watermarks = self.watermark_loader(int(record.user_id))
            expected_user_id = int(record.user_id)
            transient_failures = 0
            session_refreshed = False
            while True:
                try:
                    client = self.client_factory(record)
                    collector = A6ReadCollector(client, now=self.now)
                    batch = collector.collect(record, watermarks)
                    try:
                        self.sink.persist_collection(batch)
                    except Exception:
                        raise A6AutoError("collector_persist_failed") from None
                    return True
                except A6AutoError as exc:
                    error_class = str(exc)
                    if error_class == "collector_supplier_pending":
                        return None
                    if (
                        error_class == "collector_session_invalid"
                        and self.session_refresher is not None
                        and not session_refreshed
                    ):
                        self.session_refresher(store, record, client)
                        refreshed = store.load()
                        if (
                            not isinstance(refreshed, SessionRecord)
                            or refreshed.status != "supplier_submitted"
                            or refreshed.user_id != expected_user_id
                        ):
                            raise A6AutoError("collector_session_refresh_invalid") from None
                        record = refreshed
                        session_refreshed = True
                        continue
                    if _transient_read_failure(exc) and transient_failures < self.transport_retries:
                        transient_failures += 1
                        self.sleep(1)
                        continue
                    raise
        except A6AutoError as exc:
            return self._failure(path, str(exc))
        except Exception:
            return self._failure(path, "collector_internal_error")
        return None
    def _failure(self, path: Path, error_class: str) -> CollectionFailure:
        failure = CollectionFailure(session_ref=str(path), error_class=error_class)
        with suppress(Exception):
            self.sink.record_collection_failure(
                failure.session_ref,
                failure.error_class,
            )
        return failure


def _transient_read_failure(exc: A6AutoError) -> bool:
    return str(exc) == "a6_transport_failed" or (
        isinstance(exc, A6HttpError) and exc.transient
    )


def _earning_event(user_id: int, item: Mapping[str, Any]) -> EarningEvent:
    return EarningEvent(
        merchant_user_id=user_id,
        earning_id=_integer(item.get("id")),
        log_id=_optional_integer(item.get("log_id")),
        request_id=_text(item.get("request_id"), limit=128),
        channel_id=_optional_integer(item.get("channel_id")),
        channel_name=_text(item.get("channel_name"), limit=128),
        platform_model=_text(item.get("platform_model"), limit=128),
        own_model=_text(item.get("own_model"), limit=128),
        prompt_tokens=_integer(item.get("prompt_tokens")),
        completion_tokens=_integer(item.get("completion_tokens")),
        cache_tokens=_integer(item.get("cache_tokens")),
        quota_micros=_integer(item.get("quota_micros")),
        earned_micros=_integer(item.get("earned_micros")),
        market_quota_micros=_integer(item.get("user_market_quota")),
        supplier_earn_micros=_integer(item.get("supplier_earn_quota")),
        markup_percent=_integer(item.get("markup_percent")),
        result=_text(item.get("result"), limit=64),
        use_time_seconds=_integer(item.get("use_time")),
        created_at=_integer(item.get("created_at")),
    )


def _provider_usage_event(user_id: int, item: Mapping[str, Any]) -> ProviderUsageEvent:
    return ProviderUsageEvent(
        merchant_user_id=user_id,
        log_id=_integer(item.get("id")),
        request_id=_text(item.get("request_id"), limit=128),
        token_id=_optional_integer(item.get("token_id")),
        token_name=_text(item.get("token_name"), limit=128),
        channel_id=_optional_integer(item.get("channel")),
        channel_name=_text(item.get("channel_name"), limit=128),
        model_name=_text(item.get("model_name"), limit=128),
        route_type=_text(item.get("route_type"), limit=64),
        quota=_integer(item.get("quota")),
        prompt_tokens=_integer(item.get("prompt_tokens")),
        completion_tokens=_integer(item.get("completion_tokens")),
        use_time_seconds=_integer(item.get("use_time")),
        result_type=_integer(item.get("type")),
        created_at=_integer(item.get("created_at")),
    )


def _supplier_channel_snapshot(
    user_id: int,
    item: Mapping[str, Any],
    observed_at: str,
) -> SupplierChannelSnapshot:
    payload = {
        "merchant_user_id": user_id,
        "a6_channel_id": _integer(item.get("id")),
        "supplier_id": _optional_integer(item.get("supplier_id")),
        "name": _text(item.get("name"), limit=256),
        "nickname": _text(item.get("nickname"), limit=256),
        "channel_type": _optional_integer(item.get("channel_type")),
        "status": _optional_integer(item.get("status")),
        "availability": _optional_integer(item.get("availability")),
    }
    return SupplierChannelSnapshot(
        **payload,
        observed_at=observed_at,
        source_hash=_source_hash(payload),
    )


def _supplier_profile_snapshot(
    user_id: int,
    item: Mapping[str, Any],
    observed_at: str,
) -> SupplierProfileSnapshot:
    return SupplierProfileSnapshot(
        merchant_user_id=user_id,
        supplier_id=_integer(item.get("id")),
        name=_text(item.get("name"), limit=256),
        description=_text(item.get("description"), limit=2000),
        observed_at=observed_at,
    )


def _supplier_listing_snapshot(
    user_id: int,
    item: Mapping[str, Any],
    observed_at: str,
) -> SupplierListingSnapshot:
    payload = {
        "merchant_user_id": user_id,
        "a6_listing_id": _integer(item.get("id")),
        "a6_channel_id": _integer(item.get("channel_id")),
        "own_model": _text(item.get("own_model"), limit=256),
        "platform_model": _text(item.get("platform_model"), limit=256),
        "charge_type": _text(item.get("charge_type"), limit=64),
        "input_price_micros": _price_micros(item.get("input_price_micros")),
        "output_price_micros": _price_micros(item.get("output_price_micros")),
        "cache_read_price_micros": _price_micros(item.get("cache_read_price_micros")),
        "cache_write_price_micros": _price_micros(item.get("cache_write_price_micros")),
        "status": _optional_integer(item.get("status")),
        "availability": _optional_integer(item.get("availability")),
        "visibility": _optional_integer(item.get("visibility")),
        "display_name": _text(item.get("display_name"), limit=256),
        "description": _text(item.get("description"), limit=2000),
    }
    return SupplierListingSnapshot(
        **payload,
        observed_at=observed_at,
        source_hash=_source_hash(payload),
    )


def _price_micros(value: Any) -> int:
    price = _integer(value)
    if price < 0:
        raise A6AutoError("supplier_listings_inventory_invalid")
    return price


def _pin_snapshot(user_id: int, item: Mapping[str, Any], observed_at: str) -> PinSnapshot:
    return PinSnapshot(
        merchant_user_id=user_id,
        token_id=_optional_integer(item.get("token_id")),
        channel_id=_optional_integer(item.get("channel_id")),
        model_name=_text(item.get("model_name"), limit=128),
        status=_text(item.get("status"), limit=64),
        fallback_to_smart_routing=bool(item.get("fallback_to_smart_routing", False)),
        observed_at=observed_at,
    )


def _token_snapshot(user_id: int, item: Mapping[str, Any], observed_at: str) -> TokenSnapshot:
    status_value = item.get("status")
    return TokenSnapshot(
        merchant_user_id=user_id,
        token_id=_integer(item.get("id")),
        token_name=_text(item.get("name"), limit=128),
        status=_text(status_value, limit=64),
        unlimited_quota=bool(item.get("unlimited_quota", False)),
        expired_time=_optional_integer(item.get("expired_time")),
        observed_at=observed_at,
    )
