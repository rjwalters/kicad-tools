"""
Pydantic models for .kct project specification format.

Defines the schema for PCB project specifications including:
- Project metadata
- Design intent and interfaces
- Requirements (electrical, mechanical, environmental)
- Suggestions and preferences
- Design decisions with rationale
- Progress tracking and validation results
"""

from __future__ import annotations

import datetime
from enum import Enum
from typing import Any

try:
    from pydantic import BaseModel, Field, field_validator
except ImportError as e:
    raise ImportError(
        "pydantic is required for the spec module. "
        "Install it with: pip install 'kicad-tools[mcp]' or pip install pydantic"
    ) from e


__all__ = [
    "ProjectSpec",
    "ProjectMetadata",
    "ProjectArtifacts",
    "DesignIntent",
    "Interface",
    "Requirements",
    "ElectricalRequirements",
    "InputRequirements",
    "OutputRail",
    "MechanicalRequirements",
    "MountingHole",
    "KeepOut",
    "EnvironmentalRequirements",
    "ManufacturingRequirements",
    "Compliance",
    "Suggestions",
    "ComponentSuggestion",
    "Decision",
    "Progress",
    "PhaseProgress",
    "Validation",
    "ValidationResult",
    "DesignPhase",
    "PhaseStatus",
]


class DesignPhase(str, Enum):
    """Design phase enumeration."""

    CONCEPT = "concept"
    ARCHITECTURE = "architecture"
    SCHEMATIC = "schematic"
    LAYOUT = "layout"
    REVIEW = "review"
    PRODUCTION = "production"


class PhaseStatus(str, Enum):
    """Phase status enumeration."""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    BLOCKED = "blocked"


class ProjectArtifacts(BaseModel):
    """Project file artifacts."""

    schematic: str | None = Field(default=None, description="Path to schematic file")
    pcb: str | None = Field(default=None, description="Path to PCB file")
    project: str | None = Field(default=None, description="Path to KiCad project file")


class ProjectMetadata(BaseModel):
    """Project metadata and identification."""

    name: str = Field(..., description="Project name")
    revision: str = Field(default="A", description="Project revision")
    created: datetime.date | None = Field(default=None, description="Creation date")
    author: str | None = Field(default=None, description="Author name or email")
    description: str | None = Field(default=None, description="Brief project description")
    artifacts: ProjectArtifacts | None = Field(default=None, description="Project file paths")


class Interface(BaseModel):
    """Interface definition for the design."""

    name: str = Field(..., description="Interface name")
    type: str = Field(..., description="Interface type (e.g., usb_c_pd_sink, power_rail, gpio)")
    description: str | None = Field(default=None, description="Interface description")
    # Type-specific fields stored flexibly
    profiles: list[str] | None = Field(default=None, description="Supported profiles for USB PD")
    voltage: str | None = Field(default=None, description="Voltage for power rails")
    current_max: str | None = Field(default=None, description="Maximum current")
    pins: list[str] | None = Field(default=None, description="Pin assignments")
    protocol: str | None = Field(default=None, description="Communication protocol")


class DesignIntent(BaseModel):
    """High-level design intent - the 'why' before the 'what'."""

    summary: str = Field(..., description="Brief summary of what the board does")
    use_cases: list[str] | None = Field(default=None, description="Intended use cases")
    interfaces: list[Interface] | None = Field(default=None, description="External interfaces")
    constraints: list[str] | None = Field(
        default=None, description="Key constraints driving the design"
    )


class InputRequirements(BaseModel):
    """Input power/signal requirements."""

    voltage: dict[str, str] | None = Field(
        default=None, description="Voltage range (min, max, nominal)"
    )
    current: dict[str, str] | None = Field(
        default=None, description="Current limits (max, typical)"
    )
    frequency: dict[str, str] | None = Field(
        default=None, description="Frequency range for signals"
    )


class OutputRail(BaseModel):
    """Output power rail specification."""

    rail: str = Field(..., description="Rail name or voltage")
    tolerance: str | None = Field(default=None, description="Voltage tolerance (e.g., ±2%)")
    current_max: str | None = Field(default=None, description="Maximum current")
    ripple_max: str | None = Field(default=None, description="Maximum ripple voltage")
    efficiency_min: str | None = Field(default=None, description="Minimum efficiency")
    load_regulation: str | None = Field(default=None, description="Load regulation spec")


