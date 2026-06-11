#!/usr/bin/env python3
"""Route chorus-test-revA with the pinned recipe + partial-net rescue (Issue #3474).

This is the canonical chorus-test-revA routing recipe runner for Phase
R2 of issue #3474.  It is two stages:

1. **Main pass** -- the pinned recipe, byte-identical to the one in
   ``tests/test_chorus_reach_floor_3237.py`` and the issue body
   (jlcpcb-tier1, cpp backend, seed 42, ``--timeout 1200``,
   auto-layers, auto-fix, placement feedback, ``PYTHONHASHSEED=0``).
2. **Rescue loop** (``kicad_tools.router.partial_rescue``) -- each net
   left partially routed (1/N-pad stranding, the #3470-class signature)
   or unrouted (budget-starved tail) is re-routed ALONE against the
   committed copper of every other net.  Stranded stubs are stripped
   first so they neither poison the rescue A* nor remain as
   overlapping-copper DRC liabilities.

The chorus fixture lives in the external chorus repo, surfaced through
the ``boards/external/chorus-test-revA`` symlink.  NOTE: that symlink
is relative (``../../../chorus/...``) and only resolves from the main
repository checkout, not from ``.loom/worktrees/`` -- pass ``--pcb``
explicitly when running from a worktree.

Usage::

    PYTHONHASHSEED=0 uv run python scripts/route_chorus.py \
        [--pcb PATH] [--output PATH] [--skip-main-pass] [--seed 42]

Exit code 0 when every signal net is fully routed (the
``feedback_manufacturable_means_100pct`` bar), 1 otherwise.  Either way
the final per-class report is printed for honest re-measurement.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from kicad_tools.router.partial_rescue import (  # noqa: E402
    RescueConfig,
    partially_connected_signal_nets,
    rescue_partial_nets,
)

DEFAULT_PCB = (
    REPO_ROOT
    / "boards"
    / "external"
    / "chorus-test-revA"
    / "kicad"
    / "chorus-test-revA_v21_stripped.kicad_pcb"
)

#: Pour-carried power nets (zones exist for all five in the v21
#: fixture: GNDD on F/B/In1, GNDA on In1, +3.3V/+5V/+3.3VA on In2).
#: Their connectivity is by zone fill; the trace-connectivity checker
#: does not credit it, so they are excluded from rescue targeting.
CHORUS_POUR_NETS = frozenset({"+3.3V", "+3.3VA", "+5V", "GNDA", "GNDD"})

#: The issue #3474 pinned recipe (apples-to-apples with Wave-9/10 and
#: the R1 re-measurement).  Keep in sync with
#: tests/test_chorus_reach_floor_3237.py::test_chorus_reach_v21_floor.
PINNED_RECIPE_FLAGS = [
    "--manufacturer",
    "jlcpcb-tier1",
    "--backend",
    "cpp",
    "--placement-feedback",
    "--placement-feedback-budget",
    "5",
    "--iterations",
    "50",
    "--auto-fix",
    "--auto-fix-passes",
    "2",
    "--auto-layers",
    "--timeout",
    "1200",
]


def run_main_pass(pcb: Path, output: Path, seed: int) -> int:
    """Run the pinned recipe; returns the subprocess exit code."""
    cmd = [
        sys.executable,
        "-m",
        "kicad_tools.cli",
        "route",
        str(pcb),
        "--output",
        str(output),
        *PINNED_RECIPE_FLAGS,
        "--seed",
        str(seed),
    ]
    env = {**os.environ, "PYTHONHASHSEED": "0"}
    print(f"Main pass: {' '.join(cmd)}", flush=True)
    return subprocess.run(cmd, env=env).returncode


def report(pcb: Path) -> tuple[int, int]:
    """Print the per-class connectivity + DRC report.

    Returns ``(unfinished_signal_nets, blocking_drc_errors)``.
    """
    partial = partially_connected_signal_nets(
        pcb, excluded_nets=CHORUS_POUR_NETS, include_unrouted=False
    )
    unrouted = sorted(
        set(
            partially_connected_signal_nets(
                pcb, excluded_nets=CHORUS_POUR_NETS, include_unrouted=True
            )
        )
        - set(partial)
    )

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "kicad_tools.cli",
            "check",
            str(pcb),
            "--mfr",
            "jlcpcb-tier1",
            "--format",
            "json",
        ],
        capture_output=True,
        text=True,
    )
    drc_errors = 0
    try:
        import json

        data = json.loads(result.stdout)
        for v in data.get("violations", data.get("errors", [])):
            rule = v.get("rule_id") or v.get("rule") or v.get("type")
            if rule == "connectivity":
                continue
            if v.get("severity") == "error":
                drc_errors += 1
    except (ValueError, KeyError):
        print("  WARNING: could not parse kct check output", flush=True)

    print("\n" + "=" * 60)
    print("Final chorus report")
    print("=" * 60)
    print(f"  Partially-routed signal nets: {len(partial)}")
    for n in partial:
        print(f"    - {n}")
    print(f"  Unrouted signal nets: {len(unrouted)}")
    for n in unrouted:
        print(f"    - {n}")
    print(f"  Non-connectivity DRC errors: {drc_errors}")
    return len(partial) + len(unrouted), drc_errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pcb", type=Path, default=DEFAULT_PCB)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("/tmp/chorus_routed_r2.kicad_pcb"),
    )
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument(
        "--skip-main-pass",
        action="store_true",
        help="Treat --output as an existing routed board; run rescue only.",
    )
    parser.add_argument(
        "--rescue-unrouted",
        action="store_true",
        default=True,
        help="Also rescue nets with no copper at all (default on).",
    )
    args = parser.parse_args()

    if not args.skip_main_pass:
        if not args.pcb.exists():
            print(f"ERROR: fixture not found: {args.pcb}", file=sys.stderr)
            return 1
        run_main_pass(args.pcb, args.output, args.seed)

    if not args.output.exists():
        print(f"ERROR: no routed board at {args.output}", file=sys.stderr)
        return 1

    config = RescueConfig(
        manufacturer="jlcpcb-tier1",
        backend="cpp",
        seed=args.seed,
        excluded_nets=CHORUS_POUR_NETS,
        micro_via_in_pad_fallback=True,
    )
    targets = partially_connected_signal_nets(
        args.output,
        manufacturer=config.manufacturer,
        excluded_nets=CHORUS_POUR_NETS,
        include_unrouted=args.rescue_unrouted,
    )
    rescue_partial_nets(args.output, config, nets=targets)

    unfinished, drc_errors = report(args.output)
    print(
        f"\n  Routed board: {args.output}"
        f"\n  Manufacturable bar: unfinished={unfinished}, "
        f"blocking DRC={drc_errors} (target 0/0)"
    )
    return 0 if unfinished == 0 and drc_errors == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
