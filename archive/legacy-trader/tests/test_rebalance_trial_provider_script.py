from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

SCRIPT_PATH = Path(__file__).parents[1] / "scripts" / "rebalance_trial_provider_m19_to_m7.py"
SPEC = importlib.util.spec_from_file_location("rebalance_trial_provider_m19_to_m7", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
script = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(script)


def test_parser_defaults_to_full_dry_run_with_bounded_priority() -> None:
    args = script.parser().parse_args([])

    assert args.apply is False
    assert args.stage == "all"
    assert args.priority == 1
    assert script.OUTGOING_ACCOUNT_ID == 45896
    assert script.INCOMING_MERCHANT_ID == 7
    assert script.INCOMING_TOKEN_ID == 12465
    assert script.INCOMING_PROXY_ID == 7


def test_main_dry_run_never_passes_apply_true(monkeypatch, capsys) -> None:
    calls: list[tuple[str, bool]] = []

    class FakeRetirementExecutor:
        def __init__(self, *_args):
            pass

        def execute(self, _request, *, apply=False):
            calls.append(("retire-old", apply))
            return SimpleNamespace(summary=lambda: {"mode": "dry-run", "action": "update"})

    class FakeBindingExecutor:
        def __init__(self, *_args, **_kwargs):
            pass

        def execute(self, _request, *, apply=False):
            calls.append(("bind-new", apply))
            return SimpleNamespace(summary=lambda: {"mode": "dry-run", "action": "create"})

    monkeypatch.setenv("A6_TRADE_DATABASE_DSN", "dbname=fixture")
    monkeypatch.setattr(sys, "argv", ["rebalance_trial_provider_m19_to_m7.py"])
    monkeypatch.setattr(script, "_private_text", lambda *_args: "admin-key")
    monkeypatch.setattr(script, "_proxy_map", lambda *_args: {8085: "http://proxy:23086"})
    monkeypatch.setattr(script, "EnodotAdminClient", lambda *_args: object())
    monkeypatch.setattr(script, "PostgresProviderRetirementStore", lambda *_args: object())
    monkeypatch.setattr(script, "ProviderAccountRetirementExecutor", FakeRetirementExecutor)
    monkeypatch.setattr(script, "PostgresProviderBindingStore", lambda *_args: object())
    monkeypatch.setattr(script, "ProjectedProviderRoleStore", lambda *_args: object())
    monkeypatch.setattr(script, "ProviderBindingExecutor", FakeBindingExecutor)
    monkeypatch.setattr(
        script,
        "_switch_roles",
        lambda _factory, *, apply: calls.append(("switch-roles", apply)) or {"mode": "dry-run"},
    )

    script.main()

    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "dry-run"
    assert payload["selected_stage"] == "all"
    assert calls == [
        ("retire-old", False),
        ("switch-roles", False),
        ("bind-new", False),
    ]


def test_retire_only_stage_does_not_require_proxy_map(monkeypatch, capsys) -> None:
    class FakeRetirementExecutor:
        def __init__(self, *_args):
            pass

        def execute(self, _request, *, apply=False):
            return SimpleNamespace(summary=lambda: {"mode": "dry-run", "action": "update"})

    monkeypatch.setenv("A6_TRADE_DATABASE_DSN", "dbname=fixture")
    monkeypatch.setattr(
        sys,
        "argv",
        ["rebalance_trial_provider_m19_to_m7.py", "--stage", "retire-old"],
    )
    monkeypatch.setattr(script, "_private_text", lambda *_args: "admin-key")
    monkeypatch.setattr(
        script,
        "_proxy_map",
        lambda *_args: (_ for _ in ()).throw(AssertionError("proxy map should not load")),
    )
    monkeypatch.setattr(script, "EnodotAdminClient", lambda *_args: object())
    monkeypatch.setattr(script, "PostgresProviderRetirementStore", lambda *_args: object())
    monkeypatch.setattr(script, "ProviderAccountRetirementExecutor", FakeRetirementExecutor)
    monkeypatch.setattr(script, "PostgresProviderBindingStore", lambda *_args: object())

    script.main()

    payload = json.loads(capsys.readouterr().out)
    assert set(payload["stages"]) == {"retire-old"}
