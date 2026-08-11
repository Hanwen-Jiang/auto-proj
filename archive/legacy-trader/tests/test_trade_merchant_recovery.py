from __future__ import annotations

import json
from pathlib import Path

import pytest

from a6api_auto.models import SessionRecord
from a6api_auto.storage import SessionStore, write_private_json
from a6api_auto.trade_merchant_recovery import (
    A6ProxyVerifier,
    MerchantRecoveryController,
    MerchantRecoveryError,
    PostgresMerchantRecoveryStore,
    ProxyRepairTarget,
    ProxyVerification,
    load_proxy_map,
    load_proxy_pool,
    load_reserved_proxy_fingerprints,
    normalize_proxy_url,
)


def session(path: Path, *, user_id: int = 10, supplier_id: int = 20) -> Path:
    SessionStore(path).save(
        SessionRecord(
            schema_version=1,
            origin="https://a6api.com",
            email="fixture@example.test",
            username="fixture",
            password="secret",
            user_id=user_id,
            cookies={"session": "fixture"},
            status="supplier_submitted",
            supplier_id=supplier_id,
        )
    )
    return path


def private_list(path: Path, values: list[str]) -> Path:
    path.write_text(json.dumps(values), encoding="utf-8")
    path.chmod(0o600)
    return path


def target(path: Path) -> ProxyRepairTarget:
    return ProxyRepairTarget(
        merchant_id=7,
        a6_user_id=10,
        supplier_id=20,
        session_ref=str(path),
        poll_error_count=5,
        incident_id=99,
        incident_key="collector:7:a6_transport_failed",
        occurrence_count=12,
    )


class Store:
    def __init__(self, repair_target: ProxyRepairTarget | None) -> None:
        self.repair_target = repair_target
        self.applied: list[dict[str, object]] = []

    def resolve_terminal_archive_incidents(self) -> int:
        return 0

    def never_activated_archive_candidates(self, **_kwargs):
        return []

    def complete_never_activated_archive(self, *_args, **_kwargs):
        raise AssertionError("no archive candidate")

    def proxy_repair_targets(self, **_kwargs):
        return [self.repair_target] if self.repair_target else []

    def apply_proxy_repair(self, repair_target, **kwargs):
        self.applied.append({"target": repair_target, **kwargs})
        return True


class Verifier:
    def __init__(self, healthy: set[str]) -> None:
        self.healthy = healthy
        self.calls: list[str] = []

    def verify(self, _record, proxy):
        self.calls.append(proxy)
        return ProxyVerification(proxy, 25) if proxy in self.healthy else None


def controller_fixture(tmp_path: Path, verifier: Verifier):
    sessions = tmp_path / "sessions"
    sessions.mkdir(mode=0o700)
    path = session(sessions / "merchant.json")
    proxy_map = tmp_path / "proxy-map.json"
    write_private_json(proxy_map, {"10": "http://127.0.0.1:23080"})
    pool = private_list(
        tmp_path / "pool.json",
        [
            "http://127.0.0.1:23080",
            "http://127.0.0.1:23081",
            "http://127.0.0.1:23082",
        ],
    )
    store = Store(target(path))
    controller = MerchantRecoveryController(
        store,  # type: ignore[arg-type]
        sessions_dir=sessions,
        proxy_map_file=proxy_map,
        proxy_pool_files=(pool,),
        verifier=verifier,
    )
    return controller, store


def test_proxy_inputs_are_private_unique_loopback_configuration(tmp_path: Path) -> None:
    assert normalize_proxy_url("http://127.0.0.1:23080") == "http://127.0.0.1:23080"
    for invalid in (
        "https://example.test:443",
        "http://user:secret@127.0.0.1:23080",
        "ftp://127.0.0.1:23080",
    ):
        with pytest.raises(MerchantRecoveryError):
            normalize_proxy_url(invalid)

    mapping = tmp_path / "map.json"
    write_private_json(mapping, {"1": "http://127.0.0.1:23080"})
    assert load_proxy_map(mapping) == {1: "http://127.0.0.1:23080"}

    pool = private_list(
        tmp_path / "pool.json",
        ["http://127.0.0.1:23080", "http://127.0.0.1:23080"],
    )
    assert load_proxy_pool((pool,)) == ("http://127.0.0.1:23080",)


