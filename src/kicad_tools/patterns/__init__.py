"""
PCB Pattern Library for KiCad.

This module provides two types of PCB patterns:

1. **Placement Patterns** (inherit from PCBPattern in base.py):
   - Focus on physical component placement calculations
   - Provide `get_placements()` and `validate()` methods
   - Examples: LDOPattern, BuckPattern, CrystalPattern, USBPattern, I2CPattern

2. **Constraint Patterns** (inherit from IntentPattern in constraints.py):
   - Focus on design rules and DRC constraint derivation
   - Integrate with the intent system
   - Provide `get_placement_rules()`, `get_routing_rules()`, `derive_constraints()`
   - Examples: SPIPattern, UARTPattern, EthernetPattern, ADCInputFilter, ESDProtection

Usage (Placement Pattern)::

    from kicad_tools.patterns import LDOPattern

    pattern = LDOPattern(
        regulator="AMS1117-3.3",
        input_cap="10uF",
        output_caps=["10uF", "100nF"],
    )

    # Get recommended PCB placements relative to anchor
    placements = pattern.get_placements(regulator_at=(50, 30))

Usage (Constraint Pattern)::

    from kicad_tools.patterns import SPIPattern, ESDProtection

    # Create an SPI pattern for high-speed operation
    spi = SPIPattern(speed="high", cs_count=2)

    # Get placement guidelines
    rules = spi.get_placement_rules()

    # Get constraints for the intent system
    constraints = spi.derive_constraints(
        nets=["SPI_CLK", "SPI_MOSI", "SPI_MISO", "SPI_CS0"]
    )

Validation:
    Use PatternValidator to check that instantiated patterns meet their
    design requirements including placement rules, routing constraints,
    and component values.

Adaptation:
    Use PatternAdapter to generate pattern parameters for specific components
    by loading requirements from the component database.

User-Defined Patterns:
    Custom patterns can be defined via YAML files or using the pattern
    definition DSL.
"""

# Analog patterns (constraint-based)
from .analog import (
    ADCInputFilter,
    DACOutputFilter,
    OpAmpCircuit,
    SensorInterface,
)

# Validation and adaptation
from kicad_tools.patterns.adaptation import (
    AdaptedPatternParams,
    PatternAdapter,
)

# Base classes
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
from kicad_tools.patterns.components import (
    ComponentRequirements,
    get_component_requirements,
    list_components,
)
from .constraints import (
    ConstraintPlacementRule,
    ConstraintPriority,
    ConstraintRoutingRule,
    IntentPattern,
)

# Pattern definition DSL
from .dsl import (
    define_pattern,
    get_pattern_from_class,
    get_pattern_name_from_class,
    placement_rule,
    routing_constraint,
)

# Interface patterns (both placement and constraint-based)
from .interface import (
    # Constraint patterns
    EthernetConfig,
    EthernetPattern,
    # Placement patterns
    I2CPattern,
    SPIConfig,
    SPIPattern,
    UARTConfig,
    UARTPattern,
    USBPattern,
)

# YAML pattern loader
from .loader import PatternLoader, YAMLPattern

# Power patterns (placement-based)
from .power import BuckPattern, LDOPattern

# Protection patterns (constraint-based)
from .protection import (
    ESDProtection,
    OvercurrentProtection,
    OvervoltageProtection,
    ReversePolarityProtection,
    ThermalShutdown,
)

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

# Timing patterns (placement-based)
from .timing import CrystalPattern, OscillatorPattern
from kicad_tools.patterns.validation import (
    PatternValidationResult,
    PatternValidator,
    ViolationSeverity,
)
from kicad_tools.patterns.validation import (
    PatternViolation as ValidationViolation,
)

__all__ = [
    # Schema types
    "Placement",
    "PlacementPriority",
    "PlacementRule",
    "PatternSpec",
    "PatternViolation",
    "RoutingConstraint",
    # Base classes (placement)
    "PCBPattern",
    # Base classes (constraint)
    "IntentPattern",
    "ConstraintPlacementRule",
    "ConstraintRoutingRule",
    "ConstraintPriority",
    # Power patterns
    "LDOPattern",
    "BuckPattern",
    # Timing patterns
    "CrystalPattern",
    "OscillatorPattern",
    # Interface patterns (placement)
    "USBPattern",
    "I2CPattern",
    # Interface patterns (constraint)
    "SPIPattern",
    "SPIConfig",
    "UARTPattern",
    "UARTConfig",
    "EthernetPattern",
    "EthernetConfig",
    # Analog patterns
    "ADCInputFilter",
    "DACOutputFilter",
    "OpAmpCircuit",
    "SensorInterface",
    # Protection patterns
    "ESDProtection",
    "OvercurrentProtection",
    "OvervoltageProtection",
    "ReversePolarityProtection",
    "ThermalShutdown",
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
    # Validation
    "PatternValidator",
    "PatternValidationResult",
    "ValidationViolation",
    "ViolationSeverity",
    # Adaptation
    "PatternAdapter",
    "AdaptedPatternParams",
    # Component database
    "ComponentRequirements",
    "get_component_requirements",
    "list_components",
]
