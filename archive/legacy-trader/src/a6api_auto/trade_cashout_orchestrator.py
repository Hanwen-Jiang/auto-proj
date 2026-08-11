from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from decimal import ROUND_FLOOR, Decimal
from typing import Any, Protocol

from .errors import A6AutoError
from .trade_cashout_inventory import (
    WalletInventory,
    rendezvous_score,
)

MICROS_PER_USD = 1_000_000
FUND_LOCK_NAMESPACE = 0x41364652
CONFIG_VERSION = "a6-risk-balanced-cashout-v2"

NONTERMINAL_INTENT_STATES = frozenset(
    {
        "PLANNED",
        "AWAITING_PROVIDER_DRAIN",
        "PREVIEWED",
        "SUBMITTED",
        "PENDING",
        "UNKNOWN",
        "RECONCILING",
    }
)


class CashoutError(A6AutoError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class CashoutPolicy:
    config_version: str = CONFIG_VERSION
    standard_min_gross_usd: Decimal = Decimal("50")
    standard_fixed_fee_usd: Decimal = Decimal("3")
    standard_target_step_usd: Decimal = Decimal("0.25")
    standard_target_cap_high_risk_usd: Decimal = Decimal("58")
    standard_target_cap_medium_risk_usd: Decimal = Decimal("75")
    standard_target_cap_low_risk_usd: Decimal = Decimal("100")
    standard_target_jitter_max_usd: Decimal = Decimal("2.50")
    standard_confirmation_hours: Decimal = Decimal("24")
    standard_success_probability: Decimal = Decimal("1")
    standard_extra_cost_usd: Decimal = Decimal("0")
    callback_fee_rate: Decimal = Decimal("0.1666666666666666666666666667")
    callback_success_probability: Decimal = Decimal("1")
    callback_fixed_cost_usd: Decimal = Decimal("0")
    callback_min_balance_usd: Decimal = Decimal("1")
    high_risk_delta_floor: Decimal = Decimal("0.45")
    high_risk_balance_trigger_usd: Decimal = Decimal("5")
    high_risk_max_residual_age_hours: Decimal = Decimal("24")
    medium_risk_delta_floor: Decimal = Decimal("0.30")
    medium_risk_max_wait_hours: Decimal = Decimal("72")
    max_skipped_rounds: int = 3
    risk_training_window_days: int = 30
    risk_minimum_samples: int = 100
    reconcile_interval_seconds: int = 300
    financial_snapshot_ttl_seconds: int = 5
    route_readback_ttl_seconds: int = 86_400
    max_parallel_standard: int = 2
    max_parallel_callback: int = 1
    standard_withdraw_apply_enabled: bool = False
    callback_sweep_apply_enabled: bool = False

    def __post_init__(self) -> None:
        caps = (
            self.standard_target_cap_high_risk_usd,
            self.standard_target_cap_medium_risk_usd,
            self.standard_target_cap_low_risk_usd,
        )
        if (
            not self.config_version.strip()
            or self.standard_min_gross_usd < Decimal("50")
            or self.standard_fixed_fee_usd < 0
            or self.standard_fixed_fee_usd >= self.standard_min_gross_usd
            or self.standard_target_step_usd <= 0
            or any(cap < self.standard_min_gross_usd for cap in caps)
            or self.standard_target_jitter_max_usd < 0
            or self.standard_confirmation_hours < 0
            or self.standard_extra_cost_usd < 0
            or self.callback_fixed_cost_usd < 0
            or self.callback_min_balance_usd <= 0
            or self.high_risk_balance_trigger_usd < self.callback_min_balance_usd
            or self.high_risk_max_residual_age_hours <= 0
            or self.medium_risk_max_wait_hours <= 0
            or self.max_skipped_rounds < 1
            or self.risk_training_window_days < 1
            or self.risk_minimum_samples < 1
            or self.reconcile_interval_seconds < 1
            or self.financial_snapshot_ttl_seconds < 1
            or self.route_readback_ttl_seconds < 1
            or self.max_parallel_standard < 1
            or self.max_parallel_callback < 1
        ):
            raise CashoutError("cashout_policy_invalid")
        for probability in (
            self.standard_success_probability,
            self.callback_success_probability,
        ):
            _validate_probability(probability, "cashout_policy_probability_invalid")
        if self.callback_fee_rate < 0 or self.callback_fee_rate >= 1:
            raise CashoutError("cashout_callback_fee_rate_invalid")
        if not (Decimal("0") <= self.medium_risk_delta_floor < self.high_risk_delta_floor):
            raise CashoutError("cashout_risk_delta_threshold_invalid")
        if self.high_risk_delta_floor > Decimal("0.5"):
            raise CashoutError("cashout_risk_delta_threshold_invalid")


@dataclass(frozen=True)
class CashoutFinancialSnapshot:
    merchant_id: int
    withdrawable_micros: int
    provider_reserve_micros: int
    local_reserved_other_micros: int
    balance_source_hash: str
    observed_at: datetime
    quota_per_unit: int

    def __post_init__(self) -> None:
        if (
            self.merchant_id < 1
            or self.withdrawable_micros < 0
            or self.provider_reserve_micros < 0
            or self.local_reserved_other_micros < 0
            or self.quota_per_unit < 1
            or not _hash_valid(self.balance_source_hash)
            or self.observed_at.tzinfo is None
        ):
            raise CashoutError("cashout_financial_snapshot_invalid")

    @property
    def cashout_eligible_micros(self) -> int:
        return max(
            0,
            self.withdrawable_micros
            - self.provider_reserve_micros
            - self.local_reserved_other_micros,
        )


@dataclass(frozen=True)
class CashoutRiskEvidence:
    delta_effective: Decimal
    merchant_age_hours: Decimal
    hours_since_last_confirmed_cashout: Decimal | None
    earn_rate_p25_micros_per_hour: int
    session_refresh_failures: int = 0
    withdraw_preview_failures: int = 0
    balance_unreadable_streak: int = 0
    same_cohort_recent_restrictions: int = 0
    hard_risk: bool = False
    merchant_restricted: bool = False
    risk_model_version: str = "cashout-bootstrap-v1"
    training_window_days: int = 30
    sample_count: int = 0
    hazard_per_hour: Decimal | None = None
    fixed_probability: Decimal | None = None
    extra_evidence: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if (
            self.delta_effective < 0
            or self.delta_effective > Decimal("0.5")
            or self.merchant_age_hours < 0
            or (
                self.hours_since_last_confirmed_cashout is not None
                and self.hours_since_last_confirmed_cashout < 0
            )
            or self.earn_rate_p25_micros_per_hour < 0
            or self.session_refresh_failures < 0
            or self.withdraw_preview_failures < 0
            or self.balance_unreadable_streak < 0
            or self.same_cohort_recent_restrictions < 0
            or not self.risk_model_version.strip()
            or self.training_window_days < 1
            or self.sample_count < 0
            or (self.hazard_per_hour is not None and self.hazard_per_hour < 0)
            or not isinstance(self.extra_evidence, Mapping)
        ):
            raise CashoutError("cashout_risk_evidence_invalid")
        if self.fixed_probability is not None:
            _validate_probability(
                self.fixed_probability,
                "cashout_risk_probability_invalid",
            )

    def tier(self, policy: CashoutPolicy) -> str:
        if (
            self.hard_risk
            or self.merchant_restricted
            or self.delta_effective >= policy.high_risk_delta_floor
        ):
            return "high"
        if self.delta_effective >= policy.medium_risk_delta_floor:
            return "medium"
        return "low"

    def probability(self, horizon_hours: Decimal, policy: CashoutPolicy) -> Decimal:
        horizon = max(Decimal("0"), horizon_hours)
        if horizon == 0:
            return Decimal("0")
        if self.fixed_probability is not None:
            return self.fixed_probability
        hazard = self.hazard_per_hour
        if hazard is None:
            hazard = {
                "high": Decimal("0.006"),
                "medium": Decimal("0.0015"),
                "low": Decimal("0.00025"),
            }[self.tier(policy)]
            hazard += min(Decimal("0.01"), Decimal(self.session_refresh_failures) / 1000)
            hazard += min(Decimal("0.01"), Decimal(self.withdraw_preview_failures) / 500)
            hazard += min(Decimal("0.01"), Decimal(self.balance_unreadable_streak) / 500)
            hazard += min(
                Decimal("0.01"),
                Decimal(self.same_cohort_recent_restrictions) / 1000,
            )
            if self.hard_risk or self.merchant_restricted:
                hazard = max(hazard, Decimal("0.02"))
        probability = Decimal("1") - (-hazard * horizon).exp()
        return _clamp_probability(probability)

    def public(self) -> dict[str, Any]:
        return {
            "delta_effective": _decimal_text(self.delta_effective),
            "merchant_age_hours": _decimal_text(self.merchant_age_hours),
            "hours_since_last_confirmed_cashout": (
                _decimal_text(self.hours_since_last_confirmed_cashout)
                if self.hours_since_last_confirmed_cashout is not None
                else None
            ),
            "earn_rate_p25_micros_per_hour": self.earn_rate_p25_micros_per_hour,
            "session_refresh_failures": self.session_refresh_failures,
            "withdraw_preview_failures": self.withdraw_preview_failures,
            "balance_unreadable_streak": self.balance_unreadable_streak,
            "same_cohort_recent_restrictions": self.same_cohort_recent_restrictions,
            "hard_risk": self.hard_risk,
            "merchant_restricted": self.merchant_restricted,
            "risk_model_version": self.risk_model_version,
            "training_window_days": self.training_window_days,
            "sample_count": self.sample_count,
            "extra": dict(self.extra_evidence),
        }


@dataclass(frozen=True)
class CashoutRoutePreview:
    wallet_route_id: int
    wallet_principal_id: int
    remote_withdraw_account_id: int
    network: str
    gross_micros: int
    fee_micros: int
    net_micros: int
    confirmation_probability: Decimal
    preview_source_hash: str
    healthy: bool = True
    authoritative: bool = True
    latency_penalty_micros: int = 0
    reconciliation_risk_micros: int = 0

    def __post_init__(self) -> None:
        if (
            self.wallet_route_id < 1
            or self.wallet_principal_id < 1
            or self.remote_withdraw_account_id < 1
            or not self.network.strip()
            or self.gross_micros < 1
            or self.fee_micros < 0
            or self.net_micros < 0
            or self.fee_micros + self.net_micros > self.gross_micros
            or self.latency_penalty_micros < 0
            or self.reconciliation_risk_micros < 0
            or not _hash_valid(self.preview_source_hash)
        ):
            raise CashoutError("cashout_route_preview_invalid")
        _validate_probability(
            self.confirmation_probability,
            "cashout_route_probability_invalid",
        )

    @property
    def expected_value_micros(self) -> Decimal:
        return (
            Decimal(self.net_micros) * self.confirmation_probability
            - Decimal(self.latency_penalty_micros)
            - Decimal(self.reconciliation_risk_micros)
        )


@dataclass(frozen=True)
class CashoutCycle:
    decision_epoch: int
    standard_target_micros: int
    target_jitter_micros: int
    risk_tier: str
    balance_source_hash: str
    preview_contract_hash: str
    config_version: str

    def __post_init__(self) -> None:
        if (
            self.decision_epoch < 1
            or self.standard_target_micros < 50 * MICROS_PER_USD
            or abs(self.target_jitter_micros) > 2_500_000
            or self.risk_tier not in {"high", "medium", "low"}
            or not _hash_valid(self.balance_source_hash)
            or not _hash_valid(self.preview_contract_hash)
            or not self.config_version.strip()
        ):
            raise CashoutError("cashout_cycle_invalid")


@dataclass(frozen=True)
class CashoutSource:
    financial: CashoutFinancialSnapshot
    risk: CashoutRiskEvidence
    source_funds_accrued_micros: int = 0
    confirmed_source_debit_micros: int = 0
    last_confirmed_cashout_at: datetime | None = None
    skipped_rounds: int = 0
    oldest_residual_age_hours: Decimal = Decimal("0")
    provider_duty_state: str = "NONE"
    provider_drain_confirmed: bool = False
    callback_available: bool = False
    callback_sender_ref: str | None = None
    callback_receiver_merchant_id: int | None = None
    callback_receiver_model: str | None = None
    callback_receiver_listing_id: int | None = None
    funds_claimed: bool = False
    routes: Sequence[CashoutRoutePreview] = ()
    existing_cycle: CashoutCycle | None = None
    decision_epoch: int | None = None
    resumable_intent_id: int | None = None

    def __post_init__(self) -> None:
        if (
            self.source_funds_accrued_micros < 0
            or self.confirmed_source_debit_micros < 0
            or self.skipped_rounds < 0
            or self.oldest_residual_age_hours < 0
            or self.provider_duty_state not in {"NONE", "READY", "WARM_STANDBY"}
            or (
                self.last_confirmed_cashout_at is not None
                and self.last_confirmed_cashout_at.tzinfo is None
            )
            or (self.decision_epoch is not None and self.decision_epoch < 1)
            or (self.resumable_intent_id is not None and self.resumable_intent_id < 1)
        ):
            raise CashoutError("cashout_source_invalid")
        if self.callback_available and (
            not str(self.callback_sender_ref or "").strip()
            or int(self.callback_receiver_merchant_id or 0) < 1
            or int(self.callback_receiver_merchant_id or 0) == self.financial.merchant_id
            or not str(self.callback_receiver_model or "").strip()
            or int(self.callback_receiver_listing_id or 0) < 1
        ):
            raise CashoutError("cashout_callback_target_invalid")


@dataclass(frozen=True)
class CashoutMerchantContext:
    merchant_id: int
    a6_user_id: int
    session_ref: str
    quota_per_unit: int
    merchant_status: str

    def __post_init__(self) -> None:
        if (
            self.merchant_id < 1
            or self.a6_user_id < 1
            or not self.session_ref.strip()
            or self.quota_per_unit < 1
            or not self.merchant_status.strip()
        ):
            raise CashoutError("cashout_merchant_context_invalid")


@dataclass(frozen=True)
class CashoutCandidate:
    context: CashoutMerchantContext
    source: CashoutSource

    def __post_init__(self) -> None:
        if self.context.merchant_id != self.source.financial.merchant_id:
            raise CashoutError("cashout_candidate_merchant_mismatch")


@dataclass(frozen=True)
class CashoutPlan:
    merchant_id: int
    method: str | None
    action: str
    priority: str
    decision_reason: str
    gross_micros: int
    provider_reserve_micros: int
    cashout_eligible_micros: int
    expected_fee_micros: int
    expected_net_micros: int
    standard_wait_value_micros: Decimal
    callback_value_micros: Decimal
    risk_probability: Decimal
    risk_horizon_hours: Decimal
    p_star: Decimal
    risk_tier: str
    hard_risk: bool
    cashout_coverage: Decimal
    risk_exposure_micros: int
    skipped_rounds: int
    oldest_residual_age_hours: Decimal
    last_confirmed_cashout_at: datetime | None
    wallet_route_id: int | None
    wallet_principal_id: int | None
    remote_withdraw_account_id: int | None
    route_authoritative: bool
    callback_sender_ref: str | None
    callback_receiver_merchant_id: int | None
    callback_receiver_model: str | None
    callback_receiver_listing_id: int | None
    financial: CashoutFinancialSnapshot
    risk: CashoutRiskEvidence
    cycle: CashoutCycle
    resume_intent_id: int | None
    idempotency_key: str | None

    @property
    def executable(self) -> bool:
        return self.method is not None and self.action in {
            "execute",
            "awaiting_provider_drain",
        }

    def public(self) -> dict[str, Any]:
        return {
            "merchant_id": self.merchant_id,
            "method": self.method,
            "action": self.action,
            "priority": self.priority,
            "decision_reason": self.decision_reason,
            "gross_micros": self.gross_micros,
            "provider_reserve_micros": self.provider_reserve_micros,
            "cashout_eligible_micros": self.cashout_eligible_micros,
            "standard_target_micros": self.cycle.standard_target_micros,
            "target_jitter_micros": self.cycle.target_jitter_micros,
            "expected_fee_micros": self.expected_fee_micros,
            "expected_net_micros": self.expected_net_micros,
            "standard_wait_value_micros": _decimal_text(self.standard_wait_value_micros),
            "callback_value_micros": _decimal_text(self.callback_value_micros),
            "risk_probability": _decimal_text(self.risk_probability),
            "risk_horizon_hours": _decimal_text(self.risk_horizon_hours),
            "p_star": _decimal_text(self.p_star),
            "risk_tier": self.risk_tier,
            "hard_risk": self.hard_risk,
            "cashout_coverage": _decimal_text(self.cashout_coverage),
            "risk_exposure_micros": self.risk_exposure_micros,
            "wallet_route_id": self.wallet_route_id,
            "wallet_principal_id": self.wallet_principal_id,
            "remote_withdraw_account_id": self.remote_withdraw_account_id,
            "route_authoritative": self.route_authoritative,
            "callback_receiver_merchant_id": self.callback_receiver_merchant_id,
            "callback_receiver_model": self.callback_receiver_model,
            "balance_source_hash": self.financial.balance_source_hash,
            "decision_epoch": self.cycle.decision_epoch,
            "config_version": self.cycle.config_version,
            "resume_intent_id": self.resume_intent_id,
            "idempotency_key": self.idempotency_key,
        }


def cashout_eligible_micros(
    withdrawable_micros: int,
    provider_reserve_micros: int,
    local_reserved_other_micros: int,
) -> int:
    if (
        min(
            withdrawable_micros,
            provider_reserve_micros,
            local_reserved_other_micros,
        )
        < 0
    ):
        raise CashoutError("cashout_balance_component_invalid")
    return max(
        0,
        withdrawable_micros - provider_reserve_micros - local_reserved_other_micros,
    )


def standard_wait_hours(
    balance: Decimal,
    target: Decimal,
    earn_rate_per_hour: Decimal,
    confirmation_hours: Decimal,
) -> Decimal:
    if balance < 0 or target <= 0 or earn_rate_per_hour < 0 or confirmation_hours < 0:
        raise CashoutError("cashout_wait_input_invalid")
    deficit = max(Decimal("0"), target - balance)
    if deficit and earn_rate_per_hour == 0:
        return Decimal("Infinity")
    return (deficit / earn_rate_per_hour if deficit else Decimal("0")) + confirmation_hours


def standard_wait_value(
    balance: Decimal,
    target: Decimal,
    *,
    fixed_fee: Decimal,
    success_probability: Decimal,
    block_probability: Decimal,
    extra_cost: Decimal = Decimal("0"),
) -> Decimal:
    if balance < 0 or target <= fixed_fee or fixed_fee < 0 or extra_cost < 0:
        raise CashoutError("cashout_standard_value_input_invalid")
    _validate_probability(success_probability, "cashout_standard_probability_invalid")
    _validate_probability(block_probability, "cashout_block_probability_invalid")
    return (
        balance
        * (Decimal("1") - fixed_fee / target)
        * success_probability
        * (Decimal("1") - block_probability)
        - extra_cost
    )


def callback_value(
    balance: Decimal,
    *,
    fee_rate: Decimal,
    success_probability: Decimal,
    fixed_cost: Decimal = Decimal("0"),
) -> Decimal:
    if balance < 0 or fee_rate < 0 or fee_rate >= 1 or fixed_cost < 0:
        raise CashoutError("cashout_callback_value_input_invalid")
    _validate_probability(success_probability, "cashout_callback_probability_invalid")
    return balance * (Decimal("1") - fee_rate) * success_probability - fixed_cost


def callback_break_even_probability(
    balance: Decimal,
    target: Decimal,
    *,
    standard_fixed_fee: Decimal,
    callback_fee_rate: Decimal,
    standard_success_probability: Decimal = Decimal("1"),
    callback_success_probability: Decimal = Decimal("1"),
    standard_extra_cost: Decimal = Decimal("0"),
    callback_fixed_cost: Decimal = Decimal("0"),
) -> Decimal:
    if balance <= 0 or target <= standard_fixed_fee:
        raise CashoutError("cashout_break_even_input_invalid")
    _validate_probability(
        standard_success_probability,
        "cashout_standard_probability_invalid",
    )
    _validate_probability(
        callback_success_probability,
        "cashout_callback_probability_invalid",
    )
    denominator = (
        balance * (Decimal("1") - standard_fixed_fee / target) * standard_success_probability
    )
    if denominator <= 0:
        return Decimal("1")
    numerator = (
        balance * (Decimal("1") - callback_fee_rate) * callback_success_probability
        - callback_fixed_cost
        + standard_extra_cost
    )
    return _clamp_probability(Decimal("1") - numerator / denominator)


class CashoutPlanner:
    def __init__(self, policy: CashoutPolicy | None = None) -> None:
        self.policy = policy or CashoutPolicy()

    def plan_all(self, sources: Sequence[CashoutSource]) -> tuple[CashoutPlan, ...]:
        plans = [self.plan(source) for source in sources]
        return tuple(sorted(plans, key=self._sort_key))

    def plan(self, source: CashoutSource) -> CashoutPlan:
        policy = self.policy
        snapshot = source.financial
        eligible = snapshot.cashout_eligible_micros
        balance = _usd(eligible)
        tier = source.risk.tier(policy)
        hard_risk = source.risk.hard_risk or source.risk.merchant_restricted
        healthy_routes = tuple(
            route for route in source.routes if route.healthy and route.gross_micros == eligible
        )
        route = max(
            healthy_routes,
            key=lambda item: (item.expected_value_micros, -item.wallet_route_id),
            default=None,
        )
        standard_probability = (
            route.confirmation_probability
            if route is not None
            else policy.standard_success_probability
        )
        preview_hash = _preview_contract_hash(source.routes, policy)
        cycle = self._cycle(
            source,
            tier=tier,
            preview_contract_hash=preview_hash,
            standard_probability=standard_probability,
        )
        target = _usd(cycle.standard_target_micros)
        earn_rate = _usd(source.risk.earn_rate_p25_micros_per_hour)
        wait_hours = standard_wait_hours(
            balance,
            target,
            earn_rate,
            policy.standard_confirmation_hours,
        )
        risk_horizon = wait_hours if wait_hours.is_finite() else Decimal("1000000")
        risk_probability = source.risk.probability(risk_horizon, policy)
        p_star = callback_break_even_probability(
            max(balance, Decimal("0.000001")),
            target,
            standard_fixed_fee=policy.standard_fixed_fee_usd,
            callback_fee_rate=policy.callback_fee_rate,
            standard_success_probability=standard_probability,
            callback_success_probability=policy.callback_success_probability,
            standard_extra_cost=policy.standard_extra_cost_usd,
            callback_fixed_cost=policy.callback_fixed_cost_usd,
        )
        wait_value = standard_wait_value(
            balance,
            target,
            fixed_fee=policy.standard_fixed_fee_usd,
            success_probability=standard_probability,
            block_probability=risk_probability,
            extra_cost=policy.standard_extra_cost_usd,
        )
        cb_value = callback_value(
            balance,
            fee_rate=policy.callback_fee_rate,
            success_probability=policy.callback_success_probability,
            fixed_cost=policy.callback_fixed_cost_usd,
        )
        coverage = Decimal(source.confirmed_source_debit_micros) / Decimal(
            max(source.source_funds_accrued_micros, 1)
        )
        exposure = _floor_decimal(Decimal(eligible) * risk_probability)

        method: str | None = None
        action = "wait"
        priority = "P3"
        reason = "accumulating"
        expected_fee = 0
        expected_net = 0

        if eligible <= 0:
            reason = "reserved_or_empty"
        elif source.funds_claimed:
            reason = "funds_already_reserved"
        elif route is not None and eligible >= cycle.standard_target_micros:
            method = "crypto"
            action = "execute"
            priority = "P0" if hard_risk else "P1"
            reason = "hard_risk_standard_ready" if hard_risk else "standard_target_reached"
            expected_fee = route.fee_micros
            expected_net = _floor_decimal(route.expected_value_micros)
        elif hard_risk and route is not None and eligible >= _micros(policy.standard_min_gross_usd):
            method = "crypto"
            action = "execute"
            priority = "P0"
            reason = "hard_risk_standard_floor"
            expected_fee = route.fee_micros
            expected_net = _floor_decimal(route.expected_value_micros)
        elif self._callback_selected(
            source,
            tier=tier,
            balance=balance,
            wait_hours=wait_hours,
            risk_probability=risk_probability,
            p_star=p_star,
            wait_value=wait_value,
            cb_value=cb_value,
        ):
            method = "callback"
            action = "execute"
            priority = "P0" if hard_risk else "P2"
            reason = self._callback_reason(
                source,
                tier=tier,
                balance=balance,
                wait_hours=wait_hours,
            )
            expected_fee = _floor_decimal(
                Decimal(eligible) * policy.callback_fee_rate
                + Decimal(_micros(policy.callback_fixed_cost_usd))
            )
            expected_net = max(
                0,
                _floor_decimal(
                    Decimal(eligible)
                    * (Decimal("1") - policy.callback_fee_rate)
                    * policy.callback_success_probability
                    - Decimal(_micros(policy.callback_fixed_cost_usd))
                ),
            )
        elif route is None and eligible >= cycle.standard_target_micros:
            reason = "standard_route_unavailable"
        elif not source.callback_available and cb_value > wait_value:
            reason = "callback_unavailable"

        if (
            method is not None
            and tier == "high"
            and (
                source.provider_duty_state in {"READY", "WARM_STANDBY"}
                and not source.provider_drain_confirmed
            )
        ):
            action = "awaiting_provider_drain"
            reason = (
                "high_risk_drain_without_replacement" if hard_risk else "provider_drain_required"
            )

        idempotency_key = None
        if method is not None:
            destination = (
                f"route:{route.wallet_route_id}"
                if method == "crypto" and route is not None
                else f"receiver:{source.callback_receiver_merchant_id}"
            )
            idempotency_key = _sha256(
                {
                    "merchant_id": snapshot.merchant_id,
                    "method": method,
                    "balance_source_hash": snapshot.balance_source_hash,
                    "reserved_gross_micros": eligible,
                    "destination": destination,
                    "decision_epoch": cycle.decision_epoch,
                    "config_version": policy.config_version,
                }
            )

        return CashoutPlan(
            merchant_id=snapshot.merchant_id,
            method=method,
            action=action,
            priority=priority,
            decision_reason=reason,
            gross_micros=eligible if method is not None else 0,
            provider_reserve_micros=snapshot.provider_reserve_micros,
            cashout_eligible_micros=eligible,
            expected_fee_micros=expected_fee,
            expected_net_micros=expected_net,
            standard_wait_value_micros=wait_value * MICROS_PER_USD,
            callback_value_micros=cb_value * MICROS_PER_USD,
            risk_probability=risk_probability,
            risk_horizon_hours=risk_horizon,
            p_star=p_star,
            risk_tier=tier,
            hard_risk=hard_risk,
            cashout_coverage=coverage,
            risk_exposure_micros=exposure,
            skipped_rounds=source.skipped_rounds,
            oldest_residual_age_hours=source.oldest_residual_age_hours,
            last_confirmed_cashout_at=source.last_confirmed_cashout_at,
            wallet_route_id=route.wallet_route_id if method == "crypto" and route else None,
            wallet_principal_id=(
                route.wallet_principal_id if method == "crypto" and route else None
            ),
            remote_withdraw_account_id=(
                route.remote_withdraw_account_id if method == "crypto" and route else None
            ),
            route_authoritative=bool(
                method == "crypto" and route is not None and route.authoritative
            ),
            callback_sender_ref=(source.callback_sender_ref if method == "callback" else None),
            callback_receiver_merchant_id=(
                source.callback_receiver_merchant_id if method == "callback" else None
            ),
            callback_receiver_model=(
                source.callback_receiver_model if method == "callback" else None
            ),
            callback_receiver_listing_id=(
                source.callback_receiver_listing_id if method == "callback" else None
            ),
            financial=snapshot,
            risk=source.risk,
            cycle=cycle,
            resume_intent_id=source.resumable_intent_id,
            idempotency_key=idempotency_key,
        )

    def _cycle(
        self,
        source: CashoutSource,
        *,
        tier: str,
        preview_contract_hash: str,
        standard_probability: Decimal,
    ) -> CashoutCycle:
        existing = source.existing_cycle
        if (
            existing is not None
            and existing.risk_tier == tier
            and existing.preview_contract_hash == preview_contract_hash
            and existing.config_version == self.policy.config_version
        ):
            return existing
        decision_epoch = source.decision_epoch or (
            int(source.financial.observed_at.timestamp()) // self.policy.reconcile_interval_seconds
        )
        decision_epoch = max(1, decision_epoch)
        target, jitter = self._new_target(
            source,
            tier=tier,
            decision_epoch=decision_epoch,
            standard_probability=standard_probability,
        )
        return CashoutCycle(
            decision_epoch=decision_epoch,
            standard_target_micros=target,
            target_jitter_micros=jitter,
            risk_tier=tier,
            balance_source_hash=source.financial.balance_source_hash,
            preview_contract_hash=preview_contract_hash,
            config_version=self.policy.config_version,
        )

    def _new_target(
        self,
        source: CashoutSource,
        *,
        tier: str,
        decision_epoch: int,
        standard_probability: Decimal,
    ) -> tuple[int, int]:
        policy = self.policy
        minimum = _micros(policy.standard_min_gross_usd)
        if source.risk.hard_risk or source.risk.merchant_restricted:
            return minimum, 0
        cap = _micros(
            {
                "high": policy.standard_target_cap_high_risk_usd,
                "medium": policy.standard_target_cap_medium_risk_usd,
                "low": policy.standard_target_cap_low_risk_usd,
            }[tier]
        )
        step = _micros(policy.standard_target_step_usd)
        balance = _usd(source.financial.cashout_eligible_micros)
        earn_rate = _usd(source.risk.earn_rate_p25_micros_per_hour)
        best_target = minimum
        best_score: Decimal | None = None
        for target_micros in range(minimum, cap + 1, step):
            target = _usd(target_micros)
            wait = standard_wait_hours(
                balance,
                target,
                earn_rate,
                policy.standard_confirmation_hours,
            )
            risk_horizon = wait if wait.is_finite() else Decimal("1000000")
            probability = source.risk.probability(risk_horizon, policy)
            score = standard_wait_value(
                balance,
                target,
                fixed_fee=policy.standard_fixed_fee_usd,
                success_probability=standard_probability,
                block_probability=probability,
                extra_cost=policy.standard_extra_cost_usd,
            )
            if best_score is None or score > best_score:
                best_score = score
                best_target = target_micros
        jitter_limit_steps = _micros(policy.standard_target_jitter_max_usd) // step
        seed = _sha256(
            {
                "merchant_id": source.financial.merchant_id,
                "decision_epoch": decision_epoch,
                "risk_tier": tier,
                "config_version": policy.config_version,
            }
        )
        jitter_steps = int(seed[:16], 16) % (2 * jitter_limit_steps + 1)
        jitter_steps -= jitter_limit_steps
        requested_jitter = jitter_steps * step
        final_target = min(cap, max(minimum, best_target + requested_jitter))
        return final_target, final_target - best_target

    def _callback_selected(
        self,
        source: CashoutSource,
        *,
        tier: str,
        balance: Decimal,
        wait_hours: Decimal,
        risk_probability: Decimal,
        p_star: Decimal,
        wait_value: Decimal,
        cb_value: Decimal,
    ) -> bool:
        policy = self.policy
        if not source.callback_available or balance < policy.callback_min_balance_usd:
            return False
        if tier == "high" and balance >= policy.high_risk_balance_trigger_usd:
            return True
        if (
            tier == "high"
            and source.oldest_residual_age_hours >= policy.high_risk_max_residual_age_hours
        ):
            return True
        if tier == "medium" and (
            wait_hours <= policy.medium_risk_max_wait_hours or risk_probability <= p_star
        ):
            return False
        return cb_value > wait_value and risk_probability > p_star

    def _callback_reason(
        self,
        source: CashoutSource,
        *,
        tier: str,
        balance: Decimal,
        wait_hours: Decimal,
    ) -> str:
        policy = self.policy
        if tier == "high" and balance >= policy.high_risk_balance_trigger_usd:
            return "high_risk_balance_trigger"
        if (
            tier == "high"
            and source.oldest_residual_age_hours >= policy.high_risk_max_residual_age_hours
        ):
            return "high_risk_residual_age"
        if tier == "medium" and wait_hours > policy.medium_risk_max_wait_hours:
            return "medium_risk_wait_exceeds_threshold"
        return "callback_expected_value_higher"

    def _sort_key(self, plan: CashoutPlan) -> tuple[Any, ...]:
        priority = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}[plan.priority]
        starved = plan.skipped_rounds >= self.policy.max_skipped_rounds or (
            plan.risk_tier == "high"
            and plan.oldest_residual_age_hours >= self.policy.high_risk_max_residual_age_hours
        )
        last = (
            plan.last_confirmed_cashout_at.timestamp()
            if plan.last_confirmed_cashout_at is not None
            else float("-inf")
        )
        return (
            priority,
            0 if starved else 1,
            plan.cashout_coverage,
            -plan.risk_exposure_micros,
            last,
            plan.merchant_id,
        )


