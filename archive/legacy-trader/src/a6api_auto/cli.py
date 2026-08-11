from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import stat
import sys
import time
from collections import Counter
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .batch import BatchConfig, SlowBatchRunner
from .client import A6ApiClient
from .domains import load_upstream_domains
from .enodot import EnodotGroupKeyProvider
from .enodot_admin import EnodotAdminClient
from .errors import A6AutoError
from .mail import QQMailbox
from .singbox import SingBoxPool, load_subscription_urls
from .storage import (
    SessionStore,
    private_file_lock,
    read_private_json,
    write_private_json,
)
from .templates import (
    choose_account_template,
    choose_template,
    load_templates,
    load_workbook_template_catalog,
)
from .trade_authenticated_client import (
    AuthenticatedClientFactory,
    AuthenticatedSessionError,
    authenticated_a6_client,
)
from .trade_channel_growth import (
    CallbackTransport,
    ChannelGrowthError,
    ChannelGrowthRunner,
    PostgresChannelGrowthStore,
    authenticated_channel_growth_client,
)
from .trade_collector import A6CollectionService, CollectorWatermarks
from .trade_dashboard import serve_dashboard
from .trade_dashboard_management import DashboardManagementService
from .trade_db import (
    apply_migrations,
    connect_postgres,
    load_session_metadata,
    metadata_from_session,
    upsert_merchant_metadata,
)
from .trade_enodot_seller_resources import (
    ManagedSellerError,
    ManagedSellerService,
    PostgresManagedSellerStore,
    PrivateFileManagedSellerSecretSink,
)
from .trade_managed_autoonboard import (
    ManagedAutoOnboardError,
    ManagedAutoOnboardRequest,
    ManagedChannelAutoOnboarder,
    PostgresManagedAutoOnboardDiscovery,
    PostgresManagedAutoOnboardPriceRecorder,
)
from .trade_managed_listing_store import PostgresManagedListingStore
from .trade_managed_listings import (
    ManagedListingController,
    ManagedListingError,
    is_source_only_listing_description,
)
from .trade_merchant_recovery import PostgresMerchantRecoveryStore
from .trade_pin_failure_journal import PinFailureJournal
from .trade_pin_runner import PostgresPinAuditStore, ProviderPinRunner
from .trade_profit_settings import PostgresModelProfitSettingStore
from .trade_provider_fast_block import RedisProviderFastBlockProjector
from .trade_provider_topup import (
    DEFAULT_FLOOR_UNITS,
    A6ProviderTopupRemote,
    PostgresProviderTopupStore,
    ProviderTopupContext,
    ProviderTopupError,
    ProviderTopupService,
    authenticated_provider_topup_client,
)
from .trade_publication_control import AtomicPublicationTrigger, PostgresPublicationControl
from .trade_repository import PostgresTradeRepository
from .trade_roles import RoleAssignment, RoleAssignmentError, assign_merchant_roles
from .trade_seller_orchestration import (
    GPT_5_6_SOL_OFFICIAL_PRICES,
    SellerOrchestrationError,
    SellerPricing,
    SellerResourceBatchRequest,
    SellerResourceOrchestrator,
)
from .trade_seller_price_band import (
    PostgresSellerPriceBandStore,
    SellerPriceBandError,
    SellerPriceBandOrchestrator,
    SellerPriceBandRequest,
)
from .trade_seller_retirement import (
    A6SellerRetirementRemote,
    PostgresSellerRetirementStore,
    SellerRetirementError,
    SellerRetirementService,
)
from .trade_seller_upstreams import (
    SellerUpstreamBatchRequest,
    SellerUpstreamReconciler,
)
from .trade_session_refresh import TradeSessionRefreshService
from .trade_withdraw_accounts import (
    PostgresWithdrawAccountStore,
    WithdrawAccountError,
    WithdrawAccountService,
    load_withdraw_pool,
)
from .workflow import Workflow, allocate_session, load_qq_addresses

DEFAULT_TRADE_DATABASE_DSN_ENV = "A6_TRADE_DATABASE_DSN"
DEFAULT_TRADE_SESSIONS_DIR = Path("~/.config/a6api-auto/sessions").expanduser()
DEFAULT_SELLER_KEY_DIR = Path("~/.config/a6api-auto/enodot-seller-keys").expanduser()
_SYSTEMD_PARTIAL_RESULT_COMMANDS = frozenset(
    {
        "trade-auto-onboard-seller-channels",
        "trade-apply-seller-price-band",
        "trade-pin-providers-once",
        "trade-retire-sellers-once",
        "trade-reconcile-channel-growth-once",
    }
)


def authenticated_withdraw_client(
    merchant: Any,
    *,
    session_paths_by_user_id: Mapping[int, Path],
    proxy_by_user_id: Mapping[int, str],
    client_factory: AuthenticatedClientFactory | None = None,
) -> Any:
    proxy_url = str(proxy_by_user_id.get(merchant.a6_user_id) or "").strip()
    if not proxy_url:
        raise WithdrawAccountError("withdraw_proxy_mapping_missing")
    session_ref = session_paths_by_user_id.get(
        merchant.a6_user_id,
        Path(merchant.session_ref),
    )
    try:
        client, record = authenticated_a6_client(
            session_ref,
            expected_user_id=merchant.a6_user_id,
            proxy_url=proxy_url,
            client_factory=client_factory,
        )
    except AuthenticatedSessionError as exc:
        code = {
            "session_user_identity_mismatch": "withdraw_session_mismatch",
            "session_user_id_missing": "withdraw_session_mismatch",
            "session_not_refreshable": "withdraw_session_mismatch",
            "session_proxy_missing": "withdraw_proxy_mapping_missing",
        }.get(exc.code, f"withdraw_{exc.code}")
        raise WithdrawAccountError(code) from exc
    if record.status != "supplier_submitted" or record.supplier_id != merchant.supplier_id:
        raise WithdrawAccountError("withdraw_session_mismatch")
    return client


def authenticated_seller_client(
    context: Any,
    *,
    proxy_by_user_id: Mapping[int, str],
    client_factory: AuthenticatedClientFactory | None = None,
) -> Any:
    proxy_url = str(proxy_by_user_id.get(context.a6_user_id) or "").strip()
    if not proxy_url:
        raise SellerOrchestrationError("seller_proxy_mapping_missing")
    try:
        client, record = authenticated_a6_client(
            context.session_ref,
            expected_user_id=context.a6_user_id,
            proxy_url=proxy_url,
            client_factory=client_factory,
        )
    except AuthenticatedSessionError as exc:
        code = {
            "session_user_identity_mismatch": "seller_session_context_mismatch",
            "session_user_id_missing": "seller_session_context_mismatch",
            "session_not_refreshable": "seller_session_context_mismatch",
            "session_proxy_missing": "seller_proxy_mapping_missing",
        }.get(exc.code, f"seller_{exc.code}")
        raise SellerOrchestrationError(code) from exc
    if record.status != "supplier_submitted" or record.supplier_id != context.supplier_id:
        raise SellerOrchestrationError("seller_session_context_mismatch")
    return client


def cached_seller_client(
    context: Any,
    session: Any,
    *,
    proxy_by_user_id: Mapping[int, str],
    client_factory: AuthenticatedClientFactory | None = None,
) -> Any:
    """Use the independently refreshed cookie and let Seller preflight be the live gate."""
    proxy_url = str(proxy_by_user_id.get(context.a6_user_id) or "").strip()
    if not proxy_url:
        raise SellerOrchestrationError("seller_proxy_mapping_missing")
    if (
        session.user_id != context.a6_user_id
        or session.supplier_id != context.supplier_id
        or not session.origin
        or not session.cookies
    ):
        raise SellerOrchestrationError("seller_session_context_mismatch")
    builder = client_factory or (
        lambda record, proxy: A6ApiClient(record.origin, proxy_url=proxy)
    )
    client = builder(session, proxy_url)
    client.import_cookies(session.cookies)
    return client


def _secret_value(direct: str, env_name: str, file_path: str) -> str:
    candidates = [bool(direct), bool(env_name), bool(file_path)]
    if sum(candidates) != 1:
        raise A6AutoError("upstream_key_source_required")
    if direct:
        return direct.strip()
    if env_name:
        value = os.environ.get(env_name, "").strip()
        if not value:
            raise A6AutoError("upstream_key_environment_empty")
        return value
    path = Path(file_path).expanduser().resolve()
    try:
        info = path.stat()
        value = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise A6AutoError("upstream_key_file_unreadable") from exc
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or not value:
        raise A6AutoError("upstream_key_file_permissions")
    return value


def _proxy_pool(raw_path: str) -> tuple[str, ...]:
    if not raw_path:
        return ()
    path = Path(raw_path).expanduser().resolve()
    try:
        info = path.stat()
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise A6AutoError("proxy_file_unreadable") from exc
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
        raise A6AutoError("proxy_file_permissions")
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        parsed = [line.strip() for line in raw.splitlines() if line.strip()]
    if (
        not isinstance(parsed, list)
        or not parsed
        or not all(isinstance(item, str) for item in parsed)
    ):
        raise A6AutoError("proxy_file_shape_invalid")
    proxies: list[str] = []
    seen: set[str] = set()
    for raw_proxy in parsed:
        proxy = raw_proxy.strip()
        try:
            value = urlsplit(proxy)
            port = value.port
        except ValueError as exc:
            raise A6AutoError("proxy_url_invalid") from exc
        if (
            value.scheme not in {"http", "https", "socks5", "socks5h"}
            or not value.hostname
            or not port
        ):
            raise A6AutoError("proxy_url_invalid")
        if proxy not in seen:
            proxies.append(proxy)
            seen.add(proxy)
    if not proxies:
        raise A6AutoError("proxy_file_shape_invalid")
    return tuple(proxies)


def _proxy_from_file(raw_path: str) -> str:
    pool = _proxy_pool(raw_path)
    return secrets.choice(pool) if pool else ""


def _state_dir(value: str) -> Path:
    return Path(value or os.environ.get("A6_STATE_DIR") or "~/.config/a6api-auto").expanduser()


def _database_dsn(args: argparse.Namespace) -> str:
    raw_path = str(args.database_dsn_file or "")
    if raw_path:
        path = Path(raw_path).expanduser().resolve()
        try:
            info = path.stat()
        except OSError as exc:
            raise A6AutoError("trade_database_dsn_file_unreadable") from exc
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
            raise A6AutoError("trade_database_dsn_file_permissions")
        try:
            value = path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise A6AutoError("trade_database_dsn_file_unreadable") from exc
        if not value:
            raise A6AutoError("trade_database_dsn_file_permissions")
        return value

    env_name = str(args.database_dsn_env or "").strip()
    if not env_name:
        raise A6AutoError("trade_database_dsn_source_required")
    value = os.environ.get(env_name, "").strip()
    if not value:
        raise A6AutoError("trade_database_dsn_environment_empty")
    return value


def _trade_connection(dsn: str) -> Any:
    try:
        return connect_postgres(dsn)
    except Exception as exc:
        raise A6AutoError("trade_database_unavailable") from exc


def _close_connection(connection: Any) -> None:
    close = getattr(connection, "close", None)
    if callable(close):
        close()


def _trade_session_paths(directory: Path) -> tuple[Path, ...]:
    resolved = directory.expanduser().resolve()
    if not resolved.is_dir():
        raise A6AutoError("trade_sessions_directory_missing")
    paths = tuple(sorted(resolved.glob("session-*.json"), key=str))
    if not paths:
        raise A6AutoError("trade_sessions_empty")
    return paths


def _proxy_map(raw_path: Path | None) -> dict[int, str]:
    if raw_path is None:
        return {}
    path = raw_path.expanduser().resolve()
    try:
        info = path.stat()
    except OSError as exc:
        raise A6AutoError("collector_proxy_map_unreadable") from exc
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
        raise A6AutoError("collector_proxy_map_permissions")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise A6AutoError("collector_proxy_map_unreadable") from exc
    if not isinstance(payload, Mapping) or not payload:
        raise A6AutoError("collector_proxy_map_invalid")

    result: dict[int, str] = {}
    for raw_user_id, raw_proxy in payload.items():
        if not str(raw_user_id).isdigit() or not isinstance(raw_proxy, str):
            raise A6AutoError("collector_proxy_map_invalid")
        user_id = int(raw_user_id)
        if user_id <= 0:
            raise A6AutoError("collector_proxy_map_invalid")
        proxy = raw_proxy.strip()
        try:
            value = urlsplit(proxy)
            port = value.port
        except ValueError as exc:
            raise A6AutoError("collector_proxy_map_invalid") from exc
        if (
            value.scheme not in {"http", "https", "socks5", "socks5h"}
            or not value.hostname
            or not port
        ):
            raise A6AutoError("collector_proxy_map_invalid")
        result[user_id] = proxy
    if len(set(result.values())) != len(result):
        raise A6AutoError("collector_proxy_map_duplicate_proxy")
    return result


