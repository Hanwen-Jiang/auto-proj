from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

from a6api_auto.cli import _proxy_map
from a6api_auto.client import A6ApiClient
from a6api_auto.storage import SessionStore
from a6api_auto.trade_liquidity_token import (
    LiquidityTokenReconciler,
    PostgresLiquidityTokenStore,
)


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(
        description="Reconcile one independent duty-scoped liquidity token."
    )
    command.add_argument("--session", type=Path, required=True)
    command.add_argument("--proxy-map-file", type=Path, required=True)
    command.add_argument("--database-dsn-env", default="A6_TRADE_DATABASE_DSN")
    command.add_argument("--model", default="gpt-5.6-sol")
    command.add_argument(
        "--secret-dir",
        type=Path,
        default=Path("/var/lib/a6-trader/secrets/liquidity-tokens"),
    )
    command.add_argument("--receiver-label", default="小微商户")
    command.add_argument("--receiver-merchant-id", type=int)
    command.add_argument("--receiver-freshness-seconds", type=int, default=300)
    command.add_argument("--marketplace-page-size", type=int, default=100)
    return command


def main() -> int:
    args = parser().parse_args()
    record = SessionStore(args.session).load()
    if not record.user_id:
        raise RuntimeError("liquidity_token_session_user_missing")
    proxy_url = _proxy_map(args.proxy_map_file).get(int(record.user_id))
    if not proxy_url:
        raise RuntimeError("liquidity_token_proxy_missing")
    dsn = str(os.environ.get(args.database_dsn_env, "")).strip()
    if not dsn:
        raise RuntimeError("liquidity_token_database_dsn_missing")

    def connection_factory():
        return psycopg.connect(dsn, row_factory=dict_row, connect_timeout=5)

    client = A6ApiClient(record.origin, proxy_url=proxy_url, timeout=15)
    client.import_cookies(record.cookies)
    result = LiquidityTokenReconciler(
        PostgresLiquidityTokenStore(connection_factory),
        client,
        secret_dir=args.secret_dir,
        receiver_label=args.receiver_label,
        receiver_merchant_id=args.receiver_merchant_id,
        receiver_freshness_seconds=args.receiver_freshness_seconds,
        marketplace_page_size=args.marketplace_page_size,
    ).reconcile(
        a6_user_id=int(record.user_id),
        session_ref=str(args.session.expanduser().resolve()),
        model=args.model,
    )
    print(json.dumps(result, ensure_ascii=True, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