@dataclass(frozen=True)
class CashoutIntentRecord:
    intent_id: int
    merchant_id: int
    method: str
    state: str
    gross_micros: int
    fund_reservation_id: int
    idempotency_key: str
    remote_request_id: str | None = None
    remote_withdrawal_id: str | None = None


@dataclass(frozen=True)
class CashoutReconciliationContext:
    intent: CashoutIntentRecord
    merchant: CashoutMerchantContext
    remote_withdraw_account_id: int
    expected_fee_micros: int
    expected_net_micros: int
    balance_source_hash: str
    submit_attempted_at: datetime
    submitted_at: datetime | None
    confirmation_deadline_at: datetime | None
    baseline_withdrawable_micros: int
    baseline_withdrawing_micros: int | None
    baseline_paid_micros: int | None
    baseline_observed_at: datetime

    def __post_init__(self) -> None:
        optional_datetimes = (self.submitted_at, self.confirmation_deadline_at)
        optional_amounts = (
            self.baseline_withdrawing_micros,
            self.baseline_paid_micros,
        )
        if (
            self.intent.method != "crypto"
            or self.intent.merchant_id != self.merchant.merchant_id
            or self.remote_withdraw_account_id < 1
            or self.expected_fee_micros < 0
            or self.expected_net_micros < 0
            or self.expected_fee_micros + self.expected_net_micros
            > self.intent.gross_micros
            or not _hash_valid(self.balance_source_hash)
            or self.submit_attempted_at.tzinfo is None
            or self.baseline_observed_at.tzinfo is None
            or any(value is not None and value.tzinfo is None for value in optional_datetimes)
            or self.baseline_withdrawable_micros < 0
            or any(value is not None and value < 0 for value in optional_amounts)
        ):
            raise CashoutError("cashout_reconciliation_context_invalid")


@dataclass(frozen=True)
class CashoutReconciliation:
    outcome: str
    confirmation_type: str
    source_hash: str
    observed_at: datetime
    remote_status: str | None = None
    remote_withdrawal_id: str | None = None
    chain_txid: str | None = None
    observed_gross_micros: int | None = None
    observed_fee_micros: int | None = None
    observed_net_micros: int | None = None
    sender_debit_micros: int | None = None
    callback_fee_micros: int | None = None
    receiver_credit_micros: int | None = None
    authoritative_no_effect: bool = False
    failure_class: str | None = None
    evidence: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if (
            self.outcome not in {"pending", "confirmed", "unknown", "failed_final"}
            or not self.confirmation_type.strip()
            or not _hash_valid(self.source_hash)
            or self.observed_at.tzinfo is None
            or any(
                value is not None and value < 0
                for value in (
                    self.observed_gross_micros,
                    self.observed_fee_micros,
                    self.observed_net_micros,
                    self.sender_debit_micros,
                    self.callback_fee_micros,
                    self.receiver_credit_micros,
                )
            )
            or not isinstance(self.evidence, Mapping)
        ):
            raise CashoutError("cashout_reconciliation_invalid")
        if self.outcome == "failed_final" and not self.authoritative_no_effect:
            raise CashoutError("cashout_authoritative_no_effect_required")


