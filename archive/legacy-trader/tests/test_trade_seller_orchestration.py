from __future__ import annotations

import hashlib
import threading
import time
from copy import deepcopy
from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

from a6api_auto.errors import A6AutoError
from a6api_auto.models import MerchantTemplate, SessionRecord
from a6api_auto.trade_enodot_seller_resources import (
    ManagedSellerBinding,
    ManagedSellerContext,
    ManagedSellerError,
    ManagedSellerResult,
)
from a6api_auto.trade_managed_listings import (
    ChannelAdoptionIntent,
    ChannelCreateIntent,
    ListingAdoptionIntent,
    ListingCreateIntent,
    ManagedAction,
    ManagedChannelBinding,
    ManagedDecision,
    ManagedListingBinding,
    ManagedListingError,
    managed_channel_name,
    managed_marker,
    managed_resource_signature,
    stable_managed_uid,
)
from a6api_auto.trade_seller_orchestration import (
    GPT_5_6_SOL_OFFICIAL_PRICES,
    MODEL_OFFICIAL_PRICES,
    SellerMerchantResourceResult,
    SellerOrchestrationError,
    SellerPricing,
    SellerResourceBatchRequest,
    SellerResourceOrchestrator,
)

API_KEY = "sk-managed-seller-orchestration-secret"
UID = "a6trader:seller:merchant-5"


def pricing(**overrides: Any) -> SellerPricing:
    values = {"cost_multiplier": "0.50", "delta": "0.50"}
    values.update(overrides)
    return SellerPricing(**values)


def request(**overrides: Any) -> SellerResourceBatchRequest:
    values = {
        "merchant_ids": (5,),
        "group_id": 26,
        "model": "gpt-5.6-sol",
        "pricing": pricing(),
        "listing_description": "Team 订阅来源",
    }
    values.update(overrides)
    return SellerResourceBatchRequest(**values)


def context(roles: tuple[str, ...] = ("seller", "observer")) -> ManagedSellerContext:
    return ManagedSellerContext(
        merchant_id=5,
        a6_user_id=9517,
        supplier_id=298,
        merchant_status="supplier_submitted",
        roles=roles,
        session_ref="/private/session-5.json",
    )


def session() -> SessionRecord:
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


def template() -> MerchantTemplate:
    return MerchantTemplate(
        template_id="merchant-005",
        supplier_name="Fixture Supplier",
        supplier_description="Fixture supplier description",
        channel_name="Fixture Channel",
        channel_nickname="Fixture Nickname",
        channel_description="Fixture channel description",
    )


def binding() -> ManagedSellerBinding:
    return ManagedSellerBinding(
        binding_id=1,
        merchant_id=5,
        managed_uid=UID,
        group_id=26,
        desired_delta=Decimal("0.500000"),
        control_state="bound",
        enodot_user_id=51,
        enodot_api_key_id=61,
        api_key_fingerprint=hashlib.sha256(API_KEY.encode()).hexdigest(),
        key_secret_ref="fixture://seller-5",
    )


class SellerStore:
    def __init__(self, seller_context: ManagedSellerContext | None = None) -> None:
        self.context = seller_context or context()
        self.binding: ManagedSellerBinding | None = None

    def load_context(self, merchant_id: int):
        assert merchant_id == 5
        return self.context

    def get_binding(self, merchant_id: int):
        assert merchant_id == 5
        return self.binding


class SellerService:
    def __init__(self, store: SellerStore, events: list[str]) -> None:
        self.store = store
        self.events = events
        self.verified = False
        self.capability = object()

    def preflight_contract(self):
        self.events.append("seller.capability")
        return self.capability

    def execute(self, desired, *, apply: bool = False, capability=None):
        self.events.append("seller.execute")
        assert capability is self.capability
        if apply:
            self.store.binding = binding()
            self.verified = True
        return ManagedSellerResult(
            mode="applied" if apply else "dry-run",
            merchant_id=5,
            managed_uid=UID,
            action="create" if not self.verified else "noop",
            applied=apply,
            verified=self.verified,
            state="bound" if self.verified else "planned",
            enodot_user_id=51 if self.verified else None,
            enodot_api_key_id=61 if self.verified else None,
            delta=Decimal("0.500000"),
        )


class SecretSink:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    def read_api_key(self, managed_uid: str, secret_ref: str):
        assert (managed_uid, secret_ref) == (UID, "fixture://seller-5")
        self.events.append("secret.read")
        return API_KEY


class ListingStore:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.channel: ManagedChannelBinding | None = None
        self.listing: ManagedListingBinding | None = None
        self.actions: dict[tuple[str, str], ManagedAction] = {}

    def get_channel(self, uid: str):
        self.events.append("store.get_channel")
        return self.channel if self.channel and self.channel.managed_uid == uid else None

    def reserve_channel(self, intent: ChannelCreateIntent):
        self.events.append("store.reserve_channel")
        self.channel = ManagedChannelBinding(
            binding_id=10,
            managed_uid=intent.managed_uid,
            merchant_id=intent.merchant_id,
            create_idempotency_key=intent.create_idempotency_key,
            control_state="planned",
            desired_state=deepcopy(dict(intent.desired_state)),
        )
        return self.channel

    def adopt_channel(self, intent: ChannelAdoptionIntent):
        self.events.append("store.adopt_channel")
        self.channel = ManagedChannelBinding(
            binding_id=10,
            managed_uid=intent.managed_uid,
            merchant_id=intent.merchant_id,
            create_idempotency_key=intent.idempotency_key,
            control_state="verified",
            desired_state=deepcopy(dict(intent.desired_state)),
            observed_state=deepcopy(dict(intent.observation)),
            a6_channel_id=intent.a6_channel_id,
            create_action_id=100,
        )
        return self.channel

    def claim_channel_create(self, value):
        self.events.append("store.claim_channel")
        self.channel = replace(value, control_state="creating", create_action_id=100)
        return self.channel

    def record_channel_post(self, value, observation):
        self.events.append("store.record_channel")
        self.channel = replace(
            value,
            control_state="verifying",
            observed_state=deepcopy(dict(observation)),
        )
        return self.channel

    def mark_channel_create_unknown(self, value, error_class):
        self.channel = replace(value, control_state="create_unknown", last_error_class=error_class)
        return self.channel

    def verify_channel(self, value, channel_id, observation):
        self.events.append("store.verify_channel")
        self.channel = replace(
            value,
            control_state="verified",
            a6_channel_id=channel_id,
            observed_state=deepcopy(dict(observation)),
        )
        return self.channel

    def get_listing(self, uid: str):
        self.events.append("store.get_listing")
        return self.listing if self.listing and self.listing.managed_uid == uid else None

    def reserve_listing(self, intent: ListingCreateIntent):
        self.events.append("store.reserve_listing")
        self.listing = ManagedListingBinding(
            binding_id=11,
            managed_uid=intent.managed_uid,
            merchant_id=intent.merchant_id,
            channel_binding_id=intent.channel_binding_id,
            create_idempotency_key=intent.create_idempotency_key,
            control_state="planned",
            desired_state=deepcopy(dict(intent.desired_state)),
            a6_channel_id=intent.a6_channel_id,
        )
        return self.listing

    def adopt_listing(self, intent: ListingAdoptionIntent):
        self.events.append("store.adopt_listing")
        self.listing = ManagedListingBinding(
            binding_id=11,
            managed_uid=intent.managed_uid,
            merchant_id=intent.merchant_id,
            channel_binding_id=intent.channel_binding_id,
            create_idempotency_key=intent.idempotency_key,
            control_state="verified",
            desired_state=deepcopy(dict(intent.desired_state)),
            observed_state=deepcopy(dict(intent.observation)),
            a6_channel_id=intent.a6_channel_id,
            a6_listing_id=intent.a6_listing_id,
            create_action_id=101,
        )
        return self.listing

    def claim_listing_create(self, value):
        self.events.append("store.claim_listing")
        self.listing = replace(value, control_state="creating", create_action_id=101)
        return self.listing

    def record_listing_post(self, value, observation):
        self.events.append("store.record_listing")
        self.listing = replace(
            value,
            control_state="verifying",
            observed_state=deepcopy(dict(observation)),
        )
        return self.listing

    def mark_listing_create_unknown(self, value, error_class):
        self.listing = replace(value, control_state="create_unknown", last_error_class=error_class)
        return self.listing

    def verify_listing(self, value, listing_id, observation):
        self.events.append("store.verify_listing")
        self.listing = replace(
            value,
            control_state="verified",
            a6_listing_id=listing_id,
            observed_state=deepcopy(dict(observation)),
        )
        return self.listing

    def get_pending_action(self, resource_kind: str, uid: str):
        return self.actions.get((resource_kind, uid))

    def begin_action(self, *args, **kwargs):
        raise AssertionError("unexpected update action")


