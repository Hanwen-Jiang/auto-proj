#!/usr/bin/env python3
"""Measure the network stages used by a Sub2API request plane.

The benchmark intentionally uses only the standard library so it can be copied
to a small shadow host before the application runtime is installed.  It emits
transport timings and status codes, never request headers or response bodies.
"""

from __future__ import annotations

import argparse
import json
import math
import socket
import ssl
import statistics
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from urllib.parse import urlsplit


@dataclass
class Sample:
    status: int | None = None
    remote_ip: str = ""
    dns_ms: float | None = None
    connect_ms: float | None = None
    tls_ms: float | None = None
    ttfb_ms: float | None = None
    total_ms: float | None = None
    bytes_read: int = 0
    error: str = ""


@dataclass
class TargetResult:
    url: str
    samples: list[Sample] = field(default_factory=list)


def _header(value: str) -> tuple[str, str]:
    name, separator, content = str(value).partition(":")
    name = name.strip()
    if not separator or not name:
        raise ValueError("header must use NAME:VALUE")
    return name, content.strip()


def _target(url: str) -> tuple[str, int, str, str]:
    parsed = urlsplit(str(url).strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("url must use http or https")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("url port is invalid") from exc
    if port is None:
        port = 443 if parsed.scheme == "https" else 80
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"
    host_header = parsed.hostname
    if (parsed.scheme == "https" and port != 443) or (parsed.scheme == "http" and port != 80):
        host_header = f"{host_header}:{port}"
    return parsed.scheme, port, path, host_header


def _error_text(exc: BaseException) -> str:
    text = str(exc).strip().replace("\n", " ")
    return f"{type(exc).__name__}: {text[:140]}" if text else type(exc).__name__


def request_once(
    url: str,
    *,
    headers: list[tuple[str, str]],
    timeout: float,
    max_body_bytes: int,
) -> Sample:
    sample = Sample()
    started = time.perf_counter()
    sock: socket.socket | ssl.SSLSocket | None = None
    try:
        scheme, port, path, host_header = _target(url)
        dns_started = time.perf_counter()
        addresses = socket.getaddrinfo(
            urlsplit(url).hostname,
            port,
            type=socket.SOCK_STREAM,
        )
        sample.dns_ms = (time.perf_counter() - dns_started) * 1000
        if not addresses:
            raise OSError("no address returned")

        last_error: BaseException | None = None
        for family, socktype, proto, _canonname, sockaddr in addresses:
            candidate: socket.socket | ssl.SSLSocket | None = None
            try:
                candidate = socket.socket(family, socktype, proto)
                candidate.settimeout(timeout)
                connect_started = time.perf_counter()
                candidate.connect(sockaddr)
                sample.connect_ms = (time.perf_counter() - connect_started) * 1000
                sample.remote_ip = str(candidate.getpeername()[0])
                sock = candidate
                if scheme == "https":
                    tls_started = time.perf_counter()
                    context = ssl.create_default_context()
                    sock = context.wrap_socket(candidate, server_hostname=urlsplit(url).hostname)
                    sample.tls_ms = (time.perf_counter() - tls_started) * 1000
                break
            except (OSError, ssl.SSLError) as exc:
                last_error = exc
                if candidate is not None:
                    candidate.close()
        if sock is None:
            raise last_error or OSError("connection failed")

        request_headers = {"Host": host_header, "Connection": "close", "Accept": "*/*"}
        request_headers.update(headers)
        wire_headers = "".join(f"{name}: {value}\r\n" for name, value in request_headers.items())
        sock.sendall(f"GET {path} HTTP/1.1\r\n{wire_headers}\r\n".encode("ascii"))

        first = sock.recv(1)
        if not first:
            raise OSError("empty response")
        # Keep TTFB compatible with curl's time_starttransfer: it includes DNS,
        # connect, TLS, request write, and the wait for the first response byte.
        sample.ttfb_ms = (time.perf_counter() - started) * 1000
        body = bytearray(first)
        while len(body) < max_body_bytes:
            chunk = sock.recv(min(64 * 1024, max_body_bytes - len(body)))
            if not chunk:
                break
            body.extend(chunk)
        header_end = body.find(b"\r\n\r\n")
        status_line = bytes(body[: header_end if header_end >= 0 else len(body)]).splitlines()[0]
        try:
            sample.status = int(status_line.split(None, 2)[1])
        except (IndexError, ValueError) as exc:
            raise OSError("invalid HTTP status line") from exc
        sample.bytes_read = len(body)
        sample.total_ms = (time.perf_counter() - started) * 1000
    except (OSError, ValueError, ssl.SSLError) as exc:
        sample.error = _error_text(exc)
        sample.total_ms = (time.perf_counter() - started) * 1000
    finally:
        if sock is not None:
            sock.close()
    return sample


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, math.ceil(fraction * len(ordered)) - 1)
    return round(ordered[index], 3)


