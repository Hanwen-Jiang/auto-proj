from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from a6api_auto.cli import _proxy_map, _secret_value, _trade_connection, _trade_session_paths
from a6api_auto.client import A6ApiClient
from a6api_auto.enodot_admin import EnodotAdminClient
from a6api_auto.storage import SessionStore
from a6api_auto.trade_channel_growth import (
    CallbackTransport,
    ChannelGrowthRunner,
    PostgresChannelGrowthStore,
)
from a6api_auto.trade_enodot_seller_resources import PrivateFileManagedSellerSecretSink
from a6api_auto.trade_managed_listing_store import PostgresManagedListingStore
from a6api_auto.trade_managed_listings import ManagedListingController


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(
        description="Run one audited callback burst for a specific retirement target."
    )
    command.add_argument("--merchant-id", type=int, required=True)
    command.add_argument("--expected-requests", type=int, required=True)
    command.add_argument("--expected-concurrency", type=int, required=True)
    command.add_argument("--expected-booster-token-id", type=int, required=True)
    command.add_argument(
        "--action-type",
        choices=("callback_readiness", "callback_burst"),
        default="callback_burst",
    )
    command.add_argument("--sessions-dir", type=Path, required=True)
    command.add_argument("--proxy-map-file", type=Path, required=True)
    command.add_argument("--seller-key-dir", type=Path, required=True)
    command.add_argument("--enodot-admin-key-file", type=Path, required=True)
    command.add_argument("--model", default="gpt-5.6-sol")
    command.add_argument("--signal-max-age-seconds", type=int, default=45)
    command.add_argument("--callback-gateway-url", default="https://a6.a6api.com")
    command.add_argument("--callback-timeout-seconds", type=float, default=30)
    command.add_argument("--config-version", default="retirement-callback-v1")
    return command


def build_runner(args: argparse.Namespace) -> ChannelGrowthRunner:
    dsn = os.environ.get("A6_TRADE_DATABASE_DSN", "").strip()
    if not dsn:
        raise RuntimeError("trade_database_dsn_environment_empty")
    proxy_by_user_id = _proxy_map(args.proxy_map_file)
    records: dict[int, Any] = {}
    for path in _trade_session_paths(args.sessions_dir):
        record = SessionStore(path).load()
        if record.user_id:
            records[int(record.user_id)] = record

    def connection_factory() -> Any:
        return _trade_connection(dsn)

    def a6_client_factory(merchant: Any) -> A6ApiClient:
        user_id = int(merchant.a6_user_id)
        record = records.get(user_id)
        proxy_url = proxy_by_user_id.get(user_id)
        if record is None:
            raise RuntimeError("channel_growth_session_missing")
        if not proxy_url:
            raise RuntimeError("channel_growth_proxy_missing")
        client = A6ApiClient(record.origin, proxy_url=proxy_url, timeout=15)
        client.import_cookies(record.cookies)
        return client

    admin_key = _secret_value("", "", str(args.enodot_admin_key_file))
    return ChannelGrowthRunner(
        PostgresChannelGrowthStore(connection_factory),
        listing_controller=ManagedListingController(
            PostgresManagedListingStore(
                connection_factory,
                config_version=args.config_version,
                requested_by="run-retirement-callback-once",
            ),
            a6_client_factory,
        ),
        sessions_dir=args.sessions_dir,
        proxy_by_user_id=proxy_by_user_id,
        enodot_admin=EnodotAdminClient("https://ai.enodot.shop", admin_key),
        seller_secret_sink=PrivateFileManagedSellerSecretSink(args.seller_key_dir),
        callback_transport=CallbackTransport(timeout=args.callback_timeout_seconds),
        callback_gateway_url=args.callback_gateway_url,
    )


def main() -> int:
    args = parser().parse_args()
    if args.merchant_id < 1:
        raise RuntimeError("retirement_callback_merchant_invalid")
    if args.expected_requests < 1 or not 1 <= args.expected_concurrency <= args.expected_requests:
        raise RuntimeError("retirement_callback_batch_invalid")

    runner = build_runner(args)
    targets = runner.store.targets(
        args.model,
        signal_max_age_seconds=args.signal_max_age_seconds,
    )
    matches = tuple(target for target in targets if target.merchant_id == args.merchant_id)
    if len(matches) != 1:
        raise RuntimeError("retirement_callback_target_mismatch")
    target = matches[0]
    if not target.callback_enabled:
        raise RuntimeError("retirement_callback_disabled")
    if (
        target.callback_requests != args.expected_requests
        or target.callback_concurrency != args.expected_concurrency
    ):
        raise RuntimeError("retirement_callback_policy_readback_mismatch")

    boosters = runner.store.boosters(target.merchant_id)
    if len(boosters) != 1 or boosters[0].token_id != args.expected_booster_token_id:
        raise RuntimeError("retirement_callback_booster_readback_mismatch")

    result = runner._boost(
        targets,
        target,
        target.health_state(datetime.now(timezone.utc)),
        held={},
        action_type=args.action_type,
    )
    print(json.dumps(result, ensure_ascii=True, separators=(",", ":")))
    return 0 if result.get("mode") == "completed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
