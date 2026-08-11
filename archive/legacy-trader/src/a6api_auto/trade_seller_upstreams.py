from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import A6AutoError
from .models import MerchantTemplate, SessionRecord
from .storage import SessionStore
from .templates import choose_account_template, choose_template
from .trade_enodot_seller_resources import ManagedSellerSecretSink, ManagedSellerStore
from .trade_managed_listings import (
    SUPPORTED_MODEL,
    ManagedChannelSpec,
    ManagedListingController,
    ManagedListingError,
    ManagedListingStore,
    ManagedMerchant,
    managed_marker,
    stable_managed_uid,
)
from .trade_seller_orchestration import SellerA6Client, SellerOrchestrationError

_SELLER_STATUS = "supplier_submitted"
_SELLER_DUTIES = frozenset({"provider", "observer"})
_CHANNEL_RECONCILE_STATES = frozenset({"verified", "updating", "update_unknown"})
_LISTING_SNAPSHOT_KEYS = (
    "id",
    "channel_id",
    "status",
    "charge_type",
    "input_price_micros",
    "output_price_micros",
    "cache_read_price_micros",
    "cache_write_price_micros",
    "display_name",
    "description",
    "own_model",
    "platform_model",
)
_LISTING_PRICE_KEYS = (
    "input_price_micros",
    "output_price_micros",
    "cache_read_price_micros",
    "cache_write_price_micros",
)


@dataclass(frozen=True)
class SellerUpstreamBatchRequest:
    merchant_ids: tuple[int, ...]
    model: str = SUPPORTED_MODEL
    seller_base_url: str = "https://ai.enodot.shop/v1"
    channel_logical_key: str = "seller-primary"
    contact_info: str = "a6-trader"

    def __post_init__(self) -> None:
        merchant_ids = tuple(dict.fromkeys(_positive_int(item) for item in self.merchant_ids))
        if not merchant_ids:
            raise SellerOrchestrationError("seller_upstream_merchants_empty")
        if self.model != SUPPORTED_MODEL:
            raise SellerOrchestrationError("seller_upstream_model_unsupported")
        if not str(self.seller_base_url).strip():
            raise SellerOrchestrationError("seller_upstream_base_url_invalid")
        if not str(self.channel_logical_key).strip() or not str(self.contact_info).strip():
            raise SellerOrchestrationError("seller_upstream_request_invalid")
        object.__setattr__(self, "merchant_ids", merchant_ids)


@dataclass(frozen=True)
class SellerUpstreamMerchantResult:
    merchant_id: int
    a6_user_id: int | None
    managed_uid: str
    channel_binding_id: int | None
    a6_channel_id: int | None
    a6_channel_binding_id: int | None
    action: str
    state: str
    applied: bool
    base_url_sha256: str
    remote_base_url_sha256: str
    base_url_compliant: bool
    api_key_last4: str
    remote_api_key_last4: str
    api_key_compliant: bool
    has_api_key: bool
    model_verified: bool
    listing: Mapping[str, Any]
    error_class: str | None = None

    @property
    def verified(self) -> bool:
        return bool(
            self.error_class is None
            and self.state == "verified"
            and self.base_url_compliant
            and self.api_key_compliant
            and self.has_api_key
            and self.model_verified
            and self.listing.get("unchanged") is True
        )

    def public(self) -> dict[str, Any]:
        return {
            "merchant_id": self.merchant_id,
            "a6_user_id": self.a6_user_id,
            "managed_uid": self.managed_uid,
            "channel_binding_id": self.channel_binding_id,
            "a6_channel_id": self.a6_channel_id,
            "a6_channel_binding_id": self.a6_channel_binding_id,
            "action": self.action,
            "state": self.state,
            "applied": self.applied,
            "verified": self.verified,
            "base_url_sha256": self.base_url_sha256,
            "remote_base_url_sha256": self.remote_base_url_sha256,
            "base_url_compliant": self.base_url_compliant,
            "api_key_last4": self.api_key_last4,
            "remote_api_key_last4": self.remote_api_key_last4,
            "api_key_compliant": self.api_key_compliant,
            "has_api_key": self.has_api_key,
            "model_verified": self.model_verified,
            "listing": dict(self.listing),
            "error_class": self.error_class,
        }


