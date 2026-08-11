"""Regional Provider Redis projection with PostgreSQL-durable receipts.

Redis Pub/Sub messages emitted here are invalidation hints only. A projection is
recorded as ready only after its exact Redis keys and the live ``INFO SERVER``
``run_id`` have been read back and matched against the PostgreSQL snapshot.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

from .trade_provider_dispatch import (
    ProviderDispatchBundle,
    ProviderDispatchPublishResult,
    ProviderDispatchSink,
    ProviderDispatchSource,
)
from .trade_provider_fast_block import (
    ProviderFastBlockProjection,
    RedisProviderFastBlockProjector,
)
from .trade_provider_region import (
    ProviderRegionCapacityReceipt,
    ProviderRegionCapacityReceiptWriteResult,
    ProviderRegionProjectionReceipt,
    ProviderRegionProjectionWriteResult,
)


class ProviderRegionalRuntimeError(RuntimeError):
    """Stable, secret-free failure from the regional projection runtime."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


_CAPACITY_NOTIFICATION_LEGACY = re.compile(
    r"[1-9][0-9]*:[0-9]+(?::(?:ready|resyncing|draining|disabled))?"
)
_CAPACITY_DISPATCH_OWNER_ID = re.compile(
    r"(?:us|hk):[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
    r"[0-9a-f]{4}-[0-9a-f]{12}"
)


@dataclass(frozen=True)
class ProviderFastBlockManifest:
    model: str
    routing_generation: int
    cost_generation: int
    source_hash: str
    last_event_id: int
    entries: tuple[ProviderFastBlockProjection, ...]
    schema_version: int = 1

    def __post_init__(self) -> None:
        model = _model(self.model)
        routing = _positive_int(
            self.routing_generation,
            "provider_regional_fast_block_generation_invalid",
        )
        cost = _positive_int(
            self.cost_generation,
            "provider_regional_fast_block_generation_invalid",
        )
        source_hash = _sha256(
            self.source_hash,
            "provider_regional_fast_block_source_hash_invalid",
        )
        last_event_id = _nonnegative_int(
            self.last_event_id,
            "provider_regional_fast_block_cursor_invalid",
        )
        entries = tuple(self.entries)
        if self.schema_version != 1:
            raise ProviderRegionalRuntimeError("provider_regional_fast_block_schema_invalid")
        if any(
            not isinstance(entry, ProviderFastBlockProjection)
            or entry.model != model
            or entry.routing_generation != routing
            or entry.cost_generation != cost
            or entry.event_id > last_event_id
            for entry in entries
        ):
            raise ProviderRegionalRuntimeError("provider_regional_fast_block_entry_invalid")
        ordered = tuple(sorted(entries, key=lambda entry: entry.projection_key))
        if entries != ordered or len({entry.projection_key for entry in entries}) != len(entries):
            raise ProviderRegionalRuntimeError("provider_regional_fast_block_entry_order_invalid")
        object.__setattr__(self, "model", model)
        object.__setattr__(self, "routing_generation", routing)
        object.__setattr__(self, "cost_generation", cost)
        object.__setattr__(self, "source_hash", source_hash)
        object.__setattr__(self, "last_event_id", last_event_id)
        object.__setattr__(self, "entries", entries)

    def document(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "model": self.model,
            "routing_generation": self.routing_generation,
            "cost_generation": self.cost_generation,
            "source_hash": self.source_hash,
            "last_event_id": self.last_event_id,
            "events": [
                {
                    "event_id": entry.event_id,
                    "operation": entry.operation,
                    "projection_key": entry.projection_key,
                    "payload_sha256": hashlib.sha256(
                        _canonical_json(entry.document()).encode()
                    ).hexdigest(),
                }
                for entry in self.entries
            ],
        }

    def payload(self) -> str:
        return _canonical_json(self.document())

    def digest(self) -> str:
        """SHA-256 of ``payload``; Go must hash these exact UTF-8 bytes."""

        return hashlib.sha256(self.payload().encode()).hexdigest()


@dataclass(frozen=True)
class ProviderRegionalCapacityAllocation:
    allocation_id: int
    enodot_binding_id: int
    enodot_account_id: int
    region: str
    quota: int
    account_concurrency: int
    allocation_version: int
    allocation_digest: str
    source_hash: str
    allocation_state: str
    drain_target_quota: int | None
    effective_at: datetime

    def __post_init__(self) -> None:
        for value in (
            self.allocation_id,
            self.enodot_binding_id,
            self.enodot_account_id,
            self.allocation_version,
        ):
            _positive_int(value, "provider_regional_capacity_identity_invalid")
        region = _region(self.region)
        quota = _nonnegative_int(
            self.quota,
            "provider_regional_capacity_quota_invalid",
        )
        concurrency = _nonnegative_int(
            self.account_concurrency,
            "provider_regional_capacity_concurrency_invalid",
        )
        if quota > concurrency:
            raise ProviderRegionalRuntimeError("provider_regional_capacity_quota_invalid")
        state = str(self.allocation_state).strip().casefold()
        drain_target = self.drain_target_quota
        if state == "active":
            if quota < 1 or drain_target is not None:
                raise ProviderRegionalRuntimeError("provider_regional_capacity_state_invalid")
        elif state == "disabled":
            if quota != 0 or drain_target is not None:
                raise ProviderRegionalRuntimeError("provider_regional_capacity_state_invalid")
        elif state == "draining":
            drain_target = _nonnegative_int(
                drain_target,
                "provider_regional_capacity_drain_target_invalid",
            )
            if quota < 1 or drain_target >= quota:
                raise ProviderRegionalRuntimeError(
                    "provider_regional_capacity_drain_target_invalid"
                )
        else:
            raise ProviderRegionalRuntimeError("provider_regional_capacity_state_invalid")
        effective_at = _aware(
            self.effective_at,
            "provider_regional_capacity_effective_at_invalid",
        )
        object.__setattr__(self, "region", region)
        object.__setattr__(self, "quota", quota)
        object.__setattr__(self, "account_concurrency", concurrency)
        object.__setattr__(self, "allocation_state", state)
        object.__setattr__(self, "drain_target_quota", drain_target)
        object.__setattr__(
            self,
            "allocation_digest",
            _sha256(
                self.allocation_digest,
                "provider_regional_capacity_allocation_digest_invalid",
            ),
        )
        object.__setattr__(
            self,
            "source_hash",
            _sha256(
                self.source_hash,
                "provider_regional_capacity_source_hash_invalid",
            ),
        )
        object.__setattr__(self, "effective_at", effective_at)


@dataclass(frozen=True)
class ProviderRegionalCapacityState:
    allocation: ProviderRegionalCapacityAllocation
    receipt: Mapping[str, Any] | None


@dataclass(frozen=True)
class ProviderCapacityRedisSnapshot:
    redis_restart_epoch: str
    lease_cursor: int
    occupancy: int
    occupancy_digest: str
    leases: tuple[tuple[str, str], ...]
    identities: tuple[tuple[str, str], ...]
    epoch_changed: bool
    restart_reconciled: bool


@dataclass(frozen=True)
class ProviderCapacityRedisProjection:
    snapshot: ProviderCapacityRedisSnapshot
    state: str
    receipt_version: int
    receipt_dirty: bool


class ProviderRegionalSource(Protocol):
    def load_projection_receipt(
        self,
        region: str,
        model: str,
        projection_type: str,
    ) -> Mapping[str, Any] | None: ...

    def load_fast_block_manifest(
        self,
        bundle: ProviderDispatchBundle,
    ) -> ProviderFastBlockManifest: ...

    def load_capacity_states(
        self,
        region: str,
    ) -> tuple[ProviderRegionalCapacityState, ...]: ...

    def load_capacity_state(
        self,
        region: str,
        account_id: int,
    ) -> ProviderRegionalCapacityState | None: ...


class ProviderProjectionReceiptStore(Protocol):
    def record_projection_receipt(
        self,
        receipt: ProviderRegionProjectionReceipt,
        *,
        expected_receipt_version: int,
    ) -> ProviderRegionProjectionWriteResult: ...


class ProviderCapacityReceiptStore(Protocol):
    def record_capacity_receipt(
        self,
        receipt: ProviderRegionCapacityReceipt,
        *,
        expected_receipt_version: int,
    ) -> ProviderRegionCapacityReceiptWriteResult: ...


class PostgresProviderRegionalSource:
    """Read regional canonical state through short repeatable-read snapshots."""

    def __init__(self, connection_factory: Callable[[], Any], *, max_events: int = 10_000):
        if not callable(connection_factory):
            raise ProviderRegionalRuntimeError("provider_regional_connection_factory_invalid")
        if isinstance(max_events, bool) or not 1 <= max_events <= 100_000:
            raise ProviderRegionalRuntimeError("provider_regional_event_limit_invalid")
        self.connection_factory = connection_factory
        self.max_events = max_events

    def load_projection_receipt(
        self,
        region: str,
        model: str,
        projection_type: str,
    ) -> Mapping[str, Any] | None:
        region = _region(region)
        model = _model(model)
        projection_type = str(projection_type).strip().casefold()
        if projection_type not in {"dispatch", "fast_block"}:
            raise ProviderRegionalRuntimeError("provider_regional_projection_type_invalid")
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                cursor.execute(
                    _PROJECTION_RECEIPT_SQL,
                    (region, model, projection_type),
                )
                row = _row(cursor)
            connection.commit()
            return row
        except Exception:
            _rollback(connection)
            raise
        finally:
            _close(connection)

    def load_fast_block_manifest(
        self,
        bundle: ProviderDispatchBundle,
    ) -> ProviderFastBlockManifest:
        if not isinstance(bundle, ProviderDispatchBundle) or bundle.state != "active":
            raise ProviderRegionalRuntimeError(
                "provider_regional_fast_block_active_bundle_required"
            )
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                cursor.execute(_FAST_BLOCK_HWM_SQL, (bundle.model,))
                hwm = _row(cursor) or {}
                cursor.execute(
                    _FAST_BLOCK_LATEST_SQL,
                    (
                        bundle.model,
                        bundle.routing_generation,
                        bundle.cost_generation,
                        self.max_events + 1,
                    ),
                )
                rows = _rows(cursor)
            connection.commit()
        except Exception:
            _rollback(connection)
            raise
        finally:
            _close(connection)
        if len(rows) > self.max_events:
            raise ProviderRegionalRuntimeError("provider_regional_fast_block_event_limit_exceeded")
        entries = tuple(
            sorted(
                (_fast_block_projection(row) for row in rows),
                key=lambda item: item.projection_key,
            )
        )
        return ProviderFastBlockManifest(
            model=bundle.model,
            routing_generation=bundle.routing_generation,
            cost_generation=bundle.cost_generation,
            source_hash=bundle.source_hash,
            last_event_id=_nonnegative_int(
                hwm.get("last_event_id"),
                "provider_regional_fast_block_cursor_invalid",
            ),
            entries=entries,
        )

    def load_capacity_states(
        self,
        region: str,
    ) -> tuple[ProviderRegionalCapacityState, ...]:
        region = _region(region)
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                cursor.execute(_CAPACITY_STATES_SQL, (region,))
                rows = _rows(cursor)
            connection.commit()
        except Exception:
            _rollback(connection)
            raise
        finally:
            _close(connection)
        return tuple(_capacity_state(row) for row in rows)

    def load_capacity_state(
        self,
        region: str,
        account_id: int,
    ) -> ProviderRegionalCapacityState | None:
        region = _region(region)
        account_id = _positive_int(
            account_id,
            "provider_regional_capacity_account_invalid",
        )
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                cursor.execute(_CAPACITY_STATE_SQL, (region, account_id))
                rows = _rows(cursor)
            connection.commit()
        except Exception:
            _rollback(connection)
            raise
        finally:
            _close(connection)
        if len(rows) > 1:
            raise ProviderRegionalRuntimeError("provider_regional_capacity_state_duplicate")
        return None if not rows else _capacity_state(rows[0])


