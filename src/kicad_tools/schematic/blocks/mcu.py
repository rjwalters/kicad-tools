"""MCU-related circuit blocks."""

import contextlib
from typing import TYPE_CHECKING

from .base import CircuitBlock

if TYPE_CHECKING:
    from kicad_sch_helper import Schematic, SymbolInstance


class MCUBlock(CircuitBlock):
    """
    MCU with bypass capacitors on power pins.

    Places an MCU symbol with properly positioned bypass capacitors,
    pre-wired power connections, and exposed GPIO ports.

    Schematic:
        VDD ──┬──[C1]──┬──[C2]──┬──...──┬── MCU ── GPIO pins
              │        │        │       │
        GND ──┴────────┴────────┴───────┴─────────

    Ports:
        - VDD: Power input (after bypass caps)
        - GND: Ground
        - All MCU GPIO/signal pins by name (e.g., PA0, PB1, NRST, etc.)

    Example:
        >>> from kicad_tools.schematic.blocks import MCUBlock
        >>> # Create MCU block with bypass caps
        >>> mcu = MCUBlock(
        ...     sch,
        ...     mcu_symbol="MCU_ST_STM32F1:STM32F103C8Tx",
        ...     x=150, y=100,
        ...     bypass_caps=["100nF", "100nF", "100nF", "4.7uF"],
        ...     ref="U1",
        ... )
        >>> # Access ports
        >>> mcu.port("VDD")      # Power input
        >>> mcu.port("GND")      # Ground
        >>> mcu.port("PA0")      # GPIO port
        >>> mcu.port("NRST")     # Reset pin
        >>> # Wire to other blocks
        >>> sch.add_wire(ldo.port("VOUT"), mcu.port("VDD"))
    """

    # Pin name patterns that indicate VDD (power input)
    VDD_PATTERNS = (
        "VDD",
        "VDDA",
        "VDDIO",
        "VCC",
        "VCCA",
        "AVDD",
        "DVDD",
        "VBAT",
    )

    # Pin name patterns that indicate GND (ground)
    GND_PATTERNS = ("GND", "GNDA", "VSS", "VSSA", "AGND", "DGND", "AVSS", "DVSS")

    def __init__(
        self,
        sch: "Schematic",
        x: float,
        y: float,
        mcu_symbol: str,
        ref: str = "U1",
        value: str = "",
        bypass_caps: list[str] | None = None,
        cap_ref_start: int = 1,
        cap_ref_prefix: str = "C",
        cap_spacing: float = 10,
        cap_offset_x: float = -30,
        cap_offset_y: float = 20,
        cap_symbol: str = "Device:C",
        unit: int = 1,
    ):
        """
        Create an MCU block with bypass capacitors.

        Args:
            sch: Schematic to add to
            x: X coordinate of MCU center
            y: Y coordinate of MCU center
            mcu_symbol: KiCad symbol for MCU (e.g., "MCU_ST_STM32F1:STM32F103C8Tx")
            ref: MCU reference designator
            value: MCU value label (defaults to symbol name if empty)
            bypass_caps: List of bypass capacitor values (e.g., ["100nF", "100nF", "4.7uF"]).
                If None, uses default ["100nF", "100nF", "100nF", "100nF"]
            cap_ref_start: Starting reference number for capacitors
            cap_ref_prefix: Reference designator prefix for capacitors
            cap_spacing: Horizontal spacing between capacitors (mm)
            cap_offset_x: X offset of first capacitor relative to MCU
            cap_offset_y: Y offset of capacitors relative to MCU
            cap_symbol: KiCad symbol for bypass capacitors
            unit: Symbol unit number (for multi-unit symbols)
        """
        super().__init__(sch, x, y)

        # Default bypass caps if not specified
        if bypass_caps is None:
            bypass_caps = ["100nF", "100nF", "100nF", "100nF"]

        # Default value to symbol name if not provided
        if not value:
            value = mcu_symbol.split(":")[-1] if ":" in mcu_symbol else mcu_symbol

        # Place MCU
        self.mcu = sch.add_symbol(mcu_symbol, x, y, ref, value, unit=unit)
        self.components = {"MCU": self.mcu}

        # Identify power pins from MCU symbol
        self.vdd_pins: list[str] = []
        self.gnd_pins: list[str] = []
        self._identify_power_pins()

        # Place bypass capacitors
        self.bypass_caps: list = []
        cap_x = x + cap_offset_x
        cap_y = y + cap_offset_y

        for i, cap_value in enumerate(bypass_caps):
            cap_ref = f"{cap_ref_prefix}{cap_ref_start + i}"
            cap = sch.add_symbol(cap_symbol, cap_x + i * cap_spacing, cap_y, cap_ref, cap_value)
            self.bypass_caps.append(cap)
            self.components[f"C{i + 1}"] = cap

        # Wire bypass caps internally (all caps share VDD and GND rails)
        self._wire_bypass_caps()

        # Build ports dict with all MCU pins
        self.ports = {}
        self._build_ports()

    def _identify_power_pins(self):
        """Identify VDD and GND pins from the MCU symbol."""
        # Access pin information from the symbol definition
        if hasattr(self.mcu, "symbol_def") and hasattr(self.mcu.symbol_def, "pins"):
            for pin in self.mcu.symbol_def.pins:
                pin_name_upper = pin.name.upper()

                # Check for VDD patterns
                for pattern in self.VDD_PATTERNS:
                    if pin_name_upper.startswith(pattern) or pin_name_upper == pattern:
                        self.vdd_pins.append(pin.name)
                        break

                # Check for GND patterns
                for pattern in self.GND_PATTERNS:
                    if pin_name_upper.startswith(pattern) or pin_name_upper == pattern:
                        self.gnd_pins.append(pin.name)
                        break

        # Also check by pin type if available
        if hasattr(self.mcu, "symbol_def") and hasattr(self.mcu.symbol_def, "pins"):
            for pin in self.mcu.symbol_def.pins:
                if pin.pin_type == "power_in":
                    pin_name_upper = pin.name.upper()
                    # Additional check by type for pins we might have missed
                    if any(p in pin_name_upper for p in ("VDD", "VCC", "V+")):
                        if pin.name not in self.vdd_pins:
                            self.vdd_pins.append(pin.name)
                    elif any(p in pin_name_upper for p in ("GND", "VSS", "V-")):
                        if pin.name not in self.gnd_pins:
                            self.gnd_pins.append(pin.name)

    def _wire_bypass_caps(self):
        """Wire bypass capacitors to form a decoupling bank."""
        if not self.bypass_caps:
            return

        sch = self.schematic

        # Get first cap's pin positions for reference
        first_cap = self.bypass_caps[0]
        vdd_y = first_cap.pin_position("1")[1]
        gnd_y = first_cap.pin_position("2")[1]

        # Wire each cap to the VDD/GND bus
        for i, cap in enumerate(self.bypass_caps):
            cap_vdd = cap.pin_position("1")
            cap_gnd = cap.pin_position("2")

            # Connect to horizontal bus if not the first cap
            if i > 0:
                prev_cap = self.bypass_caps[i - 1]
                prev_vdd = prev_cap.pin_position("1")
                prev_gnd = prev_cap.pin_position("2")

                # Horizontal wire on VDD bus
                sch.add_wire(prev_vdd, (cap_vdd[0], vdd_y))
                sch.add_wire((cap_vdd[0], vdd_y), cap_vdd)

                # Horizontal wire on GND bus
                sch.add_wire(prev_gnd, (cap_gnd[0], gnd_y))
                sch.add_wire((cap_gnd[0], gnd_y), cap_gnd)

    def _build_ports(self):
        """Build ports dict exposing all MCU pins."""
        # Add VDD port (use first VDD pin position, or first cap's VDD)
        if self.vdd_pins:
            self.ports["VDD"] = self.mcu.pin_position(self.vdd_pins[0])
        elif self.bypass_caps:
            self.ports["VDD"] = self.bypass_caps[0].pin_position("1")

        # Add GND port (use first GND pin position, or first cap's GND)
        if self.gnd_pins:
            self.ports["GND"] = self.mcu.pin_position(self.gnd_pins[0])
        elif self.bypass_caps:
            self.ports["GND"] = self.bypass_caps[0].pin_position("2")

        # Expose all MCU pins as ports
        if hasattr(self.mcu, "symbol_def") and hasattr(self.mcu.symbol_def, "pins"):
            for pin in self.mcu.symbol_def.pins:
                if pin.name and pin.name not in self.ports:
                    with contextlib.suppress(Exception):
                        self.ports[pin.name] = self.mcu.pin_position(pin.name)

    def connect_to_rails(
        self,
        vdd_rail_y: float,
        gnd_rail_y: float,
        wire_all_power_pins: bool = True,
    ):
        """
        Connect MCU and bypass caps to power rails.

        Args:
            vdd_rail_y: Y coordinate of VDD power rail
            gnd_rail_y: Y coordinate of GND power rail
            wire_all_power_pins: If True, wire all VDD/GND pins to rails.
                If False, only wire the first VDD/GND pin.
        """
        sch = self.schematic

        # Wire bypass caps to rails
        for cap in self.bypass_caps:
            sch.wire_decoupling_cap(cap, vdd_rail_y, gnd_rail_y)

        # Wire MCU power pins to rails
        vdd_pins_to_wire = self.vdd_pins if wire_all_power_pins else self.vdd_pins[:1]
        gnd_pins_to_wire = self.gnd_pins if wire_all_power_pins else self.gnd_pins[:1]

        for pin_name in vdd_pins_to_wire:
            try:
                pin_pos = self.mcu.pin_position(pin_name)
                sch.add_wire(pin_pos, (pin_pos[0], vdd_rail_y))
                sch.add_junction(pin_pos[0], vdd_rail_y)
            except Exception:
                pass

        for pin_name in gnd_pins_to_wire:
            try:
                pin_pos = self.mcu.pin_position(pin_name)
                sch.add_wire(pin_pos, (pin_pos[0], gnd_rail_y))
                sch.add_junction(pin_pos[0], gnd_rail_y)
            except Exception:
                pass

    def get_gpio_pins(self) -> list[str]:
        """Get list of GPIO pin names (non-power pins)."""
        gpio_pins = []
        if hasattr(self.mcu, "symbol_def") and hasattr(self.mcu.symbol_def, "pins"):
            for pin in self.mcu.symbol_def.pins:
                if pin.name:
                    pin_upper = pin.name.upper()
                    is_power = any(
                        pin_upper.startswith(p) for p in self.VDD_PATTERNS + self.GND_PATTERNS
                    )
                    if not is_power:
                        gpio_pins.append(pin.name)
        return gpio_pins

    def get_power_pins(self) -> dict[str, list[str]]:
        """Get dict of power pin names grouped by type."""
        return {
            "VDD": self.vdd_pins.copy(),
            "GND": self.gnd_pins.copy(),
        }


