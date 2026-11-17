"""
Power supply PCB patterns: LDO and Buck converter placement rules.

This module provides PCB placement patterns for common power supply
topologies, including low-dropout regulators (LDO) and buck converters.

Example::

    from kicad_tools.patterns.power import LDOPattern

    pattern = LDOPattern(
        regulator="AMS1117-3.3",
        input_cap="10uF",
        output_caps=["10uF", "100nF"],
    )

    # Get recommended PCB placements
    placements = pattern.get_placements(regulator_at=(50, 30))
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


class LDOPattern(PCBPattern):
    """PCB placement pattern for Low Dropout Regulators.

    LDO circuits require careful capacitor placement for stability and
    noise performance. This pattern defines optimal placement rules
    for input and output decoupling capacitors.

    Key placement rules:
    - Input capacitor: Within 3mm of VIN pin
    - Output capacitors: Within 2mm of VOUT pin
    - Ground returns: Short, direct paths to regulator GND

    Attributes:
        regulator: Regulator part number or description
        input_cap: Input capacitor value
        output_caps: List of output capacitor values
    """

    def __init__(
        self,
        regulator: str = "LDO",
        input_cap: str = "10uF",
        output_caps: list[str] | None = None,
    ) -> None:
        """Initialize LDO pattern.

        Args:
            regulator: Regulator part number or description
            input_cap: Input capacitor value (e.g., "10uF")
            output_caps: List of output capacitor values
        """
        super().__init__(
            regulator=regulator,
            input_cap=input_cap,
            output_caps=output_caps,
        )
        self.regulator = regulator
        self.input_cap = input_cap
        self.output_caps = output_caps or ["10uF", "100nF"]

    def _build_spec(self) -> PatternSpec:
        """Build the LDO pattern specification."""
        components = ["regulator", "input_cap"]
        components.extend(f"output_cap_{i + 1}" for i in range(len(self.output_caps)))

        rules = [
            PlacementRule(
                component="input_cap",
                relative_to="regulator",
                max_distance_mm=3.0,
                preferred_angle=180.0,  # Left of regulator (towards input)
                angle_tolerance=45.0,
                rationale="Input cap within 3mm of VIN for input filtering",
                priority=PlacementPriority.CRITICAL,
            ),
        ]

        # Add rules for each output capacitor
        for i in range(len(self.output_caps)):
            rules.append(
                PlacementRule(
                    component=f"output_cap_{i + 1}",
                    relative_to="regulator",
                    max_distance_mm=2.0 + i * 1.0,  # Closer caps first
                    preferred_angle=0.0,  # Right of regulator (towards output)
                    angle_tolerance=45.0,
                    rationale=f"Output cap {i + 1} within {2.0 + i * 1.0:.1f}mm of VOUT",
                    priority=(PlacementPriority.CRITICAL if i == 0 else PlacementPriority.HIGH),
                )
            )

        routing_constraints = [
            RoutingConstraint(
                net_role="vin_power",
                min_width_mm=0.3,
                rationale="Wide trace for input power",
            ),
            RoutingConstraint(
                net_role="vout_power",
                min_width_mm=0.3,
                rationale="Wide trace for output power",
            ),
            RoutingConstraint(
                net_role="gnd_return",
                min_width_mm=0.3,
                plane_connection=True,
                rationale="Ground return via plane connection",
            ),
        ]

        return PatternSpec(
            name="ldo_regulator",
            description=f"LDO regulator ({self.regulator}) with decoupling",
            components=components,
            placement_rules=rules,
            routing_constraints=routing_constraints,
        )

    def get_placements(self, anchor_at: tuple[float, float]) -> dict[str, Placement]:
        """Calculate optimal placements for LDO components.

        Args:
            anchor_at: (x, y) position of the regulator in mm

        Returns:
            Dictionary mapping component roles to Placement objects
        """
        placements = {}

        # Input capacitor: left of regulator
        input_cap_pos = self._calculate_position(anchor_at, 2.5, 180.0)
        placements["input_cap"] = Placement(
            position=input_cap_pos,
            rotation=0.0,
            rationale=f"Input cap ({self.input_cap}) within 3mm of VIN",
        )

        # Output capacitors: right of regulator, staggered
        for i, cap_value in enumerate(self.output_caps):
            offset_y = i * 2.0  # Stagger vertically
            output_pos = self._calculate_position(anchor_at, 2.0 + i * 0.5, 0.0)
            output_pos = (output_pos[0], output_pos[1] + offset_y)
            placements[f"output_cap_{i + 1}"] = Placement(
                position=output_pos,
                rotation=0.0,
                rationale=f"Output cap ({cap_value}) within {2.0 + i * 1.0:.1f}mm of VOUT",
            )

        return placements

    def validate(self, pcb_path: Path | str) -> list[PatternViolation]:
        """Validate LDO pattern implementation in a PCB.

        Args:
            pcb_path: Path to the KiCad PCB file

        Returns:
            List of pattern violations found
        """
        violations = []

        # Note: Full PCB validation would require loading the PCB file
        # and extracting component positions. This is a placeholder that
        # can be extended when integrated with the PCB loading code.

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


class BuckPattern(PCBPattern):
    """PCB placement pattern for Buck (step-down) converters.

    Buck converters have stringent layout requirements due to high-frequency
    switching and high current paths. This pattern enforces critical placement
    rules for the hot loop, inductor, and output filtering.

    Key placement rules:
    - Input capacitor: Adjacent to switch node (< 2mm)
    - Output inductor: Close to switch node (< 5mm)
    - Output capacitor: Adjacent to inductor output (< 2mm)
    - Bootstrap capacitor: Close to driver (< 3mm)
    - Feedback resistors: Close to controller, away from switch node

    The "hot loop" (input cap -> high-side switch -> low-side switch -> input cap)
    must be minimized to reduce EMI.
    """

    def __init__(
        self,
        controller: str = "Buck",
        input_cap: str = "10uF",
        output_cap: str = "22uF",
        inductor: str = "4.7uH",
        bootstrap_cap: str = "100nF",
    ) -> None:
        """Initialize Buck converter pattern.

        Args:
            controller: Controller part number or description
            input_cap: Input capacitor value
            output_cap: Output capacitor value
            inductor: Inductor value
            bootstrap_cap: Bootstrap capacitor value
        """
        super().__init__(
            controller=controller,
            input_cap=input_cap,
            output_cap=output_cap,
            inductor=inductor,
            bootstrap_cap=bootstrap_cap,
        )
        self.controller = controller
        self.input_cap = input_cap
        self.output_cap = output_cap
        self.inductor = inductor
        self.bootstrap_cap = bootstrap_cap

    def _build_spec(self) -> PatternSpec:
        """Build the Buck converter pattern specification."""
        rules = [
            PlacementRule(
                component="input_cap",
                relative_to="controller",
                max_distance_mm=2.0,
                preferred_angle=180.0,
                rationale="Input cap adjacent to VIN pin to minimize hot loop",
                priority=PlacementPriority.CRITICAL,
            ),
            PlacementRule(
                component="inductor",
                relative_to="controller",
                max_distance_mm=5.0,
                preferred_angle=0.0,  # Towards output
                rationale="Inductor close to switch node",
                priority=PlacementPriority.CRITICAL,
            ),
            PlacementRule(
                component="output_cap",
                relative_to="inductor",
                max_distance_mm=2.0,
                preferred_angle=0.0,
                rationale="Output cap adjacent to inductor for low impedance",
                priority=PlacementPriority.CRITICAL,
            ),
            PlacementRule(
                component="bootstrap_cap",
                relative_to="controller",
                max_distance_mm=3.0,
                rationale="Bootstrap cap close to driver for proper operation",
                priority=PlacementPriority.HIGH,
            ),
            PlacementRule(
                component="feedback_divider",
                relative_to="controller",
                max_distance_mm=8.0,
                min_distance_mm=3.0,  # Keep away from switch node
                rationale="FB divider away from switch node to reduce noise",
                priority=PlacementPriority.HIGH,
            ),
        ]

        routing_constraints = [
            RoutingConstraint(
                net_role="switch_node",
                min_width_mm=0.5,
                max_length_mm=10.0,  # Minimize switch node area
                via_allowed=False,  # No vias in switch node
                rationale="Minimize switch node copper to reduce EMI",
            ),
            RoutingConstraint(
                net_role="vin_power",
                min_width_mm=0.5,
                rationale="Wide trace for input power",
            ),
            RoutingConstraint(
                net_role="vout_power",
                min_width_mm=0.5,
                rationale="Wide trace for output power",
            ),
            RoutingConstraint(
                net_role="gnd_power",
                plane_connection=True,
                rationale="Solid ground plane required",
            ),
            RoutingConstraint(
                net_role="feedback",
                max_length_mm=15.0,
                rationale="Keep feedback trace short and away from switch node",
            ),
        ]

        return PatternSpec(
            name="buck_converter",
            description=f"Buck converter ({self.controller}) with power stage",
            components=[
                "controller",
                "input_cap",
                "inductor",
                "output_cap",
                "bootstrap_cap",
                "feedback_divider",
            ],
            placement_rules=rules,
            routing_constraints=routing_constraints,
        )

    def get_placements(self, anchor_at: tuple[float, float]) -> dict[str, Placement]:
        """Calculate optimal placements for Buck converter components.

        Args:
            anchor_at: (x, y) position of the controller in mm

        Returns:
            Dictionary mapping component roles to Placement objects
        """
        placements = {}

        # Input capacitor: left of controller (towards input)
        input_cap_pos = self._calculate_position(anchor_at, 1.5, 180.0)
        placements["input_cap"] = Placement(
            position=input_cap_pos,
            rotation=0.0,
            rationale=f"Input cap ({self.input_cap}) minimizes hot loop area",
        )

        # Inductor: right of controller (towards output)
        inductor_pos = self._calculate_position(anchor_at, 4.0, 0.0)
        placements["inductor"] = Placement(
            position=inductor_pos,
            rotation=0.0,
            rationale=f"Inductor ({self.inductor}) close to switch node",
        )

        # Output capacitor: right of inductor
        output_cap_pos = self._calculate_position(inductor_pos, 1.5, 0.0)
        placements["output_cap"] = Placement(
            position=output_cap_pos,
            rotation=0.0,
            rationale=f"Output cap ({self.output_cap}) at inductor output",
        )

        # Bootstrap capacitor: above controller
        bootstrap_pos = self._calculate_position(anchor_at, 2.0, 270.0)
        placements["bootstrap_cap"] = Placement(
            position=bootstrap_pos,
            rotation=0.0,
            rationale=f"Bootstrap cap ({self.bootstrap_cap}) near boot pin",
        )

        # Feedback divider: below and right, away from switch node
        fb_pos = self._calculate_position(anchor_at, 5.0, 45.0)
        placements["feedback_divider"] = Placement(
            position=fb_pos,
            rotation=0.0,
            rationale="FB divider isolated from high-current paths",
        )

        return placements

    def validate(self, pcb_path: Path | str) -> list[PatternViolation]:
        """Validate Buck converter pattern implementation in a PCB.

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