def load_redis_restart_epoch(client: Any) -> str:
    """Read the live Redis/Valkey restart epoch from ``INFO SERVER run_id``."""

    info = client.info("server")
    if not isinstance(info, Mapping):
        raise ProviderRegionalRuntimeError("provider_regional_redis_info_invalid")
    raw = info.get("run_id")
    if raw is None:
        raw = info.get(b"run_id")
    epoch = _text(raw)
    if (
        not epoch
        or len(epoch) > 128
        or any(character.isspace() or ord(character) < 0x21 for character in epoch)
    ):
        raise ProviderRegionalRuntimeError("provider_regional_redis_epoch_invalid")
    return epoch


class RedisProviderRegionalDispatchProjector:
    """Publish the existing dispatch document plus a regional exact-read meta key."""

    def __init__(self, client: Any, sink: ProviderDispatchSink, *, key_prefix: str):
        if not callable(getattr(client, "get", None)) or not callable(getattr(client, "set", None)):
            raise ProviderRegionalRuntimeError("provider_regional_dispatch_redis_client_invalid")
        if not callable(getattr(sink, "publish", None)) or not callable(getattr(sink, "key", None)):
            raise ProviderRegionalRuntimeError("provider_regional_dispatch_sink_invalid")
        self.client = client
        self.sink = sink
        self.key_prefix = _redis_name(key_prefix)

    def meta_key(self, model: str) -> str:
        return f"{self.key_prefix}:{_model(model)}:meta"

    def publish(
        self,
        bundle: ProviderDispatchBundle,
        *,
        region: str,
        redis_restart_epoch: str,
    ) -> ProviderDispatchPublishResult:
        result = self.sink.publish(bundle)
        meta = self._meta(bundle, region=region, redis_restart_epoch=redis_restart_epoch)
        if self.client.set(self.meta_key(bundle.model), meta) in {False, None}:
            raise ProviderRegionalRuntimeError("provider_regional_dispatch_meta_write_failed")
        self.verify(bundle, region=region, redis_restart_epoch=redis_restart_epoch)
        return result

    def verify(
        self,
        bundle: ProviderDispatchBundle,
        *,
        region: str,
        redis_restart_epoch: str,
    ) -> None:
        payload = _text(self.client.get(self.sink.key(bundle.model)))
        meta = _text(self.client.get(self.meta_key(bundle.model)))
        if payload != bundle.payload() or meta != self._meta(
            bundle,
            region=region,
            redis_restart_epoch=redis_restart_epoch,
        ):
            raise ProviderRegionalRuntimeError("provider_regional_dispatch_exact_read_mismatch")

    def _meta(
        self,
        bundle: ProviderDispatchBundle,
        *,
        region: str,
        redis_restart_epoch: str,
    ) -> str:
        return _canonical_json(
            {
                "schema_version": 1,
                "projection_type": "dispatch",
                "projection_state": "ready",
                "region": _region(region),
                "model": bundle.model,
                "state": bundle.state,
                "routing_generation": bundle.routing_generation,
                "cost_generation": bundle.cost_generation,
                "source_hash": bundle.source_hash,
                "projection_digest": bundle.digest(),
                "redis_restart_epoch": _epoch(redis_restart_epoch),
            }
        )


class RedisProviderRegionalFastBlockProjector:
    """Rebuild exact fast-block keys and an indexed model manifest."""

    def __init__(
        self,
        client: Any,
        projector: RedisProviderFastBlockProjector,
        *,
        key_prefix: str = "a6:provider:fast-block:v1",
        notification_channel: str = "a6:provider:fast-block:changed:v1",
    ) -> None:
        required = ("eval", "get", "smembers")
        if any(not callable(getattr(client, name, None)) for name in required):
            raise ProviderRegionalRuntimeError("provider_regional_fast_block_redis_client_invalid")
        if not callable(getattr(projector, "rebuild", None)):
            raise ProviderRegionalRuntimeError("provider_regional_fast_block_projector_invalid")
        self.client = client
        self.projector = projector
        self.key_prefix = _redis_name(key_prefix)
        self.notification_channel = _redis_name(notification_channel)

    def index_key(self, model: str) -> str:
        return f"{self.key_prefix}:{_model(model)}:keys"

    def manifest_key(self, model: str) -> str:
        return f"{self.key_prefix}:{_model(model)}:manifest"

    def meta_key(self, model: str) -> str:
        return f"{self.key_prefix}:{_model(model)}:meta"

    def publish(
        self,
        manifest: ProviderFastBlockManifest,
        *,
        region: str,
        redis_restart_epoch: str,
    ) -> None:
        indexed = {_text(value) for value in self.client.smembers(self.index_key(manifest.model))}
        if any(not self._projection_key(manifest.model, value) for value in indexed):
            raise ProviderRegionalRuntimeError("provider_regional_fast_block_index_corrupt")
        for entry in manifest.entries:
            result = self.projector.rebuild(entry)
            if result.state != "projected":
                raise ProviderRegionalRuntimeError(
                    "provider_regional_fast_block_concurrent_supersede"
                )
        desired_keys = [entry.projection_key for entry in manifest.entries]
        meta = self._meta(
            manifest,
            region=region,
            redis_restart_epoch=redis_restart_epoch,
        )
        notice = _canonical_json(
            {
                "schema_version": 1,
                "hint": "exact_reread",
                "projection_type": "fast_block",
                "model": manifest.model,
                "routing_generation": manifest.routing_generation,
                "cost_generation": manifest.cost_generation,
                "last_event_id": manifest.last_event_id,
                "digest": manifest.digest(),
            }
        )
        raw = self.client.eval(
            _FINALIZE_FAST_BLOCK_MANIFEST_LUA,
            3,
            self.index_key(manifest.model),
            self.manifest_key(manifest.model),
            self.meta_key(manifest.model),
            f"{self.key_prefix}:{manifest.model}:",
            _canonical_json(desired_keys),
            manifest.payload(),
            meta,
            self.notification_channel,
            notice,
        )
        if _script_int(raw, "provider_regional_fast_block_finalize_invalid") < 0:
            raise ProviderRegionalRuntimeError("provider_regional_fast_block_index_corrupt")
        self.verify(manifest, region=region, redis_restart_epoch=redis_restart_epoch)

    def verify(
        self,
        manifest: ProviderFastBlockManifest,
        *,
        region: str,
        redis_restart_epoch: str,
    ) -> None:
        if _text(self.client.get(self.manifest_key(manifest.model))) != manifest.payload():
            raise ProviderRegionalRuntimeError("provider_regional_fast_block_manifest_mismatch")
        if _text(self.client.get(self.meta_key(manifest.model))) != self._meta(
            manifest,
            region=region,
            redis_restart_epoch=redis_restart_epoch,
        ):
            raise ProviderRegionalRuntimeError("provider_regional_fast_block_meta_mismatch")
        indexed = {_text(value) for value in self.client.smembers(self.index_key(manifest.model))}
        expected = {entry.projection_key for entry in manifest.entries}
        if indexed != expected:
            raise ProviderRegionalRuntimeError("provider_regional_fast_block_index_mismatch")
        for entry in manifest.entries:
            cursor = _text(self.client.get(f"{entry.projection_key}:cursor"))
            payload = self.client.get(entry.projection_key)
            if cursor != str(entry.event_id):
                raise ProviderRegionalRuntimeError("provider_regional_fast_block_cursor_mismatch")
            expected_payload = (
                _canonical_json(entry.document()) if entry.operation == "block" else None
            )
            if (None if payload is None else _text(payload)) != expected_payload:
                raise ProviderRegionalRuntimeError(
                    "provider_regional_fast_block_exact_read_mismatch"
                )

    def _meta(
        self,
        manifest: ProviderFastBlockManifest,
        *,
        region: str,
        redis_restart_epoch: str,
    ) -> str:
        return _canonical_json(
            {
                "schema_version": 1,
                "projection_type": "fast_block",
                "projection_state": "ready",
                "region": _region(region),
                "model": manifest.model,
                "routing_generation": manifest.routing_generation,
                "cost_generation": manifest.cost_generation,
                "source_hash": manifest.source_hash,
                "projection_digest": manifest.digest(),
                "last_event_id": manifest.last_event_id,
                "redis_restart_epoch": _epoch(redis_restart_epoch),
            }
        )

    def _projection_key(self, model: str, value: str) -> bool:
        prefix = f"{self.key_prefix}:{model}:"
        if not value.startswith(prefix):
            return False
        parts = value[len(prefix) :].split(":")
        return len(parts) == 3 and all(part.isdigit() and int(part) > 0 for part in parts)


