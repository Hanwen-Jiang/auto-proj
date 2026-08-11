"""Fail-closed verification for a captured Provider regional snapshot.

The live verifier captures PostgreSQL receipts and Redis metadata separately.
This module proves that both sides describe the same generation, cursor and
capacity state before the HK shadow can be considered regionally ready.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_MODEL_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


class ProviderRegionalGateError(RuntimeError):
    """Stable, secret-free regional gate error."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def verify_regional_snapshot(
    *,
    region: object,
    model: object,
    redis_restart_epoch: object,
    projection_receipts: object,
    projection_metadata: object,
    capacity_rows: object,
    capacity_metadata: object,
    capacity_overallocated_accounts: object,
) -> dict[str, Any]:
    """Verify one captured DB/Redis snapshot without making either side mutable."""

    normalized_region = _region(region)
    normalized_model = _model(model)
    epoch = _epoch(redis_restart_epoch)
    projection_count = _verify_projection_receipts(
        region=normalized_region,
        model=normalized_model,
        epoch=epoch,
        receipts=projection_receipts,
        metadata=projection_metadata,
    )
    capacity_count, capacity_quota = _verify_capacity_rows(
        region=normalized_region,
        epoch=epoch,
        rows=capacity_rows,
        metadata=capacity_metadata,
        overallocated_accounts=capacity_overallocated_accounts,
    )
    return {
        "success": True,
        "region": normalized_region,
        "model": normalized_model,
        "redis_restart_epoch": epoch,
        "projection_receipts": projection_count,
        "capacity_receipts": capacity_count,
        "capacity_quota": capacity_quota,
    }


def parse_redis_hgetall(raw: str) -> dict[str, str]:
    """Parse ``valkey-cli --raw HGETALL`` output and reject ambiguous data."""

    if not isinstance(raw, str):
        raise ProviderRegionalGateError("provider_regional_gate_redis_hash_invalid")
    values = raw.splitlines()
    if len(values) % 2:
        raise ProviderRegionalGateError("provider_regional_gate_redis_hash_invalid")
    result: dict[str, str] = {}
    for index in range(0, len(values), 2):
        key = values[index]
        if not key or key in result:
            raise ProviderRegionalGateError("provider_regional_gate_redis_hash_invalid")
        result[key] = values[index + 1]
    return result


def _verify_projection_receipts(
    *,
    region: str,
    model: str,
    epoch: str,
    receipts: object,
    metadata: object,
) -> int:
    rows = _sequence(receipts, "provider_regional_gate_projection_receipts_invalid")
    metas = _mapping(metadata, "provider_regional_gate_projection_metadata_invalid")
    by_type: dict[str, Mapping[str, Any]] = {}
    for value in rows:
        row = _mapping(value, "provider_regional_gate_projection_receipt_invalid")
        projection_type = _text(
            row.get("projection_type"),
            "provider_regional_gate_projection_type_invalid",
        )
        if projection_type not in {"dispatch", "fast_block"} or projection_type in by_type:
            raise ProviderRegionalGateError(
                "provider_regional_gate_projection_type_invalid"
            )
        by_type[projection_type] = row
    if set(by_type) != {"dispatch", "fast_block"} or set(metas) != {
        "dispatch",
        "fast_block",
    }:
        raise ProviderRegionalGateError("provider_regional_gate_projection_set_invalid")

    canonical: tuple[int, int, str] | None = None
    for projection_type in ("dispatch", "fast_block"):
        row = by_type[projection_type]
        if row.get("region") != region or row.get("model") != model:
            raise ProviderRegionalGateError(
                "provider_regional_gate_projection_identity_mismatch"
            )
        routing_generation = _nonnegative_int(
            row.get("routing_generation"),
            "provider_regional_gate_projection_generation_invalid",
        )
        cost_generation = _nonnegative_int(
            row.get("cost_generation"),
            "provider_regional_gate_projection_generation_invalid",
        )
        source_hash = _hash(
            row.get("source_hash"),
            "provider_regional_gate_projection_hash_invalid",
        )
        projection_digest = _hash(
            row.get("projection_digest"),
            "provider_regional_gate_projection_hash_invalid",
        )
        last_event_id = _nonnegative_int(
            row.get("last_event_id"),
            "provider_regional_gate_projection_cursor_invalid",
        )
        subscriber_cursor = _nonnegative_int(
            row.get("subscriber_cursor"),
            "provider_regional_gate_projection_cursor_invalid",
        )
        if (
            row.get("projection_state") != "ready"
            or row.get("subscriber_connected") is not True
            or subscriber_cursor != last_event_id
            or row.get("redis_restart_epoch") != epoch
            or row.get("last_error_class") is not None
        ):
            raise ProviderRegionalGateError(
                "provider_regional_gate_projection_not_ready"
            )
        if projection_type == "dispatch" and last_event_id != 0:
            raise ProviderRegionalGateError(
                "provider_regional_gate_projection_cursor_invalid"
            )

        identity = (routing_generation, cost_generation, source_hash)
        if canonical is None:
            canonical = identity
        elif identity != canonical:
            raise ProviderRegionalGateError(
                "provider_regional_gate_projection_generation_mismatch"
            )

        meta = _mapping(
            metas[projection_type],
            "provider_regional_gate_projection_metadata_invalid",
        )
        expected: dict[str, object] = {
            "schema_version": 1,
            "projection_type": projection_type,
            "projection_state": "ready",
            "region": region,
            "model": model,
            "routing_generation": routing_generation,
            "cost_generation": cost_generation,
            "source_hash": source_hash,
            "projection_digest": projection_digest,
            "redis_restart_epoch": epoch,
        }
        if projection_type == "fast_block":
            expected["last_event_id"] = last_event_id
        if any(meta.get(key) != value for key, value in expected.items()):
            raise ProviderRegionalGateError(
                "provider_regional_gate_projection_metadata_mismatch"
            )
    return len(by_type)


