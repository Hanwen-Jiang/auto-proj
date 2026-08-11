#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import stat
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row

from a6api_auto.client import A6ApiClient
from a6api_auto.storage import SessionStore, read_private_json
from a6api_auto.trade_model_publication import (
    A6SupplierListingPublicationRemote,
    CompositePublicationRemote,
    ManagedPublicationListing,
    PostgresPublicationActionLedger,
    PostgresPublicationDiscovery,
    PostgresPublicationModelLease,
    PostgresPublicationStore,
    PublicationController,
    PublicationDecision,
    PublicationState,
    SellerCostGuardPlan,
    SellerCostGuardPublicationRemote,
    apply_seller_cost_guard,
)
from a6api_auto.trade_publication_control import PostgresPublicationControl
from a6api_auto.workflow import Workflow

_DIRECT_A6_ORIGIN = "https://a6.a6api.com"
_TRUE_VALUES = {"1", "true", "yes", "on"}
_FALSE_VALUES = {"", "0", "false", "no", "off"}


class ModelPublicationCommandError(RuntimeError):
    pass


def resolve_enabled(
    cli_enabled: bool,
    *,
    environ: Mapping[str, str] | None = None,
    env_name: str = "MODEL_PUBLICATION_RECONCILE_ENABLED",
) -> bool:
    if not isinstance(cli_enabled, bool):
        raise ModelPublicationCommandError("model_publication_flag_invalid")
    if cli_enabled:
        return True
    environment = os.environ if environ is None else environ
    value = str(environment.get(env_name, "")).strip().casefold()
    if value in _TRUE_VALUES:
        return True
    if value in _FALSE_VALUES:
        return False
    raise ModelPublicationCommandError("model_publication_flag_invalid")


