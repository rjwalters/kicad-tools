#!/usr/bin/env python3
"""Routed-PCB DRC gate for CI (issue #2546).

Runs ``kct check <file> --mfr jlcpcb --errors-only --format json`` against
each PCB path passed on the command line, and compares the resulting error
count against a per-board tolerance allowlist (``.github/routed-drc-tolerance.yml``).

A file fails the gate if its actual error count exceeds the allowed value;
files not listed in the allowlist must report 0 errors. This implements the
"allowed minus epsilon" semantic: regressions (count going UP) are caught,
even on boards that are grandfathered in with non-zero counts.

Exit codes:
    0 -- All inputs within tolerance (job passes).
    1 -- Tool failure (allowlist parse error, kct check crash, etc.).
    2 -- One or more inputs exceed their tolerance (job fails).

GitHub-Actions annotations (``::error file=...::``) are emitted to stdout for
each offending file so the PR Files-changed view surfaces the failure inline.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml

DEFAULT_ALLOWLIST = Path(".github/routed-drc-tolerance.yml")


def load_allowlist(allowlist_path: Path) -> dict[str, int]:
    """Load and validate the per-board tolerance allowlist.

    Args:
        allowlist_path: Path to the YAML file. Missing file is treated as
            an empty allowlist (every board must report 0 errors).

    Returns:
        Mapping of repo-relative PCB path -> max allowed error count.

    Raises:
        ValueError: If the file exists but is malformed.
    """
    if not allowlist_path.exists():
        return {}

    try:
        data = yaml.safe_load(allowlist_path.read_text())
    except yaml.YAMLError as e:
        raise ValueError(f"Malformed allowlist YAML at {allowlist_path}: {e}") from e

    if data is None:
        return {}

    if not isinstance(data, dict):
        raise ValueError(
            f"Allowlist {allowlist_path} must be a YAML mapping at the top level, "
            f"got {type(data).__name__}"
        )

    tolerances = data.get("tolerances", {})
    if not isinstance(tolerances, dict):
        raise ValueError(
            f"Allowlist {allowlist_path} 'tolerances' field must be a mapping, "
            f"got {type(tolerances).__name__}"
        )

    result: dict[str, int] = {}
    for key, value in tolerances.items():
        if not isinstance(key, str):
            raise ValueError(
                f"Allowlist {allowlist_path}: key {key!r} must be a string path"
            )
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ValueError(
                f"Allowlist {allowlist_path}: value for {key!r} must be a "
                f"non-negative integer, got {value!r}"
            )
        result[key] = value

    return result


def count_errors(pcb_path: Path) -> int:
    """Run ``kct check`` and return the error count.

    Args:
        pcb_path: Path to a ``.kicad_pcb`` file.

    Returns:
        Number of errors reported (0 if the gate passes natively).

    Raises:
        RuntimeError: If kct check fails to run (exit code 1) or emits
            unparseable output.
    """
    cmd = [
        "uv",
        "run",
        "kct",
        "check",
        str(pcb_path),
        "--mfr",
        "jlcpcb",
        "--errors-only",
        "--format",
        "json",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)

    # Exit 1 = tool-level failure (file not found, parse error). Exit 0 = no
    # errors. Exit 2 = errors found. Both 0 and 2 produce valid JSON on stdout.
    if proc.returncode == 1:
        raise RuntimeError(
            f"kct check failed on {pcb_path} (exit 1). stderr:\n{proc.stderr.strip()}"
        )

    if proc.returncode not in (0, 2):
        raise RuntimeError(
            f"kct check returned unexpected exit code {proc.returncode} on "
            f"{pcb_path}. stderr:\n{proc.stderr.strip()}"
        )

    try:
        data: dict[str, Any] = json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        raise RuntimeError(
            f"kct check produced invalid JSON on {pcb_path}: {e}\n"
            f"stdout (first 500 chars):\n{proc.stdout[:500]}"
        ) from e

    summary = data.get("summary", {})
    errors = summary.get("errors")
    if not isinstance(errors, int):
        raise RuntimeError(
            f"kct check JSON missing summary.errors field for {pcb_path}: {data!r}"
        )
    return errors


def annotate_error(file: str, message: str) -> None:
    """Emit a GitHub-Actions ``::error file=...::`` annotation."""
    # The %0A escape is not strictly required for single-line messages; we
    # keep the message on one line so it surfaces cleanly in the Files-changed
    # view.
    print(f"::error file={file}::{message}", flush=True)


def check_file(pcb_path: Path, allowed: int) -> tuple[bool, str]:
    """Check a single PCB against its allowed error count.

    Returns:
        (passed, message) tuple. ``passed`` is True if errors <= allowed.
        ``message`` is a human-readable summary suitable for both stdout
        and GitHub annotation.
    """
    errors = count_errors(pcb_path)
    if errors <= allowed:
        if allowed == 0:
            msg = f"OK: {pcb_path} -- 0 errors (strict gate)."
        else:
            msg = (
                f"OK: {pcb_path} -- {errors} errors (allowlist max {allowed}; "
                f"reduce the allowlist value in .github/routed-drc-tolerance.yml "
                f"if this count drops further)."
            )
        return True, msg

    if allowed == 0:
        msg = (
            f"DRC errors detected by `kct check --mfr jlcpcb --errors-only`: "
            f"{errors} error(s). Boards NOT in .github/routed-drc-tolerance.yml "
            f"must report 0 errors. Either fix the routing or, if grandfathering "
            f"is justified, add an explicit allowlist entry with reviewer sign-off."
        )
    else:
        msg = (
            f"DRC regression: {errors} error(s) exceeds allowlist value "
            f"{allowed} in .github/routed-drc-tolerance.yml. Either fix the "
            f"new violations, or (if intentional) raise the allowlist value "
            f"with reviewer sign-off."
        )
    return False, msg


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="check_routed_drc",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "files",
        nargs="*",
        help="Paths to *_routed.kicad_pcb files to check. If empty, the gate "
        "is a no-op and exits 0.",
    )
    parser.add_argument(
        "--allowlist",
        default=str(DEFAULT_ALLOWLIST),
        help=f"Path to the tolerance allowlist YAML (default: {DEFAULT_ALLOWLIST}).",
    )
    args = parser.parse_args(argv)

    if not args.files:
        print("No routed PCBs to check -- gate is a no-op.")
        return 0

    try:
        allowlist = load_allowlist(Path(args.allowlist))
    except ValueError as e:
        print(f"::error::{e}", flush=True)
        return 1

    overall_failed = 0
    for raw in args.files:
        pcb_path = Path(raw)
        if not pcb_path.is_file():
            # GitHub Actions runs the gate after `git diff` has already
            # reported these files as modified; a file that no longer exists
            # was deleted in the PR (legitimate). Skip with a warning.
            print(
                f"::warning file={pcb_path}::file not found on disk "
                f"(deleted in PR?) -- skipping DRC check",
                flush=True,
            )
            continue

        # Use the repo-relative form for allowlist lookup. The CI workflow
        # passes paths in repo-relative form already; tolerate absolute paths
        # by stripping the cwd if it matches.
        lookup_key = str(pcb_path)
        try:
            rel = pcb_path.resolve().relative_to(Path.cwd())
            lookup_key = str(rel)
        except ValueError:
            # Path is outside cwd (unlikely in CI); fall back to the raw form.
            pass

        allowed = allowlist.get(lookup_key, 0)

        try:
            passed, message = check_file(pcb_path, allowed)
        except RuntimeError as e:
            annotate_error(str(pcb_path), f"kct check failed: {e}")
            overall_failed = 1
            continue

        if passed:
            print(message, flush=True)
        else:
            annotate_error(str(pcb_path), message)
            overall_failed = 2

    if overall_failed:
        print(
            f"\nGate failed (exit {overall_failed}). See ::error:: annotations "
            "above; offending files are also surfaced in the PR Files-changed "
            "view.",
            flush=True,
        )
    return overall_failed


if __name__ == "__main__":
    sys.exit(main())
