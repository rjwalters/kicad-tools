"""I2C bus interface blocks: pull-ups with optional filtering."""

from typing import TYPE_CHECKING

from ..base import CircuitBlock

if TYPE_CHECKING:
    from kicad_sch_helper import Schematic


class I2CPullups(CircuitBlock):
    """
    I2C bus pull-up resistors with optional line filtering capacitors.

    Places properly sized pull-up resistors on I2C SDA and SCL lines. Optionally
    adds filtering capacitors for noisy environments.

    Schematic (without filter caps):
        VCC ──┬─────────┬─────────
              │         │
             [R1]      [R2]
              │         │
        SDA ──┴─────────┤
                        │
        SCL ────────────┴─────────

    Schematic (with filter caps):
        VCC ──┬─────────┬─────────
              │         │
             [R1]      [R2]
              │         │
        SDA ──┼────┬────┤
              │   [C1]  │
              │    │    │
        SCL ──┼────┼────┼────┬────
              │    │    │   [C2]
              │    │    │    │
        GND ──┴────┴────┴────┴────

    Ports:
        - VCC: Power for pull-ups
        - SDA: SDA line (after pull-up)
        - SCL: SCL line (after pull-up)
        - GND: Ground (for filter caps, always provided)

    Value Guidelines:
        | Bus Speed        | Typical Resistor | Notes           |
        |------------------|------------------|-----------------|
        | 100 kHz Standard | 4.7kΩ            | Most common     |
        | 400 kHz Fast     | 2.2kΩ - 3.3kΩ    | Lower for speed |
        | 1 MHz Fast+      | 1kΩ - 2.2kΩ      | Check drive     |

    Example:
        from kicad_tools.schematic.blocks import I2CPullups

        # Basic I2C pull-ups for 100kHz
        i2c = I2CPullups(
            sch,
            x=100, y=50,
            resistor_value="4.7k",
            ref_start=1,
        )

        # With filtering capacitors for 400kHz in noisy environment
        i2c = I2CPullups(
            sch,
            x=100, y=50,
            resistor_value="2.2k",
            filter_caps="100pF",
            ref_start=1,
        )

        # Wire to MCU
        sch.add_wire(i2c.port("SDA"), mcu.port("I2C_SDA"))
        sch.add_wire(i2c.port("SCL"), mcu.port("I2C_SCL"))
    """

    def __init__(
        self,
        sch: "Schematic",
        x: float,
        y: float,
        resistor_value: str = "4.7k",
        filter_caps: str | None = None,
        ref_start: int = 1,
        ref_prefix_r: str = "R",
        ref_prefix_c: str = "C",
        spacing: float = 15,
        resistor_symbol: str = "Device:R",
        cap_symbol: str = "Device:C",
    ):
        """
        Create I2C pull-up resistors with optional filtering.

        Args:
            sch: Schematic to add to
            x: X coordinate of block (SDA resistor position)
            y: Y coordinate of block (vertical center)
            resistor_value: Pull-up resistor value (e.g., "4.7k", "2.2k")
            filter_caps: Optional filter capacitor value (e.g., "100pF").
                If None, no capacitors are placed.
            ref_start: Starting reference number for components
            ref_prefix_r: Reference designator prefix for resistors
            ref_prefix_c: Reference designator prefix for capacitors
            spacing: Horizontal spacing between SDA and SCL components
            resistor_symbol: KiCad symbol for resistors
            cap_symbol: KiCad symbol for capacitors
        """
        super().__init__(sch, x, y)
        self.resistor_value = resistor_value
        self.filter_caps_value = filter_caps

        # Place SDA pull-up resistor (R1)
        r1_ref = f"{ref_prefix_r}{ref_start}"
        self.r_sda = sch.add_symbol(resistor_symbol, x, y, r1_ref, resistor_value, rotation=0)

        # Place SCL pull-up resistor (R2)
        r2_ref = f"{ref_prefix_r}{ref_start + 1}"
        r2_x = x + spacing
        self.r_scl = sch.add_symbol(resistor_symbol, r2_x, y, r2_ref, resistor_value, rotation=0)

        self.components = {
            "R_SDA": self.r_sda,
            "R_SCL": self.r_scl,
        }

        # Get resistor pin positions
        r1_pin1 = self.r_sda.pin_position("1")  # Top (VCC side)
        r1_pin2 = self.r_sda.pin_position("2")  # Bottom (SDA side)
        r2_pin1 = self.r_scl.pin_position("1")  # Top (VCC side)
        r2_pin2 = self.r_scl.pin_position("2")  # Bottom (SCL side)

        # Wire VCC bus between resistor tops
        sch.add_wire(r1_pin1, r2_pin1)

        # Track capacitor ground positions for GND bus
        cap_gnd_positions = []

        # Place optional filter capacitors
        if filter_caps:
            cap_y_offset = 10  # Below the resistor-to-line junction

            # SDA filter capacitor (C1)
            c1_ref = f"{ref_prefix_c}{ref_start}"
            c1_x = x
            c1_y = r1_pin2[1] + cap_y_offset
            self.c_sda = sch.add_symbol(cap_symbol, c1_x, c1_y, c1_ref, filter_caps)
            self.components["C_SDA"] = self.c_sda

            # SCL filter capacitor (C2)
            c2_ref = f"{ref_prefix_c}{ref_start + 1}"
            c2_x = r2_x
            c2_y = r2_pin2[1] + cap_y_offset
            self.c_scl = sch.add_symbol(cap_symbol, c2_x, c2_y, c2_ref, filter_caps)
            self.components["C_SCL"] = self.c_scl

            # Get capacitor pin positions
            c1_pin1 = self.c_sda.pin_position("1")  # Top (signal side)
            c1_pin2 = self.c_sda.pin_position("2")  # Bottom (GND side)
            c2_pin1 = self.c_scl.pin_position("1")  # Top (signal side)
            c2_pin2 = self.c_scl.pin_position("2")  # Bottom (GND side)

            # Wire SDA resistor to SDA cap
            sch.add_wire(r1_pin2, c1_pin1)

            # Wire SCL resistor to SCL cap
            sch.add_wire(r2_pin2, c2_pin1)

            # Wire capacitor grounds together (GND bus)
            sch.add_wire(c1_pin2, c2_pin2)

            # Add junctions at resistor-to-cap connections (signal tap points)
            sch.add_junction(r1_pin2[0], r1_pin2[1])
            sch.add_junction(r2_pin2[0], r2_pin2[1])

            cap_gnd_positions = [c1_pin2, c2_pin2]

            # Ports with capacitors
            # GND is at midpoint of capacitor ground bus
            gnd_x = (c1_pin2[0] + c2_pin2[0]) / 2
            gnd_y = c1_pin2[1]

            self.ports = {
                "VCC": r1_pin1,
                "SDA": r1_pin2,  # Junction point after resistor
                "SCL": r2_pin2,  # Junction point after resistor
                "GND": (gnd_x, gnd_y),
            }

            # Store for connect_to_rails
            self._cap_gnd_positions = cap_gnd_positions

        else:
            # No capacitors - simpler layout
            self.ports = {
                "VCC": r1_pin1,
                "SDA": r1_pin2,
                "SCL": r2_pin2,
                "GND": (r1_pin2[0], r1_pin2[1] + 15),  # Virtual GND point below
            }
            self._cap_gnd_positions = []

        # Store VCC bus Y position for rail connection
        self._vcc_y = r1_pin1[1]

    def connect_to_rails(
        self,
        vcc_rail_y: float,
        gnd_rail_y: float | None = None,
        add_junctions: bool = True,
    ) -> None:
        """
        Connect I2C pull-ups to power rails.

        Args:
            vcc_rail_y: Y coordinate of VCC power rail
            gnd_rail_y: Y coordinate of GND power rail (required if filter caps used)
            add_junctions: Whether to add junction markers at rail connections
        """
        sch = self.schematic

        # Connect VCC to rail
        vcc_pos = self.ports["VCC"]
        sch.add_wire(vcc_pos, (vcc_pos[0], vcc_rail_y))

        if add_junctions:
            sch.add_junction(vcc_pos[0], vcc_rail_y)

        # Connect GND to rail if we have filter caps
        if self._cap_gnd_positions and gnd_rail_y is not None:
            gnd_pos = self.ports["GND"]
            sch.add_wire(gnd_pos, (gnd_pos[0], gnd_rail_y))

            if add_junctions:
                sch.add_junction(gnd_pos[0], gnd_rail_y)


