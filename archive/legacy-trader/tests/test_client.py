from __future__ import annotations

import json

import pytest
from curl_cffi import CurlOpt

from a6api_auto.client import A6ApiClient
from a6api_auto.errors import (
    A6AutoError,
    A6HttpError,
    A6MutationError,
    A6ProbeTransportError,
    A6TransportError,
)


class FakeCookies:
    def __init__(self) -> None:
        self.values = {}

    def set(self, name, value, **_kwargs) -> None:
        self.values[name] = value

    def get_dict(self):
        return dict(self.values)


class Response:
    def __init__(self, status: int, document: dict) -> None:
        self.status_code = status
        self.document = document

    def json(self):
        return self.document


class StreamResponse(Response):
    def __init__(self, status: int, document: dict, lines=()) -> None:
        super().__init__(status, document)
        self.lines = tuple(lines)
        self.closed = False

    def iter_lines(self):
        return iter(self.lines)

    def close(self):
        self.closed = True


class DeferredJsonStreamResponse(StreamResponse):
    def __init__(self, status: int, document: dict) -> None:
        super().__init__(status, document)
        self.raw = json.dumps(document).encode()
        self.consumed = False

    def json(self):
        if not self.consumed:
            raise json.JSONDecodeError("stream not consumed", "", 0)
        return self.document

    def iter_content(self):
        self.consumed = True
        yield self.raw


class FakeSession:
    def __init__(self, responses) -> None:
        self.responses = iter(responses)
        self.calls = []
        self.cookies = FakeCookies()
        self.curl_options = {}
        self.curl_option_snapshots = []

    def request(self, **kwargs):
        self.calls.append(kwargs)
        self.curl_option_snapshots.append(dict(self.curl_options))
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return response


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), True])
def test_client_rejects_invalid_timeout(timeout) -> None:
    with pytest.raises(A6AutoError, match="a6_timeout_invalid"):
        A6ApiClient(session=FakeSession([]), timeout=timeout)


def test_client_uses_configured_timeout() -> None:
    session = FakeSession([Response(200, {"success": True})])
    client = A6ApiClient(session=session, timeout=7.5)

    client.send_verification("fixture@qq.com")

    assert session.calls[0]["timeout"] == 7.5


def test_client_fork_uses_independent_session_and_authenticated_cookies() -> None:
    session = FakeSession([])
    client = A6ApiClient(
        "https://a6.fixture.invalid",
        proxy_url="http://proxy.fixture.invalid:1234",
        session=session,
        timeout=5,
    )
    client.import_cookies({"session": "fixture-cookie"})

    forked = client.fork()

    assert forked is not client
    assert forked.session is not client.session
    assert forked.origin == client.origin
    assert forked.proxy_url == client.proxy_url
    assert forked.timeout == client.timeout
    assert forked.export_cookies() == {"session": "fixture-cookie"}


def test_idempotent_get_retries_one_transport_failure() -> None:
    session = FakeSession(
        [
            RuntimeError("fixture TLS handshake failed"),
            Response(200, {"success": True, "data": []}),
        ]
    )
    client = A6ApiClient(session=session)

    assert client.marketplace_pins(9812) == []
    assert len(session.calls) == 2
    assert all(call["method"] == "GET" for call in session.calls)


def test_idempotent_get_retries_share_one_total_timeout(monkeypatch) -> None:
    session = FakeSession(
        [
            RuntimeError("fixture TLS handshake failed"),
            Response(200, {"success": True, "data": []}),
        ]
    )
    client = A6ApiClient(session=session, timeout=8)
    clock = iter((100.0, 100.0, 103.0))
    monkeypatch.setattr("a6api_auto.client.time.monotonic", lambda: next(clock))

    assert client.marketplace_pins(9812) == []

    assert session.calls[0]["timeout"] == 8
    assert session.calls[1]["timeout"] == 5


def test_non_idempotent_post_does_not_retry_transport_failure() -> None:
    session = FakeSession(
        [
            RuntimeError("fixture TLS handshake failed"),
            Response(200, {"success": True, "data": {}}),
        ]
    )
    client = A6ApiClient(session=session)

    with pytest.raises(A6AutoError, match="a6_transport_failed"):
        client.marketplace_pin(
            9812,
            token_id=101,
            channel_id=815,
            model="gpt-5.6-sol",
        )

    assert len(session.calls) == 1
    assert session.calls[0]["method"] == "POST"


def test_transport_failure_retains_bounded_request_and_proxy_diagnostics() -> None:
    session = FakeSession(
        [
            RuntimeError("fixture proxy secret"),
            RuntimeError("fixture proxy secret"),
        ]
    )
    client = A6ApiClient(
        session=session,
        proxy_url="socks5://proxy-user:proxy-secret@127.0.0.1:23081",
        timeout=5,
    )

    with pytest.raises(A6TransportError, match="a6_transport_failed") as captured:
        client.marketplace_pins(9812)

    diagnostics = dict(captured.value.diagnostics)
    elapsed_ms = diagnostics.pop("elapsed_ms")
    assert elapsed_ms >= 0
    assert diagnostics == {
        "method": "GET",
        "path": "/api/marketplace/pins",
        "a6_user_id": 9812,
        "proxy_configured": True,
        "proxy_host": "127.0.0.1",
        "proxy_port": 23081,
        "timeout_seconds": 5.0,
        "attempts": 2,
        "phase": "request",
        "cause_type": "RuntimeError",
        "http_status": None,
    }
    assert "proxy-secret" not in repr(diagnostics)


