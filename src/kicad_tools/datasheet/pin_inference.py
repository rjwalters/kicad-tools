"""
Pin type inference from pin names and patterns.

This module provides automatic detection of KiCad pin types based on
common naming conventions found in datasheets.

Confidence values follow the unified scoring framework in kicad_tools.utils.scoring:
- 0.95-1.0: Exact/very high confidence matches
- 0.8-0.9: High confidence pattern matches
- 0.6-0.7: Medium confidence (description-based)
- 0.3: Low confidence fallback
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from kicad_tools.utils.scoring import ConfidenceLevel

# KiCad pin types
KICAD_PIN_TYPES = {
    "input": "Input signal",
    "output": "Output signal",
    "bidirectional": "Bidirectional (I/O)",
    "tri_state": "Tri-state output",
    "passive": "Passive component",
    "free": "Not internally connected",
    "unspecified": "Type not specified",
    "power_in": "Power input (VCC, VDD, etc.)",
    "power_out": "Power output (voltage regulator)",
    "open_collector": "Open collector/drain output",
    "open_emitter": "Open emitter output",
    "no_connect": "Not connected (NC)",
}


@dataclass
class PinTypeMatch:
    """Result of a pin type inference."""

    pin_type: str
    confidence: float
    matched_pattern: str | None = None


# Pattern priority order (higher index = checked first)
# Each entry is (pattern, pin_type, confidence)
PIN_TYPE_PATTERNS: list[tuple[str, str, float]] = [
    # No connect pins - very high confidence
    (r"^NC$", "no_connect", 1.0),
    (r"^DNC$", "no_connect", 1.0),
    (r"^N\.?C\.?$", "no_connect", 1.0),
    (r"^NO[_\s]?CONNECT$", "no_connect", 1.0),
    # Power input pins - high confidence
    (r"^VCC\d*$", "power_in", 0.95),
    (r"^VDD\d*$", "power_in", 0.95),
    (r"^VBAT$", "power_in", 0.95),
    (r"^VIN$", "power_in", 0.95),
    (r"^VREF$", "power_in", 0.95),
    (r"^V[_]?REF$", "power_in", 0.95),
    (r"^AVCC$", "power_in", 0.95),
    (r"^AVDD$", "power_in", 0.95),
    (r"^DVCC$", "power_in", 0.95),
    (r"^DVDD$", "power_in", 0.95),
    (r"^VSS\d*$", "power_in", 0.95),
    (r"^VEE$", "power_in", 0.95),
    (r"^GND\d*$", "power_in", 0.95),
    (r"^AGND$", "power_in", 0.95),
    (r"^DGND$", "power_in", 0.95),
    (r"^PGND$", "power_in", 0.95),
    (r"^GNDA$", "power_in", 0.95),
    # Power output pins
    (r"^VOUT\d*$", "power_out", 0.9),
    (r"^VREG$", "power_out", 0.9),
    (r"^V[_]?REG$", "power_out", 0.9),
    (r"^LDO$", "power_out", 0.85),
    # Digital GPIO - bidirectional
    (r"^P[A-Z]\d+$", "bidirectional", 0.85),  # PA0, PB1, etc.
    (r"^GPIO\d*$", "bidirectional", 0.85),
    (r"^IO\d+$", "bidirectional", 0.85),
    (r"^PORT[A-Z]\d*$", "bidirectional", 0.85),
    (r"^GP\d+$", "bidirectional", 0.85),  # GP0, GP1, etc.
    # Communication interfaces - bidirectional
    (r"^SDA\d*$", "bidirectional", 0.9),
    (r"^SCL\d*$", "bidirectional", 0.85),  # Often input
    (r"^MOSI$", "output", 0.8),  # Master out
    (r"^MISO$", "input", 0.8),  # Master in
    (r"^COPI$", "output", 0.8),  # Controller out
    (r"^CIPO$", "input", 0.8),  # Controller in
    (r"^SDI$", "input", 0.85),  # Serial data in
    (r"^SDO$", "output", 0.85),  # Serial data out
    (r"^TX\d*$", "output", 0.85),
    (r"^TXD\d*$", "output", 0.85),
    (r"^RX\d*$", "input", 0.85),
    (r"^RXD\d*$", "input", 0.85),
    # Clock signals - typically inputs
    (r"^SCK\d*$", "input", 0.8),
    (r"^CLK\d*$", "input", 0.8),
    (r"^SCLK$", "input", 0.8),
    (r"^CLKIN$", "input", 0.9),
    (r"^CLKOUT$", "output", 0.9),
    # Chip select - typically input
    (r"^CS$", "input", 0.85),
    (r"^SS$", "input", 0.85),
    (r"^NSS$", "input", 0.85),
    (r"^CE\d*$", "input", 0.85),
    (r"^NCS$", "input", 0.85),
    (r"^/CS$", "input", 0.85),
    (r"^CSN$", "input", 0.85),
    # Analog pins
    (r"^AIN\d*$", "input", 0.85),
    (r"^ADC\d*$", "input", 0.85),
    (r"^AN\d+$", "input", 0.85),
    (r"^A\d+$", "input", 0.7),  # Lower confidence for A0, A1, etc.
    (r"^DAC\d*$", "output", 0.85),
    (r"^AOUT\d*$", "output", 0.85),
    # Control signals - typically input
    (r"^RST$", "input", 0.9),
    (r"^RESET$", "input", 0.9),
    (r"^NRST$", "input", 0.9),
    (r"^/RST$", "input", 0.9),
    (r"^RSTN$", "input", 0.9),
    (r"^EN$", "input", 0.85),
    (r"^ENABLE$", "input", 0.85),
    (r"^OE$", "input", 0.85),
    (r"^WE$", "input", 0.85),
    (r"^RD$", "input", 0.85),
    (r"^WR$", "input", 0.85),
    # Interrupt signals - typically output from device
    (r"^INT\d*$", "output", 0.8),
    (r"^IRQ\d*$", "output", 0.8),
    (r"^NINT$", "output", 0.8),
    (r"^/INT$", "output", 0.8),
    (r"^INTN$", "output", 0.8),
    # Oscillator pins
    (r"^OSCIN$", "input", 0.9),
    (r"^XTALIN$", "input", 0.9),
    (r"^XIN$", "input", 0.9),
    (r"^XI$", "input", 0.9),
    (r"^OSCOUT$", "output", 0.9),
    (r"^XTALOUT$", "output", 0.9),
    (r"^XOUT$", "output", 0.9),
    (r"^XO$", "output", 0.9),
    (r"^XTAL\d?$", "bidirectional", 0.7),  # Could be either
    # Boot/mode pins
    (r"^BOOT\d*$", "input", 0.8),
    (r"^MODE\d*$", "input", 0.8),
    # Test pins
    (r"^TEST\d*$", "input", 0.7),
    (r"^TDI$", "input", 0.9),  # JTAG
    (r"^TDO$", "output", 0.9),  # JTAG
    (r"^TMS$", "input", 0.9),  # JTAG
    (r"^TCK$", "input", 0.9),  # JTAG
    (r"^TRST$", "input", 0.9),  # JTAG
    (r"^SWDIO$", "bidirectional", 0.9),  # SWD
    (r"^SWCLK$", "input", 0.9),  # SWD
]

# Compile patterns for efficiency
_COMPILED_PATTERNS: list[tuple[re.Pattern, str, float]] = [
    (re.compile(pattern, re.IGNORECASE), pin_type, confidence)
    for pattern, pin_type, confidence in PIN_TYPE_PATTERNS
]

# Mapping from datasheet electrical types to KiCad types
ELECTRICAL_TYPE_MAP: dict[str, str] = {
    # Input types
    "i": "input",
    "in": "input",
    "input": "input",
    # Output types
    "o": "output",
    "out": "output",
    "output": "output",
    # Bidirectional
    "io": "bidirectional",
    "i/o": "bidirectional",
    "b": "bidirectional",
    "bi": "bidirectional",
    # Power
    "p": "power_in",
    "s": "power_in",  # Supply
    "pwr": "power_in",
    "power": "power_in",
    "gnd": "power_in",
    "vcc": "power_in",
    # High-Z / tri-state
    "t": "tri_state",
    "tri": "tri_state",
    "z": "tri_state",
    # Open drain/collector
    "od": "open_collector",
    "oc": "open_collector",
    # Analog
    "a": "input",  # Usually analog inputs
    "ai": "input",
    "ao": "output",
}


def infer_pin_type(
    name: str,
    electrical_type: str | None = None,
    description: str | None = None,
) -> PinTypeMatch:
    """
    Infer the KiCad pin type from a pin name and optional metadata.

    The inference follows this priority:
    1. Exact match on electrical type from datasheet (highest confidence)
    2. Pattern matching on pin name
    3. Keyword matching in description
    4. Default to passive (lowest confidence)

    Args:
        name: The pin name/signal name (e.g., "VCC", "PA0", "GPIO12")
        electrical_type: Optional electrical type from datasheet (e.g., "I/O", "P")
        description: Optional pin description for additional context

    Returns:
        PinTypeMatch with the inferred type and confidence
    """
    # Clean the name
    clean_name = name.strip().upper()

    # First, check electrical type from datasheet
    if electrical_type:
        elec_lower = electrical_type.strip().lower()
        if elec_lower in ELECTRICAL_TYPE_MAP:
            return PinTypeMatch(
                pin_type=ELECTRICAL_TYPE_MAP[elec_lower],
                confidence=0.95,
                matched_pattern=f"electrical_type:{electrical_type}",
            )

    # Check name against patterns
    for pattern, pin_type, confidence in _COMPILED_PATTERNS:
        if pattern.match(clean_name):
            return PinTypeMatch(
                pin_type=pin_type,
                confidence=confidence,
                matched_pattern=pattern.pattern,
            )

    # Check description for hints
    if description:
        desc_lower = description.lower()
        if any(kw in desc_lower for kw in ["power", "supply", "vcc", "vdd", "gnd"]):
            return PinTypeMatch(
                pin_type="power_in",
                confidence=0.7,
                matched_pattern="description:power",
            )
        if "input" in desc_lower and "output" not in desc_lower:
            return PinTypeMatch(
                pin_type="input",
                confidence=0.6,
                matched_pattern="description:input",
            )
        if "output" in desc_lower and "input" not in desc_lower:
            return PinTypeMatch(
                pin_type="output",
                confidence=0.6,
                matched_pattern="description:output",
            )
        if "gpio" in desc_lower or "i/o" in desc_lower:
            return PinTypeMatch(
                pin_type="bidirectional",
                confidence=0.6,
                matched_pattern="description:gpio",
            )

    # Default to bidirectional with low confidence for unknown pins
    return PinTypeMatch(
        pin_type="bidirectional",
        confidence=ConfidenceLevel.VERY_LOW.value,
        matched_pattern=None,
    )


def apply_type_overrides(
    pins: list,
    overrides: dict[str, str],
) -> None:
    """
    Apply manual type overrides to a list of pins.

    Modifies pins in place.

    Args:
        pins: List of ExtractedPin objects
        overrides: Dictionary mapping pin numbers to KiCad types
    """
    for pin in pins:
        if pin.number in overrides:
            pin.type = overrides[pin.number]
            pin.type_confidence = 1.0
            pin.type_source = "manual"


# Column name patterns for identifying pin table columns
# Note: These patterns use word boundaries and anchors to avoid false matches
COLUMN_PATTERNS: dict[str, list[str]] = {
    "number": [
        r"^pin\s*(no\.?|#|number)$",  # "Pin No", "Pin #", "Pin Number" but not "Pin Name"
        r"^#$",
        r"\bball\b",
        r"\bpad\b",
        r"^no\.?$",
    ],
    "name": [
        r"\bname\b",
        r"\bsignal\b",
        r"^symbol$",
    ],
    "type": [
        r"^type$",
        r"^i/?o$",  # Only match "I/O" or "IO" exactly
        r"\bdirection\b",
        r"buffer\s*type",
    ],
    "description": [
        r"\bdesc\b",
        r"\bfunction\b",
        r"\bdescription\b",
        r"\bremark\b",
        r"\bnote\b",
    ],
    "alt_functions": [
        r"\balt\b",
        r"\balternate\b",
        r"\bremap\b",
        r"\bmux\b",
        r"\bmultiplex\b",
    ],
}

# Compiled column patterns
_COLUMN_MATCHERS: dict[str, list[re.Pattern]] = {
    col_type: [re.compile(p, re.IGNORECASE) for p in patterns]
    for col_type, patterns in COLUMN_PATTERNS.items()
}


def identify_column_type(header: str) -> str | None:
    """
    Identify the type of a column from its header.

    Args:
        header: The column header text

    Returns:
        Column type ("number", "name", "type", "description", "alt_functions")
        or None if not recognized
    """
    clean_header = header.strip().lower()

    for col_type, patterns in _COLUMN_MATCHERS.items():
        for pattern in patterns:
            if pattern.search(clean_header):
                return col_type

    return None


def is_pin_table(headers: list[str]) -> tuple[bool, float]:
    """
    Determine if a table appears to be a pin definition table.

    Args:
        headers: List of column headers

    Returns:
        Tuple of (is_pin_table, confidence)
    """
    if not headers:
        return False, 0.0

    identified = [identify_column_type(h) for h in headers]

    # Must have at least number and name columns
    has_number = "number" in identified
    has_name = "name" in identified

    if not has_number or not has_name:
        return False, 0.0

    # Calculate confidence based on how many columns we recognize
    recognized_count = sum(1 for t in identified if t is not None)
    confidence = recognized_count / len(headers)

    # Boost confidence if we have type column
    if "type" in identified:
        confidence = min(1.0, confidence + 0.2)

    return True, confidence
