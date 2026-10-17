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

Visual regression (issue #6912): ``--baseline-save DIR`` stores the render plus
a sidecar naming the renderer; ``--baseline-compare DIR`` re-renders and
reports ``drift_fraction`` (or ``renderer_mismatch`` when the baseline came
from a different renderer/options, in which case no drift is computed).
"""

from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
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
    baseline = parser.add_mutually_exclusive_group()
    baseline.add_argument(
        "--baseline-save",
        metavar="DIR",
        help="Also save the render and a renderer-identity sidecar as a baseline in DIR",
    )
    baseline.add_argument(
        "--baseline-compare",
        metavar="DIR",
        help="Render the current state and report drift against the baseline in DIR",
    )
    add_format_flag(parser)

    args = parser.parse_args(argv)
    as_json = args.format == FORMAT_JSON

    input_path = Path(args.input)
    if not input_path.exists():
        return _fail(as_json, args.input, f"File not found: {args.input}")

    # Determine output path
    output_path = args.output
    if args.baseline_compare:
        # Compare mode renders to a scratch file; it never touches the baseline
        # or leaves a stray PNG beside the input.
        with tempfile.TemporaryDirectory(prefix="kct-baseline-") as scratch:
            return _run(args, input_path, str(Path(scratch) / "current.png"), as_json)
    if output_path is None:
        output_path = str(input_path.with_suffix(".png"))
    return _run(args, input_path, output_path, as_json)


def _baseline_options(args: argparse.Namespace, input_path: Path) -> dict[str, object]:
    return {
        "kind": "pcb" if input_path.suffix == ".kicad_pcb" else "schematic",
        "layers": args.layers,
        "max_size": args.max_size,
        "black_and_white": args.black_and_white,
        "theme": args.theme,
    }


def _baseline_step(
    args: argparse.Namespace, input_path: Path, result: dict, as_json: bool
) -> int:
    """Save or compare a baseline. Returns the process exit code."""
    from . import screenshot_baseline as sb

    options = _baseline_options(args, input_path)
    current = sb.build_sidecar(
        sb.renderer_identity(), options, result["width_px"], result["height_px"]
    )

    if args.baseline_save:
        directory = Path(args.baseline_save)
        png_path, sidecar_path = sb.baseline_paths(directory, input_path)
        try:
            directory.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(result["output_path"], png_path)
            sb.write_sidecar(sidecar_path, current)
        except OSError as exc:
            return _fail(as_json, args.input, f"Cannot write baseline: {exc}")
        if as_json:
            emit_json(
                {
                    "command": "screenshot",
                    "input": args.input,
                    "output": str(result["output_path"]),
                    "baseline": {"saved": str(png_path), "sidecar": str(sidecar_path), **current},
                    "success": True,
                }
            )
        else:
            print(f"Baseline saved to {png_path}")
            print(f"  Sidecar: {sidecar_path}")
        return 0

    png_path, sidecar_path = sb.baseline_paths(args.baseline_compare, input_path)
    try:
        saved = sb.load_sidecar(png_path, sidecar_path)
        diffs = sb.compare_identity(saved, current)
        drift = None if diffs else sb.pixel_drift(png_path, Path(result["output_path"]))
    except sb.BaselineError as exc:
        return _fail(as_json, args.input, str(exc))

    document: dict[str, object] = {
        "command": "screenshot",
        "input": args.input,
        "baseline": str(png_path),
    }
    if drift is None:
        document.update(status="renderer_mismatch", differences=diffs, success=False)
    else:
        document.update(drift, success=True)
    if as_json:
        emit_json(document)
    elif drift is None:
        print("Renderer mismatch: baseline is not comparable (no drift computed)", file=sys.stderr)
        for d in diffs:
            print(
                f"  {d['field']}: baseline={d['baseline']!r} current={d['current']!r}",
                file=sys.stderr,
            )
    else:
        print(
            f"Baseline {drift['status']}: {drift['changed_pixels']}/{drift['total_pixels']} "
            f"pixels changed (drift {drift['drift_fraction']:.6f})"
        )
    return 1 if drift is None else 0


def _run(args: argparse.Namespace, input_path: Path, output_path: str, as_json: bool) -> int:

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

    if args.baseline_save or args.baseline_compare:
        return _baseline_step(args, input_path, result, as_json)

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
