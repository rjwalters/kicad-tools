"""
Placement strategies for different subsystem types.

This module provides the PlacementStrategy base class and concrete
implementations for common subsystem types like power supplies,
MCU cores, and connectors.

Example::

    from kicad_tools.design.strategies import PowerSupplyStrategy

    strategy = PowerSupplyStrategy()
    placements = strategy.compute_placements(
        components=["U1", "C1", "C2"],
        anchor="U1",
        anchor_position=(20, 50),
        pcb=pcb,
    )
"""

from __future__ import annotations

import math
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from kicad_tools.design.subsystems import OptimizationGoal, SubsystemType

if TYPE_CHECKING:
    from kicad_tools.schema.pcb import PCB

# Regex pattern matching net names that indicate a crystal/oscillator pin
# on an MCU footprint. Matches XTAL, XTAL1/XTAL2, XTALIN/XTALOUT, OSC_IN,
# OSC_OUT, OSC32_IN, OSC32_OUT etc. Case-insensitive. Designed to be
# permissive enough to catch common STM32, AVR, PIC and TI naming
# conventions without false positives like "FAST_GPIO".
_XTAL_NET_PATTERN = re.compile(
    r"(?:^|[^A-Z0-9])(?:XTAL(?:IN|OUT|\d?)|OSC(?:32)?(?:_?IN|_?OUT|\d?))(?:$|[^A-Z0-9])",
    re.IGNORECASE,
)


@dataclass
class Placement:
    """A computed placement for a component.

    Attributes:
        ref: Component reference (e.g., "U1", "C1")
        x: X position in mm
        y: Y position in mm
        rotation: Rotation in degrees
        rationale: Human-readable explanation for this placement
    """

    ref: str
    x: float
    y: float
    rotation: float = 0.0
    rationale: str = ""


@dataclass
class PlacementPlan:
    """A plan for placing components in a subsystem.

    This is the result of planning a subsystem placement, showing
    all the moves that would be made without actually applying them.

    Attributes:
        steps: List of placements in order
        anchor: The anchor component reference
        anchor_position: Position of the anchor
        subsystem_type: Type of subsystem being placed
        optimization_goal: Goal used for optimization
        warnings: Any warnings about the plan
    """

    steps: list[Placement] = field(default_factory=list)
    anchor: str = ""
    anchor_position: tuple[float, float] = (0.0, 0.0)
    subsystem_type: str = ""
    optimization_goal: str = ""
    warnings: list[str] = field(default_factory=list)


class PlacementStrategy(ABC):
    """Abstract base class for placement strategies.

    A placement strategy encapsulates the logic for placing components
    in a subsystem based on design rules and optimization goals.
    """

    @property
    @abstractmethod
    def subsystem_type(self) -> SubsystemType:
        """The subsystem type this strategy handles."""

    @property
    @abstractmethod
    def supported_patterns(self) -> list[str]:
        """Pattern types supported by this strategy."""

    @abstractmethod
    def compute_placements(
        self,
        components: list[str],
        anchor: str,
        anchor_position: tuple[float, float],
        pcb: PCB,
        optimize_for: OptimizationGoal = OptimizationGoal.ROUTING,
        **kwargs: object,
    ) -> dict[str, Placement]:
        """Compute placements for all components in a subsystem.

        Args:
            components: List of component references to place
            anchor: The anchor component reference
            anchor_position: (x, y) position for the anchor
            pcb: The PCB object for context
            optimize_for: Optimization goal to use
            **kwargs: Additional strategy-specific options

        Returns:
            Dictionary mapping component refs to Placement objects
        """

    def _calculate_position(
        self,
        anchor: tuple[float, float],
        distance_mm: float,
        angle_degrees: float,
    ) -> tuple[float, float]:
        """Calculate position at given distance and angle from anchor.

        Args:
            anchor: (x, y) anchor position
            distance_mm: Distance from anchor in mm
            angle_degrees: Angle in degrees (0=right, 90=down, 180=left, 270=up)

        Returns:
            (x, y) calculated position
        """
        angle_rad = math.radians(angle_degrees)
        x = anchor[0] + distance_mm * math.cos(angle_rad)
        y = anchor[1] + distance_mm * math.sin(angle_rad)
        return (x, y)

    def _get_component_info(self, pcb: PCB, ref: str) -> dict | None:
        """Get information about a component from the PCB.

        Args:
            pcb: The PCB object
            ref: Component reference

        Returns:
            Dictionary with component info or None if not found
        """
        for fp in pcb.footprints:
            if fp.reference == ref:
                return {
                    "ref": ref,
                    "footprint": fp.footprint_name,
                    "position": fp.position,
                    "rotation": fp.rotation,
                }
        return None


