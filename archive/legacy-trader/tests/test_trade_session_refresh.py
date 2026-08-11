from __future__ import annotations

import json
from pathlib import Path
from threading import Event, Lock, get_ident

import pytest

from a6api_auto.errors import A6AutoError
from a6api_auto.models import SessionRecord
from a6api_auto.storage import SessionStore
from a6api_auto.trade_session_refresh import TradeSessionRefreshService


class RefreshClient:
    def __init__(
        self,
        user_id: int,
        *,
        cookie_valid: bool = True,
        login_user_id: int | None = None,
        supplier_id: int | None = None,
        supplier_lookup_fails: bool = False,
        supplier_requests: list[dict[str, object]] | None = None,
    ) -> None:
        self.user_id = user_id
        self.cookie_valid = cookie_valid
        self.login_user_id = login_user_id if login_user_id is not None else user_id
        self.supplier_id = supplier_id
        self.supplier_lookup_fails = supplier_lookup_fails
        self.requests = supplier_requests
        self.cookies: dict[str, str] = {}
        self.login_calls = 0

    def import_cookies(self, cookies: dict[str, str]) -> None:
        self.cookies = dict(cookies)

    def export_cookies(self) -> dict[str, str]:
        return dict(self.cookies)

    def self_user(self, user_id: int) -> dict[str, object] | None:
        assert user_id == self.user_id
        if not self.cookie_valid:
            return None
        return {"id": self.user_id, "username": f"user-{self.user_id}"}

    def login(self, _username: str, _password: str) -> dict[str, object]:
        self.login_calls += 1
        self.cookies = {"session": f"refreshed-{self.user_id}"}
        return {"id": self.login_user_id, "username": f"user-{self.login_user_id}"}

    def supplier_me(self, user_id: int) -> dict[str, int]:
        assert user_id == self.user_id
        if self.supplier_lookup_fails or self.supplier_id is None:
            raise A6AutoError("supplier_self_failed")
        return {"id": self.supplier_id}

    def supplier_requests(self, user_id: int) -> list[dict[str, object]]:
        assert user_id == self.user_id
        if self.requests is None:
            raise A6AutoError("supplier_requests_failed")
        return self.requests


def session_file(
    path: Path,
    user_id: int | None,
    *,
    status: str,
    supplier_id: int | None = None,
) -> Path:
    SessionStore(path).save(
        SessionRecord(
            schema_version=1,
            origin="https://fixture.invalid",
            email=f"user-{user_id or 0}@fixture.invalid",
            username=f"user-{user_id or 0}",
            password=f"password-secret-{user_id or 0}",
            user_id=user_id,
            cookies={"session": f"cookie-secret-{user_id or 0}"},
            status=status,
            supplier_id=supplier_id,
        )
    )
    return path


