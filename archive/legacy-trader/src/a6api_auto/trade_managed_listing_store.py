from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping
from typing import Any

from .trade_managed_listings import (
    ChannelAdoptionIntent,
    ChannelCreateIntent,
    ListingAdoptionIntent,
    ListingCreateIntent,
    ManagedAction,
    ManagedChannelBinding,
    ManagedListingBinding,
    ManagedListingError,
)

_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_ERROR = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}$")
_FORBIDDEN_KEYS = frozenset(
    {
        "access_token",
        "admin_key",
        "api_key",
        "authorization",
        "cookie",
        "key",
        "password",
        "refresh_token",
        "secret",
        "session",
        "token",
    }
)
_CREATE_STATES = frozenset({"planned", "error"})
_ACTION_STATES = {
    ("channel", "update"): "updating",
    ("listing", "update"): "updating",
    ("listing", "takedown"): "taking_down",
    ("listing", "resubmit"): "resubmitting",
}
_UNKNOWN_STATES = {
    "update": "update_unknown",
    "takedown": "takedown_unknown",
    "resubmit": "resubmit_unknown",
}
_RECOVERABLE_CHANNEL_CREATE_UNKNOWN_ERRORS = frozenset({"supplier_apply_failed"})
_CONFIRMED_ABSENT_CREATE_ERROR = "managed_channel_create_confirmed_absent"


