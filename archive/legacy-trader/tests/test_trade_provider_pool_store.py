from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field, replace
from decimal import Decimal
from typing import Any

import pytest

from a6api_auto.trade_provider_cost import build_cost_plan
from a6api_auto.trade_provider_inventory import ProviderInventoryError
from a6api_auto.trade_provider_pool import (
    ProviderBundle,
    ProviderPoolError,
    ProviderSlot,
    SlotState,
)
from a6api_auto.trade_provider_pool_shadow import (
    ProviderPoolShadowInput,
    ProviderPoolShadowReconciler,
)
from a6api_auto.trade_provider_pool_store import PostgresProviderPoolStore


@dataclass
class FakeDatabase:
    cost: dict[str, Any] | None = None
    routing: dict[str, Any] | None = None
    slots: list[tuple[Any, ...]] = field(default_factory=list)
    connections: list[FakeConnection] = field(default_factory=list)
    fail_routing_apply: bool = False
    apply_stale: bool = False
    superseded_routing_generations: list[int] = field(default_factory=list)
    superseded_cost_generations: list[int] = field(default_factory=list)
    inventory_projected_count: int | None = None
    pin_bindings: dict[int, dict[str, Any]] = field(default_factory=dict)
    pin_decisions: dict[int, dict[str, Any]] = field(default_factory=dict)
    applying_plans: dict[str, dict[str, Any]] = field(default_factory=dict)

    def connection(self) -> FakeConnection:
        connection = FakeConnection(self)
        self.connections.append(connection)
        return connection

    def snapshot(
        self,
    ) -> tuple[
        dict[str, Any] | None,
        dict[str, Any] | None,
        list,
        list[int],
        list[int],
        dict[int, dict[str, Any]],
        dict[int, dict[str, Any]],
        dict[str, dict[str, Any]],
    ]:
        return copy.deepcopy(
            (
                self.cost,
                self.routing,
                self.slots,
                self.superseded_routing_generations,
                self.superseded_cost_generations,
                self.pin_bindings,
                self.pin_decisions,
                self.applying_plans,
            )
        )

    def restore(
        self,
        snapshot: tuple[
            dict[str, Any] | None,
            dict[str, Any] | None,
            list,
            list[int],
            list[int],
            dict[int, dict[str, Any]],
            dict[int, dict[str, Any]],
            dict[str, dict[str, Any]],
        ],
    ) -> None:
        (
            self.cost,
            self.routing,
            self.slots,
            self.superseded_routing_generations,
            self.superseded_cost_generations,
            self.pin_bindings,
            self.pin_decisions,
            self.applying_plans,
        ) = copy.deepcopy(snapshot)


class FakeConnection:
    def __init__(self, database: FakeDatabase) -> None:
        self.database = database
        self.calls: list[tuple[str, Any]] = []
        self.commits = 0
        self.rollbacks = 0
        self.closed = False
        self._snapshot = database.snapshot()

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1
        self.database.restore(self._snapshot)

    def close(self) -> None:
        self.closed = True