def _session_proxy_metadata(
    paths: tuple[Path, ...],
    proxy_by_user_id: Mapping[int, str],
) -> dict[str, dict[str, str]]:
    if not proxy_by_user_id:
        return {}
    metadata: dict[str, dict[str, str]] = {}
    for path in paths:
        record = SessionStore(path).load()
        if not record.user_id:
            continue
        user_id = int(record.user_id)
        proxy = proxy_by_user_id.get(user_id)
        if not proxy:
            if record.status == "supplier_submitted":
                raise A6AutoError("trade_import_proxy_mapping_missing")
            continue
        try:
            parsed = urlsplit(proxy)
            port = parsed.port
        except ValueError as exc:
            raise A6AutoError("trade_import_proxy_lease_invalid") from exc
        if parsed.hostname not in {"127.0.0.1", "localhost", "::1"} or not port:
            raise A6AutoError("trade_import_proxy_lease_invalid")
        fingerprint = hashlib.sha256(proxy.encode()).hexdigest()
        metadata[str(path.expanduser().resolve())] = {
            "lease_id": f"singbox:{port}",
            "proxy_fingerprint": fingerprint,
            "pool": "fixed-map",
            "provider": "singbox",
        }
    return metadata


def _positive_id_map(raw_path: Path | None, *, error: str) -> dict[int, int]:
    if raw_path is None:
        return {}
    path = raw_path.expanduser().resolve()
    try:
        info = path.stat()
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise A6AutoError(error) from exc
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
        raise A6AutoError(f"{error}_permissions")
    if not isinstance(payload, Mapping):
        raise A6AutoError(error)
    result: dict[int, int] = {}
    for raw_key, raw_value in payload.items():
        try:
            key = int(raw_key)
            value = int(raw_value)
        except (TypeError, ValueError):
            raise A6AutoError(error) from None
        if isinstance(raw_value, bool) or key < 1 or value < 1:
            raise A6AutoError(error)
        result[key] = value
    return result


def _merchant_text_map(raw_path: Path | None, *, error: str) -> dict[int, str]:
    if raw_path is None:
        raise A6AutoError(error)
    path = raw_path.expanduser().resolve()
    try:
        info = path.stat()
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise A6AutoError(error) from exc
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
        raise A6AutoError(f"{error}_permissions")
    if not isinstance(payload, Mapping):
        raise A6AutoError(error)
    result: dict[int, str] = {}
    for raw_key, raw_value in payload.items():
        try:
            key = int(raw_key)
        except (TypeError, ValueError):
            raise A6AutoError(error) from None
        if not isinstance(raw_value, str):
            raise A6AutoError(error)
        value = raw_value.strip()
        if key < 1 or not value or key in result:
            raise A6AutoError(error)
        result[key] = value
    return result


def _fill_template_listing_descriptions(
    seller_store: Any,
    merchant_ids: tuple[int, ...],
    configured: Mapping[int, str],
) -> dict[int, str]:
    descriptions = {
        merchant_id: description
        for merchant_id, description in configured.items()
        if merchant_id in set(merchant_ids)
    }
    for merchant_id in merchant_ids:
        if merchant_id in descriptions:
            continue
        context = seller_store.load_context(merchant_id)
        session = SessionStore(Path(context.session_ref)).load()
        if session.user_id != context.a6_user_id:
            raise A6AutoError("seller_listing_description_session_mismatch")
        descriptions[merchant_id] = choose_account_template(
            session.template_id,
            a6_user_id=context.a6_user_id,
        ).model_description
    return descriptions


def _validate_collector_routing(
    paths: tuple[Path, ...],
    proxy_by_user_id: Mapping[int, str],
    *,
    allow_direct: bool,
) -> int:
    eligible_user_ids: set[int] = set()
    for path in paths:
        record = SessionStore(path).load()
        if record.status == "supplier_submitted" and record.user_id:
            eligible_user_ids.add(int(record.user_id))
    if allow_direct:
        if len(eligible_user_ids) > 1:
            raise A6AutoError("collector_direct_requires_single_session")
        return len(eligible_user_ids)
    missing = eligible_user_ids - set(proxy_by_user_id)
    if missing:
        raise A6AutoError("collector_proxy_mapping_incomplete")
    return len(eligible_user_ids)


