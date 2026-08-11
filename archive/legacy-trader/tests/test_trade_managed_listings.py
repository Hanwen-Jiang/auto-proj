from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import replace
from typing import Any

import pytest

from a6api_auto.errors import A6AutoError
from a6api_auto.trade_managed_listings import (
    SUPPORTED_MODEL,
    ChannelAdoptionIntent,
    ChannelCreateIntent,
    ListingAdoptionIntent,
    ListingCreateIntent,
    ManagedAction,
    ManagedChannelBinding,
    ManagedChannelSpec,
    ManagedListingBinding,
    ManagedListingController,
    ManagedListingError,
    ManagedListingSpec,
    ManagedMerchant,
    is_source_only_listing_description,
    managed_channel_name,
    managed_marker,
    managed_resource_signature,
    stable_managed_uid,
)

API_KEY = "sk-managed-channel-fixture-secret"


def merchant(
    merchant_id: int = 5,
    *,
    roles: tuple[str, ...] = ("seller",),
) -> ManagedMerchant:
    return ManagedMerchant(
        merchant_id=merchant_id,
        a6_user_id=9000 + merchant_id,
        supplier_id=200 + merchant_id,
        roles=roles,
    )


def channel_spec(*, logical_key: str = "primary") -> ManagedChannelSpec:
    return ManagedChannelSpec(
        logical_key=logical_key,
        supplier_name="Managed Supplier",
        contact_info="ops@example.invalid",
        supplier_description="fixture supplier",
        channel_name="Managed Channel",
        channel_nickname="managed",
        channel_description="fixture channel",
        base_url="https://fixture.invalid/v1",
        api_key=API_KEY,
    )


def listing_spec(
    *,
    logical_key: str = "gpt-5.6-sol",
    price_delta: int = 0,
) -> ManagedListingSpec:
    return ManagedListingSpec(
        logical_key=logical_key,
        input_price_micros=1_000_000 + price_delta,
        output_price_micros=2_000_000 + price_delta,
        cache_read_price_micros=100_000 + price_delta,
        cache_write_price_micros=200_000 + price_delta,
        description="Team 测试配额来源",
    )


@pytest.mark.parametrize(
    "description",
    (
        "Team池",
        "满血Team池",
        "Team + Plus混池",
        "bugteam共享池",
        "K12 + Pro混池",
    ),
)
def test_listing_source_accepts_short_pool_labels(description: str) -> None:
    assert is_source_only_listing_description(description)


@pytest.mark.parametrize(
    "description",
    (
        "Team面向复杂分析任务",
        "Pro高并发稳定线路",
        "优质混池",
        "Team池，适合连续调用",
        "Pro 20x早高峰池",
    ),
)
def test_listing_source_rejects_marketing_or_missing_source(description: str) -> None:
    assert not is_source_only_listing_description(description)


def channel_uid(subject: ManagedMerchant, *, logical_key: str = "primary") -> str:
    return stable_managed_uid("channel", subject.merchant_id, logical_key)


def listing_uid(
    subject: ManagedMerchant,
    bound_channel_uid: str,
    *,
    logical_key: str = "gpt-5.6-sol",
) -> str:
    return stable_managed_uid(
        "listing",
        subject.merchant_id,
        f"{logical_key}:{managed_marker(bound_channel_uid)}",
    )


def remote_channel(
    subject: ManagedMerchant,
    *,
    channel_id: int,
    spec: ManagedChannelSpec | None = None,
) -> dict[str, Any]:
    spec = spec or channel_spec()
    uid = channel_uid(subject, logical_key=spec.logical_key)
    return {
        "id": channel_id,
        "binding_id": channel_id + 10_000,
        "name": spec.channel_name,
        "nickname": spec.channel_nickname,
        "channel_type": 1,
        "base_url": spec.base_url,
        "api_key": API_KEY,
        "models": "gpt-5.6-sol",
        "description": (
            f"{spec.channel_description}\n{managed_resource_signature('channel', uid)}"
        ),
        "status": 1,
        "metadata": {
            "token": "nested-token-secret",
            "kept": [{"api_key": "nested-api-key-secret", "label": "ok"}],
        },
    }


def remote_listing(
    subject: ManagedMerchant,
    bound_channel_uid: str,
    *,
    channel_id: int,
    listing_id: int,
    spec: ManagedListingSpec | None = None,
    status: int = 2,
) -> dict[str, Any]:
    spec = spec or listing_spec()
    uid = listing_uid(subject, bound_channel_uid, logical_key=spec.logical_key)
    return {
        "id": listing_id,
        "channel_id": channel_id,
        "own_model": "gpt-5.6-sol",
        "platform_model": "gpt-5.6-sol",
        "charge_type": "per_token",
        "input_price_micros": spec.input_price_micros,
        "output_price_micros": spec.output_price_micros,
        "cache_read_price_micros": spec.cache_read_price_micros,
        "cache_write_price_micros": spec.cache_write_price_micros,
        "display_name": spec.display_name,
        "description": managed_resource_signature("listing", uid),
        "status": status,
    }


