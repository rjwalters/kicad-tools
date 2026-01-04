"""
BOM-to-PCB placement validation CLI.

Verifies that all BOM components are placed on the PCB, identifying:
- Components in BOM but missing from PCB
- Components in PCB but at origin (unplaced)

Usage:
    kct validate --placement project.kicad_pro
    kct validate --placement --schematic design.kicad_sch --pcb design.kicad_pcb
    kct validate --placement project.kicad_pro --format json

Exit Codes:
    0 - All components placed
    1 - Some components not placed or missing
    2 - Warnings only (with --strict)
"""

import argparse
import json
import sys
from pathlib import Path

from kicad_tools.project import Project
from kicad_tools.validate.placement import (
    BOMPlacementVerifier,
    PlacementResult,
    PlacementStatus,
)


def main(argv: list[str] | None = None) -> int:
    """Main entry point for validate --placement command."""
    parser = argparse.ArgumentParser(
        prog="kct validate --placement",
        description="Verify BOM components are placed on PCB",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "project",
        nargs="?",
        help="Path to .kicad_pro file (auto-finds schematic and PCB)",
    )
    parser.add_argument(
        "--schematic",
        "-s",
        help="Path to .kicad_sch file (required if no project file)",
    )
    parser.add_argument(
        "--pcb",
        "-p",
        help="Path to .kicad_pcb file (required if no project file)",
    )
    parser.add_argument(
        "--format",
        choices=["table", "json", "summary"],
        default="table",
        help="Output format (default: table)",
    )
    parser.add_argument(
        "--errors-only",
        action="store_true",
        help="Show only unplaced/missing components",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit with error code on any issues",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Show detailed placement information",
    )

    args = parser.parse_args(argv)

    # Determine schematic and PCB paths
    schematic_path: Path | None = None
    pcb_path: Path | None = None

    if args.project:
        project_path = Path(args.project)
        if not project_path.exists():
            print(f"Error: Project file not found: {project_path}", file=sys.stderr)
            return 1

        # Load project to find schematic and PCB
        try:
            project = Project.load(project_path)
            if project._schematic_path:
                schematic_path = project._schematic_path
            if project._pcb_path:
                pcb_path = project._pcb_path
        except Exception as e:
            print(f"Error loading project: {e}", file=sys.stderr)
            return 1

    # Allow overrides
    if args.schematic:
        schematic_path = Path(args.schematic)
    if args.pcb:
        pcb_path = Path(args.pcb)

    # Validate we have both files
    if not schematic_path or not pcb_path:
        if not args.project:
            print(
                "Error: Must provide either project file or both --schematic and --pcb",
                file=sys.stderr,
            )
        else:
            if not schematic_path:
                print("Error: Could not find schematic file in project", file=sys.stderr)
            if not pcb_path:
                print("Error: Could not find PCB file in project", file=sys.stderr)
        return 1

    if not schematic_path.exists():
        print(f"Error: Schematic not found: {schematic_path}", file=sys.stderr)
        return 1
    if not pcb_path.exists():
        print(f"Error: PCB not found: {pcb_path}", file=sys.stderr)
        return 1

    # Run verification
    try:
        verifier = BOMPlacementVerifier(schematic_path, pcb_path)
        result = verifier.verify()
    except Exception as e:
        print(f"Error during verification: {e}", file=sys.stderr)
        return 1

    # Output
    if args.format == "json":
        output_json(result, schematic_path, pcb_path)
    elif args.format == "summary":
        output_summary(result, schematic_path, pcb_path)
    else:
        output_table(result, schematic_path, pcb_path, args.verbose, args.errors_only)

    # Exit code
    if result.unplaced_count > 0:
        return 1
    elif args.strict and not result.all_placed:
        return 2
    return 0


def output_table(
    result: PlacementResult,
    schematic_path: Path,
    pcb_path: Path,
    verbose: bool = False,
    errors_only: bool = False,
) -> None:
    """Output results as a formatted table."""
    print(f"\n{'=' * 60}")
    print("BOM ↔ PLACEMENT VERIFICATION")
    print(f"{'=' * 60}")
    print(f"Schematic: {schematic_path.name}")
    print(f"PCB:       {pcb_path.name}")

    print(f"\nPlaced: {result.placed_count}/{result.total_count} components")

    if result.all_placed:
        print(f"\n{'=' * 60}")
        print("ALL PLACED - All BOM components are on the board")
        return

    # Show unplaced components
    if result.unplaced:
        print(f"\n{'-' * 60}")
        print(f"UNPLACED ({len(result.unplaced)}):")

        for status in result.unplaced:
            _print_status(status, verbose)

    # Optionally show placed components
    if verbose and not errors_only and result.placed:
        print(f"\n{'-' * 60}")
        print(f"PLACED ({len(result.placed)}):")

        for status in result.placed:
            _print_status(status, verbose)

    # Summary
    print(f"\n{'=' * 60}")
    print("Summary:")
    if result.missing_count > 0:
        print(f"  - {result.missing_count} component(s) in BOM but not in PCB")
    at_origin_count = len(result.at_origin)
    if at_origin_count > 0:
        print(f"  - {at_origin_count} component(s) in PCB but not placed (at origin)")


def _print_status(status: PlacementStatus, verbose: bool, indent: str = "  ") -> None:
    """Print a single placement status."""
    if status.is_placed:
        symbol = "✓"
    else:
        symbol = "✗"

    print(f"\n{indent}{symbol} {status.reference} ({status.value}, {status.footprint})")

    for issue in status.issues:
        print(f"{indent}    → {issue}")

    if verbose and status.position:
        x, y = status.position
        print(f"{indent}    Position: ({x:.2f}, {y:.2f}) on {status.layer}")


def output_json(result: PlacementResult, schematic_path: Path, pcb_path: Path) -> None:
    """Output results as JSON."""
    data = {
        "schematic": str(schematic_path),
        "pcb": str(pcb_path),
        "all_placed": result.all_placed,
        "summary": {
            "total": result.total_count,
            "placed": result.placed_count,
            "unplaced": result.unplaced_count,
            "missing_from_pcb": result.missing_count,
            "at_origin": len(result.at_origin),
        },
        "components": [s.to_dict() for s in result.statuses],
    }
    print(json.dumps(data, indent=2))


def output_summary(result: PlacementResult, schematic_path: Path, pcb_path: Path) -> None:
    """Output brief summary."""
    status = "ALL PLACED" if result.all_placed else "INCOMPLETE"
    print(f"BOM ↔ Placement: {status}")
    print(f"Schematic: {schematic_path.name}")
    print(f"PCB: {pcb_path.name}")
    print("=" * 40)

    print(f"Placed:           {result.placed_count}/{result.total_count}")

    if result.missing_count > 0:
        print(f"Missing from PCB: {result.missing_count}")
    if len(result.at_origin) > 0:
        print(f"At origin:        {len(result.at_origin)}")

    print("-" * 40)
    if result.all_placed:
        print("Status: OK")
    else:
        print(f"Status: {result.unplaced_count} component(s) need attention")


if __name__ == "__main__":
    sys.exit(main())
