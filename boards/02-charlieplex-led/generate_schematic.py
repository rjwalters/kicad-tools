#!/usr/bin/env python3
"""Generate the ATtiny85 charlieplex schematic from the shared circuit table."""

import argparse
import sys
from pathlib import Path

from design_spec import LED_CONNECTIONS
from hardware_design import write_schematic


def create_charlieplex_schematic(output_path: Path, verbose: bool = False) -> bool:
    write_schematic(output_path)
    return True


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Generate a KiCad schematic for a 3x3 Charlieplex LED grid"
    )
    parser.add_argument(
        "output",
        nargs="?",
        default=None,
        help="Output file path or directory (default: output/charlieplex_3x3.kicad_sch)",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Show detailed validation warnings",
    )

    args = parser.parse_args()

    # Default output filename
    default_filename = "charlieplex_3x3.kicad_sch"

    if args.output:
        output_path = Path(args.output)
        # If user passes a directory, auto-append the default filename
        if output_path.is_dir():
            output_path = output_path / default_filename
            print(f"Note: Directory provided, using {output_path}")
    else:
        output_path = Path(__file__).parent / "output" / default_filename

    try:
        success = create_charlieplex_schematic(output_path, verbose=args.verbose)

        print("\n" + "=" * 60)
        print("SUMMARY")
        print("=" * 60)
        print(f"Output: {output_path}")
        print(f"Result: {'SUCCESS' if success else 'FAILED'}")

        print("\nCharlieplex LED mapping:")
        print("  LED   Anode    Cathode  (To light: Anode=HIGH, Cathode=LOW)")
        for led_conn in LED_CONNECTIONS:
            print(f"  {led_conn.ref}    {led_conn.anode_node}  {led_conn.cathode_node}")

        print("\nNet connectivity (via global labels):")
        print("  ATtiny85 PB3/PB4/PB0/PB1 -> LINE_A-D -> R1-R4 -> NODE_A-D -> LEDs")

        return 0 if success else 1

    except Exception as e:
        print(f"\nError: {e}", file=sys.stderr)
        import traceback

        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
