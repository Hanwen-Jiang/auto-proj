from __future__ import annotations

import http.client
import json
import socket
import struct
import threading
from pathlib import Path

import pytest

from scripts.a6_provider_replay_synthetic_upstream import SyntheticReplayServer


@pytest.fixture
def synthetic_upstream(tmp_path: Path):
    server = SyntheticReplayServer(("127.0.0.1", 0), tmp_path / "requests.jsonl")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, tmp_path / "requests.jsonl"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _post(
    port: int,
    path: str,
    *,
    stream: bool,
    metadata: dict[str, object] | None = None,
) -> tuple[int, bytes]:
    document: dict[str, object] = {
        "model": "gpt-5.6-sol",
        "input": "fixture",
        "stream": stream,
    }
    if metadata is not None:
        document["metadata"] = metadata
    body = json.dumps(document).encode()
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
    connection.request(
        "POST",
        path,
        body=body,
        headers={"Authorization": "Bearer secret-not-for-log"},
    )
    response = connection.getresponse()
    document = response.read()
    connection.close()
    return response.status, document


def _client_frame(payload: bytes) -> bytes:
    mask = b"\x01\x02\x03\x04"
    encoded = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
    assert len(payload) < 126
    return bytes((0x81, 0x80 | len(payload))) + mask + encoded


def _read_exact(source, length: int) -> bytes:
    payload = source.read(length)
    if len(payload) != length:
        raise EOFError
    return payload


def _server_frame(source) -> tuple[int, bytes]:
    first, second = _read_exact(source, 2)
    assert first & 0x80
    assert not second & 0x80
    length = second & 0x7F
    if length == 126:
        length = struct.unpack("!H", _read_exact(source, 2))[0]
    elif length == 127:
        length = struct.unpack("!Q", _read_exact(source, 8))[0]
    return first & 0x0F, _read_exact(source, length)


def _websocket(port: int, path: str) -> list[tuple[int, bytes]]:
    connection = socket.create_connection(("127.0.0.1", port), timeout=2)
    source = connection.makefile("rb")
    try:
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
            "Authorization: Bearer secret-not-for-log\r\n\r\n"
        ).encode("ascii")
        connection.sendall(request)
        status = source.readline()
        assert b" 101 " in status
        while source.readline() not in {b"\r\n", b""}:
            pass
        payload = json.dumps(
            {
                "type": "response.create",
                "model": "gpt-5.6-sol",
                "input": "same-body",
                "stream": True,
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        connection.sendall(_client_frame(payload))
        frames: list[tuple[int, bytes]] = []
        while True:
            try:
                frame = _server_frame(source)
            except EOFError:
                break
            frames.append(frame)
            if frame[0] == 0x8:
                break
        return frames
    finally:
        source.close()
        connection.close()


def test_nonstream_provider_a_fails_and_provider_b_completes(
    synthetic_upstream,
) -> None:
    server, _ = synthetic_upstream
    port = server.server_address[1]

    a_status, a_body = _post(port, "/a/v1/responses", stream=False)
    b_status, b_body = _post(port, "/b/v1/responses", stream=False)

    assert a_status == 502
    assert b_status == 200
    assert b"winner-from-b" in b_body
    assert b"partial-from-a" not in b_body


def test_nonstream_fixture_reset_makes_only_provider_a_succeed(
    synthetic_upstream,
) -> None:
    server, _ = synthetic_upstream
    port = server.server_address[1]

    a_status, a_body = _post(
        port,
        "/a/v1/responses",
        stream=False,
        metadata={"a6_provider_replay_fixture_reset": True},
    )
    b_status, b_body = _post(
        port,
        "/b/v1/responses",
        stream=False,
        metadata={"a6_provider_replay_fixture_reset": True},
    )

    assert a_status == b_status == 200
    assert b"reset-from-a" in a_body
    assert b"winner-from-b" in b_body
    assert b"reset-from-a" not in b_body


def test_sse_provider_a_has_no_terminal_and_provider_b_has_one(
    synthetic_upstream,
) -> None:
    server, _ = synthetic_upstream
    port = server.server_address[1]

    a_status, a_body = _post(port, "/a/v1/responses", stream=True)
    b_status, b_body = _post(port, "/b/v1/responses", stream=True)

    assert a_status == b_status == 200
    assert b"partial-from-a" in a_body
    assert b"response.completed" not in a_body
    assert b"winner-from-b" in b_body
    assert b"response.completed" in b_body


def test_request_log_records_hash_but_not_authorization_value(
    synthetic_upstream,
) -> None:
    server, log_path = synthetic_upstream

    _post(server.server_address[1], "/a/v1/responses", stream=False)
    log = log_path.read_text(encoding="utf-8")
    document = json.loads(log)

    assert document["authorization_present"] is True
    assert len(document["body_sha256"]) == 64
    assert "secret-not-for-log" not in log


def test_websocket_provider_a_is_incomplete_and_provider_b_has_terminal(
    synthetic_upstream,
) -> None:
    server, log_path = synthetic_upstream
    port = server.server_address[1]

    a_frames = _websocket(port, "/a/v1/responses")
    b_frames = _websocket(port, "/b/v1/responses")

    a_text = b"\n".join(payload for opcode, payload in a_frames if opcode == 0x1)
    b_text = b"\n".join(payload for opcode, payload in b_frames if opcode == 0x1)
    assert b"partial-from-a" in a_text
    assert b"response.completed" not in a_text
    assert b"winner-from-b" in b_text
    assert b"response.completed" in b_text
    assert b_frames[-1][0] == 0x8

    logs = [json.loads(line) for line in log_path.read_text().splitlines()]
    assert [document["route"] for document in logs] == ["a", "b"]
    assert all(document["websocket"] is True for document in logs)
    assert logs[0]["body_sha256"] == logs[1]["body_sha256"]
    assert all("secret-not-for-log" not in json.dumps(document) for document in logs)