def _collector_watermarks(dsn: str) -> dict[int, CollectorWatermarks]:
    connection = _trade_connection(dsn)
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT m.a6_user_id,
                       coalesce((
                           SELECT max(e.a6_earning_id::bigint)
                           FROM a6_trade.a6_earning_events e
                           WHERE e.merchant_id = m.id
                       ), 0) AS earning_id,
                       coalesce((
                           SELECT max(
                               CASE
                                   WHEN u.a6_log_id ~ '^[0-9]+$'
                                   THEN u.a6_log_id::bigint
                               END
                           )
                           FROM a6_trade.a6_provider_usage_events u
                           WHERE u.merchant_id = m.id
                       ), 0) AS provider_log_id
                FROM a6_trade.a6_merchants m
                WHERE m.a6_user_id IS NOT NULL
                """
            )
            rows = cursor.fetchall()
    except Exception as exc:
        raise A6AutoError("trade_watermark_read_failed") from exc
    finally:
        _close_connection(connection)

    result: dict[int, CollectorWatermarks] = {}
    for row in rows:
        if isinstance(row, Mapping):
            user_id = int(row["a6_user_id"])
            earning_id = int(row["earning_id"])
            provider_log_id = int(row["provider_log_id"])
        else:
            user_id, earning_id, provider_log_id = (int(value) for value in row)
        result[user_id] = CollectorWatermarks(
            earning_id=earning_id,
            provider_log_id=provider_log_id,
        )
    return result


def _add_trade_database_arguments(command: argparse.ArgumentParser) -> None:
    command.add_argument(
        "--database-dsn-env",
        default=DEFAULT_TRADE_DATABASE_DSN_ENV,
        help=(
            f"environment containing the PostgreSQL DSN (default: {DEFAULT_TRADE_DATABASE_DSN_ENV})"
        ),
    )
    command.add_argument(
        "--database-dsn-file",
        type=Path,
        help="owner-only file containing the PostgreSQL DSN; takes precedence over the environment",
    )


def _add_trade_session_arguments(command: argparse.ArgumentParser) -> None:
    command.add_argument(
        "--sessions-dir",
        type=Path,
        default=DEFAULT_TRADE_SESSIONS_DIR,
        help=f"owner-only session directory (default: {DEFAULT_TRADE_SESSIONS_DIR})",
    )


def _add_systemd_partial_result_argument(command: argparse.ArgumentParser) -> None:
    command.add_argument(
        "--systemd-accept-partial-result",
        action="store_true",
        help="treat a structured partial result as a successful gated systemd run",
    )


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(description="a6api pure-HTTP account and supplier workflow")
    subcommands = command.add_subparsers(dest="command", required=True)

    run = subcommands.add_parser("run", help="register/login, create one key, and submit supplier")
    run.add_argument("--live", action="store_true", help="execute external mutations")
    run.add_argument("--qq-file", default=os.environ.get("A6_QQ_FILE", ""))
    run.add_argument("--email", default="", help="optional exact QQ address from the pool")
    run.add_argument("--state-dir", default="")
    run.add_argument("--session", type=Path, help="resume an existing owner-only session")
    run.add_argument("--template-id", default="", help="merchant-001..merchant-200")
    run.add_argument("--proxy-file", default="", help="optional owner-only proxy list")
    run.add_argument("--upstream-base-url", required=True)
    key = run.add_mutually_exclusive_group(required=True)
    key.add_argument("--upstream-api-key", default="")
    key.add_argument("--upstream-api-key-env", default="")
    key.add_argument("--upstream-api-key-file", default="")

    batch = subcommands.add_parser("batch", help="serial, resumable slow batch")
    batch_mode = batch.add_mutually_exclusive_group()
    batch_mode.add_argument("--live", action="store_true", help="execute external mutations")
    batch_mode.add_argument(
        "--prepare-only",
        action="store_true",
        help="freeze private batch state and print templates without external requests",
    )
    batch.add_argument("--qq-file", required=True)
    batch.add_argument("--state-dir", default="")
    batch.add_argument("--batch-state", type=Path, help="resume an existing batch state")
    batch_proxy = batch.add_mutually_exclusive_group(required=True)
    batch_proxy.add_argument("--proxy-file", default="", help="owner-only proxy pool")
    batch_proxy.add_argument(
        "--proxy-subscription-file",
        default="",
        help="owner-only sing-box subscription URL list",
    )
    batch.add_argument(
        "--proxy-binding-map-file",
        default="",
        help="owner-only user ID to stable proxy mapping updated after each success",
    )
    batch.add_argument("--proxy-subscription-insecure", action="store_true")
    batch.add_argument("--sing-box-executable", default="sing-box")
    batch.add_argument("--sing-box-port-start", type=int, default=19080)
    batch.add_argument("--count", type=int, default=20)
    batch.add_argument("--min-interval-minutes", type=float, default=180)
    batch.add_argument("--max-interval-minutes", type=float, default=480)
    batch.add_argument(
        "--distribution-window-hours",
        type=float,
        default=0,
        help="persist one randomized registration time per equal slot in this window",
    )
    batch.add_argument("--per-account-retries", type=int, default=3)
    batch.add_argument("--proxy-reservation-file", type=Path)
    batch.add_argument("--upstream-reservation-file", type=Path)
    batch.add_argument(
        "--template-catalog",
        choices=("json", "workbook"),
        default="json",
    )
    batch.add_argument(
        "--template-workbook",
        type=Path,
        help="optional workbook path; workbook mode otherwise uses the bundled catalog",
    )
    batch.add_argument("--operating-group", default="standard")
    batch.add_argument("--unique-upstream-base-urls", action="store_true")
    batch.add_argument("--upstream-base-url", default="")
    batch_key = batch.add_mutually_exclusive_group()
    batch_key.add_argument("--upstream-api-key", default="")
    batch_key.add_argument("--upstream-api-key-env", default="")
    batch_key.add_argument("--upstream-api-key-file", default="")
    batch.add_argument(
        "--upstream-domain-file",
        default="",
        help="optional JSON or line list; defaults to the bundled 30 domains",
    )
    batch.add_argument("--enodot-admin-url", default="https://ai.enodot.shop")
    batch.add_argument("--enodot-admin-key-env", default="AI_ENODOT_SHOP_ADMIN_KEY")
    batch.add_argument("--enodot-admin-key-file", default="")
    batch.add_argument("--enodot-admin-token-file", default="")
    batch.add_argument("--enodot-group", default="TPA6API")

    refresh = subcommands.add_parser("refresh-session", help="validate and relogin a session")
    refresh.add_argument("--session", type=Path, required=True)
    refresh.add_argument("--proxy-file", default="")

    trade_migrate = subcommands.add_parser(
        "trade-migrate",
        help="apply the idempotent Phase 0 PostgreSQL migrations",
    )
    _add_trade_database_arguments(trade_migrate)
    trade_migrate.add_argument(
        "--migration-dir",
        type=Path,
        help="optional migration directory; defaults to the bundled migrations",
    )

    trade_import = subcommands.add_parser(
        "trade-import-sessions",
        help="import secret-free metadata from persistent sessions",
    )
    _add_trade_database_arguments(trade_import)
    _add_trade_session_arguments(trade_import)
    trade_import.add_argument(
        "--proxy-map-file",
        type=Path,
        help="owner-only user-to-loopback-proxy map used to persist sing-box leases",
    )

    trade_refresh = subcommands.add_parser(
        "trade-refresh-sessions-once",
        help="refresh every eligible session and synchronize merchant metadata",
    )
    _add_trade_database_arguments(trade_refresh)
    _add_trade_session_arguments(trade_refresh)
    trade_refresh.add_argument("--proxy-map-file", type=Path, required=True)
    trade_refresh.add_argument("--client-timeout-seconds", type=float, default=10.0)
    trade_refresh.add_argument("--max-workers", type=int, default=4)
    trade_refresh.add_argument("--compact-output", action="store_true")

    trade_collect = subcommands.add_parser(
        "trade-collect-once",
        help="run one read-only A6 collection pass and persist normalized facts",
    )
    _add_trade_database_arguments(trade_collect)
    _add_trade_session_arguments(trade_collect)
    collector_routing = trade_collect.add_mutually_exclusive_group(required=True)
    collector_routing.add_argument(
        "--proxy-map-file",
        type=Path,
        help="owner-only JSON mapping every collectible A6 user ID to a unique stable proxy URL",
    )
    collector_routing.add_argument(
        "--allow-direct",
        action="store_true",
        help="explicitly allow direct egress when at most one session is collectible",
    )
    trade_collect.add_argument("--max-workers", type=int, default=4)
    trade_collect.add_argument(
        "--counterparty-settle-seconds",
        type=int,
        default=900,
        help=(
            "age an earning must reach before a missing provider match is treated as "
            "external revenue; younger earnings stay unclassified"
        ),
    )

    trade_dashboard = subcommands.add_parser(
        "trade-dashboard",
        help="serve the read-only Phase 0 overview dashboard",
    )
    _add_trade_database_arguments(trade_dashboard)
    trade_dashboard.add_argument(
        "--sessions-dir",
        type=Path,
        required=True,
        help="owner-only 0700 session directory used for on-demand credential reads",
    )
    trade_dashboard.add_argument("--host", default="127.0.0.1")
    trade_dashboard.add_argument("--port", type=int, default=8765)
    trade_dashboard.add_argument("--proxy-map-file", type=Path)
    trade_dashboard.add_argument("--proxy-pool-file", type=Path)
    trade_dashboard.add_argument("--batch-state-file", type=Path)
    trade_dashboard.add_argument(
        "--internal-api-token-env",
        default="A6_TRADER_INTERNAL_API_TOKEN",
        help="environment variable containing the internal control API bearer token",
    )
    trade_dashboard.add_argument(
        "--publication-trigger-file",
        type=Path,
        default=Path("/run/a6-trader/model-publication-request"),
    )
    trade_dashboard.add_argument(
        "--fast-block-redis-project",
        action="store_true",
        help="project the durable Provider fast-block outbox to Redis",
    )
    trade_dashboard.add_argument(
        "--fast-block-redis-url-env",
        default="PROVIDER_FAST_BLOCK_REDIS_URL",
        help="environment variable containing the Redis URL used only when projection is enabled",
    )
    trade_dashboard.add_argument(
        "--provider-channel-failure-threshold",
        type=int,
        default=3,
        help="consecutive canonical channel-owned failures required before a Provider Pin switch",
    )

    trade_pin = subcommands.add_parser(
        "trade-pin-providers-once",
        help="scan Observer market views and reconcile Provider Pins",
    )
    _add_trade_database_arguments(trade_pin)
    _add_trade_session_arguments(trade_pin)
    trade_pin.add_argument("--proxy-map-file", type=Path, required=True)
    trade_pin.add_argument("--pin-failure-journal-file", type=Path)
    trade_pin.add_argument("--pin-failure-cursor-file", type=Path)
    trade_pin.add_argument("--pin-failure-max-age-seconds", type=int, default=60)
    trade_pin.add_argument(
        "--pin-channel-failure-threshold",
        type=int,
        default=3,
        help="consecutive channel-owned journal failures required before a Pin switch",
    )
    trade_pin.add_argument("--model", default="gpt-5.6-sol")
    trade_pin.add_argument("--min-observers", type=int, default=2)
    trade_pin.add_argument("--top-k", type=int, default=5)
    trade_pin.add_argument("--page-size", type=int, default=100)
    trade_pin.add_argument("--observer-window-seconds", type=float, default=10.0)
    observer_round = trade_pin.add_mutually_exclusive_group()
    observer_round.add_argument("--observer-round-size", type=int, default=3)
    observer_round.add_argument(
        "--all-observers",
        action="store_true",
        help="scan the complete current Observer pool in one round",
    )
    trade_pin.add_argument(
        "--observer-snapshot-max-age-seconds",
        type=int,
        help=(
            "reuse each Observer's latest committed scan instead of issuing another market "
            "scan; 0 disables the age cutoff"
        ),
    )
    trade_pin.add_argument("--client-timeout-seconds", type=float, default=5.0)
    trade_pin.add_argument(
        "--observe-only",
        action="store_true",
        help="persist one staggered Observer market round without reconciling Providers",
    )
    trade_pin.add_argument(
        "--pin-probe-mode",
        choices=("required", "disabled"),
        default="required",
        help="require an activation request or trust Observer selection plus Pin readback",
    )
    trade_pin.add_argument("--probe-budget", type=int, default=4)
    trade_pin.add_argument("--probe-failure-ttl-seconds", type=int, default=45)
    trade_pin.add_argument("--probe-health-ttl-seconds", type=int, default=300)
    trade_pin.add_argument("--probe-max-output-tokens", type=int, default=16)
    trade_pin.add_argument("--probe-timeout-seconds", type=float, default=6.0)
    trade_pin.add_argument("--probe-round-time-budget-seconds", type=float, default=8.0)
    trade_pin.add_argument("--round-time-budget-seconds", type=float, default=90.0)
    trade_pin.add_argument("--pin-price-weight", default="1")
    trade_pin.add_argument("--pin-success-weight", default="0")
    trade_pin.add_argument("--pin-latency-weight", default="0")
    trade_pin.add_argument("--pin-price-span-ratio", default="0.25")
    trade_pin.add_argument(
        "--pin-min-success-rate",
        type=int,
        default=0,
        help="deprecated compatibility option; rolling success rate is observation-only",
    )
    trade_pin.add_argument("--pin-max-latency-ms", default="10000")
    trade_pin.add_argument("--pin-max-multiplier", default="0.05")
    trade_pin.add_argument("--pin-max-relative-cost-ratio", default="2")
    trade_pin.add_argument("--pin-failure-window-seconds", type=int, default=900)
    trade_pin.add_argument("--provider-initial-balance-ratio", default="0")
    trade_pin.add_argument("--minimum-schedulable-capacity", type=int, default=100)
    trade_pin.add_argument("--pin-min-hold-seconds", type=int, default=60)
    trade_pin.add_argument("--pin-min-switch-gain", default="0")
    trade_pin.add_argument("--emergency-boost-minimum-requests", type=int, default=3)
    trade_pin.add_argument("--emergency-boost-maximum-requests", type=int, default=5)
    trade_pin.add_argument("--emergency-boost-timeout-seconds", type=float, default=12.0)
    trade_pin.add_argument("--emergency-boost-cooldown-seconds", type=int, default=60)
    trade_pin.add_argument("--enodot-admin-url", default="https://ai.enodot.shop")
    trade_pin.add_argument("--enodot-admin-timeout-seconds", type=float, default=8.0)
    pin_admin = trade_pin.add_mutually_exclusive_group()
    pin_admin.add_argument("--enodot-admin-key-env", default="AI_ENODOT_SHOP_ADMIN_KEY")
    pin_admin.add_argument("--enodot-admin-key-file", default="")
    pin_admin.add_argument("--enodot-admin-token-file", type=Path)
    trade_pin.add_argument("--config-version", default="phase1-provider-pin-v1")
    trade_pin.add_argument("--compact-output", action="store_true")
    _add_systemd_partial_result_argument(trade_pin)
    trade_pin.add_argument("--apply", action="store_true")

    trade_roles = subcommands.add_parser(
        "trade-assign-roles",
        help="validate or apply an atomic merchant role assignment batch",
    )
    _add_trade_database_arguments(trade_roles)
    trade_roles.add_argument(
        "--provider",
        action="append",
        type=int,
        default=[],
        metavar="MERCHANT_ID",
        help="switch one Seller to Provider duty; repeat as needed",
    )
    trade_roles.add_argument(
        "--seller",
        action="append",
        type=int,
        default=[],
        metavar="MERCHANT_ID",
        help="keep one merchant as Seller-only and clear its active duty; repeat as needed",
    )
    trade_roles.add_argument(
        "--observer",
        action="append",
        type=int,
        default=[],
        metavar="MERCHANT_ID",
        help="switch one Seller to Observer duty; repeat as needed",
    )
    trade_roles.add_argument(
        "--config-version",
        default="phase1-seller-duty-v2",
        help="non-secret audit configuration version",
    )
    trade_roles.add_argument(
        "--apply",
        action="store_true",
        help="commit role changes; without this flag the command is a dry-run",
    )

    trade_withdraw_accounts = subcommands.add_parser(
        "trade-bind-withdraw-accounts",
        help="plan or create one exclusive A6 crypto withdrawal account per merchant",
    )
    _add_trade_database_arguments(trade_withdraw_accounts)
    _add_trade_session_arguments(trade_withdraw_accounts)
    trade_withdraw_accounts.add_argument(
        "--proxy-map-file",
        type=Path,
        required=True,
        help="owner-only JSON mapping A6 user IDs to unique stable proxy URLs",
    )
    trade_withdraw_accounts.add_argument(
        "--pool-file",
        type=Path,
        required=True,
        help="owner-only JSON array of exclusive network/address entries",
    )
    trade_withdraw_accounts.add_argument(
        "--merchant",
        action="append",
        type=int,
        default=[],
        metavar="MERCHANT_ID",
        help="limit processing to one merchant; repeat as needed",
    )
    trade_withdraw_accounts.add_argument(
        "--config-version",
        default="phase1-withdraw-account-v1",
        help="non-secret audit configuration version",
    )
    trade_withdraw_accounts.add_argument(
        "--apply",
        action="store_true",
        help="create missing A6 accounts; without this flag the command is a dry-run",
    )

    trade_seller_resources = subcommands.add_parser(
        "trade-ensure-seller-resources",
        help="plan or reconcile an Enodot Key, A6 channel, and Listing for every selected Seller",
    )
    _add_trade_database_arguments(trade_seller_resources)
    seller_selection = trade_seller_resources.add_mutually_exclusive_group(required=True)
    seller_selection.add_argument(
        "--merchant",
        action="append",
        type=int,
        default=[],
        metavar="MERCHANT_ID",
        help="select one Seller by database merchant ID; repeat as needed",
    )
    seller_selection.add_argument(
        "--all-sellers",
        action="store_true",
        help="select every Seller, including accounts that are not ready yet",
    )
    trade_seller_resources.add_argument("--group-id", type=int, required=True)
    trade_seller_resources.add_argument("--model", default="gpt-5.6-sol")
    trade_seller_resources.add_argument("--delta", required=True)
    trade_seller_resources.add_argument("--cost-multiplier", required=True, metavar="K")
    trade_seller_resources.add_argument("--margin", default="0.20")
    trade_seller_resources.add_argument("--conversion-factor", default="0.833333333333")
    trade_seller_resources.add_argument("--price-headroom", default="0")
    trade_seller_resources.add_argument("--maximum-price-multiplier", default="1")
    trade_seller_resources.add_argument(
        "--price-multiplier",
        default=None,
        help="explicit A6 price multiplier P; defaults to P_min times (1 + price-headroom)",
    )
    trade_seller_resources.add_argument(
        "--existing-channel-id",
        type=int,
        default=None,
        help="adopt this Seller's existing A6 channel; requires exactly one --merchant",
    )
    trade_seller_resources.add_argument(
        "--input-price-micros", type=int, default=GPT_5_6_SOL_OFFICIAL_PRICES[0]
    )
    trade_seller_resources.add_argument(
        "--output-price-micros", type=int, default=GPT_5_6_SOL_OFFICIAL_PRICES[1]
    )
    trade_seller_resources.add_argument(
        "--cache-read-price-micros", type=int, default=GPT_5_6_SOL_OFFICIAL_PRICES[2]
    )
    trade_seller_resources.add_argument(
        "--cache-write-price-micros", type=int, default=GPT_5_6_SOL_OFFICIAL_PRICES[3]
    )
    trade_seller_resources.add_argument(
        "--seller-base-url",
        default="https://ai.enodot.shop/v1",
        help="OpenAI-compatible Enodot base URL placed in the managed A6 channel",
    )
    trade_seller_resources.add_argument("--contact-info", default="a6-trader")
    trade_seller_resources.add_argument(
        "--listing-description",
        required=True,
        help="human-readable model source description for the selected Seller Listing",
    )
    trade_seller_resources.add_argument(
        "--proxy-map-file",
        type=Path,
        required=True,
        help="owner-only JSON mapping selected A6 user IDs to unique stable proxy URLs",
    )
    trade_seller_resources.add_argument(
        "--seller-key-dir",
        type=Path,
        default=DEFAULT_SELLER_KEY_DIR,
        help=f"owner-only managed Key directory (default: {DEFAULT_SELLER_KEY_DIR})",
    )
    trade_seller_resources.add_argument("--enodot-admin-url", default="https://ai.enodot.shop")
    seller_admin = trade_seller_resources.add_mutually_exclusive_group()
    seller_admin.add_argument("--enodot-admin-key-env", default="AI_ENODOT_SHOP_ADMIN_KEY")
    seller_admin.add_argument("--enodot-admin-key-file", default="")
    seller_admin.add_argument("--enodot-admin-token-file", type=Path)
    trade_seller_resources.add_argument("--config-version", default="phase1-seller-resources-v1")
    trade_seller_resources.add_argument(
        "--listing-activation-stagger-seconds",
        type=float,
        default=0,
        help="delay after each Listing activation/deactivation; price-only updates are immediate",
    )
    trade_seller_resources.add_argument("--max-workers", type=int, default=1)
    trade_seller_resources.add_argument(
        "--remote-request-timeout-seconds",
        type=float,
        default=30,
    )
    trade_seller_resources.add_argument(
        "--round-time-budget-seconds",
        type=float,
        default=300,
    )
    trade_seller_resources.add_argument(
        "--resolve-channel-create-unknown",
        action="store_true",
        help="resolve one Seller channel create_unknown after fresh absence readback",
    )
    trade_seller_resources.add_argument(
        "--apply",
        action="store_true",
        help="reconcile external resources; without this flag the command is a dry-run",
    )

    trade_managed_autoonboard = subcommands.add_parser(
        "trade-auto-onboard-seller-channels",
        help="adopt every new Seller channel and ensure its managed model Listing",
    )
    _add_trade_database_arguments(trade_managed_autoonboard)
    _add_trade_session_arguments(trade_managed_autoonboard)
    trade_managed_autoonboard.add_argument("--model", default="gpt-5.6-sol")
    trade_managed_autoonboard.add_argument(
        "--proxy-map-file",
        type=Path,
        required=True,
        help="Trader control-plane proxy mapping used for A6 panel sessions",
    )
    trade_managed_autoonboard.add_argument(
        "--inventory-max-age-seconds",
        type=int,
        default=900,
        help="ignore channel inventory older than this many seconds",
    )
    trade_managed_autoonboard.add_argument(
        "--target-limit",
        type=int,
        default=100,
        help="maximum channels reconciled in one pass",
    )
    trade_managed_autoonboard.add_argument(
        "--input-price-micros", type=int, default=GPT_5_6_SOL_OFFICIAL_PRICES[0]
    )
    trade_managed_autoonboard.add_argument(
        "--output-price-micros", type=int, default=GPT_5_6_SOL_OFFICIAL_PRICES[1]
    )
    trade_managed_autoonboard.add_argument(
        "--cache-read-price-micros", type=int, default=GPT_5_6_SOL_OFFICIAL_PRICES[2]
    )
    trade_managed_autoonboard.add_argument(
        "--cache-write-price-micros", type=int, default=GPT_5_6_SOL_OFFICIAL_PRICES[3]
    )
    trade_managed_autoonboard.add_argument(
        "--allow-configured-price-seed",
        action="store_true",
        help=(
            "bootstrap with the configured price tuple when no confirmed Seller "
            "price-band seed exists"
        ),
    )
    trade_managed_autoonboard.add_argument(
        "--config-version",
        default="phase5-managed-channel-autoonboard-v1",
    )
    trade_managed_autoonboard.add_argument("--compact-output", action="store_true")
    _add_systemd_partial_result_argument(trade_managed_autoonboard)
    trade_managed_autoonboard.add_argument(
        "--apply",
        action="store_true",
        help="adopt channels and create or activate Listings; otherwise only report the plan",
    )

    trade_seller_upstreams = subcommands.add_parser(
        "trade-reconcile-seller-upstreams",
        help=(
            "verify bound Seller channel URLs and independent Enodot Keys without touching Listings"
        ),
    )
    _add_trade_database_arguments(trade_seller_upstreams)
    upstream_selection = trade_seller_upstreams.add_mutually_exclusive_group(required=True)
    upstream_selection.add_argument(
        "--merchant",
        action="append",
        type=int,
        default=[],
        metavar="MERCHANT_ID",
        help="select one bound Seller by database merchant ID; repeat as needed",
    )
    upstream_selection.add_argument(
        "--all-bound-sellers",
        action="store_true",
        help="select every active Seller with an idempotently bound Enodot account and Key",
    )
    trade_seller_upstreams.add_argument("--model", default="gpt-5.6-sol")
    trade_seller_upstreams.add_argument(
        "--seller-base-url",
        default="https://ai.enodot.shop/v1",
    )
    trade_seller_upstreams.add_argument("--contact-info", default="a6-trader")
    trade_seller_upstreams.add_argument("--proxy-map-file", type=Path, required=True)
    trade_seller_upstreams.add_argument(
        "--seller-key-dir",
        type=Path,
        default=DEFAULT_SELLER_KEY_DIR,
    )
    trade_seller_upstreams.add_argument(
        "--config-version",
        default="phase3-seller-upstream-guard-v1",
    )
    trade_seller_upstreams.add_argument("--compact-output", action="store_true")
    trade_seller_upstreams.add_argument(
        "--apply",
        action="store_true",
        help="repair mismatches and verify by readback; otherwise only report the plan",
    )

    trade_seller_price_band = subcommands.add_parser(
        "trade-apply-seller-price-band",
        help="derive K from confirmed Pins and reconcile a dense-low Seller price band",
    )
    _add_trade_database_arguments(trade_seller_price_band)
    price_band_selection = trade_seller_price_band.add_mutually_exclusive_group(required=True)
    price_band_selection.add_argument(
        "--merchant",
        action="append",
        type=int,
        default=[],
        metavar="MERCHANT_ID",
        help="select one Seller by database merchant ID; repeat as needed",
    )
    price_band_selection.add_argument(
        "--eligible-sellers",
        action="store_true",
        help="select the Seller catalog and explicitly report accounts that are not ready",
    )
    trade_seller_price_band.add_argument(
        "--bound-sellers-only",
        action="store_true",
        help="with --eligible-sellers, reconcile only Sellers already bound to Enodot",
    )
    trade_seller_price_band.add_argument("--group-id", type=int, required=True)
    trade_seller_price_band.add_argument("--model", default="gpt-5.6-sol")
    trade_seller_price_band.add_argument("--margin", default="0.60")
    trade_seller_price_band.add_argument("--conversion-factor", default="0.833333333333")
    trade_seller_price_band.add_argument("--price-headroom", default="0")
    trade_seller_price_band.add_argument("--maximum-price-multiplier", default="1")
    trade_seller_price_band.add_argument("--maximum-final-multiplier", default="1")
    trade_seller_price_band.add_argument("--maximum-band-utilization", default="1")
    trade_seller_price_band.add_argument("--quote-max-age-seconds", type=int, default=300)
    trade_seller_price_band.add_argument("--usage-window-seconds", type=int, default=3600)
    trade_seller_price_band.add_argument(
        "--no-market-floor",
        action="store_false",
        dest="market_floor_enabled",
        help="disable isolated-low-price protection",
    )
    trade_seller_price_band.add_argument("--market-floor-gap-ratio", default="0.10")
    trade_seller_price_band.add_argument("--market-floor-neighbor-ratio", default="0.03")
    trade_seller_price_band.add_argument("--market-floor-min-neighbors", type=int, default=2)
    trade_seller_price_band.add_argument("--market-floor-min-observers", type=int, default=2)
    trade_seller_price_band.add_argument("--market-floor-position", default="0.75")
    trade_seller_price_band.add_argument("--market-density-buckets", type=int, default=8)
    trade_seller_price_band.add_argument("--market-density-coverage-ratio", default="0.20")
    trade_seller_price_band.add_argument("--market-density-min-channels", type=int, default=2)
    trade_seller_price_band.add_argument("--seller-base-url", default="https://ai.enodot.shop/v1")
    trade_seller_price_band.add_argument("--contact-info", default="a6-trader")
    trade_seller_price_band.add_argument(
        "--proxy-map-file",
        type=Path,
        required=True,
        help="owner-only JSON mapping selected A6 user IDs to unique stable proxy URLs",
    )
    trade_seller_price_band.add_argument(
        "--seller-key-dir",
        type=Path,
        default=DEFAULT_SELLER_KEY_DIR,
        help=f"owner-only managed Key directory (default: {DEFAULT_SELLER_KEY_DIR})",
    )
    trade_seller_price_band.add_argument(
        "--channel-map-file",
        type=Path,
        help="owner-only JSON mapping merchant IDs to existing A6 channel IDs",
    )
    trade_seller_price_band.add_argument(
        "--listing-description-map-file",
        type=Path,
        required=True,
        help="owner-only JSON mapping merchant IDs to model source descriptions",
    )
    trade_seller_price_band.add_argument("--enodot-admin-url", default="https://ai.enodot.shop")
    price_band_admin = trade_seller_price_band.add_mutually_exclusive_group()
    price_band_admin.add_argument("--enodot-admin-key-env", default="AI_ENODOT_SHOP_ADMIN_KEY")
    price_band_admin.add_argument("--enodot-admin-key-file", default="")
    price_band_admin.add_argument("--enodot-admin-token-file", type=Path)
    trade_seller_price_band.add_argument("--config-version", default="phase3-seller-price-band-v2")
    trade_seller_price_band.add_argument(
        "--rollout-batch-limit",
        type=int,
        default=1,
        help="maximum explicit --merchant count for apply (default: 1)",
    )
    trade_seller_price_band.add_argument(
        "--rollout-confirmation",
        default="",
        help="required second confirmation for an explicit multi-merchant apply",
    )
    trade_seller_price_band.add_argument(
        "--listing-activation-stagger-seconds",
        type=float,
        default=0,
        help="delay after each Listing activation/deactivation; price-only updates are immediate",
    )
    trade_seller_price_band.add_argument("--max-workers", type=int, default=1)
    trade_seller_price_band.add_argument(
        "--remote-request-timeout-seconds",
        type=float,
        default=30,
    )
    trade_seller_price_band.add_argument(
        "--round-time-budget-seconds",
        type=float,
        default=300,
    )
    trade_seller_price_band.add_argument(
        "--stale-claim-seconds",
        type=int,
        default=3_600,
        help="reclaim interrupted Seller price actions after this many seconds",
    )
    trade_seller_price_band.add_argument("--compact-output", action="store_true")
    _add_systemd_partial_result_argument(trade_seller_price_band)
    trade_seller_price_band.add_argument(
        "--apply",
        action="store_true",
        help="reconcile delta and Listings; without this flag the command is a dry-run",
    )

    trade_seller_retirement = subcommands.add_parser(
        "trade-retire-sellers-once",
        help="transfer a threshold-qualified Seller balance and retire it to Provider-only",
    )
    _add_trade_database_arguments(trade_seller_retirement)
    retirement_targets = trade_seller_retirement.add_mutually_exclusive_group(required=True)
    retirement_targets.add_argument(
        "--merchant",
        action="append",
        type=int,
        metavar="MERCHANT_ID",
        help="monitor one Seller retirement target; repeat as needed",
    )
    retirement_targets.add_argument(
        "--all-planned",
        action="store_true",
        help="advance any non-completed retirement plan, including dashboard requests",
    )
    trade_seller_retirement.add_argument("--proxy-map-file", type=Path, required=True)
    trade_seller_retirement.add_argument("--group-id", type=int, default=26)
    trade_seller_retirement.add_argument("--model", default="gpt-5.6-sol")
    trade_seller_retirement.add_argument("--min-observers", type=int, default=2)
    trade_seller_retirement.add_argument("--snapshot-max-age-seconds", type=int, default=600)
    trade_seller_retirement.add_argument(
        "--retire-in-place-max-success-rate",
        type=int,
        default=0,
        help=(
            "basis points; retire a Listing without waiting for the transfer threshold "
            "once A6 measures its success rate below this. 0 disables the policy"
        ),
    )
    trade_seller_retirement.add_argument(
        "--retire-in-place-min-samples",
        type=int,
        default=30,
        help="minimum A6 sample count before a low success rate counts as evidence",
    )
    trade_seller_retirement.add_argument("--enodot-admin-url", default="https://ai.enodot.shop")
    retirement_admin = trade_seller_retirement.add_mutually_exclusive_group()
    retirement_admin.add_argument("--enodot-admin-key-env", default="AI_ENODOT_SHOP_ADMIN_KEY")
    retirement_admin.add_argument("--enodot-admin-key-file", default="")
    retirement_admin.add_argument("--enodot-admin-token-file", type=Path)
    trade_seller_retirement.add_argument("--compact-output", action="store_true")
    _add_systemd_partial_result_argument(trade_seller_retirement)
    trade_seller_retirement.add_argument(
        "--apply",
        action="store_true",
        help="execute the retirement state machine; required for this mutating command",
    )

    trade_provider_topup = subcommands.add_parser(
        "trade-topup-providers-once",
        help="raise Provider A6 user balance to the floor that keeps it Pin-selectable",
    )
    _add_trade_database_arguments(trade_provider_topup)
    trade_provider_topup.add_argument(
        "--merchant",
        action="append",
        type=int,
        required=True,
        metavar="MERCHANT_ID",
        help="top up one Provider merchant; repeat as needed",
    )
    trade_provider_topup.add_argument(
        "--proxy-map-file",
        type=Path,
        required=True,
        help="Trader control-plane session proxy map used to read A6 balances",
    )
    trade_provider_topup.add_argument(
        "--floor-units",
        type=Decimal,
        default=DEFAULT_FLOOR_UNITS,
        help=(
            "target user balance in quota_per_unit units; A6 excludes a Provider under "
            "1 unit, so the default of 2 keeps a unit of headroom"
        ),
    )
    trade_provider_topup.add_argument("--compact-output", action="store_true")
    trade_provider_topup.add_argument(
        "--strict-liquidity",
        action="store_true",
        help="fail closed below the $1 hard line and mark the account for replacement",
    )
    trade_provider_topup.add_argument(
        "--apply",
        action="store_true",
        help="execute the transfers; without it the command only reports the plan",
    )

    trade_channel_growth = subcommands.add_parser(
        "trade-reconcile-channel-growth-once",
        help="reconcile one managed Listing health recovery or callback growth action",
    )
    _add_trade_database_arguments(trade_channel_growth)
    _add_trade_session_arguments(trade_channel_growth)
    trade_channel_growth.add_argument("--proxy-map-file", type=Path, required=True)
    trade_channel_growth.add_argument(
        "--seller-key-dir",
        type=Path,
        default=DEFAULT_SELLER_KEY_DIR,
    )
    trade_channel_growth.add_argument("--model", default="gpt-5.6-sol")
    trade_channel_growth.add_argument("--signal-max-age-seconds", type=int, default=45)
    trade_channel_growth.add_argument("--recycle-min-delay-seconds", type=float, default=0.8)
    trade_channel_growth.add_argument("--recycle-max-delay-seconds", type=float, default=2.0)
    trade_channel_growth.add_argument("--callback-gateway-url", default="https://a6.a6api.com")
    trade_channel_growth.add_argument("--callback-timeout-seconds", type=float, default=30)
    trade_channel_growth.add_argument("--enodot-admin-url", default="https://ai.enodot.shop")
    growth_admin = trade_channel_growth.add_mutually_exclusive_group()
    growth_admin.add_argument("--enodot-admin-key-env", default="AI_ENODOT_SHOP_ADMIN_KEY")
    growth_admin.add_argument("--enodot-admin-key-file", default="")
    growth_admin.add_argument("--enodot-admin-token-file", type=Path)
    trade_channel_growth.add_argument("--config-version", default="phase4-channel-health-growth-v1")
    trade_channel_growth.add_argument("--compact-output", action="store_true")
    _add_systemd_partial_result_argument(trade_channel_growth)
    trade_channel_growth.add_argument(
        "--apply",
        action="store_true",
        help="execute at most one health recovery action; otherwise report the next plan",
    )

    subcommands.add_parser("validate-templates", help="validate the bundled 200 templates")
    return command


def _validate_templates() -> dict[str, Any]:
    templates = load_templates()
    fields = {
        "template_ids": [item.template_id for item in templates],
        "supplier_names": [item.supplier_name for item in templates],
        "supplier_descriptions": [item.supplier_description for item in templates],
        "channel_names": [item.channel_name for item in templates],
        "channel_descriptions": [item.channel_description for item in templates],
    }
    if any(len(values) != len(set(values)) for values in fields.values()):
        raise A6AutoError("template_catalog_duplicates")
    if any(not 1 <= len(item.channel_nickname) <= 10 for item in templates):
        raise A6AutoError("template_channel_name_invalid")
    if any(not is_source_only_listing_description(item.model_description) for item in templates):
        raise A6AutoError("template_model_description_invalid")
    return {"valid": True, "templates": len(templates), "unique_fields": list(fields)}


def run_command(args: argparse.Namespace) -> dict[str, Any]:
    if not args.session and not args.qq_file:
        raise A6AutoError("qq_file_required")
    addresses = load_qq_addresses(Path(args.qq_file)) if args.qq_file else ()
    templates = load_templates()
    if not args.live:
        return {
            "ready": True,
            "qq_addresses": len(addresses),
            "templates": len(templates),
            "resume_session": str(args.session.expanduser().resolve()) if args.session else None,
            "upstream_base_url": args.upstream_base_url.rstrip("/"),
            "mutations": False,
        }
    upstream_key = _secret_value(
        args.upstream_api_key,
        args.upstream_api_key_env,
        args.upstream_api_key_file,
    )
    if args.session:
        if args.email or args.template_id:
            raise A6AutoError("resume_session_conflicts_with_allocation")
        store = SessionStore(args.session)
        record = store.load()
        template = choose_template(record.template_id)
    else:
        record, store, template = allocate_session(
            Path(args.qq_file),
            _state_dir(args.state_dir),
            explicit_email=args.email,
            template_id=args.template_id,
        )
    client = A6ApiClient(record.origin, proxy_url=_proxy_from_file(args.proxy_file))
    result = Workflow(client, QQMailbox(), store).execute(
        record,
        template,
        upstream_base_url=args.upstream_base_url,
        upstream_api_key=upstream_key,
    )
    return result


def refresh_command(args: argparse.Namespace) -> dict[str, Any]:
    store = SessionStore(args.session)
    record = store.load()
    client = A6ApiClient(record.origin, proxy_url=_proxy_from_file(args.proxy_file))
    return Workflow(client, QQMailbox(), store).refresh_session(record)


def _default_batch_state(state_dir: Path) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
    return state_dir / "batches" / f"batch-{stamp}-{secrets.token_hex(3)}.json"


def _batch_proxy_binder(raw_path: str) -> Callable[[int, str], None] | None:
    if not raw_path:
        return None
    path = Path(raw_path).expanduser().resolve()
    lock_path = path.with_name(f".{path.name}.lock")

    def bind(user_id: int, proxy: str) -> None:
        with private_file_lock(lock_path):
            try:
                values = read_private_json(path) if path.exists() else {}
            except A6AutoError as exc:
                raise A6AutoError("batch_proxy_binding_map_unreadable") from exc
            if any(
                not str(key).isdigit() or not isinstance(value, str)
                for key, value in values.items()
            ):
                raise A6AutoError("batch_proxy_binding_map_invalid")
            key = str(user_id)
            current = values.get(key)
            if current not in (None, proxy):
                raise A6AutoError("batch_proxy_binding_user_conflict")
            if any(other_key != key and value == proxy for other_key, value in values.items()):
                raise A6AutoError("batch_proxy_binding_proxy_conflict")
            values[key] = proxy
            write_private_json(path, values)

    return bind


def _batch_reserved_proxy_fingerprints(raw_path: str) -> tuple[str, ...]:
    if not raw_path:
        return ()
    path = Path(raw_path).expanduser().resolve()
    try:
        values = read_private_json(path)
    except A6AutoError as exc:
        raise A6AutoError("batch_proxy_binding_map_unreadable") from exc
    if any(not str(key).isdigit() or not isinstance(value, str) for key, value in values.items()):
        raise A6AutoError("batch_proxy_binding_map_invalid")
    return tuple(
        sorted({hashlib.sha256(value.encode()).hexdigest()[:12] for value in values.values()})
    )


def batch_command(args: argparse.Namespace) -> dict[str, Any]:
    addresses = load_qq_addresses(Path(args.qq_file))
    subscription_urls: tuple[str, ...] = ()
    if args.proxy_subscription_file:
        subscription_urls = load_subscription_urls(Path(args.proxy_subscription_file))
        proxies = tuple(
            f"socks5://127.0.0.1:{args.sing_box_port_start + index}"
            for index in range(len(subscription_urls))
        )
        proxy_source = "sing-box-subscriptions"
    else:
        proxies = _proxy_pool(args.proxy_file)
        proxy_source = "proxy-file"
    min_seconds = args.min_interval_minutes * 60
    max_seconds = args.max_interval_minutes * 60
    distribution_window_seconds = float(getattr(args, "distribution_window_hours", 0) or 0) * 3600
    proxy_reservation_file = getattr(args, "proxy_reservation_file", None)
    upstream_reservation_file = getattr(args, "upstream_reservation_file", None)
    proxy_binding_map_file = getattr(args, "proxy_binding_map_file", "")
    proxy_binder = _batch_proxy_binder(proxy_binding_map_file)
    reserved_proxy_fingerprints = _batch_reserved_proxy_fingerprints(proxy_binding_map_file)
    template_workbook = getattr(args, "template_workbook", None)
    requested_template_catalog = getattr(args, "template_catalog", "json")
    template_catalog = "workbook" if template_workbook else requested_template_catalog
    operating_group = getattr(args, "operating_group", "standard")
    unique_upstream_base_urls = bool(getattr(args, "unique_upstream_base_urls", False))
    prepare_only = bool(getattr(args, "prepare_only", False))
    explicit_state_file = args.batch_state.expanduser().resolve() if args.batch_state else None
    resume_state_exists = explicit_state_file is not None and explicit_state_file.is_file()
    static_mode = bool(args.upstream_base_url)
    static_key_sources = (
        args.upstream_api_key,
        args.upstream_api_key_env,
        args.upstream_api_key_file,
    )
    if static_mode:
        if args.upstream_domain_file:
            raise A6AutoError("batch_upstream_mode_conflict")
        domains: tuple[str, ...] = ()
        group_name = ""
    else:
        if any(static_key_sources):
            raise A6AutoError("batch_upstream_mode_conflict")
        domains = load_upstream_domains(
            Path(args.upstream_domain_file) if args.upstream_domain_file else None
        )
        group_name = args.enodot_group.strip()
    preview = BatchConfig(
        qq_file=Path(args.qq_file),
        state_dir=_state_dir(args.state_dir),
        state_file=explicit_state_file or Path("batch-preview.json"),
        proxy_urls=proxies,
        upstream_base_url=args.upstream_base_url,
        upstream_api_key="",
        target_count=args.count,
        min_interval_seconds=min_seconds,
        max_interval_seconds=max_seconds,
        per_account_retries=args.per_account_retries,
        upstream_base_urls=domains,
        upstream_group_name=group_name,
        reserved_proxy_fingerprints=reserved_proxy_fingerprints,
        template_catalog=template_catalog,
        template_workbook=template_workbook,
        operating_group=operating_group,
        unique_upstream_base_urls=unique_upstream_base_urls,
        distribution_window_seconds=distribution_window_seconds,
        proxy_reservation_file=proxy_reservation_file,
        upstream_reservation_file=upstream_reservation_file,
    )
    preview.validate(
        required_proxy_count=(0 if resume_state_exists and (args.live or prepare_only) else None)
    )
    if len(addresses) < args.count:
        raise A6AutoError("qq_pool_smaller_than_batch")
    gaps = max(args.count - 1, 0)
    if not args.live and not prepare_only:
        result = {
            "ready": True,
            "target": args.count,
            "qq_addresses": len(addresses),
            "proxies": len(proxies),
            "proxy_source": proxy_source,
            "interval_minutes": [args.min_interval_minutes, args.max_interval_minutes],
            "mutations": False,
        }
        if distribution_window_seconds:
            slot_minutes = distribution_window_seconds / args.count / 60
            margin_minutes = args.min_interval_minutes / 2
            result.update(
                {
                    "distribution_window_hours": distribution_window_seconds / 3600,
                    "slot_minutes": slot_minutes,
                    "random_offset_minutes": [
                        margin_minutes,
                        slot_minutes - margin_minutes,
                    ],
                }
            )
        else:
            result.update(
                {
                    "minimum_elapsed_hours": gaps * args.min_interval_minutes / 60,
                    "maximum_elapsed_hours": gaps * args.max_interval_minutes / 60,
                }
            )
        if template_catalog != "json" or operating_group != "standard" or unique_upstream_base_urls:
            result.update(
                {
                    "template_catalog": template_catalog,
                    "operating_group": operating_group,
                    "unique_upstream_base_urls": unique_upstream_base_urls,
                }
            )
        if template_catalog == "workbook":
            result["template_catalog_sha256"] = load_workbook_template_catalog(
                template_workbook
            ).sha256
        if static_mode:
            result["upstream_mode"] = "static"
            result["upstream_base_url"] = args.upstream_base_url.rstrip("/")
        else:
            result["upstream_mode"] = "enodot-group"
            result["upstream_domains"] = len(domains)
            result["upstream_group"] = group_name
            result["enodot_admin_url"] = args.enodot_admin_url.rstrip("/")
            result["admin_key_environment"] = args.enodot_admin_key_env
        return result

    state_file = (
        explicit_state_file
        or _default_batch_state(_state_dir(args.state_dir)).expanduser().resolve()
    )
    if prepare_only:
        config = BatchConfig(
            qq_file=Path(args.qq_file),
            state_dir=_state_dir(args.state_dir),
            state_file=state_file,
            proxy_urls=proxies,
            upstream_base_url=args.upstream_base_url,
            upstream_api_key="",
            target_count=args.count,
            min_interval_seconds=min_seconds,
            max_interval_seconds=max_seconds,
            per_account_retries=args.per_account_retries,
            upstream_base_urls=domains,
            upstream_group_name=group_name,
            reserved_proxy_fingerprints=reserved_proxy_fingerprints,
            template_catalog=template_catalog,
            template_workbook=template_workbook,
            operating_group=operating_group,
            unique_upstream_base_urls=unique_upstream_base_urls,
            distribution_window_seconds=distribution_window_seconds,
            proxy_reservation_file=proxy_reservation_file,
            upstream_reservation_file=upstream_reservation_file,
        )
        return SlowBatchRunner(config).prepare()

    upstream_selector = None
    if static_mode:
        upstream_key = _secret_value(*static_key_sources)
    else:
        if args.enodot_admin_token_file:
            admin_key = ""
            token_file = Path(args.enodot_admin_token_file)
        else:
            admin_key = _secret_value(
                "",
                "" if args.enodot_admin_key_file else args.enodot_admin_key_env,
                args.enodot_admin_key_file,
            )
            token_file = None
        provider = EnodotGroupKeyProvider(
            args.enodot_admin_url,
            admin_key,
            group_name,
            domains,
            token_file=token_file,
        )
        upstream_selector = provider.select
        upstream_key = ""

    def emit(event: dict[str, Any]) -> None:
        print(json.dumps(event, ensure_ascii=False), flush=True)

    def execute(proxy_urls: tuple[str, ...]) -> dict[str, Any]:
        config = BatchConfig(
            qq_file=Path(args.qq_file),
            state_dir=_state_dir(args.state_dir),
            state_file=state_file,
            proxy_urls=proxy_urls,
            upstream_base_url=args.upstream_base_url,
            upstream_api_key=upstream_key,
            target_count=args.count,
            min_interval_seconds=min_seconds,
            max_interval_seconds=max_seconds,
            per_account_retries=args.per_account_retries,
            upstream_base_urls=domains,
            upstream_group_name=group_name,
            reserved_proxy_fingerprints=reserved_proxy_fingerprints,
            template_catalog=template_catalog,
            template_workbook=template_workbook,
            operating_group=operating_group,
            unique_upstream_base_urls=unique_upstream_base_urls,
            distribution_window_seconds=distribution_window_seconds,
            proxy_reservation_file=proxy_reservation_file,
            upstream_reservation_file=upstream_reservation_file,
        )
        emit(
            {
                "event": "batch_started",
                "batch_state": str(state_file),
                "target": args.count,
                "proxy_source": proxy_source,
                "proxies": len(proxy_urls),
                "template_catalog": template_catalog,
                "operating_group": operating_group,
                "unique_upstream_base_urls": unique_upstream_base_urls,
                "distribution_window_hours": distribution_window_seconds / 3600,
            }
        )
        return SlowBatchRunner(
            config,
            upstream_selector=upstream_selector,
            proxy_binder=proxy_binder,
            event=emit,
        ).run()

    if subscription_urls:
        with SingBoxPool(
            subscription_urls,
            _state_dir(args.state_dir) / "sing-box",
            executable=args.sing_box_executable,
            port_start=args.sing_box_port_start,
            verify_tls=not args.proxy_subscription_insecure,
        ) as pool:
            return execute(pool.proxy_urls)
    return execute(proxies)


def trade_migrate_command(args: argparse.Namespace) -> dict[str, Any]:
    dsn = _database_dsn(args)
    connection = _trade_connection(dsn)
    try:
        applied = apply_migrations(connection, args.migration_dir)
    except Exception as exc:
        raise A6AutoError("trade_migration_failed") from exc
    finally:
        _close_connection(connection)
    return {
        "success": True,
        "applied_count": len(applied),
        "applied_versions": list(applied),
    }


def trade_import_sessions_command(args: argparse.Namespace) -> dict[str, Any]:
    paths = _trade_session_paths(args.sessions_dir)
    try:
        proxy_by_user_id = _proxy_map(args.proxy_map_file)
        proxy_metadata = _session_proxy_metadata(paths, proxy_by_user_id)
        metadata = load_session_metadata(
            paths,
            proxy_metadata_by_ref=proxy_metadata,
        )
    except A6AutoError:
        raise
    except Exception as exc:
        raise A6AutoError("trade_session_metadata_invalid") from exc
    dsn = _database_dsn(args)
    connection = _trade_connection(dsn)
    try:
        imported = upsert_merchant_metadata(connection, metadata)
    except Exception as exc:
        raise A6AutoError("trade_session_import_failed") from exc
    finally:
        _close_connection(connection)
    statuses = Counter(item.status for item in metadata)
    return {
        "success": True,
        "imported": imported,
        "tombstoned": statuses.get("abandoned", 0),
        "new_merchant_roles": ["seller"],
        "existing_roles_preserved": True,
        "statuses": dict(sorted(statuses.items())),
    }


def trade_refresh_sessions_once_command(args: argparse.Namespace) -> dict[str, Any]:
    paths = _trade_session_paths(args.sessions_dir)
    proxy_by_user_id = _proxy_map(args.proxy_map_file)
    if args.client_timeout_seconds <= 0:
        raise A6AutoError("session_refresh_timeout_invalid")
    if isinstance(args.max_workers, bool) or not 1 <= args.max_workers <= 16:
        raise A6AutoError("session_refresh_max_workers_invalid")
    dsn = _database_dsn(args)
    connection = _trade_connection(dsn)
    recovery_store = PostgresMerchantRecoveryStore(connection)

    def client_factory(record: Any, proxy: str) -> A6ApiClient:
        return A6ApiClient(
            record.origin,
            proxy_url=proxy,
            timeout=args.client_timeout_seconds,
        )

    def metadata_sink(path: Path, record: Any, proxy: str | None) -> None:
        proxy_metadata = None
        if proxy is not None:
            by_ref = _session_proxy_metadata(
                (path,),
                {int(record.user_id): proxy},
            )
            proxy_metadata = by_ref.get(str(path.expanduser().resolve()))
        metadata = metadata_from_session(path, proxy_metadata=proxy_metadata)
        upsert_merchant_metadata(connection, (metadata,))
        recovery_store.resolve_session_refresh_failures(str(path.expanduser().resolve()))

    def failure_sink(path: Path, error_class: str) -> None:
        recovery_store.record_session_refresh_failure(
            str(path.expanduser().resolve()),
            error_class,
        )

    service = TradeSessionRefreshService(
        client_factory=client_factory,
        metadata_sink=metadata_sink,
        failure_sink=failure_sink,
        mailbox=QQMailbox(),
        max_workers=args.max_workers,
    )
    try:
        return service.refresh_paths(paths, proxy_by_user_id)
    finally:
        _close_connection(connection)


def trade_collect_once_command(args: argparse.Namespace) -> dict[str, Any]:
    if isinstance(args.max_workers, bool) or not 1 <= args.max_workers <= 16:
        raise A6AutoError("collector_max_workers_invalid")
    paths = _trade_session_paths(args.sessions_dir)
    proxy_by_user_id = _proxy_map(args.proxy_map_file)
    eligible = _validate_collector_routing(
        paths,
        proxy_by_user_id,
        allow_direct=args.allow_direct,
    )
    dsn = _database_dsn(args)
    watermarks = _collector_watermarks(dsn)
    repository = PostgresTradeRepository.from_dsn(dsn)

    def client_factory(record: Any) -> A6ApiClient:
        user_id = int(record.user_id or 0)
        if proxy_by_user_id and user_id not in proxy_by_user_id:
            raise A6AutoError("collector_proxy_mapping_missing")
        return A6ApiClient(
            record.origin,
            proxy_url=proxy_by_user_id.get(user_id, ""),
            timeout=10,
        )

    def session_refresher(store: SessionStore, record: Any, client: A6ApiClient) -> None:
        Workflow(client, QQMailbox(), store).ensure_authenticated(record)

    service = A6CollectionService(
        client_factory=client_factory,
        watermark_loader=lambda user_id: watermarks.get(user_id, CollectorWatermarks()),
        sink=repository,
        max_workers=args.max_workers,
        session_refresher=session_refresher,
    )
    try:
        completed, failures = service.collect_paths(paths)
    except Exception as exc:
        raise A6AutoError("trade_collection_failed") from exc

    errors = Counter(failure.error_class for failure in failures)
    # Runs after every merchant in this pass has been persisted: an earning collected
    # from one merchant is often funded by a provider request collected from another,
    # so classification can only be settled once the whole pass is in.
    counterparty = repository.classify_earning_counterparties(
        settle_seconds=args.counterparty_settle_seconds,
    )
    return {
        "success": not failures,
        "sessions_found": len(paths),
        "eligible_sessions": eligible,
        "collected": completed,
        "failed": len(failures),
        "skipped": len(paths) - completed - len(failures),
        "errors": dict(sorted(errors.items())),
        "proxy_mode": "fixed-map" if proxy_by_user_id else "direct",
        "proxy_bindings": len(proxy_by_user_id),
        "earning_counterparty": counterparty,
        # own_pin means a provider Key consumed one of our own channels, which the Pin
        # candidate exclusion is supposed to make impossible. Non-zero is an incident.
        "own_pin_leak_detected": counterparty["own_pin"] > 0,
        "upstream_mutations": False,
    }


def trade_dashboard_command(args: argparse.Namespace) -> dict[str, Any]:
    host = str(args.host).strip()
    if not host or not 1 <= args.port <= 65535:
        raise A6AutoError("trade_dashboard_bind_invalid")
    dsn = _database_dsn(args)
    repository = PostgresTradeRepository.from_dsn(dsn)
    internal_token_env = str(args.internal_api_token_env or "").strip()
    if not internal_token_env:
        raise A6AutoError("trade_dashboard_internal_token_source_invalid")
    internal_api_token = str(os.environ.get(internal_token_env) or "").strip()
    fast_block_projector = None
    if args.fast_block_redis_project:
        redis_url_env = str(args.fast_block_redis_url_env or "").strip()
        redis_url = str(os.environ.get(redis_url_env) or "").strip()
        if not redis_url_env or not redis_url:
            raise A6AutoError("provider_fast_block_redis_url_missing")
        try:
            import redis
        except ImportError:
            raise A6AutoError("provider_fast_block_redis_dependency_missing") from None
        fast_block_projector = RedisProviderFastBlockProjector(
            redis.Redis.from_url(
                redis_url,
                decode_responses=True,
                socket_connect_timeout=2,
                socket_timeout=2,
                health_check_interval=30,
            )
        )
    publication_control = PostgresPublicationControl(
        lambda: _trade_connection(dsn),
        trigger=AtomicPublicationTrigger(args.publication_trigger_file),
        fast_block_projector=fast_block_projector,
        provider_channel_failure_threshold=args.provider_channel_failure_threshold,
    )
    fast_block_startup_replay = None
    if fast_block_projector is not None:
        replayed = publication_control.project_pending_fast_blocks(limit=500)
        rebuilt = publication_control.rebuild_fast_block_cache(limit=500)
        fast_block_startup_replay = {
            "outbox": {
                name: replayed[name] for name in ("processed", "projected", "superseded", "failed")
            },
            "cache_rebuild": {
                name: rebuilt[name]
                for name in ("processed", "projected", "superseded", "failed", "changed")
            },
        }
    management_values = (args.proxy_map_file, args.proxy_pool_file)
    if any(management_values) and not all(management_values):
        raise A6AutoError("trade_dashboard_management_config_incomplete")
    manager = (
        DashboardManagementService(
            lambda: _trade_connection(dsn),
            sessions_dir=args.sessions_dir,
            proxy_map_file=args.proxy_map_file,
            proxy_pool_file=args.proxy_pool_file,
            batch_state_file=args.batch_state_file,
        )
        if all(management_values)
        else None
    )
    print(
        json.dumps(
            {
                "event": "trade_dashboard_started",
                "url": f"http://{host}:{args.port}",
                "read_only": manager is None,
                "management_enabled": manager is not None,
                "internal_control_enabled": bool(internal_api_token),
                "fast_block_projection_enabled": fast_block_projector is not None,
                "fast_block_startup_replay": fast_block_startup_replay,
            },
            ensure_ascii=True,
        ),
        flush=True,
    )
    try:
        serve_options = {
            "host": host,
            "port": args.port,
            "sessions_dir": args.sessions_dir,
            "publication_control": publication_control,
            "internal_api_token": internal_api_token,
        }
        if manager is not None:
            serve_options["manager"] = manager
        serve_dashboard(repository, **serve_options)
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        raise A6AutoError("trade_dashboard_failed") from exc
    return {"success": True, "stopped": True}


def trade_pin_providers_once_command(args: argparse.Namespace) -> dict[str, Any]:
    dsn = _database_dsn(args)
    proxy_by_user_id = _proxy_map(args.proxy_map_file)
    if args.enodot_admin_token_file is not None:
        admin_key = ""
        token_file = args.enodot_admin_token_file
    else:
        key_file = str(args.enodot_admin_key_file or "")
        admin_key = _secret_value(
            "",
            "" if key_file else str(args.enodot_admin_key_env or ""),
            key_file,
        )
        token_file = None
    journal_file = args.pin_failure_journal_file
    cursor_file = args.pin_failure_cursor_file
    if (journal_file is None) != (cursor_file is None):
        raise A6AutoError("pin_failure_journal_cursor_pair_required")
    journal = (
        PinFailureJournal(journal_file, cursor_file)
        if journal_file is not None and cursor_file is not None
        else None
    )
    failure_batch = (
        journal.read(
            model=args.model,
            max_age_seconds=args.pin_failure_max_age_seconds,
        )
        if journal is not None
        else None
    )
    runner = ProviderPinRunner(
        PostgresPinAuditStore(lambda: _trade_connection(dsn)),
        sessions_dir=args.sessions_dir,
        proxy_by_user_id=proxy_by_user_id,
        enodot_admin=EnodotAdminClient(
            args.enodot_admin_url,
            admin_key,
            token_file=token_file,
            timeout=args.enodot_admin_timeout_seconds,
        ),
        client_timeout=args.client_timeout_seconds,
    )
    try:
        result = runner.run(
            model=args.model,
            min_observers=args.min_observers,
            top_k=args.top_k,
            page_size=args.page_size,
            observer_window_seconds=args.observer_window_seconds,
            observer_round_size=(None if args.all_observers else args.observer_round_size),
            observer_snapshot_max_age_seconds=args.observer_snapshot_max_age_seconds,
            config_version=args.config_version,
            pin_probe_mode=args.pin_probe_mode,
            probe_budget=args.probe_budget,
            probe_failure_ttl_seconds=args.probe_failure_ttl_seconds,
            probe_health_ttl_seconds=args.probe_health_ttl_seconds,
            probe_max_output_tokens=args.probe_max_output_tokens,
            probe_timeout_seconds=args.probe_timeout_seconds,
            probe_round_time_budget_seconds=args.probe_round_time_budget_seconds,
            pin_price_weight=args.pin_price_weight,
            pin_success_weight=args.pin_success_weight,
            pin_latency_weight=args.pin_latency_weight,
            pin_price_span_ratio=args.pin_price_span_ratio,
            pin_minimum_success_rate=args.pin_min_success_rate,
            pin_maximum_latency_ms=args.pin_max_latency_ms,
            pin_maximum_multiplier=args.pin_max_multiplier,
            pin_maximum_relative_cost_ratio=args.pin_max_relative_cost_ratio,
            pin_failure_window_seconds=args.pin_failure_window_seconds,
            pin_channel_failure_threshold=args.pin_channel_failure_threshold,
            provider_initial_balance_ratio=args.provider_initial_balance_ratio,
            minimum_schedulable_capacity=args.minimum_schedulable_capacity,
            pin_minimum_hold_seconds=args.pin_min_hold_seconds,
            pin_minimum_switch_gain=args.pin_min_switch_gain,
            emergency_boost_minimum_requests=args.emergency_boost_minimum_requests,
            emergency_boost_maximum_requests=args.emergency_boost_maximum_requests,
            emergency_boost_timeout_seconds=args.emergency_boost_timeout_seconds,
            emergency_boost_cooldown_seconds=args.emergency_boost_cooldown_seconds,
            round_time_budget_seconds=args.round_time_budget_seconds,
            failed_enodot_account_events=(
                failure_batch.account_events if failure_batch is not None else None
            ),
            observe_only=args.observe_only,
            apply=args.apply,
        )
        if journal is not None and failure_batch is not None:
            acknowledged = not failure_batch.account_ids or bool(
                result.get("failure_journal_consumed")
            )
            if acknowledged:
                journal.acknowledge(failure_batch)
            result["pin_failure_journal"] = failure_batch.public(
                acknowledged=acknowledged,
            )
        return result
    except A6AutoError:
        raise
    except Exception as exc:
        raise A6AutoError("trade_pin_providers_once_failed") from exc


def trade_assign_roles_command(args: argparse.Namespace) -> dict[str, Any]:
    try:
        assignments = tuple(
            RoleAssignment(merchant_id=merchant_id, role=role)
            for role, merchant_ids in (
                ("provider", args.provider),
                ("seller", args.seller),
                ("observer", args.observer),
            )
            for merchant_id in merchant_ids
        )
    except RoleAssignmentError as exc:
        raise A6AutoError(str(exc)) from exc
    dsn = _database_dsn(args)
    connection = _trade_connection(dsn)
    try:
        result = assign_merchant_roles(
            connection,
            assignments,
            apply=args.apply,
            config_version=args.config_version,
        )
    except RoleAssignmentError as exc:
        raise A6AutoError(str(exc)) from exc
    except Exception as exc:
        raise A6AutoError("trade_role_assignment_failed") from exc
    finally:
        _close_connection(connection)
    summary = result.summary()
    assignments_by_merchant: dict[int, set[str]] = {}
    for item in result.decisions:
        assignments_by_merchant.setdefault(item.merchant_id, set()).add(item.role)
    duty_counts = Counter(
        next(
            (role for role in ("provider", "observer") if role in roles),
            "none",
        )
        for roles in assignments_by_merchant.values()
    )
    summary["base_role_counts"] = {"seller": len(assignments_by_merchant)}
    summary["duty_counts"] = dict(sorted(duty_counts.items()))
    summary["assignment_counts"] = dict(
        sorted(Counter(item.role for item in result.decisions).items())
    )
    return summary


def trade_bind_withdraw_accounts_command(args: argparse.Namespace) -> dict[str, Any]:
    try:
        pool = load_withdraw_pool(args.pool_file)
        paths = _trade_session_paths(args.sessions_dir)
        session_paths_by_user_id: dict[int, Path] = {}
        for path in paths:
            record = SessionStore(path).load()
            if record.user_id is None:
                continue
            user_id = int(record.user_id)
            if user_id in session_paths_by_user_id:
                raise WithdrawAccountError("withdraw_session_user_duplicate")
            session_paths_by_user_id[user_id] = path
        proxy_by_user_id = _proxy_map(args.proxy_map_file)
        dsn = _database_dsn(args)
        store = PostgresWithdrawAccountStore(lambda: _trade_connection(dsn))

        def client_factory(merchant: Any, _record: Any) -> A6ApiClient:
            return authenticated_withdraw_client(
                merchant,
                session_paths_by_user_id=session_paths_by_user_id,
                proxy_by_user_id=proxy_by_user_id,
            )

        result = WithdrawAccountService(
            store,
            pool,
            client_factory,
            session_paths_by_user_id=session_paths_by_user_id,
            config_version=args.config_version,
            requested_by="trade-bind-withdraw-accounts",
        ).execute(args.merchant, apply=args.apply)
        return result.summary()
    except WithdrawAccountError as exc:
        raise A6AutoError(exc.code) from exc
    except Exception as exc:
        raise A6AutoError("trade_bind_withdraw_accounts_failed") from exc


def trade_auto_onboard_seller_channels_command(
    args: argparse.Namespace,
    *,
    reconciler: ManagedChannelAutoOnboarder | None = None,
) -> dict[str, Any]:
    try:
        request = ManagedAutoOnboardRequest(
            model=args.model,
            seed_prices=(
                args.input_price_micros,
                args.output_price_micros,
                args.cache_read_price_micros,
                args.cache_write_price_micros,
            ),
            inventory_max_age_seconds=args.inventory_max_age_seconds,
            target_limit=args.target_limit,
            require_confirmed_price_seed=not args.allow_configured_price_seed,
        )
        if reconciler is None:
            dsn = _database_dsn(args)
            proxy_by_user_id = _proxy_map(args.proxy_map_file)

            def connection_factory() -> Any:
                return _trade_connection(dsn)

            def client_factory(target: Any, record: Any) -> A6ApiClient:
                proxy_url = proxy_by_user_id.get(target.a6_user_id)
                if not proxy_url:
                    raise ManagedAutoOnboardError("managed_autoonboard_proxy_mapping_missing")
                return A6ApiClient(record.origin, proxy_url=proxy_url, timeout=15)

            reconciler = ManagedChannelAutoOnboarder(
                PostgresManagedAutoOnboardDiscovery(connection_factory),
                PostgresManagedListingStore(
                    connection_factory,
                    config_version=args.config_version,
                    requested_by="trade-auto-onboard-seller-channels",
                ),
                client_factory,
                sessions_dir=args.sessions_dir,
                price_recorder=PostgresManagedAutoOnboardPriceRecorder(
                    connection_factory,
                    config_version=args.config_version,
                ),
            )
        return reconciler.execute(request, apply=args.apply).summary()
    except (ManagedAutoOnboardError, ManagedListingError) as exc:
        raise A6AutoError(exc.code) from exc


def trade_ensure_seller_resources_command(
    args: argparse.Namespace,
    *,
    orchestrator: SellerResourceOrchestrator | None = None,
    seller_store: PostgresManagedSellerStore | None = None,
) -> dict[str, Any]:
    try:
        dsn = ""
        if orchestrator is None or (args.all_sellers and seller_store is None):
            dsn = _database_dsn(args)
        if seller_store is None and (orchestrator is None or args.all_sellers):
            seller_store = PostgresManagedSellerStore(lambda: _trade_connection(dsn))
        if args.all_sellers:
            assert seller_store is not None
            merchant_ids = seller_store.list_seller_ids()
            if not merchant_ids:
                raise SellerOrchestrationError("seller_merchants_empty")
        else:
            merchant_ids = tuple(args.merchant)
        if args.resolve_channel_create_unknown and len(merchant_ids) != 1:
            raise SellerOrchestrationError("seller_channel_recovery_single_merchant_required")
        if args.existing_channel_id is not None and len(merchant_ids) != 1:
            raise SellerOrchestrationError("seller_existing_channel_single_merchant_required")
        pricing = SellerPricing(
            cost_multiplier=args.cost_multiplier,
            delta=args.delta,
            margin=args.margin,
            conversion_factor=args.conversion_factor,
            headroom=args.price_headroom,
            maximum_price_multiplier=args.maximum_price_multiplier,
            price_multiplier=args.price_multiplier,
            input_price_micros=args.input_price_micros,
            output_price_micros=args.output_price_micros,
            cache_read_price_micros=args.cache_read_price_micros,
            cache_write_price_micros=args.cache_write_price_micros,
        )
        request = SellerResourceBatchRequest(
            merchant_ids=merchant_ids,
            group_id=args.group_id,
            model=args.model,
            pricing=pricing,
            listing_description=args.listing_description,
            seller_base_url=args.seller_base_url,
            channel_strategy=(
                "adopt_existing" if args.existing_channel_id is not None else "managed_create"
            ),
            existing_channel_id=args.existing_channel_id,
            config_version=args.config_version,
            requested_by="trade-ensure-seller-resources",
            contact_info=args.contact_info,
        )
        if orchestrator is None:
            proxy_by_user_id = _proxy_map(args.proxy_map_file)
            if args.enodot_admin_token_file is not None:
                admin_key = ""
                token_file = args.enodot_admin_token_file
            else:
                key_file = str(args.enodot_admin_key_file or "")
                admin_key = _secret_value(
                    "",
                    "" if key_file else str(args.enodot_admin_key_env or ""),
                    key_file,
                )
                token_file = None
            admin = EnodotAdminClient(
                args.enodot_admin_url,
                admin_key,
                token_file=token_file,
                timeout=args.remote_request_timeout_seconds,
            )
            assert seller_store is not None
            listing_store = PostgresManagedListingStore(
                lambda: _trade_connection(dsn),
                config_version=args.config_version,
                requested_by="trade-ensure-seller-resources",
            )
            secret_sink = PrivateFileManagedSellerSecretSink(args.seller_key_dir)

            def a6_client_factory(context: Any, _session: Any) -> A6ApiClient:
                return authenticated_seller_client(
                    context,
                    proxy_by_user_id=proxy_by_user_id,
                    client_factory=lambda record, proxy: A6ApiClient(
                        record.origin,
                        proxy_url=proxy,
                        timeout=args.remote_request_timeout_seconds,
                    ),
                )

            orchestrator = SellerResourceOrchestrator(
                ManagedSellerService(seller_store, admin, secret_sink),
                seller_store,
                secret_sink,
                listing_store,
                a6_client_factory,
                activation_stagger_seconds=args.listing_activation_stagger_seconds,
                max_workers=args.max_workers,
                round_time_budget_seconds=args.round_time_budget_seconds,
            )
        if args.resolve_channel_create_unknown:
            return orchestrator.resolve_channel_create_unknown(
                request,
                apply=args.apply,
            ).summary()
        return orchestrator.execute(request, apply=args.apply).summary()
    except (SellerOrchestrationError, ManagedSellerError, ManagedListingError) as exc:
        raise A6AutoError(exc.code) from exc
    except A6AutoError:
        raise
    except Exception as exc:
        raise A6AutoError("trade_ensure_seller_resources_failed") from exc


def trade_reconcile_seller_upstreams_command(
    args: argparse.Namespace,
    *,
    reconciler: SellerUpstreamReconciler | None = None,
    seller_store: PostgresManagedSellerStore | None = None,
) -> dict[str, Any]:
    try:
        dsn = ""
        if reconciler is None or (args.all_bound_sellers and seller_store is None):
            dsn = _database_dsn(args)
        if seller_store is None and (reconciler is None or args.all_bound_sellers):
            seller_store = PostgresManagedSellerStore(lambda: _trade_connection(dsn))
        if args.all_bound_sellers:
            assert seller_store is not None
            merchant_ids = seller_store.list_bound_seller_ids()
        else:
            merchant_ids = tuple(args.merchant)
        request = SellerUpstreamBatchRequest(
            merchant_ids=merchant_ids,
            model=args.model,
            seller_base_url=args.seller_base_url,
            contact_info=args.contact_info,
        )
        if reconciler is None:
            proxy_by_user_id = _proxy_map(args.proxy_map_file)
            assert seller_store is not None
            listing_store = PostgresManagedListingStore(
                lambda: _trade_connection(dsn),
                config_version=args.config_version,
                requested_by="trade-reconcile-seller-upstreams",
            )
            secret_sink = PrivateFileManagedSellerSecretSink(args.seller_key_dir)

            def a6_client_factory(context: Any, _session: Any) -> A6ApiClient:
                return authenticated_seller_client(
                    context,
                    proxy_by_user_id=proxy_by_user_id,
                )

            reconciler = SellerUpstreamReconciler(
                seller_store,
                secret_sink,
                listing_store,
                a6_client_factory,
            )
        return reconciler.execute(request, apply=args.apply).summary()
    except (SellerOrchestrationError, ManagedSellerError, ManagedListingError) as exc:
        raise A6AutoError(exc.code) from exc
    except A6AutoError:
        raise
    except Exception as exc:
        raise A6AutoError("trade_reconcile_seller_upstreams_failed") from exc


def trade_apply_seller_price_band_command(
    args: argparse.Namespace,
    *,
    orchestrator: SellerPriceBandOrchestrator | None = None,
    seller_store: PostgresManagedSellerStore | None = None,
) -> dict[str, Any]:
    try:
        dsn = ""
        if orchestrator is None or (args.eligible_sellers and seller_store is None):
            dsn = _database_dsn(args)
        if seller_store is None and (orchestrator is None or args.eligible_sellers):
            seller_store = PostgresManagedSellerStore(lambda: _trade_connection(dsn))
        runtime_margin = args.margin
        if orchestrator is None:
            setting = PostgresModelProfitSettingStore(
                lambda: _trade_connection(dsn)
            ).get(args.model)
            if setting is not None:
                runtime_margin = setting.margin
        if args.eligible_sellers:
            assert seller_store is not None
            merchant_ids = (
                seller_store.list_bound_seller_ids()
                if args.bound_sellers_only
                else seller_store.list_seller_ids()
            )
        else:
            if args.bound_sellers_only:
                raise SellerPriceBandError("seller_price_band_bound_selection_invalid")
            merchant_ids = tuple(args.merchant)
        listing_descriptions = _merchant_text_map(
            args.listing_description_map_file,
            error="seller_listing_description_map_invalid",
        )
        existing_channel_ids = _positive_id_map(
            getattr(args, "channel_map_file", None),
            error="seller_channel_map_invalid",
        )
        selected = set(merchant_ids)
        listing_descriptions = {
            merchant_id: description
            for merchant_id, description in listing_descriptions.items()
            if merchant_id in selected
        }
        existing_channel_ids = {
            merchant_id: channel_id
            for merchant_id, channel_id in existing_channel_ids.items()
            if merchant_id in selected
        }
        if set(listing_descriptions) != selected:
            if seller_store is None:
                raise SellerPriceBandError("seller_price_band_description_store_missing")
            listing_descriptions = _fill_template_listing_descriptions(
                seller_store,
                merchant_ids,
                listing_descriptions,
            )
        request = SellerPriceBandRequest(
            merchant_ids=merchant_ids,
            group_id=args.group_id,
            listing_descriptions=listing_descriptions,
            existing_channel_ids=existing_channel_ids,
            model=args.model,
            margin=runtime_margin,
            conversion_factor=args.conversion_factor,
            headroom=args.price_headroom,
            maximum_price_multiplier=args.maximum_price_multiplier,
            maximum_final_multiplier=args.maximum_final_multiplier,
            maximum_band_utilization=args.maximum_band_utilization,
            quote_max_age_seconds=args.quote_max_age_seconds,
            usage_window_seconds=args.usage_window_seconds,
            market_floor_enabled=args.market_floor_enabled,
            market_floor_gap_ratio=args.market_floor_gap_ratio,
            market_floor_neighbor_ratio=args.market_floor_neighbor_ratio,
            market_floor_min_neighbors=args.market_floor_min_neighbors,
            market_floor_min_observers=args.market_floor_min_observers,
            market_floor_position=args.market_floor_position,
            market_density_bucket_count=args.market_density_buckets,
            market_density_coverage_ratio=args.market_density_coverage_ratio,
            market_density_min_channels=args.market_density_min_channels,
            seller_base_url=args.seller_base_url,
            config_version=args.config_version,
            requested_by="trade-apply-seller-price-band",
            contact_info=args.contact_info,
        )
        if orchestrator is None:
            proxy_by_user_id = _proxy_map(args.proxy_map_file)
            if args.enodot_admin_token_file is not None:
                admin_key = ""
                token_file = args.enodot_admin_token_file
            else:
                key_file = str(args.enodot_admin_key_file or "")
                admin_key = _secret_value(
                    "",
                    "" if key_file else str(args.enodot_admin_key_env or ""),
                    key_file,
                )
                token_file = None
            admin = EnodotAdminClient(
                args.enodot_admin_url,
                admin_key,
                token_file=token_file,
                timeout=args.remote_request_timeout_seconds,
            )
            assert seller_store is not None
            listing_store = PostgresManagedListingStore(
                lambda: _trade_connection(dsn),
                config_version=args.config_version,
                requested_by="trade-apply-seller-price-band",
            )
            secret_sink = PrivateFileManagedSellerSecretSink(args.seller_key_dir)

            def a6_client_factory(context: Any, _session: Any) -> A6ApiClient:
                return cached_seller_client(
                    context,
                    _session,
                    proxy_by_user_id=proxy_by_user_id,
                    client_factory=lambda record, proxy: A6ApiClient(
                        record.origin,
                        proxy_url=proxy,
                        timeout=args.remote_request_timeout_seconds,
                    ),
                )

            resources = SellerResourceOrchestrator(
                ManagedSellerService(seller_store, admin, secret_sink),
                seller_store,
                secret_sink,
                listing_store,
                a6_client_factory,
                activation_stagger_seconds=args.listing_activation_stagger_seconds,
                max_workers=args.max_workers,
                round_time_budget_seconds=args.round_time_budget_seconds,
            )
            orchestrator = SellerPriceBandOrchestrator(
                PostgresSellerPriceBandStore(
                    lambda: _trade_connection(dsn),
                    stale_claim_seconds=args.stale_claim_seconds,
                ),
                resources,
            )
        return orchestrator.execute(
            request,
            apply=args.apply,
            rollout_batch_limit=getattr(args, "rollout_batch_limit", 1),
            rollout_confirmation=getattr(args, "rollout_confirmation", ""),
        ).summary()
    except SellerPriceBandError as exc:
        if exc.code in {
            "seller_price_band_action_pending",
            "seller_price_band_hysteresis_hold",
            "seller_price_band_minimum_interval_hold",
        }:
            return {
                "success": True,
                "mode": "held",
                "model": args.model,
                "merchant_count": len(merchant_ids),
                "skipped_merchant_count": len(merchant_ids),
                "reason": exc.code,
            }
        raise A6AutoError(exc.code) from exc
    except (
        SellerOrchestrationError,
        ManagedSellerError,
        ManagedListingError,
    ) as exc:
        raise A6AutoError(exc.code) from exc
    except A6AutoError:
        raise
    except Exception as exc:
        raise A6AutoError("trade_apply_seller_price_band_failed") from exc


def trade_retire_sellers_once_command(args: argparse.Namespace) -> dict[str, Any]:
    if not args.apply:
        raise A6AutoError("seller_retirement_apply_required")
    dsn = _database_dsn(args)
    proxy_by_user_id = _proxy_map(args.proxy_map_file)
    if args.enodot_admin_token_file is not None:
        admin_key = ""
        token_file = args.enodot_admin_token_file
    else:
        key_file = str(args.enodot_admin_key_file or "")
        admin_key = _secret_value(
            "",
            "" if key_file else str(args.enodot_admin_key_env or ""),
            key_file,
        )
        token_file = None

    def connection_factory() -> Any:
        return _trade_connection(dsn)

    merchant_ids = tuple(args.merchant or ())
    if getattr(args, "all_planned", False):
        connection = connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT merchant_id
                    FROM a6_trade.a6_seller_retirement_plans
                    WHERE state <> 'completed'
                    ORDER BY merchant_id
                    """
                )
                merchant_ids = tuple(
                    int(row["merchant_id"] if isinstance(row, Mapping) else row[0])
                    for row in cursor.fetchall()
                )
        finally:
            _close_connection(connection)
        if not merchant_ids:
            return {
                "success": True,
                "merchant_id": None,
                "action": "no_planned_retirements",
                "state": "idle",
                "amount_quota": 0,
                "enodot_account_id": None,
                "error_class": None,
            }

    service = SellerRetirementService(
        PostgresSellerRetirementStore(connection_factory),
        A6SellerRetirementRemote(
            connection_factory,
            proxy_by_user_id=proxy_by_user_id,
            enodot_admin=EnodotAdminClient(
                args.enodot_admin_url,
                admin_key,
                token_file=token_file,
            ),
            group_id=args.group_id,
            model=args.model,
            concurrency=100,
        ),
    )
    try:
        return service.run_once(
            merchant_ids,
            min_observers=args.min_observers,
            snapshot_max_age_seconds=args.snapshot_max_age_seconds,
            retire_in_place_maximum_success_rate=args.retire_in_place_max_success_rate,
            retire_in_place_minimum_samples=args.retire_in_place_min_samples,
        ).summary()
    except SellerRetirementError as exc:
        raise A6AutoError(str(exc)) from exc
    except A6AutoError:
        raise
    except Exception as exc:
        raise A6AutoError("seller_retirement_run_failed") from exc


