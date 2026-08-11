#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import sys
from pathlib import Path
from threading import Event
from typing import Any

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

from a6api_auto.trade_provider_membership_repair import (
    PostgresProviderMembershipRepairStore,
    ProviderMembershipRepairConsumer,
)
from a6api_auto.trade_publication_control import AtomicPublicationTrigger


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(
        description="Consume durable Provider membership vacancy repairs"
    )
    command.add_argument("--database-dsn-env", default="A6_TRADE_DATABASE_DSN")
    command.add_argument(
        "--notification-channel",
        default="a6_provider_membership_repair",
    )
    command.add_argument("--poll-seconds", type=float, default=15.0)
    command.add_argument("--drain-limit", type=int, default=100)
    command.add_argument("--stale-claim-seconds", type=int, default=120)
    command.add_argument(
        "--membership-apply-trigger-file",
        type=Path,
        default=Path("/run/a6-trader/provider-membership-apply-request"),
    )
    command.add_argument(
        "--auto-pool-trigger-file",
        type=Path,
        default=Path("/run/a6-trader/provider-auto-pool-request"),
    )
    return command


def build_consumer(args: argparse.Namespace) -> ProviderMembershipRepairConsumer:
    dsn = str(os.environ.get(args.database_dsn_env, "")).strip()
    if not dsn:
        raise ValueError("provider_membership_repair_database_dsn_missing")

    def connection_factory() -> Any:
        return psycopg.connect(dsn, row_factory=dict_row, connect_timeout=5)

    return ProviderMembershipRepairConsumer(
        PostgresProviderMembershipRepairStore(
            connection_factory,
            stale_claim_seconds=args.stale_claim_seconds,
        ),
        worker_id=f"membership-repair:{socket.gethostname()}:{os.getpid()}",
        membership_apply_trigger=AtomicPublicationTrigger(
            args.membership_apply_trigger_file
        ),
        auto_pool_trigger=AtomicPublicationTrigger(args.auto_pool_trigger_file),
    )


def run_listener(args: argparse.Namespace, stopped: Event) -> None:
    if not 0.1 <= args.poll_seconds <= 300:
        raise ValueError("provider_membership_repair_poll_seconds_invalid")
    if not 1 <= args.drain_limit <= 1000:
        raise ValueError("provider_membership_repair_drain_limit_invalid")
    dsn = str(os.environ.get(args.database_dsn_env, "")).strip()
    if not dsn:
        raise ValueError("provider_membership_repair_database_dsn_missing")
    consumer = build_consumer(args)
    reconnect_seconds = 1.0
    while not stopped.is_set():
        connection = None
        try:
            connection = psycopg.connect(dsn, autocommit=True, connect_timeout=5)
            with connection.cursor() as cursor:
                cursor.execute(
                    sql.SQL("LISTEN {}").format(sql.Identifier(args.notification_channel))
                )
            reconnect_seconds = 1.0
            _drain(consumer, limit=args.drain_limit)
            while not stopped.is_set():
                notification = next(
                    iter(
                        connection.notifies(
                            timeout=args.poll_seconds,
                            stop_after=1,
                        )
                    ),
                    None,
                )
                result = _drain(consumer, limit=args.drain_limit)
                if notification is not None or result["processed"]:
                    _emit(
                        {
                            "event": "provider_membership_repair_drain",
                            "notified": notification is not None,
                            **result,
                        }
                    )
        except Exception as exc:
            _emit(
                {
                    "event": "provider_membership_repair_listener_error",
                    "error_class": _error_code(exc),
                    "retry_seconds": reconnect_seconds,
                },
                stream=sys.stderr,
            )
            stopped.wait(reconnect_seconds)
            reconnect_seconds = min(reconnect_seconds * 2, 30.0)
        finally:
            if connection is not None:
                connection.close()


def _drain(
    consumer: ProviderMembershipRepairConsumer,
    *,
    limit: int,
) -> dict[str, int]:
    return consumer.drain(limit=limit)


def _emit(document: dict[str, Any], *, stream: Any = sys.stdout) -> None:
    print(
        json.dumps(document, ensure_ascii=True, sort_keys=True, separators=(",", ":")),
        file=stream,
        flush=True,
    )


def _error_code(exc: Exception) -> str:
    return str(getattr(exc, "code", "") or type(exc).__name__).strip().casefold()[:160]


def main() -> int:
    args = parser().parse_args()
    stopped = Event()

    def stop(_signum: int, _frame: Any) -> None:
        stopped.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    run_listener(args, stopped)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
