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

User-Defined Patterns
---------------------

You can define custom patterns in two ways:

1. YAML Definition::

    # patterns/my_sensor.yaml
    name: temperature_sensor
    description: NTC thermistor with filtering

    components:
      - role: thermistor
        reference_prefix: RT
      - role: filter_cap
        reference_prefix: C

    placement_rules:
      - component: filter_cap
        relative_to: thermistor
        max_distance_mm: 5

    # Load and use
    from kicad_tools.patterns import PatternRegistry
    PatternRegistry.load_yaml("patterns/my_sensor.yaml")
    pattern = PatternRegistry.get("temperature_sensor")

2. Python Decorator::

    from kicad_tools.patterns import define_pattern, placement_rule

    @define_pattern
    class MySensorPattern:
        components = ["sensor", "cap"]
        placement_rules = [
            placement_rule("cap", relative_to="sensor", max_distance_mm=5.0),
        ]

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

User-Defined Pattern Support:
    - PatternRegistry: Register and retrieve patterns by name
    - PatternLoader: Load patterns from YAML files
    - define_pattern: Decorator for Python pattern definitions
    - Validation checks: ComponentDistanceCheck, ValueMatchCheck, etc.
"""

# Schema types
# Base class
from .base import PCBPattern

# Validation checks
from .checks import (
    CheckContext,
    ComponentDistanceCheck,
    ComponentPresentCheck,
    TraceLengthCheck,
    ValidationCheck,
    ValueMatchCheck,
    ValueRangeCheck,
    create_check,
    get_check,
    register_check,
)

# Pattern definition DSL
from .dsl import (
    define_pattern,
    get_pattern_from_class,
    get_pattern_name_from_class,
    placement_rule,
    routing_constraint,
)

# Interface patterns
from .interface import I2CPattern, USBPattern

# YAML pattern loader
from .loader import PatternLoader, YAMLPattern

# Power patterns
from .power import BuckPattern, LDOPattern

# Pattern registry
from .registry import PatternRegistry, register_pattern
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
    # Pattern registry
    "PatternRegistry",
    "register_pattern",
    # Pattern loader
    "PatternLoader",
    "YAMLPattern",
    # Pattern definition DSL
    "define_pattern",
    "placement_rule",
    "routing_constraint",
    "get_pattern_from_class",
    "get_pattern_name_from_class",
    # Validation checks
    "ValidationCheck",
    "CheckContext",
    "ComponentDistanceCheck",
    "ComponentPresentCheck",
    "TraceLengthCheck",
    "ValueMatchCheck",
    "ValueRangeCheck",
    "get_check",
    "create_check",
    "register_check",
]
