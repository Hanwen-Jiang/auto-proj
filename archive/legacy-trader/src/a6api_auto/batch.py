from __future__ import annotations

import hashlib
import json
import secrets
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .client import A6ApiClient
from .errors import A6AutoError
from .mail import QQMailbox
from .merchant_policy import DEFAULT_OPERATING_GROUP, normalize_operating_group
from .storage import (
    SessionStore,
    private_file_lock,
    read_private_json,
    write_private_json,
)
from .templates import (
    choose_template,
    load_templates,
    load_workbook_template_catalog,
    sample_workbook_template_ids,
    validate_registration_template,
)
from .upstream import UpstreamSelection
from .workflow import Workflow, allocate_session

UTC = timezone.utc
NON_RETRYABLE_BATCH_ERRORS = frozenset({"email_already_registered", "qq_mailbox_unavailable"})
SHORT_RETRY_SECONDS = {
    "qq_mailbox_unavailable": 5.0,
    "qq_mail_request_failed": 60.0,
    "qq_mail_response_invalid": 60.0,
    "a6_transport_failed": 60.0,
    "upstream_models_unavailable": 60.0,
}
PENDING_PROXY_ROTATE_FAILURES = 2
MIN_BATCH_INTERVAL_SECONDS = 30 * 60
MAX_BATCH_INTERVAL_SECONDS = 8 * 60 * 60
MAX_DISTRIBUTION_WINDOW_SECONDS = 7 * 24 * 60 * 60


def _timestamp(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, UTC).isoformat().replace("+00:00", "Z")


def _proxy_fingerprint(proxy: str) -> str:
    return hashlib.sha256(proxy.encode()).hexdigest()[:12]


