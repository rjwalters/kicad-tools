"""
DRC (Design Rules Check) validation for KiCad PCBs.

Runs KiCad DRC on PCB files or parses existing DRC reports.
Optionally checks against manufacturer design rules.

Usage:
    kicad-drc design.kicad_pcb              # Run DRC on PCB
    kicad-drc design-drc.json               # Parse existing report
    kicad-drc design.kicad_pcb --mfr jlcpcb # Check manufacturer rules
    kicad-drc --rules --mfr seeed           # Show manufacturer rules
    kicad-drc --compare                     # Compare all manufacturers

Exit Codes:
    0 - No errors (warnings may be present)
    1 - Errors found or command failure
    2 - Warnings found (only with --strict)
"""

import argparse
import json
import sys
from pathlib import Path
from typing import List

from ..drc import DRCReport, DRCViolation, check_manufacturer_rules
from ..manufacturers import compare_design_rules, get_manufacturer_ids, get_profile
from .runner import find_kicad_cli, run_drc


def main(argv: List[str] | None = None) -> int:
    """Main entry point for kicad-drc command."""
    parser = argparse.ArgumentParser(
        prog="kicad-drc",
        description="Run DRC on PCBs or parse DRC reports",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "input",
        nargs="?",
        help="PCB (.kicad_pcb) to check or DRC report (.json/.rpt) to parse",
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
        "--net",
        help="Filter by net name",
    )
    parser.add_argument(
        "--mfr",
        "-m",
        choices=get_manufacturer_ids(),
        help="Target manufacturer for rules check",
    )
    parser.add_argument(
        "--layers",
        "-l",
        type=int,
        default=2,
        help="Number of copper layers (default: 2)",
    )
    parser.add_argument(
        "--rules",
        action="store_true",
        help="Print manufacturer design rules and exit",
    )
    parser.add_argument(
        "--compare",
        action="store_true",
        help="Compare design rules across manufacturers",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Show detailed violation information",
    )
    parser.add_argument(
        "--keep-report",
        action="store_true",
        help="Keep the DRC report file after running",
    )
    parser.add_argument(
        "--output",
        "-o",
        type=Path,
        help="Save DRC report to this path",
    )

    args = parser.parse_args(argv)

    # Mode: Compare manufacturers
    if args.compare:
        print_comparison(args.layers)
        return 0

    # Mode: Show manufacturer rules
    if args.rules:
        mfr = args.mfr or "jlcpcb"
        print_manufacturer_rules(mfr, args.layers)
        return 0

    # Require input for other modes
    if not args.input:
        parser.print_help()
        print("\nError: input file required", file=sys.stderr)
        print("\nTo see design rules, run with --rules --mfr <manufacturer>", file=sys.stderr)
        print("To compare manufacturers, run with --compare", file=sys.stderr)
        return 1

    input_path = Path(args.input)

    # Determine if input is PCB or report
    if input_path.suffix == ".kicad_pcb":
        # Run DRC on PCB
        report = run_drc_on_pcb(input_path, args.output, args.keep_report)
        if report is None:
            return 1
    elif input_path.suffix in (".json", ".rpt"):
        # Parse existing report
        try:
            report = DRCReport.load(input_path)
        except FileNotFoundError:
            print(f"Error: File not found: {input_path}", file=sys.stderr)
            return 1
        except Exception as e:
            print(f"Error loading report: {e}", file=sys.stderr)
            return 1
    else:
        print(f"Error: Unsupported file type: {input_path.suffix}", file=sys.stderr)
        print("Expected .kicad_pcb (PCB) or .json/.rpt (report)", file=sys.stderr)
        return 1

    # Manufacturer check mode
    if args.mfr:
        return output_manufacturer_check(report, args.mfr, args.layers, args.verbose)

    # Apply filters
    violations = list(report.violations)

    if args.errors_only:
        violations = [v for v in violations if v.is_error]

    if args.filter_type:
        filter_lower = args.filter_type.lower()
        violations = [
            v
            for v in violations
            if filter_lower in v.type_str.lower() or filter_lower in v.message.lower()
        ]

    if args.net:
        violations = [v for v in violations if args.net in v.nets]

    # Output
    if args.format == "json":
        output_json(violations, report)
    elif args.format == "summary":
        output_summary(violations, report)
    else:
        output_table(violations, report, args.verbose)

    # Exit code
    error_count = sum(1 for v in violations if v.is_error)
    warning_count = len(violations) - error_count

    if error_count > 0:
        return 1
    elif warning_count > 0 and args.strict:
        return 2
    return 0


