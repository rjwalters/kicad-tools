"""
CLI command for trace optimization.

Usage:
    kct optimize-traces board.kicad_pcb
    kct optimize-traces board.kicad_pcb --net "NET8"
    kct optimize-traces board.kicad_pcb -o optimized.kicad_pcb
"""

import argparse
import sys
from pathlib import Path
from typing import List, Optional


def main(argv: Optional[List[str]] = None) -> int:
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
""",
    )

    parser.add_argument(
        "pcb",
        help="Input PCB file (.kicad_pcb)",
    )
    parser.add_argument(
        "-o", "--output",
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
        "-v", "--verbose",
        action="store_true",
        help="Show detailed per-net statistics",
    )

    args = parser.parse_args(argv)

    # Check input file exists
    pcb_path = Path(args.pcb)
    if not pcb_path.exists():
        print(f"Error: PCB file not found: {pcb_path}", file=sys.stderr)
        return 1

    # Import here to avoid circular imports
    from kicad_tools.router.optimizer import (
        OptimizationConfig,
        OptimizationStats,
        TraceOptimizer,
    )

    # Configure optimizer
    config = OptimizationConfig(
        merge_collinear=not args.no_merge,
        eliminate_zigzags=not args.no_zigzag,
        convert_45_corners=not args.no_45,
        corner_chamfer_size=args.chamfer_size,
    )

    optimizer = TraceOptimizer(config)

    print("=" * 50)
    print("Trace Optimization")
    print("=" * 50)
    print(f"\nInput:  {pcb_path}")
    if args.output:
        print(f"Output: {args.output}")
    if args.net:
        print(f"Filter: nets matching '{args.net}'")
    print()

    # Show enabled optimizations
    print("Optimizations enabled:")
    print(f"  - Collinear merge: {'yes' if config.merge_collinear else 'no'}")
    print(f"  - Zigzag elimination: {'yes' if config.eliminate_zigzags else 'no'}")
    print(f"  - 45° corners: {'yes' if config.convert_45_corners else 'no'}")
    if config.convert_45_corners:
        print(f"    (chamfer size: {config.corner_chamfer_size}mm)")
    print()

    # Run optimization
    try:
        stats = optimizer.optimize_pcb(
            str(pcb_path),
            output_path=args.output,
            net_filter=args.net,
            dry_run=args.dry_run,
        )
    except Exception as e:
        print(f"Error during optimization: {e}", file=sys.stderr)
        return 1

    # Display results
    print("-" * 50)
    print("Results:")
    print("-" * 50)
    print(f"  Nets optimized:  {stats.nets_optimized}")
    print()
    print(f"  Segments:        {stats.segments_before:>6} -> {stats.segments_after:>6}  "
          f"({-stats.segment_reduction:+.1f}%)")
    print(f"  Corners:         {stats.corners_before:>6} -> {stats.corners_after:>6}")
    print(f"  Total length:    {stats.length_before:>6.1f}mm -> {stats.length_after:>6.1f}mm  "
          f"({-stats.length_reduction:+.1f}%)")
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
