#!/usr/bin/env python3
"""
Add a pull-up or pull-down resistor to a schematic pin.

Composite command that places a resistor, a power/ground symbol, and wires
connecting the resistor between the target IC pin and the power rail.

Usage:
    kct sch add-pull-resistor board.kicad_sch \\
        --ref U5 --pin 25 --direction up --value 10k --power-net +3.3VA
    kct sch add-pull-resistor board.kicad_sch \\
        --ref U1 --pin 3 --direction down --value 10k --power-net GNDD
    kct sch add-pull-resistor board.kicad_sch \\
        --ref U5 --pin 25 --direction up --value 10k --dry-run

Options:
    --ref <reference>      Symbol reference of target IC (e.g., U5)
    --pin <pin>            Pin number on target IC (e.g., 25)
    --direction <up|down>  Pull-up (power) or pull-down (ground)
    --value <value>        Resistor value (e.g., 10k)
    --power-net <net>      Power/ground net name (default: +3.3V for up, GND for down)
    --reference <ref>      Explicit reference for the new resistor (default: R?)
    --footprint <fp>       Resistor footprint (default: Resistor_SMD:R_0402_1005Metric)
    --offset <mm>          Distance from IC pin to resistor center (default: 5.08)
    --lib-path <path>      Library search path (repeatable)
    --lib <file>           Specific library file (repeatable)
    --dry-run              Preview without modifying
    --backup               Create backup before writing
    --force                Place even if collision detected
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
from kicad_tools.schema.wire import Wire


@dataclass
class PlannedAction:
    """An action the command will take, for reporting."""

    kind: str  # "symbol", "power", "wire", "junction", "embed"
    description: str


def _snap(value: float, grid: float = 1.27) -> float:
    """Snap a coordinate to the nearest grid point."""
    return round(value / grid) * grid


def _cross(o: tuple[float, float], a: tuple[float, float], b: tuple[float, float]) -> float:
    """Return cross product of vectors (a - o) and (b - o)."""
    return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])


def _on_segment(
    p: tuple[float, float],
    q: tuple[float, float],
    r: tuple[float, float],
) -> bool:
    """Check if point q lies on segment pr (assuming collinear)."""
    return (
        min(p[0], r[0]) <= q[0] + 1e-9
        and q[0] <= max(p[0], r[0]) + 1e-9
        and min(p[1], r[1]) <= q[1] + 1e-9
        and q[1] <= max(p[1], r[1]) + 1e-9
    )


def _segments_intersect(
    p1: tuple[float, float],
    p2: tuple[float, float],
    p3: tuple[float, float],
    p4: tuple[float, float],
    *,
    exclude_endpoints: bool = True,
) -> bool:
    """Test whether two line segments (p1-p2) and (p3-p4) intersect.

    Uses the standard cross-product orientation test.  When
    *exclude_endpoints* is True (default), touching only at shared
    endpoints is **not** considered an intersection -- this avoids
    false positives when the planned wire starts or ends on an existing
    wire endpoint (which is a valid KiCad T-junction, not a crossing).
    """
    d1 = _cross(p3, p4, p1)
    d2 = _cross(p3, p4, p2)
    d3 = _cross(p1, p2, p3)
    d4 = _cross(p1, p2, p4)

    if ((d1 > 0 and d2 < 0) or (d1 < 0 and d2 > 0)) and (
        (d3 > 0 and d4 < 0) or (d3 < 0 and d4 > 0)
    ):
        return True  # proper crossing -- segments straddle each other

    # Collinear / endpoint-on-segment cases
    eps = 1e-9
    if abs(d1) < eps and _on_segment(p3, p1, p4):
        if exclude_endpoints and (_pt_eq(p1, p3) or _pt_eq(p1, p4)):
            return False
        return True
    if abs(d2) < eps and _on_segment(p3, p2, p4):
        if exclude_endpoints and (_pt_eq(p2, p3) or _pt_eq(p2, p4)):
            return False
        return True
    if abs(d3) < eps and _on_segment(p1, p3, p2):
        if exclude_endpoints and (_pt_eq(p3, p1) or _pt_eq(p3, p2)):
            return False
        return True
    if abs(d4) < eps and _on_segment(p1, p4, p2):
        if exclude_endpoints and (_pt_eq(p4, p1) or _pt_eq(p4, p2)):
            return False
        return True

    return False


def _pt_eq(a: tuple[float, float], b: tuple[float, float], tol: float = 0.01) -> bool:
    """Check approximate point equality."""
    return abs(a[0] - b[0]) < tol and abs(a[1] - b[1]) < tol


def _check_wire_path_crossings(
    sch: Schematic,
    wire_start: tuple[float, float],
    wire_end: tuple[float, float],
) -> list[str]:
    """Check if a planned wire segment crosses existing wires or labels.

    Returns a list of warning strings, one per crossing detected.
    """
    warnings: list[str] = []
    path_wire = Wire(start=wire_start, end=wire_end)

    # Check wire-to-wire crossings
    for existing in sch.wires:
        if _segments_intersect(wire_start, wire_end, existing.start, existing.end):
            warnings.append(
                f"Wire path ({wire_start[0]:.2f},{wire_start[1]:.2f})->"
                f"({wire_end[0]:.2f},{wire_end[1]:.2f}) crosses existing wire "
                f"({existing.start[0]:.2f},{existing.start[1]:.2f})->"
                f"({existing.end[0]:.2f},{existing.end[1]:.2f})"
            )

    # Check labels on the wire path
    for label in sch.labels:
        if path_wire.contains_point(label.position, tolerance=0.1):
            if not (_pt_eq(label.position, wire_start) or _pt_eq(label.position, wire_end)):
                warnings.append(
                    f"Wire path crosses label '{label.text}' at "
                    f"({label.position[0]:.2f},{label.position[1]:.2f})"
                )

    for glabel in sch.global_labels:
        if path_wire.contains_point(glabel.position, tolerance=0.1):
            if not (_pt_eq(glabel.position, wire_start) or _pt_eq(glabel.position, wire_end)):
                warnings.append(
                    f"Wire path crosses global label '{glabel.text}' at "
                    f"({glabel.position[0]:.2f},{glabel.position[1]:.2f})"
                )

    for hlabel in sch.hierarchical_labels:
        if path_wire.contains_point(hlabel.position, tolerance=0.1):
            if not (_pt_eq(hlabel.position, wire_start) or _pt_eq(hlabel.position, wire_end)):
                warnings.append(
                    f"Wire path crosses hierarchical label '{hlabel.text}' at "
                    f"({hlabel.position[0]:.2f},{hlabel.position[1]:.2f})"
                )

    return warnings


def _find_clear_offset(
    sch: Schematic,
    pin_pos: tuple[float, float],
    ic_center_x: float,
    wire_start_y: float,
    wire_end_y: float,
    grid: float = 1.27,
    max_steps: int = 4,
) -> float | None:
    """Find a horizontal X offset that avoids wire crossings.

    Tries offsets away from *ic_center_x* first, then toward it.
    Returns the snapped X coordinate of a clear column, or ``None``
    if no clear path is found within *max_steps* grid steps.
    """
    pin_x = pin_pos[0]
    # Prefer moving away from IC center
    if pin_x >= ic_center_x:
        primary_dir = 1.0
    else:
        primary_dir = -1.0

    for step in range(1, max_steps + 1):
        for direction in (primary_dir, -primary_dir):
            candidate_x = _snap(pin_x + direction * step * grid)
            # Check vertical segment at candidate_x
            seg_start = (candidate_x, wire_start_y)
            seg_end = (candidate_x, wire_end_y)
            crossings = _check_wire_path_crossings(sch, seg_start, seg_end)
            if not crossings:
                return candidate_x

    return None


def _setup_lib_manager(
    sch: Schematic,
    lib_paths: list[str] | None,
    libs: list[str] | None,
) -> LibraryManager:
    """Create and configure a LibraryManager with embedded + external sources."""
    lib_manager = LibraryManager()

    if lib_paths:
        for lp in lib_paths:
            lib_manager.add_search_path(lp)

    if libs:
        for lib_file in libs:
            lib_manager.load_library(lib_file)

    # Load symbols already embedded in the schematic
    lib_manager.load_embedded(sch)

    return lib_manager


def _resolve_pin_position(
    sch: Schematic,
    lib_manager: LibraryManager,
    reference: str,
    pin_number: str,
) -> tuple[float, float] | None:
    """Resolve a pin's absolute position using library data.

    Returns (x, y) or None if the symbol or pin cannot be found.
    """
    symbol = sch.get_symbol(reference)
    if symbol is None:
        return None

    lib_sym = lib_manager.get_symbol(symbol.lib_id)
    if lib_sym is None and ":" in symbol.lib_id:
        sym_name = symbol.lib_id.split(":", 1)[1]
        lib_sym = lib_manager.get_symbol(sym_name)

    if lib_sym is None:
        return None

    pin_positions = lib_sym.get_all_pin_positions(
        instance_pos=symbol.position,
        instance_rot=symbol.rotation,
        mirror=symbol.mirror,
    )

    return pin_positions.get(pin_number)


def _auto_reference(sch: Schematic) -> str:
    """Return next available R reference by scanning existing resistors.

    Scans all symbols in the schematic for references matching 'R<number>',
    then returns 'R<max+1>'. Falls back to 'R1' if no numbered resistors exist.
    """
    max_n = 0
    for sym in sch.symbols:
        ref = sym.reference or ""
        if ref.startswith("R") and len(ref) > 1 and ref[1:].isdigit():
            n = int(ref[1:])
            if n > max_n:
                max_n = n
    return f"R{max_n + 1}"


def _check_collisions(
    sch: Schematic,
    positions: list[tuple[float, float]],
    tolerance: float = 2.54,
) -> list[str]:
    """Check if any existing symbol is within tolerance of the planned positions.

    Returns a list of warning messages for each collision found.
    """
    warnings = []
    for sym in sch.symbols:
        sx, sy = sym.position
        for px, py in positions:
            dist = ((sx - px) ** 2 + (sy - py) ** 2) ** 0.5
            if dist < tolerance:
                warnings.append(
                    f"Nearby component {sym.reference} at ({sx:.2f}, {sy:.2f}) "
                    f"is {dist:.2f}mm from planned position ({px:.2f}, {py:.2f})"
                )
    return warnings


def run_add_pull_resistor(args) -> int:
    """Execute the add-pull-resistor command.

    1. Load schematic
    2. Resolve IC pin position (--ref, --pin)
    3. Compute resistor and power-symbol positions
    4. Auto-assign reference if --reference not given
    5. Embed library symbols if needed (Device:R, power:<net>)
    6. Plan: symbol, power, wire x2, junctions
    7. Dry-run report or apply + save
    """
    schematic_path = Path(args.schematic)

    try:
        sch = Schematic.load(schematic_path)
    except (FileNotFoundError, KiCadFileNotFoundError):
        print(f"Error: Schematic not found: {schematic_path}", file=sys.stderr)
        return 1

    # Set up library manager
    lib_manager = _setup_lib_manager(
        sch,
        getattr(args, "lib_paths", None),
        getattr(args, "libs", None),
    )

    # --- Resolve target pin position ---
    pin_pos = _resolve_pin_position(sch, lib_manager, args.ref, args.pin)
    if pin_pos is None:
        # Determine whether the ref or pin is invalid for a better error
        symbol = sch.get_symbol(args.ref)
        if symbol is None:
            print(
                f"Error: Symbol reference '{args.ref}' not found in schematic",
                file=sys.stderr,
            )
        else:
            print(
                f"Error: Pin '{args.pin}' not found on {args.ref}",
                file=sys.stderr,
            )
        return 1

    pin_x, pin_y = pin_pos

    # --- Compute geometry ---
    offset = getattr(args, "offset", 5.08) or 5.08
    direction = args.direction

    # Determine power net name
    power_net = getattr(args, "power_net", None)
    if power_net is None:
        power_net = "+3.3V" if direction == "up" else "GND"

    # Resistor lib_id
    resistor_lib_id = "Device:R"

    # For pull-up: resistor goes above the pin, power symbol above resistor
    # For pull-down: resistor goes below the pin, ground symbol below resistor
    #
    # KiCad screen coordinates: Y increases downward
    # Device:R pin 1 at local (0, +3.81) -> top at rotation 0
    # Device:R pin 2 at local (0, -3.81) -> bottom at rotation 0
    #
    # Pull-up layout (direction == "up"):
    #   power_symbol  at (pin_x, pin_y - offset - 3.81)
    #       |
    #   [R] center    at (pin_x, pin_y - offset)
    #       |
    #   target_pin    at (pin_x, pin_y)
    #
    # Resistor at rotation 0: pin 1 (top) at center_y - 3.81, pin 2 (bottom) at center_y + 3.81
    # Wait -- KiCad pin definitions:
    #   pin 1 at local (0, 3.81) with direction 270 -> this means pin 1 is at y = center_y + 3.81 (below center)
    #   Actually with rotation 0 and the pin directions, let's use the library to compute.
    #
    # For simplicity, let's place the resistor at offset distance from the pin,
    # and use get_all_pin_positions to determine the actual pin coordinates.
    # Then wire from IC pin to nearest resistor pin, and from far resistor pin to power.

    if direction == "up":
        # Resistor center above the IC pin
        res_x = _snap(pin_x)
        res_y = _snap(pin_y - offset)
        res_rotation = 0.0
    else:
        # Resistor center below the IC pin
        res_x = _snap(pin_x)
        res_y = _snap(pin_y + offset)
        res_rotation = 0.0

    res_position = (res_x, res_y)

    # --- Resolve resistor pin positions ---
    # We need the Device:R library symbol to compute pin positions
    resistor_lib_sym = lib_manager.get_symbol(resistor_lib_id)
    need_embed_resistor = sch.get_lib_symbol(resistor_lib_id) is None

    if resistor_lib_sym is None:
        print(
            f"Error: Library symbol '{resistor_lib_id}' not found. "
            "Use --lib-path or --lib to specify library sources, "
            "or ensure it is embedded in the schematic.",
            file=sys.stderr,
        )
        return 1

    res_pin_positions = resistor_lib_sym.get_all_pin_positions(
        instance_pos=res_position,
        instance_rot=res_rotation,
        mirror="",
    )

    if "1" not in res_pin_positions or "2" not in res_pin_positions:
        print(
            "Error: Device:R does not have expected pins 1 and 2",
            file=sys.stderr,
        )
        return 1

    res_pin1 = res_pin_positions["1"]  # Pin 1
    res_pin2 = res_pin_positions["2"]  # Pin 2

    # Determine which resistor pin is closer to the IC pin (connects to IC)
    # and which is farther (connects to power/ground)
    dist1 = ((res_pin1[0] - pin_x) ** 2 + (res_pin1[1] - pin_y) ** 2) ** 0.5
    dist2 = ((res_pin2[0] - pin_x) ** 2 + (res_pin2[1] - pin_y) ** 2) ** 0.5

    if dist1 <= dist2:
        ic_side_pin = res_pin1
        power_side_pin = res_pin2
    else:
        ic_side_pin = res_pin2
        power_side_pin = res_pin1

    # Power symbol position: at the far end of the resistor
    # Power symbols connect at their pin position, so place at the power-side pin
    if direction == "up":
        power_x = _snap(power_side_pin[0])
        power_y = _snap(power_side_pin[1])
        power_rotation = 0.0
    else:
        power_x = _snap(power_side_pin[0])
        power_y = _snap(power_side_pin[1])
        power_rotation = 180.0

    power_position = (power_x, power_y)

    # --- Auto-assign reference ---
    reference = getattr(args, "reference", None)
    if not reference:
        reference = _auto_reference(sch)

    # --- Footprint ---
    footprint = getattr(args, "footprint", None) or "Resistor_SMD:R_0402_1005Metric"

    # --- Value ---
    value = args.value

    # --- Check for collisions ---
    collision_warnings = _check_collisions(sch, [res_position, power_position])
    force = getattr(args, "force", False)

    if collision_warnings and not force:
        for warning in collision_warnings:
            print(f"Warning: {warning}", file=sys.stderr)

    # --- Check for wire path crossings and reroute if needed ---
    # Compute the planned wire segments (IC pin -> resistor, resistor -> power)
    ic_pin_snapped = (_snap(pin_x), _snap(pin_y))
    ic_side_snapped = (_snap(ic_side_pin[0]), _snap(ic_side_pin[1]))
    power_side_snapped = (_snap(power_side_pin[0]), _snap(power_side_pin[1]))
    power_pos_snapped = (_snap(power_x), _snap(power_y))

    # Build list of wire segments to place: each is (start, end)
    wire_segments: list[tuple[tuple[float, float], tuple[float, float]]] = []
    rerouted = False

    # Check the IC-side wire for crossings
    ic_wire_crossings = _check_wire_path_crossings(sch, ic_pin_snapped, ic_side_snapped)

    if ic_wire_crossings and not force:
        # Try L-shaped reroute: find a clear vertical column, then route
        # horizontally from IC pin to the column, then vertically to resistor
        ic_symbol = sch.get_symbol(args.ref)
        ic_center_x = ic_symbol.position[0] if ic_symbol else pin_x

        clear_x = _find_clear_offset(
            sch,
            ic_pin_snapped,
            ic_center_x,
            wire_start_y=ic_pin_snapped[1],
            wire_end_y=ic_side_snapped[1],
        )

        if clear_x is not None:
            rerouted = True
            # Shift the resistor and power symbol to the new column
            clear_x - res_x
            res_x = clear_x
            res_position = (res_x, res_y)

            # Recompute resistor pin positions at the new X
            res_pin_positions = resistor_lib_sym.get_all_pin_positions(
                instance_pos=res_position,
                instance_rot=res_rotation,
                mirror="",
            )
            res_pin1 = res_pin_positions["1"]
            res_pin2 = res_pin_positions["2"]

            dist1 = ((res_pin1[0] - pin_x) ** 2 + (res_pin1[1] - pin_y) ** 2) ** 0.5
            dist2 = ((res_pin2[0] - pin_x) ** 2 + (res_pin2[1] - pin_y) ** 2) ** 0.5
            if dist1 <= dist2:
                ic_side_pin = res_pin1
                power_side_pin = res_pin2
            else:
                ic_side_pin = res_pin2
                power_side_pin = res_pin1

            # Recompute power position
            power_x = _snap(power_side_pin[0])
            power_y = _snap(power_side_pin[1])
            power_position = (power_x, power_y)

            # Recompute snapped coordinates
            ic_side_snapped = (_snap(ic_side_pin[0]), _snap(ic_side_pin[1]))
            power_side_snapped = (_snap(power_side_pin[0]), _snap(power_side_pin[1]))
            power_pos_snapped = (_snap(power_x), _snap(power_y))

            # L-shaped route: horizontal from IC pin, then vertical to resistor
            corner = (_snap(clear_x), ic_pin_snapped[1])
            wire_segments.append((ic_pin_snapped, corner))
            wire_segments.append((corner, ic_side_snapped))

            print(
                f"Note: Rerouted wire via L-shape at x={clear_x:.2f} "
                f"to avoid crossing existing wires",
                file=sys.stderr,
            )
        else:
            # No clear path found
            for warning in ic_wire_crossings:
                print(f"Error: {warning}", file=sys.stderr)
            print(
                "Error: Cannot find a clear wire path. "
                "Use --force to place anyway (may create net shorts).",
                file=sys.stderr,
            )
            return 1

    if not rerouted:
        # Straight wire from IC pin to resistor (no crossing or --force)
        wire_segments.append((ic_pin_snapped, ic_side_snapped))

    # Check the power-side wire for crossings (always straight -- shorter segment)
    power_wire_crossings = _check_wire_path_crossings(sch, power_side_snapped, power_pos_snapped)
    if power_wire_crossings and not force:
        for warning in power_wire_crossings:
            print(f"Warning: {warning}", file=sys.stderr)

    wire_segments.append((power_side_snapped, power_pos_snapped))

    # --- Plan actions ---
    planned: list[PlannedAction] = []

    # Embed resistor symbol if needed
    if need_embed_resistor:
        planned.append(PlannedAction("embed", f"Embed library definition for {resistor_lib_id}"))

    # Embed power symbol if needed
    power_lib_id = f"power:{power_net}"
    need_embed_power = sch.get_lib_symbol(power_lib_id) is None
    power_lib_sym = lib_manager.get_symbol(power_lib_id)

    if need_embed_power:
        if power_lib_sym is None:
            print(
                f"Error: Library symbol '{power_lib_id}' not found. "
                "Use --lib-path or --lib to specify library sources.",
                file=sys.stderr,
            )
            return 1
        planned.append(PlannedAction("embed", f"Embed library definition for {power_lib_id}"))

    # Place resistor
    planned.append(
        PlannedAction(
            "symbol",
            f"Place {resistor_lib_id} as {reference} (value={value!r},"
            f" footprint={footprint!r}) at ({res_x:.2f}, {res_y:.2f})",
        )
    )

    # Place power/ground symbol
    planned.append(
        PlannedAction(
            "power",
            f"Place power symbol {power_net} at ({power_x:.2f}, {power_y:.2f})",
        )
    )

    # Wire segments
    for seg_start, seg_end in wire_segments:
        planned.append(
            PlannedAction(
                "wire",
                f"Wire from ({seg_start[0]:.2f}, {seg_start[1]:.2f})"
                f" to ({seg_end[0]:.2f}, {seg_end[1]:.2f})",
            )
        )

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
        backup_path = f"{schematic_path}.backup-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
        shutil.copy2(schematic_path, backup_path)
        print(f"Backup created: {backup_path}")

    # --- Apply changes ---

    # 1. Embed library symbols if needed
    if need_embed_resistor and resistor_lib_sym is not None:
        if resistor_lib_sym.name != resistor_lib_id:
            resistor_lib_sym.name = resistor_lib_id
        sch.embed_lib_symbol(resistor_lib_sym)

    if need_embed_power and power_lib_sym is not None:
        if power_lib_sym.name != power_lib_id:
            power_lib_sym.name = power_lib_id
        sch.embed_lib_symbol(power_lib_sym)

    # 1b. Derive project name and instance path for the instances block
    project_name = find_project_name(schematic_path)
    sch_uuid = sch.uuid or ""
    instance_path = build_instance_path(schematic_path, sch_uuid) if sch_uuid else ""

    # 2. Place the resistor
    sch.add_symbol(
        lib_id=resistor_lib_id,
        reference=reference,
        value=value,
        footprint=footprint,
        position=res_position,
        rotation=res_rotation,
        project_name=project_name,
        instance_path=instance_path,
    )

    # 3. Place the power/ground symbol
    sch.add_power(
        power_net,
        power_position,
        power_rotation,
        project_name=project_name,
        instance_path=instance_path,
    )

    # 4. Add wires
    for seg_start, seg_end in wire_segments:
        if abs(seg_start[0] - seg_end[0]) > 0.01 or abs(seg_start[1] - seg_end[1]) > 0.01:
            sch.add_wire(seg_start, seg_end)

    # 5. Save
    sch.save()

    print(f"\nPull-{direction} resistor placed successfully")
    print(f"  Reference: {reference}")
    print(f"  Value: {value}")
    print(f"  Position: ({res_x:.2f}, {res_y:.2f})")
    print(f"  Power net: {power_net}")
    if rerouted:
        print("  Route: L-shaped (rerouted to avoid wire crossings)")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Add a pull-up or pull-down resistor to a schematic pin",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("schematic", help="Path to .kicad_sch file")
    parser.add_argument("--ref", required=True, help="Symbol reference of target IC (e.g., U5)")
    parser.add_argument("--pin", required=True, help="Pin number on target IC (e.g., 25)")
    parser.add_argument(
        "--direction",
        required=True,
        choices=["up", "down"],
        help="Pull-up (power) or pull-down (ground)",
    )
    parser.add_argument("--value", required=True, help="Resistor value (e.g., 10k)")
    parser.add_argument(
        "--power-net",
        dest="power_net",
        help="Power/ground net name (default: +3.3V for up, GND for down)",
    )
    parser.add_argument(
        "--reference",
        help="Reference designator for new resistor (default: R? auto-assign)",
    )
    parser.add_argument(
        "--footprint",
        default="Resistor_SMD:R_0402_1005Metric",
        help="Resistor footprint (default: Resistor_SMD:R_0402_1005Metric)",
    )
    parser.add_argument(
        "--offset",
        type=float,
        default=5.08,
        help="Grid distance from IC pin to resistor center in mm (default: 5.08)",
    )
    parser.add_argument("--lib-path", action="append", dest="lib_paths", help="Library search path")
    parser.add_argument("--lib", action="append", dest="libs", help="Specific library file")
    parser.add_argument("--dry-run", "-n", action="store_true", help="Preview without modifying")
    parser.add_argument("--backup", action="store_true", help="Create backup before modifying")
    parser.add_argument("--force", action="store_true", help="Place even if collision detected")

    args = parser.parse_args(argv)
    return run_add_pull_resistor(args)


if __name__ == "__main__":
    sys.exit(main())
