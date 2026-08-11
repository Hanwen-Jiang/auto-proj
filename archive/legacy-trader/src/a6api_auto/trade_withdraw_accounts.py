from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from .models import SessionRecord
from .storage import SessionStore

_NETWORK = re.compile(r"^[A-Z0-9][A-Z0-9_-]{1,31}$")
_ADDRESS = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:._-]{7,255}$")
_BOUND_STATES = frozenset({"bound", "existing_unmanaged"})


class WithdrawAccountError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class WithdrawPoolEntry:
    network: str
    address: str = field(repr=False)
    address_sha256: str
    address_masked: str
    secret_ref: str


@dataclass(frozen=True)
class WithdrawMerchant:
    merchant_id: int
    a6_user_id: int
    supplier_id: int
    session_ref: str
    status: str


@dataclass(frozen=True)
class WithdrawBinding:
    binding_id: int
    merchant_id: int
    network: str
    address_sha256: str
    address_masked: str
    address_secret_ref: str
    stable_remark: str
    a6_withdraw_account_id: int | None
    source: str
    state: str


@dataclass(frozen=True)
class ExistingWithdrawAccount:
    account_id: int
    network: str
    address: str = field(repr=False)
    address_sha256: str
    address_masked: str
    remark: str
    is_default: bool


@dataclass(frozen=True)
class WithdrawDecision:
    merchant_id: int
    action: str
    applied: bool
    state: str
    network: str
    address_masked: str
    address_sha256: str
    a6_withdraw_account_id: int | None = None
    error_class: str | None = None

    def public(self) -> dict[str, Any]:
        return {
            "merchant_id": self.merchant_id,
            "action": self.action,
            "applied": self.applied,
            "state": self.state,
            "network": self.network,
            "address_masked": self.address_masked,
            "address_fingerprint": self.address_sha256[:12],
            "a6_withdraw_account_id": self.a6_withdraw_account_id,
            "error_class": self.error_class,
        }


@dataclass(frozen=True)
class WithdrawBatchResult:
    applied: bool
    decisions: tuple[WithdrawDecision, ...]
    pool_entries: int = 0
    existing_remote_count: int = 0
    needs_creation_count: int = 0
    available_pool_count: int = 0
    shortfall: int = 0

    def summary(self) -> dict[str, Any]:
        errors = sum(item.error_class is not None for item in self.decisions)
        return {
            "success": errors == 0,
            "mode": "applied" if self.applied else "dry-run",
            "merchant_count": len(self.decisions),
            "error_count": errors,
            "pool_entries": self.pool_entries,
            "existing_remote_count": self.existing_remote_count,
            "needs_creation_count": self.needs_creation_count,
            "available_pool_count": self.available_pool_count,
            "shortfall": self.shortfall,
            "withdrawal_submitted": False,
            "decisions": [item.public() for item in self.decisions],
        }


class WithdrawAccountStore(Protocol):
    def list_merchants(self, merchant_ids: Sequence[int]) -> tuple[WithdrawMerchant, ...]: ...

    def list_bindings(self) -> tuple[WithdrawBinding, ...]: ...

    def get_binding(self, merchant_id: int) -> WithdrawBinding | None: ...

    def reserve_pool_entry(
        self,
        merchant: WithdrawMerchant,
        candidates: Sequence[WithdrawPoolEntry],
        *,
        config_version: str,
        requested_by: str,
    ) -> WithdrawBinding: ...

    def register_existing(
        self,
        merchant: WithdrawMerchant,
        account: ExistingWithdrawAccount,
        *,
        config_version: str,
        requested_by: str,
    ) -> WithdrawBinding: ...

    def claim_create(self, binding: WithdrawBinding) -> WithdrawBinding | None: ...

    def mark_create_unknown(
        self,
        binding: WithdrawBinding,
        error_class: str,
    ) -> WithdrawBinding: ...

    def mark_bound(self, binding: WithdrawBinding, account_id: int) -> WithdrawBinding: ...

    def mark_error(self, binding: WithdrawBinding, error_class: str) -> None: ...


class A6WithdrawClient(Protocol):
    def supplier_withdraw_accounts(self, user_id: int) -> list[dict[str, Any]]: ...

    def create_supplier_withdraw_account(
        self,
        user_id: int,
        *,
        network: str,
        address: str,
        remark: str,
        is_default: bool = True,
    ) -> dict[str, Any]: ...


ClientFactory = Callable[[WithdrawMerchant, SessionRecord], A6WithdrawClient]


