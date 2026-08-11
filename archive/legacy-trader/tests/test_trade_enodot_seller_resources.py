from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from a6api_auto.trade_enodot_seller_resources import (
    ManagedSellerBinding,
    ManagedSellerClaim,
    ManagedSellerContext,
    ManagedSellerError,
    ManagedSellerRemote,
    ManagedSellerRequest,
    ManagedSellerService,
    PostgresManagedSellerStore,
    PrivateFileManagedSellerSecretSink,
)

API_KEY = "sk-managed-seller-fixture-secret"


def request(*, delta: str = "0.25") -> ManagedSellerRequest:
    return ManagedSellerRequest(merchant_id=5, group_id=26, delta=delta)


def context(*, roles: tuple[str, ...] = ("seller",)) -> ManagedSellerContext:
    return ManagedSellerContext(
        merchant_id=5,
        a6_user_id=9517,
        supplier_id=298,
        merchant_status="supplier_submitted",
        roles=roles,
        session_ref="/private/merchant-5.json",
    )


def remote(*, delta: str = "0.25", api_key: str = "") -> dict[str, Any]:
    return {
        "managed_uid": request().managed_uid,
        "user_id": 51,
        "api_key_id": 61,
        "group_id": 26,
        "delta": delta,
        "initial_working_credit_usd": "999999999999.00000000",
        "status": "active",
        "api_key": api_key,
    }


def bound(*, delta: str = "0.25") -> ManagedSellerBinding:
    fingerprint = hashlib.sha256(API_KEY.encode()).hexdigest()
    return ManagedSellerBinding(
        binding_id=1,
        merchant_id=5,
        managed_uid=request().managed_uid,
        group_id=26,
        desired_delta=Decimal(delta),
        control_state="bound",
        enodot_user_id=51,
        enodot_api_key_id=61,
        api_key_fingerprint=fingerprint,
        key_secret_ref="secret://managed/seller-5",
    )


class FakeStore:
    def __init__(self, binding: ManagedSellerBinding | None = None) -> None:
        self.context = context()
        self.binding = binding
        self.events: list[str] = []
        self.desired_states: list[dict[str, Any]] = []
        self.observed_states: list[dict[str, Any]] = []
        self.error_class = ""

    def load_context(self, merchant_id: int):
        assert merchant_id == 5
        return self.context

    def get_binding(self, merchant_id: int):
        assert merchant_id == 5
        return self.binding

    def claim_create(self, seller, desired_request, desired_state):
        assert seller == self.context
        assert desired_request == request()
        if self.binding is not None and self.binding.control_state not in {"planned", "error"}:
            return None
        self.desired_states.append(dict(desired_state))
        self.binding = ManagedSellerBinding(
            binding_id=1,
            merchant_id=5,
            managed_uid=desired_request.managed_uid,
            group_id=26,
            desired_delta=desired_request.delta,
            control_state="creating",
        )
        self.events.append("claim_create")
        return ManagedSellerClaim(self.binding, action_id=10, attempt_no=1)

    def claim_update(self, binding, desired_request, desired_state):
        if binding.control_state in {"creating", "updating"}:
            return None
        self.desired_states.append(dict(desired_state))
        self.binding = replace(
            binding,
            desired_delta=desired_request.delta,
            control_state="updating",
        )
        self.events.append("claim_update")
        return ManagedSellerClaim(self.binding, action_id=11, attempt_no=1)

    def mark_unknown(self, claim, *, state: str, error_class: str):
        self.binding = replace(claim.binding, control_state=state)
        self.error_class = error_class
        self.events.append(state)

    def save_secret_receipt(
        self,
        binding,
        *,
        api_key_fingerprint: str,
        key_secret_ref: str,
    ):
        self.binding = replace(
            binding,
            api_key_fingerprint=api_key_fingerprint,
            key_secret_ref=key_secret_ref,
        )
        self.events.append("secret_saved")
        return self.binding

    def mark_bound(
        self,
        claim,
        observed,
        *,
        api_key_fingerprint: str,
        key_secret_ref: str,
    ):
        self.observed_states.append(observed.audit_snapshot())
        self.binding = replace(
            claim.binding,
            control_state="bound",
            enodot_user_id=observed.user_id,
            enodot_api_key_id=observed.api_key_id,
            api_key_fingerprint=api_key_fingerprint,
            key_secret_ref=key_secret_ref,
        )
        self.events.append("bound")
        return self.binding