@dataclass(frozen=True)
class CashoutSubmitResult:
    accepted: bool
    remote_request_id: str | None = None
    remote_withdrawal_id: str | None = None
    callback_request_id: str | None = None
    evidence: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.accepted, bool) or not isinstance(self.evidence, Mapping):
            raise CashoutError("cashout_submit_result_invalid")
        if self.accepted and not any(
            str(value or "").strip()
            for value in (
                self.remote_request_id,
                self.remote_withdrawal_id,
                self.callback_request_id,
            )
        ):
            raise CashoutError("cashout_submit_identity_missing")


class CryptoCashoutRemote(Protocol):
    def preview_candidate(self, candidate: CashoutCandidate) -> CashoutCandidate: ...

    def submit_crypto(
        self,
        plan: CashoutPlan,
        intent: CashoutIntentRecord,
    ) -> CashoutSubmitResult: ...


class CallbackCashoutRemote(Protocol):
    def submit_callback(
        self,
        plan: CashoutPlan,
        intent: CashoutIntentRecord,
    ) -> CashoutSubmitResult: ...


class A6CryptoCashoutRemote:
    def __init__(
        self,
        client_factory: Callable[[CashoutMerchantContext], Any],
        *,
        policy: CashoutPolicy | None = None,
    ) -> None:
        if not callable(client_factory):
            raise CashoutError("cashout_remote_client_factory_invalid")
        self.client_factory = client_factory
        self.policy = policy or CashoutPolicy()
        self._contexts: dict[int, CashoutMerchantContext] = {}

    def preview_candidate(self, candidate: CashoutCandidate) -> CashoutCandidate:
        context = candidate.context
        source = candidate.source
        self._contexts[context.merchant_id] = context
        if not source.routes:
            return candidate
        client = self.client_factory(context)
        summary = client.supplier_withdraw_summary(context.a6_user_id)
        live_withdrawable = _nonnegative_integer(
            summary.get("balance_available_micros"),
            "cashout_live_withdrawable_invalid",
        )
        if live_withdrawable != source.financial.withdrawable_micros:
            raise CashoutError("cashout_balance_changed_before_preview")
        authoritative: list[CashoutRoutePreview] = []
        for route in source.routes:
            try:
                preview = client.supplier_withdraw_preview(
                    context.a6_user_id,
                    account_id=route.remote_withdraw_account_id,
                    amount_micros=route.gross_micros,
                )
                if preview.get("enabled") is False:
                    continue
                fee, net = _withdraw_preview_amounts(preview, route.gross_micros)
            except Exception:
                continue
            authoritative.append(
                replace(
                    route,
                    fee_micros=fee,
                    net_micros=net,
                    preview_source_hash=_sha256(
                        {
                            "merchant_id": context.merchant_id,
                            "account_id": route.remote_withdraw_account_id,
                            "amount_micros": route.gross_micros,
                            "preview": dict(preview),
                        }
                    ),
                    healthy=True,
                    authoritative=True,
                )
            )
        return CashoutCandidate(
            context=context,
            source=replace(source, routes=tuple(authoritative)),
        )

    def submit_crypto(
        self,
        plan: CashoutPlan,
        intent: CashoutIntentRecord,
    ) -> CashoutSubmitResult:
        if (
            plan.method != "crypto"
            or plan.remote_withdraw_account_id is None
            or plan.gross_micros < _micros(self.policy.standard_min_gross_usd)
            or intent.merchant_id != plan.merchant_id
            or intent.gross_micros != plan.gross_micros
        ):
            raise CashoutError("cashout_crypto_submit_input_invalid")
        context = self._contexts.get(plan.merchant_id)
        if context is None:
            raise CashoutError("cashout_crypto_context_missing")
        response = self.client_factory(context).supplier_withdraw_submit(
            context.a6_user_id,
            account_id=plan.remote_withdraw_account_id,
            amount_micros=plan.gross_micros,
        )
        request_id = _optional_identity(response, "request_id", "requestId")
        withdrawal_id = _optional_identity(
            response,
            "withdrawal_id",
            "withdraw_id",
            "id",
        )
        if request_id is None and withdrawal_id is not None:
            request_id = f"withdrawal:{withdrawal_id}"
        return CashoutSubmitResult(
            accepted=True,
            remote_request_id=request_id,
            remote_withdrawal_id=withdrawal_id,
            evidence={"response": dict(response)},
        )


class A6CryptoCashoutReconciler:
    _SUCCESS_STATES = frozenset(
        {"complete", "completed", "success", "successful", "succeeded", "paid", "confirmed", "done"}
    )
    _FAILURE_STATES = frozenset(
        {"failed", "failure", "rejected", "cancelled", "canceled", "denied", "expired"}
    )
    _PENDING_STATES = frozenset(
        {
            "pending",
            "processing",
            "submitted",
            "queued",
            "reviewing",
            "under_review",
            "accepted",
            "approved",
        }
    )

    def __init__(
        self,
        client_factory: Callable[[CashoutMerchantContext], Any],
        *,
        clock: Callable[[], datetime] | None = None,
        match_window_seconds: int = 900,
    ) -> None:
        if not callable(client_factory):
            raise CashoutError("cashout_reconcile_client_factory_invalid")
        if isinstance(match_window_seconds, bool) or not 1 <= match_window_seconds <= 86_400:
            raise CashoutError("cashout_reconcile_match_window_invalid")
        self.client_factory = client_factory
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.match_window_seconds = match_window_seconds

    def reconcile(self, context: CashoutReconciliationContext) -> CashoutReconciliation:
        if not isinstance(context, CashoutReconciliationContext):
            raise CashoutError("cashout_reconciliation_context_invalid")
        observed_at = self.clock()
        if observed_at.tzinfo is None:
            raise CashoutError("cashout_reconcile_clock_invalid")

        errors: list[str] = []
        withdrawal: Mapping[str, Any] | None = None
        match_type: str | None = None
        summary: Mapping[str, Any] | None = None
        try:
            client = self.client_factory(context.merchant)
        except Exception as exc:
            return self._result(
                context,
                observed_at=observed_at,
                outcome="unknown",
                confirmation_type="remote_status",
                evidence={"errors": [_exception_code(exc)], "match_type": None},
            )

        if context.intent.remote_withdrawal_id is not None:
            try:
                found = client.supplier_withdraw_status(
                    context.merchant.a6_user_id,
                    context.intent.remote_withdrawal_id,
                )
                if isinstance(found, Mapping):
                    withdrawal = dict(found)
                    match_type = "remote_withdrawal_id"
            except Exception as exc:
                errors.append(_exception_code(exc))

        if withdrawal is None:
            try:
                page = client.supplier_withdrawals(
                    context.merchant.a6_user_id,
                    page=1,
                    size=1_000,
                )
                items = page.get("items") if isinstance(page, Mapping) else None
                withdrawal, match_type = _match_withdrawal(
                    items if isinstance(items, Sequence) else (),
                    context,
                    match_window_seconds=self.match_window_seconds,
                )
            except Exception as exc:
                errors.append(_exception_code(exc))

        try:
            remote_summary = client.supplier_withdraw_summary(context.merchant.a6_user_id)
            if isinstance(remote_summary, Mapping):
                summary = dict(remote_summary)
        except Exception as exc:
            errors.append(_exception_code(exc))

        balance_effect, balance_evidence = _withdraw_balance_effect(context, summary)
        remote_status = _withdraw_status(withdrawal)
        remote_withdrawal_id = (
            _optional_identity(withdrawal, "id", "withdrawal_id", "withdraw_id")
            if withdrawal is not None
            else context.intent.remote_withdrawal_id
        )
        chain_txid = (
            _optional_identity(
                withdrawal,
                "chain_txid",
                "txid",
                "tx_id",
                "transaction_hash",
            )
            if withdrawal is not None
            else None
        )
        evidence: dict[str, Any] = {
            "match_type": match_type,
            "withdrawal": dict(withdrawal or {}),
            "summary": dict(summary or {}),
            "balance_effect": balance_evidence,
            "errors": errors,
        }
        identity_conflicts = _withdrawal_identity_conflicts(withdrawal, context)
        if identity_conflicts:
            evidence["identity_conflicts"] = identity_conflicts
            return self._result(
                context,
                observed_at=observed_at,
                outcome="unknown",
                confirmation_type="remote_status",
                remote_status=remote_status,
                remote_withdrawal_id=remote_withdrawal_id,
                chain_txid=chain_txid,
                evidence=evidence,
            )

        if remote_status in self._SUCCESS_STATES and withdrawal is not None:
            amounts = _closed_withdraw_amounts(withdrawal, context.intent.gross_micros)
            if amounts is not None:
                gross, fee, net = amounts
                if not balance_effect:
                    deadline_exceeded = bool(
                        context.confirmation_deadline_at is not None
                        and observed_at >= context.confirmation_deadline_at
                    )
                    evidence["terminal_amounts_complete"] = True
                    evidence["confirmation_deadline_exceeded"] = deadline_exceeded
                    return self._result(
                        context,
                        observed_at=observed_at,
                        outcome="unknown" if deadline_exceeded else "pending",
                        confirmation_type="remote_status",
                        remote_status=remote_status,
                        remote_withdrawal_id=remote_withdrawal_id,
                        chain_txid=chain_txid,
                        observed_gross_micros=gross,
                        observed_fee_micros=fee,
                        observed_net_micros=net,
                        evidence=evidence,
                    )
                return self._result(
                    context,
                    observed_at=observed_at,
                    outcome="confirmed",
                    confirmation_type="remote_status",
                    remote_status=remote_status,
                    remote_withdrawal_id=remote_withdrawal_id,
                    chain_txid=chain_txid,
                    observed_gross_micros=gross,
                    observed_fee_micros=fee,
                    observed_net_micros=net,
                    evidence=evidence,
                )
            evidence["terminal_amounts_complete"] = False
            return self._result(
                context,
                observed_at=observed_at,
                outcome="unknown",
                confirmation_type="remote_status",
                remote_status=remote_status,
                remote_withdrawal_id=remote_withdrawal_id,
                chain_txid=chain_txid,
                evidence=evidence,
            )

        if remote_status in self._FAILURE_STATES:
            if balance_effect:
                evidence["terminal_failure_balance_conflict"] = True
                return self._result(
                    context,
                    observed_at=observed_at,
                    outcome="unknown",
                    confirmation_type="remote_status",
                    remote_status=remote_status,
                    remote_withdrawal_id=remote_withdrawal_id,
                    evidence=evidence,
                )
            return self._result(
                context,
                observed_at=observed_at,
                outcome="failed_final",
                confirmation_type="authoritative_no_effect",
                remote_status=remote_status,
                remote_withdrawal_id=remote_withdrawal_id,
                authoritative_no_effect=True,
                failure_class=f"remote_{remote_status}_no_effect",
                evidence=evidence,
            )

        deadline_exceeded = bool(
            context.confirmation_deadline_at is not None
            and observed_at >= context.confirmation_deadline_at
        )
        evidence["confirmation_deadline_exceeded"] = deadline_exceeded
        if balance_effect:
            outcome = "unknown" if deadline_exceeded else "pending"
            confirmation_type = "balance_effect"
        elif remote_status is not None and remote_status not in self._PENDING_STATES:
            outcome = "unknown"
            confirmation_type = "remote_status"
        elif withdrawal is not None or context.intent.remote_withdrawal_id is not None:
            outcome = "unknown" if deadline_exceeded else "pending"
            confirmation_type = "remote_status"
        else:
            outcome = "unknown"
            confirmation_type = "remote_status"
        return self._result(
            context,
            observed_at=observed_at,
            outcome=outcome,
            confirmation_type=confirmation_type,
            remote_status=remote_status,
            remote_withdrawal_id=remote_withdrawal_id,
            chain_txid=chain_txid,
            evidence=evidence,
        )

    @staticmethod
    def _result(
        context: CashoutReconciliationContext,
        *,
        observed_at: datetime,
        outcome: str,
        confirmation_type: str,
        evidence: Mapping[str, Any],
        remote_status: str | None = None,
        remote_withdrawal_id: str | None = None,
        chain_txid: str | None = None,
        observed_gross_micros: int | None = None,
        observed_fee_micros: int | None = None,
        observed_net_micros: int | None = None,
        authoritative_no_effect: bool = False,
        failure_class: str | None = None,
    ) -> CashoutReconciliation:
        normalized_evidence = _jsonable_mapping(evidence)
        source_hash = _sha256(
            {
                "intent_id": context.intent.intent_id,
                "confirmation_type": confirmation_type,
                "remote_status": remote_status,
                "evidence": normalized_evidence,
            }
        )
        return CashoutReconciliation(
            outcome=outcome,
            confirmation_type=confirmation_type,
            source_hash=source_hash,
            observed_at=observed_at,
            remote_status=remote_status,
            remote_withdrawal_id=remote_withdrawal_id,
            chain_txid=chain_txid,
            observed_gross_micros=observed_gross_micros,
            observed_fee_micros=observed_fee_micros,
            observed_net_micros=observed_net_micros,
            authoritative_no_effect=authoritative_no_effect,
            failure_class=failure_class,
            evidence=normalized_evidence,
        )


class MaxSpendCallbackRemote:
    """Submit exactly one callback request with a hard source-debit ceiling."""

    def __init__(
        self,
        submitter: Callable[..., Mapping[str, Any]],
    ) -> None:
        if not callable(submitter):
            raise CashoutError("cashout_callback_submitter_invalid")
        self.submitter = submitter

    def submit_callback(
        self,
        plan: CashoutPlan,
        intent: CashoutIntentRecord,
    ) -> CashoutSubmitResult:
        if (
            plan.method != "callback"
            or not str(plan.callback_sender_ref or "").strip()
            or int(plan.callback_receiver_merchant_id or 0) < 1
            or not str(plan.callback_receiver_model or "").strip()
            or int(plan.callback_receiver_listing_id or 0) < 1
            or intent.merchant_id != plan.merchant_id
            or intent.gross_micros != plan.gross_micros
        ):
            raise CashoutError("cashout_callback_submit_input_invalid")
        response = self.submitter(
            sender_ref=plan.callback_sender_ref,
            receiver_merchant_id=plan.callback_receiver_merchant_id,
            receiver_model=plan.callback_receiver_model,
            receiver_listing_id=plan.callback_receiver_listing_id,
            max_spend_micros=plan.gross_micros,
            idempotency_key=intent.idempotency_key,
            request_count=1,
        )
        if not isinstance(response, Mapping):
            raise CashoutError("cashout_callback_response_invalid")
        reported_debit = _mapping_integer(
            response,
            "sender_debit_micros",
            "adjusted_usage_micros",
        )
        if reported_debit is not None and reported_debit > plan.gross_micros:
            raise CashoutError("cashout_callback_max_spend_exceeded")
        request_id = _optional_identity(response, "request_id", "id")
        if request_id is None:
            raise CashoutError("cashout_callback_request_id_missing")
        return CashoutSubmitResult(
            accepted=True,
            remote_request_id=request_id,
            callback_request_id=request_id,
            evidence={"response": dict(response), "max_spend_micros": plan.gross_micros},
        )


