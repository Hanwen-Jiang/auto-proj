from __future__ import annotations

import json
import logging
import os
import re
import secrets
import stat
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from decimal import ROUND_CEILING, Decimal
from pathlib import Path
from socketserver import ThreadingMixIn
from typing import Any, Protocol
from urllib.parse import parse_qs
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server

from .trade_market_traffic import TRAFFIC_WINDOWS

UTC = timezone.utc
WINDOWS = ("5m", "1h", "5h", "12h", "24h", "7d", "30d", "mtd", "all")
DEFAULT_WINDOW = "1h"

_STOCK_NAMES = ("user_balance", "withdrawable", "withdrawing")
_STOCK_FIELDS = ("start_micros", "current_micros", "change_micros", "min_micros", "max_micros")
_FLOW_FIELDS = (
    "earned_micros",
    "paid_micros",
    "transfer_in_micros",
    "transfer_in_count",
    "enodot_sales_micros",
    "enodot_sales_request_count",
    "confirmed_enodot_sales_micros",
    "confirmed_enodot_sales_request_count",
    "unattributed_enodot_sales_micros",
    "unattributed_enodot_sales_request_count",
    "external_enodot_revenue_micros",
    "external_enodot_request_count",
    "confirmed_external_enodot_revenue_micros",
    "confirmed_external_enodot_request_count",
    "unattributed_external_enodot_revenue_micros",
    "unattributed_external_enodot_request_count",
    "managed_internal_debit_micros",
    "managed_internal_request_count",
    "external_provider_cost_micros",
    "external_provider_event_count",
    "managed_internal_provider_cost_micros",
    "managed_internal_provider_event_count",
    "owner_internal_debit_micros",
    "owner_internal_request_count",
    "owner_internal_provider_cost_micros",
    "owner_internal_provider_event_count",
    "enodot_provider_cost_micros",
    "confirmed_enodot_provider_cost_micros",
    "unattributed_enodot_provider_cost_micros",
    "enodot_provider_event_count",
    "unattributed_enodot_provider_event_count",
    "provider_cost_micros",
    "confirmed_provider_cost_micros",
    "unattributed_provider_cost_micros",
    "provider_event_count",
    "unattributed_provider_event_count",
    "seller_income_micros",
    "external_earned_micros",
    "own_boost_earned_micros",
    "own_pin_earned_micros",
    "unclassified_earned_micros",
    "boost_gross_cost_micros",
    "boost_net_loss_micros",
    "probe_gross_cost_micros",
    "probe_recovery_micros",
    "probe_loss_micros",
    "pin_probe_cost_micros",
    "pin_probe_event_count",
    "withdrawal_fee_micros",
    "earning_event_count",
    "unclassified_earning_event_count",
    "boost_event_count",
    "pending_binding_count",
)
_MERCHANT_MONEY_FIELDS = (
    "user_balance_micros",
    "withdrawable_micros",
    "withdrawing_micros",
    "lifetime_earned_micros",
    "paid_micros",
    "today_earned_micros",
    "month_earned_micros",
    "earned_micros",
    "transfer_in_micros",
    "enodot_sales_micros",
    "confirmed_enodot_sales_micros",
    "unattributed_enodot_sales_micros",
    "external_enodot_revenue_micros",
    "confirmed_external_enodot_revenue_micros",
    "unattributed_external_enodot_revenue_micros",
    "managed_internal_debit_micros",
    "external_provider_cost_micros",
    "managed_internal_provider_cost_micros",
    "owner_internal_debit_micros",
    "owner_internal_provider_cost_micros",
    "enodot_provider_cost_micros",
    "confirmed_enodot_provider_cost_micros",
    "unattributed_enodot_provider_cost_micros",
    "provider_cost_micros",
    "confirmed_provider_cost_micros",
    "unattributed_provider_cost_micros",
    "seller_income_micros",
    "external_earned_micros",
    "own_boost_earned_micros",
    "own_pin_earned_micros",
    "unclassified_earned_micros",
    "boost_gross_cost_micros",
    "boost_net_loss_micros",
    "probe_gross_cost_micros",
    "probe_recovery_micros",
    "probe_loss_micros",
    "pin_probe_cost_micros",
    "withdrawal_fee_micros",
)
_MERCHANT_COUNT_FIELDS = (
    "transfer_in_count",
    "enodot_sales_request_count",
    "confirmed_enodot_sales_request_count",
    "unattributed_enodot_sales_request_count",
    "external_enodot_request_count",
    "confirmed_external_enodot_request_count",
    "unattributed_external_enodot_request_count",
    "managed_internal_request_count",
    "external_provider_event_count",
    "managed_internal_provider_event_count",
    "owner_internal_request_count",
    "owner_internal_provider_event_count",
    "enodot_provider_event_count",
    "unattributed_enodot_provider_event_count",
    "provider_event_count",
    "unattributed_provider_event_count",
    "pin_probe_event_count",
    "seller_channel_count",
    "managed_channel_count",
    "existing_unmanaged_channel_count",
    "listing_total_count",
    "active_listing_count",
    "managed_active_listing_count",
    "existing_unmanaged_listing_count",
    "existing_unmanaged_active_listing_count",
    "pending_listing_count",
    "down_listing_count",
    "managed_listing_count",
    "provider_token_count",
    "enodot_account_count",
)
_FRESHNESS_STATES = frozenset({"fresh", "delayed", "stale", "error", "unknown"})
_MERCHANT_STATES = frozenset({"active", "cooling", "error", "disabled", "pending", "unknown"})
_COMPONENT_STATES = frozenset({"healthy", "degraded", "expired", "error", "disabled", "unknown"})
_ROLES = frozenset({"seller", "provider", "observer"})
_LEDGER_STATES = frozenset({"confirmed", "partial", "pending", "unavailable", "not_applicable"})
_TRAFFIC_TRENDS = frozenset({"idle", "new", "up", "flat", "down"})
_DELTA_SAMPLE_STATES = frozenset({"no_sample", "routing_unverified", "matched_count", "partial"})
_MANAGED_POSITION_STATES = frozenset({"active", "blocked"})
_MANAGED_POSITION_BLOCK_REASONS = frozenset(
    {
        "merchant_status_not_supplier_submitted",
        "merchant_seller_role_missing",
        "binding_missing",
        "binding_not_verified",
        "listing_missing",
        "listing_channel_mismatch",
        "listing_model_mismatch",
        "listing_status_inactive",
        "listing_unavailable",
        "listing_hidden",
        "unknown",
    }
)
_WITHDRAW_ACCOUNT_STATES = frozenset(
    {"missing", "reserved", "creating", "create_unknown", "bound", "existing_unmanaged", "error"}
)
_CHANNEL_HEALTH_STATES = frozenset(
    {"cooling", "unavailable", "degraded", "unlabeled", "idle", "healthy", "unknown"}
)
_CHANNEL_FAILURE_RESPONSIBILITIES = frozenset({"client", "provider", "platform", "unknown"})
_PROVIDER_GENERATION_STATES = frozenset({"planned", "applying", "active", "failed", "superseded"})
_PROVIDER_PRICING_STATES = frozenset(
    {"planned", "applying", "confirmed", "active", "failed", "superseded"}
)
_PROVIDER_SLOT_STATES = frozenset(
    {
        "PROVISIONING",
        "DRAINING",
        "REPINNING",
        "PROBING",
        "READY",
        "REPAIRING",
        "QUARANTINED",
        "VACANT",
    }
)
_PROVIDER_ROUTE_MODES = frozenset({"fixed_pin", "auto_pool"})
_PROVIDER_INVENTORY_STATES = frozenset({"READY", "WARM_STANDBY", "VACANT", "REPAIRING", "RETIRED"})
_PROVIDER_LIQUIDITY_STATES = frozenset(
    {"FUNDED", "REFILLING", "UNFUNDED", "TRANSFER_UNKNOWN", "UNKNOWN"}
)
_PROVIDER_PUBLICATION_STATES = frozenset({"published", "degraded", "unpublished", "repairing"})
_PROVIDER_FAILURE_OWNERS = frozenset(
    {"user", "client_transport", "provider", "platform_path", "internal", "unknown"}
)
_PROVIDER_QUARANTINE_SCOPES = frozenset(
    {"channel", "token", "endpoint", "egress", "account", "region"}
)
_ECONOMIC_GENERATION_STATES = frozenset({"planned", "active", "failed", "superseded"})
_ECONOMIC_PROJECTION_STATES = frozenset({"plan_only", "projected", "withdrawn", "error"})
_ECONOMIC_SOURCE_KINDS = frozenset({"api", "account", "a6_gateway"})
_ECONOMIC_COST_MODES = frozenset({"declared_k", "weekly_amortized"})
_ECONOMIC_CONTRACT_STATES = frozenset({"active", "expired", "disabled", "superseded"})
_ECONOMIC_ELIGIBILITY_STATES = frozenset(
    {
        "DISCOVERED",
        "COST_UNKNOWN",
        "REVENUE_UNKNOWN",
        "UNHEALTHY",
        "UNPROFITABLE",
        "ELIGIBLE",
        "THROTTLED",
        "EXHAUSTED",
    }
)
_ECONOMIC_HEALTH_STATES = frozenset({"HEALTHY", "UNHEALTHY", "UNKNOWN"})
_ECONOMIC_CAPACITY_STATES = frozenset({"AVAILABLE", "THROTTLED", "EXHAUSTED", "UNKNOWN"})
_ECONOMIC_QUOTA_STATES = frozenset({"VALID", "INVALID", "CONFLICT"})
_GROWTH_ACTION_TYPES = frozenset({"listing_recycle", "callback_readiness", "callback_burst"})
_GROWTH_ACTION_STATES = frozenset({"running", "completed", "failed", "skipped"})
_RETIREMENT_STATES = frozenset(
    {
        "earning",
        "checking",
        "transfer_unknown",
        "transferred",
        "binding",
        "taking_down",
        "role_switch",
        "draining",
        "completed",
    }
)
_SAFE_CODE = re.compile(r"^[a-z0-9][a-z0-9_.:-]{0,63}$")
_SAFE_MODEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,95}$")
_SECRET_LIKE = re.compile(r"(?:^sk-|bearer\s|session=|api[_-]?key|token=)", re.IGNORECASE)

logger = logging.getLogger(__name__)


class CredentialAccessError(RuntimeError):
    pass


class TradeDashboardRepository(Protocol):
    """Read-only data source for the Phase 0 dashboard.

    Monetary values in this contract are integer USD micro-units. Implementations
    aggregate flow values over ``window`` and return point-in-time stock values
    with their window extrema and change.
    """

    def health(self) -> Mapping[str, Any]: ...

    def get_overview(self, window: str) -> Mapping[str, Any]: ...

    def get_merchants(self, window: str) -> Sequence[Mapping[str, Any]]: ...

    def get_market_traffic(self, window: str, model: str) -> Mapping[str, Any]: ...

    def get_provider_pool(self, model: str) -> Mapping[str, Any] | None: ...

    def get_provider_inventory(self, model: str) -> Mapping[str, Any] | None: ...

    def get_economic_priority1(self, model: str) -> Mapping[str, Any] | None: ...

    def get_merchant_session_binding(self, merchant_id: int) -> Mapping[str, Any] | None: ...


class TradeDashboardManager(Protocol):
    def add_account(self, username: object, password: object) -> Mapping[str, Any]: ...

    def get_inventory(self, merchant_id: int) -> Mapping[str, Any]: ...

    def update_channel(
        self, merchant_id: int, channel_id: int, patch: Mapping[str, Any]
    ) -> Mapping[str, Any]: ...

    def update_listing(
        self, merchant_id: int, listing_id: int, patch: Mapping[str, Any]
    ) -> Mapping[str, Any]: ...

    def request_retirement(self, merchant_id: int) -> Mapping[str, Any]: ...

    def update_model_profit(
        self, model: str, profit_percent: object
    ) -> Mapping[str, Any]: ...


class TradePublicationControl(Protocol):
    def request_reconcile(self, model: str, document: Mapping[str, Any]) -> Mapping[str, Any]: ...

    def report_attempt_failure(self, document: Mapping[str, Any]) -> Mapping[str, Any]: ...

    def begin_quarantine_probe(
        self, model: str, document: Mapping[str, Any]
    ) -> Mapping[str, Any]: ...

    def confirm_quarantine_recovery(
        self, model: str, document: Mapping[str, Any]
    ) -> Mapping[str, Any]: ...

    def render_metrics(self) -> str: ...


