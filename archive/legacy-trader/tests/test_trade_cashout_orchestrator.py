from __future__ import annotations

import hashlib
import inspect
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from a6api_auto import trade_cashout_orchestrator as cashout_orchestrator
from a6api_auto.trade_cashout_orchestrator import (
    MICROS_PER_USD,
    A6CryptoCashoutReconciler,
    CashoutApplyExecutor,
    CashoutCycle,
    CashoutError,
    CashoutFinancialSnapshot,
    CashoutIntentRecord,
    CashoutMerchantContext,
    CashoutOrchestrator,
    CashoutPlanner,
    CashoutPolicy,
    CashoutReconciliation,
    CashoutReconciliationContext,
    CashoutReconciliationWorker,
    CashoutRiskEvidence,
    CashoutRoutePreview,
    CashoutSource,
    MaxSpendCallbackRemote,
    PostgresCashoutStore,
    callback_break_even_probability,
    cashout_eligible_micros,
)

NOW = datetime(2026, 8, 1, 5, 0, tzinfo=UTC)


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def snapshot(
    merchant_id: int = 5,
    *,
    withdrawable_usd: str = "55",
    provider_reserve_usd: str = "0",
    local_reserved_usd: str = "0",
    suffix: str = "a",
) -> CashoutFinancialSnapshot:
    return CashoutFinancialSnapshot(
        merchant_id=merchant_id,
        withdrawable_micros=int(Decimal(withdrawable_usd) * MICROS_PER_USD),
        provider_reserve_micros=int(Decimal(provider_reserve_usd) * MICROS_PER_USD),
        local_reserved_other_micros=int(Decimal(local_reserved_usd) * MICROS_PER_USD),
        balance_source_hash=digest(f"balance:{merchant_id}:{suffix}"),
        observed_at=NOW,
        quota_per_unit=500_000,
    )


def risk(
    *,
    delta: str = "0.10",
    probability: str = "0.01",
    hard: bool = False,
    earn_rate_usd: str = "1",
) -> CashoutRiskEvidence:
    return CashoutRiskEvidence(
        delta_effective=Decimal(delta),
        merchant_age_hours=Decimal("240"),
        hours_since_last_confirmed_cashout=Decimal("48"),
        earn_rate_p25_micros_per_hour=int(Decimal(earn_rate_usd) * MICROS_PER_USD),
        hard_risk=hard,
        fixed_probability=Decimal(probability),
        sample_count=120,
    )


def route(
    gross_usd: str,
    *,
    route_id: int = 1,
    fee_usd: str = "3",
    probability: str = "1",
    latency_penalty: int = 0,
) -> CashoutRoutePreview:
    gross = int(Decimal(gross_usd) * MICROS_PER_USD)
    fee = int(Decimal(fee_usd) * MICROS_PER_USD)
    return CashoutRoutePreview(
        wallet_route_id=route_id,
        wallet_principal_id=10 + route_id,
        remote_withdraw_account_id=100 + route_id,
        network="BSC" if route_id == 1 else "ETH",
        gross_micros=gross,
        fee_micros=fee,
        net_micros=gross - fee,
        confirmation_probability=Decimal(probability),
        preview_source_hash=digest(f"preview:{route_id}:{gross_usd}:{fee_usd}"),
        latency_penalty_micros=latency_penalty,
    )


def fixed_cycle(
    merchant_id: int,
    *,
    target_usd: str = "50",
    tier: str = "low",
) -> CashoutCycle:
    planner = CashoutPlanner()
    source = CashoutSource(
        financial=snapshot(merchant_id, withdrawable_usd=target_usd),
        risk=risk(delta={"low": "0.1", "medium": "0.35", "high": "0.5"}[tier]),
        routes=(route(target_usd),),
        decision_epoch=99,
    )
    planned = planner.plan(source)
    return replace(
        planned.cycle,
        standard_target_micros=int(Decimal(target_usd) * MICROS_PER_USD),
        target_jitter_micros=0,
    )


