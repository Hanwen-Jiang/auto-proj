from __future__ import annotations

import base64
import hashlib
import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from .channel_policy import is_forbidden_channel_name
from .errors import A6AutoError

SUPPORTED_MODEL = "gpt-5.6-sol"
SUPPORTED_CHARGE_TYPE = "per_token"
MAX_LISTING_DISPLAY_NAME_LENGTH = 50
MAX_LISTING_DESCRIPTION_LENGTH = 500
_LOGICAL_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_CHANNEL_MANAGED_UID = re.compile(
    r"^a6-managed:channel:m[1-9][0-9]*:[0-9a-f]{20}$"
)
_SOURCE_POOL_LABEL = re.compile(r"^[A-Za-z0-9\u3400-\u9fff+ /&\u00b7_，、-]{2,64}$")
_SOURCE_POOL_MARKERS = ("team", "k12", "bugteam", "plus", "pro")
_SOURCE_POOL_FORBIDDEN = (
    "适合",
    "面向",
    "任务",
    "调用",
    "分析",
    "生成",
    "高并发",
    "低延迟",
    "稳定线路",
    "早高峰",
    "夜间",
    "日间",
    "晨间",
    "午后",
    "周末",
    "工作日",
    "路由",
    "中继",
    "接口",
    "节点",
    "直连",
    "回程",
    "高频",
    "低频",
    "快速",
    "高缓存",
)
_PENDING_CREATE_STATES = frozenset({"creating", "create_unknown", "verifying"})
_PENDING_ACTION_STATES = frozenset(
    {
        "updating",
        "update_unknown",
        "taking_down",
        "takedown_unknown",
        "resubmitting",
        "resubmit_unknown",
    }
)
_RECOVERABLE_CHANNEL_CREATE_UNKNOWN_ERRORS = frozenset({"supplier_apply_failed"})
_CONFIRMED_ABSENT_CREATE_ERROR = "managed_channel_create_confirmed_absent"


class ManagedListingError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def is_source_only_listing_description(value: object) -> bool:
    """Return whether a Listing description is a short source/pool label."""
    if not isinstance(value, str):
        return False
    description = value.strip()
    lowered = description.casefold()
    return bool(
        description
        and len(description) <= MAX_LISTING_DESCRIPTION_LENGTH
        and _SOURCE_POOL_LABEL.fullmatch(description)
        and any(marker in lowered for marker in _SOURCE_POOL_MARKERS)
        and not any(term in description for term in _SOURCE_POOL_FORBIDDEN)
    )


@dataclass(frozen=True)
class ManagedMerchant:
    merchant_id: int
    a6_user_id: int
    supplier_id: int | None
    roles: tuple[str, ...] = ("seller",)

    def __post_init__(self) -> None:
        roles = tuple(str(item) for item in self.roles)
        valid_roles = roles == ("seller",) or (
            len(roles) == 2 and roles[0] == "seller" and roles[1] in {"provider", "observer"}
        )
        if self.merchant_id < 1 or self.a6_user_id < 1 or not valid_roles:
            raise ManagedListingError("managed_listing_merchant_invalid")
        object.__setattr__(self, "roles", roles)


@dataclass(frozen=True)
class ManagedChannelSpec:
    logical_key: str
    supplier_name: str
    contact_info: str
    channel_name: str
    channel_nickname: str
    base_url: str
    api_key: str = field(repr=False)
    supplier_description: str = ""
    channel_description: str = ""
    models: tuple[str, ...] = (SUPPORTED_MODEL,)
    channel_type: int = 1

    def __post_init__(self) -> None:
        _validate_logical_key(self.logical_key)
        required = (
            self.supplier_name,
            self.contact_info,
            self.channel_name,
            self.channel_nickname,
            self.base_url,
            self.api_key,
        )
        if any(not str(value).strip() for value in required):
            raise ManagedListingError("managed_channel_spec_invalid")
        if is_forbidden_channel_name(self.channel_name):
            raise ManagedListingError("managed_channel_name_forbidden")
        models = tuple(
            dict.fromkeys(str(item).strip() for item in self.models if str(item).strip())
        )
        if models != (SUPPORTED_MODEL,) or self.channel_type != 1:
            raise ManagedListingError("managed_channel_spec_unsupported")
        object.__setattr__(self, "models", models)


@dataclass(frozen=True)
class ManagedListingSpec:
    logical_key: str
    input_price_micros: int
    output_price_micros: int
    cache_read_price_micros: int
    cache_write_price_micros: int
    display_name: str = SUPPORTED_MODEL
    description: str = ""
    own_model: str = SUPPORTED_MODEL
    platform_model: str = SUPPORTED_MODEL
    charge_type: str = SUPPORTED_CHARGE_TYPE

    def __post_init__(self) -> None:
        _validate_logical_key(self.logical_key)
        display_name = str(self.display_name).strip()
        description = str(self.description).strip()
        prices = (
            self.input_price_micros,
            self.output_price_micros,
            self.cache_read_price_micros,
            self.cache_write_price_micros,
        )
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in prices
        ):
            raise ManagedListingError("managed_listing_prices_invalid")
        if (
            self.own_model != SUPPORTED_MODEL
            or self.platform_model != SUPPORTED_MODEL
            or self.charge_type != SUPPORTED_CHARGE_TYPE
        ):
            raise ManagedListingError("managed_listing_spec_unsupported")
        if not display_name or len(display_name) > MAX_LISTING_DISPLAY_NAME_LENGTH:
            raise ManagedListingError("managed_listing_display_name_invalid")
        if not is_source_only_listing_description(description):
            raise ManagedListingError("managed_listing_description_invalid")
        object.__setattr__(self, "display_name", display_name)
        object.__setattr__(self, "description", description)


@dataclass(frozen=True)
class ChannelCreateIntent:
    managed_uid: str
    merchant_id: int
    create_idempotency_key: str
    desired_state: Mapping[str, Any]


@dataclass(frozen=True)
class ListingCreateIntent:
    managed_uid: str
    merchant_id: int
    channel_binding_id: int
    a6_channel_id: int
    create_idempotency_key: str
    desired_state: Mapping[str, Any]


@dataclass(frozen=True)
class ChannelAdoptionIntent:
    managed_uid: str
    merchant_id: int
    a6_channel_id: int
    idempotency_key: str
    desired_state: Mapping[str, Any]
    observation: Mapping[str, Any]


@dataclass(frozen=True)
class ListingAdoptionIntent:
    managed_uid: str
    merchant_id: int
    channel_binding_id: int
    a6_channel_id: int
    a6_listing_id: int
    idempotency_key: str
    desired_state: Mapping[str, Any]
    observation: Mapping[str, Any]


@dataclass(frozen=True)
class ManagedChannelBinding:
    binding_id: int
    managed_uid: str
    merchant_id: int
    create_idempotency_key: str
    control_state: str
    desired_state: Mapping[str, Any]
    observed_state: Mapping[str, Any] | None = None
    a6_channel_id: int | None = None
    create_action_id: int | None = None
    last_error_class: str | None = None


@dataclass(frozen=True)
class ManagedListingBinding:
    binding_id: int
    managed_uid: str
    merchant_id: int
    channel_binding_id: int
    create_idempotency_key: str
    control_state: str
    desired_state: Mapping[str, Any]
    observed_state: Mapping[str, Any] | None = None
    a6_channel_id: int | None = None
    a6_listing_id: int | None = None
    create_action_id: int | None = None
    last_error_class: str | None = None


@dataclass(frozen=True)
class ManagedAction:
    action_id: int
    resource_kind: str
    managed_uid: str
    operation: str
    idempotency_key: str
    desired_state: Mapping[str, Any]
    state: str


@dataclass(frozen=True)
class ManagedDecision:
    merchant_id: int
    resource_kind: str
    managed_uid: str
    action: str
    state: str
    applied: bool
    external_id: int | None = None
    error_class: str | None = None

    def public(self) -> dict[str, Any]:
        return {
            "merchant_id": self.merchant_id,
            "resource_kind": self.resource_kind,
            "managed_uid": self.managed_uid,
            "action": self.action,
            "state": self.state,
            "applied": self.applied,
            "external_id": self.external_id,
            "error_class": self.error_class,
        }


@dataclass(frozen=True)
class ExistingChannelSelection:
    channel: Mapping[str, Any]
    listing: Mapping[str, Any] | None


class ManagedListingStore(Protocol):
    def get_channel(self, managed_uid: str) -> ManagedChannelBinding | None: ...

    def reserve_channel(self, intent: ChannelCreateIntent) -> ManagedChannelBinding: ...

    def adopt_channel(self, intent: ChannelAdoptionIntent) -> ManagedChannelBinding: ...

    def claim_channel_create(
        self, binding: ManagedChannelBinding
    ) -> ManagedChannelBinding | None: ...

    def record_channel_post(
        self, binding: ManagedChannelBinding, observation: Mapping[str, Any]
    ) -> ManagedChannelBinding: ...

    def mark_channel_create_unknown(
        self, binding: ManagedChannelBinding, error_class: str
    ) -> ManagedChannelBinding: ...

    def mark_channel_create_failed(
        self,
        binding: ManagedChannelBinding,
        error_class: str,
        observation: Mapping[str, Any],
    ) -> ManagedChannelBinding: ...

    def resolve_channel_create_unknown(
        self,
        binding: ManagedChannelBinding,
        desired_state: Mapping[str, Any],
        observation: Mapping[str, Any],
        error_class: str,
    ) -> ManagedChannelBinding: ...

    def verify_channel(
        self,
        binding: ManagedChannelBinding,
        channel_id: int,
        observation: Mapping[str, Any],
    ) -> ManagedChannelBinding: ...

    def get_listing(self, managed_uid: str) -> ManagedListingBinding | None: ...

    def list_controlled_listings(
        self,
        merchant_id: int,
        model: str,
    ) -> tuple[ManagedListingBinding, ...]: ...

    def reserve_listing(self, intent: ListingCreateIntent) -> ManagedListingBinding: ...

    def adopt_listing(self, intent: ListingAdoptionIntent) -> ManagedListingBinding: ...

    def claim_listing_create(
        self, binding: ManagedListingBinding
    ) -> ManagedListingBinding | None: ...

    def record_listing_post(
        self, binding: ManagedListingBinding, observation: Mapping[str, Any]
    ) -> ManagedListingBinding: ...

    def mark_listing_create_unknown(
        self, binding: ManagedListingBinding, error_class: str
    ) -> ManagedListingBinding: ...

    def mark_listing_create_failed(
        self,
        binding: ManagedListingBinding,
        error_class: str,
        observation: Mapping[str, Any],
    ) -> ManagedListingBinding: ...

    def resolve_listing_create_unknown(
        self,
        binding: ManagedListingBinding,
        desired_state: Mapping[str, Any],
        observation: Mapping[str, Any],
        error_class: str,
    ) -> ManagedListingBinding: ...

    def verify_listing(
        self,
        binding: ManagedListingBinding,
        listing_id: int,
        observation: Mapping[str, Any],
    ) -> ManagedListingBinding: ...

    def get_pending_action(self, resource_kind: str, managed_uid: str) -> ManagedAction | None: ...

    def begin_action(
        self,
        resource_kind: str,
        managed_uid: str,
        operation: str,
        desired_state: Mapping[str, Any],
        idempotency_key: str,
    ) -> ManagedAction | None: ...

    def mark_action_unknown(self, action: ManagedAction, error_class: str) -> None: ...

    def retry_unknown_channel_update(
        self,
        action: ManagedAction,
    ) -> ManagedAction | None: ...

    def retry_unknown_listing_update(
        self,
        action: ManagedAction,
    ) -> ManagedAction | None: ...

    def retry_unknown_listing_action(
        self,
        action: ManagedAction,
    ) -> ManagedAction | None: ...

    def supersede_listing_update(
        self,
        action: ManagedAction,
        observation: Mapping[str, Any],
    ) -> ManagedListingBinding: ...

    def complete_channel_action(
        self,
        binding: ManagedChannelBinding,
        action: ManagedAction,
        observation: Mapping[str, Any],
    ) -> ManagedChannelBinding: ...

    def complete_listing_action(
        self,
        binding: ManagedListingBinding,
        action: ManagedAction,
        observation: Mapping[str, Any],
    ) -> ManagedListingBinding: ...


