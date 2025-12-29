#!/usr/bin/env python3
"""
KiCad Symbol Generator

Creates KiCad symbol library files from pin definitions.

Supported input formats:
- JSON pin definition file
- CSV with pin number, name, type columns
- Datasheet-style text (parsed interactively)

Features:
- Automatic pin type detection from names (VCC→power_in, GND→power_in, etc.)
- Intelligent pin arrangement (power top/bottom, I/O left/right)
- Package-aware layouts (DIP, SOIC, QFP, BGA)
- Property generation (Reference, Value, Footprint, Description)

Usage:
    # From JSON definition
    python create_symbol.py --json pins.json --output MyLib.kicad_sym

    # From CSV
    python create_symbol.py --csv pins.csv --name "TPA3116D2" --output Amplifier.kicad_sym

    # Interactive from datasheet text
    python create_symbol.py --interactive --name "PCM5122" --output DAC.kicad_sym

    # From template
    python create_symbol.py --template qfp48 --name "STM32F103" --output MCU.kicad_sym

Example JSON format:
    {
        "name": "TPA3116D2",
        "reference": "U",
        "footprint": "Package_SO:HTSSOP-28-1EP_4.4x9.7mm_P0.65mm_EP3.4x9.5mm",
        "description": "Class-D Audio Amplifier, 2x50W",
        "datasheet": "https://www.ti.com/lit/ds/symlink/tpa3116d2.pdf",
        "pins": [
            {"number": "1", "name": "PVCC", "type": "power_in"},
            {"number": "2", "name": "OUTL+", "type": "output"},
            ...
        ]
    }
"""

import argparse
import csv
import json
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional


class PinType(Enum):
    """KiCad pin electrical types."""

    INPUT = "input"
    OUTPUT = "output"
    BIDIRECTIONAL = "bidirectional"
    TRI_STATE = "tri_state"
    PASSIVE = "passive"
    FREE = "free"
    UNSPECIFIED = "unspecified"
    POWER_IN = "power_in"
    POWER_OUT = "power_out"
    OPEN_COLLECTOR = "open_collector"
    OPEN_EMITTER = "open_emitter"
    UNCONNECTED = "unconnected"
    NO_CONNECT = "no_connect"


class PinStyle(Enum):
    """KiCad pin graphic styles."""

    LINE = "line"
    INVERTED = "inverted"
    CLOCK = "clock"
    INVERTED_CLOCK = "inverted_clock"
    INPUT_LOW = "input_low"
    CLOCK_LOW = "clock_low"
    OUTPUT_LOW = "output_low"
    EDGE_CLOCK_HIGH = "edge_clock_high"
    NON_LOGIC = "non_logic"


class PinSide(Enum):
    """Which side of the symbol the pin appears on."""

    LEFT = "left"
    RIGHT = "right"
    TOP = "top"
    BOTTOM = "bottom"


@dataclass
class PinDef:
    """Pin definition for symbol generation."""

    number: str
    name: str
    pin_type: PinType = PinType.PASSIVE
    style: PinStyle = PinStyle.LINE
    side: Optional[PinSide] = None  # Auto-assigned if None
    hidden: bool = False

    @classmethod
    def from_dict(cls, d: dict) -> "PinDef":
        """Create PinDef from dictionary."""
        pin_type = d.get("type", d.get("pin_type", "passive"))
        if isinstance(pin_type, str):
            pin_type = PinType(pin_type.lower())

        style = d.get("style", "line")
        if isinstance(style, str):
            style = PinStyle(style.lower())

        side = d.get("side")
        if side and isinstance(side, str):
            side = PinSide(side.lower())

        return cls(
            number=str(d["number"]),
            name=d.get("name", ""),
            pin_type=pin_type,
            style=style,
            side=side,
            hidden=d.get("hidden", False),
        )


@dataclass
class SymbolDef:
    """Complete symbol definition."""

    name: str
    pins: list[PinDef]
    reference: str = "U"
    value: str = ""
    footprint: str = ""
    datasheet: str = ""
    description: str = ""
    keywords: str = ""

    # Layout options
    pin_length: float = 2.54  # mm
    pin_spacing: float = 2.54  # mm (100 mil)
    min_width: float = 10.16  # mm (400 mil)

    def __post_init__(self):
        if not self.value:
            self.value = self.name


