#!/usr/bin/env python3
"""
Reconnect a component pin from one net to another.

Atomically disconnects the existing net connection (wires and orphaned
power symbols/labels) at a pin and places a new connection to the
target net.

Usage:
    kct sch reconnect-pin board.kicad_sch --ref C41 --pin 2 --to-net GNDD
    kct sch reconnect-pin board.kicad_sch --ref C41 --pin 2 --to-net SIG_GND --dry-run
    kct sch reconnect-pin board.kicad_sch --ref C41 --pin 2 --to-net GNDD --backup

Options:
    --ref <reference>      Symbol reference (e.g., C41)
    --pin <pin>            Pin number to reconnect
    --to-net <net>         Target net name (e.g., GNDD, SIG_GND)
    --lib-path <path>      Path to search for symbol libraries (can be repeated)
    --lib <file>           Specific library file to load (can be repeated)
    --dry-run              Show what would change without modifying
    --backup               Create backup before modifying
"""

from __future__ import annotations

import argparse
import shutil
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from kicad_tools.exceptions import FileNotFoundError as KiCadFileNotFoundError
from kicad_tools.schema import LibraryManager, Schematic
from kicad_tools.schema.instances import build_instance_path, find_project_name
from kicad_tools.schema.library import LibrarySymbol
from kicad_tools.sexp import SExp

from .sch_disconnect import (
    POINT_TOLERANCE,
    _find_wires_at_point,
    resolve_pin_position,
)

# Standard KiCad grid spacing
GRID = 2.54


@dataclass
class PlannedAction:
    """An action the command will take, for reporting."""

    kind: str  # "remove_wire", "remove_symbol", "remove_label", "add_power", "add_label", "add_wire", "embed", "no_op"
    description: str


@dataclass
class StubTrace:
    """Result of tracing a connection stub from a pin position."""

    wires: list[SExp] = field(default_factory=list)
    power_symbols: list[SExp] = field(default_factory=list)
    labels: list[SExp] = field(default_factory=list)
    global_labels: list[SExp] = field(default_factory=list)
    is_stub: bool = True  # True if no other pins connect to these wires
    far_end: tuple[float, float] | None = None  # endpoint opposite the pin


def _snap(value: float, grid: float = 1.27) -> float:
    """Snap a coordinate to the nearest grid point."""
    return round(value / grid) * grid


def _points_match(
    a: tuple[float, float], b: tuple[float, float], tolerance: float = POINT_TOLERANCE
) -> bool:
    """Check whether two points are within tolerance."""
    return abs(a[0] - b[0]) <= tolerance and abs(a[1] - b[1]) <= tolerance


def _wire_endpoints(wire_sexp: SExp) -> list[tuple[float, float]]:
    """Extract the two endpoints of a wire S-expression."""
    pts_node = wire_sexp.find("pts")
    if not pts_node:
        return []
    endpoints = []
    for xy in pts_node.find_all("xy"):
        x = xy.get_float(0) or 0.0
        y = xy.get_float(1) or 0.0
        endpoints.append((x, y))
    return endpoints


def _other_endpoint(
    wire_sexp: SExp, point: tuple[float, float]
) -> tuple[float, float] | None:
    """Return the wire endpoint that is NOT at *point*."""
    eps = _wire_endpoints(wire_sexp)
    if len(eps) < 2:
        return None
    if _points_match(eps[0], point):
        return eps[1]
    if _points_match(eps[1], point):
        return eps[0]
    return None


def _find_power_symbols_at_point(
    schematic: Schematic,
    point: tuple[float, float],
    tolerance: float = POINT_TOLERANCE,
) -> list[SExp]:
    """Find power symbols (lib_id starting with 'power:') placed at *point*."""
    matches = []
    for sym_sexp in schematic.sexp.find_children("symbol"):
        lid = sym_sexp.find("lib_id")
        if not lid:
            continue
        lib_id_str = lid.get_string(0) or ""
        if not lib_id_str.startswith("power:"):
            continue
        at = sym_sexp.find("at")
        if not at:
            continue
        sx = at.get_float(0) or 0.0
        sy = at.get_float(1) or 0.0
        if abs(sx - point[0]) <= tolerance and abs(sy - point[1]) <= tolerance:
            matches.append(sym_sexp)
    return matches


