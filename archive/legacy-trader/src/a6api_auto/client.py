from __future__ import annotations

import json
import math
import re
import secrets
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urljoin, urlsplit

from .errors import (
    A6AutoError,
    A6HttpError,
    A6MutationError,
    A6ProbeTransportError,
    A6TransportError,
)
from .models import DEFAULT_QUOTA_PER_UNIT
from .probe_prompts import build_short_probe_question

_TOKEN_UPDATE_DEFAULTS: dict[str, Any] = {
    "remain_quota": 0,
    "remain_amount": 0,
    "expired_time": -1,
    "unlimited_quota": True,
    "model_limits_enabled": False,
    "model_limits": "",
    "allow_ips": "",
    "group": "",
    "cross_group_retry": False,
    "routing_enabled": True,
    "routing_preference": "cheap",
    "pricing_strategy": "auto",
    "pricing_custom_pct": 50,
    "routing_allow_fallback": True,
}
_TOKEN_UPDATE_FIELDS = ("name", *_TOKEN_UPDATE_DEFAULTS)
_BEARER_SECRET = re.compile(r"(?i)(bearer\s+)[^\s,;]+")
_API_KEY_SECRET = re.compile(r"sk-[A-Za-z0-9._-]+")
_IDEMPOTENT_GET_TRANSPORT_ATTEMPTS = 2


