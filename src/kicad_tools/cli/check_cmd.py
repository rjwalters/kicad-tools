"""
Pure Python DRC check command for KiCad PCBs.

Runs design rule checks against manufacturer specifications without
requiring kicad-cli to be installed. Suitable for CI/CD pipelines.

Usage:
    kct check board.kicad_pcb                      # Run all checks
    kct check board.kicad_pcb --mfr jlcpcb         # With manufacturer rules
    kct check board.kicad_pcb --format json        # JSON output for CI
    kct check board.kicad_pcb --only clearance     # Run specific checks
    kct check board.kicad_pcb --skip silkscreen    # Exclude checks

Exit Codes:
    0 - No errors (warnings may be present)
    1 - Errors found or command failure
    2 - Warnings found (only with --strict)

Difference from `kct drc`:
    - kct drc: Uses kicad-cli to run DRC (requires KiCad)
    - kct check: Pure Python DRC (no external dependencies)
"""

import argparse
import json
import sys
from pathlib import Path

from kicad_tools.manufacturers import get_manufacturer_ids
from kicad_tools.schema.pcb import PCB
from kicad_tools.validate import DRCChecker, DRCResults, DRCViolation

# Available check categories
CHECK_CATEGORIES = ["clearance", "dimensions", "edge", "silkscreen"]


def main(argv: list[str] | None = None) -> int:
    """Main entry point for kct check command."""
    parser = argparse.ArgumentParser(
        prog="kct check",
        description="Pure Python DRC for PCBs (no kicad-cli required)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "pcb",
        help="Path to .kicad_pcb file to check",
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
        help="Exit with error code 2 on warnings",
    )
    parser.add_argument(
        "--mfr",
        "-m",
        choices=get_manufacturer_ids(),
        default="jlcpcb",
        help="Target manufacturer for design rules (default: jlcpcb)",
    )
    parser.add_argument(
        "--layers",
        "-l",
        type=int,
        default=2,
        help="Number of copper layers (default: 2)",
    )
    parser.add_argument(
        "--copper",
        "-c",
        type=float,
        default=1.0,
        help="Copper weight in oz (default: 1.0)",
    )
    parser.add_argument(
        "--only",
        dest="only_checks",
        help=f"Run only specific checks (comma-separated: {', '.join(CHECK_CATEGORIES)})",
    )
    parser.add_argument(
        "--skip",
        dest="skip_checks",
        help=f"Skip specific checks (comma-separated: {', '.join(CHECK_CATEGORIES)})",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Show detailed violation information",
    )

    args = parser.parse_args(argv)

    # Parse and validate filter options
    only_set: set[str] | None = None
    skip_set: set[str] = set()

    if args.only_checks:
        only_set = set()
        for cat in args.only_checks.split(","):
            cat = cat.strip().lower()
            if cat not in CHECK_CATEGORIES:
                print(f"Error: Unknown check category: {cat!r}", file=sys.stderr)
                print(f"Available: {', '.join(CHECK_CATEGORIES)}", file=sys.stderr)
                return 1
            only_set.add(cat)

    if args.skip_checks:
        for cat in args.skip_checks.split(","):
            cat = cat.strip().lower()
            if cat not in CHECK_CATEGORIES:
                print(f"Error: Unknown check category: {cat!r}", file=sys.stderr)
                print(f"Available: {', '.join(CHECK_CATEGORIES)}", file=sys.stderr)
                return 1
            skip_set.add(cat)

    # Load PCB
    pcb_path = Path(args.pcb)
    if not pcb_path.exists():
        print(f"Error: PCB file not found: {pcb_path}", file=sys.stderr)
        return 1

    if pcb_path.suffix != ".kicad_pcb":
        print(f"Error: Expected .kicad_pcb file, got: {pcb_path.suffix}", file=sys.stderr)
        return 1

    try:
        pcb = PCB.load(pcb_path)
    except Exception as e:
        print(f"Error loading PCB: {e}", file=sys.stderr)
        return 1

    # Create checker with manufacturer rules
    try:
        checker = DRCChecker(
            pcb,
            manufacturer=args.mfr,
            layers=args.layers,
            copper_oz=args.copper,
        )
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    # Run selected checks
    results = run_selected_checks(checker, only_set, skip_set)

    # Apply errors-only filter
    violations = list(results.violations)
    if args.errors_only:
        violations = [v for v in violations if v.is_error]

    # Output results
    if args.format == "json":
        output_json(violations, results, pcb_path, args.mfr, args.layers)
    elif args.format == "summary":
        output_summary(violations, results, pcb_path)
    else:
        output_table(violations, results, pcb_path, args.mfr, args.layers, args.verbose)

    # Determine exit code
    error_count = sum(1 for v in violations if v.is_error)
    warning_count = len(violations) - error_count

    if error_count > 0:
        return 1
    elif warning_count > 0 and args.strict:
        return 2
    return 0


def run_selected_checks(
    checker: DRCChecker,
    only_set: set[str] | None,
    skip_set: set[str],
) -> DRCResults:
    """Run the selected DRC checks based on filters."""
    results = DRCResults()

    # Map of category to check method
    check_methods = {
        "clearance": checker.check_clearances,
        "dimensions": checker.check_dimensions,
        "edge": checker.check_edge_clearances,
        "silkscreen": checker.check_silkscreen,
    }

    for category, method in check_methods.items():
        # Skip if --only specified and this category not in it
        if only_set is not None and category not in only_set:
            continue

        # Skip if this category is in --skip
        if category in skip_set:
            continue

        # Run the check
        category_results = method()
        results.merge(category_results)

    return results


