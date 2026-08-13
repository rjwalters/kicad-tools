"""screenshot CLI command: capture a PNG image of a KiCad board or schematic.

Usage:
    kct screenshot board.kicad_pcb
    kct screenshot board.kicad_pcb -o board.png
    kct screenshot board.kicad_pcb --layers copper --bw
    kct screenshot schematic.kicad_sch -o schematic.png

Machine output (``--format json``, issue #4674): one document carrying the
resolved ``input``/``output`` paths, the captured ``width_px``/``height_px``
and (for PCBs) ``layers_rendered`` in render order; failures emit
``{"error": ..., "success": false}`` with the exit code unchanged.  See
``docs/reference/machine-output.md``.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .format_options import FORMAT_JSON, add_format_flag, emit_json


def _fail(as_json: bool, source: str, message: str) -> int:
    """Report a screenshot failure as a document (JSON) or prose (text)."""
    if as_json:
        emit_json(
            {
                "command": "screenshot",
                "input": source,
                "error": message,
                "success": False,
            }
        )
    else:
        print(f"Error: {message}", file=sys.stderr)
    return 1


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
    add_format_flag(parser)

    args = parser.parse_args(argv)
    as_json = args.format == FORMAT_JSON

    input_path = Path(args.input)
    if not input_path.exists():
        return _fail(as_json, args.input, f"File not found: {args.input}")

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
        return _fail(
            as_json,
            args.input,
            f"Unsupported file type: {input_path.suffix} (expected .kicad_pcb or .kicad_sch)",
        )

    if not result["success"]:
        return _fail(as_json, args.input, str(result["error_message"]))

    if as_json:
        document = {
            "command": "screenshot",
            "input": args.input,
            "output": str(result["output_path"]),
            "width_px": result["width_px"],
            "height_px": result["height_px"],
            # Render order is meaningful (bottom-to-top compositing) and
            # deterministic for a given input, so it is preserved, not sorted.
            "layers_rendered": list(result.get("layers_rendered") or []),
            "success": True,
        }
        emit_json(document)
        return 0

    print(f"Screenshot saved to {result['output_path']}")
    print(f"  Size: {result['width_px']}x{result['height_px']} px")
    if result.get("layers_rendered"):
        print(f"  Layers: {', '.join(result['layers_rendered'])}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