def source_for_standard(
    merchant_id: int,
    *,
    coverage_numerator: int = 0,
    coverage_denominator: int = 1,
    skipped_rounds: int = 0,
    last_cashout_at: datetime | None = None,
) -> CashoutSource:
    financial = snapshot(merchant_id)
    preview = route("55", route_id=merchant_id)
    initial = CashoutSource(
        financial=financial,
        risk=risk(),
        source_funds_accrued_micros=coverage_denominator,
        confirmed_source_debit_micros=coverage_numerator,
        skipped_rounds=skipped_rounds,
        last_confirmed_cashout_at=last_cashout_at,
        routes=(preview,),
        decision_epoch=100 + merchant_id,
    )
    generated = CashoutPlanner().plan(initial).cycle
    cycle = replace(
        generated,
        standard_target_micros=50 * MICROS_PER_USD,
        target_jitter_micros=0,
    )
    return replace(initial, existing_cycle=cycle)


def test_cashout_eligible_subtracts_provider_and_other_claims_once() -> None:
    assert cashout_eligible_micros(52_000_000, 4_000_000, 1_000_000) == 47_000_000
    assert cashout_eligible_micros(2_000_000, 3_000_000, 4_000_000) == 0


def test_break_even_regressions_preserve_exact_fixed_fee_semantics() -> None:
    fee_rate = Decimal(1) / Decimal(6)
    at_fifty = callback_break_even_probability(
        Decimal("40"),
        Decimal("50"),
        standard_fixed_fee=Decimal("3"),
        callback_fee_rate=fee_rate,
    )
    at_seventy_five = callback_break_even_probability(
        Decimal("40"),
        Decimal("75"),
        standard_fixed_fee=Decimal("3"),
        callback_fee_rate=fee_rate,
    )

    assert abs(at_fifty - Decimal(16) / Decimal(141)) < Decimal("1e-26")
    assert abs(at_seventy_five - Decimal(19) / Decimal(144)) < Decimal("1e-26")


def test_standard_trigger_submits_full_cashout_eligible_not_target() -> None:
    planned = CashoutPlanner().plan(source_for_standard(5))

    assert planned.method == "crypto"
    assert planned.action == "execute"
    assert planned.cycle.standard_target_micros == 50 * MICROS_PER_USD
    assert planned.gross_micros == 55 * MICROS_PER_USD
    assert planned.gross_micros != planned.cycle.standard_target_micros
    assert planned.expected_fee_micros == 3 * MICROS_PER_USD


def test_sticky_target_survives_balance_and_preview_evidence_refresh() -> None:
    planner = CashoutPlanner()
    first_source = CashoutSource(
        financial=snapshot(5, withdrawable_usd="40", suffix="first"),
        risk=risk(),
        routes=(route("40"),),
        decision_epoch=77,
    )
    first = planner.plan(first_source)
    second_source = replace(
        first_source,
        financial=snapshot(5, withdrawable_usd="47", suffix="second"),
        routes=(route("47"),),
        existing_cycle=first.cycle,
    )
    second = planner.plan(second_source)

    assert second.cycle == first.cycle


def test_hard_risk_uses_crypto_at_fifty_even_when_callback_exists() -> None:
    financial = snapshot(9, withdrawable_usd="50")
    initial = CashoutSource(
        financial=financial,
        risk=risk(delta="0.50", probability="0.40", hard=True),
        callback_available=True,
        callback_sender_ref="sg-cloud",
        callback_receiver_merchant_id=99,
        callback_receiver_model="gpt-5.6-sol",
        callback_receiver_listing_id=199,
        routes=(route("50"),),
        decision_epoch=88,
    )
    planned = CashoutPlanner().plan(initial)

    assert planned.method == "crypto"
    assert planned.priority == "P0"
    assert planned.cycle.standard_target_micros == 50 * MICROS_PER_USD


def test_high_risk_five_dollar_callback_waits_for_provider_drain() -> None:
    planned = CashoutPlanner().plan(
        CashoutSource(
            financial=snapshot(12, withdrawable_usd="5"),
            risk=risk(delta="0.50", probability="0.40"),
            provider_duty_state="READY",
            callback_available=True,
            callback_sender_ref="sg-cloud",
            callback_receiver_merchant_id=77,
            callback_receiver_model="gpt-5.6-sol",
            callback_receiver_listing_id=177,
            decision_epoch=90,
        )
    )

    assert planned.method == "callback"
    assert planned.action == "awaiting_provider_drain"
    assert planned.gross_micros == 5 * MICROS_PER_USD
    assert planned.decision_reason == "provider_drain_required"


