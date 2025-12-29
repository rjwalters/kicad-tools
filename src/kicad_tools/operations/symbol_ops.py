"""
Symbol manipulation operations for KiCad schematics.

Provides functions to modify, replace, and update symbol instances.
"""

from __future__ import annotations

import uuid as uuid_lib
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional

from ..core.sexp import SExp, parse_sexp, serialize_sexp


@dataclass
class SymbolReplacement:
    """Details of a symbol replacement operation."""

    reference: str
    old_lib_id: str
    new_lib_id: str
    old_pin_count: int
    new_pin_count: int
    preserved_properties: list[str]  # Properties that were kept
    changes_made: list[str]  # List of changes made


def find_symbol_by_reference(sexp: SExp, reference: str) -> Optional[SExp]:
    """
    Find a symbol S-expression by its reference designator.

    Args:
        sexp: The schematic S-expression
        reference: The reference to find (e.g., "U1")

    Returns:
        The symbol SExp if found, None otherwise
    """
    for symbol in sexp.find_all("symbol"):
        for prop in symbol.find_all("property"):
            if prop.get_string(0) == "Reference":
                if prop.get_string(1) == reference:
                    return symbol
    return None


def get_symbol_lib_id(symbol: SExp) -> str:
    """Get the lib_id from a symbol."""
    lib_id = symbol.find("lib_id")
    if lib_id:
        return lib_id.get_string(0) or ""
    return ""


def get_symbol_pins(symbol: SExp) -> list[SExp]:
    """Get all pins from a symbol."""
    return symbol.find_all("pin")


def replace_symbol_lib_id(
    schematic_path: str,
    reference: str,
    new_lib_id: str,
    new_value: Optional[str] = None,
    new_footprint: Optional[str] = None,
    dry_run: bool = False,
) -> SymbolReplacement:
    """
    Replace a symbol's library ID (and optionally value/footprint).

    This is a "soft" replacement that changes the lib_id but keeps
    the existing pin connections. Use this when the new symbol has
    compatible pinout or when you'll rewire manually.

    Args:
        schematic_path: Path to the .kicad_sch file
        reference: Symbol reference to replace (e.g., "U1")
        new_lib_id: New library ID (e.g., "chorus-revA:TPA3116D2")
        new_value: Optional new value for the symbol
        new_footprint: Optional new footprint
        dry_run: If True, don't write changes

    Returns:
        SymbolReplacement with details of changes

    Raises:
        FileNotFoundError: If schematic doesn't exist
        ValueError: If symbol not found
    """
    path = Path(schematic_path)
    if not path.exists():
        raise FileNotFoundError(f"Schematic not found: {schematic_path}")

    # Parse the schematic
    text = path.read_text()
    sexp = parse_sexp(text)

    # Find the symbol
    symbol = find_symbol_by_reference(sexp, reference)
    if not symbol:
        raise ValueError(f"Symbol '{reference}' not found in schematic")

    # Get current values
    old_lib_id = get_symbol_lib_id(symbol)
    old_pins = get_symbol_pins(symbol)

    changes = []
    preserved = []

    # Update lib_id
    lib_id_node = symbol.find("lib_id")
    if lib_id_node and lib_id_node.values:
        lib_id_node.values[0] = new_lib_id
        changes.append(f"lib_id: {old_lib_id} → {new_lib_id}")

    # Update properties
    for prop in symbol.find_all("property"):
        name = prop.get_string(0)
        if name == "Value" and new_value:
            old_val = prop.get_string(1)
            prop.set_value(1, new_value)
            changes.append(f"Value: {old_val} → {new_value}")
        elif name == "Footprint" and new_footprint:
            old_fp = prop.get_string(1)
            prop.set_value(1, new_footprint)
            changes.append(f"Footprint: {old_fp} → {new_footprint}")
        else:
            preserved.append(name)

    result = SymbolReplacement(
        reference=reference,
        old_lib_id=old_lib_id,
        new_lib_id=new_lib_id,
        old_pin_count=len(old_pins),
        new_pin_count=len(old_pins),  # Pins not changed in soft replace
        preserved_properties=preserved,
        changes_made=changes,
    )

    # Write back if not dry run
    if not dry_run:
        new_text = serialize_sexp(sexp)
        path.write_text(new_text)

    return result