class ManagedListingsClient(Protocol):
    def supplier_me(self, user_id: int) -> dict[str, Any]: ...

    def apply_supplier(self, user_id: int, payload: Mapping[str, Any]) -> dict[str, Any]: ...

    def supplier_requests(self, user_id: int) -> list[dict[str, Any]]: ...

    def supplier_channels(self, user_id: int) -> list[dict[str, Any]]: ...

    def fetch_models_via_a6(self, user_id: int, base_url: str, api_key: str) -> list[str]: ...

    def update_supplier_channel(
        self, user_id: int, channel_id: int, payload: Mapping[str, Any]
    ) -> dict[str, Any]: ...

    def supplier_listings(
        self,
        user_id: int,
        *,
        page: int = 1,
        size: int = 1000,
        status: int | None = None,
    ) -> dict[str, Any]: ...

    def create_supplier_listings(
        self, user_id: int, listings: Sequence[Mapping[str, Any]]
    ) -> dict[str, Any]: ...

    def update_supplier_listing(
        self, user_id: int, listing_id: int, payload: Mapping[str, Any]
    ) -> dict[str, Any]: ...

    def takedown_supplier_listing(self, user_id: int, listing_id: int) -> dict[str, Any]: ...

    def resubmit_supplier_listing(self, user_id: int, listing_id: int) -> dict[str, Any]: ...


ClientFactory = Callable[[ManagedMerchant], ManagedListingsClient]


def stable_managed_uid(resource_kind: str, merchant_id: int, logical_key: str) -> str:
    if resource_kind not in {"channel", "listing"} or merchant_id < 1:
        raise ManagedListingError("managed_uid_input_invalid")
    _validate_logical_key(logical_key)
    digest = hashlib.sha256(f"{resource_kind}\0{merchant_id}\0{logical_key}".encode()).hexdigest()
    return f"a6-managed:{resource_kind}:m{merchant_id}:{digest[:20]}"


def managed_marker(managed_uid: str) -> str:
    return f"a6m-{hashlib.sha256(managed_uid.encode()).hexdigest()[:16]}"


def managed_channel_name(managed_uid: str) -> str:
    """Return the stable A6 channel identifier (ASCII, exactly 10 characters)."""
    digest = hashlib.sha256(managed_uid.encode()).digest()
    encoded = base64.b32encode(digest).decode("ascii").lower()
    return f"e{encoded[:9]}"


def managed_resource_signature(resource_kind: str, managed_uid: str) -> str:
    if resource_kind not in {"channel", "listing"}:
        raise ManagedListingError("managed_resource_kind_invalid")
    prefix = "a6c" if resource_kind == "channel" else "a6l"
    digest = hashlib.sha256(managed_uid.encode()).hexdigest()[:16]
    return f"{prefix}-{digest}"


