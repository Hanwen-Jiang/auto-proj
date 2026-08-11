from __future__ import annotations

import json
import math
import random
import re
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import ROUND_CEILING, Decimal
from pathlib import Path
from typing import Any, Protocol

from .client import A6ApiClient
from .enodot_admin import EnodotAdminClient
from .errors import A6AutoError
from .merchant_policy import FOCUS_OPERATING_GROUP
from .storage import SessionStore
from .trade_authenticated_client import (
    AuthenticatedClientFactory,
    AuthenticatedSessionError,
    authenticated_a6_client,
)
from .trade_channel_health import ChannelHealthInput, classify_channel_health
from .trade_enodot_seller_resources import PrivateFileManagedSellerSecretSink
from .trade_managed_listings import (
    ManagedListingController,
    ManagedMerchant,
)

UTC = timezone.utc
CALLBACK_TEXT = "CALLBACK 5db98307ce2d442ee568baacf8b0e7076fa657cbe2806ccff9da6386997915e2"
CALLBACK_RESPONSE = "CALLBACK"
CALLBACK_RULE_ID = "callback-0001"
_USD_QUANTUM = Decimal("0.00000001")
_ERROR_CODE = re.compile(r"^[a-z0-9][a-z0-9_.:-]{0,127}$")
_RECOVERY_WINDOW_GRACE_SECONDS = 5
_PROBE_WINDOW_GRACE_SECONDS = 1
_CALLBACK_ACTION_CLAIM_WINDOW_SECONDS = 10
_STALE_ACTION_SECONDS = 10 * 60
_READINESS_PROBE_INTERVAL_SECONDS = 1.0
_RECYCLE_FAILURE_RETRY_SECONDS = 30
_A6_READINESS_RETRY_ERRORS = frozenset({"callback_http_503"})
_DEFAULT_SELLER_READINESS_URL = "https://ai.enodot.shop/v1"


class ChannelGrowthError(A6AutoError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def authenticated_channel_growth_client(
    session_ref: Path | str,
    *,
    user_id: int,
    proxy_url: str,
    client_factory: AuthenticatedClientFactory | None = None,
) -> tuple[Any, Any]:
    builder = client_factory or (
        lambda current, fixed_proxy: A6ApiClient(
            current.origin,
            proxy_url=fixed_proxy,
            timeout=15,
        )
    )
    try:
        return authenticated_a6_client(
            session_ref,
            expected_user_id=user_id,
            proxy_url=proxy_url,
            client_factory=builder,
        )
    except AuthenticatedSessionError as exc:
        code = {
            "session_user_identity_mismatch": "channel_growth_session_mismatch",
            "session_not_refreshable": "channel_growth_session_ineligible",
            "session_proxy_missing": "channel_growth_proxy_missing",
        }.get(exc.code, f"channel_growth_{exc.code}")
        raise ChannelGrowthError(code) from exc


@dataclass(frozen=True)
class GrowthTarget:
    listing_binding_id: int
    managed_uid: str
    merchant_id: int
    a6_user_id: int
    supplier_id: int | None
    roles: tuple[str, ...]
    session_ref: str
    enodot_user_id: int
    enodot_managed_uid: str
    enodot_key_secret_ref: str
    model: str
    channel_id: int
    listing_id: int
    input_price_micros: int
    output_price_micros: int
    cache_read_price_micros: int
    cache_write_price_micros: int
    listing_status: int | None
    available: bool
    route_status: str
    circuit_state: str
    circuit_probe_status: str
    circuit_last_error_kind: str
    circuit_probe_success_required: int | None
    circuit_probe_success_streak: int | None
    circuit_scope: str
    circuit_recovery_mode: str
    circuit_cooling_until: datetime | None
    circuit_last_cooling_at: datetime | None
    circuit_last_probe_at: datetime | None
    circuit_next_probe_at: datetime | None
    cooldown_state: str
    cooldown_remaining_seconds: int | None
    cooldown_expected_recovery_at: datetime | None
    cooldown_next_probe_at: datetime | None
    smart_routing_status: str
    smart_routing_labels: tuple[str, ...]
    last_success_at: datetime | None
    last_failure_at: datetime | None
    last_failure_code: str
    sample_count: int | None
    recent_success_rate: int | None
    recent_p50_ms: Decimal | None
    p50_ttft_ms: Decimal | None
    signal_observed_at: datetime
    health_observer_count: int
    health_confirmation_count: int
    recycle_enabled: bool
    callback_enabled: bool
    action_cooldown_seconds: int
    callback_cooldown_seconds: int
    callback_requests: int
    callback_concurrency: int
    daily_budget_micros: int
    daily_spent_micros: int
    daily_readiness_budget_micros: int
    daily_readiness_spent_micros: int
    working_balance_low_usd: Decimal
    working_balance_target_usd: Decimal
    degraded_success_rate: int
    idle_seconds: int
    minimum_health_observers: int
    maximum_consecutive_failures: int
    consecutive_failures: int
    last_recycle_at: datetime | None
    recycle_next_allowed_at: datetime | None
    last_callback_at: datetime | None
    callback_next_allowed_at: datetime | None
    last_readiness_at: datetime | None
    readiness_next_allowed_at: datetime | None

    def health_state(self, now: datetime) -> str:
        return classify_channel_health(
            ChannelHealthInput(
                available=self.available,
                route_status=self.route_status,
                circuit_state=self.circuit_state,
                circuit_cooling_until=_epoch(self.circuit_cooling_until),
                cooldown_state=self.cooldown_state,
                cooldown_remaining_seconds=self.cooldown_remaining_seconds,
                cooldown_expected_recovery_at=_epoch(
                    self.cooldown_expected_recovery_at
                ),
                smart_routing_status=self.smart_routing_status,
                smart_routing_labels=self.smart_routing_labels,
                sample_count=self.sample_count,
                recent_success_rate=self.recent_success_rate,
                last_success_at=_epoch(self.last_success_at),
                last_failure_at=_epoch(self.last_failure_at),
                last_failure_code=self.last_failure_code,
            ),
            now_epoch=round(now.timestamp()),
            degraded_success_rate=self.degraded_success_rate,
            idle_seconds=self.idle_seconds,
        )


@dataclass(frozen=True)
class GrowthBooster:
    token_ref_id: int
    merchant_id: int
    a6_user_id: int
    token_id: int
    token_name: str
    session_ref: str
    daily_budget_micros: int
    daily_spent_micros: int
    daily_readiness_budget_micros: int
    daily_readiness_spent_micros: int
    maximum_burst_requests: int


@dataclass(frozen=True)
class GrowthActionClaim:
    action_id: int
    action_type: str
    health_state: str


@dataclass(frozen=True)
class CallbackUsage:
    input_tokens: int
    output_tokens: int
    cached_tokens: int
    cache_creation_tokens: int


@dataclass(frozen=True)
class CallbackRequestResult:
    success: bool
    status: int
    elapsed_ms: int
    usage: CallbackUsage | None = None
    error_class: str | None = None


class ChannelGrowthStore(Protocol):
    def recover_stale_actions(self, *, max_age_seconds: int) -> int: ...

    def targets(self, model: str, *, signal_max_age_seconds: int) -> tuple[GrowthTarget, ...]: ...

    def boosters(self, target_merchant_id: int) -> tuple[GrowthBooster, ...]: ...

    def claim_action(
        self,
        target: GrowthTarget,
        *,
        action_type: str,
        health_state: str,
        idempotency_key: str,
        booster_token_ref_id: int | None = None,
        request_count: int = 0,
        estimated_cost_micros: int = 0,
    ) -> GrowthActionClaim | None: ...

    def complete_action(
        self,
        claim: GrowthActionClaim,
        *,
        request_count: int = 0,
        success_count: int = 0,
        failure_count: int = 0,
        actual_cost_micros: int = 0,
        balance_before_usd: Decimal | None = None,
        balance_after_usd: Decimal | None = None,
        details: Mapping[str, Any] | None = None,
        next_allowed_seconds: int,
    ) -> None: ...

    def fail_action(
        self,
        claim: GrowthActionClaim,
        *,
        error_class: str,
        request_count: int = 0,
        success_count: int = 0,
        failure_count: int = 0,
        actual_cost_micros: int = 0,
        balance_before_usd: Decimal | None = None,
        balance_after_usd: Decimal | None = None,
        details: Mapping[str, Any] | None = None,
        next_allowed_seconds: int,
    ) -> None: ...


class CallbackTransport:
    def __init__(self, *, timeout: float = 30) -> None:
        if timeout <= 0:
            raise ChannelGrowthError("callback_timeout_invalid")
        try:
            from curl_cffi import requests

            self._session = requests.Session()
        except Exception:
            raise ChannelGrowthError("http_dependency_unavailable") from None
        self.timeout = float(timeout)

    def request(
        self,
        *,
        url: str,
        api_key: str,
        model: str,
        proxy_url: str = "",
    ) -> CallbackRequestResult:
        payload = {
            "model": model,
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": CALLBACK_TEXT}],
                }
            ],
            "tools": [],
            "tool_choice": "auto",
            "parallel_tool_calls": True,
            "store": False,
            "stream": False,
        }
        options: dict[str, Any] = {
            "url": f"{url.rstrip('/')}/v1/responses",
            "headers": {
                "Authorization": f"Bearer {api_key}",
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "codex_cli_rs/0.144.1",
                "originator": "codex_cli_rs",
            },
            "json": payload,
            "timeout": self.timeout,
            "allow_redirects": False,
            "impersonate": "chrome131",
        }
        if proxy_url:
            options["proxies"] = {"http": proxy_url, "https": proxy_url}
        started = time.monotonic()
        try:
            response = self._session.post(**options)
            status = int(response.status_code)
            document = response.json()
        except Exception:
            return CallbackRequestResult(
                False,
                0,
                _elapsed_ms(started),
                error_class="callback_transport_failed",
            )
        if status != 200 or not isinstance(document, Mapping):
            return CallbackRequestResult(
                False,
                status,
                _elapsed_ms(started),
                error_class=f"callback_http_{status}" if status else "callback_response_invalid",
            )
        content = _response_text(document)
        usage = _callback_usage(document.get("usage"))
        rule_id = str(response.headers.get("X-Sub2api-Probe-Rule") or "").strip()
        if content != CALLBACK_RESPONSE or usage is None:
            return CallbackRequestResult(
                False,
                status,
                _elapsed_ms(started),
                error_class="callback_short_circuit_not_ready",
            )
        if rule_id and rule_id != CALLBACK_RULE_ID:
            return CallbackRequestResult(
                False,
                status,
                _elapsed_ms(started),
                error_class="callback_rule_mismatch",
            )
        if usage.input_tokens < 1 or Decimal(usage.cached_tokens) / Decimal(
            usage.input_tokens
        ) < Decimal("0.8"):
            return CallbackRequestResult(
                False,
                status,
                _elapsed_ms(started),
                error_class="callback_usage_invalid",
            )
        return CallbackRequestResult(True, status, _elapsed_ms(started), usage=usage)

    def batch(
        self,
        *,
        url: str,
        api_key: str,
        model: str,
        count: int,
        concurrency: int,
        proxy_url: str = "",
    ) -> tuple[CallbackRequestResult, ...]:
        if count < 1 or concurrency < 1:
            raise ChannelGrowthError("callback_batch_invalid")
        results: list[CallbackRequestResult] = []
        with ThreadPoolExecutor(max_workers=min(count, concurrency)) as executor:
            futures = [
                executor.submit(
                    self.request,
                    url=url,
                    api_key=api_key,
                    model=model,
                    proxy_url=proxy_url,
                )
                for _ in range(count)
            ]
            for future in as_completed(futures):
                results.append(future.result())
        return tuple(results)


