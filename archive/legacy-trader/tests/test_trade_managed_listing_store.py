from __future__ import annotations

from collections import defaultdict, deque
from copy import deepcopy
from typing import Any

import pytest

from a6api_auto import trade_managed_listing_store as managed_store
from a6api_auto.trade_managed_listing_store import PostgresManagedListingStore
from a6api_auto.trade_managed_listings import (
    ChannelAdoptionIntent,
    ChannelCreateIntent,
    ListingAdoptionIntent,
    ListingCreateIntent,
    ManagedAction,
    ManagedChannelBinding,
    ManagedListingBinding,
)

CHANNEL_UID = "a6-managed:channel:m5:channel-fixture"
LISTING_UID = "a6-managed:listing:m5:listing-fixture"
CHANNEL_CREATE_KEY = f"managed-channel-create:{CHANNEL_UID}"
LISTING_CREATE_KEY = f"managed-listing-create:{LISTING_UID}"


def channel_desired() -> dict[str, Any]:
    return {
        "spec_fingerprint": "a" * 64,
        "marker": "a6m-channel-fixture",
        "remote_channel": {
            "name": "managed-a6m-channel-fixture",
            "nickname": "managed-a6m-channel-fixture",
            "channel_type": 1,
            "models": "gpt-5.6-sol",
            "description": f"[managed_uid:{CHANNEL_UID}] fixture",
            "api_key_sha256": "b" * 64,
        },
        "baseline_channel_ids": [10, 11],
        "baseline_request_ids": [20, 21],
    }


def listing_desired() -> dict[str, Any]:
    return {
        "spec_fingerprint": "c" * 64,
        "marker": "a6m-listing-fixture",
        "remote_listing": {
            "channel_id": 700,
            "own_model": "gpt-5.6-sol",
            "platform_model": "gpt-5.6-sol",
            "charge_type": "per_token",
            "input_price_micros": 1_000_000,
            "output_price_micros": 2_000_000,
            "cache_read_price_micros": 100_000,
            "cache_write_price_micros": 200_000,
            "display_name": "gpt-5.6-sol-a6m-listing-fixture",
            "description": f"[managed_uid:{LISTING_UID}] fixture",
        },
        "baseline_listing_ids": [30, 31],
    }


def channel_row(
    *,
    state: str = "planned",
    channel_id: int | None = None,
    create_action_id: int | None = None,
    last_action_id: int | None = None,
    last_error_class: str | None = None,
) -> dict[str, Any]:
    return {
        "id": 1,
        "binding_id": 1,
        "managed_uid": CHANNEL_UID,
        "merchant_id": 5,
        "a6_channel_id": channel_id,
        "create_idempotency_key": CHANNEL_CREATE_KEY,
        "control_state": state,
        "desired_state": channel_desired(),
        "observed_state": None,
        "create_action_id": create_action_id,
        "last_action_id": last_action_id,
        "last_error_class": last_error_class,
    }


def listing_row(
    *,
    state: str = "planned",
    listing_id: int | None = None,
    create_action_id: int | None = None,
    last_action_id: int | None = None,
    last_error_class: str | None = None,
) -> dict[str, Any]:
    return {
        "id": 2,
        "binding_id": 2,
        "managed_uid": LISTING_UID,
        "merchant_id": 5,
        "channel_binding_id": 1,
        "a6_channel_id": 700,
        "a6_listing_id": listing_id,
        "create_idempotency_key": LISTING_CREATE_KEY,
        "control_state": state,
        "desired_state": listing_desired(),
        "observed_state": None,
        "create_action_id": create_action_id,
        "last_action_id": last_action_id,
        "last_error_class": last_error_class,
    }


def channel_binding(**overrides: Any) -> ManagedChannelBinding:
    values = channel_row(**overrides)
    return ManagedChannelBinding(
        binding_id=values["binding_id"],
        managed_uid=values["managed_uid"],
        merchant_id=values["merchant_id"],
        create_idempotency_key=values["create_idempotency_key"],
        control_state=values["control_state"],
        desired_state=values["desired_state"],
        observed_state=values["observed_state"],
        a6_channel_id=values["a6_channel_id"],
        create_action_id=values["create_action_id"],
        last_error_class=values["last_error_class"],
    )