class SessionCredentialReader:
    """Read the credential allowlist from a tightly scoped private session directory."""

    _MAX_SESSION_BYTES = 128 * 1024

    def __init__(self, sessions_dir: Path) -> None:
        self.sessions_dir = sessions_dir.expanduser()
        self._validated_root()

    def read(
        self,
        binding: Mapping[str, Any],
        *,
        merchant_id: int,
    ) -> dict[str, str]:
        root = self._validated_root()
        session_ref = str(binding.get("session_ref") or "")
        basename = Path(session_ref).name
        if (
            not basename
            or basename in {".", ".."}
            or "/" in basename
            or "\\" in basename
            or "\x00" in basename
        ):
            raise CredentialAccessError("credential_session_ref_invalid")

        unresolved = root / basename
        try:
            path_info = unresolved.lstat()
            resolved = unresolved.resolve(strict=True)
        except OSError as exc:
            raise CredentialAccessError("credential_session_unavailable") from exc
        if stat.S_ISLNK(path_info.st_mode) or resolved.parent != root:
            raise CredentialAccessError("credential_session_path_invalid")
        if not stat.S_ISREG(path_info.st_mode):
            raise CredentialAccessError("credential_session_type_invalid")

        payload = self._read_private_json(resolved, expected_owner=root.stat().st_uid)
        self._validate_binding(payload, binding, merchant_id=merchant_id, session_ref=session_ref)
        credentials: dict[str, str] = {}
        for field in ("username", "email", "password"):
            value = payload.get(field)
            if not isinstance(value, str) or (field != "email" and not value) or len(value) > 4096:
                raise CredentialAccessError("credential_field_invalid")
            credentials[field] = value
        return credentials

    def _validated_root(self) -> Path:
        try:
            path_info = self.sessions_dir.lstat()
            root = self.sessions_dir.resolve(strict=True)
            root_info = root.stat()
        except OSError as exc:
            raise CredentialAccessError("credential_sessions_dir_unavailable") from exc
        if stat.S_ISLNK(path_info.st_mode) or not stat.S_ISDIR(root_info.st_mode):
            raise CredentialAccessError("credential_sessions_dir_invalid")
        if stat.S_IMODE(root_info.st_mode) != 0o700 or root_info.st_uid != os.geteuid():
            raise CredentialAccessError("credential_sessions_dir_permissions_invalid")
        return root

    def _read_private_json(self, path: Path, *, expected_owner: int) -> dict[str, Any]:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags)
            try:
                info = os.fstat(descriptor)
                if (
                    not stat.S_ISREG(info.st_mode)
                    or stat.S_IMODE(info.st_mode) != 0o600
                    or info.st_uid != expected_owner
                ):
                    raise CredentialAccessError("credential_session_permissions_invalid")
                chunks: list[bytes] = []
                remaining = self._MAX_SESSION_BYTES + 1
                while remaining:
                    chunk = os.read(descriptor, remaining)
                    if not chunk:
                        break
                    chunks.append(chunk)
                    remaining -= len(chunk)
                raw = b"".join(chunks)
            finally:
                os.close(descriptor)
        except CredentialAccessError:
            raise
        except OSError as exc:
            raise CredentialAccessError("credential_session_unavailable") from exc
        if len(raw) > self._MAX_SESSION_BYTES:
            raise CredentialAccessError("credential_session_too_large")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CredentialAccessError("credential_session_invalid") from exc
        schema_version = payload.get("schema_version") if isinstance(payload, dict) else None
        if not isinstance(payload, dict) or isinstance(schema_version, bool) or schema_version != 1:
            raise CredentialAccessError("credential_session_invalid")
        return payload

    @staticmethod
    def _validate_binding(
        payload: Mapping[str, Any],
        binding: Mapping[str, Any],
        *,
        merchant_id: int,
        session_ref: str,
    ) -> None:
        from .trade_db import _session_fingerprint

        if _strict_optional_integer(binding.get("merchant_id")) != merchant_id:
            raise CredentialAccessError("credential_binding_mismatch")
        for field in ("a6_user_id", "supplier_id"):
            if _strict_optional_integer(binding.get(field)) != _strict_optional_integer(
                payload.get("user_id" if field == "a6_user_id" else field)
            ):
                raise CredentialAccessError("credential_binding_mismatch")
        expected_fingerprint = str(binding.get("session_fingerprint") or "")
        actual_fingerprint = _session_fingerprint(session_ref, payload)
        if not re.fullmatch(r"[0-9a-f]{64}", expected_fingerprint) or not secrets.compare_digest(
            expected_fingerprint,
            actual_fingerprint,
        ):
            raise CredentialAccessError("credential_binding_mismatch")


class ThreadingWSGIServer(ThreadingMixIn, WSGIServer):
    daemon_threads = True


class TradeDashboardApplication:
    def __init__(
        self,
        repository: TradeDashboardRepository,
        *,
        html_path: Path | None = None,
        sessions_dir: Path | None = None,
        manager: TradeDashboardManager | None = None,
        publication_control: TradePublicationControl | None = None,
        internal_api_token: str | None = None,
    ) -> None:
        self.repository = repository
        self.manager = manager
        self.publication_control = publication_control
        self._internal_api_token = str(internal_api_token or "").strip()
        if self._internal_api_token and not 32 <= len(self._internal_api_token) <= 4096:
            raise ValueError("internal_api_token_invalid")
        self._credential_reader = (
            SessionCredentialReader(sessions_dir) if sessions_dir is not None else None
        )
        path = html_path or Path(__file__).with_name("data") / "trade_dashboard.html"
        self._html = path.read_bytes()

    def __call__(self, environ: Mapping[str, Any], start_response: Any) -> list[bytes]:
        method = str(environ.get("REQUEST_METHOD") or "GET").upper()
        path = str(environ.get("PATH_INFO") or "/")

        if method != "GET":
            return self._mutation(environ, start_response, method, path)

        if path == "/":
            return self._respond(
                start_response,
                "200 OK",
                self._html,
                "text/html; charset=utf-8",
            )

        if path == "/healthz":
            return self._health(start_response)

        if path == "/metrics":
            return self._publication_metrics(environ, start_response)

        provider_pool_model = self._provider_pool_model(path)
        if provider_pool_model is not None:
            return self._provider_pool(start_response, provider_pool_model)

        provider_inventory_model = self._provider_inventory_model(path)
        if provider_inventory_model is not None:
            return self._provider_inventory(start_response, provider_inventory_model)

        economic_priority1_model = self._economic_priority1_model(path)
        if economic_priority1_model is not None:
            return self._economic_priority1(start_response, economic_priority1_model)

        merchant_id = self._credential_merchant_id(path)
        if merchant_id is not None:
            return self._credentials(start_response, merchant_id)

        inventory_merchant_id = self._inventory_merchant_id(path)
        if inventory_merchant_id is not None:
            return self._inventory(start_response, inventory_merchant_id)

        if path == "/api/market-traffic":
            return self._market_traffic(environ, start_response)

        if path not in {"/api/overview", "/api/merchants"}:
            return self._respond(
                start_response,
                "404 Not Found",
                self._json_bytes({"error": {"code": "not_found"}}),
                "application/json; charset=utf-8",
            )

        window = self._parse_window(str(environ.get("QUERY_STRING") or ""))
        if window is None:
            return self._respond(
                start_response,
                "400 Bad Request",
                self._json_bytes(
                    {
                        "error": {
                            "code": "invalid_window",
                            "allowed": list(WINDOWS),
                        }
                    }
                ),
                "application/json; charset=utf-8",
            )

        try:
            if path == "/api/overview":
                raw = self.repository.get_overview(window)
                payload = _sanitize_overview(raw, window)
            else:
                raw = self.repository.get_merchants(window)
                payload = _sanitize_merchants(raw, window)
        except Exception as exc:
            logger.error(
                "trade_dashboard_repository_failed route=%s error_type=%s",
                path,
                type(exc).__name__,
            )
            return self._respond(
                start_response,
                "503 Service Unavailable",
                self._json_bytes({"error": {"code": "repository_unavailable"}}),
                "application/json; charset=utf-8",
            )

        return self._respond(
            start_response,
            "200 OK",
            self._json_bytes(payload),
            "application/json; charset=utf-8",
        )

    def _provider_pool(self, start_response: Any, model: str) -> list[bytes]:
        try:
            raw = self.repository.get_provider_pool(model)
            if raw is None:
                return self._private_json_error(
                    start_response,
                    "404 Not Found",
                    "provider_pool_not_found",
                )
            payload = _sanitize_provider_pool(raw, model)
        except Exception as exc:
            logger.error(
                "trade_dashboard_provider_pool_failed model=%s error_type=%s",
                model,
                type(exc).__name__,
            )
            return self._private_json_error(
                start_response,
                "503 Service Unavailable",
                "provider_pool_unavailable",
            )
        return self._respond(
            start_response,
            "200 OK",
            self._json_bytes(payload),
            "application/json; charset=utf-8",
            private=True,
        )

    def _provider_inventory(self, start_response: Any, model: str) -> list[bytes]:
        try:
            raw = self.repository.get_provider_inventory(model)
            if raw is None:
                return self._private_json_error(
                    start_response,
                    "404 Not Found",
                    "provider_inventory_not_found",
                )
            payload = _sanitize_provider_inventory(raw, model)
        except Exception as exc:
            logger.error(
                "trade_dashboard_provider_inventory_failed model=%s error_type=%s",
                model,
                type(exc).__name__,
            )
            return self._private_json_error(
                start_response,
                "503 Service Unavailable",
                "provider_inventory_unavailable",
            )
        return self._respond(
            start_response,
            "200 OK",
            self._json_bytes(payload),
            "application/json; charset=utf-8",
            private=True,
        )

    def _economic_priority1(self, start_response: Any, model: str) -> list[bytes]:
        try:
            raw = self.repository.get_economic_priority1(model)
            if raw is None:
                return self._private_json_error(
                    start_response,
                    "404 Not Found",
                    "economic_priority1_not_found",
                )
            payload = _sanitize_economic_priority1(raw, model)
        except Exception as exc:
            logger.error(
                "trade_dashboard_economic_priority1_failed model=%s error_type=%s",
                model,
                type(exc).__name__,
            )
            return self._private_json_error(
                start_response,
                "503 Service Unavailable",
                "economic_priority1_unavailable",
            )
        return self._respond(
            start_response,
            "200 OK",
            self._json_bytes(payload),
            "application/json; charset=utf-8",
            private=True,
        )

    def _inventory(self, start_response: Any, merchant_id: int) -> list[bytes]:
        if self.manager is None:
            return self._private_json_error(
                start_response, "503 Service Unavailable", "management_unavailable"
            )
        try:
            payload = _sanitize_inventory(self.manager.get_inventory(merchant_id), merchant_id)
        except Exception as exc:
            logger.error("trade_dashboard_inventory_failed error_type=%s", type(exc).__name__)
            return self._operation_error(start_response, exc)
        return self._respond(
            start_response,
            "200 OK",
            self._json_bytes(payload),
            "application/json; charset=utf-8",
            private=True,
        )

    def _mutation(
        self,
        environ: Mapping[str, Any],
        start_response: Any,
        method: str,
        path: str,
    ) -> list[bytes]:
        internal_route = self._internal_mutation_route(method, path)
        if internal_route is not None:
            return self._internal_mutation(
                environ,
                start_response,
                operation=internal_route[0],
                model=internal_route[1],
                quarantine_id=internal_route[2],
            )
        route = self._mutation_route(method, path)
        if route is None:
            return self._respond(
                start_response,
                "405 Method Not Allowed",
                self._json_bytes({"error": {"code": "method_not_allowed"}}),
                "application/json; charset=utf-8",
                (("Allow", "GET"),),
                private=True,
            )
        if self.manager is None:
            return self._private_json_error(
                start_response, "503 Service Unavailable", "management_unavailable"
            )
        if str(environ.get("HTTP_X_REQUESTED_WITH") or "") != "a6-trader-dashboard":
            return self._private_json_error(
                start_response, "403 Forbidden", "dashboard_action_header_required"
            )
        operation, merchant_id, resource_id, model = route
        try:
            payload = self._request_json(environ)
            if operation == "add_account":
                result = self.manager.add_account(payload.get("username"), payload.get("password"))
                status = "201 Created" if result.get("status") == "imported" else "200 OK"
            elif operation == "update_channel":
                result = self.manager.update_channel(merchant_id, resource_id, payload)
                status = "200 OK"
            elif operation == "update_listing":
                result = self.manager.update_listing(merchant_id, resource_id, payload)
                status = "200 OK"
            elif operation == "update_model_profit":
                if model is None:
                    raise ValueError("profit_setting_model_invalid")
                result = self.manager.update_model_profit(model, payload.get("profit_percent"))
                status = "200 OK"
            else:
                result = self.manager.request_retirement(merchant_id)
                status = "202 Accepted"
        except Exception as exc:
            logger.error(
                "trade_dashboard_mutation_failed route=%s error_type=%s",
                operation if route else "unknown",
                type(exc).__name__,
            )
            return self._operation_error(start_response, exc)
        return self._respond(
            start_response,
            status,
            self._json_bytes(dict(result)),
            "application/json; charset=utf-8",
            private=True,
        )

    def _internal_mutation(
        self,
        environ: Mapping[str, Any],
        start_response: Any,
        *,
        operation: str,
        model: str | None,
        quarantine_id: int | None,
    ) -> list[bytes]:
        if self.publication_control is None or not self._internal_api_token:
            return self._private_json_error(
                start_response, "503 Service Unavailable", "internal_control_unavailable"
            )
        if not self._internal_authorized(environ):
            return self._respond(
                start_response,
                "401 Unauthorized",
                self._json_bytes({"error": {"code": "internal_auth_required"}}),
                "application/json; charset=utf-8",
                (("WWW-Authenticate", 'Bearer realm="a6-trader-internal"'),),
                private=True,
            )
        try:
            payload = self._request_json(environ)
            if operation == "provider_attempt_failure":
                result = self.publication_control.report_attempt_failure(payload)
            elif operation == "publication_reconcile":
                if model is None:
                    raise ValueError("publication_model_invalid")
                result = self.publication_control.request_reconcile(model, payload)
            else:
                if model is None or quarantine_id is None:
                    raise ValueError("provider_quarantine_route_invalid")
                supplied_id = payload.get("quarantine_id")
                if supplied_id is not None and supplied_id != quarantine_id:
                    raise ValueError("provider_quarantine_path_body_mismatch")
                quarantine_payload = {**payload, "quarantine_id": quarantine_id}
                if operation == "provider_quarantine_probe":
                    result = self.publication_control.begin_quarantine_probe(
                        model, quarantine_payload
                    )
                else:
                    result = self.publication_control.confirm_quarantine_recovery(
                        model, quarantine_payload
                    )
        except Exception as exc:
            logger.error(
                "trade_dashboard_internal_control_failed operation=%s error_type=%s",
                operation,
                type(exc).__name__,
            )
            return self._internal_operation_error(start_response, exc)
        return self._respond(
            start_response,
            "202 Accepted",
            self._json_bytes(dict(result)),
            "application/json; charset=utf-8",
            private=True,
        )

    def _publication_metrics(
        self,
        environ: Mapping[str, Any],
        start_response: Any,
    ) -> list[bytes]:
        if self.publication_control is None or not self._internal_api_token:
            return self._private_json_error(
                start_response, "503 Service Unavailable", "publication_metrics_unavailable"
            )
        if not self._internal_authorized(environ):
            return self._respond(
                start_response,
                "401 Unauthorized",
                b"internal_auth_required\n",
                "text/plain; charset=utf-8",
                (("WWW-Authenticate", 'Bearer realm="a6-trader-internal"'),),
                private=True,
            )
        try:
            body = self.publication_control.render_metrics().encode("utf-8")
        except Exception as exc:
            logger.error(
                "trade_dashboard_publication_metrics_failed error_type=%s",
                type(exc).__name__,
            )
            return self._private_json_error(
                start_response, "503 Service Unavailable", "publication_metrics_unavailable"
            )
        return self._respond(
            start_response,
            "200 OK",
            body,
            "text/plain; version=0.0.4; charset=utf-8",
            private=True,
        )

    def _internal_authorized(self, environ: Mapping[str, Any]) -> bool:
        authorization = str(environ.get("HTTP_AUTHORIZATION") or "")
        scheme, separator, supplied = authorization.partition(" ")
        return (
            separator == " "
            and scheme.casefold() == "bearer"
            and bool(supplied)
            and secrets.compare_digest(supplied.strip(), self._internal_api_token)
        )

    @staticmethod
    def _internal_mutation_route(
        method: str, path: str
    ) -> tuple[str, str | None, int | None] | None:
        if method != "POST":
            return None
        if path == "/internal/provider-attempt-failures":
            return "provider_attempt_failure", None, None
        match = re.fullmatch(r"/internal/models/([^/]+)/publication/reconcile", path)
        if match is not None:
            model = _safe_model(match.group(1))
            return ("publication_reconcile", model, None) if model else None
        match = re.fullmatch(
            r"/internal/models/([^/]+)/provider-quarantines/([1-9][0-9]{0,18})/"
            r"(probe|recover)",
            path,
        )
        if match is None:
            return None
        model = _safe_model(match.group(1))
        if model is None:
            return None
        operation = (
            "provider_quarantine_probe"
            if match.group(3) == "probe"
            else "provider_quarantine_recover"
        )
        return operation, model, int(match.group(2))

    def _internal_operation_error(self, start_response: Any, exc: Exception) -> list[bytes]:
        code = str(getattr(exc, "code", None) or exc)
        if not _SAFE_CODE.fullmatch(code):
            code = "internal_control_failed"
        if code.endswith("_invalid") or code.startswith("request_"):
            status = "400 Bad Request"
        elif code == "provider_failure_attempt_conflict_or_envelope_missing" or code.endswith(
            ("_conflict", "_mismatch")
        ):
            status = "409 Conflict"
        elif code.endswith("_missing"):
            status = "404 Not Found"
        else:
            status = "503 Service Unavailable"
        return self._private_json_error(start_response, status, code)

    @staticmethod
    def _request_json(environ: Mapping[str, Any]) -> dict[str, Any]:
        content_type = str(environ.get("CONTENT_TYPE") or "").split(";", 1)[0].strip()
        if content_type != "application/json":
            raise ValueError("request_content_type_invalid")
        try:
            length = int(str(environ.get("CONTENT_LENGTH") or "0"))
        except ValueError as exc:
            raise ValueError("request_body_invalid") from exc
        if not 2 <= length <= 16 * 1024:
            raise ValueError("request_body_invalid")
        stream = environ.get("wsgi.input")
        if stream is None or not hasattr(stream, "read"):
            raise ValueError("request_body_invalid")
        raw = stream.read(length)
        if not isinstance(raw, bytes) or len(raw) != length:
            raise ValueError("request_body_invalid")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("request_body_invalid") from exc
        if not isinstance(payload, dict):
            raise ValueError("request_body_invalid")
        return payload

    @staticmethod
    def _mutation_route(method: str, path: str) -> tuple[str, int, int, str | None] | None:
        if method == "POST" and path == "/api/accounts":
            return "add_account", 0, 0, None
        if method == "PUT":
            match = re.fullmatch(r"/api/economic-priority1/([^/]+)/profit", path)
            if match is not None:
                model = _safe_model(match.group(1))
                if model:
                    return "update_model_profit", 0, 0, model
        patterns = (
            (
                "PATCH",
                "update_channel",
                r"/api/merchants/([1-9][0-9]{0,18})/channels/([1-9][0-9]{0,18})",
            ),
            (
                "PATCH",
                "update_listing",
                r"/api/merchants/([1-9][0-9]{0,18})/listings/([1-9][0-9]{0,18})",
            ),
            (
                "POST",
                "request_retirement",
                r"/api/merchants/([1-9][0-9]{0,18})/retirement",
            ),
        )
        for expected_method, operation, pattern in patterns:
            if method != expected_method:
                continue
            match = re.fullmatch(pattern, path)
            if match is None:
                continue
            merchant_id = int(match.group(1))
            resource_id = int(match.group(2)) if match.lastindex == 2 else 0
            if merchant_id <= 9_223_372_036_854_775_807 and (
                not resource_id or resource_id <= 9_223_372_036_854_775_807
            ):
                return operation, merchant_id, resource_id, None
        return None

    def _operation_error(self, start_response: Any, exc: Exception) -> list[bytes]:
        code = str(exc)
        if not _SAFE_CODE.fullmatch(code):
            code = "dashboard_operation_failed"
        if code in {
            "merchant_not_found",
            "supplier_channel_not_found",
            "supplier_listing_not_found",
        }:
            status = "404 Not Found"
        elif code in {
            "account_session_conflict",
            "proxy_user_conflict",
            "proxy_lease_conflict",
        }:
            status = "409 Conflict"
        elif code.endswith(("_invalid", "_required")) or code.startswith("request_"):
            status = "400 Bad Request"
        else:
            status = "503 Service Unavailable"
        return self._private_json_error(start_response, status, code)

    def _market_traffic(
        self,
        environ: Mapping[str, Any],
        start_response: Any,
    ) -> list[bytes]:
        query = parse_qs(str(environ.get("QUERY_STRING") or ""), keep_blank_values=True)
        window = str((query.get("window") or ["5m"])[0])
        model = _safe_model((query.get("model") or ["gpt-5.6-sol"])[0])
        if window not in TRAFFIC_WINDOWS or not model:
            return self._respond(
                start_response,
                "400 Bad Request",
                self._json_bytes(
                    {
                        "error": {
                            "code": "invalid_market_traffic_query",
                            "allowed_windows": list(TRAFFIC_WINDOWS),
                        }
                    }
                ),
                "application/json; charset=utf-8",
            )
        try:
            raw = self.repository.get_market_traffic(window, model)
            payload = _sanitize_market_traffic(raw, window, model)
        except Exception as exc:
            logger.error(
                "trade_dashboard_repository_failed route=/api/market-traffic error_type=%s",
                type(exc).__name__,
            )
            return self._respond(
                start_response,
                "503 Service Unavailable",
                self._json_bytes({"error": {"code": "repository_unavailable"}}),
                "application/json; charset=utf-8",
            )
        return self._respond(
            start_response,
            "200 OK",
            self._json_bytes(payload),
            "application/json; charset=utf-8",
        )

    def _credentials(self, start_response: Any, merchant_id: int) -> list[bytes]:
        try:
            binding = self.repository.get_merchant_session_binding(merchant_id)
        except Exception as exc:
            logger.error(
                "trade_dashboard_credentials_repository_failed error_type=%s",
                type(exc).__name__,
            )
            return self._private_json_error(
                start_response,
                "503 Service Unavailable",
                "credentials_unavailable",
            )
        if binding is None:
            return self._private_json_error(start_response, "404 Not Found", "merchant_not_found")
        if self._credential_reader is None:
            return self._private_json_error(
                start_response,
                "503 Service Unavailable",
                "credentials_unavailable",
            )
        try:
            credentials = self._credential_reader.read(binding, merchant_id=merchant_id)
        except Exception as exc:
            logger.error(
                "trade_dashboard_credentials_read_failed error_type=%s",
                type(exc).__name__,
            )
            return self._private_json_error(
                start_response,
                "503 Service Unavailable",
                "credentials_unavailable",
            )
        return self._respond(
            start_response,
            "200 OK",
            self._json_bytes({"merchant_id": str(merchant_id), **credentials}),
            "application/json; charset=utf-8",
            private=True,
        )

    def _private_json_error(
        self,
        start_response: Any,
        status: str,
        code: str,
    ) -> list[bytes]:
        return self._respond(
            start_response,
            status,
            self._json_bytes({"error": {"code": code}}),
            "application/json; charset=utf-8",
            private=True,
        )

    @staticmethod
    def _credential_merchant_id(path: str) -> int | None:
        match = re.fullmatch(r"/api/merchants/([1-9][0-9]{0,18})/credentials", path)
        if match is None:
            return None
        merchant_id = int(match.group(1))
        return merchant_id if merchant_id <= 9_223_372_036_854_775_807 else None

    @staticmethod
    def _inventory_merchant_id(path: str) -> int | None:
        match = re.fullmatch(r"/api/merchants/([1-9][0-9]{0,18})/inventory", path)
        if match is None:
            return None
        merchant_id = int(match.group(1))
        return merchant_id if merchant_id <= 9_223_372_036_854_775_807 else None

    @staticmethod
    def _provider_pool_model(path: str) -> str | None:
        match = re.fullmatch(r"/internal/provider-pools/([^/]+)", path)
        if match is None:
            return None
        model = _safe_model(match.group(1))
        return model or None

    @staticmethod
    def _provider_inventory_model(path: str) -> str | None:
        match = re.fullmatch(r"/internal/provider-inventory/([^/]+)", path)
        if match is None:
            return None
        model = _safe_model(match.group(1))
        return model or None

    @staticmethod
    def _economic_priority1_model(path: str) -> str | None:
        match = re.fullmatch(r"/internal/economic-priority1/([^/]+)", path)
        if match is None:
            return None
        model = _safe_model(match.group(1))
        return model or None

    def _health(self, start_response: Any) -> list[bytes]:
        try:
            raw = self.repository.health()
            status = _enum(raw.get("status"), frozenset({"ok", "degraded"}), "degraded")
            payload = {
                "status": status,
                "checked_at": _timestamp(raw.get("checked_at")) or _utc_now(),
                "last_success_at": _timestamp(raw.get("last_success_at")),
            }
            http_status = "200 OK"
        except Exception as exc:
            logger.error("trade_dashboard_health_failed error_type=%s", type(exc).__name__)
            payload = {"status": "unavailable", "checked_at": _utc_now()}
            http_status = "503 Service Unavailable"
        return self._respond(
            start_response,
            http_status,
            self._json_bytes(payload),
            "application/json; charset=utf-8",
        )

    @staticmethod
    def _parse_window(query_string: str) -> str | None:
        try:
            values = parse_qs(query_string, keep_blank_values=True, max_num_fields=10)
        except ValueError:
            return None
        requested = values.get("window", [DEFAULT_WINDOW])[0]
        return requested if requested in WINDOWS else None

    @staticmethod
    def _json_bytes(payload: Mapping[str, Any]) -> bytes:
        return json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")

    @staticmethod
    def _respond(
        start_response: Any,
        status: str,
        body: bytes,
        content_type: str,
        extra_headers: tuple[tuple[str, str], ...] = (),
        *,
        private: bool = False,
    ) -> list[bytes]:
        headers = [
            ("Content-Type", content_type),
            ("Content-Length", str(len(body))),
            ("Cache-Control", "no-store, private" if private else "no-store"),
            ("Content-Security-Policy", _content_security_policy()),
            ("Referrer-Policy", "no-referrer"),
            ("X-Content-Type-Options", "nosniff"),
            ("X-Frame-Options", "DENY"),
            *extra_headers,
        ]
        if private:
            headers.append(("Pragma", "no-cache"))
        start_response(status, headers)
        return [body]


