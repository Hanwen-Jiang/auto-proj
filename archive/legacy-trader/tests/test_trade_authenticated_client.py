from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from a6api_auto import cli
from a6api_auto import trade_channel_growth as channel_growth
from a6api_auto import trade_pin_runner as pin_runner
from a6api_auto import trade_provider_topup as provider_topup
from a6api_auto import trade_seller_retirement as seller_retirement
from a6api_auto.models import SessionRecord
from a6api_auto.storage import SessionStore
from a6api_auto.trade_authenticated_client import (
    AuthenticatedSessionError,
    authenticated_a6_client,
    session_lock_path,
)


def _save_session(path: Path, **overrides: Any) -> SessionStore:
    values = {
        "schema_version": 1,
        "origin": "https://a6api.com",
        "email": "merchant@fixture.invalid",
        "username": "merchant",
        "password": "password-secret",
        "user_id": 42,
        "cookies": {"session": "old-cookie"},
        "status": "supplier_submitted",
        "supplier_id": 84,
    }
    values.update(overrides)
    store = SessionStore(path)
    store.save(SessionRecord(**values))
    return store


class FakeClient:
    def __init__(
        self,
        *,
        current: dict[str, Any] | None,
        login_result: dict[str, Any] | None = None,
    ) -> None:
        self.current = current
        self.login_result = login_result
        self.imported: dict[str, str] = {}
        self.login_calls: list[tuple[str, str]] = []

    def import_cookies(self, cookies: dict[str, str]) -> None:
        self.imported = dict(cookies)

    def self_user(self, user_id: int | None = None) -> dict[str, Any] | None:
        assert user_id == 42
        return self.current

    def login(self, username: str, password: str) -> dict[str, Any] | None:
        self.login_calls.append((username, password))
        return self.login_result

    def export_cookies(self) -> dict[str, str]:
        return {"session": "fresh-cookie"}


def test_valid_cookie_is_verified_without_login_and_saved_under_sidecar_lock(
    tmp_path: Path,
) -> None:
    path = tmp_path / "session-valid.json"
    store = _save_session(path)
    client = FakeClient(current={"id": 42, "username": "merchant"})

    observed, record = authenticated_a6_client(
        path,
        expected_user_id=42,
        proxy_url="http://127.0.0.1:23001",
        client_factory=lambda _record, _proxy: client,
    )

    assert observed is client
    assert client.imported == {"session": "old-cookie"}
    assert client.login_calls == []
    assert record.user_id == 42
    assert store.load().cookies == {"session": "fresh-cookie"}
    lock_path = session_lock_path(path)
    assert lock_path.is_file()
    assert lock_path.stat().st_mode & 0o077 == 0


def test_expired_cookie_relogs_in_and_atomically_persists_new_cookie(tmp_path: Path) -> None:
    path = tmp_path / "session-expired.json"
    store = _save_session(path)
    client = FakeClient(
        current=None,
        login_result={"id": 42, "username": "merchant-renamed"},
    )

    _observed, record = authenticated_a6_client(
        path,
        expected_user_id=42,
        proxy_url="http://127.0.0.1:23001",
        client_factory=lambda _record, _proxy: client,
    )

    assert client.login_calls == [("merchant", "password-secret")]
    assert record.username == "merchant-renamed"
    persisted = store.load()
    assert persisted.username == "merchant-renamed"
    assert persisted.cookies == {"session": "fresh-cookie"}
    assert persisted.status == "supplier_submitted"


def test_relogin_identity_mismatch_does_not_overwrite_session(tmp_path: Path) -> None:
    path = tmp_path / "session-mismatch.json"
    store = _save_session(path)
    client = FakeClient(
        current=None,
        login_result={"id": 99, "username": "other-account"},
    )

    with pytest.raises(AuthenticatedSessionError, match="session_user_identity_mismatch"):
        authenticated_a6_client(
            path,
            expected_user_id=42,
            proxy_url="http://127.0.0.1:23001",
            client_factory=lambda _record, _proxy: client,
        )

    persisted = store.load()
    assert persisted.user_id == 42
    assert persisted.username == "merchant"
    assert persisted.cookies == {"session": "old-cookie"}


