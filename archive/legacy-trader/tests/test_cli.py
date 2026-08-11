from __future__ import annotations

import argparse
import hashlib
import json
import shlex
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from a6api_auto import cli
from a6api_auto.batch import BatchConfig, SlowBatchRunner, _proxy_fingerprint
from a6api_auto.cli import (
    _database_dsn,
    _merchant_text_map,
    _proxy_map,
    _proxy_pool,
    batch_command,
)
from a6api_auto.errors import A6AutoError
from a6api_auto.storage import SessionStore, write_private_json
from a6api_auto.trade_collector import CollectorWatermarks
from a6api_auto.trade_roles import (
    RoleAssignmentDecision,
    RoleAssignmentResult,
)
from a6api_auto.trade_seller_orchestration import SellerPricing
from a6api_auto.trade_withdraw_accounts import WithdrawBatchResult


def private_file(path: Path, value: str) -> Path:
    path.write_text(value)
    path.chmod(0o600)
    return path


def trade_session_file(path: Path, number: int, *, status: str = "supplier_submitted") -> Path:
    write_private_json(
        path,
        {
            "schema_version": 1,
            "origin": "https://fixture.invalid",
            "email": f"private-{number}@fixture.invalid",
            "username": f"private-{number}",
            "password": f"password-secret-{number}",
            "cookies": {"session": f"cookie-secret-{number}"},
            "a6api_key": f"sk-secret-{number}",
            "user_id": 1000 + number,
            "supplier_id": 2000 + number,
            "status": status,
        },
    )
    return path


def test_cached_seller_client_imports_refreshed_cookie_without_live_authentication() -> None:
    context = SimpleNamespace(a6_user_id=1001, supplier_id=2001)
    session = SimpleNamespace(
        user_id=1001,
        supplier_id=2001,
        origin="https://fixture.invalid",
        cookies={"session": "cookie-secret"},
    )
    captured: dict[str, object] = {}

    class Client:
        def import_cookies(self, cookies):
            captured["cookies"] = dict(cookies)

    client = cli.cached_seller_client(
        context,
        session,
        proxy_by_user_id={1001: "http://127.0.0.1:23081"},
        client_factory=lambda record, proxy: (
            captured.update(record=record, proxy=proxy) or Client()
        ),
    )

    assert isinstance(client, Client)
    assert captured == {
        "record": session,
        "proxy": "http://127.0.0.1:23081",
        "cookies": {"session": "cookie-secret"},
    }


@pytest.mark.parametrize(
    ("unit_name", "command_name", "partial_result"),
    (
        (
            "a6-trader-seller-retirement",
            "trade-retire-sellers-once",
            {
                "success": False,
                "action": "retry_scheduled",
                "merchant_id": 7,
                "error_class": "fixture_retryable",
            },
        ),
        (
            "a6-trader-seller-pilot",
            "trade-apply-seller-price-band",
            {"success": False, "mode": "applied", "plan": {}, "merchants": [{}]},
        ),
        (
            "a6-trader-pin",
            "trade-pin-providers-once",
            {
                "success": False,
                "assignments": [
                    {"action": "pin", "applied": True, "confirmed": True},
                    {"action": "error", "applied": False, "confirmed": False},
                ],
            },
        ),
        (
            "a6-trader-seller-channel-onboard",
            "trade-auto-onboard-seller-channels",
            {
                "success": False,
                "operation": "auto_onboard_seller_channels",
                "mode": "applied",
                "failed": 1,
                "targets": [{}],
            },
        ),
        (
            "a6-trader-channel-growth",
            "trade-reconcile-channel-growth-once",
            {
                "success": False,
                "mode": "failed",
                "action_id": 11,
                "error_class": "fixture_retryable",
            },
        ),
    ),
)
def test_gated_systemd_exec_start_parses_and_normalizes_only_structured_partial_results(
    unit_name: str,
    command_name: str,
    partial_result: dict[str, object],
) -> None:
    service = Path(f"deploy/systemd/{unit_name}.service").read_text()
    exec_start = next(line for line in service.splitlines() if line.startswith("ExecStart="))
    words = shlex.split(exec_start.removeprefix("ExecStart="))
    executable_index = next(
        (
            index
            for index, value in enumerate(words)
            if value.endswith("/a6api-auto") or value == "a6api_auto.cli"
        )
    )
    args = cli.parser().parse_args(words[executable_index + 1 :])

    assert args.command == command_name
    assert args.systemd_accept_partial_result is True
    assert cli._result_exit_code(args, partial_result) == 0
    assert cli._result_exit_code(args, {"success": False, "error": "unexpected"}) == 2
    args.systemd_accept_partial_result = False
    assert cli._result_exit_code(args, partial_result) == 2


def test_partial_result_override_is_rejected_for_non_gated_commands() -> None:
    args = argparse.Namespace(
        command="trade-collect-once",
        systemd_accept_partial_result=True,
    )

    assert cli._result_exit_code(args, {"success": False}) == 2


def test_pin_partial_result_requires_a_confirmed_remote_mutation() -> None:
    args = argparse.Namespace(
        command="trade-pin-providers-once",
        systemd_accept_partial_result=True,
    )

    assert cli._result_exit_code(
        args,
        {
            "success": False,
            "assignments": [
                {"action": "pin_pending_capacity", "applied": False, "confirmed": True},
                {"action": "error", "applied": False, "confirmed": False},
            ],
        },
    ) == 2


def test_proxy_pool_validates_deduplicates_and_requires_private_file(tmp_path: Path) -> None:
    proxy_file = private_file(
        tmp_path / "proxies.txt",
        "socks5://user:pass@proxy.test:1000\n"
        "https://proxy2.test:2000\n"
        "socks5://user:pass@proxy.test:1000\n",
    )

    assert _proxy_pool(str(proxy_file)) == (
        "socks5://user:pass@proxy.test:1000",
        "https://proxy2.test:2000",
    )

    proxy_file.chmod(0o644)
    with pytest.raises(A6AutoError, match="proxy_file_permissions"):
        _proxy_pool(str(proxy_file))


def test_proxy_pool_rejects_unsupported_or_incomplete_urls(tmp_path: Path) -> None:
    for index, value in enumerate(("ftp://proxy.test:21", "socks5://proxy.test")):
        path = private_file(tmp_path / f"invalid-{index}.txt", value)
        with pytest.raises(A6AutoError, match="proxy_url_invalid"):
            _proxy_pool(str(path))


def test_batch_dry_run_has_no_state_mutation_or_secret_lookup(tmp_path: Path) -> None:
    qq_file = tmp_path / "qq.txt"
    qq_file.write_text("\n".join(f"fixture{i}@qq.com" for i in range(20)))
    proxies = [f"socks5://proxy{i}.test:{1000 + i}" for i in range(20)]
    proxy_file = private_file(
        tmp_path / "proxies.json",
        json.dumps(proxies),
    )
    state_dir = tmp_path / "state"
    args = argparse.Namespace(
        live=False,
        qq_file=str(qq_file),
        state_dir=str(state_dir),
        batch_state=None,
        proxy_file=str(proxy_file),
        proxy_subscription_file="",
        proxy_subscription_insecure=False,
        sing_box_executable="sing-box",
        sing_box_port_start=19080,
        count=20,
        min_interval_minutes=180.0,
        max_interval_minutes=480.0,
        per_account_retries=3,
        upstream_base_url="https://upstream.example/v1",
        upstream_api_key="",
        upstream_api_key_env="MISSING_DRY_RUN_SECRET",
        upstream_api_key_file="",
        upstream_domain_file="",
        enodot_admin_url="https://ai.enodot.shop",
        enodot_admin_key_env="AI_ENODOT_SHOP_ADMIN_KEY",
        enodot_admin_key_file="",
        enodot_group="TPA6API",
    )

    result = batch_command(args)

    assert result == {
        "ready": True,
        "target": 20,
        "qq_addresses": 20,
        "proxies": 20,
        "proxy_source": "proxy-file",
        "interval_minutes": [180.0, 480.0],
        "minimum_elapsed_hours": 57.0,
        "maximum_elapsed_hours": 152.0,
        "mutations": False,
        "upstream_mode": "static",
        "upstream_base_url": "https://upstream.example/v1",
    }
    assert not state_dir.exists()


