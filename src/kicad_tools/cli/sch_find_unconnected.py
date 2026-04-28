#!/usr/bin/env python3
"""
Find unconnected pins and potential issues in a KiCad schematic.

Usage:
    python3 sch-find-unconnected.py <schematic.kicad_sch> [options]

Options:
    --format {table,json}      Output format (default: table)
    --filter <pattern>         Filter by symbol reference (e.g., "U*")
    --include-power            Include power symbols in analysis
    --include-dnp              Include DNP (do not populate) symbols

Examples:
    # Find all unconnected pins
    python3 sch-find-unconnected.py amplifier.kicad_sch

    # Check only ICs
    python3 sch-find-unconnected.py amplifier.kicad_sch --filter "U*"

    # Output as JSON
    python3 sch-find-unconnected.py amplifier.kicad_sch --format json
"""

import argparse
import fnmatch
import json
import sys
from dataclasses import dataclass

from kicad_tools.cli.sch_connectivity import (
    Coord,
    build_wire_graph,
    is_pin_connected,
    to_coord,
)
from kicad_tools.schema import Schematic


@dataclass
class UnconnectedPin:
    """An unconnected pin."""

    reference: str
    pin_number: str
    pin_name: str
    pin_type: str
    symbol_value: str
    lib_id: str
    position: tuple[float, float]


@dataclass
class ConnectionIssue:
    """A potential connection issue."""

    type: str  # "floating_wire", "stacked_symbols", "missing_junction"
    description: str
    position: tuple[float, float]


def analyze_schematic(
    schematic: Schematic,
    include_power: bool = False,
    include_dnp: bool = False,
    pattern: str = None,
) -> tuple[list[UnconnectedPin], list[ConnectionIssue]]:
    """
    Analyze schematic for unconnected pins and issues.

    Uses the wire-graph BFS approach with per-pin position resolution
    from embedded lib_symbols to accurately detect unconnected pins.

    Returns tuple of (unconnected_pins, connection_issues)
    """
    unconnected = []
    issues = []

    # First pass: collect all pin coordinates for wire-graph splitting
    all_pin_coords: set[Coord] = set()
    symbol_data: list[tuple] = []  # (symbol, lib_sym, pin_positions)
    symbol_positions: dict[tuple[float, float], list[str]] = {}

    for sym in schematic.symbols:
        if sym.lib_id.startswith("power:") and not include_power:
            continue
        if sym.dnp and not include_dnp:
            continue
        if pattern and not fnmatch.fnmatch(sym.reference, pattern):
            continue

        # Track symbol position for stacking detection
        pos_key = (round(sym.position[0], 1), round(sym.position[1], 1))
        if pos_key not in symbol_positions:
            symbol_positions[pos_key] = []
        symbol_positions[pos_key].append(sym.reference)

        # Resolve library symbol for per-pin positions
        lib_sym = schematic.get_lib_symbol_resolved(sym.lib_id)
        if not lib_sym:
            # Cannot resolve pin positions -- report all pins as unconnected
            for pin in sym.pins:
                unconnected.append(
                    UnconnectedPin(
                        reference=sym.reference,
                        pin_number=pin.number,
                        pin_name="",
                        pin_type="",
                        symbol_value=sym.value,
                        lib_id=sym.lib_id,
                        position=sym.position,
                    )
                )
            continue

        pin_positions = lib_sym.get_all_pin_positions(
            instance_pos=sym.position,
            instance_rot=sym.rotation,
            mirror=sym.mirror,
        )

        for pos in pin_positions.values():
            all_pin_coords.add(to_coord(*pos))

        symbol_data.append((sym, lib_sym, pin_positions))

    # Build wire graph with pin coordinates as split points
    adjacency, _net_names = build_wire_graph(schematic, extra_points=all_pin_coords)

    # Second pass: check each pin for connectivity
    for sym, lib_sym, pin_positions in symbol_data:
        for lib_pin in lib_sym.pins:
            if lib_pin.number not in pin_positions:
                continue

            pos = pin_positions[lib_pin.number]
            coord = to_coord(*pos)

            if not is_pin_connected(coord, adjacency):
                unconnected.append(
                    UnconnectedPin(
                        reference=sym.reference,
                        pin_number=lib_pin.number,
                        pin_name=lib_pin.name,
                        pin_type=lib_pin.type,
                        symbol_value=sym.value,
                        lib_id=sym.lib_id,
                        position=pos,
                    )
                )

    # Check for stacked symbols (potential issues)
    for pos, refs in symbol_positions.items():
        if len(refs) > 1:
            issues.append(
                ConnectionIssue(
                    type="stacked_symbols",
                    description=f"Multiple symbols at same position: {', '.join(refs)}",
                    position=pos,
                )
            )

    # Check for floating wire ends
    for wire in schematic.wires:
        for point in [wire.start, wire.end]:
            key = (round(point[0], 1), round(point[1], 1))
            connection_count = sum(
                [
                    len(
                        [
                            w
                            for w in schematic.wires
                            if (round(w.start[0], 1), round(w.start[1], 1)) == key
                            or (round(w.end[0], 1), round(w.end[1], 1)) == key
                        ]
                    ),
                ]
            )
            junction_positions = {
                (round(j.position[0], 1), round(j.position[1], 1))
                for j in schematic.junctions
            }
            label_positions = set()
            for lbl in schematic.labels:
                label_positions.add((round(lbl.position[0], 1), round(lbl.position[1], 1)))
            for lbl in schematic.global_labels:
                label_positions.add((round(lbl.position[0], 1), round(lbl.position[1], 1)))
            for lbl in schematic.hierarchical_labels:
                label_positions.add((round(lbl.position[0], 1), round(lbl.position[1], 1)))

            if (
                connection_count == 1
                and key not in junction_positions
                and key not in label_positions
            ):
                issues.append(
                    ConnectionIssue(
                        type="possible_floating_wire",
                        description="Wire endpoint may be floating",
                        position=point,
                    )
                )

    return unconnected, issues


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Find unconnected pins in a KiCad schematic",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("schematic", help="Path to .kicad_sch file")
    parser.add_argument(
        "--format", choices=["table", "json"], default="table", help="Output format"
    )
    parser.add_argument("--filter", dest="pattern", help="Filter by symbol reference pattern")
    parser.add_argument("--include-power", action="store_true", help="Include power symbols")
    parser.add_argument("--include-dnp", action="store_true", help="Include DNP symbols")

    args = parser.parse_args(argv)

    try:
        sch = Schematic.load(args.schematic)
    except FileNotFoundError:
        print(f"Error: File not found: {args.schematic}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Error loading schematic: {e}", file=sys.stderr)
        sys.exit(1)

    unconnected, issues = analyze_schematic(
        sch,
        include_power=args.include_power,
        include_dnp=args.include_dnp,
        pattern=args.pattern,
    )

    if args.format == "json":
        output_json(unconnected, issues)
    else:
        output_table(unconnected, issues)


