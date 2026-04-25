"""
Symbol manipulation operations for KiCad schematics.

Provides functions to modify, replace, and update symbol instances.
"""

from __future__ import annotations

import uuid as uuid_lib
from dataclasses import dataclass, field
from pathlib import Path

from kicad_tools.sexp import SExp, parse_string, serialize_sexp


@dataclass
class PinTypeChange:
    """Describes a change in pin electrical type between old and new symbol."""

    pin_number: str
    pin_name: str
    old_type: str
    new_type: str


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
    lib_symbol_updated: bool = False  # Whether the lib_symbols entry was updated
    pin_type_changes: list[PinTypeChange] = field(default_factory=list)


def find_symbol_by_reference(sexp: SExp, reference: str) -> SExp | None:
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
            if prop.get_string(0) == "Reference" and prop.get_string(1) == reference:
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
    new_value: str | None = None,
    new_footprint: str | None = None,
    dry_run: bool = False,
    lib_path: str | None = None,
) -> SymbolReplacement:
    """
    Replace a symbol's library ID (and optionally value/footprint).

    When *lib_path* is provided, the embedded ``lib_symbols`` entry is
    replaced with the definition from the library file and the instance
    pins are reconciled with the new definition.  Without *lib_path*
    this is a "soft" replacement that changes only the ``lib_id`` and
    selected properties while keeping the old pin definitions.

    Args:
        schematic_path: Path to the .kicad_sch file
        reference: Symbol reference to replace (e.g., "U1")
        new_lib_id: New library ID (e.g., "chorus-revA:TPA3116D2")
        new_value: Optional new value for the symbol
        new_footprint: Optional new footprint
        dry_run: If True, don't write changes
        lib_path: Optional path to the .kicad_sym library file
            containing the new symbol.  When provided the embedded
            ``lib_symbols`` definition and instance pins are updated.

    Returns:
        SymbolReplacement with details of changes

    Raises:
        FileNotFoundError: If schematic or library doesn't exist
        ValueError: If symbol not found in schematic or library
    """
    from kicad_tools.schema.library import LibrarySymbol, SymbolLibrary
    from kicad_tools.schema.schematic import Schematic

    path = Path(schematic_path)
    if not path.exists():
        raise FileNotFoundError(f"Schematic not found: {schematic_path}")

    # Parse the schematic
    text = path.read_text()
    sexp = parse_string(text)

    # Find the symbol
    symbol = find_symbol_by_reference(sexp, reference)
    if not symbol:
        raise ValueError(f"Symbol '{reference}' not found in schematic")

    # Get current values
    old_lib_id = get_symbol_lib_id(symbol)
    old_pins = get_symbol_pins(symbol)

    changes = []
    preserved = []
    lib_symbol_updated = False
    pin_type_changes: list[PinTypeChange] = []

    # Update lib_id
    lib_id_node = symbol.find("lib_id")
    if lib_id_node:
        lib_id_node.set_value(0, new_lib_id)
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

    # Update lib_symbols entry and reconcile instance pins
    new_pin_count = len(old_pins)
    if lib_path:
        lib_file = Path(lib_path)
        if not lib_file.exists():
            raise FileNotFoundError(f"Library not found: {lib_path}")

        lib = SymbolLibrary.load(str(lib_file))

        # The new_lib_id is typically "LibName:SymbolName".  The symbol
        # is stored in the library under the full qualified name.  Some
        # libraries use just the short name as the key.
        new_lib_sym = lib.get_symbol(new_lib_id)
        if new_lib_sym is None:
            # Try the short name (part after colon)
            short_name = new_lib_id.split(":", 1)[1] if ":" in new_lib_id else new_lib_id
            new_lib_sym = lib.get_symbol(short_name)
        if new_lib_sym is None:
            raise ValueError(
                f"Symbol '{new_lib_id}' not found in library '{lib_path}'"
            )

        # Compare old and new pin types for reporting
        old_lib_sym_sexp = _find_lib_symbol_sexp(sexp, old_lib_id)
        if old_lib_sym_sexp is not None:
            old_lib_sym = LibrarySymbol.from_sexp(old_lib_sym_sexp)
            old_pin_map = {p.number: p for p in old_lib_sym.pins}
            for new_pin in new_lib_sym.pins:
                old_pin = old_pin_map.get(new_pin.number)
                if old_pin and old_pin.type != new_pin.type:
                    pin_type_changes.append(
                        PinTypeChange(
                            pin_number=new_pin.number,
                            pin_name=new_pin.name,
                            old_type=old_pin.type,
                            new_type=new_pin.type,
                        )
                    )

        # Replace the lib_symbols entry.  We need to rename the new
        # library symbol to match the lib_id used in the schematic so
        # that KiCad can resolve the reference.
        renamed_sym = LibrarySymbol(
            name=new_lib_id,
            properties=new_lib_sym.properties,
            pins=new_lib_sym.pins,
            graphics=new_lib_sym.graphics,
            units=new_lib_sym.units,
        )

        # Use Schematic helper to swap the lib_symbols entry
        sch = Schematic.__new__(Schematic)
        sch._sexp = sexp
        sch.replace_lib_symbol(old_lib_id, renamed_sym)
        lib_symbol_updated = True
        changes.append(f"lib_symbols: replaced embedded definition for {old_lib_id}")

        # Reconcile instance pins with the new library definition
        new_pin_numbers = {p.number for p in new_lib_sym.pins}
        old_instance_pins = {p.get_string(0) for p in old_pins}

        # Remove instance pins that don't exist in the new symbol
        for pin in list(old_pins):
            pin_num = pin.get_string(0)
            if pin_num not in new_pin_numbers:
                symbol.remove(pin)
                changes.append(f"Removed instance pin {pin_num} (not in new symbol)")

        # Add instance pins that exist in new symbol but not in instance
        for pin in new_lib_sym.pins:
            if pin.number not in old_instance_pins:
                add_symbol_pin(symbol, pin.number)
                changes.append(f"Added instance pin {pin.number} (new in replacement symbol)")

        new_pin_count = len(get_symbol_pins(symbol))

    result = SymbolReplacement(
        reference=reference,
        old_lib_id=old_lib_id,
        new_lib_id=new_lib_id,
        old_pin_count=len(old_pins),
        new_pin_count=new_pin_count,
        preserved_properties=preserved,
        changes_made=changes,
        lib_symbol_updated=lib_symbol_updated,
        pin_type_changes=pin_type_changes,
    )

    # Write back if not dry run
    if not dry_run:
        new_text = serialize_sexp(sexp)
        path.write_text(new_text)

    return result


def _find_lib_symbol_sexp(sexp: SExp, lib_id: str) -> SExp | None:
    """Find a library symbol definition in the schematic's lib_symbols section."""
    lib_syms = sexp.find("lib_symbols")
    if lib_syms is None:
        return None
    for sym in lib_syms.find_all("symbol"):
        if sym.get_string(0) == lib_id:
            return sym
    return None


def update_symbol_pins(
    symbol: SExp,
    pin_mapping: dict[str, str],
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
    template_sexp = parse_string(template_text)

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
