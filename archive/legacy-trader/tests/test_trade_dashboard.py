from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Mapping, Sequence
from decimal import Decimal
from io import BytesIO
from pathlib import Path
from typing import Any

import pytest

from a6api_auto.storage import write_private_json
from a6api_auto.trade_dashboard import (
    WINDOWS,
    CredentialAccessError,
    _sanitize_economic_priority1,
    _sanitize_market_traffic,
    _sanitize_merchant,
    _sanitize_overview,
    create_dashboard_app,
)
from a6api_auto.trade_db import _session_fingerprint
from a6api_auto.trade_repository import _MERCHANT_QUERY

INTERNAL_TOKEN = "fixture-internal-token-32-characters"


class FakeRepository:
    def __init__(self) -> None:
        self.overview_calls: list[str] = []
        self.merchant_calls: list[str] = []
        self.market_traffic_calls: list[tuple[str, str]] = []
        self.credential_binding_calls: list[int] = []
        self.credential_bindings: dict[int, Mapping[str, Any]] = {}
        self.economic_priority1: Mapping[str, Any] | None = None
        self.economic_priority1_calls: list[str] = []
        self.fail_overview = False

    def health(self) -> Mapping[str, Any]:
        return {
            "status": "ok",
            "checked_at": "2026-07-24T05:10:00Z",
            "last_success_at": "2026-07-24T05:09:57Z",
            "admin_key": "ADMIN_KEY_MUST_NOT_LEAK",
        }

    def get_overview(self, window: str) -> Mapping[str, Any]:
        self.overview_calls.append(window)
        if self.fail_overview:
            raise RuntimeError("DATABASE_URL_WITH_SECRET")
        return {
            "generated_at": "2026-07-24T05:10:00Z",
            "merchant_count": 19,
            "session_cookie": "SESSION_MUST_NOT_LEAK",
            "stocks": {
                "user_balance": {
                    "start_micros": 10_000_000,
                    "current_micros": 12_500_000,
                    "change_micros": 2_500_000,
                    "min_micros": 9_000_000,
                    "max_micros": 13_000_000,
                    "password": "PASSWORD_MUST_NOT_LEAK",
                },
                "withdrawable": {
                    "start_micros": 4_000_000,
                    "current_micros": 4_500_000,
                    "change_micros": 500_000,
                    "min_micros": 3_500_000,
                    "max_micros": 4_500_000,
                },
                "withdrawing": {"current_micros": 750_000},
            },
            "flows": {
                "earned_micros": 1_500_000,
                "paid_micros": 500_000,
                "transfer_in_micros": 3_000_000,
                "transfer_in_count": 2,
                "enodot_sales_micros": 7_000_000,
                "enodot_sales_request_count": 700,
                "confirmed_enodot_sales_micros": 7_000_000,
                "confirmed_enodot_sales_request_count": 700,
                "unattributed_enodot_sales_micros": 0,
                "unattributed_enodot_sales_request_count": 0,
                "external_enodot_revenue_micros": 7_000_000,
                "external_enodot_request_count": 700,
                "confirmed_external_enodot_revenue_micros": 7_000_000,
                "confirmed_external_enodot_request_count": 700,
                "unattributed_external_enodot_revenue_micros": 0,
                "unattributed_external_enodot_request_count": 0,
                "managed_internal_debit_micros": 600_000,
                "managed_internal_request_count": 60,
                "external_provider_cost_micros": 3_500_000,
                "external_provider_event_count": 350,
                "managed_internal_provider_cost_micros": 500_000,
                "managed_internal_provider_event_count": 50,
                "owner_internal_debit_micros": 27_593,
                "owner_internal_request_count": 1,
                "owner_internal_provider_cost_micros": 398,
                "owner_internal_provider_event_count": 1,
                "enodot_provider_cost_micros": 600_000,
                "confirmed_enodot_provider_cost_micros": 500_000,
                "unattributed_enodot_provider_cost_micros": 100_000,
                "enodot_provider_event_count": 60,
                "unattributed_enodot_provider_event_count": 10,
                "provider_cost_micros": 4_500_000,
                "confirmed_provider_cost_micros": 4_000_000,
                "unattributed_provider_cost_micros": 500_000,
                "provider_event_count": 450,
                "unattributed_provider_event_count": 50,
                "seller_income_micros": 800_000,
                "probe_gross_cost_micros": 200_000,
                "probe_recovery_micros": 100_000,
                "probe_loss_micros": 100_000,
                "pin_probe_cost_micros": 20_000,
                "pin_probe_event_count": 2,
                "withdrawal_fee_micros": 50_000,
                "actual_profit_micros": 3_650_000,
                "honest_profit_micros": 3_650_000,
                "pending_binding_count": 1,
                "profit_margin": Decimal("0.521428571"),
                "profit_status": "partial",
                "sales_status": "confirmed",
                "api_key": "KEY_MUST_NOT_LEAK",
            },
            "freshness": {
                "status": "fresh",
                "last_observed_at": "2026-07-24T05:09:57Z",
                "age_seconds": 3,
                "fresh_count": 18,
                "stale_count": 1,
                "error_count": 0,
                "raw_error": "UPSTREAM_DETAIL_MUST_NOT_LEAK",
            },
        }

    def get_merchants(self, window: str) -> Sequence[Mapping[str, Any]]:
        self.merchant_calls.append(window)
        return [
            {
                "merchant_id": "merchant-01",
                "display_name": "seller-01",
                "email": "secret@example.com",
                "password": "PASSWORD_MUST_NOT_LEAK",
                "session": "SESSION_MUST_NOT_LEAK",
                "api_key": "KEY_MUST_NOT_LEAK",
                "roles": ["seller", "provider", "administrator"],
                "status": "cooling",
                "session_status": "healthy",
                "proxy_status": "degraded",
                "freshness": "fresh",
                "last_observed_at": "2026-07-24T05:09:57Z",
                "age_seconds": 3,
                "user_balance_micros": 2_000_000,
                "withdrawable_micros": 1_000_000,
                "withdrawing_micros": 100_000,
                "lifetime_earned_micros": 10_000_000,
                "paid_micros": 2_000_000,
                "today_earned_micros": 300_000,
                "month_earned_micros": 6_000_000,
                "earned_micros": 50_000,
                "transfer_in_micros": 500_000,
                "transfer_in_count": 1,
                "enodot_sales_micros": 900_000,
                "enodot_sales_request_count": 90,
                "confirmed_enodot_sales_micros": 900_000,
                "confirmed_enodot_sales_request_count": 90,
                "unattributed_enodot_sales_micros": 0,
                "unattributed_enodot_sales_request_count": 0,
                "external_enodot_revenue_micros": 900_000,
                "external_enodot_request_count": 90,
                "confirmed_external_enodot_revenue_micros": 900_000,
                "confirmed_external_enodot_request_count": 90,
                "unattributed_external_enodot_revenue_micros": 0,
                "unattributed_external_enodot_request_count": 0,
                "managed_internal_debit_micros": 60_000,
                "managed_internal_request_count": 6,
                "external_provider_cost_micros": 550_000,
                "external_provider_event_count": 55,
                "managed_internal_provider_cost_micros": 50_000,
                "managed_internal_provider_event_count": 5,
                "owner_internal_debit_micros": 27_593,
                "owner_internal_request_count": 1,
                "owner_internal_provider_cost_micros": 398,
                "owner_internal_provider_event_count": 1,
                "enodot_provider_cost_micros": 60_000,
                "confirmed_enodot_provider_cost_micros": 50_000,
                "unattributed_enodot_provider_cost_micros": 10_000,
                "enodot_provider_event_count": 6,
                "unattributed_enodot_provider_event_count": 1,
                "provider_cost_micros": 650_000,
                "confirmed_provider_cost_micros": 600_000,
                "unattributed_provider_cost_micros": 50_000,
                "provider_event_count": 65,
                "unattributed_provider_event_count": 5,
                "seller_income_micros": 40_000,
                "probe_gross_cost_micros": 10_000,
                "probe_recovery_micros": 5_000,
                "probe_loss_micros": 5_000,
                "pin_probe_cost_micros": 1_000,
                "pin_probe_event_count": 1,
                "withdrawal_fee_micros": 10_000,
                "actual_profit_micros": 325_000,
                "honest_profit_micros": 325_000,
                "profit_margin": Decimal("0.361111"),
                "withdraw_account_state": "bound",
                "withdraw_account_network": "ERC20",
                "withdraw_account_masked": "0x1234...5678",
                "profit_status": "partial",
                "sales_status": "confirmed",
                "pending_binding": False,
                "seller_channel_count": 2,
                "managed_channel_count": 1,
                "existing_unmanaged_channel_count": 1,
                "listing_total_count": 5,
                "active_listing_count": 3,
                "pending_listing_count": 1,
                "down_listing_count": 1,
                "managed_listing_count": 2,
                "managed_active_listing_count": 1,
                "existing_unmanaged_listing_count": 3,
                "existing_unmanaged_active_listing_count": 2,
                "listing_last_observed_at": "2026-07-24T05:09:56Z",
                "provider_token_count": 1,
                "enodot_account_count": 1,
                "error_rate": "0.025",
                "last_error_class": "quota_insufficient",
                "last_error_message": (
                    "Access forbidden (403): account balance is insufficient "
                    "request_id: SECRET_REQUEST_ID"
                ),
                "last_error_at": "2026-07-24T05:09:00Z",
                "cooldown_until": "2026-07-24T05:11:00Z",
                "current_pin_model": "gpt-5.6-sol",
                "current_pin_channel_id": 790,
                "pin_rates": [
                    {
                        "token_id": 14020,
                        "model": "gpt-5.6-sol",
                        "channel_id": 790,
                        "rate_multiplier": Decimal("0.118"),
                        "expected_cost_multiplier": Decimal("0.12"),
                        "market_final_multiplier": Decimal("0.118"),
                        "realtime_ratio_usd": Decimal("0.118"),
                        "realtime_ratio_cny": Decimal("0.7991196"),
                        "exchange_rate": Decimal("6.7722"),
                        "decision_status": "completed",
                        "confirmed_at": "2026-07-24T05:08:00Z",
                        "quote_observed_at": "2026-07-24T05:07:59Z",
                        "api_key": "NESTED_PIN_KEY_MUST_NOT_LEAK",
                    },
                    {
                        "token_id": 14021,
                        "model": "gpt-5.6-terra",
                        "channel_id": 791,
                        "rate_multiplier": Decimal("0.2"),
                        "confirmed_at": "2026-07-24T05:08:30Z",
                    },
                ],
                "listing_rates": [
                    {
                        "listing_id": 900,
                        "channel_id": 790,
                        "model": "gpt-5.6-sol",
                        "managed_state": "managed",
                        "control_state": "verified",
                        "desired_delta": Decimal("0.5"),
                        "observed_delta": Decimal("0.5"),
                        "price_multiplier": Decimal("0.117504"),
                        "market_price_multiplier": Decimal("0.1410048"),
                        "price_multiplier_cny": Decimal("0.95491270656"),
                        "exchange_rate": Decimal("6.7722"),
                        "input_price_micros": 587_520,
                        "output_price_micros": 3_525_120,
                        "cache_read_price_micros": 58_752,
                        "cache_write_price_micros": 734_400,
                        "status": 2,
                        "availability": 1,
                        "visibility": 1,
                        "health_state": "cooling",
                        "health_observer_count": 3,
                        "health_confirmation_count": 2,
                        "market_available": False,
                        "circuit_state": "cooling",
                        "circuit_probe_status": "idle",
                        "circuit_last_error_kind": "merchant_quota_insufficient",
                        "circuit_probe_success_required": 2,
                        "circuit_probe_success_streak": 1,
                        "circuit_scope": "listing",
                        "circuit_recovery_mode": "natural",
                        "circuit_cooling_until": "2026-07-24T05:12:00Z",
                        "circuit_last_cooling_at": "2026-07-24T05:02:00Z",
                        "circuit_last_probe_at": "2026-07-24T05:09:50Z",
                        "circuit_next_probe_at": "2026-07-24T05:10:50Z",
                        "cooldown_state": "cooling",
                        "cooldown_remaining_seconds": 124,
                        "cooldown_expected_recovery_at": "2026-07-24T05:12:00Z",
                        "cooldown_next_probe_at": "2026-07-24T05:10:50Z",
                        "cooldown_sources": [
                            "circuit",
                            {"source": "listing", "state": "cooling"},
                        ],
                        "observed_at": "2026-07-24T05:09:56Z",
                        "session": "NESTED_LISTING_SESSION_MUST_NOT_LEAK",
                    },
                    {
                        "listing_id": 901,
                        "channel_id": 790,
                        "model": "gpt-5.6-luna",
                        "managed_state": "existing_unmanaged",
                        "input_price_micros": 80_000,
                        "output_price_micros": 480_000,
                        "cache_read_price_micros": 8_000,
                        "cache_write_price_micros": 100_000,
                        "observed_at": "2026-07-24T05:09:56Z",
                    },
                ],
            },
            {
                "merchant_id": "merchant-02",
                "display_name": "private@example.com",
                "roles": ["observer"],
                "status": "active",
                "session_status": "healthy",
                "proxy_status": "healthy",
                "freshness": "stale",
            },
        ]

    def get_market_traffic(self, window: str, model: str) -> Mapping[str, Any]:
        self.market_traffic_calls.append((window, model))
        return {
            "generated_at": "2026-07-25T14:00:00Z",
            "api_key": "KEY_MUST_NOT_LEAK",
            "method": {
                "signal": "last_success_at_transition",
                "count_semantics": "lower_bound_active_seconds",
                "observer_window_seconds": 10,
                "bucket_seconds": 10,
                "current_signal_max_age_seconds": 30,
                "current_comparison_seconds": 60,
                "automatic_repricing": False,
            },
            "freshness": {
                "last_observed_at": "2026-07-25T13:59:58Z",
                "age_seconds": 2,
                "current_channel_count": 163,
                "available_channel_count": 159,
            },
            "observer_backoff": {
                "active": True,
                "consecutive_failed_rounds": 3,
                "remaining_seconds": 52,
                "retry_after": "2026-07-25T14:00:52Z",
                "last_error_class": "a6_transport_failed",
                "last_failure_at": "2026-07-25T14:00:00Z",
                "last_success_at": "2026-07-25T13:59:30Z",
            },
            "totals": {
                "event_lower_bound": 22,
                "quorum_event_count": 18,
                "active_channel_count": 9,
                "owned_event_count": 3,
                "unpriced_event_count": 1,
            },
            "bands": [
                {
                    "band_id": "k_cny_00070_00080",
                    "label": "K_CNY 0.07-0.08",
                    "minimum_k_cny": "0.07",
                    "maximum_k_cny": "0.08",
                    "current_channel_count": 14,
                    "event_lower_bound": 12,
                    "quorum_event_count": 10,
                    "owned_event_count": 2,
                    "active_channel_count": 5,
                    "just_now_rate_per_minute": 8,
                    "one_minute_ago_rate_per_minute": 4,
                    "trend": "up",
                    "trend_ratio": 1,
                    "share": 0.5,
                    "series": [
                        {
                            "bucket_start": "2026-07-25T13:59:50Z",
                            "event_lower_bound": 2,
                            "quorum_event_count": 1,
                            "secret": "SESSION_MUST_NOT_LEAK",
                        }
                    ],
                }
            ],
            "managed_positions": [
                {
                    "rank": 4,
                    "merchant_id": 4,
                    "channel_id": 768,
                    "listing_id": 2589,
                    "channel_name": "OmniAPI SOL Route",
                    "channel_nickname": "SOL",
                    "operational_state": "active",
                    "blocked_reason": "",
                    "merchant_status": "supplier_submitted",
                    "merchant_roles": ["seller", "observer"],
                    "binding_present": True,
                    "binding_control_state": "verified",
                    "listing_present": True,
                    "listing_channel_id": 768,
                    "listing_platform_model": "gpt-5.6-sol",
                    "listing_status": 2,
                    "listing_availability": 1,
                    "listing_visibility": 1,
                    "tier": "floor",
                    "price_zone": "low",
                    "delta": "0.384615",
                    "target_final_multiplier": "0.023",
                    "panel_multiplier": "0.020",
                    "current_k_cny": "0.14",
                    "market_available": True,
                    "health_state": "healthy",
                    "route_status": "default",
                    "recent_success_rate": 9875,
                    "recent_p50_ms": "812.5",
                    "p50_ttft_ms": "155.25",
                    "smart_routing_labels": ["fast", "stable"],
                    "health_observed_at": "2026-07-25T13:59:58Z",
                    "recent_failure_count": 3,
                    "recent_client_failure_count": 2,
                    "recent_provider_failure_count": 1,
                    "recent_platform_failure_count": 0,
                    "recent_unknown_failure_count": 0,
                    "last_failure_responsibility": "provider",
                    "top_failure_codes": [
                        {
                            "code": "invalid_prompt",
                            "responsibility": "client",
                            "count": 2,
                            "last_failure_at": "2026-07-25T13:59:57Z",
                        },
                        {
                            "code": "do_request_failed",
                            "responsibility": "provider",
                            "count": 1,
                            "last_failure_at": "2026-07-25T13:59:50Z",
                        },
                    ],
                    "seller_call_count": 142,
                    "seller_income_micros": 1_180_922,
                    "seller_income_per_call_micros": 8_316,
                    "seller_income_per_hour_micros": 236_184,
                    "managed_key_call_count": 0,
                    "managed_key_debit_micros": 0,
                    "delta_sample_status": "routing_unverified",
                    "estimated_contribution_micros": None,
                },
                {
                    "rank": 5,
                    "merchant_id": 5,
                    "channel_id": 769,
                    "listing_id": 2590,
                    "channel_name": "Paused Managed Listing",
                    "operational_state": "blocked",
                    "blocked_reason": "listing_status_inactive",
                    "merchant_status": "supplier_submitted",
                    "merchant_roles": ["seller"],
                    "binding_present": True,
                    "binding_control_state": "verified",
                    "listing_present": True,
                    "listing_channel_id": 769,
                    "listing_platform_model": "gpt-5.6-sol",
                    "listing_status": 0,
                    "listing_availability": 0,
                    "listing_visibility": 0,
                    "current_k_cny": "0.075",
                    "market_available": False,
                    "health_state": "unknown",
                    "seller_call_count": 0,
                    "seller_income_micros": 0,
                    "seller_income_per_hour_micros": 0,
                    "managed_key_call_count": 0,
                    "managed_key_debit_micros": 0,
                    "delta_sample_status": "no_sample",
                    "estimated_contribution_micros": None,
                },
            ],
            "channel_failures": [
                {
                    "channel_id": 790,
                    "listing_id": 1790,
                    "supplier_id": 42,
                    "market_rank": 7,
                    "health_state": "healthy",
                    "available": True,
                    "recent_success_rate": 7300,
                    "last_failure_at": "2026-07-25T13:59:57Z",
                    "last_failure_code": "invalid_prompt",
                    "last_failure_responsibility": "client",
                    "recent_failure_count": 4,
                    "recent_client_failure_count": 4,
                    "recent_provider_failure_count": 0,
                    "recent_platform_failure_count": 0,
                    "recent_unknown_failure_count": 0,
                    "top_failure_codes": [
                        {
                            "code": "invalid_prompt",
                            "responsibility": "client",
                            "count": 4,
                            "last_failure_at": "2026-07-25T13:59:57Z",
                        }
                    ],
                }
            ],
        }

    def get_economic_priority1(self, model: str) -> Mapping[str, Any] | None:
        self.economic_priority1_calls.append(model)
        return self.economic_priority1

    def get_merchant_session_binding(self, merchant_id: int) -> Mapping[str, Any] | None:
        self.credential_binding_calls.append(merchant_id)
        return self.credential_bindings.get(merchant_id)


