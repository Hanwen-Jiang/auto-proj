#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
from typing import Any

import psycopg
from psycopg.rows import dict_row

from a6api_auto.trade_provider_pool_retention import (
    PostgresProviderGenerationRetention,
    ProviderGenerationRetentionConfig,
)


def run(
    model: str,
    *,
    apply: bool,
    retention_days: int = 180,
    batch_size: int = 100,
    connection_factory: Any,
) -> dict[str, Any]:
    config = ProviderGenerationRetentionConfig(
        model=model,
        retention_days=retention_days,
        batch_size=batch_size,
    )
    return PostgresProviderGenerationRetention(connection_factory).cleanup(
        config,
        apply=apply,
    ).summary()


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(
        description="Archive and remove expired superseded A6 Provider generation pairs"
    )
    command.add_argument("--model", required=True)
    command.add_argument(
        "--apply",
        action="store_true",
        help="archive and delete eligible pairs; the default only previews them",
    )
    command.add_argument("--retention-days", type=int, default=180)
    command.add_argument("--batch-size", type=int, default=100)
    command.add_argument("--database-dsn-env", default="A6_TRADE_DATABASE_DSN")
    return command


def main() -> None:
    args = parser().parse_args()
    dsn = os.environ.get(args.database_dsn_env, "").strip()
    if not dsn:
        raise RuntimeError("provider_generation_retention_database_dsn_missing")

    def connection_factory() -> Any:
        return psycopg.connect(dsn, row_factory=dict_row)

    result = run(
        args.model,
        apply=args.apply,
        retention_days=args.retention_days,
        batch_size=args.batch_size,
        connection_factory=connection_factory,
    )
    print(json.dumps(result, ensure_ascii=True, sort_keys=True))


if __name__ == "__main__":
    main()
