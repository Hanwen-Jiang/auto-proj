from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

_ADVISORY_LOCK_NAMESPACE = 0x41365050
_DEFAULT_RETENTION_DAYS = 180
_DEFAULT_BATCH_SIZE = 100
_MAX_BATCH_SIZE = 1000


class ProviderGenerationRetentionError(RuntimeError):
    pass


@dataclass(frozen=True)
class ProviderGenerationRetentionConfig:
    model: str
    retention_days: int = _DEFAULT_RETENTION_DAYS
    batch_size: int = _DEFAULT_BATCH_SIZE

    def __post_init__(self) -> None:
        model = str(self.model).strip()
        if not model:
            raise ProviderGenerationRetentionError("provider_generation_retention_model_invalid")
        if (
            isinstance(self.retention_days, bool)
            or not isinstance(self.retention_days, int)
            or self.retention_days < 1
        ):
            raise ProviderGenerationRetentionError(
                "provider_generation_retention_days_invalid"
            )
        if (
            isinstance(self.batch_size, bool)
            or not isinstance(self.batch_size, int)
            or not 1 <= self.batch_size <= _MAX_BATCH_SIZE
        ):
            raise ProviderGenerationRetentionError(
                "provider_generation_retention_batch_size_invalid"
            )
        object.__setattr__(self, "model", model)


@dataclass(frozen=True)
class ProviderGenerationRetentionCandidate:
    model: str
    routing_generation_id: int
    routing_generation: int
    cost_generation_id: int
    cost_generation: int
    slot_count: int
    routing_updated_at: datetime
    cost_updated_at: datetime

    def summary(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "routing_generation_id": self.routing_generation_id,
            "routing_generation": self.routing_generation,
            "cost_generation_id": self.cost_generation_id,
            "cost_generation": self.cost_generation,
            "slot_count": self.slot_count,
            "routing_updated_at": self.routing_updated_at.isoformat(),
            "cost_updated_at": self.cost_updated_at.isoformat(),
        }


@dataclass(frozen=True)
class ProviderGenerationRetentionResult:
    config: ProviderGenerationRetentionConfig
    cutoff: datetime
    applied: bool
    candidates: tuple[ProviderGenerationRetentionCandidate, ...]
    archived_pairs: int = 0
    deleted_slots: int = 0
    deleted_routing_generations: int = 0
    deleted_cost_generations: int = 0

    def summary(self) -> dict[str, Any]:
        return {
            "success": True,
            "mode": "apply" if self.applied else "dry-run",
            "model": self.config.model,
            "retention_days": self.config.retention_days,
            "batch_size": self.config.batch_size,
            "cutoff": self.cutoff.isoformat(),
            "eligible_pairs": len(self.candidates),
            "archived_pairs": self.archived_pairs,
            "deleted_slots": self.deleted_slots,
            "deleted_routing_generations": self.deleted_routing_generations,
            "deleted_cost_generations": self.deleted_cost_generations,
            "candidates": [candidate.summary() for candidate in self.candidates],
        }


_CANDIDATE_QUERY = """
    SELECT generation.id AS routing_generation_id,
           generation.model,
           generation.generation AS routing_generation,
           cost.id AS cost_generation_id,
           cost.cost_generation,
           generation.updated_at AS routing_updated_at,
           cost.updated_at AS cost_updated_at,
           (
               SELECT count(*)
               FROM a6_trade.provider_pool_slots slot_count
               WHERE slot_count.generation_id = generation.id
           ) AS slot_count
    FROM a6_trade.provider_pool_generations generation
    JOIN a6_trade.provider_cost_generations cost
      ON cost.id = generation.cost_generation_id
    WHERE generation.model = %s
      AND generation.generation_state = 'superseded'
      AND cost.pricing_state = 'superseded'
      AND generation.updated_at < %s
      AND cost.updated_at < %s
      AND NOT EXISTS (
          SELECT 1
          FROM a6_trade.provider_pool_slots current_slot
          WHERE current_slot.generation_id = generation.id
            AND current_slot.valid_to IS NULL
      )
      AND NOT EXISTS (
          SELECT 1
          FROM a6_trade.provider_pool_generations other_generation
          WHERE other_generation.cost_generation_id = cost.id
            AND other_generation.id <> generation.id
      )
      AND NOT EXISTS (
          SELECT 1
          FROM a6_trade.provider_pool_slots other_cost_slot
          WHERE other_cost_slot.cost_generation_id = cost.id
            AND other_cost_slot.generation_id <> generation.id
      )
    ORDER BY GREATEST(generation.updated_at, cost.updated_at),
             generation.generation,
             cost.cost_generation
    LIMIT %s
"""