class A6Client:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.channels: list[dict[str, Any]] = []
        self.requests: list[dict[str, Any]] = []
        self.listings: list[dict[str, Any]] = []

    def supplier_channels(self, user_id: int):
        self.events.append("a6.channels")
        return deepcopy(self.channels)

    def supplier_requests(self, user_id: int):
        self.events.append("a6.requests")
        return deepcopy(self.requests)

    def supplier_me(self, user_id: int):
        self.events.append("a6.supplier")
        return {
            "id": 298,
            "user_id": 9517,
            "name": "Current Supplier",
            "contact_info": "current@example.invalid",
            "description": "current supplier profile",
        }

    def fetch_models_via_a6(self, user_id: int, base_url: str, api_key: str):
        self.events.append("a6.fetch_models")
        return ["gpt-5.6-sol"]

    def apply_supplier(self, user_id: int, payload):
        self.events.append("a6.apply_channel")
        channel = deepcopy(dict(payload["channel"]))
        channel["id"] = 700
        self.channels.append(channel)
        self.requests.append({"id": 800, "channel_name": channel["name"]})
        return {"request_id": 800, "supplier_id": 298}

    def supplier_listings(self, user_id: int, *, page=1, size=1000, status=None):
        self.events.append("a6.listings")
        return {"items": deepcopy(self.listings), "total": len(self.listings)}

    def create_supplier_listings(self, user_id: int, listings):
        self.events.append("a6.apply_listing")
        listing = deepcopy(dict(listings[0]))
        listing.update({"id": 900, "status": 2})
        self.listings.append(listing)
        return {"items": [{"id": 900}]}

    def update_supplier_channel(self, user_id: int, channel_id: int, payload):
        self.events.append("a6.update_channel")
        channel = next(
            item
            for item in self.channels
            if item["id"] == channel_id or item.get("binding_id") == channel_id
        )
        channel.update(deepcopy(dict(payload)))
        return {"id": channel_id}

    def update_supplier_listing(self, *args, **kwargs):
        raise AssertionError("unexpected listing update")


def orchestrator(*, roles: tuple[str, ...] = ("seller", "observer")):
    events: list[str] = []
    seller_store = SellerStore(context(roles))
    seller_service = SellerService(seller_store, events)
    secret_sink = SecretSink(events)
    listing_store = ListingStore(events)
    a6 = A6Client(events)

    def client_factory(_context, _session):
        events.append("a6.factory")
        return a6

    result = SellerResourceOrchestrator(
        seller_service,
        seller_store,
        secret_sink,
        listing_store,
        client_factory,
        session_loader=lambda _path: session(),
        template_loader=lambda _template_id: template(),
    )
    return result, seller_store, listing_store, a6, events


def test_manual_session_without_template_uses_stable_account_template() -> None:
    service, *_unused = orchestrator()
    selected = []
    service.session_loader = lambda _path: replace(session(), template_id="")
    service.template_loader = lambda template_id: selected.append(template_id) or template()

    service._prepare_one(request(), context())

    assert selected == ["merchant-117"]


def test_preflight_retries_transient_a6_transport_without_weakening_barrier() -> None:
    service, *_unused = orchestrator()
    attempts = []
    sleeps = []

    def preflight(*_args, **_kwargs):
        attempts.append(len(attempts) + 1)
        if len(attempts) < 6:
            raise A6AutoError("a6_transport_failed")

    service._preflight_one = preflight  # type: ignore[method-assign]
    service.sleeper = sleeps.append

    service._preflight_one_with_transport_retry(
        request(),
        SimpleNamespace(),
        capability=object(),
    )

    assert attempts == [1, 2, 3, 4, 5, 6]
    assert sleeps == [1, 2, 3, 4, 5]


def test_preflight_transport_retry_keeps_bounded_failure() -> None:
    service, *_unused = orchestrator()
    attempts = []
    sleeps = []

    def preflight(*_args, **_kwargs):
        attempts.append(len(attempts) + 1)
        raise A6AutoError("a6_transport_failed")

    service._preflight_one = preflight  # type: ignore[method-assign]
    service.sleeper = sleeps.append

    with pytest.raises(A6AutoError, match="a6_transport_failed"):
        service._preflight_one_with_transport_retry(
            request(),
            SimpleNamespace(),
            capability=object(),
        )

    assert attempts == [1, 2, 3, 4, 5, 6]
    assert sleeps == [1, 2, 3, 4, 5]


def test_pricing_enforces_profit_floor_and_scales_all_price_dimensions() -> None:
    value = pricing()

    assert value.landed_cost_multiplier == pytest.approx(Decimal("0.60"))
    assert value.minimum_price_multiplier == pytest.approx(Decimal("0.48"))
    assert value.price_multiplier == pytest.approx(Decimal("0.48"))
    assert value.market_multiplier == pytest.approx(Decimal("0.576"))
    assert value.final_multiplier == pytest.approx(Decimal("0.72"))
    assert value.prices == (2_400_001, 14_400_006, 240_000, 3_000_001)

    with pytest.raises(SellerOrchestrationError, match="seller_profit_floor_violated"):
        pricing(price_multiplier="0.39")
    with pytest.raises(SellerOrchestrationError, match="seller_price_infeasible"):
        pricing(cost_multiplier="2")