# Pin type detection patterns
PIN_TYPE_PATTERNS = [
    # Power input
    (r"^V(CC|DD|BAT|IN|BUS|IO|REF|A|D|S|L)[\dA-Z]*$", PinType.POWER_IN),
    (r"^(PVCC|AVCC|DVCC|AVDD|DVDD|PVDD)[\dA-Z]*$", PinType.POWER_IN),
    (r"^[+-]?(3V3|5V|12V|1V8|2V5|VCC|VDD)", PinType.POWER_IN),
    (r"^(GND|VSS|AGND|DGND|PGND|GNDA|GNDD)[\dA-Z]*$", PinType.POWER_IN),
    (r"^(EP|EPAD|PAD|THERMAL)$", PinType.POWER_IN),  # Exposed pad
    # No connect
    (r"^N/?C[\dA-Z]*$", PinType.NO_CONNECT),
    (r"^(NC|DNC|RSVD|RESERVED)[\dA-Z]*$", PinType.NO_CONNECT),
    # Clock inputs (with clock style)
    (r"^(CLK|CLOCK|SCK|SCLK|BCLK|MCLK|LRCLK|WS)", PinType.INPUT),
    (r"^X(TAL)?IN[\dA-Z]*$", PinType.INPUT),
    # Reset (active low)
    (r"^(~?RST|~?RESET|~?NRST|RST_?N|RESET_?N)", PinType.INPUT),
    # Standard inputs
    (r"^(SDA|SPI_?MOSI|DIN|DATA_?IN|RX|RXD|D\d*IN)", PinType.INPUT),
    (r"^(EN|ENABLE|~?SHDN|~?SHUTDOWN|~?MUTE|~?STBY|STANDBY)", PinType.INPUT),
    (r"^(GAIN\d*|MODE\d*|SEL\d*|ADR\d*|ADDR\d*)", PinType.INPUT),
    # Standard outputs
    (r"^(SCL|SPI_?MISO|DOUT|DATA_?OUT|TX|TXD|D\d*OUT)", PinType.OUTPUT),
    (r"^(OUT[LRP]?[+-]?|OUTP|OUTN|VOUT|AOUT|DOUT)", PinType.OUTPUT),
    (r"^(FAULT|~?FAULT|ERR|~?ERR|FLAG|INT|~?INT)", PinType.OUTPUT),
    # Bidirectional
    (r"^(GPIO\d*|P[A-Z]?\d+|IO\d*|D\d+)", PinType.BIDIRECTIONAL),
    (r"^(I2C_?SDA|SDA\d*)", PinType.BIDIRECTIONAL),
    # Audio/analog
    (r"^(INL|INR|LINEIN|RINEIN|L_?IN|R_?IN)", PinType.INPUT),
    (r"^(OUTL|OUTR|LINEOUT|ROUTOUT|L_?OUT|R_?OUT|HP)", PinType.OUTPUT),
    (r"^(AIN\d*|ADC\d*)", PinType.INPUT),
    (r"^(AOUT\d*|DAC\d*)", PinType.OUTPUT),
    # Crystal oscillator
    (r"^X(TAL)?OUT[\dA-Z]*$", PinType.OUTPUT),
]

