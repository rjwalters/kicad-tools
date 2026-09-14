#!/usr/bin/env python3
"""Generate the real ATtiny85 charlieplex PCB using installed KiCad footprints."""

import sys
from pathlib import Path

from design_spec import NETS
from hardware_design import BOARD_HEIGHT, BOARD_WIDTH, build_pcb

from kicad_tools.sexp import serialize_sexp


def generate_pcb() -> str:
    return serialize_sexp(build_pcb()._sexp)


def main():
    """Generate the PCB file."""
    default_filename = "charlieplex_3x3.kicad_pcb"
    if len(sys.argv) > 1:
        output_path = Path(sys.argv[1])
        if output_path.is_dir():
            # kct build passes the output directory; derive the filename within it
            output_path = output_path / default_filename
            print(f"Note: Directory provided, using {output_path}")
        elif not output_path.is_absolute():
            # Relative file path: resolve against this script's directory
            output_path = Path(__file__).parent / output_path
    else:
        output_path = Path(__file__).parent / "output" / default_filename

    output_path.parent.mkdir(parents=True, exist_ok=True)
    pcb_content = generate_pcb()
    output_path.write_text(pcb_content)

    print(f"Generated: {output_path}")
    print(f"  Board size: {BOARD_WIDTH}mm x {BOARD_HEIGHT}mm")
    print("  Components: 1 MCU, 5 resistors, 9 LEDs, 2 capacitors, power and ISP headers")
    print(
        f"  Nets: {len([n for n in NETS.values() if n > 0])} (4 LINE + 4 NODE + VCC + GND + RESET + ISP_SCK)"
    )


if __name__ == "__main__":
    main()
