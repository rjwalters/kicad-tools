"""Timing-related circuit blocks: oscillators, crystals."""

from typing import TYPE_CHECKING

from .base import CircuitBlock

if TYPE_CHECKING:
    from kicad_sch_helper import Schematic


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
        super().__init__(sch, x, y)

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
        super().__init__(sch, x, y)

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


# Factory functions


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
