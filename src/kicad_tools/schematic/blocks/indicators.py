"""Indicator circuit blocks: LEDs."""

from typing import TYPE_CHECKING

from .base import CircuitBlock

if TYPE_CHECKING:
    from kicad_sch_helper import Schematic


class LEDIndicator(CircuitBlock):
    """
    LED with current-limiting resistor.

    Schematic:
        VCC ──┬── [LED] ── [R] ── GND
              │
            (anode)

    Ports:
        - VCC: Power input (top of LED)
        - GND: Ground (bottom of resistor)
    """

    def __init__(
        self,
        sch: "Schematic",
        x: float,
        y: float,
        ref_prefix: str = "D",
        label: str = "LED",
        resistor_value: str = "330R",
        led_symbol: str = "Device:LED",
        resistor_symbol: str = "Device:R",
        vertical: bool = True,
    ):
        """
        Create an LED indicator.

        Args:
            sch: Schematic to add to
            x: X coordinate
            y: Y coordinate (of LED center)
            ref_prefix: Reference designator prefix (e.g., "D1" or just "D")
            label: Value label for LED (e.g., "PWR", "ACT")
            resistor_value: Resistor value string
            led_symbol: KiCad symbol for LED
            resistor_symbol: KiCad symbol for resistor
            vertical: If True, LED is vertical (rotated 90°)
        """
        super().__init__(sch, x, y)

        # Parse reference prefix
        d_ref = ref_prefix if ref_prefix[-1].isdigit() else ref_prefix
        r_num = ref_prefix[-1] if ref_prefix[-1].isdigit() else "1"
        r_ref = f"R{r_num}"

        # Component spacing
        led_resistor_spacing = 15  # mm between LED and resistor centers

        # Place LED
        rotation = 90 if vertical else 0
        self.led = sch.add_symbol(led_symbol, x, y, d_ref, label, rotation=rotation)

        # Place resistor below LED (if vertical)
        if vertical:
            r_y = y + led_resistor_spacing
        else:
            r_y = y
        self.resistor = sch.add_symbol(resistor_symbol, x, r_y, r_ref, resistor_value)

        self.components = {"LED": self.led, "R": self.resistor}

        # Wire LED cathode to resistor
        led_cathode = self.led.pin_position("K")
        r_pin1 = self.resistor.pin_position("1")
        sch.add_wire(led_cathode, r_pin1)

        # Define ports
        led_anode = self.led.pin_position("A")
        r_pin2 = self.resistor.pin_position("2")

        self.ports = {
            "VCC": led_anode,
            "GND": r_pin2,
        }

    def connect_to_rails(self, vcc_rail_y: float, gnd_rail_y: float, add_junctions: bool = True):
        """
        Connect LED to power rails.

        Args:
            vcc_rail_y: Y coordinate of VCC rail
            gnd_rail_y: Y coordinate of GND rail
            add_junctions: Whether to add junction markers
        """
        sch = self.schematic
        vcc_pos = self.ports["VCC"]
        gnd_pos = self.ports["GND"]

        # Connect anode to VCC rail
        sch.add_wire(vcc_pos, (vcc_pos[0], vcc_rail_y))

        # Connect resistor to GND rail
        sch.add_wire(gnd_pos, (gnd_pos[0], gnd_rail_y))

        if add_junctions:
            sch.add_junction(vcc_pos[0], vcc_rail_y)
            sch.add_junction(gnd_pos[0], gnd_rail_y)


# Factory functions


def create_power_led(
    sch: "Schematic",
    x: float,
    y: float,
    ref: str = "D1",
) -> LEDIndicator:
    """Create a power indicator LED (green, 330R)."""
    return LEDIndicator(sch, x, y, ref_prefix=ref, label="PWR", resistor_value="330R")


def create_status_led(
    sch: "Schematic",
    x: float,
    y: float,
    ref: str = "D2",
) -> LEDIndicator:
    """Create a status/debug LED (generic, 330R)."""
    return LEDIndicator(sch, x, y, ref_prefix=ref, label="STATUS", resistor_value="330R")
