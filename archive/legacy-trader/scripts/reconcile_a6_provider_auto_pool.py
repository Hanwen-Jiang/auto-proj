#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import secrets
import socket
import stat
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import suppress
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row

from a6api_auto.client import A6ApiClient
from a6api_auto.enodot_admin import EnodotAdminClient
from a6api_auto.storage import read_private_json
from a6api_auto.trade_authenticated_client import authenticated_a6_client
from a6api_auto.trade_pin_runner import PostgresPinAuditStore
from a6api_auto.trade_provider_auto_pool import (
    AutoPoolMarketQuote,
    ProviderAutoPoolError,
    ProviderAutoPoolTarget,
    plan_provider_auto_pool,
    reconcile_provider_auto_pool,
)
from a6api_auto.trade_provider_auto_pool_store import (
    PostgresProviderAutoPoolStore,
    ProviderAutoPoolAccount,
    ProviderAutoPoolGeneration,
    provider_auto_pool_execution_hash,
)
from a6api_auto.trade_provider_pool_reconciler import PostgresProviderModelLease

UTC = timezone.utc


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(
        description="Reconcile event-driven A6 Provider marketplace auto-pools"
    )
    command.add_argument("--apply", action="store_true")
    command.add_argument("--enabled-env", default="PROVIDER_AUTO_POOL_ENABLED")
    command.add_argument("--database-dsn-env", default="A6_TRADE_DATABASE_DSN")
    command.add_argument("--model", default="gpt-5.6-sol")
    command.add_argument("--enodot-url", default="https://ai.enodot.shop")
    command.add_argument(
        "--enodot-admin-key-file",
        type=Path,
        default=Path("/var/lib/a6-trader/secrets/enodot-admin-key.secret"),
    )
    command.add_argument(
        "--sessions-dir",
        type=Path,
        default=Path("/var/lib/a6-trader/secrets/sessions"),
    )
    command.add_argument(
        "--proxy-map-file",
        type=Path,
        default=Path("/var/lib/a6-trader/secrets/proxy-map.json"),
    )
    command.add_argument("--observer-snapshot-max-age-seconds", type=int, default=30)
    command.add_argument("--minimum-observers", type=int, default=2)
    command.add_argument("--target-channel-count", type=int, default=5)
    command.add_argument("--hard-max-ratio-cny", type=Decimal, default=Decimal("0.3"))
    command.add_argument("--minimum-ready-accounts", type=int, default=4)
    command.add_argument("--max-workers", type=int, default=6)
    command.add_argument("--marketplace-page-size", type=int, default=100)
    command.add_argument("--request-timeout-seconds", type=float, default=5.0)
    command.add_argument("--stale-account-claim-seconds", type=int, default=120)
    command.add_argument("--trigger-reason", default="observer_success")
    command.add_argument("--config-version", default="provider-auto-pool-v1")
    return command


