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
    non_targeted_count: int = 0
    connectivity_before: int | None = None
    connectivity_after: int | None = None
    connectivity_rolled_back: bool = False
    # Per-nudge granular rollback metadata (issue #2851):
    #   * ``reverted_uuids`` holds the UUIDs of nudges/actions that were
    #     individually reverted (subset of all applied work this pass).
    #   * ``connectivity_partial_rollback`` is ``True`` when only a
    #     proper subset of nudges was reverted; ``connectivity_rolled_back``
    #     remains the flag for a *full* rollback so existing consumers
    #     (text rendering, JSON ``rolled_back`` field, exit code 3) keep
    #     their semantics.
    reverted_uuids: tuple[str, ...] = ()
    connectivity_partial_rollback: bool = False

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
        default=0.5,
        help="Maximum nudge/slide distance in mm (default: 0.5)",
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
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "Attempt local A* rerouting for infeasible violations "
            "(segments with both endpoints at vias). On by default; "
            "use --no-local-reroute to disable."
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
        "--verify",
        action="store_true",
        help=(
            "Run the pure-Python DRC before and after repair and report "
            "a before/after violation delta.  Ensures fix-drc and check "
            "agree on remaining violation counts."
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

    # --verify: snapshot violation counts from pure-Python DRC before repair
    verify_before: DRCReport | None = None
    if args.verify:
        verify_before = _run_python_drc(pcb_path)
        if verify_before is not None and not args.quiet:
            print(
                f"[verify] Before repair: {len(verify_before.violations)} "
                f"violation(s) via pure-Python DRC"
            )

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
                + report.by_type(ViolationType.CLEARANCE_PAD_SEGMENT)
                + report.by_type(ViolationType.CLEARANCE_PAD_VIA)
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
        total_all = len(report.violations)
        non_targeted_count = total_all - total_targeted

        if total_targeted == 0:
            if pass_num == 1:
                if non_targeted_count > 0:
                    # No repairable violations, but non-targeted violations exist
                    if not args.quiet:
                        print(
                            f"No repairable violations found, but {non_targeted_count} "
                            f"non-repairable violation(s) detected "
                            f"(edge clearance, dimension, silkscreen, etc.)."
                        )
                    return 2
                # No violations at all on first pass
                if not args.quiet:
                    print("No targeted violations found. Nothing to repair.")
                return 0
            else:
                # All violations resolved by previous passes
                break

        if not args.quiet and args.format == "text" and pass_num == 1:
            print(f"Found {total_targeted} repairable violation(s):")
            if clearance_violations:
                print(f"  Clearance: {len(clearance_violations)}")
            if drill_violations:
                print(f"  Drill clearance: {len(drill_violations)}")
            if non_targeted_count > 0:
                print(
                    f"Also found {non_targeted_count} non-repairable "
                    f"violation(s) (edge clearance, dimension, silkscreen, etc.)"
                )

        # Snapshot and baseline connectivity before the repair pass.
        # Issue #2851: we record the full ConnectivityResult (when
        # available) alongside the count so the post-pass rollback can
        # attribute regressions to specific nets and undo only the
        # offending subset of nudges.  The simple count is what the
        # rollback *decision* is based on (preserving the legacy
        # _count_connected_nets mock surface used by existing tests).
        snapshot: bytes | None = None
        baseline_conn: int | None = None
        baseline_report = None
        if do_connectivity_check:
            load_for_snapshot = output_path if pass_num > 1 else pcb_path
            if load_for_snapshot.exists():
                snapshot = load_for_snapshot.read_bytes()
                baseline_conn = _count_connected_nets(load_for_snapshot)
                baseline_report = _connectivity_report(load_for_snapshot)

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
        partial_rolled_back = False
        reverted_uuids: tuple[str, ...] = ()
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
                # Connectivity decreased -- try granular rollback first.
                after_report = _connectivity_report(output_path)
                regressed = _regressed_nets(baseline_report, after_report)

                reverted_all, kept_count, reverted_uuid_list = _attempt_granular_rollback(
                    output_path=output_path,
                    clearance_result=clearance_result,
                    drill_result=drill_result,
                    regressed=regressed,
                    snapshot=snapshot,
                    pass_number=pass_num,
                )

                if reverted_all:
                    # Full bulk rollback (legacy behavior).  All applied
                    # nudges were thrown away.
                    rolled_back = True
                    connectivity_rollback_occurred = True
                    repaired_this_pass = 0
                    print(
                        f"Warning: pass {pass_num} decreased connectivity "
                        f"({baseline_conn} -> {after_conn} nets); rolled back.",
                        file=sys.stderr,
                    )
                else:
                    # Partial granular rollback: ``kept_count`` nudges
                    # survived, ``len(reverted_uuid_list)`` were reverted.
                    partial_rolled_back = True
                    reverted_uuids = tuple(reverted_uuid_list)
                    reverted_count = len(reverted_uuid_list)
                    repaired_this_pass = kept_count
                    pre_undo_after = after_conn
                    # Re-measure connectivity so the JSON/text output
                    # reports the post-undo state, not the (worse)
                    # pre-undo state.  This is one extra
                    # ConnectivityValidator call -- still O(1) in N.
                    new_after = _count_connected_nets(output_path)
                    if new_after >= 0:
                        after_conn = new_after
                    print(
                        f"Warning: pass {pass_num} initially decreased "
                        f"connectivity ({baseline_conn} -> {pre_undo_after} nets); "
                        f"reverted {reverted_count} of "
                        f"{reverted_count + kept_count} nudge(s) on regressed "
                        f"net(s) {sorted(regressed)!r}; "
                        f"now {after_conn} connected net(s).",
                        file=sys.stderr,
                    )

        pass_results.append(
            PassResult(
                pass_number=pass_num,
                violations_before=total_targeted,
                repaired=repaired_this_pass,
                clearance_result=clearance_result,
                drill_result=drill_result,
                non_targeted_count=non_targeted_count,
                connectivity_before=baseline_conn,
                connectivity_after=after_conn,
                connectivity_rolled_back=rolled_back,
                reverted_uuids=reverted_uuids,
                connectivity_partial_rollback=partial_rolled_back,
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

    # --verify: run pure-Python DRC on the (potentially modified) output and
    # print a before/after delta so the user can confirm fix-drc and check agree.
    if args.verify and not args.dry_run:
        verify_after = _run_python_drc(output_path)
        if verify_before is not None and verify_after is not None:
            before_count = len(verify_before.violations)
            after_count = len(verify_after.violations)
            delta = before_count - after_count
            if not args.quiet:
                print(f"\n{'=' * 60}")
                print("VERIFICATION (pure-Python DRC)")
                print(f"{'=' * 60}")
                print(f"  Before repair: {before_count} violation(s)")
                print(f"  After repair:  {after_count} violation(s)")
                if delta > 0:
                    print(f"  Resolved:      {delta}")
                elif delta == 0:
                    print("  No change in violation count.")
                else:
                    print(f"  WARNING: {-delta} new violation(s) introduced!")
                print(
                    "\nThese counts use the same engine as `kct check` "
                    "for consistent comparison."
                )

    # Exit code: 0 = all repaired (no remaining violations of any type),
    #            1 = no violations found/no progress,
    #            2 = partial repair or non-repairable violations remain,
    #            3 = connectivity rollback
    if connectivity_rollback_occurred:
        return 3
    final_pass = pass_results[-1] if pass_results else None
    if final_pass is None:
        return 0
    remaining_targeted = final_pass.violations_before - final_pass.repaired
    remaining_total = remaining_targeted + final_pass.non_targeted_count
    if remaining_total == 0:
        return 0
    if final_pass.repaired == 0 and final_pass.non_targeted_count == 0:
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


def _connectivity_report(pcb_path: Path):
    """Return the full :class:`ConnectivityResult` for a PCB, or ``None``.

    Used by the granular rollback path in addition to
    :func:`_count_connected_nets`: the count alone is enough to *decide*
    whether to roll back, but the per-net ``issues`` list is required to
    attribute the regression to specific nets and identify the offending
    nudges.  Returning ``None`` on failure lets callers transparently
    fall back to the bulk-snapshot rollback.
    """
    try:
        from kicad_tools.validate.connectivity import ConnectivityValidator

        validator = ConnectivityValidator(pcb_path)
        return validator.validate()
    except Exception:
        return None


def _regressed_nets(baseline, after) -> set[str]:
    """Compute the set of nets that regressed between two ConnectivityResults.

    A net is "regressed" if it was fully connected (no issue) in the
    ``baseline`` result but is broken (has an entry in ``issues``) in the
    ``after`` result.  Returns an empty set when either argument is
    ``None`` -- callers treat that as "no per-net attribution available"
    and fall back to a bulk rollback.
    """
    if baseline is None or after is None:
        return set()
    baseline_broken = {issue.net_name for issue in baseline.issues if issue.net_name}
    after_broken = {issue.net_name for issue in after.issues if issue.net_name}
    return after_broken - baseline_broken


def _attempt_granular_rollback(
    *,
    output_path: Path,
    clearance_result: RepairResult,
    drill_result: DrillRepairResult,
    regressed: set[str],
    snapshot: bytes,
    pass_number: int,
) -> tuple[bool, int, list[str]]:
    """Revert only the nudges that touched a regressed net.

    Returns a 3-tuple ``(reverted_all, kept_count, reverted_uuids)``:

    * ``reverted_all`` is ``True`` when every nudge was reverted (a true
      full rollback, e.g. all nudges touched a regressed net or
      attribution returned an empty offender set so the safety-net
      fallback fired).
    * ``kept_count`` is the number of nudges that remained applied
      (``repaired_this_pass`` minus the reverted subset).
    * ``reverted_uuids`` lists the UUIDs of nudges that were reverted via
      the per-nudge undo path so the renderer can tag them
      ``(reverted)``.

    The function never re-routes the board: it edits the live S-exp tree
    in memory and rewrites the file in a single ``save`` call, satisfying
    the O(1)-extra-routing performance guard documented in the issue.

    Falls back to the bulk-snapshot restore (and reports
    ``reverted_all=True``) on any failure: empty ``regressed`` set, every
    nudge implicated, per-nudge undo returning ``False``, or an exception
    during the in-place edit.  In every fallback case the caller sees the
    legacy "revert all" semantics.
    """
    nudges = list(clearance_result.nudges)
    actions = list(drill_result.actions)
    total = len(nudges) + len(actions)

    # No nudges were applied this pass -- nothing to do.
    if total == 0:
        return (False, 0, [])

    # Identify offenders by net_name membership.
    offending_nudges = [n for n in nudges if n.net_name in regressed]
    offending_actions = [a for a in actions if a.net_name in regressed]
    offender_count = len(offending_nudges) + len(offending_actions)

    # If we cannot attribute the regression to any specific nudge (e.g.
    # empty regressed set, or no net_name match), fall back to a bulk
    # restore.  This preserves the legacy "revert all" semantics whenever
    # the granular path can't make a confident decision.
    if offender_count == 0:
        output_path.write_bytes(snapshot)
        return (True, 0, [])

    # If *every* nudge is implicated, the granular path degenerates to
    # the legacy bulk-rollback.  Skip the per-nudge edits and just
    # restore the snapshot -- this is the cheapest and safest path.
    if offender_count == total:
        output_path.write_bytes(snapshot)
        return (True, 0, [])

    # Per-nudge undo on a freshly-loaded document so the edits are
    # written atomically.  Apply clearance undos first, save, then load
    # the saved tree to apply drill undos on top.  Any failure triggers
    # the bulk fallback so we never leave the file half-reverted.
    try:
        from kicad_tools.drc.repair_clearance import ClearanceRepairer
        from kicad_tools.drc.repair_drill_clearance import DrillClearanceRepairer

        reverted_uuids: list[str] = []

        if offending_nudges:
            clearance_repairer = ClearanceRepairer(output_path)
            for nudge in offending_nudges:
                ok = clearance_repairer._undo_nudge(nudge)
                if not ok:
                    raise RuntimeError(
                        f"per-nudge undo failed for {nudge.object_type} "
                        f"uuid={nudge.uuid!r} on net {nudge.net_name!r}"
                    )
                reverted_uuids.append(nudge.uuid)
            clearance_repairer.save(output_path)

        if offending_actions:
            drill_repairer = DrillClearanceRepairer(output_path)
            for act in offending_actions:
                ok = drill_repairer.undo_action(act)
                if not ok:
                    raise RuntimeError(
                        f"per-action undo failed for {act.action} via "
                        f"uuid={act.uuid!r} on net {act.net_name!r}"
                    )
                reverted_uuids.append(act.uuid)
            drill_repairer.save(output_path)

        kept = total - offender_count
        return (False, kept, reverted_uuids)

    except Exception as e:
        # Fall back to bulk restore on any failure.
        print(
            f"Warning: pass {pass_number} per-nudge rollback failed "
            f"({e}); falling back to bulk snapshot restore.",
            file=sys.stderr,
        )
        output_path.write_bytes(snapshot)
        return (True, 0, [])


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

    Runs all check categories (clearance, dimensions, edge clearance,
    silkscreen, solder mask) so that ``fix-drc`` can report the full
    scope of violations even when it can only repair a subset.
    """
    try:
        from kicad_tools.drc.compat import drc_results_to_report
        from kicad_tools.schema.pcb import PCB
        from kicad_tools.validate.checker import DRCChecker

        pcb = PCB.load(pcb_path)
        checker = DRCChecker(pcb)
        results = checker.check_all()

        return drc_results_to_report(results, pcb_path)

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

    # Issue #2839: when the final pass was rolled back, the per-result
    # ``repaired`` counters still reflect the pre-rollback (work-attempted)
    # counts, but the effective number of changes to the PCB is 0.  Use the
    # per-PassResult ``repaired`` (which is already zeroed on rollback) so
    # the JSON ``total_repaired`` agrees with the rolled-back state, and
    # surface ``clearance.repaired`` / ``drill_clearance.repaired`` the
    # same way for consistency.
    rolled_back = bool(last.connectivity_rolled_back) if last is not None else False
    partial_rolled_back = (
        bool(last.connectivity_partial_rollback) if last is not None else False
    )
    reverted_uuids = set(last.reverted_uuids) if last is not None else set()

    # Per-category reverted counts (for partial rollback): count nudges /
    # actions whose UUID is in the reverted set.  The repairer-side
    # counters (``RepairResult.repaired`` etc.) still reflect the
    # pre-rollback total; subtracting the reverted count gives the
    # effective post-rollback count.
    clearance_reverted = sum(
        1 for n in clearance_result.nudges if n.uuid in reverted_uuids
    )
    drill_reverted = sum(
        1 for a in drill_result.actions if a.uuid in reverted_uuids
    )

    # For single-pass (or backward compat), use the single-pass totals
    if len(pass_results) == 1:
        total_violations = clearance_result.total_violations + drill_result.total_violations
        if rolled_back:
            total_repaired = 0
        elif partial_rolled_back:
            total_repaired = (
                (clearance_result.repaired - clearance_reverted)
                + (drill_result.repaired - drill_reverted)
            )
        else:
            total_repaired = clearance_result.repaired + drill_result.repaired
    else:
        # Multi-pass: first pass had the original count; total repaired is cumulative
        first = pass_results[0]
        total_violations = first.violations_before
        total_repaired = total_repaired_all

    # Non-targeted violations (detected but not repairable by fix-drc)
    non_targeted = last.non_targeted_count if last else 0

    # Effective per-category repaired counts (zeroed on full rollback,
    # decremented by the reverted subset on partial rollback so the JSON
    # summary agrees with the text output and the exit code).
    if rolled_back:
        effective_clearance_repaired = 0
        effective_drill_repaired = 0
    elif partial_rolled_back:
        effective_clearance_repaired = clearance_result.repaired - clearance_reverted
        effective_drill_repaired = drill_result.repaired - drill_reverted
    else:
        effective_clearance_repaired = clearance_result.repaired
        effective_drill_repaired = drill_result.repaired

    data: dict = {
        "dry_run": dry_run,
        "max_displacement_mm": max_displacement,
        "total_violations": total_violations,
        "total_repaired": total_repaired,
        "non_targeted_violations": non_targeted,
        "clearance": {
            "violations": clearance_result.total_violations,
            "repaired": effective_clearance_repaired,
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
                    "reverted": (
                        bool(rolled_back) or (n.uuid in reverted_uuids)
                    ),
                }
                for n in clearance_result.nudges
            ],
        },
        "drill_clearance": {
            "violations": drill_result.total_violations,
            "repaired": effective_drill_repaired,
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
                    "reverted": (
                        bool(rolled_back) or (a.uuid in reverted_uuids)
                    ),
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
                    "partial_rollback": p.connectivity_partial_rollback,
                    "reverted_uuids": list(p.reverted_uuids),
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
    last = pass_results[-1] if pass_results else None
    non_targeted = last.non_targeted_count if last else 0

    if len(pass_results) <= 1:
        # Single-pass: backward-compatible output
        clearance_result = last.clearance_result if last else RepairResult()
        drill_result = last.drill_result if last else DrillRepairResult()
        total_violations = clearance_result.total_violations + drill_result.total_violations

        # Issue #2851: when a rollback occurred (full or partial), the
        # underlying ``RepairResult.repaired`` still counts the
        # pre-rollback work; subtract the reverted subset so the
        # ``Repaired N/M`` headline matches the on-disk state.
        rolled_back = bool(last.connectivity_rolled_back) if last is not None else False
        partial_rolled_back = (
            bool(last.connectivity_partial_rollback) if last is not None else False
        )
        reverted_uuids = set(last.reverted_uuids) if last is not None else set()
        clearance_reverted = sum(
            1 for n in clearance_result.nudges if n.uuid in reverted_uuids
        )
        drill_reverted = sum(
            1 for a in drill_result.actions if a.uuid in reverted_uuids
        )

        if rolled_back:
            total_repaired = 0
            clearance_repaired = 0
            drill_repaired = 0
        elif partial_rolled_back:
            clearance_repaired = clearance_result.repaired - clearance_reverted
            drill_repaired = drill_result.repaired - drill_reverted
            total_repaired = clearance_repaired + drill_repaired
        else:
            clearance_repaired = clearance_result.repaired
            drill_repaired = drill_result.repaired
            total_repaired = clearance_repaired + drill_repaired

        print(f"{action} {total_repaired}/{total_violations} DRC violations")
        if clearance_result.total_violations > 0:
            print(f"  Clearance: {clearance_repaired}/{clearance_result.total_violations}")
        if drill_result.total_violations > 0:
            print(
                f"  Drill clearance: "
                f"{drill_repaired}/{drill_result.total_violations}"
            )
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

    if non_targeted > 0:
        print(
            f"  Non-repairable: {non_targeted} "
            f"(edge clearance, dimension, silkscreen, etc.)"
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

    # Issue #2839: when the final pass was rolled back due to a connectivity
    # regression, ``clearance_result.nudges`` / ``drill_result.actions`` still
    # contain the *pre-rollback* lists.  Printing them verbatim contradicts the
    # ``Repaired 0/N`` summary line (which reflects the rolled-back count).
    # We use the rolled_back flag to render the section in a self-consistent
    # way: header shows ``0/total (rolled back)`` and each listed nudge gets a
    # ``(reverted)`` suffix so the user can still see *what would have been
    # changed* without contradicting the summary.
    #
    # Issue #2851: when only a *subset* of nudges is rolled back (granular
    # path), only those entries get the ``(reverted)`` tag; the others
    # remain plain so the user can see the work that survived.
    rolled_back = bool(last.connectivity_rolled_back) if last is not None else False
    partial_rolled_back = (
        bool(last.connectivity_partial_rollback) if last is not None else False
    )
    reverted_uuids = set(last.reverted_uuids) if last is not None else set()

    def _is_reverted(uuid: str) -> bool:
        if rolled_back:
            return True
        if partial_rolled_back and uuid in reverted_uuids:
            return True
        return False

    # Per-category reverted counts (for partial rollback).
    clearance_reverted_count = sum(
        1 for n in clearance_result.nudges if n.uuid in reverted_uuids
    )
    drill_reverted_count = sum(
        1 for a in drill_result.actions if a.uuid in reverted_uuids
    )

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
    summary_suffix = ""
    if rolled_back:
        summary_suffix = " (rolled back -- connectivity regression)"
    elif partial_rolled_back:
        total_reverted = clearance_reverted_count + drill_reverted_count
        summary_suffix = (
            f" (partial rollback -- {total_reverted} nudge(s) reverted "
            f"on regressed nets)"
        )
    print(f"\n{action} {total_repaired}/{total_violations} violations{summary_suffix}")
    if (rolled_back or partial_rolled_back) and last is not None:
        before = last.connectivity_before
        after = last.connectivity_after
        if before is not None and after is not None:
            if rolled_back:
                print(
                    f"  Connectivity: {before} -> {after} connected nets; "
                    f"nudges reverted to preserve connectivity."
                )
            else:
                print(
                    f"  Connectivity: {before} -> {after} connected nets "
                    f"after granular rollback."
                )

    if clearance_result.total_violations > 0:
        print(f"\n{'-' * 60}")
        # When rolled back, the *effective* repaired count is 0 even though
        # ``clearance_result.repaired`` still reflects the pre-rollback total
        # (the rollback happens after the repairer returns).  Report 0/total
        # here so the header agrees with the ``Repaired 0/N`` summary.
        if rolled_back:
            effective_repaired = 0
            header_suffix = " (reverted)"
        elif partial_rolled_back:
            effective_repaired = clearance_result.repaired - clearance_reverted_count
            header_suffix = (
                f" ({clearance_reverted_count} reverted)"
                if clearance_reverted_count > 0
                else ""
            )
        else:
            effective_repaired = clearance_result.repaired
            header_suffix = ""
        print(
            f"CLEARANCE: {effective_repaired}/{clearance_result.total_violations}"
            f"{header_suffix}"
        )
        if clearance_result.nudges:
            for nudge in clearance_result.nudges[:5]:
                nudge_suffix = " (reverted)" if _is_reverted(nudge.uuid) else ""
                print(f"  [{nudge.object_type.upper()}] {nudge.net_name}{nudge_suffix}")
                print(f"    at ({nudge.x:.4f}, {nudge.y:.4f}) -> {nudge.displacement_mm:.4f}mm")
            if len(clearance_result.nudges) > 5:
                print(f"  ... and {len(clearance_result.nudges) - 5} more")

    if drill_result.total_violations > 0:
        print(f"\n{'-' * 60}")
        if rolled_back:
            effective_drill_repaired = 0
            drill_header_suffix = " (reverted)"
        elif partial_rolled_back:
            effective_drill_repaired = drill_result.repaired - drill_reverted_count
            drill_header_suffix = (
                f" ({drill_reverted_count} reverted)"
                if drill_reverted_count > 0
                else ""
            )
        else:
            effective_drill_repaired = drill_result.repaired
            drill_header_suffix = ""
        print(
            f"DRILL CLEARANCE: {effective_drill_repaired}/{drill_result.total_violations}"
            f"{drill_header_suffix}"
        )
        if not rolled_back:
            if drill_result.deduplicated > 0:
                print(f"  De-duplicated: {drill_result.deduplicated}")
            if drill_result.slid > 0:
                print(f"  Slid apart: {drill_result.slid}")
        if drill_result.actions:
            for act in drill_result.actions[:5]:
                action_suffix = " (reverted)" if _is_reverted(act.uuid) else ""
                print(f"  [{act.action.upper()}] {act.net_name}{action_suffix}")
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

    # Non-targeted (non-repairable) violations
    non_targeted = last.non_targeted_count if last else 0
    remaining = first_violations - total_repaired_all

    if remaining <= 0 and non_targeted == 0:
        print("All violations repaired!")
    elif remaining <= 0 and non_targeted > 0:
        print("All repairable violations repaired!")
        print(
            f"{non_targeted} non-repairable violation(s) remain "
            f"(edge clearance, dimension, silkscreen, etc.)"
        )
    else:
        total_remaining = remaining + non_targeted
        print(f"{total_remaining} violation(s) remain:")
        if remaining > 0:
            print(f"  Repairable (not yet fixed): {remaining}")
            if clearance_result.skipped_exceeds_max + drill_result.skipped_exceeds_max > 0:
                print(f"    Try increasing --max-displacement (currently {max_displacement}mm)")
        if non_targeted > 0:
            print(
                f"  Non-repairable: {non_targeted} "
                f"(edge clearance, dimension, silkscreen, etc.)"
            )


if __name__ == "__main__":
    sys.exit(main())
