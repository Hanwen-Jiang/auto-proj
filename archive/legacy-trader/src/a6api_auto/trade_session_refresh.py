from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import A6AutoError
from .models import SessionRecord
from .storage import SessionStore, private_file_lock
from .trade_authenticated_client import session_lock_path
from .workflow import Workflow, utc_now

_TERMINAL_STATUS = "abandoned"
_UNALLOCATED_STATUS = "allocated"
_SAFE_ERROR = re.compile(r"^[a-z][a-z0-9_]{0,95}$")


class SessionRefreshError(A6AutoError):
    """Stable per-session failure suitable for secret-free summaries."""


ClientFactory = Callable[[SessionRecord, str], Any]
MetadataSink = Callable[[Path, SessionRecord, str | None], None]
FailureSink = Callable[[Path, str], None]


@dataclass(frozen=True)
class _RefreshOutcome:
    path: Path
    kind: str
    eligible: bool = False
    record: SessionRecord | None = None
    proxy: str | None = None
    supplier_active: bool = False
    authenticated: bool = False
    relogged: bool = False
    error_class: str | None = None


class TradeSessionRefreshService:
    def __init__(
        self,
        *,
        client_factory: ClientFactory,
        metadata_sink: MetadataSink,
        failure_sink: FailureSink | None = None,
        mailbox: Any,
        max_workers: int = 4,
    ) -> None:
        if isinstance(max_workers, bool) or not 1 <= max_workers <= 16:
            raise ValueError("session refresh max workers invalid")
        self.client_factory = client_factory
        self.metadata_sink = metadata_sink
        self.failure_sink = failure_sink
        self.mailbox = mailbox
        self.max_workers = max_workers

    def refresh_paths(
        self,
        paths: Iterable[Path],
        proxy_by_user_id: Mapping[int, str],
    ) -> dict[str, Any]:
        ordered_paths = tuple(
            sorted((Path(path).expanduser().resolve() for path in paths), key=str)
        )
        if self.max_workers == 1 or len(ordered_paths) < 2:
            outcomes = tuple(
                self._refresh_path(path, proxy_by_user_id) for path in ordered_paths
            )
        else:
            with ThreadPoolExecutor(
                max_workers=min(self.max_workers, len(ordered_paths)),
                thread_name_prefix="a6-session-refresh",
            ) as executor:
                outcomes = tuple(
                    executor.map(
                        lambda path: self._refresh_path(path, proxy_by_user_id),
                        ordered_paths,
                    )
                )

        errors: Counter[str] = Counter()
        eligible = sum(outcome.eligible for outcome in outcomes)
        refreshed = 0
        supplier_active = 0
        cookie_valid = sum(
            outcome.authenticated and not outcome.relogged for outcome in outcomes
        )
        relogged = sum(outcome.authenticated and outcome.relogged for outcome in outcomes)
        tombstoned = 0
        skipped = sum(outcome.kind == "skipped" for outcome in outcomes)
        failed = 0

        # The sink owns one shared database connection. Keep all metadata writes on
        # this thread and in path order after the concurrent network phase completes.
        for outcome in outcomes:
            if outcome.error_class is not None:
                failed += 1
                errors[outcome.error_class] += 1
                if self.failure_sink is not None:
                    try:
                        self.failure_sink(outcome.path, outcome.error_class)
                    except Exception:
                        errors["session_refresh_failure_sync_failed"] += 1
                continue
            if outcome.record is None:
                continue
            try:
                self.metadata_sink(outcome.path, outcome.record, outcome.proxy)
            except Exception:
                failed += 1
                errors["session_refresh_metadata_sync_failed"] += 1
                continue
            if outcome.kind == "tombstone":
                tombstoned += 1
            else:
                refreshed += 1
                supplier_active += int(outcome.supplier_active)

        return {
            "success": failed == 0,
            "sessions_found": len(ordered_paths),
            "eligible": eligible,
            "refreshed": refreshed,
            "supplier_active": supplier_active,
            "cookie_valid": cookie_valid,
            "relogged": relogged,
            "tombstoned": tombstoned,
            "skipped": skipped,
            "failed": failed,
            "metadata_synced": refreshed + tombstoned,
            "errors": dict(sorted(errors.items())),
        }

    def _refresh_path(
        self,
        path: Path,
        proxy_by_user_id: Mapping[int, str],
    ) -> _RefreshOutcome:
        eligible = False
        authenticated = False
        session_relogged = False
        try:
            with private_file_lock(session_lock_path(path)):
                store = SessionStore(path)
                try:
                    record = store.load()
                except Exception:
                    return _RefreshOutcome(
                        path=path,
                        kind="failed",
                        error_class="session_refresh_session_invalid",
                    )
                if record.status == _TERMINAL_STATUS:
                    return _RefreshOutcome(path=path, kind="tombstone", record=record)
                if record.status == _UNALLOCATED_STATUS or record.user_id is None:
                    return _RefreshOutcome(path=path, kind="skipped")

                eligible = True
                user_id = int(record.user_id)
                proxy = str(proxy_by_user_id.get(user_id) or "").strip()
                if not proxy:
                    return _RefreshOutcome(
                        path=path,
                        kind="failed",
                        eligible=True,
                        error_class="session_refresh_proxy_missing",
                    )

                client = self.client_factory(record, proxy)
                supplier_rejected = record.status == "supplier_rejected"
                session_relogged = Workflow(
                    client,
                    self.mailbox,
                    store,
                ).ensure_authenticated(record)
                if supplier_rejected and record.supplier_id is None:
                    record.status = "supplier_rejected"
                    record.updated_at = utc_now()
                    store.save(record)
                authenticated = True
                supplier_is_active = self._sync_supplier_profile(store, record, client)
                return _RefreshOutcome(
                    path=path,
                    kind="refreshed",
                    eligible=True,
                    record=record,
                    proxy=proxy,
                    supplier_active=supplier_is_active,
                    authenticated=True,
                    relogged=session_relogged,
                )
        except SessionRefreshError as exc:
            error_class = _safe_error_class(exc)
        except A6AutoError as exc:
            error_class = _safe_error_class(exc)
        except Exception:
            error_class = "session_refresh_internal_error"
        return _RefreshOutcome(
            path=path,
            kind="failed",
            eligible=eligible,
            authenticated=authenticated,
            relogged=session_relogged,
            error_class=error_class,
        )

    @staticmethod
    def _sync_supplier_profile(store: SessionStore, record: SessionRecord, client: Any) -> bool:
        try:
            profile = client.supplier_me(int(record.user_id or 0))
        except A6AutoError as exc:
            if (
                str(exc) == "supplier_self_failed"
                and record.status == "supplier_rejected"
                and record.supplier_id is None
            ):
                return False
            if str(exc) == "supplier_self_failed" and record.supplier_id is None:
                profile = {}
            elif str(exc) == "supplier_self_failed" and _supplier_request_rejected(
                record,
                client,
            ):
                # A submitted application can later be rejected. Persist that terminal
                # control-plane state instead of reporting a permanent session failure.
                record.supplier_id = None
                record.status = "supplier_rejected"
                record.updated_at = utc_now()
                store.save(record)
                return False
            else:
                raise
        if not isinstance(profile, Mapping):
            raise SessionRefreshError("session_refresh_supplier_profile_invalid")

        raw_supplier_id = profile.get("id")
        if raw_supplier_id in (None, ""):
            if record.supplier_id is not None:
                raise SessionRefreshError("session_refresh_supplier_identity_missing")
            record.status = "authenticated"
            return False
        if isinstance(raw_supplier_id, bool):
            raise SessionRefreshError("session_refresh_supplier_identity_invalid")
        try:
            supplier_id = int(raw_supplier_id)
        except (TypeError, ValueError):
            raise SessionRefreshError("session_refresh_supplier_identity_invalid") from None
        if supplier_id < 1:
            raise SessionRefreshError("session_refresh_supplier_identity_invalid")
        if record.supplier_id is not None and int(record.supplier_id) != supplier_id:
            raise SessionRefreshError("session_refresh_supplier_identity_mismatch")

        record.supplier_id = supplier_id
        record.status = "supplier_submitted"
        record.updated_at = utc_now()
        store.save(record)
        return True


def _safe_error_class(exc: BaseException) -> str:
    value = str(exc)
    return value if _SAFE_ERROR.fullmatch(value) else "session_refresh_internal_error"


def _supplier_request_rejected(record: SessionRecord, client: Any) -> bool:
    try:
        requests = client.supplier_requests(int(record.user_id or 0))
    except Exception:
        return False
    if not isinstance(requests, list):
        return False
    request_id = record.request_id
    candidates = [
        item
        for item in requests
        if isinstance(item, Mapping)
        and (request_id is None or _positive_integer(item.get("id")) == request_id)
    ]
    if not candidates:
        return False
    latest = max(candidates, key=lambda item: _positive_integer(item.get("id")))
    status = latest.get("status")
    return status == 2 or str(status or "").strip().casefold() in {"denied", "rejected"}


def _positive_integer(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 0
    return parsed if parsed > 0 else 0
