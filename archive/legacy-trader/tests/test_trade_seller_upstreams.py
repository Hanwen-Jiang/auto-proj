from __future__ import annotations

import hashlib
from decimal import Decimal
from types import SimpleNamespace

from a6api_auto.models import MerchantTemplate, SessionRecord
from a6api_auto.trade_enodot_seller_resources import ManagedSellerBinding
from a6api_auto.trade_managed_listings import (
    ManagedChannelBinding,
    ManagedDecision,
    ManagedListingBinding,
    managed_marker,
    stable_managed_uid,
)
from a6api_auto.trade_seller_upstreams import (
    SellerUpstreamBatchRequest,
    SellerUpstreamReconciler,
)

API_KEY = "sk-independent-enodot-seller-key-ABCD"
BASE_URL = "https://ai.enodot.shop/v1"
CHANNEL_UID = stable_managed_uid("channel", 5, "seller-primary")
LISTING_UID = stable_managed_uid(
    "listing", 5, f"gpt-5.6-sol:{managed_marker(CHANNEL_UID)}"
)


def _context():
    return SimpleNamespace(
        merchant_id=5,
        a6_user_id=9517,
        supplier_id=298,
        merchant_status="supplier_submitted",
        roles=("seller", "observer"),
        session_ref="/private/session-5.json",
    )


def _session() -> SessionRecord:
    return SessionRecord(
        schema_version=1,
        origin="https://a6.fixture.invalid",
        email="private@example.invalid",
        username="private-5",
        password="password-secret",
        user_id=9517,
        cookies={"session": "cookie-secret"},
        status="supplier_submitted",
        supplier_id=298,
        template_id="merchant-005",
    )


def _template() -> MerchantTemplate:
    return MerchantTemplate(
        template_id="merchant-005",
        supplier_name="Fixture Supplier",
        supplier_description="Fixture supplier description",
        channel_name="Fixture Channel",
        channel_nickname="Fixture",
        channel_description="Fixture channel description",
    )


class SellerStore:
    def load_context(self, merchant_id):
        assert merchant_id == 5
        return _context()

    def get_binding(self, merchant_id):
        assert merchant_id == 5
        return ManagedSellerBinding(
            binding_id=1,
            merchant_id=5,
            managed_uid="a6trader:seller:merchant-5",
            group_id=26,
            desired_delta=Decimal("0.5"),
            control_state="bound",
            enodot_user_id=51,
            enodot_api_key_id=61,
            api_key_fingerprint=hashlib.sha256(API_KEY.encode()).hexdigest(),
            key_secret_ref="fixture://seller-5",
        )


class SecretSink:
    def read_api_key(self, managed_uid, secret_ref):
        assert (managed_uid, secret_ref) == (
            "a6trader:seller:merchant-5",
            "fixture://seller-5",
        )
        return API_KEY


class ListingStore:
    def __init__(self):
        self.channel = ManagedChannelBinding(
            binding_id=10,
            managed_uid=CHANNEL_UID,
            merchant_id=5,
            create_idempotency_key="channel-create",
            control_state="verified",
            desired_state={"channel_strategy": "adopt_existing"},
            a6_channel_id=700,
        )
        self.listing = ManagedListingBinding(
            binding_id=11,
            managed_uid=LISTING_UID,
            merchant_id=5,
            channel_binding_id=10,
            create_idempotency_key="listing-create",
            control_state="verified",
            desired_state={"listing_strategy": "adopt_existing"},
            a6_channel_id=700,
            a6_listing_id=900,
        )

    def get_channel(self, managed_uid):
        assert managed_uid == CHANNEL_UID
        return self.channel

    def get_listing(self, managed_uid):
        assert managed_uid == LISTING_UID
        return self.listing


class Client:
    def __init__(self, *, models=("gpt-5.6-sol",)):
        self.models = list(models)
        self.model_probes = []
        self.channels = [
            {
                "id": 700,
                "binding_id": 1700,
                "base_url": "https://placeholder.invalid/v1",
                "api_key_last4": "xxxx",
                "has_api_key": True,
            }
        ]
        self.listings = [
            {
                "id": 900,
                "channel_id": 700,
                "status": 2,
                "charge_type": "official_price_ratio",
                "input_price_micros": 175_000,
                "output_price_micros": 1_050_000,
                "cache_read_price_micros": 17_500,
                "cache_write_price_micros": 218_750,
                "display_name": "gpt-5.6-sol",
                "description": "Team满血池",
                "own_model": "gpt-5.6-sol",
                "platform_model": "gpt-5.6-sol",
            }
        ]

    def supplier_channels(self, user_id):
        assert user_id == 9517
        return self.channels

    def supplier_listings(self, user_id, **_kwargs):
        assert user_id == 9517
        return {"items": self.listings, "total": len(self.listings)}

    def fetch_models_via_a6(self, user_id, base_url, api_key):
        assert user_id == 9517
        self.model_probes.append((base_url, api_key))
        return self.models