class PowerSupplyStrategy(PlacementStrategy):
    """Placement strategy for power supply subsystems.

    Handles LDO, buck, and boost converter placement with proper
    consideration for input/output capacitor placement and thermal
    management.
    """

    @property
    def subsystem_type(self) -> SubsystemType:
        return SubsystemType.POWER_SUPPLY

    @property
    def supported_patterns(self) -> list[str]:
        return ["ldo", "buck", "boost"]

    def compute_placements(
        self,
        components: list[str],
        anchor: str,
        anchor_position: tuple[float, float],
        pcb: PCB,
        optimize_for: OptimizationGoal = OptimizationGoal.ROUTING,
        **kwargs: object,
    ) -> dict[str, Placement]:
        """Compute placements for power supply components.

        Power supply placement rules:
        - Input capacitor(s) close to VIN (left of regulator)
        - Output capacitor(s) close to VOUT (right of regulator)
        - For buck converters: inductor between switch and output
        - Thermal considerations for the regulator
        """
        placements = {}

        # Place anchor (regulator) at specified position
        placements[anchor] = Placement(
            ref=anchor,
            x=anchor_position[0],
            y=anchor_position[1],
            rotation=0.0,
            rationale="Anchor position for power supply subsystem",
        )

        # Classify components (simple heuristic based on reference prefix)
        input_caps = []
        output_caps = []
        inductors = []
        other = []

        for comp in components:
            if comp == anchor:
                continue

            comp_upper = comp.upper()
            if comp_upper.startswith("C"):
                # Heuristic: lower numbered caps are often input caps
                # This could be improved with schematic analysis
                if len(input_caps) == 0:
                    input_caps.append(comp)
                else:
                    output_caps.append(comp)
            elif comp_upper.startswith("L"):
                inductors.append(comp)
            else:
                other.append(comp)

        # Place input capacitors (left of regulator)
        for i, cap in enumerate(input_caps):
            pos = self._calculate_position(anchor_position, 2.5 + i * 2.0, 180.0)
            placements[cap] = Placement(
                ref=cap,
                x=pos[0],
                y=pos[1],
                rotation=0.0,
                rationale="Input capacitor within 3mm of VIN pin",
            )

        # Place inductors (if buck/boost) - between regulator and output
        for i, ind in enumerate(inductors):
            pos = self._calculate_position(anchor_position, 4.0, 0.0)
            placements[ind] = Placement(
                ref=ind,
                x=pos[0],
                y=pos[1],
                rotation=0.0,
                rationale="Inductor close to switch node",
            )

        # Place output capacitors (right of regulator/inductor)
        base_x = anchor_position[0] + (6.0 if inductors else 2.5)
        for i, cap in enumerate(output_caps):
            pos = (base_x + i * 2.0, anchor_position[1] + i * 1.5)
            placements[cap] = Placement(
                ref=cap,
                x=pos[0],
                y=pos[1],
                rotation=0.0,
                rationale="Output capacitor within 2mm of VOUT pin",
            )

        # Place other components below
        for i, comp in enumerate(other):
            pos = self._calculate_position(anchor_position, 3.0, 90.0 + i * 30)
            placements[comp] = Placement(
                ref=comp,
                x=pos[0],
                y=pos[1],
                rotation=0.0,
                rationale="Supporting component for power supply",
            )

        return placements