def test_delta_discounts_panel_price_while_preserving_final_profit_floor() -> None:
    pin_cost = Decimal("0.0086258")
    value = pricing(cost_multiplier=pin_cost, delta="0.5")

    assert value.landed_cost_multiplier == pytest.approx(pin_cost * Decimal("1.20"))
    assert value.price_multiplier == pytest.approx(pin_cost * Decimal("0.96"))
    assert value.market_multiplier == pytest.approx(pin_cost * Decimal("1.152"))
    assert value.final_multiplier == pytest.approx(pin_cost * Decimal("1.44"))


def test_selling_at_original_value_needs_delta_point_four_four_for_twenty_percent_profit() -> None:
    value = SellerPricing(
        cost_multiplier=Decimal("100"),
        conversion_factor=Decimal(5) / Decimal(6),
        margin=Decimal("0.20"),
        delta=Decimal("0.44"),
        price_multiplier=Decimal("100"),
        maximum_price_multiplier=Decimal("200"),
    )

    assert value.landed_cost_multiplier == Decimal("120")
    assert value.minimum_price_multiplier == Decimal("100")
    assert value.final_multiplier == Decimal("144.00")
    assert value.final_multiplier / value.landed_cost_multiplier - 1 == Decimal("0.20")


def test_official_price_fixture_matches_captured_a6_listing_payload() -> None:
    # Live gpt-5.6-sol Listing readback exposes these exact official prices.
    assert GPT_5_6_SOL_OFFICIAL_PRICES == (
        5_000_000,
        30_000_000,
        500_000,
        6_250_000,
    )
    assert MODEL_OFFICIAL_PRICES["gpt-5.6-terra"] == (
        500_000,
        3_000_000,
        50_000,
        625_000,
    )


@pytest.mark.parametrize(
    "roles,duty",
    [
        (("seller",), None),
        (("seller", "provider"), "provider"),
        (("seller", "observer"), "observer"),
    ],
)
def test_all_seller_duties_follow_identical_resource_flow(roles, duty) -> None:
    service, _seller_store, listing_store, a6, events = orchestrator(roles=roles)

    result = service.execute(request(), apply=True).summary()

    merchant = result["merchants"][0]
    assert result["success"] is True
    assert merchant["base_role"] == "seller"
    assert merchant["duty"] == duty
    assert merchant["seller"]["verified"] is True
    assert merchant["channel"]["state"] == "verified"
    assert merchant["listing"]["state"] == "verified"
    assert listing_store.channel is not None
    assert listing_store.listing is not None
    serialized = repr((result, listing_store.channel, listing_store.listing))
    assert API_KEY not in serialized
    assert a6.channels[0]["api_key"] == API_KEY
    assert events.index("seller.execute") < events.index("secret.read")
    assert events.index("secret.read") < events.index("a6.apply_channel")
    assert events.index("store.verify_channel") < events.index("a6.apply_listing")


def test_price_band_strategy_adopts_only_existing_channel() -> None:
    service, seller_store, listing_store, a6, events = orchestrator()
    seller_store.binding = binding()
    service.seller_service.verified = True
    a6.channels.append(
        {
            "id": 42,
            "binding_id": 10_042,
            "supplier_id": 298,
            "name": "existing-channel",
            "nickname": "existing-channel",
            "channel_type": 1,
            "status": 1,
            "availability": 1,
            "models": "gpt-5.5",
            "description": "existing description",
            "base_url": "https://ai.enodot.shop/v1",
            "api_key_last4": API_KEY[-4:],
            "has_api_key": True,
        }
    )

    result = service.execute(
        request(
            channel_strategy="adopt_existing",
            existing_channel_id=42,
            config_version="phase3-seller-price-band-v2",
        ),
        apply=True,
    ).summary()

    merchant = result["merchants"][0]
    assert result["success"] is True
    assert merchant["channel"]["action"] == "adopt_existing"
    assert merchant["channel"]["external_id"] == 42
    assert merchant["listing"]["state"] == "verified"
    assert listing_store.channel is not None
    assert listing_store.channel.a6_channel_id == 42
    assert a6.channels[0]["models"] == "gpt-5.5,gpt-5.6-sol"
    assert a6.channels[0]["name"] == "existing-channel"
    assert a6.listings[0]["description"] == (
        "Team 订阅来源"
    )
    assert "a6.apply_channel" not in events


@pytest.mark.parametrize("apply", [False, True])
def test_explicit_channel_unknown_recovery_reuses_bound_seller_resources(apply) -> None:
    service, seller_store, _listing_store, _a6, events = orchestrator(
        roles=("seller", "observer")
    )
    seller_store.binding = binding()
    service.seller_service.verified = True
    calls: list[bool] = []

    class RecoveryController:
        def resolve_channel_create_unknown(self, merchant, spec, *, apply=False):
            assert merchant.roles == ("seller", "observer")
            assert spec.api_key == API_KEY
            calls.append(apply)
            return ManagedDecision(
                merchant_id=merchant.merchant_id,
                resource_kind="channel",
                managed_uid=stable_managed_uid("channel", 5, "seller-primary"),
                action="resolve_absent",
                state="error" if apply else "create_unknown",
                applied=apply,
            )

    service.listing_controller_factory = lambda _store, _client: RecoveryController()

    result = service.resolve_channel_create_unknown(request(), apply=apply).summary()

    assert result["success"] is True
    assert result["operation"] == "resolve_channel_create_unknown"
    assert result["base_role"] == "seller"
    assert result["duty"] == "observer"
    assert result["channel"]["action"] == "resolve_absent"
    assert calls == [apply]
    assert events.count("secret.read") == 1


def test_dry_run_new_seller_reports_channel_and_listing_as_planned_blocked() -> None:
    service, seller_store, listing_store, a6, events = orchestrator()

    result = service.execute(request()).summary()

    merchant = result["merchants"][0]
    assert result["success"] is True
    assert result["mode"] == "dry-run"
    assert merchant["seller"]["action"] == "create"
    assert merchant["channel"] == {
        "merchant_id": 5,
        "resource_kind": "channel",
        "managed_uid": stable_managed_uid("channel", 5, "seller-primary"),
        "action": "blocked",
        "state": "planned",
        "applied": False,
        "external_id": None,
        "error_class": None,
        "blocked_by": "enodot_seller",
    }
    assert merchant["listing"]["action"] == "blocked"
    assert merchant["listing"]["state"] == "planned"
    assert seller_store.binding is None
    assert listing_store.channel is None
    assert listing_store.listing is None
    assert a6.channels == []
    assert "secret.read" not in events
    assert "a6.apply_channel" not in events


def test_pending_channel_reconciles_without_reposting_after_preflight() -> None:
    service, _seller_store, listing_store, a6, events = orchestrator()
    post_count = 0

    def pending_channel(_user_id, _payload):
        nonlocal post_count
        post_count += 1
        events.append("a6.apply_channel")
        return {"request_id": 800, "supplier_id": 298}

    a6.apply_supplier = pending_channel  # type: ignore[method-assign]

    first = service.execute(request(), apply=True).summary()
    second = service.execute(request(), apply=True).summary()

    assert first["merchants"][0]["channel"]["state"] == "verifying"
    assert second["merchants"][0]["channel"]["action"] == "reconcile_pending"
    assert second["merchants"][0]["channel"]["state"] == "verifying"
    assert listing_store.channel is not None
    assert listing_store.channel.control_state == "verifying"
    assert post_count == 1