class ElectricalRequirements(BaseModel):
    """Electrical requirements and specifications."""

    input: InputRequirements | None = Field(default=None, description="Input requirements")
    outputs: list[OutputRail] | None = Field(default=None, description="Output rail specifications")
    signals: dict[str, Any] | None = Field(
        default=None, description="Signal specifications (impedance, timing, etc.)"
    )
    esd_protection: str | None = Field(default=None, description="ESD protection level")
    isolation: str | None = Field(default=None, description="Isolation requirements")


class MountingHole(BaseModel):
    """Mounting hole specification."""

    x: str = Field(..., description="X position")
    y: str = Field(..., description="Y position")
    diameter: str = Field(..., description="Hole diameter")
    plated: bool = Field(default=False, description="Whether hole is plated")


class KeepOut(BaseModel):
    """Keep-out region specification."""

    region: dict[str, str] = Field(..., description="Region bounds (x, y, width, height)")
    reason: str = Field(..., description="Reason for keep-out")
    layers: list[str] | None = Field(default=None, description="Affected layers")


class MechanicalRequirements(BaseModel):
    """Mechanical requirements and form factor."""

    dimensions: dict[str, str] | None = Field(
        default=None, description="Board dimensions (width, height, thickness)"
    )
    mounting_holes: list[MountingHole] | None = Field(
        default=None, description="Mounting hole positions"
    )
    keep_outs: list[KeepOut] | None = Field(default=None, description="Keep-out regions")
    edge_clearance: str | None = Field(
        default=None, description="Required clearance from board edge"
    )
    weight_max: str | None = Field(default=None, description="Maximum board weight")
    enclosure: str | None = Field(default=None, description="Target enclosure reference")


class TemperatureRange(BaseModel):
    """Temperature range specification."""

    operating: list[str] | None = Field(default=None, description="Operating range [min, max]")
    storage: list[str] | None = Field(default=None, description="Storage range [min, max]")


class EnvironmentalRequirements(BaseModel):
    """Environmental requirements."""

    temperature: TemperatureRange | None = Field(default=None, description="Temperature ranges")
    humidity: str | None = Field(default=None, description="Humidity rating")
    vibration: str | None = Field(default=None, description="Vibration resistance")
    ip_rating: str | None = Field(default=None, description="IP rating")


class ManufacturingRequirements(BaseModel):
    """Manufacturing requirements and constraints."""

    layers: dict[str, int] | None = Field(
        default=None, description="Layer count (preferred, min, max)"
    )
    min_trace: str | None = Field(default=None, description="Minimum trace width")
    min_space: str | None = Field(default=None, description="Minimum spacing")
    min_via: str | None = Field(default=None, description="Minimum via size")
    min_drill: str | None = Field(default=None, description="Minimum drill size")
    target_fab: str | None = Field(default=None, description="Target fabricator (e.g., jlcpcb)")
    assembly: str | None = Field(
        default=None, description="Assembly type (smt, through_hole, both)"
    )
    finish: str | None = Field(default=None, description="Surface finish (HASL, ENIG, etc.)")
    solder_mask: str | None = Field(default=None, description="Solder mask color")
    silkscreen: str | None = Field(default=None, description="Silkscreen color")


class Compliance(BaseModel):
    """Compliance and certification requirements."""

    standards: list[str] | None = Field(
        default=None, description="Required standards (FCC, CE, etc.)"
    )
    rohs: bool = Field(default=True, description="RoHS compliance required")
    reach: bool = Field(default=False, description="REACH compliance required")
    ul: str | None = Field(default=None, description="UL certification requirements")


class Requirements(BaseModel):
    """All project requirements."""

    electrical: ElectricalRequirements | None = Field(
        default=None, description="Electrical requirements"
    )
    mechanical: MechanicalRequirements | None = Field(
        default=None, description="Mechanical requirements"
    )
    environmental: EnvironmentalRequirements | None = Field(
        default=None, description="Environmental requirements"
    )
    manufacturing: ManufacturingRequirements | None = Field(
        default=None, description="Manufacturing requirements"
    )
    compliance: Compliance | None = Field(default=None, description="Compliance requirements")


class ComponentSuggestion(BaseModel):
    """Component suggestion with preferences and rationale."""

    preferred: list[str] | None = Field(default=None, description="Preferred part numbers")
    rationale: str | None = Field(default=None, description="Reason for preference")
    avoid: list[str] | None = Field(default=None, description="Parts to avoid")


class BOMSuggestions(BaseModel):
    """BOM-related suggestions."""

    preferred_vendors: list[str] | None = Field(
        default=None, description="Preferred component vendors"
    )
    lifecycle_min_years: int | None = Field(
        default=None, description="Minimum lifecycle requirement in years"
    )
    cost_target: str | None = Field(default=None, description="Target BOM cost")