def test_refresh_pass_isolates_failures_and_synchronizes_tombstones(tmp_path: Path) -> None:
    supplier = session_file(
        tmp_path / "session-1.json",
        1,
        status="authenticated",
        supplier_id=81,
    )
    ordinary = session_file(tmp_path / "session-2.json", 2, status="authenticated")
    allocated = session_file(tmp_path / "session-3.json", None, status="allocated")
    abandoned = session_file(tmp_path / "session-4.json", 4, status="abandoned")
    missing_proxy = session_file(tmp_path / "session-5.json", 5, status="authenticated")
    missing_user = session_file(tmp_path / "session-6.json", None, status="registered")
    clients: dict[int, RefreshClient] = {}
    metadata: list[tuple[str, str, str | None]] = []
    failures: list[tuple[str, str]] = []

    def client_factory(record: SessionRecord, _proxy: str) -> RefreshClient:
        user_id = int(record.user_id or 0)
        client = RefreshClient(
            user_id,
            cookie_valid=user_id != 1,
            supplier_id=81 if user_id == 1 else None,
        )
        clients[user_id] = client
        return client

    service = TradeSessionRefreshService(
        client_factory=client_factory,
        metadata_sink=lambda path, record, proxy: metadata.append(
            (path.name, record.status, proxy)
        ),
        failure_sink=lambda path, error: failures.append((path.name, error)),
        mailbox=object(),
    )

    result = service.refresh_paths(
        (missing_user, missing_proxy, abandoned, ordinary, allocated, supplier),
        {
            1: "socks5://proxy-one.invalid:1080",
            2: "socks5://proxy-two.invalid:1080",
            4: "socks5://proxy-four.invalid:1080",
        },
    )

    assert result == {
        "success": False,
        "sessions_found": 6,
        "eligible": 3,
        "refreshed": 2,
        "supplier_active": 1,
        "cookie_valid": 1,
        "relogged": 1,
        "tombstoned": 1,
        "skipped": 2,
        "failed": 1,
        "metadata_synced": 3,
        "errors": {"session_refresh_proxy_missing": 1},
    }
    assert clients[1].login_calls == 1
    assert clients[2].login_calls == 0
    assert 4 not in clients
    assert SessionStore(supplier).load().cookies == {"session": "refreshed-1"}
    assert SessionStore(supplier).load().status == "supplier_submitted"
    assert SessionStore(ordinary).load().status == "authenticated"
    assert metadata == [
        ("session-1.json", "supplier_submitted", "socks5://proxy-one.invalid:1080"),
        ("session-2.json", "authenticated", "socks5://proxy-two.invalid:1080"),
        ("session-4.json", "abandoned", None),
    ]
    assert failures == [("session-5.json", "session_refresh_proxy_missing")]
    rendered = json.dumps(result)
    assert "password-secret" not in rendered
    assert "cookie-secret" not in rendered
    assert "proxy-one" not in rendered


def test_refresh_promotes_new_supplier_profile_without_request_id(tmp_path: Path) -> None:
    path = session_file(tmp_path / "session-9.json", 9, status="authenticated")
    client = RefreshClient(9, supplier_id=99)
    synced: list[SessionRecord] = []
    service = TradeSessionRefreshService(
        client_factory=lambda _record, _proxy: client,
        metadata_sink=lambda _path, record, _proxy: synced.append(record),
        mailbox=object(),
    )

    result = service.refresh_paths((path,), {9: "http://127.0.0.1:23089"})

    saved = SessionStore(path).load()
    assert result["success"] is True
    assert result["supplier_active"] == 1
    assert saved.supplier_id == 99
    assert saved.request_id is None
    assert saved.status == "supplier_submitted"
    assert synced[0].supplier_id == 99


def test_refresh_rejects_login_identity_mismatch_and_continues(tmp_path: Path) -> None:
    first = session_file(tmp_path / "session-1.json", 1, status="authenticated")
    second = session_file(tmp_path / "session-2.json", 2, status="authenticated")
    synced: list[int] = []

    def client_factory(record: SessionRecord, _proxy: str) -> RefreshClient:
        user_id = int(record.user_id or 0)
        if user_id == 1:
            return RefreshClient(1, cookie_valid=False, login_user_id=999)
        return RefreshClient(2)

    service = TradeSessionRefreshService(
        client_factory=client_factory,
        metadata_sink=lambda _path, record, _proxy: synced.append(int(record.user_id or 0)),
        mailbox=object(),
    )

    result = service.refresh_paths(
        (first, second),
        {1: "http://127.0.0.1:23081", 2: "http://127.0.0.1:23082"},
    )

    assert result["refreshed"] == 1
    assert result["failed"] == 1
    assert result["errors"] == {"session_user_identity_mismatch": 1}
    assert synced == [2]
    assert SessionStore(first).load().cookies == {"session": "cookie-secret-1"}


