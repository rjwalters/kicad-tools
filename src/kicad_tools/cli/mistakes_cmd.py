"""
CLI command for detecting common PCB design mistakes.

Provides command-line access to the mistake detection module:

    kct detect-mistakes board.kicad_pcb
    kct detect-mistakes board.kicad_pcb --format json
    kct detect-mistakes board.kicad_pcb --category bypass_capacitor
    kct detect-mistakes board.kicad_pcb --severity warning

Usage:
    kicad-tools detect-mistakes <pcb_file>          # Detect all mistakes
    kicad-tools detect-mistakes <pcb_file> --json   # Output as JSON
    kicad-tools detect-mistakes --list-categories   # List check categories

Exit Codes:
    0 - No errors found (warnings may be present)
    1 - Errors found or command failure
    2 - Warnings found (only with --strict)
"""

import argparse
import json
import sys
from pathlib import Path

from kicad_tools.explain.mistakes import MistakeCategory


def main(argv: list[str] | None = None) -> int:
    """Main entry point for detect-mistakes command."""
    parser = argparse.ArgumentParser(
        prog="kicad-tools detect-mistakes",
        description="Detect common PCB design mistakes with educational explanations",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    # Main argument - PCB file
    parser.add_argument(
        "pcb_file",
        nargs="?",
        help="Path to .kicad_pcb file to analyze",
    )

    # Category filter
    parser.add_argument(
        "--category",
        "-c",
        choices=[cat.value for cat in MistakeCategory],
        help="Only check specific category",
    )

    # Severity filter
    parser.add_argument(
        "--severity",
        "-s",
        choices=["error", "warning", "info"],
        help="Only show issues of this severity or higher",
    )

    # Output format
    parser.add_argument(
        "--format",
        "-f",
        choices=["table", "json", "tree", "summary"],
        default="table",
        help="Output format (default: table)",
    )

    # Strict mode
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit with error code on warnings",
    )

    # List categories
    parser.add_argument(
        "--list-categories",
        action="store_true",
        help="List available check categories and exit",
    )

    # Verbose output
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Show detailed information",
    )

    args = parser.parse_args(argv)

    # Handle list categories mode
    if args.list_categories:
        return _list_categories()

    # Require PCB file for other modes
    if not args.pcb_file:
        parser.print_help()
        print("\nError: pcb_file required", file=sys.stderr)
        return 1

    # Load and analyze PCB
    return _analyze_pcb(args)


def _list_categories() -> int:
    """List all available check categories."""
    from kicad_tools.explain.mistakes import get_default_checks

    print("Available Mistake Categories:")
    print("=" * 50)

    # Get checks and group by category
    checks = get_default_checks()
    by_category: dict[MistakeCategory, list] = {}
    for check in checks:
        cat = check.category
        if cat not in by_category:
            by_category[cat] = []
        by_category[cat].append(check)

    for cat in MistakeCategory:
        checks_in_cat = by_category.get(cat, [])
        check_count = len(checks_in_cat)

        # Get description from docstring
        desc = _category_description(cat)

        print(f"\n{cat.value}")
        print(f"  {desc}")
        print(f"  Checks: {check_count}")
        for check in checks_in_cat:
            check_name = type(check).__name__
            print(f"    - {check_name}")

    print()
    print(f"Total categories: {len(MistakeCategory)}")
    print(f"Total checks: {len(checks)}")

    return 0


def _category_description(cat: MistakeCategory) -> str:
    """Get human-readable description for a category."""
    descriptions = {
        MistakeCategory.BYPASS_CAP: "Bypass capacitor placement issues",
        MistakeCategory.CRYSTAL: "Crystal oscillator layout problems",
        MistakeCategory.DIFFERENTIAL_PAIR: "Differential pair routing issues",
        MistakeCategory.POWER_TRACE: "Power trace width problems",
        MistakeCategory.THERMAL: "Thermal management issues",
        MistakeCategory.EMI: "EMI and shielding concerns",
        MistakeCategory.DECOUPLING: "Decoupling capacitor issues",
        MistakeCategory.GROUNDING: "Grounding and return path issues",
        MistakeCategory.VIA: "Via placement problems",
        MistakeCategory.MANUFACTURABILITY: "Manufacturing-related issues",
    }
    return descriptions.get(cat, "General PCB design issues")


def _analyze_pcb(args) -> int:
    """Load PCB and detect mistakes."""
    from kicad_tools.explain.mistakes import (
        MistakeCategory,
        MistakeDetector,
        detect_mistakes,
    )
    from kicad_tools.schema.pcb import PCB

    pcb_path = Path(args.pcb_file)

    if not pcb_path.exists():
        print(f"Error: File not found: {pcb_path}", file=sys.stderr)
        return 1

    if pcb_path.suffix != ".kicad_pcb":
        print(f"Error: Expected .kicad_pcb file, got {pcb_path.suffix}", file=sys.stderr)
        return 1

    # Load PCB
    try:
        print(f"Analyzing: {pcb_path.name}")
        pcb = PCB.load(str(pcb_path))
    except Exception as e:
        print(f"Error loading PCB: {e}", file=sys.stderr)
        return 1

    # Detect mistakes
    if args.category:
        cat = MistakeCategory(args.category)
        detector = MistakeDetector()
        mistakes = detector.detect_by_category(pcb, cat)
    else:
        mistakes = detect_mistakes(pcb)

    # Filter by severity if specified
    if args.severity:
        severity_order = {"error": 0, "warning": 1, "info": 2}
        min_severity = severity_order[args.severity]
        mistakes = [m for m in mistakes if severity_order.get(m.severity, 99) <= min_severity]

    # Output results
    if args.format == "json":
        _output_json(mistakes)
    elif args.format == "tree":
        _output_tree(mistakes)
    elif args.format == "summary":
        _output_summary(mistakes)
    else:
        _output_table(mistakes, args.verbose)

    # Determine exit code
    error_count = sum(1 for m in mistakes if m.severity == "error")
    warning_count = sum(1 for m in mistakes if m.severity == "warning")

    if error_count > 0:
        return 1
    elif warning_count > 0 and args.strict:
        return 2
    return 0


