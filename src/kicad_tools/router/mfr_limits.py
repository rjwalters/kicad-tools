"""
Manufacturer design rule limits and adaptive relaxation tiers.

This module provides:
- MfrLimits: Minimum design rules for various PCB manufacturers
- RelaxationTier: A single relaxation tier configuration
- ManufacturerSizeTier: A size/price tier in a manufacturer's cost ladder
- get_relaxation_tiers(): Generate relaxation tiers from user rules to mfr limits
- get_mfr_size_tier_ladder(): Get the cost-tier ladder for a manufacturer

Supported Manufacturers:
- JLCPCB: Chinese low-cost PCB manufacturer
- Seeed (Seeed Fusion): Uses JLCPCB-compatible manufacturing rules
- OSHPark: US-based high-quality purple boards
- PCBWay: Chinese manufacturer with good middle-ground specs
"""

from dataclasses import dataclass
from difflib import get_close_matches


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
        min_edge_clearance: Minimum copper-to-board-edge clearance
        min_hole_to_hole: Minimum drill-to-drill (hole-to-hole) edge-to-edge
            spacing in mm.  Distinct from ``min_clearance`` (copper trace/
            space) -- this is the fab's drill-pitch floor, canonically
            0.5mm.  Used by the router's via-placement guards (diff-pair
            fan-out, escape, stitching) to reject candidates that would
            emit a sub-fab-minimum drill pair, matching the validate-side
            ``DesignRules.min_hole_to_hole_mm`` (#3846) and the DRC
            ``hole_to_hole_clearance`` rule (#3842).
        via_in_pad_supported: Whether the manufacturer supports via-in-pad
            (vias drilled directly inside SMD pads, requiring filled and
            plated-over via processing).  Default ``False`` (conservative).
            When ``True``, the escape router may place vias dead-centre on
            fine-pitch SSOP/TSSOP pads to escape into an inner layer
            instead of deferring those pins to the main router.
        cost_note: Optional human-readable note about cost implications of
            choosing this tier (e.g., "Capability Plus surcharge ~$30/order").
            Surfaced verbatim by ``--auto-mfr-tier`` escalation when this
            tier is chosen over a cheaper base tier (Issue #2881).
    """

    name: str
    min_trace: float
    min_clearance: float
    min_via_drill: float
    min_via_annular: float
    min_edge_clearance: float = 0.0
    min_hole_to_hole: float = 0.5
    via_in_pad_supported: bool = False
    cost_note: str | None = None

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
    min_edge_clearance=0.3,  # 0.3mm copper-to-edge (matches .kicad_dru files)
    via_in_pad_supported=False,  # plain JLCPCB does not include via-in-pad
)

# JLCPCB Capability+ / tier 1 process supports via-in-pad with epoxy fill +
# plating over (typical surcharge ~$30/order).  Users must opt in
# explicitly to get this behavior in the router.
MFR_JLCPCB_TIER1 = MfrLimits(
    name="jlcpcb-tier1",
    min_trace=0.127,  # 5 mil
    min_clearance=0.127,  # 5 mil
    min_via_drill=0.3,
    min_via_annular=0.15,
    min_edge_clearance=0.3,
    via_in_pad_supported=True,
    cost_note="Capability Plus surcharge ~$30/order over base jlcpcb",
)

MFR_OSHPARK = MfrLimits(
    name="oshpark",
    min_trace=0.152,  # 6 mil
    min_clearance=0.152,  # 6 mil
    min_via_drill=0.254,  # 10 mil
    min_via_annular=0.127,  # 5 mil
    min_edge_clearance=0.381,  # 15 mil copper-to-edge (matches .kicad_dru files)
    via_in_pad_supported=False,  # not a standard OSHPark offering
)

MFR_PCBWAY = MfrLimits(
    name="pcbway",
    min_trace=0.127,  # 5 mil (standard process)
    min_clearance=0.127,  # 5 mil
    min_via_drill=0.2,  # 8 mil (can go smaller for extra cost)
    min_via_annular=0.15,  # 6 mil
    min_edge_clearance=0.25,  # 0.25mm copper-to-edge (matches .kicad_dru files)
    via_in_pad_supported=True,  # PCBWay offers via-in-pad as a standard option
)

# Mapping of manufacturer names to their limits
MFR_LIMITS: dict[str, MfrLimits] = {
    "jlcpcb": MFR_JLCPCB,
    "jlcpcb-tier1": MFR_JLCPCB_TIER1,
    "seeed": MFR_JLCPCB,  # Seeed Fusion uses JLCPCB-compatible rules
    "seeed-fusion": MFR_JLCPCB,
    "oshpark": MFR_OSHPARK,
    "pcbway": MFR_PCBWAY,
}

# Aliases mapping alternative names to canonical MFR_LIMITS keys
_MFR_ALIASES: dict[str, str] = {
    "seeed_fusion": "seeed-fusion",
    "seeedfusion": "seeed-fusion",
    "seeedstudio": "seeed",
    "jlcpcb-capabilityplus": "jlcpcb-tier1",
    "jlcpcb_capabilityplus": "jlcpcb-tier1",
    "jlcpcb-capability-plus": "jlcpcb-tier1",
    "jlcpcb_tier1": "jlcpcb-tier1",
}

# Issue #2881: Manufacturer tier-escalation ladders.
#
# Maps a base manufacturer name to the ordered ladder of tiers to attempt when
# ``--auto-mfr-tier`` is engaged.  The ladder runs cheapest -> tightest; the
# escalation loop walks the ladder from the user's current tier onward, trying
# each subsequent tier when geometric infeasibility is detected on the current
# one (e.g. fine-pitch QFP escape blocked because base jlcpcb lacks
# via-in-pad).
#
# Single-tier families (pcbway, oshpark) have a single entry -- escalation is
# a no-op for them today, but they still participate in the registry so the
# CLI can report "no escalation available for this manufacturer family"
# without special-casing.
#
# The architecture accepts additional tiers (e.g. a future ``pcbway-tier1``)
# without further code changes: add the tier to ``MFR_LIMITS`` and extend the
# relevant ladder here.
MFR_TIER_LADDERS: dict[str, list[str]] = {
    "jlcpcb": ["jlcpcb", "jlcpcb-tier1"],
    "jlcpcb-tier1": ["jlcpcb-tier1"],  # already at the top
    "seeed": ["seeed", "jlcpcb-tier1"],
    "seeed-fusion": ["seeed-fusion", "jlcpcb-tier1"],
    "pcbway": ["pcbway"],  # single-tier today
    "oshpark": ["oshpark"],  # single-tier today
}


# Issue #3352: Manufacturer cost-tier (size) ladders for auto-pcb-size escalation.
#
# Distinct axis from MFR_TIER_LADDERS (which is the capability ladder).  A size
# tier maps an envelope (max W x H in mm) to a price point at a reference
# quantity, so the auto-pcb-size escalation loop can choose the cheapest
# envelope that still admits the routed board.
#
# Each ManufacturerSizeTier carries pricing for both 2-layer and 4-layer
# variants at the reference quantity so the cost comparison between layer
# escalation (2L->4L same size) and size escalation (one tier up same layers)
# can be made on a single common basis.  The 4L price field is also useful
# for the `auto-layers + auto-pcb-size` matrix ladder in P_AS4.
#
# Reference quantity: 5 boards (the JLCPCB minimum order; matches the
# prototype regime architects target when picking layers-first as the
# default ladder).
@dataclass(frozen=True)
class ManufacturerSizeTier:
    """A single envelope-and-price rung in a manufacturer's cost ladder.

    All dimensions are in millimetres; all prices are in USD at the reference
    quantity (5 boards for JLCPCB).  ``max_width_mm`` and ``max_height_mm``
    are the envelope's hard ceiling -- a board that exceeds either dimension
    no longer fits this tier and must escalate to the next rung.

    Attributes:
        max_width_mm: Maximum board width (mm) admitted by this tier.
        max_height_mm: Maximum board height (mm) admitted by this tier.
        price_2l_usd: Reference-quantity price for a 2-layer board (USD).
        price_4l_usd: Reference-quantity price for a 4-layer board (USD).
        note: Optional human-readable note (cost-bracket name, caveats, etc.).
    """

    max_width_mm: float
    max_height_mm: float
    price_2l_usd: float
    price_4l_usd: float
    note: str = ""

    @property
    def area_cm2(self) -> float:
        """Convenience: tier envelope area in cm^2 (used by area-ascending sort)."""
        return (self.max_width_mm * self.max_height_mm) / 100.0


# JLCPCB cost-tier ladder (auto-pcb-size escalation).
#
# Source: JLCPCB instant quote calculator at https://cart.jlcpcb.com/quote
# Verified: 2026-06-08
#
# Methodology notes:
#   - JLCPCB's price calculator is a server-rendered SPA whose API requires
#     an authenticated cart session; static scraping is infeasible.  The
#     tier prices below were gathered from order history maintained by the
#     project owner (rjwalters), cross-checked against the JLCPCB
#     instant-quote calculator on the verified date, at reference qty=5.
#   - Pricing covers HASL finish, standard FR-4, 1.6 mm thickness, 1 oz
#     copper, green soldermask, white silkscreen (the JLCPCB defaults).
#   - "FREE green" promo applies at the 100x100 base tier (qty 5).
#   - 4-layer pricing is the calculator's default 4L offering, not
#     impedance-controlled variants.
#   - Prices drift quarterly; consumers should treat these as ordinal
#     (which-tier-is-cheapest) rather than absolute.  Re-verify against
#     the calculator before relying on absolute cost deltas in cost-aware
#     escalation decisions (P_AS4 layers-first/size-first selector).
#
# Tier ordering is by ascending envelope area; consumers iterate this list
# in order to find the smallest tier that admits a given board.
MFR_JLCPCB_SIZE_TIERS: list[ManufacturerSizeTier] = [
    ManufacturerSizeTier(
        max_width_mm=100.0,
        max_height_mm=100.0,
        price_2l_usd=2.0,
        price_4l_usd=5.0,
        note="Base bracket (FREE green promo, qty 5)",
    ),
    ManufacturerSizeTier(
        max_width_mm=100.0,
        max_height_mm=150.0,
        price_2l_usd=5.0,
        price_4l_usd=15.0,
        note="One-axis stretch from base bracket",
    ),
    ManufacturerSizeTier(
        max_width_mm=150.0,
        max_height_mm=150.0,
        price_2l_usd=8.0,
        price_4l_usd=25.0,
        note="Common square mid-tier",
    ),
    ManufacturerSizeTier(
        max_width_mm=150.0,
        max_height_mm=200.0,
        price_2l_usd=12.0,
        price_4l_usd=35.0,
        note="Most common large bracket (softstart envelope)",
    ),
    ManufacturerSizeTier(
        max_width_mm=200.0,
        max_height_mm=200.0,
        price_2l_usd=20.0,
        price_4l_usd=55.0,
        note="Diminishing returns rung",
    ),
    ManufacturerSizeTier(
        max_width_mm=200.0,
        max_height_mm=300.0,
        price_2l_usd=32.0,
        price_4l_usd=85.0,
        note="Top of escalation ladder (qty-5 prototypes)",
    ),
]

# Per-manufacturer size-tier ladders.  Single-source manufacturers (oshpark,
# pcbway) currently inherit the JLCPCB ladder as a placeholder; their
# empirical pricing differs but the *envelope* tiers are similar enough that
# the auto-pcb-size escalation logic doesn't need separate ladders yet.
# Replace with manufacturer-specific tables when escalation needs to
# discriminate on absolute price between manufacturers.
MFR_SIZE_TIER_LADDERS: dict[str, list[ManufacturerSizeTier]] = {
    "jlcpcb": MFR_JLCPCB_SIZE_TIERS,
    "jlcpcb-tier1": MFR_JLCPCB_SIZE_TIERS,
    "seeed": MFR_JLCPCB_SIZE_TIERS,  # Seeed Fusion uses JLCPCB-compatible rules
    "seeed-fusion": MFR_JLCPCB_SIZE_TIERS,
    # pcbway / oshpark: no empirical size-tier table yet; default to JLCPCB
    # ladder so the escalation loop has *some* ordering to walk.  Replace
    # with manufacturer-specific tiers when cost-aware decisions matter.
    "pcbway": MFR_JLCPCB_SIZE_TIERS,
    "oshpark": MFR_JLCPCB_SIZE_TIERS,
}


def get_mfr_size_tier_ladder(manufacturer: str) -> list[ManufacturerSizeTier]:
    """Get the cost-tier (size) ladder for a manufacturer.

    Returns the ordered list of :class:`ManufacturerSizeTier` rungs, sorted by
    ascending envelope area.  Used by the auto-pcb-size escalation loop to walk
    from the user's current envelope toward the next admissible tier.

    Args:
        manufacturer: Manufacturer name (case-insensitive; aliases resolved).

    Returns:
        Ordered list of size tiers (ascending area).  For manufacturers
        without an empirical table, returns the JLCPCB ladder as a fallback
        (see :data:`MFR_SIZE_TIER_LADDERS`).

    Raises:
        ValueError: If ``manufacturer`` is not a recognized manufacturer.

    Example:
        >>> tiers = get_mfr_size_tier_ladder("jlcpcb")
        >>> tiers[0].max_width_mm, tiers[0].max_height_mm
        (100.0, 100.0)
        >>> tiers[0].price_2l_usd
        2.0
    """
    mfr_lower = manufacturer.lower()
    canonical = _MFR_ALIASES.get(mfr_lower, mfr_lower)

    if canonical not in MFR_LIMITS:
        # Validate the manufacturer is real (raises ValueError with suggestions)
        get_mfr_limits(manufacturer)
        return list(MFR_JLCPCB_SIZE_TIERS)  # unreachable; defensive fallthrough

    return list(MFR_SIZE_TIER_LADDERS.get(canonical, MFR_JLCPCB_SIZE_TIERS))


def find_smallest_admitting_tier(
    width_mm: float,
    height_mm: float,
    manufacturer: str = "jlcpcb",
) -> ManufacturerSizeTier | None:
    """Find the smallest size tier that admits a board of the given dimensions.

    Used by the auto-pcb-size escalation loop to discover the user's current
    rung in the cost ladder.  Returns ``None`` when the board exceeds the
    largest tier (manufacturing refusal case).

    Args:
        width_mm: Board width in mm.
        height_mm: Board height in mm.
        manufacturer: Manufacturer name (case-insensitive; aliases resolved).

    Returns:
        The smallest :class:`ManufacturerSizeTier` whose envelope admits the
        board (max_width >= width AND max_height >= height), considering both
        orientations (the board may be rotated 90 deg into a tier with swapped
        axis).  Returns ``None`` when no tier admits the board.

    Example:
        >>> tier = find_smallest_admitting_tier(80, 80)
        >>> tier.max_width_mm, tier.max_height_mm
        (100.0, 100.0)
        >>> tier = find_smallest_admitting_tier(120, 80)
        >>> tier.max_width_mm, tier.max_height_mm
        (100.0, 150.0)
    """
    ladder = get_mfr_size_tier_ladder(manufacturer)
    for tier in ladder:
        # Tier admits the board if it fits in either orientation
        fits_natural = width_mm <= tier.max_width_mm and height_mm <= tier.max_height_mm
        fits_rotated = width_mm <= tier.max_height_mm and height_mm <= tier.max_width_mm
        if fits_natural or fits_rotated:
            return tier
    return None


def get_mfr_tier_ladder(manufacturer: str) -> list[str]:
    """Get the escalation ladder for a manufacturer.

    Returns the ordered list of manufacturer tier names to attempt when
    ``--auto-mfr-tier`` escalation is engaged.  The first entry is always
    the input manufacturer (canonicalized through aliases); subsequent
    entries are tighter tiers in escalation order.

    Args:
        manufacturer: Base manufacturer name (case-insensitive; aliases
            are resolved via :data:`_MFR_ALIASES`).

    Returns:
        Ordered list of manufacturer tier names, starting with the input
        manufacturer.  When no ladder is registered for the manufacturer
        family, returns ``[canonical_name]`` (single-element ladder ==
        no escalation available).

    Raises:
        ValueError: If ``manufacturer`` is not a recognized manufacturer.

    Example:
        >>> get_mfr_tier_ladder("jlcpcb")
        ['jlcpcb', 'jlcpcb-tier1']
        >>> get_mfr_tier_ladder("oshpark")
        ['oshpark']
        >>> get_mfr_tier_ladder("JLCPCB")  # case-insensitive
        ['jlcpcb', 'jlcpcb-tier1']
    """
    mfr_lower = manufacturer.lower()
    canonical = _MFR_ALIASES.get(mfr_lower, mfr_lower)

    if canonical not in MFR_LIMITS:
        # Validate the manufacturer is real so callers get a clear error
        # rather than a silent "no escalation" fallback.
        get_mfr_limits(manufacturer)  # raises ValueError with suggestions
        return [canonical]  # unreachable; defensive fallthrough

    ladder = MFR_TIER_LADDERS.get(canonical)
    if ladder is None:
        return [canonical]
    return list(ladder)


def can_escalate_via_in_pad(current_mfr: str, next_mfr: str) -> bool:
    """True iff escalating from current_mfr to next_mfr gains via-in-pad.

    Used by the auto-mfr-tier escalation loop as the canonical convergence
    guard: when the next tier in the ladder offers no via-in-pad gain AND
    no scalar relaxation, escalating is a no-op and should be skipped.

    Args:
        current_mfr: Current manufacturer tier name.
        next_mfr: Candidate next-tier manufacturer name.

    Returns:
        True when ``next_mfr`` supports via-in-pad and ``current_mfr``
        does not.  Both manufacturers must be in :data:`MFR_LIMITS`.
    """
    try:
        cur = get_mfr_limits(current_mfr)
        nxt = get_mfr_limits(next_mfr)
    except ValueError:
        return False
    return nxt.via_in_pad_supported and not cur.via_in_pad_supported


def can_escalate_scalar(current_mfr: str, next_mfr: str) -> bool:
    """True iff escalating from current_mfr to next_mfr relaxes scalar limits.

    Returns True when ``next_mfr`` offers a strictly smaller min_clearance OR
    min_trace OR min_via_drill compared to ``current_mfr``.  Used alongside
    :func:`can_escalate_via_in_pad` to determine whether escalation can ever
    help with the current failure mode.

    Args:
        current_mfr: Current manufacturer tier name.
        next_mfr: Candidate next-tier manufacturer name.

    Returns:
        True when ``next_mfr`` has tighter scalar capability than
        ``current_mfr``.  Both manufacturers must be in :data:`MFR_LIMITS`.
    """
    try:
        cur = get_mfr_limits(current_mfr)
        nxt = get_mfr_limits(next_mfr)
    except ValueError:
        return False
    return (
        nxt.min_clearance < cur.min_clearance
        or nxt.min_trace < cur.min_trace
        or nxt.min_via_drill < cur.min_via_drill
    )


def get_mfr_limits(manufacturer: str) -> MfrLimits:
    """Get manufacturer limits by name.

    Supports aliases (e.g., "seeed_fusion" -> "seeed-fusion") and
    case-insensitive lookup. On unknown manufacturer, suggests close
    matches via difflib.

    Args:
        manufacturer: Manufacturer name (case-insensitive)

    Returns:
        MfrLimits for the specified manufacturer

    Raises:
        ValueError: If manufacturer is not recognized
    """
    mfr_lower = manufacturer.lower()

    # Resolve aliases
    mfr_lower = _MFR_ALIASES.get(mfr_lower, mfr_lower)

    if mfr_lower in MFR_LIMITS:
        return MFR_LIMITS[mfr_lower]

    # Build error message with closest-match suggestions
    all_names = sorted(set(MFR_LIMITS.keys()) | set(_MFR_ALIASES.keys()))
    suggestions = get_close_matches(mfr_lower, all_names, n=3, cutoff=0.5)
    msg = f"Unknown manufacturer '{manufacturer}'. Valid options: {', '.join(sorted(MFR_LIMITS.keys()))}"
    if suggestions:
        msg += f". Did you mean: {', '.join(suggestions)}?"
    raise ValueError(msg)


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
