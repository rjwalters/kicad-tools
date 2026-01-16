"""
Interface PCB patterns: USB, I2C, and other communication interfaces.

This module provides PCB placement patterns for common communication
interfaces that require careful layout for signal integrity.

Example::

    from kicad_tools.patterns.interface import USBPattern

    pattern = USBPattern(
        connector="USB-C",
        esd_protection=True,
    )

    # Get recommended PCB placements
    placements = pattern.get_placements(connector_at=(5, 30))
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from .base import PCBPattern
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