def _find_labels_at_point(
    schematic: Schematic,
    point: tuple[float, float],
    tolerance: float = POINT_TOLERANCE,
) -> list[SExp]:
    """Find local labels at *point*."""
    matches = []
    for lbl_sexp in schematic.sexp.find_all("label"):
        at = lbl_sexp.find("at")
        if not at:
            continue
        lx = at.get_float(0) or 0.0
        ly = at.get_float(1) or 0.0
        if abs(lx - point[0]) <= tolerance and abs(ly - point[1]) <= tolerance:
            matches.append(lbl_sexp)
    return matches


def _find_global_labels_at_point(
    schematic: Schematic,
    point: tuple[float, float],
    tolerance: float = POINT_TOLERANCE,
) -> list[SExp]:
    """Find global labels at *point*."""
    matches = []
    for lbl_sexp in schematic.sexp.find_all("global_label"):
        at = lbl_sexp.find("at")
        if not at:
            continue
        lx = at.get_float(0) or 0.0
        ly = at.get_float(1) or 0.0
        if abs(lx - point[0]) <= tolerance and abs(ly - point[1]) <= tolerance:
            matches.append(lbl_sexp)
    return matches


def _count_pins_at_point(
    schematic: Schematic,
    lib_manager: LibraryManager | None,
    point: tuple[float, float],
    exclude_ref: str,
    tolerance: float = POINT_TOLERANCE,
) -> int:
    """Count component pins (not power symbols) touching *point*, excluding *exclude_ref*."""
    count = 0
    for sym in schematic.symbols:
        if sym.reference == exclude_ref:
            continue
        if sym.lib_id.startswith("power:"):
            continue
        # Quick bounding-box reject
        if abs(sym.position[0] - point[0]) > 50 or abs(sym.position[1] - point[1]) > 50:
            continue
        # We'd need lib_manager to resolve pin positions accurately, but for
        # a simpler heuristic we check if any wire endpoints connect other
        # components.  For now, skip expensive resolution -- the caller checks
        # the stub heuristic via wire branch counting instead.
    return count


def trace_stub(
    schematic: Schematic,
    pin_pos: tuple[float, float],
    exclude_ref: str = "",
) -> StubTrace:
    """Trace the connection stub from *pin_pos* outward.

    Follows wires from the pin position, collecting:
    - wire segments in the chain
    - power symbols / labels at the far end
    Returns a StubTrace describing the connection.

    A connection is considered a "stub" (removable) when the wires only
    connect to the pin and a naming element (power symbol or label) with
    no branches to other component pins.
    """
    result = StubTrace()

    visited_wires: set[int] = set()
    frontier: list[tuple[float, float]] = [pin_pos]
    visited_points: set[tuple[int, int]] = set()
    far_points: list[tuple[float, float]] = []

    while frontier:
        pt = frontier.pop()
        pt_key = (int(pt[0] * 100), int(pt[1] * 100))
        if pt_key in visited_points:
            continue
        visited_points.add(pt_key)

        wires_here = _find_wires_at_point(schematic, pt)
        for w in wires_here:
            wid = id(w)
            if wid in visited_wires:
                continue
            visited_wires.add(wid)
            result.wires.append(w)
            other = _other_endpoint(w, pt)
            if other is not None:
                frontier.append(other)

        # Check for power symbols / labels at this point
        pwr = _find_power_symbols_at_point(schematic, pt)
        result.power_symbols.extend(pwr)

        labels = _find_labels_at_point(schematic, pt)
        result.labels.extend(labels)

        glabels = _find_global_labels_at_point(schematic, pt)
        result.global_labels.extend(glabels)

        if not _points_match(pt, pin_pos):
            far_points.append(pt)

    if far_points:
        result.far_end = far_points[-1]

    # Determine whether this is a simple stub (no branching to other pins).
    # Heuristic: a stub has at most a linear chain of wires. If any
    # intermediate point has more than 2 wires it is a branch / shared bus.
    wire_count_per_point: dict[tuple[int, int], int] = {}
    for w in result.wires:
        for ep in _wire_endpoints(w):
            key = (int(ep[0] * 100), int(ep[1] * 100))
            wire_count_per_point[key] = wire_count_per_point.get(key, 0) + 1

    pin_key = (int(pin_pos[0] * 100), int(pin_pos[1] * 100))
    for key, cnt in wire_count_per_point.items():
        if key == pin_key:
            continue
        # A far-end point touched by only 1 wire is fine (terminus).
        # A mid-point touched by exactly 2 wires is a pass-through.
        # More than 2 means a junction / branch -> shared bus.
        if cnt > 2:
            result.is_stub = False
            break

    return result


