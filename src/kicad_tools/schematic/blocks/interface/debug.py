"""Debug header interface blocks: SWD, JTAG, Tag-Connect."""

from typing import TYPE_CHECKING

from ..base import CircuitBlock

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


# Factory functions


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
