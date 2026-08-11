from __future__ import annotations

import argparse
import hashlib
import io
import json
import math
import os
import posixpath
import re
import secrets
import stat
import sys
import time
import zipfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from importlib.resources import files
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

from .channel_policy import (
    FORBIDDEN_CHANNEL_NAME,
    GENERATED_CHANNEL_NAME_PATTERN,
    GENERATED_CHANNEL_NAME_SIGNAL_PATTERN,
    is_cleanup_forbidden_channel_name,
    is_forbidden_channel_name,
)
from .client import A6ApiClient
from .domains import load_upstream_domains
from .enodot import EnodotGroupKeyProvider
from .errors import A6AutoError
from .mail import QQMailbox
from .storage import SessionStore, read_private_json, write_private_json
from .upstream import UpstreamSelection, fetch_upstream_models
from .workflow import Workflow

UTC = timezone.utc
MIN_INTERVAL_SECONDS = 20 * 60
MAX_INTERVAL_SECONDS = 40 * 60
EXPECTED_CHANNEL_TEMPLATE_COUNT = 1500
CHANNEL_TEMPLATE_SHEET = "渠道资料"
CHANNEL_TEMPLATE_HEADERS = (
    "channel_name",
    "channel_description",
    "model_description",
)
EMPTY_CHANNEL_MODELS_WIRE_VALUE = ","
_CELL_REFERENCE = re.compile(r"^([A-Z]+)[1-9][0-9]*$")
_RELATIONSHIP_ID = (
    "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
)


def _timestamp(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, UTC).isoformat().replace("+00:00", "Z")


def _positive_int(value: Any, error: str) -> int:
    if isinstance(value, bool):
        raise A6AutoError(error)
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise A6AutoError(error) from exc
    if result < 1:
        raise A6AutoError(error)
    return result


def _base36(value: int) -> str:
    alphabet = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    digits = ""
    while value:
        value, remainder = divmod(value, 36)
        digits = alphabet[remainder] + digits
    return digits or "0"


def stable_channel_nickname(a6_user_id: int, channel_name: str = "") -> str:
    user_id = _positive_int(a6_user_id, "slow_channel_user_id_invalid")
    name = str(channel_name).strip()
    digest = hashlib.sha256(f"{user_id}\0{name}".encode()).hexdigest()[:5].upper()
    nickname = f"C{_base36(user_id)[-4:]}{digest}"
    if not 1 <= len(nickname) <= 10:
        raise A6AutoError("slow_channel_nickname_invalid")
    return nickname


def channel_model_scope_empty(value: Any) -> bool:
    if isinstance(value, str):
        values = re.split(r"[,，\s]+", value)
    elif isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        values = [str(item) for item in value]
    else:
        values = [str(value or "")]
    return not any(item.strip() for item in values)


@dataclass(frozen=True)
class SlowChannelTemplate:
    source_row: int
    channel_name: str
    channel_description: str

    def __post_init__(self) -> None:
        source_row = _positive_int(self.source_row, "slow_channel_template_row_invalid")
        channel_name = str(self.channel_name).strip()
        channel_description = str(self.channel_description).strip()
        if not 1 <= len(channel_name) <= 30 or is_forbidden_channel_name(channel_name):
            raise A6AutoError("slow_channel_template_name_invalid")
        if not channel_description:
            raise A6AutoError("slow_channel_template_description_invalid")
        object.__setattr__(self, "source_row", source_row)
        object.__setattr__(self, "channel_name", channel_name)
        object.__setattr__(self, "channel_description", channel_description)


def _column_index(reference: str) -> int:
    match = _CELL_REFERENCE.fullmatch(reference)
    if match is None:
        raise A6AutoError("slow_channel_template_workbook_invalid")
    value = 0
    for character in match.group(1):
        value = value * 26 + ord(character) - ord("A") + 1
    return value - 1


def _shared_strings(archive: zipfile.ZipFile) -> tuple[str, ...]:
    try:
        root = ElementTree.fromstring(archive.read("xl/sharedStrings.xml"))
    except KeyError:
        return ()
    except ElementTree.ParseError as exc:
        raise A6AutoError("slow_channel_template_workbook_invalid") from exc
    return tuple(
        "".join(node.text or "" for node in item.findall(".//{*}t"))
        for item in root.findall("{*}si")
    )


def _cell_text(cell: ElementTree.Element, shared: Sequence[str]) -> str:
    cell_type = cell.get("t", "")
    if cell_type == "inlineStr":
        return "".join(node.text or "" for node in cell.findall(".//{*}t"))
    value = cell.find("{*}v")
    text = "" if value is None else str(value.text or "")
    if cell_type == "s":
        try:
            return shared[int(text)]
        except (IndexError, ValueError) as exc:
            raise A6AutoError("slow_channel_template_workbook_invalid") from exc
    return text


