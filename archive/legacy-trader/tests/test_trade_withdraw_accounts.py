from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from a6api_auto.models import SessionRecord
from a6api_auto.storage import SessionStore
from a6api_auto.trade_withdraw_accounts import (
    ExistingWithdrawAccount,
    WithdrawAccountError,
    WithdrawAccountService,
    WithdrawBinding,
    WithdrawMerchant,
    WithdrawPoolEntry,
    load_withdraw_pool,
)

ADDRESS_A = "0x1111111111111111111111111111111111111111"
ADDRESS_B = "T222222222222222222222222222222222"


def save_pool(path: Path, rows: list[dict[str, str]]) -> tuple[WithdrawPoolEntry, ...]:
    path.write_text(json.dumps(rows), encoding="utf-8")
    path.chmod(0o600)
    return load_withdraw_pool(path)


def save_session(path: Path, merchant_id: int) -> WithdrawMerchant:
    user_id = 9_500 + merchant_id
    supplier_id = 290 + merchant_id
    SessionStore(path).save(
        SessionRecord(
            schema_version=1,
            origin="https://a6api.com",
            email=f"merchant-{merchant_id}@fixture.invalid",
            username=f"merchant-{merchant_id}",
            password="fixture-password",
            user_id=user_id,
            supplier_id=supplier_id,
            status="supplier_submitted",
        )
    )
    return WithdrawMerchant(
        merchant_id=merchant_id,
        a6_user_id=user_id,
        supplier_id=supplier_id,
        session_ref=str(path),
        status="supplier_submitted",
    )


def remote_account(
    account_id: int,
    *,
    network: str,
    address: str,
    remark: str = "existing",
    is_default: bool = True,
) -> dict[str, Any]:
    return {
        "id": account_id,
        "channel": "usdt",
        "remark": remark,
        "is_default": is_default,
        "account_info": json.dumps(
            {
                "channel": "usdt",
                "remark": remark,
                "display": "masked fixture",
                "fields": {"network": network, "address": address},
            }
        ),
    }


