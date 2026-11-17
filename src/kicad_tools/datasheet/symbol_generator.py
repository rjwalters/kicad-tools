"""
Symbol generator from datasheet data.

Automatically generates KiCad symbols from extracted datasheet information,
with intelligent pin placement and property population.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from .pin_layout import LayoutStyle, PinLayoutEngine, SymbolLayout
from .pins import ExtractedPin, PinTable

if TYPE_CHECKING:
    from kicad_tools.schema.library import LibrarySymbol, SymbolLibrary


@dataclass
class GeneratedPin:
    """A pin in the generated symbol."""

    number: str
    name: str
    type: str
    x: float
    y: float
    rotation: float
    length: float = 2.54
    unit: int = 1


@dataclass
class GeneratedSymbol:
    """Result of symbol generation."""

    name: str
    pins: list[GeneratedPin]
    properties: dict[str, str]
    layout: SymbolLayout
    source_datasheet: str
    generation_confidence: float
    units: int = 1


class SymbolGenerator:
    """
    Generator for KiCad symbols from datasheet data.

    Usage:
        from kicad_tools.datasheet import DatasheetParser
        from kicad_tools.datasheet.symbol_generator import SymbolGenerator

        parser = DatasheetParser("STM32F103.pdf")
        pins = parser.extract_pins(package="LQFP48")

        generator = SymbolGenerator()
        symbol = generator.generate(
            name="STM32F103C8T6",
            pins=pins,
            layout="functional",
        )
    """

    def __init__(
        self,
        pin_length: float = 2.54,
        pin_spacing: float = 2.54,
    ):
        """
        Initialize the symbol generator.

        Args:
            pin_length: Length of pin lines in mm (default 2.54)
            pin_spacing: Vertical spacing between pins in mm (default 2.54)
        """
        self.pin_length = pin_length
        self.pin_spacing = pin_spacing
        self.layout_engine = PinLayoutEngine(
            pin_spacing=pin_spacing,
            pin_length=pin_length,
        )

    def generate(
        self,
        name: str,
        pins: PinTable | list[ExtractedPin],
        layout: str | LayoutStyle = LayoutStyle.FUNCTIONAL,
        datasheet_url: str = "",
        manufacturer: str = "",
        description: str = "",
        footprint: str = "",
        properties: dict[str, str] | None = None,
        detect_multi_unit: bool = True,
    ) -> GeneratedSymbol:
        """
        Generate a symbol from extracted pin data.

        Args:
            name: Symbol name (e.g., "STM32F103C8T6")
            pins: PinTable or list of ExtractedPin from datasheet parsing
            layout: Pin layout style ("functional", "physical", "simple")
            datasheet_url: URL to the component datasheet
            manufacturer: Component manufacturer
            description: Component description
            footprint: KiCad footprint reference (e.g., "Package_QFP:LQFP-48")
            properties: Additional properties to set
            detect_multi_unit: If True, detect and create multi-unit symbols

        Returns:
            GeneratedSymbol with calculated pin positions
        """
        # Normalize pins to list
        pin_list = pins.pins if isinstance(pins, PinTable) else pins

        # Detect multi-unit if requested
        units = 1
        unit_assignments: dict[str, int] = {}
        if detect_multi_unit:
            unit_groups = self.layout_engine.detect_multi_unit(pin_list)
            units = len(unit_groups)
            if units > 1:
                for unit_num, unit_pins in unit_groups.items():
                    for pin in unit_pins:
                        unit_assignments[pin.number] = unit_num

        # Calculate layout
        if isinstance(layout, str):
            layout = LayoutStyle(layout)

        package_pins = len(pin_list)
        if isinstance(pins, PinTable) and pins.package:
            # Try to extract pin count from package name
            import re

            match = re.search(r"(\d+)", pins.package)
            if match:
                package_pins = int(match.group(1))

        symbol_layout = self.layout_engine.calculate_layout(
            pin_list,
            style=layout,
            package_pins=package_pins,
        )
        symbol_layout.style = layout

        # Generate pins
        generated_pins: list[GeneratedPin] = []
        for pos in symbol_layout.pin_positions:
            unit = unit_assignments.get(pos.number, 1)
            generated_pins.append(
                GeneratedPin(
                    number=pos.number,
                    name=pos.name,
                    type=pos.pin_type,
                    x=pos.x,
                    y=pos.y,
                    rotation=pos.rotation,
                    length=self.pin_length,
                    unit=unit,
                )
            )

        # Build properties
        sym_properties = {
            "Reference": "U",
            "Value": name,
        }

        if footprint:
            sym_properties["Footprint"] = footprint

        if datasheet_url:
            sym_properties["Datasheet"] = datasheet_url

        if description:
            sym_properties["Description"] = description
        elif isinstance(pins, PinTable) and pins.package:
            sym_properties["Description"] = f"{name} {pins.package}"

        if manufacturer:
            sym_properties["Manufacturer"] = manufacturer
            sym_properties["MPN"] = name

        # Add custom properties
        if properties:
            sym_properties.update(properties)

        # Calculate confidence
        confidence = self._calculate_confidence(pin_list, symbol_layout)

        return GeneratedSymbol(
            name=name,
            pins=generated_pins,
            properties=sym_properties,
            layout=symbol_layout,
            source_datasheet=datasheet_url,
            generation_confidence=confidence,
            units=units,
        )

    def _calculate_confidence(
        self,
        pins: list[ExtractedPin],
        layout: SymbolLayout,
    ) -> float:
        """Calculate overall confidence score for the generated symbol."""
        if not pins:
            return 0.0

        # Average pin type confidence
        type_confidence = sum(p.type_confidence for p in pins) / len(pins)

        # Penalize if many pins are on same side (may indicate layout issues)
        positions = layout.pin_positions
        if positions:
            rotations = [p.rotation for p in positions]
            rotation_counts = {r: rotations.count(r) for r in set(rotations)}
            max_on_one_side = max(rotation_counts.values())
            balance_factor = 1.0 - (max_on_one_side / len(positions)) * 0.5
        else:
            balance_factor = 0.5

        return type_confidence * 0.7 + balance_factor * 0.3

    def add_to_library(
        self,
        library: SymbolLibrary,
        symbol: GeneratedSymbol,
    ) -> LibrarySymbol:
        """
        Add a generated symbol to a KiCad symbol library.

        Args:
            library: SymbolLibrary to add the symbol to
            symbol: GeneratedSymbol to add

        Returns:
            The created LibrarySymbol
        """
        # Create the symbol
        lib_symbol = library.create_symbol(symbol.name, units=symbol.units)

        # Add properties
        for name, value in symbol.properties.items():
            lib_symbol.add_property(name, value)

        # Add pins
        for pin in symbol.pins:
            lib_symbol.add_pin(
                number=pin.number,
                name=pin.name,
                pin_type=pin.type,
                position=(pin.x, pin.y),
                rotation=pin.rotation,
                length=pin.length,
                unit=pin.unit,
            )

        return lib_symbol


def create_symbol_from_datasheet(
    library: SymbolLibrary,
    name: str,
    pins: PinTable | list[ExtractedPin],
    layout: str | LayoutStyle = LayoutStyle.FUNCTIONAL,
    datasheet_url: str = "",
    manufacturer: str = "",
    description: str = "",
    footprint: str = "",
    properties: dict[str, str] | None = None,
    interactive: bool = False,
) -> LibrarySymbol:
    """
    Create a KiCad symbol from datasheet-extracted pins.

    This is a convenience function that creates a SymbolGenerator,
    generates the symbol, and adds it to the library.

    Args:
        library: SymbolLibrary to add the symbol to
        name: Symbol name (e.g., "STM32F103C8T6")
        pins: PinTable or list of ExtractedPin from datasheet parsing
        layout: Pin layout style ("functional", "physical", "simple")
        datasheet_url: URL to the component datasheet
        manufacturer: Component manufacturer
        description: Component description
        footprint: KiCad footprint reference
        properties: Additional properties to set
        interactive: If True, prompt for confirmation (not yet implemented)

    Returns:
        The created LibrarySymbol

    Example:
        >>> from kicad_tools.datasheet import DatasheetParser
        >>> from kicad_tools.schema.library import SymbolLibrary
        >>> from kicad_tools.datasheet.symbol_generator import create_symbol_from_datasheet
        >>>
        >>> parser = DatasheetParser("STM32F103.pdf")
        >>> pins = parser.extract_pins(package="LQFP48")
        >>>
        >>> lib = SymbolLibrary.create("myproject.kicad_sym")
        >>> sym = create_symbol_from_datasheet(
        ...     library=lib,
        ...     name="STM32F103C8T6",
        ...     pins=pins,
        ...     datasheet_url="https://example.com/stm32f103.pdf",
        ... )
        >>> lib.save()
    """
    generator = SymbolGenerator()

    generated = generator.generate(
        name=name,
        pins=pins,
        layout=layout,
        datasheet_url=datasheet_url,
        manufacturer=manufacturer,
        description=description,
        footprint=footprint,
        properties=properties,
    )

    return generator.add_to_library(library, generated)