def run_drc_on_pcb(
    pcb_path: Path,
    output_path: Path | None,
    keep_report: bool,
) -> DRCReport | None:
    """Run DRC on a PCB and return parsed report."""
    if not pcb_path.exists():
        print(f"Error: PCB not found: {pcb_path}", file=sys.stderr)
        return None

    # Check for kicad-cli
    kicad_cli = find_kicad_cli()
    if not kicad_cli:
        print("Error: kicad-cli not found", file=sys.stderr)
        print("Install KiCad 8 from: https://www.kicad.org/download/", file=sys.stderr)
        print("\nmacOS: brew install --cask kicad", file=sys.stderr)
        return None

    print(f"Running DRC on: {pcb_path.name}")

    result = run_drc(pcb_path, output_path)

    if not result.success:
        print(f"Error running DRC: {result.stderr}", file=sys.stderr)
        return None

    # Parse the report
    try:
        report = DRCReport.load(result.output_path)
    except Exception as e:
        print(f"Error parsing DRC report: {e}", file=sys.stderr)
        return None

    # Cleanup temporary file unless keeping
    if not keep_report and output_path is None and result.output_path:
        result.output_path.unlink(missing_ok=True)

    return report


def output_table(
    violations: List[DRCViolation],
    report: DRCReport,
    verbose: bool = False,
) -> None:
    """Output violations as a formatted table."""
    error_count = sum(1 for v in violations if v.is_error)
    warning_count = len(violations) - error_count

    print(f"\n{'=' * 60}")
    print("DRC VALIDATION SUMMARY")
    print(f"{'=' * 60}")

    if report.source_file:
        print(f"File: {Path(report.source_file).name}")
    if report.pcb_name:
        print(f"PCB: {report.pcb_name}")

    print("\nResults:")
    print(f"  Errors:     {error_count}")
    print(f"  Warnings:   {warning_count}")

    if not violations:
        print(f"\n{'=' * 60}")
        print("DRC PASSED - No violations found")
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
        parts = []
        if counts["errors"]:
            parts.append(f"{counts['errors']} error{'s' if counts['errors'] != 1 else ''}")
        if counts["warnings"]:
            parts.append(f"{counts['warnings']} warning{'s' if counts['warnings'] != 1 else ''}")
        print(f"  {vtype}: {', '.join(parts)}")

    # Detailed output
    errors = [v for v in violations if v.is_error]
    warnings = [v for v in violations if not v.is_error]

    if errors:
        print(f"\n{'-' * 60}")
        print("ERRORS (must fix):")
        for v in errors:
            _print_single(v, verbose)

    if warnings:
        print(f"\n{'-' * 60}")
        print("WARNINGS (review recommended):")
        display_warnings = warnings if verbose else warnings[:10]
        for v in display_warnings:
            _print_single(v, verbose)
        if len(warnings) > 10 and not verbose:
            print(f"\n  ... and {len(warnings) - 10} more warnings (use --verbose)")

    print(f"\n{'=' * 60}")
    if errors:
        print("DRC FAILED - Fix errors before manufacturing")
    else:
        print("DRC WARNING - Review warnings")


def _print_single(v: DRCViolation, verbose: bool, indent: str = "  ") -> None:
    """Print a single violation."""
    symbol = "X" if v.is_error else "!"
    print(f"\n{indent}[{symbol}] {v.type_str}")
    print(f"{indent}    {v.message}")

    if verbose:
        if v.locations:
            for loc in v.locations:
                pos_str = f"({loc.x_mm:.2f}, {loc.y_mm:.2f})" if loc.x_mm or loc.y_mm else ""
                layer = f"[{loc.layer}]" if loc.layer else ""
                print(f"{indent}    -> {pos_str} {layer}")
        if v.nets:
            print(f"{indent}    Nets: {', '.join(v.nets)}")


def output_json(violations: List[DRCViolation], report: DRCReport) -> None:
    """Output violations as JSON."""
    data = {
        "source": report.source_file,
        "pcb_name": report.pcb_name,
        "summary": {
            "errors": sum(1 for v in violations if v.is_error),
            "warnings": sum(1 for v in violations if not v.is_error),
        },
        "violations": [v.to_dict() for v in violations],
    }
    print(json.dumps(data, indent=2))


def output_summary(violations: List[DRCViolation], report: DRCReport) -> None:
    """Output violation summary by type."""
    if not violations:
        print("No DRC violations found.")
        return

    print(f"DRC Summary: {report.source_file}")
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

    print(f"{'Type':<25} {'Errors':<8} {'Warnings':<8}")
    print("-" * 50)

    for type_name, counts in sorted(by_type.items()):
        print(f"{type_name:<25} {counts['errors']:<8} {counts['warnings']:<8}")

    print("-" * 50)
    total_errors = sum(c["errors"] for c in by_type.values())
    total_warnings = sum(c["warnings"] for c in by_type.values())
    print(f"{'TOTAL':<25} {total_errors:<8} {total_warnings:<8}")


