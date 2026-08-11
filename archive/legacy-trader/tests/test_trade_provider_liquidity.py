from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from a6api_auto.trade_provider_liquidity import (
    ProviderLiquidityCandidate,
    ProviderLiquidityHardLineGuard,
    ProviderLiquidityReconciler,
)
from a6api_auto.trade_provider_topup import (
    LiveBalance,
    ProviderTopupContext,
    ProviderTopupError,
    ProviderTopupService,
    TopupIntent,
)

NOW = datetime(2026, 8, 1, 3, 30, tzinfo=UTC)
MODEL = "gpt-5.6-sol"
UNIT = 500_000


def candidate(
    *,
    merchant_id: int = 5,
    schedulable: bool = True,
    balance_insufficient_observed: bool = False,
    cashout_drain_requested: bool = False,
):
    return ProviderLiquidityCandidate(
        model=MODEL,
        context=ProviderTopupContext(
            merchant_id=merchant_id,
            a6_user_id=9000 + merchant_id,
            session_ref=f"/private/{merchant_id}.json",
            quota_per_unit=UNIT,
        ),
        enodot_account_id=45_000 + merchant_id,
        a6_token_id=14_000 + merchant_id,
        inventory_state="READY",
        liquidity_state="FUNDED",
        schedulable=schedulable,
        assigned_slot_no=1,
        balance_insufficient_observed=balance_insufficient_observed,
        cashout_drain_requested=cashout_drain_requested,
    )


class TopupStore:
    def __init__(self, events, *, pending=None):
        self.events = events
        self.pending = pending
        self.failures = []

    def providers(self, _merchant_ids):
        raise AssertionError("automatic reconcile uses discovered contexts")

    def load_pending_intent(self, merchant_id):
        self.events.append(("pending", merchant_id))
        return self.pending

    def begin_transfer(self, context, balance, amount, _preview, *, idempotency_key):
        self.events.append(("intent", context.merchant_id, amount, idempotency_key))
        return TopupIntent(
            intent_id=77,
            amount=amount,
            user_balance_before=balance.user_balance,
            withdrawable_before=balance.withdrawable,
            fund_reservation_id=88,
        )

    def confirm_transfer(self, merchant_id, intent, _balance_after):
        self.events.append(("confirm", merchant_id, intent.intent_id))

    def record_failure(self, merchant_id, error_class):
        self.failures.append((merchant_id, error_class))


class Remote:
    def __init__(self, events, balances, *, preview_enabled=True):
        self.events = events
        self.balances = list(balances)
        self.preview_enabled = preview_enabled

    def live_balance(self, context):
        self.events.append(("balance", context.merchant_id))
        return self.balances.pop(0)

    def transfer_preview(self, context, amount):
        self.events.append(("preview", context.merchant_id, amount))
        return {"enabled": self.preview_enabled, "net_micros": amount}

    def transfer_balance(self, context, amount):
        self.events.append(("transfer", context.merchant_id, amount))


class Admin:
    def __init__(self, events, *, schedulable=True):
        self.events = events
        self.schedulable = schedulable

    def get_account(self, account_id):
        self.events.append(("account", account_id, self.schedulable))
        return {"id": account_id, "schedulable": self.schedulable}

    def set_schedulable(self, account_id, schedulable, *, idempotency_key=""):
        self.events.append(("set_schedulable", account_id, schedulable, idempotency_key))
        self.schedulable = schedulable
        return {"id": account_id, "schedulable": schedulable}


class LiquidityStore:
    def __init__(self, entries):
        self.entries = tuple(entries)
        self.projections = []

    def candidates(self, model, *, limit):
        assert model == MODEL
        return self.entries[:limit]

    def project(self, candidate, outcome, **kwargs):
        self.projections.append((candidate, outcome, kwargs))
        return {
            "action": "projected",
            "inventory_state": (
                "WARM_STANDBY"
                if outcome.funding_eligible
                else (
                    "RETIRED"
                    if kwargs.get("replacement_required")
                    and kwargs.get("hard_blocked")
                    else "REPAIRING"
                )
            ),
            "schedulable": False,
        }


