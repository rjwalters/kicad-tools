#!/usr/bin/env python3
"""
Demonstrate autorouting on the USB joystick PCB.

Issue #3308 (June 7 2026): this script previously carried its OWN
recipe -- 0.1mm grid, 0.2mm clearance, ``USB_CC1`` / ``USB_CC2``
skipped, ``route_all_with_diffpairs(enabled=True)`` -- which had
drifted from the canonical recipe in ``generate_design.py:route_pcb()``
that produced the committed routed PCB.  The drift caused
``kct build --step route`` (which invokes ``route_demo.py``, see
``src/kicad_tools/cli/build_cmd.py:1189``) to produce strictly worse
output than the committed artifact even when nothing in the router
itself had regressed.

Fix (Option 1a from the issue):
``route_demo.py`` now DELEGATES to ``generate_design.py:route_pcb()``
so the demo and the end-to-end build recipe are guaranteed to be the
same code path.  The two cannot drift again because there is now only
one copy of the recipe.

The canonical recipe (see ``generate_design.py:route_pcb()``) is the
production ``kct route`` invocation -- the SAME flags pinned by
``tests/router/test_board03_routing_baseline.py`` (Issue #3410
consolidation; board-05 set the "bake the proven kct route recipe into
the design script" precedent in PR #2981).  See ``route_pcb()`` for
the exact flag list and the rationale.

Usage:
    python route_demo.py [input_pcb] [output_pcb]

Example:
    python route_demo.py output/usb_joystick.kicad_pcb output/usb_joystick_routed.kicad_pcb
"""

import contextlib
import subprocess
import sys
from pathlib import Path

# Add src to path for development (ensures source version is used)
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from kicad_tools.dev import warn_if_stale

# Warn if running source scripts with stale pipx install
warn_if_stale()


def run_drc(pcb_path: Path) -> tuple[bool, int, int]:
    """Run DRC on the routed PCB and return a truthful (passed, errors, warnings).

    Issue #3150 / #3308: align the local DRC summary with the
    jlcpcb-tier1 profile this board ships and is gated against (see
    ``.github/routed-drc-tolerance.yml`` and
    ``generate_design.py:run_drc()``).

    Issue #3969 (Bug B): this used to call ``kct check`` WITHOUT
    ``--drc-only``.  That runs the *meta-check rollup* (DRC + ERC + LVS +
    Manifest).  On a fresh route the PCB lives in a directory with no
    ERC/LVS/Manifest artifacts, so those sub-checks are ``NOT RUN`` and
    the rollup exits ``INCOMPLETE`` (exit code 2) even though the DRC
    engine itself found ``Errors: 0``.  The old code read
    ``returncode != 0`` as ``drc_passed=False``, parsed 0 errors, and the
    caller then printed the logically impossible
    ``"0 DRC violation(s) detected!"`` banner.

    The fix mirrors ``generate_design.py:run_drc()``: add ``--drc-only``
    so the exit code reflects *geometric DRC only* (non-zero iff
    ``error_count > 0``).  This guarantees the invariant the caller relies
    on -- ``passed`` is True iff ``errors == 0`` -- so the "0 violations
    detected" message can never appear on a failing exit.

    Returns:
        Tuple of (passed, error_count, warning_count).  ``passed`` is True
        exactly when ``error_count == 0``.  On a hard failure to run the
        checker, returns ``(False, -1, -1)``.
    """
    try:
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "kicad_tools.cli",
                "check",
                str(pcb_path),
                "--mfr",
                "jlcpcb-tier1",
                # Issue #3969: scope to geometric DRC so a fresh-route temp
                # dir without ERC/LVS/Manifest artifacts does not exit
                # INCOMPLETE (2) and get misread as a DRC failure with 0
                # parsed errors.  Matches generate_design.py:run_drc().
                "--drc-only",
            ],
            capture_output=True,
            text=True,
        )

        # Parse the output to extract error/warning counts
        error_count = 0
        warning_count = 0
        for line in result.stdout.split("\n"):
            if "Errors:" in line:
                with contextlib.suppress(ValueError):
                    error_count = int(line.split(":")[-1].strip())
            elif "Warnings:" in line:
                with contextlib.suppress(ValueError):
                    warning_count = int(line.split(":")[-1].strip())

        # Issue #3969: derive ``passed`` from the parsed error count, not
        # solely the exit code, so the (passed, errors) pair is always
        # self-consistent.  With --drc-only the exit code already tracks
        # error_count, but keying off the count as well makes the "0
        # violations on a failing exit" banner structurally impossible.
        passed = result.returncode == 0 and error_count == 0
        return passed, error_count, warning_count

    except Exception as e:
        print(f"  Warning: DRC check failed: {e}")
        return False, -1, -1


