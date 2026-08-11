"""Provider inventory eligibility and deterministic warm-standby selection."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import ROUND_CEILING, Decimal, InvalidOperation
from enum import Enum
from typing import Any


class ProviderInventoryError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class InventoryState(str, Enum):
    READY = "READY"
    WARM_STANDBY = "WARM_STANDBY"
    VACANT = "VACANT"
    REPAIRING = "REPAIRING"
    RETIRED = "RETIRED"


class LiquidityState(str, Enum):
    FUNDED = "FUNDED"
    REFILLING = "REFILLING"
    UNFUNDED = "UNFUNDED"
    TRANSFER_UNKNOWN = "TRANSFER_UNKNOWN"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class InventoryEntry:
    model: str
    merchant_id: int
    binding_id: int
    account_id: int
    token_id: int
    inventory_state: InventoryState = InventoryState.WARM_STANDBY
    liquidity_state: LiquidityState = LiquidityState.UNKNOWN
    funding_eligible: bool = False
    binding_ready: bool = False
    schedulable: bool = False
    assigned_slot_no: int | None = None
    health_score: int = 0
    user_balance_runway_seconds: int = 0
    withdrawable_units: int = 0
    session_healthy: bool = True
    token_healthy: bool = True
    egress_healthy: bool = True
    recent_provider_success_at: datetime | None = None

    def __post_init__(self) -> None:
        if not str(self.model).strip():
            raise ProviderInventoryError("provider_inventory_model_invalid")
        for name in ("merchant_id", "binding_id", "account_id", "token_id"):
            value = getattr(self, name)
            if isinstance(value, bool) or value < 1:
                raise ProviderInventoryError(f"provider_inventory_{name}_invalid")
        if self.assigned_slot_no is not None and (
            isinstance(self.assigned_slot_no, bool) or self.assigned_slot_no < 1
        ):
            raise ProviderInventoryError("provider_inventory_slot_invalid")
        if isinstance(self.health_score, bool) or self.health_score < 0:
            raise ProviderInventoryError("provider_inventory_health_invalid")
        for name in ("user_balance_runway_seconds", "withdrawable_units"):
            value = getattr(self, name)
            if isinstance(value, bool) or value < 0:
                raise ProviderInventoryError(f"provider_inventory_{name}_invalid")
        for name in ("session_healthy", "token_healthy", "egress_healthy"):
            if not isinstance(getattr(self, name), bool):
                raise ProviderInventoryError(f"provider_inventory_{name}_invalid")
        if self.recent_provider_success_at is not None:
            _aware_datetime(
                self.recent_provider_success_at,
                "provider_inventory_recent_success_invalid",
            )


@dataclass(frozen=True)
class InventoryPlan:
    desired_size: int
    active: tuple[InventoryEntry, ...]
    warm_standby: tuple[InventoryEntry, ...]
    unfunded: tuple[InventoryEntry, ...]
    vacant_slot_nos: tuple[int, ...]


@dataclass(frozen=True)
class ProviderInventoryProjectionReport:
    model: str
    canonical_count: int
    funded_count: int
    changed_count: int
    retired_count: int

    def summary(self) -> dict[str, int | str]:
        return {
            "model": self.model,
            "canonical_count": self.canonical_count,
            "funded_count": self.funded_count,
            "changed_count": self.changed_count,
            "retired_count": self.retired_count,
        }


class PostgresProviderInventoryProjector:
    """Project current Provider identities and financial evidence into inventory."""

    def __init__(self, connection_factory: Callable[[], Any]) -> None:
        if not callable(connection_factory):
            raise ProviderInventoryError("provider_inventory_connection_factory_invalid")
        self.connection_factory = connection_factory

    def reconcile(self, model: str) -> ProviderInventoryProjectionReport:
        model = str(model or "").strip()
        if not model:
            raise ProviderInventoryError("provider_inventory_model_invalid")
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                # Match the Provider activation lock order: model first, inventory
                # second. This keeps the financial and active-slot projections on
                # one stable generation without racing an activation commit.
                cursor.execute(
                    "SELECT pg_advisory_xact_lock(%s, hashtext(%s))",
                    (0x41365050, model),
                )
                cursor.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                    (f"a6-provider-inventory:{model}",),
                )
                cursor.execute(_PROJECT_PROVIDER_INVENTORY_SQL, (model,))
                projected = _mapping_row(cursor.fetchone())
                cursor.execute(_RETIRE_ABSENT_PROVIDER_INVENTORY_SQL, (model,))
                retired_count = max(0, int(getattr(cursor, "rowcount", 0) or 0))
                project_active_provider_inventory(cursor, model)
            connection.commit()
        except Exception:
            rollback = getattr(connection, "rollback", None)
            if callable(rollback):
                rollback()
            raise
        finally:
            close = getattr(connection, "close", None)
            if callable(close):
                close()
        return ProviderInventoryProjectionReport(
            model=model,
            canonical_count=max(0, int(projected.get("canonical_count") or 0)),
            funded_count=max(0, int(projected.get("funded_count") or 0)),
            changed_count=max(0, int(projected.get("changed_count") or 0)),
            retired_count=retired_count,
        )


def project_active_provider_inventory(
    cursor: Any,
    model: str,
    *,
    routing_generation: int | None = None,
    expected_ready_count: int | None = None,
) -> tuple[int, int, int]:
    """Project one active slot window into inventory inside the caller transaction."""

    model = str(model or "").strip()
    if not model:
        raise ProviderInventoryError("provider_inventory_model_invalid")
    if routing_generation is not None and (
        isinstance(routing_generation, bool)
        or not isinstance(routing_generation, int)
        or routing_generation < 1
    ):
        raise ProviderInventoryError("provider_inventory_routing_generation_invalid")
    if expected_ready_count is not None and (
        isinstance(expected_ready_count, bool)
        or not isinstance(expected_ready_count, int)
        or expected_ready_count < 0
    ):
        raise ProviderInventoryError("provider_inventory_ready_count_invalid")
    cursor.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
        (f"a6-provider-inventory:{model}",),
    )
    cursor.execute(
        _PROJECT_ACTIVE_PROVIDER_INVENTORY_SQL,
        (
            model,
            routing_generation,
            routing_generation,
            routing_generation,
            model,
        ),
    )
    row = _active_projection_row(cursor.fetchone())
    active_count = int(row["active_slot_count"])
    projected_count = int(row["projected_count"])
    demoted_count = int(row["demoted_count"])
    if min(active_count, projected_count, demoted_count) < 0:
        raise ProviderInventoryError("provider_inventory_active_projection_invalid")
    if active_count != projected_count or (
        expected_ready_count is not None and active_count != expected_ready_count
    ):
        raise ProviderInventoryError("provider_inventory_active_projection_mismatch")
    return active_count, projected_count, demoted_count


def build_inventory_plan(
    entries: Iterable[InventoryEntry],
    *,
    desired_size: int | None = None,
    initial_max_active_slots: int = 12,
    preserve_desired_size: bool = True,
) -> InventoryPlan:
    """Fill all funded slots first and keep the desired size when funding disappears."""

    if isinstance(initial_max_active_slots, bool) or initial_max_active_slots < 1:
        raise ProviderInventoryError("provider_inventory_max_slots_invalid")
    normalized = sorted(entries, key=lambda item: (item.merchant_id, item.account_id))
    if len({item.merchant_id for item in normalized}) != len(normalized):
        raise ProviderInventoryError("provider_inventory_merchant_duplicate")
    funded = [
        item
        for item in normalized
        if item.funding_eligible
        and item.liquidity_state
        in {
            LiquidityState.FUNDED,
            LiquidityState.REFILLING,
            LiquidityState.TRANSFER_UNKNOWN,
        }
        and item.binding_ready
        and item.inventory_state not in {InventoryState.RETIRED, InventoryState.REPAIRING}
    ]
    if desired_size is None:
        desired = min(len(funded), initial_max_active_slots)
    else:
        if isinstance(desired_size, bool) or desired_size < 1:
            raise ProviderInventoryError("provider_inventory_desired_size_invalid")
        desired = desired_size if preserve_desired_size else min(desired_size, len(funded))
    active = tuple(replace(item, inventory_state=InventoryState.READY) for item in funded[:desired])
    active_ids = {item.merchant_id for item in active}
    standby = tuple(
        replace(item, inventory_state=InventoryState.WARM_STANDBY, assigned_slot_no=None)
        for item in funded
        if item.merchant_id not in active_ids
    )
    unfunded = tuple(
        replace(item, inventory_state=InventoryState.REPAIRING)
        for item in normalized
        if item.merchant_id not in active_ids
        and item.merchant_id not in {entry.merchant_id for entry in standby}
    )
    vacant = tuple(range(len(active) + 1, desired + 1))
    return InventoryPlan(desired, active, standby, unfunded, vacant)


def select_replacement(
    entries: Sequence[InventoryEntry],
    *,
    slot_no: int,
    excluded_account_ids: Iterable[int] = (),
    excluded_token_ids: Iterable[int] = (),
) -> InventoryEntry | None:
    """Choose one unused funded standby account with stable tie-breaking."""

    if isinstance(slot_no, bool) or slot_no < 1:
        raise ProviderInventoryError("provider_inventory_slot_invalid")
    accounts = {int(value) for value in excluded_account_ids}
    tokens = {int(value) for value in excluded_token_ids}
    candidates = [
        item
        for item in entries
        if item.inventory_state is InventoryState.WARM_STANDBY
        and item.liquidity_state
        in {
            LiquidityState.FUNDED,
            LiquidityState.REFILLING,
            LiquidityState.TRANSFER_UNKNOWN,
        }
        and item.funding_eligible
        and item.binding_ready
        and item.session_healthy
        and item.token_healthy
        and item.egress_healthy
        and item.assigned_slot_no is None
        and item.account_id not in accounts
        and item.token_id not in tokens
    ]
    if not candidates:
        return None
    return sorted(
        candidates,
        key=lambda item: (
            -item.user_balance_runway_seconds,
            -item.withdrawable_units,
            -_recent_success_timestamp(item),
            item.merchant_id,
            item.account_id,
        ),
    )[0]


def next_desired_pool_size(
    *,
    size_mode: str,
    current_desired_size: int,
    funded_eligible_provider_count: int,
    now: datetime,
    observation_started_at: datetime,
    last_shrink_at: datetime | None = None,
    p99_concurrency: int = 0,
    safe_concurrency_per_slot: int = 1,
    target_utilization: Decimal | str | int = Decimal("0.7"),
    p99_simultaneous_failed_slots: int = 0,
    providers_needed_for_24h_liquidity_runway: int = 0,
    minimum_active_slots: int = 4,
    initial_max_active_slots: int = 12,
    minimum_observation: timedelta = timedelta(days=14),
    minimum_shrink_interval: timedelta = timedelta(hours=72),
) -> int:
    """Apply the monotonic bootstrap and evidence-based shrink guards."""

    mode = str(size_mode or "").strip().casefold()
    if mode not in {"all_funded", "evidence_based"}:
        raise ProviderInventoryError("provider_inventory_size_mode_invalid")
    integer_values = {
        "current_desired_size": current_desired_size,
        "funded_eligible_provider_count": funded_eligible_provider_count,
        "p99_concurrency": p99_concurrency,
        "safe_concurrency_per_slot": safe_concurrency_per_slot,
        "p99_simultaneous_failed_slots": p99_simultaneous_failed_slots,
        "providers_needed_for_24h_liquidity_runway": (providers_needed_for_24h_liquidity_runway),
        "minimum_active_slots": minimum_active_slots,
        "initial_max_active_slots": initial_max_active_slots,
    }
    for name, value in integer_values.items():
        minimum = (
            1
            if name
            in {
                "safe_concurrency_per_slot",
                "minimum_active_slots",
                "initial_max_active_slots",
            }
            else 0
        )
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or value < minimum
            or value > 2_147_483_647
        ):
            raise ProviderInventoryError(f"provider_inventory_{name}_invalid")
    if minimum_active_slots > initial_max_active_slots:
        raise ProviderInventoryError("provider_inventory_pool_size_bounds_invalid")
    if minimum_observation < timedelta(0) or minimum_shrink_interval <= timedelta(0):
        raise ProviderInventoryError("provider_inventory_shrink_window_invalid")

    current_time = _aware_datetime(now, "provider_inventory_now_invalid")
    observation_start = _aware_datetime(
        observation_started_at,
        "provider_inventory_observation_start_invalid",
    )
    if observation_start > current_time:
        raise ProviderInventoryError("provider_inventory_observation_start_invalid")
    previous_shrink = None
    if last_shrink_at is not None:
        previous_shrink = _aware_datetime(
            last_shrink_at,
            "provider_inventory_last_shrink_invalid",
        )
        if previous_shrink > current_time:
            raise ProviderInventoryError("provider_inventory_last_shrink_invalid")

    if mode == "all_funded":
        return max(
            current_desired_size,
            min(initial_max_active_slots, funded_eligible_provider_count),
        )

    utilization = _positive_decimal(
        target_utilization,
        "provider_inventory_target_utilization_invalid",
    )
    if utilization > 1:
        raise ProviderInventoryError("provider_inventory_target_utilization_invalid")
    sign, digits, exponent = utilization.as_tuple()
    if sign or len(digits) > 18 or exponent < -12 or exponent > 0:
        raise ProviderInventoryError("provider_inventory_target_utilization_invalid")
    concurrency_capacity = Decimal(safe_concurrency_per_slot) * utilization
    concurrency_slots = int(
        (Decimal(p99_concurrency) / concurrency_capacity).to_integral_value(rounding=ROUND_CEILING)
    )
    required = min(
        initial_max_active_slots,
        max(
            minimum_active_slots,
            concurrency_slots,
            p99_simultaneous_failed_slots + 2,
            providers_needed_for_24h_liquidity_runway,
        ),
    )
    if current_desired_size <= required:
        return required
    if current_time - observation_start < minimum_observation:
        return current_desired_size
    if previous_shrink is not None and current_time - previous_shrink < minimum_shrink_interval:
        return current_desired_size
    return max(required, current_desired_size - 1)


def _recent_success_timestamp(item: InventoryEntry) -> float:
    if item.recent_provider_success_at is None:
        return datetime.min.replace(tzinfo=timezone.utc).timestamp()
    return item.recent_provider_success_at.astimezone(timezone.utc).timestamp()


def _aware_datetime(value: datetime, code: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ProviderInventoryError(code)
    return value.astimezone(timezone.utc)


def _positive_decimal(value: Decimal | str | int, code: str) -> Decimal:
    if isinstance(value, bool):
        raise ProviderInventoryError(code)
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ProviderInventoryError(code) from None
    if not parsed.is_finite() or parsed <= 0:
        raise ProviderInventoryError(code)
    return parsed


def _mapping_row(row: object) -> Mapping[str, object]:
    if isinstance(row, Mapping):
        return row
    if row is None:
        raise ProviderInventoryError("provider_inventory_projection_readback_missing")
    try:
        values = tuple(row)  # type: ignore[arg-type]
    except TypeError:
        raise ProviderInventoryError("provider_inventory_projection_readback_invalid") from None
    if len(values) != 3:
        raise ProviderInventoryError("provider_inventory_projection_readback_invalid")
    return dict(
        zip(
            ("canonical_count", "funded_count", "changed_count"),
            values,
            strict=True,
        )
    )


def _active_projection_row(row: object) -> Mapping[str, object]:
    if isinstance(row, Mapping):
        return row
    if row is None:
        raise ProviderInventoryError("provider_inventory_active_projection_missing")
    try:
        values = tuple(row)  # type: ignore[arg-type]
    except TypeError:
        raise ProviderInventoryError("provider_inventory_active_projection_invalid") from None
    if len(values) != 3:
        raise ProviderInventoryError("provider_inventory_active_projection_invalid")
    return dict(
        zip(
            ("active_slot_count", "projected_count", "demoted_count"),
            values,
            strict=True,
        )
    )


_PROJECT_PROVIDER_INVENTORY_SQL = """
WITH canonical AS (
    SELECT merchant.id AS merchant_id,
           binding.id AS enodot_binding_id,
           binding.enodot_account_id,
           token.id AS a6_token_id,
           CASE
               WHEN pending_transfer.id IS NOT NULL THEN 'TRANSFER_UNKNOWN'
               WHEN financial.merchant_id IS NULL THEN 'UNKNOWN'
               WHEN binding.last_error_class IN (
                        'account_quota_insufficient',
                        'insufficient_quota',
                        'provider_user_balance_insufficient_unfunded'
                    )
                    AND greatest(coalesce(financial.withdrawable_micros, 0), 0)
                        < merchant.quota_per_unit
               THEN 'UNFUNDED'
               WHEN greatest(coalesce(financial.user_balance_quota, 0), 0)
                        >= 2 * merchant.quota_per_unit
               THEN 'FUNDED'
               ELSE 'REFILLING'
           END AS liquidity_state,
           financial.merchant_id IS NOT NULL
               AND NOT (
                   coalesce(binding.last_error_class IN (
                       'account_quota_insufficient',
                       'insufficient_quota',
                       'provider_user_balance_insufficient_unfunded'
                   ), false)
                   AND greatest(coalesce(financial.withdrawable_micros, 0), 0)
                       < merchant.quota_per_unit
               )
                   AS funding_eligible,
           greatest(coalesce(financial.user_balance_quota, 0), 0)
               AS balance_units,
           financial.source_hash AS financial_source_hash,
           financial.observed_at AS last_financial_observed_at
    FROM a6_trade.a6_merchants merchant
    JOIN a6_trade.a6_tokens token
      ON token.merchant_id = merchant.id
     AND token.status IN ('1', 'active', 'enabled')
    JOIN a6_trade.enodot_account_bindings binding
      ON binding.a6_token_id = token.id
     AND binding.state = 'bound'
     AND binding.enodot_account_id IS NOT NULL
    LEFT JOIN a6_trade.a6_financial_current financial
      ON financial.merchant_id = merchant.id
    LEFT JOIN LATERAL (
        SELECT intent.id
        FROM a6_trade.a6_balance_transfer_intents intent
        WHERE intent.merchant_id = merchant.id
          AND intent.state = 'executing'
          AND intent.idempotency_key LIKE 'provider-topup:%%'
        ORDER BY intent.id DESC
        LIMIT 1
    ) pending_transfer ON true
    WHERE 'provider' = ANY(merchant.roles)
      AND merchant.a6_user_id IS NOT NULL
      AND merchant.status = 'supplier_submitted'
      AND merchant.quota_per_unit > 0
), upserted AS (
    INSERT INTO a6_trade.provider_inventory (
        model, merchant_id, enodot_binding_id, enodot_account_id,
        a6_token_id, inventory_state, liquidity_state, funding_eligible,
        schedulable, balance_units, financial_source_hash,
        assigned_slot_no, last_financial_observed_at
    )
    SELECT %s, canonical.merchant_id, canonical.enodot_binding_id,
           canonical.enodot_account_id, canonical.a6_token_id,
           CASE WHEN canonical.funding_eligible
                THEN 'WARM_STANDBY' ELSE 'REPAIRING' END,
           canonical.liquidity_state, canonical.funding_eligible,
           false, canonical.balance_units, canonical.financial_source_hash,
           NULL, canonical.last_financial_observed_at
    FROM canonical
    ON CONFLICT (model, merchant_id) DO UPDATE SET
        enodot_binding_id = EXCLUDED.enodot_binding_id,
        enodot_account_id = EXCLUDED.enodot_account_id,
        a6_token_id = EXCLUDED.a6_token_id,
        inventory_state = CASE
            WHEN EXCLUDED.funding_eligible
                 AND provider_inventory.inventory_state = 'READY'
            THEN 'READY'
            ELSE EXCLUDED.inventory_state
        END,
        liquidity_state = EXCLUDED.liquidity_state,
        funding_eligible = EXCLUDED.funding_eligible,
        schedulable = CASE
            WHEN EXCLUDED.funding_eligible
                 AND provider_inventory.inventory_state = 'READY'
            THEN provider_inventory.schedulable
            ELSE false
        END,
        balance_units = EXCLUDED.balance_units,
        financial_source_hash = EXCLUDED.financial_source_hash,
        assigned_slot_no = CASE
            WHEN EXCLUDED.funding_eligible
                 AND provider_inventory.inventory_state = 'READY'
            THEN provider_inventory.assigned_slot_no
            ELSE NULL
        END,
        state_version = provider_inventory.state_version + 1,
        last_financial_observed_at = EXCLUDED.last_financial_observed_at,
        updated_at = now()
    WHERE provider_inventory.enodot_binding_id
              IS DISTINCT FROM EXCLUDED.enodot_binding_id
       OR provider_inventory.enodot_account_id
              IS DISTINCT FROM EXCLUDED.enodot_account_id
       OR provider_inventory.a6_token_id IS DISTINCT FROM EXCLUDED.a6_token_id
       OR provider_inventory.inventory_state IS DISTINCT FROM CASE
              WHEN EXCLUDED.funding_eligible
                   AND provider_inventory.inventory_state = 'READY'
              THEN 'READY'
              ELSE EXCLUDED.inventory_state
          END
       OR provider_inventory.liquidity_state
              IS DISTINCT FROM EXCLUDED.liquidity_state
       OR provider_inventory.funding_eligible
              IS DISTINCT FROM EXCLUDED.funding_eligible
       OR provider_inventory.schedulable IS DISTINCT FROM CASE
              WHEN EXCLUDED.funding_eligible
                   AND provider_inventory.inventory_state = 'READY'
              THEN provider_inventory.schedulable
              ELSE false
          END
       OR provider_inventory.balance_units IS DISTINCT FROM EXCLUDED.balance_units
       OR provider_inventory.financial_source_hash
              IS DISTINCT FROM EXCLUDED.financial_source_hash
       OR provider_inventory.assigned_slot_no IS DISTINCT FROM CASE
              WHEN EXCLUDED.funding_eligible
                   AND provider_inventory.inventory_state = 'READY'
              THEN provider_inventory.assigned_slot_no
              ELSE NULL
          END
       OR provider_inventory.last_financial_observed_at
              IS DISTINCT FROM EXCLUDED.last_financial_observed_at
    RETURNING merchant_id
)
SELECT (SELECT count(*) FROM canonical)::bigint AS canonical_count,
       (SELECT count(*) FROM canonical WHERE funding_eligible)::bigint
           AS funded_count,
       count(*)::bigint AS changed_count
