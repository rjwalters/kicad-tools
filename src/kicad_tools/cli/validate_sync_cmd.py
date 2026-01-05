"""
Schematic-to-PCB netlist synchronization validation CLI.

Checks if schematic and PCB netlists are in sync, reporting mismatches clearly.

Usage:
    kct validate --sync project.kicad_pro
    kct validate --sync design.kicad_sch design.kicad_pcb
    kct validate --sync --schematic design.kicad_sch --pcb design.kicad_pcb
    kct validate --sync project.kicad_pro --format json

Exit Codes:
    0 - No errors (in sync, warnings may be present)
    1 - Errors found (out of sync)
    2 - Warnings found (only with --strict)
"""

import argparse
import json
import sys
from pathlib import Path

from kicad_tools.project import Project
from kicad_tools.validate.netlist import NetlistValidator, SyncIssue, SyncResult


def _classify_file(filepath: str) -> str | None:
    """Classify a file by extension. Returns 'project', 'schematic', 'pcb', or None."""
    lower = filepath.lower()
    if lower.endswith(".kicad_pro"):
        return "project"
    elif lower.endswith(".kicad_sch"):
        return "schematic"
    elif lower.endswith(".kicad_pcb"):
        return "pcb"
    return None


def main(argv: list[str] | None = None) -> int:
    """Main entry point for validate --sync command."""
    parser = argparse.ArgumentParser(
        prog="kct validate --sync",
        description="Check schematic-to-PCB netlist synchronization",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "files",
        nargs="*",
        help="Path(s) to .kicad_pro, .kicad_sch, or .kicad_pcb files",
    )
    parser.add_argument(
        "--schematic",
        "-s",
        help="Path to .kicad_sch file (explicit override)",
    )
    parser.add_argument(
        "--pcb",
        "-p",
        help="Path to .kicad_pcb file (explicit override)",
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

    # Determine schematic and PCB paths from positional args and flags
    schematic_path: Path | None = None
    pcb_path: Path | None = None
    project_path: Path | None = None

    # Classify positional files by extension
    for filepath in args.files or []:
        file_type = _classify_file(filepath)
        if file_type == "project":
            project_path = Path(filepath)
        elif file_type == "schematic":
            schematic_path = Path(filepath)
        elif file_type == "pcb":
            pcb_path = Path(filepath)
        else:
            print(
                f"Error: Unrecognized file type: {filepath}\n"
                f"Expected .kicad_pro, .kicad_sch, or .kicad_pcb",
                file=sys.stderr,
            )
            return 1

    # If project file given, load it to find schematic and PCB
    if project_path:
        if not project_path.exists():
            print(f"Error: Project file not found: {project_path}", file=sys.stderr)
            return 1

        try:
            project = Project.load(project_path)
            # Only use project paths if not already specified via positional args
            if not schematic_path and project._schematic_path:
                schematic_path = project._schematic_path
            if not pcb_path and project._pcb_path:
                pcb_path = project._pcb_path
        except Exception as e:
            print(f"Error loading project: {e}", file=sys.stderr)
            return 1

    # Allow explicit flag overrides
    if args.schematic:
        schematic_path = Path(args.schematic)
    if args.pcb:
        pcb_path = Path(args.pcb)

    # Validate we have both files
    if not schematic_path or not pcb_path:
        if not project_path and not args.files:
            print(
                "Error: Must provide either:\n"
                "  - A project file: kct validate --sync project.kicad_pro\n"
                "  - Schematic and PCB: kct validate --sync design.kicad_sch design.kicad_pcb\n"
                "  - Explicit flags: kct validate --sync -s design.kicad_sch -p design.kicad_pcb",
                file=sys.stderr,
            )
        else:
            if not schematic_path:
                print("Error: Could not find schematic file", file=sys.stderr)
            if not pcb_path:
                print("Error: Could not find PCB file", file=sys.stderr)
        return 1

    if not schematic_path.exists():
        print(f"Error: Schematic not found: {schematic_path}", file=sys.stderr)
        return 1
    if not pcb_path.exists():
        print(f"Error: PCB not found: {pcb_path}", file=sys.stderr)
        return 1

    # Run validation
    try:
        validator = NetlistValidator(schematic_path, pcb_path)
        result = validator.validate()
    except Exception as e:
        print(f"Error during validation: {e}", file=sys.stderr)
        return 1

    # Apply filters
    issues = list(result.issues)
    if args.errors_only:
        issues = [i for i in issues if i.is_error]

    # Create filtered result for output
    filtered_result = SyncResult(issues=issues)

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
    result: SyncResult,
    schematic_path: Path,
    pcb_path: Path,
    verbose: bool = False,
) -> None:
    """Output issues as a formatted table."""
    print(f"\n{'=' * 60}")
    print("NETLIST SYNC VALIDATION")
    print(f"{'=' * 60}")
    print(f"Schematic: {schematic_path.name}")
    print(f"PCB:       {pcb_path.name}")

    print("\nResults:")
    print(f"  Errors:     {result.error_count}")
    print(f"  Warnings:   {result.warning_count}")

    if not result.issues:
        print(f"\n{'=' * 60}")
        print("NETLIST IN SYNC - No issues found")
        return

    # Group by category
    categories = {
        "missing_on_pcb": ("MISSING ON PCB", result.missing_on_pcb),
        "orphaned_on_pcb": ("ORPHANED ON PCB", result.orphaned_on_pcb),
        "net_mismatch": ("NET MISMATCHES", result.net_mismatches),
        "pin_mismatch": ("PIN MISMATCHES", result.pin_mismatches),
    }

    for category_id, (label, issues) in categories.items():
        if not issues:
            continue

        print(f"\n{'-' * 60}")
        print(f"{label} ({len(issues)}):")

        for issue in issues:
            _print_issue(issue, verbose)

    print(f"\n{'=' * 60}")
    if result.error_count > 0:
        print("NETLIST OUT OF SYNC - Fix errors to synchronize")
    else:
        print("NETLIST SYNC WARNING - Review warnings")