def test_pending_listing_reconciles_without_reposting_after_preflight() -> None:
    service, _seller_store, listing_store, a6, events = orchestrator()
    post_count = 0

    def pending_listing(_user_id, _listings):
        nonlocal post_count
        post_count += 1
        events.append("a6.apply_listing")
        return {"items": [{"id": 900}]}

    a6.create_supplier_listings = pending_listing  # type: ignore[method-assign]

    first = service.execute(request(), apply=True).summary()
    second = service.execute(request(), apply=True).summary()

    assert first["merchants"][0]["channel"]["state"] == "verified"
    assert first["merchants"][0]["listing"]["state"] == "verifying"
    assert second["merchants"][0]["listing"]["action"] == "reconcile_pending"
    assert second["merchants"][0]["listing"]["state"] == "verifying"
    assert listing_store.listing is not None
    assert listing_store.listing.control_state == "verifying"
    assert post_count == 1


def test_preflight_failure_happens_before_enodot_or_a6_mutation() -> None:
    service, _seller_store, _listing_store, _a6, events = orchestrator()
    service.session_loader = lambda _path: replace(session(), supplier_id=999)

    with pytest.raises(SellerOrchestrationError, match="seller_session_context_mismatch"):
        service.execute(request(), apply=True)

    assert "seller.execute" not in events
    assert "a6.apply_channel" not in events


def test_second_merchant_preflight_failure_prevents_all_apply_calls() -> None:
    events: list[str] = []
    seller_service = SimpleNamespace(
        preflight_contract=lambda: events.append("capability") or object()
    )
    contexts = {
        merchant_id: replace(
            context(("seller",)),
            merchant_id=merchant_id,
            a6_user_id=9_500 + merchant_id,
            supplier_id=200 + merchant_id,
            session_ref=f"/private/session-{merchant_id}.json",
        )
        for merchant_id in (5, 19)
    }
    seller_store = SimpleNamespace(load_context=contexts.__getitem__)
    service = SellerResourceOrchestrator(
        seller_service,
        seller_store,
        object(),
        object(),
        lambda *_args: object(),
    )
    service._prepare_one = lambda _request, seller_context: seller_context.merchant_id  # type: ignore[method-assign]

    def preflight(_request, merchant_id, *, capability):
        events.append(f"preflight:{merchant_id}")
        if merchant_id == 19:
            raise SellerOrchestrationError("seller_second_preflight_failed")

    def execute_one(_request, merchant_id, *, apply, capability):
        events.append(f"apply:{merchant_id}")
        raise AssertionError("apply phase must not start")

    service._preflight_one = preflight  # type: ignore[method-assign]
    service._execute_one = execute_one  # type: ignore[method-assign]

    with pytest.raises(SellerOrchestrationError, match="seller_second_preflight_failed"):
        service.execute(request(merchant_ids=(5, 19)), apply=True)

    assert events == ["capability", "preflight:5", "preflight:19"]


def test_priced_apply_checks_every_remote_once_before_callback_or_mutation() -> None:
    events: list[str] = []
    contexts = {
        merchant_id: replace(
            context(("seller",)),
            merchant_id=merchant_id,
            a6_user_id=9_500 + merchant_id,
            supplier_id=200 + merchant_id,
            session_ref=f"/private/session-{merchant_id}.json",
        )
        for merchant_id in (5, 19)
    }
    service = SellerResourceOrchestrator(
        SimpleNamespace(preflight_contract=lambda: events.append("capability") or object()),
        SimpleNamespace(load_context=contexts.__getitem__),
        object(),
        object(),
        lambda *_args: object(),
    )
    service._prepare_one = lambda _request, seller_context: seller_context.merchant_id  # type: ignore[method-assign]

    def preflight(_request, merchant_id, *, capability):
        events.append(f"preflight:{merchant_id}")

    def execute_one(_request, merchant_id, *, apply, capability):
        events.append(f"apply:{merchant_id}")
        return SellerMerchantResourceResult(
            merchant_id=merchant_id,
            a6_user_id=contexts[merchant_id].a6_user_id,
            roles=("seller",),
            seller={"action": "noop", "state": "bound"},
            channel={"action": "noop", "state": "verified"},
            listing={"action": "noop", "state": "verified"},
        )

    service._preflight_one = preflight  # type: ignore[method-assign]
    service._execute_one = execute_one  # type: ignore[method-assign]
    priced_request = request(merchant_ids=(5, 19))

    service.execute_priced(
        {5: priced_request, 19: priced_request},
        apply=True,
        before_apply=lambda ready: events.append(f"barrier:{ready}"),
    )

    assert events == [
        "capability",
        "preflight:5",
        "preflight:19",
        "barrier:(5, 19)",
        "apply:5",
        "apply:19",
    ]


def test_priced_apply_runs_merchants_concurrently_with_one_global_barrier() -> None:
    merchant_ids = (5, 19)
    contexts = {
        merchant_id: replace(
            context(("seller",)),
            merchant_id=merchant_id,
            a6_user_id=9_500 + merchant_id,
            supplier_id=200 + merchant_id,
            session_ref=f"/private/session-{merchant_id}.json",
        )
        for merchant_id in merchant_ids
    }
    rendezvous = threading.Barrier(2, timeout=1)
    preflight_count = 0
    count_lock = threading.Lock()
    service = SellerResourceOrchestrator(
        SimpleNamespace(preflight_contract=lambda: object()),
        SimpleNamespace(load_context=contexts.__getitem__),
        object(),
        object(),
        lambda *_args: object(),
        max_workers=2,
        round_time_budget_seconds=10,
    )
    service._prepare_one = lambda _request, seller_context: seller_context.merchant_id  # type: ignore[method-assign]

    def preflight(_request, _merchant_id, *, capability):
        nonlocal preflight_count
        rendezvous.wait()
        with count_lock:
            preflight_count += 1

    def execute_one(_request, merchant_id, *, apply, capability):
        rendezvous.wait()
        return SellerMerchantResourceResult(
            merchant_id=merchant_id,
            a6_user_id=contexts[merchant_id].a6_user_id,
            roles=("seller",),
            seller={"action": "noop", "state": "bound"},
            channel={"action": "noop", "state": "verified"},
            listing={"action": "noop", "state": "verified"},
        )

    service._preflight_one = preflight  # type: ignore[method-assign]
    service._execute_one = execute_one  # type: ignore[method-assign]
    priced_request = request(merchant_ids=merchant_ids)
    barrier_observations: list[int] = []

    result = service.execute_priced(
        {merchant_id: priced_request for merchant_id in merchant_ids},
        apply=True,
        before_apply=lambda _ready: barrier_observations.append(preflight_count),
    )

    assert preflight_count == 2
    assert barrier_observations == [2]
    assert tuple(item.merchant_id for item in result) == merchant_ids