def _is_power_net(name: str, schematic: Schematic) -> bool:
    """Return True if *name* corresponds to a power net.

    A net is considered a power net when a ``power:<name>`` library
    symbol definition exists in the schematic's ``lib_symbols`` section
    or the name matches common power-symbol naming conventions.
    """
    lib_id = f"power:{name}"
    if schematic.get_lib_symbol(lib_id) is not None:
        return True
    # Heuristic: names like GND, GNDA, GNDD, VCC, +3V3, +5V, etc.
    upper = name.upper()
    if upper.startswith("GND") or upper.startswith("+") or upper.startswith("VCC") or upper.startswith("VDD"):
        return True
    return False


def _make_power_lib_sym(name: str) -> LibrarySymbol:
    """Create a minimal power symbol library definition for embedding."""
    from kicad_tools.schema.library import LibraryPin

    return LibrarySymbol(
        name=f"power:{name}",
        properties={
            "Reference": "#PWR",
            "Value": name,
        },
        pins=[
            LibraryPin(
                number="1",
                name=name,
                type="power_in",
                position=(0, 0),
                rotation=0,
                length=0,
            ),
        ],
    )


def _get_power_symbol_name(sym_sexp: SExp) -> str:
    """Extract the power net name from a power symbol S-expression."""
    lid = sym_sexp.find("lib_id")
    if lid:
        lib_id_str = lid.get_string(0) or ""
        if lib_id_str.startswith("power:"):
            return lib_id_str[6:]
    return ""


def _get_label_text(lbl_sexp: SExp) -> str:
    """Extract the text from a label or global_label S-expression."""
    return lbl_sexp.get_string(0) or ""