def test_batch_group_mode_preview_uses_bundled_domains_without_secret_lookup(
    tmp_path: Path,
) -> None:
    qq_file = tmp_path / "qq.txt"
    qq_file.write_text("\n".join(f"fixture{i}@qq.com" for i in range(20)))
    proxy_file = private_file(
        tmp_path / "proxies.txt",
        "\n".join(f"socks5://proxy{i}.test:{1000 + i}" for i in range(20)),
    )
    args = argparse.Namespace(
        live=False,
        qq_file=str(qq_file),
        state_dir=str(tmp_path / "state"),
        batch_state=None,
        proxy_file=str(proxy_file),
        proxy_subscription_file="",
        proxy_subscription_insecure=False,
        sing_box_executable="sing-box",
        sing_box_port_start=19080,
        count=20,
        min_interval_minutes=180.0,
        max_interval_minutes=480.0,
        per_account_retries=3,
        upstream_base_url="",
        upstream_api_key="",
        upstream_api_key_env="",
        upstream_api_key_file="",
        upstream_domain_file="",
        enodot_admin_url="https://ai.enodot.shop",
        enodot_admin_key_env="MISSING_ADMIN_KEY",
        enodot_admin_key_file="",
        enodot_group="TPA6API",
    )

    result = batch_command(args)

    assert result["ready"] is True
    assert result["upstream_mode"] == "enodot-group"
    assert result["upstream_domains"] == 30
    assert result["upstream_group"] == "TPA6API"
    assert result["admin_key_environment"] == "MISSING_ADMIN_KEY"


def test_batch_prepare_only_freezes_workbook_templates_without_secret_lookup(
    tmp_path: Path,
) -> None:
    qq_file = tmp_path / "qq.txt"
    qq_file.write_text("\n".join(f"fixture{i}@qq.com" for i in range(5)))
    proxy_file = private_file(
        tmp_path / "proxies.txt",
        "\n".join(f"socks5://proxy{i}.test:{1000 + i}" for i in range(5)),
    )
    state_dir = tmp_path / "state"
    state_file = state_dir / "batches" / "focus.json"
    args = argparse.Namespace(
        live=False,
        prepare_only=True,
        qq_file=str(qq_file),
        state_dir=str(state_dir),
        batch_state=state_file,
        proxy_file=str(proxy_file),
        proxy_subscription_file="",
        proxy_binding_map_file="",
        proxy_subscription_insecure=False,
        sing_box_executable="sing-box",
        sing_box_port_start=19080,
        count=5,
        min_interval_minutes=30.0,
        max_interval_minutes=120.0,
        per_account_retries=3,
        template_catalog="workbook",
        template_workbook=None,
        operating_group="重点组",
        unique_upstream_base_urls=True,
        upstream_base_url="",
        upstream_api_key="",
        upstream_api_key_env="",
        upstream_api_key_file="",
        upstream_domain_file="",
        enodot_admin_url="https://ai.enodot.shop",
        enodot_admin_key_env="MISSING_PREPARE_SECRET",
        enodot_admin_key_file="",
        enodot_admin_token_file="",
        enodot_group="TPA6API",
    )

    result = batch_command(args)

    assert result["prepared"] is True
    assert result["target"] == 5
    assert result["operating_group"] == "重点组"
    assert len(result["templates"]) == 5
    assert len({item["template_id"] for item in result["templates"]}) == 5
    assert state_file.is_file()
    assert not (state_dir / "sessions").exists()


def test_batch_prepare_only_resumes_with_capacity_for_remaining_accounts(
    tmp_path: Path,
) -> None:
    qq_file = tmp_path / "qq.txt"
    qq_file.write_text("\n".join(f"fixture{i}@qq.com" for i in range(10)))
    proxies = tuple(f"socks5://proxy{index}.test:{1000 + index}" for index in range(11))
    proxy_file = private_file(tmp_path / "proxies.json", json.dumps(proxies))
    state_dir = tmp_path / "state"
    state_file = state_dir / "batches" / "focus.json"
    initial_config = BatchConfig(
        qq_file=qq_file,
        state_dir=state_dir,
        state_file=state_file,
        proxy_urls=proxies,
        upstream_base_url="https://upstream.example/v1",
        upstream_api_key="",
        target_count=10,
        min_interval_seconds=1800,
        max_interval_seconds=7200,
        per_account_retries=3,
    )
    runner = SlowBatchRunner(initial_config)
    state = runner._new_state()
    allocated = tuple(_proxy_fingerprint(proxy) for proxy in proxies[:8])
    failed_allocations = tuple(_proxy_fingerprint(proxy) for proxy in proxies[8:10])
    state["allocated_proxy_fingerprints"] = list(allocated + failed_allocations)
    state["completed"] = [
        {
            "template_id": state["template_ids"][index],
            "proxy_fingerprint": fingerprint,
        }
        for index, fingerprint in enumerate(allocated)
    ]
    for index, fingerprint in enumerate(failed_allocations, start=8):
        session_file = str(state_dir / "sessions" / f"abandoned-{index}.json")
        state["abandoned_sessions"].append(session_file)
        state["failures"].append(
            {
                "attempt": index + 1,
                "error": "qq_otp_timeout",
                "session_file": session_file,
                "proxy_fingerprint": fingerprint,
            }
        )
    runner.store.save(state)
    proxy_map_file = private_file(
        tmp_path / "proxy-map.json",
        json.dumps({str(1000 + index): proxy for index, proxy in enumerate(proxies[:8])}),
    )
    args = argparse.Namespace(
        live=False,
        prepare_only=True,
        qq_file=str(qq_file),
        state_dir=str(state_dir),
        batch_state=state_file,
        proxy_file=str(proxy_file),
        proxy_subscription_file="",
        proxy_binding_map_file=str(proxy_map_file),
        proxy_subscription_insecure=False,
        sing_box_executable="sing-box",
        sing_box_port_start=19080,
        count=10,
        min_interval_minutes=30.0,
        max_interval_minutes=120.0,
        per_account_retries=3,
        template_catalog="json",
        template_workbook=None,
        operating_group="standard",
        unique_upstream_base_urls=False,
        upstream_base_url="https://upstream.example/v1",
        upstream_api_key="",
        upstream_api_key_env="",
        upstream_api_key_file="",
        upstream_domain_file="",
        enodot_admin_url="https://ai.enodot.shop",
        enodot_admin_key_env="MISSING_PREPARE_SECRET",
        enodot_admin_key_file="",
        enodot_admin_token_file="",
        enodot_group="TPA6API",
    )

    result = batch_command(args)

    assert result["prepared"] is True
    assert result["target"] == 10
    assert len(result["templates"]) == 10


def test_batch_parser_rejects_live_with_prepare_only() -> None:
    with pytest.raises(SystemExit):
        cli.parser().parse_args(
            [
                "batch",
                "--live",
                "--prepare-only",
                "--qq-file",
                "qq.txt",
                "--proxy-file",
                "proxies.txt",
            ]
        )


def test_batch_subscription_preview_counts_links_without_fetching(tmp_path: Path) -> None:
    qq_file = tmp_path / "qq.txt"
    qq_file.write_text("\n".join(f"fixture{i}@qq.com" for i in range(20)))
    subscriptions = private_file(
        tmp_path / "subscriptions.secret",
        "\n".join(f"https://subscription-{index}.test/token-{index}" for index in range(20)) + "\n",
    )
    args = argparse.Namespace(
        live=False,
        qq_file=str(qq_file),
        state_dir=str(tmp_path / "state"),
        batch_state=None,
        proxy_file="",
        proxy_subscription_file=str(subscriptions),
        proxy_subscription_insecure=True,
        sing_box_executable="sing-box",
        sing_box_port_start=19080,
        count=20,
        min_interval_minutes=180.0,
        max_interval_minutes=480.0,
        per_account_retries=3,
        upstream_base_url="",
        upstream_api_key="",
        upstream_api_key_env="",
        upstream_api_key_file="",
        upstream_domain_file="",
        enodot_admin_url="https://ai.enodot.shop",
        enodot_admin_key_env="MISSING_ADMIN_KEY",
        enodot_admin_key_file="",
        enodot_group="TPA6API",
    )

    result = batch_command(args)

    assert result["proxies"] == 20
    assert result["proxy_source"] == "sing-box-subscriptions"
    assert result["mutations"] is False


def test_batch_proxy_binder_updates_private_map_idempotently(tmp_path: Path) -> None:
    path = private_file(tmp_path / "proxy-map.json", "{}")
    binder = cli._batch_proxy_binder(str(path))
    assert binder is not None

    binder(1001, "http://127.0.0.1:23099")
    binder(1001, "http://127.0.0.1:23099")

    assert json.loads(path.read_text()) == {"1001": "http://127.0.0.1:23099"}
    assert path.stat().st_mode & 0o077 == 0
    with pytest.raises(A6AutoError, match="batch_proxy_binding_proxy_conflict"):
        binder(1002, "http://127.0.0.1:23099")


def test_batch_proxy_binder_preserves_parallel_updates(tmp_path: Path) -> None:
    path = private_file(tmp_path / "proxy-map.json", "{}")
    binder = cli._batch_proxy_binder(str(path))
    assert binder is not None

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [
            executor.submit(binder, 2000 + index, f"http://127.0.0.1:{24000 + index}")
            for index in range(40)
        ]
        for future in futures:
            future.result()

    values = json.loads(path.read_text())
    assert len(values) == 40
    assert values["2039"] == "http://127.0.0.1:24039"
    assert path.with_name(".proxy-map.json.lock").stat().st_mode & 0o077 == 0


