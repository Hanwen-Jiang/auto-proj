from __future__ import annotations

import inspect
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from a6api_auto.trade_seller_retirement import (
    LiveFinancial,
    PostgresSellerRetirementStore,
    SellerRetirementContext,
    SellerRetirementError,
    SellerRetirementService,
    TransferIntent,
)

NOW = datetime(2026, 7, 26, 0, 0, tzinfo=timezone.utc)


def context(**overrides):
    base = SellerRetirementContext(
        merchant_id=3,
        a6_user_id=3003,
        supplier_id=4003,
        session_ref="/private/session-3.json",
        proxy_lease_id="singbox:23082",
        roles=("seller",),
        quota_per_unit=500_000,
        snapshot_withdrawable=700_000,
        state="checking",
        transfer_intent_id=None,
        external_a6_token_id=14027,
        token_name="merchant-3",
        other_observer_count=3,
    )
    return replace(base, **overrides)


class Store:
    def __init__(self, merchant, intent=None):
        self.context = merchant
        self.intent = intent
        self.calls = []
        self.failure = None

    def ensure_targets(self, merchant_ids):
        self.calls.append(("ensure_targets", tuple(merchant_ids)))

    def claim_next(
        self,
        merchant_ids,
        *,
        snapshot_max_age_seconds,
        retire_in_place_maximum_success_rate=0,
        retire_in_place_minimum_samples=30,
    ):
        self.calls.append(
            (
                "claim",
                tuple(merchant_ids),
                snapshot_max_age_seconds,
                retire_in_place_maximum_success_rate,
                retire_in_place_minimum_samples,
            )
        )
        return self.context

    def return_to_earning(self, merchant_id, *, event_type, details, retry_after=None):
        self.calls.append(("earning", merchant_id, event_type, dict(details), retry_after))

    def begin_transfer(self, merchant, financial, preview):
        self.calls.append(("begin_transfer", financial.withdrawable, dict(preview)))
        self.intent = TransferIntent(
            91,
            financial.withdrawable,
            financial.user_balance,
            financial.withdrawable,
        )
        return self.intent

    def load_transfer_intent(self, merchant_id):
        self.calls.append(("load_transfer", merchant_id))
        assert self.intent is not None
        return self.intent

    def confirm_transfer(self, merchant_id, intent, financial_after):
        self.calls.append(("confirm_transfer", merchant_id, intent.amount, financial_after))

    def ensure_provider_duty(self, merchant_id):
        self.calls.append(("provider_duty", merchant_id))

    def mark_binding(self, merchant_id, account_id):
        self.calls.append(("binding", merchant_id, account_id))

    def mark_listings_down(self, merchant_id, listing_ids):
        self.calls.append(("listings_down", merchant_id, tuple(listing_ids)))

    def complete_retired_duties(self, merchant_id):
        self.calls.append(("provider_observer", merchant_id))

    def begin_archive_drain(self, merchant_id):
        self.calls.append(("archive_drain", merchant_id))

    def defer_archive_drain(
        self, merchant_id, *, user_balance, withdrawable, retry_after
    ):
        self.calls.append(
            ("archive_wait", merchant_id, user_balance, withdrawable, retry_after)
        )

    def complete_archived_duties(self, merchant_id):
        self.calls.append(("observer_only", merchant_id))

    def record_failure(self, merchant_id, error_class, *, retry_after):
        self.failure = (merchant_id, error_class, retry_after)


class Remote:
    def __init__(self, financials):
        self.financials = list(financials)
        self.calls = []

    def live_financial(self, merchant):
        self.calls.append(("financial", merchant.merchant_id))
        return self.financials.pop(0)

    def transfer_preview(self, merchant, amount):
        self.calls.append(("preview", amount))
        return {"enabled": True}

    def transfer_balance(self, merchant, amount):
        self.calls.append(("transfer", amount))

    def ensure_provider_binding(self, merchant):
        self.calls.append(("provider_binding", merchant.external_a6_token_id))
        return 45898

    def takedown_all_listings(self, merchant):
        self.calls.append(("takedown", merchant.merchant_id))
        return (4174,)