def test_existing_supplier_is_not_downgraded_when_profile_lookup_fails(tmp_path: Path) -> None:
    path = session_file(
        tmp_path / "session-7.json",
        7,
        status="supplier_submitted",
        supplier_id=77,
    )
    service = TradeSessionRefreshService(
        client_factory=lambda _record, _proxy: RefreshClient(
            7,
            supplier_id=77,
            supplier_lookup_fails=True,
        ),
        metadata_sink=lambda *_args: None,
        mailbox=object(),
    )

    result = service.refresh_paths((path,), {7: "http://127.0.0.1:23087"})

    assert result["failed"] == 1
    assert result["errors"] == {"supplier_self_failed": 1}
    assert result["cookie_valid"] == 1
    assert result["relogged"] == 0
    saved = SessionStore(path).load()
    assert saved.status == "supplier_submitted"
    assert saved.supplier_id == 77


def test_refresh_marks_explicitly_rejected_supplier_application(tmp_path: Path) -> None:
    path = session_file(
        tmp_path / "session-7.json",
        7,
        status="supplier_submitted",
        supplier_id=77,
    )
    store = SessionStore(path)
    submitted = store.load()
    submitted.request_id = 707
    store.save(submitted)
    synced: list[SessionRecord] = []
    service = TradeSessionRefreshService(
        client_factory=lambda _record, _proxy: RefreshClient(
            7,
            supplier_id=77,
            supplier_lookup_fails=True,
            supplier_requests=[{"id": 707, "status": 2}],
        ),
        metadata_sink=lambda _path, record, _proxy: synced.append(record),
        mailbox=object(),
    )

    result = service.refresh_paths((path,), {7: "http://127.0.0.1:23087"})

    assert result["success"] is True
    assert result["refreshed"] == 1
    assert result["supplier_active"] == 0
    assert result["errors"] == {}
    saved = SessionStore(path).load()
    assert saved.status == "supplier_rejected"
    assert saved.supplier_id is None
    assert saved.request_id == 707
    assert synced[0].status == "supplier_rejected"


def test_refresh_preserves_supplier_for_unrelated_rejected_request(tmp_path: Path) -> None:
    path = session_file(
        tmp_path / "session-7.json",
        7,
        status="supplier_submitted",
        supplier_id=77,
    )
    store = SessionStore(path)
    submitted = store.load()
    submitted.request_id = 707
    store.save(submitted)
    service = TradeSessionRefreshService(
        client_factory=lambda _record, _proxy: RefreshClient(
            7,
            supplier_id=77,
            supplier_lookup_fails=True,
            supplier_requests=[{"id": 708, "status": 2}],
        ),
        metadata_sink=lambda *_args: None,
        mailbox=object(),
    )

    result = service.refresh_paths((path,), {7: "http://127.0.0.1:23087"})

    assert result["errors"] == {"supplier_self_failed": 1}
    saved = SessionStore(path).load()
    assert saved.status == "supplier_submitted"
    assert saved.supplier_id == 77


def test_refresh_preserves_rejected_status_on_later_passes(tmp_path: Path) -> None:
    path = session_file(
        tmp_path / "session-7.json",
        7,
        status="supplier_rejected",
    )
    service = TradeSessionRefreshService(
        client_factory=lambda _record, _proxy: RefreshClient(
            7,
            supplier_lookup_fails=True,
        ),
        metadata_sink=lambda *_args: None,
        mailbox=object(),
    )

    result = service.refresh_paths((path,), {7: "http://127.0.0.1:23087"})

    assert result["success"] is True
    assert result["supplier_active"] == 0
    assert SessionStore(path).load().status == "supplier_rejected"


