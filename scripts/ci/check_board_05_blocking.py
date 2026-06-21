#!/usr/bin/env python3
"""Board-05 blocking-net baseline gate for CI (issue #3822).

Board-05 (BLDC motor controller) is the only demo board whose routing
changes cannot be validated on the local macOS development host: the
committed CI-generated artifact reaches **7 blocking signal nets**, but an
unmodified local regen of the same recipe/seed/backend reaches ~11 (the
macOS host router reaches fewer nets than the Linux CI router on the
identical recipe). See the recipe note in
``boards/05-bldc-motor-controller/design.py`` (~lines 2870-2871).

Validation path for board-05 routing changes
---------------------------------------------
Regenerate board-05 **in the CI environment** (the ``kicad/kicad:10.0``
container reaches the 7-blocking baseline); the board-05 CI job asserts
``blocking_incomplete_count <= 7`` via this gate. A LOCAL run of this gate
after a full host regen will report ~11 and exit 2 -- that is the
documented host-vs-CI reach divergence (#3822), NOT a defect in this
script or the job. The authoritative verdict is the PR's own CI run.

This gate loads the routed board-05 PCB and asserts that the number of
blocking incomplete nets does not exceed ``--max-blocking`` (default 7).
It reuses :class:`kicad_tools.analysis.net_status.NetStatusAnalyzer` --
whose ``blocking_incomplete_count`` "Mirrors
``scripts/ci/check_routed_drc.py:_count_blocking_errors``", i.e. it applies
the same advisory/plane-residual filtering the DRC gate uses -- rather than
re-deriving the metric.

The ``--max-blocking`` threshold is a CLI argument (default 7) so future
PRs (#3775 PHASE relayout, #3766 complete the 7 blocking nets) can tighten
it to 6, 5, ... 0 as they land routing improvements, locking in each gain.

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
        f"exceeds the committed baseline of {max_blocking} "
        f"(--max-blocking). Either fix the routing or, if the baseline truly "
        f"moved, adjust --max-blocking in the CI job with reviewer sign-off. "
        f"NOTE: a LOCAL macOS run routes board-05 to ~11 blocking nets; this "
        f"gate is CI-validated only (#3822).{names_suffix}",
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
            f"(default: {DEFAULT_MAX_BLOCKING}, the committed baseline). "
            "Tighten this in #3775/#3766 as routing improves."
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
