"""
CLI command for querying design decisions.

Provides command-line access to the decision tracking system:

    kct decisions show board.kicad_pcb
    kct decisions show board.kicad_pcb --component U1
    kct decisions show board.kicad_pcb --net USB_D+
    kct decisions show board.kicad_pcb --action place
    kct decisions list board.kicad_pcb
"""

import argparse
import json
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    """Main entry point for decisions command."""
    parser = argparse.ArgumentParser(
        prog="kicad-tools decisions",
        description="Query and display design decisions",
    )

    subparsers = parser.add_subparsers(dest="command", help="Command to run")

    # Show command
    show_parser = subparsers.add_parser("show", help="Show decisions for a PCB")
    show_parser.add_argument(
        "pcb",
        help="Path to PCB file (will look for .decisions.json alongside it)",
    )
    show_parser.add_argument(
        "--component",
        "-c",
        help="Filter by component reference (e.g., U1)",
    )
    show_parser.add_argument(
        "--net",
        "-n",
        help="Filter by net name (e.g., USB_D+)",
    )
    show_parser.add_argument(
        "--action",
        "-a",
        choices=["place", "route", "move", "reroute", "delete"],
        help="Filter by action type",
    )
    show_parser.add_argument(
        "--format",
        "-f",
        choices=["text", "json", "tree"],
        default="text",
        help="Output format (default: text)",
    )
    show_parser.add_argument(
        "--limit",
        "-l",
        type=int,
        default=20,
        help="Maximum number of decisions to show (default: 20)",
    )

    # List command
    list_parser = subparsers.add_parser("list", help="List all decisions summary")
    list_parser.add_argument(
        "pcb",
        help="Path to PCB file",
    )
    list_parser.add_argument(
        "--format",
        "-f",
        choices=["text", "json"],
        default="text",
        help="Output format (default: text)",
    )

    # Explain placement command
    explain_place_parser = subparsers.add_parser(
        "explain-placement", help="Explain why a component is placed where it is"
    )
    explain_place_parser.add_argument(
        "pcb",
        help="Path to PCB file",
    )
    explain_place_parser.add_argument(
        "component",
        help="Component reference (e.g., U1)",
    )
    explain_place_parser.add_argument(
        "--format",
        "-f",
        choices=["text", "json"],
        default="text",
        help="Output format (default: text)",
    )

    # Explain route command
    explain_route_parser = subparsers.add_parser(
        "explain-route", help="Explain why a net was routed the way it was"
    )
    explain_route_parser.add_argument(
        "pcb",
        help="Path to PCB file",
    )
    explain_route_parser.add_argument(
        "net",
        help="Net name (e.g., USB_D+)",
    )
    explain_route_parser.add_argument(
        "--format",
        "-f",
        choices=["text", "json"],
        default="text",
        help="Output format (default: text)",
    )

    args = parser.parse_args(argv)

    if args.command == "show":
        return _show_decisions(args)
    elif args.command == "list":
        return _list_decisions(args)
    elif args.command == "explain-placement":
        return _explain_placement(args)
    elif args.command == "explain-route":
        return _explain_route(args)
    else:
        parser.print_help()
        return 0


def _show_decisions(args) -> int:
    """Show decisions matching the specified filters."""
    from kicad_tools.explain.decisions import DecisionStore, get_decisions_path

    pcb_path = Path(args.pcb)
    if not pcb_path.exists():
        print(f"Error: PCB file not found: {args.pcb}", file=sys.stderr)
        return 1

    decisions_path = get_decisions_path(pcb_path)
    if not decisions_path.exists():
        print(f"No decisions file found at: {decisions_path}", file=sys.stderr)
        print("Run placement optimizer or autorouter with record_decisions=True first.")
        return 1

    store = DecisionStore.load(decisions_path)

    # Query with filters
    decisions = store.query(
        component=args.component,
        net=args.net,
        action=args.action,
    )

    # Limit results
    if args.limit and len(decisions) > args.limit:
        decisions = decisions[: args.limit]
        truncated = True
    else:
        truncated = False

    if not decisions:
        print("No decisions found matching the specified filters.")
        return 0

    if args.format == "json":
        output = [d.to_dict() for d in decisions]
        print(json.dumps(output, indent=2))
    elif args.format == "tree":
        _print_decisions_tree(decisions)
    else:
        _print_decisions_text(decisions)

    if truncated:
        print(f"\n(Showing {args.limit} of {len(store)} total decisions)")

    return 0


def _list_decisions(args) -> int:
    """List summary of all decisions."""
    from kicad_tools.explain.decisions import DecisionStore, get_decisions_path

    pcb_path = Path(args.pcb)
    if not pcb_path.exists():
        print(f"Error: PCB file not found: {args.pcb}", file=sys.stderr)
        return 1

    decisions_path = get_decisions_path(pcb_path)
    if not decisions_path.exists():
        print(f"No decisions file found at: {decisions_path}", file=sys.stderr)
        return 1

    store = DecisionStore.load(decisions_path)
    decisions = store.all()

    if not decisions:
        print("No decisions recorded.")
        return 0

    # Group by action
    by_action: dict[str, int] = {}
    by_decided_by: dict[str, int] = {}
    components: set[str] = set()
    nets: set[str] = set()

    for d in decisions:
        by_action[d.action] = by_action.get(d.action, 0) + 1
        by_decided_by[d.decided_by] = by_decided_by.get(d.decided_by, 0) + 1
        components.update(d.components)
        nets.update(d.nets)

    if args.format == "json":
        output = {
            "total_decisions": len(decisions),
            "by_action": by_action,
            "by_decided_by": by_decided_by,
            "unique_components": len(components),
            "unique_nets": len(nets),
        }
        print(json.dumps(output, indent=2))
    else:
        print("Decision Summary")
        print("=" * 40)
        print(f"Total decisions: {len(decisions)}")
        print()
        print("By action:")
        for action, count in sorted(by_action.items()):
            print(f"  {action}: {count}")
        print()
        print("By decided_by:")
        for decider, count in sorted(by_decided_by.items()):
            print(f"  {decider}: {count}")
        print()
        print(f"Unique components: {len(components)}")
        print(f"Unique nets: {len(nets)}")

    return 0


