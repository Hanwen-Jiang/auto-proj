from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import secrets
import stat
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .client import A6ApiClient
from .errors import A6AutoError
from .models import DEFAULT_QUOTA_PER_UNIT, SessionRecord
from .storage import SessionStore, write_private_json
from .trade_authenticated_client import (
    AuthenticatedSessionError,
    authenticated_a6_client,
)
from .trade_db import metadata_from_session, upsert_merchant_metadata
from .trade_profit_settings import (
    PostgresModelProfitSettingStore,
    profit_percent_to_bps,
)

UTC = timezone.utc
_MODEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,95}$")
_ACTIVE_SUPPLIER_STATUS = "supplier_submitted"


class DashboardManagementError(A6AutoError):
    """Stable dashboard operation error that never includes credentials or remote bodies."""


class DashboardManagementService:
    def __init__(
        self,
        connection_factory: Callable[[], Any],
        *,
        sessions_dir: Path,
        proxy_map_file: Path,
        proxy_pool_file: Path,
        batch_state_file: Path | None = None,
        client_factory: Callable[[str, str], A6ApiClient] | None = None,
    ) -> None:
        self.connection_factory = connection_factory
        self.sessions_dir = sessions_dir.expanduser().resolve()
        self.proxy_map_file = proxy_map_file.expanduser().resolve()
        self.proxy_pool_file = proxy_pool_file.expanduser().resolve()
        self.batch_state_file = (
            batch_state_file.expanduser().resolve() if batch_state_file is not None else None
        )
        self.client_factory = client_factory or (
            lambda origin, proxy: A6ApiClient(origin, proxy_url=proxy)
        )
        self._validate_private_directory(self.sessions_dir)
        self.lock_file = self.sessions_dir.parent / "dashboard-management.lock"

    def add_account(self, username: object, password: object) -> dict[str, Any]:
        login_name = _required_text(username, maximum=256, error="account_username_invalid")
        secret = _required_text(password, maximum=4096, error="account_password_invalid")
        with self._operation_lock():
            existing_session = self._session_for_login(login_name)
            proxy_map = self._proxy_map()
            if existing_session is not None and existing_session.user_id in proxy_map:
                proxy = proxy_map[int(existing_session.user_id)]
            else:
                proxy = self._available_proxy(proxy_map)
            client = self.client_factory("https://a6api.com", proxy)
            logged_in = client.login(login_name, secret)
            if not isinstance(logged_in, Mapping):
                raise DashboardManagementError("account_login_failed")
            user_id = _positive_integer(logged_in.get("id"), "account_identity_invalid")
            current = client.self_user(user_id)
            if not isinstance(current, Mapping) or _positive_integer(
                current.get("id"), "account_identity_invalid"
            ) != user_id:
                raise DashboardManagementError("account_identity_invalid")

            existing = self._merchant_binding_by_user(user_id)
            if existing is not None:
                store, record = self._record_from_binding(existing)
                record.username = login_name
                record.password = secret
                record.cookies = client.export_cookies()
                record.updated_at = _now()
                store.save(record)
                self._bind_proxy(proxy_map, user_id, proxy)
                self._import_session(store.path, proxy)
                return {
                    "merchant_id": str(existing["merchant_id"]),
                    "a6_user_id": str(user_id),
                    "status": "existing",
                    "collection": "scheduled",
                }

            profile = self._optional_supplier_profile(client, user_id)
            supplier_id = _optional_positive_integer(profile.get("id"))
            session_status = _ACTIVE_SUPPLIER_STATUS if supplier_id else "registered"
            email = _optional_text(current.get("email"), maximum=512)
            if not email:
                email = login_name if "@" in login_name else f"account-{user_id}@local.invalid"
            quota_per_unit = _optional_positive_integer(current.get("quota_per_unit"))
            quota_per_unit = quota_per_unit or DEFAULT_QUOTA_PER_UNIT
            session_path = self.sessions_dir / f"session-manual-{user_id}.json"
            if session_path.exists():
                raise DashboardManagementError("account_session_conflict")
            record = SessionRecord(
                schema_version=1,
                origin="https://a6api.com",
                email=email,
                username=login_name,
                password=secret,
                user_id=user_id,
                quota_per_unit=quota_per_unit,
                cookies=client.export_cookies(),
                status=session_status,
                supplier_id=supplier_id,
                created_at=_now(),
                updated_at=_now(),
            )
            SessionStore(session_path).save(record)
            try:
                self._bind_proxy(proxy_map, user_id, proxy)
                self._import_session(session_path, proxy)
                binding = self._merchant_binding_by_user(user_id)
                if binding is None:
                    raise DashboardManagementError("account_import_readback_failed")
            except Exception:
                if session_path.exists():
                    session_path.unlink()
                current_map = self._proxy_map()
                if current_map.get(user_id) == proxy:
                    current_map.pop(user_id, None)
                    self._write_proxy_map(current_map)
                raise
            return {
                "merchant_id": str(binding["merchant_id"]),
                "a6_user_id": str(user_id),
                "status": "imported",
                "supplier_status": session_status,
                "collection": "scheduled",
            }

    def get_inventory(self, merchant_id: int) -> dict[str, Any]:
        client, record = self._authenticated_client(merchant_id)
        profile = self._stored_supplier_profile(merchant_id)
        if profile is None:
            profile = self._optional_supplier_profile(client, int(record.user_id or 0))
            if profile:
                self._persist_supplier_profile(merchant_id, profile)
        if not profile:
            return {
                "merchant_id": str(merchant_id),
                "supplier": None,
                "channels": [],
                "supplier_state": "not_active",
            }
        channels = client.supplier_channels(int(record.user_id or 0))
        listings = self._all_listings(client, int(record.user_id or 0))
        listings_by_channel: dict[int, list[dict[str, Any]]] = {}
        for listing in listings:
            channel_id = _optional_positive_integer(listing.get("channel_id"))
            if channel_id:
                listings_by_channel.setdefault(channel_id, []).append(
                    _public_listing(listing)
                )
        public_channels = []
        for channel in channels:
            channel_id = _positive_integer(channel.get("id"), "inventory_channel_invalid")
            public_channels.append(
                {
                    **_public_channel(channel),
                    "listings": listings_by_channel.pop(channel_id, []),
                }
            )
        orphaned = [item for values in listings_by_channel.values() for item in values]
        return {
            "merchant_id": str(merchant_id),
            "supplier_state": "active",
            "supplier": _public_supplier(profile),
            "channels": public_channels,
            "orphaned_listings": orphaned,
        }

    def update_channel(
        self,
        merchant_id: int,
        channel_id: int,
        patch: Mapping[str, Any],
    ) -> dict[str, Any]:
        allowed = {"nickname": 100, "description": 1000}
        if not isinstance(patch, Mapping) or set(patch) - {*allowed, "models"}:
            raise DashboardManagementError("supplier_channel_patch_invalid")
        text_patch = {field: patch[field] for field in allowed if field in patch}
        desired = (
            _text_patch(
                text_patch,
                allowed=allowed,
                error="supplier_channel_patch_invalid",
            )
            if text_patch
            else {}
        )
        if "models" in patch:
            desired["models"] = ",".join(_models(patch.get("models")))
        if not desired:
            raise DashboardManagementError("supplier_channel_patch_invalid")
        client, record = self._authenticated_client(merchant_id)
        channels = client.supplier_channels(int(record.user_id or 0))
        current = _item_by_id(channels, channel_id)
        if current is None:
            raise DashboardManagementError("supplier_channel_not_found")
        remote_id = _optional_positive_integer(current.get("binding_id")) or channel_id
        client.update_supplier_channel(int(record.user_id or 0), remote_id, desired)
        refreshed = _item_by_id(
            client.supplier_channels(int(record.user_id or 0)), channel_id
        )
        if refreshed is None or not _channel_matches(refreshed, desired):
            raise DashboardManagementError("supplier_channel_update_readback_failed")
        return {"merchant_id": str(merchant_id), "channel": _public_channel(refreshed)}

    def update_listing(
        self,
        merchant_id: int,
        listing_id: int,
        patch: Mapping[str, Any],
    ) -> dict[str, Any]:
        desired = _text_patch(
            patch,
            allowed={"display_name": 100, "description": 1000},
            error="supplier_listing_patch_invalid",
            allow_empty=True,
        )
        client, record = self._authenticated_client(merchant_id)
        user_id = int(record.user_id or 0)
        listings = self._all_listings(client, user_id)
        current = _item_by_id(listings, listing_id)
        if current is None:
            raise DashboardManagementError("supplier_listing_not_found")
        payload = {
            field: current.get(field)
            for field in (
                "charge_type",
                "input_price_micros",
                "output_price_micros",
                "cache_read_price_micros",
                "cache_write_price_micros",
                "display_name",
                "description",
            )
        }
        payload.update(desired)
        if not isinstance(payload.get("charge_type"), str):
            raise DashboardManagementError("supplier_listing_inventory_invalid")
        for field in (
            "input_price_micros",
            "output_price_micros",
            "cache_read_price_micros",
            "cache_write_price_micros",
        ):
            payload[field] = _nonnegative_integer(
                payload.get(field), "supplier_listing_inventory_invalid"
            )
        client.update_supplier_listing(user_id, listing_id, payload)
        refreshed = _item_by_id(self._all_listings(client, user_id), listing_id)
        if refreshed is None or any(
            str(refreshed.get(field) or "").strip() != expected
            for field, expected in desired.items()
        ):
            raise DashboardManagementError("supplier_listing_update_readback_failed")
        return {"merchant_id": str(merchant_id), "listing": _public_listing(refreshed)}

    def request_retirement(self, merchant_id: int) -> dict[str, Any]:
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT id, quota_per_unit, roles
                    FROM a6_trade.a6_merchants
                    WHERE id = %s FOR UPDATE
                    """,
                    (merchant_id,),
                )
                merchant = _row(cursor)
                if merchant is None:
                    raise DashboardManagementError("merchant_not_found")
                if tuple(merchant.get("roles") or ()) == ("observer",):
                    return {
                        "merchant_id": str(merchant_id),
                        "phase": "archived",
                        "state": "completed",
                    }
                cursor.execute(
                    """
                    INSERT INTO a6_trade.a6_seller_retirement_plans (
                        merchant_id, threshold_quota, archive_mode, requested_at
                    )
                    VALUES (%s, %s, true, now())
                    ON CONFLICT (merchant_id) DO UPDATE SET
                        state = CASE
                            WHEN a6_seller_retirement_plans.state = 'completed'
                            THEN 'role_switch'
                            ELSE a6_seller_retirement_plans.state
                        END,
                        archive_mode = true,
                        requested_at = CASE
                            WHEN a6_seller_retirement_plans.state = 'completed'
                            THEN now()
                            ELSE coalesce(a6_seller_retirement_plans.requested_at, now())
                        END,
                        completed_at = CASE
                            WHEN a6_seller_retirement_plans.state = 'completed'
                            THEN NULL
                            ELSE a6_seller_retirement_plans.completed_at
                        END,
                        last_error_class = NULL,
                        retry_after = NULL,
                        updated_at = now()
                    RETURNING state, archive_mode, requested_at, updated_at,
                              completed_at, last_error_class
                    """,
                    (merchant_id, int(merchant["quota_per_unit"])),
                )
                plan = _row(cursor)
                cursor.execute(
                    """
                    INSERT INTO a6_trade.a6_seller_retirement_events (
                        merchant_id, event_type, from_state, to_state, details
                    )
                    SELECT %s, 'dashboard_safe_retirement_requested', %s, %s,
                           jsonb_build_object('requested_by', 'dashboard')
                    WHERE NOT EXISTS (
                        SELECT 1 FROM a6_trade.a6_seller_retirement_events
                        WHERE merchant_id = %s
                          AND event_type = 'dashboard_safe_retirement_requested'
                    )
                    """,
                    (merchant_id, plan["state"], plan["state"], merchant_id),
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            _close(connection)
        return {"merchant_id": str(merchant_id), **_retirement_projection(plan)}

    def update_model_profit(self, model: str, profit_percent: object) -> dict[str, Any]:
        bps = profit_percent_to_bps(profit_percent)
        setting = PostgresModelProfitSettingStore(self.connection_factory).put(
            model,
            bps,
            updated_by="dashboard",
        )
        return setting.public()

    def _authenticated_client(self, merchant_id: int) -> tuple[A6ApiClient, SessionRecord]:
        binding = self._merchant_binding(merchant_id)
        if binding is None:
            raise DashboardManagementError("merchant_not_found")
        store, record = self._record_from_binding(binding)
        user_id = int(record.user_id or 0)
        proxy = self._proxy_map().get(user_id)
        if not proxy:
            raise DashboardManagementError("merchant_proxy_missing")
        try:
            client, current_record = authenticated_a6_client(
                store.path,
                expected_user_id=user_id,
                proxy_url=proxy,
                client_factory=lambda current, fixed_proxy: self.client_factory(
                    current.origin,
                    fixed_proxy,
                ),
            )
        except AuthenticatedSessionError as exc:
            code = {
                "session_relogin_failed": "merchant_relogin_failed",
                "session_user_identity_mismatch": "merchant_identity_mismatch",
                "session_user_id_missing": "merchant_session_mismatch",
                "session_user_id_invalid": "merchant_session_mismatch",
                "session_not_refreshable": "merchant_session_mismatch",
                "session_proxy_missing": "merchant_proxy_missing",
            }.get(exc.code)
            if code is None:
                raise
            raise DashboardManagementError(code) from exc
        return client, current_record

    def _record_from_binding(
        self, binding: Mapping[str, Any]
    ) -> tuple[SessionStore, SessionRecord]:
        basename = Path(str(binding.get("session_ref") or "")).name
        path = (self.sessions_dir / basename).resolve()
        if not basename or path.parent != self.sessions_dir or not path.is_file():
            raise DashboardManagementError("merchant_session_unavailable")
        store = SessionStore(path)
        record = store.load()
        if record.user_id != int(binding.get("a6_user_id") or 0):
            raise DashboardManagementError("merchant_session_mismatch")
        return store, record

    def _merchant_binding(self, merchant_id: int) -> Mapping[str, Any] | None:
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT id AS merchant_id, a6_user_id, supplier_id, session_ref
                    FROM a6_trade.a6_merchants
                    WHERE id = %s AND status <> 'abandoned'
                    """,
                    (merchant_id,),
                )
                return _row(cursor)
        finally:
            _close(connection)

    def _merchant_binding_by_user(self, user_id: int) -> Mapping[str, Any] | None:
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT id AS merchant_id, a6_user_id, supplier_id, session_ref
                    FROM a6_trade.a6_merchants WHERE a6_user_id = %s
                    """,
                    (user_id,),
                )
                return _row(cursor)
        finally:
            _close(connection)

    def _stored_supplier_profile(self, merchant_id: int) -> dict[str, Any] | None:
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT supplier_id AS id,
                           supplier_name AS name,
                           supplier_description AS description,
                           supplier_profile_observed_at AS observed_at
                    FROM a6_trade.a6_merchants
                    WHERE id = %s
                    """,
                    (merchant_id,),
                )
                profile = _row(cursor)
        finally:
            _close(connection)
        if profile is None or _timestamp(profile.get("observed_at")) is None:
            return None
        return _public_supplier(profile)

    def _persist_supplier_profile(
        self,
        merchant_id: int,
        profile: Mapping[str, Any],
    ) -> None:
        supplier = _public_supplier(profile)
        supplier_id = _positive_integer(supplier.get("id"), "supplier_profile_invalid")
        observed_at = _now()
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE a6_trade.a6_merchants
                    SET supplier_id = %s,
                        supplier_name = %s,
                        supplier_description = %s,
                        supplier_profile_observed_at = %s,
                        updated_at = now()
                    WHERE id = %s
                      AND (
                          supplier_profile_observed_at IS NULL
                          OR supplier_profile_observed_at <= %s
                      )
                    """,
                    (
                        supplier_id,
                        supplier["name"],
                        supplier["description"],
                        observed_at,
                        merchant_id,
                        observed_at,
                    ),
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            _close(connection)

    def _import_session(self, path: Path, proxy: str) -> None:
        metadata = metadata_from_session(
            path,
            roles=("seller",),
            proxy_metadata=_proxy_metadata(proxy),
        )
        connection = self.connection_factory()
        try:
            upsert_merchant_metadata(connection, (metadata,))
        finally:
            _close(connection)

    def _session_for_login(self, login_name: str) -> SessionRecord | None:
        for path in sorted(self.sessions_dir.glob("session-*.json")):
            try:
                record = SessionStore(path).load()
            except A6AutoError:
                continue
            if login_name in {record.username, record.email}:
                return record
        return None

    def _optional_supplier_profile(
        self, client: A6ApiClient, user_id: int
    ) -> dict[str, Any]:
        try:
            return client.supplier_me(user_id)
        except A6AutoError as exc:
            if str(exc) == "supplier_self_failed":
                return {}
            raise

    @staticmethod
    def _all_listings(client: A6ApiClient, user_id: int) -> list[dict[str, Any]]:
        by_id: dict[int, dict[str, Any]] = {}
        for page in range(1, 101):
            batch = client.supplier_listings(user_id, page=page, size=100)
            items = batch.get("items")
            if not isinstance(items, Sequence) or isinstance(items, (str, bytes)):
                raise DashboardManagementError("supplier_listing_inventory_invalid")
            for item in items:
                if not isinstance(item, Mapping):
                    continue
                item_id = _optional_positive_integer(item.get("id"))
                if item_id:
                    by_id[item_id] = dict(item)
            total = _optional_positive_integer(batch.get("total")) or 0
            if not items or (total and len(by_id) >= total) or len(items) < 100:
                return [by_id[item_id] for item_id in sorted(by_id)]
        raise DashboardManagementError("supplier_listing_inventory_incomplete")

    def _proxy_map(self) -> dict[int, str]:
        payload = _read_private_json(self.proxy_map_file, expected=dict)
        result: dict[int, str] = {}
        for raw_user_id, raw_proxy in payload.items():
            try:
                user_id = int(raw_user_id)
            except (TypeError, ValueError):
                raise DashboardManagementError("proxy_map_invalid") from None
            if user_id < 1 or not isinstance(raw_proxy, str):
                raise DashboardManagementError("proxy_map_invalid")
            result[user_id] = _proxy_url(raw_proxy)
        if len(set(result.values())) != len(result):
            raise DashboardManagementError("proxy_map_duplicate")
        return result

    def _write_proxy_map(self, values: Mapping[int, str]) -> None:
        write_private_json(
            self.proxy_map_file,
            {str(key): values[key] for key in sorted(values)},
        )

    def _bind_proxy(
        self, values: dict[int, str], user_id: int, proxy: str
    ) -> None:
        if user_id in values and values[user_id] != proxy:
            raise DashboardManagementError("proxy_user_conflict")
        if any(key != user_id and value == proxy for key, value in values.items()):
            raise DashboardManagementError("proxy_lease_conflict")
        values[user_id] = proxy
        self._write_proxy_map(values)

    def _available_proxy(self, proxy_map: Mapping[int, str]) -> str:
        pool = _read_private_json(self.proxy_pool_file, expected=list)
        proxies = tuple(dict.fromkeys(_proxy_url(value) for value in pool))
        reserved = set(proxy_map.values()) | self._batch_reserved_proxies(proxies)
        available = [proxy for proxy in proxies if proxy not in reserved]
        if not available:
            raise DashboardManagementError("proxy_pool_exhausted")
        return secrets.choice(available)

    def _batch_reserved_proxies(self, proxies: Sequence[str]) -> set[str]:
        if self.batch_state_file is None or not self.batch_state_file.exists():
            return set()
        payload = _read_private_json(self.batch_state_file, expected=dict)
        fingerprints = {
            str(value)
            for value in payload.get("allocated_proxy_fingerprints") or ()
            if str(value)
        }
        return {
            proxy
            for proxy in proxies
            if hashlib.sha256(proxy.encode()).hexdigest()[:12] in fingerprints
        }

    @contextmanager
    def _operation_lock(self) -> Iterator[None]:
        descriptor = os.open(self.lock_file, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            os.fchmod(descriptor, 0o600)
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    @staticmethod
    def _validate_private_directory(path: Path) -> None:
        try:
            info = path.stat()
        except OSError as exc:
            raise DashboardManagementError("sessions_directory_unavailable") from exc
        if not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o700:
            raise DashboardManagementError("sessions_directory_permissions_invalid")


def _public_supplier(value: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": str(_optional_positive_integer(value.get("id")) or ""),
        "name": _optional_text(value.get("name"), maximum=256),
        "description": _optional_text(value.get("description"), maximum=2000),
        "contact_info": _optional_text(value.get("contact_info"), maximum=512),
    }


def _public_channel(value: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": str(_positive_integer(value.get("id"), "inventory_channel_invalid")),
        "binding_id": str(_optional_positive_integer(value.get("binding_id")) or ""),
        "name": _optional_text(value.get("name"), maximum=256),
        "nickname": _optional_text(value.get("nickname"), maximum=256),
        "description": _optional_text(value.get("description"), maximum=2000),
        "models": list(_models(value.get("models"), allow_empty=True)),
        "status": str(value.get("status") if value.get("status") is not None else ""),
        "availability": str(
            value.get("availability") if value.get("availability") is not None else ""
        ),
    }


def _public_listing(value: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": str(_positive_integer(value.get("id"), "inventory_listing_invalid")),
        "channel_id": str(
            _positive_integer(value.get("channel_id"), "inventory_listing_invalid")
        ),
        "own_model": _model(value.get("own_model")),
        "platform_model": _model(value.get("platform_model")),
        "display_name": _optional_text(value.get("display_name"), maximum=256),
        "description": _optional_text(value.get("description"), maximum=2000),
        "status": str(value.get("status") if value.get("status") is not None else ""),
        "availability": str(
            value.get("availability") if value.get("availability") is not None else ""
        ),
        "visibility": str(
            value.get("visibility") if value.get("visibility") is not None else ""
        ),
    }


def _retirement_projection(value: Mapping[str, Any]) -> dict[str, Any]:
    state = str(value.get("state") or "")
    if state in {"earning", "checking", "transfer_unknown"}:
        phase = "pre_retirement"
    elif state in {"transferred", "binding", "taking_down", "role_switch"}:
        phase = "retiring"
    elif state == "draining":
        phase = "draining"
    elif state == "completed":
        phase = "archived"
    else:
        phase = "none"
    return {
        "state": state,
        "phase": phase,
        "archive_mode": value.get("archive_mode") is True,
        "requested_at": _timestamp(value.get("requested_at")),
        "updated_at": _timestamp(value.get("updated_at")),
        "completed_at": _timestamp(value.get("completed_at")),
        "last_error_class": _optional_text(value.get("last_error_class"), maximum=128),
    }


def _text_patch(
    patch: Mapping[str, Any],
    *,
    allowed: Mapping[str, int],
    error: str,
    allow_empty: bool = False,
) -> dict[str, str]:
    if not isinstance(patch, Mapping) or set(patch) - set(allowed):
        raise DashboardManagementError(error)
    result: dict[str, str] = {}
    for field, maximum in allowed.items():
        if field not in patch:
            continue
        value = patch[field]
        if not isinstance(value, str):
            raise DashboardManagementError(error)
        normalized = value.strip()
        if (not normalized and not allow_empty) or len(normalized) > maximum:
            raise DashboardManagementError(error)
        result[field] = normalized
    if not result:
        raise DashboardManagementError(error)
    return result


def _models(value: object, *, allow_empty: bool = False) -> tuple[str, ...]:
    if isinstance(value, str):
        source: Sequence[object] = value.split(",")
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        source = value
    else:
        source = ()
    result = tuple(
        dict.fromkeys(str(item).strip() for item in source if str(item).strip())
    )
    if (not result and not allow_empty) or len(result) > 100 or any(
        _MODEL.fullmatch(item) is None for item in result
    ):
        raise DashboardManagementError("supplier_channel_models_invalid")
    return result


def _channel_matches(channel: Mapping[str, Any], desired: Mapping[str, str]) -> bool:
    for field, expected in desired.items():
        if field == "models":
            if _models(channel.get(field), allow_empty=True) != _models(expected):
                return False
        elif str(channel.get(field) or "").strip() != expected:
            return False
    return True


def _item_by_id(values: Sequence[Mapping[str, Any]], item_id: int) -> dict[str, Any] | None:
    matches = [
        dict(value)
        for value in values
        if _optional_positive_integer(value.get("id")) == item_id
    ]
    if len(matches) > 1:
        raise DashboardManagementError("inventory_identity_ambiguous")
    return matches[0] if matches else None


def _read_private_json(path: Path, *, expected: type) -> Any:
    try:
        info = path.stat()
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DashboardManagementError("private_configuration_unreadable") from exc
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or not isinstance(
        payload, expected
    ):
        raise DashboardManagementError("private_configuration_invalid")
    return payload


def _proxy_url(value: object) -> str:
    if not isinstance(value, str):
        raise DashboardManagementError("proxy_url_invalid")
    normalized = value.strip()
    try:
        parsed = urlsplit(normalized)
        port = parsed.port
    except ValueError as exc:
        raise DashboardManagementError("proxy_url_invalid") from exc
    if (
        parsed.scheme not in {"http", "https", "socks5", "socks5h"}
        or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
        or not port
    ):
        raise DashboardManagementError("proxy_url_invalid")
    return normalized


def _proxy_metadata(proxy: str) -> dict[str, str]:
    port = urlsplit(proxy).port
    if not port:
        raise DashboardManagementError("proxy_url_invalid")
    return {
        "lease_id": f"singbox:{port}",
        "proxy_fingerprint": hashlib.sha256(proxy.encode()).hexdigest(),
        "pool": "fixed-map",
        "provider": "singbox",
    }


def _required_text(value: object, *, maximum: int, error: str) -> str:
    if not isinstance(value, str):
        raise DashboardManagementError(error)
    normalized = value.strip()
    if not normalized or len(normalized) > maximum or "\x00" in normalized:
        raise DashboardManagementError(error)
    return normalized


def _optional_text(value: object, *, maximum: int) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip()[:maximum]


def _model(value: object) -> str:
    normalized = _optional_text(value, maximum=96)
    return normalized if _MODEL.fullmatch(normalized) else ""


def _positive_integer(value: object, error: str) -> int:
    result = _optional_positive_integer(value)
    if result is None:
        raise DashboardManagementError(error)
    return result


def _optional_positive_integer(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if result > 0 else None


def _nonnegative_integer(value: object, error: str) -> int:
    if isinstance(value, bool):
        raise DashboardManagementError(error)
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise DashboardManagementError(error) from exc
    if result < 0:
        raise DashboardManagementError(error)
    return result


def _row(cursor: Any) -> dict[str, Any] | None:
    row = cursor.fetchone()
    if row is None:
        return None
    if isinstance(row, Mapping):
        return dict(row)
    columns = [item.name for item in cursor.description]
    return dict(zip(columns, row, strict=True))


def _timestamp(value: object) -> str | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _close(connection: Any) -> None:
    close = getattr(connection, "close", None)
    if callable(close):
        close()