def test_batch_reserved_proxy_fingerprints_reflect_existing_bindings(
    tmp_path: Path,
) -> None:
    path = private_file(
        tmp_path / "proxy-map.json",
        json.dumps(
            {
                "1001": "http://127.0.0.1:23099",
                "1002": "http://127.0.0.1:23100",
            }
        ),
    )

    reserved = cli._batch_reserved_proxy_fingerprints(str(path))

    assert reserved == tuple(
        sorted(
            hashlib.sha256(proxy.encode()).hexdigest()[:12]
            for proxy in (
                "http://127.0.0.1:23099",
                "http://127.0.0.1:23100",
            )
        )
    )


def test_trade_parser_has_private_defaults_and_read_only_bind() -> None:
    migrate = cli.parser().parse_args(["trade-migrate"])
    with pytest.raises(SystemExit):
        cli.parser().parse_args(["trade-collect-once"])
    collect = cli.parser().parse_args(["trade-collect-once", "--allow-direct"])
    with pytest.raises(SystemExit):
        cli.parser().parse_args(["trade-dashboard"])
    dashboard = cli.parser().parse_args(["trade-dashboard", "--sessions-dir", "/private/sessions"])
    pin = cli.parser().parse_args(
        [
            "trade-pin-providers-once",
            "--proxy-map-file",
            "/private/proxy-map.json",
            "--pin-failure-journal-file",
            "/run/sub2api-a6-trader/pin-request.failures",
            "--pin-failure-cursor-file",
            "/private/pin-failures.cursor.json",
        ]
    )

    assert migrate.database_dsn_env == "A6_TRADE_DATABASE_DSN"
    assert migrate.database_dsn_file is None
    assert dashboard.fast_block_redis_project is False
    assert dashboard.fast_block_redis_url_env == "PROVIDER_FAST_BLOCK_REDIS_URL"
    assert dashboard.provider_channel_failure_threshold == 3
    assert collect.sessions_dir == Path("~/.config/a6api-auto/sessions").expanduser()
    assert collect.proxy_map_file is None
    assert collect.allow_direct is True
    assert collect.max_workers == 4
    assert dashboard.host == "127.0.0.1"
    assert dashboard.port == 8765
    assert dashboard.sessions_dir == Path("/private/sessions")
    assert pin.pin_failure_max_age_seconds == 60
    assert pin.all_observers is False
    all_observers = cli.parser().parse_args(
        [
            "trade-pin-providers-once",
            "--proxy-map-file",
            "/private/proxy-map.json",
            "--all-observers",
        ]
    )
    assert all_observers.all_observers is True
    assert pin.pin_channel_failure_threshold == 3
    assert pin.pin_max_multiplier == "0.05"
    assert pin.pin_max_relative_cost_ratio == "2"
    assert pin.round_time_budget_seconds == 90
    assert pin.enodot_admin_timeout_seconds == 8
    assert pin.pin_failure_journal_file == Path("/run/sub2api-a6-trader/pin-request.failures")
    assert pin.pin_failure_cursor_file == Path("/private/pin-failures.cursor.json")


def test_trade_seller_resources_parser_is_dry_run_and_accepts_repeated_merchants() -> None:
    args = cli.parser().parse_args(
        [
            "trade-ensure-seller-resources",
            "--merchant",
            "5",
            "--merchant",
            "19",
            "--group-id",
            "26",
            "--delta",
            "0.5",
            "--cost-multiplier",
            "0.03",
            "--listing-description",
            "Team 订阅来源",
            "--proxy-map-file",
            "/private/proxies.json",
        ]
    )

    assert args.merchant == [5, 19]
    assert args.model == "gpt-5.6-sol"
    assert args.apply is False
    assert args.price_multiplier is None
    assert args.existing_channel_id is None
    assert args.maximum_price_multiplier == "1"
    assert (
        args.input_price_micros,
        args.output_price_micros,
        args.cache_read_price_micros,
        args.cache_write_price_micros,
    ) == (5_000_000, 30_000_000, 500_000, 6_250_000)


def test_trade_seller_resources_requires_exactly_one_selection_mode() -> None:
    common = [
        "trade-ensure-seller-resources",
        "--group-id",
        "26",
        "--delta",
        "0.5",
        "--cost-multiplier",
        "0.03",
        "--listing-description",
        "Team 订阅来源",
        "--proxy-map-file",
        "/private/proxies.json",
    ]

    with pytest.raises(SystemExit):
        cli.parser().parse_args(common)
    with pytest.raises(SystemExit):
        cli.parser().parse_args([*common, "--merchant", "5", "--all-sellers"])

    selected = cli.parser().parse_args([*common, "--all-sellers"])
    assert selected.all_sellers is True
    assert selected.merchant == []


def test_trade_seller_price_band_parser_is_dynamic_and_dry_run_by_default() -> None:
    args = cli.parser().parse_args(
        [
            "trade-apply-seller-price-band",
            "--eligible-sellers",
            "--group-id",
            "26",
            "--listing-description-map-file",
            "/private/listing-descriptions.json",
            "--proxy-map-file",
            "/private/proxies.json",
        ]
    )

    assert args.apply is False
    assert args.eligible_sellers is True
    assert args.bound_sellers_only is False
    assert args.merchant == []
    assert args.model == "gpt-5.6-sol"
    assert args.margin == "0.60"
    assert args.price_headroom == "0"
    assert args.maximum_price_multiplier == "1"
    assert args.maximum_final_multiplier == "1"
    assert args.maximum_band_utilization == "1"
    assert args.quote_max_age_seconds == 300
    assert args.usage_window_seconds == 3600
    assert args.market_floor_enabled is True
    assert args.market_floor_gap_ratio == "0.10"
    assert args.market_floor_neighbor_ratio == "0.03"
    assert args.market_floor_min_neighbors == 2
    assert args.market_floor_min_observers == 2
    assert args.market_floor_position == "0.75"
    assert args.market_density_buckets == 8
    assert args.market_density_coverage_ratio == "0.20"
    assert args.market_density_min_channels == 2
    assert args.rollout_batch_limit == 1
    assert args.rollout_confirmation == ""
    assert args.listing_activation_stagger_seconds == 0
    assert args.compact_output is False
    assert args.channel_map_file is None
    assert not hasattr(args, "cost_multiplier")


def test_trade_seller_price_band_parser_accepts_explicit_merchants_exclusively() -> None:
    common = [
        "trade-apply-seller-price-band",
        "--group-id",
        "26",
        "--listing-description-map-file",
        "/private/listing-descriptions.json",
        "--proxy-map-file",
        "/private/proxies.json",
    ]
    selected = cli.parser().parse_args([*common, "--merchant", "5", "--merchant", "19"])

    assert selected.merchant == [5, 19]
    assert selected.eligible_sellers is False
    with pytest.raises(SystemExit):
        cli.parser().parse_args(common)
    with pytest.raises(SystemExit):
        cli.parser().parse_args([*common, "--merchant", "5", "--eligible-sellers"])


def test_eligible_sellers_apply_uses_dynamic_seller_catalog(tmp_path: Path) -> None:
    descriptions = tmp_path / "listing-descriptions.json"
    descriptions.write_text('{"1":"Team 来源","2":"Plus 来源"}')
    descriptions.chmod(0o600)
    args = cli.parser().parse_args(
        [
            "trade-apply-seller-price-band",
            "--eligible-sellers",
            "--group-id",
            "26",
            "--listing-description-map-file",
            str(descriptions),
            "--proxy-map-file",
            "/private/proxies.json",
            "--rollout-batch-limit",
            "2",
            "--rollout-confirmation",
            "CONFIRM_CORRELATED_ACCOUNT_BATCH_RISK",
            "--apply",
        ]
    )
    seller_store = SimpleNamespace(list_seller_ids=lambda: (1, 2))
    orchestrator = SimpleNamespace(
        execute=lambda request, **kwargs: SimpleNamespace(
            summary=lambda: {
                "merchant_ids": request.merchant_ids,
                "apply": kwargs["apply"],
            }
        )
    )

    result = cli.trade_apply_seller_price_band_command(
        args,
        orchestrator=orchestrator,
        seller_store=seller_store,
    )

    assert result == {"merchant_ids": (1, 2), "apply": True}


