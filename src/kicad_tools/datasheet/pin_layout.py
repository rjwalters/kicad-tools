"""
Pin layout engine for automatic symbol generation.

Provides three layout modes for organizing pins on a symbol:
- functional: Groups pins by type (power, GPIO, communication, etc.)
- physical: Arranges pins to match the IC package layout
- simple: Power pins on top/bottom, signals on left/right
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

from .pins import ExtractedPin


class LayoutStyle(str, Enum):
    """Available pin layout styles."""

    FUNCTIONAL = "functional"
    PHYSICAL = "physical"
    SIMPLE = "simple"


# Pin grouping categories for functional layout
PIN_GROUPS: dict[str, list[str]] = {
    "power_positive": [
        "VCC",
        "VDD",
        "VBAT",
        "VDDA",
        "AVDD",
        "DVDD",
        "IOVDD",
        "3V3",
        "5V",
        "12V",
        "V+",
        "VBUS",
        "VCAP",
        "VREF",
    ],
    "power_negative": [
        "GND",
        "VSS",
        "GNDA",
        "AVSS",
        "DGND",
        "AGND",
        "PGND",
        "V-",
        "EPAD",
        "EP",
    ],
    "reset": ["RST", "RESET", "NRST", "RSTN", "RST_N", "RESETN", "~RESET", "~RST"],
    "oscillator": [
        "XTAL",
        "XTALI",
        "XTALO",
        "OSC",
        "OSC_IN",
        "OSC_OUT",
        "CLKIN",
        "CLKOUT",
        "XIN",
        "XOUT",
        "HSE",
        "LSE",
    ],
    "boot": ["BOOT", "BOOT0", "BOOT1"],
    "analog": ["ADC", "DAC", "AIN", "AOUT", "AN", "VREF", "AREF"],
    "spi": ["SPI", "MOSI", "MISO", "SCK", "SCLK", "SS", "CS", "NSS"],
    "i2c": ["I2C", "SDA", "SCL", "SDIO", "SMBA"],
    "uart": ["UART", "TX", "RX", "TXD", "RXD", "USART", "CTS", "RTS"],
    "usb": ["USB", "DP", "DM", "D+", "D-", "VBUS", "ID"],
    "can": ["CAN", "CANRX", "CANTX", "CANH", "CANL"],
    "jtag": ["JTAG", "TDI", "TDO", "TMS", "TCK", "TRST", "SWDIO", "SWCLK", "SWO"],
    "gpio": [],  # Catch-all for GPIO pins (PAx, PBx, GPIOx, etc.)
}

# Port pattern for GPIO grouping (PA0, PB1, GPIO0, etc.)
PORT_PATTERNS = [
    re.compile(r"^P([A-K])(\d+)$", re.IGNORECASE),  # PA0, PB1, etc.
    re.compile(r"^GPIO(\d+)$", re.IGNORECASE),  # GPIO0, GPIO1, etc.
    re.compile(r"^IO(\d+)$", re.IGNORECASE),  # IO0, IO1, etc.
]


@dataclass
class PinPosition:
    """Calculated position for a pin in the symbol."""

    number: str
    name: str
    x: float
    y: float
    rotation: float  # 0=right, 90=up, 180=left, 270=down
    pin_type: str


@dataclass
class SymbolLayout:
    """Result of pin layout calculation."""

    style: LayoutStyle
    pin_positions: list[PinPosition]
    symbol_width: float
    symbol_height: float
    body_rect: tuple[float, float, float, float]  # x1, y1, x2, y2


def _classify_pin_group(pin: ExtractedPin) -> str:
    """Determine which group a pin belongs to based on its name."""
    name_upper = pin.name.upper()

    # Check against each group's patterns
    for group, patterns in PIN_GROUPS.items():
        if group == "gpio":
            continue  # Skip GPIO, check last

        for pattern in patterns:
            if pattern in name_upper or name_upper.startswith(pattern):
                return group

    # Check for port pins (PA0, PB1, etc.)
    for port_pattern in PORT_PATTERNS:
        if match := port_pattern.match(pin.name):
            if len(match.groups()) >= 1:
                port = match.group(1).upper()
                return f"port_{port.lower()}"

    # Default to gpio for unclassified pins
    return "gpio"


def _get_port_letter(pin: ExtractedPin) -> str | None:
    """Extract port letter from pin name (e.g., 'A' from 'PA0')."""
    for port_pattern in PORT_PATTERNS:
        if match := port_pattern.match(pin.name):
            if len(match.groups()) >= 1:
                return match.group(1).upper()
    return None


def _sort_pins_by_number(pins: list[ExtractedPin]) -> list[ExtractedPin]:
    """Sort pins by their number, handling both numeric and alphanumeric."""

    def pin_sort_key(pin: ExtractedPin) -> tuple[int, str]:
        # Try to extract numeric portion
        num_match = re.match(r"(\d+)", pin.number)
        if num_match:
            return (int(num_match.group(1)), pin.number)
        return (999, pin.number)

    return sorted(pins, key=pin_sort_key)


def _sort_pins_by_name(pins: list[ExtractedPin]) -> list[ExtractedPin]:
    """Sort pins by name, handling port numbering (PA0 < PA1 < PA10)."""

    def pin_sort_key(pin: ExtractedPin) -> tuple[str, int]:
        # Try to extract port and number
        for port_pattern in PORT_PATTERNS:
            if match := port_pattern.match(pin.name):
                groups = match.groups()
                if len(groups) >= 2:
                    return (groups[0].upper(), int(groups[1]))
                elif len(groups) == 1:
                    return ("", int(groups[0]))
        # Fallback to alphabetic
        return (pin.name, 0)

    return sorted(pins, key=pin_sort_key)


class PinLayoutEngine:
    """
    Engine for calculating pin positions on a symbol.

    Usage:
        engine = PinLayoutEngine()
        layout = engine.calculate_layout(pins, style="functional")
    """

    def __init__(
        self,
        pin_spacing: float = 2.54,
        pin_length: float = 2.54,
        min_width: float = 10.16,
        min_height: float = 5.08,
    ):
        """
        Initialize the layout engine.

        Args:
            pin_spacing: Vertical spacing between pins (default 2.54mm = 100 mil)
            pin_length: Length of pin lines (default 2.54mm)
            min_width: Minimum symbol body width (default 10.16mm)
            min_height: Minimum symbol body height (default 5.08mm)
        """
        self.pin_spacing = pin_spacing
        self.pin_length = pin_length
        self.min_width = min_width
        self.min_height = min_height

    def calculate_layout(
        self,
        pins: list[ExtractedPin],
        style: str | LayoutStyle = LayoutStyle.FUNCTIONAL,
        package_pins: int | None = None,
    ) -> SymbolLayout:
        """
        Calculate pin positions for a symbol.

        Args:
            pins: List of extracted pins
            style: Layout style ("functional", "physical", or "simple")
            package_pins: Total pin count for physical layout (used for arrangement)

        Returns:
            SymbolLayout with calculated positions
        """
        if isinstance(style, str):
            style = LayoutStyle(style)

        if style == LayoutStyle.FUNCTIONAL:
            return self._functional_layout(pins)
        elif style == LayoutStyle.PHYSICAL:
            return self._physical_layout(pins, package_pins or len(pins))
        else:
            return self._simple_layout(pins)

    def _functional_layout(self, pins: list[ExtractedPin]) -> SymbolLayout:
        """
        Arrange pins by functional groups.

        Power pins on top, ground on bottom, ports/signals on sides.
        """
        # Group pins
        groups: dict[str, list[ExtractedPin]] = {}
        for pin in pins:
            group = _classify_pin_group(pin)
            if group not in groups:
                groups[group] = []
            groups[group].append(pin)

        # Sort pins within each group
        for group in groups:
            groups[group] = _sort_pins_by_name(groups[group])

        # Assign pins to sides
        top_pins: list[ExtractedPin] = []
        bottom_pins: list[ExtractedPin] = []
        left_pins: list[ExtractedPin] = []
        right_pins: list[ExtractedPin] = []

        # Power positive goes on top
        if "power_positive" in groups:
            top_pins.extend(groups["power_positive"])

        # Power negative goes on bottom
        if "power_negative" in groups:
            bottom_pins.extend(groups["power_negative"])

        # Collect port pins (sorted by port letter)
        port_groups = sorted([g for g in groups if g.startswith("port_")])

        # Distribute port groups to left and right
        for i, port_group in enumerate(port_groups):
            if i % 2 == 0:
                left_pins.extend(groups[port_group])
            else:
                right_pins.extend(groups[port_group])

        # Add remaining pins to left/right based on type
        remaining_groups = [
            "analog",
            "spi",
            "i2c",
            "uart",
            "usb",
            "can",
            "jtag",
            "reset",
            "oscillator",
            "boot",
            "gpio",
        ]

        for group in remaining_groups:
            if group in groups:
                # Balance left and right
                if len(left_pins) <= len(right_pins):
                    left_pins.extend(groups[group])
                else:
                    right_pins.extend(groups[group])

        return self._calculate_positions(top_pins, bottom_pins, left_pins, right_pins)

    def _physical_layout(self, pins: list[ExtractedPin], total_pins: int) -> SymbolLayout:
        """
        Arrange pins to match physical IC package layout.

        For QFP/LQFP: pins arranged clockwise from pin 1
        For DIP: pins on left and right
        """
        sorted_pins = _sort_pins_by_number(pins)

        # Determine if it's a dual-row or quad-row package
        # DIP/SOIC: 2 sides, QFP/BGA: 4 sides
        is_quad = total_pins >= 20 and total_pins % 4 == 0

        if is_quad:
            # QFP-style: distribute to 4 sides
            pins_per_side = total_pins // 4

            # Pin 1 typically at top-left, going counterclockwise
            # Left side: pins 1 to pins_per_side (bottom to top)
            # Bottom side: pins pins_per_side+1 to 2*pins_per_side
            # Right side: 2*pins_per_side+1 to 3*pins_per_side (top to bottom)
            # Top side: 3*pins_per_side+1 to 4*pins_per_side

            left_pins = sorted_pins[:pins_per_side]
            bottom_pins = sorted_pins[pins_per_side : 2 * pins_per_side]
            right_pins = sorted_pins[2 * pins_per_side : 3 * pins_per_side]
            top_pins = sorted_pins[3 * pins_per_side :]

            # Reverse right side to maintain physical order
            right_pins = list(reversed(right_pins))
            top_pins = list(reversed(top_pins))

        else:
            # DIP-style: left and right
            half = len(sorted_pins) // 2
            left_pins = sorted_pins[:half]
            right_pins = list(reversed(sorted_pins[half:]))
            top_pins = []
            bottom_pins = []

        return self._calculate_positions(top_pins, bottom_pins, left_pins, right_pins)

    def _simple_layout(self, pins: list[ExtractedPin]) -> SymbolLayout:
        """
        Simple layout: power top/bottom, all other pins left/right.
        """
        top_pins: list[ExtractedPin] = []
        bottom_pins: list[ExtractedPin] = []
        left_pins: list[ExtractedPin] = []
        right_pins: list[ExtractedPin] = []

        for pin in pins:
            pin_type_lower = pin.type.lower()
            name_upper = pin.name.upper()

            if pin_type_lower == "power_in":
                # Check if it's ground
                is_ground = any(
                    g in name_upper for g in ["GND", "VSS", "GNDA", "AVSS", "DGND", "AGND"]
                )
                if is_ground:
                    bottom_pins.append(pin)
                else:
                    top_pins.append(pin)
            elif pin_type_lower == "power_out":
                top_pins.append(pin)
            elif pin_type_lower == "input":
                left_pins.append(pin)
            elif pin_type_lower == "output":
                right_pins.append(pin)
            else:
                # Balance bidirectional and other pins
                if len(left_pins) <= len(right_pins):
                    left_pins.append(pin)
                else:
                    right_pins.append(pin)

        # Sort by name
        top_pins = _sort_pins_by_name(top_pins)
        bottom_pins = _sort_pins_by_name(bottom_pins)
        left_pins = _sort_pins_by_name(left_pins)
        right_pins = _sort_pins_by_name(right_pins)

        return self._calculate_positions(top_pins, bottom_pins, left_pins, right_pins)

    def _calculate_positions(
        self,
        top_pins: list[ExtractedPin],
        bottom_pins: list[ExtractedPin],
        left_pins: list[ExtractedPin],
        right_pins: list[ExtractedPin],
    ) -> SymbolLayout:
        """Calculate actual positions for pins on each side."""
        positions: list[PinPosition] = []

        # Calculate required dimensions
        max_side_pins = max(len(left_pins), len(right_pins))
        max_tb_pins = max(len(top_pins), len(bottom_pins))

        # Calculate symbol body size
        body_height = max(
            (max_side_pins + 1) * self.pin_spacing,
            self.min_height,
        )
        body_width = max(
            (max_tb_pins + 1) * self.pin_spacing,
            self.min_width,
        )

        # Center the body around origin
        half_width = body_width / 2
        half_height = body_height / 2

        # Left side pins (pointing right into symbol, rotation = 0)
        if left_pins:
            y_start = half_height - self.pin_spacing
            for i, pin in enumerate(left_pins):
                y = y_start - i * self.pin_spacing
                positions.append(
                    PinPosition(
                        number=pin.number,
                        name=pin.name,
                        x=-(half_width + self.pin_length),
                        y=y,
                        rotation=0,
                        pin_type=pin.type,
                    )
                )

        # Right side pins (pointing left into symbol, rotation = 180)
        if right_pins:
            y_start = half_height - self.pin_spacing
            for i, pin in enumerate(right_pins):
                y = y_start - i * self.pin_spacing
                positions.append(
                    PinPosition(
                        number=pin.number,
                        name=pin.name,
                        x=half_width + self.pin_length,
                        y=y,
                        rotation=180,
                        pin_type=pin.type,
                    )
                )

        # Top side pins (pointing down into symbol, rotation = 270)
        if top_pins:
            x_start = -half_width + self.pin_spacing
            for i, pin in enumerate(top_pins):
                x = x_start + i * self.pin_spacing
                positions.append(
                    PinPosition(
                        number=pin.number,
                        name=pin.name,
                        x=x,
                        y=half_height + self.pin_length,
                        rotation=270,
                        pin_type=pin.type,
                    )
                )

        # Bottom side pins (pointing up into symbol, rotation = 90)
        if bottom_pins:
            x_start = -half_width + self.pin_spacing
            for i, pin in enumerate(bottom_pins):
                x = x_start + i * self.pin_spacing
                positions.append(
                    PinPosition(
                        number=pin.number,
                        name=pin.name,
                        x=x,
                        y=-(half_height + self.pin_length),
                        rotation=90,
                        pin_type=pin.type,
                    )
                )

        return SymbolLayout(
            style=LayoutStyle.FUNCTIONAL,  # Will be overwritten by caller
            pin_positions=positions,
            symbol_width=body_width,
            symbol_height=body_height,
            body_rect=(-half_width, -half_height, half_width, half_height),
        )

    def detect_multi_unit(self, pins: list[ExtractedPin]) -> dict[int, list[ExtractedPin]]:
        """
        Detect if pins form a multi-unit symbol (e.g., quad op-amp).

        Looks for patterns like:
        - Repeated functional groups (IN+, IN-, OUT repeated 4 times)
        - Shared power pins

        Returns:
            Dict mapping unit number to pins for that unit.
            Returns {1: pins} if not a multi-unit symbol.
        """
        # Look for patterns suggesting multiple units
        # Common multi-unit patterns: op-amps, comparators, logic gates

        # Count potential unit indicators
        in_plus_count = sum(
            1 for p in pins if any(pat in p.name.upper() for pat in ["IN+", "INP", "+IN", "INA"])
        )
        in_minus_count = sum(
            1 for p in pins if any(pat in p.name.upper() for pat in ["IN-", "INM", "-IN", "INB"])
        )
        out_count = sum(
            1 for p in pins if any(pat in p.name.upper() for pat in ["OUT", "OUTPUT", "Y", "Q"])
        )

        # Check for balanced unit counts
        if in_plus_count == in_minus_count == out_count and in_plus_count >= 2:
            num_units = in_plus_count

            # Group pins by unit
            units: dict[int, list[ExtractedPin]] = {}
            power_pins: list[ExtractedPin] = []

            for pin in pins:
                if pin.type in ("power_in", "power_out"):
                    power_pins.append(pin)
                    continue

                # Try to find unit number in name
                match = re.search(r"(\d+)", pin.name)
                if match:
                    unit_num = int(match.group(1))
                    if 1 <= unit_num <= num_units:
                        if unit_num not in units:
                            units[unit_num] = []
                        units[unit_num].append(pin)
                        continue

                # Assign based on position/order
                unit_num = (len([p for u in units.values() for p in u]) % num_units) + 1
                if unit_num not in units:
                    units[unit_num] = []
                units[unit_num].append(pin)

            # Add power pins to a shared unit (unit 1)
            if 1 not in units:
                units[1] = []
            units[1].extend(power_pins)

            return units

        # Not a multi-unit symbol
        return {1: pins}
