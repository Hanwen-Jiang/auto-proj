from __future__ import annotations

import hashlib
import json
import math
import threading
import time
from collections import Counter
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from .channel_policy import (
    FORBIDDEN_CHANNEL_NAME,
    GENERATED_CHANNEL_NAME_PATTERN,
    GENERATED_CHANNEL_NAME_SIGNAL_PATTERN,
)
from .trade_channel_failure import classify_channel_failure
from .trade_channel_health import ChannelHealthInput, classify_channel_health
from .trade_collector import (
    CollectionBatch,
    provider_usage_storage_key,
)
from .trade_market_traffic import (
    TRAFFIC_WINDOWS,
    build_market_traffic_snapshot,
)

UTC = timezone.utc
USD_MICROS_PER_UNIT = 1_000_000
# Below this share of attributable ledger events, profit is reported as null rather
# than as a number that reads high because unmatched cost is treated as zero.
_MIN_ATTRIBUTION_COVERAGE = Decimal("0.90")
WINDOW_DELTAS: dict[str, timedelta | None] = {
    "5m": timedelta(minutes=5),
    "1h": timedelta(hours=1),
    "5h": timedelta(hours=5),
    "12h": timedelta(hours=12),
    "24h": timedelta(hours=24),
    "7d": timedelta(days=7),
    "30d": timedelta(days=30),
    "mtd": None,
    "all": None,
}


@dataclass(frozen=True)
class EnodotSalesLedger:
    readable: bool
    by_merchant: Mapping[int, Mapping[str, int]]


@dataclass(frozen=True)
class EnodotProviderCostLedger:
    readable: bool
    by_merchant: Mapping[int, Mapping[str, int]]
    unassigned_cost_micros: int = 0
    unassigned_event_count: int = 0


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _since(window: str, now: datetime) -> datetime:
    if window not in WINDOW_DELTAS:
        raise ValueError("invalid_window")
    if window == "all":
        return datetime(1970, 1, 1, tzinfo=UTC)
    if window == "mtd":
        return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    delta = WINDOW_DELTAS[window]
    if delta is None:
        raise ValueError("invalid_window")
    return now - delta


def _observed_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _event_datetime(value: int, fallback: datetime) -> datetime:
    return datetime.fromtimestamp(value, UTC) if value > 0 else fallback


def _hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def _integer(value: Any) -> int:
    if value is None or isinstance(value, bool):
        return 0
    if isinstance(value, Decimal):
        return int(value)
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _datetime_epoch(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, datetime):
        current = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
        return round(current.timestamp())
    if isinstance(value, (int, float, Decimal)):
        return round(float(value))
    try:
        return round(_observed_datetime(str(value)).timestamp())
    except (TypeError, ValueError):
        return None


def _coverage(*parts: tuple[int, int]) -> Decimal | None:
    """Share of ledger events whose money is actually traceable.

    Each ``(total, unknown)`` pair is one unresolved side of the ledger. A sale that
    cannot be tied to a provider request and an earning whose counterparty is still
    unclassified each leave a hole that a profit figure would silently fill with zero.
    Provider usage whose amount is present in the A6 ledger is covered even when its
    request-level Enodot correlation is still pending, because the full cost is charged.

    Returns ``None`` when there is no evidence at all, which is distinct from full
    coverage of nothing.
    """

    total = sum(max(0, count) for count, _ in parts)
    if total <= 0:
        return None
    unknown = sum(max(0, count) for _, count in parts)
    return (Decimal(total - min(unknown, total)) / Decimal(total)).quantize(Decimal("0.0001"))


def _ledger_status(statuses: Sequence[str]) -> str:
    active = {status for status in statuses if status != "not_applicable"}
    if "unavailable" in active:
        return "unavailable"
    if "partial" in active or ({"confirmed", "pending"} <= active):
        return "partial"
    if "pending" in active:
        return "pending"
    if "confirmed" in active:
        return "confirmed"
    return "not_applicable"


def _column_names(cursor: Any) -> list[str]:
    return [
        str(column.name) if getattr(column, "name", None) else str(column[0])
        for column in cursor.description
    ]


def _rows(cursor: Any) -> list[dict[str, Any]]:
    raw = cursor.fetchall()
    if not raw:
        return []
    if isinstance(raw[0], Mapping):
        return [dict(row) for row in raw]
    names = _column_names(cursor)
    return [dict(zip(names, row, strict=True)) for row in raw]


def _row(cursor: Any) -> dict[str, Any] | None:
    raw = cursor.fetchone()
    if raw is None:
        return None
    if isinstance(raw, Mapping):
        return dict(raw)
    names = _column_names(cursor)
    return dict(zip(names, raw, strict=True))