class FakeStore:
    def __init__(
        self,
        merchants: list[WithdrawMerchant],
        bindings: tuple[WithdrawBinding, ...] = (),
        *,
        events: list[str] | None = None,
    ) -> None:
        self.merchants = {item.merchant_id: item for item in merchants}
        self.bindings = {item.merchant_id: item for item in bindings}
        self.events = events if events is not None else []
        self.writes: list[tuple[str, int]] = []
        self.next_binding_id = max(
            (item.binding_id for item in self.bindings.values()),
            default=0,
        ) + 1

    def list_merchants(self, merchant_ids):
        selected = set(merchant_ids)
        return tuple(
            item
            for merchant_id, item in sorted(self.merchants.items())
            if not selected or merchant_id in selected
        )

    def list_bindings(self):
        return tuple(self.bindings[key] for key in sorted(self.bindings))

    def get_binding(self, merchant_id: int):
        return self.bindings.get(merchant_id)

    def reserve_pool_entry(
        self,
        merchant: WithdrawMerchant,
        candidates,
        **_kwargs: Any,
    ) -> WithdrawBinding:
        existing = self.bindings.get(merchant.merchant_id)
        if existing is not None:
            return existing
        used = {item.address_sha256 for item in self.bindings.values()}
        entry = next(
            (candidate for candidate in candidates if candidate.address_sha256 not in used),
            None,
        )
        if entry is None:
            raise WithdrawAccountError("withdraw_pool_exhausted")
        binding = WithdrawBinding(
            binding_id=self.next_binding_id,
            merchant_id=merchant.merchant_id,
            network=entry.network,
            address_sha256=entry.address_sha256,
            address_masked=entry.address_masked,
            address_secret_ref=entry.secret_ref,
            stable_remark=(
                f"a6trader-m{merchant.merchant_id}-{entry.address_sha256[:12]}"
            ),
            a6_withdraw_account_id=None,
            source="pool",
            state="reserved",
        )
        self.next_binding_id += 1
        self.bindings[merchant.merchant_id] = binding
        self.writes.append(("reserve", merchant.merchant_id))
        self.events.append(f"reserve:{merchant.merchant_id}")
        return binding

    def register_existing(
        self,
        merchant: WithdrawMerchant,
        account: ExistingWithdrawAccount,
        **_kwargs: Any,
    ) -> WithdrawBinding:
        if any(
            item.address_sha256 == account.address_sha256
            for item in self.bindings.values()
        ):
            raise WithdrawAccountError("withdraw_existing_address_conflict")
        binding = WithdrawBinding(
            binding_id=self.next_binding_id,
            merchant_id=merchant.merchant_id,
            network=account.network,
            address_sha256=account.address_sha256,
            address_masked=account.address_masked,
            address_secret_ref=f"a6api:withdraw-account:{account.account_id}",
            stable_remark=(
                f"a6trader-existing-m{merchant.merchant_id}-a{account.account_id}"
            ),
            a6_withdraw_account_id=account.account_id,
            source="existing",
            state="existing_unmanaged",
        )
        self.next_binding_id += 1
        self.bindings[merchant.merchant_id] = binding
        self.writes.append(("register_existing", merchant.merchant_id))
        self.events.append(f"register_existing:{merchant.merchant_id}")
        return binding

    def claim_create(self, binding: WithdrawBinding) -> WithdrawBinding | None:
        current = self.bindings.get(binding.merchant_id)
        if current is None or current.state not in {"reserved", "error"}:
            return None
        updated = replace(current, state="creating")
        self.bindings[binding.merchant_id] = updated
        self.writes.append(("creating", binding.merchant_id))
        self.events.append(f"creating:{binding.merchant_id}")
        return updated

    def mark_create_unknown(
        self,
        binding: WithdrawBinding,
        error_class: str,
    ) -> WithdrawBinding:
        updated = replace(binding, state="create_unknown")
        self.bindings[binding.merchant_id] = updated
        self.writes.append((f"create_unknown:{error_class}", binding.merchant_id))
        self.events.append(f"create_unknown:{binding.merchant_id}")
        return updated

    def mark_bound(
        self,
        binding: WithdrawBinding,
        account_id: int,
    ) -> WithdrawBinding:
        updated = replace(
            binding,
            a6_withdraw_account_id=account_id,
            state="bound",
        )
        self.bindings[binding.merchant_id] = updated
        self.writes.append(("bound", binding.merchant_id))
        self.events.append(f"bound:{binding.merchant_id}")
        return updated

    def mark_error(self, binding: WithdrawBinding, error_class: str) -> None:
        self.bindings[binding.merchant_id] = replace(binding, state="error")
        self.writes.append((f"error:{error_class}", binding.merchant_id))
        self.events.append(f"error:{binding.merchant_id}")


class FakeClient:
    def __init__(
        self,
        accounts: list[dict[str, Any]] | None = None,
        *,
        events: list[str] | None = None,
        lose_first_post_response: bool = False,
    ) -> None:
        self.accounts = list(accounts or [])
        self.events = events if events is not None else []
        self.lose_first_post_response = lose_first_post_response
        self.list_calls = 0
        self.post_calls = 0

    def supplier_withdraw_accounts(self, user_id: int) -> list[dict[str, Any]]:
        assert user_id > 0
        self.list_calls += 1
        return deepcopy(self.accounts)

    def create_supplier_withdraw_account(
        self,
        user_id: int,
        *,
        network: str,
        address: str,
        remark: str,
        is_default: bool = True,
    ) -> dict[str, Any]:
        assert user_id > 0
        self.post_calls += 1
        self.events.append(f"post:{user_id - 9_500}")
        account = remote_account(
            700 + user_id,
            network=network,
            address=address,
            remark=remark,
            is_default=is_default,
        )
        self.accounts.append(account)
        if self.lose_first_post_response:
            self.lose_first_post_response = False
            raise WithdrawAccountError("withdraw_post_response_lost")
        return deepcopy(account)


def service(
    store: FakeStore,
    pool: tuple[WithdrawPoolEntry, ...],
    clients: dict[int, FakeClient],
) -> WithdrawAccountService:
    return WithdrawAccountService(
        store,
        pool,
        lambda merchant, _record: clients[merchant.merchant_id],
        config_version="test-withdraw-v1",
        requested_by="pytest",
    )