# Pin side assignment patterns (for intelligent layout)
PIN_SIDE_PATTERNS = [
    # Power on top and bottom
    (r"^V(CC|DD|BAT|IN|BUS|IO|REF|A|D|S|L)[\dA-Z]*$", PinSide.TOP),
    (r"^(PVCC|AVCC|DVCC|AVDD|DVDD|PVDD)[\dA-Z]*$", PinSide.TOP),
    (r"^[+-]?(3V3|5V|12V|1V8|2V5|VCC|VDD)", PinSide.TOP),
    (r"^(GND|VSS|AGND|DGND|PGND|GNDA|GNDD)[\dA-Z]*$", PinSide.BOTTOM),
    (r"^(EP|EPAD|PAD|THERMAL)$", PinSide.BOTTOM),
    # Inputs on left
    (r"^(IN|INPUT|SDA|MOSI|DIN|RX|CLK|SCK|EN|RST|~?RST|NRST)", PinSide.LEFT),
    (r"^(GAIN|MODE|SEL|ADR|ADDR|LINEIN|RINEIN|AIN|ADC)", PinSide.LEFT),
    # Outputs on right
    (r"^(OUT|OUTPUT|SCL|MISO|DOUT|TX|FAULT|ERR|FLAG|INT)", PinSide.RIGHT),
    (r"^(LINEOUT|ROUTOUT|HP|AOUT|DAC)", PinSide.RIGHT),
]

# Patterns that indicate clock style pins
CLOCK_PATTERNS = [
    r"^(CLK|CLOCK|SCK|SCLK|BCLK|MCLK|LRCLK|WS|TCK)",
    r"^X(TAL)?IN[\dA-Z]*$",
]

# Patterns that indicate inverted (active low) pins
INVERTED_PATTERNS = [
    r"^~",  # KiCad tilde notation
    r"_N$",  # Suffix _N
    r"N$",  # Suffix N (for RST_N, CS_N)
    r"^/(RST|CS|SS|EN|INT|IRQ)",  # /RST notation
]


def detect_pin_type(name: str) -> PinType:
    """Detect pin type from name using pattern matching."""
    name_upper = name.upper().replace("~", "").replace("/", "")

    for pattern, pin_type in PIN_TYPE_PATTERNS:
        if re.match(pattern, name_upper, re.IGNORECASE):
            return pin_type

    return PinType.PASSIVE


def detect_pin_side(name: str, pin_type: PinType) -> PinSide:
    """Detect which side of symbol a pin should be on."""
    name_upper = name.upper().replace("~", "").replace("/", "")

    # Check explicit patterns first
    for pattern, side in PIN_SIDE_PATTERNS:
        if re.match(pattern, name_upper, re.IGNORECASE):
            return side

    # Fall back to type-based assignment
    if pin_type == PinType.POWER_IN:
        if "GND" in name_upper or "VSS" in name_upper:
            return PinSide.BOTTOM
        return PinSide.TOP
    elif pin_type == PinType.INPUT:
        return PinSide.LEFT
    elif pin_type == PinType.OUTPUT:
        return PinSide.RIGHT
    elif pin_type == PinType.BIDIRECTIONAL:
        return PinSide.RIGHT
    elif pin_type == PinType.NO_CONNECT:
        return PinSide.RIGHT

    return PinSide.LEFT


def detect_pin_style(name: str, pin_type: PinType) -> PinStyle:
    """Detect pin graphic style from name."""
    # Check for clock patterns
    for pattern in CLOCK_PATTERNS:
        if re.match(pattern, name.upper(), re.IGNORECASE):
            return PinStyle.CLOCK

    # Check for inverted (active low) patterns
    for pattern in INVERTED_PATTERNS:
        if re.search(pattern, name, re.IGNORECASE):
            return PinStyle.INVERTED

    return PinStyle.LINE


def parse_json(path: Path) -> SymbolDef:
    """Parse symbol definition from JSON file."""
    with open(path) as f:
        data = json.load(f)

    pins = []
    for p in data.get("pins", []):
        pin = PinDef.from_dict(p)

        # Auto-detect type and side if not specified
        if pin.pin_type == PinType.PASSIVE and "type" not in p:
            pin.pin_type = detect_pin_type(pin.name)
        if pin.side is None:
            pin.side = detect_pin_side(pin.name, pin.pin_type)
        if pin.style == PinStyle.LINE and "style" not in p:
            pin.style = detect_pin_style(pin.name, pin.pin_type)

        pins.append(pin)

    return SymbolDef(
        name=data.get("name", "Unknown"),
        pins=pins,
        reference=data.get("reference", "U"),
        value=data.get("value", data.get("name", "")),
        footprint=data.get("footprint", ""),
        datasheet=data.get("datasheet", ""),
        description=data.get("description", ""),
        keywords=data.get("keywords", ""),
    )