class FakeCursor:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection
        self.result: dict[str, Any] | None = None
        self.rowcount = -1
        self.description = None

    def __enter__(self) -> FakeCursor:
        return self

    def __exit__(self, *_args: object) -> bool:
        return False

    def execute(self, query: str, params: Any = None) -> FakeCursor:
        normalized = " ".join(query.split())
        self.connection.calls.append((normalized, params))
        self.result = None
        self.rowcount = -1
        database = self.connection.database

        if normalized.startswith("SELECT pg_advisory_xact_lock"):
            return self
        if normalized.startswith("SELECT generation.model, generation.generation"):
            self.result = _active_row(database, full=True)
            return self
        if normalized.startswith("SELECT generation.generation, generation.cost_generation"):
            self.result = _active_row(database, full=False)
            return self
        if normalized.startswith("SELECT generation.id AS routing_generation_id"):
            if database.routing is not None and database.routing["generation"] == params[1]:
                self.result = _existing_row(database)
            return self
        if normalized.startswith("WITH stale AS"):
            routing_count = 0
            cost_count = 0
            if (
                database.routing is not None
                and database.cost is not None
                and database.routing["model"] == params[0]
                and database.routing["generation_state"] == "planned"
                and database.cost["pricing_state"] == "planned"
            ):
                database.superseded_routing_generations.append(database.routing["generation"])
                database.superseded_cost_generations.append(database.cost["cost_generation"])
                database.routing["generation_state"] = "superseded"
                database.cost["pricing_state"] = "superseded"
                routing_count = 1
                cost_count = 1
            self.result = {
                "routing_count": routing_count,
                "cost_count": cost_count,
            }
            self.rowcount = 1
            return self
        if normalized.startswith("INSERT INTO a6_trade.trade_control_actions"):
            key = str(params[0])
            if key not in database.applying_plans:
                database.applying_plans[key] = {
                    "entity_id": str(params[1]),
                    "desired_state": json.loads(params[2]),
                    "config_version": str(params[4]),
                }
                self.result = {
                    "desired_state": database.applying_plans[key]["desired_state"],
                    "config_version": database.applying_plans[key]["config_version"],
                }
                self.rowcount = 1
            else:
                self.rowcount = 0
            return self
        if normalized.startswith("SELECT desired_state, config_version"):
            plan = database.applying_plans.get(str(params[0]))
            if plan is not None:
                self.result = {
                    "desired_state": plan["desired_state"],
                    "config_version": plan["config_version"],
                }
            return self
        if normalized.startswith("SELECT action.desired_state, action.config_version"):
            model, routing_generation, cost_generation, key = params
            routing = database.routing
            cost = database.cost
            plan = database.applying_plans.get(str(key))
            if (
                plan is not None
                and routing is not None
                and cost is not None
                and routing["model"] == model
                and routing["generation"] == routing_generation
                and routing["cost_generation_id"] == cost["id"]
                and cost["cost_generation"] == cost_generation
                and routing["generation_state"] == "applying"
                and cost["pricing_state"] in {"applying", "confirmed"}
            ):
                self.result = {
                    "desired_state": plan["desired_state"],
                    "config_version": plan["config_version"],
                    "generation_state": routing["generation_state"],
                    "pricing_state": cost["pricing_state"],
                }
            return self
        if normalized.startswith("INSERT INTO a6_trade.provider_cost_generations"):
            database.cost = {
                "id": 101,
                "model": params[0],
                "cost_generation": params[1],
                "parent_cost_generation": params[2],
                "cost_source": params[3],
                "cost_input_hash": params[4],
                "cost_output_hash": params[5],
                "token_mix_version": params[6],
                "exchange_rate_version": params[7],
                "active_slot_nos": params[8],
                "desired_slot_costs": json.loads(params[9]),
                "route_cost_multiplier": params[10],
                "retry_reserve_multiplier": params[11],
                "realized_cost_p95_multiplier": params[12],
                "pool_cost_multiplier": params[13],
                "margin_bps": params[14],
                "headroom_bps": params[15],
                "billing_floor_multiplier": params[16],
                "transition_floor_multiplier": params[17],
                "observed_billing_floor_multiplier": None,
                "pricing_state": "planned",
                "config_version": params[18],
            }
            self.result = {"id": 101}
            self.rowcount = 1
            return self
        if normalized.startswith("INSERT INTO a6_trade.provider_pool_generations"):
            database.routing = {
                "id": 201,
                "model": params[0],
                "generation": params[1],
                "parent_generation": params[2],
                "generation_source": params[3],
                "source_hash": params[4],
                "cost_generation_id": params[5],
                "cost_generation": params[6],
                "desired_size": params[7],
                "ordered_channel_ids": params[8],
                "standby_channel_ids": params[9],
                "generation_state": "planned",
                "config_version": params[10],
            }
            self.result = {"id": 201}
            self.rowcount = 1
            return self
        if normalized.startswith(
            "UPDATE a6_trade.provider_cost_generations SET pricing_state = CASE"
        ):
            cost = database.cost
            if (
                cost is not None
                and (cost["model"], cost["cost_generation"], cost["parent_cost_generation"])
                == params[:3]
                and cost["pricing_state"] in {"planned", "applying", "confirmed"}
                and (
                    cost["pricing_state"] in {"applying", "confirmed"}
                    or not database.apply_stale
                )
            ):
                if cost["pricing_state"] == "planned":
                    cost["pricing_state"] = "applying"
                self.rowcount = 1
            else:
                self.rowcount = 0
            return self
        if normalized.startswith(
            "UPDATE a6_trade.provider_pool_generations SET generation_state = 'applying'"
        ):
            routing = database.routing
            if (
                not database.fail_routing_apply
                and routing is not None
                and (routing["model"], routing["generation"], routing["parent_generation"])
                == params[:3]
                and routing["generation_state"] in {"planned", "applying"}
                and (
                    routing["generation_state"] == "applying"
                    or not database.apply_stale
                )
            ):
                routing["generation_state"] = "applying"
                self.rowcount = 1
            else:
                self.rowcount = 0
            return self
        if normalized.startswith(
            "UPDATE a6_trade.provider_cost_generations SET pricing_state = 'confirmed'"
        ):
            cost = database.cost
            if (
                cost is not None
                and (cost["model"], cost["cost_generation"], cost["parent_cost_generation"])
                == params[1:4]
                and cost["pricing_state"] in {"applying", "confirmed"}
                and cost["cost_output_hash"] == params[4]
                and cost["billing_floor_multiplier"] <= params[5]
            ):
                cost["pricing_state"] = "confirmed"
                cost["observed_billing_floor_multiplier"] = params[0]
                self.rowcount = 1
            else:
                self.rowcount = 0
            return self
        if (
            normalized.startswith("SELECT generation.id AS generation_id")
            and "generation.generation_state, generation.config_version" in normalized
        ):
            routing = database.routing
            cost = database.cost
            if (
                routing is not None
                and cost is not None
                and (routing["model"], routing["generation"]) == params
                and routing["cost_generation_id"] == cost["id"]
                and cost["model"] == routing["model"]
            ):
                self.result = {
                    "generation_id": routing["id"],
                    "cost_generation_id": cost["id"],
                    "parent_generation": routing["parent_generation"],
                    "generation_source": routing["generation_source"],
                    "source_hash": routing["source_hash"],
                    "cost_generation": routing["cost_generation"],
                    "desired_size": routing["desired_size"],
                    "ordered_channel_ids": routing["ordered_channel_ids"],
                    "standby_channel_ids": routing["standby_channel_ids"],
                    "generation_state": routing["generation_state"],
                    "routing_config_version": routing["config_version"],
                    "parent_cost_generation": cost["parent_cost_generation"],
                    "cost_source": cost["cost_source"],
                    "cost_input_hash": cost["cost_input_hash"],
                    "cost_output_hash": cost["cost_output_hash"],
                    "pricing_state": cost["pricing_state"],
                    "cost_config_version": cost["config_version"],
                }
            return self
        if normalized.startswith("SELECT count(*) AS slot_count"):
            model, routing_generation = params
            self.result = {
                "slot_count": sum(
                    item.get("model") == model
                    and item.get("routing_generation") == routing_generation
                    and item.get("valid_to") is None
                    for item in database.slots
                )
            }
            return self
        if normalized.startswith(
            "UPDATE a6_trade.provider_cost_generations SET pricing_state = 'failed'"
        ):
            cost = database.cost
            if (
                cost is not None
                and cost["id"] == params[0]
                and cost["pricing_state"] == params[1]
            ):
                cost["pricing_state"] = "failed"
                self.rowcount = 1
            return self
        if normalized.startswith(
            "UPDATE a6_trade.provider_pool_generations SET generation_state = 'failed'"
        ):
            routing = database.routing
            if (
                routing is not None
                and routing["id"] == params[0]
                and routing["generation_state"] == "applying"
            ):
                routing["generation_state"] = "failed"
                self.rowcount = 1
            return self
        if normalized.startswith("SELECT generation.id AS generation_id"):
            routing = database.routing
            cost = database.cost
            if (
                routing is not None
                and cost is not None
                and (routing["model"], routing["generation"]) == params
                and routing["cost_generation_id"] == cost["id"]
                and cost["model"] == routing["model"]
                and routing["generation_state"] == "applying"
                and cost["pricing_state"] == "confirmed"
            ):
                self.result = {
                    "generation_id": routing["id"],
                    "cost_generation_id": cost["id"],
                    "routing_model": routing["model"],
                    "routing_generation": routing["generation"],
                    "parent_generation": routing["parent_generation"],
                    "generation_source": routing["generation_source"],
                    "source_hash": routing["source_hash"],
                    "routing_cost_generation": routing["cost_generation"],
                    "desired_size": routing["desired_size"],
                    "ordered_channel_ids": routing["ordered_channel_ids"],
                    "standby_channel_ids": routing["standby_channel_ids"],
                    "routing_config_version": routing["config_version"],
                    "cost_model": cost["model"],
                    "cost_generation": cost["cost_generation"],
                    "parent_cost_generation": cost["parent_cost_generation"],
                    "cost_source": cost["cost_source"],
                    "cost_input_hash": cost["cost_input_hash"],
                    "cost_output_hash": cost["cost_output_hash"],
                    "token_mix_version": cost["token_mix_version"],
                    "exchange_rate_version": cost["exchange_rate_version"],
                    "active_slot_nos": cost["active_slot_nos"],
                    "desired_slot_costs": cost["desired_slot_costs"],
                    "route_cost_multiplier": cost["route_cost_multiplier"],
                    "retry_reserve_multiplier": cost["retry_reserve_multiplier"],
                    "realized_cost_p95_multiplier": cost["realized_cost_p95_multiplier"],
                    "pool_cost_multiplier": cost["pool_cost_multiplier"],
                    "margin_bps": cost["margin_bps"],
                    "headroom_bps": cost["headroom_bps"],
                    "billing_floor_multiplier": cost["billing_floor_multiplier"],
                    "transition_floor_multiplier": cost["transition_floor_multiplier"],
                    "observed_billing_floor_multiplier": cost["observed_billing_floor_multiplier"],
                    "cost_config_version": cost["config_version"],
                }
            return self
        if normalized.startswith("SELECT slot.pin_assignment_epoch_id"):
            return self
        if normalized.startswith("INSERT INTO a6_trade.provider_pin_assignment_epochs"):
            self.result = {"id": 301}
            self.rowcount = 1
            return self
        if normalized.startswith("UPDATE a6_trade.provider_pool_slots SET valid_to"):
            self.rowcount = len(database.slots)
            return self
        if normalized.startswith("INSERT INTO a6_trade.provider_pool_slots"):
            database.slots.append(tuple(params))
            self.rowcount = 1
            return self
        if normalized.startswith(
            "SELECT slot_no, a6_token_id, desired_channel_id, observed_rate_multiplier"
        ):
            model, routing_generation = params
            self.result = None
            self._results = [
                {
                    "slot_no": slot[3],
                    "a6_token_id": slot[9],
                    "desired_channel_id": slot[11],
                    "observed_rate_multiplier": slot[15],
                    "confirmed_pin_binding_id": slot[21],
                }
                for slot in database.slots
                if slot[1] == model
                and slot[2] == routing_generation
                and slot[18] == "READY"
                and slot[22] is True
            ]
            return self
        if normalized.startswith(
            "SELECT binding.id, binding.channel_id, binding.fallback_to_smart_routing"
        ):
            token_id, model = params
            binding = database.pin_bindings.get(token_id)
            if binding is not None and binding["model"] == model:
                decision = database.pin_decisions.get(binding.get("decision_id"))
                self.result = {
                    **binding,
                    "decision_token_id": None if decision is None else decision["token_id"],
                    "decision_model": None if decision is None else decision["model"],
                    "selected_channel_id": (
                        None if decision is None else decision["selected_channel_id"]
                    ),
                    "decision_status": None if decision is None else decision["status"],
                }
            return self
        if normalized.startswith("INSERT INTO a6_trade.a6_pin_decisions"):
            decision_id = len(database.pin_decisions) + 1
            database.pin_decisions[decision_id] = {
                "token_id": params[0],
                "model": params[1],
                "selected_channel_id": params[3],
                "status": "completed",
            }
            self.result = {"id": decision_id}
            self.rowcount = 1
            return self
        if normalized.startswith("UPDATE a6_trade.a6_pin_bindings SET valid_to"):
            binding_id = params[0]
            matched = next(
                (
                    token_id
                    for token_id, binding in database.pin_bindings.items()
                    if binding["id"] == binding_id
                ),
                None,
            )
            if matched is not None:
                del database.pin_bindings[matched]
                self.rowcount = 1
            else:
                self.rowcount = 0
            return self
        if normalized.startswith("INSERT INTO a6_trade.a6_pin_bindings"):
            database.pin_bindings[params[0]] = {
                "id": len(database.pin_bindings) + 1,
                "channel_id": params[2],
                "fallback_to_smart_routing": False,
                "confirmed_at": "now",
                "decision_id": params[4],
                "model": params[1],
            }
            self.rowcount = 1
            return self
        if normalized.startswith(
            "UPDATE a6_trade.a6_pin_bindings SET fallback_to_smart_routing"
        ):
            binding_id = params[2]
            binding = next(
                (
                    item
                    for item in database.pin_bindings.values()
                    if item["id"] == binding_id and item["channel_id"] == params[3]
                ),
                None,
            )
            if binding is not None:
                binding["fallback_to_smart_routing"] = False
                binding["confirmed_at"] = "now"
                binding["decision_id"] = params[1]
                self.rowcount = 1
            else:
                self.rowcount = 0
            return self
        if normalized.startswith("UPDATE a6_trade.a6_pin_bindings binding SET valid_to"):
            active_token_ids = set(params[1])
            database.pin_bindings = {
                token_id: binding
                for token_id, binding in database.pin_bindings.items()
                if token_id in active_token_ids
            }
            self.rowcount = 0
            return self
        if normalized.startswith("WITH active_slots AS MATERIALIZED"):
            model, routing_generation = params[:2]
            active_count = sum(
                slot[1] == model
                and slot[2] == routing_generation
                and slot[18] == "READY"
                for slot in database.slots
            )
            self.result = {
                "active_slot_count": active_count,
                "projected_count": (
                    active_count
                    if database.inventory_projected_count is None
                    else database.inventory_projected_count
                ),
                "demoted_count": 0,
            }
            return self
        if normalized.startswith(
            "UPDATE a6_trade.provider_cost_generations SET pricing_state = 'active'"
        ):
            cost = database.cost
            if (
                cost is not None
                and cost["id"] == params[0]
                and cost["pricing_state"] == "confirmed"
            ):
                cost["pricing_state"] = "active"
                self.rowcount = 1
            else:
                self.rowcount = 0
            return self
        if normalized.startswith(
            "UPDATE a6_trade.provider_pool_generations SET generation_state = 'active'"
        ):
            routing = database.routing
            if (
                routing is not None
                and routing["id"] == params[0]
                and routing["generation_state"] == "applying"
            ):
                routing["generation_state"] = "active"
                self.rowcount = 1
            else:
                self.rowcount = 0
            return self
        raise AssertionError(f"unexpected SQL: {normalized}")

    def fetchone(self) -> dict[str, Any] | None:
        results = getattr(self, "_results", None)
        if results is not None:
            if results:
                return results.pop(0)
            del self._results
            return None
        return self.result

    def fetchall(self) -> list[dict[str, Any]]:
        results = getattr(self, "_results", None)
        if results is None:
            return []
        del self._results
        return list(results)


