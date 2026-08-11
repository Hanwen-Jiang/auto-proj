from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from a6api_auto.models import SessionRecord
from a6api_auto.storage import SessionStore
from a6api_auto.trade_provider_binding import (
    BindingPreparation,
    PostgresProviderBindingStore,
    ProviderBindingContext,
    ProviderBindingError,
    ProviderBindingExecutor,
    ProviderBindingRequest,
)

SECRET = "sk-provider-binding-secret-material-000000000000000000"


def context(path: Path, **overrides: Any) -> ProviderBindingContext:
    value = ProviderBindingContext(
        merchant_id=5,
        a6_user_id=9517,
        supplier_id=298,
        merchant_status="supplier_submitted",
        roles=("seller", "provider"),
        session_ref=str(path),
        proxy_lease_id="singbox:23084",
        token_row_id=4,
        external_a6_token_id=14020,
        token_name="gpt",
        token_status="1",
    )
    return ProviderBindingContext(**{**value.__dict__, **overrides})


def request(**overrides: Any) -> ProviderBindingRequest:
    value = {
        "merchant_id": 5,
        "external_a6_token_id": 14020,
        "proxy_id": 5,
    }
    value.update(overrides)
    return ProviderBindingRequest(**value)


def save_session(path: Path, *, key: str = "", token_name: str = "gpt") -> SessionStore:
    store = SessionStore(path)
    store.save(
        SessionRecord(
            schema_version=1,
            origin="https://a6api.com",
            email="provider@fixture.invalid",
            username="provider",
            password="password-secret",
            user_id=9517,
            supplier_id=298,
            status="supplier_submitted",
            token_name=token_name,
            a6api_key=key,
        )
    )
    return store


class FakeStore:
    def __init__(self, provider_context: ProviderBindingContext) -> None:
        self.context = provider_context
        self.prepared: list[dict[str, Any]] = []
        self.completed: list[dict[str, Any]] = []
        self.failed: list[dict[str, Any]] = []

    def load_context(self, merchant_id: int, external_a6_token_id: int):
        assert merchant_id == self.context.merchant_id
        assert external_a6_token_id == self.context.external_a6_token_id
        return self.context

    def prepare_binding(self, provider_context, binding_request, **kwargs):
        self.prepared.append({"context": provider_context, "request": binding_request, **kwargs})
        return BindingPreparation(9, 10, 1, None, "planned")

    def complete_binding(self, preparation, binding_request, **kwargs):
        self.completed.append({"preparation": preparation, "request": binding_request, **kwargs})

    def fail_binding(self, preparation, binding_request, **kwargs):
        self.failed.append({"preparation": preparation, "request": binding_request, **kwargs})


class FakeAdmin:
    def __init__(self, *, action: str = "create") -> None:
        self.action = action
        self.account: dict[str, Any] | None = None
        self.reconcile_calls: list[dict[str, Any]] = []
        self.create_calls: list[dict[str, Any]] = []
        self.update_calls: list[tuple[int, dict[str, Any]]] = []
        self.scheduling_calls: list[tuple[int, bool]] = []

    def request(self, method: str, path: str, **_kwargs: Any):
        assert (method, path) == ("GET", "admin/proxies/all")
        return [{"id": 5, "port": 23084, "status": "active"}]

    def reconcile_a6_provider_account(self, *, apply: bool = False, **provider: Any):
        self.reconcile_calls.append({"apply": apply, **provider})
        account_id = self.account.get("id") if self.account else None
        if apply and self.action == "update":
            assert account_id is not None
            self.account = account_from_provider(provider, account_id=account_id)
        return SimpleNamespace(
            action=self.action,
            applied=apply and self.action != "noop",
            account_id=account_id,
            changed_fields=(),
        )

    def create_account(self, payload):
        self.create_calls.append(dict(payload))
        self.account = account_from_payload(payload, account_id=71)
        return dict(self.account)

    def get_account(self, account_id: int):
        assert self.account is not None and self.account["id"] == account_id
        return dict(self.account)

    def update_account(self, account_id: int, payload):
        assert self.account is not None and self.account["id"] == account_id
        self.update_calls.append((account_id, dict(payload)))
        self.account.update(payload)
        return dict(self.account)

    def find_account_by_binding_uid(self, binding_uid: str):
        if self.account and self.account["extra"]["a6_binding_uid"] == binding_uid:
            return dict(self.account)
        return None

    def set_schedulable(self, account_id: int, schedulable: bool):
        self.scheduling_calls.append((account_id, schedulable))
        assert self.account is not None
        self.account["schedulable"] = schedulable
        return dict(self.account)