def load_withdraw_pool(path: Path) -> tuple[WithdrawPoolEntry, ...]:
    resolved = path.expanduser().absolute()
    descriptor = -1
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(resolved, flags)
        info = os.fstat(descriptor)
    except OSError as exc:
        raise WithdrawAccountError("withdraw_pool_unreadable") from exc
    try:
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
            raise WithdrawAccountError("withdraw_pool_permissions")
        with os.fdopen(descriptor, encoding="utf-8") as handle:
            descriptor = -1
            document = json.load(handle)
    except json.JSONDecodeError as exc:
        raise WithdrawAccountError("withdraw_pool_invalid_json") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if not isinstance(document, list):
        raise WithdrawAccountError("withdraw_pool_shape_invalid")
    entries: list[WithdrawPoolEntry] = []
    seen_addresses: set[str] = set()
    seen_pairs: set[tuple[str, str]] = set()
    for raw in document:
        if not isinstance(raw, Mapping) or set(raw) != {"network", "address"}:
            raise WithdrawAccountError("withdraw_pool_entry_invalid")
        network = str(raw["network"]).strip().upper()
        address = str(raw["address"]).strip()
        if network != raw["network"] or not _NETWORK.fullmatch(network):
            raise WithdrawAccountError("withdraw_pool_network_invalid")
        if address != raw["address"] or not _ADDRESS.fullmatch(address):
            raise WithdrawAccountError("withdraw_pool_address_invalid")
        address_sha256 = hashlib.sha256(address.encode()).hexdigest()
        pair = (network, address)
        if address_sha256 in seen_addresses or pair in seen_pairs:
            raise WithdrawAccountError("withdraw_pool_duplicate")
        seen_addresses.add(address_sha256)
        seen_pairs.add(pair)
        entries.append(
            WithdrawPoolEntry(
                network=network,
                address=address,
                address_sha256=address_sha256,
                address_masked=mask_address(address),
                secret_ref=f"withdraw-pool:sha256:{address_sha256}",
            )
        )
    return tuple(entries)


def mask_address(address: str) -> str:
    if len(address) <= 10:
        return "***"
    return f"{address[:6]}...{address[-4:]}"