def test_priced_apply_staggers_activation_but_not_price_updates() -> None:
    events: list[str] = []
    merchant_ids = (5, 6, 19)
    contexts = {
        merchant_id: replace(
            context(("seller",)),
            merchant_id=merchant_id,
            a6_user_id=9_500 + merchant_id,
            supplier_id=200 + merchant_id,
            session_ref=f"/private/session-{merchant_id}.json",
        )
        for merchant_id in merchant_ids
    }
    service = SellerResourceOrchestrator(
        SimpleNamespace(preflight_contract=lambda: object()),
        SimpleNamespace(load_context=contexts.__getitem__),
        object(),
        object(),
        lambda *_args: object(),
        activation_stagger_seconds=5,
        sleeper=lambda seconds: events.append(f"sleep:{seconds:g}"),
    )
    service._prepare_one = lambda _request, seller_context: seller_context.merchant_id  # type: ignore[method-assign]
    service._preflight_one = lambda *_args, **_kwargs: None  # type: ignore[method-assign]

    def execute_one(_request, merchant_id, *, apply, capability):
        events.append(f"apply:{merchant_id}")
        action = "create" if merchant_id == 6 else "update"
        return SellerMerchantResourceResult(
            merchant_id=merchant_id,
            a6_user_id=contexts[merchant_id].a6_user_id,
            roles=("seller",),
            seller={"action": "noop", "state": "bound"},
            channel={"action": "noop", "state": "verified"},
            listing={
                "action": action,
                "state": "verified",
                "applied": True,
            },
        )

    service._execute_one = execute_one  # type: ignore[method-assign]
    priced_request = request(merchant_ids=merchant_ids)
    service.execute_priced(
        {merchant_id: priced_request for merchant_id in merchant_ids},
        apply=True,
    )

    assert events == ["apply:5", "apply:6", "sleep:5", "apply:19"]


def test_price_jump_release_updates_offline_reads_back_then_resubmits() -> None:
    service, *_unused = orchestrator()
    calls: list[tuple[str, int | bool]] = []
    listing_uid = stable_managed_uid(
        "listing",
        5,
        f"gpt-5.6-sol:{managed_marker(stable_managed_uid('channel', 5, 'seller-primary'))}",
    )

    class ReleaseController:
        def update_listing(
            self,
            _merchant,
            _uid,
            _spec,
            *,
            expected_status=None,
            apply=False,
        ):
            calls.append(("update", expected_status))
            assert apply is True
            return ManagedDecision(
                5,
                "listing",
                listing_uid,
                "update",
                "verified",
                True,
                external_id=900,
            )

        def resubmit_listing(self, _merchant, _uid, *, apply=False):
            calls.append(("resubmit", apply))
            return ManagedDecision(
                5,
                "listing",
                listing_uid,
                "resubmit",
                "verified",
                True,
                external_id=900,
            )

    result = service._release_listing(
        ReleaseController(),
        SimpleNamespace(merchant_id=5, a6_user_id=9517, roles=("seller",)),
        listing_uid,
        object(),
        apply=True,
    )

    assert calls == [("update", 0), ("resubmit", True)]
    assert result["action"] == "release"
    assert result["state"] == "verified"
    assert result["release_sequence"] == [
        "update_offline",
        "strict_readback_offline",
        "resubmit",
    ]
    assert result["price_update"]["action"] == "update"


def test_price_jump_release_never_resubmits_when_offline_price_readback_is_unconfirmed() -> None:
    service, *_unused = orchestrator()
    calls: list[str] = []

    class FailedUpdateController:
        def update_listing(self, *_args, **_kwargs):
            calls.append("update")
            return ManagedDecision(
                5,
                "listing",
                "listing-uid",
                "update",
                "update_unknown",
                True,
                external_id=900,
                error_class="managed_listing_update_readback_mismatch",
            )

        def resubmit_listing(self, *_args, **_kwargs):
            calls.append("resubmit")
            raise AssertionError("unconfirmed price must remain offline")

    result = service._release_listing(
        FailedUpdateController(),
        SimpleNamespace(merchant_id=5),
        "listing-uid",
        object(),
        apply=True,
    )

    assert calls == ["update"]
    assert result["state"] == "update_unknown"
    assert result["error_class"] == "managed_listing_update_readback_mismatch"


def test_price_jump_release_recovery_takes_down_active_stale_price_before_update() -> None:
    service, *_unused = orchestrator()
    calls: list[tuple[str, int | bool]] = []

    class ActiveStaleController:
        def update_listing(
            self,
            _merchant,
            _uid,
            _spec,
            *,
            expected_status=None,
            apply=False,
        ):
            calls.append(("update", expected_status))
            if expected_status == 0 and len(calls) == 1:
                raise ManagedListingError("managed_listing_activation_changed")
            return ManagedDecision(
                5,
                "listing",
                "listing-uid",
                "update" if expected_status == 2 else "update",
                "planned" if expected_status == 2 else "verified",
                apply,
                external_id=900,
            )

        def takedown_listing(self, _merchant, _uid, *, apply=False):
            calls.append(("takedown", apply))
            return ManagedDecision(
                5,
                "listing",
                "listing-uid",
                "takedown",
                "verified",
                apply,
                external_id=900,
            )

        def resubmit_listing(self, _merchant, _uid, *, apply=False):
            calls.append(("resubmit", apply))
            return ManagedDecision(
                5,
                "listing",
                "listing-uid",
                "resubmit",
                "verified",
                apply,
                external_id=900,
            )

    result = service._release_listing(
        ActiveStaleController(),
        SimpleNamespace(merchant_id=5),
        "listing-uid",
        object(),
        apply=True,
    )

    assert calls == [
        ("update", 0),
        ("update", 2),
        ("takedown", True),
        ("update", 0),
        ("resubmit", True),
    ]
    assert result["state"] == "verified"
    assert result["takedown"]["action"] == "takedown"


def test_price_jump_takedown_skips_seller_channel_and_price_mutations() -> None:
    service, _seller_store, _listing_store, _a6, events = orchestrator()
    controller_calls: list[bool] = []

    class TakedownOnlyController:
        def takedown_listing(self, _merchant, managed_uid, *, apply=False):
            controller_calls.append(apply)
            return ManagedDecision(
                5,
                "listing",
                managed_uid,
                "takedown",
                "verified",
                apply,
                external_id=900,
            )

    service.listing_controller_factory = lambda _store, _client: TakedownOnlyController()
    prepared = service._prepare_one(request(listing_mutation="takedown"), context())
    events.clear()

    result = service._execute_one(
        request(listing_mutation="takedown"),
        prepared,
        apply=True,
        capability=service.seller_service.capability,
    )

    assert controller_calls == [True]
    assert "seller.execute" not in events
    assert result.seller["action"] == "noop"
    assert result.channel["action"] == "noop"
    assert result.listing["action"] == "takedown"


