"""
Unit value parsing for PCB specifications.

Parses string representations of electrical and physical quantities
like "5V", "2A", "100nF", "10k", "0.15mm", "-40°C", etc.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

# SI prefixes with their multipliers
SI_PREFIXES = {
    "f": 1e-15,  # femto
    "p": 1e-12,  # pico
    "n": 1e-9,  # nano
    "u": 1e-6,  # micro (also μ)
    "μ": 1e-6,  # micro (unicode)
    "m": 1e-3,  # milli
    "c": 1e-2,  # centi
    "": 1,  # no prefix
    "k": 1e3,  # kilo
    "K": 1e3,  # kilo (alternate)
    "M": 1e6,  # mega
    "G": 1e9,  # giga
    "T": 1e12,  # tera
}

# Unit type mappings - base units and their aliases
UNIT_TYPES = {
    # Voltage
    "V": "V",
    "v": "V",
    "volt": "V",
    "volts": "V",
    # Current
    "A": "A",
    "a": "A",
    "amp": "A",
    "amps": "A",
    "ampere": "A",
    "amperes": "A",
    # Resistance
    "Ω": "Ω",
    "ohm": "Ω",
    "ohms": "Ω",
    "R": "Ω",  # Common in values like "10k" (implies ohms)
    # Capacitance
    "F": "F",
    "f": "F",
    "farad": "F",
    "farads": "F",
    # Inductance
    "H": "H",
    "h": "H",
    "henry": "H",
    "henries": "H",
    # Power
    "W": "W",
    "w": "W",
    "watt": "W",
    "watts": "W",
    # Frequency
    "Hz": "Hz",
    "hz": "Hz",
    "hertz": "Hz",
    # Length
    "m": "m",
    "meter": "m",
    "meters": "m",
    "mm": "mm",
    "millimeter": "mm",
    "millimeters": "mm",
    "mil": "mil",
    "mils": "mil",
    "in": "in",
    "inch": "in",
    "inches": "in",
    # Temperature
    "°C": "°C",
    "C": "°C",
    "celsius": "°C",
    "°F": "°F",
    "fahrenheit": "°F",
    "K": "K",
    "kelvin": "K",
    # Time
    "s": "s",
    "sec": "s",
    "second": "s",
    "seconds": "s",
    # Percentage
    "%": "%",
    "percent": "%",
}


@dataclass
class UnitValue:
    """Parsed unit value with magnitude and unit.

    Attributes:
        raw: Original string representation
        value: Numeric value (with SI prefix applied)
        unit: Normalized unit string (e.g., "V", "A", "Ω")
        prefix: SI prefix used (e.g., "m" for milli)
    """

    raw: str
    value: float
    unit: str
    prefix: str = ""

    def __str__(self) -> str:
        """Return the original string representation."""
        return self.raw

    def __repr__(self) -> str:
        return f"UnitValue({self.raw!r}, value={self.value}, unit={self.unit!r})"

    def to_base(self) -> float:
        """Return value in base units (no SI prefix)."""
        return self.value

    def __eq__(self, other: Any) -> bool:
        if isinstance(other, UnitValue):
            return self.value == other.value and self.unit == other.unit
        return False

    def __lt__(self, other: UnitValue) -> bool:
        if self.unit != other.unit:
            raise ValueError(f"Cannot compare {self.unit} with {other.unit}")
        return self.value < other.value

    def __le__(self, other: UnitValue) -> bool:
        if self.unit != other.unit:
            raise ValueError(f"Cannot compare {self.unit} with {other.unit}")
        return self.value <= other.value


# Pattern for parsing unit values
# Matches: "5V", "100mA", "10k", "4.7µF", "0.15mm", "-40°C", "50mV_pp"
UNIT_PATTERN = re.compile(
    r"^"
    r"(?P<sign>[+-])?"  # Optional sign
    r"(?P<number>\d+(?:\.\d+)?)"  # Number (integer or decimal)
    r"(?P<prefix>[fpnuμmckKMGT])?"  # Optional SI prefix
    r"(?P<unit>[a-zA-Z°Ω%]+)?"  # Unit (optional for bare resistance values)
    r"(?P<suffix>_pp|_rms|_pk)?"  # Optional measurement qualifier
    r"$"
)

# Pattern for bare resistance values like "10k", "4.7M"
RESISTANCE_PATTERN = re.compile(
    r"^"
    r"(?P<number>\d+(?:\.\d+)?)"  # Number
    r"(?P<prefix>[kKMGT])"  # Prefix (required, uppercase implies resistance)
    r"$"
)


def parse_unit_value(value: str) -> UnitValue:
    """Parse a string unit value into structured form.

    Args:
        value: String like "5V", "100mA", "10k", "4.7µF", "0.15mm"

    Returns:
        UnitValue with parsed components

    Raises:
        ValueError: If the value cannot be parsed

    Examples:
        >>> parse_unit_value("5V")
        UnitValue('5V', value=5.0, unit='V')

        >>> parse_unit_value("100mA")
        UnitValue('100mA', value=0.1, unit='A')

        >>> parse_unit_value("10k")
        UnitValue('10k', value=10000.0, unit='Ω')

        >>> parse_unit_value("4.7µF")
        UnitValue('4.7µF', value=4.7e-06, unit='F')
    """
    value = value.strip()

    # Handle bare resistance values like "10k"
    resistance_match = RESISTANCE_PATTERN.match(value)
    if resistance_match:
        number = float(resistance_match.group("number"))
        prefix = resistance_match.group("prefix")
        multiplier = SI_PREFIXES.get(prefix, 1)
        return UnitValue(
            raw=value,
            value=number * multiplier,
            unit="Ω",
            prefix=prefix,
        )

    # Try full unit pattern
    match = UNIT_PATTERN.match(value)
    if not match:
        raise ValueError(f"Cannot parse unit value: {value!r}")

    sign = match.group("sign") or ""
    number_str = match.group("number")
    prefix = match.group("prefix") or ""
    unit_str = match.group("unit") or ""
    suffix = match.group("suffix") or ""

    # Parse number with sign
    number = float(sign + number_str)

    # Apply SI prefix
    multiplier = SI_PREFIXES.get(prefix, 1)

    # Handle special case: "mm" where 'm' could be milli or meter
    if prefix == "m" and unit_str == "m":
        # This is millimeters
        normalized_unit = "mm"
        prefix = ""
        multiplier = 1
    elif prefix == "m" and unit_str in ("", "m"):
        # Ambiguous - treat as milli if followed by a unit, else as millimeters
        if unit_str == "":
            # Could be meters, but in PCB context more likely millimeters
            normalized_unit = "mm"
            prefix = ""
            multiplier = 1
        else:
            normalized_unit = UNIT_TYPES.get(unit_str, unit_str)
    else:
        # Normalize the unit
        normalized_unit = UNIT_TYPES.get(unit_str, unit_str)
        # Handle mm specifically
        if unit_str == "mm":
            normalized_unit = "mm"
            prefix = ""
            multiplier = 1

    # Handle temperature units (no SI prefix on °C)
    # Note: Only match explicit temperature units, not "F" (Farad) or "C" alone
    if "°" in unit_str or unit_str.lower() in ("celsius", "fahrenheit", "kelvin"):
        normalized_unit = UNIT_TYPES.get(unit_str, unit_str)
        prefix = ""
        multiplier = 1

    calculated_value = number * multiplier

    return UnitValue(
        raw=value,
        value=calculated_value,
        unit=normalized_unit + suffix,
        prefix=prefix,
    )


def format_unit_value(value: float, unit: str, precision: int = 3) -> str:
    """Format a numeric value with appropriate SI prefix.

    Args:
        value: Numeric value in base units
        unit: Unit string (e.g., "V", "A", "Ω")
        precision: Decimal precision for display

    Returns:
        Formatted string with SI prefix

    Examples:
        >>> format_unit_value(0.001, "A")
        '1mA'

        >>> format_unit_value(10000, "Ω")
        '10kΩ'
    """
    if value == 0:
        return f"0{unit}"

    abs_value = abs(value)
    sign = "-" if value < 0 else ""

    # Find appropriate prefix
    prefixes = [
        (1e12, "T"),
        (1e9, "G"),
        (1e6, "M"),
        (1e3, "k"),
        (1, ""),
        (1e-3, "m"),
        (1e-6, "μ"),
        (1e-9, "n"),
        (1e-12, "p"),
        (1e-15, "f"),
    ]

    for threshold, prefix in prefixes:
        if abs_value >= threshold:
            scaled = abs_value / threshold
            # Format with minimal decimal places
            if scaled == int(scaled):
                return f"{sign}{int(scaled)}{prefix}{unit}"
            else:
                formatted = f"{scaled:.{precision}g}"
                return f"{sign}{formatted}{prefix}{unit}"

    # Very small value
    return f"{sign}{value:.{precision}g}{unit}"
