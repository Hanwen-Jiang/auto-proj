"""Structured fact writes for Priority 1 economic routing.

The evaluator only consumes immutable contracts and snapshots. This module is
the narrow write boundary used by collectors and operator tooling so quota
fields cannot be assembled from separate observations.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from .trade_economic_source import (
    ChannelRevenueSnapshot,
    EconomicRoutingError,
    EconomicSource,
    SourceCostContract,
    SourceQuotaSnapshot,
)

DEFAULT_FX_CNY_PER_USD = Decimal("7.2")
DEFAULT_REVENUE_WINDOW_SECONDS = 900
_SETTLEMENT_CONTRACT_REF = "sub2api-usage-settlement-v1"
_REVENUE_EPOCH = "rolling-usage-v1"
_REVENUE_PRECISION = Decimal("0.000000000001")


def compute_channel_net_k_cny(
    total_cost_usd: Decimal | str | int,
    total_tokens: int,
    *,
    fx_cny_per_usd: Decimal | str | int = DEFAULT_FX_CNY_PER_USD,
) -> Decimal:
    """Net channel revenue per 1K billed tokens in CNY.

    Revenue basis is the billed amount (usage_logs.total_cost) rather than the
    upstream account's actual cost, so the channel's own price realization is
    compared against source cost in the same K_CNY coordinate.
    """
    try:
        cost = Decimal(str(total_cost_usd))
    except Exception:
        raise EconomicRoutingError("economic_revenue_cost_invalid") from None
    try:
        fx = Decimal(str(fx_cny_per_usd))
    except Exception:
        raise EconomicRoutingError("economic_revenue_fx_invalid") from None
    if (
        not cost.is_finite()
        or cost < 0
        or not fx.is_finite()
        or fx <= 0
        or isinstance(total_tokens, bool)
        or not isinstance(total_tokens, int)
        or total_tokens < 0
    ):
        raise EconomicRoutingError("economic_revenue_evidence_invalid")
    if total_tokens == 0 or cost == 0:
        raise EconomicRoutingError("economic_revenue_evidence_empty")
    k_units = Decimal(total_tokens) / Decimal(1000)
    return (cost * fx / k_units).quantize(_REVENUE_PRECISION, rounding=ROUND_HALF_UP)


class PostgresChannelRevenueReconciler:
    """Derive rolling per-channel K_CNY from usage billing and record snapshots.

    Channel identity is the merchant seller resource's Enodot API key as it
    appears in the Sub2API usage ledger; revenue is summed over a rolling window
    so the economic generation always has fresh, positive revenue evidence for
    channels with traffic. Channels without traffic produce no snapshot and stay
    below the profit gate.
    """

    def __init__(
        self,
        connection_factory: Callable[[], Any],
        *,
        store: EconomicFactStore | None = None,
    ) -> None:
        if not callable(connection_factory):
            raise EconomicRoutingError("economic_connection_factory_invalid")
        self.connection_factory = connection_factory
        self.store = store or EconomicFactStore(connection_factory)

    def reconcile(
        self,
        model: str,
        *,
        now: datetime,
        fx_cny_per_usd: Decimal | str | int = DEFAULT_FX_CNY_PER_USD,
        usage_window_seconds: int = DEFAULT_REVENUE_WINDOW_SECONDS,
        settlement_contract_ref: str = _SETTLEMENT_CONTRACT_REF,
    ) -> dict[str, int | list[str]]:
        model = str(model).strip()
        if not model:
            raise EconomicRoutingError("economic_model_invalid")
        if (
            isinstance(usage_window_seconds, bool)
            or not isinstance(usage_window_seconds, int)
            or not 1 <= usage_window_seconds <= 86_400
        ):
            raise EconomicRoutingError("economic_revenue_window_invalid")
        window_start = now - timedelta(seconds=usage_window_seconds)
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT avg(
                        realtime_ratio_cny / nullif(realtime_ratio_usd, 0)
                    ) AS market_fx
                    FROM a6_trade.a6_market_quotes
                    WHERE model = %s
                      AND observed_at >= %s
                      AND realtime_ratio_usd > 0
                      AND realtime_ratio_cny > 0
                    """,
                    (model, window_start),
                )
                market_rows = cursor.fetchall()
                market_fx = None
                if market_rows:
                    raw = market_rows[0]
                    value = (
                        raw["market_fx"]
                        if isinstance(raw, Mapping)
                        else (raw[0] if raw else None)
                    )
                    if value is not None:
                        market_fx = Decimal(str(value))
                cursor.execute(
                    """
                    SELECT u.api_key_id,
                           r.merchant_id,
                           cb.a6_channel_id,
                           sum(u.total_cost) AS total_cost,
                           sum(
                               u.input_tokens + u.output_tokens
                               + u.cache_read_tokens + u.cache_creation_tokens
                               + u.cache_creation_5m_tokens + u.cache_creation_1h_tokens
                           ) AS total_tokens
                    FROM public.usage_logs u
                    JOIN a6_trade.enodot_managed_seller_resources r
                      ON r.observed_state->>'enodot_api_key_id' = u.api_key_id::text
                    LEFT JOIN a6_trade.a6_managed_channel_bindings cb
                      ON cb.merchant_id = r.merchant_id
                    WHERE u.model = %s
                      AND u.created_at >= %s
                      AND u.total_cost > 0
                    GROUP BY u.api_key_id, r.merchant_id, cb.a6_channel_id
                    HAVING sum(u.input_tokens + u.output_tokens
                                + u.cache_read_tokens + u.cache_creation_tokens
                                + u.cache_creation_5m_tokens + u.cache_creation_1h_tokens) > 0
                    """,
                    (model, window_start),
                )
                rows = cursor.fetchall()
        except Exception as exc:
            code = str(getattr(exc, "code", "") or "")
            if code:
                raise EconomicRoutingError(code) from None
            raise EconomicRoutingError("economic_revenue_usage_read_failed") from exc
        finally:
            _close(connection)
        recorded: list[str] = []
        for row in rows:
            if isinstance(row, Mapping):
                api_key_id = int(row["api_key_id"])
                merchant_id = int(row["merchant_id"])
                channel_id = row["a6_channel_id"]
                total_cost = Decimal(str(row["total_cost"]))
                total_tokens = int(row["total_tokens"])
            else:
                values = tuple(row)
                api_key_id = int(values[0])
                merchant_id = int(values[1])
                channel_id = values[2]
                total_cost = Decimal(str(values[3]))
                total_tokens = int(values[4])
            if channel_id is None or total_tokens <= 0 or total_cost <= 0:
                continue
            effective_fx = market_fx if market_fx is not None else fx_cny_per_usd
            k_cny = compute_channel_net_k_cny(
                total_cost,
                total_tokens,
                fx_cny_per_usd=effective_fx,
            )
            self.store.record_channel_revenue(
                model=model,
                channel_id=int(channel_id),
                revenue_epoch=_REVENUE_EPOCH,
                channel_net_k_cny=k_cny,
                settlement_contract_ref=settlement_contract_ref,
                observed_at=now,
                enodot_api_key_id=api_key_id,
                controlled_listing_refs=[f"a6trader:seller:merchant-{merchant_id}"],
                source_payload={
                    "merchant_id": merchant_id,
                    "usage_window_seconds": usage_window_seconds,
                    "total_cost_usd": format(total_cost, "f"),
                    "total_tokens": total_tokens,
                    "fx_source": "market_quotes" if market_fx is not None else "configured",
                    "fx_cny_per_usd": format(effective_fx, "f"),
                    "market_fx_cny_per_usd": (
                        format(market_fx, "f") if market_fx is not None else None
                    ),
                },
            )
            recorded.append(f"merchant:{merchant_id}")
        return {
            "channel_count": len(recorded),
            "channels": sorted(recorded),
        }


