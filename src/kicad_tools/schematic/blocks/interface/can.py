"""CAN bus transceiver interface blocks with termination and ESD protection."""

from typing import TYPE_CHECKING

from ..base import CircuitBlock

if TYPE_CHECKING:
    from kicad_sch_helper import Schematic, SymbolInstance


class CANTransceiver(CircuitBlock):
    """
    CAN bus transceiver with optional termination and ESD protection.

    Places a CAN transceiver IC with proper termination, protection, and decoupling.
    CAN bus is widely used in automotive, industrial, and IoT applications.

    Schematic (with termination and ESD):
        VCC ──┬── [C] ──┬── U ──┬── CANH ──[TVS]──┬── BUS_H
              │         │  CAN  │                  │
              └─────────┤  XCVR │         [R_term] │  (120Ω)
                        │       │                  │
        GND ────────────┴───────┴── CANL ──[TVS]──┴── BUS_L

    Split termination (better EMC):
        CANH ────┬────
                [R1]  60Ω
                 │
                [C]   4.7nF (to GND)
                 │
                [R2]  60Ω
        CANL ────┴────

    Ports:
        - VCC: Power input (after decoupling cap)
        - GND: Ground
        - TXD: Transmit data (from MCU)
        - RXD: Receive data (to MCU)
        - CANH: CAN High bus line (after protection if enabled)
        - CANL: CAN Low bus line (after protection if enabled)
        - STBY: Standby control (if available on transceiver)

    Example:
        from kicad_tools.schematic.blocks import CANTransceiver

        # Basic CAN transceiver
        can = CANTransceiver(
            sch,
            x=100, y=50,
            transceiver="MCP2551",
            termination=True,
            esd_protection=True,
            ref_prefix="U",
        )

        # 3.3V CAN transceiver with split termination
        can = CANTransceiver(
            sch,
            x=100, y=50,
            transceiver="SN65HVD230",
            termination="split",
            split_cap="4.7nF",
            esd_protection=True,
            ref_prefix="U",
        )

        # Wire to MCU
        sch.add_wire(can.port("TXD"), mcu.port("CAN_TX"))
        sch.add_wire(can.port("RXD"), mcu.port("CAN_RX"))
    """

    # Common CAN transceiver configurations
    TRANSCEIVER_CONFIGS = {
        "MCP2551": {
            "symbol": "Interface_CAN_LIN:MCP2551-I-SN",
            "voltage": 5.0,
            "has_stby": False,
            "pins": {
                "VDD": "VDD",
                "VSS": "VSS",
                "TXD": "TXD",
                "RXD": "RXD",
                "CANH": "CANH",
                "CANL": "CANL",
            },
        },
        "MCP2562": {
            "symbol": "Interface_CAN_LIN:MCP2562-E-SN",
            "voltage": 5.0,
            "has_stby": True,
            "pins": {
                "VDD": "VDD",
                "VSS": "VSS",
                "TXD": "TXD",
                "RXD": "RXD",
                "CANH": "CANH",
                "CANL": "CANL",
                "STBY": "STBY",
            },
        },
        "SN65HVD230": {
            "symbol": "Interface_CAN_LIN:SN65HVD230",
            "voltage": 3.3,
            "has_stby": False,
            "pins": {
                "VDD": "VCC",
                "VSS": "GND",
                "TXD": "D",
                "RXD": "R",
                "CANH": "CANH",
                "CANL": "CANL",
            },
        },
        "TJA1050": {
            "symbol": "Interface_CAN_LIN:TJA1050",
            "voltage": 5.0,
            "has_stby": False,
            "pins": {
                "VDD": "VCC",
                "VSS": "GND",
                "TXD": "TXD",
                "RXD": "RXD",
                "CANH": "CANH",
                "CANL": "CANL",
            },
        },
        "TJA1051": {
            "symbol": "Interface_CAN_LIN:TJA1051T",
            "voltage": 5.0,
            "has_stby": True,
            "pins": {
                "VDD": "VCC",
                "VSS": "GND",
                "TXD": "TXD",
                "RXD": "RXD",
                "CANH": "CANH",
                "CANL": "CANL",
                "STBY": "S",
            },
        },
    }

    def __init__(
        self,
        sch: "Schematic",
        x: float,
        y: float,
        transceiver: str = "MCP2551",
        termination: bool | str = False,
        split_cap: str = "4.7nF",
        esd_protection: bool = False,
        decoupling_cap: str = "100nF",
        ref_prefix: str = "U",
        cap_ref_start: int = 1,
        resistor_ref_start: int = 1,
        tvs_ref_start: int = 1,
        transceiver_symbol: str | None = None,
        cap_symbol: str = "Device:C",
        resistor_symbol: str = "Device:R",
        tvs_symbol: str = "Device:D_TVS",
        tvs_value: str = "PESD1CAN",
    ):
        """
        Create a CAN transceiver block.

        Args:
            sch: Schematic to add to
            x: X coordinate of transceiver center
            y: Y coordinate of transceiver center
            transceiver: Transceiver type - "MCP2551", "MCP2562", "SN65HVD230",
                "TJA1050", or "TJA1051"
            termination: False for no termination, True for 120Ω, or "split" for
                split termination (2×60Ω + cap)
            split_cap: Capacitor value for split termination (default "4.7nF")
            esd_protection: If True, add TVS diodes on CANH/CANL
            decoupling_cap: Decoupling capacitor value for VCC
            ref_prefix: Reference designator prefix (e.g., "U" or "U1")
            cap_ref_start: Starting reference number for capacitors
            resistor_ref_start: Starting reference number for resistors
            tvs_ref_start: Starting reference number for TVS diodes
            transceiver_symbol: KiCad symbol for transceiver (auto-selected if None)
            cap_symbol: KiCad symbol for capacitors
            resistor_symbol: KiCad symbol for resistors
            tvs_symbol: KiCad symbol for TVS diodes
            tvs_value: Part value for CAN TVS diode (e.g., "PESD1CAN")
        """
        super().__init__(sch, x, y)
        self.transceiver_type = transceiver
        self.termination = termination
        self.esd_protection = esd_protection

        # Validate transceiver type
        if transceiver not in self.TRANSCEIVER_CONFIGS:
            raise ValueError(
                f"Unknown transceiver '{transceiver}'. "
                f"Valid options: {list(self.TRANSCEIVER_CONFIGS.keys())}"
            )

        config = self.TRANSCEIVER_CONFIGS[transceiver]
        self.has_stby = config["has_stby"]

        # Parse reference prefix
        if ref_prefix[-1].isdigit():
            u_ref = ref_prefix
        else:
            u_ref = f"{ref_prefix}1"

        # Determine transceiver symbol
        if transceiver_symbol is None:
            transceiver_symbol = config["symbol"]

        # Component spacing
        cap_offset_x = -20  # Decoupling cap to the left
        cap_offset_y = 15  # Below transceiver
        termination_offset_x = 30  # Termination to the right
        tvs_offset_x = 25  # TVS between transceiver and termination

        # Place transceiver
        self.transceiver = sch.add_symbol(transceiver_symbol, x, y, u_ref, transceiver)
        self.components = {"XCVR": self.transceiver}

        # Get transceiver pin positions using config mapping
        pin_map = config["pins"]
        xcvr_vdd = self.transceiver.pin_position(pin_map["VDD"])
        xcvr_vss = self.transceiver.pin_position(pin_map["VSS"])
        xcvr_txd = self.transceiver.pin_position(pin_map["TXD"])
        xcvr_rxd = self.transceiver.pin_position(pin_map["RXD"])
        xcvr_canh = self.transceiver.pin_position(pin_map["CANH"])
        xcvr_canl = self.transceiver.pin_position(pin_map["CANL"])

        # Place decoupling capacitor
        c_ref = f"C{cap_ref_start}"
        c_x = x + cap_offset_x
        c_y = y + cap_offset_y
        self.decoupling_cap = sch.add_symbol(cap_symbol, c_x, c_y, c_ref, decoupling_cap)
        self.components["C_DEC"] = self.decoupling_cap

        # Track bus output positions (after protection/termination)
        canh_output = xcvr_canh
        canl_output = xcvr_canl

        # Initialize optional component containers
        self.tvs_diodes: dict[str, SymbolInstance] = {}
        self.termination_resistors: list = []
        self.split_cap_component = None

        # Add ESD protection if requested
        tvs_idx = 0
        if esd_protection:
            # TVS for CANH
            tvs_h_ref = f"D{tvs_ref_start + tvs_idx}"
            tvs_h_x = x + tvs_offset_x
            tvs_h_y = xcvr_canh[1]
            self.tvs_canh = sch.add_symbol(tvs_symbol, tvs_h_x, tvs_h_y, tvs_h_ref, tvs_value)
            self.tvs_diodes["CANH"] = self.tvs_canh
            self.components["TVS_CANH"] = self.tvs_canh
            tvs_idx += 1

            # Wire CANH to TVS
            tvs_h_anode = self.tvs_canh.pin_position("A")
            tvs_h_cathode = self.tvs_canh.pin_position("K")
            sch.add_wire(xcvr_canh, tvs_h_anode)

            # TVS cathode goes to ground (clamp)
            sch.add_wire(tvs_h_cathode, (tvs_h_cathode[0], xcvr_vss[1]))

            # CANH output is after TVS
            canh_output = (tvs_h_anode[0] + 5, tvs_h_anode[1])

            # TVS for CANL
            tvs_l_ref = f"D{tvs_ref_start + tvs_idx}"
            tvs_l_x = x + tvs_offset_x
            tvs_l_y = xcvr_canl[1]
            self.tvs_canl = sch.add_symbol(tvs_symbol, tvs_l_x, tvs_l_y, tvs_l_ref, tvs_value)
            self.tvs_diodes["CANL"] = self.tvs_canl
            self.components["TVS_CANL"] = self.tvs_canl
            tvs_idx += 1

            # Wire CANL to TVS
            tvs_l_anode = self.tvs_canl.pin_position("A")
            tvs_l_cathode = self.tvs_canl.pin_position("K")
            sch.add_wire(xcvr_canl, tvs_l_anode)

            # TVS cathode goes to ground
            sch.add_wire(tvs_l_cathode, (tvs_l_cathode[0], xcvr_vss[1]))

            # CANL output is after TVS
            canl_output = (tvs_l_anode[0] + 5, tvs_l_anode[1])

        # Add termination
        r_idx = 0
        if termination is True:
            # Standard 120Ω termination
            r_term_ref = f"R{resistor_ref_start + r_idx}"
            r_term_x = x + termination_offset_x
            r_term_y = (xcvr_canh[1] + xcvr_canl[1]) / 2
            self.termination_resistor = sch.add_symbol(
                resistor_symbol, r_term_x, r_term_y, r_term_ref, "120R", rotation=90
            )
            self.termination_resistors.append(self.termination_resistor)
            self.components["R_TERM"] = self.termination_resistor
            r_idx += 1

            # Wire termination resistor between CANH and CANL
            r_pin1 = self.termination_resistor.pin_position("1")
            r_pin2 = self.termination_resistor.pin_position("2")

            # Connect to bus output positions
            sch.add_wire(canh_output, (r_pin1[0], canh_output[1]))
            sch.add_wire((r_pin1[0], canh_output[1]), r_pin1)
            sch.add_wire(canl_output, (r_pin2[0], canl_output[1]))
            sch.add_wire((r_pin2[0], canl_output[1]), r_pin2)

            # Add junctions at connection points
            sch.add_junction(r_pin1[0], canh_output[1])
            sch.add_junction(r_pin2[0], canl_output[1])

        elif termination == "split":
            # Split termination: 2×60Ω with capacitor to ground
            # Upper 60Ω resistor
            r1_ref = f"R{resistor_ref_start + r_idx}"
            r1_x = x + termination_offset_x
            r1_y = xcvr_canh[1] + 5
            self.split_r1 = sch.add_symbol(resistor_symbol, r1_x, r1_y, r1_ref, "60R", rotation=90)
            self.termination_resistors.append(self.split_r1)
            self.components["R_SPLIT1"] = self.split_r1
            r_idx += 1

            # Lower 60Ω resistor
            r2_ref = f"R{resistor_ref_start + r_idx}"
            r2_x = x + termination_offset_x
            r2_y = xcvr_canl[1] - 5
            self.split_r2 = sch.add_symbol(resistor_symbol, r2_x, r2_y, r2_ref, "60R", rotation=90)
            self.termination_resistors.append(self.split_r2)
            self.components["R_SPLIT2"] = self.split_r2
            r_idx += 1

            # Split capacitor (to ground)
            c_split_ref = f"C{cap_ref_start + 1}"
            c_split_x = r1_x + 10
            c_split_y = (r1_y + r2_y) / 2
            self.split_cap_component = sch.add_symbol(
                cap_symbol, c_split_x, c_split_y, c_split_ref, split_cap
            )
            self.components["C_SPLIT"] = self.split_cap_component

            # Get resistor pin positions
            r1_pin1 = self.split_r1.pin_position("1")
            r1_pin2 = self.split_r1.pin_position("2")
            r2_pin1 = self.split_r2.pin_position("1")
            r2_pin2 = self.split_r2.pin_position("2")

            # Wire CANH to R1 pin 1
            sch.add_wire(canh_output, (r1_pin1[0], canh_output[1]))
            sch.add_wire((r1_pin1[0], canh_output[1]), r1_pin1)
            sch.add_junction(r1_pin1[0], canh_output[1])

            # Wire CANL to R2 pin 2
            sch.add_wire(canl_output, (r2_pin2[0], canl_output[1]))
            sch.add_wire((r2_pin2[0], canl_output[1]), r2_pin2)
            sch.add_junction(r2_pin2[0], canl_output[1])

            # Wire R1 pin 2 to R2 pin 1 (midpoint)
            sch.add_wire(r1_pin2, r2_pin1)

            # Wire midpoint to split cap
            midpoint = (
                (r1_pin2[0] + r2_pin1[0]) / 2,
                (r1_pin2[1] + r2_pin1[1]) / 2,
            )
            c_split_pin1 = self.split_cap_component.pin_position("1")
            sch.add_wire(midpoint, c_split_pin1)
            sch.add_junction(midpoint[0], midpoint[1])

        # Build ports dictionary
        self.ports = {
            "VCC": xcvr_vdd,
            "GND": xcvr_vss,
            "TXD": xcvr_txd,
            "RXD": xcvr_rxd,
            "CANH": canh_output,
            "CANL": canl_output,
        }

        # Add STBY port if transceiver supports it
        if self.has_stby and "STBY" in pin_map:
            xcvr_stby = self.transceiver.pin_position(pin_map["STBY"])
            self.ports["STBY"] = xcvr_stby

        # Store positions for connect_to_rails
        self._vcc_pos = xcvr_vdd
        self._gnd_pos = xcvr_vss
        self._decoupling_cap = self.decoupling_cap

    def connect_to_rails(
        self,
        vcc_rail_y: float,
        gnd_rail_y: float,
        add_junctions: bool = True,
    ) -> None:
        """
        Connect transceiver and decoupling cap to power rails.

        Args:
            vcc_rail_y: Y coordinate of VCC rail
            gnd_rail_y: Y coordinate of GND rail
            add_junctions: Whether to add junction markers
        """
        sch = self.schematic

        # Wire decoupling cap to rails
        sch.wire_decoupling_cap(self._decoupling_cap, vcc_rail_y, gnd_rail_y)

        # Connect transceiver VCC to VCC rail
        vcc_pos = self._vcc_pos
        sch.add_wire(vcc_pos, (vcc_pos[0], vcc_rail_y))

        # Connect transceiver GND to GND rail
        gnd_pos = self._gnd_pos
        sch.add_wire(gnd_pos, (gnd_pos[0], gnd_rail_y))

        # Connect split cap ground if present
        if self.split_cap_component is not None:
            c_split_pin2 = self.split_cap_component.pin_position("2")
            sch.add_wire(c_split_pin2, (c_split_pin2[0], gnd_rail_y))
            if add_junctions:
                sch.add_junction(c_split_pin2[0], gnd_rail_y)

        if add_junctions:
            sch.add_junction(vcc_pos[0], vcc_rail_y)
            sch.add_junction(gnd_pos[0], gnd_rail_y)

    def get_voltage(self) -> float:
        """Get the operating voltage for this transceiver."""
        return self.TRANSCEIVER_CONFIGS[self.transceiver_type]["voltage"]


