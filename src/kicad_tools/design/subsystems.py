"""
Subsystem type definitions for multi-resolution design abstraction.

This module defines the subsystem types that can be used with the Design
facade for high-level PCB design operations.

Example::

    from kicad_tools.design.subsystems import SUBSYSTEMS, SubsystemType

    # Get subsystem definition
    power_supply = SUBSYSTEMS[SubsystemType.POWER_SUPPLY]
    print(power_supply.patterns)  # ['ldo', 'buck', 'boost']
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass


class SubsystemType(Enum):
    """Types of subsystems supported by the design abstraction layer."""

    POWER_SUPPLY = "power_supply"
    MCU_CORE = "mcu_core"
    CONNECTOR = "connector"
    TIMING = "timing"
    ANALOG_INPUT = "analog_input"
    INTERFACE = "interface"


class OptimizationGoal(Enum):
    """Optimization goals for subsystem placement."""

    THERMAL = "thermal"
    ROUTING = "routing"
    COMPACT = "compact"
    SIGNAL_INTEGRITY = "signal_integrity"
    MECHANICAL = "mechanical"


@dataclass
class SubsystemDefinition:
    """Definition of a subsystem type with its placement rules.

    Attributes:
        subsystem_type: The type of subsystem
        patterns: List of pattern names that can implement this subsystem
        optimize_for: Default optimization goals for this subsystem
        anchor_role: The component role that serves as the anchor
        description: Human-readable description
        typical_components: Typical component types in this subsystem
        placement_hints: Additional placement guidance
    """

    subsystem_type: SubsystemType
    patterns: list[str]
    optimize_for: list[OptimizationGoal]
    anchor_role: str
    description: str = ""
    typical_components: list[str] = field(default_factory=list)
    placement_hints: dict[str, str] = field(default_factory=dict)


# Built-in subsystem definitions
SUBSYSTEMS: dict[SubsystemType, SubsystemDefinition] = {
    SubsystemType.POWER_SUPPLY: SubsystemDefinition(
        subsystem_type=SubsystemType.POWER_SUPPLY,
        patterns=["ldo", "buck", "boost"],
        optimize_for=[OptimizationGoal.THERMAL, OptimizationGoal.ROUTING],
        anchor_role="regulator",
        description="Power supply section with regulator and decoupling",
        typical_components=["regulator", "input_cap", "output_cap", "inductor"],
        placement_hints={
            "near_edge": "Place near board edge for thermal dissipation",
            "input_filtering": "Input caps should be closest to VIN",
            "output_filtering": "Output caps close to VOUT for stability",
        },
    ),
    SubsystemType.MCU_CORE: SubsystemDefinition(
        subsystem_type=SubsystemType.MCU_CORE,
        patterns=["mcu_bypass", "crystal", "reset"],
        optimize_for=[OptimizationGoal.SIGNAL_INTEGRITY, OptimizationGoal.ROUTING],
        anchor_role="mcu",
        description="MCU with bypass capacitors, crystal, and reset circuit",
        typical_components=["mcu", "bypass_cap", "crystal", "reset_cap", "reset_resistor"],
        placement_hints={
            "bypass_caps": "Place bypass caps radially around MCU power pins",
            "crystal": "Crystal and load caps close to OSC pins",
            "reset": "Reset circuit accessible for debug",
        },
    ),
    SubsystemType.CONNECTOR: SubsystemDefinition(
        subsystem_type=SubsystemType.CONNECTOR,
        patterns=["usb", "ethernet", "hdmi", "uart", "spi"],
        optimize_for=[OptimizationGoal.SIGNAL_INTEGRITY, OptimizationGoal.MECHANICAL],
        anchor_role="connector",
        description="Connector interface with ESD protection and termination",
        typical_components=["connector", "esd_protection", "termination", "filter"],
        placement_hints={
            "edge_placement": "Connectors typically at board edge",
            "esd": "ESD protection close to connector pins",
            "length_matching": "Differential pairs need length matching",
        },
    ),
    SubsystemType.TIMING: SubsystemDefinition(
        subsystem_type=SubsystemType.TIMING,
        patterns=["crystal", "oscillator"],
        optimize_for=[OptimizationGoal.SIGNAL_INTEGRITY],
        anchor_role="crystal",
        description="Timing circuit with crystal/oscillator and load capacitors",
        typical_components=["crystal", "load_cap_1", "load_cap_2"],
        placement_hints={
            "trace_length": "Keep traces to OSC pins short",
            "ground_plane": "Unbroken ground plane under crystal",
        },
    ),
    SubsystemType.ANALOG_INPUT: SubsystemDefinition(
        subsystem_type=SubsystemType.ANALOG_INPUT,
        patterns=["adc_input", "sensor_interface"],
        optimize_for=[OptimizationGoal.SIGNAL_INTEGRITY, OptimizationGoal.ROUTING],
        anchor_role="adc",
        description="Analog input section with filtering and protection",
        typical_components=["adc", "filter_cap", "filter_resistor", "protection"],
        placement_hints={
            "isolation": "Keep analog signals away from digital switching",
            "filtering": "RC filter close to ADC input pin",
        },
    ),
    SubsystemType.INTERFACE: SubsystemDefinition(
        subsystem_type=SubsystemType.INTERFACE,
        patterns=["spi", "i2c", "uart"],
        optimize_for=[OptimizationGoal.SIGNAL_INTEGRITY, OptimizationGoal.ROUTING],
        anchor_role="controller",
        description="Communication interface with termination and protection",
        typical_components=["controller", "pull_up", "termination", "esd"],
        placement_hints={
            "termination": "Termination resistors at receiver end",
            "pull_ups": "Pull-ups close to controller for I2C",
        },
    ),
}


def get_subsystem_definition(subsystem_type: str | SubsystemType) -> SubsystemDefinition:
    """Get the definition for a subsystem type.

    Args:
        subsystem_type: Subsystem type as string or enum

    Returns:
        SubsystemDefinition for the requested type

    Raises:
        ValueError: If subsystem type is not recognized
    """
    if isinstance(subsystem_type, str):
        try:
            subsystem_type = SubsystemType(subsystem_type)
        except ValueError as e:
            valid_types = [t.value for t in SubsystemType]
            raise ValueError(
                f"Unknown subsystem type: {subsystem_type}. Valid types: {valid_types}"
            ) from e

    if subsystem_type not in SUBSYSTEMS:
        raise ValueError(f"No definition found for subsystem type: {subsystem_type}")

    return SUBSYSTEMS[subsystem_type]


def list_subsystem_types() -> list[str]:
    """Get list of all available subsystem types.

    Returns:
        List of subsystem type names
    """
    return [t.value for t in SubsystemType]


__all__ = [
    "SubsystemType",
    "OptimizationGoal",
    "SubsystemDefinition",
    "SUBSYSTEMS",
    "get_subsystem_definition",
    "list_subsystem_types",
]