@dataclass(frozen=True)
class SellerUpstreamBatchResult:
    apply: bool
    model: str
    expected_base_url_sha256: str
    merchants: tuple[SellerUpstreamMerchantResult, ...]

    def summary(self) -> dict[str, Any]:
        failed = sum(item.error_class is not None for item in self.merchants)
        verified = sum(item.verified for item in self.merchants)
        repaired = sum(
            item.action in {"update", "retry_update"} and item.verified
            for item in self.merchants
        )
        listing_changes = sum(
            item.listing.get("unchanged") is False for item in self.merchants
        )
        return {
            "success": failed == 0 and (not self.apply or verified == len(self.merchants)),
            "mode": "applied" if self.apply else "dry-run",
            "operation": "reconcile_seller_upstreams",
            "model": self.model,
            "expected_base_url_sha256": self.expected_base_url_sha256,
            "merchant_count": len(self.merchants),
            "verified_count": verified,
            "repaired_count": repaired,
            "noop_count": sum(item.action == "noop" for item in self.merchants),
            "failed": failed,
            "listing_changes": listing_changes,
            "merchants": [item.public() for item in self.merchants],
        }


class SellerUpstreamReconciler:
    """Independently verify bound Seller channel URLs and per-Seller Keys."""

    def __init__(
        self,
        seller_store: ManagedSellerStore,
        secret_sink: ManagedSellerSecretSink,
        listing_store: ManagedListingStore,
        a6_client_factory: Callable[[Any, SessionRecord], SellerA6Client],
        *,
        session_loader: Callable[[Path], SessionRecord] | None = None,
        template_loader: Callable[[str], MerchantTemplate] = choose_template,
        listing_controller_factory: Callable[
            [ManagedListingStore, SellerA6Client], ManagedListingController
        ]
        | None = None,
    ) -> None:
        self.seller_store = seller_store
        self.secret_sink = secret_sink
        self.listing_store = listing_store
        self.a6_client_factory = a6_client_factory
        self.session_loader = session_loader or (lambda path: SessionStore(path).load())
        self.template_loader = template_loader
        self.listing_controller_factory = listing_controller_factory or (
            lambda store, client: ManagedListingController(store, lambda _merchant: client)
        )

    def execute(
        self,
        request: SellerUpstreamBatchRequest,
        *,
        apply: bool = False,
    ) -> SellerUpstreamBatchResult:
        if not isinstance(apply, bool):
            raise SellerOrchestrationError("seller_upstream_apply_invalid")
        results = tuple(
            self._reconcile_one(merchant_id, request, apply=apply)
            for merchant_id in request.merchant_ids
        )
        return SellerUpstreamBatchResult(
            apply=apply,
            model=request.model,
            expected_base_url_sha256=_sha256(_normalize_url(request.seller_base_url)),
            merchants=results,
        )

    def _reconcile_one(
        self,
        merchant_id: int,
        request: SellerUpstreamBatchRequest,
        *,
        apply: bool,
    ) -> SellerUpstreamMerchantResult:
        managed_uid = stable_managed_uid(
            "channel", merchant_id, request.channel_logical_key
        )
        a6_user_id: int | None = None
        channel_binding_id: int | None = None
        a6_channel_id: int | None = None
        expected_url_hash = _sha256(_normalize_url(request.seller_base_url))
        try:
            context = self.seller_store.load_context(merchant_id)
            merchant = _managed_merchant(context, merchant_id)
            a6_user_id = merchant.a6_user_id
            session = self.session_loader(Path(context.session_ref))
            _validate_session(context, session)
            template_id = session.template_id or choose_account_template(
                "", a6_user_id=merchant.a6_user_id
            ).template_id
            template = self.template_loader(template_id)
            seller_binding = self.seller_store.get_binding(merchant_id)
            if (
                seller_binding is None
                or seller_binding.control_state != "bound"
                or not seller_binding.key_secret_ref
                or not seller_binding.api_key_fingerprint
            ):
                raise SellerOrchestrationError("seller_upstream_seller_not_bound")
            api_key = self.secret_sink.read_api_key(
                seller_binding.managed_uid,
                seller_binding.key_secret_ref,
            )
            if hashlib.sha256(api_key.encode()).hexdigest() != seller_binding.api_key_fingerprint:
                raise SellerOrchestrationError("seller_secret_fingerprint_mismatch")
            channel_binding = self.listing_store.get_channel(managed_uid)
            if (
                channel_binding is None
                or channel_binding.merchant_id != merchant_id
                or channel_binding.control_state not in _CHANNEL_RECONCILE_STATES
                or not channel_binding.a6_channel_id
            ):
                raise SellerOrchestrationError("seller_upstream_channel_not_bound")
            channel_binding_id = channel_binding.binding_id
            a6_channel_id = channel_binding.a6_channel_id
            client = self.a6_client_factory(context, session)
            _required_remote_channel(
                client.supplier_channels(merchant.a6_user_id),
                a6_channel_id,
            )
            listing_binding = self.listing_store.get_listing(
                stable_managed_uid(
                    "listing",
                    merchant_id,
                    f"{request.model}:{managed_marker(managed_uid)}",
                )
            )
            listing_id = (
                int(listing_binding.a6_listing_id)
                if listing_binding is not None and listing_binding.a6_listing_id
                else None
            )
            listing_before = _read_listing_snapshot(client, merchant.a6_user_id, listing_id)
            models = client.fetch_models_via_a6(
                merchant.a6_user_id,
                request.seller_base_url,
                api_key,
            )
            if request.model not in models:
                raise ManagedListingError("managed_channel_upstream_model_missing")
            spec = ManagedChannelSpec(
                logical_key=request.channel_logical_key,
                supplier_name=template.supplier_name,
                contact_info=request.contact_info,
                channel_name=template.channel_name,
                channel_nickname=template.channel_nickname,
                base_url=request.seller_base_url,
                api_key=api_key,
                supplier_description=template.supplier_description,
                channel_description=template.channel_description,
                models=(request.model,),
                channel_type=template.channel_type,
            )
            controller = self.listing_controller_factory(self.listing_store, client)
            decision = controller.ensure_channel_upstream(
                merchant,
                managed_uid,
                spec,
                model=request.model,
                apply=apply,
            )
            remote_after = _required_remote_channel(
                client.supplier_channels(merchant.a6_user_id),
                a6_channel_id,
            )
            listing_after = _read_listing_snapshot(client, merchant.a6_user_id, listing_id)
            listing = _listing_result(listing_binding, listing_before, listing_after)
            remote_url_hash = _sha256(_normalize_url(remote_after.get("base_url")))
            expected_last4 = api_key[-4:]
            remote_last4 = str(remote_after.get("api_key_last4") or "")
            has_api_key = remote_after.get("has_api_key") is True
            base_url_compliant = remote_url_hash == expected_url_hash
            api_key_compliant = has_api_key and remote_last4 == expected_last4
            error_class = None
            if apply and (
                decision.state != "verified"
                or not base_url_compliant
                or not api_key_compliant
                or listing.get("unchanged") is not True
            ):
                error_class = "seller_upstream_verification_failed"
            return SellerUpstreamMerchantResult(
                merchant_id=merchant_id,
                a6_user_id=a6_user_id,
                managed_uid=managed_uid,
                channel_binding_id=channel_binding_id,
                a6_channel_id=a6_channel_id,
                a6_channel_binding_id=_optional_positive_int(
                    remote_after.get("binding_id")
                ),
                action=decision.action,
                state=decision.state,
                applied=decision.applied,
                base_url_sha256=expected_url_hash,
                remote_base_url_sha256=remote_url_hash,
                base_url_compliant=base_url_compliant,
                api_key_last4=expected_last4,
                remote_api_key_last4=remote_last4,
                api_key_compliant=api_key_compliant,
                has_api_key=has_api_key,
                model_verified=True,
                listing=listing,
                error_class=error_class,
            )
        except Exception as exc:
            return SellerUpstreamMerchantResult(
                merchant_id=merchant_id,
                a6_user_id=a6_user_id,
                managed_uid=managed_uid,
                channel_binding_id=channel_binding_id,
                a6_channel_id=a6_channel_id,
                a6_channel_binding_id=None,
                action="error",
                state="error",
                applied=False,
                base_url_sha256=expected_url_hash,
                remote_base_url_sha256="",
                base_url_compliant=False,
                api_key_last4="",
                remote_api_key_last4="",
                api_key_compliant=False,
                has_api_key=False,
                model_verified=False,
                listing={"managed": False, "unchanged": True},
                error_class=_error_code(exc),
            )