def account_from_payload(payload: dict[str, Any], *, account_id: int) -> dict[str, Any]:
    account = {**payload, "id": account_id}
    account["credentials"] = {**payload["credentials"], "api_key": "[REDACTED]"}
    return account


def account_from_provider(provider: dict[str, Any], *, account_id: int) -> dict[str, Any]:
    return {
        "id": account_id,
        "name": provider["name"],
        "platform": "openai",
        "type": "apikey",
        "credentials": {
            "api_key": "[REDACTED]",
            "base_url": provider["base_url"],
            "model_mapping": dict(provider["model_mapping"]),
            "pool_mode": provider["pool_mode"],
            "pool_mode_retry_count": provider["pool_mode_retry_count"],
            "pool_mode_retry_status_codes": list(provider["pool_mode_retry_status_codes"]),
        },
        "extra": dict(provider["extra"]),
        "proxy_id": provider["proxy_id"],
        "group_ids": list(provider["group_ids"]),
        "concurrency": provider["concurrency"],
        "priority": provider["priority"],
        "rate_multiplier": provider["rate_multiplier"],
        "schedulable": False,
    }


def test_default_dry_run_reveals_in_memory_without_writes(tmp_path: Path) -> None:
    session = save_session(tmp_path / "session.json")
    binding_store = FakeStore(context(session.path))
    admin = FakeAdmin()
    revealed: list[int] = []
    executor = ProviderBindingExecutor(
        binding_store,
        admin,
        key_revealer=lambda provider, _record: revealed.append(provider.merchant_id) or SECRET,
    )

    result = executor.execute(request())

    assert result.summary()["mode"] == "dry-run"
    assert result.action == "create"
    assert result.applied is False
    assert revealed == [5]
    assert session.load().a6api_key == ""
    assert binding_store.prepared == []
    assert binding_store.completed == []
    assert admin.create_calls == []


def test_provider_only_context_can_reconcile_existing_binding(tmp_path: Path) -> None:
    session = save_session(tmp_path / "session.json")
    binding_store = FakeStore(context(session.path, roles=("provider",)))
    executor = ProviderBindingExecutor(
        binding_store,
        FakeAdmin(),
        key_revealer=lambda _provider, _record: SECRET,
    )

    result = executor.execute(request())

    assert result.summary()["mode"] == "dry-run"
    assert result.action == "create"


def test_apply_saves_revealed_key_creates_disabled_and_completes_audit(
    tmp_path: Path,
) -> None:
    session = save_session(tmp_path / "session.json", token_name="stale-name")
    binding_store = FakeStore(context(session.path))
    admin = FakeAdmin()
    executor = ProviderBindingExecutor(
        binding_store,
        admin,
        key_revealer=lambda _provider, _record: SECRET,
    )

    result = executor.execute(request(), apply=True)

    assert result.summary() == {
        "success": True,
        "mode": "applied",
        "merchant_id": 5,
        "external_a6_token_id": 14020,
        "binding_uid": "a6:merchant-5:token-14020",
        "action": "create",
        "applied": True,
        "account_id": 71,
        "session_updated": True,
        "verified": True,
    }
    saved = session.load()
    assert saved.a6api_key == SECRET
    assert saved.token_name == "gpt"
    assert session.path.stat().st_mode & 0o777 == 0o600
    assert len(binding_store.prepared) == 1
    prepared = binding_store.prepared[0]
    assert prepared["key_fingerprint"] == hashlib.sha256(SECRET.encode()).hexdigest()
    assert prepared["key_secret_ref"].endswith("session.json#a6api_key")
    assert SECRET not in repr(prepared)
    assert len(admin.create_calls) == 1
    assert admin.create_calls[0]["schedulable"] is False
    assert admin.create_calls[0]["concurrency"] == 100
    assert admin.create_calls[0]["proxy_id"] is None
    assert admin.create_calls[0]["credentials"]["api_key"] == SECRET
    assert (
        admin.create_calls[0]["credentials"]["base_url"] == "http://127.0.0.1:8767/a6-user/9517/v1"
    )
    assert admin.create_calls[0]["group_ids"] == []
    assert prepared["desired_state"]["egress_proxy_id"] == 5
    assert prepared["desired_state"]["proxy_id"] is None
    assert prepared["desired_state"]["upstream_relay"] is True
    assert admin.update_calls == [(71, {"group_ids": [26]})]
    assert len(binding_store.completed) == 1
    assert binding_store.failed == []
    assert SECRET not in repr(result)


