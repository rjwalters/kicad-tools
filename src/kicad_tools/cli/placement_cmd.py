"""CLI commands for placement conflict detection and resolution.

Usage:
    kicad-tools placement check board.kicad_pcb
    kicad-tools placement fix board.kicad_pcb --strategy spread
"""

import argparse
import json
import sys
from pathlib import Path
from typing import List, Optional

from kicad_tools.placement import (
    Conflict,
    PlacementAnalyzer,
    PlacementFixer,
)
from kicad_tools.placement.analyzer import DesignRules
from kicad_tools.placement.fixer import FixStrategy


def cmd_check(args) -> int:
    """Check PCB for placement conflicts."""
    pcb_path = Path(args.pcb)
    if not pcb_path.exists():
        print(f"Error: File not found: {pcb_path}", file=sys.stderr)
        return 1

    # Build design rules from arguments
    rules = DesignRules(
        min_pad_clearance=args.pad_clearance,
        min_hole_to_hole=args.hole_clearance,
        min_edge_clearance=args.edge_clearance,
        courtyard_margin=args.courtyard_margin,
    )

    # Analyze
    analyzer = PlacementAnalyzer(verbose=args.verbose)

    try:
        conflicts = analyzer.find_conflicts(pcb_path, rules)
    except Exception as e:
        print(f"Error analyzing PCB: {e}", file=sys.stderr)
        return 1

    # Output results
    if args.format == "json":
        output_json(conflicts)
    elif args.format == "summary":
        output_summary(conflicts)
    else:
        output_table(conflicts, args.verbose)

    # Return code based on conflicts
    errors = [c for c in conflicts if c.severity.value == "error"]
    return 1 if errors else 0


def cmd_fix(args) -> int:
    """Suggest and apply fixes for placement conflicts."""
    pcb_path = Path(args.pcb)
    if not pcb_path.exists():
        print(f"Error: File not found: {pcb_path}", file=sys.stderr)
        return 1

    # Build design rules
    rules = DesignRules(
        min_pad_clearance=args.pad_clearance,
        min_hole_to_hole=args.hole_clearance,
        min_edge_clearance=args.edge_clearance,
        courtyard_margin=args.courtyard_margin,
    )

    # Analyze first
    analyzer = PlacementAnalyzer(verbose=args.verbose)
    conflicts = analyzer.find_conflicts(pcb_path, rules)

    if not conflicts:
        print("No placement conflicts found!")
        return 0

    print(f"Found {len(conflicts)} conflicts")

    # Parse strategy
    strategy = FixStrategy(args.strategy)

    # Parse anchored components
    anchored = set()
    if args.anchor:
        anchored = set(args.anchor.split(","))
        print(f"Anchored components: {anchored}")

    # Create fixer and suggest fixes
    fixer = PlacementFixer(
        strategy=strategy,
        anchored=anchored,
        verbose=args.verbose,
    )

    fixes = fixer.suggest_fixes(conflicts, analyzer)

    if not fixes:
        print("No fixes could be suggested")
        return 0

    print(f"\nSuggested {len(fixes)} fixes:")
    print(fixer.preview_fixes(fixes))

    if args.dry_run:
        print("\n(Dry run - no changes made)")
        return 0

    # Apply fixes
    output_path = args.output or pcb_path
    result = fixer.apply_fixes(pcb_path, fixes, output_path)

    print(f"\n{result.message}")

    if result.new_conflicts > 0:
        print(f"Warning: {result.new_conflicts} conflicts remain after fixes")

    return 0 if result.success else 1


def output_table(conflicts: List[Conflict], verbose: bool = False):
    """Output conflicts in table format."""
    if not conflicts:
        print("No placement conflicts found!")
        return

    print(f"\n{'Type':<18} {'Severity':<10} {'Components':<20} {'Message'}")
    print("-" * 80)

    for conflict in conflicts:
        comp_str = f"{conflict.component1} / {conflict.component2}"
        if len(comp_str) > 18:
            comp_str = comp_str[:17] + "..."

        print(
            f"{conflict.type.value:<18} "
            f"{conflict.severity.value:<10} "
            f"{comp_str:<20} "
            f"{conflict.message}"
        )

        if verbose and conflict.location:
            print(
                f"  Location: ({conflict.location.x:.3f}, {conflict.location.y:.3f}) mm"
            )

    # Summary
    errors = sum(1 for c in conflicts if c.severity.value == "error")
    warnings = sum(1 for c in conflicts if c.severity.value == "warning")

    print(f"\nTotal: {len(conflicts)} conflicts ({errors} errors, {warnings} warnings)")