class PostgresChannelGrowthStore:
    def __init__(self, connection_factory: Callable[[], Any]) -> None:
        self.connection_factory = connection_factory

    def recover_stale_actions(self, *, max_age_seconds: int) -> int:
        if (
            isinstance(max_age_seconds, bool)
            or not isinstance(max_age_seconds, int)
            or not 60 <= max_age_seconds <= 86_400
        ):
            raise ChannelGrowthError("channel_growth_stale_action_age_invalid")
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE a6_trade.a6_channel_growth_actions
                    SET state = 'skipped',
                        details = details || jsonb_build_object(
                            'stale_action_recovered', true,
                            'stale_action_recovered_at', to_jsonb(now()),
                            'stale_action_previous_updated_at', to_jsonb(updated_at)
                        ),
                        error_class = 'channel_growth_stale_action_recovered',
                        completed_at = now(),
                        next_allowed_at = now(),
                        updated_at = now()
                    WHERE state = 'running'
                      AND updated_at <= now() - (%s * interval '1 second')
                    """,
                    (max_age_seconds,),
                )
                recovered = int(cursor.rowcount)
            connection.commit()
            return recovered
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def targets(self, model: str, *, signal_max_age_seconds: int) -> tuple[GrowthTarget, ...]:
        if not model or signal_max_age_seconds < 1:
            raise ChannelGrowthError("channel_growth_target_query_invalid")
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    _TARGETS_QUERY,
                    (
                        signal_max_age_seconds,
                        signal_max_age_seconds,
                        model,
                        FOCUS_OPERATING_GROUP,
                    ),
                )
                rows = _rows(cursor)
            return tuple(_target_from_row(row) for row in rows)
        finally:
            connection.close()

    def boosters(self, target_merchant_id: int) -> tuple[GrowthBooster, ...]:
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(_BOOSTERS_QUERY, (target_merchant_id,))
                rows = _rows(cursor)
            return tuple(_booster_from_row(row) for row in rows)
        finally:
            connection.close()

    def claim_action(
        self,
        target: GrowthTarget,
        *,
        action_type: str,
        health_state: str,
        idempotency_key: str,
        booster_token_ref_id: int | None = None,
        request_count: int = 0,
        estimated_cost_micros: int = 0,
    ) -> GrowthActionClaim | None:
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                if booster_token_ref_id is not None:
                    cursor.execute(
                        _BOOSTER_CLAIM_LOCK_QUERY,
                        (booster_token_ref_id,),
                    )
                    if _row(cursor) is None:
                        connection.rollback()
                        return None
                    # The lock query may wait for a concurrent Provider binding
                    # transaction. Recheck eligibility in a new READ COMMITTED
                    # statement so a binding committed while waiting is visible.
                    cursor.execute(
                        _BOOSTER_CLAIM_GUARD_QUERY,
                        (booster_token_ref_id,),
                    )
                    if _row(cursor) is None:
                        connection.rollback()
                        return None
                cursor.execute(
                    """
                    INSERT INTO a6_trade.a6_channel_growth_actions (
                        idempotency_key, merchant_id, listing_binding_id,
                        model, channel_id, listing_id, health_state,
                        action_type, state, booster_token_ref_id,
                        request_count, estimated_cost_micros
                    )
                    SELECT
                        %s, %s, %s, %s, %s, %s, %s,
                        %s, 'running', %s, %s, %s
                    WHERE EXISTS (
                        SELECT 1
                        FROM a6_trade.a6_channel_growth_policies policy
                        WHERE policy.listing_binding_id = %s AND policy.enabled
                    )
                    ON CONFLICT (idempotency_key) DO NOTHING
                    RETURNING id
                    """,
                    (
                        idempotency_key,
                        target.merchant_id,
                        target.listing_binding_id,
                        target.model,
                        target.channel_id,
                        target.listing_id,
                        health_state,
                        action_type,
                        booster_token_ref_id,
                        request_count,
                        estimated_cost_micros,
                        target.listing_binding_id,
                    ),
                )
                row = _row(cursor)
                if row is None:
                    connection.rollback()
                    return None
                if booster_token_ref_id is not None:
                    cursor.execute(
                        """
                        UPDATE a6_trade.a6_channel_booster_tokens
                        SET last_used_at = now(), updated_at = now()
                        WHERE a6_token_ref_id = %s
                        """,
                        (booster_token_ref_id,),
                    )
            connection.commit()
            return GrowthActionClaim(int(row["id"]), action_type, health_state)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def complete_action(
        self,
        claim: GrowthActionClaim,
        *,
        request_count: int = 0,
        success_count: int = 0,
        failure_count: int = 0,
        actual_cost_micros: int = 0,
        balance_before_usd: Decimal | None = None,
        balance_after_usd: Decimal | None = None,
        details: Mapping[str, Any] | None = None,
        next_allowed_seconds: int,
    ) -> None:
        self._finish(
            claim,
            state="completed",
            request_count=request_count,
            success_count=success_count,
            failure_count=failure_count,
            actual_cost_micros=actual_cost_micros,
            balance_before_usd=balance_before_usd,
            balance_after_usd=balance_after_usd,
            details=details,
            error_class=None,
            next_allowed_seconds=next_allowed_seconds,
        )

    def fail_action(
        self,
        claim: GrowthActionClaim,
        *,
        error_class: str,
        request_count: int = 0,
        success_count: int = 0,
        failure_count: int = 0,
        actual_cost_micros: int = 0,
        balance_before_usd: Decimal | None = None,
        balance_after_usd: Decimal | None = None,
        details: Mapping[str, Any] | None = None,
        next_allowed_seconds: int,
    ) -> None:
        self._finish(
            claim,
            state="failed",
            request_count=request_count,
            success_count=success_count,
            failure_count=failure_count,
            actual_cost_micros=actual_cost_micros,
            balance_before_usd=balance_before_usd,
            balance_after_usd=balance_after_usd,
            details=details,
            error_class=_safe_error_code(error_class),
            next_allowed_seconds=next_allowed_seconds,
        )

    def _finish(
        self,
        claim: GrowthActionClaim,
        *,
        state: str,
        request_count: int = 0,
        success_count: int = 0,
        failure_count: int = 0,
        actual_cost_micros: int = 0,
        balance_before_usd: Decimal | None = None,
        balance_after_usd: Decimal | None = None,
        details: Mapping[str, Any] | None = None,
        error_class: str | None,
        next_allowed_seconds: int,
    ) -> None:
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE a6_trade.a6_channel_growth_actions
                    SET state = %s,
                        request_count = greatest(request_count, %s),
                        success_count = %s,
                        failure_count = %s,
                        actual_cost_micros = %s,
                        balance_before_usd = %s,
                        balance_after_usd = %s,
                        details = %s::jsonb,
                        error_class = %s,
                        completed_at = now(),
                        next_allowed_at = now() + (%s * interval '1 second'),
                        updated_at = now()
                    WHERE id = %s AND state = 'running'
                    """,
                    (
                        state,
                        request_count,
                        success_count,
                        failure_count,
                        actual_cost_micros,
                        balance_before_usd,
                        balance_after_usd,
                        json.dumps(dict(details or {}), ensure_ascii=True),
                        error_class,
                        max(0, next_allowed_seconds),
                        claim.action_id,
                    ),
                )
                if cursor.rowcount != 1:
                    raise ChannelGrowthError("channel_growth_action_finish_conflict")
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()