def build(
    entry,
    balances,
    *,
    apply=True,
    pending=None,
    preview_enabled=True,
):
    events = []
    topup_store = TopupStore(events, pending=pending)
    remote = Remote(events, balances, preview_enabled=preview_enabled)
    admin = Admin(events, schedulable=entry.schedulable)
    liquidity_store = LiquidityStore((entry,))
    guard = ProviderLiquidityHardLineGuard((entry,), admin, apply=apply)
    topup = ProviderTopupService(
        topup_store,
        remote,
        clock=lambda: NOW,
        apply=apply,
        strict_liquidity=True,
    )
    reconciler = ProviderLiquidityReconciler(
        liquidity_store,
        topup,
        guard,
        model=MODEL,
        apply=apply,
        clock=lambda: NOW,
    )
    return reconciler, liquidity_store, topup_store, remote, admin, events


def test_observed_insufficient_with_subunit_withdrawable_drains_without_transfer() -> None:
    entry = candidate(balance_insufficient_observed=True)
    reconciler, store, _topup_store, _remote, _admin, events = build(
        entry,
        (LiveBalance(user_balance=0, withdrawable=UNIT - 1),),
    )

    result = reconciler.run_once((entry,))

    names = [event[0] for event in events]
    assert "set_schedulable" in names
    assert "preview" not in names
    assert "transfer" not in names
    assert result["topped_up"] == 0
    assert result["hard_blocked"] == 1
    assert result["replacement_required"] == 1
    assert result["providers"][0]["amount_quota"] == 0
    assert result["providers"][0]["liquidity_state"] == "UNFUNDED"
    assert result["providers"][0]["projection"]["inventory_state"] == "RETIRED"
    assert store.projections[0][2]["hard_blocked"] is True


def test_no_whole_unit_keeps_positive_balance_eligible_without_transfer() -> None:
    entry = candidate()
    reconciler, store, _topup_store, _remote, _admin, events = build(
        entry,
        (LiveBalance(user_balance=120_000, withdrawable=392_396),),
    )

    result = reconciler.run_once((entry,))

    assert result["success"] is True
    assert result["unfunded"] == 0
    assert result["replacement_required"] == 0
    assert not any(event[0] in {"preview", "transfer"} for event in events)
    outcome = store.projections[0][1]
    assert outcome.funding_eligible is True
    assert outcome.liquidity_state == "REFILLING"


def test_cashout_drain_blocks_provider_without_starting_a_competing_topup() -> None:
    entry = candidate(cashout_drain_requested=True)
    reconciler, store, _topup_store, _remote, _admin, events = build(
        entry,
        (LiveBalance(user_balance=0, withdrawable=55 * UNIT),),
    )

    result = reconciler.run_once((entry,))

    provider = result["providers"][0]
    assert result["cashout_drained"] == 1
    assert result["hard_blocked"] == 0
    assert provider["cashout_drain_requested"] is True
    assert provider["cashout_drain_confirmed"] is True
    assert provider["replacement_required"] is True
    assert provider["action"] == "refill_suppressed_for_cashout"
    assert any(event[0] == "set_schedulable" for event in events)
    assert not any(event[0] in {"preview", "intent", "transfer"} for event in events)
    assert store.projections[0][2]["cashout_drain_requested"] is True


def test_pending_unknown_only_reads_back_and_keeps_active_intent_identity() -> None:
    entry = candidate()
    pending = TopupIntent(
        intent_id=91,
        amount=UNIT,
        user_balance_before=0,
        withdrawable_before=UNIT,
        fund_reservation_id=92,
    )
    reconciler, store, _topup_store, _remote, _admin, events = build(
        entry,
        (LiveBalance(user_balance=0, withdrawable=UNIT),),
        pending=pending,
    )

    result = reconciler.run_once((entry,))

    assert result["transfer_unknown"] == 1
    assert not any(event[0] in {"preview", "intent", "transfer"} for event in events)
    outcome = store.projections[0][1]
    assert outcome.intent_id == 91
    assert outcome.fund_reservation_id == 92
    assert outcome.liquidity_state == "TRANSFER_UNKNOWN"