class PostgresTradeRepository:
    """Collector sink and read-only dashboard repository over the Phase 0 schema."""

    def __init__(
        self,
        connection_factory: Callable[[], Any],
        *,
        now: Callable[[], datetime] = _utc_now,
        dashboard_cache_ttl_seconds: float = 10.0,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        ttl = float(dashboard_cache_ttl_seconds)
        if not math.isfinite(ttl) or ttl < 0:
            raise ValueError("dashboard_cache_ttl_invalid")
        self.connection_factory = connection_factory
        self.now = now
        self._dashboard_cache_ttl_seconds = ttl
        self._monotonic = monotonic
        self._merchant_ledger_cache: dict[
            str,
            tuple[float, tuple[list[dict[str, Any]], EnodotProviderCostLedger]],
        ] = {}
        self._market_traffic_cache: dict[tuple[str, str], tuple[float, Mapping[str, Any]]] = {}
        self._merchant_ledger_cache_lock = threading.Lock()
        self._market_traffic_cache_lock = threading.Lock()

    @classmethod
    def from_dsn(cls, dsn: str) -> PostgresTradeRepository:
        def connect() -> Any:
            import psycopg
            from psycopg.rows import dict_row

            return psycopg.connect(dsn, row_factory=dict_row)

        return cls(connect)

    @contextmanager
    def _connection(self) -> Iterator[Any]:
        connection = self.connection_factory()
        try:
            yield connection
        finally:
            close = getattr(connection, "close", None)
            if callable(close):
                close()

    def health(self) -> Mapping[str, Any]:
        checked_at = self.now()
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    count(*) FILTER (
                        WHERE status = 'supplier_submitted'
                    ) AS merchant_count,
                    max(last_poll_at) FILTER (
                        WHERE status = 'supplier_submitted'
                    ) AS last_success_at,
                    coalesce(sum(poll_error_count) FILTER (
                        WHERE status = 'supplier_submitted'
                    ), 0) AS error_count
                FROM a6_trade.a6_merchants
                WHERE status <> 'abandoned'
                """
            )
            result = _row(cursor) or {}
        merchant_count = _integer(result.get("merchant_count"))
        last_success_at = result.get("last_success_at")
        degraded = bool(_integer(result.get("error_count"))) or (
            merchant_count > 0 and last_success_at is None
        )
        return {
            "status": "degraded" if degraded else "ok",
            "checked_at": checked_at,
            "last_success_at": last_success_at,
        }

    def get_provider_pool(self, model: str) -> Mapping[str, Any] | None:
        """Return active Provider state plus a separately labelled planned preview."""

        normalized_model = str(model).strip()
        if not normalized_model or len(normalized_model) > 96:
            raise ValueError("provider_pool_model_invalid")
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(_PROVIDER_POOL_READ_MODEL_QUERY, (normalized_model,))
            row = _row(cursor)
        if row is None or not bool(row.get("known")):
            return None
        planned_routing = row.get("planned_routing")
        planned_cost = row.get("planned_cost")
        planned_slots = row.get("planned_slots") or []
        preview = None
        if planned_routing is not None or planned_cost is not None or planned_slots:
            preview = {
                "kind": str((planned_routing or {}).get("state") or "planned"),
                "routing": planned_routing,
                "cost": planned_cost,
                "slots": planned_slots,
            }
        return {
            "model": str(row.get("model") or normalized_model),
            "generated_at": row.get("generated_at"),
            "routing": row.get("routing"),
            "cost": row.get("cost"),
            "auto_pool": row.get("auto_pool"),
            "slots": row.get("slots") or [],
            "publication": row.get("publication"),
            "preview": preview,
            "active_quarantines": row.get("active_quarantines") or [],
            "recent_failures": row.get("recent_failures") or [],
            "recent_generation_failure_count": _integer(row.get("recent_generation_failure_count")),
        }

    def get_provider_inventory(self, model: str) -> Mapping[str, Any] | None:
        """Return one bounded database-snapshot view of Provider inventory."""

        normalized_model = str(model).strip()
        if not normalized_model or len(normalized_model) > 96:
            raise ValueError("provider_inventory_model_invalid")
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(_PROVIDER_INVENTORY_READ_MODEL_QUERY, (normalized_model,))
            row = _row(cursor)
        if row is None or not bool(row.get("known")):
            return None
        return {
            "model": str(row.get("model") or normalized_model),
            "generated_at": row.get("generated_at"),
            "items": row.get("items") or [],
        }

    def get_economic_priority1(self, model: str) -> Mapping[str, Any] | None:
        """Return the auditable Priority 1 economic plan for one model."""

        normalized_model = str(model).strip()
        if not normalized_model or len(normalized_model) > 96:
            raise ValueError("economic_priority1_model_invalid")
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(_ECONOMIC_PRIORITY1_READ_MODEL_QUERY, (normalized_model,))
            row = _row(cursor)
        if row is None or not bool(row.get("known")):
            return None
        return {
            "model": str(row.get("model") or normalized_model),
            "generated_at": row.get("generated_at"),
            "profit_setting": row.get("profit_setting"),
            "generation": row.get("generation"),
            "preview": row.get("preview"),
            "projection": row.get("projection"),
            "eligibility_states": row.get("eligibility_states") or {},
            "sources": row.get("sources") or [],
            "channels": row.get("channels") or [],
            "summary": row.get("summary") or {},
        }

    def get_market_traffic(self, window: str, model: str) -> Mapping[str, Any]:
        if window not in TRAFFIC_WINDOWS:
            raise ValueError("market_traffic_window_invalid")
        key = (window, model)
        if self._dashboard_cache_ttl_seconds <= 0:
            return self._load_market_traffic(window, model)
        with self._market_traffic_cache_lock:
            monotonic_now = self._monotonic()
            cached = self._market_traffic_cache.get(key)
            if cached is not None and cached[0] > monotonic_now:
                return cached[1]
            snapshot = self._load_market_traffic(window, model)
            expires_at = self._monotonic() + self._dashboard_cache_ttl_seconds
            self._market_traffic_cache = {
                cache_key: entry
                for cache_key, entry in self._market_traffic_cache.items()
                if entry[0] > monotonic_now
            }
            self._market_traffic_cache[key] = (expires_at, snapshot)
            return snapshot

    def _load_market_traffic(self, window: str, model: str) -> Mapping[str, Any]:
        now = self.now()
        since = now - timedelta(seconds=TRAFFIC_WINDOWS[window])
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT channel_id, listing_id, success_at, first_observed_at,
                       realtime_ratio_cny, is_owned, observer_ids
                FROM a6_trade.a6_market_traffic_events
                WHERE model = %s AND first_observed_at >= %s
                ORDER BY first_observed_at, id
                """,
                (model, since),
            )
            events = _rows(cursor)
            cursor.execute(
                """
                SELECT model, channel_id, realtime_ratio_cny, available,
                       last_observed_at
                FROM a6_trade.a6_market_channel_signals_current
                WHERE model = %s
                ORDER BY realtime_ratio_cny NULLS LAST, channel_id
                """,
                (model,),
            )
            signals = _rows(cursor)
            cursor.execute(
                _CHANNEL_FAILURE_SUMMARY_QUERY,
                (model, since, model),
            )
            channel_failures = _rows(cursor)
            cursor.execute(
                _MANAGED_POSITION_PERFORMANCE_QUERY,
                (
                    USD_MICROS_PER_UNIT,
                    since,
                    model,
                    since,
                    model,
                    model,
                    model,
                    model,
                    model,
                    FORBIDDEN_CHANNEL_NAME,
                    GENERATED_CHANNEL_NAME_PATTERN,
                    GENERATED_CHANNEL_NAME_SIGNAL_PATTERN,
                ),
            )
            positions = _rows(cursor)
            cursor.execute(
                """
                SELECT consecutive_failed_rounds, retry_after, last_error_class,
                       last_failure_at, last_success_at,
                       (retry_after > now()) AS active,
                       greatest(
                           0,
                           ceil(extract(epoch FROM retry_after - now()))
                       )::integer AS remaining_seconds
                FROM a6_trade.a6_market_observer_scan_backoff
                WHERE model = %s
                """,
                (model,),
            )
            observer_backoff = _row(cursor) or {}
        snapshot = build_market_traffic_snapshot(
            events,
            signals,
            model=model,
            window=window,
            now=now,
        )
        failure_documents = [_channel_failure_document(row) for row in channel_failures]
        failures_by_channel = {item["channel_id"]: item for item in failure_documents}
        snapshot["channel_failures"] = failure_documents
        snapshot["managed_positions"] = [
            _managed_position_document(
                {
                    **row,
                    **failures_by_channel.get(_integer(row.get("channel_id")), {}),
                },
                TRAFFIC_WINDOWS[window],
                now=now,
            )
            for row in positions
        ]
        snapshot["observer_backoff"] = observer_backoff
        return snapshot

    def persist_collection(self, batch: CollectionBatch) -> None:
        observed_at = _observed_datetime(batch.snapshot.observed_at)
        snapshot_payload = asdict(batch.snapshot)
        source_hash = _hash(snapshot_payload)
        with self._connection() as connection:
            try:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        SELECT id, last_poll_at
                        FROM a6_trade.a6_merchants
                        WHERE a6_user_id = %s
                        FOR UPDATE
                        """,
                        (batch.snapshot.merchant_user_id,),
                    )
                    merchant = _row(cursor)
                    if merchant is None:
                        raise LookupError("collector_merchant_not_imported")
                    merchant_id = _integer(merchant["id"])
                    token_refs = self._upsert_tokens(cursor, merchant_id, batch)
                    self._upsert_financial(cursor, merchant_id, batch, observed_at, source_hash)
                    self._sync_supplier_profile(cursor, merchant_id, batch, observed_at)
                    last_poll_at = merchant.get("last_poll_at")
                    if last_poll_at is None or last_poll_at <= observed_at:
                        self._sync_supplier_channels(cursor, merchant_id, batch, observed_at)
                        self._sync_supplier_listings(cursor, merchant_id, batch, observed_at)
                    self._insert_earnings(cursor, merchant_id, batch, observed_at)
                    self._insert_provider_usage(
                        cursor,
                        merchant_id,
                        batch,
                        token_refs,
                        observed_at,
                    )
                    self._reconcile_pins(cursor, batch, token_refs, observed_at)
                    cursor.execute(
                        """
                        UPDATE a6_trade.a6_merchants
                        SET supplier_id = COALESCE(%s, supplier_id),
                            quota_per_unit = %s,
                            last_auth_ok_at = %s,
                            last_poll_at = %s,
                            poll_error_count = 0,
                            updated_at = now()
                        WHERE id = %s
                          AND (last_poll_at IS NULL OR last_poll_at <= %s)
                        """,
                        (
                            batch.snapshot.supplier_id or None,
                            batch.snapshot.quota_per_unit,
                            observed_at,
                            observed_at,
                            merchant_id,
                            observed_at,
                        ),
                    )
                    cursor.execute(
                        """
                        UPDATE a6_trade.trade_incidents
                        SET status = 'resolved',
                            resolved_at = %s,
                            updated_at = now()
                        WHERE entity_type = 'merchant'
                          AND entity_id = %s
                          AND incident_key LIKE %s
                          AND status = 'open'
                          AND last_seen_at <= %s
                        """,
                        (
                            observed_at,
                            str(merchant_id),
                            f"collector:{merchant_id}:%",
                            observed_at,
                        ),
                    )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def classify_earning_counterparties(
        self,
        *,
        settle_seconds: int = 900,
        limit: int = 20000,
    ) -> dict[str, int]:
        """Label unclassified earnings by the funding source of the matching request.

        An earning whose ``request_id`` matches one of our own provider requests was
        funded by us, not by an external buyer: ``own_boost`` when the provider token is
        a booster token, otherwise ``own_pin``, which means the Pin own-channel
        exclusion leaked and is an incident rather than expected cost.

        Earnings older than the settle window with no match are ``external`` by
        elimination. Younger ones stay ``unknown`` because the counterparty request may
        be collected in a later pass, and guessing early would misreport revenue.
        """

        if settle_seconds < 0 or limit <= 0:
            raise ValueError("classify_earning_counterparties_bounds_invalid")
        with self._connection() as connection:
            try:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        WITH pending AS (
                            SELECT id, request_id
                            FROM a6_trade.a6_earning_events
                            WHERE counterparty_class = 'unknown'
                              AND occurred_at <= now() - make_interval(secs => %s)
                            ORDER BY occurred_at
                            LIMIT %s
                        ), counterparty AS (
                            SELECT
                                pending.id,
                                max(provider.a6_token_ref_id) AS token_ref_id,
                                bool_or(
                                    booster.a6_token_ref_id IS NOT NULL
                                    AND NOT EXISTS (
                                        SELECT 1
                                        FROM a6_trade.enodot_account_bindings provider_binding
                                        WHERE provider_binding.a6_token_id =
                                                  provider.a6_token_ref_id
                                          AND provider_binding.enodot_account_id IS NOT NULL
                                          AND provider_binding.created_at <= provider.occurred_at
                                    )
                                ) AS is_booster,
                                count(provider.id) AS match_count
                            FROM pending
                            LEFT JOIN a6_trade.a6_provider_usage_events provider
                                ON provider.request_id = pending.request_id
                            LEFT JOIN a6_trade.a6_channel_booster_tokens booster
                                ON booster.a6_token_ref_id = provider.a6_token_ref_id
                            GROUP BY pending.id
                        )
                        UPDATE a6_trade.a6_earning_events event
                        SET counterparty_class = CASE
                                WHEN counterparty.match_count = 0 THEN 'external'
                                WHEN counterparty.is_booster THEN 'own_boost'
                                ELSE 'own_pin'
                            END,
                            counterparty_token_ref_id = counterparty.token_ref_id,
                            classified_at = now()
                        FROM counterparty
                        WHERE event.id = counterparty.id
                        RETURNING event.counterparty_class
                        """,
                        (settle_seconds, limit),
                    )
                    classified = Counter(str(row["counterparty_class"]) for row in _rows(cursor))
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return {
            "external": classified.get("external", 0),
            "own_boost": classified.get("own_boost", 0),
            "own_pin": classified.get("own_pin", 0),
            "classified": sum(classified.values()),
        }

    def record_collection_failure(self, session_ref: str, error_class: str) -> None:
        normalized = str(error_class or "").strip().lower()
        if (
            not normalized
            or len(normalized) > 128
            or any(
                character not in "abcdefghijklmnopqrstuvwxyz0123456789_:-"
                for character in normalized
            )
        ):
            normalized = "collector_error"
        with self._connection() as connection:
            try:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        UPDATE a6_trade.a6_merchants
                        SET poll_error_count = poll_error_count + 1,
                            updated_at = now()
                        WHERE session_ref = %s
                        RETURNING id
                        """,
                        (session_ref,),
                    )
                    merchant = _row(cursor)
                    if merchant is None:
                        return
                    merchant_id = _integer(merchant["id"])
                    incident_key = f"collector:{merchant_id}:{normalized}"
                    cursor.execute(
                        """
                        INSERT INTO a6_trade.trade_incidents (
                            incident_key, incident_class, entity_type, entity_id,
                            severity, status, first_seen_at, last_seen_at
                        ) VALUES (%s, %s, 'merchant', %s, 'warning', 'open', now(), now())
                        ON CONFLICT (incident_key) DO UPDATE SET
                            status = 'open',
                            last_seen_at = now(),
                            occurrence_count = a6_trade.trade_incidents.occurrence_count + 1,
                            resolved_at = NULL,
                            updated_at = now()
                        """,
                        (incident_key, normalized, str(merchant_id)),
                    )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    @staticmethod
    def _upsert_tokens(cursor: Any, merchant_id: int, batch: CollectionBatch) -> dict[int, int]:
        references: dict[int, int] = {}
        for token in batch.tokens:
            cursor.execute(
                """
                INSERT INTO a6_trade.a6_tokens (
                    merchant_id, a6_token_id, token_name, status, last_reconciled_at
                ) VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (merchant_id, a6_token_id) DO UPDATE SET
                    token_name = EXCLUDED.token_name,
                    status = EXCLUDED.status,
                    last_reconciled_at = EXCLUDED.last_reconciled_at,
                    updated_at = now()
                WHERE a6_tokens.last_reconciled_at IS NULL
                   OR a6_tokens.last_reconciled_at <= EXCLUDED.last_reconciled_at
                RETURNING id
                """,
                (
                    merchant_id,
                    token.token_id,
                    token.token_name,
                    token.status,
                    _observed_datetime(token.observed_at),
                ),
            )
            row = _row(cursor)
            if row is None:
                cursor.execute(
                    """
                    SELECT id FROM a6_trade.a6_tokens
                    WHERE merchant_id = %s AND a6_token_id = %s
                    """,
                    (merchant_id, token.token_id),
                )
                row = _row(cursor)
            if row is not None:
                references[token.token_id] = _integer(row["id"])
        return references

    @staticmethod
    def _sync_supplier_profile(
        cursor: Any,
        merchant_id: int,
        batch: CollectionBatch,
        observed_at: datetime,
    ) -> None:
        profile = batch.supplier_profile
        if profile is None:
            return
        cursor.execute(
            """
            UPDATE a6_trade.a6_merchants
            SET supplier_id = COALESCE(%s, supplier_id),
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
                profile.supplier_id or None,
                profile.name,
                profile.description,
                observed_at,
                merchant_id,
                observed_at,
            ),
        )

    @staticmethod
    def _sync_supplier_channels(
        cursor: Any,
        merchant_id: int,
        batch: CollectionBatch,
        observed_at: datetime,
    ) -> None:
        channel_ids: list[int] = []
        for channel in batch.channels:
            channel_ids.append(channel.a6_channel_id)
            cursor.execute(
                """
                INSERT INTO a6_trade.a6_supplier_channels_current (
                    merchant_id, a6_channel_id, supplier_id, name, nickname,
                    channel_type, status, availability, observed_at, source_hash
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (merchant_id, a6_channel_id) DO UPDATE SET
                    supplier_id = EXCLUDED.supplier_id,
                    name = EXCLUDED.name,
                    nickname = EXCLUDED.nickname,
                    channel_type = EXCLUDED.channel_type,
                    status = EXCLUDED.status,
                    availability = EXCLUDED.availability,
                    observed_at = EXCLUDED.observed_at,
                    source_hash = EXCLUDED.source_hash,
                    updated_at = now()
                WHERE a6_supplier_channels_current.observed_at <= EXCLUDED.observed_at
                """,
                (
                    merchant_id,
                    channel.a6_channel_id,
                    channel.supplier_id,
                    channel.name,
                    channel.nickname,
                    channel.channel_type,
                    channel.status,
                    channel.availability,
                    _observed_datetime(channel.observed_at),
                    channel.source_hash,
                ),
            )
        cursor.execute(
            """
            DELETE FROM a6_trade.a6_supplier_channels_current current_channel
            WHERE current_channel.merchant_id = %s
              AND current_channel.observed_at <= %s
              AND current_channel.a6_channel_id <> ALL(%s::bigint[])
              AND NOT EXISTS (
                  SELECT 1
                  FROM a6_trade.a6_managed_channel_bindings binding
                  WHERE binding.merchant_id = current_channel.merchant_id
                    AND binding.a6_channel_id = current_channel.a6_channel_id
              )
            """,
            (merchant_id, observed_at, channel_ids),
        )

    @staticmethod
    def _sync_supplier_listings(
        cursor: Any,
        merchant_id: int,
        batch: CollectionBatch,
        observed_at: datetime,
    ) -> None:
        listing_ids: list[int] = []
        for listing in batch.listings:
            listing_ids.append(listing.a6_listing_id)
            cursor.execute(
                """
                INSERT INTO a6_trade.a6_supplier_listings_current (
                    merchant_id, a6_listing_id, a6_channel_id,
                    own_model, platform_model, charge_type,
                    input_price_micros, output_price_micros,
                    cache_read_price_micros, cache_write_price_micros,
                    status, availability, visibility, display_name, description,
                    observed_at, source_hash
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s
                )
                ON CONFLICT (merchant_id, a6_listing_id) DO UPDATE SET
                    a6_channel_id = EXCLUDED.a6_channel_id,
                    own_model = EXCLUDED.own_model,
                    platform_model = EXCLUDED.platform_model,
                    charge_type = EXCLUDED.charge_type,
                    input_price_micros = EXCLUDED.input_price_micros,
                    output_price_micros = EXCLUDED.output_price_micros,
                    cache_read_price_micros = EXCLUDED.cache_read_price_micros,
                    cache_write_price_micros = EXCLUDED.cache_write_price_micros,
                    status = EXCLUDED.status,
                    availability = EXCLUDED.availability,
                    visibility = EXCLUDED.visibility,
                    display_name = EXCLUDED.display_name,
                    description = EXCLUDED.description,
                    observed_at = EXCLUDED.observed_at,
                    source_hash = EXCLUDED.source_hash,
                    updated_at = now()
                WHERE a6_supplier_listings_current.observed_at <= EXCLUDED.observed_at
                """,
                (
                    merchant_id,
                    listing.a6_listing_id,
                    listing.a6_channel_id,
                    listing.own_model,
                    listing.platform_model,
                    listing.charge_type,
                    listing.input_price_micros,
                    listing.output_price_micros,
                    listing.cache_read_price_micros,
                    listing.cache_write_price_micros,
                    listing.status,
                    listing.availability,
                    listing.visibility,
                    listing.display_name,
                    listing.description,
                    _observed_datetime(listing.observed_at),
                    listing.source_hash,
                ),
            )
        cursor.execute(
            """
            DELETE FROM a6_trade.a6_supplier_listings_current current_listing
            WHERE current_listing.merchant_id = %s
              AND current_listing.observed_at <= %s
              AND current_listing.a6_listing_id <> ALL(%s::bigint[])
              AND NOT EXISTS (
                  SELECT 1
                  FROM a6_trade.a6_managed_listing_bindings binding
                  WHERE binding.merchant_id = current_listing.merchant_id
                    AND binding.a6_listing_id = current_listing.a6_listing_id
              )
            """,
            (merchant_id, observed_at, listing_ids),
        )

    @staticmethod
    def _upsert_financial(
        cursor: Any,
        merchant_id: int,
        batch: CollectionBatch,
        observed_at: datetime,
        source_hash: str,
    ) -> None:
        snapshot = batch.snapshot
        values = (
            snapshot.user_balance_quota,
            snapshot.used_quota,
            snapshot.request_count,
            snapshot.withdrawable_micros,
            snapshot.withdrawing_micros,
            snapshot.lifetime_earned_micros,
            snapshot.paid_micros,
            snapshot.today_earned_micros,
            snapshot.month_earned_micros,
            snapshot.today_calls,
            snapshot.month_calls,
            snapshot.settlement_pending_micros,
            snapshot.settlement_settled_micros,
            snapshot.settlement_total_income_micros,
            snapshot.settlement_week_new_micros,
            snapshot.settlement_abnormal_count,
            snapshot.markup_percent,
        )
        cursor.execute(
            """
            INSERT INTO a6_trade.a6_financial_current (
                merchant_id, user_balance_quota, used_quota, request_count,
                withdrawable_micros, withdrawing_micros, lifetime_earned_micros,
                paid_micros, today_earned_micros, month_earned_micros,
                today_calls, month_calls, settlement_pending_micros,
                settlement_settled_micros, settlement_total_income_micros,
                settlement_week_new_micros, settlement_abnormal_count,
                markup_percent, observed_at, source_hash
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
            )
            ON CONFLICT (merchant_id) DO UPDATE SET
                user_balance_quota = EXCLUDED.user_balance_quota,
                used_quota = EXCLUDED.used_quota,
                request_count = EXCLUDED.request_count,
                withdrawable_micros = EXCLUDED.withdrawable_micros,
                withdrawing_micros = EXCLUDED.withdrawing_micros,
                lifetime_earned_micros = EXCLUDED.lifetime_earned_micros,
                paid_micros = EXCLUDED.paid_micros,
                today_earned_micros = EXCLUDED.today_earned_micros,
                month_earned_micros = EXCLUDED.month_earned_micros,
                today_calls = EXCLUDED.today_calls,
                month_calls = EXCLUDED.month_calls,
                settlement_pending_micros = EXCLUDED.settlement_pending_micros,
                settlement_settled_micros = EXCLUDED.settlement_settled_micros,
                settlement_total_income_micros = EXCLUDED.settlement_total_income_micros,
                settlement_week_new_micros = EXCLUDED.settlement_week_new_micros,
                settlement_abnormal_count = EXCLUDED.settlement_abnormal_count,
                markup_percent = EXCLUDED.markup_percent,
                observed_at = EXCLUDED.observed_at,
                source_hash = EXCLUDED.source_hash,
                updated_at = now()
            WHERE a6_financial_current.observed_at <= EXCLUDED.observed_at
            """,
            (merchant_id, *values, observed_at, source_hash),
        )
        cursor.execute(
            """
            INSERT INTO a6_trade.a6_financial_snapshots (
                merchant_id, observed_bucket_5m, observed_at,
                user_balance_quota, used_quota, request_count,
                withdrawable_micros, withdrawing_micros, lifetime_earned_micros,
                paid_micros, today_earned_micros, month_earned_micros,
                today_calls, month_calls, settlement_pending_micros,
                settlement_settled_micros, settlement_total_income_micros,
                settlement_week_new_micros, settlement_abnormal_count,
                markup_percent, source_hash
            ) VALUES (
                %s, date_bin('5 minutes', %s::timestamptz, 'epoch'::timestamptz), %s,
                %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s, %s, %s, %s
            )
            ON CONFLICT (merchant_id, observed_bucket_5m) DO UPDATE SET
                observed_at = EXCLUDED.observed_at,
                user_balance_quota = EXCLUDED.user_balance_quota,
                used_quota = EXCLUDED.used_quota,
                request_count = EXCLUDED.request_count,
                withdrawable_micros = EXCLUDED.withdrawable_micros,
                withdrawing_micros = EXCLUDED.withdrawing_micros,
                lifetime_earned_micros = EXCLUDED.lifetime_earned_micros,
                paid_micros = EXCLUDED.paid_micros,
                today_earned_micros = EXCLUDED.today_earned_micros,
                month_earned_micros = EXCLUDED.month_earned_micros,
                today_calls = EXCLUDED.today_calls,
                month_calls = EXCLUDED.month_calls,
                settlement_pending_micros = EXCLUDED.settlement_pending_micros,
                settlement_settled_micros = EXCLUDED.settlement_settled_micros,
                settlement_total_income_micros = EXCLUDED.settlement_total_income_micros,
                settlement_week_new_micros = EXCLUDED.settlement_week_new_micros,
                settlement_abnormal_count = EXCLUDED.settlement_abnormal_count,
                markup_percent = EXCLUDED.markup_percent,
                source_hash = EXCLUDED.source_hash
            WHERE a6_financial_snapshots.observed_at <= EXCLUDED.observed_at
            """,
            (merchant_id, observed_at, observed_at, *values, source_hash),
        )

    @staticmethod
    def _insert_earnings(
        cursor: Any,
        merchant_id: int,
        batch: CollectionBatch,
        fallback_time: datetime,
    ) -> None:
        for event in batch.earnings:
            payload = asdict(event)
            cursor.execute(
                """
                INSERT INTO a6_trade.a6_earning_events (
                    merchant_id, a6_earning_id, log_id, request_id, channel_id,
                    model, own_model, prompt_tokens, completion_tokens,
                    cache_read_tokens, quota_micros, earned_micros,
                    market_quota_micros, supplier_earn_micros, markup_percent,
                    result_class, elapsed_ms, occurred_at, source_hash
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s, %s
                )
                ON CONFLICT DO NOTHING
                """,
                (
                    merchant_id,
                    str(event.earning_id),
                    str(event.log_id) if event.log_id is not None else None,
                    event.request_id or None,
                    event.channel_id,
                    event.platform_model,
                    event.own_model,
                    event.prompt_tokens,
                    event.completion_tokens,
                    event.cache_tokens,
                    event.quota_micros,
                    event.earned_micros,
                    event.market_quota_micros,
                    event.supplier_earn_micros,
                    event.markup_percent,
                    event.result,
                    event.use_time_seconds * 1000,
                    _event_datetime(event.created_at, fallback_time),
                    _hash(payload),
                ),
            )

    @staticmethod
    def _insert_provider_usage(
        cursor: Any,
        merchant_id: int,
        batch: CollectionBatch,
        token_refs: Mapping[int, int],
        fallback_time: datetime,
    ) -> None:
        for event in batch.provider_usage:
            payload = asdict(event)
            request_id = event.request_id or None
            source_hash = _hash(payload)
            occurred_at = _event_datetime(event.created_at, fallback_time)
            result_class = str(event.result_type)
            elapsed_ms = event.use_time_seconds * 1000
            cursor.execute(
                """
                INSERT INTO a6_trade.a6_provider_usage_events (
                    merchant_id, a6_log_id, request_id, a6_token_ref_id,
                    external_a6_token_id, model, channel_id, quota_micros,
                    prompt_tokens, completion_tokens, route_type, result_class,
                    elapsed_ms, occurred_at, source_hash
                ) SELECT
                    %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s
                WHERE NOT EXISTS (
                    SELECT 1
                    FROM a6_trade.a6_provider_usage_events existing
                    WHERE existing.merchant_id = %s
                      AND (
                          (%s::text IS NOT NULL AND existing.request_id = %s)
                          OR (
                              %s::text IS NULL AND existing.request_id IS NULL
                              AND ROW(
                                  existing.external_a6_token_id, existing.model,
                                  existing.channel_id, existing.quota_micros,
                                  existing.prompt_tokens, existing.completion_tokens,
                                  existing.route_type, existing.result_class,
                                  existing.elapsed_ms, existing.occurred_at
                              ) IS NOT DISTINCT FROM ROW(
                                  %s, %s, %s, %s, %s,
                                  %s, %s, %s, %s, %s
                              )
                          )
                      )
                )
                ON CONFLICT DO NOTHING
                """,
                (
                    merchant_id,
                    provider_usage_storage_key(event),
                    request_id,
                    token_refs.get(event.token_id or 0),
                    event.token_id,
                    event.model_name,
                    event.channel_id,
                    event.quota,
                    event.prompt_tokens,
                    event.completion_tokens,
                    event.route_type,
                    result_class,
                    elapsed_ms,
                    occurred_at,
                    source_hash,
                    merchant_id,
                    request_id,
                    request_id,
                    request_id,
                    event.token_id,
                    event.model_name,
                    event.channel_id,
                    event.quota,
                    event.prompt_tokens,
                    event.completion_tokens,
                    event.route_type,
                    result_class,
                    elapsed_ms,
                    occurred_at,
                ),
            )

    @staticmethod
    def _reconcile_pins(
        cursor: Any,
        batch: CollectionBatch,
        token_refs: Mapping[int, int],
        observed_at: datetime,
    ) -> None:
        for pin in batch.pins:
            token_ref = token_refs.get(pin.token_id or 0)
            if not token_ref or not pin.model_name or not pin.channel_id:
                continue
            cursor.execute(
                """
                UPDATE a6_trade.a6_pin_bindings
                SET valid_to = %s
                WHERE a6_token_id = %s AND model = %s AND valid_to IS NULL
                  AND channel_id <> %s
                  AND valid_from < %s
                """,
                (observed_at, token_ref, pin.model_name, pin.channel_id, observed_at),
            )
            cursor.execute(
                """
                INSERT INTO a6_trade.a6_pin_bindings (
                    a6_token_id, model, channel_id, fallback_to_smart_routing,
                    valid_from, confirmed_at, change_reason, decision_id
                )
                SELECT %s, %s, %s, %s, %s, %s, 'collector_observed', (
                    SELECT decision.id
                    FROM a6_trade.a6_pin_decisions decision
                    WHERE decision.a6_token_id = %s
                      AND decision.model = %s
                      AND decision.selected_channel_id = %s
                      AND decision.decision_status = 'completed'
                      AND decision.decided_at <= %s
                    ORDER BY decision.decided_at DESC, decision.id DESC
                    LIMIT 1
                )
                WHERE NOT EXISTS (
                    SELECT 1 FROM a6_trade.a6_pin_bindings
                    WHERE a6_token_id = %s AND model = %s AND valid_to IS NULL
                )
                """,
                (
                    token_ref,
                    pin.model_name,
                    pin.channel_id,
                    pin.fallback_to_smart_routing,
                    observed_at,
                    observed_at,
                    token_ref,
                    pin.model_name,
                    pin.channel_id,
                    observed_at,
                    token_ref,
                    pin.model_name,
                ),
            )
            cursor.execute(
                """
                UPDATE a6_trade.a6_pin_bindings
                SET confirmed_at = coalesce(confirmed_at, %s),
                    decision_id = coalesce(decision_id, (
                        SELECT decision.id
                        FROM a6_trade.a6_pin_decisions decision
                        WHERE decision.a6_token_id = %s
                          AND decision.model = %s
                          AND decision.selected_channel_id = %s
                          AND decision.decision_status = 'completed'
                          AND decision.decided_at <= %s
                        ORDER BY decision.decided_at DESC, decision.id DESC
                        LIMIT 1
                    ))
                WHERE a6_token_id = %s AND model = %s AND channel_id = %s
                  AND valid_to IS NULL
                  AND (confirmed_at IS NULL OR decision_id IS NULL)
                """,
                (
                    observed_at,
                    token_ref,
                    pin.model_name,
                    pin.channel_id,
                    observed_at,
                    token_ref,
                    pin.model_name,
                    pin.channel_id,
                ),
            )

    def get_merchants(self, window: str) -> Sequence[Mapping[str, Any]]:
        merchants, _provider_cost_ledger = self._get_merchant_ledgers(window)
        return merchants

    def get_merchant_session_binding(self, merchant_id: int) -> Mapping[str, Any] | None:
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT id AS merchant_id, a6_user_id, supplier_id,
                       session_ref, session_fingerprint
                FROM a6_trade.a6_merchants
                WHERE id = %s AND status <> 'abandoned'
                """,
                (merchant_id,),
            )
            return _row(cursor)

    def _get_merchant_ledgers(
        self,
        window: str,
    ) -> tuple[list[dict[str, Any]], EnodotProviderCostLedger]:
        if self._dashboard_cache_ttl_seconds <= 0:
            return self._load_merchant_ledgers(window)
        with self._merchant_ledger_cache_lock:
            monotonic_now = self._monotonic()
            cached = self._merchant_ledger_cache.get(window)
            if cached is not None and cached[0] > monotonic_now:
                return cached[1]
            ledgers = self._load_merchant_ledgers(window)
            expires_at = self._monotonic() + self._dashboard_cache_ttl_seconds
            self._merchant_ledger_cache = {
                cache_window: entry
                for cache_window, entry in self._merchant_ledger_cache.items()
                if entry[0] > monotonic_now
            }
            self._merchant_ledger_cache[window] = (expires_at, ledgers)
            return ledgers

    def _load_merchant_ledgers(
        self,
        window: str,
    ) -> tuple[list[dict[str, Any]], EnodotProviderCostLedger]:
        now = self.now()
        since = _since(window, now)
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                _MERCHANT_QUERY,
                (
                    USD_MICROS_PER_UNIT,
                    since,
                    since,
                    since,
                    since,
                    since,
                    since,
                    since,
                    FORBIDDEN_CHANNEL_NAME,
                    GENERATED_CHANNEL_NAME_PATTERN,
                    GENERATED_CHANNEL_NAME_SIGNAL_PATTERN,
                ),
            )
            rows = _rows(cursor)
            sales_ledger = self._enodot_sales_ledger(cursor, since)
            provider_cost_ledger = self._enodot_provider_cost_ledger(
                cursor,
                since,
                readable=sales_ledger.readable,
            )
        normalized: list[dict[str, Any]] = []
        for row in rows:
            merchant_id = _integer(row.get("merchant_id"))
            sales_entry = sales_ledger.by_merchant.get(merchant_id, {})
            provider_cost_entry = provider_cost_ledger.by_merchant.get(merchant_id, {})
            external_sales_fields = (
                "external_enodot_revenue_micros",
                "external_enodot_request_count",
                "confirmed_external_enodot_revenue_micros",
                "confirmed_external_enodot_request_count",
                "unattributed_external_enodot_revenue_micros",
                "unattributed_external_enodot_request_count",
                "external_provider_cost_micros",
                "external_provider_event_count",
                "managed_internal_provider_cost_micros",
                "managed_internal_provider_event_count",
                "owner_internal_debit_micros",
                "owner_internal_request_count",
                "owner_internal_provider_cost_micros",
                "owner_internal_provider_event_count",
            )
            for field in external_sales_fields:
                row[field] = _integer(sales_entry.get(field))

            # Compatibility aliases. The explicit external/internal fields above are the
            # authoritative accounting contract for new dashboard clients.
            row["enodot_sales_micros"] = row["external_enodot_revenue_micros"]
            row["enodot_sales_request_count"] = row["external_enodot_request_count"]
            row["confirmed_enodot_sales_micros"] = row["confirmed_external_enodot_revenue_micros"]
            row["confirmed_enodot_sales_request_count"] = row[
                "confirmed_external_enodot_request_count"
            ]
            row["unattributed_enodot_sales_micros"] = row[
                "unattributed_external_enodot_revenue_micros"
            ]
            row["unattributed_enodot_sales_request_count"] = row[
                "unattributed_external_enodot_request_count"
            ]
            for field in (
                "enodot_provider_cost_micros",
                "confirmed_enodot_provider_cost_micros",
                "unattributed_enodot_provider_cost_micros",
                "enodot_provider_event_count",
                "unattributed_enodot_provider_event_count",
            ):
                row[field] = _integer(provider_cost_entry.get(field))
            row["managed_internal_debit_micros"] = _integer(
                provider_cost_entry.get("managed_internal_debit_micros")
            )
            row["managed_internal_request_count"] = _integer(
                provider_cost_entry.get("managed_internal_request_count")
            )

            total_provider_cost = _integer(row.get("provider_cost_micros"))
            total_provider_events = _integer(row.get("provider_event_count"))
            matched_provider_cost = min(
                total_provider_cost,
                row["external_provider_cost_micros"]
                + row["managed_internal_provider_cost_micros"]
                + row["owner_internal_provider_cost_micros"],
            )
            matched_provider_events = min(
                total_provider_events,
                row["external_provider_event_count"]
                + row["managed_internal_provider_event_count"]
                + row["owner_internal_provider_event_count"],
            )
            row["confirmed_provider_cost_micros"] = matched_provider_cost
            row["unattributed_provider_cost_micros"] = max(
                0, total_provider_cost - matched_provider_cost
            )
            row["unattributed_provider_event_count"] = max(
                0, total_provider_events - matched_provider_events
            )
            imported_status = str(row.get("session_status") or "")
            row["session_status"] = {
                "supplier_submitted": "healthy",
                "allocated": "unknown",
            }.get(imported_status, "unknown")
            observed = row.get("last_observed_at")
            age = max(0, int((now - observed).total_seconds())) if observed else 0
            sales = _integer(row.get("enodot_sales_micros"))
            confirmed_sales = _integer(row.get("confirmed_enodot_sales_micros"))
            unattributed_sales = _integer(row.get("unattributed_enodot_sales_micros"))
            unattributed_sales_count = _integer(row.get("unattributed_enodot_sales_request_count"))
            provider_cost = total_provider_cost
            # seller_income is already external-only; booster money returning to us is
            # charged as boost_net_loss instead of being credited as revenue.
            seller_income = _integer(row.get("seller_income_micros"))
            probe_loss = _integer(row.get("probe_loss_micros"))
            withdrawal_fee = _integer(row.get("withdrawal_fee_micros"))
            boost_net_loss = _integer(row.get("boost_net_loss_micros"))
            candidate_profit = (
                sales - provider_cost + seller_income - probe_loss - boost_net_loss - withdrawal_fee
            )
            honest_profit = (
                confirmed_sales
                - provider_cost
                + seller_income
                - probe_loss
                - boost_net_loss
                - withdrawal_fee
            )
            attribution_coverage = _coverage(
                (
                    _integer(row.get("enodot_sales_request_count")),
                    unattributed_sales_count,
                ),
                (
                    _integer(row.get("provider_event_count")),
                    0,
                ),
                (
                    _integer(row.get("earning_event_count")),
                    _integer(row.get("unclassified_earning_event_count")),
                ),
            )
            roles = {str(role) for role in row.get("roles") or ()}
            binding_count = _integer(row.get("enodot_account_count"))
            provider_relevant = (
                "provider" in roles
                or binding_count > 0
                or _integer(row.get("provider_cost_micros")) > 0
            )
            pending_binding = "provider" in roles and binding_count == 0
            if not provider_relevant:
                sales_status = "not_applicable"
            elif not sales_ledger.readable:
                sales_status = "unavailable"
            elif binding_count == 0:
                sales_status = "pending"
            else:
                sales_status = "confirmed"
            if sales_status == "unavailable" or (
                not provider_cost_ledger.readable
                and (
                    seller_income
                    or _integer(row.get("active_listing_count"))
                    or _integer(row.get("enodot_provider_cost_micros"))
                )
            ):
                profit_status = "unavailable"
            elif unattributed_sales_count or unattributed_sales:
                profit_status = "partial"
            elif pending_binding:
                profit_status = "partial" if candidate_profit else "pending"
            elif (
                attribution_coverage is not None
                and attribution_coverage < _MIN_ATTRIBUTION_COVERAGE
            ):
                profit_status = "partial"
            else:
                profit_status = "confirmed"
            actual_profit = candidate_profit if profit_status == "confirmed" else None
            profit_margin = (
                Decimal(actual_profit) / Decimal(sales)
                if actual_profit is not None and sales
                else None
            )
            # The lower-bound figure still requires a readable sales ledger, a bound
            # Provider account, and enough settled revenue evidence.
            if (
                sales_status == "unavailable"
                or pending_binding
                or attribution_coverage is None
                or attribution_coverage < _MIN_ATTRIBUTION_COVERAGE
            ):
                honest_profit = None
            cooldown = row.get("cooldown_until")
            status = "pending"
            if cooldown and cooldown > now:
                status = "cooling"
            elif imported_status == "supplier_submitted" and _integer(row.get("poll_error_count")):
                status = "error"
            elif imported_status == "supplier_submitted":
                status = "active"
            freshness = "unknown"
            if observed:
                freshness = "fresh" if age <= 600 else "delayed" if age <= 1800 else "stale"
            normalized.append(
                {
                    **row,
                    "status": status,
                    "freshness": freshness,
                    "age_seconds": age,
                    "error_rate": 0,
                    "sales_status": sales_status,
                    "profit_status": profit_status,
                    "pending_binding": pending_binding,
                    "actual_profit_micros": actual_profit,
                    "honest_profit_micros": honest_profit,
                    "attribution_coverage": attribution_coverage,
                    "profit_margin": profit_margin,
                }
            )
        return normalized, provider_cost_ledger

    @staticmethod
    def _enodot_sales_ledger(cursor: Any, since: datetime) -> EnodotSalesLedger:
        """Read external revenue and uniquely matched real A6 Provider costs."""
        cursor.execute(_USAGE_LOGS_READABLE_QUERY)
        capability = _row(cursor) or {}
        if not capability.get("readable"):
            return EnodotSalesLedger(readable=False, by_merchant={})
        cursor.execute(_ENODOT_SALES_QUERY, (USD_MICROS_PER_UNIT, since))
        entries: dict[int, dict[str, int]] = {}
        for row in _rows(cursor):
            entries[_integer(row.get("merchant_id"))] = {
                field: _integer(row.get(field))
                for field in (
                    "external_enodot_revenue_micros",
                    "external_enodot_request_count",
                    "confirmed_external_enodot_revenue_micros",
                    "confirmed_external_enodot_request_count",
                    "unattributed_external_enodot_revenue_micros",
                    "unattributed_external_enodot_request_count",
                    "external_provider_cost_micros",
                    "external_provider_event_count",
                    "managed_internal_provider_cost_micros",
                    "managed_internal_provider_event_count",
                    "owner_internal_debit_micros",
                    "owner_internal_request_count",
                    "owner_internal_provider_cost_micros",
                    "owner_internal_provider_event_count",
                )
            }
        return EnodotSalesLedger(readable=True, by_merchant=entries)

    @staticmethod
    def _enodot_provider_cost_ledger(
        cursor: Any,
        since: datetime,
        *,
        readable: bool,
    ) -> EnodotProviderCostLedger:
        """Read managed Seller-key debits as internal ledger transfers."""
        if not readable:
            return EnodotProviderCostLedger(readable=False, by_merchant={})
        cursor.execute(
            _ENODOT_PROVIDER_COST_QUERY,
            (USD_MICROS_PER_UNIT, since),
        )
        entries: dict[int, dict[str, int]] = {}
        unassigned_cost = 0
        unassigned_count = 0
        for row in _rows(cursor):
            merchant_id = row.get("merchant_id")
            if merchant_id is None:
                unassigned_cost += _integer(row.get("unattributed_enodot_provider_cost_micros"))
                unassigned_count += _integer(row.get("unattributed_enodot_provider_event_count"))
                continue
            debit = _integer(row.get("managed_internal_debit_micros"))
            requests = _integer(row.get("managed_internal_request_count"))
            entries[_integer(merchant_id)] = {
                "managed_internal_debit_micros": debit,
                "managed_internal_request_count": requests,
                # Compatibility aliases retained for the existing dashboard while the
                # label migrates from "upstream cost" to "internal key debit".
                "enodot_provider_cost_micros": debit,
                "confirmed_enodot_provider_cost_micros": debit,
                "unattributed_enodot_provider_cost_micros": 0,
                "enodot_provider_event_count": requests,
                "unattributed_enodot_provider_event_count": 0,
            }
        return EnodotProviderCostLedger(
            readable=True,
            by_merchant=entries,
            unassigned_cost_micros=unassigned_cost,
            unassigned_event_count=unassigned_count,
        )

    def get_overview(self, window: str) -> Mapping[str, Any]:
        now = self.now()
        since = _since(window, now)
        merchants, provider_cost_ledger = self._get_merchant_ledgers(window)
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(_STOCK_WINDOW_QUERY, (since, now, USD_MICROS_PER_UNIT))
            stock_window = _row(cursor) or {}
            cursor.execute(_PAID_WINDOW_QUERY, (since, now, USD_MICROS_PER_UNIT))
            paid = _integer((_row(cursor) or {}).get("paid_micros"))

        stocks: dict[str, dict[str, int]] = {}
        for name in ("user_balance", "withdrawable", "withdrawing"):
            stocks[name] = {
                "start_micros": _integer(stock_window.get(f"{name}_start")),
                "current_micros": _integer(stock_window.get(f"{name}_current")),
                "change_micros": _integer(stock_window.get(f"{name}_current"))
                - _integer(stock_window.get(f"{name}_start")),
                "min_micros": _integer(stock_window.get(f"{name}_min")),
                "max_micros": _integer(stock_window.get(f"{name}_max")),
            }

        flow_names = (
            "earned_micros",
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
            "earning_event_count",
            "unclassified_earning_event_count",
            "boost_gross_cost_micros",
            "boost_event_count",
            "boost_net_loss_micros",
            "probe_gross_cost_micros",
            "probe_recovery_micros",
            "probe_loss_micros",
            "pin_probe_cost_micros",
            "pin_probe_event_count",
            "withdrawal_fee_micros",
        )
        flows = {name: sum(_integer(item.get(name)) for item in merchants) for name in flow_names}
        flows["enodot_provider_cost_micros"] += provider_cost_ledger.unassigned_cost_micros
        flows["unattributed_enodot_provider_cost_micros"] += (
            provider_cost_ledger.unassigned_cost_micros
        )
        flows["enodot_provider_event_count"] += provider_cost_ledger.unassigned_event_count
        flows["unattributed_enodot_provider_event_count"] += (
            provider_cost_ledger.unassigned_event_count
        )
        flows["paid_micros"] = paid
        sales = flows["enodot_sales_micros"]
        flows["pending_binding_count"] = sum(
            bool(item.get("pending_binding")) for item in merchants
        )
        flows["sales_status"] = _ledger_status(
            [str(item.get("sales_status") or "not_applicable") for item in merchants]
        )
        flows["attribution_coverage"] = _coverage(
            (
                flows["enodot_sales_request_count"],
                flows["unattributed_enodot_sales_request_count"],
            ),
            (
                flows["provider_event_count"] + provider_cost_ledger.unassigned_event_count,
                0,
            ),
            (
                flows["earning_event_count"],
                flows["unclassified_earning_event_count"],
            ),
        )
        portfolio_profit = (
            sales
            - flows["provider_cost_micros"]
            + flows["seller_income_micros"]
            - flows["probe_loss_micros"]
            - flows["boost_net_loss_micros"]
            - flows["withdrawal_fee_micros"]
        )
        honest_portfolio_profit = (
            flows["confirmed_enodot_sales_micros"]
            - flows["provider_cost_micros"]
            + flows["seller_income_micros"]
            - flows["probe_loss_micros"]
            - flows["boost_net_loss_micros"]
            - flows["withdrawal_fee_micros"]
        )
        coverage_confirmed = (
            flows["attribution_coverage"] is not None
            and flows["attribution_coverage"] >= _MIN_ATTRIBUTION_COVERAGE
        )
        if flows["sales_status"] == "unavailable" or not provider_cost_ledger.readable:
            flows["profit_status"] = "unavailable"
        elif not coverage_confirmed:
            flows["profit_status"] = "partial"
        else:
            flows["profit_status"] = "confirmed"
        flows["actual_profit_micros"] = (
            portfolio_profit if flows["profit_status"] == "confirmed" else None
        )
        flows["honest_profit_micros"] = honest_portfolio_profit if coverage_confirmed else None
        flows["profit_margin"] = (
            Decimal(flows["actual_profit_micros"]) / Decimal(sales)
            if flows["actual_profit_micros"] is not None and sales
            else None
        )
        fresh = sum(item.get("freshness") == "fresh" for item in merchants)
        delayed = sum(item.get("freshness") == "delayed" for item in merchants)
        stale = sum(item.get("freshness") == "stale" for item in merchants)
        errors = sum(item.get("status") == "error" for item in merchants)
        observed_values = [item.get("last_observed_at") for item in merchants]
        observed_values = [value for value in observed_values if value]
        last_observed = max(observed_values, default=None)
        age = max(0, int((now - last_observed).total_seconds())) if last_observed else 0
        freshness_status = "fresh"
        if errors:
            freshness_status = "error"
        elif last_observed is None:
            freshness_status = "unknown"
        elif stale:
            freshness_status = "stale"
        elif delayed:
            freshness_status = "delayed"
        return {
            "generated_at": now,
            "merchant_count": len(merchants),
            "stocks": stocks,
            "flows": flows,
            "freshness": {
                "status": freshness_status,
                "last_observed_at": last_observed,
                "age_seconds": age,
                "fresh_count": fresh,
                "stale_count": stale,
                "error_count": errors,
            },
        }


def _managed_position_document(
    row: Mapping[str, Any],
    window_seconds: int,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    current_time = now or _utc_now()
    last_failure_code = str(row.get("last_failure_code") or "")
    last_failure_responsibility = _managed_failure_responsibility(
        row,
        last_failure_code,
    )
    blocked_reason = _managed_position_blocked_reason(row)
    seller_calls = _integer(row.get("seller_call_count"))
    seller_income = _integer(row.get("seller_income_micros"))
    managed_key_calls = _integer(row.get("managed_key_call_count"))
    managed_key_debit = _integer(row.get("managed_key_debit_micros"))
    if seller_calls == 0 and managed_key_calls == 0:
        delta_sample_status = "no_sample"
    elif seller_calls > 0 and managed_key_calls == 0:
        delta_sample_status = "routing_unverified"
    elif seller_calls == managed_key_calls:
        delta_sample_status = "matched_count"
    else:
        delta_sample_status = "partial"
    contribution = (
        seller_income - managed_key_debit if delta_sample_status == "matched_count" else None
    )
    raw_health_state = str(row.get("health_state") or "unknown")
    effective_health_state = classify_channel_health(
        ChannelHealthInput(
            available=bool(row.get("market_available")),
            route_status=str(row.get("route_status") or ""),
            circuit_state=str(row.get("circuit_state") or ""),
            circuit_cooling_until=_datetime_epoch(row.get("circuit_cooling_until")),
            cooldown_state=str(row.get("cooldown_state") or ""),
            cooldown_remaining_seconds=(
                _integer(row.get("cooldown_remaining_seconds"))
                if row.get("cooldown_remaining_seconds") is not None
                else None
            ),
            cooldown_expected_recovery_at=_datetime_epoch(row.get("cooldown_expected_recovery_at")),
            smart_routing_status=str(row.get("smart_routing_status") or ""),
            smart_routing_labels=tuple(row.get("smart_routing_labels") or ()),
            sample_count=(
                _integer(row.get("sample_count")) if row.get("sample_count") is not None else None
            ),
            recent_success_rate=(
                _integer(row.get("recent_success_rate"))
                if row.get("recent_success_rate") is not None
                else None
            ),
            last_success_at=_datetime_epoch(row.get("last_success_at")),
            last_failure_at=_datetime_epoch(row.get("last_failure_at")),
            last_failure_code=last_failure_code,
            last_failure_responsibility=last_failure_responsibility,
        ),
        now_epoch=round(current_time.timestamp()),
        degraded_success_rate=(_integer(row.get("degraded_success_rate")) or 8000),
        idle_seconds=_integer(row.get("idle_seconds")) or 1800,
    )
    return {
        "rank": _integer(row.get("rank")),
        "merchant_id": _integer(row.get("merchant_id")),
        "channel_id": (
            _integer(row.get("channel_id")) if row.get("channel_id") is not None else None
        ),
        "listing_id": (
            _integer(row.get("listing_id")) if row.get("listing_id") is not None else None
        ),
        "channel_name": str(row.get("channel_name") or ""),
        "channel_nickname": str(row.get("channel_nickname") or ""),
        "operational_state": "blocked" if blocked_reason else "active",
        "blocked_reason": blocked_reason or "",
        "merchant_status": str(row.get("merchant_status") or ""),
        "merchant_roles": list(row.get("merchant_roles") or ()),
        "binding_present": bool(row.get("binding_present")),
        "binding_control_state": str(row.get("binding_control_state") or ""),
        "listing_present": bool(row.get("listing_present")),
        "listing_channel_id": (
            _integer(row.get("listing_channel_id"))
            if row.get("listing_channel_id") is not None
            else None
        ),
        "listing_platform_model": str(row.get("listing_platform_model") or ""),
        "listing_status": (
            _integer(row.get("listing_status")) if row.get("listing_status") is not None else None
        ),
        "listing_availability": (
            _integer(row.get("listing_availability"))
            if row.get("listing_availability") is not None
            else None
        ),
        "listing_visibility": (
            _integer(row.get("listing_visibility"))
            if row.get("listing_visibility") is not None
            else None
        ),
        "tier": str(row.get("tier") or ""),
        "price_zone": str(row.get("price_zone") or ""),
        "delta": str(row.get("delta") or "0"),
        "target_final_multiplier": str(row.get("target_final_multiplier") or "0"),
        "panel_multiplier": str(row.get("panel_multiplier") or "0"),
        "current_k_cny": (
            str(row.get("current_k_cny")) if row.get("current_k_cny") is not None else None
        ),
        "market_available": bool(row.get("market_available")),
        "health_state": effective_health_state,
        "raw_health_state": raw_health_state,
        "health_reason": _managed_health_reason(
            effective_health_state,
            raw_health_state,
            last_failure_responsibility,
        ),
        "route_status": str(row.get("route_status") or ""),
        "recent_success_rate": (
            _integer(row.get("recent_success_rate"))
            if row.get("recent_success_rate") is not None
            else None
        ),
        "recent_p50_ms": (
            str(row.get("recent_p50_ms")) if row.get("recent_p50_ms") is not None else None
        ),
        "p50_ttft_ms": (
            str(row.get("p50_ttft_ms")) if row.get("p50_ttft_ms") is not None else None
        ),
        "smart_routing_labels": list(row.get("smart_routing_labels") or ()),
        "health_observed_at": row.get("health_observed_at"),
        "recent_failure_count": _integer(row.get("recent_failure_count")),
        "recent_client_failure_count": _integer(row.get("recent_client_failure_count")),
        "recent_provider_failure_count": _integer(row.get("recent_provider_failure_count")),
        "recent_platform_failure_count": _integer(row.get("recent_platform_failure_count")),
        "recent_unknown_failure_count": _integer(row.get("recent_unknown_failure_count")),
        "last_failure_responsibility": last_failure_responsibility,
        "top_failure_codes": list(row.get("top_failure_codes") or ()),
        "seller_call_count": seller_calls,
        "seller_income_micros": seller_income,
        "seller_income_per_call_micros": (
            round(seller_income / seller_calls) if seller_calls else None
        ),
        "seller_income_per_hour_micros": round(seller_income * 3600 / window_seconds),
        "managed_key_call_count": managed_key_calls,
        "managed_key_debit_micros": managed_key_debit,
        "delta_sample_status": delta_sample_status,
        "estimated_contribution_micros": contribution,
    }


def _managed_position_blocked_reason(row: Mapping[str, Any]) -> str | None:
    roles_raw = row.get("merchant_roles")
    roles = (
        {str(value).strip().lower() for value in roles_raw}
        if isinstance(roles_raw, Sequence) and not isinstance(roles_raw, (str, bytes))
        else set()
    )
    if str(row.get("merchant_status") or "") != "supplier_submitted":
        return "merchant_status_not_supplier_submitted"
    if "seller" not in roles:
        return "merchant_seller_role_missing"
    if not bool(row.get("binding_present")):
        return "binding_missing"
    if str(row.get("binding_control_state") or "") != "verified":
        return "binding_not_verified"
    if not bool(row.get("listing_present")):
        return "listing_missing"
    if _integer(row.get("listing_channel_id")) != _integer(row.get("channel_id")):
        return "listing_channel_mismatch"
    if str(row.get("listing_platform_model") or "") != str(row.get("planned_model") or ""):
        return "listing_model_mismatch"
    if _integer(row.get("listing_status")) != 2:
        return "listing_status_inactive"
    if _integer(row.get("listing_availability")) != 1:
        return "listing_unavailable"
    if _integer(row.get("listing_visibility")) != 1:
        return "listing_hidden"
    return None


def _managed_health_reason(
    effective_state: str,
    raw_state: str,
    last_failure_responsibility: str,
) -> str:
    if effective_state == "cooling":
        return "active_cooling_window"
    if effective_state == "unavailable":
        return "market_unavailable"
    if effective_state == "degraded":
        return f"current_{last_failure_responsibility}_failure"
    if effective_state == "unlabeled":
        return "smart_routing_label_missing"
    if effective_state == "idle":
        return "recent_success_missing"
    if effective_state == "unknown":
        return "failure_responsibility_unconfirmed"
    if raw_state != effective_state:
        return "latest_success_supersedes_stale_failure"
    return "current_success"


def _managed_failure_responsibility(
    row: Mapping[str, Any],
    last_failure_code: str,
) -> str:
    responsibility = str(row.get("last_failure_responsibility") or "").strip().lower()
    if responsibility in {"client", "provider", "platform"}:
        return responsibility
    return classify_channel_failure(last_failure_code)


def _channel_failure_document(row: Mapping[str, Any]) -> dict[str, Any]:
    top_codes = row.get("top_failure_codes")
    if not isinstance(top_codes, Sequence) or isinstance(top_codes, (str, bytes)):
        top_codes = ()
    last_failure_code = str(row.get("last_failure_code") or "")[:128]
    return {
        "channel_id": _integer(row.get("channel_id")),
        "listing_id": _integer(row.get("listing_id")),
        "supplier_id": _integer(row.get("supplier_id")),
        "market_rank": _integer(row.get("market_rank")),
        "health_state": str(row.get("health_state") or "unknown"),
        "available": bool(row.get("available")),
        "recent_success_rate": (
            _integer(row.get("recent_success_rate"))
            if row.get("recent_success_rate") is not None
            else None
        ),
        "last_failure_at": row.get("last_failure_at"),
        "last_failure_code": last_failure_code,
        "last_failure_responsibility": classify_channel_failure(last_failure_code),
        "recent_failure_count": _integer(row.get("recent_failure_count")),
        "recent_client_failure_count": _integer(row.get("recent_client_failure_count")),
        "recent_provider_failure_count": _integer(row.get("recent_provider_failure_count")),
        "recent_platform_failure_count": _integer(row.get("recent_platform_failure_count")),
        "recent_unknown_failure_count": _integer(row.get("recent_unknown_failure_count")),
        "top_failure_codes": [
            {
                "code": str(item.get("code") or "")[:128],
                "responsibility": str(item.get("responsibility") or "unknown"),
                "count": _integer(item.get("count")),
                "last_failure_at": item.get("last_failure_at"),
            }
            for item in top_codes
            if isinstance(item, Mapping)
        ],
    }


_ECONOMIC_PRIORITY1_READ_MODEL_QUERY = """
WITH requested AS (
    SELECT %s::text AS model
), profit_setting AS MATERIALIZED (
    SELECT setting.*
    FROM requested
    JOIN a6_trade.model_profit_settings setting
      ON setting.model = requested.model
), active_generation AS MATERIALIZED (
    SELECT generation.*
    FROM requested
    JOIN a6_trade.sub2api_economic_generations generation
      ON generation.model = requested.model
    WHERE generation.generation_state = 'active'
    ORDER BY generation.economic_generation DESC, generation.generation_id DESC
    LIMIT 1
), planned_generation AS MATERIALIZED (
    SELECT generation.*
    FROM requested
    JOIN a6_trade.sub2api_economic_generations generation
      ON generation.model = requested.model
    WHERE generation.generation_state = 'planned'
    ORDER BY generation.economic_generation DESC, generation.generation_id DESC
    LIMIT 1
), model_sources AS MATERIALIZED (
    SELECT source.*
    FROM requested
    JOIN a6_trade.sub2api_economic_sources source
      ON source.model = requested.model
), current_contracts AS MATERIALIZED (
    SELECT DISTINCT ON (contract.source_id)
           contract.*
    FROM model_sources source
    JOIN a6_trade.sub2api_source_cost_contracts contract
      ON contract.source_id = source.source_id
    ORDER BY contract.source_id,
             (
                 contract.contract_state = 'active'
                 AND contract.effective_at <= clock_timestamp()
                 AND contract.expires_at > clock_timestamp()
             ) DESC,
             contract.effective_at DESC,
             contract.contract_id DESC
), current_quotas AS MATERIALIZED (
    SELECT DISTINCT ON (quota.source_id)
           quota.*
    FROM current_contracts contract
    JOIN a6_trade.sub2api_source_quota_snapshots quota
      ON quota.source_id = contract.source_id
     AND quota.contract_id = contract.contract_id
    ORDER BY quota.source_id, quota.observed_at DESC, quota.snapshot_id DESC
), active_eligibility AS MATERIALIZED (
    SELECT eligibility.*
    FROM active_generation generation
    JOIN a6_trade.sub2api_channel_source_eligibility eligibility
      ON eligibility.generation_id = generation.generation_id
     AND eligibility.model = generation.model
     AND eligibility.economic_generation = generation.economic_generation
), eligibility_source_totals AS (
    SELECT eligibility.source_id,
           min(eligibility.source_k_cny) FILTER (
               WHERE eligibility.source_k_cny IS NOT NULL
           ) AS source_k_cny,
           min(eligibility.minimum_channel_k_cny) FILTER (
               WHERE eligibility.minimum_channel_k_cny IS NOT NULL
           ) AS minimum_channel_k_cny,
           count(*)::bigint AS evaluated_channel_count,
           count(*) FILTER (
               WHERE eligibility.eligibility_state IN ('ELIGIBLE', 'THROTTLED')
           )::bigint AS eligible_channel_count,
           array_agg(eligibility.channel_id ORDER BY eligibility.channel_id)
               AS evaluated_channel_ids,
           array_agg(eligibility.channel_id ORDER BY eligibility.channel_id) FILTER (
               WHERE eligibility.eligibility_state IN ('ELIGIBLE', 'THROTTLED')
           ) AS eligible_channel_ids,
           max(eligibility.required_burn_a_per_second)
               AS required_burn_a_per_second
    FROM active_eligibility eligibility
    GROUP BY eligibility.source_id
), eligibility_source_states AS (
    SELECT states.source_id,
           jsonb_object_agg(states.eligibility_state, states.state_count)
               AS state_counts
    FROM (
        SELECT eligibility.source_id, eligibility.eligibility_state,
               count(*)::bigint AS state_count
        FROM active_eligibility eligibility
        GROUP BY eligibility.source_id, eligibility.eligibility_state
    ) states
    GROUP BY states.source_id
), source_attempts AS (
    SELECT attempt.source_id,
           count(*)::bigint AS attempt_count,
           count(*) FILTER (WHERE attempt.finished_at IS NOT NULL)::bigint
               AS finished_attempt_count,
           count(*) FILTER (WHERE attempt.realized_profit_cny < 0)::bigint
               AS negative_profit_attempt_count,
           coalesce(sum(attempt.realized_a), 0) AS realized_a,
           coalesce(sum(attempt.realized_cost_cny), 0) AS realized_cost_cny,
           coalesce(sum(attempt.realized_revenue_cny), 0) AS realized_revenue_cny,
           coalesce(sum(attempt.realized_profit_cny), 0) AS realized_profit_cny,
           coalesce(sum(attempt.realized_a) FILTER (
               WHERE attempt.finished_at >= clock_timestamp() - interval '1 hour'
           ), 0) AS realized_a_1h,
           max(attempt.finished_at) AS last_finished_at
    FROM requested
    JOIN a6_trade.sub2api_economic_attempts attempt
      ON attempt.model = requested.model
    GROUP BY attempt.source_id
), source_runtime_usage AS (
    SELECT source.source_id,
           count(usage.id)::bigint AS request_count_1h,
           coalesce(sum(usage.total_cost), 0) AS consumed_a_1h,
           max(usage.created_at) AS last_used_at,
           coalesce(
               array_agg(DISTINCT usage.group_id ORDER BY usage.group_id) FILTER (
                   WHERE usage.group_id IS NOT NULL
               ),
               '{}'::bigint[]
           ) AS group_ids
    FROM model_sources source
    LEFT JOIN public.usage_logs usage
      ON usage.account_id = source.enodot_account_id
     AND usage.model = source.model
     AND usage.created_at >= clock_timestamp() - interval '1 hour'
    GROUP BY source.source_id
), source_rows AS (
    SELECT source.source_id,
           jsonb_build_object(
               'source_id', source.source_id,
               'source_key', source.source_key,
               'source_kind', source.source_kind,
               'origin_class', source.origin_class,
               'account_id', source.enodot_account_id,
               'enabled', source.enabled,
               'health_state', source.health_state,
               'health_observed_at', source.health_observed_at,
               'capacity_state', source.capacity_state,
               'capacity_observed_at', source.capacity_observed_at,
               'capacity_epoch', source.capacity_epoch,
               'configured_reserve_a', source.configured_reserve_a::text,
               'config_version', source.config_version,
               'contract', CASE WHEN contract.contract_id IS NULL THEN NULL ELSE
                   jsonb_build_object(
                       'contract_id', contract.contract_id,
                       'cost_mode', contract.cost_mode,
                       'cycle_cost_cny', contract.cycle_cost_cny::text,
                       'declared_k_cny', contract.declared_k_cny::text,
                       'currency', contract.currency,
                       'cost_basis', contract.cost_basis,
                       'quota_basis', contract.quota_basis,
                       'effective_at', contract.effective_at,
                       'expires_at', contract.expires_at,
                       'config_version', contract.config_version,
                       'source_hash', contract.source_hash,
                       'state', contract.contract_state
                   ) END,
               'quota', CASE WHEN quota.snapshot_id IS NULL THEN NULL ELSE
                   jsonb_build_object(
                       'snapshot_id', quota.snapshot_id,
                       'quota_epoch', quota.quota_epoch,
                       'used_a', quota.used_a::text,
                       'remaining_a', quota.remaining_a::text,
                       'total_a', quota.total_a::text,
                       'reset_at', quota.reset_at,
                       'observed_at', quota.observed_at,
                       'payload_hash', quota.payload_hash,
                       'state', quota.snapshot_state,
                       'estimate_state', upper(coalesce(
                           quota.source_payload #>> '{source_payload,estimate_state}',
                           ''
                       )),
                       'estimate_method', coalesce(
                           quota.source_payload #>> '{source_payload,estimate_method}',
                           ''
                       ),
                       'evidence_point_count', coalesce(
                           quota.source_payload #>> '{source_payload,evidence_point_count}',
                           '0'
                       ),
                       'distinct_level_count', coalesce(
                           quota.source_payload #>> '{source_payload,distinct_level_count}',
                           '0'
                       ),
                       'slope_count', coalesce(
                           quota.source_payload #>> '{source_payload,slope_count}',
                           '0'
                       ),
                       'relative_mad', quota.source_payload
                           #>> '{source_payload,relative_mad}',
                       'raw_utilization_percent', quota.source_payload
                           #>> '{source_payload,raw_utilization_percent}',
                       'raw_window_cost_a', quota.source_payload
                           #>> '{source_payload,raw_window_cost_a}',
                       'upstream_updated_at', quota.source_payload
                           #>> '{source_payload,upstream_updated_at}'
                   ) END,
               'source_k_cny', coalesce(
                   eligibility.source_k_cny,
                   CASE
                       WHEN source.source_kind = 'api'
                            AND contract.cost_mode = 'declared_k'
                       THEN contract.declared_k_cny
                       WHEN source.source_kind = 'account'
                            AND contract.cost_mode = 'weekly_amortized'
                            AND quota.total_a > 0
                       THEN ceil(
                           (contract.cycle_cost_cny / quota.total_a) / 0.000001
                       ) * 0.000001
                       ELSE NULL
                   END
               )::text,
               'minimum_channel_k_cny', eligibility.minimum_channel_k_cny::text,
               'evaluated_channel_count', coalesce(
                   eligibility.evaluated_channel_count, 0
               ),
               'eligible_channel_count', coalesce(
                   eligibility.eligible_channel_count, 0
               ),
               'evaluated_channel_ids', coalesce(
                   eligibility.evaluated_channel_ids, '{}'::bigint[]
               ),
               'eligible_channel_ids', coalesce(
                   eligibility.eligible_channel_ids, '{}'::bigint[]
               ),
               'required_burn_a_per_second', coalesce(
                   eligibility.required_burn_a_per_second, 0
               )::text,
               'eligibility_states', coalesce(states.state_counts, '{}'::jsonb),
               'attempts', jsonb_build_object(
                   'attempt_count', coalesce(attempts.attempt_count, 0),
                   'finished_attempt_count', coalesce(
                       attempts.finished_attempt_count, 0
                   ),
                   'negative_profit_attempt_count', coalesce(
                       attempts.negative_profit_attempt_count, 0
                   ),
                   'realized_a', coalesce(attempts.realized_a, 0)::text,
                   'realized_cost_cny', coalesce(
                       attempts.realized_cost_cny, 0
                   )::text,
                   'realized_revenue_cny', coalesce(
                       attempts.realized_revenue_cny, 0
                   )::text,
                   'realized_profit_cny', coalesce(
                       attempts.realized_profit_cny, 0
                   )::text,
                   'realized_a_1h', coalesce(attempts.realized_a_1h, 0)::text,
                   'realized_a_per_second_1h', (
                       coalesce(attempts.realized_a_1h, 0) / 3600
                   )::text,
                   'last_finished_at', attempts.last_finished_at
               ),
               'runtime_usage', jsonb_build_object(
                   'request_count_1h', coalesce(runtime.request_count_1h, 0),
                   'consumed_a_1h', coalesce(runtime.consumed_a_1h, 0)::text,
                   'group_ids', coalesce(runtime.group_ids, '{}'::bigint[]),
                   'last_used_at', runtime.last_used_at
               ),
               'updated_at', source.updated_at
           ) AS document
    FROM model_sources source
    LEFT JOIN current_contracts contract ON contract.source_id = source.source_id
    LEFT JOIN current_quotas quota ON quota.source_id = source.source_id
    LEFT JOIN eligibility_source_totals eligibility
      ON eligibility.source_id = source.source_id
    LEFT JOIN eligibility_source_states states ON states.source_id = source.source_id
    LEFT JOIN source_attempts attempts ON attempts.source_id = source.source_id
    LEFT JOIN source_runtime_usage runtime ON runtime.source_id = source.source_id
), channel_totals AS (
    SELECT eligibility.channel_id,
           max(eligibility.channel_net_k_cny) AS channel_net_k_cny,
           min(eligibility.source_k_cny) FILTER (
               WHERE eligibility.source_k_cny IS NOT NULL
                 AND eligibility.eligibility_state IN ('ELIGIBLE', 'THROTTLED')
           ) AS minimum_source_k_cny,
           min(eligibility.minimum_channel_k_cny) FILTER (
               WHERE eligibility.minimum_channel_k_cny IS NOT NULL
                 AND eligibility.eligibility_state IN ('ELIGIBLE', 'THROTTLED')
           ) AS minimum_required_channel_k_cny,
           count(*)::bigint AS evaluated_source_count,
           count(*) FILTER (
               WHERE eligibility.eligibility_state IN ('ELIGIBLE', 'THROTTLED')
           )::bigint AS eligible_source_count
    FROM active_eligibility eligibility
    GROUP BY eligibility.channel_id
), channel_states AS (
    SELECT states.channel_id,
           jsonb_object_agg(states.eligibility_state, states.state_count)
               AS state_counts
    FROM (
        SELECT eligibility.channel_id, eligibility.eligibility_state,
               count(*)::bigint AS state_count
        FROM active_eligibility eligibility
        GROUP BY eligibility.channel_id, eligibility.eligibility_state
    ) states
    GROUP BY states.channel_id
), channel_sources AS (
    SELECT eligibility.channel_id,
           coalesce(
               jsonb_agg(
                   jsonb_build_object(
                       'source_id', eligibility.source_id,
                       'source_key', source.source_key,
                       'source_kind', eligibility.source_kind,
                       'account_id', eligibility.enodot_account_id,
                       'state', eligibility.eligibility_state,
                       'source_k_cny', eligibility.source_k_cny::text,
                       'minimum_channel_k_cny',
                           eligibility.minimum_channel_k_cny::text,
                       'profit_on_cost_bps', eligibility.profit_on_cost_bps,
                       'required_burn_a_per_second',
                           eligibility.required_burn_a_per_second::text
                   ) ORDER BY
                       CASE
                           WHEN eligibility.source_kind = 'account'
                                AND eligibility.required_burn_a_per_second > 0
                           THEN 0 ELSE 1
                       END,
                       eligibility.required_burn_a_per_second DESC,
                       eligibility.source_k_cny,
                       eligibility.source_id
               ) FILTER (
                   WHERE eligibility.eligibility_state IN ('ELIGIBLE', 'THROTTLED')
               ),
               '[]'::jsonb
           ) AS eligible_sources
    FROM active_eligibility eligibility
    JOIN model_sources source ON source.source_id = eligibility.source_id
    GROUP BY eligibility.channel_id
), channel_rows AS (
    SELECT totals.channel_id,
           jsonb_build_object(
               'channel_id', totals.channel_id,
               'channel_net_k_cny', totals.channel_net_k_cny::text,
               'minimum_source_k_cny', totals.minimum_source_k_cny::text,
               'minimum_required_channel_k_cny',
                   totals.minimum_required_channel_k_cny::text,
               'headroom_cny', CASE
                   WHEN totals.channel_net_k_cny IS NOT NULL
                        AND totals.minimum_required_channel_k_cny IS NOT NULL
                   THEN (
                       totals.channel_net_k_cny
                       - totals.minimum_required_channel_k_cny
                   )::text
                   ELSE NULL
               END,
               'headroom_bps', CASE
                   WHEN totals.channel_net_k_cny IS NOT NULL
                        AND totals.minimum_required_channel_k_cny IS NOT NULL
                        AND totals.minimum_source_k_cny > 0
                   THEN floor(
                       (
                           totals.channel_net_k_cny
                           - totals.minimum_required_channel_k_cny
                       ) / totals.minimum_source_k_cny * 10000
                   )::bigint
                   ELSE NULL
               END,
               'evaluated_source_count', totals.evaluated_source_count,
               'eligible_source_count', totals.eligible_source_count,
               'eligibility_states', coalesce(states.state_counts, '{}'::jsonb),
               'eligible_sources', coalesce(sources.eligible_sources, '[]'::jsonb)
           ) AS document
    FROM channel_totals totals
    LEFT JOIN channel_states states ON states.channel_id = totals.channel_id
    LEFT JOIN channel_sources sources ON sources.channel_id = totals.channel_id
), global_attempts AS (
    SELECT count(attempt.attempt_id)::bigint AS attempt_count,
           count(*) FILTER (WHERE attempt.realized_profit_cny < 0)::bigint
               AS negative_profit_attempt_count,
           coalesce(sum(attempt.realized_a), 0) AS realized_a,
           coalesce(sum(attempt.realized_cost_cny), 0) AS realized_cost_cny,
           coalesce(sum(attempt.realized_revenue_cny), 0) AS realized_revenue_cny,
           coalesce(sum(attempt.realized_profit_cny), 0) AS realized_profit_cny
    FROM requested
    LEFT JOIN a6_trade.sub2api_economic_attempts attempt
      ON attempt.model = requested.model
), global_states AS (
    SELECT coalesce(
        jsonb_object_agg(states.eligibility_state, states.state_count),
        '{}'::jsonb
    ) AS state_counts
    FROM (
        SELECT eligibility.eligibility_state, count(*)::bigint AS state_count
        FROM active_eligibility eligibility
        GROUP BY eligibility.eligibility_state
    ) states
)
SELECT requested.model,
       (
           EXISTS (SELECT 1 FROM model_sources)
           OR EXISTS (SELECT 1 FROM active_generation)
           OR EXISTS (SELECT 1 FROM planned_generation)
           OR EXISTS (SELECT 1 FROM profit_setting)
           OR EXISTS (
               SELECT 1
               FROM a6_trade.sub2api_economic_attempts historical
               WHERE historical.model = requested.model
           )
       ) AS known,
       clock_timestamp() AS generated_at,
       (
           SELECT jsonb_build_object(
               'minimum_profit_on_cost_bps',
                   setting.minimum_profit_on_cost_bps,
               'updated_at', setting.updated_at,
               'updated_by', setting.updated_by
           )
           FROM profit_setting setting
       ) AS profit_setting,
       (
           SELECT jsonb_build_object(
               'generation', generation.economic_generation,
               'parent_generation', generation.parent_generation,
               'state', generation.generation_state,
               'plan_only', generation.plan_only,
               'input_hash', generation.input_hash,
               'output_hash', generation.output_hash,
               'minimum_profit_on_cost_bps',
                   generation.minimum_profit_on_cost_bps,
               'quota_snapshot_max_age_seconds',
                   generation.quota_snapshot_max_age_seconds,
               'channel_revenue_max_age_seconds',
                   generation.channel_revenue_max_age_seconds,
               'health_max_age_seconds', generation.health_max_age_seconds,
               'config_version', generation.config_version,
               'evaluated_at', generation.evaluated_at,
               'activated_at', generation.activated_at
           )
           FROM active_generation generation
       ) AS generation,
       (
           SELECT jsonb_build_object(
               'generation', generation.economic_generation,
               'parent_generation', generation.parent_generation,
               'state', generation.generation_state,
               'plan_only', generation.plan_only,
               'input_hash', generation.input_hash,
               'output_hash', generation.output_hash,
               'minimum_profit_on_cost_bps',
                   generation.minimum_profit_on_cost_bps,
               'config_version', generation.config_version,
               'evaluated_at', generation.evaluated_at
           )
           FROM planned_generation generation
       ) AS preview,
       (
           SELECT jsonb_build_object(
               'generation', receipt.economic_generation,
               'digest', receipt.projection_digest,
               'state', receipt.projection_state,
               'projected_at', receipt.projected_at,
               'last_error_class', receipt.last_error_class,
               'updated_at', receipt.updated_at
           )
           FROM a6_trade.sub2api_economic_projection_receipts receipt
           WHERE receipt.model = requested.model
       ) AS projection,
       (SELECT state_counts FROM global_states) AS eligibility_states,
       coalesce(
           (
               SELECT jsonb_agg(limited.document ORDER BY limited.source_id)
               FROM (
                   SELECT source.source_id, source.document
                   FROM source_rows source
                   ORDER BY source.source_id
                   LIMIT 500
               ) limited
           ),
           '[]'::jsonb
       ) AS sources,
       coalesce(
           (
               SELECT jsonb_agg(limited.document ORDER BY limited.channel_id)
               FROM (
                   SELECT channel.channel_id, channel.document
                   FROM channel_rows channel
                   ORDER BY channel.channel_id
                   LIMIT 2000
               ) limited
           ),
           '[]'::jsonb
       ) AS channels,
       (
           SELECT jsonb_build_object(
               'source_count', (SELECT count(*) FROM model_sources),
               'returned_source_count', (
                   SELECT least(count(*), 500) FROM model_sources
               ),
               'channel_count', (SELECT count(*) FROM channel_totals),
               'returned_channel_count', (
                   SELECT least(count(*), 2000) FROM channel_totals
               ),
               'eligible_channel_count', (
                   SELECT count(*) FROM channel_totals
                   WHERE eligible_source_count > 0
               ),
               'eligible_pair_count', (
                   SELECT count(*) FROM active_eligibility
                   WHERE eligibility_state IN ('ELIGIBLE', 'THROTTLED')
               ),
               'attempt_count', attempts.attempt_count,
               'negative_profit_attempt_count',
                   attempts.negative_profit_attempt_count,
               'realized_a', attempts.realized_a::text,
               'realized_cost_cny', attempts.realized_cost_cny::text,
               'realized_revenue_cny', attempts.realized_revenue_cny::text,
               'realized_profit_cny', attempts.realized_profit_cny::text
           )
           FROM global_attempts attempts
       ) AS summary
