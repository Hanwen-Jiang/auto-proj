"""Provision and route duty-scoped A6 liquidity tokens."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from .storage import ensure_private_directory, private_file_lock
from .token_names import generate_anonymous_token_name, is_anonymous_token_name


class LiquidityTokenError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class LiquidityTokenContext:
    merchant_id: int
    a6_user_id: int
    session_ref: str
    model: str
    token_ref_id: int | None = None
    external_token_id: int | None = None
    token_name: str | None = None
    key_secret_ref: str | None = None

    def __post_init__(self) -> None:
        if (
            self.merchant_id < 1
            or self.a6_user_id < 1
            or not self.session_ref.strip()
            or not self.model.strip()
        ):
            raise LiquidityTokenError("liquidity_token_context_invalid")
        existing = (
            self.token_ref_id,
            self.external_token_id,
            self.token_name,
            self.key_secret_ref,
        )
        if any(value is not None for value in existing) and not all(
            value is not None for value in existing
        ):
            raise LiquidityTokenError("liquidity_token_existing_identity_incomplete")


@dataclass(frozen=True)
class LiquidityTokenIdentity:
    token_ref_id: int
    merchant_id: int
    a6_user_id: int
    external_token_id: int
    token_name: str
    key_fingerprint: str
    key_secret_ref: str
    model: str

    def __post_init__(self) -> None:
        if (
            min(
                self.token_ref_id,
                self.merchant_id,
                self.a6_user_id,
                self.external_token_id,
            )
            < 1
            or not is_anonymous_token_name(self.token_name)
            or len(self.key_fingerprint) != 64
            or any(value not in "0123456789abcdef" for value in self.key_fingerprint)
            or not self.key_secret_ref
            or not self.model
        ):
            raise LiquidityTokenError("liquidity_token_identity_invalid")


@dataclass(frozen=True)
class OwnedLiquidityReceiver:
    merchant_id: int
    supplier_id: int
    listing_id: int
    channel_id: int
    model: str
    last_success_at: datetime

    def __post_init__(self) -> None:
        if (
            min(self.merchant_id, self.supplier_id, self.listing_id, self.channel_id) < 1
            or not self.model.strip()
            or self.last_success_at.tzinfo is None
        ):
            raise LiquidityTokenError("liquidity_receiver_invalid")


class LiquidityTokenRemote(Protocol):
    def list_tokens(self, user_id: int) -> list[dict[str, Any]]: ...

    def ensure_one_key(self, user_id: int, token_name: str) -> str: ...

    def rename_token(
        self, user_id: int, token_id: int, new_name: str
    ) -> Mapping[str, Any]: ...

    def marketplace_channels(self, user_id: int, **kwargs: Any) -> Mapping[str, Any]: ...

    def marketplace_pin(self, user_id: int, **kwargs: Any) -> Mapping[str, Any]: ...

    def marketplace_pins(self, user_id: int) -> list[dict[str, Any]]: ...


class LiquidityTokenStore(Protocol):
    def load_context(
        self,
        *,
        a6_user_id: int,
        session_ref: str,
        model: str,
    ) -> LiquidityTokenContext: ...

    def register(
        self,
        context: LiquidityTokenContext,
        *,
        external_token_id: int,
        token_name: str,
        key_fingerprint: str,
        key_secret_ref: str,
    ) -> LiquidityTokenIdentity: ...

    def select_receiver(
        self,
        identity: LiquidityTokenIdentity,
        *,
        receiver_label: str,
        receiver_merchant_id: int | None,
        freshness_seconds: int,
    ) -> OwnedLiquidityReceiver | None: ...

    def record_route(
        self,
        identity: LiquidityTokenIdentity,
        receiver: OwnedLiquidityReceiver | None,
        *,
        route_state: str,
        pin_confirmed: bool,
        readback: Mapping[str, Any],
        error_class: str | None = None,
    ) -> None: ...


class PostgresLiquidityTokenStore:
    def __init__(self, connection_factory: Callable[[], Any]) -> None:
        if not callable(connection_factory):
            raise ValueError("liquidity_token_connection_factory_invalid")
        self.connection_factory = connection_factory

    def load_context(
        self,
        *,
        a6_user_id: int,
        session_ref: str,
        model: str,
    ) -> LiquidityTokenContext:
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT merchant.id AS merchant_id, merchant.a6_user_id,
                           merchant.session_ref, token.id AS token_ref_id,
                           token.a6_token_id AS external_token_id,
                           token.token_name, token.key_secret_ref
                    FROM a6_trade.a6_merchants merchant
                    LEFT JOIN a6_trade.a6_token_duties duty
                      ON duty.merchant_id = merchant.id
                     AND duty.duty = 'liquidity'
                     AND duty.model = %s
                     AND duty.duty_state IN ('provisioned', 'ready', 'blocked')
                    LEFT JOIN a6_trade.a6_tokens token ON token.id = duty.token_ref_id
                    WHERE merchant.a6_user_id = %s
                      AND merchant.session_ref = %s
                      AND merchant.status NOT IN ('restricted', 'abandoned')
                    """,
                    (model, a6_user_id, session_ref),
                )
                row = _row(cursor)
        finally:
            _close(connection)
        if row is None:
            raise LiquidityTokenError("liquidity_token_merchant_context_missing")
        return LiquidityTokenContext(
            merchant_id=int(row["merchant_id"]),
            a6_user_id=int(row["a6_user_id"]),
            session_ref=str(row["session_ref"]),
            model=str(model),
            token_ref_id=_optional_int(row.get("token_ref_id")),
            external_token_id=_optional_int(row.get("external_token_id")),
            token_name=_optional_text(row.get("token_name")),
            key_secret_ref=_optional_text(row.get("key_secret_ref")),
        )

    def register(
        self,
        context: LiquidityTokenContext,
        *,
        external_token_id: int,
        token_name: str,
        key_fingerprint: str,
        key_secret_ref: str,
    ) -> LiquidityTokenIdentity:
        if (
            external_token_id < 1
            or not is_anonymous_token_name(token_name)
            or len(key_fingerprint) != 64
            or not key_secret_ref
        ):
            raise LiquidityTokenError("liquidity_token_registration_invalid")
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT pg_advisory_xact_lock(%s, %s)",
                    (0x41364C54, context.merchant_id),
                )
                cursor.execute(
                    """
                    SELECT id
                    FROM a6_trade.a6_merchants
                    WHERE id = %s AND a6_user_id = %s AND session_ref = %s
                    FOR UPDATE
                    """,
                    (context.merchant_id, context.a6_user_id, context.session_ref),
                )
                if _row(cursor) is None:
                    raise LiquidityTokenError("liquidity_token_merchant_context_changed")
                cursor.execute(
                    """
                    INSERT INTO a6_trade.a6_tokens AS token (
                        merchant_id, a6_token_id, token_name,
                        key_fingerprint, key_secret_ref, status,
                        created_by_controller, last_reconciled_at
                    ) VALUES (%s, %s, %s, %s, %s, 'active', true, now())
                    ON CONFLICT (merchant_id, a6_token_id) DO UPDATE SET
                        token_name = EXCLUDED.token_name,
                        key_fingerprint = EXCLUDED.key_fingerprint,
                        key_secret_ref = EXCLUDED.key_secret_ref,
                        status = 'active', created_by_controller = true,
                        last_reconciled_at = now(), updated_at = now()
                    WHERE token.merchant_id = EXCLUDED.merchant_id
                    RETURNING token.id
                    """,
                    (
                        context.merchant_id,
                        external_token_id,
                        token_name,
                        key_fingerprint,
                        key_secret_ref,
                    ),
                )
                token = _row(cursor)
                if token is None:
                    raise LiquidityTokenError("liquidity_token_registration_conflict")
                token_ref_id = int(token["id"])
                cursor.execute(
                    """
                    INSERT INTO a6_trade.a6_token_duties AS duty (
                        token_ref_id, merchant_id, duty, model, duty_state,
                        created_by_controller, last_reconciled_at
                    ) VALUES (%s, %s, 'liquidity', %s, 'provisioned', true, now())
                    ON CONFLICT (token_ref_id) DO UPDATE SET
                        duty_state = CASE
                            WHEN duty.duty_state = 'retired' THEN 'provisioned'
                            ELSE duty.duty_state
                        END,
                        last_reconciled_at = now(), updated_at = now()
                    WHERE duty.merchant_id = EXCLUDED.merchant_id
                      AND duty.duty = EXCLUDED.duty
                      AND duty.model = EXCLUDED.model
                    RETURNING token_ref_id
                    """,
                    (token_ref_id, context.merchant_id, context.model),
                )
                if _row(cursor) is None:
                    raise LiquidityTokenError("liquidity_token_duty_conflict")
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            _close(connection)
        return LiquidityTokenIdentity(
            token_ref_id=token_ref_id,
            merchant_id=context.merchant_id,
            a6_user_id=context.a6_user_id,
            external_token_id=external_token_id,
            token_name=token_name,
            key_fingerprint=key_fingerprint,
            key_secret_ref=key_secret_ref,
            model=context.model,
        )

    def select_receiver(
        self,
        identity: LiquidityTokenIdentity,
        *,
        receiver_label: str,
        receiver_merchant_id: int | None,
        freshness_seconds: int,
    ) -> OwnedLiquidityReceiver | None:
        label = str(receiver_label or "").strip()
        if (
            not label
            or isinstance(freshness_seconds, bool)
            or not 5 <= freshness_seconds <= 3600
            or (receiver_merchant_id is not None and receiver_merchant_id < 1)
        ):
            raise LiquidityTokenError("liquidity_receiver_selector_invalid")
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT listing.merchant_id, merchant.supplier_id,
                           listing.a6_listing_id AS listing_id,
                           listing.a6_channel_id AS channel_id,
                           listing.platform_model AS model,
                           signal.last_success_at
                    FROM a6_trade.a6_supplier_listings_current listing
                    JOIN a6_trade.a6_merchants merchant
                      ON merchant.id = listing.merchant_id
                    JOIN a6_trade.a6_supplier_channels_current channel
                      ON channel.merchant_id = listing.merchant_id
                     AND channel.a6_channel_id = listing.a6_channel_id
                    JOIN a6_trade.a6_managed_listing_bindings managed
                      ON managed.merchant_id = listing.merchant_id
                     AND managed.a6_listing_id = listing.a6_listing_id
                     AND managed.a6_channel_id = listing.a6_channel_id
                     AND managed.control_state = 'verified'
                    JOIN a6_trade.a6_market_channel_signals_current signal
                      ON signal.model = listing.platform_model
                     AND signal.channel_id = listing.a6_channel_id
                     AND signal.listing_id = listing.a6_listing_id
                    LEFT JOIN a6_trade.merchant_cashout_positions position
                      ON position.merchant_id = listing.merchant_id
                    WHERE listing.platform_model = %s
                      AND listing.merchant_id <> %s
                      AND (%s IS NULL OR listing.merchant_id = %s)
                      AND merchant.supplier_id IS NOT NULL
                      AND listing.status = 2
                      AND listing.availability = 1
                      AND listing.visibility = 1
                      AND channel.status = 2
                      AND channel.availability = 1
                      AND signal.is_owned
                      AND signal.available
                      AND signal.health_state = 'healthy'
                      AND signal.last_success_at >= now() - (%s * interval '1 second')
                      AND (
                          channel.name = %s OR channel.nickname = %s
                          OR listing.display_name = %s
                          OR listing.description ILIKE ('%%' || %s || '%%')
                      )
                      AND NOT EXISTS (
                          SELECT 1
                          FROM a6_trade.a6_callback_sweep_intents callback
                          WHERE callback.receiver_merchant_id = listing.merchant_id
                            AND callback.accounting_state IN (
                                'planned', 'submitted', 'partial', 'unknown'
                            )
                      )
                    ORDER BY
                        coalesce(
                            position.rolling_7d_callback_receiver_credit_micros, 0
                        ),
                        position.last_confirmed_callback_at NULLS FIRST,
                        listing.merchant_id,
                        listing.a6_listing_id
                    LIMIT 1
                    """,
                    (
                        identity.model,
                        identity.merchant_id,
                        receiver_merchant_id,
                        receiver_merchant_id,
                        freshness_seconds,
                        label,
                        label,
                        label,
                        label,
                    ),
                )
                row = _row(cursor)
        finally:
            _close(connection)
        if row is None:
            return None
        return OwnedLiquidityReceiver(
            merchant_id=int(row["merchant_id"]),
            supplier_id=int(row["supplier_id"]),
            listing_id=int(row["listing_id"]),
            channel_id=int(row["channel_id"]),
            model=str(row["model"]),
            last_success_at=row["last_success_at"],
        )

    def record_route(
        self,
        identity: LiquidityTokenIdentity,
        receiver: OwnedLiquidityReceiver | None,
        *,
        route_state: str,
        pin_confirmed: bool,
        readback: Mapping[str, Any],
        error_class: str | None = None,
    ) -> None:
        if route_state not in {"unassigned", "ready", "blocked"}:
            raise LiquidityTokenError("liquidity_route_state_invalid")
        if route_state == "unassigned" and receiver is not None:
            raise LiquidityTokenError("liquidity_route_target_invalid")
        if route_state != "unassigned" and receiver is None:
            raise LiquidityTokenError("liquidity_route_target_invalid")
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO a6_trade.a6_liquidity_token_routes AS route (
                        token_ref_id, model, receiver_merchant_id,
                        receiver_listing_id, receiver_channel_id,
                        route_state, pin_confirmed, remote_readback,
                        last_error_class, observed_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, now())
                    ON CONFLICT (token_ref_id, model) DO UPDATE SET
                        receiver_merchant_id = EXCLUDED.receiver_merchant_id,
                        receiver_listing_id = EXCLUDED.receiver_listing_id,
                        receiver_channel_id = EXCLUDED.receiver_channel_id,
                        route_state = EXCLUDED.route_state,
                        pin_confirmed = EXCLUDED.pin_confirmed,
                        remote_readback = EXCLUDED.remote_readback,
                        last_error_class = EXCLUDED.last_error_class,
                        observed_at = now(), updated_at = now()
                    """,
                    (
                        identity.token_ref_id,
                        identity.model,
                        None if receiver is None else receiver.merchant_id,
                        None if receiver is None else receiver.listing_id,
                        None if receiver is None else receiver.channel_id,
                        route_state,
                        pin_confirmed,
                        _json(readback),
                        _optional_text(error_class),
                    ),
                )
                cursor.execute(
                    """
                    UPDATE a6_trade.a6_token_duties
                    SET duty_state = %s, last_reconciled_at = now(), updated_at = now()
                    WHERE token_ref_id = %s AND duty = 'liquidity' AND model = %s
                    """,
                    (
                        "ready" if route_state == "ready" else "blocked",
                        identity.token_ref_id,
                        identity.model,
                    ),
                )
                if cursor.rowcount != 1:
                    raise LiquidityTokenError("liquidity_token_duty_update_conflict")
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            _close(connection)


