from __future__ import annotations

import argparse
import fcntl
import getpass
import grp
import json
import os
import pwd
import re
import socket
import stat
import sys
import tempfile
import uuid
from collections.abc import Callable, Mapping
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_GATE_ROOT = Path("/var/lib/a6-trader/control-gates")
DEFAULT_GATE_OWNER = "root"
DEFAULT_GATE_GROUP = "a6trader"
_SCHEMA_VERSION = 1
_MAX_NOTE_LENGTH = 2048
_ROOT_MODE = 0o750
_FILE_MODE = 0o640
_COMMAND_ERROR_EXIT_CODE = 2
_CONDITION_NOT_MET_EXIT_CODE = 3
_CONDITION_FAILED_EXIT_CODE = 255
_EVENT_ID_PATTERN = re.compile(r"[0-9a-f]{32}")
_MARKER_KEYS = frozenset(
    {
        "schema_version",
        "event_id",
        "stage",
        "order",
        "enabled",
        "enabled_at",
        "actor",
        "hostname",
        "reason",
        "evidence",
        "previous_stage_event_id",
    }
)
_PROMOTE_AUDIT_KEYS = frozenset(
    {
        "schema_version",
        "event_id",
        "action",
        "phase",
        "stage",
        "order",
        "occurred_at",
        "actor",
        "hostname",
        "reason",
        "evidence",
    }
)
_REVOKE_AUDIT_KEYS = _PROMOTE_AUDIT_KEYS | {"revoked_stages"}


class SellerControlGateError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class SellerControlStage:
    name: str
    order: int
    marker_name: str

    @property
    def systemd_exec_condition(self) -> str:
        return (
            "ExecCondition=/opt/a6-trader/current/venv/bin/python "
            "/opt/a6-trader/current/scripts/manage_a6_seller_control_gates.py "
            f"check {self.name} --expected-owner {DEFAULT_GATE_OWNER} "
            f"--expected-group {DEFAULT_GATE_GROUP}"
        )


SELLER_CONTROL_STAGES = (
    SellerControlStage("retirement", 10, "10-retirement.enabled"),
    SellerControlStage("seller-pilot", 20, "20-seller-pilot.enabled"),
    SellerControlStage("seller-onboard", 30, "30-seller-onboard.enabled"),
    SellerControlStage("channel-growth", 40, "40-channel-growth.enabled"),
)
_STAGE_BY_NAME = {stage.name: stage for stage in SELLER_CONTROL_STAGES}


def seller_control_gate_status(
    root: Path | str = DEFAULT_GATE_ROOT,
) -> dict[str, Any]:
    gate_root = Path(root)
    if not gate_root.exists():
        return _status_document(gate_root, ())
    with _locked_root(gate_root, create=False):
        active = _load_active_prefix(gate_root)
        return _status_document(gate_root, active)


def check_seller_control_gate(
    stage_name: str,
    *,
    root: Path | str = DEFAULT_GATE_ROOT,
    expected_owner_uid: int,
    expected_group_gid: int,
) -> dict[str, Any]:
    stage = _stage(stage_name)
    ownership = _ownership(expected_owner_uid, expected_group_gid)
    gate_root = Path(root)
    with _locked_root(
        gate_root,
        create=False,
        shared=True,
        ownership=ownership,
    ):
        stage_index = SELLER_CONTROL_STAGES.index(stage)
        active = _load_active_prefix(
            gate_root,
            ownership=ownership,
            through_stage=stage,
        )
        if stage_index >= len(active):
            raise SellerControlGateError("seller_control_gate_stage_disabled")
        audited_active = _load_audited_active_prefix(gate_root, ownership=ownership)
        if tuple(active) != tuple(audited_active[: len(active)]):
            raise SellerControlGateError("seller_control_gate_audit_state_mismatch")

        document = _status_document(gate_root, active)
        document.update(
            {
                "checked_stage": stage.name,
                "checked_event_id": active[stage_index]["event_id"],
            }
        )
        return document


