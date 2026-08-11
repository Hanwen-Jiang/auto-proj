from __future__ import annotations

import inspect
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from a6api_auto.trade_provider_topup import (
    LiveBalance,
    PostgresProviderTopupStore,
    ProviderTopupContext,
    ProviderTopupError,
    ProviderTopupService,
    TopupIntent,
)

NOW = datetime(2026, 7, 26, 11, 30, tzinfo=UTC)
UNIT = 500_000


def test_topup_selection_includes_enabled_callback_boosters() -> None:
    source = inspect.getsource(PostgresProviderTopupStore.providers)

    assert "'provider' = ANY(roles)" in source
    assert "a6_channel_booster_tokens booster" in source
    assert "booster.enabled" in source
    assert "token.status IN ('1', 'active', 'enabled')" in source


def test_topup_booster_branch_excludes_reserved_and_active_provider_tokens() -> None:
    source = " ".join(inspect.getsource(PostgresProviderTopupStore.providers).split())

    assert "provider_binding.a6_token_id = token.id" in source
    assert "provider_binding.state = 'bound'" not in source
    assert "provider_slot.a6_token_id = token.id" in source
    assert "provider_slot.valid_to IS NULL" in source
    assert "provider_slot.slot_state <> 'VACANT'" in source
    assert "provider_generation.generation_state = 'active'" in source


def test_topup_intent_atomically_claims_shared_merchant_funds() -> None:
    source = " ".join(inspect.getsource(PostgresProviderTopupStore.begin_transfer).split())

    assert "pg_advisory_xact_lock" in source
    assert "merchant_fund_reservations" in source
    assert "reservation_state = 'active'" in source
    assert "ON CONFLICT (idempotency_key) DO NOTHING" in source
    assert "fund_reservation_id" in source
    assert "amount % context.quota_per_unit" in source


def test_confirmed_topup_releases_its_shared_fund_claim() -> None:
    source = " ".join(inspect.getsource(PostgresProviderTopupStore.confirm_transfer).split())

    assert "pg_advisory_xact_lock" in source
    assert "reservation_state = 'released'" in source
    assert "owner_type = 'provider_topup'" in source


def context(merchant_id: int = 9, **overrides) -> ProviderTopupContext:
    base = {
        "merchant_id": merchant_id,
        "a6_user_id": 1000 + merchant_id,
        "session_ref": f"/private/session-{merchant_id}.json",
        "quota_per_unit": UNIT,
    }
    base.update(overrides)
    return ProviderTopupContext(**base)


class Store:
    def __init__(self, contexts, pending=None):
        self.contexts = list(contexts)
        self.pending = pending
        self.calls = []
        self.failures = []
        self.next_intent_id = 501

    def providers(self, merchant_ids):
        self.calls.append(("providers", tuple(merchant_ids)))
        return self.contexts

    def load_pending_intent(self, merchant_id):
        self.calls.append(("load_pending", merchant_id))
        return self.pending

    def begin_transfer(self, context, balance, amount, preview, *, idempotency_key):
        self.calls.append(("begin", context.merchant_id, amount, idempotency_key))
        return TopupIntent(
            intent_id=self.next_intent_id,
            amount=amount,
            user_balance_before=balance.user_balance,
            withdrawable_before=balance.withdrawable,
        )

    def confirm_transfer(self, merchant_id, intent, balance_after):
        self.calls.append(("confirm", merchant_id, intent.amount))

    def record_failure(self, merchant_id, error_class):
        self.failures.append((merchant_id, error_class))


class Remote:
    def __init__(self, balances, *, preview=None):
        self.balances = list(balances)
        self.preview_payload = preview if preview is not None else {
            "enabled": True,
            "net_micros": None,
        }
        self.calls = []

    def live_balance(self, context):
        self.calls.append(("balance", context.merchant_id))
        return self.balances.pop(0)

    def transfer_preview(self, context, amount):
        self.calls.append(("preview", amount))
        payload = dict(self.preview_payload)
        if payload.get("net_micros") is None:
            payload["net_micros"] = amount
        return payload

    def transfer_balance(self, context, amount):
        self.calls.append(("transfer", amount))