def main():
    """Run the routing demo via the canonical generate_design.py recipe.

    Issue #3308: delegate to ``generate_design.py:route_pcb()`` so the
    demo cannot drift from the end-to-end recipe.  ``kct build --step
    route`` runs this script (per ``src/kicad_tools/cli/build_cmd.py:1189``);
    using the same code path that produced the committed routed PCB
    guarantees a fresh ``kct build`` reproduces the shipped artifact.
    """
    # Import here (rather than at module scope) so the import path is
    # set up before ``generate_design`` pulls in its router deps.
    sys.path.insert(0, str(Path(__file__).parent))
    from generate_design import route_pcb  # noqa: E402

    # Parse arguments
    demo_dir = Path(__file__).parent
    input_pcb = sys.argv[1] if len(sys.argv) > 1 else "output/usb_joystick.kicad_pcb"
    output_pcb = sys.argv[2] if len(sys.argv) > 2 else "output/usb_joystick_routed.kicad_pcb"

    input_path = demo_dir / input_pcb
    output_path = demo_dir / output_pcb

    if not input_path.exists():
        print(f"Error: Input PCB not found: {input_path}")
        print("Run generate_pcb.py first to create the PCB file.")
        sys.exit(1)

    print("=" * 60)
    print("USB Joystick Autorouting Demo (delegates to generate_design.py)")
    print("=" * 60)
    print(f"\nInput:  {input_path}")
    print(f"Output: {output_path}")
    print(
        "\nIssue #3308: this demo delegates to "
        "generate_design.py:route_pcb() so the demo and the canonical "
        "build recipe share a single implementation."
    )

    # Delegate to the canonical recipe.  ``route_pcb()`` prints its own
    # progress block and returns True on full success, False on partial.
    success = route_pcb(input_path, output_path)

    # Run DRC validation on the resulting routed PCB.  This is in addition
    # to whatever the recipe does internally so the demo always surfaces
    # a final DRC summary even when ``route_pcb()`` short-circuits.
    print("\n--- DRC Validation ---")
    drc_passed, drc_errors, drc_warnings = run_drc(output_path)
    if drc_passed:
        print("  DRC PASSED")
    else:
        if drc_errors > 0:
            print(f"  Errors:   {drc_errors}")
        if drc_warnings > 0:
            print(f"  Warnings: {drc_warnings}")
        print(f"\n  Run 'kct check {output_path} --mfr jlcpcb-tier1' for full details")

    # Final tally that the regression test parser (#2744) keys off of.
    # ``route_pcb`` prints its own "PARTIAL: Routed N/M signal nets"
    # line, so we add a top-level SUCCESS / PARTIAL banner here.
    print("\n" + "=" * 60)
    if success and drc_passed:
        print("SUCCESS: All nets routed, DRC passed!")
        exit_code = 0
    elif success and not drc_passed:
        # Issue #3969 (Bug B): never claim "0 DRC violation(s) detected!"
        # on a failing path.  run_drc() now guarantees drc_passed is False
        # only when errors are present (drc_errors > 0) or the checker
        # could not run (drc_errors == -1).  Word the banner to match
        # whichever case actually occurred instead of blindly printing the
        # count.
        if drc_errors > 0:
            print(f"WARNING: All nets routed, but {drc_errors} DRC violation(s) detected!")
            print("  Review DRC errors before manufacturing.")
        else:
            print("WARNING: All nets routed, but the DRC check could not be completed.")
            print("  Re-run 'kct check' manually before manufacturing.")
        exit_code = 1
    else:
        # ``route_pcb`` already printed the per-net partial line; here we
        # just consolidate.  Match ``generate_design.py``'s success rule:
        # PARTIAL routing is acceptable so long as the routed-DRC summary
        # is within the jlcpcb-tier1 ceiling.
        print("PARTIAL: not all nets routed (see route_pcb output above)")
        if not drc_passed:
            # Issue #3969: only report a violation *count* when there
            # actually is one; otherwise say the DRC could not complete.
            if drc_errors > 0:
                print(f"  Additionally, {drc_errors} DRC violation(s) detected.")
            else:
                print("  Additionally, the DRC check could not be completed.")
        # Partial routing is acceptable for this board; the USB-C
        # connector pad density exceeds what a 2-layer autorouter can
        # fully handle.  Match the approach used by generate_design.py:
        # success if DRC passes.
        exit_code = 0 if drc_passed else 1
    print("=" * 60)

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
