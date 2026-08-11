from __future__ import annotations

import importlib.util
import json
from pathlib import Path


def load_script(name: str):
    path = Path(__file__).parents[1] / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


check_a6_batch_incomplete = load_script("check_a6_batch_incomplete")
command = load_script("reconcile_a6_merchant_recovery")


def test_command_is_cold_until_explicitly_enabled(tmp_path) -> None:
    args = command.parser().parse_args(
        [
            "--sessions-dir",
            str(tmp_path / "sessions"),
            "--proxy-map-file",
            str(tmp_path / "map.json"),
            "--proxy-pool-file",
            str(tmp_path / "pool.json"),
            "--apply",
        ]
    )
    calls: list[object] = []

    result = command.execute(
        args,
        environ={},
        connector=lambda *_args, **_kwargs: calls.append("database"),
    )

    assert result["mode"] == "disabled"
    assert calls == []


def test_batch_condition_runs_only_while_target_is_incomplete(tmp_path) -> None:
    state = tmp_path / "batch.json"
    state.write_text(
        json.dumps({"target_count": 2, "completed": [{"user_id": 1}]}),
        encoding="utf-8",
    )
    state.chmod(0o600)

    assert check_a6_batch_incomplete.batch_incomplete(state) is True
    payload = json.loads(state.read_text())
    payload["completed"].append({"user_id": 2})
    state.write_text(json.dumps(payload), encoding="utf-8")
    state.chmod(0o600)
    assert check_a6_batch_incomplete.batch_incomplete(state) is False


def test_batch_condition_reports_invalid_state_as_systemd_failure(tmp_path) -> None:
    state = tmp_path / "batch.json"
    state.write_text(json.dumps({"target_count": 2, "completed": "invalid"}))
    state.chmod(0o600)

    assert check_a6_batch_incomplete.main(["--state-file", str(state)]) == 255