def _channel_sheet_path(archive: zipfile.ZipFile) -> str:
    try:
        workbook = ElementTree.fromstring(archive.read("xl/workbook.xml"))
        relationships = ElementTree.fromstring(
            archive.read("xl/_rels/workbook.xml.rels")
        )
    except (KeyError, ElementTree.ParseError) as exc:
        raise A6AutoError("slow_channel_template_workbook_invalid") from exc
    sheets = [
        item
        for item in workbook.findall(".//{*}sheet")
        if item.get("name") == CHANNEL_TEMPLATE_SHEET
    ]
    if len(sheets) != 1:
        raise A6AutoError("slow_channel_template_sheet_invalid")
    relationship_id = sheets[0].get(_RELATIONSHIP_ID, "")
    targets = [
        item.get("Target", "")
        for item in relationships.findall("{*}Relationship")
        if item.get("Id") == relationship_id
    ]
    if len(targets) != 1 or not targets[0]:
        raise A6AutoError("slow_channel_template_sheet_invalid")
    target = targets[0]
    if target.startswith("/"):
        return target.lstrip("/")
    return posixpath.normpath(posixpath.join("xl", target))


def load_channel_templates(workbook_path: Path | None = None) -> tuple[SlowChannelTemplate, ...]:
    try:
        content = (
            workbook_path.expanduser().resolve().read_bytes()
            if workbook_path is not None
            else files("a6api_auto")
            .joinpath("data/merchant_templates_split.xlsx")
            .read_bytes()
        )
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            shared = _shared_strings(archive)
            root = ElementTree.fromstring(archive.read(_channel_sheet_path(archive)))
    except A6AutoError:
        raise
    except (OSError, KeyError, zipfile.BadZipFile, ElementTree.ParseError) as exc:
        raise A6AutoError("slow_channel_template_workbook_invalid") from exc
    rows: list[list[str]] = []
    source_rows: list[int] = []
    for row in root.findall(".//{*}sheetData/{*}row"):
        values: dict[int, str] = {}
        for cell in row.findall("{*}c"):
            values[_column_index(str(cell.get("r") or ""))] = _cell_text(cell, shared)
        width = max(values, default=-1) + 1
        rows.append([values.get(index, "").strip() for index in range(width)])
        source_rows.append(int(row.get("r") or len(source_rows) + 1))
    if not rows or tuple(rows[0]) != CHANNEL_TEMPLATE_HEADERS:
        raise A6AutoError("slow_channel_template_headers_invalid")
    if len(rows) != EXPECTED_CHANNEL_TEMPLATE_COUNT + 1:
        raise A6AutoError("slow_channel_template_count_invalid")
    if any(len(row) != len(CHANNEL_TEMPLATE_HEADERS) or not all(row) for row in rows[1:]):
        raise A6AutoError("slow_channel_template_rows_invalid")
    templates = tuple(
        SlowChannelTemplate(
            source_row=source_row,
            channel_name=row[0] if len(row) > 0 else "",
            channel_description=row[1] if len(row) > 1 else "",
        )
        for source_row, row in zip(source_rows[1:], rows[1:], strict=True)
    )
    if len(templates) != EXPECTED_CHANNEL_TEMPLATE_COUNT:
        raise A6AutoError("slow_channel_template_count_invalid")
    if len({item.channel_name for item in templates}) != len(templates):
        raise A6AutoError("slow_channel_template_names_duplicate")
    if len({item.channel_description for item in templates}) != len(templates):
        raise A6AutoError("slow_channel_template_descriptions_duplicate")
    return templates


@dataclass(frozen=True)
class SlowChannelTarget:
    merchant_id: int
    a6_user_id: int
    supplier_id: int
    session_ref: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "merchant_id",
            _positive_int(self.merchant_id, "slow_channel_merchant_id_invalid"),
        )
        object.__setattr__(
            self,
            "a6_user_id",
            _positive_int(self.a6_user_id, "slow_channel_user_id_invalid"),
        )
        object.__setattr__(
            self,
            "supplier_id",
            _positive_int(self.supplier_id, "slow_channel_supplier_id_invalid"),
        )
        session_ref = str(self.session_ref).strip()
        if not session_ref:
            raise A6AutoError("slow_channel_session_ref_invalid")
        object.__setattr__(self, "session_ref", session_ref)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> SlowChannelTarget:
        return cls(
            merchant_id=value.get("merchant_id"),
            a6_user_id=value.get("a6_user_id"),
            supplier_id=value.get("supplier_id"),
            session_ref=str(value.get("session_ref") or ""),
        )


@dataclass(frozen=True)
class SlowChannelConfig:
    state_file: Path
    template_workbook: Path | None = None
    min_interval_seconds: float = MIN_INTERVAL_SECONDS
    max_interval_seconds: float = MAX_INTERVAL_SECONDS
    per_merchant_retries: int = 2

    def validate(self) -> None:
        values = (self.min_interval_seconds, self.max_interval_seconds)
        if any(isinstance(value, bool) or not math.isfinite(value) for value in values):
            raise A6AutoError("slow_channel_interval_invalid")
        if not (
            MIN_INTERVAL_SECONDS
            <= self.min_interval_seconds
            <= self.max_interval_seconds
            <= MAX_INTERVAL_SECONDS
        ):
            raise A6AutoError("slow_channel_interval_must_be_20_to_40_minutes")
        if not 1 <= self.per_merchant_retries <= 5:
            raise A6AutoError("slow_channel_retries_invalid")