@dataclass
class RollbackFakeDatabase:
    routing: dict[int, dict[str, Any]]
    costs: dict[int, dict[str, Any]]
    slots: list[dict[str, Any]]
    fail_parent_slot_restore: bool = False
    inventory_projected_count: int | None = None
    connections: list[RollbackFakeConnection] = field(default_factory=list)

    def connection(self) -> RollbackFakeConnection:
        connection = RollbackFakeConnection(self)
        self.connections.append(connection)
        return connection

    def snapshot(self) -> tuple[dict, dict, list]:
        return copy.deepcopy((self.routing, self.costs, self.slots))

    def restore(self, snapshot: tuple[dict, dict, list]) -> None:
        self.routing, self.costs, self.slots = copy.deepcopy(snapshot)


class RollbackFakeConnection:
    def __init__(self, database: RollbackFakeDatabase) -> None:
        self.database = database
        self.calls: list[tuple[str, Any]] = []
        self.commits = 0
        self.rollbacks = 0
        self.closed = False
        self._snapshot = database.snapshot()

    def cursor(self) -> RollbackFakeCursor:
        return RollbackFakeCursor(self)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1
        self.database.restore(self._snapshot)

    def close(self) -> None:
        self.closed = True


class RollbackFakeCursor:
    def __init__(self, connection: RollbackFakeConnection) -> None:
        self.connection = connection
        self.result: dict[str, Any] | None = None
        self.rowcount = -1
        self.description = None

    def __enter__(self) -> RollbackFakeCursor:
        return self

    def __exit__(self, *_args: object) -> bool:
        return False

    def execute(self, query: str, params: Any = None) -> RollbackFakeCursor:
        normalized = " ".join(query.split())
        self.connection.calls.append((normalized, params))
        self.result = None
        self.rowcount = -1
        database = self.connection.database

        if normalized.startswith("SELECT pg_advisory_xact_lock"):
            return self
        if normalized.startswith("SELECT generation.generation, generation.cost_generation"):
            for routing in database.routing.values():
                cost = database.costs[routing["cost_generation"]]
                if (
                    routing["model"] == params[0]
                    and routing["generation_state"] == "active"
                    and cost["pricing_state"] == "active"
                ):
                    self.result = {
                        "generation": routing["generation"],
                        "cost_generation": routing["cost_generation"],
                    }
                    break
            return self
        if normalized.startswith("SELECT generation.id AS generation_id"):
            model, routing_generation, cost_generation = params
            routing = database.routing.get(routing_generation)
            cost = database.costs.get(cost_generation)
            if (
                routing is not None
                and cost is not None
                and routing["model"] == model
                and routing["cost_generation"] == cost_generation
            ):
                self.result = {
                    "generation_id": routing["id"],
                    "cost_generation_id": cost["id"],
                    "parent_generation": routing["parent_generation"],
                    "parent_cost_generation": cost["parent_cost_generation"],
                    "generation_state": routing["generation_state"],
                    "pricing_state": cost["pricing_state"],
                    "desired_size": routing["desired_size"],
                    "config_version": routing.get("config_version", "test-config"),
                }
            return self
        if normalized.startswith(
            "UPDATE a6_trade.provider_pool_generations SET generation_state = 'applying'"
        ):
            routing = _rollback_row_by_id(database.routing, params[0])
            if routing is not None and routing["generation_state"] == "active":
                routing["generation_state"] = "applying"
                self.rowcount = 1
            else:
                self.rowcount = 0
            return self
        if normalized.startswith(
            "UPDATE a6_trade.provider_cost_generations SET pricing_state = 'confirmed'"
        ):
            cost = _rollback_row_by_id(database.costs, params[0])
            if cost is not None and cost["pricing_state"] == "active":
                cost["pricing_state"] = "confirmed"
                self.rowcount = 1
            else:
                self.rowcount = 0
            return self
        if normalized.startswith("UPDATE a6_trade.provider_pool_slots SET valid_to = now()"):
            model, generation = params
            changed = 0
            for slot in database.slots:
                if (
                    slot["model"] == model
                    and slot["routing_generation"] == generation
                    and slot["valid_to"] is None
                ):
                    slot["valid_to"] = "closed"
                    changed += 1
            self.rowcount = changed
            return self
        if normalized.startswith(
            "UPDATE a6_trade.provider_pin_assignment_epochs epoch"
        ):
            self.rowcount = 1
            return self
        if normalized.startswith("UPDATE a6_trade.provider_pool_slots SET valid_to = NULL"):
            if database.fail_parent_slot_restore:
                self.rowcount = 0
                return self
            model, generation = params
            changed = 0
            for slot in database.slots:
                if (
                    slot["model"] == model
                    and slot["routing_generation"] == generation
                    and slot["valid_to"] is not None
                ):
                    slot["valid_to"] = None
                    changed += 1
            self.rowcount = changed
            return self
        if normalized.startswith(
            "UPDATE a6_trade.provider_cost_generations SET pricing_state = 'active'"
        ):
            cost = _rollback_row_by_id(database.costs, params[0])
            if cost is not None and cost["pricing_state"] == "superseded":
                cost["pricing_state"] = "active"
                self.rowcount = 1
            else:
                self.rowcount = 0
            return self
        if normalized.startswith(
            "UPDATE a6_trade.provider_pool_generations SET generation_state = 'active'"
        ):
            routing = _rollback_row_by_id(database.routing, params[0])
            if routing is not None and routing["generation_state"] == "superseded":
                routing["generation_state"] = "active"
                self.rowcount = 1
            else:
                self.rowcount = 0
            return self
        if normalized.startswith("SELECT count(*) AS current_slot_count"):
            rolled_back_generation, restored_generation, model = params
            current = [
                slot
                for slot in database.slots
                if slot["model"] == model and slot["valid_to"] is None
            ]
            self.result = {
                "current_slot_count": len(current),
                "rolled_back_slot_count": sum(
                    slot["routing_generation"] == rolled_back_generation for slot in current
                ),
                "restored_slot_count": sum(
                    slot["routing_generation"] == restored_generation for slot in current
                ),
            }
            return self
        if normalized.startswith("WITH active_slots AS MATERIALIZED"):
            model, routing_generation = params[:2]
            active_count = sum(
                slot["model"] == model
                and slot["routing_generation"] == routing_generation
                and slot["valid_to"] is None
                for slot in database.slots
            )
            self.result = {
                "active_slot_count": active_count,
                "projected_count": (
                    active_count
                    if database.inventory_projected_count is None
                    else database.inventory_projected_count
                ),
                "demoted_count": 0,
            }
            return self
        if normalized.startswith(
            "SELECT slot_no, a6_token_id, desired_channel_id, observed_rate_multiplier"
        ):
            self._results = []
            return self
        if normalized.startswith("UPDATE a6_trade.a6_pin_bindings binding SET valid_to"):
            self.rowcount = 0
            return self
        raise AssertionError(f"unexpected rollback SQL: {normalized}")

    def fetchone(self) -> dict[str, Any] | None:
        return self.result

    def fetchall(self) -> list[dict[str, Any]]:
        results = getattr(self, "_results", None)
        if results is None:
            return []
        del self._results
        return list(results)