class Suggestions(BaseModel):
    """Design suggestions and preferences (SHOULD, not MUST)."""

    components: dict[str, ComponentSuggestion] | None = Field(
        default=None, description="Component suggestions by function"
    )
    layout: list[str] | None = Field(default=None, description="Layout suggestions")
    bom: BOMSuggestions | None = Field(default=None, description="BOM suggestions")
    notes: list[str] | None = Field(default=None, description="General design notes")


class Decision(BaseModel):
    """Design decision with rationale."""

    topic: str = Field(..., description="Decision topic")
    choice: str = Field(..., description="Chosen option")
    rationale: str = Field(..., description="Reasoning for the decision")
    date: datetime.date | None = Field(default=None, description="Decision date")
    phase: DesignPhase | str | None = Field(
        default=None, description="Design phase when decision was made"
    )
    alternatives: list[str] | None = Field(
        default=None, description="Alternative options considered"
    )
    author: str | None = Field(default=None, description="Who made the decision")


class PhaseProgress(BaseModel):
    """Progress tracking for a single phase."""

    status: PhaseStatus = Field(default=PhaseStatus.PENDING, description="Phase status")
    started: datetime.date | None = Field(default=None, description="Start date")
    completed: datetime.date | None = Field(default=None, description="Completion date")
    checklist: list[str] | None = Field(
        default=None, description="Checklist items with [x] or [ ] prefix"
    )
    notes: str | None = Field(default=None, description="Phase notes")


class Progress(BaseModel):
    """Overall project progress tracking."""

    phase: DesignPhase | str = Field(
        default=DesignPhase.CONCEPT, description="Current design phase"
    )
    phases: dict[str, PhaseProgress] | None = Field(default=None, description="Progress by phase")
    blockers: list[str] | None = Field(default=None, description="Current blockers")


class ValidationResult(BaseModel):
    """Single validation result."""

    status: str = Field(..., description="pass, fail, warning, pending")
    errors: int = Field(default=0, description="Error count")
    warnings: int = Field(default=0, description="Warning count")
    details: list[str] | None = Field(default=None, description="Detailed messages")


class Validation(BaseModel):
    """Validation results from design tools."""

    last_run: datetime.datetime | None = Field(
        default=None, description="Last validation timestamp"
    )
    schematic: dict[str, ValidationResult] | None = Field(
        default=None, description="Schematic validation (ERC, etc.)"
    )
    layout: dict[str, ValidationResult] | None = Field(
        default=None, description="Layout validation (DRC, etc.)"
    )
    bom: dict[str, ValidationResult] | None = Field(
        default=None, description="BOM validation (availability, etc.)"
    )


class ProjectSpec(BaseModel):
    """Complete project specification (.kct file)."""

    kct_version: str = Field(default="1.0", description="KCT format version")
    project: ProjectMetadata = Field(..., description="Project metadata")
    intent: DesignIntent | None = Field(default=None, description="Design intent")
    requirements: Requirements | None = Field(default=None, description="Requirements")
    suggestions: Suggestions | None = Field(default=None, description="Suggestions")
    decisions: list[Decision] | None = Field(default=None, description="Design decisions")
    progress: Progress | None = Field(default=None, description="Progress tracking")
    validation: Validation | None = Field(default=None, description="Validation results")

    @field_validator("kct_version")
    @classmethod
    def validate_version(cls, v: str) -> str:
        """Validate KCT format version."""
        supported = ["1.0"]
        if v not in supported:
            raise ValueError(f"Unsupported kct_version: {v}. Supported: {supported}")
        return v

    def get_completion_percentage(self) -> float:
        """Calculate overall completion percentage based on checklists."""
        if not self.progress or not self.progress.phases:
            return 0.0

        total_items = 0
        completed_items = 0

        for phase in self.progress.phases.values():
            if phase.checklist:
                for item in phase.checklist:
                    total_items += 1
                    if item.startswith("[x]") or item.startswith("[X]"):
                        completed_items += 1

        return (completed_items / total_items * 100) if total_items > 0 else 0.0

    def get_current_phase_progress(self) -> PhaseProgress | None:
        """Get progress for the current phase."""
        if not self.progress or not self.progress.phases:
            return None
        phase_key = (
            self.progress.phase.value
            if isinstance(self.progress.phase, DesignPhase)
            else self.progress.phase
        )
        return self.progress.phases.get(phase_key)