class SlowChannelStateStore:
    def __init__(self, path: Path) -> None:
        self.path = path.expanduser().resolve()

    def exists(self) -> bool:
        return self.path.is_file()

    def load(self) -> dict[str, Any]:
        state = read_private_json(self.path)
        if state.get("schema_version") != 2:
            raise A6AutoError("slow_channel_state_version_unsupported")
        return state

    def save(self, state: dict[str, Any]) -> None:
        write_private_json(self.path, state)


TargetLoader = Callable[[], Sequence[SlowChannelTarget]]
TemplateLoader = Callable[[], Sequence[SlowChannelTemplate]]
Registrar = Callable[
    [SlowChannelTarget, UpstreamSelection, SlowChannelTemplate],
    Mapping[str, Any],
]
Selector = Callable[[Mapping[str, Any] | None], UpstreamSelection]


def _template_catalog_sha256(templates: Sequence[SlowChannelTemplate]) -> str:
    document = [
        [item.source_row, item.channel_name, item.channel_description]
        for item in templates
    ]
    return hashlib.sha256(
        json.dumps(document, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


class SlowChannelRegistrationRunner:
    def __init__(
        self,
        config: SlowChannelConfig,
        *,
        target_loader: TargetLoader,
        template_loader: TemplateLoader,
        selector: Selector,
        registrar: Registrar,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], float] = time.time,
        random_interval: Callable[[float, float], float] | None = None,
        random_choice: Callable[[Sequence[Any]], Any] | None = None,
        event: Callable[[Mapping[str, Any]], None] | None = None,
    ) -> None:
        config.validate()
        self.config = config
        self.store = SlowChannelStateStore(config.state_file)
        self.target_loader = target_loader
        self.templates = tuple(template_loader())
        if len(self.templates) != EXPECTED_CHANNEL_TEMPLATE_COUNT or any(
            not isinstance(item, SlowChannelTemplate) for item in self.templates
        ):
            raise A6AutoError("slow_channel_template_count_invalid")
        if len({item.channel_name for item in self.templates}) != len(self.templates):
            raise A6AutoError("slow_channel_template_names_duplicate")
        if len({item.channel_description for item in self.templates}) != len(self.templates):
            raise A6AutoError("slow_channel_template_descriptions_duplicate")
        self.catalog_sha256 = _template_catalog_sha256(self.templates)
        self.selector = selector
        self.registrar = registrar
        self.sleep = sleep
        self.now = now
        self.random_interval = random_interval or secrets.SystemRandom().uniform
        self.random_choice = random_choice or secrets.SystemRandom().choice
        self.event = event or (lambda _event: None)

    def _new_state(self) -> dict[str, Any]:
        targets = tuple(self.target_loader())
        merchant_ids = [target.merchant_id for target in targets]
        user_ids = [target.a6_user_id for target in targets]
        if not targets:
            raise A6AutoError("slow_channel_targets_empty")
        if len(merchant_ids) != len(set(merchant_ids)) or len(user_ids) != len(set(user_ids)):
            raise A6AutoError("slow_channel_targets_duplicate")
        if len(targets) > len(self.templates):
            raise A6AutoError("slow_channel_template_catalog_exhausted")
        created = self.now()
        return {
            "schema_version": 2,
            "batch_id": f"slow-channel-{int(created)}-{secrets.token_hex(3)}",
            "created_at": _timestamp(created),
            "updated_at": _timestamp(created),
            "template_catalog_sha256": self.catalog_sha256,
            "template_count": len(self.templates),
            "min_interval_seconds": self.config.min_interval_seconds,
            "max_interval_seconds": self.config.max_interval_seconds,
            "per_merchant_retries": self.config.per_merchant_retries,
            "targets": [asdict(target) for target in targets],
            "completed": [],
            "failed": [],
            "attempts": [],
            "pending_merchant_id": 0,
            "pending_template_index": -1,
            "pending_retries": 0,
            "pending_upstream": None,
            "next_run_at": 0.0,
        }

    def _load_or_create(self) -> dict[str, Any]:
        if not self.store.exists():
            state = self._new_state()
            self.store.save(state)
            return state
        state = self.store.load()
        matches = (
            str(state.get("template_catalog_sha256") or "") == self.catalog_sha256
            and int(state.get("template_count") or 0) == len(self.templates)
            and float(state.get("min_interval_seconds") or 0)
            == self.config.min_interval_seconds
            and float(state.get("max_interval_seconds") or 0)
            == self.config.max_interval_seconds
            and int(state.get("per_merchant_retries") or 0)
            == self.config.per_merchant_retries
        )
        if not matches:
            raise A6AutoError("slow_channel_state_config_mismatch")
        targets = state.get("targets")
        if not isinstance(targets, list) or not targets:
            raise A6AutoError("slow_channel_state_targets_invalid")
        return state

    def _save(self, state: dict[str, Any]) -> None:
        state["updated_at"] = _timestamp(self.now())
        self.store.save(state)

    def _targets(self, state: Mapping[str, Any]) -> tuple[SlowChannelTarget, ...]:
        try:
            targets = tuple(SlowChannelTarget.from_mapping(item) for item in state["targets"])
        except (KeyError, TypeError, A6AutoError) as exc:
            raise A6AutoError("slow_channel_state_targets_invalid") from exc
        if len({item.merchant_id for item in targets}) != len(targets):
            raise A6AutoError("slow_channel_state_targets_invalid")
        return targets

    def _next_target(
        self, state: dict[str, Any], targets: Sequence[SlowChannelTarget]
    ) -> SlowChannelTarget | None:
        by_id = {target.merchant_id: target for target in targets}
        pending_id = int(state.get("pending_merchant_id") or 0)
        if pending_id:
            target = by_id.get(pending_id)
            if target is None:
                raise A6AutoError("slow_channel_pending_target_missing")
            return target
        processed = {
            int(item.get("merchant_id") or 0)
            for key in ("completed", "failed")
            for item in state.get(key) or []
            if isinstance(item, Mapping)
        }
        remaining = [item for item in targets if item.merchant_id not in processed]
        target = self.random_choice(remaining) if remaining else None
        if target is not None:
            state["pending_merchant_id"] = target.merchant_id
            state["pending_template_index"] = -1
            state["pending_retries"] = 0
            state["pending_upstream"] = None
            self._save(state)
        return target

    def _template(self, state: dict[str, Any]) -> tuple[int, SlowChannelTemplate]:
        pending_index = int(state.get("pending_template_index", -1))
        if pending_index >= 0:
            try:
                return pending_index, self.templates[pending_index]
            except IndexError as exc:
                raise A6AutoError("slow_channel_pending_template_missing") from exc
        used = {
            int(item.get("template_index", -1))
            for key in ("completed", "failed")
            for item in state.get(key) or []
            if isinstance(item, Mapping)
        }
        available = [
            index for index in range(len(self.templates)) if index not in used
        ]
        if not available:
            raise A6AutoError("slow_channel_template_catalog_exhausted")
        pending_index = int(self.random_choice(available))
        state["pending_template_index"] = pending_index
        self._save(state)
        return pending_index, self.templates[pending_index]

    def _wait(self, state: dict[str, Any]) -> None:
        remaining = float(state.get("next_run_at") or 0) - self.now()
        if remaining > 0:
            self.event(
                {
                    "event": "slow_channel_waiting",
                    "seconds": round(remaining),
                    "next_run_at": _timestamp(self.now() + remaining),
                }
            )
            self.sleep(remaining)
        state["next_run_at"] = 0.0
        self._save(state)

    def _selection(self, state: dict[str, Any]) -> UpstreamSelection:
        raw = state.get("pending_upstream")
        pending = raw if isinstance(raw, Mapping) else None
        selection = self.selector(pending)
        normalized = {
            "base_url": selection.base_url.rstrip("/"),
            "key_ref": str(selection.key_ref),
        }
        if not normalized["base_url"] or not normalized["key_ref"] or not selection.api_key:
            raise A6AutoError("slow_channel_upstream_invalid")
        if pending is not None and dict(pending) != normalized:
            raise A6AutoError("slow_channel_upstream_changed")
        if pending is None:
            state["pending_upstream"] = normalized
            self._save(state)
        return selection

    def _has_remaining(self, state: Mapping[str, Any], target_count: int) -> bool:
        completed = len(state.get("completed") or [])
        failed = len(state.get("failed") or [])
        return completed + failed < target_count

    def run(self) -> dict[str, Any]:
        state = self._load_or_create()
        targets = self._targets(state)
        while self._has_remaining(state, len(targets)):
            target = self._next_target(state, targets)
            if target is None:
                raise A6AutoError("slow_channel_state_progress_invalid")
            template_index, template = self._template(state)
            self._wait(state)
            state["attempts"].append(
                {
                    "merchant_id": target.merchant_id,
                    "template_index": template_index,
                    "template_row": template.source_row,
                    "channel_name": template.channel_name,
                    "attempted_at": _timestamp(self.now()),
                }
            )
            self._save(state)
            try:
                selection = self._selection(state)
                result = dict(self.registrar(target, selection, template))
                result.pop("api_key", None)
                result.update(
                    {
                        "merchant_id": target.merchant_id,
                        "a6_user_id": target.a6_user_id,
                        "template_index": template_index,
                        "template_row": template.source_row,
                        "channel_name": template.channel_name,
                        "completed_at": _timestamp(self.now()),
                    }
                )
                state["completed"].append(result)
                state["pending_merchant_id"] = 0
                state["pending_template_index"] = -1
                state["pending_retries"] = 0
                state["pending_upstream"] = None
                self.event(
                    {
                        "event": "slow_channel_completed",
                        "merchant_id": target.merchant_id,
                        "completed": len(state["completed"]),
                        "target": len(targets),
                        "action": result.get("action"),
                    }
                )
            except A6AutoError as exc:
                retries = int(state.get("pending_retries") or 0) + 1
                state["pending_retries"] = retries
                error = str(exc)
                state["attempts"][-1]["error"] = error
                terminal = retries >= self.config.per_merchant_retries
                if terminal:
                    state["failed"].append(
                        {
                            "merchant_id": target.merchant_id,
                            "a6_user_id": target.a6_user_id,
                            "template_index": template_index,
                            "template_row": template.source_row,
                            "channel_name": template.channel_name,
                            "error": error,
                            "retries": retries,
                            "failed_at": _timestamp(self.now()),
                        }
                    )
                    state["pending_merchant_id"] = 0
                    state["pending_template_index"] = -1
                    state["pending_retries"] = 0
                    state["pending_upstream"] = None
                self.event(
                    {
                        "event": "slow_channel_failed",
                        "merchant_id": target.merchant_id,
                        "error": error,
                        "retry": retries,
                        "terminal": terminal,
                    }
                )
            if self._has_remaining(state, len(targets)):
                delay = self.random_interval(
                    self.config.min_interval_seconds,
                    self.config.max_interval_seconds,
                )
                state["next_run_at"] = self.now() + delay
            self._save(state)
        return {
            "success": not state["failed"],
            "target": len(targets),
            "completed": len(state["completed"]),
            "failed": len(state["failed"]),
            "attempts": len(state["attempts"]),
            "template_catalog_sha256": self.catalog_sha256,
            "state_file": str(self.store.path),
        }


