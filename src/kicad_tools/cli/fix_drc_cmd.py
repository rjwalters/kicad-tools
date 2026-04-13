#!/usr/bin/env python3
"""Automated DRC violation repair - orchestrates clearance and drill repairs.

This command repairs multiple types of DRC violations:
- clearance_segment_segment: nudge traces via ClearanceRepairer
- dimension_drill_clearance: de-duplicate or slide vias via DrillClearanceRepairer

Usage:
    kct fix-drc board.kicad_pcb --drc-report board-drc.rpt
    kct fix-drc board.kicad_pcb --drc-report board-drc.rpt --dry-run
    kct fix-drc board.kicad_pcb --drc-report board-drc.rpt --only clearance
    kct fix-drc board.kicad_pcb --drc-report board-drc.rpt --only drill-clearance
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from kicad_tools.drc.repair_clearance import ClearanceRepairer, RepairResult
from kicad_tools.drc.repair_drill_clearance import DrillClearanceRepairer, DrillRepairResult
from kicad_tools.drc.report import DRCReport
from kicad_tools.drc.violation import ViolationType


def main(argv: list[str] | None = None) -> int:
    """Main entry point for fix-drc command."""
    parser = argparse.ArgumentParser(
        prog="kicad-tools fix-drc",
        description="Automated DRC violation repair",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    # Repair all DRC violations
    kct fix-drc board.kicad_pcb --drc-report board-drc.rpt

    # Preview changes without modifying the PCB
    kct fix-drc board.kicad_pcb --drc-report board-drc.rpt --dry-run

    # Only fix clearance violations
    kct fix-drc board.kicad_pcb --drc-report board-drc.rpt --only clearance

    # Only fix drill clearance violations
    kct fix-drc board.kicad_pcb --drc-report board-drc.rpt --only drill-clearance
        """,
    )
    parser.add_argument("pcb", help="Path to .kicad_pcb file")
    parser.add_argument(
        "--drc-report",
        help="Path to existing DRC report (.rpt or .json). If not provided, requires kicad-cli.",
    )
    parser.add_argument(
        "--max-displacement",
        type=float,
        default=0.25,
        help="Maximum nudge/slide distance in mm (default: 0.25)",
    )
    parser.add_argument(
        "--margin",
        type=float,
        default=0.01,
        help="Extra clearance margin beyond minimum in mm (default: 0.01)",
    )
    parser.add_argument(
        "--only",
        choices=["clearance", "drill-clearance"],
        help="Only fix a specific violation type",
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

    # Classify violations
    do_clearance = args.only is None or args.only == "clearance"
    do_drill = args.only is None or args.only == "drill-clearance"

    clearance_violations = report.by_type(ViolationType.CLEARANCE) if do_clearance else []
    drill_violations = (
        (
            report.by_type(ViolationType.DRILL_CLEARANCE)
            + report.by_type(ViolationType.HOLE_NEAR_HOLE)
        )
        if do_drill
        else []
    )

    total_targeted = len(clearance_violations) + len(drill_violations)

    if total_targeted == 0:
        if not args.quiet:
            print("No targeted violations found. Nothing to repair.")
        return 0

    if not args.quiet and args.format == "text":
        print(f"Found {total_targeted} targeted violation(s):")
        if clearance_violations:
            print(f"  Clearance: {len(clearance_violations)}")
        if drill_violations:
            print(f"  Drill clearance: {len(drill_violations)}")

    # Run repairs
    clearance_result = RepairResult()
    drill_result = DrillRepairResult()

    if clearance_violations:
        try:
            repairer = ClearanceRepairer(pcb_path)
            clearance_result = repairer.repair_from_report(
                report,
                max_displacement=args.max_displacement,
                margin=args.margin,
                dry_run=args.dry_run,
            )
            if clearance_result.repaired > 0 and not args.dry_run:
                output_path = Path(args.output) if args.output else pcb_path
                repairer.save(output_path)
        except Exception as e:
            print(f"Error during clearance repair: {e}", file=sys.stderr)

    if drill_violations:
        try:
            # If clearance repair already wrote to output, load from there
            load_path = pcb_path
            if clearance_result.repaired > 0 and not args.dry_run:
                load_path = Path(args.output) if args.output else pcb_path

            drill_repairer = DrillClearanceRepairer(load_path)
            drill_result = drill_repairer.repair(
                drill_violations,
                max_displacement=args.max_displacement,
                margin=args.margin,
                dry_run=args.dry_run,
            )
            if drill_result.repaired > 0 and not args.dry_run:
                output_path = Path(args.output) if args.output else pcb_path
                drill_repairer.save(output_path)
        except Exception as e:
            print(f"Error during drill clearance repair: {e}", file=sys.stderr)

    # Output results
    total_repaired = clearance_result.repaired + drill_result.repaired
    total_violations = clearance_result.total_violations + drill_result.total_violations

    if not args.quiet:
        _print_results(
            clearance_result,
            drill_result,
            args.format,
            args.dry_run,
            args.max_displacement,
        )

    # Exit code: 0 if all targeted violations repaired, 1 otherwise
    remaining = total_violations - total_repaired
    return 0 if remaining == 0 else 1


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
    clearance_result: RepairResult,
    drill_result: DrillRepairResult,
    output_format: str,
    dry_run: bool,
    max_displacement: float,
) -> None:
    """Print combined repair results."""
    if output_format == "json":
        _print_json(clearance_result, drill_result, dry_run, max_displacement)
    elif output_format == "summary":
        _print_summary(clearance_result, drill_result, dry_run)
    else:
        _print_text(clearance_result, drill_result, dry_run, max_displacement)


