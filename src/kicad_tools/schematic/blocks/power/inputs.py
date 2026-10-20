"""Power input circuit blocks: barrel jack, USB, and battery inputs."""

from typing import TYPE_CHECKING

from ..base import CircuitBlock

if TYPE_CHECKING:
    from kicad_sch_helper import Schematic

    from ...models.symbol import SymbolInstance

Point = tuple[float, float]

# Horizontal jog (one 2.54 mm grid step) between an input pin and the P-FET
# drain lead, so the input wire never runs along the gate tie.
_INPUT_JOG = 2.54


def _wire_path(sch: "Schematic", *points: Point) -> None:
    """Draw an orthogonal polyline through ``points``, skipping zero-length legs."""
    for a, b in zip(points, points[1:], strict=False):
        if a != b:
            sch.add_wire(a, b)


def _wire_reverse_pfet(
    sch: "Schematic",
    pfet: "SymbolInstance",
    vin: Point,
    gnd: Point,
    tie_gate: bool = True,
) -> Point:
    """Wire a high-side reverse-polarity P-FET between ``vin`` and its load.

    The input goes to the **drain** and the **source** feeds the load: the body
    diode (anode on the drain) conducts at power-up, the gate tied to ``gnd``
    then sees Vgs = -VOUT and the channel turns on.  With the input reversed
    the body diode is reverse-biased and Vgs >= 0, so the FET blocks.  (The
    old wiring put the input on the source, which leaves the body diode
    conducting from the load back into a reversed input; issue #7399.)

    With ``tie_gate=False`` the gate is left unwired for the caller to bias
    (``BarrelJackInput`` drops a gate resistor and Zener clamp on it).

    Returns the source position, the protected output.
    """
    drain = pfet.pin_position("D")
    gate = pfet.pin_position("G")
    jog_x = vin[0] + _INPUT_JOG
    _wire_path(sch, vin, (jog_x, vin[1]), (jog_x, drain[1]), drain)
    if tie_gate:
        _wire_path(sch, gate, (gate[0], gnd[1]), gnd)
    return pfet.pin_position("S")


