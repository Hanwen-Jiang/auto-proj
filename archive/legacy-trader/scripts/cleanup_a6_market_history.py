#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from typing import Any

import psycopg
from psycopg.rows import dict_row

from a6api_auto.trade_market_history_retention import (
    MarketHistoryRetentionConfig,
    PostgresMarketHistoryRetention,
    S3MarketHistoryObjectStore,
)


def run(
    *,
    apply: bool,
    retention_hours: int = 72,
    batch_size: int = 500,
    max_batches: int = 4,
    connection_factory: Any,
    now: datetime | None = None,
    archive_store: Any = None,
    exporter: Any = None,
    staging_directory: str = "/www/a6-market-archive-staging",
    object_prefix: str = "a6-market-history",
) -> dict[str, Any]:
    if (
        isinstance(max_batches, bool)
        or not isinstance(max_batches, int)
        or not 1 <= max_batches <= 1_000
    ):
        raise ValueError("market_history_retention_max_batches_invalid")
    config = MarketHistoryRetentionConfig(
        retention_hours=retention_hours,
        batch_size=batch_size,
    )
    observed_now = now or datetime.now(timezone.utc)
    cleaner = PostgresMarketHistoryRetention(
        connection_factory,
        archive_store=archive_store,
        exporter=exporter,
        staging_directory=staging_directory,
        object_prefix=object_prefix,
    )
    results = []
    for _ in range(max_batches if apply else 1):
        result = cleaner.cleanup(config, apply=apply, now=observed_now)
        results.append(result)
        if not result.candidates or not result.has_more:
            break
    return {
        "success": True,
        "mode": "apply" if apply else "dry-run",
        "retention_hours": retention_hours,
        "batch_size": batch_size,
        "max_batches": max_batches,
        "batches_run": len(results),
        "cutoff": results[0].cutoff.isoformat(),
        "eligible_scan_runs": sum(len(item.candidates) for item in results),
        "eligible_quotes": sum(item.eligible_quote_count for item in results),
        "deleted_scan_runs": sum(item.deleted_scan_runs for item in results),
        "deleted_quotes": sum(item.deleted_quotes for item in results),
        "deleted_payload_bytes": sum(item.deleted_payload_bytes for item in results),
        "archived_bytes": sum(item.archived_bytes for item in results),
        "archive_prefixes": [item.archive_prefix for item in results if item.archive_prefix],
        "has_more": bool(results[-1].has_more and results[-1].candidates),
        "batches": [item.summary() for item in results],
    }


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(
        description="Archive expired A6 market scans and quotes before deleting them"
    )
    command.add_argument(
        "--apply",
        action="store_true",
        help="archive, verify, and delete eligible history; the default is read-only",
    )
    command.add_argument("--retention-hours", type=int, default=72)
    command.add_argument("--batch-size", type=int, default=500)
    command.add_argument("--max-batches", type=int, default=4)
    command.add_argument("--database-dsn-env", default="A6_TRADE_DATABASE_DSN")
    command.add_argument("--archive-endpoint-env", default="A6_MARKET_ARCHIVE_ENDPOINT")
    command.add_argument("--archive-bucket-env", default="A6_MARKET_ARCHIVE_BUCKET")
    command.add_argument(
        "--archive-access-key-env",
        default="A6_MARKET_ARCHIVE_ACCESS_KEY_ID",
    )
    command.add_argument(
        "--archive-secret-key-env",
        default="A6_MARKET_ARCHIVE_SECRET_ACCESS_KEY",
    )
    command.add_argument("--archive-region-env", default="A6_MARKET_ARCHIVE_REGION")
    command.add_argument("--archive-prefix", default="a6-market-history")
    command.add_argument(
        "--staging-directory",
        default="/www/a6-market-archive-staging",
    )
    return command


def main() -> None:
    args = parser().parse_args()
    dsn = os.environ.get(args.database_dsn_env, "").strip()
    if not dsn:
        raise RuntimeError("market_history_retention_database_dsn_missing")

    def connection_factory() -> Any:
        return psycopg.connect(dsn, row_factory=dict_row)

    archive_store = None
    if args.apply:
        archive_store = S3MarketHistoryObjectStore(
            endpoint_url=os.environ.get(args.archive_endpoint_env, ""),
            bucket=os.environ.get(args.archive_bucket_env, ""),
            access_key_id=os.environ.get(args.archive_access_key_env, ""),
            secret_access_key=os.environ.get(args.archive_secret_key_env, ""),
            region=os.environ.get(args.archive_region_env, "auto"),
        )

    result = run(
        apply=args.apply,
        retention_hours=args.retention_hours,
        batch_size=args.batch_size,
        max_batches=args.max_batches,
        connection_factory=connection_factory,
        archive_store=archive_store,
        staging_directory=args.staging_directory,
        object_prefix=args.archive_prefix,
    )
    print(json.dumps(result, ensure_ascii=True, sort_keys=True))


if __name__ == "__main__":
    main()
