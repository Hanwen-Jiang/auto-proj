from __future__ import annotations

import json
from contextlib import contextmanager
from decimal import Decimal
from typing import Any

import pytest

from a6api_auto.trade_enodot_balance import (
    EnodotBalanceAuditPreparation,
    EnodotBalanceRecoveryError,
    EnodotBalanceRecoveryRequest,
    EnodotBalanceRecoveryService,
    PostgresEnodotBalanceAuditStore,
)


def recovery_request(**changes: Any) -> EnodotBalanceRecoveryRequest:
    values = {
        "user_id": 17,
        "expected_balance_usd": "-5.37927575",
        "target_balance_usd": "1.00000000",
        "notes": "confirmed legacy-route loss recovery",
    }
    values.update(changes)
    return EnodotBalanceRecoveryRequest(**values)


class FakeAdmin:
    def __init__(
        self,
        balance: str = "-5.37927575",
        *,
        fail_after_apply: bool = False,
        reject_without_apply: bool = False,
    ) -> None:
        self.balance = Decimal(balance)
        self.fail_after_apply = fail_after_apply
        self.reject_without_apply = reject_without_apply
        self.get_calls = 0
        self.add_calls: list[dict[str, Any]] = []

    def get_user(self, user_id: int):
        self.get_calls += 1
        return {"id": user_id, "balance": str(self.balance), "status": "active"}

    def add_user_balance(self, user_id, amount, *, idempotency_key, notes=""):
        self.add_calls.append(
            {
                "user_id": user_id,
                "amount": Decimal(amount),
                "idempotency_key": idempotency_key,
                "notes": notes,
            }
        )
        if self.reject_without_apply:
            raise RuntimeError("fixture rejected")
        self.balance += Decimal(amount)
        if self.fail_after_apply:
            self.fail_after_apply = False
            raise RuntimeError("fixture response lost")
        return {"id": user_id, "balance": str(self.balance), "status": "active"}


class FakeStore:
    def __init__(self) -> None:
        self.action_id = 91
        self.completed = False
        self.locked: list[int] = []
        self.started: list[dict[str, Any]] = []
        self.completions: list[dict[str, Any]] = []
        self.errors: list[dict[str, Any]] = []
        self.attempts = 0

    @contextmanager
    def lock(self, user_id: int):
        self.locked.append(user_id)
        yield

    def start(self, request, observed_before):
        self.started.append(
            {
                "request": request,
                "observed_before": dict(observed_before),
            }
        )
        if self.completed:
            return EnodotBalanceAuditPreparation(self.action_id, None, True)
        self.attempts += 1
        return EnodotBalanceAuditPreparation(self.action_id, self.attempts)

    def complete(self, preparation, observed_after, *, elapsed_ms):
        self.completed = True
        self.completions.append(
            {
                "preparation": preparation,
                "observed_after": dict(observed_after),
                "elapsed_ms": elapsed_ms,
            }
        )

    def finish_error(
        self,
        preparation,
        observed_after,
        *,
        error_class,
        elapsed_ms,
        unknown,
    ):
        self.errors.append(
            {
                "preparation": preparation,
                "observed_after": dict(observed_after) if observed_after else None,
                "error_class": error_class,
                "elapsed_ms": elapsed_ms,
                "unknown": unknown,
            }
        )


def test_request_uses_exact_eight_decimal_delta_and_stable_key() -> None:
    first = recovery_request()
    second = recovery_request()

    assert first.amount_usd == Decimal("6.37927575")
    assert first.idempotency_key == second.idempotency_key
    assert len(first.idempotency_key) <= 128
    assert first.desired_state() == {
        "endpoint": "/api/v1/admin/users/17/balance",
        "method": "POST",
        "operation": "add",
        "user_id": 17,
        "expected_balance_usd": "-5.37927575",
        "amount_usd": "6.37927575",
        "target_balance_usd": "1.00000000",
    }


@pytest.mark.parametrize(
    "changes,error",
    (
        ({"user_id": 0}, "enodot_balance_user_id_invalid"),
        ({"expected_balance_usd": "0.000000001"}, "enodot_balance_expected_invalid"),
        ({"target_balance_usd": "-6"}, "enodot_balance_target_not_above_expected"),
        ({"target_balance_usd": "NaN"}, "enodot_balance_target_invalid"),
    ),
)
def test_request_rejects_unsafe_recovery_contract(changes, error) -> None:
    with pytest.raises(EnodotBalanceRecoveryError, match=error):
        recovery_request(**changes)


def test_dry_run_reads_and_plans_without_lock_audit_or_credit() -> None:
    admin = FakeAdmin()
    store = FakeStore()

    result = EnodotBalanceRecoveryService(store, admin).execute(recovery_request())

    assert result.public() == {
        "success": True,
        "user_id": 17,
        "action": "add",
        "applied": False,
        "verified": False,
        "balance_before_usd": "-5.37927575",
        "balance_after_usd": "-5.37927575",
        "amount_usd": "6.37927575",
        "action_id": None,
        "recovered_after_transport_error": False,
    }
    assert admin.get_calls == 1
    assert admin.add_calls == []
    assert store.locked == []
    assert store.started == []