def test_eligible_sellers_fill_missing_descriptions_from_session_templates(
    tmp_path: Path,
) -> None:
    descriptions = private_file(
        tmp_path / "listing-descriptions.json",
        json.dumps({"1": "Team 来源"}, ensure_ascii=False),
    )
    session_path = trade_session_file(tmp_path / "session-2.json", 2)
    session_record = SessionStore(session_path).load()
    session_record.template_id = "merchant-026"
    SessionStore(session_path).save(session_record)
    args = cli.parser().parse_args(
        [
            "trade-apply-seller-price-band",
            "--eligible-sellers",
            "--group-id",
            "26",
            "--listing-description-map-file",
            str(descriptions),
            "--proxy-map-file",
            "/private/proxies.json",
        ]
    )
    captured = {}

    class SellerStore:
        def list_seller_ids(self):
            return (1, 2)

        def load_context(self, merchant_id):
            assert merchant_id == 2
            return SimpleNamespace(
                a6_user_id=1002,
                session_ref=str(session_path),
            )

    class Orchestrator:
        def execute(self, request, **_kwargs):
            captured["descriptions"] = dict(request.listing_descriptions)
            return SimpleNamespace(summary=lambda: {"success": True})

    result = cli.trade_apply_seller_price_band_command(
        args,
        orchestrator=Orchestrator(),  # type: ignore[arg-type]
        seller_store=SellerStore(),  # type: ignore[arg-type]
    )

    assert result == {"success": True}
    assert captured["descriptions"] == {1: "Team 来源", 2: "纯Plus号池"}


def test_bound_sellers_only_excludes_unstaged_new_accounts(tmp_path: Path) -> None:
    descriptions = private_file(
        tmp_path / "listing-descriptions.json",
        json.dumps(
            {"1": "Team 来源", "2": "Plus 来源", "149": "K12 / Plus快池"},
            ensure_ascii=False,
        ),
    )
    args = cli.parser().parse_args(
        [
            "trade-apply-seller-price-band",
            "--eligible-sellers",
            "--bound-sellers-only",
            "--group-id",
            "26",
            "--listing-description-map-file",
            str(descriptions),
            "--proxy-map-file",
            "/private/proxies.json",
        ]
    )
    captured = {}

    class SellerStore:
        def list_seller_ids(self):
            raise AssertionError("unbound catalog must not be selected")

        def list_bound_seller_ids(self):
            return (1, 2)

    class Orchestrator:
        def execute(self, request, **_kwargs):
            captured["merchant_ids"] = request.merchant_ids
            return SimpleNamespace(summary=lambda: {"success": True})

    result = cli.trade_apply_seller_price_band_command(
        args,
        orchestrator=Orchestrator(),  # type: ignore[arg-type]
        seller_store=SellerStore(),  # type: ignore[arg-type]
    )

    assert result == {"success": True}
    assert captured == {"merchant_ids": (1, 2)}


def test_seller_upstream_guard_selects_every_bound_seller() -> None:
    args = cli.parser().parse_args(
        [
            "trade-reconcile-seller-upstreams",
            "--all-bound-sellers",
            "--proxy-map-file",
            "/private/proxies.json",
            "--apply",
        ]
    )
    captured = {}

    class SellerStore:
        def list_bound_seller_ids(self):
            return (5, 17)

    class Reconciler:
        def execute(self, request, *, apply=False):
            captured["merchant_ids"] = request.merchant_ids
            captured["base_url"] = request.seller_base_url
            captured["apply"] = apply
            return SimpleNamespace(summary=lambda: {"success": True})

    result = cli.trade_reconcile_seller_upstreams_command(
        args,
        reconciler=Reconciler(),  # type: ignore[arg-type]
        seller_store=SellerStore(),  # type: ignore[arg-type]
    )

    assert result == {"success": True}
    assert captured == {
        "merchant_ids": (5, 17),
        "base_url": "https://ai.enodot.shop/v1",
        "apply": True,
    }


def test_eligible_sellers_filters_retired_entries_from_resource_maps(tmp_path: Path) -> None:
    descriptions = private_file(
        tmp_path / "listing-descriptions.json",
        json.dumps(
            {"1": "Team 来源", "2": "Plus 来源", "15": "Pro 来源"},
            ensure_ascii=False,
        ),
    )
    channels = private_file(
        tmp_path / "channel-map.json",
        json.dumps({"1": 101, "2": 102, "15": 115}),
    )
    args = cli.parser().parse_args(
        [
            "trade-apply-seller-price-band",
            "--eligible-sellers",
            "--group-id",
            "26",
            "--listing-description-map-file",
            str(descriptions),
            "--channel-map-file",
            str(channels),
            "--proxy-map-file",
            "/private/proxies.json",
        ]
    )
    captured = {}

    class Orchestrator:
        def execute(self, request, **_kwargs):
            captured["descriptions"] = dict(request.listing_descriptions)
            captured["channels"] = dict(request.existing_channel_ids or {})
            return SimpleNamespace(summary=lambda: {"success": True})

    result = cli.trade_apply_seller_price_band_command(
        args,
        orchestrator=Orchestrator(),  # type: ignore[arg-type]
        seller_store=SimpleNamespace(list_seller_ids=lambda: (1, 2)),
    )

    assert result == {"success": True}
    assert captured == {
        "descriptions": {1: "Team 来源", 2: "Plus 来源"},
        "channels": {1: 101, 2: 102},
    }


@pytest.mark.parametrize(
    "reason",
    (
        "seller_price_band_hysteresis_hold",
        "seller_price_band_minimum_interval_hold",
    ),
)
def test_seller_price_band_expected_hold_is_a_successful_noop(
    tmp_path: Path,
    reason: str,
) -> None:
    descriptions = private_file(
        tmp_path / "listing-descriptions.json",
        json.dumps({"5": "Pro 订阅来源"}, ensure_ascii=False),
    )
    args = cli.parser().parse_args(
        [
            "trade-apply-seller-price-band",
            "--merchant",
            "5",
            "--group-id",
            "26",
            "--listing-description-map-file",
            str(descriptions),
            "--proxy-map-file",
            "/not-read-when-injected.json",
            "--apply",
        ]
    )

    class Orchestrator:
        def execute(self, _request, **_kwargs):
            raise cli.SellerPriceBandError(reason)

    result = cli.trade_apply_seller_price_band_command(
        args,
        orchestrator=Orchestrator(),  # type: ignore[arg-type]
    )

    assert result == {
        "success": True,
        "mode": "held",
        "model": "gpt-5.6-sol",
        "merchant_count": 1,
        "skipped_merchant_count": 1,
        "reason": reason,
    }


def test_seller_price_band_pending_action_is_a_successful_skipped_hold(
    tmp_path: Path,
) -> None:
    descriptions = private_file(
        tmp_path / "listing-descriptions.json",
        json.dumps({"5": "Pro 订阅来源"}, ensure_ascii=False),
    )
    args = cli.parser().parse_args(
        [
            "trade-apply-seller-price-band",
            "--merchant",
            "5",
            "--group-id",
            "26",
            "--listing-description-map-file",
            str(descriptions),
            "--proxy-map-file",
            "/not-read-when-injected.json",
            "--apply",
        ]
    )

    class Orchestrator:
        def execute(self, _request, **_kwargs):
            raise cli.SellerPriceBandError("seller_price_band_action_pending")

    result = cli.trade_apply_seller_price_band_command(
        args,
        orchestrator=Orchestrator(),  # type: ignore[arg-type]
    )

    assert result == {
        "success": True,
        "mode": "held",
        "model": "gpt-5.6-sol",
        "merchant_count": 1,
        "skipped_merchant_count": 1,
        "reason": "seller_price_band_action_pending",
    }


def test_seller_price_band_stale_claim_ttl_is_runtime_configurable() -> None:
    defaults = cli.parser().parse_args(
        [
            "trade-apply-seller-price-band",
            "--merchant",
            "5",
            "--group-id",
            "26",
            "--listing-description-map-file",
            "/tmp/descriptions.json",
            "--proxy-map-file",
            "/tmp/proxies.json",
        ]
    )
    configured = cli.parser().parse_args(
        [
            "trade-apply-seller-price-band",
            "--merchant",
            "5",
            "--group-id",
            "26",
            "--listing-description-map-file",
            "/tmp/descriptions.json",
            "--proxy-map-file",
            "/tmp/proxies.json",
            "--stale-claim-seconds",
            "300",
        ]
    )

    assert defaults.stale_claim_seconds == 3_600
    assert configured.stale_claim_seconds == 300