def test_qualified_seller_transfers_all_available_then_becomes_provider_observer() -> None:
    store = Store(context())
    remote = Remote(
        [
            LiveFinancial(user_balance=100_000, withdrawable=700_000),
            LiveFinancial(user_balance=800_000, withdrawable=0),
        ]
    )

    result = SellerRetirementService(store, remote, clock=lambda: NOW).run_once(
        (3, 6),
        min_observers=2,
    )

    assert result.summary() == {
        "success": True,
        "merchant_id": 3,
        "action": "retired_to_provider_observer",
        "state": "completed",
        "amount_quota": 700_000,
        "enodot_account_id": 45898,
        "error_class": None,
    }
    assert ("transfer", 700_000) in remote.calls
    # The takedown is the point of retirement: it pulls off-market inventory out of the
    # pool. What must not follow is expecting the retiree to fund itself afterwards.
    assert [call[0] for call in store.calls][-4:] == [
        "provider_duty",
        "binding",
        "listings_down",
        "provider_observer",
    ]


def test_live_balance_below_threshold_keeps_seller_earning() -> None:
    store = Store(context())
    remote = Remote([LiveFinancial(user_balance=0, withdrawable=499_999)])

    result = SellerRetirementService(store, remote, clock=lambda: NOW).run_once((3,))

    assert result.action == "waiting_threshold"
    assert result.state == "earning"
    assert not any(call[0] == "transfer" for call in remote.calls)
    assert any(call[0] == "earning" for call in store.calls)


def test_observer_is_not_retired_when_two_other_observers_would_not_remain() -> None:
    store = Store(
        context(
            roles=("seller", "observer"),
            other_observer_count=1,
        )
    )
    remote = Remote([])

    result = SellerRetirementService(store, remote, clock=lambda: NOW).run_once(
        (3,), min_observers=2
    )

    assert result.action == "deferred_observer_capacity"
    assert remote.calls == []
    assert store.calls[-1][2] == "observer_capacity_guard"


def test_unknown_transfer_is_only_read_back_and_never_posted_again() -> None:
    intent = TransferIntent(
        intent_id=91,
        amount=700_000,
        user_balance_before=100_000,
        withdrawable_before=700_000,
    )
    store = Store(context(state="transfer_unknown", transfer_intent_id=91), intent=intent)
    remote = Remote([LiveFinancial(user_balance=100_000, withdrawable=0)])

    result = SellerRetirementService(store, remote, clock=lambda: NOW).run_once((3,))

    assert result.success is True
    assert result.state == "completed"
    assert not any(call[0] in {"preview", "transfer"} for call in remote.calls)
    assert any(call[0] == "load_transfer" for call in store.calls)


def test_retirement_keeps_the_seller_role_so_the_provider_can_fund_itself() -> None:
    """Retirement withdraws a merchant from price-band allocation, not from earning.

    Stripping `seller` removed the only income a Provider has, and A6 requires >= $1 of
    user balance to be selectable. All ten retired Providers ended below that floor and
    were excluded from Pin with `provider_user_balance_below_one_usd`.
    """

    source = inspect.getsource(PostgresSellerRetirementStore.complete_retired_duties)

    assert "ARRAY['seller', 'provider']::text[]" in source
    assert "ARRAY['provider', 'observer']::text[]" not in source
    # The one-way growth kill switch is gone: nothing ever set these back to true.
    assert "SET enabled = false, callback_enabled = false" not in source


def test_safe_archive_waits_for_provider_balance_then_becomes_observer_only() -> None:
    waiting_store = Store(
        context(
            state="draining",
            roles=("provider", "observer"),
            archive_mode=True,
        )
    )
    waiting_remote = Remote(
        [LiveFinancial(user_balance=600_000, withdrawable=0)]
    )

    waiting = SellerRetirementService(
        waiting_store, waiting_remote, clock=lambda: NOW
    ).run_once((3,))

    assert waiting.action == "draining_account_value"
    assert waiting.state == "draining"
    assert any(call[0] == "archive_wait" for call in waiting_store.calls)

    completed_store = Store(
        context(
            state="draining",
            roles=("provider", "observer"),
            archive_mode=True,
        )
    )
    completed_remote = Remote(
        [LiveFinancial(user_balance=499_999, withdrawable=0)]
    )

    completed = SellerRetirementService(
        completed_store, completed_remote, clock=lambda: NOW
    ).run_once((3,))

    assert completed.action == "archived_to_observer"
    assert completed.state == "completed"
    assert completed_store.calls[-1] == ("observer_only", 3)


