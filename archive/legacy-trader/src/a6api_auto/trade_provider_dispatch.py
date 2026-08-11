"""PostgreSQL-authoritative Redis projection for active Provider dispatch sets."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Protocol


class ProviderDispatchError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class ProviderDispatchSlot:
    slot_no: int
    account_id: int
    token_ref_id: int
    external_token_id: int
    channel_id: int
    rank: int
    priority: int
    cost_multiplier: Decimal

    def __post_init__(self) -> None:
        identities = (
            self.slot_no,
            self.account_id,
            self.token_ref_id,
            self.external_token_id,
            self.channel_id,
            self.rank,
            self.priority,
        )
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 1
            for value in identities
        ):
            raise ProviderDispatchError("provider_dispatch_slot_identity_invalid")
        if self.priority < 2 or self.priority != self.rank + 1:
            raise ProviderDispatchError("provider_dispatch_slot_priority_invalid")
        multiplier = _positive_decimal(
            self.cost_multiplier,
            "provider_dispatch_slot_cost_invalid",
        )
        object.__setattr__(self, "cost_multiplier", multiplier)

    def document(self) -> dict[str, Any]:
        return {
            "slot_no": self.slot_no,
            "account_id": self.account_id,
            "token_ref_id": self.token_ref_id,
            "external_token_id": self.external_token_id,
            "channel_id": self.channel_id,
            "rank": self.rank,
            "priority": self.priority,
            "cost_multiplier": format(self.cost_multiplier, "f"),
        }


@dataclass(frozen=True)
class ProviderDispatchBundle:
    model: str
    routing_generation: int
    cost_generation: int
    source_hash: str
    cost_input_hash: str
    cost_output_hash: str
    ready_slots: tuple[ProviderDispatchSlot, ...]
    state: str = "active"
    schema_version: int = 1

    def __post_init__(self) -> None:
        model = str(self.model).strip()
        if not model or any(ord(character) < 0x20 for character in model):
            raise ProviderDispatchError("provider_dispatch_model_invalid")
        if self.schema_version != 1:
            raise ProviderDispatchError("provider_dispatch_schema_invalid")
        state = str(self.state).strip().casefold()
        if state not in {"active", "absent"}:
            raise ProviderDispatchError("provider_dispatch_state_invalid")
        generations = (self.routing_generation, self.cost_generation)
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in generations
        ):
            raise ProviderDispatchError("provider_dispatch_generation_invalid")
        hashes = (
            str(self.source_hash).casefold(),
            str(self.cost_input_hash).casefold(),
            str(self.cost_output_hash).casefold(),
        )
        if state == "active" and (
            min(generations) < 1 or not all(_sha256(value) for value in hashes)
        ):
            raise ProviderDispatchError("provider_dispatch_hash_invalid")
        if state == "absent" and hashes != ("0" * 64, "0" * 64, "0" * 64):
            raise ProviderDispatchError("provider_dispatch_tombstone_hash_invalid")
        slots = tuple(self.ready_slots)
        if any(not isinstance(slot, ProviderDispatchSlot) for slot in slots):
            raise ProviderDispatchError("provider_dispatch_slot_invalid")
        if slots != tuple(sorted(slots, key=lambda slot: (slot.priority, slot.slot_no))):
            raise ProviderDispatchError("provider_dispatch_slot_order_invalid")
        if state == "absent" and slots:
            raise ProviderDispatchError("provider_dispatch_tombstone_slots_invalid")
        for values, code in (
            ((slot.slot_no for slot in slots), "provider_dispatch_slot_duplicate"),
            ((slot.account_id for slot in slots), "provider_dispatch_account_duplicate"),
            ((slot.token_ref_id for slot in slots), "provider_dispatch_token_duplicate"),
            (
                (slot.external_token_id for slot in slots),
                "provider_dispatch_external_token_duplicate",
            ),
            ((slot.channel_id for slot in slots), "provider_dispatch_channel_duplicate"),
            ((slot.rank for slot in slots), "provider_dispatch_rank_duplicate"),
            ((slot.priority for slot in slots), "provider_dispatch_priority_duplicate"),
        ):
            materialized = tuple(values)
            if len(materialized) != len(set(materialized)):
                raise ProviderDispatchError(code)
        object.__setattr__(self, "model", model)
        object.__setattr__(self, "state", state)
        object.__setattr__(self, "source_hash", hashes[0])
        object.__setattr__(self, "cost_input_hash", hashes[1])
        object.__setattr__(self, "cost_output_hash", hashes[2])
        object.__setattr__(self, "ready_slots", slots)

    def document(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "state": self.state,
            "model": self.model,
            "routing_generation": self.routing_generation,
            "cost_generation": self.cost_generation,
            "source_hash": self.source_hash,
            "cost_input_hash": self.cost_input_hash,
            "cost_output_hash": self.cost_output_hash,
            "ready_dispatch_set": [slot.document() for slot in self.ready_slots],
        }

    def payload(self) -> str:
        return json.dumps(
            self.document(),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )

    def digest(self) -> str:
        return hashlib.sha256(self.payload().encode()).hexdigest()

    @classmethod
    def tombstone(
        cls,
        model: str,
        *,
        routing_generation: int,
        cost_generation: int,
    ) -> ProviderDispatchBundle:
        return cls(
            model=model,
            routing_generation=routing_generation,
            cost_generation=cost_generation,
            source_hash="0" * 64,
            cost_input_hash="0" * 64,
            cost_output_hash="0" * 64,
            ready_slots=(),
            state="absent",
        )


@dataclass(frozen=True)
class ProviderDispatchPublishResult:
    key: str
    digest: str
    changed: bool
    routing_generation: int
    cost_generation: int
    state: str


class ProviderDispatchSource(Protocol):
    def load_active(self, model: str) -> ProviderDispatchBundle | None: ...


class ProviderDispatchSink(Protocol):
    def publish(self, bundle: ProviderDispatchBundle) -> ProviderDispatchPublishResult: ...

    def restore(
        self,
        bundle: ProviderDispatchBundle,
        *,
        expected_current_routing_generation: int,
        expected_current_cost_generation: int,
    ) -> ProviderDispatchPublishResult: ...


class PostgresProviderDispatchSource:
    """Load one complete active routing/cost/READY set from one DB snapshot."""

    def __init__(self, connection_factory: Callable[[], Any]) -> None:
        if not callable(connection_factory):
            raise ProviderDispatchError("provider_dispatch_connection_factory_invalid")
        self.connection_factory = connection_factory

    def load_active(self, model: str) -> ProviderDispatchBundle | None:
        model = str(model).strip()
        if not model:
            raise ProviderDispatchError("provider_dispatch_model_invalid")
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                cursor.execute(_ACTIVE_BUNDLE_SQL, (model,))
                active = _row(cursor)
                if active is None:
                    cursor.execute(_ACTIVATED_CURSOR_SQL, (model, model))
                    cursor_row = _row(cursor) or {}
                    connection.commit()
                    return ProviderDispatchBundle.tombstone(
                        model,
                        routing_generation=_nonnegative_int(
                            cursor_row.get("routing_generation"),
                            "provider_dispatch_generation_invalid",
                        ),
                        cost_generation=_nonnegative_int(
                            cursor_row.get("cost_generation"),
                            "provider_dispatch_generation_invalid",
                        ),
                    )
                routing_generation = _positive_int(
                    active.get("routing_generation"),
                    "provider_dispatch_generation_invalid",
                )
                cost_generation = _positive_int(
                    active.get("cost_generation"),
                    "provider_dispatch_generation_invalid",
                )
                desired_size = _positive_int(
                    active.get("desired_size"),
                    "provider_dispatch_desired_size_invalid",
                )
                cursor.execute(
                    _ACTIVE_SLOTS_SQL,
                    (model, routing_generation, cost_generation),
                )
                rows = _rows(cursor)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            _close(connection)

        if len(rows) != desired_size:
            raise ProviderDispatchError("provider_dispatch_slot_count_mismatch")
        slot_nos = tuple(
            _positive_int(row.get("slot_no"), "provider_dispatch_slot_invalid")
            for row in rows
        )
        if tuple(sorted(slot_nos)) != tuple(range(1, desired_size + 1)):
            raise ProviderDispatchError("provider_dispatch_slot_set_invalid")
        ready: list[ProviderDispatchSlot] = []
        for row in rows:
            state = str(row.get("slot_state") or "")
            if state == "VACANT":
                _validate_vacant_row(row)
                continue
            if state == "REPAIRING":
                # Held identities waiting for Provider auto-pool takeover are
                # never dispatchable through the fixed-Pin request plane.
                continue
            if state != "READY":
                raise ProviderDispatchError("provider_dispatch_slot_state_invalid")
            ready.append(_ready_slot(row))
        ready.sort(key=lambda slot: (slot.priority, slot.slot_no))
        return ProviderDispatchBundle(
            model=model,
            routing_generation=routing_generation,
            cost_generation=cost_generation,
            source_hash=str(active.get("source_hash") or ""),
            cost_input_hash=str(active.get("cost_input_hash") or ""),
            cost_output_hash=str(active.get("cost_output_hash") or ""),
            ready_slots=tuple(ready),
        )


class RedisProviderDispatchSink:
    """Atomically replace one model projection and publish an invalidation hint."""

    def __init__(
        self,
        client: Any,
        *,
        key_prefix: str = "a6:provider:dispatch:v1",
        notification_channel: str = "a6:provider:dispatch:changed:v1",
    ) -> None:
        if not callable(getattr(client, "eval", None)) or not callable(
            getattr(client, "get", None)
        ):
            raise ProviderDispatchError("provider_dispatch_redis_client_invalid")
        self.client = client
        self.key_prefix = _redis_name(
            key_prefix,
            "provider_dispatch_redis_key_prefix_invalid",
        )
        self.notification_channel = _redis_name(
            notification_channel,
            "provider_dispatch_redis_channel_invalid",
        )

    def key(self, model: str) -> str:
        model = str(model).strip()
        if not model or any(ord(character) < 0x20 for character in model):
            raise ProviderDispatchError("provider_dispatch_model_invalid")
        return f"{self.key_prefix}:{model}"

    def publish(self, bundle: ProviderDispatchBundle) -> ProviderDispatchPublishResult:
        if not isinstance(bundle, ProviderDispatchBundle):
            raise ProviderDispatchError("provider_dispatch_bundle_invalid")
        payload = bundle.payload()
        digest = bundle.digest()
        notification = json.dumps(
            {
                "schema_version": bundle.schema_version,
                "state": bundle.state,
                "model": bundle.model,
                "routing_generation": bundle.routing_generation,
                "cost_generation": bundle.cost_generation,
                "digest": digest,
            },
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        result = self.client.eval(
            _ATOMIC_PUBLISH_LUA,
            1,
            self.key(bundle.model),
            payload,
            bundle.schema_version,
            bundle.model,
            bundle.routing_generation,
            bundle.cost_generation,
            bundle.state,
            self.notification_channel,
            notification,
        )
        try:
            status = int(result)
        except (TypeError, ValueError):
            raise ProviderDispatchError("provider_dispatch_redis_result_invalid") from None
        errors = {
            -1: "provider_dispatch_redis_stale_generation",
        }
        if status in errors:
            raise ProviderDispatchError(errors[status])
        if status not in {0, 1}:
            raise ProviderDispatchError("provider_dispatch_redis_result_invalid")
        self._assert_exact_readback(bundle.model, payload)
        return ProviderDispatchPublishResult(
            key=self.key(bundle.model),
            digest=digest,
            changed=status == 1,
            routing_generation=bundle.routing_generation,
            cost_generation=bundle.cost_generation,
            state=bundle.state,
        )

    def restore(
        self,
        bundle: ProviderDispatchBundle,
        *,
        expected_current_routing_generation: int,
        expected_current_cost_generation: int,
    ) -> ProviderDispatchPublishResult:
        if not isinstance(bundle, ProviderDispatchBundle):
            raise ProviderDispatchError("provider_dispatch_bundle_invalid")
        expected = (
            expected_current_routing_generation,
            expected_current_cost_generation,
        )
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 1
            for value in expected
        ):
            raise ProviderDispatchError(
                "provider_dispatch_restore_expected_generation_invalid"
            )
        payload = bundle.payload()
        digest = bundle.digest()
        notification = json.dumps(
            {
                "schema_version": bundle.schema_version,
                "state": bundle.state,
                "model": bundle.model,
                "routing_generation": bundle.routing_generation,
                "cost_generation": bundle.cost_generation,
                "digest": digest,
                "restored_from_routing_generation": expected[0],
                "restored_from_cost_generation": expected[1],
            },
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        result = self.client.eval(
            _ATOMIC_RESTORE_LUA,
            1,
            self.key(bundle.model),
            payload,
            bundle.schema_version,
            bundle.model,
            expected[0],
            expected[1],
            self.notification_channel,
            notification,
        )
        try:
            status = int(result)
        except (TypeError, ValueError):
            raise ProviderDispatchError(
                "provider_dispatch_redis_restore_result_invalid"
            ) from None
        if status == -1:
            raise ProviderDispatchError(
                "provider_dispatch_redis_restore_cas_mismatch"
            )
        if status not in {0, 1}:
            raise ProviderDispatchError(
                "provider_dispatch_redis_restore_result_invalid"
            )
        self._assert_exact_readback(bundle.model, payload)
        return ProviderDispatchPublishResult(
            key=self.key(bundle.model),
            digest=digest,
            changed=status == 1,
            routing_generation=bundle.routing_generation,
            cost_generation=bundle.cost_generation,
            state=bundle.state,
        )

    def _assert_exact_readback(self, model: str, expected_payload: str) -> None:
        try:
            observed = self.client.get(self.key(model))
        except Exception as exc:
            raise ProviderDispatchError(
                "provider_dispatch_redis_readback_failed"
            ) from exc
        if isinstance(observed, bytes):
            try:
                observed = observed.decode("utf-8")
            except UnicodeDecodeError:
                raise ProviderDispatchError(
                    "provider_dispatch_redis_readback_mismatch"
                ) from None
        if not isinstance(observed, str) or observed != expected_payload:
            raise ProviderDispatchError(
                "provider_dispatch_redis_readback_mismatch"
            )


class ProviderDispatchProjector:
    """Compensating projector with an optional expected active-generation lease."""

    def __init__(self, source: ProviderDispatchSource, sink: ProviderDispatchSink) -> None:
        if not callable(getattr(source, "load_active", None)) or not callable(
            getattr(sink, "publish", None)
        ):
            raise ProviderDispatchError("provider_dispatch_projector_dependency_invalid")
        self.source = source
        self.sink = sink

    def reconcile(
        self,
        model: str,
        *,
        expected_routing_generation: int | None = None,
        expected_cost_generation: int | None = None,
    ) -> ProviderDispatchPublishResult:
        if (expected_routing_generation is None) != (expected_cost_generation is None):
            raise ProviderDispatchError("provider_dispatch_expected_generation_invalid")
        active = self.source.load_active(model)
        if active is None:
            raise ProviderDispatchError("provider_dispatch_active_bundle_missing")
        if expected_routing_generation is not None and (
            active.state != "active"
            or active.routing_generation != expected_routing_generation
            or active.cost_generation != expected_cost_generation
        ):
            raise ProviderDispatchError("provider_dispatch_active_generation_mismatch")
        return self.sink.publish(active)

    def restore(
        self,
        model: str,
        *,
        expected_current_routing_generation: int,
        expected_current_cost_generation: int,
    ) -> ProviderDispatchPublishResult:
        restore = getattr(self.sink, "restore", None)
        if not callable(restore):
            raise ProviderDispatchError("provider_dispatch_restore_unsupported")
        active = self.source.load_active(model)
        if active is None:
            raise ProviderDispatchError("provider_dispatch_active_bundle_missing")
        return restore(
            active,
            expected_current_routing_generation=expected_current_routing_generation,
            expected_current_cost_generation=expected_current_cost_generation,
        )


def _ready_slot(row: Mapping[str, Any]) -> ProviderDispatchSlot:
    confirmed_pin_binding_id = _positive_int(
        row.get("confirmed_pin_binding_id"),
        "provider_dispatch_ready_pin_invalid",
    )
    if (
        row.get("funding_eligible") is not True
        or row.get("desired_schedulable") is not True
        or row.get("observed_schedulable") is not True
        or row.get("pin_confirmed") is not True
        or row.get("probe_confirmed") is not True
        or str(row.get("liquidity_state") or "")
        not in {"FUNDED", "REFILLING", "TRANSFER_UNKNOWN"}
        or str(row.get("token_status") or "").strip().casefold()
        not in {"1", "active", "enabled"}
        or str(row.get("binding_state") or "").strip().casefold() != "bound"
        or _optional_int(row.get("current_pin_binding_id"))
        != confirmed_pin_binding_id
    ):
        raise ProviderDispatchError("provider_dispatch_ready_slot_unconfirmed")
    desired_cost = _positive_decimal(
        row.get("desired_slot_cost_multiplier"),
        "provider_dispatch_slot_cost_invalid",
    )
    observed_cost = _positive_decimal(
        row.get("observed_rate_multiplier"),
        "provider_dispatch_slot_cost_invalid",
    )
    if desired_cost != observed_cost:
        raise ProviderDispatchError("provider_dispatch_slot_cost_mismatch")
    return ProviderDispatchSlot(
        slot_no=_positive_int(row.get("slot_no"), "provider_dispatch_slot_identity_invalid"),
        account_id=_positive_int(
            row.get("account_id"),
            "provider_dispatch_slot_identity_invalid",
        ),
        token_ref_id=_positive_int(
            row.get("token_ref_id"),
            "provider_dispatch_slot_identity_invalid",
        ),
        external_token_id=_positive_int(
            row.get("external_token_id"),
            "provider_dispatch_slot_identity_invalid",
        ),
        channel_id=_positive_int(
            row.get("channel_id"),
            "provider_dispatch_slot_identity_invalid",
        ),
        rank=_positive_int(
            row.get("rank"),
            "provider_dispatch_slot_identity_invalid",
        ),
        priority=_positive_int(
            row.get("priority"),
            "provider_dispatch_slot_identity_invalid",
        ),
        cost_multiplier=desired_cost,
    )


def _validate_vacant_row(row: Mapping[str, Any]) -> None:
    populated = (
        row.get("account_id"),
        row.get("token_ref_id"),
        row.get("external_token_id"),
        row.get("channel_id"),
        row.get("rank"),
        row.get("priority"),
        row.get("desired_slot_cost_multiplier"),
        row.get("observed_rate_multiplier"),
        row.get("confirmed_pin_binding_id"),
        row.get("current_pin_binding_id"),
        row.get("binding_state"),
    )
    if (
        any(value is not None for value in populated)
        or str(row.get("liquidity_state") or "UNFUNDED") != "UNFUNDED"
        or any(
            row.get(name) is True
            for name in (
                "funding_eligible",
                "desired_schedulable",
                "observed_schedulable",
                "pin_confirmed",
                "probe_confirmed",
            )
        )
    ):
        raise ProviderDispatchError("provider_dispatch_vacant_slot_populated")


def _positive_int(value: object, code: str) -> int:
    if isinstance(value, bool):
        raise ProviderDispatchError(code)
    try:
        result = int(value)
    except (TypeError, ValueError):
        raise ProviderDispatchError(code) from None
    if result < 1:
        raise ProviderDispatchError(code)
    return result


def _nonnegative_int(value: object, code: str) -> int:
    if isinstance(value, bool):
        raise ProviderDispatchError(code)
    try:
        result = int(value or 0)
    except (TypeError, ValueError):
        raise ProviderDispatchError(code) from None
    if result < 0:
        raise ProviderDispatchError(code)
    return result


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    return _positive_int(value, "provider_dispatch_slot_identity_invalid")


def _positive_decimal(value: object, code: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except Exception:
        raise ProviderDispatchError(code) from None
    if not result.is_finite() or result <= 0:
        raise ProviderDispatchError(code)
    return result


def _sha256(value: object) -> bool:
    text = str(value)
    return len(text) == 64 and all(character in "0123456789abcdef" for character in text)


def _redis_name(value: object, code: str) -> str:
    text = str(value).strip()
    if not text or any(character.isspace() or ord(character) < 0x20 for character in text):
        raise ProviderDispatchError(code)
    return text


def _row(cursor: Any) -> dict[str, Any] | None:
    value = cursor.fetchone()
    if value is None:
        return None
    if isinstance(value, Mapping):
        return dict(value)
    names = [column.name for column in cursor.description or ()]
    return dict(zip(names, value, strict=False))


def _rows(cursor: Any) -> list[dict[str, Any]]:
    values = cursor.fetchall()
    if not values:
        return []
    if isinstance(values[0], Mapping):
        return [dict(value) for value in values]
    names = [column.name for column in cursor.description or ()]
    return [dict(zip(names, value, strict=False)) for value in values]


def _close(connection: Any) -> None:
    close = getattr(connection, "close", None)
    if callable(close):
        close()


_ACTIVE_BUNDLE_SQL = """
SELECT generation.model, generation.generation AS routing_generation,
       generation.cost_generation, generation.source_hash,
       generation.desired_size, cost.cost_input_hash, cost.cost_output_hash
