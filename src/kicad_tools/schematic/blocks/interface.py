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
        super().__init__(sch, x, y)
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

                    resistor = sch.add_symbol(resistor_symbol, r_x, r_y, r_ref, resistor_value)
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
                f"Invalid interface '{self.interface}'. Valid options: {list(valid_configs.keys())}"
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
            return self.TAG_CONNECT_6PIN_PINOUT if self.pins == 6 else self.TAG_CONNECT_10PIN_PINOUT
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