def listing_binding(**overrides: Any) -> ManagedListingBinding:
    values = listing_row(**overrides)
    return ManagedListingBinding(
        binding_id=values["binding_id"],
        managed_uid=values["managed_uid"],
        merchant_id=values["merchant_id"],
        channel_binding_id=values["channel_binding_id"],
        create_idempotency_key=values["create_idempotency_key"],
        control_state=values["control_state"],
        desired_state=values["desired_state"],
        observed_state=values["observed_state"],
        a6_channel_id=values["a6_channel_id"],
        a6_listing_id=values["a6_listing_id"],
        create_action_id=values["create_action_id"],
        last_error_class=values["last_error_class"],
    )


def channel_intent() -> ChannelCreateIntent:
    return ChannelCreateIntent(
        managed_uid=CHANNEL_UID,
        merchant_id=5,
        create_idempotency_key=CHANNEL_CREATE_KEY,
        desired_state=channel_desired(),
    )


def listing_intent() -> ListingCreateIntent:
    return ListingCreateIntent(
        managed_uid=LISTING_UID,
        merchant_id=5,
        channel_binding_id=1,
        a6_channel_id=700,
        create_idempotency_key=LISTING_CREATE_KEY,
        desired_state=listing_desired(),
    )


def action_row(*, operation: str = "update", state: str = "updating") -> dict[str, Any]:
    desired = {"payload": {"input_price_micros": 1_100_000}}
    return {
        "id": 303,
        "action_id": 303,
        "resource_kind": "listing",
        "managed_uid": LISTING_UID,
        "operation": operation,
        "idempotency_key": f"managed-action:{LISTING_UID}:{operation}:fixture",
        "desired_state": desired,
        "state": state,
        "status": "started",
        "action_type": f"{operation}_managed_listing",
        "entity_type": "a6_managed_listing",
        "entity_id": LISTING_UID,
    }


class ContractCursor:
    def __init__(self, connection: ContractConnection) -> None:
        self.connection = connection
        self.one: dict[str, Any] | None = None
        self.rowcount = -1

    def __enter__(self) -> ContractCursor:
        return self

    def __exit__(self, *_args: Any) -> bool:
        return False

    def execute(self, query: str, params: Any = None) -> ContractCursor:
        values = tuple(params or ())
        normalized = " ".join(query.lower().split())
        label = _sql_label(normalized)
        self.connection.calls.append((label, query, values))
        if self.connection.fail_on == label:
            raise RuntimeError(f"fixture SQL failure at {label}")
        queued = self.connection.responses[label]
        self.one = deepcopy(queued.popleft()) if queued else None
        self.rowcount = 1 if self.one is not None or _is_write(label) else 0
        return self

    def fetchone(self) -> dict[str, Any] | None:
        return deepcopy(self.one)

    def fetchall(self) -> list[dict[str, Any]]:
        return [] if self.one is None else [deepcopy(self.one)]


class ContractConnection:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, tuple[Any, ...]]] = []
        self.responses: defaultdict[str, deque[dict[str, Any] | None]] = defaultdict(deque)
        self.fail_on: str | None = None
        self.commits = 0
        self.rollbacks = 0
        self.closes = 0
        self.opens = 0

    def queue(self, label: str, *rows: dict[str, Any] | None) -> None:
        self.responses[label].extend(deepcopy(rows))

    def factory(self) -> ContractConnection:
        self.opens += 1
        return self

    def cursor(self) -> ContractCursor:
        return ContractCursor(self)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        self.closes += 1


def _sql_label(sql: str) -> str:
    table_operations = (
        ("insert into a6_trade.a6_supplier_channels_current", "channel_inventory_upsert"),
        ("insert into a6_trade.a6_supplier_listings_current", "listing_inventory_upsert"),
        ("insert into a6_trade.a6_managed_channel_bindings", "channel_binding_insert"),
        ("update a6_trade.a6_managed_channel_bindings", "channel_binding_update"),
        ("insert into a6_trade.a6_managed_listing_bindings", "listing_binding_insert"),
        ("update a6_trade.a6_managed_listing_bindings", "listing_binding_update"),
        ("insert into a6_trade.trade_control_action_attempts", "attempt_insert"),
        ("update a6_trade.trade_control_action_attempts", "attempt_update"),
        ("insert into a6_trade.trade_control_actions", "action_insert"),
        ("update a6_trade.trade_control_actions", "action_update"),
    )
    for fragment, label in table_operations:
        if fragment in sql:
            return label
    if "select coalesce(max(attempt_no)" in sql:
        return "attempt_next"
    if "from a6_trade.trade_control_action_attempts" in sql:
        return "attempt_select"
    if "from a6_trade.trade_control_actions" in sql:
        return "action_select"
    if "from a6_trade.a6_managed_channel_bindings" in sql:
        return "channel_binding_select"
    if "from a6_trade.a6_managed_listing_bindings" in sql:
        return "listing_binding_select"
    if "from a6_trade.a6_supplier_channels_current" in sql:
        return "channel_inventory_select"
    if "from a6_trade.a6_supplier_listings_current" in sql:
        return "listing_inventory_select"
    return "other"


