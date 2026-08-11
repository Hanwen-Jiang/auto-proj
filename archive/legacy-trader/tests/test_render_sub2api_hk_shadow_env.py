from __future__ import annotations

import stat

import pytest

from scripts.render_sub2api_hk_shadow_env import (
    _TRADER_INTERNAL_TOKEN_KEY,
    ShadowEnvError,
    parse_environment_file,
    render_environment,
    write_private_environment,
)

SOURCE = """
DATABASE_USER=sub2api
DATABASE_PASSWORD=database-secret
DATABASE_DBNAME=sub2api
REDIS_PASSWORD=redis-secret
REDIS_DB=0
JWT_SECRET=jwt-secret-value-long-enough
JWT_EXPIRE_HOUR=24
TOTP_ENCRYPTION_KEY=totp-secret
TZ=Asia/Shanghai
PROXY_POOL_SUBSCRIPTION_URL_1=https://subscription.invalid/secret
KIRO_SIDECAR_API_KEY=sidecar-secret
ADMIN_PASSWORD=admin-secret
"""


def test_renderer_keeps_only_shared_state_secrets_and_forces_loopback() -> None:
    rendered = render_environment(parse_environment_file(SOURCE))

    assert "DATABASE_PASSWORD=database-secret" in rendered
    assert "REDIS_PASSWORD=redis-secret" in rendered
    assert "JWT_SECRET=jwt-secret-value-long-enough" in rendered
    assert "TOTP_ENCRYPTION_KEY=totp-secret" in rendered
    assert "DATABASE_HOST=127.0.0.1" in rendered
    assert "DATABASE_PORT=15432" in rendered
    assert "REDIS_HOST=127.0.0.1" in rendered
    assert "REDIS_PORT=16379" in rendered
    assert "SERVER_HOST=127.0.0.1" in rendered
    assert "SERVER_PORT=48769" in rendered
    assert "GATEWAY_A6_PROVIDER_REPLAY_ENABLED=false" in rendered
    assert "GATEWAY_A6_PROVIDER_REPLAY_DURABLE_COORDINATION_ENABLED=false" in rendered
    assert "GATEWAY_A6_PROVIDER_REPLAY_DURABLE_ATTRIBUTION_ENABLED=false" in rendered
    assert "GATEWAY_A6_PROVIDER_REPLAY_CANDIDATE_SNAPSHOT_ENABLED=false" in rendered
    assert "GATEWAY_A6_PROVIDER_REPLAY_DISPATCH_LEASE_ENABLED=false" in rendered
    assert "GATEWAY_A6_PROVIDER_REPLAY_FAST_BLOCK_ENABLED=false" in rendered
    assert "GATEWAY_A6_PROVIDER_REPLAY_USAGE_ATTRIBUTION_ENABLED=false" in rendered
    assert "GATEWAY_A6_PROVIDER_REPLAY_PERSISTENCE_TIMEOUT_SECONDS=10" in rendered
    assert (
        "GATEWAY_A6_PROVIDER_REPLAY_SPOOL_DIRECTORY=/opt/sub2api-hk/replay-spool"
        in rendered
    )
    assert "PROXY_POOL_ENABLED=false" in rendered
    assert "PROXY_POOL_SUBSCRIPTION" not in rendered
    assert "KIRO_SIDECAR_API_KEY" not in rendered
    assert "ADMIN_PASSWORD" not in rendered
    assert "TRADER_FAILURE_REPORT" not in rendered


def test_renderer_maps_only_trader_internal_token_to_loopback_reporter() -> None:
    rendered = render_environment(
        parse_environment_file(SOURCE),
        parse_environment_file(
            "A6_TRADER_INTERNAL_API_TOKEN=" + "a" * 64 + "\n"
            "A6_PANEL_PASSWORD=do-not-copy\n"
        ),
    )

    assert (
        "GATEWAY_A6_PROVIDER_REPLAY_TRADER_FAILURE_REPORT_URL="
        "http://127.0.0.1:18765/internal/provider-attempt-failures\n"
    ) in rendered
    assert (
        "GATEWAY_A6_PROVIDER_REPLAY_TRADER_FAILURE_REPORT_TOKEN=" + "a" * 64
    ) in rendered
    assert (
        "GATEWAY_A6_PROVIDER_REPLAY_TRADER_FAILURE_REQUESTED_BY=sub2api-hk"
        in rendered
    )
    assert "GATEWAY_A6_PROVIDER_REPLAY_TRADER_FAILURE_TIMEOUT_MS=2000" in rendered
    assert "A6_PANEL_PASSWORD" not in rendered


@pytest.mark.parametrize("token", ["", "short", "unsafe token", "x" * 257])
def test_renderer_rejects_invalid_trader_internal_token(token: str) -> None:
    with pytest.raises(ShadowEnvError, match="trader_internal_token_invalid"):
        render_environment(
            parse_environment_file(SOURCE),
            {_TRADER_INTERNAL_TOKEN_KEY: token},
        )


def test_renderer_rejects_missing_shared_secret() -> None:
    source = parse_environment_file(SOURCE)
    source.pop("TOTP_ENCRYPTION_KEY")

    with pytest.raises(ShadowEnvError, match="TOTP_ENCRYPTION_KEY"):
        render_environment(source)


def test_renderer_accepts_explicit_empty_loopback_redis_password() -> None:
    source = parse_environment_file(
        SOURCE.replace("REDIS_PASSWORD=redis-secret", "REDIS_PASSWORD=")
    )

    rendered = render_environment(source)

    assert "REDIS_PASSWORD=" not in rendered
    assert "REDIS_HOST=127.0.0.1" in rendered


def test_private_writer_is_atomic_and_mode_0600(tmp_path) -> None:
    destination = tmp_path / "private" / "shadow.env"

    write_private_environment(destination, "SERVER_HOST=127.0.0.1\n")
    write_private_environment(destination, "SERVER_HOST=127.0.0.1\nSERVER_PORT=48769\n")

    assert destination.read_text() == "SERVER_HOST=127.0.0.1\nSERVER_PORT=48769\n"
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600


def test_private_writer_rejects_existing_symlink(tmp_path) -> None:
    target = tmp_path / "target.env"
    target.write_text("unchanged\n")
    destination = tmp_path / "shadow.env"
    destination.symlink_to(target)

    with pytest.raises(ShadowEnvError, match="destination_invalid"):
        write_private_environment(destination, "SERVER_PORT=48769\n")

    assert target.read_text() == "unchanged\n"
