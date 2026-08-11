from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from a6api_auto.trade_provider_binding import (
    ProviderBindingContext,
    ProviderBindingError,
)
from a6api_auto.trade_provider_rebalance import (
    PostgresProviderRetirementStore,
    ProjectedProviderRoleStore,
    ProviderAccountRetirementExecutor,
    ProviderRebalanceError,
    ProviderRetirementBinding,
    ProviderRetirementPreparation,
    ProviderRetirementRequest,
    assert_role_switch_readback,
)


def request() -> ProviderRetirementRequest:
    return ProviderRetirementRequest(
        merchant_id=19,
        external_a6_token_id=14093,
        enodot_account_id=45896,
    )


def binding(*, state: str = "bound") -> ProviderRetirementBinding:
    return ProviderRetirementBinding(
        binding_id=2,
        merchant_id=19,
        external_a6_token_id=14093,
        binding_uid=request().binding_uid,
        enodot_account_id=45896,
        state=state,
    )


def account(**overrides: Any) -> dict[str, Any]:
    result = {
        "id": 45896,
        "extra": {"managed_by": "a6-trader", "a6_binding_uid": request().binding_uid},
        "group_ids": [3, 26],
        "schedulable": True,
        "status": "active",
        "credentials": {"api_key": "secret-must-not-be-snapshotted"},
    }
    result.update(overrides)
    return result


class FakeRetirementStore:
    def __init__(self, provider_binding: ProviderRetirementBinding | None = None) -> None:
        self.binding = provider_binding or binding()
        self.prepared: list[dict[str, Any]] = []
        self.completed: list[dict[str, Any]] = []
        self.failed: list[dict[str, Any]] = []

    def load_binding(self, retirement_request):
        assert retirement_request == request()
        return self.binding

    def prepare_retirement(self, provider_binding, retirement_request, **kwargs):
        assert provider_binding == self.binding
        assert retirement_request == request()
        self.prepared.append(kwargs)
        return ProviderRetirementPreparation(2, 3, len(self.prepared))

    def complete_retirement(self, preparation, retirement_request, **kwargs):
        assert retirement_request == request()
        self.completed.append({"preparation": preparation, **kwargs})
        self.binding = binding(state="retired")

    def fail_retirement(self, preparation, **kwargs):
        self.failed.append({"preparation": preparation, **kwargs})


class FakeAdmin:
    def __init__(self, current: dict[str, Any] | None = None) -> None:
        self.account = current or account()
        self.calls: list[tuple[Any, ...]] = []

    def get_account(self, account_id: int):
        assert account_id == 45896
        self.calls.append(("get", account_id))
        return dict(self.account)

    def set_schedulable(self, account_id: int, schedulable: bool):
        self.calls.append(("schedulable", account_id, schedulable))
        self.account["schedulable"] = schedulable
        return dict(self.account)

    def update_account(self, account_id: int, payload):
        self.calls.append(("update", account_id, dict(payload)))
        self.account.update(payload)
        return dict(self.account)


def test_retirement_default_dry_run_has_no_writes_or_audit() -> None:
    store = FakeRetirementStore()
    admin = FakeAdmin()

    result = ProviderAccountRetirementExecutor(store, admin).execute(request())

    assert result.summary() == {
        "success": True,
        "mode": "dry-run",
        "account_id": 45896,
        "binding_uid": "a6:merchant-19:token-14093",
        "action": "update",
        "changed_fields": ["schedulable", "group_ids"],
        "binding_state_before": "bound",
        "binding_state_after": "retired",
        "applied": False,
        "verified": False,
    }
    assert admin.calls == [("get", 45896)]
    assert store.prepared == []
    assert store.completed == []
    assert "secret-must-not-be-snapshotted" not in repr(result)


def test_retirement_apply_stops_scheduling_before_removing_group_and_reads_back() -> None:
    store = FakeRetirementStore()
    admin = FakeAdmin()

    result = ProviderAccountRetirementExecutor(store, admin).execute(request(), apply=True)

    assert result.applied is True
    assert result.verified is True
    assert result.binding_state_after == "retired"
    assert admin.calls == [
        ("get", 45896),
        ("schedulable", 45896, False),
        ("get", 45896),
        ("update", 45896, {"group_ids": [3]}),
        ("get", 45896),
    ]
    assert len(store.prepared) == 1
    assert len(store.completed) == 1
    assert store.failed == []
    audit = repr((store.prepared, store.completed))
    assert "secret-must-not-be-snapshotted" not in audit
    assert "'binding_state': 'retired'" in audit


