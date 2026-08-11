from __future__ import annotations

import json
import os
import shutil
import socket
import ssl
import stat
import subprocess
import time
import urllib.request
from contextlib import suppress
from copy import deepcopy
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .errors import A6AutoError
from .storage import ensure_private_directory, read_private_json, write_private_json


def _singbox_environment() -> dict[str, str]:
    return {
        **os.environ,
        "ENABLE_DEPRECATED_LEGACY_DNS_FAKEIP_OPTIONS": "true",
        "ENABLE_DEPRECATED_LEGACY_DNS_SERVERS": "true",
        "ENABLE_DEPRECATED_MISSING_DOMAIN_RESOLVER": "true",
        "ENABLE_DEPRECATED_OUTBOUND_DNS_RULE_ITEM": "true",
    }


def load_subscription_urls(path: Path) -> tuple[str, ...]:
    resolved = path.expanduser().resolve()
    try:
        info = resolved.stat()
        raw = resolved.read_text(encoding="utf-8")
    except OSError as exc:
        raise A6AutoError("proxy_subscription_file_unreadable") from exc
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
        raise A6AutoError("proxy_subscription_file_permissions")
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        parsed = [line.strip() for line in raw.splitlines() if line.strip()]
    if not isinstance(parsed, list) or not parsed or not all(isinstance(v, str) for v in parsed):
        raise A6AutoError("proxy_subscription_file_shape_invalid")
    values: list[str] = []
    seen: set[str] = set()
    for item in parsed:
        value = item.strip()
        url = urlsplit(value)
        if url.scheme != "https" or not url.hostname:
            raise A6AutoError("proxy_subscription_url_invalid")
        if value not in seen:
            values.append(value)
            seen.add(value)
    return tuple(values)


def sanitize_singbox_config(document: dict[str, Any], port: int) -> dict[str, Any]:
    if not isinstance(document.get("outbounds"), list) or not document["outbounds"]:
        raise A6AutoError("proxy_subscription_singbox_invalid")
    config = deepcopy(document)
    mixed = next(
        (
            item
            for item in config.get("inbounds", [])
            if isinstance(item, dict) and item.get("type") == "mixed"
        ),
        None,
    )
    inbound = deepcopy(mixed) if mixed else {"type": "mixed", "tag": "mixed-in"}
    for legacy_field in ("sniff", "sniff_override_destination", "domain_strategy"):
        inbound.pop(legacy_field, None)
    inbound["listen"] = "127.0.0.1"
    inbound["listen_port"] = port
    config["inbounds"] = [inbound]
    config.pop("experimental", None)
    urltests = [
        item
        for item in config["outbounds"]
        if isinstance(item, dict)
        and item.get("type") == "urltest"
        and item.get("tag")
        and isinstance(item.get("outbounds"), list)
        and item["outbounds"]
    ]
    if urltests:
        selected = max(urltests, key=lambda item: len(item["outbounds"]))
        route = config.get("route")
        if not isinstance(route, dict):
            route = {}
            config["route"] = route
        route["final"] = selected["tag"]
    return config


class SingBoxPool:
    def __init__(
        self,
        subscription_urls: tuple[str, ...],
        cache_dir: Path,
        *,
        executable: str = "sing-box",
        port_start: int = 19080,
        verify_tls: bool = True,
        startup_timeout: float = 20,
    ) -> None:
        if not subscription_urls:
            raise A6AutoError("proxy_subscription_pool_empty")
        self.subscription_urls = subscription_urls
        self.cache_dir = ensure_private_directory(cache_dir)
        self.executable = shutil.which(executable) or executable
        self.port_start = port_start
        self.verify_tls = verify_tls
        self.startup_timeout = startup_timeout
        self.processes: list[subprocess.Popen[bytes]] = []
        self.proxy_urls: tuple[str, ...] = ()

    def _fetch(self, url: str) -> dict[str, Any]:
        context = (
            ssl.create_default_context()
            if self.verify_tls
            else ssl._create_unverified_context()
        )
        request = urllib.request.Request(
            url,
            headers={"Accept": "application/json", "User-Agent": "sing-box/1.13.12"},
        )
        try:
            with urllib.request.urlopen(
                request,
                timeout=30,
                context=context,
            ) as response:
                document = json.loads(response.read())
        except Exception as exc:
            raise A6AutoError("proxy_subscription_fetch_failed") from exc
        if not isinstance(document, dict):
            raise A6AutoError("proxy_subscription_singbox_invalid")
        return document

    @staticmethod
    def _port_available(port: int) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            return sock.connect_ex(("127.0.0.1", port)) != 0

    def _next_port(self, starting: int, used: set[int]) -> int:
        for port in range(starting, min(starting + 1000, 65536)):
            if port not in used and self._port_available(port):
                return port
        raise A6AutoError("singbox_local_port_unavailable")

    def _config_path(self, index: int) -> Path:
        return self.cache_dir / f"subscription-{index:02d}.json"

    def _refresh_config(self, url: str, index: int, port: int) -> Path:
        path = self._config_path(index)
        try:
            document = self._fetch(url)
            config = sanitize_singbox_config(document, port)
            write_private_json(path, config)
        except A6AutoError:
            if not path.is_file():
                raise
            cached = read_private_json(path)
            write_private_json(path, sanitize_singbox_config(cached, port))
        return path

    def _check(self, path: Path) -> None:
        try:
            result = subprocess.run(
                [self.executable, "check", "-c", str(path)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=30,
                check=False,
                env=_singbox_environment(),
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise A6AutoError("singbox_check_failed") from exc
        if result.returncode != 0:
            raise A6AutoError("singbox_config_rejected")

    def _start(self, path: Path, port: int) -> subprocess.Popen[bytes]:
        try:
            process = subprocess.Popen(
                [self.executable, "run", "-c", str(path)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=_singbox_environment(),
            )
        except OSError as exc:
            raise A6AutoError("singbox_start_failed") from exc
        deadline = time.monotonic() + self.startup_timeout
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise A6AutoError("singbox_exited_during_startup")
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.settimeout(0.2)
                if sock.connect_ex(("127.0.0.1", port)) == 0:
                    return process
            time.sleep(0.1)
        process.terminate()
        with suppress(subprocess.TimeoutExpired):
            process.wait(timeout=3)
        raise A6AutoError("singbox_startup_timeout")

    def start(self) -> tuple[str, ...]:
        if self.processes:
            return self.proxy_urls
        used: set[int] = set()
        proxies: list[str] = []
        try:
            for index, url in enumerate(self.subscription_urls, 1):
                port = self._next_port(self.port_start + index - 1, used)
                used.add(port)
                path = self._refresh_config(url, index, port)
                self._check(path)
                self.processes.append(self._start(path, port))
                proxies.append(f"socks5://127.0.0.1:{port}")
        except Exception:
            self.close()
            raise
        self.proxy_urls = tuple(proxies)
        return self.proxy_urls

    def close(self) -> None:
        for process in reversed(self.processes):
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)
        self.processes.clear()
        self.proxy_urls = ()

    def __enter__(self) -> SingBoxPool:
        self.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()