def test_low_risk_standard_cashout_preserves_reserve_without_provider_drain() -> None:
    source = source_for_standard(13)
    planned = CashoutPlanner().plan(
        replace(
            source,
            provider_duty_state="READY",
            provider_drain_confirmed=False,
        )
    )

    assert planned.method == "crypto"
    assert planned.action == "execute"
    assert planned.decision_reason == "standard_target_reached"


def test_route_expected_value_beats_network_round_robin() -> None:
    first = route("55", route_id=1, probability="0.98")
    second = route(
        "55",
        route_id=2,
        fee_usd="2.5",
        probability="0.99",
        latency_penalty=50,
    )
    planned = CashoutPlanner().plan(
        CashoutSource(
            financial=snapshot(14),
            risk=risk(delta="0.50", probability="0.20", hard=True),
            routes=(first, second),
            decision_epoch=114,
        )
    )

    assert planned.wallet_route_id == 2


def test_fair_sort_uses_coverage_then_oldest_cashout() -> None:
    planner = CashoutPlanner()
    higher_coverage = source_for_standard(
        21,
        coverage_numerator=80,
        coverage_denominator=100,
        last_cashout_at=NOW - timedelta(days=10),
    )
    lower_coverage = source_for_standard(
        22,
        coverage_numerator=20,
        coverage_denominator=100,
        last_cashout_at=NOW,
    )

    plans = planner.plan_all((higher_coverage, lower_coverage))

    assert [plan.merchant_id for plan in plans] == [22, 21]


def test_anti_starvation_promotes_skipped_merchant_within_layer() -> None:
    planner = CashoutPlanner()
    low_coverage = source_for_standard(
        31,
        coverage_numerator=10,
        coverage_denominator=100,
    )
    starved = source_for_standard(
        32,
        coverage_numerator=90,
        coverage_denominator=100,
        skipped_rounds=3,
    )

    plans = planner.plan_all((low_coverage, starved))

    assert [plan.merchant_id for plan in plans] == [32, 31]


def test_disabled_orchestrator_is_read_only() -> None:
    result = CashoutOrchestrator(CashoutPlanner()).run_once((source_for_standard(5),))

    assert result == {
        "success": True,
        "enabled": False,
        "mode": "disabled",
        "remote_write_count": 0,
        "plans": [],
    }


def test_failed_final_requires_authoritative_zero_effect() -> None:
    with pytest.raises(CashoutError, match="cashout_authoritative_no_effect_required"):
        CashoutReconciliation(
            outcome="failed_final",
            confirmation_type="authoritative_no_effect",
            source_hash=digest("reconcile"),
            observed_at=NOW,
        )


def reconciliation_context(
    *,
    remote_withdrawal_id: str | None = "701",
    remote_request_id: str | None = "request-701",
    deadline: datetime | None = None,
) -> CashoutReconciliationContext:
    return CashoutReconciliationContext(
        intent=CashoutIntentRecord(
            intent_id=701,
            merchant_id=5,
            method="crypto",
            state="RECONCILING",
            gross_micros=55 * MICROS_PER_USD,
            fund_reservation_id=801,
            idempotency_key=digest("reconcile-intent"),
            remote_request_id=remote_request_id,
            remote_withdrawal_id=remote_withdrawal_id,
        ),
        merchant=CashoutMerchantContext(
            merchant_id=5,
            a6_user_id=9517,
            session_ref="session-reconcile.json",
            quota_per_unit=500_000,
            merchant_status="approved",
        ),
        remote_withdraw_account_id=132,
        expected_fee_micros=3 * MICROS_PER_USD,
        expected_net_micros=52 * MICROS_PER_USD,
        balance_source_hash=digest("reconcile-balance"),
        submit_attempted_at=NOW - timedelta(minutes=5),
        submitted_at=NOW - timedelta(minutes=5),
        confirmation_deadline_at=deadline or NOW + timedelta(hours=1),
        baseline_withdrawable_micros=55 * MICROS_PER_USD,
        baseline_withdrawing_micros=0,
        baseline_paid_micros=0,
        baseline_observed_at=NOW - timedelta(minutes=6),
    )


