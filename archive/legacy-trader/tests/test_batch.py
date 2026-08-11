from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from a6api_auto.batch import BatchConfig, ProxyDeck, SlowBatchRunner, _proxy_fingerprint
from a6api_auto.errors import A6AutoError
from a6api_auto.storage import write_private_json
from a6api_auto.templates import load_templates
from a6api_auto.upstream import UpstreamSelection


def qq_file(path: Path, count: int = 4) -> Path:
    path.write_text("\n".join(f"fixture{i}@qq.com" for i in range(count)) + "\n")
    return path


def config(tmp_path: Path, **changes) -> BatchConfig:
    values = {
        "qq_file": qq_file(tmp_path / "qq.txt"),
        "state_dir": tmp_path / "private",
        "state_file": tmp_path / "private" / "batches" / "batch.json",
        "proxy_urls": ("socks5://user:pass@proxy1.test:1000", "http://proxy2.test:2000"),
        "upstream_base_url": "https://upstream.example/v1",
        "upstream_api_key": "sk-fixture",
        "target_count": 2,
        "min_interval_seconds": 10800,
        "max_interval_seconds": 28800,
        "per_account_retries": 2,
    }
    values.update(changes)
    return BatchConfig(**values)


@pytest.mark.parametrize(
    "changes,error",
    [
        (
            {"min_interval_seconds": 1799},
            "batch_interval_must_be_30_minutes_to_8_hours",
        ),
        (
            {"max_interval_seconds": 28801},
            "batch_interval_must_be_30_minutes_to_8_hours",
        ),
        ({"proxy_urls": ()}, "batch_proxy_pool_required"),
        (
            {"target_count": 3},
            "batch_proxy_pool_smaller_than_target",
        ),
    ],
)
def test_batch_config_rejects_invalid_limits(tmp_path: Path, changes, error: str) -> None:
    with pytest.raises(A6AutoError, match=error):
        config(tmp_path, **changes).validate()


def test_focus_register_unit_caps_process_restart_failures() -> None:
    service = Path("deploy/systemd/a6-trader-focus-register-10.service").read_text()
    unit, service_section = service.split("[Service]", maxsplit=1)

    assert "StartLimitIntervalSec=10min" in unit
    assert "StartLimitBurst=3" in unit
    assert "Restart=on-failure" in service_section
    assert "RestartSec=60" in service_section


def test_proxy_deck_uses_every_proxy_before_reuse() -> None:
    proxies = ("proxy-a", "proxy-b", "proxy-c")
    deck = ProxyDeck(proxies)
    first_cycle = [deck.next() for _ in proxies]
    next_proxy = deck.next()

    assert set(first_cycle) == set(proxies)
    assert next_proxy != first_cycle[-1]


def test_proxy_deck_honors_persisted_fingerprint_after_restart() -> None:
    proxies = ("proxy-a", "proxy-b")
    selected = ProxyDeck(proxies).next(_proxy_fingerprint("proxy-a"))

    assert selected == "proxy-b"


def test_runner_never_selects_a_globally_reserved_proxy(tmp_path: Path, monkeypatch) -> None:
    clock = Clock()
    proxies = ("proxy-a", "proxy-b", "proxy-c")
    selected: list[str] = []
    config_value = config(
        tmp_path,
        proxy_urls=proxies,
        reserved_proxy_fingerprints=(_proxy_fingerprint("proxy-a"),),
    )
    monkeypatch.setattr("a6api_auto.batch.Workflow.execute", successful_execute)
    runner = SlowBatchRunner(
        config_value,
        now=clock.now,
        sleep=clock.sleep,
        random_interval=lambda _low, _high: 10800,
        client_factory=lambda _origin, proxy: selected.append(proxy) or object(),
        mailbox_factory=lambda: object(),
    )

    result = runner.run()

    assert result["completed"] == 2
    assert set(selected) == {"proxy-b", "proxy-c"}