def test_provider_probe_stops_after_first_valid_created_event() -> None:
    response = StreamResponse(
        200,
        {},
        (
            b"event: response.created",
            b'data: {"type":"response.created","response":{"id":"private"}}',
            b'data: {"type":"response.output_text.delta","delta":"expensive"}',
        ),
    )
    session = FakeSession([response])
    client = A6ApiClient(proxy_url="http://provider-proxy", session=session, timeout=15)

    status, document = client.provider_responses_probe(
        "sk-provider-secret-key",
        "gpt-5.6-sol",
        timeout=6,
        connect_timeout=3,
        input_text="Answer briefly: what is 12 plus 7?",
        request_id="fixture-response-request-1",
    )

    assert (status, document) == (200, {"probe_event": "response.created"})
    assert response.closed is True
    request = session.calls[0]
    assert request["url"] == "https://a6.a6api.com/v1/responses"
    assert request["headers"]["Accept"] == "text/event-stream"
    assert request["headers"]["Authorization"] == "Bearer sk-provider-secret-key"
    assert request["headers"]["X-Request-ID"] == "fixture-response-request-1"
    assert request["proxies"]["https"] == "http://provider-proxy"
    assert request["timeout"] == 6
    assert session.curl_option_snapshots[0][CurlOpt.TIMEOUT_MS] == 6_000
    assert session.curl_option_snapshots[0][CurlOpt.CONNECTTIMEOUT_MS] == 3_000
    assert session.curl_options == {}
    assert request["stream"] is True
    assert request["json"] == {
        "model": "gpt-5.6-sol",
        "input": "Answer briefly: what is 12 plus 7?",
        "max_output_tokens": 16,
        "reasoning": {"effort": "low"},
        "stream": True,
    }


def test_provider_probe_returns_structured_error_and_closes_stream() -> None:
    response = StreamResponse(
        402,
        {"error": {"code": "fixed_merchant_unavailable", "message": "private"}},
    )
    client = A6ApiClient(session=FakeSession([response]))

    status, document = client.provider_responses_probe(
        "sk-provider-secret-key",
        "gpt-5.6-sol",
    )

    assert status == 402
    assert document["error"]["code"] == "fixed_merchant_unavailable"
    assert response.closed is True


def test_provider_probe_consumes_non_2xx_stream_before_parsing_error() -> None:
    response = DeferredJsonStreamResponse(
        503,
        {
            "error": {
                "code": "fixed_merchant_unavailable",
                "type": "fixed_merchant_unavailable",
                "message": "private",
            }
        },
    )
    client = A6ApiClient(session=FakeSession([response]))

    status, document = client.provider_responses_probe(
        "sk-provider-secret-key",
        "gpt-5.6-sol",
    )

    assert status == 503
    assert document["error"]["code"] == "fixed_merchant_unavailable"
    assert response.consumed is True
    assert response.closed is True


def test_provider_probe_sanitizes_error_sse_for_shared_classifier() -> None:
    response = StreamResponse(
        200,
        {},
        (
            b"event: error",
            b'data: {"type":"error","error":{"code":"fixed_merchant_unavailable",'
            b'"message":"private upstream request id"}}',
        ),
    )
    client = A6ApiClient(session=FakeSession([response]))

    status, document = client.provider_responses_probe(
        "sk-provider-secret-key",
        "gpt-5.6-sol",
    )

    assert status == 402
    assert document == {"error": {"code": "fixed_merchant_unavailable"}}
    assert "private upstream request id" not in str(document)
    assert response.closed is True


def test_provider_normal_request_uses_bounded_nonstream_payload() -> None:
    session = FakeSession(
        [Response(200, {"id": "resp_fixture", "status": "completed", "output": []})]
    )
    client = A6ApiClient(
        proxy_url="http://provider-proxy",
        session=session,
        timeout=15,
    )

    status, document = client.provider_responses_request(
        "sk-provider-secret-key",
        "gpt-5.6-sol",
        "Summarize the incident timeline in three concise bullet points.",
        max_output_tokens=96,
        timeout=12,
    )

    assert status == 200
    assert document["status"] == "completed"
    request = session.calls[0]
    assert request["headers"]["Accept"] == "application/json"
    assert request["headers"]["Authorization"] == "Bearer sk-provider-secret-key"
    assert request["proxies"]["https"] == "http://provider-proxy"
    assert request["timeout"] == 12
    assert request["json"] == {
        "model": "gpt-5.6-sol",
        "input": "Summarize the incident timeline in three concise bullet points.",
        "max_output_tokens": 96,
        "reasoning": {"effort": "low"},
        "stream": False,
    }


