from __future__ import annotations

import fcntl
import json
import os
import subprocess
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from typing import Any

from scripts.watch_a6_economic_facts import (
    EconomicNotificationBatch,
    build_reconcile_runner,
    run_with_lock,
    watch_notifications,
)


def test_notification_batch_accepts_only_allowlisted_models_and_coalesces() -> None:
    batch = EconomicNotificationBatch(["gpt-5.6-sol"])

    assert batch.add(json.dumps({"model": "gpt-5.6-sol", "event": "quota"})) is True
    assert batch.add(json.dumps({"model": "gpt-5.6-sol", "event": "contract"})) is True
    assert batch.add(json.dumps({"model": "gpt-other", "event": "quota"})) is False
    assert batch.add("not-json") is False
    assert batch.pop() == ("gpt-5.6-sol",)
    assert batch.pop() == ()


def test_reconcile_lock_skips_when_timer_owns_same_file(tmp_path: Path) -> None:
    lock_path = tmp_path / "economic.lock"
    owner = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
    calls: list[str] = []
    try:
        state, returncode = run_with_lock(
            "gpt-5.6-sol",
            lock_path=lock_path,
            reconcile=lambda model: calls.append(model) or 0,
        )
    finally:
        fcntl.flock(owner, fcntl.LOCK_UN)
        os.close(owner)

    assert (state, returncode) == ("busy", None)
    assert calls == []


def test_listener_coalesces_duplicate_notifications_into_one_reconcile(tmp_path: Path) -> None:
    stopped = Event()
    payloads = [
        json.dumps({"model": "gpt-5.6-sol", "event": "quota", "identity": 1}),
        json.dumps({"model": "gpt-5.6-sol", "event": "quota", "identity": 2}),
        json.dumps({"model": "gpt-other", "event": "quota", "identity": 3}),
    ]

    class Cursor:
        def __enter__(self) -> Cursor:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def execute(self, statement: object) -> None:
            assert statement == "LISTEN fixture"

    class Connection:
        closed = False

        def cursor(self) -> Cursor:
            return Cursor()

        def notifies(self, *, timeout: float, stop_after: int) -> Any:
            assert timeout > 0
            assert stop_after == 1
            if payloads:
                return iter((SimpleNamespace(payload=payloads.pop(0)),))
            stopped.set()
            return iter(())

        def close(self) -> None:
            self.closed = True

    connection = Connection()
    calls: list[str] = []
    watch_notifications(
        connection_factory=lambda: connection,
        listen_statement="LISTEN fixture",
        allowed_models=["gpt-5.6-sol"],
        lock_path=tmp_path / "economic.lock",
        reconcile=lambda model: calls.append(model) or 0,
        stopped=stopped,
        debounce_seconds=0.2,
    )

    assert calls == ["gpt-5.6-sol"]
    assert connection.closed is True


def test_reconcile_runner_maps_timeout_to_stable_exit_code(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    script = tmp_path / "reconcile.py"

    def timeout(*_args: object, **_kwargs: object) -> None:
        raise subprocess.TimeoutExpired(["python", str(script)], 12)

    monkeypatch.setattr(subprocess, "run", timeout)
    run = build_reconcile_runner(
        python=Path("/fixture/python"),
        reconcile_script=script,
        timeout_seconds=12,
    )

    assert run("gpt-5.6-sol") == 124


def test_economic_migration_wakes_for_all_decisive_fact_classes() -> None:
    root = Path("migrations/0062_priority1_economic_routing.sql").read_text()
    packaged = Path(
        "src/a6api_auto/data/migrations/0062_priority1_economic_routing.sql"
    ).read_text()

    assert root == packaged
    for trigger in (
        "sub2api_economic_sources_notify",
        "sub2api_source_cost_contracts_notify",
        "sub2api_source_quota_snapshots_notify",
        "sub2api_channel_revenue_snapshots_notify",
        "sub2api_economic_attempts_notify",
        "provider_pool_generations_economic_notify",
        "provider_cost_generations_economic_notify",
        "provider_pool_slots_economic_notify",
    ):
        assert trigger in root
    assert "a6_economic_fact_changed" in root


def test_listener_unit_shares_lock_and_is_bound_to_control_target() -> None:
    unit = Path(
        "deploy/systemd/a6-trader-economic-priority1-listener.service"
    ).read_text()
    timer = Path("deploy/systemd/a6-trader-economic-priority1.timer").read_text()
    target = Path("deploy/systemd/a6-trader-control.target").read_text()

    assert "User=a6trader" in unit
    assert "PartOf=a6-trader-control.target" in unit
    assert "--lock-file /var/lib/a6-trader/economic-priority1.lock" in unit
    assert "--reconcile-timeout-seconds 12" in unit
    assert "OnUnitInactiveSec=15s" in timer
    assert "Wants=a6-trader-economic-priority1-listener.service" in target
    assert "PropagatesStopTo=a6-trader-economic-priority1-listener.service" in target