# Factory functions


def create_i2c_pullups(
    sch: "Schematic",
    x: float,
    y: float,
    speed: str = "standard",
    with_filter: bool = False,
    ref_start: int = 1,
) -> I2CPullups:
    """
    Create I2C pull-up resistors with preset values for common bus speeds.

    Args:
        sch: Schematic to add to
        x: X coordinate
        y: Y coordinate
        speed: I2C bus speed - "standard" (100kHz), "fast" (400kHz),
            or "fast_plus" (1MHz)
        with_filter: If True, add 100pF filter capacitors
        ref_start: Starting reference number for components

    Returns:
        Configured I2CPullups block

    Example:
        # Standard mode (100kHz) without filtering
        i2c = create_i2c_pullups(sch, 100, 50)

        # Fast mode (400kHz) with filtering for noisy environment
        i2c = create_i2c_pullups(sch, 100, 50, speed="fast", with_filter=True)
    """
    # Resistor values for different speeds
    speed_resistors = {
        "standard": "4.7k",  # 100 kHz
        "fast": "2.2k",  # 400 kHz
        "fast_plus": "1k",  # 1 MHz
    }

    speed_lower = speed.lower().replace("-", "_").replace(" ", "_")
    if speed_lower not in speed_resistors:
        raise ValueError(f"Invalid speed '{speed}'. Valid options: {list(speed_resistors.keys())}")

    resistor_value = speed_resistors[speed_lower]
    filter_caps = "100pF" if with_filter else None

    return I2CPullups(
        sch,
        x,
        y,
        resistor_value=resistor_value,
        filter_caps=filter_caps,
        ref_start=ref_start,
    )
