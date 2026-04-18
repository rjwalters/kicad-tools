"""
Layout-vs-Schematic (LVS) validation CLI.

Compares schematic components (with hierarchical sub-sheet support) against
PCB footprints using multi-pass fuzzy matching.

Matching passes:
  1. Exact:           ref + value + footprint (confidence 1.0)
  2. Value+footprint: unique value+footprint pair across refs (confidence 0.8)
  3. Value+prefix:    unique value within same ref prefix (confidence 0.6)
  4. Net-based:       pad net correlation within prefix (confidence 0.4)

Usage:
    kct validate --lvs project.kicad_pro
    kct validate --lvs --schematic design.kicad_sch --pcb design.kicad_pcb
    kct validate --lvs project.kicad_pro --format json
    kct validate --lvs project.kicad_pro --min-confidence 0.5

Exit Codes:
    0 - Clean (all exact matches, no orphans)
    1 - Mismatches found (fuzzy matches or orphans)
    2 - Warnings only (with --strict)
"""

import argparse
import json
import sys
from pathlib import Path

from kicad_tools.project import Project
from kicad_tools.validate.consistency import (
    LVSMatch,
    LVSResult,
    SchematicPCBChecker,
)


def main(argv: list[str] | None = None) -> int:
    """Main entry point for validate --lvs command."""
    parser = argparse.ArgumentParser(
        prog="kct validate --lvs",
        description="Layout-vs-Schematic check with hierarchical support",
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
        help="Show only unmatched/fuzzy-matched components (hide exact matches)",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit with error code on fuzzy matches (not just orphans)",
    )
    parser.add_argument(
        "--min-confidence",
        type=float,
        default=0.0,
        help="Minimum match confidence to display (0.0-1.0, default: 0.0)",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Show detailed match information",
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

    # Run LVS check
    try:
        checker = SchematicPCBChecker(schematic_path, pcb_path)
        result = checker.check_lvs()
    except Exception as e:
        print(f"Error during LVS check: {e}", file=sys.stderr)
        return 1

    # Apply confidence filter
    if args.min_confidence > 0.0:
        result = LVSResult(
            matches=[m for m in result.matches if m.confidence >= args.min_confidence],
            unmatched_pcb=result.unmatched_pcb,
            unmatched_sch=result.unmatched_sch,
        )

    # Output
    if args.format == "json":
        output_json(result, schematic_path, pcb_path)
    elif args.format == "summary":
        output_summary(result, schematic_path, pcb_path)
    else:
        output_table(result, schematic_path, pcb_path, args.verbose, args.errors_only)

    # Exit code
    if result.unmatched_pcb or result.unmatched_sch:
        return 1
    if result.fuzzy_match_count > 0 and args.strict:
        return 2
    if result.fuzzy_match_count > 0:
        return 1
    return 0


def output_table(
    result: LVSResult,
    schematic_path: Path,
    pcb_path: Path,
    verbose: bool = False,
    errors_only: bool = False,
) -> None:
    """Output LVS results as a formatted table."""
    print(f"\n{'=' * 60}")
    print("LAYOUT vs SCHEMATIC (LVS) CHECK")
    print(f"{'=' * 60}")
    print(f"Schematic: {schematic_path.name}")
    print(f"PCB:       {pcb_path.name}")

    print("\nResults:")
    print(f"  Exact matches:   {result.exact_match_count}")
    if result.fuzzy_match_count:
        print(f"  Fuzzy matches:   {result.fuzzy_match_count}")
    if result.unmatched_pcb:
        print(f"  Unmatched (PCB): {len(result.unmatched_pcb)}")
    if result.unmatched_sch:
        print(f"  Unmatched (SCH): {len(result.unmatched_sch)}")

    # Show exact matches unless --errors-only
    if not errors_only:
        exact = [m for m in result.matches if m.confidence >= 1.0]
        if exact:
            print(f"\n{'-' * 60}")
            print(f"EXACT MATCHES ({len(exact)}):")
            for m in exact:
                print(f"  {m.sch_ref} = {m.pcb_ref}")

    # Show fuzzy matches
    fuzzy = [m for m in result.matches if m.confidence < 1.0]
    if fuzzy:
        print(f"\n{'-' * 60}")
        print(f"FUZZY MATCHES ({len(fuzzy)}):")
        for m in sorted(fuzzy, key=lambda x: x.confidence):
            _print_match(m, verbose)

    # Show orphans
    if result.unmatched_sch:
        print(f"\n{'-' * 60}")
        print(f"UNMATCHED SCHEMATIC COMPONENTS ({len(result.unmatched_sch)}):")
        for ref in result.unmatched_sch:
            print(f"  {ref} - no PCB footprint found")

    if result.unmatched_pcb:
        print(f"\n{'-' * 60}")
        print(f"UNMATCHED PCB COMPONENTS ({len(result.unmatched_pcb)}):")
        for ref in result.unmatched_pcb:
            print(f"  {ref} - no schematic symbol found")

    print(f"\n{'=' * 60}")
    if result.is_clean:
        print("LVS CLEAN - All components matched exactly")
    else:
        print("LVS MISMATCHES - Review fuzzy matches and orphans")


def _print_match(match: LVSMatch, verbose: bool, indent: str = "  ") -> None:
    """Print a single fuzzy match."""
    conf_pct = f"{match.confidence * 100:.0f}%"
    value_icon = "v" if match.value_match else "X"
    fp_icon = "v" if match.footprint_match else "X"

    print(f"{indent}{match.sch_ref} -> {match.pcb_ref} ({conf_pct} confidence)")

    if verbose:
        print(f"{indent}    Reason: {match.match_reason}")
        print(f"{indent}    Value:     [{value_icon}]  Footprint: [{fp_icon}]")


def output_json(result: LVSResult, schematic_path: Path, pcb_path: Path) -> None:
    """Output LVS results as JSON."""
    data = {
        "schematic": str(schematic_path),
        "pcb": str(pcb_path),
        "is_clean": result.is_clean,
        **result.to_dict(),
    }
    print(json.dumps(data, indent=2))


def output_summary(result: LVSResult, schematic_path: Path, pcb_path: Path) -> None:
    """Output brief LVS summary."""
    status = "CLEAN" if result.is_clean else "MISMATCHES"
    print(f"LVS: {status}")
    print(f"Schematic: {schematic_path.name}")
    print(f"PCB: {pcb_path.name}")
    print("=" * 40)
    print(result.summary())


if __name__ == "__main__":
    sys.exit(main())
