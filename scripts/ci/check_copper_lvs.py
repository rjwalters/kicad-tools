#!/usr/bin/env python3
"""Independent copper-LVS re-check asserter for CI (issue #3840).

This is the *parser/asserter* side of an out-of-process copper-LVS gate.
The CI job first emits a fresh copper-LVS verdict for a regenerated board
by running the subprocess entrypoint added in #3838::

    uv run python -m kicad_tools.lvs.copper_lvs <schematic> <routed_pcb> > out.json

That entrypoint loads both files in a clean interpreter (no in-process
recipe state), runs :func:`compare_copper_netlist`, and prints a single
JSON object of the shape produced by
:func:`kicad_tools.lvs.copper_lvs.result_to_json`::

    {"clean": true, "mismatches": []}

This script reads that JSON and asserts ``clean == true``, emitting a
GitHub-Actions ``::error::`` annotation and exiting 2 on any mismatch.

Why this exists (defense-in-depth, issue #3840):

The board-03/06/07 ``--lvs-only`` jobs gate on ``output/lvs.json``, which
is written by the same ``generate_design.py`` recipe process that routed
the board.  Since #3838 the recipe's ``lvs.json`` copper verdict is already
authoritative against the on-disk bytes (``write_lvs_report`` re-derives it
in a fresh subprocess and fails closed on divergence).  But the CI gate
still *trusts the recipe to have run that fresh check* -- a future recipe
change that drops ``fresh_copper_check=True`` / ``require_clean=True`` or
overwrites ``lvs.json`` would silently pass.  Running
``python -m kicad_tools.lvs.copper_lvs`` from a process the CI job *itself*
controls, on the regenerated ``*_routed.kicad_pcb``, and asserting the
result here removes that single point of trust: this asserter never reads
``lvs.json``.

Sibling of ``scripts/ci/check_board_00_e2e.py`` and
``scripts/ci/check_routed_drc.py`` -- same exit-code convention, same
``::error::`` annotation pattern, deliberately small so the gate's contract
stays auditable and unit-testable.

Exit codes:
    0 -- The copper-LVS result is clean (no shorts / no opens).
    1 -- Tool/usage error (missing arg, file unreadable, JSON parse
         failure, missing ``clean`` key) -- distinct from "the gate caught
         a real regression".
    2 -- The copper-LVS result reports ``clean: false`` (the gate caught a
         short or open).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


def _err(msg: str, file: str | Path | None = None) -> None:
    """Emit a GitHub Actions ``::error::`` annotation to stdout.

    The ``file=`` parameter surfaces the failure inline on the PR
    Files-changed view when the path is tracked by git; for tmp paths
    the annotation still shows in the job log with the field surfaced
    for triage.
    """
    if file is not None:
        print(f"::error file={file}::{msg}")
    else:
        print(f"::error::{msg}")


def _summarize_mismatches(mismatches: list[dict[str, Any]]) -> str:
    """Render a short human-readable summary of copper-LVS mismatches.

    Mirrors the field names produced by
    :func:`kicad_tools.lvs.copper_lvs.result_to_json`
    (``kind``/``net_a``/``net_b``/``pad_a``/``pad_b``).
    """
    shorts = sum(1 for m in mismatches if m.get("kind") == "short")
    opens = sum(1 for m in mismatches if m.get("kind") == "open")
    first = mismatches[0] if mismatches else None
    first_desc = ""
    if isinstance(first, dict):
        kind = first.get("kind", "?")
        net_a = first.get("net_a", "?")
        net_b = first.get("net_b", "?")
        pad_a = first.get("pad_a", "?")
        pad_b = first.get("pad_b", "?")
        first_desc = f"; first: {kind} {net_a}({pad_a}) <-> {net_b}({pad_b})"
    return f"{len(mismatches)} mismatch(es) ({shorts} short, {opens} open){first_desc}"


def assert_clean(payload: dict[str, Any]) -> str | None:
    """Assert a copper-LVS JSON payload reports ``clean: true``.

    Args:
        payload: Parsed JSON object as emitted by
            ``python -m kicad_tools.lvs.copper_lvs``.  Must contain a
            ``clean`` boolean; ``mismatches`` (a list) is optional.

    Returns:
        ``None`` on pass (clean).  A human-readable error message on a
        dirty result (caller maps to exit 2).

    Raises:
        KeyError: If ``clean`` is absent (caller maps to exit 1 -- a
            malformed payload, not a caught regression).
    """
    clean = payload["clean"]
    if clean:
        return None
    mismatches = payload.get("mismatches", [])
    if not isinstance(mismatches, list):
        mismatches = []
    return f"copper-LVS reports clean=false with {_summarize_mismatches(mismatches)}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="check_copper_lvs.py",
        description=(
            "Assert a fresh out-of-process copper-LVS result is clean. "
            "Reads the JSON emitted by "
            "'python -m kicad_tools.lvs.copper_lvs <sch> <routed_pcb>'. "
            "Use '/dev/stdin' (or '-') to read the result from a pipe."
        ),
    )
    parser.add_argument(
        "result_json",
        help=(
            "Path to the JSON file emitted by python -m kicad_tools.lvs.copper_lvs "
            "(use '-' or '/dev/stdin' to read from stdin)."
        ),
    )
    args = parser.parse_args(argv)

    raw_path = args.result_json
    try:
        if raw_path in ("-", "/dev/stdin"):
            text = sys.stdin.read()
            display = "<stdin>"
        else:
            path = Path(raw_path)
            if not path.is_file():
                _err(f"result JSON does not exist or is not a file: {path}")
                return 1
            text = path.read_text()
            display = str(path)
    except OSError as e:
        _err(f"could not read result JSON {raw_path}: {e}")
        return 1

    try:
        payload = json.loads(text)
    except json.JSONDecodeError as e:
        _err(f"could not parse copper-LVS JSON from {display}: {e}")
        return 1

    if not isinstance(payload, dict):
        _err(f"copper-LVS JSON from {display} is not a JSON object: {type(payload).__name__}")
        return 1

    try:
        dirty_msg = assert_clean(payload)
    except KeyError:
        _err(f"copper-LVS JSON from {display} missing required 'clean' key: {payload!r}")
        return 1

    if dirty_msg is not None:
        _err(dirty_msg, file=display)
        print(
            "\nIndependent copper-LVS re-check FAILED (exit 2). This re-check is run "
            "by the CI job itself (not the recipe) on the regenerated routed PCB, so "
            "it catches a copper short/open even if the recipe mis-wrote lvs.json.",
            flush=True,
        )
        return 2

    print("[ok] copper-LVS clean (independent out-of-process re-check)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
