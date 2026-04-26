#!/usr/bin/env python3
"""
Add a bypass/decoupling capacitor to an IC power pin in a KiCad schematic.

This is a composite command that places a capacitor, ground power symbol,
and wires connecting them to a specified IC pin.

Usage:
    kct sch add-bypass-cap board.kicad_sch --ref U8 --pin 4 \\
        --value 100nF --ground-net GNDD
    kct sch add-bypass-cap board.kicad_sch --ref U1 --pin 3 --dry-run

Options:
    --ref <REF>            Target IC reference designator (e.g., U8)
    --pin <PIN>            Target pin number on that IC (e.g., 4)
    --value <VALUE>        Capacitor value (default: 100nF)
    --ground-net <NET>     Ground power symbol name (default: GND)
    --footprint <FP>       Capacitor footprint (default: Capacitor_SMD:C_0402_1005Metric)
    --reference <REF>      Capacitor reference (auto-assigned if omitted)
    --offset <mm>          Distance from pin to cap body centre (default: 5.08)
    --dry-run              Preview without modifying
    --backup               Create timestamped backup before writing
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from kicad_tools.exceptions import FileNotFoundError as KiCadFileNotFoundError
from kicad_tools.schema import Schematic
from kicad_tools.schema.instances import build_instance_path, find_project_name
from kicad_tools.schema.library import LibrarySymbol


@dataclass
class PlannedAction:
    """An action the command will take, for reporting."""

    kind: str  # "capacitor", "ground", "wire", "junction", "embed"
    description: str


def _snap(value: float, grid: float = 1.27) -> float:
    """Snap a coordinate to the nearest grid point."""
    return round(value / grid) * grid


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



def _auto_reference(sch: Schematic, prefix: str = "C") -> str:
    """Find the next available reference designator for a given prefix.

    Scans existing symbols and returns ``prefix + (max_N + 1)``
    where max_N is the highest existing number for that prefix.
    Falls back to ``prefix + "1"`` if none exist.
    """
    max_num = 0
    pattern = re.compile(rf"^{re.escape(prefix)}(\d+)$")
    for sym in sch.symbols:
        ref = sym.reference
        m = pattern.match(ref)
        if m:
            num = int(m.group(1))
            if num > max_num:
                max_num = num
    return f"{prefix}{max_num + 1}"


def _compute_cap_offset(
    pin_rotation: float, offset_distance: float
) -> tuple[float, float]:
    """Compute the offset direction for cap placement from a pin.

    Pin rotation in KiCad library symbols:
      0   = pin points right (stub extends right from IC body)
      90  = pin points up
      180 = pin points left
      270 = pin points down

    The capacitor is placed in the direction the pin stub extends
    (i.e., away from the IC body, continuing outward from the pin tip).

    Returns (dx, dy) offset from the pin's schematic position.
    """
    # Normalize rotation to 0-360
    rot = pin_rotation % 360

    if abs(rot - 0) < 1:
        # Pin points right -> place cap to the right
        return (offset_distance, 0)
    elif abs(rot - 90) < 1:
        # Pin points up -> place cap above (negative y in KiCad)
        return (0, -offset_distance)
    elif abs(rot - 180) < 1:
        # Pin points left -> place cap to the left
        return (-offset_distance, 0)
    elif abs(rot - 270) < 1:
        # Pin points down -> place cap below (positive y in KiCad)
        return (0, offset_distance)
    else:
        # Diagonal or non-standard -- default to below
        return (0, offset_distance)


def _compute_ground_offset(
    pin_rotation: float, cap_offset: tuple[float, float], offset_distance: float
) -> tuple[float, float]:
    """Compute ground symbol position relative to pin position.

    The ground symbol is placed beyond the capacitor in the same direction.
    Total offset = cap_offset + another offset_distance in the same direction.
    """
    rot = pin_rotation % 360

    if abs(rot - 0) < 1:
        return (cap_offset[0] + offset_distance, cap_offset[1])
    elif abs(rot - 90) < 1:
        return (cap_offset[0], cap_offset[1] - offset_distance)
    elif abs(rot - 180) < 1:
        return (cap_offset[0] - offset_distance, cap_offset[1])
    elif abs(rot - 270) < 1:
        return (cap_offset[0], cap_offset[1] + offset_distance)
    else:
        return (cap_offset[0], cap_offset[1] + offset_distance)


def _cap_rotation_for_pin(pin_rotation: float) -> float:
    """Determine the capacitor rotation so its pins align with the wire axis.

    A Device:C in KiCad has pin 1 at (0, 3.81) pointing down (270 deg)
    and pin 2 at (0, -3.81) pointing up (90 deg) when rotation=0.
    That means at rotation 0 the cap is vertical (pins top/bottom).

    For a pin pointing right/left, we want the cap horizontal -> rotate 90.
    For a pin pointing up/down, we want the cap vertical -> rotate 0.
    """
    rot = pin_rotation % 360
    if abs(rot - 0) < 1 or abs(rot - 180) < 1:
        # Horizontal pin -> rotate cap 90 degrees
        return 90
    else:
        # Vertical pin -> keep cap at 0
        return 0


def run_add_bypass_cap(args) -> int:
    """Execute the add-bypass-cap command."""
    schematic_path = Path(args.schematic)

    try:
        sch = Schematic.load(schematic_path)
    except (FileNotFoundError, KiCadFileNotFoundError):
        print(f"Error: Schematic not found: {schematic_path}", file=sys.stderr)
        return 1

    # --- Resolve the target symbol ---
    target_sym = sch.get_symbol(args.ref)
    if target_sym is None:
        available_refs = sorted({s.reference for s in sch.symbols if s.reference})
        print(
            f"Error: Symbol '{args.ref}' not found in schematic. "
            f"Available references: {', '.join(available_refs[:20])}",
            file=sys.stderr,
        )
        return 1

    # --- Get the library symbol definition to resolve pin positions ---
    lib_sym_sexp = sch.get_lib_symbol(target_sym.lib_id)
    if lib_sym_sexp is None:
        print(
            f"Error: Library definition for '{target_sym.lib_id}' not found "
            "in schematic lib_symbols section.",
            file=sys.stderr,
        )
        return 1

    lib_sym = LibrarySymbol.from_sexp(lib_sym_sexp)

    # --- Resolve the target pin position ---
    pin_pos = lib_sym.get_pin_position(
        pin_number=args.pin,
        instance_pos=target_sym.position,
        instance_rot=target_sym.rotation,
        mirror=target_sym.mirror,
    )
    if pin_pos is None:
        available_pins = sorted(p.number for p in lib_sym.pins)
        print(
            f"Error: Pin '{args.pin}' not found on {args.ref} ({target_sym.lib_id}). "
            f"Available pins: {', '.join(available_pins)}",
            file=sys.stderr,
        )
        return 1

    pin_pos = (_snap(pin_pos[0]), _snap(pin_pos[1]))

    # Get pin orientation from the library definition for offset computation.
    # We need the pin's rotation in library coordinates, adjusted for instance
    # rotation and mirror.
    lib_pin = lib_sym.get_pin(args.pin)
    if lib_pin is not None:
        # Adjust pin rotation for instance rotation
        effective_pin_rot = (lib_pin.rotation + target_sym.rotation) % 360
        if target_sym.mirror == "x":
            # X mirror flips vertical direction
            if abs(effective_pin_rot - 90) < 1:
                effective_pin_rot = 270
            elif abs(effective_pin_rot - 270) < 1:
                effective_pin_rot = 90
        elif target_sym.mirror == "y":
            # Y mirror flips horizontal direction
            if abs(effective_pin_rot - 0) < 1:
                effective_pin_rot = 180
            elif abs(effective_pin_rot - 180) < 1:
                effective_pin_rot = 0
    else:
        # Fallback: place below
        effective_pin_rot = 270

    offset_distance = args.offset

    cap_offset = _compute_cap_offset(effective_pin_rot, offset_distance)
    cap_pos = (_snap(pin_pos[0] + cap_offset[0]), _snap(pin_pos[1] + cap_offset[1]))

    cap_rotation = _cap_rotation_for_pin(effective_pin_rot)

    ground_offset = _compute_ground_offset(effective_pin_rot, cap_offset, offset_distance)
    gnd_pos = (
        _snap(pin_pos[0] + ground_offset[0]),
        _snap(pin_pos[1] + ground_offset[1]),
    )

    # --- Determine capacitor reference ---
    cap_reference = args.reference if args.reference else _auto_reference(sch, "C")

    # --- Determine cap pin positions ---
    # Device:C has pin 1 at (0, 3.81) and pin 2 at (0, -3.81) at rotation 0.
    # We need to compute where pins 1 and 2 end up when the cap is placed.
    cap_lib_id = "Device:C"
    cap_lib_sexp = sch.get_lib_symbol(cap_lib_id)
    need_cap_embed = cap_lib_sexp is None

    gnd_lib_id = f"power:{args.ground_net}"
    gnd_lib_sexp = sch.get_lib_symbol(gnd_lib_id)
    need_gnd_embed = gnd_lib_sexp is None

    # Build a temporary LibrarySymbol for pin position calculation
    if cap_lib_sexp is not None:
        cap_lib_sym = LibrarySymbol.from_sexp(cap_lib_sexp)
    else:
        # Construct a minimal Device:C representation for planning
        cap_lib_sym = _make_default_cap_lib_sym()

    cap_pin_positions = cap_lib_sym.get_all_pin_positions(
        instance_pos=cap_pos,
        instance_rot=cap_rotation,
    )

    cap_pin1_pos = cap_pin_positions.get("1")
    cap_pin2_pos = cap_pin_positions.get("2")

    if cap_pin1_pos is None or cap_pin2_pos is None:
        print(
            "Error: Cannot resolve capacitor pin positions. "
            "Device:C library symbol may be malformed.",
            file=sys.stderr,
        )
        return 1

    cap_pin1_pos = (_snap(cap_pin1_pos[0]), _snap(cap_pin1_pos[1]))
    cap_pin2_pos = (_snap(cap_pin2_pos[0]), _snap(cap_pin2_pos[1]))

    # --- Select near (VDD) and far (GND) cap pins based on distance from IC pin ---
    # The cap pin closer to the IC pin connects to VDD; the other connects to GND.
    # This handles all orientations correctly: for left/down-pointing pins the
    # physical pin numbering would create a short if we always used pin 1 for VDD.
    dist1 = (
        (cap_pin1_pos[0] - pin_pos[0]) ** 2 + (cap_pin1_pos[1] - pin_pos[1]) ** 2
    ) ** 0.5
    dist2 = (
        (cap_pin2_pos[0] - pin_pos[0]) ** 2 + (cap_pin2_pos[1] - pin_pos[1]) ** 2
    ) ** 0.5
    if dist1 <= dist2:
        cap_vdd_pin_pos = cap_pin1_pos
        cap_gnd_pin_pos = cap_pin2_pos
        cap_vdd_pin_num = "1"
        cap_gnd_pin_num = "2"
    else:
        cap_vdd_pin_pos = cap_pin2_pos
        cap_gnd_pin_pos = cap_pin1_pos
        cap_vdd_pin_num = "2"
        cap_gnd_pin_num = "1"

    # --- Collect planned actions ---
    planned: list[PlannedAction] = []

    if need_cap_embed:
        planned.append(
            PlannedAction("embed", f"Embed library definition for {cap_lib_id}")
        )
    if need_gnd_embed:
        planned.append(
            PlannedAction("embed", f"Embed library definition for {gnd_lib_id}")
        )

    planned.append(
        PlannedAction(
            "capacitor",
            f"Place {cap_lib_id} as {cap_reference} (value={args.value!r},"
            f" footprint={args.footprint!r}) at ({cap_pos[0]:.2f}, {cap_pos[1]:.2f})"
            f" rotation={cap_rotation}",
        )
    )

    planned.append(
        PlannedAction(
            "ground",
            f"Place power symbol {args.ground_net} at"
            f" ({gnd_pos[0]:.2f}, {gnd_pos[1]:.2f})",
        )
    )

    # Wire from target pin to near (VDD) cap pin
    if (
        abs(pin_pos[0] - cap_vdd_pin_pos[0]) > 0.01
        or abs(pin_pos[1] - cap_vdd_pin_pos[1]) > 0.01
    ):
        planned.append(
            PlannedAction(
                "wire",
                f"Wire from target pin {args.pin} at"
                f" ({pin_pos[0]:.2f}, {pin_pos[1]:.2f})"
                f" to cap pin {cap_vdd_pin_num} at"
                f" ({cap_vdd_pin_pos[0]:.2f}, {cap_vdd_pin_pos[1]:.2f})",
            )
        )

    # Wire from far (GND) cap pin to ground symbol
    if (
        abs(cap_gnd_pin_pos[0] - gnd_pos[0]) > 0.01
        or abs(cap_gnd_pin_pos[1] - gnd_pos[1]) > 0.01
    ):
        planned.append(
            PlannedAction(
                "wire",
                f"Wire from cap pin {cap_gnd_pin_num} at"
                f" ({cap_gnd_pin_pos[0]:.2f}, {cap_gnd_pin_pos[1]:.2f})"
                f" to ground at ({gnd_pos[0]:.2f}, {gnd_pos[1]:.2f})",
            )
        )

    # Check for junction at target pin (if it lands on existing wire midpoint)
    needs_junction = False
    for wire in sch.wires:
        if _point_on_wire_midpoint(pin_pos, wire.start, wire.end):
            needs_junction = True
            planned.append(
                PlannedAction(
                    "junction",
                    f"Junction at ({pin_pos[0]:.2f}, {pin_pos[1]:.2f})"
                    f" (target pin intersects existing wire)",
                )
            )
            break

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

    # 1. Embed library symbols if needed
    if need_cap_embed:
        sch.embed_lib_symbol(cap_lib_sym)
    if need_gnd_embed:
        gnd_lib_sym = _make_default_gnd_lib_sym(args.ground_net)
        sch.embed_lib_symbol(gnd_lib_sym)

    # 1b. Derive project name and instance path for the instances block
    project_name = find_project_name(schematic_path)
    sch_uuid = sch.uuid or ""
    instance_path = build_instance_path(schematic_path, sch_uuid) if sch_uuid else ""

    # 2. Place the capacitor
    sch.add_symbol(
        lib_id=cap_lib_id,
        reference=cap_reference,
        value=args.value,
        footprint=args.footprint,
        position=cap_pos,
        rotation=cap_rotation,
        project_name=project_name,
        instance_path=instance_path,
    )

    # 3. Place the ground symbol
    sch.add_power(
        args.ground_net,
        gnd_pos,
        project_name=project_name,
        instance_path=instance_path,
    )

    # 4. Add wires
    # Wire from target pin to near (VDD) cap pin
    if (
        abs(pin_pos[0] - cap_vdd_pin_pos[0]) > 0.01
        or abs(pin_pos[1] - cap_vdd_pin_pos[1]) > 0.01
    ):
        sch.add_wire(pin_pos, cap_vdd_pin_pos)

    # Wire from far (GND) cap pin to ground
    if (
        abs(cap_gnd_pin_pos[0] - gnd_pos[0]) > 0.01
        or abs(cap_gnd_pin_pos[1] - gnd_pos[1]) > 0.01
    ):
        sch.add_wire(cap_gnd_pin_pos, gnd_pos)

    # 5. Add junction if target pin lands on existing wire midpoint
    if needs_junction:
        sch.add_junction(pin_pos)

    # 6. Save
    sch.save()

    print(f"\nBypass capacitor placed successfully:")
    print(f"  Reference: {cap_reference}")
    print(f"  Value: {args.value}")
    print(f"  Target: {args.ref} pin {args.pin} at ({pin_pos[0]:.2f}, {pin_pos[1]:.2f})")
    print(f"  Cap position: ({cap_pos[0]:.2f}, {cap_pos[1]:.2f})")
    print(f"  Ground: {args.ground_net} at ({gnd_pos[0]:.2f}, {gnd_pos[1]:.2f})")
    return 0


def _make_default_cap_lib_sym() -> LibrarySymbol:
    """Create a minimal Device:C library symbol for embedding.

    This matches the standard KiCad Device:C with pin 1 at (0, 3.81)
    pointing down (270 deg) and pin 2 at (0, -3.81) pointing up (90 deg).
    """
    from kicad_tools.schema.library import LibraryPin

    return LibrarySymbol(
        name="Device:C",
        properties={
            "Reference": "C",
            "Value": "C",
            "Footprint": "",
            "Datasheet": "",
        },
        pins=[
            LibraryPin(
                number="1",
                name="~",
                type="passive",
                position=(0, 3.81),
                rotation=270,
                length=1.27,
            ),
            LibraryPin(
                number="2",
                name="~",
                type="passive",
                position=(0, -3.81),
                rotation=90,
                length=1.27,
            ),
        ],
    )


def _make_default_gnd_lib_sym(name: str) -> LibrarySymbol:
    """Create a minimal power ground symbol for embedding.

    This creates a standard ground power symbol with a single
    power_in pin at (0, 0).
    """
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


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Add a bypass/decoupling capacitor to an IC power pin",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("schematic", help="Path to .kicad_sch file")
    parser.add_argument(
        "--ref", required=True, help="Target IC reference designator (e.g., U8)"
    )
    parser.add_argument(
        "--pin", required=True, help="Target pin number on that IC (e.g., 4)"
    )
    parser.add_argument(
        "--value", default="100nF", help="Capacitor value (default: 100nF)"
    )
    parser.add_argument(
        "--ground-net",
        default="GND",
        help="Ground power symbol name (default: GND)",
    )
    parser.add_argument(
        "--footprint",
        default="Capacitor_SMD:C_0402_1005Metric",
        help="Capacitor footprint (default: Capacitor_SMD:C_0402_1005Metric)",
    )
    parser.add_argument(
        "--reference", default=None, help="Capacitor reference (auto-assigned if omitted)"
    )
    parser.add_argument(
        "--offset",
        type=float,
        default=5.08,
        help="Distance from pin to cap body centre in mm (default: 5.08)",
    )
    parser.add_argument(
        "--dry-run", "-n", action="store_true", help="Preview without modifying"
    )
    parser.add_argument(
        "--backup", action="store_true", help="Create backup before modifying"
    )

    args = parser.parse_args(argv)
    return run_add_bypass_cap(args)


if __name__ == "__main__":
    sys.exit(main())