def test_provider_binding_accepts_stable_pool_priority_above_legacy_two() -> None:
    value = request(priority=12)

    assert value.priority == 12


def test_repeated_existing_binding_is_noop_without_admin_mutation(tmp_path: Path) -> None:
    session = save_session(tmp_path / "session.json", key=SECRET)
    provider_context = context(
        session.path,
        key_fingerprint=hashlib.sha256(SECRET.encode()).hexdigest(),
    )
    binding_store = FakeStore(provider_context)
    admin = FakeAdmin(action="noop")
    kwargs = {
        "binding_uid": request().binding_uid,
        "name": request().account_name,
        "api_key": SECRET,
        "proxy_id": None,
        "base_url": "http://127.0.0.1:8767/a6-user/9517/v1",
        "model_mapping": dict(request().model_mapping),
        "pool_mode": True,
        "pool_mode_retry_count": 0,
        "pool_mode_retry_status_codes": [],
        "concurrency": request().concurrency,
        "priority": 1,
        "rate_multiplier": 1.0,
        "group_ids": [26],
        "extra": {
            "managed_by": "a6-trader",
            "a6_provider": True,
            "a6_binding_uid": request().binding_uid,
            "a6_api_key_fingerprint": hashlib.sha256(SECRET.encode()).hexdigest(),
            "a6_merchant_id": 5,
            "a6_user_id": 9517,
            "a6_supplier_id": 298,
            "external_a6_token_id": 14020,
        },
    }
    admin.account = account_from_provider(kwargs, account_id=71)
    executor = ProviderBindingExecutor(binding_store, admin)

    first = executor.execute(request(), apply=True)
    second = executor.execute(request(), apply=True)

    assert first.action == second.action == "noop"
    assert first.applied is second.applied is False
    assert first.verified is second.verified is True
    assert admin.create_calls == []
    assert admin.update_calls == []
    assert admin.scheduling_calls == []
    assert len(binding_store.completed) == 2


def test_create_forces_disabled_when_server_ignores_create_field(tmp_path: Path) -> None:
    class IgnoringAdmin(FakeAdmin):
        def create_account(self, payload):
            created = super().create_account(payload)
            assert self.account is not None
            self.account["schedulable"] = True
            return {**created, "schedulable": True}

    session = save_session(tmp_path / "session.json", key=SECRET)
    binding_store = FakeStore(context(session.path))
    admin = IgnoringAdmin()

    result = ProviderBindingExecutor(binding_store, admin).execute(request(), apply=True)

    assert result.verified is True
    assert admin.scheduling_calls == [(71, False)]
    assert admin.account is not None
    assert admin.account["schedulable"] is False
    assert admin.account["group_ids"] == [26]


def test_external_failure_marks_binding_error_without_secret(tmp_path: Path) -> None:
    class FailingAdmin(FakeAdmin):
        def create_account(self, payload):
            raise RuntimeError(f"provider rejected {payload['credentials']['api_key']}")

    session = save_session(tmp_path / "session.json", key=SECRET)
    binding_store = FakeStore(context(session.path))
    executor = ProviderBindingExecutor(binding_store, FailingAdmin())

    with pytest.raises(ProviderBindingError) as raised:
        executor.execute(request(), apply=True)

    assert raised.value.code == "provider_binding_runtime_error"
    assert raised.value.__cause__ is None
    assert SECRET not in str(raised.value)
    assert len(binding_store.failed) == 1
    failure = binding_store.failed[0]
    assert failure["error_class"] == "provider_binding_runtime_error"
    assert len(failure["error_fingerprint"]) == 64
    assert SECRET not in repr(failure)


@pytest.mark.parametrize(
    ("overrides", "error"),
    [
        ({"roles": ("seller",)}, "provider_binding_role_invalid"),
        ({"roles": ("provider", "seller")}, "provider_binding_role_invalid"),
        ({"roles": ("seller", "provider", "provider")}, "provider_binding_role_invalid"),
        ({"merchant_status": "allocated"}, "provider_binding_merchant_status_invalid"),
        ({"token_status": "disabled"}, "provider_binding_token_status_invalid"),
        ({"proxy_lease_id": ""}, "provider_binding_proxy_lease_invalid"),
    ],
)
def test_context_validation_precedes_all_writes(
    tmp_path: Path,
    overrides: dict[str, Any],
    error: str,
) -> None:
    session = save_session(tmp_path / "session.json", key=SECRET)
    binding_store = FakeStore(context(session.path, **overrides))
    executor = ProviderBindingExecutor(binding_store, FakeAdmin())

    with pytest.raises(ProviderBindingError, match=error):
        executor.execute(request(), apply=True)

    assert binding_store.prepared == []
    assert binding_store.failed == []