class ChannelGrowthRunner:
    def __init__(
        self,
        store: ChannelGrowthStore,
        *,
        listing_controller: ManagedListingController,
        sessions_dir: Path,
        proxy_by_user_id: Mapping[int, str],
        enodot_admin: EnodotAdminClient,
        seller_secret_sink: PrivateFileManagedSellerSecretSink,
        callback_transport: CallbackTransport,
        callback_gateway_url: str = "https://a6.a6api.com",
        usage_samples: Sequence[Mapping[str, Any]] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        random_uniform: Callable[[float, float], float] = random.uniform,
    ) -> None:
        self.store = store
        self.listing_controller = listing_controller
        self.proxy_by_user_id = {int(key): str(value) for key, value in proxy_by_user_id.items()}
        self.enodot_admin = enodot_admin
        self.seller_secret_sink = seller_secret_sink
        self.callback_transport = callback_transport
        self.callback_gateway_url = callback_gateway_url.rstrip("/")
        self.sleep = sleep
        self.random_uniform = random_uniform
        self._records: dict[int, Any] = {}
        self._session_paths: dict[int, Path] = {}
        for path in sorted(sessions_dir.expanduser().resolve().glob("session-*.json")):
            resolved = path.expanduser().resolve()
            record = SessionStore(resolved).load()
            if record.user_id:
                self._records[int(record.user_id)] = record
                self._session_paths[int(record.user_id)] = resolved
        samples = usage_samples if usage_samples is not None else _load_usage_samples()
        self.usage_samples = tuple(_callback_usage(sample) for sample in samples)
        if not self.usage_samples or any(sample is None for sample in self.usage_samples):
            raise ChannelGrowthError("callback_usage_samples_invalid")

    def a6_client(self, user_id: int) -> A6ApiClient:
        path = self._session_paths.get(user_id)
        if path is None:
            raise ChannelGrowthError("channel_growth_session_missing")
        proxy = self.proxy_by_user_id.get(user_id)
        if not proxy:
            raise ChannelGrowthError("channel_growth_proxy_missing")
        client, record = authenticated_channel_growth_client(
            path,
            user_id=user_id,
            proxy_url=proxy,
        )
        self._records[user_id] = record
        return client

    def run(
        self,
        *,
        model: str,
        signal_max_age_seconds: int = 45,
        recycle_min_delay_seconds: float = 0.8,
        recycle_max_delay_seconds: float = 2.0,
        apply: bool = False,
    ) -> dict[str, Any]:
        if (
            not model
            or signal_max_age_seconds < 1
            or recycle_min_delay_seconds < 0.8
            or recycle_max_delay_seconds > 2.0
            or recycle_min_delay_seconds > recycle_max_delay_seconds
        ):
            raise ChannelGrowthError("channel_growth_run_config_invalid")
        held: dict[str, int] = {}
        recover_stale_actions = getattr(self.store, "recover_stale_actions", None)
        if apply and callable(recover_stale_actions):
            recovered = recover_stale_actions(max_age_seconds=_STALE_ACTION_SECONDS)
            if recovered:
                held["stale_action_recovered"] = recovered
        now = datetime.now(UTC)
        targets = self.store.targets(model, signal_max_age_seconds=signal_max_age_seconds)
        for target in targets:
            health_state = target.health_state(now)
            action_type, reason = self._action_for(target, health_state, now)
            if action_type is None:
                held[reason] = held.get(reason, 0) + 1
                continue
            if not apply:
                return _run_summary(
                    targets,
                    mode="planned",
                    target=target,
                    health_state=health_state,
                    action_type=action_type,
                    held=held,
                )
            if action_type == "listing_recycle":
                result = self._recycle(
                    targets,
                    target,
                    health_state,
                    min_delay=recycle_min_delay_seconds,
                    max_delay=recycle_max_delay_seconds,
                    held=held,
                )
            else:
                result = self._boost(
                    targets,
                    target,
                    health_state,
                    held=held,
                    action_type=action_type,
                )
            if result.get("mode") == "held":
                held = {
                    str(reason): int(count)
                    for reason, count in dict(result.get("held") or held).items()
                }
                continue
            return result
        return {
            "success": True,
            "mode": "held",
            "model": model,
            "target_count": len(targets),
            "held": held,
            "maximum_actions_per_run": 1,
        }

    @staticmethod
    def _action_for(
        target: GrowthTarget,
        health_state: str,
        now: datetime,
    ) -> tuple[str | None, str]:
        if target.listing_status not in {0, 2}:
            return None, "listing_status_not_actionable"
        isolated = target.listing_status == 0 or _listing_recovery_pending(target)
        if (
            not isolated
            and health_state
            in {"cooling", "unavailable", "degraded", "unlabeled", "idle"}
            and (
                target.health_observer_count < target.minimum_health_observers
                or target.health_confirmation_count < target.minimum_health_observers
            )
        ):
            return None, "awaiting_observer_quorum"
        if isolated:
            if not target.callback_enabled:
                return None, "callback_disabled"
            if _uses_probe_recovery(target):
                if target.circuit_probe_status.strip().lower() == "running":
                    return None, "platform_probe_running"
                required = target.circuit_probe_success_required or 1
                streak = target.circuit_probe_success_streak or 0
                if streak >= required:
                    return None, "awaiting_circuit_probe_completion"
                if _before(target.readiness_next_allowed_at, now):
                    return None, "readiness_next_allowed"
                probe_not_before = _probe_not_before(target)
                if probe_not_before is not None and now < probe_not_before:
                    return None, "awaiting_circuit_probe_window"
            elif health_state in {"cooling", "unavailable"}:
                recovery_deadline = _recovery_deadline(target)
                if recovery_deadline is not None and now < recovery_deadline:
                    return None, "awaiting_circuit_recovery_window"
            if _before(target.readiness_next_allowed_at, now):
                return None, "readiness_next_allowed"
            return "callback_readiness", ""
        if health_state in {"cooling", "unavailable"}:
            if not target.callback_enabled:
                return None, "callback_disabled"
            if health_state == "cooling":
                if _uses_probe_recovery(target):
                    if target.circuit_probe_status.strip().lower() == "running":
                        return None, "platform_probe_running"
                    required = target.circuit_probe_success_required or 1
                    streak = target.circuit_probe_success_streak or 0
                    if streak >= required:
                        return None, "awaiting_circuit_probe_completion"
                    probe_not_before = _probe_not_before(target)
                    if probe_not_before is not None and now < probe_not_before:
                        return None, "awaiting_circuit_probe_window"
                else:
                    recovery_deadline = _recovery_deadline(target)
                    if recovery_deadline is not None and now < recovery_deadline:
                        return None, "awaiting_circuit_recovery_window"
            if _before(target.readiness_next_allowed_at, now):
                return None, "readiness_next_allowed"
            return "callback_readiness", ""
        if health_state in {"degraded", "unlabeled", "idle"}:
            if not target.callback_enabled:
                return None, "callback_disabled"
            if _before(target.callback_next_allowed_at, now) or _within(
                target.last_callback_at, now, target.callback_cooldown_seconds
            ):
                return None, "callback_cooldown"
            return "callback_burst", ""
        return None, f"health_{health_state}"

    def _recycle(
        self,
        targets: Sequence[GrowthTarget],
        target: GrowthTarget,
        health_state: str,
        *,
        min_delay: float,
        max_delay: float,
        held: Mapping[str, int],
    ) -> dict[str, Any]:
        key = _action_key(target, "listing_recycle", target.action_cooldown_seconds)
        if target.consecutive_failures:
            key = f"{key}:retry:{target.consecutive_failures}"
        claim = self.store.claim_action(
            target,
            action_type="listing_recycle",
            health_state=health_state,
            idempotency_key=key,
        )
        if claim is None:
            return _run_summary(
                targets,
                mode="held",
                target=target,
                health_state=health_state,
                action_type="listing_recycle",
                held=_increment_held(held, "action_claim_conflict"),
            )
        before_balance: Decimal | None = None
        after_balance: Decimal | None = None
        details: dict[str, Any] = {
            "listing_isolation": True,
            "balance_credit_applied": False,
            "price_preserved": False,
            "reactivation_attempted": False,
        }
        try:
            merchant = ManagedMerchant(
                merchant_id=target.merchant_id,
                a6_user_id=target.a6_user_id,
                supplier_id=target.supplier_id,
                roles=target.roles,
            )
            expected_prices = _target_prices(target)
            status_before = self._listing_status(target)
            details["status_before"] = status_before
            if status_before != 0:
                details["takedown"] = self._listing_operation(
                    merchant,
                    target.managed_uid,
                    operation="takedown",
                )
            live = self._live_listing(target)
            if int(live.get("status", -1)) != 0:
                raise ChannelGrowthError("channel_growth_listing_not_isolated")
            if _listing_prices(live) != expected_prices:
                raise ChannelGrowthError("channel_growth_listing_price_changed")
            details["price_preserved"] = True
            details["final_status"] = 0
            self.store.complete_action(
                claim,
                balance_before_usd=before_balance,
                balance_after_usd=after_balance,
                details=details,
                next_allowed_seconds=target.action_cooldown_seconds,
            )
            return _run_summary(
                targets,
                mode="completed",
                target=target,
                health_state=health_state,
                action_type="listing_recycle",
                held=held,
                action_id=claim.action_id,
                details=details,
            )
        except Exception as exc:
            error = _exception_code(exc, "channel_growth_recycle_failed")
            details["final_listing_active"] = self._listing_status(target) != 0
            retry_seconds = min(
                target.action_cooldown_seconds,
                _RECYCLE_FAILURE_RETRY_SECONDS,
            )
            details["retry_delay_seconds"] = retry_seconds
            self.store.fail_action(
                claim,
                error_class=error,
                balance_before_usd=before_balance,
                balance_after_usd=after_balance,
                details=details,
                next_allowed_seconds=retry_seconds,
            )
            return _run_summary(
                targets,
                success=False,
                mode="failed",
                target=target,
                health_state=health_state,
                action_type="listing_recycle",
                held=held,
                action_id=claim.action_id,
                error_class=error,
                details=details,
            )

    def _boost(
        self,
        targets: Sequence[GrowthTarget],
        target: GrowthTarget,
        health_state: str,
        *,
        held: Mapping[str, int],
        action_type: str = "callback_burst",
    ) -> dict[str, Any]:
        if action_type not in {"callback_readiness", "callback_burst"}:
            raise ChannelGrowthError("channel_growth_callback_action_invalid")
        per_request = max(
            callback_cost_micros(sample, target)
            for sample in self.usage_samples
            if sample is not None
        )
        readiness = action_type == "callback_readiness"
        target_remaining = max(
            0,
            (
                target.daily_readiness_budget_micros - target.daily_readiness_spent_micros
                if readiness
                else target.daily_budget_micros - target.daily_spent_micros
            ),
        )
        candidates = self.store.boosters(target.merchant_id)
        selection: tuple[GrowthBooster, int] | None = None
        for booster in candidates:
            booster_remaining = max(
                0,
                (
                    booster.daily_readiness_budget_micros - booster.daily_readiness_spent_micros
                    if readiness
                    else booster.daily_budget_micros - booster.daily_spent_micros
                ),
            )
            desired_requests = target.callback_requests
            if action_type == "callback_readiness":
                desired_requests = max(
                    1,
                    (target.circuit_probe_success_required or 1)
                    - (target.circuit_probe_success_streak or 0),
                )
            count = min(
                desired_requests,
                booster.maximum_burst_requests,
                target_remaining // max(1, per_request),
                booster_remaining // max(1, per_request),
            )
            if count >= 1:
                selection = booster, count
                break
        if selection is None:
            return _run_summary(
                targets,
                mode="held",
                target=target,
                health_state=health_state,
                action_type=action_type,
                held=_increment_held(held, "callback_budget_or_booster_unavailable"),
            )
        booster, request_count = selection
        estimated_cost = per_request * request_count
        key = _action_key(
            target,
            action_type,
            _CALLBACK_ACTION_CLAIM_WINDOW_SECONDS,
        )
        claim = self.store.claim_action(
            target,
            action_type=action_type,
            health_state=health_state,
            idempotency_key=key,
            booster_token_ref_id=booster.token_ref_id,
            request_count=request_count,
            estimated_cost_micros=estimated_cost,
        )
        if claim is None:
            return _run_summary(
                targets,
                mode="held",
                target=target,
                health_state=health_state,
                action_type=action_type,
                held=_increment_held(held, "action_claim_conflict"),
            )

        details: dict[str, Any] = {
            "booster_merchant_id": booster.merchant_id,
            "booster_token_id": booster.token_id,
            "requested": request_count,
            "callback_concurrency": min(target.callback_concurrency, request_count),
            "callback_batch_concurrency": min(
                target.callback_concurrency,
                max(0, request_count - 1),
            ),
            "estimated_cost_micros": estimated_cost,
            "budget_scope": "readiness" if readiness else "growth",
            "target_budget_remaining_micros": target_remaining,
            "booster_budget_remaining_micros": booster_remaining,
            "pin_restored": False,
            "public_readiness": False,
            "a6_readiness": False,
            "action_mode": action_type,
            "circuit_recovery_mode": target.circuit_recovery_mode,
            "circuit_recovery_deadline": _isoformat(_recovery_deadline(target)),
            "circuit_probe_not_before": _isoformat(_probe_not_before(target)),
            "circuit_probe_success_required": target.circuit_probe_success_required,
            "circuit_probe_success_streak": target.circuit_probe_success_streak,
            "listing_reactivated_for_probe": False,
            "listing_reisolated": False,
        }
        original_pin: Mapping[str, Any] | None = None
        booster_client: A6ApiClient | None = None
        results: tuple[CallbackRequestResult, ...] = ()
        readiness_error: str | None = None
        before_balance: Decimal | None = None
        after_balance: Decimal | None = None
        listing_reactivated = False
        try:
            if booster.merchant_id == target.merchant_id:
                raise ChannelGrowthError("channel_growth_self_boost_blocked")
            before_balance, after_balance = self._ensure_working_balance(
                target,
                claim,
                details,
            )
            target_record = self._record(target.a6_user_id)
            seller_key = self.seller_secret_sink.read_api_key(
                target.enodot_managed_uid,
                target.enodot_key_secret_ref,
            )
            readiness = self.callback_transport.request(
                url=_seller_readiness_url(target_record),
                api_key=seller_key,
                model=target.model,
            )
            details["public_readiness_status"] = readiness.status
            details["public_readiness_ms"] = readiness.elapsed_ms
            if not readiness.success:
                details["public_readiness_error"] = (
                    readiness.error_class or "callback_short_circuit_not_ready"
                )
                raise ChannelGrowthError("callback_public_readiness_failed")
            details["public_readiness"] = True

            if self._listing_status(target) == 0:
                merchant = ManagedMerchant(
                    merchant_id=target.merchant_id,
                    a6_user_id=target.a6_user_id,
                    supplier_id=target.supplier_id,
                    roles=target.roles,
                )
                details["resubmit"] = self._listing_operation(
                    merchant,
                    target.managed_uid,
                    operation="resubmit",
                )
                listing_reactivated = True
                details["listing_reactivated_for_probe"] = True
                live = self._live_listing(target)
                if int(live.get("status", -1)) != 2:
                    raise ChannelGrowthError("channel_growth_listing_not_active")
                if _listing_prices(live) != _target_prices(target):
                    raise ChannelGrowthError("channel_growth_listing_price_changed")

            booster_client = self.a6_client(booster.a6_user_id)
            booster_key = booster_client.ensure_one_key(
                booster.a6_user_id,
                booster.token_name,
            )
            original_pin = _matching_pin(
                booster_client.marketplace_pins(booster.a6_user_id),
                token_id=booster.token_id,
                model=target.model,
            )
            booster_client.marketplace_pin(
                booster.a6_user_id,
                token_id=booster.token_id,
                channel_id=target.channel_id,
                model=target.model,
                fallback_to_smart_routing=False,
            )
            _confirm_pin(
                booster_client,
                booster,
                target.model,
                target.channel_id,
            )
            proxy = self.proxy_by_user_id.get(booster.a6_user_id, "")
            readiness_via_a6 = self.callback_transport.request(
                url=self.callback_gateway_url,
                api_key=booster_key,
                model=target.model,
                proxy_url=proxy,
            )
            results = (readiness_via_a6,)
            details["a6_readiness_status"] = readiness_via_a6.status
            details["a6_readiness_ms"] = readiness_via_a6.elapsed_ms
            if readiness_via_a6.success:
                details["a6_readiness"] = True
                if request_count > 1 and action_type == "callback_readiness":
                    for _index in range(request_count - 1):
                        self.sleep(_READINESS_PROBE_INTERVAL_SECONDS)
                        probe = self.callback_transport.request(
                            url=self.callback_gateway_url,
                            api_key=booster_key,
                            model=target.model,
                            proxy_url=proxy,
                        )
                        results += (probe,)
                        if not probe.success:
                            readiness_error = (
                                probe.error_class or "callback_short_circuit_not_ready"
                            )
                            break
                elif request_count > 1:
                    results += self.callback_transport.batch(
                        url=self.callback_gateway_url,
                        api_key=booster_key,
                        model=target.model,
                        count=request_count - 1,
                        concurrency=min(target.callback_concurrency, request_count - 1),
                        proxy_url=proxy,
                    )
            else:
                readiness_error = readiness_via_a6.error_class or "callback_short_circuit_not_ready"
                details["a6_readiness_error"] = readiness_error
        except Exception as exc:
            error = _exception_code(exc, "channel_growth_callback_failed")
        else:
            error = None
        finally:
            if booster_client is not None:
                try:
                    _restore_pin(
                        booster_client,
                        booster,
                        model=target.model,
                        original_pin=original_pin,
                        sleep=self.sleep,
                    )
                    details["pin_restored"] = True
                except Exception:
                    error = "channel_growth_pin_restore_failed"
            keep_listing_active = (
                bool(results)
                and len(results) == request_count
                and all(result.success for result in results)
            )
            if listing_reactivated and not keep_listing_active:
                try:
                    merchant = ManagedMerchant(
                        merchant_id=target.merchant_id,
                        a6_user_id=target.a6_user_id,
                        supplier_id=target.supplier_id,
                        roles=target.roles,
                    )
                    details["reisolate"] = self._listing_operation(
                        merchant,
                        target.managed_uid,
                        operation="takedown",
                    )
                    if self._listing_status(target) != 0:
                        raise ChannelGrowthError("channel_growth_listing_not_isolated")
                    details["listing_reisolated"] = True
                except Exception:
                    error = "channel_growth_listing_reisolation_failed"

        successes = tuple(result for result in results if result.success)
        failures = len(results) - len(successes)
        actual_cost = sum(
            callback_cost_micros(result.usage, target)
            for result in successes
            if result.usage is not None
        )
        details.update(
            {
                "completed": len(results),
                "successes": len(successes),
                "failures": failures,
                "actual_cost_micros": actual_cost,
                "latency_p50_ms": _nearest_rank([result.elapsed_ms for result in successes], 0.5),
                "http_statuses": _status_counts(results),
            }
        )
        if error is None and readiness_error is not None:
            error = readiness_error
        if error is None and len(successes) != request_count:
            error = "channel_growth_callback_partial_failure"
        next_allowed_seconds = _callback_next_allowed_seconds(
            target,
            error=error,
            now=datetime.now(UTC),
            action_type=action_type,
        )
        details["next_allowed_seconds"] = next_allowed_seconds
        details["remaining_requests"] = max(0, request_count - len(results))
        if error is not None:
            self.store.fail_action(
                claim,
                error_class=error,
                request_count=request_count,
                success_count=len(successes),
                failure_count=failures,
                actual_cost_micros=actual_cost,
                balance_before_usd=before_balance,
                balance_after_usd=after_balance,
                details=details,
                next_allowed_seconds=next_allowed_seconds,
            )
            return _run_summary(
                targets,
                success=False,
                mode="failed",
                target=target,
                health_state=health_state,
                action_type=action_type,
                held=held,
                action_id=claim.action_id,
                error_class=error,
                details=details,
            )
        self.store.complete_action(
            claim,
            request_count=request_count,
            success_count=len(successes),
            failure_count=failures,
            actual_cost_micros=actual_cost,
            balance_before_usd=before_balance,
            balance_after_usd=after_balance,
            details=details,
            next_allowed_seconds=next_allowed_seconds,
        )
        return _run_summary(
            targets,
            mode="completed",
            target=target,
            health_state=health_state,
            action_type=action_type,
            held=held,
            action_id=claim.action_id,
            details=details,
        )

    def _ensure_working_balance(
        self,
        target: GrowthTarget,
        claim: GrowthActionClaim,
        details: dict[str, Any],
    ) -> tuple[Decimal, Decimal]:
        before_user = self.enodot_admin.get_user(target.enodot_user_id)
        before_balance = Decimal(str(before_user["balance"])).quantize(_USD_QUANTUM)
        after_balance = before_balance
        details.setdefault("balance_credit_applied", False)
        if before_balance >= target.working_balance_low_usd:
            return before_balance, after_balance

        amount = (target.working_balance_target_usd - before_balance).quantize(_USD_QUANTUM)
        after_user = self.enodot_admin.add_user_balance(
            target.enodot_user_id,
            amount,
            idempotency_key=f"channel-growth-balance:{claim.action_id}",
            notes=(
                f"A6 channel {target.channel_id} {claim.action_type}; listing {target.listing_id}"
            ),
        )
        after_balance = Decimal(str(after_user["balance"])).quantize(_USD_QUANTUM)
        if after_balance < target.working_balance_low_usd:
            raise ChannelGrowthError("channel_growth_balance_readback_failed")
        details["balance_credit_applied"] = True
        details["balance_credit_usd"] = str(amount)
        return before_balance, after_balance

    def _listing_operation(
        self,
        merchant: ManagedMerchant,
        managed_uid: str,
        *,
        operation: str,
    ) -> Mapping[str, Any]:
        decision = None
        for attempt in range(3):
            if operation == "takedown":
                decision = self.listing_controller.takedown_listing(
                    merchant, managed_uid, apply=True
                )
            else:
                decision = self.listing_controller.resubmit_listing(
                    merchant, managed_uid, apply=True
                )
            if decision.state == "verified" and decision.error_class is None:
                return decision.public()
            if attempt < 2:
                self.sleep(0.2 * (attempt + 1))
        raise ChannelGrowthError(f"channel_growth_listing_{operation}_failed")

    def _live_listing(self, target: GrowthTarget) -> Mapping[str, Any]:
        client = self.a6_client(target.a6_user_id)
        page = client.supplier_listings(target.a6_user_id, page=1, size=1000)
        for item in page.get("items") or []:
            if isinstance(item, Mapping) and int(item.get("id") or 0) == target.listing_id:
                return dict(item)
        raise ChannelGrowthError("channel_growth_listing_readback_missing")

    def _listing_status(self, target: GrowthTarget) -> int | None:
        try:
            return int(self._live_listing(target).get("status"))
        except Exception:
            return None

    def _best_effort_reactivate(self, target: GrowthTarget) -> bool:
        try:
            if self._listing_status(target) == 2:
                return True
            merchant = ManagedMerchant(
                target.merchant_id,
                target.a6_user_id,
                target.supplier_id,
                target.roles,
            )
            self._listing_operation(merchant, target.managed_uid, operation="resubmit")
            return self._listing_status(target) == 2
        except Exception:
            return False

    def _record(self, user_id: int) -> Any:
        record = self._records.get(user_id)
        if record is None:
            raise ChannelGrowthError("channel_growth_session_missing")
        return record


