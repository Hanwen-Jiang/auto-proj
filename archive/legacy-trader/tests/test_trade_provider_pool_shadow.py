from __future__ import annotations

from copy import deepcopy

import pytest

from a6api_auto.trade_provider_cost import PoolDiffKind
from a6api_auto.trade_provider_pool import ProviderPoolError
from a6api_auto.trade_provider_pool_shadow import (
    ProviderPoolShadowError,
    reconcile_shadow_document,
    shadow_input_from_mapping,
)


def shadow_document() -> dict:
    return {
        "model": "gpt-5.6-sol",
        "routing_generation": 2,
        "parent_routing_generation": 1,
        "cost_generation": 3,
        "parent_cost_generation": 2,
        "generation_source": "observer-shadow",
        "source_hash": "a" * 64,
        "cost_input_hash": "b" * 64,
        "ordered_channel_ids": [627],
        "standby_channel_ids": [1008],
        "desired_size": 1,
        "slots": [
            {
                "slot_no": 1,
                "channel_id": 627,
                "rank": 1,
                "priority": 2,
                "assignment_id": "provider-1",
                "enodot_binding_id": 44001,
                "token_id": 14093,
                "account_id": 45896,
                "slot_state": "READY",
                "funding_eligible": True,
                "desired_schedulable": True,
                "observed_schedulable": True,
                "observed_cost_multiplier": "0.8",
                "pin_confirmed": True,
                "probe_confirmed": True,
            }
        ],
        "cost_plan": {
            "desired_slot_costs": {"1": "0.8"},
            "retry_reserve_multiplier": "0.1",
            "bootstrap_retry_reserve": "0.2",
            "realized_cost_p95_multiplier": "0.95",
            "margin_bps": 1000,
            "headroom_bps": 500,
            "previous_billing_floor": "1.0",
        },
        "current": {
            "ordered_channel_ids": [622],
            "assignments": {"1": "provider-1"},
            "cost_input_hash": "c" * 64,
        },
    }


class RecordingStore:
    def __init__(self) -> None:
        self.calls: list[tuple[object, int | None, int | None]] = []

    def plan(
        self,
        bundle,
        *,
        expected_routing_generation,
        expected_cost_generation,
    ):
        self.calls.append(
            (bundle, expected_routing_generation, expected_cost_generation)
        )
        return object()


def test_shadow_document_builds_separate_source_and_cost_hashes() -> None:
    desired = shadow_input_from_mapping(shadow_document())

    assert desired.bundle.source_hash == "a" * 64
    assert desired.bundle.effective_cost_input_hash == "b" * 64
    assert desired.bundle.cost_plan.cost_output_hash not in {"a" * 64, "b" * 64}
    assert desired.bundle.ready_slots[0].desired_cost_multiplier == desired.bundle.ready_slots[
        0
    ].observed_cost_multiplier


def test_shadow_dry_run_has_zero_store_and_remote_writes() -> None:
    report = reconcile_shadow_document(shadow_document())

    assert report.diff_kind is PoolDiffKind.MEMBERSHIP_AND_COST
    assert report.summary() == {
        "success": True,
        "mode": "shadow",
        "model": "gpt-5.6-sol",
        "diff_kind": "MEMBERSHIP_AND_COST",
        "routing_generation": 2,
        "cost_generation": 3,
        "persisted": False,
        "activated": False,
        "remote_write_count": 0,
    }


def test_shadow_persist_calls_only_generation_plan() -> None:
    store = RecordingStore()

    report = reconcile_shadow_document(shadow_document(), store=store, persist=True)

    assert report.persisted is True
    assert report.activated is False
    assert report.remote_write_count == 0
    assert len(store.calls) == 1
    bundle, parent_routing, parent_cost = store.calls[0]
    assert bundle.routing_generation == 2
    assert (parent_routing, parent_cost) == (1, 2)


def test_shadow_classifies_cost_only_against_explicit_current_state() -> None:
    document = shadow_document()
    document["current"] = {
        "ordered_channel_ids": [627],
        "assignments": {"1": "provider-1"},
        "cost_input_hash": "c" * 64,
    }

    report = reconcile_shadow_document(document)

    assert report.diff_kind is PoolDiffKind.COST_ONLY


def test_shadow_rejects_ready_slot_without_observed_confirmation() -> None:
    document = deepcopy(shadow_document())
    document["slots"][0]["probe_confirmed"] = False

    with pytest.raises(ProviderPoolError, match="provider_ready_confirmation_missing"):
        shadow_input_from_mapping(document)


def test_shadow_persist_requires_store() -> None:
    with pytest.raises(ProviderPoolShadowError, match="provider_shadow_store_missing"):
        reconcile_shadow_document(shadow_document(), persist=True)


def test_shadow_noop_never_persists_even_when_requested() -> None:
    document = shadow_document()
    document["current"] = {
        "ordered_channel_ids": [627],
        "assignments": {"1": "provider-1"},
        "cost_input_hash": "b" * 64,
    }
    store = RecordingStore()

    report = reconcile_shadow_document(document, store=store, persist=True)

    assert report.diff_kind is PoolDiffKind.NOOP
    assert report.persisted is False
    assert store.calls == []
