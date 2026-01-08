"""
Project specification format (.kct) for PCB design capture.

The .kct format provides a structured way to capture design intent,
requirements, and progress for PCB projects. It enables:

1. Declarative design capture - Describe *what* the board should do before *how*
2. Agent-friendly specification - Machine-parseable yet human-readable
3. Progress tracking - Track design phases, decisions, and validation status
4. Requirement traceability - Link design choices to requirements

Usage:
    from kicad_tools.spec import ProjectSpec, load_spec, save_spec

    # Load existing spec
    spec = load_spec("project.kct")

    # Create new spec
    spec = ProjectSpec(
        project=ProjectMetadata(name="My Project"),
        intent=DesignIntent(summary="A power supply board"),
    )

    # Save spec
    save_spec(spec, "project.kct")
"""

from .parser import (
    create_minimal_spec,
    get_template,
    load_spec,
    save_spec,
    validate_spec,
)
from .schema import (
    Compliance,
    Decision,
    DesignIntent,
    ElectricalRequirements,
    Interface,
    ManufacturingRequirements,
    MechanicalRequirements,
    OutputRail,
    PhaseProgress,
    Progress,
    ProjectArtifacts,
    ProjectMetadata,
    ProjectSpec,
    Requirements,
    Suggestions,
    Validation,
    ValidationResult,
)
from .units import UnitValue, parse_unit_value

__all__ = [
    # Main types
    "ProjectSpec",
    "ProjectMetadata",
    "ProjectArtifacts",
    "DesignIntent",
    "Interface",
    "Requirements",
    "ElectricalRequirements",
    "OutputRail",
    "MechanicalRequirements",
    "ManufacturingRequirements",
    "Compliance",
    "Suggestions",
    "Decision",
    "Progress",
    "PhaseProgress",
    "Validation",
    "ValidationResult",
    # Unit parsing
    "UnitValue",
    "parse_unit_value",
    # Load/save/validate
    "load_spec",
    "save_spec",
    "validate_spec",
    "create_minimal_spec",
    "get_template",
]
