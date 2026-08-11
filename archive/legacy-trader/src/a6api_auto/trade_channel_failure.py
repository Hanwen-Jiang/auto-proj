from __future__ import annotations

from dataclasses import dataclass

CHANNEL_FAILURE_RESPONSIBILITIES = frozenset({"client", "provider", "platform", "unknown"})
ATTEMPT_FAILURE_OWNERS = frozenset(
    {"user", "client_transport", "provider", "platform_path", "internal", "unknown"}
)
FAILURE_CONTROL_ACTIONS = frozenset(
    {
        "none",
        "repin_immediately",
        "probe_then_repin",
        "repair_liquidity",
        "isolate_path",
        "repair_internal",
        "open_incident",
    }
)
FAILURE_CLASSIFIER_VERSION = "failure-classifier-v3"

_CLIENT_FAILURE_CODES = frozenset(
    {
        "bad_request",
        "content_policy_violation",
        "context_limit_exceeded",
        "context_length_exceeded",
        "context_too_large",
        "cyber_policy",
        "input_too_long",
        "invalid_encrypted_content",
        "invalid_json",
        "invalid_messages",
        "invalid_parameter",
        "invalid_prompt",
        "invalid_request",
        "invalid_request_error",
        "invalid_value",
        "malformed_request",
        "moderation_blocked",
        "request_too_large",
        "session_blocked_by_cyber_policy",
        "thinking_signature_invalid",
        "unsupported_client",
        "unsupported_parameter",
        "user_cancelled",
    }
)

_DETERMINISTIC_PROVIDER_FAILURE_CODES = frozenset(
    {
        "capability_mismatch",
        "channel_cooling",
        "channel_unavailable",
        "deploymentnotfound",
        "fixed_merchant_unavailable",
        "model_price_error",
        "model_not_available",
        "no_available_channel",
        "subscription_channel_unavailable",
    }
)

_LIQUIDITY_FAILURE_CODES = frozenset(
    {
        "account_quota_insufficient",
        "insufficient_quota",
        "merchant_quota_insufficient",
        "provider_liquidity_insufficient",
    }
)

_TRANSIENT_PROVIDER_FAILURE_CODES = frozenset(
    {
        "auth_unavailable",
        "bad_response_body",
        "do_request_failed",
        "missing_terminal_event",
        "rate_limit_exceeded",
        "structured_stream_error",
    }
)

_PLATFORM_PATH_FAILURE_CODES = frozenset(
    {
        "cf_1010",
        "cloudflare_1010",
        "cloudflare_forbidden",
        "egress_unavailable",
        "endpoint_unavailable",
        "platform_overloaded",
    }
)

_INTERNAL_FAILURE_CODES = frozenset(
    {
        "cache_unavailable",
        "database_unavailable",
        "dns_unavailable",
        "internal_api_key_unauthorized",
        "insufficient_user_quota",
        "local:insufficient_quota",
        "local_insufficient_quota",
        "no_available_upstream",
        "no_available_accounts",
        "pin_readback_mismatch",
        "pin_write_failed",
        "provider_key_unauthorized",
        "provider_liquidity_insufficient",
        "redis_unavailable",
        "relay_unavailable",
        "routing_unavailable",
        "scheduler_unavailable",
    }
)

_CLIENT_TRANSPORT_FAILURE_CODES = frozenset(
    {"client_cancelled", "context_canceled", "downstream_client_cancelled"}
)

_TRANSIENT_UNKNOWN_FAILURE_CODES = frozenset(
    {
        "attempt_failed",
        "bad_response",
        "connection_reset",
        "server_error",
        "server_is_overloaded",
        "system_cpu_overloaded",
        "timeout",
        "upstream_error",
        "upstream_failover",
        "upstream_timeout",
        "upstream_unavailable",
    }
)

_TRANSIENT_HTTP_STATUSES = frozenset({502, 503, 504, 524})


@dataclass(frozen=True)
class AttemptFailureClassification:
    code: str
    owner: str
    channel_responsibility: str | None
    retry_next: bool
    fast_block_slot: bool
    control_action: str
    quarantine_scope: str | None
    classifier_version: str = FAILURE_CLASSIFIER_VERSION

    def __post_init__(self) -> None:
        if self.owner not in ATTEMPT_FAILURE_OWNERS:
            raise ValueError("attempt_failure_owner_invalid")
        if self.channel_responsibility not in CHANNEL_FAILURE_RESPONSIBILITIES | {None}:
            raise ValueError("attempt_failure_responsibility_invalid")
        if self.control_action not in FAILURE_CONTROL_ACTIONS:
            raise ValueError("attempt_failure_action_invalid")


