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

import contextlib
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
        self.schematic: Schematic = None
        self.x: float = 0
        self.y: float = 0
        self.ports: dict[str, tuple[float, float]] = {}
        self.components: dict[str, SymbolInstance] = {}

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


class CrystalOscillator(CircuitBlock):
    """
    Crystal oscillator with load capacitors.

    Places a passive crystal with two load capacitors for connection to an MCU
    oscillator input. The load capacitors are pre-wired to the crystal and ground.

    Schematic:
             ┌─────┐
      IN ────┤     ├──── OUT
             │ Y1  │
             └──┬──┘
                │
        ┌───────┼───────┐
        │       │       │
       ─┴─     ─┴─     ─┴─
       C1      GND     C2
       ─┬─             ─┬─
        │               │
        └───────┬───────┘
                │
               GND

    Ports:
        - IN: Crystal input (connect to MCU OSC_IN)
        - OUT: Crystal output (connect to MCU OSC_OUT)
        - GND: Ground reference

    Example:
        from kicad_tools.schematic.blocks import CrystalOscillator

        # Create crystal oscillator
        xtal = CrystalOscillator(
            sch,
            x=200, y=80,
            frequency="8MHz",
            load_caps="20pF",
            ref_prefix="Y",
        )

        # Wire to MCU
        sch.add_wire(xtal.port("IN"), mcu.port("OSC_IN"))
        sch.add_wire(xtal.port("OUT"), mcu.port("OSC_OUT"))
    """

    def __init__(
        self,
        sch: "Schematic",
        x: float,
        y: float,
        frequency: str = "8MHz",
        load_caps: str | tuple[str, str] = "20pF",
        ref_prefix: str = "Y",
        cap_ref_start: int = 1,
        crystal_symbol: str = "Device:Crystal",
        cap_symbol: str = "Device:C",
    ):
        """
        Create a crystal oscillator with load capacitors.

        Args:
            sch: Schematic to add to
            x: X coordinate of crystal center
            y: Y coordinate of crystal center
            frequency: Frequency value for crystal label (e.g., "8MHz", "16MHz")
            load_caps: Load capacitor value(s). Either a single string for both
                caps (e.g., "20pF") or a tuple for different values
                (e.g., ("18pF", "22pF"))
            ref_prefix: Reference designator prefix for crystal (e.g., "Y" or "Y1")
            cap_ref_start: Starting reference number for capacitors
            crystal_symbol: KiCad symbol for crystal
            cap_symbol: KiCad symbol for capacitors
        """
        super().__init__()
        self.schematic = sch
        self.x = x
        self.y = y

        # Parse reference prefix
        if ref_prefix[-1].isdigit():
            y_ref = ref_prefix
        else:
            y_ref = f"{ref_prefix}1"

        # Parse load cap values
        if isinstance(load_caps, str):
            cap1_value = load_caps
            cap2_value = load_caps
        else:
            cap1_value, cap2_value = load_caps

        # Component spacing
        cap_y_offset = 15  # mm below crystal
        cap_x_spacing = 15  # mm between caps (crystal is centered)

        # Place crystal
        self.crystal = sch.add_symbol(crystal_symbol, x, y, y_ref, frequency)

        # Place load capacitors below crystal
        c1_x = x - cap_x_spacing / 2
        c2_x = x + cap_x_spacing / 2
        cap_y = y + cap_y_offset

        c1_ref = f"C{cap_ref_start}"
        c2_ref = f"C{cap_ref_start + 1}"

        self.cap1 = sch.add_symbol(cap_symbol, c1_x, cap_y, c1_ref, cap1_value)
        self.cap2 = sch.add_symbol(cap_symbol, c2_x, cap_y, c2_ref, cap2_value)

        self.components = {
            "XTAL": self.crystal,
            "C1": self.cap1,
            "C2": self.cap2,
        }

        # Get crystal pin positions
        # Standard crystal symbols have pins 1 and 2
        xtal_pin1 = self.crystal.pin_position("1")
        xtal_pin2 = self.crystal.pin_position("2")

        # Get capacitor pin positions
        c1_pin1 = self.cap1.pin_position("1")  # Top of cap
        c1_pin2 = self.cap1.pin_position("2")  # Bottom of cap
        c2_pin1 = self.cap2.pin_position("1")  # Top of cap
        c2_pin2 = self.cap2.pin_position("2")  # Bottom of cap

        # Wire crystal pin 1 to C1 top
        sch.add_wire(xtal_pin1, (c1_pin1[0], xtal_pin1[1]))  # Horizontal from xtal
        sch.add_wire((c1_pin1[0], xtal_pin1[1]), c1_pin1)  # Vertical down to cap

        # Wire crystal pin 2 to C2 top
        sch.add_wire(xtal_pin2, (c2_pin1[0], xtal_pin2[1]))  # Horizontal from xtal
        sch.add_wire((c2_pin1[0], xtal_pin2[1]), c2_pin1)  # Vertical down to cap

        # Wire cap bottoms together (ground bus)
        sch.add_wire(c1_pin2, c2_pin2)

        # Add junctions at crystal-to-cap connection points
        sch.add_junction(c1_pin1[0], xtal_pin1[1])
        sch.add_junction(c2_pin1[0], xtal_pin2[1])

        # Define ports
        # IN/OUT are at the crystal pins (before junction points)
        # GND is at the midpoint of the capacitor ground bus
        gnd_x = (c1_pin2[0] + c2_pin2[0]) / 2
        gnd_y = c1_pin2[1]

        self.ports = {
            "IN": xtal_pin1,
            "OUT": xtal_pin2,
            "GND": (gnd_x, gnd_y),
        }

        # Store internal positions for connect_to_rails
        self._c1_gnd = c1_pin2
        self._c2_gnd = c2_pin2

    def connect_to_rails(self, gnd_rail_y: float, add_junction: bool = True):
        """
        Connect the oscillator ground to a ground rail.

        Args:
            gnd_rail_y: Y coordinate of ground rail
            add_junction: Whether to add a junction marker at the rail connection
        """
        sch = self.schematic
        gnd_pos = self.ports["GND"]

        # Connect ground bus to GND rail
        sch.add_wire(gnd_pos, (gnd_pos[0], gnd_rail_y))

        if add_junction:
            sch.add_junction(gnd_pos[0], gnd_rail_y)


