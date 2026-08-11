#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import stat
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path


class ShadowEnvError(RuntimeError):
    pass


_COPIED_KEYS = (
    "DATABASE_USER",
    "DATABASE_PASSWORD",
    "DATABASE_DBNAME",
    "REDIS_PASSWORD",
    "REDIS_DB",
    "JWT_SECRET",
    "JWT_EXPIRE_HOUR",
    "TOTP_ENCRYPTION_KEY",
    "TZ",
    "GATEWAY_SCHEDULING_OPENAI_UPSTREAM_STRATEGY",
    "SECURITY_URL_ALLOWLIST_ENABLED",
    "SECURITY_URL_ALLOWLIST_ALLOW_INSECURE_HTTP",
    "SECURITY_URL_ALLOWLIST_ALLOW_PRIVATE_HOSTS",
)

_OVERRIDES = {
    "DATA_DIR": "/opt/sub2api-hk/data",
    "AUTO_SETUP": "false",
    "SERVER_HOST": "127.0.0.1",
    "SERVER_MODE": "release",
    "RUN_MODE": "standard",
    "DATABASE_HOST": "127.0.0.1",
    "DATABASE_PORT": "15432",
    "DATABASE_SSLMODE": "disable",
    "REDIS_HOST": "127.0.0.1",
    "REDIS_PORT": "16379",
    "SERVER_PORT": "48769",
    "GATEWAY_A6_PROVIDER_REPLAY_ENABLED": "false",
    "GATEWAY_A6_PROVIDER_REPLAY_DURABLE_COORDINATION_ENABLED": "false",
    "GATEWAY_A6_PROVIDER_REPLAY_DURABLE_ATTRIBUTION_ENABLED": "false",
    "GATEWAY_A6_PROVIDER_REPLAY_CANDIDATE_SNAPSHOT_ENABLED": "false",
    "GATEWAY_A6_PROVIDER_REPLAY_DISPATCH_LEASE_ENABLED": "false",
    "GATEWAY_A6_PROVIDER_REPLAY_FAST_BLOCK_ENABLED": "false",
    "GATEWAY_A6_PROVIDER_REPLAY_USAGE_ATTRIBUTION_ENABLED": "false",
    "GATEWAY_A6_PROVIDER_REPLAY_PERSISTENCE_TIMEOUT_SECONDS": "10",
    "GATEWAY_A6_PROVIDER_REPLAY_SPOOL_DIRECTORY": "/opt/sub2api-hk/replay-spool",
    "PROXY_POOL_ENABLED": "false",
    "KIRO_ENABLED": "false",
    "CODEX_AUTH_ENABLED": "false",
}

_REQUIRED_PRESENT_KEYS = frozenset(
    {
        "DATABASE_USER",
        "DATABASE_PASSWORD",
        "DATABASE_DBNAME",
        "REDIS_PASSWORD",
        "JWT_SECRET",
        "TOTP_ENCRYPTION_KEY",
    }
)
_REQUIRED_NONEMPTY_KEYS = _REQUIRED_PRESENT_KEYS - {"REDIS_PASSWORD"}
_ENVIRONMENT_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_INTERNAL_TOKEN = re.compile(r"^[A-Za-z0-9._~-]{32,256}$")
_TRADER_INTERNAL_TOKEN_KEY = "A6_TRADER_INTERNAL_API_TOKEN"
_FAILURE_REPORT_VALUES = {
    "GATEWAY_A6_PROVIDER_REPLAY_TRADER_FAILURE_REPORT_URL": (
        "http://127.0.0.1:18765/internal/provider-attempt-failures"
    ),
    "GATEWAY_A6_PROVIDER_REPLAY_TRADER_FAILURE_REQUESTED_BY": "sub2api-hk",
    "GATEWAY_A6_PROVIDER_REPLAY_TRADER_FAILURE_TIMEOUT_MS": "2000",
}


def parse_environment_file(document: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in str(document).splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        key = key.strip()
        if not separator or _ENVIRONMENT_KEY.fullmatch(key) is None:
            raise ShadowEnvError("sub2api_hk_source_environment_invalid")
        values[key] = value.strip()
    return values


def render_environment(
    source: Mapping[str, str],
    trader_source: Mapping[str, str] | None = None,
) -> str:
    missing = sorted(key for key in _REQUIRED_PRESENT_KEYS if key not in source)
    if missing:
        raise ShadowEnvError(
            "sub2api_hk_source_environment_missing:" + ",".join(missing)
        )
    empty = sorted(key for key in _REQUIRED_NONEMPTY_KEYS if not str(source[key]))
    if empty:
        raise ShadowEnvError(
            "sub2api_hk_source_environment_empty:" + ",".join(empty)
        )
    values = {
        key: str(source[key])
        for key in _COPIED_KEYS
        if key in source and str(source[key]) != ""
    }
    values.update(_OVERRIDES)
    if trader_source is not None:
        token = str(trader_source.get(_TRADER_INTERNAL_TOKEN_KEY) or "").strip()
        if _INTERNAL_TOKEN.fullmatch(token) is None:
            raise ShadowEnvError("sub2api_hk_trader_internal_token_invalid")
        values.update(_FAILURE_REPORT_VALUES)
        values["GATEWAY_A6_PROVIDER_REPLAY_TRADER_FAILURE_REPORT_TOKEN"] = token
    return "".join(f"{key}={values[key]}\n" for key in sorted(values))


def write_private_environment(path: Path, document: str) -> None:
    destination = Path(os.path.abspath(path.expanduser()))
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if destination.exists():
        info = destination.lstat()
        if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise ShadowEnvError("sub2api_hk_destination_invalid")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        dir=destination.parent,
        text=True,
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(document)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
        os.chmod(destination, 0o600)
    finally:
        if temporary.exists():
            temporary.unlink()


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(
        description="Render the minimum private environment for the HK Sub2API shadow"
    )
    command.add_argument("--source", type=Path, required=True)
    command.add_argument("--trader-source", type=Path)
    command.add_argument("--output", type=Path, required=True)
    return command


def main(argv: Sequence[str] | None = None) -> None:
    args = parser().parse_args(argv)
    try:
        source = args.source.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        raise ShadowEnvError("sub2api_hk_source_environment_unreadable") from None
    trader_source = None
    if args.trader_source is not None:
        try:
            trader_document = args.trader_source.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            raise ShadowEnvError("sub2api_hk_trader_environment_unreadable") from None
        trader_source = parse_environment_file(trader_document)
    rendered = render_environment(parse_environment_file(source), trader_source)
    write_private_environment(args.output, rendered)
    print(
        json.dumps(
            {
                "success": True,
                "output": str(args.output),
                "key_count": len(rendered.splitlines()),
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
