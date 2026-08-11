from __future__ import annotations

from pathlib import Path

import pytest

from a6api_auto.errors import A6AutoError
from a6api_auto.models import MerchantTemplate, SessionRecord
from a6api_auto.storage import SessionStore
from a6api_auto.workflow import Workflow, allocate_session


class FakeMailbox:
    def snapshot(self, email):
        assert email.endswith("@qq.com")
        return {"old"}

    def poll_code(self, email, seen, **_kwargs):
        assert email.endswith("@qq.com")
        assert seen == {"old"}
        return "654321"


class RetryMailbox(FakeMailbox):
    def poll_code(self, email, seen, **_kwargs):
        assert email.endswith("@qq.com")
        assert seen == set()
        return "3d7925"


class FakeClient:
    def __init__(self) -> None:
        self.cookies = {}
        self.calls = []
        self.self_result = None

    def import_cookies(self, cookies):
        self.cookies.update(cookies)

    def export_cookies(self):
        return dict(self.cookies)

    def self_user(self, _user_id=None):
        return self.self_result

    def send_verification(self, email):
        self.calls.append(("send", email))

    def register(self, username, email, password, code):
        self.calls.append(("register", username, email, password, code))

    def login(self, username, password):
        self.calls.append(("login", username, password))
        self.cookies["session"] = "fixture-cookie"
        return {"id": 9517, "username": username}

    def ensure_one_key(self, user_id, token_name):
        self.calls.append(("key", user_id, token_name))
        return "sk-a6-key"

    def fetch_models_via_a6(self, *_args):
        raise AssertionError("direct model fetcher should win")

    def apply_supplier(self, user_id, payload):
        self.calls.append(("apply", user_id, payload))
        return {"supplier_id": 298, "request_id": 1066}

    def supplier_requests(self, user_id):
        self.calls.append(("requests", user_id))
        return [{"id": 1066, "status": 0}]


class LostRegisterResponseClient(FakeClient):
    def register(self, username, email, password, code):
        self.calls.append(("register-lost", username, email, password, code))
        self.cookies["session"] = "recovered-cookie"
        raise A6AutoError("registration_rejected")

    def login(self, username, password):
        self.calls.append(("login", username, password))
        return {"id": 9517, "username": username}


class ExistingSupplierRequestClient(FakeClient):
    def apply_supplier(self, *_args):
        raise AssertionError("existing request must be reconciled without a duplicate apply")

    def supplier_requests(self, _user_id):
        return [
            {
                "id": 1066,
                "supplier_id": 298,
                "name": "星桥001",
                "base_url": "https://upstream.example/v1",
                "status": 0,
            }
        ]


class TimeoutSendVerificationClient(FakeClient):
    def send_verification(self, email):
        self.calls.append(("send-timeout", email))
        raise A6AutoError("a6_transport_failed")


class ExistingEmailClient(FakeClient):
    def send_verification(self, email):
        self.calls.append(("send-existing", email))
        raise A6AutoError("email_already_registered")

    def register(self, *_args):
        raise AssertionError("an existing account must resume through login")


def template() -> MerchantTemplate:
    return MerchantTemplate(
        template_id="merchant-001",
        supplier_name="杭州星桥教育科技服务中心",
        supplier_description="面向职业培训机构提供课程内容生成与教务问答。",
        channel_name="星桥001",
        channel_nickname="星桥001",
        channel_description="杭州低延迟文本通道001",
    )


def record() -> SessionRecord:
    return SessionRecord(
        schema_version=1,
        origin="https://a6api.com",
        email="fixture@qq.com",
        username="fixture",
        password="generated-password",
        token_name="merchant-random",
        template_id="merchant-001",
    )


def test_allocate_session_generates_a6api_compatible_password(tmp_path: Path) -> None:
    qq_file = tmp_path / "qq.txt"
    qq_file.write_text("fixture@qq.com\n")
    current, _store, _template = allocate_session(qq_file, tmp_path / "state")

    assert len(current.password) == 16
    assert current.password.isalnum()


def test_full_workflow_persists_session_and_reconciles_request(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "state" / "session.json")
    current = record()
    store.save(current)
    client = FakeClient()
    workflow = Workflow(
        client,
        FakeMailbox(),
        store,
        model_fetcher=lambda _url, _key: ["gpt-5.6-sol", "gpt-5.6-terra"],
    )

    result = workflow.execute(
        current,
        template(),
        upstream_base_url="https://upstream.example/v1",
        upstream_api_key="sk-upstream-secret",
    )

    saved = store.load()
    assert result["request_id"] == 1066
    assert saved.user_id == 9517
    assert saved.cookies == {"session": "fixture-cookie"}
    assert saved.a6api_key == "sk-a6-key"
    assert saved.models == ["gpt-5.6-sol", "gpt-5.6-terra"]
    assert saved.upstream_api_key_last4 == "cret"
    assert "sk-upstream-secret" not in store.path.read_text()
    apply_payload = next(call[2] for call in client.calls if call[0] == "apply")
    assert apply_payload["supplier"]["contact_info"] == "fixture@qq.com"
    assert apply_payload["channel"]["models"] == "gpt-5.6-sol,gpt-5.6-terra"