def _managed_merchant(context: Any, merchant_id: int) -> ManagedMerchant:
    roles = tuple(getattr(context, "roles", ()) or ())
    valid_roles = roles == ("seller",) or (
        len(roles) == 2 and roles[0] == "seller" and roles[1] in _SELLER_DUTIES
    )
    if (
        getattr(context, "merchant_id", None) != merchant_id
        or getattr(context, "merchant_status", None) != _SELLER_STATUS
        or not valid_roles
        or isinstance(getattr(context, "a6_user_id", None), bool)
        or not isinstance(getattr(context, "a6_user_id", None), int)
        or context.a6_user_id < 1
        or isinstance(getattr(context, "supplier_id", None), bool)
        or not isinstance(getattr(context, "supplier_id", None), int)
        or context.supplier_id < 1
    ):
        raise SellerOrchestrationError("seller_upstream_context_ineligible")
    return ManagedMerchant(
        merchant_id=merchant_id,
        a6_user_id=context.a6_user_id,
        supplier_id=context.supplier_id,
        roles=roles,
    )


def _validate_session(context: Any, session: SessionRecord) -> None:
    if (
        session.status != _SELLER_STATUS
        or session.user_id != context.a6_user_id
        or session.supplier_id != context.supplier_id
        or not session.origin
        or not session.cookies
    ):
        raise SellerOrchestrationError("seller_session_context_mismatch")


