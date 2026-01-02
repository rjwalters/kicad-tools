"""
KiCad Schematic Exceptions

Custom exception classes for schematic operations with helpful error messages.
"""

from pathlib import Path

# Common KiCad libraries and their typical usage
# Used to provide helpful context in error messages
LIBRARY_INFO = {
    "Device": {
        "description": "Standard passive components (R, C, L, LED, etc.)",
        "common_symbols": ["R", "C", "LED", "Crystal", "D_TVS", "D_Schottky", "Q_PMOS_GSD"],
        "used_by": ["LEDIndicator", "DecouplingCaps", "CrystalOscillator", "most blocks"],
        "install_hint": "Included with KiCad installation",
    },
    "Regulator_Linear": {
        "description": "Linear voltage regulators (LDO, fixed/adjustable)",
        "common_symbols": ["AP2204K-1.5", "AP2204K-3.3", "AMS1117-3.3", "MCP1700"],
        "used_by": ["LDOBlock"],
        "install_hint": "Included with KiCad installation",
    },
    "Oscillator": {
        "description": "Oscillator modules and crystals with integrated circuits",
        "common_symbols": ["ASE-xxxMHz", "SG-210STF"],
        "used_by": ["OscillatorBlock"],
        "install_hint": "Included with KiCad installation",
    },
    "Connector": {
        "description": "USB, barrel jack, and other connectors",
        "common_symbols": [
            "USB_C_Receptacle_USB2.0",
            "USB_Micro-B",
            "USB_Mini-B",
            "USB_A",
            "Barrel_Jack_Switch",
        ],
        "used_by": ["USBConnector", "BarrelJackInput"],
        "install_hint": "Included with KiCad installation",
    },
    "Connector_Generic": {
        "description": "Generic pin headers and connectors",
        "common_symbols": ["Conn_01x02", "Conn_01x06", "Conn_02x05_Odd_Even"],
        "used_by": ["DebugHeader", "BatteryInput"],
        "install_hint": "Included with KiCad installation",
    },
    "power": {
        "description": "Power symbols (GND, VCC, +3.3V, etc.)",
        "common_symbols": ["GND", "+3.3V", "+5V", "PWR_FLAG"],
        "used_by": ["Power symbols via add_power()"],
        "install_hint": "Included with KiCad installation",
    },
}

# Alternative symbols that can be used if the default isn't found
SYMBOL_ALTERNATIVES = {
    # USB Connectors - KiCad naming varies between versions
    "USB_C_Receptacle_USB2.0": [
        "USB_C_Receptacle",
        "USB_C_Receptacle_USB2.0_16P",
        "USB_C_Plug_USB2.0",
    ],
    "USB_Micro-B": ["USB_B_Micro"],
    "USB_Mini-B": ["USB_B_Mini"],
    "USB_A": ["USB_A_Receptacle", "USB_A_Plug"],
    # LDO alternatives
    "AP2204K-1.5": ["AP2204K-3.3", "AP2204K-5.0", "AMS1117-3.3", "MCP1700-3302E_TO"],
    # Oscillator alternatives
    "ASE-xxxMHz": ["SG-210STF", "ACO-xxxMHz"],
}


class PinNotFoundError(ValueError):
    """Raised when a pin cannot be found on a symbol."""

    def __init__(
        self, pin_name: str, symbol_name: str, available_pins: list, suggestions: list[str] = None
    ):
        self.pin_name = pin_name
        self.symbol_name = symbol_name
        self.available_pins = available_pins
        self.suggestions = suggestions or []

        # Import here to avoid circular dependency
        from .helpers import _format_pin_list, _group_pins_by_type

        # Build error message
        msg_parts = [f"Pin '{pin_name}' not found on {symbol_name}"]

        if self.suggestions:
            msg_parts.append(f"\n\nDid you mean: {', '.join(self.suggestions)}?")

        # Group pins by type for organized display
        grouped = _group_pins_by_type(available_pins)
        if grouped:
            msg_parts.append("\n\nAvailable pins:")
            for group_name, pins in grouped.items():
                if pins:
                    msg_parts.append(f"\n  [{group_name}]")
                    msg_parts.append("\n" + _format_pin_list(pins, "    "))

        super().__init__("".join(msg_parts))


