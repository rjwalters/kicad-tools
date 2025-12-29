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
from typing import Dict, List, Set, Tuple

from kicad_tools.schema import Schematic

POINT_TOLERANCE = 0.5  # mm - slightly larger for pin matching


@dataclass
class UnconnectedPin:
    """An unconnected pin."""

    reference: str
    pin_number: str
    symbol_value: str
    lib_id: str
    position: Tuple[float, float]


@dataclass
class ConnectionIssue:
    """A potential connection issue."""

    type: str  # "floating_wire", "stacked_symbols", "missing_junction"
    description: str
    position: Tuple[float, float]


def find_wire_endpoints(schematic: Schematic) -> Set[Tuple[float, float]]:
    """Get all wire endpoints as a set of rounded positions."""
    endpoints = set()
    for wire in schematic.wires:
        endpoints.add((round(wire.start[0], 1), round(wire.start[1], 1)))
        endpoints.add((round(wire.end[0], 1), round(wire.end[1], 1)))
    return endpoints


def find_junction_positions(schematic: Schematic) -> Set[Tuple[float, float]]:
    """Get all junction positions."""
    return {(round(j.position[0], 1), round(j.position[1], 1)) for j in schematic.junctions}


def find_label_positions(schematic: Schematic) -> Set[Tuple[float, float]]:
    """Get all label positions."""
    positions = set()
    for lbl in schematic.labels:
        positions.add((round(lbl.position[0], 1), round(lbl.position[1], 1)))
    for lbl in schematic.global_labels:
        positions.add((round(lbl.position[0], 1), round(lbl.position[1], 1)))
    for lbl in schematic.hierarchical_labels:
        positions.add((round(lbl.position[0], 1), round(lbl.position[1], 1)))
    return positions


def point_has_connection(
    point: Tuple[float, float],
    wire_endpoints: Set[Tuple[float, float]],
    junction_positions: Set[Tuple[float, float]],
    label_positions: Set[Tuple[float, float]],
) -> bool:
    """Check if a point has any electrical connection."""
    key = (round(point[0], 1), round(point[1], 1))
    return key in wire_endpoints or key in junction_positions or key in label_positions


def analyze_schematic(
    schematic: Schematic,
    include_power: bool = False,
    include_dnp: bool = False,
    pattern: str = None,
) -> Tuple[List[UnconnectedPin], List[ConnectionIssue]]:
    """
    Analyze schematic for unconnected pins and issues.

    Returns tuple of (unconnected_pins, connection_issues)
    """
    unconnected = []
    issues = []

    wire_endpoints = find_wire_endpoints(schematic)
    junction_positions = find_junction_positions(schematic)
    label_positions = find_label_positions(schematic)

    # Combine all connection points (reserved for future proximity checks)
    _all_connections = wire_endpoints | junction_positions | label_positions  # noqa: F841

    # Check each symbol
    symbol_positions: Dict[Tuple[float, float], List[str]] = {}

    for sym in schematic.symbols:
        # Skip power symbols unless requested
        if sym.lib_id.startswith("power:") and not include_power:
            continue

        # Skip DNP symbols unless requested
        if sym.dnp and not include_dnp:
            continue

        # Apply reference filter
        if pattern and not fnmatch.fnmatch(sym.reference, pattern):
            continue

        # Track symbol position for stacking detection
        pos_key = (round(sym.position[0], 1), round(sym.position[1], 1))
        if pos_key not in symbol_positions:
            symbol_positions[pos_key] = []
        symbol_positions[pos_key].append(sym.reference)

        # Check symbol position (simplified - assumes pins are at symbol center)
        # A more complete implementation would parse the symbol library for exact pin positions
        if not point_has_connection(
            sym.position, wire_endpoints, junction_positions, label_positions
        ):
            # Symbol center not connected - check if this is expected
            # For multi-pin symbols, we'd need library info to know exact pin positions
            for pin in sym.pins:
                unconnected.append(
                    UnconnectedPin(
                        reference=sym.reference,
                        pin_number=pin.number,
                        symbol_value=sym.value,
                        lib_id=sym.lib_id,
                        position=sym.position,
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
            # Count connections at this point
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
            # If only one wire touches this point and no junction/label, it might be floating
            if (
                connection_count == 1
                and key not in junction_positions
                and key not in label_positions
            ):
                # Check if it connects to a symbol (would need library info for exact check)
                issues.append(
                    ConnectionIssue(
                        type="possible_floating_wire",
                        description="Wire endpoint may be floating",
                        position=point,
                    )
                )

    return unconnected, issues


def main():
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

    args = parser.parse_args()

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


def output_table(unconnected: List[UnconnectedPin], issues: List[ConnectionIssue]):
    """Output as formatted table."""
    # Group unconnected pins by symbol
    by_symbol: Dict[str, List[UnconnectedPin]] = {}
    for pin in unconnected:
        if pin.reference not in by_symbol:
            by_symbol[pin.reference] = []
        by_symbol[pin.reference].append(pin)

    if by_symbol:
        print("Potentially Unconnected Pins")
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
        print("✓ No unconnected pins found")

    if issues:
        print("\nPotential Issues")
        print("=" * 60)
        for issue in issues:
            print(f"⚠️  [{issue.type}] {issue.description}")
            print(f"   Position: ({issue.position[0]:.1f}, {issue.position[1]:.1f})")

    print("\n⚠️  Note: This analysis is approximate. Full pin position checking")
    print("   requires parsing symbol libraries. Run KiCad's ERC for complete check.")


def output_json(unconnected: List[UnconnectedPin], issues: List[ConnectionIssue]):
    """Output as JSON."""
    data = {
        "unconnected_pins": [
            {
                "reference": p.reference,
                "pin_number": p.pin_number,
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
            "symbols_with_issues": len(set(p.reference for p in unconnected)),
        },
    }
    print(json.dumps(data, indent=2))


if __name__ == "__main__":
    main()