FROM a6_trade.provider_pool_generations generation
JOIN a6_trade.provider_cost_generations cost
  ON cost.id = generation.cost_generation_id
 AND cost.model = generation.model
 AND cost.cost_generation = generation.cost_generation
WHERE generation.model = %s
  AND generation.generation_state = 'active'
  AND cost.pricing_state = 'active'
"""


_ACTIVATED_CURSOR_SQL = """
SELECT coalesce(
           max(generation) FILTER (WHERE activated_at IS NOT NULL),
           0
       ) AS routing_generation,
       (
           SELECT coalesce(
               max(cost_generation) FILTER (WHERE activated_at IS NOT NULL),
               0
           )
           FROM a6_trade.provider_cost_generations
           WHERE model = %s
       ) AS cost_generation
FROM a6_trade.provider_pool_generations
WHERE model = %s
"""


_ACTIVE_SLOTS_SQL = """
SELECT slot.slot_no, slot.slot_state,
       slot.enodot_account_id AS account_id,
       slot.a6_token_id AS token_ref_id,
       token.a6_token_id AS external_token_id,
       slot.desired_channel_id AS channel_id,
       slot.desired_rank AS rank,
       slot.desired_priority AS priority,
       slot.desired_slot_cost_multiplier,
       slot.observed_rate_multiplier,
       slot.liquidity_state,
       slot.funding_eligible,
       slot.desired_schedulable,
       slot.observed_schedulable,
       slot.confirmed_pin_binding_id,
       slot.pin_confirmed,
       slot.probe_confirmed,
       token.status AS token_status,
       binding.state AS binding_state,
       pin.id AS current_pin_binding_id