class RedisProviderRegionalCapacityProjector:
    """Project one regional allocation while retaining lease occupancy."""

    def __init__(
        self,
        client: Any,
        *,
        key_prefix: str = "a6:provider:capacity:v1",
        notification_channel: str = "a6:provider:capacity:changed:v1",
        lease_ttl_seconds: int = 15 * 60,
    ) -> None:
        required = ("eval", "hgetall", "zrange")
        if any(not callable(getattr(client, name, None)) for name in required):
            raise ProviderRegionalRuntimeError("provider_regional_capacity_redis_client_invalid")
        if isinstance(lease_ttl_seconds, bool) or not 60 <= lease_ttl_seconds <= 86_400:
            raise ProviderRegionalRuntimeError("provider_regional_capacity_lease_ttl_invalid")
        self.client = client
        self.key_prefix = _redis_name(key_prefix)
        self.notification_channel = _redis_name(notification_channel)
        self.lease_ttl_seconds = lease_ttl_seconds

    def keys(self, region: str, account_id: int) -> tuple[str, str]:
        region = _region(region)
        account_id = _positive_int(
            account_id,
            "provider_regional_capacity_account_invalid",
        )
        base = f"{self.key_prefix}:{{{region}:{account_id}}}"
        return f"{base}:leases", f"{base}:meta"

    def identities_key(self, region: str, account_id: int) -> str:
        leases_key, _meta_key = self.keys(region, account_id)
        return f"{leases_key}:identities"

    def read(
        self,
        allocation: ProviderRegionalCapacityAllocation,
        *,
        redis_restart_epoch: str,
    ) -> ProviderCapacityRedisProjection | None:
        leases_key, meta_key = self.keys(
            allocation.region,
            allocation.enodot_account_id,
        )
        identities_key = self.identities_key(
            allocation.region,
            allocation.enodot_account_id,
        )
        meta = {_text(key): _text(value) for key, value in self.client.hgetall(meta_key).items()}
        if not meta:
            return None
        expected = {
            "account_id": str(allocation.enodot_account_id),
            "region": allocation.region,
            "allocation_version": str(allocation.allocation_version),
            "quota": str(allocation.quota),
            "restart_epoch": _epoch(redis_restart_epoch),
            "allocation_state": allocation.allocation_state,
        }
        if any(meta.get(key) != value for key, value in expected.items()):
            return None
        state = meta.get("state", "")
        if state not in {"resyncing", "ready", "draining", "disabled"}:
            raise ProviderRegionalRuntimeError("provider_regional_capacity_metadata_corrupt")
        dirty = meta.get("receipt_dirty")
        if dirty not in {"0", "1"}:
            raise ProviderRegionalRuntimeError("provider_regional_capacity_metadata_corrupt")
        receipt_dirty = dirty == "1"
        cursor = _nonnegative_int(
            meta.get("lease_cursor"),
            "provider_regional_capacity_cursor_invalid",
        )
        leases = tuple(
            sorted(
                (
                    (_text(member), _canonical_score(score))
                    for member, score in self.client.zrange(
                        leases_key,
                        0,
                        -1,
                        withscores=True,
                    )
                ),
                key=lambda item: item[0],
            )
        )
        identities = {
            _text(key): _text(value)
            for key, value in self.client.hgetall(identities_key).items()
        }
        _validate_capacity_lease_identities(
            leases,
            identities,
            account_id=allocation.enodot_account_id,
            region=allocation.region,
            allocation_version=allocation.allocation_version,
        )
        ordered_identities = tuple(sorted(identities.items()))
        occupancy = len(leases)
        if occupancy > allocation.quota:
            raise ProviderRegionalRuntimeError("provider_regional_capacity_occupancy_invalid")
        digest = _capacity_digest(allocation, cursor=cursor, leases=leases)
        if state != "resyncing" and not receipt_dirty and (
            meta.get("occupancy") != str(occupancy)
            or meta.get("occupancy_digest") != digest
        ):
            raise ProviderRegionalRuntimeError("provider_regional_capacity_meta_mismatch")
        receipt_version = _nonnegative_int(
            meta.get("receipt_version", 0),
            "provider_regional_capacity_receipt_version_invalid",
        )
        return ProviderCapacityRedisProjection(
            snapshot=ProviderCapacityRedisSnapshot(
                redis_restart_epoch=_epoch(redis_restart_epoch),
                lease_cursor=cursor,
                occupancy=occupancy,
                occupancy_digest=digest,
                leases=leases,
                identities=ordered_identities,
                epoch_changed=False,
                restart_reconciled=(
                    meta.get("reconciled_restart_epoch") == redis_restart_epoch
                ),
            ),
            state=state,
            receipt_version=receipt_version,
            receipt_dirty=receipt_dirty,
        )

    def begin(
        self,
        allocation: ProviderRegionalCapacityAllocation,
        *,
        redis_restart_epoch: str,
    ) -> ProviderCapacityRedisSnapshot:
        leases_key, meta_key = self.keys(
            allocation.region,
            allocation.enodot_account_id,
        )
        identities_key = self.identities_key(
            allocation.region,
            allocation.enodot_account_id,
        )
        raw = self.client.eval(
            _BEGIN_CAPACITY_PROJECTION_LUA,
            3,
            leases_key,
            meta_key,
            identities_key,
            allocation.enodot_account_id,
            allocation.region,
            allocation.quota,
            allocation.allocation_version,
            _epoch(redis_restart_epoch),
            allocation.allocation_state,
            self.notification_channel,
        )
        if not isinstance(raw, (list, tuple)) or len(raw) < 4:
            raise ProviderRegionalRuntimeError("provider_regional_capacity_snapshot_invalid")
        epoch_changed = _script_int(
            raw[0],
            "provider_regional_capacity_snapshot_invalid",
        )
        if epoch_changed < 0:
            raise ProviderRegionalRuntimeError("provider_regional_capacity_metadata_corrupt")
        cursor = _nonnegative_int(
            raw[1],
            "provider_regional_capacity_cursor_invalid",
        )
        occupancy = _nonnegative_int(
            raw[2],
            "provider_regional_capacity_occupancy_invalid",
        )
        restart_reconciled = _script_int(
            raw[3],
            "provider_regional_capacity_snapshot_invalid",
        )
        if restart_reconciled not in {0, 1}:
            raise ProviderRegionalRuntimeError("provider_regional_capacity_snapshot_invalid")
        flat = tuple(raw[4:])
        if len(flat) != occupancy * 3 or occupancy > allocation.quota:
            raise ProviderRegionalRuntimeError("provider_regional_capacity_occupancy_invalid")
        leases = tuple(
            sorted(
                (
                    (_text(flat[index]), _canonical_score(flat[index + 1]))
                    for index in range(0, len(flat), 3)
                ),
                key=lambda item: item[0],
            )
        )
        identities = tuple(
            sorted(
                (
                    (_text(flat[index]), _text(flat[index + 2]))
                    for index in range(0, len(flat), 3)
                ),
                key=lambda item: item[0],
            )
        )
        _validate_capacity_lease_identities(
            leases,
            dict(identities),
            account_id=allocation.enodot_account_id,
            region=allocation.region,
            allocation_version=allocation.allocation_version,
        )
        digest = _capacity_digest(allocation, cursor=cursor, leases=leases)
        return ProviderCapacityRedisSnapshot(
            redis_restart_epoch=_epoch(redis_restart_epoch),
            lease_cursor=cursor,
            occupancy=occupancy,
            occupancy_digest=digest,
            leases=leases,
            identities=identities,
            epoch_changed=epoch_changed == 1,
            restart_reconciled=restart_reconciled == 1,
        )

    def seal(
        self,
        allocation: ProviderRegionalCapacityAllocation,
        snapshot: ProviderCapacityRedisSnapshot,
        *,
        receipt_version: int,
    ) -> None:
        leases_key, meta_key = self.keys(
            allocation.region,
            allocation.enodot_account_id,
        )
        identities_key = self.identities_key(
            allocation.region,
            allocation.enodot_account_id,
        )
        raw = self.client.eval(
            _SEAL_CAPACITY_PROJECTION_LUA,
            3,
            leases_key,
            meta_key,
            identities_key,
            allocation.allocation_version,
            allocation.quota,
            snapshot.redis_restart_epoch,
            snapshot.lease_cursor,
            snapshot.occupancy,
            snapshot.occupancy_digest,
            _positive_int(
                receipt_version,
                "provider_regional_capacity_receipt_version_invalid",
            ),
            self.notification_channel,
            _capacity_snapshot_payload(snapshot),
        )
        if _script_int(raw, "provider_regional_capacity_seal_invalid") != 1:
            raise ProviderRegionalRuntimeError("provider_regional_capacity_concurrent_change")
        self.verify(
            allocation,
            snapshot,
            receipt_version=receipt_version,
            final_state="resyncing",
        )

    def finalize(
        self,
        allocation: ProviderRegionalCapacityAllocation,
        snapshot: ProviderCapacityRedisSnapshot,
        *,
        receipt_version: int,
    ) -> None:
        leases_key, meta_key = self.keys(
            allocation.region,
            allocation.enodot_account_id,
        )
        identities_key = self.identities_key(
            allocation.region,
            allocation.enodot_account_id,
        )
        final_state = (
            "ready" if allocation.allocation_state == "active" else allocation.allocation_state
        )
        acknowledged_version = _positive_int(
            receipt_version,
            "provider_regional_capacity_receipt_version_invalid",
        )
        if acknowledged_version < 2:
            raise ProviderRegionalRuntimeError(
                "provider_regional_capacity_receipt_version_invalid"
            )
        if not snapshot.restart_reconciled:
            raise ProviderRegionalRuntimeError(
                "provider_regional_capacity_restart_reconcile_pending"
            )
        raw = self.client.eval(
            _FINALIZE_CAPACITY_PROJECTION_LUA,
            3,
            leases_key,
            meta_key,
            identities_key,
            allocation.allocation_version,
            allocation.quota,
            snapshot.redis_restart_epoch,
            snapshot.lease_cursor,
            snapshot.occupancy,
            snapshot.occupancy_digest,
            acknowledged_version - 1,
            acknowledged_version,
            final_state,
            self.notification_channel,
            _capacity_snapshot_payload(snapshot),
        )
        if _script_int(raw, "provider_regional_capacity_finalize_invalid") != 1:
            raise ProviderRegionalRuntimeError("provider_regional_capacity_concurrent_change")
        self.verify(
            allocation,
            snapshot,
            receipt_version=acknowledged_version,
            final_state=final_state,
        )

    def verify(
        self,
        allocation: ProviderRegionalCapacityAllocation,
        snapshot: ProviderCapacityRedisSnapshot,
        *,
        receipt_version: int,
        final_state: str,
    ) -> None:
        leases_key, meta_key = self.keys(
            allocation.region,
            allocation.enodot_account_id,
        )
        identities_key = self.identities_key(
            allocation.region,
            allocation.enodot_account_id,
        )
        meta = {_text(key): _text(value) for key, value in self.client.hgetall(meta_key).items()}
        expected = {
            "account_id": str(allocation.enodot_account_id),
            "region": allocation.region,
            "allocation_version": str(allocation.allocation_version),
            "quota": str(allocation.quota),
            "restart_epoch": snapshot.redis_restart_epoch,
            "allocation_state": allocation.allocation_state,
            "state": final_state,
            "lease_cursor": str(snapshot.lease_cursor),
            "occupancy": str(snapshot.occupancy),
            "occupancy_digest": snapshot.occupancy_digest,
            "receipt_version": str(receipt_version),
            "receipt_dirty": "0",
        }
        if any(meta.get(key) != value for key, value in expected.items()):
            raise ProviderRegionalRuntimeError("provider_regional_capacity_meta_mismatch")
        if (
            meta.get("reconciled_restart_epoch") == snapshot.redis_restart_epoch
        ) != snapshot.restart_reconciled:
            raise ProviderRegionalRuntimeError("provider_regional_capacity_meta_mismatch")
        leases = tuple(
            sorted(
                (
                    (_text(member), _canonical_score(score))
                    for member, score in self.client.zrange(
                        leases_key,
                        0,
                        -1,
                        withscores=True,
                    )
                ),
                key=lambda item: item[0],
            )
        )
        if leases != snapshot.leases:
            raise ProviderRegionalRuntimeError("provider_regional_capacity_lease_mismatch")
        identities = {
            _text(key): _text(value)
            for key, value in self.client.hgetall(identities_key).items()
        }
        _validate_capacity_lease_identities(
            leases,
            identities,
            account_id=allocation.enodot_account_id,
            region=allocation.region,
            allocation_version=allocation.allocation_version,
        )
        if tuple(sorted(identities.items())) != snapshot.identities:
            raise ProviderRegionalRuntimeError("provider_regional_capacity_identity_mismatch")


