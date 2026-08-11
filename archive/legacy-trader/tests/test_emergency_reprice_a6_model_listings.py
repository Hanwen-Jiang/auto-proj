from __future__ import annotations

import json
from copy import deepcopy
from decimal import ROUND_CEILING, Decimal
from pathlib import Path

import pytest

from scripts import emergency_pause_a6_model_listings as pause
from scripts import emergency_reprice_a6_model_listings as reprice
from scripts import reconcile_emergency_pause_audits as pause_audit


def target(
    *,
    merchant_id: int = 17,
    user_id: int = 11117,
    listing_id: int = 1397,
    delta: Decimal | None = Decimal("0.5"),
) -> reprice.Target:
    return reprice.Target(
        merchant_id=merchant_id,
        a6_user_id=user_id,
        session_ref=Path(f"/sessions/merchant-{merchant_id}.json"),
        managed_uid=f"listing-{merchant_id}",
        listing_id=listing_id,
        channel_id=listing_id + 100,
        model="gpt-5.6-sol",
        observed_delta=delta,
        local_prices=(10_000, 60_000, 1_000, 12_500),
    )


def listing(subject: reprice.Target, prices: tuple[int, int, int, int]) -> dict:
    return {
        "id": subject.listing_id,
        "channel_id": subject.channel_id,
        "own_model": subject.model,
        "platform_model": subject.model,
        "status": 0,
        "availability": 0,
        "visibility": 0,
        "charge_type": "token",
        "display_name": subject.model,
        "description": "Team source",
        **dict(zip(reprice._PRICE_FIELDS, prices, strict=True)),
    }


def test_pause_status_zero_is_confirmed_down() -> None:
    assert pause.is_down({"status": 0, "availability": 0, "visibility": 0}) is True
    assert pause.is_down({"status": "0", "availability": "0", "visibility": "0"}) is True
    assert pause.is_down({"status": 0, "availability": 1, "visibility": 0}) is False
    assert pause.is_down({"status": 0}) is False
    assert pause.is_down({"status": None}) is False
    assert pause.is_down({}) is False
    assert pause.is_down({"status": 2}) is False


class PauseAuditCursor:
    def __init__(self, connection) -> None:
        self.connection = connection
        self.rowcount = 1
        self.row = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, query, params=()):
        normalized = " ".join(str(query).split())
        self.connection.calls.append((normalized, tuple(params)))
        if normalized.startswith("SELECT id, status, action_type"):
            self.row = dict(self.connection.action)
        elif normalized.startswith("SELECT coalesce(max(attempt_no)"):
            self.row = {"attempt_no": 4}
        else:
            self.row = None

    def fetchone(self):
        return self.row


class PauseAuditConnection:
    def __init__(self, subject) -> None:
        self.subject = subject
        self.calls = []
        self.commits = 0
        self.rollbacks = 0
        self.action = {
            "id": 71,
            "status": "failed",
            "action_type": "emergency_model_pause",
            "entity_type": "a6_supplier_listing",
            "entity_id": f"merchant:{subject.merchant_id}:listing:{subject.listing_id}",
            "desired_state": {
                "model": subject.model,
                "merchant_id": subject.merchant_id,
                "listing_id": subject.listing_id,
                "channel_id": subject.channel_id,
                "status": 0,
                "reason": "cost_band_ceiling_exceeded",
            },
        }

    def cursor(self):
        return PauseAuditCursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def test_failed_pause_audit_replays_only_from_exact_remote_down_readback() -> None:
    subject = target()
    remote = listing(subject, subject.local_prices)
    connection = PauseAuditConnection(subject)

    pause_audit.reconcile_confirmed_down(
        connection,
        action_id=71,
        target=subject,
        remote=remote,
    )

    assert connection.commits == 1
    rendered = "\n".join(query for query, _params in connection.calls)
    assert "status = 'completed'" in rendered
    assert "WHERE id = %s AND status = 'failed'" in rendered
    assert "INSERT INTO a6_trade.trade_control_action_attempts" in rendered
    assert "status = 0, availability = 0, visibility = 0" in rendered
    assert not any("takedown_supplier_listing" in query for query, _params in connection.calls)


