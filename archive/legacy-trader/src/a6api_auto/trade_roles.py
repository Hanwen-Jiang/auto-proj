from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

OPERATIONAL_ROLES = frozenset({"seller", "provider", "observer"})
KNOWN_ROLES = OPERATIONAL_ROLES | {"unassigned"}
DUTY_ROLES = frozenset({"provider", "observer"})
ROLE_ORDER = {role: index for index, role in enumerate(("seller", "provider", "observer"))}
ASSIGNABLE_STATUSES = frozenset({"supplier_submitted"})
SAFE_CONFIG_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$")
SECRET_LIKE_LABEL = re.compile(
    r"(?:secret|password|cookie|bearer|token|api[-_]?key)",
    re.IGNORECASE,
)


class Connection(Protocol):
    def cursor(self) -> Any: ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...


class RoleAssignmentError(RuntimeError):
    """Stable role-assignment failure without merchant session material."""


@dataclass(frozen=True)
class RoleAssignment:
    merchant_id: int
    role: str

    def __post_init__(self) -> None:
        if isinstance(self.merchant_id, bool) or self.merchant_id <= 0:
            raise RoleAssignmentError("role_assignment_merchant_id_invalid")
        if self.role not in OPERATIONAL_ROLES:
            raise RoleAssignmentError("role_assignment_role_invalid")


@dataclass(frozen=True)
class RoleAssignmentDecision:
    merchant_id: int
    role: str
    previous_roles: tuple[str, ...]
    change_required: bool

    def public(self) -> dict[str, Any]:
        return {
            "merchant_id": self.merchant_id,
            "role": self.role,
            "previous_roles": list(self.previous_roles),
            "change_required": self.change_required,
        }


@dataclass(frozen=True)
class RoleAssignmentResult:
    applied: bool
    decisions: tuple[RoleAssignmentDecision, ...]
    audit_action_count: int

    def summary(self) -> dict[str, Any]:
        changes = sum(item.change_required for item in self.decisions)
        return {
            "success": True,
            "mode": "applied" if self.applied else "dry-run",
            "requested_count": len(self.decisions),
            "change_count": changes,
            "unchanged_count": len(self.decisions) - changes,
            "audit_action_count": self.audit_action_count,
            "assignments": [item.public() for item in self.decisions],
        }


def _normalize_assignments(
    assignments: Iterable[RoleAssignment],
) -> tuple[RoleAssignment, ...]:
    by_merchant: dict[int, dict[str, RoleAssignment]] = {}
    for assignment in assignments:
        merchant_assignments = by_merchant.setdefault(assignment.merchant_id, {})
        merchant_assignments[assignment.role] = assignment
        requested_roles = set(merchant_assignments)
        if len(requested_roles & DUTY_ROLES) > 1:
            raise RoleAssignmentError("role_assignment_request_conflict")
    if not by_merchant:
        raise RoleAssignmentError("role_assignment_required")
    return tuple(
        by_merchant[merchant_id][role]
        for merchant_id in sorted(by_merchant)
        for role in sorted(by_merchant[merchant_id], key=ROLE_ORDER.__getitem__)
    )


def _rows(cursor: Any) -> list[dict[str, Any]]:
    rows = cursor.fetchall()
    if not rows:
        return []
    if isinstance(rows[0], Mapping):
        return [dict(row) for row in rows]
    names = [getattr(column, "name", column[0]) for column in cursor.description]
    return [dict(zip(names, row, strict=True)) for row in rows]


def _roles(value: Any) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or not value:
        raise RoleAssignmentError("role_assignment_existing_roles_invalid")
    roles = tuple(str(role) for role in value)
    role_set = set(roles)
    if not role_set <= KNOWN_ROLES:
        raise RoleAssignmentError("role_assignment_existing_roles_invalid")
    if roles in {
        ("unassigned",),
        ("seller",),
        ("provider",),
        ("observer",),
        ("provider", "observer"),
    }:
        return roles
    if len(roles) == 2 and roles[0] == "seller" and roles[1] in DUTY_ROLES:
        return roles
    if len(role_set & DUTY_ROLES) > 1:
        raise RoleAssignmentError("role_assignment_existing_roles_conflict")
    raise RoleAssignmentError("role_assignment_existing_roles_invalid")


