from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest

from scripts import reconcile_emergency_pause_audits as subject
from scripts.emergency_pause_a6_model_listings import Target


def target(**changes) -> Target:
    values = {
        "merchant_id": 17,
        "a6_user_id": 11117,
        "session_ref": Path("/sessions/merchant-17.json"),
        "listing_id": 1397,
        "channel_id": 1497,
        "model": "gpt-5.6-sol",
    }
    values.update(changes)
    return Target(**values)


def remote_listing(item: Target, **changes) -> dict:
    values = {
        "id": item.listing_id,
        "channel_id": item.channel_id,
        "own_model": item.model,
        "platform_model": item.model,
        "status": 0,
        "availability": 0,
        "visibility": 0,
        "input_price_micros": 134_787,
        "output_price_micros": 134_787,
        "cache_read_price_micros": 134_787,
        "cache_write_price_micros": 134_787,
    }
    values.update(changes)
    return values


def failed_action(item: Target) -> dict:
    return {
        "id": 71,
        "status": "failed",
        "action_type": "emergency_model_pause",
        "entity_type": "a6_supplier_listing",
        "entity_id": f"merchant:{item.merchant_id}:listing:{item.listing_id}",
        "desired_state": {
            "model": item.model,
            "merchant_id": item.merchant_id,
            "listing_id": item.listing_id,
            "channel_id": item.channel_id,
            "status": 0,
            "reason": "cost_band_ceiling_exceeded",
        },
    }


class FakeCursor:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection
        self.row = None
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, query, params=()):
        sql = " ".join(str(query).split())
        params = tuple(params)
        self.connection.calls.append((sql, params))
        self.row = None
        self.rowcount = 0
        if sql.startswith("SELECT id, status, action_type"):
            self.row = deepcopy(self.connection.action)
        elif sql.startswith("SELECT coalesce(max(attempt_no)"):
            self.row = {"attempt_no": len(self.connection.attempts) + 4}
        elif sql.startswith("UPDATE a6_trade.trade_control_actions"):
            expected_entity = params[2]
            if (
                self.connection.cas_succeeds
                and self.connection.action["id"] == params[1]
                and self.connection.action["status"] == "failed"
                and self.connection.action["action_type"] == "emergency_model_pause"
                and self.connection.action["entity_type"] == "a6_supplier_listing"
                and self.connection.action["entity_id"] == expected_entity
            ):
                self.connection.action["status"] = "completed"
                self.connection.action["observed_after"] = json.loads(params[0])
                self.rowcount = 1
        elif sql.startswith("INSERT INTO a6_trade.trade_control_action_attempts"):
            self.connection.attempts.append(
                {
                    "action_id": params[0],
                    "attempt_no": params[1],
                    "status": "completed",
                    "observed_before": json.loads(params[2]),
                    "observed_after": json.loads(params[3]),
                }
            )
            self.rowcount = 1
        elif (
            sql.startswith("UPDATE a6_trade.a6_supplier_listings_current")
            and self.connection.inventory_exists
        ):
            self.connection.inventory_state = {
                "status": 0,
                "availability": 0,
                "visibility": 0,
                "source_hash": params[0],
            }
            self.rowcount = 1

    def fetchone(self):
        return self.row


class FakeConnection:
    def __init__(
        self,
        item: Target,
        *,
        cas_succeeds: bool = True,
        inventory_exists: bool = True,
    ) -> None:
        self.action = failed_action(item)
        self.cas_succeeds = cas_succeeds
        self.inventory_exists = inventory_exists
        self.inventory_state = None
        self.attempts = []
        self.calls = []
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


@pytest.mark.parametrize("value", (0, "0", " 0 "))
def test_remote_confirmed_down_accepts_exact_zero_states(value) -> None:
    assert subject.remote_confirmed_down(
        {"status": value, "availability": value, "visibility": value}
    )


@pytest.mark.parametrize(
    "changes",
    (
        {"status": 2},
        {"availability": 1},
        {"visibility": -1},
        {"status": None},
        {"availability": False},
        {"visibility": 0.5},
        {"visibility": "0.0"},
    ),
)
def test_remote_confirmed_down_rejects_any_state_not_exactly_zero(changes) -> None:
    assert not subject.remote_confirmed_down(
        {"status": 0, "availability": 0, "visibility": 0, **changes}
    )


