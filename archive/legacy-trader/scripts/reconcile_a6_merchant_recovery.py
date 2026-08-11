#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row

from a6api_auto.trade_merchant_recovery import (
    A6ProxyVerifier,
    MerchantRecoveryController,
    MerchantRecoveryError,
    PostgresMerchantRecoveryStore,
)


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(
        description="Reconcile bounded A6 merchant session, proxy, and retirement recovery"
    )
    command.add_argument(
        "--database-dsn-env",
        default="A6_TRADE_DATABASE_DSN",
    )
    command.add_argument("--sessions-dir", type=Path, required=True)
    command.add_argument("--proxy-map-file", type=Path, required=True)
    command.add_argument(
        "--proxy-pool-file",
        type=Path,
        action="append",
        required=True,
    )
    command.add_argument(
        "--proxy-reservation-file",
        type=Path,
        action="append",
        default=[],
    )
    command.add_argument("--collector-failure-threshold", type=int, default=3)
    command.add_argument("--stale-seconds", type=int, default=1800)
    command.add_argument("--repair-cooldown-seconds", type=int, default=3600)
    command.add_argument("--candidate-limit", type=int, default=16)
    command.add_argument("--candidate-successes", type=int, default=2)
    command.add_argument("--candidate-timeout-seconds", type=float, default=6)
    command.add_argument("--max-proxy-repairs", type=int, default=1)
    command.add_argument("--archive-minimum-age-seconds", type=int, default=1800)
    command.add_argument("--archive-minimum-errors", type=int, default=3)
    command.add_argument("--max-archives", type=int, default=10)
    command.add_argument("--apply", action="store_true")
    return command


def execute(
    args: argparse.Namespace,
    *,
    environ: Mapping[str, str] | None = None,
    connector: Callable[..., Any] = psycopg.connect,
    verifier: Any | None = None,
) -> dict[str, Any]:
    environment = os.environ if environ is None else environ
    enabled = str(environment.get("A6_MERCHANT_RECOVERY_ENABLED") or "").strip().lower()
    if enabled not in {"1", "true", "yes", "on"}:
        return {
            "success": True,
            "mode": "disabled",
            "applied": False,
            "archive_candidate_count": 0,
            "proxy_target_count": 0,
        }
    dsn = str(environment.get(args.database_dsn_env) or "").strip()
    if not dsn:
        raise MerchantRecoveryError("merchant_recovery_database_dsn_missing")
    connection = connector(dsn, row_factory=dict_row)
    try:
        controller = MerchantRecoveryController(
            PostgresMerchantRecoveryStore(connection),
            sessions_dir=args.sessions_dir,
            proxy_map_file=args.proxy_map_file,
            proxy_pool_files=tuple(args.proxy_pool_file),
            reservation_files=tuple(args.proxy_reservation_file),
            verifier=verifier
            or A6ProxyVerifier(
                required_successes=args.candidate_successes,
                timeout_seconds=args.candidate_timeout_seconds,
            ),
        )
        return controller.run_once(
            apply=bool(args.apply),
            failure_threshold=args.collector_failure_threshold,
            stale_seconds=args.stale_seconds,
            repair_cooldown_seconds=args.repair_cooldown_seconds,
            candidate_limit=args.candidate_limit,
            max_proxy_repairs=args.max_proxy_repairs,
            archive_minimum_age_seconds=args.archive_minimum_age_seconds,
            archive_minimum_errors=args.archive_minimum_errors,
            max_archives=args.max_archives,
        )
    finally:
        connection.close()


def main(argv: Sequence[str] | None = None) -> int:
    try:
        result = execute(parser().parse_args(argv))
    except MerchantRecoveryError as exc:
        result = {"success": False, "error": str(exc)}
    except Exception:
        result = {"success": False, "error": "merchant_recovery_internal_error"}
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0 if result.get("success") is not False else 2


if __name__ == "__main__":
    raise SystemExit(main())
