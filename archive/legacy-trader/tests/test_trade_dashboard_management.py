from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from a6api_auto.errors import A6AutoError
from a6api_auto.models import SessionRecord
from a6api_auto.storage import SessionStore, write_private_json
from a6api_auto.trade_authenticated_client import AuthenticatedSessionError
from a6api_auto.trade_dashboard_management import (
    DashboardManagementError,
    DashboardManagementService,
)


class CloseOnlyConnection:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class AccountClient:
    def __init__(self, user_id: int = 701) -> None:
        self.user_id = user_id

    def login(self, username: str, password: str):
        assert username
        assert password
        return {"id": self.user_id}

    def self_user(self, user_id: int):
        assert user_id == self.user_id
        return {
            "id": self.user_id,
            "email": "account@example.invalid",
            "quota_per_unit": 500_000,
        }

    def supplier_me(self, user_id: int):
        assert user_id == self.user_id
        return {"id": 801, "name": "NexusAPI", "description": "稳定 API 服务"}

    def export_cookies(self):
        return {"session": "SESSION_FIXTURE"}


class InventoryClient:
    def __init__(self) -> None:
        self.profile_calls = 0
        self.profile = {
            "id": 801,
            "name": "NexusAPI",
            "description": "稳定 API 服务",
            "contact_info": "contact@example.invalid",
        }
        self.channels = [
            {
                "id": 1148,
                "binding_id": 9148,
                "name": "SOL Route",
                "nickname": "SOL Route",
                "description": "稳定线路",
                "models": "gpt-5.6-sol",
                "status": 1,
                "availability": 1,
            }
        ]
        self.listings = [
            {
                "id": 4100,
                "channel_id": 1148,
                "own_model": "gpt-5.6-sol",
                "platform_model": "gpt-5.6-sol",
                "charge_type": "tokens",
                "input_price_micros": 1,
                "output_price_micros": 2,
                "cache_read_price_micros": 3,
                "cache_write_price_micros": 4,
                "display_name": "SOL",
                "description": "Team池",
                "status": 2,
                "availability": 1,
                "visibility": 1,
            }
        ]
        self.channel_updates: list[tuple[int, int, dict[str, object]]] = []
        self.listing_updates: list[tuple[int, int, dict[str, object]]] = []

    def supplier_me(self, user_id: int):
        assert user_id == 701
        self.profile_calls += 1
        return dict(self.profile)

    def supplier_channels(self, user_id: int):
        assert user_id == 701
        return [dict(channel) for channel in self.channels]

    def update_supplier_channel(self, user_id: int, channel_id: int, payload):
        self.channel_updates.append((user_id, channel_id, dict(payload)))
        self.channels[0].update(payload)
        return {}

    def supplier_listings(self, user_id: int, *, page: int, size: int):
        assert user_id == 701
        assert size == 100
        return {
            "items": [dict(item) for item in self.listings] if page == 1 else [],
            "total": len(self.listings),
        }

    def update_supplier_listing(self, user_id: int, listing_id: int, payload):
        self.listing_updates.append((user_id, listing_id, dict(payload)))
        self.listings[0].update(payload)
        return {}


def service_fixture(tmp_path: Path, *, client_factory=None) -> DashboardManagementService:
    sessions = tmp_path / "sessions"
    sessions.mkdir(mode=0o700)
    os.chmod(sessions, 0o700)
    proxy_map = tmp_path / "proxy-map.json"
    proxy_pool = tmp_path / "proxy-pool.json"
    write_private_json(proxy_map, {})
    write_private_json(proxy_pool, ["socks5h://127.0.0.1:23081"])
    return DashboardManagementService(
        lambda: CloseOnlyConnection(),
        sessions_dir=sessions,
        proxy_map_file=proxy_map,
        proxy_pool_file=proxy_pool,
        client_factory=client_factory,
    )


def test_add_account_reserves_a_unique_unclaimed_proxy_and_imports_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = service_fixture(tmp_path)
    reserved_proxy = "socks5h://127.0.0.1:23081"
    selected_proxy = "socks5h://127.0.0.1:23082"
    write_private_json(service.proxy_pool_file, [reserved_proxy, selected_proxy])
    batch = tmp_path / "batch.json"
    write_private_json(
        batch,
        {
            "allocated_proxy_fingerprints": [
                hashlib.sha256(reserved_proxy.encode()).hexdigest()[:12]
            ]
        },
    )
    service.batch_state_file = batch
    clients: list[tuple[str, str]] = []
    service.client_factory = lambda origin, proxy: (
        clients.append((origin, proxy)) or AccountClient()
    )
    imported = []
    binding = {
        "merchant_id": 23,
        "a6_user_id": 701,
        "supplier_id": 801,
        "session_ref": str(service.sessions_dir / "session-manual-701.json"),
    }
    monkeypatch.setattr(
        "a6api_auto.trade_dashboard_management.upsert_merchant_metadata",
        lambda _connection, rows: imported.extend(rows) or len(rows),
    )
    monkeypatch.setattr(
        service,
        "_merchant_binding_by_user",
        lambda _user_id: binding if imported else None,
    )

    result = service.add_account("fixture", "correct-password")

    assert result == {
        "merchant_id": "23",
        "a6_user_id": "701",
        "status": "imported",
        "supplier_status": "supplier_submitted",
        "collection": "scheduled",
    }
    assert clients == [("https://a6api.com", selected_proxy)]
    assert imported[0].user_id == 701
    assert imported[0].proxy_lease_id == "singbox:23082"
    assert SessionStore(Path(binding["session_ref"])).load().password == "correct-password"
    assert service._proxy_map() == {701: selected_proxy}


