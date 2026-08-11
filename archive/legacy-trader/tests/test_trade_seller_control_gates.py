from __future__ import annotations

import configparser
import grp
import json
import os
import pwd
import subprocess
import sys
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

import pytest

from a6api_auto.trade_seller_control_gates import (
    SELLER_CONTROL_STAGES,
    SellerControlGateError,
    check_seller_control_gate,
    main,
    promote_seller_control_gate,
    revoke_seller_control_gate,
    seller_control_gate_status,
)

NOW = datetime(2026, 7, 31, 11, 0, tzinfo=timezone.utc)


def systemd_success_exit_statuses(service: str) -> frozenset[int]:
    document = configparser.ConfigParser(interpolation=None, strict=False)
    document.read_string(service)
    configured = document["Service"].get("SuccessExitStatus", "").split()
    return frozenset((0, *(int(value) for value in configured)))


def simulate_systemd_oneshot(
    *,
    condition_exit_code: int,
    success_exit_statuses: frozenset[int],
    exec_start: Callable[[], int],
) -> str:
    # SuccessExitStatus applies to both ExecCondition and ExecStart.
    if condition_exit_code not in success_exit_statuses:
        if 1 <= condition_exit_code <= 254:
            return "skipped"
        return "failed"
    exec_start_exit_code = exec_start()
    if exec_start_exit_code in success_exit_statuses:
        return "success"
    return "failed"


def promote(root: Path, stage: str) -> dict[str, object]:
    return promote_seller_control_gate(
        stage,
        reason=f"recover {stage}",
        evidence=f"smoke:{stage}:passed",
        root=root,
        clock=lambda: NOW,
        actor="operator",
        hostname="us-cloud",
    )


def test_promotions_require_strict_recovery_order_and_persist_evidence(
    tmp_path: Path,
) -> None:
    root = tmp_path / "gates"
    status = seller_control_gate_status(root)
    assert status["active_stage_count"] == 0
    assert status["next_stage"] == "retirement"

    with pytest.raises(
        SellerControlGateError,
        match="seller_control_gate_promotion_order_invalid",
    ):
        promote(root, "seller-pilot")

    first = promote(root, "retirement")
    second = promote(root, "seller-pilot")
    assert first["changed"] is True
    assert second["active_stage_count"] == 2
    assert second["next_stage"] == "seller-onboard"

    retirement = json.loads((root / "10-retirement.enabled").read_text())
    pilot = json.loads((root / "20-seller-pilot.enabled").read_text())
    assert retirement["enabled_at"] == "2026-07-31T11:00:00Z"
    assert retirement["reason"] == "recover retirement"
    assert retirement["evidence"] == "smoke:retirement:passed"
    assert pilot["previous_stage_event_id"] == retirement["event_id"]

    audit = [json.loads(line) for line in (root / "audit.jsonl").read_text().splitlines()]
    assert [(item["action"], item["phase"]) for item in audit] == [
        ("promote", "intent"),
        ("promote", "complete"),
        ("promote", "intent"),
        ("promote", "complete"),
    ]


def test_revoke_cascades_to_all_later_stages(tmp_path: Path) -> None:
    root = tmp_path / "gates"
    for stage in SELLER_CONTROL_STAGES:
        promote(root, stage.name)

    result = revoke_seller_control_gate(
        "seller-pilot",
        reason="pause write loops",
        evidence="incident:provider-booster-overlap",
        root=root,
        clock=lambda: NOW,
        actor="operator",
        hostname="us-cloud",
    )

    assert result["changed"] is True
    assert result["revoked_stages"] == [
        "seller-pilot",
        "seller-onboard",
        "channel-growth",
    ]
    assert result["active_stage_count"] == 1
    assert (root / "10-retirement.enabled").exists()
    assert not (root / "20-seller-pilot.enabled").exists()
    assert not (root / "30-seller-onboard.enabled").exists()
    assert not (root / "40-channel-growth.enabled").exists()

    audit = [json.loads(line) for line in (root / "audit.jsonl").read_text().splitlines()]
    assert audit[-2]["phase"] == "intent"
    assert audit[-1]["phase"] == "complete"
    assert audit[-1]["revoked_stages"] == result["revoked_stages"]

    check_seller_control_gate(
        "retirement",
        root=root,
        expected_owner_uid=os.getuid(),
        expected_group_gid=os.getgid(),
    )
    promote(root, "seller-pilot")
    recovered = check_seller_control_gate(
        "seller-pilot",
        root=root,
        expected_owner_uid=os.getuid(),
        expected_group_gid=os.getgid(),
    )
    assert recovered["active_stage_count"] == 2