def test_all_controlled_takedown_fans_out_to_every_managed_listing() -> None:
    service, _seller_store, listing_store, _a6, events = orchestrator()
    listing_uids = ("listing-primary", "listing-secondary")
    bindings = tuple(
        ManagedListingBinding(
            binding_id=index,
            managed_uid=managed_uid,
            merchant_id=5,
            channel_binding_id=100 + index,
            create_idempotency_key=f"create-{index}",
            control_state="verified",
            desired_state={"remote_listing": {"platform_model": "gpt-5.6-sol"}},
            a6_channel_id=700 + index,
            a6_listing_id=900 + index,
        )
        for index, managed_uid in enumerate(listing_uids, start=1)
    )
    listing_store.list_controlled_listings = lambda merchant_id, model: bindings
    calls: list[tuple[str, bool]] = []

    class FanoutController:
        def takedown_listing(self, _merchant, managed_uid, *, apply=False):
            calls.append((managed_uid, apply))
            return ManagedDecision(
                5,
                "listing",
                managed_uid,
                "takedown",
                "verified",
                apply,
                external_id=bindings[len(calls) - 1].a6_listing_id,
            )

    service.listing_controller_factory = lambda _store, _client: FanoutController()
    scoped = request(listing_mutation="takedown", listing_scope="all_controlled")
    prepared = service._prepare_one(scoped, context())
    events.clear()

    result = service._execute_one(
        scoped,
        prepared,
        apply=True,
        capability=service.seller_service.capability,
    )

    assert calls == [("listing-primary", True), ("listing-secondary", True)]
    assert "seller.execute" not in events
    assert result.error_class is None
    assert result.listing["scope"] == "all_controlled"
    assert result.listing["target_count"] == 2
    assert result.listing["verified_count"] == 2
    assert result.listing["state"] == "verified"


def test_single_merchant_fans_out_listings_with_independent_clients() -> None:
    service, _seller_store, listing_store, _a6, _events = orchestrator()
    listing_uids = tuple(f"listing-{index}" for index in range(4))
    bindings = tuple(
        ManagedListingBinding(
            binding_id=index,
            managed_uid=managed_uid,
            merchant_id=5,
            channel_binding_id=100 + index,
            create_idempotency_key=f"create-{index}",
            control_state="verified",
            desired_state={"remote_listing": {"platform_model": "gpt-5.6-sol"}},
            a6_channel_id=700 + index,
            a6_listing_id=900 + index,
        )
        for index, managed_uid in enumerate(listing_uids, start=1)
    )
    listing_store.list_controlled_listings = lambda _merchant_id, _model: bindings
    client_ids = iter(range(1, 20))
    client_lock = threading.Lock()

    class ForkClient:
        def __init__(self, client_id: int) -> None:
            self.client_id = client_id

        def fork(self):
            with client_lock:
                return ForkClient(next(client_ids))

    service.a6_client_factory = lambda _context, _session: ForkClient(0)
    service.max_workers = 4
    service.round_time_budget_seconds = 10
    barriers = {False: threading.Barrier(4), True: threading.Barrier(4)}
    calls: list[tuple[bool, int, str]] = []
    calls_lock = threading.Lock()

    class ConcurrentController:
        def __init__(self, client: ForkClient) -> None:
            self.client = client

        def takedown_listing(self, _merchant, managed_uid, *, apply=False):
            with calls_lock:
                calls.append((apply, self.client.client_id, managed_uid))
            barriers[apply].wait(timeout=1)
            external_id = next(
                item.a6_listing_id for item in bindings if item.managed_uid == managed_uid
            )
            return ManagedDecision(
                5,
                "listing",
                managed_uid,
                "takedown",
                "verified",
                apply,
                external_id=external_id,
            )

    service.listing_controller_factory = (
        lambda _store, client: ConcurrentController(client)
    )

    result = service.execute(
        request(listing_mutation="takedown", listing_scope="all_controlled"),
        apply=True,
    )

    for apply in (False, True):
        phase = [item for item in calls if item[0] is apply]
        assert len(phase) == 4
        assert len({item[1] for item in phase}) == 4
        assert {item[2] for item in phase} == set(listing_uids)
    assert [item["managed_uid"] for item in result.merchants[0].listing["listings"]] == list(
        listing_uids
    )


def test_parallel_runner_returns_at_round_deadline() -> None:
    service, *_unused = orchestrator()
    release = threading.Event()
    started = threading.Event()

    def slow_call(_item: int) -> None:
        started.set()
        release.wait(timeout=1)

    started_at = time.monotonic()
    try:
        with pytest.raises(SellerOrchestrationError, match="seller_round_deadline_exceeded"):
            service._run_ordered(
                (1, 2),
                slow_call,
                max_workers=2,
                round_deadline=time.monotonic() + 0.05,
                thread_name_prefix="seller-deadline-test",
            )
    finally:
        release.set()

    assert started.wait(timeout=1)
    assert time.monotonic() - started_at < 0.5


def test_all_controlled_delta_plan_runs_listing_only_after_verified_delta_apply() -> None:
    service, seller_store, listing_store, _a6, events = orchestrator()
    seller_store.binding = binding()
    controlled = ManagedListingBinding(
        binding_id=1,
        managed_uid="listing-primary",
        merchant_id=5,
        channel_binding_id=101,
        create_idempotency_key="create-1",
        control_state="verified",
        desired_state={"remote_listing": {"platform_model": "gpt-5.6-sol"}},
        a6_channel_id=701,
        a6_listing_id=901,
    )
    listing_store.list_controlled_listings = lambda merchant_id, model: (controlled,)

    class DeltaUpdateSellerService:
        capability = object()

        def preflight_contract(self):
            return self.capability

        def execute(self, desired, *, apply=False, capability=None):
            assert capability is self.capability
            if apply:
                events.extend(("seller.delta.write", "seller.delta.readback"))
                return ManagedSellerResult(
                    mode="applied",
                    merchant_id=5,
                    managed_uid=UID,
                    action="update_delta",
                    applied=True,
                    verified=True,
                    state="bound",
                    enodot_user_id=51,
                    enodot_api_key_id=61,
                    delta=desired.delta,
                    preflight_verified=True,
                )
            events.append("seller.delta.preflight")
            return ManagedSellerResult(
                mode="dry-run",
                merchant_id=5,
                managed_uid=UID,
                action="update_delta",
                applied=False,
                verified=False,
                state="bound",
                enodot_user_id=51,
                enodot_api_key_id=61,
                delta=Decimal("0.100000"),
                preflight_verified=True,
            )

    class ListingUpdateController:
        def update_listing(self, merchant, managed_uid, _spec, *, apply=False):
            assert merchant.merchant_id == 5
            assert managed_uid == controlled.managed_uid
            events.append("listing.apply" if apply else "listing.preflight")
            return ManagedDecision(
                merchant_id=5,
                resource_kind="listing",
                managed_uid=managed_uid,
                action="update",
                state="verified" if apply else "planned",
                applied=apply,
                external_id=controlled.a6_listing_id,
            )

    service.seller_service = DeltaUpdateSellerService()
    service.listing_controller_factory = lambda _store, _client: ListingUpdateController()
    events.clear()

    result = service.execute(
        request(listing_scope="all_controlled"),
        apply=True,
    ).summary()

    assert result["success"] is True
    assert result["merchants"][0]["seller"]["action"] == "update_delta"
    assert result["merchants"][0]["listing"]["scope"] == "all_controlled"
    assert events == [
        "a6.factory",
        "seller.delta.preflight",
        "listing.preflight",
        "seller.delta.write",
        "seller.delta.readback",
        "listing.apply",
    ]