def _upstream_config_fingerprint(config: BatchConfig) -> str:
    values = config.upstream_base_urls or (config.upstream_base_url.rstrip("/"),)
    payload = "\n".join((*sorted(values), config.upstream_group_name))
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def _batch_contract_fingerprint(config: BatchConfig) -> str:
    template_hash = (
        load_workbook_template_catalog(config.template_workbook).sha256
        if config.template_catalog == "workbook"
        else "legacy-json-v1"
    )
    payload = {
        "operating_group": normalize_operating_group(config.operating_group),
        "template_catalog": config.template_catalog,
        "template_hash": template_hash,
        "unique_upstream_base_urls": config.unique_upstream_base_urls,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()[:16]


@dataclass(frozen=True)
class BatchConfig:
    qq_file: Path
    state_dir: Path
    state_file: Path
    proxy_urls: tuple[str, ...]
    upstream_base_url: str
    upstream_api_key: str
    target_count: int = 20
    min_interval_seconds: float = 10800
    max_interval_seconds: float = 28800
    per_account_retries: int = 3
    upstream_base_urls: tuple[str, ...] = ()
    upstream_group_name: str = ""
    reserved_proxy_fingerprints: tuple[str, ...] = ()
    template_catalog: str = "json"
    template_workbook: Path | None = None
    operating_group: str = DEFAULT_OPERATING_GROUP
    unique_upstream_base_urls: bool = False
    distribution_window_seconds: float = 0
    proxy_reservation_file: Path | None = None
    upstream_reservation_file: Path | None = None

    def validate(
        self,
        *,
        required_proxy_count: int | None = None,
        unavailable_proxy_fingerprints: tuple[str, ...] = (),
    ) -> None:
        if not 1 <= self.target_count <= 100:
            raise A6AutoError("batch_count_out_of_range")
        if not (
            MIN_BATCH_INTERVAL_SECONDS
            <= self.min_interval_seconds
            <= self.max_interval_seconds
            <= MAX_BATCH_INTERVAL_SECONDS
        ):
            raise A6AutoError("batch_interval_must_be_30_minutes_to_8_hours")
        if not 1 <= self.per_account_retries <= 10:
            raise A6AutoError("batch_retry_out_of_range")
        if not self.proxy_urls:
            raise A6AutoError("batch_proxy_pool_required")
        proxy_fingerprints = {_proxy_fingerprint(proxy) for proxy in self.proxy_urls}
        reserved = set(self.reserved_proxy_fingerprints)
        if len(reserved) != len(self.reserved_proxy_fingerprints) or any(
            len(value) != 12 or any(character not in "0123456789abcdef" for character in value)
            for value in reserved
        ):
            raise A6AutoError("batch_reserved_proxy_fingerprints_invalid")
        required_proxies = (
            self.target_count if required_proxy_count is None else required_proxy_count
        )
        if (
            isinstance(required_proxies, bool)
            or not isinstance(required_proxies, int)
            or not 0 <= required_proxies <= self.target_count
        ):
            raise A6AutoError("batch_proxy_requirement_invalid")
        available_proxy_fingerprints = (
            proxy_fingerprints - reserved - set(unavailable_proxy_fingerprints)
        )
        if len(available_proxy_fingerprints) < required_proxies:
            raise A6AutoError("batch_proxy_pool_smaller_than_target")
        if not self.upstream_base_url and not self.upstream_base_urls:
            raise A6AutoError("batch_upstream_source_required")
        if self.upstream_base_url and self.upstream_base_urls:
            raise A6AutoError("batch_upstream_mode_conflict")
        if self.upstream_base_urls and not self.upstream_group_name:
            raise A6AutoError("batch_upstream_group_required")
        if self.template_catalog not in {"json", "workbook"}:
            raise A6AutoError("batch_template_catalog_invalid")
        if self.template_catalog == "json" and self.template_workbook is not None:
            raise A6AutoError("batch_template_catalog_conflict")
        if self.template_catalog == "workbook":
            catalog = load_workbook_template_catalog(self.template_workbook)
            if min(len(catalog.suppliers), len(catalog.channels)) < self.target_count:
                raise A6AutoError("batch_template_pool_smaller_than_target")
        try:
            normalized_group = normalize_operating_group(self.operating_group)
        except ValueError as exc:
            raise A6AutoError("batch_operating_group_invalid") from exc
        if normalized_group != self.operating_group:
            raise A6AutoError("batch_operating_group_invalid")
        if not isinstance(self.unique_upstream_base_urls, bool):
            raise A6AutoError("batch_unique_upstream_setting_invalid")
        if self.unique_upstream_base_urls:
            if self.upstream_base_url and self.target_count > 1:
                raise A6AutoError("batch_unique_upstream_static_conflict")
            if self.upstream_base_urls and len(self.upstream_base_urls) < self.target_count:
                raise A6AutoError("batch_unique_upstream_pool_smaller_than_target")
        if self.upstream_reservation_file is not None and not self.unique_upstream_base_urls:
            raise A6AutoError("batch_upstream_reservation_requires_unique_urls")
        if (
            isinstance(self.distribution_window_seconds, bool)
            or self.distribution_window_seconds < 0
            or self.distribution_window_seconds > MAX_DISTRIBUTION_WINDOW_SECONDS
        ):
            raise A6AutoError("batch_distribution_window_invalid")
        if self.distribution_window_seconds:
            slot_seconds = self.distribution_window_seconds / self.target_count
            if slot_seconds < self.min_interval_seconds:
                raise A6AutoError("batch_distribution_window_too_small")


class BatchStore:
    def __init__(self, path: Path) -> None:
        self.path = path.expanduser().resolve()

    def exists(self) -> bool:
        return self.path.is_file()

    def load(self) -> dict[str, Any]:
        state = read_private_json(self.path)
        if state.get("schema_version") != 1:
            raise A6AutoError("batch_state_version_unsupported")
        return state

    def save(self, state: dict[str, Any]) -> None:
        write_private_json(self.path, state)


class ProxyDeck:
    def __init__(self, proxies: tuple[str, ...]) -> None:
        if not proxies:
            raise A6AutoError("batch_proxy_pool_required")
        self._proxies = proxies
        self._remaining: list[str] = []
        self._last = ""

    def next(self, exclude_fingerprint: str = "") -> str:
        if not self._remaining:
            self._remaining = list(self._proxies)
            secrets.SystemRandom().shuffle(self._remaining)
            if len(self._remaining) > 1 and self._remaining[-1] == self._last:
                self._remaining[0], self._remaining[-1] = (
                    self._remaining[-1],
                    self._remaining[0],
                )
        if (
            exclude_fingerprint
            and len(self._remaining) > 1
            and _proxy_fingerprint(self._remaining[-1]) == exclude_fingerprint
        ):
            replacement = next(
                (
                    index
                    for index, proxy in enumerate(self._remaining[:-1])
                    if _proxy_fingerprint(proxy) != exclude_fingerprint
                ),
                None,
            )
            if replacement is not None:
                self._remaining[replacement], self._remaining[-1] = (
                    self._remaining[-1],
                    self._remaining[replacement],
                )
        selected = self._remaining.pop()
        self._last = selected
        return selected


class SlowBatchRunner:
    def __init__(
        self,
        config: BatchConfig,
        *,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], float] = time.time,
        random_interval: Callable[[float, float], float] | None = None,
        client_factory: Callable[[str, str], A6ApiClient] | None = None,
        mailbox_factory: Callable[[], QQMailbox] = QQMailbox,
        upstream_selector: Callable[[Mapping[str, Any] | None], UpstreamSelection] | None = None,
        proxy_binder: Callable[[int, str], None] | None = None,
        event: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.config = config
        self.store = BatchStore(config.state_file)
        config.validate(required_proxy_count=0 if self.store.exists() else None)
        self.sleep = sleep
        self.now = now
        self.random_interval = random_interval or secrets.SystemRandom().uniform
        self.client_factory = client_factory or (
            lambda origin, proxy: A6ApiClient(origin, proxy_url=proxy)
        )
        self.mailbox_factory = mailbox_factory
        self.upstream_selector = upstream_selector
        self.proxy_binder = proxy_binder
        self.event = event or (lambda _event: None)
        self.proxies = ProxyDeck(config.proxy_urls)

    def _claim_shared_reservation(self, path: Path | None, value: str) -> bool:
        if path is None:
            return True
        ledger_path = path.expanduser().resolve()
        lock_path = ledger_path.with_name(f".{ledger_path.name}.lock")
        owner = str(self.store.path)
        with private_file_lock(lock_path):
            if ledger_path.exists():
                ledger = read_private_json(ledger_path)
            else:
                ledger = {"schema_version": 1, "reservations": {}}
            reservations = ledger.get("reservations")
            if ledger.get("schema_version") != 1 or not isinstance(reservations, dict):
                raise A6AutoError("batch_reservation_ledger_invalid")
            existing = reservations.get(value)
            if existing is not None:
                return (
                    isinstance(existing, Mapping)
                    and str(existing.get("batch_state") or "") == owner
                )
            reservations[value] = {
                "batch_state": owner,
                "reserved_at": _timestamp(self.now()),
            }
            write_private_json(ledger_path, ledger)
        return True

    def _restore_shared_reservations(self, state: Mapping[str, Any]) -> None:
        proxy_fingerprints = {
            str(value) for value in state.get("allocated_proxy_fingerprints") or () if str(value)
        }
        for fingerprint in sorted(proxy_fingerprints):
            if not self._claim_shared_reservation(
                self.config.proxy_reservation_file,
                fingerprint,
            ):
                raise A6AutoError("batch_allocated_proxy_reserved_elsewhere")
        upstream_base_urls = {
            str(value).rstrip("/")
            for value in state.get("allocated_upstream_base_urls") or ()
            if str(value).strip()
        }
        for base_url in sorted(upstream_base_urls):
            if not self._claim_shared_reservation(
                self.config.upstream_reservation_file,
                base_url,
            ):
                raise A6AutoError("batch_allocated_upstream_reserved_elsewhere")

    def _shared_proxy_reservations_owned_elsewhere(self) -> set[str]:
        path = self.config.proxy_reservation_file
        if path is None:
            return set()
        ledger_path = path.expanduser().resolve()
        lock_path = ledger_path.with_name(f".{ledger_path.name}.lock")
        owner = str(self.store.path)
        with private_file_lock(lock_path):
            if not ledger_path.exists():
                return set()
            ledger = read_private_json(ledger_path)
        reservations = ledger.get("reservations")
        if ledger.get("schema_version") != 1 or not isinstance(reservations, dict):
            raise A6AutoError("batch_reservation_ledger_invalid")
        return {
            str(fingerprint)
            for fingerprint, reservation in reservations.items()
            if not isinstance(reservation, Mapping)
            or str(reservation.get("batch_state") or "") != owner
        }

    @staticmethod
    def _reclaimable_proxy_fingerprints(state: Mapping[str, Any]) -> set[str]:
        abandoned_sessions = {
            str(value)
            for value in state.get("abandoned_sessions") or ()
            if isinstance(value, str) and value
        }
        allocated = {
            str(value)
            for value in state.get("allocated_proxy_fingerprints") or ()
            if isinstance(value, str) and value
        }
        return {
            str(item.get("proxy_fingerprint") or "")
            for item in state.get("failures") or ()
            if isinstance(item, Mapping)
            and str(item.get("session_file") or "") in abandoned_sessions
            and str(item.get("proxy_fingerprint") or "") in allocated
        }

    def _registration_schedule(self, created: float) -> tuple[float, ...]:
        if not self.config.distribution_window_seconds:
            return ()
        slot_seconds = self.config.distribution_window_seconds / self.config.target_count
        margin = self.config.min_interval_seconds / 2
        schedule: list[float] = []
        for index in range(self.config.target_count):
            low = margin
            high = slot_seconds - margin
            jitter = float(self.random_interval(low, high))
            if not low <= jitter <= high:
                raise A6AutoError("batch_distribution_random_value_invalid")
            schedule.append(created + index * slot_seconds + jitter)
        return tuple(schedule)

    def _new_state(self) -> dict[str, Any]:
        used_template_ids: set[str] = set()
        for path in (self.config.state_dir / "sessions").glob("session-*.json"):
            try:
                value = read_private_json(path)
            except A6AutoError:
                continue
            template_id = str(value.get("template_id") or "")
            if template_id:
                used_template_ids.add(template_id)
        for path in (self.config.state_dir / "batches").glob("*.json"):
            if path.expanduser().resolve() == self.store.path:
                continue
            try:
                value = read_private_json(path)
            except A6AutoError:
                continue
            for template_id in value.get("template_ids") or ():
                if str(template_id):
                    used_template_ids.add(str(template_id))
        if self.config.template_catalog == "workbook":
            template_ids = sample_workbook_template_ids(
                self.config.target_count,
                used_template_ids=used_template_ids,
                workbook_path=self.config.template_workbook,
            )
            template_catalog_sha256 = load_workbook_template_catalog(
                self.config.template_workbook
            ).sha256
        else:
            templates = [
                item for item in load_templates() if item.template_id not in used_template_ids
            ]
            if len(templates) < self.config.target_count:
                raise A6AutoError("batch_template_pool_smaller_than_target")
            selected = secrets.SystemRandom().sample(templates, self.config.target_count)
            template_ids = tuple(item.template_id for item in selected)
            template_catalog_sha256 = ""
        created = self.now()
        registration_schedule = self._registration_schedule(created)
        return {
            "schema_version": 1,
            "batch_id": f"batch-{int(created)}-{secrets.token_hex(3)}",
            "created_at": _timestamp(created),
            "updated_at": _timestamp(created),
            "target_count": self.config.target_count,
            "min_interval_seconds": self.config.min_interval_seconds,
            "max_interval_seconds": self.config.max_interval_seconds,
            "upstream_base_url": self.config.upstream_base_url.rstrip("/"),
            "upstream_group_name": self.config.upstream_group_name,
            "upstream_config_fingerprint": _upstream_config_fingerprint(self.config),
            "batch_contract_fingerprint": _batch_contract_fingerprint(self.config),
            "template_catalog": self.config.template_catalog,
            "template_catalog_sha256": template_catalog_sha256,
            "template_ids": list(template_ids),
            "operating_group": self.config.operating_group,
            "unique_upstream_base_urls": self.config.unique_upstream_base_urls,
            "distribution_window_seconds": self.config.distribution_window_seconds,
            "registration_schedule": list(registration_schedule),
            "allocated_upstream_base_urls": [],
            "completed": [],
            "failures": [],
            "abandoned_sessions": [],
            "attempts": 0,
            "pending_session": "",
            "pending_retries": 0,
            "pending_upstream": None,
            "next_run_at": registration_schedule[0] if registration_schedule else 0.0,
            "last_proxy_fingerprint": "",
            "pending_proxy_fingerprint": "",
            "allocated_proxy_fingerprints": [],
        }

    def _load_or_create(self) -> dict[str, Any]:
        if not self.store.exists():
            self.config.validate()
            state = self._new_state()
            self.store.save(state)
            return state
        state = self.store.load()
        stored_fingerprint = str(state.get("upstream_config_fingerprint") or "")
        fingerprint_matches = stored_fingerprint == _upstream_config_fingerprint(self.config)
        if not stored_fingerprint and not self.config.upstream_base_urls:
            fingerprint_matches = True
        stored_contract = str(state.get("batch_contract_fingerprint") or "")
        contract_matches = stored_contract == _batch_contract_fingerprint(self.config)
        if (
            not stored_contract
            and self.config.template_catalog == "json"
            and self.config.operating_group == DEFAULT_OPERATING_GROUP
            and self.config.unique_upstream_base_urls is False
        ):
            contract_matches = True
        expected = (
            state.get("target_count") == self.config.target_count
            and float(state.get("min_interval_seconds") or 0) == self.config.min_interval_seconds
            and float(state.get("max_interval_seconds") or 0) == self.config.max_interval_seconds
            and str(state.get("upstream_base_url") or "").rstrip("/")
            == self.config.upstream_base_url.rstrip("/")
            and str(state.get("upstream_group_name") or "") == self.config.upstream_group_name
            and fingerprint_matches
            and contract_matches
            and str(state.get("template_catalog") or "json") == self.config.template_catalog
            and str(state.get("operating_group") or DEFAULT_OPERATING_GROUP)
            == self.config.operating_group
            and bool(state.get("unique_upstream_base_urls", False))
            is self.config.unique_upstream_base_urls
            and float(state.get("distribution_window_seconds") or 0)
            == self.config.distribution_window_seconds
        )
        if not expected:
            raise A6AutoError("batch_state_config_mismatch")
        raw_schedule = state.get("registration_schedule") or ()
        if self.config.distribution_window_seconds:
            try:
                schedule = tuple(float(value) for value in raw_schedule)
            except (TypeError, ValueError) as exc:
                raise A6AutoError("batch_distribution_schedule_invalid") from exc
            if len(schedule) != self.config.target_count or any(
                current <= previous
                for previous, current in zip(schedule, schedule[1:], strict=False)
            ):
                raise A6AutoError("batch_distribution_schedule_invalid")
        elif raw_schedule:
            raise A6AutoError("batch_distribution_schedule_invalid")
        completed = state.get("completed")
        template_ids = state.get("template_ids")
        if (
            not isinstance(completed, list)
            or len(completed) > self.config.target_count
            or any(not isinstance(item, Mapping) for item in completed)
            or not isinstance(template_ids, list)
            or len(template_ids) != self.config.target_count
            or any(not isinstance(value, str) or not value for value in template_ids)
            or len(set(template_ids)) != len(template_ids)
        ):
            raise A6AutoError("batch_state_progress_invalid")
        raw_allocated = state.get("allocated_proxy_fingerprints", [])
        if not isinstance(raw_allocated, list):
            raise A6AutoError("batch_allocated_proxy_fingerprints_invalid")
        allocated_proxy_fingerprints = tuple(raw_allocated)
        if any(
            not isinstance(value, str)
            or len(value) != 12
            or any(character not in "0123456789abcdef" for character in value)
            for value in allocated_proxy_fingerprints
        ) or len(set(allocated_proxy_fingerprints)) != len(allocated_proxy_fingerprints):
            raise A6AutoError("batch_allocated_proxy_fingerprints_invalid")
        completed_proxy_fingerprints: list[str] = []
        for index, item in enumerate(completed):
            completed_proxy_fingerprint = str(item.get("proxy_fingerprint") or "")
            if (
                str(item.get("template_id") or "") != template_ids[index]
                or completed_proxy_fingerprint not in allocated_proxy_fingerprints
            ):
                raise A6AutoError("batch_state_progress_invalid")
            completed_proxy_fingerprints.append(completed_proxy_fingerprint)
        if len(set(completed_proxy_fingerprints)) != len(completed_proxy_fingerprints):
            raise A6AutoError("batch_state_progress_invalid")
        pending_proxy_fingerprint = state.get("pending_proxy_fingerprint", "")
        if (
            not isinstance(pending_proxy_fingerprint, str)
            or pending_proxy_fingerprint
            and pending_proxy_fingerprint not in allocated_proxy_fingerprints
        ):
            raise A6AutoError("batch_pending_proxy_invalid")
        self._restore_shared_reservations(state)
        pending = {pending_proxy_fingerprint} if pending_proxy_fingerprint else set()
        completed_proxy_set = set(completed_proxy_fingerprints)
        unavailable_allocations = completed_proxy_set | (
            set(allocated_proxy_fingerprints)
            - self._reclaimable_proxy_fingerprints(state)
            - pending
        )
        self.config.validate(
            required_proxy_count=self.config.target_count - len(completed),
            unavailable_proxy_fingerprints=tuple(
                unavailable_allocations | self._shared_proxy_reservations_owned_elsewhere()
            ),
        )
        return state

    def _save(self, state: dict[str, Any]) -> None:
        state["updated_at"] = _timestamp(self.now())
        self.store.save(state)

    def _wait_for_schedule(self, state: dict[str, Any]) -> None:
        remaining = float(state.get("next_run_at") or 0) - self.now()
        if remaining > 0:
            self.event(
                {
                    "event": "waiting",
                    "seconds": round(remaining),
                    "next_run_at": _timestamp(self.now() + remaining),
                    "batch_state": str(self.store.path),
                }
            )
            self.sleep(remaining)
        state["next_run_at"] = 0.0
        self._save(state)

    def _session(self, state: dict[str, Any]):
        pending = str(state.get("pending_session") or "")
        completed_count = len(state["completed"])
        template_id = str(state["template_ids"][completed_count])
        if pending:
            session_store = SessionStore(Path(pending))
            record = session_store.load()
            if record.operating_group != self.config.operating_group:
                raise A6AutoError("batch_pending_operating_group_mismatch")
            template = choose_template(
                record.template_id,
                workbook_path=self.config.template_workbook,
            )
            template = validate_registration_template(
                template,
                workbook_path=self.config.template_workbook,
            )
            return record, session_store, template
        template = choose_template(
            template_id,
            workbook_path=self.config.template_workbook,
        )
        validate_registration_template(
            template,
            workbook_path=self.config.template_workbook,
        )
        allocation_lock = self.config.state_dir / ".session-allocation.lock"
        with private_file_lock(allocation_lock):
            record, session_store, template = allocate_session(
                self.config.qq_file,
                self.config.state_dir,
                template_id=template_id,
                template_workbook=self.config.template_workbook,
                operating_group=self.config.operating_group,
            )
        validate_registration_template(
            template,
            workbook_path=self.config.template_workbook,
        )
        state["pending_session"] = str(session_store.path)
        state["pending_retries"] = 0
        self._save(state)
        return record, session_store, template

    def _proxy(self, state: dict[str, Any]) -> tuple[str, str]:
        proxies_by_fingerprint = {
            _proxy_fingerprint(proxy): proxy for proxy in self.config.proxy_urls
        }
        pending = str(state.get("pending_proxy_fingerprint") or "")
        if pending:
            proxy = proxies_by_fingerprint.get(pending)
            if proxy is None:
                raise A6AutoError("batch_pending_proxy_missing")
            if not self._claim_shared_reservation(
                self.config.proxy_reservation_file,
                pending,
            ):
                raise A6AutoError("batch_pending_proxy_reserved_elsewhere")
            return proxy, pending
        unavailable = {
            str(item.get("proxy_fingerprint") or "")
            for item in state.get("completed") or ()
            if isinstance(item, Mapping) and str(item.get("proxy_fingerprint") or "")
        }
        allocated = state.setdefault("allocated_proxy_fingerprints", [])
        unavailable.update(self.config.reserved_proxy_fingerprints)
        unavailable.update(set(allocated) - self._reclaimable_proxy_fingerprints(state))
        available = [
            (fingerprint, proxy)
            for fingerprint, proxy in proxies_by_fingerprint.items()
            if fingerprint not in unavailable
        ]
        secrets.SystemRandom().shuffle(available)
        for fingerprint, proxy in available:
            if not self._claim_shared_reservation(
                self.config.proxy_reservation_file,
                fingerprint,
            ):
                continue
            if fingerprint not in allocated:
                allocated.append(fingerprint)
            state["pending_proxy_fingerprint"] = fingerprint
            state["last_proxy_fingerprint"] = fingerprint
            self._save(state)
            return proxy, fingerprint
        raise A6AutoError("batch_unique_proxy_pool_exhausted")

    def _upstream(self, state: dict[str, Any]) -> UpstreamSelection:
        raw_pending = state.get("pending_upstream")
        pending = raw_pending if isinstance(raw_pending, Mapping) else None
        allocated = {
            str(value).rstrip("/")
            for value in state.get("allocated_upstream_base_urls") or ()
            if str(value).strip()
        }
        attempts = (
            max(8, len(self.config.upstream_base_urls) * 4)
            if self.config.unique_upstream_base_urls and pending is None
            else 1
        )
        for _attempt in range(attempts):
            if self.upstream_selector is None:
                selection = UpstreamSelection(
                    base_url=self.config.upstream_base_url.rstrip("/"),
                    api_key=self.config.upstream_api_key,
                    key_ref="static",
                )
            else:
                selection = self.upstream_selector(pending)
            normalized = {
                "base_url": selection.base_url.rstrip("/"),
                "key_ref": str(selection.key_ref),
            }
            if not normalized["base_url"] or not normalized["key_ref"] or not selection.api_key:
                raise A6AutoError("upstream_selection_invalid")
            if (
                self.config.upstream_base_urls
                and normalized["base_url"] not in self.config.upstream_base_urls
            ):
                raise A6AutoError("upstream_selection_domain_not_allowed")
            if pending and dict(pending) != normalized:
                raise A6AutoError("upstream_selection_changed_during_resume")
            if (
                pending is None
                and self.config.unique_upstream_base_urls
                and normalized["base_url"] in allocated
            ):
                continue
            if self.config.unique_upstream_base_urls and not self._claim_shared_reservation(
                self.config.upstream_reservation_file,
                normalized["base_url"],
            ):
                if pending is not None:
                    raise A6AutoError("batch_pending_upstream_reserved_elsewhere")
                continue
            if pending is None:
                state["pending_upstream"] = normalized
                if self.config.unique_upstream_base_urls:
                    state.setdefault("allocated_upstream_base_urls", []).append(
                        normalized["base_url"]
                    )
                self._save(state)
            return selection
        raise A6AutoError("batch_unique_upstream_pool_exhausted")

    def prepare(self) -> dict[str, Any]:
        state = self._load_or_create()
        templates = tuple(
            validate_registration_template(
                choose_template(
                    str(template_id),
                    workbook_path=self.config.template_workbook,
                ),
                workbook_path=self.config.template_workbook,
            )
            for template_id in state["template_ids"]
        )
        return {
            "prepared": True,
            "target": self.config.target_count,
            "batch_state": str(self.store.path),
            "template_catalog": self.config.template_catalog,
            "template_catalog_sha256": state.get("template_catalog_sha256", ""),
            "operating_group": self.config.operating_group,
            "unique_upstream_base_urls": self.config.unique_upstream_base_urls,
            "distribution_window_hours": self.config.distribution_window_seconds / 3600,
            "registration_schedule": [
                _timestamp(float(value)) for value in state.get("registration_schedule") or ()
            ],
            "templates": [
                {
                    "template_id": item.template_id,
                    "supplier_name": item.supplier_name,
                    "supplier_description": item.supplier_description,
                    "channel_name": item.channel_name,
                    "channel_nickname": item.channel_nickname,
                    "channel_description": item.channel_description,
                    "model_description": item.model_description,
                }
                for item in templates
            ],
        }

    def run(self) -> dict[str, Any]:
        state = self._load_or_create()
        max_attempts = self.config.target_count * self.config.per_account_retries * 4
        while len(state["completed"]) < self.config.target_count:
            if int(state.get("attempts") or 0) >= max_attempts:
                raise A6AutoError("batch_attempt_limit_reached")
            self._wait_for_schedule(state)
            record, session_store, template = self._session(state)
            proxy, fingerprint = self._proxy(state)
            state["attempts"] = int(state.get("attempts") or 0) + 1
            self._save(state)
            retry_delay_seconds: float | None = None
            completed_this_attempt = False
            try:
                upstream = self._upstream(state)
                client = self.client_factory(record.origin, proxy)
                result = Workflow(client, self.mailbox_factory(), session_store).execute(
                    record,
                    template,
                    upstream_base_url=upstream.base_url,
                    upstream_api_key=upstream.api_key,
                )
                user_id = int(result.get("user_id") or 0)
                if self.proxy_binder is not None:
                    if user_id < 1:
                        raise A6AutoError("batch_user_id_missing_for_proxy_binding")
                    self.proxy_binder(user_id, proxy)
                state["completed"].append(
                    {
                        "session_file": str(session_store.path),
                        "template_id": template.template_id,
                        "user_id": result.get("user_id"),
                        "request_id": result.get("request_id"),
                        "completed_at": _timestamp(self.now()),
                        "proxy_fingerprint": fingerprint,
                        "upstream_base_url": upstream.base_url,
                        "upstream_key_ref": upstream.key_ref,
                        "operating_group": self.config.operating_group,
                    }
                )
                state["pending_session"] = ""
                state["pending_retries"] = 0
                state["pending_upstream"] = None
                state["pending_proxy_fingerprint"] = ""
                completed_this_attempt = True
                self.event(
                    {
                        "event": "account_completed",
                        "completed": len(state["completed"]),
                        "target": self.config.target_count,
                        "session_file": str(session_store.path),
                        "batch_state": str(self.store.path),
                    }
                )
            except A6AutoError as exc:
                error = str(exc)
                retries = int(state.get("pending_retries") or 0) + 1
                state["pending_retries"] = retries
                terminal = error in NON_RETRYABLE_BATCH_ERRORS
                retry_delay_seconds = SHORT_RETRY_SECONDS.get(error)
                rotate_pending_proxy = (
                    error == "a6_transport_failed"
                    and retries >= PENDING_PROXY_ROTATE_FAILURES
                    and record.status == "allocated"
                    and record.user_id is None
                    and bool(state.get("pending_proxy_fingerprint"))
                )
                state["failures"].append(
                    {
                        "attempt": state["attempts"],
                        "error": error,
                        "session_file": str(session_store.path),
                        "failed_at": _timestamp(self.now()),
                        "proxy_fingerprint": fingerprint,
                    }
                )
                if rotate_pending_proxy:
                    state["pending_retries"] = 0
                    state["pending_proxy_fingerprint"] = ""
                elif terminal or retries >= self.config.per_account_retries:
                    record.status = "abandoned"
                    session_store.save(record)
                    state["abandoned_sessions"].append(str(session_store.path))
                    state["pending_session"] = ""
                    state["pending_retries"] = 0
                    state["pending_upstream"] = None
                    state["pending_proxy_fingerprint"] = ""
                self.event(
                    {
                        "event": "account_failed",
                        "error": error,
                        "retry": retries,
                        "retry_in_seconds": retry_delay_seconds,
                        "proxy_rotated": rotate_pending_proxy,
                        "batch_state": str(self.store.path),
                    }
                )
            if len(state["completed"]) < self.config.target_count:
                if retry_delay_seconds is not None:
                    state["next_run_at"] = self.now() + retry_delay_seconds
                elif completed_this_attempt and self.config.distribution_window_seconds:
                    schedule = tuple(
                        float(value) for value in state.get("registration_schedule") or ()
                    )
                    state["next_run_at"] = schedule[len(state["completed"])]
                else:
                    delay = self.random_interval(
                        self.config.min_interval_seconds,
                        self.config.max_interval_seconds,
                    )
                    state["next_run_at"] = self.now() + delay
            self._save(state)
        return {
            "completed": len(state["completed"]),
            "target": self.config.target_count,
            "attempts": state["attempts"],
            "failures": len(state["failures"]),
            "abandoned_sessions": len(state["abandoned_sessions"]),
            "operating_group": self.config.operating_group,
            "template_catalog": self.config.template_catalog,
            "batch_state": str(self.store.path),
        }