class PostgresManagedListingStore:
    """Transactional persistence for controller-owned A6 channels and listings."""

    def __init__(
        self,
        connection_factory: Callable[[], Any],
        *,
        config_version: str = "phase1-managed-listings-v1",
        requested_by: str = "a6-trader",
    ) -> None:
        if not callable(connection_factory):
            raise ManagedListingError("managed_store_connection_factory_invalid")
        if not _LABEL.fullmatch(config_version):
            raise ManagedListingError("managed_store_config_version_invalid")
        if not _LABEL.fullmatch(requested_by):
            raise ManagedListingError("managed_store_requested_by_invalid")
        self.connection_factory = connection_factory
        self.config_version = config_version
        self.requested_by = requested_by

    def get_channel(self, managed_uid: str) -> ManagedChannelBinding | None:
        return self._get_binding("channel", managed_uid)

    def get_listing(self, managed_uid: str) -> ManagedListingBinding | None:
        return self._get_binding("listing", managed_uid)

    def list_controlled_listings(
        self,
        merchant_id: int,
        model: str,
    ) -> tuple[ManagedListingBinding, ...]:
        merchant_id = _positive_int(merchant_id, "managed_merchant_id_invalid")
        model = str(model).strip()
        if not model:
            raise ManagedListingError("managed_listing_model_invalid")
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT *
                    FROM a6_trade.a6_managed_listing_bindings
                    WHERE merchant_id = %s
                      AND control_state <> 'retired'
                      AND a6_listing_id IS NOT NULL
                      AND a6_channel_id IS NOT NULL
                      AND coalesce(
                          observed_state->>'platform_model',
                          observed_state->>'own_model',
                          desired_state #>> '{remote_listing,platform_model}',
                          desired_state #>> '{remote_listing,own_model}'
                      ) = %s
                    ORDER BY id
                    """,
                    (merchant_id, model),
                )
                rows = _rows(cursor)
            return tuple(_listing_binding(row) for row in rows)
        finally:
            _close(connection)

    def _get_binding(
        self, resource_kind: str, managed_uid: str
    ) -> ManagedChannelBinding | ManagedListingBinding | None:
        _validate_uid(managed_uid)
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(_binding_select(resource_kind), (managed_uid,))
                row = _row(cursor)
            return _binding(resource_kind, row) if row is not None else None
        finally:
            _close(connection)

    def reserve_channel(self, intent: ChannelCreateIntent) -> ManagedChannelBinding:
        _validate_intent(intent.managed_uid, intent.merchant_id, intent.desired_state)
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO a6_trade.a6_managed_channel_bindings (
                        managed_uid, merchant_id, create_idempotency_key,
                        control_state, desired_state
                    ) VALUES (%s, %s, %s, 'planned', %s::jsonb)
                    ON CONFLICT (managed_uid) DO NOTHING
                    RETURNING *
                    """,
                    (
                        intent.managed_uid,
                        intent.merchant_id,
                        intent.create_idempotency_key,
                        _json(intent.desired_state),
                    ),
                )
                row = _row(cursor)
                if row is None:
                    cursor.execute(_CHANNEL_SELECT + " FOR UPDATE", (intent.managed_uid,))
                    row = _required_row(cursor, "managed_channel_reserve_failed")
                binding = _channel_binding(row)
                _verify_channel_intent(binding, intent)
            connection.commit()
            return binding
        except Exception:
            connection.rollback()
            raise
        finally:
            _close(connection)

    def reserve_listing(self, intent: ListingCreateIntent) -> ManagedListingBinding:
        _validate_intent(intent.managed_uid, intent.merchant_id, intent.desired_state)
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT * FROM a6_trade.a6_managed_channel_bindings
                    WHERE id = %s FOR UPDATE
                    """,
                    (intent.channel_binding_id,),
                )
                channel_row = _row(cursor)
                channel = _channel_binding(channel_row) if channel_row is not None else None
                if (
                    channel is None
                    or channel.binding_id != intent.channel_binding_id
                    or channel.merchant_id != intent.merchant_id
                    or channel.control_state != "verified"
                    or channel.a6_channel_id != intent.a6_channel_id
                ):
                    raise ManagedListingError("managed_listing_channel_not_verified")
                cursor.execute(
                    """
                    INSERT INTO a6_trade.a6_managed_listing_bindings (
                        managed_uid, merchant_id, channel_binding_id, a6_channel_id,
                        create_idempotency_key, control_state, desired_state
                    ) VALUES (%s, %s, %s, %s, %s, 'planned', %s::jsonb)
                    ON CONFLICT (managed_uid) DO NOTHING
                    RETURNING *
                    """,
                    (
                        intent.managed_uid,
                        intent.merchant_id,
                        intent.channel_binding_id,
                        intent.a6_channel_id,
                        intent.create_idempotency_key,
                        _json(intent.desired_state),
                    ),
                )
                row = _row(cursor)
                if row is None:
                    cursor.execute(_LISTING_SELECT + " FOR UPDATE", (intent.managed_uid,))
                    row = _required_row(cursor, "managed_listing_reserve_failed")
                binding = _listing_binding(row)
                _verify_listing_intent(binding, intent)
            connection.commit()
            return binding
        except Exception:
            connection.rollback()
            raise
        finally:
            _close(connection)

    def adopt_channel(self, intent: ChannelAdoptionIntent) -> ManagedChannelBinding:
        _validate_adoption_intent(
            intent.managed_uid,
            intent.merchant_id,
            intent.a6_channel_id,
            intent.idempotency_key,
            intent.desired_state,
            intent.observation,
        )
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(_CHANNEL_SELECT + " FOR UPDATE", (intent.managed_uid,))
                existing_row = _row(cursor)
                if existing_row is not None:
                    existing = _channel_binding(existing_row)
                    if (
                        existing.merchant_id == intent.merchant_id
                        and existing.a6_channel_id == intent.a6_channel_id
                        and existing.control_state == "verified"
                    ):
                        connection.commit()
                        return existing
                    if (
                        existing.merchant_id != intent.merchant_id
                        or existing.a6_channel_id not in {None, intent.a6_channel_id}
                        or existing.control_state
                        not in {
                            "planned",
                            "creating",
                            "create_unknown",
                            "verifying",
                            "updating",
                            "update_unknown",
                            "error",
                            "retired",
                        }
                    ):
                        raise ManagedListingError("managed_channel_adoption_binding_conflict")
                before = (
                    _binding_snapshot(_channel_binding(existing_row))
                    if existing_row is not None
                    else {}
                )
                action_id = self._record_completed_adoption(
                    cursor,
                    resource_kind="channel",
                    idempotency_key=intent.idempotency_key,
                    managed_uid=intent.managed_uid,
                    desired_state=intent.desired_state,
                    observed_before=before,
                    observed_after=intent.observation,
                )
                temporary = ManagedChannelBinding(
                    binding_id=(
                        _positive_int(existing_row.get("id"), "managed_binding_id_invalid")
                        if existing_row is not None
                        else 1
                    ),
                    managed_uid=intent.managed_uid,
                    merchant_id=intent.merchant_id,
                    create_idempotency_key=intent.idempotency_key,
                    control_state="verified",
                    desired_state=dict(intent.desired_state),
                    a6_channel_id=intent.a6_channel_id,
                    create_action_id=action_id,
                )
                _upsert_channel_inventory(
                    cursor,
                    temporary,
                    intent.a6_channel_id,
                    intent.observation,
                )
                if existing_row is None:
                    cursor.execute(
                        """
                        INSERT INTO a6_trade.a6_managed_channel_bindings (
                            managed_uid, merchant_id, a6_channel_id,
                            create_idempotency_key, control_state, desired_state,
                            observed_state, create_action_id, last_action_id,
                            first_attempted_at, last_attempted_at, last_observed_at,
                            verified_at
                        ) VALUES (
                            %s, %s, %s, %s, 'verified', %s::jsonb, %s::jsonb,
                            %s, %s, now(), now(), now(), now()
                        ) RETURNING *
                        """,
                        (
                            intent.managed_uid,
                            intent.merchant_id,
                            intent.a6_channel_id,
                            intent.idempotency_key,
                            _json(intent.desired_state),
                            _json(intent.observation),
                            action_id,
                            action_id,
                        ),
                    )
                else:
                    self._supersede_prior_create(cursor, existing_row, action_id, "channel")
                    cursor.execute(
                        """
                        UPDATE a6_trade.a6_managed_channel_bindings
                        SET a6_channel_id = %s, create_idempotency_key = %s,
                            control_state = 'verified', desired_state = %s::jsonb,
                            observed_state = %s::jsonb, create_action_id = %s,
                            last_action_id = %s, last_error_class = NULL,
                            last_attempted_at = now(), last_observed_at = now(),
                            verified_at = now(), updated_at = now()
                        WHERE managed_uid = %s AND merchant_id = %s
                          AND (a6_channel_id IS NULL OR a6_channel_id = %s)
                        RETURNING *
                        """,
                        (
                            intent.a6_channel_id,
                            intent.idempotency_key,
                            _json(intent.desired_state),
                            _json(intent.observation),
                            action_id,
                            action_id,
                            intent.managed_uid,
                            intent.merchant_id,
                            intent.a6_channel_id,
                        ),
                    )
                row = _required_row(cursor, "managed_channel_adoption_conflict")
            connection.commit()
            return _channel_binding(row)
        except Exception:
            connection.rollback()
            raise
        finally:
            _close(connection)

    def adopt_listing(self, intent: ListingAdoptionIntent) -> ManagedListingBinding:
        _validate_adoption_intent(
            intent.managed_uid,
            intent.merchant_id,
            intent.a6_listing_id,
            intent.idempotency_key,
            intent.desired_state,
            intent.observation,
        )
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT * FROM a6_trade.a6_managed_channel_bindings
                    WHERE id = %s FOR UPDATE
                    """,
                    (intent.channel_binding_id,),
                )
                channel = _channel_binding(
                    _required_row(cursor, "managed_listing_adoption_channel_missing")
                )
                if (
                    channel.merchant_id != intent.merchant_id
                    or channel.a6_channel_id != intent.a6_channel_id
                    or channel.control_state != "verified"
                ):
                    raise ManagedListingError("managed_listing_adoption_channel_conflict")
                cursor.execute(_LISTING_SELECT + " FOR UPDATE", (intent.managed_uid,))
                existing_row = _row(cursor)
                if existing_row is not None:
                    existing = _listing_binding(existing_row)
                    if (
                        existing.merchant_id == intent.merchant_id
                        and existing.channel_binding_id == intent.channel_binding_id
                        and existing.a6_listing_id == intent.a6_listing_id
                        and existing.control_state == "verified"
                    ):
                        connection.commit()
                        return existing
                    if (
                        existing.merchant_id != intent.merchant_id
                        or existing.channel_binding_id != intent.channel_binding_id
                        or existing.a6_listing_id not in {None, intent.a6_listing_id}
                        or existing.control_state
                        not in {
                            "planned",
                            "creating",
                            "create_unknown",
                            "verifying",
                            "updating",
                            "update_unknown",
                            "error",
                            "retired",
                        }
                    ):
                        raise ManagedListingError("managed_listing_adoption_binding_conflict")
                before = (
                    _binding_snapshot(_listing_binding(existing_row))
                    if existing_row is not None
                    else {}
                )
                action_id = self._record_completed_adoption(
                    cursor,
                    resource_kind="listing",
                    idempotency_key=intent.idempotency_key,
                    managed_uid=intent.managed_uid,
                    desired_state=intent.desired_state,
                    observed_before=before,
                    observed_after=intent.observation,
                )
                temporary = ManagedListingBinding(
                    binding_id=(
                        _positive_int(existing_row.get("id"), "managed_binding_id_invalid")
                        if existing_row is not None
                        else 1
                    ),
                    managed_uid=intent.managed_uid,
                    merchant_id=intent.merchant_id,
                    channel_binding_id=intent.channel_binding_id,
                    create_idempotency_key=intent.idempotency_key,
                    control_state="verified",
                    desired_state=dict(intent.desired_state),
                    a6_channel_id=intent.a6_channel_id,
                    a6_listing_id=intent.a6_listing_id,
                    create_action_id=action_id,
                )
                _upsert_listing_inventory(
                    cursor,
                    temporary,
                    intent.a6_listing_id,
                    intent.observation,
                )
                if existing_row is None:
                    cursor.execute(
                        """
                        INSERT INTO a6_trade.a6_managed_listing_bindings (
                            managed_uid, merchant_id, channel_binding_id,
                            a6_channel_id, a6_listing_id, create_idempotency_key,
                            control_state, desired_state, observed_state,
                            create_action_id, last_action_id, first_attempted_at,
                            last_attempted_at, last_observed_at, verified_at
                        ) VALUES (
                            %s, %s, %s, %s, %s, %s, 'verified',
                            %s::jsonb, %s::jsonb, %s, %s,
                            now(), now(), now(), now()
                        ) RETURNING *
                        """,
                        (
                            intent.managed_uid,
                            intent.merchant_id,
                            intent.channel_binding_id,
                            intent.a6_channel_id,
                            intent.a6_listing_id,
                            intent.idempotency_key,
                            _json(intent.desired_state),
                            _json(intent.observation),
                            action_id,
                            action_id,
                        ),
                    )
                else:
                    self._supersede_prior_create(cursor, existing_row, action_id, "listing")
                    cursor.execute(
                        """
                        UPDATE a6_trade.a6_managed_listing_bindings
                        SET a6_channel_id = %s, a6_listing_id = %s,
                            create_idempotency_key = %s, control_state = 'verified',
                            desired_state = %s::jsonb, observed_state = %s::jsonb,
                            create_action_id = %s, last_action_id = %s,
                            last_error_class = NULL, last_attempted_at = now(),
                            last_observed_at = now(), verified_at = now(), updated_at = now()
                        WHERE managed_uid = %s AND merchant_id = %s
                          AND channel_binding_id = %s
                          AND (a6_listing_id IS NULL OR a6_listing_id = %s)
                        RETURNING *
                        """,
                        (
                            intent.a6_channel_id,
                            intent.a6_listing_id,
                            intent.idempotency_key,
                            _json(intent.desired_state),
                            _json(intent.observation),
                            action_id,
                            action_id,
                            intent.managed_uid,
                            intent.merchant_id,
                            intent.channel_binding_id,
                            intent.a6_listing_id,
                        ),
                    )
                row = _required_row(cursor, "managed_listing_adoption_conflict")
            connection.commit()
            return _listing_binding(row)
        except Exception:
            connection.rollback()
            raise
        finally:
            _close(connection)

    def _record_completed_adoption(
        self,
        cursor: Any,
        *,
        resource_kind: str,
        idempotency_key: str,
        managed_uid: str,
        desired_state: Mapping[str, Any],
        observed_before: Mapping[str, Any],
        observed_after: Mapping[str, Any],
    ) -> int:
        cursor.execute(
            """
            INSERT INTO a6_trade.trade_control_actions (
                idempotency_key, action_type, entity_type, entity_id,
                desired_state, observed_before, observed_after, status,
                config_version, requested_by, started_at, completed_at
            ) VALUES (
                %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s::jsonb,
                'completed', %s, %s, now(), now()
            )
            ON CONFLICT (idempotency_key) DO NOTHING
            RETURNING id
            """,
            (
                idempotency_key,
                f"adopt_managed_{resource_kind}",
                f"a6_managed_{resource_kind}",
                managed_uid,
                _json(desired_state),
                _json(observed_before),
                _json(observed_after),
                self.config_version,
                self.requested_by,
            ),
        )
        row = _row(cursor)
        if row is None:
            cursor.execute(
                """
                SELECT id, action_type, entity_type, entity_id,
                       desired_state, observed_after, status
                FROM a6_trade.trade_control_actions
                WHERE idempotency_key = %s FOR UPDATE
                """,
                (idempotency_key,),
            )
            existing = _required_row(cursor, "managed_adoption_action_missing")
            if (
                existing.get("action_type") != f"adopt_managed_{resource_kind}"
                or existing.get("entity_type") != f"a6_managed_{resource_kind}"
                or existing.get("entity_id") != managed_uid
                or _mapping(existing.get("desired_state")) != dict(desired_state)
                or existing.get("status") != "completed"
            ):
                raise ManagedListingError("managed_adoption_action_conflict")
            return _positive_int(existing.get("id"), "managed_action_id_invalid")
        action_id = _positive_int(row.get("id"), "managed_action_id_invalid")
        cursor.execute(
            """
            INSERT INTO a6_trade.trade_control_action_attempts (
                action_id, attempt_no, status, observed_before,
                observed_after, started_at, completed_at
            ) VALUES (%s, 1, 'completed', %s::jsonb, %s::jsonb, now(), now())
            """,
            (action_id, _json(observed_before), _json(observed_after)),
        )
        return action_id

    @staticmethod
    def _supersede_prior_create(
        cursor: Any,
        existing_row: Mapping[str, Any],
        adoption_action_id: int,
        resource_kind: str,
    ) -> None:
        old_action_id = _optional_positive_int(existing_row.get("create_action_id"))
        if old_action_id is None or old_action_id == adoption_action_id:
            return
        error_class = f"managed_{resource_kind}_create_superseded_by_adoption"
        cursor.execute(
            """
            UPDATE a6_trade.trade_control_actions
            SET status = 'superseded', error_class = %s,
                completed_at = coalesce(completed_at, now()), updated_at = now()
            WHERE id = %s AND status <> 'completed'
            """,
            (error_class, old_action_id),
        )
        cursor.execute(
            """
            UPDATE a6_trade.trade_control_action_attempts
            SET status = 'superseded', error_class = %s,
                completed_at = coalesce(completed_at, now())
            WHERE action_id = %s AND status IN ('started', 'unknown')
            """,
            (error_class, old_action_id),
        )

    def claim_channel_create(self, binding: ManagedChannelBinding) -> ManagedChannelBinding | None:
        result = self._claim_create("channel", binding)
        return result if isinstance(result, ManagedChannelBinding) else None

    def claim_listing_create(self, binding: ManagedListingBinding) -> ManagedListingBinding | None:
        result = self._claim_create("listing", binding)
        return result if isinstance(result, ManagedListingBinding) else None

    def _claim_create(
        self,
        resource_kind: str,
        binding: ManagedChannelBinding | ManagedListingBinding,
    ) -> ManagedChannelBinding | ManagedListingBinding | None:
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    _binding_select(resource_kind) + " FOR UPDATE",
                    (binding.managed_uid,),
                )
                row = _required_row(cursor, "managed_create_binding_missing")
                locked = _binding(resource_kind, row)
                _same_binding(locked, binding)
                if locked.control_state not in _CREATE_STATES:
                    connection.rollback()
                    return None
                if (
                    resource_kind == "channel"
                    and locked.control_state == "error"
                    and locked.last_error_class != _CONFIRMED_ABSENT_CREATE_ERROR
                ):
                    connection.rollback()
                    return None
                before = _binding_snapshot(locked)
                cursor.execute(
                    """
                    INSERT INTO a6_trade.trade_control_actions (
                        idempotency_key, action_type, entity_type, entity_id,
                        desired_state, observed_before, status,
                        config_version, requested_by, started_at
                    ) VALUES (%s, %s, %s, %s, %s::jsonb, %s::jsonb,
                              'started', %s, %s, now())
                    ON CONFLICT (idempotency_key) DO NOTHING
                    RETURNING id
                    """,
                    (
                        locked.create_idempotency_key,
                        f"create_managed_{resource_kind}",
                        f"a6_managed_{resource_kind}",
                        locked.managed_uid,
                        _json(locked.desired_state),
                        _json(before),
                        self.config_version,
                        self.requested_by,
                    ),
                )
                action = _row(cursor)
                if action is None:
                    connection.rollback()
                    return None
                action_id = _positive_int(
                    action.get("id", action.get("action_id")),
                    "managed_create_action_invalid",
                )
                cursor.execute(
                    _claim_binding_sql(resource_kind),
                    (action_id, action_id, locked.binding_id, locked.managed_uid),
                )
                claimed_row = _required_row(cursor, "managed_create_claim_conflict")
                _start_attempt(cursor, action_id, before)
            connection.commit()
            return _binding(resource_kind, claimed_row)
        except Exception:
            connection.rollback()
            raise
        finally:
            _close(connection)

    def record_channel_post(
        self,
        binding: ManagedChannelBinding,
        observation: Mapping[str, Any],
    ) -> ManagedChannelBinding:
        result = self._record_create_post("channel", binding, observation)
        assert isinstance(result, ManagedChannelBinding)
        return result

    def record_listing_post(
        self,
        binding: ManagedListingBinding,
        observation: Mapping[str, Any],
    ) -> ManagedListingBinding:
        result = self._record_create_post("listing", binding, observation)
        assert isinstance(result, ManagedListingBinding)
        return result

    def _record_create_post(
        self,
        resource_kind: str,
        binding: ManagedChannelBinding | ManagedListingBinding,
        observation: Mapping[str, Any],
    ) -> ManagedChannelBinding | ManagedListingBinding:
        _validate_document(observation)
        action_id = _positive_int(binding.create_action_id, "managed_create_action_missing")
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    _binding_select(resource_kind) + " FOR UPDATE",
                    (binding.managed_uid,),
                )
                locked = _binding(
                    resource_kind,
                    _required_row(cursor, "managed_create_binding_missing"),
                )
                _same_binding(locked, binding)
                cursor.execute(
                    """
                    SELECT * FROM a6_trade.trade_control_actions
                    WHERE id = %s FOR UPDATE
                    """,
                    (action_id,),
                )
                action = _required_row(cursor, "managed_create_action_missing")
                _validate_create_action(resource_kind, locked, action, expected_status="started")
                if (
                    locked.control_state != "creating"
                    or _create_external_id(locked, resource_kind) is not None
                ):
                    raise ManagedListingError("managed_create_post_conflict")
                cursor.execute(
                    _update_binding_observation_sql(resource_kind, "verifying"),
                    (
                        _json(observation),
                        locked.binding_id,
                        locked.managed_uid,
                        action_id,
                    ),
                )
                row = _required_row(cursor, "managed_create_post_conflict")
            connection.commit()
            return _binding(resource_kind, row)
        except Exception:
            connection.rollback()
            raise
        finally:
            _close(connection)

    def mark_channel_create_unknown(
        self, binding: ManagedChannelBinding, error_class: str
    ) -> ManagedChannelBinding:
        result = self._mark_create_unknown("channel", binding, error_class)
        assert isinstance(result, ManagedChannelBinding)
        return result

    def mark_listing_create_unknown(
        self, binding: ManagedListingBinding, error_class: str
    ) -> ManagedListingBinding:
        result = self._mark_create_unknown("listing", binding, error_class)
        assert isinstance(result, ManagedListingBinding)
        return result

    def mark_channel_create_failed(
        self,
        binding: ManagedChannelBinding,
        error_class: str,
        observation: Mapping[str, Any],
    ) -> ManagedChannelBinding:
        result = self._mark_create_failed("channel", binding, error_class, observation)
        assert isinstance(result, ManagedChannelBinding)
        return result

    def mark_listing_create_failed(
        self,
        binding: ManagedListingBinding,
        error_class: str,
        observation: Mapping[str, Any],
    ) -> ManagedListingBinding:
        result = self._mark_create_failed("listing", binding, error_class, observation)
        assert isinstance(result, ManagedListingBinding)
        return result

    def resolve_channel_create_unknown(
        self,
        binding: ManagedChannelBinding,
        desired_state: Mapping[str, Any],
        observation: Mapping[str, Any],
        error_class: str,
    ) -> ManagedChannelBinding:
        result = self._resolve_create_unknown(
            "channel", binding, desired_state, observation, error_class
        )
        assert isinstance(result, ManagedChannelBinding)
        return result

    def resolve_listing_create_unknown(
        self,
        binding: ManagedListingBinding,
        desired_state: Mapping[str, Any],
        observation: Mapping[str, Any],
        error_class: str,
    ) -> ManagedListingBinding:
        result = self._resolve_create_unknown(
            "listing", binding, desired_state, observation, error_class
        )
        assert isinstance(result, ManagedListingBinding)
        return result

    def _mark_create_unknown(
        self,
        resource_kind: str,
        binding: ManagedChannelBinding | ManagedListingBinding,
        error_class: str,
    ) -> ManagedChannelBinding | ManagedListingBinding:
        error_class = _validate_error(error_class)
        action_id = _positive_int(binding.create_action_id, "managed_create_action_missing")
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    _binding_select(resource_kind) + " FOR UPDATE",
                    (binding.managed_uid,),
                )
                locked = _binding(
                    resource_kind,
                    _required_row(cursor, "managed_create_binding_missing"),
                )
                _same_binding(locked, binding)
                cursor.execute(
                    """
                    SELECT * FROM a6_trade.trade_control_actions
                    WHERE id = %s FOR UPDATE
                    """,
                    (action_id,),
                )
                action = _required_row(cursor, "managed_create_action_missing")
                _validate_create_action(resource_kind, locked, action, expected_status="started")
                if (
                    locked.control_state != "creating"
                    or _create_external_id(locked, resource_kind) is not None
                ):
                    raise ManagedListingError("managed_create_unknown_conflict")
                cursor.execute(
                    _mark_binding_unknown_sql(resource_kind, "create_unknown"),
                    (error_class, locked.binding_id, locked.managed_uid, action_id),
                )
                row = _required_row(cursor, "managed_create_unknown_conflict")
                _finish_action_unknown(cursor, action_id, error_class)
            connection.commit()
            return _binding(resource_kind, row)
        except Exception:
            connection.rollback()
            raise
        finally:
            _close(connection)

    def _mark_create_failed(
        self,
        resource_kind: str,
        binding: ManagedChannelBinding | ManagedListingBinding,
        error_class: str,
        observation: Mapping[str, Any],
    ) -> ManagedChannelBinding | ManagedListingBinding:
        error_class = _validate_error(error_class)
        _validate_document(observation)
        action_id = _positive_int(binding.create_action_id, "managed_create_action_missing")
        next_key = _retry_create_key(resource_kind, binding.managed_uid, action_id)
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    _binding_select(resource_kind) + " FOR UPDATE",
                    (binding.managed_uid,),
                )
                locked = _binding(
                    resource_kind,
                    _required_row(cursor, "managed_create_binding_missing"),
                )
                _same_binding(locked, binding)
                cursor.execute(
                    """
                    SELECT * FROM a6_trade.trade_control_actions
                    WHERE id = %s FOR UPDATE
                    """,
                    (action_id,),
                )
                action = _required_row(cursor, "managed_create_action_missing")
                _validate_create_action(resource_kind, locked, action, expected_status="started")
                cursor.execute(
                    _mark_binding_create_failed_sql(resource_kind),
                    (
                        next_key,
                        _json(observation),
                        error_class,
                        locked.binding_id,
                        locked.managed_uid,
                        action_id,
                        action_id,
                    ),
                )
                row = _required_row(cursor, "managed_create_failed_conflict")
                _finish_action_failed(cursor, action_id, error_class, observation)
            connection.commit()
            return _binding(resource_kind, row)
        except Exception:
            connection.rollback()
            raise
        finally:
            _close(connection)

    def _resolve_create_unknown(
        self,
        resource_kind: str,
        binding: ManagedChannelBinding | ManagedListingBinding,
        desired_state: Mapping[str, Any],
        observation: Mapping[str, Any],
        error_class: str,
    ) -> ManagedChannelBinding | ManagedListingBinding:
        _validate_kind(resource_kind)
        _validate_document(desired_state)
        _validate_document(observation)
        error_class = _validate_error(error_class)
        action_id = _positive_int(binding.create_action_id, "managed_create_action_missing")
        next_key = _retry_create_key(resource_kind, binding.managed_uid, action_id)
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    _binding_select(resource_kind) + " FOR UPDATE",
                    (binding.managed_uid,),
                )
                locked = _binding(
                    resource_kind,
                    _required_row(cursor, "managed_create_binding_missing"),
                )
                _same_binding(locked, binding)
                if (
                    locked.control_state != "create_unknown"
                    or (
                        isinstance(locked, ManagedChannelBinding)
                        and locked.a6_channel_id is not None
                    )
                    or (
                        isinstance(locked, ManagedListingBinding)
                        and locked.a6_listing_id is not None
                    )
                    or locked.create_action_id != action_id
                    or locked.create_idempotency_key != binding.create_idempotency_key
                ):
                    raise ManagedListingError("managed_create_unknown_not_resolvable")
                cursor.execute(
                    """
                    SELECT * FROM a6_trade.trade_control_actions
                    WHERE id = %s FOR UPDATE
                    """,
                    (action_id,),
                )
                action = _required_row(cursor, "managed_create_action_missing")
                _validate_create_action(resource_kind, locked, action, expected_status="unknown")
                if action.get("error_class") != locked.last_error_class or (
                    locked.last_error_class not in _RECOVERABLE_CHANNEL_CREATE_UNKNOWN_ERRORS
                ):
                    raise ManagedListingError("managed_create_unknown_not_resolvable")
                cursor.execute(
                    _resolve_binding_create_unknown_sql(resource_kind),
                    (
                        next_key,
                        _json(desired_state),
                        _json(observation),
                        error_class,
                        locked.binding_id,
                        locked.managed_uid,
                        action_id,
                        action_id,
                    ),
                )
                row = _required_row(cursor, "managed_create_unknown_resolve_conflict")
                _finish_action_failed(cursor, action_id, error_class, observation)
            connection.commit()
            return _binding(resource_kind, row)
        except Exception:
            connection.rollback()
            raise
        finally:
            _close(connection)

    def verify_channel(
        self,
        binding: ManagedChannelBinding,
        channel_id: int,
        observation: Mapping[str, Any],
    ) -> ManagedChannelBinding:
        result = self._verify_created("channel", binding, channel_id, observation)
        assert isinstance(result, ManagedChannelBinding)
        return result

    def verify_listing(
        self,
        binding: ManagedListingBinding,
        listing_id: int,
        observation: Mapping[str, Any],
    ) -> ManagedListingBinding:
        result = self._verify_created("listing", binding, listing_id, observation)
        assert isinstance(result, ManagedListingBinding)
        return result

    def _verify_created(
        self,
        resource_kind: str,
        binding: ManagedChannelBinding | ManagedListingBinding,
        external_id: int,
        observation: Mapping[str, Any],
    ) -> ManagedChannelBinding | ManagedListingBinding:
        external_id = _positive_int(external_id, "managed_external_id_invalid")
        _validate_document(observation)
        if _positive_int(observation.get("id"), "managed_observation_id_invalid") != external_id:
            raise ManagedListingError("managed_observation_id_mismatch")
        action_id = _positive_int(binding.create_action_id, "managed_create_action_missing")
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    _binding_select(resource_kind) + " FOR UPDATE",
                    (binding.managed_uid,),
                )
                locked = _binding(
                    resource_kind,
                    _required_row(cursor, "managed_create_binding_missing"),
                )
                _same_binding(locked, binding)
                cursor.execute(
                    """
                    SELECT * FROM a6_trade.trade_control_actions
                    WHERE id = %s FOR UPDATE
                    """,
                    (action_id,),
                )
                action = _required_row(cursor, "managed_create_action_missing")
                _validate_create_action(resource_kind, locked, action, expected_status="started")
                if (
                    locked.control_state not in {"creating", "verifying"}
                    or _create_external_id(locked, resource_kind) is not None
                ):
                    raise ManagedListingError("managed_verify_binding_conflict")
                if resource_kind == "channel":
                    _upsert_channel_inventory(cursor, locked, external_id, observation)
                else:
                    assert isinstance(locked, ManagedListingBinding)
                    _upsert_listing_inventory(cursor, locked, external_id, observation)
                cursor.execute(
                    _verify_binding_sql(resource_kind),
                    (
                        external_id,
                        _json(observation),
                        locked.binding_id,
                        locked.managed_uid,
                        action_id,
                    ),
                )
                row = _required_row(cursor, "managed_verify_binding_conflict")
                _complete_action(cursor, action_id, observation)
            connection.commit()
            return _binding(resource_kind, row)
        except Exception:
            connection.rollback()
            raise
        finally:
            _close(connection)

    def get_pending_action(self, resource_kind: str, managed_uid: str) -> ManagedAction | None:
        _validate_kind(resource_kind)
        _validate_uid(managed_uid)
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""
                    SELECT action.*, binding.control_state AS state
                    FROM a6_trade.trade_control_actions action
                    JOIN {_binding_table(resource_kind)} binding
                      ON binding.last_action_id = action.id
                    WHERE binding.managed_uid = %s
                      AND action.status IN ('started', 'unknown')
                    ORDER BY action.id DESC LIMIT 1
                    """,
                    (managed_uid,),
                )
                row = _row(cursor)
            return _managed_action(resource_kind, managed_uid, row) if row else None
        finally:
            _close(connection)

    def begin_action(
        self,
        resource_kind: str,
        managed_uid: str,
        operation: str,
        desired_state: Mapping[str, Any],
        idempotency_key: str,
    ) -> ManagedAction | None:
        state = _ACTION_STATES.get((resource_kind, operation))
        if state is None:
            raise ManagedListingError("managed_action_operation_invalid")
        _validate_uid(managed_uid)
        _validate_document(desired_state)
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    _binding_select(resource_kind) + " FOR UPDATE",
                    (managed_uid,),
                )
                row = _required_row(cursor, "managed_action_binding_missing")
                binding = _binding(resource_kind, row)
                if binding.control_state != "verified":
                    connection.rollback()
                    return None
                before = _binding_snapshot(binding)
                cursor.execute(
                    """
                    SELECT * FROM a6_trade.trade_control_actions
                    WHERE idempotency_key = %s FOR UPDATE
                    """,
                    (idempotency_key,),
                )
                if _row(cursor) is not None:
                    connection.rollback()
                    return None
                cursor.execute(
                    """
                    INSERT INTO a6_trade.trade_control_actions (
                        idempotency_key, action_type, entity_type, entity_id,
                        desired_state, observed_before, status,
                        config_version, requested_by, started_at
                    ) VALUES (%s, %s, %s, %s, %s::jsonb, %s::jsonb,
                              'started', %s, %s, now())
                    RETURNING id, action_type, entity_type, entity_id,
                              idempotency_key, desired_state, status
                    """,
                    (
                        idempotency_key,
                        f"{operation}_managed_{resource_kind}",
                        f"a6_managed_{resource_kind}",
                        managed_uid,
                        _json(desired_state),
                        _json(before),
                        self.config_version,
                        self.requested_by,
                    ),
                )
                action_row = _required_row(cursor, "managed_action_audit_failed")
                action_id = _positive_int(
                    action_row.get("id", action_row.get("action_id")),
                    "managed_action_id_invalid",
                )
                cursor.execute(
                    _begin_action_binding_sql(resource_kind, state),
                    (action_id, binding.binding_id, managed_uid),
                )
                _required_row(cursor, "managed_action_claim_conflict")
                _start_attempt(cursor, action_id, before)
            connection.commit()
            return ManagedAction(
                action_id=action_id,
                resource_kind=resource_kind,
                managed_uid=managed_uid,
                operation=operation,
                idempotency_key=idempotency_key,
                desired_state=dict(desired_state),
                state=state,
            )
        except Exception:
            connection.rollback()
            raise
        finally:
            _close(connection)

    def mark_action_unknown(self, action: ManagedAction, error_class: str) -> None:
        _validate_kind(action.resource_kind)
        state = _UNKNOWN_STATES.get(action.operation)
        expected_state = _ACTION_STATES.get((action.resource_kind, action.operation))
        if state is None or expected_state is None:
            raise ManagedListingError("managed_action_operation_invalid")
        _validate_uid(action.managed_uid)
        error_class = _validate_error(error_class)
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    _binding_select(action.resource_kind) + " FOR UPDATE",
                    (action.managed_uid,),
                )
                binding_row = _required_row(cursor, "managed_action_binding_missing")
                cursor.execute(
                    """
                    SELECT * FROM a6_trade.trade_control_actions
                    WHERE id = %s FOR UPDATE
                    """,
                    (action.action_id,),
                )
                action_row = _required_row(cursor, "managed_action_missing")
                _validate_managed_action(
                    action.resource_kind,
                    action,
                    action_row,
                    expected_state=expected_state,
                    error_code="managed_action_unknown_conflict",
                )
                _validate_pending_binding(
                    binding_row,
                    expected_state,
                    action.action_id,
                    "managed_action_unknown_conflict",
                )
                cursor.execute(
                    _mark_binding_unknown_sql(action.resource_kind, state),
                    (error_class, action.managed_uid, action.action_id),
                )
                _required_row(cursor, "managed_action_unknown_conflict")
                _finish_action_unknown(cursor, action.action_id, error_class)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            _close(connection)

    def retry_unknown_listing_update(
        self,
        action: ManagedAction,
    ) -> ManagedAction | None:
        if (
            action.resource_kind != "listing"
            or action.operation != "update"
            or action.state != "update_unknown"
        ):
            raise ManagedListingError("managed_listing_update_retry_invalid")
        return self.retry_unknown_listing_action(action)

    def retry_unknown_listing_action(
        self,
        action: ManagedAction,
    ) -> ManagedAction | None:
        pending_state = _ACTION_STATES.get(("listing", action.operation))
        unknown_state = _UNKNOWN_STATES.get(action.operation)
        if (
            action.resource_kind != "listing"
            or pending_state is None
            or unknown_state is None
            or action.state != unknown_state
        ):
            raise ManagedListingError("managed_listing_action_retry_invalid")
        _validate_uid(action.managed_uid)
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(_LISTING_SELECT + " FOR UPDATE", (action.managed_uid,))
                binding_row = _required_row(
                    cursor,
                    "managed_listing_update_retry_binding_missing",
                )
                binding = _listing_binding(binding_row)
                cursor.execute(
                    """
                    SELECT * FROM a6_trade.trade_control_actions
                    WHERE id = %s FOR UPDATE
                    """,
                    (action.action_id,),
                )
                action_row = _required_row(
                    cursor,
                    "managed_listing_action_retry_action_missing",
                )
                if (
                    binding.control_state != unknown_state
                    or action_row.get("status") != "unknown"
                    or action_row.get("action_type")
                    != f"{action.operation}_managed_listing"
                    or action_row.get("entity_type") != "a6_managed_listing"
                    or action_row.get("entity_id") != action.managed_uid
                    or action_row.get("idempotency_key") != action.idempotency_key
                    or _mapping(action_row.get("desired_state")) != dict(action.desired_state)
                    or binding_row.get("last_action_id") != action.action_id
                ):
                    connection.rollback()
                    return None
                cursor.execute(
                    """
                    UPDATE a6_trade.trade_control_actions
                    SET status = 'started', error_class = NULL,
                        completed_at = NULL, updated_at = now()
                    WHERE id = %s AND status = 'unknown'
                    RETURNING id
                    """,
                    (action.action_id,),
                )
                _required_row(cursor, "managed_listing_update_retry_conflict")
                cursor.execute(
                    """
                    UPDATE a6_trade.a6_managed_listing_bindings
                    SET control_state = %s, last_error_class = NULL,
                        last_attempted_at = now(), updated_at = now()
                    WHERE id = %s AND managed_uid = %s
                      AND last_action_id = %s AND control_state = %s
                    RETURNING *
                    """,
                    (
                        pending_state,
                        binding.binding_id,
                        binding.managed_uid,
                        action.action_id,
                        unknown_state,
                    ),
                )
                _required_row(cursor, "managed_listing_update_retry_conflict")
                _start_attempt(cursor, action.action_id, _binding_snapshot(binding))
            connection.commit()
            return ManagedAction(
                action_id=action.action_id,
                resource_kind=action.resource_kind,
                managed_uid=action.managed_uid,
                operation=action.operation,
                idempotency_key=action.idempotency_key,
                desired_state=dict(action.desired_state),
                state=pending_state,
            )
        except Exception:
            connection.rollback()
            raise
        finally:
            _close(connection)

    def retry_unknown_channel_update(
        self,
        action: ManagedAction,
    ) -> ManagedAction | None:
        if (
            action.resource_kind != "channel"
            or action.operation != "update"
            or action.state != "update_unknown"
        ):
            raise ManagedListingError("managed_channel_update_retry_invalid")
        _validate_uid(action.managed_uid)
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(_CHANNEL_SELECT + " FOR UPDATE", (action.managed_uid,))
                binding_row = _required_row(
                    cursor,
                    "managed_channel_update_retry_binding_missing",
                )
                binding = _channel_binding(binding_row)
                cursor.execute(
                    """
                    SELECT * FROM a6_trade.trade_control_actions
                    WHERE id = %s FOR UPDATE
                    """,
                    (action.action_id,),
                )
                action_row = _required_row(
                    cursor,
                    "managed_channel_update_retry_action_missing",
                )
                if (
                    binding.control_state != "update_unknown"
                    or action_row.get("status") != "unknown"
                    or action_row.get("action_type") != "update_managed_channel"
                    or action_row.get("entity_type") != "a6_managed_channel"
                    or action_row.get("entity_id") != action.managed_uid
                    or action_row.get("idempotency_key") != action.idempotency_key
                    or _mapping(action_row.get("desired_state")) != dict(action.desired_state)
                    or binding_row.get("last_action_id") != action.action_id
                ):
                    connection.rollback()
                    return None
                cursor.execute(
                    """
                    UPDATE a6_trade.trade_control_actions
                    SET status = 'started', error_class = NULL,
                        completed_at = NULL, updated_at = now()
                    WHERE id = %s AND status = 'unknown'
                    RETURNING id
                    """,
                    (action.action_id,),
                )
                _required_row(cursor, "managed_channel_update_retry_conflict")
                cursor.execute(
                    """
                    UPDATE a6_trade.a6_managed_channel_bindings
                    SET control_state = 'updating', last_error_class = NULL,
                        last_attempted_at = now(), updated_at = now()
                    WHERE id = %s AND managed_uid = %s
                      AND last_action_id = %s AND control_state = 'update_unknown'
                    RETURNING *
                    """,
                    (binding.binding_id, binding.managed_uid, action.action_id),
                )
                _required_row(cursor, "managed_channel_update_retry_conflict")
                _start_attempt(cursor, action.action_id, _binding_snapshot(binding))
            connection.commit()
            return ManagedAction(
                action_id=action.action_id,
                resource_kind=action.resource_kind,
                managed_uid=action.managed_uid,
                operation=action.operation,
                idempotency_key=action.idempotency_key,
                desired_state=dict(action.desired_state),
                state="updating",
            )
        except Exception:
            connection.rollback()
            raise
        finally:
            _close(connection)

    def supersede_listing_update(
        self,
        action: ManagedAction,
        observation: Mapping[str, Any],
    ) -> ManagedListingBinding:
        if (
            action.resource_kind != "listing"
            or action.operation != "update"
            or action.state not in {"updating", "update_unknown"}
        ):
            raise ManagedListingError("managed_listing_update_supersede_invalid")
        _validate_uid(action.managed_uid)
        _validate_document(observation)
        expected_status = "unknown" if action.state == "update_unknown" else "started"
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(_LISTING_SELECT + " FOR UPDATE", (action.managed_uid,))
                binding_row = _required_row(
                    cursor,
                    "managed_listing_update_supersede_binding_missing",
                )
                binding = _listing_binding(binding_row)
                cursor.execute(
                    """
                    SELECT * FROM a6_trade.trade_control_actions
                    WHERE id = %s FOR UPDATE
                    """,
                    (action.action_id,),
                )
                action_row = _required_row(
                    cursor,
                    "managed_listing_update_supersede_action_missing",
                )
                if (
                    binding.control_state != action.state
                    or binding_row.get("last_action_id") != action.action_id
                    or action_row.get("status") != expected_status
                    or action_row.get("action_type") != "update_managed_listing"
                    or action_row.get("entity_type") != "a6_managed_listing"
                    or action_row.get("entity_id") != action.managed_uid
                    or action_row.get("idempotency_key") != action.idempotency_key
                    or _mapping(action_row.get("desired_state")) != dict(action.desired_state)
                ):
                    raise ManagedListingError("managed_listing_update_supersede_conflict")
                _upsert_listing_inventory(
                    cursor,
                    binding,
                    _positive_int(
                        binding.a6_listing_id,
                        "managed_listing_update_supersede_conflict",
                    ),
                    observation,
                )
                observed = _json(observation)
                error_class = "managed_listing_update_superseded_by_new_target"
                cursor.execute(
                    """
                    UPDATE a6_trade.trade_control_actions
                    SET observed_after = %s::jsonb, status = 'superseded',
                        error_class = %s, completed_at = now(), updated_at = now()
                    WHERE id = %s AND status = %s
                    RETURNING id
                    """,
                    (observed, error_class, action.action_id, expected_status),
                )
                _required_row(cursor, "managed_listing_update_supersede_conflict")
                cursor.execute(
                    """
                    UPDATE a6_trade.trade_control_action_attempts
                    SET observed_after = %s::jsonb, status = 'superseded',
                        error_class = %s, completed_at = now()
                    WHERE action_id = %s AND attempt_no = (
                        SELECT max(attempt_no)
                        FROM a6_trade.trade_control_action_attempts
                        WHERE action_id = %s
                    ) AND status IN ('started', 'unknown')
                    """,
                    (observed, error_class, action.action_id, action.action_id),
                )
                cursor.execute(
                    """
                    UPDATE a6_trade.a6_managed_listing_bindings
                    SET control_state = 'verified', observed_state = %s::jsonb,
                        last_error_class = NULL, last_observed_at = now(),
                        verified_at = coalesce(verified_at, now()), updated_at = now()
                    WHERE id = %s AND managed_uid = %s
                      AND last_action_id = %s AND control_state = %s
                    RETURNING *
                    """,
                    (
                        observed,
                        binding.binding_id,
                        binding.managed_uid,
                        action.action_id,
                        action.state,
                    ),
                )
                row = _required_row(
                    cursor,
                    "managed_listing_update_supersede_conflict",
                )
            connection.commit()
            return _listing_binding(row)
        except Exception:
            connection.rollback()
            raise
        finally:
            _close(connection)

    def complete_channel_action(
        self,
        binding: ManagedChannelBinding,
        action: ManagedAction,
        observation: Mapping[str, Any],
    ) -> ManagedChannelBinding:
        result = self._complete_resource_action("channel", binding, action, observation)
        assert isinstance(result, ManagedChannelBinding)
        return result

    def complete_listing_action(
        self,
        binding: ManagedListingBinding,
        action: ManagedAction,
        observation: Mapping[str, Any],
    ) -> ManagedListingBinding:
        result = self._complete_resource_action("listing", binding, action, observation)
        assert isinstance(result, ManagedListingBinding)
        return result

    def _complete_resource_action(
        self,
        resource_kind: str,
        binding: ManagedChannelBinding | ManagedListingBinding,
        action: ManagedAction,
        observation: Mapping[str, Any],
    ) -> ManagedChannelBinding | ManagedListingBinding:
        if action.resource_kind != resource_kind or action.managed_uid != binding.managed_uid:
            raise ManagedListingError("managed_action_binding_mismatch")
        _validate_kind(resource_kind)
        expected_state = _ACTION_STATES.get((resource_kind, action.operation))
        if expected_state is None:
            raise ManagedListingError("managed_action_operation_invalid")
        unknown_state = _UNKNOWN_STATES.get(action.operation)
        if action.state == expected_state:
            pending_state = expected_state
            expected_status = "started"
        elif action.state == unknown_state:
            pending_state = action.state
            expected_status = "unknown"
        else:
            raise ManagedListingError("managed_action_complete_conflict")
        _validate_document(observation)
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    _binding_select(resource_kind) + " FOR UPDATE",
                    (binding.managed_uid,),
                )
                binding_row = _required_row(cursor, "managed_action_binding_missing")
                locked = _binding(resource_kind, binding_row)
                _same_binding(locked, binding)
                cursor.execute(
                    """
                    SELECT * FROM a6_trade.trade_control_actions
                    WHERE id = %s FOR UPDATE
                    """,
                    (action.action_id,),
                )
                action_row = _required_row(cursor, "managed_action_missing")
                _validate_managed_action(
                    resource_kind,
                    action,
                    action_row,
                    expected_state=pending_state,
                    expected_status=expected_status,
                    error_code="managed_action_complete_conflict",
                )
                _validate_pending_binding(
                    binding_row,
                    pending_state,
                    action.action_id,
                    "managed_action_complete_conflict",
                )
                external_id = (
                    locked.a6_channel_id
                    if resource_kind == "channel"
                    else getattr(locked, "a6_listing_id", None)
                )
                external_id = _positive_int(external_id, "managed_external_id_invalid")
                if resource_kind == "channel":
                    _upsert_channel_inventory(cursor, locked, external_id, observation)
                else:
                    assert isinstance(locked, ManagedListingBinding)
                    _upsert_listing_inventory(cursor, locked, external_id, observation)
                cursor.execute(
                    _complete_binding_action_sql(resource_kind, pending_state),
                    (
                        _json(observation),
                        locked.binding_id,
                        locked.managed_uid,
                        action.action_id,
                    ),
                )
                row = _required_row(cursor, "managed_action_complete_conflict")
                _complete_action(
                    cursor,
                    action.action_id,
                    observation,
                    expected_status=expected_status,
                )
            connection.commit()
            return _binding(resource_kind, row)
        except Exception:
            connection.rollback()
            raise
        finally:
            _close(connection)


_CHANNEL_SELECT = """
    SELECT * FROM a6_trade.a6_managed_channel_bindings WHERE managed_uid = %s