def _rollback_row_by_id(rows: dict[int, dict[str, Any]], row_id: int) -> dict[str, Any] | None:
    return next((row for row in rows.values() if row["id"] == row_id), None)


def _active_row(database: FakeDatabase, *, full: bool) -> dict[str, Any] | None:
    routing = database.routing
    cost = database.cost
    if (
        routing is None
        or cost is None
        or routing["generation_state"] != "active"
        or cost["pricing_state"] != "active"
    ):
        return None
    row = {
        "generation": routing["generation"],
        "cost_generation": routing["cost_generation"],
    }
    if full:
        row.update(
            model=routing["model"],
            source_hash=routing["source_hash"],
            cost_input_hash=cost["cost_input_hash"],
            cost_output_hash=cost["cost_output_hash"],
            desired_size=routing["desired_size"],
        )
    return row


def _existing_row(database: FakeDatabase) -> dict[str, Any]:
    routing = database.routing
    cost = database.cost
    assert routing is not None and cost is not None
    return {
        "routing_generation_id": routing["id"],
        "cost_generation_id": cost["id"],
        "parent_generation": routing["parent_generation"],
        "generation_source": routing["generation_source"],
        "source_hash": routing["source_hash"],
        "cost_generation": routing["cost_generation"],
        "desired_size": routing["desired_size"],
        "ordered_channel_ids": routing["ordered_channel_ids"],
        "standby_channel_ids": routing["standby_channel_ids"],
        "config_version": routing["config_version"],
        "parent_cost_generation": cost["parent_cost_generation"],
        "cost_input_hash": cost["cost_input_hash"],
        "cost_output_hash": cost["cost_output_hash"],
        "cost_config_version": cost["config_version"],
    }


