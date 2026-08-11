from __future__ import annotations

import inspect
import stat
from datetime import datetime, timezone
from pathlib import Path

from a6api_auto.trade_liquidity_token import (
    LiquidityTokenContext,
    LiquidityTokenIdentity,
    LiquidityTokenReconciler,
    OwnedLiquidityReceiver,
    PostgresLiquidityTokenStore,
)

NOW = datetime(2026, 8, 4, tzinfo=timezone.utc)


def context(*, existing: bool = False, token_name: str = "Codex"):
    return LiquidityTokenContext(
        merchant_id=12,
        a6_user_id=1200,
        session_ref="/sessions/merchant-12.json",
        model="gpt-5.6-sol",
        token_ref_id=88 if existing else None,
        external_token_id=900 if existing else None,
        token_name=token_name if existing else None,
        key_secret_ref="/secrets/old.secret" if existing else None,
    )


def receiver() -> OwnedLiquidityReceiver:
    return OwnedLiquidityReceiver(
        merchant_id=1,
        supplier_id=101,
        listing_id=501,
        channel_id=401,
        model="gpt-5.6-sol",
        last_success_at=NOW,
    )


class FakeStore:
    def __init__(self, sender, target) -> None:
        self.sender = sender
        self.target = target
        self.registrations = []
        self.routes = []

    def load_context(self, **kwargs):
        assert kwargs["a6_user_id"] == 1200
        return self.sender

    def register(self, sender, **kwargs):
        self.registrations.append((sender, kwargs))
        return LiquidityTokenIdentity(
            token_ref_id=88,
            merchant_id=sender.merchant_id,
            a6_user_id=sender.a6_user_id,
            external_token_id=kwargs["external_token_id"],
            token_name=kwargs["token_name"],
            key_fingerprint=kwargs["key_fingerprint"],
            key_secret_ref=kwargs["key_secret_ref"],
            model=sender.model,
        )

    def select_receiver(self, identity, **kwargs):
        assert kwargs["receiver_label"] == "小微商户"
        return self.target

    def record_route(self, identity, target, **kwargs):
        self.routes.append((identity, target, kwargs))


class FakeRemote:
    def __init__(self, tokens=None, channel=None) -> None:
        self.tokens = list(tokens or [])
        self.channel = channel
        self.ensure_calls = []
        self.rename_calls = []
        self.pin_calls = []
        self.pins = []

    def list_tokens(self, user_id):
        assert user_id == 1200
        return [dict(item) for item in self.tokens]

    def ensure_one_key(self, user_id, token_name):
        self.ensure_calls.append((user_id, token_name))
        if not any(item["name"] == token_name for item in self.tokens):
            self.tokens.append({"id": 901, "name": token_name})
        return "sk-liquidity-fixture"

    def rename_token(self, user_id, token_id, new_name):
        self.rename_calls.append((user_id, token_id, new_name))
        for item in self.tokens:
            if item["id"] == token_id:
                item["name"] = new_name
        return {"id": token_id, "name": new_name}

    def marketplace_channels(self, user_id, **kwargs):
        assert kwargs["include_user_disabled"] is True
        items = [] if self.channel is None else [dict(self.channel)]
        return {"items": items, "total": len(items)}

    def marketplace_pin(self, user_id, **kwargs):
        self.pin_calls.append((user_id, kwargs))
        self.pins = [
            {
                "token_id": kwargs["token_id"],
                "channel_id": kwargs["channel_id"],
                "model_name": kwargs["model"],
            }
        ]
        return {}

    def marketplace_pins(self, user_id):
        return [dict(item) for item in self.pins]


def remote_channel(**overrides):
    value = {
        "channel_id": 401,
        "listing_id": 501,
        "listing_availability": 1,
        "supplier_channel_disabled": False,
        "user_channel_disabled": False,
        "route_status": "available",
    }
    value.update(overrides)
    return value


def reconcile(tmp_path: Path, store: FakeStore, remote: FakeRemote, name="Codex"):
    return LiquidityTokenReconciler(
        store,
        remote,
        secret_dir=tmp_path / "liquidity",
        name_generator=lambda: name,
    ).reconcile(
        a6_user_id=1200,
        session_ref="/sessions/merchant-12.json",
        model="gpt-5.6-sol",
    )


def test_new_liquidity_token_uses_anonymous_name_and_private_secret(tmp_path: Path) -> None:
    store = FakeStore(context(), receiver())
    remote = FakeRemote(channel=remote_channel())

    result = reconcile(tmp_path, store, remote)

    assert result["status"] == "ready"
    assert result["token_name"] == "Codex"
    assert result["token_duty"] == "liquidity"
    assert len(remote.ensure_calls) == 1
    assert len(remote.pin_calls) == 1
    secret = Path(result["key_secret_ref"])
    assert secret.read_text().strip() == "sk-liquidity-fixture"
    assert stat.S_IMODE(secret.stat().st_mode) == 0o600
    assert store.routes[0][2]["route_state"] == "ready"
    assert store.routes[0][2]["pin_confirmed"] is True


def test_legacy_callback_name_is_renamed_without_changing_token_identity(
    tmp_path: Path,
) -> None:
    store = FakeStore(context(existing=True, token_name="callback"), None)
    remote = FakeRemote(tokens=[{"id": 900, "name": "callback"}])

    result = reconcile(tmp_path, store, remote, name="OpenAI")

    assert result["status"] == "skipped_no_owned_receiver_listing"
    assert result["external_token_id"] == 900
    assert result["token_name"] == "OpenAI"
    assert remote.rename_calls == [(1200, 900, "OpenAI")]
    assert remote.pin_calls == []
    assert store.routes[0][2]["route_state"] == "unassigned"


def test_no_owned_receiver_listing_performs_zero_pin(tmp_path: Path) -> None:
    store = FakeStore(context(), None)
    remote = FakeRemote()

    result = reconcile(tmp_path, store, remote)

    assert result["status"] == "skipped_no_owned_receiver_listing"
    assert remote.pin_calls == []


def test_remote_disabled_receiver_is_not_pinned(tmp_path: Path) -> None:
    store = FakeStore(context(), receiver())
    remote = FakeRemote(channel=remote_channel(user_channel_disabled=True))

    result = reconcile(tmp_path, store, remote)

    assert result["status"] == "skipped_no_owned_receiver_listing"
    assert remote.pin_calls == []
    assert store.routes[0][2]["route_state"] == "blocked"


def test_liquidity_store_persists_duty_and_requires_callable_owned_listing() -> None:
    source = inspect.getsource(PostgresLiquidityTokenStore)

    assert "a6_token_duties" in source
    assert "duty = 'liquidity'" in source
    assert "key_secret_ref" in source
    assert "listing.status = 2" in source
    assert "listing.availability = 1" in source
    assert "listing.visibility = 1" in source
    assert "signal.is_owned" in source
    assert "signal.health_state = 'healthy'" in source


def test_callback_token_script_has_no_hardcoded_callback_name() -> None:
    script = (
        Path(__file__).resolve().parents[1] / "scripts/ensure_callback_token.py"
    ).read_text()

    assert 'ensure_one_key(int(record.user_id), "callback")' not in script
    assert "LiquidityTokenReconciler" in script
    assert "liquidity-tokens" in script