class ProviderRegionalProjectionRuntime:
    """Run one fail-closed regional dispatch, fast-block and capacity cycle."""

    def __init__(
        self,
        *,
        redis_client: Any,
        dispatch_source: ProviderDispatchSource,
        regional_source: ProviderRegionalSource,
        dispatch_projector: RedisProviderRegionalDispatchProjector,
        fast_block_projector: RedisProviderRegionalFastBlockProjector,
        capacity_projector: RedisProviderRegionalCapacityProjector,
        projection_store: ProviderProjectionReceiptStore,
        capacity_store: ProviderCapacityReceiptStore | None = None,
        clock: Callable[[], datetime] | None = None,
        restart_grace_seconds: int = 15 * 60,
    ) -> None:
        if isinstance(restart_grace_seconds, bool) or not 60 <= restart_grace_seconds <= 86_400:
            raise ProviderRegionalRuntimeError("provider_regional_restart_grace_invalid")
        self.redis_client = redis_client
        self.dispatch_source = dispatch_source
        self.regional_source = regional_source
        self.dispatch_projector = dispatch_projector
        self.fast_block_projector = fast_block_projector
        self.capacity_projector = capacity_projector
        self.projection_store = projection_store
        self.capacity_store = capacity_store
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.restart_grace_seconds = restart_grace_seconds

    def reconcile(self, model: str, *, region: str) -> dict[str, Any]:
        model = _model(model)
        region = _region(region)
        epoch = load_redis_restart_epoch(self.redis_client)
        bundle = self.dispatch_source.load_active(model)
        if bundle is None:
            raise ProviderRegionalRuntimeError("provider_regional_dispatch_projection_missing")

        dispatch = self._reconcile_dispatch(bundle, region=region, epoch=epoch)
        fast_block: dict[str, Any]
        if bundle.state == "active":
            manifest = self.regional_source.load_fast_block_manifest(bundle)
            fast_block = self._reconcile_fast_block(
                bundle,
                manifest,
                region=region,
                epoch=epoch,
            )
        else:
            fast_block = {"action": "not-applicable", "ready": True}

        capacities = []
        for state in self.regional_source.load_capacity_states(region):
            capacities.append(self._reconcile_capacity(state, epoch=epoch))
        return {
            "success": True,
            "region": region,
            "model": model,
            "redis_restart_epoch": epoch,
            "dispatch": dispatch,
            "fast_block": fast_block,
            "capacity": capacities,
            "ready": dispatch["ready"]
            and fast_block["ready"]
            and all(item["ready"] for item in capacities),
        }

    def reconcile_capacities(self, *, region: str) -> dict[str, Any]:
        if self.capacity_store is None:
            raise ProviderRegionalRuntimeError("provider_regional_capacity_store_missing")
        region = _region(region)
        epoch = load_redis_restart_epoch(self.redis_client)
        results = [
            self._reconcile_capacity(state, epoch=epoch)
            for state in self.regional_source.load_capacity_states(region)
        ]
        return {
            "success": True,
            "region": region,
            "redis_restart_epoch": epoch,
            "capacity": results,
            "ready": all(item["ready"] for item in results),
        }

    def reconcile_capacity(
        self,
        *,
        region: str,
        account_id: int,
    ) -> dict[str, Any]:
        if self.capacity_store is None:
            raise ProviderRegionalRuntimeError("provider_regional_capacity_store_missing")
        region = _region(region)
        account_id = _positive_int(
            account_id,
            "provider_regional_capacity_account_invalid",
        )
        epoch = load_redis_restart_epoch(self.redis_client)
        state = self.regional_source.load_capacity_state(region, account_id)
        if state is None:
            raise ProviderRegionalRuntimeError("provider_regional_capacity_allocation_missing")
        result = self._reconcile_capacity(state, epoch=epoch)
        return {
            "success": True,
            "region": region,
            "redis_restart_epoch": epoch,
            "capacity": [result],
            "ready": result["ready"],
        }

    def _reconcile_dispatch(
        self,
        bundle: ProviderDispatchBundle,
        *,
        region: str,
        epoch: str,
    ) -> dict[str, Any]:
        current = self.regional_source.load_projection_receipt(
            region,
            bundle.model,
            "dispatch",
        )
        digest = bundle.digest()
        if _projection_receipt_matches(
            current,
            bundle=bundle,
            projection_type="dispatch",
            projection_digest=digest,
            last_event_id=0,
            epoch=epoch,
        ):
            try:
                self.dispatch_projector.verify(
                    bundle,
                    region=region,
                    redis_restart_epoch=epoch,
                )
            except ProviderRegionalRuntimeError:
                return self._prepare_dispatch(bundle, current=current, region=region, epoch=epoch)
            if _projection_receipt_ready_complete(current):
                return {
                    "action": "verified",
                    "ready": True,
                    "routing_generation": bundle.routing_generation,
                    "cost_generation": bundle.cost_generation,
                    "digest": digest,
                    "receipt_version": _receipt_version(current),
                }
            if _projection_subscriber_ack_complete(current):
                return self._finalize_dispatch(bundle, current=current, region=region, epoch=epoch)
            return {
                "action": "awaiting-subscriber",
                "ready": False,
                "routing_generation": bundle.routing_generation,
                "cost_generation": bundle.cost_generation,
                "digest": digest,
                "receipt_version": _receipt_version(current),
            }
        return self._prepare_dispatch(bundle, current=current, region=region, epoch=epoch)

    def _prepare_dispatch(
        self,
        bundle: ProviderDispatchBundle,
        *,
        current: Mapping[str, Any] | None,
        region: str,
        epoch: str,
    ) -> dict[str, Any]:
        started_at = _next_time(self.clock(), current)
        initial = ProviderRegionProjectionReceipt(
            region=region,
            model=bundle.model,
            projection_type="dispatch",
            routing_generation=bundle.routing_generation,
            cost_generation=bundle.cost_generation,
            source_hash=bundle.source_hash,
            projection_digest=bundle.digest(),
            last_event_id=0,
            subscriber_cursor=0,
            subscriber_connected=False,
            redis_restart_epoch=epoch,
            projection_state=(
                "resyncing"
                if current is None or _text(current.get("redis_restart_epoch")) != epoch
                else "projecting"
            ),
            projected_at=started_at,
        )
        write = self.projection_store.record_projection_receipt(
            initial,
            expected_receipt_version=_receipt_version(current),
        )
        self.dispatch_projector.publish(
            bundle,
            region=region,
            redis_restart_epoch=epoch,
        )
        if load_redis_restart_epoch(self.redis_client) != epoch:
            raise ProviderRegionalRuntimeError("provider_regional_redis_epoch_changed")
        confirmed = self.dispatch_source.load_active(bundle.model)
        if confirmed is None or confirmed.payload() != bundle.payload():
            raise ProviderRegionalRuntimeError("provider_regional_dispatch_canonical_changed")
        return {
            "action": "tombstoned" if bundle.state == "absent" else "projected",
            "ready": False,
            "routing_generation": bundle.routing_generation,
            "cost_generation": bundle.cost_generation,
            "digest": bundle.digest(),
            "receipt_version": write.receipt_version,
        }

    def _finalize_dispatch(
        self,
        bundle: ProviderDispatchBundle,
        *,
        current: Mapping[str, Any],
        region: str,
        epoch: str,
    ) -> dict[str, Any]:
        confirmed = self.dispatch_source.load_active(bundle.model)
        if confirmed is None or confirmed.payload() != bundle.payload():
            raise ProviderRegionalRuntimeError("provider_regional_dispatch_canonical_changed")
        if load_redis_restart_epoch(self.redis_client) != epoch:
            raise ProviderRegionalRuntimeError("provider_regional_redis_epoch_changed")
        self.dispatch_projector.verify(
            bundle,
            region=region,
            redis_restart_epoch=epoch,
        )
        fresh = self.regional_source.load_projection_receipt(region, bundle.model, "dispatch")
        if (
            not _projection_receipt_matches(
                fresh,
                bundle=bundle,
                projection_type="dispatch",
                projection_digest=bundle.digest(),
                last_event_id=0,
                epoch=epoch,
            )
            or not _projection_subscriber_ack_complete(fresh)
        ):
            raise ProviderRegionalRuntimeError("provider_regional_projection_receipt_changed")
        verified_at = _projection_verification_time(self.clock(), fresh)
        ready = ProviderRegionProjectionReceipt(
            region=region,
            model=bundle.model,
            projection_type="dispatch",
            routing_generation=bundle.routing_generation,
            cost_generation=bundle.cost_generation,
            source_hash=bundle.source_hash,
            projection_digest=bundle.digest(),
            last_event_id=0,
            subscriber_cursor=int(fresh["subscriber_cursor"]),
            subscriber_connected=bool(fresh["subscriber_connected"]),
            redis_restart_epoch=epoch,
            projection_state="ready",
            projected_at=_aware(
                fresh.get("projected_at"),
                "provider_regional_projection_timestamp_invalid",
            ),
            subscriber_verified_at=_aware(
                fresh.get("subscriber_verified_at"),
                "provider_regional_projection_timestamp_invalid",
            ),
            verified_at=verified_at,
        )
        final = self.projection_store.record_projection_receipt(
            ready,
            expected_receipt_version=_receipt_version(fresh),
        )
        if load_redis_restart_epoch(self.redis_client) != epoch:
            raise ProviderRegionalRuntimeError("provider_regional_redis_epoch_changed")
        self.dispatch_projector.verify(
            bundle,
            region=region,
            redis_restart_epoch=epoch,
        )
        return {
            "action": "verified",
            "ready": True,
            "routing_generation": bundle.routing_generation,
            "cost_generation": bundle.cost_generation,
            "digest": bundle.digest(),
            "receipt_version": final.receipt_version,
        }

    def _reconcile_fast_block(
        self,
        bundle: ProviderDispatchBundle,
        manifest: ProviderFastBlockManifest,
        *,
        region: str,
        epoch: str,
    ) -> dict[str, Any]:
        current = self.regional_source.load_projection_receipt(
            region,
            bundle.model,
            "fast_block",
        )
        if _projection_receipt_matches(
            current,
            bundle=bundle,
            projection_type="fast_block",
            projection_digest=manifest.digest(),
            last_event_id=manifest.last_event_id,
            epoch=epoch,
        ):
            try:
                self.fast_block_projector.verify(
                    manifest,
                    region=region,
                    redis_restart_epoch=epoch,
                )
            except ProviderRegionalRuntimeError:
                return self._prepare_fast_block(
                    bundle,
                    manifest,
                    current=current,
                    region=region,
                    epoch=epoch,
                )
            if _projection_receipt_ready_complete(current):
                return {
                    "action": "verified",
                    "ready": True,
                    "last_event_id": manifest.last_event_id,
                    "event_count": len(manifest.entries),
                    "digest": manifest.digest(),
                    "receipt_version": _receipt_version(current),
                }
            if _projection_subscriber_ack_complete(current):
                return self._finalize_fast_block(
                    bundle,
                    manifest,
                    current=current,
                    region=region,
                    epoch=epoch,
                )
            return {
                "action": "awaiting-subscriber",
                "ready": False,
                "last_event_id": manifest.last_event_id,
                "event_count": len(manifest.entries),
                "digest": manifest.digest(),
                "receipt_version": _receipt_version(current),
            }
        return self._prepare_fast_block(
            bundle,
            manifest,
            current=current,
            region=region,
            epoch=epoch,
        )

    def _prepare_fast_block(
        self,
        bundle: ProviderDispatchBundle,
        manifest: ProviderFastBlockManifest,
        *,
        current: Mapping[str, Any] | None,
        region: str,
        epoch: str,
    ) -> dict[str, Any]:
        epoch_changed = current is None or _text(current.get("redis_restart_epoch")) != epoch
        started_at = _next_time(self.clock(), current)
        initial = ProviderRegionProjectionReceipt(
            region=region,
            model=bundle.model,
            projection_type="fast_block",
            routing_generation=bundle.routing_generation,
            cost_generation=bundle.cost_generation,
            source_hash=bundle.source_hash,
            projection_digest=manifest.digest(),
            last_event_id=manifest.last_event_id,
            subscriber_cursor=0,
            subscriber_connected=False,
            redis_restart_epoch=epoch,
            projection_state="resyncing" if epoch_changed else "projecting",
            projected_at=started_at,
        )
        write = self.projection_store.record_projection_receipt(
            initial,
            expected_receipt_version=_receipt_version(current),
        )
        self.fast_block_projector.publish(
            manifest,
            region=region,
            redis_restart_epoch=epoch,
        )
        if load_redis_restart_epoch(self.redis_client) != epoch:
            raise ProviderRegionalRuntimeError("provider_regional_redis_epoch_changed")
        confirmed = self.regional_source.load_fast_block_manifest(bundle)
        if confirmed.payload() != manifest.payload():
            raise ProviderRegionalRuntimeError("provider_regional_fast_block_canonical_changed")
        return {
            "action": "projected",
            "ready": False,
            "last_event_id": manifest.last_event_id,
            "event_count": len(manifest.entries),
            "digest": manifest.digest(),
            "receipt_version": write.receipt_version,
        }

    def _finalize_fast_block(
        self,
        bundle: ProviderDispatchBundle,
        manifest: ProviderFastBlockManifest,
        *,
        current: Mapping[str, Any],
        region: str,
        epoch: str,
    ) -> dict[str, Any]:
        confirmed = self.regional_source.load_fast_block_manifest(bundle)
        if confirmed.payload() != manifest.payload():
            raise ProviderRegionalRuntimeError("provider_regional_fast_block_canonical_changed")
        if load_redis_restart_epoch(self.redis_client) != epoch:
            raise ProviderRegionalRuntimeError("provider_regional_redis_epoch_changed")
        self.fast_block_projector.verify(
            manifest,
            region=region,
            redis_restart_epoch=epoch,
        )
        fresh = self.regional_source.load_projection_receipt(
            region,
            bundle.model,
            "fast_block",
        )
        if (
            not _projection_receipt_matches(
                fresh,
                bundle=bundle,
                projection_type="fast_block",
                projection_digest=manifest.digest(),
                last_event_id=manifest.last_event_id,
                epoch=epoch,
            )
            or not _projection_subscriber_ack_complete(fresh)
        ):
            raise ProviderRegionalRuntimeError("provider_regional_projection_receipt_changed")
        verified_at = _projection_verification_time(self.clock(), fresh)
        ready = ProviderRegionProjectionReceipt(
            region=region,
            model=bundle.model,
            projection_type="fast_block",
            routing_generation=bundle.routing_generation,
            cost_generation=bundle.cost_generation,
            source_hash=bundle.source_hash,
            projection_digest=manifest.digest(),
            last_event_id=manifest.last_event_id,
            subscriber_cursor=int(fresh["subscriber_cursor"]),
            subscriber_connected=bool(fresh["subscriber_connected"]),
            redis_restart_epoch=epoch,
            projection_state="ready",
            projected_at=_aware(
                fresh.get("projected_at"),
                "provider_regional_projection_timestamp_invalid",
            ),
            subscriber_verified_at=_aware(
                fresh.get("subscriber_verified_at"),
                "provider_regional_projection_timestamp_invalid",
            ),
            verified_at=verified_at,
        )
        final = self.projection_store.record_projection_receipt(
            ready,
            expected_receipt_version=_receipt_version(fresh),
        )
        if load_redis_restart_epoch(self.redis_client) != epoch:
            raise ProviderRegionalRuntimeError("provider_regional_redis_epoch_changed")
        self.fast_block_projector.verify(
            manifest,
            region=region,
            redis_restart_epoch=epoch,
        )
        return {
            "action": "verified",
            "ready": True,
            "last_event_id": manifest.last_event_id,
            "event_count": len(manifest.entries),
            "digest": manifest.digest(),
            "receipt_version": final.receipt_version,
        }

    def _reconcile_capacity(
        self,
        state: ProviderRegionalCapacityState,
        *,
        epoch: str,
    ) -> dict[str, Any]:
        if self.capacity_store is None:
            raise ProviderRegionalRuntimeError("provider_regional_capacity_store_missing")
        allocation = state.allocation
        current = state.receipt
        observed = self.capacity_projector.read(
            allocation,
            redis_restart_epoch=epoch,
        )
        if observed is not None and _capacity_receipt_matches(
            current,
            allocation=allocation,
            snapshot=observed.snapshot,
        ):
            receipt_state = str(current.get("receipt_state") or "").strip().casefold()
            final_state = (
                "ready"
                if allocation.allocation_state == "active"
                else allocation.allocation_state
            )
            if (
                receipt_state == "ready"
                and observed.state == final_state
                and not observed.receipt_dirty
                and observed.receipt_version == _receipt_version(current)
                and observed.snapshot.restart_reconciled
            ):
                self.capacity_projector.verify(
                    allocation,
                    observed.snapshot,
                    receipt_version=observed.receipt_version,
                    final_state=final_state,
                )
                return {
                    "account_id": allocation.enodot_account_id,
                    "allocation_version": allocation.allocation_version,
                    "action": "verified",
                    "ready": True,
                    "occupancy": observed.snapshot.occupancy,
                    "quota": allocation.quota,
                    "lease_cursor": observed.snapshot.lease_cursor,
                    "receipt_version": observed.receipt_version,
                }
            if (
                receipt_state == "ready"
                and _capacity_receipt_has_exact_ack(current)
                and observed.state == "resyncing"
                and not observed.receipt_dirty
                and observed.receipt_version + 1 == _receipt_version(current)
                and observed.snapshot.restart_reconciled
            ):
                self._confirm_capacity_allocation(allocation, epoch=epoch)
                self.capacity_projector.finalize(
                    allocation,
                    observed.snapshot,
                    receipt_version=_receipt_version(current),
                )
                return {
                    "account_id": allocation.enodot_account_id,
                    "allocation_version": allocation.allocation_version,
                    "action": "finalized",
                    "ready": True,
                    "occupancy": observed.snapshot.occupancy,
                    "quota": allocation.quota,
                    "lease_cursor": observed.snapshot.lease_cursor,
                    "receipt_version": _receipt_version(current),
                }
            if receipt_state == "resyncing":
                current_version = _receipt_version(current)
                sealed = (
                    observed.state == "resyncing"
                    and not observed.receipt_dirty
                    and observed.receipt_version == current_version
                )
                if sealed:
                    self.capacity_projector.verify(
                        allocation,
                        observed.snapshot,
                        receipt_version=current_version,
                        final_state="resyncing",
                    )
                    error_class = _capacity_receipt_error(current)
                    if error_class == "redis_restart_grace":
                        grace_until = _aware(
                            current.get("projected_at"),
                            "provider_regional_capacity_projected_at_invalid",
                        ) + timedelta(seconds=self.restart_grace_seconds)
                        if _aware(
                            self.clock(),
                            "provider_regional_clock_invalid",
                        ) < grace_until:
                            return _capacity_result(
                                allocation,
                                observed.snapshot,
                                action="restart-grace",
                                ready=False,
                                receipt_version=current_version,
                            )
                    if error_class is not None or not observed.snapshot.restart_reconciled:
                        if not observed.snapshot.restart_reconciled:
                            return _capacity_result(
                                allocation,
                                observed.snapshot,
                                action="awaiting-durable-reconcile",
                                ready=False,
                                receipt_version=current_version,
                            )
                        # Begin a new clean resync receipt after recovery is proven.
                    else:
                        return _capacity_result(
                            allocation,
                            observed.snapshot,
                            action="awaiting-subscriber",
                            ready=False,
                            receipt_version=current_version,
                        )
                elif (
                    observed.state == "resyncing"
                    and observed.receipt_dirty
                    and observed.receipt_version < current_version
                ):
                    self._confirm_capacity_allocation(allocation, epoch=epoch)
                    self.capacity_projector.seal(
                        allocation,
                        observed.snapshot,
                        receipt_version=current_version,
                    )
                    return _capacity_result(
                        allocation,
                        observed.snapshot,
                        action="sealed",
                        ready=False,
                        receipt_version=current_version,
                    )
        snapshot = self.capacity_projector.begin(
            allocation,
            redis_restart_epoch=epoch,
        )
        current_epoch = None if current is None else _text(current.get("redis_restart_epoch"))
        if snapshot.epoch_changed and current_epoch == epoch:
            raise ProviderRegionalRuntimeError("provider_regional_capacity_metadata_epoch_mismatch")
        projected_at = _next_time(self.clock(), current)
        error_class = None
        if snapshot.epoch_changed:
            error_class = "redis_restart_grace"
        elif not snapshot.restart_reconciled:
            error_class = "capacity_reconcile_pending"
        initial = ProviderRegionCapacityReceipt(
            enodot_binding_id=allocation.enodot_binding_id,
            enodot_account_id=allocation.enodot_account_id,
            region=allocation.region,
            allocation_version=allocation.allocation_version,
            quota=allocation.quota,
            occupancy=snapshot.occupancy,
            occupancy_digest=snapshot.occupancy_digest,
            lease_cursor=snapshot.lease_cursor,
            redis_restart_epoch=epoch,
            projected_at=projected_at,
            receipt_state="resyncing",
            last_error_class=error_class,
        )
        write = self.capacity_store.record_capacity_receipt(
            initial,
            expected_receipt_version=_receipt_version(current),
        )
        self._confirm_capacity_allocation(allocation, epoch=epoch)
        self.capacity_projector.seal(
            allocation,
            snapshot,
            receipt_version=write.receipt_version,
        )
        return _capacity_result(
            allocation,
            snapshot,
            action="sealed",
            ready=False,
            receipt_version=write.receipt_version,
        )

    def _confirm_capacity_allocation(
        self,
        allocation: ProviderRegionalCapacityAllocation,
        *,
        epoch: str,
    ) -> None:
        if load_redis_restart_epoch(self.redis_client) != epoch:
            raise ProviderRegionalRuntimeError("provider_regional_redis_epoch_changed")
        confirmed = self.regional_source.load_capacity_state(
            allocation.region,
            allocation.enodot_account_id,
        )
        if confirmed is None or confirmed.allocation != allocation:
            raise ProviderRegionalRuntimeError("provider_regional_capacity_canonical_changed")


