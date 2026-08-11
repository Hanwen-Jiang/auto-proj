from __future__ import annotations

import hashlib
import json
import re
from importlib.resources import files
from pathlib import Path

EVM_ADDRESS = re.compile(r"^0x[0-9a-f]{40}$")
BASE58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
EXPECTED_CONTRACTS = {
    "usdt-bsc-bep20": "0x55d398326f99059ff775485246999027b3197955",
    "usdt-eth-erc20": "0xdac17f958d2ee523a2206206994597c13d831ec7",
    "usdt-pol-polygon": "0xc2132d05d31c914a87c6611c10748aeb04b58e8f",
    "usdt-trx-trc20": "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t",
}
EXPECTED_EVM_ADDRESSES = [
    "0xd649ddc3e66b9c09395fe2556722070b00d582dd",
    "0x36cb9d31c6f615168d088dbf0a80e9687a5c85ba",
    "0x46bccb8733c64a3ab86dea0ec9fbc12fcb137987",
    "0xae75f8f680407022d151b6d17cc2aaa5a5e07468",
    "0x1f7460112f0afefe08905037f5e9a318192cc3b2",
    "0x058fe55e8161e93e7c5049ddacd1c4a987f09789",
    "0xef73525b073d890b09b48ed7e8df41d7d3f677ab",
    "0x6c76bb20b61a6fc3cad868234192755e4bf9dee8",
    "0x04f3a751b9f5c1fd1755fddbb89b65fe283f269d",
    "0x17a6989f2f129a55eca65cae26c615726a1e3933",
    "0x84ea8586ec5d676c4613ffcc1b3154ac305cb332",
    "0x66ed06ff8c009bd1e98d2a065783a9b962b25991",
    "0x764e91a7810e128a029446889e19b5dc5fba8675",
    "0x52a27bf1965ee1e0a294ae30c83bd48dec7b3b28",
    "0x85bf74ef66c810d51b800fc65a52551975d6c241",
    "0xee378a9fe55d9b349139ae54e381430d9491ab68",
    "0x850aed47bc089c8b0ce28ab758e4211031db1e5a",
    "0x06f3b8f81da6d7926d19e4a99b7357dfbabde2c0",
    "0x27dda880da2e22fd9a2d693e9f63527bcfaf1fe1",
    "0xd9cc9386e1e7c39da1129548eadb730dc70e58ed",
]
EXPECTED_TRON_ADDRESS = "TPB1HvBVx6ow5LhSYw8TbXCxBZT3S5dFYY"


def _wallet_inventory() -> dict:
    resource = files("a6api_auto").joinpath("data/usdt_wallet_inventory.json")
    return json.loads(resource.read_text(encoding="utf-8"))


def _base58check_valid(value: str) -> bool:
    number = 0
    try:
        for character in value:
            number = number * 58 + BASE58_ALPHABET.index(character)
    except ValueError:
        return False
    decoded = number.to_bytes((number.bit_length() + 7) // 8, "big")
    decoded = b"\0" * (len(value) - len(value.lstrip("1"))) + decoded
    if len(decoded) != 25:
        return False
    checksum = hashlib.sha256(hashlib.sha256(decoded[:-4]).digest()).digest()[:4]
    return decoded[-4:] == checksum


def test_usdt_wallet_inventory_principal_and_route_counts() -> None:
    document = _wallet_inventory()
    routes = {item["route_id"]: item for item in document["route_definitions"]}
    principals = document["principals"]
    evm = [item for item in principals if item["chain_family"] == "evm"]
    tron = [item for item in principals if item["chain_family"] == "tron"]

    assert document["schema_version"] == 1
    assert document["asset"] == "USDT"
    assert len(routes) == 4
    assert len(principals) == 21
    assert len(evm) == 20
    assert len(tron) == 1
    assert sum(len(item["route_ids"]) for item in principals) == 61
    assert len(document["excluded_wallets"]) == 3
    assert document["summary"] == {
        "principal_count": 21,
        "evm_principal_count": 20,
        "tron_principal_count": 1,
        "route_assignment_count": 61,
        "active_route_assignment_count": 58,
        "excluded_route_assignment_count": 3,
    }


def test_usdt_wallet_inventory_addresses_and_contracts_are_valid() -> None:
    document = _wallet_inventory()
    routes = {item["route_id"]: item for item in document["route_definitions"]}
    principals = document["principals"]
    addresses = [item["address"] for item in principals]

    assert len(addresses) == len(set(addresses))
    assert all(
        EVM_ADDRESS.fullmatch(item["address"])
        for item in principals
        if item["chain_family"] == "evm"
    )
    tron_address = next(
        item["address"] for item in principals if item["chain_family"] == "tron"
    )
    assert [
        item["address"] for item in principals if item["chain_family"] == "evm"
    ] == EXPECTED_EVM_ADDRESSES
    assert tron_address == EXPECTED_TRON_ADDRESS
    assert _base58check_valid(tron_address)
    assert {route_id: item["token_contract"] for route_id, item in routes.items()} == (
        EXPECTED_CONTRACTS
    )
    assert all(
        set(item["route_ids"]) <= set(routes)
        and all(
            routes[route_id]["chain_family"] == item["chain_family"]
            for route_id in item["route_ids"]
        )
        for item in principals
    )


def test_design_document_links_versioned_treasury_inputs() -> None:
    repository = Path(__file__).resolve().parents[1]
    document = (
        repository / "docs/a6-provider-topn-failover-design-20260728.md"
    ).read_text(encoding="utf-8")

    assert "../src/a6api_auto/data/usdt_wallet_inventory.json" in document
    assert "../src/a6api_auto/data/token_name_corpus.json" in document
    assert "../src/a6api_auto/data/merchant_templates_split.xlsx" in document