def _print_json(
    clearance_result: RepairResult,
    drill_result: DrillRepairResult,
    dry_run: bool,
    max_displacement: float,
) -> None:
    """Print results as JSON."""
    total_violations = clearance_result.total_violations + drill_result.total_violations
    total_repaired = clearance_result.repaired + drill_result.repaired

    data = {
        "dry_run": dry_run,
        "max_displacement_mm": max_displacement,
        "total_violations": total_violations,
        "total_repaired": total_repaired,
        "clearance": {
            "violations": clearance_result.total_violations,
            "repaired": clearance_result.repaired,
            "skipped": {
                "no_location": clearance_result.skipped_no_location,
                "no_delta": clearance_result.skipped_no_delta,
                "exceeds_max": clearance_result.skipped_exceeds_max,
                "infeasible": clearance_result.skipped_infeasible,
            },
            "nudges": [
                {
                    "object_type": n.object_type,
                    "x": n.x,
                    "y": n.y,
                    "net_name": n.net_name,
                    "displacement_mm": round(n.displacement_mm, 4),
                }
                for n in clearance_result.nudges
            ],
        },
        "drill_clearance": {
            "violations": drill_result.total_violations,
            "repaired": drill_result.repaired,
            "deduplicated": drill_result.deduplicated,
            "slid": drill_result.slid,
            "skipped": {
                "no_location": drill_result.skipped_no_location,
                "no_delta": drill_result.skipped_no_delta,
                "exceeds_max": drill_result.skipped_exceeds_max,
                "infeasible": drill_result.skipped_infeasible,
            },
            "actions": [
                {
                    "action": a.action,
                    "via_x": a.via_x,
                    "via_y": a.via_y,
                    "net_name": a.net_name,
                    "displacement_mm": round(a.displacement_mm, 4),
                    "detail": a.detail,
                }
                for a in drill_result.actions
            ],
        },
    }
    print(json.dumps(data, indent=2))


def _print_summary(
    clearance_result: RepairResult,
    drill_result: DrillRepairResult,
    dry_run: bool,
) -> None:
    """Print a compact summary."""
    total_violations = clearance_result.total_violations + drill_result.total_violations
    total_repaired = clearance_result.repaired + drill_result.repaired
    action = "Would repair" if dry_run else "Repaired"
    print(f"{action} {total_repaired}/{total_violations} DRC violations")
    if clearance_result.total_violations > 0:
        print(f"  Clearance: {clearance_result.repaired}/{clearance_result.total_violations}")
    if drill_result.total_violations > 0:
        print(f"  Drill clearance: {drill_result.repaired}/{drill_result.total_violations}")