class A6SlowChannelRegistrar:
    def __init__(
        self,
        *,
        sessions_dir: Path,
        proxy_map_file: Path,
        client_factory: Callable[[Any, str], A6ApiClient] | None = None,
        model_fetcher: Callable[[str, str], list[str]] = fetch_upstream_models,
        readback_sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.sessions_dir = sessions_dir.expanduser().resolve()
        self.proxy_map_file = proxy_map_file.expanduser().resolve()
        self.client_factory = client_factory or (
            lambda record, proxy: A6ApiClient(record.origin, proxy_url=proxy)
        )
        self.model_fetcher = model_fetcher
        self.readback_sleep = readback_sleep

    def _session_store(self, target: SlowChannelTarget) -> SessionStore:
        path = Path(target.session_ref).expanduser().resolve()
        try:
            path.relative_to(self.sessions_dir)
        except ValueError as exc:
            raise A6AutoError("slow_channel_session_outside_root") from exc
        if not path.is_file():
            raise A6AutoError("slow_channel_session_missing")
        return SessionStore(path)

    def _proxy(self, user_id: int) -> str:
        values = read_private_json(self.proxy_map_file)
        proxy = values.get(str(user_id))
        if not isinstance(proxy, str) or not proxy.strip():
            raise A6AutoError("slow_channel_proxy_mapping_missing")
        return proxy.strip()

    @staticmethod
    def _matches(item: Mapping[str, Any], channel_name: str, nickname: str) -> bool:
        name = str(item.get("name") or item.get("channel_name") or "").strip()
        current_nickname = str(item.get("nickname") or "").strip()
        return name == channel_name or current_nickname == nickname

    @staticmethod
    def _active_request(item: Mapping[str, Any]) -> bool:
        status = item.get("status")
        if status in (None, 0, 1, "0", "1"):
            return True
        return str(status).strip().casefold() in {"pending", "approved", "active"}

    @staticmethod
    def _supplier_payload(
        target: SlowChannelTarget,
        profile: Mapping[str, Any],
    ) -> dict[str, str]:
        supplier_id = _positive_int(
            profile.get("id"), "slow_channel_supplier_profile_invalid"
        )
        if supplier_id != target.supplier_id:
            raise A6AutoError("slow_channel_supplier_identity_mismatch")
        profile_user = profile.get("user_id")
        if profile_user not in (None, "") and _positive_int(
            profile_user, "slow_channel_supplier_profile_invalid"
        ) != target.a6_user_id:
            raise A6AutoError("slow_channel_supplier_identity_mismatch")
        values = {
            "name": profile.get("name"),
            "contact_info": profile.get("contact_info"),
            "description": profile.get("description", ""),
        }
        if (
            not isinstance(values["name"], str)
            or not values["name"].strip()
            or not isinstance(values["contact_info"], str)
            or not values["contact_info"].strip()
            or not isinstance(values["description"], str)
        ):
            raise A6AutoError("slow_channel_supplier_profile_invalid")
        return {key: value.strip() for key, value in values.items()}

    @staticmethod
    def _safe_existing(
        target: SlowChannelTarget,
        item: Mapping[str, Any],
        *,
        source: str,
    ) -> dict[str, Any]:
        return {
            "action": "already_exists",
            "source": source,
            "supplier_id": target.supplier_id,
            "channel_id": int(item.get("id") or item.get("channel_id") or 0) or None,
            "request_id": int(item.get("request_id") or item.get("id") or 0) or None,
            "request_status": item.get("status"),
        }

    def _authenticated_client(self, target: SlowChannelTarget) -> A6ApiClient:
        store = self._session_store(target)
        record = store.load()
        if record.user_id != target.a6_user_id or record.supplier_id != target.supplier_id:
            raise A6AutoError("slow_channel_session_identity_mismatch")
        client = self.client_factory(record, self._proxy(target.a6_user_id))
        Workflow(client, QQMailbox(), store).refresh_session(record)
        return client

    @staticmethod
    def _all_listings(client: Any, user_id: int) -> list[dict[str, Any]]:
        listings: list[dict[str, Any]] = []
        page = 1
        while page <= 100:
            document = client.supplier_listings(user_id, page=page, size=100)
            items = document.get("items")
            if not isinstance(items, list):
                raise A6AutoError("slow_channel_listing_inventory_invalid")
            listings.extend(dict(item) for item in items if isinstance(item, Mapping))
            try:
                total = int(document.get("total"))
            except (TypeError, ValueError):
                total = len(listings)
            if not items or len(listings) >= total:
                break
            page += 1
        else:
            raise A6AutoError("slow_channel_listing_inventory_too_large")
        return listings

    def cleanup_forbidden(self, target: SlowChannelTarget) -> Mapping[str, Any]:
        client = self._authenticated_client(target)
        channels = client.supplier_channels(target.a6_user_id)
        forbidden = [
            item
            for item in channels
            if is_cleanup_forbidden_channel_name(item.get("name"))
        ]
        if not forbidden:
            return {
                "merchant_id": target.merchant_id,
                "channel_count": 0,
                "listing_count": 0,
            }
        channel_ids = {
            _positive_int(item.get("id"), "slow_channel_forbidden_channel_invalid")
            for item in forbidden
        }
        listings = self._all_listings(client, target.a6_user_id)
        active_listings = [
            item
            for item in listings
            if int(item.get("channel_id") or 0) in channel_ids
            and str(item.get("status")) != "0"
        ]
        for listing in active_listings:
            listing_id = _positive_int(
                listing.get("id"), "slow_channel_forbidden_listing_invalid"
            )
            client.takedown_supplier_listing(target.a6_user_id, listing_id)
        for channel in forbidden:
            channel_binding_id = _positive_int(
                channel.get("binding_id") or channel.get("id"),
                "slow_channel_forbidden_channel_invalid",
            )
            if str(channel.get("status")) == "1":
                client.takedown_supplier_channel(
                    target.a6_user_id,
                    channel_binding_id,
                )
            client.update_supplier_channel(
                target.a6_user_id,
                channel_binding_id,
                {"models": EMPTY_CHANNEL_MODELS_WIRE_VALUE},
            )
        refreshed_channels = client.supplier_channels(target.a6_user_id)
        remaining_online = [
            item
            for item in refreshed_channels
            if is_cleanup_forbidden_channel_name(item.get("name"))
            and str(item.get("status")) == "1"
        ]
        remaining_models = [
            item
            for item in refreshed_channels
            if is_cleanup_forbidden_channel_name(item.get("name"))
            and not channel_model_scope_empty(item.get("models"))
        ]
        refreshed_listings = self._all_listings(client, target.a6_user_id)
        remaining_listings = [
            item
            for item in refreshed_listings
            if int(item.get("channel_id") or 0) in channel_ids
            and str(item.get("status")) != "0"
        ]
        if remaining_online or remaining_models or remaining_listings:
            raise A6AutoError("slow_channel_forbidden_cleanup_readback_failed")
        return {
            "merchant_id": target.merchant_id,
            "channel_count": len(forbidden),
            "listing_count": len(active_listings),
        }

    def __call__(
        self,
        target: SlowChannelTarget,
        selection: UpstreamSelection,
        template: SlowChannelTemplate,
    ) -> Mapping[str, Any]:
        client = self._authenticated_client(target)
        profile = client.supplier_me(target.a6_user_id)
        supplier = self._supplier_payload(target, profile)
        nickname = stable_channel_nickname(target.a6_user_id, template.channel_name)
        channels = client.supplier_channels(target.a6_user_id)
        existing_channels = [
            item
            for item in channels
            if self._matches(item, template.channel_name, nickname)
        ]
        if existing_channels:
            selected = max(existing_channels, key=lambda item: int(item.get("id") or 0))
            return self._safe_existing(target, selected, source="channel")
        requests = client.supplier_requests(target.a6_user_id)
        existing_requests = [
            item
            for item in requests
            if self._active_request(item)
            and self._matches(item, template.channel_name, nickname)
        ]
        if existing_requests:
            selected = max(existing_requests, key=lambda item: int(item.get("id") or 0))
            return self._safe_existing(target, selected, source="request")
        try:
            models = self.model_fetcher(selection.base_url, selection.api_key)
        except A6AutoError:
            models = client.fetch_models_via_a6(
                target.a6_user_id,
                selection.base_url,
                selection.api_key,
            )
        payload = {
            "supplier": supplier,
            "channel": {
                "name": template.channel_name,
                "nickname": nickname,
                "channel_type": 1,
                "base_url": selection.base_url.rstrip("/"),
                "api_key": selection.api_key,
                "models": ",".join(models),
                "description": template.channel_description,
            },
        }
        response = client.apply_supplier(target.a6_user_id, payload)
        request_id = int(response.get("request_id") or response.get("id") or 0)
        for attempt in range(3):
            requests = client.supplier_requests(target.a6_user_id)
            matched = [
                item
                for item in requests
                if (request_id and int(item.get("id") or 0) == request_id)
                or (
                    self._active_request(item)
                    and self._matches(item, template.channel_name, nickname)
                )
            ]
            if matched:
                selected = max(matched, key=lambda item: int(item.get("id") or 0))
                return {
                    "action": "submitted",
                    "supplier_id": int(selected.get("supplier_id") or target.supplier_id),
                    "request_id": int(selected.get("id") or request_id) or None,
                    "request_status": selected.get("status"),
                    "model_count": len(models),
                    "channel_name": template.channel_name,
                    "channel_description": template.channel_description,
                    "channel_nickname": nickname,
                    "upstream_base_url": selection.base_url.rstrip("/"),
                    "upstream_key_ref": selection.key_ref,
                }
            if attempt < 2:
                self.readback_sleep(1.0)
        raise A6AutoError("slow_channel_request_readback_missing")


def load_postgres_targets(dsn: str) -> tuple[SlowChannelTarget, ...]:
    try:
        import psycopg

        with psycopg.connect(dsn) as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT id, a6_user_id, supplier_id, session_ref
                FROM a6_trade.a6_merchants
                WHERE a6_user_id IS NOT NULL
                  AND supplier_id IS NOT NULL
                  AND session_ref IS NOT NULL
                  AND session_ref <> ''
                  AND status = 'supplier_submitted'
                ORDER BY id
                """
            )
            rows = cursor.fetchall()
    except Exception as exc:
        raise A6AutoError("slow_channel_target_query_failed") from exc
    return tuple(
        SlowChannelTarget(
            merchant_id=row[0],
            a6_user_id=row[1],
            supplier_id=row[2],
            session_ref=row[3],
        )
        for row in rows
    )


def load_postgres_cleanup_targets(dsn: str) -> tuple[SlowChannelTarget, ...]:
    try:
        import psycopg

        with psycopg.connect(dsn) as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT DISTINCT merchant.id,
                       merchant.a6_user_id,
                       merchant.supplier_id,
                       merchant.session_ref
                FROM a6_trade.a6_merchants merchant
                JOIN a6_trade.a6_supplier_channels_current channel
                  ON channel.merchant_id = merchant.id
                WHERE channel.name = %s
                  AND merchant.a6_user_id IS NOT NULL
                  AND merchant.supplier_id IS NOT NULL
                  AND merchant.session_ref IS NOT NULL
                  AND merchant.session_ref <> ''
                ORDER BY merchant.id
                """,
                (FORBIDDEN_CHANNEL_NAME,),
            )
            rows = cursor.fetchall()
    except Exception as exc:
        raise A6AutoError("slow_channel_cleanup_target_query_failed") from exc
    return tuple(
        SlowChannelTarget(
            merchant_id=row[0],
            a6_user_id=row[1],
            supplier_id=row[2],
            session_ref=row[3],
        )
        for row in rows
    )