class ResetButton(CircuitBlock):
    """
    Reset button with pull-up resistor and debounce capacitor.

    Places a tactile switch with debounce circuit for MCU reset. The circuit
    includes a pull-up resistor and debounce capacitor. Optional ESD protection
    can be added with a TVS diode.

    Schematic (active-low, default):
        VCC ────┬────────────
                │
               [R]  (pull-up, typically 10k)
                │
        NRST ───┼────┬───────
                │   [C]  (debounce, 100nF typical)
               [SW]  │
                │    │
        GND ────┴────┴───────

    Schematic (active-high):
        GND ────┬────────────
                │
               [R]  (pull-down, typically 10k)
                │
        RST ────┼────┬───────
                │   [C]  (debounce, 100nF typical)
               [SW]  │
                │    │
        VCC ────┴────┴───────

    Ports:
        - VCC: Power input (for pull-up/pull-down)
        - NRST (or RST): Reset output (active-low or active-high)
        - GND: Ground

    Example:
        from kicad_tools.schematic.blocks import ResetButton

        # Basic reset button (active-low)
        reset = ResetButton(
            sch,
            x=100, y=50,
            pullup_value="10k",
            debounce_cap="100nF",
            ref_prefix="SW",
        )

        # With ESD protection
        reset = ResetButton(
            sch,
            x=100, y=50,
            pullup_value="10k",
            debounce_cap="100nF",
            esd_protection=True,
            ref_prefix="SW",
        )

        # Active-high reset (rare but exists)
        reset = ResetButton(
            sch,
            x=100, y=50,
            active_low=False,
            ref_prefix="SW",
        )

        # Access ports
        reset.port("VCC")   # Power for pull-up
        reset.port("NRST")  # Reset output (active low)
        reset.port("GND")   # Ground
    """

    def __init__(
        self,
        sch: "Schematic",
        x: float,
        y: float,
        pullup_value: str = "10k",
        debounce_cap: str = "100nF",
        active_low: bool = True,
        esd_protection: bool = False,
        ref_prefix: str = "SW",
        resistor_ref_start: int = 1,
        cap_ref_start: int = 1,
        tvs_ref_start: int = 1,
        switch_symbol: str = "Switch:SW_Push",
        resistor_symbol: str = "Device:R",
        cap_symbol: str = "Device:C",
        tvs_symbol: str = "Device:D_TVS",
        tvs_value: str = "PESD5V0S1BL",
    ):
        """
        Create a reset button with pull-up and debounce capacitor.

        Args:
            sch: Schematic to add to
            x: X coordinate of switch center
            y: Y coordinate of switch center
            pullup_value: Pull-up (or pull-down) resistor value (e.g., "10k")
            debounce_cap: Debounce capacitor value (e.g., "100nF")
            active_low: If True (default), reset is active-low (NRST).
                If False, reset is active-high (RST).
            esd_protection: If True, add TVS diode for ESD protection
            ref_prefix: Reference designator prefix for switch (e.g., "SW" or "SW1")
            resistor_ref_start: Starting reference number for resistor
            cap_ref_start: Starting reference number for capacitor
            tvs_ref_start: Starting reference number for TVS diode
            switch_symbol: KiCad symbol for tactile switch
            resistor_symbol: KiCad symbol for resistor
            cap_symbol: KiCad symbol for capacitor
            tvs_symbol: KiCad symbol for TVS diode
            tvs_value: Part value for TVS diode (e.g., "PESD5V0S1BL")
        """
        super().__init__(sch, x, y)
        self.active_low = active_low
        self.esd_protection = esd_protection

        # Parse reference prefix
        if ref_prefix[-1].isdigit():
            sw_ref = ref_prefix
        else:
            sw_ref = f"{ref_prefix}1"

        r_ref = f"R{resistor_ref_start}"
        c_ref = f"C{cap_ref_start}"

        # Component spacing
        resistor_offset_y = -15  # Resistor above switch
        cap_offset_x = 15  # Cap to the right of switch
        tvs_offset_x = 25  # TVS further right if present

        # Place switch
        self.switch = sch.add_symbol(switch_symbol, x, y, sw_ref, "RESET")
        self.components = {"SW": self.switch}

        # Get switch pin positions
        # Standard tactile switch has pins 1 and 2
        sw_pin1 = self.switch.pin_position("1")
        sw_pin2 = self.switch.pin_position("2")

        # Place pull-up/pull-down resistor above switch
        r_y = y + resistor_offset_y
        self.resistor = sch.add_symbol(resistor_symbol, x, r_y, r_ref, pullup_value)
        self.components["R"] = self.resistor

        # Get resistor pin positions
        r_pin1 = self.resistor.pin_position("1")  # Top
        r_pin2 = self.resistor.pin_position("2")  # Bottom

        # Place debounce capacitor to the right
        c_x = x + cap_offset_x
        self.cap = sch.add_symbol(cap_symbol, c_x, y, c_ref, debounce_cap)
        self.components["C"] = self.cap

        # Get cap pin positions
        c_pin1 = self.cap.pin_position("1")  # Top
        c_pin2 = self.cap.pin_position("2")  # Bottom

        # Wire resistor bottom to switch top and cap top (reset node)

        # Wire resistor pin 2 to switch pin 1 (vertical)
        sch.add_wire(r_pin2, sw_pin1)

        # Wire reset node to cap top
        # Create junction at the reset node
        sch.add_wire(sw_pin1, (c_pin1[0], sw_pin1[1]))  # Horizontal to cap x
        sch.add_wire((c_pin1[0], sw_pin1[1]), c_pin1)  # Vertical to cap top
        sch.add_junction(sw_pin1[0], sw_pin1[1])

        # Wire switch bottom to cap bottom (ground node for active-low)
        # For active-high, this would be VCC
        sch.add_wire(sw_pin2, (c_pin2[0], sw_pin2[1]))  # Horizontal
        sch.add_wire((c_pin2[0], sw_pin2[1]), c_pin2)  # Vertical

        # Add TVS diode if requested
        self.tvs: SymbolInstance | None = None
        if esd_protection:
            tvs_ref = f"D{tvs_ref_start}"
            tvs_x = x + tvs_offset_x
            self.tvs = sch.add_symbol(tvs_symbol, tvs_x, y, tvs_ref, tvs_value)
            self.components["TVS"] = self.tvs

            # Get TVS pin positions
            tvs_anode = self.tvs.pin_position("A")
            tvs_cathode = self.tvs.pin_position("K")

            # Wire TVS anode to reset node
            sch.add_wire((c_pin1[0], sw_pin1[1]), (tvs_anode[0], sw_pin1[1]))
            sch.add_wire((tvs_anode[0], sw_pin1[1]), tvs_anode)

            # TVS cathode goes to ground (will be wired in connect_to_rails)
            # Store for later
            self._tvs_cathode = tvs_cathode

        # Define ports
        # Reset output is at the junction between resistor, switch, and cap
        reset_port_x = c_pin1[0]  # At cap top x position
        reset_port_y = sw_pin1[1]  # At switch pin 1 y position

        if active_low:
            # Active-low: VCC at top, GND at bottom
            self.ports = {
                "VCC": r_pin1,  # Top of resistor (pull-up to VCC)
                "NRST": (reset_port_x, reset_port_y),  # Reset output
                "GND": sw_pin2,  # Bottom of switch (to GND)
            }
        else:
            # Active-high: GND at top, VCC at bottom
            self.ports = {
                "GND": r_pin1,  # Top of resistor (pull-down to GND)
                "RST": (reset_port_x, reset_port_y),  # Reset output
                "VCC": sw_pin2,  # Bottom of switch (to VCC)
            }

        # Store internal positions for connect_to_rails
        self._resistor_top = r_pin1
        self._switch_bottom = sw_pin2
        self._cap_bottom = c_pin2

    def connect_to_rails(
        self,
        vcc_rail_y: float,
        gnd_rail_y: float,
        add_junctions: bool = True,
    ) -> None:
        """
        Connect reset button to power rails.

        Args:
            vcc_rail_y: Y coordinate of VCC rail
            gnd_rail_y: Y coordinate of GND rail
            add_junctions: Whether to add junction markers
        """
        sch = self.schematic

        if self.active_low:
            # Active-low: Connect resistor top to VCC, switch bottom to GND
            vcc_pos = self._resistor_top
            gnd_pos = self._switch_bottom

            # Connect pull-up to VCC rail
            sch.add_wire(vcc_pos, (vcc_pos[0], vcc_rail_y))

            # Connect switch and cap bottom to GND rail
            sch.add_wire(gnd_pos, (gnd_pos[0], gnd_rail_y))

            # Connect cap bottom to GND rail
            sch.add_wire(self._cap_bottom, (self._cap_bottom[0], gnd_rail_y))

            if add_junctions:
                sch.add_junction(vcc_pos[0], vcc_rail_y)
                sch.add_junction(gnd_pos[0], gnd_rail_y)
                sch.add_junction(self._cap_bottom[0], gnd_rail_y)
        else:
            # Active-high: Connect resistor top to GND, switch bottom to VCC
            gnd_pos = self._resistor_top
            vcc_pos = self._switch_bottom

            # Connect pull-down to GND rail
            sch.add_wire(gnd_pos, (gnd_pos[0], gnd_rail_y))

            # Connect switch and cap bottom to VCC rail
            sch.add_wire(vcc_pos, (vcc_pos[0], vcc_rail_y))

            # Connect cap bottom to VCC rail
            sch.add_wire(self._cap_bottom, (self._cap_bottom[0], vcc_rail_y))

            if add_junctions:
                sch.add_junction(gnd_pos[0], gnd_rail_y)
                sch.add_junction(vcc_pos[0], vcc_rail_y)
                sch.add_junction(self._cap_bottom[0], vcc_rail_y)

        # Connect TVS cathode to GND if present
        if self.esd_protection and hasattr(self, "_tvs_cathode"):
            sch.add_wire(self._tvs_cathode, (self._tvs_cathode[0], gnd_rail_y))
            if add_junctions:
                sch.add_junction(self._tvs_cathode[0], gnd_rail_y)


