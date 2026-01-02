"""Power-related circuit blocks: LDO, decoupling caps, power inputs."""

from typing import TYPE_CHECKING

from .base import CircuitBlock

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
        super().__init__(sch, x, y)
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
        super().__init__(sch, x, y)

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
        super().__init__(sch, x, y)
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
        super().__init__(sch, x, y)
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
        super().__init__(sch, x, y)
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


# Factory functions


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
        """
        super().__init__(sch, x, y)
        self.r_top_value = r_top
        self.r_bottom_value = r_bottom
        self.has_filter_cap = filter_cap is not None

        # Reference designators
        r_top_ref = f"{ref_prefix}{ref_start}"
        r_bottom_ref = f"{ref_prefix}{ref_start + 1}"

        # Place top resistor (R1)
        self.r_top = sch.add_symbol(resistor_symbol, x, y, r_top_ref, r_top)

        # Place bottom resistor (R2) below R1
        r_bottom_y = y + resistor_spacing
        self.r_bottom = sch.add_symbol(resistor_symbol, x, r_bottom_y, r_bottom_ref, r_bottom)

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

            self.filter_cap = sch.add_symbol(cap_symbol, cap_x, cap_y, cap_ref, filter_cap)
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
