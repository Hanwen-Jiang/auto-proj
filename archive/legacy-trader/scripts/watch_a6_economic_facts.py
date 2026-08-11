#!/usr/bin/env python3
from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from threading import Event
from typing import Any

_MODEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,95}$")
_CHANNEL = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
_TRUE = frozenset({"1", "true", "yes", "on"})
_FALSE = frozenset({"", "0", "false", "no", "off"})


def env_bool(
    name: str,
    *,
    default: bool,
    environ: Mapping[str, str] | None = None,
) -> bool:
    environment = os.environ if environ is None else environ
    if name not in environment:
        return default
    value = str(environment.get(name, "")).strip().casefold()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    raise ValueError(f"economic_listener_env_flag_invalid:{name}")


class EconomicNotificationBatch:
    def __init__(self, allowed_models: Iterable[str]) -> None:
        models = frozenset(_validated_model(model) for model in allowed_models)
        if not models:
            raise ValueError("economic_listener_models_missing")
        self.allowed_models = models
        self._pending: set[str] = set()

    def add(self, payload: object) -> bool:
        if not isinstance(payload, str) or len(payload) > 8_000:
            return False
        try:
            document = json.loads(payload)
        except json.JSONDecodeError:
            return False
        if not isinstance(document, dict):
            return False
        model = str(document.get("model") or "").strip()
        if not _MODEL.fullmatch(model) or model not in self.allowed_models:
            return False
        self._pending.add(model)
        return True

    def pop(self) -> tuple[str, ...]:
        models = tuple(sorted(self._pending))
        self._pending.clear()
        return models


def run_with_lock(
    model: str,
    *,
    lock_path: Path,
    reconcile: Callable[[str], int],
) -> tuple[str, int | None]:
    normalized_model = _validated_model(model)
    descriptor = os.open(
        lock_path,
        os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0),
        0o600,
    )
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return "busy", None
        try:
            returncode = int(reconcile(normalized_model))
            return ("completed" if returncode == 0 else "failed"), returncode
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


def build_reconcile_runner(
    *,
    python: Path,
    reconcile_script: Path,
    timeout_seconds: float,
) -> Callable[[str], int]:
    if timeout_seconds <= 0 or timeout_seconds > 300:
        raise ValueError("economic_listener_reconcile_timeout_invalid")
    command_prefix = (str(python), str(reconcile_script), "--model")

    def run(model: str) -> int:
        try:
            completed = subprocess.run(
                (*command_prefix, _validated_model(model)),
                check=False,
                timeout=timeout_seconds,
            )
        except subprocess.TimeoutExpired:
            return 124
        return int(completed.returncode)

    return run


def watch_notifications(
    *,
    connection_factory: Callable[[], Any],
    listen_statement: Any,
    allowed_models: Iterable[str],
    lock_path: Path,
    reconcile: Callable[[str], int],
    stopped: Event,
    debounce_seconds: float = 0.2,
    receive_timeout_seconds: float = 1.0,
    max_reconnect_seconds: float = 15.0,
    monotonic: Callable[[], float] = time.monotonic,
) -> None:
    if debounce_seconds < 0 or receive_timeout_seconds <= 0 or max_reconnect_seconds <= 0:
        raise ValueError("economic_listener_timing_invalid")
    batch = EconomicNotificationBatch(allowed_models)
    reconnect_seconds = 1.0
    while not stopped.is_set():
        connection = None
        try:
            connection = connection_factory()
            with connection.cursor() as cursor:
                cursor.execute(listen_statement)
            reconnect_seconds = 1.0
            _emit({"event": "economic_listener_connected"})
            while not stopped.is_set():
                notification = _next_notification(connection, receive_timeout_seconds)
                if notification is None:
                    continue
                batch.add(getattr(notification, "payload", None))
                deadline = monotonic() + debounce_seconds
                while not stopped.is_set():
                    remaining = deadline - monotonic()
                    if remaining <= 0:
                        break
                    extra = _next_notification(connection, remaining)
                    if extra is None:
                        break
                    batch.add(getattr(extra, "payload", None))
                for model in batch.pop():
                    state, returncode = run_with_lock(
                        model,
                        lock_path=lock_path,
                        reconcile=reconcile,
                    )
                    _emit(
                        {
                            "event": "economic_listener_reconcile",
                            "model": model,
                            "state": state,
                            "returncode": returncode,
                        }
                    )
        except Exception as exc:
            _emit(
                {
                    "event": "economic_listener_disconnected",
                    "error_class": type(exc).__name__,
                    "retry_seconds": reconnect_seconds,
                },
                stream=sys.stderr,
            )
            stopped.wait(reconnect_seconds)
            reconnect_seconds = min(max_reconnect_seconds, reconnect_seconds * 2)
        finally:
            close = getattr(connection, "close", None)
            if callable(close):
                close()