def test_provider_chat_probe_uses_minimal_nonstream_payload() -> None:
    session = FakeSession(
        [Response(200, {"choices": [{"message": {"role": "assistant", "content": "."}}]})]
    )
    client = A6ApiClient(
        proxy_url="http://provider-proxy",
        session=session,
        timeout=15,
    )

    status, document = client.provider_chat_completions_probe(
        "sk-provider-secret-key",
        "gpt-5.6-sol",
        max_output_tokens=8,
        timeout=4,
        connect_timeout=2,
        input_text="Reply briefly: which is larger, 14 or 9?",
        request_id="fixture-chat-request-1",
    )

    assert status == 200
    assert document["choices"]
    request = session.calls[0]
    assert request["url"] == "https://a6.a6api.com/v1/chat/completions"
    assert request["headers"]["Accept"] == "application/json"
    assert request["headers"]["Authorization"] == "Bearer sk-provider-secret-key"
    assert request["headers"]["X-Request-ID"] == "fixture-chat-request-1"
    assert request["proxies"]["https"] == "http://provider-proxy"
    assert request["timeout"] == 4
    assert session.curl_option_snapshots[0][CurlOpt.TIMEOUT_MS] == 4_000
    assert session.curl_option_snapshots[0][CurlOpt.CONNECTTIMEOUT_MS] == 2_000
    assert request["json"] == {
        "model": "gpt-5.6-sol",
        "messages": [
            {"role": "user", "content": "Reply briefly: which is larger, 14 or 9?"}
        ],
        "max_completion_tokens": 8,
        "stream": False,
    }


def test_provider_probe_transport_error_keeps_tls_stage_and_curl_code() -> None:
    class CurlTimeout(RuntimeError):
        code = 28

    session = FakeSession([CurlTimeout("SSL connection timeout for sk-secret-material")])
    client = A6ApiClient(session=session, timeout=24)

    with pytest.raises(A6ProbeTransportError) as raised:
        client.provider_responses_probe(
            "sk-provider-secret-key",
            "gpt-5.6-sol",
            timeout=24,
            connect_timeout=8,
            input_text="Answer briefly: what is 12 plus 7?",
            request_id="fixture-transport-request-1",
        )

    assert str(raised.value) == "provider_probe_transport_failed"
    assert raised.value.diagnostics["protocol"] == "responses"
    assert raised.value.diagnostics["stage"] == "tls_handshake"
    assert raised.value.diagnostics["curl_code"] == 28
    assert raised.value.diagnostics["http_status"] is None
    assert raised.value.diagnostics["request_id"] == "fixture-transport-request-1"
    assert "sk-secret-material" not in str(raised.value.diagnostics)


@pytest.mark.parametrize("text", ("", "short"))
def test_provider_normal_request_rejects_tiny_inputs(text: str) -> None:
    client = A6ApiClient(session=FakeSession([]))

    with pytest.raises(A6AutoError, match="provider_request_configuration_invalid"):
        client.provider_responses_request(
            "sk-provider-secret-key",
            "gpt-5.6-sol",
            text,
        )


def test_register_login_and_supplier_requests_use_current_protocol() -> None:
    session = FakeSession(
        [
            Response(200, {"success": True}),
            Response(200, {"success": True}),
            Response(200, {"success": True, "data": {"id": 9517, "username": "fixture"}}),
            Response(200, {"success": True, "data": {"supplier_id": 298, "request_id": 1066}}),
            Response(200, {"success": True, "data": [{"id": 1066, "status": 0}]}),
        ]
    )
    client = A6ApiClient(session=session)

    client.send_verification("fixture@qq.com")
    client.register("fixture", "fixture@qq.com", "password", "654321")
    login = client.login("fixture", "password")
    applied = client.apply_supplier(9517, {"supplier": {}, "channel": {}})
    requests = client.supplier_requests(9517)

    assert login["id"] == 9517
    assert applied == {"supplier_id": 298, "request_id": 1066}
    assert requests == [{"id": 1066, "status": 0}]
    assert session.calls[0]["url"] == "https://a6api.com/api/verification"
    assert session.calls[0]["params"] == {"email": "fixture@qq.com", "turnstile": ""}
    assert session.calls[1]["url"] == "https://a6api.com/api/user/register"
    assert session.calls[1]["json"]["verification_code"] == "654321"
    assert session.calls[3]["url"] == "https://a6api.com/api/supplier/apply"
    assert session.calls[3]["headers"]["New-API-User"] == "9517"
    assert session.calls[4]["url"] == "https://a6api.com/api/supplier/me/requests"


@pytest.mark.parametrize("document", [{"success": True}, {"success": True, "data": None}])
def test_supplier_apply_accepts_success_without_data(document) -> None:
    client = A6ApiClient(session=FakeSession([Response(200, document)]))

    result = client.apply_supplier(9517, {"supplier": {}, "channel": {}})

    assert result == {}


def test_supplier_apply_preserves_pending_request_code_without_response_details() -> None:
    document = {
        "success": False,
        "error": {"code": "pending_request_exists", "message": "private detail"},
    }
    client = A6ApiClient(session=FakeSession([Response(409, document)]))

    with pytest.raises(A6AutoError, match="^supplier_apply_pending_request_exists$"):
        client.apply_supplier(9517, {"supplier": {}, "channel": {}})


def test_ensure_one_key_reconciles_by_random_name() -> None:
    session = FakeSession(
        [
            Response(200, {"success": True, "data": {"items": []}}),
            Response(200, {"success": True}),
            Response(
                200,
                {"success": True, "data": {"items": [{"id": 77, "name": "merchant-random"}]}},
            ),
            Response(200, {"success": True, "data": {"key": "abc123"}}),
        ]
    )
    client = A6ApiClient(session=session)

    key = client.ensure_one_key(9517, "merchant-random")

    assert key == "sk-abc123"
    assert session.calls[1]["json"]["name"] == "merchant-random"
    assert session.calls[1]["json"]["unlimited_quota"] is True
    assert session.calls[3]["url"].endswith("/api/token/77/key")


