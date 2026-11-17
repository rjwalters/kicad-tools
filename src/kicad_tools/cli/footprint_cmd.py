#!/usr/bin/env python3
"""
Footprint validation and repair commands.

Usage:
    kicad-tools validate-footprints board.kicad_pcb [options]
    kicad-tools fix-footprints board.kicad_pcb [options]

Examples:
    # Validate footprints with default clearance (0.15mm)
    kicad-tools validate-footprints board.kicad_pcb

    # Validate with custom clearance
    kicad-tools validate-footprints board.kicad_pcb --min-pad-gap 0.2

    # Compare footprints against KiCad standard library
    kicad-tools validate-footprints board.kicad_pcb --compare-standard

    # Compare with custom tolerance
    kicad-tools validate-footprints board.kicad_pcb --compare-standard --tolerance 0.1

    # Fix pad spacing to meet clearance
    kicad-tools fix-footprints board.kicad_pcb --min-pad-gap 0.2

    # Preview fixes without applying
    kicad-tools fix-footprints board.kicad_pcb --dry-run
"""

import argparse
import json
import sys
from pathlib import Path

from kicad_tools.footprints.fixer import FootprintFix, FootprintFixer
from kicad_tools.footprints.validator import FootprintIssue, FootprintValidator
from kicad_tools.schema import PCB


def validate_footprints(
    pcb: PCB,
    min_pad_gap: float = 0.15,
    output_format: str = "text",
    errors_only: bool = False,
) -> list[FootprintIssue]:
    """Validate footprints in a PCB and return issues.

    Args:
        pcb: The PCB to validate
        min_pad_gap: Minimum required gap between pads in mm
        output_format: Output format ("text", "json", "summary")
        errors_only: Only show errors, not warnings

    Returns:
        List of detected issues
    """
    validator = FootprintValidator(min_pad_gap=min_pad_gap)
    issues = validator.validate_pcb(pcb)

    if errors_only:
        from kicad_tools.footprints.validator import IssueSeverity

        issues = [i for i in issues if i.severity == IssueSeverity.ERROR]

    return issues


def print_validation_results(
    issues: list[FootprintIssue],
    output_format: str = "text",
    validator: FootprintValidator | None = None,
) -> None:
    """Print validation results.

    Args:
        issues: List of issues to print
        output_format: Output format ("text", "json", "summary")
        validator: Optional validator for summary generation
    """
    if output_format == "json":
        data = [
            {
                "reference": i.footprint_ref,
                "footprint": i.footprint_name,
                "type": i.issue_type.value,
                "severity": i.severity.value,
                "message": i.message,
                "details": i.details,
            }
            for i in issues
        ]
        print(json.dumps(data, indent=2))
        return

    if output_format == "summary":
        if validator:
            summary = validator.summarize(issues)
            print(f"Total issues: {summary['total']}")
            print(f"Footprints with issues: {summary['footprints_with_issues']}")
            print("\nBy severity:")
            for severity, count in sorted(summary["by_severity"].items()):
                print(f"  {severity}: {count}")
            print("\nBy type:")
            for issue_type, count in sorted(summary["by_type"].items()):
                print(f"  {issue_type}: {count}")
            print("\nBy footprint type:")
            for name, count in sorted(summary["by_footprint_name"].items(), key=lambda x: -x[1]):
                print(f"  {name}: {count} instances")
        return

    # Text output
    if not issues:
        print("No footprint issues found.")
        return

    for issue in issues:
        print(issue)

    print(f"\nFound {len(issues)} footprint issue(s)")


def fix_footprints(
    pcb: PCB,
    min_pad_gap: float = 0.2,
    dry_run: bool = False,
) -> list[FootprintFix]:
    """Fix footprint pad spacing issues.

    Args:
        pcb: The PCB to fix
        min_pad_gap: Target gap between pads in mm
        dry_run: If True, calculate but don't apply changes

    Returns:
        List of fixes applied (or would be applied)
    """
    fixer = FootprintFixer(min_pad_gap=min_pad_gap)
    fixes = fixer.fix_pcb(pcb, dry_run=dry_run)
    return fixes