class FakeStore:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.channel: ManagedChannelBinding | None = None
        self.listing: ManagedListingBinding | None = None
        self.actions: dict[tuple[str, str], ManagedAction] = {}
        self.action_keys: list[str] = []
        self.write_count = 0
        self._next_action_id = 100

    def reset_metrics(self) -> None:
        self.events.clear()
        self.write_count = 0

    def _read(self, event: str) -> None:
        self.events.append(event)

    def _write(self, event: str) -> None:
        self.events.append(event)
        self.write_count += 1

    def get_channel(self, managed_uid: str) -> ManagedChannelBinding | None:
        self._read("store.get_channel")
        if self.channel is None or self.channel.managed_uid != managed_uid:
            return None
        return self.channel

    def reserve_channel(self, intent: ChannelCreateIntent) -> ManagedChannelBinding:
        self._write("store.reserve_channel")
        if self.channel is None:
            self.channel = ManagedChannelBinding(
                binding_id=1,
                managed_uid=intent.managed_uid,
                merchant_id=intent.merchant_id,
                create_idempotency_key=intent.create_idempotency_key,
                control_state="planned",
                desired_state=deepcopy(dict(intent.desired_state)),
            )
        return self.channel

    def adopt_channel(self, intent: ChannelAdoptionIntent) -> ManagedChannelBinding:
        self._write("store.adopt_channel")
        self.channel = ManagedChannelBinding(
            binding_id=1,
            managed_uid=intent.managed_uid,
            merchant_id=intent.merchant_id,
            create_idempotency_key=intent.idempotency_key,
            control_state="verified",
            desired_state=deepcopy(dict(intent.desired_state)),
            observed_state=deepcopy(dict(intent.observation)),
            a6_channel_id=intent.a6_channel_id,
            create_action_id=self._take_action_id(),
        )
        return self.channel

    def claim_channel_create(self, binding: ManagedChannelBinding) -> ManagedChannelBinding | None:
        self._write("store.claim_channel_create")
        if binding.control_state not in {"planned", "error"}:
            return None
        self.channel = replace(
            binding,
            control_state="creating",
            create_action_id=self._take_action_id(),
        )
        return self.channel

    def record_channel_post(
        self,
        binding: ManagedChannelBinding,
        observation: dict[str, Any],
    ) -> ManagedChannelBinding:
        self._write("store.record_channel_post")
        self.channel = replace(
            binding,
            control_state="verifying",
            observed_state=deepcopy(dict(observation)),
        )
        return self.channel

    def mark_channel_create_unknown(
        self,
        binding: ManagedChannelBinding,
        error_class: str,
    ) -> ManagedChannelBinding:
        self._write("store.mark_channel_create_unknown")
        self.channel = replace(
            binding,
            control_state="create_unknown",
            last_error_class=error_class,
        )
        return self.channel

    def mark_channel_create_failed(
        self,
        binding: ManagedChannelBinding,
        error_class: str,
        observation: dict[str, Any],
    ) -> ManagedChannelBinding:
        self._write("store.mark_channel_create_failed")
        self.channel = replace(
            binding,
            control_state="error",
            create_action_id=None,
            observed_state=deepcopy(observation),
            last_error_class=error_class,
        )
        return self.channel

    def resolve_channel_create_unknown(
        self,
        binding: ManagedChannelBinding,
        desired_state: dict[str, Any],
        observation: dict[str, Any],
        error_class: str,
    ) -> ManagedChannelBinding:
        self._write("store.resolve_channel_create_unknown")
        self.channel = replace(
            binding,
            control_state="error",
            create_action_id=None,
            desired_state=deepcopy(desired_state),
            observed_state=deepcopy(observation),
            last_error_class=error_class,
        )
        return self.channel

    def verify_channel(
        self,
        binding: ManagedChannelBinding,
        channel_id: int,
        observation: dict[str, Any],
    ) -> ManagedChannelBinding:
        self._write("store.verify_channel")
        self.channel = replace(
            binding,
            control_state="verified",
            a6_channel_id=channel_id,
            observed_state=deepcopy(dict(observation)),
            last_error_class=None,
        )
        return self.channel

    def get_listing(self, managed_uid: str) -> ManagedListingBinding | None:
        self._read("store.get_listing")
        if self.listing is None or self.listing.managed_uid != managed_uid:
            return None
        return self.listing

    def reserve_listing(self, intent: ListingCreateIntent) -> ManagedListingBinding:
        self._write("store.reserve_listing")
        if self.listing is None:
            self.listing = ManagedListingBinding(
                binding_id=2,
                managed_uid=intent.managed_uid,
                merchant_id=intent.merchant_id,
                channel_binding_id=intent.channel_binding_id,
                create_idempotency_key=intent.create_idempotency_key,
                control_state="planned",
                desired_state=deepcopy(dict(intent.desired_state)),
                a6_channel_id=intent.a6_channel_id,
            )
        return self.listing

    def adopt_listing(self, intent: ListingAdoptionIntent) -> ManagedListingBinding:
        self._write("store.adopt_listing")
        self.listing = ManagedListingBinding(
            binding_id=2,
            managed_uid=intent.managed_uid,
            merchant_id=intent.merchant_id,
            channel_binding_id=intent.channel_binding_id,
            create_idempotency_key=intent.idempotency_key,
            control_state="verified",
            desired_state=deepcopy(dict(intent.desired_state)),
            observed_state=deepcopy(dict(intent.observation)),
            a6_channel_id=intent.a6_channel_id,
            a6_listing_id=intent.a6_listing_id,
            create_action_id=self._take_action_id(),
        )
        return self.listing

    def claim_listing_create(self, binding: ManagedListingBinding) -> ManagedListingBinding | None:
        self._write("store.claim_listing_create")
        if binding.control_state not in {"planned", "error"}:
            return None
        self.listing = replace(
            binding,
            control_state="creating",
            create_action_id=self._take_action_id(),
        )
        return self.listing

    def record_listing_post(
        self,
        binding: ManagedListingBinding,
        observation: dict[str, Any],
    ) -> ManagedListingBinding:
        self._write("store.record_listing_post")
        self.listing = replace(
            binding,
            control_state="verifying",
            observed_state=deepcopy(dict(observation)),
        )
        return self.listing

    def mark_listing_create_unknown(
        self,
        binding: ManagedListingBinding,
        error_class: str,
    ) -> ManagedListingBinding:
        self._write("store.mark_listing_create_unknown")
        self.listing = replace(
            binding,
            control_state="create_unknown",
            last_error_class=error_class,
        )
        return self.listing

    def mark_listing_create_failed(
        self,
        binding: ManagedListingBinding,
        error_class: str,
        observation: dict[str, Any],
    ) -> ManagedListingBinding:
        self._write("store.mark_listing_create_failed")
        self.listing = replace(
            binding,
            control_state="error",
            create_action_id=None,
            observed_state=deepcopy(observation),
            last_error_class=error_class,
        )
        return self.listing

    def resolve_listing_create_unknown(
        self,
        binding: ManagedListingBinding,
        desired_state: dict[str, Any],
        observation: dict[str, Any],
        error_class: str,
    ) -> ManagedListingBinding:
        self._write("store.resolve_listing_create_unknown")
        self.listing = replace(
            binding,
            control_state="error",
            create_action_id=None,
            desired_state=deepcopy(desired_state),
            observed_state=deepcopy(observation),
            last_error_class=error_class,
        )
        return self.listing

    def verify_listing(
        self,
        binding: ManagedListingBinding,
        listing_id: int,
        observation: dict[str, Any],
    ) -> ManagedListingBinding:
        self._write("store.verify_listing")
        self.listing = replace(
            binding,
            control_state="verified",
            a6_listing_id=listing_id,
            observed_state=deepcopy(dict(observation)),
            last_error_class=None,
        )
        return self.listing

    def get_pending_action(self, resource_kind: str, managed_uid: str) -> ManagedAction | None:
        self._read("store.get_pending_action")
        return self.actions.get((resource_kind, managed_uid))

    def begin_action(
        self,
        resource_kind: str,
        managed_uid: str,
        operation: str,
        desired_state: dict[str, Any],
        idempotency_key: str,
    ) -> ManagedAction | None:
        self._write(f"store.begin_{operation}")
        self.action_keys.append(idempotency_key)
        key = (resource_kind, managed_uid)
        if key in self.actions:
            return None
        state = {
            "update": "updating",
            "takedown": "taking_down",
            "resubmit": "resubmitting",
        }[operation]
        action = ManagedAction(
            action_id=self._take_action_id(),
            resource_kind=resource_kind,
            managed_uid=managed_uid,
            operation=operation,
            idempotency_key=idempotency_key,
            desired_state=deepcopy(dict(desired_state)),
            state=state,
        )
        self.actions[key] = action
        if resource_kind == "channel":
            assert self.channel is not None
            self.channel = replace(self.channel, control_state=state)
        else:
            assert self.listing is not None
            self.listing = replace(self.listing, control_state=state)
        return action

    def mark_action_unknown(self, action: ManagedAction, error_class: str) -> None:
        self._write("store.mark_action_unknown")
        state = {
            "update": "update_unknown",
            "takedown": "takedown_unknown",
            "resubmit": "resubmit_unknown",
        }[action.operation]
        updated = replace(action, state=state)
        self.actions[(action.resource_kind, action.managed_uid)] = updated
        if action.resource_kind == "channel":
            assert self.channel is not None
            self.channel = replace(
                self.channel,
                control_state=state,
                last_error_class=error_class,
            )
        else:
            assert self.listing is not None
            self.listing = replace(
                self.listing,
                control_state=state,
                last_error_class=error_class,
            )

    def retry_unknown_listing_update(self, action: ManagedAction) -> ManagedAction | None:
        self._write("store.retry_unknown_listing_update")
        key = ("listing", action.managed_uid)
        current = self.actions.get(key)
        if current is None or current.state != "update_unknown":
            return None
        retried = replace(current, state="updating")
        self.actions[key] = retried
        assert self.listing is not None
        self.listing = replace(
            self.listing,
            control_state="updating",
            last_error_class=None,
        )
        return retried

    def retry_unknown_listing_action(self, action: ManagedAction) -> ManagedAction | None:
        self._write("store.retry_unknown_listing_action")
        key = ("listing", action.managed_uid)
        current = self.actions.get(key)
        unknown_state = f"{action.operation}_unknown"
        pending_state = {
            "update": "updating",
            "takedown": "taking_down",
            "resubmit": "resubmitting",
        }[action.operation]
        if current is None or current.state != unknown_state:
            return None
        retried = replace(current, state=pending_state)
        self.actions[key] = retried
        assert self.listing is not None
        self.listing = replace(
            self.listing,
            control_state=pending_state,
            last_error_class=None,
        )
        return retried

    def retry_unknown_channel_update(self, action: ManagedAction) -> ManagedAction | None:
        self._write("store.retry_unknown_channel_update")
        key = ("channel", action.managed_uid)
        current = self.actions.get(key)
        if current is None or current.state != "update_unknown":
            return None
        retried = replace(current, state="updating")
        self.actions[key] = retried
        assert self.channel is not None
        self.channel = replace(
            self.channel,
            control_state="updating",
            last_error_class=None,
        )
        return retried

    def supersede_listing_update(
        self,
        action: ManagedAction,
        observation: dict[str, Any],
    ) -> ManagedListingBinding:
        self._write("store.supersede_listing_update")
        key = ("listing", action.managed_uid)
        current = self.actions.get(key)
        if current is None or current.state not in {"updating", "update_unknown"}:
            raise AssertionError("fixture listing action is not supersedable")
        del self.actions[key]
        assert self.listing is not None
        self.listing = replace(
            self.listing,
            control_state="verified",
            observed_state=deepcopy(dict(observation)),
            last_error_class=None,
        )
        return self.listing

    def complete_channel_action(
        self,
        binding: ManagedChannelBinding,
        action: ManagedAction,
        observation: dict[str, Any],
    ) -> ManagedChannelBinding:
        self._write("store.complete_channel_action")
        del self.actions[("channel", action.managed_uid)]
        assert self.channel is not None
        self.channel = replace(
            self.channel,
            control_state="verified",
            observed_state=deepcopy(dict(observation)),
            last_error_class=None,
        )
        return self.channel

    def complete_listing_action(
        self,
        binding: ManagedListingBinding,
        action: ManagedAction,
        observation: dict[str, Any],
    ) -> ManagedListingBinding:
        self._write("store.complete_listing_action")
        del self.actions[("listing", action.managed_uid)]
        assert self.listing is not None
        self.listing = replace(
            self.listing,
            control_state="verified",
            observed_state=deepcopy(dict(observation)),
            last_error_class=None,
        )
        return self.listing

    def _take_action_id(self) -> int:
        value = self._next_action_id
        self._next_action_id += 1
        return value