def promote_seller_control_gate(
    stage_name: str,
    *,
    reason: str,
    evidence: str,
    root: Path | str = DEFAULT_GATE_ROOT,
    clock: Callable[[], datetime] | None = None,
    actor: str | None = None,
    hostname: str | None = None,
    owner_uid: int | None = None,
    group_gid: int | None = None,
) -> dict[str, Any]:
    stage = _stage(stage_name)
    normalized_reason = _note(reason, "seller_control_gate_reason_invalid")
    normalized_evidence = _note(evidence, "seller_control_gate_evidence_invalid")
    gate_root = Path(root)
    ownership = _optional_ownership(owner_uid, group_gid)
    with _locked_root(gate_root, create=True, ownership=ownership):
        active = _load_active_prefix(gate_root, ownership=ownership)
        _validate_existing_audit_state(
            gate_root,
            active=active,
            ownership=ownership,
        )
        stage_index = SELLER_CONTROL_STAGES.index(stage)
        if stage_index < len(active):
            document = _status_document(gate_root, active)
            document.update({"success": True, "changed": False, "stage": stage.name})
            return document
        if stage_index != len(active):
            raise SellerControlGateError("seller_control_gate_promotion_order_invalid")

        event_id = uuid.uuid4().hex
        timestamp = _timestamp(clock)
        identity = _identity(actor)
        host = _identity(hostname or socket.gethostname())
        marker = {
            "schema_version": _SCHEMA_VERSION,
            "event_id": event_id,
            "stage": stage.name,
            "order": stage.order,
            "enabled": True,
            "enabled_at": timestamp,
            "actor": identity,
            "hostname": host,
            "reason": normalized_reason,
            "evidence": normalized_evidence,
            "previous_stage_event_id": (str(active[-1]["event_id"]) if active else None),
        }
        audit_base = {
            "schema_version": _SCHEMA_VERSION,
            "event_id": event_id,
            "action": "promote",
            "stage": stage.name,
            "order": stage.order,
            "occurred_at": timestamp,
            "actor": identity,
            "hostname": host,
            "reason": normalized_reason,
            "evidence": normalized_evidence,
        }
        _append_audit(
            gate_root,
            {**audit_base, "phase": "intent"},
            ownership=ownership,
        )
        _atomic_json(
            gate_root / stage.marker_name,
            marker,
            ownership=ownership,
        )
        _append_audit(
            gate_root,
            {**audit_base, "phase": "complete"},
            ownership=ownership,
        )
        active = _load_active_prefix(gate_root, ownership=ownership)
        document = _status_document(gate_root, active)
        document.update({"success": True, "changed": True, "stage": stage.name})
        return document


def revoke_seller_control_gate(
    stage_name: str,
    *,
    reason: str,
    evidence: str,
    root: Path | str = DEFAULT_GATE_ROOT,
    clock: Callable[[], datetime] | None = None,
    actor: str | None = None,
    hostname: str | None = None,
    owner_uid: int | None = None,
    group_gid: int | None = None,
) -> dict[str, Any]:
    stage = _stage(stage_name)
    normalized_reason = _note(reason, "seller_control_gate_reason_invalid")
    normalized_evidence = _note(evidence, "seller_control_gate_evidence_invalid")
    gate_root = Path(root)
    ownership = _optional_ownership(owner_uid, group_gid)
    with _locked_root(gate_root, create=True, ownership=ownership):
        active = _load_active_prefix(gate_root, ownership=ownership)
        _validate_existing_audit_state(
            gate_root,
            active=active,
            ownership=ownership,
        )
        stage_index = SELLER_CONTROL_STAGES.index(stage)
        if stage_index >= len(active):
            document = _status_document(gate_root, active)
            document.update(
                {
                    "success": True,
                    "changed": False,
                    "stage": stage.name,
                    "revoked_stages": [],
                }
            )
            return document

        removed_stages = [item.name for item in SELLER_CONTROL_STAGES[stage_index : len(active)]]
        event_id = uuid.uuid4().hex
        timestamp = _timestamp(clock)
        identity = _identity(actor)
        host = _identity(hostname or socket.gethostname())
        audit_base = {
            "schema_version": _SCHEMA_VERSION,
            "event_id": event_id,
            "action": "revoke",
            "stage": stage.name,
            "order": stage.order,
            "revoked_stages": removed_stages,
            "occurred_at": timestamp,
            "actor": identity,
            "hostname": host,
            "reason": normalized_reason,
            "evidence": normalized_evidence,
        }
        _append_audit(
            gate_root,
            {**audit_base, "phase": "intent"},
            ownership=ownership,
        )
        for item in reversed(SELLER_CONTROL_STAGES[stage_index : len(active)]):
            (gate_root / item.marker_name).unlink()
        _fsync_directory(gate_root)
        _append_audit(
            gate_root,
            {**audit_base, "phase": "complete"},
            ownership=ownership,
        )
        remaining = _load_active_prefix(gate_root, ownership=ownership)
        document = _status_document(gate_root, remaining)
        document.update(
            {
                "success": True,
                "changed": True,
                "stage": stage.name,
                "revoked_stages": removed_stages,
            }
        )
        return document


