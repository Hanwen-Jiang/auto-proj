from pathlib import Path

import pytest

import a6api_auto.storage as storage
from a6api_auto.errors import A6AutoError
from a6api_auto.models import SessionRecord
from a6api_auto.storage import SessionStore, private_file_lock


def record() -> SessionRecord:
    return SessionRecord(
        schema_version=1,
        origin="https://a6api.com",
        email="fixture@qq.com",
        username="fixture",
        password="secret-password",
    )


def test_session_store_is_owner_only_and_round_trips(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "private" / "session.json")
    store.save(record())

    assert store.path.parent.stat().st_mode & 0o777 == 0o700
    assert store.path.stat().st_mode & 0o777 == 0o600
    assert store.load() == record()
    assert store.load().quota_per_unit == 500_000


def test_private_file_lock_is_owner_only(tmp_path: Path) -> None:
    path = tmp_path / "private" / "allocation.lock"

    with private_file_lock(path):
        assert path.is_file()
        assert path.parent.stat().st_mode & 0o777 == 0o700
        assert path.stat().st_mode & 0o777 == 0o600


def test_session_store_round_trips_custom_quota_per_unit(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "session.json")
    current = record()
    current.quota_per_unit = 750_000

    store.save(current)

    assert store.load().quota_per_unit == 750_000


def test_session_store_round_trips_internal_operating_group(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "session.json")
    current = record()
    current.operating_group = "重点组"

    store.save(current)

    assert store.load().operating_group == "重点组"


def test_privileged_atomic_rewrite_preserves_existing_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SessionStore(tmp_path / "session.json")
    store.save(record())
    existing = store.path.stat()
    calls = []

    monkeypatch.setattr(storage.os, "geteuid", lambda: 0)
    monkeypatch.setattr(
        storage.os,
        "fchown",
        lambda descriptor, uid, gid: calls.append((descriptor, uid, gid)),
    )
    current = store.load()
    current.status = "supplier_submitted"

    store.save(current)

    assert [(uid, gid) for _descriptor, uid, gid in calls] == [
        (existing.st_uid, existing.st_gid)
    ]
    assert store.load().status == "supplier_submitted"


@pytest.mark.parametrize("value", [0, -1, True, 500_000.0, "500000"])
def test_session_rejects_invalid_quota_per_unit(value) -> None:
    with pytest.raises(ValueError, match="quota_per_unit_invalid"):
        SessionRecord(
            schema_version=1,
            origin="https://a6api.com",
            email="fixture@qq.com",
            username="fixture",
            password="secret-password",
            quota_per_unit=value,
        )


def test_session_store_rejects_open_permissions(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "session.json")
    store.save(record())
    store.path.chmod(0o644)

    with pytest.raises(A6AutoError, match="session_permissions_too_open"):
        store.load()