class ManagedListingController:
    def __init__(
        self,
        store: ManagedListingStore,
        client_factory: ClientFactory,
    ) -> None:
        self.store = store
        self.client_factory = client_factory

    def ensure_channel(
        self,
        merchant: ManagedMerchant,
        spec: ManagedChannelSpec,
        *,
        apply: bool = False,
    ) -> ManagedDecision:
        _validate_apply(apply)
        client = self.client_factory(merchant)
        managed_uid = stable_managed_uid("channel", merchant.merchant_id, spec.logical_key)
        binding = self.store.get_channel(managed_uid)
        channels = client.supplier_channels(merchant.a6_user_id)
        requests = client.supplier_requests(merchant.a6_user_id)
        desired = _channel_desired_state(managed_uid, spec, channels, requests)
        if binding is not None:
            _assert_binding_owner(binding.merchant_id, merchant.merchant_id)
            if binding.control_state == "error" and not binding.a6_channel_id:
                _assert_spec_fingerprint(binding.desired_state, desired)
                if binding.last_error_class != _CONFIRMED_ABSENT_CREATE_ERROR:
                    return _decision(
                        merchant,
                        "channel",
                        managed_uid,
                        "blocked",
                        binding.control_state,
                        False,
                        error_class="managed_channel_create_recovery_required",
                    )
                if not apply:
                    return _decision(
                        merchant, "channel", managed_uid, "retry_create", "error", False
                    )
                claimed = self.store.claim_channel_create(binding)
                if claimed is None:
                    return _decision(
                        merchant,
                        "channel",
                        managed_uid,
                        "reconcile_pending",
                        binding.control_state,
                        False,
                    )
                return self._submit_channel_create(merchant, client, claimed, spec)
            if not (binding.a6_channel_id and binding.control_state in _PENDING_ACTION_STATES):
                _assert_spec_fingerprint(binding.desired_state, desired)
            return self._reconcile_channel(
                merchant,
                client,
                binding,
                channels,
                requests,
                apply=apply,
            )
        if not apply:
            return _decision(merchant, "channel", managed_uid, "create", "planned", False)

        binding = self.store.reserve_channel(
            ChannelCreateIntent(
                managed_uid=managed_uid,
                merchant_id=merchant.merchant_id,
                create_idempotency_key=f"managed-channel-create:{managed_uid}",
                desired_state=desired,
            )
        )
        claimed = self.store.claim_channel_create(binding)
        if claimed is None:
            return _decision(
                merchant,
                "channel",
                managed_uid,
                "reconcile_pending",
                binding.control_state,
                False,
            )
        return self._submit_channel_create(merchant, client, claimed, spec)

    def ensure_listing(
        self,
        merchant: ManagedMerchant,
        channel_managed_uid: str,
        spec: ManagedListingSpec,
        *,
        apply: bool = False,
    ) -> ManagedDecision:
        _validate_apply(apply)
        channel = self.store.get_channel(channel_managed_uid)
        if (
            channel is None
            or channel.merchant_id != merchant.merchant_id
            or channel.control_state != "verified"
            or not channel.a6_channel_id
        ):
            raise ManagedListingError("managed_listing_channel_not_verified")
        if _binding_uses_forbidden_channel(channel):
            raise ManagedListingError("managed_channel_name_forbidden")
        managed_uid = stable_managed_uid(
            "listing",
            merchant.merchant_id,
            f"{spec.logical_key}:{managed_marker(channel_managed_uid)}",
        )
        client = self.client_factory(merchant)
        listings = _list_all_listings(client, merchant.a6_user_id)
        desired = _listing_desired_state(managed_uid, channel.a6_channel_id, spec, listings)
        binding = self.store.get_listing(managed_uid)
        if binding is not None:
            _assert_binding_owner(binding.merchant_id, merchant.merchant_id)
            if binding.channel_binding_id != channel.binding_id:
                raise ManagedListingError("managed_listing_channel_binding_conflict")
            if binding.control_state == "error" and not binding.a6_listing_id:
                _assert_spec_fingerprint(binding.desired_state, desired)
                if not apply:
                    return _decision(
                        merchant, "listing", managed_uid, "retry_create", "error", False
                    )
                claimed = self.store.claim_listing_create(binding)
                if claimed is None:
                    return _decision(
                        merchant,
                        "listing",
                        managed_uid,
                        "reconcile_pending",
                        binding.control_state,
                        False,
                    )
                return self._submit_listing_create(merchant, client, claimed, spec)
            if not (binding.a6_listing_id and binding.control_state in _PENDING_ACTION_STATES):
                _assert_spec_fingerprint(binding.desired_state, desired)
            return self._reconcile_listing(
                merchant,
                binding,
                listings,
                apply=apply,
            )
        if not apply:
            return _decision(merchant, "listing", managed_uid, "create", "planned", False)

        binding = self.store.reserve_listing(
            ListingCreateIntent(
                managed_uid=managed_uid,
                merchant_id=merchant.merchant_id,
                channel_binding_id=channel.binding_id,
                a6_channel_id=channel.a6_channel_id,
                create_idempotency_key=f"managed-listing-create:{managed_uid}",
                desired_state=desired,
            )
        )
        claimed = self.store.claim_listing_create(binding)
        if claimed is None:
            return _decision(
                merchant,
                "listing",
                managed_uid,
                "reconcile_pending",
                binding.control_state,
                False,
            )
        return self._submit_listing_create(merchant, client, claimed, spec)

    def select_existing_channel(
        self,
        merchant: ManagedMerchant,
        *,
        model: str = SUPPORTED_MODEL,
        preferred_channel_id: int | None = None,
    ) -> ExistingChannelSelection:
        if model != SUPPORTED_MODEL:
            raise ManagedListingError("managed_channel_adoption_model_unsupported")
        client = self.client_factory(merchant)
        channels = client.supplier_channels(merchant.a6_user_id)
        listings = _list_all_listings(client, merchant.a6_user_id)
        eligible = [item for item in channels if _channel_is_eligible_for_adoption(item)]
        if preferred_channel_id is not None:
            selected = [
                item
                for item in eligible
                if _positive_int(item.get("id"), required=False) == preferred_channel_id
            ]
            if len(selected) != 1:
                raise ManagedListingError("managed_channel_adoption_preferred_unavailable")
            channel = selected[0]
        else:
            listing_channel_ids = {
                _positive_int(item.get("channel_id"), required=False)
                for item in listings
                if item.get("own_model") == model and item.get("platform_model") == model
            }
            listing_channel_ids.discard(0)
            listed = [
                item
                for item in eligible
                if _positive_int(item.get("id"), required=False) in listing_channel_ids
            ]
            if len(listed) == 1:
                channel = listed[0]
            elif len(listed) > 1:
                raise ManagedListingError("managed_channel_adoption_multiple_model_channels")
            elif len(eligible) == 1:
                channel = eligible[0]
            elif not eligible:
                raise ManagedListingError("managed_channel_adoption_no_eligible_channel")
            else:
                raise ManagedListingError("managed_channel_adoption_ambiguous")
        channel_id = _positive_int(channel.get("id"))
        matching = [
            item
            for item in listings
            if _positive_int(item.get("channel_id"), required=False) == channel_id
            and item.get("own_model") == model
            and item.get("platform_model") == model
        ]
        if len(matching) > 1:
            raise ManagedListingError("managed_listing_channel_model_duplicate")
        return ExistingChannelSelection(
            channel=_safe_remote_item(channel),
            listing=_safe_remote_item(matching[0]) if matching else None,
        )

    def ensure_existing_channel(
        self,
        merchant: ManagedMerchant,
        logical_key: str,
        selection: ExistingChannelSelection,
        *,
        model: str = SUPPORTED_MODEL,
        managed_uid: str | None = None,
        apply: bool = False,
    ) -> ManagedDecision:
        _validate_apply(apply)
        if model != SUPPORTED_MODEL:
            raise ManagedListingError("managed_channel_adoption_model_unsupported")
        if managed_uid is None:
            managed_uid = stable_managed_uid("channel", merchant.merchant_id, logical_key)
        elif not _CHANNEL_MANAGED_UID.fullmatch(managed_uid):
            raise ManagedListingError("managed_channel_uid_invalid")
        channel = dict(selection.channel)
        if is_forbidden_channel_name(channel.get("name")):
            raise ManagedListingError("managed_channel_name_forbidden")
        channel_id = _positive_int(channel.get("id"))
        binding = self.store.get_channel(managed_uid)
        if binding is not None and binding.control_state in {
            "verified",
            "updating",
            "update_unknown",
        }:
            if (
                binding.merchant_id != merchant.merchant_id
                or binding.a6_channel_id != channel_id
                or not _channel_binding_is_adopted(binding)
            ):
                raise ManagedListingError("managed_channel_adoption_binding_conflict")
            desired_models = _channel_models_with(channel, model)
            if _channel_models(channel) == desired_models:
                return _decision(
                    merchant,
                    "channel",
                    managed_uid,
                    "noop",
                    "verified",
                    apply,
                    external_id=channel_id,
                )
            return self.update_channel(
                merchant,
                managed_uid,
                {"models": ",".join(desired_models)},
                apply=apply,
            )
        if binding is not None and (
            binding.merchant_id != merchant.merchant_id
            or binding.a6_channel_id not in {None, channel_id}
            or binding.control_state
            not in {
                "planned",
                "creating",
                "create_unknown",
                "verifying",
                "updating",
                "update_unknown",
                "error",
                "retired",
            }
        ):
            raise ManagedListingError("managed_channel_adoption_binding_conflict")
        desired_models = _channel_models_with(channel, model)
        desired_state = _channel_adoption_desired_state(
            managed_uid,
            channel,
            desired_models,
        )
        if not apply:
            return _decision(
                merchant,
                "channel",
                managed_uid,
                "adopt_existing",
                "planned",
                False,
                external_id=channel_id,
            )
        client = self.client_factory(merchant)
        if _channel_models(channel) != desired_models:
            remote_id = _positive_int(channel.get("binding_id"), required=False) or channel_id
            client.update_supplier_channel(
                merchant.a6_user_id,
                remote_id,
                {"models": ",".join(desired_models)},
            )
        refreshed = _item_by_id(client.supplier_channels(merchant.a6_user_id), channel_id)
        if (
            refreshed is None
            or not _channel_is_eligible_for_adoption(refreshed)
            or model not in _channel_models(refreshed)
        ):
            raise ManagedListingError("managed_channel_adoption_readback_failed")
        observation = _safe_remote_item(refreshed)
        adopted = self.store.adopt_channel(
            ChannelAdoptionIntent(
                managed_uid=managed_uid,
                merchant_id=merchant.merchant_id,
                a6_channel_id=channel_id,
                idempotency_key=f"managed-channel-adopt:{managed_uid}:{channel_id}",
                desired_state=desired_state,
                observation=observation,
            )
        )
        return _decision(
            merchant,
            "channel",
            managed_uid,
            "adopt_existing",
            adopted.control_state,
            True,
            external_id=channel_id,
        )

    def ensure_listing_on_existing_channel(
        self,
        merchant: ManagedMerchant,
        channel_managed_uid: str,
        selection: ExistingChannelSelection,
        spec: ManagedListingSpec,
        *,
        apply: bool = False,
    ) -> ManagedDecision:
        _validate_apply(apply)
        managed_uid = stable_managed_uid(
            "listing",
            merchant.merchant_id,
            f"{spec.logical_key}:{managed_marker(channel_managed_uid)}",
        )
        if is_forbidden_channel_name(selection.channel.get("name")):
            raise ManagedListingError("managed_channel_name_forbidden")
        channel = self.store.get_channel(channel_managed_uid)
        remote_channel_id = _positive_int(selection.channel.get("id"))
        binding = self.store.get_listing(managed_uid)
        if binding is not None:
            if (
                channel is None
                or channel.control_state != "verified"
                or channel.a6_channel_id != remote_channel_id
            ):
                raise ManagedListingError("managed_listing_adoption_channel_conflict")
            reconciled_pending_create = (
                not binding.a6_listing_id and binding.control_state in _PENDING_CREATE_STATES
            )
            if reconciled_pending_create:
                decision = self._reconcile_listing(
                    merchant,
                    binding,
                    _list_all_listings(
                        self.client_factory(merchant),
                        merchant.a6_user_id,
                    ),
                    apply=apply,
                )
            else:
                decision = self.update_listing(
                    merchant,
                    managed_uid,
                    spec,
                    apply=apply,
                )
            if not apply or decision.state != "verified":
                return decision
            if reconciled_pending_create:
                decision = self.update_listing(
                    merchant,
                    managed_uid,
                    spec,
                    apply=True,
                )
                if decision.state != "verified":
                    return decision
                return decision
            refreshed = _item_by_id(
                _list_all_listings(self.client_factory(merchant), merchant.a6_user_id),
                int(binding.a6_listing_id or 0),
            )
            if refreshed is not None and str(refreshed.get("status")) == "0":
                return self.resubmit_listing(merchant, managed_uid, apply=True)
            return decision
        existing = selection.listing
        if existing is None:
            if (
                channel is None
                or channel.control_state != "verified"
                or channel.a6_channel_id != remote_channel_id
            ):
                if not apply:
                    return _decision(
                        merchant,
                        "listing",
                        managed_uid,
                        "create_after_channel_adoption",
                        "planned",
                        False,
                    )
                raise ManagedListingError("managed_listing_adoption_channel_missing")
            return self.ensure_listing(
                merchant,
                channel_managed_uid,
                spec,
                apply=apply,
            )
        listing_id = _positive_int(existing.get("id"))
        if not apply:
            return _decision(
                merchant,
                "listing",
                managed_uid,
                "adopt_existing",
                "planned",
                False,
                external_id=listing_id,
            )
        if channel is None or channel.control_state != "verified":
            raise ManagedListingError("managed_listing_adoption_channel_missing")
        client = self.client_factory(merchant)
        inventory = _list_all_listings(client, merchant.a6_user_id)
        desired = _listing_desired_state(
            managed_uid,
            remote_channel_id,
            spec,
            inventory,
        )
        payload = _listing_update_payload(spec)
        reactivated = str(existing.get("status")) == "0"
        try:
            if reactivated:
                client.resubmit_supplier_listing(merchant.a6_user_id, listing_id)
                active = _item_by_id(
                    _list_all_listings(client, merchant.a6_user_id),
                    listing_id,
                )
                if active is None or str(active.get("status")) != "2":
                    raise ManagedListingError("managed_listing_adoption_resubmit_readback_failed")
            client.update_supplier_listing(merchant.a6_user_id, listing_id, payload)
            refreshed = _item_by_id(
                _list_all_listings(client, merchant.a6_user_id),
                listing_id,
            )
        except Exception:
            if reactivated:
                _rollback_listing_activation(client, merchant.a6_user_id, listing_id)
            raise
        if refreshed is None or not _listing_mapping_matches(refreshed, payload):
            if reactivated:
                _rollback_listing_activation(client, merchant.a6_user_id, listing_id)
            raise ManagedListingError("managed_listing_adoption_readback_failed")
        adopted = self.store.adopt_listing(
            ListingAdoptionIntent(
                managed_uid=managed_uid,
                merchant_id=merchant.merchant_id,
                channel_binding_id=channel.binding_id,
                a6_channel_id=remote_channel_id,
                a6_listing_id=listing_id,
                idempotency_key=f"managed-listing-adopt:{managed_uid}:{listing_id}",
                desired_state={**desired, "listing_strategy": "adopt_existing"},
                observation=_safe_remote_item(refreshed),
            )
        )
        if str(refreshed.get("status")) == "0":
            return self.resubmit_listing(
                merchant,
                adopted.managed_uid,
                apply=True,
            )
        return _decision(
            merchant,
            "listing",
            adopted.managed_uid,
            "adopt_existing",
            adopted.control_state,
            True,
            external_id=listing_id,
        )

    def resolve_channel_create_unknown(
        self,
        merchant: ManagedMerchant,
        spec: ManagedChannelSpec,
        *,
        apply: bool = False,
    ) -> ManagedDecision:
        _validate_apply(apply)
        client = self.client_factory(merchant)
        managed_uid = stable_managed_uid("channel", merchant.merchant_id, spec.logical_key)
        binding = self.store.get_channel(managed_uid)
        if binding is None or binding.merchant_id != merchant.merchant_id:
            raise ManagedListingError("managed_channel_create_unknown_missing")
        if binding.control_state != "create_unknown" or binding.a6_channel_id is not None:
            raise ManagedListingError("managed_channel_create_unknown_not_recoverable")
        channels = client.supplier_channels(merchant.a6_user_id)
        requests = client.supplier_requests(merchant.a6_user_id)
        desired = _channel_desired_state(managed_uid, spec, channels, requests)
        candidate = _new_channel_candidate(binding, channels, requests)
        if candidate is not None:
            channel_id = _positive_int(candidate.get("id"))
            if not apply:
                return _decision(
                    merchant,
                    "channel",
                    managed_uid,
                    "verify",
                    binding.control_state,
                    False,
                    external_id=channel_id,
                )
            verified = self.store.verify_channel(binding, channel_id, _safe_remote_item(candidate))
            return _decision(
                merchant,
                "channel",
                managed_uid,
                "verify",
                verified.control_state,
                True,
                external_id=channel_id,
            )
        if binding.last_error_class not in _RECOVERABLE_CHANNEL_CREATE_UNKNOWN_ERRORS:
            raise ManagedListingError("managed_channel_create_unknown_not_recoverable")
        evidence = _absent_create_evidence(binding, desired, channels, requests)
        if not apply:
            return _decision(
                merchant,
                "channel",
                managed_uid,
                "resolve_absent",
                binding.control_state,
                False,
            )
        resolved = self.store.resolve_channel_create_unknown(
            binding,
            desired,
            evidence,
            _CONFIRMED_ABSENT_CREATE_ERROR,
        )
        return _decision(
            merchant,
            "channel",
            managed_uid,
            "resolve_absent",
            resolved.control_state,
            True,
        )

    def resolve_listing_create_unknown(
        self,
        merchant: ManagedMerchant,
        channel_managed_uid: str,
        spec: ManagedListingSpec,
        *,
        apply: bool = False,
    ) -> ManagedDecision:
        _validate_apply(apply)
        channel = self.store.get_channel(channel_managed_uid)
        if (
            channel is None
            or channel.merchant_id != merchant.merchant_id
            or channel.control_state != "verified"
            or not channel.a6_channel_id
        ):
            raise ManagedListingError("managed_listing_channel_not_verified")
        managed_uid = stable_managed_uid(
            "listing",
            merchant.merchant_id,
            f"{spec.logical_key}:{managed_marker(channel_managed_uid)}",
        )
        binding = self.store.get_listing(managed_uid)
        if binding is None or binding.merchant_id != merchant.merchant_id:
            raise ManagedListingError("managed_listing_create_unknown_missing")
        client = self.client_factory(merchant)
        listings = _list_all_listings(client, merchant.a6_user_id)
        desired = _listing_desired_state(managed_uid, channel.a6_channel_id, spec, listings)
        candidate = _new_listing_candidate(binding, listings)
        if candidate is not None:
            listing_id = _positive_int(candidate.get("id"))
            if not apply:
                return _decision(
                    merchant,
                    "listing",
                    managed_uid,
                    "verify",
                    binding.control_state,
                    False,
                    external_id=listing_id,
                )
            verified = self.store.verify_listing(binding, listing_id, _safe_remote_item(candidate))
            return _decision(
                merchant,
                "listing",
                managed_uid,
                "verify",
                verified.control_state,
                True,
                external_id=listing_id,
            )
        evidence = _absent_create_evidence(binding, desired, listings, ())
        if not apply:
            return _decision(
                merchant,
                "listing",
                managed_uid,
                "resolve_absent",
                binding.control_state,
                False,
            )
        resolved = self.store.resolve_listing_create_unknown(
            binding,
            desired,
            evidence,
            "managed_listing_create_confirmed_absent",
        )
        return _decision(
            merchant,
            "listing",
            managed_uid,
            "resolve_absent",
            resolved.control_state,
            True,
        )

    def _submit_channel_create(
        self,
        merchant: ManagedMerchant,
        client: ManagedListingsClient,
        binding: ManagedChannelBinding,
        spec: ManagedChannelSpec,
    ) -> ManagedDecision:
        try:
            supplier = _current_supplier_payload(
                merchant,
                client.supplier_me(merchant.a6_user_id),
            )
            response = client.apply_supplier(
                merchant.a6_user_id,
                _channel_apply_payload(binding.managed_uid, spec, supplier),
            )
            binding = self.store.record_channel_post(binding, _safe_mutation_observation(response))
        except Exception as exc:
            code = _error_code(exc, "managed_channel_create_result_unknown")
            if code == "supplier_apply_pending_request_exists":
                binding = self.store.record_channel_post(
                    binding,
                    {"status": "pending_request_exists"},
                )
            elif code == "supplier_apply_failed":
                binding = self.store.mark_channel_create_unknown(binding, code)
                public_error = "managed_channel_create_recovery_required"
            elif _create_result_unknown(exc):
                binding = self.store.mark_channel_create_unknown(binding, code)
                public_error = "managed_channel_create_result_unknown"
            else:
                binding = self.store.mark_channel_create_failed(
                    binding,
                    code,
                    _known_failure_observation(code),
                )
                public_error = "managed_channel_create_failed"
            if code != "supplier_apply_pending_request_exists":
                return _decision(
                    merchant,
                    "channel",
                    binding.managed_uid,
                    "create",
                    binding.control_state,
                    True,
                    error_class=public_error,
                )
        try:
            channels = client.supplier_channels(merchant.a6_user_id)
            requests = client.supplier_requests(merchant.a6_user_id)
        except Exception as exc:
            binding = self.store.mark_channel_create_unknown(
                binding,
                _error_code(exc, "managed_channel_readback_failed"),
            )
            return _decision(
                merchant,
                "channel",
                binding.managed_uid,
                "create",
                binding.control_state,
                True,
                error_class="managed_channel_readback_failed",
            )
        return self._reconcile_channel(
            merchant,
            client,
            binding,
            channels,
            requests,
            apply=True,
        )

    def _submit_listing_create(
        self,
        merchant: ManagedMerchant,
        client: ManagedListingsClient,
        binding: ManagedListingBinding,
        spec: ManagedListingSpec,
    ) -> ManagedDecision:
        payload = _listing_payload(binding.managed_uid, binding.a6_channel_id or 0, spec)
        try:
            response = client.create_supplier_listings(merchant.a6_user_id, [payload])
            binding = self.store.record_listing_post(binding, _safe_mutation_observation(response))
        except Exception as exc:
            code = _error_code(exc, "managed_listing_create_result_unknown")
            if _create_result_unknown(exc):
                binding = self.store.mark_listing_create_unknown(binding, code)
                public_error = "managed_listing_create_result_unknown"
            else:
                binding = self.store.mark_listing_create_failed(
                    binding,
                    code,
                    _known_failure_observation(code),
                )
                public_error = "managed_listing_create_failed"
            return _decision(
                merchant,
                "listing",
                binding.managed_uid,
                "create",
                binding.control_state,
                True,
                error_class=public_error,
            )
        try:
            listings = _list_all_listings(client, merchant.a6_user_id)
        except Exception as exc:
            binding = self.store.mark_listing_create_unknown(
                binding,
                _error_code(exc, "managed_listing_readback_failed"),
            )
            return _decision(
                merchant,
                "listing",
                binding.managed_uid,
                "create",
                binding.control_state,
                True,
                error_class="managed_listing_readback_failed",
            )
        return self._reconcile_listing(merchant, binding, listings, apply=True)

    def update_channel(
        self,
        merchant: ManagedMerchant,
        managed_uid: str,
        patch: Mapping[str, Any],
        *,
        apply: bool = False,
    ) -> ManagedDecision:
        allowed = {"nickname", "description", "models", "status"}
        if not patch or set(patch) - allowed:
            raise ManagedListingError("managed_channel_update_invalid")
        return self._execute_channel_action(
            merchant,
            managed_uid,
            "update",
            dict(patch),
            apply=apply,
        )

    def ensure_channel_upstream(
        self,
        merchant: ManagedMerchant,
        managed_uid: str,
        spec: ManagedChannelSpec,
        *,
        model: str = SUPPORTED_MODEL,
        apply: bool = False,
    ) -> ManagedDecision:
        if model != SUPPORTED_MODEL or model not in spec.models:
            raise ManagedListingError("managed_channel_upstream_model_invalid")
        desired = _channel_upstream_desired_state(spec, model)

        def validate_upstream() -> None:
            client = self.client_factory(merchant)
            models = client.fetch_models_via_a6(
                merchant.a6_user_id,
                spec.base_url.strip(),
                spec.api_key,
            )
            if model not in models:
                raise ManagedListingError("managed_channel_upstream_model_missing")

        return self._execute_channel_action(
            merchant,
            managed_uid,
            "update",
            desired,
            mutation_payload={
                "base_url": spec.base_url.strip(),
                "api_key": spec.api_key,
            },
            before_mutation=validate_upstream,
            apply=apply,
        )

    def update_listing(
        self,
        merchant: ManagedMerchant,
        managed_uid: str,
        spec: ManagedListingSpec,
        *,
        expected_status: int | None = None,
        apply: bool = False,
    ) -> ManagedDecision:
        if expected_status is not None and expected_status not in {0, 2}:
            raise ManagedListingError("managed_listing_expected_status_invalid")
        binding = self._verified_listing(merchant, managed_uid)
        if not binding.a6_channel_id:
            raise ManagedListingError("managed_listing_channel_not_verified")
        payload = _listing_update_payload(spec)
        desired: dict[str, Any] = {"payload": payload}
        if expected_status is not None:
            desired["expected_status"] = expected_status
        return self._execute_listing_action(
            merchant,
            binding,
            "update",
            desired,
            apply=apply,
        )

    def takedown_listing(
        self,
        merchant: ManagedMerchant,
        managed_uid: str,
        *,
        expected_status: int = 0,
        apply: bool = False,
    ) -> ManagedDecision:
        binding = self._verified_listing(merchant, managed_uid)
        return self._execute_listing_action(
            merchant,
            binding,
            "takedown",
            {"expected_status": int(expected_status)},
            apply=apply,
        )

    def resubmit_listing(
        self,
        merchant: ManagedMerchant,
        managed_uid: str,
        *,
        expected_status: int = 2,
        apply: bool = False,
    ) -> ManagedDecision:
        binding = self._verified_listing(merchant, managed_uid)
        return self._execute_listing_action(
            merchant,
            binding,
            "resubmit",
            {"expected_status": int(expected_status)},
            apply=apply,
        )

    def _reconcile_channel(
        self,
        merchant: ManagedMerchant,
        _client: ManagedListingsClient,
        binding: ManagedChannelBinding,
        channels: Sequence[Mapping[str, Any]],
        requests: Sequence[Mapping[str, Any]],
        *,
        apply: bool,
    ) -> ManagedDecision:
        if binding.a6_channel_id:
            remote = _item_by_id(channels, binding.a6_channel_id)
            if remote is None or not _channel_owned(
                remote,
                binding.managed_uid,
                binding.desired_state,
                requests,
            ):
                return _decision(
                    merchant,
                    "channel",
                    binding.managed_uid,
                    "error",
                    binding.control_state,
                    False,
                    external_id=binding.a6_channel_id,
                    error_class="managed_channel_readback_missing_or_changed",
                )
            pending = self.store.get_pending_action("channel", binding.managed_uid)
            if pending is not None:
                return self._reconcile_channel_action(
                    merchant, binding, pending, remote, apply=apply
                )
            return _decision(
                merchant,
                "channel",
                binding.managed_uid,
                "noop",
                binding.control_state,
                apply,
                external_id=binding.a6_channel_id,
            )
        if binding.control_state not in _PENDING_CREATE_STATES:
            return _decision(
                merchant,
                "channel",
                binding.managed_uid,
                "error",
                binding.control_state,
                False,
                error_class="managed_channel_binding_not_creatable",
            )
        candidate = _new_channel_candidate(binding, channels, requests)
        if candidate is None:
            return _decision(
                merchant,
                "channel",
                binding.managed_uid,
                "reconcile_pending",
                binding.control_state,
                False,
                error_class="managed_channel_readback_pending",
            )
        channel_id = _positive_int(candidate.get("id"))
        if not apply:
            return _decision(
                merchant,
                "channel",
                binding.managed_uid,
                "verify",
                binding.control_state,
                False,
                external_id=channel_id,
            )
        binding = self.store.verify_channel(binding, channel_id, _safe_remote_item(candidate))
        return _decision(
            merchant,
            "channel",
            binding.managed_uid,
            "verify",
            binding.control_state,
            True,
            external_id=channel_id,
        )

    def _reconcile_listing(
        self,
        merchant: ManagedMerchant,
        binding: ManagedListingBinding,
        listings: Sequence[Mapping[str, Any]],
        *,
        apply: bool,
    ) -> ManagedDecision:
        if binding.a6_listing_id:
            remote = _item_by_id(listings, binding.a6_listing_id)
            if remote is None or not _listing_owned(remote, binding):
                return _decision(
                    merchant,
                    "listing",
                    binding.managed_uid,
                    "error",
                    binding.control_state,
                    False,
                    external_id=binding.a6_listing_id,
                    error_class="managed_listing_readback_missing_or_changed",
                )
            pending = self.store.get_pending_action("listing", binding.managed_uid)
            if pending is not None:
                return self._reconcile_listing_action(
                    merchant, binding, pending, remote, apply=apply
                )
            return _decision(
                merchant,
                "listing",
                binding.managed_uid,
                "noop",
                binding.control_state,
                apply,
                external_id=binding.a6_listing_id,
            )
        if binding.control_state not in _PENDING_CREATE_STATES:
            return _decision(
                merchant,
                "listing",
                binding.managed_uid,
                "error",
                binding.control_state,
                False,
                error_class="managed_listing_binding_not_creatable",
            )
        candidate = _new_listing_candidate(binding, listings)
        if candidate is None:
            return _decision(
                merchant,
                "listing",
                binding.managed_uid,
                "reconcile_pending",
                binding.control_state,
                False,
                error_class="managed_listing_readback_pending",
            )
        listing_id = _positive_int(candidate.get("id"))
        if not apply:
            return _decision(
                merchant,
                "listing",
                binding.managed_uid,
                "verify",
                binding.control_state,
                False,
                external_id=listing_id,
            )
        binding = self.store.verify_listing(binding, listing_id, _safe_remote_item(candidate))
        return _decision(
            merchant,
            "listing",
            binding.managed_uid,
            "verify",
            binding.control_state,
            True,
            external_id=listing_id,
        )

    def _execute_channel_action(
        self,
        merchant: ManagedMerchant,
        managed_uid: str,
        operation: str,
        desired: Mapping[str, Any],
        *,
        mutation_payload: Mapping[str, Any] | None = None,
        before_mutation: Callable[[], None] | None = None,
        apply: bool,
    ) -> ManagedDecision:
        _validate_apply(apply)
        binding = self.store.get_channel(managed_uid)
        if (
            binding is None
            or binding.merchant_id != merchant.merchant_id
            or not binding.a6_channel_id
        ):
            raise ManagedListingError("managed_channel_not_verified")
        client = self.client_factory(merchant)
        channels = client.supplier_channels(merchant.a6_user_id)
        requests = client.supplier_requests(merchant.a6_user_id)
        remote = _item_by_id(channels, binding.a6_channel_id)
        if remote is not None and is_forbidden_channel_name(remote.get("name")):
            raise ManagedListingError("managed_channel_name_forbidden")
        if remote is None or not _channel_owned(
            remote,
            binding.managed_uid,
            binding.desired_state,
            requests,
        ):
            raise ManagedListingError("managed_channel_readback_missing_or_changed")
        pending = self.store.get_pending_action("channel", managed_uid)
        if pending is not None:
            if _channel_action_satisfied(remote, pending.desired_state):
                return self._reconcile_channel_action(
                    merchant,
                    binding,
                    pending,
                    remote,
                    apply=apply,
                )
            if (
                pending.operation == "update"
                and pending.state == "update_unknown"
                and pending.desired_state == desired
            ):
                if before_mutation is not None:
                    before_mutation()
                if not apply:
                    return _decision(
                        merchant,
                        "channel",
                        managed_uid,
                        "retry_update",
                        "planned",
                        False,
                        external_id=binding.a6_channel_id,
                    )
                retried = self.store.retry_unknown_channel_update(pending)
                if retried is None:
                    return _decision(
                        merchant,
                        "channel",
                        managed_uid,
                        "reconcile_pending",
                        binding.control_state,
                        False,
                        external_id=binding.a6_channel_id,
                    )
                return self._submit_channel_action(
                    merchant,
                    client,
                    binding,
                    retried,
                    desired,
                    mutation_payload or desired,
                    remote,
                )
            return self._reconcile_channel_action(
                merchant,
                binding,
                pending,
                remote,
                apply=apply,
            )
        if _channel_action_satisfied(remote, desired):
            return _decision(
                merchant,
                "channel",
                managed_uid,
                "noop",
                "verified",
                apply,
                external_id=binding.a6_channel_id,
            )
        if before_mutation is not None:
            before_mutation()
        if not apply:
            return _decision(
                merchant,
                "channel",
                managed_uid,
                operation,
                "planned",
                False,
                external_id=binding.a6_channel_id,
            )
        action = self.store.begin_action(
            "channel",
            managed_uid,
            operation,
            desired,
            _action_key(managed_uid, operation, desired, remote),
        )
        if action is None:
            return _decision(
                merchant,
                "channel",
                managed_uid,
                "reconcile_pending",
                binding.control_state,
                False,
                external_id=binding.a6_channel_id,
            )
        return self._submit_channel_action(
            merchant,
            client,
            binding,
            action,
            desired,
            mutation_payload or desired,
            remote,
        )

    def _submit_channel_action(
        self,
        merchant: ManagedMerchant,
        client: ManagedListingsClient,
        binding: ManagedChannelBinding,
        action: ManagedAction,
        desired: Mapping[str, Any],
        mutation_payload: Mapping[str, Any],
        remote_before: Mapping[str, Any],
    ) -> ManagedDecision:
        try:
            binding_id = _positive_int(remote_before.get("binding_id"), required=False)
            client.update_supplier_channel(
                merchant.a6_user_id,
                binding_id or int(binding.a6_channel_id or 0),
                mutation_payload,
            )
            remote = _item_by_id(
                client.supplier_channels(merchant.a6_user_id),
                int(binding.a6_channel_id or 0),
            )
        except Exception as exc:
            self.store.mark_action_unknown(
                action,
                _error_code(exc, "managed_channel_update_unknown"),
            )
            return _decision(
                merchant,
                "channel",
                binding.managed_uid,
                action.operation,
                "update_unknown",
                True,
                external_id=binding.a6_channel_id,
                error_class="managed_channel_update_unknown",
            )
        if remote is not None and _channel_action_satisfied(remote, desired):
            binding = self.store.complete_channel_action(
                binding,
                action,
                _channel_action_observation(remote, desired),
            )
            return _decision(
                merchant,
                "channel",
                binding.managed_uid,
                action.operation,
                binding.control_state,
                True,
                external_id=binding.a6_channel_id,
            )
        self.store.mark_action_unknown(action, "managed_channel_update_readback_mismatch")
        return _decision(
            merchant,
            "channel",
            binding.managed_uid,
            action.operation,
            "update_unknown",
            True,
            external_id=binding.a6_channel_id,
            error_class="managed_channel_update_readback_mismatch",
        )

    def _execute_listing_action(
        self,
        merchant: ManagedMerchant,
        binding: ManagedListingBinding,
        operation: str,
        desired: Mapping[str, Any],
        *,
        apply: bool,
    ) -> ManagedDecision:
        _validate_apply(apply)
        client = self.client_factory(merchant)
        listings = _list_all_listings(client, merchant.a6_user_id)
        remote = _item_by_id(listings, int(binding.a6_listing_id or 0))
        if remote is None or not _listing_owned(remote, binding):
            raise ManagedListingError("managed_listing_readback_missing_or_changed")
        expected_status = desired.get("expected_status")
        if (
            operation == "update"
            and expected_status is not None
            and str(remote.get("status")) != str(expected_status)
        ):
            raise ManagedListingError("managed_listing_activation_changed")
        if operation != "takedown":
            channels = client.supplier_channels(merchant.a6_user_id)
            channel = _item_by_id(channels, int(binding.a6_channel_id or 0))
            if channel is not None and is_forbidden_channel_name(channel.get("name")):
                raise ManagedListingError("managed_channel_name_forbidden")
        pending = self.store.get_pending_action("listing", binding.managed_uid)
        if (
            pending is not None
            and pending.operation == "update"
            and not _listing_action_satisfied(
                remote,
                pending.operation,
                pending.desired_state,
            )
            and pending.desired_state != desired
        ):
            if not apply:
                return _decision(
                    merchant,
                    "listing",
                    binding.managed_uid,
                    "replace_update",
                    "planned",
                    False,
                    external_id=binding.a6_listing_id,
                )
            superseded = self.store.supersede_listing_update(
                pending,
                _safe_remote_item(remote),
            )
            return self._execute_listing_action(
                merchant,
                superseded,
                operation,
                desired,
                apply=True,
            )
        reactivated = False
        if (
            operation == "update"
            and str(remote.get("status")) == "0"
            and expected_status is None
        ):
            if not apply:
                return _decision(
                    merchant,
                    "listing",
                    binding.managed_uid,
                    "update_after_resubmit",
                    "planned",
                    False,
                    external_id=binding.a6_listing_id,
                )
            client.resubmit_supplier_listing(
                merchant.a6_user_id,
                int(binding.a6_listing_id or 0),
            )
            remote = _item_by_id(
                _list_all_listings(client, merchant.a6_user_id),
                int(binding.a6_listing_id or 0),
            )
            if remote is None or str(remote.get("status")) != "2":
                raise ManagedListingError("managed_listing_update_resubmit_readback_failed")
            reactivated = True
        if pending is not None:
            if (
                pending.operation == "update"
                and pending.state == "update_unknown"
                and not _listing_action_satisfied(
                    remote,
                    pending.operation,
                    pending.desired_state,
                )
            ):
                if not apply:
                    return _decision(
                        merchant,
                        "listing",
                        binding.managed_uid,
                        "retry_update",
                        "planned",
                        False,
                        external_id=binding.a6_listing_id,
                    )
                retried = self.store.retry_unknown_listing_update(pending)
                if retried is None:
                    return _decision(
                        merchant,
                        "listing",
                        binding.managed_uid,
                        "reconcile_pending",
                        binding.control_state,
                        False,
                        external_id=binding.a6_listing_id,
                    )
                retry_result = self._submit_listing_action(
                    merchant,
                    client,
                    binding,
                    retried,
                    pending.operation,
                    pending.desired_state,
                    rollback_activation=(
                        reactivated or pending.desired_state.get("expected_status") == 0
                    ),
                )
                if retry_result.state != "verified" or pending.desired_state == desired:
                    return retry_result
                refreshed = self._verified_listing(merchant, binding.managed_uid)
                return self._execute_listing_action(
                    merchant,
                    refreshed,
                    operation,
                    desired,
                    apply=True,
                )
            if (
                pending.operation == "update"
                and pending.state == "updating"
                and (reactivated or pending.desired_state.get("expected_status") == 0)
            ):
                return self._submit_listing_action(
                    merchant,
                    client,
                    binding,
                    pending,
                    pending.operation,
                    pending.desired_state,
                    rollback_activation=True,
                )
            return self._reconcile_listing_action(merchant, binding, pending, remote, apply=apply)
        if _listing_action_satisfied(remote, operation, desired):
            return _decision(
                merchant,
                "listing",
                binding.managed_uid,
                "noop",
                "verified",
                apply,
                external_id=binding.a6_listing_id,
            )
        if not apply:
            return _decision(
                merchant,
                "listing",
                binding.managed_uid,
                operation,
                "planned",
                False,
                external_id=binding.a6_listing_id,
            )
        action = self.store.begin_action(
            "listing",
            binding.managed_uid,
            operation,
            desired,
            _action_key(binding.managed_uid, operation, desired, remote),
        )
        if action is None:
            return _decision(
                merchant,
                "listing",
                binding.managed_uid,
                "reconcile_pending",
                binding.control_state,
                False,
                external_id=binding.a6_listing_id,
            )
        return self._submit_listing_action(
            merchant,
            client,
            binding,
            action,
            operation,
            desired,
            rollback_activation=(
                reactivated or (operation == "update" and expected_status == 0)
            ),
        )

    def _submit_listing_action(
        self,
        merchant: ManagedMerchant,
        client: ManagedListingsClient,
        binding: ManagedListingBinding,
        action: ManagedAction,
        operation: str,
        desired: Mapping[str, Any],
        rollback_activation: bool = False,
    ) -> ManagedDecision:
        try:
            listing_id = int(binding.a6_listing_id or 0)
            if operation == "update":
                client.update_supplier_listing(merchant.a6_user_id, listing_id, desired["payload"])
            elif operation == "takedown":
                client.takedown_supplier_listing(merchant.a6_user_id, listing_id)
            elif operation == "resubmit":
                client.resubmit_supplier_listing(merchant.a6_user_id, listing_id)
            else:
                raise ManagedListingError("managed_listing_operation_invalid")
            remote = _item_by_id(_list_all_listings(client, merchant.a6_user_id), listing_id)
        except Exception as exc:
            rollback_confirmed = not rollback_activation or _rollback_listing_activation(
                client,
                merchant.a6_user_id,
                int(binding.a6_listing_id or 0),
            )
            unknown_state = {
                "update": "update_unknown",
                "takedown": "takedown_unknown",
                "resubmit": "resubmit_unknown",
            }[operation]
            error = _error_code(exc, unknown_state)
            if not rollback_confirmed:
                error = "managed_listing_update_rollback_failed"
            self.store.mark_action_unknown(action, error)
            return _decision(
                merchant,
                "listing",
                binding.managed_uid,
                operation,
                unknown_state,
                True,
                external_id=binding.a6_listing_id,
                error_class=(
                    f"managed_listing_{unknown_state}"
                    if rollback_confirmed
                    else "managed_listing_update_rollback_failed"
                ),
            )
        if remote is not None and _listing_action_satisfied(remote, operation, desired):
            binding = self.store.complete_listing_action(binding, action, _safe_remote_item(remote))
            return _decision(
                merchant,
                "listing",
                binding.managed_uid,
                "update_after_resubmit"
                if (
                    operation == "update"
                    and rollback_activation
                    and desired.get("expected_status") is None
                )
                else operation,
                binding.control_state,
                True,
                external_id=binding.a6_listing_id,
            )
        if operation == "update":
            rollback_confirmed = not rollback_activation or _rollback_listing_activation(
                client,
                merchant.a6_user_id,
                int(binding.a6_listing_id or 0),
            )
            error = (
                "managed_listing_update_readback_mismatch"
                if rollback_confirmed
                else "managed_listing_update_rollback_failed"
            )
            self.store.mark_action_unknown(action, error)
            return _decision(
                merchant,
                "listing",
                binding.managed_uid,
                operation,
                "update_unknown",
                True,
                external_id=binding.a6_listing_id,
                error_class=error,
            )
        return _decision(
            merchant,
            "listing",
            binding.managed_uid,
            "reconcile_pending",
            action.state,
            False,
            external_id=binding.a6_listing_id,
            error_class=f"managed_listing_{operation}_readback_pending",
        )

    def _reconcile_channel_action(
        self,
        merchant: ManagedMerchant,
        binding: ManagedChannelBinding,
        action: ManagedAction,
        remote: Mapping[str, Any],
        *,
        apply: bool,
    ) -> ManagedDecision:
        if action.operation != "update":
            raise ManagedListingError("managed_channel_pending_action_invalid")
        if not _channel_action_satisfied(remote, action.desired_state):
            return _decision(
                merchant,
                "channel",
                binding.managed_uid,
                "reconcile_pending",
                action.state,
                False,
                external_id=binding.a6_channel_id,
                error_class="managed_channel_update_readback_pending",
            )
        if not apply:
            return _decision(
                merchant,
                "channel",
                binding.managed_uid,
                "verify",
                action.state,
                False,
                external_id=binding.a6_channel_id,
            )
        if action.state == "update_unknown":
            retried = self.store.retry_unknown_channel_update(action)
            if retried is None:
                return _decision(
                    merchant,
                    "channel",
                    binding.managed_uid,
                    "reconcile_pending",
                    action.state,
                    False,
                    external_id=binding.a6_channel_id,
                    error_class="managed_channel_update_readback_pending",
                )
            action = retried
        binding = self.store.complete_channel_action(
            binding,
            action,
            _channel_action_observation(remote, action.desired_state),
        )
        return _decision(
            merchant,
            "channel",
            binding.managed_uid,
            "verify",
            binding.control_state,
            True,
            external_id=binding.a6_channel_id,
        )

    def _reconcile_listing_action(
        self,
        merchant: ManagedMerchant,
        binding: ManagedListingBinding,
        action: ManagedAction,
        remote: Mapping[str, Any],
        *,
        apply: bool,
    ) -> ManagedDecision:
        if action.operation not in {"update", "takedown", "resubmit"}:
            raise ManagedListingError("managed_listing_pending_action_invalid")
        if not _listing_action_satisfied(remote, action.operation, action.desired_state):
            if apply and action.state == f"{action.operation}_unknown":
                retried = self.store.retry_unknown_listing_action(action)
                if retried is not None:
                    return self._submit_listing_action(
                        merchant,
                        self.client_factory(merchant),
                        binding,
                        retried,
                        retried.operation,
                        retried.desired_state,
                        rollback_activation=(
                            retried.operation == "update"
                            and retried.desired_state.get("expected_status") == 0
                        ),
                    )
            return _decision(
                merchant,
                "listing",
                binding.managed_uid,
                "reconcile_pending",
                action.state,
                False,
                external_id=binding.a6_listing_id,
                error_class=f"managed_listing_{action.operation}_readback_pending",
            )
        if not apply:
            return _decision(
                merchant,
                "listing",
                binding.managed_uid,
                "verify",
                action.state,
                False,
                external_id=binding.a6_listing_id,
            )
        if action.operation == "update" and action.state == "update_unknown":
            retried = self.store.retry_unknown_listing_update(action)
            if retried is None:
                return _decision(
                    merchant,
                    "listing",
                    binding.managed_uid,
                    "reconcile_pending",
                    action.state,
                    False,
                    external_id=binding.a6_listing_id,
                    error_class="managed_listing_update_readback_pending",
                )
            action = retried
        binding = self.store.complete_listing_action(binding, action, _safe_remote_item(remote))
        return _decision(
            merchant,
            "listing",
            binding.managed_uid,
            "verify",
            binding.control_state,
            True,
            external_id=binding.a6_listing_id,
        )

    def _verified_listing(
        self, merchant: ManagedMerchant, managed_uid: str
    ) -> ManagedListingBinding:
        binding = self.store.get_listing(managed_uid)
        if (
            binding is None
            or binding.merchant_id != merchant.merchant_id
            or not binding.a6_listing_id
            or binding.control_state not in ({"verified"} | _PENDING_ACTION_STATES)
        ):
            raise ManagedListingError("managed_listing_not_verified")
        return binding


