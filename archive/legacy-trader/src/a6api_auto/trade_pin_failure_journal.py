from __future__ import annotations

import hashlib
import json
import os
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import A6AutoError

_CURSOR_VERSION = 1
_EVENT_VERSION = 1
_EVENT_KIND = "fixed_merchant_unavailable"
_EVENT_STATUS = 402
_CURSOR_TAIL_BYTES = 256
_MAX_READ_BYTES = 128 * 1024
_MAX_FUTURE_SKEW_NS = 5_000_000_000


class PinFailureJournalError(A6AutoError):
    """Stable errors for the sub2api-to-Pin failure journal."""


@dataclass(frozen=True)
class PinFailureJournalBatch:
    account_events: tuple[tuple[int, int], ...]
    device: int
    inode: int
    start_offset: int
    next_offset: int
    tail_sha256: str
    records_read: int
    records_valid: int
    records_stale: int
    records_invalid: int
    source_exists: bool = True

    @property
    def account_ids(self) -> tuple[int, ...]:
        return tuple(account_id for account_id, _event_ns in self.account_events)

    def public(self, *, acknowledged: bool) -> dict[str, Any]:
        return {
            "records_read": self.records_read,
            "records_valid": self.records_valid,
            "records_stale": self.records_stale,
            "records_invalid": self.records_invalid,
            "account_ids": list(self.account_ids),
            "acknowledged": acknowledged,
        }


class PinFailureJournal:
    def __init__(self, journal_path: Path, cursor_path: Path) -> None:
        self.journal_path = journal_path.expanduser().resolve()
        self.cursor_path = cursor_path.expanduser().resolve()

    def read(
        self,
        *,
        model: str,
        max_age_seconds: int = 60,
        now_ns: int | None = None,
    ) -> PinFailureJournalBatch:
        model = str(model).strip()
        if not model:
            raise PinFailureJournalError("pin_failure_model_invalid")
        if isinstance(max_age_seconds, bool) or not 1 <= max_age_seconds <= 300:
            raise PinFailureJournalError("pin_failure_max_age_invalid")
        current_ns = time.time_ns() if now_ns is None else int(now_ns)
        minimum_ns = current_ns - max_age_seconds * 1_000_000_000
        maximum_ns = current_ns + _MAX_FUTURE_SKEW_NS
        cursor = self._load_cursor()

        try:
            source = self.journal_path.open("rb")
        except FileNotFoundError:
            return PinFailureJournalBatch((), 0, 0, 0, 0, "", 0, 0, 0, 0, False)
        except OSError as exc:
            raise PinFailureJournalError("pin_failure_journal_unreadable") from exc

        with source:
            info = os.fstat(source.fileno())
            start_offset = self._validated_offset(source, info, cursor)
            source.seek(start_offset)
            chunk = source.read(_MAX_READ_BYTES)
            newline = chunk.rfind(b"\n")
            complete = chunk[: newline + 1] if newline >= 0 else b""
            next_offset = start_offset + len(complete)
            tail_sha256 = self._tail_digest(source, next_offset)

        account_events: list[tuple[int, int]] = []
        records_read = 0
        records_valid = 0
        records_stale = 0
        records_invalid = 0
        for raw_line in complete.splitlines():
            records_read += 1
            try:
                event = json.loads(raw_line)
            except (UnicodeDecodeError, json.JSONDecodeError):
                records_invalid += 1
                continue
            if not self._valid_shape(event, model=model):
                records_invalid += 1
                continue
            event_ns = int(event["at_unix_nano"])
            if event_ns < minimum_ns or event_ns > maximum_ns:
                records_stale += 1
                continue
            account_id = int(event["account_id"])
            account_events.append((account_id, event_ns))
            records_valid += 1

        return PinFailureJournalBatch(
            account_events=tuple(account_events),
            device=int(info.st_dev),
            inode=int(info.st_ino),
            start_offset=start_offset,
            next_offset=next_offset,
            tail_sha256=tail_sha256,
            records_read=records_read,
            records_valid=records_valid,
            records_stale=records_stale,
            records_invalid=records_invalid,
        )

    def acknowledge(self, batch: PinFailureJournalBatch) -> None:
        if not batch.source_exists:
            return
        payload = {
            "version": _CURSOR_VERSION,
            "device": batch.device,
            "inode": batch.inode,
            "offset": batch.next_offset,
            "tail_sha256": batch.tail_sha256,
        }
        encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True).encode() + b"\n"
        parent = self.cursor_path.parent
        temporary = self.cursor_path.with_name(f".{self.cursor_path.name}.{os.getpid()}.tmp")
        try:
            parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                view = memoryview(encoded)
                while view:
                    written = os.write(descriptor, view)
                    if written < 1:
                        raise OSError("pin failure cursor write made no progress")
                    view = view[written:]
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            os.replace(temporary, self.cursor_path)
            os.chmod(self.cursor_path, 0o600)
        except Exception as exc:
            with suppress(FileNotFoundError):
                temporary.unlink()
            raise PinFailureJournalError("pin_failure_cursor_write_failed") from exc

    def _load_cursor(self) -> dict[str, Any] | None:
        try:
            value = json.loads(self.cursor_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, UnicodeDecodeError) as exc:
            raise PinFailureJournalError("pin_failure_cursor_unreadable") from exc
        except json.JSONDecodeError:
            return None
        if not isinstance(value, dict) or value.get("version") != _CURSOR_VERSION:
            return None
        return value

    @staticmethod
    def _valid_shape(event: Any, *, model: str) -> bool:
        if not isinstance(event, dict):
            return False
        integer_fields = (event.get("version"), event.get("account_id"), event.get("status_code"))
        if any(isinstance(value, bool) or not isinstance(value, int) for value in integer_fields):
            return False
        event_ns = event.get("at_unix_nano")
        return (
            event["version"] == _EVENT_VERSION
            and event.get("kind") == _EVENT_KIND
            and event.get("model") == model
            and event["account_id"] > 0
            and event["status_code"] == _EVENT_STATUS
            and isinstance(event_ns, int)
            and not isinstance(event_ns, bool)
            and event_ns > 0
        )

    def _validated_offset(
        self,
        source: Any,
        info: os.stat_result,
        cursor: dict[str, Any] | None,
    ) -> int:
        if cursor is None:
            return 0
        try:
            device = int(cursor["device"])
            inode = int(cursor["inode"])
            offset = int(cursor["offset"])
            expected_tail = str(cursor["tail_sha256"])
        except (KeyError, TypeError, ValueError):
            return 0
        if device != info.st_dev or inode != info.st_ino or not 0 <= offset <= info.st_size:
            return 0
        if offset == 0:
            return 0
        return offset if self._tail_digest(source, offset) == expected_tail else 0

    @staticmethod
    def _tail_digest(source: Any, offset: int) -> str:
        if offset <= 0:
            return ""
        start = max(0, offset - _CURSOR_TAIL_BYTES)
        source.seek(start)
        return hashlib.sha256(source.read(offset - start)).hexdigest()