def print_fix_results(
    fixes: list[FootprintFix],
    output_format: str = "text",
    dry_run: bool = False,
    fixer: FootprintFixer | None = None,
) -> None:
    """Print fix results.

    Args:
        fixes: List of fixes to print
        output_format: Output format ("text", "json", "summary")
        dry_run: Whether this was a dry run
        fixer: Optional fixer for summary generation
    """
    if output_format == "json":
        data = [
            {
                "reference": f.footprint_ref,
                "footprint": f.footprint_name,
                "old_spacing_mm": f.old_pad_spacing,
                "new_spacing_mm": f.new_pad_spacing,
                "adjustments": [
                    {
                        "pad": a.pad_number,
                        "old_position": a.old_position,
                        "new_position": a.new_position,
                    }
                    for a in f.adjustments
                ],
            }
            for f in fixes
        ]
        print(json.dumps(data, indent=2))
        return

    if output_format == "summary":
        if fixer:
            summary = fixer.summarize(fixes)
            action = "would be fixed" if dry_run else "fixed"
            print(f"Total footprints {action}: {summary['total_footprints_fixed']}")
            print(f"Total pads adjusted: {summary['total_pads_adjusted']}")
            print("\nBy footprint type:")
            for name, count in sorted(summary["by_footprint_name"].items(), key=lambda x: -x[1]):
                print(f"  {name}: {count} instances")
        return

    # Text output
    if not fixes:
        print("No footprints needed fixing.")
        return

    action = "Would fix" if dry_run else "Fixed"
    for fix in fixes:
        print(
            f"{action} {fix.footprint_name}: "
            f"moved pads from {fix.old_pad_spacing:.3f}mm to {fix.new_pad_spacing:.3f}mm spacing "
            f"({len([f for f in fixes if f.footprint_name == fix.footprint_name])} instances)"
        )

    # Group by footprint name for summary
    by_name: dict[str, int] = {}
    for fix in fixes:
        by_name[fix.footprint_name] = by_name.get(fix.footprint_name, 0) + 1

    print(f"\n{action} {len(fixes)} footprint(s)")


def main_validate(argv: list[str] | None = None) -> int:
    """Main entry point for validate-footprints command."""
    parser = argparse.ArgumentParser(
        description="Validate footprints in a KiCad PCB file",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("pcb", help="Path to .kicad_pcb file")
    parser.add_argument(
        "--min-pad-gap",
        type=float,
        default=0.15,
        help="Minimum required gap between pads in mm (default: 0.15)",
    )
    parser.add_argument(
        "--format",
        choices=["text", "json", "summary"],
        default="text",
        help="Output format (default: text)",
    )
    parser.add_argument(
        "--errors-only",
        action="store_true",
        help="Only show errors, not warnings",
    )
    parser.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="Suppress progress output (for scripting)",
    )
    # Standard library comparison options
    parser.add_argument(
        "--compare-standard",
        action="store_true",
        help="Compare footprints against KiCad standard library",
    )
    parser.add_argument(
        "--tolerance",
        type=float,
        default=0.05,
        help="Tolerance for standard comparison in mm (default: 0.05)",
    )
    parser.add_argument(
        "--kicad-library-path",
        type=str,
        default=None,
        help="Override path to KiCad footprint libraries",
    )

    args = parser.parse_args(argv)

    from kicad_tools.cli.progress import spinner

    pcb_path = Path(args.pcb)
    if not pcb_path.exists():
        print(f"Error: File not found: {pcb_path}", file=sys.stderr)
        return 1

    try:
        with spinner("Loading PCB...", quiet=args.quiet):
            pcb = PCB.load(str(pcb_path))
    except Exception as e:
        print(f"Error loading PCB: {e}", file=sys.stderr)
        return 1

    # Handle standard library comparison mode
    if args.compare_standard:
        return _run_standard_comparison(
            pcb,
            tolerance=args.tolerance,
            library_path=args.kicad_library_path,
            output_format=args.format,
            errors_only=args.errors_only,
            quiet=args.quiet,
        )

    # Normal validation mode
    validator = FootprintValidator(min_pad_gap=args.min_pad_gap)

    with spinner("Validating footprints...", quiet=args.quiet):
        issues = validate_footprints(
            pcb,
            min_pad_gap=args.min_pad_gap,
            output_format=args.format,
            errors_only=args.errors_only,
        )

    print_validation_results(issues, output_format=args.format, validator=validator)

    # Return non-zero if there are errors
    from kicad_tools.footprints.validator import IssueSeverity

    has_errors = any(i.severity == IssueSeverity.ERROR for i in issues)
    return 1 if has_errors else 0