def test_trade_seller_resources_command_passes_validated_request_to_injected_orchestrator() -> None:
    class Result:
        def summary(self):
            return {"success": True, "fixture": "summary"}

    class Orchestrator:
        def __init__(self):
            self.calls = []

        def execute(self, request, *, apply=False):
            self.calls.append((request, apply))
            return Result()

    args = cli.parser().parse_args(
        [
            "trade-ensure-seller-resources",
            "--merchant",
            "5",
            "--group-id",
            "26",
            "--delta",
            "0.5",
            "--cost-multiplier",
            "0.03",
            "--price-multiplier",
            "0.10",
            "--listing-description",
            "Team 订阅来源",
            "--proxy-map-file",
            "/not-read-when-injected.json",
            "--apply",
        ]
    )
    orchestrator = Orchestrator()

    result = cli.trade_ensure_seller_resources_command(args, orchestrator=orchestrator)

    assert result == {"success": True, "fixture": "summary"}
    assert len(orchestrator.calls) == 1
    desired, apply = orchestrator.calls[0]
    assert desired.merchant_ids == (5,)
    assert desired.group_id == 26
    assert desired.pricing == SellerPricing(
        cost_multiplier="0.03",
        delta="0.5",
        price_multiplier="0.10",
    )
    assert desired.listing_description == "Team 订阅来源"
    assert apply is True


def test_trade_seller_resources_adopts_one_explicit_existing_channel() -> None:
    class Result:
        def summary(self):
            return {"success": True}

    class Orchestrator:
        def __init__(self):
            self.calls = []

        def execute(self, request, *, apply=False):
            self.calls.append((request, apply))
            return Result()

    args = cli.parser().parse_args(
        [
            "trade-ensure-seller-resources",
            "--merchant",
            "21",
            "--group-id",
            "26",
            "--delta",
            "0",
            "--cost-multiplier",
            "0.00648",
            "--price-multiplier",
            "0.1203732548957559643205819532",
            "--existing-channel-id",
            "1283",
            "--listing-description",
            "Plus + K12混池",
            "--proxy-map-file",
            "/not-read-when-injected.json",
        ]
    )
    orchestrator = Orchestrator()

    result = cli.trade_ensure_seller_resources_command(args, orchestrator=orchestrator)

    assert result == {"success": True}
    desired, apply = orchestrator.calls[0]
    assert desired.merchant_ids == (21,)
    assert desired.channel_strategy == "adopt_existing"
    assert desired.existing_channel_id == 1283
    assert apply is False


def test_trade_seller_resources_rejects_existing_channel_for_multiple_merchants() -> None:
    args = cli.parser().parse_args(
        [
            "trade-ensure-seller-resources",
            "--merchant",
            "21",
            "--merchant",
            "31",
            "--group-id",
            "26",
            "--delta",
            "0",
            "--cost-multiplier",
            "0.00648",
            "--existing-channel-id",
            "1283",
            "--listing-description",
            "Plus + K12混池",
            "--proxy-map-file",
            "/not-read-when-injected.json",
        ]
    )

    with pytest.raises(
        cli.A6AutoError,
        match="seller_existing_channel_single_merchant_required",
    ):
        cli.trade_ensure_seller_resources_command(args, orchestrator=object())


def test_trade_seller_resources_dispatches_explicit_channel_recovery() -> None:
    class Result:
        def summary(self):
            return {"success": True, "operation": "resolve_channel_create_unknown"}

    class Orchestrator:
        def __init__(self):
            self.calls = []

        def resolve_channel_create_unknown(self, request, *, apply=False):
            self.calls.append((request, apply))
            return Result()

    args = cli.parser().parse_args(
        [
            "trade-ensure-seller-resources",
            "--merchant",
            "1",
            "--group-id",
            "26",
            "--delta",
            "0.5",
            "--cost-multiplier",
            "0.12",
            "--listing-description",
            "Team 订阅来源",
            "--proxy-map-file",
            "/not-read-when-injected.json",
            "--resolve-channel-create-unknown",
            "--apply",
        ]
    )
    orchestrator = Orchestrator()

    result = cli.trade_ensure_seller_resources_command(args, orchestrator=orchestrator)

    assert result == {"success": True, "operation": "resolve_channel_create_unknown"}
    assert len(orchestrator.calls) == 1
    assert orchestrator.calls[0][0].merchant_ids == (1,)
    assert orchestrator.calls[0][1] is True


def test_trade_seller_resources_recovery_rejects_multi_merchant_selection() -> None:
    args = cli.parser().parse_args(
        [
            "trade-ensure-seller-resources",
            "--merchant",
            "1",
            "--merchant",
            "2",
            "--group-id",
            "26",
            "--delta",
            "0.5",
            "--cost-multiplier",
            "0.12",
            "--listing-description",
            "Team 订阅来源",
            "--proxy-map-file",
            "/not-read-when-injected.json",
            "--resolve-channel-create-unknown",
        ]
    )

    with pytest.raises(cli.A6AutoError, match="seller_channel_recovery_single_merchant_required"):
        cli.trade_ensure_seller_resources_command(args, orchestrator=object())


def test_trade_seller_resources_all_sellers_passes_every_duty_in_stable_order() -> None:
    class Result:
        def summary(self):
            return {"success": True}

    class Orchestrator:
        def __init__(self):
            self.requests = []

        def execute(self, request, *, apply=False):
            self.requests.append((request, apply))
            return Result()

    class SellerStore:
        def list_seller_ids(self):
            return tuple(range(1, 20))

    args = cli.parser().parse_args(
        [
            "trade-ensure-seller-resources",
            "--all-sellers",
            "--group-id",
            "26",
            "--delta",
            "0.5",
            "--cost-multiplier",
            "0.03",
            "--listing-description",
            "Team 订阅来源",
            "--proxy-map-file",
            "/not-read-when-injected.json",
        ]
    )
    orchestrator = Orchestrator()

    result = cli.trade_ensure_seller_resources_command(
        args,
        orchestrator=orchestrator,
        seller_store=SellerStore(),  # type: ignore[arg-type]
    )

    assert result == {"success": True}
    assert len(orchestrator.requests) == 1
    desired, apply = orchestrator.requests[0]
    assert desired.merchant_ids == tuple(range(1, 20))
    assert len(desired.merchant_ids) == 19
    assert apply is False


def test_trade_seller_resources_preserves_capability_error_code() -> None:
    class Orchestrator:
        def execute(self, _request, *, apply=False):
            raise cli.ManagedSellerError("managed_seller_capability_unavailable")

    args = cli.parser().parse_args(
        [
            "trade-ensure-seller-resources",
            "--merchant",
            "5",
            "--group-id",
            "26",
            "--delta",
            "0.5",
            "--cost-multiplier",
            "0.03",
            "--listing-description",
            "Team 订阅来源",
            "--proxy-map-file",
            "/not-read-when-injected.json",
        ]
    )

    with pytest.raises(A6AutoError, match="managed_seller_capability_unavailable"):
        cli.trade_ensure_seller_resources_command(args, orchestrator=Orchestrator())


def test_trade_seller_resources_preserves_managed_listing_error_code() -> None:
    class Orchestrator:
        def resolve_channel_create_unknown(self, _request, *, apply=False):
            raise cli.ManagedListingError("managed_channel_create_unknown_remote_candidate")

    args = cli.parser().parse_args(
        [
            "trade-ensure-seller-resources",
            "--merchant",
            "1",
            "--group-id",
            "26",
            "--delta",
            "0.5",
            "--cost-multiplier",
            "0.12",
            "--listing-description",
            "Team 订阅来源",
            "--proxy-map-file",
            "/not-read-when-injected.json",
            "--resolve-channel-create-unknown",
        ]
    )

    with pytest.raises(A6AutoError, match="managed_channel_create_unknown_remote_candidate"):
        cli.trade_ensure_seller_resources_command(args, orchestrator=Orchestrator())


def test_database_dsn_uses_environment_or_owner_only_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment_dsn = "postgresql://private-user:environment-secret@db.invalid/trade"
    file_dsn = "postgresql://private-user:file-secret@db.invalid/trade"
    monkeypatch.setenv("FIXTURE_TRADE_DSN", environment_dsn)
    args = argparse.Namespace(database_dsn_env="FIXTURE_TRADE_DSN", database_dsn_file=None)

    assert _database_dsn(args) == environment_dsn

    dsn_file = private_file(tmp_path / "database.dsn", file_dsn)
    args.database_dsn_file = dsn_file
    assert _database_dsn(args) == file_dsn

    dsn_file.chmod(0o644)
    with pytest.raises(A6AutoError, match="trade_database_dsn_file_permissions"):
        _database_dsn(args)


def test_proxy_map_is_private_and_keyed_by_numeric_user_id(tmp_path: Path) -> None:
    path = private_file(
        tmp_path / "proxy-map.json",
        json.dumps(
            {
                "1001": "socks5://user:proxy-secret@proxy-one.invalid:1080",
                "1002": "https://proxy-two.invalid:8443",
            }
        ),
    )

    assert _proxy_map(path) == {
        1001: "socks5://user:proxy-secret@proxy-one.invalid:1080",
        1002: "https://proxy-two.invalid:8443",
    }

    path.chmod(0o644)
    with pytest.raises(A6AutoError, match="collector_proxy_map_permissions"):
        _proxy_map(path)

    duplicate = private_file(
        tmp_path / "duplicate-proxy-map.json",
        json.dumps(
            {
                "1001": "socks5://proxy.invalid:1080",
                "1002": "socks5://proxy.invalid:1080",
            }
        ),
    )
    with pytest.raises(A6AutoError, match="collector_proxy_map_duplicate_proxy"):
        _proxy_map(duplicate)


