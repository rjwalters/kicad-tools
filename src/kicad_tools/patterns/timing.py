"""
Timing circuit PCB patterns: Crystal oscillators and clock distribution.

This module provides PCB placement patterns for timing-critical circuits,
including crystal oscillators and clock buffers.

Example::

    from kicad_tools.patterns.timing import CrystalPattern

    pattern = CrystalPattern(
        crystal="8MHz",
        load_caps=["18pF", "18pF"],
    )

    # Get recommended PCB placements
    placements = pattern.get_placements(mcu_osc_at=(45, 25))
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


class CrystalPattern(PCBPattern):
    """PCB placement pattern for crystal oscillators.

    Crystal oscillator circuits are sensitive to parasitic capacitance
    and noise. Proper placement minimizes trace lengths and keeps the
    crystal close to the MCU oscillator pins.

    Key placement rules:
    - Crystal: Within 5mm of OSC pins
    - Load capacitors: Within 2mm of crystal, symmetrical placement
    - Ground return: Short path to MCU ground
    - Keep-out: No high-speed signals near crystal traces

    Attributes:
        crystal: Crystal frequency or part number
        load_caps: List of load capacitor values [CL1, CL2]
    """

    def __init__(
        self,
        crystal: str = "8MHz",
        load_caps: list[str] | None = None,
    ) -> None:
        """Initialize crystal pattern.

        Args:
            crystal: Crystal frequency or part number
            load_caps: Load capacitor values [CL1, CL2], typically matching
        """
        super().__init__(
            crystal=crystal,
            load_caps=load_caps,
        )
        self.crystal = crystal
        self.load_caps = load_caps or ["18pF", "18pF"]

    def _build_spec(self) -> PatternSpec:
        """Build the crystal oscillator pattern specification."""
        rules = [
            PlacementRule(
                component="crystal",
                relative_to="mcu_osc",
                max_distance_mm=5.0,
                rationale="Crystal close to OSC pins to minimize trace capacitance",
                priority=PlacementPriority.CRITICAL,
            ),
            PlacementRule(
                component="load_cap_1",
                relative_to="crystal",
                max_distance_mm=2.0,
                preferred_angle=270.0,  # Below crystal
                rationale="Load cap CL1 adjacent to crystal XIN pin",
                priority=PlacementPriority.CRITICAL,
            ),
            PlacementRule(
                component="load_cap_2",
                relative_to="crystal",
                max_distance_mm=2.0,
                preferred_angle=270.0,  # Below crystal, offset
                rationale="Load cap CL2 adjacent to crystal XOUT pin",
                priority=PlacementPriority.CRITICAL,
            ),
        ]

        routing_constraints = [
            RoutingConstraint(
                net_role="osc_in",
                max_length_mm=8.0,
                via_allowed=False,
                rationale="Minimize XIN trace length, no vias",
            ),
            RoutingConstraint(
                net_role="osc_out",
                max_length_mm=8.0,
                via_allowed=False,
                rationale="Minimize XOUT trace length, no vias",
            ),
            RoutingConstraint(
                net_role="crystal_gnd",
                plane_connection=True,
                rationale="Load caps ground via plane",
            ),
        ]

        return PatternSpec(
            name="crystal_oscillator",
            description=f"Crystal oscillator ({self.crystal}) with load caps",
            components=["mcu_osc", "crystal", "load_cap_1", "load_cap_2"],
            placement_rules=rules,
            routing_constraints=routing_constraints,
        )

    def get_placements(self, anchor_at: tuple[float, float]) -> dict[str, Placement]:
        """Calculate optimal placements for crystal components.

        Args:
            anchor_at: (x, y) position of MCU oscillator pins in mm

        Returns:
            Dictionary mapping component roles to Placement objects
        """
        placements = {}

        # Crystal: close to MCU OSC pins
        crystal_pos = self._calculate_position(anchor_at, 3.0, 0.0)
        placements["crystal"] = Placement(
            position=crystal_pos,
            rotation=0.0,
            rationale=f"Crystal ({self.crystal}) within 5mm of OSC pins",
        )

        # Load capacitors: below crystal, symmetrical
        cap_spacing = 2.5  # mm between caps

        load_cap_1_pos = (crystal_pos[0] - cap_spacing / 2, crystal_pos[1] + 2.0)
        placements["load_cap_1"] = Placement(
            position=load_cap_1_pos,
            rotation=0.0,
            rationale=f"Load cap ({self.load_caps[0]}) for XIN",
        )

        load_cap_2_pos = (crystal_pos[0] + cap_spacing / 2, crystal_pos[1] + 2.0)
        placements["load_cap_2"] = Placement(
            position=load_cap_2_pos,
            rotation=0.0,
            rationale=f"Load cap ({self.load_caps[1]}) for XOUT",
        )

        return placements

    def validate(self, pcb_path: Path | str) -> list[PatternViolation]:
        """Validate crystal pattern implementation in a PCB.

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


