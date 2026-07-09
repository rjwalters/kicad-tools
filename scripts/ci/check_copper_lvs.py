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


def assert_vacuous(payload: dict[str, Any]) -> str | None:
    """Assert a copper-LVS payload is the *vacuity-guard* verdict (#4005).

    Used for boards whose fixture schematic is deliberately unwired (board
    06): the honest expectation is that the comparator binds zero pins and
    reports ``clean: false`` with exactly the synthetic ``kind="vacuous"``
    mismatch — no shorts/opens are detectable, and a ``clean: true`` here
    would mean the vacuity guard regressed (the zero-evidence pass this
    gate exists to prevent).  A short/open or a genuinely clean result also
    fails: either means the schematic gained wired nets and the CI job must
    be upgraded to a real ``clean`` assertion.

    Returns:
        ``None`` on pass (vacuous verdict as expected).  A human-readable
        error message otherwise (caller maps to exit 2).

    Raises:
        KeyError: If ``clean`` is absent (caller maps to exit 1).
    """
    clean = payload["clean"]
    mismatches = payload.get("mismatches", [])
    if not isinstance(mismatches, list):
        mismatches = []
    kinds = sorted({m.get("kind") for m in mismatches if isinstance(m, dict)})
    if clean:
        return (
            "expected a VACUOUS copper-LVS verdict (clean=false, kind='vacuous') "
            "but got clean=true -- either the vacuity guard regressed, or the "
            "schematic is now wired and this CI gate should assert clean instead"
        )
    if kinds != ["vacuous"]:
        return (
            "expected a VACUOUS copper-LVS verdict (only kind='vacuous') but got "
            f"mismatch kinds {kinds}: {_summarize_mismatches(mismatches)} -- the "
            "schematic appears to bind pins now; upgrade this CI gate to a real "
            "clean assertion"
        )
    return None


def assert_known_opens(payload: dict[str, Any], expected_nets: set[str]) -> str | None:
    """Assert a copper-LVS payload reports EXACTLY the expected opens (#4012).

    Used for boards that route PARTIAL by design with a *wired* fixture
    schematic (board 07: 5 seed-invariant unroutable nets, #3438).  The
    honest expectation is ``clean: false`` with only ``kind="open"``
    mismatches whose net names are exactly ``expected_nets`` — nothing
    more (a NEW open/short is a regression), nothing less (an expected
    open disappearing means the router improved and the expectation, or
    the gate, must be updated), and never the vacuity verdict (the
    schematic regressed to unwired).

    Returns:
        ``None`` on pass.  A human-readable error message otherwise
        (caller maps to exit 2).

    Raises:
        KeyError: If ``clean`` is absent (caller maps to exit 1).
    """
    clean = payload["clean"]
    mismatches = payload.get("mismatches", [])
    if not isinstance(mismatches, list):
        mismatches = []
    kinds = sorted({m.get("kind") for m in mismatches if isinstance(m, dict)})
    if clean:
        return (
            f"expected copper-LVS opens on {sorted(expected_nets)} (#3438) but got "
            "clean=true -- the previously-unroutable nets appear routed now; "
            "graduate this CI gate to a plain clean assertion"
        )
    if "vacuous" in kinds:
        return (
            "expected named copper-LVS opens but got the vacuity-guard verdict "
            "(kind='vacuous') -- the schematic regressed to unwired (binds 0 pins)"
        )
    if kinds != ["open"]:
        return (
            f"expected ONLY kind='open' mismatches but got kinds {kinds}: "
            f"{_summarize_mismatches(mismatches)} -- a copper short is a hard "
            "regression regardless of the known-opens allowance"
        )
    got_nets = sorted({m.get("net_a", "?") for m in mismatches if isinstance(m, dict)})
    if set(got_nets) != expected_nets:
        unexpected = sorted(set(got_nets) - expected_nets)
        missing = sorted(expected_nets - set(got_nets))
        return (
            f"copper-LVS opens {got_nets} != expected {sorted(expected_nets)} "
            f"(unexpected: {unexpected or 'none'}; no-longer-open: {missing or 'none'}) "
            f"-- {_summarize_mismatches(mismatches)}"
        )
    return None


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
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--expect-vacuous",
        action="store_true",
        help=(
            "Invert the contract for deliberately-unwired fixture schematics: "
            "assert the result is the vacuity-guard verdict "
            "(clean=false with only kind='vacuous' mismatches, #4005 review). "
            "Fails on clean=true (guard regression) AND on real shorts/opens "
            "(schematic gained nets; upgrade the gate)."
        ),
    )
    mode_group.add_argument(
        "--expect-opens",
        metavar="NET[,NET...]",
        help=(
            "Known-opens contract for wired-schematic boards that route "
            "PARTIAL by design (board 07, #3438/#4012): assert clean=false "
            "with ONLY kind='open' mismatches whose net names are exactly "
            "this comma-separated set.  Fails on clean=true (nets became "
            "routable; upgrade the gate), on any short, on the vacuous "
            "verdict, and on any open outside the set."
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

    expected_open_nets: set[str] = set()
    if args.expect_opens:
        expected_open_nets = {n.strip() for n in args.expect_opens.split(",") if n.strip()}
        if not expected_open_nets:
            _err("--expect-opens was given an empty net list")
            return 1

    try:
        if args.expect_vacuous:
            dirty_msg = assert_vacuous(payload)
        elif expected_open_nets:
            dirty_msg = assert_known_opens(payload, expected_open_nets)
        else:
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

    if args.expect_vacuous:
        print(
            "[ok] copper-LVS vacuity guard fired as expected "
            "(clean=false, kind='vacuous'; unwired fixture schematic)"
        )
    elif expected_open_nets:
        print(
            "[ok] copper-LVS reports exactly the expected known opens "
            f"({', '.join(sorted(expected_open_nets))}) and nothing else"
        )
    else:
        print("[ok] copper-LVS clean (independent out-of-process re-check)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