def test_apply_adds_exact_delta_with_precondition_and_completes_audit() -> None:
    admin = FakeAdmin()
    store = FakeStore()
    request = recovery_request()

    result = EnodotBalanceRecoveryService(store, admin).execute(request, apply=True)

    assert result.applied is True
    assert result.verified is True
    assert result.balance_after_usd == Decimal("1.00000000")
    assert result.action_id == 91
    assert store.locked == [17]
    assert store.started[0]["observed_before"] == {
        "user_id": 17,
        "balance_usd": "-5.37927575",
        "status": "active",
    }
    assert admin.add_calls == [
        {
            "user_id": 17,
            "amount": Decimal("6.37927575"),
            "idempotency_key": request.idempotency_key,
            "notes": "confirmed legacy-route loss recovery",
        }
    ]
    assert store.completions[0]["observed_after"]["balance_usd"] == "1.00000000"
    assert store.errors == []


def test_apply_precondition_drift_is_audited_and_blocks_credit() -> None:
    admin = FakeAdmin("-5.30000000")
    store = FakeStore()

    with pytest.raises(
        EnodotBalanceRecoveryError,
        match="enodot_balance_precondition_failed",
    ):
        EnodotBalanceRecoveryService(store, admin).execute(recovery_request(), apply=True)

    assert admin.add_calls == []
    assert store.errors[0]["error_class"] == "enodot_balance_precondition_failed"
    assert store.errors[0]["unknown"] is False


def test_lost_http_response_is_resolved_by_readback_and_audit_completion() -> None:
    admin = FakeAdmin(fail_after_apply=True)
    store = FakeStore()

    result = EnodotBalanceRecoveryService(store, admin).execute(
        recovery_request(),
        apply=True,
    )

    assert result.verified is True
    assert result.recovered_after_transport_error is True
    assert admin.balance == Decimal("1.00000000")
    assert len(admin.add_calls) == 1
    assert len(store.completions) == 1
    assert store.errors == []


def test_rejected_credit_with_unchanged_balance_is_failed_not_unknown() -> None:
    admin = FakeAdmin(reject_without_apply=True)
    store = FakeStore()

    with pytest.raises(EnodotBalanceRecoveryError, match="enodot_balance_runtime_error"):
        EnodotBalanceRecoveryService(store, admin).execute(recovery_request(), apply=True)

    assert store.errors[0]["error_class"] == "enodot_balance_runtime_error"
    assert store.errors[0]["unknown"] is False


def test_transport_error_with_unavailable_readback_is_audited_unknown() -> None:
    class UnknownAdmin(FakeAdmin):
        def get_user(self, user_id: int):
            if self.get_calls:
                self.get_calls += 1
                raise RuntimeError("fixture readback unavailable")
            return super().get_user(user_id)

    admin = UnknownAdmin(reject_without_apply=True)
    store = FakeStore()

    with pytest.raises(EnodotBalanceRecoveryError, match="enodot_balance_runtime_error"):
        EnodotBalanceRecoveryService(store, admin).execute(recovery_request(), apply=True)

    assert store.errors[0]["observed_after"] is None
    assert store.errors[0]["unknown"] is True


def test_completed_action_replay_only_verifies_and_never_credits_twice() -> None:
    admin = FakeAdmin()
    store = FakeStore()
    service = EnodotBalanceRecoveryService(store, admin)

    first = service.execute(recovery_request(), apply=True)
    second = service.execute(recovery_request(), apply=True)

    assert first.action == "add"
    assert second.action == "replay"
    assert second.applied is False
    assert second.verified is True
    assert len(admin.add_calls) == 1
    assert admin.balance == Decimal("1.00000000")


class RecordingCursor:
    def __init__(self, connection) -> None:
        self.connection = connection
        self.result = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, query, params=None):
        normalized = " ".join(str(query).split())
        self.connection.calls.append((normalized, params))
        if normalized.startswith("INSERT INTO a6_trade.trade_control_actions"):
            self.result = {
                "id": 71,
                "status": "started",
                "desired_state": json.loads(params[2]),
            }
        elif normalized.startswith("SELECT coalesce(max(attempt_no)"):
            self.result = {"attempt_no": 1}
        else:
            self.result = None

    def fetchone(self):
        return self.result


class RecordingConnection:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.commits = 0
        self.rollbacks = 0
        self.closed = False

    def cursor(self):
        return RecordingCursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed = True


def test_postgres_store_writes_action_attempt_and_verified_readback() -> None:
    connections: list[RecordingConnection] = []

    def factory():
        connection = RecordingConnection()
        connections.append(connection)
        return connection

    store = PostgresEnodotBalanceAuditStore(factory)
    request = recovery_request()
    before = {"user_id": 17, "balance_usd": "-5.37927575", "status": "active"}
    after = {"user_id": 17, "balance_usd": "1.00000000", "status": "active"}

    with store.lock(17):
        preparation = store.start(request, before)
        store.complete(preparation, after, elapsed_ms=23)

    assert preparation == EnodotBalanceAuditPreparation(71, 1)
    rendered = "\n".join(query for connection in connections for query, _ in connection.calls)
    assert "SELECT pg_advisory_lock" in rendered
    assert "SELECT pg_advisory_unlock" in rendered
    assert "INSERT INTO a6_trade.trade_control_actions" in rendered
    assert "INSERT INTO a6_trade.trade_control_action_attempts" in rendered
    assert "UPDATE a6_trade.trade_control_actions" in rendered
    assert "UPDATE a6_trade.trade_control_action_attempts" in rendered
    assert all(connection.closed for connection in connections)