class FakeWithdrawReconcileClient:
    def __init__(
        self,
        *,
        status: dict[str, object] | None = None,
        withdrawals: list[dict[str, object]] | None = None,
        summary: dict[str, object] | None = None,
        transport_error: bool = False,
    ) -> None:
        self.status = status
        self.withdrawals = withdrawals or []
        self.summary = summary or {
            "balance_available_micros": 55 * MICROS_PER_USD,
            "withdrawing_micros": 0,
            "total_paid_micros": 0,
        }
        self.transport_error = transport_error
        self.status_calls = 0
        self.list_calls = 0

    def supplier_withdraw_status(self, user_id, withdrawal_id):
        assert user_id == 9517
        assert str(withdrawal_id) == "701"
        self.status_calls += 1
        if self.transport_error:
            raise RuntimeError("status transport fixture")
        return self.status

    def supplier_withdrawals(self, user_id, *, page, size):
        assert user_id == 9517
        assert (page, size) == (1, 1_000)
        self.list_calls += 1
        if self.transport_error:
            raise RuntimeError("list transport fixture")
        return {"items": self.withdrawals}

    def supplier_withdraw_summary(self, user_id):
        assert user_id == 9517
        if self.transport_error:
            raise RuntimeError("summary transport fixture")
        return self.summary


def test_crypto_reconciler_confirms_closed_terminal_status_and_balance_effect() -> None:
    client = FakeWithdrawReconcileClient(
        status={
            "id": 701,
            "status": "completed",
            "amount_micros": 55 * MICROS_PER_USD,
            "fee_micros": 3 * MICROS_PER_USD,
            "net_micros": 52 * MICROS_PER_USD,
            "txid": "chain-fixture",
        },
        summary={
            "balance_available_micros": 0,
            "withdrawing_micros": 55 * MICROS_PER_USD,
            "total_paid_micros": 52 * MICROS_PER_USD,
        },
    )
    result = A6CryptoCashoutReconciler(
        lambda _context: client,
        clock=lambda: NOW,
    ).reconcile(reconciliation_context())

    assert result.outcome == "confirmed"
    assert result.remote_withdrawal_id == "701"
    assert result.observed_gross_micros == 55 * MICROS_PER_USD
    assert result.observed_fee_micros == 3 * MICROS_PER_USD
    assert result.observed_net_micros == 52 * MICROS_PER_USD
    assert result.chain_txid == "chain-fixture"
    assert client.status_calls == 1
    assert client.list_calls == 0


def test_crypto_reconciler_uses_terminal_failure_as_authoritative_no_effect() -> None:
    client = FakeWithdrawReconcileClient(status={"id": 701, "status": "rejected"})
    result = A6CryptoCashoutReconciler(
        lambda _context: client,
        clock=lambda: NOW,
    ).reconcile(reconciliation_context())

    assert result.outcome == "failed_final"
    assert result.authoritative_no_effect is True
    assert result.failure_class == "remote_rejected_no_effect"


def test_crypto_reconciler_waits_for_balance_effect_after_terminal_success() -> None:
    client = FakeWithdrawReconcileClient(
        status={
            "id": 701,
            "status": "completed",
            "amount_micros": 55 * MICROS_PER_USD,
            "fee_micros": 3 * MICROS_PER_USD,
            "net_micros": 52 * MICROS_PER_USD,
        }
    )
    result = A6CryptoCashoutReconciler(
        lambda _context: client,
        clock=lambda: NOW,
    ).reconcile(reconciliation_context())

    assert result.outcome == "pending"
    assert result.observed_gross_micros == 55 * MICROS_PER_USD
    assert result.evidence["terminal_amounts_complete"] is True


def test_crypto_reconciler_never_releases_on_terminal_identity_conflict() -> None:
    client = FakeWithdrawReconcileClient(
        status={
            "id": 701,
            "status": "rejected",
            "account_id": 999,
            "amount_micros": 55 * MICROS_PER_USD,
        }
    )
    result = A6CryptoCashoutReconciler(
        lambda _context: client,
        clock=lambda: NOW,
    ).reconcile(reconciliation_context())

    assert result.outcome == "unknown"
    assert result.authoritative_no_effect is False
    assert result.evidence["identity_conflicts"] == ["remote_withdraw_account_id"]