FROM requested
"""


_PROVIDER_POOL_READ_MODEL_QUERY = """
WITH requested AS (
    SELECT %s::text AS model
), active_bundle AS (
    SELECT generation.id AS routing_generation_id,
           generation.generation AS routing_generation,
           generation.parent_generation AS parent_routing_generation,
           generation.generation_source,
           generation.source_hash,
           generation.desired_size,
           generation.ordered_channel_ids,
           generation.standby_channel_ids,
           generation.generation_state,
           generation.observed_at,
           generation.planned_at AS routing_planned_at,
           generation.activated_at AS routing_activated_at,
           generation.updated_at AS routing_updated_at,
           generation.config_version AS routing_config_version,
           cost.id AS cost_generation_id,
           cost.cost_generation,
           cost.parent_cost_generation,
           cost.cost_source,
           cost.cost_input_hash,
           cost.cost_output_hash,
           cost.token_mix_version,
           cost.exchange_rate_version,
           cost.quote_observed_at,
           cost.active_slot_nos,
           cost.route_cost_multiplier,
           cost.retry_reserve_multiplier,
           cost.realized_cost_p95_multiplier,
           cost.pool_cost_multiplier,
           cost.margin_bps,
           cost.headroom_bps,
           cost.billing_floor_multiplier,
           cost.transition_floor_multiplier,
           cost.observed_billing_floor_multiplier,
           cost.pricing_state,
           cost.planned_at AS cost_planned_at,
           cost.confirmed_at AS cost_confirmed_at,
           cost.activated_at AS cost_activated_at,
           cost.updated_at AS cost_updated_at,
           cost.config_version AS cost_config_version
    FROM requested
    JOIN a6_trade.provider_pool_generations generation
      ON generation.model = requested.model
    JOIN a6_trade.provider_cost_generations cost
      ON cost.id = generation.cost_generation_id
     AND cost.model = generation.model
     AND cost.cost_generation = generation.cost_generation
    WHERE generation.generation_state = 'active'
      AND cost.pricing_state = 'active'
    LIMIT 1
), active_auto_pool AS (
    SELECT auto_generation.id,
           auto_generation.auto_pool_generation,
           auto_generation.generation_state,
           auto_generation.selected_channel_ids,
           auto_generation.max_ratio_cny,
           auto_generation.max_ratio_usd,
           auto_generation.target_channel_count,
           auto_generation.observed_at,
           auto_generation.activated_at,
           auto_generation.updated_at,
           auto_generation.config_version,
           count(auto_account.id) FILTER (
               WHERE auto_account.account_state = 'ready'
           )::integer AS ready_account_count,
           count(auto_account.id)::integer AS account_count,
           bool_and(auto_account.ban_confirmed) FILTER (
               WHERE auto_account.account_state = 'ready'
           ) AS owned_channel_ban_confirmed
    FROM requested
    JOIN a6_trade.provider_auto_pool_generations auto_generation
      ON auto_generation.model = requested.model
    LEFT JOIN a6_trade.provider_auto_pool_accounts auto_account
      ON auto_account.generation_id = auto_generation.id
    WHERE auto_generation.generation_state = 'active'
    GROUP BY auto_generation.id
    LIMIT 1
), planned_bundle AS (
    SELECT generation.id AS routing_generation_id,
           generation.generation AS routing_generation,
           generation.parent_generation AS parent_routing_generation,
           generation.generation_source,
           generation.source_hash,
           generation.desired_size,
           generation.ordered_channel_ids,
           generation.standby_channel_ids,
           generation.generation_state,
           generation.observed_at,
           generation.planned_at AS routing_planned_at,
           generation.updated_at AS routing_updated_at,
           generation.config_version AS routing_config_version,
           cost.id AS cost_generation_id,
           cost.cost_generation,
           cost.parent_cost_generation,
           cost.cost_source,
           cost.cost_input_hash,
           cost.cost_output_hash,
           cost.token_mix_version,
           cost.exchange_rate_version,
           cost.quote_observed_at,
           cost.active_slot_nos,
           cost.desired_slot_costs,
           cost.route_cost_multiplier,
           cost.retry_reserve_multiplier,
           cost.realized_cost_p95_multiplier,
           cost.pool_cost_multiplier,
           cost.margin_bps,
           cost.headroom_bps,
           cost.billing_floor_multiplier,
           cost.transition_floor_multiplier,
           cost.observed_billing_floor_multiplier,
           cost.pricing_state,
           cost.planned_at AS cost_planned_at,
           cost.confirmed_at AS cost_confirmed_at,
           cost.updated_at AS cost_updated_at,
           cost.config_version AS cost_config_version
    FROM requested
    JOIN a6_trade.provider_pool_generations generation
      ON generation.model = requested.model
    JOIN a6_trade.provider_cost_generations cost
      ON cost.id = generation.cost_generation_id
     AND cost.model = generation.model
     AND cost.cost_generation = generation.cost_generation
    WHERE (
        (generation.generation_state = 'planned' AND cost.pricing_state = 'planned')
        OR (
            generation.generation_state = 'applying'
            AND cost.pricing_state IN ('applying', 'confirmed')
        )
        OR (generation.generation_state = 'failed' AND cost.pricing_state = 'failed')
    )
    ORDER BY generation.generation DESC, generation.planned_at DESC, generation.id DESC
    LIMIT 1
), planned_slot_rows AS (
    SELECT series.slot_no,
           'a6-provider-' || series.slot_no::text AS slot_name,
           planned.routing_generation,
           planned.cost_generation,
           CASE
               WHEN series.slot_no = ANY(planned.active_slot_nos) THEN 'READY'
               ELSE 'VACANT'
           END AS slot_state,
           planned.ordered_channel_ids[
               array_position(planned.active_slot_nos, series.slot_no)
           ] AS desired_channel_id,
           array_position(planned.active_slot_nos, series.slot_no) AS desired_rank,
           planned.desired_slot_costs ->> series.slot_no::text
               AS desired_slot_cost_multiplier,
           greatest(planned.routing_updated_at, planned.cost_updated_at) AS updated_at
    FROM planned_bundle planned
    CROSS JOIN LATERAL generate_series(
        1,
        least(planned.desired_size, 100)
    ) AS series(slot_no)
), current_slots AS (
    SELECT slot.slot_no, slot.slot_name, slot.routing_generation,
           slot.cost_generation, slot.slot_state,
           slot.desired_channel_id, slot.desired_rank, slot.desired_priority,
           slot.enodot_binding_id, slot.enodot_account_id, slot.a6_token_id,
           slot.desired_slot_cost_multiplier, slot.observed_rate_multiplier,
           slot.liquidity_state, slot.funding_eligible,
           slot.desired_schedulable, slot.observed_schedulable,
           slot.confirmed_pin_binding_id, slot.pin_confirmed,
           slot.probe_confirmed, slot.state_version, slot.last_success_at,
           slot.latency_ewma_ms, slot.valid_from, slot.updated_at,
           slot.route_mode, slot.auto_pool_generation_id,
           slot.auto_pool_subscription_id, slot.auto_pool_confirmed,
           slot.auto_pool_max_ratio_cny,
           auto_generation.auto_pool_generation,
           auto_account.ban_confirmed AS owned_channel_ban_confirmed,
           auto_account.unpin_confirmed AS auto_pool_unpin_confirmed,
           auto_account.account_state AS auto_pool_account_state
    FROM active_bundle active
    JOIN a6_trade.provider_pool_slots slot
      ON slot.generation_id = active.routing_generation_id
     AND slot.routing_generation = active.routing_generation
     AND slot.cost_generation_id = active.cost_generation_id
     AND slot.cost_generation = active.cost_generation
    LEFT JOIN a6_trade.provider_auto_pool_generations auto_generation
      ON auto_generation.id = slot.auto_pool_generation_id
    LEFT JOIN a6_trade.provider_auto_pool_accounts auto_account
      ON auto_account.generation_id = auto_generation.id
     AND auto_account.token_ref_id = slot.a6_token_id
     AND auto_account.enodot_account_id = slot.enodot_account_id
     AND auto_account.slot_no = slot.slot_no
    WHERE slot.valid_to IS NULL
    ORDER BY slot.slot_no
    LIMIT 100
), publication_state AS (
    SELECT publication.desired_state, publication.observed_state,
           publication.healthy_slots, publication.min_healthy_slots,
           publication.redundancy_target,
           publication.reason_code, publication.desired_generation,
           publication.desired_cost_generation, publication.publication_scope,
           publication.state_version, publication.desired_at,
           publication.action_started_at, publication.observed_at,
           publication.confirmed_at, publication.last_real_probe_at,
           publication.last_real_probe_request_id, publication.updated_at
    FROM requested
    JOIN a6_trade.model_publications publication
      ON publication.model = requested.model
    WHERE EXISTS (SELECT 1 FROM active_bundle)
), active_quarantine_rows AS (
    SELECT quarantine.id, quarantine.scope_type, quarantine.scope_id,
           quarantine.region, quarantine.reason_code, quarantine.failure_count,
           quarantine.starts_at, quarantine.next_probe_at, quarantine.ends_at,
           quarantine.updated_at
    FROM requested
    JOIN a6_trade.provider_quarantines quarantine
      ON quarantine.model = requested.model
    WHERE quarantine.state = 'active'
    ORDER BY quarantine.starts_at DESC, quarantine.id DESC
    LIMIT 100
), recent_failure_rows AS (
    SELECT attempt.id, attempt.request_id, attempt.attempt_id,
           attempt.attempt_no, attempt.routing_generation,
           attempt.cost_generation, attempt.slot_no,
           attempt.enodot_account_id, attempt.a6_token_id, attempt.channel_id,
           attempt.endpoint_host, attempt.egress_id, attempt.failure_owner,
           attempt.failure_code, attempt.http_status, attempt.transport_status,
           attempt.terminal_event_seen, attempt.delivery_state,
           attempt.actual_cost_micros,
           coalesce(attempt.finished_at, attempt.started_at) AS occurred_at
    FROM requested
    JOIN a6_trade.provider_attempts attempt
      ON attempt.model = requested.model
    WHERE attempt.state = 'failed'
    ORDER BY coalesce(attempt.finished_at, attempt.started_at) DESC, attempt.id DESC
    LIMIT 20
)
SELECT requested.model,
       (
           EXISTS (SELECT 1 FROM active_bundle)
           OR EXISTS (SELECT 1 FROM publication_state)
           OR EXISTS (SELECT 1 FROM active_auto_pool)
           OR EXISTS (
               SELECT 1
               FROM a6_trade.provider_pool_generations historical
               WHERE historical.model = requested.model
           )
           OR EXISTS (
               SELECT 1
               FROM a6_trade.provider_quarantines historical
               WHERE historical.model = requested.model
           )
           OR EXISTS (
               SELECT 1
               FROM a6_trade.provider_attempts historical
               WHERE historical.model = requested.model
           )
       ) AS known,
       clock_timestamp() AS generated_at,
       (
           SELECT jsonb_build_object(
               'generation', active.routing_generation,
               'parent_generation', active.parent_routing_generation,
               'state', active.generation_state,
               'source', active.generation_source,
               'source_hash', active.source_hash,
               'desired_size', active.desired_size,
               'topn_channel_ids', active.ordered_channel_ids,
               'standby_channel_ids', active.standby_channel_ids,
               'observed_at', active.observed_at,
               'planned_at', active.routing_planned_at,
               'activated_at', active.routing_activated_at,
               'updated_at', active.routing_updated_at,
               'config_version', active.routing_config_version
           )
           FROM active_bundle active
       ) AS routing,
       (
           SELECT jsonb_build_object(
               'generation', active.cost_generation,
               'parent_generation', active.parent_cost_generation,
               'state', active.pricing_state,
               'source', active.cost_source,
               'input_hash', active.cost_input_hash,
               'output_hash', active.cost_output_hash,
               'token_mix_version', active.token_mix_version,
               'exchange_rate_version', active.exchange_rate_version,
               'quote_observed_at', active.quote_observed_at,
               'active_slot_nos', active.active_slot_nos,
               'route_cost_multiplier', active.route_cost_multiplier::text,
               'retry_reserve_multiplier', active.retry_reserve_multiplier::text,
               'realized_cost_p95_multiplier',
                   active.realized_cost_p95_multiplier::text,
               'pool_cost_multiplier', active.pool_cost_multiplier::text,
               'margin_bps', active.margin_bps,
               'headroom_bps', active.headroom_bps,
               'billing_floor_multiplier', active.billing_floor_multiplier::text,
               'transition_floor_multiplier',
                   active.transition_floor_multiplier::text,
               'observed_billing_floor_multiplier',
                   active.observed_billing_floor_multiplier::text,
               'planned_at', active.cost_planned_at,
               'confirmed_at', active.cost_confirmed_at,
               'activated_at', active.cost_activated_at,
               'updated_at', active.cost_updated_at,
               'config_version', active.cost_config_version
           )
           FROM active_bundle active
       ) AS cost,
       (
           SELECT jsonb_build_object(
               'generation_id', auto_pool.id,
               'generation', auto_pool.auto_pool_generation,
               'state', auto_pool.generation_state,
               'selected_channel_ids', auto_pool.selected_channel_ids,
               'max_ratio_cny', auto_pool.max_ratio_cny::text,
               'max_ratio_usd', auto_pool.max_ratio_usd::text,
               'target_channel_count', auto_pool.target_channel_count,
               'ready_account_count', auto_pool.ready_account_count,
               'account_count', auto_pool.account_count,
               'owned_channel_ban_confirmed',
                   auto_pool.owned_channel_ban_confirmed,
               'observed_at', auto_pool.observed_at,
               'activated_at', auto_pool.activated_at,
               'updated_at', auto_pool.updated_at,
               'config_version', auto_pool.config_version
           )
           FROM active_auto_pool auto_pool
       ) AS auto_pool,
       (
           SELECT jsonb_build_object(
               'generation', planned.routing_generation,
               'parent_generation', planned.parent_routing_generation,
               'state', planned.generation_state,
               'source', planned.generation_source,
               'source_hash', planned.source_hash,
               'desired_size', planned.desired_size,
               'topn_channel_ids', planned.ordered_channel_ids,
               'standby_channel_ids', planned.standby_channel_ids,
               'observed_at', planned.observed_at,
               'planned_at', planned.routing_planned_at,
               'updated_at', planned.routing_updated_at,
               'config_version', planned.routing_config_version
           )
           FROM planned_bundle planned
       ) AS planned_routing,
       (
           SELECT jsonb_build_object(
               'generation', planned.cost_generation,
               'parent_generation', planned.parent_cost_generation,
               'state', planned.pricing_state,
               'source', planned.cost_source,
               'input_hash', planned.cost_input_hash,
               'output_hash', planned.cost_output_hash,
               'token_mix_version', planned.token_mix_version,
               'exchange_rate_version', planned.exchange_rate_version,
               'quote_observed_at', planned.quote_observed_at,
               'active_slot_nos', planned.active_slot_nos,
               'route_cost_multiplier', planned.route_cost_multiplier::text,
               'retry_reserve_multiplier', planned.retry_reserve_multiplier::text,
               'realized_cost_p95_multiplier',
                   planned.realized_cost_p95_multiplier::text,
               'pool_cost_multiplier', planned.pool_cost_multiplier::text,
               'margin_bps', planned.margin_bps,
               'headroom_bps', planned.headroom_bps,
               'billing_floor_multiplier', planned.billing_floor_multiplier::text,
               'transition_floor_multiplier',
                   planned.transition_floor_multiplier::text,
               'observed_billing_floor_multiplier',
                   planned.observed_billing_floor_multiplier::text,
               'planned_at', planned.cost_planned_at,
               'confirmed_at', planned.cost_confirmed_at,
               'updated_at', planned.cost_updated_at,
               'config_version', planned.cost_config_version
           )
           FROM planned_bundle planned
       ) AS planned_cost,
       coalesce(
           (
               SELECT jsonb_agg(
                   jsonb_build_object(
                       'slot_no', slot.slot_no,
                       'slot_name', slot.slot_name,
                       'routing_generation', slot.routing_generation,
                       'cost_generation', slot.cost_generation,
                       'state', slot.slot_state,
                       'channel_id', slot.desired_channel_id,
                       'rank', slot.desired_rank,
                       'desired_cost_multiplier', slot.desired_slot_cost_multiplier,
                       'updated_at', slot.updated_at
                   ) ORDER BY slot.slot_no
               )
               FROM planned_slot_rows slot
           ),
           '[]'::jsonb
       ) AS planned_slots,
       coalesce(
           (
               SELECT jsonb_agg(
                   jsonb_build_object(
                       'slot_no', slot.slot_no,
                       'slot_name', slot.slot_name,
                       'routing_generation', slot.routing_generation,
                       'cost_generation', slot.cost_generation,
                       'state', slot.slot_state,
                       'channel_id', slot.desired_channel_id,
                       'rank', slot.desired_rank,
                       'priority', slot.desired_priority,
                       'enodot_binding_id', slot.enodot_binding_id,
                       'account_id', slot.enodot_account_id,
                       'token_id', slot.a6_token_id,
                       'desired_cost_multiplier',
                           slot.desired_slot_cost_multiplier::text,
                       'observed_cost_multiplier',
                           slot.observed_rate_multiplier::text,
                       'liquidity_state', slot.liquidity_state,
                       'funding_eligible', slot.funding_eligible,
                       'desired_schedulable', slot.desired_schedulable,
                       'observed_schedulable', slot.observed_schedulable,
                       'confirmed_pin_binding_id', slot.confirmed_pin_binding_id,
                       'pin_confirmed', slot.pin_confirmed,
                       'probe_confirmed', slot.probe_confirmed,
                       'route_mode', slot.route_mode,
                       'auto_pool_generation_id', slot.auto_pool_generation_id,
                       'auto_pool_generation', slot.auto_pool_generation,
                       'auto_pool_subscription_id', slot.auto_pool_subscription_id,
                       'auto_pool_confirmed', slot.auto_pool_confirmed,
                       'auto_pool_max_ratio_cny',
                           slot.auto_pool_max_ratio_cny::text,
                       'owned_channel_ban_confirmed',
                           slot.owned_channel_ban_confirmed,
                       'auto_pool_unpin_confirmed',
                           slot.auto_pool_unpin_confirmed,
                       'auto_pool_account_state', slot.auto_pool_account_state,
                       'state_version', slot.state_version,
                       'last_success_at', slot.last_success_at,
                       'latency_ewma_ms', slot.latency_ewma_ms::text,
                       'valid_from', slot.valid_from,
                       'updated_at', slot.updated_at
                   ) ORDER BY slot.slot_no
               )
               FROM current_slots slot
           ),
           '[]'::jsonb
       ) AS slots,
       (
           SELECT jsonb_build_object(
               'desired_state', publication.desired_state,
               'observed_state', publication.observed_state,
               'healthy_slots', publication.healthy_slots,
               'min_healthy_slots', publication.min_healthy_slots,
               'redundancy_target', publication.redundancy_target,
               'reason_code', publication.reason_code,
               'desired_routing_generation', publication.desired_generation,
               'desired_cost_generation', publication.desired_cost_generation,
               'scope', publication.publication_scope,
               'state_version', publication.state_version,
               'desired_at', publication.desired_at,
               'action_started_at', publication.action_started_at,
               'observed_at', publication.observed_at,
               'confirmed_at', publication.confirmed_at,
               'last_real_probe_at', publication.last_real_probe_at,
               'last_real_probe_request_id',
                   publication.last_real_probe_request_id,
               'updated_at', publication.updated_at
           )
           FROM publication_state publication
       ) AS publication,
       coalesce(
           (
               SELECT jsonb_agg(
                   jsonb_build_object(
                       'scope_type', quarantine.scope_type,
                       'scope_id', quarantine.scope_id,
                       'region', quarantine.region,
                       'reason_code', quarantine.reason_code,
                       'failure_count', quarantine.failure_count,
                       'starts_at', quarantine.starts_at,
                       'next_probe_at', quarantine.next_probe_at,
                       'ends_at', quarantine.ends_at,
                       'updated_at', quarantine.updated_at
                   ) ORDER BY quarantine.starts_at DESC, quarantine.id DESC
               )
               FROM active_quarantine_rows quarantine
           ),
           '[]'::jsonb
       ) AS active_quarantines,
       coalesce(
           (
               SELECT jsonb_agg(
                   jsonb_build_object(
                       'request_id', failure.request_id,
                       'attempt_id', failure.attempt_id,
                       'attempt_no', failure.attempt_no,
                       'routing_generation', failure.routing_generation,
                       'cost_generation', failure.cost_generation,
                       'slot_no', failure.slot_no,
                       'account_id', failure.enodot_account_id,
                       'token_id', failure.a6_token_id,
                       'channel_id', failure.channel_id,
                       'endpoint_host', failure.endpoint_host,
                       'egress_id', failure.egress_id,
                       'failure_owner', failure.failure_owner,
                       'failure_code', failure.failure_code,
                       'http_status', failure.http_status,
                       'transport_status', failure.transport_status,
                       'terminal_event_seen', failure.terminal_event_seen,
                       'delivery_state', failure.delivery_state,
                       'actual_cost_micros', failure.actual_cost_micros,
                       'occurred_at', failure.occurred_at
                   ) ORDER BY failure.occurred_at DESC, failure.id DESC
               )
               FROM recent_failure_rows failure
           ),
           '[]'::jsonb
       ) AS recent_failures,
       (
           SELECT count(*)
           FROM a6_trade.provider_pool_generations recent_generation
           WHERE recent_generation.model = requested.model
             AND recent_generation.generation_state = 'failed'
             AND recent_generation.updated_at >= clock_timestamp() - interval '1 hour'
       ) AS recent_generation_failure_count