@pytest.mark.parametrize("running_booster_action", (False, True))
def test_postgres_prepare_serializes_against_running_booster_action(
    tmp_path: Path,
    running_booster_action: bool,
) -> None:
    session = save_session(tmp_path / "session.json", key=SECRET)
    provider_context = context(session.path)

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
            if normalized.startswith("SELECT m.id AS merchant_id"):
                self.one = {
                    "merchant_id": 5,
                    "a6_user_id": 9517,
                    "supplier_id": 298,
                    "merchant_status": "supplier_submitted",
                    "roles": ["seller", "provider"],
                    "session_ref": str(session.path),
                    "proxy_lease_id": "singbox:23084",
                    "token_row_id": 4,
                    "external_a6_token_id": 14020,
                    "token_name": "gpt",
                    "token_status": "1",
                    "key_fingerprint": "",
                    "key_secret_ref": "",
                }
            elif normalized.startswith("SELECT id FROM a6_trade.a6_tokens"):
                self.one = {"id": 4}
            elif normalized.startswith("SELECT id FROM a6_trade.a6_channel_growth_actions"):
                if self.connection.running_booster_action:
                    self.one = {"id": 88}
            elif normalized.startswith("INSERT INTO a6_trade.enodot_account_bindings"):
                self.one = {"id": 9, "enodot_account_id": None, "state": "planned"}
            elif normalized.startswith("INSERT INTO a6_trade.trade_control_actions"):
                self.one = {"id": 10, "status": "started"}
            elif normalized.startswith("SELECT coalesce(max(attempt_no)"):
                self.one = {"attempt_no": 1}
            return self

        def fetchone(self):
            return self.one

    class Connection:
        def __init__(self):
            self.calls = []
            self.running_booster_action = running_booster_action
            self.commits = 0
            self.rollbacks = 0
            self.closed = False

        def cursor(self):
            return Cursor(self)

        def commit(self):
            self.commits += 1

        def rollback(self):
            self.rollbacks += 1

        def close(self):
            self.closed = True

    connection = Connection()
    binding_request = request()
    fingerprint = hashlib.sha256(SECRET.encode()).hexdigest()
    desired = {"binding_uid": binding_request.binding_uid, "key_fingerprint": fingerprint}
    store = PostgresProviderBindingStore(lambda: connection)

    if running_booster_action:
        with pytest.raises(
            ProviderBindingError,
            match="provider_binding_booster_action_running",
        ):
            store.prepare_binding(
                provider_context,
                binding_request,
                key_fingerprint=fingerprint,
                key_secret_ref=f"{session.path}#a6api_key",
                desired_state=desired,
                idempotency_key="bind-provider:fixture",
            )
        assert connection.commits == 0
        assert connection.rollbacks == 1
        assert not any(
            "INSERT INTO a6_trade.enodot_account_bindings" in query
            for query, _params in connection.calls
        )
        return

    prepared = store.prepare_binding(
        provider_context,
        binding_request,
        key_fingerprint=fingerprint,
        key_secret_ref=f"{session.path}#a6api_key",
        desired_state=desired,
        idempotency_key="bind-provider:fixture",
    )

    assert prepared == BindingPreparation(9, 10, 1, None, "planned")
    assert connection.commits == 1
    assert connection.rollbacks == 0
    assert connection.closed is True
    rendered = repr(connection.calls)
    assert SECRET not in rendered
    assert "INSERT INTO a6_trade.enodot_account_bindings" in rendered
    assert "INSERT INTO a6_trade.trade_control_actions" in rendered
    assert "INSERT INTO a6_trade.trade_control_action_attempts" in rendered
    token_lock_index = next(
        index
        for index, (query, _params) in enumerate(connection.calls)
        if "SELECT id FROM a6_trade.a6_tokens" in " ".join(query.split())
    )
    merchant_lock_index = next(
        index
        for index, (query, _params) in enumerate(connection.calls)
        if "FOR SHARE OF m" in " ".join(query.split())
    )
    assert token_lock_index < merchant_lock_index