def _stage(value: str) -> SellerControlStage:
    stage = _STAGE_BY_NAME.get(str(value).strip())
    if stage is None:
        raise SellerControlGateError("seller_control_gate_stage_invalid")
    return stage


def _ownership(owner_uid: int, group_gid: int) -> tuple[int, int]:
    if (
        isinstance(owner_uid, bool)
        or not isinstance(owner_uid, int)
        or owner_uid < 0
        or isinstance(group_gid, bool)
        or not isinstance(group_gid, int)
        or group_gid < 0
    ):
        raise SellerControlGateError("seller_control_gate_ownership_invalid")
    return owner_uid, group_gid


def _optional_ownership(
    owner_uid: int | None,
    group_gid: int | None,
) -> tuple[int, int] | None:
    if owner_uid is None and group_gid is None:
        return None
    if owner_uid is None or group_gid is None:
        raise SellerControlGateError("seller_control_gate_ownership_invalid")
    return _ownership(owner_uid, group_gid)


def _note(value: str, code: str) -> str:
    normalized = str(value).strip()
    if not normalized or len(normalized) > _MAX_NOTE_LENGTH or "\x00" in normalized:
        raise SellerControlGateError(code)
    return normalized


def _identity(value: str | None) -> str:
    normalized = str(
        value or os.environ.get("SUDO_USER") or os.environ.get("USER") or getpass.getuser()
    ).strip()
    return _validated_identity(normalized, "seller_control_gate_identity_invalid")


