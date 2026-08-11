from __future__ import annotations

import json
import stat
from pathlib import Path

from a6api_auto.trade_pin_failure_journal import PinFailureJournal


def failure_event(account_id: int, at_unix_nano: int, *, model: str = "gpt-5.6-sol") -> bytes:
    return json.dumps(
        {
            "version": 1,
            "kind": "fixed_merchant_unavailable",
            "model": model,
            "account_id": account_id,
            "status_code": 402,
            "at_unix_nano": at_unix_nano,
        },
        separators=(",", ":"),
    ).encode()


def test_failure_journal_filters_and_preserves_order_before_advancing_complete_lines(
    tmp_path: Path,
) -> None:
    now_ns = 2_000_000_000_000
    journal_path = tmp_path / "pin-request.failures"
    cursor_path = tmp_path / "cursor.json"
    partial = failure_event(45909, now_ns - 1_000_000_000)
    journal_path.write_bytes(
        b"\n".join(
            (
                failure_event(45896, now_ns - 2_000_000_000),
                failure_event(45896, now_ns - 1_000_000_000),
                failure_event(45897, now_ns - 61_000_000_000),
                failure_event(45898, now_ns, model="gpt-5.6-luna"),
                b"not-json",
            )
        )
        + b"\n"
        + partial
    )
    journal = PinFailureJournal(journal_path, cursor_path)

    batch = journal.read(model="gpt-5.6-sol", max_age_seconds=60, now_ns=now_ns)

    assert batch.account_events == (
        (45896, now_ns - 2_000_000_000),
        (45896, now_ns - 1_000_000_000),
    )
    assert batch.records_read == 5
    assert batch.records_valid == 2
    assert batch.records_stale == 1
    assert batch.records_invalid == 2
    assert batch.next_offset == journal_path.stat().st_size - len(partial)

    journal.acknowledge(batch)
    assert stat.S_IMODE(cursor_path.stat().st_mode) == 0o600
    assert journal.read(model="gpt-5.6-sol", now_ns=now_ns).records_read == 0

    with journal_path.open("ab") as target:
        target.write(b"\n")
    resumed = journal.read(model="gpt-5.6-sol", now_ns=now_ns)
    assert resumed.account_ids == (45909,)
    assert resumed.records_read == 1


def test_failure_journal_keeps_three_same_account_events_in_one_batch(tmp_path: Path) -> None:
    now_ns = 4_000_000_000_000
    journal_path = tmp_path / "pin-request.failures"
    journal_path.write_bytes(
        b"\n".join(
            failure_event(45917, now_ns - offset)
            for offset in (3_000_000_000, 2_000_000_000, 1_000_000_000)
        )
        + b"\n"
    )

    batch = PinFailureJournal(journal_path, tmp_path / "cursor.json").read(
        model="gpt-5.6-sol",
        now_ns=now_ns,
    )

    assert batch.account_events == (
        (45917, now_ns - 3_000_000_000),
        (45917, now_ns - 2_000_000_000),
        (45917, now_ns - 1_000_000_000),
    )
    assert batch.records_valid == 3


def test_failure_journal_detects_same_inode_truncation_with_tail_digest(tmp_path: Path) -> None:
    now_ns = 3_000_000_000_000
    journal_path = tmp_path / "pin-request.failures"
    cursor_path = tmp_path / "cursor.json"
    journal_path.write_bytes(failure_event(45896, now_ns) + b"\n")
    journal = PinFailureJournal(journal_path, cursor_path)
    first = journal.read(model="gpt-5.6-sol", now_ns=now_ns)
    journal.acknowledge(first)
    first_inode = journal_path.stat().st_ino

    journal_path.write_bytes(failure_event(45909, now_ns + 1) + b"\n")
    second_inode = journal_path.stat().st_ino
    second = journal.read(model="gpt-5.6-sol", now_ns=now_ns)

    assert second.account_ids == (45909,)
    if second_inode == first_inode:
        assert second.start_offset == 0


def test_missing_failure_journal_is_an_empty_batch(tmp_path: Path) -> None:
    batch = PinFailureJournal(
        tmp_path / "missing.failures",
        tmp_path / "cursor.json",
    ).read(model="gpt-5.6-sol")

    assert batch.source_exists is False
    assert batch.account_ids == ()
    assert batch.records_read == 0
