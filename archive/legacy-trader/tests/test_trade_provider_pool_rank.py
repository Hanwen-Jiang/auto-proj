from __future__ import annotations

import hashlib
import json
import random
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from a6api_auto.trade_provider_pool_rank import (
    Candidate,
    ObserverSnapshot,
    ProviderPoolRankError,
    accept_snapshot,
    cost_input_document,
    cost_input_hash,
    rank_candidates,
    snapshot_hash,
)

NOW = datetime(2026, 7, 28, 12, 0, tzinfo=timezone.utc)


def candidate(channel_id: int, price: str, **kwargs) -> Candidate:
    return Candidate(
        channel_id=channel_id,
        model="gpt-5.6-sol",
        final_multiplier=Decimal(price),
        **kwargs,
    )


def test_rank_uses_price_then_success_latency_and_channel_id() -> None:
    rows = [
        candidate(
            30,
            "1.00",
            last_success_at=NOW - timedelta(seconds=10),
            latency_ewma_ms=Decimal("10"),
        ),
        candidate(
            20,
            "1.00",
            last_success_at=NOW - timedelta(seconds=1),
            latency_ewma_ms=Decimal("100"),
        ),
        candidate(
            10,
            "0.90",
            last_success_at=NOW - timedelta(days=1),
            latency_ewma_ms=Decimal("999"),
        ),
    ]

    ranked = rank_candidates(rows, model="gpt-5.6-sol", now=NOW, n=3)

    assert [item.channel_id for item in ranked.top] == [10, 20, 30]


def test_rank_excludes_owned_quarantined_and_unhealthy_rows() -> None:
    rows = [
        candidate(1, "0.1"),
        candidate(2, "0.2"),
        candidate(3, "0.3", health_state="cooling"),
        candidate(4, "0.4", available=False),
        candidate(5, "0.5", health_state="degraded"),
        candidate(6, "0.6", health_state="unlabeled"),
        candidate(7, "0.7", health_state="unknown"),
    ]

    ranked = rank_candidates(
        rows,
        model="gpt-5.6-sol",
        owned_ids={1},
        quarantines={2: NOW + timedelta(minutes=1)},
        now=NOW,
        n=3,
    )

    assert ranked.top == ()
    assert dict(ranked.excluded) == {
        1: "owned",
        2: "quarantined",
        3: "unavailable",
        4: "unavailable",
        5: "health_not_ready",
        6: "health_not_ready",
        7: "health_not_ready",
    }


def test_rank_keeps_resource_id_namespaces_separate() -> None:
    rows = [
        candidate(101, "0.1", supplier_id=202, listing_id=303),
        candidate(401, "0.2", supplier_id=101, listing_id=501),
        candidate(303, "0.3", supplier_id=402, listing_id=502),
        candidate(403, "0.4", supplier_id=503, listing_id=202),
    ]

    ranked = rank_candidates(
        rows,
        model="gpt-5.6-sol",
        owned_supplier_ids={101},
        owned_channel_ids={303},
        owned_listing_ids={202},
        now=NOW,
        n=4,
    )

    assert [item.channel_id for item in ranked.top] == [101]
    assert dict(ranked.excluded) == {
        303: "owned",
        401: "owned",
        403: "owned",
    }


def test_snapshot_hash_and_acceptance_reject_stale_or_tampered_input() -> None:
    snapshot = ObserverSnapshot(
        model="gpt-5.6-sol",
        observed_at=NOW,
        status="completed",
        candidates=(candidate(7, "0.5", observer_health_count=2, observer_count=2),),
        result_hash="0" * 64,
        config_version="v1",
        health_observer_count=2,
    )
    snapshot = replace(snapshot, result_hash=snapshot_hash(snapshot))

    assert (
        accept_snapshot(
            snapshot,
            now=NOW + timedelta(seconds=30),
            expected_model="gpt-5.6-sol",
            expected_config_version="v1",
        )
        == snapshot
    )

    with pytest.raises(ProviderPoolRankError, match="provider_snapshot_stale"):
        accept_snapshot(snapshot, now=NOW + timedelta(seconds=121))
    with pytest.raises(ProviderPoolRankError, match="provider_snapshot_hash_mismatch"):
        accept_snapshot(replace(snapshot, candidates=()), now=NOW)


