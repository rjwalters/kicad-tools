#!/usr/bin/env python3
"""Board-05 blocking-net baseline gate for CI (issue #3822).

Board-05 (BLDC motor controller) is the only demo board whose routing
changes cannot be validated on the local macOS development host: the
committed CI-generated artifact reaches **7 blocking signal nets**, but an
unmodified local regen of the same recipe/seed/backend reaches ~11 (the
macOS host router reaches fewer nets than the Linux CI router on the
identical recipe). See the recipe note in
``boards/05-bldc-motor-controller/design.py`` (~lines 2870-2871).

CI re-route is NONDETERMINISTIC (9-10 blocking); threshold is a TEMPORARY loose bound of 11
-------------------------------------------------------------------------------------------
The committed on-disk artifact is 7 blocking, but that 7-blocking board is
NOT reproducible from the current recipe -- a fresh full re-route INSIDE
CI (kicad/kicad:10.0, timeout raised to 90 min so it runs to completion)
is ALSO nondeterministic at its floor: it measures
**blocking_incomplete_count = 9 OR 10 depending on the run** (observed:
main re-routes to 9, the PR #3835 branch -- whose router code is identical
to main -- re-routed to 10 on two consecutive CI runs). The blocking nets
hover around: ISENSE_A+, ISENSE_A-, ISENSE_B+, ISENSE_B-, ISENSE_C-,
PHASE_A, PHASE_B, PHASE_C, PWM_BH (+ HALL_A intermittently). The 7 -> 9/10
gap between the committed artifact and a fresh CI re-route is the board-05
routing reproducibility regression tracked in **#3775 / #3766 / #3829**.

``--max-blocking`` therefore defaults to **11** -- a TEMPORARY loose bound
chosen to stop CI flakiness. The previous default of 9 sat EXACTLY on the
nondeterministic 9-vs-10 boundary, so a coin-flip re-route red-lighted PRs
that never touched the router (issue #3836, observed on PR #3835). 11 is a
safe margin above the observed CI ceiling of 10, so the gate stops flaking
while remaining a hard assertion (no ``continue-on-error``) that still
catches GROSS regressions (anything > 11 blocking).

This is a deliberately LOOSE, temporary bound -- not the end state. The
proper fix is to make the board-05 re-route DETERMINISTIC (pin seed /
iteration order) and then tighten ``--max-blocking`` back toward 7 (the
committed artifact) and ultimately 0, locking in each gain. That work is
tracked in **#3775 / #3766 / #3829**.

Validation path for board-05 routing changes
---------------------------------------------
Regenerate board-05 **in the CI environment** (the ``kicad/kicad:10.0``
container) and let the board-05 CI job assert
``blocking_incomplete_count <= 11`` via this gate. A LOCAL run of this gate
after a full host regen reports ~11 -- that is the documented host-vs-CI
reach divergence (#3822), NOT a defect in this script or the job. The
authoritative verdict is the PR's own CI run.

This gate loads the routed board-05 PCB and asserts that the number of
blocking incomplete nets does not exceed ``--max-blocking`` (default 11).
It reuses :class:`kicad_tools.analysis.net_status.NetStatusAnalyzer` --
whose ``blocking_incomplete_count`` "Mirrors
``scripts/ci/check_routed_drc.py:_count_blocking_errors``", i.e. it applies
the same advisory/plane-residual filtering the DRC gate uses -- rather than
re-deriving the metric.

The ``--max-blocking`` threshold is a CLI argument (default 11, a temporary
loose bound above the observed nondeterministic CI ceiling of 10) so future
PRs (#3775 PHASE relayout, #3766 complete the blocking nets, #3829 make the
re-route deterministic) can tighten it to 7, ... 0 as they land routing
improvements, locking in each gain.

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

# TEMPORARY loose bound (issue #3836). A fresh full re-route of board-05
# (kicad/kicad:10.0, 90-min timeout) is NONDETERMINISTIC at its floor: it
# reaches 9 OR 10 blocking nets depending on the run (observed: main=9,
# PR #3835 branch=10 twice, with identical router code). The committed
# on-disk artifact is 7, which is itself not reproducible from the current
# recipe. The previous default of 9 sat exactly on the 9-vs-10 boundary and
# intermittently red-lighted unrelated PRs. 11 is a safe margin above the
# observed CI ceiling of 10, so the gate stops flaking while still catching
# gross regressions (> 11). The proper fix is a DETERMINISTIC re-route, after
# which this should be tightened back toward 7 then 0 -- tracked in
# #3775 / #3766 / #3829.
#
# Issue #3887: board-05's re-route IS now deterministic (the main pass +
# rescue loop moved from the wall-clock --per-net-timeout cutoff to a fixed
# per-net ITERATION budget; see boards/05-bldc-motor-controller/design.py
# _BOARD_05_PER_NET_ITERATIONS). The threshold INTENTIONALLY stays at 11 in
# this PR: per #3822 the deterministic blocking_incomplete_count is CI-
# authoritative (the macOS host routes fewer nets than the Linux CI runner),
# so the tightened bound must be read off a green board-05-routing-regression
# run and is deferred to that measured follow-up rather than guessed here.
#
# Issue #3894: #3887's determinism was INCOMPLETE -- it made the per-net
# budget deterministic but left the OUTER wall-clock caps (--timeout 900 on
# the main pass, stage_timeout_s 300 on the rescue loop) in place. Those caps
# FIRED under CI runner load and truncated the otherwise-deterministic route,
# so the blocking count varied run-to-run (observed 12 vs <=11 on a byte-
# identical recipe). #3894 disables both outer caps (--timeout 0 == unbounded;
# the iteration budget is the sole terminator), so the completed-net set --
# hence this count -- is now machine-independent. The threshold STILL stays at
# 11 here: per #3822 the true deterministic floor must be read off two
# consecutive green board-05-routing-regression runs (the un-truncated route
# is expected to land back at the historical 9-10 floor, comfortably <=11),
# and tightening 11 -> that measured value is a deliberate follow-up, NOT a
# locally-guessed number.
DEFAULT_MAX_BLOCKING = 11


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
        f"exceeds --max-blocking={max_blocking}. This threshold is a TEMPORARY "
        f"loose bound (issue #3836): board-05's CI re-route is NONDETERMINISTIC "
        f"(9-10 blocking) and diverges from the committed artifact (7), so the "
        f"default of 11 sits a safe margin above the observed CI ceiling of 10 "
        f"to stop flakiness. The gate still catches GROSS regressions (> 11). "
        f"The proper fix is a DETERMINISTIC re-route, then tightening this back "
        f"toward 7 then 0 -- tracked in #3775/#3766/#3829. Either fix the "
        f"routing or, if the floor truly moved, adjust --max-blocking in the CI "
        f"job with reviewer sign-off. NOTE: a LOCAL macOS run routes board-05 to "
        f"~11 blocking nets; this gate is CI-validated only (#3822)."
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
            f"(default: {DEFAULT_MAX_BLOCKING}, a TEMPORARY loose bound above "
            "the observed nondeterministic CI ceiling of 10; board-05's CI "
            "re-route is nondeterministic at 9-10 and diverges from the "
            "committed artifact of 7 -- issue #3836). Tighten this back toward "
            "7/0 once the re-route is made deterministic (#3775/#3766/#3829)."
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