def _print_issue(issue: SyncIssue, verbose: bool, indent: str = "  ") -> None:
    """Print a single issue."""
    symbol = "X" if issue.is_error else "!"
    severity = "ERROR" if issue.is_error else "WARNING"

    print(f"\n{indent}[{symbol}] {severity}: {issue.message}")
    print(f"{indent}    Fix: {issue.suggestion}")

    if verbose:
        if issue.reference:
            print(f"{indent}    Reference: {issue.reference}")
        if issue.net_schematic or issue.net_pcb:
            print(f"{indent}    Schematic net: {issue.net_schematic or 'N/A'}")
            print(f"{indent}    PCB net: {issue.net_pcb or 'N/A'}")
        if issue.pin:
            print(f"{indent}    Pin: {issue.pin}")


def output_json(result: SyncResult, schematic_path: Path, pcb_path: Path) -> None:
    """Output issues as JSON."""
    data = {
        "schematic": str(schematic_path),
        "pcb": str(pcb_path),
        "in_sync": result.in_sync,
        "summary": {
            "errors": result.error_count,
            "warnings": result.warning_count,
            "missing_on_pcb": len(result.missing_on_pcb),
            "orphaned_on_pcb": len(result.orphaned_on_pcb),
            "net_mismatches": len(result.net_mismatches),
            "pin_mismatches": len(result.pin_mismatches),
        },
        "issues": [i.to_dict() for i in result.issues],
    }
    print(json.dumps(data, indent=2))


def output_summary(result: SyncResult, schematic_path: Path, pcb_path: Path) -> None:
    """Output brief summary."""
    status = "IN SYNC" if result.in_sync else "OUT OF SYNC"
    print(f"Netlist Sync: {status}")
    print(f"Schematic: {schematic_path.name}")
    print(f"PCB: {pcb_path.name}")
    print("=" * 40)

    if result.missing_on_pcb:
        print(f"Missing on PCB:    {len(result.missing_on_pcb)}")
    if result.orphaned_on_pcb:
        print(f"Orphaned on PCB:   {len(result.orphaned_on_pcb)}")
    if result.net_mismatches:
        print(f"Net mismatches:    {len(result.net_mismatches)}")
    if result.pin_mismatches:
        print(f"Pin mismatches:    {len(result.pin_mismatches)}")

    print("-" * 40)
    print(f"Total errors:      {result.error_count}")
    print(f"Total warnings:    {result.warning_count}")


if __name__ == "__main__":
    sys.exit(main())