def run_model(
    model: str,
    *,
    enabled: bool,
    apply: bool,
    discovery: PostgresPublicationDiscovery | None = None,
    controller_factory: Callable[..., PublicationController] | None = None,
    minimum_healthy_slots: int = 1,
    redundancy_target: int = 3,
    control_max_age_seconds: int = 120,
    minimum_health_observers: int = 2,
    seller_collector_freshness_seconds: int = 900,
    seller_success_lookback_seconds: int = 86400,
    request_queue: PostgresPublicationControl | None = None,
) -> dict[str, Any]:
    if not isinstance(enabled, bool) or not isinstance(apply, bool):
        raise ModelPublicationCommandError("model_publication_mode_invalid")
    model = str(model or "").strip()
    if not model:
        raise ModelPublicationCommandError("model_publication_model_invalid")
    if (
        isinstance(seller_success_lookback_seconds, bool)
        or not isinstance(seller_success_lookback_seconds, int)
        or seller_success_lookback_seconds < 1
    ):
        raise ModelPublicationCommandError("model_publication_success_lookback_invalid")
    if not enabled:
        return {
            "success": True,
            "model": model,
            "enabled": False,
            "apply": False,
            "action": "disabled",
        }
    if discovery is None:
        raise ModelPublicationCommandError("model_publication_discovery_missing")
    decision = discovery.decision(
        model,
        minimum_healthy_slots=minimum_healthy_slots,
        redundancy_target=redundancy_target,
        control_max_age_seconds=control_max_age_seconds,
        minimum_health_observers=minimum_health_observers,
    )
    targets = discovery.listings(model)
    guard_plan = None
    guard_loader = getattr(discovery, "seller_cost_guard_plan", None)
    if decision.desired_state in {
        PublicationState.PUBLISHED,
        PublicationState.DEGRADED,
    }:
        if not callable(guard_loader):
            decision = PublicationDecision(
                model=decision.model,
                desired_state=PublicationState.UNPUBLISHED,
                healthy_slots=decision.healthy_slots,
                minimum_healthy_slots=decision.minimum_healthy_slots,
                redundancy_target=decision.redundancy_target,
                reason_code="publication_cost_guard_evidence_missing",
                generation=decision.generation,
                cost_generation=decision.cost_generation,
            )
        else:
            guard_plan = guard_loader(
                model,
                targets,
                collector_freshness_seconds=seller_collector_freshness_seconds,
                success_lookback_seconds=seller_success_lookback_seconds,
            )
    if guard_plan is not None:
        decision = apply_seller_cost_guard(decision, guard_plan)
    result = {
        "success": True,
        "model": model,
        "enabled": True,
        "apply": apply,
        "target_listing_count": len(targets),
        "target_user_count": len({target.a6_user_id for target in targets}),
        "desired_state": decision.desired_state.value,
        "healthy_slots": decision.healthy_slots,
        "minimum_healthy_slots": decision.minimum_healthy_slots,
        "redundancy_target": decision.redundancy_target,
        "generation": decision.generation,
        "cost_generation": decision.cost_generation,
        "reason_code": decision.reason_code,
    }
    if guard_plan is not None:
        result["seller_cost_guard"] = {
            "protected_provider_cost": format(guard_plan.protected_provider_cost, "f"),
            "maximum_provider_cost_ceiling": format(guard_plan.maximum_provider_cost_ceiling, "f"),
            "publishable_listing_count": guard_plan.publishable_listing_count,
            "targets": [
                {
                    "merchant_id": item.listing.merchant_id,
                    "listing_id": item.listing.listing_id,
                    "provider_cost_ceiling": format(item.provider_cost_ceiling, "f"),
                    "desired_state": item.desired_state.value,
                    "reason_code": item.reason_code,
                }
                for item in guard_plan.targets
            ],
        }
    if not apply:
        return {**result, "action": "planned"}
    if controller_factory is None:
        raise ModelPublicationCommandError("model_publication_controller_factory_missing")
    # Leave durable requests pending while the remote saga runs.  If the
    # process terminates at any remote-write boundary, the next invocation
    # performs the controller's idempotent readback recovery before claiming
    # and completing the request.
    controller = (
        controller_factory(targets)
        if guard_plan is None
        else controller_factory(targets, guard_plan)
    )
    report = controller.reconcile(decision)
    response = {
        **result,
        "action": report.action,
        "action_disposition": report.action_disposition,
        "observed_state": report.record.observed_state.value,
        "observed_listing_state": report.observed_listing_state.value,
        "remote_write_performed": report.remote_write_performed,
        "probe_request_id": report.probe_request_id,
        "state_version": report.record.state_version,
    }
    request_claim = request_queue.claim_next(model) if request_queue is not None else None
    if request_queue is not None and request_claim is not None:
        request_queue.complete(request_claim, response)
        response["reconcile_request"] = {
            "id": request_claim.request_id,
            "source": request_claim.request_source,
            "attempt_count": request_claim.attempt_count,
        }
    return response