FROM requested
"""


_PROVIDER_INVENTORY_READ_MODEL_QUERY = """
WITH requested AS (
    SELECT %s::text AS model
), inventory_rows AS (
    SELECT inventory.merchant_id,
           inventory.enodot_binding_id,
           inventory.enodot_account_id,
           inventory.a6_token_id,
           CASE
               WHEN slot.slot_state = 'READY' THEN 'READY'
               ELSE inventory.inventory_state
           END AS inventory_state,
           inventory.liquidity_state,
           inventory.funding_eligible,
           CASE
               WHEN slot.slot_state = 'READY'
               THEN slot.observed_schedulable
               ELSE inventory.schedulable
           END AS schedulable,
           inventory.balance_units,
           inventory.financial_source_hash,
           coalesce(slot.slot_no, inventory.assigned_slot_no) AS assigned_slot_no,
           inventory.state_version,
           inventory.last_financial_observed_at,
           inventory.updated_at,
           slot.slot_no AS matched_slot_no,
           slot.slot_state,
           slot.routing_generation,
           slot.cost_generation,
           slot.desired_channel_id,
           slot.desired_priority,
           slot.observed_schedulable,
           slot.enodot_binding_id AS slot_binding_id,
           slot.enodot_account_id AS slot_account_id,
           slot.a6_token_id AS slot_token_id
    FROM requested
    JOIN a6_trade.provider_inventory inventory
      ON inventory.model = requested.model
    LEFT JOIN a6_trade.provider_pool_slots slot
      ON slot.model = inventory.model
     AND slot.valid_to IS NULL
     AND slot.enodot_binding_id = inventory.enodot_binding_id
     AND slot.enodot_account_id = inventory.enodot_account_id
     AND slot.a6_token_id = inventory.a6_token_id
    ORDER BY CASE inventory.inventory_state
                 WHEN 'READY' THEN 0
                 WHEN 'WARM_STANDBY' THEN 1
                 WHEN 'REPAIRING' THEN 2
                 WHEN 'VACANT' THEN 3
                 ELSE 4
             END,
             inventory.assigned_slot_no NULLS LAST,
             inventory.merchant_id
    LIMIT 500
)
SELECT requested.model,
       (
           EXISTS (SELECT 1 FROM inventory_rows)
           OR EXISTS (
               SELECT 1
               FROM a6_trade.provider_pool_generations generation
               WHERE generation.model = requested.model
           )
           OR EXISTS (
               SELECT 1
               FROM a6_trade.model_publications publication
               WHERE publication.model = requested.model
           )
       ) AS known,
       clock_timestamp() AS generated_at,
       coalesce(
           (
               SELECT jsonb_agg(
                   jsonb_build_object(
                       'merchant_id', inventory.merchant_id,
                       'enodot_binding_id', inventory.enodot_binding_id,
                       'account_id', inventory.enodot_account_id,
                       'token_id', inventory.a6_token_id,
                       'inventory_state', inventory.inventory_state,
                       'liquidity_state', inventory.liquidity_state,
                       'funding_eligible', inventory.funding_eligible,
                       'schedulable', inventory.schedulable,
                       'binding_ready',
                           inventory.enodot_binding_id IS NOT NULL
                           AND inventory.enodot_account_id IS NOT NULL
                           AND inventory.a6_token_id IS NOT NULL,
                       'balance_units', inventory.balance_units,
                       'financial_source_hash', inventory.financial_source_hash,
                       'assigned_slot_no', inventory.assigned_slot_no,
                       'assignment', CASE
                           WHEN inventory.assigned_slot_no IS NULL THEN NULL
                           ELSE jsonb_build_object(
                               'slot_no', inventory.assigned_slot_no,
                               'found', inventory.matched_slot_no IS NOT NULL,
                               'identity_matches',
                                   inventory.matched_slot_no IS NOT NULL
                                   AND inventory.enodot_binding_id
                                       IS NOT DISTINCT FROM inventory.slot_binding_id
                                   AND inventory.enodot_account_id
                                       IS NOT DISTINCT FROM inventory.slot_account_id
                                   AND inventory.a6_token_id
                                       IS NOT DISTINCT FROM inventory.slot_token_id,
                               'state', inventory.slot_state,
                               'routing_generation', inventory.routing_generation,
                               'cost_generation', inventory.cost_generation,
                               'channel_id', inventory.desired_channel_id,
                               'priority', inventory.desired_priority,
                               'observed_schedulable',
                                   coalesce(inventory.observed_schedulable, false)
                           )
                       END,
                       'state_version', inventory.state_version,
                       'last_financial_observed_at',
                           inventory.last_financial_observed_at,
                       'updated_at', inventory.updated_at
                   ) ORDER BY
                       CASE inventory.inventory_state
                           WHEN 'READY' THEN 0
                           WHEN 'WARM_STANDBY' THEN 1
                           WHEN 'REPAIRING' THEN 2
                           WHEN 'VACANT' THEN 3
                           ELSE 4
                       END,
                       inventory.assigned_slot_no NULLS LAST,
                       inventory.merchant_id
               )
               FROM inventory_rows inventory
           ),
           '[]'::jsonb
       ) AS items