def test_retirement_is_retryable_after_schedulable_was_already_disabled() -> None:
    store = FakeRetirementStore()
    admin = FakeAdmin(account(schedulable=False))

    result = ProviderAccountRetirementExecutor(store, admin).execute(request(), apply=True)

    assert result.changed_fields == ("group_ids",)
    assert ("schedulable", 45896, False) not in admin.calls
    assert ("update", 45896, {"group_ids": [3]}) in admin.calls
    assert result.verified is True


def test_retirement_repeated_final_state_is_noop() -> None:
    store = FakeRetirementStore(binding(state="retired"))
    admin = FakeAdmin(account(schedulable=False, group_ids=[3]))

    result = ProviderAccountRetirementExecutor(store, admin).execute(request(), apply=True)

    assert result.action == "noop"
    assert result.applied is False
    assert result.verified is True
    assert admin.calls == [("get", 45896)]
    assert store.prepared == []
    assert store.completed == []


def test_retirement_rejects_remote_identity_before_write() -> None:
    store = FakeRetirementStore()
    admin = FakeAdmin(account(extra={"managed_by": "a6-trader", "a6_binding_uid": "other-binding"}))

    with pytest.raises(
        ProviderRebalanceError,
        match="provider_retirement_account_identity_mismatch",
    ):
        ProviderAccountRetirementExecutor(store, admin).execute(request(), apply=True)

    assert admin.calls == [("get", 45896)]
    assert store.prepared == []


def test_retirement_failure_is_audited_without_remote_secret() -> None:
    class StuckAdmin(FakeAdmin):
        def update_account(self, account_id: int, payload):
            self.calls.append(("update", account_id, dict(payload)))
            return dict(self.account)

    store = FakeRetirementStore()
    admin = StuckAdmin(account(schedulable=False))

    with pytest.raises(
        ProviderRebalanceError,
        match="provider_retirement_group_readback_failed",
    ):
        ProviderAccountRetirementExecutor(store, admin).execute(request(), apply=True)

    assert len(store.failed) == 1
    assert store.failed[0]["error_class"] == "provider_retirement_group_readback_failed"
    assert len(store.failed[0]["error_fingerprint"]) == 64
    assert "secret-must-not-be-snapshotted" not in repr(store.failed)


def test_retirement_retries_after_remote_write_but_before_database_completion() -> None:
    class CompleteOnceStore(FakeRetirementStore):
        def __init__(self) -> None:
            super().__init__()
            self.complete_attempts = 0

        def complete_retirement(self, preparation, retirement_request, **kwargs):
            self.complete_attempts += 1
            if self.complete_attempts == 1:
                raise ProviderRebalanceError("provider_retirement_fixture_commit_failed")
            super().complete_retirement(preparation, retirement_request, **kwargs)

    store = CompleteOnceStore()
    admin = FakeAdmin(account(schedulable=False))
    executor = ProviderAccountRetirementExecutor(store, admin)

    with pytest.raises(
        ProviderRebalanceError,
        match="provider_retirement_fixture_commit_failed",
    ):
        executor.execute(request(), apply=True)

    assert admin.account["group_ids"] == [3]
    assert store.binding.state == "bound"
    first_call_count = len(admin.calls)

    retried = executor.execute(request(), apply=True)

    assert retried.verified is True
    assert retried.changed_fields == ()
    assert retried.binding_state_after == "retired"
    assert all(call[0] == "get" for call in admin.calls[first_call_count:])
    assert len(store.prepared) == 2
    assert len(store.failed) == 1