class FakeAdmin:
    def __init__(
        self,
        current: dict[str, Any] | None = None,
        *,
        lose_create_response: bool = False,
        lose_create_readback: bool = False,
    ) -> None:
        self.current = current
        self.lose_create_response = lose_create_response
        self.lose_create_readback = lose_create_readback
        self.events: list[str] = []
        self.create_calls: list[dict[str, Any]] = []
        self.delta_calls: list[dict[str, Any]] = []
        self.old_unmanaged_resources = [
            {"user_id": 999, "api_key_id": 888, "name": "a6-seller-old"}
        ]

    def get_managed_seller_capability(self):
        return {
            "capability": "a6_trader_managed_sellers",
            "contract_version": "1.0",
            "create_method": "PUT",
            "create_secret_delivery": "first_response_only",
            "replay_returns_api_key": False,
            "read_returns_api_key": False,
            "delta_min": 0,
            "delta_max": 0.5,
            "rate_multiplier_base": 1,
        }

    def get_managed_seller(self, managed_uid: str):
        self.events.append("get")
        if self.lose_create_readback and self.create_calls:
            self.lose_create_readback = False
            raise RuntimeError("fixture create readback lost")
        if self.current is None or self.current["managed_uid"] != managed_uid:
            return None
        result = dict(self.current)
        result.pop("api_key", None)
        return result

    def create_managed_seller(self, **payload):
        self.create_calls.append(dict(payload))
        self.events.append("create")
        self.current = remote(api_key=API_KEY)
        if self.lose_create_response:
            self.lose_create_response = False
            raise RuntimeError("fixture transport response lost with secret")
        return dict(self.current)

    def set_managed_seller_delta(self, managed_uid, delta, *, idempotency_key):
        self.delta_calls.append(
            {
                "managed_uid": managed_uid,
                "delta": delta,
                "idempotency_key": idempotency_key,
            }
        )
        self.events.append("set_delta")
        assert self.current is not None
        self.current["delta"] = str(delta)
        return dict(self.current)


class FakeSecretSink:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.store_calls = 0

    def store_api_key(self, managed_uid: str, api_key: str):
        assert managed_uid == request().managed_uid
        self.store_calls += 1
        secret_ref = "secret://managed/seller-5"
        self.values[secret_ref] = api_key
        return secret_ref, hashlib.sha256(api_key.encode()).hexdigest()

    def get_api_key_fingerprint(self, secret_ref: str):
        value = self.values.get(secret_ref, API_KEY)
        return hashlib.sha256(value.encode()).hexdigest()

    def read_api_key(self, managed_uid: str, secret_ref: str):
        assert managed_uid == request().managed_uid
        return self.values.get(secret_ref, API_KEY)


def service(
    store: FakeStore,
    admin: FakeAdmin,
    secrets: FakeSecretSink | None = None,
) -> ManagedSellerService:
    return ManagedSellerService(store, admin, secrets or FakeSecretSink())


def test_stable_managed_uid_idempotency_and_delta_validation() -> None:
    first = request()
    second = request()

    assert first.managed_uid == "a6trader:seller:merchant-5"
    assert first.idempotency_key == second.idempotency_key
    assert len(first.idempotency_key) <= 128
    assert first.delta == Decimal("0.250000")
    assert "secret" not in repr(first)
    for value in ("-0.000001", "0.500001", float("nan"), True):
        with pytest.raises(ManagedSellerError, match="managed_seller_delta_invalid"):
            ManagedSellerRequest(merchant_id=5, group_id=26, delta=value)


def test_capability_failure_blocks_dry_run_before_resource_reads_or_writes() -> None:
    class UnavailableAdmin(FakeAdmin):
        def get_managed_seller_capability(self):
            self.events.append("capability")
            raise RuntimeError("fixture capability unavailable")

    store = FakeStore()
    admin = UnavailableAdmin()

    with pytest.raises(ManagedSellerError, match="managed_seller_capability_unavailable"):
        service(store, admin).execute(request())

    assert admin.events == ["capability"]
    assert admin.create_calls == []
    assert store.binding is None
    assert store.events == []


