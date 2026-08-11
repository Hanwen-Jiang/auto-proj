"""Canonical, credential-free evidence for Provider pool shadow observations."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from decimal import Decimal
from typing import Any

from .trade_provider_cost import CostPlan
from .trade_provider_pool import ProviderBundle, SlotState
from .trade_provider_pool_rank import Candidate, ObserverSnapshot, RankedPool, cost_input_document

SHADOW_EVIDENCE_SCHEMA = "a6-provider-shadow-evidence-v1"


def canonical_sha256(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def candidate_identity(row: Candidate) -> dict[str, int | None]:
    return {
        "channel_id": row.channel_id,
        "supplier_id": row.supplier_id,
        "listing_id": row.listing_id,
    }


def identity_digest(scope: str, identities: Sequence[Mapping[str, Any]]) -> str:
    return canonical_sha256(
        {
            "scope": str(scope),
            "identities": [dict(identity) for identity in identities],
        }
    )


def cost_output_document(plan: CostPlan) -> dict[str, Any]:
    value = plan.public()
    value.pop("cost_output_hash")
    return value


def build_shadow_evidence(
    *,
    snapshot: ObserverSnapshot,
    source_hash: str,
    ranked: RankedPool,
    owned_supplier_ids: Iterable[int],
    owned_channel_ids: Iterable[int],
    owned_listing_ids: Iterable[int],
    bundle: ProviderBundle,
    previous_billing_floor: Decimal,
    conservative_fallback_cost: Decimal | str | int,
    retry_reserve_window: str,
    realized_cost_window: str,
    quantization_version: str,
    raw_realized_cost_sample_count: int,
    raw_realized_cost_p95_multiplier: Decimal | None,
    minimum_realized_cost_samples: int,
) -> dict[str, Any]:
    """Build the compact record that the journal auditor can independently replay."""

    top = [candidate_identity(row) for row in ranked.top]
    standby = [candidate_identity(row) for row in ranked.standby]
    owned = {
        "supplier_ids": sorted(int(value) for value in owned_supplier_ids),
        "channel_ids": sorted(int(value) for value in owned_channel_ids),
        "listing_ids": sorted(int(value) for value in owned_listing_ids),
    }
    owned_digest = canonical_sha256(owned)
    top_owned_overlap = _owned_overlap(top, owned)
    standby_owned_overlap = _owned_overlap(standby, owned)

    assignments = {slot.slot_no: slot.assignment_id for slot in bundle.slots}
    ready_slot_nos = [
        slot.slot_no for slot in bundle.slots if slot.slot_state is SlotState.READY
    ]
    cost_input = cost_input_document(
        model=bundle.model,
        ordered_channels=ranked.top,
        assignments=assignments,
        ready_slot_nos=ready_slot_nos,
        token_mix_version=bundle.token_mix_version,
        exchange_rate_version=bundle.exchange_rate_version,
        retry_reserve_window=retry_reserve_window,
        retry_reserve_value=bundle.cost_plan.retry_reserve_multiplier,
        realized_cost_window=realized_cost_window,
        realized_cost_p95_value=bundle.cost_plan.realized_cost_p95_multiplier,
        margin_bps=bundle.cost_plan.margin_bps,
        headroom_bps=bundle.cost_plan.headroom_bps,
        quantization_version=quantization_version,
    )
    cost_output = cost_output_document(bundle.cost_plan)
    previous_floor_text = _decimal_text(previous_billing_floor)
    fallback_text = _decimal_text(conservative_fallback_cost)
    semantic_input = {
        "model": bundle.model,
        "config_version": bundle.config_version,
        "cost_input_hash": bundle.effective_cost_input_hash,
        "previous_billing_floor_multiplier": previous_floor_text,
        "conservative_fallback_cost": fallback_text,
        "quantum": format(bundle.cost_plan.quantum, "f"),
    }
    semantic_input_hash = canonical_sha256(semantic_input)
    raw_p95_text = (
        None
        if raw_realized_cost_p95_multiplier is None
        else _decimal_text(raw_realized_cost_p95_multiplier)
    )
    enough_samples = raw_realized_cost_sample_count >= minimum_realized_cost_samples
    realized_covered = bool(
        enough_samples
        and raw_realized_cost_p95_multiplier is not None
        and bundle.cost_plan.pool_cost_multiplier >= raw_realized_cost_p95_multiplier
    )
    route_plus_retry_covered = bundle.cost_plan.pool_cost_multiplier >= (
        bundle.cost_plan.route_cost_multiplier
        + bundle.cost_plan.retry_reserve_multiplier
    )
    protected_floor_covered = (
        bundle.cost_plan.transition_floor_multiplier
        >= bundle.cost_plan.billing_floor_multiplier
        and bundle.cost_plan.transition_floor_multiplier >= previous_billing_floor
    )

    return {
        "schema": SHADOW_EVIDENCE_SCHEMA,
        "snapshot": {
            "status": snapshot.status,
            "pages_complete": snapshot.pages_complete,
            "candidate_count": len(snapshot.candidates),
            "result_hash": snapshot.result_hash.casefold(),
            "result_hash_verified": True,
            "source_hash": str(source_hash).casefold(),
        },
        "selection": {
            "top": top,
            "standby": standby,
            "top_identity_digest": identity_digest("top", top),
            "standby_identity_digest": identity_digest("standby", standby),
            "owned": {**owned, "identity_digest": owned_digest},
            "top_owned_overlap_count": top_owned_overlap,
            "standby_owned_overlap_count": standby_owned_overlap,
        },
        "cost": {
            "input": cost_input,
            "input_hash": bundle.effective_cost_input_hash,
            "output": cost_output,
            "output_hash": bundle.cost_plan.cost_output_hash,
            "coverage": {
                "minimum_realized_cost_samples": minimum_realized_cost_samples,
                "realized_cost_sample_count": raw_realized_cost_sample_count,
                "observed_realized_cost_p95_multiplier": raw_p95_text,
                "applied_realized_cost_p95_multiplier": (
                    None
                    if bundle.cost_plan.realized_cost_p95_multiplier is None
                    else _decimal_text(bundle.cost_plan.realized_cost_p95_multiplier)
                ),
                "actual_cost_coverage_state": (
                    "covered" if realized_covered else "insufficient_or_uncovered"
                ),
                "route_plus_retry_covered": route_plus_retry_covered,
                "realized_cost_covered": realized_covered,
                "protected_floor_covered": protected_floor_covered,
            },
        },
        "semantic_stability": {
            "input": semantic_input,
            "input_hash": semantic_input_hash,
            "output_hash": bundle.cost_plan.cost_output_hash,
        },
    }


def _owned_overlap(
    identities: Sequence[Mapping[str, int | None]],
    owned: Mapping[str, Sequence[int]],
) -> int:
    suppliers = set(owned["supplier_ids"])
    channels = set(owned["channel_ids"])
    listings = set(owned["listing_ids"])
    return sum(
        identity["channel_id"] in channels
        or identity["supplier_id"] in suppliers
        or identity["listing_id"] in listings
        for identity in identities
    )


def _decimal_text(value: Decimal | str | int) -> str:
    return format(Decimal(str(value)), "f")
