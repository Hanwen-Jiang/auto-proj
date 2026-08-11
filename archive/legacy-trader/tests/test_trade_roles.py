from __future__ import annotations

import json

import pytest

from a6api_auto.trade_roles import (
    RoleAssignment,
    RoleAssignmentError,
    assign_merchant_roles,
)


class FakeCursor:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection
        self.rows = []
        self.one = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, query: str, params=None):
        self.connection.calls.append((query, params))
        self.rows = []
        self.one = None
        normalized = " ".join(query.split())
        if normalized.startswith("SELECT merchant.id, merchant.a6_user_id, merchant.status"):
            self.rows = [
                dict(self.connection.merchants[merchant_id])
                for merchant_id in params[0]
                if merchant_id in self.connection.merchants
            ]
        elif normalized.startswith("UPDATE a6_trade.a6_merchants"):
            roles, merchant_id = params
            self.connection.merchants[merchant_id]["roles"] = list(roles)
        elif normalized.startswith("INSERT INTO a6_trade.trade_control_actions"):
            key = params[0]
            if key not in self.connection.actions:
                self.connection.actions[key] = params
                self.one = (len(self.connection.actions),)
        return self

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.one


class FakeConnection:
    def __init__(self, merchants: dict[int, dict]) -> None:
        self.merchants = merchants
        self.actions = {}
        self.calls = []
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def merchant(merchant_id: int, **overrides) -> dict:
    value = {
        "id": merchant_id,
        "a6_user_id": 1000 + merchant_id,
        "status": "supplier_submitted",
        "roles": ["unassigned"],
        "role_revision": 0,
        "safe_archive_completed": False,
    }
    value.update(overrides)
    return value


def phase1_assignments() -> tuple[RoleAssignment, ...]:
    return (
        RoleAssignment(1, "provider"),
        RoleAssignment(2, "provider"),
        RoleAssignment(3, "seller"),
        RoleAssignment(4, "seller"),
        RoleAssignment(5, "seller"),
        RoleAssignment(6, "observer"),
        RoleAssignment(7, "observer"),
        RoleAssignment(8, "observer"),
    )


def test_phase1_role_assignment_dry_run_rolls_back_without_writes() -> None:
    connection = FakeConnection({index: merchant(index) for index in range(1, 9)})

    result = assign_merchant_roles(connection, phase1_assignments())

    assert result.summary() == {
        "success": True,
        "mode": "dry-run",
        "requested_count": 8,
        "change_count": 8,
        "unchanged_count": 0,
        "audit_action_count": 0,
        "assignments": [item.public() for item in result.decisions],
    }
    assert [item.role for item in result.decisions] == [
        "provider",
        "provider",
        "seller",
        "seller",
        "seller",
        "observer",
        "observer",
        "observer",
    ]
    assert all(row["roles"] == ["unassigned"] for row in connection.merchants.values())
    assert connection.actions == {}
    assert connection.commits == 0
    assert connection.rollbacks == 1
    assert "FOR UPDATE" not in connection.calls[0][0]


def test_phase1_role_assignment_apply_is_atomic_audited_and_idempotent() -> None:
    connection = FakeConnection({index: merchant(index) for index in range(1, 9)})

    first = assign_merchant_roles(connection, phase1_assignments(), apply=True)

    assert first.applied is True
    assert first.summary()["change_count"] == 8
    assert first.audit_action_count == 8
    assert connection.commits == 1
    assert connection.rollbacks == 0
    assert "FOR UPDATE" in connection.calls[0][0]
    assert [connection.merchants[index]["roles"] for index in range(1, 9)] == [
        ["seller", "provider"],
        ["seller", "provider"],
        ["seller"],
        ["seller"],
        ["seller"],
        ["seller", "observer"],
        ["seller", "observer"],
        ["seller", "observer"],
    ]
    assert set(connection.actions) == {
        f"assign-role:merchant:{item.merchant_id}:revision:0:{item.role}:v2"
        for item in phase1_assignments()
    }
    audit_parameters = repr(tuple(connection.actions.values()))
    assert "password" not in audit_parameters
    assert "cookie" not in audit_parameters
    assert "api_key" not in audit_parameters
    assert "session_ref" not in audit_parameters
    first_action = connection.actions["assign-role:merchant:1:revision:0:provider:v2"]
    assert json.loads(first_action[2]) == {"roles": ["seller", "provider"]}
    assert json.loads(first_action[3]) == {
        "roles": ["unassigned"],
    }

    second = assign_merchant_roles(connection, phase1_assignments(), apply=True)

    assert second.summary()["change_count"] == 0
    assert second.summary()["unchanged_count"] == 8
    assert second.audit_action_count == 0
    assert len(connection.actions) == 8
    assert connection.commits == 2
    assert connection.rollbacks == 0


