from __future__ import annotations

import secrets
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

from .errors import A6AutoError
from .storage import read_private_json, write_private_json
from .upstream import UpstreamSelection

GROUP_NAME_ALIASES: dict[str, tuple[str, ...]] = {
    "TPA6API": ("TPA6API", "ToA6API"),
}


class EnodotGroupKeyProvider:
    def __init__(
        self,
        base_url: str,
        admin_key: str,
        group_name: str,
        domains: tuple[str, ...],
        *,
        token_file: Path | None = None,
        session: Any = None,
        choice: Callable[[Any], Any] = secrets.choice,
    ) -> None:
        if bool(admin_key) == bool(token_file):
            raise A6AutoError("enodot_admin_credential_required")
        if not group_name or not domains:
            raise A6AutoError("enodot_group_config_invalid")
        self.base_url = base_url.rstrip("/")
        self.admin_key = admin_key
        self.token_file = token_file.expanduser().resolve() if token_file else None
        self.group_name = group_name
        self.domains = domains
        self.choice = choice
        if session is None:
            try:
                from curl_cffi import requests

                session = requests.Session()
            except Exception as exc:
                raise A6AutoError("http_dependency_unavailable") from exc
        self.session = session

    def _auth_headers(self) -> dict[str, str]:
        if self.admin_key:
            return {"x-api-key": self.admin_key}
        if self.token_file is None:
            raise A6AutoError("enodot_admin_credential_required")
        tokens = read_private_json(self.token_file)
        access_token = str(tokens.get("access_token") or "")
        if not access_token:
            raise A6AutoError("enodot_admin_token_invalid")
        return {"Authorization": f"Bearer {access_token}"}

    def _refresh_tokens(self) -> None:
        if self.token_file is None:
            raise A6AutoError("enodot_admin_token_refresh_unavailable")
        current = read_private_json(self.token_file)
        refresh_token = str(current.get("refresh_token") or "")
        if not refresh_token:
            raise A6AutoError("enodot_admin_token_invalid")
        try:
            response = self.session.post(
                urljoin(f"{self.base_url}/", "/api/v1/auth/refresh".lstrip("/")),
                headers={"Accept": "application/json", "Content-Type": "application/json"},
                json={"refresh_token": refresh_token},
                timeout=30,
                allow_redirects=False,
                impersonate="chrome131",
            )
            document = response.json()
        except Exception as exc:
            raise A6AutoError("enodot_admin_token_refresh_failed") from exc
        data = document.get("data") if isinstance(document, Mapping) else None
        if (
            int(getattr(response, "status_code", 0)) != 200
            or not isinstance(document, Mapping)
            or document.get("code") != 0
            or not isinstance(data, Mapping)
            or not data.get("access_token")
            or not data.get("refresh_token")
        ):
            raise A6AutoError("enodot_admin_token_refresh_rejected")
        write_private_json(
            self.token_file,
            {
                "schema_version": 1,
                "access_token": str(data["access_token"]),
                "refresh_token": str(data["refresh_token"]),
                "expires_at": time.time() + float(data.get("expires_in") or 0),
            },
        )

    def _get(self, path: str, **params: Any) -> Any:
        response = None
        try:
            for attempt in range(2):
                response = self.session.get(
                    urljoin(f"{self.base_url}/", path.lstrip("/")),
                    headers={"Accept": "application/json", **self._auth_headers()},
                    params=params or None,
                    timeout=30,
                    allow_redirects=False,
                    impersonate="chrome131",
                )
                if int(getattr(response, "status_code", 0)) != 401 or attempt > 0:
                    break
                self._refresh_tokens()
            document = response.json()
        except Exception as exc:
            raise A6AutoError("enodot_admin_transport_failed") from exc
        if (
            int(getattr(response, "status_code", 0)) != 200
            or not isinstance(document, Mapping)
            or document.get("code") != 0
        ):
            raise A6AutoError("enodot_admin_request_rejected")
        return document.get("data")

    def _group_id(self) -> int:
        data = self._get("/api/v1/admin/groups/all")
        if not isinstance(data, list):
            raise A6AutoError("enodot_group_response_invalid")
        active = [
            item
            for item in data
            if isinstance(item, Mapping) and str(item.get("status") or "active") == "active"
        ]
        candidates = GROUP_NAME_ALIASES.get(self.group_name, (self.group_name,))
        matches = [
            item
            for item in active
            if str(item.get("name") or "").strip() == self.group_name
        ]
        if not matches and len(candidates) > 1:
            matches = [
                item
                for item in active
                if str(item.get("name") or "").strip() in candidates
            ]
        if len(matches) != 1:
            raise A6AutoError("enodot_group_not_unique")
        group_id = int(matches[0].get("id") or 0)
        if group_id < 1:
            raise A6AutoError("enodot_group_response_invalid")
        return group_id

    def _active_keys(self, group_id: int) -> list[Mapping[str, Any]]:
        items: list[Any] = []
        page = 1
        while True:
            data = self._get(
                f"/api/v1/admin/groups/{group_id}/api-keys",
                page=page,
                page_size=200,
            )
            if not isinstance(data, Mapping) or not isinstance(data.get("items"), list):
                raise A6AutoError("enodot_group_keys_response_invalid")
            items.extend(data["items"])
            pages = int(data.get("pages") or 1)
            if page >= pages:
                break
            page += 1
        keys = [
            item
            for item in items
            if isinstance(item, Mapping)
            and str(item.get("status") or "") == "active"
            and int(item.get("id") or 0) > 0
            and bool(str(item.get("key") or "").strip())
        ]
        if not keys:
            raise A6AutoError("enodot_group_has_no_active_keys")
        return keys

    def select(self, pending: Mapping[str, Any] | None = None) -> UpstreamSelection:
        group_id = self._group_id()
        keys = self._active_keys(group_id)
        if pending:
            base_url = str(pending.get("base_url") or "").rstrip("/")
            key_ref = str(pending.get("key_ref") or "")
            if base_url not in self.domains or not key_ref:
                raise A6AutoError("pending_upstream_selection_invalid")
            matches = [item for item in keys if str(item.get("id") or "") == key_ref]
            if len(matches) != 1:
                raise A6AutoError("pending_upstream_key_unavailable")
            selected = matches[0]
        else:
            base_url = self.choice(self.domains)
            selected = self.choice(keys)
            key_ref = str(selected["id"])
        return UpstreamSelection(
            base_url=base_url,
            api_key=str(selected["key"]).strip(),
            key_ref=key_ref,
        )
