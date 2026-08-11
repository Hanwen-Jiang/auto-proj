from __future__ import annotations

import argparse
import http.client
import json
import logging
import math
import queue
import re
import ssl
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlsplit

from .errors import A6AutoError

logger = logging.getLogger(__name__)

# The relay is an internal Provider boundary.  Keep its destination stable so a
# deployment cannot accidentally reintroduce the historical Cloudflare path.
A6_UPSTREAM_ORIGIN = "https://a6.a6api.com"

_ROUTE_PATH = re.compile(r"^/a6-user/([1-9][0-9]*)(/v1(?:/.*)?)$")
_REQUEST_HEADER_BLOCKLIST = frozenset(
    {
        "accept-encoding",
        "connection",
        "content-length",
        "host",
        "keep-alive",
        "proxy-authorization",
        "proxy-connection",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
        "x-forwarded-for",
        "x-forwarded-host",
        "x-forwarded-proto",
    }
)
_RESPONSE_HEADER_ALLOWLIST = frozenset(
    {
        "cache-control",
        "content-type",
        "openai-model",
        "openai-processing-ms",
        "retry-after",
        "x-request-id",
        "x-ratelimit-limit-requests",
        "x-ratelimit-limit-tokens",
        "x-ratelimit-remaining-requests",
        "x-ratelimit-remaining-tokens",
        "x-ratelimit-reset-requests",
        "x-ratelimit-reset-tokens",
    }
)
_RELAY_USER_AGENT = "a6api-auto-provider-relay/1"
_DEFAULT_POOL_MAX_CONNECTIONS = 32
_DEFAULT_POOL_IDLE_TIMEOUT_SECONDS = 30.0
_DEFAULT_POOL_ACQUIRE_TIMEOUT_SECONDS = 5.0
_DEFAULT_READINESS_TIMEOUT_SECONDS = 3.0
_DEFAULT_RESPONSE_HEADER_TIMEOUT_SECONDS = 30.0
_DEFAULT_STREAM_READ_TIMEOUT_SECONDS = 120.0
_MAX_READINESS_RESPONSE_BYTES = 64 * 1024


class A6UpstreamRelayError(A6AutoError):
    """Stable errors for the loopback A6 upstream relay."""


def _public_error_response(code: str) -> tuple[int, str, str]:
    if code == "relay_route_not_found":
        return 404, "not_found", "Resource not found"
    if code == "relay_upstream_timeout":
        return 504, "upstream_timeout", "Upstream request timed out"
    return 502, "upstream_error", "Upstream request failed"


@dataclass(frozen=True)
class RelayTarget:
    a6_user_id: int
    upstream_url: str