def _is_write(label: str) -> bool:
    return label.endswith(("_insert", "_update", "_upsert"))


def _labels(connection: ContractConnection) -> list[str]:
    return [label for label, _query, _params in connection.calls]


def _assert_subsequence(actual: list[str], expected: list[str]) -> None:
    position = 0
    for label in actual:
        if position < len(expected) and label == expected[position]:
            position += 1
    assert position == len(expected), f"missing ordered SQL events: {expected}; got {actual}"


def store(connection: ContractConnection) -> PostgresManagedListingStore:
    return PostgresManagedListingStore(
        connection.factory,
        config_version="managed-listing-store-test-v1",
        requested_by="pytest",
    )


def test_channel_reserve_then_claim_writes_action_and_attempt_atomically() -> None:
    connection = ContractConnection()
    connection.queue("channel_binding_insert", channel_row())
    connection.queue("channel_binding_select", channel_row())
    connection.queue("action_insert", {"id": 101, "action_id": 101, "status": "started"})
    connection.queue(
        "channel_binding_update",
        channel_row(state="creating", create_action_id=101, last_action_id=101),
    )
    connection.queue("attempt_next", {"attempt_no": 1})
    database = store(connection)

    reserved = database.reserve_channel(channel_intent())
    claimed = database.claim_channel_create(reserved)

    assert reserved.control_state == "planned"
    assert claimed is not None
    assert claimed.control_state == "creating"
    assert claimed.create_action_id == 101
    _assert_subsequence(
        _labels(connection),
        [
            "channel_binding_insert",
            "channel_binding_select",
            "action_insert",
            "channel_binding_update",
            "attempt_insert",
        ],
    )
    rendered = repr(connection.calls)
    assert "managed-listing-store-test-v1" in rendered
    assert "pytest" in rendered
    assert connection.commits == 2
    assert connection.rollbacks == 0
    assert connection.closes == connection.opens == 2


def test_listing_reserve_then_claim_writes_action_and_attempt_atomically() -> None:
    connection = ContractConnection()
    connection.queue("channel_binding_select", channel_row(state="verified", channel_id=700))
    connection.queue("listing_binding_insert", listing_row())
    connection.queue("listing_binding_select", listing_row())
    connection.queue("action_insert", {"id": 202, "action_id": 202, "status": "started"})
    connection.queue(
        "listing_binding_update",
        listing_row(state="creating", create_action_id=202, last_action_id=202),
    )
    connection.queue("attempt_next", {"attempt_no": 1})
    database = store(connection)

    reserved = database.reserve_listing(listing_intent())
    claimed = database.claim_listing_create(reserved)

    assert reserved.control_state == "planned"
    assert claimed is not None
    assert claimed.control_state == "creating"
    assert claimed.create_action_id == 202
    _assert_subsequence(
        _labels(connection),
        [
            "listing_binding_insert",
            "listing_binding_select",
            "action_insert",
            "listing_binding_update",
            "attempt_insert",
        ],
    )
    assert connection.commits == 2
    assert connection.rollbacks == 0
    assert connection.closes == connection.opens == 2