def parse_csv(path: Path, name: str) -> SymbolDef:
    """Parse pin definitions from CSV file.

    Expected columns: number, name, [type], [side], [style]
    """
    pins = []

    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            # Normalize column names
            row = {k.lower().strip(): v.strip() for k, v in row.items()}

            pin_type = PinType.PASSIVE
            if "type" in row and row["type"]:
                try:
                    pin_type = PinType(row["type"].lower())
                except ValueError:
                    pin_type = detect_pin_type(row.get("name", ""))
            else:
                pin_type = detect_pin_type(row.get("name", ""))

            side = None
            if "side" in row and row["side"]:
                try:
                    side = PinSide(row["side"].lower())
                except ValueError:
                    pass
            if side is None:
                side = detect_pin_side(row.get("name", ""), pin_type)

            style = PinStyle.LINE
            if "style" in row and row["style"]:
                try:
                    style = PinStyle(row["style"].lower())
                except ValueError:
                    pass
            else:
                style = detect_pin_style(row.get("name", ""), pin_type)

            pins.append(
                PinDef(
                    number=row.get("number", row.get("pin", "")),
                    name=row.get("name", ""),
                    pin_type=pin_type,
                    side=side,
                    style=style,
                )
            )

    return SymbolDef(name=name, pins=pins)


def parse_datasheet_text(text: str, name: str) -> SymbolDef:
    """Parse pin definitions from datasheet-style text.

    Accepts formats like:
        1  PVCC     Power supply
        2  OUT+     Output positive
        3  GND      Ground

    Or tab/comma separated.
    """
    pins = []

    for line in text.strip().split("\n"):
        line = line.strip()
        if not line or line.startswith("#"):
            continue

        # Try different separators
        parts = None
        for sep in ["\t", ",", "  ", " "]:
            parts = [p.strip() for p in line.split(sep) if p.strip()]
            if len(parts) >= 2:
                break

        if not parts or len(parts) < 2:
            continue

        # First part is pin number, second is name
        pin_num = parts[0]
        pin_name = parts[1]

        # Optional third part is description/type hint
        type_hint = parts[2] if len(parts) > 2 else ""

        pin_type = detect_pin_type(pin_name)
        if type_hint:
            # Try to extract type from description
            type_hint_lower = type_hint.lower()
            if "power" in type_hint_lower or "supply" in type_hint_lower:
                pin_type = PinType.POWER_IN
            elif "ground" in type_hint_lower:
                pin_type = PinType.POWER_IN
            elif "input" in type_hint_lower:
                pin_type = PinType.INPUT
            elif "output" in type_hint_lower:
                pin_type = PinType.OUTPUT
            elif "bidirectional" in type_hint_lower or "i/o" in type_hint_lower:
                pin_type = PinType.BIDIRECTIONAL
            elif "no connect" in type_hint_lower or "n/c" in type_hint_lower:
                pin_type = PinType.NO_CONNECT

        pins.append(
            PinDef(
                number=pin_num,
                name=pin_name,
                pin_type=pin_type,
                side=detect_pin_side(pin_name, pin_type),
                style=detect_pin_style(pin_name, pin_type),
            )
        )

    return SymbolDef(name=name, pins=pins)