@pytest.mark.parametrize(
    "changes",
    (
        {"status": 2},
        {"availability": 1},
        {"visibility": 1},
        {"visibility": None},
    ),
)
def test_failed_pause_audit_replay_rejects_incomplete_remote_down_state(changes) -> None:
    subject = target()
    remote = {**listing(subject, subject.local_prices), **changes}
    connection = PauseAuditConnection(subject)

    with pytest.raises(RuntimeError, match="emergency_pause_reconcile_not_confirmed_down"):
        pause_audit.reconcile_confirmed_down(
            connection,
            action_id=71,
            target=subject,
            remote=remote,
        )

    assert connection.calls == []
    assert connection.commits == 0


@pytest.mark.parametrize(
    ("delta", "expected_input"),
    (
        ("0.500000", 85_058),
        ("0.424089", 89_592),
        ("0.387587", 91_949),
        ("0", 127_587),
    ),
)
def test_price_plan_ceil_preserves_twenty_percent_landed_cost_margin(
    delta: str,
    expected_input: int,
) -> None:
    cost = Decimal("0.0177204")
    conversion = Decimal("0.833333333333")

    plan = reprice.build_price_plan(
        (0, 0, 0, 0),
        cost_multiplier=cost,
        delta=Decimal(delta),
        conversion_factor=conversion,
    )

    exact_input = (
        Decimal(5_000_000) * Decimal("1.20") * cost / conversion / (Decimal("1") + Decimal(delta))
    ).to_integral_value(rounding=ROUND_CEILING)
    assert plan.prices[0] == int(exact_input) == expected_input
    assert plan.prices == (
        expected_input,
        expected_input * 6,
        (expected_input + 5) // 10,
        (expected_input * 5 + 2) // 4,
    )
    assert plan.final_multiplier >= Decimal("1.20") * plan.landed_cost_multiplier
    assert plan.profit_gate()["passed"] is True


def test_price_plan_normalizes_up_without_decreasing_any_existing_price() -> None:
    current = (90_000, 600_000, 9_500, 130_000)

    plan = reprice.build_price_plan(
        current,
        cost_multiplier=Decimal("0.01"),
        delta=Decimal("0.5"),
    )

    assert plan.prices == (104_000, 624_000, 10_400, 130_000)
    assert all(after >= before for before, after in zip(current, plan.prices, strict=True))


def test_margin_below_twenty_percent_is_rejected() -> None:
    with pytest.raises(RuntimeError, match="emergency_reprice_margin_invalid"):
        reprice.build_price_plan(
            (0, 0, 0, 0),
            cost_multiplier=Decimal("0.1"),
            delta=Decimal("0.5"),
            margin=Decimal("0.199999"),
        )


def test_missing_observed_delta_uses_conservative_zero() -> None:
    subject = target(delta=None)
    plan = reprice.build_price_plan(
        subject.local_prices,
        cost_multiplier=Decimal("0.0177204"),
        delta=subject.effective_delta,
    )

    desired = reprice.desired_state(subject, plan)

    assert subject.effective_delta == 0
    assert desired["delta"] == "0"
    assert desired["delta_source"] == "conservative_zero"
    assert desired["prices_micros"] == {
        "input": 127_587,
        "output": 765_522,
        "cache_read": 12_759,
        "cache_write": 159_484,
    }
    assert desired["profit_gate"]["passed"] is True
    assert Decimal(desired["profit_gate"]["cost_markup"]) >= Decimal("0.20")


