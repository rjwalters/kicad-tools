#!/usr/bin/env python3
"""
Add a component symbol to a KiCad schematic file.

Supports placing regular symbols and power symbols, with optional wire
connections from pins to target coordinates.

Usage:
    kct sch add-component board.kicad_sch --lib-id "Device:R" --reference R14 \\
        --value 100k --footprint "Resistor_SMD:R_0402_1005Metric" --at 100 80
    kct sch add-component board.kicad_sch --lib-id "power:GNDD" --at 100 90
    kct sch add-component board.kicad_sch --lib-id "Device:R" --reference R14 \\
        --value 100k --footprint "Resistor_SMD:R_0402_1005Metric" --at 100 80 \\
        --connect pin1:120,80 --lib-path lib/

Options:
    --lib-id <lib_id>      Library symbol identifier (e.g., Device:R, power:GND)
    --reference <ref>      Symbol reference designator (e.g., R1, U1)
    --value <value>        Component value (e.g., 10k, 100nF)
    --footprint <fp>       Footprint name (e.g., Resistor_SMD:R_0402_1005Metric)
    --at <x> <y>           Placement position in schematic coordinates
    --rotation <degrees>   Symbol rotation in degrees (default: 0)
    --mirror <x|y>         Mirror mode (x or y)
    --connect <spec>       Connect pin to target (e.g., pin1:120,80). Repeatable.
    --lib-path <path>      Path to search for symbol libraries (repeatable)
    --lib <file>           Specific library file to load (repeatable)
    --dry-run              Show planned actions without modifying
    --backup               Create timestamped backup before writing
"""

from __future__ import annotations

import argparse
import shutil
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from kicad_tools.exceptions import FileNotFoundError as KiCadFileNotFoundError
from kicad_tools.schema import LibraryManager, Schematic
from kicad_tools.schema.instances import build_instance_path, find_project_name
from kicad_tools.schema.library import LibrarySymbol


@dataclass
class ConnectSpec:
    """Parsed --connect argument: pin number to target coordinates."""

    pin_number: str
    target: tuple[float, float]


@dataclass
class PlannedAction:
    """An action the command will take, for reporting."""

    kind: str  # "symbol", "power", "wire", "junction", "embed"
    description: str


def parse_connect(spec: str) -> ConnectSpec:
    """Parse a --connect argument like 'pin1:120,80' or '1:120,80'.

    The pin part is the pin number (with or without 'pin' prefix).
    The target part is 'x,y' coordinates.
    """
    if ":" not in spec:
        raise ValueError(
            f"Invalid --connect format: '{spec}'. Expected 'pin:x,y' (e.g., '1:120,80')"
        )

    pin_part, target_part = spec.split(":", 1)

    # Strip optional 'pin' prefix
    pin_number = pin_part
    if pin_number.lower().startswith("pin"):
        pin_number = pin_number[3:]
    pin_number = pin_number.strip()

    if not pin_number:
        raise ValueError(f"Invalid pin number in --connect: '{spec}'")

    # Parse target coordinates
    if "," not in target_part:
        raise ValueError(
            f"Invalid target coordinates in --connect: '{spec}'. Expected 'x,y'"
        )

    parts = target_part.split(",")
    if len(parts) != 2:
        raise ValueError(
            f"Invalid target coordinates in --connect: '{spec}'. Expected exactly 'x,y'"
        )

    try:
        x = float(parts[0].strip())
        y = float(parts[1].strip())
    except ValueError:
        raise ValueError(
            f"Invalid coordinate values in --connect: '{spec}'. Expected numeric x,y"
        )

    return ConnectSpec(pin_number=pin_number, target=(x, y))


def _is_power_symbol(lib_id: str) -> bool:
    """Check if lib_id denotes a power symbol."""
    return lib_id.startswith("power:")


def _snap(value: float, grid: float = 1.27) -> float:
    """Snap a coordinate to the nearest grid point."""
    return round(value / grid) * grid


def _round_pos(pos: tuple[float, float], decimals: int = 4) -> tuple[float, float]:
    """Round a position to *decimals* decimal places.

    This is a lightweight cleanup for residual floating-point noise
    after the grid-aware snap in ``get_pin_position()`` has done the
    heavy lifting.  Four decimal places (0.1 um) is well below KiCad's
    ERC tolerance while still eliminating IEEE-754 artefacts like
    ``1.2699999999999998``.
    """
    return (round(pos[0], decimals), round(pos[1], decimals))