def test_capability_token_is_bound_to_issuing_service_instance() -> None:
    first = service(FakeStore(), FakeAdmin())
    second_store = FakeStore()
    second_admin = FakeAdmin()
    token = first.preflight_contract()

    with pytest.raises(ManagedSellerError, match="managed_seller_capability_invalid"):
        second = service(second_store, second_admin)
        second.execute(request(), capability=token)

    assert second_admin.events == []
    assert second_store.binding is None
    assert second_store.events == []


def test_capability_token_is_instance_bound_and_checked_before_store_reads() -> None:
    store = FakeStore()
    admin = FakeAdmin()
    reconciler = service(store, admin)

    with pytest.raises(ManagedSellerError, match="managed_seller_capability_invalid"):
        reconciler.execute(request(), capability=object())

    assert admin.events == []
    assert store.events == []
    assert store.binding is None


def test_capability_failure_blocks_dry_run_before_managed_seller_read() -> None:
    class MissingCapabilityAdmin(FakeAdmin):
        def get_managed_seller_capability(self):
            raise RuntimeError("route missing")

    store = FakeStore()
    admin = MissingCapabilityAdmin()

    with pytest.raises(ManagedSellerError, match="managed_seller_capability_unavailable"):
        service(store, admin).execute(request())

    assert admin.events == []
    assert admin.create_calls == []
    assert store.events == []
    assert store.binding is None


@pytest.mark.parametrize(
    "roles",
    [
        ("seller",),
        ("seller", "provider"),
        ("seller", "observer"),
    ],
)
def test_all_seller_duty_combinations_are_eligible(roles: tuple[str, ...]) -> None:
    store = FakeStore()
    store.context = context(roles=roles)
    admin = FakeAdmin()

    result = service(store, admin).execute(request(), apply=True)

    assert result.action == "create"
    assert result.state == "bound"
    assert result.verified is True
    assert len(admin.create_calls) == 1


@pytest.mark.parametrize(
    "roles",
    [
        ("provider",),
        ("seller", "provider", "observer"),
        ("seller", "unknown"),
        ("observer", "seller"),
        ("seller", "seller"),
    ],
)
def test_invalid_seller_context_is_rejected(roles: tuple[str, ...]) -> None:
    store = FakeStore()
    store.context = context(roles=roles)

    with pytest.raises(ManagedSellerError, match="managed_seller_context_invalid"):
        service(store, FakeAdmin()).execute(request())


def test_dry_run_ignores_old_resources_and_plans_independent_create() -> None:
    store = FakeStore()
    admin = FakeAdmin()
    result = service(store, admin).execute(request())

    assert result.public() == {
        "success": True,
        "mode": "dry-run",
        "merchant_id": 5,
        "managed_uid": "a6trader:seller:merchant-5",
        "action": "create",
        "applied": False,
        "verified": False,
        "state": "planned",
        "enodot_user_id": None,
        "enodot_api_key_id": None,
        "delta": "0.250000",
        "error_class": None,
    }
    assert admin.old_unmanaged_resources
    assert admin.create_calls == []
    assert store.binding is None
    assert store.desired_states == []


def test_apply_creates_user_key_delta_and_persists_only_secret_receipt() -> None:
    store = FakeStore()
    admin = FakeAdmin()
    secrets = FakeSecretSink()

    result = service(store, admin, secrets).execute(request(), apply=True)

    assert result.action == "create"
    assert result.verified is True
    assert result.enodot_user_id == 51
    assert result.enodot_api_key_id == 61
    assert result.delta == Decimal("0.250000")
    assert store.events == ["claim_create", "secret_saved", "bound"]
    assert admin.events == ["get", "create", "get"]
    assert len(admin.create_calls) == 1
    assert admin.create_calls[0] == {
        "managed_uid": request().managed_uid,
        "group_id": 26,
        "delta": Decimal("0.250000"),
        "idempotency_key": request().idempotency_key,
    }
    assert secrets.store_calls == 1
    assert store.binding is not None
    assert store.binding.control_state == "bound"
    assert store.binding.api_key_fingerprint == hashlib.sha256(API_KEY.encode()).hexdigest()
    assert store.binding.key_secret_ref == "secret://managed/seller-5"
    serialized = json.dumps(
        {
            "public": result.public(),
            "desired": store.desired_states,
            "observed": store.observed_states,
            "binding": repr(store.binding),
        },
        sort_keys=True,
    )
    assert API_KEY not in serialized