class AuditCursor:
    def __init__(self, connection: AuditConnection) -> None:
        self.connection = connection
        self.rowcount = 1
        self.row: dict | None = None

    def __enter__(self) -> AuditCursor:
        return self

    def __exit__(self, *_args) -> None:
        return None

    def execute(self, query, params=()) -> None:
        normalized = " ".join(str(query).split())
        self.connection.calls.append((normalized, params))
        if normalized.startswith("INSERT INTO a6_trade.trade_control_actions"):
            self.row = {"id": 71}
        elif normalized.startswith("SELECT coalesce(max(attempt_no)"):
            self.row = {"attempt_no": 3}
        else:
            self.row = None

    def fetchone(self):
        return self.row


class AuditConnection:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple]] = []
        self.commits = 0
        self.rollbacks = 0

    def cursor(self) -> AuditCursor:
        return AuditCursor(self)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


def test_prepare_action_commits_price_band_audit_for_one_listing() -> None:
    subject = target()
    before = listing(subject, subject.local_prices)
    plan = reprice.build_price_plan(
        subject.local_prices,
        cost_multiplier=Decimal("0.0177204"),
        delta=subject.effective_delta,
    )
    connection = AuditConnection()

    action_id, attempt_no = reprice.prepare_action(
        connection,
        subject,
        before,
        plan,
        config_version="test-v1",
    )

    assert (action_id, attempt_no) == (71, 3)
    assert connection.commits == 1
    assert connection.rollbacks == 0
    action_query, action_params = connection.calls[0]
    assert "'apply_seller_price_band', 'a6_managed_listing'" in action_query
    desired = json.loads(action_params[2])
    assert desired["merchant_id"] == subject.merchant_id
    assert desired["a6_panel_multiplier"] == str(Decimal(plan.prices[0]) / 5_000_000)
    assert desired["prices_micros"]["input"] == plan.prices[0]
    assert "settlement_fee_rate" not in desired
    assert desired["profit_gate"]["passed"] is True


def test_audit_observation_is_accepted_by_publication_readback_contract() -> None:
    subject = target()
    remote = listing(subject, subject.local_prices)

    observed = reprice.audit_observation(subject, remote, action="update")

    assert observed["listing"]["state"] == "verified"
    assert observed["listing"]["error_class"] is None
    assert observed["listing"]["readback"]["status"] == 0


