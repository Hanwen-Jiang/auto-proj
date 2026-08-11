from __future__ import annotations

import hashlib
import json
import math
import random
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import contextmanager, nullcontext, suppress
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .client import A6ApiClient
from .enodot_admin import EnodotAdminClient
from .errors import A6AutoError
from .storage import SessionStore
from .trade_authenticated_client import (
    AuthenticatedSessionError,
    authenticated_a6_client,
)
from .trade_channel_failure import classify_channel_failure
from .trade_channel_health import (
    ChannelHealthInput,
    channel_health_evidence,
    classify_channel_health,
)
from .trade_errors import classify_provider_error
from .trade_pin_control import (
    ObserverQuote,
    PinAssignment,
    PinCandidate,
    PinControlError,
    PinPlan,
    ProviderPinExecutor,
    ProviderPinTarget,
    apply_pin_hysteresis,
    plan_provider_pins,
)


class ProviderPinRunnerError(A6AutoError):
    """Stable errors for the periodic Provider market-pin controller."""


_MAX_OBSERVER_SCAN_WORKERS = 32


def _pin_scan_worker_count(
    *,
    provider_count: int,
    observer_count: int,
    observe_only: bool,
) -> int:
    """Avoid queue time consuming the deadline for a read-only full Observer round."""
    if observe_only:
        return min(_MAX_OBSERVER_SCAN_WORKERS, max(1, observer_count))
    return min(8, max(1, provider_count + observer_count))


def _pin_round_deadline(round_time_budget_seconds: float) -> float:
    if (
        isinstance(round_time_budget_seconds, bool)
        or not isinstance(round_time_budget_seconds, (int, float))
        or not math.isfinite(float(round_time_budget_seconds))
        or not 15 <= float(round_time_budget_seconds) <= 300
    ):
        raise ProviderPinRunnerError("provider_pin_round_time_budget_invalid")
    return time.monotonic() + float(round_time_budget_seconds)


def _pin_round_time_remaining(round_deadline: float) -> float:
    return round_deadline - time.monotonic()


def _pin_round_checkpoint(round_deadline: float | None) -> None:
    if round_deadline is not None and _pin_round_time_remaining(round_deadline) <= 0:
        raise ProviderPinRunnerError("provider_pin_round_deadline_exceeded")


def _pin_remote_timeout(round_deadline: float, maximum_seconds: float) -> float:
    remaining = _pin_round_time_remaining(round_deadline)
    if remaining <= 0:
        raise ProviderPinRunnerError("provider_pin_round_deadline_exceeded")
    return min(float(maximum_seconds), remaining)


def _validated_probe_timing(
    timeout_seconds: float,
    round_time_budget_seconds: float,
) -> tuple[float, float]:
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or not math.isfinite(float(timeout_seconds))
        or not 1 <= float(timeout_seconds) <= 20
    ):
        raise ProviderPinRunnerError("provider_probe_timeout_invalid")
    if (
        isinstance(round_time_budget_seconds, bool)
        or not isinstance(round_time_budget_seconds, (int, float))
        or not math.isfinite(float(round_time_budget_seconds))
        or not float(timeout_seconds) <= float(round_time_budget_seconds) <= 30
    ):
        raise ProviderPinRunnerError("provider_probe_round_time_budget_invalid")
    return float(timeout_seconds), float(round_time_budget_seconds)


class _DeadlineBoundEnodotAdmin:
    """Apply the shrinking Pin-round budget to every Admin API method call."""

    def __init__(self, admin: Any, round_deadline: float) -> None:
        self._admin = admin
        self._round_deadline = round_deadline

    def __getattr__(self, name: str) -> Any:
        attribute = getattr(self._admin, name)
        if not callable(attribute):
            return attribute

        def bounded(*args: Any, **kwargs: Any) -> Any:
            remaining = _pin_remote_timeout(self._round_deadline, float("inf"))
            deadline_scope = getattr(self._admin, "request_deadline", None)
            timeout_scope = getattr(self._admin, "request_timeout", None)
            if callable(deadline_scope):
                scope = deadline_scope(self._round_deadline)
            elif callable(timeout_scope):
                scope = timeout_scope(remaining)
            else:
                scope = nullcontext()
            with scope:
                result = attribute(*args, **kwargs)
            _pin_round_checkpoint(self._round_deadline)
            return result

        return bounded


@dataclass(frozen=True)
class ProviderPinContext:
    merchant_id: int
    a6_user_id: int
    token_id: int
    token_ref_id: int
    enodot_account_id: int
    session_ref: str
    quota_per_unit: int
    user_balance_quota: int
    withdrawable_quota: int
    confirmed_pin_channel_id: int | None = None
    confirmed_pin_age_seconds: int | None = None
    temporary_provider: bool = False
    minimum_balance_quota: int | None = None
    desired_priority: int | None = None
    desired_concurrency: int | None = None


@dataclass(frozen=True)
class PinFailureTargets:
    matched: tuple[tuple[ProviderPinContext, int], ...]
    unmatched_account_ids: tuple[int, ...]

    @property
    def matched_account_ids(self) -> tuple[int, ...]:
        return tuple(context.enodot_account_id for context, _event_ns in self.matched)


@dataclass(frozen=True)
class ResolvedPinFailure:
    context: ProviderPinContext
    event_at_unix_nano: int
    channel_id: int


def _pin_failure_targets(
    contexts: Sequence[ProviderPinContext],
    account_events: Mapping[int, int] | Sequence[tuple[int, int]],
) -> PinFailureTargets:
    raw_events = (
        tuple(sorted(account_events.items()))
        if isinstance(account_events, Mapping)
        else tuple(account_events)
    )
    normalized: list[tuple[int, int]] = []
    for raw_account_id, raw_event_ns in raw_events:
        if (
            isinstance(raw_account_id, bool)
            or isinstance(raw_event_ns, bool)
            or not isinstance(raw_account_id, int)
            or not isinstance(raw_event_ns, int)
            or raw_account_id < 1
            or raw_event_ns < 1
        ):
            raise ProviderPinRunnerError("pin_failure_event_invalid")
        normalized.append((raw_account_id, raw_event_ns))
    by_account_id = {context.enodot_account_id: context for context in contexts}
    matched = tuple(
        (by_account_id[account_id], event_ns)
        for account_id, event_ns in normalized
        if account_id in by_account_id
    )
    return PinFailureTargets(
        matched=matched,
        unmatched_account_ids=tuple(
            account_id for account_id, _event_ns in normalized if account_id not in by_account_id
        ),
    )


def _pin_failure_channel(
    store: Any,
    context: ProviderPinContext,
    *,
    model: str,
    event_at_unix_nano: int,
) -> int | None:
    resolver = getattr(store, "pin_channel_at", None)
    channel_id = (
        resolver(context.token_ref_id, model, event_at_unix_nano)
        if callable(resolver)
        else context.confirmed_pin_channel_id
    )
    if channel_id is None:
        return None
    return _positive_int(channel_id, "pin_failure_channel_invalid")


def _resolve_pin_failures(
    store: Any,
    targets: PinFailureTargets,
    *,
    model: str,
) -> tuple[ResolvedPinFailure, ...]:
    resolved: list[ResolvedPinFailure] = []
    for context, event_ns in targets.matched:
        channel_id = _pin_failure_channel(
            store,
            context,
            model=model,
            event_at_unix_nano=event_ns,
        )
        if channel_id is not None:
            resolved.append(
                ResolvedPinFailure(
                    context=context,
                    event_at_unix_nano=event_ns,
                    channel_id=channel_id,
                )
            )
    return tuple(resolved)


@dataclass(frozen=True)
class ExternalPinFailureStreak:
    count: int
    threshold: int
    triggered: bool
    duplicate: bool
    success_reset: bool

    def public(self) -> dict[str, Any]:
        return {
            "count": self.count,
            "threshold": self.threshold,
            "triggered": self.triggered,
            "duplicate": self.duplicate,
            "success_reset": self.success_reset,
        }


def _external_pin_failure_streak(
    event_times: Sequence[int],
    *,
    latest_success_ns: int | None,
    threshold: int,
    inserted: bool,
) -> ExternalPinFailureStreak:
    if isinstance(threshold, bool) or not 1 <= threshold <= 100:
        raise ProviderPinRunnerError("pin_channel_failure_threshold_invalid")
    if latest_success_ns is not None and (
        isinstance(latest_success_ns, bool) or latest_success_ns < 1
    ):
        raise ProviderPinRunnerError("pin_failure_success_timestamp_invalid")
    normalized: list[int] = []
    for value in event_times:
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ProviderPinRunnerError("pin_failure_event_invalid")
        normalized.append(value)
    count = sum(
        1
        for event_ns in normalized
        if latest_success_ns is None or event_ns > latest_success_ns
    )
    success_reset = latest_success_ns is not None and any(
        event_ns <= latest_success_ns for event_ns in normalized
    )
    return ExternalPinFailureStreak(
        count=count,
        threshold=threshold,
        triggered=bool(inserted and count == threshold),
        duplicate=not inserted,
        success_reset=success_reset,
    )


@dataclass(frozen=True)
class ObserverContext:
    merchant_id: int
    a6_user_id: int
    token_id: int
    session_ref: str


@dataclass(frozen=True)
class ObserverScanResult:
    quotes: tuple[ObserverQuote, ...]
    page_count: int
    started_at_epoch: float
    completed_at_epoch: float
    elapsed_ms: int


@dataclass(frozen=True)
class PersistedObserverSnapshot:
    quotes: tuple[ObserverQuote, ...]
    scans: tuple[dict[str, Any], ...]
    latest_completed_at_epoch: float | None
    oldest_completed_at_epoch: float | None


@dataclass(frozen=True)
class ProviderProbeResult:
    success: bool
    http_status: int
    error_class: str | None
    retry_next_candidate: bool


@dataclass(frozen=True)
class ProviderPinEconomicReference:
    protected_billing_floor_multiplier: Decimal | None
    current_operating_cost_multiplier: Decimal | None
    source: str


@dataclass(frozen=True)
class PoolDisableResult:
    changed: bool
    retained_for_capacity: bool
    capacity_before: int
    capacity_after: int

    def public(self) -> dict[str, Any]:
        return {
            "schedulable_changed": self.changed,
            "retained_for_pool_capacity": self.retained_for_capacity,
            "pool_capacity_before": self.capacity_before,
            "pool_capacity_after": self.capacity_after,
        }


class _SchedulablePoolGuard:
    """Prevent one Pin round from draining the complete Provider pool."""

    def __init__(
        self,
        admin: EnodotAdminClient,
        account_ids: Sequence[int],
        *,
        minimum_capacity: int,
    ) -> None:
        if isinstance(minimum_capacity, bool) or not 1 <= minimum_capacity <= 1_000_000:
            raise ProviderPinRunnerError("provider_pool_minimum_capacity_invalid")
        normalized = tuple(dict.fromkeys(int(value) for value in account_ids))
        if not normalized or any(value < 1 for value in normalized):
            raise ProviderPinRunnerError("provider_pool_accounts_invalid")
        self.admin = admin
        self.account_ids = normalized
        self.minimum_capacity = minimum_capacity
        self._account_snapshot: dict[int, Mapping[str, Any]] | None = None

    @staticmethod
    def _capacity(account: Mapping[str, Any]) -> int:
        if (
            str(account.get("status") or "").strip().casefold() != "active"
            or account.get("schedulable") is not True
        ):
            return 0
        return _optional_int(account.get("concurrency")) or 1

    def _accounts(self) -> dict[int, Mapping[str, Any]]:
        if self._account_snapshot is None:
            self._account_snapshot = {
                account_id: dict(self.admin.get_account(account_id))
                for account_id in self.account_ids
            }
        return self._account_snapshot

    def observe(self, account_id: int, account: Mapping[str, Any]) -> None:
        """Merge one authoritative read/write response into an already-loaded snapshot."""

        if account_id not in self.account_ids:
            raise ProviderPinRunnerError("provider_pool_account_unknown")
        if not isinstance(account, Mapping):
            raise ProviderPinRunnerError("provider_pool_account_invalid")
        observed_id = _optional_int(account.get("id"))
        if observed_id is not None and observed_id != account_id:
            raise ProviderPinRunnerError("provider_pool_account_identity_mismatch")
        if self._account_snapshot is not None:
            self._account_snapshot[account_id] = dict(account)

    def capacity(self) -> int:
        return sum(self._capacity(account) for account in self._accounts().values())

    def disable(self, account_id: int, *, force: bool = False) -> PoolDisableResult:
        if not isinstance(force, bool):
            raise ProviderPinRunnerError("provider_pool_force_disable_invalid")
        if account_id not in self.account_ids:
            raise ProviderPinRunnerError("provider_pool_account_unknown")
        accounts = self._accounts()
        capacity_before = sum(self._capacity(account) for account in accounts.values())
        target_capacity = self._capacity(accounts[account_id])
        if target_capacity == 0:
            return PoolDisableResult(False, False, capacity_before, capacity_before)
        capacity_after = capacity_before - target_capacity
        if not force and capacity_after < self.minimum_capacity:
            return PoolDisableResult(False, True, capacity_before, capacity_before)
        observed = self.admin.set_schedulable(account_id, False)
        self.observe(account_id, observed)
        if observed.get("schedulable") is not False:
            observed = self.admin.get_account(account_id)
            self.observe(account_id, observed)
        if observed.get("schedulable") is not False:
            raise ProviderPinRunnerError("provider_pool_disable_readback_failed")
        return PoolDisableResult(True, False, capacity_before, capacity_after)


@dataclass(frozen=True)
class EmergencyBoostWorkload:
    profile: str
    input_text: str
    max_output_tokens: int


_EMERGENCY_CHANNEL_STATES = frozenset({"cooling", "unavailable", "degraded"})


def _emergency_pinned_channel_ids(
    quotes: Sequence[ObserverQuote],
    pinned_channel_ids: set[int] | frozenset[int],
) -> frozenset[int]:
    alerts = set()
    for quote in quotes:
        if quote.channel_id not in pinned_channel_ids:
            continue
        health = str(quote.health_state or "").strip().lower()
        route = str(quote.route_status or "").strip().lower()
        if (
            not quote.available
            or health in _EMERGENCY_CHANNEL_STATES
            or route in {"cooling", "degraded", "error", "failed", "unavailable", "yellow"}
        ):
            alerts.add(quote.channel_id)
    return frozenset(alerts)


def _observed_current_operating_cost(
    quotes: Sequence[ObserverQuote],
    pinned_channel_ids: set[int] | frozenset[int],
    *,
    min_observers: int,
) -> Decimal | None:
    by_channel: dict[int, dict[int, Decimal]] = {}
    for quote in quotes:
        if quote.channel_id not in pinned_channel_ids or quote.final_multiplier is None:
            continue
        by_channel.setdefault(quote.channel_id, {})[quote.observer_merchant_id] = (
            quote.final_multiplier
        )
    channel_costs: list[Decimal] = []
    for observations in by_channel.values():
        if len(observations) < min_observers:
            continue
        ordered = sorted(observations.values())
        middle = len(ordered) // 2
        channel_costs.append(
            ordered[middle]
            if len(ordered) % 2
            else (ordered[middle - 1] + ordered[middle]) / Decimal(2)
        )
    return max(channel_costs, default=None)


def _emergency_boost_workload(
    request_count: int,
    *,
    rng: random.Random | random.SystemRandom | None = None,
) -> tuple[EmergencyBoostWorkload, ...]:
    if not 3 <= request_count <= 5:
        raise ProviderPinRunnerError("provider_emergency_boost_count_invalid")
    rng = rng or random.SystemRandom()
    profiles = ["short", "medium", "long"]
    profiles.extend(rng.choice(("short", "medium", "long")) for _ in range(request_count - 3))
    rng.shuffle(profiles)
    workloads = []
    for profile in profiles:
        if profile == "short":
            topic = rng.choice(("retry budgets", "latency alerts", "queue backpressure"))
            input_text = (
                f"Explain {topic} in two concise sentences for an API operations note. "
                "Include one concrete recommendation."
            )
            max_output_tokens = rng.randint(48, 80)
        elif profile == "medium":
            samples = [
                f"minute {index}: latency={rng.randint(700, 4200)}ms, "
                f"success={rng.randint(91, 100)}%, queue={rng.randint(0, 18)}"
                for index in range(1, rng.randint(5, 9))
            ]
            input_text = (
                "Review the following synthetic API service metrics. Identify the main trend, "
                "one likely cause, and three prioritized actions.\n" + "\n".join(samples)
            )
            max_output_tokens = rng.randint(96, 160)
        else:
            events = [
                f"Event {index}: shard-{rng.randint(1, 8)} reported "
                f"p50={rng.randint(900, 8000)}ms, errors={rng.randint(0, 14)}, "
                f"active={rng.randint(12, 90)}; the retry controller adjusted its window."
                for index in range(1, rng.randint(12, 20))
            ]
            input_text = (
                "Analyze this synthetic incident timeline. Produce a root-cause hypothesis, "
                "separate evidence from assumptions, and propose a five-step mitigation plan.\n"
                + "\n".join(events)
            )
            max_output_tokens = rng.randint(160, 256)
        workloads.append(
            EmergencyBoostWorkload(
                profile=profile,
                input_text=input_text,
                max_output_tokens=max_output_tokens,
            )
        )
    return tuple(workloads)


def _row(cursor: Any) -> dict[str, Any] | None:
    value = cursor.fetchone()
    if value is None:
        return None
    if isinstance(value, Mapping):
        return dict(value)
    names = [str(column.name) for column in cursor.description]
    return dict(zip(names, value, strict=True))


def _rows(cursor: Any) -> list[dict[str, Any]]:
    values = cursor.fetchall()
    if not values:
        return []
    if isinstance(values[0], Mapping):
        return [dict(value) for value in values]
    names = [str(column.name) for column in cursor.description]
    return [dict(zip(names, value, strict=True)) for value in values]


def _positive_int(value: Any, error: str) -> int:
    if isinstance(value, bool):
        raise ProviderPinRunnerError(error)
    try:
        result = int(value)
    except (TypeError, ValueError):
        raise ProviderPinRunnerError(error) from None
    if result < 1:
        raise ProviderPinRunnerError(error)
    return result


def _optional_int(value: Any) -> int | None:
    if value in (None, "") or isinstance(value, bool):
        return None
    try:
        result = int(value)
    except (TypeError, ValueError):
        return None
    return result if result > 0 else None


def _optional_nonnegative_int(value: Any) -> int | None:
    if value in (None, "") or isinstance(value, bool):
        return None
    try:
        result = int(value)
    except (TypeError, ValueError):
        return None
    return result if result >= 0 else None


_UNAVAILABLE_MARKET_STATES = frozenset(
    {"blocked", "cooldown", "cooling", "disabled", "open", "unavailable"}
)


def _market_state(item: Mapping[str, Any]) -> str:
    states = [item.get("circuit_state")]
    for key in ("circuit_status", "cooldown_status"):
        nested = item.get(key)
        if isinstance(nested, Mapping):
            states.append(nested.get("state"))
    return next((str(value).strip().lower() for value in states if str(value or "").strip()), "")


def _market_quote_available(
    item: Mapping[str, Any],
    *,
    route_status: str,
    circuit_state: str,
) -> bool:
    if item.get("available") is False:
        return False
    if route_status.strip().lower() in _UNAVAILABLE_MARKET_STATES:
        return False
    if circuit_state in _UNAVAILABLE_MARKET_STATES:
        return False
    cooldown = item.get("cooldown_status")
    if isinstance(cooldown, Mapping):
        remaining = _optional_nonnegative_int(cooldown.get("remaining_seconds"))
        if remaining is not None and remaining > 0:
            return False
    return True


def _decimal(value: Any) -> Decimal | None:
    if value in (None, "") or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return result if result.is_finite() and result > 0 else None


def _nonnegative_decimal(value: Any) -> Decimal | None:
    if value in (None, "") or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return result if result.is_finite() and result >= 0 else None