def update_symbol_pins(
    symbol: SExp,
    pin_mapping: Dict[str, str],
) -> list[str]:
    """
    Update symbol pin numbers based on a mapping.

    Args:
        symbol: The symbol S-expression
        pin_mapping: Dict mapping old pin number -> new pin number

    Returns:
        List of changes made
    """
    changes = []

    for pin in symbol.find_all("pin"):
        old_num = pin.get_string(0)
        if old_num in pin_mapping:
            new_num = pin_mapping[old_num]
            pin.set_value(0, new_num)
            changes.append(f"Pin {old_num} → {new_num}")

            # Generate new UUID for the pin
            uuid_node = pin.find("uuid")
            if uuid_node:
                uuid_node.set_value(0, str(uuid_lib.uuid4()))

    return changes


def clear_symbol_pins(symbol: SExp) -> int:
    """
    Remove all pins from a symbol.

    Args:
        symbol: The symbol S-expression

    Returns:
        Number of pins removed
    """
    count = 0
    while True:
        if symbol.remove_child("pin"):
            count += 1
        else:
            break
    return count


def add_symbol_pin(symbol: SExp, pin_number: str) -> None:
    """
    Add a pin to a symbol.

    Args:
        symbol: The symbol S-expression
        pin_number: The pin number to add
    """
    pin = SExp("pin")
    pin.add(pin_number)
    pin.add(SExp("uuid").add(str(uuid_lib.uuid4())))
    symbol.add(pin)


def create_replacement_symbol(
    template_path: str,
    position: tuple[float, float],
    rotation: float = 0,
    reference: str = "U?",
    value: str = "",
    footprint: str = "",
    unit: int = 1,
) -> SExp:
    """
    Create a new symbol instance from a template library symbol.

    Args:
        template_path: Path to .kicad_sym file containing the symbol
        position: (x, y) position in schematic
        rotation: Rotation in degrees
        reference: Reference designator
        value: Value for the symbol
        footprint: Footprint assignment
        unit: Symbol unit (for multi-unit symbols)

    Returns:
        A new symbol SExp ready to be added to a schematic
    """
    # Read template
    template_text = Path(template_path).read_text()
    template_sexp = parse_sexp(template_text)

    # Find the symbol definition
    sym_def = None
    for sym in template_sexp.find_all("symbol"):
        sym_def = sym
        break

    if not sym_def:
        raise ValueError(f"No symbol found in {template_path}")

    # Get lib_id from the template
    lib_id = sym_def.get_string(0)  # First value after 'symbol' tag is the name

    # Create instance
    instance = SExp("symbol")
    instance.add(SExp("lib_id").add(lib_id))
    instance.add(SExp("at").add(position[0]).add(position[1]).add(rotation))
    instance.add(SExp("unit").add(unit))
    instance.add(SExp("in_bom").add("yes"))
    instance.add(SExp("on_board").add("yes"))
    instance.add(SExp("dnp").add("no"))
    instance.add(SExp("uuid").add(str(uuid_lib.uuid4())))

    # Add properties
    def add_property(name: str, val: str, x_off: float = 0, y_off: float = 0):
        prop = SExp("property")
        prop.add(name)
        prop.add(val)
        prop.add(SExp("at").add(position[0] + x_off).add(position[1] + y_off).add(0))
        prop.add(SExp("effects").add(SExp("font").add(SExp("size").add(1.27).add(1.27))))
        instance.add(prop)

    add_property("Reference", reference, 0, -5)
    add_property("Value", value or lib_id, 0, -2.5)
    add_property("Footprint", footprint, 0, 0)
    add_property("Datasheet", "~", 0, 0)

    # Add pins (from template)
    for sym_unit in sym_def.find_all("symbol"):
        for pin in sym_unit.find_all("pin"):
            if number_node := pin.find("number"):
                pin_num = number_node.get_string(0)
                if pin_num:
                    add_symbol_pin(instance, pin_num)

    return instance