def trade_topup_providers_once_command(args: argparse.Namespace) -> dict[str, Any]:
    dsn = _database_dsn(args)
    proxy_by_user_id = _proxy_map(args.proxy_map_file)

    def connection_factory() -> Any:
        return _trade_connection(dsn)

    def client_factory(context: ProviderTopupContext) -> A6ApiClient:
        return authenticated_provider_topup_client(
            context,
            proxy_by_user_id=proxy_by_user_id,
        )

    service = ProviderTopupService(
        PostgresProviderTopupStore(connection_factory),
        A6ProviderTopupRemote(client_factory),
        clock=lambda: datetime.now(timezone.utc),
        floor_units=args.floor_units,
        apply=bool(args.apply),
        strict_liquidity=bool(args.strict_liquidity),
    )
    try:
        return service.run_once(args.merchant)
    except ProviderTopupError as exc:
        raise A6AutoError(str(exc)) from exc
    except A6AutoError:
        raise
    except Exception as exc:
        raise A6AutoError("provider_topup_run_failed") from exc


def trade_reconcile_channel_growth_once_command(args: argparse.Namespace) -> dict[str, Any]:
    dsn = _database_dsn(args)
    proxy_by_user_id = _proxy_map(args.proxy_map_file)
    if args.enodot_admin_token_file is not None:
        admin_key = ""
        token_file = args.enodot_admin_token_file
    else:
        key_file = str(args.enodot_admin_key_file or "")
        admin_key = _secret_value(
            "",
            "" if key_file else str(args.enodot_admin_key_env or ""),
            key_file,
        )
        token_file = None

    session_paths: dict[int, Path] = {}
    for path in _trade_session_paths(args.sessions_dir):
        record = SessionStore(path).load()
        if record.user_id:
            session_paths[int(record.user_id)] = path.expanduser().resolve()

    def a6_client_factory(merchant: Any) -> A6ApiClient:
        user_id = int(merchant.a6_user_id)
        path = session_paths.get(user_id)
        proxy_url = proxy_by_user_id.get(user_id)
        if path is None:
            raise ChannelGrowthError("channel_growth_session_missing")
        if not proxy_url:
            raise ChannelGrowthError("channel_growth_proxy_missing")
        client, _record = authenticated_channel_growth_client(
            path,
            user_id=user_id,
            proxy_url=proxy_url,
        )
        return client

    runner = ChannelGrowthRunner(
        PostgresChannelGrowthStore(lambda: _trade_connection(dsn)),
        listing_controller=ManagedListingController(
            PostgresManagedListingStore(
                lambda: _trade_connection(dsn),
                config_version=args.config_version,
                requested_by="trade-reconcile-channel-growth-once",
            ),
            a6_client_factory,
        ),
        sessions_dir=args.sessions_dir,
        proxy_by_user_id=proxy_by_user_id,
        enodot_admin=EnodotAdminClient(
            args.enodot_admin_url,
            admin_key,
            token_file=token_file,
        ),
        seller_secret_sink=PrivateFileManagedSellerSecretSink(args.seller_key_dir),
        callback_transport=CallbackTransport(timeout=args.callback_timeout_seconds),
        callback_gateway_url=args.callback_gateway_url,
    )
    try:
        return runner.run(
            model=args.model,
            signal_max_age_seconds=args.signal_max_age_seconds,
            recycle_min_delay_seconds=args.recycle_min_delay_seconds,
            recycle_max_delay_seconds=args.recycle_max_delay_seconds,
            apply=args.apply,
        )
    except (ChannelGrowthError, ManagedListingError) as exc:
        raise A6AutoError(str(exc)) from exc
    except A6AutoError:
        raise
    except Exception as exc:
        raise A6AutoError("channel_growth_run_failed") from exc