class FakeClient:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.channels: list[dict[str, Any]] = []
        self.requests: list[dict[str, Any]] = []
        self.listings: list[dict[str, Any]] = []
        self.mutations: list[str] = []
        self.created_listing_batches: list[list[dict[str, Any]]] = []
        self.updated_listing_payloads: list[dict[str, Any]] = []
        self.updated_channel_payloads: list[dict[str, Any]] = []
        self.fetched_model_credentials: list[tuple[str, str]] = []
        self.applied_supplier_payloads: list[dict[str, Any]] = []
        self.lose_response_once: set[str] = set()
        self.reject_once: set[str] = set()
        self.pending_once: set[str] = set()
        self.supplier = {
            "id": 205,
            "user_id": 9005,
            "name": "Current Supplier",
            "contact_info": "current@example.invalid",
            "description": "current supplier profile",
        }
        self._next_channel_id = 700
        self._next_request_id = 800
        self._next_listing_id = 900

    def supplier_channels(self, user_id: int) -> list[dict[str, Any]]:
        self.events.append("client.supplier_channels")
        return deepcopy(self.channels)

    def supplier_requests(self, user_id: int) -> list[dict[str, Any]]:
        self.events.append("client.supplier_requests")
        return deepcopy(self.requests)

    def supplier_me(self, user_id: int) -> dict[str, Any]:
        self.events.append("client.supplier_me")
        return {
            **deepcopy(self.supplier),
            "id": user_id - 8800,
            "user_id": user_id,
        }

    def fetch_models_via_a6(self, user_id: int, base_url: str, api_key: str) -> list[str]:
        self.events.append("client.fetch_models_via_a6")
        self.fetched_model_credentials.append((base_url, api_key))
        if "fetch_models_via_a6" in self.reject_once:
            self.reject_once.remove("fetch_models_via_a6")
            raise A6AutoError("upstream_models_unavailable")
        return [SUPPORTED_MODEL]

    def apply_supplier(self, user_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        self.events.append("client.apply_supplier")
        self.mutations.append("apply_supplier")
        self.applied_supplier_payloads.append(deepcopy(payload))
        if "apply_supplier" in self.pending_once:
            self.pending_once.remove("apply_supplier")
            raise A6AutoError("supplier_apply_pending_request_exists")
        if "apply_supplier" in self.reject_once:
            self.reject_once.remove("apply_supplier")
            raise A6AutoError("supplier_apply_failed")
        request_id = self._next_request_id
        self._next_request_id += 1
        channel_id = self._next_channel_id
        self._next_channel_id += 1
        channel = deepcopy(dict(payload["channel"]))
        channel.update(
            {
                "id": channel_id,
                "status": 1,
                "metadata": {
                    "token": "nested-token-secret",
                    "kept": [{"api_key": "nested-api-key-secret", "label": "ok"}],
                },
            }
        )
        self.channels.append(channel)
        self.requests.append(
            {
                "id": request_id,
                "name": channel["name"],
            }
        )
        self._lose("apply_supplier")
        return {"request_id": request_id, "supplier_id": 205, "status": "submitted"}

    def supplier_listings(
        self,
        user_id: int,
        *,
        page: int = 1,
        size: int = 1000,
        status: int | None = None,
    ) -> dict[str, Any]:
        self.events.append("client.supplier_listings")
        selected = self.listings
        if status is not None:
            selected = [item for item in selected if str(item.get("status")) == str(status)]
        start = (page - 1) * size
        return {
            "items": deepcopy(selected[start : start + size]),
            "total": len(selected),
            "page": page,
            "size": size,
        }

    def create_supplier_listings(
        self,
        user_id: int,
        listings: list[dict[str, Any]],
    ) -> dict[str, Any]:
        self.events.append("client.create_supplier_listings")
        self.mutations.append("create_supplier_listings")
        if "create_supplier_listings" in self.reject_once:
            self.reject_once.remove("create_supplier_listings")
            raise A6AutoError("supplier_listing_create_failed")
        batch = deepcopy(list(listings))
        self.created_listing_batches.append(batch)
        assert len(batch) == 1
        listing = batch[0]
        listing.update({"id": self._next_listing_id, "status": 2})
        self._next_listing_id += 1
        self.listings.append(listing)
        self._lose("create_supplier_listings")
        return {"items": [{"id": listing["id"]}]}

    def update_supplier_channel(
        self,
        user_id: int,
        channel_id: int,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        self.events.append("client.update_supplier_channel")
        self.mutations.append("update_supplier_channel")
        self.updated_channel_payloads.append(deepcopy(dict(payload)))
        channel = next(
            item
            for item in self.channels
            if item["id"] == channel_id or item.get("binding_id") == channel_id
        )
        safe_payload = deepcopy(dict(payload))
        api_key = str(safe_payload.pop("api_key", ""))
        channel.update(safe_payload)
        if api_key:
            channel["api_key_last4"] = api_key[-4:]
            channel["has_api_key"] = True
            channel.pop("api_key", None)
        self._lose("update_supplier_channel")
        return {"id": channel_id}

    def update_supplier_listing(
        self,
        user_id: int,
        listing_id: int,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        self.events.append("client.update_supplier_listing")
        self.mutations.append("update_supplier_listing")
        self.updated_listing_payloads.append(deepcopy(dict(payload)))
        if "update_supplier_listing" in self.reject_once:
            self.reject_once.remove("update_supplier_listing")
            raise A6AutoError("supplier_listing_update_failed")
        self._by_id(self.listings, listing_id).update(deepcopy(dict(payload)))
        self._lose("update_supplier_listing")
        return {"id": listing_id}

    def takedown_supplier_listing(self, user_id: int, listing_id: int) -> dict[str, Any]:
        self.events.append("client.takedown_supplier_listing")
        self.mutations.append("takedown_supplier_listing")
        if "takedown_supplier_listing" in self.reject_once:
            self.reject_once.remove("takedown_supplier_listing")
            raise A6AutoError("supplier_listing_takedown_failed")
        self._by_id(self.listings, listing_id)["status"] = 0
        self._lose("takedown_supplier_listing")
        return {"id": listing_id, "status": 0}

    def resubmit_supplier_listing(self, user_id: int, listing_id: int) -> dict[str, Any]:
        self.events.append("client.resubmit_supplier_listing")
        self.mutations.append("resubmit_supplier_listing")
        if "resubmit_supplier_listing" in self.reject_once:
            self.reject_once.remove("resubmit_supplier_listing")
            raise A6AutoError("supplier_listing_resubmit_failed")
        self._by_id(self.listings, listing_id)["status"] = 2
        self._lose("resubmit_supplier_listing")
        return {"id": listing_id, "status": 2}

    def _lose(self, operation: str) -> None:
        if operation in self.lose_response_once:
            self.lose_response_once.remove(operation)
            raise RuntimeError(f"fixture lost {operation} response")

    @staticmethod
    def _by_id(items: list[dict[str, Any]], item_id: int) -> dict[str, Any]:
        return next(item for item in items if item["id"] == item_id)


def controller() -> tuple[ManagedListingController, FakeStore, FakeClient, list[str]]:
    events: list[str] = []
    store = FakeStore(events)
    client = FakeClient(events)
    return ManagedListingController(store, lambda _merchant: client), store, client, events


def create_channel_and_listing(
    control: ManagedListingController,
    subject: ManagedMerchant,
) -> tuple[str, str]:
    channel_result = control.ensure_channel(subject, channel_spec(), apply=True)
    listing_result = control.ensure_listing(
        subject,
        channel_result.managed_uid,
        listing_spec(),
        apply=True,
    )
    assert channel_result.state == listing_result.state == "verified"
    return channel_result.managed_uid, listing_result.managed_uid


def test_four_sellers_have_unique_uids_and_duty_switch_does_not_duplicate_resources() -> None:
    subjects = [
        merchant(1),
        merchant(2, roles=("seller", "provider")),
        merchant(3, roles=("seller", "observer")),
        merchant(4),
    ]
    channel_uids = {channel_uid(item) for item in subjects}
    listing_uids = {listing_uid(item, channel_uid(item)) for item in subjects}
    assert len(channel_uids) == len(listing_uids) == 4

    control, store, client, _events = controller()
    first = subjects[1]
    first_channel_uid, first_listing_uid = create_channel_and_listing(control, first)
    mutation_count = len(client.mutations)

    switched = merchant(first.merchant_id, roles=("seller", "observer"))
    channel_result = control.ensure_channel(switched, channel_spec(), apply=True)
    listing_result = control.ensure_listing(
        switched,
        first_channel_uid,
        listing_spec(),
        apply=True,
    )

    assert channel_result.action == listing_result.action == "noop"
    assert channel_result.managed_uid == first_channel_uid
    assert listing_result.managed_uid == first_listing_uid
    assert len(client.mutations) == mutation_count
    assert store.channel is not None and store.listing is not None


@pytest.mark.parametrize(
    "initial_roles,switched_roles,pending_state",
    [
        (("seller", "observer"), ("seller", "provider"), "updating"),
        (("seller", "observer"), ("seller", "provider"), "update_unknown"),
        (("seller", "provider"), ("seller", "observer"), "updating"),
        (("seller", "provider"), ("seller", "observer"), "update_unknown"),
    ],
)
def test_duty_switch_recovers_pending_external_resources_without_recreating(
    initial_roles: tuple[str, ...],
    switched_roles: tuple[str, ...],
    pending_state: str,
) -> None:
    control, store, client, events = controller()
    initial = merchant(roles=initial_roles)
    bound_channel_uid, bound_listing_uid = create_channel_and_listing(control, initial)
    assert store.channel is not None and store.channel.a6_channel_id is not None
    assert store.listing is not None and store.listing.a6_listing_id is not None
    channel_binding_id = store.channel.binding_id
    listing_binding_id = store.listing.binding_id
    channel_id = store.channel.a6_channel_id
    listing_id = store.listing.a6_listing_id
    channel_patch = {"nickname": "pending duty-switch nickname"}
    updated_listing_spec = listing_spec(price_delta=10)
    updated_listing = remote_listing(
        initial,
        bound_channel_uid,
        channel_id=channel_id,
        listing_id=listing_id,
        spec=updated_listing_spec,
    )
    listing_payload = {
        key: value for key, value in updated_listing.items() if key not in {"id", "status"}
    }

    if pending_state == "update_unknown":
        client.lose_response_once.update({"update_supplier_channel", "update_supplier_listing"})
        channel_pending = control.update_channel(
            initial,
            bound_channel_uid,
            channel_patch,
            apply=True,
        )
        listing_pending = control.update_listing(
            initial,
            bound_listing_uid,
            updated_listing_spec,
            apply=True,
        )
        assert channel_pending.state == listing_pending.state == "update_unknown"
    else:
        channel_action = store.begin_action(
            "channel",
            bound_channel_uid,
            "update",
            channel_patch,
            "fixture-pending-channel-update",
        )
        listing_action = store.begin_action(
            "listing",
            bound_listing_uid,
            "update",
            {"payload": listing_payload},
            "fixture-pending-listing-update",
        )
        assert channel_action is not None and listing_action is not None
        FakeClient._by_id(client.channels, channel_id).update(channel_patch)
        FakeClient._by_id(client.listings, listing_id).update(listing_payload)

    assert store.channel is not None and store.channel.control_state == pending_state
    assert store.listing is not None and store.listing.control_state == pending_state
    mutation_count = len(client.mutations)
    store.reset_metrics()
    switched = merchant(initial.merchant_id, roles=switched_roles)

    channel_result = control.ensure_channel(switched, channel_spec(), apply=True)
    listing_result = control.ensure_listing(
        switched,
        bound_channel_uid,
        updated_listing_spec,
        apply=True,
    )

    assert channel_result.action == listing_result.action == "verify"
    assert channel_result.state == listing_result.state == "verified"
    assert len(client.mutations) == mutation_count
    assert client.mutations.count("apply_supplier") == 1
    assert client.mutations.count("create_supplier_listings") == 1
    assert "store.reserve_channel" not in events
    assert "store.reserve_listing" not in events
    assert store.channel is not None
    assert store.channel.binding_id == channel_binding_id
    assert store.channel.a6_channel_id == channel_id
    assert store.listing is not None
    assert store.listing.binding_id == listing_binding_id
    assert store.listing.a6_listing_id == listing_id


@pytest.mark.parametrize(
    "roles",
    [
        ("seller",),
        ("seller", "provider"),
        ("seller", "observer"),
    ],
)
def test_every_seller_duty_can_create_a_new_channel_and_listing(
    roles: tuple[str, ...],
) -> None:
    control, store, client, _events = controller()

    channel_managed_uid, listing_managed_uid = create_channel_and_listing(
        control,
        merchant(roles=roles),
    )

    assert channel_managed_uid == channel_uid(merchant(roles=roles))
    assert listing_managed_uid == listing_uid(merchant(roles=roles), channel_managed_uid)
    assert client.mutations == ["apply_supplier", "create_supplier_listings"]
    assert store.channel is not None and store.channel.control_state == "verified"
    assert store.listing is not None and store.listing.control_state == "verified"


def _legacy_channel(
    channel_id: int,
    *,
    models: str = "gpt-5.5",
    name: str | None = None,
) -> dict[str, Any]:
    channel_name = name or f"legacy-{channel_id}"
    return {
        "id": channel_id,
        "binding_id": channel_id + 10_000,
        "supplier_id": 77,
        "name": channel_name,
        "nickname": f"legacy-{channel_id}",
        "channel_type": 1,
        "status": 1,
        "availability": 1,
        "models": models,
        "description": "legacy channel",
    }


def test_forbidden_channel_name_is_rejected_for_creation_and_adoption() -> None:
    with pytest.raises(ManagedListingError, match="managed_channel_name_forbidden"):
        replace(channel_spec(), channel_name="正常API中转站")

    control, _store, client, _events = controller()
    client.channels.append(_legacy_channel(42, name="正常API中转站"))

    with pytest.raises(ManagedListingError, match="managed_channel_adoption_no_eligible_channel"):
        control.select_existing_channel(merchant())


def test_existing_channel_selection_prefers_model_listing_and_requires_explicit_tie_break() -> None:
    control, _store, client, _events = controller()
    subject = merchant()
    client.channels.extend([_legacy_channel(41), _legacy_channel(42)])
    spec = listing_spec()
    client.listings.append(
        {
            "id": 501,
            "channel_id": 42,
            "own_model": spec.own_model,
            "platform_model": spec.platform_model,
            "charge_type": spec.charge_type,
            "input_price_micros": spec.input_price_micros,
            "output_price_micros": spec.output_price_micros,
            "cache_read_price_micros": spec.cache_read_price_micros,
            "cache_write_price_micros": spec.cache_write_price_micros,
            "status": 2,
        }
    )

    selected = control.select_existing_channel(subject)
    assert selected.channel["id"] == 42
    assert selected.listing is not None and selected.listing["id"] == 501

    client.listings.clear()
    with pytest.raises(ManagedListingError, match="managed_channel_adoption_ambiguous"):
        control.select_existing_channel(subject)
    explicit = control.select_existing_channel(subject, preferred_channel_id=41)
    assert explicit.channel["id"] == 41
    assert explicit.listing is None


def test_forbidden_channel_allows_takedown_but_blocks_update_and_resubmit() -> None:
    control, store, client, _events = controller()
    subject = merchant()
    _channel_managed_uid, bound_listing_uid = create_channel_and_listing(control, subject)
    assert store.channel is not None and store.channel.a6_channel_id is not None
    remote_channel = FakeClient._by_id(client.channels, store.channel.a6_channel_id)
    remote_channel["name"] = "正常API中转站"

    with pytest.raises(ManagedListingError, match="managed_channel_name_forbidden"):
        control.update_listing(
            subject,
            bound_listing_uid,
            listing_spec(price_delta=10),
            apply=True,
        )

    taken_down = control.takedown_listing(subject, bound_listing_uid, apply=True)
    assert taken_down.state == "verified"
    with pytest.raises(ManagedListingError, match="managed_channel_name_forbidden"):
        control.resubmit_listing(subject, bound_listing_uid, apply=True)


def test_existing_channel_adoption_only_appends_models_then_creates_listing() -> None:
    control, store, client, _events = controller()
    subject = merchant()
    client.channels.append(_legacy_channel(42))
    selected = control.select_existing_channel(subject)

    preview = control.ensure_existing_channel(
        subject,
        "seller-primary",
        selected,
        apply=False,
    )
    adopted = control.ensure_existing_channel(
        subject,
        "seller-primary",
        selected,
        apply=True,
    )
    listing = control.ensure_listing_on_existing_channel(
        subject,
        adopted.managed_uid,
        control.select_existing_channel(subject),
        listing_spec(),
        apply=True,
    )

    assert preview.action == "adopt_existing"
    assert adopted.state == "verified" and adopted.external_id == 42
    assert listing.state == "verified"
    assert client.channels[0]["models"] == "gpt-5.5,gpt-5.6-sol"
    assert client.channels[0]["name"] == "legacy-42"
    assert client.channels[0]["description"] == "legacy channel"
    assert "apply_supplier" not in client.mutations
    assert client.mutations.count("update_supplier_channel") == 1
    assert store.channel is not None
    assert store.channel.desired_state["channel_strategy"] == "adopt_existing"


def test_existing_channel_re_adoption_preview_defers_missing_listing() -> None:
    control, store, client, events = controller()
    subject = merchant()
    client.channels.append(_legacy_channel(42, models="gpt-5.6-sol"))
    selected = control.select_existing_channel(subject)
    managed_uid = channel_uid(subject, logical_key="seller-primary")
    store.channel = ManagedChannelBinding(
        binding_id=1,
        managed_uid=managed_uid,
        merchant_id=subject.merchant_id,
        create_idempotency_key="retired-channel-fixture",
        control_state="retired",
        desired_state={},
    )
    store.reset_metrics()

    listing = control.ensure_listing_on_existing_channel(
        subject,
        managed_uid,
        selected,
        listing_spec(),
        apply=False,
    )

    assert listing.action == "create_after_channel_adoption"
    assert listing.state == "planned"
    assert store.write_count == 0
    assert "create_supplier_listings" not in events


def test_existing_channel_re_adoption_can_repair_an_explicit_managed_uid() -> None:
    control, store, client, _events = controller()
    subject = merchant()
    client.channels.append(_legacy_channel(42, models="gpt-5.6-sol"))
    selected = control.select_existing_channel(subject)
    managed_uid = channel_uid(subject, logical_key="historical-channel-binding")
    store.channel = ManagedChannelBinding(
        binding_id=1,
        managed_uid=managed_uid,
        merchant_id=subject.merchant_id,
        create_idempotency_key="retired-channel-fixture",
        control_state="retired",
        desired_state={},
        a6_channel_id=42,
    )

    repaired = control.ensure_existing_channel(
        subject,
        "auto-existing-channel:42",
        selected,
        managed_uid=managed_uid,
        apply=True,
    )

    assert repaired.managed_uid == managed_uid
    assert repaired.state == "verified"
    assert store.channel is not None
    assert store.channel.managed_uid == managed_uid
    assert store.channel.control_state == "verified"


def test_existing_offline_listing_is_resubmitted_put_and_adopted() -> None:
    control, store, client, _events = controller()
    subject = merchant()
    client.channels.append(_legacy_channel(42, models="gpt-5.6-sol"))
    spec = listing_spec(price_delta=25)
    client.listings.append(
        {
            "id": 501,
            "channel_id": 42,
            "own_model": spec.own_model,
            "platform_model": spec.platform_model,
            "charge_type": spec.charge_type,
            "input_price_micros": 1,
            "output_price_micros": 1,
            "cache_read_price_micros": 1,
            "cache_write_price_micros": 1,
            "display_name": "legacy",
            "description": "legacy listing",
            "status": 0,
        }
    )
    selected = control.select_existing_channel(subject)
    adopted = control.ensure_existing_channel(
        subject,
        "seller-primary",
        selected,
        apply=True,
    )

    listing = control.ensure_listing_on_existing_channel(
        subject,
        adopted.managed_uid,
        selected,
        spec,
        apply=True,
    )

    assert listing.state == "verified"
    assert listing.action == "adopt_existing"
    assert client.mutations.count("update_supplier_listing") == 1
    assert set(client.updated_listing_payloads[0]) == {
        "charge_type",
        "input_price_micros",
        "output_price_micros",
        "cache_read_price_micros",
        "cache_write_price_micros",
        "display_name",
        "description",
    }
    assert client.mutations.count("resubmit_supplier_listing") == 1
    assert client.listings[0]["status"] == 2
    assert client.listings[0]["input_price_micros"] == spec.input_price_micros
    assert client.listings[0]["description"] == spec.description
    assert store.listing is not None
    assert store.listing.a6_listing_id == 501


def test_price_only_update_preserves_an_inactive_listing_when_status_is_expected() -> None:
    control, store, client, _events = controller()
    subject = merchant()
    _channel_managed_uid, listing_managed_uid = create_channel_and_listing(control, subject)
    assert store.listing is not None and store.listing.a6_listing_id is not None
    remote = FakeClient._by_id(client.listings, store.listing.a6_listing_id)
    remote["status"] = 0
    client.mutations.clear()
    store.reset_metrics()

    result = control.update_listing(
        subject,
        listing_managed_uid,
        listing_spec(price_delta=17),
        expected_status=0,
        apply=True,
    )

    assert result.state == "verified"
    assert result.action == "update"
    assert remote["status"] == 0
    assert client.mutations == ["update_supplier_listing"]
    assert "resubmit_supplier_listing" not in client.mutations


def test_price_only_update_stops_when_listing_activation_changed() -> None:
    control, store, client, _events = controller()
    subject = merchant()
    _channel_managed_uid, listing_managed_uid = create_channel_and_listing(control, subject)
    assert store.listing is not None and store.listing.a6_listing_id is not None
    remote = FakeClient._by_id(client.listings, store.listing.a6_listing_id)
    remote["status"] = 0
    client.mutations.clear()
    store.reset_metrics()

    with pytest.raises(ManagedListingError, match="managed_listing_activation_changed"):
        control.update_listing(
            subject,
            listing_managed_uid,
            listing_spec(price_delta=17),
            expected_status=2,
            apply=True,
        )

    assert remote["status"] == 0
    assert client.mutations == []
    assert store.actions == {}


def test_existing_offline_listing_is_rolled_back_when_put_fails() -> None:
    control, store, client, _events = controller()
    subject = merchant()
    client.channels.append(_legacy_channel(42, models="gpt-5.6-sol"))
    spec = listing_spec(price_delta=25)
    client.listings.append(
        {
            "id": 501,
            "channel_id": 42,
            "own_model": spec.own_model,
            "platform_model": spec.platform_model,
            "charge_type": spec.charge_type,
            "input_price_micros": 1,
            "output_price_micros": 1,
            "cache_read_price_micros": 1,
            "cache_write_price_micros": 1,
            "display_name": "legacy",
            "description": "legacy listing",
            "status": 0,
        }
    )
    selected = control.select_existing_channel(subject)
    adopted = control.ensure_existing_channel(
        subject,
        "seller-primary",
        selected,
        apply=True,
    )
    client.reject_once.add("update_supplier_listing")

    with pytest.raises(A6AutoError, match="supplier_listing_update_failed"):
        control.ensure_listing_on_existing_channel(
            subject,
            adopted.managed_uid,
            selected,
            spec,
            apply=True,
        )

    assert client.listings[0]["status"] == 0
    assert client.mutations[-3:] == [
        "resubmit_supplier_listing",
        "update_supplier_listing",
        "takedown_supplier_listing",
    ]
    assert store.listing is None


@pytest.mark.parametrize(
    "roles",
    [
        ("provider",),
        ("seller", "provider", "observer"),
        ("seller", "unknown"),
        ("seller", "probe"),
        ("observer", "seller"),
        ("seller", "seller"),
    ],
)
def test_merchant_requires_seller_base_role_and_at_most_one_known_duty(
    roles: tuple[str, ...],
) -> None:
    with pytest.raises(ManagedListingError, match="managed_listing_merchant_invalid"):
        merchant(5, roles=roles)


def test_channel_dry_run_has_no_store_writes_or_remote_mutations() -> None:
    control, store, client, events = controller()

    result = control.ensure_channel(merchant(), channel_spec(), apply=False)

    assert result.action == "create"
    assert result.state == "planned"
    assert result.applied is False
    assert store.write_count == 0
    assert client.mutations == []
    assert events == [
        "store.get_channel",
        "client.supplier_channels",
        "client.supplier_requests",
    ]


def test_listing_and_action_dry_runs_have_no_store_writes_or_remote_mutations() -> None:
    control, store, client, _events = controller()
    bound_channel_uid, bound_listing_uid = create_channel_and_listing(control, merchant())
    store.reset_metrics()
    mutation_count = len(client.mutations)

    listing_result = control.ensure_listing(
        merchant(),
        bound_channel_uid,
        listing_spec(),
        apply=False,
    )
    update_result = control.update_listing(
        merchant(),
        bound_listing_uid,
        listing_spec(price_delta=10),
        apply=False,
    )

    assert listing_result.action == "noop"
    assert update_result.action == "update"
    assert store.write_count == 0
    assert len(client.mutations) == mutation_count


def test_human_listing_description_can_change_without_losing_stable_binding() -> None:
    control, store, client, _events = controller()
    _channel_uid, bound_listing_uid = create_channel_and_listing(control, merchant())
    assert store.listing is not None
    assert client.listings[0]["description"] == "Team 测试配额来源"

    client.listings[0]["description"] = "Team 来源由运营人员调整"
    updated = control.update_listing(
        merchant(),
        bound_listing_uid,
        replace(listing_spec(price_delta=10), description="Pro 账户来源"),
        apply=True,
    )

    assert updated.state == "verified"
    assert client.listings[0]["description"] == "Pro 账户来源"


def test_bound_listing_identity_change_blocks_update_even_when_description_matches() -> None:
    control, _store, client, _events = controller()
    _channel_uid, bound_listing_uid = create_channel_and_listing(control, merchant())
    client.listings[0]["channel_id"] += 1

    with pytest.raises(
        ManagedListingError,
        match="managed_listing_readback_missing_or_changed",
    ):
        control.update_listing(
            merchant(),
            bound_listing_uid,
            listing_spec(price_delta=10),
            apply=True,
        )


@pytest.mark.parametrize(
    "description",
    ["", " " * 4, "x" * 501, "Pro 订阅来源，面向复杂分析任务"],
)
def test_listing_description_is_required_and_bounded(description: str) -> None:
    with pytest.raises(ManagedListingError, match="managed_listing_description_invalid"):
        replace(listing_spec(), description=description)


def test_old_exact_channel_and_listing_matches_are_not_adopted() -> None:
    control, store, client, _events = controller()
    subject = merchant()
    old_channel_id = 42
    client.channels.append(remote_channel(subject, channel_id=old_channel_id))
    client.requests.append(
        {
            "id": 43,
            "channel_name": client.channels[0]["name"],
        }
    )

    channel_result = control.ensure_channel(subject, channel_spec(), apply=True)

    assert channel_result.external_id == 700
    assert channel_result.external_id != old_channel_id
    assert store.channel is not None
    assert store.channel.desired_state["baseline_channel_ids"] == [old_channel_id]
    old_listing_id = 52
    client.listings.append(
        remote_listing(
            subject,
            channel_result.managed_uid,
            channel_id=int(channel_result.external_id or 0),
            listing_id=old_listing_id,
        )
    )

    listing_result = control.ensure_listing(
        subject,
        channel_result.managed_uid,
        listing_spec(),
        apply=True,
    )

    assert listing_result.external_id == 900
    assert listing_result.external_id != old_listing_id
    assert store.listing is not None
    assert store.listing.desired_state["baseline_listing_ids"] == [old_listing_id]


def test_channel_create_order_redacts_secrets_and_verifies_both_readbacks() -> None:
    control, store, client, events = controller()

    result = control.ensure_channel(merchant(), channel_spec(), apply=True)

    assert result.state == "verified"
    assert events == [
        "store.get_channel",
        "client.supplier_channels",
        "client.supplier_requests",
        "store.reserve_channel",
        "store.claim_channel_create",
        "client.supplier_me",
        "client.apply_supplier",
        "store.record_channel_post",
        "client.supplier_channels",
        "client.supplier_requests",
        "store.verify_channel",
    ]
    assert store.channel is not None and store.channel.observed_state is not None
    persisted = json.dumps(store.channel.desired_state, sort_keys=True)
    observed = json.dumps(store.channel.observed_state, sort_keys=True)
    assert API_KEY not in repr(channel_spec())
    assert API_KEY not in persisted
    assert hashlib.sha256(API_KEY.encode()).hexdigest() in persisted
    assert client.applied_supplier_payloads[0]["supplier"] == {
        "name": client.supplier["name"],
        "contact_info": client.supplier["contact_info"],
        "description": client.supplier["description"],
    }
    assert "current@example.invalid" not in persisted
    assert "nested-token-secret" not in observed
    assert "nested-api-key-secret" not in observed
    assert store.channel.observed_state["metadata"]["kept"][0]["label"] == "ok"


def test_listing_create_uses_single_element_array_and_strict_readback_order() -> None:
    control, store, client, events = controller()
    subject = merchant()
    bound_channel_uid = control.ensure_channel(subject, channel_spec(), apply=True).managed_uid
    store.reset_metrics()

    result = control.ensure_listing(
        subject,
        bound_channel_uid,
        listing_spec(),
        apply=True,
    )

    assert result.state == "verified"
    assert len(client.created_listing_batches) == 1
    assert isinstance(client.created_listing_batches[0], list)
    assert len(client.created_listing_batches[0]) == 1
    assert events == [
        "store.get_channel",
        "client.supplier_listings",
        "store.get_listing",
        "store.reserve_listing",
        "store.claim_listing_create",
        "client.create_supplier_listings",
        "store.record_listing_post",
        "client.supplier_listings",
        "store.verify_listing",
    ]


def test_channel_create_unknown_only_reads_back_on_retry() -> None:
    control, store, client, events = controller()
    client.lose_response_once.add("apply_supplier")

    first = control.ensure_channel(merchant(), channel_spec(), apply=True)

    assert first.state == "create_unknown"
    assert events == [
        "store.get_channel",
        "client.supplier_channels",
        "client.supplier_requests",
        "store.reserve_channel",
        "store.claim_channel_create",
        "client.supplier_me",
        "client.apply_supplier",
        "store.mark_channel_create_unknown",
    ]
    events.clear()

    second = control.ensure_channel(merchant(), channel_spec(), apply=True)

    assert second.state == "verified"
    assert client.mutations.count("apply_supplier") == 1
    assert events == [
        "store.get_channel",
        "client.supplier_channels",
        "client.supplier_requests",
        "store.verify_channel",
    ]


def test_channel_known_rejection_requires_explicit_recovery() -> None:
    control, store, client, events = controller()
    client.reject_once.add("apply_supplier")

    first = control.ensure_channel(merchant(), channel_spec(), apply=True)

    assert first.state == "create_unknown"
    assert first.error_class == "managed_channel_create_recovery_required"
    assert store.channel is not None and store.channel.create_action_id is not None
    assert store.channel.last_error_class == "supplier_apply_failed"
    assert events[-2:] == ["client.apply_supplier", "store.mark_channel_create_unknown"]
    events.clear()

    second = control.ensure_channel(merchant(), channel_spec(), apply=True)

    assert second.action == "reconcile_pending"
    assert second.state == "create_unknown"
    assert client.mutations.count("apply_supplier") == 1
    assert "store.claim_channel_create" not in events


def test_channel_unresolved_error_binding_is_blocked_from_posting() -> None:
    control, store, client, events = controller()
    client.reject_once.add("apply_supplier")
    control.ensure_channel(merchant(), channel_spec(), apply=True)
    assert store.channel is not None
    store.channel = replace(
        store.channel,
        control_state="error",
        create_action_id=None,
        last_error_class="supplier_apply_failed",
    )
    events.clear()

    result = control.ensure_channel(merchant(), channel_spec(), apply=True)

    assert result.action == "blocked"
    assert result.applied is False
    assert result.error_class == "managed_channel_create_recovery_required"
    assert client.mutations.count("apply_supplier") == 1
    assert "store.claim_channel_create" not in events


def test_pending_supplier_request_waits_for_readback_without_reposting() -> None:
    control, store, client, events = controller()
    client.pending_once.add("apply_supplier")

    first = control.ensure_channel(merchant(), channel_spec(), apply=True)

    assert first.action == "reconcile_pending"
    assert first.state == "verifying"
    assert client.mutations.count("apply_supplier") == 1
    assert store.channel is not None
    assert store.channel.observed_state == {"status": "pending_request_exists"}
    events.clear()

    second = control.ensure_channel(merchant(), channel_spec(), apply=True)

    assert second.action == "reconcile_pending"
    assert client.mutations.count("apply_supplier") == 1


def test_channel_create_unknown_requires_explicit_absence_resolution_before_retry() -> None:
    control, store, client, events = controller()
    client.reject_once.add("apply_supplier")
    first = control.ensure_channel(merchant(), channel_spec(), apply=True)
    assert first.state == "create_unknown"
    client.channels.clear()
    client.requests.clear()
    events.clear()

    pending = control.ensure_channel(merchant(), channel_spec(), apply=True)
    assert pending.action == "reconcile_pending"
    assert client.mutations.count("apply_supplier") == 1

    preview = control.resolve_channel_create_unknown(merchant(), channel_spec(), apply=False)
    assert preview.action == "resolve_absent"
    assert preview.applied is False
    resolved = control.resolve_channel_create_unknown(merchant(), channel_spec(), apply=True)
    assert resolved.state == "error"

    retried = control.ensure_channel(merchant(), channel_spec(), apply=True)
    assert retried.state == "verified"
    assert client.mutations.count("apply_supplier") == 2


@pytest.mark.parametrize(
    "error_class",
    ["a6_transport_failed", "a6_response_invalid", "managed_channel_readback_failed"],
)
def test_channel_indeterminate_unknown_cannot_be_resolved_absent(
    error_class: str,
) -> None:
    control, store, client, _events = controller()
    client.lose_response_once.add("apply_supplier")
    control.ensure_channel(merchant(), channel_spec(), apply=True)
    assert store.channel is not None
    store.channel = replace(store.channel, last_error_class=error_class)
    client.channels.clear()
    client.requests.clear()

    with pytest.raises(
        ManagedListingError,
        match="managed_channel_create_unknown_not_recoverable",
    ):
        control.resolve_channel_create_unknown(merchant(), channel_spec(), apply=True)

    assert client.mutations.count("apply_supplier") == 1


def test_channel_unknown_resolution_stops_when_pending_request_still_exists() -> None:
    control, store, client, _events = controller()
    client.lose_response_once.add("apply_supplier")
    first = control.ensure_channel(merchant(), channel_spec(), apply=True)
    assert first.state == "create_unknown"
    client.channels.clear()
    assert store.channel is not None
    store.channel = replace(store.channel, last_error_class="supplier_apply_failed")

    with pytest.raises(
        ManagedListingError,
        match="managed_channel_create_unknown_remote_candidate",
    ):
        control.resolve_channel_create_unknown(merchant(), channel_spec(), apply=True)

    assert client.mutations.count("apply_supplier") == 1


def test_listing_create_unknown_only_reads_back_on_retry() -> None:
    control, store, client, events = controller()
    subject = merchant()
    bound_channel_uid = control.ensure_channel(subject, channel_spec(), apply=True).managed_uid
    store.reset_metrics()
    client.lose_response_once.add("create_supplier_listings")

    first = control.ensure_listing(subject, bound_channel_uid, listing_spec(), apply=True)

    assert first.state == "create_unknown"
    assert events == [
        "store.get_channel",
        "client.supplier_listings",
        "store.get_listing",
        "store.reserve_listing",
        "store.claim_listing_create",
        "client.create_supplier_listings",
        "store.mark_listing_create_unknown",
    ]
    events.clear()

    second = control.ensure_listing(subject, bound_channel_uid, listing_spec(), apply=True)

    assert second.state == "verified"
    assert client.mutations.count("create_supplier_listings") == 1
    assert events == [
        "store.get_channel",
        "client.supplier_listings",
        "store.get_listing",
        "store.verify_listing",
    ]


def test_channel_update_intent_mutation_readback_order() -> None:
    control, store, client, events = controller()
    subject = merchant()
    bound_channel_uid = control.ensure_channel(subject, channel_spec(), apply=True).managed_uid
    store.reset_metrics()

    result = control.update_channel(
        subject,
        bound_channel_uid,
        {"nickname": "new managed nickname"},
        apply=True,
    )

    assert result.state == "verified"
    assert events == [
        "store.get_channel",
        "client.supplier_channels",
        "client.supplier_requests",
        "store.get_pending_action",
        "store.begin_update",
        "client.update_supplier_channel",
        "client.supplier_channels",
        "store.complete_channel_action",
    ]
    events.clear()
    assert control.ensure_channel(subject, channel_spec(), apply=True).action == "noop"


def test_channel_update_unknown_is_not_resent_after_remote_success() -> None:
    control, store, client, events = controller()
    subject = merchant()
    bound_channel_uid = control.ensure_channel(subject, channel_spec(), apply=True).managed_uid
    store.reset_metrics()
    client.lose_response_once.add("update_supplier_channel")
    patch = {"nickname": "response-lost nickname"}

    first = control.update_channel(subject, bound_channel_uid, patch, apply=True)

    assert first.state == "update_unknown"
    assert events == [
        "store.get_channel",
        "client.supplier_channels",
        "client.supplier_requests",
        "store.get_pending_action",
        "store.begin_update",
        "client.update_supplier_channel",
        "store.mark_action_unknown",
    ]
    events.clear()

    second = control.update_channel(subject, bound_channel_uid, patch, apply=True)

    assert second.state == "verified"
    assert client.mutations.count("update_supplier_channel") == 1
    assert events == [
        "store.get_channel",
        "client.supplier_channels",
        "client.supplier_requests",
        "store.get_pending_action",
        "store.retry_unknown_channel_update",
        "store.complete_channel_action",
    ]


def test_channel_upstream_noops_when_url_and_private_key_match() -> None:
    control, store, client, events = controller()
    subject = merchant()
    spec = channel_spec()
    managed_uid = control.ensure_channel(subject, spec, apply=True).managed_uid
    store.reset_metrics()

    result = control.ensure_channel_upstream(
        subject,
        managed_uid,
        spec,
        apply=True,
    )

    assert result.action == "noop"
    assert result.state == "verified"
    assert client.fetched_model_credentials == []
    assert client.updated_channel_payloads == []
    assert events == [
        "store.get_channel",
        "client.supplier_channels",
        "client.supplier_requests",
        "store.get_pending_action",
    ]


@pytest.mark.parametrize(
    ("base_url", "key_last4"),
    [
        ("https://placeholder.invalid/v1", API_KEY[-4:]),
        ("https://fixture.invalid/v1", "xxxx"),
    ],
)
def test_channel_upstream_repairs_placeholder_url_or_wrong_key(
    base_url: str,
    key_last4: str,
) -> None:
    control, store, client, events = controller()
    subject = merchant()
    spec = channel_spec()
    managed_uid = control.ensure_channel(subject, spec, apply=True).managed_uid
    assert store.channel is not None and store.channel.a6_channel_id is not None
    remote = FakeClient._by_id(client.channels, store.channel.a6_channel_id)
    remote.pop("api_key", None)
    remote.update(
        {
            "base_url": base_url,
            "api_key_last4": key_last4,
            "has_api_key": True,
        }
    )
    store.reset_metrics()

    result = control.ensure_channel_upstream(
        subject,
        managed_uid,
        spec,
        apply=True,
    )

    assert result.action == "update"
    assert result.state == "verified"
    assert client.fetched_model_credentials == [(spec.base_url, API_KEY)]
    assert client.updated_channel_payloads == [
        {"base_url": spec.base_url, "api_key": API_KEY}
    ]
    assert remote["base_url"] == spec.base_url
    assert remote["api_key_last4"] == API_KEY[-4:]
    assert remote["has_api_key"] is True
    assert store.channel is not None and store.channel.observed_state is not None
    audit = repr(store.channel.observed_state)
    assert spec.base_url not in audit
    assert API_KEY not in audit
    assert hashlib.sha256(spec.base_url.encode()).hexdigest() in audit
    assert events == [
        "store.get_channel",
        "client.supplier_channels",
        "client.supplier_requests",
        "store.get_pending_action",
        "client.fetch_models_via_a6",
        "store.begin_update",
        "client.update_supplier_channel",
        "client.supplier_channels",
        "store.complete_channel_action",
    ]


def test_channel_upstream_requires_model_probe_before_patch() -> None:
    control, store, client, _events = controller()
    subject = merchant()
    spec = channel_spec()
    managed_uid = control.ensure_channel(subject, spec, apply=True).managed_uid
    assert store.channel is not None and store.channel.a6_channel_id is not None
    remote = FakeClient._by_id(client.channels, store.channel.a6_channel_id)
    remote["base_url"] = "https://placeholder.invalid/v1"
    client.fetch_models_via_a6 = lambda *_args: ["some-other-model"]  # type: ignore[method-assign]
    store.reset_metrics()

    with pytest.raises(ManagedListingError, match="managed_channel_upstream_model_missing"):
        control.ensure_channel_upstream(subject, managed_uid, spec, apply=True)

    assert client.updated_channel_payloads == []
    assert store.actions == {}
    assert store.write_count == 0


def test_channel_upstream_response_loss_completes_from_readback_without_resend() -> None:
    control, store, client, events = controller()
    subject = merchant()
    spec = channel_spec()
    managed_uid = control.ensure_channel(subject, spec, apply=True).managed_uid
    assert store.channel is not None and store.channel.a6_channel_id is not None
    remote = FakeClient._by_id(client.channels, store.channel.a6_channel_id)
    remote["base_url"] = "https://placeholder.invalid/v1"
    client.lose_response_once.add("update_supplier_channel")
    store.reset_metrics()

    first = control.ensure_channel_upstream(subject, managed_uid, spec, apply=True)

    assert first.state == "update_unknown"
    assert client.mutations.count("update_supplier_channel") == 1
    pending = store.actions[("channel", managed_uid)]
    pending_audit = repr(pending.desired_state)
    assert spec.base_url not in pending_audit
    assert API_KEY not in pending_audit
    assert hashlib.sha256(spec.base_url.encode()).hexdigest() in pending_audit
    events.clear()

    second = control.ensure_channel_upstream(subject, managed_uid, spec, apply=True)

    assert second.state == "verified"
    assert client.mutations.count("update_supplier_channel") == 1
    assert client.fetched_model_credentials == [(spec.base_url, API_KEY)]
    assert events == [
        "store.get_channel",
        "client.supplier_channels",
        "client.supplier_requests",
        "store.get_pending_action",
        "store.retry_unknown_channel_update",
        "store.complete_channel_action",
    ]


def test_adopted_channel_keeps_unknown_upstream_action_instead_of_readopting() -> None:
    control, store, client, events = controller()
    subject = merchant()
    spec = channel_spec()
    client.channels = [
        {
            "id": 42,
            "binding_id": 10_042,
            "supplier_id": subject.supplier_id,
            "name": "existing-channel",
            "nickname": "existing-channel",
            "channel_type": 1,
            "status": 1,
            "availability": 1,
            "models": SUPPORTED_MODEL,
            "description": "existing channel",
            "base_url": "https://placeholder.invalid/v1",
            "api_key_last4": "xxxx",
            "has_api_key": True,
        }
    ]
    selection = control.select_existing_channel(subject)
    adopted = control.ensure_existing_channel(
        subject,
        spec.logical_key,
        selection,
        apply=True,
    )
    assert adopted.state == "verified"
    client.lose_response_once.add("update_supplier_channel")

    first = control.ensure_channel_upstream(
        subject,
        adopted.managed_uid,
        spec,
        apply=True,
    )
    assert first.state == "update_unknown"
    events.clear()

    refreshed_selection = control.select_existing_channel(subject)
    existing = control.ensure_existing_channel(
        subject,
        spec.logical_key,
        refreshed_selection,
        apply=True,
    )
    recovered = control.ensure_channel_upstream(
        subject,
        adopted.managed_uid,
        spec,
        apply=True,
    )

    assert existing.action == "noop"
    assert recovered.state == "verified"
    assert client.mutations.count("update_supplier_channel") == 1
    assert events.count("store.adopt_channel") == 0
    assert events.count("store.retry_unknown_channel_update") == 1


@pytest.mark.parametrize("operation", ["update", "takedown", "resubmit"])
def test_listing_action_intent_mutation_readback_order(operation: str) -> None:
    control, store, client, events = controller()
    subject = merchant()
    _channel_managed_uid, bound_listing_uid = create_channel_and_listing(control, subject)
    assert store.listing is not None and store.listing.a6_listing_id is not None
    if operation == "resubmit":
        FakeClient._by_id(client.listings, store.listing.a6_listing_id)["status"] = 0
    store.reset_metrics()

    if operation == "update":
        result = control.update_listing(
            subject,
            bound_listing_uid,
            listing_spec(price_delta=10),
            apply=True,
        )
        mutation = "update_supplier_listing"
    elif operation == "takedown":
        result = control.takedown_listing(subject, bound_listing_uid, apply=True)
        mutation = "takedown_supplier_listing"
    else:
        result = control.resubmit_listing(subject, bound_listing_uid, apply=True)
        mutation = "resubmit_supplier_listing"

    assert result.state == "verified"
    expected = [
        "store.get_listing",
        "client.supplier_listings",
    ]
    if operation != "takedown":
        expected.append("client.supplier_channels")
    expected.extend(
        [
            "store.get_pending_action",
            f"store.begin_{operation}",
            f"client.{mutation}",
            "client.supplier_listings",
            "store.complete_listing_action",
        ]
    )
    assert events == expected


@pytest.mark.parametrize("operation", ["update", "takedown", "resubmit"])
def test_listing_action_unknown_is_not_resent_after_remote_success(operation: str) -> None:
    control, store, client, events = controller()
    subject = merchant()
    _channel_managed_uid, bound_listing_uid = create_channel_and_listing(control, subject)
    assert store.listing is not None and store.listing.a6_listing_id is not None
    if operation == "resubmit":
        FakeClient._by_id(client.listings, store.listing.a6_listing_id)["status"] = 0
    mutation = f"{operation}_supplier_listing"
    if operation == "takedown":
        mutation = "takedown_supplier_listing"
    client.lose_response_once.add(mutation)
    store.reset_metrics()

    if operation == "update":
        call = lambda: control.update_listing(  # noqa: E731
            subject,
            bound_listing_uid,
            listing_spec(price_delta=10),
            apply=True,
        )
    elif operation == "takedown":
        call = lambda: control.takedown_listing(  # noqa: E731
            subject, bound_listing_uid, apply=True
        )
    else:
        call = lambda: control.resubmit_listing(  # noqa: E731
            subject, bound_listing_uid, apply=True
        )

    first = call()

    assert first.state == f"{operation}_unknown"
    assert events[-3:] == [
        f"store.begin_{operation}",
        f"client.{mutation}",
        "store.mark_action_unknown",
    ]
    events.clear()

    second = call()

    assert second.state == "verified"
    assert client.mutations.count(mutation) == 1
    expected = [
        "store.get_listing",
        "client.supplier_listings",
    ]
    if operation != "takedown":
        expected.append("client.supplier_channels")
    expected.append("store.get_pending_action")
    if operation == "update":
        expected.append("store.retry_unknown_listing_update")
    expected.append("store.complete_listing_action")
    assert events == expected


@pytest.mark.parametrize("operation", ["takedown", "resubmit"])
def test_listing_activation_action_unknown_retries_when_remote_did_not_change(
    operation: str,
) -> None:
    control, store, client, events = controller()
    subject = merchant()
    _channel_uid, listing_uid = create_channel_and_listing(control, subject)
    assert store.listing is not None and store.listing.a6_listing_id is not None
    mutation = f"{operation}_supplier_listing"
    if operation == "resubmit":
        FakeClient._by_id(client.listings, store.listing.a6_listing_id)["status"] = 0
        call = lambda: control.resubmit_listing(  # noqa: E731
            subject,
            listing_uid,
            apply=True,
        )
    else:
        call = lambda: control.takedown_listing(  # noqa: E731
            subject,
            listing_uid,
            apply=True,
        )
    client.reject_once.add(mutation)
    store.reset_metrics()

    first = call()
    second = call()

    assert first.state == f"{operation}_unknown"
    assert second.state == "verified"
    assert client.mutations.count(mutation) == 2
    assert events.count("store.retry_unknown_listing_action") == 1


def test_failed_listing_update_retries_same_audited_action() -> None:
    control, store, client, events = controller()
    subject = merchant()
    _channel_uid, listing_managed_uid = create_channel_and_listing(control, subject)
    desired = listing_spec(price_delta=10)
    client.reject_once.add("update_supplier_listing")
    store.reset_metrics()

    first = control.update_listing(
        subject,
        listing_managed_uid,
        desired,
        apply=True,
    )
    preview = control.update_listing(
        subject,
        listing_managed_uid,
        desired,
        apply=False,
    )
    retried = control.update_listing(
        subject,
        listing_managed_uid,
        desired,
        apply=True,
    )

    assert first.state == "update_unknown"
    assert preview.action == "retry_update"
    assert preview.applied is False
    assert retried.state == "verified"
    assert client.mutations.count("update_supplier_listing") == 2
    assert "store.retry_unknown_listing_update" in events
    assert store.write_count > 0


def test_failed_listing_update_supersedes_old_target_then_applies_current_target() -> None:
    control, store, client, events = controller()
    subject = merchant()
    _channel_uid, listing_managed_uid = create_channel_and_listing(control, subject)
    old_target = listing_spec(price_delta=10)
    current_target = listing_spec(price_delta=20)
    client.reject_once.add("update_supplier_listing")
    store.reset_metrics()

    failed = control.update_listing(
        subject,
        listing_managed_uid,
        old_target,
        apply=True,
    )
    recovered = control.update_listing(
        subject,
        listing_managed_uid,
        current_target,
        apply=True,
    )

    assert failed.state == "update_unknown"
    assert recovered.state == "verified"
    assert client.mutations.count("update_supplier_listing") == 2
    assert store.listing is not None and store.listing.a6_listing_id is not None
    remote = FakeClient._by_id(client.listings, store.listing.a6_listing_id)
    assert remote["input_price_micros"] == current_target.input_price_micros
    assert remote["output_price_micros"] == current_target.output_price_micros
    assert events.count("store.supersede_listing_update") == 1
    assert events.count("store.retry_unknown_listing_update") == 0
    assert events.count("store.complete_listing_action") == 1


def test_listing_price_can_return_to_a_previously_completed_target() -> None:
    control, store, client, _events = controller()
    subject = merchant()
    _channel_uid, listing_managed_uid = create_channel_and_listing(control, subject)

    for price_delta in (10, 20, 10):
        result = control.update_listing(
            subject,
            listing_managed_uid,
            listing_spec(price_delta=price_delta),
            apply=True,
        )
        assert result.state == "verified"

    assert client.mutations.count("update_supplier_listing") == 3
    listing_action_keys = [key for key in store.action_keys if ":listing:" in key]
    assert len(listing_action_keys) == 3
    assert len(set(listing_action_keys)) == 3


def test_adopted_channel_ownership_uses_channel_request_and_pending_guard() -> None:
    control, store, client, _events = controller()
    subject = merchant()
    managed_uid = control.ensure_channel(subject, channel_spec(), apply=True).managed_uid
    assert store.channel is not None
    channel = {
        "id": 700,
        "binding_id": 1700,
        "supplier_id": 205,
        "name": "legacy-channel",
        "nickname": "legacy-channel",
        "channel_type": 1,
        "status": 1,
        "availability": 1,
        "models": "gpt-5.6-sol",
        "description": managed_resource_signature("channel", managed_uid),
    }
    request = {
        "id": 800,
        "native_channel_id": 700,
        "supplier_id": 205,
        "name": "legacy-channel",
        "status": 1,
        "base_url": "https://fixture.invalid/v1",
        "models": "gpt-5.6-sol",
        "test_model": "gpt-5.6-sol",
        "description": managed_resource_signature("channel", managed_uid),
        "api_key_last4": API_KEY[-4:],
    }
    guard = {
        "id": 801,
        "native_channel_id": 0,
        "supplier_id": 205,
        "name": managed_channel_name(managed_uid),
        "status": 0,
    }
    ownership = {"channel": channel, "request": request, "request_guards": [guard]}
    ownership["fingerprint"] = hashlib.sha256(
        json.dumps(ownership, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    store.channel = replace(
        store.channel,
        desired_state={**store.channel.desired_state, "adoption": ownership},
    )
    client.channels = [deepcopy(channel)]
    client.requests = [deepcopy(request), deepcopy(guard)]

    result = control.ensure_channel(subject, channel_spec(), apply=False)

    assert result.action == "noop"
    client.requests[0]["api_key_last4"] = "xxxx"
    changed = control.ensure_channel(subject, channel_spec(), apply=False)
    assert changed.action == "error"
    assert changed.error_class == "managed_channel_readback_missing_or_changed"


def test_adopted_channel_ownership_rejects_approved_pending_guard() -> None:
    control, store, client, _events = controller()
    subject = merchant()
    managed_uid = control.ensure_channel(subject, channel_spec(), apply=True).managed_uid
    assert store.channel is not None
    channel = {
        "id": 700,
        "name": "legacy-channel",
        "description": managed_resource_signature("channel", managed_uid),
    }
    request = {"id": 800, "native_channel_id": 700, "api_key_last4": API_KEY[-4:]}
    guard = {"id": 801, "native_channel_id": 0, "status": 0}
    ownership = {"channel": channel, "request": request, "request_guards": [guard]}
    ownership["fingerprint"] = hashlib.sha256(
        json.dumps(ownership, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    store.channel = replace(
        store.channel,
        desired_state={**store.channel.desired_state, "adoption": ownership},
    )
    client.channels = [deepcopy(channel)]
    client.requests = [deepcopy(request), {**guard, "native_channel_id": 701, "status": 1}]

    result = control.ensure_channel(subject, channel_spec(), apply=False)

    assert result.action == "error"
    assert result.error_class == "managed_channel_readback_missing_or_changed"