def _point_on_wire_midpoint(
    point: tuple[float, float],
    wire_start: tuple[float, float],
    wire_end: tuple[float, float],
    tolerance: float = 0.1,
) -> bool:
    """Check if a point lies on a wire segment but not at its endpoints."""
    x, y = point

    # Check if point is at either endpoint
    for ep in (wire_start, wire_end):
        if abs(x - ep[0]) < tolerance and abs(y - ep[1]) < tolerance:
            return False

    # Check if point is on the segment
    x1, y1 = wire_start
    x2, y2 = wire_end

    # Bounding box check
    if not (min(x1, x2) - tolerance <= x <= max(x1, x2) + tolerance):
        return False
    if not (min(y1, y2) - tolerance <= y <= max(y1, y2) + tolerance):
        return False

    # Perpendicular distance check
    length = ((x2 - x1) ** 2 + (y2 - y1) ** 2) ** 0.5
    if length < tolerance:
        return False

    dist = abs((y2 - y1) * x - (x2 - x1) * y + x2 * y1 - y2 * x1) / length
    return dist < tolerance


def _point_on_wire_segment(
    point: tuple[float, float],
    wire_start: tuple[float, float],
    wire_end: tuple[float, float],
    tolerance: float = 0.1,
) -> bool:
    """Check if a point lies on a wire segment (including endpoints)."""
    x, y = point
    x1, y1 = wire_start
    x2, y2 = wire_end

    # Bounding box check
    if not (min(x1, x2) - tolerance <= x <= max(x1, x2) + tolerance):
        return False
    if not (min(y1, y2) - tolerance <= y <= max(y1, y2) + tolerance):
        return False

    # Perpendicular distance check
    length = ((x2 - x1) ** 2 + (y2 - y1) ** 2) ** 0.5
    if length < tolerance:
        # Wire is basically a point -- check distance to that point
        return ((x - x1) ** 2 + (y - y1) ** 2) ** 0.5 < tolerance

    dist = abs((y2 - y1) * x - (x2 - x1) * y + x2 * y1 - y2 * x1) / length
    return dist < tolerance


def _validate_wire_endpoints(
    sch: Schematic,
    first_new_wire_index: int,
    tolerance: float = 0.01,
    correction_radius: float = 0.5,
) -> None:
    """Check and auto-correct dangling endpoints on newly added wires.

    Iterates every endpoint of wires added after *first_new_wire_index*
    and checks that it coincides (within *tolerance* mm) with a pin,
    another wire endpoint/midpoint, or a junction.

    If an endpoint is dangling but a valid connection point exists within
    *correction_radius* mm, the endpoint is snapped to that point.
    Otherwise a warning is emitted on stderr.
    """
    all_wires = sch.wires
    new_wires = all_wires[first_new_wire_index:]
    if not new_wires:
        return

    # Collect all known connection points: wire endpoints, junction
    # positions, pin positions of placed symbols.
    connection_points: list[tuple[float, float]] = []
    for wire in all_wires:
        connection_points.append(wire.start)
        connection_points.append(wire.end)
    for junc in sch.junctions:
        connection_points.append(junc.position)

    # Pin positions of all symbols
    for sym in sch.symbols:
        lib_sym_sexp = sch.get_lib_symbol(sym.lib_id)
        if lib_sym_sexp is None:
            continue
        lib_sym_obj = LibrarySymbol.from_sexp(lib_sym_sexp)
        pin_positions = lib_sym_obj.get_all_pin_positions(
            instance_pos=sym.position,
            instance_rot=sym.rotation,
            mirror=sym.mirror,
        )
        connection_points.extend(pin_positions.values())

    for wire in new_wires:
        for attr in ("start", "end"):
            endpoint = getattr(wire, attr)
            # An endpoint is valid if it coincides with at least TWO
            # connection points (itself counted once as a wire endpoint,
            # plus at least one other element).
            matches = sum(
                1
                for cp in connection_points
                if abs(cp[0] - endpoint[0]) < tolerance
                and abs(cp[1] - endpoint[1]) < tolerance
            )
            if matches >= 2:
                continue

            # Try to auto-correct: find the nearest connection point
            # within correction_radius (excluding the endpoint itself).
            best_cp = None
            best_dist = correction_radius
            for cp in connection_points:
                dist = ((cp[0] - endpoint[0]) ** 2 + (cp[1] - endpoint[1]) ** 2) ** 0.5
                if dist < tolerance:
                    continue  # Skip the endpoint itself
                if dist < best_dist:
                    best_dist = dist
                    best_cp = cp

            if best_cp is not None:
                print(
                    f"Auto-corrected wire endpoint "
                    f"({endpoint[0]:.4f}, {endpoint[1]:.4f}) -> "
                    f"({best_cp[0]:.4f}, {best_cp[1]:.4f}) "
                    f"(drift: {best_dist:.4f} mm)",
                    file=sys.stderr,
                )
                setattr(wire, attr, best_cp)
            else:
                print(
                    f"Warning: wire endpoint ({endpoint[0]:.2f}, {endpoint[1]:.2f}) "
                    "may be dangling (not connected to any pin, wire, or junction)",
                    file=sys.stderr,
                )


