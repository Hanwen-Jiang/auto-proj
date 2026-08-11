from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin

from .client import _normalize_models
from .errors import A6AutoError


@dataclass(frozen=True)
class UpstreamSelection:
    base_url: str
    api_key: str = field(repr=False)
    key_ref: str


def fetch_upstream_models(base_url: str, api_key: str, *, session: Any = None) -> list[str]:
    if session is None:
        try:
            from curl_cffi import requests

            session = requests.Session()
        except Exception as exc:
            raise A6AutoError("http_dependency_unavailable") from exc
    root = base_url.rstrip("/")
    paths = ("models",) if root.endswith("/v1") else ("models", "v1/models")
    for path in paths:
        try:
            response = session.get(
                urljoin(f"{root}/", path),
                headers={"Accept": "application/json", "Authorization": f"Bearer {api_key}"},
                timeout=30,
                allow_redirects=False,
                impersonate="chrome131",
            )
            document = response.json()
        except Exception:
            continue
        if int(getattr(response, "status_code", 0)) != 200 or not isinstance(document, Mapping):
            continue
        values = document.get("data")
        if isinstance(values, list):
            return _normalize_models(values)
    raise A6AutoError("upstream_models_unavailable")