def test_unknown_listing_update_retry_reopens_action_with_new_attempt() -> None:
    connection = ContractConnection()
    pending = listing_row(
        state="update_unknown",
        listing_id=900,
        create_action_id=101,
        last_action_id=303,
        last_error_class="supplier_listing_update_failed",
    )
    action_document = action_row(operation="update", state="update_unknown")
    action_document["status"] = "unknown"
    connection.queue("listing_binding_select", pending)
    connection.queue("action_select", action_document)
    connection.queue("action_update", {"id": 303})
    connection.queue(
        "listing_binding_update",
        listing_row(
            state="updating",
            listing_id=900,
            create_action_id=101,
            last_action_id=303,
        ),
    )
    connection.queue("attempt_next", {"attempt_no": 2})
    action = ManagedAction(
        action_id=303,
        resource_kind="listing",
        managed_uid=LISTING_UID,
        operation="update",
        idempotency_key=action_document["idempotency_key"],
        desired_state=action_document["desired_state"],
        state="update_unknown",
    )

    retried = store(connection).retry_unknown_listing_update(action)

    assert retried is not None
    assert retried.action_id == action.action_id
    assert retried.state == "updating"
    _assert_subsequence(
        _labels(connection),
        [
            "listing_binding_select",
            "action_select",
            "action_update",
            "listing_binding_update",
            "attempt_next",
            "attempt_insert",
        ],
    )
    assert connection.commits == 1
    assert connection.rollbacks == 0


def test_unknown_channel_update_retry_reopens_action_with_new_attempt() -> None:
    connection = ContractConnection()
    desired = {
        "operation": "ensure_upstream",
        "upstream_base_url_sha256": "a" * 64,
        "upstream_api_key_sha256": "b" * 64,
        "api_key_last4": "last",
        "has_api_key": True,
        "required_model": "gpt-5.6-sol",
    }
    pending = channel_row(
        state="update_unknown",
        channel_id=700,
        create_action_id=101,
        last_action_id=303,
        last_error_class="supplier_channel_update_failed",
    )
    action_document = action_row(operation="update", state="update_unknown")
    action_document.update(
        {
            "resource_kind": "channel",
            "managed_uid": CHANNEL_UID,
            "entity_type": "a6_managed_channel",
            "entity_id": CHANNEL_UID,
            "action_type": "update_managed_channel",
            "idempotency_key": f"managed-action:{CHANNEL_UID}:update:fixture",
            "desired_state": desired,
            "status": "unknown",
        }
    )
    connection.queue("channel_binding_select", pending)
    connection.queue("action_select", action_document)
    connection.queue("action_update", {"id": 303})
    connection.queue(
        "channel_binding_update",
        channel_row(
            state="updating",
            channel_id=700,
            create_action_id=101,
            last_action_id=303,
        ),
    )
    connection.queue("attempt_next", {"attempt_no": 2})
    action = ManagedAction(
        action_id=303,
        resource_kind="channel",
        managed_uid=CHANNEL_UID,
        operation="update",
        idempotency_key=action_document["idempotency_key"],
        desired_state=desired,
        state="update_unknown",
    )

    retried = store(connection).retry_unknown_channel_update(action)

    assert retried is not None
    assert retried.action_id == action.action_id
    assert retried.state == "updating"
    _assert_subsequence(
        _labels(connection),
        [
            "channel_binding_select",
            "action_select",
            "action_update",
            "channel_binding_update",
            "attempt_next",
            "attempt_insert",
        ],
    )
    assert connection.commits == 1
    assert connection.rollbacks == 0


def test_unknown_listing_resubmit_readback_completes_without_reposting() -> None:
    connection = ContractConnection()
    pending = listing_row(
        state="resubmit_unknown",
        listing_id=900,
        create_action_id=202,
        last_action_id=303,
        last_error_class="a6_transport_failed",
    )
    action_document = action_row(operation="resubmit", state="resubmit_unknown")
    action_document["status"] = "unknown"
    observation = {"id": 900, "status": 2, **listing_desired()["remote_listing"]}
    connection.queue("listing_binding_select", pending)
    connection.queue("action_select", action_document)
    connection.queue("listing_inventory_upsert", {"merchant_id": 5, "a6_listing_id": 900})
    connection.queue(
        "listing_binding_update",
        listing_row(
            state="verified",
            listing_id=900,
            create_action_id=202,
            last_action_id=303,
        ),
    )
    action = ManagedAction(
        action_id=303,
        resource_kind="listing",
        managed_uid=LISTING_UID,
        operation="resubmit",
        idempotency_key=action_document["idempotency_key"],
        desired_state=action_document["desired_state"],
        state="resubmit_unknown",
    )

    completed = store(connection).complete_listing_action(
        listing_binding(
            state="resubmit_unknown",
            listing_id=900,
            create_action_id=202,
            last_action_id=303,
            last_error_class="a6_transport_failed",
        ),
        action,
        observation,
    )

    assert completed.control_state == "verified"
    assert completed.a6_listing_id == 900
    _assert_subsequence(
        _labels(connection),
        [
            "listing_binding_select",
            "action_select",
            "listing_inventory_upsert",
            "listing_binding_update",
            "action_update",
            "attempt_update",
        ],
    )
    binding_sql = next(
        query for label, query, _params in connection.calls if label == "listing_binding_update"
    )
    assert "control_state = 'resubmit_unknown'" in binding_sql
    completion_calls = [
        params
        for label, _query, params in connection.calls
        if label in {"action_update", "attempt_update"}
    ]
    assert completion_calls
    assert all(params[-1] == "unknown" for params in completion_calls)
    assert connection.commits == 1
    assert connection.rollbacks == 0