class A6ApiClient:
    def __init__(
        self,
        origin: str = "https://a6api.com",
        *,
        proxy_url: str = "",
        session: Any = None,
        timeout: float = 30,
    ) -> None:
        if isinstance(timeout, bool) or not math.isfinite(timeout) or timeout <= 0:
            raise A6AutoError("a6_timeout_invalid")
        self.origin = origin.rstrip("/")
        self.proxy_url = proxy_url
        self.timeout = float(timeout)
        self._request_deadline_override: ContextVar[float | None] = ContextVar(
            "a6_client_request_deadline_override",
            default=None,
        )
        if session is None:
            try:
                from curl_cffi import requests

                session = requests.Session()
            except Exception as exc:
                raise A6AutoError("http_dependency_unavailable") from exc
        self.session = session

    def _request_timeout(self) -> float:
        deadline = self._request_deadline_override.get()
        if deadline is None:
            return self.timeout
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise A6AutoError("a6_request_deadline_exceeded")
        return min(self.timeout, remaining)

    @contextmanager
    def request_deadline(self, deadline: float) -> Iterator[None]:
        if (
            isinstance(deadline, bool)
            or not isinstance(deadline, (int, float))
            or not math.isfinite(float(deadline))
            or float(deadline) <= time.monotonic()
        ):
            raise A6AutoError("a6_request_deadline_invalid")
        current = self._request_deadline_override.get()
        token = self._request_deadline_override.set(
            float(deadline) if current is None else min(current, float(deadline))
        )
        try:
            yield
        finally:
            self._request_deadline_override.reset(token)

    def _request(
        self,
        method: str,
        path: str,
        *,
        user_id: int | None = None,
        referer: str = "/",
        json_body: Mapping[str, Any] | Sequence[Mapping[str, Any]] | None = None,
        params: Mapping[str, Any] | None = None,
    ) -> tuple[int, Mapping[str, Any]]:
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Cache-Control": "no-store",
            "Referer": f"{self.origin}{referer}",
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
            ),
        }
        if json_body is not None:
            headers["Content-Type"] = "application/json"
        if user_id is not None:
            headers["New-API-User"] = str(user_id)
        request_timeout = self._request_timeout()
        options: dict[str, Any] = {
            "method": method,
            "url": urljoin(f"{self.origin}/", path.lstrip("/")),
            "headers": headers,
            "timeout": request_timeout,
            "allow_redirects": False,
            "impersonate": "chrome131",
        }
        if json_body is not None:
            options["json"] = (
                dict(json_body)
                if isinstance(json_body, Mapping)
                else [dict(item) for item in json_body]
            )
        if params is not None:
            options["params"] = dict(params)
        if self.proxy_url:
            options["proxies"] = {"http": self.proxy_url, "https": self.proxy_url}
        response = None
        method_name = str(method).strip().upper()
        attempts = _IDEMPOTENT_GET_TRANSPORT_ATTEMPTS if method_name == "GET" else 1
        started_at = time.monotonic()
        request_deadline = started_at + request_timeout
        for attempt in range(attempts):
            remaining_seconds = request_deadline - time.monotonic()
            if remaining_seconds <= 0:
                raise self._transport_error(
                    method=method_name,
                    path=path,
                    user_id=user_id,
                    timeout_seconds=request_timeout,
                    attempts=attempt,
                    started_at=started_at,
                    phase="request_deadline",
                    cause_type="TimeoutError",
                )
            if attempt:
                options["timeout"] = remaining_seconds
            try:
                response = self.session.request(**options)
                break
            except Exception as exc:
                if attempt + 1 >= attempts:
                    raise self._transport_error(
                        method=method_name,
                        path=path,
                        user_id=user_id,
                        timeout_seconds=request_timeout,
                        attempts=attempt + 1,
                        started_at=started_at,
                        phase="request",
                        cause_type=type(exc).__name__,
                    ) from exc
        try:
            status = int(response.status_code)
            document = response.json()
        except Exception as exc:
            raise self._transport_error(
                method=method_name,
                path=path,
                user_id=user_id,
                timeout_seconds=request_timeout,
                attempts=max(1, attempt + 1),
                started_at=started_at,
                phase="response_decode",
                cause_type=type(exc).__name__,
                http_status=getattr(response, "status_code", None),
            ) from exc
        if not isinstance(document, Mapping):
            raise A6AutoError("a6_response_invalid")
        return status, document

    def _transport_error(
        self,
        *,
        method: str,
        path: str,
        user_id: int | None,
        timeout_seconds: float,
        attempts: int,
        started_at: float,
        phase: str,
        cause_type: str,
        http_status: Any = None,
    ) -> A6TransportError:
        proxy = urlsplit(self.proxy_url) if self.proxy_url else None
        try:
            status = int(http_status) if http_status is not None else None
        except (TypeError, ValueError):
            status = None
        return A6TransportError(
            "a6_transport_failed",
            {
                "method": str(method).upper(),
                "path": f"/{str(path).lstrip('/').split('?', 1)[0]}",
                "a6_user_id": user_id,
                "proxy_configured": bool(self.proxy_url),
                "proxy_host": proxy.hostname if proxy is not None else None,
                "proxy_port": proxy.port if proxy is not None else None,
                "timeout_seconds": round(float(timeout_seconds), 3),
                "attempts": max(0, int(attempts)),
                "elapsed_ms": max(0, round((time.monotonic() - started_at) * 1000)),
                "phase": str(phase),
                "cause_type": str(cause_type),
                "http_status": status,
            },
        )

    @staticmethod
    def _data(document: Mapping[str, Any], error: str) -> Any:
        if document.get("success") is not True:
            raise A6AutoError(error)
        return document.get("data")

    def import_cookies(self, cookies: Mapping[str, str]) -> None:
        jar = getattr(self.session, "cookies", None)
        if jar is None:
            return
        for name, value in cookies.items():
            jar.set(str(name), str(value), domain="a6api.com", path="/")

    def export_cookies(self) -> dict[str, str]:
        jar = getattr(self.session, "cookies", None)
        if jar is None:
            return {}
        if hasattr(jar, "get_dict"):
            return {str(key): str(value) for key, value in jar.get_dict().items()}
        return {str(cookie.name): str(cookie.value) for cookie in jar}

    def fork(self) -> A6ApiClient:
        """Create an independent HTTP session with the current authenticated cookies."""
        client = A6ApiClient(
            self.origin,
            proxy_url=self.proxy_url,
            timeout=self.timeout,
        )
        client.import_cookies(self.export_cookies())
        return client

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
    ) -> tuple[int, Mapping[str, Any]]:
        """Confirm routing from the first valid Responses SSE event, then close the stream."""

        api_key = str(api_key or "").strip()
        model = str(model or "").strip()
        if not api_key or not model:
            raise A6AutoError("provider_probe_configuration_invalid")
        if (
            isinstance(max_output_tokens, bool)
            or not isinstance(max_output_tokens, int)
            or not 1 <= max_output_tokens <= 64
        ):
            raise A6AutoError("provider_probe_output_limit_invalid")
        total_timeout, connection_timeout = _probe_timeouts(
            self.timeout,
            timeout,
            connect_timeout,
            code="provider_probe_timeout_invalid",
        )
        input_text = _provider_probe_input(input_text)
        request_id = _provider_probe_request_id(request_id)
        headers = {
            "Accept": "text/event-stream",
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "X-Request-ID": request_id,
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
            ),
        }
        options: dict[str, Any] = {
            "method": "POST",
            "url": "https://a6.a6api.com/v1/responses",
            "headers": headers,
            "json": {
                "model": model,
                "input": input_text,
                "max_output_tokens": max_output_tokens,
                "reasoning": {"effort": "low"},
                "stream": True,
            },
            "timeout": total_timeout,
            "allow_redirects": False,
            "stream": True,
        }
        if self.proxy_url:
            options["proxies"] = {"http": self.proxy_url, "https": self.proxy_url}
        response = None
        curl_options = getattr(self.session, "curl_options", None)
        timeout_option = None
        connect_timeout_option = None
        timeout_missing = object()
        previous_timeout: Any = timeout_missing
        previous_connect_timeout: Any = timeout_missing
        started = time.monotonic()
        try:
            if isinstance(curl_options, dict):
                from curl_cffi import CurlOpt

                timeout_option = CurlOpt.TIMEOUT_MS
                connect_timeout_option = CurlOpt.CONNECTTIMEOUT_MS
                previous_timeout = curl_options.get(timeout_option, timeout_missing)
                previous_connect_timeout = curl_options.get(
                    connect_timeout_option,
                    timeout_missing,
                )
                curl_options[timeout_option] = max(1, round(total_timeout * 1000))
                curl_options[connect_timeout_option] = max(
                    1,
                    round(connection_timeout * 1000),
                )
            response = self.session.request(**options)
            status = int(response.status_code)
            if not 200 <= status < 300:
                try:
                    document = response.json()
                except Exception:
                    document = _bounded_stream_json(response)
                if not isinstance(document, Mapping):
                    raise A6AutoError("provider_probe_response_invalid")
                return status, document
            event_name = ""
            for raw_line in response.iter_lines():
                line = (
                    raw_line.decode("utf-8", errors="replace")
                    if isinstance(raw_line, bytes)
                    else str(raw_line or "")
                ).strip()
                if line.startswith("event:"):
                    event_name = line[6:].strip()
                    continue
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if not data or data == "[DONE]":
                    continue
                try:
                    event = json.loads(data)
                except (TypeError, ValueError):
                    continue
                event_type = str(event.get("type") or event_name).strip()
                if event_type.startswith("error") or event_type == "response.failed":
                    error = event.get("error")
                    response_data = event.get("response")
                    if not isinstance(error, Mapping) and isinstance(response_data, Mapping):
                        error = response_data.get("error")
                    error = error if isinstance(error, Mapping) else event
                    code = str(error.get("code") or "").strip().lower()
                    message = str(error.get("message") or "").casefold()
                    if not code and (
                        "账户额度不足" in message
                        or "账户余额或订阅额度不足" in message
                        or "insufficient quota" in message
                    ):
                        code = "account_quota_insufficient"
                    if not code and "no_available_channel" in message:
                        code = "no_available_channel"
                    if not code and "fixed_merchant_unavailable" in message:
                        code = "fixed_merchant_unavailable"
                    try:
                        error_status = int(error.get("status") or event.get("status") or 0)
                    except (TypeError, ValueError):
                        error_status = 0
                    if not error_status:
                        error_status = {
                            "fixed_merchant_unavailable": 402,
                            "insufficient_quota": 403,
                            "account_quota_insufficient": 403,
                            "no_available_channel": 503,
                        }.get(code, 502)
                    return error_status, {"error": {"code": code}}
                if event_type == "response.created":
                    return status, {"probe_event": "response.created"}
            raise A6AutoError("provider_probe_stream_invalid")
        except A6AutoError:
            raise
        except Exception as exc:
            raise _provider_probe_transport_error(
                "provider_probe_transport_failed",
                "responses",
                exc,
                response=response,
                request_id=request_id,
                started=started,
            ) from None
        finally:
            close = getattr(response, "close", None)
            if callable(close):
                close()
            if isinstance(curl_options, dict) and timeout_option is not None:
                if previous_timeout is timeout_missing:
                    curl_options.pop(timeout_option, None)
                else:
                    curl_options[timeout_option] = previous_timeout
            if isinstance(curl_options, dict) and connect_timeout_option is not None:
                if previous_connect_timeout is timeout_missing:
                    curl_options.pop(connect_timeout_option, None)
                else:
                    curl_options[connect_timeout_option] = previous_connect_timeout

    def provider_responses_request(
        self,
        api_key: str,
        model: str,
        input_text: str,
        *,
        max_output_tokens: int = 128,
        timeout: float = 12,
    ) -> tuple[int, Mapping[str, Any]]:
        """Run one bounded, ordinary Responses request through a Provider Pin."""

        api_key = str(api_key or "").strip()
        model = str(model or "").strip()
        input_text = str(input_text or "").strip()
        if not api_key or not model or not 8 <= len(input_text) <= 12_000:
            raise A6AutoError("provider_request_configuration_invalid")
        if (
            isinstance(max_output_tokens, bool)
            or not isinstance(max_output_tokens, int)
            or not 16 <= max_output_tokens <= 512
        ):
            raise A6AutoError("provider_request_output_limit_invalid")
        if isinstance(timeout, bool) or not math.isfinite(timeout) or timeout <= 0:
            raise A6AutoError("provider_request_timeout_invalid")
        options: dict[str, Any] = {
            "method": "POST",
            "url": "https://a6.a6api.com/v1/responses",
            "headers": {
                "Accept": "application/json",
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/150.0.0.0 Safari/537.36"
                ),
            },
            "json": {
                "model": model,
                "input": input_text,
                "max_output_tokens": max_output_tokens,
                "reasoning": {"effort": "low"},
                "stream": False,
            },
            "timeout": min(self.timeout, float(timeout)),
            "allow_redirects": False,
            "impersonate": "chrome131",
        }
        if self.proxy_url:
            options["proxies"] = {"http": self.proxy_url, "https": self.proxy_url}
        try:
            response = self.session.request(**options)
            status = int(response.status_code)
            document = response.json()
        except Exception:
            raise A6AutoError("provider_request_transport_failed") from None
        if not isinstance(document, Mapping):
            raise A6AutoError("provider_request_response_invalid")
        return status, document

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
    ) -> tuple[int, Mapping[str, Any]]:
        """Run one bounded, non-streaming Chat Completions probe."""

        api_key = str(api_key or "").strip()
        model = str(model or "").strip()
        if not api_key or not model:
            raise A6AutoError("provider_chat_probe_configuration_invalid")
        if (
            isinstance(max_output_tokens, bool)
            or not isinstance(max_output_tokens, int)
            or not 1 <= max_output_tokens <= 64
        ):
            raise A6AutoError("provider_chat_probe_output_limit_invalid")
        total_timeout, connection_timeout = _probe_timeouts(
            self.timeout,
            timeout,
            connect_timeout,
            code="provider_chat_probe_timeout_invalid",
        )
        input_text = _provider_probe_input(input_text)
        request_id = _provider_probe_request_id(request_id)
        options: dict[str, Any] = {
            "method": "POST",
            "url": "https://a6.a6api.com/v1/chat/completions",
            "headers": {
                "Accept": "application/json",
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "X-Request-ID": request_id,
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/150.0.0.0 Safari/537.36"
                ),
            },
            "json": {
                "model": model,
                "messages": [{"role": "user", "content": input_text}],
                "max_completion_tokens": max_output_tokens,
                "stream": False,
            },
            "timeout": total_timeout,
            "allow_redirects": False,
            "impersonate": "chrome131",
        }
        if self.proxy_url:
            options["proxies"] = {"http": self.proxy_url, "https": self.proxy_url}
        response = None
        curl_options = getattr(self.session, "curl_options", None)
        timeout_option = None
        connect_timeout_option = None
        timeout_missing = object()
        previous_timeout: Any = timeout_missing
        previous_connect_timeout: Any = timeout_missing
        started = time.monotonic()
        try:
            if isinstance(curl_options, dict):
                from curl_cffi import CurlOpt

                timeout_option = CurlOpt.TIMEOUT_MS
                connect_timeout_option = CurlOpt.CONNECTTIMEOUT_MS
                previous_timeout = curl_options.get(timeout_option, timeout_missing)
                previous_connect_timeout = curl_options.get(
                    connect_timeout_option,
                    timeout_missing,
                )
                curl_options[timeout_option] = max(1, round(total_timeout * 1000))
                curl_options[connect_timeout_option] = max(
                    1,
                    round(connection_timeout * 1000),
                )
            response = self.session.request(**options)
            status = int(response.status_code)
            document = response.json()
        except A6AutoError:
            raise
        except Exception as exc:
            raise _provider_probe_transport_error(
                "provider_chat_probe_transport_failed",
                "chat_completions",
                exc,
                response=response,
                request_id=request_id,
                started=started,
            ) from None
        finally:
            if isinstance(curl_options, dict) and timeout_option is not None:
                if previous_timeout is timeout_missing:
                    curl_options.pop(timeout_option, None)
                else:
                    curl_options[timeout_option] = previous_timeout
            if isinstance(curl_options, dict) and connect_timeout_option is not None:
                if previous_connect_timeout is timeout_missing:
                    curl_options.pop(connect_timeout_option, None)
                else:
                    curl_options[connect_timeout_option] = previous_connect_timeout
        if not isinstance(document, Mapping):
            raise A6AutoError("provider_chat_probe_response_invalid")
        return status, document

    def send_verification(self, email: str) -> None:
        status, document = self._request(
            "GET",
            "/api/verification",
            user_id=-1,
            referer="/register",
            params={"email": email, "turnstile": ""},
        )
        message = str(document.get("message") or "")
        if status == 200 and document.get("success") is not True and "已被占用" in message:
            raise A6AutoError("email_already_registered")
        if status != 200 or document.get("success") is not True:
            raise A6AutoError("verification_send_rejected")

    def register(self, username: str, email: str, password: str, code: str) -> None:
        status, document = self._request(
            "POST",
            "/api/user/register",
            user_id=-1,
            referer="/register",
            params={"turnstile": ""},
            json_body={
                "username": username,
                "password": password,
                "password2": password,
                "email": email,
                "verification_code": code,
                "wechat_verification_code": "",
                "aff_code": None,
            },
        )
        if status != 200 or document.get("success") is not True:
            raise A6AutoError("registration_rejected")

    def login(self, username: str, password: str) -> dict[str, Any] | None:
        status, document = self._request(
            "POST",
            "/api/user/login",
            user_id=-1,
            referer="/login",
            params={"turnstile": ""},
            json_body={"username": username, "password": password},
        )
        data = document.get("data")
        if status != 200 or document.get("success") is not True or not isinstance(data, Mapping):
            return None
        return dict(data)

    def self_user(self, user_id: int | None = None) -> dict[str, Any] | None:
        status, document = self._request(
            "GET",
            "/api/user/self",
            user_id=user_id,
            referer="/console/personal",
        )
        data = document.get("data")
        if status != 200 or document.get("success") is not True or not isinstance(data, Mapping):
            return None
        return dict(data)

    def list_tokens(self, user_id: int) -> list[dict[str, Any]]:
        status, document = self._request(
            "GET",
            "/api/token/",
            user_id=user_id,
            referer="/console/token",
            params={"p": 1, "size": 100},
        )
        data = document.get("data")
        items = data.get("items") if isinstance(data, Mapping) else None
        if status != 200 or document.get("success") is not True or not isinstance(items, list):
            raise _read_error(status, "token_list_failed")
        return [dict(item) for item in items if isinstance(item, Mapping)]

    def get_token(self, user_id: int, token_id: int) -> dict[str, Any]:
        if isinstance(token_id, bool) or not isinstance(token_id, int) or token_id < 1:
            raise A6AutoError("token_id_invalid")
        status, document = self._request(
            "GET",
            f"/api/token/{token_id}",
            user_id=user_id,
            referer="/console/token",
        )
        token = _mapping_data(status, document, "token_read_failed")
        if _positive_integer(token.get("id")) != token_id:
            raise A6AutoError("token_readback_id_mismatch")
        return token

    def update_token(
        self,
        user_id: int,
        token_id: int,
        metadata: Mapping[str, Any],
    ) -> dict[str, Any]:
        if isinstance(token_id, bool) or not isinstance(token_id, int) or token_id < 1:
            raise A6AutoError("token_id_invalid")
        name = str(metadata.get("name") or "").strip()
        if not name:
            raise A6AutoError("token_name_invalid")
        payload = {
            field: metadata[field]
            for field in _TOKEN_UPDATE_FIELDS
            if field in metadata
        }
        payload["name"] = name
        payload["id"] = token_id
        status, document = self._request(
            "PUT",
            "/api/token/",
            user_id=user_id,
            referer="/console/token",
            json_body=payload,
        )
        return _mutation_data(status, document, "token_update_failed")

    def rename_token(self, user_id: int, token_id: int, new_name: str) -> dict[str, Any]:
        new_name = str(new_name or "").strip()
        if not new_name:
            raise A6AutoError("token_name_invalid")
        current = self.get_token(user_id, token_id)
        payload = dict(_TOKEN_UPDATE_DEFAULTS)
        for field in _TOKEN_UPDATE_FIELDS:
            if field in current:
                payload[field] = current[field]
        payload["name"] = new_name
        self.update_token(user_id, token_id, payload)
        observed = self.get_token(user_id, token_id)
        if str(observed.get("name") or "") != new_name:
            raise A6AutoError("token_rename_readback_mismatch")
        return observed

    def ensure_one_key(self, user_id: int, token_name: str) -> str:
        matches = [item for item in self.list_tokens(user_id) if item.get("name") == token_name]
        if not matches:
            status, document = self._request(
                "POST",
                "/api/token/",
                user_id=user_id,
                referer="/console/token",
                json_body={
                    "name": token_name,
                    "remain_quota": 0,
                    "expired_time": -1,
                    "unlimited_quota": True,
                    "model_limits_enabled": False,
                    "model_limits": "",
                    "allow_ips": "",
                    "group": "default",
                    "cross_group_retry": False,
                },
            )
            if status != 200 or document.get("success") is not True:
                raise A6AutoError("token_create_failed")
            matches = [item for item in self.list_tokens(user_id) if item.get("name") == token_name]
        if not matches:
            raise A6AutoError("token_reconciliation_failed")
        target = max(matches, key=lambda item: int(item.get("id") or 0))
        token_id = int(target.get("id") or 0)
        if token_id < 1:
            raise A6AutoError("token_id_missing")
        status, document = self._request(
            "POST",
            f"/api/token/{token_id}/key",
            user_id=user_id,
            referer="/console/token",
            json_body=None,
        )
        data = document.get("data")
        key = str(data.get("key") or "").strip() if isinstance(data, Mapping) else ""
        if status != 200 or document.get("success") is not True or not key:
            raise A6AutoError("token_reveal_failed")
        return key if key.startswith("sk-") else f"sk-{key}"

    def fetch_models_via_a6(self, user_id: int, base_url: str, api_key: str) -> list[str]:
        status, document = self._request(
            "POST",
            "/api/supplier/fetch-models",
            user_id=user_id,
            referer="/supplier/apply",
            json_body={"base_url": base_url, "key": api_key, "channel_type": 1},
        )
        data = document.get("data")
        if status != 200 or document.get("success") is not True or not isinstance(data, list):
            raise A6AutoError("upstream_models_unavailable")
        return _normalize_models(data)

    def apply_supplier(self, user_id: int, payload: Mapping[str, Any]) -> dict[str, Any]:
        status, document = self._request(
            "POST",
            "/api/supplier/apply",
            user_id=user_id,
            referer="/supplier/apply",
            json_body=payload,
        )
        data = document.get("data")
        if status != 200 or document.get("success") is not True:
            if _document_has_code(document, "pending_request_exists"):
                raise A6AutoError("supplier_apply_pending_request_exists")
            raise A6AutoError("supplier_apply_failed")
        return dict(data) if isinstance(data, Mapping) else {}

    def supplier_requests(self, user_id: int) -> list[dict[str, Any]]:
        status, document = self._request(
            "GET",
            "/api/supplier/me/requests",
            user_id=user_id,
            referer="/supplier/requests",
        )
        data = document.get("data")
        if status != 200 or document.get("success") is not True or not isinstance(data, list):
            raise _read_error(status, "supplier_requests_failed")
        return [dict(item) for item in data if isinstance(item, Mapping)]

    def supplier_me(self, user_id: int) -> dict[str, Any]:
        status, document = self._request(
            "GET",
            "/api/supplier/me",
            user_id=user_id,
            referer="/supplier",
        )
        return _mapping_data(status, document, "supplier_self_failed")

    def supplier_channels(self, user_id: int) -> list[dict[str, Any]]:
        status, document = self._request(
            "GET",
            "/api/supplier/me/channels",
            user_id=user_id,
            referer="/supplier/channels",
        )
        data = document.get("data")
        if status != 200 or document.get("success") is not True or not isinstance(data, list):
            raise _read_error(status, "supplier_channels_failed")
        channels: list[dict[str, Any]] = []
        for item in data:
            if not isinstance(item, Mapping):
                continue
            channel = _supplier_channel(item)
            if channel is not None:
                channels.append(channel)
        return channels

    def supplier_listings(
        self,
        user_id: int,
        *,
        page: int = 1,
        size: int = 1000,
        status: int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"page": page, "size": size}
        if status is not None:
            params["status"] = status
        response_status, document = self._request(
            "GET",
            "/api/supplier/listings",
            user_id=user_id,
            referer="/supplier/listings",
            params=params,
        )
        data = document.get("data")
        if response_status != 200 or document.get("success") is not True:
            raise _read_error(response_status, "supplier_listings_failed")
        if isinstance(data, list):
            items = data
            total: Any = len(items)
            response_page: Any = page
            response_size: Any = size
        elif isinstance(data, Mapping):
            items = data.get("items")
            total = data.get("total")
            try:
                parsed_total = int(total)
            except (TypeError, ValueError):
                parsed_total = -1
            if items is None and parsed_total == 0:
                items = []
            if not isinstance(items, list):
                raise A6AutoError("supplier_listings_failed")
            response_page = data.get("page", page)
            response_size = data.get("size", size)
        else:
            raise A6AutoError("supplier_listings_failed")
        return {
            "items": [dict(item) for item in items if isinstance(item, Mapping)],
            "total": total,
            "page": response_page,
            "size": response_size,
        }

    def update_supplier_channel(
        self,
        user_id: int,
        channel_id: int,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        if channel_id < 1 or not payload:
            raise A6AutoError("supplier_channel_update_request_invalid")
        status, document = self._request(
            "PATCH",
            f"/api/supplier/me/channels/{channel_id}",
            user_id=user_id,
            referer="/supplier/channels",
            json_body=payload,
        )
        return _mutation_data(status, document, "supplier_channel_update_failed")

    def takedown_supplier_channel(
        self,
        user_id: int,
        channel_binding_id: int,
    ) -> dict[str, Any]:
        if channel_binding_id < 1:
            raise A6AutoError("supplier_channel_action_request_invalid")
        status, document = self._request(
            "POST",
            f"/api/supplier/channels/{channel_binding_id}/takedown",
            user_id=user_id,
            referer="/supplier/channels",
            json_body={},
        )
        return _mutation_data(status, document, "supplier_channel_takedown_failed")

    def create_supplier_listings(
        self,
        user_id: int,
        listings: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        payload = [dict(item) for item in listings]
        if not payload or any(not item for item in payload):
            raise A6AutoError("supplier_listing_create_request_invalid")
        status, document = self._request(
            "POST",
            "/api/supplier/listings",
            user_id=user_id,
            referer="/supplier/listings",
            json_body=payload,
        )
        return _mutation_data(status, document, "supplier_listing_create_failed")

    def update_supplier_listing(
        self,
        user_id: int,
        listing_id: int,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        if listing_id < 1 or not payload:
            raise A6AutoError("supplier_listing_update_request_invalid")
        status, document = self._request(
            "PUT",
            f"/api/supplier/listings/{listing_id}",
            user_id=user_id,
            referer="/supplier/listings",
            json_body=payload,
        )
        return _mutation_data(status, document, "supplier_listing_update_failed")

    def takedown_supplier_listing(self, user_id: int, listing_id: int) -> dict[str, Any]:
        return self._supplier_listing_empty_post(
            user_id,
            listing_id,
            operation="takedown",
        )

    def resubmit_supplier_listing(self, user_id: int, listing_id: int) -> dict[str, Any]:
        return self._supplier_listing_empty_post(
            user_id,
            listing_id,
            operation="resubmit",
        )

    def _supplier_listing_empty_post(
        self,
        user_id: int,
        listing_id: int,
        *,
        operation: str,
    ) -> dict[str, Any]:
        if listing_id < 1 or operation not in {"takedown", "resubmit"}:
            raise A6AutoError("supplier_listing_action_request_invalid")
        status, document = self._request(
            "POST",
            f"/api/supplier/listings/{listing_id}/{operation}",
            user_id=user_id,
            referer="/supplier/listings",
            json_body={},
        )
        return _mutation_data(status, document, f"supplier_listing_{operation}_failed")

    def supplier_earnings_summary(self, user_id: int) -> dict[str, Any]:
        status, document = self._request(
            "GET",
            "/api/supplier/earnings/summary",
            user_id=user_id,
            referer="/supplier/usage",
        )
        return _mapping_data(status, document, "supplier_earnings_summary_failed")

    def supplier_earnings(
        self,
        user_id: int,
        *,
        page: int = 1,
        size: int = 100,
    ) -> dict[str, Any]:
        status, document = self._request(
            "GET",
            "/api/supplier/earnings",
            user_id=user_id,
            referer="/supplier/usage",
            params={"page": page, "size": size},
        )
        return _page_data(status, document, "supplier_earnings_failed")

    def supplier_settlements_summary(self, user_id: int) -> dict[str, Any]:
        status, document = self._request(
            "GET",
            "/api/supplier/settlements/summary",
            user_id=user_id,
            referer="/supplier/settlement",
        )
        return _mapping_data(status, document, "supplier_settlements_summary_failed")

    def supplier_settlements(
        self,
        user_id: int,
        *,
        page: int = 1,
        size: int = 100,
    ) -> dict[str, Any]:
        status, document = self._request(
            "GET",
            "/api/supplier/settlements",
            user_id=user_id,
            referer="/supplier/settlement",
            params={"page": page, "size": size},
        )
        return _page_data(status, document, "supplier_settlements_failed", allow_null_items=True)

    def supplier_withdraw_summary(self, user_id: int) -> dict[str, Any]:
        status, document = self._request(
            "GET",
            "/api/supplier/withdraw/summary",
            user_id=user_id,
            referer="/supplier/withdraw",
        )
        return _mapping_data(status, document, "supplier_withdraw_summary_failed")

    def supplier_withdraw_preview(
        self,
        user_id: int,
        *,
        account_id: int,
        amount_micros: int,
    ) -> dict[str, Any]:
        if (
            isinstance(account_id, bool)
            or not isinstance(account_id, int)
            or account_id < 1
            or isinstance(amount_micros, bool)
            or not isinstance(amount_micros, int)
            or amount_micros < 50_000_000
        ):
            raise A6AutoError("supplier_withdraw_preview_request_invalid")
        status, document = self._request(
            "GET",
            "/api/supplier/withdraw/preview",
            user_id=user_id,
            referer="/supplier/withdraw",
            params={"account_id": account_id, "amount": amount_micros},
        )
        return _mapping_data(status, document, "supplier_withdraw_preview_failed")

    def supplier_withdraw_submit(
        self,
        user_id: int,
        *,
        account_id: int,
        amount_micros: int,
    ) -> dict[str, Any]:
        if (
            isinstance(account_id, bool)
            or not isinstance(account_id, int)
            or account_id < 1
            or isinstance(amount_micros, bool)
            or not isinstance(amount_micros, int)
            or amount_micros < 50_000_000
        ):
            raise A6AutoError("supplier_withdraw_submit_request_invalid")
        status, document = self._request(
            "POST",
            "/api/supplier/withdraw",
            user_id=user_id,
            referer="/supplier/withdraw",
            json_body={"account_id": account_id, "amount": amount_micros},
        )
        return _mutation_data(status, document, "supplier_withdraw_submit_failed")

    def supplier_withdrawals(
        self,
        user_id: int,
        *,
        page: int = 1,
        size: int = 100,
    ) -> dict[str, Any]:
        if (
            isinstance(page, bool)
            or not isinstance(page, int)
            or page < 1
            or isinstance(size, bool)
            or not isinstance(size, int)
            or not 1 <= size <= 1_000
        ):
            raise A6AutoError("supplier_withdrawals_request_invalid")
        status, document = self._request(
            "GET",
            "/api/supplier/withdraw",
            user_id=user_id,
            referer="/supplier/withdraw",
            params={"page": page, "size": size},
        )
        return _page_data(
            status,
            document,
            "supplier_withdrawals_failed",
            allow_null_items=True,
        )

    def supplier_withdraw_status(
        self,
        user_id: int,
        withdrawal_id: str | int,
    ) -> dict[str, Any] | None:
        expected = str(withdrawal_id or "").strip()
        if not expected:
            raise A6AutoError("supplier_withdraw_status_request_invalid")
        page = self.supplier_withdrawals(user_id, page=1, size=1_000)
        for item in page["items"]:
            candidate = item.get("id")
            if candidate is None:
                candidate = item.get("withdrawal_id")
            if str(candidate or "").strip() == expected:
                return dict(item)
        return None

    def supplier_withdraw_accounts(self, user_id: int) -> list[dict[str, Any]]:
        status, document = self._request(
            "GET",
            "/api/supplier/withdraw/accounts",
            user_id=user_id,
            referer="/supplier/withdraw",
        )
        data = document.get("data")
        if isinstance(data, Mapping):
            data = data.get("items")
        if status != 200 or document.get("success") is not True or not isinstance(data, list):
            raise A6AutoError("supplier_withdraw_accounts_failed")
        return [dict(item) for item in data if isinstance(item, Mapping)]

    def create_supplier_withdraw_account(
        self,
        user_id: int,
        *,
        network: str,
        address: str,
        remark: str,
        is_default: bool = True,
    ) -> dict[str, Any]:
        account_info = {
            "channel": "usdt",
            "remark": remark,
            "display": f"{network}: {_mask_withdraw_address(address)}",
            "fields": {"network": network, "address": address},
        }
        status, document = self._request(
            "POST",
            "/api/supplier/withdraw/accounts",
            user_id=user_id,
            referer="/supplier/withdraw",
            json_body={
                "channel": "usdt",
                "remark": remark,
                "account_info": json.dumps(
                    account_info,
                    ensure_ascii=True,
                    separators=(",", ":"),
                ),
                "is_default": bool(is_default),
            },
        )
        if status != 200 or document.get("success") is not True:
            raise A6AutoError("supplier_withdraw_account_create_failed")
        data = document.get("data")
        if data is None:
            return {}
        if not isinstance(data, Mapping):
            raise A6AutoError("supplier_withdraw_account_create_failed")
        return dict(data)

    def balance_transfer_preview(
        self,
        user_id: int,
        amount_micros: int,
        *,
        quota_per_unit: int = DEFAULT_QUOTA_PER_UNIT,
    ) -> dict[str, Any]:
        if (
            isinstance(quota_per_unit, bool)
            or not isinstance(quota_per_unit, int)
            or quota_per_unit < 1
        ):
            raise A6AutoError("quota_per_unit_invalid")
        if amount_micros < quota_per_unit:
            raise A6AutoError("balance_transfer_amount_invalid")
        status, document = self._request(
            "GET",
            "/api/supplier/balance/transfer/preview",
            user_id=user_id,
            referer="/supplier/withdraw",
            params={"amount": amount_micros},
        )
        return _mapping_data(status, document, "balance_transfer_preview_failed")

    def balance_transfer(
        self,
        user_id: int,
        amount_quota: int,
        *,
        quota_per_unit: int = DEFAULT_QUOTA_PER_UNIT,
    ) -> dict[str, Any]:
        if (
            isinstance(quota_per_unit, bool)
            or not isinstance(quota_per_unit, int)
            or quota_per_unit < 1
        ):
            raise A6AutoError("quota_per_unit_invalid")
        if (
            isinstance(amount_quota, bool)
            or not isinstance(amount_quota, int)
            or amount_quota < quota_per_unit
        ):
            raise A6AutoError("balance_transfer_amount_invalid")
        status, document = self._request(
            "POST",
            "/api/supplier/balance/transfer",
            user_id=user_id,
            referer="/supplier/withdraw",
            json_body={"amount": amount_quota},
        )
        return _mutation_data(status, document, "balance_transfer_failed")

    def user_logs(
        self,
        user_id: int,
        *,
        page: int = 1,
        page_size: int = 100,
        start_timestamp: int,
        end_timestamp: int,
        token_name: str = "",
        model_name: str = "",
        request_id: str = "",
    ) -> dict[str, Any]:
        status, document = self._request(
            "GET",
            "/api/log/self",
            user_id=user_id,
            referer="/console/log",
            params={
                "p": page,
                "page_size": page_size,
                "type": 0,
                "token_name": token_name,
                "model_name": model_name,
                "start_timestamp": start_timestamp,
                "end_timestamp": end_timestamp,
                "group": "",
                "request_id": request_id,
            },
        )
        return _page_data(status, document, "user_logs_failed")

    def marketplace_pins(self, user_id: int) -> list[dict[str, Any]]:
        status, document = self._request(
            "GET",
            "/api/marketplace/pins",
            user_id=user_id,
            referer="/marketplace",
        )
        data = document.get("data")
        if status != 200 or document.get("success") is not True or not isinstance(data, list):
            raise _read_error(status, "marketplace_pins_failed")
        return [dict(item) for item in data if isinstance(item, Mapping)]

    def marketplace_channels(
        self,
        user_id: int,
        *,
        token_id: int,
        model: str,
        page: int = 1,
        page_size: int = 100,
        sort: str = "input_asc",
        route_status: str = "all",
        exclude_unavailable: bool = True,
        include_seed: bool = True,
        include_disabled: bool = False,
        include_user_disabled: bool = False,
    ) -> dict[str, Any]:
        model = str(model or "").strip()
        sort = str(sort or "").strip()
        route_status = str(route_status or "").strip().casefold()
        if (
            isinstance(token_id, bool)
            or not isinstance(token_id, int)
            or token_id < 1
            or not model
            or isinstance(page, bool)
            or not isinstance(page, int)
            or page < 1
            or isinstance(page_size, bool)
            or not isinstance(page_size, int)
            or page_size < 1
            or not sort
            or route_status != "all"
        ):
            raise A6AutoError("marketplace_channels_request_invalid")
        if any(
            not isinstance(value, bool)
            for value in (
                exclude_unavailable,
                include_seed,
                include_disabled,
                include_user_disabled,
            )
        ):
            raise A6AutoError("marketplace_channels_request_invalid")
        status, document = self._request(
            "GET",
            "/api/marketplace/channels/search",
            user_id=user_id,
            referer="/marketplace",
            params={
                "token_id": token_id,
                "model": model,
                "route_status": route_status,
                "exclude_unavailable": str(exclude_unavailable).lower(),
                "include_seed": str(include_seed).lower(),
                "include_disabled": str(include_disabled).lower(),
                "include_user_disabled": str(include_user_disabled).lower(),
                "sort": sort,
                "view": "list",
                "page": page,
                "page_size": page_size,
            },
        )
        return _page_data(status, document, "marketplace_channels_failed")

    def marketplace_subscriptions(self, user_id: int) -> list[dict[str, Any]]:
        status, document = self._request(
            "GET",
            "/api/subscriptions",
            user_id=user_id,
            referer="/marketplace",
        )
        data = document.get("data")
        if isinstance(data, Mapping):
            data = data.get("items")
        if status != 200 or document.get("success") is not True or not isinstance(data, list):
            raise _read_error(status, "marketplace_subscriptions_failed")
        return [dict(item) for item in data if isinstance(item, Mapping)]

    def marketplace_subscription(
        self,
        user_id: int,
        *,
        subscription_id: int,
    ) -> dict[str, Any]:
        if (
            isinstance(subscription_id, bool)
            or not isinstance(subscription_id, int)
            or subscription_id < 1
        ):
            raise A6AutoError("marketplace_subscription_request_invalid")
        status, document = self._request(
            "GET",
            f"/api/subscriptions/{subscription_id}",
            user_id=user_id,
            referer="/marketplace",
        )
        return _mapping_data(status, document, "marketplace_subscription_failed")

    def marketplace_disable_channel(
        self,
        user_id: int,
        *,
        channel_id: int,
        model: str,
    ) -> dict[str, Any]:
        model = str(model or "").strip()
        if channel_id < 1 or not model:
            raise A6AutoError("marketplace_channel_disable_request_invalid")
        status, document = self._request(
            "POST",
            f"/api/marketplace/channels/{channel_id}/disable",
            user_id=user_id,
            referer="/marketplace",
            params={"model": model},
        )
        if status != 200 or document.get("success") is not True:
            raise A6MutationError(
                "marketplace_channel_disable_failed",
                operation="marketplace_channel_disable",
                status_code=status,
                provider_error_code=_provider_error_code(document),
            )
        data = document.get("data")
        if data is None:
            return {}
        if not isinstance(data, Mapping):
            raise A6AutoError("marketplace_channel_disable_failed")
        return dict(data)

    def marketplace_create_auto_pool(
        self,
        user_id: int,
        *,
        token_id: int,
        model: str,
        min_ratio: Decimal | str | int,
        max_ratio: Decimal | str | int,
    ) -> dict[str, Any]:
        model = str(model or "").strip()
        if token_id < 1 or not model:
            raise A6AutoError("marketplace_auto_pool_request_invalid")
        minimum, maximum = _auto_pool_ratios(min_ratio, max_ratio)
        status, document = self._request(
            "POST",
            "/api/subscriptions",
            user_id=user_id,
            referer="/marketplace",
            json_body={
                "token_id": token_id,
                "model_name": model,
                "channels": [],
                "caps": {
                    "input_price_micros": 0,
                    "output_price_micros": 0,
                    "cache_read_price_micros": 0,
                    "cache_write_price_micros": 0,
                    "per_request_price_micros": 0,
                },
                "sort_strategy": "price_asc",
                "fallback_enabled": False,
                "auto_pool_enabled": True,
                "min_ratio": minimum,
                "max_ratio": maximum,
            },
        )
        return _marketplace_mutation_data(
            status,
            document,
            code="marketplace_auto_pool_create_failed",
            operation="marketplace_auto_pool_create",
        )

    def marketplace_update_auto_pool(
        self,
        user_id: int,
        *,
        subscription_id: int,
        min_ratio: Decimal | str | int,
        max_ratio: Decimal | str | int,
    ) -> dict[str, Any]:
        if subscription_id < 1:
            raise A6AutoError("marketplace_auto_pool_request_invalid")
        minimum, maximum = _auto_pool_ratios(min_ratio, max_ratio)
        status, document = self._request(
            "PUT",
            f"/api/subscriptions/{subscription_id}/auto-pool",
            user_id=user_id,
            referer="/marketplace",
            json_body={
                "enabled": True,
                "min_ratio": minimum,
                "max_ratio": maximum,
            },
        )
        return _marketplace_mutation_data(
            status,
            document,
            code="marketplace_auto_pool_update_failed",
            operation="marketplace_auto_pool_update",
        )

    def marketplace_pin(
        self,
        user_id: int,
        *,
        token_id: int,
        channel_id: int,
        model: str,
        fallback_to_smart_routing: bool = False,
    ) -> dict[str, Any]:
        if token_id < 1 or channel_id < 1 or not str(model).strip():
            raise A6AutoError("marketplace_pin_request_invalid")
        if not isinstance(fallback_to_smart_routing, bool):
            raise A6AutoError("marketplace_pin_request_invalid")
        status, document = self._request(
            "POST",
            "/api/marketplace/pin",
            user_id=user_id,
            referer="/marketplace",
            json_body={
                "token_id": token_id,
                "channel_id": channel_id,
                "model_name": str(model).strip(),
                "fallback_to_smart_routing": fallback_to_smart_routing,
            },
        )
        if status != 200 or document.get("success") is not True:
            raise A6MutationError(
                "marketplace_pin_failed",
                operation="marketplace_pin",
                status_code=status,
                provider_error_code=_provider_error_code(document),
            )
        data = document.get("data")
        if data is None:
            return {}
        if not isinstance(data, Mapping):
            raise A6MutationError(
                "marketplace_pin_failed",
                operation="marketplace_pin",
                status_code=status,
                provider_error_code=_provider_error_code(document),
            )
        return dict(data)

    def marketplace_unpin(self, user_id: int, *, token_id: int, model: str) -> dict[str, Any]:
        if token_id < 1 or not str(model).strip():
            raise A6AutoError("marketplace_unpin_request_invalid")
        status, document = self._request(
            "POST",
            "/api/marketplace/unpin",
            user_id=user_id,
            referer="/marketplace",
            json_body={"token_id": token_id, "model_name": str(model).strip()},
        )
        if status != 200 or document.get("success") is not True:
            raise A6AutoError("marketplace_unpin_failed")
        data = document.get("data")
        if data is None:
            return {}
        if not isinstance(data, Mapping):
            raise A6AutoError("marketplace_unpin_failed")
        return dict(data)


def _provider_probe_input(value: object) -> str:
    if value is not None:
        text = str(value or "").strip()
        if not 8 <= len(text) <= 512:
            raise A6AutoError("provider_probe_input_invalid")
        return text
    return build_short_probe_question()


def _provider_probe_request_id(value: object) -> str:
    request_id = str(value or "").strip() or secrets.token_hex(16)
    if (
        not 16 <= len(request_id) <= 128
        or any(character.isspace() for character in request_id)
    ):
        raise A6AutoError("provider_probe_request_id_invalid")
    return request_id


def _probe_timeouts(
    client_timeout: float,
    timeout: object,
    connect_timeout: object,
    *,
    code: str,
) -> tuple[float, float]:
    for value in (timeout, connect_timeout):
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value <= 0
        ):
            raise A6AutoError(code)
    total = min(float(client_timeout), float(timeout))
    return total, min(total, float(connect_timeout))


def _provider_probe_transport_error(
    code: str,
    protocol: str,
    exc: Exception,
    *,
    response: object,
    request_id: str,
    started: float,
) -> A6ProbeTransportError:
    raw_detail = _BEARER_SECRET.sub(r"\1[REDACTED]", str(exc or ""))
    detail = _API_KEY_SECRET.sub("[REDACTED]", raw_detail).strip()[:512]
    lowered = detail.casefold()
    status_value = getattr(response, "status_code", None)
    if status_value is None:
        exception_response = getattr(exc, "response", None)
        status_value = getattr(exception_response, "status_code", None)
    try:
        http_status = int(status_value) if status_value is not None else None
    except (TypeError, ValueError):
        http_status = None
    raw_curl_code = getattr(exc, "code", None)
    if raw_curl_code is None and getattr(exc, "args", ()):
        first_arg = exc.args[0]
        raw_curl_code = first_arg if isinstance(first_arg, int) else None
    try:
        curl_code = int(raw_curl_code) if raw_curl_code is not None else None
    except (TypeError, ValueError):
        curl_code = None
    if http_status is not None:
        stage = "response_read"
    elif "resolve" in lowered or "dns" in lowered:
        stage = "dns"
    elif any(value in lowered for value in ("ssl", "tls", "certificate")):
        stage = "tls_handshake"
    elif any(value in lowered for value in ("connect", "connection")):
        stage = "tcp_connect"
    elif "timed out" in lowered or "timeout" in lowered:
        stage = "pre_http_timeout"
    else:
        stage = "transport"
    diagnostics: dict[str, Any] = {
        "protocol": protocol,
        "request_id": request_id,
        "stage": stage,
        "curl_code": curl_code,
        "http_status": http_status,
        "elapsed_ms": max(0, round((time.monotonic() - started) * 1000)),
        "exception_type": type(exc).__name__[:128],
    }
    if detail:
        diagnostics["detail"] = detail
    return A6ProbeTransportError(code, diagnostics)


def _bounded_stream_json(response: Any, *, max_bytes: int = 65_536) -> object:
    iterator = getattr(response, "iter_content", None)
    if not callable(iterator):
        raise A6AutoError("provider_probe_response_invalid")
    payload = bytearray()
    try:
        for chunk in iterator():
            if isinstance(chunk, str):
                raw = chunk.encode()
            elif isinstance(chunk, (bytes, bytearray, memoryview)):
                raw = bytes(chunk)
            else:
                raise A6AutoError("provider_probe_response_invalid")
            payload.extend(raw)
            if len(payload) > max_bytes:
                raise A6AutoError("provider_probe_response_invalid")
        return json.loads(payload)
    except A6AutoError:
        raise
    except Exception:
        raise A6AutoError("provider_probe_response_invalid") from None


def _normalize_models(values: list[Any]) -> list[str]:
    models: list[str] = []
    seen: set[str] = set()
    for item in values:
        model = (
            str(item.get("id") or "").strip() if isinstance(item, Mapping) else str(item).strip()
        )
        if model and model not in seen:
            models.append(model)
            seen.add(model)
    if not models or len(models) > 10000:
        raise A6AutoError("upstream_models_unavailable")
    return models


def _document_has_code(document: Mapping[str, Any], expected: str) -> bool:
    def matches(value: Any) -> bool:
        if isinstance(value, Mapping):
            return any(
                str(nested).strip().casefold() == expected.casefold()
                for key, nested in value.items()
                if str(key).casefold() in {"code", "error_code"}
            ) or any(matches(nested) for nested in value.values())
        if isinstance(value, (list, tuple)):
            return any(matches(nested) for nested in value)
        return False

    return matches(document)


def _provider_error_code(document: Mapping[str, Any]) -> str | None:
    """Extract only a bounded machine code; response messages stay out of logs."""

    def normalized(value: Any) -> str | None:
        candidate = str(value or "").strip().casefold()
        if not candidate or len(candidate) > 128:
            return None
        return candidate if re.fullmatch(r"[a-z0-9][a-z0-9_.-]*", candidate) else None

    def find(value: Any) -> str | None:
        if isinstance(value, Mapping):
            for key in ("code", "error_code"):
                result = normalized(value.get(key))
                if result is not None:
                    return result
            scalar_error = value.get("error")
            if not isinstance(scalar_error, (Mapping, list, tuple)):
                result = normalized(scalar_error)
                if result is not None:
                    return result
            for nested in value.values():
                result = find(nested)
                if result is not None:
                    return result
        elif isinstance(value, (list, tuple)):
            for nested in value:
                result = find(nested)
                if result is not None:
                    return result
        return None

    return find(document)


def _auto_pool_ratios(
    min_ratio: Decimal | str | int,
    max_ratio: Decimal | str | int,
) -> tuple[str, str]:
    try:
        minimum = Decimal(str(min_ratio))
        maximum = Decimal(str(max_ratio))
    except (InvalidOperation, ValueError):
        raise A6AutoError("marketplace_auto_pool_request_invalid") from None
    if (
        not minimum.is_finite()
        or not maximum.is_finite()
        or minimum < 0
        or maximum <= 0
        or maximum < minimum
    ):
        raise A6AutoError("marketplace_auto_pool_request_invalid")
    return format(minimum, "f"), format(maximum, "f")


def _marketplace_mutation_data(
    status: int,
    document: Mapping[str, Any],
    *,
    code: str,
    operation: str,
) -> dict[str, Any]:
    if status != 200 or document.get("success") is not True:
        raise A6MutationError(
            code,
            operation=operation,
            status_code=status,
            provider_error_code=_provider_error_code(document),
        )
    data = document.get("data")
    if not isinstance(data, Mapping):
        raise A6AutoError(code)
    return dict(data)


def _mask_withdraw_address(address: str) -> str:
    value = str(address)
    if len(value) <= 10:
        return "***"
    return f"{value[:6]}...{value[-4:]}"


def _mapping_data(
    status: int,
    document: Mapping[str, Any],
    error: str,
) -> dict[str, Any]:
    data = document.get("data")
    if status != 200 or document.get("success") is not True or not isinstance(data, Mapping):
        raise _read_error(status, error)
    return dict(data)


def _mutation_data(
    status: int,
    document: Mapping[str, Any],
    error: str,
) -> dict[str, Any]:
    if status != 200 or document.get("success") is not True:
        raise A6AutoError(error)
    data = document.get("data")
    if data is None:
        return {}
    if isinstance(data, Mapping):
        return dict(data)
    if isinstance(data, list) and all(isinstance(item, Mapping) for item in data):
        return {"items": [dict(item) for item in data]}
    raise A6AutoError(error)


def _page_data(
    status: int,
    document: Mapping[str, Any],
    error: str,
    *,
    allow_null_items: bool = False,
) -> dict[str, Any]:
    data = document.get("data")
    if status != 200 or document.get("success") is not True or not isinstance(data, Mapping):
        raise _read_error(status, error)
    items = data.get("items")
    if items is None and allow_null_items:
        items = []
    if not isinstance(items, list):
        raise A6AutoError(error)
    result = dict(data)
    result["items"] = [dict(item) for item in items if isinstance(item, Mapping)]
    return result


def _read_error(status: int, error: str) -> A6AutoError:
    if status == 429 or 500 <= status <= 599:
        return A6HttpError(error, status_code=status)
    return A6AutoError(error)


def _positive_integer(value: Any) -> int | None:
    if value in (None, "") or isinstance(value, bool):
        return None
    try:
        result = int(value)
    except (TypeError, ValueError):
        return None
    return result if result > 0 else None


def _supplier_channel(item: Mapping[str, Any]) -> dict[str, Any] | None:
    """Normalize both legacy flat rows and current binding/channel/request rows."""
    channel = item.get("channel")
    binding = item.get("binding")
    request = item.get("request")
    if not any(isinstance(value, Mapping) for value in (channel, binding, request)):
        allowed = {
            "id",
            "binding_id",
            "supplier_id",
            "name",
            "nickname",
            "channel_type",
            "status",
            "availability",
            "models",
            "description",
            "base_url",
            "api_key_last4",
            "has_api_key",
        }
        return {key: item[key] for key in allowed if key in item}
    channel = channel if isinstance(channel, Mapping) else {}
    binding = binding if isinstance(binding, Mapping) else {}
    request = request if isinstance(request, Mapping) else {}
    channel_id = channel.get("id") or binding.get("channel_id") or request.get(
        "native_channel_id"
    )
    if isinstance(channel_id, bool):
        return None
    try:
        channel_id = int(channel_id)
    except (TypeError, ValueError):
        return None
    if channel_id < 1:
        return None
    result: dict[str, Any] = {
        "id": channel_id,
        "supplier_id": binding.get("supplier_id") or request.get("supplier_id"),
        "name": channel.get("name") or request.get("name") or "",
        "nickname": request.get("nickname") or channel.get("name") or "",
        "channel_type": request.get("channel_type") or channel.get("type"),
        "status": (
            channel.get("status")
            if channel.get("status") is not None
            else request.get("status")
        ),
        "models": channel.get("models") or request.get("models") or "",
        "description": request.get("description") or "",
    }
    binding_id = binding.get("id")
    if not isinstance(binding_id, bool):
        try:
            binding_id = int(binding_id)
        except (TypeError, ValueError):
            binding_id = 0
        if binding_id > 0:
            result["binding_id"] = binding_id
    if binding.get("disabled") is not None:
        result["availability"] = 0 if bool(binding.get("disabled")) else 1
    for key in ("base_url", "api_key_last4", "has_api_key"):
        for source in (channel, request, binding):
            if source.get(key) is not None:
                result[key] = source[key]
                break
    if "has_api_key" not in result:
        result["has_api_key"] = bool(str(result.get("api_key_last4") or "").strip())
    return result