class Clock:
    def __init__(self, value: float = 1_800_000_000.0) -> None:
        self.value = value
        self.sleeps: list[float] = []

    def now(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.value += seconds


def successful_execute(self, record, template, **_kwargs):
    return {"user_id": 100, "request_id": int(template.template_id[-3:])}


def test_runner_persists_schedule_and_completes_serially(tmp_path: Path, monkeypatch) -> None:
    clock = Clock()
    events = []
    seen_state = []
    proxy_bindings = []

    def execute(self, record, template, **kwargs):
        state = json.loads(config_value.state_file.read_text())
        seen_state.append((state["pending_session"], state["last_proxy_fingerprint"]))
        return successful_execute(self, record, template, **kwargs)

    config_value = config(tmp_path)
    monkeypatch.setattr("a6api_auto.batch.Workflow.execute", execute)
    runner = SlowBatchRunner(
        config_value,
        now=clock.now,
        sleep=clock.sleep,
        random_interval=lambda low, high: 14400,
        client_factory=lambda _origin, _proxy: object(),
        mailbox_factory=lambda: object(),
        proxy_binder=lambda user_id, proxy: proxy_bindings.append((user_id, proxy)),
        event=events.append,
    )

    result = runner.run()
    state = json.loads(config_value.state_file.read_text())

    assert result["completed"] == 2
    assert clock.sleeps == [14400]
    assert all(10800 <= delay <= 28800 for delay in clock.sleeps)
    assert all(session and fingerprint for session, fingerprint in seen_state)
    assert state["next_run_at"] == 0
    assert "socks5://" not in config_value.state_file.read_text()
    assert "http://proxy2" not in config_value.state_file.read_text()
    assert any(event["event"] == "waiting" for event in events)
    assert len(proxy_bindings) == 2
    assert len({proxy for _user_id, proxy in proxy_bindings}) == 2


def test_new_batch_excludes_template_ids_already_used_by_sessions(tmp_path: Path) -> None:
    config_value = config(tmp_path)
    sessions = config_value.state_dir / "sessions"
    sessions.mkdir(parents=True)
    used = sessions / "session-existing.json"
    used.write_text('{"template_id":"merchant-001"}\n')
    used.chmod(0o600)

    state = SlowBatchRunner(config_value)._new_state()

    assert "merchant-001" not in state["template_ids"]


def test_new_batch_excludes_template_ids_frozen_by_another_batch(tmp_path: Path) -> None:
    config_value = config(tmp_path)
    other = config_value.state_dir / "batches" / "other.json"
    other.parent.mkdir(parents=True)
    other.write_text('{"template_ids":["merchant-001","merchant-002"]}\n')
    other.chmod(0o600)

    state = SlowBatchRunner(config_value)._new_state()

    assert set(state["template_ids"]).isdisjoint({"merchant-001", "merchant-002"})


def test_registration_name_gate_runs_before_session_allocation(tmp_path: Path, monkeypatch) -> None:
    config_value = config(tmp_path, target_count=1)
    runner = SlowBatchRunner(config_value)
    state = runner._new_state()
    bad_template = replace(load_templates()[0], supplier_name="赣新156")
    monkeypatch.setattr(
        "a6api_auto.batch.choose_template",
        lambda _template_id, **_kwargs: bad_template,
    )

    with pytest.raises(A6AutoError, match="^batch_template_naming_rejected$"):
        runner._session(state)

    assert not (config_value.state_dir / "sessions").exists()


def test_resume_waits_only_remaining_schedule(tmp_path: Path, monkeypatch) -> None:
    clock = Clock()
    config_value = config(tmp_path, target_count=1)
    monkeypatch.setattr("a6api_auto.batch.Workflow.execute", successful_execute)
    runner = SlowBatchRunner(
        config_value,
        now=clock.now,
        sleep=clock.sleep,
        client_factory=lambda _origin, _proxy: object(),
        mailbox_factory=lambda: object(),
    )
    state = runner._new_state()
    state["next_run_at"] = clock.now() + 14400
    runner.store.save(state)
    clock.value += 600

    runner.run()

    assert clock.sleeps == [13800]


def test_static_runner_accepts_state_created_before_upstream_fingerprint(
    tmp_path: Path, monkeypatch
) -> None:
    clock = Clock()
    config_value = config(tmp_path, target_count=1)
    monkeypatch.setattr("a6api_auto.batch.Workflow.execute", successful_execute)
    runner = SlowBatchRunner(
        config_value,
        now=clock.now,
        sleep=clock.sleep,
        client_factory=lambda _origin, _proxy: object(),
        mailbox_factory=lambda: object(),
    )
    state = runner._new_state()
    state.pop("upstream_config_fingerprint")
    state.pop("upstream_group_name")
    state.pop("pending_upstream")
    runner.store.save(state)

    result = runner.run()

    assert result["completed"] == 1


def test_static_runner_accepts_state_created_before_distribution_schedule(
    tmp_path: Path,
) -> None:
    config_value = config(tmp_path)
    runner = SlowBatchRunner(config_value)
    state = runner._new_state()
    state.pop("distribution_window_seconds")
    state.pop("registration_schedule")
    runner.store.save(state)

    loaded = runner._load_or_create()

    assert loaded["target_count"] == 2


def test_failed_attempt_retries_pending_session_with_same_proxy(
    tmp_path: Path, monkeypatch
) -> None:
    clock = Clock()
    attempts = []

    def execute(self, record, template, **_kwargs):
        attempts.append((record.email, self.client))
        if len(attempts) == 1:
            raise A6AutoError("transient_fixture")
        return {"user_id": 100, "request_id": 200}

    monkeypatch.setattr("a6api_auto.batch.Workflow.execute", execute)
    runner = SlowBatchRunner(
        config(tmp_path, target_count=1),
        now=clock.now,
        sleep=clock.sleep,
        random_interval=lambda low, high: 10800,
        client_factory=lambda _origin, proxy: proxy,
        mailbox_factory=lambda: object(),
    )

    result = runner.run()

    assert result["completed"] == 1
    assert result["failures"] == 1
    assert clock.sleeps == [10800]
    assert attempts[0][0] == attempts[1][0]
    assert attempts[0][1] == attempts[1][1]


def test_allocated_session_rotates_proxy_after_consecutive_transport_failures(
    tmp_path: Path, monkeypatch
) -> None:
    clock = Clock()
    attempts = []

    def execute(self, record, template, **_kwargs):
        attempts.append((record.email, self.client))
        if len(attempts) <= 2:
            raise A6AutoError("a6_transport_failed")
        return {"user_id": 100, "request_id": 200}

    monkeypatch.setattr("a6api_auto.batch.Workflow.execute", execute)
    runner = SlowBatchRunner(
        config(
            tmp_path,
            target_count=1,
            per_account_retries=3,
            proxy_urls=("proxy-a", "proxy-b", "proxy-c"),
        ),
        now=clock.now,
        sleep=clock.sleep,
        random_interval=lambda low, high: 10800,
        client_factory=lambda _origin, proxy: proxy,
        mailbox_factory=lambda: object(),
    )

    result = runner.run()
    state = json.loads(runner.config.state_file.read_text())

    assert result["completed"] == 1
    assert result["failures"] == 2
    assert result["abandoned_sessions"] == 0
    assert clock.sleeps == [60, 60]
    assert attempts[0] == attempts[1]
    assert attempts[2][0] == attempts[0][0]
    assert attempts[2][1] != attempts[0][1]
    assert len(state["allocated_proxy_fingerprints"]) == 2


def test_non_retryable_email_error_abandons_session_immediately(
    tmp_path: Path, monkeypatch
) -> None:
    clock = Clock()
    seen = []

    def execute(self, record, template, **_kwargs):
        seen.append(record.email)
        if len(seen) == 1:
            raise A6AutoError("email_already_registered")
        return {"user_id": 100, "request_id": 200}

    monkeypatch.setattr("a6api_auto.batch.Workflow.execute", execute)
    runner = SlowBatchRunner(
        config(tmp_path, target_count=1),
        now=clock.now,
        sleep=clock.sleep,
        random_interval=lambda low, high: 10800,
        client_factory=lambda _origin, proxy: proxy,
        mailbox_factory=lambda: object(),
    )

    result = runner.run()

    assert result["completed"] == 1
    assert result["failures"] == 1
    assert result["abandoned_sessions"] == 1
    assert clock.sleeps == [10800]
    assert len(set(seen)) == 2


def test_unavailable_mailbox_is_abandoned_after_short_cooldown(tmp_path: Path, monkeypatch) -> None:
    clock = Clock()
    attempts = 0

    def execute(self, record, template, **_kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise A6AutoError("qq_mailbox_unavailable")
        return {"user_id": 100, "request_id": 200}

    monkeypatch.setattr("a6api_auto.batch.Workflow.execute", execute)
    config_value = config(tmp_path, target_count=1)
    result = SlowBatchRunner(
        config_value,
        now=clock.now,
        sleep=clock.sleep,
        client_factory=lambda _origin, proxy: proxy,
        mailbox_factory=lambda: object(),
    ).run()

    state = json.loads(config_value.state_file.read_text())
    abandoned = Path(state["abandoned_sessions"][0])
    assert result["completed"] == 1
    assert result["abandoned_sessions"] == 1
    assert clock.sleeps == [5.0]
    assert json.loads(abandoned.read_text())["status"] == "abandoned"


def test_dynamic_upstream_selection_is_persisted_without_key(tmp_path: Path, monkeypatch) -> None:
    clock = Clock()
    selections = []
    secret_key = "sk-dynamic-fixture-secret"

    def select(pending):
        selections.append(pending)
        return UpstreamSelection("https://domain-a.test", secret_key, "key-17")

    def execute(self, record, template, **kwargs):
        state_text = config_value.state_file.read_text()
        state = json.loads(state_text)
        assert state["pending_upstream"] == {
            "base_url": "https://domain-a.test",
            "key_ref": "key-17",
        }
        assert secret_key not in state_text
        assert kwargs["upstream_base_url"] == "https://domain-a.test"
        assert kwargs["upstream_api_key"] == secret_key
        return {"user_id": 100, "request_id": 200}

    config_value = config(
        tmp_path,
        target_count=1,
        upstream_base_url="",
        upstream_api_key="",
        upstream_base_urls=("https://domain-a.test", "https://domain-b.test"),
        upstream_group_name="TPA6API",
    )
    monkeypatch.setattr("a6api_auto.batch.Workflow.execute", execute)

    result = SlowBatchRunner(
        config_value,
        now=clock.now,
        sleep=clock.sleep,
        client_factory=lambda _origin, _proxy: object(),
        mailbox_factory=lambda: object(),
        upstream_selector=select,
    ).run()

    state_text = config_value.state_file.read_text()
    assert result["completed"] == 1
    assert selections == [None]
    assert secret_key not in state_text
    assert json.loads(state_text)["completed"][0]["upstream_key_ref"] == "key-17"


def test_workbook_focus_batch_freezes_strict_templates_and_internal_group(
    tmp_path: Path,
) -> None:
    config_value = config(
        tmp_path,
        template_catalog="workbook",
        operating_group="重点组",
        unique_upstream_base_urls=True,
        upstream_base_url="",
        upstream_api_key="",
        upstream_base_urls=("https://domain-a.test", "https://domain-b.test"),
        upstream_group_name="TPA6API",
    )

    state = SlowBatchRunner(
        config_value,
        upstream_selector=lambda _pending: UpstreamSelection(
            "https://domain-a.test", "sk-fixture", "17"
        ),
    )._new_state()

    assert state["template_catalog"] == "workbook"
    assert state["template_catalog_sha256"] == (
        "908f922bfd50cac26f7addb4bdb14fd23c42761002988ea71f1bb71d1ff1a078"
    )
    assert state["operating_group"] == "重点组"
    assert state["unique_upstream_base_urls"] is True
    assert len(state["template_ids"]) == 2
    assert all(value.startswith("workbook-s") for value in state["template_ids"])


def test_prepare_freezes_templates_without_allocating_sessions_or_upstreams(
    tmp_path: Path,
) -> None:
    config_value = config(
        tmp_path,
        template_catalog="workbook",
        operating_group="重点组",
        unique_upstream_base_urls=True,
        upstream_base_url="",
        upstream_api_key="",
        upstream_base_urls=("https://domain-a.test", "https://domain-b.test"),
        upstream_group_name="TPA6API",
    )

    result = SlowBatchRunner(config_value).prepare()
    state = json.loads(config_value.state_file.read_text())

    assert result["prepared"] is True
    assert result["operating_group"] == "重点组"
    assert len(result["templates"]) == 2
    assert [item["template_id"] for item in result["templates"]] == state["template_ids"]
    assert state["pending_session"] == ""
    assert state["allocated_upstream_base_urls"] == []
    assert not (config_value.state_dir / "sessions").exists()
    assert config_value.state_file.stat().st_mode & 0o077 == 0


def test_focus_batch_reserves_a_different_base_url_for_every_account(
    tmp_path: Path,
    monkeypatch,
) -> None:
    clock = Clock()
    selected = iter(
        (
            "https://domain-a.test",
            "https://domain-a.test",
            "https://domain-b.test",
        )
    )
    observed: list[str] = []

    def select(pending):
        base_url = str(pending.get("base_url")) if pending else next(selected)
        return UpstreamSelection(base_url, "sk-secret", "17")

    def execute(self, record, template, **kwargs):
        observed.append(kwargs["upstream_base_url"])
        assert record.operating_group == "重点组"
        return {"user_id": 100 + len(observed), "request_id": 200 + len(observed)}

    config_value = config(
        tmp_path,
        template_catalog="workbook",
        operating_group="重点组",
        unique_upstream_base_urls=True,
        upstream_base_url="",
        upstream_api_key="",
        upstream_base_urls=("https://domain-a.test", "https://domain-b.test"),
        upstream_group_name="TPA6API",
    )
    monkeypatch.setattr("a6api_auto.batch.Workflow.execute", execute)

    result = SlowBatchRunner(
        config_value,
        now=clock.now,
        sleep=clock.sleep,
        random_interval=lambda _low, _high: 10800,
        client_factory=lambda _origin, _proxy: object(),
        mailbox_factory=lambda: object(),
        upstream_selector=select,
    ).run()
    state = json.loads(config_value.state_file.read_text())

    assert result["completed"] == 2
    assert result["operating_group"] == "重点组"
    assert observed == ["https://domain-a.test", "https://domain-b.test"]
    assert state["allocated_upstream_base_urls"] == observed
    assert {item["operating_group"] for item in state["completed"]} == {"重点组"}
    assert {
        json.loads(Path(item["session_file"]).read_text())["operating_group"]
        for item in state["completed"]
    } == {"重点组"}


def test_distribution_schedule_has_ten_persisted_slots_inside_window(tmp_path: Path) -> None:
    clock = Clock()
    config_value = config(
        tmp_path,
        target_count=10,
        proxy_urls=tuple(f"proxy-{index}" for index in range(10)),
        min_interval_seconds=1800,
        max_interval_seconds=5400,
        distribution_window_seconds=36000,
    )
    runner = SlowBatchRunner(
        config_value,
        now=clock.now,
        random_interval=lambda low, high: (low + high) / 2,
    )

    prepared = runner.prepare()
    first_state = json.loads(config_value.state_file.read_text())
    second_state = SlowBatchRunner(config_value, now=clock.now).prepare()
    schedule = first_state["registration_schedule"]

    assert len(schedule) == 10
    assert clock.now() < schedule[0] < schedule[-1] < clock.now() + 36000
    assert all(
        current - previous >= 1800
        for previous, current in zip(schedule, schedule[1:], strict=False)
    )
    assert prepared["registration_schedule"] == second_state["registration_schedule"]
    assert first_state == json.loads(config_value.state_file.read_text())


def test_distribution_runner_advances_to_persisted_success_slots(
    tmp_path: Path,
    monkeypatch,
) -> None:
    clock = Clock()
    config_value = config(
        tmp_path,
        min_interval_seconds=1800,
        max_interval_seconds=5400,
        distribution_window_seconds=7200,
    )
    monkeypatch.setattr("a6api_auto.batch.Workflow.execute", successful_execute)
    runner = SlowBatchRunner(
        config_value,
        now=clock.now,
        sleep=clock.sleep,
        random_interval=lambda low, high: (low + high) / 2,
        client_factory=lambda _origin, _proxy: object(),
        mailbox_factory=lambda: object(),
    )

    result = runner.run()

    assert result["completed"] == 2
    assert clock.sleeps == [1800, 3600]


def test_shared_proxy_reservations_prevent_cross_batch_reuse(tmp_path: Path) -> None:
    ledger = tmp_path / "private" / "proxy-reservations.json"
    proxies = ("proxy-a", "proxy-b")
    first_config = config(
        tmp_path,
        target_count=1,
        proxy_urls=proxies,
        state_file=tmp_path / "private" / "batches" / "first.json",
        proxy_reservation_file=ledger,
    )
    second_config = replace(
        first_config,
        state_file=tmp_path / "private" / "batches" / "second.json",
    )
    first = SlowBatchRunner(first_config)
    second = SlowBatchRunner(second_config)

    _first_proxy, first_fingerprint = first._proxy(first._new_state())
    _second_proxy, second_fingerprint = second._proxy(second._new_state())

    assert first_fingerprint != second_fingerprint
    reservations = json.loads(ledger.read_text())["reservations"]
    assert len(reservations) == 2


def test_shared_upstream_reservations_force_second_batch_to_reselect(
    tmp_path: Path,
) -> None:
    ledger = tmp_path / "private" / "upstream-reservations.json"
    domains = ("https://domain-a.test", "https://domain-b.test")
    first_config = config(
        tmp_path,
        target_count=1,
        upstream_base_url="",
        upstream_api_key="",
        upstream_base_urls=domains,
        upstream_group_name="TPA6API",
        unique_upstream_base_urls=True,
        upstream_reservation_file=ledger,
        state_file=tmp_path / "private" / "batches" / "first.json",
    )
    second_config = replace(
        first_config,
        state_file=tmp_path / "private" / "batches" / "second.json",
    )
    first = SlowBatchRunner(
        first_config,
        upstream_selector=lambda _pending: UpstreamSelection(domains[0], "sk-first", "first"),
    )
    selections = iter(domains)
    second = SlowBatchRunner(
        second_config,
        upstream_selector=lambda _pending: UpstreamSelection(
            next(selections), "sk-second", "second"
        ),
    )

    first_selection = first._upstream(first._new_state())
    second_selection = second._upstream(second._new_state())

    assert first_selection.base_url == domains[0]
    assert second_selection.base_url == domains[1]


def test_existing_allocations_are_reclaimed_when_batch_resumes(tmp_path: Path) -> None:
    proxy_ledger = tmp_path / "private" / "proxy-reservations.json"
    upstream_ledger = tmp_path / "private" / "upstream-reservations.json"
    config_value = config(
        tmp_path,
        target_count=1,
        upstream_base_url="",
        upstream_api_key="",
        upstream_base_urls=("https://domain-a.test",),
        upstream_group_name="TPA6API",
        unique_upstream_base_urls=True,
        proxy_reservation_file=proxy_ledger,
        upstream_reservation_file=upstream_ledger,
    )
    runner = SlowBatchRunner(config_value)
    state = runner._new_state()
    state["allocated_proxy_fingerprints"] = [_proxy_fingerprint("proxy-a")]
    state["allocated_upstream_base_urls"] = ["https://domain-a.test"]
    runner.store.save(state)

    runner._load_or_create()

    assert _proxy_fingerprint("proxy-a") in json.loads(proxy_ledger.read_text())["reservations"]
    assert "https://domain-a.test" in json.loads(upstream_ledger.read_text())["reservations"]


def _persist_partial_batch(
    runner: SlowBatchRunner,
    proxies: tuple[str, ...],
    *,
    completed_count: int,
    allocated_count: int | None = None,
) -> tuple[str, ...]:
    state = runner._new_state()
    if allocated_count is None:
        allocated_count = completed_count
    allocated = tuple(_proxy_fingerprint(proxy) for proxy in proxies[:allocated_count])
    state["allocated_proxy_fingerprints"] = list(allocated)
    state["completed"] = [
        {
            "template_id": state["template_ids"][index],
            "proxy_fingerprint": fingerprint,
        }
        for index, fingerprint in enumerate(allocated)
        if index < completed_count
    ]
    for index, fingerprint in enumerate(
        allocated[completed_count:],
        start=completed_count,
    ):
        session_file = str(runner.config.state_dir / "sessions" / f"abandoned-{index}.json")
        state["abandoned_sessions"].append(session_file)
        state["failures"].append(
            {
                "attempt": index + 1,
                "error": "qq_otp_timeout",
                "session_file": session_file,
                "proxy_fingerprint": fingerprint,
            }
        )
    runner.store.save(state)
    return allocated


def test_resume_proxy_capacity_only_requires_unfinished_accounts(tmp_path: Path) -> None:
    proxies = tuple(f"proxy-{index}" for index in range(11))
    reservation_file = tmp_path / "private" / "proxy-reservations.json"
    initial_config = config(tmp_path, target_count=10, proxy_urls=proxies)
    allocated = _persist_partial_batch(
        SlowBatchRunner(initial_config),
        proxies,
        completed_count=8,
        allocated_count=10,
    )
    resume_config = replace(
        initial_config,
        reserved_proxy_fingerprints=allocated[:8],
        proxy_reservation_file=reservation_file,
    )

    state = SlowBatchRunner(resume_config)._load_or_create()

    reservations = json.loads(reservation_file.read_text())["reservations"]
    assert len(state["completed"]) == 8
    assert set(reservations) == set(allocated)
    assert {reservation["batch_state"] for reservation in reservations.values()} == {
        str(resume_config.state_file.resolve())
    }


def test_resume_reuses_abandoned_proxies_but_not_another_batch_reservation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    proxies = tuple(f"proxy-{index}" for index in range(11))
    reservation_file = tmp_path / "private" / "proxy-reservations.json"
    initial_config = config(tmp_path, target_count=10, proxy_urls=proxies)
    allocated = _persist_partial_batch(
        SlowBatchRunner(initial_config),
        proxies,
        completed_count=8,
        allocated_count=10,
    )
    owner = str(initial_config.state_file.resolve())
    reservations = {
        fingerprint: {"batch_state": owner, "reserved_at": "2026-08-01T00:00:00Z"}
        for fingerprint in allocated
    }
    reservations[_proxy_fingerprint(proxies[10])] = {
        "batch_state": "/another/batch.json",
        "reserved_at": "2026-08-01T00:00:00Z",
    }
    write_private_json(
        reservation_file,
        {"schema_version": 1, "reservations": reservations},
    )
    resume_config = replace(
        initial_config,
        reserved_proxy_fingerprints=allocated[:8],
        proxy_reservation_file=reservation_file,
    )
    selected: list[str] = []
    clock = Clock()
    monkeypatch.setattr("a6api_auto.batch.Workflow.execute", successful_execute)
    result = SlowBatchRunner(
        resume_config,
        now=clock.now,
        sleep=clock.sleep,
        random_interval=lambda _low, _high: 10800,
        client_factory=lambda _origin, proxy: selected.append(proxy) or object(),
        mailbox_factory=lambda: object(),
    ).run()
    state = json.loads(resume_config.state_file.read_text())
    selected_fingerprints = {_proxy_fingerprint(proxy) for proxy in selected}

    assert result["completed"] == 10
    assert selected_fingerprints == set(allocated[8:])
    assert _proxy_fingerprint(proxies[10]) not in selected_fingerprints
    assert len(state["allocated_proxy_fingerprints"]) == 10


def test_resume_proxy_capacity_rejects_pool_smaller_than_remaining_target(
    tmp_path: Path,
) -> None:
    proxies = tuple(f"proxy-{index}" for index in range(11))
    initial_config = config(tmp_path, target_count=10, proxy_urls=proxies)
    allocated = _persist_partial_batch(
        SlowBatchRunner(initial_config),
        proxies,
        completed_count=8,
        allocated_count=10,
    )
    resume_config = replace(
        initial_config,
        proxy_urls=proxies[:9],
        reserved_proxy_fingerprints=allocated,
    )

    runner = SlowBatchRunner(resume_config)

    with pytest.raises(A6AutoError, match="^batch_proxy_pool_smaller_than_target$"):
        runner._load_or_create()


def test_resume_proxy_capacity_still_enforces_reservation_ownership(
    tmp_path: Path,
) -> None:
    proxies = tuple(f"proxy-{index}" for index in range(11))
    reservation_file = tmp_path / "private" / "proxy-reservations.json"
    initial_config = config(tmp_path, target_count=10, proxy_urls=proxies)
    allocated = _persist_partial_batch(
        SlowBatchRunner(initial_config),
        proxies,
        completed_count=8,
    )
    reservation_file.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "reservations": {
                    allocated[0]: {
                        "batch_state": "/another/batch.json",
                        "reserved_at": "2026-08-01T00:00:00Z",
                    }
                },
            }
        )
    )
    reservation_file.chmod(0o600)
    resume_config = replace(
        initial_config,
        reserved_proxy_fingerprints=allocated,
        proxy_reservation_file=reservation_file,
    )

    runner = SlowBatchRunner(resume_config)

    with pytest.raises(A6AutoError, match="^batch_allocated_proxy_reserved_elsewhere$"):
        runner._load_or_create()