def test_reserved_proxy_fingerprints_are_excluded_from_recovery(tmp_path: Path) -> None:
    reservation = tmp_path / "reservations.json"
    write_private_json(
        reservation,
        {"schema_version": 1, "reservations": {"0123456789ab": {"owner": "batch"}}},
    )

    assert load_reserved_proxy_fingerprints((reservation,)) == {"0123456789ab"}


def test_proxy_verifier_requires_repeated_user_and_supplier_identity(tmp_path: Path) -> None:
    record = SessionStore(session(tmp_path / "s.json")).load()

    class Client:
        def __init__(self, supplier_id=20):
            self.supplier_id = supplier_id

        def import_cookies(self, _cookies):
            pass

        def self_user(self, _user_id):
            return {"id": 10}

        def supplier_me(self, _user_id):
            return {"id": self.supplier_id}

    calls: list[int] = []

    def factory(_record, _proxy, _timeout):
        calls.append(1)
        return Client()

    verifier = A6ProxyVerifier(required_successes=2, client_factory=factory)
    result = verifier.verify(record, "http://127.0.0.1:23080")

    assert result is not None
    assert len(calls) == 2
    mismatch = A6ProxyVerifier(
        required_successes=2,
        client_factory=lambda *_args: Client(supplier_id=21),
    )
    assert mismatch.verify(record, "http://127.0.0.1:23080") is None


def test_controller_keeps_a_recovered_current_proxy(tmp_path: Path) -> None:
    current = "http://127.0.0.1:23080"
    verifier = Verifier({current})
    controller, store = controller_fixture(tmp_path, verifier)

    result = controller.run_once(apply=True)

    assert result["proxy_repair_count"] == 0
    assert result["proxy_results"] == [
        {"merchant_id": 7, "action": "current_proxy_recovered", "applied": False}
    ]
    assert store.applied == []


def test_controller_replaces_only_with_a_verified_unused_proxy(tmp_path: Path) -> None:
    selected = "http://127.0.0.1:23082"
    verifier = Verifier({selected})
    controller, store = controller_fixture(tmp_path, verifier)

    result = controller.run_once(apply=True)

    assert verifier.calls == [
        "http://127.0.0.1:23080",
        "http://127.0.0.1:23081",
        selected,
    ]
    assert result["proxy_repair_count"] == 1
    assert result["proxy_results"][0]["new_lease_id"] == "singbox:23082"
    assert len(store.applied) == 1
    assert store.applied[0]["expected_proxy"] == "http://127.0.0.1:23080"


def test_controller_defers_when_no_candidate_passes_identity_check(tmp_path: Path) -> None:
    controller, store = controller_fixture(tmp_path, Verifier(set()))

    result = controller.run_once(apply=True)

    assert result["proxy_results"][0]["action"] == "deferred_no_healthy_proxy"
    assert store.applied == []


class RecoveryCursor:
    def __init__(self, rows=None, current=None) -> None:
        self.rows = list(rows or [])
        self.current = current
        self.queries: list[str] = []
        self.parameters: list[object] = []
        self.description = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, query, params=None):
        self.queries.append(" ".join(str(query).split()))
        self.parameters.append(params)

    def fetchone(self):
        value = self.current
        self.current = None
        return value

    def fetchall(self):
        rows, self.rows = self.rows, []
        return rows


class RecoveryConnection:
    def __init__(self, cursor: RecoveryCursor) -> None:
        self.cursor_value = cursor
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return self.cursor_value

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def test_proxy_targets_include_consecutive_session_refresh_transport_failures() -> None:
    cursor = RecoveryCursor(
        rows=[
            {
                "merchant_id": 7,
                "a6_user_id": 10,
                "supplier_id": 20,
                "session_ref": "/sessions/merchant.json",
                "poll_error_count": 0,
                "incident_id": 99,
                "incident_key": "session-refresh:7:a6_transport_failed",
                "occurrence_count": 3,
            }
        ]
    )
    store = PostgresMerchantRecoveryStore(RecoveryConnection(cursor))

    targets = store.proxy_repair_targets(
        failure_threshold=3,
        stale_seconds=1800,
        repair_cooldown_seconds=3600,
        limit=1,
    )

    assert targets[0].poll_error_count == 0
    assert targets[0].incident_key == "session-refresh:7:a6_transport_failed"
    assert cursor.parameters[0] == (3, 1800, 3, 3600, 1)
    assert "JOIN LATERAL" in cursor.queries[0]
    assert "session-refresh:" in cursor.queries[0]
    assert "supplier_self_failed" in cursor.queries[0]


