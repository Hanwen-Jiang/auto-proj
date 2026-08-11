#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import psycopg
from psycopg.rows import dict_row

from a6api_auto.client import A6ApiClient
from a6api_auto.storage import SessionStore, read_private_json, write_private_json
from a6api_auto.trade_collector import _earning_event
from a6api_auto.trade_repository import PostgresTradeRepository

UTC = timezone.utc


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(description="Idempotently backfill A6 Seller earnings")
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
    command.add_argument(
        "--state-file",
        type=Path,
        default=Path("/var/lib/a6-trader/secrets/earnings-backfill-state.json"),
    )
    command.add_argument("--page-size", type=int, default=100)
    command.add_argument("--max-pages", type=int, default=1000)
    command.add_argument("--sleep-seconds", type=float, default=0.15)
    return command


def _load_state(path: Path) -> dict:
    if not path.exists():
        return {"schema_version": 1, "users": {}}
    state = read_private_json(path)
    if state.get("schema_version") != 1 or not isinstance(state.get("users"), dict):
        raise RuntimeError("earnings_backfill_state_invalid")
    return state


def _eligible_users(dsn: str) -> set[int]:
    with psycopg.connect(dsn, row_factory=dict_row) as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT m.a6_user_id
            FROM a6_trade.a6_merchants m
            JOIN a6_trade.a6_financial_current f ON f.merchant_id = m.id
            WHERE m.a6_user_id IS NOT NULL AND f.lifetime_earned_micros > 0
            """
        )
        return {int(row["a6_user_id"]) for row in cursor.fetchall()}


def _merchant_id(connection, user_id: int) -> int:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT id FROM a6_trade.a6_merchants WHERE a6_user_id = %s",
            (user_id,),
        )
        row = cursor.fetchone()
    if row is None:
        raise RuntimeError("earnings_backfill_merchant_missing")
    return int(row["id"])


def _persist_page(dsn: str, user_id: int, items: list[dict]) -> None:
    events = tuple(_earning_event(user_id, item) for item in items)
    with psycopg.connect(dsn, row_factory=dict_row) as connection:
        merchant_id = _merchant_id(connection, user_id)
        with connection.cursor() as cursor:
            PostgresTradeRepository._insert_earnings(
                cursor,
                merchant_id,
                SimpleNamespace(earnings=events),
                datetime.now(UTC),
            )
        connection.commit()


def main() -> None:
    args = parser().parse_args()
    if args.page_size < 1 or args.max_pages < 1 or args.sleep_seconds < 0:
        raise RuntimeError("earnings_backfill_limits_invalid")
    dsn = os.environ.get("A6_TRADE_DATABASE_DSN", "").strip()
    if not dsn:
        raise RuntimeError("earnings_backfill_database_dsn_missing")
    proxy_map = {
        int(user_id): str(proxy)
        for user_id, proxy in read_private_json(args.proxy_map_file).items()
    }
    state = _load_state(args.state_file)
    eligible = _eligible_users(dsn)

    for session_path in sorted(args.sessions_dir.glob("session-*.json"), key=str):
        record = SessionStore(session_path).load()
        if record.status != "supplier_submitted" or not record.user_id:
            continue
        user_id = int(record.user_id)
        if user_id not in eligible:
            continue
        user_state = state["users"].setdefault(str(user_id), {"next_page": 1})
        if user_state.get("complete") is True:
            continue
        proxy_url = proxy_map.get(user_id)
        if not proxy_url:
            raise RuntimeError("earnings_backfill_proxy_missing")
        client = A6ApiClient(record.origin, proxy_url=proxy_url)
        client.import_cookies(record.cookies)
        page = max(1, int(user_state.get("next_page") or 1))

        while page <= args.max_pages:
            result = client.supplier_earnings(user_id, page=page, size=args.page_size)
            items = result["items"]
            total = int(result.get("total") or 0)
            if items:
                _persist_page(dsn, user_id, items)
            user_state.update(
                {
                    "next_page": page + 1,
                    "last_page": page,
                    "total": total,
                    "updated_at": datetime.now(UTC).isoformat(),
                }
            )
            done = not items or len(items) < args.page_size or page * args.page_size >= total
            if done:
                user_state["complete"] = True
            write_private_json(args.state_file, state)
            if page == 1 or page % 10 == 0 or done:
                print(
                    json.dumps(
                        {
                            "user_id": user_id,
                            "page": page,
                            "items": len(items),
                            "total": total,
                            "complete": done,
                        },
                        ensure_ascii=True,
                    ),
                    flush=True,
                )
            if done:
                break
            page += 1
            time.sleep(args.sleep_seconds)
        else:
            raise RuntimeError("earnings_backfill_page_limit_reached")

    with psycopg.connect(dsn, row_factory=dict_row) as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT coalesce(sum(f.lifetime_earned_micros), 0)::bigint AS lifetime_micros,
                   coalesce((SELECT sum(e.earned_micros)
                             FROM a6_trade.a6_earning_events e), 0)::bigint AS event_micros,
                   (SELECT count(*) FROM a6_trade.a6_earning_events)::bigint AS event_count
            FROM a6_trade.a6_financial_current f
            """
        )
        totals = dict(cursor.fetchone())
    totals["gap_micros"] = int(totals["lifetime_micros"]) - int(totals["event_micros"])
    print(json.dumps({"success": True, **totals}, ensure_ascii=True, sort_keys=True))


if __name__ == "__main__":
    main()