def test_refresh_bounds_network_concurrency_then_syncs_metadata_in_path_order(
    tmp_path: Path,
) -> None:
    paths = tuple(
        session_file(tmp_path / f"session-{user_id}.json", user_id, status="authenticated")
        for user_id in range(1, 5)
    )
    network_gate = Event()
    state_lock = Lock()
    active = 0
    peak = 0
    network_done: set[int] = set()
    network_threads: set[int] = set()
    metadata: list[tuple[str, int]] = []

    class ConcurrentRefreshClient(RefreshClient):
        def self_user(self, user_id: int) -> dict[str, object] | None:
            nonlocal active, peak
            with state_lock:
                active += 1
                peak = max(peak, active)
                network_threads.add(get_ident())
                if active == 2:
                    network_gate.set()
            assert network_gate.wait(timeout=2)
            try:
                return super().self_user(user_id)
            finally:
                with state_lock:
                    active -= 1
                    network_done.add(user_id)

    def metadata_sink(path: Path, _record: SessionRecord, _proxy: str | None) -> None:
        assert network_done == {1, 2, 3, 4}
        metadata.append((path.name, get_ident()))

    service = TradeSessionRefreshService(
        client_factory=lambda record, _proxy: ConcurrentRefreshClient(int(record.user_id or 0)),
        metadata_sink=metadata_sink,
        mailbox=object(),
        max_workers=2,
    )

    result = service.refresh_paths(
        reversed(paths),
        {user_id: f"http://127.0.0.1:{23080 + user_id}" for user_id in range(1, 5)},
    )

    assert result["success"] is True
    assert result["refreshed"] == 4
    assert result["cookie_valid"] == 4
    assert result["relogged"] == 0
    assert peak == 2
    assert len(network_threads) == 2
    assert metadata == [
        ("session-1.json", get_ident()),
        ("session-2.json", get_ident()),
        ("session-3.json", get_ident()),
        ("session-4.json", get_ident()),
    ]
    assert all(
        (tmp_path / f".session-{user_id}.json.auth.lock").is_file()
        for user_id in range(1, 5)
    )


def test_metadata_failure_does_not_block_later_path(tmp_path: Path) -> None:
    first = session_file(tmp_path / "session-1.json", 1, status="authenticated")
    second = session_file(tmp_path / "session-2.json", 2, status="authenticated")
    metadata_calls: list[str] = []

    def metadata_sink(path: Path, _record: SessionRecord, _proxy: str | None) -> None:
        metadata_calls.append(path.name)
        if path == first:
            raise RuntimeError("database fixture failure")

    service = TradeSessionRefreshService(
        client_factory=lambda record, _proxy: RefreshClient(int(record.user_id or 0)),
        metadata_sink=metadata_sink,
        mailbox=object(),
        max_workers=2,
    )

    result = service.refresh_paths(
        (second, first),
        {1: "http://127.0.0.1:23081", 2: "http://127.0.0.1:23082"},
    )

    assert metadata_calls == ["session-1.json", "session-2.json"]
    assert result["refreshed"] == 1
    assert result["metadata_synced"] == 1
    assert result["failed"] == 1
    assert result["cookie_valid"] == 2
    assert result["errors"] == {"session_refresh_metadata_sync_failed": 1}


@pytest.mark.parametrize("max_workers", [False, 0, 17])
def test_refresh_rejects_invalid_max_workers(max_workers: int) -> None:
    with pytest.raises(ValueError, match="session refresh max workers invalid"):
        TradeSessionRefreshService(
            client_factory=lambda *_args: None,
            metadata_sink=lambda *_args: None,
            mailbox=object(),
            max_workers=max_workers,
        )


def test_session_refresh_systemd_units_use_shared_blocking_scan_lock() -> None:
    service = Path("deploy/systemd/a6-trader-session-refresh.service").read_text()
    timer = Path("deploy/systemd/a6-trader-session-refresh.timer").read_text()

    assert "flock --exclusive /var/lib/a6-trader/network-scan.lock" in service
    assert "trade-refresh-sessions-once" in service
    assert "--max-workers 8" in service
    assert "SuccessExitStatus=2" in service
    assert "OnUnitActiveSec=7min" in timer
    assert "RandomizedDelaySec=3min" in timer
    assert "Persistent=true" in timer