def retire_forbidden_managed_bindings(dsn: str) -> dict[str, int]:
    try:
        import psycopg

        with psycopg.connect(dsn) as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                WITH forbidden_channels AS (
                    SELECT channel_binding.id
                    FROM a6_trade.a6_managed_channel_bindings channel_binding
                    JOIN a6_trade.a6_supplier_channels_current channel
                      ON channel.merchant_id = channel_binding.merchant_id
                     AND channel.a6_channel_id = channel_binding.a6_channel_id
                    WHERE btrim(channel.name) = %s
                       OR (
                           btrim(channel.name) ~ %s
                           AND btrim(channel.name) ~ %s
                       )
                )
                UPDATE a6_trade.a6_managed_listing_bindings listing_binding
                SET control_state = 'retired',
                    last_error_class = 'forbidden_channel_name',
                    updated_at = clock_timestamp()
                WHERE listing_binding.channel_binding_id IN (
                    SELECT id FROM forbidden_channels
                )
                  AND listing_binding.control_state <> 'retired'
                """,
                (
                    FORBIDDEN_CHANNEL_NAME,
                    GENERATED_CHANNEL_NAME_PATTERN,
                    GENERATED_CHANNEL_NAME_SIGNAL_PATTERN,
                ),
            )
            retired_listings = max(int(cursor.rowcount or 0), 0)
            cursor.execute(
                """
                UPDATE a6_trade.a6_managed_channel_bindings channel_binding
                SET control_state = 'retired',
                    last_error_class = 'forbidden_channel_name',
                    updated_at = clock_timestamp()
                FROM a6_trade.a6_supplier_channels_current channel
                WHERE channel.merchant_id = channel_binding.merchant_id
                  AND channel.a6_channel_id = channel_binding.a6_channel_id
                  AND (
                      btrim(channel.name) = %s
                      OR (
                          btrim(channel.name) ~ %s
                          AND btrim(channel.name) ~ %s
                      )
                  )
                  AND channel_binding.control_state <> 'retired'
                """,
                (
                    FORBIDDEN_CHANNEL_NAME,
                    GENERATED_CHANNEL_NAME_PATTERN,
                    GENERATED_CHANNEL_NAME_SIGNAL_PATTERN,
                ),
            )
            retired_channels = max(int(cursor.rowcount or 0), 0)
    except Exception as exc:
        raise A6AutoError("slow_channel_forbidden_binding_retire_failed") from exc
    return {
        "retired_channel_bindings": retired_channels,
        "retired_listing_bindings": retired_listings,
    }


def cleanup_forbidden_targets(
    registrar: A6SlowChannelRegistrar,
    targets: Sequence[SlowChannelTarget],
    *,
    event: Callable[[Mapping[str, Any]], None] | None = None,
) -> dict[str, int]:
    emit = event or (lambda _event: None)
    cleaned_channels = 0
    cleaned_listings = 0
    failed = 0
    for target in targets:
        try:
            result = registrar.cleanup_forbidden(target)
            cleaned_channels += int(result.get("channel_count") or 0)
            cleaned_listings += int(result.get("listing_count") or 0)
            emit({"event": "forbidden_channel_cleaned", **dict(result)})
        except A6AutoError as exc:
            failed += 1
            emit(
                {
                    "event": "forbidden_channel_cleanup_failed",
                    "merchant_id": target.merchant_id,
                    "error": str(exc),
                }
            )
    return {
        "target_count": len(targets),
        "cleaned_channel_count": cleaned_channels,
        "cleaned_listing_count": cleaned_listings,
        "failed": failed,
    }


def _private_text(path: Path, error: str) -> str:
    path = path.expanduser().resolve()
    try:
        info = path.stat()
        value = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise A6AutoError(error) from exc
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or not value:
        raise A6AutoError(error)
    return value


def _database_dsn(args: argparse.Namespace) -> str:
    if args.database_dsn_file:
        return _private_text(args.database_dsn_file, "slow_channel_database_dsn_invalid")
    value = os.environ.get(args.database_dsn_env, "").strip()
    if not value:
        raise A6AutoError("slow_channel_database_dsn_missing")
    return value


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(
        description="random, resumable workbook-driven A6 channel registration"
    )
    command.add_argument("--live", action="store_true")
    command.add_argument("--cleanup-forbidden-only", action="store_true")
    command.add_argument("--database-dsn-env", default="A6_TRADE_DATABASE_DSN")
    command.add_argument("--database-dsn-file", type=Path)
    command.add_argument("--sessions-dir", type=Path, required=True)
    command.add_argument("--proxy-map-file", type=Path, required=True)
    command.add_argument("--state-file", type=Path, required=True)
    command.add_argument("--template-workbook", type=Path)
    command.add_argument("--min-interval-minutes", type=float, default=20)
    command.add_argument("--max-interval-minutes", type=float, default=40)
    command.add_argument("--per-merchant-retries", type=int, default=2)
    command.add_argument("--enodot-admin-url", default="https://ai.enodot.shop")
    command.add_argument("--enodot-admin-key-file", type=Path, required=True)
    command.add_argument("--enodot-group", default="TPA6API")
    command.add_argument("--upstream-domain-file", type=Path)
    return command


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        config = SlowChannelConfig(
            state_file=args.state_file,
            template_workbook=args.template_workbook,
            min_interval_seconds=args.min_interval_minutes * 60,
            max_interval_seconds=args.max_interval_minutes * 60,
            per_merchant_retries=args.per_merchant_retries,
        )
        config.validate()
        templates = load_channel_templates(config.template_workbook)
        dsn = _database_dsn(args)
        targets = load_postgres_targets(dsn)
        cleanup_targets = load_postgres_cleanup_targets(dsn)
        if not args.live:
            print(
                json.dumps(
                    {
                        "ready": True,
                        "target": len(targets),
                        "cleanup_target": len(cleanup_targets),
                        "template_count": len(templates),
                        "template_catalog_sha256": _template_catalog_sha256(templates),
                        "interval_minutes": [
                            args.min_interval_minutes,
                            args.max_interval_minutes,
                        ],
                        "mutations": False,
                    },
                    ensure_ascii=False,
                )
            )
            return 0
        def emit(event: Mapping[str, Any]) -> None:
            print(json.dumps(dict(event), ensure_ascii=False), flush=True)

        registrar = A6SlowChannelRegistrar(
            sessions_dir=args.sessions_dir,
            proxy_map_file=args.proxy_map_file,
        )
        cleanup = cleanup_forbidden_targets(registrar, cleanup_targets, event=emit)
        if cleanup["failed"]:
            emit({"event": "forbidden_channel_cleanup_summary", **cleanup})
            raise A6AutoError("slow_channel_forbidden_cleanup_failed")
        retired = retire_forbidden_managed_bindings(dsn)
        emit({"event": "forbidden_channel_cleanup_summary", **cleanup, **retired})
        if args.cleanup_forbidden_only:
            print(
                json.dumps(
                    {"success": True, "cleanup": cleanup, "bindings": retired},
                    ensure_ascii=False,
                ),
                flush=True,
            )
            return 0
        domains = load_upstream_domains(args.upstream_domain_file)
        provider = EnodotGroupKeyProvider(
            args.enodot_admin_url,
            _private_text(args.enodot_admin_key_file, "slow_channel_admin_key_invalid"),
            args.enodot_group,
            domains,
        )
        result = SlowChannelRegistrationRunner(
            config,
            target_loader=lambda: targets,
            template_loader=lambda: templates,
            selector=provider.select,
            registrar=registrar,
            event=emit,
        ).run()
        print(json.dumps(result, ensure_ascii=False), flush=True)
        return 0 if result["success"] else 2
    except A6AutoError as exc:
        print(json.dumps({"success": False, "error": str(exc)}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