def _explain_placement(args) -> int:
    """Explain a component's placement."""
    from kicad_tools.explain.rationale import explain_placement
    from kicad_tools.schema.pcb import PCB

    pcb_path = Path(args.pcb)
    if not pcb_path.exists():
        print(f"Error: PCB file not found: {args.pcb}", file=sys.stderr)
        return 1

    try:
        pcb = PCB.load(str(pcb_path))
    except Exception as e:
        print(f"Error loading PCB: {e}", file=sys.stderr)
        return 1

    rationale = explain_placement(args.component, pcb)

    if rationale is None:
        print(f"Component {args.component} not found in PCB.")
        return 1

    if args.format == "json":
        print(json.dumps(rationale.to_dict(), indent=2))
    else:
        print(f"Component: {rationale.component}")
        print(f"Position: ({rationale.position[0]:.2f}, {rationale.position[1]:.2f})")
        print(f"Decided by: {rationale.decided_by}")
        if rationale.timestamp:
            print(f"Timestamp: {rationale.timestamp}")
        print()
        print(f"Rationale: {rationale.rationale}")
        if rationale.alternatives:
            print()
            print("Alternatives considered:")
            for alt in rationale.alternatives:
                print(f"  - {alt.description}")
                print(f"    Rejected: {alt.rejected_because}")
        if rationale.constraints:
            print()
            print(f"Constraints satisfied: {', '.join(rationale.constraints)}")

    return 0


def _explain_route(args) -> int:
    """Explain a net's routing."""
    from kicad_tools.explain.rationale import explain_route
    from kicad_tools.schema.pcb import PCB

    pcb_path = Path(args.pcb)
    if not pcb_path.exists():
        print(f"Error: PCB file not found: {args.pcb}", file=sys.stderr)
        return 1

    try:
        pcb = PCB.load(str(pcb_path))
    except Exception as e:
        print(f"Error loading PCB: {e}", file=sys.stderr)
        return 1

    rationale = explain_route(args.net, pcb)

    if rationale is None:
        print(f"No routing decision found for net: {args.net}")
        return 1

    if args.format == "json":
        print(json.dumps(rationale.to_dict(), indent=2))
    else:
        print(f"Net: {rationale.net}")
        if rationale.decided_by:
            print(f"Decided by: {rationale.decided_by}")
        if rationale.timestamp:
            print(f"Timestamp: {rationale.timestamp}")
        print()
        print(f"Rationale: {rationale.rationale}")
        if rationale.metrics:
            print()
            print("Metrics:")
            for key, value in rationale.metrics.items():
                print(f"  {key}: {value}")
        if rationale.alternatives:
            print()
            print("Alternatives considered:")
            for alt in rationale.alternatives:
                print(f"  - {alt.description}")
                print(f"    Rejected: {alt.rejected_because}")
        if rationale.constraints:
            print()
            print(f"Constraints satisfied: {', '.join(rationale.constraints)}")

    return 0


def _print_decisions_text(decisions: list) -> None:
    """Print decisions in text format."""
    print("Design Decisions")
    print("=" * 60)

    for d in decisions:
        print()
        print(f"[{d.id}] {d.action.upper()}")
        print(f"  Timestamp: {d.timestamp}")
        print(f"  Decided by: {d.decided_by}")

        if d.components:
            print(f"  Components: {', '.join(d.components)}")
        if d.nets:
            print(f"  Nets: {', '.join(d.nets)}")
        if d.position:
            print(f"  Position: ({d.position[0]:.2f}, {d.position[1]:.2f})")

        if d.rationale:
            print(f"  Rationale: {d.rationale}")

        if d.alternatives:
            print("  Alternatives:")
            for alt in d.alternatives:
                print(f"    - {alt.description}")
                print(f"      Rejected: {alt.rejected_because}")

        if d.constraints_satisfied:
            print(f"  Constraints: {', '.join(d.constraints_satisfied)}")

        if d.metrics:
            metrics_str = ", ".join(f"{k}={v}" for k, v in d.metrics.items())
            print(f"  Metrics: {metrics_str}")


def _print_decisions_tree(decisions: list) -> None:
    """Print decisions in tree format."""
    print("Design Decisions")
    print()

    for i, d in enumerate(decisions):
        is_last = i == len(decisions) - 1
        prefix = "└─" if is_last else "├─"
        child_prefix = "  " if is_last else "│ "

        print(
            f"{prefix} [{d.action}] {', '.join(d.components) if d.components else ', '.join(d.nets)}"
        )

        parts = []
        if d.decided_by:
            parts.append(f"by {d.decided_by}")
        if d.position:
            parts.append(f"at ({d.position[0]:.1f}, {d.position[1]:.1f})")

        if parts:
            print(f"{child_prefix}├─ {' '.join(parts)}")

        if d.rationale:
            # Truncate long rationale
            rationale = d.rationale[:60] + "..." if len(d.rationale) > 60 else d.rationale
            print(f"{child_prefix}└─ {rationale}")


if __name__ == "__main__":
    sys.exit(main())
