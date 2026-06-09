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
import re
from enum import Enum
from typing import Any, Literal

try:
    from pydantic import BaseModel, Field, field_validator, model_validator
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
    "MountingHoleGroupSpec",
    "KeepOut",
    "EnvironmentalRequirements",
    "ManufacturingRequirements",
    "EscalationPolicy",
    "Compliance",
    "Suggestions",
    "ComponentSuggestion",
    "BOMEntry",
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
    pcb: str | None = Field(default=None, description="Path to PCB file (unrouted)")
    pcb_routed: str | None = Field(
        default=None,
        description=(
            "Path to routed PCB file. If unset, defaults to '<pcb_stem>_routed.kicad_pcb' "
            "alongside the PCB. Consumed by `kct build` (see _run_step_route)."
        ),
    )
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


class MountingHoleGroupSpec(BaseModel):
    """Placeable group of mounting holes with fixed relative geometry.

    Issue #3352 (Q3 reframe): mounting holes form a rigid pattern that can be
    *placed* as a unit when auto-pcb-size escalation grows the board.  The
    group's anchor moves; the holes within it preserve their relative
    positions.  This replaces the older "refuse if any mounting hole present"
    behaviour with a richer primitive: layouts can grow as long as the hole
    group still fits within the escalated envelope at its declared anchor.

    The geometry conventions are:
      - ``holes`` are ``(x, y)`` positions in millimetres *relative to the
        group's local origin* (NOT board coordinates).
      - ``anchor`` is the position in *board coordinates* where the group's
        local origin sits.  The on-board hole position is therefore
        ``(anchor_x + hole_x, anchor_y + hole_y)``.
      - ``hole_diameter_mm`` applies uniformly to all holes in the group
        (M3 = 3.2 mm clearance is the default; matches softstart rev B).
      - ``keepout_radius_mm`` is the no-copper radius around each hole that
        the router treats as a soft obstacle.

    Consumed by the :class:`kicad_tools.pcb.mounting_holes.MountingHoleGroup`
    primitive, which handles the geometric placement / envelope-fit check.

    Attributes:
        holes: List of ``(x, y)`` hole positions in mm, relative to anchor.
        anchor: Default placement position ``(x, y)`` in mm, in board coords.
        hole_diameter_mm: Clearance hole diameter in mm (default 3.2 = M3).
        keepout_radius_mm: No-copper keepout radius in mm (default 5.0).
    """

    holes: list[tuple[float, float]] = Field(
        ...,
        description=(
            "List of (x, y) hole positions in mm, relative to the group's "
            "local origin.  Must contain at least one hole."
        ),
    )
    anchor: tuple[float, float] = Field(
        ...,
        description=(
            "Default placement position (x, y) in mm, in board coordinates. "
            "The auto-pcb-size escalation loop may move this anchor when "
            "the board envelope changes."
        ),
    )
    hole_diameter_mm: float = Field(
        default=3.2,
        description="Clearance hole diameter in mm (default 3.2 mm = M3 clearance).",
    )
    keepout_radius_mm: float = Field(
        default=5.0,
        description=(
            "No-copper keepout radius in mm around each hole.  Treated as a "
            "soft obstacle by the auto-router."
        ),
    )

    @field_validator("holes")
    @classmethod
    def validate_holes_nonempty(
        cls, v: list[tuple[float, float]]
    ) -> list[tuple[float, float]]:
        """Reject empty hole lists -- a group with zero holes is meaningless."""
        if not v:
            raise ValueError(
                "MountingHoleGroupSpec.holes must contain at least one hole; "
                "omit the field entirely if no mounting holes are required."
            )
        return v

    @field_validator("hole_diameter_mm", "keepout_radius_mm")
    @classmethod
    def validate_positive_dimensions(cls, v: float) -> float:
        """Hole diameter and keepout radius must be positive."""
        if v <= 0:
            raise ValueError(f"Dimension must be positive, got {v}")
        return v


