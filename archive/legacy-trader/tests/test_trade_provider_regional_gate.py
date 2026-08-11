from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from a6api_auto.trade_provider_regional_gate import (
    ProviderRegionalGateError,
    main,
    parse_redis_hgetall,
    verify_regional_snapshot,
)

HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64
EPOCH = "e" * 40


def snapshot() -> dict[str, object]:
    projections = [
        {
            "region": "hk",
            "model": "gpt-5.6-sol",
            "projection_type": "dispatch",
            "routing_generation": 42,
            "cost_generation": 19,
            "source_hash": HASH_A,
            "projection_digest": HASH_B,
            "last_event_id": 0,
            "subscriber_cursor": 0,
            "subscriber_connected": True,
            "redis_restart_epoch": EPOCH,
            "projection_state": "ready",
            "last_error_class": None,
        },
        {
            "region": "hk",
            "model": "gpt-5.6-sol",
            "projection_type": "fast_block",
            "routing_generation": 42,
            "cost_generation": 19,
            "source_hash": HASH_A,
            "projection_digest": HASH_C,
            "last_event_id": 17,
            "subscriber_cursor": 17,
            "subscriber_connected": True,
            "redis_restart_epoch": EPOCH,
            "projection_state": "ready",
            "last_error_class": None,
        },
    ]
    projection_metadata = {
        "dispatch": {
            "schema_version": 1,
            "projection_type": "dispatch",
            "projection_state": "ready",
            "region": "hk",
            "model": "gpt-5.6-sol",
            "state": "active",
            "routing_generation": 42,
            "cost_generation": 19,
            "source_hash": HASH_A,
            "projection_digest": HASH_B,
            "redis_restart_epoch": EPOCH,
        },
        "fast_block": {
            "schema_version": 1,
            "projection_type": "fast_block",
            "projection_state": "ready",
            "region": "hk",
            "model": "gpt-5.6-sol",
            "routing_generation": 42,
            "cost_generation": 19,
            "source_hash": HASH_A,
            "projection_digest": HASH_C,
            "last_event_id": 17,
            "redis_restart_epoch": EPOCH,
        },
    }
    capacities = [
        {
            "enodot_account_id": 51,
            "region": "hk",
            "allocation_version": 3,
            "quota": 2,
            "account_concurrency": 10,
            "allocation_state": "active",
            "receipt_allocation_version": 3,
            "receipt_quota": 2,
            "receipt_occupancy": 1,
            "receipt_occupancy_digest": HASH_B,
            "receipt_lease_cursor": 8,
            "receipt_redis_restart_epoch": EPOCH,
            "receipt_state": "ready",
            "receipt_version": 4,
            "receipt_last_error_class": None,
        }
    ]
    capacity_metadata = {
        "51": {
            "account_id": "51",
            "region": "hk",
            "allocation_version": "3",
            "quota": "2",
            "restart_epoch": EPOCH,
            "reconciled_restart_epoch": EPOCH,
            "allocation_state": "active",
            "state": "ready",
            "lease_cursor": "8",
            "occupancy": "1",
            "occupancy_digest": HASH_B,
            "receipt_version": "4",
            "receipt_dirty": "0",
        }
    }
    return {
        "region": "hk",
        "model": "gpt-5.6-sol",
        "redis_restart_epoch": EPOCH,
        "projection_receipts": projections,
        "projection_metadata": projection_metadata,
        "capacity_rows": capacities,
        "capacity_metadata": capacity_metadata,
        "capacity_overallocated_accounts": 0,
    }


def verify(document: dict[str, object]) -> dict[str, object]:
    return verify_regional_snapshot(**document)


def test_exact_regional_snapshot_matches_every_receipt_and_metadata_field() -> None:
    result = verify(snapshot())

    assert result == {
        "success": True,
        "region": "hk",
        "model": "gpt-5.6-sol",
        "redis_restart_epoch": EPOCH,
        "projection_receipts": 2,
        "capacity_receipts": 1,
        "capacity_quota": 2,
    }


