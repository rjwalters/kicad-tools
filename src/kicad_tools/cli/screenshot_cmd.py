"""screenshot CLI command: capture a PNG image of a KiCad board or schematic.

Usage:
    kct screenshot board.kicad_pcb
    kct screenshot board.kicad_pcb -o board.png
    kct screenshot board.kicad_pcb --layers copper --bw
    kct screenshot schematic.kicad_sch -o schematic.png
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    """Entry point for the screenshot CLI command."""
    parser = argparse.ArgumentParser(
        prog="kct screenshot",
        description="Capture a PNG screenshot of a KiCad board or schematic.",
    )
    parser.add_argument(
        "input",
        help="Path to .kicad_pcb or .kicad_sch file",
    )
    parser.add_argument(
        "-o",
        "--output",
        help="Output PNG file path (default: <input>.png)",
    )
    parser.add_argument(
        "--layers",
        default=None,
        help=(
            "Layer specification for PCB screenshots. "
            "Preset name (default, copper, assembly, front, back) "
            "or comma-separated layer list (e.g. 'F.Cu,B.Cu,Edge.Cuts')"
        ),
    )
    parser.add_argument(
        "--max-size",
        type=int,
        default=1568,
        help="Maximum image dimension in pixels (default: 1568)",
    )
    parser.add_argument(
        "--bw",
        "--black-and-white",
        action="store_true",
        dest="black_and_white",
        help="Use black and white rendering",
    )
    parser.add_argument(
        "--theme",
        default=None,
        help="KiCad color theme name",
    )

    args = parser.parse_args(argv)

    input_path = Path(args.input)
    if not input_path.exists():
        print(f"Error: File not found: {args.input}", file=sys.stderr)
        return 1

    # Determine output path
    output_path = args.output
    if output_path is None:
        output_path = str(input_path.with_suffix(".png"))

    # Import screenshot functions (lazy to avoid import cost)
    from kicad_tools.mcp.tools.screenshot import screenshot_board, screenshot_schematic

    if input_path.suffix == ".kicad_pcb":
        result = screenshot_board(
            pcb_path=str(input_path),
            layers=args.layers,
            max_size_px=args.max_size,
            output_path=output_path,
            black_and_white=args.black_and_white,
            theme=args.theme,
        )
    elif input_path.suffix == ".kicad_sch":
        result = screenshot_schematic(
            sch_path=str(input_path),
            max_size_px=args.max_size,
            output_path=output_path,
            black_and_white=args.black_and_white,
            theme=args.theme,
        )
    else:
        print(
            f"Error: Unsupported file type: {input_path.suffix} "
            "(expected .kicad_pcb or .kicad_sch)",
            file=sys.stderr,
        )
        return 1

    if not result["success"]:
        print(f"Error: {result['error_message']}", file=sys.stderr)
        return 1

    print(f"Screenshot saved to {result['output_path']}")
    print(f"  Size: {result['width_px']}x{result['height_px']} px")
    if result.get("layers_rendered"):
        print(f"  Layers: {', '.join(result['layers_rendered'])}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