def test_existing_account_refreshes_credentials_and_repairs_missing_proxy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = service_fixture(tmp_path)
    session_path = service.sessions_dir / "session-existing.json"
    SessionStore(session_path).save(
        SessionRecord(
            schema_version=1,
            origin="https://a6api.com",
            email="account@example.invalid",
            username="fixture",
            password="old-password",
            user_id=701,
            cookies={"session": "OLD"},
            status="supplier_submitted",
            supplier_id=801,
        )
    )
    binding = {
        "merchant_id": 23,
        "a6_user_id": 701,
        "supplier_id": 801,
        "session_ref": str(session_path),
    }
    imported = []
    service.client_factory = lambda _origin, _proxy: AccountClient()
    monkeypatch.setattr(service, "_merchant_binding_by_user", lambda _user_id: binding)
    monkeypatch.setattr(
        "a6api_auto.trade_dashboard_management.upsert_merchant_metadata",
        lambda _connection, rows: imported.extend(rows) or len(rows),
    )

    result = service.add_account("fixture", "new-password")

    assert result["status"] == "existing"
    assert SessionStore(session_path).load().password == "new-password"
    assert service._proxy_map() == {701: "socks5h://127.0.0.1:23081"}
    assert len(imported) == 1


def test_authenticated_inventory_client_uses_shared_session_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = service_fixture(tmp_path)
    session_path = service.sessions_dir / "session-existing.json"
    record = SessionRecord(
        schema_version=1,
        origin="https://a6api.com",
        email="account@example.invalid",
        username="fixture",
        password="secret",
        user_id=701,
        cookies={"session": "OLD"},
        status="supplier_submitted",
        supplier_id=801,
    )
    SessionStore(session_path).save(record)
    write_private_json(service.proxy_map_file, {"701": "socks5h://127.0.0.1:23081"})
    monkeypatch.setattr(
        service,
        "_merchant_binding",
        lambda _merchant_id: {
            "merchant_id": 23,
            "a6_user_id": 701,
            "supplier_id": 801,
            "session_ref": str(session_path),
        },
    )
    observed_client = object()
    built: list[tuple[str, str]] = []
    service.client_factory = lambda origin, proxy: (
        built.append((origin, proxy)) or observed_client
    )
    captured: dict[str, object] = {}

    def recover(session_ref: Path, **kwargs):
        captured.update(session_ref=session_ref, **kwargs)
        assert kwargs["client_factory"](record, kwargs["proxy_url"]) is observed_client
        return observed_client, record

    monkeypatch.setattr(
        "a6api_auto.trade_dashboard_management.authenticated_a6_client",
        recover,
    )

    client, current = service._authenticated_client(23)

    assert client is observed_client
    assert current is record
    assert captured["session_ref"] == session_path.resolve()
    assert captured["expected_user_id"] == 701
    assert captured["proxy_url"] == "socks5h://127.0.0.1:23081"
    assert built == [("https://a6api.com", "socks5h://127.0.0.1:23081")]


@pytest.mark.parametrize(
    ("session_code", "management_code"),
    (
        ("session_relogin_failed", "merchant_relogin_failed"),
        ("session_user_identity_mismatch", "merchant_identity_mismatch"),
    ),
)
def test_authenticated_inventory_client_preserves_management_error_codes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    session_code: str,
    management_code: str,
) -> None:
    service = service_fixture(tmp_path)
    session_path = service.sessions_dir / "session-existing.json"
    SessionStore(session_path).save(
        SessionRecord(
            schema_version=1,
            origin="https://a6api.com",
            email="account@example.invalid",
            username="fixture",
            password="secret",
            user_id=701,
            cookies={"session": "OLD"},
            status="supplier_submitted",
            supplier_id=801,
        )
    )
    write_private_json(service.proxy_map_file, {"701": "socks5h://127.0.0.1:23081"})
    monkeypatch.setattr(
        service,
        "_merchant_binding",
        lambda _merchant_id: {
            "merchant_id": 23,
            "a6_user_id": 701,
            "supplier_id": 801,
            "session_ref": str(session_path),
        },
    )

    def fail(*_args, **_kwargs):
        raise AuthenticatedSessionError(session_code)

    monkeypatch.setattr(
        "a6api_auto.trade_dashboard_management.authenticated_a6_client",
        fail,
    )

    with pytest.raises(DashboardManagementError, match=f"^{management_code}$"):
        service._authenticated_client(23)