def test_snapshot_requires_quorum_and_monotonic_observed_at() -> None:
    snapshot = ObserverSnapshot(
        model="gpt-5.6-sol",
        observed_at=NOW,
        status="completed",
        candidates=(candidate(7, "0.5"),),
        result_hash="0" * 64,
        health_observer_count=1,
    )
    snapshot = replace(snapshot, result_hash=snapshot_hash(snapshot))
    with pytest.raises(ProviderPoolRankError, match="observer_quorum"):
        accept_snapshot(snapshot, now=NOW, minimum_health_observers=2)
    with pytest.raises(ProviderPoolRankError, match="out_of_order"):
        accept_snapshot(snapshot, now=NOW, minimum_health_observers=1, current_observed_at=NOW)


def test_cost_input_document_is_the_exact_hashed_semantic_payload() -> None:
    rows = (candidate(7, "0.5"),)
    arguments = {
        "model": "gpt-5.6-sol",
        "ordered_channels": rows,
        "assignments": {1: 1001},
        "ready_slot_nos": [1],
        "token_mix_version": "mix-v1",
        "exchange_rate_version": "fx-v1",
        "retry_reserve_window": "24h",
        "retry_reserve_value": Decimal("0.2"),
        "realized_cost_window": "24h",
        "realized_cost_p95_value": Decimal("0.7"),
        "margin_bps": 1000,
        "headroom_bps": 500,
        "quantization_version": "ceil-v1",
    }

    document = cost_input_document(**arguments)

    expected = hashlib.sha256(
        json.dumps(
            document,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    assert cost_input_hash(**arguments) == expected


def test_rank_randomized_permutation_and_owned_resource_properties() -> None:
    rng = random.Random(20260728)
    for _case in range(500):
        rows: list[Candidate] = []
        for channel_id in range(1, rng.randint(4, 24)):
            success_age = rng.choice((None, rng.randint(0, 86_400)))
            latency = rng.choice((None, rng.randint(1, 2_000)))
            rows.append(
                candidate(
                    channel_id,
                    str(Decimal(rng.randint(1, 50)) / Decimal(10)),
                    supplier_id=10_000 + channel_id,
                    listing_id=20_000 + channel_id,
                    last_success_at=(
                        None if success_age is None else NOW - timedelta(seconds=success_age)
                    ),
                    latency_ewma_ms=(None if latency is None else Decimal(latency)),
                )
            )

        owned_channels = {row.channel_id for row in rows if rng.randrange(5) == 0}
        owned_suppliers = {row.supplier_id for row in rows if rng.randrange(7) == 0}
        owned_listings = {row.listing_id for row in rows if rng.randrange(11) == 0}
        shuffled = list(rows)
        rng.shuffle(shuffled)
        ranked = rank_candidates(
            shuffled,
            model="gpt-5.6-sol",
            owned_channel_ids=owned_channels,
            owned_supplier_ids=owned_suppliers,
            owned_listing_ids=owned_listings,
            now=NOW,
            n=len(rows),
        )

        expected = sorted(
            (
                row
                for row in rows
                if row.channel_id not in owned_channels
                and row.supplier_id not in owned_suppliers
                and row.listing_id not in owned_listings
            ),
            key=lambda row: (
                row.final_multiplier,
                row.last_success_at is None,
                -(row.last_success_at.timestamp() if row.last_success_at is not None else 0),
                row.latency_ewma_ms is None,
                row.latency_ewma_ms or Decimal(0),
                row.channel_id,
            ),
        )
        assert [row.channel_id for row in ranked.top] == [row.channel_id for row in expected]
        assert all(
            row.channel_id not in owned_channels
            and row.supplier_id not in owned_suppliers
            and row.listing_id not in owned_listings
            for row in ranked.top
        )