def canonical_endpoint_fingerprint(endpoint_ref: str, origin_class: str) -> str:
    endpoint = str(endpoint_ref).strip().rstrip("/").casefold()
    origin = str(origin_class).strip().casefold()
    if not endpoint or not origin:
        raise EconomicRoutingError("economic_endpoint_identity_invalid")
    return hashlib.sha256(f"{origin}\n{endpoint}".encode()).hexdigest()


class EconomicFactStore:
    """Write economic facts with source-scoped serialization and readback."""

    def __init__(self, connection_factory: Callable[[], Any]) -> None:
        if not callable(connection_factory):
            raise EconomicRoutingError("economic_connection_factory_invalid")
        self.connection_factory = connection_factory

    def upsert_source(
        self,
        *,
        source_key: str,
        source_kind: str,
        origin_class: str,
        model: str,
        endpoint_ref: str,
        enodot_account_id: int | None,
        configured_reserve_a: Decimal | str | int = 0,
        enabled: bool = False,
        config_version: str = "priority1-economic-v1",
        endpoint_fingerprint: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> EconomicSource:
        fingerprint = endpoint_fingerprint or canonical_endpoint_fingerprint(
            endpoint_ref, origin_class
        )
        desired = EconomicSource(
            source_id=1,
            source_key=source_key,
            source_kind=source_kind,
            origin_class=origin_class,
            model=model,
            endpoint_ref=endpoint_ref,
            endpoint_fingerprint=fingerprint,
            enodot_account_id=enodot_account_id,
            configured_reserve_a=Decimal(str(configured_reserve_a)),
            enabled=enabled,
            config_version=config_version,
        )
        metadata_json = _json_object(metadata)
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                _lock_source(cursor, desired.source_key)
                cursor.execute(
                    """
                    SELECT source_id, source_key, enodot_account_id, source_kind,
                           origin_class, model, endpoint_ref, endpoint_fingerprint,
                           health_state, health_observed_at, capacity_state,
                           capacity_observed_at, capacity_epoch, configured_reserve_a,
                           enabled, config_version
                    FROM a6_trade.sub2api_economic_sources
                    WHERE source_key = %s
                    FOR UPDATE
                    """,
                    (desired.source_key,),
                )
                current = _row(cursor)
                if current is None:
                    cursor.execute(
                        """
                        INSERT INTO a6_trade.sub2api_economic_sources (
                            source_key, enodot_account_id, source_kind, origin_class,
                            model, endpoint_ref, endpoint_fingerprint,
                            configured_reserve_a, enabled, config_version, metadata
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                        RETURNING source_id, source_key, enodot_account_id, source_kind,
                                  origin_class, model, endpoint_ref, endpoint_fingerprint,
                                  health_state, health_observed_at, capacity_state,
                                  capacity_observed_at, capacity_epoch,
                                  configured_reserve_a, enabled, config_version
                        """,
                        (
                            desired.source_key,
                            desired.enodot_account_id,
                            desired.source_kind,
                            desired.origin_class,
                            desired.model,
                            desired.endpoint_ref,
                            desired.endpoint_fingerprint,
                            desired.configured_reserve_a,
                            desired.enabled,
                            desired.config_version,
                            metadata_json,
                        ),
                    )
                else:
                    _assert_source_identity(current, desired)
                    cursor.execute(
                        """
                        UPDATE a6_trade.sub2api_economic_sources
                        SET configured_reserve_a = %s,
                            enabled = %s,
                            config_version = %s,
                            metadata = %s::jsonb,
                            updated_at = now()
                        WHERE source_id = %s
                        RETURNING source_id, source_key, enodot_account_id, source_kind,
                                  origin_class, model, endpoint_ref, endpoint_fingerprint,
                                  health_state, health_observed_at, capacity_state,
                                  capacity_observed_at, capacity_epoch,
                                  configured_reserve_a, enabled, config_version
                        """,
                        (
                            desired.configured_reserve_a,
                            desired.enabled,
                            desired.config_version,
                            metadata_json,
                            current["source_id"],
                        ),
                    )
                result = _source_from_row(_required_row(cursor))
                _notify(cursor, result.model, "source_upserted", result.source_id)
            connection.commit()
            return result
        except Exception:
            connection.rollback()
            raise
        finally:
            _close(connection)

    def update_source_state(
        self,
        source_id: int,
        *,
        health_state: str,
        capacity_state: str,
        observed_at: datetime,
        capacity_epoch: str | None = None,
        enabled: bool | None = None,
    ) -> EconomicSource:
        if source_id <= 0 or isinstance(source_id, bool):
            raise EconomicRoutingError("economic_source_id_invalid")
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                _lock_source(cursor, f"id:{source_id}")
                cursor.execute(
                    """
                    SELECT source_id, source_key, enodot_account_id, source_kind,
                           origin_class, model, endpoint_ref, endpoint_fingerprint,
                           health_state, health_observed_at, capacity_state,
                           capacity_observed_at, capacity_epoch, configured_reserve_a,
                           enabled, config_version
                    FROM a6_trade.sub2api_economic_sources
                    WHERE source_id = %s
                    FOR UPDATE
                    """,
                    (source_id,),
                )
                current = _source_from_row(_required_row(cursor))
                desired = EconomicSource(
                    **{
                        **current.__dict__,
                        "health_state": health_state,
                        "health_observed_at": observed_at,
                        "capacity_state": capacity_state,
                        "capacity_observed_at": observed_at,
                        "capacity_epoch": capacity_epoch,
                        "enabled": current.enabled if enabled is None else enabled,
                    }
                )
                cursor.execute(
                    """
                    UPDATE a6_trade.sub2api_economic_sources
                    SET health_state = %s, health_observed_at = %s,
                        capacity_state = %s, capacity_observed_at = %s,
                        capacity_epoch = %s, enabled = %s, updated_at = now()
                    WHERE source_id = %s
                    RETURNING source_id, source_key, enodot_account_id, source_kind,
                              origin_class, model, endpoint_ref, endpoint_fingerprint,
                              health_state, health_observed_at, capacity_state,
                              capacity_observed_at, capacity_epoch, configured_reserve_a,
                              enabled, config_version
                    """,
                    (
                        desired.health_state,
                        desired.health_observed_at,
                        desired.capacity_state,
                        desired.capacity_observed_at,
                        desired.capacity_epoch,
                        desired.enabled,
                        desired.source_id,
                    ),
                )
                result = _source_from_row(_required_row(cursor))
                _notify(cursor, result.model, "source_state_updated", result.source_id)
            connection.commit()
            return result
        except Exception:
            connection.rollback()
            raise
        finally:
            _close(connection)

    def record_cost_contract(
        self,
        *,
        source_id: int,
        cost_mode: str,
        effective_at: datetime,
        expires_at: datetime,
        config_version: str,
        declared_k_cny: Decimal | str | int | None = None,
        cycle_cost_cny: Decimal | str | int | None = None,
        cost_basis: str | None = None,
        quota_basis: str | None = None,
        contract_payload: Mapping[str, Any] | None = None,
    ) -> SourceCostContract:
        payload = {
            "schema_version": 1,
            "source_id": source_id,
            "cost_mode": cost_mode,
            "declared_k_cny": _decimal_text(declared_k_cny),
            "cycle_cost_cny": _decimal_text(cycle_cost_cny),
            "currency": "CNY",
            "cost_basis": cost_basis,
            "quota_basis": quota_basis,
            "effective_at": _datetime_text(effective_at),
            "expires_at": _datetime_text(expires_at),
            "config_version": config_version,
            "source_payload": dict(contract_payload or {}),
        }
        payload_json = _canonical_json(payload)
        source_hash = hashlib.sha256(payload_json.encode()).hexdigest()
        desired = SourceCostContract(
            contract_id=1,
            source_id=source_id,
            cost_mode=cost_mode,
            effective_at=effective_at,
            expires_at=expires_at,
            config_version=config_version,
            source_hash=source_hash,
            declared_k_cny=declared_k_cny,
            cycle_cost_cny=cycle_cost_cny,
            cost_basis=cost_basis,
            quota_basis=quota_basis,
        )
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                _lock_source(cursor, f"id:{desired.source_id}")
                cursor.execute(
                    """
                    SELECT source_key, source_kind, model
                    FROM a6_trade.sub2api_economic_sources
                    WHERE source_id = %s
                    FOR UPDATE
                    """,
                    (desired.source_id,),
                )
                source = _required_row(cursor)
                expected_mode = (
                    "declared_k" if source["source_kind"] == "api" else "weekly_amortized"
                )
                if source["source_kind"] == "a6_gateway" or desired.cost_mode != expected_mode:
                    raise EconomicRoutingError("economic_contract_source_kind_mismatch")
                cursor.execute(
                    """
                    SELECT contract_id, source_id, cost_mode, cycle_cost_cny,
                           declared_k_cny, currency, cost_basis, quota_basis,
                           effective_at, expires_at, config_version, source_hash,
                           contract_state
                    FROM a6_trade.sub2api_source_cost_contracts
                    WHERE source_id = %s AND config_version = %s
                    FOR UPDATE
                    """,
                    (desired.source_id, desired.config_version),
                )
                existing = _row(cursor)
                if existing is not None:
                    if str(existing["source_hash"]).casefold() != desired.source_hash:
                        raise EconomicRoutingError("economic_contract_version_conflict")
                    result = _contract_from_row(existing)
                else:
                    cursor.execute(
                        """
                        UPDATE a6_trade.sub2api_source_cost_contracts
                        SET contract_state = 'superseded'
                        WHERE source_id = %s AND contract_state = 'active'
                        """,
                        (desired.source_id,),
                    )
                    cursor.execute(
                        """
                        INSERT INTO a6_trade.sub2api_source_cost_contracts (
                            source_id, cost_mode, cycle_cost_cny, declared_k_cny,
                            currency, cost_basis, quota_basis, effective_at,
                            expires_at, config_version, source_hash, contract_state,
                            contract_payload
                        ) VALUES (
                            %s, %s, %s, %s, 'CNY', %s, %s, %s, %s, %s, %s,
                            'active', %s::jsonb
                        )
                        RETURNING contract_id, source_id, cost_mode, cycle_cost_cny,
                                  declared_k_cny, currency, cost_basis, quota_basis,
                                  effective_at, expires_at, config_version, source_hash,
                                  contract_state
                        """,
                        (
                            desired.source_id,
                            desired.cost_mode,
                            desired.cycle_cost_cny,
                            desired.declared_k_cny,
                            desired.cost_basis,
                            desired.quota_basis,
                            desired.effective_at,
                            desired.expires_at,
                            desired.config_version,
                            desired.source_hash,
                            payload_json,
                        ),
                    )
                    result = _contract_from_row(_required_row(cursor))
                _notify(cursor, source["model"], "cost_contract_recorded", source_id)
            connection.commit()
            return result
        except Exception:
            connection.rollback()
            raise
        finally:
            _close(connection)

    def record_quota_snapshot(
        self,
        *,
        source_id: int,
        contract_id: int,
        quota_epoch: str,
        used_a: Decimal | str | int,
        remaining_a: Decimal | str | int,
        total_a: Decimal | str | int,
        reset_at: datetime,
        observed_at: datetime,
        source_payload: Mapping[str, Any] | None = None,
        allow_total_change: bool = False,
    ) -> SourceQuotaSnapshot:
        payload = {
            "schema_version": 1,
            "source_id": source_id,
            "contract_id": contract_id,
            "quota_epoch": quota_epoch,
            "used_a": _decimal_text(used_a),
            "remaining_a": _decimal_text(remaining_a),
            "total_a": _decimal_text(total_a),
            "reset_at": _datetime_text(reset_at),
            "observed_at": _datetime_text(observed_at),
            "source_payload": dict(source_payload or {}),
        }
        payload_json = _canonical_json(payload)
        payload_hash = hashlib.sha256(payload_json.encode()).hexdigest()
        desired = SourceQuotaSnapshot(
            snapshot_id=1,
            source_id=source_id,
            contract_id=contract_id,
            quota_epoch=quota_epoch,
            used_a=used_a,
            remaining_a=remaining_a,
            total_a=total_a,
            reset_at=reset_at,
            observed_at=observed_at,
            payload_hash=payload_hash,
        )
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                _lock_source(cursor, f"id:{desired.source_id}")
                cursor.execute(
                    """
                    SELECT source.source_key, source.model, source.source_kind,
                           contract.cost_mode, contract.contract_state
                    FROM a6_trade.sub2api_economic_sources source
                    JOIN a6_trade.sub2api_source_cost_contracts contract
                      ON contract.source_id = source.source_id
                    WHERE source.source_id = %s AND contract.contract_id = %s
                    FOR UPDATE OF source, contract
                    """,
                    (desired.source_id, desired.contract_id),
                )
                source = _required_row(cursor)
                if (
                    source["source_kind"] != "account"
                    or source["cost_mode"] != "weekly_amortized"
                    or source["contract_state"] != "active"
                ):
                    raise EconomicRoutingError("economic_quota_contract_inactive")
                cursor.execute(
                    """
                    SELECT snapshot_id, source_id, contract_id, quota_epoch,
                           used_a, remaining_a, total_a, reset_at, observed_at,
                           payload_hash, snapshot_state
                    FROM a6_trade.sub2api_source_quota_snapshots
                    WHERE source_id = %s
                    ORDER BY observed_at DESC, snapshot_id DESC
                    LIMIT 1
                    FOR UPDATE
                    """,
                    (desired.source_id,),
                )
                latest_row = _row(cursor)
                state = "VALID"
                if latest_row is not None:
                    latest = _quota_from_row(latest_row)
                    if desired.observed_at < latest.observed_at:
                        raise EconomicRoutingError("economic_quota_observed_regression")
                    if desired.quota_epoch == latest.quota_epoch and _quota_progress_regressed(
                        latest,
                        desired,
                        allow_total_change=allow_total_change,
                    ):
                        state = "CONFLICT"
                cursor.execute(
                    """
                    INSERT INTO a6_trade.sub2api_source_quota_snapshots (
                        source_id, contract_id, quota_epoch, used_a, remaining_a,
                        total_a, reset_at, observed_at, payload_hash,
                        snapshot_state, source_payload
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                    ON CONFLICT (source_id, quota_epoch, observed_at, payload_hash)
                    DO UPDATE SET payload_hash = EXCLUDED.payload_hash
                    RETURNING snapshot_id, source_id, contract_id, quota_epoch,
                              used_a, remaining_a, total_a, reset_at, observed_at,
                              payload_hash, snapshot_state
                    """,
                    (
                        desired.source_id,
                        desired.contract_id,
                        desired.quota_epoch,
                        desired.used_a,
                        desired.remaining_a,
                        desired.total_a,
                        desired.reset_at,
                        desired.observed_at,
                        desired.payload_hash,
                        state,
                        payload_json,
                    ),
                )
                result = _quota_from_row(_required_row(cursor))
                _notify(cursor, source["model"], "quota_snapshot_recorded", source_id)
            connection.commit()
            return result
        except Exception:
            connection.rollback()
            raise
        finally:
            _close(connection)

    def record_channel_revenue(
        self,
        *,
        model: str,
        channel_id: int,
        revenue_epoch: str,
        channel_net_k_cny: Decimal | str | int,
        settlement_contract_ref: str,
        observed_at: datetime,
        enodot_api_key_id: int | None = None,
        sub2api_group_id: int | None = None,
        controlled_listing_refs: list[Any] | tuple[Any, ...] = (),
        source_payload: Mapping[str, Any] | None = None,
    ) -> ChannelRevenueSnapshot:
        payload = {
            "schema_version": 1,
            "model": model,
            "channel_id": channel_id,
            "enodot_api_key_id": enodot_api_key_id,
            "sub2api_group_id": sub2api_group_id,
            "revenue_epoch": revenue_epoch,
            "channel_net_k_cny": _decimal_text(channel_net_k_cny),
            "settlement_contract_ref": settlement_contract_ref,
            "controlled_listing_refs": list(controlled_listing_refs),
            "observed_at": _datetime_text(observed_at),
            "source_payload": dict(source_payload or {}),
        }
        payload_json = _canonical_json(payload)
        payload_hash = hashlib.sha256(payload_json.encode()).hexdigest()
        desired = ChannelRevenueSnapshot(
            snapshot_id=1,
            model=model,
            channel_id=channel_id,
            revenue_epoch=revenue_epoch,
            channel_net_k_cny=channel_net_k_cny,
            settlement_contract_ref=settlement_contract_ref,
            observed_at=observed_at,
            payload_hash=payload_hash,
            enodot_api_key_id=enodot_api_key_id,
            sub2api_group_id=sub2api_group_id,
        )
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO a6_trade.sub2api_channel_revenue_snapshots (
                        model, channel_id, enodot_api_key_id, sub2api_group_id,
                        revenue_epoch, channel_net_k_cny, settlement_contract_ref,
                        controlled_listing_refs, observed_at, payload_hash,
                        snapshot_state, source_payload
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s,
                        'VALID', %s::jsonb
                    )
                    ON CONFLICT (model, channel_id, revenue_epoch, observed_at, payload_hash)
                    DO UPDATE SET payload_hash = EXCLUDED.payload_hash
                    RETURNING snapshot_id, model, channel_id, enodot_api_key_id,
                              sub2api_group_id, revenue_epoch, channel_net_k_cny,
                              settlement_contract_ref, observed_at, payload_hash,
                              snapshot_state
                    """,
                    (
                        desired.model,
                        desired.channel_id,
                        desired.enodot_api_key_id,
                        desired.sub2api_group_id,
                        desired.revenue_epoch,
                        desired.channel_net_k_cny,
                        desired.settlement_contract_ref,
                        _canonical_json(list(controlled_listing_refs)),
                        desired.observed_at,
                        desired.payload_hash,
                        payload_json,
                    ),
                )
                result = _revenue_from_row(_required_row(cursor))
                _notify(cursor, result.model, "channel_revenue_recorded", result.channel_id)
            connection.commit()
            return result
        except Exception:
            connection.rollback()
            raise
        finally:
            _close(connection)


def _assert_source_identity(row: Mapping[str, Any], desired: EconomicSource) -> None:
    observed = (
        row.get("source_kind"),
        row.get("origin_class"),
        row.get("model"),
        row.get("endpoint_ref"),
        str(row.get("endpoint_fingerprint") or "").casefold(),
        row.get("enodot_account_id"),
    )
    expected = (
        desired.source_kind,
        desired.origin_class,
        desired.model,
        desired.endpoint_ref,
        desired.endpoint_fingerprint,
        desired.enodot_account_id,
    )
    if observed != expected:
        raise EconomicRoutingError("economic_source_identity_conflict")


def _quota_progress_regressed(
    latest: SourceQuotaSnapshot,
    desired: SourceQuotaSnapshot,
    *,
    allow_total_change: bool,
) -> bool:
    if desired.reset_at != latest.reset_at:
        return True
    if desired.total_a == latest.total_a:
        return desired.used_a < latest.used_a
    if not allow_total_change:
        return True
    return desired.used_a / desired.total_a < latest.used_a / latest.total_a


def _source_from_row(row: Mapping[str, Any]) -> EconomicSource:
    return EconomicSource(**dict(row))


def _contract_from_row(row: Mapping[str, Any]) -> SourceCostContract:
    return SourceCostContract(**dict(row))


def _quota_from_row(row: Mapping[str, Any]) -> SourceQuotaSnapshot:
    return SourceQuotaSnapshot(**dict(row))


def _revenue_from_row(row: Mapping[str, Any]) -> ChannelRevenueSnapshot:
    return ChannelRevenueSnapshot(**dict(row))


def _lock_source(cursor: Any, source_key: str) -> None:
    cursor.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
        (f"a6-economic-source:{source_key}",),
    )


def _notify(cursor: Any, model: str, event: str, identity: int) -> None:
    cursor.execute(
        "SELECT pg_notify(%s, %s)",
        (
            "a6_economic_fact_changed",
            _canonical_json({"model": model, "event": event, "identity": identity}),
        ),
    )


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _json_object(value: Mapping[str, Any] | None) -> str:
    if value is None:
        return "{}"
    if not isinstance(value, Mapping):
        raise EconomicRoutingError("economic_json_object_invalid")
    return _canonical_json(dict(value))


def _decimal_text(value: Decimal | str | int | None) -> str | None:
    if value is None:
        return None
    try:
        decimal = Decimal(str(value))
    except Exception:
        raise EconomicRoutingError("economic_decimal_invalid") from None
    if not decimal.is_finite():
        raise EconomicRoutingError("economic_decimal_invalid")
    return format(decimal, "f")


def _datetime_text(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise EconomicRoutingError("economic_datetime_invalid")
    return value.isoformat()


def _row(cursor: Any) -> dict[str, Any] | None:
    value = cursor.fetchone()
    if value is None:
        return None
    if isinstance(value, Mapping):
        return dict(value)
    columns = [item.name if hasattr(item, "name") else item[0] for item in cursor.description]
    return dict(zip(columns, value, strict=True))


def _required_row(cursor: Any) -> dict[str, Any]:
    row = _row(cursor)
    if row is None:
        raise EconomicRoutingError("economic_fact_not_found")
    return row


def _close(connection: Any) -> None:
    close = getattr(connection, "close", None)
    if callable(close):
        close()