# Factory functions


def create_can_transceiver_mcp2551(
    sch: "Schematic",
    x: float,
    y: float,
    ref: str = "U1",
    termination: bool | str = False,
    esd_protection: bool = True,
) -> CANTransceiver:
    """
    Create an MCP2551 CAN transceiver (5V, classic, widely used).

    Args:
        sch: Schematic to add to
        x: X coordinate
        y: Y coordinate
        ref: Transceiver reference designator
        termination: False, True (120Ω), or "split"
        esd_protection: Add TVS protection on bus lines
    """
    return CANTransceiver(
        sch,
        x,
        y,
        transceiver="MCP2551",
        termination=termination,
        esd_protection=esd_protection,
        ref_prefix=ref,
    )


def create_can_transceiver_sn65hvd230(
    sch: "Schematic",
    x: float,
    y: float,
    ref: str = "U1",
    termination: bool | str = False,
    esd_protection: bool = True,
) -> CANTransceiver:
    """
    Create an SN65HVD230 CAN transceiver (3.3V, for STM32/ESP32).

    Args:
        sch: Schematic to add to
        x: X coordinate
        y: Y coordinate
        ref: Transceiver reference designator
        termination: False, True (120Ω), or "split"
        esd_protection: Add TVS protection on bus lines
    """
    return CANTransceiver(
        sch,
        x,
        y,
        transceiver="SN65HVD230",
        termination=termination,
        esd_protection=esd_protection,
        ref_prefix=ref,
    )


def create_can_transceiver_tja1050(
    sch: "Schematic",
    x: float,
    y: float,
    ref: str = "U1",
    termination: bool | str = False,
    esd_protection: bool = True,
) -> CANTransceiver:
    """
    Create a TJA1050 CAN transceiver (5V, automotive grade).

    Args:
        sch: Schematic to add to
        x: X coordinate
        y: Y coordinate
        ref: Transceiver reference designator
        termination: False, True (120Ω), or "split"
        esd_protection: Add TVS protection on bus lines
    """
    return CANTransceiver(
        sch,
        x,
        y,
        transceiver="TJA1050",
        termination=termination,
        esd_protection=esd_protection,
        ref_prefix=ref,
    )