FROM upserted
"""


_RETIRE_ABSENT_PROVIDER_INVENTORY_SQL = """
UPDATE a6_trade.provider_inventory inventory
SET inventory_state = 'RETIRED', liquidity_state = 'UNKNOWN',
    funding_eligible = false, schedulable = false,
    assigned_slot_no = NULL, state_version = state_version + 1,
    updated_at = now()
WHERE inventory.model = %s
  AND (
      inventory.inventory_state <> 'RETIRED'
      OR inventory.liquidity_state <> 'UNKNOWN'
      OR inventory.funding_eligible
      OR inventory.schedulable
      OR inventory.assigned_slot_no IS NOT NULL
  )
  AND NOT EXISTS (
      SELECT 1
      FROM a6_trade.a6_merchants merchant
      JOIN a6_trade.a6_tokens token
        ON token.merchant_id = merchant.id
       AND token.status IN ('1', 'active', 'enabled')
      JOIN a6_trade.enodot_account_bindings binding
        ON binding.a6_token_id = token.id
       AND binding.state = 'bound'
       AND binding.enodot_account_id IS NOT NULL
      WHERE merchant.id = inventory.merchant_id
        AND 'provider' = ANY(merchant.roles)
        AND merchant.a6_user_id IS NOT NULL
        AND merchant.status = 'supplier_submitted'
        AND merchant.quota_per_unit > 0
  )