def test_private_pool_validation_duplicate_detection_and_masking(tmp_path: Path) -> None:
    pool_path = tmp_path / "withdraw-pool.json"
    pool = save_pool(
        pool_path,
        [
            {"network": "ERC20", "address": ADDRESS_A},
            {"network": "TRC20", "address": ADDRESS_B},
        ],
    )

    assert pool_path.stat().st_mode & 0o777 == 0o600
    assert pool[0].address_masked == "0x1111...1111"
    assert ADDRESS_A not in repr(pool[0])
    assert ADDRESS_A not in pool[0].secret_ref

    duplicate_path = tmp_path / "duplicate-pool.json"
    duplicate_path.write_text(
        json.dumps(
            [
                {"network": "ERC20", "address": ADDRESS_A},
                {"network": "BEP20", "address": ADDRESS_A},
            ]
        ),
        encoding="utf-8",
    )
    duplicate_path.chmod(0o600)
    with pytest.raises(WithdrawAccountError, match="^withdraw_pool_duplicate$"):
        load_withdraw_pool(duplicate_path)


def test_empty_private_pool_supports_existing_account_inventory(tmp_path: Path) -> None:
    pool_path = tmp_path / "withdraw-pool.json"
    pool = save_pool(pool_path, [])
    merchant = save_session(tmp_path / "merchant-1.json", 1)
    store = FakeStore([merchant])
    client = FakeClient(
        [remote_account(701, network="ERC20", address=ADDRESS_A)]
    )

    result = service(store, pool, {1: client}).execute()

    assert pool == ()
    assert result.summary()["success"] is True
    assert result.decisions[0].action == "register_existing"
    assert result.pool_entries == 0
    assert result.shortfall == 0


@pytest.mark.parametrize(
    ("contents", "mode", "error"),
    [
        ("not-json", 0o600, "withdraw_pool_invalid_json"),
        ("{}", 0o600, "withdraw_pool_shape_invalid"),
        (
            json.dumps([{"network": "erc20", "address": ADDRESS_A}]),
            0o600,
            "withdraw_pool_network_invalid",
        ),
        (
            json.dumps([{"network": "ERC20", "address": "short"}]),
            0o600,
            "withdraw_pool_address_invalid",
        ),
        (
            json.dumps([{"network": "ERC20", "address": ADDRESS_A}]),
            0o644,
            "withdraw_pool_permissions",
        ),
    ],
)
def test_pool_rejects_invalid_or_non_private_json(
    tmp_path: Path,
    contents: str,
    mode: int,
    error: str,
) -> None:
    path = tmp_path / "pool.json"
    path.write_text(contents, encoding="utf-8")
    path.chmod(mode)

    with pytest.raises(WithdrawAccountError, match=f"^{error}$"):
        load_withdraw_pool(path)


def test_dry_run_assigns_unique_addresses_without_store_writes(tmp_path: Path) -> None:
    merchants = [
        save_session(tmp_path / "merchant-1.json", 1),
        save_session(tmp_path / "merchant-2.json", 2),
    ]
    pool = save_pool(
        tmp_path / "pool.json",
        [
            {"network": "ERC20", "address": ADDRESS_A},
            {"network": "TRC20", "address": ADDRESS_B},
        ],
    )
    store = FakeStore(merchants)
    clients = {item.merchant_id: FakeClient() for item in merchants}

    result = service(store, pool, clients).execute()

    assert [item.action for item in result.decisions] == ["create", "create"]
    assert len({item.address_sha256 for item in result.decisions}) == 2
    assert all(item.applied is False and item.state == "planned" for item in result.decisions)
    assert result.summary()["mode"] == "dry-run"
    assert store.writes == []
    assert store.bindings == {}
    assert all(client.post_calls == 0 for client in clients.values())
    public = json.dumps(result.summary(), sort_keys=True)
    assert ADDRESS_A not in public
    assert ADDRESS_B not in public


def test_remote_existing_is_planned_and_excluded_from_later_pool_choice(
    tmp_path: Path,
) -> None:
    merchants = [
        save_session(tmp_path / "merchant-1.json", 1),
        save_session(tmp_path / "merchant-2.json", 2),
    ]
    pool = save_pool(
        tmp_path / "pool.json",
        [
            {"network": "ERC20", "address": ADDRESS_A},
            {"network": "TRC20", "address": ADDRESS_B},
        ],
    )
    store = FakeStore(merchants)
    clients = {
        1: FakeClient(
            [remote_account(101, network="ERC20", address=ADDRESS_A)]
        ),
        2: FakeClient(),
    }

    result = service(store, pool, clients).execute()

    assert [item.action for item in result.decisions] == [
        "register_existing",
        "create",
    ]
    assert result.decisions[0].state == "observed"
    assert result.decisions[1].address_sha256 == pool[1].address_sha256
    assert result.existing_remote_count == 1
    assert store.writes == []
    assert clients[1].post_calls == clients[2].post_calls == 0