def generate_symbol_sexp(sym: SymbolDef) -> str:
    """Generate KiCad S-expression for symbol."""

    # Group pins by side
    pins_by_side = {side: [] for side in PinSide}
    for pin in sym.pins:
        side = pin.side or PinSide.LEFT
        pins_by_side[side].append(pin)

    # Calculate dimensions
    left_count = len(pins_by_side[PinSide.LEFT])
    right_count = len(pins_by_side[PinSide.RIGHT])
    top_count = len(pins_by_side[PinSide.TOP])
    bottom_count = len(pins_by_side[PinSide.BOTTOM])

    max_side = max(left_count, right_count)
    height = max(max_side * sym.pin_spacing, 5.08)  # At least 200 mil

    max_top_bottom = max(top_count, bottom_count)
    width = max(max_top_bottom * sym.pin_spacing, sym.min_width)

    # Ensure width accommodates the longest pin name
    max_name_len = max((len(p.name) for p in sym.pins), default=0)
    min_name_width = max_name_len * 1.27 + sym.pin_length * 2 + 5.08
    width = max(width, min_name_width)

    # Round to grid
    width = round(width / 2.54) * 2.54
    height = round(height / 2.54) * 2.54

    half_w = width / 2
    half_h = height / 2

    # Generate pins
    pin_lines = []

    # Left side pins (pointing right, angle=0)
    for i, pin in enumerate(pins_by_side[PinSide.LEFT]):
        y = half_h - sym.pin_spacing * (i + 0.5) if left_count > 0 else 0
        y = round(y / 1.27) * 1.27  # Snap to 50 mil grid
        x = -half_w - sym.pin_length
        pin_lines.append(_gen_pin_sexp(pin, x, y, 0, sym.pin_length))

    # Right side pins (pointing left, angle=180)
    for i, pin in enumerate(pins_by_side[PinSide.RIGHT]):
        y = half_h - sym.pin_spacing * (i + 0.5) if right_count > 0 else 0
        y = round(y / 1.27) * 1.27
        x = half_w + sym.pin_length
        pin_lines.append(_gen_pin_sexp(pin, x, y, 180, sym.pin_length))

    # Top pins (pointing down, angle=270)
    for i, pin in enumerate(pins_by_side[PinSide.TOP]):
        x = -half_w + sym.pin_spacing * (i + 0.5) if top_count > 0 else 0
        x = round(x / 1.27) * 1.27
        y = half_h + sym.pin_length
        pin_lines.append(_gen_pin_sexp(pin, x, y, 270, sym.pin_length))

    # Bottom pins (pointing up, angle=90)
    for i, pin in enumerate(pins_by_side[PinSide.BOTTOM]):
        x = -half_w + sym.pin_spacing * (i + 0.5) if bottom_count > 0 else 0
        x = round(x / 1.27) * 1.27
        y = -half_h - sym.pin_length
        pin_lines.append(_gen_pin_sexp(pin, x, y, 90, sym.pin_length))

    pins_sexp = "\n".join(pin_lines)

    # Build complete symbol
    sexp = f'''(kicad_symbol_lib
\t(version 20231120)
\t(generator "create_symbol.py")
\t(generator_version "1.0")
\t(symbol "{sym.name}"
\t\t(exclude_from_sim no)
\t\t(in_bom yes)
\t\t(on_board yes)
\t\t(property "Reference" "{sym.reference}"
\t\t\t(at 0 {half_h + 2.54:.2f} 0)
\t\t\t(effects
\t\t\t\t(font
\t\t\t\t\t(size 1.27 1.27)
\t\t\t\t)
\t\t\t)
\t\t)
\t\t(property "Value" "{sym.value}"
\t\t\t(at 0 {-half_h - 2.54:.2f} 0)
\t\t\t(effects
\t\t\t\t(font
\t\t\t\t\t(size 1.27 1.27)
\t\t\t\t)
\t\t\t)
\t\t)
\t\t(property "Footprint" "{sym.footprint}"
\t\t\t(at 0 {-half_h - 5.08:.2f} 0)
\t\t\t(effects
\t\t\t\t(font
\t\t\t\t\t(size 1.27 1.27)
\t\t\t\t)
\t\t\t\t(hide yes)
\t\t\t)
\t\t)
\t\t(property "Datasheet" "{sym.datasheet}"
\t\t\t(at 0 {-half_h - 7.62:.2f} 0)
\t\t\t(effects
\t\t\t\t(font
\t\t\t\t\t(size 1.27 1.27)
\t\t\t\t)
\t\t\t\t(hide yes)
\t\t\t)
\t\t)
\t\t(property "Description" "{sym.description}"
\t\t\t(at 0 {-half_h - 10.16:.2f} 0)
\t\t\t(effects
\t\t\t\t(font
\t\t\t\t\t(size 1.27 1.27)
\t\t\t\t)
\t\t\t\t(hide yes)
\t\t\t)
\t\t)
\t\t(property "ki_keywords" "{sym.keywords}"
\t\t\t(at 0 0 0)
\t\t\t(effects
\t\t\t\t(font
\t\t\t\t\t(size 1.27 1.27)
\t\t\t\t)
\t\t\t\t(hide yes)
\t\t\t)
\t\t)
\t\t(symbol "{sym.name}_0_1"
\t\t\t(rectangle
\t\t\t\t(start {-half_w:.2f} {half_h:.2f})
\t\t\t\t(end {half_w:.2f} {-half_h:.2f})
\t\t\t\t(stroke
\t\t\t\t\t(width 0.254)
\t\t\t\t\t(type default)
\t\t\t\t)
\t\t\t\t(fill
\t\t\t\t\t(type background)
\t\t\t\t)
\t\t\t)
\t\t)
\t\t(symbol "{sym.name}_1_1"
{pins_sexp}
\t\t)
\t)
)'''

    return sexp


