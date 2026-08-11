#!/usr/bin/env python3
"""Collect strict A-to-B Provider replay dispatch samples without exposing secrets."""

from __future__ import annotations

import argparse
import http.client
import json
import math
import os
import ssl
import sys
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO
from urllib.parse import urlsplit

_TRANSIENT_RESET_METADATA_KEY = "a6_provider_replay_fixture_reset"


@dataclass(frozen=True)
class FixtureEvent:
    route: str
    body_sha256: str
    time_ns: int


@dataclass(frozen=True)
class RequestResult:
    status: int
    body: bytes
    elapsed_seconds: float


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, math.ceil(fraction * len(ordered)) - 1)
    return round(ordered[index], 3)


def _read_fixture_events(log_path: Path, offset: int) -> tuple[int, list[FixtureEvent]]:
    events: list[FixtureEvent] = []
    with log_path.open("rb") as source:
        source.seek(offset)
        payload = source.read()
        next_offset = source.tell()
    for raw_line in payload.splitlines():
        try:
            document = json.loads(raw_line)
            event = FixtureEvent(
                route=str(document["route"]),
                body_sha256=str(document["body_sha256"]),
                time_ns=int(document["time_ns"]),
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            continue
        if event.route in {"a", "b"} and event.body_sha256:
            events.append(event)
    return next_offset, events


def _select_strict_pair(events: list[FixtureEvent]) -> tuple[FixtureEvent, FixtureEvent] | None:
    grouped: dict[str, list[FixtureEvent]] = defaultdict(list)
    for event in events:
        grouped[event.body_sha256].append(event)
    pairs: list[tuple[FixtureEvent, FixtureEvent]] = []
    for group in grouped.values():
        ordered = sorted(group, key=lambda item: item.time_ns)
        if len(ordered) == 2 and [item.route for item in ordered] == ["a", "b"]:
            pairs.append((ordered[0], ordered[1]))
    if len(pairs) != 1:
        return None
    return pairs[0]


def _provider_a_reset_succeeded(result: RequestResult, events: list[FixtureEvent]) -> bool:
    return (
        result.status == 200
        and result.body.count(b"reset-from-a") == 1
        and [event.route for event in events] == ["a"]
    )


def _request_once(
    url: str,
    api_key: str,
    request_id: str,
    payload: bytes,
    timeout_seconds: float,
    max_response_bytes: int,
) -> RequestResult:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("endpoint must use http or https")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    connection_type = (
        http.client.HTTPSConnection
        if parsed.scheme == "https"
        else http.client.HTTPConnection
    )
    kwargs: dict[str, object] = {
        "host": parsed.hostname,
        "port": port,
        "timeout": timeout_seconds,
    }
    if parsed.scheme == "https":
        kwargs["context"] = ssl.create_default_context()
    connection = connection_type(**kwargs)
    authorization = api_key if api_key.lower().startswith("bearer ") else f"Bearer {api_key}"
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"
    started = time.perf_counter()
    try:
        connection.request(
            "POST",
            path,
            body=payload,
            headers={
                "Authorization": authorization,
                "Content-Type": "application/json",
                "User-Agent": "a6-provider-replay-benchmark/1",
                "X-Request-ID": request_id,
            },
        )
        response = connection.getresponse()
        body = response.read(max_response_bytes + 1)
        if len(body) > max_response_bytes:
            raise ValueError("response exceeds benchmark byte limit")
        return RequestResult(
            status=response.status,
            body=body,
            elapsed_seconds=time.perf_counter() - started,
        )
    finally:
        connection.close()


def _open_manifest(path: Path) -> BinaryIO:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    return os.fdopen(descriptor, "wb", buffering=0)


def _write_row(manifest: BinaryIO, values: list[object]) -> None:
    line = "\t".join(str(value) for value in values) + "\n"
    manifest.write(line.encode("ascii", "strict"))


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(description=__doc__)
    command.add_argument("--endpoint", default="http://127.0.0.1:48769/v1/responses")
    command.add_argument("--api-key-file", type=Path, default=Path("/run/hk-shadow-e2e.key"))
    command.add_argument(
        "--fixture-log",
        type=Path,
        default=Path("/opt/a6-provider-relay-hk/synthetic/requests.jsonl"),
    )
    command.add_argument("--samples", type=int, default=20)
    command.add_argument("--max-attempts", type=int, default=60)
    command.add_argument("--timeout-seconds", type=float, default=90.0)
    command.add_argument("--settle-seconds", type=float, default=0.05)
    command.add_argument(
        "--reset-a-between-samples",
        action="store_true",
        help="send a fixture-only A success before/between samples to clear model cooldown",
    )
    command.add_argument("--reset-max-attempts", type=int, default=12)
    command.add_argument("--reset-retry-seconds", type=float, default=5.0)
    command.add_argument("--require-p95-ms", type=float, default=250.0)
    command.add_argument("--max-response-bytes", type=int, default=2 * 1024 * 1024)
    command.add_argument("--manifest", type=Path)
    return command


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.samples < 1 or args.max_attempts < args.samples:
        parser().error("max attempts must be at least the positive sample count")
    if args.timeout_seconds <= 0 or args.settle_seconds < 0 or args.require_p95_ms <= 0:
        parser().error("timeouts and p95 threshold must be positive")
    if args.reset_max_attempts < 1 or args.reset_retry_seconds < 0:
        parser().error("reset attempts must be positive and reset retry delay non-negative")
    if args.max_response_bytes < 1:
        parser().error("max response bytes must be positive")

    api_key = args.api_key_file.read_text(encoding="utf-8").strip()
    if not api_key:
        raise ValueError("API key file is empty")
    timestamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    manifest_path = args.manifest or Path(f"/run/hk-shadow-dispatch-bench-{timestamp}.tsv")
    fixture_offset = args.fixture_log.stat().st_size
    accepted_dispatch_ms: list[float] = []
    accepted = 0
    attempts = 0
    leak_total = 0

    def reset_provider_a(sample_no: int) -> None:
        nonlocal fixture_offset
        for reset_attempt in range(1, args.reset_max_attempts + 1):
            request_id = (
                f"hk-shadow-hotpath-{timestamp}-reset-{sample_no:03d}-{reset_attempt:02d}"
            )
            payload = json.dumps(
                {
                    "metadata": {_TRANSIENT_RESET_METADATA_KEY: True},
                    "model": "gpt-5.6-sol",
                    "input": f"fixture transient reset {request_id}",
                    "stream": False,
                },
                separators=(",", ":"),
                sort_keys=True,
            ).encode("ascii")
            before_offset = fixture_offset
            result = _request_once(
                args.endpoint,
                api_key,
                request_id,
                payload,
                args.timeout_seconds,
                args.max_response_bytes,
            )
            if args.settle_seconds:
                time.sleep(args.settle_seconds)
            fixture_offset, events = _read_fixture_events(args.fixture_log, before_offset)
            succeeded = _provider_a_reset_succeeded(result, events)
            print(
                json.dumps(
                    {
                        "fixture_reset": sample_no,
                        "reset_attempt": reset_attempt,
                        "routes": ",".join(event.route for event in events),
                        "succeeded": succeeded,
                    },
                    separators=(",", ":"),
                    sort_keys=True,
                ),
                flush=True,
            )
            if succeeded:
                return
            if reset_attempt < args.reset_max_attempts and args.reset_retry_seconds:
                time.sleep(args.reset_retry_seconds)
        raise RuntimeError("fixture provider A transient reset did not succeed")

    with _open_manifest(manifest_path) as manifest:
        _write_row(
            manifest,
            [
                "sample",
                "attempt",
                "request_id",
                "routes",
                "body_hash_equal",
                "dispatch_ms",
                "http_status",
                "total_seconds",
                "response_bytes",
                "winner_count",
                "a_leak_count",
                "accepted",
                "reason",
            ],
        )
        if args.reset_a_between_samples:
            reset_provider_a(0)
        while accepted < args.samples and attempts < args.max_attempts:
            attempts += 1
            request_id = f"hk-shadow-hotpath-{timestamp}-{attempts:03d}"
            payload = json.dumps(
                {
                    "model": "gpt-5.6-sol",
                    "input": f"strict failover dispatch sample {request_id}",
                    "stream": False,
                },
                separators=(",", ":"),
                sort_keys=True,
            ).encode("ascii")
            before_offset = fixture_offset
            result = _request_once(
                args.endpoint,
                api_key,
                request_id,
                payload,
                args.timeout_seconds,
                args.max_response_bytes,
            )
            if args.settle_seconds:
                time.sleep(args.settle_seconds)
            fixture_offset, events = _read_fixture_events(args.fixture_log, before_offset)
            pair = _select_strict_pair(events)
            routes = ",".join(
                event.route for event in sorted(events, key=lambda item: item.time_ns)
            )
            dispatch_ms: float | None = None
            body_hash_equal = False
            if pair is not None:
                first, second = pair
                body_hash_equal = first.body_sha256 == second.body_sha256
                dispatch_ms = round((second.time_ns - first.time_ns) / 1_000_000, 3)
            winner_count = result.body.count(b"winner-from-b")
            a_leak_count = result.body.count(b"partial-from-a") + result.body.count(
                b"synthetic provider A failure"
            )
            leak_total += a_leak_count
            reason = "accepted"
            if pair is None:
                reason = "not_strict_a_b"
            elif not body_hash_equal:
                reason = "body_hash_mismatch"
            elif dispatch_ms is None or dispatch_ms < 0:
                reason = "invalid_dispatch_time"
            elif result.status != 200:
                reason = "http_status"
            elif winner_count != 1:
                reason = "winner_count"
            elif a_leak_count != 0:
                reason = "a_content_leak"
            is_accepted = reason == "accepted"
            if is_accepted:
                accepted += 1
                accepted_dispatch_ms.append(float(dispatch_ms))
            _write_row(
                manifest,
                [
                    accepted if is_accepted else "",
                    attempts,
                    request_id,
                    routes,
                    str(body_hash_equal).lower(),
                    "" if dispatch_ms is None else dispatch_ms,
                    result.status,
                    f"{result.elapsed_seconds:.3f}",
                    len(result.body),
                    winner_count,
                    a_leak_count,
                    str(is_accepted).lower(),
                    reason,
                ],
            )
            print(
                json.dumps(
                    {
                        "accepted": accepted,
                        "attempt": attempts,
                        "dispatch_ms": dispatch_ms,
                        "reason": reason,
                        "routes": routes,
                    },
                    separators=(",", ":"),
                    sort_keys=True,
                ),
                flush=True,
            )
            if args.reset_a_between_samples and is_accepted and accepted < args.samples:
                reset_provider_a(accepted)

    p50 = _percentile(accepted_dispatch_ms, 0.50)
    p95 = _percentile(accepted_dispatch_ms, 0.95)
    summary = {
        "accepted_samples": accepted,
        "attempts": attempts,
        "dispatch_ms": {"p50": p50, "p95": p95},
        "manifest": str(manifest_path),
        "partial_leak_count": leak_total,
        "threshold_ms": args.require_p95_ms,
    }
    print(json.dumps(summary, separators=(",", ":"), sort_keys=True))
    if accepted != args.samples:
        return 2
    if p95 is None or p95 >= args.require_p95_ms or leak_total != 0:
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