def test_rename_token_preserves_metadata_and_confirms_same_id_and_name() -> None:
    current = {
        "id": 77,
        "name": "merchant-random",
        "remain_quota": 0,
        "expired_time": -1,
        "unlimited_quota": True,
        "model_limits_enabled": True,
        "model_limits": "gpt-5.6-sol",
        "allow_ips": "127.0.0.1",
        "group": "default",
        "cross_group_retry": True,
        "routing_enabled": True,
        "routing_preference": "recent",
        "pricing_strategy": "custom",
        "pricing_custom_pct": 35,
        "routing_allow_fallback": False,
        "key": "sk-masked-secret-must-not-be-written",
    }
    renamed = {**current, "name": "z00000000000000000000000"}
    session = FakeSession(
        [
            Response(200, {"success": True, "data": current}),
            Response(200, {"success": True}),
            Response(200, {"success": True, "data": renamed}),
        ]
    )
    client = A6ApiClient(session=session)

    observed = client.rename_token(9517, 77, renamed["name"])

    assert observed == renamed
    assert session.calls[0]["method"] == "GET"
    assert session.calls[0]["url"].endswith("/api/token/77")
    update = session.calls[1]
    assert update["method"] == "PUT"
    assert update["url"].endswith("/api/token/")
    assert update["json"]["id"] == 77
    assert update["json"]["name"] == renamed["name"]
    assert update["json"]["model_limits"] == "gpt-5.6-sol"
    assert update["json"]["routing_preference"] == "recent"
    assert update["json"]["pricing_custom_pct"] == 35
    assert "key" not in update["json"]


def test_rename_token_rejects_wrong_id_readback_without_update() -> None:
    session = FakeSession(
        [Response(200, {"success": True, "data": {"id": 78, "name": "merchant-random"}})]
    )
    client = A6ApiClient(session=session)

    with pytest.raises(A6AutoError, match="token_readback_id_mismatch"):
        client.rename_token(9517, 77, "z00000000000000000000000")

    assert len(session.calls) == 1


def test_send_verification_marks_occupied_email_as_terminal() -> None:
    session = FakeSession([Response(200, {"success": False, "message": "邮箱地址已被占用"})])
    client = A6ApiClient(session=session)

    with pytest.raises(A6AutoError, match="email_already_registered"):
        client.send_verification("fixture@qq.com")


def test_balance_transfer_preview_uses_default_quota_per_unit() -> None:
    session = FakeSession([Response(200, {"success": True, "data": {"amount": 500_000}})])
    client = A6ApiClient(session=session)

    preview = client.balance_transfer_preview(9517, 500_000)

    assert preview == {"amount": 500_000}
    assert session.calls[0]["params"] == {"amount": 500_000}


def test_balance_transfer_preview_accepts_custom_quota_per_unit() -> None:
    session = FakeSession([Response(200, {"success": True, "data": {"amount": 250_000}})])
    client = A6ApiClient(session=session)

    preview = client.balance_transfer_preview(9517, 250_000, quota_per_unit=250_000)

    assert preview == {"amount": 250_000}
    assert session.calls[0]["params"] == {"amount": 250_000}


def test_balance_transfer_posts_quota_amount_and_accepts_empty_data() -> None:
    session = FakeSession([Response(200, {"success": True, "data": None})])
    client = A6ApiClient(session=session)

    result = client.balance_transfer(9517, 500_000)

    assert result == {}
    assert session.calls[0]["url"].endswith("/api/supplier/balance/transfer")
    assert session.calls[0]["json"] == {"amount": 500_000}


@pytest.mark.parametrize(
    ("amount_micros", "quota_per_unit"),
    [(499_999, 500_000), (249_999, 250_000)],
)
def test_balance_transfer_preview_rejects_amount_below_one_usd(
    amount_micros: int,
    quota_per_unit: int,
) -> None:
    session = FakeSession([])
    client = A6ApiClient(session=session)

    with pytest.raises(A6AutoError, match="balance_transfer_amount_invalid"):
        client.balance_transfer_preview(
            9517,
            amount_micros,
            quota_per_unit=quota_per_unit,
        )

    assert session.calls == []


@pytest.mark.parametrize("quota_per_unit", [True, False, 0, -1, 500_000.0, "500000", None])
def test_balance_transfer_preview_rejects_invalid_quota_per_unit(
    quota_per_unit: object,
) -> None:
    session = FakeSession([])
    client = A6ApiClient(session=session)

    with pytest.raises(A6AutoError, match="quota_per_unit_invalid"):
        client.balance_transfer_preview(9517, 500_000, quota_per_unit=quota_per_unit)  # type: ignore[arg-type]

    assert session.calls == []


def test_withdraw_account_list_and_create_follow_nested_json_protocol() -> None:
    existing_info = {
        "channel": "usdt",
        "remark": "existing",
        "display": "ERC20: masked",
        "fields": {"network": "ERC20", "address": "0xexistingaddress1234"},
    }
    session = FakeSession(
        [
            Response(
                200,
                {
                    "success": True,
                    "data": [
                        {
                            "id": 132,
                            "channel": "usdt",
                            "account_info": json.dumps(existing_info),
                        }
                    ],
                },
            ),
            Response(200, {"success": True, "data": None}),
        ]
    )
    client = A6ApiClient(session=session)

    accounts = client.supplier_withdraw_accounts(9517)
    created = client.create_supplier_withdraw_account(
        9517,
        network="ERC20",
        address="0xnewaddress1234567890",
        remark="a6trader-m5-fixture",
    )

    assert accounts[0]["id"] == 132
    assert created == {}
    payload = session.calls[1]["json"]
    assert payload["channel"] == "usdt"
    assert payload["is_default"] is True
    inner = json.loads(payload["account_info"])
    assert inner["fields"] == {
        "network": "ERC20",
        "address": "0xnewaddress1234567890",
    }
    assert inner["remark"] == "a6trader-m5-fixture"


