from __future__ import annotations

import json
from pathlib import Path

import pytest

import a6api_auto.slow_channel_registration as slow_channel_registration
from a6api_auto.errors import A6AutoError
from a6api_auto.models import SessionRecord
from a6api_auto.slow_channel_registration import (
    A6SlowChannelRegistrar,
    SlowChannelConfig,
    SlowChannelRegistrationRunner,
    SlowChannelTarget,
    SlowChannelTemplate,
    channel_model_scope_empty,
    load_channel_templates,
    stable_channel_nickname,
)
from a6api_auto.storage import SessionStore, write_private_json
from a6api_auto.upstream import UpstreamSelection


class Clock:
    def __init__(self) -> None:
        self.value = 1_800_000_000.0
        self.sleeps: list[float] = []

    def now(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.value += seconds


def target(merchant_id: int, user_id: int) -> SlowChannelTarget:
    return SlowChannelTarget(merchant_id, user_id, merchant_id + 200, f"/sessions/{merchant_id}")


def config(tmp_path: Path, **changes) -> SlowChannelConfig:
    values = {
        "state_file": tmp_path / "private" / "slow-channel.json",
        "min_interval_seconds": 1200,
        "max_interval_seconds": 2400,
    }
    values.update(changes)
    return SlowChannelConfig(**values)


def channel_templates() -> tuple[SlowChannelTemplate, ...]:
    return tuple(
        SlowChannelTemplate(
            source_row=index + 2,
            channel_name=f"渠道样本{index:04d}",
            channel_description=f"渠道描述样本{index:04d}",
        )
        for index in range(1500)
    )


def test_channel_nickname_is_stable_unique_and_within_a6_limit() -> None:
    values = [stable_channel_nickname(user_id) for user_id in (1, 11450, 11461, 99999999)]

    assert len(values) == len(set(values))
    assert all(1 <= len(value) <= 10 for value in values)
    assert stable_channel_nickname(11450) == stable_channel_nickname(11450)


@pytest.mark.parametrize(
    "minimum,maximum",
    [(1199, 2400), (1200, 2401), (1800, 1799)],
)
def test_config_rejects_intervals_outside_twenty_to_forty_minutes(
    tmp_path: Path, minimum: float, maximum: float
) -> None:
    with pytest.raises(A6AutoError, match="slow_channel_interval_must_be_20_to_40_minutes"):
        config(
            tmp_path,
            min_interval_seconds=minimum,
            max_interval_seconds=maximum,
        ).validate()


def test_runner_freezes_targets_schedules_serially_and_omits_key(tmp_path: Path) -> None:
    clock = Clock()
    initial = (target(2, 102), target(1, 101))
    templates = channel_templates()
    calls: list[tuple[int, str, str, str]] = []

    def register(
        item: SlowChannelTarget,
        selection: UpstreamSelection,
        template: SlowChannelTemplate,
    ):
        calls.append(
            (
                item.merchant_id,
                selection.api_key,
                template.channel_name,
                template.channel_description,
            )
        )
        return {"action": "submitted", "request_id": item.merchant_id + 1000}

    runner = SlowChannelRegistrationRunner(
        config(tmp_path),
        target_loader=lambda: initial,
        template_loader=lambda: templates,
        selector=lambda pending: UpstreamSelection(
            base_url=(pending or {}).get("base_url", "https://upstream.example/v1"),
            api_key="sk-secret",
            key_ref=(pending or {}).get("key_ref", "17"),
        ),
        registrar=register,
        now=clock.now,
        sleep=clock.sleep,
        random_interval=lambda _low, _high: 1200,
        random_choice=lambda values: values[0],
    )

    result = runner.run()
    state_text = config(tmp_path).state_file.read_text()

    assert result["success"] is True
    assert result["target"] == 2
    assert result["completed"] == 2
    assert result["failed"] == 0
    assert result["attempts"] == 2
    assert result["state_file"] == str(config(tmp_path).state_file.resolve())
    assert calls == [
        (2, "sk-secret", "渠道样本0000", "渠道描述样本0000"),
        (1, "sk-secret", "渠道样本0001", "渠道描述样本0001"),
    ]
    assert clock.sleeps == [1200]
    assert "sk-secret" not in state_text
    assert json.loads(state_text)["targets"] == [
        {
            "a6_user_id": 102,
            "merchant_id": 2,
            "session_ref": "/sessions/2",
            "supplier_id": 202,
        },
        {
            "a6_user_id": 101,
            "merchant_id": 1,
            "session_ref": "/sessions/1",
            "supplier_id": 201,
        },
    ]


def test_runner_retries_then_continues_to_next_merchant(tmp_path: Path) -> None:
    clock = Clock()
    attempts: dict[int, int] = {}
    attempt_templates: list[tuple[int, int, str]] = []

    def register(
        item: SlowChannelTarget,
        selection: UpstreamSelection,
        template: SlowChannelTemplate,
    ):
        attempts[item.merchant_id] = attempts.get(item.merchant_id, 0) + 1
        attempt_templates.append((item.merchant_id, template.source_row, selection.key_ref))
        if item.merchant_id == 1:
            raise A6AutoError("fixture_failure")
        return {"action": "already_exists"}

    runner = SlowChannelRegistrationRunner(
        config(tmp_path),
        target_loader=lambda: (target(1, 101), target(2, 102)),
        template_loader=channel_templates,
        selector=lambda pending: UpstreamSelection(
            "https://upstream.example/v1", "sk-secret", "7"
        ),
        registrar=register,
        now=clock.now,
        sleep=clock.sleep,
        random_interval=lambda _low, _high: 1200,
        random_choice=lambda values: values[0],
    )

    result = runner.run()

    assert result["completed"] == 1
    assert result["failed"] == 1
    assert attempts == {1: 2, 2: 1}
    assert attempt_templates == [(1, 2, "7"), (1, 2, "7"), (2, 3, "7")]
    assert clock.sleeps == [1200, 1200]


def test_packaged_workbook_is_the_strict_channel_template_catalog() -> None:
    templates = load_channel_templates()

    assert len(templates) == 1500
    assert templates[0].channel_name == "一半热度"
    assert templates[-1].channel_name == "一纸闲话"
    assert len({item.channel_name for item in templates}) == 1500
    assert len({item.channel_description for item in templates}) == 1500
    assert all(item.channel_name != "正常API中转站" for item in templates)


@pytest.mark.parametrize("channel_name", ("正常API中转站", "eqgb5qrv35", "expdpxroye"))
def test_forbidden_channel_name_is_rejected_as_a_template(channel_name: str) -> None:
    with pytest.raises(A6AutoError, match="slow_channel_template_name_invalid"):
        SlowChannelTemplate(2, channel_name, "不应被使用")


class FakeClient:
    def __init__(self) -> None:
        self.cookies: dict[str, str] = {}
        self.requests: list[dict[str, object]] = []
        self.applied: list[dict[str, object]] = []
        self.channels: list[dict[str, object]] = []
        self.listings: list[dict[str, object]] = []
        self.updated_channel_ids: list[int] = []
        self.taken_down_listing_ids: list[int] = []
        self.taken_down_channel_ids: list[int] = []

    def import_cookies(self, cookies):
        self.cookies.update(cookies)

    def export_cookies(self):
        return {"session": "fresh"}

    def self_user(self, user_id):
        return {"id": user_id, "username": "fixture"}

    def login(self, _username, _password):
        raise AssertionError("valid cookie should avoid login")

    def supplier_me(self, user_id):
        return {
            "id": 301,
            "user_id": user_id,
            "name": "FixtureAPI",
            "contact_info": "fixture@example.invalid",
            "description": "Fixture supplier",
        }

    def supplier_channels(self, _user_id):
        return [dict(item) for item in self.channels]

    def supplier_requests(self, _user_id):
        return list(self.requests)

    def fetch_models_via_a6(self, *_args):
        raise AssertionError("direct fetch should win")

    def supplier_listings(self, _user_id, *, page, size):
        start = (page - 1) * size
        return {
            "items": [dict(item) for item in self.listings[start : start + size]],
            "total": len(self.listings),
            "page": page,
            "size": size,
        }

    def takedown_supplier_listing(self, _user_id, listing_id):
        self.taken_down_listing_ids.append(listing_id)
        next(item for item in self.listings if item["id"] == listing_id)["status"] = 0
        return {}

    def update_supplier_channel(self, _user_id, channel_id, payload):
        self.updated_channel_ids.append(channel_id)
        channel = next(
            item
            for item in self.channels
            if item["id"] == channel_id or item.get("binding_id") == channel_id
        )
        channel.update(dict(payload))
        return {"id": channel_id}

    def takedown_supplier_channel(self, _user_id, channel_id):
        self.taken_down_channel_ids.append(channel_id)
        channel = next(
            item
            for item in self.channels
            if item["id"] == channel_id or item.get("binding_id") == channel_id
        )
        channel["status"] = 2
        return {}

    def apply_supplier(self, user_id, payload):
        self.applied.append(dict(payload))
        request = {
            "id": 801,
            "supplier_id": 301,
            "name": payload["channel"]["name"],
            "nickname": payload["channel"]["nickname"],
            "status": 0,
        }
        self.requests.append(request)
        return {"request_id": 801, "supplier_id": 301}


def test_registrar_refreshes_submits_and_reads_back_without_storing_upstream_key(
    tmp_path: Path,
) -> None:
    sessions = tmp_path / "sessions"
    session_path = sessions / "session.json"
    store = SessionStore(session_path)
    store.save(
        SessionRecord(
            schema_version=1,
            origin="https://a6.fixture.invalid",
            email="fixture@example.invalid",
            username="fixture",
            password="password",
            user_id=101,
            cookies={"session": "old"},
            status="supplier_submitted",
            supplier_id=301,
            request_id=701,
        )
    )
    proxy_map = tmp_path / "proxy-map.json"
    write_private_json(proxy_map, {"101": "socks5://proxy.invalid:1080"})
    client = FakeClient()
    registrar = A6SlowChannelRegistrar(
        sessions_dir=sessions,
        proxy_map_file=proxy_map,
        client_factory=lambda _record, _proxy: client,
        model_fetcher=lambda _url, _key: ["gpt-5.6-sol"],
        readback_sleep=lambda _seconds: None,
    )

    result = registrar(
        SlowChannelTarget(1, 101, 301, str(session_path)),
        UpstreamSelection("https://upstream.example/v1", "sk-upstream-secret", "9"),
        SlowChannelTemplate(23, "一半热度", "日常模型调用配额，按公开规则提供。"),
    )

    assert result["action"] == "submitted"
    assert result["request_id"] == 801
    assert client.applied[0]["channel"]["name"] == "一半热度"
    assert client.applied[0]["channel"]["description"] == "日常模型调用配额，按公开规则提供。"
    assert client.applied[0]["channel"]["nickname"] == stable_channel_nickname(
        101, "一半热度"
    )
    assert SessionStore(session_path).load().status == "supplier_submitted"
    assert "sk-upstream-secret" not in session_path.read_text()


def test_cleanup_takes_down_all_forbidden_listings_and_clears_models(tmp_path: Path) -> None:
    sessions = tmp_path / "sessions"
    session_path = sessions / "session.json"
    SessionStore(session_path).save(
        SessionRecord(
            schema_version=1,
            origin="https://a6.fixture.invalid",
            email="fixture@example.invalid",
            username="fixture",
            password="password",
            user_id=101,
            cookies={"session": "old"},
            status="supplier_submitted",
            supplier_id=301,
            request_id=701,
        )
    )
    proxy_map = tmp_path / "proxy-map.json"
    write_private_json(proxy_map, {"101": "socks5://proxy.invalid:1080"})
    client = FakeClient()
    client.channels = [
        {
            "id": 20,
            "binding_id": 120,
            "name": "正常API中转站",
            "models": "gpt-5.6-sol",
            "status": 1,
        },
        {
            "id": 21,
            "binding_id": 121,
            "name": "正常API中转站",
            "models": "gpt-5.5,gpt-5.6-sol",
            "status": 2,
        },
        {
            "id": 22,
            "binding_id": 122,
            "name": "保留渠道",
            "models": "gpt-5.6-sol",
            "status": 1,
        },
    ]
    client.listings = [
        {"id": 501, "channel_id": 20, "status": 2},
        {"id": 502, "channel_id": 21, "status": 0},
        {"id": 503, "channel_id": 22, "status": 2},
    ]
    registrar = A6SlowChannelRegistrar(
        sessions_dir=sessions,
        proxy_map_file=proxy_map,
        client_factory=lambda _record, _proxy: client,
        readback_sleep=lambda _seconds: None,
    )

    result = registrar.cleanup_forbidden(
        SlowChannelTarget(1, 101, 301, str(session_path))
    )

    assert result == {"merchant_id": 1, "channel_count": 2, "listing_count": 1}
    assert client.taken_down_listing_ids == [501]
    assert client.taken_down_channel_ids == [120]
    assert client.updated_channel_ids == [120, 121]
    assert channel_model_scope_empty(client.channels[0]["models"])
    assert channel_model_scope_empty(client.channels[1]["models"])
    assert not channel_model_scope_empty(client.channels[2]["models"])
    assert [item["status"] for item in client.channels] == [2, 2, 1]
    assert [item["status"] for item in client.listings] == [0, 0, 2]


def test_cleanup_failure_stops_before_managed_bindings_are_retired(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    retired: list[str] = []
    monkeypatch.setenv("A6_TRADE_DATABASE_DSN", "postgresql://fixture")
    monkeypatch.setattr(slow_channel_registration, "load_postgres_targets", lambda _dsn: ())
    monkeypatch.setattr(
        slow_channel_registration,
        "load_postgres_cleanup_targets",
        lambda _dsn: (target(1, 101),),
    )
    monkeypatch.setattr(
        slow_channel_registration,
        "A6SlowChannelRegistrar",
        lambda **_kwargs: object(),
    )
    monkeypatch.setattr(
        slow_channel_registration,
        "cleanup_forbidden_targets",
        lambda _registrar, _targets, *, event: {
            "target_count": 1,
            "cleaned_channel_count": 0,
            "cleaned_listing_count": 0,
            "failed": 1,
        },
    )
    monkeypatch.setattr(
        slow_channel_registration,
        "retire_forbidden_managed_bindings",
        lambda dsn: retired.append(dsn),
    )

    exit_code = slow_channel_registration.main(
        [
            "--live",
            "--cleanup-forbidden-only",
            "--sessions-dir",
            str(tmp_path / "sessions"),
            "--proxy-map-file",
            str(tmp_path / "proxy-map.json"),
            "--state-file",
            str(tmp_path / "state.json"),
            "--enodot-admin-key-file",
            str(tmp_path / "admin.secret"),
        ]
    )

    assert exit_code == 1
    assert retired == []
