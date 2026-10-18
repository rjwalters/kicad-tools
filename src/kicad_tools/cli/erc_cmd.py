"""
ERC (Electrical Rules Check) validation for KiCad schematics.

Runs KiCad ERC on schematics or parses existing ERC reports.

Usage:
    kicad-erc design.kicad_sch              # Run ERC on schematic
    kicad-erc design-erc.json               # Parse existing report
    kicad-erc design.kicad_sch --strict     # Exit non-zero on warnings
    kicad-erc design.kicad_sch --format json

Exit Codes:
    0 - No errors (warnings may be present)
    1 - Errors found or command failure
    2 - Warnings found (only with --strict)

Verdict:
    Every violation kicad-cli reports (minus KiCad exclusions) counts toward
    the exit code.  The only suppression is an explicit --filter-config, and
    it is always disclosed (`Filtered:` in the table, `summary.filtered` in
    JSON).  Running on a schematic gives at least the findings of parsing
    kicad-cli's own report for it; it additionally runs a cross-sheet
    duplicate-reference check.
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from ..erc import (
    ERC_CATEGORIES,
    ERC_TYPE_DESCRIPTIONS,
    ERCReport,
    ERCViolation,
    check_cross_sheet_duplicates,
)
from ._gate import gate_exit_code, hidden_by_view_filter  # noqa: F401  (re-exported; Issue #7230)
from .runner import find_kicad_cli, run_erc


def build_parser() -> argparse.ArgumentParser:
    """Return the ``kicad-erc`` argument parser.

    ``kct erc [parse]`` mirrors every option declared here (Issue #7161);
    ``tests/test_cli_parser_drift.py`` fails if the two drift apart.
    """
    parser = argparse.ArgumentParser(
        prog="kicad-erc",
        description="Run ERC on schematics or parse ERC reports",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "input",
        nargs="?",
        help="Schematic (.kicad_sch) to check or ERC report (.json/.rpt) to parse",
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
        "--type",
        "-t",
        dest="filter_type",
        help="Filter by violation type (partial match)",
    )
    parser.add_argument(
        "--sheet",
        help="Filter by sheet path",
    )
    parser.add_argument(
        "--by-sheet",
        action="store_true",
        help="Group violations by sheet",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Show detailed violation information",
    )
    parser.add_argument(
        "--filter-config",
        type=Path,
        help="TOML file with [[erc.filters]] rules to suppress or reclassify violations",
    )
    parser.add_argument(
        "--keep-report",
        action="store_true",
        help="Keep the ERC report file after running",
    )
    parser.add_argument(
        "--output",
        "-o",
        type=Path,
        help="Save ERC report to this path",
    )
    parser.add_argument(
        "--list-types",
        action="store_true",
        help="List all known ERC violation types and exit",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Main entry point for kicad-erc command."""
    parser = build_parser()
    args = parser.parse_args(argv)

    # Mode: List types
    if args.list_types:
        print_types()
        return 0

    # Require input for other modes
    if not args.input:
        parser.print_help()
        print("\nError: input file required", file=sys.stderr)
        return 1

    input_path = Path(args.input)

    # Determine if input is schematic or report
    if input_path.suffix == ".kicad_sch":
        # Run ERC on schematic
        report = run_erc_on_schematic(input_path, args.output, args.keep_report)
        if report is None:
            return 1

        # Run cross-sheet duplicate reference check (supplements KiCad ERC)
        try:
            cross_sheet_violations = check_cross_sheet_duplicates(str(input_path))
            report.violations.extend(cross_sheet_violations)
        except Exception:
            # Non-fatal: cross-sheet check is supplemental
            pass
    elif input_path.suffix in (".json", ".rpt"):
        # Parse existing report
        try:
            report = ERCReport.load(input_path)
        except FileNotFoundError:
            print(f"Error: File not found: {input_path}", file=sys.stderr)
            return 1
        except Exception as e:
            print(f"Error loading report: {e}", file=sys.stderr)
            return 1
    else:
        print(f"Error: Unsupported file type: {input_path.suffix}", file=sys.stderr)
        print("Expected .kicad_sch (schematic) or .json/.rpt (report)", file=sys.stderr)
        return 1

    # Apply violation filters from config file
    filter_ignored_count = 0
    if args.filter_config:
        try:
            from ..validate.filters import load_filters_from_toml

            _drc_filters, erc_filters = load_filters_from_toml(str(args.filter_config))
            if erc_filters:
                from ..validate.filters import FilterEngine

                engine = FilterEngine(erc_filters)
                filter_result = engine.apply(report.violations)
                report = ERCReport(
                    source_file=report.source_file,
                    kicad_version=report.kicad_version,
                    coordinate_units=report.coordinate_units,
                    violations=filter_result.kept,
                )
                filter_ignored_count = filter_result.ignored_count
        except FileNotFoundError as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1
        except Exception as e:
            print(f"Error loading filter config: {e}", file=sys.stderr)
            return 1

    # The verdict set: everything left after KiCad exclusions and the
    # deliberate ``--filter-config`` suppression.  The exit code is computed
    # from this set -- the view filters below only change what is *shown*, so
    # a filter that hides every error can never read as a clean board
    # (Issue #7094).
    gate_violations = [v for v in report.violations if not v.excluded]

    # View filters (display only)
    violations = list(gate_violations)

    if args.errors_only:
        violations = [v for v in violations if v.is_error]

    if args.filter_type:
        filter_lower = args.filter_type.lower()
        violations = [
            v
            for v in violations
            if filter_lower in v.type_str.lower()
            or filter_lower in v.description.lower()
            or filter_lower in v.type_description.lower()
        ]

    if args.sheet:
        violations = [v for v in violations if args.sheet in v.sheet]

    hidden = hidden_by_view_filter(gate_violations, violations)

    # Exit code -- from the pre-view-filter set, decided before output so
    # ``--format json`` can carry it (Issue #7230).
    error_count = sum(1 for v in gate_violations if v.is_error)
    warning_count = len(gate_violations) - error_count
    exit_code, verdict = gate_exit_code(error_count, warning_count, strict=args.strict, style="drc")

    # Output
    if args.format == "json":
        output_json(
            violations,
            report,
            filtered_count=filter_ignored_count,
            hidden=hidden,
            verdict=verdict,
            exit_code=exit_code,
        )
    elif args.format == "summary":
        output_summary(violations, report, filtered_count=filter_ignored_count, hidden=hidden)
    else:
        output_table(
            violations,
            report,
            args.verbose,
            args.by_sheet,
            filtered_count=filter_ignored_count,
            hidden=hidden,
        )

    return exit_code


def no_match_message(hidden: dict[str, int]) -> str:
    """``No violations match the filter (N hidden: E error(s), W warning(s))``."""
    total = hidden["errors"] + hidden["warnings"]
    return (
        f"No violations match the filter ({total} hidden: "
        f"{hidden['errors']} error(s), {hidden['warnings']} warning(s))"
    )


def run_erc_on_schematic(
    schematic_path: Path,
    output_path: Path | None,
    keep_report: bool,
) -> ERCReport | None:
    """Run ERC on a schematic and return parsed report."""
    if not schematic_path.exists():
        print(f"Error: Schematic not found: {schematic_path}", file=sys.stderr)
        return None

    # Check for kicad-cli
    kicad_cli = find_kicad_cli()
    if not kicad_cli:
        print("Error: kicad-cli not found", file=sys.stderr)
        print("Install KiCad 8 from: https://www.kicad.org/download/", file=sys.stderr)
        print("\nmacOS: brew install --cask kicad", file=sys.stderr)
        return None

    print(f"Running ERC on: {schematic_path.name}", file=sys.stderr)

    result = run_erc(schematic_path, output_path)

    if not result.success:
        print(f"Error running ERC: {result.stderr}", file=sys.stderr)
        return None

    # Parse the report
    try:
        report = ERCReport.load(result.output_path)
    except Exception as e:
        print(f"Error parsing ERC report: {e}", file=sys.stderr)
        return None

    # Cleanup temporary file unless keeping
    if not keep_report and output_path is None and result.output_path:
        result.output_path.unlink(missing_ok=True)

    return report


def output_table(
    violations: list[ERCViolation],
    report: ERCReport,
    verbose: bool = False,
    by_sheet: bool = False,
    filtered_count: int = 0,
    hidden: dict[str, int] | None = None,
) -> None:
    """Output violations as a formatted table.

    ``hidden`` is the count of violations a view filter left out (see
    :func:`hidden_by_view_filter`); the verdict line accounts for them.
    """
    error_count = sum(1 for v in violations if v.is_error)
    warning_count = len(violations) - error_count
    hidden_errors = hidden["errors"] if hidden else 0
    hidden_warnings = hidden["warnings"] if hidden else 0

    print(f"\n{'=' * 60}")
    print("ERC VALIDATION SUMMARY")
    print(f"{'=' * 60}")

    if report.source_file:
        print(f"File: {Path(report.source_file).name}")
    if report.kicad_version:
        print(f"KiCad: {report.kicad_version}")

    print("\nResults:")
    print(f"  Errors:     {error_count}")
    print(f"  Warnings:   {warning_count}")
    if filtered_count > 0:
        print(f"  Filtered:   {filtered_count} (--filter-config, not counted)")
    if report.exclusion_count > 0:
        print(f"  Excluded:   {report.exclusion_count} (not counted)")
    if hidden:
        print(
            f"  Hidden:     {hidden_errors} error(s), {hidden_warnings} warning(s) "
            "by view filter (still counted)"
        )

    if not violations:
        print(f"\n{'=' * 60}")
        if not hidden:
            print("ERC PASSED - No violations found")
            return
        print(no_match_message(hidden))
        if hidden_errors:
            print("ERC FAILED - Fix errors before proceeding (hidden by view filter)")
        else:
            print("ERC WARNING - Review warnings (hidden by view filter)")
        return

    # Group by type summary
    by_type: dict = {}
    for v in violations:
        if v.type_str not in by_type:
            by_type[v.type_str] = {"errors": 0, "warnings": 0}
        if v.is_error:
            by_type[v.type_str]["errors"] += 1
        else:
            by_type[v.type_str]["warnings"] += 1

    print(f"\n{'-' * 60}")
    print("BY TYPE:")
    for vtype, counts in sorted(
        by_type.items(), key=lambda x: -(x[1]["errors"] + x[1]["warnings"])
    ):
        desc = ERC_TYPE_DESCRIPTIONS.get(vtype, vtype)
        parts = []
        if counts["errors"]:
            parts.append(f"{counts['errors']} error{'s' if counts['errors'] != 1 else ''}")
        if counts["warnings"]:
            parts.append(f"{counts['warnings']} warning{'s' if counts['warnings'] != 1 else ''}")
        print(f"  {desc}: {', '.join(parts)}")

    # Detailed output
    errors = [v for v in violations if v.is_error]
    warnings = [v for v in violations if not v.is_error]

    if errors:
        print(f"\n{'-' * 60}")
        print("ERRORS (must fix):")
        _print_violations(errors, verbose, by_sheet)

    if warnings:
        print(f"\n{'-' * 60}")
        print("WARNINGS (review recommended):")
        display_warnings = warnings if verbose else warnings[:20]
        _print_violations(display_warnings, verbose, by_sheet)
        if len(warnings) > 20 and not verbose:
            print(f"\n  ... and {len(warnings) - 20} more warnings (use --verbose)")

    print(f"\n{'=' * 60}")
    if errors or hidden_errors:
        print("ERC FAILED - Fix errors before proceeding")
    else:
        print("ERC WARNING - Review warnings")


def _print_violations(
    violations: list[ERCViolation],
    verbose: bool,
    by_sheet: bool,
) -> None:
    """Print list of violations."""
    if by_sheet:
        grouped: dict[str, list] = {}
        for v in violations:
            sheet = v.sheet or "root"
            if sheet not in grouped:
                grouped[sheet] = []
            grouped[sheet].append(v)

        for sheet, sheet_violations in sorted(grouped.items()):
            print(f"\n  [{sheet}]")
            for v in sheet_violations:
                _print_single(v, verbose, "    ")
    else:
        for v in violations:
            _print_single(v, verbose, "  ")


def _print_single(v: ERCViolation, verbose: bool, indent: str = "  ") -> None:
    """Print a single violation."""
    symbol = "X" if v.is_error else "!"
    print(f"\n{indent}[{symbol}] {v.type_description}")
    print(f"{indent}    {v.description}")

    if verbose:
        if v.items:
            for item in v.items:
                print(f"{indent}    -> {item}")
        if v.location_str:
            print(f"{indent}    Location: {v.location_str}")
        if v.suggestions:
            print(f"{indent}    Suggestions:")
            for suggestion in v.suggestions[:3]:  # Show top 3 suggestions
                print(f"{indent}      - {suggestion}")


def output_json(
    violations: list[ERCViolation],
    report: ERCReport,
    filtered_count: int = 0,
    hidden: dict[str, int] | None = None,
    verdict: str | None = None,
    exit_code: int | None = None,
) -> None:
    """Output violations as JSON.

    Issue #7230: top-level ``verdict`` and ``exit_code`` when given.
    """
    summary: dict = {
        "errors": sum(1 for v in violations if v.is_error),
        "warnings": sum(1 for v in violations if not v.is_error),
    }
    if filtered_count > 0:
        summary["filtered"] = filtered_count
    if hidden:
        summary["hidden_by_view_filter"] = hidden

    data: dict[str, Any] = {
        "source": report.source_file,
        "kicad_version": report.kicad_version,
        "summary": summary,
        "violations": [v.to_dict() for v in violations],
    }
    if verdict is not None:
        data["verdict"] = verdict
    if exit_code is not None:
        data["exit_code"] = exit_code
    print(json.dumps(data, indent=2))


def output_summary(
    violations: list[ERCViolation],
    report: ERCReport,
    filtered_count: int = 0,
    hidden: dict[str, int] | None = None,
) -> None:
    """Output violation summary by type."""
    if not violations:
        if hidden:
            print(f"{no_match_message(hidden)}.")
        elif filtered_count > 0:
            print(f"No ERC violations found ({filtered_count} violations filtered).")
        else:
            print("No ERC violations found.")
        return

    print(f"ERC Summary: {report.source_file}")
    print("=" * 50)

    # Group by type
    by_type: dict = {}
    for v in violations:
        key = v.type_str
        if key not in by_type:
            by_type[key] = {"errors": 0, "warnings": 0}
        if v.is_error:
            by_type[key]["errors"] += 1
        else:
            by_type[key]["warnings"] += 1

    print(f"{'Type':<35} {'Errors':<8} {'Warnings':<8}")
    print("-" * 50)

    for type_name, counts in sorted(by_type.items()):
        print(f"{type_name:<35} {counts['errors']:<8} {counts['warnings']:<8}")

    print("-" * 50)
    total_errors = sum(c["errors"] for c in by_type.values())
    total_warnings = sum(c["warnings"] for c in by_type.values())
    print(f"{'TOTAL':<35} {total_errors:<8} {total_warnings:<8}")
    if hidden:
        print(
            f"Hidden by view filter: {hidden['errors']} error(s), "
            f"{hidden['warnings']} warning(s) (still counted)"
        )


def print_types() -> None:
    """Print all known ERC violation types."""
    print("\nKnown ERC Violation Types:")
    print("=" * 60)

    for category, types in ERC_CATEGORIES.items():
        print(f"\n{category}:")
        for t in types:
            desc = ERC_TYPE_DESCRIPTIONS.get(t, t)
            print(f"  {t:30} {desc}")


if __name__ == "__main__":
    sys.exit(main())
