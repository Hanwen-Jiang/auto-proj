from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from .client import A6ApiClient
from .errors import A6AutoError
from .mail import QQMailbox
from .models import SessionRecord
from .storage import SessionStore, private_file_lock
from .workflow import Workflow

_INELIGIBLE_STATUSES = frozenset({"allocated", "abandoned"})


class AuthenticatedSessionError(A6AutoError):
    """Stable session recovery error without credentials or cookie material."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


AuthenticatedClientFactory = Callable[[SessionRecord, str], Any]


def session_lock_path(path: Path) -> Path:
    """Return the stable sidecar lock shared by inline and periodic refreshes."""

    resolved = Path(path).expanduser().resolve()
    return resolved.with_name(f".{resolved.name}.auth.lock")


def authenticated_a6_client(
    session_ref: Path | str,
    *,
    expected_user_id: int,
    proxy_url: str,
    client_factory: AuthenticatedClientFactory | None = None,
    mailbox: Any | None = None,
) -> tuple[Any, SessionRecord]:
    """Load, validate, and if needed relogin one existing merchant session."""

    if (
        isinstance(expected_user_id, bool)
        or not isinstance(expected_user_id, int)
        or expected_user_id < 1
    ):
        raise AuthenticatedSessionError("session_user_id_invalid")
    proxy = str(proxy_url or "").strip()
    if not proxy:
        raise AuthenticatedSessionError("session_proxy_missing")

    path = Path(session_ref).expanduser().resolve()
    store = SessionStore(path)
    builder = client_factory or (
        lambda record, fixed_proxy: A6ApiClient(record.origin, proxy_url=fixed_proxy)
    )
    with private_file_lock(session_lock_path(path)):
        record = store.load()
        if str(record.status or "").strip().casefold() in _INELIGIBLE_STATUSES:
            raise AuthenticatedSessionError("session_not_refreshable")
        if record.user_id is None or isinstance(record.user_id, bool):
            raise AuthenticatedSessionError("session_user_id_missing")
        if int(record.user_id) != expected_user_id:
            raise AuthenticatedSessionError("session_user_identity_mismatch")

        client = builder(record, proxy)
        try:
            Workflow(client, mailbox or QQMailbox(), store).ensure_authenticated(record)
        except AuthenticatedSessionError:
            raise
        except A6AutoError as exc:
            raise AuthenticatedSessionError(str(exc)) from exc
        if record.user_id != expected_user_id:
            raise AuthenticatedSessionError("session_user_identity_mismatch")
        return client, record