def create_dashboard_app(
    repository: TradeDashboardRepository,
    *,
    html_path: Path | None = None,
    sessions_dir: Path | None = None,
    manager: TradeDashboardManager | None = None,
    publication_control: TradePublicationControl | None = None,
    internal_api_token: str | None = None,
) -> TradeDashboardApplication:
    return TradeDashboardApplication(
        repository,
        html_path=html_path,
        sessions_dir=sessions_dir,
        manager=manager,
        publication_control=publication_control,
        internal_api_token=internal_api_token,
    )


def serve_dashboard(
    repository: TradeDashboardRepository,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    sessions_dir: Path,
    manager: TradeDashboardManager | None = None,
    publication_control: TradePublicationControl | None = None,
    internal_api_token: str | None = None,
) -> None:
    app = create_dashboard_app(
        repository,
        sessions_dir=sessions_dir,
        manager=manager,
        publication_control=publication_control,
        internal_api_token=internal_api_token,
    )
    with make_server(
        host,
        port,
        app,
        server_class=ThreadingWSGIServer,
        handler_class=WSGIRequestHandler,
    ) as server:
        server.serve_forever()


def _sanitize_economic_generation(raw: object) -> dict[str, Any] | None:
    if not isinstance(raw, Mapping):
        return None
    generation = _optional_positive_integer(raw.get("generation"))
    state = _enum(raw.get("state"), _ECONOMIC_GENERATION_STATES, "")
    if generation is None or not state:
        raise TypeError("economic generation identity is invalid")
    return {
        "generation": generation,
        "parent_generation": _optional_positive_integer(raw.get("parent_generation")),
        "state": state,
        "plan_only": _boolean(raw.get("plan_only")),
        "input_hash": _safe_sha256(raw.get("input_hash")),
        "output_hash": _safe_sha256(raw.get("output_hash")),
        "minimum_profit_on_cost_bps": _nonnegative_integer(raw.get("minimum_profit_on_cost_bps")),
        "quota_snapshot_max_age_seconds": _optional_positive_integer(
            raw.get("quota_snapshot_max_age_seconds")
        ),
        "channel_revenue_max_age_seconds": _optional_positive_integer(
            raw.get("channel_revenue_max_age_seconds")
        ),
        "health_max_age_seconds": _optional_positive_integer(raw.get("health_max_age_seconds")),
        "config_version": _safe_text(raw.get("config_version"), 128),
        "evaluated_at": _timestamp(raw.get("evaluated_at")),
        "activated_at": _timestamp(raw.get("activated_at")),
    }


def _sanitize_economic_state_counts(raw: object) -> dict[str, int]:
    if not isinstance(raw, Mapping):
        return {}
    result: dict[str, int] = {}
    for key, value in raw.items():
        state = str(key or "").strip().upper()
        if state in _ECONOMIC_ELIGIBILITY_STATES:
            result[state] = _nonnegative_integer(value)
    return dict(sorted(result.items()))


def _sanitize_profit_setting(raw: object) -> dict[str, Any] | None:
    if not isinstance(raw, Mapping):
        return None
    bps = _nonnegative_integer(raw.get("minimum_profit_on_cost_bps"))
    if bps > 100_000:
        raise TypeError("profit setting is outside the supported range")
    net_multiplier = Decimal(1) + Decimal(bps) / Decimal(10_000)
    return {
        "profit_percent": _signed_decimal_text(Decimal(bps) / Decimal(100)),
        "minimum_profit_on_cost_bps": bps,
        "net_multiplier": _signed_decimal_text(net_multiplier),
        "gross_listing_multiplier": _signed_decimal_text(
            net_multiplier * Decimal("1.2")
        ),
        "updated_at": _timestamp(raw.get("updated_at")),
        "updated_by": _safe_code(raw.get("updated_by")),
    }