def output_table(
    violations: list[DRCViolation],
    results: DRCResults,
    pcb_path: Path,
    mfr: str,
    layers: int,
    verbose: bool = False,
) -> None:
    """Output violations as a formatted table."""
    error_count = sum(1 for v in violations if v.is_error)
    warning_count = len(violations) - error_count

    print(f"\n{'=' * 60}")
    print("PURE PYTHON DRC CHECK")
    print(f"{'=' * 60}")
    print(f"File: {pcb_path.name}")
    print(f"Manufacturer: {mfr.upper()}")
    print(f"Layers: {layers}")
    print(f"Rules checked: {results.rules_checked}")

    print("\nResults:")
    print(f"  Errors:     {error_count}")
    print(f"  Warnings:   {warning_count}")

    if not violations:
        print(f"\n{'=' * 60}")
        print("DRC PASSED - No violations found")
        return

    # Group by rule_id summary
    by_rule: dict[str, dict[str, int]] = {}
    for v in violations:
        if v.rule_id not in by_rule:
            by_rule[v.rule_id] = {"errors": 0, "warnings": 0}
        if v.is_error:
            by_rule[v.rule_id]["errors"] += 1
        else:
            by_rule[v.rule_id]["warnings"] += 1

    print(f"\n{'-' * 60}")
    print("BY RULE:")
    for rule_id, counts in sorted(
        by_rule.items(), key=lambda x: -(x[1]["errors"] + x[1]["warnings"])
    ):
        parts = []
        if counts["errors"]:
            parts.append(f"{counts['errors']} error{'s' if counts['errors'] != 1 else ''}")
        if counts["warnings"]:
            parts.append(f"{counts['warnings']} warning{'s' if counts['warnings'] != 1 else ''}")
        print(f"  {rule_id}: {', '.join(parts)}")

    # Detailed output
    errors = [v for v in violations if v.is_error]
    warnings = [v for v in violations if not v.is_error]

    if errors:
        print(f"\n{'-' * 60}")
        print("ERRORS (must fix):")
        for v in errors:
            _print_violation(v, verbose)

    if warnings:
        print(f"\n{'-' * 60}")
        print("WARNINGS (review recommended):")
        display_warnings = warnings if verbose else warnings[:10]
        for v in display_warnings:
            _print_violation(v, verbose)
        if len(warnings) > 10 and not verbose:
            print(f"\n  ... and {len(warnings) - 10} more warnings (use --verbose)")

    print(f"\n{'=' * 60}")
    if errors:
        print("DRC FAILED - Fix errors before manufacturing")
    else:
        print("DRC WARNING - Review warnings")


def _print_violation(v: DRCViolation, verbose: bool, indent: str = "  ") -> None:
    """Print a single violation."""
    symbol = "X" if v.is_error else "!"
    print(f"\n{indent}[{symbol}] {v.rule_id}")
    print(f"{indent}    {v.message}")

    if verbose:
        if v.location:
            print(f"{indent}    -> ({v.location[0]:.2f}, {v.location[1]:.2f}) mm")
        if v.layer:
            print(f"{indent}    Layer: {v.layer}")
        if v.actual_value is not None and v.required_value is not None:
            print(f"{indent}    Actual: {v.actual_value:.3f}mm, Required: {v.required_value:.3f}mm")
        if v.items:
            print(f"{indent}    Items: {', '.join(v.items)}")


def output_json(
    violations: list[DRCViolation],
    results: DRCResults,
    pcb_path: Path,
    mfr: str,
    layers: int,
) -> None:
    """Output violations as JSON."""
    error_count = sum(1 for v in violations if v.is_error)
    warning_count = len(violations) - error_count

    data = {
        "file": str(pcb_path),
        "manufacturer": mfr,
        "layers": layers,
        "summary": {
            "errors": error_count,
            "warnings": warning_count,
            "rules_checked": results.rules_checked,
            "passed": error_count == 0,
        },
        "violations": [v.to_dict() for v in violations],
    }
    print(json.dumps(data, indent=2))


def output_summary(
    violations: list[DRCViolation],
    results: DRCResults,
    pcb_path: Path,
) -> None:
    """Output violation summary by rule."""
    if not violations:
        print(f"DRC PASSED: {pcb_path.name}")
        print(f"  {results.rules_checked} rules checked, no violations found.")
        return

    print(f"DRC Summary: {pcb_path.name}")
    print("=" * 50)

    # Group by rule_id
    by_rule: dict[str, dict[str, int]] = {}
    for v in violations:
        key = v.rule_id
        if key not in by_rule:
            by_rule[key] = {"errors": 0, "warnings": 0}
        if v.is_error:
            by_rule[key]["errors"] += 1
        else:
            by_rule[key]["warnings"] += 1

    print(f"{'Rule ID':<30} {'Errors':<8} {'Warnings':<8}")
    print("-" * 50)

    for rule_id, counts in sorted(by_rule.items()):
        print(f"{rule_id:<30} {counts['errors']:<8} {counts['warnings']:<8}")

    print("-" * 50)
    total_errors = sum(c["errors"] for c in by_rule.values())
    total_warnings = sum(c["warnings"] for c in by_rule.values())
    print(f"{'TOTAL':<30} {total_errors:<8} {total_warnings:<8}")


if __name__ == "__main__":
    sys.exit(main())