def _gen_pin_sexp(pin: PinDef, x: float, y: float, angle: float, length: float) -> str:
    """Generate S-expression for a single pin."""

    hidden = "\n\t\t\t\t(hide yes)" if pin.hidden else ""

    return f'''\t\t\t(pin {pin.pin_type.value} {pin.style.value}
\t\t\t\t(at {x:.2f} {y:.2f} {angle:.0f})
\t\t\t\t(length {length:.2f})
\t\t\t\t(name "{pin.name}"
\t\t\t\t\t(effects
\t\t\t\t\t\t(font
\t\t\t\t\t\t\t(size 1.27 1.27)
\t\t\t\t\t\t)
\t\t\t\t\t)
\t\t\t\t)
\t\t\t\t(number "{pin.number}"
\t\t\t\t\t(effects
\t\t\t\t\t\t(font
\t\t\t\t\t\t\t(size 1.27 1.27)
\t\t\t\t\t\t)
\t\t\t\t\t){hidden}
\t\t\t\t)
\t\t\t)'''


# Package templates for common IC configurations
PACKAGE_TEMPLATES = {
    "dip8": {
        "pins": 8,
        "layout": "dual",  # Pins on left and right
        "left_pins": [1, 2, 3, 4],
        "right_pins": [8, 7, 6, 5],  # Reversed (standard DIP)
    },
    "dip14": {
        "pins": 14,
        "layout": "dual",
        "left_pins": list(range(1, 8)),
        "right_pins": list(range(14, 7, -1)),
    },
    "dip16": {
        "pins": 16,
        "layout": "dual",
        "left_pins": list(range(1, 9)),
        "right_pins": list(range(16, 8, -1)),
    },
    "soic8": {
        "pins": 8,
        "layout": "dual",
        "left_pins": [1, 2, 3, 4],
        "right_pins": [8, 7, 6, 5],
    },
    "soic14": {
        "pins": 14,
        "layout": "dual",
        "left_pins": list(range(1, 8)),
        "right_pins": list(range(14, 7, -1)),
    },
    "soic16": {
        "pins": 16,
        "layout": "dual",
        "left_pins": list(range(1, 9)),
        "right_pins": list(range(16, 8, -1)),
    },
    "tssop20": {
        "pins": 20,
        "layout": "dual",
        "left_pins": list(range(1, 11)),
        "right_pins": list(range(20, 10, -1)),
    },
    "tssop28": {
        "pins": 28,
        "layout": "dual",
        "left_pins": list(range(1, 15)),
        "right_pins": list(range(28, 14, -1)),
    },
    "qfp32": {
        "pins": 32,
        "layout": "quad",
        "left_pins": list(range(1, 9)),
        "bottom_pins": list(range(9, 17)),
        "right_pins": list(range(17, 25)),
        "top_pins": list(range(25, 33)),
    },
    "qfp48": {
        "pins": 48,
        "layout": "quad",
        "left_pins": list(range(1, 13)),
        "bottom_pins": list(range(13, 25)),
        "right_pins": list(range(25, 37)),
        "top_pins": list(range(37, 49)),
    },
    "qfp64": {
        "pins": 64,
        "layout": "quad",
        "left_pins": list(range(1, 17)),
        "bottom_pins": list(range(17, 33)),
        "right_pins": list(range(33, 49)),
        "top_pins": list(range(49, 65)),
    },
}