def _sanitize_economic_priority1(raw: Mapping[str, Any], model: str) -> dict[str, Any]:
    if not isinstance(raw, Mapping) or _safe_model(raw.get("model")) != model:
        raise TypeError("economic Priority 1 plan must match model")

    generation = _sanitize_economic_generation(raw.get("generation"))
    preview = _sanitize_economic_generation(raw.get("preview"))
    profit_setting = _sanitize_profit_setting(raw.get("profit_setting"))
    active_profit_bps = (
        generation["minimum_profit_on_cost_bps"]
        if generation is not None
        else (
            profit_setting["minimum_profit_on_cost_bps"]
            if profit_setting is not None
            else 0
        )
    )
    active_net_multiplier = Decimal(1) + Decimal(active_profit_bps) / Decimal(10_000)
    active_gross_listing_multiplier = active_net_multiplier * Decimal("1.2")

    projection_raw = raw.get("projection")
    projection: dict[str, Any] | None = None
    if isinstance(projection_raw, Mapping):
        projection_generation = _optional_positive_integer(projection_raw.get("generation"))
        projection_state = _enum(projection_raw.get("state"), _ECONOMIC_PROJECTION_STATES, "")
        if projection_generation is None or not projection_state:
            raise TypeError("economic projection identity is invalid")
        projection = {
            "generation": projection_generation,
            "digest": _safe_sha256(projection_raw.get("digest")),
            "state": projection_state,
            "projected_at": _timestamp(projection_raw.get("projected_at")),
            "last_error_class": _safe_code(projection_raw.get("last_error_class")),
            "updated_at": _timestamp(projection_raw.get("updated_at")),
        }

    sources_raw = raw.get("sources")
    if not isinstance(sources_raw, Sequence) or isinstance(sources_raw, (str, bytes)):
        raise TypeError("economic sources must be a sequence")
    sources: list[dict[str, Any]] = []
    for value in sources_raw[:500]:
        if not isinstance(value, Mapping):
            raise TypeError("economic source must be a mapping")
        source_id = _optional_positive_integer(value.get("source_id"))
        source_kind = _enum(value.get("source_kind"), _ECONOMIC_SOURCE_KINDS, "")
        if source_id is None or not source_kind:
            raise TypeError("economic source identity is invalid")
        health_state = str(value.get("health_state") or "").strip().upper()
        capacity_state = str(value.get("capacity_state") or "").strip().upper()

        contract_raw = value.get("contract")
        contract: dict[str, Any] | None = None
        if isinstance(contract_raw, Mapping):
            contract_id = _optional_positive_integer(contract_raw.get("contract_id"))
            cost_mode = _enum(contract_raw.get("cost_mode"), _ECONOMIC_COST_MODES, "")
            contract_state = _enum(contract_raw.get("state"), _ECONOMIC_CONTRACT_STATES, "")
            if contract_id is None or not cost_mode or not contract_state:
                raise TypeError("economic contract identity is invalid")
            contract = {
                "contract_id": contract_id,
                "cost_mode": cost_mode,
                "cycle_cost_cny": _positive_decimal_text(contract_raw.get("cycle_cost_cny")),
                "declared_k_cny": _positive_decimal_text(contract_raw.get("declared_k_cny")),
                "currency": (
                    "CNY" if str(contract_raw.get("currency") or "").strip() == "CNY" else ""
                ),
                "cost_basis": _safe_code(contract_raw.get("cost_basis")),
                "quota_basis": _safe_code(contract_raw.get("quota_basis")),
                "effective_at": _timestamp(contract_raw.get("effective_at")),
                "expires_at": _timestamp(contract_raw.get("expires_at")),
                "config_version": _safe_text(contract_raw.get("config_version"), 128),
                "source_hash": _safe_sha256(contract_raw.get("source_hash")),
                "state": contract_state,
            }

        quota_raw = value.get("quota")
        quota: dict[str, Any] | None = None
        if isinstance(quota_raw, Mapping):
            snapshot_id = _optional_positive_integer(quota_raw.get("snapshot_id"))
            quota_state = str(quota_raw.get("state") or "").strip().upper()
            if snapshot_id is None or quota_state not in _ECONOMIC_QUOTA_STATES:
                raise TypeError("economic quota identity is invalid")
            quota = {
                "snapshot_id": snapshot_id,
                "quota_epoch": _safe_text(quota_raw.get("quota_epoch"), 256),
                "used_a": _nonnegative_decimal_text(quota_raw.get("used_a")),
                "remaining_a": _nonnegative_decimal_text(quota_raw.get("remaining_a")),
                "total_a": _positive_decimal_text(quota_raw.get("total_a")),
                "reset_at": _timestamp(quota_raw.get("reset_at")),
                "observed_at": _timestamp(quota_raw.get("observed_at")),
                "payload_hash": _safe_sha256(quota_raw.get("payload_hash")),
                "state": quota_state,
                "estimate_state": _enum(
                    quota_raw.get("estimate_state"),
                    frozenset({"provisional", "converged"}),
                    "",
                ).upper(),
                "estimate_method": _safe_code(quota_raw.get("estimate_method")),
                "evidence_point_count": _nonnegative_integer(quota_raw.get("evidence_point_count")),
                "distinct_level_count": _nonnegative_integer(quota_raw.get("distinct_level_count")),
                "slope_count": _nonnegative_integer(quota_raw.get("slope_count")),
                "relative_mad": _nonnegative_decimal_text(quota_raw.get("relative_mad")),
                "raw_utilization_percent": _nonnegative_decimal_text(
                    quota_raw.get("raw_utilization_percent")
                ),
                "raw_window_cost_a": _nonnegative_decimal_text(quota_raw.get("raw_window_cost_a")),
                "upstream_updated_at": _timestamp(quota_raw.get("upstream_updated_at")),
            }

        attempts_raw = value.get("attempts")
        attempts_source = attempts_raw if isinstance(attempts_raw, Mapping) else {}
        attempts = {
            "attempt_count": _nonnegative_integer(attempts_source.get("attempt_count")),
            "finished_attempt_count": _nonnegative_integer(
                attempts_source.get("finished_attempt_count")
            ),
            "negative_profit_attempt_count": _nonnegative_integer(
                attempts_source.get("negative_profit_attempt_count")
            ),
            "realized_a": _nonnegative_decimal_text(attempts_source.get("realized_a")),
            "realized_cost_cny": _nonnegative_decimal_text(
                attempts_source.get("realized_cost_cny")
            ),
            "realized_revenue_cny": _nonnegative_decimal_text(
                attempts_source.get("realized_revenue_cny")
            ),
            "realized_profit_cny": _signed_decimal_text(attempts_source.get("realized_profit_cny")),
            "realized_a_1h": _nonnegative_decimal_text(attempts_source.get("realized_a_1h")),
            "realized_a_per_second_1h": _nonnegative_decimal_text(
                attempts_source.get("realized_a_per_second_1h")
            ),
            "last_finished_at": _timestamp(attempts_source.get("last_finished_at")),
        }
        runtime_raw = value.get("runtime_usage")
        runtime_source = runtime_raw if isinstance(runtime_raw, Mapping) else {}
        runtime_usage = {
            "request_count_1h": _nonnegative_integer(runtime_source.get("request_count_1h")),
            "consumed_a_1h": _nonnegative_decimal_text(runtime_source.get("consumed_a_1h")),
            "group_ids": _positive_integer_list(runtime_source.get("group_ids"), maximum=100),
            "last_used_at": _timestamp(runtime_source.get("last_used_at")),
        }
        source_k_cny = _positive_decimal_text(value.get("source_k_cny"))
        minimum_channel_k_cny = _positive_decimal_text(value.get("minimum_channel_k_cny"))
        if minimum_channel_k_cny is None:
            minimum_channel_k_cny = _economic_floor_text(
                source_k_cny,
                active_net_multiplier,
            )
        sources.append(
            {
                "source_id": source_id,
                "source_key": _safe_text(value.get("source_key"), 256),
                "source_kind": source_kind,
                "origin_class": _safe_text(value.get("origin_class"), 128),
                "account_id": _optional_positive_integer(value.get("account_id")),
                "enabled": _boolean(value.get("enabled")),
                "health_state": (
                    health_state if health_state in _ECONOMIC_HEALTH_STATES else "UNKNOWN"
                ),
                "health_observed_at": _timestamp(value.get("health_observed_at")),
                "capacity_state": (
                    capacity_state if capacity_state in _ECONOMIC_CAPACITY_STATES else "UNKNOWN"
                ),
                "capacity_observed_at": _timestamp(value.get("capacity_observed_at")),
                "capacity_epoch": _safe_text(value.get("capacity_epoch"), 256),
                "configured_reserve_a": _nonnegative_decimal_text(
                    value.get("configured_reserve_a")
                ),
                "config_version": _safe_text(value.get("config_version"), 128),
                "contract": contract,
                "quota": quota,
                "source_k_cny": source_k_cny,
                "minimum_channel_k_cny": minimum_channel_k_cny,
                "minimum_listing_k_cny": _economic_floor_text(
                    source_k_cny,
                    active_gross_listing_multiplier,
                ),
                "evaluated_channel_count": _nonnegative_integer(
                    value.get("evaluated_channel_count")
                ),
                "eligible_channel_count": _nonnegative_integer(value.get("eligible_channel_count")),
                "evaluated_channel_ids": _positive_integer_list(
                    value.get("evaluated_channel_ids"), maximum=2000
                ),
                "eligible_channel_ids": _positive_integer_list(
                    value.get("eligible_channel_ids"), maximum=2000
                ),
                "required_burn_a_per_second": _nonnegative_decimal_text(
                    value.get("required_burn_a_per_second")
                ),
                "eligibility_states": _sanitize_economic_state_counts(
                    value.get("eligibility_states")
                ),
                "attempts": attempts,
                "runtime_usage": runtime_usage,
                "updated_at": _timestamp(value.get("updated_at")),
            }
        )
    sources.sort(key=lambda item: item["source_id"])

    channels_raw = raw.get("channels")
    if not isinstance(channels_raw, Sequence) or isinstance(channels_raw, (str, bytes)):
        raise TypeError("economic channels must be a sequence")
    channels: list[dict[str, Any]] = []
    for value in channels_raw[:2000]:
        if not isinstance(value, Mapping):
            raise TypeError("economic channel must be a mapping")
        channel_id = _optional_positive_integer(value.get("channel_id"))
        if channel_id is None:
            raise TypeError("economic channel identity is invalid")
        eligible_raw = value.get("eligible_sources")
        eligible_sources: list[dict[str, Any]] = []
        if isinstance(eligible_raw, Sequence) and not isinstance(eligible_raw, (str, bytes)):
            for candidate in eligible_raw[:500]:
                if not isinstance(candidate, Mapping):
                    continue
                source_id = _optional_positive_integer(candidate.get("source_id"))
                source_kind = _enum(candidate.get("source_kind"), _ECONOMIC_SOURCE_KINDS, "")
                candidate_state = str(candidate.get("state") or "").strip().upper()
                if (
                    source_id is None
                    or not source_kind
                    or candidate_state not in {"ELIGIBLE", "THROTTLED"}
                ):
                    continue
                eligible_sources.append(
                    {
                        "source_id": source_id,
                        "source_key": _safe_text(candidate.get("source_key"), 256),
                        "source_kind": source_kind,
                        "account_id": _optional_positive_integer(candidate.get("account_id")),
                        "state": candidate_state,
                        "source_k_cny": _positive_decimal_text(candidate.get("source_k_cny")),
                        "minimum_channel_k_cny": _positive_decimal_text(
                            candidate.get("minimum_channel_k_cny")
                        ),
                        "profit_on_cost_bps": _optional_integer(
                            candidate.get("profit_on_cost_bps")
                        ),
                        "required_burn_a_per_second": _nonnegative_decimal_text(
                            candidate.get("required_burn_a_per_second")
                        ),
                    }
                )
        channels.append(
            {
                "channel_id": channel_id,
                "channel_net_k_cny": _positive_decimal_text(value.get("channel_net_k_cny")),
                "minimum_source_k_cny": _positive_decimal_text(value.get("minimum_source_k_cny")),
                "minimum_required_channel_k_cny": _positive_decimal_text(
                    value.get("minimum_required_channel_k_cny")
                ),
                "headroom_cny": _signed_decimal_text(value.get("headroom_cny")),
                "headroom_bps": _optional_integer(value.get("headroom_bps")),
                "evaluated_source_count": _nonnegative_integer(value.get("evaluated_source_count")),
                "eligible_source_count": _nonnegative_integer(value.get("eligible_source_count")),
                "eligibility_states": _sanitize_economic_state_counts(
                    value.get("eligibility_states")
                ),
                "eligible_sources": eligible_sources,
            }
        )
    channels.sort(key=lambda item: item["channel_id"])

    summary_raw = raw.get("summary")
    summary_source = summary_raw if isinstance(summary_raw, Mapping) else {}
    summary = {
        "source_count": _nonnegative_integer(summary_source.get("source_count")),
        "returned_source_count": _nonnegative_integer(summary_source.get("returned_source_count")),
        "channel_count": _nonnegative_integer(summary_source.get("channel_count")),
        "returned_channel_count": _nonnegative_integer(
            summary_source.get("returned_channel_count")
        ),
        "eligible_channel_count": _nonnegative_integer(
            summary_source.get("eligible_channel_count")
        ),
        "eligible_pair_count": _nonnegative_integer(summary_source.get("eligible_pair_count")),
        "attempt_count": _nonnegative_integer(summary_source.get("attempt_count")),
        "negative_profit_attempt_count": _nonnegative_integer(
            summary_source.get("negative_profit_attempt_count")
        ),
        "realized_a": _nonnegative_decimal_text(summary_source.get("realized_a")),
        "realized_cost_cny": _nonnegative_decimal_text(summary_source.get("realized_cost_cny")),
        "realized_revenue_cny": _nonnegative_decimal_text(
            summary_source.get("realized_revenue_cny")
        ),
        "realized_profit_cny": _signed_decimal_text(summary_source.get("realized_profit_cny")),
    }
    projection_matches_generation = bool(
        generation
        and projection
        and projection["generation"] == generation["generation"]
        and (
            (generation["plan_only"] and projection["state"] == "plan_only")
            or (not generation["plan_only"] and projection["state"] in {"projected", "withdrawn"})
        )
    )
    return {
        "model": model,
        "generated_at": _timestamp(raw.get("generated_at")) or _utc_now(),
        "profit_setting": profit_setting,
        "generation": generation,
        "preview": preview,
        "projection": projection,
        "projection_matches_generation": projection_matches_generation,
        "eligibility_states": _sanitize_economic_state_counts(raw.get("eligibility_states")),
        "sources": sources,
        "channels": channels,
        "summary": summary,
    }


