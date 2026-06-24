#!/usr/bin/env python3
"""Route chorus-test-revA with the pinned recipe + partial-net rescue (Issue #3474).

This is the canonical chorus-test-revA routing recipe runner for Phase
R2 of issue #3474.  It is three stages:

1. **Main pass** -- the R2 recipe (``R2_RECIPE_FLAGS``): pinned 4
   layers (NO escalation ladder -- one attempt with the full 1200s
   budget; the load-bearing change, 14-20/51 -> 33/51 measured),
   ``--deterministic-budget`` (issue #3877: iteration-budgeted per-net
   A* so the route is reproducible regardless of machine load --
   replaced the former wall-clock ``--per-net-timeout 60``),
   jlcpcb-tier1, cpp backend, seed 42, auto-fix, placement feedback,
   ``PYTHONHASHSEED=0``.
2. **Completion passes** (``complete_unfinished_nets``) -- every net
   left partially routed (1/N-pad stranding, the #3470-class signature)
   or unrouted (budget-starved tail) is stripped of stranded stubs and
   re-routed TOGETHER against the strict nets' preserved copper, so the
   unfinished cohort can still negotiate among itself.  Repeats while
   the unfinished count drops (max 3 passes).
3. **Per-net rescue** (``rescue_partial_nets``) -- only when <= 10
   residual nets remain; each is routed alone against all committed
   copper.  Measured 2026-06-11: running this stage on a large cohort
   is ineffective on chorus (0/6 -- a solo net cannot negotiate with
   non-rippable preserved copper), hence the completion passes first.

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
    complete_unfinished_nets,
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
#: Retained for reference/re-measurement; the R2 recipe below routes
#: measurably better and is what this script runs.
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

#: The R2 recipe (issue #3474, measured 2026-06-11).  Two changes vs
#: the pinned recipe, both budget-shape:
#:
#: 1. ``--layers 4 --no-auto-layers`` -- ONE routing attempt with the
#:    full wall budget.  The auto-layers escalation ladder ran 5-6
#:    rungs of ~180-200s each, so every rung re-routed the same
#:    head-of-queue clock nets from scratch and died around net 12-25
#:    of 51.  The single pinned-layers attempt walks the entire 51-net
#:    queue (detailed routing reached 100% of nets for the first time
#:    on this board).
#: 2. ``--deterministic-budget`` (issue #3877) -- REPLACES the former
#:    ``--per-net-timeout 60`` wall-clock cutoff.  ``--per-net-timeout``
#:    made the per-net A* search load-dependent: on a loaded dev box or
#:    CI runner the wall-clock budget fired mid-search and the net landed
#:    LESS copper than on an idle machine, so chorus measured anywhere
#:    from 8/51 to 31/51 depending on machine load.  ``--deterministic-budget``
#:    (#3538) swaps that wall-clock cutoff for a fixed C++ A*
#:    node-expansion ITERATION backstop (12M expansions), so each per-net
#:    search either finds a path or aborts after the SAME amount of work on
#:    EVERY machine.  Chorus now routes the same copper run-to-run
#:    regardless of load, which is what makes the M2/M3 measurement (#3873)
#:    reliable on any host.  The outer ``--timeout 1200`` is retained ONLY
#:    as a safety backstop so a pathological net cannot run unbounded; it
#:    must not be the binding constraint (size it generously).
#:
#: Measured strict reach (cpp, seed 42, PYTHONHASHSEED=0, t=1200):
#:   pinned recipe (5-rung ladder):     14-20/51 across runs
#:   auto-layers --starting/max 4:      22/51 (2 rungs)
#:   --layers 4, 1 rung, --per-net-timeout 60: 33/51 (65%, May-10 parity)
#:   THIS RECIPE (--deterministic-budget):     reproducible run-to-run
R2_RECIPE_FLAGS = [
    "--manufacturer",
    "jlcpcb-tier1",
    "--backend",
    "cpp",
    "--layers",
    "4",
    "--no-auto-layers",
    "--micro-via-in-pad-fallback",
    "--deterministic-budget",
    # Issue #3881: tuned per-net iteration cap.  --deterministic-budget alone
    # pins the C++ A* per-net cap to the 12M MEMORY backstop, which is
    # effectively unbounded per-net: one hard net (e.g. I2S_BCLK) burned 280s
    # of the 1200s budget and geometric-failure nets fell through to the slow
    # Python A*, so only ~14 of 51 nets were attempted (chorus 13/51 vs the old
    # wall-clock recipe's 31/51).  A 1M-expansion per-net cap bounds each net to
    # a fair iteration slice (load-independent -> still deterministic) so hard
    # nets give up fast and more nets get a turn -- recovering throughput WHILE
    # staying reproducible.  (--deterministic-budget defaults this same value;
    # set explicitly here to make the recipe self-documenting.)
    "--per-net-iterations",
    "1000000",
    "--placement-feedback",
    "--placement-feedback-budget",
    "5",
    "--iterations",
    "50",
    "--auto-fix",
    "--auto-fix-passes",
    "2",
    "--timeout",
    "1200",
]


def run_main_pass(pcb: Path, output: Path, seed: int) -> int:
    """Run the R2 recipe; returns the subprocess exit code."""
    cmd = [
        sys.executable,
        "-m",
        "kicad_tools.cli",
        "route",
        str(pcb),
        "--output",
        str(output),
        *R2_RECIPE_FLAGS,
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
    parser.add_argument(
        "--keep-stubs",
        action="store_true",
        help=(
            "Keep stranded partial copper in the output instead of "
            "pruning it (useful for debugging where partial routes end)."
        ),
    )
    parser.add_argument(
        "--joint-region-resolve",
        action="store_true",
        help=(
            "Issue #3864 (M2): enable the JOINT region re-solve in the "
            "completion stage.  Sets KCT_JOINT_REGION_RESOLVE=1 in the "
            "environment so each completion-pass `kct route` subprocess "
            "re-solves a ripped congested pocket JOINTLY (a bounded inner "
            "negotiated loop with a net-positive rollback guard) instead "
            "of the legacy sequential one-at-a-time reroute.  Off by "
            "default; commits only on a strict-count increase so it can "
            "never regress the board."
        ),
    )
    parser.add_argument(
        "--placement-nudge",
        action="store_true",
        help=(
            "Issue #3865 (M3): after the completion passes, classify the "
            "remaining stuck nets (M1) and nudge the parts owning "
            "PLACEMENT_BOUND pads by a bounded, board-outline-aware "
            "displacement (default <=1.5mm), then re-route and accept ONLY "
            "if the strict signal-net count strictly increases and DRC does "
            "not worsen (else roll back byte-for-byte).  Off by default; "
            "moves parts on the board so it is the riskiest stage, fully "
            "net-positive-guarded.  Respects locked parts and the J2/J4 "
            "mechanical connectors."
        ),
    )
    args = parser.parse_args()

    if args.joint_region_resolve:
        # Inherited by every completion-pass subprocess (they run with the
        # parent environment).  See negotiated.region_resolve / core.py.
        os.environ["KCT_JOINT_REGION_RESOLVE"] = "1"
        print("Joint region re-solve ENABLED (KCT_JOINT_REGION_RESOLVE=1)")

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
        # Issue #3877: the completion/rescue passes must be as
        # load-independent as the main pass, otherwise chorus's final
        # reach still varies run-to-run.  This drops the wall-clock
        # per-net cutoff on every rescue subprocess in favour of the
        # fixed iteration backstop (#3538).
        deterministic_budget=True,
    )

    # Stage 2: batch completion passes.  All unfinished nets route
    # TOGETHER against the strict nets' preserved copper, so they can
    # still negotiate among themselves.  Measured (2026-06-11): the
    # single-net rescue loop alone lands 0/6 on chorus because a solo
    # net cannot negotiate with non-rippable preserved copper.
    complete_unfinished_nets(args.output, config, max_passes=3)

    # Stage 3: per-net rescue for a small residual only.  Worth one
    # 300s stage per net when few remain; pointless (and slow) for a
    # large cohort -- the completion pass is the bulk mechanism.
    residual = partially_connected_signal_nets(
        args.output,
        manufacturer=config.manufacturer,
        excluded_nets=CHORUS_POUR_NETS,
        include_unrouted=args.rescue_unrouted,
    )
    if 0 < len(residual) <= 10:
        rescue_partial_nets(args.output, config, nets=residual)

    # Stage 3.5 (issue #3865, M3): congestion/escape-driven placement nudge.
    # Off by default; only the placement-bound nets that NO routing change can
    # fix (chorus U5 codec/QFN analog cluster + the no-rippable control nets)
    # are addressed here, by MOVING the owning part a bounded amount.  The
    # stage is net-positive-guarded: it accepts the nudged + re-routed board
    # only on a strict-count increase with no DRC regression, else rolls back
    # byte-for-byte.  Runs before the stub-prune so a successful nudge's new
    # copper is kept and a failed one leaves the board untouched.
    if args.placement_nudge:
        from kicad_tools.router.placement_nudge import (
            NudgeConfig,
            nudge_placement_bound_nets,
        )

        print("\nPlacement nudge ENABLED (issue #3865, M3)")
        nudge_result = nudge_placement_bound_nets(
            args.output,
            NudgeConfig(rescue=config),
        )
        print(nudge_result.summary())

    # Stage 4: prune stranded stubs.  Whatever is still unfinished
    # contributes zero to strict reach but its stranded copper is a DRC
    # liability (#3470 defect 2: overlapping stub copper).  Stripping is
    # loss-free for the reach metric and measurably reduces blocking
    # DRC (2026-06-11: 47 -> 24 non-connectivity errors on the 22/51
    # board).  Issue #3474 R2 AC: "zero overlapping stranded-stub
    # copper in partial outputs".
    if not args.keep_stubs:
        unfinished = partially_connected_signal_nets(
            args.output,
            manufacturer=config.manufacturer,
            excluded_nets=CHORUS_POUR_NETS,
            include_unrouted=True,
        )
        if unfinished:
            from kicad_tools.router.partial_rescue import strip_net_copper

            removed = strip_net_copper(args.output, unfinished)
            print(
                f"\nPruned {removed} stranded copper block(s) from "
                f"{len(unfinished)} unfinished net(s) (issue #3470 stub hygiene)"
            )

    unfinished, drc_errors = report(args.output)
    print(
        f"\n  Routed board: {args.output}"
        f"\n  Manufacturable bar: unfinished={unfinished}, "
        f"blocking DRC={drc_errors} (target 0/0)"
    )
    return 0 if unfinished == 0 and drc_errors == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
