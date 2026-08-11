from __future__ import annotations

import hashlib
import re
import time
from collections.abc import Callable, Mapping
from email.utils import parseaddr
from typing import Any

from .errors import A6AutoError

QQ_MAIL_API_URL = "https://mail-api.zeabur.app/check"
A6_SENDER_NAME = "a6api"
A6_SENDER_EMAIL = "noreply@notify.a6api.com"
A6_SUBJECT = "A6API 邮箱验证码"
MAX_MAILS = 200


def _record_id(record: Mapping[str, Any]) -> str:
    if record.get("mail_id") not in (None, ""):
        return str(record["mail_id"])
    stable = "\x00".join(str(record.get(key) or "") for key in ("sender", "subject", "time"))
    return hashlib.sha256(stable.encode()).hexdigest()


def extract_a6_code(record: Mapping[str, Any]) -> str:
    display_name, address = parseaddr(str(record.get("sender") or ""))
    if display_name.strip().casefold() != A6_SENDER_NAME or address.lower() != A6_SENDER_EMAIL:
        return ""
    if str(record.get("subject") or "").strip() != A6_SUBJECT:
        return ""
    text = " ".join(str(record.get(key) or "") for key in ("subject", "content", "text", "html"))
    patterns = (
        r"(?:验证码|校验码|动态码)\s*(?:为|是)?\s*[:：-]?\s*([A-Za-z0-9]{6,8})",
        r"(?:verification\s*code|verify\s*code|otp)\s*(?:is)?\s*[:：-]?\s*([A-Za-z0-9]{6,8})",
    )
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return match.group(1)
    return ""


class QQMailbox:
    def __init__(
        self,
        endpoint: str = QQ_MAIL_API_URL,
        *,
        requester: Callable[[str, dict[str, str]], Mapping[str, Any]] | None = None,
    ) -> None:
        self.endpoint = endpoint
        self._requester = requester

    def _fetch(self, email: str) -> list[Mapping[str, Any]]:
        if self._requester is None:
            try:
                from curl_cffi import requests

                response = requests.post(
                    self.endpoint,
                    json={"email": email},
                    timeout=30,
                    impersonate="chrome131",
                )
                document = response.json()
            except Exception as exc:
                raise A6AutoError("qq_mail_request_failed") from exc
        else:
            document = self._requester(self.endpoint, {"email": email})
        if not isinstance(document, Mapping) or document.get("success") is not True:
            raise A6AutoError("qq_mail_response_rejected")
        mails = document.get("mails")
        if isinstance(mails, Mapping):
            if str(mails.get("error") or "").strip():
                raise A6AutoError("qq_mailbox_unavailable")
            raise A6AutoError("qq_mail_response_invalid")
        if not isinstance(mails, list):
            raise A6AutoError("qq_mail_response_invalid")
        records = [item for item in mails if isinstance(item, Mapping)]
        if len(records) > MAX_MAILS:
            records = sorted(
                records,
                key=lambda item: (
                    int(item.get("mail_id") or 0),
                    str(item.get("time") or ""),
                ),
                reverse=True,
            )[:MAX_MAILS]
        return records

    def snapshot(self, email: str) -> set[str]:
        return {_record_id(item) for item in self._fetch(email)}

    def poll_code(
        self,
        email: str,
        seen: set[str],
        *,
        timeout_seconds: float,
        interval_seconds: float,
    ) -> str:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            try:
                records = sorted(
                    self._fetch(email),
                    key=lambda item: (
                        int(item.get("mail_id") or 0),
                        str(item.get("time") or ""),
                    ),
                    reverse=True,
                )
            except A6AutoError:
                time.sleep(max(3.0, interval_seconds))
                continue
            for record in records:
                if _record_id(record) in seen:
                    continue
                code = extract_a6_code(record)
                if code:
                    return code
            time.sleep(max(3.0, interval_seconds))
        raise A6AutoError("qq_otp_timeout")