@pytest.mark.parametrize(
    ("merchants", "error"),
    [
        ({}, "role_assignment_merchant_missing"),
        ({1: merchant(1, status="allocated")}, "role_assignment_merchant_status_invalid"),
        ({1: merchant(1, a6_user_id=None)}, "role_assignment_merchant_identity_missing"),
        (
            {1: merchant(1, roles=["seller", "observer", "provider"])},
            "role_assignment_existing_roles_conflict",
        ),
    ],
)
def test_role_assignment_rejects_invalid_batch_before_any_write(
    merchants: dict[int, dict],
    error: str,
) -> None:
    connection = FakeConnection(merchants)

    with pytest.raises(RoleAssignmentError, match=error):
        assign_merchant_roles(connection, (RoleAssignment(1, "provider"),), apply=True)

    assert not any(
        "UPDATE a6_trade.a6_merchants" in query
        or "INSERT INTO a6_trade.trade_control_actions" in query
        for query, _ in connection.calls
    )
    assert connection.actions == {}
    assert connection.commits == 0
    assert connection.rollbacks == 1


def test_role_assignment_combines_seller_and_provider_with_complete_idempotent_audit() -> None:
    connection = FakeConnection({1: merchant(1)})

    first = assign_merchant_roles(
        connection,
        (RoleAssignment(1, "provider"), RoleAssignment(1, "seller")),
        apply=True,
    )

    assert [item.role for item in first.decisions] == ["seller", "provider"]
    assert all(item.previous_roles == ("unassigned",) for item in first.decisions)
    assert all(item.change_required for item in first.decisions)
    assert connection.merchants[1]["roles"] == ["seller", "provider"]
    assert sum("UPDATE a6_trade.a6_merchants" in query for query, _ in connection.calls) == 1
    assert first.audit_action_count == 2
    assert set(connection.actions) == {
        "assign-role:merchant:1:revision:0:seller:v2",
        "assign-role:merchant:1:revision:0:provider:v2",
    }
    for parameters in connection.actions.values():
        assert json.loads(parameters[2]) == {"roles": ["seller", "provider"]}
        assert json.loads(parameters[3]) == {"roles": ["unassigned"]}
        assert json.loads(parameters[4]) == {"roles": ["seller", "provider"]}

    second = assign_merchant_roles(
        connection,
        (RoleAssignment(1, "seller"), RoleAssignment(1, "provider")),
        apply=True,
    )

    assert second.summary()["change_count"] == 0
    assert second.audit_action_count == 0
    assert len(connection.actions) == 2
    assert sum("UPDATE a6_trade.a6_merchants" in query for query, _ in connection.calls) == 1


def test_role_assignment_adds_duty_to_seller_base_role() -> None:
    connection = FakeConnection({1: merchant(1, roles=["seller"])})

    result = assign_merchant_roles(
        connection,
        (RoleAssignment(1, "provider"),),
        apply=True,
    )

    assert result.summary()["change_count"] == 1
    assert connection.merchants[1]["roles"] == ["seller", "provider"]
    action = connection.actions["assign-role:merchant:1:revision:0:provider:v2"]
    assert json.loads(action[2]) == {"roles": ["seller", "provider"]}
    assert json.loads(action[4]) == {"roles": ["seller", "provider"]}


@pytest.mark.parametrize(
    "roles",
    [
        ["observer", "seller"],
        ["seller", "seller"],
    ],
)
def test_role_assignment_rejects_existing_state_without_exact_seller_base_contract(
    roles: list[str],
) -> None:
    connection = FakeConnection({1: merchant(1, roles=roles)})

    with pytest.raises(RoleAssignmentError, match="role_assignment_existing_roles_invalid"):
        assign_merchant_roles(connection, (RoleAssignment(1, "seller"),), apply=True)

    assert connection.commits == 0
    assert connection.rollbacks == 1