def run(args: argparse.Namespace) -> dict[str, Any]:
    model = str(args.model or "").strip()
    if not model:
        raise ProviderAutoPoolError("provider_auto_pool_model_invalid")
    if not 1 <= args.minimum_observers <= 12:
        raise ProviderAutoPoolError("provider_auto_pool_minimum_observers_invalid")
    if not 1 <= args.target_channel_count <= 100:
        raise ProviderAutoPoolError("provider_auto_pool_target_count_invalid")
    if not 1 <= args.minimum_ready_accounts <= 12:
        raise ProviderAutoPoolError("provider_auto_pool_minimum_ready_invalid")
    if not 1 <= args.max_workers <= 32:
        raise ProviderAutoPoolError("provider_auto_pool_workers_invalid")
    if not 1 <= args.marketplace_page_size <= 1000:
        raise ProviderAutoPoolError("provider_auto_pool_page_size_invalid")
    if not 1 <= args.observer_snapshot_max_age_seconds <= 300:
        raise ProviderAutoPoolError("provider_auto_pool_snapshot_age_invalid")
    if not 0 < args.request_timeout_seconds <= 60:
        raise ProviderAutoPoolError("provider_auto_pool_timeout_invalid")
    if not 5 <= args.stale_account_claim_seconds <= 3600:
        raise ProviderAutoPoolError("provider_auto_pool_stale_claim_invalid")
    hard_max = _positive_decimal(
        args.hard_max_ratio_cny,
        "provider_auto_pool_hard_max_invalid",
    )
    dsn = str(os.environ.get(args.database_dsn_env, "")).strip()
    if not dsn:
        raise ProviderAutoPoolError("provider_auto_pool_database_dsn_missing")
    if args.apply and str(os.environ.get(args.enabled_env, "")).strip().casefold() not in {
        "1",
        "true",
        "yes",
        "on",
    }:
        raise ProviderAutoPoolError("provider_auto_pool_apply_not_enabled")

    def connection_factory() -> Any:
        return psycopg.connect(dsn, row_factory=dict_row, connect_timeout=5)

    store = PostgresProviderAutoPoolStore(
        connection_factory,
        stale_claim_seconds=args.stale_account_claim_seconds,
    )
    pin_store = PostgresPinAuditStore(
        connection_factory,
        requested_by="provider-auto-pool",
    )
    accounts = store.load_accounts(model)
    if len(accounts) < args.minimum_ready_accounts:
        raise ProviderAutoPoolError("provider_auto_pool_accounts_below_minimum")
    owned = store.owned_resources(model)
    runtime_context = store.active_runtime_context(model)
    observer_ids = tuple(item.merchant_id for item in pin_store.observer_contexts())
    snapshot = pin_store.latest_observer_snapshot(
        model,
        observer_merchant_ids=observer_ids,
        max_age_seconds=args.observer_snapshot_max_age_seconds,
    )
    if not snapshot.scans or snapshot.latest_completed_at_epoch is None:
        raise ProviderAutoPoolError("provider_auto_pool_observer_snapshot_empty")
    quotes = tuple(_auto_pool_quote(item) for item in snapshot.quotes)
    plan = plan_provider_auto_pool(
        quotes,
        model=model,
        owned=owned,
        minimum_observers=args.minimum_observers,
        target_channel_count=args.target_channel_count,
        hard_max_ratio=hard_max,
        expand_above_ratio=(
            runtime_context.max_ratio_cny
            if runtime_context is not None
            and runtime_context.failed_account_count > 0
            else None
        ),
    )
    trigger_reason = str(args.trigger_reason or "").strip()
    if runtime_context is not None and runtime_context.failed_account_count > 0:
        trigger_reason = "runtime_range_expansion"
    elif runtime_context is not None and runtime_context.unfunded_account_count > 0:
        trigger_reason = "liquidity_repair"
    source_hash = provider_auto_pool_execution_hash(plan, owned, accounts)
    plan_document = {
        "success": True,
        "apply": bool(args.apply),
        "model": model,
        "source_hash": source_hash,
        "max_ratio_cny": str(plan.max_ratio),
        "max_ratio_usd": str(plan.max_ratio_usd),
        "selected_channel_ids": [item.channel_id for item in plan.selected],
        "eligible_channel_count": len(plan.eligible),
        "trigger_reason": trigger_reason,
        "expanded_from_max_ratio_cny": (
            str(runtime_context.max_ratio_cny)
            if runtime_context is not None
            and runtime_context.failed_account_count > 0
            else None
        ),
        "account_count": len(accounts),
        "observer_scan_ids": [int(item["scan_run_id"]) for item in snapshot.scans],
    }
    if not args.apply:
        return plan_document

    with PostgresProviderModelLease(connection_factory).hold(model):
        generation = store.begin_generation(
            plan,
            accounts=accounts,
            observer_scan_ids=[int(item["scan_run_id"]) for item in snapshot.scans],
            observed_at=datetime.fromtimestamp(snapshot.latest_completed_at_epoch, tz=UTC),
            trigger_reason=trigger_reason,
            config_version=args.config_version,
            source_hash=source_hash,
            minimum_observers=args.minimum_observers,
            target_channel_count=args.target_channel_count,
            hard_max_ratio_cny=hard_max,
        )
        operation_id = secrets.token_hex(24)

        admin = EnodotAdminClient(
            args.enodot_url,
            _private_text(args.enodot_admin_key_file),
            timeout=args.request_timeout_seconds,
        )
        proxies = _proxy_map(args.proxy_map_file)
        sessions_root = args.sessions_dir.expanduser().resolve()
        store.claim_accounts(
            generation,
            accounts,
            claim_token=operation_id,
            claimed_by=f"provider-auto-pool:{socket.gethostname()}:{os.getpid()}",
        )
        prepared: list[ProviderAutoPoolAccount] = []
        failures: list[dict[str, Any]] = []
        with ThreadPoolExecutor(max_workers=min(args.max_workers, len(accounts))) as executor:
            futures = {
                executor.submit(
                    _prepare_account,
                    store,
                    admin,
                    generation,
                    account,
                    plan,
                    proxies,
                    sessions_root,
                    args.marketplace_page_size,
                    args.request_timeout_seconds,
                    operation_id,
                ): account
                for account in accounts
            }
            for future in as_completed(futures):
                account = futures[future]
                try:
                    future.result()
                    prepared.append(account)
                except Exception as exc:
                    error_class = _error_code(exc)
                    _keep_paused(
                        admin,
                        account,
                        generation,
                        "prepare-failed",
                        operation_id,
                    )
                    store.record_failed(
                        generation,
                        account,
                        claim_token=operation_id,
                        error_class=error_class,
                    )
                    failures.append(_failure(account, error_class))

        if len(prepared) < args.minimum_ready_accounts:
            store.fail_generation(
                generation,
                error_class="provider_auto_pool_minimum_ready_not_met",
            )
            raise ProviderAutoPoolError("provider_auto_pool_minimum_ready_not_met")

        ready: list[ProviderAutoPoolAccount] = []
        with ThreadPoolExecutor(max_workers=min(args.max_workers, len(prepared))) as executor:
            futures = {
                executor.submit(
                    _resume_account,
                    store,
                    admin,
                    generation,
                    account,
                    operation_id,
                ): account
                for account in prepared
            }
            for future in as_completed(futures):
                account = futures[future]
                try:
                    future.result()
                    ready.append(account)
                except Exception as exc:
                    error_class = _error_code(exc)
                    _keep_paused(
                        admin,
                        account,
                        generation,
                        "resume-failed",
                        operation_id,
                    )
                    store.record_failed(
                        generation,
                        account,
                        claim_token=operation_id,
                        error_class=error_class,
                    )
                    failures.append(_failure(account, error_class))

        if len(ready) < args.minimum_ready_accounts:
            for account in ready:
                _keep_paused(
                    admin,
                    account,
                    generation,
                    "activation-aborted",
                    operation_id,
                )
            store.fail_generation(
                generation,
                error_class="provider_auto_pool_minimum_ready_not_met",
            )
            raise ProviderAutoPoolError("provider_auto_pool_minimum_ready_not_met")
        try:
            activated = store.activate(
                model,
                generation,
                minimum_ready=args.minimum_ready_accounts,
            )
        except Exception:
            for account in ready:
                _keep_paused(
                    admin,
                    account,
                    generation,
                    "activation-failed",
                    operation_id,
                )
            store.fail_generation(
                generation,
                error_class="provider_auto_pool_activation_failed",
            )
            raise
        return {
            **plan_document,
            "status": (
                "active_refreshed"
                if generation.reused_active
                else "active_resumed"
                if generation.reused_inflight
                else "active"
            ),
            "auto_pool_generation": generation.generation,
            "generation_reused": generation.reused_active or generation.reused_inflight,
            "ready_accounts": activated,
            "failed_accounts": len(failures),
            "failures": sorted(failures, key=lambda item: item["slot_no"]),
        }