def _sanitize_provider_pool(raw: Mapping[str, Any], model: str) -> dict[str, Any]:
    if not isinstance(raw, Mapping) or _safe_model(raw.get("model")) != model:
        raise TypeError("provider pool must match model")

    routing_raw = raw.get("routing")
    routing: dict[str, Any] | None = None
    if isinstance(routing_raw, Mapping):
        generation = _optional_positive_integer(routing_raw.get("generation"))
        if generation is None:
            raise TypeError("provider routing generation must be positive")
        routing = {
            "generation": generation,
            "parent_generation": _optional_positive_integer(routing_raw.get("parent_generation")),
            "state": _enum(routing_raw.get("state"), _PROVIDER_GENERATION_STATES, "unknown"),
            "source": _safe_code(routing_raw.get("source")),
            "source_hash": _safe_sha256(routing_raw.get("source_hash")),
            "desired_size": _nonnegative_integer(routing_raw.get("desired_size")),
            "topn_channel_ids": _positive_integer_list(
                routing_raw.get("topn_channel_ids"), maximum=100
            ),
            "standby_channel_ids": _positive_integer_list(
                routing_raw.get("standby_channel_ids"), maximum=100
            ),
            "observed_at": _timestamp(routing_raw.get("observed_at")),
            "planned_at": _timestamp(routing_raw.get("planned_at")),
            "activated_at": _timestamp(routing_raw.get("activated_at")),
            "updated_at": _timestamp(routing_raw.get("updated_at")),
            "config_version": _safe_text(routing_raw.get("config_version"), 128),
        }

    cost_raw = raw.get("cost")
    cost: dict[str, Any] | None = None
    if isinstance(cost_raw, Mapping):
        generation = _optional_positive_integer(cost_raw.get("generation"))
        if generation is None:
            raise TypeError("provider cost generation must be positive")
        cost = {
            "generation": generation,
            "parent_generation": _optional_positive_integer(cost_raw.get("parent_generation")),
            "state": _enum(cost_raw.get("state"), _PROVIDER_PRICING_STATES, "unknown"),
            "source": _safe_code(cost_raw.get("source")),
            "input_hash": _safe_sha256(cost_raw.get("input_hash")),
            "output_hash": _safe_sha256(cost_raw.get("output_hash")),
            "token_mix_version": _safe_text(cost_raw.get("token_mix_version"), 128),
            "exchange_rate_version": _safe_text(cost_raw.get("exchange_rate_version"), 128),
            "quote_observed_at": _timestamp(cost_raw.get("quote_observed_at")),
            "active_slot_nos": _positive_integer_list(cost_raw.get("active_slot_nos"), maximum=100),
            "route_cost_multiplier": _nonnegative_decimal_text(
                cost_raw.get("route_cost_multiplier")
            ),
            "retry_reserve_multiplier": _nonnegative_decimal_text(
                cost_raw.get("retry_reserve_multiplier")
            ),
            "realized_cost_p95_multiplier": _nonnegative_decimal_text(
                cost_raw.get("realized_cost_p95_multiplier")
            ),
            "pool_cost_multiplier": _nonnegative_decimal_text(cost_raw.get("pool_cost_multiplier")),
            "margin_bps": _nonnegative_integer(cost_raw.get("margin_bps")),
            "headroom_bps": _nonnegative_integer(cost_raw.get("headroom_bps")),
            "billing_floor_multiplier": _nonnegative_decimal_text(
                cost_raw.get("billing_floor_multiplier")
            ),
            "transition_floor_multiplier": _nonnegative_decimal_text(
                cost_raw.get("transition_floor_multiplier")
            ),
            "observed_billing_floor_multiplier": _nonnegative_decimal_text(
                cost_raw.get("observed_billing_floor_multiplier")
            ),
            "planned_at": _timestamp(cost_raw.get("planned_at")),
            "confirmed_at": _timestamp(cost_raw.get("confirmed_at")),
            "activated_at": _timestamp(cost_raw.get("activated_at")),
            "updated_at": _timestamp(cost_raw.get("updated_at")),
            "config_version": _safe_text(cost_raw.get("config_version"), 128),
        }

    auto_pool_raw = raw.get("auto_pool")
    auto_pool: dict[str, Any] | None = None
    if isinstance(auto_pool_raw, Mapping):
        generation = _optional_positive_integer(auto_pool_raw.get("generation"))
        generation_id = _optional_positive_integer(auto_pool_raw.get("generation_id"))
        if generation is None or generation_id is None:
            raise TypeError("provider auto-pool generation must be positive")
        auto_pool = {
            "generation_id": generation_id,
            "generation": generation,
            "state": _enum(
                auto_pool_raw.get("state"), _PROVIDER_GENERATION_STATES, "unknown"
            ),
            "selected_channel_ids": _positive_integer_list(
                auto_pool_raw.get("selected_channel_ids"), maximum=100
            ),
            "max_ratio_cny": _nonnegative_decimal_text(auto_pool_raw.get("max_ratio_cny")),
            "max_ratio_usd": _nonnegative_decimal_text(auto_pool_raw.get("max_ratio_usd")),
            "target_channel_count": _nonnegative_integer(
                auto_pool_raw.get("target_channel_count")
            ),
            "ready_account_count": _nonnegative_integer(
                auto_pool_raw.get("ready_account_count")
            ),
            "account_count": _nonnegative_integer(auto_pool_raw.get("account_count")),
            "owned_channel_ban_confirmed": _boolean(
                auto_pool_raw.get("owned_channel_ban_confirmed")
            ),
            "observed_at": _timestamp(auto_pool_raw.get("observed_at")),
            "activated_at": _timestamp(auto_pool_raw.get("activated_at")),
            "updated_at": _timestamp(auto_pool_raw.get("updated_at")),
            "config_version": _safe_text(auto_pool_raw.get("config_version"), 128),
        }

    slots: list[dict[str, Any]] = []
    slots_raw = raw.get("slots")
    if isinstance(slots_raw, Sequence) and not isinstance(slots_raw, (str, bytes)):
        for value in slots_raw[:100]:
            if not isinstance(value, Mapping):
                continue
            slot_no = _optional_positive_integer(value.get("slot_no"))
            if slot_no is None:
                continue
            slot_state = str(value.get("state") or "").strip().upper()
            liquidity_state = str(value.get("liquidity_state") or "").strip().upper()
            route_mode = str(value.get("route_mode") or "fixed_pin").strip().casefold()
            slots.append(
                {
                    "slot_no": slot_no,
                    "slot_name": _safe_identifier(value.get("slot_name")),
                    "routing_generation": _optional_positive_integer(
                        value.get("routing_generation")
                    ),
                    "cost_generation": _optional_positive_integer(value.get("cost_generation")),
                    "state": (slot_state if slot_state in _PROVIDER_SLOT_STATES else "UNKNOWN"),
                    "channel_id": _optional_positive_integer(value.get("channel_id")),
                    "rank": _optional_positive_integer(value.get("rank")),
                    "priority": _optional_nonnegative_integer(value.get("priority")),
                    "enodot_binding_id": _optional_positive_integer(value.get("enodot_binding_id")),
                    "account_id": _optional_positive_integer(value.get("account_id")),
                    "token_id": _optional_positive_integer(value.get("token_id")),
                    "desired_cost_multiplier": _nonnegative_decimal_text(
                        value.get("desired_cost_multiplier")
                    ),
                    "observed_cost_multiplier": _nonnegative_decimal_text(
                        value.get("observed_cost_multiplier")
                    ),
                    "liquidity_state": (
                        liquidity_state
                        if liquidity_state in _PROVIDER_LIQUIDITY_STATES
                        else "UNKNOWN"
                    ),
                    "funding_eligible": _boolean(value.get("funding_eligible")),
                    "desired_schedulable": _boolean(value.get("desired_schedulable")),
                    "observed_schedulable": _boolean(value.get("observed_schedulable")),
                    "confirmed_pin_binding_id": _optional_positive_integer(
                        value.get("confirmed_pin_binding_id")
                    ),
                    "pin_confirmed": _boolean(value.get("pin_confirmed")),
                    "probe_confirmed": _boolean(value.get("probe_confirmed")),
                    "route_mode": (
                        route_mode if route_mode in _PROVIDER_ROUTE_MODES else "unknown"
                    ),
                    "auto_pool_generation_id": _optional_positive_integer(
                        value.get("auto_pool_generation_id")
                    ),
                    "auto_pool_generation": _optional_positive_integer(
                        value.get("auto_pool_generation")
                    ),
                    "auto_pool_subscription_id": _optional_positive_integer(
                        value.get("auto_pool_subscription_id")
                    ),
                    "auto_pool_confirmed": _boolean(value.get("auto_pool_confirmed")),
                    "auto_pool_max_ratio_cny": _nonnegative_decimal_text(
                        value.get("auto_pool_max_ratio_cny")
                    ),
                    "owned_channel_ban_confirmed": _boolean(
                        value.get("owned_channel_ban_confirmed")
                    ),
                    "auto_pool_unpin_confirmed": _boolean(
                        value.get("auto_pool_unpin_confirmed")
                    ),
                    "auto_pool_account_state": _safe_code(
                        value.get("auto_pool_account_state")
                    ),
                    "state_version": _nonnegative_integer(value.get("state_version")),
                    "last_success_at": _timestamp(value.get("last_success_at")),
                    "latency_ewma_ms": _nonnegative_decimal_text(value.get("latency_ewma_ms")),
                    "valid_from": _timestamp(value.get("valid_from")),
                    "updated_at": _timestamp(value.get("updated_at")),
                }
            )
    slots.sort(key=lambda item: item["slot_no"])

    publication_raw = raw.get("publication")
    publication: dict[str, Any] | None = None
    if isinstance(publication_raw, Mapping):
        desired_state = _enum(
            publication_raw.get("desired_state"),
            _PROVIDER_PUBLICATION_STATES,
            "unknown",
        )
        observed_state = _enum(
            publication_raw.get("observed_state"),
            _PROVIDER_PUBLICATION_STATES,
            "unknown",
        )
        desired_at = _timestamp(publication_raw.get("desired_at"))
        confirmed_at = _timestamp(publication_raw.get("confirmed_at"))
        last_real_probe_at = _timestamp(publication_raw.get("last_real_probe_at"))
        confirmation_fresh = _timestamp_at_or_after(confirmed_at, desired_at)
        probe_fresh = desired_state not in {"published", "degraded"} or (
            _timestamp_at_or_after(last_real_probe_at, desired_at)
        )
        publication = {
            "desired_state": desired_state,
            "observed_state": observed_state,
            "converged": (
                desired_state != "unknown"
                and desired_state == observed_state
                and confirmation_fresh
                and probe_fresh
            ),
            "healthy_slots": _nonnegative_integer(publication_raw.get("healthy_slots")),
            "min_healthy_slots": _nonnegative_integer(publication_raw.get("min_healthy_slots")),
            "redundancy_target": _nonnegative_integer(publication_raw.get("redundancy_target")),
            "reason_code": _safe_code(publication_raw.get("reason_code")),
            "desired_routing_generation": _optional_positive_integer(
                publication_raw.get("desired_routing_generation")
            ),
            "desired_cost_generation": _optional_positive_integer(
                publication_raw.get("desired_cost_generation")
            ),
            "scope": _safe_text(publication_raw.get("scope"), 512),
            "state_version": _nonnegative_integer(publication_raw.get("state_version")),
            "desired_at": desired_at,
            "action_started_at": _timestamp(publication_raw.get("action_started_at")),
            "observed_at": _timestamp(publication_raw.get("observed_at")),
            "confirmed_at": confirmed_at,
            "last_real_probe_at": last_real_probe_at,
            "last_real_probe_request_id": _safe_text(
                publication_raw.get("last_real_probe_request_id"), 128
            ),
            "updated_at": _timestamp(publication_raw.get("updated_at")),
        }

    quarantines: list[dict[str, Any]] = []
    quarantine_raw = raw.get("active_quarantines")
    if isinstance(quarantine_raw, Sequence) and not isinstance(quarantine_raw, (str, bytes)):
        for value in quarantine_raw[:100]:
            if not isinstance(value, Mapping):
                continue
            scope_type = _enum(value.get("scope_type"), _PROVIDER_QUARANTINE_SCOPES, "")
            if not scope_type:
                continue
            quarantines.append(
                {
                    "scope_type": scope_type,
                    "scope_id": _safe_text(value.get("scope_id"), 128),
                    "region": _safe_optional_label(value.get("region")),
                    "reason_code": _safe_code(value.get("reason_code")),
                    "failure_count": _nonnegative_integer(value.get("failure_count")),
                    "starts_at": _timestamp(value.get("starts_at")),
                    "next_probe_at": _timestamp(value.get("next_probe_at")),
                    "ends_at": _timestamp(value.get("ends_at")),
                    "updated_at": _timestamp(value.get("updated_at")),
                }
            )

    failures: list[dict[str, Any]] = []
    failures_raw = raw.get("recent_failures")
    if isinstance(failures_raw, Sequence) and not isinstance(failures_raw, (str, bytes)):
        for value in failures_raw[:20]:
            if not isinstance(value, Mapping):
                continue
            attempt_no = _optional_positive_integer(value.get("attempt_no"))
            slot_no = _optional_positive_integer(value.get("slot_no"))
            if attempt_no is None or slot_no is None:
                continue
            failures.append(
                {
                    "request_id": _safe_text(value.get("request_id"), 128),
                    "attempt_id": _safe_text(value.get("attempt_id"), 128),
                    "attempt_no": attempt_no,
                    "routing_generation": _optional_positive_integer(
                        value.get("routing_generation")
                    ),
                    "cost_generation": _optional_positive_integer(value.get("cost_generation")),
                    "slot_no": slot_no,
                    "account_id": _optional_positive_integer(value.get("account_id")),
                    "token_id": _optional_positive_integer(value.get("token_id")),
                    "channel_id": _optional_positive_integer(value.get("channel_id")),
                    "endpoint_host": _safe_text(value.get("endpoint_host"), 255),
                    "egress_id": _safe_identifier(value.get("egress_id")),
                    "failure_owner": _enum(
                        value.get("failure_owner"), _PROVIDER_FAILURE_OWNERS, "unknown"
                    ),
                    "failure_code": _safe_code(value.get("failure_code")),
                    "http_status": _http_status(value.get("http_status")),
                    "transport_status": _http_status(value.get("transport_status")),
                    "terminal_event_seen": _boolean(value.get("terminal_event_seen")),
                    "delivery_state": _safe_code(value.get("delivery_state")),
                    "actual_cost_micros": _nonnegative_integer(value.get("actual_cost_micros")),
                    "occurred_at": _timestamp(value.get("occurred_at")),
                }
            )

    # Active state is an all-or-nothing bundle. Historical publication rows and
    # orphaned slots must never be promoted when no active generations exist.
    if routing is None or cost is None:
        routing = None
        cost = None
        auto_pool = None
        slots = []
        publication = None

    preview: dict[str, Any] | None = None
    preview_raw = raw.get("preview")
    if isinstance(preview_raw, Mapping):
        preview_projection = _sanitize_provider_pool(
            {
                "model": model,
                "generated_at": raw.get("generated_at"),
                "routing": preview_raw.get("routing"),
                "cost": preview_raw.get("cost"),
                "slots": preview_raw.get("slots"),
                "publication": None,
                "active_quarantines": [],
                "recent_failures": [],
            },
            model,
        )
        preview_routing = preview_projection["routing"]
        preview_cost = preview_projection["cost"]
        preview_slots = preview_projection["slots"]
        preview_kind = _enum(
            preview_raw.get("kind"),
            frozenset({"planned", "applying", "failed"}),
            "",
        )
        valid_state_pair = bool(
            preview_routing
            and preview_cost
            and (preview_routing["state"], preview_cost["state"])
            in {
                ("planned", "planned"),
                ("applying", "applying"),
                ("applying", "confirmed"),
                ("failed", "failed"),
            }
        )
        if (
            preview_routing is None
            or preview_cost is None
            or not preview_kind
            or preview_kind != preview_routing["state"]
            or not valid_state_pair
        ):
            raise TypeError("provider preview must be an unpublished bundle")
        routing_generation = preview_routing["generation"]
        cost_generation = preview_cost["generation"]
        if any(
            item["routing_generation"] != routing_generation
            or item["cost_generation"] != cost_generation
            for item in preview_slots
        ):
            raise TypeError("provider preview slot generation mismatch")
        preview_summary = preview_projection["summary"]
        preview = {
            "kind": preview_kind,
            "routing": preview_routing,
            "cost": preview_cost,
            "slots": preview_slots,
            "summary": {
                key: preview_summary[key]
                for key in (
                    "topn_count",
                    "standby_count",
                    "slot_count",
                    "ready_slot_count",
                    "vacant_slot_count",
                )
            },
        }

    topn_count = len(routing["topn_channel_ids"]) if routing is not None else 0
    standby_count = len(routing["standby_channel_ids"]) if routing is not None else 0
    return {
        "model": model,
        "generated_at": _timestamp(raw.get("generated_at")) or _utc_now(),
        "routing": routing,
        "cost": cost,
        "auto_pool": auto_pool,
        "slots": slots,
        "publication": publication,
        "preview": preview,
        "active_quarantines": quarantines,
        "recent_failures": failures,
        "recent_generation_failure_count": _nonnegative_integer(
            raw.get("recent_generation_failure_count")
        ),
        "summary": {
            "topn_count": topn_count,
            "standby_count": standby_count,
            "slot_count": len(slots),
            "ready_slot_count": sum(item["state"] == "READY" for item in slots),
            "vacant_slot_count": sum(item["state"] == "VACANT" for item in slots),
            "schedulable_slot_count": sum(
                item["state"] == "READY" and item["observed_schedulable"] for item in slots
            ),
            "active_quarantine_count": len(quarantines),
            "recent_failure_count": len(failures),
        },
    }


