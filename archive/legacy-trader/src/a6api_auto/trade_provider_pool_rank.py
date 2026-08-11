"""Deterministic Observer snapshot admission and Provider TopN ranking.

The request plane must be able to consume a stable ordered list without calling the
marketplace.  This module deliberately contains no network or database code: callers
can persist the accepted snapshot and use the exact same functions in shadow and apply
modes.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any


class ProviderPoolRankError(ValueError):
    """Stable validation/rejection code for a market snapshot or candidate row."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


_ELIGIBLE_HEALTH_STATES = frozenset({"healthy", "idle"})
_UNAVAILABLE_HEALTH_STATES = frozenset({"cooling", "unavailable", "quarantined"})


@dataclass(frozen=True)
class Candidate:
    channel_id: int
    model: str
    price_key: Decimal | None = None
    final_multiplier: Decimal | None = None
    input_price: Decimal | None = None
    output_price: Decimal | None = None
    cache_read_price: Decimal | None = None
    cache_write_price: Decimal | None = None
    realtime_ratio_cny: Decimal | None = None
    exchange_rate: Decimal | None = None
    supplier_id: int | None = None
    listing_id: int | None = None
    health_state: str = "healthy"
    available: bool = True
    last_success_at: datetime | None = None
    latency_ewma_ms: Decimal | None = None
    observer_health_count: int = 1
    observer_count: int = 1
    observed_at: datetime | None = None

    def __post_init__(self) -> None:
        if isinstance(self.channel_id, bool) or self.channel_id < 1:
            raise ProviderPoolRankError("provider_candidate_channel_id_invalid")
        if not str(self.model).strip():
            raise ProviderPoolRankError("provider_candidate_model_invalid")
        for name in ("supplier_id", "listing_id"):
            value = getattr(self, name)
            if value is not None and (isinstance(value, bool) or value < 1):
                raise ProviderPoolRankError(f"provider_candidate_{name}_invalid")
        if isinstance(self.available, bool) is False:
            raise ProviderPoolRankError("provider_candidate_available_invalid")
        if self.observer_health_count < 0 or self.observer_count < 0:
            raise ProviderPoolRankError("provider_candidate_observer_count_invalid")
        if self.observer_health_count > self.observer_count:
            raise ProviderPoolRankError("provider_candidate_observer_quorum_invalid")
        for name in (
            "price_key",
            "final_multiplier",
            "input_price",
            "output_price",
            "cache_read_price",
            "cache_write_price",
            "realtime_ratio_cny",
            "exchange_rate",
            "latency_ewma_ms",
        ):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, Decimal) or value < 0):
                raise ProviderPoolRankError(f"provider_candidate_{name}_invalid")


@dataclass(frozen=True)
class ObserverSnapshot:
    model: str
    observed_at: datetime
    status: str
    candidates: tuple[Candidate, ...]
    result_hash: str
    candidate_count: int | None = None
    pages_complete: bool = True
    config_version: str = ""
    sequence: int | None = None
    health_observer_count: int = 0

    def __post_init__(self) -> None:
        if not str(self.model).strip():
            raise ProviderPoolRankError("provider_snapshot_model_invalid")
        _aware(self.observed_at, "provider_snapshot_observed_at_invalid")
        if self.status != "completed":
            raise ProviderPoolRankError("provider_snapshot_incomplete")
        if not isinstance(self.pages_complete, bool) or not self.pages_complete:
            raise ProviderPoolRankError("provider_snapshot_pages_incomplete")
        expected = len(self.candidates) if self.candidate_count is None else self.candidate_count
        if isinstance(expected, bool) or expected < 0 or expected != len(self.candidates):
            raise ProviderPoolRankError("provider_snapshot_candidate_count_mismatch")
        if self.health_observer_count < 0:
            raise ProviderPoolRankError("provider_snapshot_observer_count_invalid")
        if not _is_sha256(self.result_hash):
            raise ProviderPoolRankError("provider_snapshot_hash_invalid")