class MechanicalRequirements(BaseModel):
    """Mechanical requirements and form factor."""

    dimensions: dict[str, str] | None = Field(
        default=None, description="Board dimensions (width, height, thickness)"
    )
    mounting_holes: list[MountingHole] | None = Field(
        default=None, description="Mounting hole positions"
    )
    mounting_hole_group: MountingHoleGroupSpec | None = Field(
        default=None,
        description=(
            "Issue #3352: Placeable mounting hole group with fixed relative "
            "geometry.  Preferred over the flat ``mounting_holes`` list when "
            "auto-pcb-size escalation is enabled; the group can be repositioned "
            "as a unit when the board envelope grows, while preserving the hole "
            "pattern.  When both fields are present, ``mounting_hole_group`` "
            "takes precedence for routing/escalation; ``mounting_holes`` is "
            "retained for backwards compatibility with non-grouped layouts."
        ),
    )
    keep_outs: list[KeepOut] | None = Field(default=None, description="Keep-out regions")
    edge_clearance: str | None = Field(
        default=None, description="Required clearance from board edge"
    )
    weight_max: str | None = Field(default=None, description="Maximum board weight")
    enclosure: str | None = Field(default=None, description="Target enclosure reference")
    envelope_hard: bool = Field(
        default=False,
        description=(
            "Issue #3352: When True, ``dimensions`` is a non-negotiable "
            "enclosure constraint and auto-pcb-size escalation refuses to "
            "grow the board, emitting an actionable error suggesting layer / "
            "clearance / BOM levers instead.  Default False -- dimensions "
            "are treated as initial sizing only and may be grown by "
            "escalation when routability requires it."
        ),
    )


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


_COPPER_WEIGHT_RE = re.compile(r"^\s*([\d.]+)\s*(?:oz)?\s*$", re.IGNORECASE)


def _parse_copper_weight_oz(value: int | float | str) -> float:
    """Parse a copper weight value to float oz.

    Accepts:
      - int or float (returned as-is, e.g. 2 -> 2.0)
      - str like '2oz', '0.5oz', '2 oz', '2OZ', or bare '2'

    Raises ValueError for unrecognised formats.
    """
    if isinstance(value, int | float):
        return float(value)
    if not isinstance(value, str):
        raise ValueError(
            f"Invalid copper_weight type: {type(value).__name__}. "
            "Expected a number or string like '2oz', '0.5oz', '1 oz'."
        )
    m = _COPPER_WEIGHT_RE.match(value)
    if m is None:
        raise ValueError(
            f"Invalid copper_weight: '{value}'. "
            "Expected a number or string like '2oz', '0.5oz', '1 oz'."
        )
    return float(m.group(1))