"""


_PROJECT_ACTIVE_PROVIDER_INVENTORY_SQL = """
WITH active_slots AS MATERIALIZED (
    SELECT slot.model, slot.slot_no, slot.enodot_binding_id,
           slot.enodot_account_id, slot.a6_token_id,
           slot.observed_schedulable
    FROM a6_trade.provider_pool_slots slot
    WHERE slot.model = %s
      AND slot.valid_to IS NULL
      AND slot.slot_state = 'READY'
      AND (
          (
              %s::bigint IS NOT NULL
              AND slot.routing_generation = %s::bigint
          )
          OR (
              %s::bigint IS NULL
              AND EXISTS (
                  SELECT 1
                  FROM a6_trade.provider_pool_generations generation
                  WHERE generation.model = slot.model
                    AND generation.generation = slot.routing_generation
                    AND generation.generation_state = 'active'
              )
          )
      )
), matched_inventory AS MATERIALIZED (
    SELECT inventory.merchant_id, inventory.model,
           active.slot_no, active.observed_schedulable
    FROM a6_trade.provider_inventory inventory
    JOIN active_slots active
      ON inventory.model = active.model
     AND inventory.enodot_binding_id = active.enodot_binding_id
     AND inventory.enodot_account_id = active.enodot_account_id
     AND inventory.a6_token_id = active.a6_token_id
), projected AS (
    UPDATE a6_trade.provider_inventory inventory
    SET inventory_state = 'READY',
        schedulable = matched.observed_schedulable,
        assigned_slot_no = matched.slot_no,
        state_version = inventory.state_version + 1,
        updated_at = now()
    FROM matched_inventory matched
    WHERE inventory.model = matched.model
      AND inventory.merchant_id = matched.merchant_id
      AND (
          inventory.inventory_state IS DISTINCT FROM 'READY'
          OR inventory.schedulable IS DISTINCT FROM matched.observed_schedulable
          OR inventory.assigned_slot_no IS DISTINCT FROM matched.slot_no
      )
    RETURNING inventory.merchant_id
), demoted AS (
    UPDATE a6_trade.provider_inventory inventory
    SET inventory_state = CASE
            WHEN inventory.inventory_state = 'READY'
                 AND inventory.funding_eligible
            THEN 'WARM_STANDBY'
            WHEN inventory.inventory_state = 'READY'
            THEN 'REPAIRING'
            ELSE inventory.inventory_state
        END,
        schedulable = false,
        assigned_slot_no = NULL,
        state_version = inventory.state_version + 1,
        updated_at = now()
    WHERE inventory.model = %s
      AND (
          inventory.inventory_state = 'READY'
          OR inventory.schedulable
          OR inventory.assigned_slot_no IS NOT NULL
      )
      AND NOT EXISTS (
          SELECT 1
          FROM active_slots active
          WHERE active.model = inventory.model
            AND active.enodot_binding_id = inventory.enodot_binding_id
            AND active.enodot_account_id = inventory.enodot_account_id
            AND active.a6_token_id = inventory.a6_token_id
      )
    RETURNING inventory.merchant_id
)
SELECT (SELECT count(*) FROM active_slots)::bigint AS active_slot_count,
       (SELECT count(*) FROM matched_inventory)::bigint AS projected_count,
       (SELECT count(*) FROM demoted)::bigint AS demoted_count
"""