def test_crypto_reconciler_keeps_processing_and_balance_only_effect_nonterminal() -> None:
    processing = A6CryptoCashoutReconciler(
        lambda _context: FakeWithdrawReconcileClient(
            status={"id": 701, "status": "processing"}
        ),
        clock=lambda: NOW,
    ).reconcile(reconciliation_context())
    balance_only = A6CryptoCashoutReconciler(
        lambda _context: FakeWithdrawReconcileClient(
            summary={
                "balance_available_micros": 0,
                "withdrawing_micros": 55 * MICROS_PER_USD,
                "total_paid_micros": 0,
            }
        ),
        clock=lambda: NOW,
    ).reconcile(
        reconciliation_context(
            remote_withdrawal_id=None,
            remote_request_id=None,
        )
    )

    assert processing.outcome == "pending"
    assert balance_only.outcome == "pending"
    assert balance_only.confirmation_type == "balance_effect"
    assert balance_only.observed_net_micros is None


def test_crypto_reconciler_does_not_treat_unknown_remote_status_as_pending() -> None:
    result = A6CryptoCashoutReconciler(
        lambda _context: FakeWithdrawReconcileClient(
            status={"id": 701, "status": "fixture_new_state"}
        ),
        clock=lambda: NOW,
    ).reconcile(reconciliation_context())

    assert result.outcome == "unknown"
    assert result.remote_status == "fixture_new_state"


def test_crypto_reconciler_matches_lost_submit_response_by_account_gross_and_time() -> None:
    client = FakeWithdrawReconcileClient(
        withdrawals=[
            {
                "id": 909,
                "account_id": 132,
                "amount_micros": 55 * MICROS_PER_USD,
                "fee_micros": 3 * MICROS_PER_USD,
                "net_micros": 52 * MICROS_PER_USD,
                "created_at": (NOW - timedelta(minutes=4)).isoformat(),
                "status": "completed",
            }
        ],
        summary={
            "balance_available_micros": 0,
            "withdrawing_micros": 55 * MICROS_PER_USD,
            "total_paid_micros": 52 * MICROS_PER_USD,
        },
    )
    result = A6CryptoCashoutReconciler(
        lambda _context: client,
        clock=lambda: NOW,
    ).reconcile(
        reconciliation_context(
            remote_withdrawal_id=None,
            remote_request_id=None,
        )
    )

    assert result.outcome == "confirmed"
    assert result.remote_withdrawal_id == "909"
    assert result.evidence["match_type"] == "account_gross_time"


def test_crypto_reconciler_transport_loss_stays_unknown() -> None:
    result = A6CryptoCashoutReconciler(
        lambda _context: FakeWithdrawReconcileClient(transport_error=True),
        clock=lambda: NOW,
    ).reconcile(
        reconciliation_context(
            remote_withdrawal_id=None,
            remote_request_id=None,
        )
    )

    assert result.outcome == "unknown"
    assert len(result.evidence["errors"]) == 2


def test_reconciliation_worker_never_calls_remote_submit() -> None:
    context = reconciliation_context()
    pending = CashoutReconciliation(
        outcome="pending",
        confirmation_type="remote_status",
        source_hash=digest("worker-pending"),
        observed_at=NOW,
    )

    class Store:
        def __init__(self) -> None:
            self.claims = 0
            self.completed = []

        def claim_reconciliation(self, worker_id, **kwargs):
            assert worker_id == "worker-fixture"
            assert kwargs == {
                "lease_seconds": 60,
                "method": "crypto",
                "retry_interval_seconds": 300,
            }
            self.claims += 1
            return context.intent if self.claims == 1 else None

        def load_reconciliation_context(self, intent_id):
            assert intent_id == context.intent.intent_id
            return context

        def complete_reconciliation(self, intent_id, worker_id, result):
            self.completed.append((intent_id, worker_id, result))

    class Reconciler:
        def reconcile(self, claimed_context):
            assert claimed_context is context
            return pending

    store = Store()
    result = CashoutReconciliationWorker(  # type: ignore[arg-type]
        store,
        Reconciler(),  # type: ignore[arg-type]
        worker_id="worker-fixture",
    ).run_once()

    assert result["claimed_count"] == 1
    assert result["pending_count"] == 1
    assert store.completed == [(701, "worker-fixture", pending)]


def test_store_claims_intent_and_shared_reservation_in_one_transaction() -> None:
    source = " ".join(inspect.getsource(PostgresCashoutStore.create_intent).split())

    assert "pg_advisory_xact_lock" in source
    assert "merchant_fund_reservations" in source
    assert "reservation_state = 'active'" in source
    assert "fund_reservation_id" in source
    assert "_validate_funds_snapshot" in source
    assert "connection.commit()" in source