def apply_template(sym: SymbolDef, template_name: str):
    """Apply a package template to pin layout."""
    if template_name not in PACKAGE_TEMPLATES:
        raise ValueError(
            f"Unknown template: {template_name}. Available: {list(PACKAGE_TEMPLATES.keys())}"
        )

    template = PACKAGE_TEMPLATES[template_name]

    # Create a map of pin number to pin object
    pin_map = {p.number: p for p in sym.pins}

    # Assign sides based on template
    if template["layout"] == "dual":
        for num in template["left_pins"]:
            if str(num) in pin_map:
                pin_map[str(num)].side = PinSide.LEFT
        for num in template["right_pins"]:
            if str(num) in pin_map:
                pin_map[str(num)].side = PinSide.RIGHT

    elif template["layout"] == "quad":
        for num in template.get("left_pins", []):
            if str(num) in pin_map:
                pin_map[str(num)].side = PinSide.LEFT
        for num in template.get("right_pins", []):
            if str(num) in pin_map:
                pin_map[str(num)].side = PinSide.RIGHT
        for num in template.get("top_pins", []):
            if str(num) in pin_map:
                pin_map[str(num)].side = PinSide.TOP
        for num in template.get("bottom_pins", []):
            if str(num) in pin_map:
                pin_map[str(num)].side = PinSide.BOTTOM


def create_pins_from_template(template_name: str) -> list[PinDef]:
    """Create placeholder pins from a template."""
    if template_name not in PACKAGE_TEMPLATES:
        raise ValueError(f"Unknown template: {template_name}")

    template = PACKAGE_TEMPLATES[template_name]
    pins = []

    for i in range(1, template["pins"] + 1):
        side = PinSide.LEFT  # Default

        if template["layout"] == "dual":
            if i in template["left_pins"]:
                side = PinSide.LEFT
            elif i in template["right_pins"]:
                side = PinSide.RIGHT
        elif template["layout"] == "quad":
            if i in template.get("left_pins", []):
                side = PinSide.LEFT
            elif i in template.get("right_pins", []):
                side = PinSide.RIGHT
            elif i in template.get("top_pins", []):
                side = PinSide.TOP
            elif i in template.get("bottom_pins", []):
                side = PinSide.BOTTOM

        pins.append(
            PinDef(
                number=str(i),
                name=f"P{i}",
                pin_type=PinType.PASSIVE,
                side=side,
            )
        )

    return pins


def print_pin_summary(sym: SymbolDef):
    """Print a summary of pins grouped by type and side."""
    print(f"\nSymbol: {sym.name}")
    print(f"  Reference: {sym.reference}")
    print(f"  Total pins: {len(sym.pins)}")

    # Group by type
    by_type = {}
    for pin in sym.pins:
        by_type.setdefault(pin.pin_type.value, []).append(pin)

    print("\n  Pins by type:")
    for ptype, pins in sorted(by_type.items()):
        pin_list = ", ".join(f"{p.name}({p.number})" for p in pins)
        print(f"    {ptype}: {pin_list}")

    # Group by side
    by_side = {}
    for pin in sym.pins:
        side = pin.side or PinSide.LEFT
        by_side.setdefault(side.value, []).append(pin)

    print("\n  Pins by side:")
    for side, pins in sorted(by_side.items()):
        pin_list = ", ".join(f"{p.name}({p.number})" for p in pins)
        print(f"    {side}: {pin_list}")