def bundle() -> ProviderBundle:
    plan = build_cost_plan(
        {1: "1"},
        retry_reserve_multiplier="0",
        bootstrap_retry_reserve="0",
        realized_cost_p95_multiplier=None,
    )
    return ProviderBundle(
        model="gpt-5.6-sol",
        routing_generation=1,
        parent_routing_generation=None,
        cost_generation=1,
        parent_cost_generation=None,
        generation_source="bootstrap",
        source_hash="a" * 64,
        ordered_channel_ids=(7,),
        standby_channel_ids=(8,),
        desired_size=1,
        slots=(
            ProviderSlot(
                slot_no=1,
                channel_id=7,
                rank=1,
                priority=2,
                assignment_id=31,
                enodot_binding_id=31,
                token_id=41,
                account_id=51,
                slot_state=SlotState.READY,
                funding_eligible=True,
                desired_schedulable=True,
                observed_schedulable=True,
                desired_cost_multiplier=Decimal("1"),
                observed_cost_multiplier=Decimal("1"),
                confirmed_pin_binding_id=61,
                pin_confirmed=True,
                probe_confirmed=True,
            ),
        ),
        cost_plan=plan,
    )


def rollback_database(
    *,
    with_parent: bool,
    fail_parent_slot_restore: bool = False,
) -> tuple[RollbackFakeDatabase, ProviderBundle]:
    desired = bundle()
    if with_parent:
        desired = replace(
            desired,
            routing_generation=2,
            parent_routing_generation=1,
            cost_generation=2,
            parent_cost_generation=1,
            source_hash="b" * 64,
        )
        routing = {
            1: {
                "id": 201,
                "model": desired.model,
                "generation": 1,
                "parent_generation": None,
                "cost_generation": 1,
                "generation_state": "superseded",
                "desired_size": 1,
            },
            2: {
                "id": 202,
                "model": desired.model,
                "generation": 2,
                "parent_generation": 1,
                "cost_generation": 2,
                "generation_state": "active",
                "desired_size": 1,
            },
        }
        costs = {
            1: {
                "id": 101,
                "parent_cost_generation": None,
                "pricing_state": "superseded",
            },
            2: {
                "id": 102,
                "parent_cost_generation": 1,
                "pricing_state": "active",
            },
        }
        slots = [
            {"model": desired.model, "routing_generation": 1, "valid_to": "closed"},
            {"model": desired.model, "routing_generation": 2, "valid_to": None},
        ]
    else:
        routing = {
            1: {
                "id": 201,
                "model": desired.model,
                "generation": 1,
                "parent_generation": None,
                "cost_generation": 1,
                "generation_state": "active",
                "desired_size": 1,
            }
        }
        costs = {
            1: {
                "id": 101,
                "parent_cost_generation": None,
                "pricing_state": "active",
            }
        }
        slots = [{"model": desired.model, "routing_generation": 1, "valid_to": None}]
    return (
        RollbackFakeDatabase(
            routing,
            costs,
            slots,
            fail_parent_slot_restore=fail_parent_slot_restore,
        ),
        desired,
    )