FROM requested
"""


_CHANNEL_FAILURE_SUMMARY_QUERY = """
WITH recent AS (
    SELECT channel_id, failure_code, responsibility, failure_at
    FROM a6_trade.a6_channel_failure_events
    WHERE model = %s AND failure_at >= %s
), per_channel AS (
    SELECT channel_id,
           count(*)::integer AS recent_failure_count,
           count(*) FILTER (WHERE responsibility = 'client')::integer
               AS recent_client_failure_count,
           count(*) FILTER (WHERE responsibility = 'provider')::integer
               AS recent_provider_failure_count,
           count(*) FILTER (WHERE responsibility = 'platform')::integer
               AS recent_platform_failure_count,
           count(*) FILTER (WHERE responsibility = 'unknown')::integer
               AS recent_unknown_failure_count
    FROM recent
    GROUP BY channel_id
), per_code AS (
    SELECT channel_id, failure_code, responsibility,
           count(*)::integer AS failure_count,
           max(failure_at) AS last_failure_at,
           row_number() OVER (
               PARTITION BY channel_id
               ORDER BY count(*) DESC, max(failure_at) DESC, failure_code
           ) AS code_rank
    FROM recent
    GROUP BY channel_id, failure_code, responsibility
), code_summary AS (
    SELECT channel_id,
           jsonb_agg(
               jsonb_build_object(
                   'code', failure_code,
                   'responsibility', responsibility,
                   'count', failure_count,
                   'last_failure_at', last_failure_at
               ) ORDER BY failure_count DESC, last_failure_at DESC, failure_code
           ) AS top_failure_codes
    FROM per_code
    WHERE code_rank <= 8
    GROUP BY channel_id
)
SELECT signal.channel_id, signal.listing_id, signal.supplier_id,
       signal.market_rank, signal.health_state, signal.available,
       signal.recent_success_rate, signal.last_failure_at,
       signal.last_failure_code,
       coalesce(summary.recent_failure_count, 0)::integer
           AS recent_failure_count,
       coalesce(summary.recent_client_failure_count, 0)::integer
           AS recent_client_failure_count,
       coalesce(summary.recent_provider_failure_count, 0)::integer
           AS recent_provider_failure_count,
       coalesce(summary.recent_platform_failure_count, 0)::integer
           AS recent_platform_failure_count,
       coalesce(summary.recent_unknown_failure_count, 0)::integer
           AS recent_unknown_failure_count,
       coalesce(codes.top_failure_codes, '[]'::jsonb) AS top_failure_codes