def test_postgres_retirement_store_audits_and_marks_binding_retired() -> None:
    class Cursor:
        def __init__(self, connection):
            self.connection = connection
            self.one = None

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, query, params=None):
            values = tuple(params or ())
            assert query.count("%s") == len(values)
            self.connection.calls.append((query, values))
            normalized = " ".join(query.split())
            self.one = None
            if normalized.startswith("SELECT b.id AS binding_id"):
                self.one = {
                    "binding_id": 2,
                    "merchant_id": 19,
                    "external_a6_token_id": 14093,
                    "binding_uid": request().binding_uid,
                    "enodot_account_id": 45896,
                    "state": self.connection.binding_state,
                }
            elif normalized.startswith("INSERT INTO a6_trade.trade_control_actions"):
                self.one = {"id": 3}
            elif normalized.startswith("SELECT coalesce(max(attempt_no)"):
                self.one = {"attempt_no": 1}
            elif normalized.startswith("UPDATE a6_trade.enodot_account_bindings"):
                self.connection.binding_state = "retired"
                self.one = (2,)
            return self

        def fetchone(self):
            return self.one

    class Connection:
        def __init__(self):
            self.calls = []
            self.binding_state = "bound"
            self.commits = 0
            self.rollbacks = 0
            self.closes = 0

        def cursor(self):
            return Cursor(self)

        def commit(self):
            self.commits += 1

        def rollback(self):
            self.rollbacks += 1

        def close(self):
            self.closes += 1

    connection = Connection()
    store = PostgresProviderRetirementStore(lambda: connection)
    provider_binding = store.load_binding(request())

    preparation = store.prepare_retirement(
        provider_binding,
        request(),
        desired_state={"account_id": 45896, "binding_state": "retired"},
        observed_before={"account_id": 45896, "group_ids": [26]},
    )
    store.complete_retirement(
        preparation,
        request(),
        observed_after={"account_id": 45896, "group_ids": [], "binding_state": "retired"},
        elapsed_ms=12,
    )

    assert preparation == ProviderRetirementPreparation(2, 3, 1)
    assert connection.binding_state == "retired"
    assert connection.commits == 2
    assert connection.rollbacks == 0
    rendered = repr(connection.calls)
    assert "retire_provider_account" in rendered
    assert "trade_control_action_attempts" in rendered
    assert "secret" not in rendered


def provider_context(path: Path, **overrides: Any) -> ProviderBindingContext:
    value = ProviderBindingContext(
        merchant_id=7,
        a6_user_id=2909,
        supplier_id=291,
        merchant_status="supplier_submitted",
        roles=("seller",),
        session_ref=str(path),
        proxy_lease_id="singbox:23086",
        token_row_id=8,
        external_a6_token_id=12465,
        token_name="gpt",
        token_status="active",
    )
    return ProviderBindingContext(**{**value.__dict__, **overrides})


class ContextStore:
    def __init__(self, context: ProviderBindingContext) -> None:
        self.context = context

    def load_context(self, merchant_id: int, external_a6_token_id: int):
        assert merchant_id == 7
        assert external_a6_token_id == 12465
        return self.context


def test_projected_store_exposes_post_switch_role_only_in_memory(tmp_path: Path) -> None:
    original = provider_context(tmp_path / "session.json")
    delegate = ContextStore(original)
    projected = ProjectedProviderRoleStore(delegate, merchant_id=7)

    result = projected.load_context(7, 12465)

    assert result.roles == ("seller", "provider")
    assert delegate.context.roles == ("seller",)
    with pytest.raises(ProviderBindingError, match="provider_binding_projection_write_forbidden"):
        projected.prepare_binding(None, None)


def test_projected_store_rejects_unexpected_observer_role(tmp_path: Path) -> None:
    projected = ProjectedProviderRoleStore(
        ContextStore(provider_context(tmp_path / "session.json", roles=("seller", "observer"))),
        merchant_id=7,
    )

    with pytest.raises(ProviderBindingError, match="provider_binding_projection_role_invalid"):
        projected.load_context(7, 12465)


def test_role_switch_readback_requires_exact_final_roles() -> None:
    assert_role_switch_readback(
        {19: ("seller",), 7: ("seller", "provider")},
        outgoing_merchant_id=19,
        incoming_merchant_id=7,
    )

    with pytest.raises(
        ProviderRebalanceError,
        match="provider_rebalance_incoming_role_readback_failed",
    ):
        assert_role_switch_readback(
            {19: ("seller",), 7: ("seller",)},
            outgoing_merchant_id=19,
            incoming_merchant_id=7,
        )
