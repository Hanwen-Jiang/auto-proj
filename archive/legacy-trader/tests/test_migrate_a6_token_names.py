from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from a6api_auto.token_name_migration import TokenNameMigrationError

SCRIPT_PATH = Path(__file__).parents[1] / "scripts" / "migrate_a6_token_names.py"
SPEC = importlib.util.spec_from_file_location("migrate_a6_token_names", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
script = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(script)


def test_parser_defaults_to_read_only_preview() -> None:
    args = script.parser().parse_args([])

    assert args.apply is False
    assert args.all_token_names is False
    assert args.allow_direct_missing_proxy is False
    assert args.confirm == ""
    assert args.database_dsn_env == "A6_TRADE_DATABASE_DSN"


def test_apply_requires_exact_confirmation_before_reading_environment() -> None:
    args = script.parser().parse_args(["--apply", "--confirm", "wrong"])

    with pytest.raises(TokenNameMigrationError, match="token_name_migration_confirmation_required"):
        script.run(args)


def test_all_token_names_mode_is_explicit() -> None:
    args = script.parser().parse_args(
        ["--all-token-names", "--allow-direct-missing-proxy"]
    )

    assert args.all_token_names is True
    assert args.allow_direct_missing_proxy is True