class CashoutApplyExecutor:
    def __init__(
        self,
        store: PostgresCashoutStore,
        *,
        crypto_remote: CryptoCashoutRemote | None = None,
        callback_remote: CallbackCashoutRemote | None = None,
        policy: CashoutPolicy | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.store = store
        self.crypto_remote = crypto_remote
        self.callback_remote = callback_remote
        self.policy = policy or CashoutPolicy()
        self.clock = clock or (lambda: datetime.now().astimezone())

    def execute(self, plan: CashoutPlan) -> dict[str, Any]:
        if not plan.executable or plan.method not in {"crypto", "callback"}:
            raise CashoutError("cashout_plan_not_executable")
        if plan.method == "crypto":
            if not self.policy.standard_withdraw_apply_enabled:
                return self._disabled(plan, "standard_withdraw_apply_disabled")
            if not plan.route_authoritative:
                raise CashoutError("cashout_authoritative_preview_required")
            if self.crypto_remote is None:
                raise CashoutError("cashout_crypto_remote_missing")
        else:
            if not self.policy.callback_sweep_apply_enabled:
                return self._disabled(plan, "callback_sweep_apply_disabled")
            if self.callback_remote is None:
                raise CashoutError("cashout_callback_remote_missing")

        intent = self.store.create_intent(plan)
        if intent.state == "AWAITING_PROVIDER_DRAIN":
            return {
                "merchant_id": plan.merchant_id,
                "method": plan.method,
                "intent_id": intent.intent_id,
                "state": intent.state,
                "remote_write_count": 0,
            }
        if intent.state != "PREVIEWED":
            return {
                "merchant_id": plan.merchant_id,
                "method": plan.method,
                "intent_id": intent.intent_id,
                "state": intent.state,
                "remote_write_count": 0,
                "decision": "existing_intent_not_resubmitted",
            }
        submitting = self.store.begin_submit(intent.intent_id)
        try:
            if plan.method == "crypto":
                assert self.crypto_remote is not None
                result = self.crypto_remote.submit_crypto(plan, submitting)
            else:
                assert self.callback_remote is not None
                result = self.callback_remote.submit_callback(plan, submitting)
        except Exception:
            self.store.record_submit_result(
                intent.intent_id,
                accepted=False,
                remote_request_id=None,
            )
            return {
                "merchant_id": plan.merchant_id,
                "method": plan.method,
                "intent_id": intent.intent_id,
                "state": "UNKNOWN",
                "remote_write_count": 1,
                "decision": "submit_result_unknown",
            }
        remote_request_id = result.remote_request_id or result.callback_request_id
        self.store.record_submit_result(
            intent.intent_id,
            accepted=result.accepted,
            remote_request_id=remote_request_id,
            remote_withdrawal_id=result.remote_withdrawal_id,
            confirmation_deadline_at=(
                self.clock() + timedelta(hours=float(self.policy.standard_confirmation_hours))
            ),
        )
        return {
            "merchant_id": plan.merchant_id,
            "method": plan.method,
            "intent_id": intent.intent_id,
            "state": "PENDING" if result.accepted else "UNKNOWN",
            "remote_write_count": 1,
            "remote_request_id": remote_request_id,
            "remote_withdrawal_id": result.remote_withdrawal_id,
        }

    @staticmethod
    def _disabled(plan: CashoutPlan, reason: str) -> dict[str, Any]:
        return {
            "merchant_id": plan.merchant_id,
            "method": plan.method,
            "state": "METHOD_DISABLED",
            "remote_write_count": 0,
            "decision": reason,
        }


class PostgresCashoutStore:
    def __init__(
        self,
        connection_factory: Callable[[], Any],
        *,
        financial_snapshot_ttl_seconds: int = 5,
    ) -> None:
        if not callable(connection_factory):
            raise CashoutError("cashout_connection_factory_invalid")
        if isinstance(financial_snapshot_ttl_seconds, bool) or financial_snapshot_ttl_seconds < 1:
            raise CashoutError("cashout_financial_snapshot_ttl_invalid")
        self.connection_factory = connection_factory
        self.financial_snapshot_ttl_seconds = financial_snapshot_ttl_seconds

    def sync_wallet_inventory(
        self,
        inventory: WalletInventory,
        *,
        activate: bool = False,
    ) -> Mapping[str, int | bool | str]:
        if not isinstance(inventory, WalletInventory) or not isinstance(activate, bool):
            raise CashoutError("cashout_wallet_inventory_sync_invalid")
        connection = self.connection_factory()
        principal_count = 0
        route_count = 0
        active_principal_ids: list[int] = []
        active_route_ids: list[int] = []
        try:
            with connection.cursor() as cursor:
                for principal in inventory.principals:
                    cursor.execute(
                        """
                        INSERT INTO a6_trade.treasury_wallet_principals (
                            inventory_principal_key, principal_type, address_sha256,
                            address_masked, address_secret_ref, enabled, principal_state
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (principal_type, address_sha256) DO UPDATE SET
                            inventory_principal_key = EXCLUDED.inventory_principal_key,
                            address_masked = EXCLUDED.address_masked,
                            address_secret_ref = EXCLUDED.address_secret_ref,
                            enabled = CASE
                                WHEN %s THEN true
                                ELSE a6_trade.treasury_wallet_principals.enabled
                            END,
                            principal_state = CASE
                                WHEN %s THEN 'enabled'
                                ELSE a6_trade.treasury_wallet_principals.principal_state
                            END,
                            state_version =
                                a6_trade.treasury_wallet_principals.state_version + 1,
                            updated_at = now()
                        RETURNING id
                        """,
                        (
                            principal.principal_key,
                            principal.principal_type,
                            principal.address_sha256,
                            principal.address_masked,
                            principal.address_secret_ref,
                            activate,
                            "enabled" if activate else "staged",
                            activate,
                            activate,
                        ),
                    )
                    row = _row(cursor)
                    if row is None:
                        raise CashoutError("cashout_wallet_principal_sync_failed")
                    principal_id = int(row["id"])
                    active_principal_ids.append(principal_id)
                    principal_count += 1
                    for route in principal.routes:
                        cursor.execute(
                            """
                            INSERT INTO a6_trade.treasury_wallet_routes (
                                wallet_principal_id, inventory_route_key, network,
                                protocol, token_contract_sha256,
                                token_contract_display, route_state
                            ) VALUES (%s, %s, %s, %s, %s, %s, 'staged')
                            ON CONFLICT (
                                wallet_principal_id, network, token_contract_sha256
                            ) DO UPDATE SET
                                inventory_route_key = EXCLUDED.inventory_route_key,
                                protocol = EXCLUDED.protocol,
                                token_contract_display = EXCLUDED.token_contract_display,
                                route_state = CASE
                                    WHEN a6_trade.treasury_wallet_routes.route_state
                                         IN ('disabled', 'retired')
                                    THEN 'staged'
                                    ELSE a6_trade.treasury_wallet_routes.route_state
                                END,
                                updated_at = now()
                            RETURNING id
                            """,
                            (
                                principal_id,
                                route.route_key,
                                route.network,
                                route.protocol,
                                route.token_contract_sha256,
                                route.token_contract_display,
                            ),
                        )
                        route_row = _row(cursor)
                        if route_row is None:
                            raise CashoutError("cashout_wallet_route_sync_failed")
                        active_route_ids.append(int(route_row["id"]))
                        route_count += 1
                cursor.execute(
                    """
                    UPDATE a6_trade.treasury_wallet_routes
                    SET route_state = 'disabled', updated_at = now()
                    WHERE NOT (id = ANY(%s))
                      AND route_state <> 'disabled'
                    """,
                    (active_route_ids,),
                )
                cursor.execute(
                    """
                    UPDATE a6_trade.treasury_wallet_principals
                    SET enabled = false,
                        principal_state = 'disabled',
                        state_version = state_version + 1,
                        updated_at = now()
                    WHERE NOT (id = ANY(%s))
                      AND (enabled OR principal_state <> 'disabled')
                    """,
                    (active_principal_ids,),
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            _close(connection)
        return {
            "success": True,
            "inventory_id": inventory.inventory_id,
            "activated": activate,
            "principal_count": principal_count,
            "route_count": route_count,
        }

    def assign_wallet_principals(
        self,
        merchant_ids: Sequence[int],
        *,
        config_version: str,
    ) -> Mapping[int, int]:
        merchants = tuple(sorted(set(merchant_ids)))
        version = str(config_version or "").strip()
        invalid_merchants = any(
            isinstance(item, bool) or not isinstance(item, int) or item < 1 for item in merchants
        )
        if not version or invalid_merchants:
            raise CashoutError("cashout_wallet_assignment_input_invalid")
        connection = self.connection_factory()
        assignments: dict[int, int] = {}
        try:
            with connection.cursor() as cursor:
                for merchant_id in merchants:
                    cursor.execute(
                        "SELECT pg_advisory_xact_lock(%s, %s)",
                        (FUND_LOCK_NAMESPACE, merchant_id),
                    )
                    cursor.execute(
                        """
                        SELECT assignment.id, assignment.wallet_principal_id,
                               principal.enabled
                        FROM a6_trade.merchant_treasury_assignments assignment
                        JOIN a6_trade.treasury_wallet_principals principal
                          ON principal.id = assignment.wallet_principal_id
                        WHERE assignment.merchant_id = %s
                          AND assignment.assignment_state = 'active'
                        FOR UPDATE OF assignment
                        """,
                        (merchant_id,),
                    )
                    active = _row(cursor)
                    if active is not None and bool(active["enabled"]):
                        principal_id = int(active["wallet_principal_id"])
                    else:
                        if active is not None:
                            cursor.execute(
                                """
                                UPDATE a6_trade.merchant_treasury_assignments
                                SET assignment_state = 'released', released_at = now(),
                                    updated_at = now()
                                WHERE id = %s AND assignment_state = 'active'
                                """,
                                (int(active["id"]),),
                            )
                        cursor.execute(_WALLET_PRINCIPAL_CANDIDATES_SQL)
                        candidates = _rows(cursor)
                        if not candidates:
                            raise CashoutError("cashout_wallet_principal_unavailable")
                        minimum_received = min(int(row["received_micros"]) for row in candidates)
                        tied = [
                            row
                            for row in candidates
                            if int(row["received_micros"]) == minimum_received
                        ]
                        selected = max(
                            tied,
                            key=lambda row: rendezvous_score(
                                version,
                                merchant_id,
                                str(row["inventory_principal_key"]),
                            ),
                        )
                        principal_id = int(selected["id"])
                        score = rendezvous_score(
                            version,
                            merchant_id,
                            str(selected["inventory_principal_key"]),
                        )
                        cursor.execute(
                            """
                            INSERT INTO a6_trade.merchant_treasury_assignments (
                                merchant_id, wallet_principal_id, assignment_state,
                                config_version, assignment_score
                            ) VALUES (%s, %s, 'active', %s, %s)
                            RETURNING id
                            """,
                            (merchant_id, principal_id, version, score),
                        )
                        if _row(cursor) is None:
                            raise CashoutError("cashout_wallet_assignment_write_failed")
                    cursor.execute(
                        """
                        INSERT INTO a6_trade.merchant_treasury_route_bindings (
                            merchant_id, wallet_route_id, binding_state
                        )
                        SELECT %s, route.id, 'staged'
                        FROM a6_trade.treasury_wallet_routes route
                        WHERE route.wallet_principal_id = %s
                          AND route.route_state NOT IN ('disabled', 'retired')
                        ON CONFLICT (merchant_id, wallet_route_id) DO NOTHING
                        """,
                        (merchant_id, principal_id),
                    )
                    assignments[merchant_id] = principal_id
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            _close(connection)
        return assignments

    def record_route_binding(
        self,
        merchant_id: int,
        wallet_route_id: int,
        remote_withdraw_account_id: int,
        *,
        source_hash: str,
        observed_at: datetime,
        healthy: bool,
    ) -> None:
        if (
            min(merchant_id, wallet_route_id, remote_withdraw_account_id) < 1
            or not _hash_valid(source_hash)
            or observed_at.tzinfo is None
            or not isinstance(healthy, bool)
        ):
            raise CashoutError("cashout_route_binding_input_invalid")
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT pg_advisory_xact_lock(%s, %s)",
                    (FUND_LOCK_NAMESPACE, merchant_id),
                )
                cursor.execute(
                    """
                    SELECT 1
                    FROM a6_trade.merchant_treasury_assignments assignment
                    JOIN a6_trade.treasury_wallet_routes route
                      ON route.wallet_principal_id = assignment.wallet_principal_id
                    WHERE assignment.merchant_id = %s
                      AND assignment.assignment_state = 'active'
                      AND route.id = %s
                    FOR UPDATE OF assignment, route
                    """,
                    (merchant_id, wallet_route_id),
                )
                if _row(cursor) is None:
                    raise CashoutError("cashout_route_assignment_mismatch")
                cursor.execute(
                    """
                    INSERT INTO a6_trade.merchant_treasury_route_bindings (
                        merchant_id, wallet_route_id, a6_withdraw_account_id,
                        binding_state, remote_account_source_hash, last_readback_at
                    ) VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (merchant_id, wallet_route_id) DO UPDATE SET
                        a6_withdraw_account_id = EXCLUDED.a6_withdraw_account_id,
                        binding_state = EXCLUDED.binding_state,
                        remote_account_source_hash = EXCLUDED.remote_account_source_hash,
                        last_readback_at = EXCLUDED.last_readback_at,
                        updated_at = now()
                    """,
                    (
                        merchant_id,
                        wallet_route_id,
                        remote_withdraw_account_id,
                        "healthy" if healthy else "degraded",
                        source_hash,
                        observed_at,
                    ),
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            _close(connection)

    def load_candidates(
        self,
        observed_at: datetime,
        *,
        policy: CashoutPolicy,
        limit: int = 1_000,
    ) -> tuple[CashoutCandidate, ...]:
        if observed_at.tzinfo is None or isinstance(limit, bool) or not 1 <= limit <= 10_000:
            raise CashoutError("cashout_candidate_query_invalid")
        financial_cutoff = observed_at - timedelta(seconds=policy.financial_snapshot_ttl_seconds)
        route_cutoff = observed_at - timedelta(seconds=policy.route_readback_ttl_seconds)
        params = {
            "observed_at": observed_at,
            "financial_cutoff": financial_cutoff,
            "earning_cutoff": observed_at - timedelta(days=policy.risk_training_window_days),
            "reconcile_interval": policy.reconcile_interval_seconds,
            "limit": limit,
        }
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(_CASHOUT_CANDIDATES_SQL, params)
                base_rows = _rows(cursor)
                merchant_ids = [int(row["merchant_id"]) for row in base_rows]
                route_rows: list[dict[str, Any]] = []
                if merchant_ids:
                    cursor.execute(
                        _CASHOUT_ROUTES_SQL,
                        {"merchant_ids": merchant_ids, "route_cutoff": route_cutoff},
                    )
                    route_rows = _rows(cursor)
        finally:
            _close(connection)
        routes_by_merchant: dict[int, list[dict[str, Any]]] = {}
        for row in route_rows:
            routes_by_merchant.setdefault(int(row["merchant_id"]), []).append(row)
        return tuple(
            self._candidate_from_row(
                row,
                routes_by_merchant.get(int(row["merchant_id"]), ()),
                policy,
            )
            for row in base_rows
        )

    @staticmethod
    def _candidate_from_row(
        row: Mapping[str, Any],
        route_rows: Sequence[Mapping[str, Any]],
        policy: CashoutPolicy,
    ) -> CashoutCandidate:
        merchant_id = int(row["merchant_id"])
        financial = CashoutFinancialSnapshot(
            merchant_id=merchant_id,
            withdrawable_micros=max(0, int(row.get("withdrawable_micros") or 0)),
            provider_reserve_micros=max(0, int(row.get("provider_reserve_micros") or 0)),
            local_reserved_other_micros=max(0, int(row.get("local_reserved_other_micros") or 0)),
            balance_source_hash=str(row["balance_source_hash"]).strip(),
            observed_at=row["financial_observed_at"],
            quota_per_unit=int(row["quota_per_unit"]),
        )
        eligible = financial.cashout_eligible_micros
        fallback_fee = _micros(policy.standard_fixed_fee_usd)
        routes: list[CashoutRoutePreview] = []
        if eligible > fallback_fee:
            for route_row in route_rows:
                route_hash = _sha256(
                    {
                        "merchant_id": merchant_id,
                        "wallet_route_id": int(route_row["wallet_route_id"]),
                        "remote_withdraw_account_id": int(route_row["a6_withdraw_account_id"]),
                        "gross_micros": eligible,
                        "fallback_fee_micros": fallback_fee,
                        "binding_source_hash": str(
                            route_row.get("remote_account_source_hash") or ""
                        ).strip(),
                    }
                )
                routes.append(
                    CashoutRoutePreview(
                        wallet_route_id=int(route_row["wallet_route_id"]),
                        wallet_principal_id=int(route_row["wallet_principal_id"]),
                        remote_withdraw_account_id=int(route_row["a6_withdraw_account_id"]),
                        network=str(route_row["network"]),
                        gross_micros=eligible,
                        fee_micros=fallback_fee,
                        net_micros=eligible - fallback_fee,
                        confirmation_probability=Decimal(
                            str(route_row["confirmation_probability"])
                        ),
                        preview_source_hash=route_hash,
                        healthy=True,
                        authoritative=False,
                        latency_penalty_micros=int(route_row.get("latency_penalty_micros") or 0),
                        reconciliation_risk_micros=int(
                            route_row.get("reconciliation_risk_micros") or 0
                        ),
                    )
                )
        existing_cycle = None
        if row.get("cycle_id") is not None:
            existing_cycle = CashoutCycle(
                decision_epoch=int(row["cycle_decision_epoch"]),
                standard_target_micros=int(row["standard_target_micros"]),
                target_jitter_micros=int(row["target_jitter_micros"]),
                risk_tier=str(row["cycle_risk_tier"]),
                balance_source_hash=str(row["cycle_balance_source_hash"]).strip(),
                preview_contract_hash=str(row["preview_contract_hash"]).strip(),
                config_version=str(row["cycle_config_version"]),
            )
        hours_since = row.get("hours_since_last_confirmed_cashout")
        risk = CashoutRiskEvidence(
            delta_effective=Decimal(str(row["delta_effective"])),
            merchant_age_hours=Decimal(str(row["merchant_age_hours"])),
            hours_since_last_confirmed_cashout=(
                Decimal(str(hours_since)) if hours_since is not None else None
            ),
            earn_rate_p25_micros_per_hour=max(
                0, int(row.get("earn_rate_p25_micros_per_hour") or 0)
            ),
            session_refresh_failures=max(0, int(row.get("session_refresh_failures") or 0)),
            withdraw_preview_failures=max(0, int(row.get("withdraw_preview_failures") or 0)),
            same_cohort_recent_restrictions=max(
                0, int(row.get("same_cohort_recent_restrictions") or 0)
            ),
            hard_risk=bool(row.get("hard_risk")),
            merchant_restricted=bool(row.get("merchant_restricted")),
            training_window_days=policy.risk_training_window_days,
            sample_count=max(0, int(row.get("risk_sample_count") or 0)),
            extra_evidence={
                "financial_snapshot_fresh": True,
                "provider_reserve_fresh": True,
                "delta_readback": row.get("observed_delta") is not None,
            },
        )
        source = CashoutSource(
            financial=financial,
            risk=risk,
            source_funds_accrued_micros=max(0, int(row.get("source_funds_accrued_micros") or 0)),
            confirmed_source_debit_micros=max(
                0, int(row.get("confirmed_source_debit_micros") or 0)
            ),
            last_confirmed_cashout_at=row.get("last_confirmed_cashout_at"),
            skipped_rounds=max(0, int(row.get("skipped_rounds") or 0)),
            oldest_residual_age_hours=Decimal(str(row.get("oldest_residual_age_hours") or 0)),
            provider_duty_state=str(row.get("provider_duty_state") or "NONE"),
            provider_drain_confirmed=bool(row.get("provider_drain_confirmed")),
            funds_claimed=bool(row.get("funds_claimed")),
            routes=tuple(routes),
            existing_cycle=existing_cycle,
            decision_epoch=int(row["next_decision_epoch"]),
            resumable_intent_id=(
                int(row["resumable_intent_id"])
                if row.get("resumable_intent_id") is not None
                else None
            ),
        )
        return CashoutCandidate(
            context=CashoutMerchantContext(
                merchant_id=merchant_id,
                a6_user_id=int(row["a6_user_id"]),
                session_ref=str(row["session_ref"]),
                quota_per_unit=int(row["quota_per_unit"]),
                merchant_status=str(row["merchant_status"]),
            ),
            source=source,
        )

    def record_plan(self, plan: CashoutPlan) -> tuple[int, int]:
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT pg_advisory_xact_lock(%s, %s)",
                    (FUND_LOCK_NAMESPACE, plan.merchant_id),
                )
                cycle_id, risk_id = self._write_cycle_and_risk(cursor, plan)
            connection.commit()
            return cycle_id, risk_id
        except Exception:
            connection.rollback()
            raise
        finally:
            _close(connection)

    def create_intent(self, plan: CashoutPlan) -> CashoutIntentRecord:
        if not plan.executable or plan.method not in {"crypto", "callback"}:
            raise CashoutError("cashout_plan_not_executable")
        if not plan.idempotency_key or not _hash_valid(plan.idempotency_key):
            raise CashoutError("cashout_idempotency_key_invalid")
        if plan.resume_intent_id is not None:
            return self._resume_awaiting_intent(plan)
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT pg_advisory_xact_lock(%s, %s)",
                    (FUND_LOCK_NAMESPACE, plan.merchant_id),
                )
                existing = self._intent_by_key(cursor, plan.idempotency_key)
                if existing is not None:
                    connection.commit()
                    return existing
                cursor.execute(
                    """
                    SELECT id FROM a6_trade.merchant_fund_reservations
                    WHERE merchant_id = %s AND reservation_state = 'active'
                    FOR UPDATE
                    """,
                    (plan.merchant_id,),
                )
                if _row(cursor) is not None:
                    raise CashoutError("cashout_funds_already_reserved")
                self._validate_funds_snapshot(cursor, plan)
                cycle_id, risk_id = self._write_cycle_and_risk(cursor, plan)
                state = (
                    "AWAITING_PROVIDER_DRAIN"
                    if plan.action == "awaiting_provider_drain"
                    else "PREVIEWED"
                )
                cursor.execute(
                    """
                    INSERT INTO a6_trade.a6_withdrawal_intents (
                        decision_cycle_id, risk_snapshot_id, merchant_id, method,
                        gross_micros, reserved_micros, provider_reserve_micros,
                        cashout_eligible_micros, expected_fee_micros,
                        expected_net_micros, risk_probability, risk_horizon_hours,
                        risk_model_version, risk_tier, decision_reason,
                        cashout_coverage_before, risk_exposure_before_micros,
                        wallet_route_id, remote_withdraw_account_id,
                        idempotency_key, intent_state, balance_source_hash,
                        config_version
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s
                    )
                    RETURNING id
                    """,
                    (
                        cycle_id,
                        risk_id,
                        plan.merchant_id,
                        plan.method,
                        plan.gross_micros,
                        plan.gross_micros,
                        plan.provider_reserve_micros,
                        plan.cashout_eligible_micros,
                        plan.expected_fee_micros,
                        plan.expected_net_micros,
                        plan.risk_probability,
                        plan.risk_horizon_hours,
                        plan.risk.risk_model_version,
                        plan.risk_tier,
                        plan.decision_reason,
                        plan.cashout_coverage,
                        plan.risk_exposure_micros,
                        plan.wallet_route_id,
                        plan.remote_withdraw_account_id,
                        plan.idempotency_key,
                        state,
                        plan.financial.balance_source_hash,
                        plan.cycle.config_version,
                    ),
                )
                row = _row(cursor)
                if row is None:
                    raise CashoutError("cashout_intent_write_failed")
                intent_id = int(row["id"])
                owner_type = "crypto_cashout" if plan.method == "crypto" else "callback_cashout"
                cursor.execute(
                    """
                    INSERT INTO a6_trade.merchant_fund_reservations (
                        merchant_id, owner_type, owner_intent_id, reserved_micros,
                        reservation_state, balance_source_hash
                    ) VALUES (%s, %s, %s, %s, 'active', %s)
                    RETURNING id
                    """,
                    (
                        plan.merchant_id,
                        owner_type,
                        intent_id,
                        plan.gross_micros,
                        plan.financial.balance_source_hash,
                    ),
                )
                reservation = _row(cursor)
                if reservation is None:
                    raise CashoutError("cashout_reservation_write_failed")
                reservation_id = int(reservation["id"])
                cursor.execute(
                    """
                    UPDATE a6_trade.a6_withdrawal_intents
                    SET fund_reservation_id = %s, updated_at = now()
                    WHERE id = %s AND fund_reservation_id IS NULL
                    """,
                    (reservation_id, intent_id),
                )
                if int(getattr(cursor, "rowcount", 0) or 0) != 1:
                    raise CashoutError("cashout_reservation_link_failed")
                if plan.method == "callback":
                    cursor.execute(
                        """
                        INSERT INTO a6_trade.a6_callback_sweep_intents (
                            withdrawal_intent_id, sg_cloud_sender_ref,
                            receiver_merchant_id, receiver_model,
                            receiver_listing_id, max_spend_micros
                        ) VALUES (%s, %s, %s, %s, %s, %s)
                        """,
                        (
                            intent_id,
                            plan.callback_sender_ref,
                            plan.callback_receiver_merchant_id,
                            plan.callback_receiver_model,
                            plan.callback_receiver_listing_id,
                            plan.gross_micros,
                        ),
                    )
                record = CashoutIntentRecord(
                    intent_id=intent_id,
                    merchant_id=plan.merchant_id,
                    method=plan.method,
                    state=state,
                    gross_micros=plan.gross_micros,
                    fund_reservation_id=reservation_id,
                    idempotency_key=plan.idempotency_key,
                )
            connection.commit()
            return record
        except Exception:
            connection.rollback()
            raise
        finally:
            _close(connection)

    def _resume_awaiting_intent(self, plan: CashoutPlan) -> CashoutIntentRecord:
        intent_id = int(plan.resume_intent_id or 0)
        if intent_id < 1:
            raise CashoutError("cashout_resume_intent_invalid")
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT pg_advisory_xact_lock(%s, %s)",
                    (FUND_LOCK_NAMESPACE, plan.merchant_id),
                )
                cursor.execute(
                    """
                    SELECT intent.id, intent.merchant_id, intent.method,
                           intent.intent_state, intent.gross_micros,
                           intent.fund_reservation_id, intent.idempotency_key,
                           intent.remote_request_id, intent.remote_withdrawal_id,
                           reservation.reservation_state
                    FROM a6_trade.a6_withdrawal_intents intent
                    JOIN a6_trade.merchant_fund_reservations reservation
                      ON reservation.id = intent.fund_reservation_id
                     AND reservation.merchant_id = intent.merchant_id
                     AND reservation.owner_intent_id = intent.id
                    WHERE intent.id = %s AND intent.merchant_id = %s
                    FOR UPDATE OF intent, reservation
                    """,
                    (intent_id, plan.merchant_id),
                )
                current = _row(cursor)
                if current is None:
                    raise CashoutError("cashout_resume_intent_missing")
                if str(current["intent_state"]) != "AWAITING_PROVIDER_DRAIN":
                    if str(current["idempotency_key"]) == plan.idempotency_key:
                        connection.commit()
                        return _intent_record(current)
                    raise CashoutError("cashout_resume_intent_state_conflict")
                if str(current["reservation_state"]) != "active":
                    raise CashoutError("cashout_resume_reservation_inactive")
                if plan.action != "execute":
                    connection.commit()
                    return _intent_record(current)
                cursor.execute(
                    """
                    SELECT EXISTS (
                        SELECT 1
                        FROM a6_trade.provider_inventory inventory
                        WHERE inventory.merchant_id = %s
                          AND inventory.inventory_state IN ('READY', 'WARM_STANDBY')
                    ) OR EXISTS (
                        SELECT 1
                        FROM a6_trade.provider_pool_slots slot
                        JOIN a6_trade.provider_inventory inventory
                          ON inventory.model = slot.model
                         AND inventory.enodot_account_id = slot.enodot_account_id
                         AND inventory.a6_token_id = slot.a6_token_id
                        WHERE inventory.merchant_id = %s
                          AND slot.valid_to IS NULL
                          AND slot.slot_state <> 'VACANT'
                    ) AS provider_duty_active
                    """,
                    (plan.merchant_id, plan.merchant_id),
                )
                duty = _row(cursor)
                if duty is None or bool(duty.get("provider_duty_active")):
                    raise CashoutError("cashout_provider_drain_incomplete")
                self._validate_funds_snapshot(cursor, plan)
                cycle_id, risk_id = self._write_cycle_and_risk(cursor, plan)
                cursor.execute(
                    """
                    SELECT id
                    FROM a6_trade.a6_withdrawal_intents
                    WHERE idempotency_key = %s AND id <> %s
                    """,
                    (plan.idempotency_key, intent_id),
                )
                if _row(cursor) is not None:
                    raise CashoutError("cashout_resume_idempotency_conflict")
                if plan.method == "crypto":
                    cursor.execute(
                        """
                        DELETE FROM a6_trade.a6_callback_sweep_intents
                        WHERE withdrawal_intent_id = %s
                        """,
                        (intent_id,),
                    )
                cursor.execute(
                    """
                    UPDATE a6_trade.a6_withdrawal_intents
                    SET decision_cycle_id = %s, risk_snapshot_id = %s,
                        method = %s, gross_micros = %s, reserved_micros = %s,
                        provider_reserve_micros = %s,
                        cashout_eligible_micros = %s,
                        expected_fee_micros = %s, expected_net_micros = %s,
                        risk_probability = %s, risk_horizon_hours = %s,
                        risk_model_version = %s, risk_tier = %s,
                        decision_reason = %s, cashout_coverage_before = %s,
                        risk_exposure_before_micros = %s,
                        wallet_route_id = %s, remote_withdraw_account_id = %s,
                        idempotency_key = %s, intent_state = 'PREVIEWED',
                        balance_source_hash = %s, config_version = %s,
                        updated_at = now()
                    WHERE id = %s AND intent_state = 'AWAITING_PROVIDER_DRAIN'
                    RETURNING id, merchant_id, method, intent_state, gross_micros,
                              fund_reservation_id, idempotency_key,
                              remote_request_id, remote_withdrawal_id
                    """,
                    (
                        cycle_id,
                        risk_id,
                        plan.method,
                        plan.gross_micros,
                        plan.gross_micros,
                        plan.provider_reserve_micros,
                        plan.cashout_eligible_micros,
                        plan.expected_fee_micros,
                        plan.expected_net_micros,
                        plan.risk_probability,
                        plan.risk_horizon_hours,
                        plan.risk.risk_model_version,
                        plan.risk_tier,
                        plan.decision_reason,
                        plan.cashout_coverage,
                        plan.risk_exposure_micros,
                        plan.wallet_route_id,
                        plan.remote_withdraw_account_id,
                        plan.idempotency_key,
                        plan.financial.balance_source_hash,
                        plan.cycle.config_version,
                        intent_id,
                    ),
                )
                resumed = _row(cursor)
                if resumed is None:
                    raise CashoutError("cashout_resume_intent_update_failed")
                owner_type = (
                    "crypto_cashout" if plan.method == "crypto" else "callback_cashout"
                )
                cursor.execute(
                    """
                    UPDATE a6_trade.merchant_fund_reservations
                    SET owner_type = %s, reserved_micros = %s,
                        balance_source_hash = %s, updated_at = now()
                    WHERE id = %s AND merchant_id = %s
                      AND owner_intent_id = %s AND reservation_state = 'active'
                    """,
                    (
                        owner_type,
                        plan.gross_micros,
                        plan.financial.balance_source_hash,
                        int(current["fund_reservation_id"]),
                        plan.merchant_id,
                        intent_id,
                    ),
                )
                if int(getattr(cursor, "rowcount", 0) or 0) != 1:
                    raise CashoutError("cashout_resume_reservation_update_failed")
                if plan.method == "callback":
                    cursor.execute(
                        """
                        INSERT INTO a6_trade.a6_callback_sweep_intents (
                            withdrawal_intent_id, sg_cloud_sender_ref,
                            receiver_merchant_id, receiver_model,
                            receiver_listing_id, max_spend_micros
                        ) VALUES (%s, %s, %s, %s, %s, %s)
                        ON CONFLICT (withdrawal_intent_id) DO UPDATE SET
                            sg_cloud_sender_ref = EXCLUDED.sg_cloud_sender_ref,
                            receiver_merchant_id = EXCLUDED.receiver_merchant_id,
                            receiver_model = EXCLUDED.receiver_model,
                            receiver_listing_id = EXCLUDED.receiver_listing_id,
                            max_spend_micros = EXCLUDED.max_spend_micros,
                            accounting_state = 'planned', updated_at = now()
                        """,
                        (
                            intent_id,
                            plan.callback_sender_ref,
                            plan.callback_receiver_merchant_id,
                            plan.callback_receiver_model,
                            plan.callback_receiver_listing_id,
                            plan.gross_micros,
                        ),
                    )
            connection.commit()
            return _intent_record(resumed)
        except Exception:
            connection.rollback()
            raise
        finally:
            _close(connection)

    def begin_submit(self, intent_id: int) -> CashoutIntentRecord:
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE a6_trade.a6_withdrawal_intents
                    SET intent_state = 'SUBMITTED', submit_attempted_at = now(),
                        updated_at = now()
                    WHERE id = %s AND intent_state IN ('PLANNED', 'PREVIEWED')
                    RETURNING id, merchant_id, method, intent_state, gross_micros,
                              fund_reservation_id, idempotency_key,
                              remote_request_id, remote_withdrawal_id
                    """,
                    (intent_id,),
                )
                row = _row(cursor)
                if row is None:
                    raise CashoutError("cashout_intent_not_submittable")
            connection.commit()
            return _intent_record(row)
        except Exception:
            connection.rollback()
            raise
        finally:
            _close(connection)

    def record_submit_result(
        self,
        intent_id: int,
        *,
        accepted: bool,
        remote_request_id: str | None,
        remote_withdrawal_id: str | None = None,
        confirmation_deadline_at: datetime | None = None,
    ) -> None:
        certain = accepted and bool(
            str(remote_request_id or "").strip() or str(remote_withdrawal_id or "").strip()
        )
        state = "PENDING" if certain else "UNKNOWN"
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE a6_trade.a6_withdrawal_intents
                    SET intent_state = %s, remote_request_id = %s,
                        remote_withdrawal_id = %s,
                        submitted_at = CASE WHEN %s THEN now() ELSE submitted_at END,
                        confirmation_deadline_at = %s, updated_at = now()
                    WHERE id = %s AND intent_state = 'SUBMITTED'
                    """,
                    (
                        state,
                        remote_request_id,
                        remote_withdrawal_id,
                        certain,
                        confirmation_deadline_at,
                        intent_id,
                    ),
                )
                if int(getattr(cursor, "rowcount", 0) or 0) != 1:
                    raise CashoutError("cashout_submit_result_state_conflict")
                cursor.execute(
                    """
                    UPDATE a6_trade.a6_callback_sweep_intents callback
                    SET callback_request_id = coalesce(%s, callback_request_id),
                        accounting_state = CASE
                            WHEN %s THEN 'submitted'
                            ELSE 'unknown'
                        END,
                        updated_at = now()
                    FROM a6_trade.a6_withdrawal_intents intent
                    WHERE callback.withdrawal_intent_id = intent.id
                      AND intent.id = %s
                      AND intent.method = 'callback'
                    """,
                    (remote_request_id, certain, intent_id),
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            _close(connection)

    def claim_reconciliation(
        self,
        worker_id: str,
        *,
        lease_seconds: int = 60,
        method: str | None = None,
        retry_interval_seconds: int = 300,
    ) -> CashoutIntentRecord | None:
        owner = str(worker_id or "").strip()
        requested_method = str(method or "").strip() or None
        if (
            not owner
            or isinstance(lease_seconds, bool)
            or lease_seconds < 1
            or lease_seconds > 3600
            or requested_method not in {None, "crypto", "callback"}
            or isinstance(retry_interval_seconds, bool)
            or retry_interval_seconds < 0
            or retry_interval_seconds > 86_400
        ):
            raise CashoutError("cashout_reconcile_lease_invalid")
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT id
                    FROM a6_trade.a6_withdrawal_intents
                    WHERE (
                        (
                            intent_state IN ('SUBMITTED', 'PENDING', 'UNKNOWN')
                            AND (
                                last_reconciled_at IS NULL
                                OR last_reconciled_at <= now()
                                   - make_interval(secs => %s)
                            )
                        )
                        OR (
                            intent_state = 'RECONCILING'
                            AND reconcile_lease_until < now()
                        )
                    )
                      AND (%s::text IS NULL OR method = %s::text)
                      AND (
                          reconcile_lease_until IS NULL
                          OR reconcile_lease_until < now()
                      )
                    ORDER BY
                        CASE WHEN intent_state = 'RECONCILING' THEN 0 ELSE 1 END,
                        coalesce(last_reconciled_at, submit_attempted_at, updated_at),
                        id
                    FOR UPDATE SKIP LOCKED
                    LIMIT 1
                    """,
                    (
                        retry_interval_seconds,
                        requested_method,
                        requested_method,
                    ),
                )
                selected = _row(cursor)
                if selected is None:
                    connection.commit()
                    return None
                cursor.execute(
                    """
                    UPDATE a6_trade.a6_withdrawal_intents
                    SET intent_state = 'RECONCILING', reconcile_owner = %s,
                        reconcile_lease_until = now() + make_interval(secs => %s),
                        last_reconciled_at = now(), updated_at = now()
                    WHERE id = %s
                    RETURNING id, merchant_id, method, intent_state, gross_micros,
                              fund_reservation_id, idempotency_key,
                              remote_request_id, remote_withdrawal_id
                    """,
                    (owner, lease_seconds, int(selected["id"])),
                )
                row = _row(cursor)
                if row is None:
                    raise CashoutError("cashout_reconcile_claim_failed")
            connection.commit()
            return _intent_record(row)
        except Exception:
            connection.rollback()
            raise
        finally:
            _close(connection)

    def load_reconciliation_context(
        self,
        intent_id: int,
    ) -> CashoutReconciliationContext:
        if isinstance(intent_id, bool) or intent_id < 1:
            raise CashoutError("cashout_reconciliation_context_invalid")
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT intent.id, intent.merchant_id, intent.method,
                           intent.intent_state, intent.gross_micros,
                           intent.fund_reservation_id, intent.idempotency_key,
                           intent.remote_request_id, intent.remote_withdrawal_id,
                           intent.remote_withdraw_account_id,
                           intent.expected_fee_micros, intent.expected_net_micros,
                           intent.balance_source_hash, intent.submit_attempted_at,
                           intent.submitted_at, intent.confirmation_deadline_at,
                           merchant.a6_user_id, merchant.session_ref,
                           merchant.quota_per_unit,
                           merchant.status AS merchant_status,
                           coalesce(
                               baseline.withdrawable_micros,
                               intent.cashout_eligible_micros
                                   + intent.provider_reserve_micros
                           )::bigint AS baseline_withdrawable_micros,
                           baseline.withdrawing_micros
                               AS baseline_withdrawing_micros,
                           baseline.paid_micros AS baseline_paid_micros,
                           coalesce(baseline.observed_at, intent.created_at)
                               AS baseline_observed_at
                    FROM a6_trade.a6_withdrawal_intents intent
                    JOIN a6_trade.a6_merchants merchant
                      ON merchant.id = intent.merchant_id
                    LEFT JOIN LATERAL (
                        SELECT facts.withdrawable_micros,
                               facts.withdrawing_micros, facts.paid_micros,
                               facts.observed_at
                        FROM (
                            SELECT current.withdrawable_micros,
                                   current.withdrawing_micros,
                                   current.paid_micros, current.observed_at
                            FROM a6_trade.a6_financial_current current
                            WHERE current.merchant_id = intent.merchant_id
                              AND current.source_hash = intent.balance_source_hash
                            UNION ALL
                            SELECT snapshot.withdrawable_micros,
                                   snapshot.withdrawing_micros,
                                   snapshot.paid_micros, snapshot.observed_at
                            FROM a6_trade.a6_financial_snapshots snapshot
                            WHERE snapshot.merchant_id = intent.merchant_id
                              AND snapshot.source_hash = intent.balance_source_hash
                        ) facts
                        ORDER BY facts.observed_at DESC
                        LIMIT 1
                    ) baseline ON true
                    WHERE intent.id = %s AND intent.method = 'crypto'
                    """,
                    (intent_id,),
                )
                row = _row(cursor)
        finally:
            _close(connection)
        if row is None:
            raise CashoutError("cashout_reconciliation_context_missing")
        if str(row["intent_state"]) != "RECONCILING":
            raise CashoutError("cashout_reconciliation_context_not_claimed")
        submit_attempted_at = row.get("submit_attempted_at")
        account_id = row.get("remote_withdraw_account_id")
        if submit_attempted_at is None or account_id is None:
            raise CashoutError("cashout_reconciliation_context_invalid")
        merchant = CashoutMerchantContext(
            merchant_id=int(row["merchant_id"]),
            a6_user_id=int(row["a6_user_id"]),
            session_ref=str(row["session_ref"]),
            quota_per_unit=int(row["quota_per_unit"]),
            merchant_status=str(row["merchant_status"]),
        )
        return CashoutReconciliationContext(
            intent=_intent_record(row),
            merchant=merchant,
            remote_withdraw_account_id=int(account_id),
            expected_fee_micros=int(row["expected_fee_micros"]),
            expected_net_micros=int(row["expected_net_micros"]),
            balance_source_hash=str(row["balance_source_hash"]),
            submit_attempted_at=submit_attempted_at,
            submitted_at=row.get("submitted_at"),
            confirmation_deadline_at=row.get("confirmation_deadline_at"),
            baseline_withdrawable_micros=int(row["baseline_withdrawable_micros"]),
            baseline_withdrawing_micros=(
                int(row["baseline_withdrawing_micros"])
                if row.get("baseline_withdrawing_micros") is not None
                else None
            ),
            baseline_paid_micros=(
                int(row["baseline_paid_micros"])
                if row.get("baseline_paid_micros") is not None
                else None
            ),
            baseline_observed_at=row["baseline_observed_at"],
        )

    def complete_reconciliation(
        self,
        intent_id: int,
        worker_id: str,
        result: CashoutReconciliation,
    ) -> None:
        owner = str(worker_id or "").strip()
        if not owner:
            raise CashoutError("cashout_reconcile_owner_invalid")
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT *, reconcile_lease_until > now() AS reconcile_lease_valid
                    FROM a6_trade.a6_withdrawal_intents
                    WHERE id = %s FOR UPDATE
                    """,
                    (intent_id,),
                )
                intent = _row(cursor)
                if intent is None:
                    raise CashoutError("cashout_intent_missing")
                if str(intent["intent_state"]) in {"CONFIRMED", "FAILED_FINAL"}:
                    connection.commit()
                    return
                if (
                    str(intent["intent_state"]) != "RECONCILING"
                    or str(intent.get("reconcile_owner") or "") != owner
                    or intent.get("reconcile_lease_until") is None
                    or not bool(intent.get("reconcile_lease_valid"))
                ):
                    raise CashoutError("cashout_reconcile_claim_lost")
                merchant_id = int(intent["merchant_id"])
                cursor.execute(
                    "SELECT pg_advisory_xact_lock(%s, %s)",
                    (FUND_LOCK_NAMESPACE, merchant_id),
                )
                self._write_confirmation(cursor, intent_id, result)
                if result.outcome == "confirmed":
                    self._confirm_intent(cursor, intent, result)
                elif result.outcome == "failed_final":
                    self._fail_final(cursor, intent, result)
                else:
                    next_state = "PENDING" if result.outcome == "pending" else "UNKNOWN"
                    cursor.execute(
                        """
                        UPDATE a6_trade.a6_withdrawal_intents
                        SET intent_state = %s, reconcile_owner = NULL,
                            reconcile_lease_until = NULL,
                            remote_withdrawal_id = coalesce(%s, remote_withdrawal_id),
                            chain_txid = coalesce(%s, chain_txid),
                            updated_at = now()
                        WHERE id = %s
                        """,
                        (
                            next_state,
                            result.remote_withdrawal_id,
                            result.chain_txid,
                            intent_id,
                        ),
                    )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            _close(connection)

    def _write_cycle_and_risk(
        self,
        cursor: Any,
        plan: CashoutPlan,
    ) -> tuple[int, int]:
        cursor.execute(
            """
            SELECT id, decision_epoch, config_version, risk_tier,
                   standard_target_micros, target_jitter_micros,
                   preview_contract_hash
            FROM a6_trade.a6_cashout_decision_cycles
            WHERE merchant_id = %s AND cycle_state = 'active'
            FOR UPDATE
            """,
            (plan.merchant_id,),
        )
        active = _row(cursor)
        cycle = plan.cycle
        matches = active is not None and (
            int(active["decision_epoch"]) == cycle.decision_epoch
            and str(active["config_version"]) == cycle.config_version
            and str(active["risk_tier"]) == cycle.risk_tier
            and int(active["standard_target_micros"]) == cycle.standard_target_micros
            and int(active["target_jitter_micros"]) == cycle.target_jitter_micros
            and str(active["preview_contract_hash"]) == cycle.preview_contract_hash
        )
        if active is not None and not matches:
            cursor.execute(
                """
                UPDATE a6_trade.a6_cashout_decision_cycles
                SET cycle_state = 'expired', closed_at = now(),
                    close_reason = 'decision_contract_changed', updated_at = now()
                WHERE id = %s
                """,
                (int(active["id"]),),
            )
        if matches:
            cycle_id = int(active["id"])
        else:
            cursor.execute(
                """
                INSERT INTO a6_trade.a6_cashout_decision_cycles (
                    merchant_id, decision_epoch, config_version, risk_tier,
                    hard_risk, standard_target_micros, target_jitter_micros,
                    balance_source_hash, preview_contract_hash, cycle_state
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'active')
                ON CONFLICT (merchant_id, decision_epoch, config_version)
                DO NOTHING
                RETURNING id
                """,
                (
                    plan.merchant_id,
                    cycle.decision_epoch,
                    cycle.config_version,
                    cycle.risk_tier,
                    plan.hard_risk,
                    cycle.standard_target_micros,
                    cycle.target_jitter_micros,
                    cycle.balance_source_hash,
                    cycle.preview_contract_hash,
                ),
            )
            row = _row(cursor)
            if row is None:
                raise CashoutError("cashout_cycle_epoch_reused")
            cycle_id = int(row["id"])
        evidence = plan.risk.public()
        evidence_hash = _sha256(evidence)
        cursor.execute(
            """
            INSERT INTO a6_trade.a6_cashout_risk_snapshots (
                decision_cycle_id, merchant_id, delta_effective,
                merchant_age_hours, hours_since_last_confirmed_cashout,
                cashout_eligible_micros, earn_rate_p25_micros_per_hour,
                session_refresh_failures, withdraw_preview_failures,
                risk_probability, risk_horizon_hours, risk_model_version,
                training_window_days, sample_count, evidence, evidence_hash,
                observed_at
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s::jsonb, %s, %s
            ) RETURNING id
            """,
            (
                cycle_id,
                plan.merchant_id,
                plan.risk.delta_effective,
                plan.risk.merchant_age_hours,
                plan.risk.hours_since_last_confirmed_cashout,
                plan.cashout_eligible_micros,
                plan.risk.earn_rate_p25_micros_per_hour,
                plan.risk.session_refresh_failures,
                plan.risk.withdraw_preview_failures,
                plan.risk_probability,
                plan.risk_horizon_hours,
                plan.risk.risk_model_version,
                plan.risk.training_window_days,
                plan.risk.sample_count,
                _json(evidence),
                evidence_hash,
                plan.financial.observed_at,
            ),
        )
        risk = _row(cursor)
        if risk is None:
            raise CashoutError("cashout_risk_snapshot_write_failed")
        return cycle_id, int(risk["id"])

    def _validate_funds_snapshot(self, cursor: Any, plan: CashoutPlan) -> None:
        cursor.execute(
            """
            SELECT withdrawable_micros, source_hash, observed_at,
                   observed_at >= now() - make_interval(secs => %s)
                       AS snapshot_fresh
            FROM a6_trade.a6_financial_current
            WHERE merchant_id = %s
            FOR UPDATE
            """,
            (self.financial_snapshot_ttl_seconds, plan.merchant_id),
        )
        financial = _row(cursor)
        if financial is None or not bool(financial.get("snapshot_fresh")):
            raise CashoutError("cashout_financial_snapshot_stale")
        if (
            int(financial["withdrawable_micros"]) != plan.financial.withdrawable_micros
            or str(financial["source_hash"]).strip() != plan.financial.balance_source_hash
            or financial["observed_at"] != plan.financial.observed_at
        ):
            raise CashoutError("cashout_financial_snapshot_changed")

        cursor.execute(
            """
            SELECT count(inventory.*)::bigint AS provider_count,
                   coalesce(sum(reserve.reserved_micros), 0)::bigint
                       AS provider_reserve_micros,
                   bool_and(
                       reserve.merchant_id IS NOT NULL
                       AND reserve.observed_at >=
                           now() - make_interval(secs => %s)
                   ) AS reserve_fresh
            FROM a6_trade.provider_inventory inventory
            LEFT JOIN a6_trade.provider_fund_reserves reserve
              ON reserve.model = inventory.model
             AND reserve.merchant_id = inventory.merchant_id
            WHERE inventory.merchant_id = %s
              AND inventory.inventory_state <> 'RETIRED'
            """,
            (self.financial_snapshot_ttl_seconds, plan.merchant_id),
        )
        provider = _row(cursor)
        if provider is None:
            raise CashoutError("cashout_provider_reserve_read_failed")
        provider_count = int(provider["provider_count"])
        provider_reserve = int(provider["provider_reserve_micros"])
        if provider_count > 0 and not bool(provider.get("reserve_fresh")):
            raise CashoutError("cashout_provider_reserve_stale")
        if provider_reserve != plan.provider_reserve_micros:
            raise CashoutError("cashout_provider_reserve_changed")
        if plan.financial.local_reserved_other_micros != 0:
            raise CashoutError("cashout_funds_already_reserved")
        current_eligible = max(
            0,
            int(financial["withdrawable_micros"]) - provider_reserve,
        )
        if plan.gross_micros > current_eligible or plan.cashout_eligible_micros != current_eligible:
            raise CashoutError("cashout_eligible_balance_changed")

    def _intent_by_key(
        self,
        cursor: Any,
        idempotency_key: str,
    ) -> CashoutIntentRecord | None:
        cursor.execute(
            """
            SELECT id, merchant_id, method, intent_state, gross_micros,
                   fund_reservation_id, idempotency_key,
                   remote_request_id, remote_withdrawal_id
            FROM a6_trade.a6_withdrawal_intents
            WHERE idempotency_key = %s
            """,
            (idempotency_key,),
        )
        row = _row(cursor)
        return _intent_record(row) if row is not None else None

    def _write_confirmation(
        self,
        cursor: Any,
        intent_id: int,
        result: CashoutReconciliation,
    ) -> None:
        cursor.execute(
            """
            INSERT INTO a6_trade.a6_withdrawal_confirmations (
                withdrawal_intent_id, confirmation_type,
                remote_withdrawal_id, chain_txid, observed_gross_micros,
                observed_fee_micros, observed_net_micros, remote_status,
                source_hash, evidence, observed_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s)
            ON CONFLICT (withdrawal_intent_id, confirmation_type, source_hash)
            DO NOTHING
            """,
            (
                intent_id,
                result.confirmation_type,
                result.remote_withdrawal_id,
                result.chain_txid,
                result.observed_gross_micros,
                result.observed_fee_micros,
                result.observed_net_micros,
                result.remote_status,
                result.source_hash,
                _json(dict(result.evidence)),
                result.observed_at,
            ),
        )

    def _confirm_intent(
        self,
        cursor: Any,
        intent: Mapping[str, Any],
        result: CashoutReconciliation,
    ) -> None:
        intent_id = int(intent["id"])
        merchant_id = int(intent["merchant_id"])
        gross = int(result.observed_gross_micros or intent["gross_micros"])
        fee = int(result.observed_fee_micros or 0)
        net = int(result.observed_net_micros or max(0, gross - fee))
        method = str(intent["method"])
        receiver_merchant_id: int | None = None
        if gross != int(intent["gross_micros"]):
            raise CashoutError("cashout_confirmed_gross_mismatch")
        if method == "callback":
            sender = result.sender_debit_micros
            callback_fee = result.callback_fee_micros
            receiver = result.receiver_credit_micros
            if sender is None or callback_fee is None or receiver is None:
                raise CashoutError("cashout_callback_legs_incomplete")
            if abs(sender - callback_fee - receiver) > 1:
                raise CashoutError("cashout_callback_legs_unbalanced")
            gross, fee, net = sender, callback_fee, receiver
            cursor.execute(
                """
                SELECT receiver_merchant_id
                FROM a6_trade.a6_callback_sweep_intents
                WHERE withdrawal_intent_id = %s
                FOR UPDATE
                """,
                (intent_id,),
            )
            callback = _row(cursor)
            if callback is None:
                raise CashoutError("cashout_callback_detail_missing")
            receiver_merchant_id = int(callback["receiver_merchant_id"])
            cursor.execute(
                """
                UPDATE a6_trade.a6_callback_sweep_intents
                SET sender_debit_micros = %s, callback_fee_micros = %s,
                    receiver_credit_micros = %s, accounting_state = 'confirmed',
                    updated_at = now()
                WHERE withdrawal_intent_id = %s
                """,
                (sender, callback_fee, receiver, intent_id),
            )
            if int(getattr(cursor, "rowcount", 0) or 0) != 1:
                raise CashoutError("cashout_callback_detail_update_failed")
        cursor.execute(
            """
            UPDATE a6_trade.a6_withdrawal_intents
            SET intent_state = 'CONFIRMED', actual_fee_micros = %s,
                actual_net_micros = %s,
                remote_withdrawal_id = coalesce(%s, remote_withdrawal_id),
                chain_txid = coalesce(%s, chain_txid), confirmed_at = %s,
                reconcile_owner = NULL, reconcile_lease_until = NULL,
                updated_at = now()
            WHERE id = %s
            """,
            (
                fee,
                net,
                result.remote_withdrawal_id,
                result.chain_txid,
                result.observed_at,
                intent_id,
            ),
        )
        self._release_reservation(cursor, intent)
        legs: tuple[tuple[str, int, int], ...] = (
            (
                ("crypto_withdraw_gross", gross, merchant_id),
                ("crypto_withdraw_fee", fee, merchant_id),
                ("crypto_withdraw_net", net, merchant_id),
            )
            if method == "crypto"
            else (
                ("callback_sweep_gross", gross, merchant_id),
                ("callback_sweep_fee", fee, merchant_id),
                (
                    "callback_receiver_credit",
                    net,
                    int(receiver_merchant_id or 0),
                ),
            )
        )
        for leg_type, amount, leg_merchant_id in legs:
            if leg_merchant_id < 1:
                raise CashoutError("cashout_ledger_merchant_invalid")
            cursor.execute(
                """
                INSERT INTO a6_trade.a6_cashout_ledger_entries (
                    withdrawal_intent_id, merchant_id, leg_type,
                    amount_micros, source_hash, occurred_at
                ) VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (withdrawal_intent_id, leg_type, source_hash)
                DO NOTHING
                """,
                (
                    intent_id,
                    leg_merchant_id,
                    leg_type,
                    amount,
                    result.source_hash,
                    result.observed_at,
                ),
            )
        cursor.execute(
            """
            INSERT INTO a6_trade.merchant_cashout_positions (
                merchant_id, source_funds_accrued_micros,
                confirmed_source_debit_micros, confirmed_crypto_net_micros,
                skipped_rounds, source_hash, observed_at,
                last_confirmed_cashout_at, last_confirmed_callback_at
            ) VALUES (%s, 0, %s, %s, 0, %s, %s, %s, %s)
            ON CONFLICT (merchant_id) DO UPDATE SET
                confirmed_source_debit_micros =
                    a6_trade.merchant_cashout_positions.confirmed_source_debit_micros
                    + EXCLUDED.confirmed_source_debit_micros,
                confirmed_crypto_net_micros =
                    a6_trade.merchant_cashout_positions.confirmed_crypto_net_micros
                    + EXCLUDED.confirmed_crypto_net_micros,
                skipped_rounds = 0,
                source_hash = EXCLUDED.source_hash,
                observed_at = EXCLUDED.observed_at,
                last_confirmed_cashout_at = EXCLUDED.last_confirmed_cashout_at,
                last_confirmed_callback_at = coalesce(
                    EXCLUDED.last_confirmed_callback_at,
                    a6_trade.merchant_cashout_positions.last_confirmed_callback_at
                ),
                updated_at = now()
            """,
            (
                merchant_id,
                gross,
                net if method == "crypto" else 0,
                result.source_hash,
                result.observed_at,
                result.observed_at,
                result.observed_at if method == "callback" else None,
            ),
        )
        if receiver_merchant_id is not None:
            cursor.execute(
                """
                INSERT INTO a6_trade.merchant_cashout_positions (
                    merchant_id, source_funds_accrued_micros,
                    confirmed_source_debit_micros, confirmed_crypto_net_micros,
                    rolling_7d_callback_receiver_credit_micros, skipped_rounds,
                    source_hash, observed_at
                )
                SELECT %s, 0, 0, 0,
                       coalesce(sum(ledger.amount_micros), 0)::bigint,
                       0, %s, %s
                FROM a6_trade.a6_cashout_ledger_entries ledger
                WHERE ledger.merchant_id = %s
                  AND ledger.leg_type = 'callback_receiver_credit'
                  AND ledger.occurred_at >= %s - interval '7 days'
                ON CONFLICT (merchant_id) DO UPDATE SET
                    rolling_7d_callback_receiver_credit_micros =
                        EXCLUDED.rolling_7d_callback_receiver_credit_micros,
                    source_hash = EXCLUDED.source_hash,
                    observed_at = EXCLUDED.observed_at,
                    updated_at = now()
                """,
                (
                    receiver_merchant_id,
                    result.source_hash,
                    result.observed_at,
                    receiver_merchant_id,
                    result.observed_at,
                ),
            )
        cursor.execute(
            """
            UPDATE a6_trade.a6_cashout_decision_cycles
            SET cycle_state = 'closed', closed_at = %s,
                close_reason = 'cashout_confirmed', updated_at = now()
            WHERE id = %s AND cycle_state = 'active'
            """,
            (result.observed_at, int(intent["decision_cycle_id"])),
        )

    def _fail_final(
        self,
        cursor: Any,
        intent: Mapping[str, Any],
        result: CashoutReconciliation,
    ) -> None:
        if not result.authoritative_no_effect:
            raise CashoutError("cashout_authoritative_no_effect_required")
        cursor.execute(
            """
            UPDATE a6_trade.a6_withdrawal_intents
            SET intent_state = 'FAILED_FINAL', failure_class = %s,
                authoritative_no_effect_at = %s, reconcile_owner = NULL,
                reconcile_lease_until = NULL, updated_at = now()
            WHERE id = %s
            """,
            (
                result.failure_class or "authoritative_no_effect",
                result.observed_at,
                int(intent["id"]),
            ),
        )
        self._release_reservation(cursor, intent)
        cursor.execute(
            """
            UPDATE a6_trade.a6_cashout_decision_cycles
            SET cycle_state = 'closed', closed_at = %s,
                close_reason = 'authoritative_no_effect', updated_at = now()
            WHERE id = %s AND cycle_state = 'active'
            """,
            (result.observed_at, int(intent["decision_cycle_id"])),
        )

    @staticmethod
    def _release_reservation(cursor: Any, intent: Mapping[str, Any]) -> None:
        reservation_id = intent.get("fund_reservation_id")
        if reservation_id is None:
            raise CashoutError("cashout_reservation_missing")
        cursor.execute(
            """
            UPDATE a6_trade.merchant_fund_reservations
            SET reservation_state = 'released', released_at = now(),
                updated_at = now()
            WHERE id = %s AND merchant_id = %s
              AND owner_intent_id = %s AND reservation_state = 'active'
              AND owner_type IN ('crypto_cashout', 'callback_cashout')
            """,
            (
                int(reservation_id),
                int(intent["merchant_id"]),
                int(intent["id"]),
            ),
        )
        if int(getattr(cursor, "rowcount", 0) or 0) != 1:
            raise CashoutError("cashout_reservation_release_failed")


class CashoutReconciliationWorker:
    def __init__(
        self,
        store: PostgresCashoutStore,
        reconciler: A6CryptoCashoutReconciler,
        *,
        worker_id: str,
        lease_seconds: int = 60,
        retry_interval_seconds: int = 300,
    ) -> None:
        owner = str(worker_id or "").strip()
        if (
            not owner
            or isinstance(lease_seconds, bool)
            or not 1 <= lease_seconds <= 3600
            or isinstance(retry_interval_seconds, bool)
            or not 0 <= retry_interval_seconds <= 86_400
        ):
            raise CashoutError("cashout_reconcile_worker_invalid")
        self.store = store
        self.reconciler = reconciler
        self.worker_id = owner
        self.lease_seconds = lease_seconds
        self.retry_interval_seconds = retry_interval_seconds

    def run_once(self, *, max_intents: int = 100) -> dict[str, Any]:
        if isinstance(max_intents, bool) or not 1 <= max_intents <= 10_000:
            raise CashoutError("cashout_reconcile_limit_invalid")
        counts = {
            "confirmed": 0,
            "pending": 0,
            "unknown": 0,
            "failed_final": 0,
        }
        claimed = 0
        for _ in range(max_intents):
            intent = self.store.claim_reconciliation(
                self.worker_id,
                lease_seconds=self.lease_seconds,
                method="crypto",
                retry_interval_seconds=self.retry_interval_seconds,
            )
            if intent is None:
                break
            claimed += 1
            context = self.store.load_reconciliation_context(intent.intent_id)
            result = self.reconciler.reconcile(context)
            self.store.complete_reconciliation(
                intent.intent_id,
                self.worker_id,
                result,
            )
            counts[result.outcome] += 1
        return {
            "success": True,
            "claimed_count": claimed,
            "confirmed_count": counts["confirmed"],
            "pending_count": counts["pending"],
            "unknown_count": counts["unknown"],
            "failed_final_count": counts["failed_final"],
            "remote_write_count": 0,
        }


class CashoutOrchestrator:
    """Generate plans and persist evidence; remote execution is independently gated."""

    def __init__(
        self,
        planner: CashoutPlanner,
        store: PostgresCashoutStore | None = None,
        executor: CashoutApplyExecutor | None = None,
        *,
        enabled: bool = False,
        plan_only: bool = True,
    ) -> None:
        self.planner = planner
        self.store = store
        self.executor = executor
        self.enabled = enabled
        self.plan_only = plan_only

    def run_once(self, sources: Sequence[CashoutSource]) -> dict[str, Any]:
        if not self.enabled:
            return {
                "success": True,
                "enabled": False,
                "mode": "disabled",
                "remote_write_count": 0,
                "plans": [],
            }
        plans = self.planner.plan_all(sources)
        journaled = 0
        executions: list[dict[str, Any]] = []
        remote_write_count = 0
        if self.plan_only and self.store is not None:
            for plan in plans:
                self.store.record_plan(plan)
                journaled += 1
        if not self.plan_only:
            policy = self.planner.policy
            if not (policy.standard_withdraw_apply_enabled or policy.callback_sweep_apply_enabled):
                raise CashoutError("cashout_apply_methods_disabled")
            if self.store is None or self.executor is None:
                raise CashoutError("cashout_apply_executor_missing")
            method_counts = {"crypto": 0, "callback": 0}
            method_limits = {
                "crypto": policy.max_parallel_standard,
                "callback": policy.max_parallel_callback,
            }
            for plan in plans:
                method_enabled = (
                    plan.method == "crypto" and policy.standard_withdraw_apply_enabled
                ) or (plan.method == "callback" and policy.callback_sweep_apply_enabled)
                at_capacity = bool(
                    plan.method in method_counts
                    and method_counts[str(plan.method)] >= method_limits[str(plan.method)]
                )
                if not plan.executable or not method_enabled or at_capacity:
                    self.store.record_plan(plan)
                    journaled += 1
                    if plan.executable:
                        executions.append(
                            {
                                "merchant_id": plan.merchant_id,
                                "method": plan.method,
                                "state": "DEFERRED",
                                "decision": (
                                    "method_disabled" if not method_enabled else "round_capacity"
                                ),
                                "remote_write_count": 0,
                            }
                        )
                    continue
                result = self.executor.execute(plan)
                executions.append(result)
                journaled += 1
                method_counts[str(plan.method)] += 1
                remote_write_count += int(result.get("remote_write_count") or 0)
        return {
            "success": True,
            "enabled": True,
            "mode": "plan-only" if self.plan_only else "apply-gated",
            "candidate_count": len(plans),
            "journaled_count": journaled,
            "executable_count": sum(plan.executable for plan in plans),
            "remote_write_count": remote_write_count,
            "total_risk_exposure_micros": sum(plan.risk_exposure_micros for plan in plans),
            "executions": executions,
            "plans": [plan.public() for plan in plans],
        }


_WALLET_PRINCIPAL_CANDIDATES_SQL = """
SELECT principal.id, principal.inventory_principal_key,
       coalesce(sum(ledger.amount_micros) FILTER (
           WHERE ledger.occurred_at >= now() - interval '30 days'
       ), 0)::bigint AS received_micros