def _fixed_upstream_origin(value: object) -> str:
    """Validate the compatibility argument and always return the fixed origin."""

    candidate = str(value or "").strip()
    try:
        parsed = urlsplit(candidate.rstrip("/"))
    except ValueError:
        raise A6UpstreamRelayError("relay_upstream_origin_invalid") from None
    try:
        port = parsed.port
    except ValueError:
        port = -1
    if (
        parsed.scheme.casefold() != "https"
        or parsed.hostname is None
        or parsed.hostname.casefold() != "a6.a6api.com"
        or port is not None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise A6UpstreamRelayError("relay_upstream_origin_invalid")
    return A6_UPSTREAM_ORIGIN


class DirectRelayResolver:
    """Resolve account-attributed relay routes without selecting an egress proxy."""

    def __init__(self, *, upstream_origin: str = A6_UPSTREAM_ORIGIN) -> None:
        self.upstream_origin = _fixed_upstream_origin(upstream_origin)

    def resolve(self, raw_path: str) -> RelayTarget:
        parsed = urlsplit(str(raw_path or ""))
        match = _ROUTE_PATH.fullmatch(parsed.path)
        if match is None:
            raise A6UpstreamRelayError("relay_route_not_found")
        a6_user_id = int(match.group(1))
        upstream_url = f"{self.upstream_origin}{match.group(2)}"
        if parsed.query:
            upstream_url = f"{upstream_url}?{parsed.query}"
        return RelayTarget(a6_user_id, upstream_url)


# Compatibility for imports made while the relay still used the Trader proxy map. The
# constructor intentionally accepts no path so old call sites fail closed instead of proxying.
ProxyMapResolver = DirectRelayResolver


def _forward_request_headers(headers: Mapping[str, str]) -> dict[str, str]:
    connection_tokens = {
        token.strip().casefold()
        for name, value in headers.items()
        if str(name).strip().casefold() == "connection"
        for token in str(value).split(",")
        if token.strip()
    }
    forwarded = {
        str(name): str(value)
        for name, value in headers.items()
        if str(name).strip().casefold() not in _REQUEST_HEADER_BLOCKLIST | connection_tokens
    }
    for name in tuple(forwarded):
        if name.casefold() == "user-agent":
            forwarded.pop(name)
    forwarded["User-Agent"] = _RELAY_USER_AGENT
    forwarded["Accept-Encoding"] = "identity"
    return forwarded


@dataclass(frozen=True)
class _IdleHTTPSConnection:
    connection: Any
    returned_at: float


class _DirectHTTPSConnectionPool:
    """Bound concurrent upstream sockets and reuse only fully consumed responses."""

    def __init__(
        self,
        *,
        connection_factory: Callable[..., Any],
        ssl_context: ssl.SSLContext,
        max_connections: int,
        idle_timeout_seconds: float,
        acquire_timeout_seconds: float,
    ) -> None:
        if isinstance(max_connections, bool) or not isinstance(max_connections, int):
            raise A6UpstreamRelayError("relay_pool_size_invalid")
        if max_connections <= 0:
            raise A6UpstreamRelayError("relay_pool_size_invalid")
        for value, code in (
            (idle_timeout_seconds, "relay_pool_idle_timeout_invalid"),
            (acquire_timeout_seconds, "relay_pool_acquire_timeout_invalid"),
        ):
            try:
                parsed = float(value)
            except (TypeError, ValueError):
                raise A6UpstreamRelayError(code) from None
            if isinstance(value, bool) or not math.isfinite(parsed) or parsed <= 0:
                raise A6UpstreamRelayError(code)
        self.connection_factory = connection_factory
        self.ssl_context = ssl_context
        self.max_connections = max_connections
        self.idle_timeout_seconds = float(idle_timeout_seconds)
        self.acquire_timeout_seconds = float(acquire_timeout_seconds)
        self._slots = threading.BoundedSemaphore(max_connections)
        self._idle: queue.LifoQueue[_IdleHTTPSConnection] = queue.LifoQueue(maxsize=max_connections)
        self._state_lock = threading.Lock()
        self._closed = False

    def acquire(self, *, request_timeout: float) -> Any:
        if not self._slots.acquire(timeout=self.acquire_timeout_seconds):
            raise A6UpstreamRelayError("relay_connection_pool_exhausted")
        with self._state_lock:
            closed = self._closed
        if closed:
            self._slots.release()
            raise A6UpstreamRelayError("relay_connection_pool_closed")

        now = time.monotonic()
        while True:
            try:
                idle = self._idle.get_nowait()
            except queue.Empty:
                break
            connection = idle.connection
            if (
                now - idle.returned_at <= self.idle_timeout_seconds
                and getattr(connection, "sock", None) is not None
            ):
                self._configure_timeout(connection, request_timeout)
                return connection
            self._close_connection(connection)

        try:
            return self.connection_factory(
                "a6.a6api.com",
                port=443,
                timeout=float(request_timeout),
                context=self.ssl_context,
            )
        except Exception:
            self._slots.release()
            raise

    def release(self, connection: Any, *, reusable: bool) -> None:
        with self._state_lock:
            closed = self._closed
        if reusable and not closed and getattr(connection, "sock", None) is not None:
            try:
                self._idle.put_nowait(_IdleHTTPSConnection(connection, time.monotonic()))
            except queue.Full:
                self._close_connection(connection)
        else:
            self._close_connection(connection)
        self._slots.release()

    def close(self) -> None:
        with self._state_lock:
            self._closed = True
        while True:
            try:
                idle = self._idle.get_nowait()
            except queue.Empty:
                break
            self._close_connection(idle.connection)

    @staticmethod
    def _configure_timeout(connection: Any, timeout: float) -> None:
        connection.timeout = float(timeout)
        sock = getattr(connection, "sock", None)
        settimeout = getattr(sock, "settimeout", None)
        if callable(settimeout):
            settimeout(float(timeout))

    @staticmethod
    def _close_connection(connection: Any) -> None:
        with suppress(Exception):
            connection.close()


class DirectHTTPResponse:
    """Streaming response that returns an eligible connection to its pool."""

    def __init__(
        self,
        pool: _DirectHTTPSConnectionPool,
        connection: Any,
        response: Any,
    ) -> None:
        self.pool = pool
        self.connection = connection
        self.response = response
        self.status_code = int(response.status)
        self.headers = response.headers
        self._complete = False
        self._closed = False

    def iter_content(self, chunk_size: int = 16_384):
        while True:
            chunk = self.response.read(int(chunk_size))
            if not chunk:
                self._complete = True
                return
            yield chunk

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        reusable = self._complete and getattr(self.response, "will_close", True) is False
        try:
            self.response.close()
        except Exception:
            reusable = False
        finally:
            self.pool.release(self.connection, reusable=reusable)


class DirectHTTPSession:
    """Direct HTTP/1.1 transport; it never consults environment proxy settings."""

    def __init__(
        self,
        *,
        connection_factory: Callable[..., Any] = http.client.HTTPSConnection,
        ssl_context: ssl.SSLContext | None = None,
        max_connections: int = _DEFAULT_POOL_MAX_CONNECTIONS,
        idle_timeout_seconds: float = _DEFAULT_POOL_IDLE_TIMEOUT_SECONDS,
        acquire_timeout_seconds: float = _DEFAULT_POOL_ACQUIRE_TIMEOUT_SECONDS,
    ) -> None:
        self.connection_factory = connection_factory
        self.ssl_context = ssl_context or ssl.create_default_context()
        self.pool = _DirectHTTPSConnectionPool(
            connection_factory=connection_factory,
            ssl_context=self.ssl_context,
            max_connections=max_connections,
            idle_timeout_seconds=idle_timeout_seconds,
            acquire_timeout_seconds=acquire_timeout_seconds,
        )

    def request(
        self,
        *,
        method: str,
        url: str,
        headers: Mapping[str, str],
        data: bytes,
        timeout: float,
        stream_read_timeout: float | None = None,
        allow_redirects: bool,
        stream: bool,
    ) -> DirectHTTPResponse:
        parsed = urlsplit(str(url))
        if (
            parsed.scheme != "https"
            or parsed.hostname != "a6.a6api.com"
            or parsed.port is not None
            or parsed.username is not None
            or parsed.password is not None
            or allow_redirects is not False
            or stream is not True
        ):
            raise A6UpstreamRelayError("relay_direct_transport_target_invalid")
        path = parsed.path or "/"
        if parsed.query:
            path = f"{path}?{parsed.query}"
        try:
            response_header_timeout = float(timeout)
            response_body_timeout = float(
                timeout if stream_read_timeout is None else stream_read_timeout
            )
        except (TypeError, ValueError):
            raise A6UpstreamRelayError("relay_direct_transport_timeout_invalid") from None
        if (
            isinstance(timeout, bool)
            or isinstance(stream_read_timeout, bool)
            or not math.isfinite(response_header_timeout)
            or not math.isfinite(response_body_timeout)
            or response_header_timeout <= 0
            or response_body_timeout <= 0
        ):
            raise A6UpstreamRelayError("relay_direct_transport_timeout_invalid")

        connection = self.pool.acquire(request_timeout=response_header_timeout)
        try:
            connection.request(
                str(method).upper(),
                path,
                body=bytes(data),
                headers=dict(headers),
            )
            response = connection.getresponse()
            self.pool._configure_timeout(connection, response_body_timeout)
            return DirectHTTPResponse(self.pool, connection, response)
        except Exception:
            self.pool.release(connection, reusable=False)
            raise

    def close(self) -> None:
        self.pool.close()


class A6UpstreamRelay:
    def __init__(
        self,
        resolver: DirectRelayResolver,
        *,
        session: Any = None,
        response_header_timeout_seconds: float = _DEFAULT_RESPONSE_HEADER_TIMEOUT_SECONDS,
        stream_read_timeout_seconds: float = _DEFAULT_STREAM_READ_TIMEOUT_SECONDS,
        readiness_timeout_seconds: float = _DEFAULT_READINESS_TIMEOUT_SECONDS,
    ) -> None:
        for value, code in (
            (response_header_timeout_seconds, "relay_response_header_timeout_invalid"),
            (stream_read_timeout_seconds, "relay_stream_read_timeout_invalid"),
            (readiness_timeout_seconds, "relay_readiness_timeout_invalid"),
        ):
            if isinstance(value, bool) or not math.isfinite(value) or value <= 0:
                raise A6UpstreamRelayError(code)
        if session is None:
            session = DirectHTTPSession()
        self.resolver = resolver
        self.session = session
        self.response_header_timeout_seconds = float(response_header_timeout_seconds)
        self.stream_read_timeout_seconds = float(stream_read_timeout_seconds)
        self.readiness_timeout_seconds = float(readiness_timeout_seconds)

    def open(
        self,
        method: str,
        raw_path: str,
        headers: Mapping[str, str],
        body: bytes,
    ) -> tuple[RelayTarget, Any]:
        target = self.resolver.resolve(raw_path)
        options: dict[str, Any] = {
            "method": str(method).upper(),
            "url": target.upstream_url,
            "headers": _forward_request_headers(headers),
            "data": body,
            "timeout": self.response_header_timeout_seconds,
            "stream_read_timeout": self.stream_read_timeout_seconds,
            "allow_redirects": False,
            "stream": True,
        }
        try:
            response = self.session.request(**options)
        except A6UpstreamRelayError:
            raise
        except TimeoutError:
            raise A6UpstreamRelayError("relay_upstream_timeout") from None
        except Exception:
            raise A6UpstreamRelayError("relay_upstream_transport_failed") from None
        return target, response

    def probe_upstream(self) -> None:
        response = None
        total_bytes = 0
        try:
            response = self.session.request(
                method="GET",
                url=f"{self.resolver.upstream_origin}/v1/models",
                headers=_forward_request_headers({}),
                data=b"",
                timeout=self.readiness_timeout_seconds,
                allow_redirects=False,
                stream=True,
            )
            if int(response.status_code) != 401:
                raise A6UpstreamRelayError("relay_upstream_readiness_status_invalid")
            for chunk in response.iter_content(chunk_size=4096):
                total_bytes += len(chunk)
                if total_bytes > _MAX_READINESS_RESPONSE_BYTES:
                    raise A6UpstreamRelayError("relay_upstream_readiness_body_too_large")
        except A6UpstreamRelayError:
            raise
        except Exception:
            raise A6UpstreamRelayError("relay_upstream_readiness_failed") from None
        finally:
            close = getattr(response, "close", None)
            if callable(close):
                close()

    def close(self) -> None:
        close = getattr(self.session, "close", None)
        if callable(close):
            close()


class _RelayHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        server_address: tuple[str, int],
        relay: A6UpstreamRelay,
        *,
        max_request_bytes: int,
    ) -> None:
        super().__init__(server_address, _RelayRequestHandler)
        self.relay = relay
        self.max_request_bytes = max_request_bytes