def _next_notification(connection: Any, timeout_seconds: float) -> Any | None:
    notifications = connection.notifies(timeout=timeout_seconds, stop_after=1)
    return next(iter(notifications), None)


def _validated_model(value: object) -> str:
    model = str(value or "").strip()
    if not _MODEL.fullmatch(model):
        raise ValueError("economic_listener_model_invalid")
    return model


def _emit(document: Mapping[str, object], *, stream: Any = sys.stdout) -> None:
    print(
        json.dumps(dict(document), ensure_ascii=True, sort_keys=True, separators=(",", ":")),
        file=stream,
        flush=True,
    )


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(
        description="Wake the Priority 1 economic reconciler after committed fact changes"
    )
    command.add_argument("--model", action="append", required=True)
    command.add_argument("--database-dsn-env", default="A6_TRADE_DATABASE_DSN")
    command.add_argument("--reconcile-enabled-env", default="ECONOMIC_PRIORITY1_RECONCILE_ENABLED")
    command.add_argument("--notification-channel", default="a6_economic_fact_changed")
    command.add_argument(
        "--lock-file",
        type=Path,
        default=Path("/var/lib/a6-trader/economic-priority1.lock"),
    )
    command.add_argument(
        "--reconcile-script",
        type=Path,
        default=Path(__file__).with_name("reconcile_a6_economic_sources.py"),
    )
    command.add_argument("--python", type=Path, default=Path(sys.executable))
    command.add_argument("--debounce-seconds", type=float, default=0.2)
    command.add_argument("--reconcile-timeout-seconds", type=float, default=12.0)
    return command


def main() -> None:
    args = parser().parse_args()
    models = tuple(_validated_model(model) for model in args.model)
    if not _CHANNEL.fullmatch(args.notification_channel):
        raise ValueError("economic_listener_channel_invalid")
    if not env_bool(args.reconcile_enabled_env, default=False):
        _emit({"event": "economic_listener_disabled", "models": models})
        return
    dsn = str(os.environ.get(args.database_dsn_env, "")).strip()
    if not dsn:
        raise ValueError("economic_listener_database_dsn_missing")

    import psycopg
    from psycopg import sql

    stopped = Event()

    def stop(_signum: int, _frame: object) -> None:
        stopped.set()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    def connection_factory() -> Any:
        return psycopg.connect(dsn, autocommit=True, connect_timeout=5)

    watch_notifications(
        connection_factory=connection_factory,
        listen_statement=sql.SQL("LISTEN {}").format(sql.Identifier(args.notification_channel)),
        allowed_models=models,
        lock_path=args.lock_file,
        reconcile=build_reconcile_runner(
            python=args.python,
            reconcile_script=args.reconcile_script,
            timeout_seconds=args.reconcile_timeout_seconds,
        ),
        stopped=stopped,
        debounce_seconds=args.debounce_seconds,
    )


if __name__ == "__main__":
    main()
