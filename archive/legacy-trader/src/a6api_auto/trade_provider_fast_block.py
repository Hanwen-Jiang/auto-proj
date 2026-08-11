"""Redis projection for PostgreSQL-authoritative Provider fast blocks."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


class ProviderFastBlockError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class ProviderFastBlockProjection:
    event_id: int
    operation: str
    model: str
    routing_generation: int
    cost_generation: int
    slot_no: int
    channel_id: int
    projection_key: str
    payload: Mapping[str, Any]

    def __post_init__(self) -> None:
        identities = (
            self.event_id,
            self.routing_generation,
            self.cost_generation,
            self.slot_no,
            self.channel_id,
        )
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 1
            for value in identities
        ):
            raise ProviderFastBlockError("provider_fast_block_identity_invalid")
        if self.operation not in {"block", "unblock"}:
            raise ProviderFastBlockError("provider_fast_block_operation_invalid")
        if not _safe_text(self.model) or not _safe_text(self.projection_key):
            raise ProviderFastBlockError("provider_fast_block_key_invalid")
        if not isinstance(self.payload, Mapping):
            raise ProviderFastBlockError("provider_fast_block_payload_invalid")

    def document(self) -> dict[str, Any]:
        document = dict(self.payload)
        document.update(
            {
                "schema_version": 1,
                "state": "blocked" if self.operation == "block" else "unblocked",
                "event_id": self.event_id,
                "model": self.model,
                "routing_generation": self.routing_generation,
                "cost_generation": self.cost_generation,
                "slot_no": self.slot_no,
                "channel_id": self.channel_id,
            }
        )
        return document


@dataclass(frozen=True)
class ProviderFastBlockProjectionResult:
    event_id: int
    projection_key: str
    state: str
    changed: bool


class RedisProviderFastBlockProjector:
    """Apply ordered block/unblock events and publish invalidation hints."""

    def __init__(
        self,
        client: Any,
        *,
        key_prefix: str = "a6:provider:fast-block:v1",
        notification_channel: str = "a6:provider:fast-block:changed:v1",
        block_ttl_seconds: int = 7 * 24 * 60 * 60,
        cursor_ttl_seconds: int = 30 * 24 * 60 * 60,
    ) -> None:
        if not callable(getattr(client, "eval", None)):
            raise ProviderFastBlockError("provider_fast_block_redis_client_invalid")
        if not _safe_text(key_prefix) or not _safe_text(notification_channel):
            raise ProviderFastBlockError("provider_fast_block_redis_name_invalid")
        durations = (block_ttl_seconds, cursor_ttl_seconds)
        if any(isinstance(value, bool) or not 60 <= value <= 31_536_000 for value in durations):
            raise ProviderFastBlockError("provider_fast_block_ttl_invalid")
        if cursor_ttl_seconds < block_ttl_seconds:
            raise ProviderFastBlockError("provider_fast_block_cursor_ttl_invalid")
        self.client = client
        self.key_prefix = key_prefix
        self.notification_channel = notification_channel
        self.block_ttl_seconds = int(block_ttl_seconds)
        self.cursor_ttl_seconds = int(cursor_ttl_seconds)

    def key(self, model: str, routing_generation: int, slot_no: int, channel_id: int) -> str:
        if not _safe_text(model):
            raise ProviderFastBlockError("provider_fast_block_model_invalid")
        values = (routing_generation, slot_no, channel_id)
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 1 for value in values
        ):
            raise ProviderFastBlockError("provider_fast_block_identity_invalid")
        return f"{self.key_prefix}:{model}:{routing_generation}:{slot_no}:{channel_id}"

    def apply(self, projection: ProviderFastBlockProjection) -> ProviderFastBlockProjectionResult:
        return self._apply(projection, rebuild=False)

    def rebuild(
        self, projection: ProviderFastBlockProjection
    ) -> ProviderFastBlockProjectionResult:
        """Restore one PostgreSQL-selected latest event after Redis state loss.

        The caller must select only the newest durable event for a projection key.
        The rebuild script repairs a missing or malformed cursor, while a valid newer
        cursor still wins so a concurrent block/unblock cannot be rolled back.
        """

        return self._apply(projection, rebuild=True)

    def _apply(
        self,
        projection: ProviderFastBlockProjection,
        *,
        rebuild: bool,
    ) -> ProviderFastBlockProjectionResult:
        if not isinstance(projection, ProviderFastBlockProjection):
            raise ProviderFastBlockError("provider_fast_block_projection_invalid")
        expected_key = self.key(
            projection.model,
            projection.routing_generation,
            projection.slot_no,
            projection.channel_id,
        )
        if projection.projection_key != expected_key:
            raise ProviderFastBlockError("provider_fast_block_projection_key_mismatch")
        payload = json.dumps(
            projection.document(),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        notification = json.dumps(
            {
                "schema_version": 1,
                "event_id": projection.event_id,
                "operation": projection.operation,
                "model": projection.model,
                "routing_generation": projection.routing_generation,
                "slot_no": projection.slot_no,
                "channel_id": projection.channel_id,
            },
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        result = self.client.eval(
            _REBUILD_FAST_BLOCK_LUA if rebuild else _APPLY_FAST_BLOCK_LUA,
            2,
            expected_key,
            f"{expected_key}:cursor",
            projection.event_id,
            projection.operation,
            payload,
            self.block_ttl_seconds,
            self.cursor_ttl_seconds,
            self.notification_channel,
            notification,
        )
        try:
            status = int(result)
        except (TypeError, ValueError):
            raise ProviderFastBlockError("provider_fast_block_redis_result_invalid") from None
        if status not in {-1, 0, 1}:
            raise ProviderFastBlockError("provider_fast_block_redis_result_invalid")
        return ProviderFastBlockProjectionResult(
            event_id=projection.event_id,
            projection_key=expected_key,
            state="superseded" if status == -1 else "projected",
            changed=status == 1,
        )


def _safe_text(value: object) -> bool:
    text = str(value or "").strip()
    return bool(text) and len(text) <= 512 and all(ord(character) >= 0x20 for character in text)


_APPLY_FAST_BLOCK_LUA = r"""
local event_id = tonumber(ARGV[1])
local operation = ARGV[2]
local payload = ARGV[3]
local block_ttl = tonumber(ARGV[4])
local cursor_ttl = tonumber(ARGV[5])
local channel = ARGV[6]
local notification = ARGV[7]
if event_id == nil or (operation ~= 'block' and operation ~= 'unblock') then
    return -2