@pytest.mark.parametrize(
    ("path", "value", "error"),
    (
        (
            ("projection_metadata", "dispatch", "projection_digest"),
            HASH_C,
            "provider_regional_gate_projection_metadata_mismatch",
        ),
        (
            ("projection_metadata", "fast_block", "last_event_id"),
            16,
            "provider_regional_gate_projection_metadata_mismatch",
        ),
        (
            ("projection_receipts", 1, "routing_generation"),
            41,
            "provider_regional_gate_projection_generation_mismatch",
        ),
        (
            ("projection_receipts", 1, "subscriber_cursor"),
            16,
            "provider_regional_gate_projection_not_ready",
        ),
        (
            ("capacity_metadata", "51", "occupancy_digest"),
            HASH_C,
            "provider_regional_gate_capacity_metadata_mismatch",
        ),
        (
            ("capacity_metadata", "51", "receipt_dirty"),
            "1",
            "provider_regional_gate_capacity_metadata_mismatch",
        ),
        (
            ("capacity_rows", 0, "receipt_allocation_version"),
            2,
            "provider_regional_gate_capacity_not_ready",
        ),
        (
            ("capacity_rows", 0, "receipt_occupancy"),
            3,
            "provider_regional_gate_capacity_not_ready",
        ),
        (
            ("capacity_overallocated_accounts",),
            1,
            "provider_regional_gate_capacity_overallocated",
        ),
    ),
)
def test_exact_regional_snapshot_fails_closed_on_cross_store_drift(
    path: tuple[object, ...],
    value: object,
    error: str,
) -> None:
    document = copy.deepcopy(snapshot())
    target: object = document
    for component in path[:-1]:
        target = target[component]  # type: ignore[index]
    target[path[-1]] = value  # type: ignore[index]

    with pytest.raises(ProviderRegionalGateError, match=error):
        verify(document)


def test_redis_hgetall_parser_rejects_odd_or_duplicate_fields() -> None:
    assert parse_redis_hgetall("state\nready\nquota\n2\n") == {
        "state": "ready",
        "quota": "2",
    }
    with pytest.raises(ProviderRegionalGateError, match="redis_hash_invalid"):
        parse_redis_hgetall("state\n")
    with pytest.raises(ProviderRegionalGateError, match="redis_hash_invalid"):
        parse_redis_hgetall("state\nready\nstate\nstale\n")


def test_cli_reads_captured_files_and_emits_secret_free_summary(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    document = snapshot()
    projections = tmp_path / "projections.json"
    dispatch = tmp_path / "dispatch.json"
    fast_block = tmp_path / "fast-block.json"
    capacities = tmp_path / "capacities.json"
    projections.write_text(json.dumps(document["projection_receipts"]), encoding="utf-8")
    projection_metadata = document["projection_metadata"]
    dispatch.write_text(json.dumps(projection_metadata["dispatch"]), encoding="utf-8")
    fast_block.write_text(json.dumps(projection_metadata["fast_block"]), encoding="utf-8")
    capacities.write_text(json.dumps(document["capacity_rows"]), encoding="utf-8")
    capacity = document["capacity_metadata"]["51"]
    (tmp_path / "capacity-51.hgetall").write_text(
        "".join(f"{key}\n{value}\n" for key, value in capacity.items()),
        encoding="utf-8",
    )

    assert (
        main(
            (
                "--region",
                "hk",
                "--model",
                "gpt-5.6-sol",
                "--redis-restart-epoch",
                EPOCH,
                "--projection-receipts-file",
                str(projections),
                "--dispatch-meta-file",
                str(dispatch),
                "--fast-block-meta-file",
                str(fast_block),
                "--capacity-rows-file",
                str(capacities),
                "--capacity-metadata-dir",
                str(tmp_path),
                "--capacity-overallocated-accounts",
                "0",
            )
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["success"] is True