def create_reset_button(
    sch: "Schematic",
    x: float,
    y: float,
    ref: str = "SW1",
    pullup_value: str = "10k",
    debounce_cap: str = "100nF",
    with_esd: bool = False,
) -> ResetButton:
    """
    Create a reset button with standard values.

    Args:
        sch: Schematic to add to
        x: X coordinate
        y: Y coordinate
        ref: Switch reference designator
        pullup_value: Pull-up resistor value (default "10k")
        debounce_cap: Debounce capacitor value (default "100nF")
        with_esd: Add ESD protection TVS diode
    """
    return ResetButton(
        sch,
        x,
        y,
        pullup_value=pullup_value,
        debounce_cap=debounce_cap,
        esd_protection=with_esd,
        ref_prefix=ref,
    )


class BootModeSelector(CircuitBlock):
    """
    Boot mode selector circuit for MCU boot configuration.

    Supports common MCU boot mode configurations including STM32, ESP32,
    and generic single-pin boot selectors. Each configuration includes
    appropriate pull-up/pull-down resistors and optional boot button.

    Schematic (STM32 BOOT0 with button):
        VCC ─────────────────┬────
                             │
                            [SW]  (optional BOOT button)
                             │
        BOOT0 ───────────────┼────
                             │
                            [R]   (10k pull-down for normal boot)
                             │
        GND ─────────────────┴────

    Schematic (ESP32 GPIO0 with button):
        VCC ─────────────────┬────
                             │
                            [R]   (10k pull-up for normal boot)
                             │
        GPIO0 ───────────────┼────
                             │
                            [SW]  (optional BOOT button)
                             │
        GND ─────────────────┴────

    Boot Mode Configurations:
        STM32:
            BOOT0=0 → Boot from Main Flash (default)
            BOOT0=1 → Boot from System Memory (bootloader)

        ESP32:
            GPIO0=1 → Normal boot (default)
            GPIO0=0 → Download mode

    Ports:
        - VCC: Power input
        - GND: Ground
        - BOOT0: Boot pin (STM32 mode)
        - BOOT1: Second boot pin (STM32 mode, if enabled)
        - GPIO0: Boot pin (ESP32 mode)
        - GPIO2: Second boot pin (ESP32 mode, if enabled)
        - BOOT: Boot pin (generic mode)

    Example:
        from kicad_tools.schematic.blocks import BootModeSelector

        # STM32 boot configuration (BOOT0 low = boot from flash)
        boot = BootModeSelector(
            sch,
            x=100, y=50,
            mode="stm32",
            default_state="flash",  # BOOT0 = low
            include_button=True,    # Add button to force bootloader
            ref_prefix="R",
        )

        # ESP32 boot configuration
        boot = BootModeSelector(
            sch,
            x=100, y=50,
            mode="esp32",
            default_state="normal",  # GPIO0 = high
            include_button=True,     # Add BOOT button
            ref_prefix="R",
        )

        # Access ports
        boot.port("VCC")    # Power
        boot.port("BOOT0")  # Boot pin (STM32)
        boot.port("GND")    # Ground
    """

    # Mode configurations
    MODE_CONFIGS = {
        "stm32": {
            "pins": ["BOOT0"],
            "default_high": False,  # BOOT0=0 for flash boot
            "boot_pin_name": "BOOT0",
        },
        "stm32_dual": {
            "pins": ["BOOT0", "BOOT1"],
            "default_high": False,  # BOOT0=0, BOOT1=0 for flash boot
            "boot_pin_name": "BOOT0",
        },
        "esp32": {
            "pins": ["GPIO0"],
            "default_high": True,  # GPIO0=1 for normal boot
            "boot_pin_name": "GPIO0",
        },
        "esp32_dual": {
            "pins": ["GPIO0", "GPIO2"],
            "default_high": True,  # GPIO0=1, GPIO2=0 for normal boot
            "boot_pin_name": "GPIO0",
        },
        "generic": {
            "pins": ["BOOT"],
            "default_high": False,  # Configurable
            "boot_pin_name": "BOOT",
        },
    }

    # Default state mappings for user-friendly names
    STATE_MAPPINGS = {
        "stm32": {
            "flash": False,  # BOOT0 low
            "bootloader": True,  # BOOT0 high
            "system": True,  # Same as bootloader
            "low": False,
            "high": True,
        },
        "stm32_dual": {
            "flash": False,
            "bootloader": True,
            "system": True,
            "low": False,
            "high": True,
        },
        "esp32": {
            "normal": True,  # GPIO0 high
            "download": False,  # GPIO0 low
            "flash": False,  # Same as download
            "low": False,
            "high": True,
        },
        "esp32_dual": {
            "normal": True,
            "download": False,
            "flash": False,
            "low": False,
            "high": True,
        },
        "generic": {
            "low": False,
            "high": True,
        },
    }

    def __init__(
        self,
        sch: "Schematic",
        x: float,
        y: float,
        mode: str = "stm32",
        default_state: str = "flash",
        include_button: bool = True,
        resistor_value: str = "10k",
        ref_prefix: str = "R",
        button_ref_prefix: str = "SW",
        resistor_symbol: str = "Device:R",
        button_symbol: str = "Switch:SW_Push",
    ):
        """
        Create a boot mode selector circuit.

        Args:
            sch: Schematic to add to
            x: X coordinate of circuit center
            y: Y coordinate of circuit center
            mode: Boot mode configuration - "stm32", "stm32_dual", "esp32",
                "esp32_dual", or "generic"
            default_state: Default boot state (mode-specific):
                - STM32: "flash" (low) or "bootloader"/"system" (high)
                - ESP32: "normal" (high) or "download"/"flash" (low)
                - Generic: "low" or "high"
            include_button: If True, add a push button for boot mode selection
            resistor_value: Pull-up/pull-down resistor value (e.g., "10k", "4.7k")
            ref_prefix: Reference designator prefix for resistor
            button_ref_prefix: Reference designator prefix for button
            resistor_symbol: KiCad symbol for resistor
            button_symbol: KiCad symbol for push button
        """
        super().__init__(sch, x, y)
        self.mode = mode.lower()
        self.include_button = include_button
        self.resistor_value = resistor_value

        # Validate mode
        if self.mode not in self.MODE_CONFIGS:
            raise ValueError(
                f"Invalid mode '{mode}'. Valid options: {list(self.MODE_CONFIGS.keys())}"
            )

        config = self.MODE_CONFIGS[self.mode]

        # Parse default state
        state_map = self.STATE_MAPPINGS.get(self.mode, {})
        if default_state.lower() in state_map:
            self.default_high = state_map[default_state.lower()]
        else:
            # Interpret as boolean-like
            self.default_high = default_state.lower() in ("high", "true", "1")

        # Parse reference prefixes
        r_ref = ref_prefix if ref_prefix[-1].isdigit() else f"{ref_prefix}1"
        sw_ref = button_ref_prefix if button_ref_prefix[-1].isdigit() else f"{button_ref_prefix}1"

        # Component spacing
        component_spacing = 10  # mm between components

        # Initialize components dict
        self.components = {}
        self.resistors = {}
        self.buttons = {}

        # Calculate positions based on whether we're pulling up or down
        if self.default_high:
            # Pull-up configuration: resistor on top, button on bottom
            resistor_y = y - component_spacing
            button_y = y + component_spacing
        else:
            # Pull-down configuration: button on top, resistor on bottom
            button_y = y - component_spacing
            resistor_y = y + component_spacing

        # Place resistor (always present)
        self.resistor = sch.add_symbol(resistor_symbol, x, resistor_y, r_ref, resistor_value)
        self.components["R"] = self.resistor
        self.resistors["R1"] = self.resistor

        # Get resistor pin positions
        r_pin1 = self.resistor.pin_position("1")
        r_pin2 = self.resistor.pin_position("2")

        # Determine which resistor pin connects to boot pin vs rail
        if self.default_high:
            # Pull-up: pin1 connects to VCC, pin2 connects to boot pin
            r_rail_pin = r_pin1
            r_boot_pin = r_pin2
        else:
            # Pull-down: pin1 connects to boot pin, pin2 connects to GND
            r_boot_pin = r_pin1
            r_rail_pin = r_pin2

        # Place button if requested
        if include_button:
            self.button = sch.add_symbol(button_symbol, x, button_y, sw_ref, "BOOT")
            self.components["SW"] = self.button
            self.buttons["SW1"] = self.button

            # Get button pin positions
            sw_pin1 = self.button.pin_position("1")
            sw_pin2 = self.button.pin_position("2")

            # Wire button to boot pin junction
            # The boot pin junction is at the resistor's boot pin level
            boot_junction_y = r_boot_pin[1]
            boot_junction_x = x

            # Connect button pin closest to boot junction
            if self.default_high:
                # Button connects boot pin to GND when pressed
                # Button is below the boot pin
                sch.add_wire(sw_pin1, (boot_junction_x, boot_junction_y))
            else:
                # Button connects boot pin to VCC when pressed
                # Button is above the boot pin
                sch.add_wire(sw_pin2, (boot_junction_x, boot_junction_y))

            # Store button rail pin for connect_to_rails
            if self.default_high:
                self._button_rail_pin = sw_pin2  # Bottom connects to GND
            else:
                self._button_rail_pin = sw_pin1  # Top connects to VCC
        else:
            self._button_rail_pin = None

        # Build ports dictionary
        self.ports = {}

        # Boot pin port at the junction point
        boot_pin_name = config["boot_pin_name"]
        self.ports[boot_pin_name] = (x, r_boot_pin[1])

        # VCC and GND ports at the resistor/button ends
        if self.default_high:
            self.ports["VCC"] = r_rail_pin  # Resistor connects to VCC
            if include_button:
                self.ports["GND"] = self._button_rail_pin  # Button connects to GND
            else:
                # GND port at a reasonable offset below the circuit
                self.ports["GND"] = (x, y + component_spacing * 2)
        else:
            self.ports["GND"] = r_rail_pin  # Resistor connects to GND
            if include_button:
                self.ports["VCC"] = self._button_rail_pin  # Button connects to VCC
            else:
                # VCC port at a reasonable offset above the circuit
                self.ports["VCC"] = (x, y - component_spacing * 2)

        # Store internal state for connect_to_rails
        self._r_rail_pin = r_rail_pin
        self._boot_junction = (x, r_boot_pin[1])

    def connect_to_rails(
        self,
        vcc_rail_y: float,
        gnd_rail_y: float,
        add_junctions: bool = True,
    ) -> None:
        """
        Connect boot selector to power rails.

        Args:
            vcc_rail_y: Y coordinate of VCC rail
            gnd_rail_y: Y coordinate of GND rail
            add_junctions: Whether to add junction markers
        """
        sch = self.schematic

        if self.default_high:
            # Pull-up configuration: resistor to VCC, button to GND
            # Connect resistor to VCC
            sch.add_wire(self._r_rail_pin, (self._r_rail_pin[0], vcc_rail_y))
            if add_junctions:
                sch.add_junction(self._r_rail_pin[0], vcc_rail_y)

            # Connect button to GND if present
            if self.include_button and self._button_rail_pin:
                sch.add_wire(self._button_rail_pin, (self._button_rail_pin[0], gnd_rail_y))
                if add_junctions:
                    sch.add_junction(self._button_rail_pin[0], gnd_rail_y)
        else:
            # Pull-down configuration: resistor to GND, button to VCC
            # Connect resistor to GND
            sch.add_wire(self._r_rail_pin, (self._r_rail_pin[0], gnd_rail_y))
            if add_junctions:
                sch.add_junction(self._r_rail_pin[0], gnd_rail_y)

            # Connect button to VCC if present
            if self.include_button and self._button_rail_pin:
                sch.add_wire(self._button_rail_pin, (self._button_rail_pin[0], vcc_rail_y))
                if add_junctions:
                    sch.add_junction(self._button_rail_pin[0], vcc_rail_y)

    def get_boot_pin_name(self) -> str:
        """Get the primary boot pin name for this mode."""
        return self.MODE_CONFIGS[self.mode]["boot_pin_name"]

    def is_default_high(self) -> bool:
        """Check if the default boot state is high."""
        return self.default_high


