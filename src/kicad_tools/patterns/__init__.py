"""Pattern validation and adaptation framework.

This module provides tools for validating circuit pattern implementations
and adapting patterns for specific components.

Validation:
    Use PatternValidator to check that instantiated patterns meet their
    design requirements including placement rules, routing constraints,
    and component values.

    >>> from kicad_tools.patterns import PatternValidator
    >>> from kicad_tools.schema.pcb import PCB
    >>>
    >>> pcb = PCB.load("board.kicad_pcb")
    >>> validator = PatternValidator()
    >>> result = validator.validate_ldo_pattern(
    ...     pcb,
    ...     regulator="U1",
    ...     input_cap="C1",
    ...     output_caps=["C2", "C3"],
    ... )
    >>> for violation in result:
    ...     print(f"{violation.severity}: {violation.message}")

Adaptation:
    Use PatternAdapter to generate pattern parameters for specific components
    by loading requirements from the component database.

    >>> from kicad_tools.patterns import PatternAdapter
    >>>
    >>> adapter = PatternAdapter()
    >>> params = adapter.adapt_ldo_pattern("AMS1117-3.3")
    >>> print(params.parameters)
    {'input_cap': '10uF', 'output_caps': ['10uF', '100nF'], ...}

Component Database:
    Query component requirements directly using get_component_requirements().

    >>> from kicad_tools.patterns import get_component_requirements
    >>>
    >>> reqs = get_component_requirements("AMS1117-3.3")
    >>> print(f"Input cap: {reqs.input_cap_min_uf}uF")
    Input cap: 10.0uF
"""

from kicad_tools.patterns.adaptation import (
    AdaptedPatternParams,
    PatternAdapter,
)
from kicad_tools.patterns.components import (
    ComponentRequirements,
    get_component_requirements,
    list_components,
)
from kicad_tools.patterns.validation import (
    PatternValidationResult,
    PatternValidator,
    PatternViolation,
    ViolationSeverity,
)

__all__ = [
    # Validation
    "PatternValidator",
    "PatternViolation",
    "PatternValidationResult",
    "ViolationSeverity",
    # Adaptation
    "PatternAdapter",
    "AdaptedPatternParams",
    # Component database
    "ComponentRequirements",
    "get_component_requirements",
    "list_components",
]