def test_pool_shortfall_is_reported_in_batch_summary(tmp_path: Path) -> None:
    merchants = [
        save_session(tmp_path / "merchant-1.json", 1),
        save_session(tmp_path / "merchant-2.json", 2),
    ]
    pool = save_pool(
        tmp_path / "pool.json",
        [{"network": "ERC20", "address": ADDRESS_A}],
    )
    store = FakeStore(merchants)
    clients = {item.merchant_id: FakeClient() for item in merchants}

    summary = service(store, pool, clients).execute().summary()

    assert summary["success"] is False
    assert summary["needs_creation_count"] == 2
    assert summary["available_pool_count"] == 1
    assert summary["shortfall"] == 1
    assert summary["error_count"] == 1
    assert summary["decisions"][1]["error_class"] == "withdraw_pool_exhausted"
    assert store.writes == []


def test_apply_reserves_then_posts_reads_back_and_marks_bound(tmp_path: Path) -> None:
    merchant = save_session(tmp_path / "merchant-1.json", 1)
    pool = save_pool(
        tmp_path / "pool.json",
        [{"network": "ERC20", "address": ADDRESS_A}],
    )
    events: list[str] = []
    store = FakeStore([merchant], events=events)
    client = FakeClient(events=events)

    result = service(store, pool, {1: client}).execute(apply=True)

    assert result.summary()["success"] is True
    assert result.decisions[0].action == "create"
    assert result.decisions[0].state == "bound"
    assert result.decisions[0].applied is True
    assert events == ["reserve:1", "creating:1", "post:1", "bound:1"]
    assert client.list_calls == 3
    assert client.post_calls == 1
    assert store.writes == [("reserve", 1), ("creating", 1), ("bound", 1)]
    assert store.bindings[1].a6_withdraw_account_id == 10_201
    assert ADDRESS_A not in json.dumps(result.summary(), sort_keys=True)


def test_lost_post_response_recovers_by_stable_remark_without_second_post(
    tmp_path: Path,
) -> None:
    merchant = save_session(tmp_path / "merchant-1.json", 1)
    pool = save_pool(
        tmp_path / "pool.json",
        [{"network": "ERC20", "address": ADDRESS_A}],
    )
    events: list[str] = []
    store = FakeStore([merchant], events=events)
    client = FakeClient(events=events, lose_first_post_response=True)
    account_service = service(store, pool, {1: client})

    first = account_service.execute(apply=True)
    second = account_service.execute(apply=True)

    assert first.decisions[0].error_class == "withdraw_create_result_unknown"
    assert first.decisions[0].state == "create_unknown"
    assert store.writes[:3] == [
        ("reserve", 1),
        ("creating", 1),
        ("create_unknown:withdraw_post_response_lost", 1),
    ]
    assert second.summary()["success"] is True
    assert second.decisions[0].action == "reconcile"
    assert second.decisions[0].state == "bound"
    assert client.post_calls == 1
    assert sum(event.startswith("reserve:") for event in events) == 1
    assert events[-1] == "bound:1"


def test_existing_bound_account_is_noop_without_mutation(tmp_path: Path) -> None:
    merchant = save_session(tmp_path / "merchant-1.json", 1)
    pool = save_pool(
        tmp_path / "pool.json",
        [{"network": "ERC20", "address": ADDRESS_A}],
    )
    entry = pool[0]
    remark = f"a6trader-m1-{entry.address_sha256[:12]}"
    binding = WithdrawBinding(
        binding_id=9,
        merchant_id=1,
        network=entry.network,
        address_sha256=entry.address_sha256,
        address_masked=entry.address_masked,
        address_secret_ref=entry.secret_ref,
        stable_remark=remark,
        a6_withdraw_account_id=701,
        source="pool",
        state="bound",
    )
    store = FakeStore([merchant], (binding,))
    client = FakeClient(
        [
            remote_account(
                701,
                network=entry.network,
                address=entry.address,
                remark=remark,
            )
        ]
    )

    result = service(store, pool, {1: client}).execute(apply=True)

    assert result.decisions[0].action == "noop"
    assert result.decisions[0].applied is True
    assert result.decisions[0].state == "bound"
    assert store.writes == []
    assert client.post_calls == 0