def callback_cost_micros(usage: CallbackUsage, target: GrowthTarget) -> int:
    uncached = max(0, usage.input_tokens - usage.cached_tokens - usage.cache_creation_tokens)
    numerator = (
        uncached * target.input_price_micros
        + usage.cached_tokens * target.cache_read_price_micros
        + usage.cache_creation_tokens * target.cache_write_price_micros
        + usage.output_tokens * target.output_price_micros
    )
    return int((Decimal(numerator) / Decimal(1_000_000)).to_integral_value(rounding=ROUND_CEILING))


def _seller_readiness_url(record: Any) -> str:
    configured = str(getattr(record, "upstream_base_url", "") or "").strip()
    return configured or _DEFAULT_SELLER_READINESS_URL


def _load_usage_samples() -> Sequence[Mapping[str, Any]]:
    path = Path(__file__).with_name("data") / "callback_usage_samples.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raise ChannelGrowthError("callback_usage_samples_invalid") from None
    if not isinstance(value, list):
        raise ChannelGrowthError("callback_usage_samples_invalid")
    return value


def _callback_usage(value: Any) -> CallbackUsage | None:
    if not isinstance(value, Mapping):
        return None
    details = value.get("input_tokens_details")
    if not isinstance(details, Mapping):
        details = value
    try:
        usage = CallbackUsage(
            input_tokens=int(value.get("input_tokens", value.get("prompt_tokens"))),
            output_tokens=int(value.get("output_tokens", value.get("completion_tokens"))),
            cached_tokens=int(details.get("cached_tokens") or 0),
            cache_creation_tokens=int(details.get("cache_creation_tokens") or 0),
        )
    except (TypeError, ValueError):
        return None
    if (
        min(
            usage.input_tokens,
            usage.output_tokens,
            usage.cached_tokens,
            usage.cache_creation_tokens,
        )
        < 0
    ):
        return None
    if usage.cached_tokens + usage.cache_creation_tokens > usage.input_tokens:
        return None
    return usage


