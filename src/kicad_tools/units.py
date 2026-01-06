"""
Unit formatting system for kicad-tools.

Provides configurable unit display (mm vs mils) across all CLI output.
Supports layered configuration: CLI flag > Environment variable > Config file > Default (mm).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import Config

__all__ = [
    "UnitSystem",
    "UnitFormatter",
    "MM_PER_MIL",
    "get_unit_formatter",
    "format_length",
    "format_coordinate",
]

# Conversion constant
MM_PER_MIL = 0.0254

# Environment variable for unit preference
UNITS_ENV_VAR = "KICAD_TOOLS_UNITS"


class UnitSystem(Enum):
    """Unit system for display output."""

    MM = "mm"
    MILS = "mils"

    @classmethod
    def from_string(cls, value: str | None) -> UnitSystem | None:
        """Parse a unit system from a string value.

        Args:
            value: String like "mm", "mils", "mil", or None

        Returns:
            UnitSystem or None if value is None or invalid
        """
        if value is None:
            return None
        value = value.lower().strip()
        if value in ("mm", "millimeters", "millimeter"):
            return cls.MM
        if value in ("mils", "mil", "thou", "thousandths"):
            return cls.MILS
        return None


@dataclass
class UnitFormatter:
    """Formatter for length values with configurable unit system.

    Handles conversion between mm (internal storage) and display units.
    All internal values in kicad-tools are stored in mm.

    Examples:
        >>> fmt = UnitFormatter(UnitSystem.MM)
        >>> fmt.format(0.254)
        '0.254 mm'

        >>> fmt = UnitFormatter(UnitSystem.MILS)
        >>> fmt.format(0.254)
        '10.0 mils'
    """

    system: UnitSystem
    precision_mm: int = 3
    precision_mils: int = 1

    def format(self, value_mm: float, include_unit: bool = True) -> str:
        """Format a mm value in the configured unit system.

        Args:
            value_mm: Value in millimeters
            include_unit: Whether to include the unit suffix (default: True)

        Returns:
            Formatted string with value and optional unit
        """
        if self.system == UnitSystem.MILS:
            value = value_mm / MM_PER_MIL
            if include_unit:
                return f"{value:.{self.precision_mils}f} mils"
            return f"{value:.{self.precision_mils}f}"

        if include_unit:
            return f"{value_mm:.{self.precision_mm}f} mm"
        return f"{value_mm:.{self.precision_mm}f}"

    def format_compact(self, value_mm: float) -> str:
        """Format a mm value compactly (no space before unit).

        Args:
            value_mm: Value in millimeters

        Returns:
            Formatted string like "0.254mm" or "10.0mils"
        """
        if self.system == UnitSystem.MILS:
            value = value_mm / MM_PER_MIL
            return f"{value:.{self.precision_mils}f}mils"
        return f"{value_mm:.{self.precision_mm}f}mm"

    def format_range(self, min_mm: float, max_mm: float) -> str:
        """Format a range of values.

        Args:
            min_mm: Minimum value in mm
            max_mm: Maximum value in mm

        Returns:
            Formatted string like "0.1-0.5 mm" or "3.9-19.7 mils"
        """
        if self.system == UnitSystem.MILS:
            min_val = min_mm / MM_PER_MIL
            max_val = max_mm / MM_PER_MIL
            return f"{min_val:.{self.precision_mils}f}-{max_val:.{self.precision_mils}f} mils"
        return f"{min_mm:.{self.precision_mm}f}-{max_mm:.{self.precision_mm}f} mm"

    def format_coordinate(self, x_mm: float, y_mm: float) -> str:
        """Format a coordinate pair.

        Args:
            x_mm: X coordinate in mm
            y_mm: Y coordinate in mm

        Returns:
            Formatted string like "(1.234, 5.678) mm" or "(48.6, 223.5) mils"
        """
        if self.system == UnitSystem.MILS:
            x = x_mm / MM_PER_MIL
            y = y_mm / MM_PER_MIL
            return f"({x:.{self.precision_mils}f}, {y:.{self.precision_mils}f}) mils"
        return f"({x_mm:.{self.precision_mm}f}, {y_mm:.{self.precision_mm}f}) mm"

    def format_delta(self, delta_mm: float) -> str:
        """Format a delta/difference value with sign.

        Args:
            delta_mm: Delta value in mm

        Returns:
            Formatted string like "+0.050 mm" or "-2.0 mils"
        """
        if self.system == UnitSystem.MILS:
            value = delta_mm / MM_PER_MIL
            return f"{value:+.{self.precision_mils}f} mils"
        return f"{delta_mm:+.{self.precision_mm}f} mm"

    def format_comparison(
        self, actual_mm: float, required_mm: float, *, show_delta: bool = True
    ) -> str:
        """Format a comparison between actual and required values.

        Args:
            actual_mm: Actual value in mm
            required_mm: Required/limit value in mm
            show_delta: Whether to show the difference

        Returns:
            Formatted string like "0.150 mm (required: 0.200 mm, need +0.050 mm)"
        """
        actual_str = self.format(actual_mm)
        required_str = self.format(required_mm)

        if show_delta:
            delta = required_mm - actual_mm
            delta_str = self.format_delta(delta)
            return f"{actual_str} (required: {required_str}, need {delta_str})"
        return f"{actual_str} (required: {required_str})"

    @property
    def unit_name(self) -> str:
        """Get the unit name for this formatter."""
        return self.system.value

    def convert_to_display(self, value_mm: float) -> float:
        """Convert a mm value to the display unit (for calculations).

        Args:
            value_mm: Value in millimeters

        Returns:
            Value in the display unit system
        """
        if self.system == UnitSystem.MILS:
            return value_mm / MM_PER_MIL
        return value_mm

    def convert_from_display(self, value: float) -> float:
        """Convert a display unit value back to mm (for calculations).

        Args:
            value: Value in display units

        Returns:
            Value in millimeters
        """
        if self.system == UnitSystem.MILS:
            return value * MM_PER_MIL
        return value


# Global formatter instance (set by CLI initialization)
_current_formatter: UnitFormatter | None = None


def get_unit_formatter(
    cli_units: str | None = None,
    config: Config | None = None,
) -> UnitFormatter:
    """Get a unit formatter based on precedence: CLI > env > config > default.

    Args:
        cli_units: Unit system from CLI flag (highest priority)
        config: Config object to read display.units from

    Returns:
        Configured UnitFormatter instance
    """
    # Priority 1: CLI argument
    system = UnitSystem.from_string(cli_units)

    # Priority 2: Environment variable
    if system is None:
        env_value = os.environ.get(UNITS_ENV_VAR)
        system = UnitSystem.from_string(env_value)

    # Priority 3: Config file
    if system is None and config is not None:
        config_units = getattr(config.display, "units", None)
        system = UnitSystem.from_string(config_units)

    # Priority 4: Default to mm
    if system is None:
        system = UnitSystem.MM

    # Get precision settings from config if available
    precision_mm = 3
    precision_mils = 1
    if config is not None:
        precision_mm = getattr(config.display, "precision_mm", 3)
        precision_mils = getattr(config.display, "precision_mils", 1)

    return UnitFormatter(
        system=system,
        precision_mm=precision_mm,
        precision_mils=precision_mils,
    )


def set_current_formatter(formatter: UnitFormatter) -> None:
    """Set the global unit formatter for the current session.

    This is called during CLI initialization to make the formatter
    available globally without passing it through every function.

    Args:
        formatter: The UnitFormatter to use globally
    """
    global _current_formatter
    _current_formatter = formatter


def get_current_formatter() -> UnitFormatter:
    """Get the current global unit formatter.

    Returns:
        The currently configured UnitFormatter, or a default mm formatter
    """
    if _current_formatter is None:
        return UnitFormatter(UnitSystem.MM)
    return _current_formatter


# Convenience functions that use the global formatter


def format_length(value_mm: float, include_unit: bool = True) -> str:
    """Format a length value using the current unit system.

    Args:
        value_mm: Value in millimeters
        include_unit: Whether to include the unit suffix

    Returns:
        Formatted string
    """
    return get_current_formatter().format(value_mm, include_unit)


def format_coordinate(x_mm: float, y_mm: float) -> str:
    """Format a coordinate pair using the current unit system.

    Args:
        x_mm: X coordinate in mm
        y_mm: Y coordinate in mm

    Returns:
        Formatted coordinate string
    """
    return get_current_formatter().format_coordinate(x_mm, y_mm)