def test_listing_description_map_is_private_and_text_only(tmp_path: Path) -> None:
    path = private_file(
        tmp_path / "listing-descriptions.json",
        json.dumps(
            {
                "1": "Team 订阅来源",
                "2": "K12 配额来源",
            },
            ensure_ascii=False,
        ),
    )

    assert _merchant_text_map(path, error="fixture_invalid") == {
        1: "Team 订阅来源",
        2: "K12 配额来源",
    }

    path.chmod(0o644)
    with pytest.raises(A6AutoError, match="fixture_invalid_permissions"):
        _merchant_text_map(path, error="fixture_invalid")

    invalid = private_file(
        tmp_path / "listing-descriptions-invalid.json",
        json.dumps({"1": ["Team"]}),
    )
    with pytest.raises(A6AutoError, match="fixture_invalid"):
        _merchant_text_map(invalid, error="fixture_invalid")


def test_collector_routing_requires_complete_map_or_one_explicit_direct_session(
    tmp_path: Path,
) -> None:
    sessions = tmp_path / "sessions"
    paths = (
        trade_session_file(sessions / "session-one.json", 1),
        trade_session_file(sessions / "session-two.json", 2),
        trade_session_file(sessions / "session-three.json", 3, status="allocated"),
    )

    with pytest.raises(A6AutoError, match="collector_proxy_mapping_incomplete"):
        cli._validate_collector_routing(
            paths,
            {1001: "socks5://proxy-one.invalid:1080"},
            allow_direct=False,
        )
    assert (
        cli._validate_collector_routing(
            paths,
            {
                1001: "socks5://proxy-one.invalid:1080",
                1002: "socks5://proxy-two.invalid:1080",
            },
            allow_direct=False,
        )
        == 2
    )
    with pytest.raises(A6AutoError, match="collector_direct_requires_single_session"):
        cli._validate_collector_routing(paths, {}, allow_direct=True)

    one_path = (trade_session_file(sessions / "session-four.json", 4),)
    assert cli._validate_collector_routing(one_path, {}, allow_direct=True) == 1


def test_collector_watermarks_compare_numeric_text_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, query):
            calls.append(query)

        def fetchall(self):
            return [(42, 100, 101)]

    connection = SimpleNamespace(cursor=Cursor, close=lambda: None)
    monkeypatch.setattr(cli, "_trade_connection", lambda _dsn: connection)

    result = cli._collector_watermarks("fixture-dsn")

    assert result[42] == CollectorWatermarks(earning_id=100, provider_log_id=101)
    assert "max(e.a6_earning_id::bigint)" in calls[0]
    assert "WHEN u.a6_log_id ~ '^[0-9]+$'" in calls[0]
    assert "THEN u.a6_log_id::bigint" in calls[0]


def test_trade_migrate_wires_dsn_without_returning_it(monkeypatch: pytest.MonkeyPatch) -> None:
    secret_dsn = "postgresql://private:database-secret@db.invalid/trade"
    monkeypatch.setenv("FIXTURE_TRADE_DSN", secret_dsn)
    connection = SimpleNamespace(closed=False)
    connection.close = lambda: setattr(connection, "closed", True)
    seen: dict[str, object] = {}

    def connect(dsn: str):
        seen["dsn"] = dsn
        return connection

    def migrate(received, directory):
        seen["connection"] = received
        seen["directory"] = directory
        return ("0001",)

    monkeypatch.setattr(cli, "connect_postgres", connect)
    monkeypatch.setattr(cli, "apply_migrations", migrate)
    args = cli.parser().parse_args(["trade-migrate", "--database-dsn-env", "FIXTURE_TRADE_DSN"])

    result = cli.trade_migrate_command(args)

    assert result == {
        "success": True,
        "applied_count": 1,
        "applied_versions": ["0001"],
    }
    assert seen == {"dsn": secret_dsn, "connection": connection, "directory": None}
    assert connection.closed is True
    assert "database-secret" not in json.dumps(result)