class LiquidityTokenReconciler:
    def __init__(
        self,
        store: LiquidityTokenStore,
        remote: LiquidityTokenRemote,
        *,
        secret_dir: Path,
        receiver_label: str = "小微商户",
        receiver_merchant_id: int | None = None,
        receiver_freshness_seconds: int = 300,
        marketplace_page_size: int = 100,
        name_generator: Callable[[], str] = generate_anonymous_token_name,
    ) -> None:
        self.store = store
        self.remote = remote
        self.secret_dir = secret_dir.expanduser().resolve()
        self.receiver_label = str(receiver_label or "").strip()
        self.receiver_merchant_id = receiver_merchant_id
        self.receiver_freshness_seconds = receiver_freshness_seconds
        self.marketplace_page_size = marketplace_page_size
        self.name_generator = name_generator
        if (
            not self.receiver_label
            or not 5 <= receiver_freshness_seconds <= 3600
            or not 1 <= marketplace_page_size <= 1000
            or not callable(name_generator)
        ):
            raise LiquidityTokenError("liquidity_token_reconciler_invalid")

    def reconcile(
        self,
        *,
        a6_user_id: int,
        session_ref: str,
        model: str,
    ) -> dict[str, Any]:
        context = self.store.load_context(
            a6_user_id=a6_user_id,
            session_ref=session_ref,
            model=model,
        )
        tokens = self.remote.list_tokens(context.a6_user_id)
        token_name, external_token_id, created_key = self._token_identity(context, tokens)
        key = created_key or self.remote.ensure_one_key(context.a6_user_id, token_name)
        if not key.startswith("sk-"):
            raise LiquidityTokenError("liquidity_token_key_invalid")
        secret_path = self.secret_dir / str(context.merchant_id) / f"{external_token_id}.secret"
        _write_private_secret(secret_path, key)
        fingerprint = hashlib.sha256(key.encode()).hexdigest()
        identity = self.store.register(
            context,
            external_token_id=external_token_id,
            token_name=token_name,
            key_fingerprint=fingerprint,
            key_secret_ref=str(secret_path),
        )
        receiver = self.store.select_receiver(
            identity,
            receiver_label=self.receiver_label,
            receiver_merchant_id=self.receiver_merchant_id,
            freshness_seconds=self.receiver_freshness_seconds,
        )
        if receiver is None:
            self.store.record_route(
                identity,
                None,
                route_state="unassigned",
                pin_confirmed=False,
                readback={"reason": "skipped_no_owned_receiver_listing"},
            )
            return _public(identity, status="skipped_no_owned_receiver_listing")
        remote_channel = self._remote_receiver(identity, receiver)
        if remote_channel is None:
            self.store.record_route(
                identity,
                receiver,
                route_state="blocked",
                pin_confirmed=False,
                readback={"reason": "receiver_not_callable"},
                error_class="liquidity_receiver_not_callable",
            )
            return _public(
                identity,
                status="skipped_no_owned_receiver_listing",
                receiver=receiver,
            )
        self.remote.marketplace_pin(
            identity.a6_user_id,
            token_id=identity.external_token_id,
            channel_id=receiver.channel_id,
            model=identity.model,
            fallback_to_smart_routing=False,
        )
        pin = _matching_pin(
            self.remote.marketplace_pins(identity.a6_user_id),
            token_id=identity.external_token_id,
            model=identity.model,
        )
        if pin is None or _channel_id(pin) != receiver.channel_id:
            self.store.record_route(
                identity,
                receiver,
                route_state="blocked",
                pin_confirmed=False,
                readback={"pin": pin or {}},
                error_class="liquidity_pin_readback_mismatch",
            )
            raise LiquidityTokenError("liquidity_pin_readback_mismatch")
        self.store.record_route(
            identity,
            receiver,
            route_state="ready",
            pin_confirmed=True,
            readback={"channel": remote_channel, "pin": pin},
        )
        return _public(identity, status="ready", receiver=receiver)

    def _token_identity(
        self,
        context: LiquidityTokenContext,
        tokens: Sequence[Mapping[str, Any]],
    ) -> tuple[str, int, str | None]:
        if context.external_token_id is not None:
            matches = [
                item
                for item in tokens
                if _positive_int(item.get("id")) == context.external_token_id
            ]
            if len(matches) != 1:
                raise LiquidityTokenError("liquidity_token_remote_identity_missing")
            current_name = str(matches[0].get("name") or "").strip()
            if is_anonymous_token_name(current_name):
                return current_name, context.external_token_id, None
            new_name = self._unique_name(tokens)
            observed = self.remote.rename_token(
                context.a6_user_id,
                context.external_token_id,
                new_name,
            )
            if str(observed.get("name") or "") != new_name:
                raise LiquidityTokenError("liquidity_token_rename_readback_mismatch")
            return new_name, context.external_token_id, None
        name = self._unique_name(tokens)
        key = self.remote.ensure_one_key(context.a6_user_id, name)
        refreshed = self.remote.list_tokens(context.a6_user_id)
        matches = [item for item in refreshed if str(item.get("name") or "") == name]
        if len(matches) != 1 or _positive_int(matches[0].get("id")) is None:
            raise LiquidityTokenError("liquidity_token_creation_readback_mismatch")
        return name, int(matches[0]["id"]), key

    def _unique_name(self, tokens: Sequence[Mapping[str, Any]]) -> str:
        existing = {str(item.get("name") or "") for item in tokens}
        for _ in range(64):
            value = self.name_generator()
            if is_anonymous_token_name(value) and value not in existing:
                return value
        raise LiquidityTokenError("liquidity_token_name_exhausted")

    def _remote_receiver(
        self,
        identity: LiquidityTokenIdentity,
        receiver: OwnedLiquidityReceiver,
    ) -> dict[str, Any] | None:
        page = 1
        matches: list[dict[str, Any]] = []
        while page <= 10_000:
            document = self.remote.marketplace_channels(
                identity.a6_user_id,
                token_id=identity.external_token_id,
                model=identity.model,
                page=page,
                page_size=self.marketplace_page_size,
                sort="input_asc",
                exclude_unavailable=False,
                include_disabled=True,
                include_user_disabled=True,
            )
            items = document.get("items")
            if not isinstance(items, list):
                raise LiquidityTokenError("liquidity_marketplace_page_invalid")
            for raw in items:
                if not isinstance(raw, Mapping):
                    continue
                item = dict(raw)
                if (
                    _channel_id(item) == receiver.channel_id
                    and _positive_int(item.get("listing_id")) == receiver.listing_id
                ):
                    matches.append(item)
            total = _positive_int(document.get("total"))
            if not items or (total is not None and page * self.marketplace_page_size >= total):
                break
            page += 1
        if len(matches) != 1:
            return None
        target = matches[0]
        if (
            target.get("supplier_channel_disabled") is True
            or target.get("user_channel_disabled") is True
            or str(target.get("route_status") or "").casefold()
            in {"user_disabled", "supplier_disabled", "unavailable"}
            or int(target.get("listing_availability") or 0) != 1
        ):
            return None
        return target