def _run_standard_comparison(
    pcb: PCB,
    tolerance: float,
    library_path: str | None,
    output_format: str,
    errors_only: bool,
    quiet: bool,
) -> int:
    """Run standard library comparison mode."""
    from kicad_tools.cli.progress import spinner
    from kicad_tools.footprints.standard_comparison import (
        StandardFootprintComparator,
    )

    comparator = StandardFootprintComparator(
        tolerance_mm=tolerance,
        library_path=library_path,
    )

    if not comparator.library_found:
        print(
            "Warning: KiCad standard footprint library not found.",
            file=sys.stderr,
        )
        print(
            "Use --kicad-library-path to specify the location, or set KICAD_FOOTPRINT_DIR",
            file=sys.stderr,
        )
        return 1

    if not quiet:
        print(f"Using KiCad library: {comparator.library_path}")

    with spinner("Comparing footprints against standard library...", quiet=quiet):
        comparisons = comparator.compare_pcb(pcb)

    # Filter if errors only
    if errors_only:
        comparisons = [c for c in comparisons if c.error_count > 0]

    # Output results
    if output_format == "json":
        _print_comparison_json(comparisons)
    elif output_format == "summary":
        _print_comparison_summary(comparator.summarize(comparisons))
    else:
        _print_comparison_text(comparisons, errors_only)

    # Return non-zero if there are errors
    total_errors = sum(c.error_count for c in comparisons)
    return 1 if total_errors > 0 else 0


def _print_comparison_text(comparisons, errors_only: bool) -> None:
    """Print comparison results in text format."""
    from kicad_tools.footprints.standard_comparison import ComparisonSeverity

    print("\nComparing footprints against KiCad standard library...\n")

    matching_count = 0
    not_found_count = 0

    for comp in comparisons:
        if not comp.found_standard:
            not_found_count += 1
            continue

        if comp.matches_standard:
            matching_count += 1
            continue

        # Has issues - print details
        print(f"{comp.footprint_ref} ({comp.footprint_name}):")

        for pad_comp in comp.pad_comparisons:
            if errors_only and pad_comp.severity != ComparisonSeverity.ERROR:
                continue

            severity = pad_comp.severity.value.upper()
            print(f"  {severity}: {pad_comp.message}")

            if pad_comp.our_value is not None and pad_comp.standard_value is not None:
                if isinstance(pad_comp.our_value, tuple):
                    print(f"    Ours: ({pad_comp.our_value[0]:.3f}, {pad_comp.our_value[1]:.3f})")
                    print(
                        f"    Standard: "
                        f"({pad_comp.standard_value[0]:.3f}, {pad_comp.standard_value[1]:.3f})"
                    )
                else:
                    print(f"    Ours: {pad_comp.our_value}")
                    print(f"    Standard: {pad_comp.standard_value}")

            if pad_comp.delta is not None:
                if isinstance(pad_comp.delta, tuple):
                    print(f"    Delta: ({pad_comp.delta[0]:.3f}, {pad_comp.delta[1]:.3f})mm")
                else:
                    print(f"    Delta: {pad_comp.delta:.3f}mm")

            if pad_comp.delta_percent is not None:
                print(f"    Delta: {pad_comp.delta_percent:.1f}%")

        print()

    # Summary
    total_checked = len(comparisons)
    with_issues = sum(1 for c in comparisons if c.has_issues and c.found_standard)

    print(f"Summary: {total_checked} footprints checked")
    print(f"  - {matching_count} matching standard")
    print(f"  - {with_issues} with warnings/errors")
    print(f"  - {not_found_count} not found in standard library")