def test_resume_proxy_capacity_still_enforces_state_contract(tmp_path: Path) -> None:
    proxies = tuple(f"proxy-{index}" for index in range(11))
    initial_config = config(tmp_path, target_count=10, proxy_urls=proxies)
    allocated = _persist_partial_batch(
        SlowBatchRunner(initial_config),
        proxies,
        completed_count=8,
    )
    state = json.loads(initial_config.state_file.read_text())
    state["target_count"] = 9
    initial_config.state_file.write_text(json.dumps(state))
    initial_config.state_file.chmod(0o600)
    resume_config = replace(
        initial_config,
        reserved_proxy_fingerprints=allocated,
    )

    runner = SlowBatchRunner(resume_config)

    with pytest.raises(A6AutoError, match="^batch_state_config_mismatch$"):
        runner._load_or_create()


def test_resume_proxy_capacity_rejects_invalid_existing_allocations(
    tmp_path: Path,
) -> None:
    proxies = tuple(f"proxy-{index}" for index in range(11))
    initial_config = config(tmp_path, target_count=10, proxy_urls=proxies)
    allocated = _persist_partial_batch(
        SlowBatchRunner(initial_config),
        proxies,
        completed_count=8,
    )
    state = json.loads(initial_config.state_file.read_text())
    state["allocated_proxy_fingerprints"].append(allocated[0])
    initial_config.state_file.write_text(json.dumps(state))
    initial_config.state_file.chmod(0o600)
    resume_config = replace(
        initial_config,
        reserved_proxy_fingerprints=allocated,
    )

    runner = SlowBatchRunner(resume_config)

    with pytest.raises(
        A6AutoError,
        match="^batch_allocated_proxy_fingerprints_invalid$",
    ):
        runner._load_or_create()