def test_repeated_bound_resource_is_noop_without_create_or_delta_mutation() -> None:
    store = FakeStore(bound())
    admin = FakeAdmin(remote())
    secrets = FakeSecretSink()

    first = service(store, admin, secrets).execute(request(), apply=True)
    second = service(store, admin, secrets).execute(request(), apply=True)

    assert first.action == second.action == "noop"
    assert first.verified is second.verified is True
    assert admin.create_calls == []
    assert admin.delta_calls == []
    assert store.events == []
    assert secrets.store_calls == 0


def test_delta_change_is_dry_run_then_single_update_with_readback() -> None:
    store = FakeStore(bound(delta="0.10"))
    admin = FakeAdmin(remote(delta="0.10"))
    secrets = FakeSecretSink()
    reconciler = service(store, admin, secrets)

    planned = reconciler.execute(request(delta="0.30"))
    applied = reconciler.execute(request(delta="0.30"), apply=True)
    repeated = reconciler.execute(request(delta="0.30"), apply=True)

    assert planned.action == "update_delta"
    assert planned.applied is False
    assert planned.verified is False
    assert planned.preflight_verified is True
    assert applied.action == "update_delta"
    assert applied.applied is True
    assert applied.delta == Decimal("0.300000")
    assert repeated.action == "noop"
    assert len(admin.delta_calls) == 1
    assert admin.delta_calls[0]["delta"] == Decimal("0.300000")
    assert store.events == ["claim_update", "bound"]
    assert secrets.store_calls == 0


def test_lost_create_response_marks_unknown_and_never_blindly_reposts() -> None:
    store = FakeStore()
    admin = FakeAdmin(lose_create_response=True)
    secrets = FakeSecretSink()
    reconciler = service(store, admin, secrets)

    with pytest.raises(ManagedSellerError, match="managed_seller_create_result_unknown"):
        reconciler.execute(request(), apply=True)

    assert store.binding is not None
    assert store.binding.control_state == "create_unknown"
    assert store.error_class == "managed_seller_runtimeerror"
    assert len(admin.create_calls) == 1
    assert secrets.store_calls == 0

    with pytest.raises(ManagedSellerError, match="managed_seller_create_secret_lost"):
        reconciler.execute(request(), apply=True)

    assert store.binding.control_state == "create_unknown"
    assert secrets.store_calls == 0
    assert len(admin.create_calls) == 1


def test_lost_create_readback_reconciles_from_local_secret_without_reposting() -> None:
    store = FakeStore()
    admin = FakeAdmin(lose_create_readback=True)
    secrets = FakeSecretSink()
    reconciler = service(store, admin, secrets)

    with pytest.raises(ManagedSellerError, match="managed_seller_create_result_unknown"):
        reconciler.execute(request(), apply=True)

    assert store.binding is not None
    assert store.binding.control_state == "create_unknown"
    assert store.binding.key_secret_ref == "secret://managed/seller-5"
    assert secrets.store_calls == 1
    assert len(admin.create_calls) == 1

    recovered = reconciler.execute(request(), apply=True)

    assert recovered.action == "reconcile"
    assert recovered.verified is True
    assert store.binding.control_state == "bound"
    assert secrets.store_calls == 1
    assert len(admin.create_calls) == 1


def test_exact_managed_uid_and_remote_ids_are_enforced() -> None:
    store = FakeStore(bound())
    wrong_uid = remote()
    wrong_uid["managed_uid"] = "a6trader:seller:merchant-19"
    with pytest.raises(ManagedSellerError, match="managed_seller_bound_readback_missing"):
        service(store, FakeAdmin(wrong_uid)).execute(request(), apply=True)

    wrong_key = remote()
    wrong_key["api_key_id"] = 999
    with pytest.raises(ManagedSellerError, match="managed_seller_remote_key_mismatch"):
        service(store, FakeAdmin(wrong_key)).execute(request(), apply=True)


@pytest.mark.parametrize(
    "amount",
    (None, 0, 1, "999999999998.99999999", True, float("nan"), float("inf")),
)
def test_remote_requires_fixed_initial_working_credit(amount) -> None:
    document = remote()
    if amount is None:
        document.pop("initial_working_credit_usd")
    else:
        document["initial_working_credit_usd"] = amount

    with pytest.raises(ManagedSellerError, match="managed_seller_response_invalid"):
        service(FakeStore(bound()), FakeAdmin(document)).execute(request(), apply=True)


