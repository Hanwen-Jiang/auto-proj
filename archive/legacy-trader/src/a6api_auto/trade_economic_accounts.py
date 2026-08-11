"""Automatic discovery and quota costing for owned weekly accounts."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal, InvalidOperation
from statistics import median
from typing import Any, Protocol

from .trade_economic_facts import EconomicFactStore
from .trade_economic_source import EconomicRoutingError, EconomicSource

_PRICE_PATTERN = re.compile(
    r"^\s*(?:(?:CNY|RMB)\s*)?(?:\u00a5|\uffe5)?\s*"
    r"(?P<amount>[0-9]+(?:\.[0-9]{1,6})?)\s*"
    r"(?:(?:CNY|RMB)|\u5143)?\s*$",
    re.IGNORECASE,
)
_COST_QUANTUM = Decimal("0.000001")
_VALUE_QUANTUM = Decimal("0.000000000001")


@dataclass(frozen=True)
class EconomicAccountSyncConfig:
    model: str = "gpt-5.6-sol"
    account_name: str = "gpt-plus"
    group_id: int = 26
    provisional_total_a: Decimal = Decimal("100")
    minimum_total_a: Decimal = Decimal("50")
    maximum_total_a: Decimal = Decimal("300")
    minimum_utilization_delta: Decimal = Decimal("2")
    minimum_utilization_span: Decimal = Decimal("4")
    minimum_distinct_levels: int = 3
    minimum_slope_count: int = 3
    maximum_relative_mad: Decimal = Decimal("0.30")
    reset_bucket_seconds: int = 300
    evidence_limit: int = 256
    usage_workers: int = 4
    source_config_version: str = "priority1-economic-account-sync-v1"

    def __post_init__(self) -> None:
        if not str(self.model).strip() or not str(self.account_name).strip():
            raise EconomicRoutingError("economic_account_sync_identity_invalid")
        if isinstance(self.group_id, bool) or self.group_id < 1:
            raise EconomicRoutingError("economic_account_sync_group_invalid")
        for value in (
            self.provisional_total_a,
            self.minimum_total_a,
            self.maximum_total_a,
            self.minimum_utilization_delta,
            self.minimum_utilization_span,
            self.maximum_relative_mad,
        ):
            if _decimal(value, "economic_account_sync_decimal_invalid") <= 0:
                raise EconomicRoutingError("economic_account_sync_decimal_invalid")
        if not self.minimum_total_a <= self.provisional_total_a <= self.maximum_total_a:
            raise EconomicRoutingError("economic_account_sync_provisional_invalid")
        for value in (
            self.minimum_distinct_levels,
            self.minimum_slope_count,
            self.reset_bucket_seconds,
            self.evidence_limit,
            self.usage_workers,
        ):
            if isinstance(value, bool) or value < 1:
                raise EconomicRoutingError("economic_account_sync_integer_invalid")


@dataclass(frozen=True)
class OwnedAccountCandidate:
    account_id: int
    name: str
    cycle_cost_cny: Decimal
    note_sha256: str
    status: str
    schedulable: bool
    created_at: datetime


@dataclass(frozen=True)
class AccountQuotaObservation:
    observed_at: datetime
    upstream_updated_at: datetime
    reset_at: datetime
    quota_epoch: str
    utilization_percent: Decimal
    window_cost_a: Decimal
    requests: int
    tokens: int
    reset_inferred: bool = False
    reset_stabilized: bool = False
    estimate_state: str = ""
    estimated_total_a: Decimal | None = None


@dataclass(frozen=True)
class QuotaEstimate:
    total_a: Decimal
    state: str
    method: str
    evidence_point_count: int
    distinct_level_count: int
    slope_count: int
    relative_mad: Decimal | None = None


class EconomicAccountStore(Protocol):
    def reconcile_account(
        self,
        candidate: OwnedAccountCandidate,
        observation: AccountQuotaObservation,
        *,
        config: EconomicAccountSyncConfig,
    ) -> Mapping[str, Any]: ...

    def disable_missing_accounts(
        self,
        model: str,
        account_ids: Sequence[int],
        *,
        observed_at: datetime,
    ) -> int: ...


def parse_cycle_cost_cny(notes: object) -> Decimal:
    match = _PRICE_PATTERN.fullmatch(str(notes or ""))
    if match is None:
        raise EconomicRoutingError("economic_account_cost_note_invalid")
    amount = _decimal(match.group("amount"), "economic_account_cost_note_invalid")
    if amount <= 0 or amount > Decimal("100000"):
        raise EconomicRoutingError("economic_account_cost_note_invalid")
    return amount.quantize(_COST_QUANTUM)


def parse_owned_account(
    account: Mapping[str, Any],
    config: EconomicAccountSyncConfig,
) -> OwnedAccountCandidate | None:
    if str(account.get("name") or "").strip().casefold() != config.account_name.casefold():
        return None
    if str(account.get("platform") or "").strip().casefold() != "openai":
        return None
    if str(account.get("type") or "").strip().casefold() != "oauth":
        return None
    group_ids = {
        _positive_int(value, "economic_account_group_invalid")
        for value in account.get("group_ids") or ()
    }
    if config.group_id not in group_ids:
        return None
    account_id = _positive_int(account.get("id"), "economic_account_id_invalid")
    notes = str(account.get("notes") or "").strip()
    return OwnedAccountCandidate(
        account_id=account_id,
        name=str(account.get("name") or "").strip(),
        cycle_cost_cny=parse_cycle_cost_cny(notes),
        note_sha256=hashlib.sha256(notes.encode()).hexdigest(),
        status=str(account.get("status") or "").strip().casefold(),
        schedulable=account.get("schedulable") is True,
        created_at=_timestamp(account.get("created_at"), "economic_account_created_at_invalid"),
    )


def parse_account_quota_observation(
    payload: Mapping[str, Any],
    *,
    observed_at: datetime,
    config: EconomicAccountSyncConfig,
    fallback_reset_at: datetime | None = None,
) -> AccountQuotaObservation:
    sampled_at = _aware_datetime(observed_at, "economic_account_observed_at_invalid")
    seven_day = payload.get("seven_day")
    if not isinstance(seven_day, Mapping):
        raise EconomicRoutingError("economic_account_seven_day_missing")
    utilization = _decimal(seven_day.get("utilization"), "economic_account_utilization_invalid")
    if utilization < 0 or utilization > Decimal("1000"):
        raise EconomicRoutingError("economic_account_utilization_invalid")
    reset_value = seven_day.get("resets_at")
    reset_inferred = reset_value in (None, "") and utilization == 0
    if reset_inferred and fallback_reset_at is not None:
        raw_reset = _aware_datetime(fallback_reset_at, "economic_account_reset_invalid")
    else:
        raw_reset = _timestamp(reset_value, "economic_account_reset_invalid")
    reset_at = normalize_reset_at(raw_reset, config.reset_bucket_seconds)
    if reset_at <= sampled_at:
        raise EconomicRoutingError("economic_account_reset_invalid")
    updated_raw = payload.get("updated_at")
    upstream_updated_at = (
        sampled_at
        if updated_raw in (None, "")
        else _timestamp(updated_raw, "economic_account_updated_at_invalid")
    )
    stats = seven_day.get("window_stats")
    if not isinstance(stats, Mapping):
        raise EconomicRoutingError("economic_account_window_stats_missing")
    window_cost = _decimal(stats.get("cost"), "economic_account_window_cost_invalid")
    if window_cost < 0:
        raise EconomicRoutingError("economic_account_window_cost_invalid")
    return AccountQuotaObservation(
        observed_at=sampled_at,
        upstream_updated_at=upstream_updated_at,
        reset_at=reset_at,
        quota_epoch=f"codex-7d:{reset_at.astimezone(timezone.utc).isoformat()}",
        utilization_percent=utilization,
        window_cost_a=window_cost,
        requests=_nonnegative_int(stats.get("requests") or 0, "economic_account_requests_invalid"),
        tokens=_nonnegative_int(stats.get("tokens") or 0, "economic_account_tokens_invalid"),
        reset_inferred=reset_inferred,
    )


def normalize_reset_at(value: datetime, bucket_seconds: int = 300) -> datetime:
    aware = _aware_datetime(value, "economic_account_reset_invalid").astimezone(timezone.utc)
    if isinstance(bucket_seconds, bool) or bucket_seconds < 1:
        raise EconomicRoutingError("economic_account_reset_bucket_invalid")
    rounded = (
        int(
            (Decimal(str(aware.timestamp())) / Decimal(bucket_seconds)).to_integral_value(
                rounding=ROUND_HALF_UP
            )
        )
        * bucket_seconds
    )
    return datetime.fromtimestamp(rounded, tz=timezone.utc)


def estimate_weekly_total_a(
    evidence: Sequence[AccountQuotaObservation],
    config: EconomicAccountSyncConfig,
) -> QuotaEstimate:
    points = tuple(evidence)[-config.evidence_limit :]
    grouped: dict[Decimal, list[Decimal]] = {}
    for item in points:
        if item.utilization_percent <= 0 or item.window_cost_a < 0:
            continue
        grouped.setdefault(item.utilization_percent, []).append(item.window_cost_a)
    levels = sorted((utilization, Decimal(median(costs))) for utilization, costs in grouped.items())
    slopes: list[Decimal] = []
    for index, (left_utilization, left_cost) in enumerate(levels):
        for right_utilization, right_cost in levels[index + 1 :]:
            delta_utilization = right_utilization - left_utilization
            delta_cost = right_cost - left_cost
            if delta_utilization < config.minimum_utilization_delta or delta_cost <= 0:
                continue
            total = delta_cost * Decimal("100") / delta_utilization
            if config.minimum_total_a <= total <= config.maximum_total_a:
                slopes.append(total)
    level_span = levels[-1][0] - levels[0][0] if len(levels) > 1 else Decimal(0)
    relative_mad: Decimal | None = None
    if slopes:
        center = Decimal(median(slopes))
        relative_mad = Decimal(median([abs(value - center) for value in slopes])) / center
    converged = (
        len(levels) >= config.minimum_distinct_levels
        and level_span >= config.minimum_utilization_span
        and len(slopes) >= config.minimum_slope_count
        and relative_mad is not None
        and relative_mad <= config.maximum_relative_mad
    )
    if converged:
        total = _lower_quartile(slopes).quantize(_COST_QUANTUM, rounding=ROUND_FLOOR)
        return QuotaEstimate(
            total_a=total,
            state="CONVERGED",
            method="delta_slope_lower_quartile",
            evidence_point_count=len(points),
            distinct_level_count=len(levels),
            slope_count=len(slopes),
            relative_mad=relative_mad,
        )
    previous = next(
        (
            item.estimated_total_a
            for item in reversed(points)
            if item.estimate_state == "CONVERGED" and item.estimated_total_a is not None
        ),
        None,
    )
    if previous is not None:
        return QuotaEstimate(
            total_a=previous,
            state="CONVERGED",
            method="held_last_converged",
            evidence_point_count=len(points),
            distinct_level_count=len(levels),
            slope_count=len(slopes),
            relative_mad=relative_mad,
        )
    return QuotaEstimate(
        total_a=config.provisional_total_a.quantize(_COST_QUANTUM),
        state="PROVISIONAL",
        method="default_100A_until_delta_slope_converges",
        evidence_point_count=len(points),
        distinct_level_count=len(levels),
        slope_count=len(slopes),
        relative_mad=relative_mad,
    )


class PostgresEconomicAccountStore:
    def __init__(self, connection_factory: Callable[[], Any]) -> None:
        self.connection_factory = connection_factory
        self.facts = EconomicFactStore(connection_factory)

    def reconcile_account(
        self,
        candidate: OwnedAccountCandidate,
        observation: AccountQuotaObservation,
        *,
        config: EconomicAccountSyncConfig,
    ) -> Mapping[str, Any]:
        endpoint = f"sub2api://account/{candidate.account_id}/openai-oauth/{config.model}"
        source = self.facts.upsert_source(
            source_key=f"sub2api-account-{candidate.account_id}-{config.model}",
            source_kind="account",
            origin_class="sub2api_owned_account",
            model=config.model,
            endpoint_ref=endpoint,
            enodot_account_id=candidate.account_id,
            enabled=candidate.status == "active" and candidate.schedulable,
            config_version=config.source_config_version,
            metadata={
                "managed_by": "a6-economic-account-sync",
                "account_name": candidate.name,
                "note_sha256": candidate.note_sha256,
                "cost_contract_ref": f"sub2api-account:{candidate.account_id}:auto",
                "quota_normalization": "provisional_100A_then_delta_slope",
            },
        )
        observation = self._stabilize_epoch(
            source.source_id,
            observation,
            reset_bucket_seconds=config.reset_bucket_seconds,
        )
        evidence = [
            *self._load_evidence(source.source_id, observation.quota_epoch, config.evidence_limit),
            observation,
        ]
        estimate = estimate_weekly_total_a(evidence, config)
        utilization = min(observation.utilization_percent, Decimal("100"))
        used = (estimate.total_a * utilization / Decimal("100")).quantize(_VALUE_QUANTUM)
        remaining = (estimate.total_a - used).quantize(_VALUE_QUANTUM)
        quota_payload = {
            "estimate_state": estimate.state,
            "estimate_method": estimate.method,
            "estimated_total_a": format(estimate.total_a, "f"),
            "evidence_point_count": estimate.evidence_point_count,
            "distinct_level_count": estimate.distinct_level_count,
            "slope_count": estimate.slope_count,
            "relative_mad": (
                None if estimate.relative_mad is None else format(estimate.relative_mad, "f")
            ),
            "raw_utilization_percent": format(observation.utilization_percent, "f"),
            "raw_window_cost_a": format(observation.window_cost_a, "f"),
            "raw_requests": observation.requests,
            "raw_tokens": observation.tokens,
            "reset_inferred": observation.reset_inferred,
            "reset_stabilized": observation.reset_stabilized,
            "upstream_updated_at": observation.upstream_updated_at.isoformat(),
        }
        for attempt in range(2):
            contract_id = self._ensure_contract(source, candidate, observation)
            try:
                quota = self.facts.record_quota_snapshot(
                    source_id=source.source_id,
                    contract_id=contract_id,
                    quota_epoch=observation.quota_epoch,
                    used_a=used,
                    remaining_a=remaining,
                    total_a=estimate.total_a,
                    reset_at=observation.reset_at,
                    observed_at=observation.observed_at,
                    allow_total_change=True,
                    source_payload=quota_payload,
                )
            except EconomicRoutingError as exc:
                if attempt == 0 and exc.code == "economic_quota_contract_inactive":
                    continue
                raise
            break
        else:
            raise EconomicRoutingError("economic_quota_contract_refresh_failed")
        enabled = candidate.status == "active" and candidate.schedulable
        capacity = "EXHAUSTED" if utilization >= 100 or not enabled else "AVAILABLE"
        self.facts.update_source_state(
            source.source_id,
            health_state="HEALTHY" if candidate.status == "active" else "UNHEALTHY",
            capacity_state=capacity,
            observed_at=observation.observed_at,
            capacity_epoch=observation.quota_epoch,
            enabled=enabled,
        )
        return {
            "account_id": candidate.account_id,
            "source_id": source.source_id,
            "contract_id": contract_id,
            "snapshot_id": quota.snapshot_id,
            "estimate_state": estimate.state,
            "estimated_total_a": format(estimate.total_a, "f"),
            "utilization_percent": format(observation.utilization_percent, "f"),
            "source_k_cny": format(
                (candidate.cycle_cost_cny / estimate.total_a).quantize(
                    _COST_QUANTUM, rounding=ROUND_CEILING
                ),
                "f",
            ),
            "capacity_state": capacity,
            "quota_epoch": observation.quota_epoch,
            "reset_inferred": observation.reset_inferred,
            "reset_stabilized": observation.reset_stabilized,
        }

    def _stabilize_epoch(
        self,
        source_id: int,
        observation: AccountQuotaObservation,
        *,
        reset_bucket_seconds: int,
    ) -> AccountQuotaObservation:
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT contract.expires_at,
                           quota.source_payload->'source_payload'->>'reset_inferred'
                               AS latest_reset_inferred,
                           quota.source_payload->'source_payload'->>'raw_utilization_percent'
                               AS latest_utilization_percent
                    FROM a6_trade.sub2api_source_cost_contracts contract
                    LEFT JOIN LATERAL (
                        SELECT source_payload
                        FROM a6_trade.sub2api_source_quota_snapshots
                        WHERE contract_id = contract.contract_id
                          AND snapshot_state = 'VALID'
                        ORDER BY observed_at DESC, snapshot_id DESC
                        LIMIT 1
                    ) quota ON true
                    WHERE contract.source_id = %s AND contract.contract_state = 'active'
                      AND contract.cost_mode = 'weekly_amortized'
                    ORDER BY contract.effective_at DESC, contract.contract_id DESC
                    LIMIT 1
                    """,
                    (source_id,),
                )
                row = _row(cursor)
            connection.commit()
        finally:
            _close(connection)
        if row is None:
            return observation
        active_reset_at = _timestamp(row.get("expires_at"), "economic_account_reset_invalid")
        latest_inferred = str(row.get("latest_reset_inferred") or "").strip().casefold()
        latest_utilization_raw = row.get("latest_utilization_percent")
        latest_utilization = (
            Decimal("0")
            if latest_utilization_raw in (None, "")
            else _decimal(latest_utilization_raw, "economic_account_utilization_invalid")
        )
        active_epoch_authoritative = latest_inferred == "false" or (
            not latest_inferred and latest_utilization > 0
        )
        return _stabilize_observation_epoch(
            observation,
            active_reset_at=active_reset_at,
            active_epoch_authoritative=active_epoch_authoritative,
            reset_bucket_seconds=reset_bucket_seconds,
        )

    def disable_missing_accounts(
        self,
        model: str,
        account_ids: Sequence[int],
        *,
        observed_at: datetime,
    ) -> int:
        expected = {int(value) for value in account_ids}
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT source_id, enodot_account_id
                    FROM a6_trade.sub2api_economic_sources
                    WHERE model = %s
                      AND source_kind = 'account'
                      AND metadata->>'managed_by' = 'a6-economic-account-sync'
                      AND enabled
                    """,
                    (model,),
                )
                rows = _rows(cursor)
            connection.commit()
        finally:
            _close(connection)
        disabled = 0
        for row in rows:
            account_id = _positive_int(row.get("enodot_account_id"), "economic_account_id_invalid")
            if account_id in expected:
                continue
            self.facts.update_source_state(
                _positive_int(row.get("source_id"), "economic_source_id_invalid"),
                health_state="UNKNOWN",
                capacity_state="UNKNOWN",
                observed_at=observed_at,
                enabled=False,
            )
            disabled += 1
        return disabled

    def _ensure_contract(
        self,
        source: EconomicSource,
        candidate: OwnedAccountCandidate,
        observation: AccountQuotaObservation,
    ) -> int:
        epoch = observation.reset_at.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        version = f"priority1-auto-{candidate.account_id}-{epoch}-{candidate.note_sha256[:8]}"
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT contract_id
                    FROM a6_trade.sub2api_source_cost_contracts
                    WHERE source_id = %s AND config_version = %s
                      AND contract_state = 'active'
                      AND cycle_cost_cny = %s
                      AND effective_at = %s AND expires_at = %s
                    """,
                    (
                        source.source_id,
                        version,
                        candidate.cycle_cost_cny,
                        observation.reset_at - timedelta(days=7),
                        observation.reset_at,
                    ),
                )
                row = _row(cursor)
            connection.commit()
        finally:
            _close(connection)
        if row is not None:
            return _positive_int(row.get("contract_id"), "economic_contract_id_invalid")
        contract = self.facts.record_cost_contract(
            source_id=source.source_id,
            cost_mode="weekly_amortized",
            cycle_cost_cny=candidate.cycle_cost_cny,
            cost_basis="weekly",
            quota_basis="full_cycle",
            effective_at=observation.reset_at - timedelta(days=7),
            expires_at=observation.reset_at,
            config_version=version,
            contract_payload={
                "account_id": candidate.account_id,
                "account_name": candidate.name,
                "cost_source": "account_notes",
                "note_sha256": candidate.note_sha256,
                "normalized_cycle_cost_cny": format(candidate.cycle_cost_cny, "f"),
            },
        )
        return contract.contract_id

    def _load_evidence(
        self,
        source_id: int,
        quota_epoch: str,
        limit: int,
    ) -> tuple[AccountQuotaObservation, ...]:
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT observed_at, reset_at,
                           source_payload->'source_payload' AS payload
                    FROM a6_trade.sub2api_source_quota_snapshots
                    WHERE source_id = %s AND quota_epoch = %s
                      AND snapshot_state = 'VALID'
                    ORDER BY observed_at DESC, snapshot_id DESC
                    LIMIT %s
                    """,
                    (source_id, quota_epoch, limit),
                )
                rows = _rows(cursor)
            connection.commit()
        finally:
            _close(connection)
        evidence: list[AccountQuotaObservation] = []
        for row in reversed(rows):
            payload = row.get("payload")
            if not isinstance(payload, Mapping):
                continue
            try:
                evidence.append(
                    AccountQuotaObservation(
                        observed_at=_aware_datetime(
                            row.get("observed_at"), "economic_account_observed_at_invalid"
                        ),
                        upstream_updated_at=_timestamp(
                            payload.get("upstream_updated_at") or row.get("observed_at"),
                            "economic_account_updated_at_invalid",
                        ),
                        reset_at=_aware_datetime(
                            row.get("reset_at"), "economic_account_reset_invalid"
                        ),
                        quota_epoch=quota_epoch,
                        utilization_percent=_decimal(
                            payload.get("raw_utilization_percent"),
                            "economic_account_utilization_invalid",
                        ),
                        window_cost_a=_decimal(
                            payload.get("raw_window_cost_a"),
                            "economic_account_window_cost_invalid",
                        ),
                        requests=_nonnegative_int(
                            payload.get("raw_requests") or 0,
                            "economic_account_requests_invalid",
                        ),
                            tokens=_nonnegative_int(
                                payload.get("raw_tokens") or 0,
                                "economic_account_tokens_invalid",
                            ),
                            reset_inferred=payload.get("reset_inferred") is True,
                            reset_stabilized=payload.get("reset_stabilized") is True,
                            estimate_state=str(payload.get("estimate_state") or "").upper(),
                        estimated_total_a=(
                            None
                            if payload.get("estimated_total_a") in (None, "")
                            else _decimal(
                                payload.get("estimated_total_a"),
                                "economic_account_estimate_invalid",
                            )
                        ),
                    )
                )
            except EconomicRoutingError:
                continue
        return tuple(evidence)


def sync_owned_accounts(
    admin: Any,
    store: EconomicAccountStore,
    *,
    config: EconomicAccountSyncConfig,
    usage_client_factory: Callable[[], Any] | None = None,
    now: Callable[[], datetime] | None = None,
) -> dict[str, Any]:
    clock = now or (lambda: datetime.now(timezone.utc))
    accounts = list(
        admin.iter_accounts(
            page_size=200,
            filters={"platform": "openai", "type": "oauth"},
        )
    )
    candidates: list[OwnedAccountCandidate] = []
    errors: list[dict[str, Any]] = []
    for account in accounts:
        try:
            candidate = parse_owned_account(account, config)
        except Exception as exc:
            account_id = account.get("id") if isinstance(account, Mapping) else None
            errors.append(
                {
                    "account_id": account_id,
                    "stage": "discover",
                    "error_class": getattr(exc, "code", type(exc).__name__),
                }
            )
            continue
        if candidate is not None:
            candidates.append(candidate)

    usage_by_id: dict[int, Mapping[str, Any]] = {}

    def load_usage(candidate: OwnedAccountCandidate) -> Mapping[str, Any]:
        client = usage_client_factory() if usage_client_factory is not None else admin
        return client.get_account_usage(candidate.account_id, source="active", force=True)

    if candidates:
        worker_count = min(config.usage_workers, len(candidates))
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            futures = {
                executor.submit(load_usage, candidate): candidate for candidate in candidates
            }
            for future in as_completed(futures):
                candidate = futures[future]
                try:
                    usage_by_id[candidate.account_id] = future.result()
                except Exception as exc:
                    errors.append(
                        {
                            "account_id": candidate.account_id,
                            "stage": "usage",
                            "error_class": getattr(exc, "code", type(exc).__name__),
                        }
                    )

    records: list[Mapping[str, Any]] = []
    for candidate in sorted(candidates, key=lambda item: item.account_id):
        payload = usage_by_id.get(candidate.account_id)
        if payload is None:
            continue
        observation: AccountQuotaObservation | None = None
        try:
            observed_at = clock()
            observation = parse_account_quota_observation(
                payload,
                observed_at=observed_at,
                config=config,
                fallback_reset_at=_next_weekly_reset(
                    candidate.created_at + timedelta(days=7), observed_at
                ),
            )
            records.append(store.reconcile_account(candidate, observation, config=config))
        except Exception as exc:
            error: dict[str, Any] = {
                "account_id": candidate.account_id,
                "stage": "persist",
                "error_class": getattr(exc, "code", type(exc).__name__),
            }
            if observation is not None:
                error.update(
                    {
                        "quota_epoch": observation.quota_epoch,
                        "utilization_percent": format(
                            observation.utilization_percent, "f"
                        ),
                        "reset_inferred": observation.reset_inferred,
                    }
                )
            errors.append(error)
    disabled = store.disable_missing_accounts(
        config.model,
        [item.account_id for item in candidates],
        observed_at=clock(),
    )
    return {
        "success": not errors,
        "model": config.model,
        "scanned_account_count": len(accounts),
        "matched_account_count": len(candidates),
        "synced_account_count": len(records),
        "provisional_account_count": sum(
            1 for item in records if item.get("estimate_state") == "PROVISIONAL"
        ),
        "converged_account_count": sum(
            1 for item in records if item.get("estimate_state") == "CONVERGED"
        ),
        "disabled_missing_count": disabled,
        "accounts": list(records),
        "errors": errors,
    }


def _lower_quartile(values: Sequence[Decimal]) -> Decimal:
    ordered = sorted(values)
    if not ordered:
        raise EconomicRoutingError("economic_account_slope_missing")
    position = Decimal(len(ordered) - 1) * Decimal("0.25")
    lower = int(position.to_integral_value(rounding=ROUND_FLOOR))
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - Decimal(lower)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def _next_weekly_reset(value: datetime, observed_at: datetime) -> datetime:
    reset_at = _aware_datetime(value, "economic_account_reset_invalid")
    sampled_at = _aware_datetime(observed_at, "economic_account_observed_at_invalid")
    if reset_at <= sampled_at:
        elapsed = sampled_at - reset_at
        reset_at += timedelta(days=7 * (elapsed.days // 7 + 1))
    return reset_at


def _stabilize_observation_epoch(
    observation: AccountQuotaObservation,
    *,
    active_reset_at: datetime,
    active_epoch_authoritative: bool,
    reset_bucket_seconds: int,
) -> AccountQuotaObservation:
    reset_at = normalize_reset_at(active_reset_at, reset_bucket_seconds)
    if reset_at <= observation.observed_at:
        return observation
    drift_seconds = abs((observation.reset_at - reset_at).total_seconds())
    should_stabilize = observation.reset_inferred or (
        active_epoch_authoritative and drift_seconds <= reset_bucket_seconds
    )
    if not should_stabilize or observation.reset_at == reset_at:
        return observation
    return replace(
        observation,
        reset_at=reset_at,
        quota_epoch=f"codex-7d:{reset_at.astimezone(timezone.utc).isoformat()}",
        reset_stabilized=True,
    )


def _decimal(value: object, code: str) -> Decimal:
    if isinstance(value, bool):
        raise EconomicRoutingError(code)
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise EconomicRoutingError(code) from None
    if not result.is_finite():
        raise EconomicRoutingError(code)
    return result


def _positive_int(value: object, code: str) -> int:
    result = _nonnegative_int(value, code)
    if result < 1:
        raise EconomicRoutingError(code)
    return result


def _nonnegative_int(value: object, code: str) -> int:
    if isinstance(value, bool):
        raise EconomicRoutingError(code)
    try:
        result = int(value)
    except (TypeError, ValueError):
        raise EconomicRoutingError(code) from None
    if result < 0:
        raise EconomicRoutingError(code)
    return result


def _timestamp(value: object, code: str) -> datetime:
    if isinstance(value, datetime):
        return _aware_datetime(value, code)
    raw = str(value or "").strip()
    nanosecond = re.fullmatch(
        r"(?P<prefix>.+T[0-9]{2}:[0-9]{2}:[0-9]{2})\."
        r"(?P<fraction>[0-9]+)(?P<zone>Z|[+-][0-9]{2}:[0-9]{2})",
        raw,
    )
    if nanosecond is not None:
        fraction = nanosecond.group("fraction")[:6].ljust(6, "0")
        raw = f"{nanosecond.group('prefix')}.{fraction}{nanosecond.group('zone')}"
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        raise EconomicRoutingError(code) from None
    return _aware_datetime(parsed, code)


def _aware_datetime(value: object, code: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise EconomicRoutingError(code)
    return value.astimezone(timezone.utc)


def _row(cursor: Any) -> dict[str, Any] | None:
    value = cursor.fetchone()
    if value is None:
        return None
    if isinstance(value, Mapping):
        return dict(value)
    columns = [item.name if hasattr(item, "name") else item[0] for item in cursor.description]
    return dict(zip(columns, value, strict=True))


def _rows(cursor: Any) -> list[dict[str, Any]]:
    values = cursor.fetchall()
    if not values:
        return []
    if isinstance(values[0], Mapping):
        return [dict(value) for value in values]
    columns = [item.name if hasattr(item, "name") else item[0] for item in cursor.description]
    return [dict(zip(columns, value, strict=True)) for value in values]


def _close(connection: Any) -> None:
    close = getattr(connection, "close", None)
    if callable(close):
        close()