FROM a6_trade.a6_market_channel_signals_current signal
LEFT JOIN per_channel summary ON summary.channel_id = signal.channel_id
LEFT JOIN code_summary codes ON codes.channel_id = signal.channel_id
WHERE signal.model = %s
  AND summary.recent_failure_count > 0
ORDER BY
    coalesce(summary.recent_provider_failure_count, 0)
        + coalesce(summary.recent_platform_failure_count, 0) DESC,
    coalesce(summary.recent_failure_count, 0) DESC,
    signal.market_rank NULLS LAST,
    signal.channel_id
"""


_MANAGED_POSITION_PERFORMANCE_QUERY = """
WITH money AS (
    SELECT %s::numeric AS usd_micros_per_unit
), earning_flow AS (
    SELECT event.merchant_id,
           event.channel_id,
           count(*)::bigint AS seller_call_count,
           round(
               sum(event.earned_micros)::numeric
               * money.usd_micros_per_unit
               / merchant.quota_per_unit
           )::bigint AS seller_income_micros
    FROM a6_trade.a6_earning_events event
    JOIN a6_trade.a6_merchants merchant ON merchant.id = event.merchant_id
    CROSS JOIN money
    WHERE event.occurred_at >= %s
      AND event.model = %s
    GROUP BY
        event.merchant_id,
        event.channel_id,
        merchant.quota_per_unit,
        money.usd_micros_per_unit
), managed_key_flow AS (
    SELECT managed.merchant_id,
           count(*)::bigint AS managed_key_call_count,
           round(sum(usage.actual_cost) * money.usd_micros_per_unit)::bigint
               AS managed_key_debit_micros
    FROM a6_trade.enodot_managed_seller_resources managed
    JOIN public.usage_logs usage
      ON usage.api_key_id = managed.enodot_api_key_id
    CROSS JOIN money
    WHERE usage.created_at >= %s
      AND (
          usage.model = %s
          OR usage.requested_model = %s
          OR usage.upstream_model = %s
      )
    GROUP BY managed.merchant_id, money.usd_micros_per_unit
)
SELECT plan.rank,
       listing.merchant_id,
       current_listing.platform_model AS planned_model,
       position_merchant.status AS merchant_status,
       position_merchant.roles AS merchant_roles,
       true AS binding_present,
       listing.control_state AS binding_control_state,
       listing.a6_channel_id AS channel_id,
       listing.a6_listing_id AS listing_id,
       (
           current_listing.a6_listing_id IS NOT NULL
           AND current_listing.observed_at >= position_merchant.last_poll_at
       ) AS listing_present,
       current_listing.a6_channel_id AS listing_channel_id,
       current_listing.platform_model AS listing_platform_model,
       current_listing.status AS listing_status,
       current_listing.availability AS listing_availability,
       current_listing.visibility AS listing_visibility,
       channel.name AS channel_name,
       channel.nickname AS channel_nickname,
       plan.tier,
       plan.price_zone,
       plan.delta,
       plan.target_final_multiplier,
       plan.panel_multiplier,
       signal.realtime_ratio_cny AS current_k_cny,
       coalesce(signal.available, false) AS market_available,
       coalesce(signal.health_state, 'unknown') AS health_state,
       coalesce(signal.route_status, '') AS route_status,
       coalesce(signal.circuit_state, '') AS circuit_state,
       signal.circuit_cooling_until,
       coalesce(signal.cooldown_state, '') AS cooldown_state,
       signal.cooldown_remaining_seconds,
       signal.cooldown_expected_recovery_at,
       coalesce(signal.smart_routing_status, '') AS smart_routing_status,
       signal.sample_count,
       signal.recent_success_rate,
       signal.recent_p50_ms,
       signal.p50_ttft_ms,
       signal.last_success_at,
       signal.last_failure_at,
       signal.last_failure_code,
       coalesce(signal.smart_routing_labels, '{}'::text[]) AS smart_routing_labels,
       signal.last_observed_at AS health_observed_at,
       growth_policy.degraded_success_rate,
       growth_policy.idle_seconds,
       coalesce(earning.seller_call_count, 0)::bigint AS seller_call_count,
       coalesce(earning.seller_income_micros, 0)::bigint AS seller_income_micros,
       coalesce(key_flow.managed_key_call_count, 0)::bigint
           AS managed_key_call_count,
       coalesce(key_flow.managed_key_debit_micros, 0)::bigint
           AS managed_key_debit_micros
FROM a6_trade.a6_managed_listing_bindings listing
JOIN a6_trade.a6_managed_channel_bindings channel_binding
  ON channel_binding.id = listing.channel_binding_id
 AND channel_binding.merchant_id = listing.merchant_id
JOIN a6_trade.a6_merchants position_merchant
  ON position_merchant.id = listing.merchant_id
LEFT JOIN a6_trade.a6_supplier_listings_current current_listing
  ON current_listing.merchant_id = listing.merchant_id
 AND current_listing.a6_listing_id = listing.a6_listing_id
LEFT JOIN a6_trade.a6_supplier_channels_current channel
  ON channel.merchant_id = listing.merchant_id
 AND channel.a6_channel_id = listing.a6_channel_id
LEFT JOIN LATERAL (
    SELECT (action.desired_state ->> 'rank')::integer AS rank,
           action.desired_state ->> 'tier' AS tier,
           action.desired_state ->> 'price_zone' AS price_zone,
           (action.desired_state ->> 'delta')::numeric AS delta,
           (action.desired_state ->> 'target_final_multiplier')::numeric
               AS target_final_multiplier,
           (action.desired_state ->> 'a6_panel_multiplier')::numeric
               AS panel_multiplier
    FROM a6_trade.trade_control_actions action
    WHERE action.action_type = 'apply_seller_price_band'
      AND action.entity_type = 'a6_managed_listing'
      AND action.entity_id = listing.managed_uid
      AND action.status = 'completed'
      AND action.desired_state ->> 'model' = current_listing.platform_model
    ORDER BY action.completed_at DESC, action.id DESC
    LIMIT 1
) plan ON true
LEFT JOIN earning_flow earning
  ON earning.merchant_id = listing.merchant_id
 AND earning.channel_id = listing.a6_channel_id
LEFT JOIN managed_key_flow key_flow ON key_flow.merchant_id = listing.merchant_id
LEFT JOIN a6_trade.a6_market_channel_signals_current signal
 ON signal.model = %s
 AND signal.channel_id = listing.a6_channel_id
LEFT JOIN a6_trade.a6_channel_growth_policies growth_policy
  ON growth_policy.listing_binding_id = listing.id
WHERE listing.control_state <> 'retired'
  AND channel_binding.control_state <> 'retired'
  AND position_merchant.status <> 'abandoned'
  AND position_merchant.roles @> ARRAY['seller']::text[]
  AND position_merchant.last_poll_at IS NOT NULL
  AND NOT EXISTS (
      SELECT 1
      FROM a6_trade.a6_seller_retirement_plans retirement
      WHERE retirement.merchant_id = position_merchant.id
        AND retirement.state = 'completed'
  )
  AND current_listing.platform_model = %s
  AND (
      channel.name IS NULL
      OR (
          btrim(channel.name) <> %s
          AND NOT (
              btrim(channel.name) ~ %s
              AND btrim(channel.name) ~ %s
          )
      )
  )
ORDER BY plan.rank NULLS LAST, listing.merchant_id, listing.id
"""

_STOCK_WINDOW_QUERY = """
WITH stock_params AS (
    SELECT %s::timestamptz AS since,
           %s::timestamptz AS as_of,
           %s::numeric AS usd_micros_per_unit
), per_merchant_raw AS (
    SELECT c.merchant_id, m.quota_per_unit, p.usd_micros_per_unit,
           coalesce(c.user_balance_quota, 0)::bigint AS user_balance_current,
           coalesce(c.withdrawable_micros, 0)::bigint AS withdrawable_current,
           coalesce(c.withdrawing_micros, 0)::bigint AS withdrawing_current,
           coalesce(b.user_balance_quota, c.user_balance_quota, 0)::bigint
               AS user_balance_start,
           coalesce(b.withdrawable_micros, c.withdrawable_micros, 0)::bigint
               AS withdrawable_start,
           coalesce(b.withdrawing_micros, c.withdrawing_micros, 0)::bigint
               AS withdrawing_start,
           least(
               coalesce(b.user_balance_quota, c.user_balance_quota, 0),
               coalesce(w.user_balance_min, c.user_balance_quota, 0),
               coalesce(c.user_balance_quota, 0)
           )::bigint AS user_balance_min,
           greatest(
               coalesce(b.user_balance_quota, c.user_balance_quota, 0),
               coalesce(w.user_balance_max, c.user_balance_quota, 0),
               coalesce(c.user_balance_quota, 0)
           )::bigint AS user_balance_max,
           least(
               coalesce(b.withdrawable_micros, c.withdrawable_micros, 0),
               coalesce(w.withdrawable_min, c.withdrawable_micros, 0),
               coalesce(c.withdrawable_micros, 0)
           )::bigint AS withdrawable_min,
           greatest(
               coalesce(b.withdrawable_micros, c.withdrawable_micros, 0),
               coalesce(w.withdrawable_max, c.withdrawable_micros, 0),
               coalesce(c.withdrawable_micros, 0)
           )::bigint AS withdrawable_max,
           least(
               coalesce(b.withdrawing_micros, c.withdrawing_micros, 0),
               coalesce(w.withdrawing_min, c.withdrawing_micros, 0),
               coalesce(c.withdrawing_micros, 0)
           )::bigint AS withdrawing_min,
           greatest(
               coalesce(b.withdrawing_micros, c.withdrawing_micros, 0),
               coalesce(w.withdrawing_max, c.withdrawing_micros, 0),
               coalesce(c.withdrawing_micros, 0)
           )::bigint AS withdrawing_max
    FROM a6_trade.a6_financial_current c
    JOIN a6_trade.a6_merchants m ON m.id = c.merchant_id
    CROSS JOIN stock_params p
    LEFT JOIN LATERAL (
        SELECT s.user_balance_quota, s.withdrawable_micros, s.withdrawing_micros
        FROM a6_trade.a6_financial_snapshots s
        WHERE s.merchant_id = c.merchant_id AND s.observed_at <= p.as_of
        ORDER BY
            (s.observed_at > p.since),
            CASE WHEN s.observed_at <= p.since THEN s.observed_at END DESC,
            CASE WHEN s.observed_at > p.since THEN s.observed_at END ASC
        LIMIT 1
    ) b ON true
    LEFT JOIN LATERAL (
        SELECT min(s.user_balance_quota) AS user_balance_min,
               max(s.user_balance_quota) AS user_balance_max,
               min(s.withdrawable_micros) AS withdrawable_min,
               max(s.withdrawable_micros) AS withdrawable_max,
               min(s.withdrawing_micros) AS withdrawing_min,
               max(s.withdrawing_micros) AS withdrawing_max
        FROM a6_trade.a6_financial_snapshots s
        WHERE s.merchant_id = c.merchant_id
          AND s.observed_at >= p.since AND s.observed_at <= p.as_of
    ) w ON true
), per_merchant AS (
    SELECT merchant_id,
           round(user_balance_current::numeric * usd_micros_per_unit / quota_per_unit)::bigint
               AS user_balance_current,
           round(withdrawable_current::numeric * usd_micros_per_unit / quota_per_unit)::bigint
               AS withdrawable_current,
           round(withdrawing_current::numeric * usd_micros_per_unit / quota_per_unit)::bigint
               AS withdrawing_current,
           round(user_balance_start::numeric * usd_micros_per_unit / quota_per_unit)::bigint
               AS user_balance_start,
           round(withdrawable_start::numeric * usd_micros_per_unit / quota_per_unit)::bigint
               AS withdrawable_start,
           round(withdrawing_start::numeric * usd_micros_per_unit / quota_per_unit)::bigint
               AS withdrawing_start,
           round(user_balance_min::numeric * usd_micros_per_unit / quota_per_unit)::bigint
               AS user_balance_min,
           round(user_balance_max::numeric * usd_micros_per_unit / quota_per_unit)::bigint
               AS user_balance_max,
           round(withdrawable_min::numeric * usd_micros_per_unit / quota_per_unit)::bigint
               AS withdrawable_min,
           round(withdrawable_max::numeric * usd_micros_per_unit / quota_per_unit)::bigint
               AS withdrawable_max,
           round(withdrawing_min::numeric * usd_micros_per_unit / quota_per_unit)::bigint
               AS withdrawing_min,
           round(withdrawing_max::numeric * usd_micros_per_unit / quota_per_unit)::bigint
               AS withdrawing_max
    FROM per_merchant_raw
)
SELECT coalesce(sum(user_balance_start), 0)::bigint AS user_balance_start,
       coalesce(sum(user_balance_current), 0)::bigint AS user_balance_current,
       coalesce(sum(user_balance_min), 0)::bigint AS user_balance_min,
       coalesce(sum(user_balance_max), 0)::bigint AS user_balance_max,
       coalesce(sum(withdrawable_start), 0)::bigint AS withdrawable_start,
       coalesce(sum(withdrawable_current), 0)::bigint AS withdrawable_current,
       coalesce(sum(withdrawable_min), 0)::bigint AS withdrawable_min,
       coalesce(sum(withdrawable_max), 0)::bigint AS withdrawable_max,
       coalesce(sum(withdrawing_start), 0)::bigint AS withdrawing_start,
       coalesce(sum(withdrawing_current), 0)::bigint AS withdrawing_current,
       coalesce(sum(withdrawing_min), 0)::bigint AS withdrawing_min,
       coalesce(sum(withdrawing_max), 0)::bigint AS withdrawing_max
FROM per_merchant
"""


_PAID_WINDOW_QUERY = """
WITH paid_params AS (
    SELECT %s::timestamptz AS since,
           %s::timestamptz AS as_of,
           %s::numeric AS usd_micros_per_unit
)
SELECT coalesce(
           sum(
               round(
                   greatest(
                       coalesce(c.paid_micros, 0)
                           - coalesce(b.paid_micros, c.paid_micros, 0),
                       0
                   )::numeric * p.usd_micros_per_unit / m.quota_per_unit
               )
           ),
           0
       )::bigint AS paid_micros