_ARCHIVE_QUERY = """
    INSERT INTO a6_trade.provider_generation_archives (
        model, routing_generation, cost_generation,
        routing_generation_id, cost_generation_id,
        source_hash, cost_input_hash, cost_output_hash,
        generation_state, pricing_state,
        routing_updated_at, cost_updated_at,
        routing_snapshot, cost_snapshot, slot_snapshot, slot_count,
        retention_cutoff, archive_reason
    )
    SELECT routing.model,
           routing.generation,
           cost.cost_generation,
           routing.id,
           cost.id,
           routing.source_hash,
           cost.cost_input_hash,
           cost.cost_output_hash,
           routing.generation_state,
           cost.pricing_state,
           routing.updated_at,
           cost.updated_at,
           to_jsonb(routing),
           to_jsonb(cost),
           COALESCE(
               (
                   SELECT jsonb_agg(to_jsonb(slot) ORDER BY slot.slot_no, slot.id)
                   FROM a6_trade.provider_pool_slots slot
                   WHERE slot.generation_id = routing.id
               ),
               '[]'::jsonb
           ),
           (
               SELECT count(*)
               FROM a6_trade.provider_pool_slots slot_count
               WHERE slot_count.generation_id = routing.id
           ),
           %s,
           'retention_expired'
    FROM a6_trade.provider_pool_generations routing
    JOIN a6_trade.provider_cost_generations cost
      ON cost.id = routing.cost_generation_id
    WHERE routing.id = %s
      AND cost.id = %s
      AND routing.model = %s
      AND routing.generation = %s
      AND cost.cost_generation = %s
      AND routing.generation_state = 'superseded'
      AND cost.pricing_state = 'superseded'
      AND routing.updated_at < %s
      AND cost.updated_at < %s
      AND NOT EXISTS (
          SELECT 1
          FROM a6_trade.provider_pool_slots current_slot
          WHERE current_slot.generation_id = routing.id
            AND current_slot.valid_to IS NULL
      )
      AND NOT EXISTS (
          SELECT 1
          FROM a6_trade.provider_pool_generations other_generation
          WHERE other_generation.cost_generation_id = cost.id
            AND other_generation.id <> routing.id
      )
      AND NOT EXISTS (
          SELECT 1
          FROM a6_trade.provider_pool_slots other_cost_slot
          WHERE other_cost_slot.cost_generation_id = cost.id
            AND other_cost_slot.generation_id <> routing.id
      )
    RETURNING slot_count
"""


class PostgresProviderGenerationRetention:
    def __init__(self, connection_factory: Callable[[], Any]) -> None:
        if not callable(connection_factory):
            raise ProviderGenerationRetentionError(
                "provider_generation_retention_connection_factory_invalid"
            )
        self.connection_factory = connection_factory

    def cleanup(
        self,
        config: ProviderGenerationRetentionConfig,
        *,
        apply: bool = False,
        now: datetime | None = None,
    ) -> ProviderGenerationRetentionResult:
        if not isinstance(config, ProviderGenerationRetentionConfig):
            raise ProviderGenerationRetentionError(
                "provider_generation_retention_config_invalid"
            )
        if not isinstance(apply, bool):
            raise ProviderGenerationRetentionError(
                "provider_generation_retention_apply_invalid"
            )
        observed_now = _aware_utc(now or datetime.now(timezone.utc))
        cutoff = observed_now - timedelta(days=config.retention_days)
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                if apply:
                    _lock_model(cursor, config.model)
                else:
                    cursor.execute(
                        "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"
                    )
                candidates = _candidates(
                    cursor,
                    config=config,
                    cutoff=cutoff,
                    lock=apply,
                )
                if not apply:
                    connection.commit()
                    return ProviderGenerationRetentionResult(
                        config=config,
                        cutoff=cutoff,
                        applied=False,
                        candidates=candidates,
                    )

                deleted_slots = 0
                for candidate in candidates:
                    deleted_slots += _archive_and_delete(
                        cursor,
                        candidate=candidate,
                        cutoff=cutoff,
                    )
            connection.commit()
            count = len(candidates)
            return ProviderGenerationRetentionResult(
                config=config,
                cutoff=cutoff,
                applied=True,
                candidates=candidates,
                archived_pairs=count,
                deleted_slots=deleted_slots,
                deleted_routing_generations=count,
                deleted_cost_generations=count,
            )
        except Exception:
            connection.rollback()
            raise
        finally:
            _close(connection)


