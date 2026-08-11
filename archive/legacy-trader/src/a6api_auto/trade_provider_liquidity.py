"""Automatic Provider liquidity discovery, fail-closed drain, and state projection."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
from typing import Any, Protocol

from .trade_provider_topup import (
    LiveBalance,
    ProviderTopupContext,
    ProviderTopupError,
    ProviderTopupOutcome,
    ProviderTopupService,
)

_FUND_LOCK_NAMESPACE = 0x41364652
_LIQUIDITY_STATES = {
    "FUNDED",
    "REFILLING",
    "UNFUNDED",
    "TRANSFER_UNKNOWN",
    "UNKNOWN",
}


class ProviderLiquidityError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class ProviderLiquidityCandidate:
    model: str
    context: ProviderTopupContext
    enodot_account_id: int
    a6_token_id: int
    inventory_state: str
    liquidity_state: str
    schedulable: bool
    assigned_slot_no: int | None = None
    balance_insufficient_observed: bool = False
    cashout_drain_requested: bool = False

    def __post_init__(self) -> None:
        if not str(self.model or "").strip():
            raise ProviderLiquidityError("provider_liquidity_model_invalid")
        if self.enodot_account_id < 1 or self.a6_token_id < 1:
            raise ProviderLiquidityError("provider_liquidity_identity_invalid")
        if self.inventory_state not in {
            "READY",
            "WARM_STANDBY",
            "VACANT",
            "REPAIRING",
        }:
            raise ProviderLiquidityError("provider_liquidity_inventory_state_invalid")
        if self.liquidity_state not in _LIQUIDITY_STATES:
            raise ProviderLiquidityError("provider_liquidity_state_invalid")
        if not isinstance(self.schedulable, bool):
            raise ProviderLiquidityError("provider_liquidity_schedulable_invalid")
        if self.assigned_slot_no is not None and self.assigned_slot_no < 1:
            raise ProviderLiquidityError("provider_liquidity_slot_invalid")
        if not isinstance(self.balance_insufficient_observed, bool):
            raise ProviderLiquidityError("provider_liquidity_balance_error_invalid")
        if not isinstance(self.cashout_drain_requested, bool):
            raise ProviderLiquidityError("provider_liquidity_cashout_drain_invalid")


class ProviderLiquidityStore(Protocol):
    def candidates(
        self,
        model: str,
        *,
        limit: int,
    ) -> Sequence[ProviderLiquidityCandidate]: ...

    def project(
        self,
        candidate: ProviderLiquidityCandidate,
        outcome: ProviderTopupOutcome,
        *,
        hard_blocked: bool,
        replacement_required: bool,
        observed_at: datetime,
        event_key: str,
        refill_target_units: Decimal,
        cashout_drain_requested: bool,
    ) -> Mapping[str, Any]: ...


class ProviderSchedulabilityAdmin(Protocol):
    def get_account(self, account_id: int) -> Mapping[str, Any]: ...

    def set_schedulable(
        self,
        account_id: int,
        schedulable: bool,
        *,
        idempotency_key: str = "",
    ) -> Mapping[str, Any]: ...


class ProviderLiquidityHardLineGuard:
    """Drain and read back one Enodot account before any low-balance transfer."""

    def __init__(
        self,
        candidates: Sequence[ProviderLiquidityCandidate],
        admin: ProviderSchedulabilityAdmin,
        *,
        apply: bool,
    ) -> None:
        self.candidates = {item.context.merchant_id: item for item in candidates}
        self.admin = admin
        self.apply = apply
        self.encountered: set[int] = set()
        self.confirmed: set[int] = set()
        self.balances: dict[int, LiveBalance] = {}
        self.remote_write_count = 0

    def __call__(self, context: ProviderTopupContext, balance: LiveBalance) -> None:
        candidate = self.candidates.get(context.merchant_id)
        if candidate is None:
            raise ProviderTopupError("provider_liquidity_candidate_missing")
        self.encountered.add(context.merchant_id)
        self.balances[context.merchant_id] = balance
        if not self.apply:
            return
        try:
            account = self.admin.get_account(candidate.enodot_account_id)
            if int(account.get("id") or 0) != candidate.enodot_account_id:
                raise ProviderTopupError("provider_liquidity_account_readback_mismatch")
            observed = account.get("schedulable")
            if not isinstance(observed, bool):
                raise ProviderTopupError("provider_liquidity_schedulable_readback_invalid")
            if observed:
                digest = hashlib.sha256(
                    (
                        f"{candidate.model}:{context.merchant_id}:"
                        f"{candidate.enodot_account_id}:{balance.user_balance}"
                    ).encode()
                ).hexdigest()[:24]
                self.admin.set_schedulable(
                    candidate.enodot_account_id,
                    False,
                    idempotency_key=f"provider-liquidity-block:{digest}",
                )
                self.remote_write_count += 1
            readback = self.admin.get_account(candidate.enodot_account_id)
            if (
                int(readback.get("id") or 0) != candidate.enodot_account_id
                or readback.get("schedulable") is not False
            ):
                raise ProviderTopupError("provider_liquidity_hard_block_readback_mismatch")
        except ProviderTopupError:
            raise
        except Exception:
            raise ProviderTopupError("provider_liquidity_hard_block_failed") from None
        self.confirmed.add(context.merchant_id)

    def hard_line_encountered(self, merchant_id: int) -> bool:
        return merchant_id in self.encountered

    def hard_line_confirmed(self, merchant_id: int) -> bool:
        return merchant_id in self.confirmed

    def hard_line_balance(self, merchant_id: int) -> LiveBalance | None:
        return self.balances.get(merchant_id)


class ProviderLiquidityReconciler:
    """Discover every managed Provider and run the existing strict top-up saga."""

    def __init__(
        self,
        store: ProviderLiquidityStore,
        topup: ProviderTopupService,
        guard: ProviderLiquidityHardLineGuard,
        *,
        model: str,
        apply: bool,
        clock: Callable[[], datetime],
        max_providers: int = 100,
        refill_target_units: Decimal = Decimal("2"),
    ) -> None:
        self.store = store
        self.topup = topup
        self.guard = guard
        self.model = str(model or "").strip()
        self.apply = apply
        self.clock = clock
        self.max_providers = max_providers
        self.refill_target_units = refill_target_units
        if not self.model:
            raise ProviderLiquidityError("provider_liquidity_model_invalid")
        if isinstance(max_providers, bool) or not 1 <= max_providers <= 10_000:
            raise ProviderLiquidityError("provider_liquidity_limit_invalid")
        if refill_target_units < 1:
            raise ProviderLiquidityError("provider_liquidity_target_invalid")

    def run_once(
        self,
        candidates: Sequence[ProviderLiquidityCandidate] | None = None,
    ) -> dict[str, Any]:
        selected = tuple(
            candidates
            if candidates is not None
            else self.store.candidates(self.model, limit=self.max_providers)
        )
        run_id = str(uuid.uuid4())
        rows: list[dict[str, Any]] = []
        projection_failures = 0
        for candidate in selected:
            if candidate.model != self.model:
                raise ProviderLiquidityError("provider_liquidity_candidate_scope_mismatch")
            outcome = self.topup.reconcile_one(
                candidate.context,
                allow_new_transfer=not candidate.cashout_drain_requested,
            )
            observed_balance = LiveBalance(
                user_balance=max(
                    0,
                    int(
                        outcome.user_balance_after
                        if outcome.user_balance_after is not None
                        else outcome.user_balance_before
                    ),
                ),
                withdrawable=max(0, int(outcome.withdrawable_before)),
            )
            if (
                candidate.balance_insufficient_observed
                and observed_balance.withdrawable < candidate.context.quota_per_unit
            ):
                self.guard(candidate.context, observed_balance)
            balance_hard_line = bool(
                candidate.balance_insufficient_observed
                and observed_balance.withdrawable < candidate.context.quota_per_unit
            )
            if candidate.cashout_drain_requested and not balance_hard_line:
                self.guard(candidate.context, observed_balance)
            drain_encountered = self.guard.hard_line_encountered(
                candidate.context.merchant_id
            )
            drain_confirmed = self.guard.hard_line_confirmed(candidate.context.merchant_id)
            if balance_hard_line:
                outcome = replace(
                    outcome,
                    funding_eligible=False,
                    replacement_required=True,
                    liquidity_state="UNFUNDED",
                )
            elif candidate.cashout_drain_requested:
                outcome = replace(outcome, replacement_required=True)
            replacement_required = bool(
                outcome.replacement_required
                or balance_hard_line
                or candidate.cashout_drain_requested
            )
            projection: Mapping[str, Any] = {"action": "preview"}
            credible_observation = outcome.action != "failed" or (
                drain_encountered and drain_confirmed
            )
            if self.apply and credible_observation and (
                not drain_encountered or drain_confirmed
            ):
                try:
                    projection = self.store.project(
                        candidate,
                        outcome,
                        hard_blocked=drain_encountered,
                        replacement_required=replacement_required,
                        observed_at=self.clock(),
                        event_key=(
                            f"provider-liquidity:{run_id}:"
                            f"{candidate.context.merchant_id}"
                        ),
                        refill_target_units=self.refill_target_units,
                        cashout_drain_requested=candidate.cashout_drain_requested,
                    )
                except Exception as exc:
                    projection_failures += 1
                    diagnostic = getattr(exc, "diag", None)
                    projection = {
                        "action": "projection_failed",
                        "error_class": str(
                            getattr(exc, "sqlstate", "") or type(exc).__name__
                        ),
                        "detail": str(exc)[:256],
                        "constraint": getattr(diagnostic, "constraint_name", None),
                    }
            elif self.apply and drain_encountered and not drain_confirmed:
                projection = {"action": "hard_block_unconfirmed"}
                projection_failures += 1
            elif self.apply:
                projection = {"action": "observation_failed"}
                projection_failures += 1
            row = outcome.public()
            row.update(
                {
                    "model": candidate.model,
                    "enodot_account_id": candidate.enodot_account_id,
                    "a6_token_id": candidate.a6_token_id,
                    "slot_no": candidate.assigned_slot_no,
                    "hard_line_encountered": balance_hard_line,
                    "hard_line_confirmed": balance_hard_line and drain_confirmed,
                    "cashout_drain_requested": candidate.cashout_drain_requested,
                    "cashout_drain_confirmed": (
                        candidate.cashout_drain_requested and drain_confirmed
                    ),
                    "replacement_required": replacement_required,
                    "projection": dict(projection),
                }
            )
            rows.append(row)
        transfer_actions = sum(1 for row in rows if row["action"] == "topped_up")
        return {
            "success": projection_failures == 0
            and all(row["error_class"] is None for row in rows),
            "mode": "applied" if self.apply else "preview",
            "model": self.model,
            "run_id": run_id,
            "provider_count": len(rows),
            "topped_up": transfer_actions,
            "unfunded": sum(1 for row in rows if row["liquidity_state"] == "UNFUNDED"),
            "transfer_unknown": sum(
                1 for row in rows if row["liquidity_state"] == "TRANSFER_UNKNOWN"
            ),
            "hard_blocked": sum(1 for row in rows if row["hard_line_confirmed"]),
            "cashout_drained": sum(
                1 for row in rows if row["cashout_drain_confirmed"]
            ),
            "replacement_required": sum(
                1 for row in rows if row["replacement_required"]
            ),
            "projection_failures": projection_failures,
            "remote_write_count_min": self.guard.remote_write_count + transfer_actions,
            "providers": rows,
        }


class PostgresProviderLiquidityStore:
    def __init__(self, connection_factory: Callable[[], Any]) -> None:
        if not callable(connection_factory):
            raise ProviderLiquidityError("provider_liquidity_connection_factory_invalid")
        self.connection_factory = connection_factory

    def candidates(
        self,
        model: str,
        *,
        limit: int,
    ) -> Sequence[ProviderLiquidityCandidate]:
        model = str(model or "").strip()
        if not model:
            raise ProviderLiquidityError("provider_liquidity_model_invalid")
        if isinstance(limit, bool) or not 1 <= limit <= 10_000:
            raise ProviderLiquidityError("provider_liquidity_limit_invalid")
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(_CANDIDATES_SQL, (model, limit))
                rows = _rows(cursor)
        finally:
            _close(connection)
        return tuple(_candidate(row) for row in rows)

    def project(
        self,
        candidate: ProviderLiquidityCandidate,
        outcome: ProviderTopupOutcome,
        *,
        hard_blocked: bool,
        replacement_required: bool,
        observed_at: datetime,
        event_key: str,
        refill_target_units: Decimal,
        cashout_drain_requested: bool,
    ) -> Mapping[str, Any]:
        if outcome.liquidity_state not in _LIQUIDITY_STATES:
            raise ProviderLiquidityError("provider_liquidity_state_invalid")
        balance = max(
            0,
            int(
                outcome.user_balance_after
                if outcome.user_balance_after is not None
                else outcome.user_balance_before
            ),
        )
        withdrawable = max(0, int(outcome.withdrawable_before))
        quota = candidate.context.quota_per_unit
        target = int(refill_target_units * quota)
        immediate_need = _ceil_to_unit(max(0, target - balance), quota)
        pending_need = (
            max(0, int(outcome.amount))
            if outcome.liquidity_state == "TRANSFER_UNKNOWN"
            else 0
        )
        retirement_required = bool(
            hard_blocked
            and replacement_required
            and (cashout_drain_requested or not outcome.funding_eligible)
        )
        vacancy_required = bool(
            retirement_required and candidate.assigned_slot_no is not None
        )
        projected_funding_eligible = bool(
            outcome.funding_eligible and not retirement_required
        )
        reserve = (
            0
            if retirement_required or cashout_drain_requested
            else max(quota, immediate_need, pending_need)
        )
        if retirement_required and not cashout_drain_requested:
            reserve_basis = "provider_retirement"
        elif cashout_drain_requested:
            reserve_basis = "cashout_drain"
        elif pending_need >= reserve:
            reserve_basis = "pending_topup"
        elif immediate_need >= reserve:
            reserve_basis = "immediate_refill"
        else:
            reserve_basis = "configured_minimum"
        evidence = {
            "schema_version": 1,
            "model": candidate.model,
            "merchant_id": candidate.context.merchant_id,
            "account_id": candidate.enodot_account_id,
            "token_id": candidate.a6_token_id,
            "slot_no": candidate.assigned_slot_no,
            "action": outcome.action,
            "liquidity_state": outcome.liquidity_state,
            "funding_eligible": outcome.funding_eligible,
            "hard_blocked": hard_blocked,
            "replacement_required": replacement_required,
            "retirement_required": retirement_required,
            "vacancy_required": vacancy_required,
            "cashout_drain_requested": cashout_drain_requested,
            "intent_id": outcome.intent_id,
            "fund_reservation_id": outcome.fund_reservation_id,
            "transfer_amount": outcome.amount,
            "user_balance_before": outcome.user_balance_before,
            "user_balance_after": outcome.user_balance_after,
            "withdrawable_before": outcome.withdrawable_before,
            "provider_reserve": reserve,
            "error_class": outcome.error_class,
            "observed_at": observed_at.isoformat(),
        }
        evidence_json = _json(evidence)
        source_hash = hashlib.sha256(evidence_json.encode()).hexdigest()
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT pg_advisory_xact_lock(%s, %s)",
                    (_FUND_LOCK_NAMESPACE, candidate.context.merchant_id),
                )
                cursor.execute(
                    """
                    SELECT inventory_state, schedulable
                    FROM a6_trade.provider_inventory
                    WHERE model = %s AND merchant_id = %s
                      AND enodot_account_id = %s AND a6_token_id = %s
                    FOR UPDATE
                    """,
                    (
                        candidate.model,
                        candidate.context.merchant_id,
                        candidate.enodot_account_id,
                        candidate.a6_token_id,
                    ),
                )
                current = _row(cursor)
                if current is None:
                    raise ProviderLiquidityError("provider_liquidity_inventory_missing")
                current_state = str(current.get("inventory_state") or "")
                requires_reverification = bool(
                    hard_blocked
                    or cashout_drain_requested
                    or not outcome.funding_eligible
                    or current_state == "REPAIRING"
                )
                if retirement_required:
                    inventory_state = "RETIRED"
                elif cashout_drain_requested or not outcome.funding_eligible:
                    inventory_state = "REPAIRING"
                elif requires_reverification:
                    inventory_state = "WARM_STANDBY"
                else:
                    inventory_state = current_state
                schedulable = bool(current.get("schedulable")) and not requires_reverification
                cursor.execute(
                    """
                    UPDATE a6_trade.provider_inventory
                    SET inventory_state = %s,
                        liquidity_state = %s,
                        funding_eligible = %s,
                        schedulable = %s,
                        assigned_slot_no = %s,
                        balance_units = %s,
                        financial_source_hash = %s,
                        state_version = state_version + 1,
                        last_financial_observed_at = %s,
                        updated_at = now()
                    WHERE model = %s AND merchant_id = %s
                      AND enodot_account_id = %s AND a6_token_id = %s
                    """,
                    (
                        inventory_state,
                        outcome.liquidity_state,
                        projected_funding_eligible,
                        schedulable,
                        None if retirement_required else candidate.assigned_slot_no,
                        balance,
                        source_hash,
                        observed_at,
                        candidate.model,
                        candidate.context.merchant_id,
                        candidate.enodot_account_id,
                        candidate.a6_token_id,
                    ),
                )
                inventory_changed = int(getattr(cursor, "rowcount", 0) or 0)
                slot_changed = 0
                membership_repair_request_id: int | None = None
                if vacancy_required:
                    cursor.execute(
                        """
                        UPDATE a6_trade.provider_pool_slots
                        SET enodot_binding_id = NULL,
                            enodot_account_id = NULL,
                            a6_token_id = NULL,
                            desired_channel_id = NULL,
                            desired_rank = NULL,
                            desired_priority = NULL,
                            desired_slot_cost_multiplier = NULL,
                            observed_rate_multiplier = NULL,
                            liquidity_state = %s,
                            funding_eligible = false,
                            slot_state = 'VACANT',
                            desired_schedulable = false,
                            observed_schedulable = false,
                            confirmed_pin_binding_id = NULL,
                            pin_confirmed = false,
                            probe_confirmed = false,
                            route_mode = 'fixed_pin',
                            auto_pool_generation_id = NULL,
                            auto_pool_subscription_id = NULL,
                            auto_pool_confirmed = false,
                            auto_pool_max_ratio_cny = NULL,
                            state_version = state_version + 1,
                            updated_at = now()
                        WHERE model = %s AND valid_to IS NULL
                          AND slot_no = %s
                          AND enodot_account_id = %s AND a6_token_id = %s
                          AND slot_state <> 'VACANT'
                        RETURNING routing_generation, cost_generation
                        """,
                        (
                            outcome.liquidity_state,
                            candidate.model,
                            candidate.assigned_slot_no,
                            candidate.enodot_account_id,
                            candidate.a6_token_id,
                        ),
                    )
                    vacated_slot = _row(cursor)
                    slot_changed = 1 if vacated_slot is not None else 0
                    if vacated_slot is None:
                        raise ProviderLiquidityError(
                            "provider_liquidity_vacancy_projection_conflict"
                        )
                    cursor.execute(
                        """
                        UPDATE a6_trade.provider_auto_pool_accounts account
                        SET account_state = 'superseded',
                            schedulable_confirmed = false,
                            error_class = coalesce(error_class, %s),
                            completed_at = coalesce(completed_at, now()),
                            updated_at = now()
                        FROM a6_trade.provider_auto_pool_generations generation
                        WHERE generation.id = account.generation_id
                          AND generation.model = %s
                          AND generation.generation_state = 'active'
                          AND account.enodot_account_id = %s
                          AND account.token_ref_id = %s
                          AND account.account_state IN ('ready', 'failed', 'unfunded')
                        """,
                        (
                            outcome.error_class or "provider_membership_replacement_required",
                            candidate.model,
                            candidate.enodot_account_id,
                            candidate.a6_token_id,
                        ),
                    )
                    cursor.execute(
                        """
                        INSERT INTO a6_trade.provider_membership_repair_requests (
                            model, source_routing_generation,
                            source_cost_generation, slot_no,
                            outgoing_merchant_id, outgoing_enodot_account_id,
                            outgoing_a6_token_id, reason_code
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                        RETURNING id
                        """,
                        (
                            candidate.model,
                            int(vacated_slot["routing_generation"]),
                            int(vacated_slot["cost_generation"]),
                            candidate.assigned_slot_no,
                            candidate.context.merchant_id,
                            candidate.enodot_account_id,
                            candidate.a6_token_id,
                            (
                                "cashout_drain"
                                if cashout_drain_requested
                                else "provider_funding_contract_failed"
                            ),
                        ),
                    )
                    repair_request = _row(cursor)
                    if repair_request is None:
                        raise ProviderLiquidityError(
                            "provider_liquidity_membership_repair_enqueue_failed"
                        )
                    membership_repair_request_id = int(repair_request["id"])
                elif requires_reverification:
                    cursor.execute(
                        """
                        UPDATE a6_trade.provider_pool_slots
                        SET liquidity_state = %s,
                            funding_eligible = %s,
                            slot_state = 'REPAIRING',
                            desired_schedulable = false,
                            observed_schedulable = false,
                            state_version = state_version + 1,
                            updated_at = now()
                        WHERE model = %s AND valid_to IS NULL
                          AND enodot_account_id = %s AND a6_token_id = %s
                          AND slot_state <> 'VACANT'
                        """,
                        (
                            outcome.liquidity_state,
                            outcome.funding_eligible,
                            candidate.model,
                            candidate.enodot_account_id,
                            candidate.a6_token_id,
                        ),
                    )
                    slot_changed = int(getattr(cursor, "rowcount", 0) or 0)
                else:
                    cursor.execute(
                        """
                        UPDATE a6_trade.provider_pool_slots
                        SET liquidity_state = %s,
                            funding_eligible = %s,
                            state_version = state_version + 1,
                            updated_at = now()
                        WHERE model = %s AND valid_to IS NULL
                          AND enodot_account_id = %s AND a6_token_id = %s
                          AND slot_state <> 'VACANT'
                          AND (
                              liquidity_state IS DISTINCT FROM %s
                              OR funding_eligible IS DISTINCT FROM %s
                          )
                        """,
                        (
                            outcome.liquidity_state,
                            outcome.funding_eligible,
                            candidate.model,
                            candidate.enodot_account_id,
                            candidate.a6_token_id,
                            outcome.liquidity_state,
                            outcome.funding_eligible,
                        ),
                    )
                    slot_changed = int(getattr(cursor, "rowcount", 0) or 0)
                if retirement_required:
                    cursor.execute(
                        """
                        UPDATE a6_trade.enodot_account_bindings
                        SET state = 'drained',
                            last_error_class = %s,
                            last_error_at = now(),
                            updated_at = now()
                        WHERE a6_token_id = %s AND enodot_account_id = %s
                        """,
                        (
                            outcome.error_class or "provider_membership_replacement_required",
                            candidate.a6_token_id,
                            candidate.enodot_account_id,
                        ),
                    )
                    cursor.execute(
                        """
                        UPDATE a6_trade.a6_merchants
                        SET roles = CASE
                                WHEN 'seller' = ANY(roles)
                                THEN ARRAY['seller', 'observer']::text[]
                                ELSE ARRAY['observer']::text[]
                            END,
                            role_revision = role_revision + 1,
                            updated_at = now()
                        WHERE id = %s AND 'provider' = ANY(roles)
                        """,
                        (candidate.context.merchant_id,),
                    )
                cursor.execute(
                    """
                    INSERT INTO a6_trade.provider_fund_reserves (
                        model, merchant_id, reserved_micros, reserve_basis,
                        user_balance_micros, withdrawable_micros, quota_per_unit,
                        pending_transfer_intent_id, balance_source_hash, observed_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (model, merchant_id) DO UPDATE SET
                        reserved_micros = EXCLUDED.reserved_micros,
                        reserve_basis = EXCLUDED.reserve_basis,
                        user_balance_micros = EXCLUDED.user_balance_micros,
                        withdrawable_micros = EXCLUDED.withdrawable_micros,
                        quota_per_unit = EXCLUDED.quota_per_unit,
                        pending_transfer_intent_id = EXCLUDED.pending_transfer_intent_id,
                        balance_source_hash = EXCLUDED.balance_source_hash,
                        observed_at = EXCLUDED.observed_at,
                        updated_at = now()
                    """,
                    (
                        candidate.model,
                        candidate.context.merchant_id,
                        reserve,
                        reserve_basis,
                        balance,
                        withdrawable,
                        quota,
                        (
                            outcome.intent_id
                            if outcome.liquidity_state == "TRANSFER_UNKNOWN"
                            else None
                        ),
                        source_hash,
                        observed_at,
                    ),
                )
                cursor.execute(
                    """
                    INSERT INTO a6_trade.provider_liquidity_events (
                        idempotency_key, model, merchant_id, enodot_account_id,
                        a6_token_id, slot_no, action, liquidity_state,
                        funding_eligible, hard_blocked, replacement_required,
                        transfer_intent_id, fund_reservation_id,
                        transfer_amount_micros, user_balance_before_micros,
                        user_balance_after_micros, withdrawable_before_micros,
                        provider_reserve_micros, error_class, evidence,
                        evidence_hash, observed_at
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s
                    )
                    ON CONFLICT (idempotency_key) DO NOTHING
                    """,
                    (
                        event_key,
                        candidate.model,
                        candidate.context.merchant_id,
                        candidate.enodot_account_id,
                        candidate.a6_token_id,
                        candidate.assigned_slot_no,
                        outcome.action,
                        outcome.liquidity_state,
                        projected_funding_eligible,
                        hard_blocked,
                        replacement_required,
                        outcome.intent_id,
                        outcome.fund_reservation_id,
                        outcome.amount,
                        outcome.user_balance_before,
                        outcome.user_balance_after,
                        outcome.withdrawable_before,
                        reserve,
                        outcome.error_class,
                        evidence_json,
                        source_hash,
                        observed_at,
                    ),
                )
            connection.commit()
        except Exception:
            rollback = getattr(connection, "rollback", None)
            if callable(rollback):
                rollback()
            raise
        finally:
            _close(connection)
        return {
            "action": "projected",
            "inventory_state": inventory_state,
            "liquidity_state": outcome.liquidity_state,
            "funding_eligible": projected_funding_eligible,
            "schedulable": schedulable,
            "inventory_changed": inventory_changed,
            "slot_changed": slot_changed,
            "slot_vacated": vacancy_required,
            "provider_duty_retired": retirement_required,
            "membership_repair_required": vacancy_required,
            "membership_repair_request_id": membership_repair_request_id,
            "provider_reserve_micros": reserve,
            "reserve_basis": reserve_basis,
        }


_CANDIDATES_SQL = """
SELECT inventory.model,
       inventory.merchant_id,
       merchant.a6_user_id,
       merchant.session_ref,
       merchant.quota_per_unit,
       inventory.enodot_account_id,
       inventory.a6_token_id,
       inventory.inventory_state,
       inventory.liquidity_state,
       inventory.schedulable,
       inventory.assigned_slot_no,
       binding.last_error_class IN (
           'account_quota_insufficient',
           'insufficient_quota',
           'provider_user_balance_insufficient_unfunded'
       ) AS balance_insufficient_observed,
       EXISTS (
           SELECT 1
           FROM a6_trade.a6_withdrawal_intents cashout
           WHERE cashout.merchant_id = inventory.merchant_id
             AND cashout.intent_state = 'AWAITING_PROVIDER_DRAIN'
       ) AS cashout_drain_requested
FROM a6_trade.provider_inventory inventory
JOIN a6_trade.a6_merchants merchant
  ON merchant.id = inventory.merchant_id
JOIN a6_trade.enodot_account_bindings binding
  ON binding.id = inventory.enodot_binding_id
 AND binding.a6_token_id = inventory.a6_token_id
 AND binding.enodot_account_id = inventory.enodot_account_id
 AND binding.state = 'bound'
WHERE inventory.model = %s
  AND inventory.inventory_state <> 'RETIRED'
  AND 'provider' = ANY(merchant.roles)
  AND merchant.status = 'supplier_submitted'
  AND merchant.a6_user_id IS NOT NULL
  AND merchant.quota_per_unit > 0
  AND inventory.enodot_account_id IS NOT NULL
  AND inventory.a6_token_id IS NOT NULL
ORDER BY
  CASE
    WHEN EXISTS (
      SELECT 1
      FROM a6_trade.a6_withdrawal_intents cashout
      WHERE cashout.merchant_id = inventory.merchant_id
        AND cashout.intent_state = 'AWAITING_PROVIDER_DRAIN'
    ) THEN 0
    WHEN inventory.inventory_state = 'REPAIRING'
      OR inventory.liquidity_state IN ('UNFUNDED', 'TRANSFER_UNKNOWN')
    THEN 0
    WHEN inventory.balance_units IS NULL
      OR inventory.balance_units < 2 * merchant.quota_per_unit
    THEN 1
    ELSE 2
  END,
  inventory.last_financial_observed_at NULLS FIRST,
  inventory.merchant_id
LIMIT %s
"""


def _candidate(row: Mapping[str, Any]) -> ProviderLiquidityCandidate:
    return ProviderLiquidityCandidate(
        model=str(row.get("model") or ""),
        context=ProviderTopupContext(
            merchant_id=int(row.get("merchant_id") or 0),
            a6_user_id=int(row.get("a6_user_id") or 0),
            session_ref=str(row.get("session_ref") or ""),
            quota_per_unit=int(row.get("quota_per_unit") or 0),
        ),
        enodot_account_id=int(row.get("enodot_account_id") or 0),
        a6_token_id=int(row.get("a6_token_id") or 0),
        inventory_state=str(row.get("inventory_state") or ""),
        liquidity_state=str(row.get("liquidity_state") or "UNKNOWN"),
        schedulable=bool(row.get("schedulable")),
        assigned_slot_no=(
            int(row["assigned_slot_no"])
            if row.get("assigned_slot_no") is not None
            else None
        ),
        balance_insufficient_observed=bool(row.get("balance_insufficient_observed")),
        cashout_drain_requested=bool(row.get("cashout_drain_requested")),
    )


def _ceil_to_unit(value: int, unit: int) -> int:
    if value <= 0:
        return 0
    return -(-value // unit) * unit


def _json(value: Mapping[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


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
