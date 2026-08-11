#!/usr/bin/python3
from __future__ import annotations

import argparse
import fcntl
import json
import os
import subprocess
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Sequence


TARGET = "a6-trader-control.target"
LOCK_FILE = Path("/run/lock/a6-trader-control.lock")
DISCOVERY_PATTERNS = (
    "a6-trader-*",
    "a6-listing-refresh*",
)
OWNED_SUB2API_UNITS = (
    "sub2api-bluegreen-48767.service",
    "sub2api-callback-candidate-48768.service",
    "sub2api-paid-send-candidate-48769.service",
)
STOP_ONLY_UNITS = {
    "a6-listing-refresh.service",
    "a6-trader-provider-liquidity.timer",
    "a6-trader-provider-liquidity.service",
    "a6-trader-provider-pool-shadow.timer",
    "a6-trader-provider-pool-shadow.service",
    "a6-trader-focus-register-10.timer",
    "a6-trader-focus-register-10.service",
    "a6-trader-focus-register.service",
    "a6-trader-slow-channel-registration.service",
    "a6-trader-slow-register.service",
}
DISABLEABLE_STATES = {
    "enabled",
    "enabled-runtime",
    "linked",
    "linked-runtime",
}


class ControlError(RuntimeError):
    pass


def _run(
    arguments: Sequence[str],
    *,
    check: bool = True,
    capture: bool = False,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(arguments),
        check=check,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
    )


def _systemctl(*arguments: str, check: bool = True, capture: bool = False) -> subprocess.CompletedProcess[str]:
    return _run(("systemctl", *arguments), check=check, capture=capture)


def _target_members() -> tuple[set[str], set[str]]:
    result = _systemctl(
        "show",
        TARGET,
        "--property=Wants",
        "--property=PropagatesStopTo",
        capture=True,
    )
    properties: dict[str, set[str]] = {}
    for raw_line in result.stdout.splitlines():
        key, separator, raw_value = raw_line.partition("=")
        if separator:
            properties[key] = set(raw_value.split())
    start_units = properties.get("Wants", set())
    stop_units = properties.get("PropagatesStopTo", set())
    if not start_units or not stop_units:
        raise ControlError(f"{TARGET} has an empty control membership")
    missing_from_stop = start_units - stop_units
    if missing_from_stop:
        missing = ", ".join(sorted(missing_from_stop))
        raise ControlError(f"start units missing from stop boundary: {missing}")
    return start_units, stop_units


def _discover_a6_units() -> set[str]:
    result = _systemctl(
        "list-unit-files",
        "--no-legend",
        "--plain",
        *DISCOVERY_PATTERNS,
        check=False,
        capture=True,
    )
    units: set[str] = set()
    for raw_line in result.stdout.splitlines():
        fields = raw_line.split()
        if fields and fields[0].endswith((".service", ".timer", ".path", ".target")):
            units.add(fields[0])
    return units


def _unit_file_state(unit: str) -> str:
    result = _systemctl("is-enabled", unit, check=False, capture=True)
    return result.stdout.strip() or "unknown"


def _disable_independent_autostart(units: set[str]) -> None:
    disableable = sorted(
        unit
        for unit in units
        if unit != TARGET and _unit_file_state(unit) in DISABLEABLE_STATES
    )
    if disableable:
        _systemctl("disable", *disableable)


@contextmanager
def _control_lock() -> Iterator[None]:
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    with LOCK_FILE.open("a+", encoding="ascii") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ControlError("another A6 control operation is running") from exc
        yield


def _require_root() -> None:
    if os.geteuid() != 0:
        raise ControlError("start and stop must run as root")


def start() -> None:
    _require_root()
    start_units, stop_units = _target_members()
    discovered = _discover_a6_units()
    all_owned = stop_units | discovered | set(OWNED_SUB2API_UNITS)
    with _control_lock():
        _disable_independent_autostart(all_owned)
        _systemctl("enable", TARGET)
        _systemctl("stop", *sorted(STOP_ONLY_UNITS))
        # Listing every required unit also repairs a partially running target.
        # systemd still submits the complete start as one dependency transaction.
        _systemctl("start", TARGET, *sorted(start_units))
    print(f"{TARGET}: start transaction completed")