def _validated_identity(value: object, code: str) -> str:
    if (
        not isinstance(value, str)
        or value != value.strip()
        or not value
        or len(value) > 256
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise SellerControlGateError(code)
    return value


def _validated_note(value: object, code: str) -> str:
    if not isinstance(value, str) or value != value.strip():
        raise SellerControlGateError(code)
    return _note(value, code)


def _validated_event_id(value: object, code: str) -> str:
    if not isinstance(value, str) or _EVENT_ID_PATTERN.fullmatch(value) is None:
        raise SellerControlGateError(code)
    return value


def _validated_timestamp(value: object, code: str) -> str:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise SellerControlGateError(code)
    try:
        parsed = datetime.fromisoformat(f"{value[:-1]}+00:00")
    except ValueError as exc:
        raise SellerControlGateError(code) from exc
    canonical = parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if parsed.utcoffset() is None or parsed.utcoffset().total_seconds() != 0 or canonical != value:
        raise SellerControlGateError(code)
    return value


def _timestamp(clock: Callable[[], datetime] | None) -> str:
    value = (clock or (lambda: datetime.now(timezone.utc)))()
    if not isinstance(value, datetime):
        raise SellerControlGateError("seller_control_gate_clock_invalid")
    if value.tzinfo is None or value.utcoffset() is None:
        raise SellerControlGateError("seller_control_gate_clock_invalid")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


@contextmanager
def _locked_root(
    root: Path,
    *,
    create: bool,
    shared: bool = False,
    ownership: tuple[int, int] | None = None,
):
    root_existed = os.path.lexists(root)
    if create:
        root.mkdir(parents=True, exist_ok=True, mode=_ROOT_MODE)
    try:
        root_info = root.lstat()
    except OSError as exc:
        raise SellerControlGateError("seller_control_gate_root_invalid") from exc
    if not stat.S_ISDIR(root_info.st_mode):
        raise SellerControlGateError("seller_control_gate_root_invalid")
    if not root_existed:
        os.chmod(root, _ROOT_MODE)
        if ownership is not None:
            os.chown(root, *ownership)
        root_info = root.lstat()
    if ownership is not None:
        _validate_artifact_stat(
            root_info,
            ownership=ownership,
            expected_mode=_ROOT_MODE,
            code="seller_control_gate_root_security_invalid",
            directory=True,
        )

    lock_path = root / ".gate.lock"
    lock_existed = os.path.lexists(lock_path)
    flags = os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
    if shared:
        flags |= os.O_RDONLY
    else:
        flags |= os.O_CREAT | os.O_RDWR
    try:
        descriptor = os.open(lock_path, flags, _FILE_MODE)
    except OSError as exc:
        raise SellerControlGateError("seller_control_gate_lock_invalid") from exc
    try:
        if not lock_existed:
            os.fchmod(descriptor, _FILE_MODE)
            if ownership is not None:
                os.fchown(descriptor, *ownership)
        _validate_artifact_stat(
            os.fstat(descriptor),
            ownership=ownership,
            expected_mode=_FILE_MODE if ownership is not None else None,
            code="seller_control_gate_lock_security_invalid",
        )
        fcntl.flock(descriptor, fcntl.LOCK_SH if shared else fcntl.LOCK_EX)
        yield
    finally:
        with suppress(OSError):
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        with suppress(OSError):
            os.close(descriptor)


def _load_active_prefix(
    root: Path,
    *,
    ownership: tuple[int, int] | None = None,
    through_stage: SellerControlStage | None = None,
) -> tuple[Mapping[str, Any], ...]:
    if through_stage is None:
        expected_names = {stage.marker_name for stage in SELLER_CONTROL_STAGES}
        unexpected = sorted(
            path.name for path in root.glob("*.enabled") if path.name not in expected_names
        )
        if unexpected:
            raise SellerControlGateError("seller_control_gate_marker_unknown")
        stages = SELLER_CONTROL_STAGES
    else:
        through_index = SELLER_CONTROL_STAGES.index(through_stage)
        stages = SELLER_CONTROL_STAGES[: through_index + 1]

    documents: list[Mapping[str, Any]] = []
    gap_seen = False
    previous_event_id: str | None = None
    for stage in stages:
        path = root / stage.marker_name
        if not os.path.lexists(path):
            gap_seen = True
            continue
        if gap_seen:
            raise SellerControlGateError("seller_control_gate_order_invalid")
        document = _read_marker(path, stage, ownership=ownership)
        if document.get("previous_stage_event_id") != previous_event_id:
            raise SellerControlGateError("seller_control_gate_chain_invalid")
        previous_event_id = str(document["event_id"])
        documents.append(document)
    return tuple(documents)


def _read_marker(
    path: Path,
    stage: SellerControlStage,
    *,
    ownership: tuple[int, int] | None = None,
) -> Mapping[str, Any]:
    payload = _read_regular_payload(
        path,
        ownership=ownership,
        expected_mode=_FILE_MODE if ownership is not None else None,
        code="seller_control_gate_marker_security_invalid",
    )
    try:
        text = payload.decode("utf-8")
        document = json.loads(text)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SellerControlGateError("seller_control_gate_marker_invalid") from exc
    if not isinstance(document, Mapping):
        raise SellerControlGateError("seller_control_gate_marker_invalid")
    canonical = f"{_canonical_json(document)}\n".encode()
    if payload != canonical:
        raise SellerControlGateError("seller_control_gate_marker_noncanonical")
    _validate_marker_document(document, stage)
    return document


def _validate_marker_document(
    document: Mapping[str, Any],
    stage: SellerControlStage,
) -> None:
    if (
        frozenset(document) != _MARKER_KEYS
        or type(document.get("schema_version")) is not int
        or document.get("schema_version") != _SCHEMA_VERSION
        or document.get("stage") != stage.name
        or type(document.get("order")) is not int
        or document.get("order") != stage.order
        or document.get("enabled") is not True
    ):
        raise SellerControlGateError("seller_control_gate_marker_invalid")
    _validated_event_id(document.get("event_id"), "seller_control_gate_marker_invalid")
    _validated_timestamp(document.get("enabled_at"), "seller_control_gate_marker_invalid")
    _validated_identity(document.get("actor"), "seller_control_gate_marker_invalid")
    _validated_identity(document.get("hostname"), "seller_control_gate_marker_invalid")
    _validated_note(document.get("reason"), "seller_control_gate_marker_invalid")
    _validated_note(document.get("evidence"), "seller_control_gate_marker_invalid")
    previous = document.get("previous_stage_event_id")
    if previous is not None:
        _validated_event_id(previous, "seller_control_gate_marker_invalid")


def _load_audited_active_prefix(
    root: Path,
    *,
    ownership: tuple[int, int] | None,
) -> tuple[Mapping[str, Any], ...]:
    payload = _read_regular_payload(
        root / "audit.jsonl",
        ownership=ownership,
        expected_mode=_FILE_MODE,
        code="seller_control_gate_audit_security_invalid",
    )
    if not payload or not payload.endswith(b"\n"):
        raise SellerControlGateError("seller_control_gate_audit_noncanonical")

    entries: list[Mapping[str, Any]] = []
    for raw_line in payload.splitlines(keepends=True):
        if raw_line == b"\n" or not raw_line.endswith(b"\n"):
            raise SellerControlGateError("seller_control_gate_audit_noncanonical")
        try:
            document = json.loads(raw_line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SellerControlGateError("seller_control_gate_audit_invalid") from exc
        if not isinstance(document, Mapping):
            raise SellerControlGateError("seller_control_gate_audit_invalid")
        if raw_line != f"{_canonical_json(document)}\n".encode():
            raise SellerControlGateError("seller_control_gate_audit_noncanonical")
        _validate_audit_document(document)
        entries.append(document)

    if len(entries) % 2 != 0:
        raise SellerControlGateError("seller_control_gate_audit_incomplete")

    active: list[Mapping[str, Any]] = []
    seen_event_ids: set[str] = set()
    for offset in range(0, len(entries), 2):
        intent = entries[offset]
        complete = entries[offset + 1]
        if intent["phase"] != "intent" or complete["phase"] != "complete":
            raise SellerControlGateError("seller_control_gate_audit_phase_invalid")
        expected_complete = dict(intent)
        expected_complete["phase"] = "complete"
        if dict(complete) != expected_complete:
            raise SellerControlGateError("seller_control_gate_audit_pair_mismatch")
        event_id = str(intent["event_id"])
        if event_id in seen_event_ids:
            raise SellerControlGateError("seller_control_gate_audit_event_duplicate")
        seen_event_ids.add(event_id)

        stage = _stage(str(intent["stage"]))
        stage_index = SELLER_CONTROL_STAGES.index(stage)
        if intent["action"] == "promote":
            if stage_index != len(active):
                raise SellerControlGateError("seller_control_gate_audit_order_invalid")
            active.append(
                {
                    "schema_version": _SCHEMA_VERSION,
                    "event_id": event_id,
                    "stage": stage.name,
                    "order": stage.order,
                    "enabled": True,
                    "enabled_at": intent["occurred_at"],
                    "actor": intent["actor"],
                    "hostname": intent["hostname"],
                    "reason": intent["reason"],
                    "evidence": intent["evidence"],
                    "previous_stage_event_id": (active[-1]["event_id"] if active else None),
                }
            )
            continue

        if stage_index >= len(active):
            raise SellerControlGateError("seller_control_gate_audit_order_invalid")
        expected_revoked = [item.name for item in SELLER_CONTROL_STAGES[stage_index : len(active)]]
        if intent["revoked_stages"] != expected_revoked:
            raise SellerControlGateError("seller_control_gate_audit_revoke_invalid")
        del active[stage_index:]
    return tuple(active)


def _validate_existing_audit_state(
    root: Path,
    *,
    active: tuple[Mapping[str, Any], ...],
    ownership: tuple[int, int] | None,
) -> None:
    audit_path = root / "audit.jsonl"
    if not os.path.lexists(audit_path):
        if active:
            raise SellerControlGateError("seller_control_gate_audit_missing")
        return
    audited_active = _load_audited_active_prefix(root, ownership=ownership)
    if tuple(active) != tuple(audited_active):
        raise SellerControlGateError("seller_control_gate_audit_state_mismatch")


def _validate_audit_document(document: Mapping[str, Any]) -> None:
    action = document.get("action")
    expected_keys = _REVOKE_AUDIT_KEYS if action == "revoke" else _PROMOTE_AUDIT_KEYS
    if (
        action not in {"promote", "revoke"}
        or frozenset(document) != expected_keys
        or type(document.get("schema_version")) is not int
        or document.get("schema_version") != _SCHEMA_VERSION
        or document.get("phase") not in {"intent", "complete"}
    ):
        raise SellerControlGateError("seller_control_gate_audit_invalid")
    event_id = _validated_event_id(
        document.get("event_id"),
        "seller_control_gate_audit_invalid",
    )
    stage = _stage(str(document.get("stage") or ""))
    if type(document.get("order")) is not int or document.get("order") != stage.order:
        raise SellerControlGateError("seller_control_gate_audit_invalid")
    _validated_timestamp(document.get("occurred_at"), "seller_control_gate_audit_invalid")
    _validated_identity(document.get("actor"), "seller_control_gate_audit_invalid")
    _validated_identity(document.get("hostname"), "seller_control_gate_audit_invalid")
    _validated_note(document.get("reason"), "seller_control_gate_audit_invalid")
    _validated_note(document.get("evidence"), "seller_control_gate_audit_invalid")
    if not event_id:
        raise SellerControlGateError("seller_control_gate_audit_invalid")
    if action == "revoke":
        revoked = document.get("revoked_stages")
        if not isinstance(revoked, list) or any(
            not isinstance(item, str) or item not in _STAGE_BY_NAME for item in revoked
        ):
            raise SellerControlGateError("seller_control_gate_audit_invalid")


def _read_regular_payload(
    path: Path,
    *,
    ownership: tuple[int, int] | None,
    expected_mode: int | None,
    code: str,
) -> bytes:
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
        )
    except OSError as exc:
        raise SellerControlGateError(code) from exc
    try:
        _validate_artifact_stat(
            os.fstat(descriptor),
            ownership=ownership,
            expected_mode=expected_mode,
            code=code,
        )
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = -1
            return stream.read()
    finally:
        if descriptor >= 0:
            with suppress(OSError):
                os.close(descriptor)


def _validate_artifact_stat(
    info: os.stat_result,
    *,
    ownership: tuple[int, int] | None,
    expected_mode: int | None,
    code: str,
    directory: bool = False,
) -> None:
    expected_type = stat.S_ISDIR if directory else stat.S_ISREG
    if not expected_type(info.st_mode):
        raise SellerControlGateError(code)
    if expected_mode is not None and stat.S_IMODE(info.st_mode) != expected_mode:
        raise SellerControlGateError(code)
    if ownership is not None and (info.st_uid, info.st_gid) != ownership:
        raise SellerControlGateError(code)


def _canonical_json(document: Mapping[str, Any]) -> str:
    return json.dumps(document, sort_keys=True, separators=(",", ":"))


def _status_document(
    root: Path,
    active: tuple[Mapping[str, Any], ...],
) -> dict[str, Any]:
    active_by_stage = {str(item["stage"]): item for item in active}
    next_stage = (
        SELLER_CONTROL_STAGES[len(active)].name
        if len(active) < len(SELLER_CONTROL_STAGES)
        else None
    )
    return {
        "success": True,
        "gate_root": str(root),
        "active_stage_count": len(active),
        "fully_enabled": len(active) == len(SELLER_CONTROL_STAGES),
        "next_stage": next_stage,
        "stages": [
            {
                "stage": stage.name,
                "order": stage.order,
                "enabled": stage.name in active_by_stage,
                "marker": str(root / stage.marker_name),
                "event_id": (
                    active_by_stage[stage.name].get("event_id")
                    if stage.name in active_by_stage
                    else None
                ),
                "enabled_at": (
                    active_by_stage[stage.name].get("enabled_at")
                    if stage.name in active_by_stage
                    else None
                ),
            }
            for stage in SELLER_CONTROL_STAGES
        ],
    }


def _atomic_json(
    path: Path,
    document: Mapping[str, Any],
    *,
    ownership: tuple[int, int] | None,
) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary_path = Path(temporary_name)
    try:
        os.fchmod(descriptor, _FILE_MODE)
        if ownership is not None:
            os.fchown(descriptor, *ownership)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            descriptor = -1
            stream.write(f"{_canonical_json(document)}\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
        _fsync_directory(path.parent)
    finally:
        if descriptor >= 0:
            with suppress(OSError):
                os.close(descriptor)
        temporary_path.unlink(missing_ok=True)


def _append_audit(
    root: Path,
    document: Mapping[str, Any],
    *,
    ownership: tuple[int, int] | None,
) -> None:
    path = root / "audit.jsonl"
    existed = os.path.lexists(path)
    try:
        descriptor = os.open(
            path,
            os.O_CREAT | os.O_APPEND | os.O_WRONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
            _FILE_MODE,
        )
    except OSError as exc:
        raise SellerControlGateError("seller_control_gate_audit_security_invalid") from exc
    try:
        if not existed:
            os.fchmod(descriptor, _FILE_MODE)
            if ownership is not None:
                os.fchown(descriptor, *ownership)
        _validate_artifact_stat(
            os.fstat(descriptor),
            ownership=ownership,
            expected_mode=_FILE_MODE,
            code="seller_control_gate_audit_security_invalid",
        )
        payload = f"{_canonical_json(document)}\n"
        with os.fdopen(descriptor, "a", encoding="utf-8") as stream:
            descriptor = -1
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        if descriptor >= 0:
            with suppress(OSError):
                os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _resolved_ownership(owner: str, group: str) -> tuple[int, int]:
    try:
        owner_uid = pwd.getpwnam(owner).pw_uid
    except KeyError as exc:
        raise SellerControlGateError("seller_control_gate_owner_unknown") from exc
    try:
        group_gid = grp.getgrnam(group).gr_gid
    except KeyError as exc:
        raise SellerControlGateError("seller_control_gate_group_unknown") from exc
    return _ownership(owner_uid, group_gid)


def _add_ownership_arguments(command: argparse.ArgumentParser) -> None:
    command.add_argument("--expected-owner", default=DEFAULT_GATE_OWNER)
    command.add_argument("--expected-group", default=DEFAULT_GATE_GROUP)


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(
        description="Manage persistent ordered gates for A6 Seller control loops",
    )
    command.add_argument("--root", type=Path, default=DEFAULT_GATE_ROOT)
    subcommands = command.add_subparsers(dest="command", required=True)
    subcommands.add_parser("status")
    check = subcommands.add_parser("check")
    check.add_argument("stage", choices=tuple(_STAGE_BY_NAME))
    _add_ownership_arguments(check)
    for name in ("promote", "revoke"):
        operation = subcommands.add_parser(name)
        operation.add_argument("stage", choices=tuple(_STAGE_BY_NAME))
        operation.add_argument("--reason", required=True)
        operation.add_argument("--evidence", required=True)
        _add_ownership_arguments(operation)
    return command


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "status":
            result = seller_control_gate_status(args.root)
        elif args.command == "check":
            owner_uid, group_gid = _resolved_ownership(
                args.expected_owner,
                args.expected_group,
            )
            result = check_seller_control_gate(
                args.stage,
                root=args.root,
                expected_owner_uid=owner_uid,
                expected_group_gid=group_gid,
            )
        elif args.command == "promote":
            owner_uid, group_gid = _resolved_ownership(
                args.expected_owner,
                args.expected_group,
            )
            result = promote_seller_control_gate(
                args.stage,
                reason=args.reason,
                evidence=args.evidence,
                root=args.root,
                owner_uid=owner_uid,
                group_gid=group_gid,
            )
        else:
            owner_uid, group_gid = _resolved_ownership(
                args.expected_owner,
                args.expected_group,
            )
            result = revoke_seller_control_gate(
                args.stage,
                reason=args.reason,
                evidence=args.evidence,
                root=args.root,
                owner_uid=owner_uid,
                group_gid=group_gid,
            )
        print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
        return 0
    except SellerControlGateError as exc:
        print(
            json.dumps(
                {"success": False, "error": exc.code},
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            file=sys.stderr,
        )
        if args.command == "check":
            if exc.code == "seller_control_gate_stage_disabled":
                return _CONDITION_NOT_MET_EXIT_CODE
            return _CONDITION_FAILED_EXIT_CODE
        return _COMMAND_ERROR_EXIT_CODE


if __name__ == "__main__":
    raise SystemExit(main())