def _output_table(mistakes: list, verbose: bool = False) -> None:
    """Output mistakes as formatted table."""
    if not mistakes:
        print("\n" + "=" * 60)
        print("NO DESIGN MISTAKES DETECTED")
        print("=" * 60)
        print("Your PCB passed all checks!")
        return

    error_count = sum(1 for m in mistakes if m.severity == "error")
    warning_count = sum(1 for m in mistakes if m.severity == "warning")
    info_count = sum(1 for m in mistakes if m.severity == "info")

    print("\n" + "=" * 60)
    print("PCB DESIGN MISTAKE ANALYSIS")
    print("=" * 60)

    print("\nSummary:")
    print(f"  Errors:   {error_count}")
    print(f"  Warnings: {warning_count}")
    print(f"  Info:     {info_count}")

    # Group by category
    by_category: dict = {}
    for m in mistakes:
        cat = m.category.value
        if cat not in by_category:
            by_category[cat] = []
        by_category[cat].append(m)

    print("\n" + "-" * 60)
    print("BY CATEGORY:")
    for cat, cat_mistakes in sorted(by_category.items()):
        print(f"  {cat}: {len(cat_mistakes)} issue(s)")

    # Show detailed mistakes
    errors = [m for m in mistakes if m.severity == "error"]
    warnings = [m for m in mistakes if m.severity == "warning"]
    infos = [m for m in mistakes if m.severity == "info"]

    if errors:
        print("\n" + "-" * 60)
        print("ERRORS (must fix):")
        for m in errors:
            _print_mistake(m, verbose)

    if warnings:
        print("\n" + "-" * 60)
        print("WARNINGS (should review):")
        display_warnings = warnings if verbose else warnings[:10]
        for m in display_warnings:
            _print_mistake(m, verbose)
        if len(warnings) > 10 and not verbose:
            print(f"\n  ... and {len(warnings) - 10} more warnings (use --verbose)")

    if infos and verbose:
        print("\n" + "-" * 60)
        print("INFO (suggestions):")
        for m in infos:
            _print_mistake(m, verbose)

    print("\n" + "=" * 60)
    if errors:
        print("FIX ERRORS BEFORE MANUFACTURING")
    elif warnings:
        print("REVIEW WARNINGS FOR BEST RESULTS")
    else:
        print("DESIGN LOOKS GOOD!")


def _print_mistake(m, verbose: bool = False) -> None:
    """Print a single mistake."""
    symbol = {"error": "X", "warning": "!", "info": "i"}.get(m.severity, "?")
    print(f"\n  [{symbol}] {m.title}")
    print(f"      Components: {', '.join(m.components)}")

    if m.location:
        print(f"      Location: ({m.location[0]:.2f}, {m.location[1]:.2f}) mm")

    if verbose:
        print(f"      Problem: {m.explanation}")
        print(f"      Fix: {m.fix_suggestion}")
        if m.learn_more_url:
            print(f"      Learn more: {m.learn_more_url}")


def _output_json(mistakes: list) -> None:
    """Output mistakes as JSON."""
    data = {
        "summary": {
            "errors": sum(1 for m in mistakes if m.severity == "error"),
            "warnings": sum(1 for m in mistakes if m.severity == "warning"),
            "info": sum(1 for m in mistakes if m.severity == "info"),
        },
        "mistakes": [m.to_dict() for m in mistakes],
    }
    print(json.dumps(data, indent=2))


def _output_tree(mistakes: list) -> None:
    """Output mistakes in tree format."""
    if not mistakes:
        print("No design mistakes detected.")
        return

    for m in mistakes:
        print(m.format_tree())
        print()


def _output_summary(mistakes: list) -> None:
    """Output summary only."""
    error_count = sum(1 for m in mistakes if m.severity == "error")
    warning_count = sum(1 for m in mistakes if m.severity == "warning")
    info_count = sum(1 for m in mistakes if m.severity == "info")

    print(f"Errors: {error_count}, Warnings: {warning_count}, Info: {info_count}")

    if not mistakes:
        print("No design mistakes detected!")
    else:
        # Group by category
        by_category: dict = {}
        for m in mistakes:
            cat = m.category.value
            by_category[cat] = by_category.get(cat, 0) + 1

        for cat, count in sorted(by_category.items()):
            print(f"  {cat}: {count}")


if __name__ == "__main__":
    sys.exit(main())