class MCUCoreStrategy(PlacementStrategy):
    """Placement strategy for MCU core subsystems.

    Handles MCU bypass capacitor placement, crystal oscillator,
    and reset circuit placement.
    """

    @property
    def subsystem_type(self) -> SubsystemType:
        return SubsystemType.MCU_CORE

    @property
    def supported_patterns(self) -> list[str]:
        return ["mcu_bypass", "crystal", "reset"]

    @staticmethod
    def _xtal_edge_angle(pcb: PCB, mcu_ref: str) -> float | None:
        """Determine the edge of an MCU package that carries XTAL/OSC pads.

        Inspects the MCU footprint, finds all pads whose ``net_name``
        matches a crystal-oscillator naming pattern (XTAL, XTAL1/2,
        XTALIN/OUT, OSC_IN/OUT, OSC32_IN/OUT, etc.), computes their
        centroid in the world frame (applying the footprint's rotation),
        and snaps the centroid direction to the nearest cardinal edge.

        Args:
            pcb: PCB containing the footprint.
            mcu_ref: Reference designator of the MCU (e.g. ``"U1"``).

        Returns:
            One of {0.0, 90.0, 180.0, 270.0} in the same screen-Y angle
            convention used by :meth:`_calculate_position`
            (0=east, 90=south, 180=west, 270=north). Returns ``None`` if
            the footprint cannot be located, has no pads, or has no
            pads whose net names match the XTAL/OSC pattern. Returning
            ``None`` signals the caller to fall back to the legacy
            default (270.0).
        """
        # Locate the footprint in the PCB. We tolerate missing pcb
        # attributes / empty PCBs so test harnesses that pass a minimal
        # stub PCB continue to work (the strategy was already permissive
        # — see existing tests in tests/test_design.py).
        footprints = getattr(pcb, "footprints", None)
        if not footprints:
            return None

        mcu_fp = None
        for fp in footprints:
            if getattr(fp, "reference", None) == mcu_ref:
                mcu_fp = fp
                break
        if mcu_fp is None:
            return None

        pads = getattr(mcu_fp, "pads", None) or []
        if not pads:
            return None

        # Collect pad-local coordinates of XTAL/OSC pads.
        xtal_offsets: list[tuple[float, float]] = []
        for pad in pads:
            net_name = getattr(pad, "net_name", "") or ""
            if not net_name:
                continue
            if _XTAL_NET_PATTERN.search(net_name):
                px, py = pad.position
                xtal_offsets.append((float(px), float(py)))

        if not xtal_offsets:
            return None

        # Rotate pad-local offsets into the world frame using the
        # footprint's rotation. KiCad applies the orientation as a NEGATED
        # angle vs standard CCW math (verified vs pcbnew 10.0.1, #3739).
        # This matches PCB.get_pad_position / core.geometry.rotate_pad_offset
        # and the router's adaptive._add_component_to_router transform.
        rotation_deg = float(getattr(mcu_fp, "rotation", 0.0) or 0.0)
        rot_rad = math.radians(-rotation_deg)
        cos_r, sin_r = math.cos(rot_rad), math.sin(rot_rad)

        sum_x = 0.0
        sum_y = 0.0
        for px, py in xtal_offsets:
            rx = px * cos_r - py * sin_r
            ry = px * sin_r + py * cos_r
            sum_x += rx
            sum_y += ry

        n = len(xtal_offsets)
        cx = sum_x / n
        cy = sum_y / n

        # Snap the centroid direction to the nearest cardinal edge.
        # Note: screen-Y convention (Y grows downward), so
        # 0=east(+x), 90=south(+y), 180=west(-x), 270=north(-y).
        # If the centroid is essentially at the MCU centre (extremely
        # unlikely but possible for a 1-pad XTAL or a fully symmetric
        # placement), fall back to the legacy default.
        if abs(cx) < 1e-9 and abs(cy) < 1e-9:
            return None

        if abs(cx) >= abs(cy):
            return 0.0 if cx > 0 else 180.0
        return 90.0 if cy > 0 else 270.0

    def compute_placements(
        self,
        components: list[str],
        anchor: str,
        anchor_position: tuple[float, float],
        pcb: PCB,
        optimize_for: OptimizationGoal = OptimizationGoal.ROUTING,
        **kwargs: object,
    ) -> dict[str, Placement]:
        """Compute placements for MCU core components.

        MCU placement rules:
        - Bypass capacitors radially around MCU power pins
        - Crystal and load caps close to OSC pins
        - Reset circuit accessible for debug
        """
        placements = {}

        # Place anchor (MCU) at specified position
        placements[anchor] = Placement(
            ref=anchor,
            x=anchor_position[0],
            y=anchor_position[1],
            rotation=0.0,
            rationale="Anchor position for MCU core subsystem",
        )

        # Classify components
        bypass_caps = []
        crystal = None
        load_caps = []
        reset_components = []
        other = []

        for comp in components:
            if comp == anchor:
                continue

            comp_upper = comp.upper()
            # Simple classification heuristic
            if comp_upper.startswith("C"):
                if crystal is not None and len(load_caps) < 2:
                    load_caps.append(comp)
                else:
                    bypass_caps.append(comp)
            elif comp_upper.startswith("Y") or comp_upper.startswith("X"):
                crystal = comp
            elif comp_upper.startswith("R"):
                reset_components.append(comp)
            else:
                other.append(comp)

        # Place bypass capacitors radially around MCU
        # Typical positions: corners and edges
        bypass_angles = [45, 135, 225, 315]
        for i, cap in enumerate(bypass_caps):
            angle = bypass_angles[i % len(bypass_angles)]
            distance = 4.0 + (i // len(bypass_angles)) * 2.0
            pos = self._calculate_position(anchor_position, distance, angle)
            placements[cap] = Placement(
                ref=cap,
                x=pos[0],
                y=pos[1],
                rotation=0.0,
                rationale="Bypass capacitor close to MCU power pin",
            )

        # Place crystal (typically near OSC pins on one side)
        if crystal:
            # Inspect the MCU footprint's XTAL/OSC pads to choose a side.
            # Falls back to 270 deg ("above MCU" in screen-Y convention)
            # when the footprint has no XTAL-named pins or when the MCU
            # can't be located in the PCB.
            xtal_angle = self._xtal_edge_angle(pcb, anchor)
            crystal_angle = xtal_angle if xtal_angle is not None else 270.0
            pos = self._calculate_position(anchor_position, 5.0, crystal_angle)
            rationale = (
                f"Crystal placed on MCU XTAL-pin edge (angle {crystal_angle:.0f} deg)"
                if xtal_angle is not None
                else "Crystal close to OSC pins (fallback: no XTAL pads found)"
            )
            placements[crystal] = Placement(
                ref=crystal,
                x=pos[0],
                y=pos[1],
                rotation=0.0,
                rationale=rationale,
            )

            # Place load caps near crystal. The two caps straddle the
            # crystal along the axis perpendicular to the MCU-to-crystal
            # vector (so each load cap sits adjacent to one crystal
            # pad/MCU XTAL pin) and are nudged 1.5mm back toward the MCU
            # so their GND pads sit closest to the MCU ground.
            perp_rad = math.radians(crystal_angle + 90.0)
            perp_dx = math.cos(perp_rad)
            perp_dy = math.sin(perp_rad)
            # Unit vector from crystal back toward MCU centre (= -outward).
            inward_rad = math.radians(crystal_angle + 180.0)
            inward_dx = math.cos(inward_rad)
            inward_dy = math.sin(inward_rad)
            for i, cap in enumerate(load_caps):
                offset = 1.5 if i == 0 else -1.5
                placements[cap] = Placement(
                    ref=cap,
                    x=pos[0] + offset * perp_dx + 1.5 * inward_dx,
                    y=pos[1] + offset * perp_dy + 1.5 * inward_dy,
                    rotation=0.0,
                    rationale="Load capacitor near crystal",
                )

        # Place reset components
        for i, comp in enumerate(reset_components):
            pos = self._calculate_position(anchor_position, 6.0 + i * 2.0, 0.0)
            placements[comp] = Placement(
                ref=comp,
                x=pos[0],
                y=pos[1],
                rotation=0.0,
                rationale="Reset circuit component",
            )

        # Place other components
        for i, comp in enumerate(other):
            pos = self._calculate_position(anchor_position, 7.0, 90.0 + i * 20)
            placements[comp] = Placement(
                ref=comp,
                x=pos[0],
                y=pos[1],
                rotation=0.0,
                rationale="Supporting component for MCU",
            )

        return placements


class ConnectorStrategy(PlacementStrategy):
    """Placement strategy for connector interface subsystems.

    Handles USB, Ethernet, HDMI and other connector interfaces
    with ESD protection and termination.
    """

    @property
    def subsystem_type(self) -> SubsystemType:
        return SubsystemType.CONNECTOR

    @property
    def supported_patterns(self) -> list[str]:
        return ["usb", "ethernet", "hdmi", "uart", "spi"]

    def compute_placements(
        self,
        components: list[str],
        anchor: str,
        anchor_position: tuple[float, float],
        pcb: PCB,
        optimize_for: OptimizationGoal = OptimizationGoal.ROUTING,
        **kwargs: object,
    ) -> dict[str, Placement]:
        """Compute placements for connector interface components.

        Connector placement rules:
        - Connector typically at board edge
        - ESD protection close to connector pins
        - Termination and filtering between connector and IC
        """
        placements = {}

        # Place anchor (connector) at specified position
        placements[anchor] = Placement(
            ref=anchor,
            x=anchor_position[0],
            y=anchor_position[1],
            rotation=0.0,
            rationale="Anchor position for connector interface",
        )

        # Classify components
        esd_protection = []
        filters = []
        termination = []
        other = []

        for comp in components:
            if comp == anchor:
                continue

            comp_upper = comp.upper()
            # Simple classification heuristic
            if comp_upper.startswith("D") or "ESD" in comp_upper:
                esd_protection.append(comp)
            elif comp_upper.startswith("FB") or comp_upper.startswith("L"):
                filters.append(comp)
            elif comp_upper.startswith("R"):
                termination.append(comp)
            elif comp_upper.startswith("C"):
                filters.append(comp)
            else:
                other.append(comp)

        # Place ESD protection (closest to connector)
        for i, comp in enumerate(esd_protection):
            pos = self._calculate_position(anchor_position, 3.0 + i * 2.5, 0.0)
            placements[comp] = Placement(
                ref=comp,
                x=pos[0],
                y=pos[1],
                rotation=0.0,
                rationale="ESD protection close to connector",
            )

        # Place filters (between ESD and main circuit)
        base_x = anchor_position[0] + (5.0 if esd_protection else 3.0)
        for i, comp in enumerate(filters):
            y_offset = (i - len(filters) / 2) * 2.0
            placements[comp] = Placement(
                ref=comp,
                x=base_x + 2.0,
                y=anchor_position[1] + y_offset,
                rotation=0.0,
                rationale="Filter component for connector interface",
            )

        # Place termination resistors
        base_x = base_x + 4.0
        for i, comp in enumerate(termination):
            y_offset = (i - len(termination) / 2) * 2.0
            placements[comp] = Placement(
                ref=comp,
                x=base_x,
                y=anchor_position[1] + y_offset,
                rotation=90.0,  # Vertical orientation for termination
                rationale="Termination resistor for signal integrity",
            )

        # Place other components
        for i, comp in enumerate(other):
            pos = self._calculate_position(anchor_position, 8.0 + i * 2.0, 0.0)
            placements[comp] = Placement(
                ref=comp,
                x=pos[0],
                y=pos[1],
                rotation=0.0,
                rationale="Supporting component for connector",
            )

        return placements


# Strategy registry
STRATEGIES: dict[SubsystemType, type[PlacementStrategy]] = {
    SubsystemType.POWER_SUPPLY: PowerSupplyStrategy,
    SubsystemType.MCU_CORE: MCUCoreStrategy,
    SubsystemType.CONNECTOR: ConnectorStrategy,
}


def get_strategy(subsystem_type: str | SubsystemType) -> PlacementStrategy:
    """Get a placement strategy for a subsystem type.

    Args:
        subsystem_type: Subsystem type as string or enum

    Returns:
        PlacementStrategy instance for the requested type

    Raises:
        ValueError: If no strategy exists for the subsystem type
    """
    if isinstance(subsystem_type, str):
        try:
            subsystem_type = SubsystemType(subsystem_type)
        except ValueError as e:
            valid_types = list(STRATEGIES.keys())
            raise ValueError(
                f"Unknown subsystem type: {subsystem_type}. "
                f"Valid types: {[t.value for t in valid_types]}"
            ) from e

    if subsystem_type not in STRATEGIES:
        raise ValueError(f"No strategy found for subsystem type: {subsystem_type}")

    return STRATEGIES[subsystem_type]()


__all__ = [
    "Placement",
    "PlacementPlan",
    "PlacementStrategy",
    "PowerSupplyStrategy",
    "MCUCoreStrategy",
    "ConnectorStrategy",
    "STRATEGIES",
    "get_strategy",
]