class ProviderRegionalNotificationConsumer:
    """Treat regional Pub/Sub messages only as exact-reread wakeups."""

    def __init__(
        self,
        runtime: ProviderRegionalProjectionRuntime,
        *,
        dispatch_channel: str = "a6:provider:dispatch:changed:v1",
        fast_block_channel: str = "a6:provider:fast-block:changed:v1",
        capacity_channel: str = "a6:provider:capacity:changed:v1",
    ) -> None:
        self.runtime = runtime
        self.dispatch_channel = _redis_name(dispatch_channel)
        self.fast_block_channel = _redis_name(fast_block_channel)
        self.capacity_channel = _redis_name(capacity_channel)

    def handle(
        self,
        channel: object,
        payload: object,
        *,
        model: str,
        region: str,
    ) -> dict[str, Any]:
        channel = _text(channel)
        payload = _text(payload)
        model = _model(model)
        region = _region(region)
        if channel == self.capacity_channel:
            document = _capacity_notification(payload)
            if document is None:
                return self.runtime.reconcile_capacities(region=region)
            if document.get("origin") == "projector":
                return {
                    "success": True,
                    "region": region,
                    "action": "projector-hint-observed",
                }
            if document["region"] != region:
                raise ProviderRegionalRuntimeError("provider_regional_notification_region_mismatch")
            return self.runtime.reconcile_capacity(
                region=region,
                account_id=document["account_id"],
            )
        if channel not in {self.dispatch_channel, self.fast_block_channel}:
            raise ProviderRegionalRuntimeError("provider_regional_notification_channel_invalid")
        try:
            document = json.loads(payload)
        except json.JSONDecodeError:
            raise ProviderRegionalRuntimeError("provider_regional_notification_invalid") from None
        if (
            not isinstance(document, Mapping)
            or document.get("schema_version") != 1
            or document.get("model") != model
        ):
            raise ProviderRegionalRuntimeError("provider_regional_notification_invalid")
        return self.runtime.reconcile(model, region=region)

    def run_forever(
        self,
        client: Any,
        *,
        model: str,
        region: str,
        reconnect_seconds: float = 1.0,
        poll_seconds: float = 1.0,
    ) -> None:
        if reconnect_seconds < 0.05 or reconnect_seconds > 60:
            raise ProviderRegionalRuntimeError("provider_regional_notification_reconnect_invalid")
        if poll_seconds < 0.05 or poll_seconds > 60:
            raise ProviderRegionalRuntimeError("provider_regional_notification_poll_invalid")
        channels = (
            self.dispatch_channel,
            self.fast_block_channel,
            self.capacity_channel,
        )
        while True:
            pubsub = None
            try:
                pubsub = client.pubsub(ignore_subscribe_messages=True)
                pubsub.subscribe(*channels)
                # Subscribe first so messages committed during full resync remain queued.
                self.runtime.reconcile(_model(model), region=_region(region))
                get_message = getattr(pubsub, "get_message", None)
                if not callable(get_message):
                    raise ProviderRegionalRuntimeError(
                        "provider_regional_notification_poll_unsupported"
                    )
                while True:
                    message = get_message(timeout=poll_seconds)
                    if isinstance(message, Mapping) and message.get("type") == "message":
                        self.handle(
                            message.get("channel"),
                            message.get("data"),
                            model=model,
                            region=region,
                        )
                    else:
                        self.runtime.reconcile(_model(model), region=_region(region))
            except KeyboardInterrupt:
                raise
            except Exception:
                time.sleep(reconnect_seconds)
            finally:
                close = getattr(pubsub, "close", None)
                if callable(close):
                    close()