def _market_object(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _json_value(value: Any, *, default: Any) -> Any:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return list(value)
    if default is None and isinstance(value, (str, int, float, bool)):
        return value
    return default


def _json_array(value: Any) -> list[Any]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return list(value)
    return []


def _observer_quote_from_payload(
    observer_merchant_id: int,
    item: Mapping[str, Any],
    *,
    expected_model: str,
) -> ObserverQuote:
    model = str(item.get("model") or expected_model).strip()
    if model != expected_model:
        raise ProviderPinRunnerError("observer_snapshot_model_mismatch")
    labels = item.get("smart_routing_labels")
    smart_routing_labels = (
        tuple(
            dict.fromkeys(
                str(value).strip()
                for value in labels
                if str(value).strip()
            )
        )
        if isinstance(labels, Sequence) and not isinstance(labels, (str, bytes))
        else ()
    )
    return ObserverQuote(
        observer_merchant_id=_positive_int(
            observer_merchant_id,
            "observer_snapshot_merchant_invalid",
        ),
        channel_id=_positive_int(
            item.get("channel_id"),
            "observer_snapshot_channel_invalid",
        ),
        listing_id=_optional_int(item.get("listing_id")),
        model=model,
        final_multiplier=_decimal(item.get("final_multiplier")),
        input_price_micros=_optional_nonnegative_int(item.get("input_price_micros")) or 0,
        output_price_micros=_optional_nonnegative_int(item.get("output_price_micros")) or 0,
        available=item.get("available") is True,
        supplier_id=_optional_int(item.get("supplier_id")),
        route_status=str(item.get("route_status") or ""),
        last_success_at=_optional_int(item.get("last_success_at")),
        circuit_state=str(item.get("circuit_state") or ""),
        circuit_probe_status=str(item.get("circuit_probe_status") or ""),
        circuit_last_error_kind=str(item.get("circuit_last_error_kind") or ""),
        circuit_probe_success_required=_optional_nonnegative_int(
            item.get("circuit_probe_success_required")
        ),
        circuit_probe_success_streak=_optional_nonnegative_int(
            item.get("circuit_probe_success_streak")
        ),
        circuit_scope=str(item.get("circuit_scope") or ""),
        circuit_recovery_mode=str(item.get("circuit_recovery_mode") or ""),
        circuit_cooling_until=_optional_int(item.get("circuit_cooling_until")),
        circuit_last_cooling_at=_optional_int(item.get("circuit_last_cooling_at")),
        circuit_last_probe_at=_optional_int(item.get("circuit_last_probe_at")),
        circuit_next_probe_at=_optional_int(item.get("circuit_next_probe_at")),
        cooldown_state=str(item.get("cooldown_state") or ""),
        cooldown_label=str(item.get("cooldown_label") or ""),
        cooldown_remaining_seconds=_optional_nonnegative_int(
            item.get("cooldown_remaining_seconds")
        ),
        cooldown_expected_recovery_at=_optional_int(
            item.get("cooldown_expected_recovery_at")
        ),
        cooldown_next_probe_at=_optional_int(item.get("cooldown_next_probe_at")),
        cooldown_sources=_json_array(item.get("cooldown_sources")),
        capability_cooldowns=_json_value(item.get("capability_cooldowns"), default={}),
        smart_routing_status=str(item.get("smart_routing_status") or ""),
        smart_routing_labels=smart_routing_labels,
        listing_availability=_json_value(item.get("listing_availability"), default=None),
        last_failure_at=_optional_int(item.get("last_failure_at")),
        last_failure_code=str(item.get("last_failure_code") or "")[:128],
        last_failure_responsibility=str(
            item.get("last_failure_responsibility")
            or item.get("failure_responsibility")
            or "unknown"
        ),
        last_sample_at=_optional_int(item.get("last_sample_at")),
        sample_count=_optional_nonnegative_int(item.get("sample_count")),
        recent_success_rate=_optional_nonnegative_int(item.get("recent_success_rate")),
        success_buckets=_json_value(item.get("success_buckets"), default={}),
        recent_p50_ms=_nonnegative_decimal(item.get("recent_p50_ms")),
        p50_ttft_ms=_nonnegative_decimal(item.get("p50_ttft_ms")),
        health_state=str(item.get("health_state") or "unknown"),
        realtime_ratio_usd=_decimal(item.get("realtime_ratio_usd")),
        realtime_ratio_cny=_decimal(item.get("realtime_ratio_cny")),
        exchange_rate=_decimal(item.get("exchange_rate")),
    )


def _with_failure_summary(
    quote: ObserverQuote,
    summary: Mapping[str, Any] | None,
) -> ObserverQuote:
    value = summary or {}
    top_codes = value.get("top_codes")
    code_values = top_codes if isinstance(top_codes, Sequence) else ()
    return replace(
        quote,
        recent_failure_count=max(0, int(value.get("failure_count") or 0)),
        recent_client_failure_count=max(0, int(value.get("client_failure_count") or 0)),
        recent_provider_failure_count=max(0, int(value.get("provider_failure_count") or 0)),
        recent_platform_failure_count=max(0, int(value.get("platform_failure_count") or 0)),
        recent_unknown_failure_count=max(0, int(value.get("unknown_failure_count") or 0)),
        recent_failure_codes=tuple(
            str(item.get("code") or "")[:128]
            for item in code_values
            if isinstance(item, Mapping) and str(item.get("code") or "").strip()
        )[:8],
    )


def _hash(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def _scan_error_code(exc: Exception) -> str:
    value = str(getattr(exc, "code", "") or str(exc) or type(exc).__name__).lower()
    normalized = "".join(
        character if character.isalnum() or character in "._:-" else "_" for character in value
    ).strip("_")
    return (normalized or "observer_scan_failed")[:128]


def _observer_backoff_seconds(consecutive_failed_rounds: int) -> int:
    return min(300, 15 * (2 ** min(5, max(0, consecutive_failed_rounds - 1))))


_MARKET_SIGNAL_UPSERT_SQL = """
WITH incoming AS (
    SELECT
        item.model, item.channel_id, item.listing_id, item.supplier_id,
        item.realtime_ratio_usd, item.realtime_ratio_cny, item.final_multiplier,
        item.market_rank, item.available, item.is_owned,
        item.route_status, item.circuit_state, item.circuit_probe_status,
        item.circuit_last_error_kind, item.circuit_probe_success_required,
        item.circuit_probe_success_streak, item.circuit_scope,
        item.circuit_recovery_mode,
        CASE WHEN item.circuit_cooling_until IS NULL THEN NULL
             ELSE to_timestamp(item.circuit_cooling_until) END AS circuit_cooling_until,
        CASE WHEN item.circuit_last_cooling_at IS NULL THEN NULL
             ELSE to_timestamp(item.circuit_last_cooling_at) END AS circuit_last_cooling_at,
        CASE WHEN item.circuit_last_probe_at IS NULL THEN NULL
             ELSE to_timestamp(item.circuit_last_probe_at) END AS circuit_last_probe_at,
        CASE WHEN item.circuit_next_probe_at IS NULL THEN NULL
             ELSE to_timestamp(item.circuit_next_probe_at) END AS circuit_next_probe_at,
        item.cooldown_state, item.cooldown_label, item.cooldown_remaining_seconds,
        CASE WHEN item.cooldown_expected_recovery_at IS NULL THEN NULL
             ELSE to_timestamp(item.cooldown_expected_recovery_at)
        END AS cooldown_expected_recovery_at,
        CASE WHEN item.cooldown_next_probe_at IS NULL THEN NULL
             ELSE to_timestamp(item.cooldown_next_probe_at) END AS cooldown_next_probe_at,
        coalesce(item.cooldown_sources, '[]'::jsonb) AS cooldown_sources,
        coalesce(item.capability_cooldowns, '{}'::jsonb) AS capability_cooldowns,
        item.smart_routing_status,
        coalesce(item.smart_routing_labels, '{}'::text[]) AS smart_routing_labels,
        item.listing_availability,
        CASE
            WHEN item.last_success_at IS NULL THEN NULL
            ELSE to_timestamp(item.last_success_at)
        END AS last_success_at,
        CASE
            WHEN item.last_failure_at IS NULL THEN NULL
            ELSE to_timestamp(item.last_failure_at)
        END AS last_failure_at,
        item.last_failure_code, item.failure_responsibility,
        CASE
            WHEN item.last_sample_at IS NULL THEN NULL
            ELSE to_timestamp(item.last_sample_at)
        END AS last_sample_at,
        item.sample_count, item.recent_success_rate,
        coalesce(item.success_buckets, '{}'::jsonb) AS success_buckets,
        item.recent_p50_ms, item.p50_ttft_ms,
        item.health_state, item.health_evidence,
        to_timestamp(item.observed_at) AS observed_at
    FROM jsonb_to_recordset(%s::jsonb) AS item(
        model text,
        channel_id bigint,
        listing_id bigint,
        supplier_id bigint,
        realtime_ratio_usd numeric,
        realtime_ratio_cny numeric,
        final_multiplier numeric,
        market_rank integer,
        available boolean,
        is_owned boolean,
        route_status text,
        circuit_state text,
        circuit_probe_status text,
        circuit_last_error_kind text,
        circuit_probe_success_required integer,
        circuit_probe_success_streak integer,
        circuit_scope text,
        circuit_recovery_mode text,
        circuit_cooling_until double precision,
        circuit_last_cooling_at double precision,
        circuit_last_probe_at double precision,
        circuit_next_probe_at double precision,
        cooldown_state text,
        cooldown_label text,
        cooldown_remaining_seconds integer,
        cooldown_expected_recovery_at double precision,
        cooldown_next_probe_at double precision,
        cooldown_sources jsonb,
        capability_cooldowns jsonb,
        smart_routing_status text,
        smart_routing_labels text[],
        listing_availability jsonb,
        last_success_at double precision,
        last_failure_at double precision,
        last_failure_code text,
        failure_responsibility text,
        last_sample_at double precision,
        sample_count integer,
        recent_success_rate integer,
        success_buckets jsonb,
        recent_p50_ms numeric,
        p50_ttft_ms numeric,
        health_state text,
        health_evidence jsonb,
        observed_at double precision
    )
), recorded_failure_events AS (
    INSERT INTO a6_trade.a6_channel_failure_events (
        model, channel_id, listing_id, supplier_id,
        failure_at, failure_code, responsibility,
        first_observed_at, last_observed_at, observer_ids
    )
    SELECT
        model, channel_id, listing_id, supplier_id,
        last_failure_at, last_failure_code, failure_responsibility,
        observed_at, observed_at, ARRAY[%s::bigint]
    FROM incoming
    WHERE last_failure_at IS NOT NULL
      AND last_failure_code <> ''
    ON CONFLICT (model, channel_id, failure_at, failure_code) DO UPDATE SET
        listing_id = coalesce(
            a6_channel_failure_events.listing_id,
            EXCLUDED.listing_id
        ),
        supplier_id = coalesce(
            a6_channel_failure_events.supplier_id,
            EXCLUDED.supplier_id
        ),
        responsibility = EXCLUDED.responsibility,
        last_observed_at = greatest(
            a6_channel_failure_events.last_observed_at,
            EXCLUDED.last_observed_at
        ),
        observer_ids = CASE
            WHEN EXCLUDED.observer_ids[1] = ANY(a6_channel_failure_events.observer_ids)
            THEN a6_channel_failure_events.observer_ids
            ELSE array_append(
                a6_channel_failure_events.observer_ids,
                EXCLUDED.observer_ids[1]
            )
        END,
        updated_at = now()
    RETURNING id
), event_candidates AS (
    SELECT incoming.*
    FROM incoming
    JOIN a6_trade.a6_market_channel_signals_current current_signal
      ON current_signal.model = incoming.model
     AND current_signal.channel_id = incoming.channel_id
    WHERE incoming.last_success_at IS NOT NULL
      AND (
          (
              current_signal.last_success_at IS NOT NULL
              AND incoming.last_success_at > current_signal.last_success_at
          )
          OR EXISTS (
              SELECT 1
              FROM a6_trade.a6_market_traffic_events existing_event
              WHERE existing_event.model = incoming.model
                AND existing_event.channel_id = incoming.channel_id
                AND existing_event.success_at = incoming.last_success_at
          )
      )
), recorded_events AS (
    INSERT INTO a6_trade.a6_market_traffic_events (
        model, channel_id, listing_id, supplier_id, success_at,
        first_observed_at, last_observed_at,
        realtime_ratio_usd, realtime_ratio_cny, final_multiplier,
        market_rank, is_owned, observer_ids
    )
    SELECT
        model, channel_id, listing_id, supplier_id, last_success_at,
        observed_at, observed_at,
        realtime_ratio_usd, realtime_ratio_cny, final_multiplier,
        market_rank, is_owned, ARRAY[%s::bigint]
    FROM event_candidates
    ON CONFLICT (model, channel_id, success_at) DO UPDATE SET
        last_observed_at = greatest(
            a6_market_traffic_events.last_observed_at,
            EXCLUDED.last_observed_at
        ),
        is_owned = a6_market_traffic_events.is_owned OR EXCLUDED.is_owned,
        observer_ids = CASE
            WHEN EXCLUDED.observer_ids[1] = ANY(a6_market_traffic_events.observer_ids)
            THEN a6_market_traffic_events.observer_ids
            ELSE array_append(
                a6_market_traffic_events.observer_ids,
                EXCLUDED.observer_ids[1]
            )
        END,
        updated_at = now()
    RETURNING id
), recorded_health_events AS (
    INSERT INTO a6_trade.a6_channel_health_events (
        model, channel_id, listing_id, observer_merchant_id,
        previous_state, current_state, evidence, observed_at
    )
    SELECT
        incoming.model, incoming.channel_id, incoming.listing_id, %s::bigint,
        current_signal.health_state, incoming.health_state,
        incoming.health_evidence, incoming.observed_at
    FROM incoming
    JOIN a6_trade.a6_market_channel_signals_current current_signal
      ON current_signal.model = incoming.model
     AND current_signal.channel_id = incoming.channel_id
    WHERE current_signal.health_state <> incoming.health_state
      AND incoming.observed_at >= current_signal.last_observed_at
    RETURNING id
), recorded_observer_health AS (
    INSERT INTO a6_trade.a6_channel_observer_health_current (
        model, channel_id, listing_id, observer_merchant_id,
        health_state, available, evidence, observed_at, scan_run_id
    )
    SELECT
        incoming.model, incoming.channel_id, incoming.listing_id, %s::bigint,
        incoming.health_state, incoming.available, incoming.health_evidence,
        incoming.observed_at, %s::bigint
    FROM incoming
    ON CONFLICT (model, channel_id, observer_merchant_id) DO UPDATE SET
        listing_id = EXCLUDED.listing_id,
        health_state = EXCLUDED.health_state,
        available = EXCLUDED.available,
        evidence = EXCLUDED.evidence,
        observed_at = EXCLUDED.observed_at,
        scan_run_id = EXCLUDED.scan_run_id,
        updated_at = now()
    WHERE EXCLUDED.observed_at >= a6_channel_observer_health_current.observed_at
    RETURNING channel_id
)
INSERT INTO a6_trade.a6_market_channel_signals_current (
    model, channel_id, listing_id, supplier_id,
    realtime_ratio_usd, realtime_ratio_cny, final_multiplier,
    market_rank, available, is_owned,
    route_status, circuit_state, circuit_probe_status,
    circuit_last_error_kind, circuit_probe_success_required,
    circuit_probe_success_streak, circuit_scope, circuit_recovery_mode,
    circuit_cooling_until, circuit_last_cooling_at, circuit_last_probe_at,
    circuit_next_probe_at,
    cooldown_state, cooldown_label, cooldown_remaining_seconds,
    cooldown_expected_recovery_at, cooldown_next_probe_at, cooldown_sources,
    capability_cooldowns, smart_routing_status, smart_routing_labels,
    listing_availability, last_success_at, last_failure_at,
    last_failure_code, last_sample_at, sample_count, recent_success_rate,
    success_buckets, recent_p50_ms, p50_ttft_ms, health_state,
    first_observed_at, last_observed_at,
    observer_merchant_id, scan_run_id
)
SELECT
    model, channel_id, listing_id, supplier_id,
    realtime_ratio_usd, realtime_ratio_cny, final_multiplier,
    market_rank, available, is_owned,
    route_status, circuit_state, circuit_probe_status,
    circuit_last_error_kind, circuit_probe_success_required,
    circuit_probe_success_streak, circuit_scope, circuit_recovery_mode,
    circuit_cooling_until, circuit_last_cooling_at, circuit_last_probe_at,
    circuit_next_probe_at,
    cooldown_state, cooldown_label, cooldown_remaining_seconds,
    cooldown_expected_recovery_at, cooldown_next_probe_at, cooldown_sources,
    capability_cooldowns, smart_routing_status, smart_routing_labels,
    listing_availability, last_success_at, last_failure_at,
    last_failure_code, last_sample_at, sample_count, recent_success_rate,
    success_buckets, recent_p50_ms, p50_ttft_ms, health_state,
    observed_at, observed_at,
    %s::bigint, %s::bigint
FROM incoming
ON CONFLICT (model, channel_id) DO UPDATE SET
    listing_id = EXCLUDED.listing_id,
    supplier_id = EXCLUDED.supplier_id,
    realtime_ratio_usd = EXCLUDED.realtime_ratio_usd,
    realtime_ratio_cny = EXCLUDED.realtime_ratio_cny,
    final_multiplier = EXCLUDED.final_multiplier,
    market_rank = EXCLUDED.market_rank,
    available = EXCLUDED.available,
    is_owned = a6_market_channel_signals_current.is_owned OR EXCLUDED.is_owned,
    route_status = EXCLUDED.route_status,
    circuit_state = EXCLUDED.circuit_state,
    circuit_probe_status = EXCLUDED.circuit_probe_status,
    circuit_last_error_kind = EXCLUDED.circuit_last_error_kind,
    circuit_probe_success_required = EXCLUDED.circuit_probe_success_required,
    circuit_probe_success_streak = EXCLUDED.circuit_probe_success_streak,
    circuit_scope = EXCLUDED.circuit_scope,
    circuit_recovery_mode = EXCLUDED.circuit_recovery_mode,
    circuit_cooling_until = EXCLUDED.circuit_cooling_until,
    circuit_last_cooling_at = EXCLUDED.circuit_last_cooling_at,
    circuit_last_probe_at = EXCLUDED.circuit_last_probe_at,
    circuit_next_probe_at = EXCLUDED.circuit_next_probe_at,
    cooldown_state = EXCLUDED.cooldown_state,
    cooldown_label = EXCLUDED.cooldown_label,
    cooldown_remaining_seconds = EXCLUDED.cooldown_remaining_seconds,
    cooldown_expected_recovery_at = EXCLUDED.cooldown_expected_recovery_at,
    cooldown_next_probe_at = EXCLUDED.cooldown_next_probe_at,
    cooldown_sources = EXCLUDED.cooldown_sources,
    capability_cooldowns = EXCLUDED.capability_cooldowns,
    smart_routing_status = EXCLUDED.smart_routing_status,
    smart_routing_labels = EXCLUDED.smart_routing_labels,
    listing_availability = EXCLUDED.listing_availability,
    last_success_at = CASE
        WHEN a6_market_channel_signals_current.last_success_at IS NULL
          OR EXCLUDED.last_success_at > a6_market_channel_signals_current.last_success_at
        THEN EXCLUDED.last_success_at
        ELSE a6_market_channel_signals_current.last_success_at
    END,
    last_failure_at = EXCLUDED.last_failure_at,
    last_failure_code = EXCLUDED.last_failure_code,
    last_sample_at = EXCLUDED.last_sample_at,
    sample_count = EXCLUDED.sample_count,
    recent_success_rate = EXCLUDED.recent_success_rate,
    success_buckets = EXCLUDED.success_buckets,
    recent_p50_ms = EXCLUDED.recent_p50_ms,
    p50_ttft_ms = EXCLUDED.p50_ttft_ms,
    health_state = EXCLUDED.health_state,
    last_observed_at = EXCLUDED.last_observed_at,
    observer_merchant_id = EXCLUDED.observer_merchant_id,
    scan_run_id = EXCLUDED.scan_run_id,
    updated_at = now()
WHERE EXCLUDED.last_observed_at >= a6_market_channel_signals_current.last_observed_at
"""


class PostgresPinAuditStore:
    """Read Provider/Observer state and persist market/Pin facts transactionally."""

    def __init__(
        self,
        connection_factory: Callable[[], Any],
        *,
        requested_by: str = "trade-pin-providers-once",
    ) -> None:
        if not callable(connection_factory):
            raise ProviderPinRunnerError("provider_pin_connection_factory_invalid")
        self.connection_factory = connection_factory
        self.requested_by = str(requested_by).strip() or "trade-pin-providers-once"

    def provider_contexts(self, model: str) -> tuple[ProviderPinContext, ...]:
        model = str(model).strip()
        if not model:
            raise ProviderPinRunnerError("provider_pin_model_invalid")
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT m.id AS merchant_id, m.a6_user_id, t.a6_token_id,
                           t.id AS token_ref_id, b.enodot_account_id,
                           m.session_ref, m.quota_per_unit,
                           coalesce(f.user_balance_quota, 0) AS user_balance_quota,
                           coalesce(f.withdrawable_micros, 0) AS withdrawable_quota,
                           pin.channel_id AS confirmed_pin_channel_id,
                           extract(epoch FROM now() - pin.valid_from)::bigint
                               AS confirmed_pin_age_seconds,
                           residual.merchant_id IS NOT NULL AS temporary_provider,
                           residual.reserve_balance_quota AS minimum_balance_quota,
                           residual.desired_priority,
                           residual.desired_concurrency
                    FROM a6_trade.a6_merchants m
                    JOIN a6_trade.a6_tokens t ON t.merchant_id = m.id
                    JOIN a6_trade.enodot_account_bindings b ON b.a6_token_id = t.id
                    LEFT JOIN a6_trade.a6_financial_current f ON f.merchant_id = m.id
                    LEFT JOIN a6_trade.residual_provider_policies residual
                      ON residual.model = %s
                     AND residual.merchant_id = m.id
                     AND residual.enabled
                     AND residual.state = 'active'
                    LEFT JOIN LATERAL (
                        SELECT current_pin.channel_id, current_pin.valid_from
                        FROM a6_trade.a6_pin_bindings current_pin
                        JOIN a6_trade.a6_pin_decisions pin_decision
                          ON pin_decision.id = current_pin.decision_id
                         AND pin_decision.a6_token_id = current_pin.a6_token_id
                         AND pin_decision.model = current_pin.model
                         AND pin_decision.selected_channel_id = current_pin.channel_id
                         AND pin_decision.decision_status = 'completed'
                        WHERE current_pin.a6_token_id = t.id
                          AND current_pin.model = %s
                          AND current_pin.valid_to IS NULL
                          AND current_pin.confirmed_at IS NOT NULL
                        ORDER BY current_pin.valid_from DESC
                        LIMIT 1
                    ) pin ON true
                    WHERE 'provider' = ANY(m.roles)
                      AND m.a6_user_id IS NOT NULL
                      AND m.status = 'supplier_submitted'
                      AND t.status IN ('1', 'active', 'enabled')
                      AND b.state = 'bound'
                      AND b.enodot_account_id IS NOT NULL
                    ORDER BY m.id, t.id
                    """,
                    (model, model),
                )
                rows = _rows(cursor)
            return tuple(
                ProviderPinContext(
                    merchant_id=_positive_int(
                        row.get("merchant_id"), "provider_pin_context_invalid"
                    ),
                    a6_user_id=_positive_int(row.get("a6_user_id"), "provider_pin_context_invalid"),
                    token_id=_positive_int(row.get("a6_token_id"), "provider_pin_context_invalid"),
                    token_ref_id=_positive_int(
                        row.get("token_ref_id"), "provider_pin_context_invalid"
                    ),
                    enodot_account_id=_positive_int(
                        row.get("enodot_account_id"), "provider_pin_context_invalid"
                    ),
                    session_ref=str(row.get("session_ref") or ""),
                    quota_per_unit=_positive_int(
                        row.get("quota_per_unit"), "provider_pin_context_invalid"
                    ),
                    user_balance_quota=max(0, int(row.get("user_balance_quota") or 0)),
                    withdrawable_quota=max(0, int(row.get("withdrawable_quota") or 0)),
                    confirmed_pin_channel_id=_optional_int(row.get("confirmed_pin_channel_id")),
                    confirmed_pin_age_seconds=_optional_nonnegative_int(
                        row.get("confirmed_pin_age_seconds")
                    ),
                    temporary_provider=bool(row.get("temporary_provider")),
                    minimum_balance_quota=_optional_nonnegative_int(
                        row.get("minimum_balance_quota")
                    ),
                    desired_priority=_optional_int(row.get("desired_priority")),
                    desired_concurrency=_optional_int(row.get("desired_concurrency")),
                )
                for row in rows
            )
        finally:
            connection.close()

    def provider_pin_economic_reference(self, model: str) -> ProviderPinEconomicReference:
        model = str(model).strip()
        if not model:
            raise ProviderPinRunnerError("provider_pin_model_invalid")
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT cost.billing_floor_multiplier,
                           cost.route_cost_multiplier
                    FROM a6_trade.provider_pool_generations generation
                    JOIN a6_trade.provider_cost_generations cost
                      ON cost.id = generation.cost_generation_id
                     AND cost.model = generation.model
                    WHERE generation.model = %s
                      AND generation.generation_state = 'active'
                      AND cost.pricing_state = 'active'
                    ORDER BY generation.generation DESC
                    LIMIT 1
                    """,
                    (model,),
                )
                row = _row(cursor)
            if row is None:
                return ProviderPinEconomicReference(None, None, "no_active_cost_generation")
            protected_floor = _decimal(row.get("billing_floor_multiplier"))
            current_cost = _decimal(row.get("route_cost_multiplier"))
            if protected_floor is None or current_cost is None:
                raise ProviderPinRunnerError("provider_pin_active_cost_reference_invalid")
            return ProviderPinEconomicReference(
                protected_floor,
                current_cost,
                "active_cost_generation",
            )
        finally:
            connection.close()

    def promote_residual_provider_candidates(
        self,
        model: str,
        *,
        maximum_active: int = 3,
    ) -> tuple[int, ...]:
        """Fill vacant temporary-Provider seats from retired Observer candidates."""

        model = str(model).strip()
        if not model:
            raise ProviderPinRunnerError("residual_provider_model_invalid")
        if isinstance(maximum_active, bool) or not 1 <= maximum_active <= 12:
            raise ProviderPinRunnerError("residual_provider_active_limit_invalid")
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT pg_advisory_xact_lock(%s, hashtext(%s))",
                    (0x41365250, model),
                )
                cursor.execute(
                    """
                    SELECT count(*) AS active_count
                    FROM a6_trade.residual_provider_policies
                    WHERE model = %s AND enabled AND state = 'active'
                    """,
                    (model,),
                )
                count_row = _row(cursor) or {}
                vacancy_count = max(
                    0,
                    maximum_active - max(0, int(count_row.get("active_count") or 0)),
                )
                if vacancy_count == 0:
                    connection.commit()
                    return ()
                cursor.execute(
                    """
                    SELECT policy.merchant_id, policy.reserve_balance_quota,
                           policy.desired_priority, policy.desired_concurrency,
                           merchant.quota_per_unit, financial.user_balance_quota,
                           financial.source_hash AS financial_source_hash,
                           financial.observed_at AS financial_observed_at,
                           identity.token_ref_id, identity.binding_id,
                           identity.enodot_account_id
                    FROM a6_trade.residual_provider_policies policy
                    JOIN a6_trade.a6_merchants merchant
                      ON merchant.id = policy.merchant_id
                    JOIN a6_trade.a6_seller_retirement_plans retirement
                      ON retirement.merchant_id = merchant.id
                    JOIN a6_trade.a6_financial_current financial
                      ON financial.merchant_id = merchant.id
                    JOIN LATERAL (
                        SELECT token.id AS token_ref_id,
                               binding.id AS binding_id,
                               binding.enodot_account_id
                        FROM a6_trade.a6_tokens token
                        JOIN a6_trade.enodot_account_bindings binding
                          ON binding.a6_token_id = token.id
                        WHERE token.merchant_id = merchant.id
                          AND token.status IN ('1', 'active', 'enabled')
                          AND binding.state = 'bound'
                          AND binding.enodot_account_id IS NOT NULL
                        ORDER BY binding.id
                        LIMIT 1
                    ) identity ON true
                    WHERE policy.model = %s
                      AND policy.enabled
                      AND policy.state = 'candidate'
                      AND merchant.roles = ARRAY['observer']::text[]
                      AND merchant.status = 'supplier_submitted'
                      AND retirement.archive_mode
                      AND retirement.state = 'completed'
                      AND financial.user_balance_quota > policy.reserve_balance_quota
                    ORDER BY
                        (financial.user_balance_quota - policy.reserve_balance_quota)::numeric
                            / greatest(merchant.quota_per_unit, 1) DESC,
                        policy.merchant_id
                    FOR UPDATE OF policy, merchant SKIP LOCKED
                    LIMIT %s
                    """,
                    (model, vacancy_count),
                )
                candidates = _rows(cursor)
                promoted: list[int] = []
                for candidate in candidates:
                    merchant_id = _positive_int(
                        candidate.get("merchant_id"),
                        "residual_provider_candidate_invalid",
                    )
                    balance = max(0, int(candidate.get("user_balance_quota") or 0))
                    reserve = max(0, int(candidate.get("reserve_balance_quota") or 0))
                    priority = _positive_int(
                        candidate.get("desired_priority"),
                        "residual_provider_candidate_invalid",
                    )
                    concurrency = _positive_int(
                        candidate.get("desired_concurrency"),
                        "residual_provider_candidate_invalid",
                    )
                    token_ref_id = _positive_int(
                        candidate.get("token_ref_id"),
                        "residual_provider_candidate_invalid",
                    )
                    binding_id = _positive_int(
                        candidate.get("binding_id"),
                        "residual_provider_candidate_invalid",
                    )
                    account_id = _positive_int(
                        candidate.get("enodot_account_id"),
                        "residual_provider_candidate_invalid",
                    )
                    cursor.execute(
                        """
                        UPDATE a6_trade.residual_provider_policies
                        SET state = 'active', last_balance_quota = %s,
                            activated_at = now(), drained_at = NULL,
                            last_error_class = NULL, updated_at = now()
                        WHERE model = %s AND merchant_id = %s
                          AND enabled AND state = 'candidate'
                        """,
                        (balance, model, merchant_id),
                    )
                    cursor.execute(
                        """
                        UPDATE a6_trade.a6_merchants
                        SET roles = ARRAY['provider', 'observer']::text[],
                            role_revision = role_revision + 1,
                            updated_at = now()
                        WHERE id = %s AND roles = ARRAY['observer']::text[]
                        """,
                        (merchant_id,),
                    )
                    cursor.execute(
                        """
                        UPDATE a6_trade.enodot_account_bindings
                        SET desired_priority = %s, desired_concurrency = %s,
                            last_error_class = NULL, updated_at = now()
                        WHERE id = %s AND a6_token_id = %s
                          AND enodot_account_id = %s AND state = 'bound'
                        """,
                        (priority, concurrency, binding_id, token_ref_id, account_id),
                    )
                    cursor.execute(
                        """
                        INSERT INTO a6_trade.provider_inventory (
                            model, merchant_id, enodot_binding_id,
                            enodot_account_id, a6_token_id,
                            inventory_state, liquidity_state, funding_eligible,
                            schedulable, balance_units, financial_source_hash,
                            assigned_slot_no, last_financial_observed_at
                        ) VALUES (
                            %s, %s, %s, %s, %s,
                            'WARM_STANDBY', 'FUNDED', true,
                            false, %s, %s, NULL, %s
                        )
                        ON CONFLICT (model, merchant_id) DO UPDATE SET
                            enodot_binding_id = EXCLUDED.enodot_binding_id,
                            enodot_account_id = EXCLUDED.enodot_account_id,
                            a6_token_id = EXCLUDED.a6_token_id,
                            inventory_state = EXCLUDED.inventory_state,
                            liquidity_state = EXCLUDED.liquidity_state,
                            funding_eligible = EXCLUDED.funding_eligible,
                            schedulable = EXCLUDED.schedulable,
                            balance_units = EXCLUDED.balance_units,
                            financial_source_hash = EXCLUDED.financial_source_hash,
                            assigned_slot_no = NULL,
                            state_version = provider_inventory.state_version + 1,
                            last_financial_observed_at =
                                EXCLUDED.last_financial_observed_at,
                            updated_at = now()
                        """,
                        (
                            model,
                            merchant_id,
                            binding_id,
                            account_id,
                            token_ref_id,
                            balance,
                            candidate.get("financial_source_hash"),
                            candidate.get("financial_observed_at"),
                        ),
                    )
                    cursor.execute(
                        """
                        INSERT INTO a6_trade.residual_provider_events (
                            model, merchant_id, event_type, balance_quota,
                            reserve_balance_quota, details
                        ) VALUES (
                            %s, %s, 'candidate_promoted', %s, %s, %s::jsonb
                        )
                        """,
                        (
                            model,
                            merchant_id,
                            balance,
                            reserve,
                            json.dumps(
                                {
                                    "desired_priority": priority,
                                    "desired_concurrency": concurrency,
                                    "enodot_account_id": account_id,
                                },
                                ensure_ascii=True,
                                sort_keys=True,
                            ),
                        ),
                    )
                    cursor.execute(
                        """
                        INSERT INTO a6_trade.trade_control_actions (
                            idempotency_key, action_type, entity_type, entity_id,
                            desired_state, observed_before, observed_after, status,
                            config_version, requested_by, started_at, completed_at
                        ) VALUES (
                            %s, 'promote_residual_provider', 'merchant', %s,
                            %s::jsonb, %s::jsonb, %s::jsonb, 'completed',
                            'residual-provider-v1', 'trade-pin-providers-once',
                            now(), now()
                        )
                        ON CONFLICT (idempotency_key) DO NOTHING
                        """,
                        (
                            f"residual-provider:merchant:{merchant_id}:promote:v1",
                            str(merchant_id),
                            json.dumps(
                                {
                                    "roles": ["provider", "observer"],
                                    "reserve_balance_quota": reserve,
                                    "desired_concurrency": concurrency,
                                },
                                ensure_ascii=True,
                                sort_keys=True,
                            ),
                            json.dumps(
                                {"roles": ["observer"]},
                                ensure_ascii=True,
                                sort_keys=True,
                            ),
                            json.dumps(
                                {
                                    "roles": ["provider", "observer"],
                                    "schedulable": False,
                                },
                                ensure_ascii=True,
                                sort_keys=True,
                            ),
                        ),
                    )
                    promoted.append(merchant_id)
            connection.commit()
            return tuple(promoted)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def drain_residual_provider(
        self,
        context: ProviderPinContext,
        *,
        model: str,
        balance_quota: int,
        error_class: str,
    ) -> bool:
        """Return a spent temporary Provider to Observer-only after remote pause."""

        if not context.temporary_provider:
            return False
        balance = max(0, int(balance_quota))
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT state, reserve_balance_quota
                    FROM a6_trade.residual_provider_policies
                    WHERE model = %s AND merchant_id = %s AND enabled
                    FOR UPDATE
                    """,
                    (model, context.merchant_id),
                )
                policy = _row(cursor)
                if policy is None or str(policy.get("state") or "") != "active":
                    connection.rollback()
                    return False
                reserve = max(0, int(policy.get("reserve_balance_quota") or 0))
                if balance > reserve:
                    connection.rollback()
                    return False
                cursor.execute(
                    """
                    UPDATE a6_trade.a6_merchants
                    SET roles = ARRAY['observer']::text[],
                        role_revision = role_revision + 1,
                        updated_at = now()
                    WHERE id = %s
                      AND 'provider' = ANY(roles)
                    """,
                    (context.merchant_id,),
                )
                cursor.execute(
                    """
                    UPDATE a6_trade.residual_provider_policies
                    SET state = 'drained', last_balance_quota = %s,
                        drained_at = now(), last_error_class = %s,
                        updated_at = now()
                    WHERE model = %s AND merchant_id = %s AND state = 'active'
                    """,
                    (balance, error_class, model, context.merchant_id),
                )
                cursor.execute(
                    """
                    UPDATE a6_trade.provider_inventory
                    SET inventory_state = 'RETIRED', liquidity_state = 'UNFUNDED',
                        funding_eligible = false, schedulable = false,
                        balance_units = %s, assigned_slot_no = NULL,
                        state_version = state_version + 1,
                        last_financial_observed_at = now(), updated_at = now()
                    WHERE model = %s AND merchant_id = %s
                    """,
                    (balance, model, context.merchant_id),
                )
                cursor.execute(
                    """
                    INSERT INTO a6_trade.residual_provider_events (
                        model, merchant_id, event_type, balance_quota,
                        reserve_balance_quota, details
                    ) VALUES (%s, %s, 'reserve_reached', %s, %s, %s::jsonb)
                    """,
                    (
                        model,
                        context.merchant_id,
                        balance,
                        reserve,
                        json.dumps(
                            {
                                "enodot_account_id": context.enodot_account_id,
                                "error_class": error_class,
                                "roles_after": ["observer"],
                            },
                            ensure_ascii=True,
                            sort_keys=True,
                        ),
                    ),
                )
            connection.commit()
            return True
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def observer_contexts(self) -> tuple[ObserverContext, ...]:
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT m.id AS merchant_id, m.a6_user_id, t.a6_token_id,
                           m.session_ref
                    FROM a6_trade.a6_merchants m
                    JOIN LATERAL (
                        SELECT token.a6_token_id
                        FROM a6_trade.a6_tokens token
                        WHERE token.merchant_id = m.id
                          AND token.status IN ('1', 'active', 'enabled')
                        ORDER BY (lower(token.token_name) = 'callback'), token.id
                        LIMIT 1
                    ) t ON true
                    WHERE 'observer' = ANY(m.roles)
                      AND m.a6_user_id IS NOT NULL
                      AND m.status IN ('authenticated', 'supplier_submitted')
                    ORDER BY m.id
                    """
                )
                rows = _rows(cursor)
            return tuple(
                ObserverContext(
                    merchant_id=_positive_int(
                        row.get("merchant_id"), "observer_pin_context_invalid"
                    ),
                    a6_user_id=_positive_int(row.get("a6_user_id"), "observer_pin_context_invalid"),
                    token_id=_positive_int(row.get("a6_token_id"), "observer_pin_context_invalid"),
                    session_ref=str(row.get("session_ref") or ""),
                )
                for row in rows
            )
        finally:
            connection.close()

    def owned_channel_ids(self) -> frozenset[int]:
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT DISTINCT a6_channel_id
                    FROM a6_trade.a6_supplier_channels_current
                    WHERE a6_channel_id > 0
                    """
                )
                return frozenset(
                    _positive_int(row.get("a6_channel_id"), "owned_channel_invalid")
                    for row in _rows(cursor)
                )
        finally:
            connection.close()

    def current_channel(self, token_ref_id: int, model: str) -> int | None:
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT channel_id
                    FROM a6_trade.a6_pin_bindings
                    WHERE a6_token_id = %s AND model = %s AND valid_to IS NULL
                    ORDER BY valid_from DESC LIMIT 1
                    """,
                    (token_ref_id, model),
                )
                row = _row(cursor)
            return _optional_int(row.get("channel_id")) if row else None
        finally:
            connection.close()

    def pin_channel_at(
        self,
        token_ref_id: int,
        model: str,
        event_at_unix_nano: int,
    ) -> int | None:
        if isinstance(event_at_unix_nano, bool) or event_at_unix_nano < 1:
            raise ProviderPinRunnerError("pin_failure_event_invalid")
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT channel_id
                    FROM a6_trade.a6_pin_bindings
                    WHERE a6_token_id = %s
                      AND model = %s
                      AND valid_from <= to_timestamp(
                          %s::double precision / 1000000000.0
                      )
                      AND (
                          valid_to IS NULL
                          OR valid_to > to_timestamp(
                              %s::double precision / 1000000000.0
                          )
                      )
                    ORDER BY valid_from DESC, id DESC
                    LIMIT 1
                    """,
                    (
                        token_ref_id,
                        model,
                        event_at_unix_nano,
                        event_at_unix_nano,
                    ),
                )
                row = _row(cursor)
            return _optional_int(row.get("channel_id")) if row else None
        finally:
            connection.close()

    def mark_account_error(self, token_ref_id: int, error_class: str) -> None:
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE a6_trade.enodot_account_bindings
                    SET last_error_class = %s, last_error_at = now(), updated_at = now()
                    WHERE a6_token_id = %s AND state = 'bound'
                    """,
                    (str(error_class), token_ref_id),
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def clear_account_error(self, token_ref_id: int) -> None:
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE a6_trade.enodot_account_bindings
                    SET last_error_class = NULL, last_error_at = NULL,
                        cooldown_until = NULL, updated_at = now()
                    WHERE a6_token_id = %s AND state = 'bound'
                    """,
                    (token_ref_id,),
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def negative_probe_channel_ids(self, model: str, *, ttl_seconds: int) -> frozenset[int]:
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT DISTINCT (desired_state->>'channel_id')::bigint AS channel_id
                    FROM a6_trade.trade_control_actions
                    WHERE status = 'failed'
                      AND desired_state->>'model' = %s
                      AND desired_state->>'channel_id' ~ '^[1-9][0-9]*$'
                      AND completed_at >= now() - (%s * interval '1 second')
                      AND (
                          (
                              action_type = 'provider_pin_probe'
                              AND error_class IN (
                                  'fixed_merchant_unavailable',
                                  'account_quota_insufficient',
                                  'no_available_channel'
                              )
                          )
                          OR (
                              action_type = 'provider_pin_channel_failure'
                              AND error_class = 'fixed_merchant_unavailable'
                              AND desired_state->>'failure_streak_triggered' = 'true'
                          )
                      )
                    """,
                    (model, ttl_seconds),
                )
                return frozenset(
                    _positive_int(row.get("channel_id"), "provider_probe_channel_invalid")
                    for row in _rows(cursor)
                )
        finally:
            connection.close()

    def recent_failure_summaries(
        self,
        model: str,
        *,
        window_seconds: int,
    ) -> dict[int, dict[str, Any]]:
        if not 60 <= int(window_seconds) <= 86400:
            raise ProviderPinRunnerError("pin_failure_window_invalid")
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    WITH recent AS (
                        SELECT channel_id, failure_code, responsibility, failure_at
                        FROM a6_trade.a6_channel_failure_events
                        WHERE model = %s
                          AND failure_at >= now() - (%s * interval '1 second')
                    ), per_code AS (
                        SELECT channel_id, failure_code, responsibility,
                               count(*)::integer AS failure_count,
                               max(failure_at) AS last_failure_at
                        FROM recent
                        GROUP BY channel_id, failure_code, responsibility
                    ), per_channel AS (
                        SELECT channel_id,
                               count(*)::integer AS failure_count,
                               count(*) FILTER (WHERE responsibility = 'client')::integer
                                   AS client_failure_count,
                               count(*) FILTER (WHERE responsibility = 'provider')::integer
                                   AS provider_failure_count,
                               count(*) FILTER (WHERE responsibility = 'platform')::integer
                                   AS platform_failure_count,
                               count(*) FILTER (WHERE responsibility = 'unknown')::integer
                                   AS unknown_failure_count
                        FROM recent
                        GROUP BY channel_id
                    )
                    SELECT summary.*,
                           coalesce(codes.top_codes, '[]'::jsonb) AS top_codes
                    FROM per_channel summary
                    LEFT JOIN LATERAL (
                        SELECT jsonb_agg(
                            jsonb_build_object(
                                'code', ranked.failure_code,
                                'responsibility', ranked.responsibility,
                                'count', ranked.failure_count,
                                'last_failure_at', ranked.last_failure_at
                            ) ORDER BY ranked.failure_count DESC,
                                       ranked.last_failure_at DESC,
                                       ranked.failure_code
                        ) AS top_codes
                        FROM (
                            SELECT failure_code, responsibility, failure_count,
                                   last_failure_at
                            FROM per_code
                            WHERE per_code.channel_id = summary.channel_id
                            ORDER BY failure_count DESC, last_failure_at DESC,
                                     failure_code
                            LIMIT 8
                        ) ranked
                    ) codes ON true
                    """,
                    (model, int(window_seconds)),
                )
                rows = _rows(cursor)
            summaries: dict[int, dict[str, Any]] = {}
            for row in rows:
                channel_id = _positive_int(row.get("channel_id"), "pin_failure_channel_invalid")
                top_codes = row.get("top_codes")
                if not isinstance(top_codes, Sequence) or isinstance(top_codes, (str, bytes)):
                    top_codes = []
                summaries[channel_id] = {
                    "failure_count": max(0, int(row.get("failure_count") or 0)),
                    "client_failure_count": max(0, int(row.get("client_failure_count") or 0)),
                    "provider_failure_count": max(0, int(row.get("provider_failure_count") or 0)),
                    "platform_failure_count": max(0, int(row.get("platform_failure_count") or 0)),
                    "unknown_failure_count": max(0, int(row.get("unknown_failure_count") or 0)),
                    "top_codes": [dict(item) for item in top_codes if isinstance(item, Mapping)],
                }
            return summaries
        finally:
            connection.close()

    def probe_health_fresh(
        self,
        token_ref_id: int,
        model: str,
        channel_id: int,
        *,
        ttl_seconds: int,
    ) -> bool:
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT coalesce((
                        SELECT status = 'completed'
                            OR (
                                status = 'failed'
                                AND error_class = 'provider_probe_transport_failed'
                                AND completed_at >= now() - interval '45 seconds'
                            )
                        FROM a6_trade.trade_control_actions
                        WHERE action_type = 'provider_pin_probe'
                          AND entity_type = 'a6_provider_token'
                          AND entity_id = %s
                          AND status IN ('completed', 'failed')
                          AND desired_state->>'model' = %s
                          AND desired_state->>'channel_id' = %s
                          AND completed_at >= now() - (%s * interval '1 second')
                        ORDER BY completed_at DESC, id DESC
                        LIMIT 1
                    ), false) AS fresh
                    """,
                    (str(token_ref_id), model, str(channel_id), ttl_seconds),
                )
                row = _row(cursor)
                return bool((row or {}).get("fresh"))
        finally:
            connection.close()

    def recent_provider_probe_failed(
        self,
        token_ref_id: int,
        model: str,
        *,
        ttl_seconds: int,
    ) -> bool:
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT coalesce((
                        SELECT status = 'failed'
                            AND error_class IS DISTINCT FROM 'provider_probe_transport_failed'
                        FROM a6_trade.trade_control_actions
                        WHERE action_type = 'provider_pin_probe'
                          AND entity_type = 'a6_provider_token'
                          AND entity_id = %s
                          AND status IN ('completed', 'failed')
                          AND desired_state->>'model' = %s
                          AND completed_at >= now() - (%s * interval '1 second')
                        ORDER BY completed_at DESC, id DESC
                        LIMIT 1
                    ), false) AS failed
                    """,
                    (str(token_ref_id), model, ttl_seconds),
                )
                row = _row(cursor)
                return bool((row or {}).get("failed"))
        finally:
            connection.close()

    @contextmanager
    def provider_probe_lock(self, token_ref_id: int) -> Iterator[bool]:
        connection = self.connection_factory()
        acquired = False
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT pg_try_advisory_lock(%s, %s) AS acquired",
                    (0x41365052, token_ref_id),
                )
                row = _row(cursor)
                acquired = bool((row or {}).get("acquired"))
            yield acquired
        finally:
            if acquired:
                try:
                    with connection.cursor() as cursor:
                        cursor.execute(
                            "SELECT pg_advisory_unlock(%s, %s)",
                            (0x41365052, token_ref_id),
                        )
                except Exception:
                    pass
            connection.close()

    def record_probe(
        self,
        context: ProviderPinContext,
        *,
        model: str,
        channel_id: int,
        result: ProviderProbeResult,
        failure_ttl_seconds: int,
        config_version: str,
        elapsed_ms: int,
    ) -> None:
        desired = {
            "a6_token_id": context.token_ref_id,
            "channel_id": channel_id,
            "model": model,
            "failure_ttl_seconds": failure_ttl_seconds,
            "request_kind": "minimal_responses_probe",
        }
        observed = {
            "http_status": result.http_status,
            "probe_success": result.success,
            "short_circuited": result.retry_next_candidate,
        }
        key = f"provider-probe:{context.token_ref_id}:{channel_id}:{time.time_ns()}"
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO a6_trade.trade_control_actions (
                        idempotency_key, action_type, entity_type, entity_id,
                        desired_state, observed_after, status, error_class,
                        config_version, requested_by, started_at, completed_at, elapsed_ms
                    ) VALUES (
                        %s, 'provider_pin_probe', 'a6_provider_token', %s,
                        %s::jsonb, %s::jsonb, %s, %s,
                        %s, %s, now(), now(), %s
                    )
                    """,
                    (
                        key,
                        str(context.token_ref_id),
                        json.dumps(desired, ensure_ascii=True),
                        json.dumps(observed, ensure_ascii=True),
                        "completed" if result.success else "failed",
                        result.error_class,
                        config_version,
                        self.requested_by,
                        max(0, elapsed_ms),
                    ),
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def record_external_pin_failure(
        self,
        context: ProviderPinContext,
        *,
        model: str,
        channel_id: int,
        event_at_unix_nano: int,
        failure_ttl_seconds: int,
        failure_threshold: int,
        config_version: str,
    ) -> ExternalPinFailureStreak:
        if isinstance(failure_threshold, bool) or not 1 <= failure_threshold <= 100:
            raise ProviderPinRunnerError("pin_channel_failure_threshold_invalid")
        desired = {
            "a6_token_id": context.token_ref_id,
            "enodot_account_id": context.enodot_account_id,
            "channel_id": channel_id,
            "model": model,
            "failure_ttl_seconds": failure_ttl_seconds,
            "failure_streak_count": 0,
            "failure_streak_threshold": failure_threshold,
            "failure_streak_triggered": False,
            "failure_streak_success_reset": False,
            "request_kind": "sub2api_traffic_failure",
            "event_at_unix_nano": event_at_unix_nano,
        }
        observed = {
            "http_status": 402,
            "probe_success": False,
            "short_circuited": True,
            "source": "sub2api_failure_journal",
        }
        key = (
            "provider-channel-failure:"
            f"{context.enodot_account_id}:{context.token_ref_id}:"
            f"{model}:{channel_id}:{event_at_unix_nano}"
        )
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO a6_trade.trade_control_actions (
                        idempotency_key, action_type, entity_type, entity_id,
                        desired_state, observed_after, status, error_class,
                        config_version, requested_by, started_at, completed_at, elapsed_ms
                    ) VALUES (
                        %s, 'provider_pin_channel_failure', 'a6_provider_token', %s,
                        %s::jsonb, %s::jsonb, 'running', NULL,
                        %s, %s, now(), NULL, 0
                    )
                    ON CONFLICT (idempotency_key) DO NOTHING
                    RETURNING id
                    """,
                    (
                        key,
                        str(context.token_ref_id),
                        json.dumps(desired, ensure_ascii=True),
                        json.dumps(observed, ensure_ascii=True),
                        config_version,
                        self.requested_by,
                    ),
                )
                inserted_row = _row(cursor)
                if inserted_row is None:
                    cursor.execute(
                        """
                        SELECT desired_state->>'failure_streak_count' AS streak_count,
                               desired_state->>'failure_streak_threshold' AS streak_threshold,
                               desired_state->>'failure_streak_success_reset' AS success_reset
                        FROM a6_trade.trade_control_actions
                        WHERE idempotency_key = %s
                        """,
                        (key,),
                    )
                    existing = _row(cursor) or {}
                    connection.commit()
                    return ExternalPinFailureStreak(
                        count=int(existing.get("streak_count") or 0),
                        threshold=int(existing.get("streak_threshold") or failure_threshold),
                        triggered=False,
                        duplicate=True,
                        success_reset=str(existing.get("success_reset") or "").lower() == "true",
                    )

                action_id = _positive_int(
                    inserted_row.get("id"),
                    "pin_failure_action_insert_failed",
                )
                cursor.execute(
                    """
                    SELECT max(created_at) AS latest_success_at,
                           (
                               extract(epoch FROM max(created_at)) * 1000000000
                           )::bigint AS latest_success_ns
                    FROM public.usage_logs
                    WHERE account_id = %s
                      AND (model = %s OR requested_model = %s OR upstream_model = %s)
                      AND created_at <= to_timestamp(%s::double precision / 1000000000.0)
                    """,
                    (
                        context.enodot_account_id,
                        model,
                        model,
                        model,
                        event_at_unix_nano,
                    ),
                )
                success_row = _row(cursor) or {}
                latest_success_ns = (
                    int(success_row["latest_success_ns"])
                    if success_row.get("latest_success_ns") is not None
                    else None
                )
                cursor.execute(
                    """
                    SELECT (desired_state->>'event_at_unix_nano')::bigint AS event_ns
                    FROM a6_trade.trade_control_actions
                    WHERE action_type = 'provider_pin_channel_failure'
                      AND entity_type = 'a6_provider_token'
                      AND entity_id = %s
                      AND desired_state->>'enodot_account_id' = %s
                      AND desired_state->>'model' = %s
                      AND desired_state->>'channel_id' = %s
                      AND desired_state->>'request_kind' = 'sub2api_traffic_failure'
                      AND desired_state->>'event_at_unix_nano' ~ '^[1-9][0-9]*$'
                      AND (desired_state->>'event_at_unix_nano')::numeric <= %s
                    ORDER BY (desired_state->>'event_at_unix_nano')::numeric DESC
                    LIMIT %s
                    """,
                    (
                        str(context.token_ref_id),
                        str(context.enodot_account_id),
                        model,
                        str(channel_id),
                        event_at_unix_nano,
                        failure_threshold + 1,
                    ),
                )
                streak = _external_pin_failure_streak(
                    tuple(int(row["event_ns"]) for row in _rows(cursor)),
                    latest_success_ns=latest_success_ns,
                    threshold=failure_threshold,
                    inserted=True,
                )
                desired.update(
                    {
                        "failure_streak_count": streak.count,
                        "failure_streak_triggered": streak.triggered,
                        "failure_streak_success_reset": streak.success_reset,
                        "latest_success_at": (
                            str(success_row["latest_success_at"])
                            if success_row.get("latest_success_at") is not None
                            else None
                        ),
                    }
                )
                observed.update(
                    {
                        "failure_streak_count": streak.count,
                        "failure_streak_threshold": streak.threshold,
                        "failure_streak_triggered": streak.triggered,
                        "failure_streak_success_reset": streak.success_reset,
                    }
                )
                cursor.execute(
                    """
                    UPDATE a6_trade.trade_control_actions
                    SET desired_state = %s::jsonb,
                        observed_after = %s::jsonb,
                        status = 'failed',
                        error_class = 'fixed_merchant_unavailable',
                        completed_at = now()
                    WHERE id = %s
                    RETURNING id
                    """,
                    (
                        json.dumps(desired, ensure_ascii=True),
                        json.dumps(observed, ensure_ascii=True),
                        action_id,
                    ),
                )
                if _row(cursor) is None:
                    raise ProviderPinRunnerError("pin_failure_action_update_failed")
            connection.commit()
            return streak
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def emergency_boost_recent(
        self,
        token_ref_id: int,
        model: str,
        *,
        ttl_seconds: int,
    ) -> bool:
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT EXISTS (
                        SELECT 1
                        FROM a6_trade.trade_control_actions
                        WHERE action_type = 'provider_pin_emergency_boost'
                          AND entity_type = 'a6_provider_token'
                          AND entity_id = %s
                          AND desired_state->>'model' = %s
                          AND status IN ('completed', 'failed')
                          AND completed_at >= now() - (%s * interval '1 second')
                    ) AS recent
                    """,
                    (str(token_ref_id), model, ttl_seconds),
                )
                row = _row(cursor)
                return bool((row or {}).get("recent"))
        finally:
            connection.close()

    def record_emergency_boost(
        self,
        context: ProviderPinContext,
        *,
        model: str,
        source_channel_id: int,
        channel_id: int,
        requests: Sequence[Mapping[str, Any]],
        config_version: str,
        elapsed_ms: int,
    ) -> None:
        summarized = [
            {
                "profile": str(item.get("profile") or ""),
                "input_chars": max(0, int(item.get("input_chars") or 0)),
                "max_output_tokens": max(0, int(item.get("max_output_tokens") or 0)),
                "http_status": max(0, int(item.get("http_status") or 0)),
                "success": bool(item.get("success")),
                "error_class": str(item.get("error_class") or "") or None,
                "elapsed_ms": max(0, int(item.get("elapsed_ms") or 0)),
            }
            for item in requests
        ]
        success_count = sum(1 for item in summarized if item["success"])
        desired = {
            "a6_token_id": context.token_ref_id,
            "model": model,
            "source_channel_id": source_channel_id,
            "channel_id": channel_id,
            "request_kind": "normal_responses_emergency_boost",
            "request_count": len(summarized),
            "workload": [
                {key: item[key] for key in ("profile", "input_chars", "max_output_tokens")}
                for item in summarized
            ],
        }
        observed = {
            "request_count": len(summarized),
            "success_count": success_count,
            "failure_count": len(summarized) - success_count,
            "requests": summarized,
        }
        failed = success_count == 0
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO a6_trade.trade_control_actions (
                        idempotency_key, action_type, entity_type, entity_id,
                        desired_state, observed_after, status, error_class,
                        config_version, requested_by, started_at, completed_at, elapsed_ms
                    ) VALUES (
                        %s, 'provider_pin_emergency_boost', 'a6_provider_token', %s,
                        %s::jsonb, %s::jsonb, %s, %s,
                        %s, %s, now(), now(), %s
                    )
                    """,
                    (
                        f"provider-boost:{context.token_ref_id}:{channel_id}:{time.time_ns()}",
                        str(context.token_ref_id),
                        json.dumps(desired, ensure_ascii=True),
                        json.dumps(observed, ensure_ascii=True),
                        "failed" if failed else "completed",
                        "provider_emergency_boost_all_failed" if failed else None,
                        config_version,
                        self.requested_by,
                        max(0, elapsed_ms),
                    ),
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def record_scan(
        self,
        observer_merchant_id: int,
        model: str,
        quotes: Sequence[ObserverQuote],
        *,
        page_count: int,
        owned_channel_ids: frozenset[int],
        pinned_channel_ids: frozenset[int] = frozenset(),
        started_at_epoch: float | None = None,
        completed_at_epoch: float | None = None,
        elapsed_ms: int | None = None,
    ) -> None:
        completed_at_epoch = (
            time.time() if completed_at_epoch is None else float(completed_at_epoch)
        )
        started_at_epoch = (
            completed_at_epoch if started_at_epoch is None else float(started_at_epoch)
        )
        elapsed_ms = (
            max(0, round((completed_at_epoch - started_at_epoch) * 1000))
            if elapsed_ms is None
            else max(0, int(elapsed_ms))
        )
        payload = [
            {
                "channel_id": quote.channel_id,
                "listing_id": quote.listing_id,
                "supplier_id": quote.supplier_id,
                "model": quote.model,
                "input_price_micros": quote.input_price_micros,
                "output_price_micros": quote.output_price_micros,
                "final_multiplier": str(quote.final_multiplier)
                if quote.final_multiplier is not None
                else None,
                "realtime_ratio_usd": str(quote.realtime_ratio_usd)
                if quote.realtime_ratio_usd is not None
                else None,
                "realtime_ratio_cny": str(quote.realtime_ratio_cny)
                if quote.realtime_ratio_cny is not None
                else None,
                "exchange_rate": str(quote.exchange_rate)
                if quote.exchange_rate is not None
                else None,
                "available": quote.available,
                "route_status": quote.route_status,
                "circuit_state": quote.circuit_state,
                "circuit_probe_status": quote.circuit_probe_status,
                "circuit_last_error_kind": quote.circuit_last_error_kind,
                "circuit_probe_success_required": (quote.circuit_probe_success_required),
                "circuit_probe_success_streak": quote.circuit_probe_success_streak,
                "circuit_scope": quote.circuit_scope,
                "circuit_recovery_mode": quote.circuit_recovery_mode,
                "circuit_cooling_until": quote.circuit_cooling_until,
                "circuit_last_cooling_at": quote.circuit_last_cooling_at,
                "circuit_last_probe_at": quote.circuit_last_probe_at,
                "circuit_next_probe_at": quote.circuit_next_probe_at,
                "cooldown_state": quote.cooldown_state,
                "cooldown_label": quote.cooldown_label,
                "cooldown_remaining_seconds": quote.cooldown_remaining_seconds,
                "cooldown_expected_recovery_at": quote.cooldown_expected_recovery_at,
                "cooldown_next_probe_at": quote.cooldown_next_probe_at,
                "cooldown_sources": list(quote.cooldown_sources or ()),
                "capability_cooldowns": quote.capability_cooldowns or {},
                "smart_routing_status": quote.smart_routing_status,
                "smart_routing_labels": list(quote.smart_routing_labels),
                "listing_availability": quote.listing_availability,
                "last_success_at": quote.last_success_at,
                "last_failure_at": quote.last_failure_at,
                "last_failure_code": quote.last_failure_code,
                "last_failure_responsibility": quote.last_failure_responsibility,
                "last_sample_at": quote.last_sample_at,
                "sample_count": quote.sample_count,
                "recent_success_rate": quote.recent_success_rate,
                "success_buckets": quote.success_buckets or {},
                "recent_p50_ms": (
                    str(quote.recent_p50_ms) if quote.recent_p50_ms is not None else None
                ),
                "p50_ttft_ms": (
                    str(quote.p50_ttft_ms) if quote.p50_ttft_ms is not None else None
                ),
                "health_state": quote.health_state,
            }
            for quote in quotes
        ]
        source_hash = _hash(payload)
        ordered_quotes = sorted(
            quotes,
            key=lambda item: (
                item.final_multiplier or Decimal("Infinity"),
                item.input_price_micros,
                item.output_price_micros,
                item.channel_id,
            ),
        )
        healthy_quotes = tuple(item for item in ordered_quotes if item.available)
        healthy_top_ids = {quote.channel_id for quote in healthy_quotes[:10]}
        persisted_quotes = tuple(
            quote
            for quote in ordered_quotes
            if quote.channel_id in healthy_top_ids
            or quote.channel_id in owned_channel_ids
            or quote.channel_id in pinned_channel_ids
        )
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO a6_trade.a6_market_scan_runs (
                        observer_merchant_id, model, started_at, completed_at,
                        page_count, candidate_count, http_status, elapsed_ms,
                        result_hash, result_payload, status
                    ) VALUES (
                        %s, %s, to_timestamp(%s), to_timestamp(%s),
                        %s, %s, 200, %s, %s, %s::jsonb, 'completed'
                    )
                    RETURNING id
                    """,
                    (
                        observer_merchant_id,
                        model,
                        started_at_epoch,
                        completed_at_epoch,
                        page_count,
                        len(quotes),
                        elapsed_ms,
                        source_hash,
                        json.dumps(
                            payload,
                            ensure_ascii=True,
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                    ),
                )
                scan = _row(cursor)
                scan_id = _positive_int((scan or {}).get("id"), "market_scan_audit_failed")
                rank_by_channel = {
                    quote.channel_id: index for index, quote in enumerate(ordered_quotes, start=1)
                }
                signal_quotes = {quote.channel_id: quote for quote in ordered_quotes}
                signal_payload = [
                    {
                        "model": quote.model,
                        "channel_id": quote.channel_id,
                        "listing_id": quote.listing_id,
                        "supplier_id": quote.supplier_id,
                        "realtime_ratio_usd": str(quote.realtime_ratio_usd)
                        if quote.realtime_ratio_usd is not None
                        else None,
                        "realtime_ratio_cny": str(quote.realtime_ratio_cny)
                        if quote.realtime_ratio_cny is not None
                        else None,
                        "final_multiplier": str(quote.final_multiplier)
                        if quote.final_multiplier is not None
                        else None,
                        "market_rank": rank_by_channel[quote.channel_id],
                        "available": quote.available,
                        "is_owned": quote.channel_id in owned_channel_ids,
                        "route_status": quote.route_status,
                        "circuit_state": quote.circuit_state,
                        "circuit_probe_status": quote.circuit_probe_status,
                        "circuit_last_error_kind": quote.circuit_last_error_kind,
                        "circuit_probe_success_required": (quote.circuit_probe_success_required),
                        "circuit_probe_success_streak": (quote.circuit_probe_success_streak),
                        "circuit_scope": quote.circuit_scope,
                        "circuit_recovery_mode": quote.circuit_recovery_mode,
                        "circuit_cooling_until": quote.circuit_cooling_until,
                        "circuit_last_cooling_at": quote.circuit_last_cooling_at,
                        "circuit_last_probe_at": quote.circuit_last_probe_at,
                        "circuit_next_probe_at": quote.circuit_next_probe_at,
                        "cooldown_state": quote.cooldown_state,
                        "cooldown_label": quote.cooldown_label,
                        "cooldown_remaining_seconds": quote.cooldown_remaining_seconds,
                        "cooldown_expected_recovery_at": (quote.cooldown_expected_recovery_at),
                        "cooldown_next_probe_at": quote.cooldown_next_probe_at,
                        "cooldown_sources": list(quote.cooldown_sources or ()),
                        "capability_cooldowns": quote.capability_cooldowns or {},
                        "smart_routing_status": quote.smart_routing_status,
                        "smart_routing_labels": list(quote.smart_routing_labels),
                        "listing_availability": quote.listing_availability,
                        "last_success_at": quote.last_success_at,
                        "last_failure_at": quote.last_failure_at,
                        "last_failure_code": quote.last_failure_code,
                        "failure_responsibility": quote.last_failure_responsibility,
                        "last_sample_at": quote.last_sample_at,
                        "sample_count": quote.sample_count,
                        "recent_success_rate": quote.recent_success_rate,
                        "success_buckets": quote.success_buckets or {},
                        "recent_p50_ms": str(quote.recent_p50_ms)
                        if quote.recent_p50_ms is not None
                        else None,
                        "p50_ttft_ms": str(quote.p50_ttft_ms)
                        if quote.p50_ttft_ms is not None
                        else None,
                        "health_state": quote.health_state,
                        "health_evidence": channel_health_evidence(
                            {
                                "route_status": quote.route_status,
                                "circuit_state": quote.circuit_state,
                                "circuit_probe_status": quote.circuit_probe_status,
                                "circuit_last_error_kind": quote.circuit_last_error_kind,
                                "circuit_probe_success_required": (
                                    quote.circuit_probe_success_required
                                ),
                                "circuit_probe_success_streak": (
                                    quote.circuit_probe_success_streak
                                ),
                                "circuit_scope": quote.circuit_scope,
                                "circuit_recovery_mode": quote.circuit_recovery_mode,
                                "circuit_cooling_until": quote.circuit_cooling_until,
                                "circuit_last_cooling_at": quote.circuit_last_cooling_at,
                                "circuit_last_probe_at": quote.circuit_last_probe_at,
                                "circuit_next_probe_at": quote.circuit_next_probe_at,
                                "cooldown_state": quote.cooldown_state,
                                "cooldown_label": quote.cooldown_label,
                                "cooldown_remaining_seconds": (quote.cooldown_remaining_seconds),
                                "cooldown_expected_recovery_at": (
                                    quote.cooldown_expected_recovery_at
                                ),
                                "cooldown_next_probe_at": quote.cooldown_next_probe_at,
                                "cooldown_sources": quote.cooldown_sources or (),
                                "smart_routing_status": quote.smart_routing_status,
                                "smart_routing_labels": quote.smart_routing_labels,
                                "sample_count": quote.sample_count,
                                "recent_success_rate": quote.recent_success_rate,
                                "last_failure_code": quote.last_failure_code,
                                "recent_p50_ms": str(quote.recent_p50_ms)
                                if quote.recent_p50_ms is not None
                                else None,
                                "p50_ttft_ms": str(quote.p50_ttft_ms)
                                if quote.p50_ttft_ms is not None
                                else None,
                            }
                        ),
                        "observed_at": completed_at_epoch,
                    }
                    for quote in signal_quotes.values()
                ]
                cursor.execute(
                    _MARKET_SIGNAL_UPSERT_SQL,
                    (
                        json.dumps(signal_payload, ensure_ascii=True),
                        observer_merchant_id,
                        observer_merchant_id,
                        observer_merchant_id,
                        observer_merchant_id,
                        scan_id,
                        observer_merchant_id,
                        scan_id,
                    ),
                )
                quote_rows = [
                    (
                        scan_id,
                        quote.supplier_id,
                        quote.channel_id,
                        quote.listing_id,
                        quote.model,
                        quote.input_price_micros,
                        quote.output_price_micros,
                        quote.final_multiplier,
                        quote.realtime_ratio_usd,
                        quote.realtime_ratio_cny,
                        quote.exchange_rate,
                        quote.route_status,
                        quote.last_success_at,
                        quote.last_success_at,
                        quote.circuit_state,
                        rank_by_channel[quote.channel_id],
                        quote.channel_id in owned_channel_ids,
                        completed_at_epoch,
                        source_hash,
                    )
                    for quote in persisted_quotes
                ]
                if quote_rows:
                    cursor.executemany(
                        """
                        INSERT INTO a6_trade.a6_market_quotes (
                            scan_run_id, supplier_id, channel_id, listing_id, model,
                            input_price, output_price, final_multiplier,
                            realtime_ratio_usd, realtime_ratio_cny, exchange_rate,
                            route_status, last_success_at, circuit_state,
                            market_rank, is_owned, observed_at, source_hash
                        ) VALUES (
                            %s, %s, %s, %s, %s, %s, %s, %s,
                            %s, %s, %s,
                            %s, CASE
                                WHEN %s::double precision IS NULL THEN NULL
                                ELSE to_timestamp(%s::double precision)
                            END,
                            %s, %s, %s, to_timestamp(%s), %s
                        )
                        """,
                        quote_rows,
                    )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def latest_observer_snapshot(
        self,
        model: str,
        *,
        observer_merchant_ids: Sequence[int],
        max_age_seconds: int,
    ) -> PersistedObserverSnapshot:
        if (
            isinstance(max_age_seconds, bool)
            or not isinstance(max_age_seconds, int)
            or not 0 <= max_age_seconds <= 300
        ):
            raise ProviderPinRunnerError("observer_snapshot_max_age_invalid")
        observer_ids = tuple(
            dict.fromkeys(
                _positive_int(value, "observer_snapshot_merchant_invalid")
                for value in observer_merchant_ids
            )
        )
        if not observer_ids:
            raise ProviderPinRunnerError("observer_snapshot_merchants_empty")
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT DISTINCT ON (scan.observer_merchant_id)
                           scan.id AS scan_run_id,
                           scan.observer_merchant_id,
                           extract(epoch FROM scan.started_at)::double precision
                               AS started_at_epoch,
                           extract(epoch FROM scan.completed_at)::double precision
                               AS completed_at_epoch,
                           scan.page_count,
                           scan.candidate_count,
                           scan.elapsed_ms,
                           scan.result_hash,
                           scan.result_payload
                    FROM a6_trade.a6_market_scan_runs scan
                    WHERE scan.model = %s
                      AND scan.observer_merchant_id = ANY(%s)
                      AND scan.status = 'completed'
                      AND (
                          %s = 0
                          OR scan.completed_at >= now() - (%s * interval '1 second')
                      )
                      AND scan.result_hash IS NOT NULL
                      AND scan.result_payload IS NOT NULL
                    ORDER BY scan.observer_merchant_id,
                             scan.completed_at DESC,
                             scan.id DESC
                    """,
                    (model, list(observer_ids), max_age_seconds, max_age_seconds),
                )
                rows = _rows(cursor)
        finally:
            connection.close()

        quotes: list[ObserverQuote] = []
        scans: list[dict[str, Any]] = []
        completed_times: list[float] = []
        for row in rows:
            payload = row.get("result_payload")
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except json.JSONDecodeError:
                    continue
            if not isinstance(payload, Sequence) or isinstance(payload, (str, bytes)):
                continue
            payload_items = list(payload)
            if any(not isinstance(item, Mapping) for item in payload_items):
                continue
            if _hash(payload_items) != str(row.get("result_hash") or "").strip():
                continue
            observer_merchant_id = _positive_int(
                row.get("observer_merchant_id"),
                "observer_snapshot_merchant_invalid",
            )
            try:
                scan_quotes = tuple(
                    _observer_quote_from_payload(
                        observer_merchant_id,
                        item,
                        expected_model=model,
                    )
                    for item in payload_items
                )
            except ProviderPinRunnerError:
                continue
            completed_at_epoch = float(row.get("completed_at_epoch") or 0)
            if not math.isfinite(completed_at_epoch) or completed_at_epoch <= 0:
                continue
            quotes.extend(scan_quotes)
            completed_times.append(completed_at_epoch)
            scans.append(
                {
                    "merchant_id": observer_merchant_id,
                    "scan_run_id": _positive_int(
                        row.get("scan_run_id"),
                        "observer_snapshot_scan_invalid",
                    ),
                    "pages": max(0, int(row.get("page_count") or 0)),
                    "quotes": len(scan_quotes),
                    "observed_at_epoch": completed_at_epoch,
                    "elapsed_ms": max(0, int(row.get("elapsed_ms") or 0)),
                    "source": "persisted_observer_snapshot",
                }
            )
        return PersistedObserverSnapshot(
            quotes=tuple(quotes),
            scans=tuple(scans),
            latest_completed_at_epoch=max(completed_times, default=None),
            oldest_completed_at_epoch=min(completed_times, default=None),
        )

    def observer_scan_backoff(self, model: str) -> dict[str, Any] | None:
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT consecutive_failed_rounds, retry_after, last_error_class,
                           greatest(
                               0,
                               ceil(extract(epoch FROM retry_after - now()))
                           )::integer AS remaining_seconds
                    FROM a6_trade.a6_market_observer_scan_backoff
                    WHERE model = %s AND retry_after > now()
                    """,
                    (model,),
                )
                return _row(cursor)
        finally:
            connection.close()

    def claim_observer_round(self, model: str) -> int:
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO a6_trade.a6_market_observer_round_cursors (
                        model, next_round_index
                    ) VALUES (%s, 1)
                    ON CONFLICT (model) DO UPDATE SET
                        next_round_index = (
                            a6_trade.a6_market_observer_round_cursors.next_round_index + 1
                        ),
                        updated_at = now()
                    RETURNING next_round_index - 1 AS round_index
                    """,
                    (model,),
                )
                row = _row(cursor) or {}
            connection.commit()
            return max(0, int(row.get("round_index") or 0))
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def record_observer_scan_failure(
        self,
        observer_merchant_id: int,
        model: str,
        *,
        started_at_epoch: float,
        completed_at_epoch: float,
        error_class: str,
    ) -> None:
        elapsed_ms = max(0, round((completed_at_epoch - started_at_epoch) * 1000))
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO a6_trade.a6_market_scan_runs (
                        observer_merchant_id, model, started_at, completed_at,
                        page_count, candidate_count, elapsed_ms, status, error_class
                    ) VALUES (
                        %s, %s, to_timestamp(%s), to_timestamp(%s),
                        0, 0, %s, 'failed', %s
                    )
                    """,
                    (
                        observer_merchant_id,
                        model,
                        started_at_epoch,
                        completed_at_epoch,
                        elapsed_ms,
                        error_class,
                    ),
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def record_observer_round(
        self,
        model: str,
        *,
        quorum_met: bool,
        error_class: str | None,
    ) -> int:
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    DELETE FROM a6_trade.a6_channel_failure_events
                    WHERE failure_at < now() - interval '30 days'
                    """
                )
                cursor.execute(
                    """
                    SELECT consecutive_failed_rounds
                    FROM a6_trade.a6_market_observer_scan_backoff
                    WHERE model = %s
                    FOR UPDATE
                    """,
                    (model,),
                )
                current = _row(cursor) or {}
                if quorum_met:
                    cursor.execute(
                        """
                        INSERT INTO a6_trade.a6_market_observer_scan_backoff (
                            model, consecutive_failed_rounds, retry_after,
                            last_error_class, last_success_at
                        ) VALUES (%s, 0, NULL, NULL, now())
                        ON CONFLICT (model) DO UPDATE SET
                            consecutive_failed_rounds = 0,
                            retry_after = NULL,
                            last_error_class = NULL,
                            last_success_at = now(),
                            updated_at = now()
                        """,
                        (model,),
                    )
                    delay_seconds = 0
                else:
                    failures = int(current.get("consecutive_failed_rounds") or 0) + 1
                    delay_seconds = _observer_backoff_seconds(failures)
                    cursor.execute(
                        """
                        INSERT INTO a6_trade.a6_market_observer_scan_backoff (
                            model, consecutive_failed_rounds, retry_after,
                            last_error_class, last_failure_at
                        ) VALUES (
                            %s, %s, now() + (%s * interval '1 second'), %s, now()
                        )
                        ON CONFLICT (model) DO UPDATE SET
                            consecutive_failed_rounds = EXCLUDED.consecutive_failed_rounds,
                            retry_after = EXCLUDED.retry_after,
                            last_error_class = EXCLUDED.last_error_class,
                            last_failure_at = EXCLUDED.last_failure_at,
                            updated_at = now()
                        """,
                        (model, failures, delay_seconds, error_class),
                    )
            connection.commit()
            return delay_seconds
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def prepare(self, assignment: PinAssignment, *, config_version: str) -> str:
        candidate = assignment.candidate
        candidate_set = [
            {
                "channel_id": candidate.channel_id,
                "listing_id": candidate.listing_id,
                "observer_count": candidate.observer_count,
                "input_price_micros": candidate.median_input_price_micros,
                "output_price_micros": candidate.median_output_price_micros,
                "median_multiplier": str(candidate.median_multiplier)
                if candidate.median_multiplier is not None
                else None,
                "latest_success_at": candidate.latest_success_at,
                "median_success_rate": candidate.median_success_rate,
                "median_latency_ms": (
                    str(candidate.median_latency_ms)
                    if candidate.median_latency_ms is not None
                    else None
                ),
                "median_ttft_ms": (
                    str(candidate.median_ttft_ms) if candidate.median_ttft_ms is not None else None
                ),
                "effective_latency_ms": (
                    str(candidate.effective_latency_ms)
                    if candidate.effective_latency_ms is not None
                    else None
                ),
                "health_state": candidate.health_state,
                "failure_responsibility": candidate.failure_responsibility,
                "recent_failure_count": candidate.recent_failure_count,
                "recent_client_failure_count": candidate.recent_client_failure_count,
                "recent_provider_failure_count": candidate.recent_provider_failure_count,
                "recent_platform_failure_count": candidate.recent_platform_failure_count,
                "recent_unknown_failure_count": candidate.recent_unknown_failure_count,
                "recent_failure_codes": list(candidate.recent_failure_codes),
                "smart_routing_labels": list(candidate.smart_routing_labels),
                "price_score": str(candidate.price_score),
                "success_score": str(candidate.success_score),
                "latency_score": str(candidate.latency_score),
                "composite_score": str(candidate.composite_score),
            }
        ]
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT channel_id
                    FROM a6_trade.a6_pin_bindings
                    WHERE a6_token_id = %s AND model = %s AND valid_to IS NULL
                    ORDER BY valid_from DESC LIMIT 1
                    """,
                    (assignment.target.token_ref_id, candidate.model),
                )
                current = _row(cursor)
                cursor.execute(
                    """
                    INSERT INTO a6_trade.a6_pin_decisions (
                        a6_token_id, model, current_channel_id, selected_channel_id,
                        candidate_set, exclusion_reasons, expected_cost_multiplier,
                        expected_switch_gain, hysteresis_threshold, config_version,
                        decision_status
                    ) VALUES (
                        %s, %s, %s, %s, %s::jsonb, '{}'::jsonb, %s,
                        %s, %s, %s, 'applying'
                    )
                    RETURNING id
                    """,
                    (
                        assignment.target.token_ref_id,
                        candidate.model,
                        _optional_int((current or {}).get("channel_id")),
                        candidate.channel_id,
                        json.dumps(candidate_set, ensure_ascii=True),
                        candidate.median_multiplier,
                        assignment.expected_switch_gain,
                        assignment.hysteresis_threshold,
                        config_version,
                    ),
                )
                decision = _row(cursor)
                decision_id = _positive_int((decision or {}).get("id"), "pin_decision_audit_failed")
            connection.commit()
            return f"pin-decision:{decision_id}"
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def complete(
        self,
        action_ref: str,
        assignment: PinAssignment,
        observed_pin: Mapping[str, Any],
    ) -> None:
        decision_id = _positive_int(str(action_ref).rsplit(":", 1)[-1], "pin_decision_ref_invalid")
        candidate = assignment.candidate
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE a6_trade.a6_pin_bindings
                    SET valid_to = now()
                    WHERE a6_token_id = %s AND model = %s AND valid_to IS NULL
                      AND channel_id <> %s
                    """,
                    (assignment.target.token_ref_id, candidate.model, candidate.channel_id),
                )
                cursor.execute(
                    """
                    INSERT INTO a6_trade.a6_pin_bindings (
                        a6_token_id, model, channel_id, fallback_to_smart_routing,
                        valid_from, confirmed_at, change_reason, decision_id
                    )
                    SELECT %s, %s, %s, false, now(), now(), %s, %s
                    WHERE NOT EXISTS (
                        SELECT 1 FROM a6_trade.a6_pin_bindings
                        WHERE a6_token_id = %s AND model = %s AND valid_to IS NULL
                          AND channel_id = %s
                    )
                    """,
                    (
                        assignment.target.token_ref_id,
                        candidate.model,
                        candidate.channel_id,
                        assignment.switch_reason or "provider_dynamic_pin",
                        decision_id,
                        assignment.target.token_ref_id,
                        candidate.model,
                        candidate.channel_id,
                    ),
                )
                cursor.execute(
                    """
                    UPDATE a6_trade.a6_pin_bindings
                    SET decision_id = %s,
                        confirmed_at = coalesce(confirmed_at, now()),
                        change_reason = %s
                    WHERE a6_token_id = %s AND model = %s
                      AND channel_id = %s AND valid_to IS NULL
                    """,
                    (
                        decision_id,
                        assignment.switch_reason or "provider_dynamic_pin",
                        assignment.target.token_ref_id,
                        candidate.model,
                        candidate.channel_id,
                    ),
                )
                cursor.execute(
                    """
                    UPDATE a6_trade.a6_pin_decisions
                    SET decision_status = 'completed', decided_at = now()
                    WHERE id = %s
                    """,
                    (decision_id,),
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def fail(self, action_ref: str, *, error_class: str) -> None:
        decision_id = _positive_int(str(action_ref).rsplit(":", 1)[-1], "pin_decision_ref_invalid")
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE a6_trade.a6_pin_decisions
                    SET decision_status = 'failed',
                        exclusion_reasons = jsonb_build_object('error_class', %s),
                        decided_at = now()
                    WHERE id = %s
                    """,
                    (str(error_class), decision_id),
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()