class OscillatorPattern(PCBPattern):
    """PCB placement pattern for external oscillator modules.

    External oscillators (pre-packaged clock sources) are simpler than
    crystals but still require proper placement for clean clock signals.

    Key placement rules:
    - Oscillator: Within 20mm of clock input
    - Decoupling capacitor: Adjacent to VCC pin
    - Output: Direct connection, short trace
    """

    def __init__(
        self,
        oscillator: str = "Oscillator",
        frequency: str = "25MHz",
        decoupling_cap: str = "100nF",
    ) -> None:
        """Initialize oscillator pattern.

        Args:
            oscillator: Oscillator part number or description
            frequency: Oscillator frequency
            decoupling_cap: Decoupling capacitor value
        """
        super().__init__(
            oscillator=oscillator,
            frequency=frequency,
            decoupling_cap=decoupling_cap,
        )
        self.oscillator = oscillator
        self.frequency = frequency
        self.decoupling_cap = decoupling_cap

    def _build_spec(self) -> PatternSpec:
        """Build the oscillator module pattern specification."""
        rules = [
            PlacementRule(
                component="oscillator",
                relative_to="clock_input",
                max_distance_mm=20.0,
                rationale="Oscillator reasonably close to clock input",
                priority=PlacementPriority.HIGH,
            ),
            PlacementRule(
                component="decoupling_cap",
                relative_to="oscillator",
                max_distance_mm=2.0,
                rationale="Decoupling cap adjacent to oscillator VCC",
                priority=PlacementPriority.CRITICAL,
            ),
        ]

        routing_constraints = [
            RoutingConstraint(
                net_role="clock_output",
                min_width_mm=0.15,
                max_length_mm=30.0,
                rationale="Clock signal with controlled impedance",
            ),
            RoutingConstraint(
                net_role="osc_power",
                plane_connection=True,
                rationale="Clean power via plane",
            ),
        ]

        return PatternSpec(
            name="oscillator_module",
            description=f"Oscillator module ({self.oscillator} {self.frequency})",
            components=["clock_input", "oscillator", "decoupling_cap"],
            placement_rules=rules,
            routing_constraints=routing_constraints,
        )

    def get_placements(self, anchor_at: tuple[float, float]) -> dict[str, Placement]:
        """Calculate optimal placements for oscillator components.

        Args:
            anchor_at: (x, y) position of clock input pin in mm

        Returns:
            Dictionary mapping component roles to Placement objects
        """
        placements = {}

        # Oscillator: nearby clock input
        osc_pos = self._calculate_position(anchor_at, 10.0, 180.0)
        placements["oscillator"] = Placement(
            position=osc_pos,
            rotation=0.0,
            rationale=f"Oscillator ({self.frequency}) near clock input",
        )

        # Decoupling cap: adjacent to oscillator
        cap_pos = self._calculate_position(osc_pos, 1.5, 90.0)
        placements["decoupling_cap"] = Placement(
            position=cap_pos,
            rotation=0.0,
            rationale=f"Decoupling ({self.decoupling_cap}) at oscillator VCC",
        )

        return placements

    def validate(self, pcb_path: Path | str) -> list[PatternViolation]:
        """Validate oscillator pattern implementation in a PCB.

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