def test_store_revalidates_fresh_balance_and_provider_reserve_under_fund_lock() -> None:
    source = " ".join(inspect.getsource(PostgresCashoutStore.create_intent).split())
    validator = " ".join(inspect.getsource(PostgresCashoutStore._validate_funds_snapshot).split())

    assert source.index("pg_advisory_xact_lock") < source.index("_validate_funds_snapshot")
    assert "a6_financial_current" in validator
    assert "FOR UPDATE" in validator
    assert "snapshot_fresh" in validator
    assert "provider_fund_reserves" in validator
    assert "reserve_fresh" in validator
    assert "cashout_eligible_balance_changed" in validator


def test_candidate_query_requires_post_confirmation_financial_snapshot() -> None:
    query = " ".join(cashout_orchestrator._CASHOUT_CANDIDATES_SQL.split())

    assert "financial.observed_at > debit.last_confirmed_cashout_at" in query


def test_resume_waits_until_old_provider_slot_is_replaced_or_vacant() -> None:
    source = " ".join(
        inspect.getsource(PostgresCashoutStore._resume_awaiting_intent).split()
    )

    assert "provider_pool_slots" in source
    assert "slot.valid_to IS NULL" in source
    assert "slot.slot_state <> 'VACANT'" in source
    assert source.index("provider_duty_active") < source.index("_validate_funds_snapshot")


def test_unknown_reconcile_uses_lease_and_does_not_resubmit() -> None:
    claim = " ".join(inspect.getsource(PostgresCashoutStore.claim_reconciliation).split())
    submit = " ".join(inspect.getsource(PostgresCashoutStore.record_submit_result).split())

    assert "FOR UPDATE SKIP LOCKED" in claim
    assert "reconcile_lease_until" in claim
    assert "intent_state = 'RECONCILING'" in claim
    assert 'state = "PENDING" if certain else "UNKNOWN"' in submit


def test_migration_has_nonterminal_and_callback_accounting_guards() -> None:
    root = Path(__file__).resolve().parents[1]
    migration = (root / "migrations/0050_risk_balanced_cashout.sql").read_text()
    packaged = (root / "src/a6api_auto/data/migrations/0050_risk_balanced_cashout.sql").read_text()

    assert packaged == migration
    assert "a6_withdrawal_intent_one_nonterminal_idx" in migration
    assert "merchant_fund_reservations" in migration
    assert "a6_callback_sweep_closure_valid" in migration
    assert "treasury_legacy_binding_migrations" in migration
    assert "merchant_treasury_route_bindings" in migration
    assert "merchant_treasury_remote_account_uidx" in migration
    assert "standard_target_micros >= 50000000" in migration


def test_reconciliation_schedule_migration_is_packaged_and_method_scoped() -> None:
    root = Path(__file__).resolve().parents[1]
    migration = (root / "migrations/0053_cashout_reconcile_schedule.sql").read_text()
    packaged = (
        root / "src/a6api_auto/data/migrations/0053_cashout_reconcile_schedule.sql"
    ).read_text()

    assert packaged == migration
    assert "a6_withdrawal_intent_reconcile_due_idx" in migration
    assert "method" in migration
    assert "last_reconciled_at NULLS FIRST" in migration


def test_cashout_drain_reserve_basis_migration_is_packaged() -> None:
    root = Path(__file__).resolve().parents[1]
    migration = (root / "migrations/0054_provider_cashout_drain_reserve.sql").read_text()
    packaged = (
        root / "src/a6api_auto/data/migrations/0054_provider_cashout_drain_reserve.sql"
    ).read_text()

    assert packaged == migration
    assert "provider_fund_reserve_basis_known" in migration
    assert "'cashout_drain'" in migration


class FakeApplyStore:
    def __init__(self) -> None:
        self.state = "PREVIEWED"
        self.submit_results: list[dict[str, object]] = []
        self.create_calls = 0
        self.begin_calls = 0

    def create_intent(self, plan):
        self.create_calls += 1
        return CashoutIntentRecord(
            intent_id=91,
            merchant_id=plan.merchant_id,
            method=str(plan.method),
            state=self.state,
            gross_micros=plan.gross_micros,
            fund_reservation_id=191,
            idempotency_key=str(plan.idempotency_key),
        )

    def begin_submit(self, intent_id):
        assert intent_id == 91
        assert self.state == "PREVIEWED"
        self.begin_calls += 1
        self.state = "SUBMITTED"
        return CashoutIntentRecord(
            intent_id=91,
            merchant_id=5,
            method="crypto",
            state=self.state,
            gross_micros=55 * MICROS_PER_USD,
            fund_reservation_id=191,
            idempotency_key=digest("apply-intent"),
        )

    def record_submit_result(self, intent_id, **result):
        assert intent_id == 91
        self.submit_results.append(dict(result))
        self.state = "PENDING" if result["accepted"] else "UNKNOWN"