class SymbolNotFoundError(ValueError):
    """Raised when a symbol cannot be found in a library."""

    def __init__(
        self,
        symbol_name: str,
        library_file: str,
        available_symbols: list[str] = None,
        suggestions: list[str] = None,
    ):
        self.symbol_name = symbol_name
        self.library_file = library_file
        self.available_symbols = available_symbols or []
        self.suggestions = suggestions or []

        msg_parts = [f"Symbol '{symbol_name}' not found in {library_file}"]

        if self.suggestions:
            msg_parts.append(f"\n\nDid you mean: {', '.join(self.suggestions)}?")

        # Check for known alternative symbols
        alternatives = SYMBOL_ALTERNATIVES.get(symbol_name, [])
        if alternatives:
            # Filter to only show alternatives that are actually available
            available_alternatives = [alt for alt in alternatives if alt in self.available_symbols]
            if available_alternatives:
                msg_parts.append("\n\nAlternative symbols available in this library:")
                for alt in available_alternatives[:5]:
                    msg_parts.append(f"\n  {alt}")
            elif alternatives:
                msg_parts.append("\n\nKnown alternatives (may require different library/version):")
                for alt in alternatives[:5]:
                    msg_parts.append(f"\n  {alt}")

        # Show available symbols if no suggestions or alternatives
        if not self.suggestions and not alternatives and self.available_symbols:
            # Show first 10 available symbols
            shown = self.available_symbols[:10]
            msg_parts.append(f"\n\nAvailable symbols ({len(self.available_symbols)} total):")
            for sym in shown:
                msg_parts.append(f"\n  {sym}")
            if len(self.available_symbols) > 10:
                msg_parts.append(f"\n  ... and {len(self.available_symbols) - 10} more")

        # Add usage hint
        msg_parts.append("\n\nTip: Most circuit blocks accept a *_symbol parameter")
        msg_parts.append("\nto override the default symbol. For example:")
        msg_parts.append(
            "\n  USBConnector(sch, ..., connector_symbol='Connector:USB_C_Receptacle')"
        )

        super().__init__("".join(msg_parts))


class LibraryNotFoundError(FileNotFoundError):
    """Raised when a KiCad library file cannot be found."""

    def __init__(self, library_name: str, searched_paths: list[Path]):
        self.library_name = library_name
        self.searched_paths = searched_paths

        # Extract library base name (without .kicad_sym extension)
        lib_base = library_name.replace(".kicad_sym", "")

        msg_parts = [f"Library '{library_name}' not found"]

        # Add library-specific context if available
        if lib_base in LIBRARY_INFO:
            info = LIBRARY_INFO[lib_base]
            msg_parts.append(f"\n\nThis library contains: {info['description']}")
            msg_parts.append(f"\nUsed by: {', '.join(info['used_by'])}")
            if info["install_hint"]:
                msg_parts.append(f"\nNote: {info['install_hint']}")

        msg_parts.append("\n\nSearched paths:")
        for path in searched_paths:
            exists_marker = "" if path.exists() else " (not found)"
            msg_parts.append(f"\n  {path}{exists_marker}")

        msg_parts.append("\n\nTo fix:")
        msg_parts.append("\n  1. Verify KiCad 8+ is installed")
        msg_parts.append("\n  2. Check library exists at standard location:")
        msg_parts.append("\n     - Linux: /usr/share/kicad/symbols/")
        msg_parts.append(
            "\n     - macOS: /Applications/KiCad/KiCad.app/Contents/SharedSupport/symbols/"
        )
        msg_parts.append(
            "\n     - Windows: C:\\Program Files\\KiCad\\<version>\\share\\kicad\\symbols\\"
        )
        msg_parts.append("\n  3. Add custom library paths via lib_paths parameter")
        msg_parts.append("\n  4. Use a custom symbol via the *_symbol parameter on the block")

        super().__init__("".join(msg_parts))
