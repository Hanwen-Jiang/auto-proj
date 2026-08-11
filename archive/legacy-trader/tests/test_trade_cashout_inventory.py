from __future__ import annotations

import json
from pathlib import Path

import pytest

from a6api_auto.trade_cashout_inventory import (
    CashoutInventoryError,
    load_wallet_inventory,
    rendezvous_score,
)


def inventory_path() -> Path:
    return Path(__file__).parents[1] / "src" / "a6api_auto" / "data" / "usdt_wallet_inventory.json"


def test_wallet_inventory_normalizes_principals_routes_and_secret_output() -> None:
    inventory = load_wallet_inventory(inventory_path())

    assert len(inventory.principals) == 20
    assert inventory.total_wallet_count == 61
    assert inventory.route_count == 58
    assert len(inventory.excluded_wallets) == 3
    assert len(inventory.principals[0].routes) == 3
    assert inventory.principals[0].address.startswith("0x")
    assert inventory.principals[-1].principal_type == "tron"
    assert inventory.principals[0].address not in repr(inventory.principals[0])
    assert "resource:a6-usdt-wallet-inventory-20260801" in (
        inventory.principals[0].address_secret_ref
    )


def test_wallet_inventory_rejects_invalid_tron_checksum(tmp_path: Path) -> None:
    document = json.loads(inventory_path().read_text(encoding="utf-8"))
    document["principals"][-1]["address"] = "TPB1HvBVx6ow5LhSYw8TbXCxBZT3S5dFYX"
    target = tmp_path / "wallets.json"
    target.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(CashoutInventoryError, match="cashout_wallet_address_invalid"):
        load_wallet_inventory(target)


def test_rendezvous_score_is_stable_and_merchant_specific() -> None:
    first = rendezvous_score("cashout-v2", 7, "evm-01")

    assert first == rendezvous_score("cashout-v2", 7, "evm-01")
    assert first != rendezvous_score("cashout-v2", 8, "evm-01")