def build_controller(
    targets: tuple[ManagedPublicationListing, ...],
    *,
    connection_factory: Callable[[], Any],
    sessions_dir: Path,
    proxy_by_user_id: Mapping[int, str],
    probe_api_key: str,
    config_version: str,
    cost_guard_plan: SellerCostGuardPlan | None = None,
    panel_timeout_seconds: float = 10,
    probe_timeout_seconds: float = 6,
    probe_max_output_tokens: int = 16,
) -> PublicationController:
    if not targets:
        raise ModelPublicationCommandError("model_publication_targets_empty")
    panel_clients: dict[int, A6ApiClient] = {}
    probe_client = _direct_probe_client(probe_timeout_seconds)
    root = sessions_dir.expanduser().resolve()
    remotes = []
    remotes_by_identity = {}
    for target in targets:
        client = panel_clients.get(target.a6_user_id)
        if client is None:
            session_path = Path(target.session_ref).expanduser().resolve()
            if not session_path.is_relative_to(root):
                raise ModelPublicationCommandError("model_publication_session_ref_outside_root")
            store = SessionStore(session_path)
            record = store.load()
            if int(record.user_id or 0) != target.a6_user_id:
                raise ModelPublicationCommandError("model_publication_session_identity_mismatch")
            proxy_url = str(proxy_by_user_id.get(target.a6_user_id) or "").strip()
            if not proxy_url:
                raise ModelPublicationCommandError("model_publication_panel_proxy_missing")
            client = A6ApiClient(
                record.origin,
                proxy_url=proxy_url,
                timeout=panel_timeout_seconds,
            )
            if str(record.status or "") == "allocated":
                raise ModelPublicationCommandError("model_publication_session_not_registered")
            Workflow(client, _NoMailbox(), store).ensure_authenticated(record)
            if int(record.user_id or 0) != target.a6_user_id:
                raise ModelPublicationCommandError(
                    "model_publication_authenticated_identity_mismatch"
                )
            panel_clients[target.a6_user_id] = client
        remote = A6SupplierListingPublicationRemote(
            client,
            probe_client=probe_client,
            user_id=target.a6_user_id,
            listing_id=target.listing_id,
            probe_api_key=probe_api_key,
            probe_timeout=probe_timeout_seconds,
            probe_max_output_tokens=probe_max_output_tokens,
        )
        remotes.append(remote)
        remotes_by_identity[(target.a6_user_id, target.listing_id)] = remote
    # Every primitive has a direct client; the composite deliberately spends one probe.
    if cost_guard_plan is not None and cost_guard_plan.any_publishable:
        probe_remote = next(
            remotes_by_identity[(target.listing.a6_user_id, target.listing.listing_id)]
            for target in cost_guard_plan.targets
            if target.desired_state.value == "published"
        )
        remote = SellerCostGuardPublicationRemote(
            remotes_by_identity,
            plan=cost_guard_plan,
            probe_remote=probe_remote,
        )
    else:
        remote = CompositePublicationRemote(tuple(remotes), probe_remote=remotes[0])
    return PublicationController(
        PostgresPublicationStore(connection_factory),
        remote,
        ledger=PostgresPublicationActionLedger(
            connection_factory,
            config_version=config_version,
        ),
        model_lease=PostgresPublicationModelLease(connection_factory),
    )


def _direct_probe_client(timeout: float) -> A6ApiClient:
    client = A6ApiClient(
        _DIRECT_A6_ORIGIN,
        proxy_url="",
        timeout=timeout,
    )
    if client.origin != _DIRECT_A6_ORIGIN or client.proxy_url:
        raise ModelPublicationCommandError("model_publication_probe_client_not_direct")
    return client


class _NoMailbox:
    def snapshot(self, _email: str):
        raise ModelPublicationCommandError("model_publication_mailbox_forbidden")

    def poll_code(self, *_args: Any, **_kwargs: Any):
        raise ModelPublicationCommandError("model_publication_mailbox_forbidden")


def _proxy_map(path: Path) -> dict[int, str]:
    try:
        document = read_private_json(path)
        result = {int(key): str(value).strip() for key, value in document.items()}
    except (TypeError, ValueError):
        raise ModelPublicationCommandError("model_publication_proxy_map_invalid") from None
    if any(user_id < 1 or not proxy for user_id, proxy in result.items()):
        raise ModelPublicationCommandError("model_publication_proxy_map_invalid")
    return result