def test_reconcile_repairs_failed_audit_from_authoritative_remote_readback() -> None:
    item = target()
    remote = remote_listing(item)
    connection = FakeConnection(item)

    subject.reconcile_confirmed_down(
        connection,
        action_id=71,
        target=item,
        remote=remote,
    )

    assert connection.commits == 1
    assert connection.action["status"] == "completed"
    assert connection.action["observed_after"] == subject.public_listing(remote)
    assert connection.attempts == [
        {
            "action_id": 71,
            "attempt_no": 4,
            "status": "completed",
            "observed_before": {
                "config_version": subject.CONFIG_VERSION,
                "listing": subject.public_listing(remote),
                "source": "remote_supplier_listing_readback",
            },
            "observed_after": subject.public_listing(remote),
        }
    ]
    assert connection.inventory_state is not None
    assert connection.inventory_state["status"] == 0
    assert connection.inventory_state["availability"] == 0
    assert connection.inventory_state["visibility"] == 0

    rendered = "\n".join(query for query, _params in connection.calls)
    assert "FOR UPDATE" in rendered
    assert "WHERE id = %s AND status = 'failed'" in rendered
    assert "AND action_type = 'emergency_model_pause'" in rendered
    assert rendered.index("UPDATE a6_trade.trade_control_actions") < rendered.index(
        "INSERT INTO a6_trade.trade_control_action_attempts"
    )


@pytest.mark.parametrize(
    "changes",
    (
        {"status": 2},
        {"availability": 1},
        {"visibility": 1},
        {"visibility": None},
        {"availability": False},
        {"status": 0.5},
    ),
)
def test_reconcile_does_not_touch_database_without_exact_remote_down_readback(changes) -> None:
    item = target()
    connection = FakeConnection(item)

    with pytest.raises(RuntimeError, match="emergency_pause_reconcile_not_confirmed_down"):
        subject.reconcile_confirmed_down(
            connection,
            action_id=71,
            target=item,
            remote=remote_listing(item, **changes),
        )

    assert connection.calls == []
    assert connection.commits == 0
    assert connection.attempts == []


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("status", "completed"),
        ("action_type", "apply_seller_price_band"),
        ("entity_type", "a6_managed_listing"),
        ("entity_id", "merchant:17:listing:9999"),
        ("id", 72),
    ),
)
def test_reconcile_rejects_action_identity_or_status_drift(field, value) -> None:
    item = target()
    connection = FakeConnection(item)
    connection.action[field] = value

    with pytest.raises(RuntimeError, match="emergency_pause_reconcile_action_conflict"):
        subject.reconcile_confirmed_down(
            connection,
            action_id=71,
            target=item,
            remote=remote_listing(item),
        )

    assert connection.commits == 0
    assert connection.attempts == []


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("model", "gpt-5.6-other"),
        ("merchant_id", 18),
        ("listing_id", 1398),
        ("channel_id", 1498),
        ("status", 2),
        ("status", None),
        ("status", False),
        ("reason", "operator_request"),
    ),
)
def test_reconcile_rejects_changed_desired_state(field, value) -> None:
    item = target()
    connection = FakeConnection(item)
    connection.action["desired_state"][field] = value

    with pytest.raises(RuntimeError, match="emergency_pause_reconcile_action_conflict"):
        subject.reconcile_confirmed_down(
            connection,
            action_id=71,
            target=item,
            remote=remote_listing(item),
        )

    assert connection.commits == 0
    assert connection.attempts == []


def test_reconcile_requires_failed_to_completed_compare_and_swap() -> None:
    item = target()
    connection = FakeConnection(item, cas_succeeds=False)

    with pytest.raises(RuntimeError, match="emergency_pause_reconcile_action_cas_mismatch"):
        subject.reconcile_confirmed_down(
            connection,
            action_id=71,
            target=item,
            remote=remote_listing(item),
        )

    assert connection.commits == 0
    assert connection.attempts == []
    rendered = "\n".join(query for query, _params in connection.calls)
    assert "INSERT INTO a6_trade.trade_control_action_attempts" not in rendered
    assert "UPDATE a6_trade.a6_supplier_listings_current" not in rendered


def test_reconcile_rejects_remote_listing_identity_mismatch_before_database_access() -> None:
    item = target()
    connection = FakeConnection(item)

    with pytest.raises(RuntimeError, match="emergency_pause_listing_identity_mismatch"):
        subject.reconcile_confirmed_down(
            connection,
            action_id=71,
            target=item,
            remote=remote_listing(item, channel_id=9999),
        )

    assert connection.calls == []
    assert connection.commits == 0
