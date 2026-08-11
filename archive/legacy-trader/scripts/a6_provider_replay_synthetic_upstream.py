#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import struct
import time
from collections.abc import Sequence
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

_WEBSOCKET_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
_MAX_WEBSOCKET_PAYLOAD_BYTES = 4 * 1024 * 1024
_TRANSIENT_RESET_METADATA_KEY = "a6_provider_replay_fixture_reset"


def _response_document(response_id: str, text: str) -> dict[str, object]:
    return {
        "id": response_id,
        "object": "response",
        "created_at": int(time.time()),
        "status": "completed",
        "model": "gpt-5.6-sol",
        "output": [
            {
                "id": f"msg_{response_id}",
                "type": "message",
                "status": "completed",
                "role": "assistant",
                "content": [
                    {
                        "type": "output_text",
                        "text": text,
                        "annotations": [],
                    }
                ],
            }
        ],
        "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
    }


def _sse_event(event: str, document: dict[str, object]) -> bytes:
    payload = json.dumps(document, separators=(",", ":"), sort_keys=True)
    return f"event: {event}\ndata: {payload}\n\n".encode()


def _websocket_accept(key: str) -> str:
    digest = hashlib.sha1(
        (str(key) + _WEBSOCKET_GUID).encode("ascii"),
        usedforsecurity=False,
    ).digest()
    return base64.b64encode(digest).decode("ascii")


def _read_exact(source, length: int) -> bytes:
    payload = source.read(length)
    if len(payload) != length:
        raise EOFError("websocket frame truncated")
    return payload


def _read_websocket_frame(source) -> tuple[int, bytes]:
    first, second = _read_exact(source, 2)
    if not first & 0x80 or first & 0x70:
        raise ValueError("websocket fragmentation or reserved bits are unsupported")
    opcode = first & 0x0F
    if not second & 0x80:
        raise ValueError("client websocket frame must be masked")
    length = second & 0x7F
    if length == 126:
        length = struct.unpack("!H", _read_exact(source, 2))[0]
    elif length == 127:
        length = struct.unpack("!Q", _read_exact(source, 8))[0]
    if length > _MAX_WEBSOCKET_PAYLOAD_BYTES:
        raise ValueError("websocket payload exceeds fixture limit")
    mask = _read_exact(source, 4)
    payload = _read_exact(source, length)
    return opcode, bytes(value ^ mask[index % 4] for index, value in enumerate(payload))


def _write_websocket_frame(destination, opcode: int, payload: bytes) -> None:
    length = len(payload)
    header = bytearray([0x80 | opcode])
    if length < 126:
        header.append(length)
    elif length <= 0xFFFF:
        header.extend((126, *struct.pack("!H", length)))
    else:
        header.extend((127, *struct.pack("!Q", length)))
    destination.write(bytes(header) + payload)
    destination.flush()


class SyntheticReplayServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], log_path: Path) -> None:
        super().__init__(address, SyntheticReplayHandler)
        self.log_path = log_path

    def record(self, document: dict[str, object]) -> None:
        line = json.dumps(document, separators=(",", ":"), sort_keys=True) + "\n"
        descriptor = os.open(
            self.log_path,
            os.O_APPEND | os.O_CREAT | os.O_WRONLY,
            0o600,
        )
        try:
            os.write(descriptor, line.encode())
        finally:
            os.close(descriptor)


class SyntheticReplayHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server: SyntheticReplayServer

    def log_message(self, format: str, *args: object) -> None:
        return

    def _body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or "0")
        return self.rfile.read(length) if length else b""

    def _send_json(self, status: int, document: dict[str, object]) -> None:
        body = json.dumps(document, separators=(",", ":"), sort_keys=True).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    def _send_sse(self, events: Sequence[tuple[str, dict[str, object]]]) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        for event, document in events:
            self.wfile.write(_sse_event(event, document))
            self.wfile.flush()
        self.close_connection = True

    def _record_request(self, body: bytes, *, route: str, stream: bool) -> None:
        self.server.record(
            {
                "authorization_present": bool(self.headers.get("Authorization")),
                "body_bytes": len(body),
                "body_sha256": hashlib.sha256(body).hexdigest(),
                "path": self.path,
                "route": route,
                "stream": stream,
                "time_ns": time.time_ns(),
                "websocket": self.headers.get("Upgrade", "").casefold() == "websocket",
            }
        )

    def _handle_websocket(self) -> None:
        key = self.headers.get("Sec-WebSocket-Key", "").strip()
        if (
            not key
            or self.headers.get("Upgrade", "").casefold() != "websocket"
            or "upgrade" not in self.headers.get("Connection", "").casefold()
            or self.headers.get("Sec-WebSocket-Version", "") != "13"
        ):
            self.send_error(400)
            return
        self.send_response(101)
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", _websocket_accept(key))
        self.end_headers()

        try:
            while True:
                opcode, body = _read_websocket_frame(self.rfile)
                if opcode == 0x9:
                    _write_websocket_frame(self.wfile, 0xA, body)
                    continue
                if opcode not in {0x1, 0x2}:
                    raise ValueError("websocket request must be text or binary")
                break
        except (EOFError, OSError, ValueError):
            self.close_connection = True
            return

        route = "a" if self.path.startswith("/a/") else "b"
        self._record_request(body, route=route, stream=True)
        frames = [
            {
                "type": "response.created",
                "response": {
                    "id": f"resp_synthetic_{route}",
                    "model": "gpt-5.6-sol",
                    "status": "in_progress",
                },
            },
            {
                "type": "response.output_text.delta",
                "response_id": f"resp_synthetic_{route}",
                "item_id": f"msg_synthetic_{route}",
                "output_index": 0,
                "content_index": 0,
                "delta": "partial-from-a" if route == "a" else "winner-from-b",
            },
        ]
        if route == "b":
            frames.append(
                {
                    "type": "response.completed",
                    "response": _response_document(
                        "resp_synthetic_b", "winner-from-b"
                    ),
                }
            )
        try:
            for frame in frames:
                _write_websocket_frame(
                    self.wfile,
                    0x1,
                    json.dumps(frame, separators=(",", ":"), sort_keys=True).encode(),
                )
            if route == "b":
                _write_websocket_frame(self.wfile, 0x8, struct.pack("!H", 1000) + b"done")
        except OSError:
            pass
        self.close_connection = True

    def do_GET(self) -> None:
        self._handle_websocket()

    def do_POST(self) -> None:
        body = self._body()
        try:
            request = json.loads(body)
        except (TypeError, ValueError):
            request = {}
        stream = request.get("stream") is True
        route = "a" if self.path.startswith("/a/") else "b"
        self._record_request(body, route=route, stream=stream)

        metadata = request.get("metadata")
        reset_transient_state = (
            route == "a"
            and not stream
            and isinstance(metadata, dict)
            and metadata.get(_TRANSIENT_RESET_METADATA_KEY) is True
        )
        if reset_transient_state:
            self._send_json(
                200,
                _response_document("resp_synthetic_a_reset", "reset-from-a"),
            )
            return
        if route == "a" and not stream:
            self._send_json(
                502,
                {"error": {"message": "synthetic provider A failure", "type": "server_error"}},
            )
            return
        if route == "a":
            self._send_sse(
                [
                    (
                        "response.created",
                        {
                            "type": "response.created",
                            "response": {
                                "id": "resp_synthetic_a",
                                "model": "gpt-5.6-sol",
                                "status": "in_progress",
                            },
                        },
                    ),
                    (
                        "response.output_text.delta",
                        {
                            "type": "response.output_text.delta",
                            "response_id": "resp_synthetic_a",
                            "item_id": "msg_synthetic_a",
                            "output_index": 0,
                            "content_index": 0,
                            "delta": "partial-from-a",
                        },
                    ),
                ]
            )
            return

        if not stream:
            self._send_json(200, _response_document("resp_synthetic_b", "winner-from-b"))
            return
        self._send_sse(
            [
                (
                    "response.created",
                    {
                        "type": "response.created",
                        "response": {
                            "id": "resp_synthetic_b",
                            "model": "gpt-5.6-sol",
                            "status": "in_progress",
                        },
                    },
                ),
                (
                    "response.output_text.delta",
                    {
                        "type": "response.output_text.delta",
                        "response_id": "resp_synthetic_b",
                        "item_id": "msg_synthetic_b",
                        "output_index": 0,
                        "content_index": 0,
                        "delta": "winner-from-b",
                    },
                ),
                (
                    "response.completed",
                    {
                        "type": "response.completed",
                        "response": _response_document(
                            "resp_synthetic_b", "winner-from-b"
                        ),
                    },
                ),
            ]
        )


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser()
    command.add_argument("--host", default="127.0.0.1")
    command.add_argument("--port", type=int, default=18881)
    command.add_argument("--log-file", type=Path, required=True)
    return command


def main(argv: Sequence[str] | None = None) -> None:
    args = parser().parse_args(argv)
    args.log_file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    server = SyntheticReplayServer((args.host, args.port), args.log_file)
    server.serve_forever()


if __name__ == "__main__":
    main()