def _idempotency_key(assignment: RoleAssignment, role_revision: int) -> str:
    return (
        f"assign-role:merchant:{assignment.merchant_id}:revision:{role_revision}:"
        f"{assignment.role}:v2"
    )


def _normalize_expected_previous_roles(
    expected: Mapping[int, Iterable[Iterable[str]]] | None,
    merchant_ids: set[int],
) -> dict[int, frozenset[tuple[str, ...]]] | None:
    if expected is None:
        return None
    normalized: dict[int, frozenset[tuple[str, ...]]] = {}
    try:
        if set(expected) != merchant_ids:
            raise RoleAssignmentError("role_assignment_precondition_invalid")
        for merchant_id, candidates in expected.items():
            allowed = frozenset(_roles(tuple(candidate)) for candidate in candidates)
            if not allowed:
                raise RoleAssignmentError("role_assignment_precondition_invalid")
            normalized[int(merchant_id)] = allowed
    except (TypeError, ValueError, RoleAssignmentError):
        raise RoleAssignmentError("role_assignment_precondition_invalid") from None
    if set(normalized) != merchant_ids:
        raise RoleAssignmentError("role_assignment_precondition_invalid")
    return normalized


def assign_merchant_roles(
    connection: Connection,
    assignments: Iterable[RoleAssignment],
    *,
    apply: bool = False,
    config_version: str = "phase1-role-v1",
    requested_by: str = "a6api-auto-cli",
    expected_previous_roles: Mapping[int, Iterable[Iterable[str]]] | None = None,
) -> RoleAssignmentResult:
    """Validate an entire role batch, then atomically apply and audit it."""
    normalized = _normalize_assignments(assignments)
    expected_roles = _normalize_expected_previous_roles(
        expected_previous_roles,
        {item.merchant_id for item in normalized},
    )
    if not SAFE_CONFIG_VERSION.fullmatch(config_version) or SECRET_LIKE_LABEL.search(
        config_version
    ):
        raise RoleAssignmentError("role_assignment_config_version_invalid")
    if not SAFE_CONFIG_VERSION.fullmatch(requested_by) or SECRET_LIKE_LABEL.search(requested_by):
        raise RoleAssignmentError("role_assignment_requested_by_invalid")

    try:
        with connection.cursor() as cursor:
            lock_clause = " FOR UPDATE" if apply else ""
            cursor.execute(
                """
                SELECT merchant.id, merchant.a6_user_id, merchant.status,
                       merchant.roles, merchant.role_revision,
                       EXISTS (
                           SELECT 1
                           FROM a6_trade.a6_seller_retirement_plans retirement
                           WHERE retirement.merchant_id = merchant.id
                             AND retirement.state = 'completed'
                             AND retirement.archive_mode
                       ) AS safe_archive_completed
                FROM a6_trade.a6_merchants merchant
                WHERE merchant.id = ANY(%s)
                """
                + lock_clause,
                ([item.merchant_id for item in normalized],),
            )
            merchants = {int(row["id"]): row for row in _rows(cursor)}
            if set(merchants) != {item.merchant_id for item in normalized}:
                raise RoleAssignmentError("role_assignment_merchant_missing")

            assignments_by_merchant: dict[int, list[RoleAssignment]] = {}
            for assignment in normalized:
                assignments_by_merchant.setdefault(assignment.merchant_id, []).append(assignment)

            decisions: list[RoleAssignmentDecision] = []
            previous_roles_by_merchant: dict[int, tuple[str, ...]] = {}
            desired_roles_by_merchant: dict[int, tuple[str, ...]] = {}
            role_revision_by_merchant: dict[int, int] = {}
            for merchant_id, merchant_assignments in assignments_by_merchant.items():
                merchant = merchants[merchant_id]
                if merchant.get("status") not in ASSIGNABLE_STATUSES:
                    raise RoleAssignmentError("role_assignment_merchant_status_invalid")
                if merchant.get("a6_user_id") is None:
                    raise RoleAssignmentError("role_assignment_merchant_identity_missing")
                previous_roles = _roles(merchant.get("roles"))
                if expected_roles is not None and previous_roles not in expected_roles[merchant_id]:
                    raise RoleAssignmentError("role_assignment_precondition_failed")
                requested_roles = {item.role for item in merchant_assignments}
                safe_archive_completed = bool(merchant.get("safe_archive_completed"))
                if safe_archive_completed and requested_roles != {"observer"}:
                    raise RoleAssignmentError("role_assignment_safe_archive_terminal")
                requested_duties = requested_roles & DUTY_ROLES
                existing_duties = set(previous_roles) & DUTY_ROLES
                selected_duty = (
                    next(iter(requested_duties))
                    if requested_duties
                    else None
                    if "seller" in requested_roles
                    else next(iter(existing_duties), None)
                )
                if safe_archive_completed:
                    desired_roles = ("observer",)
                elif previous_roles == ("observer",) and requested_roles == {"provider"}:
                    desired_roles = ("provider", "observer")
                elif previous_roles in {
                    ("provider",),
                    ("provider", "observer"),
                } and requested_roles == {"provider"}:
                    desired_roles = ("provider",)
                elif previous_roles in {
                    ("provider",),
                    ("provider", "observer"),
                } and requested_roles == {"observer"}:
                    desired_roles = ("provider", "observer")
                else:
                    desired_roles = ("seller",) + ((selected_duty,) if selected_duty else ())
                try:
                    role_revision = int(merchant.get("role_revision") or 0)
                except (TypeError, ValueError):
                    raise RoleAssignmentError("role_assignment_revision_invalid") from None
                if role_revision < 0:
                    raise RoleAssignmentError("role_assignment_revision_invalid")
                previous_roles_by_merchant[merchant_id] = previous_roles
                desired_roles_by_merchant[merchant_id] = desired_roles
                role_revision_by_merchant[merchant_id] = role_revision
                decisions.extend(
                    RoleAssignmentDecision(
                        merchant_id=merchant_id,
                        role=item.role,
                        previous_roles=previous_roles,
                        change_required=previous_roles != desired_roles,
                    )
                    for item in merchant_assignments
                )

            audit_action_count = 0
            if apply:
                for merchant_id, desired_roles in desired_roles_by_merchant.items():
                    if previous_roles_by_merchant[merchant_id] == desired_roles:
                        continue
                    cursor.execute(
                        """
                        UPDATE a6_trade.a6_merchants
                        SET roles = %s::text[],
                            role_revision = role_revision + 1,
                            updated_at = now()
                        WHERE id = %s
                        """,
                        (list(desired_roles), merchant_id),
                    )
                for decision in decisions:
                    if not decision.change_required:
                        continue
                    assignment = RoleAssignment(decision.merchant_id, decision.role)
                    desired_state = {"roles": list(desired_roles_by_merchant[decision.merchant_id])}
                    observed_before = {
                        "roles": list(decision.previous_roles),
                    }
                    cursor.execute(
                        """
                        INSERT INTO a6_trade.trade_control_actions (
                            idempotency_key, action_type, entity_type, entity_id,
                            desired_state, observed_before, observed_after, status,
                            config_version, requested_by, started_at, completed_at
                        ) VALUES (
                            %s, 'assign_merchant_role', 'merchant', %s,
                            %s::jsonb, %s::jsonb, %s::jsonb, 'completed',
                            %s, %s, now(), now()
                        )
                        ON CONFLICT (idempotency_key) DO NOTHING
                        RETURNING id
                        """,
                        (
                            _idempotency_key(
                                assignment,
                                role_revision_by_merchant[assignment.merchant_id],
                            ),
                            str(assignment.merchant_id),
                            json.dumps(desired_state, ensure_ascii=True, sort_keys=True),
                            json.dumps(observed_before, ensure_ascii=True, sort_keys=True),
                            json.dumps(desired_state, ensure_ascii=True, sort_keys=True),
                            config_version,
                            requested_by,
                        ),
                    )
                    if cursor.fetchone() is not None:
                        audit_action_count += 1
                connection.commit()
            else:
                connection.rollback()
    except Exception:
        connection.rollback()
        raise

    return RoleAssignmentResult(
        applied=apply,
        decisions=tuple(decisions),
        audit_action_count=audit_action_count,
    )