FROM a6_trade.a6_financial_current c
JOIN a6_trade.a6_merchants m ON m.id = c.merchant_id
CROSS JOIN paid_params p
LEFT JOIN LATERAL (
    SELECT s.paid_micros
    FROM a6_trade.a6_financial_snapshots s
    WHERE s.merchant_id = c.merchant_id AND s.observed_at <= p.as_of
    ORDER BY
        (s.observed_at > p.since),
        CASE WHEN s.observed_at <= p.since THEN s.observed_at END DESC,
        CASE WHEN s.observed_at > p.since THEN s.observed_at END ASC
    LIMIT 1
) b ON true
"""


_MERCHANT_QUERY = """
WITH money AS (
    SELECT %s::numeric AS usd_micros_per_unit
), pin_probe_candidate_pairs AS (
    SELECT action.id AS action_id,
           event.id AS provider_event_id,
           event.merchant_id,
           event.request_id,
           event.quota_micros,
           count(*) OVER (PARTITION BY action.id) AS action_candidate_count,
           count(*) OVER (PARTITION BY event.id) AS event_candidate_count
    FROM a6_trade.trade_control_actions action
    JOIN a6_trade.a6_provider_usage_events event
      ON event.a6_token_ref_id = CASE
             WHEN action.entity_id ~ '^[0-9]+$' THEN action.entity_id::bigint
             ELSE NULL
         END
     AND event.model = action.desired_state ->> 'model'
     AND event.channel_id = CASE
             WHEN action.desired_state ->> 'channel_id' ~ '^[0-9]+$'
             THEN (action.desired_state ->> 'channel_id')::bigint
             ELSE NULL
         END
     AND event.occurred_at BETWEEN
         action.started_at - make_interval(secs => 2)
         AND action.started_at + make_interval(secs => 2)
    WHERE action.action_type = 'provider_pin_probe'
      AND action.entity_type = 'a6_provider_token'
      AND action.started_at >= %s - make_interval(secs => 2)
      AND event.probe_run_id IS NULL
), pin_probe_event AS (
    SELECT provider_event_id, merchant_id, request_id, quota_micros
    FROM pin_probe_candidate_pairs
    WHERE action_candidate_count = 1 AND event_candidate_count = 1
), probe_request AS (
    SELECT DISTINCT request_id
    FROM a6_trade.a6_provider_usage_events
    WHERE probe_run_id IS NOT NULL AND request_id IS NOT NULL
    UNION
    SELECT DISTINCT request_id
    FROM pin_probe_event
    WHERE request_id IS NOT NULL
), earning_flow AS (
    -- Earnings are split by who funded the request. Only 'external' is revenue;
    -- 'own_boost' is booster money returning to us minus the A6 platform cut, and
    -- counting it as income is what inflated the old profit figure.
    SELECT e.merchant_id,
           round(
               sum(e.earned_micros)::numeric * money.usd_micros_per_unit
               / m.quota_per_unit
           )::bigint AS earned_micros,
           round(
               coalesce(sum(e.earned_micros) FILTER (
                   WHERE e.counterparty_class = 'external'
               ), 0)::numeric * money.usd_micros_per_unit / m.quota_per_unit
           )::bigint AS external_earned_micros,
           round(
               coalesce(sum(e.earned_micros) FILTER (
                   WHERE e.counterparty_class = 'own_boost'
               ), 0)::numeric * money.usd_micros_per_unit / m.quota_per_unit
           )::bigint AS own_boost_earned_micros,
           round(
               coalesce(sum(e.earned_micros) FILTER (
                   WHERE e.counterparty_class = 'own_pin'
               ), 0)::numeric * money.usd_micros_per_unit / m.quota_per_unit
           )::bigint AS own_pin_earned_micros,
           round(
               coalesce(sum(e.earned_micros) FILTER (
                   WHERE e.counterparty_class = 'unknown'
               ), 0)::numeric * money.usd_micros_per_unit / m.quota_per_unit
           )::bigint AS unclassified_earned_micros,
           count(*)::bigint AS earning_event_count,
           count(*) FILTER (
               WHERE e.counterparty_class = 'unknown'
           )::bigint AS unclassified_earning_event_count
    FROM a6_trade.a6_earning_events e
    JOIN a6_trade.a6_merchants m ON m.id = e.merchant_id
    CROSS JOIN money
    WHERE e.occurred_at >= %s
      AND NOT EXISTS (
          SELECT 1 FROM probe_request p WHERE p.request_id = e.request_id
      )
    GROUP BY e.merchant_id, m.quota_per_unit, money.usd_micros_per_unit
), boost_flow AS (
    -- Gross cost of booster traffic. Paired with own_boost_earned_micros this gives
    -- the real net loss of the 抖加 mechanism.
    SELECT u.merchant_id,
           round(
               sum(u.quota_micros)::numeric * money.usd_micros_per_unit
               / m.quota_per_unit
           )::bigint AS boost_gross_cost_micros,
           count(*)::bigint AS boost_event_count
    FROM a6_trade.a6_provider_usage_events u
    JOIN a6_trade.a6_merchants m ON m.id = u.merchant_id
    JOIN a6_trade.a6_channel_booster_tokens booster
      ON booster.a6_token_ref_id = u.a6_token_ref_id
    CROSS JOIN money
    WHERE u.occurred_at >= %s
      AND NOT EXISTS (
          SELECT 1
          FROM a6_trade.enodot_account_bindings provider_binding
          WHERE provider_binding.a6_token_id = u.a6_token_ref_id
            AND provider_binding.enodot_account_id IS NOT NULL
            AND provider_binding.created_at <= u.occurred_at
      )
    GROUP BY u.merchant_id, m.quota_per_unit, money.usd_micros_per_unit
), provider_flow AS (
    SELECT u.merchant_id,
           round(
               coalesce(sum(u.quota_micros) FILTER (
                   WHERE u.probe_run_id IS NULL
                     AND NOT EXISTS (
                         SELECT 1 FROM pin_probe_event probe
                         WHERE probe.provider_event_id = u.id
                     )
               ), 0)::numeric * money.usd_micros_per_unit / m.quota_per_unit
           )::bigint AS provider_cost_micros,
           round(
               coalesce(sum(u.quota_micros) FILTER (
                   WHERE u.probe_run_id IS NULL
                     AND NOT EXISTS (
                         SELECT 1 FROM pin_probe_event probe
                         WHERE probe.provider_event_id = u.id
                     )
                     AND EXISTS (
                       SELECT 1 FROM a6_trade.enodot_account_bindings b
                       WHERE b.a6_token_id = u.a6_token_ref_id
                         AND b.enodot_account_id IS NOT NULL
                         AND b.created_at <= u.occurred_at
                   )
               ), 0)::numeric * money.usd_micros_per_unit / m.quota_per_unit
           )::bigint AS confirmed_provider_cost_micros,
           round(
               coalesce(sum(u.quota_micros) FILTER (
                   WHERE u.probe_run_id IS NULL
                     AND NOT EXISTS (
                         SELECT 1 FROM pin_probe_event probe
                         WHERE probe.provider_event_id = u.id
                     )
                     AND NOT EXISTS (
                       SELECT 1 FROM a6_trade.enodot_account_bindings b
                       WHERE b.a6_token_id = u.a6_token_ref_id
                         AND b.enodot_account_id IS NOT NULL
                         AND b.created_at <= u.occurred_at
                   )
               ), 0)::numeric * money.usd_micros_per_unit / m.quota_per_unit
           )::bigint AS unattributed_provider_cost_micros,
           count(*) FILTER (
               WHERE u.probe_run_id IS NULL
                 AND NOT EXISTS (
                     SELECT 1 FROM pin_probe_event probe
                     WHERE probe.provider_event_id = u.id
                 )
           )::bigint
               AS provider_event_count,
           count(*) FILTER (
               WHERE u.probe_run_id IS NULL
                 AND NOT EXISTS (
                     SELECT 1 FROM pin_probe_event probe
                     WHERE probe.provider_event_id = u.id
                 )
                 AND NOT EXISTS (
                   SELECT 1 FROM a6_trade.enodot_account_bindings b
                   WHERE b.a6_token_id = u.a6_token_ref_id
                     AND b.enodot_account_id IS NOT NULL
                     AND b.created_at <= u.occurred_at
               )
           )::bigint AS unattributed_provider_event_count
    FROM a6_trade.a6_provider_usage_events u
    JOIN a6_trade.a6_merchants m ON m.id = u.merchant_id
    CROSS JOIN money
    WHERE u.occurred_at >= %s
      AND NOT EXISTS (
          SELECT 1
          FROM a6_trade.a6_channel_booster_tokens booster_event
          WHERE booster_event.a6_token_ref_id = u.a6_token_ref_id
            AND NOT EXISTS (
                SELECT 1
                FROM a6_trade.enodot_account_bindings provider_binding
                WHERE provider_binding.a6_token_id = u.a6_token_ref_id
                  AND provider_binding.enodot_account_id IS NOT NULL
                  AND provider_binding.created_at <= u.occurred_at
            )
      )
    GROUP BY u.merchant_id, m.quota_per_unit, money.usd_micros_per_unit
), transfer_flow AS (
    SELECT i.merchant_id,
           round(
               sum(i.amount_micros)::numeric * money.usd_micros_per_unit
               / m.quota_per_unit
           )::bigint AS transfer_in_micros,
           count(*)::bigint AS transfer_in_count
    FROM a6_trade.a6_balance_transfer_intents i
    JOIN a6_trade.a6_merchants m ON m.id = i.merchant_id
    CROSS JOIN money
    WHERE EXISTS (
        SELECT 1 FROM a6_trade.a6_balance_transfer_confirmations c
        WHERE c.intent_id = i.id AND c.status = 'confirmed' AND c.observed_at >= %s
    )
    GROUP BY i.merchant_id, m.quota_per_unit, money.usd_micros_per_unit
), probe_provider_raw AS (
    SELECT probe_run_id, merchant_id, sum(quota_micros)::bigint AS gross_cost_quota
    FROM a6_trade.a6_provider_usage_events
    WHERE probe_run_id IS NOT NULL
    GROUP BY probe_run_id, merchant_id
), probe_provider AS (
    SELECT p.probe_run_id,
           sum(
               round(
                   p.gross_cost_quota::numeric * money.usd_micros_per_unit
                   / m.quota_per_unit
               )
           )::bigint AS gross_cost_micros
    FROM probe_provider_raw p
    JOIN a6_trade.a6_merchants m ON m.id = p.merchant_id
    CROSS JOIN money
    GROUP BY p.probe_run_id
), probe_recovery_raw AS (
    SELECT p.probe_run_id, e.merchant_id,
           sum(e.earned_micros)::bigint AS recovery_quota
    FROM (
        SELECT DISTINCT probe_run_id, request_id
        FROM a6_trade.a6_provider_usage_events
        WHERE probe_run_id IS NOT NULL AND request_id IS NOT NULL
    ) p
    JOIN a6_trade.a6_earning_events e ON e.request_id = p.request_id
    GROUP BY p.probe_run_id, e.merchant_id
), probe_recovery AS (
    SELECT p.probe_run_id,
           sum(
               round(
                   p.recovery_quota::numeric * money.usd_micros_per_unit
                   / m.quota_per_unit
               )
           )::bigint AS recovery_micros
    FROM probe_recovery_raw p
    JOIN a6_trade.a6_merchants m ON m.id = p.merchant_id
    CROSS JOIN money
    GROUP BY p.probe_run_id
), probe_flow AS (
    SELECT r.merchant_id,
           sum(
               greatest(
                   round(
                       r.cost_micros::numeric * money.usd_micros_per_unit
                       / m.quota_per_unit
                   )::bigint,
                   coalesce(pp.gross_cost_micros, 0)
               )
           )::bigint AS probe_gross_cost_micros,
           sum(coalesce(pr.recovery_micros, 0))::bigint AS probe_recovery_micros,
           sum(
               greatest(
                   round(
                       r.cost_micros::numeric * money.usd_micros_per_unit
                       / m.quota_per_unit
                   )::bigint,
                   coalesce(pp.gross_cost_micros, 0)
               )
               - coalesce(pr.recovery_micros, 0)
           )::bigint AS probe_loss_micros
    FROM a6_trade.trade_probe_runs r
    JOIN a6_trade.a6_merchants m ON m.id = r.merchant_id
    CROSS JOIN money
    LEFT JOIN probe_provider pp ON pp.probe_run_id = r.id
    LEFT JOIN probe_recovery pr ON pr.probe_run_id = r.id
    WHERE r.created_at >= %s AND r.status IN ('completed', 'partial')
    GROUP BY r.merchant_id
), pin_probe_flow AS (
    SELECT probe.merchant_id,
           round(
               sum(probe.quota_micros)::numeric * money.usd_micros_per_unit
               / merchant.quota_per_unit
           )::bigint AS pin_probe_cost_micros,
           count(*)::bigint AS pin_probe_event_count
    FROM pin_probe_event probe
    JOIN a6_trade.a6_merchants merchant ON merchant.id = probe.merchant_id
    CROSS JOIN money
    GROUP BY probe.merchant_id, merchant.quota_per_unit, money.usd_micros_per_unit
), withdrawal_fee_flow AS (
    SELECT w.merchant_id,
           round(
               sum(coalesce(w.fee_micros, 0))::numeric * money.usd_micros_per_unit
               / m.quota_per_unit
           )::bigint AS withdrawal_fee_micros
    FROM a6_trade.a6_withdrawal_events w
    JOIN a6_trade.a6_merchants m ON m.id = w.merchant_id
    CROSS JOIN money
    WHERE w.completed_at >= %s
    GROUP BY w.merchant_id, m.quota_per_unit, money.usd_micros_per_unit
), latest_exchange AS (
    SELECT coalesce(
               q.exchange_rate,
               q.realtime_ratio_cny / nullif(q.realtime_ratio_usd, 0),
               q.realtime_ratio_cny / nullif(q.final_multiplier, 0)
           ) AS exchange_rate
    FROM a6_trade.a6_market_quotes q
    WHERE q.exchange_rate IS NOT NULL
       OR (
           q.realtime_ratio_cny IS NOT NULL
           AND coalesce(q.realtime_ratio_usd, q.final_multiplier) IS NOT NULL
       )
    ORDER BY q.observed_at DESC, q.id DESC
    LIMIT 1
), channel_name_policy AS (
    SELECT %s::text AS forbidden_name,
           %s::text AS generated_name_pattern,
           %s::text AS generated_name_signal_pattern
), active_seller_channels AS (
    SELECT channel.merchant_id, channel.a6_channel_id
    FROM a6_trade.a6_supplier_channels_current channel
    JOIN a6_trade.a6_merchants seller ON seller.id = channel.merchant_id
    CROSS JOIN channel_name_policy policy
    WHERE seller.status = 'supplier_submitted'
      AND seller.roles @> ARRAY['seller']::text[]
      AND seller.last_poll_at IS NOT NULL
      AND channel.observed_at = seller.last_poll_at
      AND channel.status = 1
      AND channel.availability = 1
      AND channel.channel_type = 1
      AND btrim(channel.name) <> policy.forbidden_name
      AND NOT (
          btrim(channel.name) ~ policy.generated_name_pattern
          AND btrim(channel.name) ~ policy.generated_name_signal_pattern
      )
      AND NOT EXISTS (
          SELECT 1
          FROM a6_trade.a6_seller_retirement_plans completed_retirement
          WHERE completed_retirement.merchant_id = seller.id
            AND completed_retirement.state = 'completed'
      )
)
SELECT m.id AS merchant_id,
       coalesce(
           nullif(m.supplier_name, ''),
           'merchant-' || lpad(m.id::text, 4, '0')
       ) AS display_name,
       m.roles, m.status AS session_status,
       retirement.state AS retirement_state,
       retirement.archive_mode AS retirement_archive_mode,
       retirement.requested_at AS retirement_requested_at,
       retirement.updated_at AS retirement_updated_at,
       retirement.completed_at AS retirement_completed_at,
       retirement.last_error_class AS retirement_error_class,
       CASE WHEN m.proxy_lease_id IS NULL THEN 'unknown' ELSE 'healthy' END AS proxy_status,
       f.observed_at AS last_observed_at, m.poll_error_count,
       incident.incident_class AS last_error_class,
       incident.last_seen_at AS last_error_at,
       binding.cooldown_until,
       pin.model AS current_pin_model, pin.channel_id AS current_pin_channel_id,
       coalesce(pin_rates.items, '[]'::jsonb) AS pin_rates,
       coalesce(listing_rates.items, '[]'::jsonb) AS listing_rates,
       round(
           coalesce(f.user_balance_quota, 0)::numeric
           * money.usd_micros_per_unit / m.quota_per_unit
       )::bigint AS user_balance_micros,
       round(
           coalesce(f.withdrawable_micros, 0)::numeric
           * money.usd_micros_per_unit / m.quota_per_unit
       )::bigint AS withdrawable_micros,
       round(
           coalesce(f.withdrawing_micros, 0)::numeric
           * money.usd_micros_per_unit / m.quota_per_unit
       )::bigint AS withdrawing_micros,
       round(
           coalesce(f.lifetime_earned_micros, 0)::numeric
           * money.usd_micros_per_unit / m.quota_per_unit
       )::bigint AS lifetime_earned_micros,
       round(
           coalesce(f.paid_micros, 0)::numeric
           * money.usd_micros_per_unit / m.quota_per_unit
       )::bigint AS paid_micros,
       round(
           coalesce(f.today_earned_micros, 0)::numeric
           * money.usd_micros_per_unit / m.quota_per_unit
       )::bigint AS today_earned_micros,
       round(
           coalesce(f.month_earned_micros, 0)::numeric
           * money.usd_micros_per_unit / m.quota_per_unit
       )::bigint AS month_earned_micros,
       coalesce(e.earned_micros, 0)::bigint AS earned_micros,
       coalesce(tf.transfer_in_micros, 0)::bigint AS transfer_in_micros,
       coalesce(tf.transfer_in_count, 0)::bigint AS transfer_in_count,
       coalesce(pf.provider_cost_micros, 0)::bigint AS provider_cost_micros,
       coalesce(pf.confirmed_provider_cost_micros, 0)::bigint
           AS confirmed_provider_cost_micros,
       coalesce(pf.unattributed_provider_cost_micros, 0)::bigint
           AS unattributed_provider_cost_micros,
       coalesce(pf.provider_event_count, 0)::bigint AS provider_event_count,
       coalesce(pf.unattributed_provider_event_count, 0)::bigint
           AS unattributed_provider_event_count,
       -- Revenue is external earnings only. Booster money returning to us is not
       -- income; it is reported separately as a net loss.
       coalesce(e.external_earned_micros, 0)::bigint AS seller_income_micros,
       coalesce(e.external_earned_micros, 0)::bigint AS external_earned_micros,
       coalesce(e.own_boost_earned_micros, 0)::bigint AS own_boost_earned_micros,
       coalesce(e.own_pin_earned_micros, 0)::bigint AS own_pin_earned_micros,
       coalesce(e.unclassified_earned_micros, 0)::bigint
           AS unclassified_earned_micros,
       coalesce(e.earning_event_count, 0)::bigint AS earning_event_count,
       coalesce(e.unclassified_earning_event_count, 0)::bigint
           AS unclassified_earning_event_count,
       coalesce(bf.boost_gross_cost_micros, 0)::bigint AS boost_gross_cost_micros,
       coalesce(bf.boost_event_count, 0)::bigint AS boost_event_count,
       (
           coalesce(bf.boost_gross_cost_micros, 0)
           - coalesce(e.own_boost_earned_micros, 0)
       )::bigint AS boost_net_loss_micros,
       (
           coalesce(pr.probe_gross_cost_micros, 0)
           + coalesce(pin_probe.pin_probe_cost_micros, 0)
       )::bigint AS probe_gross_cost_micros,
       coalesce(pr.probe_recovery_micros, 0)::bigint AS probe_recovery_micros,
       (
           coalesce(pr.probe_loss_micros, 0)
           + coalesce(pin_probe.pin_probe_cost_micros, 0)
       )::bigint AS probe_loss_micros,
       coalesce(pin_probe.pin_probe_cost_micros, 0)::bigint AS pin_probe_cost_micros,
       coalesce(pin_probe.pin_probe_event_count, 0)::bigint AS pin_probe_event_count,
       coalesce(wf.withdrawal_fee_micros, 0)::bigint AS withdrawal_fee_micros,
       coalesce(inventory.seller_channel_count, 0)::bigint AS seller_channel_count,
       coalesce(inventory.managed_channel_count, 0)::bigint AS managed_channel_count,
       coalesce(inventory.existing_unmanaged_channel_count, 0)::bigint
           AS existing_unmanaged_channel_count,
       coalesce(inventory.listing_total_count, 0)::bigint AS listing_total_count,
       coalesce(inventory.active_listing_count, 0)::bigint AS active_listing_count,
       coalesce(inventory.managed_listing_count, 0)::bigint AS managed_listing_count,
       coalesce(inventory.managed_active_listing_count, 0)::bigint
           AS managed_active_listing_count,
       coalesce(inventory.existing_unmanaged_listing_count, 0)::bigint
           AS existing_unmanaged_listing_count,
       coalesce(inventory.existing_unmanaged_active_listing_count, 0)::bigint
           AS existing_unmanaged_active_listing_count,
       coalesce(inventory.pending_listing_count, 0)::bigint AS pending_listing_count,
       coalesce(inventory.down_listing_count, 0)::bigint AS down_listing_count,
       inventory.listing_last_observed_at AS listing_last_observed_at,
       coalesce(tokens.provider_token_count, 0)::bigint AS provider_token_count,
       coalesce(binding.enodot_account_count, 0)::bigint AS enodot_account_count,
       withdraw_binding.state AS withdraw_account_state,
       withdraw_binding.network AS withdraw_account_network,
       withdraw_binding.address_masked AS withdraw_account_masked
FROM a6_trade.a6_merchants m
CROSS JOIN money
LEFT JOIN latest_exchange market_currency ON true
LEFT JOIN a6_trade.a6_financial_current f ON f.merchant_id = m.id
LEFT JOIN a6_trade.a6_seller_retirement_plans retirement
  ON retirement.merchant_id = m.id
