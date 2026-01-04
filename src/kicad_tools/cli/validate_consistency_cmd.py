"""
Schematic-to-PCB consistency validation CLI.

Checks if schematic and PCB are consistent, reporting mismatches in:
- Components (missing/extra)
- Net connectivity
- Properties (value, footprint)

Usage:
    kct validate --consistency project.kicad_pro
    kct validate --consistency --schematic design.kicad_sch --pcb design.kicad_pcb
    kct validate --consistency project.kicad_pro --format json

Exit Codes:
    0 - No errors (consistent, warnings may be present)
    1 - Errors found (inconsistent)
    2 - Warnings found (only with --strict)
"""

import argparse
import json
import sys
from pathlib import Path

from kicad_tools.project import Project
from kicad_tools.validate.consistency import (
    ConsistencyIssue,
    ConsistencyResult,
    SchematicPCBChecker,
)


def main(argv: list[str] | None = None) -> int:
    """Main entry point for validate --consistency command."""
    parser = argparse.ArgumentParser(
        prog="kct validate --consistency",
        description="Check schematic-to-PCB consistency",
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
        help="Show only errors, not warnings",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit with error code on warnings",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Show detailed issue information",
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

    # Run validation
    try:
        checker = SchematicPCBChecker(schematic_path, pcb_path)
        result = checker.check()
    except Exception as e:
        print(f"Error during validation: {e}", file=sys.stderr)
        return 1

    # Apply filters
    issues = list(result.issues)
    if args.errors_only:
        issues = [i for i in issues if i.is_error]

    # Create filtered result for output
    filtered_result = ConsistencyResult(issues=issues)

    # Output
    if args.format == "json":
        output_json(filtered_result, schematic_path, pcb_path)
    elif args.format == "summary":
        output_summary(filtered_result, schematic_path, pcb_path)
    else:
        output_table(filtered_result, schematic_path, pcb_path, args.verbose)

    # Exit code
    if filtered_result.error_count > 0:
        return 1
    elif filtered_result.warning_count > 0 and args.strict:
        return 2
    return 0


def output_table(
    result: ConsistencyResult,
    schematic_path: Path,
    pcb_path: Path,
    verbose: bool = False,
) -> None:
    """Output issues as a formatted table."""
    print(f"\n{'=' * 60}")
    print("SCHEMATIC ↔ PCB CONSISTENCY CHECK")
    print(f"{'=' * 60}")
    print(f"Schematic: {schematic_path.name}")
    print(f"PCB:       {pcb_path.name}")

    print("\nResults:")
    print(f"  Errors:     {result.error_count}")
    print(f"  Warnings:   {result.warning_count}")

    if not result.issues:
        print(f"\n{'=' * 60}")
        print("CONSISTENT - No issues found")
        return

    # Group by domain
    domains = {
        "component": ("COMPONENT ISSUES", result.component_issues),
        "net": ("NET ISSUES", result.net_issues),
        "property": ("PROPERTY ISSUES", result.property_issues),
    }

    for domain_id, (label, issues) in domains.items():
        if not issues:
            continue

        print(f"\n{'-' * 60}")
        print(f"{label} ({len(issues)}):")

        for issue in issues:
            _print_issue(issue, verbose)

    print(f"\n{'=' * 60}")
    if result.error_count > 0:
        print("INCONSISTENT - Fix errors to synchronize schematic and PCB")
    else:
        print("CONSISTENCY WARNING - Review warnings")


def _print_issue(issue: ConsistencyIssue, verbose: bool, indent: str = "  ") -> None:
    """Print a single issue."""
    symbol = "✗" if issue.is_error else "⚠"
    severity = "ERROR" if issue.is_error else "WARNING"

    print(f"\n{indent}{symbol} {severity}: {issue.reference}")
    print(f"{indent}    → {issue.suggestion}")

    if verbose:
        if issue.schematic_value is not None:
            print(f"{indent}    Schematic: {issue.schematic_value}")
        if issue.pcb_value is not None:
            print(f"{indent}    PCB: {issue.pcb_value}")
        print(f"{indent}    Type: {issue.issue_type} ({issue.domain})")


def output_json(result: ConsistencyResult, schematic_path: Path, pcb_path: Path) -> None:
    """Output issues as JSON."""
    data = {
        "schematic": str(schematic_path),
        "pcb": str(pcb_path),
        "is_consistent": result.is_consistent,
        "summary": {
            "errors": result.error_count,
            "warnings": result.warning_count,
            "component_issues": len(result.component_issues),
            "net_issues": len(result.net_issues),
            "property_issues": len(result.property_issues),
        },
        "issues": [i.to_dict() for i in result.issues],
    }
    print(json.dumps(data, indent=2))


def output_summary(result: ConsistencyResult, schematic_path: Path, pcb_path: Path) -> None:
    """Output brief summary."""
    status = "CONSISTENT" if result.is_consistent else "INCONSISTENT"
    print(f"Schematic ↔ PCB: {status}")
    print(f"Schematic: {schematic_path.name}")
    print(f"PCB: {pcb_path.name}")
    print("=" * 40)

    if result.component_issues:
        errors = sum(1 for i in result.component_issues if i.is_error)
        print(f"Component issues: {len(result.component_issues)} ({errors} errors)")
    if result.net_issues:
        errors = sum(1 for i in result.net_issues if i.is_error)
        print(f"Net issues:       {len(result.net_issues)} ({errors} errors)")
    if result.property_issues:
        errors = sum(1 for i in result.property_issues if i.is_error)
        print(f"Property issues:  {len(result.property_issues)} ({errors} errors)")

    print("-" * 40)
    print(f"Total errors:     {result.error_count}")
    print(f"Total warnings:   {result.warning_count}")


if __name__ == "__main__":
    sys.exit(main())
