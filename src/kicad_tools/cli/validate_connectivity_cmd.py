"""
Net connectivity validation CLI.

Checks if all schematic net connections are physically routed on the PCB,
detecting unrouted segments and partially connected nets (islands).

Usage:
    kct validate --connectivity board.kicad_pcb
    kct validate --connectivity board.kicad_pcb --format json
    kct validate --connectivity board.kicad_pcb --errors-only

Exit Codes:
    0 - No errors (fully routed, warnings may be present)
    1 - Errors found (unrouted connections)
    2 - Warnings found (only with --strict)
"""

import argparse
import json
import sys
from pathlib import Path

from kicad_tools.validate.connectivity import (
    ConnectivityIssue,
    ConnectivityResult,
    ConnectivityValidator,
)


def main(argv: list[str] | None = None) -> int:
    """Main entry point for validate --connectivity command."""
    parser = argparse.ArgumentParser(
        prog="kct validate --connectivity",
        description="Check net connectivity on PCB",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "pcb",
        help="Path to .kicad_pcb file",
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

    # Validate PCB path
    pcb_path = Path(args.pcb)
    if not pcb_path.exists():
        print(f"Error: PCB not found: {pcb_path}", file=sys.stderr)
        return 1

    # Run validation
    try:
        validator = ConnectivityValidator(pcb_path)
        result = validator.validate()
    except Exception as e:
        print(f"Error during validation: {e}", file=sys.stderr)
        return 1

    # Apply filters
    issues = list(result.issues)
    if args.errors_only:
        issues = [i for i in issues if i.is_error]

    # Create filtered result for output
    filtered_result = ConnectivityResult(
        issues=issues,
        total_nets=result.total_nets,
        connected_nets=result.connected_nets,
    )

    # Output
    if args.format == "json":
        output_json(filtered_result, pcb_path)
    elif args.format == "summary":
        output_summary(filtered_result, pcb_path)
    else:
        output_table(filtered_result, pcb_path, args.verbose)

    # Exit code
    if filtered_result.error_count > 0:
        return 1
    elif filtered_result.warning_count > 0 and args.strict:
        return 2
    return 0


def output_table(
    result: ConnectivityResult,
    pcb_path: Path,
    verbose: bool = False,
) -> None:
    """Output issues as a formatted table."""
    print(f"\n{'=' * 60}")
    print("NET CONNECTIVITY VALIDATION")
    print(f"{'=' * 60}")
    print(f"PCB: {pcb_path.name}")

    print("\nResults:")
    print(f"  Nets:       {result.connected_nets}/{result.total_nets} fully connected")
    print(f"  Errors:     {result.error_count}")
    print(f"  Warnings:   {result.warning_count}")

    if not result.issues:
        print(f"\n{'=' * 60}")
        print("ALL NETS FULLY ROUTED - No connectivity issues found")
        return

    # Group by issue type
    categories = {
        "unrouted": ("UNROUTED CONNECTIONS", result.unrouted),
        "partial": ("PARTIAL CONNECTIONS (ISLANDS)", result.partial),
        "isolated": ("ISOLATED PADS", result.isolated),
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
        print(f"CONNECTIVITY ISSUES FOUND - {result.unconnected_pad_count} unconnected pads")
    else:
        print("CONNECTIVITY OK - Review warnings if present")


def _print_issue(issue: ConnectivityIssue, verbose: bool, indent: str = "  ") -> None:
    """Print a single issue."""
    symbol = "X" if issue.is_error else "!"
    severity = "ERROR" if issue.is_error else "WARNING"

    print(f"\n{indent}[{symbol}] {severity}: {issue.message}")
    print(f"{indent}    Fix: {issue.suggestion}")

    if verbose:
        if issue.islands:
            for i, island in enumerate(issue.islands, 1):
                island_pads = ", ".join(island[:5])
                if len(island) > 5:
                    island_pads += f" (+{len(island) - 5} more)"
                print(f"{indent}    Island {i}: {island_pads}")
        if issue.connected_pads:
            connected = ", ".join(issue.connected_pads[:5])
            if len(issue.connected_pads) > 5:
                connected += f" (+{len(issue.connected_pads) - 5} more)"
            print(f"{indent}    Connected: {connected}")
        if issue.unconnected_pads:
            unconnected = ", ".join(issue.unconnected_pads[:5])
            if len(issue.unconnected_pads) > 5:
                unconnected += f" (+{len(issue.unconnected_pads) - 5} more)"
            print(f"{indent}    Unconnected: {unconnected}")


def output_json(result: ConnectivityResult, pcb_path: Path) -> None:
    """Output issues as JSON."""
    data = {
        "pcb": str(pcb_path),
        "is_fully_routed": result.is_fully_routed,
        "summary": {
            "total_nets": result.total_nets,
            "connected_nets": result.connected_nets,
            "errors": result.error_count,
            "warnings": result.warning_count,
            "unrouted_count": len(result.unrouted),
            "partial_count": len(result.partial),
            "isolated_count": len(result.isolated),
            "unconnected_pads": result.unconnected_pad_count,
        },
        "issues": [i.to_dict() for i in result.issues],
    }
    print(json.dumps(data, indent=2))


def output_summary(result: ConnectivityResult, pcb_path: Path) -> None:
    """Output brief summary."""
    status = "FULLY ROUTED" if result.is_fully_routed else "CONNECTIVITY ISSUES"
    print(f"Connectivity: {status}")
    print(f"PCB: {pcb_path.name}")
    print("=" * 40)

    print(f"Nets connected:    {result.connected_nets}/{result.total_nets}")
    if result.unrouted:
        print(f"Unrouted:          {len(result.unrouted)}")
    if result.partial:
        print(f"Partial:           {len(result.partial)}")
    if result.isolated:
        print(f"Isolated:          {len(result.isolated)}")

    print("-" * 40)
    print(f"Total errors:      {result.error_count}")
    print(f"Total warnings:    {result.warning_count}")
    print(f"Unconnected pads:  {result.unconnected_pad_count}")


if __name__ == "__main__":
    sys.exit(main())
