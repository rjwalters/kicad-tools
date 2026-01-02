"""USB connector interface blocks with ESD protection."""

import contextlib
from typing import TYPE_CHECKING

from ..base import CircuitBlock

if TYPE_CHECKING:
    from kicad_sch_helper import Schematic, SymbolInstance


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
        super().__init__(sch, x, y)
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


# Factory functions


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
