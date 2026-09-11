#!/usr/bin/env python3
"""Conservative job selection from a complete Git event diff (#5240).

Only documented independent docs/board trees narrow the native job matrix.
Unknown inputs or unavailable history select every job. Selection never changes
an individual job's acceptance rules or the Board07 operator switch.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

BOARD_JOBS: dict[str, tuple[str, ...]] = {
    "00-simple-led": ("board-00-end-to-end",),
    "01-voltage-divider": ("board-01-end-to-end",),
    "02-charlieplex-led": ("board-02-end-to-end",),
    "03-usb-joystick": ("board-03-end-to-end",),
    "04-stm32-devboard": ("board-04-end-to-end",),
    "05-bldc-motor-controller": ("board-05-routing-regression",),
    "06-diffpair-test": ("board-06-end-to-end", "diffpair-routing-regression"),
    "07-matchgroup-test": ("board-07-end-to-end", "matchgroup-routing-regression"),
    "08-precision-acquisition": (),
    "09-usbc-pd-power": (),
}
JOBS = ("test", "cpp-build-check", "kicad-cli-smoke") + tuple(
    job for jobs in BOARD_JOBS.values() for job in jobs
)


def select_jobs(paths: list[str] | None) -> dict[str, bool]:
    """Return the union of affected consumers; None means discovery failed."""
    selected: set[str] = set()
    if paths is None:
        return dict.fromkeys(JOBS, True)
    for path in paths:
        parts = path.split("/")
        if not path or path.startswith("/") or any(p in {"", ".", ".."} for p in parts):
            return dict.fromkeys(JOBS, True)
        if parts[0] == "docs" and len(parts) > 1:
            # Documentation/source-citation tests actually consume these files.
            selected.add("test")
        elif len(parts) > 2 and parts[0] == "boards" and parts[1] in BOARD_JOBS:
            # General tests consume recipe code and saved board fixtures too.
            selected.add("test")
            selected.update(BOARD_JOBS[parts[1]])
            if parts[2] == "output":
                # Round-trip smoke reads committed board schematics and the
                # Board00 routed PCB; keep their independent named gate.
                selected.add("kicad-cli-smoke")
        else:
            # Includes src, scripts, tests, native/build/dependency inputs,
            # workflow selection itself, shared board utilities and new boards.
            return dict.fromkeys(JOBS, True)
    return {job: job in selected for job in JOBS}


def _sha(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-fA-F]{40}", value):
        raise ValueError("event lacks a full commit SHA")
    if value == "0" * 40:
        raise ValueError("event has no previous commit")
    return value


def changed_paths(event: dict[str, Any], event_name: str, repo: Path) -> list[str]:
    """Use PR merge-base or complete push range, preserving both rename sides."""
    if event_name == "pull_request":
        pr = event["pull_request"]
        base, head = _sha(pr["base"]["sha"]), _sha(pr["head"]["sha"])
        base = (
            subprocess.check_output(
                ["git", "merge-base", base, head], cwd=repo, stderr=subprocess.PIPE
            )
            .decode("ascii")
            .strip()
        )
        _sha(base)
    elif event_name == "push":
        base, head = _sha(event["before"]), _sha(event["after"])
    else:
        raise ValueError(f"unsupported event: {event_name}")
    # No rename detection: a rename is a deletion plus addition, so both the
    # old and new consumer remain selected, including cross-board moves.
    raw = subprocess.check_output(
        ["git", "diff", "--name-only", "--no-renames", "-z", base, head, "--"],
        cwd=repo,
        stderr=subprocess.PIPE,
    )
    if raw and not raw.endswith(b"\0"):
        raise ValueError("incomplete NUL-delimited Git diff")
    return [os.fsdecode(path) for path in raw.split(b"\0") if path]


def plan(event_path: Path, event_name: str, repo: Path) -> dict[str, bool]:
    try:
        event = json.loads(event_path.read_text())
        paths = changed_paths(event, event_name, repo)
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        print(
            f"CI selection: full coverage; diff unavailable ({type(exc).__name__})", file=sys.stderr
        )
        paths = None
    return select_jobs(paths)


def main() -> None:
    result = plan(
        Path(os.environ["GITHUB_EVENT_PATH"]),
        os.environ.get("GITHUB_EVENT_NAME", ""),
        Path.cwd(),
    )
    with Path(os.environ["GITHUB_OUTPUT"]).open("a") as output:
        for job, selected in result.items():
            output.write(f"{job}={str(selected).lower()}\n")
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
