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
from kicad_tools.schema.library import LibrarySymbol, SymbolLibrary


@dataclass
class PlannedAction:
    """An action the command will take, for reporting."""

    kind: str  # "symbol", "power", "wire", "junction", "embed"
    description: str


def _snap(value: float, grid: float = 1.27) -> float:
    """Snap a coordinate to the nearest grid point."""
    return round(value / grid) * grid


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
    if sch.lib_symbols:
        for sym_sexp in sch.lib_symbols.find_all("symbol"):
            sym_name = sym_sexp.get_string(0) or ""
            if sym_name:
                lib_sym_obj = LibrarySymbol.from_sexp(sym_sexp)
                if ":" in sym_name:
                    lib_name = sym_name.split(":")[0]
                    sym_short = sym_name.split(":", 1)[1]
                    if lib_name not in lib_manager.libraries:
                        lib_manager.libraries[lib_name] = SymbolLibrary(
                            path="", symbols={}
                        )
                    lib_manager.libraries[lib_name].symbols[sym_short] = lib_sym_obj

    # Also load embedded via the standard API
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
    """Return 'R?' for unannotated auto-assignment."""
    return "R?"


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

    if resistor_lib_sym is None and not need_embed_resistor:
        # Try from embedded
        lib_sym_sexp = sch.get_lib_symbol(resistor_lib_id)
        if lib_sym_sexp is not None:
            resistor_lib_sym = LibrarySymbol.from_sexp(lib_sym_sexp)

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
        power_rotation = 0.0

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

    # --- Plan actions ---
    planned: list[PlannedAction] = []

    # Embed resistor symbol if needed
    if need_embed_resistor:
        planned.append(
            PlannedAction("embed", f"Embed library definition for {resistor_lib_id}")
        )

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
        planned.append(
            PlannedAction("embed", f"Embed library definition for {power_lib_id}")
        )

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

    # Wire from IC pin to near resistor pin
    planned.append(
        PlannedAction(
            "wire",
            f"Wire from IC pin ({pin_x:.2f}, {pin_y:.2f})"
            f" to resistor at ({ic_side_pin[0]:.2f}, {ic_side_pin[1]:.2f})",
        )
    )

    # Wire from far resistor pin to power symbol
    planned.append(
        PlannedAction(
            "wire",
            f"Wire from resistor at ({power_side_pin[0]:.2f}, {power_side_pin[1]:.2f})"
            f" to power symbol at ({power_x:.2f}, {power_y:.2f})",
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
        backup_path = (
            f"{schematic_path}.backup-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
        )
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

    # 2. Place the resistor
    sch.add_symbol(
        lib_id=resistor_lib_id,
        reference=reference,
        value=value,
        footprint=footprint,
        position=res_position,
        rotation=res_rotation,
    )

    # 3. Place the power/ground symbol
    sch.add_power(power_net, power_position, power_rotation)

    # 4. Add wires
    # Wire from IC pin to near resistor pin
    ic_pin_snapped = (_snap(pin_x), _snap(pin_y))
    ic_side_snapped = (_snap(ic_side_pin[0]), _snap(ic_side_pin[1]))
    power_side_snapped = (_snap(power_side_pin[0]), _snap(power_side_pin[1]))
    power_pos_snapped = (_snap(power_x), _snap(power_y))

    # Only add wire if start != end
    if abs(ic_pin_snapped[0] - ic_side_snapped[0]) > 0.01 or abs(
        ic_pin_snapped[1] - ic_side_snapped[1]
    ) > 0.01:
        sch.add_wire(ic_pin_snapped, ic_side_snapped)

    if abs(power_side_snapped[0] - power_pos_snapped[0]) > 0.01 or abs(
        power_side_snapped[1] - power_pos_snapped[1]
    ) > 0.01:
        sch.add_wire(power_side_snapped, power_pos_snapped)

    # 5. Save
    sch.save()

    print(f"\nPull-{direction} resistor placed successfully")
    print(f"  Reference: {reference}")
    print(f"  Value: {value}")
    print(f"  Position: ({res_x:.2f}, {res_y:.2f})")
    print(f"  Power net: {power_net}")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Add a pull-up or pull-down resistor to a schematic pin",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("schematic", help="Path to .kicad_sch file")
    parser.add_argument(
        "--ref", required=True, help="Symbol reference of target IC (e.g., U5)"
    )
    parser.add_argument(
        "--pin", required=True, help="Pin number on target IC (e.g., 25)"
    )
    parser.add_argument(
        "--direction",
        required=True,
        choices=["up", "down"],
        help="Pull-up (power) or pull-down (ground)",
    )
    parser.add_argument(
        "--value", required=True, help="Resistor value (e.g., 10k)"
    )
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
    parser.add_argument(
        "--lib-path", action="append", dest="lib_paths", help="Library search path"
    )
    parser.add_argument(
        "--lib", action="append", dest="libs", help="Specific library file"
    )
    parser.add_argument(
        "--dry-run", "-n", action="store_true", help="Preview without modifying"
    )
    parser.add_argument(
        "--backup", action="store_true", help="Create backup before modifying"
    )
    parser.add_argument(
        "--force", action="store_true", help="Place even if collision detected"
    )

    args = parser.parse_args(argv)
    return run_add_pull_resistor(args)


if __name__ == "__main__":
    sys.exit(main())
