#!/usr/bin/env python3
"""Board-05 blocking-net baseline gate for CI (issue #3822).

Board-05 (BLDC motor controller) is the only demo board whose routing
changes cannot be validated on the local macOS development host: an
unmodified local regen of the recipe reaches fewer nets than the Linux CI
router on the identical recipe/seed/backend (host-vs-CI reach divergence,
#3822). See the recipe note in
``boards/05-bldc-motor-controller/design.py`` (~lines 2870-2871).

Phase 5 lowered the fresh-regen floor from 9-10 to 6; threshold is now 7
----------------------------------------------------------------------
The **committed** on-disk artifact
(``boards/05-bldc-motor-controller/output/bldc_controller_routed.kicad_pcb``)
is hand-verified at **0 blocking** signal nets (PR #4003, DRC-clean and
LVS-clean). That gold artifact is NOT reproducible from a fully-unattended
regen yet. A fresh full re-route INSIDE CI (kicad/kicad:10.0, timeout
raised to 90 min so it runs to completion) measured
**blocking_incomplete_count = 6** on commit 2de4089a -- with the Phase-5
Kelvin-star rooting + batch-completion + rescue-revival wiring active
(PR #4527 / #4532; CI run 30666373355). The residual blocking cohort is
``ISENSE_A-, ISENSE_B-, ISENSE_C-, PHASE_A, PHASE_B, PHASE_C``. That is a
large win over the pre-Phase-5 floor of 9-10 (historically nondeterministic
run-to-run), but still short of the committed artifact's 0.

``--max-blocking`` therefore defaults to **7** -- one net of safety margin
above the Phase-5-measured CI ceiling of 6, the same "+1 above the observed
ceiling" methodology the previous loose bound of 11 (10 + 1) used. This
locks in Phase 5's gain (11 -> 7) while tolerating board-05's historical
run-to-run nondeterminism, so the gate stops flaking while remaining a hard
assertion (no ``continue-on-error``) that still catches GROSS regressions
(anything > 7 blocking).

This bound is still not the end state. As #4548 (re-add
``--escape-corridor-reservation`` after CI blocking-count validation, which
targets the PHASE_A/B/C south-escape congestion) and a dedicated follow-up
for the ISENSE ``-``-leg residuals land, tighten ``--max-blocking`` further
toward 0, locking in each gain.

Validation path for board-05 routing changes
---------------------------------------------
Regenerate board-05 **in the CI environment** (the ``kicad/kicad:10.0``
container) and let the board-05 CI job assert
``blocking_incomplete_count <= 7`` via this gate. A LOCAL run of this gate
after a full host regen reports more blocking nets -- that is the documented
host-vs-CI reach divergence (#3822), NOT a defect in this script or the job.
The authoritative verdict is the PR's own CI run.

This gate loads the routed board-05 PCB and asserts that the number of
blocking incomplete nets does not exceed ``--max-blocking`` (default 7).
It reuses :class:`kicad_tools.analysis.net_status.NetStatusAnalyzer` --
whose ``blocking_incomplete_count`` "Mirrors
``scripts/ci/check_routed_drc.py:_count_blocking_errors``", i.e. it applies
the same advisory/plane-residual filtering the DRC gate uses -- rather than
re-deriving the metric.

The ``--max-blocking`` threshold is a CLI argument (default 7, one net above
the Phase-5-measured CI ceiling of 6) so future PRs (#4548 escape-corridor
reservation, the ISENSE ``-``-leg follow-up) can tighten it toward 0 as
they land routing improvements, locking in each gain.

Exit codes (mirrors ``scripts/ci/check_routed_drc.py``):
    0 -- Blocking count within threshold (job passes).
    1 -- Tool failure (file missing, PCB parse error, etc.).
    2 -- Blocking count exceeds threshold (regression -- job fails).

GitHub-Actions annotations (``::error::``) are emitted to stdout so the
PR Files-changed view surfaces a regression inline.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Phase-5 bound (issue #4479). A fresh full re-route of board-05
# (kicad/kicad:10.0, 90-min timeout) measured blocking_incomplete_count = 6
# on commit 2de4089a, with Phase 5's Kelvin-star rooting + batch-completion +
# rescue-revival wiring active (PR #4527 / #4532; CI run 30666373355). The
# residual cohort is ISENSE_A-, ISENSE_B-, ISENSE_C-, PHASE_A, PHASE_B,
# PHASE_C. That is a large win over the pre-Phase-5 floor of 9-10 (which was
# nondeterministic run-to-run), though still short of the committed artifact's
# 0 blocking (hand-verified, PR #4003). 7 is one net of safety margin above
# the measured CI ceiling of 6 -- the same "+1 above the observed ceiling"
# methodology the previous loose bound of 11 (10 + 1) used -- so the gate
# stops flaking while still catching gross regressions (> 7). Tighten further
# toward 0 as #4548 (escape-corridor reservation) and the ISENSE -leg
# follow-up land.
#
# History (why the bound was 11 before Phase 5):
# Issue #3836: the pre-Phase-5 wall-clock re-route was NONDETERMINISTIC at 9-10
# blocking (observed main=9, PR #3835 branch=10 twice, identical router code);
# a default of 9 sat exactly on the 9-vs-10 boundary and intermittently
# red-lighted unrelated PRs, so it was loosened to 11 (10 + 1).
# Issue #3887 (SUPERSEDED by #3894): moved board-05's main pass + rescue loop
# from the wall-clock --per-net-timeout cutoff to a fixed per-net ITERATION
# budget, claiming determinism. That claim did not hold up on CI.
# Issue #3894: REVERTED #3887's per-net ITERATION budget back to the wall-clock
# recipe (--per-net-timeout 60 / --timeout 900); #3887's budget did LESS total
# routing work and REGRESSED the count to 12-15. Per #3822 the CI-measured
# blocking_incomplete_count is authoritative, so this bound is only tightened
# on CI-measured evidence (as it is here for Phase 5), NOT a locally-guessed
# number.
DEFAULT_MAX_BLOCKING = 7


def annotate_error(file: str, message: str) -> None:
    """Emit a GitHub-Actions ``::error file=...::`` annotation."""
    print(f"::error file={file}::{message}", flush=True)


def count_blocking(pcb_path: Path) -> tuple[int, list[str]]:
    """Load the routed PCB and return its blocking-incomplete net count.

    Args:
        pcb_path: Path to a routed ``.kicad_pcb`` file.

    Returns:
        Tuple of ``(blocking_count, blocking_net_names)`` where
        ``blocking_count`` is ``NetStatusResult.blocking_incomplete_count``
        (advisory/plane residuals already filtered out) and
        ``blocking_net_names`` is the sorted list of offending net names for
        diagnostic output.

    Raises:
        RuntimeError: If the PCB cannot be loaded or analyzed.
    """
    # Imported lazily so ``--help`` works even outside the ``uv run``
    # environment where ``kicad_tools`` is importable.
    from kicad_tools.analysis.net_status import NetStatusAnalyzer

    try:
        result = NetStatusAnalyzer(str(pcb_path)).analyze()
    except Exception as e:  # noqa: BLE001 -- surface any load/parse failure as a tool error
        raise RuntimeError(f"failed to analyze {pcb_path}: {e}") from e

    blocking_names = sorted(n.net_name for n in result.blocking_incomplete)
    return result.blocking_incomplete_count, blocking_names


def check_pcb(pcb_path: Path, max_blocking: int) -> tuple[int, str]:
    """Compare a routed PCB's blocking-net count to the threshold.

    Args:
        pcb_path: Path to the routed ``.kicad_pcb`` file.
        max_blocking: Maximum allowed ``blocking_incomplete_count``.

    Returns:
        ``(exit_code, message)``. ``exit_code`` is 0 (pass), 1 (tool
        failure), or 2 (regression). ``message`` is a human-readable
        summary suitable for stdout and GitHub annotations.
    """
    if not pcb_path.is_file():
        return 1, f"routed PCB not found: {pcb_path}"

    try:
        count, blocking_names = count_blocking(pcb_path)
    except RuntimeError as e:
        return 1, str(e)

    # Always surface the measured count + net names on plain stdout, BEFORE
    # the pass/fail verdict, so CI logs record the real number on every path
    # (pass, or regression with exit 2).  This is the authoritative figure CI
    # reaches on a fresh full re-route -- print it unconditionally so it is
    # readable even when the gate ultimately fails.
    names_for_log = ", ".join(blocking_names) if blocking_names else "(none)"
    print(
        f"MEASURED blocking_incomplete_count = {count} "
        f"(threshold <= {max_blocking})\n"
        f"  blocking nets: {names_for_log}",
        flush=True,
    )

    names_suffix = f" [blocking nets: {', '.join(blocking_names)}]" if blocking_names else ""

    if count <= max_blocking:
        return (
            0,
            f"OK: {pcb_path} -- {count} blocking incomplete net(s) "
            f"(threshold <= {max_blocking}).{names_suffix}",
        )

    return (
        2,
        f"Board-05 blocking-net regression: {count} blocking incomplete net(s) "
        f"exceeds --max-blocking={max_blocking}. The default of 7 is one net "
        f"above the Phase-5-measured CI ceiling of 6 (issue #4479): board-05's "
        f"CI re-route reaches 6 blocking with Phase 5 active, while the committed "
        f"artifact is 0 blocking (PR #4003). The gate still catches GROSS "
        f"regressions (> 7). Tighten this further toward 0 as #4548 (escape- "
        f"corridor reservation) and the ISENSE -leg follow-up land. Either fix "
        f"the routing or, if the floor truly moved, adjust --max-blocking in the "
        f"CI job with reviewer sign-off. NOTE: a LOCAL macOS run routes board-05 "
        f"to more blocking nets; this gate is CI-validated only (#3822)."
        f"{names_suffix}",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="check_board_05_blocking",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "pcb",
        help="Path to the routed board-05 PCB (bldc_controller_routed.kicad_pcb).",
    )
    parser.add_argument(
        "--max-blocking",
        type=int,
        default=DEFAULT_MAX_BLOCKING,
        help=(
            "Maximum allowed blocking_incomplete_count before the gate fails "
            f"(default: {DEFAULT_MAX_BLOCKING}, one net above the Phase-5- "
            "measured CI ceiling of 6; board-05's CI re-route reaches 6 blocking "
            "with Phase 5 active and diverges from the committed 0-blocking "
            "artifact -- issue #4479). Tighten this further toward 0 as #4548 "
            "and the ISENSE -leg follow-up land."
        ),
    )
    args = parser.parse_args(argv)

    if args.max_blocking < 0:
        print("::error::--max-blocking must be a non-negative integer", flush=True)
        return 1

    pcb_path = Path(args.pcb)
    exit_code, message = check_pcb(pcb_path, args.max_blocking)

    if exit_code == 0:
        print(message, flush=True)
    else:
        annotate_error(str(pcb_path), message)
        print(
            f"\nGate failed (exit {exit_code}). See ::error:: annotation above.",
            flush=True,
        )

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
