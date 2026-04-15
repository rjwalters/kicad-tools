#!/usr/bin/env python3
"""Automated DRC violation repair - orchestrates clearance and drill repairs.

This command repairs multiple types of DRC violations:
- clearance_segment_segment: nudge traces via ClearanceRepairer
- clearance_segment_via: nudge traces away from enlarged vias via ClearanceRepairer
- dimension_drill_clearance: de-duplicate or slide vias via DrillClearanceRepairer

Usage:
    kct fix-drc board.kicad_pcb --drc-report board-drc.rpt
    kct fix-drc board.kicad_pcb --drc-report board-drc.rpt --dry-run
    kct fix-drc board.kicad_pcb --drc-report board-drc.rpt --only clearance
    kct fix-drc board.kicad_pcb --drc-report board-drc.rpt --only drill-clearance
    kct fix-drc board.kicad_pcb --drc-report board-drc.rpt --max-passes 3
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path

from kicad_tools.drc.repair_clearance import ClearanceRepairer, RepairResult
from kicad_tools.drc.repair_drill_clearance import DrillClearanceRepairer, DrillRepairResult
from kicad_tools.drc.report import DRCReport
from kicad_tools.drc.violation import ViolationType


@dataclass
class PassResult:
    """Statistics for a single repair pass."""

    pass_number: int
    violations_before: int
    repaired: int
    clearance_result: RepairResult
    drill_result: DrillRepairResult
    connectivity_before: int | None = None
    connectivity_after: int | None = None
    connectivity_rolled_back: bool = False

    @property
    def violations_after(self) -> int:
        """Number of violations remaining after this pass."""
        return self.violations_before - self.repaired

    @property
    def converged(self) -> bool:
        """Whether this pass made no progress."""
        return self.repaired == 0


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

    # Run up to 3 iterative repair passes
    kct fix-drc board.kicad_pcb --drc-report board-drc.rpt --max-passes 3
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
        "--max-passes",
        type=int,
        default=1,
        help=(
            "Maximum number of detect-repair cycles (default: 1). "
            "Each pass re-runs DRC detection on the modified PCB. "
            "Iteration stops early when no violations are repaired in a pass."
        ),
    )
    parser.add_argument(
        "--local-reroute",
        action="store_true",
        help=(
            "Attempt local A* rerouting for infeasible violations "
            "(segments with both endpoints at vias). Off by default."
        ),
    )
    parser.add_argument(
        "--format",
        choices=["text", "json", "summary"],
        default="text",
        help="Output format (default: text)",
    )
    parser.add_argument(
        "--no-connectivity-check",
        action="store_true",
        help=(
            "Skip post-pass connectivity check and rollback. "
            "Use for boards with no footprints where connectivity is meaningless."
        ),
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

    # Validate max_passes
    if args.max_passes < 1:
        print("Error: --max-passes must be at least 1", file=sys.stderr)
        return 1

    # Effective max passes: dry-run forces single pass since no geometry changes
    effective_max_passes = 1 if args.dry_run else args.max_passes

    # Get initial DRC report
    report = _get_drc_report(args.drc_report, pcb_path)
    if report is None:
        return 1

    # Determine output path used for saving
    output_path = Path(args.output) if args.output else pcb_path

    pass_results: list[PassResult] = []
    do_connectivity_check = not args.dry_run and not args.no_connectivity_check
    connectivity_rollback_occurred = False

    for pass_num in range(1, effective_max_passes + 1):
        # Classify violations from current report
        do_clearance = args.only is None or args.only == "clearance"
        do_drill = args.only is None or args.only == "drill-clearance"

        clearance_violations = (
            (
                report.by_type(ViolationType.CLEARANCE)
                + report.by_type(ViolationType.CLEARANCE_SEGMENT_VIA)
            )
            if do_clearance
            else []
        )
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
            if pass_num == 1:
                # No violations at all on first pass
                if not args.quiet:
                    print("No targeted violations found. Nothing to repair.")
                return 0
            else:
                # All violations resolved by previous passes
                break

        if not args.quiet and args.format == "text" and pass_num == 1:
            print(f"Found {total_targeted} targeted violation(s):")
            if clearance_violations:
                print(f"  Clearance: {len(clearance_violations)}")
            if drill_violations:
                print(f"  Drill clearance: {len(drill_violations)}")

        # Snapshot and baseline connectivity before the repair pass
        snapshot: bytes | None = None
        baseline_conn: int | None = None
        if do_connectivity_check:
            load_for_snapshot = output_path if pass_num > 1 else pcb_path
            if load_for_snapshot.exists():
                snapshot = load_for_snapshot.read_bytes()
                baseline_conn = _count_connected_nets(load_for_snapshot)

        # Run single-pass repairs
        clearance_result, drill_result = _run_single_pass(
            report=report,
            pcb_path=pcb_path,
            output_path=output_path,
            clearance_violations=clearance_violations,
            drill_violations=drill_violations,
            max_displacement=args.max_displacement,
            margin=args.margin,
            dry_run=args.dry_run,
            pass_number=pass_num,
            local_reroute=args.local_reroute,
        )

        repaired_this_pass = clearance_result.repaired + drill_result.repaired

        # Post-pass connectivity check and rollback
        after_conn: int | None = None
        rolled_back = False
        if (
            snapshot is not None
            and baseline_conn is not None
            and baseline_conn >= 0
            and not args.dry_run
            and repaired_this_pass > 0
            and output_path.exists()
        ):
            after_conn = _count_connected_nets(output_path)
            if after_conn >= 0 and after_conn < baseline_conn:
                # Connectivity decreased -- rollback
                output_path.write_bytes(snapshot)
                rolled_back = True
                connectivity_rollback_occurred = True
                repaired_this_pass = 0
                print(
                    f"Warning: pass {pass_num} decreased connectivity "
                    f"({baseline_conn} -> {after_conn} nets); rolled back.",
                    file=sys.stderr,
                )

        pass_results.append(
            PassResult(
                pass_number=pass_num,
                violations_before=total_targeted,
                repaired=repaired_this_pass,
                clearance_result=clearance_result,
                drill_result=drill_result,
                connectivity_before=baseline_conn,
                connectivity_after=after_conn,
                connectivity_rolled_back=rolled_back,
            )
        )

        # Stop if rolled back -- no point continuing
        if rolled_back:
            break

        # Stop if no progress
        if repaired_this_pass == 0:
            break

        # Re-run detection for next pass (unless this is the last allowed pass)
        if pass_num < effective_max_passes:
            report = _run_python_drc(output_path)
            if report is None:
                break

    # Output results
    if not args.quiet:
        _print_results(
            pass_results,
            args.format,
            args.dry_run,
            args.max_displacement,
            args.max_passes,
        )

    # Exit code: 0 = all repaired, 1 = no violations found/no progress,
    #            2 = partial repair, 3 = connectivity rollback
    if connectivity_rollback_occurred:
        return 3
    final_pass = pass_results[-1] if pass_results else None
    if final_pass is None:
        return 0
    remaining = final_pass.violations_before - final_pass.repaired
    if remaining == 0:
        return 0
    if final_pass.repaired == 0:
        return 1
    return 2


def _run_single_pass(
    *,
    report: DRCReport,
    pcb_path: Path,
    output_path: Path,
    clearance_violations: list,
    drill_violations: list,
    max_displacement: float,
    margin: float,
    dry_run: bool,
    pass_number: int = 1,
    local_reroute: bool = False,
) -> tuple[RepairResult, DrillRepairResult]:
    """Execute a single repair pass (clearance + drill) and return results."""
    clearance_result = RepairResult()
    drill_result = DrillRepairResult()

    if clearance_violations:
        try:
            # For pass 2+, load from output_path (which has the previous pass's saved result).
            # On pass 1, output_path may equal pcb_path (no --output given), so this is safe.
            load_path = output_path if pass_number > 1 else pcb_path
            repairer = ClearanceRepairer(load_path)
            clearance_result = repairer.repair_from_report(
                report,
                max_displacement=max_displacement,
                margin=margin,
                dry_run=dry_run,
                local_reroute=local_reroute,
            )
            if clearance_result.repaired > 0 and not dry_run:
                repairer.save(output_path)
        except Exception as e:
            print(f"Error during clearance repair: {e}", file=sys.stderr)

    if drill_violations:
        try:
            # If clearance repair already wrote to output, load from there
            load_path = pcb_path
            if clearance_result.repaired > 0 and not dry_run:
                load_path = output_path

            drill_repairer = DrillClearanceRepairer(load_path)
            drill_result = drill_repairer.repair(
                drill_violations,
                max_displacement=max_displacement,
                margin=margin,
                dry_run=dry_run,
            )
            if drill_result.repaired > 0 and not dry_run:
                drill_repairer.save(output_path)
        except Exception as e:
            print(f"Error during drill clearance repair: {e}", file=sys.stderr)

    return clearance_result, drill_result


def _count_connected_nets(pcb_path: Path) -> int:
    """Return the number of fully connected nets, or -1 on error.

    Uses :class:`~kicad_tools.validate.connectivity.ConnectivityValidator`
    to perform the check.  Returns ``-1`` when the validator cannot be
    imported or raises an exception so that callers can treat the result
    as "unknown" and skip rollback logic.
    """
    try:
        from kicad_tools.validate.connectivity import ConnectivityValidator

        validator = ConnectivityValidator(pcb_path)
        result = validator.validate()
        return result.connected_nets
    except Exception:
        return -1


def _get_drc_report(drc_report_path: str | None, pcb_path: Path) -> DRCReport | None:
    """Load or generate a DRC report.

    Resolution order:
    1. If ``--drc-report`` is given, load the file directly.
    2. If kicad-cli is available, run it and parse the resulting report.
    3. Fall back to the pure-Python ``DRCChecker`` so that segment-to-via
       violations are detected even without kicad-cli installed.
    """
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
        if kicad_cli:
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
        pass

    # Fall back to the pure-Python DRC checker
    return _run_python_drc(pcb_path)


def _run_python_drc(pcb_path: Path) -> DRCReport | None:
    """Run pure-Python DRC and convert results into a DRCReport.

    This allows ``fix-drc`` to detect segment-to-via clearance violations
    even when kicad-cli is not installed.
    """
    try:
        from kicad_tools.core.types import Severity
        from kicad_tools.drc.violation import DRCViolation as ReportViolation
        from kicad_tools.drc.violation import Location, ViolationType
        from kicad_tools.schema.pcb import PCB
        from kicad_tools.validate.checker import DRCChecker

        pcb = PCB.load(pcb_path)
        checker = DRCChecker(pcb)
        results = checker.check_clearances()

        violations: list[ReportViolation] = []
        for v in results.violations:
            vtype = ViolationType.from_string(v.rule_id)
            loc_list: list[Location] = []
            if v.location:
                loc_list.append(
                    Location(x_mm=v.location[0], y_mm=v.location[1], layer=v.layer or "")
                )

            violations.append(
                ReportViolation(
                    type=vtype,
                    type_str=v.rule_id,
                    severity=Severity.from_string(v.severity),
                    message=v.message,
                    locations=loc_list,
                    items=list(v.items),
                    required_value_mm=v.required_value,
                    actual_value_mm=v.actual_value,
                )
            )

        return DRCReport(
            source_file=str(pcb_path),
            created_at=None,
            pcb_name=pcb_path.name,
            violations=violations,
        )

    except Exception as e:
        print(f"Error running pure-Python DRC: {e}", file=sys.stderr)
        print(
            "Provide a DRC report with --drc-report, or install KiCad 8.",
            file=sys.stderr,
        )
        return None


def _print_results(
    pass_results: list[PassResult],
    output_format: str,
    dry_run: bool,
    max_displacement: float,
    max_passes: int = 1,
) -> None:
    """Print combined repair results."""
    if output_format == "json":
        _print_json(pass_results, dry_run, max_displacement, max_passes)
    elif output_format == "summary":
        _print_summary(pass_results, dry_run)
    else:
        _print_text(pass_results, dry_run, max_displacement)


def _print_json(
    pass_results: list[PassResult],
    dry_run: bool,
    max_displacement: float,
    max_passes: int = 1,
) -> None:
    """Print results as JSON."""
    # Aggregate totals from the last pass for backward-compatible top-level keys
    last = pass_results[-1] if pass_results else None
    clearance_result = last.clearance_result if last else RepairResult()
    drill_result = last.drill_result if last else DrillRepairResult()

    # Compute overall totals across all passes
    total_repaired_all = sum(p.repaired for p in pass_results)

    # For single-pass (or backward compat), use the single-pass totals
    if len(pass_results) == 1:
        total_violations = clearance_result.total_violations + drill_result.total_violations
        total_repaired = clearance_result.repaired + drill_result.repaired
    else:
        # Multi-pass: first pass had the original count; total repaired is cumulative
        first = pass_results[0]
        total_violations = first.violations_before
        total_repaired = total_repaired_all

    data: dict = {
        "dry_run": dry_run,
        "max_displacement_mm": max_displacement,
        "total_violations": total_violations,
        "total_repaired": total_repaired,
        "clearance": {
            "violations": clearance_result.total_violations,
            "repaired": clearance_result.repaired,
            "relocated_vias": clearance_result.relocated_vias,
            "endpoint_nudges": clearance_result.endpoint_nudges,
            "local_rerouted": clearance_result.local_rerouted,
            "cluster_rerouted": clearance_result.cluster_rerouted,
            "skipped": {
                "no_location": clearance_result.skipped_no_location,
                "no_delta": clearance_result.skipped_no_delta,
                "exceeds_max": clearance_result.skipped_exceeds_max,
                "infeasible": clearance_result.skipped_infeasible,
                "no_local_route": clearance_result.skipped_no_local_route,
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

    # Add connectivity_check section when any pass has connectivity data
    has_connectivity_data = any(
        p.connectivity_before is not None or p.connectivity_after is not None for p in pass_results
    )
    if has_connectivity_data:
        data["connectivity_check"] = {
            "passes": [
                {
                    "pass": p.pass_number,
                    "connected_nets_before": p.connectivity_before,
                    "connected_nets_after": p.connectivity_after,
                    "rolled_back": p.connectivity_rolled_back,
                }
                for p in pass_results
                if p.connectivity_before is not None or p.connectivity_after is not None
            ],
        }

    # Add passes array only when the user requested multi-pass mode
    if max_passes > 1:
        data["passes"] = [
            {
                "pass": p.pass_number,
                "violations_before": p.violations_before,
                "repaired": p.repaired,
                "violations_after": p.violations_after,
                **(
                    {"connectivity_rolled_back": p.connectivity_rolled_back}
                    if p.connectivity_before is not None
                    else {}
                ),
            }
            for p in pass_results
        ]

    print(json.dumps(data, indent=2))


def _print_summary(
    pass_results: list[PassResult],
    dry_run: bool,
) -> None:
    """Print a compact summary."""
    action = "Would repair" if dry_run else "Repaired"

    if len(pass_results) <= 1:
        # Single-pass: backward-compatible output
        last = pass_results[-1] if pass_results else None
        clearance_result = last.clearance_result if last else RepairResult()
        drill_result = last.drill_result if last else DrillRepairResult()
        total_violations = clearance_result.total_violations + drill_result.total_violations
        total_repaired = clearance_result.repaired + drill_result.repaired
        print(f"{action} {total_repaired}/{total_violations} DRC violations")
        if clearance_result.total_violations > 0:
            print(f"  Clearance: {clearance_result.repaired}/{clearance_result.total_violations}")
        if drill_result.total_violations > 0:
            print(f"  Drill clearance: {drill_result.repaired}/{drill_result.total_violations}")
    else:
        # Multi-pass: per-pass progress
        total_repaired_all = sum(p.repaired for p in pass_results)
        first_violations = pass_results[0].violations_before
        print(f"{action} {total_repaired_all}/{first_violations} DRC violations")
        for p in pass_results:
            if p.converged:
                print(
                    f"  Pass {p.pass_number}: {p.violations_before} -> "
                    f"{p.violations_before} (converged)"
                )
            else:
                print(
                    f"  Pass {p.pass_number}: {p.violations_before} -> "
                    f"{p.violations_after} (-{p.repaired})"
                )


def _print_text(
    pass_results: list[PassResult],
    dry_run: bool,
    max_displacement: float,
) -> None:
    """Print detailed text output."""
    # Aggregate across all passes
    total_repaired_all = sum(p.repaired for p in pass_results)
    first_violations = pass_results[0].violations_before if pass_results else 0
    last = pass_results[-1] if pass_results else None
    clearance_result = last.clearance_result if last else RepairResult()
    drill_result = last.drill_result if last else DrillRepairResult()
    action = "Would repair" if dry_run else "Repaired"

    print(f"\n{'=' * 60}")
    print("DRC VIOLATION REPAIR")
    print(f"{'=' * 60}")
    print(f"Max displacement: {max_displacement}mm")
    print(f"Mode: {'DRY RUN' if dry_run else 'APPLY'}")

    if len(pass_results) > 1:
        # Multi-pass progress lines
        for p in pass_results:
            if p.converged:
                print(
                    f"  Pass {p.pass_number}: {p.violations_before} -> "
                    f"{p.violations_before} (converged)"
                )
            else:
                print(
                    f"  Pass {p.pass_number}: {p.violations_before} -> "
                    f"{p.violations_after} (-{p.repaired})"
                )

    total_violations = first_violations
    total_repaired = total_repaired_all
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

    remaining = first_violations - total_repaired_all
    if remaining <= 0:
        print("All targeted violations repaired!")
    else:
        print(f"{remaining} violation(s) require manual repair")
        if clearance_result.skipped_exceeds_max + drill_result.skipped_exceeds_max > 0:
            print(f"  Try increasing --max-displacement (currently {max_displacement}mm)")


if __name__ == "__main__":
    sys.exit(main())