class LostResponseRemote:
    def __init__(self) -> None:
        self.calls = 0

    def submit_crypto(self, plan, intent):
        assert plan.merchant_id == intent.merchant_id
        self.calls += 1
        raise RuntimeError("fixture response lost")


def test_apply_transport_loss_becomes_unknown_and_original_intent_is_not_reposted() -> None:
    plan = CashoutPlanner().plan(source_for_standard(5))
    store = FakeApplyStore()
    remote = LostResponseRemote()
    policy = CashoutPolicy(standard_withdraw_apply_enabled=True)
    executor = CashoutApplyExecutor(  # type: ignore[arg-type]
        store,
        crypto_remote=remote,
        policy=policy,
        clock=lambda: NOW,
    )

    first = executor.execute(plan)
    second = executor.execute(plan)

    assert first["state"] == "UNKNOWN"
    assert first["remote_write_count"] == 1
    assert second["decision"] == "existing_intent_not_resubmitted"
    assert second["remote_write_count"] == 0
    assert remote.calls == 1
    assert store.begin_calls == 1
    assert store.submit_results == [{"accepted": False, "remote_request_id": None}]


def test_apply_rejects_non_authoritative_fee_fallback_before_claim() -> None:
    plan = replace(
        CashoutPlanner().plan(source_for_standard(5)),
        route_authoritative=False,
    )
    store = FakeApplyStore()
    executor = CashoutApplyExecutor(  # type: ignore[arg-type]
        store,
        crypto_remote=LostResponseRemote(),
        policy=CashoutPolicy(standard_withdraw_apply_enabled=True),
        clock=lambda: NOW,
    )

    with pytest.raises(CashoutError, match="cashout_authoritative_preview_required"):
        executor.execute(plan)

    assert store.create_calls == 0


def test_callback_adapter_submits_one_request_with_exact_max_spend() -> None:
    calls = []

    def submitter(**kwargs):
        calls.append(kwargs)
        return {
            "request_id": "callback-request-fixture",
            "sender_debit_micros": 4_999_999,
        }

    plan = CashoutPlanner().plan(
        CashoutSource(
            financial=snapshot(12, withdrawable_usd="5"),
            risk=risk(delta="0.50", probability="0.40"),
            callback_available=True,
            callback_sender_ref="sg-cloud",
            callback_receiver_merchant_id=77,
            callback_receiver_model="gpt-5.6-sol",
            callback_receiver_listing_id=177,
            decision_epoch=90,
        )
    )
    intent = CashoutIntentRecord(
        intent_id=99,
        merchant_id=12,
        method="callback",
        state="SUBMITTED",
        gross_micros=5 * MICROS_PER_USD,
        fund_reservation_id=199,
        idempotency_key=str(plan.idempotency_key),
    )

    result = MaxSpendCallbackRemote(submitter).submit_callback(plan, intent)

    assert result.callback_request_id == "callback-request-fixture"
    assert len(calls) == 1
    assert calls[0]["request_count"] == 1
    assert calls[0]["max_spend_micros"] == 5 * MICROS_PER_USD


def test_store_candidate_loader_enforces_fresh_reserve_and_merchant_route_binding() -> None:
    source = inspect.getsource(PostgresCashoutStore.load_candidates)
    module_source = inspect.getmodule(PostgresCashoutStore)
    assert module_source is not None
    sql_source = inspect.getsource(module_source)

    assert "financial_snapshot_ttl_seconds" in source
    assert "provider.reserve_fresh" in sql_source
    assert "merchant_fund_reservations" in sql_source
    assert "merchant_treasury_route_bindings" in sql_source
    assert "latest.latest_epoch + 1" in sql_source
    assert (
        "ON CONFLICT (merchant_id, decision_epoch, config_version)\n"
        "                DO NOTHING" in sql_source
    )