def _prepare_account(
    store: PostgresProviderAutoPoolStore,
    admin: EnodotAdminClient,
    generation: ProviderAutoPoolGeneration,
    account: ProviderAutoPoolAccount,
    plan: Any,
    proxies: dict[int, str],
    sessions_root: Path,
    page_size: int,
    request_timeout_seconds: float,
    operation_id: str,
) -> None:
    paused = admin.set_schedulable(
        account.enodot_account_id,
        False,
        idempotency_key=_key(generation, account, "pause", operation_id),
    )
    if paused.get("schedulable") is not False:
        paused = admin.get_account(account.enodot_account_id)
    if paused.get("schedulable") is not False:
        raise ProviderAutoPoolError("provider_auto_pool_pause_readback_failed")
    session_path = Path(account.session_ref).expanduser().resolve()
    if not session_path.is_relative_to(sessions_root):
        raise ProviderAutoPoolError("provider_auto_pool_session_ref_outside_root")
    proxy = str(proxies.get(account.a6_user_id) or "").strip()
    client, _record = authenticated_a6_client(
        session_path,
        expected_user_id=account.a6_user_id,
        proxy_url=proxy,
        client_factory=lambda record, fixed_proxy: A6ApiClient(
            record.origin,
            proxy_url=fixed_proxy,
            timeout=request_timeout_seconds,
        ),
    )
    remote = reconcile_provider_auto_pool(
        client,
        ProviderAutoPoolTarget(
            account.a6_user_id,
            account.external_token_id,
            plan.model,
        ),
        plan,
        store.owned_resources(plan.model, merchant_id=account.merchant_id),
        page_size=page_size,
        owned_listings_by_channel=store.owned_listings_by_channel(
            plan.model,
            merchant_id=account.merchant_id,
        ),
    )
    current = admin.get_account(account.enodot_account_id)
    extra = current.get("extra") if isinstance(current.get("extra"), dict) else {}
    desired_extra = {
        **extra,
        "a6_route_mode": "auto_pool",
        "a6_auto_pool_generation": generation.generation,
        "a6_auto_pool_subscription_id": remote.subscription_id,
        "a6_auto_pool_max_ratio_cny": str(plan.max_ratio),
    }
    admin.update_account(
        account.enodot_account_id,
        {
            "rate_multiplier": float(plan.max_ratio_usd),
            "extra": desired_extra,
        },
        idempotency_key=_key(generation, account, "cost", operation_id),
    )
    observed = admin.get_account(account.enodot_account_id)
    observed_multiplier = _positive_decimal(
        observed.get("rate_multiplier"),
        "provider_auto_pool_cost_readback_failed",
    )
    if (
        abs(observed_multiplier - plan.max_ratio_usd) > Decimal("0.000000000001")
        or observed.get("schedulable") is not False
    ):
        raise ProviderAutoPoolError("provider_auto_pool_cost_readback_failed")
    store.record_prepared(
        generation,
        account,
        claim_token=operation_id,
        subscription_id=remote.subscription_id,
        visible_owned_channel_ids=remote.visible_owned_channel_ids,
        disabled_owned_channel_ids=remote.confirmed_disabled_channel_ids,
        max_ratio_cny=plan.max_ratio,
        max_ratio_usd=plan.max_ratio_usd,
        readback={
            "subscription_id": remote.subscription_id,
            "visible_owned_channel_ids": list(remote.visible_owned_channel_ids),
            "newly_disabled_owned_channel_ids": list(remote.disabled_channel_ids),
            "disabled_owned_channel_ids": list(
                remote.confirmed_disabled_channel_ids
            ),
            "max_ratio_cny": str(plan.max_ratio),
            "max_ratio_usd": str(plan.max_ratio_usd),
            "auto_channel_ids": sorted(
                int(item["channel_id"])
                for item in remote.subscription.get("auto_channels", [])
                if isinstance(item, dict) and item.get("channel_id")
            ),
            "enodot_schedulable": False,
            "enodot_rate_multiplier": str(plan.max_ratio_usd),
        },
    )