def test_unproductive_channel_retires_in_place_without_a_transfer() -> None:
    """A losing Listing comes down now instead of inching toward the threshold.

    Merchant 1 (鹭湾066) held the cheapest public slot -- the one A6 routes buyers to
    first -- at a 4% success rate, earning about $0.12 a day. Waiting for it to reach the
    $1 transfer threshold means paying reputation for small change. Its balance is below
    both the $1 balance-transfer minimum and the $50 crypto-withdrawal minimum, so it
    cannot be moved at all: leave it accruing on the merchant and take the Listing down.
    """

    store = Store(
        context(
            state="checking",
            snapshot_withdrawable=106_498,
            channel_success_rate=400,
            channel_sample_count=100,
        )
    )
    remote = Remote([LiveFinancial(user_balance=0, withdrawable=106_498)])

    result = SellerRetirementService(store, remote, clock=lambda: NOW).run_once(
        (3,),
        retire_in_place_maximum_success_rate=3000,
        retire_in_place_minimum_samples=30,
    )

    assert result.success is True
    assert result.state == "completed"
    assert result.amount == 0
    # No transfer is attempted: A6 would reject it below one quota_per_unit.
    assert not any(call[0] in {"preview", "transfer"} for call in remote.calls)
    assert any(call[0] == "listings_down" for call in store.calls)
    reason = next(call for call in store.calls if call[0] == "earning")
    assert reason[2] == "retire_in_place_unproductive"
    assert reason[3]["stranded_below_transfer_minimum"] is True
    assert reason[3]["channel_success_rate"] == 400


def test_safe_archive_never_bypasses_the_transfer_threshold() -> None:
    store = Store(
        context(
            state="checking",
            archive_mode=True,
            snapshot_withdrawable=106_498,
            channel_success_rate=400,
            channel_sample_count=100,
        )
    )
    remote = Remote([LiveFinancial(user_balance=0, withdrawable=106_498)])

    result = SellerRetirementService(store, remote, clock=lambda: NOW).run_once(
        (3,),
        retire_in_place_maximum_success_rate=3000,
        retire_in_place_minimum_samples=30,
    )

    assert result.action == "waiting_threshold"
    assert result.state == "earning"
    assert not any(call[0] in {"transfer", "listings_down"} for call in remote.calls)


def test_healthy_channel_below_threshold_still_waits() -> None:
    """Retire-in-place must not fire on a channel that is merely still accumulating."""

    store = Store(
        context(
            state="checking",
            snapshot_withdrawable=400_000,
            channel_success_rate=9_800,
            channel_sample_count=100,
        )
    )
    remote = Remote([LiveFinancial(user_balance=0, withdrawable=400_000)])

    result = SellerRetirementService(store, remote, clock=lambda: NOW).run_once(
        (3,),
        retire_in_place_maximum_success_rate=3000,
    )

    assert result.action == "waiting_threshold"
    assert result.state == "earning"
    assert not any(call[0] == "listings_down" for call in store.calls)


def test_low_success_rate_needs_enough_samples_before_retiring_in_place() -> None:
    """A brand-new channel reads 0% on one sample; that is not evidence of a loser."""

    store = Store(
        context(
            state="checking",
            snapshot_withdrawable=1_000,
            channel_success_rate=0,
            channel_sample_count=1,
        )
    )
    remote = Remote([LiveFinancial(user_balance=0, withdrawable=1_000)])

    result = SellerRetirementService(store, remote, clock=lambda: NOW).run_once(
        (3,),
        retire_in_place_maximum_success_rate=3000,
        retire_in_place_minimum_samples=30,
    )

    assert result.action == "waiting_threshold"
    assert not any(call[0] == "listings_down" for call in store.calls)


def test_retire_in_place_is_off_by_default() -> None:
    """The default threshold of 0 bp can never match, so the policy is opt-in."""

    store = Store(
        context(state="checking", channel_success_rate=0, channel_sample_count=100)
    )
    remote = Remote([LiveFinancial(user_balance=0, withdrawable=1_000)])

    result = SellerRetirementService(store, remote, clock=lambda: NOW).run_once((3,))

    assert result.action == "waiting_threshold"


def test_retire_in_place_bounds_are_validated() -> None:
    store = Store(context())
    service = SellerRetirementService(store, Remote([]), clock=lambda: NOW)

    for kwargs in (
        {"retire_in_place_maximum_success_rate": -1},
        {"retire_in_place_maximum_success_rate": 10_001},
        {"retire_in_place_minimum_samples": 9},
    ):
        with pytest.raises(SellerRetirementError):
            service.run_once((3,), **kwargs)