def _sanitize_provider_inventory(raw: Mapping[str, Any], model: str) -> dict[str, Any]:
    if not isinstance(raw, Mapping) or _safe_model(raw.get("model")) != model:
        raise TypeError("provider inventory must match model")
    items_raw = raw.get("items")
    if not isinstance(items_raw, Sequence) or isinstance(items_raw, (str, bytes)):
        raise TypeError("provider inventory items must be a sequence")

    items: list[dict[str, Any]] = []
    for value in items_raw[:500]:
        if not isinstance(value, Mapping):
            raise TypeError("provider inventory item must be a mapping")
        merchant_id = _optional_positive_integer(value.get("merchant_id"))
        inventory_state = str(value.get("inventory_state") or "").strip().upper()
        if merchant_id is None or inventory_state not in _PROVIDER_INVENTORY_STATES:
            raise TypeError("provider inventory identity or state is invalid")

        assigned_slot_no = _optional_positive_integer(value.get("assigned_slot_no"))
        assignment_raw = value.get("assignment")
        assignment: dict[str, Any] | None = None
        if assignment_raw is not None:
            if not isinstance(assignment_raw, Mapping) or assigned_slot_no is None:
                raise TypeError("provider inventory assignment is invalid")
            assignment_slot_no = _optional_positive_integer(assignment_raw.get("slot_no"))
            if assignment_slot_no != assigned_slot_no:
                raise TypeError("provider inventory assignment slot mismatch")
            assignment_state = str(assignment_raw.get("state") or "").strip().upper()
            assignment = {
                "slot_no": assignment_slot_no,
                "found": _boolean(assignment_raw.get("found")),
                "identity_matches": _boolean(assignment_raw.get("identity_matches")),
                "state": (
                    assignment_state if assignment_state in _PROVIDER_SLOT_STATES else "UNKNOWN"
                ),
                "routing_generation": _optional_positive_integer(
                    assignment_raw.get("routing_generation")
                ),
                "cost_generation": _optional_positive_integer(
                    assignment_raw.get("cost_generation")
                ),
                "channel_id": _optional_positive_integer(assignment_raw.get("channel_id")),
                "priority": _optional_positive_integer(assignment_raw.get("priority")),
                "observed_schedulable": _boolean(assignment_raw.get("observed_schedulable")),
            }

        binding_ready = _boolean(value.get("binding_ready"))
        liquidity_state = str(value.get("liquidity_state") or "").strip().upper()
        item = {
            "merchant_id": merchant_id,
            "enodot_binding_id": _optional_positive_integer(value.get("enodot_binding_id")),
            "account_id": _optional_positive_integer(value.get("account_id")),
            "token_id": _optional_positive_integer(value.get("token_id")),
            "inventory_state": inventory_state,
            "liquidity_state": (
                liquidity_state if liquidity_state in _PROVIDER_LIQUIDITY_STATES else "UNKNOWN"
            ),
            "funding_eligible": _boolean(value.get("funding_eligible")),
            "schedulable": _boolean(value.get("schedulable")),
            "binding_ready": binding_ready,
            "balance_units": _optional_nonnegative_integer(value.get("balance_units")),
            "financial_source_hash": _safe_sha256(value.get("financial_source_hash")),
            "assigned_slot_no": assigned_slot_no,
            "assignment": assignment,
            "assignment_consistent": assigned_slot_no is None
            or (assignment is not None and assignment["found"] and assignment["identity_matches"]),
            "state_version": _nonnegative_integer(value.get("state_version")),
            "last_financial_observed_at": _timestamp(value.get("last_financial_observed_at")),
            "updated_at": _timestamp(value.get("updated_at")),
        }
        items.append(item)

    return {
        "model": model,
        "generated_at": _timestamp(raw.get("generated_at")) or _utc_now(),
        "items": items,
        "summary": {
            "inventory_count": len(items),
            "ready_count": sum(item["inventory_state"] == "READY" for item in items),
            "warm_standby_count": sum(item["inventory_state"] == "WARM_STANDBY" for item in items),
            "repairing_count": sum(item["inventory_state"] == "REPAIRING" for item in items),
            "retired_count": sum(item["inventory_state"] == "RETIRED" for item in items),
            "funded_count": sum(item["funding_eligible"] for item in items),
            "schedulable_count": sum(item["schedulable"] for item in items),
            "assigned_count": sum(item["assigned_slot_no"] is not None for item in items),
            "inconsistent_assignment_count": sum(
                not item["assignment_consistent"] for item in items
            ),
        },
    }


def _sanitize_overview(raw: Mapping[str, Any], window: str) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise TypeError("overview must be a mapping")
    stocks = raw.get("stocks")
    flows = raw.get("flows")
    freshness = raw.get("freshness")
    stock_source = stocks if isinstance(stocks, Mapping) else {}
    flow_source = flows if isinstance(flows, Mapping) else {}
    freshness_source = freshness if isinstance(freshness, Mapping) else {}
    profit_status = _enum(flow_source.get("profit_status"), _LEDGER_STATES, "pending")
    actual_profit = _optional_integer(flow_source.get("actual_profit_micros"))
    if profit_status not in {"confirmed", "not_applicable"}:
        actual_profit = None
    return {
        "window": window,
        "generated_at": _timestamp(raw.get("generated_at")) or _utc_now(),
        "merchant_count": _nonnegative_integer(raw.get("merchant_count")),
        "stocks": {name: _sanitize_stock(stock_source.get(name)) for name in _STOCK_NAMES},
        "flows": {
            **{field: _integer(flow_source.get(field)) for field in _FLOW_FIELDS},
            "actual_profit_micros": actual_profit,
            # Stays null when attribution coverage is too low to trust a number.
            "honest_profit_micros": _optional_integer(flow_source.get("honest_profit_micros")),
            "attribution_coverage": _optional_ratio(flow_source.get("attribution_coverage")),
            "profit_margin": (
                _optional_ratio(flow_source.get("profit_margin"))
                if actual_profit is not None
                else None
            ),
            "profit_status": profit_status,
            "sales_status": _enum(flow_source.get("sales_status"), _LEDGER_STATES, "unavailable"),
        },
        "freshness": _sanitize_freshness(freshness_source),
    }


def _sanitize_merchants(raw: Sequence[Mapping[str, Any]], window: str) -> dict[str, Any]:
    if isinstance(raw, (str, bytes)) or not isinstance(raw, Sequence):
        raise TypeError("merchants must be a sequence")
    items = [_sanitize_merchant(item) for item in raw if isinstance(item, Mapping)]
    return {
        "window": window,
        "generated_at": _utc_now(),
        "count": len(items),
        "items": items,
    }


def _sanitize_inventory(raw: Mapping[str, Any], merchant_id: int) -> dict[str, Any]:
    if not isinstance(raw, Mapping) or str(raw.get("merchant_id") or "") != str(merchant_id):
        raise TypeError("inventory must match merchant")
    supplier_raw = raw.get("supplier")
    supplier = None
    if isinstance(supplier_raw, Mapping):
        supplier = {
            "id": _safe_identifier(supplier_raw.get("id")),
            "name": _safe_optional_label(supplier_raw.get("name")),
            "description": _safe_text(supplier_raw.get("description"), 2000),
            "contact_info": _safe_text(supplier_raw.get("contact_info"), 512),
        }
    channels_raw = raw.get("channels")
    channels: list[dict[str, Any]] = []
    if isinstance(channels_raw, Sequence) and not isinstance(channels_raw, (str, bytes)):
        for value in channels_raw[:100]:
            if not isinstance(value, Mapping):
                continue
            models_raw = value.get("models")
            models = (
                [_safe_model(item) for item in models_raw[:200]]
                if isinstance(models_raw, Sequence) and not isinstance(models_raw, (str, bytes))
                else []
            )
            listings_raw = value.get("listings")
            listings = (
                [_sanitize_inventory_listing(item) for item in listings_raw[:200]]
                if isinstance(listings_raw, Sequence) and not isinstance(listings_raw, (str, bytes))
                else []
            )
            channels.append(
                {
                    "id": _safe_identifier(value.get("id")),
                    "binding_id": _safe_identifier(value.get("binding_id")),
                    "name": _safe_optional_label(value.get("name")),
                    "nickname": _safe_optional_label(value.get("nickname")),
                    "description": _safe_text(value.get("description"), 2000),
                    "models": [model for model in models if model],
                    "status": _safe_code(value.get("status")),
                    "availability": _safe_code(value.get("availability")),
                    "listings": listings,
                }
            )
    orphaned_raw = raw.get("orphaned_listings")
    orphaned = (
        [_sanitize_inventory_listing(item) for item in orphaned_raw[:200]]
        if isinstance(orphaned_raw, Sequence) and not isinstance(orphaned_raw, (str, bytes))
        else []
    )
    return {
        "merchant_id": str(merchant_id),
        "supplier_state": _enum(
            raw.get("supplier_state"), frozenset({"active", "not_active"}), "not_active"
        ),
        "supplier": supplier,
        "channels": channels,
        "orphaned_listings": orphaned,
    }


def _sanitize_inventory_listing(raw: object) -> dict[str, Any]:
    value = raw if isinstance(raw, Mapping) else {}
    return {
        "id": _safe_identifier(value.get("id")),
        "channel_id": _safe_identifier(value.get("channel_id")),
        "own_model": _safe_model(value.get("own_model")),
        "platform_model": _safe_model(value.get("platform_model")),
        "display_name": _safe_optional_label(value.get("display_name")),
        "description": _safe_text(value.get("description"), 2000),
        "status": _safe_code(value.get("status")),
        "availability": _safe_code(value.get("availability")),
        "visibility": _safe_code(value.get("visibility")),
    }