class FakeManager:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []

    def add_account(self, username: object, password: object) -> Mapping[str, Any]:
        self.calls.append(("add", username, password))
        return {
            "merchant_id": "23",
            "a6_user_id": "1023",
            "status": "imported",
            "collection": "scheduled",
        }

    def get_inventory(self, merchant_id: int) -> Mapping[str, Any]:
        self.calls.append(("inventory", merchant_id))
        return {
            "merchant_id": str(merchant_id),
            "supplier_state": "active",
            "supplier": {
                "id": "308",
                "name": "OmniAPI",
                "description": "多模型 API 中转服务",
                "contact_info": "contact@example.invalid",
                "api_key": "INVENTORY_SECRET_MUST_NOT_LEAK",
            },
            "channels": [
                {
                    "id": "1148",
                    "binding_id": "9148",
                    "name": "SOL回程",
                    "nickname": "SOL回程",
                    "description": "稳定可用",
                    "models": ["gpt-5.6-sol", "INVALID MODEL"],
                    "status": "1",
                    "availability": "1",
                    "base_url": "UPSTREAM_MUST_NOT_LEAK",
                    "listings": [
                        {
                            "id": "4100",
                            "channel_id": "1148",
                            "own_model": "gpt-5.6-sol",
                            "platform_model": "gpt-5.6-sol",
                            "display_name": "gpt-5.6-sol",
                            "description": "Team满血池",
                            "status": "2",
                            "availability": "1",
                            "visibility": "1",
                            "token": "LISTING_SECRET_MUST_NOT_LEAK",
                        }
                    ],
                }
            ],
        }

    def update_channel(
        self, merchant_id: int, channel_id: int, patch: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        self.calls.append(("channel", merchant_id, channel_id, dict(patch)))
        return {"merchant_id": str(merchant_id), "channel": {"id": str(channel_id)}}

    def update_listing(
        self, merchant_id: int, listing_id: int, patch: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        self.calls.append(("listing", merchant_id, listing_id, dict(patch)))
        return {"merchant_id": str(merchant_id), "listing": {"id": str(listing_id)}}

    def request_retirement(self, merchant_id: int) -> Mapping[str, Any]:
        self.calls.append(("retirement", merchant_id))
        return {
            "merchant_id": str(merchant_id),
            "phase": "pre_retirement",
            "state": "earning",
        }

    def update_model_profit(
        self, model: str, profit_percent: object
    ) -> Mapping[str, Any]:
        self.calls.append(("profit", model, profit_percent))
        return {
            "model": model,
            "profit_percent": str(profit_percent),
            "minimum_profit_on_cost_bps": 6000,
            "net_multiplier": "1.6",
            "gross_listing_multiplier": "1.92",
        }


def request(
    app: Any,
    path: str,
    *,
    query: str = "",
    method: str = "GET",
    json_body: Mapping[str, Any] | None = None,
    action_header: bool = False,
    bearer_token: str | None = None,
) -> tuple[str, dict[str, str], bytes]:
    captured: dict[str, Any] = {}

    def start_response(status: str, headers: list[tuple[str, str]]) -> None:
        captured["status"] = status
        captured["headers"] = dict(headers)

    raw_body = (
        json.dumps(dict(json_body), ensure_ascii=True).encode("utf-8")
        if json_body is not None
        else b""
    )
    body = b"".join(
        app(
            {
                "REQUEST_METHOD": method,
                "PATH_INFO": path,
                "QUERY_STRING": query,
                "SERVER_NAME": "localhost",
                "SERVER_PORT": "80",
                "wsgi.url_scheme": "http",
                "CONTENT_TYPE": "application/json" if json_body is not None else "",
                "CONTENT_LENGTH": str(len(raw_body)),
                "wsgi.input": BytesIO(raw_body),
                "HTTP_X_REQUESTED_WITH": ("a6-trader-dashboard" if action_header else ""),
                "HTTP_AUTHORIZATION": (
                    f"Bearer {bearer_token}" if bearer_token is not None else ""
                ),
            },
            start_response,
        )
    )
    return captured["status"], captured["headers"], body


def economic_priority1_fixture() -> dict[str, Any]:
    return {
        "model": "gpt-5.6-sol",
        "generated_at": "2026-08-03T08:00:00Z",
        "profit_setting": {
            "minimum_profit_on_cost_bps": 6000,
            "updated_at": "2026-08-04T05:00:00Z",
            "updated_by": "dashboard",
        },
        "generation": {
            "generation": 7,
            "parent_generation": 6,
            "state": "active",
            "plan_only": True,
            "input_hash": "a" * 64,
            "output_hash": "b" * 64,
            "minimum_profit_on_cost_bps": 6000,
            "quota_snapshot_max_age_seconds": 60,
            "channel_revenue_max_age_seconds": 30,
            "health_max_age_seconds": 30,
            "config_version": "priority1-economic-v1",
            "evaluated_at": "2026-08-03T07:59:59Z",
            "activated_at": "2026-08-03T07:59:59Z",
        },
        "projection": {
            "generation": 7,
            "digest": "c" * 64,
            "state": "plan_only",
            "projected_at": None,
            "updated_at": "2026-08-03T08:00:00Z",
            "redis_key": "SECRET_REDIS_KEY_MUST_NOT_LEAK",
        },
        "eligibility_states": {"ELIGIBLE": 1, "UNPROFITABLE": 1},
        "sources": [
            {
                "source_id": 11,
                "source_key": "owned-weekly-11",
                "source_kind": "account",
                "origin_class": "owned-account",
                "account_id": 501,
                "enabled": True,
                "health_state": "HEALTHY",
                "health_observed_at": "2026-08-03T07:59:58Z",
                "capacity_state": "AVAILABLE",
                "capacity_observed_at": "2026-08-03T07:59:58Z",
                "capacity_epoch": "quota-week-31",
                "configured_reserve_a": "2",
                "config_version": "source-v1",
                "endpoint_ref": "UPSTREAM_SECRET_MUST_NOT_LEAK",
                "contract": {
                    "contract_id": 21,
                    "cost_mode": "weekly_amortized",
                    "cycle_cost_cny": "120",
                    "declared_k_cny": None,
                    "currency": "CNY",
                    "cost_basis": "weekly",
                    "quota_basis": "full_cycle",
                    "effective_at": "2026-08-01T00:00:00Z",
                    "expires_at": "2026-08-08T00:00:00Z",
                    "config_version": "contract-v1",
                    "source_hash": "d" * 64,
                    "state": "active",
                    "contract_payload": {"api_key": "KEY_MUST_NOT_LEAK"},
                },
                "quota": {
                    "snapshot_id": 31,
                    "quota_epoch": "week-31",
                    "used_a": "18",
                    "remaining_a": "82",
                    "total_a": "100",
                    "reset_at": "2026-08-08T00:00:00Z",
                    "observed_at": "2026-08-03T07:59:58Z",
                    "payload_hash": "e" * 64,
                    "state": "VALID",
                    "estimate_state": "PROVISIONAL",
                    "estimate_method": "default_100A_until_delta_slope_converges",
                    "evidence_point_count": 1,
                    "distinct_level_count": 1,
                    "slope_count": 0,
                    "relative_mad": None,
                    "raw_utilization_percent": "18",
                    "raw_window_cost_a": "21.6",
                    "upstream_updated_at": "2026-08-03T07:59:57Z",
                    "source_payload": {"token": "TOKEN_MUST_NOT_LEAK"},
                },
                "source_k_cny": "1.2",
                "minimum_channel_k_cny": "1.92",
                "evaluated_channel_count": 2,
                "eligible_channel_count": 1,
                "evaluated_channel_ids": [1841, 1842],
                "eligible_channel_ids": [1841],
                "required_burn_a_per_second": "0.00025",
                "eligibility_states": {"ELIGIBLE": 1, "UNPROFITABLE": 1},
                "attempts": {
                    "attempt_count": 3,
                    "finished_attempt_count": 3,
                    "negative_profit_attempt_count": 1,
                    "realized_a": "2.5",
                    "realized_cost_cny": "3.0",
                    "realized_revenue_cny": "3.5",
                    "realized_profit_cny": "0.5",
                    "realized_a_1h": "1.0",
                    "realized_a_per_second_1h": "0.000277777778",
                    "last_finished_at": "2026-08-03T07:59:50Z",
                    "request_id": "SECRET_REQUEST_MUST_NOT_LEAK",
                },
                "runtime_usage": {
                    "request_count_1h": 7,
                    "consumed_a_1h": "3.25",
                    "group_ids": [3, 26],
                    "last_used_at": "2026-08-03T07:59:57Z",
                },
                "updated_at": "2026-08-03T07:59:58Z",
            }
        ],
        "channels": [
            {
                "channel_id": 1841,
                "channel_net_k_cny": "2.0",
                "minimum_source_k_cny": "1.2",
                "minimum_required_channel_k_cny": "1.92",
                "headroom_cny": "0.08",
                "headroom_bps": 666,
                "evaluated_source_count": 1,
                "eligible_source_count": 1,
                "eligibility_states": {"ELIGIBLE": 1},
                "eligible_sources": [
                    {
                        "source_id": 11,
                        "source_key": "owned-weekly-11",
                        "source_kind": "account",
                        "account_id": 501,
                        "state": "ELIGIBLE",
                        "source_k_cny": "1.2",
                        "minimum_channel_k_cny": "1.92",
                        "profit_on_cost_bps": 6666,
                        "required_burn_a_per_second": "0.00025",
                    }
                ],
            }
        ],
        "summary": {
            "source_count": 1,
            "returned_source_count": 1,
            "channel_count": 1,
            "returned_channel_count": 1,
            "eligible_channel_count": 1,
            "eligible_pair_count": 1,
            "attempt_count": 3,
            "negative_profit_attempt_count": 1,
            "realized_a": "2.5",
            "realized_cost_cny": "3.0",
            "realized_revenue_cny": "3.5",
            "realized_profit_cny": "0.5",
        },
    }


class FakePublicationControl:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []

    def report_attempt_failure(self, document: Mapping[str, Any]) -> Mapping[str, Any]:
        self.calls.append(("failure", dict(document)))
        return {"accepted": True, "publication_requested": True}

    def request_reconcile(self, model: str, document: Mapping[str, Any]) -> Mapping[str, Any]:
        self.calls.append(("reconcile", model, dict(document)))
        return {"id": 71, "model": model, "status": "pending"}

    def begin_quarantine_probe(self, model: str, document: Mapping[str, Any]) -> Mapping[str, Any]:
        self.calls.append(("quarantine_probe", model, dict(document)))
        return {"quarantine_id": document["quarantine_id"], "status": "probing"}

    def confirm_quarantine_recovery(
        self, model: str, document: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        self.calls.append(("quarantine_recover", model, dict(document)))
        return {"quarantine_id": document["quarantine_id"], "status": "verified"}

    def render_metrics(self) -> str:
        self.calls.append(("metrics",))
        return 'a6_model_publication_state{model="gpt-5.6-sol",state="published"} 1\n'


def test_internal_control_rejects_short_bearer_configuration() -> None:
    with pytest.raises(ValueError, match="internal_api_token_invalid"):
        create_dashboard_app(
            FakeRepository(),
            publication_control=FakePublicationControl(),
            internal_api_token="short-token",
        )


def test_economic_priority1_read_model_is_private_and_strictly_allowlisted() -> None:
    repository = FakeRepository()
    repository.economic_priority1 = economic_priority1_fixture()
    app = create_dashboard_app(repository)

    status, headers, body = request(
        app,
        "/internal/economic-priority1/gpt-5.6-sol",
    )

    payload = json.loads(body)
    assert status == "200 OK"
    assert headers["Cache-Control"] == "no-store, private"
    assert repository.economic_priority1_calls == ["gpt-5.6-sol"]
    assert payload["generation"]["generation"] == 7
    assert payload["generation"]["plan_only"] is True
    assert payload["profit_setting"]["profit_percent"] == "60"
    assert payload["profit_setting"]["gross_listing_multiplier"] == "1.92"
    assert payload["projection_matches_generation"] is True
    assert payload["sources"][0]["contract"]["cycle_cost_cny"] == "120"
    assert payload["sources"][0]["quota"]["total_a"] == "100"
    assert payload["sources"][0]["quota"]["estimate_state"] == "PROVISIONAL"
    assert payload["sources"][0]["quota"]["evidence_point_count"] == 1
    assert payload["sources"][0]["quota"]["raw_utilization_percent"] == "18"
    assert payload["sources"][0]["source_k_cny"] == "1.2"
    assert payload["sources"][0]["minimum_channel_k_cny"] == "1.92"
    assert payload["sources"][0]["minimum_listing_k_cny"] == "2.304000"
    assert payload["sources"][0]["eligible_channel_ids"] == [1841]
    assert payload["sources"][0]["runtime_usage"] == {
        "request_count_1h": 7,
        "consumed_a_1h": "3.25",
        "group_ids": [3, 26],
        "last_used_at": "2026-08-03T07:59:57Z",
    }
    assert payload["channels"][0]["headroom_cny"] == "0.08"
    assert payload["summary"]["negative_profit_attempt_count"] == 1
    for secret in (
        b"UPSTREAM_SECRET_MUST_NOT_LEAK",
        b"KEY_MUST_NOT_LEAK",
        b"TOKEN_MUST_NOT_LEAK",
        b"SECRET_REQUEST_MUST_NOT_LEAK",
        b"SECRET_REDIS_KEY_MUST_NOT_LEAK",
    ):
        assert secret not in body


def test_economic_priority1_sanitizer_marks_projection_generation_mismatch() -> None:
    raw = economic_priority1_fixture()
    raw["projection"] = {**raw["projection"], "generation": 6}

    payload = _sanitize_economic_priority1(raw, "gpt-5.6-sol")

    assert payload["projection_matches_generation"] is False


def test_inventory_is_loaded_on_demand_and_strictly_allowlisted() -> None:
    manager = FakeManager()
    app = create_dashboard_app(FakeRepository(), manager=manager)

    status, headers, body = request(app, "/api/merchants/1/inventory")

    payload = json.loads(body)
    assert status == "200 OK"
    assert headers["Cache-Control"] == "no-store, private"
    assert manager.calls == [("inventory", 1)]
    assert payload["supplier"]["name"] == "OmniAPI"
    assert payload["channels"][0]["models"] == ["gpt-5.6-sol"]
    assert payload["channels"][0]["listings"][0]["description"] == "Team满血池"
    assert b"INVENTORY_SECRET_MUST_NOT_LEAK" not in body
    assert b"UPSTREAM_MUST_NOT_LEAK" not in body
    assert b"LISTING_SECRET_MUST_NOT_LEAK" not in body


def test_dashboard_mutations_require_action_header_and_dispatch_allowlisted_routes() -> None:
    manager = FakeManager()
    app = create_dashboard_app(FakeRepository(), manager=manager)

    denied, _, denied_body = request(
        app,
        "/api/accounts",
        method="POST",
        json_body={"username": "fixture", "password": "secret"},
    )
    assert denied == "403 Forbidden"
    assert json.loads(denied_body) == {"error": {"code": "dashboard_action_header_required"}}

    status, headers, body = request(
        app,
        "/api/accounts",
        method="POST",
        json_body={"username": "fixture", "password": "secret"},
        action_header=True,
    )
    assert status == "201 Created"
    assert headers["Cache-Control"] == "no-store, private"
    assert json.loads(body)["merchant_id"] == "23"

    profile_status, _, profile_body = request(
        app,
        "/api/merchants/1/profile",
        method="PATCH",
        json_body={"name": "NexusAPI"},
        action_header=True,
    )
    assert profile_status == "405 Method Not Allowed"
    assert json.loads(profile_body) == {"error": {"code": "method_not_allowed"}}

    for method, path, payload in (
        ("PATCH", "/api/merchants/1/channels/1148", {"description": "稳定"}),
        ("PATCH", "/api/merchants/1/listings/4100", {"description": "Team池"}),
        ("POST", "/api/merchants/1/retirement", {}),
        ("PUT", "/api/economic-priority1/gpt-5.6-sol/profit", {"profit_percent": 60}),
    ):
        operation_status, _, _ = request(
            app,
            path,
            method=method,
            json_body=payload,
            action_header=True,
        )
        assert operation_status.startswith(("200", "202"))

    assert manager.calls == [
        ("add", "fixture", "secret"),
        ("channel", 1, 1148, {"description": "稳定"}),
        ("listing", 1, 4100, {"description": "Team池"}),
        ("retirement", 1),
        ("profit", "gpt-5.6-sol", 60),
    ]


def test_internal_publication_routes_require_bearer_auth_not_dashboard_header() -> None:
    control = FakePublicationControl()
    app = create_dashboard_app(
        FakeRepository(),
        publication_control=control,
        internal_api_token=INTERNAL_TOKEN,
    )
    payload = {
        "idempotency_key": "failure-req-1",
        "expected_generation": 42,
        "requested_by": "sub2api-us",
        "trace_id": "trace-1",
    }

    missing, missing_headers, _ = request(
        app,
        "/internal/provider-attempt-failures",
        method="POST",
        json_body=payload,
        action_header=True,
    )
    wrong, _, _ = request(
        app,
        "/internal/provider-attempt-failures",
        method="POST",
        json_body=payload,
        bearer_token="wrong-token",
    )
    accepted, accepted_headers, accepted_body = request(
        app,
        "/internal/provider-attempt-failures",
        method="POST",
        json_body=payload,
        bearer_token=INTERNAL_TOKEN,
    )

    assert missing == "401 Unauthorized"
    assert wrong == "401 Unauthorized"
    assert missing_headers["WWW-Authenticate"] == 'Bearer realm="a6-trader-internal"'
    assert accepted == "202 Accepted"
    assert accepted_headers["Cache-Control"] == "no-store, private"
    assert json.loads(accepted_body)["publication_requested"] is True
    assert control.calls == [("failure", payload)]


def test_internal_publication_reconcile_and_metrics_are_authenticated() -> None:
    control = FakePublicationControl()
    app = create_dashboard_app(
        FakeRepository(),
        publication_control=control,
        internal_api_token=INTERNAL_TOKEN,
    )
    payload = {
        "idempotency_key": "manual-1",
        "expected_generation": 42,
        "requested_by": "operator",
        "trace_id": "trace-2",
        "reason_code": "slo_drill",
    }

    status, _, body = request(
        app,
        "/internal/models/gpt-5.6-sol/publication/reconcile",
        method="POST",
        json_body=payload,
        bearer_token=INTERNAL_TOKEN,
    )
    metric_status, metric_headers, metric_body = request(
        app,
        "/metrics",
        bearer_token=INTERNAL_TOKEN,
    )

    assert status == "202 Accepted"
    assert json.loads(body) == {
        "id": 71,
        "model": "gpt-5.6-sol",
        "status": "pending",
    }
    assert metric_status == "200 OK"
    assert metric_headers["Content-Type"] == "text/plain; version=0.0.4; charset=utf-8"
    assert b"a6_model_publication_state" in metric_body
    assert control.calls == [
        ("reconcile", "gpt-5.6-sol", payload),
        ("metrics",),
    ]


def test_internal_quarantine_probe_and_recovery_routes_bind_path_identity() -> None:
    control = FakePublicationControl()
    app = create_dashboard_app(
        FakeRepository(),
        publication_control=control,
        internal_api_token=INTERNAL_TOKEN,
    )
    probe_payload = {
        "idempotency_key": "probe-1",
        "expected_generation": 42,
        "requested_by": "reconciler",
        "trace_id": "trace-probe-1",
    }
    probe_status, _, probe_body = request(
        app,
        "/internal/models/gpt-5.6-sol/provider-quarantines/17/probe",
        method="POST",
        json_body=probe_payload,
        bearer_token=INTERNAL_TOKEN,
    )
    recovery_payload = {
        **probe_payload,
        "idempotency_key": "recover-1",
        "probe_token": "probe-token-1",
    }
    recovery_status, _, recovery_body = request(
        app,
        "/internal/models/gpt-5.6-sol/provider-quarantines/17/recover",
        method="POST",
        json_body=recovery_payload,
        bearer_token=INTERNAL_TOKEN,
    )

    assert probe_status == "202 Accepted"
    assert json.loads(probe_body)["status"] == "probing"
    assert recovery_status == "202 Accepted"
    assert json.loads(recovery_body)["status"] == "verified"
    assert control.calls == [
        ("quarantine_probe", "gpt-5.6-sol", {**probe_payload, "quarantine_id": 17}),
        (
            "quarantine_recover",
            "gpt-5.6-sol",
            {**recovery_payload, "quarantine_id": 17},
        ),
    ]


def credential_fixture(
    tmp_path: Path,
    repository: FakeRepository,
    *,
    merchant_id: int = 1,
) -> tuple[Path, Path, dict[str, Any]]:
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir(mode=0o700)
    os.chmod(sessions_dir, 0o700)
    path = sessions_dir / f"merchant-{merchant_id}.json"
    payload = {
        "schema_version": 1,
        "origin": "https://fixture.invalid",
        "username": f"merchant-{merchant_id}",
        "email": f"merchant-{merchant_id}@fixture.invalid",
        "password": f"password-secret-{merchant_id}",
        "cookies": {"session": f"cookie-secret-{merchant_id}"},
        "a6api_key": f"sk-secret-{merchant_id}",
        "user_id": 1000 + merchant_id,
        "supplier_id": 2000 + merchant_id,
        "status": "supplier_submitted",
    }
    write_private_json(path, payload)
    session_ref = f"/legacy/import/sessions/{path.name}"
    repository.credential_bindings[merchant_id] = {
        "merchant_id": merchant_id,
        "a6_user_id": payload["user_id"],
        "supplier_id": payload["supplier_id"],
        "session_ref": session_ref,
        "session_fingerprint": _session_fingerprint(session_ref, payload),
    }
    return sessions_dir, path, payload


def test_credentials_are_read_on_demand_with_private_cache_headers(tmp_path: Path) -> None:
    repository = FakeRepository()
    sessions_dir, _, payload = credential_fixture(tmp_path, repository)
    app = create_dashboard_app(repository, sessions_dir=sessions_dir)

    merchant_status, _, merchant_body = request(app, "/api/merchants", query="window=1h")
    assert merchant_status == "200 OK"
    assert repository.credential_binding_calls == []
    assert payload["password"].encode() not in merchant_body

    status, headers, body = request(app, "/api/merchants/1/credentials")

    assert status == "200 OK"
    assert headers["Cache-Control"] == "no-store, private"
    assert headers["Pragma"] == "no-cache"
    assert headers["Content-Type"] == "application/json; charset=utf-8"
    assert repository.credential_binding_calls == [1]
    assert json.loads(body) == {
        "merchant_id": "1",
        "username": payload["username"],
        "email": payload["email"],
        "password": payload["password"],
    }
    assert payload["cookies"]["session"].encode() not in body
    assert payload["a6api_key"].encode() not in body


def test_credentials_missing_merchant_is_private_404(tmp_path: Path) -> None:
    repository = FakeRepository()
    sessions_dir, _, _ = credential_fixture(tmp_path, repository)
    app = create_dashboard_app(repository, sessions_dir=sessions_dir)

    status, headers, body = request(app, "/api/merchants/2/credentials")

    assert status == "404 Not Found"
    assert headers["Cache-Control"] == "no-store, private"
    assert headers["Pragma"] == "no-cache"
    assert json.loads(body) == {"error": {"code": "merchant_not_found"}}


@pytest.mark.parametrize("binding_field", ["a6_user_id", "supplier_id", "session_fingerprint"])
def test_credentials_reject_mismatched_database_binding_without_logging_values(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    binding_field: str,
) -> None:
    repository = FakeRepository()
    sessions_dir, _, payload = credential_fixture(tmp_path, repository)
    binding = dict(repository.credential_bindings[1])
    binding[binding_field] = "f" * 64 if binding_field == "session_fingerprint" else 99999
    repository.credential_bindings[1] = binding
    app = create_dashboard_app(repository, sessions_dir=sessions_dir)

    status, headers, body = request(app, "/api/merchants/1/credentials")

    assert status == "503 Service Unavailable"
    assert headers["Cache-Control"] == "no-store, private"
    assert headers["Pragma"] == "no-cache"
    assert json.loads(body) == {"error": {"code": "credentials_unavailable"}}
    assert payload["username"] not in caplog.text
    assert payload["email"] not in caplog.text
    assert payload["password"] not in caplog.text
    assert payload["cookies"]["session"] not in caplog.text


def test_credentials_require_exact_directory_and_file_permissions(tmp_path: Path) -> None:
    repository = FakeRepository()
    sessions_dir, path, _ = credential_fixture(tmp_path, repository)
    os.chmod(sessions_dir, 0o750)
    with pytest.raises(CredentialAccessError, match="credential_sessions_dir_permissions_invalid"):
        create_dashboard_app(repository, sessions_dir=sessions_dir)

    os.chmod(sessions_dir, 0o700)
    os.chmod(path, 0o640)
    app = create_dashboard_app(repository, sessions_dir=sessions_dir)
    status, _, body = request(app, "/api/merchants/1/credentials")
    assert status == "503 Service Unavailable"
    assert json.loads(body) == {"error": {"code": "credentials_unavailable"}}


def test_credentials_reject_session_symlink_outside_root(tmp_path: Path) -> None:
    repository = FakeRepository()
    sessions_dir, path, payload = credential_fixture(tmp_path, repository)
    outside = tmp_path / "outside.json"
    write_private_json(outside, payload)
    path.unlink()
    path.symlink_to(outside)
    app = create_dashboard_app(repository, sessions_dir=sessions_dir)

    status, _, body = request(app, "/api/merchants/1/credentials")

    assert status == "503 Service Unavailable"
    assert json.loads(body) == {"error": {"code": "credentials_unavailable"}}


def test_credentials_route_rejects_noncanonical_merchant_ids(tmp_path: Path) -> None:
    repository = FakeRepository()
    sessions_dir, _, _ = credential_fixture(tmp_path, repository)
    app = create_dashboard_app(repository, sessions_dir=sessions_dir)

    for path in (
        "/api/merchants/0/credentials",
        "/api/merchants/01/credentials",
        "/api/merchants/../credentials",
        "/api/merchants/9223372036854775808/credentials",
    ):
        status, _, body = request(app, path)
        assert status == "404 Not Found"
        assert json.loads(body) == {"error": {"code": "not_found"}}
    assert repository.credential_binding_calls == []


def test_healthz_projects_only_public_health_fields() -> None:
    app = create_dashboard_app(FakeRepository())

    status, headers, body = request(app, "/healthz")

    assert status == "200 OK"
    assert headers["Content-Type"] == "application/json; charset=utf-8"
    assert headers["Cache-Control"] == "no-store"
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert int(headers["Content-Length"]) == len(body)
    assert json.loads(body) == {
        "status": "ok",
        "checked_at": "2026-07-24T05:10:00Z",
        "last_success_at": "2026-07-24T05:09:57Z",
    }
    assert b"ADMIN_KEY_MUST_NOT_LEAK" not in body


@pytest.mark.parametrize("window", WINDOWS)
def test_overview_accepts_every_supported_window(window: str) -> None:
    repository = FakeRepository()
    app = create_dashboard_app(repository)

    status, _, body = request(app, "/api/overview", query=f"window={window}")

    payload = json.loads(body)
    assert status == "200 OK"
    assert payload["window"] == window
    assert payload["merchant_count"] == 19
    assert payload["stocks"]["user_balance"] == {
        "start_micros": 10_000_000,
        "current_micros": 12_500_000,
        "change_micros": 2_500_000,
        "min_micros": 9_000_000,
        "max_micros": 13_000_000,
    }
    assert payload["flows"]["actual_profit_micros"] is None
    assert payload["flows"]["honest_profit_micros"] == 3_650_000
    assert payload["flows"]["profit_margin"] is None
    assert payload["flows"]["profit_status"] == "partial"
    assert payload["flows"]["sales_status"] == "confirmed"
    assert payload["flows"]["unattributed_provider_cost_micros"] == 500_000
    assert payload["flows"]["external_enodot_revenue_micros"] == 7_000_000
    assert payload["flows"]["external_enodot_request_count"] == 700
    assert payload["flows"]["managed_internal_debit_micros"] == 600_000
    assert payload["flows"]["managed_internal_request_count"] == 60
    assert payload["flows"]["external_provider_cost_micros"] == 3_500_000
    assert payload["flows"]["managed_internal_provider_cost_micros"] == 500_000
    assert payload["flows"]["owner_internal_debit_micros"] == 27_593
    assert payload["flows"]["owner_internal_request_count"] == 1
    assert payload["flows"]["owner_internal_provider_cost_micros"] == 398
    assert payload["flows"]["pin_probe_cost_micros"] == 20_000
    assert payload["flows"]["pin_probe_event_count"] == 2
    assert payload["flows"]["enodot_provider_cost_micros"] == 600_000
    assert payload["flows"]["confirmed_enodot_provider_cost_micros"] == 500_000
    assert payload["flows"]["unattributed_enodot_provider_cost_micros"] == 100_000
    assert payload["flows"]["unattributed_enodot_provider_event_count"] == 10
    assert repository.overview_calls == [window]
    assert b"MUST_NOT_LEAK" not in body


def test_overview_uses_one_hour_default() -> None:
    repository = FakeRepository()
    app = create_dashboard_app(repository)

    status, _, body = request(app, "/api/overview")

    assert status == "200 OK"
    assert json.loads(body)["window"] == "1h"
    assert repository.overview_calls == ["1h"]


@pytest.mark.parametrize("window", ("5m", "15m", "1h"))
def test_market_traffic_projects_realtime_bands_and_positions(window: str) -> None:
    repository = FakeRepository()
    app = create_dashboard_app(repository)

    status, headers, body = request(
        app,
        "/api/market-traffic",
        query=f"window={window}&model=gpt-5.6-sol",
    )

    payload = json.loads(body)
    assert status == "200 OK"
    assert headers["Cache-Control"] == "no-store"
    assert payload["window"] == window
    assert payload["model"] == "gpt-5.6-sol"
    assert payload["method"]["count_semantics"] == "lower_bound_active_seconds"
    assert payload["method"]["current_signal_max_age_seconds"] == 30
    assert payload["method"]["automatic_repricing"] is False
    assert payload["freshness"]["current_channel_count"] == 163
    assert payload["observer_backoff"] == {
        "active": True,
        "consecutive_failed_rounds": 3,
        "remaining_seconds": 52,
        "retry_after": "2026-07-25T14:00:52Z",
        "last_error_class": "a6_transport_failed",
        "last_failure_at": "2026-07-25T14:00:00Z",
        "last_success_at": "2026-07-25T13:59:30Z",
    }
    assert payload["totals"]["event_lower_bound"] == 22
    assert payload["bands"][0]["label"] == "K_CNY 0.07-0.08"
    assert payload["bands"][0]["just_now_rate_per_minute"] == 8
    assert payload["managed_positions"][0]["channel_id"] == "768"
    assert payload["managed_positions"][0]["channel_name"] == "OmniAPI SOL Route"
    assert payload["managed_positions"][0]["health_state"] == "healthy"
    assert payload["managed_positions"][0]["recent_success_rate"] == 9875
    assert payload["managed_positions"][0]["recent_p50_ms"] == 812.5
    assert payload["managed_positions"][0]["smart_routing_labels"] == [
        "fast",
        "stable",
    ]
    assert payload["managed_positions"][0]["recent_client_failure_count"] == 2
    assert payload["managed_positions"][0]["recent_provider_failure_count"] == 1
    assert payload["managed_positions"][0]["operational_state"] == "active"
    assert payload["managed_positions"][0]["blocked_reason"] == ""
    assert payload["managed_positions"][0]["merchant_status"] == "supplier_submitted"
    assert payload["managed_positions"][0]["binding_control_state"] == "verified"
    assert payload["managed_positions"][0]["listing_status"] == 2
    blocked = payload["managed_positions"][1]
    assert blocked["operational_state"] == "blocked"
    assert blocked["blocked_reason"] == "listing_status_inactive"
    assert blocked["merchant_status"] == "supplier_submitted"
    assert blocked["merchant_roles"] == ["seller"]
    assert blocked["binding_present"] is True
    assert blocked["binding_control_state"] == "verified"
    assert blocked["listing_present"] is True
    assert blocked["listing_status"] == 0
    assert blocked["listing_availability"] == 0
    assert blocked["listing_visibility"] == 0
    assert payload["channel_failures"][0]["last_failure_responsibility"] == "client"
    assert payload["channel_failures"][0]["top_failure_codes"] == [
        {
            "code": "invalid_prompt",
            "responsibility": "client",
            "count": 4,
            "last_failure_at": "2026-07-25T13:59:57Z",
        }
    ]
    assert payload["managed_positions"][0]["delta_sample_status"] == "routing_unverified"
    assert repository.market_traffic_calls == [(window, "gpt-5.6-sol")]
    assert b"MUST_NOT_LEAK" not in body


def test_market_traffic_sanitizer_does_not_truncate_managed_plan_positions() -> None:
    payload = _sanitize_market_traffic(
        {
            "managed_positions": [
                {
                    "rank": rank,
                    "merchant_id": rank,
                    "operational_state": "blocked",
                    "blocked_reason": "binding_missing",
                    "merchant_status": "supplier_submitted",
                    "merchant_roles": ["seller"],
                    "binding_present": False,
                    "listing_present": False,
                }
                for rank in range(1, 102)
            ]
        },
        "5m",
        "gpt-5.6-sol",
    )

    assert len(payload["managed_positions"]) == 101
    assert payload["managed_positions"][0]["merchant_id"] == "1"
    assert payload["managed_positions"][-1]["merchant_id"] == "101"


@pytest.mark.parametrize(
    "query",
    ("window=all", "window=5m&model=bad%20model", "window=2m"),
)
def test_market_traffic_rejects_invalid_query(query: str) -> None:
    repository = FakeRepository()
    app = create_dashboard_app(repository)

    status, _, body = request(app, "/api/market-traffic", query=query)

    assert status == "400 Bad Request"
    assert json.loads(body)["error"]["code"] == "invalid_market_traffic_query"
    assert repository.market_traffic_calls == []


def test_profit_margin_remains_unknown_until_confirmed_sales_exist() -> None:
    payload = _sanitize_overview(
        {
            "flows": {
                "actual_profit_micros": 125,
                "profit_margin": None,
                "profit_status": "pending",
                "sales_status": "pending",
            }
        },
        "1h",
    )

    assert payload["flows"]["actual_profit_micros"] is None
    assert payload["flows"]["profit_margin"] is None
    assert payload["flows"]["profit_status"] == "pending"
    assert payload["flows"]["sales_status"] == "pending"


def test_pending_profit_preserves_null_and_unattributed_sales_evidence() -> None:
    payload = _sanitize_overview(
        {
            "flows": {
                "actual_profit_micros": None,
                "honest_profit_micros": 0,
                "unattributed_enodot_sales_micros": 6_208_343,
                "unattributed_enodot_sales_request_count": 85,
                "profit_margin": None,
                "profit_status": "pending",
                "sales_status": "confirmed",
            }
        },
        "1h",
    )

    assert payload["flows"]["actual_profit_micros"] is None
    assert payload["flows"]["honest_profit_micros"] == 0
    assert payload["flows"]["unattributed_enodot_sales_micros"] == 6_208_343
    assert payload["flows"]["unattributed_enodot_sales_request_count"] == 85
    assert payload["flows"]["profit_status"] == "pending"


def test_invalid_window_is_rejected_before_repository_access() -> None:
    repository = FakeRepository()
    app = create_dashboard_app(repository)

    status, _, body = request(app, "/api/overview", query="window=1month")

    assert status == "400 Bad Request"
    assert json.loads(body) == {"error": {"code": "invalid_window", "allowed": list(WINDOWS)}}
    assert repository.overview_calls == []


def test_merchants_are_allowlisted_and_upstream_details_are_removed() -> None:
    repository = FakeRepository()
    app = create_dashboard_app(repository)

    status, _, body = request(app, "/api/merchants", query="window=5m")

    payload = json.loads(body)
    merchant = payload["items"][0]
    assert status == "200 OK"
    assert payload["window"] == "5m"
    assert payload["count"] == 2
    assert repository.merchant_calls == ["5m"]
    assert merchant["roles"] == ["provider", "seller"]
    assert merchant["last_error_class"] == "quota_insufficient"
    assert merchant["current_pin_channel_id"] == "790"
    assert len(merchant["pin_rates"]) == 2
    assert merchant["pin_rates"][0] == {
        "token_id": "14020",
        "model": "gpt-5.6-sol",
        "channel_id": "790",
        "rate_multiplier": pytest.approx(0.118),
        "expected_cost_multiplier": pytest.approx(0.12),
        "market_final_multiplier": pytest.approx(0.118),
        "realtime_ratio_usd": pytest.approx(0.118),
        "realtime_ratio_cny": pytest.approx(0.7991196),
        "exchange_rate": pytest.approx(6.7722),
        "decision_status": "completed",
        "confirmed_at": "2026-07-24T05:08:00Z",
        "quote_observed_at": "2026-07-24T05:07:59Z",
    }
    assert merchant["pin_rates"][1]["model"] == "gpt-5.6-terra"
    assert merchant["pin_rates"][1]["rate_multiplier"] == pytest.approx(0.2)
    assert len(merchant["listing_rates"]) == 2
    assert {
        key: merchant["listing_rates"][0][key]
        for key in (
            "listing_id",
            "channel_id",
            "model",
            "managed_state",
            "control_state",
            "desired_delta",
            "observed_delta",
            "price_multiplier",
            "market_price_multiplier",
            "price_multiplier_cny",
            "exchange_rate",
            "input_price_micros",
            "output_price_micros",
            "cache_read_price_micros",
            "cache_write_price_micros",
            "status",
            "availability",
            "visibility",
            "observed_at",
        )
    } == {
        "listing_id": "900",
        "channel_id": "790",
        "model": "gpt-5.6-sol",
        "managed_state": "managed",
        "control_state": "verified",
        "desired_delta": pytest.approx(0.5),
        "observed_delta": pytest.approx(0.5),
        "price_multiplier": pytest.approx(0.117504),
        "market_price_multiplier": pytest.approx(0.1410048),
        "price_multiplier_cny": pytest.approx(0.95491270656),
        "exchange_rate": pytest.approx(6.7722),
        "input_price_micros": 587_520,
        "output_price_micros": 3_525_120,
        "cache_read_price_micros": 58_752,
        "cache_write_price_micros": 734_400,
        "status": 2,
        "availability": 1,
        "visibility": 1,
        "observed_at": "2026-07-24T05:09:56Z",
    }
    assert merchant["listing_rates"][0]["health_state"] == "cooling"
    assert merchant["listing_rates"][0]["health_observer_count"] == 3
    assert merchant["listing_rates"][0]["health_confirmation_count"] == 2
    assert merchant["listing_rates"][0]["circuit_probe_success_required"] == 2
    assert merchant["listing_rates"][0]["circuit_probe_success_streak"] == 1
    assert merchant["listing_rates"][0]["circuit_scope"] == "listing"
    assert merchant["listing_rates"][0]["circuit_recovery_mode"] == "natural"
    assert merchant["listing_rates"][0]["circuit_cooling_until"] == ("2026-07-24T05:12:00Z")
    assert merchant["listing_rates"][0]["circuit_next_probe_at"] == ("2026-07-24T05:10:50Z")
    assert merchant["listing_rates"][0]["cooldown_sources"] == [
        "circuit",
        "listing:cooling",
    ]
    assert merchant["listing_rates"][0]["growth_enabled"] is False
    assert merchant["listing_rates"][0]["smart_routing_labels"] == []
    assert merchant["listing_rates"][1]["model"] == "gpt-5.6-luna"
    assert merchant["listing_rates"][1]["price_multiplier"] is None
    assert merchant["actual_profit_micros"] is None
    assert merchant["honest_profit_micros"] == 325_000
    assert merchant["profit_status"] == "partial"
    assert merchant["sales_status"] == "confirmed"
    assert merchant["confirmed_enodot_sales_micros"] == 900_000
    assert merchant["confirmed_enodot_sales_request_count"] == 90
    assert merchant["unattributed_enodot_sales_micros"] == 0
    assert merchant["unattributed_enodot_sales_request_count"] == 0
    assert merchant["external_enodot_revenue_micros"] == 900_000
    assert merchant["external_enodot_request_count"] == 90
    assert merchant["confirmed_external_enodot_revenue_micros"] == 900_000
    assert merchant["managed_internal_debit_micros"] == 60_000
    assert merchant["managed_internal_request_count"] == 6
    assert merchant["external_provider_cost_micros"] == 550_000
    assert merchant["managed_internal_provider_cost_micros"] == 50_000
    assert merchant["owner_internal_debit_micros"] == 27_593
    assert merchant["owner_internal_request_count"] == 1
    assert merchant["owner_internal_provider_cost_micros"] == 398
    assert merchant["pin_probe_cost_micros"] == 1_000
    assert merchant["pin_probe_event_count"] == 1
    assert merchant["enodot_provider_cost_micros"] == 60_000
    assert merchant["confirmed_enodot_provider_cost_micros"] == 50_000
    assert merchant["unattributed_enodot_provider_cost_micros"] == 10_000
    assert merchant["unattributed_enodot_provider_event_count"] == 1
    assert merchant["unattributed_provider_cost_micros"] == 50_000
    assert merchant["profit_margin"] is None
    assert merchant["withdraw_account_state"] == "bound"
    assert merchant["withdraw_account_network"] == "ERC20"
    assert merchant["withdraw_account_masked"] == "0x1234...5678"
    assert merchant["listing_total_count"] == 5
    assert merchant["active_listing_count"] == 3
    assert merchant["managed_channel_count"] == 1
    assert merchant["existing_unmanaged_channel_count"] == 1
    assert merchant["pending_listing_count"] == 1
    assert merchant["down_listing_count"] == 1
    assert merchant["managed_listing_count"] == 2
    assert merchant["managed_active_listing_count"] == 1
    assert merchant["existing_unmanaged_listing_count"] == 3
    assert payload["items"][1]["roles"] == ["observer"]
    assert merchant["existing_unmanaged_active_listing_count"] == 2
    assert merchant["listing_last_observed_at"] == "2026-07-24T05:09:56Z"
    assert payload["items"][1]["display_name"] == "merchant-02"
    assert payload["items"][1]["managed_channel_count"] == 0
    assert payload["items"][1]["managed_listing_count"] == 0
    assert payload["items"][1]["existing_unmanaged_listing_count"] == 0
    assert payload["items"][1]["pin_rates"] == []
    assert payload["items"][1]["listing_rates"] == []
    for private_value in (
        b"secret@example.com",
        b"private@example.com",
        b"PASSWORD_MUST_NOT_LEAK",
        b"SESSION_MUST_NOT_LEAK",
        b"KEY_MUST_NOT_LEAK",
        b"SECRET_REQUEST_ID",
        b"Access forbidden",
        b"NESTED_PIN_KEY_MUST_NOT_LEAK",
        b"NESTED_LISTING_SESSION_MUST_NOT_LEAK",
    ):
        assert private_value not in body


def test_provider_only_role_is_not_rewritten_to_seller() -> None:
    raw = FakeRepository().get_merchants("5m")[0]
    sanitized = _sanitize_merchant({**raw, "roles": ["provider"]})

    assert sanitized["roles"] == ["provider"]


def test_only_safe_archive_completion_is_presented_as_archived() -> None:
    raw = FakeRepository().get_merchants("5m")[0]

    legacy = _sanitize_merchant(
        {
            **raw,
            "retirement_state": "completed",
            "retirement_archive_mode": False,
        }
    )
    archived = _sanitize_merchant(
        {
            **raw,
            "retirement_state": "completed",
            "retirement_archive_mode": True,
        }
    )
    legacy_pending = _sanitize_merchant(
        {
            **raw,
            "retirement_state": "earning",
            "retirement_archive_mode": False,
        }
    )

    assert legacy["retirement_phase"] == "none"
    assert legacy_pending["retirement_phase"] == "none"
    assert archived["retirement_phase"] == "archived"


def test_missing_withdraw_account_mask_stays_empty() -> None:
    repository = FakeRepository()
    app = create_dashboard_app(repository)

    status, _, body = request(app, "/api/merchants", query="window=1h")

    assert status == "200 OK"
    merchant = json.loads(body)["items"][1]
    assert merchant["withdraw_account_masked"] == ""


def test_repository_exception_returns_generic_503() -> None:
    repository = FakeRepository()
    repository.fail_overview = True
    app = create_dashboard_app(repository)

    status, _, body = request(app, "/api/overview", query="window=1h")

    assert status == "503 Service Unavailable"
    assert json.loads(body) == {"error": {"code": "repository_unavailable"}}
    assert b"DATABASE_URL_WITH_SECRET" not in body


def test_index_serves_responsive_dashboard() -> None:
    app = create_dashboard_app(FakeRepository())

    status, headers, body = request(app, "/")

    assert status == "200 OK"
    assert headers["Content-Type"] == "text/html; charset=utf-8"
    assert b'<meta name="viewport"' in body
    assert "A6 商户总览".encode() in body
    assert b'data-window="30d"' in body
    assert b'data-window="mtd"' in body
    assert b"api/overview" in body
    assert b"api/merchants" in body
    assert b"overviewController = new AbortController()" in body
    assert b"merchantsController = new AbortController()" in body
    assert b"overviewController.abort(), 30000" in body
    assert b"merchantsController.abort(), 30000" in body
    assert "实时流量更新异常，保留最近数据".encode() in body
    assert ">受管渠道</h3>".encode() in body
    assert b'id="traffic-position-count"' in body
    listing_filter_label = (
        '<label class="visually-hidden" for="traffic-position-filter">上架状态</label>'
    )
    assert listing_filter_label.encode() in body
    assert '<option value="listed" selected>已上架</option>'.encode() in body
    assert '<option value="unlisted">未上架</option>'.encode() in body
    assert "Provider 路由与发布".encode() in body
    assert b"internal/provider-pools/" in body
    assert b'id="provider-mode"' in body
    assert "影子计划".encode() in body
    assert b'id="provider-slot-rows"' in body
    assert "渠道 / 上架".encode() in body
    assert "Listing 四价 / 倍率".encode() in body
    assert "Pin / 实时倍率".encode() in body
    assert b"const channelLabel = channelIds.length" in body
    assert "${channelLabel} · 面板 ${integerFormat.format(managedActive)}".encode() in body
    assert "已有未受管 ${integerFormat.format(unmanagedActive)}".encode() in body
    assert b"function listingRatesCell(merchant)" in body
    assert b"for (const rate of rates)" in body
    assert b"K_CNY ${formatMultiplier(rate.realtime_ratio_cny)}" in body
    assert b"K_USD" not in body
    assert b"USD/CNY" not in body
    assert b"K_CNY ${formatMultiplier(rate.price_multiplier_cny)}" in body
    assert "成本 K_CNY".encode() in body
    assert b"const merchants = Array.isArray(state.merchants) ? state.merchants : [];" in body
    assert b"formatCnyMultiplier(slot.desired_cost_multiplier, exchangeRate)" in body
    assert b'channel ${rate.channel_id || "--"}' in body
    assert b'listing ${rate.listing_id || "--"}' in body
    assert b"formatTime(rate.quote_observed_at || rate.confirmed_at)" in body
    assert b"formatTime(rate.observed_at)" in body
    assert b'class="merchant-table"' in body
    assert (
        b'row.className = `summary-row${isObserverOnly(merchant) ? " archived-row" : ""}`' in body
    )
    assert b'className = "detail-row"' in body
    assert b"row.hidden = !expanded" in body
    assert b"data-merchant-toggle" in body
    assert b"cell.colSpan = 8" in body
    assert "资金与结算".encode() in body
    assert "Pin / 实时倍率".encode() in body
    assert "Listing 四价 / 倍率".encode() in body
    assert "Enodot 绑定".encode() in body
    assert "A6 登录凭据".encode() in body
    assert "账号密码已遮罩".encode() in body
    assert "重新遮罩账号密码".encode() in body
    assert b"function credentialCell(merchant)" in body
    assert b"async function toggleCredentials(merchantKey)" in body
    assert b"api/merchants/${encodeURIComponent(merchantKey)}/credentials" in body
    assert b"state.credentials: " not in body
    assert b"credentials: new Map()" in body
    assert b"dataset.credentialToggle" in body
    assert b"grid-template-columns: repeat(2, minmax(0, 1fr))" in body
    assert b"grid-template-columns: 1fr" in body
    assert "另有 ${integerFormat.format".encode() not in body
    assert '<label for="duty-filter">运行职责</label>'.encode() in body
    assert '<option value="">全部运行职责</option>'.encode() in body
    assert '<option value="seller-only">Seller-only（无额外职责）</option>'.encode() in body
    assert b'<option value="provider">Provider duty</option>' in body
    assert b'<option value="observer">Observer duty</option>' in body
    assert b'<option value="probe">' not in body
    assert "身份 / 健康".encode() in body
    assert b'if (roles.has("seller")) badges.append(badge("Seller", "base-role"));' in body
    assert b'badges.append(badge(`${dutyLabels[duty]} duty`, "duty"));' in body
    assert b'duty === "seller-only" && (!roles.has("seller") || activeDuties.length)' in body
    assert b'const activeDuties = ["provider", "observer"]' in body
    assert "Probe 损耗".encode() in body
    assert "Managed Key 内部扣账".encode() in body
    assert "Seller 毛收益 / Managed Key 内部扣账".encode() in body
    assert b"function sellerIncomeCell(merchant)" in body
    assert "A6 Provider 成本待归属".encode() in body
    assert "外部收入缺少 Provider 成本".encode() in body
    assert "Enodot 外部收入未匹配 Provider 成本".encode() in body
    assert "均不计收入".encode() in body
    assert b"Owner/Internal" in body
    assert "Pin 探活".encode() in body
    assert b'profitConfirmed && rawAmount != null ? formatMicros(amount) : "--"' in body
    assert b'confirmed && value != null ? formatMicros(value) : "--"' in body
    assert b"return `$${amountFormat.format(amount)}`;" in body
    assert "额度`".encode() not in body


@pytest.mark.skipif(shutil.which("node") is None, reason="node runtime is not installed")
def test_dashboard_credential_dom_interaction() -> None:
    result = subprocess.run(
        ["node", "--test", "tests/test_trade_dashboard_dom.mjs"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_rate_query_prefers_quote_and_derives_missing_sol_multiplier() -> None:
    normalized = " ".join(_MERCHANT_QUERY.split())

    assert "coalesce( nullif(m.supplier_name, ''), 'merchant-'" in normalized
    assert (
        "'rate_multiplier', coalesce( current_pin.market_final_multiplier, "
        "current_pin.expected_cost_multiplier )"
    ) in normalized
    assert "'realtime_ratio_usd', coalesce(" in normalized
    assert "'realtime_ratio_cny', current_pin.realtime_ratio_cny" in normalized
    assert "'exchange_rate', current_pin.exchange_rate" in normalized
    assert "q.input_price / 5000000::numeric" in normalized
    assert "'market_price_multiplier', CASE" in normalized
    assert "/ 0.833333333333::numeric" in normalized


def test_dashboard_systemd_uses_current_release_python() -> None:
    service = Path("deploy/systemd/a6-trader-dashboard.service").read_text()

    assert (
        "ExecStart=/opt/a6-trader/current/venv/bin/python "
        "/opt/a6-trader/current/venv/bin/a6api-auto trade-dashboard "
    ) in service


def test_unknown_route_and_non_get_method_have_stable_errors() -> None:
    app = create_dashboard_app(FakeRepository())

    missing_status, _, missing_body = request(app, "/missing")
    method_status, method_headers, method_body = request(app, "/api/overview", method="POST")

    assert missing_status == "404 Not Found"
    assert json.loads(missing_body) == {"error": {"code": "not_found"}}
    assert method_status == "405 Method Not Allowed"
    assert method_headers["Allow"] == "GET"
    assert json.loads(method_body) == {"error": {"code": "method_not_allowed"}}