class WithdrawAccountService:
    def __init__(
        self,
        store: WithdrawAccountStore,
        pool: Sequence[WithdrawPoolEntry],
        client_factory: ClientFactory,
        *,
        session_paths_by_user_id: Mapping[int, Path] | None = None,
        config_version: str = "phase1-withdraw-account-v1",
        requested_by: str = "a6-trader",
    ) -> None:
        self.store = store
        self.pool = tuple(pool)
        self.client_factory = client_factory
        self.require_session_mapping = session_paths_by_user_id is not None
        self.session_paths_by_user_id = dict(session_paths_by_user_id or {})
        self.config_version = config_version
        self.requested_by = requested_by

    def execute(
        self,
        merchant_ids: Iterable[int] = (),
        *,
        apply: bool = False,
    ) -> WithdrawBatchResult:
        if not isinstance(apply, bool):
            raise WithdrawAccountError("withdraw_apply_invalid")
        requested_ids = tuple(dict.fromkeys(int(value) for value in merchant_ids))
        if any(value < 1 for value in requested_ids):
            raise WithdrawAccountError("withdraw_merchant_filter_invalid")
        all_merchants = self.store.list_merchants(())
        merchants_by_id = {item.merchant_id: item for item in all_merchants}
        if requested_ids and not set(requested_ids) <= set(merchants_by_id):
            raise WithdrawAccountError("withdraw_merchant_filter_missing")
        merchants = (
            tuple(merchants_by_id[item] for item in requested_ids)
            if requested_ids
            else all_merchants
        )
        bindings = {item.merchant_id: item for item in self.store.list_bindings()}
        used_owners = {item.address_sha256: item.merchant_id for item in bindings.values()}
        clients: dict[int, A6WithdrawClient] = {}
        remote_by_merchant: dict[int, tuple[ExistingWithdrawAccount, ...]] = {}
        inventory_errors: dict[int, str] = {}
        for merchant in all_merchants:
            try:
                record = self._load_session(merchant)
                client = self.client_factory(merchant, record)
                remote = _parse_accounts(
                    client.supplier_withdraw_accounts(merchant.a6_user_id)
                )
                for account in remote:
                    owner = used_owners.get(account.address_sha256)
                    if owner is not None and owner != merchant.merchant_id:
                        raise WithdrawAccountError("withdraw_existing_address_conflict")
                    used_owners[account.address_sha256] = merchant.merchant_id
                clients[merchant.merchant_id] = client
                remote_by_merchant[merchant.merchant_id] = remote
            except Exception as exc:
                inventory_errors[merchant.merchant_id] = _error_code(exc)

        if inventory_errors:
            decisions = tuple(
                WithdrawDecision(
                    merchant_id=merchant.merchant_id,
                    action="error",
                    applied=False,
                    state=bindings.get(merchant.merchant_id).state
                    if merchant.merchant_id in bindings
                    else "error",
                    network="",
                    address_masked="",
                    address_sha256="",
                    error_class=inventory_errors.get(
                        merchant.merchant_id,
                        "withdraw_remote_inventory_incomplete",
                    ),
                )
                for merchant in merchants
            )
            return WithdrawBatchResult(
                applied=apply,
                decisions=decisions,
                pool_entries=len(self.pool),
                shortfall=len(merchants),
            )

        initially_used_hashes = set(used_owners)
        remote_existing_hashes: set[str] = set()
        existing_remote_count = 0
        needs_creation_count = 0
        decisions: list[WithdrawDecision] = []
        for merchant in merchants:
            binding = bindings.get(merchant.merchant_id)
            had_binding = binding is not None
            try:
                decision, binding = self._execute_one(
                    merchant,
                    binding,
                    used_owners,
                    clients[merchant.merchant_id],
                    remote_by_merchant[merchant.merchant_id],
                    apply=apply,
                )
            except Exception as exc:
                code = _error_code(exc)
                if apply:
                    current_binding = self.store.get_binding(merchant.merchant_id)
                    if current_binding is not None:
                        binding = current_binding
                if binding is not None and apply and binding.state in {"reserved", "error"}:
                    self.store.mark_error(binding, code)
                decision = WithdrawDecision(
                    merchant_id=merchant.merchant_id,
                    action="error",
                    applied=False,
                    state=binding.state if binding is not None else "error",
                    network=binding.network if binding else "",
                    address_masked=binding.address_masked if binding else "",
                    address_sha256=binding.address_sha256 if binding else "",
                    a6_withdraw_account_id=(
                        binding.a6_withdraw_account_id if binding else None
                    ),
                    error_class=code,
                )
            if binding is not None:
                bindings[merchant.merchant_id] = binding
                used_owners[binding.address_sha256] = merchant.merchant_id
            if decision.address_sha256:
                used_owners[decision.address_sha256] = merchant.merchant_id
            if decision.action == "register_existing":
                existing_remote_count += 1
                remote_existing_hashes.add(decision.address_sha256)
            elif not had_binding and (
                decision.action == "create"
                or decision.error_class == "withdraw_pool_exhausted"
                or (binding is not None and binding.source == "pool")
            ):
                needs_creation_count += 1
            decisions.append(decision)
        unavailable_at_start = initially_used_hashes | remote_existing_hashes
        available_pool_count = sum(
            item.address_sha256 not in unavailable_at_start for item in self.pool
        )
        return WithdrawBatchResult(
            applied=apply,
            decisions=tuple(decisions),
            pool_entries=len(self.pool),
            existing_remote_count=existing_remote_count,
            needs_creation_count=needs_creation_count,
            available_pool_count=available_pool_count,
            shortfall=max(needs_creation_count - available_pool_count, 0),
        )

    def _execute_one(
        self,
        merchant: WithdrawMerchant,
        binding: WithdrawBinding | None,
        used_owners: dict[str, int],
        client: A6WithdrawClient,
        remote: tuple[ExistingWithdrawAccount, ...],
        *,
        apply: bool,
    ) -> tuple[WithdrawDecision, WithdrawBinding | None]:
        for account in remote:
            owner = used_owners.get(account.address_sha256)
            if owner is not None and owner != merchant.merchant_id:
                raise WithdrawAccountError("withdraw_existing_address_conflict")
            used_owners[account.address_sha256] = merchant.merchant_id
        if remote:
            matched = _choose_existing(remote, binding)
            if binding is not None and (
                matched.address_sha256 != binding.address_sha256
                or matched.network != binding.network
            ):
                raise WithdrawAccountError("withdraw_existing_account_conflict")
            if not apply:
                if binding is None:
                    return _decision_existing(merchant, matched, False), binding
                action = (
                    "noop"
                    if binding.state in _BOUND_STATES
                    and binding.a6_withdraw_account_id == matched.account_id
                    else "reconcile"
                )
                return _decision_remote_binding(merchant, binding, matched, action, False), binding
            if binding is None:
                binding = self.store.register_existing(
                    merchant,
                    matched,
                    config_version=self.config_version,
                    requested_by=self.requested_by,
                )
                return _decision_existing(merchant, matched, True), binding
            action = (
                "noop"
                if binding.state in _BOUND_STATES
                and binding.a6_withdraw_account_id == matched.account_id
                else "reconcile"
            )
            if action == "reconcile":
                binding = self.store.mark_bound(binding, matched.account_id)
            return _decision_remote_binding(merchant, binding, matched, action, True), binding

        if binding is not None and binding.state in _BOUND_STATES:
            raise WithdrawAccountError("withdraw_bound_account_readback_missing")

        if binding is not None and binding.state in {"creating", "create_unknown"}:
            if not apply:
                return (
                    WithdrawDecision(
                        merchant_id=merchant.merchant_id,
                        action="reconcile_pending",
                        applied=False,
                        state=binding.state,
                        network=binding.network,
                        address_masked=binding.address_masked,
                        address_sha256=binding.address_sha256,
                        a6_withdraw_account_id=binding.a6_withdraw_account_id,
                    ),
                    binding,
                )
            raise WithdrawAccountError("withdraw_create_reconciliation_pending")

        entry = self._entry_for_binding(binding) if binding else None
        if entry is None:
            entry = next(
                (item for item in self.pool if item.address_sha256 not in used_owners),
                None,
            )
        if entry is None:
            raise WithdrawAccountError("withdraw_pool_exhausted")
        remark = _stable_remark(merchant.merchant_id, entry.address_sha256)
        if not apply:
            return (
                WithdrawDecision(
                    merchant_id=merchant.merchant_id,
                    action="create",
                    applied=False,
                    state="planned",
                    network=entry.network,
                    address_masked=entry.address_masked,
                    address_sha256=entry.address_sha256,
                ),
                binding or _virtual_binding(merchant, entry, remark),
            )

        binding = self.store.reserve_pool_entry(
            merchant,
            self._ordered_candidates(entry),
            config_version=self.config_version,
            requested_by=self.requested_by,
        )
        entry = self._entry_for_binding(binding)
        if entry is None:
            raise WithdrawAccountError("withdraw_reserved_secret_missing")
        remote = _parse_accounts(client.supplier_withdraw_accounts(merchant.a6_user_id))
        for account in remote:
            owner = used_owners.get(account.address_sha256)
            if owner is not None and owner != merchant.merchant_id:
                raise WithdrawAccountError("withdraw_existing_address_conflict")
            used_owners[account.address_sha256] = merchant.merchant_id
        if remote:
            matched = _match_stable_remark(remote, binding.stable_remark)
            if matched is None or (
                matched.address_sha256 != binding.address_sha256
                or matched.network != binding.network
            ):
                raise WithdrawAccountError("withdraw_existing_account_conflict")
            binding = self.store.mark_bound(binding, matched.account_id)
            return _decision_bound(merchant, binding, "reconcile"), binding

        claimed = self.store.claim_create(binding)
        if claimed is None:
            raise WithdrawAccountError("withdraw_create_already_claimed")
        binding = claimed

        try:
            client.create_supplier_withdraw_account(
                merchant.a6_user_id,
                network=entry.network,
                address=entry.address,
                remark=binding.stable_remark,
                is_default=True,
            )
        except Exception as exc:
            code = _error_code(exc)
            binding = self.store.mark_create_unknown(binding, code)
            raise WithdrawAccountError("withdraw_create_result_unknown") from None
        remote = _parse_accounts(client.supplier_withdraw_accounts(merchant.a6_user_id))
        matched = _match_stable_remark(remote, binding.stable_remark)
        if matched is None:
            binding = self.store.mark_create_unknown(
                binding,
                "withdraw_create_readback_missing",
            )
            raise WithdrawAccountError("withdraw_create_reconciliation_pending")
        if (
            matched.address_sha256 != binding.address_sha256
            or matched.network != binding.network
        ):
            raise WithdrawAccountError("withdraw_create_readback_mismatch")
        binding = self.store.mark_bound(binding, matched.account_id)
        return _decision_bound(merchant, binding, "create"), binding

    def _load_session(self, merchant: WithdrawMerchant) -> SessionRecord:
        if (
            self.require_session_mapping
            and merchant.a6_user_id not in self.session_paths_by_user_id
        ):
            raise WithdrawAccountError("withdraw_session_missing")
        path = self.session_paths_by_user_id.get(
            merchant.a6_user_id,
            Path(merchant.session_ref),
        )
        record = SessionStore(path).load()
        if (
            record.status != "supplier_submitted"
            or record.user_id != merchant.a6_user_id
            or record.supplier_id != merchant.supplier_id
        ):
            raise WithdrawAccountError("withdraw_session_mismatch")
        return record

    def _entry_for_binding(self, binding: WithdrawBinding | None) -> WithdrawPoolEntry | None:
        if binding is None or binding.source != "pool":
            return None
        return next(
            (item for item in self.pool if item.address_sha256 == binding.address_sha256),
            None,
        )

    def _ordered_candidates(
        self,
        preferred: WithdrawPoolEntry,
    ) -> tuple[WithdrawPoolEntry, ...]:
        return (preferred, *(item for item in self.pool if item != preferred))