class _RelayRequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server: _RelayHTTPServer

    def log_message(self, format: str, *args: object) -> None:
        return

    def _json_error(self, status: int, error_type: str, message: str) -> None:
        document = json.dumps(
            {"error": {"type": error_type, "message": message}},
            separators=(",", ":"),
        ).encode()
        self.close_connection = True
        with suppress(BrokenPipeError, ConnectionResetError):
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(document)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(document)

    def _health(self) -> None:
        document = b'{"status":"ok"}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(document)))
        self.end_headers()
        self.wfile.write(document)

    def _readiness(self) -> None:
        try:
            self.server.relay.probe_upstream()
        except A6UpstreamRelayError:
            status = 503
            document = b'{"status":"not_ready","checks":{"upstream":"failed"}}'
        else:
            status = 200
            document = b'{"status":"ready","checks":{"upstream":"ok"}}'
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(document)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(document)

    def _request_body(self) -> bytes:
        raw_length = str(self.headers.get("Content-Length") or "0").strip()
        try:
            length = int(raw_length)
        except ValueError:
            raise A6UpstreamRelayError("relay_content_length_invalid") from None
        if length < 0 or length > self.server.max_request_bytes:
            raise A6UpstreamRelayError("relay_request_too_large")
        return self.rfile.read(length) if length else b""

    def _proxy(self) -> None:
        if self.path == "/healthz":
            self._health()
            return
        if self.path == "/readyz":
            self._readiness()
            return
        started = time.monotonic()
        response = None
        target = None
        response_started = False
        header_ms: int | None = None
        try:
            body = self._request_body()
            target, response = self.server.relay.open(
                self.command,
                self.path,
                dict(self.headers.items()),
                body,
            )
            header_ms = round((time.monotonic() - started) * 1000)
            status = int(response.status_code)
            self.send_response(status)
            response_headers = getattr(response, "headers", {})
            for name, value in response_headers.items():
                if str(name).casefold() in _RESPONSE_HEADER_ALLOWLIST:
                    self.send_header(str(name), str(value))
            self.send_header("Connection", "close")
            self.end_headers()
            response_started = True
            content_type = str(response_headers.get("content-type") or "").casefold()
            for chunk in response.iter_content(chunk_size=16_384):
                if not chunk:
                    continue
                self.wfile.write(chunk)
                if "text/event-stream" in content_type:
                    self.wfile.flush()
            self.close_connection = True
            logger.info(
                "relay_request method=%s a6_user_id=%s status=%s header_ms=%s elapsed_ms=%s",
                self.command,
                target.a6_user_id,
                status,
                header_ms,
                round((time.monotonic() - started) * 1000),
            )
        except A6UpstreamRelayError as exc:
            if not response_started:
                status, error_type, message = _public_error_response(str(exc))
                self._json_error(status, error_type, message)
            else:
                self.close_connection = True
            logger.warning(
                "relay_request_failed method=%s error=%s header_ms=%s elapsed_ms=%s",
                self.command,
                str(exc),
                "none" if header_ms is None else header_ms,
                round((time.monotonic() - started) * 1000),
            )
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except Exception:
            if not response_started:
                status, error_type, message = _public_error_response(
                    "relay_upstream_stream_failed"
                )
                self._json_error(status, error_type, message)
            else:
                self.close_connection = True
            logger.exception(
                "relay_stream_failed method=%s a6_user_id=%s header_ms=%s elapsed_ms=%s",
                self.command,
                None if target is None else target.a6_user_id,
                "none" if header_ms is None else header_ms,
                round((time.monotonic() - started) * 1000),
            )
        finally:
            close = getattr(response, "close", None)
            if callable(close):
                close()

    do_DELETE = _proxy
    do_GET = _proxy
    do_PATCH = _proxy
    do_POST = _proxy
    do_PUT = _proxy


