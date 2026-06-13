#!/usr/bin/env python3
"""
Break a wire and insert a component inline in a signal path.

This is a composite command that:
1. Finds the target wire (by endpoints or proximity)
2. Resolves the symbol's pin geometry from the library
3. Optionally expands the gap if the wire is too short for the component
4. Removes the original wire
5. Places the component at the midpoint
6. Adds two new wire segments reconnecting each side

Usage:
    kct sch insert-inline board.kicad_sch --lib-id "Device:D" \\
        --value BAT54 --footprint "Diode_SMD:D_SOD-323" --reference D1 \\
        --from 100 50 --to 120 50
    kct sch insert-inline board.kicad_sch --lib-id "Device:D" \\
        --value BAT54 --footprint "Diode_SMD:D_SOD-323" --reference D1 \\
        --near 110 50 --expand-gap
    kct sch insert-inline board.kicad_sch --lib-id "Device:D" \\
        --value BAT54 --footprint "Diode_SMD:D_SOD-323" --reference D1 \\
        --from 100 50 --to 120 50 --dry-run

Options:
    --lib-id <lib_id>      Library symbol identifier (e.g., Device:D)
    --reference <ref>      Symbol reference designator (e.g., D1)
    --value <value>        Component value (e.g., BAT54)
    --footprint <fp>       Footprint name
    --from X Y             Start endpoint of the target wire
    --to X Y               End endpoint of the target wire
    --near X Y             Find target wire nearest to this point
    --pin-a <pin>          Upstream pin number (default: 1)
    --pin-b <pin>          Downstream pin number (default: 2)
    --rotation <degrees>   Force symbol rotation (auto-detected if omitted)
    --expand-gap           Shift downstream geometry if gap is too small
    --lib-path <path>      Library search path (repeatable)
    --lib <file>           Specific library file (repeatable)
    --dry-run              Preview without modifying
    --backup               Create timestamped backup before writing
"""

from __future__ import annotations

import argparse
import math
import re
import shutil
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from kicad_tools.exceptions import FileNotFoundError as KiCadFileNotFoundError
from kicad_tools.schema import LibraryManager, Schematic
from kicad_tools.schema.library import LibrarySymbol

from .sch_remove_wire import (
    _wire_start_end,
    find_nearest_wire,
    find_wire_by_endpoints,
    remove_wire_and_orphan_junctions,
)


@dataclass
class PlannedAction:
    """An action the command will take, for reporting."""

    kind: str  # "remove-wire", "symbol", "wire", "shift", "embed"
    description: str


def _snap(value: float, grid: float = 1.27) -> float:
    """Snap a coordinate to the nearest grid point."""
    return round(value / grid) * grid


def _auto_reference(sch: Schematic, prefix: str) -> str:
    """Find the next available reference designator for a given prefix."""
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


def _is_axis_aligned(
    start: tuple[float, float], end: tuple[float, float], tolerance: float = 0.1
) -> bool:
    """Check if a wire segment is horizontal or vertical."""
    return abs(start[0] - end[0]) < tolerance or abs(start[1] - end[1]) < tolerance


def _is_horizontal(
    start: tuple[float, float], end: tuple[float, float], tolerance: float = 0.1
) -> bool:
    """Check if a wire segment is horizontal."""
    return abs(start[1] - end[1]) < tolerance


def _wire_length(start: tuple[float, float], end: tuple[float, float]) -> float:
    """Compute Euclidean length of a wire segment."""
    return math.hypot(end[0] - start[0], end[1] - start[1])


def _resolve_lib_symbol(
    sch: Schematic,
    lib_id: str,
    lib_manager: LibraryManager,
) -> tuple[LibrarySymbol | None, bool]:
    """Resolve a library symbol, returning (lib_sym, need_embed).

    If the symbol is already embedded in the schematic, returns (parsed, False).
    Otherwise tries the LibraryManager and returns (lib_sym, True).
    Returns (None, False) if not found anywhere.
    """
    existing_sexp = sch.get_lib_symbol(lib_id)
    if existing_sexp is not None:
        return LibrarySymbol.from_sexp(existing_sexp), False

    lib_sym = lib_manager.get_symbol(lib_id)
    if lib_sym is not None:
        return lib_sym, True

    return None, False