def test_withdraw_preview_submit_and_status_preserve_integer_contract() -> None:
    session = FakeSession(
        [
            Response(
                200,
                {
                    "success": True,
                    "data": {"fee_micros": 3_000_000, "net_micros": 52_000_000},
                },
            ),
            Response(
                200,
                {
                    "success": True,
                    "data": {"request_id": "request-fixture", "id": 701},
                },
            ),
            Response(
                200,
                {
                    "success": True,
                    "data": {"items": [{"id": 701, "status": "processing"}]},
                },
            ),
        ]
    )
    client = A6ApiClient(session=session)

    preview = client.supplier_withdraw_preview(
        9517,
        account_id=132,
        amount_micros=55_000_000,
    )
    submitted = client.supplier_withdraw_submit(
        9517,
        account_id=132,
        amount_micros=55_000_000,
    )
    status = client.supplier_withdraw_status(9517, 701)

    assert preview["fee_micros"] == 3_000_000
    assert submitted == {"request_id": "request-fixture", "id": 701}
    assert status == {"id": 701, "status": "processing"}
    assert session.calls[0]["params"] == {"account_id": 132, "amount": 55_000_000}
    assert session.calls[1]["json"] == {"account_id": 132, "amount": 55_000_000}
    assert session.calls[2]["params"] == {"page": 1, "size": 1_000}


@pytest.mark.parametrize("amount", [0, 49_999_999, True])
def test_withdraw_preview_and_submit_reject_platform_subminimum(amount: object) -> None:
    session = FakeSession([])
    client = A6ApiClient(session=session)

    with pytest.raises(A6AutoError, match="supplier_withdraw_preview_request_invalid"):
        client.supplier_withdraw_preview(9517, account_id=132, amount_micros=amount)  # type: ignore[arg-type]
    with pytest.raises(A6AutoError, match="supplier_withdraw_submit_request_invalid"):
        client.supplier_withdraw_submit(9517, account_id=132, amount_micros=amount)  # type: ignore[arg-type]

    assert session.calls == []


def test_supplier_channels_reads_current_channels_and_discards_non_objects() -> None:
    session = FakeSession(
        [
            Response(
                200,
                {
                    "success": True,
                    "data": [
                        {
                            "id": 1087,
                            "supplier_id": 298,
                            "name": "fixture-channel",
                            "nickname": "fixture",
                            "channel_type": 1,
                            "status": 1,
                        },
                        "invalid-channel-entry",
                    ],
                },
            )
        ]
    )
    client = A6ApiClient(session=session)

    channels = client.supplier_channels(9517)

    assert channels == [
        {
            "id": 1087,
            "supplier_id": 298,
            "name": "fixture-channel",
            "nickname": "fixture",
            "channel_type": 1,
            "status": 1,
        }
    ]
    assert session.calls[0]["method"] == "GET"
    assert session.calls[0]["url"] == "https://a6api.com/api/supplier/me/channels"
    assert session.calls[0]["headers"]["New-API-User"] == "9517"
    assert session.calls[0]["headers"]["Referer"] == "https://a6api.com/supplier/channels"
    assert "params" not in session.calls[0]


def test_supplier_channels_normalizes_current_nested_contract_without_secrets() -> None:
    session = FakeSession(
        [
            Response(
                200,
                {
                    "success": True,
                    "data": [
                        {
                            "binding": {
                                "id": 1111,
                                "supplier_id": 308,
                                "channel_id": 1120,
                                "disabled": False,
                            },
                            "channel": {
                                "id": 1120,
                                "name": "fixture-channel",
                                "type": 1,
                                "status": 1,
                                "models": "gpt-5.6-sol",
                                "base_url": "https://secret.invalid",
                                "api_key_last4": "abcd",
                            },
                            "request": {
                                "supplier_id": 308,
                                "nickname": "fixture",
                                "description": "fixture description",
                                "channel_type": 1,
                                "status": 1,
                                "api_key_last4": "abcd",
                            },
                        }
                    ],
                },
            )
        ]
    )

    assert A6ApiClient(session=session).supplier_channels(9645) == [
        {
            "id": 1120,
            "binding_id": 1111,
            "supplier_id": 308,
            "name": "fixture-channel",
            "nickname": "fixture",
            "channel_type": 1,
            "status": 1,
            "availability": 1,
            "models": "gpt-5.6-sol",
            "description": "fixture description",
            "base_url": "https://secret.invalid",
            "api_key_last4": "abcd",
            "has_api_key": True,
        }
    ]


@pytest.mark.parametrize(
    "response",
    [
        Response(503, {"success": False, "message": "temporary"}),
        Response(200, {"success": False, "data": []}),
        Response(200, {"success": True, "data": {"items": []}}),
    ],
)
def test_supplier_channels_rejects_unsuccessful_or_non_list_envelopes(response) -> None:
    client = A6ApiClient(session=FakeSession([response]))

    with pytest.raises(A6AutoError, match="supplier_channels_failed"):
        client.supplier_channels(9517)