def test_priced_apply_does_not_repeat_identical_remote_preflight() -> None:
    events: list[str] = []
    contexts = {
        merchant_id: replace(
            context(("seller",)),
            merchant_id=merchant_id,
            a6_user_id=9_500 + merchant_id,
            supplier_id=200 + merchant_id,
            session_ref=f"/private/session-{merchant_id}.json",
        )
        for merchant_id in (5, 19)
    }
    service = SellerResourceOrchestrator(
        SimpleNamespace(preflight_contract=lambda: object()),
        SimpleNamespace(load_context=contexts.__getitem__),
        object(),
        object(),
        lambda *_args: object(),
    )
    service._prepare_one = lambda _request, seller_context: seller_context.merchant_id  # type: ignore[method-assign]
    def preflight(_request, merchant_id, *, capability):
        events.append(f"preflight:{merchant_id}")

    service._preflight_one = preflight  # type: ignore[method-assign]
    service._execute_one = lambda *_args, **_kwargs: events.append("apply")  # type: ignore[method-assign]
    priced_request = request(merchant_ids=(5, 19))

    service.execute_priced(
        {5: priced_request, 19: priced_request},
        apply=True,
        before_apply=lambda _ready: events.append("barrier"),
    )

    assert events == [
        "preflight:5",
        "preflight:19",
        "barrier",
        "apply",
        "apply",
    ]


def test_nineteen_sellers_partition_sixteen_ready_and_three_not_ready() -> None:
    events: list[str] = []
    contexts = {
        merchant_id: ManagedSellerContext(
            merchant_id=merchant_id,
            a6_user_id=9_500 + merchant_id if merchant_id <= 16 else None,
            supplier_id=200 + merchant_id if merchant_id <= 16 else None,
            merchant_status=("supplier_submitted" if merchant_id <= 16 else "allocated"),
            roles=(
                ("seller", "provider")
                if merchant_id in {1, 2}
                else ("seller", "observer")
                if merchant_id in {3, 4, 5}
                else ("seller",)
            ),
            session_ref=f"/private/session-{merchant_id}.json",
        )
        for merchant_id in range(1, 20)
    }
    capability = object()
    seller_service = SimpleNamespace(
        preflight_contract=lambda: events.append("capability") or capability
    )
    clients: list[int] = []

    def session_loader(path):
        merchant_id = int(path.stem.rsplit("-", 1)[1])
        events.append(f"session:{merchant_id}")
        return replace(
            session(),
            user_id=9_500 + merchant_id,
            supplier_id=200 + merchant_id,
            template_id="merchant-005",
        )

    def client_factory(seller_context, _record):
        clients.append(seller_context.merchant_id)
        return object()

    service = SellerResourceOrchestrator(
        seller_service,
        SimpleNamespace(load_context=contexts.__getitem__),
        object(),
        object(),
        client_factory,
        session_loader=session_loader,
        template_loader=lambda _template_id: template(),
    )

    def preflight(_request, prepared, *, capability):
        assert capability is not None
        events.append(f"preflight:{prepared.context.merchant_id}")

    def execute_one(_request, prepared, *, apply, capability):
        merchant_id = prepared.context.merchant_id
        events.append(f"apply:{merchant_id}")
        return SellerMerchantResourceResult(
            merchant_id=merchant_id,
            a6_user_id=prepared.context.a6_user_id,
            roles=prepared.context.roles,
            seller={"action": "create", "state": "bound", "verified": True},
            channel={"action": "create", "state": "verified"},
            listing={"action": "create", "state": "verified"},
        )

    service._preflight_one = preflight  # type: ignore[method-assign]
    service._execute_one = execute_one  # type: ignore[method-assign]

    summary = service.execute(
        request(merchant_ids=tuple(range(19, 0, -1))),
        apply=True,
    ).summary()

    assert summary["success"] is True
    assert summary["merchant_count"] == 19
    assert summary["ready_merchant_count"] == 16
    assert summary["not_ready_merchant_count"] == 3
    assert [item["merchant_id"] for item in summary["merchants"]] == list(
        range(19, 0, -1)
    )
    assert clients == list(range(16, 0, -1))
    not_ready = summary["merchants"][:3]
    assert all(item["readiness"] == "not_ready" for item in not_ready)
    assert all(item["seller"]["action"] == "not_ready" for item in not_ready)
    assert all(item["seller"]["state"] == "pending" for item in not_ready)
    assert all(item["channel"]["blocked_by"] == "seller_not_ready" for item in not_ready)
    assert all(item["listing"]["state"] == "pending" for item in not_ready)
    assert not any(event in {"session:17", "session:18", "session:19"} for event in events)


def test_untracked_remote_marker_blocks_seller_and_a6_mutations() -> None:
    service, seller_store, _listing_store, a6, events = orchestrator()
    channel_uid = stable_managed_uid("channel", 5, "seller-primary")
    a6.channels.append(
        {
            "id": 701,
            "name": f"orphan-{managed_marker(channel_uid)}",
            "description": channel_uid,
        }
    )

    with pytest.raises(SellerOrchestrationError, match="seller_channel_remote_untracked"):
        service.execute(request(), apply=True)

    assert seller_store.binding is None
    assert "a6.apply_channel" not in events
    assert "a6.apply_listing" not in events


@pytest.mark.parametrize("short_field", ["name", "description"])
def test_untracked_short_channel_signature_blocks_seller_mutations(short_field) -> None:
    service, seller_store, _listing_store, a6, events = orchestrator()
    channel_uid = stable_managed_uid("channel", 5, "seller-primary")
    value = (
        managed_channel_name(channel_uid)
        if short_field == "name"
        else managed_resource_signature("channel", channel_uid)
    )
    a6.requests.append({"id": 701, short_field: value})

    with pytest.raises(SellerOrchestrationError, match="seller_channel_remote_untracked"):
        service.execute(request(), apply=True)

    assert seller_store.binding is None
    assert "a6.apply_channel" not in events