def _sanitize_market_traffic(
    raw: Mapping[str, Any],
    window: str,
    model: str,
) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise TypeError("market traffic must be a mapping")
    freshness_raw = raw.get("freshness")
    totals_raw = raw.get("totals")
    method_raw = raw.get("method")
    observer_backoff_raw = raw.get("observer_backoff")
    freshness = freshness_raw if isinstance(freshness_raw, Mapping) else {}
    totals = totals_raw if isinstance(totals_raw, Mapping) else {}
    method = method_raw if isinstance(method_raw, Mapping) else {}
    observer_backoff = observer_backoff_raw if isinstance(observer_backoff_raw, Mapping) else {}
    raw_bands = raw.get("bands")
    raw_positions = raw.get("managed_positions")
    raw_channel_failures = raw.get("channel_failures")
    bands: list[dict[str, Any]] = []
    if isinstance(raw_bands, Sequence) and not isinstance(raw_bands, (str, bytes)):
        for value in raw_bands[:64]:
            if not isinstance(value, Mapping):
                continue
            series_raw = value.get("series")
            series = []
            if isinstance(series_raw, Sequence) and not isinstance(series_raw, (str, bytes)):
                series = [
                    {
                        "bucket_start": _timestamp(item.get("bucket_start")),
                        "event_lower_bound": _nonnegative_integer(item.get("event_lower_bound")),
                        "quorum_event_count": _nonnegative_integer(item.get("quorum_event_count")),
                    }
                    for item in series_raw[:360]
                    if isinstance(item, Mapping)
                ]
            bands.append(
                {
                    "band_id": _safe_identifier(value.get("band_id")),
                    "label": _safe_optional_label(value.get("label")),
                    "minimum_k_cny": _optional_nonnegative_number(value.get("minimum_k_cny")),
                    "maximum_k_cny": _optional_nonnegative_number(value.get("maximum_k_cny")),
                    "current_channel_count": _nonnegative_integer(
                        value.get("current_channel_count")
                    ),
                    "event_lower_bound": _nonnegative_integer(value.get("event_lower_bound")),
                    "quorum_event_count": _nonnegative_integer(value.get("quorum_event_count")),
                    "owned_event_count": _nonnegative_integer(value.get("owned_event_count")),
                    "active_channel_count": _nonnegative_integer(value.get("active_channel_count")),
                    "just_now_rate_per_minute": _nonnegative_integer(
                        value.get("just_now_rate_per_minute")
                    ),
                    "one_minute_ago_rate_per_minute": _nonnegative_integer(
                        value.get("one_minute_ago_rate_per_minute")
                    ),
                    "trend": _enum(value.get("trend"), _TRAFFIC_TRENDS, "idle"),
                    "trend_ratio": _optional_ratio(value.get("trend_ratio")),
                    "share": _ratio(value.get("share")),
                    "series": series,
                }
            )
    positions: list[dict[str, Any]] = []
    if isinstance(raw_positions, Sequence) and not isinstance(raw_positions, (str, bytes)):
        for value in raw_positions:
            if not isinstance(value, Mapping):
                continue
            operational_state = _enum(
                value.get("operational_state"),
                _MANAGED_POSITION_STATES,
                "blocked",
            )
            blocked_reason = (
                ""
                if operational_state == "active"
                else _enum(
                    value.get("blocked_reason"),
                    _MANAGED_POSITION_BLOCK_REASONS,
                    "unknown",
                )
            )
            positions.append(
                {
                    "rank": _nonnegative_integer(value.get("rank")),
                    "merchant_id": _safe_identifier(value.get("merchant_id")),
                    "channel_id": _safe_identifier(value.get("channel_id")),
                    "listing_id": _safe_identifier(value.get("listing_id")),
                    "channel_name": _safe_optional_label(value.get("channel_name")),
                    "channel_nickname": _safe_optional_label(value.get("channel_nickname")),
                    "operational_state": operational_state,
                    "blocked_reason": blocked_reason,
                    "merchant_status": _safe_code(value.get("merchant_status")),
                    "merchant_roles": [
                        role
                        for role in _safe_code_list(value.get("merchant_roles"), maximum=8)
                        if role in _ROLES
                    ],
                    "binding_present": _boolean(value.get("binding_present")),
                    "binding_control_state": _safe_code(value.get("binding_control_state")),
                    "listing_present": _boolean(value.get("listing_present")),
                    "listing_channel_id": _safe_identifier(value.get("listing_channel_id")),
                    "listing_platform_model": _safe_model(value.get("listing_platform_model")),
                    "listing_status": _optional_nonnegative_integer(value.get("listing_status")),
                    "listing_availability": _optional_nonnegative_integer(
                        value.get("listing_availability")
                    ),
                    "listing_visibility": _optional_nonnegative_integer(
                        value.get("listing_visibility")
                    ),
                    "tier": _safe_code(value.get("tier")),
                    "price_zone": _safe_code(value.get("price_zone")),
                    "delta": _optional_nonnegative_number(value.get("delta")),
                    "target_final_multiplier": _optional_nonnegative_number(
                        value.get("target_final_multiplier")
                    ),
                    "panel_multiplier": _optional_nonnegative_number(value.get("panel_multiplier")),
                    "current_k_cny": _optional_nonnegative_number(value.get("current_k_cny")),
                    "market_available": bool(value.get("market_available")),
                    "health_state": _enum(
                        value.get("health_state"),
                        _CHANNEL_HEALTH_STATES,
                        "unknown",
                    ),
                    "raw_health_state": _enum(
                        value.get("raw_health_state"),
                        _CHANNEL_HEALTH_STATES,
                        "unknown",
                    ),
                    "health_reason": _safe_code(value.get("health_reason")),
                    "route_status": _safe_code(value.get("route_status")),
                    "recent_success_rate": _optional_nonnegative_integer(
                        value.get("recent_success_rate")
                    ),
                    "recent_p50_ms": _optional_nonnegative_number(value.get("recent_p50_ms")),
                    "p50_ttft_ms": _optional_nonnegative_number(value.get("p50_ttft_ms")),
                    "smart_routing_labels": _safe_code_list(
                        value.get("smart_routing_labels"), maximum=20
                    ),
                    "health_observed_at": _timestamp(value.get("health_observed_at")),
                    "recent_failure_count": _nonnegative_integer(value.get("recent_failure_count")),
                    "recent_client_failure_count": _nonnegative_integer(
                        value.get("recent_client_failure_count")
                    ),
                    "recent_provider_failure_count": _nonnegative_integer(
                        value.get("recent_provider_failure_count")
                    ),
                    "recent_platform_failure_count": _nonnegative_integer(
                        value.get("recent_platform_failure_count")
                    ),
                    "recent_unknown_failure_count": _nonnegative_integer(
                        value.get("recent_unknown_failure_count")
                    ),
                    "last_failure_responsibility": _enum(
                        value.get("last_failure_responsibility"),
                        _CHANNEL_FAILURE_RESPONSIBILITIES,
                        "unknown",
                    ),
                    "top_failure_codes": _sanitize_failure_codes(value.get("top_failure_codes")),
                    "seller_call_count": _nonnegative_integer(value.get("seller_call_count")),
                    "seller_income_micros": _integer(value.get("seller_income_micros")),
                    "seller_income_per_call_micros": _optional_integer(
                        value.get("seller_income_per_call_micros")
                    ),
                    "seller_income_per_hour_micros": _integer(
                        value.get("seller_income_per_hour_micros")
                    ),
                    "managed_key_call_count": _nonnegative_integer(
                        value.get("managed_key_call_count")
                    ),
                    "managed_key_debit_micros": _integer(value.get("managed_key_debit_micros")),
                    "delta_sample_status": _enum(
                        value.get("delta_sample_status"),
                        _DELTA_SAMPLE_STATES,
                        "no_sample",
                    ),
                    "estimated_contribution_micros": _optional_integer(
                        value.get("estimated_contribution_micros")
                    ),
                }
            )
    channel_failures: list[dict[str, Any]] = []
    if isinstance(raw_channel_failures, Sequence) and not isinstance(
        raw_channel_failures, (str, bytes)
    ):
        for value in raw_channel_failures[:500]:
            if not isinstance(value, Mapping):
                continue
            channel_failures.append(
                {
                    "channel_id": _safe_identifier(value.get("channel_id")),
                    "listing_id": _safe_identifier(value.get("listing_id")),
                    "supplier_id": _safe_identifier(value.get("supplier_id")),
                    "market_rank": _nonnegative_integer(value.get("market_rank")),
                    "health_state": _enum(
                        value.get("health_state"),
                        _CHANNEL_HEALTH_STATES,
                        "unknown",
                    ),
                    "available": bool(value.get("available")),
                    "recent_success_rate": _optional_nonnegative_integer(
                        value.get("recent_success_rate")
                    ),
                    "last_failure_at": _timestamp(value.get("last_failure_at")),
                    "last_failure_code": _safe_code(value.get("last_failure_code")),
                    "last_failure_responsibility": _enum(
                        value.get("last_failure_responsibility"),
                        _CHANNEL_FAILURE_RESPONSIBILITIES,
                        "unknown",
                    ),
                    "recent_failure_count": _nonnegative_integer(value.get("recent_failure_count")),
                    "recent_client_failure_count": _nonnegative_integer(
                        value.get("recent_client_failure_count")
                    ),
                    "recent_provider_failure_count": _nonnegative_integer(
                        value.get("recent_provider_failure_count")
                    ),
                    "recent_platform_failure_count": _nonnegative_integer(
                        value.get("recent_platform_failure_count")
                    ),
                    "recent_unknown_failure_count": _nonnegative_integer(
                        value.get("recent_unknown_failure_count")
                    ),
                    "top_failure_codes": _sanitize_failure_codes(value.get("top_failure_codes")),
                }
            )
    return {
        "model": model,
        "window": window,
        "generated_at": _timestamp(raw.get("generated_at")) or _utc_now(),
        "method": {
            "signal": (
                "last_success_at_transition"
                if method.get("signal") == "last_success_at_transition"
                else "unknown"
            ),
            "count_semantics": (
                "lower_bound_active_seconds"
                if method.get("count_semantics") == "lower_bound_active_seconds"
                else "unknown"
            ),
            "observer_window_seconds": _nonnegative_integer(method.get("observer_window_seconds")),
            "bucket_seconds": _nonnegative_integer(method.get("bucket_seconds")),
            "current_signal_max_age_seconds": _nonnegative_integer(
                method.get("current_signal_max_age_seconds")
            ),
            "current_comparison_seconds": _nonnegative_integer(
                method.get("current_comparison_seconds")
            ),
            "automatic_repricing": False,
        },
        "freshness": {
            "last_observed_at": _timestamp(freshness.get("last_observed_at")),
            "age_seconds": _optional_nonnegative_integer(freshness.get("age_seconds")),
            "current_channel_count": _nonnegative_integer(freshness.get("current_channel_count")),
            "available_channel_count": _nonnegative_integer(
                freshness.get("available_channel_count")
            ),
        },
        "observer_backoff": {
            "active": bool(observer_backoff.get("active")),
            "consecutive_failed_rounds": _nonnegative_integer(
                observer_backoff.get("consecutive_failed_rounds")
            ),
            "remaining_seconds": _nonnegative_integer(observer_backoff.get("remaining_seconds")),
            "retry_after": _timestamp(observer_backoff.get("retry_after")),
            "last_error_class": _safe_code(observer_backoff.get("last_error_class")),
            "last_failure_at": _timestamp(observer_backoff.get("last_failure_at")),
            "last_success_at": _timestamp(observer_backoff.get("last_success_at")),
        },
        "totals": {
            field: _nonnegative_integer(totals.get(field))
            for field in (
                "event_lower_bound",
                "quorum_event_count",
                "active_channel_count",
                "owned_event_count",
                "unpriced_event_count",
            )
        },
        "bands": bands,
        "managed_positions": positions,
        "channel_failures": channel_failures,
    }


