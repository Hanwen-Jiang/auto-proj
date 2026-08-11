#!/usr/bin/env python3
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import shutil
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from a6api_auto.storage import SessionStore, read_private_json, write_private_json

UTC = timezone.utc


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(
        description="Repair a completed batch account whose proxy binding conflicted"
    )
    command.add_argument("--state-file", type=Path, required=True)
    command.add_argument("--proxy-map-file", type=Path, required=True)
    command.add_argument("--proxy-pool-file", type=Path, required=True)
    command.add_argument("--apply", action="store_true")
    return command


def fingerprint(proxy: str) -> str:
    return hashlib.sha256(proxy.encode()).hexdigest()[:12]


def timestamp() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


@contextmanager
def repair_lock(state_file: Path):
    lock_file = state_file.parent / "pending-proxy-repair.lock"
    descriptor = os.open(lock_file, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def private_backup(path: Path) -> Path:
    backup = path.with_name(f"{path.name}.before-pending-proxy-repair")
    if not backup.exists():
        shutil.copyfile(path, backup)
        os.chmod(backup, 0o600)
    return backup


def main() -> None:
    args = parser().parse_args()
    state_file = args.state_file.expanduser().resolve()
    proxy_map_file = args.proxy_map_file.expanduser().resolve()
    proxy_pool_file = args.proxy_pool_file.expanduser().resolve()
    with repair_lock(state_file):
        state = read_private_json(state_file)
        mapping = read_private_json(proxy_map_file)
        pool_info = proxy_pool_file.stat()
        if pool_info.st_mode & 0o077:
            raise RuntimeError("pending_proxy_repair_pool_permissions")
        pool = json.loads(proxy_pool_file.read_text(encoding="utf-8"))
        if (
            not isinstance(state, dict)
            or not isinstance(mapping, dict)
            or not isinstance(pool, list)
        ):
            raise RuntimeError("pending_proxy_repair_state_invalid")
        pending_path = Path(str(state.get("pending_session") or ""))
        if not pending_path.is_file():
            raise RuntimeError("pending_proxy_repair_session_missing")
        record = SessionStore(pending_path).load()
        user_id = int(record.user_id or 0)
        if (
            user_id < 1
            or record.status != "supplier_submitted"
            or int(record.supplier_id or 0) < 1
            or int(record.request_id or 0) < 1
        ):
            raise RuntimeError("pending_proxy_repair_session_incomplete")
        if any(int(item.get("user_id") or 0) == user_id for item in state.get("completed") or []):
            raise RuntimeError("pending_proxy_repair_already_completed")
        normalized_pool = [str(value).strip() for value in pool if str(value).strip()]
        if len(normalized_pool) != len(set(normalized_pool)):
            raise RuntimeError("pending_proxy_repair_pool_invalid")
        current_proxy = mapping.get(str(user_id))
        used_by_other = {
            str(value)
            for key, value in mapping.items()
            if str(key) != str(user_id)
        }
        allocated = {
            str(value)
            for value in state.get("allocated_proxy_fingerprints") or []
            if str(value)
        }
        if current_proxy is not None:
            selected = str(current_proxy)
            if selected not in normalized_pool or selected in used_by_other:
                raise RuntimeError("pending_proxy_repair_existing_binding_invalid")
        else:
            available = [
                proxy
                for proxy in normalized_pool
                if proxy not in used_by_other and fingerprint(proxy) not in allocated
            ]
            if not available:
                raise RuntimeError("pending_proxy_repair_pool_exhausted")
            selected = min(available, key=fingerprint)
        selected_fingerprint = fingerprint(selected)
        preview: dict[str, Any] = {
            "success": True,
            "mode": "applied" if args.apply else "dry-run",
            "user_id": user_id,
            "supplier_id": int(record.supplier_id or 0),
            "request_id": int(record.request_id or 0),
            "proxy_fingerprint": selected_fingerprint,
            "completed_before": len(state.get("completed") or []),
            "completed_after": len(state.get("completed") or []) + 1,
        }
        if not args.apply:
            print(json.dumps(preview, sort_keys=True))
            return
        private_backup(proxy_map_file)
        private_backup(state_file)
        mapping[str(user_id)] = selected
        write_private_json(proxy_map_file, mapping)
        pending_upstream = state.get("pending_upstream")
        if not isinstance(pending_upstream, dict):
            raise RuntimeError("pending_proxy_repair_upstream_missing")
        state.setdefault("completed", []).append(
            {
                "session_file": str(pending_path),
                "template_id": str(record.template_id or ""),
                "user_id": user_id,
                "request_id": int(record.request_id or 0),
                "completed_at": timestamp(),
                "proxy_fingerprint": selected_fingerprint,
                "upstream_base_url": str(pending_upstream.get("base_url") or ""),
                "upstream_key_ref": str(pending_upstream.get("key_ref") or ""),
            }
        )
        fingerprints = state.setdefault("allocated_proxy_fingerprints", [])
        if selected_fingerprint not in fingerprints:
            fingerprints.append(selected_fingerprint)
        state["last_proxy_fingerprint"] = selected_fingerprint
        state["pending_session"] = ""
        state["pending_retries"] = 0
        state["pending_upstream"] = None
        state["pending_proxy_fingerprint"] = ""
        state["updated_at"] = timestamp()
        write_private_json(state_file, state)
        print(json.dumps(preview, sort_keys=True))


if __name__ == "__main__":
    main()