def test_inventory_editors_reject_channel_name_and_confirm_remote_readback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = service_fixture(tmp_path)
    client = InventoryClient()
    record = SessionRecord(
        schema_version=1,
        origin="https://a6api.com",
        email="account@example.invalid",
        username="fixture",
        password="secret",
        user_id=701,
    )
    monkeypatch.setattr(service, "_authenticated_client", lambda _merchant_id: (client, record))

    with pytest.raises(DashboardManagementError, match="supplier_channel_patch_invalid"):
        service.update_channel(23, 1148, {"name": "Renamed Route"})

    channel = service.update_channel(
        23,
        1148,
        {"models": "gpt-5.6-sol,gpt-5.6-terra"},
    )
    listing = service.update_listing(23, 4100, {"description": "Team+Pro混池"})

    assert channel["channel"]["models"] == ["gpt-5.6-sol", "gpt-5.6-terra"]
    assert client.channel_updates == [
        (701, 9148, {"models": "gpt-5.6-sol,gpt-5.6-terra"})
    ]
    assert listing["listing"]["description"] == "Team+Pro混池"
    assert client.listing_updates[0][2]["input_price_micros"] == 1
    assert client.listing_updates[0][2]["display_name"] == "SOL"


def test_inventory_uses_persisted_supplier_profile_without_remote_profile_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = service_fixture(tmp_path)
    client = InventoryClient()
    record = SessionRecord(
        schema_version=1,
        origin="https://a6api.com",
        email="account@example.invalid",
        username="fixture",
        password="secret",
        user_id=701,
    )
    monkeypatch.setattr(service, "_authenticated_client", lambda _merchant_id: (client, record))
    monkeypatch.setattr(
        service,
        "_stored_supplier_profile",
        lambda _merchant_id: {
            "id": "801",
            "name": "Persisted Gateway",
            "description": "持久化商户简介",
            "contact_info": "",
        },
    )

    inventory = service.get_inventory(23)

    assert inventory["supplier"]["name"] == "Persisted Gateway"
    assert inventory["supplier"]["description"] == "持久化商户简介"
    assert client.profile_calls == 0


def test_inventory_backfills_missing_supplier_profile_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = service_fixture(tmp_path)
    client = InventoryClient()
    record = SessionRecord(
        schema_version=1,
        origin="https://a6api.com",
        email="account@example.invalid",
        username="fixture",
        password="secret",
        user_id=701,
    )
    persisted: list[tuple[int, dict[str, object]]] = []
    monkeypatch.setattr(service, "_authenticated_client", lambda _merchant_id: (client, record))
    monkeypatch.setattr(service, "_stored_supplier_profile", lambda _merchant_id: None)
    monkeypatch.setattr(
        service,
        "_persist_supplier_profile",
        lambda merchant_id, profile: persisted.append((merchant_id, dict(profile))),
    )

    inventory = service.get_inventory(23)

    assert inventory["supplier"]["name"] == "NexusAPI"
    assert client.profile_calls == 1
    assert persisted == [(23, client.profile)]


def test_optional_supplier_profile_only_swallows_the_absent_supplier_code(
    tmp_path: Path,
) -> None:
    service = service_fixture(tmp_path)

    class Missing:
        def supplier_me(self, _user_id: int):
            raise A6AutoError("supplier_self_failed")

    class Broken:
        def supplier_me(self, _user_id: int):
            raise A6AutoError("supplier_transport_failed")

    assert service._optional_supplier_profile(Missing(), 701) == {}
    with pytest.raises(A6AutoError, match="supplier_transport_failed"):
        service._optional_supplier_profile(Broken(), 701)


class RetirementCursor:
    def __init__(self) -> None:
        self.current = None
        self.queries: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def execute(self, query: str, _params=None) -> None:
        self.queries.append(query)
        if "SELECT id, quota_per_unit, roles" in query:
            self.current = {"id": 23, "quota_per_unit": 500_000, "roles": ["provider"]}
        elif "RETURNING state, archive_mode" in query:
            self.current = {
                "state": "role_switch",
                "archive_mode": True,
                "requested_at": None,
                "updated_at": None,
                "completed_at": None,
                "last_error_class": None,
            }
        else:
            self.current = None

    def fetchone(self):
        return self.current


class RetirementConnection:
    def __init__(self) -> None:
        self.cursor_value = RetirementCursor()
        self.committed = False
        self.rolled_back = False

    def cursor(self):
        return self.cursor_value

    def commit(self) -> None:
        self.committed = True

    def rollback(self) -> None:
        self.rolled_back = True

    def close(self) -> None:
        return None


def test_safe_retirement_reopens_a_completed_legacy_plan(tmp_path: Path) -> None:
    connection = RetirementConnection()
    service = service_fixture(tmp_path)
    service.connection_factory = lambda: connection

    result = service.request_retirement(23)

    assert result["phase"] == "retiring"
    assert result["archive_mode"] is True
    assert connection.committed is True
    upsert = next(query for query in connection.cursor_value.queries if "ON CONFLICT" in query)
    assert "THEN 'role_switch'" in upsert
    assert "completed_at = CASE" in upsert
