from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

UTC = timezone.utc
TRAFFIC_BUCKET_SECONDS = 10
CURRENT_SIGNAL_MAX_AGE_SECONDS = 30
TRAFFIC_WINDOWS = {
    "5m": 5 * 60,
    "15m": 15 * 60,
    "1h": 60 * 60,
}
K_CNY_BAND_EDGES = tuple(
    Decimal(value)
    for value in (
        "0",
        "0.05",
        "0.06",
        "0.07",
        "0.08",
        "0.09",
        "0.10",
        "0.11",
        "0.12",
        "0.14",
        "0.16",
        "0.20",
        "0.25",
        "0.32",
        "0.40",
        "0.55",
        "0.75",
        "1.00",
        "1.50",
        "2.50",
        "4.00",
        "6.00",
        "10.00",
    )
)


def build_market_traffic_snapshot(
    events: Sequence[Mapping[str, Any]],
    signals: Sequence[Mapping[str, Any]],
    *,
    model: str,
    window: str,
    now: datetime,
) -> dict[str, Any]:
    if window not in TRAFFIC_WINDOWS:
        raise ValueError("market_traffic_window_invalid")
    now = _aware(now)
    duration_seconds = TRAFFIC_WINDOWS[window]
    since = now - timedelta(seconds=duration_seconds)
    bucket_count = duration_seconds // TRAFFIC_BUCKET_SECONDS
    bucket_starts = tuple(
        since + timedelta(seconds=index * TRAFFIC_BUCKET_SECONDS)
        for index in range(bucket_count)
    )
    bands = [_empty_band(index, bucket_starts) for index in range(len(K_CNY_BAND_EDGES))]
    priced_events = 0
    unpriced_events = 0
    all_channels: set[int] = set()
    quorum_events = 0
    owned_events = 0

    for raw in events:
        observed_at = _datetime(raw.get("first_observed_at"))
        if observed_at is None or observed_at < since or observed_at > now + timedelta(seconds=5):
            continue
        channel_id = _positive_int(raw.get("channel_id"))
        if channel_id is None:
            continue
        ratio = _positive_decimal(raw.get("realtime_ratio_cny"))
        if ratio is None:
            unpriced_events += 1
            continue
        band = bands[_band_index(ratio)]
        observer_count = _observer_count(raw.get("observer_ids"), raw.get("observer_count"))
        is_quorum = observer_count >= 2
        is_owned = bool(raw.get("is_owned"))
        bucket_index = min(
            bucket_count - 1,
            max(0, int((observed_at - since).total_seconds()) // TRAFFIC_BUCKET_SECONDS),
        )
        band["_channels"].add(channel_id)
        band["event_lower_bound"] += 1
        band["quorum_event_count"] += int(is_quorum)
        band["owned_event_count"] += int(is_owned)
        band["series"][bucket_index]["event_lower_bound"] += 1
        band["series"][bucket_index]["quorum_event_count"] += int(is_quorum)
        if observed_at >= now - timedelta(seconds=60):
            band["just_now_event_count"] += 1
        elif observed_at >= now - timedelta(seconds=120):
            band["one_minute_ago_event_count"] += 1
        priced_events += 1
        all_channels.add(channel_id)
        quorum_events += int(is_quorum)
        owned_events += int(is_owned)

    latest_observed_at: datetime | None = None
    available_channel_count = 0
    current_channel_count = 0
    current_signal_since = now - timedelta(seconds=CURRENT_SIGNAL_MAX_AGE_SECONDS)
    for raw in signals:
        if str(raw.get("model") or "") != model:
            continue
        observed_at = _datetime(raw.get("last_observed_at"))
        if (
            observed_at is None
            or observed_at < current_signal_since
            or observed_at > now + timedelta(seconds=5)
        ):
            continue
        if observed_at is not None and (
            latest_observed_at is None or observed_at > latest_observed_at
        ):
            latest_observed_at = observed_at
        current_channel_count += 1
        available_channel_count += int(bool(raw.get("available")))
        ratio = _positive_decimal(raw.get("realtime_ratio_cny"))
        if ratio is not None:
            bands[_band_index(ratio)]["current_channel_count"] += 1

    public_bands: list[dict[str, Any]] = []
    for band in bands:
        current = int(band["just_now_event_count"])
        previous = int(band["one_minute_ago_event_count"])
        trend_ratio = None if previous == 0 else (current - previous) / previous
        if current == previous == 0:
            trend = "idle"
        elif previous == 0:
            trend = "new"
        elif trend_ratio is not None and trend_ratio >= 0.25:
            trend = "up"
        elif trend_ratio is not None and trend_ratio <= -0.25:
            trend = "down"
        else:
            trend = "flat"
        band["active_channel_count"] = len(band.pop("_channels"))
        band["just_now_rate_per_minute"] = current
        band["one_minute_ago_rate_per_minute"] = previous
        band["trend"] = trend
        band["trend_ratio"] = trend_ratio
        band["share"] = (
            band["event_lower_bound"] / priced_events if priced_events else 0.0
        )
        public_bands.append(band)

    return {
        "model": model,
        "window": window,
        "generated_at": now,
        "method": {
            "signal": "last_success_at_transition",
            "count_semantics": "lower_bound_active_seconds",
            "observer_window_seconds": 10,
            "bucket_seconds": TRAFFIC_BUCKET_SECONDS,
            "current_signal_max_age_seconds": CURRENT_SIGNAL_MAX_AGE_SECONDS,
            "current_comparison_seconds": 60,
            "automatic_repricing": False,
        },
        "freshness": {
            "last_observed_at": latest_observed_at,
            "age_seconds": (
                max(0, round((now - latest_observed_at).total_seconds()))
                if latest_observed_at is not None
                else None
            ),
            "current_channel_count": current_channel_count,
            "available_channel_count": available_channel_count,
        },
        "totals": {
            "event_lower_bound": priced_events,
            "quorum_event_count": quorum_events,
            "active_channel_count": len(all_channels),
            "owned_event_count": owned_events,
            "unpriced_event_count": unpriced_events,
        },
        "bands": public_bands,
    }


def _empty_band(index: int, bucket_starts: Sequence[datetime]) -> dict[str, Any]:
    lower = K_CNY_BAND_EDGES[index]
    upper = (
        K_CNY_BAND_EDGES[index + 1]
        if index + 1 < len(K_CNY_BAND_EDGES)
        else None
    )
    return {
        "band_id": _band_id(lower, upper),
        "label": _band_label(lower, upper),
        "minimum_k_cny": str(lower),
        "maximum_k_cny": str(upper) if upper is not None else None,
        "current_channel_count": 0,
        "event_lower_bound": 0,
        "quorum_event_count": 0,
        "owned_event_count": 0,
        "active_channel_count": 0,
        "just_now_event_count": 0,
        "one_minute_ago_event_count": 0,
        "just_now_rate_per_minute": 0,
        "one_minute_ago_rate_per_minute": 0,
        "trend": "idle",
        "trend_ratio": None,
        "share": 0.0,
        "series": [
            {
                "bucket_start": bucket_start,
                "event_lower_bound": 0,
                "quorum_event_count": 0,
            }
            for bucket_start in bucket_starts
        ],
        "_channels": set(),
    }


def _band_index(value: Decimal) -> int:
    for index, upper in enumerate(K_CNY_BAND_EDGES[1:]):
        if value < upper:
            return index
    return len(K_CNY_BAND_EDGES) - 1


def _band_id(lower: Decimal, upper: Decimal | None) -> str:
    def token(value: Decimal) -> str:
        return f"{int(value * 1000):05d}"

    return f"k_cny_{token(lower)}_{token(upper) if upper is not None else 'plus'}"


def _band_label(lower: Decimal, upper: Decimal | None) -> str:
    if upper is None:
        return f"K_CNY >= {_decimal_label(lower)}"
    return f"K_CNY {_decimal_label(lower)}-{_decimal_label(upper)}"


def _decimal_label(value: Decimal) -> str:
    return format(value.normalize(), "f")


def _aware(value: datetime) -> datetime:
    if not isinstance(value, datetime):
        raise ValueError("market_traffic_clock_invalid")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("market_traffic_clock_invalid")
    return value.astimezone(UTC)


def _datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _positive_decimal(value: Any) -> Decimal | None:
    if value in (None, "") or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return result if result.is_finite() and result > 0 else None


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        result = int(value)
    except (TypeError, ValueError):
        return None
    return result if result > 0 else None


def _observer_count(raw_ids: Any, raw_count: Any) -> int:
    if isinstance(raw_ids, (list, tuple)):
        return len({_positive_int(item) for item in raw_ids} - {None})
    return _positive_int(raw_count) or 0
