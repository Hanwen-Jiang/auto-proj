#!/usr/bin/env python3
"""Reconcile profit-gate params (profit_required / oauth_auto_cost) on sub2api panel accounts.

Policy (per model):
- Every enabled sub2api economic account gets profit_required = (1 + bps/10000) * 1.2 - 1
  (gross listing factor) and oauth_auto_cost = true (panel auto-updates its real-time
  cost multiplier K from usage_logs).
- Every bound provider account gets profit_required = (1 + margin_bps/10000) * 1.2 - 1
  and oauth_auto_cost = false (key account multiplier stays externally/manually written).

Mutations are GET-merge-PUT-readback against the Enodot admin API and only happen
with --apply; otherwise the plan is reported.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row

from a6api_auto.enodot_admin import EnodotAdminClient, EnodotAdminError
from a6api_auto.trade_profit_settings import (
    A6_GROSS_LISTING_FACTOR,
    DEFAULT_PROFIT_ON_COST_BPS,
    PostgresModelProfitSettingStore,
)

CONFIG_VERSION = "panel-profit-gate-v1"
LOCK_FILE = Path("/var/lib/a6-trader/panel-profit-gate.lock")
_DEFAULT_MARGIN_BPS = 2000


class PanelProfitGateError(RuntimeError):
    pass


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--model", default=os.environ.get("A6_TRADER_MODEL", "gpt-5.6-sol"))
    p.add_argument("--apply", action="store_true")
    p.add_argument("--enodot-admin-url", default=os.environ.get("ENODOT_ADMIN_URL", ""))
    p.add_argument(
        "--enodot-admin-key-env",
        default=os.environ.get("ENODOT_ADMIN_KEY_ENV", "ENODOT_ADMIN_KEY"),
    )
    p.add_argument(
        "--enodot-admin-key-file",
        default=os.environ.get("ENODOT_ADMIN_KEY_FILE", ""),
    )
    p.add_argument(
        "--enodot-admin-token-file",
        default=os.environ.get("ENODOT_ADMIN_TOKEN_FILE", ""),
    )
    p.add_argument(
        "--database-dsn-env",
        default=os.environ.get("A6_TRADE_DATABASE_DSN_ENV", "A6_TRADE_DATABASE_DSN"),
    )
    p.add_argument(
        "--enodot-admin-timeout-seconds",
        default=float(os.environ.get("ENODOT_ADMIN_TIMEOUT_SECONDS", "30")),
    )
    return p


def _admin_client(args: argparse.Namespace) -> EnodotAdminClient:
    base_url = str(args.enodot_admin_url or "").strip()
    if not base_url:
        raise PanelProfitGateError("enodot_admin_url_missing")
    token_file = str(args.enodot_admin_token_file or "").strip()
    admin_key = ""
    if not token_file:
        key_file = str(args.enodot_admin_key_file or "").strip()
        if key_file:
            admin_key = Path(key_file).read_text(encoding="utf-8").strip()
        else:
            admin_key = str(os.environ.get(str(args.enodot_admin_key_env), "") or "").strip()
        if not admin_key:
            raise PanelProfitGateError("enodot_admin_credential_missing")
    return EnodotAdminClient(
        base_url,
        admin_key,
        token_file=Path(token_file) if token_file else None,
        timeout=float(args.enodot_admin_timeout_seconds),
    )


@contextmanager
def _locked(lock_file: Path) -> Iterator[None]:
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_file, "a", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _profit_rate(bps: int) -> Decimal:
    return ((Decimal(1) + Decimal(int(bps)) / Decimal(10_000)) * A6_GROSS_LISTING_FACTOR - 1).quantize(
        Decimal("0.000001"), rounding=ROUND_HALF_UP
    )


def _active_provider_margin_bps(connection: Any, model: str) -> int:
    row = connection.execute(
        """
        SELECT cost.margin_bps
        FROM a6_trade.provider_cost_generations cost
        JOIN a6_trade.provider_pool_generations generation
          ON generation.model = cost.model
         AND generation.cost_generation = cost.cost_generation
         AND generation.cost_generation_id = cost.cost_generation_id
        WHERE generation.model = %s
          AND generation.generation_state = 'active'
          AND cost.pricing_state = 'active'
        ORDER BY cost.cost_generation DESC
        LIMIT 1
        """,
        (model,),
    ).fetchone()
    if row is None:
        return _DEFAULT_MARGIN_BPS
    try:
        margin = int(row.get("margin_bps") or _DEFAULT_MARGIN_BPS)
    except (TypeError, ValueError):
        return _DEFAULT_MARGIN_BPS
    return margin if 0 <= margin <= 100_000 else _DEFAULT_MARGIN_BPS


def _reconcile_accounts(
    *,
    admin: EnodotAdminClient,
    accounts: Mapping[int, Mapping[str, Any]],
    apply: bool,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for account_id, params in sorted(accounts.items()):
        try:
            result = admin.reconcile_account_profit_gate(
                account_id,
                profit_required=float(params["profit_required"]),
                oauth_auto_cost=bool(params["oauth_auto_cost"]),
                apply=apply,
            )
        except EnodotAdminError as exc:
            results.append(
                {
                    "account_id": account_id,
                    "action": "error",
                    "applied": False,
                    "error": exc.code,
                }
            )
            continue
        results.append(
            {
                "account_id": account_id,
                "action": result.action,
                "applied": result.applied,
                "changed_fields": list(result.changed_fields),
            }
        )
    return results


def run_model(
    *,
    model: str,
    apply: bool,
    admin: EnodotAdminClient,
    connection_factory: Any,
) -> dict[str, Any]:
    connection = connection_factory()
    try:
        setting = PostgresModelProfitSettingStore(connection_factory).get(model)
        profit_bps = (
            int(setting.minimum_profit_on_cost_bps)
            if setting is not None
            else DEFAULT_PROFIT_ON_COST_BPS
        )
        margin_bps = _active_provider_margin_bps(connection, model)
        sub2api_rate = _profit_rate(profit_bps)
        provider_rate = _profit_rate(margin_bps)

        source_rows = connection.execute(
            """
            SELECT DISTINCT enodot_account_id
            FROM a6_trade.sub2api_economic_sources
            WHERE enodot_account_id IS NOT NULL
              AND enabled
            """
        ).fetchall()
        provider_rows = connection.execute(
            """
            SELECT DISTINCT enodot_account_id
            FROM a6_trade.enodot_account_bindings
            WHERE enodot_account_id IS NOT NULL
              AND state = 'bound'
            """
        ).fetchall()

        accounts: dict[int, dict[str, Any]] = {}
        for row in source_rows:
            accounts[int(row["enodot_account_id"])] = {
                "kind": "sub2api",
                "profit_required": sub2api_rate,
                "oauth_auto_cost": True,
            }
        for row in provider_rows:
            account_id = int(row["enodot_account_id"])
            accounts.setdefault(account_id, {})["kind"] = "provider"
            accounts[account_id]["profit_required"] = provider_rate
            accounts[account_id]["oauth_auto_cost"] = False

        results = _reconcile_accounts(admin=admin, accounts=accounts, apply=apply)
    finally:
        connection.close()

    summary: dict[str, int] = {}
    for item in results:
        summary[item["action"]] = summary.get(item["action"], 0) + 1
    return {
        "config_version": CONFIG_VERSION,
        "model": model,
        "evaluated_at": datetime.now(timezone.utc).isoformat(),
        "apply": bool(apply),
        "profit_required": {
            "sub2api_profit_bps": int(profit_bps),
            "sub2api_rate": str(sub2api_rate),
            "provider_margin_bps": int(margin_bps),
            "provider_rate": str(provider_rate),
        },
        "accounts": results,
        "summary": summary,
    }


def main() -> None:
    args = parser().parse_args()
    dsn = str(os.environ.get(str(args.database_dsn_env), "")).strip()
    if not dsn:
        raise PanelProfitGateError("economic_database_dsn_missing")

    def connection_factory() -> Any:
        return psycopg.connect(dsn, row_factory=dict_row)

    admin = _admin_client(args)
    with _locked(LOCK_FILE):
        print(
            json.dumps(
                run_model(
                    model=str(args.model),
                    apply=bool(args.apply),
                    admin=admin,
                    connection_factory=connection_factory,
                ),
                ensure_ascii=True,
                sort_keys=True,
            )
        )


if __name__ == "__main__":
    main()
