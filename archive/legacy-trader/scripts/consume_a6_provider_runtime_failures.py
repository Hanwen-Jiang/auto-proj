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

from a6api_auto.trade_provider_fast_block import RedisProviderFastBlockProjector
from a6api_auto.trade_provider_runtime_failure import (
    PostgresProviderRuntimeFailureStore,
    ProviderRuntimeFailureConsumer,
)
from a6api_auto.trade_publication_control import (
    AtomicPublicationTrigger,
    PostgresPublicationControl,
)


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(
        description="Consume committed Sub2API failures into Provider control"
    )
    command.add_argument("--model", default="gpt-5.6-sol")
    command.add_argument("--database-dsn-env", default="A6_TRADE_DATABASE_DSN")
    command.add_argument(
        "--redis-url-env",
        default="PROVIDER_DISPATCH_REDIS_URL",
    )
    command.add_argument(
        "--notification-channel",
        default="a6_provider_runtime_failure",
    )
    command.add_argument("--poll-seconds", type=float, default=15.0)
    command.add_argument("--drain-limit", type=int, default=100)
    command.add_argument("--stale-claim-seconds", type=int, default=60)
    command.add_argument(
        "--publication-trigger-file",
        type=Path,
        default=Path("/run/a6-trader/model-publication-request"),
    )
    command.add_argument(
        "--auto-pool-trigger-file",
        type=Path,
        default=Path("/run/a6-trader/provider-auto-pool-request"),
    )
    command.add_argument(
        "--liquidity-trigger-file",
        type=Path,
        default=Path("/run/a6-trader/provider-liquidity-request"),
    )
    return command


def build_consumer(args: argparse.Namespace) -> ProviderRuntimeFailureConsumer:
    dsn = str(os.environ.get(args.database_dsn_env, "")).strip()
    if not dsn:
        raise ValueError("provider_runtime_failure_database_dsn_missing")

    def connection_factory() -> Any:
        return psycopg.connect(dsn, row_factory=dict_row, connect_timeout=5)

    projector = None
    redis_url = str(os.environ.get(args.redis_url_env, "")).strip()
    if redis_url:
        import redis

        client = redis.Redis.from_url(
            redis_url,
            decode_responses=True,
            socket_connect_timeout=2,
            socket_timeout=2,
            health_check_interval=30,
        )
        projector = RedisProviderFastBlockProjector(client)
    control = PostgresPublicationControl(
        connection_factory,
        trigger=AtomicPublicationTrigger(args.publication_trigger_file),
        fast_block_projector=projector,
        provider_channel_failure_threshold=3,
    )
    store = PostgresProviderRuntimeFailureStore(
        connection_factory,
        stale_claim_seconds=args.stale_claim_seconds,
    )
    worker_id = f"runtime-failure:{socket.gethostname()}:{os.getpid()}"
    return ProviderRuntimeFailureConsumer(
        store,
        control,
        worker_id=worker_id,
        auto_pool_trigger=AtomicPublicationTrigger(args.auto_pool_trigger_file),
        liquidity_trigger=AtomicPublicationTrigger(args.liquidity_trigger_file),
    )


def run_listener(args: argparse.Namespace, stopped: Event) -> None:
    if not 0.1 <= args.poll_seconds <= 300:
        raise ValueError("provider_runtime_failure_poll_seconds_invalid")
    if not 1 <= args.drain_limit <= 1000:
        raise ValueError("provider_runtime_failure_drain_limit_invalid")
    dsn = str(os.environ.get(args.database_dsn_env, "")).strip()
    if not dsn:
        raise ValueError("provider_runtime_failure_database_dsn_missing")
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
                            "event": "provider_runtime_failure_drain",
                            "notified": notification is not None,
                            **result,
                        }
                    )
        except Exception as exc:
            _emit(
                {
                    "event": "provider_runtime_failure_listener_error",
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
    consumer: ProviderRuntimeFailureConsumer,
    *,
    limit: int,
) -> dict[str, Any]:
    return consumer.drain(limit=limit)


def _emit(document: dict[str, Any], *, stream: Any = sys.stdout) -> None:
    print(
        json.dumps(document, ensure_ascii=True, sort_keys=True, separators=(",", ":")),
        file=stream,
        flush=True,
    )


def _error_code(exc: Exception) -> str:
    value = str(getattr(exc, "code", "") or type(exc).__name__)
    return value.strip().casefold().replace("-", "_")[:160]


def main() -> None:
    args = parser().parse_args()
    stopped = Event()

    def stop(_signum: int, _frame: object) -> None:
        stopped.set()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    run_listener(args, stopped)


if __name__ == "__main__":
    main()