def test_started_listing_update_can_be_superseded_after_absent_readback() -> None:
    connection = ContractConnection()
    pending = listing_row(
        state="updating",
        listing_id=900,
        create_action_id=101,
        last_action_id=303,
    )
    action_document = action_row(operation="update", state="updating")
    connection.queue("listing_binding_select", pending)
    connection.queue("action_select", action_document)
    connection.queue("action_update", {"id": 303})
    connection.queue(
        "listing_binding_update",
        listing_row(
            state="verified",
            listing_id=900,
            create_action_id=101,
            last_action_id=303,
        ),
    )
    action = ManagedAction(
        action_id=303,
        resource_kind="listing",
        managed_uid=LISTING_UID,
        operation="update",
        idempotency_key=action_document["idempotency_key"],
        desired_state=action_document["desired_state"],
        state="updating",
    )
    observation = {
        **listing_desired()["remote_listing"],
        "id": 900,
        "status": 0,
        "availability": 0,
        "visibility": 0,
    }

    binding = store(connection).supersede_listing_update(action, observation)

    assert binding.control_state == "verified"
    _assert_subsequence(
        _labels(connection),
        [
            "listing_binding_select",
            "action_select",
            "listing_inventory_upsert",
            "action_update",
            "attempt_update",
            "listing_binding_update",
        ],
    )
    assert connection.commits == 1
    assert connection.rollbacks == 0


def test_retired_channel_binding_can_be_audit_adopted() -> None:
    connection = ContractConnection()
    connection.queue("channel_binding_select", channel_row(state="retired"))
    connection.queue("action_insert", {"id": 401})
    connection.queue(
        "channel_binding_update",
        channel_row(state="verified", channel_id=42, create_action_id=401),
    )
    desired = {
        "spec_fingerprint": "d" * 64,
        "channel_strategy": "adopt_existing",
        "models_after": ["gpt-5.5", "gpt-5.6-sol"],
    }
    observation = {
        "id": 42,
        "supplier_id": 77,
        "name": "existing-channel",
        "nickname": "existing-channel",
        "channel_type": 1,
        "status": 1,
        "availability": 1,
        "models": "gpt-5.5,gpt-5.6-sol",
        "description": "existing channel",
    }

    adopted = store(connection).adopt_channel(
        ChannelAdoptionIntent(
            managed_uid=CHANNEL_UID,
            merchant_id=5,
            a6_channel_id=42,
            idempotency_key=f"managed-channel-adopt:{CHANNEL_UID}:42",
            desired_state=desired,
            observation=observation,
        )
    )

    assert adopted.control_state == "verified"
    assert adopted.a6_channel_id == 42
    _assert_subsequence(
        _labels(connection),
        [
            "channel_binding_select",
            "action_insert",
            "attempt_insert",
            "channel_inventory_upsert",
            "channel_binding_update",
        ],
    )
    rendered = repr(connection.calls)
    assert "base_url" not in rendered
    assert "api_key" not in rendered


def test_existing_listing_can_be_audit_adopted_under_adopted_channel() -> None:
    connection = ContractConnection()
    connection.queue(
        "channel_binding_select",
        channel_row(state="verified", channel_id=42, create_action_id=401),
    )
    connection.queue("action_insert", {"id": 402})
    adopted_row = listing_row(state="verified", listing_id=501, create_action_id=402)
    adopted_row["a6_channel_id"] = 42
    connection.queue("listing_binding_insert", adopted_row)
    desired = listing_desired()
    observation = dict(desired["remote_listing"])
    observation.update({"id": 501, "channel_id": 42, "status": 2})

    adopted = store(connection).adopt_listing(
        ListingAdoptionIntent(
            managed_uid=LISTING_UID,
            merchant_id=5,
            channel_binding_id=1,
            a6_channel_id=42,
            a6_listing_id=501,
            idempotency_key=f"managed-listing-adopt:{LISTING_UID}:501",
            desired_state=desired,
            observation=observation,
        )
    )

    assert adopted.control_state == "verified"
    assert adopted.a6_listing_id == 501
    _assert_subsequence(
        _labels(connection),
        [
            "channel_binding_select",
            "listing_binding_select",
            "action_insert",
            "attempt_insert",
            "listing_inventory_upsert",
            "listing_binding_insert",
        ],
    )