def test_provider_assignment_keeps_retired_provider_provider_only() -> None:
    connection = FakeConnection({1: merchant(1, roles=["provider"])})

    result = assign_merchant_roles(
        connection,
        (RoleAssignment(1, "provider"),),
        apply=True,
    )

    assert result.summary()["change_count"] == 0
    assert connection.merchants[1]["roles"] == ["provider"]


def test_observer_assignment_adds_part_time_duty_to_retired_provider() -> None:
    connection = FakeConnection({1: merchant(1, roles=["provider"])})

    result = assign_merchant_roles(
        connection,
        (RoleAssignment(1, "observer"),),
        apply=True,
    )

    assert result.summary()["change_count"] == 1
    assert connection.merchants[1]["roles"] == ["provider", "observer"]


def test_provider_assignment_keeps_observer_duty_on_retired_observer() -> None:
    connection = FakeConnection({1: merchant(1, roles=["observer"])})

    result = assign_merchant_roles(
        connection,
        (RoleAssignment(1, "provider"),),
        apply=True,
    )

    assert result.summary()["change_count"] == 1
    assert connection.merchants[1]["roles"] == ["provider", "observer"]


def test_safe_archive_terminal_rejects_provider_repromotion() -> None:
    connection = FakeConnection(
        {
            1: merchant(
                1,
                roles=["observer"],
                safe_archive_completed=True,
            )
        }
    )

    with pytest.raises(RoleAssignmentError, match="role_assignment_safe_archive_terminal"):
        assign_merchant_roles(
            connection,
            (RoleAssignment(1, "provider"),),
            apply=True,
        )

    assert connection.merchants[1]["roles"] == ["observer"]
    assert connection.actions == {}


def test_safe_archive_terminal_observer_assignment_repairs_stale_provider_role() -> None:
    connection = FakeConnection(
        {
            1: merchant(
                1,
                roles=["provider", "observer"],
                role_revision=4,
                safe_archive_completed=True,
            )
        }
    )

    result = assign_merchant_roles(
        connection,
        (RoleAssignment(1, "observer"),),
        apply=True,
        config_version="safe-archive-role-integrity-v1",
    )

    assert result.summary()["change_count"] == 1
    assert connection.merchants[1]["roles"] == ["observer"]
    action = connection.actions["assign-role:merchant:1:revision:4:observer:v2"]
    assert json.loads(action[2]) == {"roles": ["observer"]}
    assert json.loads(action[3]) == {"roles": ["provider", "observer"]}


def test_provider_assignment_removes_part_time_observer_duty() -> None:
    connection = FakeConnection({1: merchant(1, roles=["provider", "observer"])})

    result = assign_merchant_roles(
        connection,
        (RoleAssignment(1, "provider"),),
        apply=True,
    )

    assert result.summary()["change_count"] == 1
    assert connection.merchants[1]["roles"] == ["provider"]


def test_role_assignment_later_append_audits_complete_final_roles_once() -> None:
    connection = FakeConnection({1: merchant(1)})
    assign_merchant_roles(
        connection,
        (RoleAssignment(1, "seller"),),
        apply=True,
    )

    appended = assign_merchant_roles(
        connection,
        (RoleAssignment(1, "provider"),),
        apply=True,
    )
    repeated = assign_merchant_roles(
        connection,
        (RoleAssignment(1, "provider"),),
        apply=True,
    )

    assert connection.merchants[1]["roles"] == ["seller", "provider"]
    assert appended.audit_action_count == 1
    assert repeated.audit_action_count == 0
    provider_action = connection.actions["assign-role:merchant:1:revision:0:provider:v2"]
    assert json.loads(provider_action[2]) == {"roles": ["seller", "provider"]}
    assert json.loads(provider_action[3]) == {"roles": ["seller"]}
    assert json.loads(provider_action[4]) == {"roles": ["seller", "provider"]}


