"""
PCB Pattern Library for KiCad.

This module provides PCB placement patterns that encapsulate best practices
for laying out common circuit topologies. Each pattern defines:

- Component roles and their relationships
- Placement rules with distance constraints
- Routing constraints for critical nets
- Validation logic to check implementations

Usage::

    from kicad_tools.patterns import LDOPattern

    pattern = LDOPattern(
        regulator="AMS1117-3.3",
        input_cap="10uF",
        output_caps=["10uF", "100nF"],
    )

    # Get recommended PCB placements relative to anchor
    placements = pattern.get_placements(regulator_at=(50, 30))
    # Returns:
    # {
    #   "input_cap": Placement(position=(48, 32), rotation=0, ...),
    #   "output_cap_1": Placement(position=(52, 32), rotation=0, ...),
    #   "output_cap_2": Placement(position=(52, 34), rotation=0, ...),
    # }

    # Map pattern roles to actual component references
    pattern.set_component_map({
        "regulator": "U1",
        "input_cap": "C1",
        "output_cap_1": "C2",
        "output_cap_2": "C3",
    })

    # Validate an existing PCB layout
    violations = pattern.validate("board.kicad_pcb")

Available Patterns:

Power Supply:
    - LDOPattern: Low-dropout regulator with decoupling
    - BuckPattern: Buck converter with proper hot loop layout

Timing:
    - CrystalPattern: Crystal oscillator with load capacitors
    - OscillatorPattern: External oscillator module

Interfaces:
    - USBPattern: USB interface with ESD protection
    - I2CPattern: I2C bus with pull-ups

Schema Types:
    - Placement: Computed position with rationale
    - PlacementRule: Rule for component positioning
    - RoutingConstraint: Constraint for trace routing
    - PatternSpec: Complete pattern specification
    - PatternViolation: Validation result
"""

# Schema types
# Base class
from .base import PCBPattern

# Interface patterns
from .interface import I2CPattern, USBPattern

# Power patterns
from .power import BuckPattern, LDOPattern
from .schema import (
    PatternSpec,
    PatternViolation,
    Placement,
    PlacementPriority,
    PlacementRule,
    RoutingConstraint,
)

# Timing patterns
from .timing import CrystalPattern, OscillatorPattern

__all__ = [
    # Schema types
    "Placement",
    "PlacementPriority",
    "PlacementRule",
    "PatternSpec",
    "PatternViolation",
    "RoutingConstraint",
    # Base class
    "PCBPattern",
    # Power patterns
    "LDOPattern",
    "BuckPattern",
    # Timing patterns
    "CrystalPattern",
    "OscillatorPattern",
    # Interface patterns
    "USBPattern",
    "I2CPattern",
]