def test_expired_session_relogs_in_and_rewrites_cookie(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "state" / "session.json")
    current = record()
    current.status = "authenticated"
    current.user_id = 9517
    current.cookies = {"session": "expired"}
    store.save(current)
    client = FakeClient()

    result = Workflow(client, FakeMailbox(), store).refresh_session(current)

    assert result["cookie_names"] == ["session"]
    assert store.load().cookies == {"session": "fixture-cookie"}
    assert any(call[0] == "login" for call in client.calls)


def test_expired_session_rejects_different_login_identity_before_persisting(
    tmp_path: Path,
) -> None:
    store = SessionStore(tmp_path / "state" / "session.json")
    current = record()
    current.status = "authenticated"
    current.user_id = 9517
    current.cookies = {"session": "expired"}
    store.save(current)

    class WrongAccountClient(FakeClient):
        def login(self, username, password):
            self.calls.append(("login", username, password))
            self.cookies["session"] = "wrong-account-cookie"
            return {"id": 9999, "username": username}

    with pytest.raises(A6AutoError, match="session_user_identity_mismatch"):
        Workflow(WrongAccountClient(), FakeMailbox(), store).refresh_session(current)

    saved = store.load()
    assert saved.user_id == 9517
    assert saved.cookies == {"session": "expired"}


def test_expired_supplier_session_preserves_submitted_status(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "state" / "session.json")
    current = record()
    current.status = "authenticated"
    current.user_id = 9517
    current.supplier_id = 298
    current.request_id = 1066
    current.cookies = {"session": "expired"}
    store.save(current)

    Workflow(FakeClient(), FakeMailbox(), store).refresh_session(current)

    assert store.load().status == "supplier_submitted"


def test_existing_supplier_profile_restores_submitted_status_without_request_id(
    tmp_path: Path,
) -> None:
    store = SessionStore(tmp_path / "state" / "session.json")
    current = record()
    current.status = "authenticated"
    current.user_id = 9517
    current.supplier_id = 298
    current.request_id = None
    current.cookies = {"session": "expired"}
    store.save(current)

    Workflow(FakeClient(), FakeMailbox(), store).refresh_session(current)

    saved = store.load()
    assert saved.status == "supplier_submitted"
    assert saved.supplier_id == 298
    assert saved.request_id is None


def test_lost_registration_response_recovers_with_saved_credentials(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "state" / "session.json")
    current = record()
    store.save(current)
    client = LostRegisterResponseClient()

    Workflow(client, FakeMailbox(), store).ensure_authenticated(current)

    assert current.user_id == 9517
    assert current.status == "authenticated"
    assert any(call[0] == "register-lost" for call in client.calls)
    assert any(call[0] == "login" for call in client.calls)


def test_existing_supplier_request_is_reconciled_after_crash(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "state" / "session.json")
    current = record()
    current.status = "authenticated"
    current.user_id = 9517
    current.cookies = {"session": "valid"}
    current.a6api_key = "sk-a6-key"
    current.models = ["gpt-5.6-sol"]
    store.save(current)
    client = ExistingSupplierRequestClient()
    client.self_result = {"id": 9517, "username": "fixture"}

    result = Workflow(client, FakeMailbox(), store).execute(
        current,
        template(),
        upstream_base_url="https://upstream.example/v1",
        upstream_api_key="sk-upstream-secret",
    )

    assert result["request_id"] == 1066
    assert store.load().supplier_id == 298


def test_timeout_after_verification_send_still_allows_mailbox_recovery(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "state" / "session.json")
    current = record()
    store.save(current)
    client = TimeoutSendVerificationClient()

    Workflow(client, RetryMailbox(), store).ensure_authenticated(current)

    assert current.user_id == 9517
    assert current.status == "authenticated"
    assert ("send-timeout", "fixture@qq.com") in client.calls


def test_existing_email_resumes_allocated_session_with_saved_credentials(
    tmp_path: Path,
) -> None:
    store = SessionStore(tmp_path / "state" / "session.json")
    current = record()
    store.save(current)
    client = ExistingEmailClient()

    Workflow(client, FakeMailbox(), store).ensure_authenticated(current)

    assert current.user_id == 9517
    assert current.status == "authenticated"
    assert ("send-existing", "fixture@qq.com") in client.calls
    assert any(call[0] == "login" for call in client.calls)