def _channel_desired_state(
    managed_uid: str,
    spec: ManagedChannelSpec,
    channels: Sequence[Mapping[str, Any]],
    requests: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    payload = _channel_apply_payload(
        managed_uid,
        spec,
        {
            "name": spec.supplier_name.strip(),
            "contact_info": spec.contact_info.strip(),
            "description": spec.supplier_description.strip(),
        },
    )
    public_channel = dict(payload["channel"])
    public_channel.pop("api_key", None)
    public_channel["api_key_sha256"] = hashlib.sha256(spec.api_key.encode()).hexdigest()
    desired = {
        "spec_fingerprint": _hash_document(public_channel),
        "marker": managed_marker(managed_uid),
        "remote_channel": public_channel,
        "baseline_channel_ids": sorted(_ids(channels)),
        "baseline_request_ids": sorted(_ids(requests)),
    }
    return desired


def _binding_uses_forbidden_channel(binding: ManagedChannelBinding) -> bool:
    desired = binding.desired_state
    candidates: list[Mapping[str, Any]] = []
    remote = desired.get("remote_channel")
    if isinstance(remote, Mapping):
        candidates.append(remote)
    adoption = desired.get("adoption")
    if isinstance(adoption, Mapping):
        channel = adoption.get("channel")
        if isinstance(channel, Mapping):
            candidates.append(channel)
    if isinstance(binding.observed_state, Mapping):
        candidates.append(binding.observed_state)
    return any(is_forbidden_channel_name(item.get("name")) for item in candidates)


def _channel_is_eligible_for_adoption(item: Mapping[str, Any]) -> bool:
    return (
        not is_forbidden_channel_name(item.get("name"))
        and str(item.get("status")) == "1"
        and str(item.get("availability")) == "1"
    )


def _channel_models(item: Mapping[str, Any]) -> tuple[str, ...]:
    raw = item.get("models")
    if isinstance(raw, str):
        values = raw.split(",")
    elif isinstance(raw, (list, tuple)):
        values = raw
    else:
        values = ()
    return tuple(dict.fromkeys(str(value).strip() for value in values if str(value).strip()))


def _channel_models_with(
    item: Mapping[str, Any],
    model: str,
) -> tuple[str, ...]:
    return tuple(dict.fromkeys((*_channel_models(item), model)))


def _channel_adoption_desired_state(
    managed_uid: str,
    channel: Mapping[str, Any],
    desired_models: tuple[str, ...],
) -> dict[str, Any]:
    identity = {
        key: channel.get(key)
        for key in (
            "id",
            "binding_id",
            "supplier_id",
            "name",
            "nickname",
            "channel_type",
            "status",
            "availability",
        )
    }
    adoption = {
        "channel": identity,
        "fingerprint": _hash_document(identity),
    }
    return {
        "spec_fingerprint": _hash_document(
            {
                "channel": identity,
                "models": list(desired_models),
            }
        ),
        "marker": managed_marker(managed_uid),
        "channel_strategy": "adopt_existing",
        "models_before": list(_channel_models(channel)),
        "models_after": list(desired_models),
        "adoption": adoption,
    }


def _channel_upstream_desired_state(
    spec: ManagedChannelSpec,
    model: str,
) -> dict[str, Any]:
    base_url = _normalize_upstream_base_url(spec.base_url)
    api_key = spec.api_key
    return {
        "operation": "ensure_upstream",
        "upstream_base_url_sha256": _text_sha256(base_url),
        "upstream_api_key_sha256": _text_sha256(api_key),
        "api_key_last4": api_key[-4:],
        "has_api_key": True,
        "required_model": model,
    }


def _channel_binding_is_adopted(binding: ManagedChannelBinding) -> bool:
    desired = binding.desired_state
    return desired.get("channel_strategy") == "adopt_existing" or isinstance(
        desired.get("adoption"), Mapping
    )


def _channel_apply_payload(
    managed_uid: str,
    spec: ManagedChannelSpec,
    supplier: Mapping[str, str],
) -> dict[str, Any]:
    return {
        "supplier": dict(supplier),
        "channel": {
            "name": spec.channel_name.strip(),
            "nickname": spec.channel_nickname.strip(),
            "channel_type": spec.channel_type,
            "base_url": spec.base_url.strip(),
            "api_key": spec.api_key,
            "models": ",".join(spec.models),
            "description": _managed_channel_description(
                spec.channel_description,
                managed_uid,
            ),
        },
    }


def _managed_channel_description(description: str, managed_uid: str) -> str:
    signature = managed_resource_signature("channel", managed_uid)
    normalized = str(description or "").strip()
    return f"{normalized}\n{signature}" if normalized else signature


def _listing_desired_state(
    managed_uid: str,
    channel_id: int,
    spec: ManagedListingSpec,
    listings: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    payload = _listing_payload(managed_uid, channel_id, spec)
    return {
        "spec_fingerprint": _hash_document(payload),
        "marker": managed_marker(managed_uid),
        "remote_listing": payload,
        "baseline_listing_ids": sorted(_ids(listings)),
    }


def _listing_payload(
    managed_uid: str,
    channel_id: int,
    spec: ManagedListingSpec,
) -> dict[str, Any]:
    return {
        "channel_id": channel_id,
        "own_model": spec.own_model,
        "platform_model": spec.platform_model,
        "charge_type": spec.charge_type,
        "input_price_micros": spec.input_price_micros,
        "output_price_micros": spec.output_price_micros,
        "cache_read_price_micros": spec.cache_read_price_micros,
        "cache_write_price_micros": spec.cache_write_price_micros,
        "display_name": spec.display_name,
        "description": spec.description,
    }


def _listing_update_payload(spec: ManagedListingSpec) -> dict[str, Any]:
    return {
        "charge_type": spec.charge_type,
        "input_price_micros": spec.input_price_micros,
        "output_price_micros": spec.output_price_micros,
        "cache_read_price_micros": spec.cache_read_price_micros,
        "cache_write_price_micros": spec.cache_write_price_micros,
        "display_name": spec.display_name,
        "description": spec.description,
    }


def _rollback_listing_activation(
    client: ManagedListingsClient,
    user_id: int,
    listing_id: int,
) -> bool:
    try:
        client.takedown_supplier_listing(user_id, listing_id)
        remote = _item_by_id(_list_all_listings(client, user_id), listing_id)
    except Exception:
        return False
    return remote is not None and str(remote.get("status")) == "0"


def _new_channel_candidate(
    binding: ManagedChannelBinding,
    channels: Sequence[Mapping[str, Any]],
    requests: Sequence[Mapping[str, Any]],
) -> Mapping[str, Any] | None:
    baseline = {int(item) for item in binding.desired_state.get("baseline_channel_ids", [])}
    candidates = [
        item
        for item in channels
        if _positive_int(item.get("id"), required=False) not in baseline
        and _channel_matches(item, binding.desired_state)
    ]
    if len(candidates) > 1:
        raise ManagedListingError("managed_channel_readback_ambiguous")
    if not candidates:
        return None
    baseline_requests = {
        int(item) for item in binding.desired_state.get("baseline_request_ids", [])
    }
    expected_name = str(binding.desired_state["remote_channel"]["name"])
    request_id = _positive_int((binding.observed_state or {}).get("request_id"), required=False)
    request_confirmed = any(
        _request_confirms_channel(item, expected_name, request_id, baseline_requests)
        for item in requests
    )
    return candidates[0] if request_confirmed else None


def _new_listing_candidate(
    binding: ManagedListingBinding,
    listings: Sequence[Mapping[str, Any]],
) -> Mapping[str, Any] | None:
    baseline = {int(item) for item in binding.desired_state.get("baseline_listing_ids", [])}
    candidates = [
        item
        for item in listings
        if _positive_int(item.get("id"), required=False) not in baseline
        and _listing_matches(item, binding.desired_state)
    ]
    if len(candidates) > 1:
        raise ManagedListingError("managed_listing_readback_ambiguous")
    return candidates[0] if candidates else None


def _channel_matches(item: Mapping[str, Any], desired: Mapping[str, Any]) -> bool:
    remote = desired.get("remote_channel")
    if not isinstance(remote, Mapping):
        return False
    keys = ("name", "nickname", "channel_type", "description")
    return all(item.get(key) == remote.get(key) for key in keys)


def _listing_matches(item: Mapping[str, Any], desired: Mapping[str, Any]) -> bool:
    remote = desired.get("remote_listing")
    return isinstance(remote, Mapping) and _listing_mapping_matches(item, remote)


def _channel_owned(
    item: Mapping[str, Any],
    managed_uid: str,
    desired_state: Mapping[str, Any] | None = None,
    requests: Sequence[Mapping[str, Any]] = (),
) -> bool:
    channel_name = managed_channel_name(managed_uid)
    signature = managed_resource_signature("channel", managed_uid)
    description = str(item.get("description") or "")
    signed = description == signature or description.endswith(f"\n{signature}")
    current = item.get("name") == channel_name and signed
    desired = _channel_matches(item, desired_state or {})
    legacy = managed_marker(managed_uid) in str(item.get("name") or "") and managed_uid in str(
        item.get("description") or ""
    )
    adoption = (desired_state or {}).get("adoption")
    if not isinstance(adoption, Mapping):
        return signed or current or desired or legacy
    channel = adoption.get("channel")
    if (desired_state or {}).get("channel_strategy") == "adopt_existing":
        fingerprint = adoption.get("fingerprint")
        return (
            isinstance(channel, Mapping)
            and isinstance(fingerprint, str)
            and _hash_document(channel) == fingerprint
            and _mapping_matches(item, channel)
        )
    request = adoption.get("request")
    guards = adoption.get("request_guards", [])
    fingerprint = adoption.get("fingerprint")
    if (
        not isinstance(channel, Mapping)
        or not isinstance(request, Mapping)
        or not isinstance(guards, list)
        or any(not isinstance(guard, Mapping) for guard in guards)
        or not isinstance(fingerprint, str)
        or _hash_document({"channel": channel, "request": request, "request_guards": guards})
        != fingerprint
        or not _mapping_matches(item, channel)
    ):
        return False
    matches = [candidate for candidate in requests if _mapping_matches(candidate, request)]
    if len(matches) != 1:
        return False
    return all(
        len([candidate for candidate in requests if _mapping_matches(candidate, guard)]) == 1
        for guard in guards
    )


def _listing_owned(
    item: Mapping[str, Any],
    binding: ManagedListingBinding,
) -> bool:
    remote = binding.desired_state.get("remote_listing")
    if not isinstance(remote, Mapping):
        return False
    listing_id = _positive_int(item.get("id"), required=False)
    channel_id = _positive_int(item.get("channel_id"), required=False)
    return (
        listing_id == binding.a6_listing_id
        and channel_id == binding.a6_channel_id
        and item.get("own_model") == remote.get("own_model")
        and item.get("platform_model") == remote.get("platform_model")
    )


def _request_confirms_channel(
    item: Mapping[str, Any],
    expected_name: str,
    expected_id: int,
    baseline_ids: set[int],
) -> bool:
    item_id = _positive_int(item.get("id"), required=False)
    if not item_id or item_id in baseline_ids:
        return False
    if expected_id:
        return item_id == expected_id
    nested = item.get("channel")
    names = [item.get("name"), item.get("channel_name")]
    if isinstance(nested, Mapping):
        names.append(nested.get("name"))
    return any(str(name or "") == expected_name for name in names)


def _listing_action_satisfied(
    remote: Mapping[str, Any],
    operation: str,
    desired: Mapping[str, Any],
) -> bool:
    if operation == "update":
        payload = desired.get("payload")
        expected_status = desired.get("expected_status")
        return (
            isinstance(payload, Mapping)
            and _listing_mapping_matches(remote, payload)
            and (
                expected_status is None
                or str(remote.get("status")) == str(expected_status)
            )
        )
    if operation in {"takedown", "resubmit"}:
        return str(remote.get("status")) == str(desired.get("expected_status"))
    return False


def _channel_action_satisfied(
    remote: Mapping[str, Any],
    desired: Mapping[str, Any],
) -> bool:
    if desired.get("operation") != "ensure_upstream":
        return _mapping_matches(remote, desired)
    evidence = _channel_upstream_evidence(remote)
    if not all(
        evidence.get(key) == desired.get(key)
        for key in (
            "upstream_base_url_sha256",
            "api_key_last4",
            "has_api_key",
        )
    ):
        return False
    observed_key_hash = evidence.get("upstream_api_key_sha256")
    return observed_key_hash in {None, desired.get("upstream_api_key_sha256")}


def _channel_action_observation(
    remote: Mapping[str, Any],
    desired: Mapping[str, Any],
) -> dict[str, Any]:
    observation = _safe_remote_item(remote)
    if desired.get("operation") == "ensure_upstream":
        observation.update(_channel_upstream_evidence(remote))
        observation["required_model"] = desired.get("required_model")
    return observation


def _channel_upstream_evidence(remote: Mapping[str, Any]) -> dict[str, Any]:
    base_url = _normalize_upstream_base_url(remote.get("base_url"))
    raw_key = str(remote.get("api_key") or "")
    key_last4 = str(remote.get("api_key_last4") or (raw_key[-4:] if raw_key else ""))
    has_api_key = remote.get("has_api_key")
    if not isinstance(has_api_key, bool):
        has_api_key = bool(key_last4 or raw_key)
    evidence: dict[str, Any] = {
        "upstream_base_url_sha256": _text_sha256(base_url) if base_url else "",
        "api_key_last4": key_last4,
        "has_api_key": has_api_key,
    }
    if raw_key:
        evidence["upstream_api_key_sha256"] = _text_sha256(raw_key)
    return evidence


def _normalize_upstream_base_url(value: object) -> str:
    return str(value or "").strip().rstrip("/")


def _text_sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _mapping_matches(remote: Mapping[str, Any], expected: Mapping[str, Any]) -> bool:
    return all(remote.get(key) == value for key, value in expected.items())


def _listing_mapping_matches(remote: Mapping[str, Any], expected: Mapping[str, Any]) -> bool:
    if _mapping_matches(remote, expected):
        return True
    price_keys = (
        "input_price_micros",
        "output_price_micros",
        "cache_read_price_micros",
        "cache_write_price_micros",
    )
    if not all(
        isinstance(expected.get(key), int) and not isinstance(expected.get(key), bool)
        for key in price_keys
    ):
        return False
    input_price = int(expected["input_price_micros"])
    if input_price < 1:
        return False
    normalized = dict(expected)
    normalized.update(
        {
            "output_price_micros": input_price * 6,
            "cache_read_price_micros": (input_price + 5) // 10,
            "cache_write_price_micros": (input_price * 5 + 2) // 4,
        }
    )
    return _mapping_matches(remote, normalized)


def _list_all_listings(
    client: ManagedListingsClient,
    user_id: int,
    *,
    page_size: int = 1000,
    max_pages: int = 10,
) -> list[dict[str, Any]]:
    by_id: dict[int, dict[str, Any]] = {}
    expected_total: int | None = None
    for page in range(1, max_pages + 1):
        response = client.supplier_listings(user_id, page=page, size=page_size)
        raw_items = response.get("items")
        if not isinstance(raw_items, list):
            raise ManagedListingError("managed_listing_inventory_invalid")
        total = response.get("total")
        if total is not None:
            try:
                parsed_total = int(total)
            except (TypeError, ValueError) as exc:
                raise ManagedListingError("managed_listing_inventory_invalid") from exc
            if parsed_total < 0 or (expected_total is not None and expected_total != parsed_total):
                raise ManagedListingError("managed_listing_inventory_invalid")
            expected_total = parsed_total
        for raw in raw_items:
            if isinstance(raw, Mapping):
                item_id = _positive_int(raw.get("id"), required=False)
                if item_id:
                    by_id[item_id] = dict(raw)
        if expected_total is not None and len(by_id) >= expected_total:
            return [by_id[item_id] for item_id in sorted(by_id)]
        if len(raw_items) < page_size:
            if expected_total is None or len(by_id) >= expected_total:
                return [by_id[item_id] for item_id in sorted(by_id)]
            raise ManagedListingError("managed_listing_inventory_incomplete")
    raise ManagedListingError("managed_listing_inventory_incomplete")


def _safe_mutation_observation(response: Mapping[str, Any]) -> dict[str, Any]:
    allowed = {"id", "request_id", "supplier_id", "status"}
    return {key: response[key] for key in allowed if key in response}


def _known_failure_observation(error_class: str) -> dict[str, Any]:
    return {"result": "rejected", "error_class": str(error_class)}


def _current_supplier_payload(
    merchant: ManagedMerchant,
    profile: Mapping[str, Any],
) -> dict[str, str]:
    if not isinstance(profile, Mapping):
        raise ManagedListingError("managed_supplier_profile_invalid")
    supplier_id = _positive_int(profile.get("id"), required=False)
    user_id = _positive_int(profile.get("user_id"), required=False)
    if (
        not supplier_id
        or merchant.supplier_id != supplier_id
        or (user_id and user_id != merchant.a6_user_id)
    ):
        raise ManagedListingError("managed_supplier_profile_identity_mismatch")
    name = profile.get("name")
    contact_info = profile.get("contact_info")
    description = profile.get("description", "")
    if (
        not isinstance(name, str)
        or not name.strip()
        or not isinstance(contact_info, str)
        or not contact_info.strip()
        or not isinstance(description, str)
    ):
        raise ManagedListingError("managed_supplier_profile_invalid")
    return {
        "name": name.strip(),
        "contact_info": contact_info.strip(),
        "description": description.strip(),
    }


def _absent_create_evidence(
    binding: ManagedChannelBinding | ManagedListingBinding,
    desired: Mapping[str, Any],
    resources: Sequence[Mapping[str, Any]],
    requests: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    baseline_key = (
        "baseline_channel_ids"
        if isinstance(binding, ManagedChannelBinding)
        else "baseline_listing_ids"
    )
    baseline = {int(item) for item in binding.desired_state.get(baseline_key, [])}
    current = sorted(_ids(resources))
    request_baseline = {int(item) for item in binding.desired_state.get("baseline_request_ids", [])}
    kind = "channel" if isinstance(binding, ManagedChannelBinding) else "listing"
    candidate_resources = [
        item
        for item in resources
        if _positive_int(item.get("id"), required=False) not in baseline
        and _create_document_matches(binding, desired, item, kind)
    ]
    candidate_requests = [
        item
        for item in requests
        if _positive_int(item.get("id"), required=False) not in request_baseline
        and _create_document_matches(binding, desired, item, kind)
    ]
    if candidate_resources or candidate_requests:
        raise ManagedListingError(f"managed_{kind}_create_unknown_remote_candidate")
    return {
        "result": "confirmed_absent",
        "resource_kind": kind,
        "baseline_ids": sorted(baseline),
        "current_ids": current,
        "new_ids": sorted(set(current) - baseline),
        "baseline_request_ids": sorted(request_baseline),
        "current_request_ids": sorted(_ids(requests)),
        "matching_candidates": 0,
    }


def _create_document_matches(
    binding: ManagedChannelBinding | ManagedListingBinding,
    desired: Mapping[str, Any],
    document: Mapping[str, Any],
    resource_kind: str,
) -> bool:
    remote_key = "remote_channel" if resource_kind == "channel" else "remote_listing"
    old_remote = binding.desired_state.get(remote_key)
    new_remote = desired.get(remote_key)
    tokens = {
        binding.managed_uid,
        managed_marker(binding.managed_uid),
        managed_resource_signature(resource_kind, binding.managed_uid),
    }
    for remote in (old_remote, new_remote):
        if not isinstance(remote, Mapping):
            continue
        if resource_kind == "channel" and _channel_matches(document, {remote_key: remote}):
            return True
        if resource_kind == "listing" and _listing_matches(document, {remote_key: remote}):
            return True
        for key in ("name", "description"):
            value = remote.get(key)
            if isinstance(value, str) and value:
                tokens.add(value)
    return any(_document_contains_text(document, token) for token in tokens)


def _document_contains_text(value: Any, expected: str) -> bool:
    if isinstance(value, Mapping):
        return any(_document_contains_text(item, expected) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_document_contains_text(item, expected) for item in value)
    return str(value or "") == expected or expected in str(value or "")


def _create_result_unknown(exc: Exception) -> bool:
    code = str(exc)
    return code in {"a6_transport_failed", "a6_response_invalid"} or not isinstance(
        exc, (ManagedListingError, A6AutoError)
    )


def _safe_remote_item(item: Mapping[str, Any]) -> dict[str, Any]:
    forbidden = {"api_key", "key", "secret", "token", "base_url", "contact_info"}

    def scrub(value: Any) -> Any:
        if isinstance(value, Mapping):
            return {
                str(key): scrub(nested)
                for key, nested in value.items()
                if str(key).lower() not in forbidden
            }
        if isinstance(value, (list, tuple)):
            return [scrub(nested) for nested in value]
        return value

    return scrub(item)


def _decision(
    merchant: ManagedMerchant,
    kind: str,
    uid: str,
    action: str,
    state: str,
    applied: bool,
    *,
    external_id: int | None = None,
    error_class: str | None = None,
) -> ManagedDecision:
    return ManagedDecision(
        merchant_id=merchant.merchant_id,
        resource_kind=kind,
        managed_uid=uid,
        action=action,
        state=state,
        applied=applied,
        external_id=external_id,
        error_class=error_class,
    )


def _action_key(
    uid: str,
    operation: str,
    desired: Mapping[str, Any],
    observed: Mapping[str, Any],
) -> str:
    transition = {"desired": desired, "observed": _safe_remote_item(observed)}
    return f"managed-action:{uid}:{operation}:{_hash_document(transition)[:20]}"


def _assert_spec_fingerprint(persisted: Mapping[str, Any], desired: Mapping[str, Any]) -> None:
    if persisted.get("spec_fingerprint") != desired.get("spec_fingerprint"):
        raise ManagedListingError("managed_resource_spec_conflict")


def _assert_binding_owner(actual: int, expected: int) -> None:
    if actual != expected:
        raise ManagedListingError("managed_resource_owner_conflict")


def _ids(items: Sequence[Mapping[str, Any]]) -> set[int]:
    return {
        item_id for item in items if (item_id := _positive_int(item.get("id"), required=False)) > 0
    }


def _item_by_id(items: Sequence[Mapping[str, Any]], item_id: int) -> Mapping[str, Any] | None:
    matches = [item for item in items if _positive_int(item.get("id"), required=False) == item_id]
    if len(matches) > 1:
        raise ManagedListingError("managed_remote_id_ambiguous")
    return matches[0] if matches else None


def _positive_int(value: Any, *, required: bool = True) -> int:
    if isinstance(value, bool):
        parsed = 0
    else:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            parsed = 0
    if required and parsed < 1:
        raise ManagedListingError("managed_remote_id_invalid")
    return parsed if parsed > 0 else 0


def _hash_document(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def _validate_logical_key(value: str) -> None:
    if not _LOGICAL_KEY.fullmatch(str(value)):
        raise ManagedListingError("managed_logical_key_invalid")


def _validate_apply(value: bool) -> None:
    if not isinstance(value, bool):
        raise ManagedListingError("managed_apply_invalid")


def _error_code(exc: Exception, fallback: str) -> str:
    if isinstance(exc, ManagedListingError):
        return exc.code
    if isinstance(exc, A6AutoError):
        return str(exc) or fallback
    return fallback