@pytest.mark.parametrize(
    "amount",
    (999_999_999_999, 999_999_999_999.0, "999999999999.00000000"),
)
def test_remote_accepts_exact_initial_working_credit(amount) -> None:
    document = remote()
    document["initial_working_credit_usd"] = amount

    result = service(FakeStore(bound()), FakeAdmin(document)).execute(
        request(), apply=True
    )

    assert result.action == "noop"
    assert result.verified is True


def test_private_file_secret_sink_is_idempotent_owner_only_and_detects_conflict(
    tmp_path: Path,
) -> None:
    sink = PrivateFileManagedSellerSecretSink(tmp_path / "seller-keys")

    first_ref, first_fingerprint = sink.store_api_key(request().managed_uid, API_KEY)
    second_ref, second_fingerprint = sink.store_api_key(request().managed_uid, API_KEY)

    assert first_ref == second_ref
    assert first_fingerprint == second_fingerprint == hashlib.sha256(API_KEY.encode()).hexdigest()
    assert sink.get_api_key_fingerprint(first_ref) == first_fingerprint
    assert sink.read_api_key(request().managed_uid, first_ref) == API_KEY
    assert (tmp_path / "seller-keys").stat().st_mode & 0o777 == 0o700
    files = list((tmp_path / "seller-keys").glob("*.json"))
    assert len(files) == 1
    assert files[0].stat().st_mode & 0o777 == 0o600
    assert API_KEY not in repr(sink)

    with pytest.raises(ManagedSellerError, match="managed_seller_secret_conflict"):
        sink.store_api_key(request().managed_uid, f"{API_KEY}-different")

    with pytest.raises(ManagedSellerError, match="managed_seller_secret_identity_mismatch"):
        sink.read_api_key("a6trader:seller:merchant-19", first_ref)


def test_private_file_secret_sink_constructor_is_read_only(tmp_path: Path) -> None:
    root = tmp_path / "seller-keys"

    PrivateFileManagedSellerSecretSink(root)

    assert not root.exists()


class StoreCursor:
    def __init__(self, connection: StoreConnection) -> None:
        self.connection = connection
        self.one: dict[str, Any] | None = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, query: str, params=None):
        values = tuple(params or ())
        assert query.count("%s") == len(values)
        self.connection.calls.append((query, values))
        normalized = " ".join(query.split())
        self.one = None
        if normalized.startswith("SELECT id AS merchant_id"):
            self.one = {
                "merchant_id": 5,
                "a6_user_id": 9517,
                "supplier_id": 298,
                "merchant_status": "supplier_submitted",
                "roles": ["seller", "observer"],
                "session_ref": "/private/merchant-5.json",
            }
        elif normalized.startswith("SELECT id, merchant_id, managed_uid"):
            if self.connection.binding is not None:
                self.one = dict(self.connection.binding)
        elif normalized.startswith("INSERT INTO a6_trade.trade_control_actions"):
            self.one = {"id": 10}
        elif normalized.startswith(
            "INSERT INTO a6_trade.enodot_managed_seller_resources"
        ):
            self.connection.binding = binding_row(control_state="creating")
            self.one = dict(self.connection.binding)
        elif normalized.startswith("SELECT coalesce(max(attempt_no)"):
            self.one = {"attempt_no": 1}
        elif normalized.startswith(
            "UPDATE a6_trade.enodot_managed_seller_resources SET api_key_fingerprint"
        ):
            assert self.connection.binding is not None
            self.connection.binding["api_key_fingerprint"] = values[0]
            self.connection.binding["key_secret_ref"] = values[1]
            self.one = dict(self.connection.binding)
        elif normalized.startswith(
            "UPDATE a6_trade.enodot_managed_seller_resources SET enodot_user_id"
        ):
            assert self.connection.binding is not None
            self.connection.binding.update(
                {
                    "enodot_user_id": values[0],
                    "enodot_api_key_id": values[1],
                    "api_key_fingerprint": values[2],
                    "key_secret_ref": values[3],
                    "desired_delta": values[4],
                    "control_state": "bound",
                }
            )
            self.one = dict(self.connection.binding)
        return self

    def fetchone(self):
        return self.one

    def fetchall(self):
        normalized = " ".join(self.connection.calls[-1][0].split())
        if normalized.startswith(
            "SELECT merchant.id AS merchant_id FROM a6_trade.a6_merchants"
        ):
            return [{"merchant_id": merchant_id} for merchant_id in range(19, 0, -1)]
        return []