FROM a6_trade.treasury_wallet_principals principal
LEFT JOIN a6_trade.treasury_wallet_routes route
  ON route.wallet_principal_id = principal.id
LEFT JOIN a6_trade.a6_withdrawal_intents intent
  ON intent.wallet_route_id = route.id AND intent.method = 'crypto'
LEFT JOIN a6_trade.a6_cashout_ledger_entries ledger
  ON ledger.withdrawal_intent_id = intent.id
 AND ledger.leg_type = 'crypto_withdraw_net'
WHERE principal.enabled
  AND principal.principal_state = 'enabled'
GROUP BY principal.id, principal.inventory_principal_key
ORDER BY principal.id
"""


_CASHOUT_CANDIDATES_SQL = """
WITH provider_state AS (
    SELECT inventory.merchant_id,
           coalesce(sum(reserve.reserved_micros), 0)::bigint AS provider_reserve_micros,
           bool_and(
               reserve.merchant_id IS NOT NULL
               AND reserve.observed_at >= %(financial_cutoff)s
           ) AS reserve_fresh,
           CASE
               WHEN bool_or(
                   inventory.inventory_state = 'READY'
                   OR EXISTS (
                       SELECT 1
                       FROM a6_trade.provider_pool_slots slot
                       WHERE slot.model = inventory.model
                         AND slot.enodot_account_id = inventory.enodot_account_id
                         AND slot.a6_token_id = inventory.a6_token_id
                         AND slot.valid_to IS NULL
                         AND slot.slot_state <> 'VACANT'
                   )
               ) THEN 'READY'
               WHEN bool_or(inventory.inventory_state = 'WARM_STANDBY') THEN 'WARM_STANDBY'
               ELSE 'NONE'
           END AS provider_duty_state,
           NOT bool_or(
               inventory.inventory_state IN ('READY', 'WARM_STANDBY')
               OR EXISTS (
                   SELECT 1
                   FROM a6_trade.provider_pool_slots slot
                   WHERE slot.model = inventory.model
                     AND slot.enodot_account_id = inventory.enodot_account_id
                     AND slot.a6_token_id = inventory.a6_token_id
                     AND slot.valid_to IS NULL
                     AND slot.slot_state <> 'VACANT'
               )
           ) AS provider_drain_confirmed
    FROM a6_trade.provider_inventory inventory
    LEFT JOIN a6_trade.provider_fund_reserves reserve
      ON reserve.model = inventory.model
     AND reserve.merchant_id = inventory.merchant_id
    WHERE inventory.inventory_state <> 'RETIRED'
    GROUP BY inventory.merchant_id
), resumable_intents AS (
    SELECT intent.id, intent.merchant_id
    FROM a6_trade.a6_withdrawal_intents intent
    JOIN a6_trade.merchant_fund_reservations reservation
      ON reservation.id = intent.fund_reservation_id
     AND reservation.reservation_state = 'active'
     AND reservation.owner_intent_id = intent.id
     AND reservation.owner_type IN ('crypto_cashout', 'callback_cashout')
    WHERE intent.intent_state = 'AWAITING_PROVIDER_DRAIN'
), active_claims AS (
    SELECT reservation.merchant_id,
           sum(reservation.reserved_micros)::bigint AS reserved_micros,
           count(*)::bigint AS claim_count
    FROM a6_trade.merchant_fund_reservations reservation
    WHERE reservation.reservation_state = 'active'
      AND NOT EXISTS (
          SELECT 1
          FROM resumable_intents resumable
          WHERE resumable.id = reservation.owner_intent_id
            AND resumable.merchant_id = reservation.merchant_id
            AND reservation.owner_type IN ('crypto_cashout', 'callback_cashout')
      )
    GROUP BY reservation.merchant_id
), external_funds AS (
    SELECT event.merchant_id,
           coalesce(sum(event.earned_micros), 0)::bigint AS earned_micros,
           min(event.occurred_at) AS first_earned_at
    FROM a6_trade.a6_earning_events event
    WHERE event.counterparty_class = 'external'
    GROUP BY event.merchant_id
), callback_credits AS (
    SELECT ledger.merchant_id,
           coalesce(sum(ledger.amount_micros), 0)::bigint AS credited_micros,
           min(ledger.occurred_at) AS first_credited_at
    FROM a6_trade.a6_cashout_ledger_entries ledger
    WHERE ledger.leg_type = 'callback_receiver_credit'
    GROUP BY ledger.merchant_id
), confirmed_debits AS (
    SELECT ledger.merchant_id,
           coalesce(sum(ledger.amount_micros), 0)::bigint AS debited_micros,
           max(ledger.occurred_at) AS last_confirmed_cashout_at
    FROM a6_trade.a6_cashout_ledger_entries ledger
    WHERE ledger.leg_type IN ('crypto_withdraw_gross', 'callback_sweep_gross')
    GROUP BY ledger.merchant_id
), earning_hours AS (
    SELECT event.merchant_id, date_trunc('hour', event.occurred_at) AS bucket,
           sum(event.earned_micros)::bigint AS hourly_micros
    FROM a6_trade.a6_earning_events event
    WHERE event.counterparty_class = 'external'
      AND event.occurred_at >= %(earning_cutoff)s
    GROUP BY event.merchant_id, date_trunc('hour', event.occurred_at)
), earning_rates AS (
    SELECT merchant_id,
           floor(percentile_cont(0.25) WITHIN GROUP (
               ORDER BY hourly_micros
           ))::bigint AS earn_rate_p25_micros_per_hour,
           count(*)::bigint AS sample_count
    FROM earning_hours
    GROUP BY merchant_id
), preview_failures AS (
    SELECT incident.entity_id::bigint AS merchant_id,
           sum(incident.occurrence_count)::bigint AS failure_count
    FROM a6_trade.trade_incidents incident
    WHERE incident.entity_type = 'merchant'
      AND incident.incident_class LIKE 'cashout_withdraw_preview%%'
      AND incident.last_seen_at >= %(earning_cutoff)s
      AND incident.entity_id ~ '^[0-9]+$'
    GROUP BY incident.entity_id::bigint
), active_cycles AS (
    SELECT cycle.*
    FROM a6_trade.a6_cashout_decision_cycles cycle
    WHERE cycle.cycle_state = 'active'
), latest_cycles AS (
    SELECT cycle.merchant_id, max(cycle.decision_epoch) AS latest_epoch
    FROM a6_trade.a6_cashout_decision_cycles cycle
    GROUP BY cycle.merchant_id
)
SELECT merchant.id AS merchant_id,
       merchant.a6_user_id, merchant.session_ref, merchant.quota_per_unit,
       merchant.status AS merchant_status,
       financial.withdrawable_micros,
       financial.source_hash AS balance_source_hash,
       financial.observed_at AS financial_observed_at,
       coalesce(provider.provider_reserve_micros, 0)::bigint
           AS provider_reserve_micros,
       coalesce(claim.reserved_micros, 0)::bigint AS local_reserved_other_micros,
       coalesce(claim.claim_count, 0) > 0 AS funds_claimed,
       coalesce(resource.observed_delta, resource.desired_delta, 0.5)
           AS delta_effective,
       resource.observed_delta,
       greatest(
           0,
           extract(epoch FROM (%(observed_at)s - merchant.created_at)) / 3600
       )::numeric(20, 6) AS merchant_age_hours,
       CASE
           WHEN debit.last_confirmed_cashout_at IS NULL THEN NULL
           ELSE greatest(
               0,
               extract(epoch FROM (
                   %(observed_at)s - debit.last_confirmed_cashout_at
               )) / 3600
           )::numeric(20, 6)
       END AS hours_since_last_confirmed_cashout,
       coalesce(rate.earn_rate_p25_micros_per_hour, 0)::bigint
           AS earn_rate_p25_micros_per_hour,
       greatest(0, merchant.poll_error_count)::integer AS session_refresh_failures,
       coalesce(preview.failure_count, 0)::integer AS withdraw_preview_failures,
       (
           SELECT count(*)::integer
           FROM a6_trade.a6_merchants cohort
           WHERE cohort.status IN ('restricted', 'abandoned')
             AND cohort.updated_at >= %(observed_at)s - interval '24 hours'
       ) AS same_cohort_recent_restrictions,
       merchant.status IN ('restricted', 'abandoned') AS hard_risk,
       merchant.status IN ('restricted', 'abandoned') AS merchant_restricted,
       coalesce(external.earned_micros, 0)
           + coalesce(credit.credited_micros, 0)
           AS source_funds_accrued_micros,
       coalesce(debit.debited_micros, 0)::bigint AS confirmed_source_debit_micros,
       debit.last_confirmed_cashout_at,
       coalesce(position.skipped_rounds, 0)::integer AS skipped_rounds,
       CASE
           WHEN coalesce(external.earned_micros, 0)
                  + coalesce(credit.credited_micros, 0)
                <= coalesce(debit.debited_micros, 0)
           THEN 0
           ELSE greatest(
               0,
               extract(epoch FROM (
                   %(observed_at)s - coalesce(
                       position.oldest_residual_at,
                       debit.last_confirmed_cashout_at,
                       least(external.first_earned_at, credit.first_credited_at),
                       external.first_earned_at,
                       credit.first_credited_at,
                       merchant.created_at
                   )
               )) / 3600
           )
       END::numeric(20, 6) AS oldest_residual_age_hours,
       coalesce(provider.provider_duty_state, 'NONE') AS provider_duty_state,
       coalesce(provider.provider_drain_confirmed, true) AS provider_drain_confirmed,
       coalesce(rate.sample_count, 0)::bigint AS risk_sample_count,
       active.id AS cycle_id,
       active.decision_epoch AS cycle_decision_epoch,
       active.config_version AS cycle_config_version,
       active.risk_tier AS cycle_risk_tier,
       active.standard_target_micros,
       active.target_jitter_micros,
       active.balance_source_hash AS cycle_balance_source_hash,
       active.preview_contract_hash,
       resumable.id AS resumable_intent_id,
       greatest(
           1,
           floor(
               extract(epoch FROM %(observed_at)s) / %(reconcile_interval)s
           )::bigint,
           coalesce(latest.latest_epoch + 1, 1)
       ) AS next_decision_epoch