def test_role_assignment_rejects_mutually_exclusive_requested_roles() -> None:
    connection = FakeConnection({1: merchant(1)})

    with pytest.raises(RoleAssignmentError, match="role_assignment_request_conflict"):
        assign_merchant_roles(
            connection,
            (RoleAssignment(1, "provider"), RoleAssignment(1, "observer")),
            apply=True,
        )

    assert connection.calls == []
    assert connection.actions == {}


def test_probe_is_not_a_merchant_role() -> None:
    with pytest.raises(RoleAssignmentError, match="role_assignment_role_invalid"):
        RoleAssignment(1, "probe")


def test_role_assignment_switches_duty_and_can_return_to_seller_only() -> None:
    connection = FakeConnection({1: merchant(1, roles=["seller", "provider"], role_revision=7)})

    observer = assign_merchant_roles(
        connection,
        (RoleAssignment(1, "observer"),),
        apply=True,
    )
    connection.merchants[1]["role_revision"] = 8
    seller_only = assign_merchant_roles(
        connection,
        (RoleAssignment(1, "seller"),),
        apply=True,
    )

    assert observer.summary()["change_count"] == 1
    assert seller_only.summary()["change_count"] == 1
    assert connection.merchants[1]["roles"] == ["seller"]
    assert set(connection.actions) >= {
        "assign-role:merchant:1:revision:7:observer:v2",
        "assign-role:merchant:1:revision:8:seller:v2",
    }


def test_role_assignment_deduplicates_identical_requests() -> None:
    connection = FakeConnection({1: merchant(1)})

    result = assign_merchant_roles(
        connection,
        (RoleAssignment(1, "provider"), RoleAssignment(1, "provider")),
    )

    assert result.summary()["requested_count"] == 1
    assert connection.rollbacks == 1


def test_role_assignment_rejects_secret_like_audit_labels() -> None:
    connection = FakeConnection({1: merchant(1)})

    with pytest.raises(RoleAssignmentError, match="role_assignment_config_version_invalid"):
        assign_merchant_roles(
            connection,
            (RoleAssignment(1, "provider"),),
            config_version="secret-rollout-v1",
        )

    assert connection.calls == []


def test_role_assignment_enforces_expected_roles_inside_locked_batch() -> None:
    connection = FakeConnection(
        {
            7: merchant(7, roles=["seller"]),
            19: merchant(19, roles=["seller", "provider"]),
        }
    )

    result = assign_merchant_roles(
        connection,
        (RoleAssignment(7, "provider"), RoleAssignment(19, "seller")),
        apply=True,
        expected_previous_roles={
            7: (("seller",), ("seller", "provider")),
            19: (("seller", "provider"), ("seller",)),
        },
    )

    assert result.summary()["change_count"] == 2
    assert connection.merchants[7]["roles"] == ["seller", "provider"]
    assert connection.merchants[19]["roles"] == ["seller"]
    assert "FOR UPDATE" in connection.calls[0][0]


def test_role_assignment_precondition_failure_is_atomic() -> None:
    connection = FakeConnection(
        {
            7: merchant(7, roles=["seller", "observer"]),
            19: merchant(19, roles=["seller", "provider"]),
        }
    )

    with pytest.raises(RoleAssignmentError, match="role_assignment_precondition_failed"):
        assign_merchant_roles(
            connection,
            (RoleAssignment(7, "provider"), RoleAssignment(19, "seller")),
            apply=True,
            expected_previous_roles={
                7: (("seller",), ("seller", "provider")),
                19: (("seller", "provider"), ("seller",)),
            },
        )

    assert connection.merchants[7]["roles"] == ["seller", "observer"]
    assert connection.merchants[19]["roles"] == ["seller", "provider"]
    assert connection.actions == {}
    assert connection.commits == 0
    assert connection.rollbacks == 1


def test_role_assignment_rejects_incomplete_precondition_map_before_query() -> None:
    connection = FakeConnection({7: merchant(7), 19: merchant(19)})

    with pytest.raises(RoleAssignmentError, match="role_assignment_precondition_invalid"):
        assign_merchant_roles(
            connection,
            (RoleAssignment(7, "provider"), RoleAssignment(19, "seller")),
            expected_previous_roles={7: (("seller",),)},
        )

    assert connection.calls == []
