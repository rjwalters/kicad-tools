#!/usr/bin/env python3
"""
Find unconnected pins and potential issues in a KiCad schematic.

This command performs two complementary checks:

1. **Wire-graph analysis** -- walks the schematic's wire-graph and reports
   pins that are not connected to any wire, label, or junction.  For
   hierarchical schematics, the analysis is repeated for every sub-sheet
   discovered via the ``Sheetfile`` property.

2. **Netlist cross-check** -- exports the netlist via ``kicad-cli`` (no
   Python fallback) and reports any pin that exists in the schematic but
   is missing from the netlist.  This catches the false-negative observed
   when a symbol's ``(instances)`` block contains only ``wrong_project``
   entries: ``kicad-cli`` silently drops the symbol from the netlist but
   the wire-graph analysis still sees it.  Use
   ``--no-check-netlist-export`` to skip this cross-check.

Usage:
    python3 sch-find-unconnected.py <schematic.kicad_sch> [options]

Options:
    --format {table,json}      Output format (default: table)
    --filter <pattern>         Filter by symbol reference (e.g., "U*")
    --include-power            Include power symbols in analysis
    --include-dnp              Include DNP (do not populate) symbols
    --no-check-netlist-export  Skip the netlist-export cross-check

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
from pathlib import Path

from kicad_tools.cli.sch_connectivity import (
    Coord,
    build_wire_graph,
    is_pin_connected,
    to_coord,
)
from kicad_tools.cli.sch_set_footprint import _collect_schematic_files
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
    sheet: str = ""  # Sub-sheet file name (empty for root)


@dataclass
class ConnectionIssue:
    """A potential connection issue."""

    type: str  # "floating_wire", "stacked_symbols", "missing_junction"
    description: str
    position: tuple[float, float]
    sheet: str = ""


@dataclass
class MissingFromNetlist:
    """A pin that exists in the schematic but is missing from the netlist export.

    This indicates the symbol was silently dropped by ``kicad-cli`` --
    most commonly because its ``(instances)`` block contains only
    ``wrong_project`` entries (see ``kct sch repair-instances``).
    """

    reference: str
    pin_number: str
    pin_name: str
    pin_type: str
    symbol_value: str
    lib_id: str
    position: tuple[float, float]
    sheet: str = ""


def _analyze_single_sheet(
    schematic: Schematic,
    include_power: bool,
    include_dnp: bool,
    pattern: str | None,
    sheet_name: str,
) -> tuple[list[UnconnectedPin], list[ConnectionIssue], list[tuple]]:
    """Run wire-graph analysis on one schematic file.

    Returns:
        Tuple of (unconnected_pins, issues, symbol_data) where ``symbol_data``
        is a list of ``(sym, lib_sym, pin_positions)`` tuples for every
        symbol that survived filtering on this sheet.  The caller uses
        ``symbol_data`` to build the global schematic-side pin set for the
        netlist cross-check.
    """
    unconnected: list[UnconnectedPin] = []
    issues: list[ConnectionIssue] = []

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
                        sheet=sheet_name,
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
                        sheet=sheet_name,
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
                    sheet=sheet_name,
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
                (round(j.position[0], 1), round(j.position[1], 1)) for j in schematic.junctions
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
                        sheet=sheet_name,
                    )
                )

    return unconnected, issues, symbol_data


def _cross_check_netlist(
    root_path: Path,
    schematic_pins: list[tuple[str, str, "UnconnectedPin"]],
) -> tuple[list[MissingFromNetlist], str | None]:
    """Cross-check schematic pins against the netlist export.

    Args:
        root_path: Path to the root .kicad_sch file.
        schematic_pins: List of ``(reference, pin_number, pin_record)``
            tuples for every schematic pin that should appear in the
            netlist (after power/DNP filtering).  ``pin_record`` carries
            the metadata used to build the finding.

    Returns:
        Tuple of ``(findings, warning)``.  ``warning`` is a human-readable
        message if the cross-check could not run (kicad-cli unavailable,
        crashed, or export missing); in that case ``findings`` is empty.
    """
    # Import here to avoid forcing operations/netlist deps for callers
    # that pass ``--no-check-netlist-export``.
    try:
        from kicad_tools.operations.netlist import export_netlist
    except Exception as e:
        return [], f"netlist module unavailable: {e}"

    # Export via kicad-cli with fallback=False -- we explicitly want to
    # see what kicad-cli emits, since that is what every downstream
    # consumer (pcbnew, KiCad, exported BOMs, fabrication outputs)
    # actually sees.  The Python fallback would *hide* the bug by
    # walking the hierarchy correctly.
    try:
        netlist = export_netlist(root_path, fallback=False)
    except FileNotFoundError as e:
        return [], f"kicad-cli not available, netlist cross-check skipped: {e}"
    except RuntimeError as e:
        return [], f"kicad-cli netlist export failed, cross-check skipped: {e}"
    except Exception as e:  # pragma: no cover - defensive
        return [], f"netlist export raised unexpected error, cross-check skipped: {e}"

    # Build set of (reference, pin) tuples present in the netlist.
    netlist_pin_set: set[tuple[str, str]] = set()
    for net in netlist.nets:
        for node in net.nodes:
            netlist_pin_set.add((node.reference, node.pin))

    findings: list[MissingFromNetlist] = []
    for ref, pin_num, record in schematic_pins:
        if (ref, pin_num) not in netlist_pin_set:
            findings.append(
                MissingFromNetlist(
                    reference=ref,
                    pin_number=pin_num,
                    pin_name=record.pin_name,
                    pin_type=record.pin_type,
                    symbol_value=record.symbol_value,
                    lib_id=record.lib_id,
                    position=record.position,
                    sheet=record.sheet,
                )
            )

    return findings, None


def analyze_schematic(
    schematic: Schematic,
    include_power: bool = False,
    include_dnp: bool = False,
    pattern: str | None = None,
    *,
    root_path: Path | None = None,
    check_netlist_export: bool = True,
) -> tuple[list[UnconnectedPin], list[ConnectionIssue], list[MissingFromNetlist]]:
    """Analyze a schematic (and its hierarchy) for connectivity issues.

    Performs three checks:

    1. Wire-graph BFS on every sheet in the hierarchy (root + sub-sheets).
    2. Stacked-symbol detection per sheet.
    3. Netlist-export cross-check via ``kicad-cli`` (root only).

    Args:
        schematic: Root schematic (already loaded).
        include_power: Include ``power:`` library symbols.
        include_dnp: Include ``dnp`` symbols.
        pattern: ``fnmatch`` pattern to filter symbol references.
        root_path: Path to the root .kicad_sch file (required for sub-sheet
            traversal and netlist export).  If omitted, the hierarchy walk
            and netlist cross-check are skipped.
        check_netlist_export: Run the netlist-export cross-check.

    Returns:
        Tuple of ``(unconnected_pins, connection_issues, missing_from_netlist)``.
    """
    unconnected: list[UnconnectedPin] = []
    issues: list[ConnectionIssue] = []
    # Build the global schematic-side pin set across the whole hierarchy,
    # keyed on (reference, pin_number).  This is used for the netlist
    # cross-check at the end.
    schematic_pin_records: list[tuple[str, str, UnconnectedPin]] = []
    # De-duplicate hierarchical instances of the same (ref, pin) so we
    # don't report the same missing pin once per sheet.
    seen_pins: set[tuple[str, str]] = set()

    # Collect all sheets in the hierarchy.
    if root_path is not None:
        sheet_files = _collect_schematic_files(Path(root_path))
    else:
        sheet_files = []

    # Always include the root schematic we were given.
    sheets_to_analyze: list[tuple[Schematic, str]] = [(schematic, "")]

    # Append sub-sheets (skip the root which we already loaded).
    if sheet_files:
        root_resolved = Path(root_path).resolve()
        for sf in sheet_files:
            if sf.resolve() == root_resolved:
                continue
            try:
                sub = Schematic.load(sf)
            except Exception as e:
                # Don't fail the entire run if a sub-sheet won't parse;
                # warn and continue.
                print(
                    f"Warning: could not load sub-sheet {sf}: {e}",
                    file=sys.stderr,
                )
                continue
            sheets_to_analyze.append((sub, sf.name))

    for sub_sch, sheet_name in sheets_to_analyze:
        sub_unconn, sub_issues, sub_symbol_data = _analyze_single_sheet(
            sub_sch,
            include_power=include_power,
            include_dnp=include_dnp,
            pattern=pattern,
            sheet_name=sheet_name,
        )
        unconnected.extend(sub_unconn)
        issues.extend(sub_issues)

        # Record every schematic-side pin for the netlist cross-check.
        # We use the position/value of the first instance we see; if the
        # same (ref, pin) appears on multiple sheets (which shouldn't
        # normally happen) we keep the first record.
        for sym, lib_sym, pin_positions in sub_symbol_data:
            for lib_pin in lib_sym.pins:
                if lib_pin.number not in pin_positions:
                    continue
                key = (sym.reference, lib_pin.number)
                if key in seen_pins:
                    continue
                seen_pins.add(key)
                pos = pin_positions[lib_pin.number]
                record = UnconnectedPin(
                    reference=sym.reference,
                    pin_number=lib_pin.number,
                    pin_name=lib_pin.name,
                    pin_type=lib_pin.type,
                    symbol_value=sym.value,
                    lib_id=sym.lib_id,
                    position=pos,
                    sheet=sheet_name,
                )
                schematic_pin_records.append((sym.reference, lib_pin.number, record))

    missing_from_netlist: list[MissingFromNetlist] = []
    if check_netlist_export and root_path is not None:
        findings, warning = _cross_check_netlist(Path(root_path), schematic_pin_records)
        if warning:
            print(f"Warning: {warning}", file=sys.stderr)
        else:
            missing_from_netlist = findings

    return unconnected, issues, missing_from_netlist


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
    parser.add_argument(
        "--no-check-netlist-export",
        dest="check_netlist_export",
        action="store_false",
        default=True,
        help="Skip the netlist-export cross-check (default: enabled)",
    )

    args = parser.parse_args(argv)

    try:
        sch_path = Path(args.schematic)
        sch = Schematic.load(args.schematic)
    except FileNotFoundError:
        print(f"Error: File not found: {args.schematic}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Error loading schematic: {e}", file=sys.stderr)
        sys.exit(1)

    unconnected, issues, missing_from_netlist = analyze_schematic(
        sch,
        include_power=args.include_power,
        include_dnp=args.include_dnp,
        pattern=args.pattern,
        root_path=sch_path,
        check_netlist_export=args.check_netlist_export,
    )

    if args.format == "json":
        output_json(unconnected, issues, missing_from_netlist)
    else:
        output_table(unconnected, issues, missing_from_netlist)


def output_table(
    unconnected: list[UnconnectedPin],
    issues: list[ConnectionIssue],
    missing_from_netlist: list[MissingFromNetlist] | None = None,
):
    """Output as formatted table."""
    missing_from_netlist = missing_from_netlist or []

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

    if missing_from_netlist:
        print("\nMissing from Netlist Export")
        print("=" * 60)
        print(
            "These pins exist in the schematic but were dropped by kicad-cli during netlist export."
        )
        print(
            "Most commonly caused by wrong_project (instances) entries"
            " -- run `kct sch repair-instances` to fix."
        )
        print("-" * 60)
        by_ref: dict[str, list[MissingFromNetlist]] = {}
        for p in missing_from_netlist:
            by_ref.setdefault(p.reference, []).append(p)
        for ref in sorted(by_ref.keys()):
            pins = by_ref[ref]
            pin_nums = sorted(p.pin_number for p in pins)
            print(f"  {ref}: pin(s) {', '.join(pin_nums)}")
        print(f"\nTotal: {len(missing_from_netlist)} pin(s) missing from netlist")


def output_json(
    unconnected: list[UnconnectedPin],
    issues: list[ConnectionIssue],
    missing_from_netlist: list[MissingFromNetlist] | None = None,
):
    """Output as JSON."""
    missing_from_netlist = missing_from_netlist or []
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
                "sheet": p.sheet,
            }
            for p in unconnected
        ],
        "issues": [
            {
                "type": i.type,
                "description": i.description,
                "position": list(i.position),
                "sheet": i.sheet,
            }
            for i in issues
        ],
        "missing_from_netlist": [
            {
                "reference": p.reference,
                "pin_number": p.pin_number,
                "pin_name": p.pin_name,
                "pin_type": p.pin_type,
                "symbol_value": p.symbol_value,
                "lib_id": p.lib_id,
                "position": list(p.position),
                "sheet": p.sheet,
                "remediation": (
                    "Pin exists in schematic but is missing from netlist"
                    " export -- run `kct sch repair-instances` to fix"
                    " wrong_project (instances) entries."
                ),
            }
            for p in missing_from_netlist
        ],
        "summary": {
            "unconnected_pin_count": len(unconnected),
            "issue_count": len(issues),
            "symbols_with_issues": len({p.reference for p in unconnected}),
            "missing_from_netlist_count": len(missing_from_netlist),
        },
    }
    print(json.dumps(data, indent=2))


if __name__ == "__main__":
    main()