def output_table(unconnected: list[UnconnectedPin], issues: list[ConnectionIssue]):
    """Output as formatted table."""
    by_symbol: dict[str, list[UnconnectedPin]] = {}
    for pin in unconnected:
        if pin.reference not in by_symbol:
            by_symbol[pin.reference] = []
        by_symbol[pin.reference].append(pin)

    if by_symbol:
        print("Unconnected Pins")
        print("=" * 60)
        print(f"{'Reference':<10}  {'Value':<15}  {'Pins':<30}")
        print("-" * 60)

        for ref in sorted(by_symbol.keys()):
            pins = by_symbol[ref]
            value = pins[0].symbol_value
            pin_nums = sorted(p.pin_number for p in pins)
            pin_str = ", ".join(pin_nums[:10])
            if len(pin_nums) > 10:
                pin_str += f" +{len(pin_nums) - 10} more"
            print(f"{ref:<10}  {value:<15}  {pin_str:<30}")

        total_pins = sum(len(pins) for pins in by_symbol.values())
        print(f"\nTotal: {len(by_symbol)} symbols, {total_pins} pins")
    else:
        print("All pins connected!")

    if issues:
        print("\nPotential Issues")
        print("=" * 60)
        for issue in issues:
            print(f"  [{issue.type}] {issue.description}")
            print(f"   Position: ({issue.position[0]:.1f}, {issue.position[1]:.1f})")


def output_json(unconnected: list[UnconnectedPin], issues: list[ConnectionIssue]):
    """Output as JSON."""
    data = {
        "unconnected_pins": [
            {
                "reference": p.reference,
                "pin_number": p.pin_number,
                "pin_name": p.pin_name,
                "pin_type": p.pin_type,
                "symbol_value": p.symbol_value,
                "lib_id": p.lib_id,
                "position": list(p.position),
            }
            for p in unconnected
        ],
        "issues": [
            {
                "type": i.type,
                "description": i.description,
                "position": list(i.position),
            }
            for i in issues
        ],
        "summary": {
            "unconnected_pin_count": len(unconnected),
            "issue_count": len(issues),
            "symbols_with_issues": len({p.reference for p in unconnected}),
        },
    }
    print(json.dumps(data, indent=2))


if __name__ == "__main__":
    main()