def _response_text(document: Mapping[str, Any]) -> str:
    if isinstance(document.get("output_text"), str):
        return str(document["output_text"])
    parts: list[str] = []
    for item in document.get("output") or []:
        if not isinstance(item, Mapping):
            continue
        for block in item.get("content") or []:
            if (
                isinstance(block, Mapping)
                and block.get("type") in {"output_text", "text"}
                and isinstance(block.get("text"), str)
            ):
                parts.append(str(block["text"]))
    return "".join(parts)


def _matching_pin(
    pins: Sequence[Mapping[str, Any]],
    *,
    token_id: int,
    model: str,
) -> Mapping[str, Any] | None:
    matches = [
        pin
        for pin in pins
        if int(pin.get("token_id") or 0) == token_id and pin.get("model_name") == model
    ]
    if len(matches) > 1:
        raise ChannelGrowthError("channel_growth_pin_duplicate")
    return dict(matches[0]) if matches else None


def _confirm_pin(
    client: A6ApiClient,
    booster: GrowthBooster,
    model: str,
    channel_id: int,
) -> None:
    observed = _matching_pin(
        client.marketplace_pins(booster.a6_user_id),
        token_id=booster.token_id,
        model=model,
    )
    if (
        observed is None
        or int(observed.get("channel_id") or 0) != channel_id
        or str(observed.get("status") or "") != "active"
    ):
        raise ChannelGrowthError("channel_growth_pin_readback_failed")