def test_create_unknown_updates_binding_action_and_attempt_in_one_transaction() -> None:
    connection = ContractConnection()
    connection.queue(
        "channel_binding_select",
        channel_row(state="creating", create_action_id=101, last_action_id=101),
    )
    connection.queue(
        "action_select",
        {
            "id": 101,
            "action_type": "create_managed_channel",
            "entity_type": "a6_managed_channel",
            "entity_id": CHANNEL_UID,
            "idempotency_key": CHANNEL_CREATE_KEY,
            "status": "started",
            "desired_state": channel_desired(),
        },
    )
    connection.queue(
        "channel_binding_update",
        channel_row(state="create_unknown", create_action_id=101, last_action_id=101),
    )
    database = store(connection)

    updated = database.mark_channel_create_unknown(
        channel_binding(state="creating", create_action_id=101, last_action_id=101),
        "managed_channel_fixture_timeout",
    )

    assert updated.control_state == "create_unknown"
    _assert_subsequence(
        _labels(connection),
        ["channel_binding_update", "action_update", "attempt_update"],
    )
    assert connection.commits == 1
    assert connection.rollbacks == 0
    assert connection.closes == connection.opens == 1


def test_known_create_failure_rotates_key_and_finishes_audit_as_failed() -> None:
    connection = ContractConnection()
    connection.queue(
        "channel_binding_select",
        channel_row(state="creating", create_action_id=101, last_action_id=101),
    )
    connection.queue(
        "action_select",
        {
            "id": 101,
            "action_type": "create_managed_channel",
            "entity_type": "a6_managed_channel",
            "entity_id": CHANNEL_UID,
            "idempotency_key": CHANNEL_CREATE_KEY,
            "status": "started",
            "desired_state": channel_desired(),
        },
    )
    connection.queue(
        "channel_binding_update",
        {
            **channel_row(state="error", last_action_id=101),
            "create_idempotency_key": f"{CHANNEL_CREATE_KEY}:retry:101",
            "observed_state": {"result": "rejected"},
            "last_error_class": "supplier_apply_failed",
        },
    )
    database = store(connection)

    failed = database.mark_channel_create_failed(
        channel_binding(state="creating", create_action_id=101, last_action_id=101),
        "supplier_apply_failed",
        {"result": "rejected"},
    )

    assert failed.control_state == "error"
    assert failed.create_action_id is None
    assert failed.create_idempotency_key.endswith(":retry:101")
    _assert_subsequence(
        _labels(connection),
        [
            "channel_binding_select",
            "action_select",
            "channel_binding_update",
            "action_update",
            "attempt_update",
        ],
    )
    selects = [
        query
        for label, query, _params in connection.calls
        if label in {"channel_binding_select", "action_select"}
    ]
    assert all("FOR UPDATE" in query for query in selects)
    binding_sql = next(
        query for label, query, _params in connection.calls if label == "channel_binding_update"
    )
    assert "create_action_id = NULL" in binding_sql
    assert "control_state IN ('creating', 'verifying')" in binding_sql