def test_bound_account_missing_from_readback_is_never_recreated(tmp_path: Path) -> None:
    merchant = save_session(tmp_path / "merchant-1.json", 1)
    pool = save_pool(
        tmp_path / "pool.json",
        [{"network": "ERC20", "address": ADDRESS_A}],
    )
    entry = pool[0]
    binding = WithdrawBinding(
        binding_id=9,
        merchant_id=1,
        network=entry.network,
        address_sha256=entry.address_sha256,
        address_masked=entry.address_masked,
        address_secret_ref=entry.secret_ref,
        stable_remark=f"a6trader-m1-{entry.address_sha256[:12]}",
        a6_withdraw_account_id=701,
        source="pool",
        state="bound",
    )
    store = FakeStore([merchant], (binding,))
    client = FakeClient()

    result = service(store, pool, {1: client}).execute(apply=True)

    assert result.decisions[0].error_class == "withdraw_bound_account_readback_missing"
    assert result.decisions[0].state == "bound"
    assert store.bindings[1].state == "bound"
    assert store.writes == []
    assert client.post_calls == 0


def test_create_unknown_without_readback_is_never_reposted(tmp_path: Path) -> None:
    merchant = save_session(tmp_path / "merchant-1.json", 1)
    pool = save_pool(
        tmp_path / "pool.json",
        [{"network": "ERC20", "address": ADDRESS_A}],
    )
    entry = pool[0]
    binding = WithdrawBinding(
        binding_id=9,
        merchant_id=1,
        network=entry.network,
        address_sha256=entry.address_sha256,
        address_masked=entry.address_masked,
        address_secret_ref=entry.secret_ref,
        stable_remark=f"a6trader-m1-{entry.address_sha256[:12]}",
        a6_withdraw_account_id=None,
        source="pool",
        state="create_unknown",
    )
    store = FakeStore([merchant], (binding,))
    client = FakeClient()

    result = service(store, pool, {1: client}).execute(apply=True)

    assert result.decisions[0].error_class == "withdraw_create_reconciliation_pending"
    assert result.decisions[0].state == "create_unknown"
    assert store.writes == []
    assert client.post_calls == 0


@pytest.mark.parametrize("state", ["creating", "create_unknown"])
def test_dry_run_reports_pending_reconciliation_without_planning_create(
    tmp_path: Path,
    state: str,
) -> None:
    merchant = save_session(tmp_path / "merchant-1.json", 1)
    pool = save_pool(
        tmp_path / "pool.json",
        [{"network": "ERC20", "address": ADDRESS_A}],
    )
    entry = pool[0]
    binding = WithdrawBinding(
        binding_id=9,
        merchant_id=1,
        network=entry.network,
        address_sha256=entry.address_sha256,
        address_masked=entry.address_masked,
        address_secret_ref=entry.secret_ref,
        stable_remark=f"a6trader-m1-{entry.address_sha256[:12]}",
        a6_withdraw_account_id=None,
        source="pool",
        state=state,
    )
    store = FakeStore([merchant], (binding,))
    client = FakeClient()

    result = service(store, pool, {1: client}).execute()

    assert result.summary()["success"] is True
    assert result.needs_creation_count == 0
    assert result.decisions[0].action == "reconcile_pending"
    assert result.decisions[0].state == state
    assert result.decisions[0].applied is False
    assert store.writes == []
    assert client.post_calls == 0


