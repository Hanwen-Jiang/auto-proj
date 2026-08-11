"""Production remote controls for the Provider routing/cost saga.

The adapter deliberately keeps A6 panel traffic, direct Provider probes, Enodot
account controls, billing controls, and local pin facts as separate dependencies.
Every mutation performs an immediate readback. ``readback`` then reconstructs the
complete activation evidence without trusting mutation response bodies.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from types import MappingProxyType
from typing import Any, Protocol

from .errors import A6ProbeTransportError
from .probe_prompts import build_short_probe_question
from .trade_provider_pool import ProviderBundle, ProviderSlot, SlotState
from .trade_provider_pool_reconciler import (
    ProviderPoolReadback,
    ProviderProbeResult,
    ProviderSlotReadback,
)

_DIRECT_A6_ORIGIN = "https://a6.a6api.com"
_LIQUIDITY_PROBE_CODES = frozenset(
    {"account_quota_insufficient", "insufficient_quota", "merchant_quota_insufficient"}
)
_CHANNEL_PROBE_CODES = frozenset(
    {
        "capability_mismatch",
        "channel_cooling",
        "channel_unavailable",
        "fixed_merchant_unavailable",
        "model_not_available",
        "no_available_channel",
        "provider_upstream_unavailable",
        "relay_upstream_transport_failed",
        "subscription_channel_unavailable",
        "upstream_unavailable",
    }
)
_PROVIDER_PROBE_CODES = frozenset(
    {
        "invalid_api_key",
        "provider_key_unauthorized",
        "token_disabled",
        "token_expired",
        "unauthorized",
    }
)
_PLATFORM_PROBE_CODES = frozenset(
    {
        "bad_gateway",
        "gateway_timeout",
        "internal_server_error",
        "provider_probe_backoff_active",
        "service_unavailable",
        "unknown_api_error",
    }
)
_READBACK_RETRYABLE_CODES = frozenset(
    {
        "a6_request_deadline_exceeded",
        "a6_transport_failed",
        "enodot_admin_transport_failed",
    }
)


class ProviderPoolRemoteError(RuntimeError):
    def __init__(
        self,
        code: str,
        *,
        diagnostics: Mapping[str, Any] | None = None,
    ) -> None:
        self.code = code
        self.diagnostics = MappingProxyType(dict(diagnostics or {}))
        message = code
        if self.diagnostics:
            message = (
                f"{code}: "
                f"{json.dumps(dict(self.diagnostics), ensure_ascii=True, sort_keys=True)}"
            )
        super().__init__(message)


class ProviderA6PanelClient(Protocol):
    def marketplace_pins(self, user_id: int) -> list[dict[str, Any]]: ...

    def marketplace_pin(
        self,
        user_id: int,
        *,
        token_id: int,
        channel_id: int,
        model: str,
        fallback_to_smart_routing: bool = False,
    ) -> dict[str, Any]: ...


class DirectProviderProbeClient(Protocol):
    origin: str
    proxy_url: str

    def provider_responses_probe(
        self,
        api_key: str,
        model: str,
        *,
        max_output_tokens: int = 16,
        timeout: float = 24,
        connect_timeout: float = 8,
        input_text: str | None = None,
        request_id: str | None = None,
    ) -> tuple[int, Mapping[str, Any]]: ...

    def provider_chat_completions_probe(
        self,
        api_key: str,
        model: str,
        *,
        max_output_tokens: int = 16,
        timeout: float = 24,
        connect_timeout: float = 8,
        input_text: str | None = None,
        request_id: str | None = None,
    ) -> tuple[int, Mapping[str, Any]]: ...


@dataclass(frozen=True)
class ProviderProbeProfile:
    """One secret-free, single-request profile generated for a Provider check."""

    protocol: str
    input_text: str = field(repr=False, compare=False)
    max_output_tokens: int
    delay_ms: int
    request_id: str
    prompt_fingerprint: str = ""

    def __post_init__(self) -> None:
        protocol = str(self.protocol or "").strip().casefold()
        input_text = str(self.input_text or "").strip()
        request_id = str(self.request_id or "").strip()
        fingerprint = str(self.prompt_fingerprint or "").strip().casefold()
        if protocol not in {"responses", "chat_completions"}:
            raise ProviderPoolRemoteError("provider_probe_profile_protocol_invalid")
        if not 8 <= len(input_text) <= 512:
            raise ProviderPoolRemoteError("provider_probe_profile_input_invalid")
        if (
            isinstance(self.max_output_tokens, bool)
            or not isinstance(self.max_output_tokens, int)
            or not 1 <= self.max_output_tokens <= 64
        ):
            raise ProviderPoolRemoteError("provider_probe_profile_output_limit_invalid")
        if (
            isinstance(self.delay_ms, bool)
            or not isinstance(self.delay_ms, int)
            or not 0 <= self.delay_ms <= 10_000
        ):
            raise ProviderPoolRemoteError("provider_probe_profile_delay_invalid")
        if not 16 <= len(request_id) <= 128 or any(
            character.isspace() for character in request_id
        ):
            raise ProviderPoolRemoteError("provider_probe_profile_request_id_invalid")
        expected = hashlib.sha256(input_text.encode()).hexdigest()
        if fingerprint and fingerprint != expected:
            raise ProviderPoolRemoteError("provider_probe_profile_fingerprint_invalid")
        object.__setattr__(self, "protocol", protocol)
        object.__setattr__(self, "input_text", input_text)
        object.__setattr__(self, "request_id", request_id)
        object.__setattr__(self, "prompt_fingerprint", expected)

    def observation(self) -> dict[str, Any]:
        return {
            "protocol": self.protocol,
            "prompt_fingerprint": self.prompt_fingerprint,
            "max_output_tokens": self.max_output_tokens,
            "delay_ms": self.delay_ms,
            "request_id": self.request_id,
        }


def build_provider_probe_profile(max_output_tokens: int) -> ProviderProbeProfile:
    if (
        isinstance(max_output_tokens, bool)
        or not isinstance(max_output_tokens, int)
        or not 1 <= max_output_tokens <= 64
    ):
        raise ProviderPoolRemoteError("provider_probe_profile_output_limit_invalid")
    input_text = build_short_probe_question()
    lower_budget = max(1, max_output_tokens // 2)
    output_tokens = lower_budget + secrets.randbelow(
        max_output_tokens - lower_budget + 1
    )
    return ProviderProbeProfile(
        protocol=secrets.choice(("responses", "chat_completions")),
        input_text=input_text,
        max_output_tokens=output_tokens,
        delay_ms=250 + secrets.randbelow(2_501),
        request_id=secrets.token_hex(16),
    )


class ProviderEnodotAdmin(Protocol):
    def get_account(self, account_id: int) -> dict[str, Any]: ...

    def update_account(
        self,
        account_id: int,
        payload: Mapping[str, Any],
        *,
        idempotency_key: str = "",
    ) -> dict[str, Any]: ...

    def set_schedulable(
        self,
        account_id: int,
        schedulable: bool,
        *,
        idempotency_key: str = "",
    ) -> dict[str, Any]: ...


class ProviderBillingFloor(Protocol):
    def set_floor(
        self,
        model: str,
        multiplier: Decimal,
        *,
        idempotency_key: str,
    ) -> Decimal: ...

    def read_floor(self, model: str) -> Decimal: ...


class ProviderPinBindingStore(Protocol):
    def confirm(
        self,
        token_ref_id: int,
        model: str,
        channel_id: int,
        *,
        change_reason: str,
    ) -> int: ...

    def current(self, token_ref_id: int, model: str, channel_id: int) -> int | None: ...


@dataclass(frozen=True)
class ProviderRemoteSlotContext:
    """Secret-bearing runtime context for one internal ``a6_tokens.id``."""

    token_ref_id: int
    external_token_id: int
    merchant_id: int
    a6_user_id: int
    enodot_binding_id: int
    enodot_account_id: int
    key_fingerprint: str
    panel_client: ProviderA6PanelClient | None = field(repr=False, compare=False)
    probe_api_key: str = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        for value in (
            self.token_ref_id,
            self.external_token_id,
            self.merchant_id,
            self.a6_user_id,
            self.enodot_binding_id,
            self.enodot_account_id,
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ProviderPoolRemoteError("provider_remote_context_identity_invalid")
        fingerprint = str(self.key_fingerprint or "")
        if (
            len(fingerprint) != 64
            or fingerprint != fingerprint.casefold()
            or any(character not in "0123456789abcdef" for character in fingerprint)
        ):
            raise ProviderPoolRemoteError("provider_remote_context_key_fingerprint_invalid")
        key = str(self.probe_api_key or "").strip()
        if self.panel_client is None:
            if key:
                raise ProviderPoolRemoteError("provider_remote_context_secret_pair_invalid")
        else:
            if not key.startswith("sk-") or any(character.isspace() for character in key):
                raise ProviderPoolRemoteError("provider_remote_context_probe_key_invalid")
            if not callable(
                getattr(self.panel_client, "marketplace_pin", None)
            ) or not callable(getattr(self.panel_client, "marketplace_pins", None)):
                raise ProviderPoolRemoteError("provider_remote_context_panel_client_invalid")
            if hashlib.sha256(key.encode()).hexdigest() != fingerprint:
                raise ProviderPoolRemoteError(
                    "provider_remote_context_probe_key_fingerprint_mismatch"
                )
        object.__setattr__(self, "key_fingerprint", fingerprint)
        object.__setattr__(self, "probe_api_key", key)


@dataclass(frozen=True)
class EnodotBillingFloorTarget:
    group_id: int
    user_id: int

    def __post_init__(self) -> None:
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 1
            for value in (self.group_id, self.user_id)
        ):
            raise ProviderPoolRemoteError("provider_billing_target_invalid")


class EnodotGroupUserBillingFloor:
    """Apply a model floor through one Enodot user-specific group multiplier."""

    def __init__(
        self,
        admin: Any,
        targets: Mapping[str, EnodotBillingFloorTarget],
        *,
        group_lock: Callable[[int], Any],
    ) -> None:
        normalized: dict[str, EnodotBillingFloorTarget] = {}
        for raw_model, target in targets.items():
            model = str(raw_model or "").strip()
            if not model or not isinstance(target, EnodotBillingFloorTarget):
                raise ProviderPoolRemoteError("provider_billing_target_invalid")
            normalized[model] = target
        if not normalized or not callable(group_lock):
            raise ProviderPoolRemoteError("provider_billing_configuration_invalid")
        for method in ("get_group_rate_multipliers", "merge_group_rate_multiplier"):
            if not callable(getattr(admin, method, None)):
                raise ProviderPoolRemoteError("provider_billing_admin_invalid")
        self.admin = admin
        self.targets = MappingProxyType(normalized)
        self.group_lock = group_lock

    def set_floor(
        self,
        model: str,
        multiplier: Decimal,
        *,
        idempotency_key: str,
    ) -> Decimal:
        target = self._target(model)
        desired = _positive_decimal(multiplier, "provider_billing_multiplier_invalid")
        observed = self.admin.merge_group_rate_multiplier(
            target.group_id,
            target.user_id,
            float(desired),
            lock=self.group_lock,
            visible_to_user=False,
            idempotency_key=str(idempotency_key),
        )
        value = _billing_entry(observed, target.user_id)
        if value != desired:
            raise ProviderPoolRemoteError("provider_billing_write_readback_mismatch")
        return value

    def read_floor(self, model: str) -> Decimal:
        target = self._target(model)
        return _billing_entry(
            self.admin.get_group_rate_multipliers(target.group_id),
            target.user_id,
        )

    def _target(self, model: str) -> EnodotBillingFloorTarget:
        target = self.targets.get(str(model or "").strip())
        if target is None:
            raise ProviderPoolRemoteError("provider_billing_target_missing")
        return target


class PostgresProviderPinBindingStore:
    """Persist the exact pin identity that a READY slot references by FK."""

    def __init__(self, connection_factory: Callable[[], Any]) -> None:
        if not callable(connection_factory):
            raise ProviderPoolRemoteError("provider_pin_store_configuration_invalid")
        self.connection_factory = connection_factory

    def confirm(
        self,
        token_ref_id: int,
        model: str,
        channel_id: int,
        *,
        change_reason: str,
    ) -> int:
        token_ref_id = _positive_int(token_ref_id, "provider_pin_store_identity_invalid")
        channel_id = _positive_int(channel_id, "provider_pin_store_identity_invalid")
        model = _nonempty(model, "provider_pin_store_identity_invalid")
        reason = _nonempty(change_reason, "provider_pin_store_reason_invalid")
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT id FROM a6_trade.a6_tokens WHERE id = %s FOR UPDATE",
                    (token_ref_id,),
                )
                if _row(cursor) is None:
                    raise ProviderPoolRemoteError("provider_pin_store_token_missing")
                cursor.execute(
                    """
                    UPDATE a6_trade.a6_pin_bindings
                    SET valid_to = now()
                    WHERE a6_token_id = %s AND model = %s AND valid_to IS NULL
                      AND channel_id <> %s
                    """,
                    (token_ref_id, model, channel_id),
                )
                cursor.execute(
                    """
                    INSERT INTO a6_trade.a6_pin_bindings (
                        a6_token_id, model, channel_id, fallback_to_smart_routing,
                        valid_from, confirmed_at, change_reason
                    )
                    SELECT %s, %s, %s, false, now(), now(), %s
                    WHERE NOT EXISTS (
                        SELECT 1 FROM a6_trade.a6_pin_bindings
                        WHERE a6_token_id = %s AND model = %s
                          AND channel_id = %s AND valid_to IS NULL
                    )
                    RETURNING id
                    """,
                    (
                        token_ref_id,
                        model,
                        channel_id,
                        reason,
                        token_ref_id,
                        model,
                        channel_id,
                    ),
                )
                inserted = _row(cursor)
                if inserted is None:
                    cursor.execute(
                        """
                        UPDATE a6_trade.a6_pin_bindings
                        SET confirmed_at = now(), change_reason = %s
                        WHERE a6_token_id = %s AND model = %s
                          AND channel_id = %s AND valid_to IS NULL
                        RETURNING id
                        """,
                        (reason, token_ref_id, model, channel_id),
                    )
                    inserted = _row(cursor)
                binding_id = _positive_int(
                    (inserted or {}).get("id"),
                    "provider_pin_store_confirm_failed",
                )
            connection.commit()
            return binding_id
        except Exception:
            connection.rollback()
            raise
        finally:
            _close(connection)

    def current(self, token_ref_id: int, model: str, channel_id: int) -> int | None:
        token_ref_id = _positive_int(token_ref_id, "provider_pin_store_identity_invalid")
        channel_id = _positive_int(channel_id, "provider_pin_store_identity_invalid")
        model = _nonempty(model, "provider_pin_store_identity_invalid")
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT id
                    FROM a6_trade.a6_pin_bindings
                    WHERE a6_token_id = %s AND model = %s AND channel_id = %s
                      AND valid_to IS NULL AND confirmed_at IS NOT NULL
                    """,
                    (token_ref_id, model, channel_id),
                )
                row = _row(cursor)
            return None if row is None else _positive_int(
                row.get("id"),
                "provider_pin_store_readback_invalid",
            )
        finally:
            _close(connection)


