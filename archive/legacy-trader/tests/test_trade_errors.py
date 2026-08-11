from a6api_auto.trade_errors import classify_provider_error, public_error_payload


def test_fixed_merchant_unavailable_402_is_short_circuited() -> None:
    decision = classify_provider_error(
        402,
        {
            "error": {
                "code": "fixed_merchant_unavailable",
                "message": "private upstream detail",
            }
        },
    )

    assert decision.error_class == "fixed_merchant_unavailable"
    assert decision.short_circuit is True
    assert decision.retry_next_provider is True
    assert decision.diagnosis_steps == ("pin", "channel_health")
    assert decision.suppress_upstream_detail is True
    assert "private upstream detail" not in str(public_error_payload(decision))


def test_quota_insufficient_403_is_short_circuited_without_detail() -> None:
    raw = (
        "Access forbidden (403): 账户额度不足，请充值或调整请求后重试。 "
        "原因：当前账户余额或订阅额度不足，本次预扣或调用未完成。 "
        "request_id: upstream-secret | consecutive_403=3/3"
    )

    decision = classify_provider_error(403, raw)

    assert decision.error_class == "account_quota_insufficient"
    assert decision.short_circuit is True
    assert decision.retry_next_provider is True
    assert decision.cooldown_seconds == 60
    assert decision.diagnosis_steps == ("balance", "transfer_policy", "pin")
    public = public_error_payload(decision)
    assert public["error"]["code"] == "provider_pool_temporarily_unavailable"
    assert "upstream-secret" not in str(public)


def test_no_available_channel_is_short_circuited() -> None:
    decision = classify_provider_error(
        503,
        {"error": {"code": "no_available_channel", "request_id": "upstream-secret"}},
    )

    assert decision.error_class == "no_available_channel"
    assert decision.short_circuit is True
    assert decision.retry_next_provider is True
    assert decision.diagnosis_steps == ("balance", "pin", "channel_health")
    assert "upstream-secret" not in str(public_error_payload(decision))


def test_unclassified_error_keeps_normal_failure_path() -> None:
    decision = classify_provider_error(502, {"error": {"code": "bad_gateway"}})

    assert decision.error_class == "provider_upstream_error"
    assert decision.short_circuit is False
    assert decision.retry_next_provider is False
    assert decision.cooldown_seconds == 0