def serve_a6_upstream_relay(
    *,
    host: str,
    port: int,
    upstream_origin: str = A6_UPSTREAM_ORIGIN,
    response_header_timeout_seconds: float = _DEFAULT_RESPONSE_HEADER_TIMEOUT_SECONDS,
    stream_read_timeout_seconds: float = _DEFAULT_STREAM_READ_TIMEOUT_SECONDS,
    max_request_bytes: int = 64 * 1024 * 1024,
    pool_max_connections: int = _DEFAULT_POOL_MAX_CONNECTIONS,
    pool_idle_timeout_seconds: float = _DEFAULT_POOL_IDLE_TIMEOUT_SECONDS,
    pool_acquire_timeout_seconds: float = _DEFAULT_POOL_ACQUIRE_TIMEOUT_SECONDS,
    readiness_timeout_seconds: float = _DEFAULT_READINESS_TIMEOUT_SECONDS,
) -> None:
    if not 1 <= int(port) <= 65535:
        raise A6UpstreamRelayError("relay_port_invalid")
    if not 1 <= int(max_request_bytes) <= 1024 * 1024 * 1024:
        raise A6UpstreamRelayError("relay_request_limit_invalid")
    resolver = DirectRelayResolver(upstream_origin=upstream_origin)
    relay = A6UpstreamRelay(
        resolver,
        response_header_timeout_seconds=response_header_timeout_seconds,
        stream_read_timeout_seconds=stream_read_timeout_seconds,
        readiness_timeout_seconds=readiness_timeout_seconds,
        session=DirectHTTPSession(
            max_connections=pool_max_connections,
            idle_timeout_seconds=pool_idle_timeout_seconds,
            acquire_timeout_seconds=pool_acquire_timeout_seconds,
        ),
    )
    server = _RelayHTTPServer(
        (str(host), int(port)),
        relay,
        max_request_bytes=int(max_request_bytes),
    )
    logger.info("relay_started host=%s port=%s", host, port)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        relay.close()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Loopback direct A6 Provider upstream relay")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8767)
    parser.add_argument(
        "--response-header-timeout-seconds",
        type=float,
        default=_DEFAULT_RESPONSE_HEADER_TIMEOUT_SECONDS,
    )
    parser.add_argument(
        "--stream-read-timeout-seconds",
        type=float,
        default=_DEFAULT_STREAM_READ_TIMEOUT_SECONDS,
    )
    parser.add_argument(
        "--readiness-timeout-seconds",
        type=float,
        default=_DEFAULT_READINESS_TIMEOUT_SECONDS,
    )
    parser.add_argument("--max-request-bytes", type=int, default=64 * 1024 * 1024)
    parser.add_argument("--pool-max-connections", type=int, default=_DEFAULT_POOL_MAX_CONNECTIONS)
    parser.add_argument(
        "--pool-idle-timeout-seconds",
        type=float,
        default=_DEFAULT_POOL_IDLE_TIMEOUT_SECONDS,
    )
    parser.add_argument(
        "--pool-acquire-timeout-seconds",
        type=float,
        default=_DEFAULT_POOL_ACQUIRE_TIMEOUT_SECONDS,
    )
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    args = _parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    serve_a6_upstream_relay(
        host=args.host,
        port=args.port,
        response_header_timeout_seconds=args.response_header_timeout_seconds,
        stream_read_timeout_seconds=args.stream_read_timeout_seconds,
        readiness_timeout_seconds=args.readiness_timeout_seconds,
        max_request_bytes=args.max_request_bytes,
        pool_max_connections=args.pool_max_connections,
        pool_idle_timeout_seconds=args.pool_idle_timeout_seconds,
        pool_acquire_timeout_seconds=args.pool_acquire_timeout_seconds,
    )


if __name__ == "__main__":
    main()
