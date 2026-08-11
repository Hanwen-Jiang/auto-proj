from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

from .client import A6ApiClient
from .enodot_admin import EnodotAdminClient
from .errors import A6AutoError
from .trade_authenticated_client import (
    AuthenticatedClientFactory,
    AuthenticatedSessionError,
    authenticated_a6_client,
)
from .trade_pin_runner import _balance_transfer_confirmed
from .trade_provider_binding import (
    PostgresProviderBindingStore,
    ProviderBindingError,
    ProviderBindingExecutor,
    ProviderBindingRequest,
)

_ACTIVE_TOKEN_STATES = frozenset({"1", "active", "enabled"})
_RETIREMENT_STATES = frozenset(
    {
        "earning",
        "checking",
        "transfer_unknown",
        "transferred",
        "binding",
        "taking_down",
        "role_switch",
        "draining",
        "completed",
    }
)


class SellerRetirementError(A6AutoError):
    """Stable Seller-retirement error without session or Provider secrets."""


@dataclass(frozen=True)
class SellerRetirementContext:
    merchant_id: int
    a6_user_id: int
    supplier_id: int
    session_ref: str
    proxy_lease_id: str
    roles: tuple[str, ...]
    quota_per_unit: int
    snapshot_withdrawable: int
    state: str
    transfer_intent_id: int | None
    external_a6_token_id: int
    token_name: str
    other_observer_count: int
    archive_mode: bool = False
    # A6's own rolling measurement of the merchant's live channel, in basis points.
    # An off-market Listing that wins the cheapest slot and then fails most of what it
    # wins is not accumulating a retirement fund, it is trading reputation for small
    # change: it should come down now rather than wait to reach the threshold.
    channel_success_rate: int | None = None
    channel_sample_count: int | None = None

    def unproductive(
        self,
        *,
        maximum_success_rate: int,
        minimum_samples: int,
    ) -> bool:
        """True when the channel has enough evidence to call it a losing position."""

        if self.channel_sample_count is None or self.channel_success_rate is None:
            return False
        return (
            self.channel_sample_count >= minimum_samples
            and self.channel_success_rate < maximum_success_rate
        )


@dataclass(frozen=True)
class LiveFinancial:
    user_balance: int
    withdrawable: int


@dataclass(frozen=True)
class TransferIntent:
    intent_id: int
    amount: int
    user_balance_before: int
    withdrawable_before: int


@dataclass(frozen=True)
class SellerRetirementResult:
    merchant_id: int | None
    action: str
    state: str
    success: bool = True
    amount: int = 0
    enodot_account_id: int | None = None
    error_class: str | None = None

    def summary(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "merchant_id": self.merchant_id,
            "action": self.action,
            "state": self.state,
            "amount_quota": self.amount,
            "enodot_account_id": self.enodot_account_id,
            "error_class": self.error_class,
        }


class SellerRetirementStore(Protocol):
    def ensure_targets(self, merchant_ids: Sequence[int]) -> None: ...

    def claim_next(
        self,
        merchant_ids: Sequence[int],
        *,
        snapshot_max_age_seconds: int,
    ) -> SellerRetirementContext | None: ...

    def return_to_earning(
        self,
        merchant_id: int,
        *,
        event_type: str,
        details: Mapping[str, Any],
        retry_after: datetime | None = None,
    ) -> None: ...

    def begin_transfer(
        self,
        context: SellerRetirementContext,
        financial: LiveFinancial,
        preview: Mapping[str, Any],
    ) -> TransferIntent: ...

    def load_transfer_intent(self, merchant_id: int) -> TransferIntent: ...

    def confirm_transfer(
        self,
        merchant_id: int,
        intent: TransferIntent,
        financial_after: LiveFinancial,
    ) -> None: ...

    def ensure_provider_duty(self, merchant_id: int) -> None: ...

    def mark_binding(self, merchant_id: int, account_id: int) -> None: ...

    def complete_retired_duties(self, merchant_id: int) -> None: ...

    def begin_archive_drain(self, merchant_id: int) -> None: ...

    def defer_archive_drain(
        self,
        merchant_id: int,
        *,
        user_balance: int,
        withdrawable: int,
        retry_after: datetime,
    ) -> None: ...

    def complete_archived_duties(self, merchant_id: int) -> None: ...

    def record_failure(
        self,
        merchant_id: int,
        error_class: str,
        *,
        retry_after: datetime,
    ) -> None: ...


class SellerRetirementRemote(Protocol):
    def live_financial(self, context: SellerRetirementContext) -> LiveFinancial: ...

    def transfer_preview(
        self, context: SellerRetirementContext, amount: int
    ) -> Mapping[str, Any]: ...

    def transfer_balance(self, context: SellerRetirementContext, amount: int) -> None: ...

    def ensure_provider_binding(self, context: SellerRetirementContext) -> int: ...


