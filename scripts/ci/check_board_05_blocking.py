#!/usr/bin/env python3
"""Board-05 blocking-net baseline gate for CI (issue #3822).

Board-05 (BLDC motor controller) is the only demo board whose routing
changes cannot be validated on the local macOS development host: the
committed CI-generated artifact reaches **7 blocking signal nets**, but an
unmodified local regen of the same recipe/seed/backend reaches ~11 (the
macOS host router reaches fewer nets than the Linux CI router on the
identical recipe). See the recipe note in
``boards/05-bldc-motor-controller/design.py`` (~lines 2870-2871).

Reproducible CI floor is 9, not 7
---------------------------------
The committed on-disk artifact is 7 blocking, but that 7-blocking board is
NOT reproducible from the current recipe -- a fresh full re-route INSIDE
CI (kicad/kicad:10.0, timeout raised to 90 min so it runs to completion)
measures **blocking_incomplete_count = 9** (blocking nets: ISENSE_A+,
ISENSE_A-, ISENSE_B+, ISENSE_B-, ISENSE_C-, PHASE_A, PHASE_B, PHASE_C,
PWM_BH). The 7 -> 9 gap between the committed artifact and a fresh CI
re-route is the board-05 routing reproducibility regression tracked in
**#3775 / #3766**.

This gate is therefore calibrated to the MEASURED, REPRODUCIBLE CI floor
of **9** (the default ``--max-blocking``), not to the aspirational,
non-reproducible committed value of 7. This is calibration to reality, not
gate-weakening: the gate is a hard assertion (no ``continue-on-error``) and
guards against regressions BEYOND the current reproducible floor of 9. As
#3775 / #3766 land routing improvements, tighten ``--max-blocking`` back
toward 7 (the committed artifact) and ultimately 0, locking in each gain.

Validation path for board-05 routing changes
---------------------------------------------
Regenerate board-05 **in the CI environment** (the ``kicad/kicad:10.0``
container) and let the board-05 CI job assert
``blocking_incomplete_count <= 9`` via this gate. A LOCAL run of this gate
after a full host regen will report ~11 and exit 2 -- that is the
documented host-vs-CI reach divergence (#3822), NOT a defect in this
script or the job. The authoritative verdict is the PR's own CI run.

This gate loads the routed board-05 PCB and asserts that the number of
blocking incomplete nets does not exceed ``--max-blocking`` (default 9).
It reuses :class:`kicad_tools.analysis.net_status.NetStatusAnalyzer` --
whose ``blocking_incomplete_count`` "Mirrors
``scripts/ci/check_routed_drc.py:_count_blocking_errors``", i.e. it applies
the same advisory/plane-residual filtering the DRC gate uses -- rather than
re-deriving the metric.

The ``--max-blocking`` threshold is a CLI argument (default 9, the measured
reproducible CI floor) so future PRs (#3775 PHASE relayout, #3766 complete
the blocking nets) can tighten it to 8, 7, ... 0 as they land routing
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

# Measured, reproducible CI floor for a fresh full re-route of board-05
# (kicad/kicad:10.0, 90-min timeout): 9 blocking nets. The committed on-disk
# artifact is 7, but that 7-blocking board is NOT reproducible from the
# current recipe -- the 7 -> 9 gap is the reproducibility regression tracked
# in #3775 / #3766. Tighten this back toward 7/0 as those land.
DEFAULT_MAX_BLOCKING = 9


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
        f"exceeds the measured reproducible CI floor of {max_blocking} "
        f"(--max-blocking). The committed on-disk artifact is 7 blocking, but a "
        f"fresh CI re-route reaches 9 (the 7 -> 9 reproducibility gap is tracked "
        f"in #3775/#3766); this gate guards against regressions BEYOND that "
        f"floor. Either fix the routing or, if the floor truly moved, adjust "
        f"--max-blocking in the CI job with reviewer sign-off. NOTE: a LOCAL "
        f"macOS run routes board-05 to ~11 blocking nets; this gate is "
        f"CI-validated only (#3822).{names_suffix}",
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
            f"(default: {DEFAULT_MAX_BLOCKING}, the measured reproducible CI "
            "floor; the committed artifact is 7 but a fresh CI re-route reaches "
            "9 -- the 7->9 gap is tracked in #3775/#3766). Tighten this back "
            "toward 7/0 in #3775/#3766 as routing improves."
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
