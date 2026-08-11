from __future__ import annotations

import hashlib
import math
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, wait
from contextlib import contextmanager
from dataclasses import dataclass, replace
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Protocol

from .errors import A6AutoError
from .models import MerchantTemplate, SessionRecord
from .storage import SessionStore
from .templates import choose_account_template, choose_template
from .trade_enodot_seller_resources import (
    ManagedSellerBinding,
    ManagedSellerError,
    ManagedSellerRequest,
    ManagedSellerResult,
    ManagedSellerSecretSink,
    ManagedSellerService,
    ManagedSellerStore,
)
from .trade_managed_listings import (
    SUPPORTED_MODEL,
    ManagedChannelSpec,
    ManagedListingController,
    ManagedListingError,
    ManagedListingSpec,
    ManagedListingStore,
    ManagedMerchant,
    is_source_only_listing_description,
    managed_channel_name,
    managed_marker,
    managed_resource_signature,
    stable_managed_uid,
)

_SELLER_STATUS = "supplier_submitted"
_DUTIES = frozenset({"provider", "observer"})
_CHANNEL_EXISTING_STATES = frozenset({"verified", "updating", "update_unknown"})
_CREATE_RECONCILE_STATES = frozenset({"creating", "create_unknown", "verifying"})
_TAKEDOWN_RECONCILE_STATES = frozenset({"taking_down", "takedown_unknown"})
_LISTING_EXISTING_STATES = frozenset(
    {
        "verified",
        "updating",
        "update_unknown",
        "taking_down",
        "takedown_unknown",
        "resubmitting",
        "resubmit_unknown",
    }
)
# A6 ``official_price`` fields (input/output/cache read/cache write per 1M tokens).
# These values are model-specific: the lower Terra tuple in the HAR is not a Sol unit scale.
MODEL_OFFICIAL_PRICES = {
    "gpt-5.6-sol": (5_000_000, 30_000_000, 500_000, 6_250_000),
    "gpt-5.6-terra": (500_000, 3_000_000, 50_000, 625_000),
}
GPT_5_6_SOL_OFFICIAL_PRICES = MODEL_OFFICIAL_PRICES[SUPPORTED_MODEL]


class SellerOrchestrationError(A6AutoError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class SellerA6Client(Protocol):
    def supplier_me(self, user_id: int) -> dict[str, Any]: ...

    def supplier_channels(self, user_id: int) -> list[dict[str, Any]]: ...

    def supplier_requests(self, user_id: int) -> list[dict[str, Any]]: ...

    def supplier_listings(self, user_id: int, **kwargs: Any) -> dict[str, Any]: ...

    def fetch_models_via_a6(self, user_id: int, base_url: str, api_key: str) -> list[str]: ...

    def apply_supplier(
        self, user_id: int, payload: Mapping[str, Any]
    ) -> dict[str, Any]: ...

    def update_supplier_channel(
        self, user_id: int, channel_id: int, payload: Mapping[str, Any]
    ) -> dict[str, Any]: ...

    def create_supplier_listings(
        self, user_id: int, listings: Sequence[Mapping[str, Any]]
    ) -> dict[str, Any]: ...

    def update_supplier_listing(
        self, user_id: int, listing_id: int, payload: Mapping[str, Any]
    ) -> dict[str, Any]: ...


@dataclass(frozen=True)
class _PreparedSeller:
    context: Any
    merchant: ManagedMerchant
    session: SessionRecord
    template: MerchantTemplate
    client: SellerA6Client


@dataclass(frozen=True)
class SellerPricing:
    cost_multiplier: Decimal | str | float
    delta: Decimal | str | float
    margin: Decimal | str | float = Decimal("0.20")
    conversion_factor: Decimal | str | float = Decimal("0.833333333333")
    headroom: Decimal | str | float = Decimal("0")
    maximum_price_multiplier: Decimal | str | float = Decimal("1")
    price_multiplier: Decimal | str | float | None = None
    input_price_micros: int = GPT_5_6_SOL_OFFICIAL_PRICES[0]
    output_price_micros: int = GPT_5_6_SOL_OFFICIAL_PRICES[1]
    cache_read_price_micros: int = GPT_5_6_SOL_OFFICIAL_PRICES[2]
    cache_write_price_micros: int = GPT_5_6_SOL_OFFICIAL_PRICES[3]

    def __post_init__(self) -> None:
        cost = _positive_decimal(self.cost_multiplier, "seller_cost_multiplier_invalid")
        delta = _bounded_decimal(self.delta, Decimal("0"), Decimal("0.5"), "seller_delta_invalid")
        margin = _bounded_decimal(self.margin, Decimal("0"), Decimal("10"), "seller_margin_invalid")
        conversion = _bounded_decimal(
            self.conversion_factor,
            Decimal("0.000000000001"),
            Decimal("1"),
            "seller_conversion_factor_invalid",
        )
        headroom = _bounded_decimal(
            self.headroom,
            Decimal("0"),
            Decimal("10"),
            "seller_price_headroom_invalid",
        )
        maximum = _positive_decimal(
            self.maximum_price_multiplier,
            "seller_maximum_price_multiplier_invalid",
        )
        landed_cost = cost / conversion
        final_floor = (
            (Decimal("1") + margin)
            * landed_cost
            * (Decimal("1") + headroom)
        )
        minimum = final_floor / (Decimal("1") + delta)
        if self.price_multiplier is None:
            selected = minimum
        else:
            selected = _positive_decimal(
                self.price_multiplier,
                "seller_price_multiplier_invalid",
            )
        if selected < minimum:
            raise SellerOrchestrationError("seller_profit_floor_violated")
        if selected > maximum:
            raise SellerOrchestrationError("seller_price_infeasible")
        base_prices = (
            self.input_price_micros,
            self.output_price_micros,
            self.cache_read_price_micros,
            self.cache_write_price_micros,
        )
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in base_prices
        ):
            raise SellerOrchestrationError("seller_base_prices_invalid")
        object.__setattr__(self, "cost_multiplier", cost)
        object.__setattr__(self, "delta", delta)
        object.__setattr__(self, "margin", margin)
        object.__setattr__(self, "conversion_factor", conversion)
        object.__setattr__(self, "headroom", headroom)
        object.__setattr__(self, "maximum_price_multiplier", maximum)
        object.__setattr__(self, "price_multiplier", selected)

    @property
    def minimum_price_multiplier(self) -> Decimal:
        return (
            (Decimal("1") + self.margin)
            * self.landed_cost_multiplier
            * (Decimal("1") + self.headroom)
            / (Decimal("1") + self.delta)
        )

    @property
    def landed_cost_multiplier(self) -> Decimal:
        return self.cost_multiplier / self.conversion_factor

    @property
    def market_multiplier(self) -> Decimal:
        return self.price_multiplier / self.conversion_factor

    @property
    def final_multiplier(self) -> Decimal:
        return (Decimal("1") + self.delta) * self.price_multiplier

    @property
    def prices(self) -> tuple[int, int, int, int]:
        input_price = int(
            (
                Decimal(self.input_price_micros) * self.price_multiplier
            ).to_integral_value(rounding=ROUND_CEILING)
        )
        return (input_price,) + tuple(
            int(
                (
                    Decimal(input_price)
                    * Decimal(value)
                    / Decimal(self.input_price_micros)
                ).to_integral_value(rounding=ROUND_HALF_UP)
            )
            for value in (
                self.output_price_micros,
                self.cache_read_price_micros,
                self.cache_write_price_micros,
            )
        )

    def public(self) -> dict[str, Any]:
        return {
            "cost_multiplier": _decimal_text(self.cost_multiplier),
            "landed_cost_multiplier": _decimal_text(self.landed_cost_multiplier),
            "delta": _decimal_text(self.delta),
            "margin": _decimal_text(self.margin),
            "conversion_factor": _decimal_text(self.conversion_factor),
            "headroom": _decimal_text(self.headroom),
            "minimum_price_multiplier": _decimal_text(self.minimum_price_multiplier),
            "price_multiplier": _decimal_text(self.price_multiplier),
            "market_multiplier": _decimal_text(self.market_multiplier),
            "maximum_price_multiplier": _decimal_text(self.maximum_price_multiplier),
            "final_multiplier": _decimal_text(self.final_multiplier),
            "prices_micros": dict(
                zip(
                    ("input", "output", "cache_read", "cache_write"),
                    self.prices,
                    strict=True,
                )
            ),
        }


