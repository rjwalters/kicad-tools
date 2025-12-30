"""
KiCad Schematic Exceptions

Custom exception classes for schematic operations with helpful error messages.
"""

from pathlib import Path


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
        elif self.available_symbols:
            # Show first 10 available symbols
            shown = self.available_symbols[:10]
            msg_parts.append(f"\n\nAvailable symbols ({len(self.available_symbols)} total):")
            for sym in shown:
                msg_parts.append(f"\n  {sym}")
            if len(self.available_symbols) > 10:
                msg_parts.append(f"\n  ... and {len(self.available_symbols) - 10} more")

        super().__init__("".join(msg_parts))


class LibraryNotFoundError(FileNotFoundError):
    """Raised when a KiCad library file cannot be found."""

    def __init__(self, library_name: str, searched_paths: list[Path]):
        self.library_name = library_name
        self.searched_paths = searched_paths

        msg_parts = [f"Library '{library_name}' not found"]
        msg_parts.append("\n\nSearched paths:")
        for path in searched_paths:
            exists_marker = "" if path.exists() else " (not found)"
            msg_parts.append(f"\n  {path}{exists_marker}")

        msg_parts.append("\n\nTo fix:")
        msg_parts.append("\n  1. Verify library name spelling")
        msg_parts.append("\n  2. Check if KiCad is installed at the expected location")
        msg_parts.append("\n  3. Add custom library paths via lib_paths parameter")

        super().__init__("".join(msg_parts))
