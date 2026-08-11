from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

SCRIPT_PATH = Path(__file__).parents[1] / "scripts" / "promote_funded_provider.py"
SPEC = importlib.util.spec_from_file_location("promote_funded_provider", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
script = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(script)


def test_parser_requires_explicit_provider_identity() -> None:
    args = script.parser().parse_args(
        ["--merchant", "12715", "--token", "16237", "--proxy-id", "32"]
    )

    assert args.apply is False
    assert args.stage == "all"
    assert args.priority == 1
    assert args.concurrency == 100


def test_dry_run_projects_role_and_never_applies(monkeypatch, capsys) -> None:
    calls: list[tuple[str, bool]] = []

    class FakeBindingExecutor:
        def __init__(self, *_args, **_kwargs):
            pass

        def execute(self, request, *, apply=False):
            calls.append((f"binding:{request.merchant_id}", apply))
            return SimpleNamespace(summary=lambda: {"action": "create", "pool_mode": True})

    monkeypatch.setenv("A6_TRADE_DATABASE_DSN", "dbname=fixture")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "promote_funded_provider.py",
            "--merchant",
            "12715",
            "--token",
            "16237",
            "--proxy-id",
            "32",
        ],
    )
    monkeypatch.setattr(script, "_private_text", lambda *_args: "admin-key")
    monkeypatch.setattr(script, "_proxy_map", lambda *_args: {11229: "http://proxy:23110"})
    monkeypatch.setattr(
        script,
        "_assign_provider_role",
        lambda *_args, **kwargs: calls.append(("role", kwargs["apply"]))
        or {"mode": "dry-run"},
    )
    monkeypatch.setattr(script, "PostgresProviderBindingStore", lambda *_args: object())
    monkeypatch.setattr(script, "ProjectedProviderRoleStore", lambda *_args: object())
    monkeypatch.setattr(script, "EnodotAdminClient", lambda *_args: object())
    monkeypatch.setattr(script, "ProviderBindingExecutor", FakeBindingExecutor)

    script.main()

    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "dry-run"
    assert payload["merchant_id"] == 12715
    assert calls == [("role", False), ("binding:12715", False)]
