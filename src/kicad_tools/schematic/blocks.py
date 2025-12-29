#!/usr/bin/env python3
"""
KiCad Circuit Blocks

Reusable circuit block abstractions for common schematic patterns.
Each block encapsulates component placement, wiring, and port definitions.

Usage:
    from kicad_circuit_blocks import LDOBlock, LEDIndicator, DecouplingCaps

    # Create an LDO power supply section
    ldo = LDOBlock(
        sch, x=100, y=80,
        ref_prefix="U1",
        input_voltage=5.0,
        output_voltage=3.3,
        input_cap="10uF",
        output_caps=["10uF", "100nF"]
    )

    # Connect to rails
    sch.add_wire(ldo.ports["VIN"], (100, RAIL_5V))
    sch.add_wire(ldo.ports["VOUT"], (100, RAIL_3V3))

    # Add LED indicator
    led = LEDIndicator(sch, x=150, y=80, ref_prefix="D1", label="PWR")
    led.connect_to_rails(RAIL_3V3, RAIL_GND)
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kicad_sch_helper import Schematic, SymbolInstance


@dataclass
class Port:
    """A connection point on a circuit block."""

    name: str
    x: float
    y: float
    direction: str = "passive"  # input, output, bidirectional, passive, power

    def pos(self) -> tuple[float, float]:
        """Get position as tuple."""
        return (self.x, self.y)


class CircuitBlock:
    """
    Base class for reusable circuit blocks.

    A circuit block represents a common subcircuit pattern that can be
    instantiated multiple times in a schematic. Each block:
    - Places its components at specified coordinates
    - Wires internal connections
    - Exposes ports for external connections

    Subclasses should implement their setup logic in __init__, calling
    super().__init__() first and then setting up components, wiring, and ports.
    """

    def __init__(self):
        """Initialize base attributes."""
        self.schematic: "Schematic" = None
        self.x: float = 0
        self.y: float = 0
        self.ports: dict[str, tuple[float, float]] = {}
        self.components: dict[str, "SymbolInstance"] = {}

    def port(self, name: str) -> tuple[float, float]:
        """Get a port position by name."""
        if name not in self.ports:
            available = list(self.ports.keys())
            raise KeyError(f"Port '{name}' not found. Available: {available}")
        return self.ports[name]


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
        super().__init__()
        self.schematic = sch
        self.x = x
        self.y = y

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


class DecouplingCaps(CircuitBlock):
    """
    Bank of decoupling capacitors on a power rail.

    Schematic:
        VCC ──┬──┬──┬── ...
              │  │  │
             [C1][C2][C3]
              │  │  │
        GND ──┴──┴──┴── ...

    Ports:
        - VCC: Power rail connection
        - GND: Ground rail connection
    """

    def __init__(
        self,
        sch: "Schematic",
        x: float,
        y: float,
        values: list[str],
        ref_start: int = 1,
        ref_prefix: str = "C",
        spacing: float = 15,
        cap_symbol: str = "Device:C",
    ):
        """
        Create a bank of decoupling capacitors.

        Args:
            sch: Schematic to add to
            x: X coordinate of first capacitor
            y: Y coordinate (center of caps)
            values: List of capacitor values (e.g., ["10uF", "100nF"])
            ref_start: Starting reference number
            ref_prefix: Reference designator prefix
            spacing: Horizontal spacing between caps
            cap_symbol: KiCad symbol for capacitors
        """
        super().__init__()
        self.schematic = sch
        self.x = x
        self.y = y
        self.caps = []

        # Place capacitors
        for i, value in enumerate(values):
            cap_x = x + i * spacing
            ref = f"{ref_prefix}{ref_start + i}"
            cap = sch.add_symbol(cap_symbol, cap_x, y, ref, value)
            self.caps.append(cap)

        self.components = {f"C{i + 1}": cap for i, cap in enumerate(self.caps)}

        # Calculate port positions (at first and last cap)
        first_cap = self.caps[0]
        last_cap = self.caps[-1] if len(self.caps) > 1 else first_cap

        # Ports at the rail level (top/bottom of caps)
        p1 = first_cap.pin_position("1")
        p2 = first_cap.pin_position("2")

        self.ports = {
            "VCC": p1,
            "GND": p2,
            "VCC_END": last_cap.pin_position("1"),
            "GND_END": last_cap.pin_position("2"),
        }

        # Store for rail connections
        self._vcc_y = p1[1]
        self._gnd_y = p2[1]

    def connect_to_rails(
        self, vcc_rail_y: float, gnd_rail_y: float, wire_between_caps: bool = True
    ):
        """
        Connect all caps to power rails.

        Args:
            vcc_rail_y: Y coordinate of VCC rail
            gnd_rail_y: Y coordinate of GND rail
            wire_between_caps: If True, add horizontal wires between caps
        """
        sch = self.schematic

        for cap in self.caps:
            sch.wire_decoupling_cap(cap, vcc_rail_y, gnd_rail_y)


class LDOBlock(CircuitBlock):
    """
    Low Dropout Regulator with input and output capacitors.

    Schematic:
        VIN ──┬── [C_in] ──┬── LDO ──┬── [C_out1] ──┬── [C_out2] ──┬── VOUT
              │            │    │    │              │              │
              └────────────┼────┴────┼──────────────┼──────────────┘
                           │   EN    │              │
        GND ───────────────┴─────────┴──────────────┴───────────────

    Ports:
        - VIN: Input voltage
        - VOUT: Output voltage
        - GND: Ground
        - EN: Enable (optional, usually tied to VIN)
    """

    def __init__(
        self,
        sch: "Schematic",
        x: float,
        y: float,
        ref: str = "U1",
        value: str = "LDO",
        ldo_symbol: str = "Regulator_Linear:AP2204K-1.5",
        input_cap: str = "10uF",
        output_caps: list[str] = None,
        cap_ref_start: int = 1,
        en_tied_to_vin: bool = True,
    ):
        """
        Create an LDO power supply block.

        Args:
            sch: Schematic to add to
            x: X coordinate of LDO center
            y: Y coordinate of LDO center
            ref: LDO reference designator
            value: LDO value label
            ldo_symbol: KiCad symbol for LDO
            input_cap: Input capacitor value
            output_caps: List of output capacitor values
            cap_ref_start: Starting reference number for caps
            en_tied_to_vin: If True, tie EN pin to VIN
        """
        super().__init__()

        if output_caps is None:
            output_caps = ["10uF", "100nF"]

        self.schematic = sch
        self.x = x
        self.y = y

        # Spacing constants
        cap_spacing = 15
        input_cap_offset = -20  # Left of LDO
        output_cap_offset = 20  # Right of LDO

        # Place LDO
        self.ldo = sch.add_symbol(ldo_symbol, x, y, ref, value)

        # Place input capacitor
        c_in_x = x + input_cap_offset
        c_in_ref = f"C{cap_ref_start}"
        self.input_cap = sch.add_symbol("Device:C", c_in_x, y + 15, c_in_ref, input_cap)

        # Place output capacitors
        self.output_caps = []
        for i, cap_value in enumerate(output_caps):
            c_out_x = x + output_cap_offset + i * cap_spacing
            c_out_ref = f"C{cap_ref_start + 1 + i}"
            cap = sch.add_symbol("Device:C", c_out_x, y + 15, c_out_ref, cap_value)
            self.output_caps.append(cap)

        # Store all components
        self.components = {
            "LDO": self.ldo,
            "C_IN": self.input_cap,
        }
        for i, cap in enumerate(self.output_caps):
            self.components[f"C_OUT{i + 1}"] = cap

        # Get LDO pin positions
        vin_pos = self.ldo.pin_position("VIN")
        vout_pos = self.ldo.pin_position("VOUT")
        gnd_pos = self.ldo.pin_position("GND")
        en_pos = self.ldo.pin_position("EN")

        # Define ports
        self.ports = {
            "VIN": vin_pos,
            "VOUT": vout_pos,
            "GND": gnd_pos,
            "EN": en_pos,
        }

        # Tie EN to VIN if requested
        if en_tied_to_vin:
            # Connect EN to VIN (vertical wire)
            sch.add_wire(en_pos, (en_pos[0], vin_pos[1]))

    def connect_to_rails(
        self,
        vin_rail_y: float,
        vout_rail_y: float,
        gnd_rail_y: float,
        extend_vout_rail_to: float = None,
    ):
        """
        Connect LDO and caps to power rails.

        Args:
            vin_rail_y: Y coordinate of input voltage rail
            vout_rail_y: Y coordinate of output voltage rail
            gnd_rail_y: Y coordinate of ground rail
            extend_vout_rail_to: If set, extend VOUT rail to this X coordinate
        """
        sch = self.schematic

        # Connect LDO VIN to input rail
        sch.wire_to_rail(self.ldo, "VIN", vin_rail_y)

        # Connect LDO VOUT to output rail
        sch.wire_to_rail(self.ldo, "VOUT", vout_rail_y)

        # Connect LDO GND to ground rail
        sch.wire_to_rail(self.ldo, "GND", gnd_rail_y)

        # Wire input cap
        sch.wire_decoupling_cap(self.input_cap, vin_rail_y, gnd_rail_y)

        # Wire output caps
        for cap in self.output_caps:
            sch.wire_decoupling_cap(cap, vout_rail_y, gnd_rail_y)

        # Extend VOUT rail if requested
        if extend_vout_rail_to is not None:
            vout_pos = self.ldo.pin_position("VOUT")
            sch.add_rail(vout_rail_y, vout_pos[0], extend_vout_rail_to)


class OscillatorBlock(CircuitBlock):
    """
    Crystal oscillator with enable and decoupling.

    Schematic:
        VCC ──┬── [C] ──┬── OSC ──── OUT
              │         │    │
              └─────────┤   EN
                        │    │
        GND ────────────┴────┴───────

    Ports:
        - VCC: Power input
        - GND: Ground
        - OUT: Clock output
        - EN: Enable (usually tied to VCC)
    """

    def __init__(
        self,
        sch: "Schematic",
        x: float,
        y: float,
        ref: str = "Y1",
        value: str = "24.576MHz",
        osc_symbol: str = "Oscillator:ASE-xxxMHz",
        decoupling_cap: str = "100nF",
        cap_ref: str = "C1",
        en_tied_to_vcc: bool = True,
    ):
        """
        Create an oscillator block.

        Args:
            sch: Schematic to add to
            x: X coordinate of oscillator center
            y: Y coordinate of oscillator center
            ref: Oscillator reference designator
            value: Frequency value label
            osc_symbol: KiCad symbol for oscillator
            decoupling_cap: Decoupling capacitor value
            cap_ref: Capacitor reference designator
            en_tied_to_vcc: If True, tie EN pin to VCC
        """
        super().__init__()
        self.schematic = sch
        self.x = x
        self.y = y

        # Place oscillator
        self.osc = sch.add_symbol(osc_symbol, x, y, ref, value)

        # Place decoupling cap to the left
        cap_offset = -15
        self.cap = sch.add_symbol("Device:C", x + cap_offset, y + 15, cap_ref, decoupling_cap)

        self.components = {
            "OSC": self.osc,
            "C": self.cap,
        }

        # Get pin positions
        vdd_pos = self.osc.pin_position("Vdd")
        gnd_pos = self.osc.pin_position("GND")
        out_pos = self.osc.pin_position("OUT")
        en_pos = self.osc.pin_position("EN")

        self.ports = {
            "VCC": vdd_pos,
            "GND": gnd_pos,
            "OUT": out_pos,
            "EN": en_pos,
        }

        self._en_tied_to_vcc = en_tied_to_vcc

    def connect_to_rails(
        self,
        vcc_rail_y: float,
        gnd_rail_y: float,
    ):
        """
        Connect oscillator and cap to power rails.

        Args:
            vcc_rail_y: Y coordinate of VCC rail
            gnd_rail_y: Y coordinate of GND rail
        """
        sch = self.schematic

        # Connect Vdd to VCC rail
        vdd_pos = self.ports["VCC"]
        sch.add_wire(vdd_pos, (vdd_pos[0], vcc_rail_y))

        # Connect GND to ground rail
        gnd_pos = self.ports["GND"]
        sch.add_wire(gnd_pos, (gnd_pos[0], gnd_rail_y))

        # Wire decoupling cap
        sch.wire_decoupling_cap(self.cap, vcc_rail_y, gnd_rail_y)

        # Tie EN to VCC rail if requested
        if self._en_tied_to_vcc:
            en_pos = self.ports["EN"]
            sch.add_wire(en_pos, (en_pos[0], vcc_rail_y))

        # Add junctions
        sch.add_junction(vdd_pos[0], vcc_rail_y)
        sch.add_junction(gnd_pos[0], gnd_rail_y)


class DebugHeader(CircuitBlock):
    """
    SWD debug header for ARM Cortex-M microcontrollers.

    Schematic:
        VCC ──── [1] ┐
        SWDIO ── [2] │ Header
        SWCLK ── [3] │
        GND ──── [4] ┘

    Ports:
        - VCC: Power (pin 1)
        - SWDIO: Debug data (pin 2)
        - SWCLK: Debug clock (pin 3)
        - GND: Ground (pin 4)
    """

    def __init__(
        self,
        sch: "Schematic",
        x: float,
        y: float,
        ref: str = "J1",
        value: str = "SWD",
        header_symbol: str = "Connector_Generic:Conn_01x04",
    ):
        """
        Create a debug header block.

        Args:
            sch: Schematic to add to
            x: X coordinate of header
            y: Y coordinate of header center
            ref: Header reference designator
            value: Header value label
            header_symbol: KiCad symbol for 4-pin header
        """
        super().__init__()
        self.schematic = sch
        self.x = x
        self.y = y

        # Place header
        self.header = sch.add_symbol(header_symbol, x, y, ref, value)

        self.components = {"HEADER": self.header}

        # Get pin positions (assuming standard 1x4 header)
        # Pins are typically at 2.54mm spacing
        self.ports = {
            "VCC": self.header.pin_position("1"),
            "SWDIO": self.header.pin_position("2"),
            "SWCLK": self.header.pin_position("3"),
            "GND": self.header.pin_position("4"),
        }


# Factory functions for common configurations


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


def create_3v3_ldo(
    sch: "Schematic",
    x: float,
    y: float,
    ref: str = "U1",
    cap_ref_start: int = 1,
) -> LDOBlock:
    """Create a 3.3V LDO block with standard capacitors."""
    return LDOBlock(
        sch,
        x,
        y,
        ref=ref,
        value="XC6206-3.3V",
        ldo_symbol="Regulator_Linear:AP2204K-1.5",
        input_cap="10uF",
        output_caps=["10uF", "100nF"],
        cap_ref_start=cap_ref_start,
    )


def create_mclk_oscillator(
    sch: "Schematic",
    x: float,
    y: float,
    ref: str = "Y1",
    cap_ref: str = "C1",
    frequency: str = "24.576MHz",
) -> OscillatorBlock:
    """Create an audio MCLK oscillator block."""
    return OscillatorBlock(
        sch,
        x,
        y,
        ref=ref,
        value=frequency,
        decoupling_cap="100nF",
        cap_ref=cap_ref,
    )