def _restore_pin(
    client: A6ApiClient,
    booster: GrowthBooster,
    *,
    model: str,
    original_pin: Mapping[str, Any] | None,
    sleep: Callable[[float], None],
) -> None:
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            if original_pin is None:
                client.marketplace_unpin(
                    booster.a6_user_id,
                    token_id=booster.token_id,
                    model=model,
                )
                if (
                    _matching_pin(
                        client.marketplace_pins(booster.a6_user_id),
                        token_id=booster.token_id,
                        model=model,
                    )
                    is not None
                ):
                    raise ChannelGrowthError("channel_growth_unpin_readback_failed")
            else:
                channel_id = int(original_pin.get("channel_id") or 0)
                client.marketplace_pin(
                    booster.a6_user_id,
                    token_id=booster.token_id,
                    channel_id=channel_id,
                    model=model,
                    fallback_to_smart_routing=bool(
                        original_pin.get("fallback_to_smart_routing", False)
                    ),
                )
                _confirm_pin(client, booster, model, channel_id)
            return
        except Exception as exc:
            last_error = exc
            if attempt < 2:
                sleep(attempt + 1)
    raise ChannelGrowthError("channel_growth_pin_restore_failed") from last_error


def _target_prices(target: GrowthTarget) -> tuple[int, int, int, int]:
    return (
        target.input_price_micros,
        target.output_price_micros,
        target.cache_read_price_micros,
        target.cache_write_price_micros,
    )


def _listing_prices(value: Mapping[str, Any]) -> tuple[int, int, int, int]:
    return tuple(
        int(value.get(field) or 0)
        for field in (
            "input_price_micros",
            "output_price_micros",
            "cache_read_price_micros",
            "cache_write_price_micros",
        )
    )


def _action_key(target: GrowthTarget, action_type: str, cooldown_seconds: int) -> str:
    bucket = int(time.time()) // max(1, cooldown_seconds)
    return f"channel-growth:{target.listing_binding_id}:{action_type}:{bucket}"


def _listing_recovery_pending(target: GrowthTarget) -> bool:
    if target.last_recycle_at is None:
        return False
    completed = tuple(
        value for value in (target.last_callback_at, target.last_readiness_at) if value is not None
    )
    return not completed or max(completed) < target.last_recycle_at


def _exception_code(exc: Exception, fallback: str) -> str:
    value = str(getattr(exc, "code", "") or str(exc))
    return _safe_error_code(value) if _ERROR_CODE.fullmatch(value) else fallback


def _safe_error_code(value: str) -> str:
    normalized = str(value).strip().lower()
    return normalized if _ERROR_CODE.fullmatch(normalized) else "channel_growth_action_failed"


def _elapsed_ms(started: float) -> int:
    return max(0, round((time.monotonic() - started) * 1000))


def _nearest_rank(values: Sequence[int], percentile: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(percentile * len(ordered)) - 1)]


