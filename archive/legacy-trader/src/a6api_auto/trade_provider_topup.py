"""Keep Provider A6 user balance funded without inventing a schedulability floor.

A Provider spends its A6 user balance to serve Enodot traffic. The numeric user balance
does not decide schedulability; a real insufficient-balance response does. Even then,
the account is only unfunded when withdrawable balance is below ``quota_per_unit``, the
whole-unit transfer increment. Earnings arrive on the merchant's *withdrawable* balance,
which is a different pot: moving it across is an explicit transfer.

This module closes that loop. It reuses the intent -> execute -> read-back discipline
and the ``a6_balance_transfer_intents`` table already established by seller retirement,
so a transfer whose response is lost is reconciled by reading the balance back, never by
blindly re-posting the same amount.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Protocol

from .errors import A6AutoError
from .trade_authenticated_client import (
    AuthenticatedClientFactory,
    AuthenticatedSessionError,
    authenticated_a6_client,
)

# Strict liquidity mode keeps refill headroom without conflating the whole-unit transfer
# increment with request schedulability. The caller owns the observed-error hard line.
DEFAULT_USABLE_FLOOR_UNITS = Decimal("0")
DEFAULT_REFILL_TRIGGER_UNITS = Decimal("2")
DEFAULT_REFILL_TARGET_UNITS = Decimal("2")
DEFAULT_FLOOR_UNITS = Decimal("2")
_MAXIMUM_FLOOR_UNITS = Decimal("20")
_FUND_LOCK_NAMESPACE = 0x41364652


class ProviderTopupError(A6AutoError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class ProviderTopupContext:
    merchant_id: int
    a6_user_id: int
    session_ref: str
    quota_per_unit: int

    def __post_init__(self) -> None:
        if self.merchant_id <= 0:
            raise ProviderTopupError("provider_topup_merchant_id_invalid")
        if self.a6_user_id <= 0:
            raise ProviderTopupError("provider_topup_user_id_invalid")
        if self.quota_per_unit <= 0:
            raise ProviderTopupError("provider_topup_quota_per_unit_invalid")


def authenticated_provider_topup_client(
    context: ProviderTopupContext,
    *,
    proxy_by_user_id: Mapping[int, str],
    client_factory: AuthenticatedClientFactory | None = None,
) -> Any:
    proxy_url = str(proxy_by_user_id.get(context.a6_user_id) or "").strip()
    if not proxy_url:
        raise ProviderTopupError("provider_topup_proxy_missing")
    try:
        client, _record = authenticated_a6_client(
            context.session_ref,
            expected_user_id=context.a6_user_id,
            proxy_url=proxy_url,
            client_factory=client_factory,
        )
    except AuthenticatedSessionError as exc:
        code = {
            "session_user_identity_mismatch": "provider_topup_session_mismatch",
            "session_user_id_missing": "provider_topup_session_mismatch",
            "session_not_refreshable": "provider_topup_session_mismatch",
            "session_proxy_missing": "provider_topup_proxy_missing",
        }.get(exc.code, f"provider_topup_{exc.code}")
        raise ProviderTopupError(code) from exc
    return client


@dataclass(frozen=True)
class LiveBalance:
    user_balance: int
    withdrawable: int


@dataclass(frozen=True)
class TopupIntent:
    intent_id: int
    amount: int
    user_balance_before: int
    withdrawable_before: int
    fund_reservation_id: int | None = None


@dataclass(frozen=True)
class ProviderTopupOutcome:
    merchant_id: int
    action: str
    applied: bool
    user_balance_before: int
    withdrawable_before: int
    amount: int = 0
    user_balance_after: int | None = None
    shortfall: int = 0
    error_class: str | None = None
    funding_eligible: bool = False
    replacement_required: bool = False
    liquidity_state: str = "UNKNOWN"
    intent_id: int | None = None
    fund_reservation_id: int | None = None

    def public(self) -> dict[str, Any]:
        return {
            "merchant_id": self.merchant_id,
            "action": self.action,
            "applied": self.applied,
            "amount_quota": self.amount,
            "user_balance_before": self.user_balance_before,
            "user_balance_after": self.user_balance_after,
            "withdrawable_before": self.withdrawable_before,
            "shortfall_quota": self.shortfall,
            "error_class": self.error_class,
            "funding_eligible": self.funding_eligible,
            "replacement_required": self.replacement_required,
            "liquidity_state": self.liquidity_state,
            "intent_id": self.intent_id,
            "fund_reservation_id": self.fund_reservation_id,
        }


class ProviderTopupStore(Protocol):
    def providers(self, merchant_ids: Sequence[int]) -> Sequence[ProviderTopupContext]: ...

    def begin_transfer(
        self,
        context: ProviderTopupContext,
        balance: LiveBalance,
        amount: int,
        preview: Mapping[str, Any],
        *,
        idempotency_key: str,
    ) -> TopupIntent: ...

    def load_pending_intent(self, merchant_id: int) -> TopupIntent | None: ...

    def confirm_transfer(
        self,
        merchant_id: int,
        intent: TopupIntent,
        balance_after: LiveBalance,
    ) -> None: ...

    def record_failure(self, merchant_id: int, error_class: str) -> None: ...


class ProviderTopupRemote(Protocol):
    def live_balance(self, context: ProviderTopupContext) -> LiveBalance: ...

    def transfer_preview(
        self, context: ProviderTopupContext, amount: int
    ) -> Mapping[str, Any]: ...

    def transfer_balance(self, context: ProviderTopupContext, amount: int) -> None: ...


class ProviderTopupService:
    """Raise every selected Provider to the balance floor, one merchant at a time."""

    def __init__(
        self,
        store: ProviderTopupStore,
        remote: ProviderTopupRemote,
        *,
        clock: Callable[[], datetime],
        floor_units: Decimal = DEFAULT_FLOOR_UNITS,
        apply: bool = False,
        strict_liquidity: bool = False,
        usable_floor_units: Decimal = DEFAULT_USABLE_FLOOR_UNITS,
        refill_trigger_units: Decimal = DEFAULT_REFILL_TRIGGER_UNITS,
        refill_target_units: Decimal = DEFAULT_REFILL_TARGET_UNITS,
        hard_line_guard: Callable[[ProviderTopupContext, LiveBalance], None] | None = None,
    ) -> None:
        if floor_units < 1 or floor_units > _MAXIMUM_FLOOR_UNITS:
            raise ProviderTopupError("provider_topup_floor_invalid")
        if not isinstance(strict_liquidity, bool):
            raise ProviderTopupError("provider_topup_strict_liquidity_invalid")
        if (
            usable_floor_units < 0
            or refill_trigger_units < usable_floor_units
            or refill_target_units < refill_trigger_units
            or refill_target_units > _MAXIMUM_FLOOR_UNITS
        ):
            raise ProviderTopupError("provider_topup_liquidity_threshold_invalid")
        self.store = store
        self.remote = remote
        self.clock = clock
        self.floor_units = floor_units
        self.apply = apply
        self.strict_liquidity = strict_liquidity
        self.usable_floor_units = usable_floor_units
        self.refill_trigger_units = refill_trigger_units
        self.refill_target_units = refill_target_units
        if hard_line_guard is not None and not callable(hard_line_guard):
            raise ProviderTopupError("provider_topup_hard_line_guard_invalid")
        self.hard_line_guard = hard_line_guard

    def run_once(self, merchant_ids: Sequence[int]) -> dict[str, Any]:
        outcomes: list[ProviderTopupOutcome] = []
        for context in self.store.providers(merchant_ids):
            outcomes.append(self.reconcile_one(context))
        return {
            "success": all(item.error_class is None for item in outcomes),
            "mode": "applied" if self.apply else "preview",
            "floor_units": str(self.floor_units),
            "strict_liquidity": self.strict_liquidity,
            "usable_floor_units": str(self.usable_floor_units),
            "refill_trigger_units": str(self.refill_trigger_units),
            "refill_target_units": str(self.refill_target_units),
            "provider_count": len(outcomes),
            "topped_up": sum(1 for item in outcomes if item.applied),
            "awaiting_settlement": sum(
                1 for item in outcomes if item.action == "awaiting_settlement"
            ),
            "providers": [item.public() for item in outcomes],
        }

    def reconcile_one(
        self,
        context: ProviderTopupContext,
        *,
        allow_new_transfer: bool = True,
    ) -> ProviderTopupOutcome:
        """Reconcile one already-discovered Provider without re-querying selection."""

        if not isinstance(allow_new_transfer, bool):
            raise ProviderTopupError("provider_topup_transfer_gate_invalid")
        try:
            return self._reconcile(context, allow_new_transfer=allow_new_transfer)
        except ProviderTopupError as exc:
            self.store.record_failure(context.merchant_id, exc.code)
            return ProviderTopupOutcome(
                context.merchant_id,
                "failed",
                False,
                0,
                0,
                error_class=exc.code,
            )
        except Exception:
            code = "provider_topup_remote_failed"
            self.store.record_failure(context.merchant_id, code)
            return ProviderTopupOutcome(
                context.merchant_id,
                "failed",
                False,
                0,
                0,
                error_class=code,
            )

    def _reconcile(
        self,
        context: ProviderTopupContext,
        *,
        allow_new_transfer: bool,
    ) -> ProviderTopupOutcome:
        if self.strict_liquidity:
            return self._reconcile_strict(
                context,
                allow_new_transfer=allow_new_transfer,
            )
        # A transfer left in flight by a lost response is reconciled by reading the
        # balance back. Re-posting the same amount could double-move real money.
        pending = self.store.load_pending_intent(context.merchant_id)
        if pending is not None:
            balance_after = self.remote.live_balance(context)
            if not _transfer_landed(pending, balance_after):
                raise ProviderTopupError("provider_topup_transfer_unconfirmed")
            self.store.confirm_transfer(context.merchant_id, pending, balance_after)
            return ProviderTopupOutcome(
                context.merchant_id,
                "reconciled_pending_transfer",
                True,
                pending.user_balance_before,
                pending.withdrawable_before,
                amount=pending.amount,
                user_balance_after=balance_after.user_balance,
            )

        balance = self.remote.live_balance(context)
        floor = int(self.floor_units * context.quota_per_unit)
        if balance.user_balance >= floor:
            return ProviderTopupOutcome(
                context.merchant_id,
                "above_floor",
                False,
                balance.user_balance,
                balance.withdrawable,
            )

        # A6 accepts whole units only, and at least one. Round the deficit up so a
        # single transfer actually clears the floor instead of landing just under it.
        deficit = floor - balance.user_balance
        units = -(-deficit // context.quota_per_unit)
        wanted = units * context.quota_per_unit
        if not allow_new_transfer:
            return ProviderTopupOutcome(
                context.merchant_id,
                "refill_suppressed_for_cashout",
                False,
                balance.user_balance,
                balance.withdrawable,
                shortfall=wanted,
            )
        available_units = balance.withdrawable // context.quota_per_unit
        if available_units < 1:
            # Earnings settle to withdrawable with a delay. This Provider is not broken,
            # it is waiting for money it has already earned, so it keeps its duty and is
            # reported rather than excluded.
            return ProviderTopupOutcome(
                context.merchant_id,
                "awaiting_settlement",
                False,
                balance.user_balance,
                balance.withdrawable,
                shortfall=wanted,
            )
        amount = min(wanted, available_units * context.quota_per_unit)

        if not self.apply:
            return ProviderTopupOutcome(
                context.merchant_id,
                "would_transfer",
                False,
                balance.user_balance,
                balance.withdrawable,
                amount=amount,
                shortfall=max(0, wanted - amount),
            )

        preview = self.remote.transfer_preview(context, amount)
        if preview.get("enabled") is not True:
            raise ProviderTopupError("provider_topup_transfer_disabled")
        # The server is authoritative on what a transfer actually moves.
        net = _integer(preview.get("net_micros"), amount)
        if net <= 0:
            raise ProviderTopupError("provider_topup_preview_net_invalid")
        intent = self.store.begin_transfer(
            context,
            balance,
            amount,
            preview,
            idempotency_key=self._idempotency_key(context, amount),
        )
        self.remote.transfer_balance(context, intent.amount)
        balance_after = self.remote.live_balance(context)
        if not _transfer_landed(intent, balance_after):
            raise ProviderTopupError("provider_topup_transfer_unconfirmed")
        self.store.confirm_transfer(context.merchant_id, intent, balance_after)
        return ProviderTopupOutcome(
            context.merchant_id,
            "topped_up",
            True,
            balance.user_balance,
            balance.withdrawable,
            amount=intent.amount,
            user_balance_after=balance_after.user_balance,
            shortfall=max(0, wanted - intent.amount),
        )

    def _reconcile_strict(
        self,
        context: ProviderTopupContext,
        *,
        allow_new_transfer: bool,
    ) -> ProviderTopupOutcome:
        """Apply the independent refill target; the caller owns error-driven draining."""

        target = int(self.refill_target_units * context.quota_per_unit)
        pending = self.store.load_pending_intent(context.merchant_id)
        if pending is not None:
            balance_after = self.remote.live_balance(context)
            if _transfer_landed(pending, balance_after):
                self.store.confirm_transfer(context.merchant_id, pending, balance_after)
                return ProviderTopupOutcome(
                    context.merchant_id,
                    "reconciled_pending_transfer",
                    True,
                    pending.user_balance_before,
                    pending.withdrawable_before,
                    amount=pending.amount,
                    user_balance_after=balance_after.user_balance,
                    funding_eligible=True,
                    replacement_required=False,
                    liquidity_state="FUNDED",
                    intent_id=pending.intent_id,
                    fund_reservation_id=pending.fund_reservation_id,
                )
            return ProviderTopupOutcome(
                context.merchant_id,
                "transfer_unknown",
                False,
                pending.user_balance_before,
                pending.withdrawable_before,
                user_balance_after=balance_after.user_balance,
                funding_eligible=True,
                replacement_required=False,
                liquidity_state="TRANSFER_UNKNOWN",
                shortfall=0,
                intent_id=pending.intent_id,
                fund_reservation_id=pending.fund_reservation_id,
            )

        balance = self.remote.live_balance(context)
        if balance.user_balance >= target:
            return ProviderTopupOutcome(
                context.merchant_id,
                "above_floor",
                False,
                balance.user_balance,
                balance.withdrawable,
                funding_eligible=True,
                liquidity_state="FUNDED",
            )

        deficit = max(0, target - balance.user_balance)
        units = -(-deficit // context.quota_per_unit)
        wanted = units * context.quota_per_unit
        if not allow_new_transfer:
            return ProviderTopupOutcome(
                context.merchant_id,
                "refill_suppressed_for_cashout",
                False,
                balance.user_balance,
                balance.withdrawable,
                shortfall=wanted,
                funding_eligible=True,
                replacement_required=False,
                liquidity_state="REFILLING",
            )
        available_units = balance.withdrawable // context.quota_per_unit
        if available_units < 1:
            return ProviderTopupOutcome(
                context.merchant_id,
                "refill_deferred",
                False,
                balance.user_balance,
                balance.withdrawable,
                shortfall=wanted,
                funding_eligible=True,
                replacement_required=False,
                liquidity_state="REFILLING",
            )
        amount = min(wanted, available_units * context.quota_per_unit)
        if not self.apply:
            return ProviderTopupOutcome(
                context.merchant_id,
                "would_transfer",
                False,
                balance.user_balance,
                balance.withdrawable,
                amount=amount,
                shortfall=max(0, wanted - amount),
                funding_eligible=True,
                replacement_required=False,
                liquidity_state="REFILLING",
            )
        preview = self.remote.transfer_preview(context, amount)
        if preview.get("enabled") is not True:
            code = "provider_topup_transfer_disabled"
            self.store.record_failure(context.merchant_id, code)
            return ProviderTopupOutcome(
                context.merchant_id,
                "transfer_disabled",
                False,
                balance.user_balance,
                balance.withdrawable,
                amount=amount,
                shortfall=max(0, wanted - amount),
                error_class=code,
                funding_eligible=True,
                replacement_required=False,
                liquidity_state="REFILLING",
            )
        net = _integer(preview.get("net_micros"), amount)
        if net <= 0:
            raise ProviderTopupError("provider_topup_preview_net_invalid")
        intent = self.store.begin_transfer(
            context,
            balance,
            amount,
            preview,
            idempotency_key=self._idempotency_key(context, amount),
        )
        try:
            self.remote.transfer_balance(context, intent.amount)
            balance_after = self.remote.live_balance(context)
        except Exception:
            code = "provider_topup_transfer_unknown"
            self.store.record_failure(context.merchant_id, code)
            return ProviderTopupOutcome(
                context.merchant_id,
                "transfer_unknown",
                False,
                balance.user_balance,
                balance.withdrawable,
                amount=intent.amount,
                shortfall=max(0, wanted - intent.amount),
                error_class=code,
                funding_eligible=True,
                replacement_required=False,
                liquidity_state="TRANSFER_UNKNOWN",
                intent_id=intent.intent_id,
                fund_reservation_id=intent.fund_reservation_id,
            )
        if not _transfer_landed(intent, balance_after):
            code = "provider_topup_transfer_unconfirmed"
            self.store.record_failure(context.merchant_id, code)
            return ProviderTopupOutcome(
                context.merchant_id,
                "transfer_unknown",
                False,
                balance.user_balance,
                balance.withdrawable,
                amount=intent.amount,
                user_balance_after=balance_after.user_balance,
                shortfall=max(0, wanted - intent.amount),
                error_class=code,
                funding_eligible=True,
                replacement_required=False,
                liquidity_state="TRANSFER_UNKNOWN",
                intent_id=intent.intent_id,
                fund_reservation_id=intent.fund_reservation_id,
            )
        try:
            self.store.confirm_transfer(context.merchant_id, intent, balance_after)
        except Exception:
            code = "provider_topup_confirmation_unknown"
            self.store.record_failure(context.merchant_id, code)
            return ProviderTopupOutcome(
                context.merchant_id,
                "transfer_unknown",
                False,
                balance.user_balance,
                balance.withdrawable,
                amount=intent.amount,
                user_balance_after=balance_after.user_balance,
                shortfall=max(0, wanted - intent.amount),
                error_class=code,
                funding_eligible=True,
                replacement_required=False,
                liquidity_state="TRANSFER_UNKNOWN",
                intent_id=intent.intent_id,
                fund_reservation_id=intent.fund_reservation_id,
            )
        return ProviderTopupOutcome(
            context.merchant_id,
            "topped_up",
            True,
            balance.user_balance,
            balance.withdrawable,
            amount=intent.amount,
            user_balance_after=balance_after.user_balance,
            shortfall=max(0, wanted - intent.amount),
            funding_eligible=True,
            replacement_required=False,
            liquidity_state="FUNDED",
            intent_id=intent.intent_id,
            fund_reservation_id=intent.fund_reservation_id,
        )

    def _idempotency_key(self, context: ProviderTopupContext, amount: int) -> str:
        # Bucketed by minute so a retry inside one pass reuses the same intent, while a
        # genuinely later top-up gets its own. Pending intents are reconciled above, so
        # a new bucket can never bypass an in-flight transfer.
        bucket = int(self.clock().timestamp()) // 60
        return (
            f"provider-topup:merchant:{context.merchant_id}"
            f":amount:{amount}:bucket:{bucket}"
        )


def _transfer_landed(intent: TopupIntent, balance_after: LiveBalance) -> bool:
    """Confirm by observed effect, not by the response we may never have received."""

    return (
        balance_after.user_balance >= intent.user_balance_before + intent.amount
        and balance_after.withdrawable <= intent.withdrawable_before - intent.amount
    )


def _integer(value: Any, default: int) -> int:
    if value is None or isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


class PostgresProviderTopupStore:
    """Persists top-up intents in the same table seller retirement already uses."""

    def __init__(self, connection_factory: Callable[[], Any]) -> None:
        self.connection_factory = connection_factory

    def providers(self, merchant_ids: Sequence[int]) -> Sequence[ProviderTopupContext]:
        selected = tuple(sorted({int(value) for value in merchant_ids if int(value) > 0}))
        if not selected:
            raise ProviderTopupError("provider_topup_merchant_selection_empty")
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT id AS merchant_id, a6_user_id, session_ref, quota_per_unit
                    FROM a6_trade.a6_merchants
                    WHERE id = ANY(%s)
                      AND (
                          'provider' = ANY(roles)
                          OR EXISTS (
                              SELECT 1
                              FROM a6_trade.a6_tokens token
                              JOIN a6_trade.a6_channel_booster_tokens booster
                                ON booster.a6_token_ref_id = token.id
                               AND booster.enabled
                              WHERE token.merchant_id = a6_merchants.id
                                AND token.status IN ('1', 'active', 'enabled')
                                AND NOT EXISTS (
                                    SELECT 1
                                    FROM a6_trade.enodot_account_bindings provider_binding
                                    WHERE provider_binding.a6_token_id = token.id
                                )
                                AND NOT EXISTS (
                                    SELECT 1
                                    FROM a6_trade.provider_pool_slots provider_slot
                                    JOIN a6_trade.provider_pool_generations provider_generation
                                      ON provider_generation.id = provider_slot.generation_id
                                    WHERE provider_slot.a6_token_id = token.id
                                      AND provider_slot.valid_to IS NULL
                                      AND provider_slot.slot_state <> 'VACANT'
                                      AND provider_generation.generation_state = 'active'
                                )
                          )
                      )
                      AND a6_user_id IS NOT NULL
                      AND status = 'supplier_submitted'
                    ORDER BY id
                    """,
                    (list(selected),),
                )
                rows = _rows(cursor)
            return tuple(
                ProviderTopupContext(
                    merchant_id=int(row["merchant_id"]),
                    a6_user_id=int(row["a6_user_id"]),
                    session_ref=str(row["session_ref"] or ""),
                    quota_per_unit=int(row["quota_per_unit"] or 0),
                )
                for row in rows
            )
        finally:
            _close(connection)

    def load_pending_intent(self, merchant_id: int) -> TopupIntent | None:
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT id, amount_micros, preview, fund_reservation_id
                    FROM a6_trade.a6_balance_transfer_intents
                    WHERE merchant_id = %s
                      AND state = 'executing'
                      AND idempotency_key LIKE 'provider-topup:%%'
                    ORDER BY id DESC
                    LIMIT 1
                    """,
                    (merchant_id,),
                )
                row = _row(cursor)
            if row is None:
                return None
            preview = _mapping(row.get("preview"))
            return TopupIntent(
                intent_id=int(row["id"]),
                amount=int(row["amount_micros"]),
                user_balance_before=int(preview.get("user_balance_before") or 0),
                withdrawable_before=int(preview.get("withdrawable_before") or 0),
                fund_reservation_id=(
                    int(row["fund_reservation_id"])
                    if row.get("fund_reservation_id") is not None
                    else None
                ),
            )
        finally:
            _close(connection)

    def begin_transfer(
        self,
        context: ProviderTopupContext,
        balance: LiveBalance,
        amount: int,
        preview: Mapping[str, Any],
        *,
        idempotency_key: str,
    ) -> TopupIntent:
        if amount < context.quota_per_unit or amount % context.quota_per_unit != 0:
            raise ProviderTopupError("provider_topup_transfer_units_invalid")
        balance_source_hash = hashlib.sha256(
            _json(
                {
                    "merchant_id": context.merchant_id,
                    "quota_per_unit": context.quota_per_unit,
                    "user_balance": balance.user_balance,
                    "withdrawable": balance.withdrawable,
                }
            ).encode()
        ).hexdigest()
        safe_preview = {
            "enabled": preview.get("enabled") is True,
            "amount_quota": amount,
            "net_micros": _integer(preview.get("net_micros"), amount),
            "fee_micros": _integer(preview.get("fee_micros"), 0),
            "user_balance_before": balance.user_balance,
            "withdrawable_before": balance.withdrawable,
            "balance_source_hash": balance_source_hash,
        }
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT pg_advisory_xact_lock(%s, %s)",
                    (_FUND_LOCK_NAMESPACE, context.merchant_id),
                )
                cursor.execute(
                    """
                    SELECT id
                    FROM a6_trade.merchant_fund_reservations
                    WHERE merchant_id = %s AND reservation_state = 'active'
                    FOR UPDATE
                    """,
                    (context.merchant_id,),
                )
                if _row(cursor) is not None:
                    raise ProviderTopupError("provider_topup_funds_already_reserved")
                cursor.execute(
                    """
                    INSERT INTO a6_trade.a6_balance_transfer_intents (
                        merchant_id, idempotency_key, amount_micros, preview,
                        state, requested_by
                    ) VALUES (%s, %s, %s, %s::jsonb, 'executing', %s)
                    ON CONFLICT (idempotency_key) DO NOTHING
                    RETURNING id, amount_micros, preview
                    """,
                    (
                        context.merchant_id,
                        idempotency_key,
                        amount,
                        _json(safe_preview),
                        "a6-trader-provider-topup",
                    ),
                )
                row = _row(cursor)
                if row is None:
                    raise ProviderTopupError("provider_topup_intent_already_executing")
                stored = _mapping(row.get("preview"))
                intent_id = int(row["id"])
                cursor.execute(
                    """
                    INSERT INTO a6_trade.merchant_fund_reservations (
                        merchant_id, owner_type, owner_intent_id,
                        reserved_micros, reservation_state, balance_source_hash
                    ) VALUES (%s, 'provider_topup', %s, %s, 'active', %s)
                    RETURNING id
                    """,
                    (
                        context.merchant_id,
                        intent_id,
                        amount,
                        balance_source_hash,
                    ),
                )
                reservation = _row(cursor)
                if reservation is None:
                    raise ProviderTopupError("provider_topup_reservation_write_failed")
                reservation_id = int(reservation["id"])
                cursor.execute(
                    """
                    UPDATE a6_trade.a6_balance_transfer_intents
                    SET fund_reservation_id = %s, updated_at = now()
                    WHERE id = %s AND fund_reservation_id IS NULL
                    """,
                    (reservation_id, intent_id),
                )
                if int(getattr(cursor, "rowcount", 0) or 0) != 1:
                    raise ProviderTopupError("provider_topup_reservation_link_failed")
                intent = TopupIntent(
                    intent_id=intent_id,
                    amount=int(row["amount_micros"]),
                    user_balance_before=int(stored.get("user_balance_before") or 0),
                    withdrawable_before=int(stored.get("withdrawable_before") or 0),
                    fund_reservation_id=reservation_id,
                )
            connection.commit()
            return intent
        except Exception:
            connection.rollback()
            raise
        finally:
            _close(connection)

    def confirm_transfer(
        self,
        merchant_id: int,
        intent: TopupIntent,
        balance_after: LiveBalance,
    ) -> None:
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT pg_advisory_xact_lock(%s, %s)",
                    (_FUND_LOCK_NAMESPACE, merchant_id),
                )
                evidence = {
                    "intent_id": intent.intent_id,
                    "amount_quota": intent.amount,
                    "user_balance_before": intent.user_balance_before,
                    "user_balance_after": balance_after.user_balance,
                    "withdrawable_before": intent.withdrawable_before,
                    "withdrawable_after": balance_after.withdrawable,
                }
                cursor.execute(
                    """
                    INSERT INTO a6_trade.a6_balance_transfer_confirmations (
                        intent_id, status, balance_before_micros, balance_after_micros,
                        merchant_available_before_micros,
                        merchant_available_after_micros, observed_at, evidence_hash
                    )
                    SELECT %s, 'confirmed', %s, %s, %s, %s, now(), %s
                    WHERE NOT EXISTS (
                        SELECT 1 FROM a6_trade.a6_balance_transfer_confirmations
                        WHERE intent_id = %s AND status = 'confirmed'
                    )
                    """,
                    (
                        intent.intent_id,
                        intent.user_balance_before,
                        balance_after.user_balance,
                        intent.withdrawable_before,
                        balance_after.withdrawable,
                        hashlib.sha256(_json(evidence).encode()).hexdigest(),
                        intent.intent_id,
                    ),
                )
                cursor.execute(
                    """
                    UPDATE a6_trade.a6_balance_transfer_intents
                    SET state = 'confirmed', updated_at = now()
                    WHERE id = %s
                    """,
                    (intent.intent_id,),
                )
                if intent.fund_reservation_id is not None:
                    cursor.execute(
                        """
                        UPDATE a6_trade.merchant_fund_reservations
                        SET reservation_state = 'released', released_at = now(),
                            updated_at = now()
                        WHERE id = %s
                          AND merchant_id = %s
                          AND owner_type = 'provider_topup'
                          AND owner_intent_id = %s
                          AND reservation_state = 'active'
                        """,
                        (
                            intent.fund_reservation_id,
                            merchant_id,
                            intent.intent_id,
                        ),
                    )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            _close(connection)

    def record_failure(self, merchant_id: int, error_class: str) -> None:
        code = str(error_class or "").strip().lower()[:128] or "provider_topup_failed"
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO a6_trade.trade_incidents (
                        incident_key, incident_class, entity_type, entity_id,
                        severity, status, first_seen_at, last_seen_at
                    ) VALUES (%s, %s, 'merchant', %s, 'warning', 'open', now(), now())
                    ON CONFLICT (incident_key) DO UPDATE SET
                        status = 'open',
                        last_seen_at = now(),
                        occurrence_count =
                            a6_trade.trade_incidents.occurrence_count + 1,
                        resolved_at = NULL,
                        updated_at = now()
                    """,
                    (
                        f"provider-topup:{merchant_id}:{code}",
                        code,
                        str(merchant_id),
                    ),
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            _close(connection)