def test_session_refresh_incident_is_counted_and_resolved_per_merchant() -> None:
    cursor = RecoveryCursor(current={"id": 7})
    connection = RecoveryConnection(cursor)
    store = PostgresMerchantRecoveryStore(connection)

    assert store.record_session_refresh_failure(
        "/sessions/merchant.json", "a6_transport_failed"
    )
    cursor.rows = [{"id": 99}]
    assert store.resolve_session_refresh_failures("/sessions/merchant.json") == 1

    joined = "\n".join(cursor.queries)
    assert "session-refresh:7:a6_transport_failed" in str(cursor.parameters)
    assert "WHEN a6_trade.trade_incidents.status = 'resolved' THEN 1" in joined
    assert "merchant.id::text || ':%%'" in joined
    assert connection.commits == 2


def test_proxy_repair_accepts_session_incident_without_collector_error_count(
    tmp_path: Path,
) -> None:
    proxy_map = tmp_path / "proxy-map.json"
    current_proxy = "http://127.0.0.1:23080"
    selected_proxy = "http://127.0.0.1:23081"
    write_private_json(proxy_map, {"10": current_proxy})
    repair_target = ProxyRepairTarget(
        merchant_id=7,
        a6_user_id=10,
        supplier_id=20,
        session_ref="/sessions/merchant.json",
        poll_error_count=0,
        incident_id=99,
        incident_key="session-refresh:7:a6_transport_failed",
        occurrence_count=3,
    )
    cursor = RecoveryCursor(
        current={
            "id": 7,
            "a6_user_id": 10,
            "poll_error_count": 0,
            "proxy_lease_id": "singbox:23080",
            "incident_status": "open",
            "incident_key": repair_target.incident_key,
        }
    )
    connection = RecoveryConnection(cursor)

    changed = PostgresMerchantRecoveryStore(connection).apply_proxy_repair(
        repair_target,
        proxy_map_file=proxy_map,
        expected_proxy=current_proxy,
        selected=ProxyVerification(selected_proxy, 25),
    )

    assert changed is True
    assert load_proxy_map(proxy_map) == {10: selected_proxy}
    assert connection.commits == 1


class ArchiveCursor:
    def __init__(self) -> None:
        self.queries: list[str] = []
        self.parameters: list[object] = []
        self.rowcount = 0
        self.description = []
        self._candidate = True

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, query, params=None):
        self.queries.append(" ".join(str(query).split()))
        self.parameters.append(params)
        self.rowcount = 1 if "UPDATE a6_trade.a6_seller_retirement_plans" in query else 0

    def fetchone(self):
        if self._candidate:
            self._candidate = False
            return {"merchant_id": 46375, "roles": ["seller"]}
        return None


class ArchiveConnection:
    def __init__(self) -> None:
        self.cursor_value = ArchiveCursor()
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return self.cursor_value

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def test_never_activated_archive_is_atomic_and_audited() -> None:
    connection = ArchiveConnection()
    store = PostgresMerchantRecoveryStore(connection)

    changed = store.complete_never_activated_archive(
        46375,
        minimum_age_seconds=1800,
        minimum_errors=3,
    )

    assert changed is True
    assert connection.commits == 1
    joined = "\n".join(connection.cursor_value.queries)
    assert "NOT EXISTS ( SELECT 1 FROM a6_trade.a6_supplier_channels_current" in joined
    assert "NOT EXISTS ( SELECT 1 FROM a6_trade.a6_supplier_listings_current" in joined
    assert "ARRAY['observer']::text[]" in joined
    assert "safe_archive_completed_without_supplier_activation" in joined
    assert "retire_seller_role" in joined


def test_network_scanners_share_one_blocking_systemd_lock() -> None:
    units = (
        "a6-trader-collector.service",
        "a6-trader-session-refresh.service",
        "a6-trader-merchant-recovery.service",
    )
    for unit in units:
        text = (Path("deploy/systemd") / unit).read_text()
        assert "flock --exclusive /var/lib/a6-trader/network-scan.lock" in text
        assert "--nonblock" not in text
