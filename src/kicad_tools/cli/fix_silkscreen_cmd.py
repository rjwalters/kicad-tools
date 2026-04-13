#!/usr/bin/env python3
"""Fix silkscreen line widths to meet manufacturer specifications.

Usage:
    kct fix-silkscreen board.kicad_pcb [options]

Examples:
    # Widen silkscreen lines to meet JLCPCB minimums (default)
    kct fix-silkscreen board.kicad_pcb --mfr jlcpcb

    # Specify minimum width directly
    kct fix-silkscreen board.kicad_pcb --min-width 0.15

    # Preview changes without applying
    kct fix-silkscreen board.kicad_pcb --dry-run

    # Output to a different file
    kct fix-silkscreen board.kicad_pcb -o fixed_board.kicad_pcb
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

from kicad_tools.drc.repair_silkscreen import SilkscreenRepairer, SilkscreenRepairResult
from kicad_tools.manufacturers.base import load_design_rules_from_yaml


def _get_min_width(
    mfr: str | None,
    layers: int,
    copper: float,
    explicit_width: float | None,
) -> float:
    """Resolve the target minimum silkscreen width.

    If *explicit_width* is given it takes precedence.  Otherwise the value is
    loaded from the manufacturer YAML profile.
    """
    if explicit_width is not None:
        return explicit_width

    if mfr:
        try:
            rules_dict = load_design_rules_from_yaml(mfr)
            key = f"{layers}layer_{int(copper)}oz"
            if key in rules_dict:
                return rules_dict[key].min_silkscreen_width_mm
            # Try without copper weight
            key = f"{layers}layer_1oz"
            if key in rules_dict:
                return rules_dict[key].min_silkscreen_width_mm
            # Fall back to first available
            first = next(iter(rules_dict.values()))
            return first.min_silkscreen_width_mm
        except FileNotFoundError:
            print(
                f"Warning: No configuration found for manufacturer '{mfr}'",
                file=sys.stderr,
            )

    # Sensible default matching JLCPCB minimum
    return 0.15


def _print_results(
    result: SilkscreenRepairResult,
    output_format: str,
    dry_run: bool,
    mfr: str | None,
) -> None:
    """Print repair results in the requested format."""
    if output_format == "json":
        _print_json(result, dry_run, mfr)
    elif output_format == "summary":
        _print_summary(result, dry_run, mfr)
    else:
        _print_text(result, dry_run, mfr)


def _print_json(
    result: SilkscreenRepairResult,
    dry_run: bool,
    mfr: str | None,
) -> None:
    data = {
        "min_width_mm": result.min_width_mm,
        "manufacturer": mfr,
        "dry_run": dry_run,
        "total_fixed": result.total_fixed,
        "fixes": [
            {
                "element_type": f.element_type,
                "layer": f.layer,
                "footprint_ref": f.footprint_ref,
                "old_width_mm": f.old_width,
                "new_width_mm": f.new_width,
            }
            for f in result.fixes
        ],
    }
    print(json.dumps(data, indent=2))


def _print_summary(
    result: SilkscreenRepairResult,
    dry_run: bool,
    mfr: str | None,
) -> None:
    action = "Would fix" if dry_run else "Fixed"
    source = f" to {mfr.upper()} minimum" if mfr else ""
    print(
        f"{action} {result.total_fixed} silkscreen line width violation(s)"
        f"{source} (min: {result.min_width_mm}mm)"
    )


def _print_text(
    result: SilkscreenRepairResult,
    dry_run: bool,
    mfr: str | None,
) -> None:
    if result.total_fixed == 0:
        print("No silkscreen lines needed widening.")
        return

    action = "Would widen" if dry_run else "Widened"
    source = f" to {mfr.upper()} minimum" if mfr else ""
    print(
        f"{action} {result.total_fixed} silkscreen line(s){source} (min: {result.min_width_mm}mm):"
    )

    # Group by footprint for readable output
    by_fp: Counter[str] = Counter()
    width_examples: dict[str, tuple[float, float]] = {}
    for fix in result.fixes:
        key = fix.footprint_ref or "(board-level)"
        by_fp[key] += 1
        if key not in width_examples:
            width_examples[key] = (fix.old_width, fix.new_width)

    for fp_ref, count in by_fp.most_common():
        old_w, new_w = width_examples[fp_ref]
        print(f"  {fp_ref}: {count} line(s) widened ({old_w:.2f}mm -> {new_w:.2f}mm)")


def main(argv: list[str] | None = None) -> int:
    """Main entry point for fix-silkscreen command."""
    parser = argparse.ArgumentParser(
        description="Fix silkscreen line widths to meet manufacturer specifications",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    # Widen silkscreen lines to meet JLCPCB minimums
    kct fix-silkscreen board.kicad_pcb --mfr jlcpcb

    # Specify minimum width directly
    kct fix-silkscreen board.kicad_pcb --min-width 0.15

    # Preview changes without applying
    kct fix-silkscreen board.kicad_pcb --dry-run
        """,
    )
    parser.add_argument("pcb", help="Path to .kicad_pcb file")
    parser.add_argument(
        "--mfr",
        choices=["jlcpcb", "pcbway", "oshpark", "seeed"],
        default="jlcpcb",
        help="Manufacturer to use for design rules (default: jlcpcb)",
    )
    parser.add_argument(
        "--layers",
        type=int,
        default=2,
        help="Number of PCB layers (default: 2)",
    )
    parser.add_argument(
        "--copper",
        type=float,
        default=1.0,
        help="Outer copper weight in oz (default: 1.0)",
    )
    parser.add_argument(
        "--min-width",
        type=float,
        help="Minimum silkscreen line width in mm (overrides manufacturer rules)",
    )
    parser.add_argument(
        "-o",
        "--output",
        help="Output file path (default: overwrite input)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview changes without modifying files",
    )
    parser.add_argument(
        "--format",
        choices=["text", "json", "summary"],
        default="text",
        help="Output format (default: text)",
    )
    parser.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="Suppress progress output (for scripting)",
    )

    args = parser.parse_args(argv)

    # Validate input file
    pcb_path = Path(args.pcb)
    if not pcb_path.exists():
        print(f"Error: PCB file not found: {pcb_path}", file=sys.stderr)
        return 1

    if pcb_path.suffix.lower() != ".kicad_pcb":
        print(f"Error: Expected .kicad_pcb file, got: {pcb_path.suffix}", file=sys.stderr)
        return 1

    # Resolve minimum width
    min_width = _get_min_width(args.mfr, args.layers, args.copper, args.min_width)

    # Parse and repair
    try:
        repairer = SilkscreenRepairer(pcb_path)
    except Exception as e:
        print(f"Error parsing PCB file: {e}", file=sys.stderr)
        return 1

    result = repairer.repair_line_widths(min_width, dry_run=args.dry_run)

    # Print results
    if not args.quiet:
        _print_results(result, output_format=args.format, dry_run=args.dry_run, mfr=args.mfr)

    # Save if not dry run and there were fixes
    if result.total_fixed > 0 and not args.dry_run:
        output_path = Path(args.output) if args.output else pcb_path
        try:
            repairer.save(output_path)
            if not args.quiet and args.format == "text":
                print(f"\nSaved to: {output_path}")
        except Exception as e:
            print(f"Error saving PCB file: {e}", file=sys.stderr)
            return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