class BarrelJackInput(CircuitBlock):
    """
    Barrel jack power input with optional reverse polarity protection.

    Schematic (with P-FET protection):
        VIN ──D[Q]S──┬── VOUT
               │     │
               G  [C_filt]
               │     │
        GND ───┴─────┘

    The P-FET's drain takes the input and its source feeds the load (see
    ``_wire_reverse_pfet``).  The gate returns to ground through ``R_GATE``
    and a Zener ``D_GATE`` (cathode on the source, anode on the gate) clamps
    Vgs so a 12 V or 24 V jack cannot exceed the Si2301's +/-8 V Vgs(max).

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
        pfet_symbol: str = "Device:Q_PMOS",
        diode_symbol: str = "Device:D_Schottky",
        cap_symbol: str = "Device:C_Polarized",
        resistor_symbol: str = "Device:R",
        zener_symbol: str = "Device:D_Zener",
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
            resistor_symbol: KiCad symbol for the P-FET gate resistor
            zener_symbol: KiCad symbol for the P-FET gate-clamp Zener
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
        # ``Connector:Barrel_Jack_Switch`` leaves its pins unnamed: pin 1 is the
        # centre pin (tip, positive), pin 2 the sleeve (ground) and pin 3 the
        # sleeve switch, which this block leaves unconnected (issue #7399).
        jack_tip = self.jack.pin_position("1")  # Positive (centre pin)
        jack_sleeve = self.jack.pin_position("2")  # Ground (sleeve)

        # Define VIN port at jack tip
        self.ports = {"VIN": jack_tip, "GND": jack_sleeve}

        # Add protection device
        if protection == "pfet":
            q_ref = f"Q{base_num}"
            q_x = x + protection_offset
            self.pfet = sch.add_symbol(pfet_symbol, q_x, y, q_ref, "Si2301")
            self.components["Q"] = self.pfet

            # Jack tip -> drain; output from the source.  The gate is biased
            # below instead of tied straight to the sleeve.
            output_pos = _wire_reverse_pfet(sch, self.pfet, jack_tip, jack_sleeve, tie_gate=False)

            # The conducting FET sees Vgs = -VIN, which a 12 V jack drives
            # past the Si2301's +/-8 V Vgs(max) (Vishay Si2301 datasheet,
            # absolute maximum ratings).  R_GATE returns the gate to the
            # sleeve and a 5.1 V Zener (cathode on the source, anode on the
            # gate) clamps |Vgs| to ~5.1 V: above the Si2301's ~1 V maximum
            # Vgs(th) so the channel still enhances, below the 8 V limit.
            # Both parts sit below the FET, clear of the source/output wire.
            gate = self.pfet.pin_position("G")
            source = self.pfet.pin_position("S")
            rail_y = gate[1] + 7.62  # gate net rail, below the FET body
            r_gate = sch.add_symbol(resistor_symbol, gate[0], rail_y + 3.81, f"R{base_num}", "10k")
            d_gate = sch.add_symbol(
                zener_symbol,
                gate[0] + 3.81,  # one 1.5-grid step clear of R_GATE
                rail_y + 3.81,
                f"D{base_num}",
                "5.1V",
                rotation=90,  # anode up, cathode down
            )
            self.components["R_GATE"] = r_gate
            self.components["D_GATE"] = d_gate

            r_top = r_gate.pin_position("1")
            r_bot = r_gate.pin_position("2")
            a_pin = d_gate.pin_position("A")
            k_pin = d_gate.pin_position("K")
            # Gate net: gate -> R_GATE pin 1, and across to the Zener anode.
            _wire_path(sch, gate, r_top)
            _wire_path(sch, r_top, (a_pin[0], r_top[1]), a_pin)
            # R_GATE pin 2 -> sleeve (ground).
            _wire_path(sch, r_bot, (jack_sleeve[0], r_bot[1]), jack_sleeve)
            # Zener cathode -> source net.
            _wire_path(sch, k_pin, (source[0], k_pin[1]), source)

        elif protection == "diode":
            d_ref = f"D{base_num}"
            d_x = x + protection_offset
            # Rotated 180 degrees so the anode faces the input and the
            # cathode faces the filter cap; at 0 degrees the output wire
            # would run through the anode pin and short the diode.
            self.diode = sch.add_symbol(diode_symbol, d_x, y, d_ref, "SS34", rotation=180)
            self.components["D"] = self.diode

            # Wire jack tip to diode anode
            diode_anode = self.diode.pin_position("A")
            diode_cathode = self.diode.pin_position("K")

            jog_x = jack_tip[0] + _INPUT_JOG
            _wire_path(sch, jack_tip, (jog_x, jack_tip[1]), (jog_x, diode_anode[1]), diode_anode)

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

        # (With no protection the input feeds the cap directly; it used to be
        # left unwired, issue #7399.)
        _wire_path(sch, output_pos, (cap_pos[0], output_pos[1]), cap_pos)

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
        jack_sleeve = self.jack.pin_position("2")  # Sleeve (unnamed pin 2)
        sch.add_wire(jack_sleeve, (jack_sleeve[0], gnd_rail_y))

        if add_junctions:
            sch.add_junction(cap_neg[0], gnd_rail_y)
            sch.add_junction(jack_sleeve[0], gnd_rail_y)


class USBPowerInput(CircuitBlock):
    """
    USB power input with optional fuse protection.

    This block models a **power-only** USB inlet (VBUS in -> fuse -> filter cap -> V5).
    No connector symbol is placed and there are no D+/D- (data) ports — the input is
    a single ``VBUS_IN`` coordinate intended to be wired to an external VBUS source
    or a USB-as-wall-wart receptacle.

    For a USB connector with both **data and power** lines (D+/D-, CC1/CC2, GND,
    optional ESD on data, optional VBUS TVS), use ``USBConnector`` (and the
    ``create_usb_type_c`` / ``create_usb_micro_b`` factories) from
    ``kicad_tools.schematic.blocks.interface.usb`` instead. Pair it with
    ``DecouplingCaps`` for VBUS bypass.

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
        VBAT_IN ──D[Q]S──┬── VBAT
                   │     │
                   G  [C_filt]
                   │     │
        GND ───────┴─────┘

    The P-FET's drain takes the input and its source feeds the load, with
    the gate tied to ground (see ``_wire_reverse_pfet``).

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
        pfet_symbol: str = "Device:Q_PMOS",
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

            # Connector + -> drain, gate -> connector -; output from the source.
            output_pos = _wire_reverse_pfet(sch, self.pfet, conn_pos, conn_neg)

        elif protection == "diode":
            d_ref = f"D{base_num}"
            d_x = x + protection_offset
            # Rotated 180 degrees so the anode faces the input and the
            # cathode faces the filter cap; at 0 degrees the output wire
            # would run through the anode pin and short the diode.
            self.diode = sch.add_symbol(diode_symbol, d_x, y, d_ref, "SS34", rotation=180)
            self.components["D"] = self.diode

            # Wire connector positive to diode anode
            diode_anode = self.diode.pin_position("A")
            diode_cathode = self.diode.pin_position("K")

            jog_x = conn_pos[0] + _INPUT_JOG
            _wire_path(sch, conn_pos, (jog_x, conn_pos[1]), (jog_x, diode_anode[1]), diode_anode)

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

        # (With no protection the input feeds the cap directly; it used to be
        # left unwired, issue #7399.)
        _wire_path(sch, output_pos, (cap_pos[0], output_pos[1]), cap_pos)

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
    """Create a USB power input with polyfuse protection.

    This is a **power-only** USB inlet: VBUS in -> polyfuse -> 10uF filter cap -> V5.
    It does **not** place a USB connector symbol and exposes no D+/D- ports.

    For a USB connector that carries both data and power, use
    :func:`kicad_tools.schematic.blocks.interface.usb.create_usb_type_c` (or
    ``create_usb_micro_b``) instead, optionally paired with ``DecouplingCaps`` for
    VBUS bypass.
    """
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