def _compute_pin_span(
    lib_sym: LibrarySymbol,
    pin_a: str,
    pin_b: str,
    rotation: float,
) -> float | None:
    """Compute the distance between two pins at a given rotation.

    Places the symbol at the origin with the given rotation and returns
    the Euclidean distance between pin_a and pin_b.  Returns None if
    either pin cannot be resolved.
    """
    positions = lib_sym.get_all_pin_positions(
        instance_pos=(0, 0),
        instance_rot=rotation,
    )
    pos_a = positions.get(pin_a)
    pos_b = positions.get(pin_b)
    if pos_a is None or pos_b is None:
        return None
    return math.hypot(pos_b[0] - pos_a[0], pos_b[1] - pos_a[1])


def _auto_rotation_for_wire(
    start: tuple[float, float],
    end: tuple[float, float],
    lib_sym: LibrarySymbol,
    pin_a: str,
    pin_b: str,
) -> float:
    """Determine the rotation that aligns pin_a -> pin_b along the wire direction.

    For a 2-pin KiCad symbol at rotation=0 the pins are typically vertical
    (e.g. Device:D has pin 1 at y=+1.27 and pin 2 at y=-1.27).
    For a horizontal wire we need to rotate the symbol 90 degrees (or 270).
    For a vertical wire we keep rotation 0.

    Returns the best rotation from {0, 90, 180, 270}.
    """
    horiz = _is_horizontal(start, end)

    # Compute pin axis at rotation 0
    positions_0 = lib_sym.get_all_pin_positions(
        instance_pos=(0, 0),
        instance_rot=0,
    )
    pa = positions_0.get(pin_a)
    pb = positions_0.get(pin_b)
    if pa is None or pb is None:
        return 90 if horiz else 0

    # Determine the dominant axis of pin_a -> pin_b at rot=0
    dx = abs(pb[0] - pa[0])
    dy = abs(pb[1] - pa[1])
    pins_horizontal_at_0 = dx > dy

    if horiz:
        # Wire is horizontal -- we need pins to be horizontal
        return 0 if pins_horizontal_at_0 else 90
    else:
        # Wire is vertical -- we need pins to be vertical
        return 90 if pins_horizontal_at_0 else 0


def _shift_downstream_wires(
    sch: Schematic,
    anchor: tuple[float, float],
    direction: tuple[float, float],
    delta: float,
    tolerance: float = 0.1,
) -> int:
    """Shift all wire endpoints at *anchor* by *delta* along *direction*.

    Walks the connectivity chain from *anchor* and shifts every endpoint
    at the anchor position.  Returns the number of endpoints shifted.

    Only shifts the immediate connected endpoints, not a deep graph walk,
    to keep behaviour predictable.
    """
    dx = direction[0] * delta
    dy = direction[1] * delta
    shifted = 0

    for wire in sch.wires:
        for attr in ("start", "end"):
            pt = getattr(wire, attr)
            if abs(pt[0] - anchor[0]) < tolerance and abs(pt[1] - anchor[1]) < tolerance:
                new_pt = (_snap(pt[0] + dx), _snap(pt[1] + dy))
                setattr(wire, attr, new_pt)
                shifted += 1

    return shifted


