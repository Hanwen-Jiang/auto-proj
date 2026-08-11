from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[1]
STAGE_SCRIPT = REPOSITORY / "deploy/hk-shadow/stage-provider-regional-runtime.sh"
VERIFY_SCRIPT = REPOSITORY / "deploy/hk-shadow/verify-provider-regional-runtime.sh"


def test_hk_provider_regional_stage_is_default_off_and_selector_scoped(
    tmp_path: Path,
) -> None:
    secrets = tmp_path / "regional.env"
    secrets.write_text(
        "\n".join(
            (
                "A6_PROVIDER_VALKEY_RUNTIME_PASSWORD=" + "r" * 40,
                "A6_PROVIDER_VALKEY_PROJECTOR_PASSWORD=" + "p" * 40,
                "PROVIDER_SUBSCRIBER_DATABASE_HOST=127.0.0.1",
                "PROVIDER_SUBSCRIBER_DATABASE_PORT=15432",
                "PROVIDER_SUBSCRIBER_DATABASE_USER=a6_provider_subscriber",
                "PROVIDER_SUBSCRIBER_DATABASE_PASSWORD=" + "s" * 40,
                "PROVIDER_SUBSCRIBER_DATABASE_DBNAME=sub2api_codexmanager",
                "PROVIDER_SUBSCRIBER_DATABASE_SSLMODE=disable",
                "PROVIDER_REGIONAL_PROJECTOR_DATABASE_DSN="
                "postgresql://a6_provider_projector:"
                + "d" * 40
                + "@127.0.0.1:15432/sub2api_codexmanager?sslmode=disable",
                "",
            )
        ),
        encoding="utf-8",
    )
    secrets.chmod(0o600)
    destination = tmp_path / "stage"
    environment = dict(os.environ)
    environment["DESTDIR"] = str(destination)

    subprocess.run(
        (str(STAGE_SCRIPT), str(secrets)),
        cwd=REPOSITORY,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )

    acl_path = destination / "etc/a6-provider-valkey-hk/users.acl"
    acl = acl_path.read_text(encoding="utf-8")
    assert acl.splitlines()[0] == "user default off"
    assert "user a6-provider-runtime reset on " in acl
    assert "~a6:provider:dispatch:* ~a6:provider:fast-block:*" in acl
    assert "~a6:provider:capacity:* ~sub2api:a6-readiness:*" in acl
    assert "user a6-provider-projector reset on " in acl
    assert "~a6:provider:*" in acl
    assert stat.S_IMODE(acl_path.stat().st_mode) == 0o640

    runtime_env_path = (
        destination / "etc/sub2api-hk/sub2api-hk-shadow-provider-regional.env"
    )
    runtime_env = runtime_env_path.read_text(encoding="utf-8")
    assert "PROVIDER_REDIS_USERNAME=a6-provider-runtime\n" in runtime_env
    assert stat.S_IMODE(runtime_env_path.stat().st_mode) == 0o600

    projector_env_path = destination / "etc/a6-provider-regional-hk/projector.env"
    projector_env = projector_env_path.read_text(encoding="utf-8")
    assert "PROVIDER_REDIS_PROJECTOR_USERNAME=a6-provider-projector\n" in projector_env
    assert stat.S_IMODE(projector_env_path.stat().st_mode) == 0o600


def test_hk_provider_regional_scripts_and_centos7_unit_contract() -> None:
    assert os.access(STAGE_SCRIPT, os.X_OK)
    assert os.access(VERIFY_SCRIPT, os.X_OK)
    subprocess.run(("sh", "-n", str(STAGE_SCRIPT)), check=True)
    subprocess.run(("sh", "-n", str(VERIFY_SCRIPT)), check=True)

    verifier = VERIFY_SCRIPT.read_text(encoding="utf-8")
    assert 'provider_username=$(env_value PROVIDER_REDIS_USERNAME' in verifier
    assert '--user "$provider_username"' in verifier
    assert 'fail "runtime ACL permits projection writes"' in verifier
    assert "-m a6api_auto.trade_provider_regional_gate" in verifier
    assert "capacity_overallocated_accounts" in verifier
    assert 'HGETALL "a6:provider:capacity:v1:{hk:$account_id}:meta"' in verifier

    unit = (REPOSITORY / "deploy/systemd/a6-provider-valkey-hk.service").read_text(
        encoding="utf-8"
    )
    assert "ExecStart=/usr/bin/valkey-server" in unit
    assert "ExecStartPre=/usr/bin/test -d /var/lib/a6-provider-valkey-hk" in unit
    for unsupported in (
        "StateDirectory=",
        "ProtectClock=",
        "ProtectControlGroups=",
        "ProtectHostname=",
        "ProtectKernelLogs=",
        "ProtectKernelModules=",
        "ProtectKernelTunables=",
        "ProtectSystem=strict",
    ):
        assert unsupported not in unit
