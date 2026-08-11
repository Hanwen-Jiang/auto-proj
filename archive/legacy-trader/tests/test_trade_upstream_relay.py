from __future__ import annotations

from pathlib import Path

import pytest

from a6api_auto.trade_upstream_relay import (
    A6_UPSTREAM_ORIGIN,
    A6UpstreamRelay,
    A6UpstreamRelayError,
    DirectHTTPSession,
    DirectRelayResolver,
    _forward_request_headers,
    _parser,
    _public_error_response,
)


class FakeResponse:
    status_code = 200

    def iter_content(self, chunk_size=16_384):
        del chunk_size
        yield b"{}"

    def close(self) -> None:
        return


class FakeSession:
    def __init__(self) -> None:
        self.calls = []

    def request(self, **options):
        self.calls.append(options)
        return FakeResponse()


def test_direct_resolver_builds_fixed_a6_target() -> None:
    resolver = DirectRelayResolver()

    target = resolver.resolve("/a6-user/7775/v1/responses?trace=1")

    assert target.a6_user_id == 7775
    assert target.upstream_url == "https://a6.a6api.com/v1/responses?trace=1"


@pytest.mark.parametrize(
    "origin",
    [
        "https://api.a6api.com",
        "https://45.61.235.131",
        "http://a6.a6api.com",
        "https://a6.a6api.com/v1",
        "https://user:password@a6.a6api.com",
        "https://[malformed",
    ],
)
def test_direct_resolver_rejects_noncanonical_upstream_origin(origin) -> None:
    with pytest.raises(A6UpstreamRelayError, match="relay_upstream_origin_invalid"):
        DirectRelayResolver(upstream_origin=origin)


def test_direct_resolver_normalizes_only_canonical_trailing_slash() -> None:
    resolver = DirectRelayResolver(upstream_origin=f"{A6_UPSTREAM_ORIGIN}/")

    assert resolver.upstream_origin == A6_UPSTREAM_ORIGIN


@pytest.mark.parametrize(
    "path",
    [
        "/v1/responses",
        "/a6-user/0/v1/responses",
        "/a6-user/7775/admin/users",
        "/a6-user/not-a-number/v1/responses",
    ],
)
def test_direct_resolver_rejects_unknown_route(path) -> None:
    with pytest.raises(A6UpstreamRelayError, match="relay_route_not_found"):
        DirectRelayResolver().resolve(path)


def test_relay_cli_has_no_proxy_map_option() -> None:
    parser = _parser()

    assert "--proxy-map-file" not in parser.format_help()
    with pytest.raises(SystemExit):
        parser.parse_args(["--proxy-map-file", "/tmp/proxy-map.json"])


def test_forward_headers_replaces_identity_and_strips_hop_by_hop() -> None:
    headers = _forward_request_headers(
        {
            "Authorization": "Bearer secret",
            "Content-Type": "application/json",
            "Content-Length": "10",
            "Host": "127.0.0.1:8767",
            "User-Agent": "Go-http-client/1.1",
            "Connection": "keep-alive, X-Internal-Hop",
            "X-Internal-Hop": "private-hop-value",
            "X-Forwarded-For": "198.51.100.1",
        }
    )

    assert headers["Authorization"] == "Bearer secret"
    assert headers["Content-Type"] == "application/json"
    assert headers["User-Agent"] == "a6api-auto-provider-relay/1"
    assert headers["Accept-Encoding"] == "identity"
    assert "Connection" not in headers
    assert "Content-Length" not in headers
    assert "Host" not in headers
    assert "X-Internal-Hop" not in headers
    assert "X-Forwarded-For" not in headers