class A6ProviderTopupRemote:
    """Reads balances and moves merchant balance to user balance over fixed egress."""

    def __init__(
        self,
        client_factory: Callable[[ProviderTopupContext], Any],
    ) -> None:
        self.client_factory = client_factory

    def live_balance(self, context: ProviderTopupContext) -> LiveBalance:
        client = self.client_factory(context)
        user = client.self_user(context.a6_user_id) or {}
        withdraw = client.supplier_withdraw_summary(context.a6_user_id)
        return LiveBalance(
            user_balance=max(0, int(user.get("quota") or 0)),
            withdrawable=max(0, int(withdraw.get("balance_available_micros") or 0)),
        )

    def transfer_preview(
        self, context: ProviderTopupContext, amount: int
    ) -> Mapping[str, Any]:
        return self.client_factory(context).balance_transfer_preview(
            context.a6_user_id,
            amount,
            quota_per_unit=context.quota_per_unit,
        )

    def transfer_balance(self, context: ProviderTopupContext, amount: int) -> None:
        self.client_factory(context).balance_transfer(
            context.a6_user_id,
            amount,
            quota_per_unit=context.quota_per_unit,
        )


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


def _mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return dict(parsed) if isinstance(parsed, Mapping) else {}
    return {}


def _json(value: Mapping[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _close(connection: Any) -> None:
    close = getattr(connection, "close", None)
    if callable(close):
        close()
