#!/usr/bin/env python3
"""
Repair clearance violations by nudging traces and vias.

Unlike the destructive fix_clearance_violations in DRCFixer (which deletes traces),
this tool computes minimal displacements to achieve the required clearance.

Usage:
    kicad-tools repair-clearance board.kicad_pcb --mfr jlcpcb
    kicad-tools repair-clearance board.kicad_pcb --max-displacement 0.1
    kicad-tools repair-clearance board.kicad_pcb --dry-run
    kicad-tools repair-clearance board.kicad_pcb --prefer move-via

Examples:
    # Fix clearance violations using JLCPCB rules (default)
    kct repair-clearance board.kicad_pcb --mfr jlcpcb

    # Preview changes without applying
    kct repair-clearance board.kicad_pcb --dry-run

    # Limit maximum displacement to 0.05mm
    kct repair-clearance board.kicad_pcb --max-displacement 0.05

    # Prefer moving vias instead of traces
    kct repair-clearance board.kicad_pcb --prefer move-via

    # Output to a different file
    kct repair-clearance board.kicad_pcb -o fixed_board.kicad_pcb

    # Use a DRC report instead of running DRC
    kct repair-clearance board.kicad_pcb --drc-report board-drc.rpt
"""

import argparse
import json
import sys
from pathlib import Path

from kicad_tools.drc.repair_clearance import ClearanceRepairer, NudgeResult, RepairResult
from kicad_tools.drc.report import DRCReport
from kicad_tools.manufacturers import get_manufacturer_ids


