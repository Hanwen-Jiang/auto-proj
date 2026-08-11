from __future__ import annotations

import pytest

from a6api_auto.trade_channel_failure import (
    FAILURE_CLASSIFIER_VERSION,
    channel_failure_affects_health,
    classify_channel_failure,
    classify_provider_attempt_failure,
    normalize_channel_failure_code,
)


@pytest.mark.parametrize(
    ("code", "responsibility"),
    (
        ("invalid_prompt", "client"),
        ("invalid_encrypted_content", "client"),
        ("cyber_policy", "client"),
        ("context_length_exceeded", "client"),
        ("context_limit_exceeded", "client"),
        ("invalid_request_error", "client"),
        ("thinking_signature_invalid", "client"),
        ("do_request_failed", "provider"),
        ("bad_response_body", "provider"),
        ("DeploymentNotFound", "provider"),
        ("server_is_overloaded", "unknown"),
        ("no_available_channel", "provider"),
        ("fixed_merchant_unavailable", "provider"),
        ("subscription_channel_unavailable", "provider"),
        ("model_price_error", "provider"),
        ("timeout", "unknown"),
        ("bad_response_status_code", "unknown"),
        ("unknown_error", "unknown"),
    ),
)
def test_classifies_observed_market_failure_codes(code: str, responsibility: str) -> None:
    assert classify_channel_failure(code) == responsibility


def test_only_channel_attributable_failures_affect_health() -> None:
    assert channel_failure_affects_health("do_request_failed") is True
    assert channel_failure_affects_health("no_available_channel") is True
    assert channel_failure_affects_health("subscription_channel_unavailable") is True
    assert channel_failure_affects_health("invalid_prompt") is False
    assert channel_failure_affects_health("insufficient_user_quota") is False
    assert channel_failure_affects_health("bad_response_status_code") is False
    assert normalize_channel_failure_code(" DeploymentNotFound ") == "deploymentnotfound"


@pytest.mark.parametrize(
    ("code", "kwargs", "owner", "action", "fast_block", "scope"),
    (
        ("invalid_json", {}, "user", "none", False, None),
        (
            "context_canceled",
            {},
            "client_transport",
            "none",
            False,
            None,
        ),
        (
            "fixed_merchant_unavailable",
            {"http_status": 503},
            "provider",
            "none",
            False,
            "channel",
        ),
        (
            "missing_terminal_event",
            {},
            "provider",
            "none",
            False,
            "channel",
        ),
        (
            "access_forbidden",
            {"http_status": 403, "cloudflare_evidence": True},
            "platform_path",
            "isolate_path",
            False,
            "endpoint",
        ),
        (
            "upstream_unavailable",
            {"http_status": 503},
            "unknown",
            "open_incident",
            False,
            None,
        ),
        (
            "upstream_failover",
            {"http_status": None},
            "unknown",
            "open_incident",
            False,
            None,
        ),
        (
            "provider_key_unauthorized",
            {"http_status": 401},
            "internal",
            "repair_internal",
            False,
            None,
        ),
        (
            "insufficient_user_quota",
            {},
            "internal",
            "repair_internal",
            False,
            None,
        ),
        (
            "subscription_channel_unavailable",
            {},
            "provider",
            "none",
            False,
            "channel",
        ),
        (
            "insufficient_quota",
            {"http_status": 403},
            "provider",
            "repair_liquidity",
            True,
            "account",
        ),
        (
            "mystery_failure",
            {},
            "unknown",
            "open_incident",
            False,
            None,
        ),
    ),
)
def test_attempt_classifier_returns_versioned_owner_and_control_action(
    code: str,
    kwargs: dict[str, object],
    owner: str,
    action: str,
    fast_block: bool,
    scope: str | None,
) -> None:
    result = classify_provider_attempt_failure(code, **kwargs)

    assert result.owner == owner
    assert result.control_action == action
    assert result.fast_block_slot is fast_block
    assert result.quarantine_scope == scope
    assert result.classifier_version == FAILURE_CLASSIFIER_VERSION


def test_internal_failure_is_not_projected_as_a_channel_fact() -> None:
    result = classify_provider_attempt_failure("opaque_failure", internal_fault=True)

    assert result.owner == "internal"
    assert result.channel_responsibility is None
    assert classify_channel_failure("provider_key_unauthorized") == "unknown"


def test_http_status_alone_never_turns_unknown_4xx_into_user_fault() -> None:
    result = classify_provider_attempt_failure("opaque_4xx", http_status=422)

    assert result.owner == "unknown"
    assert result.retry_next is True
    assert result.control_action == "open_incident"


def test_attempt_classifier_rejects_truthy_non_boolean_evidence() -> None:
    with pytest.raises(ValueError, match="attempt_failure_evidence_invalid"):
        classify_provider_attempt_failure(
            "access_forbidden",
            cloudflare_evidence="false",  # type: ignore[arg-type]
        )