def test_relay_uses_direct_transport_without_a_provider_proxy() -> None:
    session = FakeSession()
    relay = A6UpstreamRelay(DirectRelayResolver(), session=session)

    target, response = relay.open(
        "POST",
        "/a6-user/9812/v1/responses",
        {"Authorization": "Bearer key", "Content-Type": "application/json"},
        b'{"model":"gpt-5.6-sol"}',
    )

    assert target.a6_user_id == 9812
    assert response.status_code == 200
    assert session.calls == [
        {
            "method": "POST",
            "url": "https://a6.a6api.com/v1/responses",
            "headers": {
                "Authorization": "Bearer key",
                "Content-Type": "application/json",
                "User-Agent": session.calls[0]["headers"]["User-Agent"],
                "Accept-Encoding": "identity",
            },
            "data": b'{"model":"gpt-5.6-sol"}',
            "timeout": 30.0,
            "stream_read_timeout": 120.0,
            "allow_redirects": False,
            "stream": True,
        }
    ]


def test_relay_classifies_response_header_timeout() -> None:
    session = ReadinessSession(error=TimeoutError())
    relay = A6UpstreamRelay(DirectRelayResolver(), session=session)

    with pytest.raises(A6UpstreamRelayError, match="relay_upstream_timeout"):
        relay.open(
            "POST",
            "/a6-user/9812/v1/responses",
            {"Authorization": "Bearer key"},
            b"{}",
        )


@pytest.mark.parametrize(
    ("internal_code", "expected"),
    [
        (
            "relay_upstream_timeout",
            (504, "upstream_timeout", "Upstream request timed out"),
        ),
        (
            "relay_upstream_transport_failed",
            (502, "upstream_error", "Upstream request failed"),
        ),
    ],
)
def test_public_relay_errors_hide_internal_provider_details(internal_code, expected) -> None:
    response = _public_error_response(internal_code)

    assert response == expected
    assert "a6" not in " ".join(map(str, response)).casefold()
    assert "relay" not in " ".join(map(str, response)).casefold()


class ReadinessResponse:
    def __init__(self, status_code: int, chunks: list[bytes] | None = None) -> None:
        self.status_code = status_code
        self.chunks = chunks or [b'{"error":"unauthorized"}']
        self.closed = False

    def iter_content(self, chunk_size=16_384):
        assert chunk_size == 4096
        yield from self.chunks

    def close(self) -> None:
        self.closed = True


class ReadinessSession:
    def __init__(
        self,
        response: ReadinessResponse | None = None,
        error: Exception | None = None,
    ) -> None:
        self.response = response or ReadinessResponse(401)
        self.error = error
        self.calls = []

    def request(self, **options):
        self.calls.append(options)
        if self.error is not None:
            raise self.error
        return self.response


def test_relay_readiness_probes_fixed_direct_upstream_without_credentials() -> None:
    response = ReadinessResponse(401)
    session = ReadinessSession(response)
    relay = A6UpstreamRelay(
        DirectRelayResolver(),
        session=session,
        readiness_timeout_seconds=2.5,
    )

    relay.probe_upstream()

    assert response.closed is True
    assert session.calls == [
        {
            "method": "GET",
            "url": "https://a6.a6api.com/v1/models",
            "headers": {
                "User-Agent": "a6api-auto-provider-relay/1",
                "Accept-Encoding": "identity",
            },
            "data": b"",
            "timeout": 2.5,
            "allow_redirects": False,
            "stream": True,
        }
    ]


@pytest.mark.parametrize(
    ("session", "expected"),
    [
        (ReadinessSession(ReadinessResponse(200)), "relay_upstream_readiness_status_invalid"),
        (ReadinessSession(error=TimeoutError()), "relay_upstream_readiness_failed"),
        (
            ReadinessSession(ReadinessResponse(401, [b"x" * (64 * 1024 + 1)])),
            "relay_upstream_readiness_body_too_large",
        ),
    ],
)
def test_relay_readiness_fails_closed(session, expected) -> None:
    relay = A6UpstreamRelay(DirectRelayResolver(), session=session)

    with pytest.raises(A6UpstreamRelayError, match=expected):
        relay.probe_upstream()

    assert session.response.closed is (session.error is None)