def test_duplicate_remote_address_reports_stable_error_without_address_leak(
    tmp_path: Path,
) -> None:
    merchants = [
        save_session(tmp_path / "merchant-1.json", 1),
        save_session(tmp_path / "merchant-2.json", 2),
    ]
    pool = save_pool(
        tmp_path / "pool.json",
        [
            {"network": "ERC20", "address": ADDRESS_A},
            {"network": "TRC20", "address": ADDRESS_B},
        ],
    )
    existing = WithdrawBinding(
        binding_id=9,
        merchant_id=1,
        network=pool[0].network,
        address_sha256=pool[0].address_sha256,
        address_masked=pool[0].address_masked,
        address_secret_ref=pool[0].secret_ref,
        stable_remark=f"a6trader-m1-{pool[0].address_sha256[:12]}",
        a6_withdraw_account_id=701,
        source="pool",
        state="bound",
    )
    store = FakeStore(merchants, (existing,))
    clients = {
        1: FakeClient(),
        2: FakeClient(
            [remote_account(702, network="ERC20", address=ADDRESS_A)]
        ),
    }

    result = service(store, pool, clients).execute((2,), apply=True)
    summary_text = json.dumps(result.summary(), sort_keys=True)

    assert result.decisions[0].action == "error"
    assert result.decisions[0].error_class == "withdraw_existing_address_conflict"
    assert result.decisions[0].address_masked == ""
    assert result.decisions[0].address_sha256 == ""
    assert ADDRESS_A not in summary_text
    assert ADDRESS_A not in repr(result.decisions[0])
    assert store.writes == []
    assert clients[2].post_calls == 0


def test_multiple_remote_accounts_require_one_explicit_default(tmp_path: Path) -> None:
    merchant = save_session(tmp_path / "merchant-1.json", 1)
    pool = save_pool(
        tmp_path / "pool.json",
        [
            {"network": "ERC20", "address": ADDRESS_A},
            {"network": "TRC20", "address": ADDRESS_B},
        ],
    )
    store = FakeStore([merchant])
    client = FakeClient(
        [
            remote_account(
                701,
                network="ERC20",
                address=ADDRESS_A,
                is_default=False,
            ),
            remote_account(
                702,
                network="TRC20",
                address=ADDRESS_B,
                is_default=False,
            ),
        ]
    )

    result = service(store, pool, {1: client}).execute()

    assert result.decisions[0].error_class == "withdraw_remote_account_ambiguous"
    assert client.post_calls == 0


def test_remote_default_flag_must_be_json_boolean(tmp_path: Path) -> None:
    merchant = save_session(tmp_path / "merchant-1.json", 1)
    pool = save_pool(
        tmp_path / "pool.json",
        [{"network": "ERC20", "address": ADDRESS_A}],
    )
    account = remote_account(701, network="ERC20", address=ADDRESS_A)
    account["is_default"] = "false"

    result = service(FakeStore([merchant]), pool, {1: FakeClient([account])}).execute()

    assert result.decisions[0].error_class == "withdraw_account_default_invalid"


def test_filtered_run_still_reserves_addresses_seen_on_other_merchants(
    tmp_path: Path,
) -> None:
    merchants = [
        save_session(tmp_path / "merchant-1.json", 1),
        save_session(tmp_path / "merchant-2.json", 2),
    ]
    pool = save_pool(
        tmp_path / "pool.json",
        [
            {"network": "ERC20", "address": ADDRESS_A},
            {"network": "TRC20", "address": ADDRESS_B},
        ],
    )
    clients = {
        1: FakeClient(
            [remote_account(701, network="ERC20", address=ADDRESS_A)]
        ),
        2: FakeClient(),
    }

    result = service(FakeStore(merchants), pool, clients).execute((2,))

    assert len(result.decisions) == 1
    assert result.decisions[0].merchant_id == 2
    assert result.decisions[0].address_sha256 == pool[1].address_sha256
    assert clients[1].list_calls == clients[2].list_calls == 1


def test_any_global_inventory_failure_blocks_new_allocations(tmp_path: Path) -> None:
    class FailingClient(FakeClient):
        def supplier_withdraw_accounts(self, user_id: int):
            raise WithdrawAccountError("withdraw_inventory_transport_failed")

    merchants = [
        save_session(tmp_path / "merchant-1.json", 1),
        save_session(tmp_path / "merchant-2.json", 2),
    ]
    pool = save_pool(
        tmp_path / "pool.json",
        [{"network": "ERC20", "address": ADDRESS_A}],
    )
    store = FakeStore(merchants)
    clients = {1: FailingClient(), 2: FakeClient()}

    result = service(store, pool, clients).execute((2,), apply=True)

    assert result.summary()["success"] is False
    assert result.decisions[0].error_class == "withdraw_remote_inventory_incomplete"
    assert store.writes == []
    assert clients[2].post_calls == 0