class StoreConnection:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.binding: dict[str, Any] | None = None
        self.commits = 0
        self.rollbacks = 0
        self.closed = 0

    def cursor(self):
        return StoreCursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed += 1


def binding_row(*, control_state: str) -> dict[str, Any]:
    return {
        "id": 1,
        "merchant_id": 5,
        "managed_uid": request().managed_uid,
        "group_id": 26,
        "desired_delta": Decimal("0.250000"),
        "control_state": control_state,
        "enodot_user_id": None,
        "enodot_api_key_id": None,
        "api_key_fingerprint": "",
        "key_secret_ref": "",
    }


def test_postgres_store_selects_collected_nonretired_sellers_without_duty_filter() -> None:
    connection = StoreConnection()
    store = PostgresManagedSellerStore(lambda: connection)

    merchant_ids = store.list_seller_ids()

    assert merchant_ids == tuple(range(1, 20))
    assert len(merchant_ids) == 19
    query, params = connection.calls[0]
    normalized = " ".join(query.split())
    assert params == ()
    assert "status = 'supplier_submitted'" not in normalized
    assert "a6_user_id IS NOT NULL" not in normalized
    assert "supplier_id IS NOT NULL" not in normalized
    assert "roles[1] = 'seller'" in normalized
    assert "last_poll_at IS NOT NULL" in normalized
    assert "a6_seller_retirement_plans" in normalized
    assert "retirement.state = 'completed'" in normalized
    assert "ORDER BY merchant.id" in normalized
    assert "provider" not in normalized
    assert "observer" not in normalized
    assert "probe" not in normalized
    assert connection.commits == connection.rollbacks == 0
    assert connection.closed == 1


def test_postgres_store_selects_only_enodot_bound_sellers_for_continuous_control() -> None:
    connection = StoreConnection()
    store = PostgresManagedSellerStore(lambda: connection)

    merchant_ids = store.list_bound_seller_ids()

    assert merchant_ids == tuple(range(1, 20))
    query, params = connection.calls[0]
    normalized = " ".join(query.split())
    assert params == ()
    assert "JOIN a6_trade.enodot_managed_seller_resources seller" in normalized
    assert "seller.control_state = 'bound'" in normalized
    assert "merchant.last_poll_at IS NOT NULL" in normalized
    assert "retirement.state = 'completed'" in normalized
    assert connection.closed == 1


def test_postgres_store_create_claim_secret_receipt_and_bound_are_audited() -> None:
    connection = StoreConnection()
    store = PostgresManagedSellerStore(lambda: connection)
    seller_context = store.load_context(5)

    claim = store.claim_create(seller_context, request(), {"managed_uid": request().managed_uid})
    assert claim is not None
    fingerprint = hashlib.sha256(API_KEY.encode()).hexdigest()
    saved = store.save_secret_receipt(
        claim.binding,
        api_key_fingerprint=fingerprint,
        key_secret_ref="enodot-seller:sha256:" + "a" * 64,
    )
    completed = store.mark_bound(
        replace(claim, binding=saved),
        # Remote never serializes api_key into the audit rows.
        ManagedSellerRemote(
            managed_uid=request().managed_uid,
            user_id=51,
            api_key_id=61,
            group_id=26,
            delta=Decimal("0.250000"),
            initial_working_credit_usd=Decimal("999999999999.00000000"),
            status="active",
            api_key=API_KEY,
        ),
        api_key_fingerprint=fingerprint,
        key_secret_ref=saved.key_secret_ref,
    )

    assert completed.control_state == "bound"
    assert completed.enodot_user_id == 51
    assert completed.enodot_api_key_id == 61
    rendered = repr(connection.calls)
    assert API_KEY not in rendered
    assert "create_enodot_seller_resource" in rendered
    assert "trade_control_action_attempts" in rendered
    assert connection.commits == 3
    assert connection.rollbacks == 0
    assert connection.closed == 4
