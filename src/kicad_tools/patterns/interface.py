"""
Interface PCB patterns for communication protocols.

This module provides both PCB placement patterns (USBPattern, I2CPattern)
and intent-based constraint patterns (SPIPattern, UARTPattern, EthernetPattern)
for common communication interfaces.

Placement patterns (inherit from PCBPattern):
    - USBPattern: USB interface with ESD protection placement
    - I2CPattern: I2C bus with pull-up resistor placement

Constraint patterns (inherit from IntentPattern):
    - SPIPattern: SPI bus with routing constraints
    - UARTPattern: UART interface with trace length constraints
    - EthernetPattern: Ethernet with differential impedance constraints

Example (placement pattern)::

    from kicad_tools.patterns.interface import USBPattern

    pattern = USBPattern(connector="USB-C", esd_protection=True)
    placements = pattern.get_placements(connector_at=(5, 30))

Example (constraint pattern)::

    from kicad_tools.patterns.interface import SPIPattern

    spi = SPIPattern(speed="high", cs_count=3)
    constraints = spi.derive_constraints(["SPI_CLK", "SPI_MOSI", "SPI_MISO"])
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from kicad_tools.intent import Constraint, InterfaceCategory

from .base import PCBPattern
from .constraints import (
    ConstraintPlacementRule,
    ConstraintPriority,
    ConstraintRoutingRule,
    IntentPattern,
)
from .schema import (
    PatternSpec,
    PatternViolation,
    Placement,
    PlacementPriority,
    PlacementRule,
    RoutingConstraint,
)

if TYPE_CHECKING:
    pass


# =============================================================================
# Placement-based patterns (inherit from PCBPattern)
# =============================================================================


class USBPattern(PCBPattern):
    """PCB placement pattern for USB interfaces.

    USB requires controlled impedance differential pairs and proper
    ESD protection placement. This pattern ensures optimal layout
    for USB 2.0 High-Speed signals.

    Key placement rules:
    - ESD protection: Within 5mm of connector
    - Termination resistors: Close to MCU pins
    - Decoupling: Adjacent to VBUS
    - Differential pair routing: 90 ohm impedance

    Attributes:
        connector: Connector type (USB-C, Micro-B, etc.)
        esd_protection: Whether ESD protection is included
        termination_resistors: Whether termination resistors are included
    """

    def __init__(
        self,
        connector: str = "USB",
        esd_protection: bool = True,
        termination_resistors: bool = True,
        vbus_cap: str = "4.7uF",
    ) -> None:
        """Initialize USB pattern.

        Args:
            connector: Connector type description
            esd_protection: Include ESD protection IC
            termination_resistors: Include series termination resistors
            vbus_cap: VBUS decoupling capacitor value
        """
        super().__init__(
            connector=connector,
            esd_protection=esd_protection,
            termination_resistors=termination_resistors,
            vbus_cap=vbus_cap,
        )
        self.connector = connector
        self.esd_protection = esd_protection
        self.termination_resistors = termination_resistors
        self.vbus_cap = vbus_cap

    def _build_spec(self) -> PatternSpec:
        """Build the USB interface pattern specification."""
        components = ["connector", "vbus_cap"]
        rules = [
            PlacementRule(
                component="vbus_cap",
                relative_to="connector",
                max_distance_mm=3.0,
                rationale="VBUS decoupling at connector",
                priority=PlacementPriority.HIGH,
            ),
        ]

        if self.esd_protection:
            components.append("esd_protection")
            rules.append(
                PlacementRule(
                    component="esd_protection",
                    relative_to="connector",
                    max_distance_mm=5.0,
                    preferred_angle=0.0,  # Inline with data path
                    rationale="ESD protection within 5mm of connector for effectiveness",
                    priority=PlacementPriority.CRITICAL,
                )
            )

        if self.termination_resistors:
            components.extend(["term_r_dp", "term_r_dm"])
            rules.extend(
                [
                    PlacementRule(
                        component="term_r_dp",
                        relative_to="mcu_usb",
                        max_distance_mm=3.0,
                        rationale="D+ termination near MCU for impedance matching",
                        priority=PlacementPriority.HIGH,
                    ),
                    PlacementRule(
                        component="term_r_dm",
                        relative_to="mcu_usb",
                        max_distance_mm=3.0,
                        rationale="D- termination near MCU for impedance matching",
                        priority=PlacementPriority.HIGH,
                    ),
                ]
            )

        components.append("mcu_usb")

        routing_constraints = [
            RoutingConstraint(
                net_role="usb_dp",
                min_width_mm=0.15,  # For 90 ohm diff impedance
                max_length_mm=100.0,
                rationale="USB D+ differential pair, 90 ohm impedance",
            ),
            RoutingConstraint(
                net_role="usb_dm",
                min_width_mm=0.15,
                max_length_mm=100.0,
                rationale="USB D- differential pair, 90 ohm impedance",
            ),
            RoutingConstraint(
                net_role="vbus",
                min_width_mm=0.5,
                rationale="VBUS power (500mA typical)",
            ),
        ]

        return PatternSpec(
            name="usb_interface",
            description=f"USB interface ({self.connector}) with protection",
            components=components,
            placement_rules=rules,
            routing_constraints=routing_constraints,
        )

    def get_placements(self, anchor_at: tuple[float, float]) -> dict[str, Placement]:
        """Calculate optimal placements for USB components.

        Args:
            anchor_at: (x, y) position of USB connector in mm

        Returns:
            Dictionary mapping component roles to Placement objects
        """
        placements = {}

        # VBUS capacitor: adjacent to connector
        vbus_cap_pos = self._calculate_position(anchor_at, 2.0, 90.0)
        placements["vbus_cap"] = Placement(
            position=vbus_cap_pos,
            rotation=0.0,
            rationale=f"VBUS decoupling ({self.vbus_cap}) at connector",
        )

        if self.esd_protection:
            # ESD protection: inline between connector and MCU
            esd_pos = self._calculate_position(anchor_at, 4.0, 0.0)
            placements["esd_protection"] = Placement(
                position=esd_pos,
                rotation=0.0,
                rationale="ESD protection inline for shortest path to connector",
            )

        if self.termination_resistors:
            # Termination resistors: near MCU side
            # Assuming MCU is ~30mm from connector
            mcu_offset = 25.0
            term_dp_pos = (anchor_at[0] + mcu_offset, anchor_at[1] - 1.0)
            term_dm_pos = (anchor_at[0] + mcu_offset, anchor_at[1] + 1.0)

            placements["term_r_dp"] = Placement(
                position=term_dp_pos,
                rotation=90.0,
                rationale="D+ termination (22R typical) near MCU",
            )
            placements["term_r_dm"] = Placement(
                position=term_dm_pos,
                rotation=90.0,
                rationale="D- termination (22R typical) near MCU",
            )

        return placements

    def validate(self, pcb_path: Path | str) -> list[PatternViolation]:
        """Validate USB pattern implementation in a PCB.

        Args:
            pcb_path: Path to the KiCad PCB file

        Returns:
            List of pattern violations found
        """
        violations = []

        if not self.component_map:
            violations.append(
                PatternViolation(
                    rule=None,
                    component="",
                    message="No component mapping set. Call set_component_map() first.",
                    severity=PlacementPriority.CRITICAL,
                )
            )

        return violations


class I2CPattern(PCBPattern):
    """PCB placement pattern for I2C bus interfaces.

    I2C buses require proper pull-up resistor placement for reliable
    communication. This pattern handles single and multi-device I2C buses.

    Key placement rules:
    - Pull-up resistors: Central location for multi-device buses
    - Decoupling: At each device's VCC pin
    - Keep traces short for higher speed I2C

    Attributes:
        bus_speed: I2C speed mode (standard, fast, fast-plus)
        pull_up_value: Pull-up resistor value
    """

    def __init__(
        self,
        bus_speed: str = "fast",
        pull_up_value: str = "4.7k",
        device_count: int = 1,
    ) -> None:
        """Initialize I2C pattern.

        Args:
            bus_speed: I2C speed mode
            pull_up_value: Pull-up resistor value
            device_count: Number of I2C devices on bus
        """
        super().__init__(
            bus_speed=bus_speed,
            pull_up_value=pull_up_value,
            device_count=device_count,
        )
        self.bus_speed = bus_speed
        self.pull_up_value = pull_up_value
        self.device_count = device_count

    def _build_spec(self) -> PatternSpec:
        """Build the I2C bus pattern specification."""
        components = ["master", "pullup_sda", "pullup_scl"]
        components.extend(f"device_{i + 1}" for i in range(self.device_count))

        # Max trace length depends on bus speed
        max_length = {
            "standard": 1000.0,  # 100kHz - very forgiving
            "fast": 300.0,  # 400kHz
            "fast-plus": 100.0,  # 1MHz
        }.get(self.bus_speed, 300.0)

        rules = [
            PlacementRule(
                component="pullup_sda",
                relative_to="master",
                max_distance_mm=10.0,
                rationale="SDA pull-up near master for signal integrity",
                priority=PlacementPriority.HIGH,
            ),
            PlacementRule(
                component="pullup_scl",
                relative_to="master",
                max_distance_mm=10.0,
                rationale="SCL pull-up near master for signal integrity",
                priority=PlacementPriority.HIGH,
            ),
        ]

        routing_constraints = [
            RoutingConstraint(
                net_role="i2c_sda",
                min_width_mm=0.15,
                max_length_mm=max_length,
                rationale=f"SDA line for {self.bus_speed} mode",
            ),
            RoutingConstraint(
                net_role="i2c_scl",
                min_width_mm=0.15,
                max_length_mm=max_length,
                rationale=f"SCL line for {self.bus_speed} mode",
            ),
        ]

        return PatternSpec(
            name="i2c_bus",
            description=f"I2C bus ({self.bus_speed} mode) with {self.device_count} device(s)",
            components=components,
            placement_rules=rules,
            routing_constraints=routing_constraints,
        )

    def get_placements(self, anchor_at: tuple[float, float]) -> dict[str, Placement]:
        """Calculate optimal placements for I2C components.

        Args:
            anchor_at: (x, y) position of I2C master in mm

        Returns:
            Dictionary mapping component roles to Placement objects
        """
        placements = {}

        # Pull-up resistors: near master
        pullup_sda_pos = self._calculate_position(anchor_at, 5.0, 90.0)
        pullup_scl_pos = self._calculate_position(anchor_at, 5.0, 270.0)

        placements["pullup_sda"] = Placement(
            position=pullup_sda_pos,
            rotation=0.0,
            rationale=f"SDA pull-up ({self.pull_up_value})",
        )
        placements["pullup_scl"] = Placement(
            position=pullup_scl_pos,
            rotation=0.0,
            rationale=f"SCL pull-up ({self.pull_up_value})",
        )

        return placements

    def validate(self, pcb_path: Path | str) -> list[PatternViolation]:
        """Validate I2C pattern implementation in a PCB.

        Args:
            pcb_path: Path to the KiCad PCB file

        Returns:
            List of pattern violations found
        """
        violations = []

        if not self.component_map:
            violations.append(
                PatternViolation(
                    rule=None,
                    component="",
                    message="No component mapping set. Call set_component_map() first.",
                    severity=PlacementPriority.CRITICAL,
                )
            )

        return violations


# =============================================================================
# Constraint-based patterns (inherit from IntentPattern)
# =============================================================================


@dataclass
class SPIConfig:
    """Configuration for an SPI speed variant.

    Attributes:
        max_freq_hz: Maximum clock frequency in Hz.
        max_trace_length_mm: Maximum recommended trace length.
        length_tolerance_mm: Maximum length mismatch, or None if not required.
        termination_recommended: Whether series termination is recommended.
        ground_reference: Whether traces should be routed over ground.
    """

    max_freq_hz: float
    max_trace_length_mm: float
    length_tolerance_mm: float | None
    termination_recommended: bool
    ground_reference: bool = True


# SPI speed variant configurations
SPI_CONFIGS: dict[str, SPIConfig] = {
    "low": SPIConfig(
        max_freq_hz=1e6,
        max_trace_length_mm=300.0,
        length_tolerance_mm=None,
        termination_recommended=False,
        ground_reference=False,
    ),
    "standard": SPIConfig(
        max_freq_hz=10e6,
        max_trace_length_mm=200.0,
        length_tolerance_mm=None,
        termination_recommended=False,
        ground_reference=True,
    ),
    "high": SPIConfig(
        max_freq_hz=50e6,
        max_trace_length_mm=100.0,
        length_tolerance_mm=5.0,
        termination_recommended=True,
        ground_reference=True,
    ),
}


class SPIPattern(IntentPattern):
    """SPI bus pattern with proper layout guidelines.

    Provides placement and routing rules for SPI interfaces at different
    speed grades. Higher speeds require more careful layout with length
    matching and termination.

    Attributes:
        speed: Speed grade ("low", "standard", or "high").
        cs_count: Number of chip select lines.
    """

    def __init__(
        self,
        speed: Literal["low", "standard", "high"] = "standard",
        cs_count: int = 1,
    ) -> None:
        """Initialize SPI pattern.

        Args:
            speed: Speed grade. "low" (<1MHz), "standard" (1-10MHz),
                "high" (>10MHz up to 50MHz).
            cs_count: Number of chip select lines (1-8).

        Raises:
            ValueError: If speed or cs_count is invalid.
        """
        if speed not in SPI_CONFIGS:
            valid = ", ".join(SPI_CONFIGS.keys())
            raise ValueError(f"Invalid speed '{speed}'. Valid: {valid}")
        if not 1 <= cs_count <= 8:
            raise ValueError(f"cs_count must be 1-8, got {cs_count}")

        self._speed = speed
        self._cs_count = cs_count
        self._config = SPI_CONFIGS[speed]

    @property
    def name(self) -> str:
        """Return pattern name (e.g., 'spi_standard')."""
        return f"spi_{self._speed}"

    @property
    def category(self) -> InterfaceCategory:
        """Return BUS category for SPI."""
        return InterfaceCategory.BUS

    def get_placement_rules(self) -> list[ConstraintPlacementRule]:
        """Return SPI placement rules.

        Returns:
            List of placement rules for SPI layout.
        """
        rules = [
            ConstraintPlacementRule(
                name="clock_near_master",
                description="Keep CLK trace short by placing slave near master",
                priority=ConstraintPriority.RECOMMENDED,
                params={"max_distance_mm": self._config.max_trace_length_mm / 2},
            ),
            ConstraintPlacementRule(
                name="decoupling_near_slave",
                description="Place decoupling capacitors within 5mm of slave VDD pins",
                priority=ConstraintPriority.CRITICAL,
                params={"max_distance_mm": 5.0},
            ),
        ]

        if self._config.termination_recommended:
            rules.append(
                ConstraintPlacementRule(
                    name="termination_at_source",
                    description="Place series termination resistors (22-33Ω) near master",
                    priority=ConstraintPriority.RECOMMENDED,
                    component_refs=["R_TERM_CLK", "R_TERM_MOSI"],
                    params={"resistance_ohms": 33, "distance_mm": 5.0},
                )
            )

        return rules

    def get_routing_rules(self) -> list[ConstraintRoutingRule]:
        """Return SPI routing rules.

        Returns:
            List of routing rules for SPI traces.
        """
        rules = [
            ConstraintRoutingRule(
                name="max_trace_length",
                description=f"Keep all SPI traces under {self._config.max_trace_length_mm}mm",
                net_pattern="SPI_*",
                params={"max_mm": self._config.max_trace_length_mm},
            ),
        ]

        if self._config.ground_reference:
            rules.append(
                ConstraintRoutingRule(
                    name="ground_reference",
                    description="Route SPI traces over continuous ground plane",
                    net_pattern="SPI_*",
                    params={"reference_plane": "GND", "avoid_splits": True},
                )
            )

        if self._config.length_tolerance_mm is not None:
            rules.append(
                ConstraintRoutingRule(
                    name="length_matching",
                    description=f"Match CLK/MOSI/MISO lengths within ±{self._config.length_tolerance_mm}mm",
                    net_pattern="SPI_{CLK,MOSI,MISO}",
                    params={"tolerance_mm": self._config.length_tolerance_mm},
                )
            )

        return rules

    def validate(self, **kwargs: Any) -> list[str]:
        """Validate SPI pattern configuration.

        Args:
            **kwargs: Optional validation parameters:
                - nets: List of net names to validate count.

        Returns:
            List of validation error messages.
        """
        errors = []
        nets = kwargs.get("nets", [])

        if nets:
            # Minimum 3 nets: CLK, MOSI/MISO, CS
            min_nets = 3 + (self._cs_count - 1)  # Additional CS lines
            if len(nets) < min_nets:
                errors.append(
                    f"SPI {self._speed} with {self._cs_count} CS requires at least "
                    f"{min_nets} nets, got {len(nets)}"
                )

        return errors

    def derive_constraints(
        self, nets: list[str], params: dict[str, Any] | None = None
    ) -> list[Constraint]:
        """Derive intent constraints from SPI pattern.

        Args:
            nets: List of SPI net names.
            params: Optional parameters for constraint derivation.

        Returns:
            List of Constraint objects for the intent system.
        """
        params = params or {}
        constraints = []
        source = f"pattern:{self.name}"

        # Maximum trace length for all nets
        constraints.append(
            Constraint(
                type="max_length",
                params={"nets": nets, "max_mm": self._config.max_trace_length_mm},
                source=source,
                severity="warning",
            )
        )

        # Length matching for high-speed
        if self._config.length_tolerance_mm is not None:
            # Find data signals (exclude CS lines)
            data_nets = [n for n in nets if not any(cs in n.upper() for cs in ["CS", "SS"])]
            if len(data_nets) >= 2:
                constraints.append(
                    Constraint(
                        type="length_match",
                        params={
                            "nets": data_nets,
                            "tolerance_mm": self._config.length_tolerance_mm,
                        },
                        source=source,
                        severity="warning",
                    )
                )

        # Termination recommendation
        if self._config.termination_recommended:
            constraints.append(
                Constraint(
                    type="termination",
                    params={"nets": nets, "recommended": True, "resistance_ohms": 33},
                    source=source,
                    severity="warning",
                )
            )

        return constraints


@dataclass
class UARTConfig:
    """Configuration for a UART baud rate.

    Attributes:
        baud_rate: Baud rate in bits per second.
        max_trace_length_mm: Maximum recommended trace length.
        esd_recommended: Whether ESD protection is recommended.
    """

    baud_rate: int
    max_trace_length_mm: float
    esd_recommended: bool


# UART baud rate configurations
UART_CONFIGS: dict[str, UARTConfig] = {
    "low": UARTConfig(baud_rate=9600, max_trace_length_mm=500.0, esd_recommended=False),
    "standard": UARTConfig(baud_rate=115200, max_trace_length_mm=300.0, esd_recommended=False),
    "high": UARTConfig(baud_rate=921600, max_trace_length_mm=150.0, esd_recommended=True),
    "very_high": UARTConfig(baud_rate=3000000, max_trace_length_mm=75.0, esd_recommended=True),
}


class UARTPattern(IntentPattern):
    """UART interface pattern.

    Provides placement and routing rules for UART interfaces at different
    baud rates. Higher baud rates require shorter traces and may need
    ESD protection.

    Attributes:
        baud_rate: UART baud rate.
    """

    def __init__(self, baud_rate: int = 115200) -> None:
        """Initialize UART pattern.

        Args:
            baud_rate: Baud rate in bps. Common values: 9600, 115200, 921600, 3000000.
        """
        self._baud_rate = baud_rate

        # Find appropriate config based on baud rate
        if baud_rate <= 9600:
            self._config_name = "low"
        elif baud_rate <= 115200:
            self._config_name = "standard"
        elif baud_rate <= 921600:
            self._config_name = "high"
        else:
            self._config_name = "very_high"

        self._config = UARTConfig(
            baud_rate=baud_rate,
            max_trace_length_mm=UART_CONFIGS[self._config_name].max_trace_length_mm,
            esd_recommended=UART_CONFIGS[self._config_name].esd_recommended,
        )

    @property
    def name(self) -> str:
        """Return pattern name."""
        return f"uart_{self._baud_rate}"

    @property
    def category(self) -> InterfaceCategory:
        """Return SINGLE_ENDED category for UART."""
        return InterfaceCategory.SINGLE_ENDED

    def get_placement_rules(self) -> list[ConstraintPlacementRule]:
        """Return UART placement rules.

        Returns:
            List of placement rules for UART layout.
        """
        rules = [
            ConstraintPlacementRule(
                name="level_shifter_position",
                description="Place level shifter (if used) near connector",
                priority=ConstraintPriority.RECOMMENDED,
                params={"max_distance_mm": 10.0},
            ),
        ]

        if self._config.esd_recommended:
            rules.append(
                ConstraintPlacementRule(
                    name="esd_at_connector",
                    description="Place ESD protection at connector, before any series resistors",
                    priority=ConstraintPriority.CRITICAL,
                    component_refs=["D_ESD_TX", "D_ESD_RX"],
                    params={"max_distance_mm": 5.0},
                )
            )

        return rules

    def get_routing_rules(self) -> list[ConstraintRoutingRule]:
        """Return UART routing rules.

        Returns:
            List of routing rules for UART traces.
        """
        return [
            ConstraintRoutingRule(
                name="max_trace_length",
                description=f"Keep UART traces under {self._config.max_trace_length_mm}mm",
                net_pattern="UART_{TX,RX}",
                params={"max_mm": self._config.max_trace_length_mm},
            ),
            ConstraintRoutingRule(
                name="avoid_parallel_routing",
                description="Avoid routing TX and RX parallel for long distances to reduce crosstalk",
                net_pattern="UART_{TX,RX}",
                params={"max_parallel_mm": 50.0},
            ),
        ]

    def validate(self, **kwargs: Any) -> list[str]:
        """Validate UART pattern configuration.

        Args:
            **kwargs: Optional validation parameters.

        Returns:
            List of validation error messages.
        """
        errors = []
        nets = kwargs.get("nets", [])

        if nets and len(nets) < 2:
            errors.append("UART requires at least 2 nets (TX and RX)")

        if self._baud_rate > 3000000:
            errors.append(f"Baud rate {self._baud_rate} exceeds typical UART limits")

        return errors

    def derive_constraints(
        self, nets: list[str], params: dict[str, Any] | None = None
    ) -> list[Constraint]:
        """Derive intent constraints from UART pattern.

        Args:
            nets: List of UART net names.
            params: Optional parameters for constraint derivation.

        Returns:
            List of Constraint objects for the intent system.
        """
        constraints = []
        source = f"pattern:{self.name}"

        constraints.append(
            Constraint(
                type="max_length",
                params={"nets": nets, "max_mm": self._config.max_trace_length_mm},
                source=source,
                severity="warning",
            )
        )

        if self._config.esd_recommended:
            constraints.append(
                Constraint(
                    type="esd_protection",
                    params={"nets": nets, "recommended": True},
                    source=source,
                    severity="warning",
                )
            )

        return constraints


@dataclass
class EthernetConfig:
    """Configuration for an Ethernet speed variant.

    Attributes:
        speed_mbps: Speed in Mbps.
        differential_impedance_ohms: Target differential impedance.
        impedance_tolerance: Impedance tolerance as fraction (e.g., 0.1 = ±10%).
        pair_length_match_mm: Maximum length mismatch within a pair.
        inter_pair_skew_mm: Maximum skew between pairs.
        min_via_spacing_mm: Minimum spacing around vias for impedance control.
    """

    speed_mbps: int
    differential_impedance_ohms: float
    impedance_tolerance: float
    pair_length_match_mm: float
    inter_pair_skew_mm: float
    min_via_spacing_mm: float


# Ethernet speed configurations
ETHERNET_CONFIGS: dict[str, EthernetConfig] = {
    "10base_t": EthernetConfig(
        speed_mbps=10,
        differential_impedance_ohms=100.0,
        impedance_tolerance=0.15,
        pair_length_match_mm=50.0,
        inter_pair_skew_mm=100.0,
        min_via_spacing_mm=0.5,
    ),
    "100base_tx": EthernetConfig(
        speed_mbps=100,
        differential_impedance_ohms=100.0,
        impedance_tolerance=0.10,
        pair_length_match_mm=5.0,
        inter_pair_skew_mm=50.0,
        min_via_spacing_mm=0.5,
    ),
    "1000base_t": EthernetConfig(
        speed_mbps=1000,
        differential_impedance_ohms=100.0,
        impedance_tolerance=0.10,
        pair_length_match_mm=2.0,
        inter_pair_skew_mm=12.0,
        min_via_spacing_mm=0.3,
    ),
}


class EthernetPattern(IntentPattern):
    """Ethernet interface pattern.

    Provides placement and routing rules for Ethernet interfaces at different
    speeds. All Ethernet interfaces are differential and require impedance
    control.

    Attributes:
        speed: Ethernet speed variant.
    """

    def __init__(
        self,
        speed: Literal["10base_t", "100base_tx", "1000base_t"] = "100base_tx",
    ) -> None:
        """Initialize Ethernet pattern.

        Args:
            speed: Ethernet speed variant.

        Raises:
            ValueError: If speed is not recognized.
        """
        if speed not in ETHERNET_CONFIGS:
            valid = ", ".join(ETHERNET_CONFIGS.keys())
            raise ValueError(f"Invalid speed '{speed}'. Valid: {valid}")

        self._speed = speed
        self._config = ETHERNET_CONFIGS[speed]

    @property
    def name(self) -> str:
        """Return pattern name."""
        return f"ethernet_{self._speed}"

    @property
    def category(self) -> InterfaceCategory:
        """Return DIFFERENTIAL category for Ethernet."""
        return InterfaceCategory.DIFFERENTIAL

    def get_placement_rules(self) -> list[ConstraintPlacementRule]:
        """Return Ethernet placement rules.

        Returns:
            List of placement rules for Ethernet layout.
        """
        rules = [
            ConstraintPlacementRule(
                name="phy_near_jack",
                description="Place PHY chip close to RJ45 connector",
                priority=ConstraintPriority.CRITICAL,
                params={"max_distance_mm": 25.0},
            ),
            ConstraintPlacementRule(
                name="magnetics_inline",
                description="Place magnetics between PHY and connector, inline with pairs",
                priority=ConstraintPriority.CRITICAL,
                component_refs=["T1", "MAGNETICS"],
                params={"inline": True},
            ),
            ConstraintPlacementRule(
                name="crystal_near_phy",
                description="Place PHY crystal/oscillator within 10mm of PHY",
                priority=ConstraintPriority.RECOMMENDED,
                component_refs=["Y1", "Y_PHY"],
                params={"max_distance_mm": 10.0},
            ),
            ConstraintPlacementRule(
                name="esd_at_connector",
                description="Place ESD protection at RJ45 connector",
                priority=ConstraintPriority.CRITICAL,
                component_refs=["D_ESD"],
                params={"max_distance_mm": 5.0},
            ),
        ]

        return rules

    def get_routing_rules(self) -> list[ConstraintRoutingRule]:
        """Return Ethernet routing rules.

        Returns:
            List of routing rules for Ethernet traces.
        """
        rules = [
            ConstraintRoutingRule(
                name="differential_impedance",
                description=f"Route as {self._config.differential_impedance_ohms}Ω differential pairs",
                net_pattern="ETH_{TX,RX}*",
                params={
                    "impedance_ohms": self._config.differential_impedance_ohms,
                    "tolerance": self._config.impedance_tolerance,
                    "differential": True,
                },
            ),
            ConstraintRoutingRule(
                name="pair_length_matching",
                description=f"Match P/N within each pair to ±{self._config.pair_length_match_mm}mm",
                net_pattern="ETH_{TX,RX}*",
                params={"tolerance_mm": self._config.pair_length_match_mm},
            ),
            ConstraintRoutingRule(
                name="pair_coupling",
                description="Keep differential pairs tightly coupled",
                net_pattern="ETH_{TX,RX}*",
                params={"max_uncoupled_mm": 5.0},
            ),
            ConstraintRoutingRule(
                name="avoid_layer_changes",
                description="Minimize vias in differential pairs",
                net_pattern="ETH_{TX,RX}*",
                params={"max_vias": 2, "via_spacing_mm": self._config.min_via_spacing_mm},
            ),
        ]

        if self._config.speed_mbps >= 1000:
            rules.append(
                ConstraintRoutingRule(
                    name="inter_pair_skew",
                    description=f"Match all pairs within ±{self._config.inter_pair_skew_mm}mm",
                    net_pattern="ETH_*",
                    params={"tolerance_mm": self._config.inter_pair_skew_mm},
                )
            )

        return rules

    def validate(self, **kwargs: Any) -> list[str]:
        """Validate Ethernet pattern configuration.

        Args:
            **kwargs: Optional validation parameters.

        Returns:
            List of validation error messages.
        """
        errors = []
        nets = kwargs.get("nets", [])

        if nets:
            # 10/100 Mbps needs 2 pairs (4 nets), Gigabit needs 4 pairs (8 nets)
            min_nets = 8 if self._config.speed_mbps >= 1000 else 4
            if len(nets) < min_nets:
                errors.append(
                    f"Ethernet {self._speed} requires at least {min_nets} nets, got {len(nets)}"
                )

        return errors

    def derive_constraints(
        self, nets: list[str], params: dict[str, Any] | None = None
    ) -> list[Constraint]:
        """Derive intent constraints from Ethernet pattern.

        Args:
            nets: List of Ethernet net names.
            params: Optional parameters for constraint derivation.

        Returns:
            List of Constraint objects for the intent system.
        """
        constraints = []
        source = f"pattern:{self.name}"

        # Differential impedance constraint
        constraints.append(
            Constraint(
                type="impedance",
                params={
                    "nets": nets,
                    "target": self._config.differential_impedance_ohms,
                    "tolerance": self._config.impedance_tolerance,
                    "differential": True,
                },
                source=source,
                severity="error",
            )
        )

        # Length matching within pairs
        constraints.append(
            Constraint(
                type="length_match",
                params={"nets": nets, "tolerance_mm": self._config.pair_length_match_mm},
                source=source,
                severity="warning",
            )
        )

        # ESD protection
        constraints.append(
            Constraint(
                type="esd_protection",
                params={"nets": nets, "required": True},
                source=source,
                severity="warning",
            )
        )

        return constraints


__all__ = [
    # Placement patterns
    "USBPattern",
    "I2CPattern",
    # Constraint patterns
    "SPIPattern",
    "SPIConfig",
    "UARTPattern",
    "UARTConfig",
    "EthernetPattern",
    "EthernetConfig",
]