def _private_text(path: Path) -> str:
    resolved = path.expanduser().resolve()
    try:
        info = resolved.stat()
        value = resolved.read_text(encoding="utf-8").strip()
    except OSError:
        raise ModelPublicationCommandError("model_publication_probe_key_unreadable") from None
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or not value:
        raise ModelPublicationCommandError("model_publication_probe_key_invalid")
    return value


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(
        description="Reconcile one model across every managed A6 seller listing"
    )
    command.add_argument("--model", required=True)
    command.add_argument("--apply", action="store_true")
    command.add_argument(
        "--model-publication-reconcile-enabled",
        action="store_true",
        help="explicitly enable this default-off controller",
    )
    command.add_argument(
        "--enabled-env",
        default="MODEL_PUBLICATION_RECONCILE_ENABLED",
    )
    command.add_argument("--database-dsn-env", default="A6_TRADE_DATABASE_DSN")
    command.add_argument(
        "--sessions-dir",
        type=Path,
        default=Path("/var/lib/a6-trader/secrets/sessions"),
    )
    command.add_argument(
        "--proxy-map-file",
        type=Path,
        default=Path("/var/lib/a6-trader/secrets/proxy-map.json"),
        help="A6 panel session proxy map; never used by the Responses probe client",
    )
    command.add_argument(
        "--probe-api-key-file",
        type=Path,
        default=Path("/var/lib/a6-trader/secrets/provider-publication-probe-key.secret"),
    )
    command.add_argument("--minimum-healthy-slots", type=int, default=1)
    command.add_argument("--redundancy-target", type=int, default=3)
    command.add_argument("--control-max-age-seconds", type=int, default=120)
    command.add_argument("--minimum-health-observers", type=int, default=2)
    command.add_argument("--seller-collector-freshness-seconds", type=int, default=900)
    command.add_argument("--seller-success-lookback-seconds", type=int, default=86400)
    command.add_argument("--panel-timeout-seconds", type=float, default=10)
    command.add_argument("--probe-timeout-seconds", type=float, default=6)
    command.add_argument("--probe-max-output-tokens", type=int, default=16)
    command.add_argument(
        "--config-version",
        default="a6-provider-publication-v1",
    )
    return command


def main() -> None:
    args = parser().parse_args()
    enabled = resolve_enabled(
        args.model_publication_reconcile_enabled,
        env_name=args.enabled_env,
    )
    if not enabled:
        print(
            json.dumps(
                run_model(args.model, enabled=False, apply=args.apply),
                ensure_ascii=True,
                sort_keys=True,
            )
        )
        return
    dsn = str(os.environ.get(args.database_dsn_env, "")).strip()
    if not dsn:
        raise ModelPublicationCommandError("model_publication_database_dsn_missing")

    def connection_factory() -> Any:
        return psycopg.connect(dsn, row_factory=dict_row)

    discovery = PostgresPublicationDiscovery(connection_factory)
    request_queue = PostgresPublicationControl(connection_factory)
    controller_factory = None
    if args.apply:
        proxy_by_user_id = _proxy_map(args.proxy_map_file)
        probe_api_key = _private_text(args.probe_api_key_file)

        def controller_factory(
            targets: tuple[ManagedPublicationListing, ...],
            cost_guard_plan: SellerCostGuardPlan | None = None,
        ) -> PublicationController:
            return build_controller(
                targets,
                connection_factory=connection_factory,
                sessions_dir=args.sessions_dir,
                proxy_by_user_id=proxy_by_user_id,
                probe_api_key=probe_api_key,
                config_version=args.config_version,
                cost_guard_plan=cost_guard_plan,
                panel_timeout_seconds=args.panel_timeout_seconds,
                probe_timeout_seconds=args.probe_timeout_seconds,
                probe_max_output_tokens=args.probe_max_output_tokens,
            )

    result = run_model(
        args.model,
        enabled=True,
        apply=args.apply,
        discovery=discovery,
        controller_factory=controller_factory,
        minimum_healthy_slots=args.minimum_healthy_slots,
        redundancy_target=args.redundancy_target,
        control_max_age_seconds=args.control_max_age_seconds,
        minimum_health_observers=args.minimum_health_observers,
        seller_collector_freshness_seconds=args.seller_collector_freshness_seconds,
        seller_success_lookback_seconds=args.seller_success_lookback_seconds,
        request_queue=request_queue,
    )
    print(json.dumps(result, ensure_ascii=True, sort_keys=True))


if __name__ == "__main__":
    main()