@pytest.mark.parametrize(
    ("action_override", "locked_override"),
    [
        ({"action_type": "update_managed_channel"}, {}),
        ({"entity_type": "a6_managed_listing"}, {}),
        ({"entity_id": "a6-managed:channel:m5:other"}, {}),
        ({"idempotency_key": "managed-channel-create:other"}, {}),
        ({"status": "unknown"}, {}),
        ({"desired_state": {"spec_fingerprint": "different"}}, {}),
        ({}, {"create_action_id": 102}),
        ({}, {"desired_state": {"spec_fingerprint": "changed"}}),
    ],
)
def test_known_create_failure_rejects_action_or_binding_conflict_before_writes(
    action_override: dict[str, Any], locked_override: dict[str, Any]
) -> None:
    connection = ContractConnection()
    locked = channel_row(state="creating", create_action_id=101, last_action_id=101)
    locked.update(locked_override)
    action = {
        "id": 101,
        "action_type": "create_managed_channel",
        "entity_type": "a6_managed_channel",
        "entity_id": CHANNEL_UID,
        "idempotency_key": CHANNEL_CREATE_KEY,
        "status": "started",
        "desired_state": channel_desired(),
    }
    action.update(action_override)
    connection.queue("channel_binding_select", locked)
    connection.queue("action_select", action)
    database = store(connection)

    with pytest.raises(managed_store.ManagedListingError) as error:
        database.mark_channel_create_failed(
            channel_binding(state="creating", create_action_id=101, last_action_id=101),
            "supplier_apply_failed",
            {"result": "rejected"},
        )

    assert str(error.value) == "managed_create_action_conflict"
    assert _labels(connection) == ["channel_binding_select", "action_select"]
    assert connection.commits == 0
    assert connection.rollbacks == 1


def test_resolve_unknown_locks_binding_and_action_then_rotates_retry_key() -> None:
    connection = ContractConnection()
    unknown = channel_row(
        state="create_unknown",
        create_action_id=101,
        last_action_id=101,
        last_error_class="supplier_apply_failed",
    )
    connection.queue("channel_binding_select", unknown)
    connection.queue(
        "action_select",
        {
            "id": 101,
            "action_type": "create_managed_channel",
            "entity_type": "a6_managed_channel",
            "entity_id": CHANNEL_UID,
            "idempotency_key": CHANNEL_CREATE_KEY,
            "status": "unknown",
            "desired_state": channel_desired(),
            "error_class": "supplier_apply_failed",
        },
    )
    connection.queue(
        "channel_binding_update",
        {
            **channel_row(state="error", last_action_id=101),
            "create_idempotency_key": f"{CHANNEL_CREATE_KEY}:retry:101",
            "observed_state": {"result": "confirmed_absent"},
            "last_error_class": "managed_channel_create_confirmed_absent",
        },
    )
    database = store(connection)

    resolved = database.resolve_channel_create_unknown(
        channel_binding(
            state="create_unknown",
            create_action_id=101,
            last_action_id=101,
            last_error_class="supplier_apply_failed",
        ),
        channel_desired(),
        {"result": "confirmed_absent"},
        "managed_channel_create_confirmed_absent",
    )

    assert resolved.control_state == "error"
    assert resolved.create_action_id is None
    _assert_subsequence(
        _labels(connection),
        [
            "channel_binding_select",
            "action_select",
            "channel_binding_update",
            "action_update",
            "attempt_update",
        ],
    )
    selects = [
        query
        for label, query, _params in connection.calls
        if label in {"channel_binding_select", "action_select"}
    ]
    assert all("FOR UPDATE" in query for query in selects)
    binding_update = next(
        query for label, query, _params in connection.calls if label == "channel_binding_update"
    )
    assert "child.channel_binding_id = target.id" in binding_update


def test_listing_unknown_resolution_keeps_verified_channel_reference() -> None:
    sql = managed_store._resolve_binding_create_unknown_sql("listing")

    assert "a6_listing_id IS NULL" in sql
    assert "a6_channel_id IS NULL" not in sql


def test_verify_channel_upserts_inventory_before_binding_and_completes_audit() -> None:
    connection = ContractConnection()
    connection.queue(
        "channel_binding_select",
        channel_row(state="verifying", create_action_id=101, last_action_id=101),
    )
    connection.queue(
        "action_select",
        {
            "id": 101,
            "action_type": "create_managed_channel",
            "entity_type": "a6_managed_channel",
            "entity_id": CHANNEL_UID,
            "idempotency_key": CHANNEL_CREATE_KEY,
            "status": "started",
            "desired_state": channel_desired(),
        },
    )
    connection.queue("channel_inventory_upsert", {"merchant_id": 5, "a6_channel_id": 700})
    connection.queue(
        "channel_binding_update",
        channel_row(state="verified", channel_id=700, create_action_id=101, last_action_id=101),
    )
    database = store(connection)
    observation = {
        "id": 700,
        "supplier_id": 205,
        "name": "managed-a6m-channel-fixture",
        "nickname": "managed-a6m-channel-fixture",
        "channel_type": 1,
        "status": 1,
        "description": f"[managed_uid:{CHANNEL_UID}] fixture",
    }

    verified = database.verify_channel(
        channel_binding(state="verifying", create_action_id=101, last_action_id=101),
        700,
        observation,
    )

    assert verified.control_state == "verified"
    assert verified.a6_channel_id == 700
    _assert_subsequence(
        _labels(connection),
        [
            "channel_inventory_upsert",
            "channel_binding_update",
            "action_update",
            "attempt_update",
        ],
    )
    assert _labels(connection).index("channel_inventory_upsert") < _labels(connection).index(
        "channel_binding_update"
    )
    assert connection.commits == 1
    assert connection.rollbacks == 0