def test_marker_chain_tampering_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "gates"
    promote(root, "retirement")
    promote(root, "seller-pilot")
    marker = root / "20-seller-pilot.enabled"
    pilot = json.loads(marker.read_text())
    pilot["previous_stage_event_id"] = "0" * 32
    marker.write_text(
        json.dumps(pilot, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(
        SellerControlGateError,
        match="seller_control_gate_chain_invalid",
    ):
        seller_control_gate_status(root)


def test_check_requires_enabled_stage_and_matching_audit(tmp_path: Path) -> None:
    root = tmp_path / "gates"
    promote(root, "retirement")
    promote(root, "seller-pilot")

    result = check_seller_control_gate(
        "seller-pilot",
        root=root,
        expected_owner_uid=os.getuid(),
        expected_group_gid=os.getgid(),
    )
    assert result["checked_stage"] == "seller-pilot"
    assert result["active_stage_count"] == 2

    with pytest.raises(
        SellerControlGateError,
        match="seller_control_gate_stage_disabled",
    ):
        check_seller_control_gate(
            "seller-onboard",
            root=root,
            expected_owner_uid=os.getuid(),
            expected_group_gid=os.getgid(),
        )

    promote(root, "seller-onboard")
    promote(root, "channel-growth")
    growth_marker = root / "40-channel-growth.enabled"
    growth = json.loads(growth_marker.read_text())
    growth_marker.write_text(json.dumps(growth, indent=2), encoding="utf-8")
    assert (
        check_seller_control_gate(
            "retirement",
            root=root,
            expected_owner_uid=os.getuid(),
            expected_group_gid=os.getgid(),
        )["checked_stage"]
        == "retirement"
    )
    with pytest.raises(
        SellerControlGateError,
        match="seller_control_gate_marker_noncanonical",
    ):
        check_seller_control_gate(
            "channel-growth",
            root=root,
            expected_owner_uid=os.getuid(),
            expected_group_gid=os.getgid(),
        )


def test_check_rejects_touch_pretty_json_and_relaxed_marker_mode(tmp_path: Path) -> None:
    root = tmp_path / "gates"
    promote(root, "retirement")
    marker = root / "10-retirement.enabled"

    marker.write_text("", encoding="utf-8")
    with pytest.raises(SellerControlGateError, match="seller_control_gate_marker_invalid"):
        check_seller_control_gate(
            "retirement",
            root=root,
            expected_owner_uid=os.getuid(),
            expected_group_gid=os.getgid(),
        )

    root = tmp_path / "symlink-gates"
    promote(root, "retirement")
    marker = root / "10-retirement.enabled"
    target = root / "marker-target.json"
    marker.rename(target)
    marker.symlink_to(target.name)
    with pytest.raises(
        SellerControlGateError,
        match="seller_control_gate_marker_security_invalid",
    ):
        check_seller_control_gate(
            "retirement",
            root=root,
            expected_owner_uid=os.getuid(),
            expected_group_gid=os.getgid(),
        )

    root = tmp_path / "pretty-gates"
    promote(root, "retirement")
    marker = root / "10-retirement.enabled"
    document = json.loads(marker.read_text())
    marker.write_text(json.dumps(document, indent=2), encoding="utf-8")
    with pytest.raises(
        SellerControlGateError,
        match="seller_control_gate_marker_noncanonical",
    ):
        check_seller_control_gate(
            "retirement",
            root=root,
            expected_owner_uid=os.getuid(),
            expected_group_gid=os.getgid(),
        )

    root = tmp_path / "mode-gates"
    promote(root, "retirement")
    marker = root / "10-retirement.enabled"
    marker.chmod(0o660)
    with pytest.raises(
        SellerControlGateError,
        match="seller_control_gate_marker_security_invalid",
    ):
        check_seller_control_gate(
            "retirement",
            root=root,
            expected_owner_uid=os.getuid(),
            expected_group_gid=os.getgid(),
        )


def test_check_rejects_marker_identity_and_audit_pair_tampering(tmp_path: Path) -> None:
    root = tmp_path / "identity-gates"
    promote(root, "retirement")
    marker = root / "10-retirement.enabled"
    document = json.loads(marker.read_text())
    document["actor"] = " operator "
    marker.write_text(
        json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(SellerControlGateError, match="seller_control_gate_marker_invalid"):
        check_seller_control_gate(
            "retirement",
            root=root,
            expected_owner_uid=os.getuid(),
            expected_group_gid=os.getgid(),
        )

    root = tmp_path / "audit-mode-gates"
    promote(root, "retirement")
    (root / "audit.jsonl").chmod(0o644)
    with pytest.raises(
        SellerControlGateError,
        match="seller_control_gate_audit_security_invalid",
    ):
        check_seller_control_gate(
            "retirement",
            root=root,
            expected_owner_uid=os.getuid(),
            expected_group_gid=os.getgid(),
        )

    root = tmp_path / "audit-gates"
    promote(root, "retirement")
    audit_path = root / "audit.jsonl"
    entries = [json.loads(line) for line in audit_path.read_text().splitlines()]
    entries[1]["evidence"] = "different-complete-evidence"
    audit_path.write_text(
        "".join(
            json.dumps(entry, sort_keys=True, separators=(",", ":")) + "\n" for entry in entries
        ),
        encoding="utf-8",
    )
    with pytest.raises(
        SellerControlGateError,
        match="seller_control_gate_audit_pair_mismatch",
    ):
        check_seller_control_gate(
            "retirement",
            root=root,
            expected_owner_uid=os.getuid(),
            expected_group_gid=os.getgid(),
        )


def test_check_rejects_unexpected_artifact_owner(tmp_path: Path) -> None:
    root = tmp_path / "gates"
    promote(root, "retirement")

    with pytest.raises(
        SellerControlGateError,
        match="seller_control_gate_root_security_invalid",
    ):
        check_seller_control_gate(
            "retirement",
            root=root,
            expected_owner_uid=os.getuid() + 1,
            expected_group_gid=os.getgid(),
        )


def test_cli_writes_expected_ownership_and_check_uses_condition_exit_code(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "gates"
    owner = pwd.getpwuid(os.getuid()).pw_name
    group = grp.getgrgid(os.getgid()).gr_name
    ownership_arguments = ["--expected-owner", owner, "--expected-group", group]

    assert (
        main(
            [
                "--root",
                str(root),
                "promote",
                "retirement",
                "--reason",
                "recover retirement",
                "--evidence",
                "smoke:retirement:passed",
                *ownership_arguments,
            ]
        )
        == 0
    )
    assert main(["--root", str(root), "check", "retirement", *ownership_arguments]) == 0
    assert main(["--root", str(root), "check", "seller-pilot", *ownership_arguments]) == 3
    captured = capsys.readouterr()
    assert '"checked_stage":"retirement"' in captured.out
    assert '"error":"seller_control_gate_stage_disabled"' in captured.err

    for path, mode in (
        (root, 0o750),
        (root / ".gate.lock", 0o640),
        (root / "audit.jsonl", 0o640),
        (root / "10-retirement.enabled", 0o640),
    ):
        info = path.stat()
        assert info.st_uid == os.getuid()
        assert info.st_gid == os.getgid()
        assert info.st_mode & 0o777 == mode


def test_systemd_services_validate_gates_without_conditioning_timers() -> None:
    root = Path(__file__).resolve().parents[1]
    unit_stages = {
        "a6-trader-seller-retirement": SELLER_CONTROL_STAGES[0],
        "a6-trader-seller-pilot": SELLER_CONTROL_STAGES[1],
        "a6-trader-seller-channel-onboard": SELLER_CONTROL_STAGES[2],
        "a6-trader-channel-growth": SELLER_CONTROL_STAGES[3],
    }

    for unit_name, stage in unit_stages.items():
        service = (root / f"deploy/systemd/{unit_name}.service").read_text()
        timer = (root / f"deploy/systemd/{unit_name}.timer").read_text()
        assert service.count("ExecCondition=") == 1
        assert stage.systemd_exec_condition in service
        assert "ConditionPathExists=" not in service
        assert "ExecCondition=" not in timer
        assert "ConditionPathExists=" not in timer
        assert "/run/a6-trader/allow-" not in service
        assert "/run/a6-trader/allow-" not in timer
        assert "WantedBy=timers.target" in timer
        expected_success_statuses = {0, 75} if unit_name == "a6-trader-channel-growth" else {0}
        assert systemd_success_exit_statuses(service) == expected_success_statuses
        assert service.count("--systemd-accept-partial-result") == 1


@pytest.mark.parametrize(
    ("unit_name", "stage_index"),
    (
        ("a6-trader-seller-retirement", 0),
        ("a6-trader-seller-pilot", 1),
        ("a6-trader-seller-channel-onboard", 2),
        ("a6-trader-channel-growth", 3),
    ),
)
def test_systemd_gate_is_fail_closed_with_command_scoped_partial_normalization(
    tmp_path: Path,
    unit_name: str,
    stage_index: int,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repository_root = Path(__file__).resolve().parents[1]
    service = (repository_root / f"deploy/systemd/{unit_name}.service").read_text()
    success_exit_statuses = systemd_success_exit_statuses(service)
    stage = SELLER_CONTROL_STAGES[stage_index]
    root = tmp_path / unit_name
    owner = pwd.getpwuid(os.getuid()).pw_name
    group = grp.getgrgid(os.getgid()).gr_name
    check_arguments = [
        "--root",
        str(root),
        "check",
        stage.name,
        "--expected-owner",
        owner,
        "--expected-group",
        group,
    ]

    if stage_index == 0:
        revoke_seller_control_gate(
            stage.name,
            reason="initialize disabled gate fixture",
            evidence="fixture:disabled",
            root=root,
        )
    else:
        for prerequisite in SELLER_CONTROL_STAGES[:stage_index]:
            promote(root, prerequisite.name)

    disabled_exit_code = main(check_arguments)
    capsys.readouterr()
    assert disabled_exit_code == 3
    assert {2, 3, 255}.isdisjoint(success_exit_statuses)
    exec_start_count = 0

    def exec_start() -> int:
        nonlocal exec_start_count
        exec_start_count += 1
        return 0

    assert (
        simulate_systemd_oneshot(
            condition_exit_code=disabled_exit_code,
            success_exit_statuses=success_exit_statuses,
            exec_start=exec_start,
        )
        == "skipped"
    )
    assert exec_start_count == 0

    promote(root, stage.name)
    enabled_exit_code = main(check_arguments)
    capsys.readouterr()
    assert enabled_exit_code == 0
    assert (
        simulate_systemd_oneshot(
            condition_exit_code=enabled_exit_code,
            success_exit_statuses=success_exit_statuses,
            exec_start=exec_start,
        )
        == "success"
    )
    assert exec_start_count == 1


@pytest.mark.parametrize("failure_mode", ("corrupt-marker", "wrong-owner"))
def test_systemd_gate_validation_failure_fails_unit_without_exec_start(
    tmp_path: Path,
    failure_mode: str,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository_root = Path(__file__).resolve().parents[1]
    service = (repository_root / "deploy/systemd/a6-trader-seller-retirement.service").read_text()
    success_exit_statuses = systemd_success_exit_statuses(service)
    root = tmp_path / failure_mode
    promote(root, "retirement")
    owner = pwd.getpwuid(os.getuid()).pw_name
    group = grp.getgrgid(os.getgid()).gr_name

    if failure_mode == "corrupt-marker":
        (root / "10-retirement.enabled").write_text("{}\n", encoding="utf-8")
    else:
        monkeypatch.setattr(
            "a6api_auto.trade_seller_control_gates._resolved_ownership",
            lambda _owner, _group: (os.getuid() + 1, os.getgid()),
        )

    condition_exit_code = main(
        [
            "--root",
            str(root),
            "check",
            "retirement",
            "--expected-owner",
            owner,
            "--expected-group",
            group,
        ]
    )
    captured = capsys.readouterr()
    assert condition_exit_code == 255
    assert '"success":false' in captured.err
    assert {2, 3, 255}.isdisjoint(success_exit_statuses)
    exec_start_count = 0

    def exec_start() -> int:
        nonlocal exec_start_count
        exec_start_count += 1
        return 2

    assert (
        simulate_systemd_oneshot(
            condition_exit_code=condition_exit_code,
            success_exit_statuses=success_exit_statuses,
            exec_start=exec_start,
        )
        == "failed"
    )
    assert exec_start_count == 0


@pytest.mark.parametrize(
    "unit_name",
    (
        "a6-trader-seller-retirement",
        "a6-trader-seller-pilot",
        "a6-trader-seller-channel-onboard",
        "a6-trader-channel-growth",
    ),
)
@pytest.mark.parametrize("condition_failure", ("missing-script", "argparse-error"))
def test_condition_process_exit_two_never_reaches_exec_start(
    tmp_path: Path,
    unit_name: str,
    condition_failure: str,
) -> None:
    repository_root = Path(__file__).resolve().parents[1]
    service = (repository_root / f"deploy/systemd/{unit_name}.service").read_text()
    success_exit_statuses = systemd_success_exit_statuses(service)
    if condition_failure == "missing-script":
        command = [sys.executable, str(tmp_path / "missing-gate-script.py")]
    else:
        command = [
            sys.executable,
            str(repository_root / "scripts/manage_a6_seller_control_gates.py"),
            "check",
            "retirement",
            "--unexpected-argument",
        ]
    condition = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
    )
    assert condition.returncode == 2
    assert 2 not in success_exit_statuses
    exec_start_count = 0

    def exec_start() -> int:
        nonlocal exec_start_count
        exec_start_count += 1
        return 0

    assert (
        simulate_systemd_oneshot(
            condition_exit_code=condition.returncode,
            success_exit_statuses=success_exit_statuses,
            exec_start=exec_start,
        )
        == "skipped"
    )
    assert exec_start_count == 0