FROM a6_trade.a6_merchants merchant
JOIN a6_trade.a6_financial_current financial
  ON financial.merchant_id = merchant.id
LEFT JOIN provider_state provider ON provider.merchant_id = merchant.id
LEFT JOIN active_claims claim ON claim.merchant_id = merchant.id
LEFT JOIN a6_trade.enodot_managed_seller_resources resource
  ON resource.merchant_id = merchant.id
LEFT JOIN external_funds external ON external.merchant_id = merchant.id
LEFT JOIN callback_credits credit ON credit.merchant_id = merchant.id
LEFT JOIN confirmed_debits debit ON debit.merchant_id = merchant.id
LEFT JOIN earning_rates rate ON rate.merchant_id = merchant.id
LEFT JOIN preview_failures preview ON preview.merchant_id = merchant.id
LEFT JOIN a6_trade.merchant_cashout_positions position
  ON position.merchant_id = merchant.id
LEFT JOIN active_cycles active ON active.merchant_id = merchant.id
LEFT JOIN latest_cycles latest ON latest.merchant_id = merchant.id
LEFT JOIN resumable_intents resumable ON resumable.merchant_id = merchant.id
WHERE merchant.a6_user_id IS NOT NULL
  AND merchant.supplier_id IS NOT NULL
  AND merchant.status = 'supplier_submitted'
  AND merchant.quota_per_unit > 0
  AND financial.observed_at >= %(financial_cutoff)s
  AND coalesce(financial.withdrawable_micros, 0) > 0
  AND (
      debit.last_confirmed_cashout_at IS NULL
      OR financial.observed_at > debit.last_confirmed_cashout_at
  )
  AND (provider.merchant_id IS NULL OR provider.reserve_fresh)