def create_stm32_boot(
    sch: "Schematic",
    x: float,
    y: float,
    ref: str = "R1",
    include_button: bool = True,
    resistor_value: str = "10k",
) -> BootModeSelector:
    """
    Create STM32 boot mode selector (BOOT0 pin configuration).

    Default configuration boots from flash (BOOT0 = low).
    Optional button pulls BOOT0 high to enter bootloader mode.

    Args:
        sch: Schematic to add to
        x: X coordinate
        y: Y coordinate
        ref: Resistor reference designator
        include_button: Add BOOT button for bootloader entry
        resistor_value: Pull-down resistor value
    """
    return BootModeSelector(
        sch,
        x,
        y,
        mode="stm32",
        default_state="flash",
        include_button=include_button,
        resistor_value=resistor_value,
        ref_prefix=ref,
    )


def create_esp32_boot(
    sch: "Schematic",
    x: float,
    y: float,
    ref: str = "R1",
    include_button: bool = True,
    resistor_value: str = "10k",
) -> BootModeSelector:
    """
    Create ESP32 boot mode selector (GPIO0 pin configuration).

    Default configuration is normal boot (GPIO0 = high).
    Optional button pulls GPIO0 low to enter download mode.

    Args:
        sch: Schematic to add to
        x: X coordinate
        y: Y coordinate
        ref: Resistor reference designator
        include_button: Add BOOT button for download mode entry
        resistor_value: Pull-up resistor value
    """
    return BootModeSelector(
        sch,
        x,
        y,
        mode="esp32",
        default_state="normal",
        include_button=include_button,
        resistor_value=resistor_value,
        ref_prefix=ref,
    )


def create_generic_boot(
    sch: "Schematic",
    x: float,
    y: float,
    ref: str = "R1",
    default_high: bool = False,
    include_button: bool = True,
    resistor_value: str = "10k",
) -> BootModeSelector:
    """
    Create a generic boot mode selector.

    Args:
        sch: Schematic to add to
        x: X coordinate
        y: Y coordinate
        ref: Resistor reference designator
        default_high: If True, use pull-up (default high). If False, use pull-down.
        include_button: Add BOOT button
        resistor_value: Pull-up or pull-down resistor value
    """
    return BootModeSelector(
        sch,
        x,
        y,
        mode="generic",
        default_state="high" if default_high else "low",
        include_button=include_button,
        resistor_value=resistor_value,
        ref_prefix=ref,
    )