LEFT JOIN earning_flow e ON e.merchant_id = m.id
LEFT JOIN boost_flow bf ON bf.merchant_id = m.id
LEFT JOIN provider_flow pf ON pf.merchant_id = m.id
LEFT JOIN transfer_flow tf ON tf.merchant_id = m.id
LEFT JOIN probe_flow pr ON pr.merchant_id = m.id
LEFT JOIN pin_probe_flow pin_probe ON pin_probe.merchant_id = m.id
LEFT JOIN withdrawal_fee_flow wf ON wf.merchant_id = m.id
LEFT JOIN LATERAL (
    SELECT p.model, p.channel_id FROM a6_trade.a6_pin_bindings p
    JOIN a6_trade.a6_tokens t ON t.id = p.a6_token_id
    WHERE t.merchant_id = m.id AND p.valid_to IS NULL
    ORDER BY p.confirmed_at DESC NULLS LAST LIMIT 1
) pin ON true
LEFT JOIN LATERAL (
    SELECT jsonb_agg(
        jsonb_build_object(
            'token_id', current_pin.a6_token_id,
            'model', current_pin.model,
            'channel_id', current_pin.channel_id,
            'rate_multiplier', coalesce(
                current_pin.market_final_multiplier,
                current_pin.expected_cost_multiplier
            ),
            'expected_cost_multiplier', current_pin.expected_cost_multiplier,
            'market_final_multiplier', current_pin.market_final_multiplier,
            'realtime_ratio_usd', coalesce(
                current_pin.realtime_ratio_usd,
                current_pin.market_final_multiplier,
                current_pin.expected_cost_multiplier
            ),
            'realtime_ratio_cny', current_pin.realtime_ratio_cny,
            'exchange_rate', current_pin.exchange_rate,
            'decision_status', current_pin.decision_status,
            'confirmed_at', current_pin.confirmed_at,
            'quote_observed_at', current_pin.quote_observed_at
        ) ORDER BY current_pin.model, current_pin.a6_token_id
    ) AS items
    FROM (
        SELECT t.a6_token_id,
               p.model,
               p.channel_id,
               d.expected_cost_multiplier,
               d.decision_status,
               coalesce(p.confirmed_at, p.valid_from) AS confirmed_at,
               quote.final_multiplier AS market_final_multiplier,
               quote.realtime_ratio_usd,
               quote.realtime_ratio_cny,
               quote.exchange_rate,
               quote.observed_at AS quote_observed_at
        FROM a6_trade.a6_pin_bindings p
        JOIN a6_trade.a6_tokens t ON t.id = p.a6_token_id
        LEFT JOIN a6_trade.a6_pin_decisions d ON d.id = p.decision_id
        LEFT JOIN LATERAL (
            SELECT CASE
                       WHEN q.final_multiplier IS NOT NULL THEN q.final_multiplier
                       WHEN q.model = 'gpt-5.6-sol' AND q.input_price IS NOT NULL
                       THEN q.input_price / 5000000::numeric
                       ELSE NULL
                   END AS final_multiplier,
                   coalesce(
                       q.realtime_ratio_usd,
                       q.final_multiplier,
                       CASE
                           WHEN q.model = 'gpt-5.6-sol' AND q.input_price IS NOT NULL
                           THEN q.input_price / 5000000::numeric
                           ELSE NULL
                       END
                   ) AS realtime_ratio_usd,
                   coalesce(
                       q.realtime_ratio_cny,
                       coalesce(
                           q.realtime_ratio_usd,
                           q.final_multiplier,
                           CASE
                               WHEN q.model = 'gpt-5.6-sol' AND q.input_price IS NOT NULL
                               THEN q.input_price / 5000000::numeric
                               ELSE NULL
                           END
                       ) * q.exchange_rate
                   ) AS realtime_ratio_cny,
                   coalesce(
                       q.exchange_rate,
                       q.realtime_ratio_cny / nullif(q.realtime_ratio_usd, 0),
                       q.realtime_ratio_cny / nullif(q.final_multiplier, 0)
                   ) AS exchange_rate,
                   q.observed_at
            FROM a6_trade.a6_market_quotes q
            WHERE q.channel_id = p.channel_id AND q.model = p.model
            ORDER BY q.observed_at DESC LIMIT 1
        ) quote ON true
        WHERE t.merchant_id = m.id AND p.valid_to IS NULL
    ) current_pin
) pin_rates ON true
LEFT JOIN LATERAL (
    SELECT jsonb_agg(
        jsonb_build_object(
            'listing_id', l.a6_listing_id,
            'channel_id', l.a6_channel_id,
            'model', l.platform_model,
            'managed_state', l.managed_state,
            'control_state', managed.control_state,
            'desired_delta', seller.desired_delta,
            'observed_delta', seller.observed_delta,
            'price_multiplier', CASE
                WHEN l.platform_model = 'gpt-5.6-sol'
                THEN l.input_price_micros::numeric / 5000000::numeric
                ELSE NULL
            END,
            'market_price_multiplier', CASE
                WHEN l.platform_model = 'gpt-5.6-sol'
                THEN l.input_price_micros::numeric / 5000000::numeric
                     / 0.833333333333::numeric
                ELSE NULL
            END,
            'price_multiplier_cny', CASE
                WHEN l.platform_model = 'gpt-5.6-sol'
                     AND market_currency.exchange_rate IS NOT NULL
                THEN l.input_price_micros::numeric / 5000000::numeric
                     / 0.833333333333::numeric
                     * market_currency.exchange_rate
                ELSE NULL
            END,
            'exchange_rate', market_currency.exchange_rate,
            'input_price_micros', l.input_price_micros,
            'output_price_micros', l.output_price_micros,
            'cache_read_price_micros', l.cache_read_price_micros,
            'cache_write_price_micros', l.cache_write_price_micros,
            'status', l.status,
            'availability', l.availability,
            'visibility', l.visibility
        ) || jsonb_build_object(
            'health_state', signal.health_state,
            'health_observer_count', health_quorum.observer_count,
            'health_confirmation_count', health_quorum.confirmation_count,
            'market_available', signal.available,
            'route_status', signal.route_status,
            'circuit_state', signal.circuit_state,
            'circuit_probe_status', signal.circuit_probe_status,
            'circuit_last_error_kind', signal.circuit_last_error_kind,
            'circuit_probe_success_required', signal.circuit_probe_success_required,
            'circuit_probe_success_streak', signal.circuit_probe_success_streak,
            'circuit_scope', signal.circuit_scope,
            'circuit_recovery_mode', signal.circuit_recovery_mode,
            'circuit_cooling_until', signal.circuit_cooling_until,
            'circuit_last_cooling_at', signal.circuit_last_cooling_at,
            'circuit_last_probe_at', signal.circuit_last_probe_at,
            'circuit_next_probe_at', signal.circuit_next_probe_at,
            'cooldown_state', signal.cooldown_state,
            'cooldown_label', signal.cooldown_label,
            'cooldown_remaining_seconds', signal.cooldown_remaining_seconds,
            'cooldown_expected_recovery_at', signal.cooldown_expected_recovery_at,
            'cooldown_next_probe_at', signal.cooldown_next_probe_at,
            'cooldown_sources', signal.cooldown_sources,
            'smart_routing_status', signal.smart_routing_status,
            'smart_routing_labels', signal.smart_routing_labels,
            'last_success_at', signal.last_success_at,
            'last_failure_at', signal.last_failure_at,
            'last_failure_code', signal.last_failure_code,
            'last_sample_at', signal.last_sample_at,
            'sample_count', signal.sample_count,
            'recent_success_rate', signal.recent_success_rate,
            'recent_p50_ms', signal.recent_p50_ms,
            'p50_ttft_ms', signal.p50_ttft_ms,
            'health_observed_at', signal.last_observed_at,
            'growth_enabled', growth_policy.enabled,
            'growth_action_type', growth_action.action_type,
            'growth_action_state', growth_action.state,
            'growth_request_count', growth_action.request_count,
            'growth_success_count', growth_action.success_count,
            'growth_failure_count', growth_action.failure_count,
            'growth_estimated_cost_micros', growth_action.estimated_cost_micros,
            'growth_actual_cost_micros', growth_action.actual_cost_micros,
            'growth_error_class', growth_action.error_class,
            'growth_started_at', growth_action.started_at,
            'growth_completed_at', growth_action.completed_at,
            'growth_next_allowed_at', growth_action.next_allowed_at,
            'observed_at', l.observed_at
        ) ORDER BY l.platform_model, l.a6_listing_id
    ) AS items
    FROM a6_trade.a6_supplier_listings_current l
    LEFT JOIN a6_trade.a6_managed_listing_bindings managed
      ON managed.merchant_id = l.merchant_id
     AND managed.a6_listing_id = l.a6_listing_id
    LEFT JOIN a6_trade.enodot_managed_seller_resources seller
      ON seller.merchant_id = l.merchant_id
    LEFT JOIN a6_trade.a6_market_channel_signals_current signal
      ON signal.model = l.platform_model
     AND signal.channel_id = l.a6_channel_id
    LEFT JOIN LATERAL (
        SELECT
            count(*)::integer AS observer_count,
            count(*) FILTER (
                WHERE observer_health.health_state = signal.health_state
            )::integer AS confirmation_count
        FROM a6_trade.a6_channel_observer_health_current observer_health
        WHERE observer_health.model = signal.model
          AND observer_health.channel_id = signal.channel_id
          AND observer_health.observed_at >= now() - interval '45 seconds'
    ) health_quorum ON true
    LEFT JOIN a6_trade.a6_channel_growth_policies growth_policy
      ON growth_policy.listing_binding_id = managed.id
    LEFT JOIN LATERAL (
        SELECT action.*
        FROM a6_trade.a6_channel_growth_actions action
        WHERE action.listing_binding_id = managed.id
        ORDER BY action.started_at DESC, action.id DESC
        LIMIT 1
    ) growth_action ON true
    WHERE l.merchant_id = m.id
) listing_rates ON true
LEFT JOIN LATERAL (
    SELECT max(b.cooldown_until) AS cooldown_until,
           count(*) FILTER (WHERE b.enodot_account_id IS NOT NULL)::bigint AS enodot_account_count
    FROM a6_trade.enodot_account_bindings b
    JOIN a6_trade.a6_tokens t ON t.id = b.a6_token_id WHERE t.merchant_id = m.id
) binding ON true
LEFT JOIN LATERAL (
    SELECT count(*)::bigint AS provider_token_count
    FROM a6_trade.a6_tokens t WHERE t.merchant_id = m.id
) tokens ON true
LEFT JOIN a6_trade.a6_withdraw_account_bindings withdraw_binding
  ON withdraw_binding.merchant_id = m.id
LEFT JOIN LATERAL (
    SELECT
        (
            SELECT count(*)
            FROM active_seller_channels c
            WHERE c.merchant_id = m.id
        )::bigint AS seller_channel_count,
        (
            SELECT count(*)
            FROM active_seller_channels c
            JOIN a6_trade.a6_managed_channel_bindings b
              ON b.merchant_id = c.merchant_id
             AND b.a6_channel_id = c.a6_channel_id
             AND b.control_state = 'verified'
             AND b.managed_uid <> ''
             AND b.verified_at IS NOT NULL
            WHERE c.merchant_id = m.id
        )::bigint AS managed_channel_count,
        (
            SELECT count(*)
            FROM active_seller_channels c
            WHERE c.merchant_id = m.id
              AND NOT EXISTS (
                  SELECT 1
                  FROM a6_trade.a6_managed_channel_bindings b
                  WHERE b.merchant_id = c.merchant_id
                    AND b.a6_channel_id = c.a6_channel_id
                    AND b.control_state = 'verified'
                    AND b.managed_uid <> ''
                    AND b.verified_at IS NOT NULL
              )
        )::bigint AS existing_unmanaged_channel_count,
        count(*)::bigint AS listing_total_count,
        count(*) FILTER (
            WHERE l.status = 2 AND l.availability = 1 AND l.visibility = 1
        )::bigint AS active_listing_count,
        count(*) FILTER (WHERE managed.a6_listing_id IS NOT NULL)::bigint
            AS managed_listing_count,
        count(*) FILTER (
            WHERE managed.a6_listing_id IS NOT NULL
              AND l.status = 2 AND l.availability = 1 AND l.visibility = 1
        )::bigint AS managed_active_listing_count,
        count(*) FILTER (WHERE managed.a6_listing_id IS NULL)::bigint
            AS existing_unmanaged_listing_count,
        count(*) FILTER (
            WHERE managed.a6_listing_id IS NULL
              AND l.status = 2 AND l.availability = 1 AND l.visibility = 1
        )::bigint AS existing_unmanaged_active_listing_count,
        count(*) FILTER (WHERE l.status NOT IN (0, 2))::bigint AS pending_listing_count,
        count(*) FILTER (WHERE l.status = 0)::bigint AS down_listing_count,
        max(l.observed_at) AS listing_last_observed_at
    FROM a6_trade.a6_supplier_listings_current l
    LEFT JOIN LATERAL (
        SELECT b.a6_listing_id
        FROM a6_trade.a6_managed_listing_bindings b
        WHERE b.merchant_id = l.merchant_id
          AND b.a6_listing_id = l.a6_listing_id
          AND b.control_state = 'verified'
          AND b.managed_uid <> ''
          AND b.verified_at IS NOT NULL
        LIMIT 1
    ) managed ON true
    WHERE l.merchant_id = m.id
      AND l.observed_at = f.observed_at
) inventory ON true
LEFT JOIN LATERAL (
    SELECT i.incident_class, i.last_seen_at FROM a6_trade.trade_incidents i
    WHERE i.entity_type = 'merchant'
      AND i.entity_id = m.id::text
      AND i.status = 'open'
    ORDER BY i.last_seen_at DESC LIMIT 1
) incident ON true
WHERE m.status <> 'abandoned'
ORDER BY m.id
"""


_USAGE_LOGS_READABLE_QUERY = """
SELECT EXISTS (
    SELECT 1
    FROM pg_catalog.pg_class c
    JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'public'
      AND c.relname = 'usage_logs'
      AND c.relkind IN ('r', 'p', 'v', 'm', 'f')
      AND has_schema_privilege(n.oid, 'USAGE')
      AND has_table_privilege(c.oid, 'SELECT')
) AS readable
"""


_ENODOT_SALES_QUERY = """
WITH attribution_params AS (
    SELECT %s::numeric AS usd_micros_per_unit,
           %s::timestamptz AS since,
           make_interval(secs => 5) AS match_tolerance
), managed_keys AS (
    SELECT enodot_api_key_id AS api_key_id
    FROM a6_trade.enodot_managed_seller_resources
    WHERE enodot_api_key_id IS NOT NULL
), internal_rules AS (
    SELECT id, enodot_user_id, enodot_api_key_id, classification,
           valid_from, valid_to
    FROM a6_trade.enodot_usage_classification_rules
), bound_usage AS (
    SELECT u.id AS usage_log_id,
           t.merchant_id,
           b.a6_token_id AS a6_token_ref_id,
           CASE
               WHEN managed.api_key_id IS NOT NULL THEN 'managed_internal'
               ELSE coalesce(internal.classification, 'external')
           END AS usage_class,
           u.model,
           u.requested_model,
           u.upstream_model,
           coalesce(u.input_tokens, 0)
               + coalesce(u.cache_read_tokens, 0)
               + coalesce(u.cache_creation_tokens, 0) AS prompt_tokens,
           coalesce(u.output_tokens, 0) AS completion_tokens,
           u.actual_cost,
           u.created_at
    FROM public.usage_logs u
    CROSS JOIN attribution_params params
    JOIN a6_trade.enodot_account_bindings b
      ON b.enodot_account_id = u.account_id AND b.created_at <= u.created_at
    JOIN a6_trade.a6_tokens t ON t.id = b.a6_token_id
    LEFT JOIN managed_keys managed ON managed.api_key_id = u.api_key_id
    LEFT JOIN LATERAL (
        SELECT rule.classification
        FROM internal_rules rule
        WHERE (rule.enodot_user_id IS NULL OR rule.enodot_user_id = u.user_id)
          AND (rule.enodot_api_key_id IS NULL OR rule.enodot_api_key_id = u.api_key_id)
          AND rule.valid_from <= u.created_at
          AND (rule.valid_to IS NULL OR u.created_at < rule.valid_to)
        ORDER BY
            ((rule.enodot_user_id IS NOT NULL)::integer
             + (rule.enodot_api_key_id IS NOT NULL)::integer) DESC,
            rule.valid_from DESC,
            rule.id DESC
        LIMIT 1
    ) internal ON true
    WHERE u.created_at >= params.since
), usage_match_keys AS MATERIALIZED (
    SELECT usage.*,
           candidate_model.model AS candidate_model,
           floor(extract(epoch FROM usage.created_at) / 5)::bigint
               + bucket.delta AS match_bucket
    FROM bound_usage usage
    CROSS JOIN LATERAL (
        SELECT DISTINCT value::text AS model
        FROM (
            VALUES (usage.model), (usage.requested_model), (usage.upstream_model)
        ) candidates(value)
        WHERE value IS NOT NULL
    ) candidate_model
    CROSS JOIN (VALUES (-1::bigint), (0::bigint), (1::bigint)) bucket(delta)
), provider_events AS (
    SELECT event.id AS provider_event_id,
           event.merchant_id,
           event.a6_token_ref_id,
           event.model,
           event.channel_id,
           event.prompt_tokens,
           event.completion_tokens,
           event.quota_micros,
           event.occurred_at,
           floor(extract(epoch FROM event.occurred_at) / 5)::bigint AS match_bucket,
           merchant.quota_per_unit
    FROM a6_trade.a6_provider_usage_events event
    CROSS JOIN attribution_params params
    JOIN a6_trade.a6_merchants merchant ON merchant.id = event.merchant_id
    JOIN a6_trade.enodot_account_bindings binding
      ON binding.a6_token_id = event.a6_token_ref_id
     AND binding.created_at <= event.occurred_at
    WHERE event.occurred_at >= params.since
      AND event.probe_run_id IS NULL
      AND event.result_class = '2'
), pin_probe_candidate_pairs AS (
    SELECT action.id AS action_id,
           event.provider_event_id,
           count(*) OVER (PARTITION BY action.id) AS action_candidate_count,
           count(*) OVER (PARTITION BY event.provider_event_id)
               AS event_candidate_count
    FROM a6_trade.trade_control_actions action
    CROSS JOIN attribution_params params
    JOIN provider_events event
     ON event.a6_token_ref_id = CASE
             WHEN action.entity_id ~ '^[0-9]+$' THEN action.entity_id::bigint
             ELSE NULL
         END
     AND event.model = action.desired_state ->> 'model'
     AND event.channel_id = CASE
             WHEN action.desired_state ->> 'channel_id' ~ '^[0-9]+$'
             THEN (action.desired_state ->> 'channel_id')::bigint
             ELSE NULL
         END
     AND event.occurred_at BETWEEN
         action.started_at - make_interval(secs => 2)
         AND action.started_at + make_interval(secs => 2)
    WHERE action.action_type = 'provider_pin_probe'
      AND action.entity_type = 'a6_provider_token'
      AND action.started_at >= params.since - make_interval(secs => 2)
      AND event.provider_event_id IS NOT NULL
), pin_probe_event AS (
    SELECT provider_event_id
    FROM pin_probe_candidate_pairs
    WHERE action_candidate_count = 1 AND event_candidate_count = 1
), candidate_pairs AS (
    SELECT usage.usage_log_id,
           usage.merchant_id,
           usage.usage_class,
           event.provider_event_id,
           event.quota_micros,
           event.quota_per_unit,
           count(*) OVER (PARTITION BY usage.usage_log_id)
               AS usage_candidate_count,
           count(*) OVER (PARTITION BY event.provider_event_id)
               AS provider_event_candidate_count
    FROM usage_match_keys usage
    CROSS JOIN attribution_params params
    JOIN provider_events event
      ON event.merchant_id = usage.merchant_id
     AND event.a6_token_ref_id = usage.a6_token_ref_id
     AND event.model = usage.candidate_model
     AND event.match_bucket = usage.match_bucket
     AND event.occurred_at BETWEEN
         usage.created_at - params.match_tolerance
         AND usage.created_at + params.match_tolerance
     AND (
         event.prompt_tokens IS NULL
         OR event.prompt_tokens = usage.prompt_tokens
     )
     AND (
         event.completion_tokens IS NULL
         OR event.completion_tokens = usage.completion_tokens
     )
    WHERE NOT EXISTS (
        SELECT 1 FROM pin_probe_event probe
        WHERE probe.provider_event_id = event.provider_event_id
    )
), unique_matches AS (
    SELECT *
    FROM candidate_pairs
    WHERE usage_candidate_count = 1
      AND provider_event_candidate_count = 1
), attributed AS (
    SELECT usage.*,
           matched.provider_event_id,
           matched.quota_micros,
           matched.quota_per_unit
    FROM bound_usage usage
    LEFT JOIN unique_matches matched ON matched.usage_log_id = usage.usage_log_id
)
SELECT merchant_id,
       coalesce(
           round(
               sum(actual_cost) FILTER (WHERE usage_class = 'external')
               * params.usd_micros_per_unit
           ),
           0
       )::bigint AS external_enodot_revenue_micros,
       count(*) FILTER (WHERE usage_class = 'external')::bigint
           AS external_enodot_request_count,
       coalesce(
           round(
               sum(actual_cost) FILTER (
                   WHERE usage_class = 'external' AND provider_event_id IS NOT NULL
               ) * params.usd_micros_per_unit
           ),
           0
       )::bigint AS confirmed_external_enodot_revenue_micros,
       count(*) FILTER (
           WHERE usage_class = 'external' AND provider_event_id IS NOT NULL
       )::bigint AS confirmed_external_enodot_request_count,
       coalesce(
           round(
               sum(actual_cost) FILTER (
                   WHERE usage_class = 'external' AND provider_event_id IS NULL
               ) * params.usd_micros_per_unit
           ),
           0
       )::bigint AS unattributed_external_enodot_revenue_micros,
       count(*) FILTER (
           WHERE usage_class = 'external' AND provider_event_id IS NULL
       )::bigint AS unattributed_external_enodot_request_count,
       coalesce(
           round(
               sum(quota_micros::numeric / quota_per_unit) FILTER (
                   WHERE usage_class = 'external' AND provider_event_id IS NOT NULL
               ) * params.usd_micros_per_unit
           ),
           0
       )::bigint AS external_provider_cost_micros,
       count(*) FILTER (
           WHERE usage_class = 'external' AND provider_event_id IS NOT NULL
       )::bigint AS external_provider_event_count,
       coalesce(
           round(
               sum(quota_micros::numeric / quota_per_unit) FILTER (
                   WHERE usage_class = 'managed_internal'
                     AND provider_event_id IS NOT NULL
               ) * params.usd_micros_per_unit
           ),
           0
       )::bigint AS managed_internal_provider_cost_micros,
       count(*) FILTER (
           WHERE usage_class = 'managed_internal' AND provider_event_id IS NOT NULL
       )::bigint AS managed_internal_provider_event_count,
       coalesce(
           round(
               sum(actual_cost) FILTER (
                   WHERE usage_class IN ('owner_activation', 'internal_operation')
               ) * params.usd_micros_per_unit
           ),
           0
       )::bigint AS owner_internal_debit_micros,
       count(*) FILTER (
           WHERE usage_class IN ('owner_activation', 'internal_operation')
       )::bigint AS owner_internal_request_count,
       coalesce(
           round(
               sum(quota_micros::numeric / quota_per_unit) FILTER (
                   WHERE usage_class IN ('owner_activation', 'internal_operation')
                     AND provider_event_id IS NOT NULL
               ) * params.usd_micros_per_unit
           ),
           0
       )::bigint AS owner_internal_provider_cost_micros,
       count(*) FILTER (
           WHERE usage_class IN ('owner_activation', 'internal_operation')
             AND provider_event_id IS NOT NULL
       )::bigint AS owner_internal_provider_event_count
FROM attributed
CROSS JOIN attribution_params params
GROUP BY merchant_id, params.usd_micros_per_unit
"""


_ENODOT_PROVIDER_COST_QUERY = """
WITH internal_debit_params AS (
    SELECT %s::numeric AS usd_micros_per_unit,
           %s::timestamptz AS since
), managed_internal_debits AS (
    SELECT managed.merchant_id,
           u.actual_cost
    FROM public.usage_logs u
    CROSS JOIN internal_debit_params params
    JOIN a6_trade.enodot_managed_seller_resources managed
      ON managed.enodot_api_key_id = u.api_key_id
    WHERE u.created_at >= params.since
)
SELECT merchant_id,
       coalesce(
           round(sum(actual_cost) * params.usd_micros_per_unit),
           0
       )::bigint AS managed_internal_debit_micros,
       count(*)::bigint AS managed_internal_request_count
FROM managed_internal_debits
CROSS JOIN internal_debit_params params
GROUP BY merchant_id, params.usd_micros_per_unit
"""