def test_postgres_store_runs_idempotent_generation_lifecycle() -> None:
    database = FakeDatabase()
    store = PostgresProviderPoolStore(database.connection)
    desired = bundle()

    planned = store.plan(
        desired,
        expected_routing_generation=None,
        expected_cost_generation=None,
    )
    repeated = store.plan(
        desired,
        expected_routing_generation=None,
        expected_cost_generation=None,
    )
    store.mark_applying(desired)
    store.confirm_cost(
        desired,
        observed_output_hash=desired.cost_plan.cost_output_hash,
        observed_billing_floor=desired.cost_plan.transition_floor_multiplier,
    )
    store.mark_applying(desired)
    store.confirm_cost(
        desired,
        observed_output_hash=desired.cost_plan.cost_output_hash,
        observed_billing_floor=desired.cost_plan.transition_floor_multiplier,
    )
    active = store.activate(desired)

    assert planned == repeated
    assert active.routing_generation == 1
    assert store.active(desired.model) == active
    assert database.slots[0][16] == "FUNDED"
    assert database.slots[0][21] == 61
    assert database.pin_bindings[41]["channel_id"] == 7
    assert database.pin_bindings[41]["confirmed_at"] == "now"
    decision_id = database.pin_bindings[41]["decision_id"]
    assert database.pin_decisions[decision_id] == {
        "token_id": 41,
        "model": "gpt-5.6-sol",
        "selected_channel_id": 7,
        "status": "completed",
    }
    slot_write = next(
        query
        for connection in database.connections
        for query, _params in connection.calls
        if query.startswith("INSERT INTO a6_trade.provider_pool_slots")
    )
    assert "ON CONFLICT (model, routing_generation, slot_no) DO UPDATE" in slot_write
    assert "existing.valid_to IS NOT NULL" in slot_write
    epoch_write = next(
        query
        for connection in database.connections
        for query, _params in connection.calls
        if query.startswith("INSERT INTO a6_trade.provider_pin_assignment_epochs")
    )
    assert "ON CONFLICT (model, slot_no, started_routing_generation) DO UPDATE" in epoch_write
    assert "provider_pin_assignment_epochs.ended_at IS NOT NULL" in epoch_write
    assert "provider_pin_assignment_epochs.channel_id = EXCLUDED.channel_id" in epoch_write
    activation_statements = next(
        [query for query, _params in connection.calls]
        for connection in database.connections
        if any(
            query.startswith("WITH active_slots AS MATERIALIZED")
            for query, _params in connection.calls
        )
    )
    projection_index = next(
        index
        for index, query in enumerate(activation_statements)
        if query.startswith("WITH active_slots AS MATERIALIZED")
    )
    routing_activation_index = next(
        index
        for index, query in enumerate(activation_statements)
        if query.startswith(
            "UPDATE a6_trade.provider_pool_generations SET generation_state = 'active'"
        )
    )
    assert projection_index < routing_activation_index
    assert all(connection.commits == 1 for connection in database.connections[:-1])
    assert all(connection.closed for connection in database.connections)


def test_postgres_store_round_trips_immutable_applying_bundle() -> None:
    database = FakeDatabase()
    store = PostgresProviderPoolStore(database.connection)
    desired = bundle()
    store.plan(
        desired,
        expected_routing_generation=None,
        expected_cost_generation=None,
    )

    store.mark_applying(desired)
    loaded = store.load_applying_bundle(
        desired.model,
        routing_generation=desired.routing_generation,
        cost_generation=desired.cost_generation,
    )
    store.mark_applying(desired)

    assert loaded == desired
    assert len(database.applying_plans) == 1
    document = next(iter(database.applying_plans.values()))["desired_state"]
    assert document["schema"] == "a6-provider-applying-plan-v1"
    assert document["slots"][0]["channel_id"] == 7


def test_postgres_activation_rolls_back_when_inventory_identity_is_missing() -> None:
    database = FakeDatabase(inventory_projected_count=0)
    store = PostgresProviderPoolStore(database.connection)
    desired = bundle()
    store.plan(
        desired,
        expected_routing_generation=None,
        expected_cost_generation=None,
    )
    store.mark_applying(desired)
    store.confirm_cost(
        desired,
        observed_output_hash=desired.cost_plan.cost_output_hash,
        observed_billing_floor=desired.cost_plan.transition_floor_multiplier,
    )
    before = database.snapshot()

    with pytest.raises(
        ProviderInventoryError,
        match="provider_inventory_active_projection_mismatch",
    ):
        store.activate(desired)

    assert database.snapshot() == before
    failed = database.connections[-1]
    assert failed.commits == 0
    assert failed.rollbacks == 1
    assert failed.closed is True


def test_postgres_store_rejects_semantic_generation_reuse() -> None:
    database = FakeDatabase()
    store = PostgresProviderPoolStore(database.connection)
    desired = bundle()
    store.plan(
        desired,
        expected_routing_generation=None,
        expected_cost_generation=None,
    )
    assert database.routing is not None
    database.routing["ordered_channel_ids"] = [999]

    with pytest.raises(ProviderPoolError, match="generation_conflict"):
        store.plan(
            desired,
            expected_routing_generation=None,
            expected_cost_generation=None,
        )

    failed = database.connections[-1]
    assert failed.rollbacks == 1
    assert failed.closed is True