end

local current_raw = redis.call('GET', KEYS[2])
local current_id = 0
if current_raw then
    current_id = tonumber(current_raw)
    if current_id == nil then
        return -2
    end
end
if current_id > event_id then
    return -1
end

local changed = 0
if operation == 'block' then
    if redis.call('GET', KEYS[1]) ~= payload then
        redis.call('SET', KEYS[1], payload, 'EX', block_ttl)
        changed = 1
    elseif redis.call('TTL', KEYS[1]) < math.floor(block_ttl / 2) then
        redis.call('EXPIRE', KEYS[1], block_ttl)
    end
else
    if redis.call('DEL', KEYS[1]) > 0 then
        changed = 1
    end
end

if current_id < event_id then
    redis.call('SET', KEYS[2], event_id, 'EX', cursor_ttl)
elseif redis.call('TTL', KEYS[2]) < math.floor(cursor_ttl / 2) then
    redis.call('EXPIRE', KEYS[2], cursor_ttl)
end
if changed == 1 then
    redis.call('PUBLISH', channel, notification)
end
return changed
"""


_REBUILD_FAST_BLOCK_LUA = r"""
local event_id = tonumber(ARGV[1])
local operation = ARGV[2]
local payload = ARGV[3]
local block_ttl = tonumber(ARGV[4])
local cursor_ttl = tonumber(ARGV[5])
local channel = ARGV[6]
local notification = ARGV[7]
if event_id == nil or (operation ~= 'block' and operation ~= 'unblock') then
    return -2
end

local current_raw = redis.call('GET', KEYS[2])
local current_id = nil
if current_raw then
    current_id = tonumber(current_raw)
end
if current_id ~= nil and current_id > event_id then
    return -1
end

local changed = 0
if operation == 'block' then
    if redis.call('GET', KEYS[1]) ~= payload then
        redis.call('SET', KEYS[1], payload, 'EX', block_ttl)
        changed = 1
    elseif redis.call('TTL', KEYS[1]) < math.floor(block_ttl / 2) then
        redis.call('EXPIRE', KEYS[1], block_ttl)
    end
else
    if redis.call('DEL', KEYS[1]) > 0 then
        changed = 1
    end
end

redis.call('SET', KEYS[2], event_id, 'EX', cursor_ttl)
if changed == 1 then
    redis.call('PUBLISH', channel, notification)
end
return changed
"""