def service(store, remote, *, apply=False, floor_units=Decimal("2"), strict_liquidity=False):
    return ProviderTopupService(
        store,
        remote,
        clock=lambda: NOW,
        floor_units=floor_units,
        apply=apply,
        strict_liquidity=strict_liquidity,
    )


def test_provider_below_the_floor_is_raised_to_it_in_whole_units() -> None:
    """Merchant 9 sat at 102,886 quota ($0.21) with the floor at $2."""

    store = Store([context(9)])
    remote = Remote(
        [
            LiveBalance(user_balance=102_886, withdrawable=3_000_000),
            LiveBalance(user_balance=2_102_886, withdrawable=1_000_000),
        ]
    )

    result = service(store, remote, apply=True).run_once((9,))

    assert result["success"] is True
    assert result["mode"] == "applied"
    assert result["topped_up"] == 1
    provider = result["providers"][0]
    # Deficit is 897,114; rounded up to whole $1 units that is 1,000,000.
    assert provider["amount_quota"] == 2 * UNIT
    assert provider["action"] == "topped_up"
    assert provider["user_balance_after"] == 2_102_886
    assert ("transfer", 2 * UNIT) in remote.calls
    assert ("confirm", 9, 2 * UNIT) in store.calls


def test_preview_disabled_never_posts_a_transfer() -> None:
    store = Store([context(9)])
    remote = Remote(
        [LiveBalance(user_balance=0, withdrawable=3_000_000)],
        preview={"enabled": False, "net_micros": UNIT},
    )

    result = service(store, remote, apply=True).run_once((9,))

    assert result["success"] is False
    assert store.failures == [(9, "provider_topup_transfer_disabled")]
    assert not any(call[0] == "transfer" for call in remote.calls)


def test_unsettled_earnings_keep_the_provider_instead_of_excluding_it() -> None:
    """Earnings reach `withdrawable` with a delay, so an empty pot is not a fault.

    Merchant 11 was at 19 quota of user balance with 0 withdrawable. Reporting it as a
    shortfall keeps its duty and its listings, rather than dropping it from the fleet.
    """

    store = Store([context(11)])
    remote = Remote([LiveBalance(user_balance=19, withdrawable=0)])

    result = service(store, remote, apply=True).run_once((11,))

    assert result["success"] is True
    assert result["awaiting_settlement"] == 1
    provider = result["providers"][0]
    assert provider["action"] == "awaiting_settlement"
    assert provider["shortfall_quota"] == 2 * UNIT
    assert not any(call[0] == "transfer" for call in remote.calls)


def test_partial_withdrawable_transfers_what_exists_and_reports_the_rest() -> None:
    store = Store([context(15)])
    remote = Remote(
        [
            LiveBalance(user_balance=0, withdrawable=700_000),
            LiveBalance(user_balance=500_000, withdrawable=200_000),
        ]
    )

    result = service(store, remote, apply=True).run_once((15,))

    provider = result["providers"][0]
    assert provider["amount_quota"] == UNIT
    assert provider["shortfall_quota"] == UNIT
    assert provider["action"] == "topped_up"


def test_provider_above_the_floor_is_left_alone() -> None:
    store = Store([context(3)])
    remote = Remote([LiveBalance(user_balance=1_500_000, withdrawable=4_064)])

    result = service(store, remote, apply=True).run_once((3,))

    assert result["providers"][0]["action"] == "above_floor"
    assert result["topped_up"] == 0
    assert not any(call[0] in {"preview", "transfer"} for call in remote.calls)


def test_preview_mode_reports_the_plan_without_touching_a6() -> None:
    store = Store([context(9)])
    remote = Remote([LiveBalance(user_balance=0, withdrawable=5_000_000)])

    result = service(store, remote).run_once((9,))

    assert result["mode"] == "preview"
    assert result["providers"][0]["action"] == "would_transfer"
    assert result["providers"][0]["amount_quota"] == 2 * UNIT
    assert result["topped_up"] == 0
    assert not any(call[0] in {"preview", "transfer"} for call in remote.calls)
    assert not any(call[0] == "begin" for call in store.calls)