def test_new_plan_supersedes_prior_planned_routing_and_cost_together() -> None:
    database = FakeDatabase()
    store = PostgresProviderPoolStore(database.connection)
    initial = bundle()
    replacement = replace(
        initial,
        routing_generation=2,
        cost_generation=2,
        source_hash="b" * 64,
    )

    store.plan(
        initial,
        expected_routing_generation=None,
        expected_cost_generation=None,
    )
    store.plan(
        replacement,
        expected_routing_generation=None,
        expected_cost_generation=None,
    )

    assert database.superseded_routing_generations == [1]
    assert database.superseded_cost_generations == [1]
    assert database.routing is not None
    assert database.routing["generation"] == 2
    assert database.routing["generation_state"] == "planned"
    assert database.cost is not None
    assert database.cost["cost_generation"] == 2
    assert database.cost["pricing_state"] == "planned"


def test_postgres_store_rolls_back_both_apply_transitions_on_routing_cas_miss() -> None:
    database = FakeDatabase()
    store = PostgresProviderPoolStore(database.connection)
    desired = bundle()
    store.plan(
        desired,
        expected_routing_generation=None,
        expected_cost_generation=None,
    )
    database.fail_routing_apply = True

    with pytest.raises(ProviderPoolError, match="routing_generation_apply_cas_mismatch"):
        store.mark_applying(desired)

    assert database.cost is not None
    assert database.cost["pricing_state"] == "planned"
    assert database.connections[-1].rollbacks == 1


def test_postgres_store_rejects_stale_generation_before_remote_apply() -> None:
    database = FakeDatabase()
    store = PostgresProviderPoolStore(database.connection, max_apply_age_seconds=900)
    desired = bundle()
    store.plan(
        desired,
        expected_routing_generation=None,
        expected_cost_generation=None,
    )
    database.apply_stale = True

    with pytest.raises(ProviderPoolError, match="cost_generation_apply_cas_mismatch"):
        store.mark_applying(desired)

    assert database.cost is not None
    assert database.cost["pricing_state"] == "planned"
    assert database.routing is not None
    assert database.routing["generation_state"] == "planned"
    assert database.connections[-1].rollbacks == 1
    cost_update = next(
        call
        for call in database.connections[-1].calls
        if call[0].startswith(
            "UPDATE a6_trade.provider_cost_generations SET pricing_state = CASE"
        )
    )
    assert "planned_at >= now() - (%s * interval '1 second')" in cost_update[0]
    assert cost_update[1][-1] == 900
    assert database.applying_plans == {}


def test_postgres_store_resumes_stale_applying_generation() -> None:
    database = FakeDatabase()
    store = PostgresProviderPoolStore(database.connection, max_apply_age_seconds=900)
    desired = bundle()
    store.plan(
        desired,
        expected_routing_generation=None,
        expected_cost_generation=None,
    )
    store.mark_applying(desired)
    database.apply_stale = True

    store.mark_applying(desired)

    assert database.cost is not None
    assert database.cost["pricing_state"] == "applying"
    assert database.routing is not None
    assert database.routing["generation_state"] == "applying"
    assert database.connections[-1].commits == 1


def test_fail_applying_accepts_confirmed_cost_and_closed_historical_slots() -> None:
    database = FakeDatabase()
    store = PostgresProviderPoolStore(database.connection)
    desired = bundle()
    store.plan(
        desired,
        expected_routing_generation=None,
        expected_cost_generation=None,
    )
    store.mark_applying(desired)
    store.confirm_cost(
        desired,
        observed_output_hash=desired.cost_plan.cost_output_hash,
        observed_billing_floor=desired.cost_plan.transition_floor_multiplier,
    )
    database.slots.append(
        {
            "model": desired.model,
            "routing_generation": desired.routing_generation,
            "valid_to": "closed",
        }
    )

    store.fail_applying(desired, reason="provider_reconcile_pin_failed")

    assert database.routing is not None
    assert database.routing["generation_state"] == "failed"
    assert database.cost is not None
    assert database.cost["pricing_state"] == "failed"


def test_fail_applying_rejects_open_candidate_slots() -> None:
    database = FakeDatabase()
    store = PostgresProviderPoolStore(database.connection)
    desired = bundle()
    store.plan(
        desired,
        expected_routing_generation=None,
        expected_cost_generation=None,
    )
    store.mark_applying(desired)
    database.slots.append(
        {
            "model": desired.model,
            "routing_generation": desired.routing_generation,
            "valid_to": None,
        }
    )

    with pytest.raises(
        ProviderPoolError,
        match="provider_generation_failure_slots_present",
    ):
        store.fail_applying(desired, reason="provider_reconcile_pin_failed")

    assert database.routing is not None
    assert database.routing["generation_state"] == "applying"
    assert database.cost is not None
    assert database.cost["pricing_state"] == "applying"


@pytest.mark.parametrize("max_age", [0, 86_401, True, 1.5])
def test_postgres_store_rejects_invalid_apply_max_age(max_age) -> None:
    with pytest.raises(ValueError, match="provider_apply_max_age_invalid"):
        PostgresProviderPoolStore(lambda: None, max_apply_age_seconds=max_age)