class FakeHTTPResponse:
    status = 401
    headers = {"Content-Type": "application/json"}
    will_close = False

    def __init__(self) -> None:
        self.chunks = [b'{"error":', b'"unauthorized"}', b""]
        self.closed = False

    def read(self, _size: int) -> bytes:
        return self.chunks.pop(0)

    def close(self) -> None:
        self.closed = True


class FakeHTTPSConnection:
    def __init__(self, host, *, port, timeout, context) -> None:
        self.init = (host, port, timeout, context)
        self.timeout = timeout
        self.calls = []
        self.responses = []
        self.sock = FakeSocket()
        self.closed = False

    def request(self, method, path, *, body, headers) -> None:
        self.calls.append((method, path, body, headers))

    def getresponse(self):
        response = FakeHTTPResponse()
        self.responses.append(response)
        return response

    def close(self) -> None:
        self.closed = True
        self.sock = None


class FakeSocket:
    def __init__(self) -> None:
        self.timeouts = []

    def settimeout(self, timeout: float) -> None:
        self.timeouts.append(timeout)


def test_direct_http_session_uses_canonical_host_and_owns_connection() -> None:
    connections = []

    def factory(*args, **kwargs):
        connection = FakeHTTPSConnection(*args, **kwargs)
        connections.append(connection)
        return connection

    session = DirectHTTPSession(connection_factory=factory)
    response = session.request(
        method="GET",
        url="https://a6.a6api.com/v1/models?probe=1",
        headers={"Authorization": "Bearer fixture"},
        data=b"",
        timeout=12.5,
        stream_read_timeout=90,
        allow_redirects=False,
        stream=True,
    )

    assert response.status_code == 401
    assert list(response.iter_content(1024)) == [b'{"error":', b'"unauthorized"}']
    connection = connections[0]
    assert connection.init[:3] == ("a6.a6api.com", 443, 12.5)
    assert connection.calls == [
        (
            "GET",
            "/v1/models?probe=1",
            b"",
            {"Authorization": "Bearer fixture"},
        )
    ]
    assert connection.sock.timeouts == [90.0]
    response.close()
    response.close()
    assert connection.responses[0].closed is True
    assert connection.closed is False
    session.close()
    assert connection.closed is True


def test_direct_http_session_reuses_a_fully_consumed_keepalive_connection() -> None:
    connections = []

    def factory(*args, **kwargs):
        connection = FakeHTTPSConnection(*args, **kwargs)
        connections.append(connection)
        return connection

    session = DirectHTTPSession(connection_factory=factory, max_connections=1)
    options = {
        "method": "GET",
        "url": "https://a6.a6api.com/v1/models",
        "headers": {"Authorization": "Bearer fixture"},
        "data": b"",
        "timeout": 12.5,
        "stream_read_timeout": 90,
        "allow_redirects": False,
        "stream": True,
    }

    first = session.request(**options)
    assert list(first.iter_content())
    first.close()
    second = session.request(**options)
    assert list(second.iter_content())
    second.close()

    assert len(connections) == 1
    assert len(connections[0].calls) == 2
    assert connections[0].sock.timeouts == [90.0, 12.5, 90.0]
    session.close()


def test_direct_http_session_discards_an_incompletely_consumed_connection() -> None:
    connections = []

    def factory(*args, **kwargs):
        connection = FakeHTTPSConnection(*args, **kwargs)
        connections.append(connection)
        return connection

    session = DirectHTTPSession(connection_factory=factory, max_connections=1)
    options = {
        "method": "GET",
        "url": "https://a6.a6api.com/v1/models",
        "headers": {},
        "data": b"",
        "timeout": 12.5,
        "allow_redirects": False,
        "stream": True,
    }

    first = session.request(**options)
    first.close()
    second = session.request(**options)

    assert len(connections) == 2
    assert connections[0].closed is True
    second.close()
    session.close()