def test_trade_import_sessions_imports_active_rows_and_abandoned_tombstone(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions = tmp_path / "sessions"
    paths = [trade_session_file(sessions / f"session-{index}.json", index) for index in range(19)]
    trade_session_file(sessions / "session-abandoned.json", 20, status="abandoned")
    monkeypatch.setenv("FIXTURE_TRADE_DSN", "postgresql://private:secret@db.invalid/trade")
    connection = SimpleNamespace(close=lambda: None)
    captured = {}
    monkeypatch.setattr(cli, "connect_postgres", lambda _dsn: connection)

    def upsert(received, metadata):
        captured["connection"] = received
        captured["metadata"] = tuple(metadata)
        return len(captured["metadata"])

    monkeypatch.setattr(cli, "upsert_merchant_metadata", upsert)
    args = cli.parser().parse_args(
        [
            "trade-import-sessions",
            "--database-dsn-env",
            "FIXTURE_TRADE_DSN",
            "--sessions-dir",
            str(sessions),
        ]
    )

    result = cli.trade_import_sessions_command(args)

    assert result == {
        "success": True,
        "imported": 20,
        "tombstoned": 1,
        "new_merchant_roles": ["seller"],
        "existing_roles_preserved": True,
        "statuses": {"abandoned": 1, "supplier_submitted": 19},
    }
    assert captured["connection"] is connection
    assert len(captured["metadata"]) == 20
    assert {item.session_ref for item in captured["metadata"]} == {
        *(str(path.resolve()) for path in paths),
        str((sessions / "session-abandoned.json").resolve()),
    }
    rendered = repr(captured["metadata"])
    assert "@fixture.invalid" not in rendered
    assert "password-secret" not in rendered
    assert "cookie-secret" not in rendered
    assert "sk-secret" not in rendered


def test_trade_import_sessions_persists_loopback_singbox_leases(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions = tmp_path / "sessions"
    first = trade_session_file(sessions / "session-one.json", 1)
    second = trade_session_file(sessions / "session-two.json", 2)
    proxy_map = private_file(
        tmp_path / "proxy-map.json",
        json.dumps(
            {
                "1001": "http://127.0.0.1:23081",
                "1002": "http://127.0.0.1:23082",
            }
        ),
    )
    monkeypatch.setenv("FIXTURE_TRADE_DSN", "postgresql://private:secret@db.invalid/trade")
    connection = SimpleNamespace(close=lambda: None)
    captured = {}
    monkeypatch.setattr(cli, "connect_postgres", lambda _dsn: connection)

    def upsert(_received, metadata):
        captured["metadata"] = {item.session_ref: item for item in metadata}
        return len(captured["metadata"])

    monkeypatch.setattr(cli, "upsert_merchant_metadata", upsert)
    args = cli.parser().parse_args(
        [
            "trade-import-sessions",
            "--database-dsn-env",
            "FIXTURE_TRADE_DSN",
            "--sessions-dir",
            str(sessions),
            "--proxy-map-file",
            str(proxy_map),
        ]
    )

    result = cli.trade_import_sessions_command(args)

    assert result["imported"] == 2
    first_metadata = captured["metadata"][str(first.resolve())]
    second_metadata = captured["metadata"][str(second.resolve())]
    assert first_metadata.proxy_lease_id == "singbox:23081"
    assert second_metadata.proxy_lease_id == "singbox:23082"
    assert first_metadata.proxy_metadata["provider"] == "singbox"
    assert len(first_metadata.proxy_fingerprint or "") == 64


def test_trade_refresh_sessions_once_wires_proxy_client_and_metadata_sync(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions = tmp_path / "sessions"
    path = trade_session_file(sessions / "session-one.json", 1, status="authenticated")
    proxy_secret = "socks5://proxy-user:proxy-secret@127.0.0.1:23081"
    proxy_map = private_file(tmp_path / "proxy-map.json", json.dumps({"1001": proxy_secret}))
    database_secret = "postgresql://private:database-secret@db.invalid/trade"
    monkeypatch.setenv("FIXTURE_TRADE_DSN", database_secret)
    connection = SimpleNamespace(close=lambda: None)
    captured = {}
    monkeypatch.setattr(cli, "connect_postgres", lambda _dsn: connection)
    mailbox = object()
    monkeypatch.setattr(cli, "QQMailbox", lambda: mailbox)

    class FakeClient:
        def __init__(self, origin, *, proxy_url, timeout):
            captured["client"] = (origin, proxy_url, timeout)

    class FakeRecoveryStore:
        def __init__(self, received):
            captured["recovery_connection"] = received

        def resolve_session_refresh_failures(self, session_ref):
            captured["resolved_session_ref"] = session_ref
            return 1

        def record_session_refresh_failure(self, session_ref, error_class):
            captured["failed_session"] = (session_ref, error_class)
            return True

    class FakeRefreshService:
        def __init__(
            self,
            *,
            client_factory,
            metadata_sink,
            failure_sink,
            mailbox,
            max_workers,
        ):
            captured["mailbox"] = mailbox
            captured["client_factory"] = client_factory
            captured["metadata_sink"] = metadata_sink
            captured["failure_sink"] = failure_sink
            captured["max_workers"] = max_workers

        def refresh_paths(self, paths, proxy_by_user_id):
            captured["paths"] = tuple(paths)
            captured["proxy_map"] = dict(proxy_by_user_id)
            record = SessionStore(path).load()
            captured["client_factory"](record, proxy_by_user_id[1001])
            captured["metadata_sink"](path, record, proxy_by_user_id[1001])
            captured["failure_sink"](path, "a6_transport_failed")
            return {"success": True, "refreshed": 1, "errors": {}}

    def upsert(received, metadata):
        captured["connection"] = received
        captured["metadata"] = tuple(metadata)
        return 1

    monkeypatch.setattr(cli, "A6ApiClient", FakeClient)
    monkeypatch.setattr(cli, "PostgresMerchantRecoveryStore", FakeRecoveryStore)
    monkeypatch.setattr(cli, "TradeSessionRefreshService", FakeRefreshService)
    monkeypatch.setattr(cli, "upsert_merchant_metadata", upsert)
    args = cli.parser().parse_args(
        [
            "trade-refresh-sessions-once",
            "--database-dsn-env",
            "FIXTURE_TRADE_DSN",
            "--sessions-dir",
            str(sessions),
            "--proxy-map-file",
            str(proxy_map),
            "--client-timeout-seconds",
            "7.5",
            "--max-workers",
            "6",
        ]
    )

    result = cli.trade_refresh_sessions_once_command(args)

    assert result == {"success": True, "refreshed": 1, "errors": {}}
    assert captured["paths"] == (path.resolve(),)
    assert captured["proxy_map"] == {1001: proxy_secret}
    assert captured["client"] == ("https://fixture.invalid", proxy_secret, 7.5)
    assert captured["mailbox"] is mailbox
    assert captured["max_workers"] == 6
    assert captured["connection"] is connection
    assert captured["recovery_connection"] is connection
    assert captured["resolved_session_ref"] == str(path.resolve())
    assert captured["failed_session"] == (str(path.resolve()), "a6_transport_failed")
    assert captured["metadata"][0].status == "authenticated"
    rendered = json.dumps(result)
    assert "proxy-secret" not in rendered
    assert "database-secret" not in rendered


def test_trade_collect_once_wires_watermarks_fixed_proxies_and_safe_summary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions = tmp_path / "sessions"
    first = trade_session_file(sessions / "session-one.json", 1)
    trade_session_file(sessions / "session-two.json", 2, status="allocated")
    proxy_secret = "socks5://user:proxy-secret@proxy.invalid:1080"
    proxy_map = private_file(
        tmp_path / "proxy-map.json",
        json.dumps({"1001": proxy_secret}),
    )
    database_secret = "postgresql://private:database-secret@db.invalid/trade"
    monkeypatch.setenv("FIXTURE_TRADE_DSN", database_secret)
    monkeypatch.setattr(
        cli,
        "_collector_watermarks",
        lambda dsn: {1001: CollectorWatermarks(earning_id=7, provider_log_id=8)},
    )
    captured = {}

    class FakeRepository:
        @staticmethod
        def classify_earning_counterparties(*, settle_seconds):
            captured["counterparty_settle_seconds"] = settle_seconds
            return {"external": 4, "own_boost": 2, "own_pin": 0, "classified": 6}

    repository = FakeRepository()
    monkeypatch.setattr(
        cli,
        "PostgresTradeRepository",
        SimpleNamespace(from_dsn=lambda dsn: repository),
    )

    class FakeClient:
        def __init__(self, origin, *, proxy_url, timeout):
            captured["origin"] = origin
            captured["proxy_url"] = proxy_url
            captured["timeout"] = timeout

    class FakeService:
        def __init__(
            self,
            *,
            client_factory,
            watermark_loader,
            sink,
            max_workers,
            session_refresher,
        ):
            captured["sink"] = sink
            captured["client_factory"] = client_factory
            captured["watermark_loader"] = watermark_loader
            captured["max_workers"] = max_workers
            captured["session_refresher"] = session_refresher

        def collect_paths(self, paths):
            record = SessionStore(first).load()
            captured["client_factory"](record)
            captured["watermark"] = captured["watermark_loader"](1001)
            captured["paths"] = tuple(paths)
            return 1, (SimpleNamespace(error_class="collector_session_invalid"),)

    monkeypatch.setattr(cli, "A6ApiClient", FakeClient)
    monkeypatch.setattr(cli, "A6CollectionService", FakeService)
    mailbox = object()
    monkeypatch.setattr(cli, "QQMailbox", lambda: mailbox)

    class FakeWorkflow:
        def __init__(self, client, received_mailbox, store):
            captured["refresh_workflow"] = (client, received_mailbox, store)

        def ensure_authenticated(self, record):
            captured["refresh_record"] = record

    monkeypatch.setattr(cli, "Workflow", FakeWorkflow)
    args = cli.parser().parse_args(
        [
            "trade-collect-once",
            "--database-dsn-env",
            "FIXTURE_TRADE_DSN",
            "--sessions-dir",
            str(sessions),
            "--proxy-map-file",
            str(proxy_map),
            "--max-workers",
            "2",
        ]
    )

    result = cli.trade_collect_once_command(args)

    assert result == {
        "success": False,
        "sessions_found": 2,
        "eligible_sessions": 1,
        "collected": 1,
        "failed": 1,
        "skipped": 0,
        "errors": {"collector_session_invalid": 1},
        "proxy_mode": "fixed-map",
        "proxy_bindings": 1,
        "earning_counterparty": {
            "external": 4,
            "own_boost": 2,
            "own_pin": 0,
            "classified": 6,
        },
        "own_pin_leak_detected": False,
        "upstream_mutations": False,
    }
    assert captured["counterparty_settle_seconds"] == 900
    assert captured["sink"] is repository
    assert captured["proxy_url"] == proxy_secret
    assert captured["timeout"] == 10
    assert captured["max_workers"] == 2
    assert captured["watermark"] == CollectorWatermarks(earning_id=7, provider_log_id=8)
    refresh_store = SessionStore(first)
    refresh_record = refresh_store.load()
    refresh_client = captured["client_factory"](refresh_record)
    captured["session_refresher"](refresh_store, refresh_record, refresh_client)
    assert captured["refresh_workflow"] == (refresh_client, mailbox, refresh_store)
    assert captured["refresh_record"] == refresh_record
    rendered = json.dumps(result)
    assert "proxy-secret" not in rendered
    assert "database-secret" not in rendered


@pytest.mark.parametrize("workers", [True, 0, 17])
def test_trade_collect_once_rejects_invalid_max_workers(workers: object) -> None:
    with pytest.raises(A6AutoError, match="^collector_max_workers_invalid$"):
        cli.trade_collect_once_command(argparse.Namespace(max_workers=workers))


def test_collector_systemd_uses_conservative_worker_limit() -> None:
    service = Path("deploy/systemd/a6-trader-collector.service").read_text()

    assert "trade-collect-once" in service
    assert "--max-workers 8" in service
    assert "TimeoutStartSec=8min" in service


def test_trade_dashboard_binds_read_only_server_without_printing_dsn(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database_secret = "postgresql://private:database-secret@db.invalid/trade"
    monkeypatch.setenv("FIXTURE_TRADE_DSN", database_secret)
    repository = object()
    monkeypatch.setattr(
        cli,
        "PostgresTradeRepository",
        SimpleNamespace(from_dsn=lambda dsn: repository),
    )
    captured = {}

    def serve(
        received,
        *,
        host,
        port,
        sessions_dir,
        publication_control,
        internal_api_token,
    ):
        captured.update(
            repository=received,
            host=host,
            port=port,
            sessions_dir=sessions_dir,
            publication_control=publication_control,
            internal_api_token=internal_api_token,
        )

    monkeypatch.setattr(cli, "serve_dashboard", serve)
    args = cli.parser().parse_args(
        [
            "trade-dashboard",
            "--sessions-dir",
            "/var/lib/a6-trader/secrets/sessions",
            "--database-dsn-env",
            "FIXTURE_TRADE_DSN",
            "--host",
            "127.0.0.2",
            "--port",
            "9876",
        ]
    )

    result = cli.trade_dashboard_command(args)
    output = capsys.readouterr().out

    assert result == {"success": True, "stopped": True}
    assert captured["repository"] is repository
    assert captured["host"] == "127.0.0.2"
    assert captured["port"] == 9876
    assert captured["sessions_dir"] == Path("/var/lib/a6-trader/secrets/sessions")
    assert isinstance(captured["publication_control"], cli.PostgresPublicationControl)
    assert captured["internal_api_token"] == ""
    assert '"read_only": true' in output
    assert "http://127.0.0.2:9876" in output
    assert "database-secret" not in output


def test_trade_dashboard_replays_outbox_then_rebuilds_fast_block_cache(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("FIXTURE_TRADE_DSN", "postgresql://fixture.invalid/trade")
    monkeypatch.setenv("FIXTURE_FAST_BLOCK_REDIS", "redis://fixture.invalid/0")
    monkeypatch.setattr(
        cli,
        "PostgresTradeRepository",
        SimpleNamespace(from_dsn=lambda _dsn: object()),
    )
    calls: list[str] = []
    redis_client = object()
    projector = object()

    def from_url(url: str, **options):
        assert url == "redis://fixture.invalid/0"
        assert options["decode_responses"] is True
        return redis_client

    monkeypatch.setitem(
        sys.modules,
        "redis",
        SimpleNamespace(Redis=SimpleNamespace(from_url=from_url)),
    )
    monkeypatch.setattr(
        cli,
        "RedisProviderFastBlockProjector",
        lambda client: projector if client is redis_client else None,
    )

    class Control:
        def __init__(self, *_args, fast_block_projector=None, **_kwargs):
            assert fast_block_projector is projector

        def project_pending_fast_blocks(self, *, limit: int):
            calls.append(f"outbox:{limit}")
            return {"processed": 1, "projected": 1, "superseded": 0, "failed": 0}

        def rebuild_fast_block_cache(self, *, limit: int):
            calls.append(f"rebuild:{limit}")
            return {
                "processed": 2,
                "projected": 2,
                "superseded": 0,
                "failed": 0,
                "changed": 1,
            }

    monkeypatch.setattr(cli, "PostgresPublicationControl", Control)
    monkeypatch.setattr(cli, "serve_dashboard", lambda *_args, **_kwargs: None)
    args = cli.parser().parse_args(
        [
            "trade-dashboard",
            "--sessions-dir",
            "/var/lib/a6-trader/secrets/sessions",
            "--database-dsn-env",
            "FIXTURE_TRADE_DSN",
            "--fast-block-redis-project",
            "--fast-block-redis-url-env",
            "FIXTURE_FAST_BLOCK_REDIS",
        ]
    )

    assert cli.trade_dashboard_command(args) == {"success": True, "stopped": True}
    event = json.loads(capsys.readouterr().out)
    assert calls == ["outbox:500", "rebuild:500"]
    assert event["fast_block_startup_replay"] == {
        "outbox": {"processed": 1, "projected": 1, "superseded": 0, "failed": 0},
        "cache_rebuild": {
            "processed": 2,
            "projected": 2,
            "superseded": 0,
            "failed": 0,
            "changed": 1,
        },
    }


def test_trade_assign_roles_defaults_to_phase1_dry_run() -> None:
    args = cli.parser().parse_args(
        [
            "trade-assign-roles",
            "--provider",
            "1",
            "--provider",
            "2",
            "--seller",
            "3",
            "--seller",
            "4",
            "--seller",
            "5",
            "--observer",
            "6",
            "--observer",
            "7",
            "--observer",
            "8",
        ]
    )

    assert args.provider == [1, 2]
    assert args.seller == [3, 4, 5]
    assert args.observer == [6, 7, 8]
    assert args.config_version == "phase1-seller-duty-v2"
    assert args.apply is False


def test_trade_bind_withdraw_accounts_defaults_to_dry_run() -> None:
    args = cli.parser().parse_args(
        [
            "trade-bind-withdraw-accounts",
            "--proxy-map-file",
            "proxy-map.json",
            "--pool-file",
            "withdraw-pool.json",
            "--merchant",
            "5",
        ]
    )

    assert args.merchant == [5]
    assert args.apply is False
    assert args.config_version == "phase1-withdraw-account-v1"


def test_trade_bind_withdraw_accounts_wires_private_inputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions = tmp_path / "sessions"
    trade_session_file(sessions / "session-one.json", 1)
    proxy_secret = "socks5://user:proxy-secret@proxy.invalid:1080"
    proxy_map = private_file(
        tmp_path / "proxy-map.json",
        json.dumps({"1001": proxy_secret}),
    )
    pool = private_file(
        tmp_path / "withdraw-pool.json",
        json.dumps([{"network": "ERC20", "address": "0xfixtureaddress123456"}]),
    )
    monkeypatch.setenv("FIXTURE_TRADE_DSN", "postgresql://private:secret@db/trade")
    monkeypatch.setattr(cli, "PostgresWithdrawAccountStore", lambda factory: ("store", factory))
    captured: dict[str, object] = {}

    class FakeService:
        def __init__(
            self,
            store,
            received_pool,
            client_factory,
            **kwargs,
        ):
            captured.update(
                store=store,
                pool=received_pool,
                client_factory=client_factory,
                kwargs=kwargs,
            )

        def execute(self, merchant_ids, *, apply):
            captured.update(merchant_ids=tuple(merchant_ids), apply=apply)
            return WithdrawBatchResult(
                applied=apply,
                decisions=(),
                pool_entries=1,
                available_pool_count=1,
            )

    monkeypatch.setattr(cli, "WithdrawAccountService", FakeService)
    args = cli.parser().parse_args(
        [
            "trade-bind-withdraw-accounts",
            "--database-dsn-env",
            "FIXTURE_TRADE_DSN",
            "--sessions-dir",
            str(sessions),
            "--proxy-map-file",
            str(proxy_map),
            "--pool-file",
            str(pool),
            "--merchant",
            "1",
        ]
    )

    result = cli.trade_bind_withdraw_accounts_command(args)

    assert captured["merchant_ids"] == (1,)
    assert captured["apply"] is False
    assert result["mode"] == "dry-run"
    assert result["withdrawal_submitted"] is False
    assert "fixtureaddress" not in json.dumps(result)
    assert "proxy-secret" not in json.dumps(result)


@pytest.mark.parametrize("apply", [False, True])
def test_trade_assign_roles_wires_atomic_batch_without_returning_dsn(
    apply: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_secret = "postgresql://private:database-secret@db.invalid/trade"
    monkeypatch.setenv("FIXTURE_TRADE_DSN", database_secret)
    connection = SimpleNamespace(closed=False)
    connection.close = lambda: setattr(connection, "closed", True)
    monkeypatch.setattr(cli, "connect_postgres", lambda dsn: connection)
    captured = {}

    def assign(received, assignments, *, apply, config_version):
        assignments = tuple(assignments)
        captured.update(
            connection=received,
            assignments=assignments,
            apply=apply,
            config_version=config_version,
        )
        return RoleAssignmentResult(
            applied=apply,
            decisions=tuple(
                RoleAssignmentDecision(
                    merchant_id=item.merchant_id,
                    role=item.role,
                    previous_roles=("unassigned",),
                    change_required=True,
                )
                for item in assignments
            ),
            audit_action_count=len(assignments) if apply else 0,
        )

    monkeypatch.setattr(cli, "assign_merchant_roles", assign)
    arguments = [
        "trade-assign-roles",
        "--database-dsn-env",
        "FIXTURE_TRADE_DSN",
        "--provider",
        "1",
        "--provider",
        "2",
        "--seller",
        "3",
        "--seller",
        "4",
        "--seller",
        "5",
        "--observer",
        "6",
        "--observer",
        "7",
        "--observer",
        "8",
    ]
    if apply:
        arguments.append("--apply")

    result = cli.trade_assign_roles_command(cli.parser().parse_args(arguments))

    assert captured["connection"] is connection
    assert captured["apply"] is apply
    assert captured["config_version"] == "phase1-seller-duty-v2"
    assert [(item.merchant_id, item.role) for item in captured["assignments"]] == [
        (1, "provider"),
        (2, "provider"),
        (3, "seller"),
        (4, "seller"),
        (5, "seller"),
        (6, "observer"),
        (7, "observer"),
        (8, "observer"),
    ]
    assert result["mode"] == ("applied" if apply else "dry-run")
    assert result["base_role_counts"] == {"seller": 8}
    assert result["duty_counts"] == {"none": 3, "observer": 3, "provider": 2}
    assert result["assignment_counts"] == {
        "observer": 3,
        "provider": 2,
        "seller": 3,
    }
    assert result["audit_action_count"] == (8 if apply else 0)
    assert connection.closed is True
    assert "database-secret" not in json.dumps(result)
