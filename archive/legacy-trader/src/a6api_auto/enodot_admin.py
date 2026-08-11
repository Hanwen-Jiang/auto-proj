from __future__ import annotations

import hashlib
import math
import re
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import quote, urljoin

from .errors import A6AutoError
from .storage import read_private_json, write_private_json
from .trade_errors import ProviderErrorDecision, classify_provider_error

UTC = timezone.utc
_REDACTED = "[REDACTED]"
_SECRET_FIELDS = {
    "access_token",
    "admin_key",
    "api_key",
    "authorization",
    "cookie",
    "id_token",
    "key",
    "password",
    "refresh_token",
    "secret",
    "session",
    "token",
}
_MANAGED_SELLER_UID = re.compile(r"^[a-z0-9][a-z0-9:._-]{0,255}$")
_IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_MANAGED_SELLER_INITIAL_WORKING_CREDIT_USD = Decimal("999999999999.00000000")
_USD_QUANTUM = Decimal("0.00000001")
_MAX_ABSOLUTE_BALANCE_USD = Decimal("1000000000000.00000000")

# accounts.extra 中的 profit-gate 键（与面板 Go 侧
# AccountProfitRequiredExtraKey / AccountOAuthAutoCostExtraKey 一致）。
ACCOUNT_PROFIT_REQUIRED_EXTRA_KEY = "profit_required"
ACCOUNT_OAUTH_AUTO_COST_EXTRA_KEY = "oauth_auto_cost"
_MAX_PROFIT_REQUIRED = 10.0
_MANAGED_SELLER_CAPABILITY = {
    "capability": "a6_trader_managed_sellers",
    "contract_version": "1.0",
    "create_method": "PUT",
    "create_secret_delivery": "first_response_only",
    "replay_returns_api_key": False,
    "read_returns_api_key": False,
    "delta_min": 0.0,
    "delta_max": 0.5,
    "rate_multiplier_base": 1.0,
}