def test_supplier_listings_sends_status_filter_and_normalizes_page() -> None:
    session = FakeSession(
        [
            Response(
                200,
                {
                    "success": True,
                    "data": {
                        "items": [
                            {
                                "id": 3759,
                                "channel_id": 1087,
                                "own_model": "gpt-5.6-terra",
                                "platform_model": "gpt-5.6-terra",
                                "charge_type": "per_token",
                                "input_price_micros": 500_000,
                                "output_price_micros": 3_000_000,
                                "cache_read_price_micros": 50_000,
                                "cache_write_price_micros": 625_000,
                                "status": 2,
                            },
                            None,
                        ],
                        "total": 1,
                        "page": 3,
                        "size": 20,
                        "server_only": "not part of the normalized contract",
                    },
                },
            )
        ]
    )
    client = A6ApiClient(session=session)

    page = client.supplier_listings(9517, page=3, size=20, status=2)

    assert page == {
        "items": [
            {
                "id": 3759,
                "channel_id": 1087,
                "own_model": "gpt-5.6-terra",
                "platform_model": "gpt-5.6-terra",
                "charge_type": "per_token",
                "input_price_micros": 500_000,
                "output_price_micros": 3_000_000,
                "cache_read_price_micros": 50_000,
                "cache_write_price_micros": 625_000,
                "status": 2,
            }
        ],
        "total": 1,
        "page": 3,
        "size": 20,
    }
    assert session.calls[0]["method"] == "GET"
    assert session.calls[0]["url"] == "https://a6api.com/api/supplier/listings"
    assert session.calls[0]["params"] == {"page": 3, "size": 20, "status": 2}
    assert session.calls[0]["headers"]["New-API-User"] == "9517"
    assert session.calls[0]["headers"]["Referer"] == "https://a6api.com/supplier/listings"


def test_supplier_listings_normalizes_null_items_only_for_zero_total() -> None:
    empty = A6ApiClient(
        session=FakeSession(
            [Response(200, {"success": True, "data": {"items": None, "total": 0}})]
        )
    )

    assert empty.supplier_listings(9645) == {
        "items": [],
        "total": 0,
        "page": 1,
        "size": 1000,
    }

    incomplete = A6ApiClient(
        session=FakeSession(
            [Response(200, {"success": True, "data": {"items": None, "total": 1}})]
        )
    )
    with pytest.raises(A6AutoError, match="supplier_listings_failed"):
        incomplete.supplier_listings(9645)


def test_supplier_listings_omits_absent_status_and_uses_requested_page_defaults() -> None:
    session = FakeSession(
        [
            Response(
                200,
                {
                    "success": True,
                    "data": {
                        "items": [],
                        "total": None,
                    },
                },
            )
        ]
    )
    client = A6ApiClient(session=session)

    page = client.supplier_listings(9517)

    assert page == {"items": [], "total": None, "page": 1, "size": 1000}
    assert session.calls[0]["params"] == {"page": 1, "size": 1000}


@pytest.mark.parametrize(
    "response",
    [
        Response(500, {"success": False}),
        Response(200, {"success": False, "data": {"items": []}}),
        Response(200, {"success": True, "data": {"items": None}}),
    ],
)
def test_supplier_listings_rejects_unsuccessful_or_malformed_pages(response) -> None:
    client = A6ApiClient(session=FakeSession([response]))

    with pytest.raises(A6AutoError, match="supplier_listings_failed"):
        client.supplier_listings(9517)


def test_supplier_listings_accepts_direct_data_array() -> None:
    client = A6ApiClient(
        session=FakeSession(
            [Response(200, {"success": True, "data": [{"id": 3759}, "ignored"]})]
        )
    )

    assert client.supplier_listings(9517, page=2, size=10) == {
        "items": [{"id": 3759}],
        "total": 2,
        "page": 2,
        "size": 10,
    }


@pytest.mark.parametrize("status", [429, 500, 503])
def test_read_endpoint_failure_exposes_transient_http_status(status: int) -> None:
    client = A6ApiClient(session=FakeSession([Response(status, {"success": False})]))

    with pytest.raises(A6HttpError, match="^supplier_earnings_failed$") as captured:
        client.supplier_earnings(9517)

    assert captured.value.status_code == status
    assert captured.value.transient is True


def test_read_endpoint_explicit_4xx_preserves_nontransient_error() -> None:
    client = A6ApiClient(session=FakeSession([Response(403, {"success": False})]))

    with pytest.raises(A6AutoError, match="^supplier_listings_failed$") as captured:
        client.supplier_listings(9517)

    assert not isinstance(captured.value, A6HttpError)