class Controller:
    def __init__(self, client: Client):
        self.client = client
        self.calls = 0

    def ensure_channel_upstream(self, merchant, managed_uid, spec, *, model, apply=False):
        assert (merchant.merchant_id, managed_uid, model) == (5, CHANNEL_UID, "gpt-5.6-sol")
        self.calls += 1
        remote = self.client.channels[0]
        matches = (
            remote["base_url"] == spec.base_url
            and remote["api_key_last4"] == spec.api_key[-4:]
            and remote["has_api_key"] is True
        )
        if apply and not matches:
            remote.update(
                {
                    "base_url": spec.base_url,
                    "api_key_last4": spec.api_key[-4:],
                    "has_api_key": True,
                }
            )
        return ManagedDecision(
            merchant_id=5,
            resource_kind="channel",
            managed_uid=managed_uid,
            action="noop" if matches else "update",
            state="verified" if matches or apply else "planned",
            applied=apply and not matches,
            external_id=700,
        )


def _reconciler(client: Client):
    listing_store = ListingStore()
    controller = Controller(client)
    reconciler = SellerUpstreamReconciler(
        SellerStore(),
        SecretSink(),
        listing_store,
        lambda _context, _session: client,
        session_loader=lambda _path: _session(),
        template_loader=lambda _template_id: _template(),
        listing_controller_factory=lambda _store, _client: controller,
    )
    return reconciler, controller


def test_reconciler_repairs_bound_upstream_and_preserves_listing() -> None:
    client = Client()
    reconciler, controller = _reconciler(client)

    result = reconciler.execute(
        SellerUpstreamBatchRequest(merchant_ids=(5,)),
        apply=True,
    ).summary()

    assert result["success"] is True
    assert result["verified_count"] == 1
    assert result["repaired_count"] == 1
    merchant = result["merchants"][0]
    assert merchant["a6_channel_id"] == 700
    assert merchant["a6_channel_binding_id"] == 1700
    assert merchant["base_url_compliant"] is True
    assert merchant["api_key_compliant"] is True
    assert merchant["api_key_last4"] == "ABCD"
    assert merchant["model_verified"] is True
    assert merchant["listing"]["a6_listing_id"] == 900
    assert merchant["listing"]["status_before"] == 2
    assert merchant["listing"]["status_after"] == 2
    assert merchant["listing"]["prices_before"] == merchant["listing"]["prices_after"]
    assert merchant["listing"]["unchanged"] is True
    assert client.model_probes == [(BASE_URL, API_KEY)]
    assert controller.calls == 1
    output = repr(result)
    assert API_KEY not in output
    assert BASE_URL not in output


def test_reconciler_probes_models_even_when_remote_fields_already_match() -> None:
    client = Client()
    client.channels[0].update(
        {
            "base_url": BASE_URL,
            "api_key_last4": API_KEY[-4:],
            "has_api_key": True,
        }
    )
    reconciler, _controller = _reconciler(client)

    result = reconciler.execute(SellerUpstreamBatchRequest(merchant_ids=(5,)), apply=True)

    assert result.merchants[0].action == "noop"
    assert result.merchants[0].verified is True
    assert client.model_probes == [(BASE_URL, API_KEY)]


def test_reconciler_stops_before_channel_mutation_when_model_is_missing() -> None:
    client = Client(models=("some-other-model",))
    reconciler, controller = _reconciler(client)

    result = reconciler.execute(SellerUpstreamBatchRequest(merchant_ids=(5,)), apply=True)

    assert result.summary()["success"] is False
    assert result.merchants[0].error_class == "managed_channel_upstream_model_missing"
    assert controller.calls == 0
    assert client.channels[0]["base_url"] == "https://placeholder.invalid/v1"


def test_listing_change_is_reported_as_verification_failure() -> None:
    client = Client()
    reconciler, controller = _reconciler(client)
    original = controller.ensure_channel_upstream

    def mutate_listing(*args, **kwargs):
        decision = original(*args, **kwargs)
        client.listings[0] = {**client.listings[0], "input_price_micros": 999}
        return decision

    controller.ensure_channel_upstream = mutate_listing

    result = reconciler.execute(SellerUpstreamBatchRequest(merchant_ids=(5,)), apply=True)

    assert result.summary()["success"] is False
    assert result.merchants[0].error_class == "seller_upstream_verification_failed"
    assert result.merchants[0].listing["unchanged"] is False