class EscalationPolicy(BaseModel):
    """Issue #3352: Routing escalation policy for the auto-* ladder.

    Declares the order in which the routing system will attempt to overcome
    over-constrained designs.  Each rung in the ladder corresponds to a
    different *axis* of slack (more layers, larger envelope, etc.) and the
    routing system will only escalate along axes the recipe permits.

    The ``ladder`` field selects between five composable strategies:

    - ``"layers-first"`` (default): exhaust the layer escalation ladder
      (2L -> 4L -> 6L) at the current envelope before trying size escalation.
      Cheapest at JLCPCB prototype quantities (qty 5) where layer adds are
      cheaper than envelope upgrades.
    - ``"size-first"``: walk the manufacturer's size-tier ladder before
      adding layers.  Preferred when the board's mechanical envelope is
      genuinely negotiable and the user values 2-layer cost optimization
      at production volume.
    - ``"layers-only"``: layer escalation only; refuse to grow the envelope.
      Equivalent to ``layers-first`` + ``envelope_hard=True`` but declared
      on the manufacturing side rather than the mechanical side.
    - ``"size-only"``: size escalation only; refuse to add layers.
      Useful when layer count is fixed by cost or stackup constraints.
    - ``"none"``: no escalation; refuse to escalate either axis.  The
      routing system reports the best-attempt result and lets the recipe
      author decide how to respond.

    The policy composes with :attr:`MechanicalRequirements.envelope_hard`:
    when the recipe declares the envelope as a hard mechanical constraint,
    any ``ladder`` value containing ``"size"`` falls back to layer-only
    behaviour (the size axis is implicitly disabled).

    The ``max_layers`` ceiling caps the layer escalation ladder.  The
    ``max_size_tier`` ceiling caps the size-tier escalation ladder by
    index into the manufacturer's :data:`MFR_SIZE_TIER_LADDERS` entry.
    A ``None`` ceiling means "use the manufacturer's maximum tier".

    The ``density_threshold_viols_per_cm2`` field is the DRC density
    trigger used by the size-escalation logic to discriminate "true
    envelope over-constraint" from "single hot-spot we can hand-fix".
    Hardcoded for now per Issue #3352 Q4 decision -- promote to a CLI
    flag if empirical evidence shows recipe-by-recipe tuning is needed.

    The ``starting_layers`` field is the lower rung of the layer
    escalation ladder.  Default 2 preserves the historical behaviour
    (probe 2L first, then 4L, then 6L).  Boards that have no realistic
    chance of routing at 2L can opt out of the 2L tax by declaring
    ``starting_layers=4`` (ladder becomes ``[4, 6]``).  Per Issue #3400,
    the field is bounded ``[2, 6]`` and must be ``<= max_layers``.

    Attributes:
        ladder: Escalation strategy selector.
        max_layers: Layer escalation ceiling (default 4 -- covers 2L, 4L).
        starting_layers: Layer escalation floor (default 2 -- start at 2L).
            Set to 4 to skip the 2L probe entirely (Issue #3400).
        max_size_tier: Size-tier ceiling (index into MFR_SIZE_TIER_LADDERS
            for the manufacturer; None = use manufacturer's max).
        density_threshold_viols_per_cm2: DRC violation density that
            triggers escalation (per Issue #3352 Q4: 0.5 viols/cm^2).
    """

    ladder: Literal[
        "layers-first",
        "size-first",
        "layers-only",
        "size-only",
        "none",
    ] = Field(
        default="layers-first",
        description=(
            "Escalation strategy.  See class docstring for the five options "
            "and their cost/policy implications."
        ),
    )
    max_layers: int = Field(
        default=4,
        description=(
            "Maximum layer count the escalation ladder may reach.  Default 4 "
            "covers the common 2L -> 4L transition; recipes that need 6L+ "
            "must opt in explicitly."
        ),
    )
    starting_layers: int = Field(
        default=2,
        ge=2,
        le=6,
        description=(
            "Lower rung of the layer escalation ladder (Issue #3400).  "
            "Default 2 preserves the historical 2L->4L->6L ladder.  Boards "
            "with no realistic chance of routing at 2L can opt out of the "
            "2L tax by declaring ``starting_layers=4`` (ladder becomes "
            "``[4, 6]``).  Must be <= max_layers."
        ),
    )
    max_size_tier: int | None = Field(
        default=None,
        description=(
            "Index into the manufacturer's size-tier ladder (see "
            ":data:`kicad_tools.router.mfr_limits.MFR_SIZE_TIER_LADDERS`) "
            "above which size escalation refuses.  ``None`` (default) means "
            "use the manufacturer's largest available tier."
        ),
    )
    density_threshold_viols_per_cm2: float = Field(
        default=0.5,
        description=(
            "DRC violation density (per cm^2 of board area) that triggers "
            "auto-pcb-size escalation.  Per Issue #3352 Q4: hardcoded at "
            "0.5 viols/cm^2 based on the softstart rev B 132/150 = 0.88 "
            "case.  Tunable here for forward compatibility; promote to a "
            "CLI flag if recipe-by-recipe tuning becomes necessary."
        ),
    )
    packing_overhead: float = Field(
        default=2.5,
        description=(
            "Issue #3403: heuristic packing-density multiplier used by "
            "the pre-route sum-of-clearances area estimator.  The estimator "
            "computes ``packing_overhead * (sum(footprint_area + "
            "clearance_perimeter) + routing_channel_estimate)`` -- the "
            "multiplier accounts for the geometric inefficiency of routing "
            "channels, vias, fillets, and component keepouts not modeled "
            "by the per-footprint terms.  Default 2.5 is a moderate "
            "prototype-density figure; bump to 3.0+ for tightly packed "
            "designs or down to 1.8 for very loose layouts.  Set to 0 to "
            "disable the pre-route check (reactive DRC-density backstop "
            "still applies).  Tunable per recipe via spec or per invocation "
            "via the ``--packing-overhead`` CLI flag."
        ),
    )

    @field_validator("max_layers")
    @classmethod
    def validate_max_layers(cls, v: int) -> int:
        """Layer count must be a positive even number (KiCad convention)."""
        if v < 1:
            raise ValueError(f"max_layers must be >= 1, got {v}")
        return v

    @field_validator("max_size_tier")
    @classmethod
    def validate_max_size_tier(cls, v: int | None) -> int | None:
        """Size-tier index must be non-negative when present."""
        if v is not None and v < 0:
            raise ValueError(f"max_size_tier must be >= 0, got {v}")
        return v

    @field_validator("density_threshold_viols_per_cm2")
    @classmethod
    def validate_density_threshold(cls, v: float) -> float:
        """Density threshold must be positive (negative makes no physical sense)."""
        if v <= 0:
            raise ValueError(
                f"density_threshold_viols_per_cm2 must be positive, got {v}"
            )
        return v

    @field_validator("packing_overhead")
    @classmethod
    def validate_packing_overhead(cls, v: float) -> float:
        """Packing overhead must be non-negative (0 disables the pre-route check)."""
        if v < 0:
            raise ValueError(f"packing_overhead must be >= 0, got {v}")
        return v

    @model_validator(mode="after")
    def validate_starting_layers_le_max(self) -> EscalationPolicy:
        """Issue #3400: starting_layers must not exceed max_layers."""
        if self.starting_layers > self.max_layers:
            raise ValueError(
                f"starting_layers ({self.starting_layers}) must be <= "
                f"max_layers ({self.max_layers}); a ladder cannot start "
                f"above its ceiling."
            )
        return self


