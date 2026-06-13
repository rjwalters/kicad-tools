#!/usr/bin/env python3
"""Calibration helper for the Issue #3403 sum-of-clearances area estimator.

Run from the repository root::

    uv run python scripts/calibrate_area_estimate.py

For each known test board, prints the current envelope area, the estimated
required area at ``packing_overhead=2.5``, and the ratio
``envelope / estimate``.  The ratio is the headline calibration figure:

  - ratio > 1.0: envelope meets the heuristic lower bound (routable).
  - ratio < 1.0: envelope is below the lower bound -- the pre-route check
    would skip the doomed routing attempt and escalate.

Acceptance per the Issue #3403 body: the ratio should track empirical
routability.  We expect already-routable boards to land comfortably above
1.0; pathologically over-constrained recipes (synthetic small envelopes
with large parts) should fall below.

To explore the sensitivity to packing_overhead, edit the
``--packing-overhead`` value below.  Setting it to 0 disables the
estimator entirely (the reactive DRC-density backstop still applies in
the live router).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--packing-overhead",
        type=float,
        default=2.5,
        help="Packing-density multiplier (default: 2.5).",
    )
    parser.add_argument(
        "--mfr",
        type=str,
        default="jlcpcb",
        help="Manufacturer for clearance lookup (default: jlcpcb).",
    )
    args = parser.parse_args()

    from kicad_tools.router.auto_pcb_size import estimate_required_area
    from kicad_tools.router.io import extract_board_dimensions
    from kicad_tools.router.mfr_limits import get_mfr_limits
    from kicad_tools.schema.pcb import PCB

    boards = {
        "02-charlieplex": "boards/02-charlieplex-led/output/charlieplex_3x3.kicad_pcb",
        "03-usb-joystick": "boards/03-usb-joystick/output/usb_joystick.kicad_pcb",
        "04-stm32-devboard": "boards/04-stm32-devboard/output/stm32_devboard.kicad_pcb",
        "05-bldc-controller": "boards/05-bldc-motor-controller/output/bldc_controller.kicad_pcb",
        "06-diffpair-test": "boards/06-diffpair-test/output/diffpair_test.kicad_pcb",
        "07-matchgroup-test": "boards/07-matchgroup-test/output/matchgroup_test.kicad_pcb",
    }

    try:
        mfr_limits = get_mfr_limits(args.mfr)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print(
        f"Calibration @ packing_overhead={args.packing_overhead}, "
        f"mfr={args.mfr} (min_clearance={mfr_limits.min_clearance} mm)"
    )
    print()
    print(f"{'board':<22}{'env mm^2':>12}{'est mm^2':>12}{'ratio':>10}{'fps':>6}{'nets':>6}")
    print("-" * 70)

    for name, path in boards.items():
        p = Path(path)
        if not p.exists():
            print(f"{name:<22}{'NOT FOUND':>12}")
            continue
        try:
            pcb = PCB.load(p)
            est = estimate_required_area(
                pcb,
                mfr_limits,
                packing_overhead=args.packing_overhead,
            )
            dims = extract_board_dimensions(p)
            if dims is None:
                print(f"{name:<22}{'NO OUTLINE':>12}")
                continue
            w, h = dims
            envelope_mm2 = w * h
            ratio = envelope_mm2 / est.total_mm2 if est.total_mm2 > 0 else float("inf")
            print(
                f"{name:<22}{envelope_mm2:>12.0f}{est.total_mm2:>12.0f}"
                f"{ratio:>10.2f}{est.footprint_count:>6d}{est.signal_net_count:>6d}"
            )
        except Exception as exc:
            print(f"{name:<22} ERROR: {exc}")

    print()
    print(
        "Headline: ratio > 1.0 means the envelope meets the heuristic "
        "lower bound (the routing\nattempt is not provably doomed)."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