class DebugHeader(CircuitBlock):
    """
    Debug header for ARM Cortex-M microcontrollers (SWD, JTAG, Tag-Connect).

    Supports standard debug interfaces with optional series resistors for protection.

    Example:
        # ARM SWD header (standard 10-pin Cortex Debug)
        swd = DebugHeader(
            sch,
            x=250, y=50,
            interface="swd",
            pins=10,
            series_resistors=True,
            ref="J1",
        )

        # Wire to MCU
        sch.add_wire(swd.port("SWDIO"), mcu.port("SWDIO"))
        sch.add_wire(swd.port("SWCLK"), mcu.port("SWCLK"))
        sch.add_wire(swd.port("NRST"), mcu.port("NRST"))

    Interfaces:
        - swd (6-pin): VCC, GND, SWDIO, SWCLK, NRST, SWO (optional)
        - swd (10-pin): ARM Cortex Debug 10-pin (includes key pin)
        - jtag (20-pin): Standard 20-pin ARM JTAG
        - tag-connect (6/10-pin): Tag-Connect pogo-pin interface

    Ports (SWD):
        - VCC: Target VCC sense
        - GND: Ground
        - SWDIO: Debug data (bidirectional)
        - SWCLK: Debug clock
        - NRST: Reset (active low)
        - SWO: Trace output (10-pin only)

    Ports (JTAG):
        - VCC, GND: Power
        - TDI, TDO, TMS, TCK: JTAG signals
        - TRST, NRST: Reset signals
    """

    # Standard pinouts for each interface type
    # Based on ARM Cortex Debug Connector specifications
    SWD_6PIN_PINOUT = {
        "1": "VCC",
        "2": "SWDIO",
        "3": "GND",
        "4": "SWCLK",
        "5": "GND",
        "6": "NRST",
    }

    SWD_10PIN_PINOUT = {
        "1": "VCC",
        "2": "SWDIO",
        "3": "GND",
        "4": "SWCLK",
        "5": "GND",
        "6": "SWO",
        "7": "KEY",  # No connect / key pin
        "8": "NC",
        "9": "GND",
        "10": "NRST",
    }

    JTAG_20PIN_PINOUT = {
        "1": "VCC",
        "2": "VCC",
        "3": "TRST",
        "4": "GND",
        "5": "TDI",
        "6": "GND",
        "7": "TMS",
        "8": "GND",
        "9": "TCK",
        "10": "GND",
        "11": "RTCK",
        "12": "GND",
        "13": "TDO",
        "14": "GND",
        "15": "NRST",
        "16": "GND",
        "17": "NC",
        "18": "GND",
        "19": "NC",
        "20": "GND",
    }

    # Tag-Connect uses same pinout as SWD
    TAG_CONNECT_6PIN_PINOUT = SWD_6PIN_PINOUT
    TAG_CONNECT_10PIN_PINOUT = SWD_10PIN_PINOUT

    def __init__(
        self,
        sch: "Schematic",
        x: float,
        y: float,
        interface: str = "swd",
        pins: int = 10,
        series_resistors: bool = False,
        resistor_value: str = "10R",
        ref: str = "J1",
        resistor_ref_start: int = 1,
        header_symbol: str | None = None,
        resistor_symbol: str = "Device:R",
    ):
        """
        Create a debug header block.

        Args:
            sch: Schematic to add to
            x: X coordinate of header
            y: Y coordinate of header center
            interface: Debug interface type: "swd", "jtag", or "tag-connect"
            pins: Number of pins (6 or 10 for SWD/Tag-Connect, 20 for JTAG)
            series_resistors: If True, add series resistors for protection
            resistor_value: Value for series resistors (default 10R)
            ref: Header reference designator
            resistor_ref_start: Starting reference number for resistors
            header_symbol: KiCad symbol for header (auto-selected if None)
            resistor_symbol: KiCad symbol for resistors
        """
        super().__init__()
        self.schematic = sch
        self.x = x
        self.y = y
        self.interface = interface.lower()
        self.pins = pins
        self.series_resistors = series_resistors

        # Validate interface and pin count
        self._validate_config()

        # Get pinout for this configuration
        self.pinout = self._get_pinout()

        # Determine header symbol if not specified
        if header_symbol is None:
            header_symbol = self._get_default_symbol()

        # Place header
        value = self._get_value_label()
        self.header = sch.add_symbol(header_symbol, x, y, ref, value)
        self.components = {"HEADER": self.header}

        # Get signals that need resistors (data lines, not power/ground)
        protected_signals = self._get_protected_signals()

        # Place series resistors if requested
        self.resistors: dict[str, SymbolInstance] = {}
        if series_resistors:
            resistor_offset = 15  # mm to the left of header
            r_idx = 0

            for pin_num, signal in self.pinout.items():
                if signal in protected_signals:
                    r_ref = f"R{resistor_ref_start + r_idx}"
                    # Calculate resistor position
                    pin_pos = self.header.pin_position(pin_num)
                    r_x = pin_pos[0] - resistor_offset
                    r_y = pin_pos[1]

                    resistor = sch.add_symbol(resistor_symbol, r_x, r_y, r_ref, resistor_value)
                    self.resistors[signal] = resistor
                    self.components[f"R_{signal}"] = resistor
                    r_idx += 1

                    # Wire resistor pin 2 to header pin
                    r_pin2 = resistor.pin_position("2")
                    sch.add_wire(r_pin2, pin_pos)

        # Build ports dictionary
        self.ports = self._build_ports()

    def _validate_config(self) -> None:
        """Validate interface and pin count combination."""
        valid_configs = {
            "swd": [6, 10],
            "jtag": [20],
            "tag-connect": [6, 10],
        }

        if self.interface not in valid_configs:
            raise ValueError(
                f"Invalid interface '{self.interface}'. Valid options: {list(valid_configs.keys())}"
            )

        if self.pins not in valid_configs[self.interface]:
            raise ValueError(
                f"Invalid pin count {self.pins} for interface '{self.interface}'. "
                f"Valid options: {valid_configs[self.interface]}"
            )

    def _get_pinout(self) -> dict[str, str]:
        """Get pinout dictionary for current configuration."""
        if self.interface == "swd":
            return self.SWD_6PIN_PINOUT if self.pins == 6 else self.SWD_10PIN_PINOUT
        elif self.interface == "tag-connect":
            return self.TAG_CONNECT_6PIN_PINOUT if self.pins == 6 else self.TAG_CONNECT_10PIN_PINOUT
        else:  # jtag
            return self.JTAG_20PIN_PINOUT

    def _get_default_symbol(self) -> str:
        """Get default KiCad symbol for current configuration."""
        if self.interface == "tag-connect":
            # Tag-Connect uses specific footprints but generic symbols
            return f"Connector_Generic:Conn_01x{self.pins:02d}"
        elif self.interface == "jtag":
            return "Connector_Generic:Conn_02x10_Odd_Even"
        else:  # swd
            if self.pins == 10:
                return "Connector_Generic:Conn_02x05_Odd_Even"
            else:
                return f"Connector_Generic:Conn_01x{self.pins:02d}"

    def _get_value_label(self) -> str:
        """Get value label for header."""
        if self.interface == "tag-connect":
            return f"Tag-Connect-{self.pins}"
        elif self.interface == "jtag":
            return "JTAG"
        else:
            return f"SWD-{self.pins}"

    def _get_protected_signals(self) -> set[str]:
        """Get set of signals that should have series resistors."""
        # Data lines that benefit from protection
        # Exclude power, ground, and no-connect pins
        swd_signals = {"SWDIO", "SWCLK", "SWO", "NRST"}
        jtag_signals = {"TDI", "TDO", "TMS", "TCK", "TRST", "NRST", "RTCK"}

        if self.interface in ("swd", "tag-connect"):
            return swd_signals
        else:
            return jtag_signals

    def _build_ports(self) -> dict[str, tuple[float, float]]:
        """Build ports dictionary from pinout."""
        ports = {}
        protected_signals = self._get_protected_signals()

        for pin_num, signal in self.pinout.items():
            # Skip NC and KEY pins
            if signal in ("NC", "KEY"):
                continue

            # For GND/VCC, use first occurrence only (avoid duplicates)
            if signal in ("GND", "VCC") and signal in ports:
                continue

            # Get position - either from resistor (if protected) or header
            if self.series_resistors and signal in protected_signals:
                # Port is at resistor pin 1 (external side)
                resistor = self.resistors.get(signal)
                if resistor:
                    ports[signal] = resistor.pin_position("1")
            else:
                # Port is at header pin
                ports[signal] = self.header.pin_position(pin_num)

        return ports

    def connect_to_rails(
        self, vcc_rail_y: float, gnd_rail_y: float, add_junctions: bool = True
    ) -> None:
        """
        Connect VCC and GND to power rails.

        Args:
            vcc_rail_y: Y coordinate of VCC rail
            gnd_rail_y: Y coordinate of GND rail
            add_junctions: Whether to add junction markers
        """
        sch = self.schematic

        # Connect VCC
        if "VCC" in self.ports:
            vcc_pos = self.ports["VCC"]
            sch.add_wire(vcc_pos, (vcc_pos[0], vcc_rail_y))
            if add_junctions:
                sch.add_junction(vcc_pos[0], vcc_rail_y)

        # Connect GND
        if "GND" in self.ports:
            gnd_pos = self.ports["GND"]
            sch.add_wire(gnd_pos, (gnd_pos[0], gnd_rail_y))
            if add_junctions:
                sch.add_junction(gnd_pos[0], gnd_rail_y)