class EnodotAdminError(A6AutoError):
    """Stable Admin API error containing no provider response or credential data."""

    def __init__(self, code: str, *, status: int | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.status = status

    def __repr__(self) -> str:
        return f"{type(self).__name__}(code={self.code!r}, status={self.status!r})"


@dataclass(frozen=True)
class AccountReconcileResult:
    action: str
    applied: bool
    account_id: int | None
    changed_fields: tuple[str, ...] = ()
    account: Mapping[str, Any] | None = field(default=None, repr=False, compare=False)


@dataclass(frozen=True)
class ShortCooldownResult:
    account_id: int
    resume_at: datetime
    account: Mapping[str, Any] | None = field(default=None, repr=False, compare=False)


class EnodotAdminClient:
    """Small, read-first client for the Enodot Admin API.

    Construction only validates local configuration. Network reads and every mutation are explicit
    method calls.
    """

    def __init__(
        self,
        base_url: str,
        admin_key: str = "",
        *,
        token_file: Path | None = None,
        proxy_url: str = "",
        session: Any = None,
        timeout: float = 30,
    ) -> None:
        if bool(admin_key) == bool(token_file):
            raise EnodotAdminError("enodot_admin_credential_required")
        if not str(base_url).strip():
            raise EnodotAdminError("enodot_admin_base_url_invalid")
        if timeout <= 0:
            raise EnodotAdminError("enodot_admin_timeout_invalid")

        base = str(base_url).strip().rstrip("/")
        self.api_root = base if base.endswith("/api/v1") else f"{base}/api/v1"
        self._admin_key = str(admin_key)
        self._token_file = token_file.expanduser().resolve() if token_file else None
        self._proxy_url = str(proxy_url).strip()
        self.timeout = float(timeout)
        self._request_deadline_override: ContextVar[float | None] = ContextVar(
            "enodot_admin_request_deadline_override",
            default=None,
        )

        self._session_factory: Callable[[], Any] | None = None
        if session is None:
            try:
                from curl_cffi import requests

                self._session_factory = requests.Session
                session = requests.Session()
            except Exception:
                raise EnodotAdminError("http_dependency_unavailable") from None
        self.session = session
        self._session_owner_thread_id = threading.get_ident()
        self._thread_local = threading.local()

    def _session(self) -> Any:
        if self._session_factory is None or threading.get_ident() == self._session_owner_thread_id:
            return self.session
        session = getattr(self._thread_local, "session", None)
        if session is None:
            session = self._session_factory()
            self._thread_local.session = session
        return session

    def __repr__(self) -> str:
        credential = "x-api-key" if self._admin_key else "token-file"
        return (
            f"{type(self).__name__}(api_root={self.api_root!r}, "
            f"credential={credential!r}, proxy_configured={bool(self._proxy_url)!r})"
        )

    def _request_timeout(self) -> float:
        deadline = self._request_deadline_override.get()
        if deadline is None:
            return self.timeout
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise EnodotAdminError("enodot_admin_request_deadline_exceeded")
        return min(self.timeout, remaining)

    @contextmanager
    def request_deadline(self, deadline: float) -> Iterator[None]:
        if (
            isinstance(deadline, bool)
            or not isinstance(deadline, (int, float))
            or not math.isfinite(float(deadline))
            or float(deadline) <= time.monotonic()
        ):
            raise EnodotAdminError("enodot_admin_timeout_invalid")
        current = self._request_deadline_override.get()
        token = self._request_deadline_override.set(
            float(deadline) if current is None else min(current, float(deadline))
        )
        try:
            yield
        finally:
            self._request_deadline_override.reset(token)

    @contextmanager
    def request_timeout(self, timeout: float) -> Iterator[None]:
        """Temporarily narrow request timeouts in the current execution context."""

        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(float(timeout))
            or float(timeout) <= 0
        ):
            raise EnodotAdminError("enodot_admin_timeout_invalid")
        with self.request_deadline(time.monotonic() + float(timeout)):
            yield

    def _url(self, path: str) -> str:
        return urljoin(f"{self.api_root}/", path.lstrip("/"))

    def _auth_headers(self) -> dict[str, str]:
        if self._admin_key:
            return {"x-api-key": self._admin_key}
        if self._token_file is None:
            raise EnodotAdminError("enodot_admin_credential_required")
        tokens = read_private_json(self._token_file)
        access_token = str(tokens.get("access_token") or "").strip()
        if not access_token:
            raise EnodotAdminError("enodot_admin_token_invalid")
        return {"Authorization": f"Bearer {access_token}"}

    def _refresh_tokens(self) -> None:
        if self._token_file is None:
            raise EnodotAdminError("enodot_admin_token_refresh_unavailable")
        current = read_private_json(self._token_file)
        refresh_token = str(current.get("refresh_token") or "").strip()
        if not refresh_token:
            raise EnodotAdminError("enodot_admin_token_invalid")
        options: dict[str, Any] = {
            "method": "POST",
            "url": self._url("auth/refresh"),
            "headers": {"Accept": "application/json", "Content-Type": "application/json"},
            "json": {"refresh_token": refresh_token},
            "timeout": self._request_timeout(),
            "allow_redirects": False,
            "impersonate": "chrome131",
        }
        if self._proxy_url:
            options["proxies"] = {"http": self._proxy_url, "https": self._proxy_url}
        try:
            response = self._session().request(**options)
            status = int(getattr(response, "status_code", 0))
            document = response.json()
        except Exception:
            raise EnodotAdminError("enodot_admin_token_refresh_failed") from None
        data = document.get("data") if isinstance(document, Mapping) else None
        if (
            status != 200
            or not isinstance(document, Mapping)
            or document.get("code") != 0
            or not isinstance(data, Mapping)
            or not data.get("access_token")
            or not data.get("refresh_token")
        ):
            raise EnodotAdminError(
                "enodot_admin_token_refresh_rejected",
                status=status,
            )
        try:
            expires_at = float(data.get("expires_at") or 0)
            if not expires_at and data.get("expires_in") is not None:
                expires_at = time.time() + float(data.get("expires_in") or 0)
        except (TypeError, ValueError):
            raise EnodotAdminError("enodot_admin_token_refresh_rejected", status=status) from None
        try:
            write_private_json(
                self._token_file,
                {
                    "schema_version": 1,
                    "access_token": str(data["access_token"]),
                    "refresh_token": str(data["refresh_token"]),
                    "expires_at": expires_at,
                },
            )
        except A6AutoError:
            raise EnodotAdminError("enodot_admin_token_write_failed") from None

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json_body: Mapping[str, Any] | None = None,
        idempotency_key: str = "",
    ) -> Any:
        if idempotency_key and not _IDEMPOTENCY_KEY.fullmatch(str(idempotency_key)):
            raise EnodotAdminError("enodot_idempotency_key_invalid")
        response = None
        for attempt in range(2):
            headers = {"Accept": "application/json", **self._auth_headers()}
            if idempotency_key:
                headers["Idempotency-Key"] = str(idempotency_key)
            options: dict[str, Any] = {
                "method": method.upper(),
                "url": self._url(path),
                "headers": headers,
                "timeout": self._request_timeout(),
                "allow_redirects": False,
                "impersonate": "chrome131",
            }
            if params:
                options["params"] = dict(params)
            if json_body is not None:
                headers["Content-Type"] = "application/json"
                options["json"] = dict(json_body)
            if self._proxy_url:
                options["proxies"] = {"http": self._proxy_url, "https": self._proxy_url}
            try:
                response = self._session().request(**options)
            except Exception:
                raise EnodotAdminError("enodot_admin_transport_failed") from None
            status = int(getattr(response, "status_code", 0))
            if status != 401 or self._token_file is None or attempt > 0:
                break
            self._refresh_tokens()

        status = int(getattr(response, "status_code", 0))
        if status == 204:
            return None
        try:
            document = response.json()
        except Exception:
            raise EnodotAdminError("enodot_admin_response_invalid", status=status) from None
        if not 200 <= status < 300:
            raise EnodotAdminError("enodot_admin_request_rejected", status=status)
        if not isinstance(document, Mapping):
            raise EnodotAdminError("enodot_admin_response_invalid", status=status)
        if "code" in document:
            if document.get("code") != 0:
                raise EnodotAdminError("enodot_admin_request_rejected", status=status)
            return document.get("data")
        return document

    def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json_body: Mapping[str, Any] | None = None,
    ) -> Any:
        """Call an Admin endpoint and recursively redact returned credential fields."""

        return _redact(self._request(method, path, params=params, json_body=json_body))

    def get_managed_seller_capability(self) -> dict[str, Any]:
        data = self._request("GET", "admin/capabilities/a6-trader-managed-sellers")
        return _validate_managed_seller_capability(data)

    def get_managed_seller(self, managed_uid: str) -> dict[str, Any] | None:
        uid, path = _managed_seller_path(managed_uid)
        try:
            data = self._request("GET", path)
        except EnodotAdminError as exc:
            if exc.status == 404 and exc.code == "enodot_admin_request_rejected":
                return None
            raise
        if data is None:
            return None
        result = _validate_managed_seller(data, expected_uid=uid)
        if result.get("api_key"):
            raise EnodotAdminError("enodot_managed_seller_response_invalid")
        result.pop("api_key", None)
        return result

    def create_managed_seller(
        self,
        *,
        managed_uid: str,
        group_id: int,
        delta: Any,
        idempotency_key: str,
    ) -> dict[str, Any]:
        uid, path = _managed_seller_path(managed_uid)
        group_id = _account_id(group_id)
        delta = _managed_seller_delta(delta)
        data = self._request(
            "PUT",
            path,
            json_body={"group_id": group_id, "delta": delta},
            idempotency_key=idempotency_key,
        )
        return _validate_managed_seller(
            data,
            expected_uid=uid,
            expected_group_id=group_id,
            expected_delta=delta,
        )

    def set_managed_seller_delta(
        self,
        managed_uid: str,
        delta: Any,
        *,
        idempotency_key: str,
    ) -> dict[str, Any]:
        uid, base_path = _managed_seller_path(managed_uid)
        delta = _managed_seller_delta(delta)
        data = self._request(
            "PATCH",
            f"{base_path}/api-key/delta",
            json_body={"delta": delta},
            idempotency_key=idempotency_key,
        )
        result = _validate_managed_seller(
            data,
            expected_uid=uid,
            expected_delta=delta,
        )
        if result.get("api_key"):
            raise EnodotAdminError("enodot_managed_seller_response_invalid")
        result.pop("api_key", None)
        return result

    def get_user(self, user_id: int) -> dict[str, Any]:
        """Read one Enodot user while retaining only a validated, redacted document."""

        user_id = _account_id(user_id)
        data = self._request("GET", f"admin/users/{user_id}")
        return _redact(_validate_admin_user(data, expected_user_id=user_id))

    def add_user_balance(
        self,
        user_id: int,
        amount: Any,
        *,
        idempotency_key: str,
        notes: str = "",
    ) -> dict[str, Any]:
        """Add USD credit through Enodot's audited Admin balance endpoint."""

        user_id = _account_id(user_id)
        normalized_amount = _usd_amount(
            amount,
            positive=True,
            error="enodot_user_balance_amount_invalid",
        )
        normalized_notes = str(notes or "").strip()
        if len(normalized_notes) > 1024:
            raise EnodotAdminError("enodot_user_balance_notes_invalid")
        data = self._request(
            "POST",
            f"admin/users/{user_id}/balance",
            json_body={
                "balance": float(normalized_amount),
                "operation": "add",
                "notes": normalized_notes,
            },
            idempotency_key=idempotency_key,
        )
        try:
            return _redact(_validate_admin_user(data, expected_user_id=user_id))
        except EnodotAdminError:
            # The balance endpoint may return an operation receipt instead of the User.
            # A same-user GET is the authoritative postcondition for the accepted write.
            return self.get_user(user_id)

    def _list_accounts_raw(
        self,
        *,
        page: int,
        page_size: int,
        filters: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        params = _page_params(page, page_size, filters)
        data = self._request("GET", "admin/accounts", params=params)
        return _validate_page(data, "enodot_accounts_response_invalid")

    def list_accounts(
        self,
        *,
        page: int = 1,
        page_size: int = 100,
        filters: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        return _redact(self._list_accounts_raw(page=page, page_size=page_size, filters=filters))

    def iter_accounts(
        self,
        *,
        page_size: int = 200,
        filters: Mapping[str, Any] | None = None,
    ) -> Iterator[dict[str, Any]]:
        page = 1
        while True:
            data = self._list_accounts_raw(page=page, page_size=page_size, filters=filters)
            for item in data["items"]:
                yield _redact(item)
            if page >= data["pages"]:
                return
            page += 1

    def _get_account_raw(self, account_id: int) -> dict[str, Any]:
        account_id = _account_id(account_id)
        data = self._request("GET", f"admin/accounts/{account_id}")
        return _validate_account(data)

    def get_account(self, account_id: int) -> dict[str, Any]:
        return _redact(self._get_account_raw(account_id))

    def get_account_usage(
        self,
        account_id: int,
        *,
        source: str = "active",
        force: bool = False,
    ) -> dict[str, Any]:
        account_id = _account_id(account_id)
        normalized_source = str(source or "").strip().casefold()
        if normalized_source not in {"active", "passive"}:
            raise EnodotAdminError("enodot_account_usage_source_invalid")
        if not isinstance(force, bool):
            raise EnodotAdminError("enodot_account_usage_force_invalid")
        params: dict[str, str] = {"source": normalized_source}
        if force:
            params["force"] = "true"
        data = self._request(
            "GET",
            f"admin/accounts/{account_id}/usage",
            params=params,
        )
        if not isinstance(data, Mapping):
            raise EnodotAdminError("enodot_account_usage_response_invalid")
        return _redact(dict(data))

    def _find_account_by_binding_uid_raw(
        self,
        binding_uid: str,
        *,
        page_size: int = 200,
    ) -> dict[str, Any] | None:
        binding_uid = _binding_uid(binding_uid)
        matches: list[dict[str, Any]] = []
        page = 1
        while True:
            data = self._list_accounts_raw(
                page=page,
                page_size=page_size,
                filters={"platform": "openai", "type": "apikey"},
            )
            for item in data["items"]:
                extra = item.get("extra")
                if isinstance(extra, Mapping) and extra.get("a6_binding_uid") == binding_uid:
                    matches.append(item)
            if page >= data["pages"]:
                break
            page += 1
        if len(matches) > 1:
            raise EnodotAdminError("enodot_account_binding_not_unique")
        return matches[0] if matches else None

    def find_account_by_binding_uid(
        self,
        binding_uid: str,
        *,
        page_size: int = 200,
    ) -> dict[str, Any] | None:
        account = self._find_account_by_binding_uid_raw(binding_uid, page_size=page_size)
        return _redact(account) if account is not None else None

    def _write_account(
        self,
        method: str,
        path: str,
        payload: Mapping[str, Any],
        *,
        idempotency_key: str = "",
    ) -> dict[str, Any]:
        data = self._request(
            method,
            path,
            json_body=payload,
            idempotency_key=idempotency_key,
        )
        return _validate_account(data)

    def create_account(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return _redact(self._write_account("POST", "admin/accounts", payload))

    def update_account(
        self,
        account_id: int,
        payload: Mapping[str, Any],
        *,
        idempotency_key: str = "",
    ) -> dict[str, Any]:
        account_id = _account_id(account_id)
        return _redact(
            self._write_account(
                "PUT",
                f"admin/accounts/{account_id}",
                payload,
                idempotency_key=idempotency_key,
            )
        )

    def create_a6_provider_account(self, **provider: Any) -> dict[str, Any]:
        payload = _a6_provider_payload(**provider)
        return self.create_account(payload)

    def update_a6_provider_account(self, account_id: int, **provider: Any) -> dict[str, Any]:
        payload = _a6_provider_payload(**provider)
        return self.update_account(account_id, payload)

    def reconcile_a6_provider_account(
        self,
        *,
        apply: bool = False,
        **provider: Any,
    ) -> AccountReconcileResult:
        """Plan an idempotent reconciliation, mutating only when ``apply=True``."""

        if not isinstance(apply, bool):
            raise EnodotAdminError("enodot_reconcile_apply_invalid")
        desired = _a6_provider_payload(**provider)
        binding_uid = str(desired["extra"]["a6_binding_uid"])
        current = self._find_account_by_binding_uid_raw(binding_uid)
        if current is None:
            if not apply:
                return AccountReconcileResult("create", False, None)
            created = self._write_account("POST", "admin/accounts", desired)
            return AccountReconcileResult(
                "create",
                True,
                _optional_account_id(created.get("id")),
                account=_redact(created),
            )

        account_id = _account_id(current.get("id"))
        changes = _provider_changes(current, desired)
        if not changes:
            return AccountReconcileResult(
                "noop",
                False,
                account_id,
                account=_redact(current),
            )
        if not apply:
            return AccountReconcileResult(
                "update",
                False,
                account_id,
                tuple(changes),
                account=_redact(current),
            )
        updated = self._write_account(
            "PUT",
            f"admin/accounts/{account_id}",
            {key: changes[key] for key in changes},
        )
        return AccountReconcileResult(
            "update",
            True,
            account_id,
            tuple(changes),
            account=_redact(updated),
        )

    def _list_usage_raw(
        self,
        *,
        page: int,
        page_size: int,
        filters: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        params = _page_params(page, page_size, filters)
        data = self._request("GET", "admin/usage", params=params)
        return _validate_page(data, "enodot_usage_response_invalid")

    def list_usage(
        self,
        *,
        page: int = 1,
        page_size: int = 100,
        filters: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        return _redact(self._list_usage_raw(page=page, page_size=page_size, filters=filters))

    def iter_usage(
        self,
        *,
        page_size: int = 200,
        filters: Mapping[str, Any] | None = None,
    ) -> Iterator[dict[str, Any]]:
        page = 1
        while True:
            data = self._list_usage_raw(page=page, page_size=page_size, filters=filters)
            for item in data["items"]:
                yield _redact(item)
            if page >= data["pages"]:
                return
            page += 1

    def get_usage_summary(
        self,
        filters: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        data = self._request("GET", "admin/usage/stats", params=filters)
        if not isinstance(data, Mapping):
            raise EnodotAdminError("enodot_usage_summary_response_invalid")
        return _redact(dict(data))

    def set_schedulable(
        self,
        account_id: int,
        schedulable: bool,
        *,
        idempotency_key: str = "",
    ) -> dict[str, Any]:
        account_id = _account_id(account_id)
        if not isinstance(schedulable, bool):
            raise EnodotAdminError("enodot_schedulable_invalid")
        data = self._request(
            "POST",
            f"admin/accounts/{account_id}/schedulable",
            json_body={"schedulable": bool(schedulable)},
            idempotency_key=idempotency_key,
        )
        return _redact(_validate_account(data))

    def start_short_cooldown(
        self,
        account_id: int,
        *,
        seconds: int = 60,
        now: datetime | None = None,
    ) -> ShortCooldownResult:
        account_id = _account_id(account_id)
        seconds = _bounded_int(
            seconds,
            minimum=1,
            maximum=3600,
            error="enodot_cooldown_seconds_invalid",
        )
        started_at = now or datetime.now(UTC)
        if started_at.tzinfo is None:
            raise EnodotAdminError("enodot_cooldown_time_invalid")
        account = self.set_schedulable(account_id, False)
        return ShortCooldownResult(
            account_id=account_id,
            resume_at=started_at + timedelta(seconds=seconds),
            account=account,
        )

    def end_short_cooldown(self, account_id: int) -> dict[str, Any]:
        return self.set_schedulable(account_id, True)

    def update_scheduling(
        self,
        account_id: int,
        *,
        priority: int | None = None,
        concurrency: int | None = None,
        idempotency_key: str = "",
    ) -> dict[str, Any]:
        payload: dict[str, int] = {}
        if priority is not None:
            payload["priority"] = _bounded_int(
                priority,
                minimum=1,
                maximum=32767,
                error="enodot_priority_invalid",
            )
        if concurrency is not None:
            payload["concurrency"] = _bounded_int(
                concurrency,
                minimum=1,
                error="enodot_concurrency_invalid",
            )
        if not payload:
            raise EnodotAdminError("enodot_scheduling_update_empty")
        return self.update_account(
            account_id,
            payload,
            idempotency_key=idempotency_key,
        )

    def update_priority(
        self,
        account_id: int,
        priority: int,
        *,
        idempotency_key: str = "",
    ) -> dict[str, Any]:
        return self.update_scheduling(
            account_id,
            priority=priority,
            idempotency_key=idempotency_key,
        )

    def update_concurrency(self, account_id: int, concurrency: int) -> dict[str, Any]:
        return self.update_scheduling(account_id, concurrency=concurrency)

    def reconcile_account_profit_gate(
        self,
        account_id: int,
        *,
        profit_required: Any,
        oauth_auto_cost: Any,
        apply: bool = False,
        idempotency_key: str = "",
    ) -> AccountReconcileResult:
        """Reconcile profit-gate params on a panel account (GET-merge-PUT-readback).

        ``profit_required`` 是账号的所需利润倍率（rate，如 0.92/0.44；0 表示不要求
        额外利润但绝不亏本）。``oauth_auto_cost`` 为 OAuth 账号实时成本倍率自动更新
        开关。只动 extra 中两个键，其余字段保持不变。
        """

        account_id = _account_id(account_id)
        profit_required = _profit_required(profit_required)
        if not isinstance(oauth_auto_cost, bool):
            raise EnodotAdminError("enodot_oauth_auto_cost_invalid")
        current = self._get_account_raw(account_id)
        current_extra = current.get("extra")
        current_extra = dict(current_extra) if isinstance(current_extra, Mapping) else {}
        desired_extra = dict(current_extra)
        desired_extra[ACCOUNT_PROFIT_REQUIRED_EXTRA_KEY] = profit_required
        desired_extra[ACCOUNT_OAUTH_AUTO_COST_EXTRA_KEY] = oauth_auto_cost

        changed: tuple[str, ...] = ()
        if current_extra.get(ACCOUNT_PROFIT_REQUIRED_EXTRA_KEY) != profit_required:
            changed += (ACCOUNT_PROFIT_REQUIRED_EXTRA_KEY,)
        if current_extra.get(ACCOUNT_OAUTH_AUTO_COST_EXTRA_KEY) != oauth_auto_cost:
            changed += (ACCOUNT_OAUTH_AUTO_COST_EXTRA_KEY,)
        if not changed:
            return AccountReconcileResult("noop", False, account_id, account=_redact(current))
        if not apply:
            return AccountReconcileResult(
                "update",
                False,
                account_id,
                changed,
                account=_redact(current),
            )
        updated = self._write_account(
            "PUT",
            f"admin/accounts/{account_id}",
            {"extra": desired_extra},
            idempotency_key=idempotency_key,
        )
        observed = self._get_account_raw(account_id)
        observed_extra = observed.get("extra")
        observed_extra = dict(observed_extra) if isinstance(observed_extra, Mapping) else {}
        if (
            observed_extra.get(ACCOUNT_PROFIT_REQUIRED_EXTRA_KEY) != profit_required
            or observed_extra.get(ACCOUNT_OAUTH_AUTO_COST_EXTRA_KEY) != oauth_auto_cost
        ):
            raise EnodotAdminError("enodot_account_profit_gate_readback_mismatch")
        return AccountReconcileResult(
            "update",
            True,
            account_id,
            changed,
            account=_redact(updated),
        )

    def get_group_rate_multipliers(self, group_id: int) -> list[dict[str, Any]]:
        group_id = _account_id(group_id)
        data = self._request("GET", f"admin/groups/{group_id}/rate-multipliers")
        return _validate_rate_multiplier_entries(data)

    def merge_group_rate_multiplier(
        self,
        group_id: int,
        user_id: int,
        rate_multiplier: float,
        *,
        lock: Callable[[int], Any] | None,
        visible_to_user: bool = False,
        idempotency_key: str = "",
    ) -> list[dict[str, Any]]:
        """GET-merge-PUT-readback under a caller-provided cross-process group lock."""

        group_id = _account_id(group_id)
        user_id = _account_id(user_id)
        rate_multiplier = _positive_finite_float(
            rate_multiplier,
            "enodot_rate_multiplier_invalid",
        )
        if not isinstance(visible_to_user, bool):
            raise EnodotAdminError("enodot_rate_multiplier_visibility_invalid")
        if lock is None or not callable(lock):
            raise EnodotAdminError("enodot_rate_multiplier_lock_required")
        try:
            context = lock(group_id)
        except Exception:
            raise EnodotAdminError("enodot_rate_multiplier_lock_failed") from None
        if not hasattr(context, "__enter__") or not hasattr(context, "__exit__"):
            raise EnodotAdminError("enodot_rate_multiplier_lock_invalid")
        try:
            with context:
                current = self.get_group_rate_multipliers(group_id)
                by_user = {int(item["user_id"]): item for item in current}
                by_user[user_id] = {
                    "user_id": user_id,
                    "rate_multiplier": rate_multiplier,
                    "visible_to_user": visible_to_user,
                }
                entries = [by_user[item_user_id] for item_user_id in sorted(by_user)]
                self._request(
                    "PUT",
                    f"admin/groups/{group_id}/rate-multipliers",
                    json_body={"entries": entries},
                    idempotency_key=idempotency_key,
                )
                observed = self.get_group_rate_multipliers(group_id)
        except EnodotAdminError:
            raise
        except Exception:
            raise EnodotAdminError("enodot_rate_multiplier_lock_failed") from None
        matched = [item for item in observed if int(item["user_id"]) == user_id]
        if len(matched) != 1 or (
            float(matched[0]["rate_multiplier"]) != rate_multiplier
            or bool(matched[0]["visible_to_user"]) != visible_to_user
        ):
            raise EnodotAdminError("enodot_rate_multiplier_readback_mismatch")
        return observed

    @staticmethod
    def postgres_group_rate_lock(connection_factory: Callable[[], Any]) -> Callable[[int], Any]:
        if not callable(connection_factory):
            raise EnodotAdminError("enodot_rate_multiplier_lock_invalid")

        @contextmanager
        def lock(group_id: int):
            connection = connection_factory()
            try:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT pg_advisory_lock(%s, %s)",
                        (0x41365452, _account_id(group_id)),
                    )
                try:
                    yield
                finally:
                    with connection.cursor() as cursor:
                        cursor.execute(
                            "SELECT pg_advisory_unlock(%s, %s)",
                            (0x41365452, _account_id(group_id)),
                        )
            except EnodotAdminError:
                raise
            except Exception:
                raise EnodotAdminError("enodot_rate_multiplier_lock_failed") from None
            finally:
                close = getattr(connection, "close", None)
                if callable(close):
                    close()

        return lock

    @staticmethod
    def classify_upstream_error(
        status: int,
        payload: Mapping[str, Any] | str | bytes | None,
    ) -> ProviderErrorDecision:
        return classify_provider_error(status, payload)


def _binding_uid(value: Any) -> str:
    uid = str(value or "").strip()
    if not uid or len(uid) > 256:
        raise EnodotAdminError("enodot_binding_uid_invalid")
    return uid


def _managed_seller_path(value: Any) -> tuple[str, str]:
    uid = str(value or "").strip()
    if not _MANAGED_SELLER_UID.fullmatch(uid):
        raise EnodotAdminError("enodot_managed_seller_uid_invalid")
    return uid, f"admin/managed-sellers/{quote(uid, safe='')}"


def _managed_seller_delta(value: Any) -> float:
    if isinstance(value, bool):
        raise EnodotAdminError("enodot_managed_seller_delta_invalid")
    try:
        delta = float(value)
    except (TypeError, ValueError):
        raise EnodotAdminError("enodot_managed_seller_delta_invalid") from None
    if not math.isfinite(delta) or not 0 <= delta <= 0.5:
        raise EnodotAdminError("enodot_managed_seller_delta_invalid")
    return round(delta, 6)


def _validate_managed_seller_capability(data: Any) -> dict[str, Any]:
    if not isinstance(data, Mapping):
        raise EnodotAdminError("enodot_managed_seller_capability_invalid")
    normalized = dict(data)
    try:
        normalized["delta_min"] = _managed_seller_delta(data["delta_min"])
        normalized["delta_max"] = _managed_seller_delta(data["delta_max"])
        normalized["rate_multiplier_base"] = float(data["rate_multiplier_base"])
    except (KeyError, TypeError, ValueError):
        raise EnodotAdminError("enodot_managed_seller_capability_invalid") from None
    if normalized != _MANAGED_SELLER_CAPABILITY:
        raise EnodotAdminError("enodot_managed_seller_capability_invalid")
    return normalized


def _validate_managed_seller(
    data: Any,
    *,
    expected_uid: str,
    expected_group_id: int | None = None,
    expected_delta: float | None = None,
) -> dict[str, Any]:
    if not isinstance(data, Mapping):
        raise EnodotAdminError("enodot_managed_seller_response_invalid")
    try:
        managed_uid = str(data["managed_uid"])
        user_id = _account_id(data["user_id"])
        api_key_id = _account_id(data["api_key_id"])
        group_id = _account_id(data["group_id"])
        delta = _managed_seller_delta(data["delta"])
        initial_working_credit_usd = _managed_seller_initial_working_credit(
            data["initial_working_credit_usd"]
        )
        status = str(data["status"]).strip()
    except (KeyError, TypeError, ValueError):
        raise EnodotAdminError("enodot_managed_seller_response_invalid") from None
    if (
        managed_uid != expected_uid
        or not status
        or len(status) > 64
        or (expected_group_id is not None and group_id != expected_group_id)
        or (expected_delta is not None and delta != expected_delta)
    ):
        raise EnodotAdminError("enodot_managed_seller_response_invalid")
    result: dict[str, Any] = {
        "managed_uid": managed_uid,
        "user_id": user_id,
        "api_key_id": api_key_id,
        "group_id": group_id,
        "delta": delta,
        "initial_working_credit_usd": initial_working_credit_usd,
        "status": status,
    }
    if "api_key" in data and data.get("api_key") is not None:
        api_key = str(data.get("api_key") or "").strip()
        if not api_key or len(api_key) > 1024:
            raise EnodotAdminError("enodot_managed_seller_response_invalid")
        result["api_key"] = api_key
    return result


def _managed_seller_initial_working_credit(value: Any) -> float:
    if isinstance(value, bool):
        raise EnodotAdminError("enodot_managed_seller_response_invalid")
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise EnodotAdminError("enodot_managed_seller_response_invalid") from None
    if not amount.is_finite() or amount != _MANAGED_SELLER_INITIAL_WORKING_CREDIT_USD:
        raise EnodotAdminError("enodot_managed_seller_response_invalid")
    return float(_MANAGED_SELLER_INITIAL_WORKING_CREDIT_USD)


def _usd_amount(value: Any, *, positive: bool, error: str) -> Decimal:
    if isinstance(value, bool):
        raise EnodotAdminError(error)
    try:
        amount = Decimal(str(value))
        normalized = amount.quantize(_USD_QUANTUM)
    except (InvalidOperation, TypeError, ValueError):
        raise EnodotAdminError(error) from None
    if (
        not amount.is_finite()
        or amount != normalized
        or abs(normalized) > _MAX_ABSOLUTE_BALANCE_USD
        or (positive and normalized <= 0)
    ):
        raise EnodotAdminError(error)
    return normalized


def _validate_admin_user(data: Any, *, expected_user_id: int) -> dict[str, Any]:
    if not isinstance(data, Mapping):
        raise EnodotAdminError("enodot_user_response_invalid")
    try:
        user_id = _account_id(data.get("id"))
        balance = _usd_amount(
            data.get("balance"),
            positive=False,
            error="enodot_user_response_invalid",
        )
    except EnodotAdminError:
        raise EnodotAdminError("enodot_user_response_invalid") from None
    if user_id != expected_user_id:
        raise EnodotAdminError("enodot_user_response_invalid")
    result = dict(data)
    result["id"] = user_id
    result["balance"] = float(balance)
    return result


def _account_id(value: Any) -> int:
    try:
        account_id = int(value)
    except (TypeError, ValueError):
        raise EnodotAdminError("enodot_account_id_invalid") from None
    if isinstance(value, bool) or account_id < 1:
        raise EnodotAdminError("enodot_account_id_invalid")
    return account_id


def _optional_account_id(value: Any) -> int | None:
    if value is None:
        return None
    return _account_id(value)


def _positive_finite_float(value: Any, error: str) -> float:
    if isinstance(value, bool):
        raise EnodotAdminError(error)
    try:
        result = float(value)
    except (TypeError, ValueError):
        raise EnodotAdminError(error) from None
    if not math.isfinite(result) or result <= 0:
        raise EnodotAdminError(error)
    return result


def _profit_required(value: Any) -> float:
    if isinstance(value, bool):
        raise EnodotAdminError("enodot_profit_required_invalid")
    try:
        result = float(value)
    except (TypeError, ValueError):
        raise EnodotAdminError("enodot_profit_required_invalid") from None
    if not math.isfinite(result) or result < 0 or result > _MAX_PROFIT_REQUIRED:
        raise EnodotAdminError("enodot_profit_required_invalid")
    return round(result, 6)


def _validate_rate_multiplier_entries(data: Any) -> list[dict[str, Any]]:
    if not isinstance(data, list):
        raise EnodotAdminError("enodot_rate_multipliers_response_invalid")
    entries: list[dict[str, Any]] = []
    seen: set[int] = set()
    for item in data:
        if not isinstance(item, Mapping):
            raise EnodotAdminError("enodot_rate_multipliers_response_invalid")
        user_id = _account_id(item.get("user_id"))
        if item.get("rate_multiplier") is None:
            continue
        if user_id in seen:
            raise EnodotAdminError("enodot_rate_multipliers_response_invalid")
        seen.add(user_id)
        entries.append(
            {
                "user_id": user_id,
                "rate_multiplier": _positive_finite_float(
                    item.get("rate_multiplier"),
                    "enodot_rate_multipliers_response_invalid",
                ),
                "visible_to_user": bool(item.get("visible_to_user", False)),
            }
        )
    return entries


def _page_params(
    page: int,
    page_size: int,
    filters: Mapping[str, Any] | None,
) -> dict[str, Any]:
    page = _bounded_int(page, minimum=1, error="enodot_page_invalid")
    page_size = _bounded_int(
        page_size,
        minimum=1,
        maximum=500,
        error="enodot_page_size_invalid",
    )
    params = dict(filters or {})
    params.update({"page": page, "page_size": page_size})
    return params


def _validate_page(data: Any, error: str) -> dict[str, Any]:
    if not isinstance(data, Mapping) or not isinstance(data.get("items"), list):
        raise EnodotAdminError(error)
    items = [dict(item) for item in data["items"] if isinstance(item, Mapping)]
    try:
        page = int(data.get("page") or 1)
        page_size = int(data.get("page_size") or len(items) or 1)
        pages_value = data.get("pages")
        total_value = data.get("total")
        pages = int(1 if pages_value is None else pages_value)
        total = int(len(items) if total_value is None else total_value)
    except (TypeError, ValueError):
        raise EnodotAdminError(error) from None
    if page < 1 or page_size < 1 or pages < 0 or total < 0:
        raise EnodotAdminError(error)
    return {
        "items": items,
        "page": page,
        "page_size": page_size,
        "pages": pages,
        "total": total,
    }


def _validate_account(data: Any) -> dict[str, Any]:
    if not isinstance(data, Mapping):
        raise EnodotAdminError("enodot_account_response_invalid")
    return dict(data)


def _a6_provider_payload(
    *,
    binding_uid: str,
    name: str,
    api_key: str,
    proxy_id: int | str | None,
    base_url: str = "https://a6.a6api.com/v1",
    model_mapping: Mapping[str, str] | None = None,
    pool_mode: bool = True,
    pool_mode_retry_count: int = 0,
    pool_mode_retry_status_codes: Sequence[int] = (),
    concurrency: int = 100,
    priority: int = 1,
    schedulable: bool = False,
    rate_multiplier: float = 1,
    extra: Mapping[str, Any] | None = None,
    group_ids: list[int] | tuple[int, ...] | None = None,
) -> dict[str, Any]:
    binding_uid = _binding_uid(binding_uid)
    name = str(name or "").strip()
    api_key = str(api_key or "").strip()
    base_url = str(base_url or "").strip().rstrip("/")
    if not name or len(name) > 256:
        raise EnodotAdminError("enodot_account_name_invalid")
    if not api_key:
        raise EnodotAdminError("enodot_provider_key_invalid")
    if not base_url:
        raise EnodotAdminError("enodot_provider_base_url_invalid")
    if not isinstance(schedulable, bool):
        raise EnodotAdminError("enodot_schedulable_invalid")
    if not isinstance(pool_mode, bool):
        raise EnodotAdminError("enodot_pool_mode_invalid")
    pool_mode_retry_count = _bounded_int(
        pool_mode_retry_count,
        minimum=0,
        maximum=10,
        error="enodot_pool_mode_retry_count_invalid",
    )
    if isinstance(pool_mode_retry_status_codes, (str, bytes)):
        raise EnodotAdminError("enodot_pool_mode_retry_status_codes_invalid")
    try:
        retry_status_codes = tuple(
            dict.fromkeys(int(value) for value in pool_mode_retry_status_codes)
        )
    except (TypeError, ValueError):
        raise EnodotAdminError("enodot_pool_mode_retry_status_codes_invalid") from None
    if any(code < 100 or code > 599 for code in retry_status_codes):
        raise EnodotAdminError("enodot_pool_mode_retry_status_codes_invalid")
    concurrency = _bounded_int(
        concurrency,
        minimum=1,
        error="enodot_concurrency_invalid",
    )
    priority = _bounded_int(
        priority,
        minimum=1,
        maximum=32767,
        error="enodot_priority_invalid",
    )
    if isinstance(rate_multiplier, bool):
        raise EnodotAdminError("enodot_rate_multiplier_invalid")
    try:
        rate_multiplier = float(rate_multiplier)
    except (TypeError, ValueError):
        raise EnodotAdminError("enodot_rate_multiplier_invalid") from None
    if not math.isfinite(rate_multiplier) or rate_multiplier <= 0:
        raise EnodotAdminError("enodot_rate_multiplier_invalid")

    desired_extra = dict(extra or {})
    desired_extra.update(
        {
            "managed_by": "a6-trader",
            "a6_provider": True,
            "a6_binding_uid": binding_uid,
            "a6_api_key_fingerprint": hashlib.sha256(api_key.encode()).hexdigest(),
        }
    )
    payload: dict[str, Any] = {
        "name": name,
        "platform": "openai",
        "type": "apikey",
        "credentials": {
            "api_key": api_key,
            "base_url": base_url,
            "model_mapping": dict(model_mapping or {}),
            "pool_mode": pool_mode,
            "pool_mode_retry_count": pool_mode_retry_count,
            "pool_mode_retry_status_codes": list(retry_status_codes),
        },
        "extra": desired_extra,
        "proxy_id": proxy_id,
        "concurrency": concurrency,
        "priority": priority,
        "schedulable": schedulable,
        "rate_multiplier": rate_multiplier,
    }
    if group_ids is not None:
        payload["group_ids"] = [_account_id(group_id) for group_id in group_ids]
    return payload


def _provider_changes(
    current: Mapping[str, Any],
    desired: Mapping[str, Any],
) -> dict[str, Any]:
    changes: dict[str, Any] = {}
    for key in (
        "name",
        "platform",
        "type",
        "proxy_id",
        "concurrency",
        "priority",
        "schedulable",
        "rate_multiplier",
    ):
        if current.get(key) != desired.get(key):
            # The update API uses proxy_id=0 (rather than JSON null) to clear an old proxy.
            changes[key] = 0 if key == "proxy_id" and desired[key] is None else desired[key]
    if "group_ids" in desired and list(current.get("group_ids") or []) != desired["group_ids"]:
        changes["group_ids"] = desired["group_ids"]

    current_extra = current.get("extra")
    current_extra = dict(current_extra) if isinstance(current_extra, Mapping) else {}
    desired_extra = dict(desired["extra"])
    if any(current_extra.get(key) != value for key, value in desired_extra.items()):
        changes["extra"] = {**current_extra, **desired_extra}

    current_credentials = current.get("credentials")
    current_credentials = current_credentials if isinstance(current_credentials, Mapping) else {}
    desired_credentials = desired["credentials"]
    credentials_changed = (
        current_extra.get("a6_api_key_fingerprint") != desired_extra["a6_api_key_fingerprint"]
        or str(current_credentials.get("base_url") or "").rstrip("/")
        != desired_credentials["base_url"]
        or dict(current_credentials.get("model_mapping") or {})
        != desired_credentials["model_mapping"]
        or current_credentials.get("pool_mode") is not desired_credentials["pool_mode"]
        or current_credentials.get("pool_mode_retry_count")
        != desired_credentials["pool_mode_retry_count"]
        or list(current_credentials.get("pool_mode_retry_status_codes") or [])
        != desired_credentials["pool_mode_retry_status_codes"]
    )
    if credentials_changed:
        changes["credentials"] = desired_credentials
    return changes


def _redact(value: Any, *, field_name: str = "") -> Any:
    if field_name.casefold() in _SECRET_FIELDS:
        return _REDACTED
    if isinstance(value, Mapping):
        return {str(key): _redact(item, field_name=str(key)) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact(item) for item in value)
    return value


def _bounded_int(
    value: Any,
    *,
    minimum: int,
    error: str,
    maximum: int | None = None,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise EnodotAdminError(error)
    if value < minimum or (maximum is not None and value > maximum):
        raise EnodotAdminError(error)
    return value