def _print_text(
    clearance_result: RepairResult,
    drill_result: DrillRepairResult,
    dry_run: bool,
    max_displacement: float,
) -> None:
    """Print detailed text output."""
    total_violations = clearance_result.total_violations + drill_result.total_violations
    total_repaired = clearance_result.repaired + drill_result.repaired
    action = "Would repair" if dry_run else "Repaired"

    print(f"\n{'=' * 60}")
    print("DRC VIOLATION REPAIR")
    print(f"{'=' * 60}")
    print(f"Max displacement: {max_displacement}mm")
    print(f"Mode: {'DRY RUN' if dry_run else 'APPLY'}")
    print(f"\n{action} {total_repaired}/{total_violations} violations")

    if clearance_result.total_violations > 0:
        print(f"\n{'-' * 60}")
        print(f"CLEARANCE: {clearance_result.repaired}/{clearance_result.total_violations}")
        if clearance_result.nudges:
            for nudge in clearance_result.nudges[:5]:
                print(f"  [{nudge.object_type.upper()}] {nudge.net_name}")
                print(f"    at ({nudge.x:.4f}, {nudge.y:.4f}) -> {nudge.displacement_mm:.4f}mm")
            if len(clearance_result.nudges) > 5:
                print(f"  ... and {len(clearance_result.nudges) - 5} more")

    if drill_result.total_violations > 0:
        print(f"\n{'-' * 60}")
        print(f"DRILL CLEARANCE: {drill_result.repaired}/{drill_result.total_violations}")
        if drill_result.deduplicated > 0:
            print(f"  De-duplicated: {drill_result.deduplicated}")
        if drill_result.slid > 0:
            print(f"  Slid apart: {drill_result.slid}")
        if drill_result.actions:
            for act in drill_result.actions[:5]:
                print(f"  [{act.action.upper()}] {act.net_name}")
                print(f"    at ({act.via_x:.4f}, {act.via_y:.4f}) - {act.detail}")
            if len(drill_result.actions) > 5:
                print(f"  ... and {len(drill_result.actions) - 5} more")

    # Show skipped totals
    total_skipped = (
        clearance_result.skipped_exceeds_max
        + clearance_result.skipped_infeasible
        + clearance_result.skipped_no_location
        + clearance_result.skipped_no_delta
        + drill_result.skipped_exceeds_max
        + drill_result.skipped_infeasible
        + drill_result.skipped_no_location
        + drill_result.skipped_no_delta
    )
    if total_skipped > 0:
        print(f"\n{'-' * 60}")
        print(f"SKIPPED ({total_skipped}):")
        exceeds = clearance_result.skipped_exceeds_max + drill_result.skipped_exceeds_max
        infeasible = clearance_result.skipped_infeasible + drill_result.skipped_infeasible
        no_loc = clearance_result.skipped_no_location + drill_result.skipped_no_location
        no_delta = clearance_result.skipped_no_delta + drill_result.skipped_no_delta
        if exceeds > 0:
            print(f"  Exceeds max displacement: {exceeds}")
        if infeasible > 0:
            print(f"  Infeasible: {infeasible}")
        if no_loc > 0:
            print(f"  No location info: {no_loc}")
        if no_delta > 0:
            print(f"  No clearance delta info: {no_delta}")

    print(f"\n{'=' * 60}")

    remaining = total_violations - total_repaired
    if remaining == 0:
        print("All targeted violations repaired!")
    else:
        print(f"{remaining} violation(s) require manual repair")
        if clearance_result.skipped_exceeds_max + drill_result.skipped_exceeds_max > 0:
            print(f"  Try increasing --max-displacement (currently {max_displacement}mm)")


if __name__ == "__main__":
    sys.exit(main())