def _status_counts(results: Sequence[CallbackRequestResult]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for result in results:
        key = str(result.status)
        counts[key] = counts.get(key, 0) + 1
    return counts


def _within(value: datetime | None, now: datetime, seconds: int) -> bool:
    return value is not None and (now - value).total_seconds() < seconds


def _before(value: datetime | None, now: datetime) -> bool:
    return value is not None and now < value


def _recovery_deadline(target: GrowthTarget) -> datetime | None:
    candidates = [
        value
        for value in (
            target.circuit_cooling_until,
            target.cooldown_expected_recovery_at,
        )
        if value is not None
    ]
    if target.cooldown_remaining_seconds is not None and target.cooldown_remaining_seconds > 0:
        candidates.append(
            target.signal_observed_at + timedelta(seconds=max(0, target.cooldown_remaining_seconds))
        )
    if not candidates:
        return None
    return max(candidates) + timedelta(seconds=_RECOVERY_WINDOW_GRACE_SECONDS)


def _uses_probe_recovery(target: GrowthTarget) -> bool:
    return target.circuit_recovery_mode.strip().lower() == "probe"


def _probe_not_before(target: GrowthTarget) -> datetime | None:
    candidates = [
        value
        for value in (
            target.circuit_next_probe_at,
            target.cooldown_next_probe_at,
        )
        if value is not None
    ]
    if candidates:
        return max(candidates) + timedelta(seconds=_PROBE_WINDOW_GRACE_SECONDS)
    return _recovery_deadline(target)


def _callback_next_allowed_seconds(
    target: GrowthTarget,
    *,
    error: str | None,
    now: datetime,
    action_type: str,
) -> int:
    if action_type == "callback_readiness":
        delay = (
            _CALLBACK_ACTION_CLAIM_WINDOW_SECONDS
            if error is None
            else min(60, target.callback_cooldown_seconds)
        )
        if error in _A6_READINESS_RETRY_ERRORS:
            deadline = _probe_not_before(target)
            if deadline is not None and deadline > now:
                delay = max(delay, math.ceil((deadline - now).total_seconds()))
        return delay
    delay = target.callback_cooldown_seconds
    if error not in _A6_READINESS_RETRY_ERRORS:
        return delay
    deadline = (
        _probe_not_before(target)
        if action_type == "callback_readiness"
        else _recovery_deadline(target)
    )
    if deadline is not None and deadline > now:
        delay = max(delay, math.ceil((deadline - now).total_seconds()))
    return delay


def _isoformat(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _epoch(value: datetime | None) -> int | None:
    return round(value.timestamp()) if value is not None else None


def _run_summary(
    targets: Sequence[GrowthTarget],
    *,
    mode: str,
    target: GrowthTarget,
    health_state: str,
    action_type: str,
    held: Mapping[str, int],
    success: bool = True,
    action_id: int | None = None,
    error_class: str | None = None,
    details: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "success": success,
        "mode": mode,
        "model": target.model,
        "target_count": len(targets),
        "merchant_id": target.merchant_id,
        "channel_id": target.channel_id,
        "listing_id": target.listing_id,
        "health_state": health_state,
        "action_type": action_type,
        "action_id": action_id,
        "error_class": error_class,
        "held": dict(held),
        "details": dict(details or {}),
        "maximum_actions_per_run": 1,
    }


def _increment_held(held: Mapping[str, int], reason: str) -> dict[str, int]:
    updated = dict(held)
    updated[reason] = updated.get(reason, 0) + 1
    return updated


def _row(cursor: Any) -> dict[str, Any] | None:
    value = cursor.fetchone()
    if value is None:
        return None
    if isinstance(value, Mapping):
        return dict(value)
    names = [str(column.name) for column in cursor.description]
    return dict(zip(names, value, strict=True))


def _rows(cursor: Any) -> list[dict[str, Any]]:
    values = cursor.fetchall()
    if not values:
        return []
    if isinstance(values[0], Mapping):
        return [dict(value) for value in values]
    names = [str(column.name) for column in cursor.description]
    return [dict(zip(names, value, strict=True)) for value in values]


def _target_from_row(row: Mapping[str, Any]) -> GrowthTarget:
    roles = tuple(str(value) for value in row.get("roles") or ())
    labels = tuple(str(value) for value in row.get("smart_routing_labels") or ())
    return GrowthTarget(
        listing_binding_id=int(row["listing_binding_id"]),
        managed_uid=str(row["managed_uid"]),
        merchant_id=int(row["merchant_id"]),
        a6_user_id=int(row["a6_user_id"]),
        supplier_id=int(row["supplier_id"]) if row.get("supplier_id") else None,
        roles=roles,
        session_ref=str(row["session_ref"]),
        enodot_user_id=int(row["enodot_user_id"]),
        enodot_managed_uid=str(row["enodot_managed_uid"]),
        enodot_key_secret_ref=str(row["enodot_key_secret_ref"]),
        model=str(row["model"]),
        channel_id=int(row["channel_id"]),
        listing_id=int(row["listing_id"]),
        input_price_micros=int(row["input_price_micros"]),
        output_price_micros=int(row["output_price_micros"]),
        cache_read_price_micros=int(row["cache_read_price_micros"]),
        cache_write_price_micros=int(row["cache_write_price_micros"]),
        listing_status=(
            int(row["listing_status"])
            if row.get("listing_status") is not None
            else None
        ),
        available=bool(row["available"]),
        route_status=str(row.get("route_status") or ""),
        circuit_state=str(row.get("circuit_state") or ""),
        circuit_probe_status=str(row.get("circuit_probe_status") or ""),
        circuit_last_error_kind=str(row.get("circuit_last_error_kind") or ""),
        circuit_probe_success_required=(
            int(row["circuit_probe_success_required"])
            if row.get("circuit_probe_success_required") is not None
            else None
        ),
        circuit_probe_success_streak=(
            int(row["circuit_probe_success_streak"])
            if row.get("circuit_probe_success_streak") is not None
            else None
        ),
        circuit_scope=str(row.get("circuit_scope") or ""),
        circuit_recovery_mode=str(row.get("circuit_recovery_mode") or ""),
        circuit_cooling_until=row.get("circuit_cooling_until"),
        circuit_last_cooling_at=row.get("circuit_last_cooling_at"),
        circuit_last_probe_at=row.get("circuit_last_probe_at"),
        circuit_next_probe_at=row.get("circuit_next_probe_at"),
        cooldown_state=str(row.get("cooldown_state") or ""),
        cooldown_remaining_seconds=(
            int(row["cooldown_remaining_seconds"])
            if row.get("cooldown_remaining_seconds") is not None
            else None
        ),
        cooldown_expected_recovery_at=row.get("cooldown_expected_recovery_at"),
        cooldown_next_probe_at=row.get("cooldown_next_probe_at"),
        smart_routing_status=str(row.get("smart_routing_status") or ""),
        smart_routing_labels=labels,
        last_success_at=row.get("last_success_at"),
        last_failure_at=row.get("last_failure_at"),
        last_failure_code=str(row.get("last_failure_code") or ""),
        sample_count=int(row["sample_count"]) if row.get("sample_count") is not None else None,
        recent_success_rate=(
            int(row["recent_success_rate"]) if row.get("recent_success_rate") is not None else None
        ),
        recent_p50_ms=(
            Decimal(str(row["recent_p50_ms"])) if row.get("recent_p50_ms") is not None else None
        ),
        p50_ttft_ms=(
            Decimal(str(row["p50_ttft_ms"])) if row.get("p50_ttft_ms") is not None else None
        ),
        signal_observed_at=row["signal_observed_at"],
        health_observer_count=int(row["health_observer_count"]),
        health_confirmation_count=int(row["health_confirmation_count"]),
        recycle_enabled=bool(row["recycle_enabled"]),
        callback_enabled=bool(row["callback_enabled"]),
        action_cooldown_seconds=int(row["action_cooldown_seconds"]),
        callback_cooldown_seconds=int(row["callback_cooldown_seconds"]),
        callback_requests=int(row["callback_requests"]),
        callback_concurrency=int(row["callback_concurrency"]),
        daily_budget_micros=int(row["daily_budget_micros"]),
        daily_spent_micros=int(row["daily_spent_micros"]),
        daily_readiness_budget_micros=int(row["daily_readiness_budget_micros"]),
        daily_readiness_spent_micros=int(row["daily_readiness_spent_micros"]),
        working_balance_low_usd=Decimal(str(row["working_balance_low_usd"])),
        working_balance_target_usd=Decimal(str(row["working_balance_target_usd"])),
        degraded_success_rate=int(row["degraded_success_rate"]),
        idle_seconds=int(row["idle_seconds"]),
        minimum_health_observers=int(row["minimum_health_observers"]),
        maximum_consecutive_failures=int(row["maximum_consecutive_failures"]),
        consecutive_failures=int(row["consecutive_failures"]),
        last_recycle_at=row.get("last_recycle_at"),
        recycle_next_allowed_at=row.get("recycle_next_allowed_at"),
        last_callback_at=row.get("last_callback_at"),
        callback_next_allowed_at=row.get("callback_next_allowed_at"),
        last_readiness_at=row.get("last_readiness_at"),
        readiness_next_allowed_at=row.get("readiness_next_allowed_at"),
    )


def _booster_from_row(row: Mapping[str, Any]) -> GrowthBooster:
    return GrowthBooster(
        token_ref_id=int(row["token_ref_id"]),
        merchant_id=int(row["merchant_id"]),
        a6_user_id=int(row["a6_user_id"]),
        token_id=int(row["token_id"]),
        token_name=str(row["token_name"]),
        session_ref=str(row["session_ref"]),
        daily_budget_micros=int(row["daily_budget_micros"]),
        daily_spent_micros=int(row["daily_spent_micros"]),
        daily_readiness_budget_micros=int(row["daily_readiness_budget_micros"]),
        daily_readiness_spent_micros=int(row["daily_readiness_spent_micros"]),
        maximum_burst_requests=int(row["maximum_burst_requests"]),
    )


_TARGETS_QUERY = """
SELECT
    listing.id AS listing_binding_id,
    listing.managed_uid,
    merchant.id AS merchant_id,
    merchant.a6_user_id,
    merchant.supplier_id,
    merchant.roles,
    merchant.session_ref,
    seller.enodot_user_id,
    seller.managed_uid AS enodot_managed_uid,
    seller.key_secret_ref AS enodot_key_secret_ref,
    inventory.platform_model AS model,
    listing.a6_channel_id AS channel_id,
    listing.a6_listing_id AS listing_id,
    inventory.input_price_micros,
    inventory.output_price_micros,
    inventory.cache_read_price_micros,
    inventory.cache_write_price_micros,
    inventory.status AS listing_status,
    signal.available,
    signal.route_status,
    signal.circuit_state,
    signal.circuit_probe_status,
    signal.circuit_last_error_kind,
    signal.circuit_probe_success_required,
    signal.circuit_probe_success_streak,
    signal.circuit_scope,
    signal.circuit_recovery_mode,
    signal.circuit_cooling_until,
    signal.circuit_last_cooling_at,
    signal.circuit_last_probe_at,
    signal.circuit_next_probe_at,
    signal.cooldown_state,
    signal.cooldown_remaining_seconds,
    signal.cooldown_expected_recovery_at,
    signal.cooldown_next_probe_at,
    signal.smart_routing_status,
    signal.smart_routing_labels,
    signal.last_success_at,
    signal.last_failure_at,
    signal.last_failure_code,
    signal.sample_count,
    signal.recent_success_rate,
    signal.recent_p50_ms,
    signal.p50_ttft_ms,
    signal.last_observed_at AS signal_observed_at,
    coalesce(health_quorum.observer_count, 0)::integer AS health_observer_count,
    coalesce(health_quorum.confirmation_count, 0)::integer AS health_confirmation_count,
    policy.recycle_enabled,
    policy.callback_enabled,
    policy.action_cooldown_seconds,
    policy.callback_cooldown_seconds,
    policy.callback_requests,
    policy.callback_concurrency,
    policy.daily_budget_micros,
    policy.daily_readiness_budget_micros,
    coalesce(today.daily_spent_micros, 0)::bigint AS daily_spent_micros,
    coalesce(today.daily_readiness_spent_micros, 0)::bigint
        AS daily_readiness_spent_micros,
    policy.working_balance_low_usd,
    policy.working_balance_target_usd,
    policy.degraded_success_rate,
    policy.idle_seconds,
    policy.minimum_health_observers,
    policy.maximum_consecutive_failures,
    coalesce(failures.consecutive_failures, 0)::integer AS consecutive_failures,
    latest.last_recycle_at,
    latest.recycle_next_allowed_at,
    latest.last_callback_at,
    latest.callback_next_allowed_at,
    latest.last_readiness_at,
    latest.readiness_next_allowed_at
FROM a6_trade.a6_channel_growth_policies policy
JOIN a6_trade.a6_managed_listing_bindings listing
  ON listing.id = policy.listing_binding_id
JOIN a6_trade.a6_merchants merchant
  ON merchant.id = listing.merchant_id
JOIN a6_trade.enodot_managed_seller_resources seller
  ON seller.merchant_id = merchant.id
JOIN a6_trade.a6_supplier_listings_current inventory
  ON inventory.merchant_id = listing.merchant_id
 AND inventory.a6_listing_id = listing.a6_listing_id
JOIN a6_trade.a6_market_channel_signals_current signal
  ON signal.model = inventory.platform_model
 AND signal.channel_id = listing.a6_channel_id
LEFT JOIN LATERAL (
    SELECT
        count(*)::integer AS observer_count,
        count(*) FILTER (
            WHERE observer_health.health_state = signal.health_state
        )::integer AS confirmation_count
    FROM a6_trade.a6_channel_observer_health_current observer_health
    WHERE observer_health.model = signal.model
      AND observer_health.channel_id = signal.channel_id
      AND observer_health.observed_at >= now() - (%s * interval '1 second')
) health_quorum ON true
LEFT JOIN LATERAL (
    SELECT
        max(started_at) FILTER (
            WHERE action_type = 'listing_recycle' AND state = 'completed'
        ) AS last_recycle_at,
        max(next_allowed_at) FILTER (
            WHERE action_type = 'listing_recycle'
        ) AS recycle_next_allowed_at,
        max(started_at) FILTER (
            WHERE action_type = 'callback_burst' AND state = 'completed'
        ) AS last_callback_at,
        max(next_allowed_at) FILTER (
            WHERE action_type = 'callback_burst'
        ) AS callback_next_allowed_at,
        max(started_at) FILTER (
            WHERE action_type = 'callback_readiness' AND state = 'completed'
        ) AS last_readiness_at,
        max(next_allowed_at) FILTER (
            WHERE action_type = 'callback_readiness'
        ) AS readiness_next_allowed_at
    FROM a6_trade.a6_channel_growth_actions action
    WHERE action.listing_binding_id = listing.id
) latest ON true
LEFT JOIN LATERAL (
    SELECT
        coalesce(sum(
            CASE WHEN action.state = 'running'
                 THEN action.estimated_cost_micros ELSE action.actual_cost_micros END
        ) FILTER (WHERE action.action_type = 'callback_burst'), 0)::bigint
            AS daily_spent_micros,
        coalesce(sum(
            CASE WHEN action.state = 'running'
                 THEN action.estimated_cost_micros ELSE action.actual_cost_micros END
        ) FILTER (WHERE action.action_type = 'callback_readiness'), 0)::bigint
            AS daily_readiness_spent_micros
    FROM a6_trade.a6_channel_growth_actions action
    WHERE action.listing_binding_id = listing.id
      AND action.started_at >= date_trunc('day', now())
      AND action.action_type IN ('callback_readiness', 'callback_burst')
) today ON true
LEFT JOIN LATERAL (
    SELECT count(*)::integer AS consecutive_failures
    FROM a6_trade.a6_channel_growth_actions action
    WHERE action.listing_binding_id = listing.id
      AND action.state = 'failed'
      AND action.started_at > coalesce(
          (
              SELECT max(completed.started_at)
              FROM a6_trade.a6_channel_growth_actions completed
              WHERE completed.listing_binding_id = listing.id
                AND completed.state = 'completed'
          ),
          '-infinity'::timestamptz
      )
) failures ON true
WHERE policy.enabled
  -- Time-bounded suppression, so a transient failure can never permanently remove a
  -- listing from health management the way `enabled=false` used to.
  AND (policy.suppressed_until IS NULL OR policy.suppressed_until <= now())
  -- Retired merchants stay in scope: they keep their listings and keep earning, so
  -- their channels still need cooldown recovery. Excluding them here was the third
  -- one-way gate that shrank the growth target set as retirement progressed.
  AND listing.control_state = 'verified'
  AND listing.a6_channel_id IS NOT NULL
  AND listing.a6_listing_id IS NOT NULL
  AND merchant.a6_user_id IS NOT NULL
  AND merchant.status = 'supplier_submitted'
  AND seller.control_state = 'bound'
  AND seller.enodot_user_id IS NOT NULL
  AND seller.key_secret_ref IS NOT NULL
  AND signal.is_owned
  AND (
      signal.last_observed_at >= now() - (%s * interval '1 second')
      OR (
          latest.last_recycle_at IS NOT NULL
          AND greatest(
              coalesce(latest.last_callback_at, '-infinity'::timestamptz),
              coalesce(latest.last_readiness_at, '-infinity'::timestamptz)
          ) < latest.last_recycle_at
      )
  )
  AND inventory.platform_model = %s
ORDER BY
    CASE signal.health_state
        WHEN 'cooling' THEN 1
        WHEN 'unavailable' THEN 2
        WHEN 'degraded' THEN 3
        WHEN 'unlabeled' THEN 4
        WHEN 'idle' THEN 5
        WHEN 'unknown' THEN 6
        ELSE 7
    END,
    CASE WHEN merchant.operating_group = %s THEN 0 ELSE 1 END,
    signal.last_observed_at DESC,
    listing.id
"""


_BOOSTERS_QUERY = """
SELECT
    booster.a6_token_ref_id AS token_ref_id,
    token.merchant_id,
    merchant.a6_user_id,
    token.a6_token_id AS token_id,
    token.token_name,
    merchant.session_ref,
    booster.daily_budget_micros,
    booster.daily_readiness_budget_micros,
    booster.maximum_burst_requests,
    coalesce(spend.daily_spent_micros, 0)::bigint AS daily_spent_micros,
    coalesce(spend.daily_readiness_spent_micros, 0)::bigint
        AS daily_readiness_spent_micros
FROM a6_trade.a6_channel_booster_tokens booster
JOIN a6_trade.a6_tokens token ON token.id = booster.a6_token_ref_id
JOIN a6_trade.a6_merchants merchant ON merchant.id = token.merchant_id
JOIN a6_trade.a6_financial_current financial
  ON financial.merchant_id = merchant.id
LEFT JOIN LATERAL (
    SELECT
        coalesce(sum(
            CASE WHEN action.state = 'running'
                 THEN action.estimated_cost_micros ELSE action.actual_cost_micros END
        ) FILTER (WHERE action.action_type = 'callback_burst'), 0)::bigint
            AS daily_spent_micros,
        coalesce(sum(
            CASE WHEN action.state = 'running'
                 THEN action.estimated_cost_micros ELSE action.actual_cost_micros END
        ) FILTER (WHERE action.action_type = 'callback_readiness'), 0)::bigint
            AS daily_readiness_spent_micros
    FROM a6_trade.a6_channel_growth_actions action
    WHERE action.booster_token_ref_id = token.id
      AND action.started_at >= date_trunc('day', now())
      AND action.action_type IN ('callback_readiness', 'callback_burst')
) spend ON true
WHERE booster.enabled
  AND token.merchant_id <> %s
  AND token.status IN ('1', 'active', 'enabled')
  AND NOT EXISTS (
      SELECT 1
      FROM a6_trade.enodot_account_bindings provider_binding
      WHERE provider_binding.a6_token_id = token.id
  )
  AND NOT EXISTS (
      SELECT 1
      FROM a6_trade.provider_pool_slots provider_slot
      JOIN a6_trade.provider_pool_generations provider_generation
        ON provider_generation.id = provider_slot.generation_id
      WHERE provider_slot.a6_token_id = token.id
        AND provider_slot.valid_to IS NULL
        AND provider_slot.slot_state <> 'VACANT'
        AND provider_generation.generation_state = 'active'
  )
  AND merchant.a6_user_id IS NOT NULL
  AND merchant.status = 'supplier_submitted'
  AND financial.user_balance_quota >= merchant.quota_per_unit
  AND financial.observed_at >= now() - interval '10 minutes'
ORDER BY booster.last_used_at NULLS FIRST, token.id
"""


_BOOSTER_CLAIM_LOCK_QUERY = """
SELECT token.id
FROM a6_trade.a6_tokens token
JOIN a6_trade.a6_channel_booster_tokens booster
  ON booster.a6_token_ref_id = token.id
WHERE token.id = %s
FOR UPDATE OF token, booster
"""


_BOOSTER_CLAIM_GUARD_QUERY = """
SELECT token.id
FROM a6_trade.a6_tokens token
JOIN a6_trade.a6_channel_booster_tokens booster
  ON booster.a6_token_ref_id = token.id
WHERE token.id = %s
  AND booster.enabled
  AND token.status IN ('1', 'active', 'enabled')
  AND NOT EXISTS (
      SELECT 1
      FROM a6_trade.enodot_account_bindings provider_binding
      WHERE provider_binding.a6_token_id = token.id
  )
  AND NOT EXISTS (
      SELECT 1
      FROM a6_trade.provider_pool_slots provider_slot
      JOIN a6_trade.provider_pool_generations provider_generation
        ON provider_generation.id = provider_slot.generation_id
      WHERE provider_slot.a6_token_id = token.id
        AND provider_slot.valid_to IS NULL
        AND provider_slot.slot_state <> 'VACANT'
        AND provider_generation.generation_state = 'active'
  )
"""
