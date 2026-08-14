"""
CLI command for trace optimization.

Usage:
    kct optimize-traces board.kicad_pcb
    kct optimize-traces board.kicad_pcb --net "NET8"
    kct optimize-traces board.kicad_pcb -o optimized.kicad_pcb
    kct optimize-traces board.kicad_pcb --drc-aware --mfr jlcpcb --layers 4

Machine output (``--format json``, issue #4674): one deterministic document
describing the run -- the resolved ``pcb``/``output`` paths, the enabled
``optimizations``, the before/after ``stats`` and whether the result was
``saved`` -- or ``{"error": ..., "success": false}`` on any failure, with the
exit code unchanged.  See ``docs/reference/machine-output.md``.
"""

import argparse
import sys
from pathlib import Path

from kicad_tools.cli.format_options import FORMAT_JSON, add_format_flag, emit_json


def _fail(as_json: bool, pcb: str, message: str, *, text: str | None = None) -> int:
    """Report an optimize-traces failure as a document (JSON) or prose (text).

    ``text`` overrides the conventional ``Error: <message>`` prose for the one
    failure path that deliberately words itself differently (``Error during
    optimization: ...``); text-mode output stays byte-identical.
    """
    if as_json:
        emit_json(
            {
                "command": "optimize-traces",
                "pcb": pcb,
                "error": message,
                "saved": False,
                "success": False,
            }
        )
    else:
        print(text if text is not None else f"Error: {message}", file=sys.stderr)
    return 1