"""
_LISTING_SELECT = """
    SELECT * FROM a6_trade.a6_managed_listing_bindings WHERE managed_uid = %s
"""


def _binding_table(resource_kind: str) -> str:
    _validate_kind(resource_kind)
    return f"a6_trade.a6_managed_{resource_kind}_bindings"


def _binding_select(resource_kind: str) -> str:
    return _CHANNEL_SELECT if resource_kind == "channel" else _LISTING_SELECT


def _pending_action_state(resource_kind: str, unknown_state: str) -> str:
    _validate_kind(resource_kind)
    for (kind, _operation), pending_state in _ACTION_STATES.items():
        if kind == resource_kind:
            operation_unknown_state = _UNKNOWN_STATES.get(_operation)
            if operation_unknown_state == unknown_state:
                return pending_state
    raise ManagedListingError("managed_action_operation_invalid")


def _claim_binding_sql(resource_kind: str) -> str:
    table = _binding_table(resource_kind)
    return f"""
        UPDATE {table}
        SET control_state = 'creating',
            create_action_id = coalesce(create_action_id, %s),
            last_action_id = %s,
            first_attempted_at = coalesce(first_attempted_at, now()),
            last_attempted_at = now(), last_error_class = NULL, updated_at = now()
        WHERE id = %s AND managed_uid = %s AND control_state IN ('planned', 'error')
        RETURNING *
    """


def _update_binding_observation_sql(resource_kind: str, state: str) -> str:
    table = _binding_table(resource_kind)
    external_guard = "a6_channel_id IS NULL"
    if resource_kind == "listing":
        external_guard = "a6_listing_id IS NULL"
    return f"""
        UPDATE {table}
        SET control_state = '{state}', observed_state = %s::jsonb,
            last_observed_at = now(), updated_at = now()
        WHERE id = %s AND managed_uid = %s AND create_action_id = %s
          AND control_state = 'creating' AND {external_guard}
        RETURNING *
    """


def _mark_binding_unknown_sql(resource_kind: str, state: str) -> str:
    table = _binding_table(resource_kind)
    if state == "create_unknown":
        external_guard = "a6_channel_id IS NULL"
        if resource_kind == "listing":
            external_guard = "a6_listing_id IS NULL"
        where = (
            "id = %s AND managed_uid = %s AND create_action_id = %s "
            f"AND control_state = 'creating' AND {external_guard}"
        )
    else:
        pending_state = _pending_action_state(resource_kind, state)
        where = f"managed_uid = %s AND last_action_id = %s AND control_state = '{pending_state}'"
    return f"""
        UPDATE {table}
        SET control_state = '{state}', last_error_class = %s,
            last_attempted_at = now(), updated_at = now()
        WHERE {where}
        RETURNING *
    """


def _mark_binding_create_failed_sql(resource_kind: str) -> str:
    table = _binding_table(resource_kind)
    external_guard = "a6_channel_id IS NULL"
    if resource_kind == "listing":
        external_guard = "a6_listing_id IS NULL"
    return f"""
        UPDATE {table}
        SET create_idempotency_key = %s, control_state = 'error',
            create_action_id = NULL, observed_state = %s::jsonb,
            last_error_class = %s, last_observed_at = now(),
            last_attempted_at = now(), updated_at = now()
        WHERE id = %s AND managed_uid = %s AND create_action_id = %s
          AND last_action_id = %s
          AND control_state IN ('creating', 'verifying')
          AND {external_guard}
        RETURNING *
    """


def _resolve_binding_create_unknown_sql(resource_kind: str) -> str:
    table = _binding_table(resource_kind)
    external_guard = "a6_channel_id IS NULL"
    if resource_kind == "listing":
        external_guard = "a6_listing_id IS NULL"
    dependency_guard = ""
    if resource_kind == "channel":
        dependency_guard = """
          AND NOT EXISTS (
              SELECT 1 FROM a6_trade.a6_managed_listing_bindings child
              WHERE child.channel_binding_id = target.id
          )
        """
    return f"""
        UPDATE {table} AS target
        SET create_idempotency_key = %s, desired_state = %s::jsonb,
            control_state = 'error', create_action_id = NULL,
            observed_state = %s::jsonb, last_error_class = %s,
            last_observed_at = now(), updated_at = now()
        WHERE id = %s AND managed_uid = %s AND create_action_id = %s
          AND last_action_id = %s
          AND control_state = 'create_unknown' AND {external_guard}
          {dependency_guard}
        RETURNING *
    """


def _verify_binding_sql(resource_kind: str) -> str:
    table = _binding_table(resource_kind)
    id_column = "a6_channel_id" if resource_kind == "channel" else "a6_listing_id"
    return f"""
        UPDATE {table}
        SET {id_column} = %s, observed_state = %s::jsonb,
            control_state = 'verified', last_error_class = NULL,
            last_observed_at = now(), verified_at = now(), updated_at = now()
        WHERE id = %s AND managed_uid = %s AND create_action_id = %s
          AND control_state IN ('creating', 'verifying')
          AND {id_column} IS NULL
        RETURNING *
    """


def _begin_action_binding_sql(resource_kind: str, state: str) -> str:
    table = _binding_table(resource_kind)
    return f"""
        UPDATE {table}
        SET control_state = '{state}', last_action_id = %s,
            last_attempted_at = now(), last_error_class = NULL, updated_at = now()
        WHERE id = %s AND managed_uid = %s AND control_state = 'verified'
        RETURNING *
    """


def _complete_binding_action_sql(resource_kind: str, expected_state: str) -> str:
    table = _binding_table(resource_kind)
    return f"""
        UPDATE {table}
        SET control_state = 'verified', observed_state = %s::jsonb,
            last_error_class = NULL, last_observed_at = now(),
            verified_at = now(), updated_at = now()
        WHERE id = %s AND managed_uid = %s AND last_action_id = %s
          AND control_state = '{expected_state}'
        RETURNING *
    """


def _start_attempt(cursor: Any, action_id: int, observed_before: Mapping[str, Any]) -> int:
    cursor.execute(
        """
        SELECT coalesce(max(attempt_no), 0) + 1 AS attempt_no
        FROM a6_trade.trade_control_action_attempts WHERE action_id = %s
        """,
        (action_id,),
    )
    row = _required_row(cursor, "managed_action_attempt_invalid")
    attempt_no = _positive_int(row.get("attempt_no"), "managed_action_attempt_invalid")
    cursor.execute(
        """
        INSERT INTO a6_trade.trade_control_action_attempts (
            action_id, attempt_no, status, observed_before, started_at
        ) VALUES (%s, %s, 'started', %s::jsonb, now())
        """,
        (action_id, attempt_no, _json(observed_before)),
    )
    return attempt_no


def _finish_action_unknown(cursor: Any, action_id: int, error_class: str) -> None:
    fingerprint = hashlib.sha256(error_class.encode()).hexdigest()
    cursor.execute(
        """
        UPDATE a6_trade.trade_control_actions
        SET status = 'unknown', error_class = %s,
            completed_at = now(), updated_at = now()
        WHERE id = %s AND status = 'started'
        """,
        (error_class, action_id),
    )
    cursor.execute(
        """
        UPDATE a6_trade.trade_control_action_attempts
        SET status = 'unknown', error_class = %s, error_fingerprint = %s,
            completed_at = now()
        WHERE action_id = %s AND attempt_no = (
            SELECT max(attempt_no) FROM a6_trade.trade_control_action_attempts
            WHERE action_id = %s
        ) AND status = 'started'
        """,
        (error_class, fingerprint, action_id, action_id),
    )


def _finish_action_failed(
    cursor: Any,
    action_id: int,
    error_class: str,
    observation: Mapping[str, Any],
) -> None:
    observed = _json(observation)
    fingerprint = hashlib.sha256(error_class.encode()).hexdigest()
    cursor.execute(
        """
        UPDATE a6_trade.trade_control_actions
        SET observed_after = %s::jsonb, status = 'failed', error_class = %s,
            completed_at = coalesce(completed_at, now()), updated_at = now()
        WHERE id = %s AND status IN ('started', 'unknown')
        """,
        (observed, error_class, action_id),
    )
    cursor.execute(
        """
        UPDATE a6_trade.trade_control_action_attempts
        SET observed_after = %s::jsonb, status = 'failed', error_class = %s,
            error_fingerprint = %s, completed_at = coalesce(completed_at, now())
        WHERE action_id = %s AND attempt_no = (
            SELECT max(attempt_no) FROM a6_trade.trade_control_action_attempts
            WHERE action_id = %s
        ) AND status IN ('started', 'unknown')
        """,
        (observed, error_class, fingerprint, action_id, action_id),
    )


def _retry_create_key(resource_kind: str, managed_uid: str, action_id: int) -> str:
    _validate_kind(resource_kind)
    _validate_uid(managed_uid)
    _positive_int(action_id, "managed_create_action_missing")
    return f"managed-{resource_kind}-create:{managed_uid}:retry:{action_id}"


def _validate_create_action(
    resource_kind: str,
    binding: ManagedChannelBinding | ManagedListingBinding,
    action: Mapping[str, Any],
    *,
    expected_status: str,
) -> None:
    action_id = _positive_int(action.get("id"), "managed_create_action_invalid")
    if (
        action_id != binding.create_action_id
        or action.get("action_type") != f"create_managed_{resource_kind}"
        or action.get("entity_type") != f"a6_managed_{resource_kind}"
        or action.get("entity_id") != binding.managed_uid
        or action.get("idempotency_key") != binding.create_idempotency_key
        or action.get("status") != expected_status
        or _mapping(action.get("desired_state")) != dict(binding.desired_state)
    ):
        raise ManagedListingError("managed_create_action_conflict")


def _validate_managed_action(
    resource_kind: str,
    action: ManagedAction,
    row: Mapping[str, Any],
    *,
    expected_state: str,
    expected_status: str = "started",
    error_code: str,
) -> None:
    if (
        action.resource_kind != resource_kind
        or action.state != expected_state
        or row.get("id", row.get("action_id")) != action.action_id
        or row.get("action_type") != f"{action.operation}_managed_{resource_kind}"
        or row.get("entity_type") != f"a6_managed_{resource_kind}"
        or row.get("entity_id") != action.managed_uid
        or row.get("idempotency_key") != action.idempotency_key
        or row.get("status") != expected_status
        or _mapping(row.get("desired_state")) != dict(action.desired_state)
    ):
        raise ManagedListingError(error_code)


def _validate_pending_binding(
    row: Mapping[str, Any],
    expected_state: str,
    action_id: int,
    error_code: str,
) -> None:
    if row.get("control_state") != expected_state or row.get("last_action_id") != action_id:
        raise ManagedListingError(error_code)


def _complete_action(
    cursor: Any,
    action_id: int,
    observation: Mapping[str, Any],
    *,
    expected_status: str = "started",
) -> None:
    observed = _json(observation)
    cursor.execute(
        """
        UPDATE a6_trade.trade_control_actions
        SET observed_after = %s::jsonb, status = 'completed', error_class = NULL,
            completed_at = now(), updated_at = now()
        WHERE id = %s AND status = %s
        """,
        (observed, action_id, expected_status),
    )
    cursor.execute(
        """
        UPDATE a6_trade.trade_control_action_attempts
        SET observed_after = %s::jsonb, status = 'completed', error_class = NULL,
            completed_at = now()
        WHERE action_id = %s AND attempt_no = (
            SELECT max(attempt_no) FROM a6_trade.trade_control_action_attempts
            WHERE action_id = %s
        ) AND status = %s
        """,
        (observed, action_id, action_id, expected_status),
    )


def _upsert_channel_inventory(
    cursor: Any,
    binding: ManagedChannelBinding | ManagedListingBinding,
    channel_id: int,
    observation: Mapping[str, Any],
) -> None:
    source_hash = _document_hash(observation)
    cursor.execute(
        """
        INSERT INTO a6_trade.a6_supplier_channels_current (
            merchant_id, a6_channel_id, supplier_id, name, nickname,
            channel_type, status, availability, observed_at, source_hash
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, now(), %s)
        ON CONFLICT (merchant_id, a6_channel_id) DO UPDATE SET
            supplier_id = EXCLUDED.supplier_id, name = EXCLUDED.name,
            nickname = EXCLUDED.nickname, channel_type = EXCLUDED.channel_type,
            status = EXCLUDED.status, availability = EXCLUDED.availability,
            observed_at = EXCLUDED.observed_at, source_hash = EXCLUDED.source_hash,
            updated_at = now()
        RETURNING merchant_id, a6_channel_id
        """,
        (
            binding.merchant_id,
            channel_id,
            _optional_int(observation.get("supplier_id")),
            str(observation.get("name") or ""),
            str(observation.get("nickname") or ""),
            _optional_int(observation.get("channel_type")),
            _optional_int(observation.get("status")),
            _optional_int(observation.get("availability")),
            source_hash,
        ),
    )


def _upsert_listing_inventory(
    cursor: Any,
    binding: ManagedListingBinding,
    listing_id: int,
    observation: Mapping[str, Any],
) -> None:
    channel_id = _positive_int(
        observation.get("channel_id", binding.a6_channel_id),
        "managed_listing_channel_id_invalid",
    )
    if channel_id != binding.a6_channel_id:
        raise ManagedListingError("managed_listing_channel_id_mismatch")
    prices = [
        _nonnegative_int(observation.get(name), f"managed_listing_{name}_invalid")
        for name in (
            "input_price_micros",
            "output_price_micros",
            "cache_read_price_micros",
            "cache_write_price_micros",
        )
    ]
    cursor.execute(
        """
        INSERT INTO a6_trade.a6_supplier_listings_current (
            merchant_id, a6_listing_id, a6_channel_id, own_model, platform_model,
            charge_type, input_price_micros, output_price_micros,
            cache_read_price_micros, cache_write_price_micros,
            status, availability, visibility, display_name, description,
            managed_state, observed_at, source_hash
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                  %s, %s, %s, %s, %s, 'managed', now(), %s)
        ON CONFLICT (merchant_id, a6_listing_id) DO UPDATE SET
            a6_channel_id = EXCLUDED.a6_channel_id,
            own_model = EXCLUDED.own_model, platform_model = EXCLUDED.platform_model,
            charge_type = EXCLUDED.charge_type,
            input_price_micros = EXCLUDED.input_price_micros,
            output_price_micros = EXCLUDED.output_price_micros,
            cache_read_price_micros = EXCLUDED.cache_read_price_micros,
            cache_write_price_micros = EXCLUDED.cache_write_price_micros,
            status = EXCLUDED.status, availability = EXCLUDED.availability,
            visibility = EXCLUDED.visibility, display_name = EXCLUDED.display_name,
            description = EXCLUDED.description, managed_state = 'managed',
            observed_at = EXCLUDED.observed_at, source_hash = EXCLUDED.source_hash,
            updated_at = now()
        RETURNING merchant_id, a6_listing_id
        """,
        (
            binding.merchant_id,
            listing_id,
            channel_id,
            str(observation.get("own_model") or ""),
            str(observation.get("platform_model") or ""),
            str(observation.get("charge_type") or "unknown"),
            *prices,
            _optional_int(observation.get("status")),
            _optional_int(observation.get("availability")),
            _optional_int(observation.get("visibility")),
            str(observation.get("display_name") or ""),
            str(observation.get("description") or ""),
            _document_hash(observation),
        ),
    )


def _managed_action(resource_kind: str, managed_uid: str, row: Mapping[str, Any]) -> ManagedAction:
    action_type = str(row.get("action_type") or "")
    operation = str(row.get("operation") or action_type.removesuffix(f"_managed_{resource_kind}"))
    state = str(row.get("state") or _UNKNOWN_STATES.get(operation) or "updating")
    return ManagedAction(
        action_id=_positive_int(row.get("id", row.get("action_id")), "managed_action_id_invalid"),
        resource_kind=resource_kind,
        managed_uid=managed_uid,
        operation=operation,
        idempotency_key=str(row.get("idempotency_key") or ""),
        desired_state=_mapping(row.get("desired_state")),
        state=state,
    )


def _channel_binding(row: Mapping[str, Any]) -> ManagedChannelBinding:
    return ManagedChannelBinding(
        binding_id=_positive_int(
            row.get("binding_id", row.get("id")), "managed_binding_id_invalid"
        ),
        managed_uid=str(row.get("managed_uid") or ""),
        merchant_id=_positive_int(row.get("merchant_id"), "managed_merchant_id_invalid"),
        create_idempotency_key=str(row.get("create_idempotency_key") or ""),
        control_state=str(row.get("control_state") or ""),
        desired_state=_mapping(row.get("desired_state")),
        observed_state=_optional_mapping(row.get("observed_state")),
        a6_channel_id=_optional_positive_int(row.get("a6_channel_id")),
        create_action_id=_optional_positive_int(row.get("create_action_id")),
        last_error_class=str(row.get("last_error_class") or "") or None,
    )


def _listing_binding(row: Mapping[str, Any]) -> ManagedListingBinding:
    return ManagedListingBinding(
        binding_id=_positive_int(
            row.get("binding_id", row.get("id")), "managed_binding_id_invalid"
        ),
        managed_uid=str(row.get("managed_uid") or ""),
        merchant_id=_positive_int(row.get("merchant_id"), "managed_merchant_id_invalid"),
        channel_binding_id=_positive_int(
            row.get("channel_binding_id"), "managed_channel_binding_id_invalid"
        ),
        create_idempotency_key=str(row.get("create_idempotency_key") or ""),
        control_state=str(row.get("control_state") or ""),
        desired_state=_mapping(row.get("desired_state")),
        observed_state=_optional_mapping(row.get("observed_state")),
        a6_channel_id=_optional_positive_int(row.get("a6_channel_id")),
        a6_listing_id=_optional_positive_int(row.get("a6_listing_id")),
        create_action_id=_optional_positive_int(row.get("create_action_id")),
        last_error_class=str(row.get("last_error_class") or "") or None,
    )


def _binding(
    resource_kind: str, row: Mapping[str, Any]
) -> ManagedChannelBinding | ManagedListingBinding:
    return _channel_binding(row) if resource_kind == "channel" else _listing_binding(row)


def _verify_channel_intent(binding: ManagedChannelBinding, intent: ChannelCreateIntent) -> None:
    if (
        binding.merchant_id != intent.merchant_id
        or binding.create_idempotency_key != intent.create_idempotency_key
        or dict(binding.desired_state) != dict(intent.desired_state)
    ):
        raise ManagedListingError("managed_channel_reservation_conflict")


def _verify_listing_intent(binding: ManagedListingBinding, intent: ListingCreateIntent) -> None:
    if (
        binding.merchant_id != intent.merchant_id
        or binding.channel_binding_id != intent.channel_binding_id
        or binding.a6_channel_id != intent.a6_channel_id
        or binding.create_idempotency_key != intent.create_idempotency_key
        or dict(binding.desired_state) != dict(intent.desired_state)
    ):
        raise ManagedListingError("managed_listing_reservation_conflict")


def _same_binding(
    actual: ManagedChannelBinding | ManagedListingBinding,
    expected: ManagedChannelBinding | ManagedListingBinding,
) -> None:
    if (
        type(actual) is not type(expected)
        or actual.binding_id != expected.binding_id
        or actual.managed_uid != expected.managed_uid
        or actual.merchant_id != expected.merchant_id
    ):
        raise ManagedListingError("managed_binding_changed")


def _binding_snapshot(
    binding: ManagedChannelBinding | ManagedListingBinding,
) -> dict[str, Any]:
    value = {
        "binding_id": binding.binding_id,
        "managed_uid": binding.managed_uid,
        "merchant_id": binding.merchant_id,
        "control_state": binding.control_state,
        "a6_channel_id": binding.a6_channel_id,
    }
    if isinstance(binding, ManagedListingBinding):
        value["a6_listing_id"] = binding.a6_listing_id
        value["channel_binding_id"] = binding.channel_binding_id
    return value


def _create_external_id(
    binding: ManagedChannelBinding | ManagedListingBinding, resource_kind: str
) -> int | None:
    if resource_kind == "channel":
        return binding.a6_channel_id
    assert isinstance(binding, ManagedListingBinding)
    return binding.a6_listing_id


def _validate_kind(resource_kind: str) -> None:
    if resource_kind not in {"channel", "listing"}:
        raise ManagedListingError("managed_resource_kind_invalid")


def _validate_uid(value: str) -> None:
    if not value or len(value) > 256:
        raise ManagedListingError("managed_uid_invalid")


def _validate_intent(managed_uid: str, merchant_id: int, desired_state: Mapping[str, Any]) -> None:
    _validate_uid(managed_uid)
    _positive_int(merchant_id, "managed_merchant_id_invalid")
    _validate_document(desired_state)


def _validate_adoption_intent(
    managed_uid: str,
    merchant_id: int,
    external_id: int,
    idempotency_key: str,
    desired_state: Mapping[str, Any],
    observation: Mapping[str, Any],
) -> None:
    _validate_intent(managed_uid, merchant_id, desired_state)
    _positive_int(external_id, "managed_external_id_invalid")
    if not _LABEL.fullmatch(str(idempotency_key or "")) and not str(
        idempotency_key or ""
    ).startswith("managed-"):
        raise ManagedListingError("managed_adoption_idempotency_key_invalid")
    if not idempotency_key or len(idempotency_key) > 512:
        raise ManagedListingError("managed_adoption_idempotency_key_invalid")
    _validate_document(observation)


def _validate_document(value: Mapping[str, Any]) -> None:
    if not isinstance(value, Mapping):
        raise ManagedListingError("managed_document_invalid")

    def walk(item: Any) -> None:
        if isinstance(item, Mapping):
            for key, nested in item.items():
                if str(key).casefold() in _FORBIDDEN_KEYS:
                    raise ManagedListingError("managed_document_contains_secret")
                walk(nested)
        elif isinstance(item, (list, tuple)):
            for nested in item:
                walk(nested)

    walk(value)


def _validate_error(value: str) -> str:
    error = str(value or "")
    if not _ERROR.fullmatch(error):
        raise ManagedListingError("managed_error_class_invalid")
    return error


def _mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, Mapping):
            return dict(parsed)
    raise ManagedListingError("managed_document_invalid")


def _optional_mapping(value: Any) -> dict[str, Any] | None:
    return None if value is None else _mapping(value)


def _positive_int(value: Any, code: str) -> int:
    if isinstance(value, bool):
        raise ManagedListingError(code)
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise ManagedListingError(code) from None
    if parsed < 1:
        raise ManagedListingError(code)
    return parsed


def _nonnegative_int(value: Any, code: str) -> int:
    if isinstance(value, bool):
        raise ManagedListingError(code)
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise ManagedListingError(code) from None
    if parsed < 0:
        raise ManagedListingError(code)
    return parsed


def _optional_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _optional_positive_int(value: Any) -> int | None:
    parsed = _optional_int(value)
    return parsed if parsed is not None and parsed > 0 else None


def _document_hash(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _json(value: Mapping[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _row(cursor: Any) -> dict[str, Any] | None:
    raw = cursor.fetchone()
    if raw is None:
        return None
    if isinstance(raw, Mapping):
        return dict(raw)
    names = [getattr(column, "name", column[0]) for column in cursor.description]
    return dict(zip(names, raw, strict=True))


def _rows(cursor: Any) -> list[dict[str, Any]]:
    raw = cursor.fetchall()
    if not raw:
        return []
    if isinstance(raw[0], Mapping):
        return [dict(item) for item in raw]
    names = [getattr(column, "name", column[0]) for column in cursor.description]
    return [dict(zip(names, item, strict=True)) for item in raw]


def _required_row(cursor: Any, code: str) -> dict[str, Any]:
    row = _row(cursor)
    if row is None:
        raise ManagedListingError(code)
    return row


def _close(connection: Any) -> None:
    close = getattr(connection, "close", None)
    if callable(close):
        close()
