"""Power input circuit blocks: barrel jack, USB, and battery inputs."""

from typing import TYPE_CHECKING

from ..base import CircuitBlock

if TYPE_CHECKING:
    from kicad_sch_helper import Schematic


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
        cap_symbol: str = "Device:C_Polarized",
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