@dataclass(frozen=True)
class SellerResourceBatchRequest:
    merchant_ids: tuple[int, ...]
    group_id: int
    model: str
    pricing: SellerPricing
    listing_description: str
    listing_mutation: str = "apply"
    listing_scope: str = "canonical"
    seller_base_url: str = "https://ai.enodot.shop/v1"
    channel_logical_key: str = "seller-primary"
    channel_strategy: str = "managed_create"
    existing_channel_id: int | None = None
    config_version: str = "phase1-seller-resources-v1"
    requested_by: str = "trade-ensure-seller-resources"
    contact_info: str = "a6-trader"

    def __post_init__(self) -> None:
        merchant_ids = tuple(
            dict.fromkeys(
                _positive_int(item, "seller_merchant_id_invalid")
                for item in self.merchant_ids
            )
        )
        if not merchant_ids:
            raise SellerOrchestrationError("seller_merchants_empty")
        group_id = _positive_int(self.group_id, "seller_group_id_invalid")
        model = str(self.model).strip()
        if model != SUPPORTED_MODEL:
            raise SellerOrchestrationError("seller_model_unsupported")
        if not str(self.seller_base_url).strip():
            raise SellerOrchestrationError("seller_base_url_invalid")
        listing_description = str(self.listing_description).strip()
        if not is_source_only_listing_description(listing_description):
            raise SellerOrchestrationError("seller_listing_description_invalid")
        if self.listing_mutation not in {"apply", "takedown", "release", "hold"}:
            raise SellerOrchestrationError("seller_listing_mutation_invalid")
        if self.listing_scope not in {"canonical", "all_controlled"}:
            raise SellerOrchestrationError("seller_listing_scope_invalid")
        if self.channel_strategy not in {"managed_create", "adopt_existing"}:
            raise SellerOrchestrationError("seller_channel_strategy_invalid")
        existing_channel_id = self.existing_channel_id
        if existing_channel_id is not None:
            existing_channel_id = _positive_int(
                existing_channel_id,
                "seller_existing_channel_id_invalid",
            )
            if self.channel_strategy != "adopt_existing":
                raise SellerOrchestrationError("seller_existing_channel_strategy_invalid")
        if not str(self.contact_info).strip():
            raise SellerOrchestrationError("seller_contact_info_invalid")
        object.__setattr__(self, "merchant_ids", merchant_ids)
        object.__setattr__(self, "group_id", group_id)
        object.__setattr__(self, "model", model)
        object.__setattr__(self, "listing_description", listing_description)
        object.__setattr__(self, "existing_channel_id", existing_channel_id)


@dataclass(frozen=True)
class SellerMerchantResourceResult:
    merchant_id: int
    a6_user_id: int | None
    roles: tuple[str, ...]
    seller: Mapping[str, Any]
    channel: Mapping[str, Any]
    listing: Mapping[str, Any]
    error_class: str | None = None

    def public(self) -> dict[str, Any]:
        readiness = (
            "not_ready" if self.seller.get("action") == "not_ready" else "ready"
        )
        return {
            "merchant_id": self.merchant_id,
            "a6_user_id": self.a6_user_id,
            "base_role": "seller" if "seller" in self.roles else None,
            "duty": next((role for role in self.roles if role in _DUTIES), None),
            "readiness": readiness,
            "seller": dict(self.seller),
            "channel": dict(self.channel),
            "listing": dict(self.listing),
            "error_class": self.error_class,
        }


@dataclass(frozen=True)
class SellerResourceBatchResult:
    apply: bool
    model: str
    pricing: SellerPricing
    merchants: tuple[SellerMerchantResourceResult, ...]

    def summary(self) -> dict[str, Any]:
        not_ready = sum(
            item.seller.get("action") == "not_ready" for item in self.merchants
        )
        failures = sum(item.error_class is not None for item in self.merchants)
        blocked = sum(
            decision.get("action") == "blocked"
            for item in self.merchants
            for decision in (item.channel, item.listing)
        )
        pending = sum(
            decision.get("action") == "reconcile_pending"
            or bool(decision.get("error_class"))
            for item in self.merchants
            for decision in (item.seller, item.channel, item.listing)
        )
        actionable_blocked = sum(
            decision.get("action") == "blocked"
            for item in self.merchants
            if item.seller.get("action") != "not_ready"
            for decision in (item.channel, item.listing)
        )
        success = (
            failures == 0
            and pending == 0
            and (not self.apply or actionable_blocked == 0)
        )
        return {
            "success": success,
            "mode": "applied" if self.apply else "dry-run",
            "model": self.model,
            "merchant_count": len(self.merchants),
            "ready_merchant_count": len(self.merchants) - not_ready,
            "not_ready_merchant_count": not_ready,
            "failed": failures,
            "blocked_resources": blocked,
            "pending_resources": pending,
            "pricing": self.pricing.public(),
            "merchants": [item.public() for item in self.merchants],
        }


@dataclass(frozen=True)
class SellerChannelRecoveryResult:
    apply: bool
    model: str
    merchant_id: int
    roles: tuple[str, ...]
    seller: Mapping[str, Any]
    channel: Mapping[str, Any]

    def summary(self) -> dict[str, Any]:
        error_class = self.channel.get("error_class")
        return {
            "success": error_class is None,
            "mode": "applied" if self.apply else "dry-run",
            "operation": "resolve_channel_create_unknown",
            "model": self.model,
            "merchant_id": self.merchant_id,
            "base_role": "seller",
            "duty": next((role for role in self.roles if role in _DUTIES), None),
            "seller": dict(self.seller),
            "channel": dict(self.channel),
        }