def test_direct_http_session_bounds_concurrent_streams() -> None:
    session = DirectHTTPSession(
        connection_factory=FakeHTTPSConnection,
        max_connections=1,
        acquire_timeout_seconds=0.01,
    )
    options = {
        "method": "GET",
        "url": "https://a6.a6api.com/v1/models",
        "headers": {},
        "data": b"",
        "timeout": 12.5,
        "allow_redirects": False,
        "stream": True,
    }

    first = session.request(**options)
    with pytest.raises(A6UpstreamRelayError, match="relay_connection_pool_exhausted"):
        session.request(**options)
    first.close()
    session.close()


@pytest.mark.parametrize(
    ("options", "error"),
    [
        ({"max_connections": 0}, "relay_pool_size_invalid"),
        ({"max_connections": True}, "relay_pool_size_invalid"),
        ({"idle_timeout_seconds": 0}, "relay_pool_idle_timeout_invalid"),
        ({"idle_timeout_seconds": "fast"}, "relay_pool_idle_timeout_invalid"),
        ({"acquire_timeout_seconds": float("inf")}, "relay_pool_acquire_timeout_invalid"),
    ],
)
def test_direct_http_session_rejects_invalid_pool_limits(options, error) -> None:
    with pytest.raises(A6UpstreamRelayError, match=error):
        DirectHTTPSession(**options)


@pytest.mark.parametrize("timeout", [0, True, float("inf"), "slow"])
def test_direct_http_session_rejects_invalid_stream_read_timeout(timeout) -> None:
    session = DirectHTTPSession(connection_factory=FakeHTTPSConnection)

    with pytest.raises(A6UpstreamRelayError, match="relay_direct_transport_timeout_invalid"):
        session.request(
            method="GET",
            url="https://a6.a6api.com/v1/models",
            headers={},
            data=b"",
            timeout=12.5,
            stream_read_timeout=timeout,
            allow_redirects=False,
            stream=True,
        )

    session.close()


@pytest.mark.parametrize(
    ("options", "error"),
    [
        (
            {"response_header_timeout_seconds": 0},
            "relay_response_header_timeout_invalid",
        ),
        (
            {"stream_read_timeout_seconds": float("nan")},
            "relay_stream_read_timeout_invalid",
        ),
    ],
)
def test_relay_rejects_invalid_phase_timeouts(options, error) -> None:
    with pytest.raises(A6UpstreamRelayError, match=error):
        A6UpstreamRelay(DirectRelayResolver(), **options)


def test_relay_cli_exposes_phase_timeout_defaults() -> None:
    args = _parser().parse_args([])

    assert args.response_header_timeout_seconds == 30.0
    assert args.stream_read_timeout_seconds == 120.0


def test_relay_default_session_is_direct_standard_library_transport() -> None:
    relay = A6UpstreamRelay(DirectRelayResolver())

    assert isinstance(relay.session, DirectHTTPSession)


def test_relay_unit_isolated_from_trader_proxy_and_release() -> None:
    unit = Path("deploy/systemd/a6-trader-upstream-relay.service").read_text()

    assert "sub2api-singbox-proxy-pool.service" not in unit
    assert "--proxy-map-file" not in unit
    assert "WorkingDirectory=/opt/a6-provider-relay/current" in unit
    assert "ExecStart=/opt/a6-provider-relay/current/venv/bin/python" in unit
    assert "Environment=A6_PROVIDER_RELAY_RESPONSE_HEADER_TIMEOUT_SECONDS=30" in unit
    assert "Environment=A6_PROVIDER_RELAY_STREAM_READ_TIMEOUT_SECONDS=120" in unit
    assert (
        "--response-header-timeout-seconds ${A6_PROVIDER_RELAY_RESPONSE_HEADER_TIMEOUT_SECONDS}"
        in unit
    )
    assert "--stream-read-timeout-seconds ${A6_PROVIDER_RELAY_STREAM_READ_TIMEOUT_SECONDS}" in unit