FROM a6_trade.provider_pool_slots slot
LEFT JOIN a6_trade.a6_tokens token ON token.id = slot.a6_token_id
LEFT JOIN a6_trade.enodot_account_bindings binding
  ON binding.id = slot.enodot_binding_id
 AND binding.a6_token_id = slot.a6_token_id
 AND binding.enodot_account_id = slot.enodot_account_id
LEFT JOIN a6_trade.a6_pin_bindings pin
  ON pin.id = slot.confirmed_pin_binding_id
 AND pin.model = slot.model
 AND pin.a6_token_id = slot.a6_token_id
 AND pin.channel_id = slot.desired_channel_id
 AND pin.valid_to IS NULL
 AND pin.confirmed_at IS NOT NULL
WHERE slot.model = %s
  AND slot.routing_generation = %s
  AND slot.cost_generation = %s
  AND slot.valid_to IS NULL
ORDER BY slot.slot_no
"""


_ATOMIC_PUBLISH_LUA = """
local current = redis.call('GET', KEYS[1])
if current == ARGV[1] then
    return 0
end
if current then
    local ok, decoded = pcall(cjson.decode, current)
    if ok
       and tonumber(decoded.schema_version) == tonumber(ARGV[2])
       and tostring(decoded.model) == ARGV[3]
       and tonumber(decoded.routing_generation) ~= nil
       and tonumber(decoded.cost_generation) ~= nil then
        local current_routing = tonumber(decoded.routing_generation)
        local current_cost = tonumber(decoded.cost_generation)
        local desired_routing = tonumber(ARGV[4])
        local desired_cost = tonumber(ARGV[5])
        local current_state = tostring(decoded.state)
        local desired_state = ARGV[6]
        if current_routing == desired_routing and current_cost == desired_cost then
            if current_state == 'absent' and desired_state ~= 'absent' then
                return -1
            end
        elseif current_routing >= desired_routing or current_cost >= desired_cost then
            return -1
        end
    end
end
redis.call('SET', KEYS[1], ARGV[1])
redis.call('PUBLISH', ARGV[7], ARGV[8])
return 1
"""


_ATOMIC_RESTORE_LUA = """
local current = redis.call('GET', KEYS[1])
if current == ARGV[1] then
    return 0
end
if current then
    local ok, decoded = pcall(cjson.decode, current)
    if not ok
       or tonumber(decoded.schema_version) ~= tonumber(ARGV[2])
       or tostring(decoded.model) ~= ARGV[3]
       or tonumber(decoded.routing_generation) ~= tonumber(ARGV[4])
       or tonumber(decoded.cost_generation) ~= tonumber(ARGV[5]) then
        return -1
    end
end
redis.call('SET', KEYS[1], ARGV[1])
redis.call('PUBLISH', ARGV[6], ARGV[7])
return 1
"""