@pytest.mark.parametrize(
    ("row_name", "field", "mutated_value"),
    [
        ("routing", "parent_generation", 99),
        ("routing", "generation_source", "tampered"),
        ("routing", "source_hash", "b" * 64),
        ("routing", "cost_generation", 99),
        ("routing", "desired_size", 2),
        ("routing", "ordered_channel_ids", [999]),
        ("routing", "standby_channel_ids", [999]),
        ("routing", "config_version", "tampered"),
        ("cost", "parent_cost_generation", 99),
        ("cost", "cost_source", "tampered"),
        ("cost", "cost_input_hash", "b" * 64),
        ("cost", "cost_output_hash", "b" * 64),
        ("cost", "token_mix_version", "tampered"),
        ("cost", "exchange_rate_version", "tampered"),
        ("cost", "active_slot_nos", [2]),
        ("cost", "desired_slot_costs", {"1": "2"}),
        ("cost", "route_cost_multiplier", Decimal("2")),
        ("cost", "retry_reserve_multiplier", Decimal("2")),
        ("cost", "realized_cost_p95_multiplier", Decimal("2")),
        ("cost", "pool_cost_multiplier", Decimal("2")),
        ("cost", "margin_bps", 999),
        ("cost", "headroom_bps", 999),
        ("cost", "billing_floor_multiplier", Decimal("2")),
        ("cost", "transition_floor_multiplier", Decimal("2")),
        ("cost", "observed_billing_floor_multiplier", Decimal("0")),
        ("cost", "config_version", "tampered"),
    ],
)
def test_activate_rejects_confirmed_generation_semantic_drift_before_writes(
    row_name: str,
    field: str,
    mutated_value: object,
) -> None:
    database = FakeDatabase()
    store = PostgresProviderPoolStore(database.connection)
    desired = bundle()
    store.plan(
        desired,
        expected_routing_generation=None,
        expected_cost_generation=None,
    )
    store.mark_applying(desired)
    store.confirm_cost(
        desired,
        observed_output_hash=desired.cost_plan.cost_output_hash,
        observed_billing_floor=desired.cost_plan.transition_floor_multiplier,
    )
    row = getattr(database, row_name)
    assert row is not None
    row[field] = mutated_value

    with pytest.raises(
        ProviderPoolError,
        match="provider_bundle_activation_generation_conflict",
    ):
        store.activate(desired)

    assert database.routing is not None
    assert database.routing["generation_state"] == "applying"
    assert database.cost is not None
    assert database.cost["pricing_state"] == "confirmed"
    assert database.slots == []
    failed = database.connections[-1]
    assert failed.commits == 0
    assert failed.rollbacks == 1
    assert failed.closed is True


def test_postgres_store_checks_bundle_parent_before_opening_transaction() -> None:
    database = FakeDatabase()
    store = PostgresProviderPoolStore(database.connection)

    with pytest.raises(ProviderPoolError, match="parent_routing_mismatch"):
        store.plan(
            bundle(),
            expected_routing_generation=9,
            expected_cost_generation=None,
        )

    assert database.connections == []


def test_postgres_shadow_persists_plan_without_apply_activate_or_slot_writes() -> None:
    database = FakeDatabase()
    store = PostgresProviderPoolStore(database.connection)
    desired = replace(bundle(), cost_input_hash="b" * 64)

    report = ProviderPoolShadowReconciler(store).reconcile(
        ProviderPoolShadowInput(desired),
        persist=True,
    )

    assert report.persisted is True
    assert report.activated is False
    assert report.remote_write_count == 0
    assert database.routing is not None
    assert database.routing["generation_state"] == "planned"
    assert database.cost is not None
    assert database.cost["pricing_state"] == "planned"
    assert database.cost["cost_input_hash"] == "b" * 64
    assert database.slots == []
    statements = [query for connection in database.connections for query, _ in connection.calls]
    assert not any("SET generation_state = 'applying'" in query for query in statements)
    assert not any("SET generation_state = 'active'" in query for query in statements)


def test_postgres_activation_rollback_restores_parent_slots_and_is_idempotent() -> None:
    database, desired = rollback_database(with_parent=True)
    store = PostgresProviderPoolStore(database.connection)

    rollback = store.rollback_activation(desired)
    repeated = store.rollback_activation(desired)

    assert rollback == repeated
    assert rollback.restored_routing_generation == 1
    assert rollback.restored_cost_generation == 1
    assert database.routing[1]["generation_state"] == "active"
    assert database.costs[1]["pricing_state"] == "active"
    assert database.routing[2]["generation_state"] == "applying"
    assert database.costs[2]["pricing_state"] == "confirmed"
    assert database.slots == [
        {"model": desired.model, "routing_generation": 1, "valid_to": None},
        {"model": desired.model, "routing_generation": 2, "valid_to": "closed"},
    ]
    assert not any(
        query.startswith("UPDATE")
        and not query.startswith("UPDATE a6_trade.a6_pin_bindings binding SET valid_to")
        for query, _params in database.connections[-1].calls
    )
    assert any(
        query.startswith("WITH active_slots AS MATERIALIZED")
        for query, _params in database.connections[-1].calls
    )
    assert all(connection.commits == 1 for connection in database.connections)
    assert all(connection.closed for connection in database.connections)


def test_postgres_activation_rollback_without_parent_removes_current_slots() -> None:
    database, desired = rollback_database(with_parent=False)
    store = PostgresProviderPoolStore(database.connection)

    rollback = store.rollback_activation(desired)

    assert rollback.restored_routing_generation is None
    assert rollback.restored_cost_generation is None
    assert database.routing[1]["generation_state"] == "applying"
    assert database.costs[1]["pricing_state"] == "confirmed"
    assert database.slots[0]["valid_to"] == "closed"
    assert store.rollback_activation(desired) == rollback


def test_postgres_activation_rollback_is_atomic_with_inventory_projection() -> None:
    database, desired = rollback_database(with_parent=True)
    database.inventory_projected_count = 0
    before = database.snapshot()
    store = PostgresProviderPoolStore(database.connection)

    with pytest.raises(
        ProviderInventoryError,
        match="provider_inventory_active_projection_mismatch",
    ):
        store.rollback_activation(desired)

    assert database.snapshot() == before
    failed = database.connections[-1]
    assert failed.commits == 0
    assert failed.rollbacks == 1


def test_postgres_activation_rollback_fails_closed_on_parent_slot_drift() -> None:
    database, desired = rollback_database(
        with_parent=True,
        fail_parent_slot_restore=True,
    )
    before = database.snapshot()
    store = PostgresProviderPoolStore(database.connection)

    with pytest.raises(ProviderPoolError, match="parent_slots_restore_failed"):
        store.rollback_activation(desired)

    assert database.snapshot() == before
    failed = database.connections[-1]
    assert failed.commits == 0
    assert failed.rollbacks == 1
    assert failed.closed is True