def test_marketplace_channels_can_read_back_user_disabled_channels() -> None:
    session = FakeSession(
        [
            Response(
                200,
                {
                    "success": True,
                    "data": {
                        "items": [
                            {
                                "channel_id": 1283,
                                "supplier_id": 355,
                                "user_channel_disabled": True,
                                "route_status": "user_disabled",
                            }
                        ],
                        "total": 1,
                        "page": 1,
                        "page_size": 20,
                    },
                },
            )
        ]
    )
    client = A6ApiClient(session=session)

    page = client.marketplace_channels(
        2292,
        token_id=20406,
        model="gpt-5.6-sol",
        page=1,
        page_size=20,
        include_user_disabled=True,
    )

    assert page["items"] == [
        {
            "channel_id": 1283,
            "supplier_id": 355,
            "user_channel_disabled": True,
            "route_status": "user_disabled",
        }
    ]
    assert session.calls[0]["method"] == "GET"
    assert session.calls[0]["url"] == (
        "https://a6api.com/api/marketplace/channels/search"
    )
    assert session.calls[0]["params"] == {
        "token_id": 20406,
        "model": "gpt-5.6-sol",
        "route_status": "all",
        "exclude_unavailable": "true",
        "include_seed": "true",
        "include_disabled": "false",
        "include_user_disabled": "true",
        "sort": "input_asc",
        "view": "list",
        "page": 1,
        "page_size": 20,
    }


@pytest.mark.parametrize(
    ("token_id", "model", "page", "page_size", "sort"),
    [
        (0, "gpt-5.6-sol", 1, 20, "input_asc"),
        (1, "", 1, 20, "input_asc"),
        (1, "gpt-5.6-sol", 0, 20, "input_asc"),
        (1, "gpt-5.6-sol", 1, 0, "input_asc"),
        (1, "gpt-5.6-sol", 1, 20, ""),
    ],
)
def test_marketplace_channels_rejects_invalid_page_request_without_network(
    token_id: int,
    model: str,
    page: int,
    page_size: int,
    sort: str,
) -> None:
    session = FakeSession([])
    client = A6ApiClient(session=session)

    with pytest.raises(A6AutoError, match="marketplace_channels_request_invalid"):
        client.marketplace_channels(
            2292,
            token_id=token_id,
            model=model,
            page=page,
            page_size=page_size,
            sort=sort,
        )

    assert session.calls == []


def test_marketplace_disable_and_subscription_auto_pool_contracts() -> None:
    subscription = {
        "id": 26816,
        "user_id": 2292,
        "token_id": 20406,
        "model_name": "gpt-5.6-sol",
        "auto_pool_enabled": True,
        "min_ratio": "0",
        "max_ratio": "0.0567353452",
    }
    session = FakeSession(
        [
            Response(200, {"success": True}),
            Response(200, {"success": True, "data": [subscription]}),
            Response(200, {"success": True, "data": subscription}),
            Response(200, {"success": True, "data": subscription}),
            Response(200, {"success": True, "data": subscription}),
        ]
    )
    client = A6ApiClient(session=session)

    assert client.marketplace_disable_channel(
        2292,
        channel_id=1283,
        model="gpt-5.6-sol",
    ) == {}
    assert client.marketplace_subscriptions(2292) == [subscription]
    assert client.marketplace_subscription(2292, subscription_id=26816) == subscription
    created = client.marketplace_create_auto_pool(
        2292,
        token_id=20406,
        model="gpt-5.6-sol",
        min_ratio="0",
        max_ratio="0.0567353452",
    )
    updated = client.marketplace_update_auto_pool(
        2292,
        subscription_id=26816,
        min_ratio="0",
        max_ratio="0.0567353452",
    )

    assert created == subscription
    assert updated == subscription
    assert session.calls[0]["method"] == "POST"
    assert session.calls[0]["url"] == (
        "https://a6api.com/api/marketplace/channels/1283/disable"
    )
    assert session.calls[0]["params"] == {"model": "gpt-5.6-sol"}
    assert session.calls[1]["url"] == "https://a6api.com/api/subscriptions"
    assert session.calls[2]["url"] == "https://a6api.com/api/subscriptions/26816"
    assert session.calls[3]["json"]["auto_pool_enabled"] is True
    assert session.calls[3]["json"]["max_ratio"] == "0.0567353452"
    assert session.calls[4]["url"] == (
        "https://a6api.com/api/subscriptions/26816/auto-pool"
    )
    assert session.calls[4]["json"] == {
        "enabled": True,
        "min_ratio": "0",
        "max_ratio": "0.0567353452",
    }


def test_marketplace_pin_and_unpin_use_model_token_contract() -> None:
    session = FakeSession(
        [
            Response(200, {"success": True, "data": {"id": 91}}),
            Response(200, {"success": True, "data": None}),
        ]
    )
    client = A6ApiClient(session=session)

    pinned = client.marketplace_pin(
        9517,
        token_id=14020,
        channel_id=839,
        model="gpt-5.6-sol",
    )
    unpinned = client.marketplace_unpin(9517, token_id=14020, model="gpt-5.6-sol")

    assert pinned == {"id": 91}
    assert unpinned == {}
    assert session.calls[0]["url"] == "https://a6api.com/api/marketplace/pin"
    assert session.calls[0]["json"] == {
        "token_id": 14020,
        "channel_id": 839,
        "model_name": "gpt-5.6-sol",
        "fallback_to_smart_routing": False,
    }
    assert session.calls[1]["url"] == "https://a6api.com/api/marketplace/unpin"
    assert session.calls[1]["json"] == {
        "token_id": 14020,
        "model_name": "gpt-5.6-sol",
    }