class SellerRetirementService:
    def __init__(
        self,
        store: SellerRetirementStore,
        remote: SellerRetirementRemote,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.store = store
        self.remote = remote
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def run_once(
        self,
        merchant_ids: Sequence[int],
        *,
        min_observers: int = 2,
        snapshot_max_age_seconds: int = 600,
        retire_in_place_maximum_success_rate: int = 0,
        retire_in_place_minimum_samples: int = 30,
    ) -> SellerRetirementResult:
        targets = _merchant_ids(merchant_ids)
        if min_observers < 0:
            raise SellerRetirementError("seller_retirement_min_observers_invalid")
        if snapshot_max_age_seconds < 60:
            raise SellerRetirementError("seller_retirement_snapshot_age_invalid")
        if not 0 <= retire_in_place_maximum_success_rate <= 10000:
            raise SellerRetirementError("seller_retirement_in_place_rate_invalid")
        if retire_in_place_minimum_samples < 10:
            raise SellerRetirementError("seller_retirement_in_place_samples_invalid")
        self.store.ensure_targets(targets)
        context = self.store.claim_next(
            targets,
            snapshot_max_age_seconds=snapshot_max_age_seconds,
            retire_in_place_maximum_success_rate=retire_in_place_maximum_success_rate,
            retire_in_place_minimum_samples=retire_in_place_minimum_samples,
        )
        if context is None:
            return SellerRetirementResult(None, "waiting_threshold", "earning")

        try:
            return self._advance(
                context,
                min_observers=min_observers,
                retire_in_place_maximum_success_rate=(
                    retire_in_place_maximum_success_rate
                ),
                retire_in_place_minimum_samples=retire_in_place_minimum_samples,
            )
        except Exception as exc:
            error_class = _error_code(exc)
            self.store.record_failure(
                context.merchant_id,
                error_class,
                retry_after=self.clock() + timedelta(minutes=5),
            )
            return SellerRetirementResult(
                context.merchant_id,
                "retry_scheduled",
                context.state,
                success=False,
                error_class=error_class,
            )

    def _advance(
        self,
        context: SellerRetirementContext,
        *,
        min_observers: int,
        retire_in_place_maximum_success_rate: int = 0,
        retire_in_place_minimum_samples: int = 30,
    ) -> SellerRetirementResult:
        state = context.state
        intent: TransferIntent | None = None
        transferred_amount = 0
        retire_in_place = not context.archive_mode and context.unproductive(
            maximum_success_rate=retire_in_place_maximum_success_rate,
            minimum_samples=retire_in_place_minimum_samples,
        )

        if state in {"earning", "checking"}:
            if "observer" in context.roles and context.other_observer_count < min_observers:
                self.store.return_to_earning(
                    context.merchant_id,
                    event_type="observer_capacity_guard",
                    details={
                        "other_observer_count": context.other_observer_count,
                        "minimum_observers": min_observers,
                    },
                    retry_after=self.clock() + timedelta(minutes=10),
                )
                return SellerRetirementResult(
                    context.merchant_id,
                    "deferred_observer_capacity",
                    "earning",
                )
            financial = self.remote.live_financial(context)
            if financial.withdrawable < context.quota_per_unit:
                if not retire_in_place:
                    self.store.return_to_earning(
                        context.merchant_id,
                        event_type="live_threshold_not_met",
                        details={
                            "withdrawable_quota": financial.withdrawable,
                            "threshold_quota": context.quota_per_unit,
                        },
                    )
                    return SellerRetirementResult(
                        context.merchant_id,
                        "waiting_threshold",
                        "earning",
                    )
                # A6 rejects a transfer below one quota_per_unit and crypto withdrawal
                # needs $50, so this balance cannot be moved at all yet. Leave it on the
                # merchant, where it keeps accruing, and take the losing Listing down now
                # instead of paying reputation to inch toward the threshold.
                self.store.return_to_earning(
                    context.merchant_id,
                    event_type="retire_in_place_unproductive",
                    details={
                        "withdrawable_quota": financial.withdrawable,
                        "threshold_quota": context.quota_per_unit,
                        "stranded_below_transfer_minimum": True,
                        "channel_success_rate": context.channel_success_rate,
                        "channel_sample_count": context.channel_sample_count,
                    },
                )
                state = "transferred"
            else:
                preview = self.remote.transfer_preview(context, financial.withdrawable)
                if preview.get("enabled") is not True:
                    raise SellerRetirementError("seller_retirement_transfer_disabled")
                intent = self.store.begin_transfer(context, financial, preview)
                state = "transfer_unknown"
                self.remote.transfer_balance(context, intent.amount)
                financial_after = self.remote.live_financial(context)
                if not _transfer_confirmed(intent, financial_after):
                    raise SellerRetirementError(
                        "seller_retirement_transfer_unconfirmed"
                    )
                self.store.confirm_transfer(
                    context.merchant_id,
                    intent,
                    financial_after,
                )
                transferred_amount = intent.amount
                state = "transferred"

        if state == "transfer_unknown":
            intent = intent or self.store.load_transfer_intent(context.merchant_id)
            financial_after = self.remote.live_financial(context)
            if not _transfer_confirmed(intent, financial_after):
                raise SellerRetirementError("seller_retirement_transfer_unconfirmed")
            self.store.confirm_transfer(context.merchant_id, intent, financial_after)
            transferred_amount = intent.amount
            state = "transferred"

        if state == "transferred":
            self.store.ensure_provider_duty(context.merchant_id)
            state = "binding"

        account_id: int | None = None
        if state == "binding":
            account_id = self.remote.ensure_provider_binding(context)
            self.store.mark_binding(context.merchant_id, account_id)
            state = "taking_down"

        if state == "taking_down":
            # Retirement exists to pull off-market inventory out of the pool: the
            # auto-generated pseudo-brand channels (鹭湾066, 海岬103, 榕信125, ...) that
            # do not fit the marketplace. For those, taking the Listing down is the whole
            # point, so it stays.
            #
            # What must NOT follow from this is provider duty. A retired merchant has no
            # Listing, therefore no merchant earnings, therefore nothing to transfer into
            # its A6 user balance -- and A6 refuses to route a merchant under $1. Every
            # merchant retired this way drained to zero and left the provider pool at two
            # usable accounts out of thirteen. Provider duty belongs to merchants that
            # still sell; see `trade_provider_topup` for the funding loop.
            listing_ids = self.remote.takedown_all_listings(context)
            self.store.mark_listings_down(context.merchant_id, listing_ids)
            state = "role_switch"

        if state == "role_switch":
            if context.archive_mode:
                self.store.begin_archive_drain(context.merchant_id)
                state = "draining"
            else:
                self.store.complete_retired_duties(context.merchant_id)
                state = "completed"

        if state == "draining":
            financial = self.remote.live_financial(context)
            if (
                financial.user_balance >= context.quota_per_unit
                or financial.withdrawable >= context.quota_per_unit
            ):
                self.store.defer_archive_drain(
                    context.merchant_id,
                    user_balance=financial.user_balance,
                    withdrawable=financial.withdrawable,
                    retry_after=self.clock() + timedelta(minutes=5),
                )
                return SellerRetirementResult(
                    context.merchant_id,
                    "draining_account_value",
                    "draining",
                    amount=transferred_amount,
                    enodot_account_id=account_id,
                )
            self.store.complete_archived_duties(context.merchant_id)
            state = "completed"

        return SellerRetirementResult(
            context.merchant_id,
            "archived_to_observer" if context.archive_mode else "retired_to_provider_observer",
            state,
            amount=transferred_amount,
            enodot_account_id=account_id,
        )


class PostgresSellerRetirementStore:
    def __init__(self, connection_factory: Callable[[], Any]) -> None:
        self.connection_factory = connection_factory

    def ensure_targets(self, merchant_ids: Sequence[int]) -> None:
        targets = _merchant_ids(merchant_ids)
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT id FROM a6_trade.a6_merchants
                    WHERE id = ANY(%s)
                    """,
                    (list(targets),),
                )
                found = {int(row["id"]) for row in _rows(cursor)}
                if found != set(targets):
                    raise SellerRetirementError("seller_retirement_target_missing")
                cursor.execute(
                    """
                    INSERT INTO a6_trade.a6_seller_retirement_plans (
                        merchant_id, threshold_quota
                    )
                    SELECT id, quota_per_unit
                    FROM a6_trade.a6_merchants
                    WHERE id = ANY(%s)
                    ON CONFLICT (merchant_id) DO UPDATE SET
                        threshold_quota = EXCLUDED.threshold_quota,
                        updated_at = CASE
                            WHEN a6_seller_retirement_plans.threshold_quota
                                IS DISTINCT FROM EXCLUDED.threshold_quota
                            THEN now() ELSE a6_seller_retirement_plans.updated_at
                        END
                    """,
                    (list(targets),),
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            _close(connection)

    def claim_next(
        self,
        merchant_ids: Sequence[int],
        *,
        snapshot_max_age_seconds: int,
        retire_in_place_maximum_success_rate: int = 0,
        retire_in_place_minimum_samples: int = 30,
    ) -> SellerRetirementContext | None:
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT p.merchant_id, m.a6_user_id, m.supplier_id, m.session_ref,
                           coalesce(m.proxy_lease_id, '') AS proxy_lease_id,
                           m.roles, m.quota_per_unit,
                           coalesce(f.withdrawable_micros, 0) AS snapshot_withdrawable,
                           p.state, p.transfer_intent_id, p.archive_mode,
                           token.a6_token_id AS external_a6_token_id,
                           token.token_name,
                           (
                               SELECT count(*)
                               FROM a6_trade.a6_merchants observer
                               WHERE observer.id <> m.id
                                 AND 'observer' = ANY(observer.roles)
                                 AND observer.status = 'supplier_submitted'
                           ) AS other_observer_count,
                           health.recent_success_rate AS channel_success_rate,
                           health.sample_count AS channel_sample_count
                    FROM a6_trade.a6_seller_retirement_plans p
                    JOIN a6_trade.a6_merchants m ON m.id = p.merchant_id
                    LEFT JOIN a6_trade.a6_financial_current f ON f.merchant_id = m.id
                    LEFT JOIN LATERAL (
                        SELECT signal.recent_success_rate, signal.sample_count
                        FROM a6_trade.a6_supplier_listings_current listing
                        JOIN a6_trade.a6_market_channel_signals_current signal
                          ON signal.channel_id = listing.a6_channel_id
                         AND signal.model = listing.platform_model
                        WHERE listing.merchant_id = m.id
                          AND listing.status = 2
                          AND listing.availability = 1
                        ORDER BY signal.sample_count DESC NULLS LAST
                        LIMIT 1
                    ) health ON true
                    JOIN LATERAL (
                        SELECT t.a6_token_id, t.token_name
                        FROM a6_trade.a6_tokens t
                        WHERE t.merchant_id = m.id
                          AND lower(t.status) = ANY(%s)
                        ORDER BY t.id
                        LIMIT 1
                    ) token ON true
                    WHERE p.merchant_id = ANY(%s)
                      AND p.state <> 'completed'
                      AND (p.retry_after IS NULL OR p.retry_after <= now())
                      AND (
                          p.state NOT IN ('earning', 'checking')
                          OR (
                              f.withdrawable_micros >= p.threshold_quota
                              AND f.observed_at >= now() - (%s * interval '1 second')
                          )
                          -- Retire in place: a channel with enough samples and a low
                          -- success rate never reaches the threshold, it just keeps
                          -- losing. Claim it without waiting.
                          OR (
                              NOT p.archive_mode
                              AND
                              health.sample_count >= %s
                              AND health.recent_success_rate < %s
                          )
                      )
                      AND (
                          p.state <> 'checking'
                          OR p.updated_at < now() - interval '10 minutes'
                      )
                    ORDER BY
                        CASE WHEN p.state IN ('earning', 'checking') THEN 1 ELSE 0 END,
                        f.withdrawable_micros DESC NULLS LAST,
                        p.updated_at,
                        p.merchant_id
                    FOR UPDATE OF p SKIP LOCKED
                    LIMIT 1
                    """,
                    (
                        list(_ACTIVE_TOKEN_STATES),
                        list(_merchant_ids(merchant_ids)),
                        snapshot_max_age_seconds,
                        retire_in_place_minimum_samples,
                        retire_in_place_maximum_success_rate,
                    ),
                )
                row = _row(cursor)
                if row is None:
                    connection.commit()
                    return None
                previous_state = str(row["state"])
                claimed_state = "checking" if previous_state == "earning" else previous_state
                cursor.execute(
                    """
                    UPDATE a6_trade.a6_seller_retirement_plans
                    SET state = %s, attempt_count = attempt_count + 1,
                        last_started_at = now(), last_error_class = NULL,
                        retry_after = NULL, updated_at = now()
                    WHERE merchant_id = %s
                    """,
                    (claimed_state, int(row["merchant_id"])),
                )
                if claimed_state != previous_state:
                    self._event(
                        cursor,
                        int(row["merchant_id"]),
                        "threshold_claimed",
                        previous_state,
                        claimed_state,
                        {"snapshot_withdrawable": int(row["snapshot_withdrawable"] or 0)},
                    )
            connection.commit()
            row["state"] = claimed_state
            return _context(row)
        except Exception:
            connection.rollback()
            raise
        finally:
            _close(connection)

    def return_to_earning(
        self,
        merchant_id: int,
        *,
        event_type: str,
        details: Mapping[str, Any],
        retry_after: datetime | None = None,
    ) -> None:
        self._transition(
            merchant_id,
            "earning",
            event_type,
            details,
            retry_after=retry_after,
        )

    def begin_transfer(
        self,
        context: SellerRetirementContext,
        financial: LiveFinancial,
        preview: Mapping[str, Any],
    ) -> TransferIntent:
        amount = financial.withdrawable
        safe_preview = {
            "enabled": preview.get("enabled") is True,
            "amount_quota": amount,
            "user_balance_before": financial.user_balance,
            "withdrawable_before": financial.withdrawable,
        }
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO a6_trade.a6_balance_transfer_intents (
                        merchant_id, idempotency_key, amount_micros, preview,
                        state, requested_by
                    ) VALUES (%s, %s, %s, %s::jsonb, 'executing', %s)
                    ON CONFLICT (idempotency_key) DO UPDATE SET
                        updated_at = now()
                    RETURNING id, amount_micros, preview
                    """,
                    (
                        context.merchant_id,
                        f"seller-retirement:merchant:{context.merchant_id}:transfer:v1",
                        amount,
                        _json(safe_preview),
                        "a6-trader-seller-retirement",
                    ),
                )
                row = _row(cursor)
                if row is None:
                    raise SellerRetirementError("seller_retirement_intent_write_failed")
                stored_preview = _mapping(row.get("preview"))
                intent = TransferIntent(
                    intent_id=int(row["id"]),
                    amount=int(row["amount_micros"]),
                    user_balance_before=int(stored_preview.get("user_balance_before") or 0),
                    withdrawable_before=int(stored_preview.get("withdrawable_before") or 0),
                )
                cursor.execute(
                    """
                    UPDATE a6_trade.a6_seller_retirement_plans
                    SET state = 'transfer_unknown', transfer_intent_id = %s,
                        external_a6_token_id = %s, updated_at = now()
                    WHERE merchant_id = %s
                    """,
                    (intent.intent_id, context.external_a6_token_id, context.merchant_id),
                )
                self._event(
                    cursor,
                    context.merchant_id,
                    "balance_transfer_started",
                    context.state,
                    "transfer_unknown",
                    {"intent_id": intent.intent_id, "amount_quota": intent.amount},
                )
            connection.commit()
            return intent
        except Exception:
            connection.rollback()
            raise
        finally:
            _close(connection)

    def load_transfer_intent(self, merchant_id: int) -> TransferIntent:
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT i.id, i.amount_micros, i.preview
                    FROM a6_trade.a6_seller_retirement_plans p
                    JOIN a6_trade.a6_balance_transfer_intents i
                      ON i.id = p.transfer_intent_id
                    WHERE p.merchant_id = %s
                    """,
                    (merchant_id,),
                )
                row = _row(cursor)
            if row is None:
                raise SellerRetirementError("seller_retirement_intent_missing")
            preview = _mapping(row.get("preview"))
            return TransferIntent(
                int(row["id"]),
                int(row["amount_micros"]),
                int(preview.get("user_balance_before") or 0),
                int(preview.get("withdrawable_before") or 0),
            )
        finally:
            _close(connection)

    def confirm_transfer(
        self,
        merchant_id: int,
        intent: TransferIntent,
        financial_after: LiveFinancial,
    ) -> None:
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                evidence = {
                    "intent_id": intent.intent_id,
                    "amount_quota": intent.amount,
                    "user_balance_before": intent.user_balance_before,
                    "user_balance_after": financial_after.user_balance,
                    "withdrawable_before": intent.withdrawable_before,
                    "withdrawable_after": financial_after.withdrawable,
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
                        financial_after.user_balance,
                        intent.withdrawable_before,
                        financial_after.withdrawable,
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
                cursor.execute(
                    """
                    UPDATE a6_trade.a6_seller_retirement_plans
                    SET state = 'transferred', last_error_class = NULL,
                        retry_after = NULL, updated_at = now()
                    WHERE merchant_id = %s
                    """,
                    (merchant_id,),
                )
                self._event(
                    cursor,
                    merchant_id,
                    "balance_transfer_confirmed",
                    "transfer_unknown",
                    "transferred",
                    evidence,
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            _close(connection)

    def ensure_provider_duty(self, merchant_id: int) -> None:
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT roles, role_revision
                    FROM a6_trade.a6_merchants
                    WHERE id = %s FOR UPDATE
                    """,
                    (merchant_id,),
                )
                row = _row(cursor)
                if row is None:
                    raise SellerRetirementError("seller_retirement_merchant_missing")
                before = tuple(str(role) for role in row["roles"])
                if before == ("provider",):
                    after = before
                elif before in {
                    ("seller",),
                    ("seller", "provider"),
                    ("seller", "observer"),
                }:
                    after = ("seller", "provider")
                else:
                    raise SellerRetirementError("seller_retirement_role_invalid")
                if after != before:
                    cursor.execute(
                        """
                        UPDATE a6_trade.a6_merchants
                        SET roles = %s, role_revision = role_revision + 1,
                            updated_at = now()
                        WHERE id = %s
                        """,
                        (list(after), merchant_id),
                    )
                self._audit_role(cursor, merchant_id, before, after, "prepare_provider")
                cursor.execute(
                    """
                    UPDATE a6_trade.a6_seller_retirement_plans
                    SET state = 'binding', updated_at = now()
                    WHERE merchant_id = %s
                    """,
                    (merchant_id,),
                )
                self._event(
                    cursor,
                    merchant_id,
                    "provider_duty_prepared",
                    "transferred",
                    "binding",
                    {"roles_before": list(before), "roles_after": list(after)},
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            _close(connection)

    def mark_binding(self, merchant_id: int, account_id: int) -> None:
        self._transition(
            merchant_id,
            "taking_down",
            "provider_binding_verified",
            {"enodot_account_id": account_id},
            enodot_account_id=account_id,
        )

    def complete_retired_duties(self, merchant_id: int) -> None:
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT m.roles, b.state AS binding_state, b.enodot_account_id
                    FROM a6_trade.a6_merchants m
                    JOIN a6_trade.a6_seller_retirement_plans p ON p.merchant_id = m.id
                    JOIN a6_trade.a6_tokens t
                      ON t.merchant_id = m.id AND t.a6_token_id = p.external_a6_token_id
                    JOIN a6_trade.enodot_account_bindings b ON b.a6_token_id = t.id
                    WHERE m.id = %s AND p.state = 'role_switch'
                    FOR UPDATE OF m, p
                    """,
                    (merchant_id,),
                )
                row = _row(cursor)
                if row is None or row.get("binding_state") != "bound":
                    raise SellerRetirementError("seller_retirement_binding_not_ready")
                before = tuple(str(role) for role in row["roles"])
                if before not in {
                    ("seller", "provider"),
                    ("provider",),
                    ("provider", "observer"),
                }:
                    raise SellerRetirementError("seller_retirement_role_invalid")
                # Retirement only withdraws the merchant from price-band allocation. It
                # keeps the seller role, because a Provider funds its own A6 spend out
                # of its own seller earnings: stripping `seller` removed the income and
                # every retired Provider then spent itself below the $1 usability floor.
                after = ("seller", "provider")
                if before != after:
                    cursor.execute(
                        """
                        UPDATE a6_trade.a6_merchants
                        SET roles = ARRAY['seller', 'provider']::text[],
                            role_revision = role_revision + 1, updated_at = now()
                        WHERE id = %s
                        """,
                        (merchant_id,),
                    )
                self._audit_role(
                    cursor,
                    merchant_id,
                    before,
                    after,
                    "retire_seller",
                )
                # Growth policies are deliberately left alone. Disabling them here was a
                # one-way switch with no counterpart that ever re-enabled them, so with
                # this timer running every 60s the fleet's growth capability decayed
                # monotonically to zero. Suppression is now time-bounded instead.
                cursor.execute(
                    """
                    UPDATE a6_trade.a6_seller_retirement_plans
                    SET state = 'completed', completed_at = now(),
                        last_error_class = NULL, retry_after = NULL, updated_at = now()
                    WHERE merchant_id = %s
                    """,
                    (merchant_id,),
                )
                self._event(
                    cursor,
                    merchant_id,
                    "seller_retired",
                    "role_switch",
                    "completed",
                    {
                        "roles_before": list(before),
                        "roles_after": list(after),
                        "enodot_account_id": int(row["enodot_account_id"]),
                    },
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            _close(connection)

    def begin_archive_drain(self, merchant_id: int) -> None:
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT m.roles, b.state AS binding_state, b.enodot_account_id
                    FROM a6_trade.a6_merchants m
                    JOIN a6_trade.a6_seller_retirement_plans p ON p.merchant_id = m.id
                    JOIN a6_trade.a6_tokens t
                      ON t.merchant_id = m.id AND t.a6_token_id = p.external_a6_token_id
                    JOIN a6_trade.enodot_account_bindings b ON b.a6_token_id = t.id
                    WHERE m.id = %s AND p.state = 'role_switch' AND p.archive_mode
                    FOR UPDATE OF m, p
                    """,
                    (merchant_id,),
                )
                row = _row(cursor)
                if row is None or row.get("binding_state") != "bound":
                    raise SellerRetirementError("seller_retirement_binding_not_ready")
                before = tuple(str(role) for role in row["roles"])
                if before not in {
                    ("seller", "provider"),
                    ("provider",),
                    ("provider", "observer"),
                }:
                    raise SellerRetirementError("seller_retirement_role_invalid")
                after = ("provider", "observer")
                if before != after:
                    cursor.execute(
                        """
                        UPDATE a6_trade.a6_merchants
                        SET roles = ARRAY['provider', 'observer']::text[],
                            role_revision = role_revision + 1, updated_at = now()
                        WHERE id = %s
                        """,
                        (merchant_id,),
                    )
                self._audit_role(cursor, merchant_id, before, after, "begin_safe_archive")
                cursor.execute(
                    """
                    UPDATE a6_trade.a6_seller_retirement_plans
                    SET state = 'draining', last_error_class = NULL,
                        retry_after = NULL, updated_at = now()
                    WHERE merchant_id = %s
                    """,
                    (merchant_id,),
                )
                self._event(
                    cursor,
                    merchant_id,
                    "safe_archive_drain_started",
                    "role_switch",
                    "draining",
                    {
                        "roles_before": list(before),
                        "roles_after": list(after),
                        "enodot_account_id": int(row["enodot_account_id"]),
                    },
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            _close(connection)

    def defer_archive_drain(
        self,
        merchant_id: int,
        *,
        user_balance: int,
        withdrawable: int,
        retry_after: datetime,
    ) -> None:
        self._transition(
            merchant_id,
            "draining",
            "safe_archive_value_remaining",
            {
                "user_balance_quota": max(0, int(user_balance)),
                "withdrawable_quota": max(0, int(withdrawable)),
            },
            retry_after=retry_after,
        )

    def complete_archived_duties(self, merchant_id: int) -> None:
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT roles FROM a6_trade.a6_merchants
                    WHERE id = %s FOR UPDATE
                    """,
                    (merchant_id,),
                )
                row = _row(cursor)
                if row is None:
                    raise SellerRetirementError("seller_retirement_merchant_missing")
                before = tuple(str(role) for role in row["roles"])
                if before not in {("provider", "observer"), ("observer",)}:
                    raise SellerRetirementError("seller_retirement_role_invalid")
                after = ("observer",)
                if before != after:
                    cursor.execute(
                        """
                        UPDATE a6_trade.a6_merchants
                        SET roles = ARRAY['observer']::text[],
                            role_revision = role_revision + 1, updated_at = now()
                        WHERE id = %s
                        """,
                        (merchant_id,),
                    )
                self._audit_role(cursor, merchant_id, before, after, "complete_safe_archive")
                cursor.execute(
                    """
                    UPDATE a6_trade.a6_seller_retirement_plans
                    SET state = 'completed', completed_at = now(),
                        last_error_class = NULL, retry_after = NULL, updated_at = now()
                    WHERE merchant_id = %s AND state = 'draining' AND archive_mode
                    """,
                    (merchant_id,),
                )
                if cursor.rowcount != 1:
                    raise SellerRetirementError("seller_retirement_state_invalid")
                self._event(
                    cursor,
                    merchant_id,
                    "safe_archive_completed",
                    "draining",
                    "completed",
                    {"roles_before": list(before), "roles_after": list(after)},
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            _close(connection)

    def record_failure(
        self,
        merchant_id: int,
        error_class: str,
        *,
        retry_after: datetime,
    ) -> None:
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE a6_trade.a6_seller_retirement_plans
                    SET last_error_class = %s, retry_after = %s, updated_at = now()
                    WHERE merchant_id = %s
                    RETURNING state
                    """,
                    (error_class, retry_after, merchant_id),
                )
                row = _row(cursor)
                if row is not None:
                    self._event(
                        cursor,
                        merchant_id,
                        "step_failed",
                        str(row["state"]),
                        str(row["state"]),
                        {"error_class": error_class, "retry_after": retry_after.isoformat()},
                    )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            _close(connection)

    def _transition(
        self,
        merchant_id: int,
        state: str,
        event_type: str,
        details: Mapping[str, Any],
        *,
        retry_after: datetime | None = None,
        enodot_account_id: int | None = None,
    ) -> None:
        if state not in _RETIREMENT_STATES:
            raise SellerRetirementError("seller_retirement_state_invalid")
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT state FROM a6_trade.a6_seller_retirement_plans
                    WHERE merchant_id = %s FOR UPDATE
                    """,
                    (merchant_id,),
                )
                row = _row(cursor)
                if row is None:
                    raise SellerRetirementError("seller_retirement_plan_missing")
                previous = str(row["state"])
                cursor.execute(
                    """
                    UPDATE a6_trade.a6_seller_retirement_plans
                    SET state = %s, retry_after = %s,
                        enodot_account_id = coalesce(%s, enodot_account_id),
                        last_error_class = NULL, updated_at = now()
                    WHERE merchant_id = %s
                    """,
                    (state, retry_after, enodot_account_id, merchant_id),
                )
                self._event(cursor, merchant_id, event_type, previous, state, details)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            _close(connection)

    @staticmethod
    def _event(
        cursor: Any,
        merchant_id: int,
        event_type: str,
        from_state: str | None,
        to_state: str | None,
        details: Mapping[str, Any],
    ) -> None:
        cursor.execute(
            """
            INSERT INTO a6_trade.a6_seller_retirement_events (
                merchant_id, event_type, from_state, to_state, details
            ) VALUES (%s, %s, %s, %s, %s::jsonb)
            """,
            (merchant_id, event_type, from_state, to_state, _json(details)),
        )

    @staticmethod
    def _audit_role(
        cursor: Any,
        merchant_id: int,
        before: tuple[str, ...],
        after: tuple[str, ...],
        operation: str,
    ) -> None:
        desired = {"merchant_id": merchant_id, "roles": list(after), "operation": operation}
        cursor.execute(
            """
            INSERT INTO a6_trade.trade_control_actions (
                idempotency_key, action_type, entity_type, entity_id,
                desired_state, observed_before, observed_after, status,
                config_version, requested_by, started_at, completed_at
            ) VALUES (
                %s, 'retire_seller_role', 'a6_merchant', %s,
                %s::jsonb, %s::jsonb, %s::jsonb, 'completed',
                'seller-retirement-v1', 'a6-trader-seller-retirement', now(), now()
            ) ON CONFLICT (idempotency_key) DO NOTHING
            """,
            (
                f"seller-retirement:merchant:{merchant_id}:role:{operation}:v1",
                str(merchant_id),
                _json(desired),
                _json({"roles": list(before)}),
                _json({"roles": list(after)}),
            ),
        )


class A6SellerRetirementRemote:
    def __init__(
        self,
        connection_factory: Callable[[], Any],
        *,
        proxy_by_user_id: Mapping[int, str],
        enodot_admin: EnodotAdminClient,
        group_id: int = 26,
        model: str = "gpt-5.6-sol",
        concurrency: int = 100,
        a6_client_factory: AuthenticatedClientFactory | None = None,
    ) -> None:
        self.connection_factory = connection_factory
        self.proxy_by_user_id = dict(proxy_by_user_id)
        self.enodot_admin = enodot_admin
        self.group_id = int(group_id)
        self.model = str(model)
        self.concurrency = int(concurrency)
        self.a6_client_factory = a6_client_factory

    def live_financial(self, context: SellerRetirementContext) -> LiveFinancial:
        client = self._client(context)
        user = client.self_user(context.a6_user_id) or {}
        withdraw = client.supplier_withdraw_summary(context.a6_user_id)
        return LiveFinancial(
            user_balance=max(0, int(user.get("quota") or 0)),
            withdrawable=max(0, int(withdraw.get("balance_available_micros") or 0)),
        )

    def transfer_preview(
        self,
        context: SellerRetirementContext,
        amount: int,
    ) -> Mapping[str, Any]:
        return self._client(context).balance_transfer_preview(
            context.a6_user_id,
            amount,
            quota_per_unit=context.quota_per_unit,
        )

    def transfer_balance(self, context: SellerRetirementContext, amount: int) -> None:
        self._client(context).balance_transfer(
            context.a6_user_id,
            amount,
            quota_per_unit=context.quota_per_unit,
        )

    def ensure_provider_binding(self, context: SellerRetirementContext) -> int:
        proxy_id = self._proxy_id(context.proxy_lease_id)
        executor = ProviderBindingExecutor(
            PostgresProviderBindingStore(self.connection_factory),
            self.enodot_admin,
            key_revealer=self._reveal_key,
        )
        result = executor.execute(
            ProviderBindingRequest(
                merchant_id=context.merchant_id,
                external_a6_token_id=context.external_a6_token_id,
                proxy_id=proxy_id,
                group_ids=(self.group_id,),
                model_scope=(self.model,),
                model_mapping={self.model: self.model},
                concurrency=self.concurrency,
                priority=1,
                config_version="seller-retirement-provider-v1",
                requested_by="a6-trader-seller-retirement",
            ),
            apply=True,
        )
        if not result.verified or result.account_id is None:
            raise SellerRetirementError("seller_retirement_provider_binding_unverified")
        return result.account_id

    def _client(self, context: SellerRetirementContext) -> A6ApiClient:
        proxy_url = self.proxy_by_user_id.get(context.a6_user_id)
        if not proxy_url:
            raise SellerRetirementError("seller_retirement_proxy_missing")
        try:
            client, record = authenticated_a6_client(
                context.session_ref,
                expected_user_id=context.a6_user_id,
                proxy_url=proxy_url,
                client_factory=self.a6_client_factory,
            )
        except AuthenticatedSessionError as exc:
            code = {
                "session_user_identity_mismatch": "seller_retirement_session_mismatch",
                "session_user_id_missing": "seller_retirement_session_mismatch",
                "session_not_refreshable": "seller_retirement_session_mismatch",
                "session_proxy_missing": "seller_retirement_proxy_missing",
            }.get(exc.code, f"seller_retirement_{exc.code}")
            raise SellerRetirementError(code) from exc
        if record.supplier_id != context.supplier_id:
            raise SellerRetirementError("seller_retirement_session_mismatch")
        return client

    def _proxy_id(self, proxy_lease_id: str) -> int:
        if not proxy_lease_id.startswith("singbox:"):
            raise SellerRetirementError("seller_retirement_proxy_lease_invalid")
        port = int(proxy_lease_id.split(":", 1)[1])
        inventory = self.enodot_admin.request("GET", "admin/proxies/all")
        if not isinstance(inventory, Sequence) or isinstance(inventory, (str, bytes)):
            raise SellerRetirementError("seller_retirement_proxy_inventory_invalid")
        matches = [
            item
            for item in inventory
            if isinstance(item, Mapping)
            and int(item.get("port") or 0) == port
            and str(item.get("status") or "").casefold() == "active"
        ]
        if len(matches) != 1 or int(matches[0].get("id") or 0) < 1:
            raise SellerRetirementError("seller_retirement_proxy_match_invalid")
        return int(matches[0]["id"])

    def _reveal_key(self, context: Any, _record: Any) -> str:
        proxy_url = self.proxy_by_user_id.get(context.a6_user_id)
        if not proxy_url:
            raise ProviderBindingError("provider_binding_proxy_mapping_missing")
        try:
            client, _current = authenticated_a6_client(
                context.session_ref,
                expected_user_id=context.a6_user_id,
                proxy_url=proxy_url,
                client_factory=self.a6_client_factory,
            )
        except AuthenticatedSessionError as exc:
            raise ProviderBindingError(f"provider_binding_{exc.code}") from exc
        matches = [
            item
            for item in client.list_tokens(context.a6_user_id)
            if int(item.get("id") or 0) == context.external_a6_token_id
        ]
        if len(matches) != 1 or str(matches[0].get("name") or "") != context.token_name:
            raise ProviderBindingError("provider_binding_token_readback_mismatch")
        status, document = client._request(
            "POST",
            f"/api/token/{context.external_a6_token_id}/key",
            user_id=context.a6_user_id,
            referer="/console/token",
        )
        data = document.get("data")
        key = str(data.get("key") or "").strip() if isinstance(data, Mapping) else ""
        if status != 200 or document.get("success") is not True or not key:
            raise ProviderBindingError("provider_binding_key_reveal_failed")
        return key


def _transfer_confirmed(intent: TransferIntent, financial_after: LiveFinancial) -> bool:
    return _balance_transfer_confirmed(
        user_balance_before=intent.user_balance_before,
        withdrawable_before=intent.withdrawable_before,
        user_balance_after=financial_after.user_balance,
        withdrawable_after=financial_after.withdrawable,
        amount=intent.amount,
    )


def _context(row: Mapping[str, Any]) -> SellerRetirementContext:
    try:
        context = SellerRetirementContext(
            merchant_id=int(row["merchant_id"]),
            a6_user_id=int(row["a6_user_id"]),
            supplier_id=int(row["supplier_id"]),
            session_ref=str(row["session_ref"]),
            proxy_lease_id=str(row["proxy_lease_id"]),
            roles=tuple(str(role) for role in row["roles"]),
            quota_per_unit=int(row["quota_per_unit"]),
            snapshot_withdrawable=int(row["snapshot_withdrawable"] or 0),
            state=str(row["state"]),
            transfer_intent_id=(
                int(row["transfer_intent_id"])
                if row.get("transfer_intent_id") is not None
                else None
            ),
            external_a6_token_id=int(row["external_a6_token_id"]),
            token_name=str(row["token_name"]),
            other_observer_count=int(row["other_observer_count"] or 0),
            archive_mode=row.get("archive_mode") is True,
            channel_success_rate=(
                int(row["channel_success_rate"])
                if row.get("channel_success_rate") is not None
                else None
            ),
            channel_sample_count=(
                int(row["channel_sample_count"])
                if row.get("channel_sample_count") is not None
                else None
            ),
        )
    except (KeyError, TypeError, ValueError):
        raise SellerRetirementError("seller_retirement_context_invalid") from None
    if (
        min(
            context.merchant_id,
            context.a6_user_id,
            context.supplier_id,
            context.quota_per_unit,
            context.external_a6_token_id,
        )
        < 1
        or context.state not in _RETIREMENT_STATES
        or not context.session_ref
        or not context.token_name
    ):
        raise SellerRetirementError("seller_retirement_context_invalid")
    return context


def _merchant_ids(values: Sequence[int]) -> tuple[int, ...]:
    if not values or any(isinstance(value, bool) or int(value) < 1 for value in values):
        raise SellerRetirementError("seller_retirement_targets_invalid")
    normalized = tuple(sorted({int(value) for value in values}))
    if len(normalized) != len(values):
        raise SellerRetirementError("seller_retirement_targets_invalid")
    return normalized


def _row(cursor: Any) -> dict[str, Any] | None:
    value = cursor.fetchone()
    if value is None:
        return None
    if isinstance(value, Mapping):
        return dict(value)
    names = [str(getattr(column, "name", column[0])) for column in cursor.description]
    return dict(zip(names, value, strict=True))


def _rows(cursor: Any) -> list[dict[str, Any]]:
    values = cursor.fetchall()
    if not values:
        return []
    if isinstance(values[0], Mapping):
        return [dict(value) for value in values]
    names = [str(getattr(column, "name", column[0])) for column in cursor.description]
    return [dict(zip(names, value, strict=True)) for value in values]


def _mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, Mapping):
            return dict(parsed)
    raise SellerRetirementError("seller_retirement_json_invalid")


def _json(value: Mapping[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _error_code(exc: Exception) -> str:
    code = str(getattr(exc, "code", "") or str(exc)).strip()
    if code.startswith(("seller_retirement_", "provider_binding_")):
        return code
    return f"seller_retirement_{type(exc).__name__.lower()}"


def _close(connection: Any) -> None:
    close = getattr(connection, "close", None)
    if callable(close):
        close()