ORDER BY financial.withdrawable_micros DESC, merchant.id
LIMIT %(limit)s
"""


_CASHOUT_ROUTES_SQL = """
SELECT binding.merchant_id, route.id AS wallet_route_id,
       route.wallet_principal_id, route.network,
       binding.a6_withdraw_account_id,
       binding.remote_account_source_hash,
       route.confirmation_probability,
       route.latency_penalty_micros,
       route.reconciliation_risk_micros
FROM a6_trade.merchant_treasury_route_bindings binding
JOIN a6_trade.treasury_wallet_routes route
  ON route.id = binding.wallet_route_id
JOIN a6_trade.treasury_wallet_principals principal
  ON principal.id = route.wallet_principal_id
JOIN a6_trade.merchant_treasury_assignments assignment
  ON assignment.merchant_id = binding.merchant_id
 AND assignment.wallet_principal_id = route.wallet_principal_id
 AND assignment.assignment_state = 'active'
WHERE binding.merchant_id = ANY(%(merchant_ids)s)
  AND binding.binding_state = 'healthy'
  AND binding.a6_withdraw_account_id IS NOT NULL
  AND binding.last_readback_at >= %(route_cutoff)s
  AND principal.enabled
  AND principal.principal_state = 'enabled'
  AND route.route_state NOT IN ('disabled', 'retired')
