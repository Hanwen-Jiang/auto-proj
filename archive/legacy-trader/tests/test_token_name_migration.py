from __future__ import annotations

import json
from pathlib import Path

import pytest

import a6api_auto.token_name_migration as migration
from a6api_auto.errors import A6AutoError
from a6api_auto.storage import read_private_json, write_private_json
from a6api_auto.token_name_migration import (
    DatabaseTokenIdentity,
    PrivateTokenNameJournal,
    TokenNameMigrationCandidate,
    execute_token_name_migration,
    plan_token_name_migration,
)
from a6api_auto.token_names import is_identity_linked_token_name

OLD_NAME = "merchant-deadbeef01"
NEW_NAME = "z00000000000000000000000"


class FakeDatabase:
    def __init__(self, *, cas_conflict: bool = False) -> None:
        self.name = OLD_NAME
        self.cas_conflict = cas_conflict
        self.calls: list[tuple[str, str]] = []

    def resolve(self, session_ref, user_id, token_name):
        assert user_id == 9517
        assert token_name == OLD_NAME
        return DatabaseTokenIdentity(
            merchant_id=7,
            token_ref_id=20,
            external_token_id=77,
            token_name=OLD_NAME,
        )

    def current_name(self, _candidate):
        return self.name

    def compare_and_swap_name(self, _candidate, expected_name, new_name):
        self.calls.append((expected_name, new_name))
        if self.cas_conflict or self.name != expected_name:
            return False
        self.name = new_name
        return True


class FakeRemote:
    def __init__(
        self,
        *,
        rename_error_after_write: bool = False,
        extra_names: tuple[str, ...] = (),
    ) -> None:
        self.name = OLD_NAME
        self.rename_error_after_write = rename_error_after_write
        self.extra_names = extra_names
        self.rename_calls: list[tuple[int, int, str]] = []

    def get_token(self, user_id, token_id):
        assert (user_id, token_id) == (9517, 77)
        return {"id": 77, "name": self.name}

    def list_tokens(self, user_id):
        assert user_id == 9517
        return [{"id": 77, "name": self.name}] + [
            {"id": 78 + index, "name": name}
            for index, name in enumerate(self.extra_names)
        ]

    def rename_token(self, user_id, token_id, new_name):
        self.rename_calls.append((user_id, token_id, new_name))
        self.name = new_name
        if self.rename_error_after_write and new_name == NEW_NAME:
            raise A6AutoError("token_rename_readback_mismatch")
        return {"id": token_id, "name": new_name}


def session_path(tmp_path: Path) -> Path:
    path = tmp_path / "session-fixture.json"
    write_private_json(
        path,
        {
            "schema_version": 1,
            "origin": "https://a6api.com",
            "email": "fixture@qq.com",
            "username": "fixture",
            "password": "private-password",
            "user_id": 9517,
            "token_name": OLD_NAME,
            "a6api_key": "sk-private-key",
            "cookies": {"session": "private-cookie"},
        },
    )
    return path


def candidate(path: Path) -> TokenNameMigrationCandidate:
    return TokenNameMigrationCandidate(path, 9517, 7, 20, 77, OLD_NAME, NEW_NAME)


def test_plan_uses_exact_database_and_live_token_id(tmp_path: Path) -> None:
    path = session_path(tmp_path)

    planned = plan_token_name_migration(
        path,
        FakeDatabase(),
        FakeRemote(),
        name_factory=lambda: NEW_NAME,
    )

    assert planned == candidate(path)


def test_plan_resamples_names_that_exist_under_same_user(tmp_path: Path) -> None:
    path = session_path(tmp_path)
    generated = iter(("gpt", "Codex"))

    planned = plan_token_name_migration(
        path,
        FakeDatabase(),
        FakeRemote(extra_names=("gpt",)),
        name_factory=lambda: next(generated),
    )

    assert planned.new_name == "Codex"


