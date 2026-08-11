from __future__ import annotations

import importlib.util
import json
import stat
from pathlib import Path

SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "provision_a6_transfer_asset_keys.py"
)
SPEC = importlib.util.spec_from_file_location("provision_a6_transfer_asset_keys", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


def test_select_token_name_uses_corpus_and_avoids_existing_names() -> None:
    first = module.select_token_name(10492, reserved_names=set(), remote_names=set())
    second = module.select_token_name(
        10492,
        reserved_names={first},
        remote_names=set(),
    )
    third = module.select_token_name(
        10492,
        reserved_names=set(),
        remote_names={first},
    )

    assert first in module.token_name_corpus()
    assert second in module.token_name_corpus()
    assert second != first
    assert third == second


def test_render_accounts_only_schedules_ready_transfer_assets() -> None:
    document = module.new_manifest(session_dir=Path("/sessions"), model="gpt-5.6-sol")
    document["assets"] = {
        "2": {
            "state": "planned",
            "token_name": "Codex",
        },
        "1": {
            "state": "ready",
            "token_id": 77,
            "token_name": "GPT",
            "session_file": "/sessions/session-1.json",
            "api_key": "sk-secret",
            "proxy": "http://127.0.0.1:23001",
        },
    }

    module.render_accounts(document)

    assert document["accounts"] == [
        {
            "name": "user-1",
            "new_api_user": 1,
            "token_id": 77,
            "token_name": "GPT",
            "session_file": "/sessions/session-1.json",
            "api_key": "sk-secret",
            "proxy": "http://127.0.0.1:23001",
            "duty": module.DUTY,
        }
    ]


def test_write_private_json_is_atomic_and_private(tmp_path: Path) -> None:
    output = tmp_path / "private" / "pool.json"

    module.write_private_json(output, {"schema_version": 1, "api_key": "sk-secret"})

    assert json.loads(output.read_text()) == {
        "schema_version": 1,
        "api_key": "sk-secret",
    }
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert stat.S_IMODE(output.parent.stat().st_mode) == 0o700
