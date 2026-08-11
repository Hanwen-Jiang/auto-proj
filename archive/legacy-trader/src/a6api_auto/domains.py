from __future__ import annotations

import json
from importlib.resources import files
from pathlib import Path
from urllib.parse import urlsplit

from .errors import A6AutoError


def _normalize_domain(value: str) -> str:
    raw = value.strip()
    if not raw:
        raise A6AutoError("upstream_domain_invalid")
    candidate = raw if "://" in raw else f"https://{raw}"
    parsed = urlsplit(candidate)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise A6AutoError("upstream_domain_invalid")
    try:
        port = parsed.port
    except ValueError as exc:
        raise A6AutoError("upstream_domain_invalid") from exc
    authority = parsed.hostname
    if port:
        authority = f"{authority}:{port}"
    return f"{parsed.scheme}://{authority}"


def load_upstream_domains(path: Path | None = None) -> tuple[str, ...]:
    try:
        if path is None:
            raw = files("a6api_auto").joinpath("data/upstream_domains.json").read_text(
                encoding="utf-8"
            )
        else:
            raw = path.expanduser().read_text(encoding="utf-8")
    except OSError as exc:
        raise A6AutoError("upstream_domain_file_unreadable") from exc
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        parsed = [line.strip() for line in raw.splitlines() if line.strip()]
    if not isinstance(parsed, list) or not parsed or not all(isinstance(v, str) for v in parsed):
        raise A6AutoError("upstream_domain_file_shape_invalid")
    values: list[str] = []
    seen: set[str] = set()
    for item in parsed:
        normalized = _normalize_domain(item)
        if normalized not in seen:
            values.append(normalized)
            seen.add(normalized)
    if not values:
        raise A6AutoError("upstream_domain_file_shape_invalid")
    return tuple(values)