class ManufacturingRequirements(BaseModel):
    """Manufacturing requirements and constraints."""

    layers: dict[str, int] | None = Field(
        default=None, description="Layer count (preferred, min, max)"
    )
    stackup: list[str] | None = Field(
        default=None,
        description=(
            "Optional human-readable description of each PCB layer "
            "(e.g., ['F.Cu (signal)', 'In1.Cu (GND plane)', ...]). "
            "Captured for documentation/audit; not consumed by the router."
        ),
    )
    copper_weight: float | None = Field(
        default=None,
        description="Copper weight in oz/ft^2 (e.g., 1, 2, '2oz', '0.5oz')",
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
    escalation: EscalationPolicy | None = Field(
        default=None,
        description=(
            "Issue #3352: Auto-routing escalation policy.  When unset (the "
            "default), no auto-escalation is performed -- the router uses "
            "the declared layer count / envelope and reports the best "
            "attempt.  When set, the router walks the declared ladder "
            "before reporting refusal."
        ),
    )

    @field_validator("copper_weight", mode="before")
    @classmethod
    def validate_copper_weight(cls, v: Any) -> float | None:
        """Parse copper weight from int, float, or oz-bearing string."""
        if v is None:
            return None
        return _parse_copper_weight_oz(v)

    @model_validator(mode="before")
    @classmethod
    def extract_copper_weight_from_layers(cls, data: Any) -> Any:
        """Promote copper_weight from layers dict to top-level field if nested."""
        if isinstance(data, dict):
            layers = data.get("layers")
            if isinstance(layers, dict) and "copper_weight" in layers:
                # Only promote if not already set at top level
                if "copper_weight" not in data or data["copper_weight"] is None:
                    data["copper_weight"] = layers.pop("copper_weight")
                else:
                    layers.pop("copper_weight")
        return data

    @model_validator(mode="before")
    @classmethod
    def extract_stackup_from_layers(cls, data: Any) -> Any:
        """Promote ``stackup`` from layers dict to top-level field if nested.

        Boards historically wrote::

            layers:
              preferred: 4
              stackup:
                - "F.Cu (signal)"
                - "In1.Cu (GND plane)"

        ``layers`` is typed ``dict[str, int]`` so a nested ``stackup`` list
        produces a Pydantic validation error. Promote it to the top-level
        ``stackup`` field for backwards compatibility.
        """
        if isinstance(data, dict):
            layers = data.get("layers")
            if isinstance(layers, dict) and "stackup" in layers:
                if "stackup" not in data or data["stackup"] is None:
                    data["stackup"] = layers.pop("stackup")
                else:
                    layers.pop("stackup")
        return data


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
    """Component suggestion with preferences and rationale.

    Accepts two equivalent YAML shapes for ergonomic authoring:

    * Full form (preserves rationale/avoid)::

          regulator:
            preferred: ["LM7805", "TPS562201"]
            rationale: "Common, well-documented regulators"

    * Shorthand (bare string -> ``preferred: [<string>]``)::

          regulator: "LM7805 5V LDO"

    The shorthand is unwrapped by :meth:`unwrap_string_shorthand` so the
    canonical model still has a list-of-strings ``preferred`` field.
    """

    preferred: list[str] | None = Field(default=None, description="Preferred part numbers")
    rationale: str | None = Field(default=None, description="Reason for preference")
    avoid: list[str] | None = Field(default=None, description="Parts to avoid")

    @model_validator(mode="before")
    @classmethod
    def unwrap_string_shorthand(cls, data: Any) -> Any:
        """Allow a bare string to stand in for ``{preferred: [<string>]}``.

        This is symmetric with how the loader is permissive about
        single-element lists elsewhere -- it keeps board authors from
        having to write the full mapping for the common case of a
        one-line component note.
        """
        if isinstance(data, str):
            return {"preferred": [data]}
        return data


class BOMSuggestions(BaseModel):
    """BOM-related suggestions."""

    preferred_vendors: list[str] | None = Field(
        default=None, description="Preferred component vendors"
    )
    lifecycle_min_years: int | None = Field(
        default=None, description="Minimum lifecycle requirement in years"
    )
    cost_target: str | None = Field(default=None, description="Target BOM cost")


class BOMEntry(BaseModel):
    """Per-reference BOM mapping for explicit part assignment.

    Allows the project spec to declare exact MPN and LCSC part numbers
    for specific reference designators, overriding whatever the schematic
    symbol properties may (or may not) contain.

    The ``ref`` field accepts single references (``"U1"``) or dash-separated
    ranges (``"Q1-Q4"``) which are expanded into individual designators.
    """

    ref: str = Field(..., description="Reference designator or range (e.g. 'U1' or 'Q1-Q4')")
    part: str = Field(..., description="Manufacturer Part Number (MPN)")
    source: str = Field(default="LCSC", description="Supplier name (e.g. 'LCSC', 'Digikey')")
    lcsc: str | None = Field(default=None, description="Explicit LCSC part number if known")


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
    bom_entries: list[BOMEntry] | None = Field(
        default=None,
        description="Per-reference BOM part mappings (MPN, LCSC)",
    )
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