class MCUBlock(CircuitBlock):
    """
    MCU with bypass capacitors on power pins.

    Places an MCU symbol with properly positioned bypass capacitors,
    pre-wired power connections, and exposed GPIO ports.

    Schematic:
        VDD ──┬──[C1]──┬──[C2]──┬──...──┬── MCU ── GPIO pins
              │        │        │       │
        GND ──┴────────┴────────┴───────┴─────────

    Ports:
        - VDD: Power input (after bypass caps)
        - GND: Ground
        - All MCU GPIO/signal pins by name (e.g., PA0, PB1, NRST, etc.)

    Example:
        >>> from kicad_tools.schematic.blocks import MCUBlock
        >>> # Create MCU block with bypass caps
        >>> mcu = MCUBlock(
        ...     sch,
        ...     mcu_symbol="MCU_ST_STM32F1:STM32F103C8Tx",
        ...     x=150, y=100,
        ...     bypass_caps=["100nF", "100nF", "100nF", "4.7uF"],
        ...     ref="U1",
        ... )
        >>> # Access ports
        >>> mcu.port("VDD")      # Power input
        >>> mcu.port("GND")      # Ground
        >>> mcu.port("PA0")      # GPIO port
        >>> mcu.port("NRST")     # Reset pin
        >>> # Wire to other blocks
        >>> sch.add_wire(ldo.port("VOUT"), mcu.port("VDD"))
    """

    # Pin name patterns that indicate VDD (power input)
    VDD_PATTERNS = (
        "VDD",
        "VDDA",
        "VDDIO",
        "VCC",
        "VCCA",
        "AVDD",
        "DVDD",
        "VBAT",
    )

    # Pin name patterns that indicate GND (ground)
    GND_PATTERNS = ("GND", "GNDA", "VSS", "VSSA", "AGND", "DGND", "AVSS", "DVSS")

    def __init__(
        self,
        sch: "Schematic",
        x: float,
        y: float,
        mcu_symbol: str,
        ref: str = "U1",
        value: str = "",
        bypass_caps: list[str] | None = None,
        cap_ref_start: int = 1,
        cap_ref_prefix: str = "C",
        cap_spacing: float = 10,
        cap_offset_x: float = -30,
        cap_offset_y: float = 20,
        cap_symbol: str = "Device:C",
        unit: int = 1,
    ):
        """
        Create an MCU block with bypass capacitors.

        Args:
            sch: Schematic to add to
            x: X coordinate of MCU center
            y: Y coordinate of MCU center
            mcu_symbol: KiCad symbol for MCU (e.g., "MCU_ST_STM32F1:STM32F103C8Tx")
            ref: MCU reference designator
            value: MCU value label (defaults to symbol name if empty)
            bypass_caps: List of bypass capacitor values (e.g., ["100nF", "100nF", "4.7uF"]).
                If None, uses default ["100nF", "100nF", "100nF", "100nF"]
            cap_ref_start: Starting reference number for capacitors
            cap_ref_prefix: Reference designator prefix for capacitors
            cap_spacing: Horizontal spacing between capacitors (mm)
            cap_offset_x: X offset of first capacitor relative to MCU
            cap_offset_y: Y offset of capacitors relative to MCU
            cap_symbol: KiCad symbol for bypass capacitors
            unit: Symbol unit number (for multi-unit symbols)
        """
        super().__init__()
        self.schematic = sch
        self.x = x
        self.y = y

        # Default bypass caps if not specified
        if bypass_caps is None:
            bypass_caps = ["100nF", "100nF", "100nF", "100nF"]

        # Default value to symbol name if not provided
        if not value:
            value = mcu_symbol.split(":")[-1] if ":" in mcu_symbol else mcu_symbol

        # Place MCU
        self.mcu = sch.add_symbol(mcu_symbol, x, y, ref, value, unit=unit)
        self.components = {"MCU": self.mcu}

        # Identify power pins from MCU symbol
        self.vdd_pins: list[str] = []
        self.gnd_pins: list[str] = []
        self._identify_power_pins()

        # Place bypass capacitors
        self.bypass_caps: list = []
        cap_x = x + cap_offset_x
        cap_y = y + cap_offset_y

        for i, cap_value in enumerate(bypass_caps):
            cap_ref = f"{cap_ref_prefix}{cap_ref_start + i}"
            cap = sch.add_symbol(cap_symbol, cap_x + i * cap_spacing, cap_y, cap_ref, cap_value)
            self.bypass_caps.append(cap)
            self.components[f"C{i + 1}"] = cap

        # Wire bypass caps internally (all caps share VDD and GND rails)
        self._wire_bypass_caps()

        # Build ports dict with all MCU pins
        self.ports = {}
        self._build_ports()

    def _identify_power_pins(self):
        """Identify VDD and GND pins from the MCU symbol."""
        # Access pin information from the symbol definition
        if hasattr(self.mcu, "symbol_def") and hasattr(self.mcu.symbol_def, "pins"):
            for pin in self.mcu.symbol_def.pins:
                pin_name_upper = pin.name.upper()

                # Check for VDD patterns
                for pattern in self.VDD_PATTERNS:
                    if pin_name_upper.startswith(pattern) or pin_name_upper == pattern:
                        self.vdd_pins.append(pin.name)
                        break

                # Check for GND patterns
                for pattern in self.GND_PATTERNS:
                    if pin_name_upper.startswith(pattern) or pin_name_upper == pattern:
                        self.gnd_pins.append(pin.name)
                        break

        # Also check by pin type if available
        if hasattr(self.mcu, "symbol_def") and hasattr(self.mcu.symbol_def, "pins"):
            for pin in self.mcu.symbol_def.pins:
                if pin.pin_type == "power_in":
                    pin_name_upper = pin.name.upper()
                    # Additional check by type for pins we might have missed
                    if any(p in pin_name_upper for p in ("VDD", "VCC", "V+")):
                        if pin.name not in self.vdd_pins:
                            self.vdd_pins.append(pin.name)
                    elif any(p in pin_name_upper for p in ("GND", "VSS", "V-")):
                        if pin.name not in self.gnd_pins:
                            self.gnd_pins.append(pin.name)

    def _wire_bypass_caps(self):
        """Wire bypass capacitors to form a decoupling bank."""
        if not self.bypass_caps:
            return

        sch = self.schematic

        # Get first cap's pin positions for reference
        first_cap = self.bypass_caps[0]
        vdd_y = first_cap.pin_position("1")[1]
        gnd_y = first_cap.pin_position("2")[1]

        # Wire each cap to the VDD/GND bus
        for i, cap in enumerate(self.bypass_caps):
            cap_vdd = cap.pin_position("1")
            cap_gnd = cap.pin_position("2")

            # Connect to horizontal bus if not the first cap
            if i > 0:
                prev_cap = self.bypass_caps[i - 1]
                prev_vdd = prev_cap.pin_position("1")
                prev_gnd = prev_cap.pin_position("2")

                # Horizontal wire on VDD bus
                sch.add_wire(prev_vdd, (cap_vdd[0], vdd_y))
                sch.add_wire((cap_vdd[0], vdd_y), cap_vdd)

                # Horizontal wire on GND bus
                sch.add_wire(prev_gnd, (cap_gnd[0], gnd_y))
                sch.add_wire((cap_gnd[0], gnd_y), cap_gnd)

    def _build_ports(self):
        """Build ports dict exposing all MCU pins."""
        # Add VDD port (use first VDD pin position, or first cap's VDD)
        if self.vdd_pins:
            self.ports["VDD"] = self.mcu.pin_position(self.vdd_pins[0])
        elif self.bypass_caps:
            self.ports["VDD"] = self.bypass_caps[0].pin_position("1")

        # Add GND port (use first GND pin position, or first cap's GND)
        if self.gnd_pins:
            self.ports["GND"] = self.mcu.pin_position(self.gnd_pins[0])
        elif self.bypass_caps:
            self.ports["GND"] = self.bypass_caps[0].pin_position("2")

        # Expose all MCU pins as ports
        if hasattr(self.mcu, "symbol_def") and hasattr(self.mcu.symbol_def, "pins"):
            for pin in self.mcu.symbol_def.pins:
                if pin.name and pin.name not in self.ports:
                    with contextlib.suppress(Exception):
                        self.ports[pin.name] = self.mcu.pin_position(pin.name)

    def connect_to_rails(
        self,
        vdd_rail_y: float,
        gnd_rail_y: float,
        wire_all_power_pins: bool = True,
    ):
        """
        Connect MCU and bypass caps to power rails.

        Args:
            vdd_rail_y: Y coordinate of VDD power rail
            gnd_rail_y: Y coordinate of GND power rail
            wire_all_power_pins: If True, wire all VDD/GND pins to rails.
                If False, only wire the first VDD/GND pin.
        """
        sch = self.schematic

        # Wire bypass caps to rails
        for cap in self.bypass_caps:
            sch.wire_decoupling_cap(cap, vdd_rail_y, gnd_rail_y)

        # Wire MCU power pins to rails
        vdd_pins_to_wire = self.vdd_pins if wire_all_power_pins else self.vdd_pins[:1]
        gnd_pins_to_wire = self.gnd_pins if wire_all_power_pins else self.gnd_pins[:1]

        for pin_name in vdd_pins_to_wire:
            try:
                pin_pos = self.mcu.pin_position(pin_name)
                sch.add_wire(pin_pos, (pin_pos[0], vdd_rail_y))
                sch.add_junction(pin_pos[0], vdd_rail_y)
            except Exception:
                pass

        for pin_name in gnd_pins_to_wire:
            try:
                pin_pos = self.mcu.pin_position(pin_name)
                sch.add_wire(pin_pos, (pin_pos[0], gnd_rail_y))
                sch.add_junction(pin_pos[0], gnd_rail_y)
            except Exception:
                pass

    def get_gpio_pins(self) -> list[str]:
        """Get list of GPIO pin names (non-power pins)."""
        gpio_pins = []
        if hasattr(self.mcu, "symbol_def") and hasattr(self.mcu.symbol_def, "pins"):
            for pin in self.mcu.symbol_def.pins:
                if pin.name:
                    pin_upper = pin.name.upper()
                    is_power = any(
                        pin_upper.startswith(p) for p in self.VDD_PATTERNS + self.GND_PATTERNS
                    )
                    if not is_power:
                        gpio_pins.append(pin.name)
        return gpio_pins

    def get_power_pins(self) -> dict[str, list[str]]:
        """Get dict of power pin names grouped by type."""
        return {
            "VDD": self.vdd_pins.copy(),
            "GND": self.gnd_pins.copy(),
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


class BarrelJackInput(CircuitBlock):
    """
    Barrel jack power input with optional reverse polarity protection.

    Schematic (with P-FET protection):
        VIN ──┬── [Q] ──┬── [C_filt] ──┬── VOUT
              │    │    │              │
              └────┴────┼──────────────┘
                       GND

    Schematic (with diode protection):
        VIN ──── [D] ──┬── [C_filt] ──┬── VOUT
                       │              │
                      GND ────────────┘

    Ports:
        - VIN: Raw input from barrel jack
        - VOUT: Protected output
        - GND: Ground
    """

    def __init__(
        self,
        sch: "Schematic",
        x: float,
        y: float,
        voltage: str = "12V",
        protection: str = "pfet",  # "pfet", "diode", or "none"
        filter_cap: str = "100uF",
        ref_prefix: str = "J1",
        jack_symbol: str = "Connector:Barrel_Jack_Switch",
        pfet_symbol: str = "Device:Q_PMOS_GSD",
        diode_symbol: str = "Device:D_Schottky",
        cap_symbol: str = "Device:CP",
    ):
        """
        Create a barrel jack power input block.

        Args:
            sch: Schematic to add to
            x: X coordinate of barrel jack
            y: Y coordinate of barrel jack
            voltage: Input voltage label (e.g., "12V", "9V")
            protection: Protection type - "pfet", "diode", or "none"
            filter_cap: Filter capacitor value
            ref_prefix: Reference designator prefix for jack
            jack_symbol: KiCad symbol for barrel jack
            pfet_symbol: KiCad symbol for P-channel MOSFET
            diode_symbol: KiCad symbol for Schottky diode
            cap_symbol: KiCad symbol for polarized capacitor
        """
        super().__init__()
        self.schematic = sch
        self.x = x
        self.y = y
        self.protection = protection

        # Component spacing
        protection_offset = 20  # Distance to protection device
        cap_offset = 40  # Distance to filter cap

        # Parse reference prefix
        j_ref = ref_prefix if ref_prefix[-1].isdigit() else f"{ref_prefix}1"
        base_num = int(j_ref[-1]) if j_ref[-1].isdigit() else 1

        # Place barrel jack
        self.jack = sch.add_symbol(jack_symbol, x, y, j_ref, voltage)
        self.components = {"JACK": self.jack}

        # Get jack pin positions
        jack_tip = self.jack.pin_position("Tip")  # Positive
        jack_sleeve = self.jack.pin_position("Sleeve")  # Ground

        # Define VIN port at jack tip
        self.ports = {"VIN": jack_tip, "GND": jack_sleeve}

        # Add protection device
        if protection == "pfet":
            q_ref = f"Q{base_num}"
            q_x = x + protection_offset
            self.pfet = sch.add_symbol(pfet_symbol, q_x, y, q_ref, "Si2301")
            self.components["Q"] = self.pfet

            # Wire jack tip to PFET source
            pfet_source = self.pfet.pin_position("S")
            pfet_gate = self.pfet.pin_position("G")
            pfet_drain = self.pfet.pin_position("D")

            sch.add_wire(jack_tip, pfet_source)

            # Gate tied to ground for always-on reverse protection
            sch.add_wire(pfet_gate, (pfet_gate[0], jack_sleeve[1]))

            # Output comes from drain
            output_pos = pfet_drain

        elif protection == "diode":
            d_ref = f"D{base_num}"
            d_x = x + protection_offset
            self.diode = sch.add_symbol(diode_symbol, d_x, y, d_ref, "SS34")
            self.components["D"] = self.diode

            # Wire jack tip to diode anode
            diode_anode = self.diode.pin_position("A")
            diode_cathode = self.diode.pin_position("K")

            sch.add_wire(jack_tip, diode_anode)

            # Output comes from cathode
            output_pos = diode_cathode

        else:  # no protection
            output_pos = jack_tip

        # Place filter capacitor
        c_ref = f"C{base_num}"
        c_x = x + cap_offset
        self.filter_cap = sch.add_symbol(cap_symbol, c_x, y + 10, c_ref, filter_cap)
        self.components["C_FILT"] = self.filter_cap

        # Wire protection output to cap
        cap_pos = self.filter_cap.pin_position("1")
        cap_neg = self.filter_cap.pin_position("2")

        if protection != "none":
            sch.add_wire(output_pos, cap_pos)

        # Define output port at cap positive
        self.ports["VOUT"] = cap_pos

        # Store positions for rail connections
        self._output_y = cap_pos[1]
        self._gnd_y = cap_neg[1]

    def connect_to_rails(
        self,
        gnd_rail_y: float,
        add_junctions: bool = True,
    ):
        """
        Connect filter cap ground to ground rail.

        Args:
            gnd_rail_y: Y coordinate of ground rail
            add_junctions: Whether to add junction markers
        """
        sch = self.schematic

        # Connect cap negative to GND rail
        cap_neg = self.filter_cap.pin_position("2")
        sch.add_wire(cap_neg, (cap_neg[0], gnd_rail_y))

        # Connect jack sleeve to GND rail
        jack_sleeve = self.jack.pin_position("Sleeve")
        sch.add_wire(jack_sleeve, (jack_sleeve[0], gnd_rail_y))

        if add_junctions:
            sch.add_junction(cap_neg[0], gnd_rail_y)
            sch.add_junction(jack_sleeve[0], gnd_rail_y)


class USBPowerInput(CircuitBlock):
    """
    USB power input with optional fuse protection.

    Schematic (with fuse):
        VBUS_IN ──── [F] ──┬── [C_filt] ──┬── V5
                           │              │
                          GND ────────────┘

    Ports:
        - VBUS_IN: Raw VBUS from USB connector
        - V5: Protected 5V output
        - GND: Ground
    """

    def __init__(
        self,
        sch: "Schematic",
        x: float,
        y: float,
        protection: str = "fuse",  # "fuse", "polyfuse", or "none"
        filter_cap: str = "10uF",
        fuse_rating: str = "500mA",
        ref_prefix: str = "J1",
        fuse_symbol: str = "Device:Polyfuse",
        cap_symbol: str = "Device:C",
    ):
        """
        Create a USB power input block.

        Args:
            sch: Schematic to add to
            x: X coordinate (where VBUS enters)
            y: Y coordinate
            protection: Protection type - "fuse", "polyfuse", or "none"
            filter_cap: Filter capacitor value
            fuse_rating: Fuse/polyfuse current rating
            ref_prefix: Reference designator prefix
            fuse_symbol: KiCad symbol for fuse/polyfuse
            cap_symbol: KiCad symbol for capacitor
        """
        super().__init__()
        self.schematic = sch
        self.x = x
        self.y = y
        self.protection = protection

        # Component spacing
        fuse_offset = 15  # Distance to fuse
        cap_offset = 35  # Distance to filter cap

        # Parse reference prefix for numbering
        base_num = 1
        if ref_prefix[-1].isdigit():
            base_num = int(ref_prefix[-1])

        self.components = {}

        # Input position (representing VBUS from USB connector)
        input_pos = (x, y)
        self.ports = {"VBUS_IN": input_pos}

        # Add fuse protection
        if protection in ("fuse", "polyfuse"):
            f_ref = f"F{base_num}"
            f_x = x + fuse_offset
            self.fuse = sch.add_symbol(fuse_symbol, f_x, y, f_ref, fuse_rating)
            self.components["F"] = self.fuse

            # Wire input to fuse
            fuse_in = self.fuse.pin_position("1")
            fuse_out = self.fuse.pin_position("2")
            sch.add_wire(input_pos, fuse_in)

            # Output comes from fuse
            output_pos = fuse_out
        else:
            output_pos = input_pos

        # Place filter capacitor
        c_ref = f"C{base_num}"
        c_x = x + cap_offset
        self.filter_cap = sch.add_symbol(cap_symbol, c_x, y + 10, c_ref, filter_cap)
        self.components["C_FILT"] = self.filter_cap

        # Wire fuse output to cap
        cap_pos = self.filter_cap.pin_position("1")
        cap_neg = self.filter_cap.pin_position("2")

        sch.add_wire(output_pos, cap_pos)

        # Define ports
        self.ports["V5"] = cap_pos
        self.ports["GND"] = cap_neg

    def connect_to_rails(
        self,
        gnd_rail_y: float,
        add_junctions: bool = True,
    ):
        """
        Connect filter cap ground to ground rail.

        Args:
            gnd_rail_y: Y coordinate of ground rail
            add_junctions: Whether to add junction markers
        """
        sch = self.schematic

        # Connect cap negative to GND rail
        cap_neg = self.filter_cap.pin_position("2")
        sch.add_wire(cap_neg, (cap_neg[0], gnd_rail_y))

        if add_junctions:
            sch.add_junction(cap_neg[0], gnd_rail_y)


class BatteryInput(CircuitBlock):
    """
    Battery input with optional reverse polarity protection.

    Schematic (with P-FET protection):
        VBAT_IN ──┬── [Q] ──┬── [C_filt] ──┬── VBAT
                  │    │    │              │
                  └────┴────┼──────────────┘
                           GND

    Ports:
        - VBAT_IN: Raw battery input
        - VBAT: Protected battery output
        - GND: Ground
    """

    def __init__(
        self,
        sch: "Schematic",
        x: float,
        y: float,
        voltage: str = "3.7V",
        connector: str = "JST-PH",
        protection: str = "pfet",  # "pfet", "diode", or "none"
        filter_cap: str = "10uF",
        ref_prefix: str = "J1",
        connector_symbol: str = "Connector_Generic:Conn_01x02",
        pfet_symbol: str = "Device:Q_PMOS_GSD",
        diode_symbol: str = "Device:D_Schottky",
        cap_symbol: str = "Device:C",
    ):
        """
        Create a battery input block.

        Args:
            sch: Schematic to add to
            x: X coordinate of connector
            y: Y coordinate of connector
            voltage: Battery voltage label (e.g., "3.7V", "7.4V")
            connector: Connector type label (e.g., "JST-PH", "JST-XH")
            protection: Protection type - "pfet", "diode", or "none"
            filter_cap: Filter capacitor value
            ref_prefix: Reference designator prefix
            connector_symbol: KiCad symbol for battery connector
            pfet_symbol: KiCad symbol for P-channel MOSFET
            diode_symbol: KiCad symbol for Schottky diode
            cap_symbol: KiCad symbol for capacitor
        """
        super().__init__()
        self.schematic = sch
        self.x = x
        self.y = y
        self.protection = protection
        self.voltage = voltage
        self.connector_type = connector

        # Component spacing
        protection_offset = 20
        cap_offset = 40

        # Parse reference prefix
        j_ref = ref_prefix if ref_prefix[-1].isdigit() else f"{ref_prefix}1"
        base_num = int(j_ref[-1]) if j_ref[-1].isdigit() else 1

        # Place battery connector
        value = f"{connector} {voltage}"
        self.connector = sch.add_symbol(connector_symbol, x, y, j_ref, value)
        self.components = {"CONN": self.connector}

        # Get connector pin positions
        conn_pos = self.connector.pin_position("1")  # Positive
        conn_neg = self.connector.pin_position("2")  # Ground

        # Define input port
        self.ports = {"VBAT_IN": conn_pos, "GND": conn_neg}

        # Add protection device
        if protection == "pfet":
            q_ref = f"Q{base_num}"
            q_x = x + protection_offset
            self.pfet = sch.add_symbol(pfet_symbol, q_x, y, q_ref, "Si2301")
            self.components["Q"] = self.pfet

            # Wire connector positive to PFET source
            pfet_source = self.pfet.pin_position("S")
            pfet_gate = self.pfet.pin_position("G")
            pfet_drain = self.pfet.pin_position("D")

            sch.add_wire(conn_pos, pfet_source)

            # Gate tied to ground for always-on reverse protection
            sch.add_wire(pfet_gate, (pfet_gate[0], conn_neg[1]))

            # Output from drain
            output_pos = pfet_drain

        elif protection == "diode":
            d_ref = f"D{base_num}"
            d_x = x + protection_offset
            self.diode = sch.add_symbol(diode_symbol, d_x, y, d_ref, "SS34")
            self.components["D"] = self.diode

            # Wire connector positive to diode anode
            diode_anode = self.diode.pin_position("A")
            diode_cathode = self.diode.pin_position("K")

            sch.add_wire(conn_pos, diode_anode)

            # Output from cathode
            output_pos = diode_cathode

        else:  # no protection
            output_pos = conn_pos

        # Place filter capacitor
        c_ref = f"C{base_num}"
        c_x = x + cap_offset
        self.filter_cap = sch.add_symbol(cap_symbol, c_x, y + 10, c_ref, filter_cap)
        self.components["C_FILT"] = self.filter_cap

        # Wire protection output to cap
        cap_pos = self.filter_cap.pin_position("1")

        if protection != "none":
            sch.add_wire(output_pos, cap_pos)

        # Define output port
        self.ports["VBAT"] = cap_pos

    def connect_to_rails(
        self,
        gnd_rail_y: float,
        add_junctions: bool = True,
    ):
        """
        Connect filter cap and connector ground to ground rail.

        Args:
            gnd_rail_y: Y coordinate of ground rail
            add_junctions: Whether to add junction markers
        """
        sch = self.schematic

        # Connect cap negative to GND rail
        cap_neg = self.filter_cap.pin_position("2")
        sch.add_wire(cap_neg, (cap_neg[0], gnd_rail_y))

        # Connect connector ground to GND rail
        conn_neg = self.connector.pin_position("2")
        sch.add_wire(conn_neg, (conn_neg[0], gnd_rail_y))

        if add_junctions:
            sch.add_junction(cap_neg[0], gnd_rail_y)
            sch.add_junction(conn_neg[0], gnd_rail_y)


# Factory functions for power inputs


def create_12v_barrel_jack(
    sch: "Schematic",
    x: float,
    y: float,
    ref: str = "J1",
    protection: str = "pfet",
) -> BarrelJackInput:
    """Create a 12V barrel jack input with reverse polarity protection."""
    return BarrelJackInput(
        sch,
        x,
        y,
        voltage="12V",
        protection=protection,
        filter_cap="100uF",
        ref_prefix=ref,
    )


def create_usb_power(
    sch: "Schematic",
    x: float,
    y: float,
    ref: str = "J1",
) -> USBPowerInput:
    """Create a USB power input with polyfuse protection."""
    return USBPowerInput(
        sch,
        x,
        y,
        protection="polyfuse",
        filter_cap="10uF",
        fuse_rating="500mA",
        ref_prefix=ref,
    )


def create_lipo_battery(
    sch: "Schematic",
    x: float,
    y: float,
    ref: str = "J1",
) -> BatteryInput:
    """Create a 3.7V LiPo battery input with JST-PH connector."""
    return BatteryInput(
        sch,
        x,
        y,
        voltage="3.7V",
        connector="JST-PH",
        protection="pfet",
        filter_cap="10uF",
        ref_prefix=ref,
    )


class USBConnector(CircuitBlock):
    """
    USB connector with ESD protection TVS diodes.

    Places a USB connector (Type-C, Micro-B, Mini-B, or Type-A) with optional
    ESD protection on data lines and VBUS protection.

    Schematic (Type-C with ESD):
        VBUS ──┬── [TVS_VBUS] ──┬── VBUS_OUT
               │                │
               └────────────────┴───── GND
        D+ ──── [TVS_DATA] ──── D+_OUT
        D- ──── [TVS_DATA] ──── D-_OUT
        CC1 ─────────────────── CC1_OUT
        CC2 ─────────────────── CC2_OUT
        GND ─────────────────── GND

    Ports:
        - VBUS: 5V from USB (after protection if enabled)
        - D+: Data+ (after ESD protection if enabled)
        - D-: Data- (after ESD protection if enabled)
        - GND: Ground
        - CC1: Type-C CC1 (Type-C only)
        - CC2: Type-C CC2 (Type-C only)
        - ID: OTG ID pin (Micro-B/Mini-B only)
        - SHIELD: Connector shield (when available)

    Example:
        from kicad_tools.schematic.blocks import USBConnector

        # USB Type-C with ESD protection
        usb = USBConnector(
            sch,
            x=50, y=100,
            connector_type="type-c",
            esd_protection=True,
            vbus_protection=True,
            ref_prefix="J",
        )

        # Access ports
        usb.port("VBUS")   # 5V from USB
        usb.port("D+")     # Data+ (after ESD)
        usb.port("D-")     # Data- (after ESD)
        usb.port("GND")    # Ground
        usb.port("CC1")    # Type-C CC1
        usb.port("CC2")    # Type-C CC2

        # Wire to MCU
        sch.add_wire(usb.port("D+"), mcu.port("USB_DP"))
        sch.add_wire(usb.port("D-"), mcu.port("USB_DM"))
    """

    # Connector type configurations
    CONNECTOR_CONFIGS = {
        "type-c": {
            "symbol": "Connector:USB_C_Receptacle_USB2.0",
            "pins": ["VBUS", "GND", "D+", "D-", "CC1", "CC2", "SHIELD"],
            "has_cc": True,
            "has_id": False,
        },
        "micro-b": {
            "symbol": "Connector:USB_Micro-B",
            "pins": ["VBUS", "GND", "D+", "D-", "ID", "SHIELD"],
            "has_cc": False,
            "has_id": True,
        },
        "mini-b": {
            "symbol": "Connector:USB_Mini-B",
            "pins": ["VBUS", "GND", "D+", "D-", "ID"],
            "has_cc": False,
            "has_id": True,
        },
        "type-a": {
            "symbol": "Connector:USB_A",
            "pins": ["VBUS", "GND", "D+", "D-", "SHIELD"],
            "has_cc": False,
            "has_id": False,
        },
    }

    def __init__(
        self,
        sch: "Schematic",
        x: float,
        y: float,
        connector_type: str = "type-c",
        esd_protection: bool = True,
        vbus_protection: bool = False,
        ref_prefix: str = "J",
        tvs_ref_start: int = 1,
        connector_symbol: str | None = None,
        esd_tvs_symbol: str = "Device:D_TVS",
        vbus_tvs_symbol: str = "Device:D_TVS",
        esd_tvs_value: str = "USBLC6-2SC6",
        vbus_tvs_value: str = "SMBJ5.0A",
    ):
        """
        Create a USB connector with ESD protection.

        Args:
            sch: Schematic to add to
            x: X coordinate of connector
            y: Y coordinate of connector
            connector_type: USB connector type - "type-c", "micro-b", "mini-b", or "type-a"
            esd_protection: If True, add TVS diodes on D+/D- lines
            vbus_protection: If True, add TVS diode on VBUS
            ref_prefix: Reference designator prefix (e.g., "J" or "J1")
            tvs_ref_start: Starting reference number for TVS diodes
            connector_symbol: KiCad symbol for connector (auto-selected if None)
            esd_tvs_symbol: KiCad symbol for ESD TVS diode
            vbus_tvs_symbol: KiCad symbol for VBUS TVS diode
            esd_tvs_value: Part value for ESD TVS (e.g., "USBLC6-2SC6")
            vbus_tvs_value: Part value for VBUS TVS (e.g., "SMBJ5.0A")
        """
        super().__init__()
        self.schematic = sch
        self.x = x
        self.y = y
        self.connector_type = connector_type.lower()
        self.esd_protection = esd_protection
        self.vbus_protection = vbus_protection

        # Validate connector type
        if self.connector_type not in self.CONNECTOR_CONFIGS:
            raise ValueError(
                f"Invalid connector type '{connector_type}'. "
                f"Valid options: {list(self.CONNECTOR_CONFIGS.keys())}"
            )

        config = self.CONNECTOR_CONFIGS[self.connector_type]

        # Parse reference prefix
        if ref_prefix[-1].isdigit():
            j_ref = ref_prefix
        else:
            j_ref = f"{ref_prefix}1"

        # Determine connector symbol
        if connector_symbol is None:
            connector_symbol = config["symbol"]

        # Component spacing
        tvs_offset_x = 20  # Distance to TVS diodes from connector

        # Place connector
        self.connector = sch.add_symbol(connector_symbol, x, y, j_ref, self.connector_type.upper())
        self.components = {"CONN": self.connector}

        # Get connector pin positions
        conn_pins = {}
        for pin_name in config["pins"]:
            with contextlib.suppress(Exception):
                conn_pins[pin_name] = self.connector.pin_position(pin_name)

        # Initialize TVS components
        self.tvs_diodes: dict[str, SymbolInstance] = {}
        tvs_idx = 0

        # Track output positions (after protection)
        output_positions = {}

        # Add ESD protection on D+/D- if requested
        if esd_protection and "D+" in conn_pins and "D-" in conn_pins:
            tvs_ref = f"D{tvs_ref_start + tvs_idx}"
            # Place ESD TVS diode (dual-line ESD like USBLC6-2SC6)
            tvs_x = x + tvs_offset_x
            tvs_y = (conn_pins["D+"][1] + conn_pins["D-"][1]) / 2

            self.esd_tvs = sch.add_symbol(esd_tvs_symbol, tvs_x, tvs_y, tvs_ref, esd_tvs_value)
            self.tvs_diodes["ESD"] = self.esd_tvs
            self.components["TVS_ESD"] = self.esd_tvs
            tvs_idx += 1

            # Wire D+ through TVS
            dp_conn = conn_pins["D+"]
            tvs_pin1 = self.esd_tvs.pin_position("A")
            tvs_pin2 = self.esd_tvs.pin_position("K")

            # Connect D+ from connector to TVS input
            sch.add_wire(dp_conn, (tvs_pin1[0], dp_conn[1]))

            # D+ output is after TVS (use same x as TVS output)
            output_positions["D+"] = (tvs_pin2[0], dp_conn[1])

            # Wire D- through TVS (assuming dual-channel TVS)
            dm_conn = conn_pins["D-"]
            sch.add_wire(dm_conn, (tvs_pin1[0], dm_conn[1]))

            # D- output is after TVS
            output_positions["D-"] = (tvs_pin2[0], dm_conn[1])

        else:
            # No ESD protection, output is same as connector pin
            if "D+" in conn_pins:
                output_positions["D+"] = conn_pins["D+"]
            if "D-" in conn_pins:
                output_positions["D-"] = conn_pins["D-"]

        # Add VBUS protection if requested
        if vbus_protection and "VBUS" in conn_pins:
            vbus_tvs_ref = f"D{tvs_ref_start + tvs_idx}"
            vbus_conn = conn_pins["VBUS"]
            vbus_tvs_x = x + tvs_offset_x
            vbus_tvs_y = vbus_conn[1]

            self.vbus_tvs = sch.add_symbol(
                vbus_tvs_symbol, vbus_tvs_x, vbus_tvs_y, vbus_tvs_ref, vbus_tvs_value
            )
            self.tvs_diodes["VBUS"] = self.vbus_tvs
            self.components["TVS_VBUS"] = self.vbus_tvs
            tvs_idx += 1

            # Wire VBUS through TVS
            vbus_anode = self.vbus_tvs.pin_position("A")
            vbus_cathode = self.vbus_tvs.pin_position("K")

            # Connect VBUS from connector to TVS
            sch.add_wire(vbus_conn, vbus_anode)

            # VBUS output is after TVS
            output_positions["VBUS"] = vbus_cathode

            # Connect TVS cathode to GND (TVS clamps to ground)
            if "GND" in conn_pins:
                gnd_pos = conn_pins["GND"]
                sch.add_wire(vbus_cathode, (vbus_cathode[0], gnd_pos[1]))

        else:
            # No VBUS protection, output is same as connector pin
            if "VBUS" in conn_pins:
                output_positions["VBUS"] = conn_pins["VBUS"]

        # Build ports dictionary
        self.ports = {}

        # Add protected/unprotected data lines
        if "D+" in output_positions:
            self.ports["D+"] = output_positions["D+"]
        if "D-" in output_positions:
            self.ports["D-"] = output_positions["D-"]

        # Add VBUS (protected or direct)
        if "VBUS" in output_positions:
            self.ports["VBUS"] = output_positions["VBUS"]

        # Add GND
        if "GND" in conn_pins:
            self.ports["GND"] = conn_pins["GND"]

        # Add CC pins for Type-C
        if config["has_cc"]:
            if "CC1" in conn_pins:
                self.ports["CC1"] = conn_pins["CC1"]
            if "CC2" in conn_pins:
                self.ports["CC2"] = conn_pins["CC2"]

        # Add ID pin for Micro-B/Mini-B
        if config["has_id"] and "ID" in conn_pins:
            self.ports["ID"] = conn_pins["ID"]

        # Add SHIELD if available
        if "SHIELD" in conn_pins:
            self.ports["SHIELD"] = conn_pins["SHIELD"]

    def connect_to_rails(
        self,
        vbus_rail_y: float | None = None,
        gnd_rail_y: float | None = None,
        add_junctions: bool = True,
    ) -> None:
        """
        Connect USB power to rails.

        Args:
            vbus_rail_y: Y coordinate of VBUS rail (optional)
            gnd_rail_y: Y coordinate of GND rail (optional)
            add_junctions: Whether to add junction markers
        """
        sch = self.schematic

        # Connect VBUS to rail
        if vbus_rail_y is not None and "VBUS" in self.ports:
            vbus_pos = self.ports["VBUS"]
            sch.add_wire(vbus_pos, (vbus_pos[0], vbus_rail_y))
            if add_junctions:
                sch.add_junction(vbus_pos[0], vbus_rail_y)

        # Connect GND to rail
        if gnd_rail_y is not None and "GND" in self.ports:
            gnd_pos = self.ports["GND"]
            sch.add_wire(gnd_pos, (gnd_pos[0], gnd_rail_y))
            if add_junctions:
                sch.add_junction(gnd_pos[0], gnd_rail_y)

    def has_cc_pins(self) -> bool:
        """Check if this connector type has CC pins."""
        return self.CONNECTOR_CONFIGS[self.connector_type]["has_cc"]

    def has_id_pin(self) -> bool:
        """Check if this connector type has an ID pin."""
        return self.CONNECTOR_CONFIGS[self.connector_type]["has_id"]


def create_usb_type_c(
    sch: "Schematic",
    x: float,
    y: float,
    ref: str = "J1",
    with_esd: bool = True,
    with_vbus_protection: bool = False,
) -> USBConnector:
    """
    Create a USB Type-C connector with optional protection.

    Args:
        sch: Schematic to add to
        x: X coordinate
        y: Y coordinate
        ref: Connector reference designator
        with_esd: Add ESD protection on D+/D-
        with_vbus_protection: Add VBUS TVS protection
    """
    return USBConnector(
        sch,
        x,
        y,
        connector_type="type-c",
        esd_protection=with_esd,
        vbus_protection=with_vbus_protection,
        ref_prefix=ref,
    )


def create_usb_micro_b(
    sch: "Schematic",
    x: float,
    y: float,
    ref: str = "J1",
    with_esd: bool = True,
    with_vbus_protection: bool = False,
) -> USBConnector:
    """
    Create a USB Micro-B connector with optional protection.

    Args:
        sch: Schematic to add to
        x: X coordinate
        y: Y coordinate
        ref: Connector reference designator
        with_esd: Add ESD protection on D+/D-
        with_vbus_protection: Add VBUS TVS protection
    """
    return USBConnector(
        sch,
        x,
        y,
        connector_type="micro-b",
        esd_protection=with_esd,
        vbus_protection=with_vbus_protection,
        ref_prefix=ref,
    )


def create_swd_header(
    sch: "Schematic",
    x: float,
    y: float,
    ref: str = "J1",
    pins: int = 10,
    with_protection: bool = False,
) -> DebugHeader:
    """
    Create an ARM SWD debug header.

    Args:
        sch: Schematic to add to
        x: X coordinate
        y: Y coordinate
        ref: Header reference designator
        pins: 6 for minimal SWD, 10 for ARM Cortex Debug
        with_protection: Add 10R series resistors
    """
    return DebugHeader(
        sch,
        x,
        y,
        interface="swd",
        pins=pins,
        series_resistors=with_protection,
        ref=ref,
    )


def create_jtag_header(
    sch: "Schematic",
    x: float,
    y: float,
    ref: str = "J1",
    with_protection: bool = False,
) -> DebugHeader:
    """
    Create a standard 20-pin ARM JTAG debug header.

    Args:
        sch: Schematic to add to
        x: X coordinate
        y: Y coordinate
        ref: Header reference designator
        with_protection: Add 10R series resistors
    """
    return DebugHeader(
        sch,
        x,
        y,
        interface="jtag",
        pins=20,
        series_resistors=with_protection,
        ref=ref,
    )


def create_tag_connect_header(
    sch: "Schematic",
    x: float,
    y: float,
    ref: str = "J1",
    pins: int = 10,
    with_protection: bool = False,
) -> DebugHeader:
    """
    Create a Tag-Connect debug header (pogo-pin interface).

    Args:
        sch: Schematic to add to
        x: X coordinate
        y: Y coordinate
        ref: Header reference designator
        pins: 6 or 10 pins
        with_protection: Add 10R series resistors
    """
    return DebugHeader(
        sch,
        x,
        y,
        interface="tag-connect",
        pins=pins,
        series_resistors=with_protection,
        ref=ref,
    )