ORDER BY binding.merchant_id, route.id
"""


def _intent_record(row: Mapping[str, Any]) -> CashoutIntentRecord:
    reservation_id = row.get("fund_reservation_id")
    if reservation_id is None:
        raise CashoutError("cashout_reservation_missing")
    return CashoutIntentRecord(
        intent_id=int(row["id"]),
        merchant_id=int(row["merchant_id"]),
        method=str(row["method"]),
        state=str(row["intent_state"]),
        gross_micros=int(row["gross_micros"]),
        fund_reservation_id=int(reservation_id),
        idempotency_key=str(row["idempotency_key"]),
        remote_request_id=(
            str(row["remote_request_id"]) if row.get("remote_request_id") is not None else None
        ),
        remote_withdrawal_id=(
            str(row["remote_withdrawal_id"])
            if row.get("remote_withdrawal_id") is not None
            else None
        ),
    )


def _preview_contract_hash(
    routes: Sequence[CashoutRoutePreview],
    policy: CashoutPolicy,
) -> str:
    return _sha256(
        {
            "routes": [
                {
                    "wallet_route_id": route.wallet_route_id,
                    "fee_micros": route.fee_micros,
                    "confirmation_probability": _decimal_text(route.confirmation_probability),
                    "healthy": route.healthy,
                    "authoritative": route.authoritative,
                }
                for route in sorted(routes, key=lambda item: item.wallet_route_id)
            ],
            "standard_fixed_fee_usd": _decimal_text(policy.standard_fixed_fee_usd),
            "callback_fee_rate": _decimal_text(policy.callback_fee_rate),
        }
    )


def _validate_probability(value: Decimal, code: str) -> None:
    if value < 0 or value > 1:
        raise CashoutError(code)


def _clamp_probability(value: Decimal) -> Decimal:
    return min(Decimal("1"), max(Decimal("0"), value))


def _micros(value: Decimal) -> int:
    return _floor_decimal(value * MICROS_PER_USD)


def _usd(value: int) -> Decimal:
    return Decimal(value) / Decimal(MICROS_PER_USD)


def _floor_decimal(value: Decimal) -> int:
    return int(value.to_integral_value(rounding=ROUND_FLOOR))


def _decimal_text(value: Decimal) -> str:
    if not value.is_finite():
        return str(value)
    return format(value.normalize(), "f")


def _withdraw_preview_amounts(
    preview: Mapping[str, Any],
    gross_micros: int,
) -> tuple[int, int]:
    fee = _mapping_integer(
        preview,
        "fee_micros",
        "withdraw_fee_micros",
        "fee",
        "handling_fee",
    )
    net = _mapping_integer(
        preview,
        "net_micros",
        "receive_micros",
        "actual_amount_micros",
        "amount_received_micros",
        "actual_amount",
    )
    if fee is None and net is None:
        raise CashoutError("cashout_withdraw_preview_amounts_missing")
    if fee is None:
        fee = gross_micros - int(net or 0)
    if net is None:
        net = gross_micros - fee
    if fee < 0 or net < 0 or fee + net > gross_micros:
        raise CashoutError("cashout_withdraw_preview_amounts_invalid")
    return fee, net


def _match_withdrawal(
    items: Sequence[Any],
    context: CashoutReconciliationContext,
    *,
    match_window_seconds: int,
) -> tuple[Mapping[str, Any] | None, str | None]:
    candidates = [dict(item) for item in items if isinstance(item, Mapping)]
    expected_withdrawal_id = str(context.intent.remote_withdrawal_id or "").strip()
    if expected_withdrawal_id:
        for item in candidates:
            withdrawal_id = _optional_identity(item, "id", "withdrawal_id", "withdraw_id")
            if withdrawal_id == expected_withdrawal_id:
                return item, "remote_withdrawal_id"
    expected_request_id = str(context.intent.remote_request_id or "").strip()
    if expected_request_id:
        for item in candidates:
            request_id = _optional_identity(item, "request_id", "requestId")
            if request_id == expected_request_id:
                return item, "remote_request_id"

    submitted_at = context.submitted_at or context.submit_attempted_at
    strict_matches: list[tuple[float, Mapping[str, Any]]] = []
    for item in candidates:
        remote_user_id = _mapping_integer(item, "user_id", "supplier_user_id")
        if remote_user_id is not None and remote_user_id != context.merchant.a6_user_id:
            continue
        account_id = _mapping_integer(
            item,
            "account_id",
            "withdraw_account_id",
            "withdrawal_account_id",
        )
        gross = _mapping_integer(
            item,
            "gross_micros",
            "amount_micros",
            "requested_amount_micros",
            "withdraw_amount_micros",
            "amount",
        )
        created_at = _mapping_datetime(
            item,
            "submitted_at",
            "created_at",
            "created_time",
            "apply_time",
            "time",
        )
        if (
            account_id != context.remote_withdraw_account_id
            or gross != context.intent.gross_micros
            or created_at is None
        ):
            continue
        distance = abs((created_at - submitted_at).total_seconds())
        if distance <= match_window_seconds:
            strict_matches.append((distance, item))
    if not strict_matches:
        return None, None
    strict_matches.sort(
        key=lambda match: (
            match[0],
            _optional_identity(match[1], "id", "withdrawal_id", "withdraw_id") or "",
        )
    )
    return strict_matches[0][1], "account_gross_time"


def _withdraw_status(withdrawal: Mapping[str, Any] | None) -> str | None:
    if withdrawal is None:
        return None
    status = _mapping_text(withdrawal, "status", "state", "withdraw_status")
    if status is None:
        return None
    return status.lower().replace("-", "_").replace(" ", "_")


def _withdrawal_identity_conflicts(
    withdrawal: Mapping[str, Any] | None,
    context: CashoutReconciliationContext,
) -> list[str]:
    if withdrawal is None:
        return []
    conflicts: list[str] = []
    user_id = _mapping_integer(withdrawal, "user_id", "supplier_user_id")
    if user_id is not None and user_id != context.merchant.a6_user_id:
        conflicts.append("remote_user_id")
    account_id = _mapping_integer(
        withdrawal,
        "account_id",
        "withdraw_account_id",
        "withdrawal_account_id",
    )
    if account_id is not None and account_id != context.remote_withdraw_account_id:
        conflicts.append("remote_withdraw_account_id")
    gross = _mapping_integer(
        withdrawal,
        "gross_micros",
        "amount_micros",
        "requested_amount_micros",
        "withdraw_amount_micros",
        "amount",
    )
    if gross is not None and gross != context.intent.gross_micros:
        conflicts.append("gross_micros")
    expected_withdrawal_id = str(context.intent.remote_withdrawal_id or "").strip()
    observed_withdrawal_id = _optional_identity(
        withdrawal,
        "id",
        "withdrawal_id",
        "withdraw_id",
    )
    if (
        expected_withdrawal_id
        and observed_withdrawal_id is not None
        and observed_withdrawal_id != expected_withdrawal_id
    ):
        conflicts.append("remote_withdrawal_id")
    return conflicts


def _closed_withdraw_amounts(
    withdrawal: Mapping[str, Any],
    expected_gross_micros: int,
) -> tuple[int, int, int] | None:
    reported_gross = _mapping_integer(
        withdrawal,
        "gross_micros",
        "amount_micros",
        "requested_amount_micros",
        "withdraw_amount_micros",
        "amount",
    )
    gross = expected_gross_micros if reported_gross is None else reported_gross
    fee = _mapping_integer(
        withdrawal,
        "fee_micros",
        "withdraw_fee_micros",
        "handling_fee_micros",
        "fee",
        "handling_fee",
    )
    net = _mapping_integer(
        withdrawal,
        "net_micros",
        "receive_micros",
        "received_micros",
        "actual_amount_micros",
        "amount_received_micros",
        "actual_amount",
    )
    if gross != expected_gross_micros or (fee is None and net is None):
        return None
    if fee is None:
        fee = gross - int(net or 0)
    if net is None:
        net = gross - fee
    if fee < 0 or net < 0 or abs(gross - fee - net) > 1:
        return None
    return gross, fee, net


def _withdraw_balance_effect(
    context: CashoutReconciliationContext,
    summary: Mapping[str, Any] | None,
) -> tuple[bool, Mapping[str, Any]]:
    if summary is None:
        return False, {"observed": False}
    current_withdrawable = _mapping_integer(
        summary,
        "balance_available_micros",
        "withdrawable_micros",
    )
    current_withdrawing = _mapping_integer(summary, "withdrawing_micros")
    current_paid = _mapping_integer(summary, "total_paid_micros", "paid_micros")
    withdrawable_drop = (
        max(0, context.baseline_withdrawable_micros - current_withdrawable)
        if current_withdrawable is not None
        else 0
    )
    withdrawing_increase = (
        max(0, current_withdrawing - context.baseline_withdrawing_micros)
        if current_withdrawing is not None
        and context.baseline_withdrawing_micros is not None
        else 0
    )
    paid_increase = (
        max(0, current_paid - context.baseline_paid_micros)
        if current_paid is not None and context.baseline_paid_micros is not None
        else 0
    )
    gross_threshold = max(1, context.intent.gross_micros - 1)
    net_threshold = max(1, context.expected_net_micros - 1)
    effect = bool(
        withdrawable_drop >= gross_threshold
        or withdrawing_increase >= gross_threshold
        or paid_increase >= net_threshold
    )
    return effect, {
        "observed": True,
        "withdrawable_drop_micros": withdrawable_drop,
        "withdrawing_increase_micros": withdrawing_increase,
        "paid_increase_micros": paid_increase,
    }


def _mapping_integer(document: Mapping[str, Any], *keys: str) -> int | None:
    for key in keys:
        if key not in document:
            continue
        value = document[key]
        if isinstance(value, bool):
            continue
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            continue
        if parsed >= 0:
            return parsed
    for nested_key in ("preview", "result", "withdrawal"):
        nested = document.get(nested_key)
        if isinstance(nested, Mapping):
            parsed = _mapping_integer(nested, *keys)
            if parsed is not None:
                return parsed
    return None


def _mapping_text(document: Mapping[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = document.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    for nested_key in ("result", "withdrawal"):
        nested = document.get(nested_key)
        if isinstance(nested, Mapping):
            parsed = _mapping_text(nested, *keys)
            if parsed is not None:
                return parsed
    return None


def _mapping_datetime(document: Mapping[str, Any], *keys: str) -> datetime | None:
    for key in keys:
        if key not in document:
            continue
        parsed = _parse_datetime(document[key])
        if parsed is not None:
            return parsed
    for nested_key in ("result", "withdrawal"):
        nested = document.get(nested_key)
        if isinstance(nested, Mapping):
            parsed = _mapping_datetime(nested, *keys)
            if parsed is not None:
                return parsed
    return None


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        timestamp = float(value)
        if timestamp > 100_000_000_000:
            timestamp /= 1000
        try:
            return datetime.fromtimestamp(timestamp, tz=timezone.utc)
        except (OSError, OverflowError, ValueError):
            return None
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _nonnegative_integer(value: Any, code: str) -> int:
    if isinstance(value, bool):
        raise CashoutError(code)
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise CashoutError(code) from None
    if parsed < 0:
        raise CashoutError(code)
    return parsed


def _optional_identity(document: Mapping[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = document.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    for nested_key in ("request", "withdrawal", "result"):
        nested = document.get(nested_key)
        if isinstance(nested, Mapping):
            value = _optional_identity(nested, *keys)
            if value is not None:
                return value
    return None


def _hash_valid(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def _sha256(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _json(value: Mapping[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _jsonable_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    def normalize(item: Any) -> Any:
        if isinstance(item, Mapping):
            return {str(key): normalize(nested) for key, nested in item.items()}
        if isinstance(item, (list, tuple)):
            return [normalize(nested) for nested in item]
        if isinstance(item, datetime):
            return item.isoformat()
        if isinstance(item, Decimal):
            return _decimal_text(item)
        if item is None or isinstance(item, (str, int, float, bool)):
            return item
        return str(item)

    return {str(key): normalize(item) for key, item in value.items()}


def _exception_code(exc: Exception) -> str:
    return str(getattr(exc, "code", "") or exc.__class__.__name__).strip()


def _row(cursor: Any) -> dict[str, Any] | None:
    row = cursor.fetchone()
    if row is None:
        return None
    if isinstance(row, Mapping):
        return dict(row)
    names = [column.name for column in cursor.description or ()]
    return dict(zip(names, row, strict=False))


def _rows(cursor: Any) -> list[dict[str, Any]]:
    rows = cursor.fetchall() or []
    if rows and isinstance(rows[0], Mapping):
        return [dict(row) for row in rows]
    names = [column.name for column in cursor.description or ()]
    return [dict(zip(names, row, strict=False)) for row in rows]


def _close(connection: Any) -> None:
    close = getattr(connection, "close", None)
    if callable(close):
        close()
