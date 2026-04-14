#!/usr/bin/env python3
"""Automated ERC violation repair - inserts PWR_FLAG and no-connect markers.

This command repairs common ERC violations:
- power_pin_not_driven: inserts PWR_FLAG symbol near the violation
- pin_not_connected: inserts no-connect marker at the pin position

Usage:
    kct fix-erc board.kicad_sch --erc-report board-erc.json
    kct fix-erc board.kicad_sch --erc-report board-erc.json --dry-run
    kct fix-erc board.kicad_sch
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class FixAction:
    """A single fix action applied to the schematic."""

    violation_type: str
    action: str  # "insert_pwr_flag" or "insert_no_connect"
    x: float
    y: float
    description: str


@dataclass
class FixERCResult:
    """Result of the fix-erc operation."""

    total_violations: int = 0
    pwr_flag_inserted: int = 0
    no_connect_inserted: int = 0
    skipped_unknown: int = 0
    skipped_duplicate: int = 0
    actions: list[FixAction] = field(default_factory=list)

    @property
    def total_fixed(self) -> int:
        """Total number of fixes applied."""
        return self.pwr_flag_inserted + self.no_connect_inserted

    @property
    def total_skipped(self) -> int:
        """Total number of violations skipped."""
        return self.skipped_unknown + self.skipped_duplicate


def main(argv: list[str] | None = None) -> int:
    """Main entry point for fix-erc command."""
    parser = argparse.ArgumentParser(
        prog="kicad-tools fix-erc",
        description="Automated ERC violation repair",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    # Repair all auto-fixable ERC violations
    kct fix-erc board.kicad_sch --erc-report board-erc.json

    # Preview changes without modifying the schematic
    kct fix-erc board.kicad_sch --erc-report board-erc.json --dry-run

    # Run ERC automatically (requires kicad-cli)
    kct fix-erc board.kicad_sch
        """,
    )
    parser.add_argument("schematic", help="Path to .kicad_sch file")
    parser.add_argument(
        "--erc-report",
        help="Path to existing ERC report (.rpt or .json). If not provided, requires kicad-cli.",
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
    sch_path = Path(args.schematic)
    if not sch_path.exists():
        print(f"Error: Schematic file not found: {sch_path}", file=sys.stderr)
        return 1

    if sch_path.suffix.lower() != ".kicad_sch":
        print(f"Error: Expected .kicad_sch file, got: {sch_path.suffix}", file=sys.stderr)
        return 1

    # Load ERC report
    report = _get_erc_report(args.erc_report, sch_path)
    if report is None:
        return 1

    # Apply fixes
    result = _apply_fixes(sch_path, report, dry_run=args.dry_run, quiet=args.quiet)

    # Print results
    if not args.quiet:
        _print_results(result, args.format, args.dry_run)

    # Exit code: 0 when all targeted violations are fixed; non-zero when any remain.
    # skipped_duplicate violations count as resolved (dedup prevents inserting twice at the
    # same position), so subtract them from the outstanding count.
    remaining = result.total_violations - result.total_fixed - result.skipped_duplicate
    if result.total_violations == 0:
        return 0
    return 0 if remaining == 0 else 1


def _get_erc_report(erc_report_path: str | None, sch_path: Path):
    """Load or generate an ERC report."""
    from kicad_tools.erc.report import ERCReport

    if erc_report_path:
        report_path = Path(erc_report_path)
        if not report_path.exists():
            print(f"Error: ERC report not found: {report_path}", file=sys.stderr)
            return None
        try:
            return ERCReport.load(report_path)
        except Exception as e:
            print(f"Error loading ERC report: {e}", file=sys.stderr)
            return None

    # Try to run ERC using kicad-cli
    try:
        from kicad_tools.cli.runner import find_kicad_cli, run_erc

        kicad_cli = find_kicad_cli()
        if kicad_cli:
            print(f"Running ERC on: {sch_path.name}")
            erc_result = run_erc(sch_path, kicad_cli=kicad_cli)
            if not erc_result.success:
                print(f"Error running ERC: {erc_result.stderr}", file=sys.stderr)
                return None

            report = ERCReport.load(erc_result.output_path)
            if erc_result.output_path:
                erc_result.output_path.unlink(missing_ok=True)
            return report
    except ImportError:
        pass

    print(
        "Error: No ERC report provided and kicad-cli not found.\n"
        "Provide a report with --erc-report, or install KiCad 8.",
        file=sys.stderr,
    )
    return None


def _apply_fixes(sch_path: Path, report, *, dry_run: bool, quiet: bool) -> FixERCResult:
    """Apply ERC fixes to the schematic.

    Handles:
    - power_pin_not_driven: insert PWR_FLAG at violation pos with +2.54mm Y offset
    - pin_not_connected: insert no-connect marker at pin position
    - unknown/other types: skip with warning
    """
    from kicad_tools.erc.violation import ERCViolationType

    result = FixERCResult()

    # Classify violations
    pwr_violations = report.by_type(ERCViolationType.POWER_PIN_NOT_DRIVEN)
    nc_violations = report.by_type(ERCViolationType.PIN_NOT_CONNECTED)

    # Count unknown/unhandled types
    handled_types = {ERCViolationType.POWER_PIN_NOT_DRIVEN, ERCViolationType.PIN_NOT_CONNECTED}
    all_violations = [v for v in report.violations if not v.excluded]
    unhandled = [v for v in all_violations if v.type not in handled_types]
    unknown_violations = [v for v in unhandled if v.type == ERCViolationType.UNKNOWN]

    result.total_violations = len(pwr_violations) + len(nc_violations)
    result.skipped_unknown = len(unknown_violations)

    if result.total_violations == 0:
        if not quiet:
            if unknown_violations:
                for v in unknown_violations:
                    print(
                        f"Warning: Skipping UNKNOWN violation type '{v.type_str}': {v.description}",
                        file=sys.stderr,
                    )
            print("No auto-fixable ERC violations found. Nothing to fix.")
        return result

    # Track positions to avoid inserting duplicates at the same location
    pwr_flag_positions: set[tuple[float, float]] = set()
    no_connect_positions: set[tuple[float, float]] = set()

    # Load schematic (only if we actually need to modify it)
    sch = None
    if not dry_run:
        from kicad_tools.schematic.models import Schematic

        sch = Schematic.load(sch_path)

    # Warn about unknown violations
    for v in unknown_violations:
        if not quiet:
            print(
                f"Warning: Skipping UNKNOWN violation type '{v.type_str}': {v.description}",
                file=sys.stderr,
            )

    # Fix power_pin_not_driven: insert PWR_FLAG at violation coordinates + 2.54mm Y offset
    for v in pwr_violations:
        pwr_x = v.pos_x
        pwr_y = v.pos_y + 2.54  # Offset below the power rail wire
        pos_key = (round(pwr_x, 2), round(pwr_y, 2))

        if pos_key in pwr_flag_positions:
            result.skipped_duplicate += 1
            continue

        pwr_flag_positions.add(pos_key)

        if not dry_run and sch is not None:
            sch.add_pwr_flag(pwr_x, pwr_y)

        result.pwr_flag_inserted += 1
        result.actions.append(
            FixAction(
                violation_type="power_pin_not_driven",
                action="insert_pwr_flag",
                x=pwr_x,
                y=pwr_y,
                description=v.description,
            )
        )

    # Fix pin_not_connected: insert no-connect marker at the pin position
    for v in nc_violations:
        nc_x = v.pos_x
        nc_y = v.pos_y
        pos_key = (round(nc_x, 2), round(nc_y, 2))

        if pos_key in no_connect_positions:
            result.skipped_duplicate += 1
            continue

        no_connect_positions.add(pos_key)

        if not dry_run and sch is not None:
            sch.add_no_connect(nc_x, nc_y)

        result.no_connect_inserted += 1
        result.actions.append(
            FixAction(
                violation_type="pin_not_connected",
                action="insert_no_connect",
                x=nc_x,
                y=nc_y,
                description=v.description,
            )
        )

    # Save modified schematic
    if not dry_run and sch is not None and result.total_fixed > 0:
        sch.write(sch_path)

    return result


def _print_results(result: FixERCResult, output_format: str, dry_run: bool) -> None:
    """Print fix results."""
    if output_format == "json":
        _print_json(result, dry_run)
    elif output_format == "summary":
        _print_summary(result, dry_run)
    else:
        _print_text(result, dry_run)


def _print_json(result: FixERCResult, dry_run: bool) -> None:
    """Print results as JSON."""
    data = {
        "dry_run": dry_run,
        "total_violations": result.total_violations,
        "total_fixed": result.total_fixed,
        "pwr_flag_inserted": result.pwr_flag_inserted,
        "no_connect_inserted": result.no_connect_inserted,
        "skipped_unknown": result.skipped_unknown,
        "skipped_duplicate": result.skipped_duplicate,
        "actions": [
            {
                "violation_type": a.violation_type,
                "action": a.action,
                "x": a.x,
                "y": a.y,
                "description": a.description,
            }
            for a in result.actions
        ],
    }
    print(json.dumps(data, indent=2))


def _print_summary(result: FixERCResult, dry_run: bool) -> None:
    """Print a compact summary."""
    action = "Would fix" if dry_run else "Fixed"
    print(f"{action} {result.total_fixed}/{result.total_violations} ERC violations")
    if result.pwr_flag_inserted > 0:
        print(f"  PWR_FLAG inserted: {result.pwr_flag_inserted}")
    if result.no_connect_inserted > 0:
        print(f"  No-connect markers: {result.no_connect_inserted}")
    if result.skipped_unknown > 0:
        print(f"  Skipped (unknown type): {result.skipped_unknown}")
    if result.skipped_duplicate > 0:
        print(f"  Skipped (duplicate position): {result.skipped_duplicate}")


def _print_text(result: FixERCResult, dry_run: bool) -> None:
    """Print detailed text output."""
    action = "Would fix" if dry_run else "Fixed"

    print(f"\n{'=' * 60}")
    print("ERC VIOLATION REPAIR")
    print(f"{'=' * 60}")
    print(f"Mode: {'DRY RUN' if dry_run else 'APPLY'}")

    print(f"\n{action} {result.total_fixed}/{result.total_violations} violations")

    if result.pwr_flag_inserted > 0:
        print(f"\n{'-' * 60}")
        print(f"PWR_FLAG INSERTIONS: {result.pwr_flag_inserted}")
        for a in result.actions:
            if a.action == "insert_pwr_flag":
                print(f"  at ({a.x:.2f}, {a.y:.2f}): {a.description}")

    if result.no_connect_inserted > 0:
        print(f"\n{'-' * 60}")
        print(f"NO-CONNECT MARKERS: {result.no_connect_inserted}")
        for a in result.actions:
            if a.action == "insert_no_connect":
                print(f"  at ({a.x:.2f}, {a.y:.2f}): {a.description}")

    if result.skipped_unknown > 0:
        print(f"\n{'-' * 60}")
        print(f"SKIPPED ({result.skipped_unknown} unknown type violations)")

    if result.skipped_duplicate > 0:
        print(f"SKIPPED ({result.skipped_duplicate} duplicate position violations)")

    print(f"\n{'=' * 60}")

    remaining = result.total_violations - result.total_fixed
    if remaining <= 0:
        print("All targeted violations fixed!")
    else:
        print(f"{remaining} violation(s) remain unfixed")


if __name__ == "__main__":
    sys.exit(main())