class ProviderProbeHistory(Protocol):
    def platform_backoff(self, model: str) -> ProviderProbeResult | None: ...

    def reusable_result(
        self,
        slot: ProviderSlot,
        context: ProviderRemoteSlotContext,
        model: str,
    ) -> ProviderProbeResult | None: ...


class PostgresProviderProbeHistory:
    """Reuse a confirmed Pin check and enforce durable transport backoff."""

    def __init__(
        self,
        connection_factory: Callable[[], Any],
        *,
        base_backoff_seconds: int = 90,
        max_backoff_seconds: int = 1_800,
    ) -> None:
        if not callable(connection_factory):
            raise ProviderPoolRemoteError("provider_probe_history_configuration_invalid")
        if (
            isinstance(base_backoff_seconds, bool)
            or isinstance(max_backoff_seconds, bool)
            or not 1 <= base_backoff_seconds <= max_backoff_seconds <= 86_400
        ):
            raise ProviderPoolRemoteError("provider_probe_history_backoff_invalid")
        self.connection_factory = connection_factory
        self.base_backoff_seconds = int(base_backoff_seconds)
        self.max_backoff_seconds = int(max_backoff_seconds)

    def reusable_result(
        self,
        slot: ProviderSlot,
        context: ProviderRemoteSlotContext,
        model: str,
    ) -> ProviderProbeResult | None:
        model = _nonempty(model, "provider_probe_history_identity_invalid")
        channel_id = _slot_channel(slot)
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT action.id, action.idempotency_key, action.status,
                           action.observed_after, action.completed_at,
                           greatest(
                               extract(epoch FROM (now() - action.completed_at)),
                               0
                           ) AS age_seconds
                    FROM a6_trade.trade_control_actions action
                    JOIN a6_trade.a6_pin_bindings pin
                      ON pin.a6_token_id = %s
                     AND pin.model = %s
                     AND pin.channel_id = %s
                     AND pin.valid_to IS NULL
                     AND pin.confirmed_at IS NOT NULL
                    WHERE action.action_type = 'provider_pool_probe_slot'
                      AND action.entity_type = 'provider_slot'
                      AND action.entity_id = %s
                      AND action.status IN ('completed', 'failed')
                      AND action.completed_at IS NOT NULL
                      AND action.completed_at >= pin.valid_from
                    ORDER BY action.completed_at DESC, action.id DESC
                    LIMIT 64
                    """,
                    (
                        context.token_ref_id,
                        model,
                        channel_id,
                        f"{model}:{slot.slot_no}",
                    ),
                )
                rows = _rows(cursor)
        finally:
            _close(connection)
        return self._decision(rows)

    def platform_backoff(self, model: str) -> ProviderProbeResult | None:
        """Return only an active model-wide platform backoff decision."""

        model = _nonempty(model, "provider_probe_history_identity_invalid")
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT action.id, action.idempotency_key, action.status,
                           action.observed_after, action.completed_at,
                           greatest(
                               extract(epoch FROM (now() - action.completed_at)),
                               0
                           ) AS age_seconds
                    FROM a6_trade.trade_control_actions action
                    WHERE action.action_type = 'provider_pool_probe_slot'
                      AND action.entity_type = 'provider_slot'
                      AND action.desired_state ->> 'model' = %s
                      AND action.status IN ('completed', 'failed')
                      AND action.completed_at IS NOT NULL
                    ORDER BY action.completed_at DESC, action.id DESC
                    LIMIT 64
                    """,
                    (model,),
                )
                rows = _rows(cursor)
        finally:
            _close(connection)
        result = self._decision(rows)
        if (
            result is not None
            and not result.confirmed
            and result.error_code == "provider_probe_backoff_active"
            and result.failure_owner == "platform"
        ):
            return result
        return None

    def _decision(self, rows: Sequence[Mapping[str, Any]]) -> ProviderProbeResult | None:
        attempts: list[tuple[Mapping[str, Any], Mapping[str, Any]]] = []
        for row in rows:
            observed = _mapping_document(row.get("observed_after"))
            probe = observed.get("probe")
            if not isinstance(probe, Mapping):
                continue
            if str(probe.get("error_code") or "") == "provider_probe_backoff_active":
                continue
            attempts.append((row, probe))
        if not attempts:
            return None
        latest_row, latest_probe = attempts[0]
        if latest_row.get("status") == "completed" and latest_probe.get("confirmed") is True:
            protocol = str(latest_probe.get("protocol") or "unknown").strip().casefold()
            if protocol not in {"responses", "chat_completions", "unknown"}:
                protocol = "unknown"
            return ProviderProbeResult(
                confirmed=True,
                protocol=protocol,
                http_status=200,
                error_code=None,
                failure_owner="none",
                replan_scope="none",
                document={
                    "cache": "postgres_pin_epoch",
                    "request_sent": False,
                    "source_action_id": int(latest_row["id"]),
                },
            )
        if str(latest_probe.get("failure_owner") or "") != "platform":
            return None
        consecutive = 0
        for row, probe in attempts:
            if row.get("status") == "completed" and probe.get("confirmed") is True:
                break
            if str(probe.get("failure_owner") or "") != "platform":
                break
            consecutive += 1
        if consecutive < 1:
            return None
        exponent = min(consecutive - 1, 20)
        nominal = min(
            self.max_backoff_seconds,
            self.base_backoff_seconds * (2**exponent),
        )
        persisted_key = str(latest_row.get("idempotency_key") or latest_row.get("id") or "")
        digest = hashlib.sha256(persisted_key.encode()).digest()
        jitter = 0.75 + (int.from_bytes(digest[:8], "big") / ((1 << 64) - 1)) * 0.5
        delay_seconds = nominal * jitter
        try:
            age_seconds = max(0.0, float(latest_row.get("age_seconds") or 0))
        except (TypeError, ValueError):
            raise ProviderPoolRemoteError("provider_probe_history_age_invalid") from None
        remaining_seconds = delay_seconds - age_seconds
        if remaining_seconds <= 0:
            return None
        return ProviderProbeResult(
            confirmed=False,
            protocol=str(latest_probe.get("protocol") or "unknown"),
            http_status=None,
            error_code="provider_probe_backoff_active",
            failure_owner="platform",
            replan_scope="platform",
            document={
                "request_sent": False,
                "retry_after_ms": max(1, round(remaining_seconds * 1_000)),
                "consecutive_platform_failures": consecutive,
                "last_error_code": _bounded_probe_text(
                    latest_probe.get("error_code") or "provider_probe_failed"
                ),
                "source_action_id": int(latest_row["id"]),
            },
        )