def test_marketplace_pin_failure_preserves_only_stable_mutation_diagnostics() -> None:
    client = A6ApiClient(
        session=FakeSession(
            [
                Response(
                    400,
                    {
                        "success": False,
                        "error": {
                            "code": "channel_not_found",
                            "message": "private provider response detail",
                        },
                    },
                )
            ]
        )
    )

    with pytest.raises(A6MutationError, match="^marketplace_pin_failed$") as captured:
        client.marketplace_pin(
            9517,
            token_id=14020,
            channel_id=1075,
            model="gpt-5.6-sol",
        )

    error = captured.value
    assert error.operation == "marketplace_pin"
    assert error.status_code == 400
    assert error.provider_error_code == "channel_not_found"
    assert dict(error.diagnostics) == {
        "operation": "marketplace_pin",
        "http_status": 400,
        "provider_error_code": "channel_not_found",
    }
    assert "private provider response detail" not in str(error)


def test_marketplace_pin_5xx_is_marked_transient_without_losing_provider_code() -> None:
    client = A6ApiClient(
        session=FakeSession(
            [Response(503, {"success": False, "error_code": "service_unavailable"})]
        )
    )

    with pytest.raises(A6MutationError) as captured:
        client.marketplace_pin(
            9517,
            token_id=14020,
            channel_id=1075,
            model="gpt-5.6-sol",
        )

    assert captured.value.transient is True
    assert captured.value.provider_error_code == "service_unavailable"


@pytest.mark.parametrize(
    ("token_id", "channel_id", "model", "fallback"),
    [(0, 1, "gpt-5.6-sol", False), (1, 0, "gpt-5.6-sol", False), (1, 2, "", False),
     (1, 2, "gpt-5.6-sol", 1)],
)
def test_marketplace_pin_rejects_invalid_request_without_network(
    token_id: int,
    channel_id: int,
    model: str,
    fallback: object,
) -> None:
    session = FakeSession([])
    client = A6ApiClient(session=session)

    with pytest.raises(A6AutoError, match="marketplace_pin_request_invalid"):
        client.marketplace_pin(
            9517,
            token_id=token_id,
            channel_id=channel_id,
            model=model,
            fallback_to_smart_routing=fallback,  # type: ignore[arg-type]
        )

    assert session.calls == []


def test_supplier_listing_mutations_use_array_put_and_empty_action_bodies() -> None:
    session = FakeSession(
        [
            Response(200, {"success": True, "data": [{"id": 3759}]}),
            Response(200, {"success": True, "data": {"id": 3759}}),
            Response(200, {"success": True, "data": None}),
            Response(200, {"success": True, "data": None}),
            Response(200, {"success": True, "data": None}),
            Response(200, {"success": True, "data": {"id": 1087}}),
        ]
    )
    client = A6ApiClient(session=session)
    listing = {
        "channel_id": 1087,
        "own_model": "gpt-5.6-sol",
        "platform_model": "gpt-5.6-sol",
        "charge_type": "per_token",
        "input_price_micros": 500_000,
        "output_price_micros": 3_000_000,
        "cache_read_price_micros": 50_000,
        "cache_write_price_micros": 625_000,
    }

    assert client.create_supplier_listings(9517, [listing]) == {
        "items": [{"id": 3759}]
    }
    assert client.update_supplier_listing(9517, 3759, listing) == {"id": 3759}
    assert client.takedown_supplier_listing(9517, 3759) == {}
    assert client.resubmit_supplier_listing(9517, 3759) == {}
    assert client.takedown_supplier_channel(9517, 1087) == {}
    assert client.update_supplier_channel(9517, 1087, {"nickname": "managed"}) == {
        "id": 1087
    }

    assert session.calls[0]["method"] == "POST"
    assert session.calls[0]["url"] == "https://a6api.com/api/supplier/listings"
    assert session.calls[0]["json"] == [listing]
    assert session.calls[1]["method"] == "PUT"
    assert session.calls[1]["url"].endswith("/api/supplier/listings/3759")
    assert session.calls[2]["json"] == {}
    assert session.calls[2]["url"].endswith("/3759/takedown")
    assert session.calls[3]["json"] == {}
    assert session.calls[3]["url"].endswith("/3759/resubmit")
    assert session.calls[4]["json"] == {}
    assert session.calls[4]["url"].endswith("/api/supplier/channels/1087/takedown")
    assert session.calls[5]["method"] == "PATCH"
    assert session.calls[5]["url"].endswith("/api/supplier/me/channels/1087")


@pytest.mark.parametrize(
    ("method", "args", "error"),
    [
        ("create_supplier_listings", (9517, []), "supplier_listing_create_request_invalid"),
        (
            "update_supplier_listing",
            (9517, 0, {"name": "x"}),
            "supplier_listing_update_request_invalid",
        ),
        ("update_supplier_listing", (9517, 1, {}), "supplier_listing_update_request_invalid"),
        ("takedown_supplier_listing", (9517, 0), "supplier_listing_action_request_invalid"),
        ("resubmit_supplier_listing", (9517, -1), "supplier_listing_action_request_invalid"),
        ("takedown_supplier_channel", (9517, 0), "supplier_channel_action_request_invalid"),
        (
            "update_supplier_channel",
            (9517, 0, {"name": "x"}),
            "supplier_channel_update_request_invalid",
        ),
    ],
)
def test_supplier_mutations_reject_invalid_requests_without_network(
    method: str,
    args: tuple,
    error: str,
) -> None:
    session = FakeSession([])
    client = A6ApiClient(session=session)

    with pytest.raises(A6AutoError, match=error):
        getattr(client, method)(*args)

    assert session.calls == []
