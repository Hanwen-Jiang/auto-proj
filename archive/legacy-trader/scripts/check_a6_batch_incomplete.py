#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path

from a6api_auto.storage import read_private_json


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(description="Check whether an A6 batch still has work")
    command.add_argument("--state-file", type=Path, required=True)
    return command


def batch_incomplete(path: Path) -> bool:
    state = read_private_json(path)
    target = state.get("target_count")
    completed = state.get("completed")
    if (
        isinstance(target, bool)
        or not isinstance(target, int)
        or target < 1
        or not isinstance(completed, list)
        or len(completed) > target
    ):
        raise RuntimeError("batch_incomplete_state_invalid")
    return len(completed) < target


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        incomplete = batch_incomplete(args.state_file)
    except Exception:
        print(json.dumps({"success": False, "error": "batch_incomplete_state_invalid"}))
        # ExecCondition treats 1..254 as a normal skip; 255 makes corrupt state visible.
        return 255
    print(
        json.dumps(
            {
                "success": True,
                "state": "incomplete" if incomplete else "completed",
            },
            separators=(",", ":"),
        )
    )
    return 0 if incomplete else 1


if __name__ == "__main__":
    raise SystemExit(main())