def plan_reconnect(
    schematic: Schematic,
    lib_manager: LibraryManager,
    reference: str,
    pin_number: str,
    to_net: str,
) -> tuple[list[PlannedAction], tuple[float, float] | None, StubTrace | None]:
    """Plan the reconnection without modifying the schematic.

    Returns (planned_actions, pin_position, stub_trace).
    """
    planned: list[PlannedAction] = []

    # 1. Resolve pin position
    pos = resolve_pin_position(schematic, lib_manager, reference, pin_number)
    if pos is None:
        return planned, None, None

    # 2. Trace existing connection
    stub = trace_stub(schematic, pos, exclude_ref=reference)

    # Determine current net name from the stub
    current_net = None
    for ps in stub.power_symbols:
        current_net = _get_power_symbol_name(ps)
        if current_net:
            break
    if current_net is None:
        for gl in stub.global_labels:
            current_net = _get_label_text(gl)
            if current_net:
                break
    if current_net is None:
        for ll in stub.labels:
            current_net = _get_label_text(ll)
            if current_net:
                break

    # Check if already connected to target
    if current_net == to_net:
        planned.append(PlannedAction("no_op", f"Pin already connected to {to_net}"))
        return planned, pos, stub

    # 3. Plan removal of old connection
    if stub.is_stub and stub.wires:
        for _w in stub.wires:
            planned.append(PlannedAction("remove_wire", "Remove stub wire segment"))
        for ps in stub.power_symbols:
            name = _get_power_symbol_name(ps)
            planned.append(PlannedAction("remove_symbol", f"Remove orphaned power symbol {name}"))
        for gl in stub.global_labels:
            text = _get_label_text(gl)
            planned.append(PlannedAction("remove_label", f"Remove orphaned global label {text}"))
        for ll in stub.labels:
            text = _get_label_text(ll)
            planned.append(PlannedAction("remove_label", f"Remove orphaned label {text}"))
    elif not stub.is_stub:
        # Shared bus: only remove the naming element, keep wires
        for ps in stub.power_symbols:
            name = _get_power_symbol_name(ps)
            planned.append(PlannedAction("remove_symbol", f"Remove power symbol {name} (shared bus wires preserved)"))
        for gl in stub.global_labels:
            text = _get_label_text(gl)
            planned.append(PlannedAction("remove_label", f"Remove global label {text} (shared bus wires preserved)"))
        for ll in stub.labels:
            text = _get_label_text(ll)
            planned.append(PlannedAction("remove_label", f"Remove label {text} (shared bus wires preserved)"))

    # 4. Plan placement of new connection
    is_power = _is_power_net(to_net, schematic)

    # Need to embed library symbol?
    if is_power:
        lib_id = f"power:{to_net}"
        if schematic.get_lib_symbol(lib_id) is None:
            planned.append(PlannedAction("embed", f"Embed library definition for {lib_id}"))
        # Place position: reuse far_end if we removed a stub, else offset from pin
        if stub.is_stub and stub.far_end:
            place_pos = stub.far_end
        else:
            place_pos = (pos[0], _snap(pos[1] + GRID))
        planned.append(PlannedAction("add_power", f"Place power symbol {to_net} at ({place_pos[0]:.2f}, {place_pos[1]:.2f})"))
        # Wire from pin to power symbol if not at same position
        if not _points_match(pos, place_pos):
            if stub.is_stub and stub.wires:
                planned.append(PlannedAction("add_wire", f"Wire from pin to {to_net} at ({place_pos[0]:.2f}, {place_pos[1]:.2f})"))
            elif not stub.wires:
                planned.append(PlannedAction("add_wire", f"Wire from pin to {to_net} at ({place_pos[0]:.2f}, {place_pos[1]:.2f})"))
    else:
        # Signal net -> place global label
        if stub.is_stub and stub.far_end:
            place_pos = stub.far_end
        elif stub.wires:
            # Non-stub with wires: place label at pin position
            place_pos = pos
        else:
            place_pos = pos
        planned.append(PlannedAction("add_label", f"Place global label {to_net} at ({place_pos[0]:.2f}, {place_pos[1]:.2f})"))
        if not _points_match(pos, place_pos) and stub.is_stub:
            planned.append(PlannedAction("add_wire", f"Wire from pin to label at ({place_pos[0]:.2f}, {place_pos[1]:.2f})"))

    return planned, pos, stub


def execute_reconnect(
    schematic: Schematic,
    pin_pos: tuple[float, float],
    stub: StubTrace,
    to_net: str,
    project_name: str = "",
    instance_path: str = "",
) -> None:
    """Execute the reconnection, modifying the schematic in place."""

    current_net = None
    for ps in stub.power_symbols:
        current_net = _get_power_symbol_name(ps)
        if current_net:
            break
    if current_net == to_net:
        return  # no-op

    is_power = _is_power_net(to_net, schematic)

    # Determine placement position before removal
    if stub.is_stub and stub.far_end:
        place_pos = stub.far_end
    elif not stub.wires:
        if is_power:
            place_pos = (pin_pos[0], _snap(pin_pos[1] + GRID))
        else:
            place_pos = pin_pos
    else:
        if is_power:
            place_pos = (pin_pos[0], _snap(pin_pos[1] + GRID))
        else:
            place_pos = pin_pos

    # Remove old connection elements
    if stub.is_stub:
        for w in stub.wires:
            schematic.sexp.remove(w)
        for ps in stub.power_symbols:
            schematic.sexp.remove(ps)
        for gl in stub.global_labels:
            schematic.sexp.remove(gl)
        for ll in stub.labels:
            schematic.sexp.remove(ll)
    else:
        # Shared bus: only remove naming elements
        for ps in stub.power_symbols:
            schematic.sexp.remove(ps)
        for gl in stub.global_labels:
            schematic.sexp.remove(gl)
        for ll in stub.labels:
            schematic.sexp.remove(ll)

    # Place new connection
    if is_power:
        lib_id = f"power:{to_net}"
        if schematic.get_lib_symbol(lib_id) is None:
            lib_sym = _make_power_lib_sym(to_net)
            schematic.embed_lib_symbol(lib_sym)

        schematic.add_power(
            to_net,
            place_pos,
            project_name=project_name,
            instance_path=instance_path,
        )
    else:
        schematic.add_global_label(to_net, place_pos)

    # Add wire from pin to new element if needed
    need_wire = not _points_match(pin_pos, place_pos)
    if stub.is_stub and need_wire:
        schematic.add_wire(pin_pos, place_pos)
    elif not stub.wires and need_wire:
        # Pin had no existing connection -- add wire to new element
        schematic.add_wire(pin_pos, place_pos)

    schematic.invalidate_cache()


