"""
CLI command for explaining design rules and DRC violations.

Provides command-line access to the explain module:

    kct explain trace_clearance
    kct explain trace_clearance --value 0.15 --required 0.2
    kct explain --list
    kct explain --search clearance
    kct explain --drc-report design-drc.rpt
"""

import argparse
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    """Main entry point for explain command."""
    parser = argparse.ArgumentParser(
        prog="kicad-tools explain",
        description="Explain design rules and DRC violations",
    )

    # Main argument - rule ID (optional if using --list or --search)
    parser.add_argument(
        "rule",
        nargs="?",
        help="Rule ID to explain (e.g., trace_clearance, via_drill)",
    )

    # Discovery options
    parser.add_argument(
        "--list",
        "-l",
        action="store_true",
        help="List all available rule IDs",
    )
    parser.add_argument(
        "--search",
        "-s",
        metavar="QUERY",
        help="Search for rules matching a query",
    )

    # Context values
    parser.add_argument(
        "--value",
        "-v",
        type=float,
        help="Current/actual value for contextualized explanation",
    )
    parser.add_argument(
        "--required",
        "-r",
        type=float,
        help="Required/minimum value",
    )
    parser.add_argument(
        "--unit",
        "-u",
        default="mm",
        help="Unit of measurement (default: mm)",
    )
    parser.add_argument(
        "--net1",
        help="First net name for context",
    )
    parser.add_argument(
        "--net2",
        help="Second net name for context",
    )

    # DRC report integration
    parser.add_argument(
        "--drc-report",
        "-d",
        metavar="FILE",
        help="Path to DRC report file to explain all violations",
    )

    # Output format
    parser.add_argument(
        "--format",
        "-f",
        choices=["text", "tree", "json", "markdown"],
        default="text",
        help="Output format (default: text)",
    )

    # Interface/net explanation
    parser.add_argument(
        "--net",
        "-n",
        metavar="NAME",
        help="Explain constraints for a specific net",
    )
    parser.add_argument(
        "--interface",
        "-i",
        help="Specify interface type for net (usb, i2c, spi)",
    )

    args = parser.parse_args(argv)

    # Handle list mode
    if args.list:
        return _list_rules()

    # Handle search mode
    if args.search:
        return _search_rules(args.search, args.format)

    # Handle DRC report mode
    if args.drc_report:
        return _explain_drc_report(args.drc_report, args.format)

    # Handle net explanation mode
    if args.net:
        return _explain_net(args.net, args.interface, args.format)

    # Handle single rule explanation
    if not args.rule:
        parser.print_help()
        return 0

    return _explain_rule(args)


def _list_rules() -> int:
    """List all available rule IDs."""
    from kicad_tools.explain import list_rules

    rules = list_rules()

    if not rules:
        print("No rules found. Check that spec YAML files are present.")
        return 1

    print("Available Rules:")
    print("=" * 40)

    for rule in rules:
        print(f"  {rule}")

    print()
    print(f"Total: {len(rules)} rules")
    print("\nUse 'kct explain <rule>' to see details.")

    return 0


def _search_rules(query: str, format_type: str) -> int:
    """Search for rules matching a query."""
    from kicad_tools.explain import format_result, search_rules

    matches = search_rules(query)

    if not matches:
        print(f"No rules found matching '{query}'")
        return 1

    print(f"Rules matching '{query}':")
    print("=" * 40)

    for exp in matches:
        if format_type == "json":
            import json

            print(json.dumps(exp.to_dict(), indent=2))
        else:
            print(f"\n{exp.rule_id}: {exp.title}")
            print(f"  {exp.explanation[:100]}...")

    print()
    print(f"Total: {len(matches)} matches")

    return 0


def _explain_drc_report(report_path: str, format_type: str) -> int:
    """Explain all violations in a DRC report."""
    from kicad_tools.drc import DRCReport
    from kicad_tools.explain import explain_violations, format_violations

    path = Path(report_path)
    if not path.exists():
        print(f"Error: File not found: {report_path}", file=sys.stderr)
        return 1

    try:
        report = DRCReport.load(str(path))
    except Exception as e:
        print(f"Error loading DRC report: {e}", file=sys.stderr)
        return 1

    if not report.violations:
        print("No violations found in report.")
        return 0

    # Explain all violations
    explained = explain_violations(report.violations)

    # Format output
    output = format_violations(explained, format_type)
    print(output)

    return 0


def _explain_net(net_name: str, interface_type: str | None, format_type: str) -> int:
    """Explain constraints for a specific net."""
    from kicad_tools.explain import explain_net_constraints, format_result

    result = explain_net_constraints(net_name, interface_type)

    output = format_result(result, format_type)
    print(output)

    return 0


def _explain_rule(args) -> int:
    """Explain a single rule with optional context."""
    from kicad_tools.explain import explain, format_result

    # Build context from arguments
    context = {}
    if args.value is not None:
        context["value"] = args.value
    if args.required is not None:
        context["required_value"] = args.required
    if args.unit:
        context["unit"] = args.unit
    if args.net1:
        context["net1"] = args.net1
    if args.net2:
        context["net2"] = args.net2

    try:
        result = explain(args.rule, context if context else None)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        print("\nUse 'kct explain --list' to see available rules.")
        return 1

    output = format_result(result, args.format)
    print(output)

    return 0


if __name__ == "__main__":
    sys.exit(main())
