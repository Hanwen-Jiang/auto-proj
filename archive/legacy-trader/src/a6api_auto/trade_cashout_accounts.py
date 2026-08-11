from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol

from .errors import A6AutoError
from .trade_cashout_inventory import WalletInventory

FUND_LOCK_NAMESPACE = 0x41364652


class TreasuryAccountError(A6AutoError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class TreasuryAccountMerchant:
    merchant_id: int
    a6_user_id: int
    session_ref: str


@dataclass(frozen=True)
class TreasuryAccountTarget:
    merchant: TreasuryAccountMerchant
    wallet_route_id: int
    principal_key: str
    route_key: str
    network: str
    protocol: str
    address: str = field(repr=False)
    current_remote_account_id: int | None = None


@dataclass(frozen=True)
class TreasuryAccountObservation:
    merchant_id: int
    wallet_route_id: int
    remote_account_id: int
    source_hash: str
    observed_at: datetime
    healthy: bool


@dataclass(frozen=True)
class _RemoteAccount:
    account_id: int
    protocol: str
    address: str = field(repr=False)
    source_hash: str


class TreasuryAccountClient(Protocol):
    def supplier_withdraw_accounts(self, user_id: int) -> list[dict[str, Any]]: ...

    def create_supplier_withdraw_account(
        self,
        user_id: int,
        *,
        network: str,
        address: str,
        remark: str,
        is_default: bool = False,
    ) -> dict[str, Any]: ...


class PostgresTreasuryAccountStore:
    def __init__(self, connection_factory: Callable[[], Any]) -> None:
        if not callable(connection_factory):
            raise TreasuryAccountError("treasury_account_connection_factory_invalid")
        self.connection_factory = connection_factory

    def list_assignable_merchant_ids(self) -> tuple[int, ...]:
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT id
                    FROM a6_trade.a6_merchants
                    WHERE a6_user_id IS NOT NULL
                      AND supplier_id IS NOT NULL
                      AND coalesce(session_ref, '') <> ''
                      AND status = 'supplier_submitted'
                    ORDER BY id
                    """
                )
                return tuple(int(row["id"]) for row in _rows(cursor))
        finally:
            _close(connection)

    def release_ineligible_assignments(self) -> int:
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE a6_trade.merchant_treasury_assignments assignment
                    SET assignment_state = 'released',
                        released_at = now(),
                        updated_at = now()
                    FROM a6_trade.a6_merchants merchant
                    WHERE merchant.id = assignment.merchant_id
                      AND assignment.assignment_state = 'active'
                      AND (
                          merchant.status <> 'supplier_submitted'
                          OR merchant.a6_user_id IS NULL
                          OR merchant.supplier_id IS NULL
                          OR coalesce(merchant.session_ref, '') = ''
                      )
                    """
                )
                released = max(0, int(cursor.rowcount))
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            _close(connection)
        return released

    def list_targets(self, inventory: WalletInventory) -> tuple[TreasuryAccountTarget, ...]:
        principals = {item.principal_key: item for item in inventory.principals}
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT assignment.merchant_id, merchant.a6_user_id,
                           merchant.session_ref, principal.inventory_principal_key,
                           route.id AS wallet_route_id, route.inventory_route_key,
                           route.protocol, binding.a6_withdraw_account_id
                    FROM a6_trade.merchant_treasury_assignments assignment
                    JOIN a6_trade.a6_merchants merchant
                      ON merchant.id = assignment.merchant_id
                    JOIN a6_trade.treasury_wallet_principals principal
                      ON principal.id = assignment.wallet_principal_id
                    JOIN a6_trade.treasury_wallet_routes route
                      ON route.wallet_principal_id = principal.id
                    LEFT JOIN a6_trade.merchant_treasury_route_bindings binding
                      ON binding.merchant_id = assignment.merchant_id
                     AND binding.wallet_route_id = route.id
                    WHERE assignment.assignment_state = 'active'
                      AND principal.enabled
                      AND principal.principal_state = 'enabled'
                      AND route.route_state NOT IN ('disabled', 'retired')
                      AND merchant.a6_user_id IS NOT NULL
                      AND coalesce(merchant.session_ref, '') <> ''
                      AND merchant.status = 'supplier_submitted'
                    ORDER BY assignment.merchant_id, route.id
                    """
                )
                rows = _rows(cursor)
        finally:
            _close(connection)

        targets: list[TreasuryAccountTarget] = []
        for row in rows:
            principal_key = str(row["inventory_principal_key"])
            route_key = str(row["inventory_route_key"])
            principal = principals.get(principal_key)
            if principal is None:
                raise TreasuryAccountError("treasury_account_principal_not_in_inventory")
            route = next((item for item in principal.routes if item.route_key == route_key), None)
            if route is None:
                raise TreasuryAccountError("treasury_account_route_not_in_inventory")
            remote_id = row.get("a6_withdraw_account_id")
            targets.append(
                TreasuryAccountTarget(
                    merchant=TreasuryAccountMerchant(
                        merchant_id=int(row["merchant_id"]),
                        a6_user_id=int(row["a6_user_id"]),
                        session_ref=str(row["session_ref"]),
                    ),
                    wallet_route_id=int(row["wallet_route_id"]),
                    principal_key=principal_key,
                    route_key=route_key,
                    network=route.network,
                    protocol=route.protocol,
                    address=principal.address,
                    current_remote_account_id=(int(remote_id) if remote_id is not None else None),
                )
            )
        return tuple(targets)

    def record_observations(
        self,
        observations: Sequence[TreasuryAccountObservation],
    ) -> int:
        unique: dict[tuple[int, int], TreasuryAccountObservation] = {}
        for observation in observations:
            key = (observation.merchant_id, observation.wallet_route_id)
            if key in unique:
                raise TreasuryAccountError("treasury_account_observation_duplicate")
            if (
                min(observation.merchant_id, observation.wallet_route_id,
                    observation.remote_account_id) < 1
                or len(observation.source_hash) != 64
                or observation.observed_at.tzinfo is None
            ):
                raise TreasuryAccountError("treasury_account_observation_invalid")
            unique[key] = observation
        if not unique:
            return 0

        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                for merchant_id in sorted({item.merchant_id for item in unique.values()}):
                    cursor.execute(
                        "SELECT pg_advisory_xact_lock(%s, %s)",
                        (FUND_LOCK_NAMESPACE, merchant_id),
                    )
                for observation in unique.values():
                    cursor.execute(
                        """
                        INSERT INTO a6_trade.merchant_treasury_route_bindings (
                            merchant_id, wallet_route_id, a6_withdraw_account_id,
                            binding_state, remote_account_source_hash, last_readback_at
                        )
                        SELECT %s, route.id, %s, %s, %s, %s
                        FROM a6_trade.treasury_wallet_routes route
                        JOIN a6_trade.merchant_treasury_assignments assignment
                          ON assignment.wallet_principal_id = route.wallet_principal_id
                         AND assignment.merchant_id = %s
                         AND assignment.assignment_state = 'active'
                        WHERE route.id = %s
                          AND route.route_state NOT IN ('disabled', 'retired')
                        ON CONFLICT (merchant_id, wallet_route_id) DO UPDATE SET
                            a6_withdraw_account_id = EXCLUDED.a6_withdraw_account_id,
                            binding_state = EXCLUDED.binding_state,
                            remote_account_source_hash = EXCLUDED.remote_account_source_hash,
                            last_readback_at = EXCLUDED.last_readback_at,
                            updated_at = now()
                        RETURNING id
                        """,
                        (
                            observation.merchant_id,
                            observation.remote_account_id,
                            "healthy" if observation.healthy else "degraded",
                            observation.source_hash,
                            observation.observed_at,
                            observation.merchant_id,
                            observation.wallet_route_id,
                        ),
                    )
                    if _row(cursor) is None:
                        raise TreasuryAccountError("treasury_account_assignment_changed")
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            _close(connection)
        return len(unique)


class TreasuryAccountReconciler:
    def __init__(
        self,
        store: PostgresTreasuryAccountStore,
        client_factory: Callable[[TreasuryAccountMerchant], TreasuryAccountClient],
        *,
        max_workers: int = 8,
    ) -> None:
        if not callable(client_factory) or not 1 <= max_workers <= 32:
            raise TreasuryAccountError("treasury_account_reconciler_invalid")
        self.store = store
        self.client_factory = client_factory
        self.max_workers = max_workers

    def run_once(
        self,
        inventory: WalletInventory,
        *,
        apply: bool = False,
        max_creates: int = 24,
    ) -> dict[str, Any]:
        if not isinstance(apply, bool) or not 0 <= max_creates <= 1_000:
            raise TreasuryAccountError("treasury_account_reconcile_input_invalid")
        targets = self.store.list_targets(inventory)
        grouped = _group_targets(targets)
        reads: dict[int, tuple[TreasuryAccountClient, tuple[_RemoteAccount, ...]]] = {}
        read_errors: dict[int, str] = {}
        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            futures = {
                executor.submit(self._read_accounts, merchant_targets[0].merchant): merchant_id
                for merchant_id, merchant_targets in grouped.items()
            }
            for future in as_completed(futures):
                merchant_id = futures[future]
                try:
                    reads[merchant_id] = future.result()
                except Exception as exc:
                    read_errors[merchant_id] = _error_code(exc)

        matches: dict[tuple[int, int], _RemoteAccount] = {}
        conflicts: set[tuple[int, int]] = set()
        missing: list[TreasuryAccountTarget] = []
        for merchant_id, merchant_targets in grouped.items():
            if merchant_id in read_errors:
                continue
            accounts = reads[merchant_id][1]
            for target in merchant_targets:
                candidates = _matches(accounts, target)
                key = (merchant_id, target.wallet_route_id)
                if len(candidates) == 1:
                    matches[key] = candidates[0]
                elif len(candidates) > 1:
                    conflicts.add(key)
                else:
                    missing.append(target)

        selected = tuple(missing[:max_creates]) if apply else ()
        selected_by_merchant = _group_targets(selected)
        create_errors: dict[tuple[int, int], str] = {}
        create_requests = 0
        if selected_by_merchant:
            with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
                futures = {
                    executor.submit(
                        self._create_and_readback,
                        reads[merchant_id][0],
                        merchant_targets,
                    ): merchant_id
                    for merchant_id, merchant_targets in selected_by_merchant.items()
                }
                for future in as_completed(futures):
                    merchant_id = futures[future]
                    merchant_targets = selected_by_merchant[merchant_id]
                    create_requests += len(merchant_targets)
                    try:
                        accounts, errors = future.result()
                    except Exception as exc:
                        error = _error_code(exc)
                        accounts = reads[merchant_id][1]
                        errors = {
                            target.wallet_route_id: error for target in merchant_targets
                        }
                    reads[merchant_id] = (reads[merchant_id][0], accounts)
                    for route_id, error in errors.items():
                        create_errors[(merchant_id, route_id)] = error
                    for target in grouped[merchant_id]:
                        key = (merchant_id, target.wallet_route_id)
                        candidates = _matches(accounts, target)
                        if len(candidates) == 1:
                            matches[key] = candidates[0]
                            conflicts.discard(key)
                            create_errors.pop(key, None)
                        elif len(candidates) > 1:
                            conflicts.add(key)
                            matches.pop(key, None)

        observed_at = datetime.now(timezone.utc)
        observations: list[TreasuryAccountObservation] = []
        for target in targets:
            key = (target.merchant.merchant_id, target.wallet_route_id)
            account = matches.get(key)
            if account is not None:
                observations.append(
                    TreasuryAccountObservation(
                        merchant_id=target.merchant.merchant_id,
                        wallet_route_id=target.wallet_route_id,
                        remote_account_id=account.account_id,
                        source_hash=account.source_hash,
                        observed_at=observed_at,
                        healthy=True,
                    )
                )
            elif (
                target.current_remote_account_id is not None
                and target.merchant.merchant_id not in read_errors
            ):
                observations.append(
                    TreasuryAccountObservation(
                        merchant_id=target.merchant.merchant_id,
                        wallet_route_id=target.wallet_route_id,
                        remote_account_id=target.current_remote_account_id,
                        source_hash=_sha256(
                            {
                                "merchant_id": target.merchant.merchant_id,
                                "wallet_route_id": target.wallet_route_id,
                                "remote_account_id": target.current_remote_account_id,
                                "readback": "missing",
                            }
                        ),
                        observed_at=observed_at,
                        healthy=False,
                    )
                )
        recorded = self.store.record_observations(observations)
        selected_keys = {
            (target.merchant.merchant_id, target.wallet_route_id) for target in selected
        }
        return {
            "success": not read_errors and not conflicts and not create_errors,
            "mode": "apply" if apply else "readback-only",
            "merchant_count": len(grouped),
            "target_count": len(targets),
            "healthy_count": len(matches),
            "missing_count": sum(
                (target.merchant.merchant_id, target.wallet_route_id) not in matches
                for target in targets
            ),
            "degraded_count": sum(not item.healthy for item in observations),
            "read_error_count": len(read_errors),
            "conflict_count": len(conflicts),
            "create_candidate_count": len(missing),
            "create_request_count": create_requests,
            "create_deferred_count": max(0, len(missing) - len(selected_keys)),
            "create_error_count": len(create_errors),
            "create_error_classes": dict(
                sorted(Counter(create_errors.values()).items())
            ),
            "recorded_count": recorded,
            "remote_write_count": create_requests,
            "read_errors": dict(sorted(read_errors.items())),
        }

    def _read_accounts(
        self,
        merchant: TreasuryAccountMerchant,
    ) -> tuple[TreasuryAccountClient, tuple[_RemoteAccount, ...]]:
        client = self.client_factory(merchant)
        rows = client.supplier_withdraw_accounts(merchant.a6_user_id)
        return client, _parse_accounts(rows)

    @staticmethod
    def _create_and_readback(
        client: TreasuryAccountClient,
        targets: Sequence[TreasuryAccountTarget],
    ) -> tuple[tuple[_RemoteAccount, ...], dict[int, str]]:
        errors: dict[int, str] = {}
        merchant = targets[0].merchant
        for target in targets:
            try:
                client.create_supplier_withdraw_account(
                    merchant.a6_user_id,
                    network=_create_network(target),
                    address=target.address,
                    remark=(
                        f"a6treasury-m{merchant.merchant_id}-"
                        f"{target.route_key.removeprefix('usdt-')}"
                    )[:64],
                    is_default=False,
                )
            except Exception as exc:
                errors[target.wallet_route_id] = _error_code(exc)
        accounts = _parse_accounts(client.supplier_withdraw_accounts(merchant.a6_user_id))
        return accounts, errors


def _group_targets(
    targets: Sequence[TreasuryAccountTarget],
) -> dict[int, tuple[TreasuryAccountTarget, ...]]:
    grouped: dict[int, list[TreasuryAccountTarget]] = {}
    for target in targets:
        grouped.setdefault(target.merchant.merchant_id, []).append(target)
    return {key: tuple(value) for key, value in sorted(grouped.items())}


def _parse_accounts(rows: Sequence[Mapping[str, Any]]) -> tuple[_RemoteAccount, ...]:
    accounts: list[_RemoteAccount] = []
    for row in rows:
        raw_info = row.get("account_info")
        try:
            info = json.loads(raw_info) if isinstance(raw_info, str) else raw_info
        except json.JSONDecodeError:
            raise TreasuryAccountError("treasury_account_readback_invalid") from None
        if not isinstance(info, Mapping) or not isinstance(info.get("fields"), Mapping):
            raise TreasuryAccountError("treasury_account_readback_invalid")
        channel = str(row.get("channel") or info.get("channel") or "").strip().casefold()
        if channel != "usdt":
            continue
        fields = info["fields"]
        account_id = _positive_int(row.get("id"))
        protocol = _canonical_protocol(fields.get("network"))
        address = _canonical_address(fields.get("address"))
        accounts.append(
            _RemoteAccount(
                account_id=account_id,
                protocol=protocol,
                address=address,
                source_hash=_sha256(
                    {
                        "account_id": account_id,
                        "protocol": protocol,
                        "address": address,
                        "account": dict(row),
                    }
                ),
            )
        )
    return tuple(accounts)


def _matches(
    accounts: Sequence[_RemoteAccount],
    target: TreasuryAccountTarget,
) -> tuple[_RemoteAccount, ...]:
    protocol = _canonical_protocol(target.protocol)
    address = _canonical_address(target.address)
    return tuple(
        account
        for account in accounts
        if account.protocol == protocol and account.address == address
    )


def _create_network(target: TreasuryAccountTarget) -> str:
    protocol = _canonical_protocol(target.protocol)
    return "BSC" if protocol == "BEP20" else target.protocol


def _canonical_protocol(value: Any) -> str:
    normalized = str(value or "").strip().upper()
    aliases = {
        "BSC": "BEP20",
        "BEP20": "BEP20",
        "ETH": "ERC20",
        "ERC20": "ERC20",
        "POL": "POLYGON",
        "POLYGON": "POLYGON",
        "TRX": "TRC20",
        "TRC20": "TRC20",
    }
    result = aliases.get(normalized)
    if result is None:
        raise TreasuryAccountError("treasury_account_protocol_invalid")
    return result


def _canonical_address(value: Any) -> str:
    address = str(value or "").strip()
    if address.startswith(("0x", "0X")) and len(address) == 42:
        return address.lower()
    if len(address) >= 20:
        return address
    raise TreasuryAccountError("treasury_account_address_invalid")


def _positive_int(value: Any) -> int:
    if isinstance(value, bool):
        raise TreasuryAccountError("treasury_account_id_invalid")
    try:
        result = int(value)
    except (TypeError, ValueError):
        raise TreasuryAccountError("treasury_account_id_invalid") from None
    if result < 1:
        raise TreasuryAccountError("treasury_account_id_invalid")
    return result


def _sha256(document: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            document,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode()
    ).hexdigest()


def _error_code(exc: Exception) -> str:
    return str(
        getattr(exc, "code", "") or str(exc).strip() or exc.__class__.__name__
    )[:128]


def _rows(cursor: Any) -> list[dict[str, Any]]:
    return [dict(row) for row in cursor.fetchall()]


def _row(cursor: Any) -> dict[str, Any] | None:
    row = cursor.fetchone()
    return dict(row) if row is not None else None


def _close(connection: Any) -> None:
    close = getattr(connection, "close", None)
    if callable(close):
        close()