def run_reconnect_pin(args) -> int:
    """Execute the reconnect-pin command."""
    schematic_path = Path(args.schematic)

    if not args.ref or not args.pin or not args.to_net:
        print("Error: --ref, --pin, and --to-net are required", file=sys.stderr)
        return 1

    try:
        sch = Schematic.load(schematic_path)
    except (FileNotFoundError, KiCadFileNotFoundError):
        print(f"Error: Schematic not found: {schematic_path}", file=sys.stderr)
        return 1

    # Set up library manager
    lib_manager = LibraryManager()
    if args.lib_paths:
        for lp in args.lib_paths:
            lib_manager.add_search_path(lp)
    if args.libs:
        for lib_file in args.libs:
            lib_manager.load_library(lib_file)
    lib_manager.load_embedded(sch)

    # Plan
    planned, pos, stub = plan_reconnect(sch, lib_manager, args.ref, args.pin, args.to_net)

    if pos is None:
        print(
            f"Error: Could not resolve position for {args.ref} pin {args.pin}",
            file=sys.stderr,
        )
        return 1

    # Report
    if args.dry_run:
        print("DRY RUN - No changes will be made")
    print("=" * 60)
    print(f"Pin: {args.ref} pin {args.pin} at ({pos[0]:.2f}, {pos[1]:.2f})")
    print(f"Target net: {args.to_net}")
    print(f"Planned actions ({len(planned)}):")
    for action in planned:
        print(f"  [{action.kind}] {action.description}")

    if args.dry_run:
        return 0

    # Check for no-op
    if len(planned) == 1 and planned[0].kind == "no_op":
        print(planned[0].description)
        return 0

    # Create backup if requested
    if args.backup:
        backup_path = f"{schematic_path}.backup-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
        shutil.copy2(schematic_path, backup_path)
        print(f"Backup created: {backup_path}")

    # Derive project info for instances block
    project_name = find_project_name(schematic_path)
    sch_uuid = sch.uuid or ""
    instance_path = build_instance_path(schematic_path, sch_uuid) if sch_uuid else ""

    # Execute
    assert stub is not None
    execute_reconnect(sch, pos, stub, args.to_net, project_name, instance_path)

    sch.save()

    print(f"\nReconnected {args.ref} pin {args.pin} to {args.to_net}")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Reconnect a component pin from one net to another",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("schematic", help="Path to .kicad_sch file")
    parser.add_argument("--ref", required=True, help="Symbol reference (e.g., C41)")
    parser.add_argument("--pin", required=True, help="Pin number to reconnect")
    parser.add_argument("--to-net", required=True, help="Target net name (e.g., GNDD)")
    parser.add_argument(
        "--lib-path", action="append", dest="lib_paths", help="Library search path"
    )
    parser.add_argument("--lib", action="append", dest="libs", help="Specific library file")
    parser.add_argument(
        "--dry-run", "-n", action="store_true", help="Preview without modifying"
    )
    parser.add_argument(
        "--backup", action="store_true", help="Create backup before modifying"
    )

    args = parser.parse_args(argv)
    return run_reconnect_pin(args)


if __name__ == "__main__":
    sys.exit(main())