def _is_expected_systemd_partial_result(
    command: str,
    result: Mapping[str, Any],
) -> bool:
    if command == "trade-auto-onboard-seller-channels":
        return (
            result.get("operation") == "auto_onboard_seller_channels"
            and result.get("mode") == "applied"
            and type(result.get("failed")) is int
            and result["failed"] > 0
            and isinstance(result.get("targets"), list)
        )
    if command == "trade-apply-seller-price-band":
        return (
            result.get("mode") == "applied"
            and isinstance(result.get("plan"), Mapping)
            and isinstance(result.get("merchants"), list)
        )
    if command == "trade-pin-providers-once":
        assignments = result.get("assignments")
        return isinstance(assignments, list) and any(
            isinstance(item, Mapping)
            and item.get("applied") is True
            and item.get("confirmed") is True
            for item in assignments
        )
    if command == "trade-retire-sellers-once":
        return (
            result.get("action") == "retry_scheduled"
            and type(result.get("merchant_id")) is int
            and bool(str(result.get("error_class") or "").strip())
        )
    if command == "trade-reconcile-channel-growth-once":
        return (
            result.get("mode") == "failed"
            and type(result.get("action_id")) is int
            and bool(str(result.get("error_class") or "").strip())
        )
    return False


def _result_exit_code(args: argparse.Namespace, result: Mapping[str, Any]) -> int:
    if result.get("success") is not False:
        return 0
    if (
        args.command in _SYSTEMD_PARTIAL_RESULT_COMMANDS
        and getattr(args, "systemd_accept_partial_result", False)
        and _is_expected_systemd_partial_result(args.command, result)
    ):
        return 0
    return 2