def test_plan_stops_after_32_name_collisions(tmp_path: Path) -> None:
    path = session_path(tmp_path)

    with pytest.raises(
        A6AutoError,
        match="token_name_migration_name_allocation_failed",
    ):
        plan_token_name_migration(
            path,
            FakeDatabase(),
            FakeRemote(extra_names=("gpt",)),
            name_factory=lambda: "gpt",
        )


def test_dry_run_performs_no_remote_or_local_writes(tmp_path: Path) -> None:
    path = session_path(tmp_path)
    remote = FakeRemote()
    database = FakeDatabase()

    result = execute_token_name_migration(candidate(path), database, remote)

    assert result["state"] == "planned"
    assert remote.rename_calls == []
    assert database.calls == []
    assert read_private_json(path)["token_name"] == OLD_NAME


def test_success_keeps_key_cookie_and_password_and_converges_three_states(tmp_path: Path) -> None:
    path = session_path(tmp_path)
    remote = FakeRemote()
    database = FakeDatabase()
    journal = PrivateTokenNameJournal(tmp_path / "private" / "journal.jsonl")

    result = execute_token_name_migration(
        candidate(path), database, remote, apply=True, journal=journal
    )

    saved = read_private_json(path)
    assert result["state"] == "completed"
    assert remote.name == database.name == saved["token_name"] == NEW_NAME
    assert saved["a6api_key"] == "sk-private-key"
    assert saved["cookies"] == {"session": "private-cookie"}
    assert saved["password"] == "private-password"
    assert journal.path.stat().st_mode & 0o777 == 0o600
    journal_text = journal.path.read_text()
    assert "sk-private-key" not in journal_text
    assert "private-cookie" not in journal_text
    assert "private-password" not in journal_text
    assert not is_identity_linked_token_name(saved["token_name"])


def test_remote_uncertain_failure_is_read_back_and_rolled_back(tmp_path: Path) -> None:
    path = session_path(tmp_path)
    remote = FakeRemote(rename_error_after_write=True)

    with pytest.raises(A6AutoError, match="token_rename_readback_mismatch"):
        execute_token_name_migration(
            candidate(path),
            FakeDatabase(),
            remote,
            apply=True,
            journal=PrivateTokenNameJournal(tmp_path / "journal.jsonl"),
        )

    assert remote.name == OLD_NAME
    assert remote.rename_calls[-1] == (9517, 77, OLD_NAME)
    assert read_private_json(path)["token_name"] == OLD_NAME


def test_session_failure_rolls_remote_back(tmp_path: Path, monkeypatch) -> None:
    path = session_path(tmp_path)
    remote = FakeRemote()
    original = migration._compare_and_swap_session

    def fail_new_name(subject, expected, new):
        if new == NEW_NAME:
            raise A6AutoError("session_write_failed")
        return original(subject, expected, new)

    monkeypatch.setattr(migration, "_compare_and_swap_session", fail_new_name)

    with pytest.raises(A6AutoError, match="session_write_failed"):
        execute_token_name_migration(
            candidate(path),
            FakeDatabase(),
            remote,
            apply=True,
            journal=PrivateTokenNameJournal(tmp_path / "journal.jsonl"),
        )

    assert remote.name == OLD_NAME
    assert read_private_json(path)["token_name"] == OLD_NAME


def test_database_cas_conflict_rolls_session_and_remote_back(tmp_path: Path) -> None:
    path = session_path(tmp_path)
    remote = FakeRemote()
    database = FakeDatabase(cas_conflict=True)

    with pytest.raises(A6AutoError, match="token_name_migration_database_cas_conflict"):
        execute_token_name_migration(
            candidate(path),
            database,
            remote,
            apply=True,
            journal=PrivateTokenNameJournal(tmp_path / "journal.jsonl"),
        )

    assert remote.name == database.name == read_private_json(path)["token_name"] == OLD_NAME
    lines = (tmp_path / "journal.jsonl").read_text().splitlines()
    events = [json.loads(line)["state"] for line in lines]
    assert events[-1] == "rolled_back"