class PostgresWithdrawAccountStore:
    def __init__(self, connection_factory: Callable[[], Any]) -> None:
        self.connection_factory = connection_factory

    def list_merchants(self, merchant_ids: Sequence[int]) -> tuple[WithdrawMerchant, ...]:
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                params: tuple[Any, ...] = ()
                filter_sql = ""
                if merchant_ids:
                    filter_sql = " AND id = ANY(%s)"
                    params = (list(merchant_ids),)
                cursor.execute(
                    """
                    SELECT id AS merchant_id, a6_user_id, supplier_id, session_ref, status
                    FROM a6_trade.a6_merchants
                    WHERE status = 'supplier_submitted'
                      AND a6_user_id IS NOT NULL AND supplier_id IS NOT NULL
                    """
                    + filter_sql
                    + " ORDER BY id",
                    params,
                )
                rows = _rows(cursor)
            return tuple(_merchant(row) for row in rows)
        finally:
            _close(connection)

    def claim_create(self, binding: WithdrawBinding) -> WithdrawBinding | None:
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE a6_trade.a6_withdraw_account_bindings
                    SET state = 'creating', last_error_class = NULL, updated_at = now()
                    WHERE id = %s AND stable_remark = %s AND state IN ('reserved', 'error')
                    RETURNING id AS binding_id, merchant_id, network, address_sha256,
                              address_masked, address_secret_ref, stable_remark,
                              a6_withdraw_account_id, source, state
                    """,
                    (binding.binding_id, binding.stable_remark),
                )
                row = _row(cursor)
            connection.commit()
            return _binding(row) if row is not None else None
        except Exception:
            connection.rollback()
            raise
        finally:
            _close(connection)

    def mark_create_unknown(
        self,
        binding: WithdrawBinding,
        error_class: str,
    ) -> WithdrawBinding:
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE a6_trade.a6_withdraw_account_bindings
                    SET state = 'create_unknown', last_error_class = %s, updated_at = now()
                    WHERE id = %s AND stable_remark = %s AND state = 'creating'
                    RETURNING id AS binding_id, merchant_id, network, address_sha256,
                              address_masked, address_secret_ref, stable_remark,
                              a6_withdraw_account_id, source, state
                    """,
                    (error_class, binding.binding_id, binding.stable_remark),
                )
                row = _row(cursor)
                if row is None:
                    raise WithdrawAccountError("withdraw_binding_state_conflict")
                cursor.execute(
                    """
                    UPDATE a6_trade.trade_control_actions
                    SET status = 'started', error_class = %s, updated_at = now()
                    WHERE idempotency_key = %s AND status <> 'completed'
                    """,
                    (error_class, _action_key(binding.stable_remark)),
                )
            connection.commit()
            return _binding(row)
        except Exception:
            connection.rollback()
            raise
        finally:
            _close(connection)

    def list_bindings(self) -> tuple[WithdrawBinding, ...]:
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT id AS binding_id, merchant_id, network, address_sha256,
                           address_masked, address_secret_ref, stable_remark,
                           a6_withdraw_account_id, source, state
                    FROM a6_trade.a6_withdraw_account_bindings
                    ORDER BY merchant_id
                    """
                )
                rows = _rows(cursor)
            return tuple(_binding(row) for row in rows)
        finally:
            _close(connection)

    def get_binding(self, merchant_id: int) -> WithdrawBinding | None:
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT id AS binding_id, merchant_id, network, address_sha256,
                           address_masked, address_secret_ref, stable_remark,
                           a6_withdraw_account_id, source, state
                    FROM a6_trade.a6_withdraw_account_bindings
                    WHERE merchant_id = %s
                    """,
                    (merchant_id,),
                )
                row = _row(cursor)
            return _binding(row) if row is not None else None
        finally:
            _close(connection)

    def reserve_pool_entry(
        self,
        merchant: WithdrawMerchant,
        candidates: Sequence[WithdrawPoolEntry],
        *,
        config_version: str,
        requested_by: str,
    ) -> WithdrawBinding:
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT id FROM a6_trade.a6_merchants WHERE id = %s FOR UPDATE",
                    (merchant.merchant_id,),
                )
                if cursor.fetchone() is None:
                    raise WithdrawAccountError("withdraw_merchant_missing")
                cursor.execute(
                    """
                    SELECT id AS binding_id, merchant_id, network, address_sha256,
                           address_masked, address_secret_ref, stable_remark,
                           a6_withdraw_account_id, source, state
                    FROM a6_trade.a6_withdraw_account_bindings
                    WHERE merchant_id = %s
                    """,
                    (merchant.merchant_id,),
                )
                existing = _row(cursor)
                if existing is not None:
                    connection.commit()
                    return _binding(existing)
                reserved: dict[str, Any] | None = None
                for entry in candidates:
                    remark = _stable_remark(merchant.merchant_id, entry.address_sha256)
                    cursor.execute(
                        """
                        INSERT INTO a6_trade.a6_withdraw_account_bindings (
                            merchant_id, network, address_sha256, address_masked,
                            address_secret_ref, stable_remark, source, state
                        ) VALUES (%s, %s, %s, %s, %s, %s, 'pool', 'reserved')
                        ON CONFLICT (address_sha256) DO NOTHING
                        RETURNING id AS binding_id, merchant_id, network, address_sha256,
                                  address_masked, address_secret_ref, stable_remark,
                                  a6_withdraw_account_id, source, state
                        """,
                        (
                            merchant.merchant_id,
                            entry.network,
                            entry.address_sha256,
                            entry.address_masked,
                            entry.secret_ref,
                            remark,
                        ),
                    )
                    reserved = _row(cursor)
                    if reserved is not None:
                        break
                if reserved is None:
                    raise WithdrawAccountError("withdraw_pool_exhausted")
                _insert_action(
                    cursor,
                    reserved,
                    config_version=config_version,
                    requested_by=requested_by,
                    status="started",
                )
            connection.commit()
            return _binding(reserved)
        except Exception:
            connection.rollback()
            raise
        finally:
            _close(connection)

    def register_existing(
        self,
        merchant: WithdrawMerchant,
        account: ExistingWithdrawAccount,
        *,
        config_version: str,
        requested_by: str,
    ) -> WithdrawBinding:
        connection = self.connection_factory()
        remark = f"a6trader-existing-m{merchant.merchant_id}-a{account.account_id}"
        secret_ref = f"a6api:withdraw-account:{account.account_id}"
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO a6_trade.a6_withdraw_account_bindings (
                        merchant_id, network, address_sha256, address_masked,
                        address_secret_ref, stable_remark, a6_withdraw_account_id,
                        source, state, reconciled_at
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s, %s,
                        'existing', 'existing_unmanaged', now()
                    )
                    ON CONFLICT (merchant_id) DO NOTHING
                    RETURNING id AS binding_id, merchant_id, network, address_sha256,
                              address_masked, address_secret_ref, stable_remark,
                              a6_withdraw_account_id, source, state
                    """,
                    (
                        merchant.merchant_id,
                        account.network,
                        account.address_sha256,
                        account.address_masked,
                        secret_ref,
                        remark,
                        account.account_id,
                    ),
                )
                row = _row(cursor)
                if row is None:
                    cursor.execute(
                        """
                        SELECT id AS binding_id, merchant_id, network, address_sha256,
                               address_masked, address_secret_ref, stable_remark,
                               a6_withdraw_account_id, source, state
                        FROM a6_trade.a6_withdraw_account_bindings
                        WHERE merchant_id = %s
                        """,
                        (merchant.merchant_id,),
                    )
                    row = _row(cursor)
                    if row is None:
                        raise WithdrawAccountError("withdraw_existing_binding_conflict")
                    current = _binding(row)
                    if (
                        current.source != "existing"
                        or current.network != account.network
                        or current.address_sha256 != account.address_sha256
                        or current.a6_withdraw_account_id != account.account_id
                    ):
                        raise WithdrawAccountError("withdraw_existing_binding_conflict")
                    connection.commit()
                    return current
                _insert_action(
                    cursor,
                    row,
                    config_version=config_version,
                    requested_by=requested_by,
                    status="completed",
                )
            connection.commit()
            return _binding(row)
        except Exception:
            connection.rollback()
            raise
        finally:
            _close(connection)

    def mark_bound(self, binding: WithdrawBinding, account_id: int) -> WithdrawBinding:
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE a6_trade.a6_withdraw_account_bindings
                    SET a6_withdraw_account_id = %s, state = 'bound',
                        last_error_class = NULL, reconciled_at = now(), updated_at = now()
                    WHERE id = %s AND stable_remark = %s
                      AND state IN ('reserved', 'creating', 'create_unknown', 'error')
                    RETURNING id AS binding_id, merchant_id, network, address_sha256,
                              address_masked, address_secret_ref, stable_remark,
                              a6_withdraw_account_id, source, state
                    """,
                    (account_id, binding.binding_id, binding.stable_remark),
                )
                row = _row(cursor)
                if row is None:
                    raise WithdrawAccountError("withdraw_binding_update_missing")
                cursor.execute(
                    """
                    UPDATE a6_trade.trade_control_actions
                    SET status = 'completed', observed_after = %s::jsonb,
                        completed_at = now(), updated_at = now()
                    WHERE idempotency_key = %s
                    """,
                    (
                        _json({"a6_withdraw_account_id": account_id, "state": "bound"}),
                        _action_key(binding.stable_remark),
                    ),
                )
            connection.commit()
            return _binding(row)
        except Exception:
            connection.rollback()
            raise
        finally:
            _close(connection)

    def mark_error(self, binding: WithdrawBinding, error_class: str) -> None:
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE a6_trade.a6_withdraw_account_bindings
                    SET state = 'error', last_error_class = %s, updated_at = now()
                    WHERE id = %s AND state IN ('reserved', 'error')
                    """,
                    (error_class, binding.binding_id),
                )
                cursor.execute(
                    """
                    UPDATE a6_trade.trade_control_actions
                    SET status = 'failed', error_class = %s,
                        completed_at = now(), updated_at = now()
                    WHERE idempotency_key = %s AND status <> 'completed'
                    """,
                    (error_class, _action_key(binding.stable_remark)),
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            _close(connection)


def _parse_accounts(rows: Sequence[Mapping[str, Any]]) -> tuple[ExistingWithdrawAccount, ...]:
    accounts: list[ExistingWithdrawAccount] = []
    for row in rows:
        raw_info = row.get("account_info")
        try:
            info = json.loads(raw_info) if isinstance(raw_info, str) else raw_info
        except json.JSONDecodeError:
            raise WithdrawAccountError("withdraw_account_info_invalid") from None
        if not isinstance(info, Mapping) or not isinstance(info.get("fields"), Mapping):
            raise WithdrawAccountError("withdraw_account_info_invalid")
        channel = str(row.get("channel") or info.get("channel") or "").strip().casefold()
        if channel != "usdt":
            continue
        account_id = _positive_int(row.get("id"), "withdraw_account_id_invalid")
        fields = info["fields"]
        network = str(fields.get("network") or "").strip().upper()
        address = str(fields.get("address") or "").strip()
        if not _NETWORK.fullmatch(network) or not _ADDRESS.fullmatch(address):
            raise WithdrawAccountError("withdraw_account_info_invalid")
        is_default = row.get("is_default", False)
        if not isinstance(is_default, bool):
            raise WithdrawAccountError("withdraw_account_default_invalid")
        remark = str(row.get("remark") or info.get("remark") or "").strip()
        accounts.append(
            ExistingWithdrawAccount(
                account_id=account_id,
                network=network,
                address=address,
                address_sha256=hashlib.sha256(address.encode()).hexdigest(),
                address_masked=mask_address(address),
                remark=remark,
                is_default=is_default,
            )
        )
    return tuple(accounts)


def _choose_existing(
    accounts: Sequence[ExistingWithdrawAccount],
    binding: WithdrawBinding | None,
) -> ExistingWithdrawAccount:
    if binding is not None:
        matched = _match_stable_remark(accounts, binding.stable_remark)
        if matched is not None:
            return matched
        same_address = next(
            (item for item in accounts if item.address_sha256 == binding.address_sha256),
            None,
        )
        if same_address is not None:
            return same_address
    defaults = [item for item in accounts if item.is_default]
    if len(defaults) == 1:
        return defaults[0]
    if len(accounts) == 1:
        return accounts[0]
    raise WithdrawAccountError("withdraw_remote_account_ambiguous")


def _match_stable_remark(
    accounts: Sequence[ExistingWithdrawAccount],
    remark: str,
) -> ExistingWithdrawAccount | None:
    matches = [item for item in accounts if item.remark == remark]
    if len(matches) > 1:
        raise WithdrawAccountError("withdraw_remote_remark_not_unique")
    return matches[0] if matches else None


def _stable_remark(merchant_id: int, address_sha256: str) -> str:
    return f"a6trader-m{merchant_id}-{address_sha256[:12]}"


def _virtual_binding(
    merchant: WithdrawMerchant,
    entry: WithdrawPoolEntry,
    remark: str,
) -> WithdrawBinding:
    return WithdrawBinding(
        binding_id=0,
        merchant_id=merchant.merchant_id,
        network=entry.network,
        address_sha256=entry.address_sha256,
        address_masked=entry.address_masked,
        address_secret_ref=entry.secret_ref,
        stable_remark=remark,
        a6_withdraw_account_id=None,
        source="pool",
        state="planned",
    )


def _decision_existing(
    merchant: WithdrawMerchant,
    account: ExistingWithdrawAccount,
    applied: bool,
) -> WithdrawDecision:
    return WithdrawDecision(
        merchant_id=merchant.merchant_id,
        action="register_existing",
        applied=applied,
        state="existing_unmanaged" if applied else "observed",
        network=account.network,
        address_masked=account.address_masked,
        address_sha256=account.address_sha256,
        a6_withdraw_account_id=account.account_id,
    )


def _decision_bound(
    merchant: WithdrawMerchant,
    binding: WithdrawBinding,
    action: str,
) -> WithdrawDecision:
    return WithdrawDecision(
        merchant_id=merchant.merchant_id,
        action=action,
        applied=True,
        state=binding.state,
        network=binding.network,
        address_masked=binding.address_masked,
        address_sha256=binding.address_sha256,
        a6_withdraw_account_id=binding.a6_withdraw_account_id,
    )


def _decision_remote_binding(
    merchant: WithdrawMerchant,
    binding: WithdrawBinding,
    account: ExistingWithdrawAccount,
    action: str,
    applied: bool,
) -> WithdrawDecision:
    return WithdrawDecision(
        merchant_id=merchant.merchant_id,
        action=action,
        applied=applied,
        state=binding.state,
        network=binding.network,
        address_masked=binding.address_masked,
        address_sha256=binding.address_sha256,
        a6_withdraw_account_id=account.account_id,
    )


def _insert_action(
    cursor: Any,
    binding: Mapping[str, Any],
    *,
    config_version: str,
    requested_by: str,
    status: str,
) -> None:
    desired = {
        "network": binding["network"],
        "address_sha256": binding["address_sha256"],
        "address_masked": binding["address_masked"],
        "stable_remark": binding["stable_remark"],
        "create_account_only": True,
        "submit_withdrawal": False,
    }
    cursor.execute(
        """
        INSERT INTO a6_trade.trade_control_actions (
            idempotency_key, action_type, entity_type, entity_id,
            desired_state, status, config_version, requested_by,
            started_at, completed_at
        ) VALUES (
            %s, 'bind_crypto_withdraw_account', 'merchant', %s,
            %s::jsonb, %s, %s, %s, now(), CASE WHEN %s = 'completed' THEN now() END
        )
        ON CONFLICT (idempotency_key) DO NOTHING
        """,
        (
            _action_key(str(binding["stable_remark"])),
            str(binding["merchant_id"]),
            _json(desired),
            status,
            config_version,
            requested_by,
            status,
        ),
    )


def _merchant(row: Mapping[str, Any]) -> WithdrawMerchant:
    return WithdrawMerchant(
        merchant_id=int(row["merchant_id"]),
        a6_user_id=int(row["a6_user_id"]),
        supplier_id=int(row["supplier_id"]),
        session_ref=str(row["session_ref"]),
        status=str(row["status"]),
    )


def _binding(row: Mapping[str, Any]) -> WithdrawBinding:
    return WithdrawBinding(
        binding_id=int(row["binding_id"]),
        merchant_id=int(row["merchant_id"]),
        network=str(row["network"]),
        address_sha256=str(row["address_sha256"]),
        address_masked=str(row["address_masked"]),
        address_secret_ref=str(row["address_secret_ref"]),
        stable_remark=str(row["stable_remark"]),
        a6_withdraw_account_id=(
            int(row["a6_withdraw_account_id"])
            if row.get("a6_withdraw_account_id") is not None
            else None
        ),
        source=str(row["source"]),
        state=str(row["state"]),
    )


def _action_key(remark: str) -> str:
    return f"bind-withdraw-account:{remark}:v1"


def _error_code(exc: Exception) -> str:
    if isinstance(exc, WithdrawAccountError):
        return exc.code
    candidate = str(getattr(exc, "code", "") or "")
    if re.fullmatch(r"[a-z0-9_.:-]{1,96}", candidate):
        return candidate
    name = re.sub(r"(?<!^)(?=[A-Z])", "_", type(exc).__name__).lower()
    return f"withdraw_{name}"


def _positive_int(value: Any, error: str) -> int:
    if value is None or isinstance(value, bool):
        raise WithdrawAccountError(error)
    try:
        result = int(value)
    except (TypeError, ValueError):
        raise WithdrawAccountError(error) from None
    if result < 1:
        raise WithdrawAccountError(error)
    return result


def _rows(cursor: Any) -> list[dict[str, Any]]:
    raw = cursor.fetchall()
    if not raw:
        return []
    if isinstance(raw[0], Mapping):
        return [dict(item) for item in raw]
    names = [getattr(column, "name", column[0]) for column in cursor.description]
    return [dict(zip(names, item, strict=True)) for item in raw]


def _row(cursor: Any) -> dict[str, Any] | None:
    raw = cursor.fetchone()
    if raw is None:
        return None
    if isinstance(raw, Mapping):
        return dict(raw)
    names = [getattr(column, "name", column[0]) for column in cursor.description]
    return dict(zip(names, raw, strict=True))


def _json(value: Mapping[str, Any]) -> str:
    return json.dumps(dict(value), ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _close(connection: Any) -> None:
    close = getattr(connection, "close", None)
    if callable(close):
        close()