def _verify_capacity_rows(
    *,
    region: str,
    epoch: str,
    rows: object,
    metadata: object,
    overallocated_accounts: object,
) -> tuple[int, int]:
    if _nonnegative_int(
        overallocated_accounts,
        "provider_regional_gate_capacity_overallocation_invalid",
    ) != 0:
        raise ProviderRegionalGateError(
            "provider_regional_gate_capacity_overallocated"
        )
    values = _sequence(rows, "provider_regional_gate_capacity_rows_invalid")
    metas = _mapping(metadata, "provider_regional_gate_capacity_metadata_invalid")
    if not values:
        raise ProviderRegionalGateError("provider_regional_gate_capacity_missing")

    seen: set[int] = set()
    total_quota = 0
    for value in values:
        row = _mapping(value, "provider_regional_gate_capacity_row_invalid")
        account_id = _positive_int(
            row.get("enodot_account_id"),
            "provider_regional_gate_capacity_identity_invalid",
        )
        if account_id in seen:
            raise ProviderRegionalGateError(
                "provider_regional_gate_capacity_identity_invalid"
            )
        seen.add(account_id)
        if row.get("region") != region:
            raise ProviderRegionalGateError(
                "provider_regional_gate_capacity_identity_invalid"
            )
        allocation_version = _positive_int(
            row.get("allocation_version"),
            "provider_regional_gate_capacity_version_invalid",
        )
        receipt_allocation_version = _positive_int(
            row.get("receipt_allocation_version"),
            "provider_regional_gate_capacity_version_invalid",
        )
        quota = _nonnegative_int(
            row.get("quota"),
            "provider_regional_gate_capacity_quota_invalid",
        )
        receipt_quota = _nonnegative_int(
            row.get("receipt_quota"),
            "provider_regional_gate_capacity_quota_invalid",
        )
        concurrency = _nonnegative_int(
            row.get("account_concurrency"),
            "provider_regional_gate_capacity_quota_invalid",
        )
        occupancy = _nonnegative_int(
            row.get("receipt_occupancy"),
            "provider_regional_gate_capacity_occupancy_invalid",
        )
        cursor = _nonnegative_int(
            row.get("receipt_lease_cursor"),
            "provider_regional_gate_capacity_cursor_invalid",
        )
        receipt_version = _positive_int(
            row.get("receipt_version"),
            "provider_regional_gate_capacity_version_invalid",
        )
        digest = _hash(
            row.get("receipt_occupancy_digest"),
            "provider_regional_gate_capacity_digest_invalid",
        )
        allocation_state = _text(
            row.get("allocation_state"),
            "provider_regional_gate_capacity_state_invalid",
        )
        if allocation_state not in {"active", "draining", "disabled"}:
            raise ProviderRegionalGateError(
                "provider_regional_gate_capacity_state_invalid"
            )
        if (
            allocation_version != receipt_allocation_version
            or quota != receipt_quota
            or quota > concurrency
            or occupancy > quota
            or (allocation_state == "disabled") != (quota == 0)
            or row.get("receipt_state") != "ready"
            or row.get("receipt_redis_restart_epoch") != epoch
            or row.get("receipt_last_error_class") is not None
        ):
            raise ProviderRegionalGateError(
                "provider_regional_gate_capacity_not_ready"
            )

        meta = _mapping(
            metas.get(str(account_id)),
            "provider_regional_gate_capacity_metadata_invalid",
        )
        final_state = "ready" if allocation_state == "active" else allocation_state
        expected = {
            "account_id": str(account_id),
            "region": region,
            "allocation_version": str(allocation_version),
            "quota": str(quota),
            "restart_epoch": epoch,
            "reconciled_restart_epoch": epoch,
            "allocation_state": allocation_state,
            "state": final_state,
            "lease_cursor": str(cursor),
            "occupancy": str(occupancy),
            "occupancy_digest": digest,
            "receipt_version": str(receipt_version),
            "receipt_dirty": "0",
        }
        if any(meta.get(key) != expected_value for key, expected_value in expected.items()):
            raise ProviderRegionalGateError(
                "provider_regional_gate_capacity_metadata_mismatch"
            )
        total_quota += quota

    if set(metas) != {str(account_id) for account_id in seen} or total_quota <= 0:
        raise ProviderRegionalGateError("provider_regional_gate_capacity_set_invalid")
    return len(seen), total_quota


