from __future__ import annotations

import fcntl
import json
import os
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any

from .errors import A6AutoError
from .models import SessionRecord


def ensure_private_directory(path: Path) -> Path:
    path = path.expanduser().resolve()
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path, 0o700)
    return path


@contextmanager
def private_file_lock(path: Path) -> Iterator[None]:
    path = path.expanduser().resolve()
    ensure_private_directory(path.parent)
    descriptor = -1
    try:
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
    except OSError as exc:
        if descriptor >= 0:
            os.close(descriptor)
        raise A6AutoError("private_file_lock_failed") from exc
    try:
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def write_private_json(path: Path, value: dict[str, Any]) -> None:
    path = path.expanduser().resolve()
    ensure_private_directory(path.parent)
    payload = json.dumps(value, ensure_ascii=True, indent=2, sort_keys=True).encode() + b"\n"
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        try:
            existing = path.stat()
            existing_owner = (existing.st_uid, existing.st_gid)
        except FileNotFoundError:
            existing_owner = None
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            get_effective_uid = getattr(os, "geteuid", None)
            if (
                existing_owner is not None
                and callable(get_effective_uid)
                and get_effective_uid() == 0
            ):
                os.fchown(descriptor, *existing_owner)
            os.write(descriptor, payload)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    except Exception as exc:
        with suppress(FileNotFoundError):
            temporary.unlink()
        raise A6AutoError("session_write_failed") from exc


def read_private_json(path: Path) -> dict[str, Any]:
    path = path.expanduser().resolve()
    try:
        info = path.stat()
        raw = path.read_text(encoding="utf-8")
        value = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        raise A6AutoError("session_read_failed") from exc
    if info.st_mode & 0o077:
        raise A6AutoError("session_permissions_too_open")
    if not isinstance(value, dict):
        raise A6AutoError("session_shape_invalid")
    return value


class SessionStore:
    def __init__(self, path: Path) -> None:
        self.path = path.expanduser().resolve()

    def exists(self) -> bool:
        return self.path.is_file()

    def load(self) -> SessionRecord:
        value = read_private_json(self.path)
        if value.get("schema_version") != 1:
            raise A6AutoError("session_version_unsupported")
        try:
            return SessionRecord.from_dict(value)
        except TypeError as exc:
            raise A6AutoError("session_shape_invalid") from exc

    def save(self, record: SessionRecord) -> None:
        write_private_json(self.path, record.to_dict())