def test_in_flight_transfer_is_read_back_and_never_reposted() -> None:
    pending = TopupIntent(
        intent_id=501,
        amount=UNIT,
        user_balance_before=0,
        withdrawable_before=UNIT,
    )
    store = Store([context(17)], pending=pending)
    remote = Remote([LiveBalance(user_balance=UNIT, withdrawable=0)])

    result = service(store, remote, apply=True).run_once((17,))

    assert result["providers"][0]["action"] == "reconciled_pending_transfer"
    assert not any(call[0] == "transfer" for call in remote.calls)
    assert ("confirm", 17, UNIT) in store.calls


def test_in_flight_transfer_that_did_not_land_is_an_error_not_a_repost() -> None:
    pending = TopupIntent(
        intent_id=501,
        amount=UNIT,
        user_balance_before=0,
        withdrawable_before=UNIT,
    )
    store = Store([context(17)], pending=pending)
    remote = Remote([LiveBalance(user_balance=0, withdrawable=UNIT)])

    result = service(store, remote, apply=True).run_once((17,))

    assert result["success"] is False
    assert store.failures == [(17, "provider_topup_transfer_unconfirmed")]
    assert not any(call[0] == "transfer" for call in remote.calls)


def test_idempotency_key_is_stable_inside_one_minute_bucket() -> None:
    store = Store([context(9)])
    remote = Remote(
        [
            LiveBalance(user_balance=0, withdrawable=5_000_000),
            LiveBalance(user_balance=2 * UNIT, withdrawable=4_000_000),
        ]
    )

    service(store, remote, apply=True).run_once((9,))

    key = next(call[3] for call in store.calls if call[0] == "begin")
    bucket = int(NOW.timestamp()) // 60
    assert key == f"provider-topup:merchant:9:amount:{2 * UNIT}:bucket:{bucket}"


def test_floor_outside_the_supported_range_is_rejected() -> None:
    store = Store([])
    remote = Remote([])

    with pytest.raises(ProviderTopupError):
        service(store, remote, floor_units=Decimal("0.5"))
    with pytest.raises(ProviderTopupError):
        service(store, remote, floor_units=Decimal("21"))


def test_context_rejects_invalid_identity_and_quota() -> None:
    with pytest.raises(ProviderTopupError):
        context(merchant_id=0)
    with pytest.raises(ProviderTopupError):
        context(quota_per_unit=0)
    with pytest.raises(ProviderTopupError):
        context(a6_user_id=0)


def test_strict_liquidity_keeps_any_positive_balance_eligible() -> None:
    store = Store([context(22)])
    remote = Remote([LiveBalance(user_balance=19, withdrawable=0)])

    result = service(store, remote, apply=True, strict_liquidity=True).run_once((22,))

    provider = result["providers"][0]
    assert provider["action"] == "refill_deferred"
    assert provider["funding_eligible"] is True
    assert provider["replacement_required"] is False
    assert provider["liquidity_state"] == "REFILLING"


def test_strict_liquidity_keeps_balance_at_hard_line_eligible() -> None:
    store = Store([context(23)])
    remote = Remote([LiveBalance(user_balance=UNIT, withdrawable=0)])

    result = service(store, remote, apply=True, strict_liquidity=True).run_once((23,))

    provider = result["providers"][0]
    assert provider["action"] == "refill_deferred"
    assert provider["funding_eligible"] is True
    assert provider["replacement_required"] is False
    assert provider["liquidity_state"] == "REFILLING"


def test_strict_liquidity_pending_unknown_does_not_repost_or_claim_funding() -> None:
    pending = TopupIntent(
        intent_id=502,
        amount=UNIT,
        user_balance_before=0,
        withdrawable_before=UNIT,
    )
    store = Store([context(24)], pending=pending)
    remote = Remote([LiveBalance(user_balance=0, withdrawable=UNIT)])

    result = service(store, remote, apply=True, strict_liquidity=True).run_once((24,))

    provider = result["providers"][0]
    assert provider["action"] == "transfer_unknown"
    assert provider["funding_eligible"] is True
    assert provider["replacement_required"] is False
    assert not any(call[0] == "transfer" for call in remote.calls)