def main():
    parser = argparse.ArgumentParser(
        description="Generate KiCad symbol library files",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # From JSON definition
  %(prog)s --json tpa3116d2.json --output Amplifier.kicad_sym

  # From CSV (number,name,type columns)
  %(prog)s --csv pcm5122.csv --name PCM5122 --output DAC.kicad_sym

  # From template with names added
  %(prog)s --template soic8 --name NE555 --output Timer.kicad_sym

  # Detect pin types from names
  %(prog)s --csv pins.csv --name MyChip --auto-types --output MyLib.kicad_sym
""",
    )

    input_group = parser.add_mutually_exclusive_group(required=True)
    input_group.add_argument("--json", type=Path, help="JSON pin definition file")
    input_group.add_argument("--csv", type=Path, help="CSV pin definition file")
    input_group.add_argument(
        "--template", choices=list(PACKAGE_TEMPLATES.keys()), help="Use package template"
    )
    input_group.add_argument("--interactive", action="store_true", help="Enter pins interactively")
    input_group.add_argument("--text", type=Path, help="Datasheet-style text file")

    parser.add_argument(
        "--name", "-n", required=False, help="Symbol name (required for CSV/template/interactive)"
    )
    parser.add_argument("--output", "-o", type=Path, required=True, help="Output .kicad_sym file")
    parser.add_argument("--reference", "-r", default="U", help="Reference designator (default: U)")
    parser.add_argument("--footprint", "-f", default="", help="Default footprint")
    parser.add_argument("--description", "-d", default="", help="Symbol description")
    parser.add_argument("--datasheet", default="", help="Datasheet URL")
    parser.add_argument("--keywords", "-k", default="", help="Search keywords")
    parser.add_argument(
        "--auto-types", action="store_true", help="Auto-detect pin types from names"
    )
    parser.add_argument(
        "--auto-sides", action="store_true", help="Auto-assign pin sides based on type"
    )
    parser.add_argument(
        "--apply-template",
        choices=list(PACKAGE_TEMPLATES.keys()),
        help="Apply layout from package template",
    )
    parser.add_argument(
        "--summary", action="store_true", help="Print pin summary before generating"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Show what would be generated without writing"
    )

    args = parser.parse_args()

    # Check name is provided for non-JSON inputs
    if not args.json and not args.name:
        parser.error("--name is required for CSV, template, text, and interactive modes")

    # Parse input
    if args.json:
        sym = parse_json(args.json)
    elif args.csv:
        sym = parse_csv(args.csv, args.name)
    elif args.template:
        pins = create_pins_from_template(args.template)
        sym = SymbolDef(name=args.name, pins=pins)
    elif args.text:
        text = args.text.read_text()
        sym = parse_datasheet_text(text, args.name)
    elif args.interactive:
        print("Enter pins (number name [type]), one per line. Empty line to finish:")
        lines = []
        while True:
            try:
                line = input()
                if not line.strip():
                    break
                lines.append(line)
            except EOFError:
                break
        text = "\n".join(lines)
        sym = parse_datasheet_text(text, args.name)
    else:
        parser.error("No input source specified")
        return

    # Apply overrides
    if args.reference:
        sym.reference = args.reference
    if args.footprint:
        sym.footprint = args.footprint
    if args.description:
        sym.description = args.description
    if args.datasheet:
        sym.datasheet = args.datasheet
    if args.keywords:
        sym.keywords = args.keywords

    # Auto-detect types if requested
    if args.auto_types:
        for pin in sym.pins:
            if pin.pin_type == PinType.PASSIVE:
                pin.pin_type = detect_pin_type(pin.name)
                pin.style = detect_pin_style(pin.name, pin.pin_type)

    # Auto-assign sides if requested
    if args.auto_sides:
        for pin in sym.pins:
            if pin.side is None:
                pin.side = detect_pin_side(pin.name, pin.pin_type)

    # Apply template layout
    if args.apply_template:
        apply_template(sym, args.apply_template)

    # Print summary if requested
    if args.summary or args.dry_run:
        print_pin_summary(sym)

    # Generate S-expression
    sexp = generate_symbol_sexp(sym)

    if args.dry_run:
        print("\n--- Generated S-expression ---")
        print(sexp[:2000] + "..." if len(sexp) > 2000 else sexp)
        print(f"\n(Total {len(sexp)} characters)")
        return

    # Write output
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(sexp)
    print(f"\nWrote {args.output} ({len(sexp)} bytes)")
    print(f"  Symbol: {sym.name}")
    print(f"  Pins: {len(sym.pins)}")


if __name__ == "__main__":
    main()