# Backward-compatible aliases -- the canonical implementations now live in
# kicad_tools.schema.instances and are imported at the top of this module.
_find_project_name = find_project_name
_build_instance_path = build_instance_path


def run_add_component(args) -> int:
    """Execute the add-component command."""
    schematic_path = Path(args.schematic)

    try:
        sch = Schematic.load(schematic_path)
    except (FileNotFoundError, KiCadFileNotFoundError):
        print(f"Error: Schematic not found: {schematic_path}", file=sys.stderr)
        return 1

    # Snap placement coordinates to grid
    at_x = _snap(args.at[0])
    at_y = _snap(args.at[1])
    position = (at_x, at_y)

    rotation = getattr(args, "rotation", 0) or 0
    mirror = getattr(args, "mirror", "") or ""

    is_power = _is_power_symbol(args.lib_id)

    # Collect planned actions for reporting
    planned: list[PlannedAction] = []

    # --- Library symbol resolution ---
    lib_manager = LibraryManager()

    if args.lib_paths:
        for lp in args.lib_paths:
            lib_manager.add_search_path(lp)

    if args.libs:
        for lib_file in args.libs:
            lib_manager.load_library(lib_file)

    # Also load symbols already embedded in the schematic
    if sch.lib_symbols:
        for sym_sexp in sch.lib_symbols.find_all("symbol"):
            sym_name = sym_sexp.get_string(0) or ""
            if sym_name:
                lib_sym_obj = LibrarySymbol.from_sexp(sym_sexp)
                # Register in library manager under a synthetic library
                # derived from the lib_id prefix (e.g., "Device" from "Device:R")
                if ":" in sym_name:
                    lib_name = sym_name.split(":")[0]
                    sym_short = sym_name.split(":", 1)[1]
                    from kicad_tools.schema.library import SymbolLibrary

                    if lib_name not in lib_manager.libraries:
                        lib_manager.libraries[lib_name] = SymbolLibrary(
                            path="", symbols={}
                        )
                    lib_manager.libraries[lib_name].symbols[sym_short] = lib_sym_obj

    # Check if we need to embed the library symbol
    need_embed = sch.get_lib_symbol(args.lib_id) is None

    if need_embed:
        lib_sym = lib_manager.get_symbol(args.lib_id)
        if lib_sym is None:
            print(
                f"Error: Library symbol '{args.lib_id}' not found. "
                "Use --lib-path or --lib to specify library sources.",
                file=sys.stderr,
            )
            return 1
        planned.append(
            PlannedAction("embed", f"Embed library definition for {args.lib_id}")
        )
    else:
        lib_sym = None  # Already embedded, no need to re-embed

    # --- Plan the symbol placement ---
    if is_power:
        power_name = args.lib_id.split(":", 1)[1]
        planned.append(
            PlannedAction(
                "power",
                f"Place power symbol {power_name} at ({at_x:.2f}, {at_y:.2f})"
                f" rotation={rotation}",
            )
        )
    else:
        reference = args.reference
        value = args.value or ""
        footprint = args.footprint or ""
        if not reference:
            print(
                "Error: --reference is required for non-power symbols",
                file=sys.stderr,
            )
            return 1
        planned.append(
            PlannedAction(
                "symbol",
                f"Place {args.lib_id} as {reference} (value={value!r},"
                f" footprint={footprint!r}) at ({at_x:.2f}, {at_y:.2f})"
                f" rotation={rotation} mirror={mirror!r}",
            )
        )

    # --- Plan wire connections ---
    connect_specs: list[ConnectSpec] = []
    if args.connects:
        for spec_str in args.connects:
            try:
                cs = parse_connect(spec_str)
            except ValueError as e:
                print(f"Error: {e}", file=sys.stderr)
                return 1
            # Try to snap the target to an existing connection point
            # (wire endpoint, junction, pin) within a 2 mm radius.
            # If nothing is nearby, fall back to grid snap.
            nearby = sch.find_nearest_connection_point(cs.target, radius=2.0)
            if nearby is not None:
                target = nearby
            else:
                target = (_snap(cs.target[0]), _snap(cs.target[1]))
            cs = ConnectSpec(
                pin_number=cs.pin_number,
                target=target,
            )
            connect_specs.append(cs)

    if connect_specs:
        # We need the library symbol to compute pin positions
        # Get it from the embedded or about-to-embed definition
        if need_embed and lib_sym is not None:
            pin_src = lib_sym
        else:
            lib_sym_sexp = sch.get_lib_symbol(args.lib_id)
            if lib_sym_sexp is not None:
                pin_src = LibrarySymbol.from_sexp(lib_sym_sexp)
            else:
                print(
                    f"Error: Cannot resolve pin positions for '{args.lib_id}'",
                    file=sys.stderr,
                )
                return 1

        pin_positions = pin_src.get_all_pin_positions(
            instance_pos=position,
            instance_rot=rotation,
            mirror=mirror,
        )

        for cs in connect_specs:
            if cs.pin_number not in pin_positions:
                available = ", ".join(sorted(pin_positions.keys()))
                print(
                    f"Error: Pin '{cs.pin_number}' not found on {args.lib_id}. "
                    f"Available pins: {available}",
                    file=sys.stderr,
                )
                return 1

            pin_pos = pin_positions[cs.pin_number]
            # Round to eliminate floating-point drift from rotation
            # transforms.  Do NOT use _snap() here -- the position is
            # already derived from a grid-snapped origin and applying
            # _snap() again can shift to the wrong grid point.
            pin_pos = _round_pos(pin_pos)

            planned.append(
                PlannedAction(
                    "wire",
                    f"Wire from pin {cs.pin_number} at"
                    f" ({pin_pos[0]:.2f}, {pin_pos[1]:.2f})"
                    f" to ({cs.target[0]:.2f}, {cs.target[1]:.2f})",
                )
            )

            # Check if target intersects existing wires (endpoint or midpoint)
            for wire in sch.wires:
                if _point_on_wire_segment(cs.target, wire.start, wire.end):
                    planned.append(
                        PlannedAction(
                            "junction",
                            f"Junction at ({cs.target[0]:.2f}, {cs.target[1]:.2f})"
                            f" (wire intersection)",
                        )
                    )
                    break  # One junction per target point is enough

    # --- Report planned actions ---
    if args.dry_run:
        print("DRY RUN - No changes will be made")
        print("=" * 60)

    print(f"Planned actions ({len(planned)}):")
    for action in planned:
        print(f"  [{action.kind}] {action.description}")

    if args.dry_run:
        return 0

    # --- Create backup if requested ---
    if args.backup:
        backup_path = (
            f"{schematic_path}.backup-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
        )
        shutil.copy2(schematic_path, backup_path)
        print(f"Backup created: {backup_path}")

    # --- Apply changes ---

    # 1. Embed library symbol if needed
    if need_embed and lib_sym is not None:
        # KiCad schematics embed symbols with the fully-qualified lib_id
        # (e.g. "Connector_Generic:Conn_01x04") while .kicad_sym files
        # use bare names ("Conn_01x04").  Rename before embedding so that
        # add_symbol / add_power can later look the symbol up by lib_id.
        if lib_sym.name != args.lib_id:
            lib_sym.name = args.lib_id
        sch.embed_lib_symbol(lib_sym)

    # 2. Derive project_name and instance_path for the instances block
    explicit_project = getattr(args, "project_name", "") or ""
    explicit_path = getattr(args, "instance_path", "") or ""

    if explicit_project:
        project_name = explicit_project
    else:
        project_name = _find_project_name(schematic_path)

    if explicit_path:
        instance_path = explicit_path
    else:
        sch_uuid = sch.uuid or ""
        if sch_uuid:
            instance_path = _build_instance_path(schematic_path, sch_uuid)
        else:
            instance_path = ""

    # 3. Place the symbol
    if is_power:
        power_name = args.lib_id.split(":", 1)[1]
        sym_instance = sch.add_power(
            power_name,
            position,
            rotation,
            project_name=project_name,
            instance_path=instance_path,
        )
    else:
        sym_instance = sch.add_symbol(
            lib_id=args.lib_id,
            reference=args.reference,
            value=args.value or "",
            footprint=args.footprint or "",
            position=position,
            rotation=rotation,
            mirror=mirror,
            project_name=project_name,
            instance_path=instance_path,
        )

    # 4. Add wire connections (reuse pin_positions computed during planning)
    if connect_specs:
        # Record wires that existed before we start adding, so we can
        # correctly detect pre-existing wires for junction insertion.
        pre_existing_wire_count = len(sch.wires)

        for cs in connect_specs:
            pin_pos = pin_positions.get(cs.pin_number)
            if pin_pos is None:
                continue

            pin_pos = _round_pos(pin_pos)

            # Skip duplicate: don't add a wire if start == end
            if (
                abs(pin_pos[0] - cs.target[0]) < 0.01
                and abs(pin_pos[1] - cs.target[1]) < 0.01
            ):
                continue

            sch.add_wire(pin_pos, cs.target)

            # Add junction if target intersects a pre-existing wire segment
            for wire in sch.wires[:pre_existing_wire_count]:
                if _point_on_wire_segment(cs.target, wire.start, wire.end):
                    sch.add_junction(cs.target)
                    break

    # 5. Post-placement validation: check for dangling wire endpoints
    if connect_specs:
        _validate_wire_endpoints(sch, pre_existing_wire_count)

    # 6. Save
    sch.save()

    print(f"\nComponent placed successfully: {sym_instance.lib_id}")
    if not is_power:
        print(f"  Reference: {sym_instance.reference}")
        print(f"  Value: {sym_instance.value}")
    print(f"  Position: ({position[0]:.2f}, {position[1]:.2f})")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Add a component symbol to a KiCad schematic",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("schematic", help="Path to .kicad_sch file")
    parser.add_argument(
        "--lib-id", required=True, help="Library symbol ID (e.g., Device:R, power:GND)"
    )
    parser.add_argument("--reference", help="Symbol reference (e.g., R1, U1)")
    parser.add_argument("--value", help="Component value (e.g., 10k, 100nF)")
    parser.add_argument(
        "--footprint", help="Footprint name (e.g., Resistor_SMD:R_0402_1005Metric)"
    )
    parser.add_argument(
        "--at",
        nargs=2,
        type=float,
        required=True,
        metavar=("X", "Y"),
        help="Placement coordinates",
    )
    parser.add_argument(
        "--rotation", type=float, default=0, help="Rotation in degrees (default: 0)"
    )
    parser.add_argument(
        "--mirror", choices=["x", "y"], default="", help="Mirror mode (x or y)"
    )
    parser.add_argument(
        "--connect",
        action="append",
        dest="connects",
        metavar="PIN:X,Y",
        help="Connect pin to target coordinates (e.g., 1:120,80). Repeatable.",
    )
    parser.add_argument(
        "--lib-path", action="append", dest="lib_paths", help="Library search path"
    )
    parser.add_argument(
        "--lib", action="append", dest="libs", help="Specific library file"
    )
    parser.add_argument(
        "--project-name",
        dest="project_name",
        default="",
        help="Project name for the instances block (auto-detected from .kicad_pro)",
    )
    parser.add_argument(
        "--instance-path",
        dest="instance_path",
        default="",
        help="Hierarchy path for instances block (auto-detected from schematic UUID)",
    )
    parser.add_argument(
        "--dry-run", "-n", action="store_true", help="Preview without modifying"
    )
    parser.add_argument(
        "--backup", action="store_true", help="Create backup before modifying"
    )

    args = parser.parse_args(argv)
    return run_add_component(args)


if __name__ == "__main__":
    sys.exit(main())