class SellerResourceOrchestrator:
    def __init__(
        self,
        seller_service: ManagedSellerService,
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
        activation_stagger_seconds: float = 0,
        max_workers: int = 1,
        round_time_budget_seconds: float | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if (
            isinstance(activation_stagger_seconds, bool)
            or not 0 <= activation_stagger_seconds <= 60
        ):
            raise SellerOrchestrationError("seller_activation_stagger_invalid")
        if not callable(sleeper):
            raise SellerOrchestrationError("seller_activation_sleeper_invalid")
        if (
            isinstance(max_workers, bool)
            or not isinstance(max_workers, int)
            or not 1 <= max_workers <= 32
        ):
            raise SellerOrchestrationError("seller_max_workers_invalid")
        if round_time_budget_seconds is not None and (
            isinstance(round_time_budget_seconds, bool)
            or not isinstance(round_time_budget_seconds, (int, float))
            or not math.isfinite(float(round_time_budget_seconds))
            or not 10 <= float(round_time_budget_seconds) <= 600
        ):
            raise SellerOrchestrationError("seller_round_time_budget_invalid")
        if max_workers > 1 and activation_stagger_seconds > 0:
            raise SellerOrchestrationError("seller_parallel_stagger_conflict")
        self.seller_service = seller_service
        self.seller_store = seller_store
        self.secret_sink = secret_sink
        self.listing_store = listing_store
        self.a6_client_factory = a6_client_factory
        self.session_loader = session_loader or (lambda path: SessionStore(path).load())
        self.template_loader = template_loader
        self.activation_stagger_seconds = float(activation_stagger_seconds)
        self.max_workers = max_workers
        self.round_time_budget_seconds = (
            None
            if round_time_budget_seconds is None
            else float(round_time_budget_seconds)
        )
        self.sleeper = sleeper
        self.listing_controller_factory = listing_controller_factory or (
            lambda store, client: ManagedListingController(store, lambda _merchant: client)
        )

    def execute(
        self,
        request: SellerResourceBatchRequest,
        *,
        apply: bool = False,
    ) -> SellerResourceBatchResult:
        results = self.execute_priced(
            {merchant_id: request for merchant_id in request.merchant_ids},
            apply=apply,
        )
        return SellerResourceBatchResult(apply, request.model, request.pricing, results)

    def execute_priced(
        self,
        requests_by_merchant: Mapping[int, SellerResourceBatchRequest],
        *,
        apply: bool = False,
        before_apply: Callable[
            [tuple[int, ...]], Mapping[int, str] | None
        ]
        | None = None,
    ) -> tuple[SellerMerchantResourceResult, ...]:
        """Run one independently priced request per Seller behind one preflight barrier."""
        if not isinstance(apply, bool):
            raise SellerOrchestrationError("seller_apply_invalid")
        if not requests_by_merchant:
            raise SellerOrchestrationError("seller_merchants_empty")
        round_deadline = (
            None
            if self.round_time_budget_seconds is None
            else time.monotonic() + self.round_time_budget_seconds
        )
        ordered = tuple(requests_by_merchant.items())
        if any(
            isinstance(merchant_id, bool)
            or not isinstance(merchant_id, int)
            or merchant_id < 1
            or merchant_id not in request.merchant_ids
            for merchant_id, request in ordered
        ):
            raise SellerOrchestrationError("seller_priced_request_invalid")
        execution_requests = dict(ordered)
        contexts = {
            merchant_id: self.seller_store.load_context(merchant_id)
            for merchant_id, _request in ordered
        }
        for merchant_id, context in contexts.items():
            _validate_seller_context_shape(context, merchant_id)
        ready_ids = tuple(
            merchant_id
            for merchant_id, _request in ordered
            if _seller_is_ready(contexts[merchant_id])
        )
        prepared = {
            merchant_id: self._prepare_one(
                execution_requests[merchant_id], contexts[merchant_id]
            )
            for merchant_id in ready_ids
        }
        ready_results: dict[int, SellerMerchantResourceResult] = {}
        if ready_ids:
            capability = self.seller_service.preflight_contract()
            parallel_listings = len(ready_ids) == 1 and self.max_workers > 1
            self._run_ready(
                ready_ids,
                lambda merchant_id: self._preflight_one_with_transport_retry(
                    execution_requests[merchant_id],
                    prepared[merchant_id],
                    capability=capability,
                    round_deadline=round_deadline,
                    parallel_listings=parallel_listings,
                ),
                round_deadline=round_deadline,
            )
            if apply and before_apply is not None:
                # Refresh pricing immediately after the complete remote-read barrier.
                # Repeating the same preflight here only doubles live A6 traffic.
                policies = before_apply(ready_ids)
                # The callback may atomically replace the caller-owned request map
                # with pricing rebuilt from the mutation-barrier Pin snapshot.
                execution_requests = {
                    merchant_id: requests_by_merchant[merchant_id]
                    for merchant_id, _request in ordered
                }
                if policies is not None:
                    if (
                        not isinstance(policies, Mapping)
                        or set(policies) != set(ready_ids)
                        or any(
                            action not in {"apply", "takedown", "release", "hold"}
                            for action in policies.values()
                        )
                    ):
                        raise SellerOrchestrationError(
                            "seller_listing_mutation_policies_invalid"
                        )
                    for merchant_id, action in policies.items():
                        execution_requests[merchant_id] = replace(
                            execution_requests[merchant_id],
                            listing_mutation=action,
                        )
            if self.max_workers == 1:
                for index, merchant_id in enumerate(ready_ids):
                    self._checkpoint(round_deadline)
                    result = self._execute_one_with_deadline(
                        execution_requests[merchant_id],
                        prepared[merchant_id],
                        apply=apply,
                        capability=capability,
                        round_deadline=round_deadline,
                        parallel_listings=parallel_listings,
                    )
                    ready_results[merchant_id] = result
                    if (
                        apply
                        and index < len(ready_ids) - 1
                        and self.activation_stagger_seconds > 0
                        and _listing_activation_changed(result)
                    ):
                        self.sleeper(self.activation_stagger_seconds)
            else:
                applied = self._run_ready(
                    ready_ids,
                    lambda merchant_id: self._execute_one_with_deadline(
                        execution_requests[merchant_id],
                        prepared[merchant_id],
                        apply=apply,
                        capability=capability,
                        round_deadline=round_deadline,
                        parallel_listings=parallel_listings,
                    ),
                    round_deadline=round_deadline,
                )
                ready_results.update(zip(ready_ids, applied, strict=True))
        results = tuple(
            ready_results[merchant_id]
            if merchant_id in ready_results
            else _not_ready_result(
                execution_requests[merchant_id],
                contexts[merchant_id],
                apply=apply,
            )
            for merchant_id, _request in ordered
        )
        return results

    @staticmethod
    def _checkpoint(round_deadline: float | None) -> None:
        if round_deadline is not None and time.monotonic() >= round_deadline:
            raise SellerOrchestrationError("seller_round_deadline_exceeded")

    def _run_ready(
        self,
        merchant_ids: Sequence[int],
        call: Callable[[int], Any],
        *,
        round_deadline: float | None,
    ) -> tuple[Any, ...]:
        return self._run_ordered(
            merchant_ids,
            call,
            max_workers=self.max_workers,
            round_deadline=round_deadline,
            thread_name_prefix="a6-seller",
        )

    def _run_ordered(
        self,
        items: Sequence[Any],
        call: Callable[[Any], Any],
        *,
        max_workers: int,
        round_deadline: float | None,
        thread_name_prefix: str,
    ) -> tuple[Any, ...]:
        self._checkpoint(round_deadline)
        if max_workers == 1 or len(items) <= 1:
            results = []
            for item in items:
                self._checkpoint(round_deadline)
                results.append(call(item))
            ordered = tuple(results)
            self._checkpoint(round_deadline)
            return ordered

        executor = ThreadPoolExecutor(
            max_workers=min(max_workers, len(items)),
            thread_name_prefix=thread_name_prefix,
        )
        futures = tuple(executor.submit(call, item) for item in items)
        wait_for_workers = True
        try:
            timeout = (
                None
                if round_deadline is None
                else max(0.0, round_deadline - time.monotonic())
            )
            _done, pending = wait(futures, timeout=timeout)
            if pending:
                wait_for_workers = False
                for future in pending:
                    future.cancel()
                raise SellerOrchestrationError("seller_round_deadline_exceeded")
            ordered = tuple(future.result() for future in futures)
        except Exception:
            wait_for_workers = False
            for future in futures:
                future.cancel()
            raise
        finally:
            executor.shutdown(wait=wait_for_workers, cancel_futures=True)
        self._checkpoint(round_deadline)
        return ordered

    @contextmanager
    def _client_request_deadline(
        self,
        client: SellerA6Client,
        round_deadline: float | None,
    ) -> Iterator[None]:
        self._checkpoint(round_deadline)
        deadline_scope = getattr(client, "request_deadline", None)
        if round_deadline is None or not callable(deadline_scope):
            yield
            return
        try:
            with deadline_scope(round_deadline):
                yield
        except A6AutoError as exc:
            if str(exc) in {
                "a6_request_deadline_exceeded",
                "a6_request_deadline_invalid",
            }:
                raise SellerOrchestrationError(
                    "seller_round_deadline_exceeded"
                ) from exc
            raise

    def _independent_listing_client(
        self,
        prepared: _PreparedSeller,
    ) -> SellerA6Client:
        fork = getattr(prepared.client, "fork", None)
        if callable(fork):
            return fork()
        return self.a6_client_factory(prepared.context, prepared.session)

    def _preflight_one_with_transport_retry(
        self,
        request: SellerResourceBatchRequest,
        prepared: _PreparedSeller,
        *,
        capability: object,
        round_deadline: float | None = None,
        parallel_listings: bool = False,
    ) -> None:
        attempts = 1 if request.listing_scope == "all_controlled" else 6
        for attempt in range(attempts):
            self._checkpoint(round_deadline)
            try:
                client = getattr(prepared, "client", None)
                with self._client_request_deadline(client, round_deadline):
                    if request.listing_scope == "all_controlled":
                        self._preflight_one(
                            request,
                            prepared,
                            capability=capability,
                            round_deadline=round_deadline,
                            parallel_listings=parallel_listings,
                        )
                    else:
                        self._preflight_one(
                            request,
                            prepared,
                            capability=capability,
                        )
                return
            except A6AutoError as exc:
                if str(exc) != "a6_transport_failed" or attempt + 1 >= attempts:
                    raise
                delay = attempt + 1
                if round_deadline is not None and time.monotonic() + delay >= round_deadline:
                    raise SellerOrchestrationError(
                        "seller_round_deadline_exceeded"
                    ) from exc
                self.sleeper(delay)

    def resolve_channel_create_unknown(
        self,
        request: SellerResourceBatchRequest,
        *,
        apply: bool = False,
    ) -> SellerChannelRecoveryResult:
        if not isinstance(apply, bool):
            raise SellerOrchestrationError("seller_apply_invalid")
        if len(request.merchant_ids) != 1:
            raise SellerOrchestrationError("seller_channel_recovery_single_merchant_required")
        merchant_id = request.merchant_ids[0]
        context = self.seller_store.load_context(merchant_id)
        _validate_seller_context_shape(context, merchant_id)
        if not _seller_is_ready(context):
            raise SellerOrchestrationError("seller_channel_recovery_not_ready")
        prepared = self._prepare_one(request, context)
        capability = self.seller_service.preflight_contract()
        seller_result = self.seller_service.execute(
            ManagedSellerRequest(
                merchant_id=merchant_id,
                group_id=request.group_id,
                delta=request.pricing.delta,
                config_version=request.config_version,
                requested_by=request.requested_by,
            ),
            apply=False,
            capability=capability,
        )
        binding = self.seller_store.get_binding(merchant_id)
        gate_error = _seller_gate_error(seller_result, binding, apply=False)
        if gate_error is not None or binding is None:
            raise SellerOrchestrationError("seller_channel_recovery_enodot_not_ready")
        api_key = self.secret_sink.read_api_key(
            binding.managed_uid,
            binding.key_secret_ref,
        )
        if hashlib.sha256(api_key.encode()).hexdigest() != binding.api_key_fingerprint:
            raise SellerOrchestrationError("seller_secret_fingerprint_mismatch")
        controller = self.listing_controller_factory(
            self.listing_store,
            prepared.client,
        )
        decision = controller.resolve_channel_create_unknown(
            prepared.merchant,
            _channel_spec(request, prepared.template, prepared.session, api_key),
            apply=apply,
        )
        return SellerChannelRecoveryResult(
            apply=apply,
            model=request.model,
            merchant_id=merchant_id,
            roles=prepared.merchant.roles,
            seller=seller_result.public(),
            channel=decision.public(),
        )

    def _prepare_one(
        self,
        request: SellerResourceBatchRequest,
        context: Any,
    ) -> _PreparedSeller:
        merchant = _managed_merchant(context)
        session = self.session_loader(Path(context.session_ref))
        _validate_session(context, session)
        template_id = session.template_id
        if not template_id:
            template_id = choose_account_template(
                "",
                a6_user_id=context.a6_user_id,
            ).template_id
        template = self.template_loader(template_id)
        client = self.a6_client_factory(context, session)
        return _PreparedSeller(context, merchant, session, template, client)

    def _controlled_listing_bindings(
        self,
        request: SellerResourceBatchRequest,
        merchant_id: int,
    ) -> tuple[Any, ...]:
        loader = getattr(self.listing_store, "list_controlled_listings", None)
        if callable(loader):
            bindings = tuple(loader(merchant_id, request.model))
        else:
            channel_uid = stable_managed_uid(
                "channel",
                merchant_id,
                request.channel_logical_key,
            )
            listing_uid = stable_managed_uid(
                "listing",
                merchant_id,
                f"{request.model}:{managed_marker(channel_uid)}",
            )
            binding = self.listing_store.get_listing(listing_uid)
            bindings = (binding,) if binding is not None else ()
        controlled = tuple(
            binding
            for binding in bindings
            if binding is not None
            and getattr(binding, "merchant_id", None) == merchant_id
            and getattr(binding, "control_state", "") != "retired"
            and getattr(binding, "a6_listing_id", None)
            and getattr(binding, "a6_channel_id", None)
        )
        if not controlled:
            raise SellerOrchestrationError("seller_controlled_listings_missing")
        managed_uids = tuple(str(binding.managed_uid) for binding in controlled)
        if len(set(managed_uids)) != len(managed_uids):
            raise SellerOrchestrationError("seller_controlled_listings_duplicate")
        return controlled

    def _preflight_all_controlled(
        self,
        request: SellerResourceBatchRequest,
        prepared: _PreparedSeller,
        *,
        round_deadline: float | None,
        parallel_listings: bool,
    ) -> None:
        listing_spec = _listing_spec(request)
        bindings = self._controlled_listing_bindings(
            request,
            prepared.merchant.merchant_id,
        )

        def preflight_binding(binding: Any) -> None:
            client = (
                self._independent_listing_client(prepared)
                if parallel_listings
                else prepared.client
            )
            controller = self.listing_controller_factory(self.listing_store, client)
            with self._client_request_deadline(client, round_deadline):
                self._preflight_controlled_listing(
                    request,
                    prepared,
                    binding,
                    controller,
                    listing_spec,
                )

        self._run_ordered(
            bindings,
            preflight_binding,
            max_workers=self.max_workers if parallel_listings else 1,
            round_deadline=round_deadline,
            thread_name_prefix="a6-seller-preflight-listing",
        )

    def _preflight_controlled_listing(
        self,
        request: SellerResourceBatchRequest,
        prepared: _PreparedSeller,
        binding: Any,
        controller: Any,
        listing_spec: ManagedListingSpec,
    ) -> None:
        if request.listing_mutation == "hold":
            return
        if request.listing_mutation == "takedown":
            decision = controller.takedown_listing(
                prepared.merchant,
                binding.managed_uid,
                apply=False,
            )
            _require_preflight_decision(
                decision,
                "seller_price_jump_takedown_preflight_blocked",
                pending_error_class="managed_listing_takedown_readback_pending",
                pending_states=_TAKEDOWN_RECONCILE_STATES,
            )
            return
        if request.listing_mutation == "release":
            decision = self._release_listing(
                controller,
                prepared.merchant,
                binding.managed_uid,
                listing_spec,
                apply=False,
            )
            if decision.get("state") not in {"planned", "verified"} or decision.get(
                "error_class"
            ):
                raise SellerOrchestrationError(
                    "seller_price_jump_release_preflight_blocked"
                )
            return
        decision = controller.update_listing(
            prepared.merchant,
            binding.managed_uid,
            listing_spec,
            apply=False,
        )
        _require_preflight_decision(
            decision,
            "seller_listing_preflight_blocked",
            pending_error_class="managed_listing_readback_pending",
        )

    def _preflight_one(
        self,
        request: SellerResourceBatchRequest,
        prepared: _PreparedSeller,
        *,
        capability: object,
        round_deadline: float | None = None,
        parallel_listings: bool = False,
    ) -> None:
        merchant = prepared.merchant
        merchant_id = merchant.merchant_id
        seller_result = self.seller_service.execute(
            ManagedSellerRequest(
                merchant_id=merchant_id,
                group_id=request.group_id,
                delta=request.pricing.delta,
                config_version=request.config_version,
                requested_by=request.requested_by,
            ),
            apply=False,
            capability=capability,
        )
        binding = self.seller_store.get_binding(merchant_id)
        if request.listing_scope == "all_controlled":
            if request.listing_mutation not in {"takedown", "hold"}:
                gate_error = _seller_gate_error(seller_result, binding, apply=False)
                if gate_error is not None or binding is None:
                    raise SellerOrchestrationError("seller_enodot_resource_not_ready")
            self._preflight_all_controlled(
                request,
                prepared,
                round_deadline=round_deadline,
                parallel_listings=parallel_listings,
            )
            return
        channel_uid = stable_managed_uid(
            "channel", merchant_id, request.channel_logical_key
        )
        listing_uid = stable_managed_uid(
            "listing",
            merchant_id,
            f"{request.model}:{managed_marker(channel_uid)}",
        )
        if request.listing_mutation == "takedown":
            controller = self.listing_controller_factory(
                self.listing_store,
                prepared.client,
            )
            listing = controller.takedown_listing(
                merchant,
                listing_uid,
                apply=False,
            )
            _require_preflight_decision(
                listing,
                "seller_price_jump_takedown_preflight_blocked",
                pending_error_class="managed_listing_takedown_readback_pending",
                pending_states=_TAKEDOWN_RECONCILE_STATES,
            )
            return
        if binding is None or not binding.key_secret_ref or not binding.api_key_fingerprint:
            if seller_result.action != "create":
                raise SellerOrchestrationError("seller_enodot_resource_not_ready")
            if request.channel_strategy == "adopt_existing":
                controller = self.listing_controller_factory(
                    self.listing_store,
                    prepared.client,
                )
                selection = controller.select_existing_channel(
                    merchant,
                    model=request.model,
                    preferred_channel_id=request.existing_channel_id,
                )
                channel = controller.ensure_existing_channel(
                    merchant,
                    request.channel_logical_key,
                    selection,
                    model=request.model,
                    apply=False,
                )
                _require_preflight_decision(
                    channel,
                    "seller_channel_adoption_preflight_blocked",
                )
                listing = controller.ensure_listing_on_existing_channel(
                    merchant,
                    channel_uid,
                    selection,
                    _listing_spec(request),
                    apply=False,
                )
                _require_preflight_decision(
                    listing,
                    "seller_listing_adoption_preflight_blocked",
                )
                return
            self._preflight_unbound_resources(prepared, channel_uid, listing_uid)
            return
        if seller_result.action == "reconcile_pending":
            raise SellerOrchestrationError("seller_enodot_resource_pending")
        if binding.control_state in {"creating", "updating"}:
            raise SellerOrchestrationError("seller_enodot_resource_pending")
        api_key = self.secret_sink.read_api_key(
            binding.managed_uid,
            binding.key_secret_ref,
        )
        if hashlib.sha256(api_key.encode()).hexdigest() != binding.api_key_fingerprint:
            raise SellerOrchestrationError("seller_secret_fingerprint_mismatch")
        controller = self.listing_controller_factory(
            self.listing_store,
            prepared.client,
        )
        if request.channel_strategy == "adopt_existing":
            channel_spec = _channel_spec(
                request,
                prepared.template,
                prepared.session,
                api_key,
            )
            selection = controller.select_existing_channel(
                merchant,
                model=request.model,
                preferred_channel_id=request.existing_channel_id,
            )
            channel = controller.ensure_existing_channel(
                merchant,
                request.channel_logical_key,
                selection,
                model=request.model,
                apply=False,
            )
            _require_preflight_decision(
                channel,
                "seller_channel_adoption_preflight_blocked",
            )
            channel_binding = self.listing_store.get_channel(channel_uid)
            if (
                channel_binding is not None
                and channel_binding.a6_channel_id
                and channel_binding.control_state in _CHANNEL_EXISTING_STATES
            ):
                upstream = controller.ensure_channel_upstream(
                    merchant,
                    channel_uid,
                    channel_spec,
                    model=request.model,
                    apply=False,
                )
                _require_preflight_decision(
                    upstream,
                    "seller_channel_upstream_preflight_blocked",
                )
            if request.listing_mutation == "release":
                listing = controller.update_listing(
                    merchant,
                    listing_uid,
                    _listing_spec(request),
                    expected_status=0,
                    apply=False,
                )
            else:
                listing = controller.ensure_listing_on_existing_channel(
                    merchant,
                    channel_uid,
                    selection,
                    _listing_spec(request),
                    apply=False,
                )
            _require_preflight_decision(
                listing,
                "seller_listing_adoption_preflight_blocked",
            )
            return
        channel = controller.ensure_channel(
            merchant,
            _channel_spec(request, prepared.template, prepared.session, api_key),
            apply=False,
        )
        _require_preflight_decision(
            channel,
            "seller_channel_preflight_blocked",
            pending_error_class="managed_channel_readback_pending",
        )
        channel_binding = self.listing_store.get_channel(channel_uid)
        listing_spec = _listing_spec(request)
        if (
            channel_binding is not None
            and channel_binding.a6_channel_id
            and channel_binding.control_state in _CHANNEL_EXISTING_STATES
        ):
            upstream = controller.ensure_channel_upstream(
                merchant,
                channel_uid,
                _channel_spec(request, prepared.template, prepared.session, api_key),
                model=request.model,
                apply=False,
            )
            _require_preflight_decision(
                upstream,
                "seller_channel_upstream_preflight_blocked",
            )
            listing_binding = self.listing_store.get_listing(listing_uid)
            if (
                listing_binding is not None
                and listing_binding.channel_binding_id != channel_binding.binding_id
            ):
                raise SellerOrchestrationError("seller_listing_channel_binding_conflict")
            if (
                listing_binding is not None
                and listing_binding.a6_listing_id
                and listing_binding.control_state in _LISTING_EXISTING_STATES
            ):
                if request.listing_mutation == "release":
                    listing = controller.update_listing(
                        merchant,
                        listing_uid,
                        listing_spec,
                        expected_status=0,
                        apply=False,
                    )
                else:
                    listing = controller.update_listing(
                        merchant,
                        listing_uid,
                        listing_spec,
                        apply=False,
                    )
            else:
                listing = controller.ensure_listing(
                    merchant,
                    channel_uid,
                    listing_spec,
                    apply=False,
                )
            _require_preflight_decision(
                listing,
                "seller_listing_preflight_blocked",
                pending_error_class="managed_listing_readback_pending",
            )
            return
        if channel_binding is None:
            self._preflight_unbound_resources(prepared, channel_uid, listing_uid)
        else:
            self._preflight_unbound_listing(prepared, listing_uid)

    def _preflight_unbound_resources(
        self,
        prepared: _PreparedSeller,
        channel_uid: str,
        listing_uid: str,
    ) -> None:
        if self.listing_store.get_channel(channel_uid) is not None:
            raise SellerOrchestrationError("seller_channel_binding_without_secret")
        if self.listing_store.get_listing(listing_uid) is not None:
            raise SellerOrchestrationError("seller_listing_binding_without_channel")
        channels = _inventory_sequence(
            prepared.client.supplier_channels(prepared.merchant.a6_user_id),
            "seller_channel_inventory_invalid",
        )
        requests = _inventory_sequence(
            prepared.client.supplier_requests(prepared.merchant.a6_user_id),
            "seller_request_inventory_invalid",
        )
        tokens = (
            managed_marker(channel_uid),
            channel_uid,
            managed_channel_name(channel_uid),
            managed_resource_signature("channel", channel_uid),
        )
        if any(
            any(_document_contains_text(item, token) for token in tokens)
            for item in (*channels, *requests)
        ):
            raise SellerOrchestrationError("seller_channel_remote_untracked")
        self._preflight_unbound_listing(prepared, listing_uid)

    def _preflight_unbound_listing(
        self,
        prepared: _PreparedSeller,
        listing_uid: str,
    ) -> None:
        if self.listing_store.get_listing(listing_uid) is not None:
            raise SellerOrchestrationError("seller_listing_binding_without_channel")
        listings = _read_listing_inventory(prepared.client, prepared.merchant.a6_user_id)
        tokens = (
            managed_marker(listing_uid),
            listing_uid,
            managed_resource_signature("listing", listing_uid),
        )
        if any(
            any(_document_contains_text(item, token) for token in tokens)
            for item in listings
        ):
            raise SellerOrchestrationError("seller_listing_remote_untracked")

    def _execute_one_with_deadline(
        self,
        request: SellerResourceBatchRequest,
        prepared: _PreparedSeller,
        *,
        apply: bool,
        capability: object,
        round_deadline: float | None,
        parallel_listings: bool,
    ) -> SellerMerchantResourceResult:
        client = getattr(prepared, "client", None)
        with self._client_request_deadline(client, round_deadline):
            if request.listing_scope != "all_controlled":
                result = self._execute_one(
                    request,
                    prepared,
                    apply=apply,
                    capability=capability,
                )
            else:
                result = self._execute_one(
                    request,
                    prepared,
                    apply=apply,
                    capability=capability,
                    round_deadline=round_deadline,
                    parallel_listings=parallel_listings,
                )
        self._checkpoint(round_deadline)
        return result

    def _execute_one(
        self,
        request: SellerResourceBatchRequest,
        prepared: _PreparedSeller,
        *,
        apply: bool,
        capability: object,
        round_deadline: float | None = None,
        parallel_listings: bool = False,
    ) -> SellerMerchantResourceResult:
        merchant_id = prepared.merchant.merchant_id
        channel_uid = stable_managed_uid("channel", merchant_id, request.channel_logical_key)
        listing_uid = stable_managed_uid(
            "listing",
            merchant_id,
            f"{request.model}:{managed_marker(channel_uid)}",
        )
        if request.listing_scope == "all_controlled":
            return self._execute_all_controlled(
                request,
                prepared,
                apply=apply,
                capability=capability,
                round_deadline=round_deadline,
                parallel_listings=parallel_listings,
            )
        context = prepared.context
        seller_public: dict[str, Any] = _seller_error(merchant_id, request.group_id)
        try:
            merchant = prepared.merchant
            if request.listing_mutation in {"takedown", "hold"}:
                return self._execute_listing_control_only(
                    request,
                    prepared,
                    listing_uid=listing_uid,
                    apply=apply,
                )
            seller_result = self.seller_service.execute(
                ManagedSellerRequest(
                    merchant_id=merchant_id,
                    group_id=request.group_id,
                    delta=request.pricing.delta,
                    config_version=request.config_version,
                    requested_by=request.requested_by,
                ),
                apply=apply,
                capability=capability,
            )
            seller_public = seller_result.public()
            binding = self.seller_store.get_binding(merchant_id)
            gate_error = _seller_gate_error(seller_result, binding, apply=apply)
            if gate_error is not None:
                return SellerMerchantResourceResult(
                    merchant_id,
                    merchant.a6_user_id,
                    merchant.roles,
                    seller_public,
                    _blocked_decision(merchant_id, "channel", channel_uid, gate_error, apply),
                    _blocked_decision(merchant_id, "listing", listing_uid, "channel", apply),
                )
            assert binding is not None
            api_key = self.secret_sink.read_api_key(binding.managed_uid, binding.key_secret_ref)
            if hashlib.sha256(api_key.encode()).hexdigest() != binding.api_key_fingerprint:
                raise SellerOrchestrationError("seller_secret_fingerprint_mismatch")
            session = prepared.session
            template = prepared.template
            client = prepared.client
            controller = self.listing_controller_factory(self.listing_store, client)
            channel_spec = _channel_spec(request, template, session, api_key)
            listing_spec = _listing_spec(request)
            if request.channel_strategy == "adopt_existing":
                selection = controller.select_existing_channel(
                    merchant,
                    model=request.model,
                    preferred_channel_id=request.existing_channel_id,
                )
                channel = controller.ensure_existing_channel(
                    merchant,
                    request.channel_logical_key,
                    selection,
                    model=request.model,
                    apply=apply,
                )
                if channel.state != "verified":
                    return SellerMerchantResourceResult(
                        merchant_id,
                        merchant.a6_user_id,
                        merchant.roles,
                        seller_public,
                        channel.public(),
                        _blocked_decision(
                            merchant_id,
                            "listing",
                            listing_uid,
                            "channel",
                            apply,
                        ),
                    )
                upstream = controller.ensure_channel_upstream(
                    merchant,
                    channel.managed_uid,
                    channel_spec,
                    model=request.model,
                    apply=apply,
                )
                if upstream.state != "verified":
                    return SellerMerchantResourceResult(
                        merchant_id,
                        merchant.a6_user_id,
                        merchant.roles,
                        seller_public,
                        upstream.public(),
                        _blocked_decision(
                            merchant_id,
                            "listing",
                            listing_uid,
                            "channel_upstream",
                            apply,
                        ),
                    )
                if request.listing_mutation == "release":
                    listing_public = self._release_listing(
                        controller,
                        merchant,
                        listing_uid,
                        listing_spec,
                        apply=apply,
                    )
                else:
                    listing_public = controller.ensure_listing_on_existing_channel(
                        merchant,
                        channel.managed_uid,
                        selection,
                        listing_spec,
                        apply=apply,
                    ).public()
                return SellerMerchantResourceResult(
                    merchant_id,
                    merchant.a6_user_id,
                    merchant.roles,
                    seller_public,
                    (
                        channel.public()
                        if upstream.action == "noop"
                        else upstream.public()
                    ),
                    listing_public,
                )
            channel = controller.ensure_channel(merchant, channel_spec, apply=apply)
            if channel.state != "verified":
                return SellerMerchantResourceResult(
                    merchant_id,
                    merchant.a6_user_id,
                    merchant.roles,
                    seller_public,
                    channel.public(),
                    _blocked_decision(merchant_id, "listing", listing_uid, "channel", apply),
                )
            upstream = controller.ensure_channel_upstream(
                merchant,
                channel.managed_uid,
                channel_spec,
                model=request.model,
                apply=apply,
            )
            if upstream.state != "verified":
                return SellerMerchantResourceResult(
                    merchant_id,
                    merchant.a6_user_id,
                    merchant.roles,
                    seller_public,
                    upstream.public(),
                    _blocked_decision(
                        merchant_id,
                        "listing",
                        listing_uid,
                        "channel_upstream",
                        apply,
                    ),
                )
            listing_binding = self.listing_store.get_listing(listing_uid)
            if request.listing_mutation == "release":
                listing_public = self._release_listing(
                    controller,
                    merchant,
                    listing_uid,
                    listing_spec,
                    apply=apply,
                )
            elif (
                listing_binding is None
                or not listing_binding.a6_listing_id
                or listing_binding.control_state not in _LISTING_EXISTING_STATES
            ):
                listing_public = controller.ensure_listing(
                    merchant,
                    channel.managed_uid,
                    listing_spec,
                    apply=apply,
                ).public()
            else:
                listing_public = controller.update_listing(
                    merchant,
                    listing_uid,
                    listing_spec,
                    apply=apply,
                ).public()
            return SellerMerchantResourceResult(
                merchant_id,
                merchant.a6_user_id,
                merchant.roles,
                seller_public,
                channel.public() if upstream.action == "noop" else upstream.public(),
                listing_public,
            )
        except Exception as exc:
            code = _error_code(exc)
            a6_user_id = int(getattr(context, "a6_user_id", 0) or 0) or None
            roles = tuple(getattr(context, "roles", ()) or ())
            return SellerMerchantResourceResult(
                merchant_id,
                a6_user_id,
                roles,
                seller_public,
                _blocked_decision(merchant_id, "channel", channel_uid, code, apply),
                _blocked_decision(merchant_id, "listing", listing_uid, "channel", apply),
                error_class=code,
            )

    def _execute_all_controlled(
        self,
        request: SellerResourceBatchRequest,
        prepared: _PreparedSeller,
        *,
        apply: bool,
        capability: object,
        round_deadline: float | None,
        parallel_listings: bool,
    ) -> SellerMerchantResourceResult:
        merchant = prepared.merchant
        merchant_id = merchant.merchant_id
        group_uid = f"a6-managed:listing-group:m{merchant_id}:{request.model}"
        seller_binding = self.seller_store.get_binding(merchant_id)
        if request.listing_mutation in {"takedown", "hold"}:
            seller_public = _price_control_seller_decision(
                request,
                seller_binding,
                merchant_id=merchant_id,
                apply=apply,
            )
        else:
            seller_result = self.seller_service.execute(
                ManagedSellerRequest(
                    merchant_id=merchant_id,
                    group_id=request.group_id,
                    delta=request.pricing.delta,
                    config_version=request.config_version,
                    requested_by=request.requested_by,
                ),
                apply=apply,
                capability=capability,
            )
            seller_public = seller_result.public()
            seller_binding = self.seller_store.get_binding(merchant_id)
            gate_error = _seller_gate_error(
                seller_result,
                seller_binding,
                apply=apply,
            )
            if gate_error is not None:
                return SellerMerchantResourceResult(
                    merchant_id,
                    merchant.a6_user_id,
                    merchant.roles,
                    seller_public,
                    _blocked_decision(
                        merchant_id,
                        "channel",
                        group_uid,
                        gate_error,
                        apply,
                    ),
                    _blocked_decision(
                        merchant_id,
                        "listing",
                        group_uid,
                        "seller",
                        apply,
                    ),
                )

        channel_public = _price_control_resource_decision(
            merchant_id,
            "channel",
            group_uid,
            request.listing_mutation,
            external_id=None,
        )
        try:
            bindings = self._controlled_listing_bindings(request, merchant_id)
            listing_spec = _listing_spec(request)

            def execute_binding(binding: Any) -> dict[str, Any]:
                client = (
                    self._independent_listing_client(prepared)
                    if parallel_listings
                    else prepared.client
                )
                controller = self.listing_controller_factory(self.listing_store, client)
                try:
                    with self._client_request_deadline(client, round_deadline):
                        if request.listing_mutation == "hold":
                            public = _price_control_resource_decision(
                                merchant_id,
                                "listing",
                                binding.managed_uid,
                                "hold",
                                external_id=binding.a6_listing_id,
                            )
                        elif request.listing_mutation == "takedown":
                            public = controller.takedown_listing(
                                merchant,
                                binding.managed_uid,
                                apply=apply,
                            ).public()
                            public["price_jump_control"] = "takedown"
                        elif request.listing_mutation == "release":
                            public = self._release_listing(
                                controller,
                                merchant,
                                binding.managed_uid,
                                listing_spec,
                                apply=apply,
                            )
                        else:
                            public = controller.update_listing(
                                merchant,
                                binding.managed_uid,
                                listing_spec,
                                apply=apply,
                            ).public()
                    return dict(public)
                except Exception as exc:
                    return {
                        "merchant_id": merchant_id,
                        "resource_kind": "listing",
                        "managed_uid": binding.managed_uid,
                        "action": request.listing_mutation,
                        "state": "error",
                        "applied": False,
                        "external_id": binding.a6_listing_id,
                        "error_class": _error_code(exc),
                    }

            decisions = self._run_ordered(
                bindings,
                execute_binding,
                max_workers=self.max_workers if parallel_listings else 1,
                round_deadline=round_deadline,
                thread_name_prefix="a6-seller-apply-listing",
            )
            listing_public = _controlled_listing_group_result(
                merchant_id,
                group_uid,
                request.listing_mutation,
                decisions,
                apply=apply,
            )
            return SellerMerchantResourceResult(
                merchant_id,
                merchant.a6_user_id,
                merchant.roles,
                seller_public,
                channel_public,
                listing_public,
                error_class=listing_public.get("error_class"),
            )
        except Exception as exc:
            code = _error_code(exc)
            return SellerMerchantResourceResult(
                merchant_id,
                merchant.a6_user_id,
                merchant.roles,
                seller_public,
                channel_public,
                _blocked_decision(
                    merchant_id,
                    "listing",
                    group_uid,
                    code,
                    apply,
                ),
                error_class=code,
            )

    def _execute_listing_control_only(
        self,
        request: SellerResourceBatchRequest,
        prepared: _PreparedSeller,
        *,
        listing_uid: str,
        apply: bool,
    ) -> SellerMerchantResourceResult:
        merchant = prepared.merchant
        merchant_id = merchant.merchant_id
        channel_uid = stable_managed_uid(
            "channel",
            merchant_id,
            request.channel_logical_key,
        )
        seller_binding = self.seller_store.get_binding(merchant_id)
        channel_binding = self.listing_store.get_channel(channel_uid)
        listing_binding = self.listing_store.get_listing(listing_uid)
        seller_public = _price_control_seller_decision(
            request,
            seller_binding,
            merchant_id=merchant_id,
            apply=apply,
        )
        channel_public = _price_control_resource_decision(
            merchant_id,
            "channel",
            channel_uid,
            request.listing_mutation,
            external_id=getattr(channel_binding, "a6_channel_id", None),
        )
        if request.listing_mutation == "hold":
            listing_public = _price_control_resource_decision(
                merchant_id,
                "listing",
                listing_uid,
                "hold",
                external_id=getattr(listing_binding, "a6_listing_id", None),
            )
        else:
            controller = self.listing_controller_factory(
                self.listing_store,
                prepared.client,
            )
            listing_public = controller.takedown_listing(
                merchant,
                listing_uid,
                apply=apply,
            ).public()
            listing_public["price_jump_control"] = "takedown"
        return SellerMerchantResourceResult(
            merchant_id,
            merchant.a6_user_id,
            merchant.roles,
            seller_public,
            channel_public,
            listing_public,
        )

    def _release_listing(
        self,
        controller: ManagedListingController,
        merchant: ManagedMerchant,
        listing_uid: str,
        listing_spec: ManagedListingSpec,
        *,
        apply: bool,
    ) -> dict[str, Any]:
        takedown = None
        try:
            price_update = controller.update_listing(
                merchant,
                listing_uid,
                listing_spec,
                expected_status=0,
                apply=apply,
            )
        except ManagedListingError as exc:
            if str(exc) != "managed_listing_activation_changed":
                raise
            active_readback = controller.update_listing(
                merchant,
                listing_uid,
                listing_spec,
                expected_status=2,
                apply=False,
            )
            if active_readback.action == "noop" and active_readback.state == "verified":
                return _release_listing_public(
                    active_readback,
                    sequence=("active_target_readback",),
                )
            if not apply:
                return _release_listing_public(
                    active_readback,
                    sequence=(
                        "takedown",
                        "update_offline",
                        "strict_readback_offline",
                        "resubmit",
                    ),
                    planned=True,
                )
            takedown = controller.takedown_listing(
                merchant,
                listing_uid,
                apply=True,
            )
            if not _managed_decision_verified(takedown):
                return _release_listing_public(
                    takedown,
                    sequence=("takedown",),
                )
            price_update = controller.update_listing(
                merchant,
                listing_uid,
                listing_spec,
                expected_status=0,
                apply=True,
            )

        if not _managed_decision_verified(price_update):
            return _release_listing_public(
                price_update,
                sequence=("update_offline", "strict_readback_offline"),
                planned=not apply,
                takedown=takedown,
            )
        if not apply:
            return _release_listing_public(
                price_update,
                sequence=("update_offline", "strict_readback_offline", "resubmit"),
                planned=True,
            )
        activation = controller.resubmit_listing(
            merchant,
            listing_uid,
            apply=True,
        )
        return _release_listing_public(
            activation,
            sequence=("update_offline", "strict_readback_offline", "resubmit"),
            price_update=price_update,
            takedown=takedown,
        )


def _price_control_seller_decision(
    request: SellerResourceBatchRequest,
    binding: ManagedSellerBinding | None,
    *,
    merchant_id: int,
    apply: bool,
) -> dict[str, Any]:
    verified = binding is not None and binding.control_state == "bound"
    return {
        "success": True,
        "mode": "applied" if apply else "dry-run",
        "merchant_id": merchant_id,
        "managed_uid": (
            binding.managed_uid
            if binding is not None
            else f"a6trader:seller:merchant-{merchant_id}"
        ),
        "action": "noop",
        "applied": False,
        "verified": verified,
        "state": "bound" if verified else "bypassed",
        "enodot_user_id": getattr(binding, "enodot_user_id", None),
        "enodot_api_key_id": getattr(binding, "enodot_api_key_id", None),
        "delta": _decimal_text(
            binding.desired_delta if binding is not None else request.pricing.delta
        ),
        "group_id": request.group_id,
        "error_class": None,
        "price_jump_control": request.listing_mutation,
    }


def _price_control_resource_decision(
    merchant_id: int,
    resource_kind: str,
    managed_uid: str,
    control: str,
    *,
    external_id: int | None,
) -> dict[str, Any]:
    return {
        "merchant_id": merchant_id,
        "resource_kind": resource_kind,
        "managed_uid": managed_uid,
        "action": "hold" if resource_kind == "listing" and control == "hold" else "noop",
        "state": "verified",
        "applied": False,
        "external_id": external_id,
        "error_class": None,
        "price_jump_control": control,
    }


def _controlled_listing_group_result(
    merchant_id: int,
    managed_uid: str,
    mutation: str,
    decisions: Sequence[Mapping[str, Any]],
    *,
    apply: bool,
) -> dict[str, Any]:
    if not decisions:
        raise SellerOrchestrationError("seller_controlled_listings_missing")
    errors = tuple(
        str(item.get("error_class") or "").strip()
        for item in decisions
        if item.get("error_class")
    )
    uncertain = any(
        str(item.get("state") or "") == "unknown"
        or str(item.get("state") or "").endswith("_unknown")
        or str(item.get("action") or "") == "reconcile_pending"
        or str(item.get("action") or "").endswith("_unknown")
        for item in decisions
    )
    verified = not errors and all(
        str(item.get("state") or "") == "verified" for item in decisions
    )
    state = (
        "verified"
        if verified or mutation == "hold"
        else "unknown"
        if uncertain
        else "error"
        if errors
        else "planned"
    )
    return {
        "merchant_id": merchant_id,
        "resource_kind": "listing_group",
        "managed_uid": managed_uid,
        "action": "update" if mutation == "apply" else mutation,
        "state": state,
        "applied": bool(apply and any(item.get("applied") for item in decisions)),
        "external_id": None,
        "error_class": errors[0] if errors else None,
        "scope": "all_controlled",
        "target_count": len(decisions),
        "verified_count": sum(
            str(item.get("state") or "") == "verified" for item in decisions
        ),
        "listings": [dict(item) for item in decisions],
    }


def _managed_decision_verified(decision: Any) -> bool:
    return (
        getattr(decision, "state", None) == "verified"
        and not getattr(decision, "error_class", None)
    )


def _release_listing_public(
    decision: Any,
    *,
    sequence: tuple[str, ...],
    planned: bool = False,
    price_update: Any | None = None,
    takedown: Any | None = None,
) -> dict[str, Any]:
    public = dict(decision.public())
    public["underlying_action"] = public.get("action")
    public["action"] = "release"
    if planned:
        public["state"] = "planned"
        public["applied"] = False
    public["price_jump_control"] = "release"
    public["release_sequence"] = list(sequence)
    if takedown is not None:
        public["takedown"] = dict(takedown.public())
    if price_update is not None:
        public["price_update"] = dict(price_update.public())
    return public


def _channel_spec(
    request: SellerResourceBatchRequest,
    template: MerchantTemplate,
    session: SessionRecord,
    api_key: str,
) -> ManagedChannelSpec:
    return ManagedChannelSpec(
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


def _listing_spec(request: SellerResourceBatchRequest) -> ManagedListingSpec:
    input_price, output_price, cache_read_price, cache_write_price = request.pricing.prices
    return ManagedListingSpec(
        logical_key=request.model,
        input_price_micros=input_price,
        output_price_micros=output_price,
        cache_read_price_micros=cache_read_price,
        cache_write_price_micros=cache_write_price,
        display_name=request.model,
        description=request.listing_description,
        own_model=request.model,
        platform_model=request.model,
    )


def _listing_activation_changed(result: SellerMerchantResourceResult) -> bool:
    return result.listing.get("applied") is True and result.listing.get("action") in {
        "create",
        "adopt_existing",
        "resubmit",
        "takedown",
        "release",
        "update_after_resubmit",
    }


def _validate_seller_context_shape(context: Any, merchant_id: int) -> None:
    roles = tuple(getattr(context, "roles", ()) or ())
    valid_roles = roles == ("seller",) or (
        len(roles) == 2 and roles[0] == "seller" and roles[1] in _DUTIES
    )
    a6_user_id = getattr(context, "a6_user_id", None)
    supplier_id = getattr(context, "supplier_id", None)
    valid_a6_user_id = a6_user_id is None or (
        isinstance(a6_user_id, int)
        and not isinstance(a6_user_id, bool)
        and a6_user_id > 0
    )
    valid_supplier_id = supplier_id is None or (
        isinstance(supplier_id, int)
        and not isinstance(supplier_id, bool)
        and supplier_id > 0
    )
    if (
        getattr(context, "merchant_id", None) != merchant_id
        or not str(getattr(context, "merchant_status", "")).strip()
        or not str(getattr(context, "session_ref", "")).strip()
        or not valid_roles
        or not valid_a6_user_id
        or not valid_supplier_id
    ):
        raise SellerOrchestrationError("seller_context_invalid")


def _seller_is_ready(context: Any) -> bool:
    return (
        context.merchant_status == _SELLER_STATUS
        and context.a6_user_id is not None
        and context.supplier_id is not None
    )


def _not_ready_result(
    request: SellerResourceBatchRequest,
    context: Any,
    *,
    apply: bool,
) -> SellerMerchantResourceResult:
    merchant_id = context.merchant_id
    channel_uid = stable_managed_uid(
        "channel", merchant_id, request.channel_logical_key
    )
    listing_uid = stable_managed_uid(
        "listing",
        merchant_id,
        f"{request.model}:{managed_marker(channel_uid)}",
    )
    seller = {
        "success": True,
        "mode": "applied" if apply else "dry-run",
        "merchant_id": merchant_id,
        "managed_uid": f"a6trader:seller:merchant-{merchant_id}",
        "action": "not_ready",
        "applied": False,
        "verified": False,
        "state": "pending",
        "enodot_user_id": None,
        "enodot_api_key_id": None,
        "delta": _decimal_text(request.pricing.delta),
        "group_id": request.group_id,
        "merchant_status": context.merchant_status,
        "error_class": None,
    }
    return SellerMerchantResourceResult(
        merchant_id,
        context.a6_user_id,
        tuple(context.roles),
        seller,
        _readiness_blocked_decision(merchant_id, "channel", channel_uid),
        _readiness_blocked_decision(merchant_id, "listing", listing_uid),
    )


def _managed_merchant(context: Any) -> ManagedMerchant:
    roles = tuple(context.roles)
    expected_roles = ("seller",)
    if len(roles) == 2 and roles[1] in _DUTIES:
        expected_roles = ("seller", roles[1])
    if (
        context.merchant_status != _SELLER_STATUS
        or roles != expected_roles
    ):
        raise SellerOrchestrationError("seller_context_ineligible")
    return ManagedMerchant(
        merchant_id=context.merchant_id,
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


def _seller_gate_error(
    result: ManagedSellerResult,
    binding: ManagedSellerBinding | None,
    *,
    apply: bool,
) -> str | None:
    planned_delta_update = (
        not apply
        and result.preflight_verified
        and result.mode == "dry-run"
        and result.action == "update_delta"
        and not result.applied
        and result.state == "bound"
        and result.error_class is None
    )
    if not result.verified and not planned_delta_update:
        return "enodot_seller" if not apply else "enodot_seller_unverified"
    if (
        binding is None
        or binding.control_state != "bound"
        or binding.managed_uid != result.managed_uid
        or not binding.key_secret_ref
        or not binding.api_key_fingerprint
    ):
        return "enodot_seller_binding"
    return None


def _blocked_decision(
    merchant_id: int,
    resource_kind: str,
    managed_uid: str,
    blocked_by: str,
    apply: bool,
) -> dict[str, Any]:
    return {
        "merchant_id": merchant_id,
        "resource_kind": resource_kind,
        "managed_uid": managed_uid,
        "action": "blocked",
        "state": "blocked" if apply else "planned",
        "applied": False,
        "external_id": None,
        "error_class": None,
        "blocked_by": blocked_by,
    }


def _readiness_blocked_decision(
    merchant_id: int,
    resource_kind: str,
    managed_uid: str,
) -> dict[str, Any]:
    return {
        "merchant_id": merchant_id,
        "resource_kind": resource_kind,
        "managed_uid": managed_uid,
        "action": "blocked",
        "state": "pending",
        "applied": False,
        "external_id": None,
        "error_class": None,
        "blocked_by": "seller_not_ready",
    }


def _seller_error(merchant_id: int, group_id: int) -> dict[str, Any]:
    return {
        "success": False,
        "mode": "unreconciled",
        "merchant_id": merchant_id,
        "managed_uid": f"a6trader:seller:merchant-{merchant_id}",
        "action": "error",
        "applied": False,
        "verified": False,
        "state": "error",
        "enodot_user_id": None,
        "enodot_api_key_id": None,
        "delta": None,
        "group_id": group_id,
        "error_class": None,
    }


def _error_code(exc: Exception) -> str:
    code = getattr(exc, "code", None)
    if isinstance(code, str) and code:
        return code
    if isinstance(exc, (ManagedSellerError, ManagedListingError, A6AutoError)):
        value = str(exc)
        if value and value.replace("_", "").isalnum():
            return value
    return f"seller_orchestration_{type(exc).__name__.lower()}"


def _require_preflight_decision(
    decision: Any,
    code: str,
    *,
    pending_error_class: str | None = None,
    pending_states: frozenset[str] = _CREATE_RECONCILE_STATES,
) -> None:
    action = getattr(decision, "action", None)
    error_class = getattr(decision, "error_class", None)
    if (
        pending_error_class is not None
        and action == "reconcile_pending"
        and getattr(decision, "state", None) in pending_states
        and error_class in {None, pending_error_class}
    ):
        return
    if (
        error_class
        or action in {"error", "reconcile_pending"}
        or (
            action == "noop"
            and getattr(decision, "state", None) != "verified"
        )
    ):
        raise SellerOrchestrationError(code)


def _inventory_sequence(value: Any, code: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or any(not isinstance(item, Mapping) for item in value):
        raise SellerOrchestrationError(code)
    return [dict(item) for item in value]


def _document_contains_text(value: Any, expected: str, *, depth: int = 0) -> bool:
    if depth > 8:
        return False
    if isinstance(value, Mapping):
        return any(
            _document_contains_text(item, expected, depth=depth + 1)
            for item in value.values()
        )
    if isinstance(value, (list, tuple)):
        return any(
            _document_contains_text(item, expected, depth=depth + 1)
            for item in value
        )
    return isinstance(value, str) and expected in value


def _read_listing_inventory(
    client: SellerA6Client,
    user_id: int,
    *,
    page_size: int = 1000,
    max_pages: int = 10,
) -> list[dict[str, Any]]:
    by_id: dict[int, dict[str, Any]] = {}
    expected_total: int | None = None
    for page in range(1, max_pages + 1):
        response = client.supplier_listings(user_id, page=page, size=page_size)
        if not isinstance(response, Mapping):
            raise SellerOrchestrationError("seller_listing_inventory_invalid")
        items = _inventory_sequence(
            response.get("items"),
            "seller_listing_inventory_invalid",
        )
        raw_total = response.get("total")
        if raw_total is not None:
            try:
                total = int(raw_total)
            except (TypeError, ValueError):
                raise SellerOrchestrationError("seller_listing_inventory_invalid") from None
            if total < 0 or expected_total is not None and total != expected_total:
                raise SellerOrchestrationError("seller_listing_inventory_invalid")
            expected_total = total
        for item in items:
            try:
                item_id = int(item.get("id") or 0)
            except (TypeError, ValueError):
                raise SellerOrchestrationError("seller_listing_inventory_invalid") from None
            if item_id > 0:
                by_id[item_id] = item
        if expected_total is not None and len(by_id) >= expected_total:
            return [by_id[item_id] for item_id in sorted(by_id)]
        if len(items) < page_size:
            if expected_total is None or len(by_id) >= expected_total:
                return [by_id[item_id] for item_id in sorted(by_id)]
            raise SellerOrchestrationError("seller_listing_inventory_incomplete")
    raise SellerOrchestrationError("seller_listing_inventory_incomplete")


def _positive_int(value: Any, code: str) -> int:
    if isinstance(value, bool):
        raise SellerOrchestrationError(code)
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise SellerOrchestrationError(code) from None
    if parsed < 1:
        raise SellerOrchestrationError(code)
    return parsed


def _positive_decimal(value: Any, code: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise SellerOrchestrationError(code) from None
    if not parsed.is_finite() or parsed <= 0:
        raise SellerOrchestrationError(code)
    return parsed


def _bounded_decimal(value: Any, minimum: Decimal, maximum: Decimal, code: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise SellerOrchestrationError(code) from None
    if not parsed.is_finite() or parsed < minimum or parsed > maximum:
        raise SellerOrchestrationError(code)
    return parsed


def _decimal_text(value: Decimal) -> str:
    return format(value.normalize(), "f")
