from __future__ import annotations

import hashlib
import secrets
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from .client import A6ApiClient
from .errors import A6AutoError
from .mail import QQMailbox
from .merchant_policy import DEFAULT_OPERATING_GROUP, normalize_operating_group
from .models import MerchantTemplate, SessionRecord
from .storage import SessionStore, ensure_private_directory, read_private_json
from .templates import choose_template
from .token_names import generate_anonymous_token_name
from .upstream import fetch_upstream_models

UTC = timezone.utc

def utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _generated_password(length: int = 16) -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789"
    return "".join(secrets.choice(alphabet) for _ in range(length))


def load_qq_addresses(path: Path) -> tuple[str, ...]:
    try:
        lines = path.expanduser().read_text(encoding="utf-8-sig").splitlines()
    except OSError as exc:
        raise A6AutoError("qq_pool_unreadable") from exc
    values: list[str] = []
    seen: set[str] = set()
    for line in lines:
        email = line.strip().lower()
        if email.endswith("@qq.com") and " " not in email and email not in seen:
            values.append(email)
            seen.add(email)
    if not values:
        raise A6AutoError("qq_pool_empty")
    return tuple(values)


def session_filename(email: str) -> str:
    digest = hashlib.sha256(email.encode()).hexdigest()[:20]
    return f"session-{digest}.json"


def allocate_session(
    qq_file: Path,
    state_dir: Path,
    *,
    explicit_email: str = "",
    template_id: str = "",
    template_workbook: Path | None = None,
    operating_group: str = DEFAULT_OPERATING_GROUP,
) -> tuple[SessionRecord, SessionStore, MerchantTemplate]:
    root = ensure_private_directory(state_dir)
    accounts_dir = ensure_private_directory(root / "sessions")
    addresses = load_qq_addresses(qq_file)
    used: set[str] = set()
    for path in accounts_dir.glob("session-*.json"):
        try:
            value = read_private_json(path)
        except A6AutoError:
            continue
        email = str(value.get("email") or "").lower()
        if email:
            used.add(email)
    if explicit_email:
        email = explicit_email.strip().lower()
        if email not in addresses:
            raise A6AutoError("email_not_in_qq_pool")
    else:
        available = [value for value in addresses if value not in used]
        if not available:
            raise A6AutoError("qq_pool_exhausted")
        email = secrets.choice(available)
    store = SessionStore(accounts_dir / session_filename(email))
    normalized_group = normalize_operating_group(operating_group)
    if store.exists():
        record = store.load()
        if record.operating_group != normalized_group:
            raise A6AutoError("session_operating_group_mismatch")
        template = choose_template(
            record.template_id,
            workbook_path=template_workbook,
        )
        return record, store, template
    template = choose_template(template_id, workbook_path=template_workbook)
    now = utc_now()
    record = SessionRecord(
        schema_version=1,
        origin="https://a6api.com",
        email=email,
        username=email.split("@", 1)[0],
        password=_generated_password(),
        token_name=generate_anonymous_token_name(),
        template_id=template.template_id,
        operating_group=normalized_group,
        created_at=now,
        updated_at=now,
    )
    store.save(record)
    return record, store, template


