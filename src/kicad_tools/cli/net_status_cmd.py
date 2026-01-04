"""Net connectivity status CLI command.

Report net connectivity status showing which nets are complete, incomplete,
or unrouted, with details on what's missing.

Usage:
    kicad-net-status board.kicad_pcb
    kicad-net-status board.kicad_pcb --incomplete
    kicad-net-status board.kicad_pcb --net GND
    kicad-net-status board.kicad_pcb --by-class
    kicad-net-status board.kicad_pcb --format json

Exit Codes:
    0 - No incomplete nets
    1 - Error loading file or other error
    2 - Incomplete nets found
"""

import argparse
import json
import sys
from pathlib import Path

from kicad_tools.analysis.net_status import NetStatus, NetStatusAnalyzer, NetStatusResult


def main(argv: list[str] | None = None) -> int:
    """Main entry point for kicad-net-status command."""
    parser = argparse.ArgumentParser(
        prog="kicad-net-status",
        description="Report net connectivity status for a PCB",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("pcb", help="Path to .kicad_pcb file")
    parser.add_argument(
        "--format",
        choices=["text", "json"],
        default="text",
        help="Output format (default: text)",
    )
    parser.add_argument(
        "--incomplete",
        action="store_true",
        help="Show only incomplete nets",
    )
    parser.add_argument(
        "--net",
        help="Show status for a specific net by name",
    )
    parser.add_argument(
        "--by-class",
        action="store_true",
        dest="by_class",
        help="Group output by net class",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Show all pads with coordinates",
    )

    args = parser.parse_args(argv)

    # Validate PCB path
    pcb_path = Path(args.pcb)
    if not pcb_path.exists():
        print(f"Error: PCB not found: {pcb_path}", file=sys.stderr)
        return 1

    # Run analysis
    try:
        analyzer = NetStatusAnalyzer(pcb_path)
        result = analyzer.analyze()
    except Exception as e:
        print(f"Error during analysis: {e}", file=sys.stderr)
        return 1

    # Filter results if needed
    if args.net:
        net_status = result.get_net(args.net)
        if not net_status:
            print(f"Error: Net '{args.net}' not found", file=sys.stderr)
            print("\nAvailable nets:", file=sys.stderr)
            for net in sorted(result.nets, key=lambda n: n.net_name)[:20]:
                print(f"  {net.net_name}", file=sys.stderr)
            if len(result.nets) > 20:
                print(f"  ... and {len(result.nets) - 20} more", file=sys.stderr)
            return 1
        # Create a result with just this net
        filtered = NetStatusResult(nets=[net_status], total_nets=1)
    elif args.incomplete:
        filtered = NetStatusResult(
            nets=result.incomplete + result.unrouted,
            total_nets=result.total_nets,
        )
    else:
        filtered = result

    # Output
    if args.format == "json":
        output_json(filtered, pcb_path, args.by_class)
    else:
        output_text(filtered, pcb_path, args.verbose, args.by_class, args.incomplete)

    # Exit code
    if result.incomplete_count > 0 or result.unrouted_count > 0:
        return 2
    return 0


def output_text(
    result: NetStatusResult,
    pcb_path: Path,
    verbose: bool = False,
    by_class: bool = False,
    incomplete_only: bool = False,
) -> None:
    """Output results as formatted text."""
    print(f"Net Status: {pcb_path.name}")
    print("=" * 60)
    print()
    print(f"Summary: {result.total_nets} nets total")
    print(f"  Complete:   {result.complete_count} (100% connected)")
    print(f"  Incomplete: {result.incomplete_count} (partially connected)")
    print(f"  Unrouted:   {result.unrouted_count} (0% connected)")
    print()

    if by_class:
        _output_by_class(result, verbose)
    else:
        _output_flat(result, verbose, incomplete_only)


def _output_flat(
    result: NetStatusResult,
    verbose: bool,
    incomplete_only: bool,
) -> None:
    """Output nets in flat list format."""
    # Show incomplete nets with details
    if result.incomplete or result.unrouted:
        print("Incomplete Nets:")
        print("=" * 60)

        for net in result.incomplete + result.unrouted:
            _print_net_status(net, verbose)

    elif not incomplete_only:
        print("All nets are fully connected!")

    # Show complete nets if not filtering
    if not incomplete_only and result.complete:
        print()
        print("Complete Nets:")
        print("-" * 60)
        for net in result.complete[:20]:
            print(f"  {net.net_name} ({net.total_pads} pads)")
        if len(result.complete) > 20:
            print(f"  ... and {len(result.complete) - 20} more")


def _output_by_class(result: NetStatusResult, verbose: bool) -> None:
    """Output nets grouped by net class."""
    by_class = result.by_net_class()

    for class_name, nets in sorted(by_class.items()):
        incomplete = [n for n in nets if n.status != "complete"]
        complete = [n for n in nets if n.status == "complete"]

        print(f"\nNet Class: {class_name}")
        print("-" * 60)
        print(f"  Complete: {len(complete)}, Incomplete: {len(incomplete)}")

        if incomplete:
            print()
            for net in incomplete:
                _print_net_status(net, verbose, indent="  ")


def _print_net_status(net: NetStatus, verbose: bool, indent: str = "") -> None:
    """Print status for a single net."""
    # Status indicator
    if net.status == "complete":
        status_char = "+"
    elif net.status == "incomplete":
        status_char = "!"
    else:  # unrouted
        status_char = "X"

    # Net type indicator
    type_info = ""
    if net.is_plane_net:
        type_info = f" -- Plane net on {net.plane_layer}"

    print()
    print(
        f"{indent}[{status_char}] {net.net_name} ({net.unconnected_count} pads unconnected){type_info}"
    )

    # Show unconnected pads
    pads_to_show = net.unconnected_pads[:5] if not verbose else net.unconnected_pads
    for pad in pads_to_show:
        fix_hint = "needs via to plane" if net.is_plane_net else "needs routing"
        print(
            f"{indent}  {pad.full_name:<8} @ ({pad.position[0]:.2f}, {pad.position[1]:.2f}) -- {fix_hint}"
        )

    if not verbose and len(net.unconnected_pads) > 5:
        print(f"{indent}  ... ({len(net.unconnected_pads) - 5} more)")

    # Suggested fix
    print(f"{indent}  -> Fix: {net.suggested_fix}")


def output_json(
    result: NetStatusResult,
    pcb_path: Path,
    by_class: bool = False,
) -> None:
    """Output results as JSON."""
    data = {
        "pcb": str(pcb_path),
        "summary": {
            "total_nets": result.total_nets,
            "complete": result.complete_count,
            "incomplete": result.incomplete_count,
            "unrouted": result.unrouted_count,
            "total_unconnected_pads": result.total_unconnected_pads,
        },
    }

    if by_class:
        data["by_class"] = {
            class_name: [n.to_dict() for n in nets]
            for class_name, nets in result.by_net_class().items()
        }
    else:
        data["nets"] = [n.to_dict() for n in result.nets]

    print(json.dumps(data, indent=2))


if __name__ == "__main__":
    sys.exit(main())