def _resume_account(
    store: PostgresProviderAutoPoolStore,
    admin: EnodotAdminClient,
    generation: ProviderAutoPoolGeneration,
    account: ProviderAutoPoolAccount,
    operation_id: str,
) -> None:
    observed = admin.set_schedulable(
        account.enodot_account_id,
        True,
        idempotency_key=_key(generation, account, "resume", operation_id),
    )
    if observed.get("schedulable") is not True:
        observed = admin.get_account(account.enodot_account_id)
    if observed.get("schedulable") is not True:
        raise ProviderAutoPoolError("provider_auto_pool_resume_readback_failed")
    store.record_ready(generation, account, claim_token=operation_id)


def _keep_paused(
    admin: EnodotAdminClient,
    account: ProviderAutoPoolAccount,
    generation: ProviderAutoPoolGeneration,
    stage: str,
    operation_id: str,
) -> None:
    with suppress(Exception):
        admin.set_schedulable(
            account.enodot_account_id,
            False,
            idempotency_key=_key(generation, account, stage, operation_id),
        )


def _auto_pool_quote(quote: Any) -> AutoPoolMarketQuote:
    return AutoPoolMarketQuote(
        observer_merchant_id=int(quote.observer_merchant_id),
        channel_id=int(quote.channel_id),
        model=str(quote.model),
        realtime_ratio_cny=quote.realtime_ratio_cny,
        realtime_ratio_usd=quote.realtime_ratio_usd or quote.final_multiplier,
        available=bool(quote.available),
        health_state=str(quote.health_state or "unknown"),
        supplier_id=quote.supplier_id,
        listing_id=quote.listing_id,
        last_success_at=int(quote.last_success_at) if quote.last_success_at else None,
        latency_ms=quote.recent_p50_ms or quote.p50_ttft_ms,
    )