def _fast_block_projection(row: Mapping[str, Any]) -> ProviderFastBlockProjection:
    operation = str(row.get("operation") or "")
    quarantine_state = str(row.get("quarantine_state") or "")
    if (
        row.get("quarantine_id") is None
        or (operation == "block" and quarantine_state not in {"active", "probing"})
        or (operation == "unblock" and quarantine_state != "closed")
    ):
        raise ProviderRegionalRuntimeError("provider_regional_fast_block_canonical_state_invalid")
    payload = row.get("payload")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            raise ProviderRegionalRuntimeError(
                "provider_regional_fast_block_payload_invalid"
            ) from None
    if not isinstance(payload, Mapping):
        raise ProviderRegionalRuntimeError("provider_regional_fast_block_payload_invalid")
    return ProviderFastBlockProjection(
        event_id=int(row["id"]),
        operation=operation,
        model=str(row["model"]),
        routing_generation=int(row["routing_generation"]),
        cost_generation=int(row["cost_generation"]),
        slot_no=int(row["slot_no"]),
        channel_id=int(row["channel_id"]),
        projection_key=str(row["projection_key"]),
        payload=dict(payload),
    )


def _capacity_state(row: Mapping[str, Any]) -> ProviderRegionalCapacityState:
    allocation = ProviderRegionalCapacityAllocation(
        allocation_id=int(row["allocation_id"]),
        enodot_binding_id=int(row["enodot_binding_id"]),
        enodot_account_id=int(row["enodot_account_id"]),
        region=str(row["region"]),
        quota=int(row["quota"]),
        account_concurrency=int(row["account_concurrency"]),
        allocation_version=int(row["allocation_version"]),
        allocation_digest=str(row["allocation_digest"]),
        source_hash=str(row["source_hash"]),
        allocation_state=str(row["allocation_state"]),
        drain_target_quota=(
            None if row.get("drain_target_quota") is None else int(row["drain_target_quota"])
        ),
        effective_at=row["effective_at"],
    )
    receipt = None
    if row.get("receipt_version") is not None:
        receipt = {
            "enodot_binding_id": row["receipt_binding_id"],
            "enodot_account_id": row["enodot_account_id"],
            "region": row["region"],
            "allocation_version": row["receipt_allocation_version"],
            "quota": row["receipt_quota"],
            "occupancy": row["receipt_occupancy"],
            "occupancy_digest": row["receipt_occupancy_digest"],
            "lease_cursor": row["receipt_lease_cursor"],
            "redis_restart_epoch": row["receipt_redis_restart_epoch"],
            "receipt_state": row["receipt_state"],
            "receipt_version": row["receipt_version"],
            "projected_at": row["receipt_projected_at"],
            "verified_at": row["receipt_verified_at"],
            "last_error_class": row["receipt_last_error_class"],
        }
    return ProviderRegionalCapacityState(allocation, receipt)


def _capacity_notification(payload: str) -> dict[str, Any] | None:
    if _CAPACITY_NOTIFICATION_LEGACY.fullmatch(payload):
        return None
    try:
        document = json.loads(payload)
    except json.JSONDecodeError:
        raise ProviderRegionalRuntimeError(
            "provider_regional_capacity_notification_invalid"
        ) from None
    allowed = {
        "schema_version",
        "hint",
        "projection_type",
        "region",
        "account_id",
        "allocation_version",
        "lease_cursor",
        "state",
        "origin",
    }
    if (
        not isinstance(document, Mapping)
        or set(document) - allowed
        or document.get("schema_version") != 1
        or document.get("hint") != "exact_reread"
        or document.get("projection_type") != "capacity"
        or str(document.get("origin") or "request_plane") not in {"projector", "request_plane"}
    ):
        raise ProviderRegionalRuntimeError("provider_regional_capacity_notification_invalid")
    return {
        "region": _region(document.get("region")),
        "account_id": _positive_int(
            document.get("account_id"),
            "provider_regional_capacity_notification_invalid",
        ),
        "allocation_version": _positive_int(
            document.get("allocation_version"),
            "provider_regional_capacity_notification_invalid",
        ),
        "lease_cursor": _nonnegative_int(
            document.get("lease_cursor"),
            "provider_regional_capacity_notification_invalid",
        ),
        "origin": str(document.get("origin") or "request_plane"),
    }


def _capacity_digest(
    allocation: ProviderRegionalCapacityAllocation,
    *,
    cursor: int,
    leases: Sequence[tuple[str, str]],
) -> str:
    payload = _canonical_json(
        {
            "schema_version": 1,
            "region": allocation.region,
            "account_id": allocation.enodot_account_id,
            "allocation_version": allocation.allocation_version,
            "quota": allocation.quota,
            "lease_cursor": cursor,
            "leases": [{"request_id": request_id, "score": score} for request_id, score in leases],
        }
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _capacity_snapshot_payload(snapshot: ProviderCapacityRedisSnapshot) -> str:
    identities = dict(snapshot.identities)
    if set(identities) != {member for member, _score in snapshot.leases}:
        raise ProviderRegionalRuntimeError("provider_regional_capacity_identity_mismatch")
    return _canonical_json(
        [
            value
            for member, score in snapshot.leases
            for value in (member, score, identities[member])
        ]
    )


def _capacity_receipt_matches(
    row: Mapping[str, Any] | None,
    *,
    allocation: ProviderRegionalCapacityAllocation,
    snapshot: ProviderCapacityRedisSnapshot,
) -> bool:
    if row is None:
        return False
    try:
        return (
            int(row.get("enodot_binding_id")) == allocation.enodot_binding_id
            and int(row.get("enodot_account_id")) == allocation.enodot_account_id
            and _region(row.get("region")) == allocation.region
            and int(row.get("allocation_version")) == allocation.allocation_version
            and _nonnegative_int(
                row.get("quota"),
                "provider_regional_capacity_quota_invalid",
            )
            == allocation.quota
            and _nonnegative_int(
                row.get("occupancy"),
                "provider_regional_capacity_occupancy_invalid",
            )
            == snapshot.occupancy
            and _sha256(
                row.get("occupancy_digest"),
                "provider_regional_capacity_digest_invalid",
            )
            == snapshot.occupancy_digest
            and _nonnegative_int(
                row.get("lease_cursor"),
                "provider_regional_capacity_cursor_invalid",
            )
            == snapshot.lease_cursor
            and _epoch(row.get("redis_restart_epoch")) == snapshot.redis_restart_epoch
        )
    except (ProviderRegionalRuntimeError, TypeError, ValueError):
        return False


def _capacity_receipt_has_exact_ack(row: Mapping[str, Any]) -> bool:
    """Validate receipt-local ACK proof; subscriber liveness is tracked separately."""

    try:
        projected_at = _aware(
            row.get("projected_at"),
            "provider_regional_capacity_projected_at_invalid",
        )
        verified_at = _aware(
            row.get("verified_at"),
            "provider_regional_capacity_verified_at_invalid",
        )
    except ProviderRegionalRuntimeError:
        return False
    return (
        str(row.get("receipt_state") or "").strip().casefold() == "ready"
        and _capacity_receipt_error(row) is None
        and verified_at >= projected_at
    )


def _capacity_receipt_error(row: Mapping[str, Any]) -> str | None:
    raw = row.get("last_error_class")
    if raw is None:
        return None
    value = str(raw).strip()
    if not value or len(value) > 160 or any(ord(character) < 0x20 for character in value):
        raise ProviderRegionalRuntimeError("provider_regional_capacity_error_class_invalid")
    return value


def _capacity_result(
    allocation: ProviderRegionalCapacityAllocation,
    snapshot: ProviderCapacityRedisSnapshot,
    *,
    action: str,
    ready: bool,
    receipt_version: int,
) -> dict[str, Any]:
    return {
        "account_id": allocation.enodot_account_id,
        "allocation_version": allocation.allocation_version,
        "action": action,
        "ready": ready,
        "occupancy": snapshot.occupancy,
        "quota": allocation.quota,
        "lease_cursor": snapshot.lease_cursor,
        "receipt_version": receipt_version,
    }


def _validate_capacity_lease_identities(
    leases: Sequence[tuple[str, str]],
    identities: Mapping[str, str],
    *,
    account_id: int,
    region: str,
    allocation_version: int,
) -> None:
    members = {member for member, _score in leases}
    if set(identities) != members:
        raise ProviderRegionalRuntimeError("provider_regional_capacity_identity_mismatch")
    for member in members:
        try:
            document = json.loads(identities[member])
        except (json.JSONDecodeError, TypeError):
            raise ProviderRegionalRuntimeError(
                "provider_regional_capacity_identity_invalid"
            ) from None
        if (
            not isinstance(document, Mapping)
            or document.get("schema_version") != 1
            or document.get("attempt_id") != member
            or document.get("account_id") != account_id
            or document.get("region") != region
            or document.get("allocation_version") != allocation_version
            or not isinstance(document.get("dispatch_owner_id"), str)
            or not _CAPACITY_DISPATCH_OWNER_ID.fullmatch(document["dispatch_owner_id"])
            or not document["dispatch_owner_id"].startswith(f"{region}:")
            or any(
                not isinstance(document.get(field), str) or not document[field]
                for field in ("lease_id", "request_id")
            )
        ):
            raise ProviderRegionalRuntimeError("provider_regional_capacity_identity_invalid")


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )


def _canonical_score(value: object) -> str:
    try:
        score = Decimal(_text(value))
    except (InvalidOperation, ValueError):
        raise ProviderRegionalRuntimeError("provider_regional_capacity_score_invalid") from None
    if not score.is_finite() or score < 0:
        raise ProviderRegionalRuntimeError("provider_regional_capacity_score_invalid")
    rendered = format(score, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered or "0"


def _receipt_version(row: Mapping[str, Any] | None) -> int:
    if row is None:
        return 0
    return _positive_int(
        row.get("receipt_version"),
        "provider_regional_receipt_version_invalid",
    )


def _projection_receipt_matches(
    row: Mapping[str, Any] | None,
    *,
    bundle: ProviderDispatchBundle,
    projection_type: str,
    projection_digest: str,
    last_event_id: int,
    epoch: str,
) -> bool:
    if row is None:
        return False
    try:
        return (
            _region(row.get("region")) in {"us", "hk"}
            and _model(row.get("model")) == bundle.model
            and str(row.get("projection_type") or "").strip().casefold()
            == projection_type
            and int(row.get("routing_generation")) == bundle.routing_generation
            and int(row.get("cost_generation")) == bundle.cost_generation
            and _sha256(
                row.get("source_hash"),
                "provider_regional_projection_source_hash_invalid",
            )
            == bundle.source_hash
            and _sha256(
                row.get("projection_digest"),
                "provider_regional_projection_digest_invalid",
            )
            == projection_digest
            and _nonnegative_int(
                row.get("last_event_id"),
                "provider_regional_projection_cursor_invalid",
            )
            == last_event_id
            and _epoch(row.get("redis_restart_epoch")) == epoch
        )
    except (ProviderRegionalRuntimeError, TypeError, ValueError):
        return False


def _projection_subscriber_ack_complete(row: Mapping[str, Any] | None) -> bool:
    if row is None or not bool(row.get("subscriber_connected")):
        return False
    try:
        cursor = _nonnegative_int(
            row.get("subscriber_cursor"),
            "provider_regional_projection_cursor_invalid",
        )
        last_event_id = _nonnegative_int(
            row.get("last_event_id"),
            "provider_regional_projection_cursor_invalid",
        )
        projected_at = _aware(
            row.get("projected_at"),
            "provider_regional_projection_timestamp_invalid",
        )
        subscriber_verified_at = _aware(
            row.get("subscriber_verified_at"),
            "provider_regional_projection_timestamp_invalid",
        )
    except ProviderRegionalRuntimeError:
        return False
    return cursor == last_event_id and subscriber_verified_at >= projected_at


def _projection_receipt_ready_complete(row: Mapping[str, Any] | None) -> bool:
    if (
        row is None
        or str(row.get("projection_state") or "").strip().casefold() != "ready"
        or row.get("last_error_class") is not None
        or not _projection_subscriber_ack_complete(row)
    ):
        return False
    try:
        verified_at = _aware(
            row.get("verified_at"),
            "provider_regional_projection_timestamp_invalid",
        )
        subscriber_verified_at = _aware(
            row.get("subscriber_verified_at"),
            "provider_regional_projection_timestamp_invalid",
        )
    except ProviderRegionalRuntimeError:
        return False
    return verified_at >= subscriber_verified_at


def _projection_verification_time(
    value: datetime,
    row: Mapping[str, Any],
) -> datetime:
    previous = max(
        _aware(
            row.get("projected_at"),
            "provider_regional_projection_timestamp_invalid",
        ),
        _aware(
            row.get("subscriber_verified_at"),
            "provider_regional_projection_timestamp_invalid",
        ),
        _aware(
            row.get("verified_at"),
            "provider_regional_projection_timestamp_invalid",
        )
        if row.get("verified_at") is not None
        else datetime.min.replace(tzinfo=timezone.utc),
    )
    return _strictly_after(value, previous)


def _next_time(value: datetime, current: Mapping[str, Any] | None) -> datetime:
    value = _aware(value, "provider_regional_clock_invalid")
    if current is None:
        return value
    return _strictly_after(
        value,
        _aware(
            current.get("projected_at"),
            "provider_regional_receipt_time_invalid",
        ),
    )


def _strictly_after(value: datetime, previous: datetime) -> datetime:
    value = _aware(value, "provider_regional_clock_invalid")
    previous = _aware(previous, "provider_regional_receipt_time_invalid")
    return value if value > previous else previous + timedelta(microseconds=1)


def _model(value: object) -> str:
    model = str(value or "").strip()
    if not model or len(model) > 128 or any(ord(character) < 0x20 for character in model):
        raise ProviderRegionalRuntimeError("provider_regional_model_invalid")
    return model


def _region(value: object) -> str:
    region = str(value or "").strip().casefold()
    if region not in {"us", "hk"}:
        raise ProviderRegionalRuntimeError("provider_regional_region_invalid")
    return region


def _epoch(value: object) -> str:
    epoch = _text(value)
    if not epoch or len(epoch) > 128 or any(character.isspace() for character in epoch):
        raise ProviderRegionalRuntimeError("provider_regional_redis_epoch_invalid")
    return epoch


def _redis_name(value: object) -> str:
    name = str(value or "").strip()
    if (
        not name
        or len(name) > 256
        or any(character.isspace() or ord(character) < 0x20 for character in name)
    ):
        raise ProviderRegionalRuntimeError("provider_regional_redis_name_invalid")
    return name


def _text(value: object) -> str:
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError:
            raise ProviderRegionalRuntimeError("provider_regional_redis_text_invalid") from None
    return "" if value is None else str(value)


def _positive_int(value: object, code: str) -> int:
    if isinstance(value, bool):
        raise ProviderRegionalRuntimeError(code)
    try:
        result = int(value)
    except (TypeError, ValueError):
        raise ProviderRegionalRuntimeError(code) from None
    if result < 1:
        raise ProviderRegionalRuntimeError(code)
    return result


def _nonnegative_int(value: object, code: str) -> int:
    if isinstance(value, bool):
        raise ProviderRegionalRuntimeError(code)
    try:
        result = int(value or 0)
    except (TypeError, ValueError):
        raise ProviderRegionalRuntimeError(code) from None
    if result < 0:
        raise ProviderRegionalRuntimeError(code)
    return result


def _sha256(value: object, code: str) -> str:
    digest = str(value or "").strip().casefold()
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise ProviderRegionalRuntimeError(code)
    return digest


def _aware(value: object, code: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ProviderRegionalRuntimeError(code)
    normalized = value.astimezone(timezone.utc)
    if normalized.utcoffset() != timedelta(0):
        raise ProviderRegionalRuntimeError(code)
    return normalized


def _script_int(value: object, code: str) -> int:
    if isinstance(value, bool):
        raise ProviderRegionalRuntimeError(code)
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ProviderRegionalRuntimeError(code) from None


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


def _rollback(connection: Any) -> None:
    rollback = getattr(connection, "rollback", None)
    if callable(rollback):
        rollback()


def _close(connection: Any) -> None:
    close = getattr(connection, "close", None)
    if callable(close):
        close()


_PROJECTION_RECEIPT_SQL = """
SELECT *
FROM a6_trade.provider_region_projection_receipts
WHERE region = %s AND model = %s AND projection_type = %s
"""


_FAST_BLOCK_HWM_SQL = """
SELECT coalesce(max(id), 0) AS last_event_id
FROM a6_trade.provider_fast_block_outbox
WHERE model = %s
"""


_FAST_BLOCK_LATEST_SQL = """
WITH latest AS (
    SELECT DISTINCT ON (projection.projection_key) projection.id
    FROM a6_trade.provider_fast_block_outbox projection
    WHERE projection.model = %s
      AND projection.routing_generation = %s
      AND projection.cost_generation = %s
    ORDER BY projection.projection_key, projection.id DESC
)
SELECT projection.id, projection.operation, projection.model,
       projection.routing_generation, projection.cost_generation,
       projection.slot_no, projection.channel_id, projection.quarantine_id,
       projection.projection_key, projection.payload,
       quarantine.state AS quarantine_state
FROM latest
JOIN a6_trade.provider_fast_block_outbox projection
  ON projection.id = latest.id
LEFT JOIN a6_trade.provider_quarantines quarantine
  ON quarantine.id = projection.quarantine_id
ORDER BY projection.projection_key
LIMIT %s
"""


_CAPACITY_SELECT_SQL = """
SELECT allocation.id AS allocation_id, allocation.enodot_binding_id,
       allocation.enodot_account_id, allocation.region, allocation.quota,
       allocation.account_concurrency, allocation.allocation_version,
       allocation.allocation_digest, allocation.source_hash,
       allocation.allocation_state, allocation.drain_target_quota,
       allocation.effective_at,
       receipt.enodot_binding_id AS receipt_binding_id,
       receipt.allocation_version AS receipt_allocation_version,
       receipt.quota AS receipt_quota,
       receipt.occupancy AS receipt_occupancy,
       receipt.occupancy_digest AS receipt_occupancy_digest,
       receipt.lease_cursor AS receipt_lease_cursor,
       receipt.redis_restart_epoch AS receipt_redis_restart_epoch,
       receipt.receipt_state, receipt.receipt_version,
       receipt.projected_at AS receipt_projected_at,
       receipt.verified_at AS receipt_verified_at,
       receipt.last_error_class AS receipt_last_error_class
FROM a6_trade.provider_region_capacity_allocations allocation
LEFT JOIN a6_trade.provider_region_capacity_receipts receipt
  ON receipt.enodot_account_id = allocation.enodot_account_id
 AND receipt.region = allocation.region
WHERE allocation.region = %s
  AND allocation.valid_to IS NULL
"""

_CAPACITY_STATES_SQL = _CAPACITY_SELECT_SQL + " ORDER BY allocation.enodot_account_id"
_CAPACITY_STATE_SQL = (
    _CAPACITY_SELECT_SQL
    + " AND allocation.enodot_account_id = %s ORDER BY allocation.enodot_account_id"
)


_FINALIZE_FAST_BLOCK_MANIFEST_LUA = r"""
local prefix = ARGV[1]
local desired = cjson.decode(ARGV[2])
if type(desired) ~= 'table' then
    return -2
end
local desired_set = {}
for _, key in ipairs(desired) do
    if type(key) ~= 'string' or string.sub(key, 1, string.len(prefix)) ~= prefix then
        return -2
    end
    desired_set[key] = true
end
local current = redis.call('SMEMBERS', KEYS[1])
for _, key in ipairs(current) do
    if string.sub(key, 1, string.len(prefix)) ~= prefix then
        return -1
    end
    if not desired_set[key] then
        redis.call('DEL', key)
        redis.call('DEL', key .. ':cursor')
    end
end
redis.call('DEL', KEYS[1])
for _, key in ipairs(desired) do
    redis.call('SADD', KEYS[1], key)
end
redis.call('SET', KEYS[2], ARGV[3])
redis.call('SET', KEYS[3], ARGV[4])
redis.call('PUBLISH', ARGV[5], ARGV[6])
return #desired
"""


_BEGIN_CAPACITY_PROJECTION_LUA = r"""
local function owner_valid(owner, region)
    if type(owner) ~= 'string'
       or string.sub(owner, 1, string.len(region) + 1) ~= region .. ':' then
        return false
    end
    local first, second, third, fourth, fifth = string.match(
        string.sub(owner, string.len(region) + 2),
        '^([0-9a-f]+)%-([0-9a-f]+)%-([0-9a-f]+)%-([0-9a-f]+)%-([0-9a-f]+)$'
    )
    return first ~= nil and #first == 8 and #second == 4 and #third == 4
        and #fourth == 4 and #fifth == 12
end

local function identities_valid(
    leases_key, identities_key, account_id, region, allocation_version
)
    local members = redis.call('ZRANGE', leases_key, 0, -1)
    if redis.call('HLEN', identities_key) ~= #members then
        return false
    end
    for _, member in ipairs(members) do
        local raw = redis.call('HGET', identities_key, member)
        if raw == false then
            return false
        end
        local ok, identity = pcall(cjson.decode, raw)
        if not ok or type(identity) ~= 'table'
           or identity.schema_version ~= 1
           or identity.attempt_id ~= member
           or tostring(identity.account_id) ~= account_id
           or identity.region ~= region
           or tostring(identity.allocation_version) ~= allocation_version
           or type(identity.lease_id) ~= 'string' or identity.lease_id == ''
           or type(identity.request_id) ~= 'string' or identity.request_id == ''
           or not owner_valid(identity.dispatch_owner_id, region) then
            return false
        end
    end
    return true
end

local account_id = ARGV[1]
local region = ARGV[2]
local quota = tonumber(ARGV[3])
local allocation_version = ARGV[4]
local restart_epoch = ARGV[5]
local allocation_state = ARGV[6]
local channel = ARGV[7]
if quota == nil or quota < 0 then
    return {-3, 0, 0}
end
local metadata_exists = redis.call('EXISTS', KEYS[2])
local existing_count = redis.call('ZCARD', KEYS[1])
if not identities_valid(
    KEYS[1], KEYS[3], account_id, region, allocation_version
) then
    return {-5, 0, existing_count, 0}
end
if metadata_exists == 0 and existing_count > 0 then
    return {-1, 0, existing_count, 0}
end
local current_epoch = redis.call('HGET', KEYS[2], 'restart_epoch')
local epoch_changed = metadata_exists == 0 or current_epoch ~= restart_epoch
local cursor = tonumber(redis.call('HGET', KEYS[2], 'lease_cursor') or '0')
if cursor == nil then
    return {-2, 0, existing_count, 0}
end
if epoch_changed then
    cursor = 0
    redis.call('HDEL', KEYS[2], 'reconciled_restart_epoch')
end
local occupancy = redis.call('ZCARD', KEYS[1])
local recorded_occupancy = tonumber(redis.call('HGET', KEYS[2], 'occupancy') or occupancy)
if not epoch_changed and recorded_occupancy ~= occupancy then
    return {-4, cursor, occupancy, 0}
end
redis.call('HSET', KEYS[2],
    'account_id', account_id,
    'region', region,
    'allocation_version', allocation_version,
    'quota', quota,
    'restart_epoch', restart_epoch,
    'allocation_state', allocation_state,
    'state', 'resyncing',
    'receipt_dirty', 1,
    'lease_cursor', cursor,
    'occupancy', occupancy)
redis.call('PUBLISH', channel, cjson.encode({
    schema_version = 1,
    hint = 'exact_reread',
    projection_type = 'capacity',
    origin = 'projector',
    region = region,
    account_id = account_id,
    allocation_version = allocation_version,
    lease_cursor = cursor,
    state = 'resyncing'
}))
local leases = redis.call('ZRANGE', KEYS[1], 0, -1, 'WITHSCORES')
local restart_reconciled = redis.call(
    'HGET', KEYS[2], 'reconciled_restart_epoch'
) == restart_epoch
local result = {
    epoch_changed and 1 or 0,
    cursor,
    occupancy,
    restart_reconciled and 1 or 0
}
for index = 1, #leases, 2 do
    local member = leases[index]
    table.insert(result, member)
    table.insert(result, leases[index + 1])
    table.insert(result, redis.call('HGET', KEYS[3], member))
end
return result
"""


_SEAL_CAPACITY_PROJECTION_LUA = r"""
local function owner_valid(owner, region)
    if type(owner) ~= 'string'
       or string.sub(owner, 1, string.len(region) + 1) ~= region .. ':' then
        return false
    end
    local first, second, third, fourth, fifth = string.match(
        string.sub(owner, string.len(region) + 2),
        '^([0-9a-f]+)%-([0-9a-f]+)%-([0-9a-f]+)%-([0-9a-f]+)%-([0-9a-f]+)$'
    )
    return first ~= nil and #first == 8 and #second == 4 and #third == 4
        and #fourth == 4 and #fifth == 12
end

local function snapshot_valid(
    leases_key, identities_key, account_id, region, allocation_version,
    occupancy, payload
)
    local ok, expected = pcall(cjson.decode, payload)
    if not ok or type(expected) ~= 'table' or #expected ~= occupancy * 3 then
        return false
    end
    local actual = redis.call('ZRANGE', leases_key, 0, -1, 'WITHSCORES')
    if #actual ~= occupancy * 2 or redis.call('HLEN', identities_key) ~= occupancy then
        return false
    end
    for actual_index = 1, #actual, 2 do
        local expected_index = ((actual_index - 1) / 2) * 3 + 1
        local member = actual[actual_index]
        if member ~= expected[expected_index]
           or tonumber(actual[actual_index + 1])
              ~= tonumber(expected[expected_index + 1]) then
            return false
        end
        local raw = redis.call('HGET', identities_key, member)
        if raw == false or raw ~= expected[expected_index + 2] then
            return false
        end
        local decoded, identity = pcall(cjson.decode, raw)
        if not decoded or type(identity) ~= 'table'
           or identity.schema_version ~= 1
           or identity.attempt_id ~= member
           or tostring(identity.account_id) ~= account_id
           or identity.region ~= region
           or tostring(identity.allocation_version) ~= allocation_version
           or type(identity.lease_id) ~= 'string' or identity.lease_id == ''
           or type(identity.request_id) ~= 'string' or identity.request_id == ''
           or not owner_valid(identity.dispatch_owner_id, region) then
            return false
        end
    end
    return true
end

local allocation_version = ARGV[1]
local quota = ARGV[2]
local restart_epoch = ARGV[3]
local cursor = ARGV[4]
local occupancy = tonumber(ARGV[5])
local digest = ARGV[6]
local receipt_version = ARGV[7]
local channel = ARGV[8]
local leases_payload = ARGV[9]
local account_id = redis.call('HGET', KEYS[2], 'account_id')
local region = redis.call('HGET', KEYS[2], 'region')
local desired_version = tonumber(receipt_version)
local current_version = tonumber(redis.call('HGET', KEYS[2], 'receipt_version') or '0')
if occupancy == nil or desired_version == nil or current_version == nil
   or desired_version <= current_version then
    return 0
end
if redis.call('HGET', KEYS[2], 'state') ~= 'resyncing'
   or redis.call('HGET', KEYS[2], 'receipt_dirty') ~= '1'
   or redis.call('HGET', KEYS[2], 'allocation_version') ~= allocation_version
   or redis.call('HGET', KEYS[2], 'quota') ~= quota
   or redis.call('HGET', KEYS[2], 'restart_epoch') ~= restart_epoch
   or redis.call('HGET', KEYS[2], 'lease_cursor') ~= cursor
   or account_id == false
   or region == false
   or not snapshot_valid(
       KEYS[1], KEYS[3], account_id, region, allocation_version,
       occupancy, leases_payload
   ) then
    return 0
end
redis.call('HSET', KEYS[2],
    'state', 'resyncing',
    'occupancy', occupancy,
    'occupancy_digest', digest,
    'receipt_version', receipt_version,
    'receipt_dirty', 0)
redis.call('PUBLISH', channel, cjson.encode({
    schema_version = 1,
    hint = 'exact_reread',
    projection_type = 'capacity',
    origin = 'projector',
    region = redis.call('HGET', KEYS[2], 'region'),
    account_id = redis.call('HGET', KEYS[2], 'account_id'),
    allocation_version = allocation_version,
    lease_cursor = cursor,
    state = 'resyncing'
}))
return 1
"""


_FINALIZE_CAPACITY_PROJECTION_LUA = r"""
local function owner_valid(owner, region)
    if type(owner) ~= 'string'
       or string.sub(owner, 1, string.len(region) + 1) ~= region .. ':' then
        return false
    end
    local first, second, third, fourth, fifth = string.match(
        string.sub(owner, string.len(region) + 2),
        '^([0-9a-f]+)%-([0-9a-f]+)%-([0-9a-f]+)%-([0-9a-f]+)%-([0-9a-f]+)$'
    )
    return first ~= nil and #first == 8 and #second == 4 and #third == 4
        and #fourth == 4 and #fifth == 12
end

local function snapshot_valid(
    leases_key, identities_key, account_id, region, allocation_version,
    occupancy, payload
)
    local ok, expected = pcall(cjson.decode, payload)
    if not ok or type(expected) ~= 'table' or #expected ~= occupancy * 3 then
        return false
    end
    local actual = redis.call('ZRANGE', leases_key, 0, -1, 'WITHSCORES')
    if #actual ~= occupancy * 2 or redis.call('HLEN', identities_key) ~= occupancy then
        return false
    end
    for actual_index = 1, #actual, 2 do
        local expected_index = ((actual_index - 1) / 2) * 3 + 1
        local member = actual[actual_index]
        if member ~= expected[expected_index]
           or tonumber(actual[actual_index + 1])
              ~= tonumber(expected[expected_index + 1]) then
            return false
        end
        local raw = redis.call('HGET', identities_key, member)
        if raw == false or raw ~= expected[expected_index + 2] then
            return false
        end
        local decoded, identity = pcall(cjson.decode, raw)
        if not decoded or type(identity) ~= 'table'
           or identity.schema_version ~= 1
           or identity.attempt_id ~= member
           or tostring(identity.account_id) ~= account_id
           or identity.region ~= region
           or tostring(identity.allocation_version) ~= allocation_version
           or type(identity.lease_id) ~= 'string' or identity.lease_id == ''
           or type(identity.request_id) ~= 'string' or identity.request_id == ''
           or not owner_valid(identity.dispatch_owner_id, region) then
            return false
        end
    end
    return true
end

local allocation_version = ARGV[1]
local quota = ARGV[2]
local restart_epoch = ARGV[3]
local cursor = ARGV[4]
local occupancy = tonumber(ARGV[5])
local digest = ARGV[6]
local expected_receipt_version = ARGV[7]
local receipt_version = ARGV[8]
local state = ARGV[9]
local channel = ARGV[10]
local leases_payload = ARGV[11]
local account_id = redis.call('HGET', KEYS[2], 'account_id')
local region = redis.call('HGET', KEYS[2], 'region')
local expected_version_number = tonumber(expected_receipt_version)
local version_number = tonumber(receipt_version)
if occupancy == nil or expected_version_number == nil or version_number == nil
   or version_number ~= expected_version_number + 1 then
    return 0
end
if redis.call('HGET', KEYS[2], 'state') ~= 'resyncing'
   or redis.call('HGET', KEYS[2], 'receipt_dirty') ~= '0'
   or redis.call('HGET', KEYS[2], 'allocation_version') ~= allocation_version
   or redis.call('HGET', KEYS[2], 'quota') ~= quota
   or redis.call('HGET', KEYS[2], 'restart_epoch') ~= restart_epoch
   or redis.call('HGET', KEYS[2], 'reconciled_restart_epoch') ~= restart_epoch
   or redis.call('HGET', KEYS[2], 'lease_cursor') ~= cursor
   or redis.call('HGET', KEYS[2], 'occupancy') ~= ARGV[5]
   or redis.call('HGET', KEYS[2], 'occupancy_digest') ~= digest
   or redis.call('HGET', KEYS[2], 'receipt_version') ~= expected_receipt_version
   or account_id == false
   or region == false
   or not snapshot_valid(
       KEYS[1], KEYS[3], account_id, region, allocation_version,
       occupancy, leases_payload
   ) then
    return 0
end
redis.call('HSET', KEYS[2],
    'state', state,
    'receipt_version', receipt_version,
    'receipt_dirty', 0)
redis.call('PUBLISH', channel, cjson.encode({
    schema_version = 1,
    hint = 'exact_reread',
    projection_type = 'capacity',
    origin = 'projector',
    region = redis.call('HGET', KEYS[2], 'region'),
    account_id = account_id,
    allocation_version = allocation_version,
    lease_cursor = cursor,
    state = state
}))
return 1
"""