def output_summary(conflicts: List[Conflict]):
    """Output conflict summary."""
    if not conflicts:
        print("No placement conflicts found!")
        return

    # Count by type
    by_type: dict = {}
    for c in conflicts:
        t = c.type.value
        if t not in by_type:
            by_type[t] = {"error": 0, "warning": 0}
        by_type[t][c.severity.value] += 1

    print("\nConflict Summary")
    print("=" * 50)

    for ctype, counts in sorted(by_type.items()):
        total = counts["error"] + counts["warning"]
        print(f"  {ctype}: {total} ({counts['error']} errors, {counts['warning']} warnings)")

    errors = sum(1 for c in conflicts if c.severity.value == "error")
    warnings = sum(1 for c in conflicts if c.severity.value == "warning")
    print(f"\nTotal: {len(conflicts)} conflicts ({errors} errors, {warnings} warnings)")


def output_json(conflicts: List[Conflict]):
    """Output conflicts as JSON."""
    print(json.dumps([c.to_dict() for c in conflicts], indent=2))


def main(argv: Optional[List[str]] = None) -> int:
    """Main entry point for placement commands."""
    parser = argparse.ArgumentParser(
        prog="kicad-tools placement",
        description="Detect and fix placement conflicts in KiCad PCBs",
    )

    subparsers = parser.add_subparsers(dest="command", help="Commands")

    # Check subcommand
    check_parser = subparsers.add_parser("check", help="Check PCB for placement conflicts")
    check_parser.add_argument("pcb", help="Path to .kicad_pcb file")
    check_parser.add_argument(
        "--format",
        choices=["table", "json", "summary"],
        default="table",
        help="Output format",
    )
    check_parser.add_argument(
        "--pad-clearance",
        type=float,
        default=0.1,
        help="Minimum pad-to-pad clearance in mm (default: 0.1)",
    )
    check_parser.add_argument(
        "--hole-clearance",
        type=float,
        default=0.5,
        help="Minimum hole-to-hole clearance in mm (default: 0.5)",
    )
    check_parser.add_argument(
        "--edge-clearance",
        type=float,
        default=0.3,
        help="Minimum edge clearance in mm (default: 0.3)",
    )
    check_parser.add_argument(
        "--courtyard-margin",
        type=float,
        default=0.25,
        help="Courtyard margin around pads in mm (default: 0.25)",
    )
    check_parser.add_argument("-v", "--verbose", action="store_true", help="Verbose output")

    # Fix subcommand
    fix_parser = subparsers.add_parser("fix", help="Suggest and apply placement fixes")
    fix_parser.add_argument("pcb", help="Path to .kicad_pcb file")
    fix_parser.add_argument(
        "-o", "--output",
        help="Output file path (default: modify in place)",
    )
    fix_parser.add_argument(
        "--strategy",
        choices=["spread", "compact", "anchor"],
        default="spread",
        help="Fix strategy (default: spread)",
    )
    fix_parser.add_argument(
        "--anchor",
        help="Comma-separated list of component references to keep fixed",
    )
    fix_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show suggested fixes without applying",
    )
    fix_parser.add_argument(
        "--pad-clearance",
        type=float,
        default=0.1,
        help="Minimum pad-to-pad clearance in mm",
    )
    fix_parser.add_argument(
        "--hole-clearance",
        type=float,
        default=0.5,
        help="Minimum hole-to-hole clearance in mm",
    )
    fix_parser.add_argument(
        "--edge-clearance",
        type=float,
        default=0.3,
        help="Minimum edge clearance in mm",
    )
    fix_parser.add_argument(
        "--courtyard-margin",
        type=float,
        default=0.25,
        help="Courtyard margin around pads in mm",
    )
    fix_parser.add_argument("-v", "--verbose", action="store_true", help="Verbose output")

    args = parser.parse_args(argv)

    if not args.command:
        parser.print_help()
        return 0

    if args.command == "check":
        return cmd_check(args)
    elif args.command == "fix":
        return cmd_fix(args)

    return 0


if __name__ == "__main__":
    sys.exit(main())