def _candidates(
    cursor: Any,
    *,
    config: ProviderGenerationRetentionConfig,
    cutoff: datetime,
    lock: bool,
) -> tuple[ProviderGenerationRetentionCandidate, ...]:
    lock_clause = " FOR UPDATE OF generation, cost SKIP LOCKED" if lock else ""
    cursor.execute(
        _CANDIDATE_QUERY + lock_clause,
        (config.model, cutoff, cutoff, config.batch_size),
    )
    return tuple(_candidate(row) for row in _rows(cursor))


def _archive_and_delete(
    cursor: Any,
    *,
    candidate: ProviderGenerationRetentionCandidate,
    cutoff: datetime,
) -> int:
    cursor.execute(
        _ARCHIVE_QUERY,
        (
            cutoff,
            candidate.routing_generation_id,
            candidate.cost_generation_id,
            candidate.model,
            candidate.routing_generation,
            candidate.cost_generation,
            cutoff,
            cutoff,
        ),
    )
    archived = _row(cursor)
    if archived is None or cursor.rowcount != 1:
        raise ProviderGenerationRetentionError(
            "provider_generation_archive_pair_changed"
        )
    archived_slot_count = int(archived["slot_count"])
    if archived_slot_count != candidate.slot_count:
        raise ProviderGenerationRetentionError(
            "provider_generation_archive_slot_snapshot_mismatch"
        )

    cursor.execute(
        """
        DELETE FROM a6_trade.provider_pool_slots
        WHERE generation_id = %s
        """,
        (candidate.routing_generation_id,),
    )
    if cursor.rowcount != archived_slot_count:
        raise ProviderGenerationRetentionError(
            "provider_generation_archive_slot_delete_mismatch"
        )

    cursor.execute(
        """
        DELETE FROM a6_trade.provider_pool_generations
        WHERE id = %s
          AND model = %s
          AND generation = %s
          AND cost_generation_id = %s
          AND cost_generation = %s
          AND generation_state = 'superseded'
        """,
        (
            candidate.routing_generation_id,
            candidate.model,
            candidate.routing_generation,
            candidate.cost_generation_id,
            candidate.cost_generation,
        ),
    )
    if cursor.rowcount != 1:
        raise ProviderGenerationRetentionError(
            "provider_generation_archive_routing_delete_mismatch"
        )

    cursor.execute(
        """
        DELETE FROM a6_trade.provider_cost_generations cost
        WHERE cost.id = %s
          AND cost.model = %s
          AND cost.cost_generation = %s
          AND cost.pricing_state = 'superseded'
          AND NOT EXISTS (
              SELECT 1
              FROM a6_trade.provider_pool_generations generation
              WHERE generation.cost_generation_id = cost.id
          )
          AND NOT EXISTS (
              SELECT 1
              FROM a6_trade.provider_pool_slots slot
              WHERE slot.cost_generation_id = cost.id
          )
        """,
        (
            candidate.cost_generation_id,
            candidate.model,
            candidate.cost_generation,
        ),
    )
    if cursor.rowcount != 1:
        raise ProviderGenerationRetentionError(
            "provider_generation_archive_cost_delete_mismatch"
        )
    return archived_slot_count


def _candidate(row: Mapping[str, Any]) -> ProviderGenerationRetentionCandidate:
    return ProviderGenerationRetentionCandidate(
        model=str(row["model"]),
        routing_generation_id=int(row["routing_generation_id"]),
        routing_generation=int(row["routing_generation"]),
        cost_generation_id=int(row["cost_generation_id"]),
        cost_generation=int(row["cost_generation"]),
        slot_count=int(row["slot_count"]),
        routing_updated_at=_aware_utc(row["routing_updated_at"]),
        cost_updated_at=_aware_utc(row["cost_updated_at"]),
    )


def _aware_utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ProviderGenerationRetentionError(
            "provider_generation_retention_datetime_invalid"
        )
    return value.astimezone(timezone.utc)


def _rows(cursor: Any) -> tuple[dict[str, Any], ...]:
    rows = cursor.fetchall()
    if not rows:
        return ()
    if isinstance(rows[0], Mapping):
        return tuple(dict(row) for row in rows)
    names = [column.name for column in cursor.description or ()]
    return tuple(dict(zip(names, row, strict=False)) for row in rows)


def _row(cursor: Any) -> dict[str, Any] | None:
    row = cursor.fetchone()
    if row is None:
        return None
    if isinstance(row, Mapping):
        return dict(row)
    names = [column.name for column in cursor.description or ()]
    return dict(zip(names, row, strict=False))


def _lock_model(cursor: Any, model: str) -> None:
    cursor.execute(
        "SELECT pg_advisory_xact_lock(%s, hashtext(%s))",
        (_ADVISORY_LOCK_NAMESPACE, model),
    )


def _close(connection: Any) -> None:
    close = getattr(connection, "close", None)
    if callable(close):
        close()