def _print_comparison_json(comparisons) -> None:
    """Print comparison results in JSON format."""
    data = []
    for comp in comparisons:
        item = {
            "reference": comp.footprint_ref,
            "footprint": comp.footprint_name,
            "standard_library": comp.standard_library,
            "standard_footprint": comp.standard_footprint,
            "found_standard": comp.found_standard,
            "matches_standard": comp.matches_standard,
            "error_count": comp.error_count,
            "warning_count": comp.warning_count,
            "issues": [
                {
                    "pad": p.pad_number,
                    "type": p.comparison_type.value,
                    "severity": p.severity.value,
                    "message": p.message,
                    "our_value": p.our_value,
                    "standard_value": p.standard_value,
                    "delta": p.delta,
                    "delta_percent": p.delta_percent,
                }
                for p in comp.pad_comparisons
            ],
        }
        data.append(item)

    print(json.dumps(data, indent=2))


def _print_comparison_summary(summary: dict) -> None:
    """Print comparison summary."""
    print("Footprint Standard Library Comparison Summary")
    print("=" * 50)
    print(f"Total footprints checked: {summary['total_checked']}")
    print(f"Found in standard library: {summary['found_standard']}")
    print(f"Not found: {summary['not_found']}")
    print(f"Matching standard: {summary['matching_standard']}")
    print(f"With issues: {summary['with_issues']}")
    print()
    print(f"Total errors: {summary['total_errors']}")
    print(f"Total warnings: {summary['total_warnings']}")

    if summary["by_footprint_name"]:
        print("\nIssues by footprint type:")
        for name, count in sorted(summary["by_footprint_name"].items(), key=lambda x: -x[1]):
            print(f"  {name}: {count}")


def main_fix(argv: list[str] | None = None) -> int:
    """Main entry point for fix-footprints command."""
    parser = argparse.ArgumentParser(
        description="Fix footprint pad spacing issues in a KiCad PCB file",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("pcb", help="Path to .kicad_pcb file")
    parser.add_argument(
        "-o",
        "--output",
        help="Output file path (default: modify in place)",
    )
    parser.add_argument(
        "--min-pad-gap",
        type=float,
        default=0.2,
        help="Target gap between pads in mm (default: 0.2)",
    )
    parser.add_argument(
        "--format",
        choices=["text", "json", "summary"],
        default="text",
        help="Output format (default: text)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be changed without applying",
    )
    parser.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="Suppress progress output (for scripting)",
    )

    args = parser.parse_args(argv)

    from kicad_tools.cli.progress import spinner

    pcb_path = Path(args.pcb)
    if not pcb_path.exists():
        print(f"Error: File not found: {pcb_path}", file=sys.stderr)
        return 1

    try:
        with spinner("Loading PCB...", quiet=args.quiet):
            pcb = PCB.load(str(pcb_path))
    except Exception as e:
        print(f"Error loading PCB: {e}", file=sys.stderr)
        return 1

    fixer = FootprintFixer(min_pad_gap=args.min_pad_gap)

    with spinner("Analyzing footprints...", quiet=args.quiet):
        fixes = fix_footprints(
            pcb,
            min_pad_gap=args.min_pad_gap,
            dry_run=args.dry_run,
        )

    print_fix_results(fixes, output_format=args.format, dry_run=args.dry_run, fixer=fixer)

    # Save changes if not dry run and there are fixes
    if fixes and not args.dry_run:
        output_path = args.output or str(pcb_path)
        try:
            with spinner("Saving PCB...", quiet=args.quiet):
                pcb.save(output_path)
            if args.format == "text":
                print(f"\nSaved to: {output_path}")
        except Exception as e:
            print(f"Error saving PCB: {e}", file=sys.stderr)
            return 1

    return 0


if __name__ == "__main__":
    sys.exit(main_validate())