def test_listing_source_hash_matches_collector_payload_contract() -> None:
    subject = target()
    remote = listing(subject, subject.local_prices)
    expected_payload = {
        "merchant_user_id": subject.a6_user_id,
        "a6_listing_id": subject.listing_id,
        "a6_channel_id": subject.channel_id,
        "own_model": subject.model,
        "platform_model": subject.model,
        "charge_type": "token",
        "input_price_micros": 10_000,
        "output_price_micros": 60_000,
        "cache_read_price_micros": 1_000,
        "cache_write_price_micros": 12_500,
        "status": 0,
        "availability": 0,
        "visibility": 0,
        "display_name": subject.model,
        "description": "Team source",
    }
    expected = reprice.hashlib.sha256(
        json.dumps(
            expected_payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()

    assert reprice.listing_source_hash(subject, remote) == expected


class ListingClient:
    def __init__(
        self,
        before: dict,
        *,
        lose_update_response: bool = False,
        activate_on_update: bool = False,
        activate_after_update_response: bool = False,
        lose_takedown_response: bool = False,
        takedown_keeps_active: bool = False,
    ) -> None:
        self.remote = deepcopy(before)
        self.lose_update_response = lose_update_response
        self.activate_on_update = activate_on_update
        self.activate_after_update_response = activate_after_update_response
        self.lose_takedown_response = lose_takedown_response
        self.takedown_keeps_active = takedown_keeps_active
        self.updates: list[tuple[int, int, dict]] = []
        self.takedowns: list[tuple[int, int]] = []
        self.reads = 0

    def update_supplier_listing(self, user_id: int, listing_id: int, payload: dict) -> dict:
        self.updates.append((user_id, listing_id, deepcopy(payload)))
        self.remote.update(payload)
        if self.activate_on_update:
            self.remote.update({"status": 2, "availability": 1, "visibility": 1})
        if self.lose_update_response:
            raise RuntimeError("supplier_listing_update_response_lost")
        response = deepcopy(self.remote)
        if self.activate_after_update_response:
            self.remote.update({"status": 2, "availability": 1, "visibility": 1})
        return response

    def takedown_supplier_listing(self, user_id: int, listing_id: int) -> dict:
        self.takedowns.append((user_id, listing_id))
        if not self.takedown_keeps_active:
            self.remote.update({"status": 0, "availability": 0, "visibility": 0})
        if self.lose_takedown_response:
            raise RuntimeError("supplier_listing_takedown_response_lost")
        return {"id": listing_id}

    def supplier_listings(self, _user_id: int, *, page: int, size: int) -> dict:
        assert page == 1 and size == 100
        self.reads += 1
        return {"items": [deepcopy(self.remote)], "total": 1}


def test_process_target_puts_full_prices_polls_get_and_keeps_listing_down(monkeypatch) -> None:
    subject = target()
    before = listing(subject, subject.local_prices)
    plan = reprice.build_price_plan(
        subject.local_prices,
        cost_multiplier=Decimal("0.0177204"),
        delta=subject.effective_delta,
    )
    client = ListingClient(before)
    completed: list[dict] = []
    monkeypatch.setattr(reprice, "prepare_action", lambda *_args, **_kwargs: (71, 1))
    monkeypatch.setattr(
        reprice,
        "complete_action",
        lambda _connection, _target, _action, _attempt, after, _elapsed, **_kwargs: (
            completed.append(deepcopy(after))
        ),
    )
    monkeypatch.setattr(reprice, "fail_action", lambda *_args, **_kwargs: pytest.fail())

    result = reprice.process_target(
        object(),
        subject,
        client,
        before,
        cost_multiplier=Decimal("0.0177204"),
        margin=Decimal("0.20"),
        conversion_factor=Decimal("0.833333333333"),
        headroom=Decimal("0"),
        config_version="test-v1",
        readback_attempts=2,
        readback_interval_seconds=0,
    )

    assert result["confirmed"] is True
    assert client.reads == 1
    assert len(client.updates) == 1
    payload = client.updates[0][2]
    assert tuple(payload[field] for field in reprice._PRICE_FIELDS) == plan.prices
    assert completed[0]["status"] == 0
    assert tuple(completed[0][field] for field in reprice._PRICE_FIELDS) == plan.prices


def test_process_target_recovers_lost_put_response_from_exact_get(monkeypatch) -> None:
    subject = target()
    before = listing(subject, subject.local_prices)
    client = ListingClient(before, lose_update_response=True)
    completed: list[dict] = []
    monkeypatch.setattr(reprice, "prepare_action", lambda *_args, **_kwargs: (71, 1))
    monkeypatch.setattr(
        reprice,
        "complete_action",
        lambda _connection, _target, _action, _attempt, after, _elapsed, **_kwargs: (
            completed.append(deepcopy(after))
        ),
    )
    monkeypatch.setattr(reprice, "fail_action", lambda *_args, **_kwargs: pytest.fail())

    result = reprice.process_target(
        object(),
        subject,
        client,
        before,
        cost_multiplier=Decimal("0.0177204"),
        margin=Decimal("0.20"),
        conversion_factor=Decimal("0.833333333333"),
        headroom=Decimal("0"),
        config_version="test-v1",
        readback_attempts=2,
        readback_interval_seconds=0,
    )

    assert result["confirmed"] is True
    assert len(client.updates) == 1
    assert client.reads == 1
    assert completed[0]["status"] == 0


def test_process_target_immediately_takes_down_listing_activated_by_price_update(
    monkeypatch,
) -> None:
    subject = target()
    before = listing(subject, subject.local_prices)
    client = ListingClient(before, activate_on_update=True)
    completed: list[tuple[dict, dict]] = []
    monkeypatch.setattr(reprice, "prepare_action", lambda *_args, **_kwargs: (71, 1))
    monkeypatch.setattr(
        reprice,
        "complete_action",
        lambda _connection, _target, _action, _attempt, after, _elapsed, **kwargs: completed.append(
            (deepcopy(after), deepcopy(kwargs))
        ),
    )
    monkeypatch.setattr(reprice, "fail_action", lambda *_args, **_kwargs: pytest.fail())

    result = reprice.process_target(
        object(),
        subject,
        client,
        before,
        cost_multiplier=Decimal("0.0187204"),
        margin=Decimal("0.20"),
        conversion_factor=Decimal("0.833333333333"),
        headroom=Decimal("0"),
        config_version="test-v1",
        readback_attempts=2,
        readback_interval_seconds=0,
    )

    assert result["confirmed"] is True
    assert result["activation_recovered"] is True
    assert result["action"] == "reprice_and_takedown"
    assert client.takedowns == [(subject.a6_user_id, subject.listing_id)]
    assert completed[0][0]["status"] == 0
    assert completed[0][1]["operation"] == "update_then_takedown"
    assert completed[0][1]["activation_recovery"]["final_down_confirmed"] is True


def test_process_target_takes_down_activation_first_observed_by_get(monkeypatch) -> None:
    subject = target()
    before = listing(subject, subject.local_prices)
    client = ListingClient(before, activate_after_update_response=True)
    completed: list[tuple[dict, dict]] = []
    monkeypatch.setattr(reprice, "prepare_action", lambda *_args, **_kwargs: (71, 1))
    monkeypatch.setattr(
        reprice,
        "complete_action",
        lambda _connection, _target, _action, _attempt, after, _elapsed, **kwargs: completed.append(
            (deepcopy(after), deepcopy(kwargs))
        ),
    )
    monkeypatch.setattr(reprice, "fail_action", lambda *_args, **_kwargs: pytest.fail())

    result = reprice.process_target(
        object(),
        subject,
        client,
        before,
        cost_multiplier=Decimal("0.0187204"),
        margin=Decimal("0.20"),
        conversion_factor=Decimal("0.833333333333"),
        headroom=Decimal("0"),
        config_version="test-v1",
        readback_attempts=2,
        readback_interval_seconds=0,
    )

    assert result["confirmed"] is True
    assert result["activation_recovered"] is True
    assert client.takedowns == [(subject.a6_user_id, subject.listing_id)]
    assert completed[0][0]["status"] == 0
    recovery = completed[0][1]["activation_recovery"]
    assert recovery["source"] == "get_readback"
    assert recovery["observed_activation"]["status"] == 2
    assert recovery["final_down_confirmed"] is True


def test_process_target_accepts_lost_takedown_response_only_after_down_readback(
    monkeypatch,
) -> None:
    subject = target()
    before = listing(subject, subject.local_prices)
    client = ListingClient(
        before,
        activate_on_update=True,
        lose_takedown_response=True,
    )
    completed: list[tuple[dict, dict]] = []
    monkeypatch.setattr(reprice, "prepare_action", lambda *_args, **_kwargs: (71, 1))
    monkeypatch.setattr(
        reprice,
        "complete_action",
        lambda _connection, _target, _action, _attempt, after, _elapsed, **kwargs: completed.append(
            (deepcopy(after), deepcopy(kwargs))
        ),
    )
    monkeypatch.setattr(reprice, "fail_action", lambda *_args, **_kwargs: pytest.fail())

    result = reprice.process_target(
        object(),
        subject,
        client,
        before,
        cost_multiplier=Decimal("0.0187204"),
        margin=Decimal("0.20"),
        conversion_factor=Decimal("0.833333333333"),
        headroom=Decimal("0"),
        config_version="test-v1",
        readback_attempts=2,
        readback_interval_seconds=0,
    )

    assert result["confirmed"] is True
    assert result["activation_recovered"] is True
    recovery = completed[0][1]["activation_recovery"]
    assert recovery["takedown_attempts"] == 1
    assert recovery["takedown_errors"] == ["supplier_listing_takedown_response_lost"]
    assert recovery["final_down_confirmed"] is True
    assert recovery["final_readback"]["status"] == 0


def test_process_target_fails_and_audits_final_live_readback_when_takedown_sticks(
    monkeypatch,
) -> None:
    subject = target()
    before = listing(subject, subject.local_prices)
    client = ListingClient(
        before,
        activate_on_update=True,
        takedown_keeps_active=True,
    )
    failed: list[tuple[tuple, dict]] = []
    monkeypatch.setattr(reprice, "prepare_action", lambda *_args, **_kwargs: (71, 1))
    monkeypatch.setattr(reprice, "complete_action", lambda *_args, **_kwargs: pytest.fail())
    monkeypatch.setattr(
        reprice,
        "fail_action",
        lambda *args, **kwargs: failed.append((deepcopy(args), deepcopy(kwargs))),
    )

    result = reprice.process_target(
        object(),
        subject,
        client,
        before,
        cost_multiplier=Decimal("0.0187204"),
        margin=Decimal("0.20"),
        conversion_factor=Decimal("0.833333333333"),
        headroom=Decimal("0"),
        config_version="test-v1",
        readback_attempts=2,
        readback_interval_seconds=0,
    )

    assert result == {
        "merchant_id": subject.merchant_id,
        "listing_id": subject.listing_id,
        "action": "error",
        "confirmed": False,
        "activation_recovered": False,
        "error_class": "emergency_reprice_activation_still_live",
    }
    assert client.takedowns == [
        (subject.a6_user_id, subject.listing_id),
        (subject.a6_user_id, subject.listing_id),
    ]
    assert len(failed) == 1
    _args, kwargs = failed[0]
    assert kwargs["after"]["status"] == 2
    recovery = kwargs["activation_recovery"]
    assert recovery["takedown_attempts"] == 2
    assert recovery["final_down_confirmed"] is False
    assert recovery["observed_activation"]["status"] == 2
    assert recovery["final_readback"]["status"] == 2


class RollbackConnection:
    def __init__(self) -> None:
        self.rollbacks = 0

    def rollback(self) -> None:
        self.rollbacks += 1


def test_run_targets_continues_after_one_listing_fails(monkeypatch) -> None:
    first = target(merchant_id=17, user_id=11117, listing_id=1397)
    second = target(merchant_id=19, user_id=11119, listing_id=1442)
    inventories = {
        first.a6_user_id: {first.listing_id: listing(first, first.local_prices)},
        second.a6_user_id: {second.listing_id: listing(second, second.local_prices)},
    }
    monkeypatch.setattr(reprice, "client_for", lambda subject, *_args, **_kwargs: subject)
    monkeypatch.setattr(
        reprice,
        "listing_inventory",
        lambda client, user_id: inventories[user_id],
    )

    def process(_connection, subject, *_args, **_kwargs):
        if subject is first:
            raise RuntimeError("first_listing_failed")
        return {
            "merchant_id": subject.merchant_id,
            "listing_id": subject.listing_id,
            "action": "reprice",
            "confirmed": True,
        }

    monkeypatch.setattr(reprice, "process_target", process)
    connection = RollbackConnection()

    results = reprice.run_targets(
        connection,
        (first, second),
        {},
        timeout=1,
        cost_multiplier=Decimal("0.1"),
        margin=Decimal("0.2"),
        conversion_factor=Decimal("0.833333333333"),
        headroom=Decimal("0"),
        config_version="test-v1",
        readback_attempts=1,
        readback_interval_seconds=0,
    )

    assert [item["confirmed"] for item in results] == [False, True]
    assert results[0]["error_class"] == "first_listing_failed"
    assert connection.rollbacks == 1
