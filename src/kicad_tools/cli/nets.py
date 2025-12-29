"""
Trace and analyze nets in KiCad schematics.

Usage:
    kicad-nets <schematic.kicad_sch> [options]

Examples:
    kicad-nets clock.kicad_sch
    kicad-nets clock.kicad_sch --net MCLK_DAC
    kicad-nets clock.kicad_sch --stats
"""

import argparse
import json
import sys
from typing import List

from ..operations.net_ops import Net, find_net, trace_nets
from ..schema.schematic import Schematic


def main(argv: List[str] | None = None) -> int:
    """Main entry point for kicad-nets command."""
    parser = argparse.ArgumentParser(
        prog="kicad-nets",
        description="Trace nets in a KiCad schematic",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("schematic", help="Path to .kicad_sch file")
    parser.add_argument(
        "--format", choices=["table", "json"], default="table", help="Output format"
    )
    parser.add_argument("--net", help="Trace a specific net by label")
    parser.add_argument("--stats", action="store_true", help="Show net statistics only")

    args = parser.parse_args(argv)

    try:
        sch = Schematic.load(args.schematic)
    except FileNotFoundError:
        print(f"Error: File not found: {args.schematic}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"Error loading schematic: {e}", file=sys.stderr)
        return 1

    if args.net:
        # Trace a specific net
        net = find_net(sch, args.net)
        if not net:
            print(f"Error: Net '{args.net}' not found", file=sys.stderr)
            # Show available nets
            all_nets = trace_nets(sch)
            labeled = [n for n in all_nets if n.has_label]
            if labeled:
                print(
                    f"Available labeled nets: {', '.join(n.name for n in labeled)}",
                    file=sys.stderr,
                )
            return 1

        if args.format == "json":
            output_net_json(net)
        else:
            output_net_detail(net)
    else:
        # Trace all nets
        nets = trace_nets(sch)

        if args.stats:
            output_stats(nets)
        elif args.format == "json":
            output_all_json(nets)
        else:
            output_all_table(nets)

    return 0


def output_net_detail(net: Net) -> None:
    """Output detailed info about a single net."""
    print(f"Net: {net.name}")
    print("=" * 50)
    print(f"Has label: {net.has_label}")
    print(f"Wire count: {len(net.wires)}")
    print(f"Connection count: {len(net.connections)}")

    total_length = sum(w.length for w in net.wires)
    print(f"Total wire length: {total_length:.2f} mm")

    if net.connections:
        print("\nConnections:")
        print("-" * 50)
        for conn in net.connections:
            if conn.type == "pin":
                print(f"  Pin: {conn.reference}.{conn.pin_number}")
            elif conn.type == "label":
                print(f"  Label: {conn.reference}")
            elif conn.type == "junction":
                print(f"  Junction at ({conn.point[0]:.1f}, {conn.point[1]:.1f})")
            else:
                print(f"  {conn.type}: ({conn.point[0]:.1f}, {conn.point[1]:.1f})")

    if net.wires:
        print("\nWire segments:")
        print("-" * 50)
        for i, wire in enumerate(net.wires, 1):
            print(
                f"  {i}. ({wire.start[0]:.1f}, {wire.start[1]:.1f}) -> "
                f"({wire.end[0]:.1f}, {wire.end[1]:.1f}) [{wire.length:.2f} mm]"
            )


def output_net_json(net: Net) -> None:
    """Output a single net as JSON."""
    data = {
        "name": net.name,
        "has_label": net.has_label,
        "wire_count": len(net.wires),
        "total_length": sum(w.length for w in net.wires),
        "connections": [
            {
                "type": c.type,
                "point": list(c.point),
                "reference": c.reference,
                "pin_number": c.pin_number,
            }
            for c in net.connections
        ],
        "wires": [
            {
                "start": list(w.start),
                "end": list(w.end),
                "length": w.length,
            }
            for w in net.wires
        ],
    }
    print(json.dumps(data, indent=2))


def output_all_table(nets: List[Net]) -> None:
    """Output all nets as a table."""
    if not nets:
        print("No nets found.")
        return

    # Sort: labeled nets first, then by name
    nets.sort(key=lambda n: (not n.has_label, n.name))

    print(f"{'Name':<25}  {'Label':<5}  {'Wires':<6}  {'Length':<10}  Connections")
    print("-" * 70)

    for net in nets:
        total_length = sum(w.length for w in net.wires)
        label_mark = "Y" if net.has_label else ""
        conn_types = []
        for conn in net.connections:
            if conn.type == "pin":
                conn_types.append(f"{conn.reference}.{conn.pin_number}")
            elif conn.type == "junction":
                conn_types.append("junction")

        conn_str = ", ".join(conn_types[:3])
        if len(conn_types) > 3:
            conn_str += f" +{len(conn_types) - 3} more"

        print(
            f"{net.name:<25}  {label_mark:<5}  {len(net.wires):<6}  "
            f"{total_length:>7.2f} mm  {conn_str}"
        )

    print(f"\nTotal: {len(nets)} nets")


def output_all_json(nets: List[Net]) -> None:
    """Output all nets as JSON."""
    data = []
    for net in nets:
        data.append(
            {
                "name": net.name,
                "has_label": net.has_label,
                "wire_count": len(net.wires),
                "total_length": sum(w.length for w in net.wires),
                "connection_count": len(net.connections),
            }
        )
    print(json.dumps(data, indent=2))


def output_stats(nets: List[Net]) -> None:
    """Output net statistics."""
    if not nets:
        print("No nets found.")
        return

    labeled = sum(1 for n in nets if n.has_label)
    unlabeled = len(nets) - labeled
    total_wires = sum(len(n.wires) for n in nets)
    total_length = sum(sum(w.length for w in n.wires) for n in nets)
    total_connections = sum(len(n.connections) for n in nets)

    print("Net Statistics")
    print("=" * 40)
    print(f"Total nets:         {len(nets)}")
    print(f"  Labeled:          {labeled}")
    print(f"  Unlabeled:        {unlabeled}")
    print(f"Total wires:        {total_wires}")
    print(f"Total wire length:  {total_length:.2f} mm")
    print(f"Total connections:  {total_connections}")

    if nets:
        # Find nets with most wires
        most_wires = max(nets, key=lambda n: len(n.wires))
        if most_wires.wires:
            print(f"\nLargest net:        {most_wires.name} ({len(most_wires.wires)} wires)")


if __name__ == "__main__":
    sys.exit(main())