def test_hk_shadow_state_tunnel_exposes_only_loopback_forwarders() -> None:
    unit = Path("deploy/systemd/sub2api-hk-state-tunnel.service").read_text()

    assert "EnvironmentFile=/etc/sub2api-hk-tunnel/tunnel.env" in unit
    assert "sub2api-tunnel@${US_TUNNEL_HOST}" in unit
    assert "-L 127.0.0.1:15432:127.0.0.1:5432" in unit
    assert "-L 127.0.0.1:16379:127.0.0.1:6379" in unit
    assert "-L 127.0.0.1:18765:127.0.0.1:8765" in unit
    assert "-L 127.0.0.1:18767:127.0.0.1:8767" in unit
    assert "ExitOnForwardFailure=yes" in unit
    assert "StrictHostKeyChecking=yes" in unit
    assert "0.0.0.0" not in unit


def test_hk_shadow_request_plane_is_loopback_configured_and_tunnel_bound() -> None:
    unit = Path("deploy/systemd/sub2api-hk-shadow.service").read_text()

    assert "Requires=sub2api-hk-state-tunnel.service" in unit
    assert "After=network-online.target sub2api-hk-state-tunnel.service" in unit
    assert "EnvironmentFile=/etc/sub2api-hk/sub2api-hk-shadow.env" in unit
    assert "GATEWAY_A6_PROVIDER_REPLAY_PERSISTENCE_TIMEOUT_SECONDS=10" in unit
    assert "GATEWAY_A6_PROVIDER_REPLAY_DISPATCH_LEASE_ENABLED=false" in unit
    assert "GATEWAY_A6_PROVIDER_REPLAY_FAST_BLOCK_ENABLED=false" in unit
    assert "ExecStart=/opt/sub2api-hk/current/sub2api" in unit
    assert "TimeoutStopSec=20s" in unit
    assert "User=sub2api" in unit


def test_hk_synthetic_replay_profile_exercises_full_durable_contract() -> None:
    profile = Path("deploy/systemd/sub2api-hk-shadow-synthetic-replay.conf").read_text()

    assert "GATEWAY_A6_PROVIDER_REPLAY_ENABLED=true" in profile
    assert "GATEWAY_A6_PROVIDER_REPLAY_DURABLE_COORDINATION_ENABLED=true" in profile
    assert "GATEWAY_A6_PROVIDER_REPLAY_DURABLE_ATTRIBUTION_ENABLED=true" in profile
    assert "GATEWAY_A6_PROVIDER_REPLAY_CANDIDATE_SNAPSHOT_ENABLED=true" in profile
    assert "GATEWAY_A6_PROVIDER_REPLAY_USAGE_ATTRIBUTION_ENABLED=true" in profile
    assert "GATEWAY_A6_PROVIDER_REPLAY_DISPATCH_LEASE_ENABLED=false" in profile
    assert "GATEWAY_A6_PROVIDER_REPLAY_FAST_BLOCK_ENABLED=false" in profile


def test_hk_provider_relay_is_direct_loopback_and_separate_release() -> None:
    unit = Path("deploy/systemd/a6-provider-relay-hk.service").read_text()

    assert "User=a6relay" in unit
    assert "WorkingDirectory=/opt/a6-provider-relay-hk/current" in unit
    assert "-m a6api_auto.trade_upstream_relay" in unit
    assert "--host 127.0.0.1 --port 8767" in unit
    assert "Environment=A6_PROVIDER_RELAY_RESPONSE_HEADER_TIMEOUT_SECONDS=30" in unit
    assert "Environment=A6_PROVIDER_RELAY_STREAM_READ_TIMEOUT_SECONDS=120" in unit
    assert (
        "--response-header-timeout-seconds ${A6_PROVIDER_RELAY_RESPONSE_HEADER_TIMEOUT_SECONDS}"
        in unit
    )
    assert "--stream-read-timeout-seconds ${A6_PROVIDER_RELAY_STREAM_READ_TIMEOUT_SECONDS}" in unit
    assert "--readiness-timeout-seconds 3" in unit
    assert "--pool-max-connections 32" in unit
    assert "--pool-idle-timeout-seconds 30" in unit
    assert "--pool-acquire-timeout-seconds 5" in unit
    assert "proxy" not in unit.casefold()
    assert "sub2api-hk-state-tunnel.service" not in unit
