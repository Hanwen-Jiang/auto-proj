"""Planning and runtime-context boundaries for Provider pool remote apply."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from types import MappingProxyType
from typing import Any

from .trade_provider_cost import PoolDiffKind
from .trade_provider_pool import ProviderBundle, ProviderSlot, SlotState
from .trade_provider_pool_shadow import ProviderPoolShadowInput, ProviderPoolShadowReconciler
from .trade_provider_pool_shadow_controller import (
    ActivePoolProjection,
    PlannedPoolProjection,
    ProviderPoolPlanningEvidence,
    ProviderPoolPlanningEvidenceSource,
    ProviderPoolShadowControllerConfig,
    ProviderPoolShadowControllerError,
    ProviderPoolShadowControllerReport,
    SingleModelProviderPoolShadowController,
)


class ProviderPoolApplyError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class ProviderPoolApplyStage(str, Enum):
    """Monotonic production rollout stages for remote Provider mutations."""

    DISABLED = "DISABLED"
    COST_ONLY = "COST_ONLY"
    PRIORITY_AND_COST = "PRIORITY_AND_COST"
    MEMBERSHIP_AND_COST = "MEMBERSHIP_AND_COST"
    FULL = "FULL"


@dataclass(frozen=True)
class ProviderPoolApplyPolicy:
    """Fail-closed standing approval for one bounded rollout stage."""

    stage: ProviderPoolApplyStage
    max_membership_changes: int
    confirmation: str

    def __post_init__(self) -> None:
        if not isinstance(self.stage, ProviderPoolApplyStage):
            raise ProviderPoolApplyError("provider_apply_stage_invalid")
        if (
            isinstance(self.max_membership_changes, bool)
            or not isinstance(self.max_membership_changes, int)
            or self.max_membership_changes < 0
        ):
            raise ProviderPoolApplyError("provider_apply_membership_limit_invalid")
        if not isinstance(self.confirmation, str):
            raise ProviderPoolApplyError("provider_apply_confirmation_invalid")

    def expected_confirmation(
        self,
        *,
        model: str,
        config_version: str,
        billing_contract: str,
    ) -> str:
        fields = (
            "a6-provider-pool",
            str(model).strip(),
            str(config_version).strip(),
            self.stage.value,
            f"max-membership={self.max_membership_changes}",
            str(billing_contract).strip(),
        )
        if any(not value for value in fields):
            raise ProviderPoolApplyError("provider_apply_confirmation_scope_invalid")
        return ":".join(fields)

    def authorize(
        self,
        plan: ProviderPoolApplyPlan,
        *,
        billing_contract: str,
    ) -> None:
        if not isinstance(getattr(plan, "diff_kind", None), PoolDiffKind) or not isinstance(
            getattr(plan, "changed_slot_nos", None), tuple
        ):
            raise ProviderPoolApplyError("provider_apply_plan_invalid")
        if self.stage is ProviderPoolApplyStage.DISABLED:
            raise ProviderPoolApplyError("provider_apply_stage_disabled")
        allowed = {
            ProviderPoolApplyStage.COST_ONLY: {
                PoolDiffKind.NOOP,
                PoolDiffKind.COST_ONLY,
            },
            ProviderPoolApplyStage.PRIORITY_AND_COST: {
                PoolDiffKind.NOOP,
                PoolDiffKind.COST_ONLY,
                PoolDiffKind.PRIORITY_AND_COST,
            },
            ProviderPoolApplyStage.MEMBERSHIP_AND_COST: {
                PoolDiffKind.NOOP,
                PoolDiffKind.COST_ONLY,
                PoolDiffKind.PRIORITY_AND_COST,
                PoolDiffKind.MEMBERSHIP_AND_COST,
            },
            ProviderPoolApplyStage.FULL: set(PoolDiffKind),
        }[self.stage]
        if plan.diff_kind not in allowed:
            raise ProviderPoolApplyError("provider_apply_diff_not_authorized")
        if plan.diff_kind in {
            PoolDiffKind.MEMBERSHIP_AND_COST,
            PoolDiffKind.LIQUIDITY_REPLACEMENT,
        }:
            if self.max_membership_changes < 1:
                raise ProviderPoolApplyError("provider_apply_membership_limit_required")
            if len(plan.changed_slot_nos) > self.max_membership_changes:
                raise ProviderPoolApplyError("provider_apply_membership_limit_exceeded")
        expected = self.expected_confirmation(
            model=plan.bundle.model,
            config_version=plan.bundle.config_version,
            billing_contract=billing_contract,
        )
        if not self.confirmation or self.confirmation != expected:
            raise ProviderPoolApplyError("provider_apply_confirmation_mismatch")


@dataclass(frozen=True)
class ProviderPoolApplyPlan:
    """One immutable desired bundle plus the active state it was planned from."""

    report: ProviderPoolShadowControllerReport
    active: ActivePoolProjection | None
    verified_slots: Mapping[int, ProviderSlot] | None = None

    @property
    def bundle(self) -> ProviderBundle:
        return self.report.bundle

    @property
    def diff_kind(self) -> PoolDiffKind:
        return self.report.shadow.diff_kind

    @property
    def current_routing(self) -> tuple[int, ...]:
        return () if self.active is None else self.active.ordered_channel_ids

    @property
    def current_assignments(self) -> Mapping[int, str | int | None]:
        if self.active is None:
            return MappingProxyType({})
        return self.active.assignments

    @property
    def current_slots(self) -> Mapping[int, ProviderSlot]:
        if self.active is None:
            return MappingProxyType({})
        return MappingProxyType({slot.slot_no: slot for slot in self.active.slots})

    @property
    def current_cost_input_hash(self) -> str:
        return "0" * 64 if self.active is None else self.active.cost_input_hash

    @property
    def context_slots(self) -> tuple[ProviderSlot, ...]:
        current = () if self.active is None else self.active.slots
        return tuple(current) + tuple(self.bundle.slots)

    @property
    def desired_token_ids(self) -> frozenset[int]:
        return frozenset(
            int(slot.token_id)
            for slot in self.bundle.ready_slots
            if slot.token_id is not None
        )

    @property
    def verified_slot_nos(self) -> tuple[int, ...]:
        return tuple(sorted((self.verified_slots or {}).keys()))

    @property
    def changed_slot_nos(self) -> tuple[int, ...]:
        current = self.current_slots
        desired = {slot.slot_no: slot for slot in self.bundle.slots}
        changed: list[int] = []
        for slot_no in sorted(set(current) | set(desired)):
            previous = current.get(slot_no)
            target = desired.get(slot_no)
            previous_identity = _slot_identity(previous)
            target_identity = _slot_identity(target)
            if previous_identity != target_identity:
                changed.append(slot_no)
        return tuple(changed)

    @property
    def billing_finalize_required(self) -> bool:
        if self.active is None:
            return False
        return (
            self.active.observed_billing_floor_multiplier
            != self.active.billing_floor_multiplier
        )

    def summary(self) -> dict[str, Any]:
        summary = self.report.summary()
        summary["mode"] = "remote-apply"
        summary["persisted"] = False
        summary["activated"] = False
        summary["remote_write_count"] = 0
        return summary


class ProviderPoolApplyPlanner:
    """Build an apply plan from exactly one planning-evidence snapshot."""

    def __init__(
        self,
        source: ProviderPoolPlanningEvidenceSource,
        config: ProviderPoolShadowControllerConfig,
        *,
        applying_replan_handler: Callable[[PlannedPoolProjection, str], None]
        | None = None,
        applying_bundle_loader: Callable[[PlannedPoolProjection], ProviderBundle | None]
        | None = None,
    ) -> None:
        self.source = source
        self.config = config
        self.applying_replan_handler = applying_replan_handler
        self.applying_bundle_loader = applying_bundle_loader
        self._rejected_channel_ids: set[int] = set()
        self._rejected_provider_identities: set[tuple[int, int]] = set()
        self._verified_slots: dict[int, ProviderSlot] = {}
        self._applying_replans: list[tuple[int, str]] = []

    @property
    def rejected_channel_ids(self) -> frozenset[int]:
        return frozenset(self._rejected_channel_ids)

    @property
    def verified_slots(self) -> Mapping[int, ProviderSlot]:
        return MappingProxyType(dict(self._verified_slots))

    @property
    def rejected_provider_identities(self) -> frozenset[tuple[int, int]]:
        return frozenset(self._rejected_provider_identities)

    @property
    def applying_replans(self) -> tuple[tuple[int, str], ...]:
        return tuple(self._applying_replans)

    def reject_channel(self, channel_id: int) -> None:
        if (
            isinstance(channel_id, bool)
            or not isinstance(channel_id, int)
            or channel_id < 1
        ):
            raise ProviderPoolApplyError("provider_apply_rejected_channel_invalid")
        self._rejected_channel_ids.add(channel_id)
        self._verified_slots = {
            slot_no: slot
            for slot_no, slot in self._verified_slots.items()
            if slot.channel_id != channel_id
        }

    def reject_provider(self, account_id: int, token_id: int) -> None:
        for name, value in (("account", account_id), ("token", token_id)):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ProviderPoolApplyError(f"provider_apply_rejected_{name}_invalid")
        identity = (account_id, token_id)
        self._rejected_provider_identities.add(identity)
        self._verified_slots = {
            slot_no: slot
            for slot_no, slot in self._verified_slots.items()
            if (slot.account_id, slot.token_id) != identity
        }

    def restore_provider(self, account_id: int, token_id: int) -> None:
        for name, value in (("account", account_id), ("token", token_id)):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ProviderPoolApplyError(f"provider_apply_rejected_{name}_invalid")
        self._rejected_provider_identities.discard((account_id, token_id))

    def retain_verified_slots(
        self,
        plan: ProviderPoolApplyPlan,
        slot_nos: Sequence[int],
    ) -> None:
        desired = {slot.slot_no: slot for slot in plan.bundle.ready_slots}
        for raw_slot_no in slot_nos:
            if (
                isinstance(raw_slot_no, bool)
                or not isinstance(raw_slot_no, int)
                or raw_slot_no < 1
            ):
                raise ProviderPoolApplyError("provider_apply_verified_slot_invalid")
            slot = desired.get(raw_slot_no)
            if (
                slot is None
                or slot.channel_id in self._rejected_channel_ids
                or (slot.account_id, slot.token_id) in self._rejected_provider_identities
            ):
                raise ProviderPoolApplyError("provider_apply_verified_slot_invalid")
            self._verified_slots[raw_slot_no] = slot

    def plan(self, *, now: datetime | None = None) -> ProviderPoolApplyPlan:
        planning_now = now or datetime.now(timezone.utc)
        applying_replan_attempted = False
        while True:
            captured = _CapturingEvidenceSource(
                self.source,
                rejected_channel_ids=self.rejected_channel_ids,
                rejected_provider_identities=self.rejected_provider_identities,
                slot_hints=self.verified_slots,
            )
            try:
                report = SingleModelProviderPoolShadowController(
                    captured,
                    self.config,
                ).run(
                    now=planning_now,
                    persist=False,
                )
            except ProviderPoolShadowControllerError as exc:
                latest = (
                    None
                    if captured.evidence is None
                    else captured.evidence.latest_planned
                )
                resumed = self._resume_applying(latest)
                if resumed is not None:
                    assert captured.evidence is not None
                    report = _resumed_applying_report(resumed, captured.evidence)
                    break
                if not self._can_replan_applying(
                    latest,
                    code=exc.code,
                    already_attempted=applying_replan_attempted,
                ):
                    raise
                assert latest is not None
                assert self.applying_replan_handler is not None
                self.applying_replan_handler(latest, exc.code)
                self._applying_replans.append((latest.routing_generation, exc.code))
                applying_replan_attempted = True
                continue
            latest = (
                None
                if captured.evidence is None
                else captured.evidence.latest_planned
            )
            resumed = self._resume_applying(latest)
            if resumed is not None:
                assert captured.evidence is not None
                report = _resumed_applying_report(resumed, captured.evidence)
            break
        if captured.evidence is None:
            raise ProviderPoolApplyError("provider_apply_planning_evidence_missing")
        desired = {slot.slot_no: slot for slot in report.bundle.ready_slots}
        retained: dict[int, ProviderSlot] = {}
        for slot_no, hint in self._verified_slots.items():
            slot = desired.get(slot_no)
            if slot is None or _slot_identity(slot) != _slot_identity(hint):
                raise ProviderPoolApplyError("provider_apply_verified_slot_not_retained")
            retained[slot_no] = slot
        return ProviderPoolApplyPlan(
            report,
            captured.evidence.active,
            MappingProxyType(retained),
        )

    def _resume_applying(
        self,
        latest: PlannedPoolProjection | None,
    ) -> ProviderBundle | None:
        if (
            self.applying_bundle_loader is None
            or latest is None
            or latest.generation_state != "applying"
            or latest.pricing_state not in {"applying", "confirmed"}
            or latest.config_version != self.config.config_version
            or latest.generation_source != self.config.generation_source
        ):
            return None
        bundle = self.applying_bundle_loader(latest)
        if bundle is None:
            return None
        if (
            bundle.model != self.config.model
            or bundle.routing_generation != latest.routing_generation
            or bundle.cost_generation != latest.cost_generation
            or bundle.parent_routing_generation != latest.parent_routing_generation
            or bundle.parent_cost_generation != latest.parent_cost_generation
            or bundle.desired_size != latest.desired_size
            or bundle.ordered_channel_ids != latest.ordered_channel_ids
            or bundle.standby_channel_ids != latest.standby_channel_ids
            or bundle.effective_cost_input_hash != latest.cost_input_hash
            or bundle.cost_plan.cost_output_hash != latest.cost_output_hash
            or bundle.config_version != latest.config_version
            or bundle.source_hash != latest.source_hash
            or bundle.generation_source != latest.generation_source
        ):
            raise ProviderPoolApplyError("provider_apply_frozen_plan_mismatch")
        return bundle

    def _can_replan_applying(
        self,
        latest: PlannedPoolProjection | None,
        *,
        code: str,
        already_attempted: bool,
    ) -> bool:
        return bool(
            not already_attempted
            and self.applying_replan_handler is not None
            and latest is not None
            and latest.generation_state == "applying"
            and latest.pricing_state in {"applying", "confirmed"}
            and latest.config_version == self.config.config_version
            and latest.generation_source == self.config.generation_source
            and code
            in {
                "provider_shadow_controller_apply_in_progress_replan_required",
                "provider_shadow_controller_applying_cost_no_longer_covers",
            }
        )


class _CapturingEvidenceSource:
    def __init__(
        self,
        source: ProviderPoolPlanningEvidenceSource,
        *,
        rejected_channel_ids: frozenset[int] = frozenset(),
        rejected_provider_identities: frozenset[tuple[int, int]] = frozenset(),
        slot_hints: Mapping[int, ProviderSlot] | None = None,
    ) -> None:
        self.source = source
        self.rejected_channel_ids = rejected_channel_ids
        self.rejected_provider_identities = rejected_provider_identities
        self.slot_hints = MappingProxyType(dict(slot_hints or {}))
        self.evidence: ProviderPoolPlanningEvidence | None = None

    def load(self, model: str, **kwargs: Any) -> ProviderPoolPlanningEvidence:
        if self.evidence is not None:
            raise ProviderPoolApplyError("provider_apply_planning_evidence_reloaded")
        evidence = self.source.load(model, **kwargs)
        self.evidence = evidence
        if self.rejected_channel_ids:
            quarantines = dict(evidence.quarantines or {})
            never_reuse = datetime.max.replace(tzinfo=timezone.utc)
            for channel_id in self.rejected_channel_ids:
                quarantines[channel_id] = never_reuse
            evidence = replace(evidence, quarantines=quarantines)
        if self.rejected_provider_identities:
            inventory = tuple(
                item
                for item in evidence.inventory
                if (item.account_id, item.token_id)
                not in self.rejected_provider_identities
            )
            hints = {
                slot_no: slot
                for slot_no, slot in dict(evidence.slot_hints or {}).items()
                if (slot.account_id, slot.token_id)
                not in self.rejected_provider_identities
            }
            evidence = replace(evidence, inventory=inventory, slot_hints=hints)
        if self.slot_hints:
            evidence = replace(evidence, slot_hints=self.slot_hints)
        return evidence


def _resumed_applying_report(
    bundle: ProviderBundle,
    evidence: ProviderPoolPlanningEvidence,
) -> ProviderPoolShadowControllerReport:
    active = evidence.active
    desired = ProviderPoolShadowInput(
        bundle=bundle,
        current_routing=() if active is None else active.ordered_channel_ids,
        current_assignments={} if active is None else active.assignments,
        current_cost_input_hash=("0" * 64 if active is None else active.cost_input_hash),
    )
    shadow = ProviderPoolShadowReconciler().reconcile(desired, persist=False)
    return ProviderPoolShadowControllerReport(
        shadow=shadow,
        bundle=bundle,
        snapshot_sequence=evidence.snapshot.sequence,
        snapshot_candidate_count=len(evidence.snapshot.candidates),
        eligible_count=len(evidence.snapshot.candidates),
        top_count=len(bundle.ordered_channel_ids),
        standby_count=len(bundle.standby_channel_ids),
        vacant_count=sum(slot.slot_state is SlotState.VACANT for slot in bundle.slots),
        excluded=(),
        reused_planned=True,
        realized_cost_sample_count=evidence.realized_cost_sample_count,
        realized_cost_p95_multiplier=evidence.realized_cost_p95_multiplier,
        shadow_evidence={"resume_source": "persisted_applying_plan"},
        pool_size_evidence={},
    )


@dataclass(frozen=True)
class ProviderRemoteContextRecord:
    """Non-secret database identity used to assemble one remote slot context."""

    token_ref_id: int
    external_token_id: int
    merchant_id: int
    a6_user_id: int
    session_ref: str
    merchant_status: str
    roles: tuple[str, ...]
    token_status: str
    key_fingerprint: str
    key_secret_ref: str
    enodot_binding_id: int
    enodot_account_id: int
    binding_state: str
    model_scope: tuple[str, ...]

    def __post_init__(self) -> None:
        identities = (
            self.token_ref_id,
            self.external_token_id,
            self.merchant_id,
            self.a6_user_id,
            self.enodot_binding_id,
            self.enodot_account_id,
        )
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 1
            for value in identities
        ):
            raise ProviderPoolApplyError("provider_apply_context_identity_invalid")
        if not self.session_ref:
            raise ProviderPoolApplyError("provider_apply_context_session_ref_invalid")
        fingerprint = self.key_fingerprint.casefold()
        if len(fingerprint) != 64 or any(value not in "0123456789abcdef" for value in fingerprint):
            raise ProviderPoolApplyError("provider_apply_context_key_fingerprint_invalid")
        if not self.key_secret_ref:
            raise ProviderPoolApplyError("provider_apply_context_key_secret_ref_invalid")
        object.__setattr__(self, "key_fingerprint", fingerprint)


class PostgresProviderRemoteContextSource:
    """Read and validate the exact Token/binding/account identities used by a plan."""

    def __init__(self, connection_factory: Callable[[], Any]) -> None:
        if not callable(connection_factory):
            raise ProviderPoolApplyError("provider_apply_context_source_invalid")
        self.connection_factory = connection_factory

    def load(
        self,
        model: str,
        slots: Sequence[ProviderSlot],
        *,
        desired_token_ids: frozenset[int],
    ) -> tuple[ProviderRemoteContextRecord, ...]:
        model = str(model or "").strip()
        if not model:
            raise ProviderPoolApplyError("provider_apply_context_model_invalid")
        expected = _expected_slot_identities(slots)
        desired = _positive_ids(
            desired_token_ids,
            "provider_apply_context_desired_token_invalid",
        )
        if not desired.issubset(expected):
            raise ProviderPoolApplyError("provider_apply_context_desired_token_missing")
        if not expected:
            return ()

        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    _REMOTE_CONTEXT_SQL,
                    (sorted(expected), sorted({value[0] for value in expected.values()})),
                )
                rows = _rows(cursor)
        finally:
            _close(connection)

        records: dict[int, ProviderRemoteContextRecord] = {}
        for row in rows:
            record = _context_record(row)
            identity = expected.get(record.token_ref_id)
            if identity is None or identity != (
                record.enodot_binding_id,
                record.enodot_account_id,
            ):
                continue
            if record.token_ref_id in records:
                raise ProviderPoolApplyError("provider_apply_context_duplicate")
            if record.token_ref_id in desired:
                _validate_desired_context(record, model=model)
            records[record.token_ref_id] = record
        if set(records) != set(expected):
            raise ProviderPoolApplyError("provider_apply_context_missing")
        return tuple(records[token_id] for token_id in sorted(records))


def _expected_slot_identities(
    slots: Sequence[ProviderSlot],
) -> dict[int, tuple[int, int]]:
    result: dict[int, tuple[int, int]] = {}
    for slot in slots:
        if not isinstance(slot, ProviderSlot):
            raise ProviderPoolApplyError("provider_apply_context_slot_invalid")
        if slot.slot_state is SlotState.VACANT:
            continue
        if (
            slot.token_id is None
            or slot.enodot_binding_id is None
            or slot.account_id is None
        ):
            raise ProviderPoolApplyError("provider_apply_context_slot_identity_missing")
        identity = (int(slot.enodot_binding_id), int(slot.account_id))
        previous = result.setdefault(int(slot.token_id), identity)
        if previous != identity:
            raise ProviderPoolApplyError("provider_apply_context_slot_identity_conflict")
    return result


def _slot_identity(slot: ProviderSlot | None) -> tuple[object, ...] | None:
    if slot is None or slot.slot_state is SlotState.VACANT:
        return None
    return (
        slot.assignment_id,
        slot.enodot_binding_id,
        slot.token_id,
        slot.account_id,
        slot.channel_id,
    )


def _validate_desired_context(record: ProviderRemoteContextRecord, *, model: str) -> None:
    if record.merchant_status != "supplier_submitted":
        raise ProviderPoolApplyError("provider_apply_context_merchant_status_invalid")
    if "provider" not in record.roles:
        raise ProviderPoolApplyError("provider_apply_context_role_invalid")
    if record.token_status.casefold() not in {"1", "active", "enabled"}:
        raise ProviderPoolApplyError("provider_apply_context_token_status_invalid")
    if record.binding_state != "bound":
        raise ProviderPoolApplyError("provider_apply_context_binding_state_invalid")
    if model not in record.model_scope:
        raise ProviderPoolApplyError("provider_apply_context_model_scope_invalid")


def _context_record(row: Mapping[str, Any]) -> ProviderRemoteContextRecord:
    try:
        return ProviderRemoteContextRecord(
            token_ref_id=int(row["token_ref_id"]),
            external_token_id=int(row["external_token_id"]),
            merchant_id=int(row["merchant_id"]),
            a6_user_id=int(row["a6_user_id"]),
            session_ref=str(row["session_ref"]),
            merchant_status=str(row["merchant_status"]),
            roles=tuple(str(value) for value in (row.get("roles") or ())),
            token_status=str(row["token_status"]),
            key_fingerprint=str(row.get("key_fingerprint") or ""),
            key_secret_ref=str(row.get("key_secret_ref") or ""),
            enodot_binding_id=int(row["enodot_binding_id"]),
            enodot_account_id=int(row["enodot_account_id"]),
            binding_state=str(row["binding_state"]),
            model_scope=tuple(str(value) for value in (row.get("model_scope") or ())),
        )
    except (KeyError, TypeError, ValueError):
        raise ProviderPoolApplyError("provider_apply_context_row_invalid") from None


def _positive_ids(values: Sequence[int], code: str) -> frozenset[int]:
    result: set[int] = set()
    for value in values:
        if isinstance(value, bool):
            raise ProviderPoolApplyError(code)
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            raise ProviderPoolApplyError(code) from None
        if parsed < 1:
            raise ProviderPoolApplyError(code)
        result.add(parsed)
    return frozenset(result)


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


_REMOTE_CONTEXT_SQL = """
SELECT token.id AS token_ref_id, token.a6_token_id AS external_token_id,
       token.status AS token_status, token.key_fingerprint, token.key_secret_ref,
       merchant.id AS merchant_id, merchant.a6_user_id, merchant.session_ref,
       merchant.status AS merchant_status, merchant.roles,
       binding.id AS enodot_binding_id, binding.enodot_account_id,
       binding.state AS binding_state, binding.model_scope
FROM a6_trade.a6_tokens token
JOIN a6_trade.a6_merchants merchant ON merchant.id = token.merchant_id
JOIN a6_trade.enodot_account_bindings binding ON binding.a6_token_id = token.id
WHERE token.id = ANY(%s)
  AND binding.id = ANY(%s)
ORDER BY token.id, binding.id
"""