def normalize_channel_failure_code(value: object) -> str:
    code = str(value or "").strip().casefold().replace("-", "_").replace(" ", "_")
    return "_".join(part for part in code.split("_") if part)[:128]


def classify_provider_attempt_failure(
    value: object,
    *,
    http_status: object = None,
    cloudflare_evidence: bool = False,
    client_cancelled: bool = False,
    internal_fault: bool = False,
) -> AttemptFailureClassification:
    """Classify one attempt into the versioned owner/domain/action contract."""

    if not all(
        isinstance(flag, bool) for flag in (cloudflare_evidence, client_cancelled, internal_fault)
    ):
        raise ValueError("attempt_failure_evidence_invalid")
    code = normalize_channel_failure_code(value)
    status = _http_status(http_status)
    if client_cancelled or status == 499 or code in _CLIENT_TRANSPORT_FAILURE_CODES:
        return _classification(
            code,
            owner="client_transport",
            channel_responsibility="client",
            retry_next=False,
            fast_block_slot=False,
            control_action="none",
        )
    if code in _LIQUIDITY_FAILURE_CODES:
        return _classification(
            code,
            owner="provider",
            channel_responsibility=None,
            retry_next=True,
            fast_block_slot=True,
            control_action="repair_liquidity",
            quarantine_scope="account",
        )
    if internal_fault or code in _INTERNAL_FAILURE_CODES:
        return _classification(
            code,
            owner="internal",
            channel_responsibility=None,
            retry_next=True,
            fast_block_slot=False,
            control_action="repair_internal",
        )
    if code in _CLIENT_FAILURE_CODES or code.startswith(
        ("context_length_", "invalid_prompt_", "cyber_policy_")
    ):
        return _classification(
            code,
            owner="user",
            channel_responsibility="client",
            retry_next=False,
            fast_block_slot=False,
            control_action="none",
        )
    if code in _DETERMINISTIC_PROVIDER_FAILURE_CODES or code.startswith(
        ("fixed_merchant_unavailable", "no_available_channel")
    ):
        return _classification(
            code,
            owner="provider",
            channel_responsibility="provider",
            retry_next=True,
            fast_block_slot=False,
            control_action="none",
            quarantine_scope="channel",
        )
    if code in _PLATFORM_PATH_FAILURE_CODES or (cloudflare_evidence and status == 403):
        return _classification(
            code,
            owner="platform_path",
            channel_responsibility="platform",
            retry_next=True,
            fast_block_slot=False,
            control_action="isolate_path",
            quarantine_scope="endpoint",
        )
    if code in _TRANSIENT_PROVIDER_FAILURE_CODES or code.startswith("provider_"):
        return _classification(
            code,
            owner="provider",
            channel_responsibility="provider",
            retry_next=True,
            fast_block_slot=False,
            control_action="none",
            quarantine_scope="channel",
        )
    if status in _TRANSIENT_HTTP_STATUSES or code in _TRANSIENT_UNKNOWN_FAILURE_CODES:
        return _classification(
            code,
            owner="unknown",
            channel_responsibility="unknown",
            retry_next=True,
            fast_block_slot=False,
            control_action="open_incident",
        )
    return _classification(
        code,
        owner="unknown",
        channel_responsibility="unknown",
        retry_next=True,
        fast_block_slot=False,
        control_action="open_incident",
    )


def classify_channel_failure(value: object) -> str:
    """Return the four-value compatibility projection used by channel facts."""

    classification = classify_provider_attempt_failure(value)
    return classification.channel_responsibility or "unknown"


def channel_failure_affects_health(value: object) -> bool:
    classification = classify_provider_attempt_failure(value)
    return classification.owner in {"provider", "platform_path"}


def _classification(
    code: str,
    *,
    owner: str,
    channel_responsibility: str | None,
    retry_next: bool,
    fast_block_slot: bool,
    control_action: str,
    quarantine_scope: str | None = None,
) -> AttemptFailureClassification:
    return AttemptFailureClassification(
        code=code,
        owner=owner,
        channel_responsibility=channel_responsibility,
        retry_next=retry_next,
        fast_block_slot=fast_block_slot,
        control_action=control_action,
        quarantine_scope=quarantine_scope,
    )


def _http_status(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        status = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return None
    return status if 100 <= status <= 599 else None