def _proxy_map(path: Path) -> dict[int, str]:
    raw = read_private_json(path.expanduser().resolve())
    try:
        result = {int(key): str(value).strip() for key, value in raw.items()}
    except (AttributeError, TypeError, ValueError):
        raise ProviderAutoPoolError("provider_auto_pool_proxy_map_invalid") from None
    if not result or any(key < 1 or not value for key, value in result.items()):
        raise ProviderAutoPoolError("provider_auto_pool_proxy_map_invalid")
    return result


def _private_text(path: Path) -> str:
    resolved = path.expanduser().resolve()
    try:
        info = resolved.stat()
        value = resolved.read_text(encoding="utf-8").strip()
    except OSError:
        raise ProviderAutoPoolError("provider_auto_pool_admin_key_unreadable") from None
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or not value:
        raise ProviderAutoPoolError("provider_auto_pool_admin_key_invalid")
    return value


def _positive_decimal(value: object, code: str) -> Decimal:
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ProviderAutoPoolError(code) from None
    if not result.is_finite() or result <= 0:
        raise ProviderAutoPoolError(code)
    return result


def _key(
    generation: ProviderAutoPoolGeneration,
    account: ProviderAutoPoolAccount,
    stage: str,
    operation_id: str,
) -> str:
    return (
        f"auto-pool:{generation.generation}:{operation_id}:"
        f"{account.enodot_account_id}:{stage}"
    )[:128]


def _error_code(exc: BaseException) -> str:
    raw = str(getattr(exc, "code", "") or type(exc).__name__).strip().casefold()
    return "_".join(part for part in raw.replace("-", "_").split("_") if part)[:160]


def _failure(account: ProviderAutoPoolAccount, error_class: str) -> dict[str, Any]:
    return {
        "slot_no": account.slot_no,
        "merchant_id": account.merchant_id,
        "enodot_account_id": account.enodot_account_id,
        "error_class": error_class,
    }


def main() -> None:
    args = parser().parse_args()
    try:
        result = run(args)
    except Exception as exc:
        print(
            json.dumps(
                {"success": False, "error_class": _error_code(exc)},
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ),
            file=sys.stderr,
        )
        raise SystemExit(1) from None
    print(
        json.dumps(result, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    )


if __name__ == "__main__":
    main()
