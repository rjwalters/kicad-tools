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

The canonical recipe (see ``generate_design.py:route_pcb()``) uses:

  * 0.05mm routing grid (needed for J1 USB-C off-grid pad escape; #3095)
  * 0.15mm trace width / 0.15mm trace clearance
  * ``manufacturer="jlcpcb-tier1"`` declared on ``DesignRules`` so the
    EscapeRouter can resolve ``via_in_pad_supported`` (#3183)
  * fine-pitch clearance 0.08mm at 0.8mm threshold (#3095)
  * Only VCC / GND / VBUS skipped -- USB_CC1 / USB_CC2 are now routable
    on the finer grid (#3095)
  * ``intra_pair_clearance=0.15mm`` on USB_D+/USB_D- (#3095)
  * ``random.seed(42)`` for determinism
  * ``KICAD_TOOLS_EXTENDED_PITCH_IN_PAD_FALLBACK=1`` env (#3183)
  * ``route_all`` with in-pad escape rescues on U1 pins 12-15, 26-27
    (#3183)

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
    """Run DRC on the PCB using kct check for consistent results.

    Issue #3150 / #3308: align the local DRC summary with the
    jlcpcb-tier1 profile this board ships and is gated against (see
    ``.github/routed-drc-tolerance.yml`` and
    ``generate_design.py:run_drc()``).

    Returns:
        Tuple of (success, error_count, warning_count)
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

        return result.returncode == 0, error_count, warning_count

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
        print(f"WARNING: All nets routed, but {drc_errors} DRC violation(s) detected!")
        print("  Review DRC errors before manufacturing.")
        exit_code = 1
    else:
        # ``route_pcb`` already printed the per-net partial line; here we
        # just consolidate.  Match ``generate_design.py``'s success rule:
        # PARTIAL routing is acceptable so long as the routed-DRC summary
        # is within the jlcpcb-tier1 ceiling.
        print("PARTIAL: not all nets routed (see route_pcb output above)")
        if not drc_passed:
            print(f"  Additionally, {drc_errors} DRC violation(s) detected.")
        # Partial routing is acceptable for this board; the USB-C
        # connector pad density exceeds what a 2-layer autorouter can
        # fully handle.  Match the approach used by generate_design.py:
        # success if DRC passes.
        exit_code = 0 if drc_passed else 1
    print("=" * 60)

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