def stop() -> None:
    _require_root()
    start_units, stop_units = _target_members()
    discovered = _discover_a6_units()
    all_owned = stop_units | start_units | discovered | set(OWNED_SUB2API_UNITS)
    with _control_lock():
        # Persist the stopped state across reboot before closing the live workload.
        _systemctl("disable", TARGET)
        _disable_independent_autostart(all_owned)
        # Supplying the complete set creates one stop transaction. This closes
        # timers/paths and cancels already-running workers without a trigger gap.
        _systemctl("stop", TARGET, *sorted(all_owned - {TARGET}))
    print(f"{TARGET}: stop transaction completed")


def _show_units(units: set[str]) -> dict[str, dict[str, str]]:
    if not units:
        return {}
    result = _systemctl(
        "show",
        *sorted(units),
        "--property=Id",
        "--property=LoadState",
        "--property=ActiveState",
        "--property=SubState",
        "--property=Result",
        "--property=UnitFileState",
        check=False,
        capture=True,
    )
    records: dict[str, dict[str, str]] = {}
    current: dict[str, str] = {}
    for raw_line in (*result.stdout.splitlines(), ""):
        if not raw_line:
            unit = current.get("Id")
            if unit:
                records[unit] = current
            current = {}
            continue
        key, separator, value = raw_line.partition("=")
        if separator:
            current[key] = value
    return records


def status(*, as_json: bool) -> None:
    start_units, stop_units = _target_members()
    discovered = _discover_a6_units()
    all_units = {TARGET} | stop_units | discovered | set(OWNED_SUB2API_UNITS)
    records = _show_units(all_units)
    unmanaged = discovered - stop_units - {TARGET}
    target_record = records.get(TARGET, {})
    active_state = target_record.get("ActiveState", "unknown")
    enabled_state = target_record.get("UnitFileState", _unit_file_state(TARGET))
    desired_state = "running" if enabled_state == "enabled" else "stopped"
    payload_units = []
    for unit in sorted(all_units):
        if unit == TARGET:
            role = "target"
        elif unit in start_units:
            role = "start"
        elif unit in stop_units:
            role = "stop-only"
        else:
            role = "unmanaged"
        record = records.get(unit, {})
        payload_units.append(
            {
                "unit": unit,
                "role": role,
                "load": record.get("LoadState", "not-found"),
                "enabled": record.get("UnitFileState", "unknown"),
                "active": record.get("ActiveState", "unknown"),
                "sub": record.get("SubState", "unknown"),
                "result": record.get("Result", "unknown"),
            }
        )
    payload = {
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "target": TARGET,
        "desired_state": desired_state,
        "active_state": active_state,
        "unmanaged_units": sorted(unmanaged),
        "units": payload_units,
    }
    if as_json:
        print(json.dumps(payload, ensure_ascii=True, separators=(",", ":")))
        return
    print(
        f"{TARGET}: desired={desired_state} active={active_state} "
        f"unmanaged={len(unmanaged)}"
    )
    print(f"{'ROLE':10} {'ENABLED':16} {'ACTIVE':12} {'SUB':14} {'RESULT':12} UNIT")
    for item in payload_units:
        print(
            f"{item['role']:10} {item['enabled']:16} {item['active']:12} "
            f"{item['sub']:14} {item['result']:12} {item['unit']}"
        )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Start, stop, or inspect the complete A6 Trader production workload."
    )
    parser.add_argument("action", choices=("start", "stop", "status"))
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit machine-readable status output",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        if arguments.action == "start":
            start()
        elif arguments.action == "stop":
            stop()
        else:
            status(as_json=arguments.json)
    except (ControlError, OSError, subprocess.CalledProcessError) as exc:
        print(f"a6-trader-control: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