def test_sixteenth_ready_preflight_conflict_keeps_entire_batch_write_free() -> None:
    events: list[str] = []

    class MultiSellerStore:
        def load_context(self, merchant_id):
            return ManagedSellerContext(
                merchant_id=merchant_id,
                a6_user_id=9_500 + merchant_id if merchant_id <= 16 else None,
                supplier_id=200 + merchant_id if merchant_id <= 16 else None,
                merchant_status=(
                    "supplier_submitted" if merchant_id <= 16 else "allocated"
                ),
                roles=("seller", "observer") if merchant_id == 16 else ("seller",),
                session_ref=f"/private/session-{merchant_id}.json",
            )

        def get_binding(self, _merchant_id):
            return None

    class MultiSellerService:
        def __init__(self):
            self.capability = object()

        def preflight_contract(self):
            events.append("seller.capability")
            return self.capability

        def execute(self, desired, *, apply=False, capability=None):
            assert capability is self.capability
            events.append(f"seller.{'apply' if apply else 'read'}:{desired.merchant_id}")
            if desired.merchant_id == 16:
                raise ManagedSellerError("managed_seller_remote_identity_mismatch")
            return ManagedSellerResult(
                mode="applied" if apply else "dry-run",
                merchant_id=desired.merchant_id,
                managed_uid=desired.managed_uid,
                action="create",
                applied=apply,
                verified=False,
                state="planned",
                delta=desired.delta,
            )

    seller_store = MultiSellerStore()
    listing_store = ListingStore(events)
    clients: dict[int, A6Client] = {}

    def client_factory(seller_context, _record):
        client = clients.setdefault(seller_context.merchant_id, A6Client(events))
        return client

    def session_loader(path):
        merchant_id = int(path.stem.rsplit("-", 1)[1])
        return replace(
            session(),
            user_id=9_500 + merchant_id,
            supplier_id=200 + merchant_id,
            template_id="merchant-005",
        )

    orchestrator = SellerResourceOrchestrator(
        MultiSellerService(),
        seller_store,
        SecretSink(events),
        listing_store,
        client_factory,
        session_loader=session_loader,
        template_loader=lambda _template_id: template(),
    )

    with pytest.raises(ManagedSellerError, match="managed_seller_remote_identity_mismatch"):
        orchestrator.execute(request(merchant_ids=tuple(range(1, 20))), apply=True)

    assert [
        event for event in events if event.startswith("seller.read:")
    ] == [f"seller.read:{merchant_id}" for merchant_id in range(1, 17)]
    assert not any(event.startswith("seller.apply:") for event in events)
    assert "store.reserve_channel" not in events
    assert "store.reserve_listing" not in events
    assert sorted(clients) == list(range(1, 17))
    assert all(client.channels == [] and client.listings == [] for client in clients.values())


@pytest.mark.parametrize(
    "pending_kind,pending_state",
    [
        ("channel", "update_unknown"),
        ("listing", "update_unknown"),
    ],
)
def test_existing_pending_actions_use_update_reconciliation_paths(
    pending_kind: str,
    pending_state: str,
) -> None:
    service, seller_store, listing_store, _a6, events = orchestrator()
    seller_store.binding = binding()
    service.seller_service.verified = True
    channel_uid = stable_managed_uid("channel", 5, "seller-primary")
    listing_uid = stable_managed_uid(
        "listing",
        5,
        f"gpt-5.6-sol:{managed_marker(channel_uid)}",
    )
    listing_store.channel = ManagedChannelBinding(
        binding_id=10,
        managed_uid=channel_uid,
        merchant_id=5,
        create_idempotency_key=f"managed-channel-create:{channel_uid}",
        control_state=pending_state if pending_kind == "channel" else "verified",
        desired_state={"spec_fingerprint": "channel"},
        a6_channel_id=700,
        create_action_id=100,
    )
    listing_store.listing = ManagedListingBinding(
        binding_id=11,
        managed_uid=listing_uid,
        merchant_id=5,
        channel_binding_id=10,
        create_idempotency_key=f"managed-listing-create:{listing_uid}",
        control_state=pending_state if pending_kind == "listing" else "verified",
        desired_state={"spec_fingerprint": "listing"},
        a6_channel_id=700,
        a6_listing_id=900,
        create_action_id=101,
    )
    calls: list[tuple[str, bool]] = []

    class PendingController:
        def ensure_channel(self, _merchant, _spec, *, apply=False):
            calls.append(("ensure_channel", apply))
            return ManagedDecision(
                merchant_id=5,
                resource_kind="channel",
                managed_uid=channel_uid,
                action="verify" if pending_kind == "channel" else "noop",
                state="verified" if apply else listing_store.channel.control_state,
                applied=apply,
                external_id=700,
            )

        def ensure_channel_upstream(
            self,
            _merchant,
            _uid,
            _spec,
            *,
            model,
            apply=False,
        ):
            calls.append(("ensure_channel_upstream", apply))
            return ManagedDecision(
                merchant_id=5,
                resource_kind="channel",
                managed_uid=channel_uid,
                action="noop",
                state="verified",
                applied=apply,
                external_id=700,
            )

        def ensure_listing(self, *_args, **_kwargs):
            raise AssertionError("existing listing must not enter create reconciliation")

        def update_listing(self, _merchant, _uid, _spec, *, apply=False):
            calls.append(("update_listing", apply))
            return ManagedDecision(
                merchant_id=5,
                resource_kind="listing",
                managed_uid=listing_uid,
                action="verify" if pending_kind == "listing" else "noop",
                state="verified" if apply else listing_store.listing.control_state,
                applied=apply,
                external_id=900,
            )

    service.listing_controller_factory = lambda _store, _client: PendingController()

    result = service.execute(request(), apply=True).summary()

    assert result["success"] is True
    assert calls == [
        ("ensure_channel", False),
        ("ensure_channel_upstream", False),
        ("update_listing", False),
        ("ensure_channel", True),
        ("ensure_channel_upstream", True),
        ("update_listing", True),
    ]
    assert "a6.apply_channel" not in events
    assert "a6.apply_listing" not in events


@pytest.mark.parametrize(
    "roles",
    [
        ("observer",),
        ("provider", "seller"),
        ("seller", "seller"),
        ("seller", "provider", "observer"),
    ],
)
def test_role_shape_is_exactly_seller_plus_at_most_one_duty(roles) -> None:
    service, _seller_store, _listing_store, _a6, events = orchestrator(roles=roles)

    with pytest.raises((SellerOrchestrationError, ValueError)):
        service.execute(request())

    assert "seller.execute" not in events


def test_uid_is_independent_of_duty() -> None:
    uids = []
    for roles in (
        ("seller",),
        ("seller", "provider"),
        ("seller", "observer"),
    ):
        service, _seller_store, listing_store, _a6, _events = orchestrator(roles=roles)
        service.execute(request(), apply=True)
        assert listing_store.channel is not None
        assert listing_store.listing is not None
        uids.append((listing_store.channel.managed_uid, listing_store.listing.managed_uid))

    assert len(set(uids)) == 1


def test_duty_switch_reuses_existing_seller_channel_and_listing() -> None:
    service, seller_store, listing_store, _a6, events = orchestrator(
        roles=("seller", "observer")
    )

    first = service.execute(request(), apply=True).summary()
    seller_store.context = replace(
        seller_store.context,
        roles=("seller", "provider"),
    )
    second = service.execute(request(), apply=True).summary()

    assert first["merchants"][0]["duty"] == "observer"
    assert second["merchants"][0]["duty"] == "provider"
    assert second["merchants"][0]["seller"]["action"] == "noop"
    assert second["merchants"][0]["channel"]["action"] == "noop"
    assert second["merchants"][0]["listing"]["action"] == "noop"
    assert events.count("a6.apply_channel") == 1
    assert events.count("a6.apply_listing") == 1
    assert listing_store.channel is not None
    assert listing_store.listing is not None