def main(argv: list[str] | None = None) -> int:
    """Main entry point for trace optimization CLI."""
    parser = argparse.ArgumentParser(
        prog="kct optimize-traces",
        description="Optimize PCB traces to minimize bends and reduce segment count",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    kct optimize-traces board.kicad_pcb
    kct optimize-traces board.kicad_pcb --net USB_D+
    kct optimize-traces board.kicad_pcb -o optimized.kicad_pcb --no-45
    kct optimize-traces board.kicad_pcb --dry-run
    kct optimize-traces board.kicad_pcb --drc-aware --mfr jlcpcb --layers 4
""",
    )

    parser.add_argument(
        "pcb",
        help="Input PCB file (.kicad_pcb)",
    )
    parser.add_argument(
        "-o",
        "--output",
        help="Output PCB file (default: modify in place)",
    )
    parser.add_argument(
        "--net",
        help="Only optimize traces for nets matching this pattern",
    )
    parser.add_argument(
        "--no-merge",
        action="store_true",
        help="Disable collinear segment merging",
    )
    parser.add_argument(
        "--no-zigzag",
        action="store_true",
        help="Disable zigzag elimination",
    )
    parser.add_argument(
        "--no-45",
        action="store_true",
        help="Disable 45-degree corner conversion",
    )
    parser.add_argument(
        "--chamfer-size",
        type=float,
        default=0.5,
        help="Size of 45-degree chamfer in mm (default: 0.5)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show optimization results without writing output",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Show detailed per-net statistics",
    )
    parser.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="Suppress progress output (for scripting)",
    )

    # DRC-aware mode arguments
    parser.add_argument(
        "--drc-aware",
        action="store_true",
        help="Enable DRC-aware mode: roll back per-net optimizations that increase violations",
    )
    parser.add_argument(
        "--mfr",
        help="Target manufacturer for DRC rules (e.g., jlcpcb, oshpark). Required with --drc-aware",
    )
    parser.add_argument(
        "--layers",
        type=int,
        default=2,
        help="Number of copper layers for DRC checks (default: 2)",
    )
    parser.add_argument(
        "--copper",
        type=float,
        default=1.0,
        help="Copper weight in oz for DRC checks (default: 1.0)",
    )
    add_format_flag(parser)

    args = parser.parse_args(argv)
    as_json = args.format == FORMAT_JSON

    # Validate DRC-aware arguments
    if args.drc_aware and not args.mfr:
        return _fail(
            as_json,
            args.pcb,
            "--drc-aware requires --mfr to specify the manufacturer profile (e.g., --mfr jlcpcb)",
        )

    # Check input file exists
    pcb_path = Path(args.pcb)
    if not pcb_path.exists():
        return _fail(as_json, str(pcb_path), f"PCB file not found: {pcb_path}")

    # Validate manufacturer ID if provided
    if args.mfr:
        from kicad_tools.manufacturers import get_manufacturer_ids

        valid_ids = get_manufacturer_ids()
        if args.mfr not in valid_ids:
            return _fail(
                as_json,
                str(pcb_path),
                f"Unknown manufacturer '{args.mfr}'. Valid options: {', '.join(valid_ids)}",
            )

    # Import here to avoid circular imports
    from kicad_tools.cli.progress import spinner
    from kicad_tools.router.optimizer import (
        OptimizationConfig,
        TraceOptimizer,
    )

    # JSON mode owns stdout: the banner, the spinner and the results table are
    # all suppressed so exactly one document is printed.
    quiet = args.quiet or as_json

    # Configure optimizer
    config = OptimizationConfig(
        merge_collinear=not args.no_merge,
        eliminate_zigzags=not args.no_zigzag,
        convert_45_corners=not args.no_45,
        corner_chamfer_size=args.chamfer_size,
        drc_aware=args.drc_aware,
        drc_manufacturer=args.mfr,
        drc_layers=args.layers,
        drc_copper_oz=args.copper,
    )

    optimizer = TraceOptimizer(config)

    if not quiet:
        print("=" * 50)
        print("Trace Optimization")
        print("=" * 50)
        print(f"\nInput:  {pcb_path}")
        if args.output:
            print(f"Output: {args.output}")
        if args.net:
            print(f"Filter: nets matching '{args.net}'")
        if args.drc_aware:
            print(f"DRC:    aware (mfr={args.mfr}, layers={args.layers})")
        print()

        # Show enabled optimizations
        print("Optimizations enabled:")
        print(f"  - Collinear merge: {'yes' if config.merge_collinear else 'no'}")
        print(f"  - Zigzag elimination: {'yes' if config.eliminate_zigzags else 'no'}")
        print(f"  - 45 corners: {'yes' if config.convert_45_corners else 'no'}")
        if config.convert_45_corners:
            print(f"    (chamfer size: {config.corner_chamfer_size}mm)")
        if args.drc_aware:
            print(f"  - DRC-aware rollback: yes (mfr={args.mfr})")
        print()

    # Run optimization
    try:
        with spinner("Optimizing traces...", quiet=quiet):
            stats = optimizer.optimize_pcb(
                str(pcb_path),
                output_path=args.output,
                net_filter=args.net,
                dry_run=args.dry_run,
            )
    except Exception as e:
        return _fail(
            as_json,
            str(pcb_path),
            f"during optimization: {e}",
            text=f"Error during optimization: {e}",
        )

    if as_json:
        emit_json(
            {
                "command": "optimize-traces",
                "pcb": str(pcb_path),
                "output": args.output,
                "net_filter": args.net,
                "optimizations": {
                    "merge_collinear": config.merge_collinear,
                    "eliminate_zigzags": config.eliminate_zigzags,
                    "convert_45_corners": config.convert_45_corners,
                    "chamfer_size_mm": config.corner_chamfer_size,
                },
                "drc_aware": args.drc_aware,
                "manufacturer": args.mfr,
                "layers": args.layers,
                "copper_oz": args.copper,
                "stats": {
                    "nets_optimized": stats.nets_optimized,
                    "nets_rolled_back": stats.nets_rolled_back,
                    "segments_before": stats.segments_before,
                    "segments_after": stats.segments_after,
                    "segment_reduction_pct": stats.segment_reduction,
                    "corners_before": stats.corners_before,
                    "corners_after": stats.corners_after,
                    "length_before_mm": stats.length_before,
                    "length_after_mm": stats.length_after,
                    "length_reduction_pct": stats.length_reduction,
                    "drc_errors_before": stats.drc_errors_before,
                    "drc_errors_after": stats.drc_errors_after,
                },
                "dry_run": args.dry_run,
                "saved": not args.dry_run,
                "written_to": None if args.dry_run else (args.output or str(pcb_path)),
                "success": True,
            }
        )
        return 0

    if not quiet:
        # Display results
        print("-" * 50)
        print("Results:")
        print("-" * 50)

        # Show DRC-aware net stats
        if args.drc_aware:
            drc_safe = stats.nets_optimized - stats.nets_rolled_back
            print(
                f"  Nets optimized:  {stats.nets_optimized} "
                f"(DRC safe: {drc_safe}, rolled back: {stats.nets_rolled_back})"
            )
        else:
            print(f"  Nets optimized:  {stats.nets_optimized}")
        print()

        print(
            f"  Segments:        {stats.segments_before:>6} -> {stats.segments_after:>6}  "
            f"({-stats.segment_reduction:+.1f}%)"
        )
        print(f"  Corners:         {stats.corners_before:>6} -> {stats.corners_after:>6}")
        print(
            f"  Total length:    {stats.length_before:>6.1f}mm -> {stats.length_after:>6.1f}mm  "
            f"({-stats.length_reduction:+.1f}%)"
        )

        if args.drc_aware:
            delta = stats.drc_errors_after - stats.drc_errors_before
            if delta > 0:
                suffix = f"({delta} new errors, {stats.nets_rolled_back} nets rolled back)"
            elif stats.nets_rolled_back > 0:
                suffix = f"(no regressions, {stats.nets_rolled_back} nets rolled back)"
            else:
                suffix = "(no regressions)"
            print(
                f"  DRC errors:      {stats.drc_errors_before:>6} -> "
                f"{stats.drc_errors_after:>6}  {suffix}"
            )
        print()

        if args.dry_run:
            print("(Dry run - no changes written)")
        elif args.output:
            print(f"Saved to: {args.output}")
        else:
            print(f"Updated: {pcb_path}")

        print("=" * 50)

    return 0


if __name__ == "__main__":
    sys.exit(main())