def output_manufacturer_check(
    report: DRCReport,
    mfr: str,
    layers: int,
    verbose: bool,
) -> int:
    """Check DRC violations against manufacturer limits."""
    profile = get_profile(mfr)

    print(f"\n{'=' * 60}")
    print(f"MANUFACTURER COMPATIBILITY: {profile.name}")
    print(f"{'=' * 60}")
    print(f"Layer count: {layers}")
    print(f"Website: {profile.website}")

    checks = check_manufacturer_rules(report, mfr, layers=layers)

    compatible_count = 0
    incompatible = []

    for check in checks:
        if check.is_compatible:
            compatible_count += 1
        else:
            incompatible.append(check)

    if incompatible:
        print(f"\n{'-' * 60}")
        print(f"INCOMPATIBLE VIOLATIONS ({len(incompatible)}):")
        for check in incompatible:
            print(f"\n  [X] {check.violation.type_str}")
            print(f"      {check.violation.message}")
            if check.actual_value is not None and check.manufacturer_limit is not None:
                print(
                    f"      Actual: {check.actual_value:.3f}mm, Limit: {check.manufacturer_limit:.3f}mm"
                )
    else:
        print(f"\n{'-' * 60}")
        print(f"All {compatible_count} checked violations are compatible!")

    print(f"\n{'=' * 60}")
    print(f"Compatible: {compatible_count}, Incompatible: {len(incompatible)}")

    if profile.supports_assembly():
        print("Assembly: Supported")
        if profile.parts_library:
            print(f"Parts Library: {profile.parts_library.name}")
    else:
        print("Assembly: Not available")

    return 1 if incompatible else 0


def print_manufacturer_rules(mfr: str, layers: int) -> None:
    """Print manufacturing rules for a manufacturer."""
    profile = get_profile(mfr)
    rules = profile.get_design_rules(layers)

    print(f"\n{'=' * 60}")
    print(f"{profile.name.upper()} {layers}-LAYER PCB CAPABILITIES")
    print(f"{'=' * 60}")

    print("\nMinimum Values:")
    print(
        f"  Trace width:      {rules.min_trace_width_mm:.4f} mm ({rules.min_trace_width_mil:.1f} mil)"
    )
    print(
        f"  Trace spacing:    {rules.min_clearance_mm:.4f} mm ({rules.min_clearance_mil:.1f} mil)"
    )
    print(f"  Via drill:        {rules.min_via_drill_mm} mm")
    print(f"  Via diameter:     {rules.min_via_diameter_mm} mm")
    print(f"  Annular ring:     {rules.min_annular_ring_mm} mm")
    print(f"  Copper-to-edge:   {rules.min_copper_to_edge_mm} mm")

    print("\nSilkscreen:")
    print(f"  Min line width:   {rules.min_silkscreen_width_mm} mm")
    print(f"  Min text height:  {rules.min_silkscreen_height_mm} mm")

    print("\nBoard Specifications:")
    print(f"  Thickness:        {rules.board_thickness_mm} mm")
    print(f"  Outer copper:     {rules.outer_copper_oz} oz")
    if rules.inner_copper_oz > 0:
        print(f"  Inner copper:     {rules.inner_copper_oz} oz")

    print(f"\nWebsite: {profile.website}")

    if profile.supports_assembly():
        print("\nAssembly: Supported")
        if profile.parts_library:
            print(f"Parts Library: {profile.parts_library.name}")
            if profile.parts_library.catalog_url:
                print(f"Catalog: {profile.parts_library.catalog_url}")
    else:
        print("\nAssembly: Not available (PCB only)")

    print(f"\n{'=' * 60}")


def print_comparison(layers: int) -> None:
    """Print comparison of design rules across manufacturers."""
    rules_by_mfr = compare_design_rules(layers=layers)

    print(f"\n{'=' * 70}")
    print(f"MANUFACTURER COMPARISON - {layers}-LAYER PCB")
    print(f"{'=' * 70}")

    # Header
    mfrs = list(rules_by_mfr.keys())
    header = f"{'Constraint':<25}"
    for mfr in mfrs:
        header += f"{mfr.upper():>12}"
    print(header)
    print("-" * 70)

    # Trace width
    row = f"{'Trace width (mil)':<25}"
    for mfr in mfrs:
        row += f"{rules_by_mfr[mfr].min_trace_width_mil:>12.1f}"
    print(row)

    # Clearance
    row = f"{'Clearance (mil)':<25}"
    for mfr in mfrs:
        row += f"{rules_by_mfr[mfr].min_clearance_mil:>12.1f}"
    print(row)

    # Via drill
    row = f"{'Via drill (mm)':<25}"
    for mfr in mfrs:
        row += f"{rules_by_mfr[mfr].min_via_drill_mm:>12.2f}"
    print(row)

    # Via diameter
    row = f"{'Via diameter (mm)':<25}"
    for mfr in mfrs:
        row += f"{rules_by_mfr[mfr].min_via_diameter_mm:>12.2f}"
    print(row)

    # Copper to edge
    row = f"{'Copper-to-edge (mm)':<25}"
    for mfr in mfrs:
        row += f"{rules_by_mfr[mfr].min_copper_to_edge_mm:>12.2f}"
    print(row)

    print("-" * 70)

    # Assembly support
    row = f"{'Assembly':<25}"
    for mfr in mfrs:
        profile = get_profile(mfr)
        row += f"{'Yes':>12}" if profile.supports_assembly() else f"{'No':>12}"
    print(row)

    print(f"\n{'=' * 70}")


if __name__ == "__main__":
    sys.exit(main())
