from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from run_retirement_callback_once import build_runner


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(
        description="Run one audited Listing recycle for a specific cooling target."
    )
    command.add_argument("--merchant-id", type=int, required=True)
    command.add_argument("--expected-channel-id", type=int, required=True)
    command.add_argument("--expected-listing-id", type=int, required=True)
    command.add_argument("--sessions-dir", type=Path, required=True)
    command.add_argument("--proxy-map-file", type=Path, required=True)
    command.add_argument("--seller-key-dir", type=Path, required=True)
    command.add_argument("--enodot-admin-key-file", type=Path, required=True)
    command.add_argument("--model", default="gpt-5.6-sol")
    command.add_argument("--signal-max-age-seconds", type=int, default=45)
    command.add_argument("--callback-gateway-url", default="https://a6.a6api.com")
    command.add_argument("--callback-timeout-seconds", type=float, default=30)
    command.add_argument("--config-version", default="targeted-listing-recycle-v1")
    command.add_argument("--min-delay-seconds", type=float, default=0.8)
    command.add_argument("--max-delay-seconds", type=float, default=2.0)
    return command


def main() -> int:
    args = parser().parse_args()
    if min(args.merchant_id, args.expected_channel_id, args.expected_listing_id) < 1:
        raise RuntimeError("targeted_recycle_identity_invalid")
    if (
        args.min_delay_seconds < 0.8
        or args.max_delay_seconds > 2.0
        or args.min_delay_seconds > args.max_delay_seconds
    ):
        raise RuntimeError("targeted_recycle_delay_invalid")

    runner = build_runner(args)
    targets = runner.store.targets(
        args.model,
        signal_max_age_seconds=args.signal_max_age_seconds,
    )
    matches = tuple(target for target in targets if target.merchant_id == args.merchant_id)
    if len(matches) != 1:
        raise RuntimeError("targeted_recycle_target_mismatch")
    target = matches[0]
    if (
        target.channel_id != args.expected_channel_id
        or target.listing_id != args.expected_listing_id
    ):
        raise RuntimeError("targeted_recycle_resource_readback_mismatch")
    if not target.recycle_enabled:
        raise RuntimeError("targeted_recycle_disabled")
    health_state = target.health_state(datetime.now(timezone.utc))
    if health_state not in {"cooling", "unavailable"}:
        raise RuntimeError("targeted_recycle_health_mismatch")

    result = runner._recycle(
        targets,
        target,
        health_state,
        min_delay=args.min_delay_seconds,
        max_delay=args.max_delay_seconds,
        held={},
    )
    print(json.dumps(result, ensure_ascii=True, separators=(",", ":")))
    return 0 if result.get("mode") == "completed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
