"""Interface circuit blocks: debug headers, USB connectors."""

import contextlib
from typing import TYPE_CHECKING

from .base import CircuitBlock

if TYPE_CHECKING:
    from kicad_sch_helper import Schematic, SymbolInstance


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

                    resistor = sch.add_symbol(
                        resistor_symbol, r_x, r_y, r_ref, resistor_value
                    )
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
                f"Invalid interface '{self.interface}'. "
                f"Valid options: {list(valid_configs.keys())}"
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
            return (
                self.TAG_CONNECT_6PIN_PINOUT
                if self.pins == 6
                else self.TAG_CONNECT_10PIN_PINOUT
            )
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
        self.connector = sch.add_symbol(
            connector_symbol, x, y, j_ref, self.connector_type.upper()
        )
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

            self.esd_tvs = sch.add_symbol(
                esd_tvs_symbol, tvs_x, tvs_y, tvs_ref, esd_tvs_value
            )
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
