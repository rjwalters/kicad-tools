"""
Manufacturer design rule limits and adaptive relaxation tiers.

This module provides:
- MfrLimits: Minimum design rules for various PCB manufacturers
- RelaxationTier: A single relaxation tier configuration
- get_relaxation_tiers(): Generate relaxation tiers from user rules to mfr limits

Supported Manufacturers:
- JLCPCB: Chinese low-cost PCB manufacturer
- OSHPark: US-based high-quality purple boards
- PCBWay: Chinese manufacturer with good middle-ground specs
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class MfrLimits:
    """Minimum design rule limits for a PCB manufacturer.

    All measurements are in millimeters.

    Attributes:
        name: Manufacturer name (e.g., "jlcpcb")
        min_trace: Minimum trace width
        min_clearance: Minimum trace-to-trace clearance
        min_via_drill: Minimum via drill diameter
        min_via_annular: Minimum via annular ring width
        min_via_diameter: Computed minimum via diameter (drill + 2*annular)
    """

    name: str
    min_trace: float
    min_clearance: float
    min_via_drill: float
    min_via_annular: float

    @property
    def min_via_diameter(self) -> float:
        """Minimum via diameter (drill + 2 * annular ring)."""
        return self.min_via_drill + 2 * self.min_via_annular


# Well-known manufacturer limits
# Note: These are capability limits, not recommendations.
# Production yield is better with larger values.

MFR_JLCPCB = MfrLimits(
    name="jlcpcb",
    min_trace=0.127,  # 5 mil
    min_clearance=0.127,  # 5 mil
    min_via_drill=0.3,  # 0.3mm is standard, 0.2mm costs extra
    min_via_annular=0.15,  # 6 mil annular ring
)

MFR_OSHPARK = MfrLimits(
    name="oshpark",
    min_trace=0.152,  # 6 mil
    min_clearance=0.152,  # 6 mil
    min_via_drill=0.254,  # 10 mil
    min_via_annular=0.127,  # 5 mil
)

MFR_PCBWAY = MfrLimits(
    name="pcbway",
    min_trace=0.127,  # 5 mil (standard process)
    min_clearance=0.127,  # 5 mil
    min_via_drill=0.2,  # 8 mil (can go smaller for extra cost)
    min_via_annular=0.15,  # 6 mil
)

# Mapping of manufacturer names to their limits
MFR_LIMITS: dict[str, MfrLimits] = {
    "jlcpcb": MFR_JLCPCB,
    "oshpark": MFR_OSHPARK,
    "pcbway": MFR_PCBWAY,
}


def get_mfr_limits(manufacturer: str) -> MfrLimits:
    """Get manufacturer limits by name.

    Args:
        manufacturer: Manufacturer name (case-insensitive)

    Returns:
        MfrLimits for the specified manufacturer

    Raises:
        ValueError: If manufacturer is not recognized
    """
    mfr_lower = manufacturer.lower()
    if mfr_lower not in MFR_LIMITS:
        valid_mfrs = ", ".join(sorted(MFR_LIMITS.keys()))
        raise ValueError(f"Unknown manufacturer '{manufacturer}'. Valid options: {valid_mfrs}")
    return MFR_LIMITS[mfr_lower]


@dataclass
class RelaxationTier:
    """A single design rule relaxation tier.

    Attributes:
        tier: Tier number (0 = strictest/user-specified)
        trace_width: Trace width in mm
        clearance: Trace clearance in mm
        via_drill: Via drill diameter in mm
        via_diameter: Via pad diameter in mm
        description: Human-readable description of this tier
    """

    tier: int
    trace_width: float
    clearance: float
    via_drill: float
    via_diameter: float
    description: str

    def __str__(self) -> str:
        """Format tier as a summary string."""
        return (
            f"Tier {self.tier}: trace={self.trace_width:.3f}mm, "
            f"clearance={self.clearance:.3f}mm ({self.description})"
        )


def get_relaxation_tiers(
    initial_trace_width: float,
    initial_clearance: float,
    initial_via_drill: float,
    initial_via_diameter: float,
    manufacturer: str = "jlcpcb",
    min_trace_floor: float | None = None,
    min_clearance_floor: float | None = None,
    num_tiers: int = 4,
) -> list[RelaxationTier]:
    """Generate relaxation tiers from user rules down to manufacturer limits.

    Creates a series of progressively relaxed design rules, starting from the
    user-specified values and ending at the manufacturer minimum capabilities.

    Args:
        initial_trace_width: User-specified trace width (mm)
        initial_clearance: User-specified clearance (mm)
        initial_via_drill: User-specified via drill (mm)
        initial_via_diameter: User-specified via diameter (mm)
        manufacturer: Manufacturer name for limits (default: "jlcpcb")
        min_trace_floor: Minimum trace width floor (overrides mfr limit)
        min_clearance_floor: Minimum clearance floor (overrides mfr limit)
        num_tiers: Number of relaxation tiers to generate (default: 4)

    Returns:
        List of RelaxationTier objects from strictest (tier 0) to most relaxed

    Example:
        >>> tiers = get_relaxation_tiers(
        ...     initial_trace_width=0.2,
        ...     initial_clearance=0.4,
        ...     initial_via_drill=0.3,
        ...     initial_via_diameter=0.6,
        ...     manufacturer="jlcpcb",
        ... )
        >>> for tier in tiers:
        ...     print(tier)
        Tier 0: trace=0.200mm, clearance=0.400mm (User-specified)
        Tier 1: trace=0.175mm, clearance=0.309mm (Moderate relaxation)
        Tier 2: trace=0.150mm, clearance=0.218mm (Aggressive relaxation)
        Tier 3: trace=0.127mm, clearance=0.127mm (JLCPCB minimum)
    """
    mfr = get_mfr_limits(manufacturer)

    # Determine final minimum values (user floor or mfr limit)
    min_trace = max(min_trace_floor or 0, mfr.min_trace)
    min_clearance = max(min_clearance_floor or 0, mfr.min_clearance)
    min_via_drill = mfr.min_via_drill
    min_via_diameter = mfr.min_via_diameter

    # If user values are already at or below minimum, return single tier
    if initial_trace_width <= min_trace and initial_clearance <= min_clearance:
        return [
            RelaxationTier(
                tier=0,
                trace_width=initial_trace_width,
                clearance=initial_clearance,
                via_drill=initial_via_drill,
                via_diameter=initial_via_diameter,
                description="User-specified (at minimum)",
            )
        ]

    tiers: list[RelaxationTier] = []

    # Generate tiers with linear interpolation
    for i in range(num_tiers):
        # Progress from 0.0 (initial) to 1.0 (minimum)
        t = i / (num_tiers - 1) if num_tiers > 1 else 0

        # Interpolate values (can't go below minimum)
        trace = max(min_trace, initial_trace_width - t * (initial_trace_width - min_trace))
        clearance = max(min_clearance, initial_clearance - t * (initial_clearance - min_clearance))
        via_drill = max(min_via_drill, initial_via_drill - t * (initial_via_drill - min_via_drill))
        via_diam = max(
            min_via_diameter, initial_via_diameter - t * (initial_via_diameter - min_via_diameter)
        )

        # Determine description
        if i == 0:
            desc = "User-specified"
        elif i == num_tiers - 1:
            desc = f"{mfr.name.upper()} minimum"
        elif i == 1:
            desc = "Moderate relaxation"
        else:
            desc = "Aggressive relaxation"

        tiers.append(
            RelaxationTier(
                tier=i,
                trace_width=trace,
                clearance=clearance,
                via_drill=via_drill,
                via_diameter=via_diam,
                description=desc,
            )
        )

    return tiers