class ProviderPoolRemoteAdapter:
    """Concrete A6/Enodot/Billing implementation of ``ProviderPoolRemote``."""

    def __init__(
        self,
        model: str,
        enodot_admin: ProviderEnodotAdmin,
        billing: ProviderBillingFloor,
        pin_store: ProviderPinBindingStore,
        probe_client: DirectProviderProbeClient,
        contexts: Sequence[ProviderRemoteSlotContext],
        *,
        pre_enable_guard: Callable[[ProviderRemoteSlotContext], None],
        probe_timeout_seconds: float = 24,
        probe_connect_timeout_seconds: float = 8,
        probe_max_output_tokens: int = 16,
        probe_history: ProviderProbeHistory | None = None,
        activation_probe_mode: str = "required",
        readback_max_workers: int = 1,
        readback_max_attempts: int = 1,
        probe_profile_factory: Callable[[int], ProviderProbeProfile] = (
            build_provider_probe_profile
        ),
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.model = _nonempty(model, "provider_remote_model_invalid")
        if (
            str(getattr(probe_client, "origin", "")).rstrip("/") != _DIRECT_A6_ORIGIN
            or str(getattr(probe_client, "proxy_url", "")).strip()
        ):
            raise ProviderPoolRemoteError("provider_remote_probe_client_not_direct")
        if (
            isinstance(probe_timeout_seconds, bool)
            or not isinstance(probe_timeout_seconds, (int, float))
            or probe_timeout_seconds <= 0
        ):
            raise ProviderPoolRemoteError("provider_remote_probe_timeout_invalid")
        if (
            isinstance(probe_connect_timeout_seconds, bool)
            or not isinstance(probe_connect_timeout_seconds, (int, float))
            or probe_connect_timeout_seconds <= 0
        ):
            raise ProviderPoolRemoteError("provider_remote_probe_connect_timeout_invalid")
        if (
            isinstance(probe_max_output_tokens, bool)
            or not isinstance(probe_max_output_tokens, int)
            or not 1 <= probe_max_output_tokens <= 64
        ):
            raise ProviderPoolRemoteError("provider_remote_probe_output_limit_invalid")
        if (
            isinstance(readback_max_workers, bool)
            or not isinstance(readback_max_workers, int)
            or not 1 <= readback_max_workers <= 32
        ):
            raise ProviderPoolRemoteError("provider_remote_readback_workers_invalid")
        if (
            isinstance(readback_max_attempts, bool)
            or not isinstance(readback_max_attempts, int)
            or not 1 <= readback_max_attempts <= 5
        ):
            raise ProviderPoolRemoteError("provider_remote_readback_attempts_invalid")
        for dependency, methods, code in (
            (enodot_admin, ("get_account", "update_account", "set_schedulable"), "admin"),
            (billing, ("set_floor", "read_floor"), "billing"),
            (pin_store, ("confirm", "current"), "pin_store"),
            (
                probe_client,
                ("provider_responses_probe", "provider_chat_completions_probe"),
                "probe_client",
            ),
        ):
            if any(not callable(getattr(dependency, method, None)) for method in methods):
                raise ProviderPoolRemoteError(f"provider_remote_{code}_invalid")
        probe_mode = str(activation_probe_mode or "").strip().casefold()
        if probe_mode not in {"required", "disabled"}:
            raise ProviderPoolRemoteError("provider_remote_probe_mode_invalid")
        if probe_history is not None and not callable(
            getattr(probe_history, "reusable_result", None)
        ):
            raise ProviderPoolRemoteError("provider_remote_probe_history_invalid")
        if not callable(pre_enable_guard):
            raise ProviderPoolRemoteError("provider_remote_pre_enable_guard_invalid")
        if not callable(probe_profile_factory) or not callable(sleep):
            raise ProviderPoolRemoteError("provider_remote_probe_profile_factory_invalid")
        by_token: dict[int, ProviderRemoteSlotContext] = {}
        for context in contexts:
            if not isinstance(context, ProviderRemoteSlotContext):
                raise ProviderPoolRemoteError("provider_remote_context_invalid")
            if context.token_ref_id in by_token:
                raise ProviderPoolRemoteError("provider_remote_context_duplicate")
            by_token[context.token_ref_id] = context
        self.enodot_admin = enodot_admin
        self.billing = billing
        self.pin_store = pin_store
        self.probe_client = probe_client
        self.contexts = MappingProxyType(by_token)
        self.pre_enable_guard = pre_enable_guard
        self.probe_timeout_seconds = float(probe_timeout_seconds)
        self.probe_connect_timeout_seconds = min(
            float(probe_connect_timeout_seconds),
            self.probe_timeout_seconds,
        )
        self.probe_max_output_tokens = probe_max_output_tokens
        self.probe_history = probe_history
        self.activation_probe_mode = probe_mode
        self.readback_max_workers = readback_max_workers
        self.readback_max_attempts = readback_max_attempts
        self.probe_profile_factory = probe_profile_factory
        self.sleep = sleep
        self._successful_probe_cache: dict[
            tuple[int, int, int, str], ProviderProbeResult
        ] = {}

    def platform_backoff(self, model: str) -> ProviderProbeResult | None:
        self._assert_model(model)
        if self.activation_probe_mode == "disabled":
            return None
        if self.probe_history is None:
            return None
        checker = getattr(self.probe_history, "platform_backoff", None)
        if not callable(checker):
            return None
        result = checker(self.model)
        if result is not None and not isinstance(result, ProviderProbeResult):
            raise ProviderPoolRemoteError(
                "provider_remote_probe_history_result_invalid"
            )
        return result

    def stage_billing_floor(
        self,
        model: str,
        multiplier: Decimal,
        *,
        idempotency_key: str,
    ) -> bool:
        self._assert_model(model)
        desired = _positive_decimal(multiplier, "provider_remote_billing_floor_invalid")
        try:
            current = self.billing.read_floor(self.model)
        except ProviderPoolRemoteError as exc:
            if exc.code != "provider_billing_readback_missing":
                raise
        else:
            if current >= desired:
                return False
        observed = self.billing.set_floor(
            self.model,
            desired,
            idempotency_key=idempotency_key,
        )
        if _positive_decimal(observed, "provider_remote_billing_readback_invalid") < desired:
            raise ProviderPoolRemoteError("provider_remote_billing_readback_mismatch")
        return True

    def finalize_billing_floor(
        self,
        model: str,
        multiplier: Decimal,
        *,
        idempotency_key: str,
    ) -> bool:
        self._assert_model(model)
        desired = _positive_decimal(multiplier, "provider_remote_billing_floor_invalid")
        try:
            current = self.billing.read_floor(self.model)
        except ProviderPoolRemoteError as exc:
            if exc.code != "provider_billing_readback_missing":
                raise
        else:
            if current == desired:
                return False
        observed = self.billing.set_floor(
            self.model,
            desired,
            idempotency_key=idempotency_key,
        )
        if _positive_decimal(observed, "provider_remote_billing_readback_invalid") != desired:
            raise ProviderPoolRemoteError("provider_remote_billing_readback_mismatch")
        return True

    def read_billing_floor(self, model: str) -> Decimal:
        self._assert_model(model)
        return _positive_decimal(
            self.billing.read_floor(self.model),
            "provider_remote_billing_readback_invalid",
        )

    def set_slot_cost(
        self,
        slot: ProviderSlot,
        multiplier: Decimal,
        *,
        idempotency_key: str,
    ) -> bool:
        context = self._context(slot)
        desired = _positive_decimal(multiplier, "provider_remote_slot_cost_invalid")
        if _optional_decimal(self._account(context).get("rate_multiplier")) == desired:
            return False
        self.enodot_admin.update_account(
            context.enodot_account_id,
            {"rate_multiplier": float(desired)},
            idempotency_key=idempotency_key,
        )
        observed = self._account(context)
        if _optional_decimal(observed.get("rate_multiplier")) != desired:
            raise ProviderPoolRemoteError("provider_remote_slot_cost_readback_mismatch")
        return True

    def drain_slot(self, slot: ProviderSlot, *, idempotency_key: str) -> bool:
        context = self._context(slot)
        if self._account(context).get("schedulable") is False:
            return False
        self.enodot_admin.set_schedulable(
            context.enodot_account_id,
            False,
            idempotency_key=idempotency_key,
        )
        if self._account(context).get("schedulable") is not False:
            raise ProviderPoolRemoteError("provider_remote_drain_readback_mismatch")
        return True

    def repin_slot(self, slot: ProviderSlot, *, idempotency_key: str) -> bool:
        context = self._panel_context(slot)
        channel_id = _slot_channel(slot)
        self._assert_drained(context)
        observed_channel = _pin_channel(
            context.panel_client.marketplace_pins(context.a6_user_id),
            token_id=context.external_token_id,
            model=self.model,
            required=False,
        )
        if observed_channel == channel_id:
            self.pin_store.confirm(
                context.token_ref_id,
                self.model,
                channel_id,
                change_reason=idempotency_key,
            )
            return False
        context.panel_client.marketplace_pin(
            context.a6_user_id,
            token_id=context.external_token_id,
            channel_id=channel_id,
            model=self.model,
            fallback_to_smart_routing=False,
        )
        observed_channel = _pin_channel(
            context.panel_client.marketplace_pins(context.a6_user_id),
            token_id=context.external_token_id,
            model=self.model,
            required=True,
        )
        if observed_channel != channel_id:
            raise ProviderPoolRemoteError("provider_remote_pin_readback_mismatch")
        self.pin_store.confirm(
            context.token_ref_id,
            self.model,
            channel_id,
            change_reason=idempotency_key,
        )
        return True

    def probe_slot(
        self, slot: ProviderSlot, *, idempotency_key: str
    ) -> ProviderProbeResult:
        del idempotency_key
        if self.activation_probe_mode == "disabled":
            raise ProviderPoolRemoteError("provider_remote_probe_disabled")
        context = self._secret_context(slot)
        return self._probe_with_cache(slot, context, self.model)

    def probe_slot_for_recovery(
        self, slot: ProviderSlot, *, idempotency_key: str
    ) -> ProviderProbeResult:
        """Use one direct probe to restore a parent assignment despite backoff."""

        del idempotency_key
        if self.activation_probe_mode == "disabled":
            raise ProviderPoolRemoteError("provider_remote_probe_disabled")
        context = self._secret_context(slot)
        result = self._probe(context, self.model)
        if result.confirmed:
            key = (
                context.token_ref_id,
                context.external_token_id,
                _slot_channel(slot),
                self.model,
            )
            self._successful_probe_cache[key] = result
        return result

    def set_priority(self, slot: ProviderSlot, *, idempotency_key: str) -> bool:
        context = self._context(slot)
        priority = _slot_priority(slot)
        if _optional_int(self._account(context).get("priority")) == priority:
            return False
        self.enodot_admin.update_account(
            context.enodot_account_id,
            {"priority": priority},
            idempotency_key=idempotency_key,
        )
        if _optional_int(self._account(context).get("priority")) != priority:
            raise ProviderPoolRemoteError("provider_remote_priority_readback_mismatch")
        return True

    def set_assignment_metadata(
        self,
        bundle: ProviderBundle,
        slot: ProviderSlot,
        *,
        idempotency_key: str,
    ) -> bool:
        self._assert_model(bundle.model)
        context = self._context(slot)
        desired = _assignment_metadata(bundle, slot, context)
        account = self._account(context)
        current_extra = _account_extra(account)
        if _strict_subset_matches(current_extra, desired):
            return False
        merged = {**current_extra, **desired}
        self.enodot_admin.update_account(
            context.enodot_account_id,
            {"extra": merged},
            idempotency_key=idempotency_key,
        )
        observed_extra = _account_extra(self._account(context))
        if not _strict_subset_matches(observed_extra, merged):
            raise ProviderPoolRemoteError(
                "provider_remote_assignment_metadata_readback_mismatch"
            )
        return True

    def set_schedulable(
        self,
        slot: ProviderSlot,
        enabled: bool,
        *,
        idempotency_key: str,
    ) -> bool:
        if not isinstance(enabled, bool):
            raise ProviderPoolRemoteError("provider_remote_schedulable_invalid")
        context = self._context(slot)
        if enabled:
            self.pre_enable_guard(context)
        if self._account(context).get("schedulable") is enabled:
            return False
        self.enodot_admin.set_schedulable(
            context.enodot_account_id,
            enabled,
            idempotency_key=idempotency_key,
        )
        if self._account(context).get("schedulable") is not enabled:
            raise ProviderPoolRemoteError("provider_remote_schedulable_readback_mismatch")
        return True

    def read_slot_schedulable(self, slot: ProviderSlot) -> bool:
        value = self._account(self._context(slot)).get("schedulable")
        if not isinstance(value, bool):
            raise ProviderPoolRemoteError("provider_remote_schedulable_readback_invalid")
        return value

    def readback(
        self,
        bundle: ProviderBundle,
        *,
        deferred_slot_nos: frozenset[int] = frozenset(),
    ) -> ProviderPoolReadback:
        self._assert_model(bundle.model)
        billing_floor = self.billing.read_floor(bundle.model)
        candidates = tuple(slot for slot in bundle.slots if slot.token_id is not None)

        def read_slot_once(slot: ProviderSlot) -> tuple[int, ProviderSlotReadback]:
            if slot.token_id is None:
                raise ProviderPoolRemoteError("provider_remote_slot_identity_missing")
            context = self._panel_context(slot)
            account = self._readback_phase(
                slot,
                context,
                "enodot_account",
                lambda: self._account(context),
            )
            if slot.slot_no in deferred_slot_nos:
                # Deferred identities are never pinned: skip the marketplace
                # Pin GET entirely (no Pin, no channel, no binding).
                observed_channel = None
                pin_binding_id = None
                pin_confirmed = False
                probe_confirmed = False
                desired_metadata = {}
            else:
                raw_pins = self._readback_phase(
                    slot,
                    context,
                    "a6_marketplace_pins",
                    lambda: context.panel_client.marketplace_pins(context.a6_user_id),
                )
                observed_channel = self._readback_phase(
                    slot,
                    context,
                    "a6_marketplace_pin_parse",
                    lambda: _pin_channel(
                        raw_pins,
                        token_id=context.external_token_id,
                        model=bundle.model,
                        required=False,
                    ),
                )
                pin_binding_id = (
                    self._readback_phase(
                        slot,
                        context,
                        "postgres_pin_binding",
                        lambda: self.pin_store.current(
                            context.token_ref_id,
                            bundle.model,
                            slot.channel_id,
                        ),
                    )
                    if observed_channel == slot.channel_id and slot.channel_id is not None
                    else None
                )
                pin_confirmed = (
                    pin_binding_id is not None and observed_channel == slot.channel_id
                )
                probe_confirmed = False
                if self.activation_probe_mode == "required" and pin_confirmed:
                    probe_confirmed = self._probe_with_cache(
                        slot,
                        self._secret_context(slot),
                        bundle.model,
                    ).confirmed
                desired_metadata = _assignment_metadata(bundle, slot, context)
            account_active = str(account.get("status") or "active").strip().casefold() == "active"
            observed_extra = _account_extra(account)
            assignment_metadata = MappingProxyType(
                {key: observed_extra.get(key) for key in desired_metadata}
            )
            return slot.slot_no, ProviderSlotReadback(
                slot_no=slot.slot_no,
                account_id=context.enodot_account_id,
                token_id=context.token_ref_id if observed_channel is not None else None,
                channel_id=observed_channel,
                priority=_optional_int(account.get("priority")),
                rate_multiplier=_optional_decimal(account.get("rate_multiplier")),
                schedulable=account.get("schedulable") is True and account_active,
                pin_binding_id=pin_binding_id,
                pin_confirmed=pin_confirmed,
                probe_confirmed=probe_confirmed,
                assignment_metadata=assignment_metadata,
            )

        def read_slot(slot: ProviderSlot) -> tuple[int, ProviderSlotReadback]:
            started = time.monotonic()
            last_error: ProviderPoolRemoteError | None = None
            for _attempt in range(1, self.readback_max_attempts + 1):
                try:
                    return read_slot_once(slot)
                except ProviderPoolRemoteError as exc:
                    if exc.code not in _READBACK_RETRYABLE_CODES:
                        raise
                    last_error = exc
            if last_error is None:
                raise ProviderPoolRemoteError("provider_remote_slot_readback_failed")
            diagnostics = dict(last_error.diagnostics)
            diagnostics.update(
                {
                    "attempts": self.readback_max_attempts,
                    "elapsed_ms": max(
                        0,
                        int((time.monotonic() - started) * 1000),
                    ),
                }
            )
            raise ProviderPoolRemoteError(
                last_error.code,
                diagnostics=diagnostics,
            ) from last_error

        if self.readback_max_workers == 1 or len(candidates) <= 1:
            observed = [read_slot(slot) for slot in candidates]
        else:
            with ThreadPoolExecutor(
                max_workers=min(self.readback_max_workers, len(candidates)),
                thread_name_prefix="a6-provider-readback",
            ) as executor:
                observed = list(executor.map(read_slot, candidates))
        slots = dict(observed)
        return ProviderPoolReadback(bundle.model, billing_floor, MappingProxyType(slots))

    def _readback_phase(
        self,
        slot: ProviderSlot,
        context: ProviderRemoteSlotContext,
        phase: str,
        call: Callable[[], Any],
    ) -> Any:
        started = time.monotonic()
        try:
            return call()
        except Exception as exc:
            inherited = getattr(exc, "diagnostics", None)
            diagnostics = dict(inherited) if isinstance(inherited, Mapping) else {}
            cause_code = _remote_exception_code(exc)
            diagnostics.update(
                {
                    "cause_code": cause_code,
                    "phase": phase,
                    "slot_no": slot.slot_no,
                    "channel_id": slot.channel_id,
                    "a6_user_id": context.a6_user_id,
                    "account_id": context.enodot_account_id,
                    "token_ref_id": context.token_ref_id,
                    "proxy_configured": bool(
                        str(getattr(context.panel_client, "proxy_url", "")).strip()
                    ),
                    "phase_elapsed_ms": max(
                        0,
                        int((time.monotonic() - started) * 1000),
                    ),
                }
            )
            raise ProviderPoolRemoteError(
                cause_code,
                diagnostics=diagnostics,
            ) from exc

    def _context(self, slot: ProviderSlot) -> ProviderRemoteSlotContext:
        if not isinstance(slot, ProviderSlot) or slot.token_id is None:
            raise ProviderPoolRemoteError("provider_remote_slot_identity_missing")
        context = self.contexts.get(slot.token_id)
        if context is None:
            raise ProviderPoolRemoteError("provider_remote_context_missing")
        if (
            slot.account_id != context.enodot_account_id
            or slot.enodot_binding_id != context.enodot_binding_id
        ):
            raise ProviderPoolRemoteError("provider_remote_context_identity_mismatch")
        return context

    def _panel_context(self, slot: ProviderSlot) -> ProviderRemoteSlotContext:
        context = self._context(slot)
        if context.panel_client is None:
            raise ProviderPoolRemoteError("provider_remote_secret_context_missing")
        return context

    def _secret_context(self, slot: ProviderSlot) -> ProviderRemoteSlotContext:
        context = self._panel_context(slot)
        if not context.probe_api_key:
            raise ProviderPoolRemoteError("provider_remote_secret_context_missing")
        return context

    def _assert_model(self, model: object) -> None:
        if str(model or "").strip() != self.model:
            raise ProviderPoolRemoteError("provider_remote_model_mismatch")

    def _account(self, context: ProviderRemoteSlotContext) -> Mapping[str, Any]:
        account = self.enodot_admin.get_account(context.enodot_account_id)
        if not isinstance(account, Mapping) or _optional_int(account.get("id")) != (
            context.enodot_account_id
        ):
            raise ProviderPoolRemoteError("provider_remote_account_identity_mismatch")
        return account

    def _assert_drained(self, context: ProviderRemoteSlotContext) -> None:
        if self._account(context).get("schedulable") is not False:
            raise ProviderPoolRemoteError("provider_remote_repin_requires_drained_slot")

    def _probe(
        self, context: ProviderRemoteSlotContext, model: str
    ) -> ProviderProbeResult:
        model = _nonempty(model, "provider_remote_model_invalid")
        profile = self.probe_profile_factory(self.probe_max_output_tokens)
        if not isinstance(profile, ProviderProbeProfile):
            raise ProviderPoolRemoteError("provider_remote_probe_profile_invalid")
        if profile.max_output_tokens > self.probe_max_output_tokens:
            raise ProviderPoolRemoteError("provider_remote_probe_profile_budget_exceeded")
        if profile.delay_ms:
            self.sleep(profile.delay_ms / 1_000)
        method = (
            self.probe_client.provider_responses_probe
            if profile.protocol == "responses"
            else self.probe_client.provider_chat_completions_probe
        )
        try:
            status, document = method(
                context.probe_api_key,
                model,
                max_output_tokens=profile.max_output_tokens,
                timeout=self.probe_timeout_seconds,
                connect_timeout=self.probe_connect_timeout_seconds,
                input_text=profile.input_text,
                request_id=profile.request_id,
            )
        except A6ProbeTransportError as exc:
            return ProviderProbeResult(
                confirmed=False,
                protocol=profile.protocol,
                http_status=None,
                error_code=exc.code,
                failure_owner="platform",
                replan_scope="platform",
                document={
                    "profile": profile.observation(),
                    "request_sent": True,
                    "transport": dict(exc.diagnostics),
                },
            )
        if not 200 <= int(status) < 300:
            return _failed_probe_result(
                profile.protocol,
                status,
                document,
                profile=profile,
            )
        if profile.protocol == "responses":
            if document.get("probe_event") != "response.created":
                return ProviderProbeResult(
                    confirmed=False,
                    protocol=profile.protocol,
                    http_status=int(status),
                    error_code="provider_responses_probe_response_invalid",
                    failure_owner="unknown",
                    replan_scope="unknown",
                    document={
                        "profile": profile.observation(),
                        "request_sent": True,
                    },
                )
            result_document = _redacted_probe_document(document)
        else:
            choices = document.get("choices")
            if not isinstance(choices, list) or not choices:
                return ProviderProbeResult(
                    confirmed=False,
                    protocol=profile.protocol,
                    http_status=int(status),
                    error_code="provider_chat_probe_response_invalid",
                    failure_owner="unknown",
                    replan_scope="unknown",
                    document={
                        "profile": profile.observation(),
                        "request_sent": True,
                        "choice_count": 0,
                    },
                )
            result_document = {"choice_count": len(choices)}
        return ProviderProbeResult(
            confirmed=True,
            protocol=profile.protocol,
            http_status=int(status),
            error_code=None,
            failure_owner="none",
            replan_scope="none",
            document={
                "profile": profile.observation(),
                "request_sent": True,
                "result": result_document,
            },
        )

    def _probe_with_cache(
        self,
        slot: ProviderSlot,
        context: ProviderRemoteSlotContext,
        model: str,
    ) -> ProviderProbeResult:
        if slot.channel_id is None:
            raise ProviderPoolRemoteError("provider_remote_probe_channel_missing")
        key = (
            context.token_ref_id,
            context.external_token_id,
            slot.channel_id,
            _nonempty(model, "provider_remote_model_invalid"),
        )
        cached = self._successful_probe_cache.get(key)
        if cached is not None:
            return cached
        if self.probe_history is not None:
            historical = self.probe_history.reusable_result(slot, context, model)
            if historical is not None and not isinstance(historical, ProviderProbeResult):
                raise ProviderPoolRemoteError(
                    "provider_remote_probe_history_result_invalid"
                )
            if historical is not None:
                if historical.confirmed:
                    self._successful_probe_cache[key] = historical
                return historical
        result = self._probe(context, model)
        if result.confirmed:
            self._successful_probe_cache[key] = result
        return result


def _failed_probe_result(
    protocol: str,
    status: object,
    document: Mapping[str, Any],
    *,
    profile: ProviderProbeProfile | None = None,
) -> ProviderProbeResult:
    http_status = int(status)
    error = document.get("error") if isinstance(document, Mapping) else None
    error = error if isinstance(error, Mapping) else document
    raw_code = ""
    if isinstance(error, Mapping):
        raw_code = str(error.get("code") or error.get("type") or "")
    code = _normalized_probe_code(raw_code) or f"provider_probe_http_{http_status}"
    if code in _LIQUIDITY_PROBE_CODES:
        owner, scope = "liquidity", "liquidity"
    elif code in _CHANNEL_PROBE_CODES:
        owner, scope = "channel", "channel"
    elif code in _PROVIDER_PROBE_CODES or http_status == 401:
        owner, scope = "provider", "provider"
    elif (
        code in _PLATFORM_PROBE_CODES
        or code.startswith(("cf_", "cloudflare_", "egress_", "endpoint_"))
        or http_status in {408, 425, 429, 500, 502, 504}
    ):
        owner, scope = "platform", "platform"
    else:
        owner, scope = "unknown", "unknown"
    result_document: dict[str, Any] = {
        "request_sent": True,
        "response": _redacted_probe_document(document),
    }
    if profile is not None:
        result_document["profile"] = profile.observation()
    return ProviderProbeResult(
        confirmed=False,
        protocol=protocol,
        http_status=http_status,
        error_code=code,
        failure_owner=owner,
        replan_scope=scope,
        document=result_document,
    )


def _normalized_probe_code(value: object) -> str:
    code = str(value or "").strip().casefold().replace("-", "_").replace(" ", "_")
    return "_".join(part for part in code.split("_") if part)[:128]


def _redacted_probe_document(document: object) -> dict[str, Any]:
    if not isinstance(document, Mapping):
        return {}
    error = document.get("error")
    if isinstance(error, Mapping):
        allowed = {
            key: _bounded_probe_text(error.get(key))
            for key in ("type", "code", "reason", "message", "request_id")
            if error.get(key) is not None
        }
        return {"error": allowed}
    result: dict[str, Any] = {}
    for key in ("type", "code", "reason", "message", "request_id", "probe_event"):
        if document.get(key) is not None:
            result[key] = _bounded_probe_text(document.get(key))
    return result


def _bounded_probe_text(value: object) -> str:
    return str(value or "").strip()[:512]


def _assignment_metadata(
    bundle: ProviderBundle,
    slot: ProviderSlot,
    context: ProviderRemoteSlotContext,
) -> dict[str, Any]:
    if slot.slot_state is not SlotState.READY:
        raise ProviderPoolRemoteError("provider_remote_assignment_slot_not_ready")
    channel_id = _slot_channel(slot)
    planned_cost = bundle.cost_plan.desired_slot_costs.get(slot.slot_no)
    if planned_cost is None:
        raise ProviderPoolRemoteError("provider_remote_assignment_cost_missing")
    cost = _positive_decimal(planned_cost, "provider_remote_assignment_cost_invalid")
    return {
        "a6_provider": True,
        "a6_api_key_fingerprint": context.key_fingerprint,
        "a6_provider_routing_generation": bundle.routing_generation,
        "a6_provider_cost_generation": bundle.cost_generation,
        "a6_provider_slot_no": slot.slot_no,
        "a6_provider_token_ref_id": context.token_ref_id,
        "a6_provider_channel_id": channel_id,
        "external_a6_token_id": context.external_token_id,
        "a6_provider_rank": _slot_rank(slot),
        "a6_provider_cost_multiplier": format(cost, "f"),
        "a6_provider_source_hash": bundle.source_hash,
        "a6_provider_cost_input_hash": bundle.effective_cost_input_hash,
        "a6_provider_cost_output_hash": bundle.cost_plan.cost_output_hash,
    }


def _account_extra(account: Mapping[str, Any]) -> dict[str, Any]:
    value = account.get("extra")
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ProviderPoolRemoteError("provider_remote_account_extra_invalid")
    return dict(value)


def _strict_subset_matches(
    observed: Mapping[str, Any], expected: Mapping[str, Any]
) -> bool:
    return all(
        key in observed
        and type(observed[key]) is type(value)
        and observed[key] == value
        for key, value in expected.items()
    )

def _slot_channel(slot: ProviderSlot) -> int:
    return _positive_int(slot.channel_id, "provider_remote_slot_channel_invalid")


def _slot_rank(slot: ProviderSlot) -> int:
    return _positive_int(slot.rank, "provider_remote_slot_rank_invalid")


def _slot_priority(slot: ProviderSlot) -> int:
    return _positive_int(slot.priority, "provider_remote_slot_priority_invalid")


def _pin_channel(
    pins: Sequence[Mapping[str, Any]],
    *,
    token_id: int,
    model: str,
    required: bool,
) -> int | None:
    matches = [
        pin
        for pin in pins
        if isinstance(pin, Mapping)
        and _optional_int(pin.get("token_id")) == token_id
        and str(pin.get("model_name") or pin.get("model") or "").strip() == model
        and not bool(pin.get("deleted"))
    ]
    if not matches:
        if required:
            raise ProviderPoolRemoteError("provider_remote_pin_readback_missing")
        return None
    if len(matches) != 1:
        raise ProviderPoolRemoteError("provider_remote_pin_readback_not_unique")
    direct = _optional_int(matches[0].get("channel_id"))
    channel = matches[0].get("channel")
    nested = _optional_int(channel.get("id")) if isinstance(channel, Mapping) else None
    observed = direct or nested
    if observed is None:
        raise ProviderPoolRemoteError("provider_remote_pin_channel_missing")
    return observed


def _billing_entry(entries: object, user_id: int) -> Decimal:
    if not isinstance(entries, Sequence) or isinstance(entries, (str, bytes)):
        raise ProviderPoolRemoteError("provider_billing_readback_invalid")
    matches = [
        item
        for item in entries
        if isinstance(item, Mapping) and _optional_int(item.get("user_id")) == user_id
    ]
    if len(matches) != 1:
        raise ProviderPoolRemoteError("provider_billing_readback_missing")
    value = _optional_decimal(matches[0].get("rate_multiplier"))
    if value is None or value <= 0:
        raise ProviderPoolRemoteError("provider_billing_readback_invalid")
    return value


def _positive_decimal(value: object, code: str) -> Decimal:
    result = _optional_decimal(value)
    if result is None or result <= 0:
        raise ProviderPoolRemoteError(code)
    return result


def _remote_exception_code(exc: BaseException) -> str:
    code = str(getattr(exc, "code", "") or exc).split(":", 1)[0].strip()
    return code or type(exc).__name__


def _optional_decimal(value: object) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return result if result.is_finite() else None


def _positive_int(value: object, code: str) -> int:
    result = _optional_int(value)
    if result is None:
        raise ProviderPoolRemoteError(code)
    return result


def _optional_int(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = int(value)
    except (TypeError, ValueError):
        return None
    return result if result > 0 else None


def _nonempty(value: object, code: str) -> str:
    result = str(value or "").strip()
    if not result:
        raise ProviderPoolRemoteError(code)
    return result


def _row(cursor: Any) -> Mapping[str, Any] | None:
    row = cursor.fetchone()
    if row is None:
        return None
    if isinstance(row, Mapping):
        return row
    description = getattr(cursor, "description", None)
    if isinstance(row, (tuple, list)) and description:
        return {
            str(getattr(column, "name", column[0])): value
            for column, value in zip(description, row, strict=False)
        }
    raise ProviderPoolRemoteError("provider_pin_store_row_invalid")


def _rows(cursor: Any) -> list[Mapping[str, Any]]:
    values = cursor.fetchall()
    if not values:
        return []
    if isinstance(values[0], Mapping):
        return [dict(value) for value in values]
    description = getattr(cursor, "description", None)
    if not description:
        raise ProviderPoolRemoteError("provider_probe_history_row_invalid")
    names = [str(getattr(column, "name", column[0])) for column in description]
    return [dict(zip(names, value, strict=False)) for value in values]


def _mapping_document(value: object) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return parsed if isinstance(parsed, Mapping) else {}
    return {}


def _close(connection: Any) -> None:
    close = getattr(connection, "close", None)
    if callable(close):
        close()