class ProviderPinRunner:
    def __init__(
        self,
        store: PostgresPinAuditStore,
        *,
        sessions_dir: Path,
        proxy_by_user_id: Mapping[int, str],
        enodot_admin: EnodotAdminClient,
        client_timeout: float = 15,
    ) -> None:
        self.store = store
        self.sessions_dir = sessions_dir.expanduser().resolve()
        self.proxy_by_user_id = {int(key): str(value) for key, value in proxy_by_user_id.items()}
        self.enodot_admin = enodot_admin
        self.client_timeout = float(client_timeout)
        if not 1 <= self.client_timeout <= 15:
            raise ProviderPinRunnerError("provider_client_timeout_invalid")
        self._records: dict[int, Any] = {}
        self._session_paths: dict[int, Path] = {}
        for path in sorted(self.sessions_dir.glob("*.json")):
            resolved = path.expanduser().resolve()
            record = SessionStore(resolved).load()
            if record.user_id:
                self._records[int(record.user_id)] = record
                self._session_paths[int(record.user_id)] = resolved

    def _client(self, user_id: int) -> A6ApiClient:
        path = self._session_paths.get(user_id)
        if path is None:
            raise ProviderPinRunnerError("pin_session_missing")
        proxy = self.proxy_by_user_id.get(user_id)
        if not proxy:
            raise ProviderPinRunnerError("pin_proxy_mapping_missing")
        try:
            client, record = authenticated_a6_client(
                path,
                expected_user_id=user_id,
                proxy_url=proxy,
                client_factory=lambda current, fixed_proxy: A6ApiClient(
                    current.origin,
                    proxy_url=fixed_proxy,
                    timeout=self.client_timeout,
                ),
            )
        except AuthenticatedSessionError as exc:
            code = {
                "session_user_identity_mismatch": "pin_session_mismatch",
                "session_not_refreshable": "pin_session_ineligible",
                "session_proxy_missing": "pin_proxy_mapping_missing",
            }.get(exc.code, f"pin_{exc.code}")
            raise ProviderPinRunnerError(code) from exc
        self._records[user_id] = record
        return client

    def _observer_client(self, user_id: int) -> A6ApiClient:
        """Build a read-only client from the session refresher's verified cookie."""

        record = self._records.get(user_id)
        if record is None:
            raise ProviderPinRunnerError("pin_session_missing")
        if record.user_id is None or int(record.user_id) != user_id:
            raise ProviderPinRunnerError("pin_session_mismatch")
        if not record.cookies:
            raise ProviderPinRunnerError("pin_session_ineligible")
        proxy = self.proxy_by_user_id.get(user_id)
        if not proxy:
            raise ProviderPinRunnerError("pin_proxy_mapping_missing")
        client = A6ApiClient(
            record.origin,
            proxy_url=proxy,
            timeout=self.client_timeout,
        )
        client.import_cookies(record.cookies)
        return client

    def _scan_observer(
        self,
        observer: ObserverContext,
        *,
        model: str,
        page_size: int,
        round_deadline: float | None = None,
    ) -> tuple[tuple[ObserverQuote, ...], int]:
        client = self._observer_client(observer.a6_user_id)
        quotes: list[ObserverQuote] = []
        page = 1
        pages = 0
        total: int | None = None
        while page <= 20:
            _pin_round_checkpoint(round_deadline)
            request_scope = (
                client.request_deadline(round_deadline)
                if round_deadline is not None
                else nullcontext()
            )
            with request_scope:
                data = client.marketplace_channels(
                    observer.a6_user_id,
                    token_id=observer.token_id,
                    model=model,
                    page=page,
                    page_size=page_size,
                    sort="input_asc",
                    exclude_unavailable=False,
                )
            items = data.get("items") or []
            pages += 1
            if total is None:
                try:
                    total = int(data.get("total")) if data.get("total") is not None else None
                except (TypeError, ValueError):
                    total = None
            for item in items:
                channel_id = _optional_int(item.get("channel_id") or item.get("id"))
                if channel_id is None:
                    continue
                input_price = max(0, int(item.get("input_price_micros") or 0))
                output_price = max(0, int(item.get("output_price_micros") or 0))
                route_status = str(item.get("route_status") or "")
                circuit_status = _market_object(item.get("circuit_status"))
                cooldown_status = _market_object(item.get("cooldown_status"))
                circuit_state = (
                    str(circuit_status.get("state") or item.get("circuit_state") or "")
                    .strip()
                    .lower()
                )
                cooldown_state = str(cooldown_status.get("state") or "").strip().lower()
                available = _market_quote_available(
                    item,
                    route_status=route_status,
                    circuit_state=circuit_state,
                )
                realtime_ratio_usd = _decimal(item.get("realtime_ratio_usd"))
                realtime_ratio_cny = _decimal(item.get("realtime_ratio_cny"))
                exchange_rate = _decimal(item.get("exchange_rate"))
                final_multiplier = _decimal(item.get("final_multiplier")) or realtime_ratio_usd
                last_success_at = _optional_int(item.get("last_success_at"))
                last_failure_at = _optional_int(item.get("last_failure_at"))
                sample_count = _optional_nonnegative_int(item.get("sample_count"))
                recent_success_rate = _optional_nonnegative_int(item.get("recent_success_rate"))
                if recent_success_rate is not None and recent_success_rate > 10000:
                    recent_success_rate = None
                cooldown_remaining = _optional_nonnegative_int(
                    cooldown_status.get("remaining_seconds")
                )
                labels_value = item.get("smart_routing_labels")
                smart_routing_labels = (
                    tuple(
                        dict.fromkeys(
                            str(value).strip() for value in labels_value if str(value).strip()
                        )
                    )
                    if isinstance(labels_value, Sequence)
                    and not isinstance(labels_value, (str, bytes))
                    else ()
                )
                smart_routing_status = str(item.get("smart_routing_status") or "")
                last_failure_code = str(item.get("last_failure_code") or "").strip()[:128]
                last_failure_responsibility = classify_channel_failure(last_failure_code)
                health_state = classify_channel_health(
                    ChannelHealthInput(
                        available=available,
                        route_status=route_status,
                        circuit_state=circuit_state,
                        cooldown_state=cooldown_state,
                        cooldown_remaining_seconds=cooldown_remaining,
                        smart_routing_status=smart_routing_status,
                        smart_routing_labels=smart_routing_labels,
                        sample_count=sample_count,
                        recent_success_rate=recent_success_rate,
                        last_success_at=last_success_at,
                        last_failure_at=last_failure_at,
                        last_failure_code=last_failure_code,
                        last_failure_responsibility=last_failure_responsibility,
                    ),
                    now_epoch=int(time.time()),
                )
                quotes.append(
                    ObserverQuote(
                        observer_merchant_id=observer.merchant_id,
                        channel_id=channel_id,
                        listing_id=_optional_int(item.get("listing_id")),
                        model=str(item.get("model_name") or item.get("model") or model),
                        final_multiplier=final_multiplier,
                        input_price_micros=input_price,
                        output_price_micros=output_price,
                        available=available,
                        supplier_id=_optional_int(item.get("supplier_id")),
                        route_status=route_status,
                        last_success_at=last_success_at,
                        circuit_state=circuit_state,
                        circuit_probe_status=str(circuit_status.get("probe_status") or ""),
                        circuit_last_error_kind=str(circuit_status.get("last_error_kind") or ""),
                        circuit_probe_success_required=_optional_nonnegative_int(
                            circuit_status.get("probe_success_required")
                        ),
                        circuit_probe_success_streak=_optional_nonnegative_int(
                            circuit_status.get("probe_success_streak")
                        ),
                        circuit_scope=str(circuit_status.get("scope") or ""),
                        circuit_recovery_mode=str(circuit_status.get("recovery_mode") or ""),
                        circuit_cooling_until=_optional_int(circuit_status.get("cooling_until")),
                        circuit_last_cooling_at=_optional_int(
                            circuit_status.get("last_cooling_at")
                        ),
                        circuit_last_probe_at=_optional_int(circuit_status.get("last_probe_at")),
                        circuit_next_probe_at=_optional_int(circuit_status.get("next_probe_at")),
                        cooldown_state=cooldown_state,
                        cooldown_label=str(cooldown_status.get("label") or ""),
                        cooldown_remaining_seconds=cooldown_remaining,
                        cooldown_expected_recovery_at=_optional_int(
                            cooldown_status.get("expected_recovery_at")
                        ),
                        cooldown_next_probe_at=_optional_int(cooldown_status.get("next_probe_at")),
                        cooldown_sources=_json_array(cooldown_status.get("sources")),
                        capability_cooldowns=_json_value(
                            item.get("capability_cooldowns"), default={}
                        ),
                        smart_routing_status=smart_routing_status,
                        smart_routing_labels=smart_routing_labels,
                        listing_availability=_json_value(
                            item.get("listing_availability"), default=None
                        ),
                        last_failure_at=last_failure_at,
                        last_failure_code=last_failure_code,
                        last_failure_responsibility=last_failure_responsibility,
                        last_sample_at=_optional_int(item.get("last_sample_at")),
                        sample_count=sample_count,
                        recent_success_rate=recent_success_rate,
                        success_buckets=_json_value(item.get("success_buckets"), default={}),
                        recent_p50_ms=_nonnegative_decimal(item.get("recent_p50_ms")),
                        p50_ttft_ms=_nonnegative_decimal(item.get("p50_ttft_ms")),
                        health_state=health_state,
                        realtime_ratio_usd=realtime_ratio_usd,
                        realtime_ratio_cny=realtime_ratio_cny,
                        exchange_rate=exchange_rate,
                    )
                )
            if len(items) < page_size or (total is not None and page * page_size >= total):
                break
            page += 1
        if page > 20:
            raise ProviderPinRunnerError("pin_market_pages_exceeded")
        return tuple(quotes), pages

    def _scan_observer_observed(
        self,
        observer: ObserverContext,
        *,
        model: str,
        page_size: int,
        round_deadline: float | None = None,
    ) -> ObserverScanResult:
        started_at_epoch = time.time()
        started_monotonic = time.monotonic()
        quotes, page_count = self._scan_observer(
            observer,
            model=model,
            page_size=page_size,
            round_deadline=round_deadline,
        )
        elapsed_ms = max(0, round((time.monotonic() - started_monotonic) * 1000))
        return ObserverScanResult(
            quotes=quotes,
            page_count=page_count,
            started_at_epoch=started_at_epoch,
            completed_at_epoch=time.time(),
            elapsed_ms=elapsed_ms,
        )

    @staticmethod
    def _live_pin_channel(
        client: A6ApiClient,
        user_id: int,
        token_id: int,
        model: str,
    ) -> int | None:
        pins = client.marketplace_pins(user_id)
        matches = [
            item
            for item in pins
            if _optional_int(item.get("token_id")) == token_id
            and str(item.get("model_name") or "").strip() == model
            and not bool(item.get("deleted"))
        ]
        if len(matches) > 1:
            raise ProviderPinRunnerError("pin_live_readback_not_unique")
        if not matches:
            return None
        direct = _optional_int(matches[0].get("channel_id"))
        nested = matches[0].get("channel")
        if direct:
            return direct
        return _optional_int(nested.get("id")) if isinstance(nested, Mapping) else None

    def _probe_provider(
        self,
        context: ProviderPinContext,
        client: A6ApiClient,
        *,
        model: str,
        channel_id: int,
        failure_ttl_seconds: int,
        config_version: str,
        max_output_tokens: int,
        timeout_seconds: float,
    ) -> ProviderProbeResult:
        record = self._records.get(context.a6_user_id)
        api_key = str(getattr(record, "a6api_key", "") or "").strip()
        if not api_key or not api_key.startswith("sk-") or any(char.isspace() for char in api_key):
            raise ProviderPinRunnerError("provider_probe_api_key_missing")
        started = time.monotonic()
        try:
            status, document = client.provider_responses_probe(
                api_key,
                model,
                max_output_tokens=max_output_tokens,
                timeout=timeout_seconds,
            )
            if 200 <= status < 300:
                result = ProviderProbeResult(True, status, None, False)
            else:
                decision = classify_provider_error(status, document)
                result = ProviderProbeResult(
                    False,
                    status,
                    decision.error_class,
                    decision.short_circuit and decision.retry_next_provider,
                )
        except A6AutoError as exc:
            error_class = str(exc) or "provider_probe_runtime_error"
            result = ProviderProbeResult(
                False,
                0,
                error_class,
                error_class == "provider_probe_transport_failed",
            )
        elapsed_ms = max(0, round((time.monotonic() - started) * 1000))
        self.store.record_probe(
            context,
            model=model,
            channel_id=channel_id,
            result=result,
            failure_ttl_seconds=failure_ttl_seconds,
            config_version=config_version,
            elapsed_ms=elapsed_ms,
        )
        return result

    def _restore_provider_account(
        self,
        account_id: int,
        account_before: Mapping[str, Any],
        *,
        enodot_admin: Any | None = None,
        pool_guard: _SchedulablePoolGuard | None = None,
    ) -> Mapping[str, Any]:
        admin = self.enodot_admin if enodot_admin is None else enodot_admin
        try:
            if str(account_before.get("status") or "").strip().casefold() != "active":
                updated = admin.update_account(account_id, {"status": "active"})
                if pool_guard is not None:
                    pool_guard.observe(account_id, updated)
                if str(updated.get("status") or "").strip().casefold() != "active":
                    raise ProviderPinRunnerError("provider_account_status_recovery_unconfirmed")
            enabled = admin.set_schedulable(account_id, True)
            if pool_guard is not None:
                pool_guard.observe(account_id, enabled)
            observed = admin.get_account(account_id)
            if pool_guard is not None:
                pool_guard.observe(account_id, observed)
            if (
                str(observed.get("status") or "").strip().casefold() != "active"
                or observed.get("schedulable") is not True
            ):
                raise ProviderPinRunnerError("provider_account_recovery_readback_failed")
        except Exception:
            with suppress(Exception):
                disabled = admin.set_schedulable(account_id, False)
                if pool_guard is not None:
                    pool_guard.observe(account_id, disabled)
            raise
        return observed

    def _run_emergency_boost(
        self,
        context: ProviderPinContext,
        *,
        model: str,
        source_channel_id: int,
        channel_id: int,
        minimum_requests: int,
        maximum_requests: int,
        timeout_seconds: float,
        cooldown_seconds: int,
        config_version: str,
    ) -> dict[str, Any]:
        if self.store.emergency_boost_recent(
            context.token_ref_id,
            model,
            ttl_seconds=cooldown_seconds,
        ):
            return {
                "triggered": False,
                "skipped": "cooldown",
                "source_channel_id": source_channel_id,
                "channel_id": channel_id,
                "request_count": 0,
                "success_count": 0,
            }
        record = self._records.get(context.a6_user_id)
        api_key = str(getattr(record, "a6api_key", "") or "").strip()
        if (
            not api_key
            or not api_key.startswith("sk-")
            or any(character.isspace() for character in api_key)
        ):
            return {
                "triggered": False,
                "skipped": "provider_api_key_missing",
                "source_channel_id": source_channel_id,
                "channel_id": channel_id,
                "request_count": 0,
                "success_count": 0,
            }
        rng = random.SystemRandom()
        request_count = rng.randint(minimum_requests, maximum_requests)
        workloads = _emergency_boost_workload(request_count, rng=rng)

        def execute(workload: EmergencyBoostWorkload) -> dict[str, Any]:
            started = time.monotonic()
            status = 0
            error_class = ""
            success = False
            try:
                status, document = self._client(context.a6_user_id).provider_responses_request(
                    api_key,
                    model,
                    workload.input_text,
                    max_output_tokens=workload.max_output_tokens,
                    timeout=timeout_seconds,
                )
                response_status = str(document.get("status") or "").strip().lower()
                success = (
                    200 <= status < 300
                    and response_status not in {"failed", "incomplete", "cancelled"}
                    and not isinstance(document.get("error"), Mapping)
                )
                if not success:
                    decision = classify_provider_error(status or 502, document)
                    error_class = decision.error_class
            except A6AutoError as exc:
                error_class = str(exc) or "provider_request_runtime_error"
            except Exception:
                error_class = "provider_request_runtime_error"
            return {
                "profile": workload.profile,
                "input_chars": len(workload.input_text),
                "max_output_tokens": workload.max_output_tokens,
                "http_status": status,
                "success": success,
                "error_class": error_class or None,
                "elapsed_ms": max(
                    0,
                    round((time.monotonic() - started) * 1000),
                ),
            }

        started = time.monotonic()
        with ThreadPoolExecutor(max_workers=request_count) as executor:
            requests = list(executor.map(execute, workloads))
        elapsed_ms = max(0, round((time.monotonic() - started) * 1000))
        audit_recorded = True
        try:
            self.store.record_emergency_boost(
                context,
                model=model,
                source_channel_id=source_channel_id,
                channel_id=channel_id,
                requests=requests,
                config_version=config_version,
                elapsed_ms=elapsed_ms,
            )
        except Exception:
            audit_recorded = False
        return {
            "triggered": True,
            "source_channel_id": source_channel_id,
            "channel_id": channel_id,
            "request_count": request_count,
            "success_count": sum(1 for item in requests if item["success"]),
            "failure_count": sum(1 for item in requests if not item["success"]),
            "elapsed_ms": elapsed_ms,
            "audit_recorded": audit_recorded,
            "requests": requests,
        }

    @staticmethod
    def _candidate_order(
        assignment: PinAssignment,
        candidates: Sequence[PinCandidate],
    ) -> tuple[PinCandidate, ...]:
        return (assignment.candidate,) + tuple(
            candidate
            for candidate in candidates
            if candidate.channel_id != assignment.candidate.channel_id
        )

    def _reconcile_provider_pin(
        self,
        context: ProviderPinContext,
        client: A6ApiClient,
        assignment: PinAssignment,
        *,
        candidates: Sequence[PinCandidate],
        ranked_candidates: Sequence[PinCandidate] | None = None,
        owned_channel_ids: set[int],
        negative_channel_ids: set[int],
        model: str,
        config_version: str,
        apply: bool,
        probe_budget_remaining: int,
        probe_failure_ttl_seconds: int,
        probe_health_ttl_seconds: int,
        probe_max_output_tokens: int,
        probe_timeout_seconds: float,
        probe_round_deadline: float,
        balance_transferred: bool,
        pin_probe_mode: str = "required",
        reserved_channel_ids: frozenset[int] = frozenset(),
        emergency_channel_ids: frozenset[int] = frozenset(),
        force_predrain_channel_ids: frozenset[int] = frozenset(),
        pin_minimum_hold_seconds: int = 60,
        pin_minimum_switch_gain: Decimal | float | str = Decimal("0.05"),
        emergency_boost_minimum_requests: int = 3,
        emergency_boost_maximum_requests: int = 5,
        emergency_boost_timeout_seconds: float = 12,
        emergency_boost_cooldown_seconds: int = 60,
        pool_guard: _SchedulablePoolGuard | None = None,
        round_deadline: float | None = None,
        enodot_admin: Any | None = None,
    ) -> tuple[dict[str, Any], int]:
        admin = self.enodot_admin if enodot_admin is None else enodot_admin
        _pin_round_checkpoint(round_deadline)
        lock = self.store.provider_probe_lock(context.token_ref_id) if apply else nullcontext(True)
        with lock as acquired:
            if not acquired:
                return (
                    {
                        "merchant_id": context.merchant_id,
                        "action": "error",
                        "selected_channel_id": assignment.candidate.channel_id,
                        "applied": False,
                        "error_class": "provider_probe_lock_busy",
                    },
                    probe_budget_remaining,
                )
            _pin_round_checkpoint(round_deadline)
            current = self._live_pin_channel(
                client,
                context.a6_user_id,
                context.token_id,
                model,
            )
            emergency_source_channel_id = current if current in emergency_channel_ids else None
            unaudited_live_pin = current is not None and context.confirmed_pin_channel_id is None
            confirmed_pin_drift = (
                current is not None and current != context.confirmed_pin_channel_id
            )
            proposed_channel_id = assignment.candidate.channel_id
            ranked = tuple(ranked_candidates or candidates)

            def select_assignment(*, bypass: bool) -> tuple[PinAssignment, dict[str, Any]]:
                decision = apply_pin_hysteresis(
                    current,
                    assignment.candidate,
                    ranked,
                    current_age_seconds=(
                        context.confirmed_pin_age_seconds
                        if current == context.confirmed_pin_channel_id
                        else 0
                    ),
                    minimum_hold_seconds=pin_minimum_hold_seconds,
                    minimum_switch_gain=pin_minimum_switch_gain,
                    bypass=bypass,
                )
                selected = replace(
                    assignment,
                    candidate=decision.candidate,
                    expected_switch_gain=decision.expected_switch_gain,
                    hysteresis_threshold=Decimal(str(pin_minimum_switch_gain)),
                    switch_reason=decision.action,
                )
                return selected, {
                    "proposed_channel_id": proposed_channel_id,
                    "switch_policy": decision.action,
                    "expected_switch_gain": (
                        str(decision.expected_switch_gain)
                        if decision.expected_switch_gain is not None
                        else None
                    ),
                    "hysteresis_threshold": str(pin_minimum_switch_gain),
                    "pin_age_seconds": (
                        context.confirmed_pin_age_seconds
                        if current == context.confirmed_pin_channel_id
                        else 0
                    ),
                    "confirmed_pin_drift": confirmed_pin_drift,
                    "hold_remaining_seconds": decision.hold_remaining_seconds,
                }

            base_bypass = (
                confirmed_pin_drift
                or current in emergency_channel_ids
                or current in negative_channel_ids
                or current in reserved_channel_ids
            )
            if not apply:
                assignment, switch_details = select_assignment(bypass=base_bypass)
                if current == assignment.candidate.channel_id and not unaudited_live_pin:
                    return (
                        {
                            "merchant_id": context.merchant_id,
                            "action": (
                                switch_details["switch_policy"]
                                if str(switch_details["switch_policy"]).startswith("hold_")
                                else "noop"
                            ),
                            "current_channel_id": current,
                            "selected_channel_id": current,
                            "applied": False,
                            "confirmed": True,
                            "balance_transferred": balance_transferred,
                            **switch_details,
                        },
                        probe_budget_remaining,
                    )
                result = ProviderPinExecutor(
                    client,
                    admin,
                    audit_store=self.store,
                ).execute(
                    assignment,
                    owned_channel_ids=owned_channel_ids,
                    config_version=config_version,
                    apply=False,
                )
                _pin_round_checkpoint(round_deadline)
                return (
                    {
                        "merchant_id": result.merchant_id,
                        "action": result.action,
                        "current_channel_id": current,
                        "selected_channel_id": result.channel_id,
                        "applied": False,
                        "confirmed": False,
                        "balance_transferred": balance_transferred,
                        **switch_details,
                    },
                    probe_budget_remaining,
                )

            _pin_round_checkpoint(round_deadline)
            account = admin.get_account(context.enodot_account_id)
            if pool_guard is not None:
                pool_guard.observe(context.enodot_account_id, account)
            if context.temporary_provider:
                priority = context.desired_priority
                concurrency = context.desired_concurrency
                if priority is None or concurrency is None:
                    raise ProviderPinRunnerError("residual_provider_scheduling_policy_missing")
                if (
                    _optional_int(account.get("priority")) != priority
                    or _optional_int(account.get("concurrency")) != concurrency
                ):
                    account = admin.update_scheduling(
                        context.enodot_account_id,
                        priority=priority,
                        concurrency=concurrency,
                    )
                    if pool_guard is not None:
                        pool_guard.observe(context.enodot_account_id, account)
                    if (
                        _optional_int(account.get("priority")) != priority
                        or _optional_int(account.get("concurrency")) != concurrency
                    ):
                        account = admin.get_account(context.enodot_account_id)
                        if pool_guard is not None:
                            pool_guard.observe(context.enodot_account_id, account)
                    if (
                        _optional_int(account.get("priority")) != priority
                        or _optional_int(account.get("concurrency")) != concurrency
                    ):
                        raise ProviderPinRunnerError("residual_provider_scheduling_readback_failed")
            account_unhealthy = (
                str(account.get("status") or "").strip().casefold() != "active"
                or account.get("schedulable") is not True
            )
            recent_failure = self.store.recent_provider_probe_failed(
                context.token_ref_id,
                model,
                ttl_seconds=probe_failure_ttl_seconds,
            )
            assignment, switch_details = select_assignment(
                bypass=base_bypass or account_unhealthy or recent_failure,
            )
            probe_attempts: list[dict[str, Any]] = []
            pause_details: dict[str, Any] = {}
            account_drained = account.get("schedulable") is not True

            def pause_account(*, force: bool = False) -> bool:
                nonlocal account_drained, pause_details
                if account_drained:
                    return True
                if pool_guard is None:
                    observed = admin.set_schedulable(
                        context.enodot_account_id,
                        False,
                    )
                    if observed.get("schedulable") is not False:
                        observed = admin.get_account(context.enodot_account_id)
                    if observed.get("schedulable") is not False:
                        raise ProviderPinRunnerError("provider_account_predrain_readback_failed")
                    account_drained = True
                    return True
                decision = pool_guard.disable(context.enodot_account_id, force=force)
                pause_details = decision.public()
                account_drained = not decision.retained_for_capacity
                return account_drained

            if pin_probe_mode == "disabled":
                candidate = next(
                    (
                        item
                        for item in self._candidate_order(assignment, candidates)
                        if item.channel_id not in owned_channel_ids
                        and item.channel_id not in negative_channel_ids
                        and item.channel_id not in reserved_channel_ids
                    ),
                    None,
                )
                if candidate is None:
                    return (
                        {
                            "merchant_id": context.merchant_id,
                            "action": "error",
                            "current_channel_id": current,
                            "selected_channel_id": None,
                            "applied": False,
                            "confirmed": False,
                            "error_class": "provider_observer_candidates_exhausted",
                            "probe_mode": "disabled",
                            "probe_attempts": [],
                            **switch_details,
                        },
                        probe_budget_remaining,
                    )
                pin_changed = current != candidate.channel_id or unaudited_live_pin
                if not pin_changed and not account_unhealthy:
                    return (
                        {
                            "merchant_id": context.merchant_id,
                            "action": (
                                switch_details["switch_policy"]
                                if str(switch_details["switch_policy"]).startswith("hold_")
                                else "noop"
                            ),
                            "current_channel_id": current,
                            "selected_channel_id": current,
                            "applied": False,
                            "confirmed": True,
                            "observer_confirmed": True,
                            "probe_mode": "disabled",
                            "probe_attempts": [],
                            "readiness_state": "READY",
                            "balance_transferred": balance_transferred,
                            **switch_details,
                        },
                        probe_budget_remaining,
                    )
                candidate_assignment = replace(assignment, candidate=candidate)
                if pin_changed:
                    if not pause_account(force=current in force_predrain_channel_ids):
                        return (
                            {
                                "merchant_id": context.merchant_id,
                                "action": "pin_pending_capacity",
                                "current_channel_id": current,
                                "selected_channel_id": candidate.channel_id,
                                "applied": False,
                                "confirmed": current is not None,
                                "error_class": "provider_pin_predrain_capacity_guard",
                                "observer_confirmed": True,
                                "probe_mode": "disabled",
                                "probe_attempts": [],
                                "balance_transferred": balance_transferred,
                                **switch_details,
                                **pause_details,
                            },
                            probe_budget_remaining,
                        )
                    pin_result = ProviderPinExecutor(
                        client,
                        admin,
                        audit_store=self.store,
                    ).execute(
                        candidate_assignment,
                        owned_channel_ids=owned_channel_ids,
                        config_version=config_version,
                        apply=True,
                        pause_before_change=False,
                    )
                    _pin_round_checkpoint(round_deadline)
                    if not pin_result.confirmed:
                        raise ProviderPinRunnerError("provider_pin_readback_unconfirmed")
                    current = candidate.channel_id
                confirmed_channel = self._live_pin_channel(
                    client,
                    context.a6_user_id,
                    context.token_id,
                    model,
                )
                if confirmed_channel != candidate.channel_id:
                    raise ProviderPinRunnerError("provider_pin_readback_mismatch")
                if account_drained or account_unhealthy:
                    _pin_round_checkpoint(round_deadline)
                    self._restore_provider_account(
                        context.enodot_account_id,
                        account,
                        enodot_admin=admin,
                        pool_guard=pool_guard,
                    )
                return (
                    {
                        "merchant_id": context.merchant_id,
                        "action": "pin" if pin_changed else "observer_recovered",
                        "current_channel_id": current,
                        "selected_channel_id": candidate.channel_id,
                        "applied": True,
                        "confirmed": True,
                        "observer_confirmed": True,
                        "probe_confirmed": False,
                        "probe_mode": "disabled",
                        "probe_attempts": [],
                        "readiness_state": (
                            "READY_UNPROVEN" if pin_changed else "READY"
                        ),
                        "balance_transferred": balance_transferred,
                        **switch_details,
                        **pause_details,
                    },
                    probe_budget_remaining,
                )

            last_error = "provider_probe_candidates_exhausted"
            for candidate in self._candidate_order(assignment, candidates):
                _pin_round_checkpoint(round_deadline)
                if (
                    candidate.channel_id in owned_channel_ids
                    or candidate.channel_id in negative_channel_ids
                    or candidate.channel_id in reserved_channel_ids
                ):
                    continue
                pin_changed = current != candidate.channel_id or unaudited_live_pin
                health_fresh = False
                if not pin_changed:
                    health_fresh = self.store.probe_health_fresh(
                        context.token_ref_id,
                        model,
                        candidate.channel_id,
                        ttl_seconds=probe_health_ttl_seconds,
                    )
                needs_probe = pin_changed or account_unhealthy or recent_failure or not health_fresh
                if not needs_probe:
                    return (
                        {
                            "merchant_id": context.merchant_id,
                            "action": (
                                switch_details["switch_policy"]
                                if str(switch_details["switch_policy"]).startswith("hold_")
                                else "noop"
                            ),
                            "current_channel_id": current,
                            "selected_channel_id": current,
                            "applied": False,
                            "confirmed": True,
                            "probe_health_fresh": True,
                            "balance_transferred": balance_transferred,
                            **switch_details,
                        },
                        probe_budget_remaining,
                    )
                if probe_budget_remaining < 1:
                    return (
                        {
                            "merchant_id": context.merchant_id,
                            "action": "error",
                            "current_channel_id": current,
                            "selected_channel_id": candidate.channel_id,
                            "applied": False,
                            "error_class": "provider_probe_budget_exhausted",
                            "probe_attempts": probe_attempts,
                            **switch_details,
                            **pause_details,
                        },
                        probe_budget_remaining,
                    )
                probe_time_remaining = probe_round_deadline - time.monotonic()
                if probe_time_remaining <= 0.25:
                    return (
                        {
                            "merchant_id": context.merchant_id,
                            "action": "error",
                            "current_channel_id": current,
                            "selected_channel_id": candidate.channel_id,
                            "applied": False,
                            "error_class": "provider_probe_time_budget_exhausted",
                            "probe_attempts": probe_attempts,
                            **switch_details,
                            **pause_details,
                        },
                        probe_budget_remaining,
                    )

                candidate_assignment = replace(assignment, candidate=candidate)
                if pin_changed:
                    if not pause_account(force=current in force_predrain_channel_ids):
                        return (
                            {
                                "merchant_id": context.merchant_id,
                                "action": "pin_pending_capacity",
                                "current_channel_id": current,
                                "selected_channel_id": candidate.channel_id,
                                "applied": False,
                                "confirmed": current is not None,
                                "error_class": "provider_pin_predrain_capacity_guard",
                                "probe_attempts": probe_attempts,
                                "balance_transferred": balance_transferred,
                                **switch_details,
                                **pause_details,
                            },
                            probe_budget_remaining,
                        )
                    pin_result = ProviderPinExecutor(
                        client,
                        admin,
                        audit_store=self.store,
                    ).execute(
                        candidate_assignment,
                        owned_channel_ids=owned_channel_ids,
                        config_version=config_version,
                        apply=True,
                        pause_before_change=False,
                    )
                    _pin_round_checkpoint(round_deadline)
                    if not pin_result.confirmed:
                        raise ProviderPinRunnerError("provider_pin_readback_unconfirmed")
                    current = candidate.channel_id
                elif account_unhealthy:
                    pause_account()
                confirmed_channel = self._live_pin_channel(
                    client,
                    context.a6_user_id,
                    context.token_id,
                    model,
                )
                if confirmed_channel != candidate.channel_id:
                    raise ProviderPinRunnerError("provider_probe_pin_readback_mismatch")

                probe_time_remaining = probe_round_deadline - time.monotonic()
                if probe_time_remaining <= 0.25:
                    return (
                        {
                            "merchant_id": context.merchant_id,
                            "action": "error",
                            "current_channel_id": current,
                            "selected_channel_id": candidate.channel_id,
                            "applied": False,
                            "confirmed": True,
                            "error_class": "provider_probe_time_budget_exhausted",
                            "probe_attempts": probe_attempts,
                            **switch_details,
                            **pause_details,
                        },
                        probe_budget_remaining,
                    )

                probe_budget_remaining -= 1
                try:
                    probe = self._probe_provider(
                        context,
                        client,
                        model=model,
                        channel_id=candidate.channel_id,
                        failure_ttl_seconds=probe_failure_ttl_seconds,
                        config_version=config_version,
                        max_output_tokens=probe_max_output_tokens,
                        timeout_seconds=_pin_remote_timeout(
                            probe_round_deadline,
                            probe_timeout_seconds,
                        ),
                    )
                except Exception:
                    pause_account(force=True)
                    raise
                probe_attempts.append(
                    {
                        "channel_id": candidate.channel_id,
                        "http_status": probe.http_status,
                        "success": probe.success,
                        "error_class": probe.error_class,
                    }
                )
                if probe.success:
                    _pin_round_checkpoint(round_deadline)
                    self._restore_provider_account(
                        context.enodot_account_id,
                        account,
                        enodot_admin=admin,
                        pool_guard=pool_guard,
                    )
                    emergency_boost = None
                    if pin_changed and emergency_source_channel_id is not None:
                        _pin_round_checkpoint(round_deadline)
                        emergency_boost = self._run_emergency_boost(
                            context,
                            model=model,
                            source_channel_id=emergency_source_channel_id,
                            channel_id=candidate.channel_id,
                            minimum_requests=emergency_boost_minimum_requests,
                            maximum_requests=emergency_boost_maximum_requests,
                            timeout_seconds=(
                                _pin_remote_timeout(
                                    round_deadline,
                                    emergency_boost_timeout_seconds,
                                )
                                if round_deadline is not None
                                else emergency_boost_timeout_seconds
                            ),
                            cooldown_seconds=emergency_boost_cooldown_seconds,
                            config_version=config_version,
                        )
                    return (
                        {
                            "merchant_id": context.merchant_id,
                            "action": "pin" if pin_changed else "probe_recovered",
                            "current_channel_id": current,
                            "selected_channel_id": candidate.channel_id,
                            "applied": True,
                            "confirmed": True,
                            "probe_confirmed": True,
                            "probe_attempts": probe_attempts,
                            "emergency_channel_detected": (emergency_source_channel_id is not None),
                            "emergency_boost": emergency_boost,
                            "balance_transferred": balance_transferred,
                            **switch_details,
                        },
                        probe_budget_remaining,
                    )
                last_error = probe.error_class or "provider_probe_failed"
                pause_account(force=True)
                if probe.error_class == "provider_probe_transport_failed" and not account_unhealthy:
                    return (
                        {
                            "merchant_id": context.merchant_id,
                            "action": "pin_pending_probe" if pin_changed else "probe_pending",
                            "current_channel_id": current,
                            "selected_channel_id": candidate.channel_id,
                            "applied": pin_changed,
                            "confirmed": True,
                            "probe_confirmed": False,
                            "probe_pending": True,
                            "probe_attempts": probe_attempts,
                            "balance_transferred": balance_transferred,
                            **switch_details,
                        },
                        probe_budget_remaining,
                    )
                account_unhealthy = account_drained or account_unhealthy
                if not probe.retry_next_candidate:
                    return (
                        {
                            "merchant_id": context.merchant_id,
                            "action": "error",
                            "current_channel_id": current,
                            "selected_channel_id": candidate.channel_id,
                            "applied": False,
                            "confirmed": False,
                            "error_class": last_error,
                            "probe_attempts": probe_attempts,
                            **switch_details,
                            **pause_details,
                        },
                        probe_budget_remaining,
                    )
                negative_channel_ids.add(candidate.channel_id)
                recent_failure = True

            return (
                {
                    "merchant_id": context.merchant_id,
                    "action": "error",
                    "current_channel_id": current,
                    "selected_channel_id": None,
                    "applied": False,
                    "confirmed": False,
                    "error_class": last_error,
                    "probe_attempts": probe_attempts,
                    **switch_details,
                    **pause_details,
                },
                probe_budget_remaining,
            )

    def run(
        self,
        *,
        model: str = "gpt-5.6-sol",
        min_observers: int = 2,
        top_k: int = 5,
        page_size: int = 100,
        observer_window_seconds: float = 10.0,
        observer_round_size: int | None = 3,
        observer_snapshot_max_age_seconds: int | None = None,
        config_version: str = "phase1-provider-pin-v1",
        pin_probe_mode: str = "required",
        probe_budget: int = 4,
        probe_failure_ttl_seconds: int = 45,
        probe_health_ttl_seconds: int = 300,
        probe_max_output_tokens: int = 16,
        probe_timeout_seconds: float = 6,
        probe_round_time_budget_seconds: float = 8,
        pin_price_weight: Decimal | float | str = Decimal("1"),
        pin_success_weight: Decimal | float | str = Decimal("0"),
        pin_latency_weight: Decimal | float | str = Decimal("0"),
        pin_price_span_ratio: Decimal | float | str = Decimal("0.25"),
        pin_minimum_success_rate: int = 0,
        pin_maximum_latency_ms: Decimal | float | str = Decimal("10000"),
        pin_maximum_multiplier: Decimal | float | str = Decimal("0.05"),
        pin_maximum_relative_cost_ratio: Decimal | float | str = Decimal("2"),
        pin_failure_window_seconds: int = 900,
        pin_channel_failure_threshold: int = 3,
        provider_initial_balance_ratio: Decimal | float | str = Decimal("0"),
        minimum_schedulable_capacity: int = 100,
        pin_minimum_hold_seconds: int = 60,
        pin_minimum_switch_gain: Decimal | float | str = Decimal("0"),
        emergency_boost_minimum_requests: int = 3,
        emergency_boost_maximum_requests: int = 5,
        emergency_boost_timeout_seconds: float = 12,
        emergency_boost_cooldown_seconds: int = 60,
        round_time_budget_seconds: float = 90,
        failed_enodot_account_events: (
            Mapping[int, int] | Sequence[tuple[int, int]] | None
        ) = None,
        observe_only: bool = False,
        apply: bool = False,
    ) -> dict[str, Any]:
        round_deadline = _pin_round_deadline(round_time_budget_seconds)
        residual_provider_promotions: tuple[int, ...] = ()
        _pin_round_checkpoint(round_deadline)
        promote = getattr(self.store, "promote_residual_provider_candidates", None)
        if apply and callable(promote):
            residual_provider_promotions = tuple(
                int(value) for value in promote(model, maximum_active=3)
            )
        _pin_round_checkpoint(round_deadline)
        providers = self.store.provider_contexts(model)
        observer_pool = self.store.observer_contexts()
        failure_events = (
            tuple(sorted(failed_enodot_account_events.items()))
            if isinstance(failed_enodot_account_events, Mapping)
            else tuple(failed_enodot_account_events or ())
        )
        if not providers:
            raise ProviderPinRunnerError("pin_provider_targets_empty")
        if len(observer_pool) < min_observers:
            raise ProviderPinRunnerError("pin_observer_quorum_unavailable")
        if observer_window_seconds <= 0:
            raise ProviderPinRunnerError("pin_observer_window_invalid")
        if not isinstance(observe_only, bool):
            raise ProviderPinRunnerError("pin_observe_only_invalid")
        if observer_snapshot_max_age_seconds is not None and (
            isinstance(observer_snapshot_max_age_seconds, bool)
            or not isinstance(observer_snapshot_max_age_seconds, int)
            or not 0 <= observer_snapshot_max_age_seconds <= 300
        ):
            raise ProviderPinRunnerError("observer_snapshot_max_age_invalid")
        use_observer_snapshot = (
            observer_snapshot_max_age_seconds is not None and not observe_only
        )
        if observer_round_size is not None and (
            isinstance(observer_round_size, bool)
            or observer_round_size < min_observers
        ):
            raise ProviderPinRunnerError("pin_observer_round_size_invalid")
        selected_observer_count = (
            len(observer_pool) if observer_round_size is None else observer_round_size
        )
        backoff = (
            None
            if observe_only or use_observer_snapshot
            else self.store.observer_scan_backoff(model)
        )
        if backoff is not None and not failure_events:
            return {
                "success": True,
                "mode": "observer_backoff",
                "model": model,
                "providers": len(providers),
                "observers": min(len(observer_pool), selected_observer_count),
                "observer_pool_count": len(observer_pool),
                "consecutive_failed_rounds": int(backoff.get("consecutive_failed_rounds") or 0),
                "retry_after": (
                    str(backoff["retry_after"]) if backoff.get("retry_after") is not None else None
                ),
                "remaining_seconds": int(backoff.get("remaining_seconds") or 0),
                "last_error_class": str(backoff.get("last_error_class") or ""),
                "residual_provider_promotions": list(residual_provider_promotions),
                "scans": [],
                "assignments": [],
            }
        if use_observer_snapshot:
            observers = tuple(observer_pool)
        else:
            claim_observer_round = getattr(self.store, "claim_observer_round", None)
            round_index = (
                int(claim_observer_round(model))
                if callable(claim_observer_round)
                else int(time.time() // observer_window_seconds)
            )
            observers = _select_observer_round(
                observer_pool,
                round_size=selected_observer_count,
                round_index=round_index,
            )
        pin_probe_mode = str(pin_probe_mode).strip().lower()
        if pin_probe_mode not in {"required", "disabled"}:
            raise ProviderPinRunnerError("provider_pin_probe_mode_invalid")
        minimum_probe_budget = 0 if pin_probe_mode == "disabled" else 1
        if (
            isinstance(probe_budget, bool)
            or not minimum_probe_budget <= probe_budget <= 100
        ):
            raise ProviderPinRunnerError("provider_probe_budget_invalid")
        if isinstance(probe_failure_ttl_seconds, bool) or not 30 <= probe_failure_ttl_seconds <= 60:
            raise ProviderPinRunnerError("provider_probe_failure_ttl_invalid")
        if isinstance(probe_health_ttl_seconds, bool) or not 60 <= probe_health_ttl_seconds <= 3600:
            raise ProviderPinRunnerError("provider_probe_health_ttl_invalid")
        if isinstance(probe_max_output_tokens, bool) or not 1 <= probe_max_output_tokens <= 64:
            raise ProviderPinRunnerError("provider_probe_output_limit_invalid")
        probe_timeout_seconds, probe_round_time_budget_seconds = _validated_probe_timing(
            probe_timeout_seconds,
            probe_round_time_budget_seconds,
        )
        if (
            isinstance(emergency_boost_minimum_requests, bool)
            or isinstance(emergency_boost_maximum_requests, bool)
            or not 3 <= emergency_boost_minimum_requests <= 5
            or not emergency_boost_minimum_requests <= emergency_boost_maximum_requests <= 5
        ):
            raise ProviderPinRunnerError("provider_emergency_boost_count_invalid")
        if (
            isinstance(emergency_boost_timeout_seconds, bool)
            or not 3 <= emergency_boost_timeout_seconds <= 30
        ):
            raise ProviderPinRunnerError("provider_emergency_boost_timeout_invalid")
        if (
            isinstance(emergency_boost_cooldown_seconds, bool)
            or not 30 <= emergency_boost_cooldown_seconds <= 3600
        ):
            raise ProviderPinRunnerError("provider_emergency_boost_cooldown_invalid")
        if (
            isinstance(pin_failure_window_seconds, bool)
            or not 60 <= pin_failure_window_seconds <= 86400
        ):
            raise ProviderPinRunnerError("pin_failure_window_invalid")
        if (
            isinstance(pin_channel_failure_threshold, bool)
            or not 1 <= pin_channel_failure_threshold <= 100
        ):
            raise ProviderPinRunnerError("pin_channel_failure_threshold_invalid")
        try:
            initial_balance_ratio = Decimal(str(provider_initial_balance_ratio))
        except (InvalidOperation, ValueError):
            raise ProviderPinRunnerError("provider_initial_balance_ratio_invalid") from None
        if (
            not initial_balance_ratio.is_finite()
            or initial_balance_ratio < 0
            or initial_balance_ratio > 1
        ):
            raise ProviderPinRunnerError("provider_initial_balance_ratio_invalid")
        if (
            isinstance(minimum_schedulable_capacity, bool)
            or not 1 <= minimum_schedulable_capacity <= 1_000_000
        ):
            raise ProviderPinRunnerError("provider_pool_minimum_capacity_invalid")
        owned = set(self.store.owned_channel_ids())
        pinned_channel_ids = frozenset(
            context.confirmed_pin_channel_id
            for context in providers
            if context.confirmed_pin_channel_id is not None
        )
        negative_channels = set(
            self.store.negative_probe_channel_ids(
                model,
                ttl_seconds=probe_failure_ttl_seconds,
            )
        )
        failure_targets = _pin_failure_targets(providers, failure_events)
        resolved_failures = _resolve_pin_failures(
            self.store,
            failure_targets,
            model=model,
        )
        triggered_channels: set[int] = set()
        failure_streaks: list[dict[str, Any]] = []
        if apply:
            for failure in resolved_failures:
                context = failure.context
                event_ns = failure.event_at_unix_nano
                channel_id = failure.channel_id
                streak = self.store.record_external_pin_failure(
                    context,
                    model=model,
                    channel_id=channel_id,
                    event_at_unix_nano=event_ns,
                    failure_ttl_seconds=probe_failure_ttl_seconds,
                    failure_threshold=pin_channel_failure_threshold,
                    config_version=config_version,
                )
                if streak.triggered:
                    triggered_channels.add(channel_id)
                failure_streaks.append(
                    {
                        "account_id": context.enodot_account_id,
                        "a6_token_id": context.token_ref_id,
                        "channel_id": channel_id,
                        "model": model,
                        "event_at_unix_nano": event_ns,
                        **streak.public(),
                    }
                )
        negative_channels.update(triggered_channels)
        failure_details = {
            "failure_journal_consumed": bool(failure_events) and apply,
            "failure_event_account_ids": [account_id for account_id, _ in failure_events],
            "failure_event_matched_account_ids": list(failure_targets.matched_account_ids),
            "failure_event_unmatched_account_ids": list(failure_targets.unmatched_account_ids),
            "failure_event_channel_ids": sorted(
                {failure.channel_id for failure in resolved_failures}
            ),
            "failure_triggered_channel_ids": sorted(triggered_channels),
            "failure_streaks": failure_streaks,
        }
        all_quotes: list[ObserverQuote] = []
        scan_summaries: list[dict[str, Any]] = []
        scan_errors: list[str] = []
        snapshot_details: dict[str, Any] = {}
        observer_deadline_details: dict[str, Any] = {}

        def read_owned_channels(provider: ProviderPinContext) -> Sequence[Mapping[str, Any]]:
            client = self._client(provider.a6_user_id)
            return client.supplier_channels(provider.a6_user_id)

        def finish_observer_scan(
            observer: ObserverContext,
            submitted_offset_ms: int,
            submitted_at_epoch: float,
            read_scan: Callable[[], ObserverScanResult],
        ) -> tuple[dict[str, Any] | None, tuple[ObserverQuote, ...], str | None]:
            try:
                scan = read_scan()
                _pin_round_checkpoint(round_deadline)
                self.store.record_scan(
                    observer.merchant_id,
                    model,
                    scan.quotes,
                    page_count=scan.page_count,
                    owned_channel_ids=frozenset(owned),
                    pinned_channel_ids=pinned_channel_ids,
                    started_at_epoch=scan.started_at_epoch,
                    completed_at_epoch=scan.completed_at_epoch,
                    elapsed_ms=scan.elapsed_ms,
                )
                return (
                    {
                        "merchant_id": observer.merchant_id,
                        "window_offset_ms": submitted_offset_ms,
                        "pages": scan.page_count,
                        "quotes": len(scan.quotes),
                        "observed_at_epoch": scan.completed_at_epoch,
                        "elapsed_ms": scan.elapsed_ms,
                    },
                    scan.quotes,
                    None,
                )
            except Exception as exc:
                error_class = _scan_error_code(exc)
                completed_at_epoch = time.time()
                with suppress(Exception):
                    self.store.record_observer_scan_failure(
                        observer.merchant_id,
                        model,
                        started_at_epoch=submitted_at_epoch,
                        completed_at_epoch=completed_at_epoch,
                        error_class=error_class,
                    )
                return (
                    None,
                    (),
                    f"observer:{observer.merchant_id}:{error_class}",
                )

        def scan_observer_and_persist(
            observer: ObserverContext,
            submitted_offset_ms: int,
            submitted_at_epoch: float,
        ) -> tuple[dict[str, Any] | None, tuple[ObserverQuote, ...], str | None]:
            return finish_observer_scan(
                observer,
                submitted_offset_ms,
                submitted_at_epoch,
                lambda: self._scan_observer_observed(
                    observer,
                    model=model,
                    page_size=page_size,
                    round_deadline=round_deadline,
                ),
            )

        scan_executor = ThreadPoolExecutor(
            max_workers=_pin_scan_worker_count(
                provider_count=len(providers),
                observer_count=len(observers),
                observe_only=observe_only,
            )
        )
        try:
            owned_reads = (
                ()
                if observe_only
                else tuple(
                    (
                        provider,
                        scan_executor.submit(read_owned_channels, provider),
                    )
                    for provider in providers
                )
            )
            observer_reads = []
            if not use_observer_snapshot:
                window_started = time.monotonic()
                observer_step = observer_window_seconds / len(observers)
                for index, observer in enumerate(observers):
                    _pin_round_checkpoint(round_deadline)
                    scheduled_at = window_started + index * observer_step
                    delay = scheduled_at - time.monotonic()
                    if delay > 0:
                        if delay >= _pin_round_time_remaining(round_deadline):
                            raise ProviderPinRunnerError("provider_pin_round_deadline_exceeded")
                        time.sleep(delay)
                    _pin_round_checkpoint(round_deadline)
                    submitted_offset_ms = round((time.monotonic() - window_started) * 1000)
                    submitted_at_epoch = time.time()
                    if observe_only:
                        future = scan_executor.submit(
                            scan_observer_and_persist,
                            observer,
                            submitted_offset_ms,
                            submitted_at_epoch,
                        )
                    else:
                        future = scan_executor.submit(
                            self._scan_observer_observed,
                            observer,
                            model=model,
                            page_size=page_size,
                            round_deadline=round_deadline,
                        )
                    observer_reads.append(
                        (
                            observer,
                            submitted_offset_ms,
                            submitted_at_epoch,
                            future,
                        )
                    )
            if observe_only:
                pending = {item[3]: item for item in observer_reads}
                completed_observer_reads = []
                while pending:
                    remaining = _pin_round_time_remaining(round_deadline)
                    if remaining <= 0:
                        break
                    completed, _not_completed = wait(
                        pending,
                        timeout=remaining,
                        return_when=FIRST_COMPLETED,
                    )
                    if not completed:
                        break
                    for future in completed:
                        pending.pop(future, None)
                        completed_observer_reads.append(future.result())

                timed_out_observers = tuple(
                    item[0] for item in pending.values()
                )
                for future in pending:
                    future.cancel()
                observer_deadline_details = {
                    "observer_deadline_settled": bool(timed_out_observers),
                    "observer_timed_out_merchant_ids": [
                        observer.merchant_id for observer in timed_out_observers
                    ],
                }
                completed_reads = tuple(completed_observer_reads)
            else:
                completed_reads = ()
        finally:
            scan_executor.shutdown(wait=True, cancel_futures=True)

        if observe_only:
            for observer_id in observer_deadline_details.get(
                "observer_timed_out_merchant_ids", ()
            ):
                scan_errors.append(
                    f"observer:{observer_id}:provider_pin_round_deadline_exceeded"
                )
        else:
            _pin_round_checkpoint(round_deadline)
        for _provider, future in owned_reads:
            _pin_round_checkpoint(round_deadline)
            try:
                owned.update(
                    _positive_int(item.get("id"), "owned_channel_invalid")
                    for item in future.result()
                    if _optional_int(item.get("id"))
                )
            except Exception:
                # The persisted inventory remains the minimum exclusion set; the Pin executor
                # performs the final owned-channel readback check before re-enabling the account.
                continue

        def finish_observer_read(
            item: tuple[ObserverContext, int, float, Any],
        ) -> tuple[dict[str, Any] | None, tuple[ObserverQuote, ...], str | None]:
            observer, submitted_offset_ms, submitted_at_epoch, future = item
            return finish_observer_scan(
                observer,
                submitted_offset_ms,
                submitted_at_epoch,
                future.result,
            )

        if use_observer_snapshot:
            snapshot = self.store.latest_observer_snapshot(
                model,
                observer_merchant_ids=tuple(
                    observer.merchant_id for observer in observer_pool
                ),
                max_age_seconds=int(observer_snapshot_max_age_seconds or 0),
            )
            all_quotes.extend(snapshot.quotes)
            scan_summaries.extend(snapshot.scans)
            snapshot_observer_ids = {
                int(scan["merchant_id"]) for scan in snapshot.scans
            }
            observers = tuple(
                observer
                for observer in observer_pool
                if observer.merchant_id in snapshot_observer_ids
            )
            now_epoch = time.time()
            snapshot_details = {
                "observer_snapshot_reused": True,
                "observer_snapshot_max_age_seconds": observer_snapshot_max_age_seconds,
                "observer_snapshot_latest_age_seconds": (
                    max(0, now_epoch - snapshot.latest_completed_at_epoch)
                    if snapshot.latest_completed_at_epoch is not None
                    else None
                ),
                "observer_snapshot_oldest_age_seconds": (
                    max(0, now_epoch - snapshot.oldest_completed_at_epoch)
                    if snapshot.oldest_completed_at_epoch is not None
                    else None
                ),
            }
            completed_reads = ()
        elif observe_only:
            pass
        else:
            with ThreadPoolExecutor(max_workers=len(observer_reads)) as executor:
                completed_reads = tuple(executor.map(finish_observer_read, observer_reads))
        for summary, quotes, error in completed_reads:
            if not observe_only:
                _pin_round_checkpoint(round_deadline)
            if summary is not None:
                scan_summaries.append(summary)
                all_quotes.extend(quotes)
            if error is not None:
                scan_errors.append(error)
        quorum_met = len(scan_summaries) >= min_observers
        backoff_seconds = 0
        if not observe_only and not use_observer_snapshot:
            backoff_seconds = self.store.record_observer_round(
                model,
                quorum_met=quorum_met,
                error_class=scan_errors[0].rsplit(":", 1)[-1] if scan_errors else None,
            )
        if not quorum_met:
            return {
                "success": False,
                "mode": (
                    "observer_snapshot_unavailable"
                    if use_observer_snapshot
                    else "observer_quorum_failed"
                ),
                "model": model,
                "providers": len(providers),
                "observers": len(observers),
                "observer_pool_count": len(observer_pool),
                "scan_errors": scan_errors,
                "backoff_seconds": backoff_seconds,
                "scans": scan_summaries,
                "assignments": [],
                **failure_details,
                **snapshot_details,
                **observer_deadline_details,
            }
        if observe_only:
            return {
                "success": True,
                "mode": "observer_only",
                "model": model,
                "providers": len(providers),
                "observers": len(observers),
                "observer_pool_count": len(observer_pool),
                "quote_count": len(all_quotes),
                "residual_provider_promotions": list(residual_provider_promotions),
                "scan_errors": scan_errors,
                "scans": scan_summaries,
                "assignments": [],
                **observer_deadline_details,
            }
        _pin_round_checkpoint(round_deadline)
        failure_summaries = self.store.recent_failure_summaries(
            model,
            window_seconds=pin_failure_window_seconds,
        )
        all_quotes = [
            _with_failure_summary(quote, failure_summaries.get(quote.channel_id))
            for quote in all_quotes
        ]
        operating_channel_ids = _provider_operating_channel_ids(
            providers,
            minimum_initial_balance_ratio=initial_balance_ratio,
        )
        observed_current_cost = _observed_current_operating_cost(
            all_quotes,
            operating_channel_ids,
            min_observers=min_observers,
        )
        reference_loader = getattr(self.store, "provider_pin_economic_reference", None)
        persisted_reference = (
            reference_loader(model)
            if callable(reference_loader)
            else ProviderPinEconomicReference(None, None, "store_reference_unavailable")
        )
        economic_reference = ProviderPinEconomicReference(
            persisted_reference.protected_billing_floor_multiplier,
            persisted_reference.current_operating_cost_multiplier or observed_current_cost,
            (
                persisted_reference.source
                if persisted_reference.current_operating_cost_multiplier is not None
                else "observer_confirmed_pins"
                if observed_current_cost is not None
                else persisted_reference.source
            ),
        )
        planning_providers = _provider_planning_contexts(
            providers,
            minimum_initial_balance_ratio=initial_balance_ratio,
        )
        planning_merchant_ids = {
            context.merchant_id for context in planning_providers
        }
        skipped_providers = tuple(
            context
            for context in providers
            if context.merchant_id not in planning_merchant_ids
        )
        targets = tuple(
            ProviderPinTarget(
                merchant_id=item.merchant_id,
                a6_user_id=item.a6_user_id,
                token_id=item.token_id,
                token_ref_id=item.token_ref_id,
                enodot_account_id=item.enodot_account_id,
            )
            for item in planning_providers
        )
        emergency_channel_ids = _emergency_pinned_channel_ids(
            all_quotes,
            {
                channel_id
                for provider in providers
                if (channel_id := provider.confirmed_pin_channel_id) is not None
            },
        )
        try:
            _pin_round_checkpoint(round_deadline)
            plan: PinPlan = plan_provider_pins(
                targets,
                [
                    quote
                    for quote in all_quotes
                    if quote.channel_id not in negative_channels
                    and quote.channel_id not in emergency_channel_ids
                ],
                model=model,
                owned_channel_ids=owned,
                min_observers=min_observers,
                top_k=top_k,
                price_weight=pin_price_weight,
                success_weight=pin_success_weight,
                latency_weight=pin_latency_weight,
                price_span_ratio=pin_price_span_ratio,
                minimum_success_rate=pin_minimum_success_rate,
                maximum_latency_ms=pin_maximum_latency_ms,
                maximum_multiplier=pin_maximum_multiplier,
                protected_billing_floor_multiplier=(
                    economic_reference.protected_billing_floor_multiplier
                ),
                current_operating_cost_multiplier=(
                    economic_reference.current_operating_cost_multiplier
                ),
                maximum_relative_cost_ratio=pin_maximum_relative_cost_ratio,
            )
        except PinControlError as exc:
            return {
                "success": False,
                "model": model,
                "providers": len(providers),
                "observers": len(observers),
                "observer_pool_count": len(observer_pool),
                "scan_errors": [exc.code],
                "emergency_channel_ids": sorted(emergency_channel_ids),
                "residual_provider_promotions": list(residual_provider_promotions),
                "pin_economic_reference": {
                    "source": economic_reference.source,
                    "protected_billing_floor_multiplier": (
                        str(economic_reference.protected_billing_floor_multiplier)
                        if economic_reference.protected_billing_floor_multiplier is not None
                        else None
                    ),
                    "current_operating_cost_multiplier": (
                        str(economic_reference.current_operating_cost_multiplier)
                        if economic_reference.current_operating_cost_multiplier is not None
                        else None
                    ),
                    "absolute_cost_ceiling_multiplier": str(pin_maximum_multiplier),
                    "maximum_relative_cost_ratio": str(pin_maximum_relative_cost_ratio),
                },
                "scans": scan_summaries,
                "assignments": [],
                **failure_details,
                **snapshot_details,
            }
        by_merchant = {item.merchant_id: item for item in providers}
        plan = _stabilize_provider_plan_assignments(plan, by_merchant)
        results: list[dict[str, Any]] = [
            {
                "merchant_id": context.merchant_id,
                "action": "skipped",
                "applied": False,
                "temporary_provider": context.temporary_provider,
                "current_channel_id": context.confirmed_pin_channel_id,
                "skip_reason": (
                    "residual_provider_below_reserve"
                    if context.temporary_provider
                    else "provider_not_plannable"
                ),
            }
            for context in skipped_providers
        ]
        probe_budget_remaining = probe_budget
        round_admin = _DeadlineBoundEnodotAdmin(self.enodot_admin, round_deadline)
        pool_guard = (
            _SchedulablePoolGuard(
                round_admin,
                tuple(context.enodot_account_id for context in providers),
                minimum_capacity=minimum_schedulable_capacity,
            )
            if apply
            else None
        )
        assignment_by_merchant = {
            assignment.target.merchant_id: assignment for assignment in plan.assignments
        }

        def low_balance_result(
            context: ProviderPinContext,
            *,
            current_channel_id: int | None,
            selected_channel_id: int,
            user_balance_quota: int,
            withdrawable_quota: int,
        ) -> dict[str, Any]:
            pool_fields: dict[str, Any] = {}
            error_class = (
                "residual_provider_reserve_reached"
                if context.temporary_provider
                else "provider_user_balance_insufficient_unfunded"
            )
            if apply:
                _pin_round_checkpoint(round_deadline)
                if context.temporary_provider:
                    before = round_admin.get_account(context.enodot_account_id)
                    if pool_guard is not None:
                        pool_guard.observe(context.enodot_account_id, before)
                    _pin_round_checkpoint(round_deadline)
                    observed = round_admin.set_schedulable(
                        context.enodot_account_id,
                        False,
                    )
                    if pool_guard is not None:
                        pool_guard.observe(context.enodot_account_id, observed)
                    if observed.get("schedulable") is not False:
                        _pin_round_checkpoint(round_deadline)
                        observed = round_admin.get_account(context.enodot_account_id)
                        if pool_guard is not None:
                            pool_guard.observe(context.enodot_account_id, observed)
                    if observed.get("schedulable") is not False:
                        raise ProviderPinRunnerError("residual_provider_pause_readback_failed")
                    pool_fields = {
                        "schedulable_changed": before.get("schedulable") is not False,
                        "retained_for_pool_capacity": False,
                    }
                    drain = getattr(self.store, "drain_residual_provider", None)
                    if callable(drain):
                        pool_fields["returned_to_observer"] = bool(
                            drain(
                                context,
                                model=model,
                                balance_quota=user_balance_quota,
                                error_class=error_class,
                            )
                        )
                else:
                    if pool_guard is None:
                        raise ProviderPinRunnerError("provider_pool_guard_missing")
                    pool_fields = pool_guard.disable(context.enodot_account_id).public()
                self.store.mark_account_error(
                    context.token_ref_id,
                    error_class,
                )
            return {
                "merchant_id": context.merchant_id,
                "action": "blocked_low_balance",
                "provider_state": (
                    "residual_provider_drained"
                    if context.temporary_provider
                    else "awaiting_topup_pool_guard"
                    if pool_fields.get("retained_for_pool_capacity") is True
                    else "draining_awaiting_topup"
                ),
                "temporary_provider": context.temporary_provider,
                "current_channel_id": current_channel_id,
                "selected_channel_id": selected_channel_id,
                "applied": bool(pool_fields.get("schedulable_changed")),
                "error_class": error_class,
                "user_balance_quota": user_balance_quota,
                "withdrawable_quota": withdrawable_quota,
                **pool_fields,
            }

        for context in _provider_reconciliation_order(
            planning_providers,
            minimum_initial_balance_ratio=initial_balance_ratio,
        ):
            _pin_round_checkpoint(round_deadline)
            assignment = assignment_by_merchant.get(context.merchant_id)
            if assignment is None:
                results.append(
                    {
                        "merchant_id": context.merchant_id,
                        "action": "error",
                        "applied": False,
                        "error_class": "provider_assignment_missing",
                    }
                )
                continue
            if not apply:
                current_channel_id = context.confirmed_pin_channel_id
                selected_channel_id = assignment.candidate.channel_id
                confirmed = (
                    current_channel_id is not None
                    and current_channel_id == selected_channel_id
                )
                results.append(
                    {
                        "merchant_id": context.merchant_id,
                        "action": "noop" if confirmed else "pin",
                        "current_channel_id": current_channel_id,
                        "selected_channel_id": selected_channel_id,
                        "proposed_channel_id": selected_channel_id,
                        "applied": False,
                        "confirmed": confirmed,
                        "balance_transferred": False,
                        "plan_only": True,
                        "source": "persisted_provider_context",
                    }
                )
                continue
            try:
                if not _provider_balance_ready(
                    context,
                    minimum_initial_balance_ratio=initial_balance_ratio,
                ):
                    results.append(
                        low_balance_result(
                            context,
                            current_channel_id=None,
                            selected_channel_id=assignment.candidate.channel_id,
                            user_balance_quota=context.user_balance_quota,
                            withdrawable_quota=context.withdrawable_quota,
                        )
                    )
                    continue
                client = self._client(context.a6_user_id)
                _pin_round_checkpoint(round_deadline)
                current = self._live_pin_channel(
                    client, context.a6_user_id, context.token_id, model
                )
                _pin_round_checkpoint(round_deadline)
                live_user = client.self_user(context.a6_user_id) or {}
                live_balance = max(0, int(live_user.get("quota") or 0))
                live_withdrawable = context.withdrawable_quota
                transferred = False
                if live_balance < context.quota_per_unit:
                    _pin_round_checkpoint(round_deadline)
                    live_withdraw = client.supplier_withdraw_summary(context.a6_user_id)
                    live_withdrawable = max(
                        0,
                        int(live_withdraw.get("balance_available_micros") or 0),
                    )
                if (
                    apply
                    and not context.temporary_provider
                    and live_balance < context.quota_per_unit <= live_withdrawable
                ):
                    _pin_round_checkpoint(round_deadline)
                    preview = client.balance_transfer_preview(
                        context.a6_user_id,
                        context.quota_per_unit,
                        quota_per_unit=context.quota_per_unit,
                    )
                    if preview.get("enabled") is not True:
                        raise ProviderPinRunnerError("provider_balance_transfer_disabled")
                    _pin_round_checkpoint(round_deadline)
                    client.balance_transfer(
                        context.a6_user_id,
                        context.quota_per_unit,
                        quota_per_unit=context.quota_per_unit,
                    )
                    _pin_round_checkpoint(round_deadline)
                    confirmed_user = client.self_user(context.a6_user_id) or {}
                    _pin_round_checkpoint(round_deadline)
                    confirmed_withdraw = client.supplier_withdraw_summary(context.a6_user_id)
                    confirmed_balance = max(0, int(confirmed_user.get("quota") or 0))
                    confirmed_available = max(
                        0,
                        int(confirmed_withdraw.get("balance_available_micros") or 0),
                    )
                    if not _balance_transfer_confirmed(
                        user_balance_before=live_balance,
                        withdrawable_before=live_withdrawable,
                        user_balance_after=confirmed_balance,
                        withdrawable_after=confirmed_available,
                        amount=context.quota_per_unit,
                    ):
                        raise ProviderPinRunnerError("provider_balance_transfer_unconfirmed")
                    live_balance = confirmed_balance
                    live_withdrawable = confirmed_available
                    transferred = True
                if not _provider_balance_ready(
                    context,
                    user_balance_quota=live_balance,
                    withdrawable_quota=live_withdrawable,
                    has_confirmed_pin=(
                        current is not None or context.confirmed_pin_channel_id is not None
                    ),
                    minimum_initial_balance_ratio=initial_balance_ratio,
                ):
                    results.append(
                        low_balance_result(
                            context,
                            current_channel_id=current,
                            selected_channel_id=assignment.candidate.channel_id,
                            user_balance_quota=live_balance,
                            withdrawable_quota=live_withdrawable,
                        )
                    )
                    continue
                result, probe_budget_remaining = self._reconcile_provider_pin(
                    context,
                    client,
                    assignment,
                    candidates=plan.candidates,
                    ranked_candidates=plan.ranked_candidates,
                    owned_channel_ids=owned,
                    negative_channel_ids=negative_channels,
                    model=model,
                    config_version=config_version,
                    apply=apply,
                    probe_budget_remaining=probe_budget_remaining,
                    probe_failure_ttl_seconds=probe_failure_ttl_seconds,
                    probe_health_ttl_seconds=probe_health_ttl_seconds,
                    probe_max_output_tokens=probe_max_output_tokens,
                    probe_timeout_seconds=probe_timeout_seconds,
                    probe_round_deadline=min(
                        time.monotonic() + probe_round_time_budget_seconds,
                        round_deadline,
                    ),
                    balance_transferred=transferred,
                    pin_probe_mode=pin_probe_mode,
                    reserved_channel_ids=_reserved_candidate_channel_ids(
                        plan,
                        by_merchant,
                        assignment.target.merchant_id,
                        minimum_initial_balance_ratio=initial_balance_ratio,
                    ),
                    emergency_channel_ids=emergency_channel_ids,
                    force_predrain_channel_ids=frozenset(triggered_channels),
                    pin_minimum_hold_seconds=pin_minimum_hold_seconds,
                    pin_minimum_switch_gain=pin_minimum_switch_gain,
                    emergency_boost_minimum_requests=(emergency_boost_minimum_requests),
                    emergency_boost_maximum_requests=(emergency_boost_maximum_requests),
                    emergency_boost_timeout_seconds=emergency_boost_timeout_seconds,
                    emergency_boost_cooldown_seconds=emergency_boost_cooldown_seconds,
                    pool_guard=pool_guard,
                    round_deadline=round_deadline,
                    enodot_admin=round_admin,
                )
                balance_insufficient = any(
                    str(attempt.get("error_class") or "")
                    in {"account_quota_insufficient", "insufficient_quota"}
                    for attempt in result.get("probe_attempts", ())
                    if isinstance(attempt, Mapping)
                )
                if (
                    apply
                    and balance_insufficient
                    and live_withdrawable < context.quota_per_unit
                ):
                    self.store.mark_account_error(
                        context.token_ref_id,
                        "provider_user_balance_insufficient_unfunded",
                    )
                    result["liquidity_hard_line"] = True
                    result["withdrawable_below_transfer_minimum"] = True
                if apply and (
                    result.get("probe_confirmed") is True
                    or result.get("probe_health_fresh") is True
                    or result.get("observer_confirmed") is True
                ):
                    self.store.clear_account_error(context.token_ref_id)
                results.append(result)
            except Exception as exc:
                error_class = getattr(exc, "code", None)
                if not error_class and isinstance(exc, A6AutoError):
                    error_class = str(exc)
                results.append(
                    {
                        "merchant_id": context.merchant_id,
                        "action": "error",
                        "selected_channel_id": assignment.candidate.channel_id,
                        "applied": False,
                        "error_class": error_class or "pin_runtime_error",
                    }
                )
        return {
            "success": not any(item.get("action") == "error" for item in results),
            "model": model,
            "providers": len(providers),
            "observers": len(observers),
            "observer_pool_count": len(observer_pool),
            "owned_channel_count": len(owned),
            "negative_probe_channel_ids": sorted(negative_channels),
            "pin_probe_mode": pin_probe_mode,
            **failure_details,
            "emergency_channel_ids": sorted(emergency_channel_ids),
            "residual_provider_promotions": list(residual_provider_promotions),
            "probe_budget_used": probe_budget - probe_budget_remaining,
            "probe_budget_remaining": probe_budget_remaining,
            **snapshot_details,
            "candidate_count": len(plan.candidates),
            "eligible_candidate_count": len(plan.ranked_candidates),
            "candidates": [
                {
                    "channel_id": item.channel_id,
                    "listing_id": item.listing_id,
                    "observer_count": item.observer_count,
                    "input_price_micros": item.median_input_price_micros,
                    "output_price_micros": item.median_output_price_micros,
                    "median_multiplier": (
                        str(item.median_multiplier) if item.median_multiplier is not None else None
                    ),
                    "latest_success_at": item.latest_success_at,
                    "median_success_rate": item.median_success_rate,
                    "median_latency_ms": (
                        str(item.median_latency_ms) if item.median_latency_ms is not None else None
                    ),
                    "median_ttft_ms": (
                        str(item.median_ttft_ms) if item.median_ttft_ms is not None else None
                    ),
                    "effective_latency_ms": (
                        str(item.effective_latency_ms)
                        if item.effective_latency_ms is not None
                        else None
                    ),
                    "health_state": item.health_state,
                    "failure_responsibility": item.failure_responsibility,
                    "recent_failure_count": item.recent_failure_count,
                    "recent_client_failure_count": item.recent_client_failure_count,
                    "recent_provider_failure_count": item.recent_provider_failure_count,
                    "recent_platform_failure_count": item.recent_platform_failure_count,
                    "recent_unknown_failure_count": item.recent_unknown_failure_count,
                    "recent_failure_codes": list(item.recent_failure_codes),
                    "smart_routing_labels": list(item.smart_routing_labels),
                    "price_score": str(item.price_score),
                    "success_score": str(item.success_score),
                    "latency_score": str(item.latency_score),
                    "composite_score": str(item.composite_score),
                }
                for item in plan.candidates
            ],
            "pin_score_weights": {
                "price": str(pin_price_weight),
                "success": str(pin_success_weight),
                "latency": str(pin_latency_weight),
                "price_span_ratio": str(pin_price_span_ratio),
                "minimum_success_rate": pin_minimum_success_rate,
                "maximum_latency_ms": str(pin_maximum_latency_ms),
                "maximum_multiplier": str(pin_maximum_multiplier),
                "maximum_relative_cost_ratio": str(pin_maximum_relative_cost_ratio),
                "protected_billing_floor_multiplier": (
                    str(plan.protected_billing_floor_multiplier)
                    if plan.protected_billing_floor_multiplier is not None
                    else None
                ),
                "current_operating_cost_multiplier": (
                    str(plan.current_operating_cost_multiplier)
                    if plan.current_operating_cost_multiplier is not None
                    else None
                ),
                "relative_cost_ceiling_multiplier": (
                    str(plan.relative_cost_ceiling_multiplier)
                    if plan.relative_cost_ceiling_multiplier is not None
                    else None
                ),
                "effective_cost_ceiling_multiplier": (
                    str(plan.effective_cost_ceiling_multiplier)
                    if plan.effective_cost_ceiling_multiplier is not None
                    else None
                ),
                "economic_reference_source": economic_reference.source,
                "failure_window_seconds": pin_failure_window_seconds,
                "provider_initial_balance_ratio": str(initial_balance_ratio),
                "minimum_schedulable_capacity": minimum_schedulable_capacity,
                "minimum_hold_seconds": pin_minimum_hold_seconds,
                "minimum_switch_gain": str(pin_minimum_switch_gain),
            },
            "schedulable_pool_capacity": (
                pool_guard.capacity() if pool_guard is not None else None
            ),
            "scan_errors": scan_errors,
            "scans": scan_summaries,
            "assignments": results,
        }


def _balance_transfer_confirmed(
    *,
    user_balance_before: int,
    withdrawable_before: int,
    user_balance_after: int,
    withdrawable_after: int,
    amount: int,
) -> bool:
    values = (
        user_balance_before,
        withdrawable_before,
        user_balance_after,
        withdrawable_after,
        amount,
    )
    if any(isinstance(value, bool) or not isinstance(value, int) for value in values):
        return False
    if min(values) < 0 or amount < 1:
        return False
    balance_credit_confirmed = user_balance_after >= user_balance_before + amount
    withdraw_debit_confirmed = (
        withdrawable_before >= amount and withdrawable_after <= withdrawable_before - amount
    )
    return balance_credit_confirmed or withdraw_debit_confirmed


def _select_observer_round(
    observers: Sequence[ObserverContext],
    *,
    round_size: int,
    round_index: int,
) -> tuple[ObserverContext, ...]:
    ordered = tuple(sorted(observers, key=lambda item: item.merchant_id))
    if not ordered:
        return ()
    size = min(len(ordered), round_size)
    start = (max(0, round_index) * size) % len(ordered)
    return tuple(ordered[(start + offset) % len(ordered)] for offset in range(size))


def _provider_balance_ready(
    context: ProviderPinContext,
    *,
    user_balance_quota: int | None = None,
    withdrawable_quota: int | None = None,
    has_confirmed_pin: bool | None = None,
    minimum_initial_balance_ratio: Decimal | float | str = Decimal("0"),
) -> bool:
    balance = max(
        0,
        int(context.user_balance_quota if user_balance_quota is None else user_balance_quota),
    )
    if bool(getattr(context, "temporary_provider", False)):
        reserve = getattr(context, "minimum_balance_quota", None)
        if reserve is None or isinstance(reserve, bool) or reserve < 0:
            return False
        # The reserve itself is not spendable.  Withdrawable value does not extend this
        # duty because retirement balances below one whole unit cannot be transferred.
        return balance > reserve
    try:
        configured_ratio = Decimal(str(minimum_initial_balance_ratio))
    except (InvalidOperation, ValueError):
        return False
    if not configured_ratio.is_finite() or not Decimal(0) <= configured_ratio <= Decimal(1):
        return False
    # ``quota_per_unit`` is the transfer increment, not a request gate. Normal Providers
    # always reach a real probe; only an observed insufficient-balance response combined
    # with withdrawable below one transfer unit creates a liquidity hard block.
    return configured_ratio.is_finite() and Decimal(0) <= configured_ratio <= Decimal(1)


def _provider_planning_contexts(
    contexts: Sequence[ProviderPinContext],
    *,
    minimum_initial_balance_ratio: Decimal | float | str = Decimal("0"),
) -> tuple[ProviderPinContext, ...]:
    ready = tuple(
        context
        for context in contexts
        if _provider_balance_ready(
            context,
            minimum_initial_balance_ratio=minimum_initial_balance_ratio,
        )
    )
    return ready


def _provider_operating_channel_ids(
    contexts: Sequence[ProviderPinContext],
    *,
    minimum_initial_balance_ratio: Decimal | float | str = Decimal("0"),
) -> frozenset[int]:
    return frozenset(
        context.confirmed_pin_channel_id
        for context in contexts
        if context.confirmed_pin_channel_id is not None
        and _provider_balance_ready(
            context,
            minimum_initial_balance_ratio=minimum_initial_balance_ratio,
        )
    )


def _provider_reconciliation_order(
    contexts: Sequence[ProviderPinContext],
    *,
    minimum_initial_balance_ratio: Decimal | float | str = Decimal("0"),
) -> tuple[ProviderPinContext, ...]:
    """Bring funded Providers online before draining accounts that need a top-up."""

    return tuple(
        sorted(
            contexts,
            key=lambda context: (
                not _provider_balance_ready(
                    context,
                    minimum_initial_balance_ratio=minimum_initial_balance_ratio,
                ),
                context.merchant_id,
            ),
        )
    )


def _stabilize_provider_plan_assignments(
    plan: PinPlan,
    contexts: Mapping[int, ProviderPinContext],
) -> PinPlan:
    """Keep current owners when the selected channel set has not changed."""

    desired_candidates = tuple(item.candidate for item in plan.assignments)
    candidates_by_channel = {item.channel_id: item for item in desired_candidates}
    if len(candidates_by_channel) != len(desired_candidates):
        return plan

    stabilized: dict[int, PinAssignment] = {}
    used_channel_ids: set[int] = set()
    for assignment in plan.assignments:
        merchant_id = assignment.target.merchant_id
        context = contexts.get(merchant_id)
        current_channel_id = context.confirmed_pin_channel_id if context is not None else None
        candidate = candidates_by_channel.get(current_channel_id)
        if candidate is None or candidate.channel_id in used_channel_ids:
            continue
        stabilized[merchant_id] = replace(assignment, candidate=candidate)
        used_channel_ids.add(candidate.channel_id)

    remaining_candidates = iter(
        item for item in desired_candidates if item.channel_id not in used_channel_ids
    )
    for assignment in plan.assignments:
        merchant_id = assignment.target.merchant_id
        if merchant_id in stabilized:
            continue
        stabilized[merchant_id] = replace(
            assignment,
            candidate=next(remaining_candidates),
        )

    return replace(
        plan,
        assignments=tuple(stabilized[item.target.merchant_id] for item in plan.assignments),
    )


def _reserved_candidate_channel_ids(
    plan: PinPlan,
    contexts: Mapping[int, ProviderPinContext],
    current_merchant_id: int,
    *,
    minimum_initial_balance_ratio: Decimal | float | str = Decimal("0"),
) -> frozenset[int]:
    current_candidate_channel_id = next(
        (
            item.candidate.channel_id
            for item in plan.assignments
            if item.target.merchant_id == current_merchant_id
        ),
        None,
    )
    return frozenset(
        item.candidate.channel_id
        for item in plan.assignments
        if (
            item.target.merchant_id != current_merchant_id
            and item.candidate.channel_id != current_candidate_channel_id
            and _provider_balance_ready(
                contexts[item.target.merchant_id],
                minimum_initial_balance_ratio=minimum_initial_balance_ratio,
            )
        )
    )
