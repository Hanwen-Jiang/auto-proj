from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

from .errors import A6AutoError


class PinControlError(A6AutoError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


_PIN_ELIGIBLE_HEALTH_STATES = frozenset({"healthy", "idle"})


@dataclass(frozen=True)
class ObserverQuote:
    observer_merchant_id: int
    channel_id: int
    listing_id: int | None
    model: str
    final_multiplier: Decimal | None
    input_price_micros: int
    output_price_micros: int
    available: bool = True
    supplier_id: int | None = None
    route_status: str = ""
    last_success_at: int | None = None
    circuit_state: str = ""
    circuit_probe_status: str = ""
    circuit_last_error_kind: str = ""
    circuit_probe_success_required: int | None = None
    circuit_probe_success_streak: int | None = None
    circuit_scope: str = ""
    circuit_recovery_mode: str = ""
    circuit_cooling_until: int | None = None
    circuit_last_cooling_at: int | None = None
    circuit_last_probe_at: int | None = None
    circuit_next_probe_at: int | None = None
    cooldown_state: str = ""
    cooldown_label: str = ""
    cooldown_remaining_seconds: int | None = None
    cooldown_expected_recovery_at: int | None = None
    cooldown_next_probe_at: int | None = None
    cooldown_sources: Sequence[Any] | None = None
    capability_cooldowns: Mapping[str, Any] | Sequence[Any] | None = None
    smart_routing_status: str = ""
    smart_routing_labels: tuple[str, ...] = ()
    listing_availability: Mapping[str, Any] | Sequence[Any] | str | None = None
    last_failure_at: int | None = None
    last_failure_code: str = ""
    last_failure_responsibility: str = "unknown"
    recent_failure_count: int = 0
    recent_client_failure_count: int = 0
    recent_provider_failure_count: int = 0
    recent_platform_failure_count: int = 0
    recent_unknown_failure_count: int = 0
    recent_failure_codes: tuple[str, ...] = ()
    last_sample_at: int | None = None
    sample_count: int | None = None
    recent_success_rate: int | None = None
    success_buckets: Mapping[str, Any] | Sequence[Any] | None = None
    recent_p50_ms: Decimal | None = None
    p50_ttft_ms: Decimal | None = None
    health_state: str = "unknown"
    realtime_ratio_usd: Decimal | None = None
    realtime_ratio_cny: Decimal | None = None
    exchange_rate: Decimal | None = None


@dataclass(frozen=True)
class ProviderPinTarget:
    merchant_id: int
    a6_user_id: int
    token_id: int
    token_ref_id: int
    enodot_account_id: int


@dataclass(frozen=True)
class PinCandidate:
    channel_id: int
    listing_id: int | None
    model: str
    observer_count: int
    median_multiplier: Decimal | None
    median_input_price_micros: int
    median_output_price_micros: int
    latest_success_at: int | None = None
    median_success_rate: int | None = None
    median_latency_ms: Decimal | None = None
    median_ttft_ms: Decimal | None = None
    health_state: str = "unknown"
    failure_responsibility: str = "unknown"
    recent_failure_count: int = 0
    recent_client_failure_count: int = 0
    recent_provider_failure_count: int = 0
    recent_platform_failure_count: int = 0
    recent_unknown_failure_count: int = 0
    recent_failure_codes: tuple[str, ...] = ()
    smart_routing_labels: tuple[str, ...] = ()
    price_score: Decimal = Decimal(0)
    success_score: Decimal = Decimal(0)
    latency_score: Decimal = Decimal(0)
    composite_score: Decimal = Decimal(0)

    @property
    def effective_latency_ms(self) -> Decimal | None:
        return _candidate_latency(self)


@dataclass(frozen=True)
class PinAssignment:
    target: ProviderPinTarget
    candidate: PinCandidate
    expected_switch_gain: Decimal | None = None
    hysteresis_threshold: Decimal = Decimal(0)
    switch_reason: str = ""


@dataclass(frozen=True)
class PinPlan:
    model: str
    assignments: tuple[PinAssignment, ...]
    candidates: tuple[PinCandidate, ...]
    ranked_candidates: tuple[PinCandidate, ...]
    owned_channel_ids: frozenset[int]
    absolute_cost_ceiling_multiplier: Decimal | None = None
    protected_billing_floor_multiplier: Decimal | None = None
    current_operating_cost_multiplier: Decimal | None = None
    relative_cost_ceiling_multiplier: Decimal | None = None
    effective_cost_ceiling_multiplier: Decimal | None = None


@dataclass(frozen=True)
class PinExecutionResult:
    action: str
    applied: bool
    confirmed: bool
    merchant_id: int
    token_id: int
    channel_id: int
    enodot_account_id: int


@dataclass(frozen=True)
class PinSwitchDecision:
    candidate: PinCandidate
    action: str
    expected_switch_gain: Decimal | None
    hold_remaining_seconds: int = 0


class PinA6Client(Protocol):
    def marketplace_pins(self, user_id: int) -> list[dict[str, Any]]: ...

    def marketplace_pin(
        self,
        user_id: int,
        *,
        token_id: int,
        channel_id: int,
        model: str,
        fallback_to_smart_routing: bool = False,
    ) -> dict[str, Any]: ...


class PinEnodotAdmin(Protocol):
    def set_schedulable(self, account_id: int, schedulable: bool) -> Mapping[str, Any]: ...


class PinAuditStore(Protocol):
    def prepare(self, assignment: PinAssignment, *, config_version: str) -> str: ...

    def complete(
        self,
        action_ref: str,
        assignment: PinAssignment,
        observed_pin: Mapping[str, Any],
    ) -> None: ...

    def fail(self, action_ref: str, *, error_class: str) -> None: ...


def plan_provider_pins(
    targets: Sequence[ProviderPinTarget],
    quotes: Sequence[ObserverQuote],
    *,
    model: str,
    owned_channel_ids: Sequence[int] | set[int] | frozenset[int],
    min_observers: int = 2,
    top_k: int = 5,
    price_weight: Decimal | float | str = Decimal("1"),
    success_weight: Decimal | float | str = Decimal("0"),
    latency_weight: Decimal | float | str = Decimal("0"),
    price_span_ratio: Decimal | float | str = Decimal("0.25"),
    minimum_success_rate: int = 0,
    maximum_latency_ms: Decimal | float | str | None = None,
    maximum_multiplier: Decimal | float | str | None = Decimal("0.05"),
    protected_billing_floor_multiplier: Decimal | float | str | None = None,
    current_operating_cost_multiplier: Decimal | float | str | None = None,
    maximum_relative_cost_ratio: Decimal | float | str = Decimal("2"),
) -> PinPlan:
    model = str(model).strip()
    owned = frozenset(
        _positive_int(item, "pin_owned_channel_invalid") for item in owned_channel_ids
    )
    if not model:
        raise PinControlError("pin_model_invalid")
    if min_observers < 2:
        raise PinControlError("pin_min_observers_invalid")
    if top_k < 1:
        raise PinControlError("pin_top_k_invalid")
    if not targets:
        raise PinControlError("pin_targets_empty")
    weights = _pin_weights(price_weight, success_weight, latency_weight)
    price_span = _positive_decimal(price_span_ratio, "pin_price_span_ratio_invalid")
    minimum_success = _bounded_int(
        minimum_success_rate,
        minimum=0,
        maximum=10000,
        error="pin_minimum_success_rate_invalid",
    )
    maximum_latency = (
        _positive_decimal(maximum_latency_ms, "pin_maximum_latency_invalid")
        if maximum_latency_ms is not None
        else None
    )
    maximum_price_multiplier = (
        _positive_decimal(maximum_multiplier, "pin_maximum_multiplier_invalid")
        if maximum_multiplier is not None
        else None
    )
    protected_billing_floor = (
        _positive_decimal(
            protected_billing_floor_multiplier,
            "pin_protected_billing_floor_invalid",
        )
        if protected_billing_floor_multiplier is not None
        else None
    )
    current_operating_cost = (
        _positive_decimal(
            current_operating_cost_multiplier,
            "pin_current_operating_cost_invalid",
        )
        if current_operating_cost_multiplier is not None
        else None
    )
    relative_cost_ratio = _positive_decimal(
        maximum_relative_cost_ratio,
        "pin_maximum_relative_cost_ratio_invalid",
    )
    if relative_cost_ratio < 1:
        raise PinControlError("pin_maximum_relative_cost_ratio_invalid")
    relative_cost_ceiling = (
        current_operating_cost * relative_cost_ratio
        if current_operating_cost is not None
        else None
    )
    effective_cost_ceiling = _minimum_optional_decimal(
        maximum_price_multiplier,
        protected_billing_floor,
        relative_cost_ceiling,
    )

    normalized_targets = tuple(_validate_target(target) for target in targets)
    if len({target.token_id for target in normalized_targets}) != len(normalized_targets):
        raise PinControlError("pin_target_token_duplicate")

    grouped: dict[int, dict[int, ObserverQuote]] = defaultdict(dict)
    for quote in quotes:
        normalized = _validate_quote(quote)
        if (
            normalized.model != model
            or not normalized.available
            or normalized.channel_id in owned
            or normalized.health_state.strip().lower() not in _PIN_ELIGIBLE_HEALTH_STATES
        ):
            continue
        grouped[normalized.channel_id][normalized.observer_merchant_id] = normalized

    candidates = []
    for channel_id, by_observer in grouped.items():
        if len(by_observer) < min_observers:
            continue
        values = tuple(by_observer.values())
        listing_ids = {quote.listing_id for quote in values if quote.listing_id is not None}
        if len(listing_ids) > 1:
            continue
        candidates.append(
            PinCandidate(
                channel_id=channel_id,
                listing_id=next(iter(listing_ids), None),
                model=model,
                observer_count=len(values),
                median_multiplier=_median_decimal(
                    [
                        quote.final_multiplier
                        for quote in values
                        if quote.final_multiplier is not None
                    ]
                ),
                median_input_price_micros=_median_int(
                    [quote.input_price_micros for quote in values]
                ),
                median_output_price_micros=_median_int(
                    [quote.output_price_micros for quote in values]
                ),
                latest_success_at=max(
                    (
                        quote.last_success_at
                        for quote in values
                        if quote.last_success_at is not None
                    ),
                    default=None,
                ),
                median_success_rate=_median_optional_int(
                    [
                        quote.recent_success_rate
                        for quote in values
                        if quote.recent_success_rate is not None
                    ]
                ),
                median_latency_ms=_median_decimal(
                    [quote.recent_p50_ms for quote in values if quote.recent_p50_ms is not None]
                ),
                median_ttft_ms=_median_decimal(
                    [quote.p50_ttft_ms for quote in values if quote.p50_ttft_ms is not None]
                ),
                health_state=_worst_health_state(quote.health_state for quote in values),
                failure_responsibility=_worst_failure_responsibility(
                    quote.last_failure_responsibility for quote in values
                ),
                recent_failure_count=max(
                    (quote.recent_failure_count for quote in values), default=0
                ),
                recent_client_failure_count=max(
                    (quote.recent_client_failure_count for quote in values), default=0
                ),
                recent_provider_failure_count=max(
                    (quote.recent_provider_failure_count for quote in values), default=0
                ),
                recent_platform_failure_count=max(
                    (quote.recent_platform_failure_count for quote in values), default=0
                ),
                recent_unknown_failure_count=max(
                    (quote.recent_unknown_failure_count for quote in values), default=0
                ),
                recent_failure_codes=tuple(
                    dict.fromkeys(
                        code for quote in values for code in quote.recent_failure_codes if code
                    )
                )[:8],
                smart_routing_labels=tuple(
                    sorted(
                        {label for quote in values for label in quote.smart_routing_labels if label}
                    )
                ),
            )
        )

    if not candidates:
        raise PinControlError("pin_consensus_candidates_empty")
    candidates = [
        candidate
        for candidate in candidates
        if _candidate_passes_hard_gates(
            candidate,
            minimum_success_rate=minimum_success,
            maximum_latency_ms=maximum_latency,
            maximum_multiplier=effective_cost_ceiling,
        )
    ]
    candidates = list(
        _score_pin_candidates(
            candidates,
            price_weight=weights[0],
            success_weight=weights[1],
            latency_weight=weights[2],
            price_span_ratio=price_span,
        )
    )
    candidates.sort(key=_candidate_sort_key)
    ranked = tuple(candidates)
    selected = tuple(candidates[:top_k])
    if not selected:
        raise PinControlError("pin_economic_or_health_gate_candidates_empty")

    assignments = tuple(
        PinAssignment(target=target, candidate=selected[index % len(selected)])
        for index, target in enumerate(normalized_targets)
    )
    return PinPlan(
        model=model,
        assignments=assignments,
        candidates=selected,
        ranked_candidates=ranked,
        owned_channel_ids=owned,
        absolute_cost_ceiling_multiplier=maximum_price_multiplier,
        protected_billing_floor_multiplier=protected_billing_floor,
        current_operating_cost_multiplier=current_operating_cost,
        relative_cost_ceiling_multiplier=relative_cost_ceiling,
        effective_cost_ceiling_multiplier=effective_cost_ceiling,
    )


def weighted_pin_multiplier(
    assignments: Sequence[PinAssignment],
    one_hour_usage_by_channel: Mapping[int, int | Decimal],
) -> Decimal:
    if not assignments:
        raise PinControlError("pin_multiplier_assignments_empty")
    weighted_total = Decimal(0)
    weight_total = Decimal(0)
    multipliers: list[Decimal] = []
    for assignment in assignments:
        multiplier = assignment.candidate.median_multiplier
        if multiplier is None or multiplier <= 0:
            continue
        multipliers.append(multiplier)
        weight = _decimal(one_hour_usage_by_channel.get(assignment.candidate.channel_id, 0))
        if weight > 0:
            weighted_total += multiplier * weight
            weight_total += weight
    if weight_total > 0:
        return weighted_total / weight_total
    if not multipliers:
        raise PinControlError("pin_multiplier_unavailable")
    return sum(multipliers, Decimal(0)) / Decimal(len(multipliers))


def apply_pin_hysteresis(
    current_channel_id: int | None,
    proposed_candidate: PinCandidate,
    ranked_candidates: Sequence[PinCandidate],
    *,
    current_age_seconds: int | None,
    minimum_hold_seconds: int,
    minimum_switch_gain: Decimal | float | str,
    bypass: bool = False,
) -> PinSwitchDecision:
    minimum_hold = _bounded_int(
        minimum_hold_seconds,
        minimum=0,
        maximum=3600,
        error="pin_minimum_hold_invalid",
    )
    gain_threshold = _nonnegative_decimal(
        minimum_switch_gain,
        "pin_minimum_switch_gain_invalid",
    )
    if gain_threshold > 1:
        raise PinControlError("pin_minimum_switch_gain_invalid")
    if not isinstance(bypass, bool):
        raise PinControlError("pin_hysteresis_bypass_invalid")
    if current_channel_id is None or current_channel_id == proposed_candidate.channel_id:
        return PinSwitchDecision(
            proposed_candidate,
            "noop" if current_channel_id is not None else "switch_unpinned",
            None,
        )

    current = next(
        (
            candidate
            for candidate in ranked_candidates
            if candidate.channel_id == current_channel_id
        ),
        None,
    )
    if current is None or bypass:
        return PinSwitchDecision(
            proposed_candidate,
            "switch_bypass" if bypass else "switch_ineligible_current",
            None,
        )

    positions = {candidate.channel_id: index for index, candidate in enumerate(ranked_candidates)}
    if positions[current.channel_id] <= positions.get(
        proposed_candidate.channel_id, len(positions)
    ):
        return PinSwitchDecision(current, "hold_current_priority", Decimal(0))
    expected_gain = _candidate_price_gain(current, proposed_candidate)
    age = max(0, int(current_age_seconds or 0))
    if age < minimum_hold:
        return PinSwitchDecision(
            current,
            "hold_minimum_duration",
            expected_gain,
            minimum_hold - age,
        )
    return PinSwitchDecision(
        proposed_candidate,
        "switch_priority",
        expected_gain,
    )


class ProviderPinExecutor:
    def __init__(
        self,
        a6_client: PinA6Client,
        enodot_admin: PinEnodotAdmin,
        *,
        audit_store: PinAuditStore | None = None,
    ) -> None:
        self.a6_client = a6_client
        self.enodot_admin = enodot_admin
        self.audit_store = audit_store

    def execute(
        self,
        assignment: PinAssignment,
        *,
        owned_channel_ids: Sequence[int] | set[int] | frozenset[int],
        config_version: str,
        apply: bool = False,
        pause_before_change: bool = True,
    ) -> PinExecutionResult:
        if not isinstance(apply, bool) or not isinstance(pause_before_change, bool):
            raise PinControlError("pin_apply_invalid")
        target = _validate_target(assignment.target)
        candidate = assignment.candidate
        owned = frozenset(
            _positive_int(item, "pin_owned_channel_invalid") for item in owned_channel_ids
        )
        if candidate.channel_id in owned:
            raise PinControlError("pin_owned_channel_selected")
        if not str(config_version).strip():
            raise PinControlError("pin_config_version_invalid")
        if not apply:
            return PinExecutionResult(
                action="pin",
                applied=False,
                confirmed=False,
                merchant_id=target.merchant_id,
                token_id=target.token_id,
                channel_id=candidate.channel_id,
                enodot_account_id=target.enodot_account_id,
            )

        action_ref = ""
        if self.audit_store is not None:
            action_ref = self.audit_store.prepare(assignment, config_version=config_version)
        try:
            if pause_before_change:
                self.enodot_admin.set_schedulable(target.enodot_account_id, False)
            self.a6_client.marketplace_pin(
                target.a6_user_id,
                token_id=target.token_id,
                channel_id=candidate.channel_id,
                model=candidate.model,
                fallback_to_smart_routing=False,
            )
            observed = _find_pin(
                self.a6_client.marketplace_pins(target.a6_user_id),
                token_id=target.token_id,
                model=candidate.model,
            )
            observed_channel = _pin_channel_id(observed)
            if observed_channel in owned:
                raise PinControlError("pin_readback_owned_channel")
            if observed_channel != candidate.channel_id:
                raise PinControlError("pin_readback_mismatch")
            if self.audit_store is not None:
                self.audit_store.complete(action_ref, assignment, observed)
        except PinControlError as exc:
            if self.audit_store is not None and action_ref:
                self.audit_store.fail(action_ref, error_class=exc.code)
            raise
        except Exception:
            if self.audit_store is not None and action_ref:
                self.audit_store.fail(action_ref, error_class="pin_runtime_error")
            raise PinControlError("pin_runtime_error") from None

        return PinExecutionResult(
            action="pin",
            applied=True,
            confirmed=True,
            merchant_id=target.merchant_id,
            token_id=target.token_id,
            channel_id=candidate.channel_id,
            enodot_account_id=target.enodot_account_id,
        )


def _find_pin(
    pins: Sequence[Mapping[str, Any]],
    *,
    token_id: int,
    model: str,
) -> Mapping[str, Any]:
    matches = [
        pin
        for pin in pins
        if _optional_int(pin.get("token_id")) == token_id
        and str(pin.get("model_name") or "").strip() == model
        and not bool(pin.get("deleted"))
    ]
    if len(matches) != 1:
        raise PinControlError("pin_readback_not_unique")
    return matches[0]


def _pin_channel_id(pin: Mapping[str, Any]) -> int:
    direct = _optional_int(pin.get("channel_id"))
    if direct:
        return direct
    channel = pin.get("channel")
    if isinstance(channel, Mapping):
        nested = _optional_int(channel.get("id"))
        if nested:
            return nested
    raise PinControlError("pin_readback_channel_missing")


def _validate_target(target: ProviderPinTarget) -> ProviderPinTarget:
    for value in (
        target.merchant_id,
        target.a6_user_id,
        target.token_id,
        target.token_ref_id,
        target.enodot_account_id,
    ):
        _positive_int(value, "pin_target_invalid")
    return target


def _validate_quote(quote: ObserverQuote) -> ObserverQuote:
    _positive_int(quote.observer_merchant_id, "pin_quote_invalid")
    _positive_int(quote.channel_id, "pin_quote_invalid")
    if quote.listing_id is not None:
        _positive_int(quote.listing_id, "pin_quote_invalid")
    if not quote.model or quote.input_price_micros < 0 or quote.output_price_micros < 0:
        raise PinControlError("pin_quote_invalid")
    if quote.final_multiplier is not None and quote.final_multiplier <= 0:
        raise PinControlError("pin_quote_invalid")
    return quote


def _candidate_sort_key(
    candidate: PinCandidate,
) -> tuple[Decimal, int, int, int, Decimal, int]:
    multiplier = (
        candidate.median_multiplier
        if candidate.median_multiplier is not None
        else Decimal("Infinity")
    )
    success = candidate.latest_success_at
    latency = _candidate_latency(candidate)
    return (
        multiplier,
        1 if success is None else 0,
        -(success or 0),
        1 if latency is None else 0,
        latency if latency is not None else Decimal(0),
        candidate.channel_id,
    )


def _candidate_price_gain(current: PinCandidate, proposed: PinCandidate) -> Decimal:
    current_price = current.median_multiplier
    proposed_price = proposed.median_multiplier
    if current_price is None or proposed_price is None or current_price <= 0:
        return Decimal(0)
    return max(Decimal(0), (current_price - proposed_price) / current_price)


def _score_pin_candidates(
    candidates: Sequence[PinCandidate],
    *,
    price_weight: Decimal,
    success_weight: Decimal,
    latency_weight: Decimal,
    price_span_ratio: Decimal,
) -> tuple[PinCandidate, ...]:
    priced = [
        candidate.median_multiplier
        for candidate in candidates
        if candidate.median_multiplier is not None and candidate.median_multiplier > 0
    ]
    price_floor, _price_ceiling = _winsorized_bounds(priced)
    latencies = [
        latency
        for candidate in candidates
        if (latency := _candidate_latency(candidate)) is not None
    ]
    minimum_latency, maximum_latency = _winsorized_bounds(latencies)
    scored: list[PinCandidate] = []
    for candidate in candidates:
        if price_floor is None or candidate.median_multiplier is None:
            price_score = Decimal(1)
        else:
            price_score = _unit_interval(
                (candidate.median_multiplier / price_floor - Decimal(1)) / price_span_ratio
            )

        # The rolling success-rate window remains observable but is intentionally not
        # part of provider selection. Fresh last-success evidence is the second sort key.
        success_score = Decimal(0)

        latency = _candidate_latency(candidate)
        if latency is None:
            latency_score = Decimal("0.5")
        elif minimum_latency is None or maximum_latency == minimum_latency:
            latency_score = Decimal(0)
        else:
            latency_score = _unit_interval(
                (latency - minimum_latency) / (maximum_latency - minimum_latency)
            )
        composite_score = (
            price_weight * price_score
            + success_weight * success_score
            + latency_weight * latency_score
        )
        scored.append(
            replace(
                candidate,
                price_score=price_score,
                success_score=success_score,
                latency_score=latency_score,
                composite_score=composite_score,
            )
        )
    return tuple(scored)


def _candidate_latency(candidate: PinCandidate) -> Decimal | None:
    values = tuple(
        value
        for value in (candidate.median_latency_ms, candidate.median_ttft_ms)
        if value is not None
    )
    return max(values) if values else None


def _candidate_passes_hard_gates(
    candidate: PinCandidate,
    *,
    minimum_success_rate: int,
    maximum_latency_ms: Decimal | None,
    maximum_multiplier: Decimal | None,
) -> bool:
    _ = minimum_success_rate
    if maximum_multiplier is not None and (
        candidate.median_multiplier is None
        or candidate.median_multiplier > maximum_multiplier
    ):
        return False
    if maximum_latency_ms is not None:
        latency = _candidate_latency(candidate)
        if latency is None or latency > maximum_latency_ms:
            return False
    return True


def _minimum_optional_decimal(*values: Decimal | None) -> Decimal | None:
    present = tuple(value for value in values if value is not None)
    return min(present) if present else None


def _winsorized_bounds(
    values: Sequence[Decimal],
) -> tuple[Decimal | None, Decimal | None]:
    if not values:
        return None, None
    ordered = sorted(values)
    if len(ordered) < 5:
        return ordered[0], ordered[-1]
    return (
        _quantile_decimal(ordered, numerator=1, denominator=10),
        _quantile_decimal(ordered, numerator=9, denominator=10),
    )


def _quantile_decimal(
    ordered: Sequence[Decimal],
    *,
    numerator: int,
    denominator: int,
) -> Decimal:
    if not ordered or denominator < 1 or not 0 <= numerator <= denominator:
        raise PinControlError("pin_quantile_invalid")
    position_numerator = (len(ordered) - 1) * numerator
    lower_index, remainder = divmod(position_numerator, denominator)
    lower = ordered[lower_index]
    if remainder == 0:
        return lower
    upper = ordered[lower_index + 1]
    return lower + (upper - lower) * Decimal(remainder) / Decimal(denominator)


def _worst_health_state(values: Sequence[str] | Any) -> str:
    priority = {
        "healthy": 0,
        "unlabeled": 1,
        "idle": 2,
        "unknown": 3,
        "degraded": 4,
        "cooling": 5,
        "unavailable": 6,
    }
    normalized = [str(value or "unknown").strip().lower() for value in values]
    if not normalized:
        return "unknown"
    return max(normalized, key=lambda value: priority.get(value, 3))


def _worst_failure_responsibility(values: Sequence[str] | Any) -> str:
    priority = {"client": 0, "unknown": 1, "platform": 2, "provider": 3}
    normalized = [str(value or "unknown").strip().lower() for value in values]
    if not normalized:
        return "unknown"
    return max(normalized, key=lambda value: priority.get(value, 1))


def _pin_weights(
    price_weight: Decimal | float | str,
    success_weight: Decimal | float | str,
    latency_weight: Decimal | float | str,
) -> tuple[Decimal, Decimal, Decimal]:
    weights = tuple(
        _nonnegative_decimal(value, "pin_weights_invalid")
        for value in (price_weight, success_weight, latency_weight)
    )
    if abs(sum(weights, Decimal(0)) - Decimal(1)) > Decimal("0.000001"):
        raise PinControlError("pin_weights_invalid")
    return weights


def _unit_interval(value: Decimal) -> Decimal:
    return max(Decimal(0), min(Decimal(1), value))


def _median_decimal(values: Sequence[Decimal]) -> Decimal | None:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / Decimal(2)


def _median_int(values: Sequence[int]) -> int:
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) // 2


