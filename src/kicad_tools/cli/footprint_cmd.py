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

    # Fix pad spacing to meet clearance
    kicad-tools fix-footprints board.kicad_pcb --min-pad-gap 0.2

    # Preview fixes without applying
    kicad-tools fix-footprints board.kicad_pcb --dry-run
"""

import argparse
import json
import sys
from pathlib import Path
from typing import List, Optional

from kicad_tools.footprints.fixer import FootprintFix, FootprintFixer
from kicad_tools.footprints.validator import FootprintIssue, FootprintValidator
from kicad_tools.schema import PCB


def validate_footprints(
    pcb: PCB,
    min_pad_gap: float = 0.15,
    output_format: str = "text",
    errors_only: bool = False,
) -> List[FootprintIssue]:
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
    issues: List[FootprintIssue],
    output_format: str = "text",
    validator: Optional[FootprintValidator] = None,
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
            for name, count in sorted(
                summary["by_footprint_name"].items(), key=lambda x: -x[1]
            ):
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
) -> List[FootprintFix]:
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
    fixes: List[FootprintFix],
    output_format: str = "text",
    dry_run: bool = False,
    fixer: Optional[FootprintFixer] = None,
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
            for name, count in sorted(
                summary["by_footprint_name"].items(), key=lambda x: -x[1]
            ):
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


def main_validate(argv: Optional[List[str]] = None) -> int:
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

    args = parser.parse_args(argv)

    pcb_path = Path(args.pcb)
    if not pcb_path.exists():
        print(f"Error: File not found: {pcb_path}", file=sys.stderr)
        return 1

    try:
        pcb = PCB.load(str(pcb_path))
    except Exception as e:
        print(f"Error loading PCB: {e}", file=sys.stderr)
        return 1

    validator = FootprintValidator(min_pad_gap=args.min_pad_gap)
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


def main_fix(argv: Optional[List[str]] = None) -> int:
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

    args = parser.parse_args(argv)

    pcb_path = Path(args.pcb)
    if not pcb_path.exists():
        print(f"Error: File not found: {pcb_path}", file=sys.stderr)
        return 1

    try:
        pcb = PCB.load(str(pcb_path))
    except Exception as e:
        print(f"Error loading PCB: {e}", file=sys.stderr)
        return 1

    fixer = FootprintFixer(min_pad_gap=args.min_pad_gap)
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
            pcb.save(output_path)
            if args.format == "text":
                print(f"\nSaved to: {output_path}")
        except Exception as e:
            print(f"Error saving PCB: {e}", file=sys.stderr)
            return 1

    return 0


if __name__ == "__main__":
    sys.exit(main_validate())
