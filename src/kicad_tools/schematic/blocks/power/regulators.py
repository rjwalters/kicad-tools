"""Voltage regulator circuit blocks: LDO and buck converters."""

from typing import TYPE_CHECKING

from ..base import CircuitBlock

if TYPE_CHECKING:
    from kicad_sch_helper import Schematic


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

    Power Domain Support:
        Use the `domain` parameter to create domain-specific power nets:
        - domain="" (default): Uses standard power symbols (+3V3, GND)
        - domain="A": Creates analog domain (+3V3A, AGND)
        - domain="D": Creates digital domain (+3V3D, DGND)
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
        domain: str = "",
        output_voltage: str = "3V3",
        auto_footprint: bool = False,
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
            domain: Power domain identifier ("" for generic, "A" for analog, "D" for digital)
            output_voltage: Output voltage string (e.g., "3V3", "5V") for net naming
            auto_footprint: If True, automatically select footprint for caps based on value
        """
        super().__init__(sch, x, y)
        self.domain = domain
        self.output_voltage = output_voltage

        if output_caps is None:
            output_caps = ["10uF", "100nF"]

        # Spacing constants
        cap_spacing = 15
        input_cap_offset = -20  # Left of LDO
        output_cap_offset = 20  # Right of LDO

        # Place LDO
        self.ldo = sch.add_symbol(ldo_symbol, x, y, ref, value)

        # Place input capacitor
        c_in_x = x + input_cap_offset
        c_in_ref = f"C{cap_ref_start}"
        self.input_cap = sch.add_symbol(
            "Device:C", c_in_x, y + 15, c_in_ref, input_cap, auto_footprint=auto_footprint
        )

        # Place output capacitors
        self.output_caps = []
        for i, cap_value in enumerate(output_caps):
            c_out_x = x + output_cap_offset + i * cap_spacing
            c_out_ref = f"C{cap_ref_start + 1 + i}"
            cap = sch.add_symbol(
                "Device:C", c_out_x, y + 15, c_out_ref, cap_value, auto_footprint=auto_footprint
            )
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

    def get_vout_net_name(self) -> str:
        """Get the domain-specific output voltage net name.

        Returns:
            Net name based on domain and output voltage:
            - domain="": "+3V3" (generic)
            - domain="A": "+3V3A" (analog)
            - domain="D": "+3V3D" (digital)
        """
        if self.domain:
            return f"+{self.output_voltage}{self.domain}"
        return f"+{self.output_voltage}"

    def get_gnd_net_name(self) -> str:
        """Get the domain-specific ground net name.

        Returns:
            Ground net name based on domain:
            - domain="": "GND" (generic)
            - domain="A": "AGND" (analog)
            - domain="D": "DGND" (digital)
        """
        if self.domain:
            return f"{self.domain}GND"
        return "GND"

    def add_power_labels(
        self,
        vout_rail_y: float,
        gnd_rail_y: float,
        label_x_offset: float = -10,
    ):
        """Add domain-specific global labels for power nets.

        This method adds global labels instead of power symbols, enabling
        proper domain isolation (analog vs digital power planes).

        Args:
            vout_rail_y: Y coordinate where VOUT label should be placed
            gnd_rail_y: Y coordinate where GND label should be placed
            label_x_offset: X offset from LDO center for labels

        Example:
            ldo = LDOBlock(sch, x=100, y=50, domain="A", output_voltage="3V3")
            ldo.add_power_labels(vout_rail_y=30, gnd_rail_y=80)
            # Creates global labels: "+3V3A" and "AGND"
        """
        sch = self.schematic
        label_x = self.x + label_x_offset

        # Add output voltage global label (power input shape for consumers)
        vout_name = self.get_vout_net_name()
        sch.add_global_label(vout_name, label_x, vout_rail_y, shape="input", rotation=0)

        # Add ground global label (passive for bidirectional current flow)
        gnd_name = self.get_gnd_net_name()
        sch.add_global_label(gnd_name, label_x, gnd_rail_y, shape="passive", rotation=0)


class BuckConverter(CircuitBlock):
    """
    Switching buck (step-down) converter with configurable components.

    Creates a complete buck converter subcircuit for efficient voltage
    regulation from higher to lower voltages. Supports asynchronous
    topology (external Schottky diode) and synchronous topology
    (internal low-side FET).

    Schematic (async topology):
        VIN ──┬── [C_in] ──┬── U1 ──┬── [L1] ──┬── [C_out] ──┬── VOUT
              │            │   │    │          │             │
              │            │  FB    │    SW    │             │
              │            │   │   [D1]        │             │
              │            │  [R1]  │          │             │
              │            │   │   [R2]        │             │
              │            │   │    │          │             │
        GND ──┴────────────┴───┴────┴──────────┴─────────────┴──

    Schematic (sync topology - simplified, no external diode):
        VIN ──┬── [C_in] ──┬── U1 ──┬── [L1] ──┬── [C_out] ──┬── VOUT
              │            │   │    │          │             │
              │            │  FB   SW          │             │
              │            │   │    │          │             │
              │            │  [R1] [R2]        │             │
              │            │   │    │          │             │
        GND ──┴────────────┴───┴────┴──────────┴─────────────┴──

    Ports:
        - VIN: Input voltage
        - VOUT: Output voltage
        - GND: Ground
        - SW: Switch node (for debug/monitoring)
        - FB: Feedback node (for adjustable versions)

    Example:
        from kicad_tools.schematic.blocks import BuckConverter

        # Create 24V to 5V buck converter
        buck = BuckConverter(
            sch, x=100, y=100,
            ref="U1",
            value="LM2596-5.0",
            input_voltage=24,
            output_voltage=5,
            input_cap="100uF",
            output_cap="220uF",
            inductor="33uH",
            diode="SS34",
        )

        # Connect to rails
        buck.connect_to_rails(
            vin_rail_y=30,
            vout_rail_y=50,
            gnd_rail_y=200,
        )
    """

    def __init__(
        self,
        sch: "Schematic",
        x: float,
        y: float,
        ref: str = "U1",
        value: str = "LM2596-5.0",
        regulator_symbol: str = "Regulator_Switching:LM2596S-5",
        input_voltage: float = 24.0,
        output_voltage: float = 5.0,
        topology: str = "async",
        input_cap: str = "100uF",
        output_cap: str = "220uF",
        inductor: str = "33uH",
        diode: str = "SS34",
        feedback_divider: bool = False,
        r_top: str = "10k",
        r_bottom: str = "3.3k",
        cap_ref_start: int = 1,
        inductor_ref: str = "L1",
        diode_ref: str = "D1",
        auto_footprint: bool = False,
    ):
        """
        Create a buck converter power supply block.

        Args:
            sch: Schematic to add to
            x: X coordinate of regulator center
            y: Y coordinate of regulator center
            ref: Regulator reference designator
            value: Regulator value label (e.g., "LM2596-5.0", "MP1584EN")
            regulator_symbol: KiCad symbol for the regulator IC
            input_voltage: Input voltage in volts
            output_voltage: Output voltage in volts
            topology: "async" (external Schottky diode) or "sync" (internal FET)
            input_cap: Input capacitor value (e.g., "100uF")
            output_cap: Output capacitor value (e.g., "220uF")
            inductor: Inductor value (e.g., "33uH", "47uH")
            diode: Schottky diode part number (for async topology)
            feedback_divider: If True, add feedback resistor divider (for adjustable versions)
            r_top: Top feedback resistor value (if feedback_divider=True)
            r_bottom: Bottom feedback resistor value (if feedback_divider=True)
            cap_ref_start: Starting reference number for capacitors
            inductor_ref: Reference designator for inductor
            diode_ref: Reference designator for diode
            auto_footprint: If True, automatically select footprints
        """
        super().__init__(sch, x, y)
        self.input_voltage = input_voltage
        self.output_voltage = output_voltage
        self.topology = topology
        self.has_feedback_divider = feedback_divider

        # Spacing constants
        input_cap_offset = -25  # Left of regulator
        output_cap_offset = 50  # Right of regulator
        inductor_offset = 25  # Between regulator and output cap
        diode_y_offset = 20  # Below switch node

        # Place regulator IC
        self.regulator = sch.add_symbol(regulator_symbol, x, y, ref, value)

        # Place input capacitor
        c_in_x = x + input_cap_offset
        c_in_ref = f"C{cap_ref_start}"
        self.input_cap = sch.add_symbol(
            "Device:C_Polarized", c_in_x, y + 15, c_in_ref, input_cap, auto_footprint=auto_footprint
        )

        # Place inductor
        l_x = x + inductor_offset
        self.inductor = sch.add_symbol(
            "Device:L", l_x, y, inductor_ref, inductor, auto_footprint=auto_footprint
        )

        # Place output capacitor
        c_out_x = x + output_cap_offset
        c_out_ref = f"C{cap_ref_start + 1}"
        self.output_cap = sch.add_symbol(
            "Device:C_Polarized",
            c_out_x,
            y + 15,
            c_out_ref,
            output_cap,
            auto_footprint=auto_footprint,
        )

        # Store all components
        self.components = {
            "REGULATOR": self.regulator,
            "C_IN": self.input_cap,
            "L": self.inductor,
            "C_OUT": self.output_cap,
        }

        # Place diode for async topology
        if topology == "async":
            d_x = x + inductor_offset
            d_y = y + diode_y_offset
            self.diode = sch.add_symbol(
                "Device:D_Schottky",
                d_x,
                d_y,
                diode_ref,
                diode,
                rotation=90,
                auto_footprint=auto_footprint,
            )
            self.components["D"] = self.diode

        # Add feedback divider if requested (for adjustable versions)
        if feedback_divider:
            fb_x = x + output_cap_offset + 15
            r_top_ref = f"R{cap_ref_start}"
            r_bottom_ref = f"R{cap_ref_start + 1}"

            self.r_fb_top = sch.add_symbol(
                "Device:R", fb_x, y + 5, r_top_ref, r_top, auto_footprint=auto_footprint
            )
            self.r_fb_bottom = sch.add_symbol(
                "Device:R", fb_x, y + 20, r_bottom_ref, r_bottom, auto_footprint=auto_footprint
            )
            self.components["R_FB_TOP"] = self.r_fb_top
            self.components["R_FB_BOTTOM"] = self.r_fb_bottom

            # Wire feedback divider internally
            r_top_2 = self.r_fb_top.pin_position("2")
            r_bottom_1 = self.r_fb_bottom.pin_position("1")
            sch.add_wire(r_top_2, r_bottom_1)

        # Get regulator pin positions (depends on actual symbol pinout)
        # Common pins: VIN, VOUT/SW, GND, FB, ON/OFF
        # Note: Pin names vary by symbol; we'll use generic approach
        try:
            vin_pos = self.regulator.pin_position("VIN")
        except KeyError:
            vin_pos = self.regulator.pin_position("IN")

        try:
            sw_pos = self.regulator.pin_position("OUT")
        except KeyError:
            try:
                sw_pos = self.regulator.pin_position("SW")
            except KeyError:
                sw_pos = self.regulator.pin_position("VOUT")

        try:
            gnd_pos = self.regulator.pin_position("GND")
        except KeyError:
            gnd_pos = self.regulator.pin_position("VSS")

        # Get optional FB pin
        try:
            fb_pos = self.regulator.pin_position("FB")
            has_fb_pin = True
        except KeyError:
            fb_pos = None
            has_fb_pin = False

        # Get inductor positions
        l_in = self.inductor.pin_position("1")
        l_out = self.inductor.pin_position("2")

        # Wire regulator SW/OUT to inductor input
        sch.add_wire(sw_pos, l_in)

        # Wire diode for async topology (cathode to SW node, anode to GND)
        if topology == "async":
            d_cathode = self.diode.pin_position("K")

            # Connect diode cathode to switch node
            sch.add_wire(d_cathode, (l_in[0], d_cathode[1]))
            sch.add_wire((l_in[0], d_cathode[1]), l_in)
            sch.add_junction(l_in[0], l_in[1])

        # Get capacitor positions
        c_out_pos = self.output_cap.pin_position("1")

        # Wire inductor output to output capacitor
        sch.add_wire(l_out, (l_out[0], c_out_pos[1]))
        sch.add_wire((l_out[0], c_out_pos[1]), c_out_pos)
        sch.add_junction(l_out[0], c_out_pos[1])

        # Wire feedback divider to output if present
        if feedback_divider:
            r_top_1 = self.r_fb_top.pin_position("1")
            # Connect top of divider to VOUT
            sch.add_wire(r_top_1, (r_top_1[0], c_out_pos[1]))
            sch.add_junction(r_top_1[0], c_out_pos[1])

            # Connect FB divider midpoint to FB pin if available
            if has_fb_pin and fb_pos:
                fb_node = self.r_fb_top.pin_position("2")
                sch.add_wire(fb_pos, (fb_node[0], fb_pos[1]))
                sch.add_wire((fb_node[0], fb_pos[1]), fb_node)

        # Define ports
        self.ports = {
            "VIN": vin_pos,
            "VOUT": c_out_pos,
            "GND": gnd_pos,
            "SW": l_in,  # Switch node
        }
        if has_fb_pin and fb_pos:
            self.ports["FB"] = fb_pos

        # Store internal positions for rail connections
        self._sw_node = l_in
        self._vout_node = c_out_pos

    def connect_to_rails(
        self,
        vin_rail_y: float,
        vout_rail_y: float,
        gnd_rail_y: float,
        add_junctions: bool = True,
    ):
        """
        Connect buck converter and caps to power rails.

        Args:
            vin_rail_y: Y coordinate of input voltage rail
            vout_rail_y: Y coordinate of output voltage rail
            gnd_rail_y: Y coordinate of ground rail
            add_junctions: Whether to add junction markers
        """
        sch = self.schematic

        # Connect regulator VIN to input rail
        vin_pos = self.ports["VIN"]
        sch.add_wire(vin_pos, (vin_pos[0], vin_rail_y))

        # Connect regulator GND to ground rail
        gnd_pos = self.ports["GND"]
        sch.add_wire(gnd_pos, (gnd_pos[0], gnd_rail_y))

        # Wire input cap
        sch.wire_decoupling_cap(self.input_cap, vin_rail_y, gnd_rail_y)

        # Wire output cap
        sch.wire_decoupling_cap(self.output_cap, vout_rail_y, gnd_rail_y)

        # Connect VOUT to output rail
        vout_pos = self.ports["VOUT"]
        sch.add_wire(vout_pos, (vout_pos[0], vout_rail_y))

        # Connect diode anode to GND (async topology)
        if self.topology == "async":
            d_anode = self.diode.pin_position("A")
            sch.add_wire(d_anode, (d_anode[0], gnd_rail_y))
            if add_junctions:
                sch.add_junction(d_anode[0], gnd_rail_y)

        # Connect feedback divider bottom to GND if present
        if self.has_feedback_divider:
            r_bottom_2 = self.r_fb_bottom.pin_position("2")
            sch.add_wire(r_bottom_2, (r_bottom_2[0], gnd_rail_y))
            if add_junctions:
                sch.add_junction(r_bottom_2[0], gnd_rail_y)

        if add_junctions:
            sch.add_junction(vin_pos[0], vin_rail_y)
            sch.add_junction(gnd_pos[0], gnd_rail_y)
            sch.add_junction(vout_pos[0], vout_rail_y)

    def get_efficiency_estimate(self) -> float:
        """
        Get estimated efficiency for this buck converter.

        Returns:
            Estimated efficiency as a decimal (0.80-0.95 typical).
            Actual efficiency depends on load current, switching frequency,
            and component quality.
        """
        # Rough efficiency estimate based on topology and voltage ratio
        duty_cycle = self.output_voltage / self.input_voltage

        if self.topology == "sync":
            # Synchronous buck typically 90-95% efficient
            base_efficiency = 0.92
        else:
            # Async buck typically 80-90% efficient (diode losses)
            base_efficiency = 0.85

        # Efficiency drops at extreme duty cycles
        if duty_cycle < 0.1 or duty_cycle > 0.9:
            return base_efficiency * 0.95

        return base_efficiency


# Factory functions


def create_3v3_ldo(
    sch: "Schematic",
    x: float,
    y: float,
    ref: str = "U1",
    cap_ref_start: int = 1,
    domain: str = "",
) -> LDOBlock:
    """Create a 3.3V LDO block with standard capacitors.

    Args:
        sch: Schematic to add to
        x: X coordinate of LDO center
        y: Y coordinate of LDO center
        ref: LDO reference designator
        cap_ref_start: Starting reference number for capacitors
        domain: Power domain identifier ("" for generic, "A" for analog, "D" for digital)

    Returns:
        LDOBlock with domain-aware power net naming

    Example:
        # Create analog and digital 3.3V LDOs
        analog_ldo = create_3v3_ldo(sch, 50, 50, "U1", domain="A")
        digital_ldo = create_3v3_ldo(sch, 150, 50, "U2", domain="D")

        # Wire to rails and add domain-specific labels
        analog_ldo.connect_to_rails(vin_rail_y=30, vout_rail_y=40, gnd_rail_y=80)
        analog_ldo.add_power_labels(vout_rail_y=40, gnd_rail_y=80)
        # Creates: +3V3A and AGND global labels

        digital_ldo.connect_to_rails(vin_rail_y=30, vout_rail_y=40, gnd_rail_y=80)
        digital_ldo.add_power_labels(vout_rail_y=40, gnd_rail_y=80)
        # Creates: +3V3D and DGND global labels
    """
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
        domain=domain,
        output_voltage="3V3",
    )


def create_5v_buck(
    sch: "Schematic",
    x: float,
    y: float,
    ref: str = "U1",
    input_voltage: float = 24.0,
    cap_ref_start: int = 1,
) -> BuckConverter:
    """
    Create a 5V buck converter with sensible defaults.

    Common configuration for 12V or 24V to 5V conversion using
    the LM2596-5.0 fixed-output regulator.

    Args:
        sch: Schematic to add to
        x: X coordinate of regulator center
        y: Y coordinate of regulator center
        ref: Regulator reference designator
        input_voltage: Input voltage (typically 12V or 24V)
        cap_ref_start: Starting reference number for capacitors

    Returns:
        BuckConverter instance configured for 5V output.

    Example:
        buck = create_5v_buck(sch, x=100, y=100, input_voltage=24)
        buck.connect_to_rails(vin_rail_y=30, vout_rail_y=50, gnd_rail_y=200)
    """
    return BuckConverter(
        sch,
        x,
        y,
        ref=ref,
        value="LM2596-5.0",
        regulator_symbol="Regulator_Switching:LM2596S-5",
        input_voltage=input_voltage,
        output_voltage=5.0,
        topology="async",
        input_cap="100uF",
        output_cap="220uF",
        inductor="33uH",
        diode="SS34",
        feedback_divider=False,
        cap_ref_start=cap_ref_start,
    )


def create_3v3_buck(
    sch: "Schematic",
    x: float,
    y: float,
    ref: str = "U1",
    input_voltage: float = 12.0,
    cap_ref_start: int = 1,
) -> BuckConverter:
    """
    Create a 3.3V buck converter with sensible defaults.

    Common configuration for 5V or 12V to 3.3V conversion using
    the LM2596-3.3 fixed-output regulator.

    Args:
        sch: Schematic to add to
        x: X coordinate of regulator center
        y: Y coordinate of regulator center
        ref: Regulator reference designator
        input_voltage: Input voltage (typically 5V or 12V)
        cap_ref_start: Starting reference number for capacitors

    Returns:
        BuckConverter instance configured for 3.3V output.

    Example:
        buck = create_3v3_buck(sch, x=100, y=100, input_voltage=12)
        buck.connect_to_rails(vin_rail_y=30, vout_rail_y=50, gnd_rail_y=200)
    """
    return BuckConverter(
        sch,
        x,
        y,
        ref=ref,
        value="LM2596-3.3",
        regulator_symbol="Regulator_Switching:LM2596S-3.3",
        input_voltage=input_voltage,
        output_voltage=3.3,
        topology="async",
        input_cap="100uF",
        output_cap="220uF",
        inductor="33uH",
        diode="SS34",
        feedback_divider=False,
        cap_ref_start=cap_ref_start,
    )


def create_12v_buck(
    sch: "Schematic",
    x: float,
    y: float,
    ref: str = "U1",
    input_voltage: float = 24.0,
    cap_ref_start: int = 1,
) -> BuckConverter:
    """
    Create a 12V buck converter with sensible defaults.

    Common configuration for 24V or 48V to 12V conversion using
    the LM2596-12 fixed-output regulator.

    Args:
        sch: Schematic to add to
        x: X coordinate of regulator center
        y: Y coordinate of regulator center
        ref: Regulator reference designator
        input_voltage: Input voltage (typically 24V or 48V)
        cap_ref_start: Starting reference number for capacitors

    Returns:
        BuckConverter instance configured for 12V output.

    Example:
        buck = create_12v_buck(sch, x=100, y=100, input_voltage=24)
        buck.connect_to_rails(vin_rail_y=30, vout_rail_y=50, gnd_rail_y=200)
    """
    return BuckConverter(
        sch,
        x,
        y,
        ref=ref,
        value="LM2596-12",
        regulator_symbol="Regulator_Switching:LM2596S-12",
        input_voltage=input_voltage,
        output_voltage=12.0,
        topology="async",
        input_cap="100uF",
        output_cap="330uF",
        inductor="68uH",
        diode="SS54",
        feedback_divider=False,
        cap_ref_start=cap_ref_start,
    )