def _required_remote_channel(
    channels: list[dict[str, Any]],
    channel_id: int,
) -> Mapping[str, Any]:
    matches = [item for item in channels if _optional_positive_int(item.get("id")) == channel_id]
    if len(matches) != 1:
        raise SellerOrchestrationError("seller_upstream_channel_readback_failed")
    return matches[0]


def _read_listing_snapshot(
    client: SellerA6Client,
    user_id: int,
    listing_id: int | None,
) -> Mapping[str, Any] | None:
    if listing_id is None:
        return None
    response = client.supplier_listings(user_id, page=1, size=1000)
    items = response.get("items")
    if not isinstance(items, list):
        raise SellerOrchestrationError("seller_upstream_listing_readback_failed")
    matches = [
        item
        for item in items
        if isinstance(item, Mapping) and _optional_positive_int(item.get("id")) == listing_id
    ]
    if len(matches) != 1:
        raise SellerOrchestrationError("seller_upstream_listing_readback_failed")
    return {key: matches[0].get(key) for key in _LISTING_SNAPSHOT_KEYS}


def _listing_result(
    binding: Any,
    before: Mapping[str, Any] | None,
    after: Mapping[str, Any] | None,
) -> dict[str, Any]:
    if binding is None:
        return {"managed": False, "unchanged": True}
    unchanged = before is not None and before == after
    return {
        "managed": True,
        "binding_id": binding.binding_id,
        "a6_listing_id": binding.a6_listing_id,
        "status_before": before.get("status") if before else None,
        "status_after": after.get("status") if after else None,
        "prices_before": {
            key: before.get(key) if before else None for key in _LISTING_PRICE_KEYS
        },
        "prices_after": {
            key: after.get(key) if after else None for key in _LISTING_PRICE_KEYS
        },
        "fingerprint_before": _document_hash(before) if before else "",
        "fingerprint_after": _document_hash(after) if after else "",
        "unchanged": unchanged,
    }


def _positive_int(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise SellerOrchestrationError("seller_upstream_merchant_id_invalid")
    return value


def _optional_positive_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _normalize_url(value: Any) -> str:
    return str(value or "").strip().rstrip("/")


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _document_hash(value: Mapping[str, Any]) -> str:
    payload = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return _sha256(payload)


def _error_code(exc: Exception) -> str:
    code = getattr(exc, "code", None)
    if isinstance(code, str) and code:
        return code
    if isinstance(exc, A6AutoError):
        value = str(exc)
        if value and value.replace("_", "").isalnum():
            return value
    return f"seller_upstream_{type(exc).__name__.lower()}"
