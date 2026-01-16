"""Passive power circuit blocks: decoupling capacitors and voltage dividers."""

from typing import TYPE_CHECKING

from ..base import CircuitBlock

if TYPE_CHECKING:
    from kicad_sch_helper import Schematic


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
        auto_footprint: bool = False,
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
            auto_footprint: If True, automatically select footprint based on value
        """
        super().__init__(sch, x, y)
        self.caps = []

        # Place capacitors
        for i, value in enumerate(values):
            cap_x = x + i * spacing
            ref = f"{ref_prefix}{ref_start + i}"
            cap = sch.add_symbol(cap_symbol, cap_x, y, ref, value, auto_footprint=auto_footprint)
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


class VoltageDivider(CircuitBlock):
    """
    Resistive voltage divider with optional output filter capacitor.

    Creates a voltage divider for ADC input scaling, voltage sensing,
    reference voltage generation, or level shifting.

    Schematic:
        VIN ────┬────
                │
               [R1]  (R_top)
                │
        VOUT ───┼────┬────
                │   [C]  (optional filter)
               [R2]  │   (R_bottom)
                │    │
        GND ────┴────┴────

    Ratio calculation:
        VOUT = VIN × (R_bottom / (R_top + R_bottom))

    Ports:
        - VIN: Input voltage (top of R_top)
        - VOUT: Divided output (junction of R_top and R_bottom)
        - GND: Ground (bottom of R_bottom)

    Example:
        from kicad_tools.schematic.blocks import VoltageDivider

        # Simple 2:1 divider
        divider = VoltageDivider(
            sch,
            x=100, y=50,
            r_top="10k",
            r_bottom="10k",
            ref_start=1,
        )

        # With output filter capacitor (for ADC inputs)
        divider = VoltageDivider(
            sch,
            x=100, y=50,
            r_top="100k",
            r_bottom="47k",
            filter_cap="100nF",
            ref_start=1,
        )

        # Access ports
        divider.port("VIN")   # Input voltage
        divider.port("VOUT")  # Divided output
        divider.port("GND")   # Ground
    """

    def __init__(
        self,
        sch: "Schematic",
        x: float,
        y: float,
        r_top: str = "10k",
        r_bottom: str = "10k",
        filter_cap: str | None = None,
        ref_start: int = 1,
        ref_prefix: str = "R",
        cap_ref_prefix: str = "C",
        resistor_spacing: float = 15,
        cap_offset: float = 10,
        resistor_symbol: str = "Device:R",
        cap_symbol: str = "Device:C",
        auto_footprint: bool = False,
    ):
        """
        Create a voltage divider.

        Args:
            sch: Schematic to add to
            x: X coordinate of resistor chain
            y: Y coordinate (top of R_top)
            r_top: Top resistor value (e.g., "10k", "100k")
            r_bottom: Bottom resistor value (e.g., "10k", "47k")
            filter_cap: Optional filter capacitor value (e.g., "100nF"). If None,
                no capacitor is placed.
            ref_start: Starting reference number for components
            ref_prefix: Reference designator prefix for resistors
            cap_ref_prefix: Reference designator prefix for capacitor
            resistor_spacing: Vertical spacing between resistors (mm)
            cap_offset: Horizontal offset for filter capacitor (mm)
            resistor_symbol: KiCad symbol for resistors
            cap_symbol: KiCad symbol for capacitor
            auto_footprint: If True, automatically select footprint based on value
        """
        super().__init__(sch, x, y)
        self.r_top_value = r_top
        self.r_bottom_value = r_bottom
        self.has_filter_cap = filter_cap is not None

        # Reference designators
        r_top_ref = f"{ref_prefix}{ref_start}"
        r_bottom_ref = f"{ref_prefix}{ref_start + 1}"

        # Place top resistor (R1)
        self.r_top = sch.add_symbol(
            resistor_symbol, x, y, r_top_ref, r_top, auto_footprint=auto_footprint
        )

        # Place bottom resistor (R2) below R1
        r_bottom_y = y + resistor_spacing
        self.r_bottom = sch.add_symbol(
            resistor_symbol, x, r_bottom_y, r_bottom_ref, r_bottom, auto_footprint=auto_footprint
        )

        self.components = {
            "R_TOP": self.r_top,
            "R_BOTTOM": self.r_bottom,
        }

        # Get pin positions
        r_top_pin1 = self.r_top.pin_position("1")  # VIN side
        r_top_pin2 = self.r_top.pin_position("2")  # VOUT side
        r_bottom_pin1 = self.r_bottom.pin_position("1")  # VOUT side
        r_bottom_pin2 = self.r_bottom.pin_position("2")  # GND side

        # Wire R_top to R_bottom (VOUT junction)
        sch.add_wire(r_top_pin2, r_bottom_pin1)

        # VOUT junction position (between the two resistors)
        vout_pos = r_top_pin2

        # Add junction marker at VOUT
        sch.add_junction(vout_pos[0], vout_pos[1])

        # Place optional filter capacitor
        if filter_cap is not None:
            cap_ref = f"{cap_ref_prefix}{ref_start}"
            cap_x = x + cap_offset
            # Place cap at same Y as VOUT junction, extending to GND
            cap_y = (vout_pos[1] + r_bottom_pin2[1]) / 2

            self.filter_cap = sch.add_symbol(
                cap_symbol, cap_x, cap_y, cap_ref, filter_cap, auto_footprint=auto_footprint
            )
            self.components["C_FILT"] = self.filter_cap

            # Get cap pin positions
            cap_pin1 = self.filter_cap.pin_position("1")  # VOUT side
            cap_pin2 = self.filter_cap.pin_position("2")  # GND side

            # Wire VOUT junction to cap
            sch.add_wire(vout_pos, (cap_pin1[0], vout_pos[1]))
            sch.add_wire((cap_pin1[0], vout_pos[1]), cap_pin1)

            # Wire cap GND to resistor GND
            sch.add_wire(cap_pin2, (cap_pin2[0], r_bottom_pin2[1]))
            sch.add_wire((cap_pin2[0], r_bottom_pin2[1]), r_bottom_pin2)

            # Add junction at cap-to-VOUT connection
            sch.add_junction(cap_pin1[0], vout_pos[1])

            # GND position is between cap and resistor
            gnd_x = (r_bottom_pin2[0] + cap_pin2[0]) / 2
            gnd_y = r_bottom_pin2[1]
            gnd_pos = (gnd_x, gnd_y)
        else:
            gnd_pos = r_bottom_pin2

        # Define ports
        self.ports = {
            "VIN": r_top_pin1,
            "VOUT": vout_pos,
            "GND": gnd_pos,
        }

        # Store for internal use
        self._r_bottom_gnd = r_bottom_pin2

    def get_ratio(self) -> float:
        """
        Get the voltage division ratio.

        Returns:
            The ratio VOUT/VIN = R_bottom / (R_top + R_bottom).
            For example, a 10k/10k divider returns 0.5.
        """
        r_top = self._parse_resistance(self.r_top_value)
        r_bottom = self._parse_resistance(self.r_bottom_value)
        return r_bottom / (r_top + r_bottom)

    def get_output_voltage(self, input_voltage: float) -> float:
        """
        Calculate the output voltage for a given input voltage.

        Args:
            input_voltage: The input voltage in volts.

        Returns:
            The output voltage in volts.
        """
        return input_voltage * self.get_ratio()

    @staticmethod
    def _parse_resistance(value: str) -> float:
        """
        Parse a resistance string to ohms.

        Supports common suffixes: R (ohms), k (kilo-ohms), M (mega-ohms).

        Args:
            value: Resistance string like "10k", "4.7k", "100R", "1M"

        Returns:
            Resistance in ohms.
        """
        value = value.strip().upper()

        # Handle inline R notation (e.g., "4R7" = 4.7 ohms)
        if "R" in value and not value.endswith("R"):
            parts = value.split("R")
            if len(parts) == 2:
                return float(parts[0]) + float(f"0.{parts[1]}")

        # Handle suffix notation
        if value.endswith("K"):
            return float(value[:-1]) * 1000
        elif value.endswith("M"):
            return float(value[:-1]) * 1_000_000
        elif value.endswith("R"):
            return float(value[:-1])
        else:
            # Try parsing as plain number (assume ohms)
            return float(value)

    def connect_to_rails(
        self,
        vin_rail_y: float,
        gnd_rail_y: float,
        add_junctions: bool = True,
    ) -> None:
        """
        Connect the voltage divider to power rails.

        Args:
            vin_rail_y: Y coordinate of input voltage rail
            gnd_rail_y: Y coordinate of ground rail
            add_junctions: Whether to add junction markers
        """
        sch = self.schematic

        # Connect VIN to rail
        vin_pos = self.ports["VIN"]
        sch.add_wire(vin_pos, (vin_pos[0], vin_rail_y))

        # Connect GND to rail
        gnd_pos = self._r_bottom_gnd
        sch.add_wire(gnd_pos, (gnd_pos[0], gnd_rail_y))

        # If we have a filter cap, also connect its GND
        if self.has_filter_cap:
            cap_gnd = self.filter_cap.pin_position("2")
            sch.add_wire(cap_gnd, (cap_gnd[0], gnd_rail_y))
            if add_junctions:
                sch.add_junction(cap_gnd[0], gnd_rail_y)

        if add_junctions:
            sch.add_junction(vin_pos[0], vin_rail_y)
            sch.add_junction(gnd_pos[0], gnd_rail_y)


def _format_resistance(ohms: float) -> str:
    """
    Format a resistance value to a human-readable string.

    Args:
        ohms: Resistance in ohms

    Returns:
        Formatted string like "10k", "4.7k", "100R", "1M"
    """
    if ohms >= 1_000_000:
        value = ohms / 1_000_000
        suffix = "M"
    elif ohms >= 1000:
        value = ohms / 1000
        suffix = "k"
    else:
        value = ohms
        suffix = "R"

    # Format with appropriate precision
    if value == int(value):
        return f"{int(value)}{suffix}"
    else:
        return f"{value:.1f}{suffix}"


def create_voltage_divider(
    sch: "Schematic",
    x: float,
    y: float,
    input_voltage: float,
    output_voltage: float,
    impedance: str = "medium",
    with_filter: bool = False,
    ref_start: int = 1,
) -> VoltageDivider:
    """
    Create a voltage divider with automatic resistor calculation.

    Calculates resistor values to achieve the target output voltage from the
    given input voltage, using standard E24 resistor values.

    Args:
        sch: Schematic to add to
        x: X coordinate
        y: Y coordinate
        input_voltage: Input voltage in volts
        output_voltage: Target output voltage in volts
        impedance: Impedance level - "low" (<10k), "medium" (10k-100k), "high" (>100k)
        with_filter: If True, add a 100nF filter capacitor
        ref_start: Starting reference number

    Returns:
        VoltageDivider instance with calculated resistor values.

    Example:
        # 12V to 3V divider (4:1 ratio)
        divider = create_voltage_divider(
            sch, x=100, y=50,
            input_voltage=12.0,
            output_voltage=3.0,
            impedance="high",
        )
    """
    # Calculate required ratio
    ratio = output_voltage / input_voltage

    # Select base resistance based on impedance preference
    base_resistances = {
        "low": 1000,  # 1k base
        "medium": 10000,  # 10k base
        "high": 100000,  # 100k base
    }
    base_r = base_resistances.get(impedance.lower(), 10000)

    # Calculate resistor values
    # ratio = R_bottom / (R_top + R_bottom)
    # Solving: R_top = R_bottom * (1 - ratio) / ratio
    r_bottom = base_r
    r_top = r_bottom * (1 - ratio) / ratio

    # Format resistor values
    r_top_str = _format_resistance(r_top)
    r_bottom_str = _format_resistance(r_bottom)

    return VoltageDivider(
        sch,
        x,
        y,
        r_top=r_top_str,
        r_bottom=r_bottom_str,
        filter_cap="100nF" if with_filter else None,
        ref_start=ref_start,
    )