def _write_private_secret(path: Path, value: str) -> None:
    if not value:
        raise LiquidityTokenError("liquidity_token_key_invalid")
    resolved = path.expanduser().resolve()
    ensure_private_directory(resolved.parent)
    with private_file_lock(resolved.with_suffix(resolved.suffix + ".lock")):
        temporary = resolved.with_name(f".{resolved.name}.{os.getpid()}.{secrets.token_hex(4)}")
        descriptor = -1
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.write(descriptor, (value + "\n").encode())
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = -1
            os.replace(temporary, resolved)
            os.chmod(resolved, 0o600)
        except Exception:
            if descriptor >= 0:
                os.close(descriptor)
            with suppress(FileNotFoundError):
                temporary.unlink()
            raise


def _matching_pin(
    pins: Sequence[Mapping[str, Any]],
    *,
    token_id: int,
    model: str,
) -> dict[str, Any] | None:
    matches = [
        dict(item)
        for item in pins
        if _positive_int(item.get("token_id")) == token_id
        and str(item.get("model_name") or item.get("model") or "").strip() == model
    ]
    if len(matches) > 1:
        raise LiquidityTokenError("liquidity_pin_readback_ambiguous")
    return None if not matches else matches[0]


def _public(
    identity: LiquidityTokenIdentity,
    *,
    status: str,
    receiver: OwnedLiquidityReceiver | None = None,
) -> dict[str, Any]:
    return {
        "success": True,
        "status": status,
        "merchant_id": identity.merchant_id,
        "a6_user_id": identity.a6_user_id,
        "token_ref_id": identity.token_ref_id,
        "external_token_id": identity.external_token_id,
        "token_name": identity.token_name,
        "token_duty": "liquidity",
        "key_fingerprint": identity.key_fingerprint,
        "key_secret_ref": identity.key_secret_ref,
        "model": identity.model,
        "receiver_merchant_id": None if receiver is None else receiver.merchant_id,
        "receiver_listing_id": None if receiver is None else receiver.listing_id,
        "receiver_channel_id": None if receiver is None else receiver.channel_id,
    }


def _channel_id(value: Mapping[str, Any]) -> int | None:
    return _positive_int(value.get("channel_id") or value.get("id"))


def _positive_int(value: object) -> int | None:
    try:
        parsed = int(value) if value is not None else 0
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _optional_int(value: object) -> int | None:
    return None if value is None else int(value)


def _optional_text(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


def _json(value: Mapping[str, Any]) -> str:
    return json.dumps(dict(value), ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _row(cursor: Any) -> dict[str, Any] | None:
    value = cursor.fetchone()
    if value is None:
        return None
    if isinstance(value, Mapping):
        return dict(value)
    names = [column.name for column in cursor.description or ()]
    return dict(zip(names, value, strict=False))


def _close(connection: Any) -> None:
    close = getattr(connection, "close", None)
    if callable(close):
        close()