def test_verify_listing_upserts_inventory_before_binding_and_completes_audit() -> None:
    connection = ContractConnection()
    connection.queue(
        "listing_binding_select",
        listing_row(state="verifying", create_action_id=202, last_action_id=202),
    )
    connection.queue(
        "action_select",
        {
            "id": 202,
            "action_type": "create_managed_listing",
            "entity_type": "a6_managed_listing",
            "entity_id": LISTING_UID,
            "idempotency_key": LISTING_CREATE_KEY,
            "status": "started",
            "desired_state": listing_desired(),
        },
    )
    connection.queue("listing_inventory_upsert", {"merchant_id": 5, "a6_listing_id": 900})
    connection.queue(
        "listing_binding_update",
        listing_row(state="verified", listing_id=900, create_action_id=202, last_action_id=202),
    )
    database = store(connection)
    observation = {"id": 900, "status": 2, **listing_desired()["remote_listing"]}

    verified = database.verify_listing(
        listing_binding(state="verifying", create_action_id=202, last_action_id=202),
        900,
        observation,
    )

    assert verified.control_state == "verified"
    assert verified.a6_listing_id == 900
    _assert_subsequence(
        _labels(connection),
        [
            "listing_inventory_upsert",
            "listing_binding_update",
            "action_update",
            "attempt_update",
        ],
    )
    assert _labels(connection).index("listing_inventory_upsert") < _labels(connection).index(
        "listing_binding_update"
    )
    assert connection.commits == 1
    assert connection.rollbacks == 0


def test_listing_action_claim_and_unknown_are_fully_audited() -> None:
    connection = ContractConnection()
    pending_binding = listing_row(state="updating", listing_id=900, last_action_id=303)
    connection.queue("listing_binding_select", listing_row(state="verified", listing_id=900))
    connection.queue("action_select", None)
    connection.queue("action_insert", action_row())
    connection.queue("listing_binding_update", pending_binding)
    connection.queue("attempt_next", {"attempt_no": 1})
    database = store(connection)

    action = database.begin_action(
        "listing",
        LISTING_UID,
        "update",
        {"payload": {"input_price_micros": 1_100_000}},
        f"managed-action:{LISTING_UID}:update:fixture",
    )

    assert isinstance(action, ManagedAction)
    assert action.operation == "update"
    _assert_subsequence(
        _labels(connection),
        [
            "listing_binding_select",
            "action_insert",
            "listing_binding_update",
            "attempt_insert",
        ],
    )
    calls_after_claim = len(connection.calls)

    connection.queue("listing_binding_select", pending_binding)
    connection.queue("action_select", action_row())
    connection.queue(
        "listing_binding_update",
        listing_row(state="update_unknown", listing_id=900, last_action_id=303),
    )
    database.mark_action_unknown(action, "managed_listing_fixture_timeout")

    unknown_labels = _labels(connection)[calls_after_claim:]
    _assert_subsequence(
        unknown_labels,
        ["listing_binding_update", "action_update", "attempt_update"],
    )
    assert connection.commits == 2
    assert connection.rollbacks == 0
    assert connection.closes == connection.opens == 2


def test_claim_rolls_back_when_attempt_audit_insert_fails() -> None:
    connection = ContractConnection()
    connection.queue("channel_binding_select", channel_row())
    connection.queue("action_insert", {"id": 101, "action_id": 101, "status": "started"})
    connection.queue(
        "channel_binding_update",
        channel_row(state="creating", create_action_id=101, last_action_id=101),
    )
    connection.queue("attempt_next", {"attempt_no": 1})
    connection.fail_on = "attempt_insert"
    database = store(connection)

    with pytest.raises(Exception, match="fixture SQL failure at attempt_insert"):
        database.claim_channel_create(channel_binding())

    assert connection.commits == 0
    assert connection.rollbacks == 1
    assert connection.closes == connection.opens == 1
