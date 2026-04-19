"""
Schematic-to-PCB synchronization CLI.

Analyzes mismatches between schematic and PCB and optionally applies fixes.

Usage:
    kct sync --analyze project.kicad_pro
    kct sync --analyze --schematic design.kicad_sch --pcb design.kicad_pcb
    kct sync --apply --dry-run project.kicad_pro
    kct sync --apply --confirm project.kicad_pro

Exit Codes:
    0 - Success (in sync, or changes applied)
    1 - Error (file not found, invalid arguments)
    2 - Out of sync (analysis found mismatches, no --apply)
"""

import json
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    """Main entry point for kct sync command."""
    import argparse

    parser = argparse.ArgumentParser(
        prog="kct sync",
        description="Reconcile schematic and PCB references",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    # Mode selection
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--analyze",
        action="store_true",
        help="Analyze mismatches and report proposed changes",
    )
    mode.add_argument(
        "--apply",
        action="store_true",
        help="Apply proposed changes (requires --dry-run or --confirm)",
    )

    # File arguments
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

    # Output options
    parser.add_argument(
        "--format",
        choices=["table", "json"],
        default="table",
        help="Output format (default: table)",
    )
    parser.add_argument(
        "--output-mapping",
        "-m",
        help="Save analysis mapping to JSON file",
    )
    parser.add_argument(
        "-o",
        "--output",
        help="Write modified PCB to this file instead of overwriting",
    )

    # Apply options
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would change without modifying files",
    )
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="Actually apply changes (required with --apply)",
    )
    parser.add_argument(
        "--min-confidence",
        choices=["high", "medium", "low"],
        default="high",
        help="Minimum confidence level to apply (default: high)",
    )

    args = parser.parse_args(argv)

    # Validate apply mode requires either --dry-run or --confirm
    if args.apply and not args.dry_run and not args.confirm:
        print(
            "Error: --apply requires either --dry-run or --confirm",
            file=sys.stderr,
        )
        return 1

    # Build reconciler kwargs
    kwargs = {}
    if args.project:
        kwargs["project"] = args.project
    if args.schematic:
        kwargs["schematic"] = args.schematic
    if args.pcb:
        kwargs["pcb"] = args.pcb

    if not kwargs:
        print(
            "Error: Must provide either project file or both --schematic and --pcb",
            file=sys.stderr,
        )
        return 1

    try:
        from kicad_tools.sync.reconciler import Reconciler

        reconciler = Reconciler(**kwargs)
    except (FileNotFoundError, ValueError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    # Run analysis
    try:
        analysis = reconciler.analyze()
    except Exception as e:
        print(f"Error during analysis: {e}", file=sys.stderr)
        return 1

    # Save mapping if requested
    if args.output_mapping:
        try:
            reconciler.save_mapping(analysis, args.output_mapping)
            print(f"Mapping saved to: {args.output_mapping}", file=sys.stderr)
        except Exception as e:
            print(f"Error saving mapping: {e}", file=sys.stderr)
            return 1

    if args.analyze:
        # Analysis mode: report results
        if args.format == "json":
            _output_json(analysis)
        else:
            _output_table(analysis)

        return 0 if analysis.is_in_sync else 2

    elif args.apply:
        # Apply mode
        try:
            changes = reconciler.apply(
                analysis,
                dry_run=args.dry_run,
                min_confidence=args.min_confidence,
                output=args.output,
            )
        except Exception as e:
            print(f"Error applying changes: {e}", file=sys.stderr)
            return 1

        if args.format == "json":
            _output_changes_json(changes, args.dry_run)
        else:
            _output_changes_table(changes, args.dry_run)

        return 0

    return 0


def _output_table(analysis) -> None:
    """Output analysis as a formatted table."""
    print(f"\n{'=' * 60}")
    print("SCHEMATIC <-> PCB SYNC ANALYSIS")
    print(f"{'=' * 60}")

    if analysis.is_in_sync:
        print("\nIN SYNC - No changes needed.")
        return

    print(f"\n{analysis.summary()}")

    # Detail sections
    if analysis.value_mismatches:
        print(f"\n{'-' * 60}")
        print(f"VALUE MISMATCHES ({len(analysis.value_mismatches)}):")
        for mm in analysis.value_mismatches:
            print(f"  {mm['reference']}:")
            print(f"    Schematic: {mm['schematic_value']}")
            print(f"    PCB:       {mm['pcb_value']}")

    if analysis.footprint_mismatches:
        print(f"\n{'-' * 60}")
        print(f"FOOTPRINT MISMATCHES ({len(analysis.footprint_mismatches)}):")
        for mm in analysis.footprint_mismatches:
            print(f"  {mm['reference']}:")
            print(f"    Schematic: {mm['schematic_footprint']}")
            print(f"    PCB:       {mm['pcb_footprint']}")

    if analysis.medium_confidence_matches or analysis.low_confidence_matches:
        print(f"\n{'-' * 60}")
        print("PROPOSED REFERENCE RENAMES:")
        for match in analysis.medium_confidence_matches:
            print(f"  [{match.confidence}] {match.pcb_ref} -> {match.schematic_ref}")
            print(f"    Matched by: {match.match_type}")
        for match in analysis.low_confidence_matches:
            print(f"  [{match.confidence}] {match.pcb_ref} -> {match.schematic_ref}")
            print(f"    Matched by: {match.match_type}")

    if analysis.add_footprint_actions:
        print(f"\n{'-' * 60}")
        print(f"ADD FOOTPRINT ({len(analysis.add_footprint_actions)}):")
        for action in analysis.add_footprint_actions:
            ref = action["reference"]
            fp = action.get("footprint", "")
            val = action.get("value", "")
            print(f"  {ref}: {fp} ({val}) - needs placement on PCB")

    if analysis.schematic_orphans:
        print(f"\n{'-' * 60}")
        print(f"SCHEMATIC-ONLY ({len(analysis.schematic_orphans)}):")
        for ref in analysis.schematic_orphans:
            print(f"  {ref} - missing from PCB")

    if analysis.pcb_orphans:
        print(f"\n{'-' * 60}")
        print(f"PCB-ONLY ({len(analysis.pcb_orphans)}):")
        for ref in analysis.pcb_orphans:
            print(f"  {ref} - not in schematic")

    print(f"\n{'=' * 60}")


def _output_json(analysis) -> None:
    """Output analysis as JSON."""
    print(json.dumps(analysis.to_dict(), indent=2))


def _output_changes_table(changes, dry_run: bool) -> None:
    """Output applied changes as a table."""
    mode = "DRY RUN" if dry_run else "APPLIED"
    print(f"\n{'=' * 60}")
    print(f"SYNC CHANGES ({mode})")
    print(f"{'=' * 60}")

    if not changes:
        print("\nNo changes to apply.")
        return

    for change in changes:
        status = "(would apply)" if dry_run else "(applied)"
        if change.change_type == "update_footprint":
            status = "(manual - footprint change invalidates routing)"
        elif change.change_type == "add_footprint":
            status = "(manual - requires KiCad libraries for placement)"

        print(f"\n  {change.change_type}: {change.reference}")
        if change.change_type == "add_footprint":
            print(f"    {change.new_value} {status}")
        else:
            print(f"    {change.old_value} -> {change.new_value} {status}")

    print(f"\n{'=' * 60}")
    print(f"Total: {len(changes)} change(s)")
    if dry_run:
        print("Use --apply --confirm to apply these changes.")


def _output_changes_json(changes, dry_run: bool) -> None:
    """Output applied changes as JSON."""
    data = {
        "dry_run": dry_run,
        "changes": [c.to_dict() for c in changes],
        "total": len(changes),
    }
    print(json.dumps(data, indent=2))


if __name__ == "__main__":
    sys.exit(main())