def test_provider_at_hard_line_remains_eligible_while_refill_is_deferred() -> None:
    entry = candidate()
    reconciler, store, _topup_store, _remote, _admin, events = build(
        entry,
        (LiveBalance(user_balance=UNIT, withdrawable=0),),
    )

    result = reconciler.run_once((entry,))

    provider = result["providers"][0]
    assert provider["liquidity_state"] == "REFILLING"
    assert provider["funding_eligible"] is True
    assert provider["replacement_required"] is False
    assert not any(event[0] == "set_schedulable" for event in events)
    assert store.projections[0][2]["hard_blocked"] is False


def test_disabled_preview_keeps_eligibility_but_never_posts_transfer() -> None:
    entry = candidate()
    reconciler, store, topup_store, _remote, _admin, events = build(
        entry,
        (LiveBalance(user_balance=0, withdrawable=UNIT),),
        preview_enabled=False,
    )

    result = reconciler.run_once((entry,))

    assert result["success"] is False
    assert result["providers"][0]["action"] == "transfer_disabled"
    assert result["providers"][0]["liquidity_state"] == "REFILLING"
    assert result["providers"][0]["funding_eligible"] is True
    assert topup_store.failures == [(entry.context.merchant_id, "provider_topup_transfer_disabled")]
    assert not any(event[0] == "transfer" for event in events)
    assert len(store.projections) == 1


def test_fund_claim_conflict_does_not_invent_a_balance_hard_block() -> None:
    entry = candidate()
    reconciler, store, topup_store, _remote, _admin, events = build(
        entry,
        (LiveBalance(user_balance=0, withdrawable=UNIT),),
    )

    def conflict(*_args, **_kwargs):
        raise ProviderTopupError("provider_topup_funds_already_reserved")

    topup_store.begin_transfer = conflict
    result = reconciler.run_once((entry,))

    assert result["success"] is False
    assert result["providers"][0]["error_class"] == (
        "provider_topup_funds_already_reserved"
    )
    assert result["providers"][0]["liquidity_state"] == "UNKNOWN"
    assert result["providers"][0]["hard_line_confirmed"] is False
    assert result["providers"][0]["projection"]["action"] == "observation_failed"
    assert not any(event[0] == "transfer" for event in events)
    assert store.projections == []


def test_preview_mode_plans_transfer_without_preemptive_hard_block() -> None:
    entry = candidate(balance_insufficient_observed=True)
    reconciler, store, _topup_store, _remote, _admin, events = build(
        entry,
        (LiveBalance(user_balance=0, withdrawable=2 * UNIT),),
        apply=False,
    )

    result = reconciler.run_once((entry,))

    assert result["mode"] == "preview"
    assert result["providers"][0]["action"] == "would_transfer"
    assert result["providers"][0]["hard_line_encountered"] is False
    assert result["providers"][0]["hard_line_confirmed"] is False
    assert not any(event[0] in {"set_schedulable", "intent", "transfer"} for event in events)
    assert store.projections == []


def test_provider_retirement_migration_and_queue_are_packaged() -> None:
    source = Path("migrations/0068_provider_membership_retirement.sql").read_text(
        encoding="utf-8"
    )
    packaged = Path(
        "src/a6api_auto/data/migrations/0068_provider_membership_retirement.sql"
    ).read_text(encoding="utf-8")

    assert source == packaged
    assert "'provider_retirement'" in source
    assert "provider_membership_repair_requests" in source
    assert "provider_membership_repair_one_open_slot_idx" in source


def test_provider_retirement_projection_enqueues_membership_repair() -> None:
    source = Path("src/a6api_auto/trade_provider_liquidity.py").read_text(
        encoding="utf-8"
    )

    assert "SET enodot_binding_id = NULL" in source
    assert "slot_state = 'VACANT'" in source
    assert "SET inventory_state = %s" in source
    assert "provider_membership_repair_requests" in source
    assert '"membership_repair_request_id"' in source