def main(argv: list[str] | None = None) -> int:
    """Main entry point for repair-clearance command."""
    parser = argparse.ArgumentParser(
        prog="kicad-tools repair-clearance",
        description="Repair clearance violations by nudging traces and vias",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    # Fix clearance violations using JLCPCB rules
    kct repair-clearance board.kicad_pcb --mfr jlcpcb

    # Preview changes without modifying the PCB
    kct repair-clearance board.kicad_pcb --dry-run

    # Limit displacement and prefer moving vias
    kct repair-clearance board.kicad_pcb --max-displacement 0.05 --prefer move-via

    # Use an existing DRC report
    kct repair-clearance board.kicad_pcb --drc-report board-drc.rpt
        """,
    )
    parser.add_argument("pcb", help="Path to .kicad_pcb file")
    parser.add_argument(
        "--drc-report",
        help="Path to existing DRC report (.rpt or .json). If not provided, requires kicad-cli.",
    )
    parser.add_argument(
        "--mfr",
        "-m",
        choices=get_manufacturer_ids(),
        help="Target manufacturer (for clearance rules context)",
    )
    parser.add_argument(
        "--max-displacement",
        type=float,
        default=0.1,
        help="Maximum nudge distance in mm (default: 0.1)",
    )
    parser.add_argument(
        "--margin",
        type=float,
        default=0.01,
        help="Extra clearance margin beyond minimum in mm (default: 0.01)",
    )
    parser.add_argument(
        "--prefer",
        choices=["move-trace", "move-via"],
        default="move-trace",
        help="Which object to move when both are movable (default: move-trace)",
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

    # Get DRC report
    report = _get_drc_report(args.drc_report, pcb_path)
    if report is None:
        return 1

    # Count clearance violations
    from kicad_tools.drc.violation import ViolationType

    clearance_count = len(report.by_type(ViolationType.CLEARANCE))
    if clearance_count == 0:
        if not args.quiet:
            print("No clearance violations found. Nothing to repair.")
        return 0

    if not args.quiet and args.format == "text":
        print(f"Found {clearance_count} clearance violation(s) to repair")

    # Create repairer and run
    try:
        repairer = ClearanceRepairer(pcb_path)
    except Exception as e:
        print(f"Error loading PCB file: {e}", file=sys.stderr)
        return 1

    result = repairer.repair_from_report(
        report,
        max_displacement=args.max_displacement,
        margin=args.margin,
        prefer=args.prefer,
        dry_run=args.dry_run,
    )

    # Print results
    if not args.quiet:
        _print_results(result, args.format, args.dry_run, args.max_displacement, args.mfr)

    # Save if not dry run and there were repairs
    if result.repaired > 0 and not args.dry_run:
        output_path = Path(args.output) if args.output else pcb_path
        try:
            repairer.save(output_path)
            if not args.quiet and args.format == "text":
                print(f"\nSaved to: {output_path}")
        except Exception as e:
            print(f"Error saving PCB file: {e}", file=sys.stderr)
            return 1

    # Return non-zero if there are unrepaired violations
    unrepaired = result.total_violations - result.repaired
    return 1 if unrepaired > 0 else 0


def _get_drc_report(drc_report_path: str | None, pcb_path: Path) -> DRCReport | None:
    """Load or generate a DRC report."""
    if drc_report_path:
        report_path = Path(drc_report_path)
        if not report_path.exists():
            print(f"Error: DRC report not found: {report_path}", file=sys.stderr)
            return None
        try:
            return DRCReport.load(report_path)
        except Exception as e:
            print(f"Error loading DRC report: {e}", file=sys.stderr)
            return None

    # Try to run DRC using kicad-cli
    try:
        from kicad_tools.cli.runner import find_kicad_cli, run_drc

        kicad_cli = find_kicad_cli()
        if not kicad_cli:
            print(
                "Error: No DRC report provided and kicad-cli not found.",
                file=sys.stderr,
            )
            print(
                "Provide a DRC report with --drc-report, or install KiCad 8.",
                file=sys.stderr,
            )
            return None

        print(f"Running DRC on: {pcb_path.name}")
        drc_result = run_drc(pcb_path)
        if not drc_result.success:
            print(f"Error running DRC: {drc_result.stderr}", file=sys.stderr)
            return None

        report = DRCReport.load(drc_result.output_path)
        # Clean up temporary report file
        if drc_result.output_path:
            drc_result.output_path.unlink(missing_ok=True)
        return report
    except ImportError:
        print(
            "Error: No DRC report provided and kicad-cli runner not available.",
            file=sys.stderr,
        )
        print(
            "Provide a DRC report with --drc-report.",
            file=sys.stderr,
        )
        return None


def _print_results(
    result: RepairResult,
    output_format: str,
    dry_run: bool,
    max_displacement: float,
    mfr: str | None,
) -> None:
    """Print repair results."""
    if output_format == "json":
        _print_json(result, dry_run, max_displacement, mfr)
    elif output_format == "summary":
        _print_summary(result, dry_run)
    else:
        _print_text(result, dry_run, max_displacement, mfr)


def _print_json(
    result: RepairResult,
    dry_run: bool,
    max_displacement: float,
    mfr: str | None,
) -> None:
    """Print results as JSON."""
    data = {
        "dry_run": dry_run,
        "max_displacement_mm": max_displacement,
        "manufacturer": mfr,
        "total_violations": result.total_violations,
        "repaired": result.repaired,
        "skipped": {
            "no_location": result.skipped_no_location,
            "no_delta": result.skipped_no_delta,
            "exceeds_max": result.skipped_exceeds_max,
            "infeasible": result.skipped_infeasible,
        },
        "nudges": [
            {
                "object_type": n.object_type,
                "x": n.x,
                "y": n.y,
                "net_name": n.net_name,
                "layer": n.layer,
                "displacement_x_mm": round(n.displacement_x, 4),
                "displacement_y_mm": round(n.displacement_y, 4),
                "displacement_mm": round(n.displacement_mm, 4),
                "old_clearance_mm": round(n.old_clearance_mm, 4),
                "new_clearance_mm": round(n.new_clearance_mm, 4),
                "uuid": n.uuid,
            }
            for n in result.nudges
        ],
    }
    print(json.dumps(data, indent=2))


def _print_summary(result: RepairResult, dry_run: bool) -> None:
    """Print a compact summary."""
    action = "Would repair" if dry_run else "Repaired"
    print(f"{action} {result.repaired}/{result.total_violations} clearance violations")
    unrepaired = result.total_violations - result.repaired
    if unrepaired > 0:
        print(f"  {unrepaired} violations could not be repaired")


def _print_text(
    result: RepairResult,
    dry_run: bool,
    max_displacement: float,
    mfr: str | None,
) -> None:
    """Print detailed text output."""
    action = "Would repair" if dry_run else "Repaired"
    mfr_str = f" (target: {mfr.upper()})" if mfr else ""

    print(f"\n{'=' * 60}")
    print(f"CLEARANCE REPAIR{mfr_str}")
    print(f"{'=' * 60}")
    print(f"Max displacement: {max_displacement}mm")
    print(f"Mode: {'DRY RUN' if dry_run else 'APPLY'}")
    print(f"\n{action} {result.repaired}/{result.total_violations} clearance violations")

    if result.nudges:
        print(f"\n{'-' * 60}")
        print("NUDGES:")

        # Show all nudges if 5 or fewer, otherwise show first 3
        display_nudges = result.nudges if len(result.nudges) <= 5 else result.nudges[:3]
        for nudge in display_nudges:
            _print_nudge(nudge)

        if len(result.nudges) > 5:
            print(f"\n  ... and {len(result.nudges) - 3} more")

    # Show skipped details
    skipped_total = (
        result.skipped_exceeds_max
        + result.skipped_infeasible
        + result.skipped_no_location
        + result.skipped_no_delta
    )
    if skipped_total > 0:
        print(f"\n{'-' * 60}")
        print(f"SKIPPED ({skipped_total}):")
        if result.skipped_exceeds_max > 0:
            print(f"  Exceeds max displacement: {result.skipped_exceeds_max}")
        if result.skipped_infeasible > 0:
            print(f"  Infeasible (no movable object): {result.skipped_infeasible}")
        if result.skipped_no_location > 0:
            print(f"  No location info: {result.skipped_no_location}")
        if result.skipped_no_delta > 0:
            print(f"  No clearance delta info: {result.skipped_no_delta}")

    print(f"\n{'=' * 60}")

    if result.repaired == result.total_violations:
        print("All clearance violations repaired!")
    else:
        unrepaired = result.total_violations - result.repaired
        print(f"{unrepaired} violation(s) require manual repair")
        if result.skipped_exceeds_max > 0:
            print(f"  Try increasing --max-displacement (currently {max_displacement}mm)")


def _print_nudge(nudge: NudgeResult) -> None:
    """Print a single nudge result."""
    print(f"\n  [{nudge.object_type.upper()}] {nudge.net_name}")
    print(f"    Position: ({nudge.x:.4f}, {nudge.y:.4f}) on {nudge.layer}")
    print(
        f"    Displacement: ({nudge.displacement_x:+.4f}, {nudge.displacement_y:+.4f}) mm "
        f"= {nudge.displacement_mm:.4f} mm"
    )
    print(
        f"    Clearance: {nudge.old_clearance_mm:.4f} -> {nudge.new_clearance_mm:.4f} mm"
    )


if __name__ == "__main__":
    sys.exit(main())
