from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ProviderErrorDecision:
    error_class: str
    short_circuit: bool
    retry_next_provider: bool
    cooldown_seconds: int
    diagnosis_steps: tuple[str, ...]
    public_code: str
    suppress_upstream_detail: bool


_TEMPORARY_PUBLIC_CODE = "provider_pool_temporarily_unavailable"


def _compact_payload(payload: Mapping[str, Any] | str | bytes | None) -> str:
    if payload is None:
        return ""
    if isinstance(payload, bytes):
        return payload.decode("utf-8", errors="replace")
    if isinstance(payload, str):
        return payload
    try:
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        return str(payload)


def _nested_error_code(payload: Mapping[str, Any] | str | bytes | None) -> str:
    if not isinstance(payload, Mapping):
        return ""
    error = payload.get("error")
    if isinstance(error, Mapping):
        return str(error.get("code") or "").strip().lower()
    return str(payload.get("code") or "").strip().lower()


def classify_provider_error(
    status: int,
    payload: Mapping[str, Any] | str | bytes | None,
) -> ProviderErrorDecision:
    """Classify provider failures without retaining upstream error details."""

    code = _nested_error_code(payload)
    text = _compact_payload(payload).casefold()

    if status == 402 and (
        code == "fixed_merchant_unavailable" or "fixed_merchant_unavailable" in text
    ):
        return ProviderErrorDecision(
            error_class="fixed_merchant_unavailable",
            short_circuit=True,
            retry_next_provider=True,
            cooldown_seconds=60,
            diagnosis_steps=("pin", "channel_health"),
            public_code=_TEMPORARY_PUBLIC_CODE,
            suppress_upstream_detail=True,
        )

    quota_markers = (
        "账户额度不足",
        "账户余额或订阅额度不足",
        "额度不足，请充值",
        "insufficient_quota",
        "insufficient quota",
        "account balance or subscription quota",
    )
    if status == 403 and (
        code in {"insufficient_quota", "account_quota_insufficient"}
        or any(marker in text for marker in quota_markers)
    ):
        return ProviderErrorDecision(
            error_class="account_quota_insufficient",
            short_circuit=True,
            retry_next_provider=True,
            cooldown_seconds=60,
            diagnosis_steps=("balance", "transfer_policy", "pin"),
            public_code=_TEMPORARY_PUBLIC_CODE,
            suppress_upstream_detail=True,
        )

    if status == 503 and (code == "no_available_channel" or "no_available_channel" in text):
        return ProviderErrorDecision(
            error_class="no_available_channel",
            short_circuit=True,
            retry_next_provider=True,
            cooldown_seconds=60,
            diagnosis_steps=("balance", "pin", "channel_health"),
            public_code=_TEMPORARY_PUBLIC_CODE,
            suppress_upstream_detail=True,
        )

    return ProviderErrorDecision(
        error_class="provider_upstream_error",
        short_circuit=False,
        retry_next_provider=False,
        cooldown_seconds=0,
        diagnosis_steps=(),
        public_code="provider_request_failed",
        suppress_upstream_detail=False,
    )


def public_error_payload(decision: ProviderErrorDecision) -> dict[str, dict[str, str]]:
    message = (
        "Provider pool is temporarily unavailable; retry shortly."
        if decision.short_circuit
        else "Provider request failed."
    )
    return {"error": {"code": decision.public_code, "message": message}}