def _sanitize_failure_codes(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return []
    return [
        {
            "code": _safe_code(value.get("code")),
            "responsibility": _enum(
                value.get("responsibility"),
                _CHANNEL_FAILURE_RESPONSIBILITIES,
                "unknown",
            ),
            "count": _nonnegative_integer(value.get("count")),
            "last_failure_at": _timestamp(value.get("last_failure_at")),
        }
        for value in raw[:8]
        if isinstance(value, Mapping)
    ]


def _sanitize_stock(raw: Any) -> dict[str, int]:
    source = raw if isinstance(raw, Mapping) else {}
    return {field: _integer(source.get(field)) for field in _STOCK_FIELDS}


def _sanitize_freshness(raw: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "status": _enum(raw.get("status"), _FRESHNESS_STATES, "unknown"),
        "last_observed_at": _timestamp(raw.get("last_observed_at")),
        "age_seconds": _nonnegative_integer(raw.get("age_seconds")),
        "fresh_count": _nonnegative_integer(raw.get("fresh_count")),
        "stale_count": _nonnegative_integer(raw.get("stale_count")),
        "error_count": _nonnegative_integer(raw.get("error_count")),
    }


def _sanitize_merchant(raw: Mapping[str, Any]) -> dict[str, Any]:
    merchant_id = _safe_identifier(raw.get("merchant_id"))
    roles = raw.get("roles")
    role_values = (
        roles if isinstance(roles, Sequence) and not isinstance(roles, (str, bytes)) else ()
    )
    normalized_roles = {_enum(role, _ROLES, "") for role in role_values} - {""}
    if not normalized_roles:
        normalized_roles.add("seller")
    profit_status = _enum(raw.get("profit_status"), _LEDGER_STATES, "pending")
    actual_profit = _optional_integer(raw.get("actual_profit_micros"))
    if profit_status not in {"confirmed", "not_applicable"}:
        actual_profit = None
    item: dict[str, Any] = {
        "merchant_id": merchant_id,
        "display_name": _safe_label(raw.get("display_name"), merchant_id),
        "roles": sorted(normalized_roles),
        "status": _enum(raw.get("status"), _MERCHANT_STATES, "unknown"),
        "session_status": _enum(raw.get("session_status"), _COMPONENT_STATES, "unknown"),
        "proxy_status": _enum(raw.get("proxy_status"), _COMPONENT_STATES, "unknown"),
        "freshness": _enum(raw.get("freshness"), _FRESHNESS_STATES, "unknown"),
        "last_observed_at": _timestamp(raw.get("last_observed_at")),
        "age_seconds": _nonnegative_integer(raw.get("age_seconds")),
        "error_rate": _ratio(raw.get("error_rate")),
        "last_error_class": _safe_code(raw.get("last_error_class")),
        "last_error_at": _timestamp(raw.get("last_error_at")),
        "cooldown_until": _timestamp(raw.get("cooldown_until")),
        "current_pin_model": _safe_model(raw.get("current_pin_model")),
        "current_pin_channel_id": _safe_identifier(raw.get("current_pin_channel_id")),
        "pin_rates": _sanitize_pin_rates(raw.get("pin_rates")),
        "listing_rates": _sanitize_listing_rates(raw.get("listing_rates")),
        "profit_status": profit_status,
        "sales_status": _enum(raw.get("sales_status"), _LEDGER_STATES, "unavailable"),
        "pending_binding": _boolean(raw.get("pending_binding")),
        "profit_margin": (
            _optional_ratio(raw.get("profit_margin")) if actual_profit is not None else None
        ),
        "actual_profit_micros": actual_profit,
        "honest_profit_micros": _optional_integer(raw.get("honest_profit_micros")),
        "attribution_coverage": _optional_ratio(raw.get("attribution_coverage")),
        "withdraw_account_state": _enum(
            raw.get("withdraw_account_state"),
            _WITHDRAW_ACCOUNT_STATES,
            "missing",
        ),
        "withdraw_account_network": _safe_identifier(raw.get("withdraw_account_network")),
        "withdraw_account_masked": _safe_optional_label(raw.get("withdraw_account_masked")),
        "listing_last_observed_at": _timestamp(raw.get("listing_last_observed_at")),
        "retirement_state": _enum(raw.get("retirement_state"), _RETIREMENT_STATES, ""),
        "retirement_phase": _retirement_phase(
            raw.get("retirement_state"),
            archive_mode=_boolean(raw.get("retirement_archive_mode")),
        ),
        "retirement_archive_mode": _boolean(raw.get("retirement_archive_mode")),
        "retirement_requested_at": _timestamp(raw.get("retirement_requested_at")),
        "retirement_updated_at": _timestamp(raw.get("retirement_updated_at")),
        "retirement_completed_at": _timestamp(raw.get("retirement_completed_at")),
        "retirement_error_class": _safe_code(raw.get("retirement_error_class")),
    }
    item.update({field: _integer(raw.get(field)) for field in _MERCHANT_MONEY_FIELDS})
    item.update({field: _nonnegative_integer(raw.get(field)) for field in _MERCHANT_COUNT_FIELDS})
    return item


def _retirement_phase(value: object, *, archive_mode: bool = True) -> str:
    if not archive_mode:
        return "none"
    state = str(value or "")
    if state in {"earning", "checking", "transfer_unknown"}:
        return "pre_retirement"
    if state in {"transferred", "binding", "taking_down", "role_switch"}:
        return "retiring"
    if state == "draining":
        return "draining"
    if state == "completed":
        return "archived"
    return "none"


def _sanitize_pin_rates(raw: Any) -> list[dict[str, Any]]:
    if isinstance(raw, (str, bytes)) or not isinstance(raw, Sequence):
        return []
    items: list[dict[str, Any]] = []
    for value in raw[:100]:
        if not isinstance(value, Mapping):
            continue
        item = {
            "token_id": _safe_identifier(value.get("token_id")),
            "model": _safe_model(value.get("model")),
            "channel_id": _safe_identifier(value.get("channel_id")),
            "rate_multiplier": _optional_nonnegative_number(value.get("rate_multiplier")),
            "expected_cost_multiplier": _optional_nonnegative_number(
                value.get("expected_cost_multiplier")
            ),
            "market_final_multiplier": _optional_nonnegative_number(
                value.get("market_final_multiplier")
            ),
            "realtime_ratio_usd": _optional_nonnegative_number(value.get("realtime_ratio_usd")),
            "realtime_ratio_cny": _optional_nonnegative_number(value.get("realtime_ratio_cny")),
            "exchange_rate": _optional_nonnegative_number(value.get("exchange_rate")),
            "decision_status": _safe_code(value.get("decision_status")),
            "confirmed_at": _timestamp(value.get("confirmed_at")),
            "quote_observed_at": _timestamp(value.get("quote_observed_at")),
        }
        if item["model"] or item["channel_id"] or item["token_id"]:
            items.append(item)
    return items


def _sanitize_listing_rates(raw: Any) -> list[dict[str, Any]]:
    if isinstance(raw, (str, bytes)) or not isinstance(raw, Sequence):
        return []
    items: list[dict[str, Any]] = []
    for value in raw[:100]:
        if not isinstance(value, Mapping):
            continue
        item = {
            "listing_id": _safe_identifier(value.get("listing_id")),
            "channel_id": _safe_identifier(value.get("channel_id")),
            "model": _safe_model(value.get("model")),
            "managed_state": _safe_code(value.get("managed_state")),
            "control_state": _safe_code(value.get("control_state")),
            "desired_delta": _optional_nonnegative_number(value.get("desired_delta")),
            "observed_delta": _optional_nonnegative_number(value.get("observed_delta")),
            "price_multiplier": _optional_nonnegative_number(value.get("price_multiplier")),
            "market_price_multiplier": _optional_nonnegative_number(
                value.get("market_price_multiplier")
            ),
            "price_multiplier_cny": _optional_nonnegative_number(value.get("price_multiplier_cny")),
            "exchange_rate": _optional_nonnegative_number(value.get("exchange_rate")),
            "input_price_micros": _nonnegative_integer(value.get("input_price_micros")),
            "output_price_micros": _nonnegative_integer(value.get("output_price_micros")),
            "cache_read_price_micros": _nonnegative_integer(value.get("cache_read_price_micros")),
            "cache_write_price_micros": _nonnegative_integer(value.get("cache_write_price_micros")),
            "status": _optional_nonnegative_integer(value.get("status")),
            "availability": _optional_nonnegative_integer(value.get("availability")),
            "visibility": _optional_nonnegative_integer(value.get("visibility")),
            "health_state": _enum(value.get("health_state"), _CHANNEL_HEALTH_STATES, "unknown"),
            "health_observer_count": _nonnegative_integer(value.get("health_observer_count")),
            "health_confirmation_count": _nonnegative_integer(
                value.get("health_confirmation_count")
            ),
            "market_available": bool(value.get("market_available")),
            "route_status": _safe_code(value.get("route_status")),
            "circuit_state": _safe_code(value.get("circuit_state")),
            "circuit_probe_status": _safe_code(value.get("circuit_probe_status")),
            "circuit_last_error_kind": _safe_code(value.get("circuit_last_error_kind")),
            "circuit_probe_success_required": _optional_nonnegative_integer(
                value.get("circuit_probe_success_required")
            ),
            "circuit_probe_success_streak": _optional_nonnegative_integer(
                value.get("circuit_probe_success_streak")
            ),
            "circuit_scope": _safe_code(value.get("circuit_scope")),
            "circuit_recovery_mode": _safe_code(value.get("circuit_recovery_mode")),
            "circuit_cooling_until": _timestamp(value.get("circuit_cooling_until")),
            "circuit_last_cooling_at": _timestamp(value.get("circuit_last_cooling_at")),
            "circuit_last_probe_at": _timestamp(value.get("circuit_last_probe_at")),
            "circuit_next_probe_at": _timestamp(value.get("circuit_next_probe_at")),
            "cooldown_state": _safe_code(value.get("cooldown_state")),
            "cooldown_label": _safe_text(value.get("cooldown_label"), 128),
            "cooldown_remaining_seconds": _optional_nonnegative_integer(
                value.get("cooldown_remaining_seconds")
            ),
            "cooldown_expected_recovery_at": _timestamp(value.get("cooldown_expected_recovery_at")),
            "cooldown_next_probe_at": _timestamp(value.get("cooldown_next_probe_at")),
            "cooldown_sources": _safe_source_list(value.get("cooldown_sources"), maximum=20),
            "smart_routing_status": _safe_code(value.get("smart_routing_status")),
            "smart_routing_labels": _safe_code_list(value.get("smart_routing_labels"), maximum=20),
            "last_success_at": _timestamp(value.get("last_success_at")),
            "last_failure_at": _timestamp(value.get("last_failure_at")),
            "last_failure_code": _safe_code(value.get("last_failure_code")),
            "last_sample_at": _timestamp(value.get("last_sample_at")),
            "sample_count": _optional_nonnegative_integer(value.get("sample_count")),
            "recent_success_rate": _optional_nonnegative_integer(value.get("recent_success_rate")),
            "recent_p50_ms": _optional_nonnegative_number(value.get("recent_p50_ms")),
            "p50_ttft_ms": _optional_nonnegative_number(value.get("p50_ttft_ms")),
            "health_observed_at": _timestamp(value.get("health_observed_at")),
            "growth_enabled": bool(value.get("growth_enabled")),
            "growth_action_type": _enum(value.get("growth_action_type"), _GROWTH_ACTION_TYPES, ""),
            "growth_action_state": _enum(
                value.get("growth_action_state"), _GROWTH_ACTION_STATES, ""
            ),
            "growth_request_count": _nonnegative_integer(value.get("growth_request_count")),
            "growth_success_count": _nonnegative_integer(value.get("growth_success_count")),
            "growth_failure_count": _nonnegative_integer(value.get("growth_failure_count")),
            "growth_estimated_cost_micros": _nonnegative_integer(
                value.get("growth_estimated_cost_micros")
            ),
            "growth_actual_cost_micros": _nonnegative_integer(
                value.get("growth_actual_cost_micros")
            ),
            "growth_error_class": _safe_code(value.get("growth_error_class")),
            "growth_started_at": _timestamp(value.get("growth_started_at")),
            "growth_completed_at": _timestamp(value.get("growth_completed_at")),
            "growth_next_allowed_at": _timestamp(value.get("growth_next_allowed_at")),
            "observed_at": _timestamp(value.get("observed_at")),
        }
        if item["model"] or item["listing_id"]:
            items.append(item)
    return items


def _integer(value: Any) -> int:
    if isinstance(value, bool) or value is None:
        return 0
    if isinstance(value, Decimal):
        return int(value)
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return 0


def _nonnegative_integer(value: Any) -> int:
    return max(0, _integer(value))


def _optional_nonnegative_integer(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if result >= 0 else None


def _optional_positive_integer(value: Any) -> int | None:
    result = _optional_nonnegative_integer(value)
    return result if result is not None and result > 0 else None


def _positive_integer_list(value: Any, *, maximum: int) -> list[int]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        return []
    return [
        parsed
        for item in value[:maximum]
        if (parsed := _optional_positive_integer(item)) is not None
    ]


def _nonnegative_decimal_text(value: Any) -> str | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = Decimal(str(value))
    except Exception:
        return None
    if not result.is_finite() or result < 0:
        return None
    return format(result, "f")


def _positive_decimal_text(value: Any) -> str | None:
    result = _nonnegative_decimal_text(value)
    if result is None or Decimal(result) <= 0:
        return None
    return result


def _economic_floor_text(value: Any, multiplier: Decimal) -> str | None:
    source = _positive_decimal_text(value)
    if source is None or not multiplier.is_finite() or multiplier <= 0:
        return None
    quantum = Decimal("0.000001")
    result = (
        (Decimal(source) * multiplier / quantum).to_integral_value(rounding=ROUND_CEILING)
        * quantum
    )
    return format(result, ".6f")


def _signed_decimal_text(value: Any) -> str | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = Decimal(str(value))
    except Exception:
        return None
    if not result.is_finite():
        return None
    return format(result, "f")


def _http_status(value: Any) -> int | None:
    result = _optional_nonnegative_integer(value)
    return result if result is not None and 100 <= result <= 599 else None


def _optional_integer(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _strict_optional_integer(value: Any) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise CredentialAccessError("credential_binding_mismatch")
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise CredentialAccessError("credential_binding_mismatch") from exc
    if result < 1:
        raise CredentialAccessError("credential_binding_mismatch")
    return result


def _boolean(value: Any) -> bool:
    return value is True or (isinstance(value, int) and not isinstance(value, bool) and value == 1)


def _ratio(value: Any) -> float:
    if isinstance(value, bool) or value is None:
        return 0.0
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    if result != result or result in {float("inf"), float("-inf")}:
        return 0.0
    return result


def _optional_ratio(value: Any) -> float | None:
    if value is None:
        return None
    return _ratio(value)


def _optional_nonnegative_number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if result != result or result in {float("inf"), float("-inf")} or result < 0:
        return None
    return result


def _enum(value: Any, allowed: frozenset[str], default: str) -> str:
    normalized = str(value or "").strip().lower()
    return normalized if normalized in allowed else default


def _safe_identifier(value: Any) -> str:
    if isinstance(value, bool) or value is None:
        return ""
    normalized = str(value).strip()
    if not normalized or len(normalized) > 64 or _SECRET_LIKE.search(normalized):
        return ""
    return normalized if re.fullmatch(r"[A-Za-z0-9_.:-]+", normalized) else ""


def _safe_sha256(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    return normalized if re.fullmatch(r"[0-9a-f]{64}", normalized) else ""


def _safe_label(value: Any, merchant_id: str) -> str:
    normalized = " ".join(str(value or "").split())
    if (
        not normalized
        or len(normalized) > 64
        or "@" in normalized
        or _SECRET_LIKE.search(normalized)
    ):
        return merchant_id or "merchant"
    return normalized


def _safe_optional_label(value: Any) -> str:
    normalized = " ".join(str(value or "").split())
    if (
        not normalized
        or len(normalized) > 64
        or "@" in normalized
        or _SECRET_LIKE.search(normalized)
    ):
        return ""
    return normalized


def _safe_code(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    return normalized if _SAFE_CODE.fullmatch(normalized) else ""


def _safe_text(value: Any, maximum: int) -> str:
    normalized = " ".join(str(value or "").split())
    if not normalized or len(normalized) > maximum or _SECRET_LIKE.search(normalized):
        return ""
    return normalized


def _safe_code_list(value: Any, *, maximum: int) -> list[str]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        return []
    return [code for item in value[:maximum] if (code := _safe_code(item))]


def _safe_source_list(value: Any, *, maximum: int) -> list[str]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        return []
    result: list[str] = []
    for item in value[:maximum]:
        if isinstance(item, Mapping):
            parts = [
                _safe_text(item.get(key), 64)
                for key in ("source", "kind", "scope", "label", "state")
            ]
            normalized = ":".join(part for part in parts if part)
        else:
            normalized = _safe_text(item, 128)
        if normalized:
            result.append(normalized)
    return result


def _safe_model(value: Any) -> str:
    normalized = str(value or "").strip()
    return normalized if _SAFE_MODEL.fullmatch(normalized) else ""


def _timestamp(value: Any) -> str | None:
    if isinstance(value, datetime):
        current = value if value.tzinfo else value.replace(tzinfo=UTC)
        return current.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if len(normalized) > 40 or not re.fullmatch(r"[0-9T:.+Z-]+", normalized):
        return None
    return normalized or None


def _timestamp_at_or_after(value: str | None, floor: str | None) -> bool:
    if value is None or floor is None:
        return False
    try:
        observed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        required = datetime.fromisoformat(floor.replace("Z", "+00:00"))
    except ValueError:
        return False
    return observed >= required


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _content_security_policy() -> str:
    return (
        "default-src 'self'; style-src 'self' 'unsafe-inline'; "
        "script-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; "
        "object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
    )