@dataclass(frozen=True)
class RankedPool:
    top: tuple[Candidate, ...]
    standby: tuple[Candidate, ...]
    eligible: tuple[Candidate, ...]
    excluded: tuple[tuple[int, str], ...]


def _aware(value: datetime, code: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ProviderPoolRankError(code)
    return value.astimezone(timezone.utc)


def _decimal(value: object, code: str) -> Decimal | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ProviderPoolRankError(code)
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ProviderPoolRankError(code) from None
    if not result.is_finite() or result < 0:
        raise ProviderPoolRankError(code)
    return result


def _int_or_none(value: object, code: str) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ProviderPoolRankError(code)
    try:
        result = int(value)
    except (TypeError, ValueError):
        raise ProviderPoolRankError(code) from None
    if result < 1:
        raise ProviderPoolRankError(code)
    return result


def _datetime_or_none(value: object, code: str) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return _aware(value, code)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        raise ProviderPoolRankError(code) from None
    return _aware(parsed, code)


def candidate_from_mapping(value: Mapping[str, Any]) -> Candidate:
    """Normalize a row read from the existing market quote tables."""

    if not isinstance(value, Mapping):
        raise ProviderPoolRankError("provider_candidate_shape_invalid")
    try:
        channel_id = int(value.get("channel_id") or 0)
    except (TypeError, ValueError):
        raise ProviderPoolRankError("provider_candidate_channel_id_invalid") from None
    model = str(value.get("model") or "").strip()
    return Candidate(
        channel_id=channel_id,
        model=model,
        price_key=_decimal(value.get("price_key"), "provider_candidate_price_key_invalid"),
        final_multiplier=_decimal(
            value.get("final_multiplier"), "provider_candidate_final_multiplier_invalid"
        ),
        input_price=_decimal(value.get("input_price"), "provider_candidate_input_price_invalid"),
        output_price=_decimal(value.get("output_price"), "provider_candidate_output_price_invalid"),
        cache_read_price=_decimal(
            value.get("cache_read_price"), "provider_candidate_cache_read_price_invalid"
        ),
        cache_write_price=_decimal(
            value.get("cache_write_price"), "provider_candidate_cache_write_price_invalid"
        ),
        realtime_ratio_cny=_decimal(
            value.get("realtime_ratio_cny"),
            "provider_candidate_realtime_ratio_cny_invalid",
        ),
        exchange_rate=_decimal(
            value.get("exchange_rate"),
            "provider_candidate_exchange_rate_invalid",
        ),
        supplier_id=_int_or_none(
            value.get("supplier_id"), "provider_candidate_supplier_id_invalid"
        ),
        listing_id=_int_or_none(value.get("listing_id"), "provider_candidate_listing_id_invalid"),
        health_state=str(value.get("health_state") or "unknown").strip().casefold(),
        available=bool(value.get("available", True)),
        last_success_at=_datetime_or_none(
            value.get("last_success_at"), "provider_candidate_last_success_invalid"
        ),
        latency_ewma_ms=_decimal(
            value.get("latency_ewma_ms"), "provider_candidate_latency_invalid"
        ),
        observer_health_count=int(value.get("observer_health_count", 1) or 0),
        observer_count=int(value.get("observer_count", 1) or 0),
        observed_at=_datetime_or_none(
            value.get("observed_at"), "provider_candidate_observed_invalid"
        ),
    )


def canonical_snapshot_payload(snapshot: ObserverSnapshot) -> bytes:
    rows = []
    for row in snapshot.candidates:
        rows.append(
            {
                "channel_id": row.channel_id,
                "model": row.model,
                "supplier_id": row.supplier_id,
                "listing_id": row.listing_id,
                "final_multiplier": _decimal_text(row.final_multiplier),
                "price_key": _decimal_text(row.price_key),
                "input_price": _decimal_text(row.input_price),
                "output_price": _decimal_text(row.output_price),
                "cache_read_price": _decimal_text(row.cache_read_price),
                "cache_write_price": _decimal_text(row.cache_write_price),
                "realtime_ratio_cny": _decimal_text(row.realtime_ratio_cny),
                "exchange_rate": _decimal_text(row.exchange_rate),
                "health_state": row.health_state,
                "available": row.available,
                "last_success_at": _time_text(row.last_success_at),
                "latency_ewma_ms": _decimal_text(row.latency_ewma_ms),
                "observer_health_count": row.observer_health_count,
                "observer_count": row.observer_count,
            }
        )
    payload = {
        "model": snapshot.model,
        "observed_at": _time_text(snapshot.observed_at),
        "status": snapshot.status,
        "candidate_count": snapshot.candidate_count,
        "pages_complete": snapshot.pages_complete,
        "config_version": snapshot.config_version,
        "sequence": snapshot.sequence,
        "health_observer_count": snapshot.health_observer_count,
        "candidates": rows,
    }
    return json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()


def snapshot_hash(snapshot: ObserverSnapshot) -> str:
    return hashlib.sha256(canonical_snapshot_payload(snapshot)).hexdigest()


def validate_snapshot_hash(snapshot: ObserverSnapshot) -> None:
    if snapshot_hash(snapshot) != snapshot.result_hash.casefold():
        raise ProviderPoolRankError("provider_snapshot_hash_mismatch")


def accept_snapshot(
    snapshot: ObserverSnapshot,
    *,
    now: datetime,
    freshness_seconds: int = 120,
    minimum_health_observers: int = 2,
    current_observed_at: datetime | None = None,
    expected_model: str | None = None,
    expected_config_version: str | None = None,
) -> ObserverSnapshot:
    """Validate and return a snapshot that is safe to advance as routing evidence."""

    now = _aware(now, "provider_snapshot_now_invalid")
    if isinstance(freshness_seconds, bool) or freshness_seconds <= 0:
        raise ProviderPoolRankError("provider_snapshot_freshness_invalid")
    if isinstance(minimum_health_observers, bool) or minimum_health_observers < 1:
        raise ProviderPoolRankError("provider_snapshot_minimum_observers_invalid")
    if expected_model is not None and snapshot.model != str(expected_model):
        raise ProviderPoolRankError("provider_snapshot_model_mismatch")
    if expected_config_version is not None and snapshot.config_version != str(
        expected_config_version
    ):
        raise ProviderPoolRankError("provider_snapshot_config_mismatch")
    if now - _aware(snapshot.observed_at, "provider_snapshot_observed_at_invalid") > timedelta(
        seconds=freshness_seconds
    ):
        raise ProviderPoolRankError("provider_snapshot_stale")
    if snapshot.health_observer_count < minimum_health_observers:
        raise ProviderPoolRankError("provider_snapshot_observer_quorum_insufficient")
    validate_snapshot_hash(snapshot)
    if current_observed_at is not None and snapshot.observed_at <= _aware(
        current_observed_at, "provider_snapshot_current_time_invalid"
    ):
        raise ProviderPoolRankError("provider_snapshot_out_of_order")
    return snapshot


def effective_price(
    row: Candidate,
    *,
    input_weight: Decimal = Decimal("1"),
    output_weight: Decimal = Decimal("1"),
    cache_read_weight: Decimal = Decimal("0"),
    cache_write_weight: Decimal = Decimal("0"),
) -> Decimal:
    if row.price_key is not None:
        return row.price_key
    if row.final_multiplier is not None:
        return row.final_multiplier
    components = (row.input_price, row.output_price, row.cache_read_price, row.cache_write_price)
    weights = (input_weight, output_weight, cache_read_weight, cache_write_weight)
    if any(value is None for value in components):
        raise ProviderPoolRankError("provider_candidate_price_missing")
    return sum(
        (value * weight for value, weight in zip(components, weights, strict=True)),
        Decimal("0"),
    )


def effective_price_cny(row: Candidate) -> Decimal:
    if row.realtime_ratio_cny is not None:
        return row.realtime_ratio_cny
    if row.exchange_rate is not None and row.exchange_rate > 0:
        return effective_price(row) * row.exchange_rate
    raise ProviderPoolRankError("provider_candidate_cny_price_missing")


def rank_candidates(
    rows: Iterable[Candidate | Mapping[str, Any]],
    *,
    model: str,
    owned_ids: Iterable[int] = (),
    owned_supplier_ids: Iterable[int] = (),
    owned_channel_ids: Iterable[int] = (),
    owned_listing_ids: Iterable[int] = (),
    quarantines: Mapping[int, datetime] | Iterable[int] = (),
    now: datetime,
    n: int,
    standby_count: int = 0,
    minimum_health_observers: int = 1,
) -> RankedPool:
    """Apply eligibility rules and return deterministic TopN plus standby rows."""

    if isinstance(n, bool) or n < 1 or isinstance(standby_count, bool) or standby_count < 0:
        raise ProviderPoolRankError("provider_rank_size_invalid")
    now = _aware(now, "provider_rank_now_invalid")
    # ``owned_ids`` is retained as the legacy channel-ID input.  Resource IDs live in
    # independent namespaces, so supplier/listing IDs must never be compared to a
    # candidate channel ID (or vice versa).
    owned_suppliers = {int(item) for item in owned_supplier_ids}
    owned_channels = {int(item) for item in owned_channel_ids}
    owned_channels.update(int(item) for item in owned_ids)
    owned_listings = {int(item) for item in owned_listing_ids}
    quarantine_map: dict[int, datetime | None] = {}
    if isinstance(quarantines, Mapping):
        for key, value in quarantines.items():
            quarantine_map[int(key)] = _aware(value, "provider_quarantine_time_invalid")
    else:
        quarantine_map = {int(item): None for item in quarantines}

    normalized: dict[int, Candidate] = {}
    excluded: list[tuple[int, str]] = []
    for raw in rows:
        row = raw if isinstance(raw, Candidate) else candidate_from_mapping(raw)
        if row.model != model:
            excluded.append((row.channel_id, "model_mismatch"))
            continue
        if (
            row.channel_id in owned_channels
            or row.supplier_id in owned_suppliers
            or row.listing_id in owned_listings
        ):
            excluded.append((row.channel_id, "owned"))
            continue
        if row.channel_id in quarantine_map:
            until = quarantine_map[row.channel_id]
            if until is None or until > now:
                excluded.append((row.channel_id, "quarantined"))
                continue
        if not row.available or row.health_state in _UNAVAILABLE_HEALTH_STATES:
            excluded.append((row.channel_id, "unavailable"))
            continue
        if row.health_state not in _ELIGIBLE_HEALTH_STATES:
            excluded.append((row.channel_id, "health_not_ready"))
            continue
        if row.observer_health_count < minimum_health_observers:
            excluded.append((row.channel_id, "observer_quorum"))
            continue
        try:
            effective_price(row)
        except ProviderPoolRankError:
            excluded.append((row.channel_id, "price_missing"))
            continue
        # Multiple Observer rows for a channel collapse to one deterministic row. Prefer
        # the row with the better complete sort key, then the one seen first by channel ID.
        previous = normalized.get(row.channel_id)
        if previous is None or _sort_key(row) < _sort_key(previous):
            normalized[row.channel_id] = row

    ordered = sorted(normalized.values(), key=_sort_key)
    limit = n + standby_count
    return RankedPool(
        top=tuple(ordered[:n]),
        standby=tuple(ordered[n:limit]),
        eligible=tuple(ordered),
        excluded=tuple(sorted(excluded)),
    )


def _sort_key(row: Candidate) -> tuple[Any, ...]:
    price = effective_price(row)
    success = row.last_success_at.timestamp() if row.last_success_at is not None else None
    latency = row.latency_ewma_ms
    # Python tuple ordering cannot put None after a Decimal without a marker.
    return (
        price,
        1 if success is None else 0,
        -(success or 0),
        1 if latency is None else 0,
        latency if latency is not None else Decimal("0"),
        row.channel_id,
    )


def cost_input_document(
    *,
    model: str,
    ordered_channels: Sequence[Candidate],
    assignments: Mapping[int, str | int | None],
    ready_slot_nos: Sequence[int],
    token_mix_version: str,
    exchange_rate_version: str,
    retry_reserve_window: str,
    retry_reserve_value: Decimal | str | int,
    realized_cost_window: str,
    realized_cost_p95_value: Decimal | str | int | None,
    margin_bps: int,
    headroom_bps: int,
    quantization_version: str,
) -> dict[str, Any]:
    """Return canonical semantic cost inputs without scan IDs or timestamps."""

    def normalized_value(value: object) -> str | None:
        if value is None:
            return None
        try:
            result = Decimal(str(value))
        except (InvalidOperation, ValueError):
            raise ProviderPoolRankError("provider_cost_input_decimal_invalid") from None
        if not result.is_finite():
            raise ProviderPoolRankError("provider_cost_input_decimal_invalid")
        return format(result, "f")

    return {
        "model": str(model),
        "ordered_channels": [
            {
                "channel_id": row.channel_id,
                "price": normalized_value(effective_price(row)),
                "final_multiplier": normalized_value(row.final_multiplier),
            }
            for row in ordered_channels
        ],
        "assignments": [[int(slot), assignments[slot]] for slot in sorted(assignments)],
        "ready_slot_nos": sorted(int(slot) for slot in ready_slot_nos),
        "token_mix_version": str(token_mix_version),
        "exchange_rate_version": str(exchange_rate_version),
        "retry_reserve_window": str(retry_reserve_window),
        "retry_reserve_value": normalized_value(retry_reserve_value),
        "realized_cost_window": str(realized_cost_window),
        "realized_cost_p95_value": normalized_value(realized_cost_p95_value),
        "margin_bps": int(margin_bps),
        "headroom_bps": int(headroom_bps),
        "quantization_version": str(quantization_version),
    }


def cost_input_hash(
    *,
    model: str,
    ordered_channels: Sequence[Candidate],
    assignments: Mapping[int, str | int | None],
    ready_slot_nos: Sequence[int],
    token_mix_version: str,
    exchange_rate_version: str,
    retry_reserve_window: str,
    retry_reserve_value: Decimal | str | int,
    realized_cost_window: str,
    realized_cost_p95_value: Decimal | str | int | None,
    margin_bps: int,
    headroom_bps: int,
    quantization_version: str,
) -> str:
    """Hash only semantic cost inputs; timestamps and scan IDs are evidence, not identity."""

    payload = cost_input_document(
        model=model,
        ordered_channels=ordered_channels,
        assignments=assignments,
        ready_slot_nos=ready_slot_nos,
        token_mix_version=token_mix_version,
        exchange_rate_version=exchange_rate_version,
        retry_reserve_window=retry_reserve_window,
        retry_reserve_value=retry_reserve_value,
        realized_cost_window=realized_cost_window,
        realized_cost_p95_value=realized_cost_p95_value,
        margin_bps=margin_bps,
        headroom_bps=headroom_bps,
        quantization_version=quantization_version,
    )
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _decimal_text(value: Decimal | None) -> str | None:
    return None if value is None else format(value, "f")


def _time_text(value: datetime | None) -> str | None:
    return None if value is None else _aware(value, "provider_time_invalid").isoformat()


def _is_sha256(value: str) -> bool:
    return len(str(value)) == 64 and all(char in "0123456789abcdefABCDEF" for char in str(value))