@pytest.mark.parametrize("status", ("allocated", "abandoned"))
def test_ineligible_status_is_rejected_before_client_or_mailbox_use(
    tmp_path: Path,
    status: str,
) -> None:
    path = tmp_path / f"session-{status}.json"
    _save_session(path, status=status)
    factory_calls: list[bool] = []

    with pytest.raises(AuthenticatedSessionError, match="session_not_refreshable"):
        authenticated_a6_client(
            path,
            expected_user_id=42,
            proxy_url="http://127.0.0.1:23001",
            client_factory=lambda _record, _proxy: factory_calls.append(True),
        )

    assert factory_calls == []


def test_lock_path_is_stable_and_separate_from_replaceable_session_inode(
    tmp_path: Path,
) -> None:
    path = tmp_path / "session.json"

    assert session_lock_path(path) == tmp_path.resolve() / ".session.json.auth.lock"
    assert session_lock_path(path) != path.resolve()


def test_withdraw_cli_factory_uses_authenticated_session_helper(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    path = tmp_path / "session-withdraw.json"
    record = _save_session(path).load()
    observed_client = object()
    captured: dict[str, Any] = {}

    def recover(session_ref: Path, **kwargs: Any) -> tuple[Any, SessionRecord]:
        captured.update(session_ref=session_ref, **kwargs)
        return observed_client, record

    monkeypatch.setattr(cli, "authenticated_a6_client", recover)
    merchant = SimpleNamespace(
        a6_user_id=42,
        supplier_id=84,
        session_ref="/stale/database/session.json",
    )

    assert cli.authenticated_withdraw_client(
        merchant,
        session_paths_by_user_id={42: path},
        proxy_by_user_id={42: "http://127.0.0.1:23006"},
    ) is observed_client
    assert captured == {
        "session_ref": path,
        "expected_user_id": 42,
        "proxy_url": "http://127.0.0.1:23006",
        "client_factory": None,
    }


def test_cli_session_factories_preserve_domain_session_mismatch_codes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    path = tmp_path / "session-domain-errors.json"
    _save_session(path)

    def mismatch(*_args: Any, **_kwargs: Any) -> tuple[Any, SessionRecord]:
        raise AuthenticatedSessionError("session_user_identity_mismatch")

    monkeypatch.setattr(cli, "authenticated_a6_client", mismatch)
    withdraw_merchant = SimpleNamespace(
        a6_user_id=42,
        supplier_id=84,
        session_ref=str(path),
    )
    seller_context = SimpleNamespace(
        a6_user_id=42,
        supplier_id=84,
        session_ref=str(path),
    )

    with pytest.raises(cli.WithdrawAccountError, match="^withdraw_session_mismatch$"):
        cli.authenticated_withdraw_client(
            withdraw_merchant,
            session_paths_by_user_id={42: path},
            proxy_by_user_id={42: "http://127.0.0.1:23006"},
        )
    with pytest.raises(
        cli.SellerOrchestrationError,
        match="^seller_session_context_mismatch$",
    ):
        cli.authenticated_seller_client(
            seller_context,
            proxy_by_user_id={42: "http://127.0.0.1:23007"},
        )


def _patch_seller_cli_dependencies(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "_database_dsn", lambda _args: "fixture-dsn")
    monkeypatch.setattr(
        cli,
        "_proxy_map",
        lambda _path: {42: "http://127.0.0.1:23007"},
    )
    monkeypatch.setattr(cli, "_secret_value", lambda *_args: "admin-key")
    monkeypatch.setattr(cli, "EnodotAdminClient", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(cli, "ManagedSellerService", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(cli, "PostgresManagedListingStore", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(
        cli,
        "PrivateFileManagedSellerSecretSink",
        lambda *_args, **_kwargs: object(),
    )


def _seller_recovery_fixture(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> tuple[SimpleNamespace, SessionRecord, object, dict[str, Any]]:
    path = tmp_path / "session-seller.json"
    record = _save_session(path).load()
    context = SimpleNamespace(
        a6_user_id=42,
        supplier_id=84,
        session_ref=str(path),
    )
    observed_client = object()
    captured: dict[str, Any] = {}

    def recover(received: Any, **kwargs: Any) -> Any:
        captured.update(context=received, **kwargs)
        return observed_client

    monkeypatch.setattr(cli, "authenticated_seller_client", recover)
    _patch_seller_cli_dependencies(monkeypatch)
    return context, record, observed_client, captured


def test_seller_resources_cli_factory_uses_authenticated_session_helper(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    context, record, observed_client, captured = _seller_recovery_fixture(
        monkeypatch,
        tmp_path,
    )

    class Orchestrator:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            self.client_factory = _args[4]

        def execute(self, _request: Any, *, apply: bool) -> Any:
            assert apply is False
            assert self.client_factory(context, record) is observed_client
            return SimpleNamespace(summary=lambda: {"success": True})

    monkeypatch.setattr(cli, "SellerResourceOrchestrator", Orchestrator)
    args = cli.parser().parse_args(
        [
            "trade-ensure-seller-resources",
            "--merchant",
            "7",
            "--group-id",
            "26",
            "--delta",
            "0.5",
            "--cost-multiplier",
            "0.03",
            "--listing-description",
            "Team source",
            "--proxy-map-file",
            "fixture-proxies.json",
        ]
    )

    result = cli.trade_ensure_seller_resources_command(args, seller_store=object())

    assert result == {"success": True}
    assert captured["context"] is context
    assert captured["proxy_by_user_id"] == {42: "http://127.0.0.1:23007"}
    assert callable(captured["client_factory"])


def test_seller_upstream_cli_factory_uses_authenticated_session_helper(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    context, record, observed_client, captured = _seller_recovery_fixture(
        monkeypatch,
        tmp_path,
    )

    class Reconciler:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            self.client_factory = _args[3]

        def execute(self, _request: Any, *, apply: bool) -> Any:
            assert apply is False
            assert self.client_factory(context, record) is observed_client
            return SimpleNamespace(summary=lambda: {"success": True})

    monkeypatch.setattr(cli, "SellerUpstreamReconciler", Reconciler)
    args = cli.parser().parse_args(
        [
            "trade-reconcile-seller-upstreams",
            "--merchant",
            "7",
            "--proxy-map-file",
            "fixture-proxies.json",
        ]
    )

    result = cli.trade_reconcile_seller_upstreams_command(args, seller_store=object())

    assert result == {"success": True}
    assert captured == {
        "context": context,
        "proxy_by_user_id": {42: "http://127.0.0.1:23007"},
    }


def test_seller_price_band_cli_factory_uses_cached_refreshed_session(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    context, record, observed_client, captured = _seller_recovery_fixture(
        monkeypatch,
        tmp_path,
    )
    descriptions = tmp_path / "listing-descriptions.json"
    descriptions.write_text('{"7":"Team source"}', encoding="utf-8")
    descriptions.chmod(0o600)

    def cached(received: Any, session: Any, **kwargs: Any) -> Any:
        captured.update(context=received, session=session, **kwargs)
        return observed_client

    monkeypatch.setattr(cli, "cached_seller_client", cached)

    class Resources:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            self.client_factory = _args[4]

    class PriceBand:
        def __init__(self, _store: Any, resources: Resources) -> None:
            self.resources = resources

        def execute(self, _request: Any, **_kwargs: Any) -> Any:
            assert self.resources.client_factory(context, record) is observed_client
            return SimpleNamespace(summary=lambda: {"success": True})

    monkeypatch.setattr(cli, "SellerResourceOrchestrator", Resources)
    monkeypatch.setattr(cli, "SellerPriceBandOrchestrator", PriceBand)
    monkeypatch.setattr(cli, "PostgresSellerPriceBandStore", lambda *_args: object())
    args = cli.parser().parse_args(
        [
            "trade-apply-seller-price-band",
            "--merchant",
            "7",
            "--group-id",
            "26",
            "--listing-description-map-file",
            str(descriptions),
            "--proxy-map-file",
            "fixture-proxies.json",
        ]
    )

    result = cli.trade_apply_seller_price_band_command(args, seller_store=object())

    assert result == {"success": True}
    assert captured["context"] is context
    assert captured["session"] is record
    assert captured["proxy_by_user_id"] == {42: "http://127.0.0.1:23007"}
    assert callable(captured["client_factory"])


def test_channel_growth_runner_client_uses_authenticated_session_helper(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    path = tmp_path / "session-growth.json"
    record = _save_session(path).load()
    observed_client = object()
    captured: dict[str, Any] = {}

    def recover(session_ref: Path, **kwargs: Any) -> tuple[Any, SessionRecord]:
        captured.update(session_ref=session_ref, **kwargs)
        return observed_client, record

    monkeypatch.setattr(channel_growth, "authenticated_channel_growth_client", recover)
    runner = object.__new__(channel_growth.ChannelGrowthRunner)
    runner._session_paths = {42: path}
    runner._records = {42: record}
    runner.proxy_by_user_id = {42: "http://127.0.0.1:23001"}

    assert runner.a6_client(42) is observed_client
    assert captured == {
        "session_ref": path,
        "user_id": 42,
        "proxy_url": "http://127.0.0.1:23001",
    }


def test_pin_runner_client_uses_authenticated_session_helper(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    path = tmp_path / "session-pin.json"
    record = _save_session(path).load()
    observed_client = object()
    captured: dict[str, Any] = {}

    def recover(session_ref: Path, **kwargs: Any) -> tuple[Any, SessionRecord]:
        captured.update(session_ref=session_ref, **kwargs)
        return observed_client, record

    monkeypatch.setattr(pin_runner, "authenticated_a6_client", recover)
    runner = object.__new__(pin_runner.ProviderPinRunner)
    runner._session_paths = {42: path}
    runner._records = {42: record}
    runner.proxy_by_user_id = {42: "http://127.0.0.1:23002"}
    runner.client_timeout = 5.0

    assert runner._client(42) is observed_client
    assert captured["session_ref"] == path
    assert captured["expected_user_id"] == 42
    assert captured["proxy_url"] == "http://127.0.0.1:23002"
    assert callable(captured["client_factory"])


def test_provider_topup_factory_uses_authenticated_session_helper(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    path = tmp_path / "session-topup.json"
    record = _save_session(path).load()
    observed_client = object()
    captured: dict[str, Any] = {}

    def recover(session_ref: str, **kwargs: Any) -> tuple[Any, SessionRecord]:
        captured.update(session_ref=session_ref, **kwargs)
        return observed_client, record

    monkeypatch.setattr(provider_topup, "authenticated_a6_client", recover)
    context = provider_topup.ProviderTopupContext(7, 42, str(path), 500_000)

    assert provider_topup.authenticated_provider_topup_client(
        context,
        proxy_by_user_id={42: "http://127.0.0.1:23003"},
    ) is observed_client
    assert captured == {
        "session_ref": str(path),
        "expected_user_id": 42,
        "proxy_url": "http://127.0.0.1:23003",
        "client_factory": None,
    }


def test_seller_retirement_remote_uses_authenticated_session_helper(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    path = tmp_path / "session-retirement.json"
    record = _save_session(path).load()
    observed_client = object()
    captured: dict[str, Any] = {}

    def recover(session_ref: str, **kwargs: Any) -> tuple[Any, SessionRecord]:
        captured.update(session_ref=session_ref, **kwargs)
        return observed_client, record

    monkeypatch.setattr(seller_retirement, "authenticated_a6_client", recover)
    remote = seller_retirement.A6SellerRetirementRemote(
        lambda: None,
        proxy_by_user_id={42: "http://127.0.0.1:23004"},
        enodot_admin=object(),  # type: ignore[arg-type]
    )
    context = seller_retirement.SellerRetirementContext(
        merchant_id=7,
        a6_user_id=42,
        supplier_id=84,
        session_ref=str(path),
        proxy_lease_id="singbox:23004",
        roles=("seller",),
        quota_per_unit=500_000,
        snapshot_withdrawable=0,
        state="checking",
        transfer_intent_id=None,
        external_a6_token_id=14001,
        token_name="merchant-7",
        other_observer_count=3,
    )

    assert remote._client(context) is observed_client
    assert captured == {
        "session_ref": str(path),
        "expected_user_id": 42,
        "proxy_url": "http://127.0.0.1:23004",
        "client_factory": None,
    }


def test_channel_growth_cli_listing_factory_uses_authenticated_session_helper(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    path = tmp_path / "session-growth-cli.json"
    record = _save_session(path).load()
    observed_client = object()
    captured: dict[str, Any] = {}

    def recover(session_ref: Path, **kwargs: Any) -> tuple[Any, SessionRecord]:
        captured.update(session_ref=session_ref, **kwargs)
        return observed_client, record

    class FakeRunner:
        def __init__(self, _store: Any, *, listing_controller: Any, **_kwargs: Any) -> None:
            self.listing_controller = listing_controller

        def run(self, **_kwargs: Any) -> dict[str, Any]:
            merchant = SimpleNamespace(a6_user_id=42)
            assert self.listing_controller.client_factory(merchant) is observed_client
            return {"success": True}

    monkeypatch.setattr(cli, "_database_dsn", lambda _args: "fixture-dsn")
    monkeypatch.setattr(
        cli,
        "_proxy_map",
        lambda _path: {42: "http://127.0.0.1:23005"},
    )
    monkeypatch.setattr(cli, "_trade_session_paths", lambda _path: (path,))
    monkeypatch.setattr(cli, "_secret_value", lambda *_args: "admin-key")
    monkeypatch.setattr(cli, "authenticated_channel_growth_client", recover)
    monkeypatch.setattr(cli, "ChannelGrowthRunner", FakeRunner)
    monkeypatch.setattr(cli, "PostgresChannelGrowthStore", lambda *_args: object())
    monkeypatch.setattr(cli, "PostgresManagedListingStore", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(cli, "EnodotAdminClient", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(cli, "PrivateFileManagedSellerSecretSink", lambda *_args: object())
    monkeypatch.setattr(cli, "CallbackTransport", lambda **_kwargs: object())
    args = SimpleNamespace(
        proxy_map_file=tmp_path / "proxy-map.json",
        enodot_admin_token_file=None,
        enodot_admin_key_file=None,
        enodot_admin_key_env="ADMIN_KEY",
        sessions_dir=tmp_path,
        config_version="fixture-v1",
        enodot_admin_url="https://admin.fixture.invalid",
        seller_key_dir=tmp_path / "seller-keys",
        callback_timeout_seconds=5,
        callback_gateway_url="https://a6.a6api.com",
        model="gpt-5.6-sol",
        signal_max_age_seconds=45,
        recycle_min_delay_seconds=0.8,
        recycle_max_delay_seconds=2.0,
        apply=True,
    )

    assert cli.trade_reconcile_channel_growth_once_command(args) == {"success": True}
    assert captured == {
        "session_ref": path.resolve(),
        "user_id": 42,
        "proxy_url": "http://127.0.0.1:23005",
    }
