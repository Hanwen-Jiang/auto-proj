from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import A6AutoError

_BASE58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_BASE58_INDEX = {character: index for index, character in enumerate(_BASE58_ALPHABET)}


class CashoutInventoryError(A6AutoError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class WalletRouteDefinition:
    route_key: str
    chain_family: str
    network: str
    protocol: str
    token_contract_sha256: str
    token_contract_display: str


@dataclass(frozen=True)
class WalletPrincipalDefinition:
    principal_key: str
    principal_type: str
    address: str
    address_sha256: str
    address_masked: str
    address_secret_ref: str
    routes: tuple[WalletRouteDefinition, ...]

    def __repr__(self) -> str:
        return (
            "WalletPrincipalDefinition("
            f"principal_key={self.principal_key!r}, "
            f"principal_type={self.principal_type!r}, "
            f"address_masked={self.address_masked!r}, "
            f"routes={self.routes!r})"
        )


@dataclass(frozen=True)
class WalletInventory:
    inventory_id: str
    schema_version: int
    asset: str
    principals: tuple[WalletPrincipalDefinition, ...]
    excluded_wallets: tuple[tuple[str, str], ...] = ()

    @property
    def route_count(self) -> int:
        return sum(len(principal.routes) for principal in self.principals)

    @property
    def total_wallet_count(self) -> int:
        return self.route_count + len(self.excluded_wallets)


def load_wallet_inventory(path: Path | str) -> WalletInventory:
    source = Path(path).expanduser().resolve()
    try:
        document = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise CashoutInventoryError("cashout_wallet_inventory_unreadable") from None
    if not isinstance(document, dict):
        raise CashoutInventoryError("cashout_wallet_inventory_shape_invalid")
    schema_version = _positive_integer(document.get("schema_version"))
    inventory_id = str(document.get("inventory_id") or "").strip()
    asset = str(document.get("asset") or "").strip().upper()
    raw_routes = document.get("route_definitions")
    raw_principals = document.get("principals")
    raw_exclusions = document.get("excluded_wallets", [])
    if (
        schema_version != 1
        or not inventory_id
        or asset != "USDT"
        or not isinstance(raw_routes, list)
        or not isinstance(raw_principals, list)
        or not isinstance(raw_exclusions, list)
    ):
        raise CashoutInventoryError("cashout_wallet_inventory_shape_invalid")

    routes: dict[str, WalletRouteDefinition] = {}
    for item in raw_routes:
        if not isinstance(item, dict):
            raise CashoutInventoryError("cashout_wallet_route_invalid")
        route_key = str(item.get("route_id") or "").strip()
        chain_family = str(item.get("chain_family") or "").strip().lower()
        network = str(item.get("network") or "").strip().upper()
        protocol = str(item.get("protocol") or "").strip()
        contract = _normalized_contract(chain_family, item.get("token_contract"))
        if (
            not route_key
            or route_key in routes
            or chain_family not in {"evm", "tron"}
            or not network
            or not protocol
        ):
            raise CashoutInventoryError("cashout_wallet_route_invalid")
        routes[route_key] = WalletRouteDefinition(
            route_key=route_key,
            chain_family=chain_family,
            network=network,
            protocol=protocol,
            token_contract_sha256=hashlib.sha256(contract.encode()).hexdigest(),
            token_contract_display=contract,
        )

    exclusions: set[tuple[str, str]] = set()
    for item in raw_exclusions:
        if not isinstance(item, dict) or set(item) != {"route_id", "address", "reason"}:
            raise CashoutInventoryError("cashout_wallet_exclusion_invalid")
        route_key = str(item.get("route_id") or "").strip()
        route = routes.get(route_key)
        reason = str(item.get("reason") or "").strip()
        if route is None or reason != "operator_excluded":
            raise CashoutInventoryError("cashout_wallet_exclusion_invalid")
        address = _normalized_address(route.chain_family, item.get("address"))
        identity = (route_key, address)
        if identity in exclusions:
            raise CashoutInventoryError("cashout_wallet_exclusion_invalid")
        exclusions.add(identity)

    principals: list[WalletPrincipalDefinition] = []
    principal_keys: set[str] = set()
    principal_hashes: set[tuple[str, str]] = set()
    for item in raw_principals:
        if not isinstance(item, dict):
            raise CashoutInventoryError("cashout_wallet_principal_invalid")
        key = str(item.get("principal_id") or "").strip()
        principal_type = str(item.get("chain_family") or "").strip().lower()
        address = _normalized_address(principal_type, item.get("address"))
        raw_route_ids = item.get("route_ids")
        if (
            not key
            or key in principal_keys
            or principal_type not in {"evm", "tron"}
            or not isinstance(raw_route_ids, list)
            or not raw_route_ids
        ):
            raise CashoutInventoryError("cashout_wallet_principal_invalid")
        selected: list[WalletRouteDefinition] = []
        seen_routes: set[str] = set()
        for raw_route_id in raw_route_ids:
            route_id = str(raw_route_id or "").strip()
            route = routes.get(route_id)
            if route is None or route_id in seen_routes or route.chain_family != principal_type:
                raise CashoutInventoryError("cashout_wallet_principal_route_invalid")
            if (route_id, address) not in exclusions:
                selected.append(route)
            seen_routes.add(route_id)
        address_hash = hashlib.sha256(address.encode()).hexdigest()
        identity = (principal_type, address_hash)
        if identity in principal_hashes:
            raise CashoutInventoryError("cashout_wallet_principal_duplicate")
        principal_keys.add(key)
        principal_hashes.add(identity)
        if selected:
            principals.append(
                WalletPrincipalDefinition(
                    principal_key=key,
                    principal_type=principal_type,
                    address=address,
                    address_sha256=address_hash,
                    address_masked=_mask_address(address),
                    address_secret_ref=(f"resource:{inventory_id}#principals/{key}/address"),
                    routes=tuple(selected),
                )
            )

    summary = document.get("summary")
    if not principals or not isinstance(summary, dict):
        raise CashoutInventoryError("cashout_wallet_inventory_shape_invalid")
    source_principal_count = len(raw_principals)
    source_route_count = sum(
        len(item.get("route_ids", ())) for item in raw_principals if isinstance(item, dict)
    )
    active_route_count = sum(len(principal.routes) for principal in principals)
    if (
        _positive_integer(summary.get("principal_count")) != source_principal_count
        or _positive_integer(summary.get("route_assignment_count")) != source_route_count
        or _positive_integer(summary.get("active_route_assignment_count"))
        != active_route_count
        or _positive_integer(summary.get("excluded_route_assignment_count"))
        != len(exclusions)
        or source_route_count != active_route_count + len(exclusions)
    ):
        raise CashoutInventoryError("cashout_wallet_inventory_summary_mismatch")
    return WalletInventory(
        inventory_id=inventory_id,
        schema_version=schema_version,
        asset=asset,
        principals=tuple(principals),
        excluded_wallets=tuple(sorted(exclusions)),
    )


def rendezvous_score(config_version: str, merchant_id: int, principal_key: str) -> str:
    version = str(config_version or "").strip()
    key = str(principal_key or "").strip()
    if not version or merchant_id < 1 or not key:
        raise CashoutInventoryError("cashout_wallet_assignment_input_invalid")
    return hashlib.sha256(f"{version}:{merchant_id}:{key}".encode()).hexdigest()


def _normalized_contract(chain_family: str, value: Any) -> str:
    contract = str(value or "").strip()
    if chain_family == "evm":
        return _normalized_evm(contract, "cashout_wallet_contract_invalid")
    if chain_family == "tron" and _valid_tron_address(contract):
        return contract
    raise CashoutInventoryError("cashout_wallet_contract_invalid")


def _normalized_address(principal_type: str, value: Any) -> str:
    address = str(value or "").strip()
    if principal_type == "evm":
        return _normalized_evm(address, "cashout_wallet_address_invalid")
    if principal_type == "tron" and _valid_tron_address(address):
        return address
    raise CashoutInventoryError("cashout_wallet_address_invalid")


def _normalized_evm(value: str, error: str) -> str:
    normalized = value.lower()
    if (
        len(normalized) != 42
        or not normalized.startswith("0x")
        or any(character not in "0123456789abcdef" for character in normalized[2:])
    ):
        raise CashoutInventoryError(error)
    return normalized


def _valid_tron_address(value: str) -> bool:
    try:
        decoded = _base58check_decode(value)
    except ValueError:
        return False
    return len(decoded) == 21 and decoded[0] == 0x41


def _base58check_decode(value: str) -> bytes:
    if not value:
        raise ValueError("empty")
    number = 0
    for character in value:
        if character not in _BASE58_INDEX:
            raise ValueError("alphabet")
        number = number * 58 + _BASE58_INDEX[character]
    width = max(1, (number.bit_length() + 7) // 8)
    decoded = number.to_bytes(width, "big")
    leading_zeroes = len(value) - len(value.lstrip("1"))
    decoded = b"\0" * leading_zeroes + decoded
    if len(decoded) < 5:
        raise ValueError("short")
    payload, checksum = decoded[:-4], decoded[-4:]
    expected = hashlib.sha256(hashlib.sha256(payload).digest()).digest()[:4]
    if checksum != expected:
        raise ValueError("checksum")
    return payload


def _mask_address(address: str) -> str:
    return f"{address[:6]}...{address[-4:]}"


def _positive_integer(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 0
    return parsed if parsed > 0 else 0