def _median_optional_int(values: Sequence[int]) -> int | None:
    return _median_int(values) if values else None


def _nonnegative_decimal(value: Any, error: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise PinControlError(error) from None
    if not result.is_finite() or result < 0:
        raise PinControlError(error)
    return result


def _positive_decimal(value: Any, error: str) -> Decimal:
    result = _nonnegative_decimal(value, error)
    if result <= 0:
        raise PinControlError(error)
    return result


def _decimal(value: Any) -> Decimal:
    if isinstance(value, bool):
        return Decimal(0)
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return Decimal(0)
    return result if result.is_finite() else Decimal(0)


def _positive_int(value: Any, error: str) -> int:
    if isinstance(value, bool):
        raise PinControlError(error)
    try:
        result = int(value)
    except (TypeError, ValueError):
        raise PinControlError(error) from None
    if result < 1:
        raise PinControlError(error)
    return result


def _bounded_int(value: Any, *, minimum: int, maximum: int, error: str) -> int:
    if isinstance(value, bool):
        raise PinControlError(error)
    try:
        result = int(value)
    except (TypeError, ValueError):
        raise PinControlError(error) from None
    if result < minimum or result > maximum:
        raise PinControlError(error)
    return result


def _optional_int(value: Any) -> int | None:
    try:
        result = int(value)
    except (TypeError, ValueError):
        return None
    return result if not isinstance(value, bool) and result > 0 else None