def _summary(result: TargetResult) -> dict[str, object]:
    def values(field: str) -> list[float]:
        return [
            float(value)
            for sample in result.samples
            if (value := getattr(sample, field)) is not None
        ]

    statuses = Counter(str(sample.status) for sample in result.samples if sample.status is not None)
    errors = Counter(sample.error for sample in result.samples if sample.error)
    return {
        "url": result.url,
        "samples": len(result.samples),
        "responses": sum(1 for sample in result.samples if sample.status is not None),
        "status_counts": dict(sorted(statuses.items())),
        "remote_ips": sorted({sample.remote_ip for sample in result.samples if sample.remote_ip}),
        "errors": dict(errors),
        "timing_ms": {
            field: {
                "p50": _percentile(values(field), 0.50),
                "p95": _percentile(values(field), 0.95),
                "mean": round(statistics.fmean(values(field)), 3) if values(field) else None,
            }
            for field in ("dns_ms", "connect_ms", "tls_ms", "ttfb_ms", "total_ms")
        },
    }


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(description=__doc__)
    command.add_argument(
        "--url",
        action="append",
        required=True,
        help="target URL; repeat per region",
    )
    command.add_argument("--count", type=int, default=10)
    command.add_argument("--timeout-seconds", type=float, default=15)
    command.add_argument("--max-body-bytes", type=int, default=32 * 1024 * 1024)
    command.add_argument(
        "--header",
        action="append",
        default=[],
        metavar="NAME:VALUE",
        help="request header; values are never printed",
    )
    command.add_argument("--json", action="store_true", dest="as_json")
    return command


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.count < 1 or args.count > 1000:
        parser().error("--count must be between 1 and 1000")
    if args.timeout_seconds <= 0 or not math.isfinite(args.timeout_seconds):
        parser().error("--timeout-seconds must be positive")
    if args.max_body_bytes < 1:
        parser().error("--max-body-bytes must be positive")
    try:
        headers = [_header(value) for value in args.header]
        for url in args.url:
            _target(url)
    except ValueError as exc:
        parser().error(str(exc))

    results = []
    for url in args.url:
        result = TargetResult(url)
        for _ in range(args.count):
            result.samples.append(
                request_once(
                    url,
                    headers=headers,
                    timeout=args.timeout_seconds,
                    max_body_bytes=args.max_body_bytes,
                )
            )
        results.append(_summary(result))

    document = {"count": args.count, "targets": results}
    if args.as_json:
        print(json.dumps(document, ensure_ascii=True, separators=(",", ":")))
    else:
        for target in results:
            timing = target["timing_ms"]
            print(
                f"{target['url']} responses={target['responses']}/{target['samples']} "
                f"status={target['status_counts']} "
                f"ttfb_p50_ms={timing['ttfb_ms']['p50']} "
                f"total_p50_ms={timing['total_ms']['p50']} "
                f"total_p95_ms={timing['total_ms']['p95']}"
            )
    return 0 if any(target["responses"] for target in results) else 2


if __name__ == "__main__":
    sys.exit(main())