def _load_json(path: Path, code: str) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ProviderRegionalGateError(code) from None


def _load_capacity_metadata(directory: Path, rows: object) -> dict[str, dict[str, str]]:
    values = _sequence(rows, "provider_regional_gate_capacity_rows_invalid")
    result: dict[str, dict[str, str]] = {}
    for value in values:
        row = _mapping(value, "provider_regional_gate_capacity_row_invalid")
        account_id = _positive_int(
            row.get("enodot_account_id"),
            "provider_regional_gate_capacity_identity_invalid",
        )
        path = directory / f"capacity-{account_id}.hgetall"
        try:
            raw = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            raise ProviderRegionalGateError(
                "provider_regional_gate_capacity_metadata_invalid"
            ) from None
        result[str(account_id)] = parse_redis_hgetall(raw)
    return result


def _mapping(value: object, code: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise ProviderRegionalGateError(code)
    return value


def _sequence(value: object, code: str) -> Sequence[Any]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise ProviderRegionalGateError(code)
    return value


def _text(value: object, code: str) -> str:
    if not isinstance(value, str) or not value:
        raise ProviderRegionalGateError(code)
    return value


def _hash(value: object, code: str) -> str:
    text = _text(value, code)
    if _HASH_RE.fullmatch(text) is None:
        raise ProviderRegionalGateError(code)
    return text


def _nonnegative_int(value: object, code: str) -> int:
    if isinstance(value, bool):
        raise ProviderRegionalGateError(code)
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        raise ProviderRegionalGateError(code) from None
    if parsed < 0 or (isinstance(value, float) and not value.is_integer()):
        raise ProviderRegionalGateError(code)
    return parsed


def _positive_int(value: object, code: str) -> int:
    parsed = _nonnegative_int(value, code)
    if parsed < 1:
        raise ProviderRegionalGateError(code)
    return parsed


def _region(value: object) -> str:
    region = _text(value, "provider_regional_gate_region_invalid").casefold()
    if region not in {"us", "hk"}:
        raise ProviderRegionalGateError("provider_regional_gate_region_invalid")
    return region


def _model(value: object) -> str:
    model = _text(value, "provider_regional_gate_model_invalid")
    if _MODEL_RE.fullmatch(model) is None:
        raise ProviderRegionalGateError("provider_regional_gate_model_invalid")
    return model


def _epoch(value: object) -> str:
    epoch = _text(value, "provider_regional_gate_epoch_invalid")
    if len(epoch) > 128 or any(character.isspace() for character in epoch):
        raise ProviderRegionalGateError("provider_regional_gate_epoch_invalid")
    return epoch


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(
        description="Verify exact PostgreSQL/Redis Provider regional state"
    )
    command.add_argument("--region", required=True)
    command.add_argument("--model", required=True)
    command.add_argument("--redis-restart-epoch", required=True)
    command.add_argument("--projection-receipts-file", type=Path, required=True)
    command.add_argument("--dispatch-meta-file", type=Path, required=True)
    command.add_argument("--fast-block-meta-file", type=Path, required=True)
    command.add_argument("--capacity-rows-file", type=Path, required=True)
    command.add_argument("--capacity-metadata-dir", type=Path, required=True)
    command.add_argument("--capacity-overallocated-accounts", required=True)
    return command


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        receipts = _load_json(
            args.projection_receipts_file,
            "provider_regional_gate_projection_receipts_invalid",
        )
        capacities = _load_json(
            args.capacity_rows_file,
            "provider_regional_gate_capacity_rows_invalid",
        )
        result = verify_regional_snapshot(
            region=args.region,
            model=args.model,
            redis_restart_epoch=args.redis_restart_epoch,
            projection_receipts=receipts,
            projection_metadata={
                "dispatch": _load_json(
                    args.dispatch_meta_file,
                    "provider_regional_gate_projection_metadata_invalid",
                ),
                "fast_block": _load_json(
                    args.fast_block_meta_file,
                    "provider_regional_gate_projection_metadata_invalid",
                ),
            },
            capacity_rows=capacities,
            capacity_metadata=_load_capacity_metadata(
                args.capacity_metadata_dir,
                capacities,
            ),
            capacity_overallocated_accounts=args.capacity_overallocated_accounts,
        )
    except ProviderRegionalGateError as exc:
        print(
            json.dumps({"success": False, "error": exc.code}, sort_keys=True),
            file=sys.stderr,
        )
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