def run_insert_inline(args) -> int:
    """Execute the insert-inline command."""
    schematic_path = Path(args.schematic)

    # --- Validate mutual exclusivity of --from/--to vs --near ---
    has_endpoints = args.from_pt is not None and args.to_pt is not None
    has_near = args.near is not None

    if has_endpoints and has_near:
        print(
            "Error: --from/--to and --near are mutually exclusive",
            file=sys.stderr,
        )
        return 1
    if not has_endpoints and not has_near:
        print(
            "Error: Either --from and --to, or --near must be specified",
            file=sys.stderr,
        )
        return 1
    if (args.from_pt is not None) != (args.to_pt is not None):
        print("Error: --from and --to must be used together", file=sys.stderr)
        return 1

    # --- Load schematic ---
    try:
        sch = Schematic.load(schematic_path)
    except (FileNotFoundError, KiCadFileNotFoundError):
        print(f"Error: Schematic not found: {schematic_path}", file=sys.stderr)
        return 1

    # --- Find the target wire ---
    if has_endpoints:
        wire_sexp = find_wire_by_endpoints(sch, tuple(args.from_pt), tuple(args.to_pt))
    else:
        wire_sexp = find_nearest_wire(sch, tuple(args.near))

    if wire_sexp is None:
        if has_endpoints:
            print(
                f"Error: No wire found matching endpoints "
                f"({args.from_pt[0]:.2f}, {args.from_pt[1]:.2f}) -> "
                f"({args.to_pt[0]:.2f}, {args.to_pt[1]:.2f})",
                file=sys.stderr,
            )
        else:
            print("Error: No wires found in schematic", file=sys.stderr)
        return 1

    wire_start, wire_end = _wire_start_end(wire_sexp)

    # --- Validate axis-aligned ---
    if not _is_axis_aligned(wire_start, wire_end):
        print(
            "Error: Only axis-aligned (horizontal or vertical) wires are supported. "
            f"Wire ({wire_start[0]:.2f}, {wire_start[1]:.2f}) -> "
            f"({wire_end[0]:.2f}, {wire_end[1]:.2f}) is diagonal.",
            file=sys.stderr,
        )
        return 1

    # --- Resolve library symbol ---
    lib_manager = LibraryManager()
    if args.lib_paths:
        for lp in args.lib_paths:
            lib_manager.add_search_path(lp)
    if args.libs:
        for lib_file in args.libs:
            lib_manager.load_library(lib_file)

    # Also register symbols already embedded in the schematic
    if sch.lib_symbols:
        for sym_sexp in sch.lib_symbols.find_all("symbol"):
            sym_name = sym_sexp.get_string(0) or ""
            if sym_name and ":" in sym_name:
                lib_sym_obj = LibrarySymbol.from_sexp(sym_sexp)
                lib_name = sym_name.split(":")[0]
                sym_short = sym_name.split(":", 1)[1]
                from kicad_tools.schema.library import SymbolLibrary

                if lib_name not in lib_manager.libraries:
                    lib_manager.libraries[lib_name] = SymbolLibrary(path="", symbols={})
                lib_manager.libraries[lib_name].symbols[sym_short] = lib_sym_obj

    lib_sym, need_embed = _resolve_lib_symbol(sch, args.lib_id, lib_manager)
    if lib_sym is None:
        print(
            f"Error: Library symbol '{args.lib_id}' not found. "
            "Use --lib-path or --lib to specify library sources.",
            file=sys.stderr,
        )
        return 1

    pin_a = args.pin_a
    pin_b = args.pin_b

    # Validate pins exist
    available_pins = sorted(p.number for p in lib_sym.pins)
    if pin_a not in available_pins:
        print(
            f"Error: Pin '{pin_a}' not found on {args.lib_id}. "
            f"Available: {', '.join(available_pins)}",
            file=sys.stderr,
        )
        return 1
    if pin_b not in available_pins:
        print(
            f"Error: Pin '{pin_b}' not found on {args.lib_id}. "
            f"Available: {', '.join(available_pins)}",
            file=sys.stderr,
        )
        return 1

    # --- Determine rotation ---
    if args.rotation is not None:
        rotation = args.rotation
    else:
        rotation = _auto_rotation_for_wire(wire_start, wire_end, lib_sym, pin_a, pin_b)

    # --- Compute pin span ---
    pin_span = _compute_pin_span(lib_sym, pin_a, pin_b, rotation)
    if pin_span is None or pin_span < 0.01:
        print(
            f"Error: Cannot compute pin span for pins {pin_a}/{pin_b} on {args.lib_id}",
            file=sys.stderr,
        )
        return 1

    wire_len = _wire_length(wire_start, wire_end)
    gap_deficit = pin_span - wire_len

    # --- Check if gap expansion is needed ---
    if gap_deficit > 0.01 and not args.expand_gap:
        print(
            f"Error: Wire length ({wire_len:.2f} mm) is shorter than component pin span "
            f"({pin_span:.2f} mm). Use --expand-gap to automatically shift downstream geometry.",
            file=sys.stderr,
        )
        return 1

    # --- Compute placement geometry ---
    # Wire direction unit vector (from start to end)
    if wire_len > 0.01:
        dir_x = (wire_end[0] - wire_start[0]) / wire_len
        dir_y = (wire_end[1] - wire_start[1]) / wire_len
    else:
        dir_x, dir_y = 1.0, 0.0

    # After potential gap expansion, the effective wire endpoints are:
    effective_start = wire_start
    if gap_deficit > 0.01:
        # Expand by shifting the end point outward
        effective_end = (
            _snap(wire_end[0] + dir_x * gap_deficit),
            _snap(wire_end[1] + dir_y * gap_deficit),
        )
    else:
        effective_end = wire_end

    _wire_length(effective_start, effective_end)

    # Component placement at midpoint
    mid_x = _snap((effective_start[0] + effective_end[0]) / 2)
    mid_y = _snap((effective_start[1] + effective_end[1]) / 2)
    comp_pos = (mid_x, mid_y)

    # Compute pin positions with the component placed at the midpoint
    pin_positions = lib_sym.get_all_pin_positions(
        instance_pos=comp_pos,
        instance_rot=rotation,
    )
    pin_a_pos = pin_positions.get(pin_a)
    pin_b_pos = pin_positions.get(pin_b)
    if pin_a_pos is None or pin_b_pos is None:
        print(
            "Error: Cannot resolve pin positions after placement",
            file=sys.stderr,
        )
        return 1

    pin_a_pos = (_snap(pin_a_pos[0]), _snap(pin_a_pos[1]))
    pin_b_pos = (_snap(pin_b_pos[0]), _snap(pin_b_pos[1]))

    # Assign pins to upstream/downstream: pin_a connects to wire start,
    # pin_b connects to wire end.  If pin_a is actually farther from
    # start than pin_b, swap them.
    dist_a_to_start = math.hypot(
        pin_a_pos[0] - effective_start[0], pin_a_pos[1] - effective_start[1]
    )
    dist_b_to_start = math.hypot(
        pin_b_pos[0] - effective_start[0], pin_b_pos[1] - effective_start[1]
    )
    if dist_a_to_start > dist_b_to_start:
        # Swap: pin_b is closer to start
        pin_a_pos, pin_b_pos = pin_b_pos, pin_a_pos

    # Determine reference
    reference = args.reference
    if not reference:
        # Extract prefix from lib_id (e.g., "Device:D" -> "D")
        sym_name = args.lib_id.split(":")[-1] if ":" in args.lib_id else args.lib_id
        # Common prefix extraction: first letter(s)
        prefix = ""
        for ch in sym_name:
            if ch.isalpha():
                prefix += ch
            else:
                break
        if not prefix:
            prefix = sym_name[0] if sym_name else "U"
        reference = _auto_reference(sch, prefix)

    # --- Collect planned actions ---
    planned: list[PlannedAction] = []

    if gap_deficit > 0.01:
        planned.append(
            PlannedAction(
                "shift",
                f"Shift downstream endpoint ({wire_end[0]:.2f}, {wire_end[1]:.2f}) "
                f"by {gap_deficit:.2f} mm along wire direction to "
                f"({effective_end[0]:.2f}, {effective_end[1]:.2f})",
            )
        )

    planned.append(
        PlannedAction(
            "remove-wire",
            f"Remove wire ({wire_start[0]:.2f}, {wire_start[1]:.2f}) -> "
            f"({wire_end[0]:.2f}, {wire_end[1]:.2f})",
        )
    )

    if need_embed:
        planned.append(PlannedAction("embed", f"Embed library definition for {args.lib_id}"))

    planned.append(
        PlannedAction(
            "symbol",
            f"Place {args.lib_id} as {reference} (value={args.value!r},"
            f" footprint={args.footprint!r}) at ({comp_pos[0]:.2f}, {comp_pos[1]:.2f})"
            f" rotation={rotation}",
        )
    )

    # Wire from upstream (wire start) to pin_a
    if (
        abs(effective_start[0] - pin_a_pos[0]) > 0.01
        or abs(effective_start[1] - pin_a_pos[1]) > 0.01
    ):
        planned.append(
            PlannedAction(
                "wire",
                f"Wire from ({effective_start[0]:.2f}, {effective_start[1]:.2f}) "
                f"to pin {args.pin_a} at ({pin_a_pos[0]:.2f}, {pin_a_pos[1]:.2f})",
            )
        )

    # Wire from pin_b to downstream (wire end / effective_end)
    if abs(pin_b_pos[0] - effective_end[0]) > 0.01 or abs(pin_b_pos[1] - effective_end[1]) > 0.01:
        planned.append(
            PlannedAction(
                "wire",
                f"Wire from pin {args.pin_b} at ({pin_b_pos[0]:.2f}, {pin_b_pos[1]:.2f}) "
                f"to ({effective_end[0]:.2f}, {effective_end[1]:.2f})",
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

    # 1. Gap expansion: shift downstream endpoints
    if gap_deficit > 0.01:
        _shift_downstream_wires(
            sch,
            wire_end,
            (dir_x, dir_y),
            gap_deficit,
        )

    # 2. Remove the original wire
    remove_wire_and_orphan_junctions(sch, wire_sexp)

    # 3. Embed library symbol if needed
    if need_embed:
        if lib_sym.name != args.lib_id:
            lib_sym.name = args.lib_id
        sch.embed_lib_symbol(lib_sym)

    # 4. Place the component
    sch.add_symbol(
        lib_id=args.lib_id,
        reference=reference,
        value=args.value or "",
        footprint=args.footprint or "",
        position=comp_pos,
        rotation=rotation,
    )

    # 5. Add reconnection wires
    if (
        abs(effective_start[0] - pin_a_pos[0]) > 0.01
        or abs(effective_start[1] - pin_a_pos[1]) > 0.01
    ):
        sch.add_wire(effective_start, pin_a_pos)

    if abs(pin_b_pos[0] - effective_end[0]) > 0.01 or abs(pin_b_pos[1] - effective_end[1]) > 0.01:
        sch.add_wire(pin_b_pos, effective_end)

    # 6. Save
    sch.save()

    print("\nComponent inserted inline successfully:")
    print(f"  Reference: {reference}")
    print(f"  Value: {args.value}")
    print(f"  Position: ({comp_pos[0]:.2f}, {comp_pos[1]:.2f})")
    print(
        f"  Wire: ({effective_start[0]:.2f}, {effective_start[1]:.2f}) -> "
        f"({effective_end[0]:.2f}, {effective_end[1]:.2f})"
    )
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Break a wire and insert a component inline in a signal path",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("schematic", help="Path to .kicad_sch file")
    parser.add_argument("--lib-id", required=True, help="Library symbol ID (e.g., Device:D)")
    parser.add_argument("--reference", help="Symbol reference (e.g., D1)")
    parser.add_argument("--value", default="", help="Component value (e.g., BAT54)")
    parser.add_argument("--footprint", default="", help="Footprint name")
    parser.add_argument(
        "--from",
        nargs=2,
        type=float,
        dest="from_pt",
        metavar=("X", "Y"),
        help="Start endpoint of the target wire",
    )
    parser.add_argument(
        "--to",
        nargs=2,
        type=float,
        dest="to_pt",
        metavar=("X", "Y"),
        help="End endpoint of the target wire",
    )
    parser.add_argument(
        "--near",
        nargs=2,
        type=float,
        metavar=("X", "Y"),
        help="Find target wire nearest to this point",
    )
    parser.add_argument("--pin-a", default="1", help="Upstream pin number (default: 1)")
    parser.add_argument("--pin-b", default="2", help="Downstream pin number (default: 2)")
    parser.add_argument(
        "--rotation",
        type=float,
        default=None,
        help="Symbol rotation in degrees (auto-detected if omitted)",
    )
    parser.add_argument(
        "--expand-gap",
        action="store_true",
        help="Shift downstream geometry if wire is too short for the component",
    )
    parser.add_argument("--lib-path", action="append", dest="lib_paths", help="Library search path")
    parser.add_argument("--lib", action="append", dest="libs", help="Specific library file")
    parser.add_argument("--dry-run", "-n", action="store_true", help="Preview without modifying")
    parser.add_argument("--backup", action="store_true", help="Create backup before modifying")

    args = parser.parse_args(argv)
    return run_insert_inline(args)


if __name__ == "__main__":
    sys.exit(main())