def main() -> None:
    args = parser().parse_args()
    try:
        if args.command == "run":
            result = run_command(args)
        elif args.command == "batch":
            result = batch_command(args)
        elif args.command == "refresh-session":
            result = refresh_command(args)
        elif args.command == "trade-migrate":
            result = trade_migrate_command(args)
        elif args.command == "trade-import-sessions":
            result = trade_import_sessions_command(args)
        elif args.command == "trade-refresh-sessions-once":
            result = trade_refresh_sessions_once_command(args)
        elif args.command == "trade-collect-once":
            result = trade_collect_once_command(args)
        elif args.command == "trade-dashboard":
            result = trade_dashboard_command(args)
        elif args.command == "trade-pin-providers-once":
            result = trade_pin_providers_once_command(args)
        elif args.command == "trade-assign-roles":
            result = trade_assign_roles_command(args)
        elif args.command == "trade-bind-withdraw-accounts":
            result = trade_bind_withdraw_accounts_command(args)
        elif args.command == "trade-auto-onboard-seller-channels":
            result = trade_auto_onboard_seller_channels_command(args)
        elif args.command == "trade-ensure-seller-resources":
            result = trade_ensure_seller_resources_command(args)
        elif args.command == "trade-reconcile-seller-upstreams":
            result = trade_reconcile_seller_upstreams_command(args)
        elif args.command == "trade-apply-seller-price-band":
            result = trade_apply_seller_price_band_command(args)
        elif args.command == "trade-retire-sellers-once":
            result = trade_retire_sellers_once_command(args)
        elif args.command == "trade-topup-providers-once":
            result = trade_topup_providers_once_command(args)
        elif args.command == "trade-reconcile-channel-growth-once":
            result = trade_reconcile_channel_growth_once_command(args)
        else:
            result = _validate_templates()
        if getattr(args, "compact_output", False):
            print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
        else:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        exit_code = _result_exit_code(args, result)
        if exit_code:
            raise SystemExit(exit_code)
    except A6AutoError as exc:
        payload: dict[str, Any] = {"success": False, "error": str(exc)}
        diagnostics = getattr(exc, "diagnostics", None)
        if isinstance(diagnostics, Mapping):
            payload["diagnostics"] = dict(diagnostics)
        print(json.dumps(payload, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
