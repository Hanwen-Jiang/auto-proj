from pathlib import Path

import pytest

from a6api_auto.errors import A6AutoError
from a6api_auto.singbox import SingBoxPool, load_subscription_urls, sanitize_singbox_config


def test_subscription_file_is_private_and_deduplicated(tmp_path: Path) -> None:
    path = tmp_path / "subscriptions.secret"
    path.write_text(
        "https://one.test/token\nhttps://two.test/subscription\nhttps://one.test/token\n"
    )
    path.chmod(0o600)

    assert load_subscription_urls(path) == (
        "https://one.test/token",
        "https://two.test/subscription",
    )

    path.chmod(0o644)
    with pytest.raises(A6AutoError, match="proxy_subscription_file_permissions"):
        load_subscription_urls(path)


def test_sanitize_keeps_outbounds_and_only_one_local_mixed_inbound() -> None:
    source = {
        "inbounds": [
            {"type": "tun", "tag": "tun-in"},
            {
                "type": "mixed",
                "tag": "mixed-in",
                "listen": "0.0.0.0",
                "listen_port": 7891,
                "sniff": True,
                "sniff_override_destination": True,
                "domain_strategy": "prefer_ipv4",
            },
        ],
        "outbounds": [
            {"type": "vless", "tag": "node", "server": "node.test"},
            {"type": "urltest", "tag": "auto", "outbounds": ["node"]},
        ],
        "experimental": {"clash_api": {"external_controller": "127.0.0.1:9090"}},
        "route": {"final": "node"},
    }

    sanitized = sanitize_singbox_config(source, 19080)

    assert sanitized["inbounds"] == [
        {"type": "mixed", "tag": "mixed-in", "listen": "127.0.0.1", "listen_port": 19080}
    ]
    assert sanitized["outbounds"] == source["outbounds"]
    assert sanitized["route"] == {"final": "auto"}
    assert "experimental" not in sanitized
    assert source["inbounds"][1]["listen_port"] == 7891
    assert source["route"] == {"final": "node"}


def test_sanitize_can_add_mixed_inbound() -> None:
    sanitized = sanitize_singbox_config(
        {"inbounds": [{"type": "tun"}], "outbounds": [{"type": "direct"}]},
        19081,
    )

    assert sanitized["inbounds"] == [
        {"type": "mixed", "tag": "mixed-in", "listen": "127.0.0.1", "listen_port": 19081}
    ]


def test_singbox_pool_exposes_local_socks5_urls(tmp_path: Path) -> None:
    pool = SingBoxPool(
        ("https://subscription-one.test/token", "https://subscription-two.test/token"),
        tmp_path,
        executable="sing-box",
        port_start=19080,
    )
    seen_ports: list[int] = []

    class DummyProcess:
        def poll(self):
            return None

        def terminate(self):
            return None

        def wait(self, timeout=None):
            return 0

    pool._refresh_config = lambda url, index, port: tmp_path / f"subscription-{index:02d}.json"  # type: ignore[method-assign]
    pool._check = lambda path: None  # type: ignore[method-assign]
    pool._next_port = lambda candidate, used: candidate  # type: ignore[method-assign]

    def fake_start(path: Path, port: int):
        seen_ports.append(port)
        return DummyProcess()

    pool._start = fake_start  # type: ignore[method-assign]
    try:
        assert pool.start() == (
            "socks5://127.0.0.1:19080",
            "socks5://127.0.0.1:19081",
        )
        assert seen_ports == [19080, 19081]
    finally:
        pool.close()