class Workflow:
    def __init__(
        self,
        client: A6ApiClient,
        mailbox: QQMailbox,
        store: SessionStore,
        *,
        model_fetcher: Callable[[str, str], list[str]] = fetch_upstream_models,
    ) -> None:
        self.client = client
        self.mailbox = mailbox
        self.store = store
        self.model_fetcher = model_fetcher

    def _save(self, record: SessionRecord) -> None:
        record.updated_at = utc_now()
        record.cookies = self.client.export_cookies()
        self.store.save(record)

    def ensure_authenticated(self, record: SessionRecord) -> bool:
        expected_user_id = record.user_id
        self.client.import_cookies(record.cookies)
        current = self.client.self_user(record.user_id) if record.cookies else None
        relogged = current is None
        if current is None and record.status != "allocated":
            current = self.client.login(record.username, record.password)
            if current is None and record.username != record.email:
                current = self.client.login(record.email, record.password)
        if current is None and record.status == "allocated":
            seen = self.mailbox.snapshot(record.email)
            try:
                self.client.send_verification(record.email)
            except A6AutoError as exc:
                if str(exc) == "email_already_registered":
                    current = self.client.login(record.username, record.password)
                    if current is None and record.username != record.email:
                        current = self.client.login(record.email, record.password)
                    if current is None:
                        raise A6AutoError("session_relogin_failed") from None
                elif str(exc) == "a6_transport_failed":
                    seen = set()
                else:
                    raise
            if current is None:
                code = self.mailbox.poll_code(
                    record.email,
                    seen,
                    timeout_seconds=300,
                    interval_seconds=5,
                )
                registered = False
                try:
                    self.client.register(record.username, record.email, record.password, code)
                    registered = True
                except A6AutoError:
                    # Recover a successful registration whose response was lost before persistence.
                    current = self.client.login(record.username, record.password)
                    if current is None and record.username != record.email:
                        try:
                            self.client.register(record.email, record.email, record.password, code)
                            record.username = record.email
                            registered = True
                        except A6AutoError:
                            current = self.client.login(record.email, record.password)
                if registered:
                    record.status = "registered"
                    self._save(record)
                    current = self.client.login(record.username, record.password)
                if current is None and record.username != record.email:
                    current = self.client.login(record.email, record.password)
        if current is None:
            raise A6AutoError("session_relogin_failed")
        user_id = int(current.get("id") or 0)
        if user_id < 1:
            raise A6AutoError("session_user_id_missing")
        if expected_user_id is not None and user_id != expected_user_id:
            raise A6AutoError("session_user_identity_mismatch")
        record.user_id = user_id
        record.username = str(current.get("username") or record.username)
        record.status = (
            "supplier_submitted"
            if record.supplier_id is not None
            else "authenticated"
        )
        self._save(record)
        return relogged

    def execute(
        self,
        record: SessionRecord,
        template: MerchantTemplate,
        *,
        upstream_base_url: str,
        upstream_api_key: str,
    ) -> dict[str, object]:
        self.ensure_authenticated(record)
        if record.user_id is None:
            raise A6AutoError("session_user_id_missing")
        if not record.a6api_key:
            record.a6api_key = self.client.ensure_one_key(record.user_id, record.token_name)
            self._save(record)
        if not record.models:
            try:
                record.models = self.model_fetcher(upstream_base_url, upstream_api_key)
            except A6AutoError:
                record.models = self.client.fetch_models_via_a6(
                    record.user_id,
                    upstream_base_url,
                    upstream_api_key,
                )
            record.upstream_base_url = upstream_base_url.rstrip("/")
            record.upstream_api_key_last4 = upstream_api_key[-4:]
            self._save(record)
        if record.request_id is None:
            existing_requests = self.client.supplier_requests(record.user_id)
            existing = [
                item
                for item in existing_requests
                if str(item.get("name") or "") == template.channel_name
                and str(item.get("base_url") or "").rstrip("/")
                == upstream_base_url.rstrip("/")
            ]
            if len(existing) == 1:
                record.request_id = int(existing[0].get("id") or 0) or None
                record.supplier_id = int(existing[0].get("supplier_id") or 0) or None
                record.status = "supplier_submitted"
                self._save(record)
        if record.request_id is None:
            payload = {
                "supplier": {
                    "name": template.supplier_name,
                    "contact_info": record.email,
                    "description": template.supplier_description,
                },
                "channel": {
                    "name": template.channel_name,
                    "nickname": template.channel_nickname,
                    "channel_type": template.channel_type,
                    "base_url": upstream_base_url.rstrip("/"),
                    "api_key": upstream_api_key,
                    "models": ",".join(record.models),
                    "description": template.channel_description,
                },
            }
            result = self.client.apply_supplier(record.user_id, payload)
            record.supplier_id = int(result.get("supplier_id") or 0) or None
            record.request_id = int(result.get("request_id") or 0) or None
            if record.request_id is None:
                raise A6AutoError("supplier_request_id_missing")
            record.status = "supplier_submitted"
            self._save(record)
        requests = self.client.supplier_requests(record.user_id)
        matched = [item for item in requests if int(item.get("id") or 0) == record.request_id]
        if len(matched) != 1:
            raise A6AutoError("supplier_request_reconciliation_failed")
        return {
            "email": record.email,
            "user_id": record.user_id,
            "key_created": bool(record.a6api_key),
            "model_count": len(record.models),
            "supplier_id": record.supplier_id,
            "request_id": record.request_id,
            "request_status": matched[0].get("status"),
            "session_file": str(self.store.path),
        }

    def refresh_session(self, record: SessionRecord) -> dict[str, object]:
        self.ensure_authenticated(record)
        return {
            "email": record.email,
            "user_id": record.user_id,
            "cookie_names": sorted(record.cookies),
            "session_file": str(self.store.path),
        }
