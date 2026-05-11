"""DRC violation data structures."""

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any, Optional

from kicad_tools.core.types import Severity

if TYPE_CHECKING:
    from kicad_tools.drc.suggestions import FixSuggestion
    from kicad_tools.exceptions import SourcePosition


class ViolationCategory(Enum):
    """Root-cause category for DRC violations.

    Helps agents distinguish actionable errors from inherent IC behavior:
    - PLACEMENT: inherent to component placement (pad spacing, courtyard)
    - ROUTING: fixable by rerouting (trace clearance, via placement)
    - MANUFACTURING: depends on fab house capabilities (mask dam, drill)
    - CONNECTIVITY: netlist issues (shorts, unconnected)
    - COSMETIC: visual only (silk overlap)
    """

    PLACEMENT = "placement"
    ROUTING = "routing"
    MANUFACTURING = "manufacturing"
    CONNECTIVITY = "connectivity"
    COSMETIC = "cosmetic"


# Mapping from ViolationType to default ViolationCategory
_TYPE_CATEGORY_MAP: dict["ViolationType", ViolationCategory] = {}


def _init_type_category_map() -> None:
    """Populate the type-to-category mapping after ViolationType is defined."""
    _TYPE_CATEGORY_MAP.update(
        {
            # Routing: fixable by rerouting traces/vias
            ViolationType.CLEARANCE: ViolationCategory.ROUTING,
            ViolationType.CLEARANCE_SEGMENT_VIA: ViolationCategory.ROUTING,
            ViolationType.CLEARANCE_PAD_SEGMENT: ViolationCategory.ROUTING,
            ViolationType.CLEARANCE_PAD_VIA: ViolationCategory.ROUTING,
            ViolationType.CLEARANCE_SEGMENT_SEGMENT: ViolationCategory.ROUTING,
            ViolationType.CLEARANCE_VIA_VIA: ViolationCategory.ROUTING,
            # Differential-pair within-pair clearance (Issue #2560, Epic #2556 Phase 1D).
            # Same category as other clearance subtypes -- fixable by rerouting.
            ViolationType.DIFFPAIR_CLEARANCE_INTRA: ViolationCategory.ROUTING,
            # Differential-pair routing continuity (Issue #2640, Epic #2556 Phase 2G).
            # Fixable by adjusting the route so the two halves stay coupled
            # for a larger share of their length -- pure routing concern.
            ViolationType.DIFFPAIR_ROUTING_CONTINUITY: ViolationCategory.ROUTING,
            # Differential-pair length-skew (Issue #2649, Epic #2556 Phase 3J).
            # Fixable by routing-side length tuning (serpentine insertion in
            # Phase 3I or manual touch-up) -- the skew is a function of the
            # routed geometry of the two halves, not their footprints.
            ViolationType.DIFFPAIR_LENGTH_SKEW: ViolationCategory.ROUTING,
            # Match-group (N-trace) length-skew (Issue #2702, Epic #2661 Phase 2G).
            # N>=3 generalization of DIFFPAIR_LENGTH_SKEW: same routing-
            # fixable nature (serpentine tuning of the shorter members up
            # to the longest, Phase 2E's domain), categorized identically.
            ViolationType.MATCH_GROUP_LENGTH_SKEW: ViolationCategory.ROUTING,
            ViolationType.TRACK_WIDTH: ViolationCategory.ROUTING,
            ViolationType.TRACK_ANGLE: ViolationCategory.ROUTING,
            ViolationType.DIMENSION_TRACE_WIDTH: ViolationCategory.ROUTING,
            ViolationType.DIMENSION_DRILL_CLEARANCE: ViolationCategory.ROUTING,
            # Placement: inherent to component placement
            ViolationType.CLEARANCE_PAD_PAD: ViolationCategory.PLACEMENT,
            ViolationType.COURTYARD_OVERLAP: ViolationCategory.PLACEMENT,
            ViolationType.SOLDER_MASK_BRIDGE: ViolationCategory.PLACEMENT,
            # Manufacturing: depends on fab capabilities
            ViolationType.COPPER_EDGE_CLEARANCE: ViolationCategory.MANUFACTURING,
            ViolationType.EDGE_CLEARANCE_TRACE: ViolationCategory.MANUFACTURING,
            ViolationType.EDGE_CLEARANCE_PAD: ViolationCategory.MANUFACTURING,
            ViolationType.EDGE_CLEARANCE_PAD_HOLE: ViolationCategory.MANUFACTURING,
            ViolationType.EDGE_CLEARANCE_VIA: ViolationCategory.MANUFACTURING,
            ViolationType.EDGE_CLEARANCE_ZONE: ViolationCategory.MANUFACTURING,
            ViolationType.VIA_HOLE_LARGER_THAN_PAD: ViolationCategory.MANUFACTURING,
            ViolationType.VIA_ANNULAR_WIDTH: ViolationCategory.MANUFACTURING,
            ViolationType.MICRO_VIA_HOLE_TOO_SMALL: ViolationCategory.MANUFACTURING,
            # Via-in-pad is a manufacturer-capability question (filled and
            # plated-over via processing) -- categorize with the other
            # via/fab capabilities.
            ViolationType.VIA_IN_PAD: ViolationCategory.MANUFACTURING,
            ViolationType.DRILL_HOLE_TOO_SMALL: ViolationCategory.MANUFACTURING,
            ViolationType.DRILL_CLEARANCE: ViolationCategory.MANUFACTURING,
            ViolationType.NPTH_HOLE_TOO_SMALL: ViolationCategory.MANUFACTURING,
            ViolationType.HOLE_NEAR_HOLE: ViolationCategory.MANUFACTURING,
            ViolationType.DIMENSION_VIA_DRILL: ViolationCategory.MANUFACTURING,
            ViolationType.DIMENSION_VIA_DIAMETER: ViolationCategory.MANUFACTURING,
            ViolationType.DIMENSION_ANNULAR_RING: ViolationCategory.MANUFACTURING,
            ViolationType.SOLDER_MASK_CLEARANCE: ViolationCategory.MANUFACTURING,
            ViolationType.MIN_PAD_SIZE: ViolationCategory.MANUFACTURING,
            ViolationType.PTH_ANNULAR_RING: ViolationCategory.MANUFACTURING,
            ViolationType.IMPEDANCE: ViolationCategory.MANUFACTURING,
            # Connectivity: netlist issues
            ViolationType.UNCONNECTED_ITEMS: ViolationCategory.CONNECTIVITY,
            ViolationType.SHORTING_ITEMS: ViolationCategory.CONNECTIVITY,
            ViolationType.NET_UNDECLARED: ViolationCategory.CONNECTIVITY,
            ViolationType.SINGLE_PAD_NET: ViolationCategory.CONNECTIVITY,
            # Cosmetic: visual only
            ViolationType.SILK_OVER_COPPER: ViolationCategory.COSMETIC,
            ViolationType.SILK_OVERLAP: ViolationCategory.COSMETIC,
            ViolationType.SILKSCREEN_LINE_WIDTH: ViolationCategory.COSMETIC,
            ViolationType.SILKSCREEN_TEXT_HEIGHT: ViolationCategory.COSMETIC,
            ViolationType.SILKSCREEN_OVER_PAD: ViolationCategory.COSMETIC,
            # Zone fill: connectivity issues
            ViolationType.ZONE_UNFILLED: ViolationCategory.CONNECTIVITY,
            ViolationType.ZONE_FILL_DISABLED: ViolationCategory.CONNECTIVITY,
            ViolationType.ZONE_NO_NET: ViolationCategory.CONNECTIVITY,
            # Placement: footprint/outline issues
            ViolationType.FOOTPRINT: ViolationCategory.PLACEMENT,
            ViolationType.MALFORMED_OUTLINE: ViolationCategory.PLACEMENT,
            ViolationType.DUPLICATE_FOOTPRINT: ViolationCategory.PLACEMENT,
            ViolationType.EXTRA_FOOTPRINT: ViolationCategory.PLACEMENT,
            ViolationType.MISSING_FOOTPRINT: ViolationCategory.PLACEMENT,
        }
    )


class ViolationType(Enum):
    """Known DRC violation types."""

    # Clearance violations
    CLEARANCE = "clearance"
    CLEARANCE_SEGMENT_VIA = "clearance_segment_via"
    CLEARANCE_PAD_SEGMENT = "clearance_pad_segment"
    CLEARANCE_PAD_VIA = "clearance_pad_via"
    CLEARANCE_PAD_PAD = "clearance_pad_pad"
    CLEARANCE_SEGMENT_SEGMENT = "clearance_segment_segment"
    CLEARANCE_VIA_VIA = "clearance_via_via"
    # Differential-pair within-pair clearance (Issue #2560, Epic #2556 Phase 1D).
    # Distinct from the generic CLEARANCE family because it validates the
    # *intra-pair* gap (allowed to be tighter than the manufacturer's inter-pair
    # ``min_clearance_mm``) against the per-class ``intra_pair_clearance``.
    DIFFPAIR_CLEARANCE_INTRA = "diffpair_clearance_intra"
    # Differential-pair length-skew (Issue #2649, Epic #2556 Phase 3J).
    # Fires when an *engaged* differential pair's routed length skew
    # (``|L_p - L_n|``) exceeds the per-class
    # ``skew_tolerance_mm`` (default 0.5 mm).  Distinct from
    # DIFFPAIR_ROUTING_CONTINUITY (which checks parallel-coupling
    # fraction): this rule cares about *total* length parity, not the
    # geometric topology of the route.  Distinct from CLEARANCE because
    # it checks a length-matching property, not edge-to-edge spacing.
    DIFFPAIR_LENGTH_SKEW = "diffpair_length_skew"
    # Match-group (N-trace) length-skew (Issue #2702, Epic #2661 Phase 2G).
    # Fires when a declared match group's per-member routed-length skew
    # (``max(L) - min(L)`` across the group) exceeds the per-class
    # ``length_match_tolerance_mm`` (default 0.5 mm).  N>=3 generalization
    # of DIFFPAIR_LENGTH_SKEW: the diff-pair rule is the N=2 special
    # case, this rule is the bus-group case (DDR DQ-strobe, MIPI CSI
    # lanes, TMDS).  Distinct from DIFFPAIR_LENGTH_SKEW because group
    # identity is the group's *name* (Phase 1B convention), not a P/N
    # name tuple.
    MATCH_GROUP_LENGTH_SKEW = "match_group_length_skew"
    # Differential-pair routing continuity (Issue #2640, Epic #2556 Phase 2G).
    # Fires when an *engaged* differential pair's coupled fraction (the
    # share of P's routed length whose nearest point on N is within the
    # coupling window AND parallel within +/-15 degrees) falls below the
    # per-class ``coupled_continuity_threshold`` (default 0.7).  Distinct
    # from CLEARANCE because it checks a topology / geometry property
    # (parallel-coupling continuity), not edge-to-edge spacing.
    DIFFPAIR_ROUTING_CONTINUITY = "diffpair_routing_continuity"
    COPPER_EDGE_CLEARANCE = "copper_edge_clearance"
    EDGE_CLEARANCE_TRACE = "edge_clearance_trace"
    EDGE_CLEARANCE_PAD = "edge_clearance_pad"
    EDGE_CLEARANCE_PAD_HOLE = "edge_clearance_pad_hole"
    EDGE_CLEARANCE_VIA = "edge_clearance_via"
    EDGE_CLEARANCE_ZONE = "edge_clearance_zone"
    COURTYARD_OVERLAP = "courtyard_overlap"

    # Connection issues
    UNCONNECTED_ITEMS = "unconnected_items"
    SHORTING_ITEMS = "shorting_items"

    # Via issues
    VIA_HOLE_LARGER_THAN_PAD = "via_hole_larger_than_pad"
    VIA_ANNULAR_WIDTH = "via_annular_width"
    MICRO_VIA_HOLE_TOO_SMALL = "micro_via_hole_too_small"
    # Via placed inside SMD pad on a manufacturer profile that does not
    # support via-in-pad processing.  See issue #2635 and
    # ``validate/rules/via_in_pad.py``.
    VIA_IN_PAD = "via_in_pad"

    # Track/trace dimension issues
    TRACK_WIDTH = "track_width"
    TRACK_ANGLE = "track_angle"
    DIMENSION_TRACE_WIDTH = "dimension_trace_width"
    DIMENSION_VIA_DRILL = "dimension_via_drill"
    DIMENSION_VIA_DIAMETER = "dimension_via_diameter"
    DIMENSION_ANNULAR_RING = "dimension_annular_ring"
    DIMENSION_DRILL_CLEARANCE = "dimension_drill_clearance"

    # Hole issues
    DRILL_HOLE_TOO_SMALL = "drill_hole_too_small"
    DRILL_CLEARANCE = "drill_clearance"
    NPTH_HOLE_TOO_SMALL = "npth_hole_too_small"
    HOLE_NEAR_HOLE = "hole_near_hole"

    # Silkscreen
    SILK_OVER_COPPER = "silk_over_copper"
    SILK_OVERLAP = "silk_overlap"
    SILKSCREEN_LINE_WIDTH = "silkscreen_line_width"
    SILKSCREEN_TEXT_HEIGHT = "silkscreen_text_height"
    SILKSCREEN_OVER_PAD = "silkscreen_over_pad"

    # Solder mask
    SOLDER_MASK_BRIDGE = "solder_mask_bridge"
    SOLDER_MASK_CLEARANCE = "solder_mask_clearance"
    MIN_PAD_SIZE = "min_pad_size"
    PTH_ANNULAR_RING = "pth_annular_ring"

    # Impedance
    IMPEDANCE = "impedance"

    # Placement
    FOOTPRINT_OUTSIDE_BOARD = "footprint_outside_board"
    # Netlist integrity
    NET_UNDECLARED = "net_undeclared"
    SINGLE_PAD_NET = "single_pad_net"

    # Misc
    FOOTPRINT = "footprint"
    MALFORMED_OUTLINE = "malformed_outline"
    DUPLICATE_FOOTPRINT = "duplicate_footprint"
    EXTRA_FOOTPRINT = "extra_footprint"
    MISSING_FOOTPRINT = "missing_footprint"

    # Zone fill
    ZONE_UNFILLED = "zone_unfilled"
    ZONE_FILL_DISABLED = "zone_fill_disabled"
    ZONE_NO_NET = "zone_no_net"

    # Unknown (catch-all)
    UNKNOWN = "unknown"

    @classmethod
    def from_string(cls, s: str) -> "ViolationType":
        """Parse violation type from string.

        Handles three sources of type strings:
        - KiCad-cli DRC report types (e.g., "clearance", "track_width")
        - Validate-module rule_id values (e.g., "clearance_pad_pad",
          "dimension_trace_width", "silkscreen_line_width")
        - Free-form descriptions from older reports
        """
        s_lower = s.lower().strip()

        # Try direct enum value match first (covers both legacy and new members)
        for vtype in cls:
            if vtype.value == s_lower:
                return vtype

        # Explicit alias table for validate-module rule_ids and common
        # variants that don't match an enum value directly.  This table
        # is checked before the fuzzy heuristics so that specific rule
        # names are never misclassified.
        _ALIASES: dict[str, ViolationType] = {
            # clearance subtypes produced by validate clearance checker
            "clearance_pad_pad": cls.CLEARANCE_PAD_PAD,
            "clearance_pad_segment": cls.CLEARANCE_PAD_SEGMENT,
            "clearance_pad_via": cls.CLEARANCE_PAD_VIA,
            "clearance_segment_segment": cls.CLEARANCE_SEGMENT_SEGMENT,
            "clearance_segment_via": cls.CLEARANCE_SEGMENT_VIA,
            "clearance_via_via": cls.CLEARANCE_VIA_VIA,
            # clearance subtypes using "trace" (synonym for "segment")
            "clearance_pad_trace": cls.CLEARANCE_PAD_SEGMENT,
            "clearance_trace_trace": cls.CLEARANCE_SEGMENT_SEGMENT,
            "clearance_trace_via": cls.CLEARANCE_SEGMENT_VIA,
            # Differential-pair within-pair clearance (Issue #2560).
            # MUST be in the alias table -- without this entry the fuzzy
            # fallback at the bottom of from_string() silently matches
            # ``"clearance"`` and miscategorizes this rule_id as the
            # generic CLEARANCE type, masking the new violation type from
            # downstream consumers that filter by exact ``type`` value.
            "diffpair_clearance_intra": cls.DIFFPAIR_CLEARANCE_INTRA,
            # Differential-pair length-skew (Issue #2649, Epic #2556 Phase 3J).
            # MUST be aliased explicitly even though the rule_id string
            # ``"diffpair_length_skew"`` does NOT contain the substring
            # ``"clearance"`` -- the fuzzy fallback at the bottom of
            # from_string() would otherwise drop through to ``UNKNOWN``,
            # which silently corrupts the violation type field for any
            # downstream filter that compares by exact type value.  This
            # entry is the only defense; do NOT delete it as "redundant".
            "diffpair_length_skew": cls.DIFFPAIR_LENGTH_SKEW,
            # Match-group (N-trace) length-skew (Issue #2702, Epic #2661
            # Phase 2G).  MUST be aliased explicitly even though the
            # rule_id string ``"match_group_length_skew"`` does NOT
            # contain the substring ``"clearance"`` -- the fuzzy fallback
            # at the bottom of from_string() would otherwise drop through
            # to ``UNKNOWN``, which silently corrupts the violation type
            # field for any downstream filter that compares by exact
            # type value.  This is the #2521 critical-gotcha precedent
            # carried forward (mirrors the diffpair_length_skew entry
            # above).  This entry is the only defense; do NOT delete it
            # as "redundant".
            "match_group_length_skew": cls.MATCH_GROUP_LENGTH_SKEW,
            # Differential-pair routing continuity (Issue #2640, Epic #2556
            # Phase 2G).  MUST be aliased explicitly even though the
            # rule_id string ``"diffpair_routing_continuity"`` does NOT
            # contain the substring ``"clearance"`` -- the fuzzy fallback
            # at the bottom of from_string() would otherwise drop through
            # to ``UNKNOWN``, which silently corrupts the violation type
            # field for any downstream filter that compares by exact
            # type value.  This entry is the only defense; do NOT delete
            # it as "redundant".
            "diffpair_routing_continuity": cls.DIFFPAIR_ROUTING_CONTINUITY,
            # edge clearance subtypes
            "edge_clearance_trace": cls.EDGE_CLEARANCE_TRACE,
            "edge_clearance_pad": cls.EDGE_CLEARANCE_PAD,
            "edge_clearance_pad_hole": cls.EDGE_CLEARANCE_PAD_HOLE,
            "edge_clearance_via": cls.EDGE_CLEARANCE_VIA,
            "edge_clearance_zone": cls.EDGE_CLEARANCE_ZONE,
            # dimension rules from validate dimensions checker
            "dimension_trace_width": cls.DIMENSION_TRACE_WIDTH,
            "dimension_via_drill": cls.DIMENSION_VIA_DRILL,
            "dimension_via_diameter": cls.DIMENSION_VIA_DIAMETER,
            "dimension_annular_ring": cls.DIMENSION_ANNULAR_RING,
            "dimension_drill_clearance": cls.DIMENSION_DRILL_CLEARANCE,
            # silkscreen rules from validate silkscreen checker
            "silkscreen_line_width": cls.SILKSCREEN_LINE_WIDTH,
            "silkscreen_text_height": cls.SILKSCREEN_TEXT_HEIGHT,
            "silkscreen_over_pad": cls.SILKSCREEN_OVER_PAD,
            # solder mask rules from validate solder_mask checker
            "solder_mask_clearance": cls.SOLDER_MASK_CLEARANCE,
            "min_pad_size": cls.MIN_PAD_SIZE,
            "pth_annular_ring": cls.PTH_ANNULAR_RING,
            # impedance rule from validate impedance checker
            "impedance": cls.IMPEDANCE,
            # placement rule from validate placement checker
            "footprint_outside_board": cls.FOOTPRINT_OUTSIDE_BOARD,
            # netlist integrity rule from validate netlist checker
            "net_undeclared": cls.NET_UNDECLARED,
            "single_pad_net": cls.SINGLE_PAD_NET,
            # zone fill rules from validate zone_fill checker
            "zone_unfilled": cls.ZONE_UNFILLED,
            "zone_fill_disabled": cls.ZONE_FILL_DISABLED,
            "zone_no_net": cls.ZONE_NO_NET,
            # via-in-pad rule from validate via_in_pad checker (issue #2635).
            # MUST be aliased explicitly -- the fuzzy fallback below would
            # otherwise match "via" and miscategorize this rule_id.
            "via_in_pad": cls.VIA_IN_PAD,
        }

        alias_match = _ALIASES.get(s_lower)
        if alias_match is not None:
            return alias_match

        # Fuzzy heuristics for free-form strings (e.g., KiCad-cli descriptions)
        # Check drill_clearance before general clearance (both contain "clearance")
        if "drill" in s_lower and "clearance" in s_lower:
            return cls.DRILL_CLEARANCE
        if "clearance" in s_lower:
            if "edge" in s_lower:
                return cls.COPPER_EDGE_CLEARANCE
            if "segment" in s_lower and "via" in s_lower:
                return cls.CLEARANCE_SEGMENT_VIA
            if "pad" in s_lower and "segment" in s_lower:
                return cls.CLEARANCE_PAD_SEGMENT
            if "pad" in s_lower and "via" in s_lower:
                return cls.CLEARANCE_PAD_VIA
            return cls.CLEARANCE
        if "unconnected" in s_lower:
            return cls.UNCONNECTED_ITEMS
        if "short" in s_lower:
            return cls.SHORTING_ITEMS
        if "courtyard" in s_lower:
            return cls.COURTYARD_OVERLAP
        if ("track" in s_lower or "trace" in s_lower) and "width" in s_lower:
            return cls.TRACK_WIDTH
        if "via" in s_lower:
            if "annular" in s_lower:
                return cls.VIA_ANNULAR_WIDTH
            if "hole" in s_lower and "larger" in s_lower:
                return cls.VIA_HOLE_LARGER_THAN_PAD
            if "micro" in s_lower:
                return cls.MICRO_VIA_HOLE_TOO_SMALL
        if "drill" in s_lower:
            return cls.DRILL_HOLE_TOO_SMALL
        if "silk" in s_lower:
            if "copper" in s_lower:
                return cls.SILK_OVER_COPPER
            if "line" in s_lower and "width" in s_lower:
                return cls.SILKSCREEN_LINE_WIDTH
            if "text" in s_lower and "height" in s_lower:
                return cls.SILKSCREEN_TEXT_HEIGHT
            if "over" in s_lower and "pad" in s_lower:
                return cls.SILKSCREEN_OVER_PAD
            return cls.SILK_OVERLAP
        if "solder" in s_lower and "mask" in s_lower:
            if "clearance" in s_lower:
                return cls.SOLDER_MASK_CLEARANCE
            return cls.SOLDER_MASK_BRIDGE
        if "impedance" in s_lower:
            return cls.IMPEDANCE
        if "footprint" in s_lower:
            if "outside" in s_lower:
                return cls.FOOTPRINT_OUTSIDE_BOARD
            if "duplicate" in s_lower:
                return cls.DUPLICATE_FOOTPRINT
            if "extra" in s_lower:
                return cls.EXTRA_FOOTPRINT
            if "missing" in s_lower:
                return cls.MISSING_FOOTPRINT
            return cls.FOOTPRINT
        if "outline" in s_lower:
            return cls.MALFORMED_OUTLINE

        return cls.UNKNOWN


@dataclass
class Location:
    """Position on the PCB."""

    x_mm: float
    y_mm: float
    layer: str = ""

    @classmethod
    def from_string(cls, s: str) -> Optional["Location"]:
        """Parse location from string like '@(162.4500 mm, 100.3250 mm)'."""
        import re

        # Match @(x mm, y mm) pattern
        match = re.search(r"@\s*\(\s*([\d.]+)\s*mm\s*,\s*([\d.]+)\s*mm\s*\)", s)
        if match:
            return cls(
                x_mm=float(match.group(1)),
                y_mm=float(match.group(2)),
            )

        # Also try pos:{x:..., y:...} for JSON format
        match = re.search(r'"x"\s*:\s*([\d.]+).*?"y"\s*:\s*([\d.]+)', s)
        if match:
            return cls(
                x_mm=float(match.group(1)),
                y_mm=float(match.group(2)),
            )

        return None

    def __str__(self) -> str:
        layer_str = f" on {self.layer}" if self.layer else ""
        return f"({self.x_mm:.2f}, {self.y_mm:.2f}) mm{layer_str}"


@dataclass
class DRCViolation:
    """Represents a single DRC violation.

    Includes both board-level location (x_mm, y_mm) and optional file-level
    source position (file:line:column) for precise error reporting.
    """

    type: ViolationType
    type_str: str  # Original type string from report
    severity: Severity
    message: str
    rule: str = ""
    locations: list[Location] = field(default_factory=list)
    items: list[str] = field(default_factory=list)
    nets: list[str] = field(default_factory=list)

    # Extracted numeric values (when available)
    required_value_mm: float | None = None
    actual_value_mm: float | None = None

    # Source position in KiCad file (file:line:column)
    source_position: "SourcePosition | None" = None

    # Fix suggestions (populated by generate_fix_suggestions)
    suggestions: list["FixSuggestion"] = field(default_factory=list)

    @property
    def category(self) -> ViolationCategory:
        """Infer root-cause category from violation type and context.

        For SOLDER_MASK_BRIDGE violations, checks whether both items reference
        pads on the same component (placement-inherent) or involve different
        components/vias (routing-fixable).
        """
        # Special case: solder mask bridge between same-component pads is
        # placement-inherent; between different components or vias is routing
        if self.type == ViolationType.SOLDER_MASK_BRIDGE:
            refs = _extract_component_refs(self.items)
            if len(refs) == 1:
                # Both items on same component -- placement-inherent
                return ViolationCategory.PLACEMENT
            elif len(refs) >= 2:
                # Different components -- potentially routing-fixable
                return ViolationCategory.ROUTING

        return _TYPE_CATEGORY_MAP.get(self.type, ViolationCategory.ROUTING)

    def is_same_component_pad_clearance(self) -> bool:
        """Check if this is a pad-pad clearance violation between pads on the same component.

        Adjacent pads within a single IC footprint (e.g., TSSOP-28 with 0.65mm pitch)
        can trigger CLEARANCE_PAD_PAD violations when the board-level clearance rule
        exceeds the inherent pad gap.  These are false positives because the pad
        spacing is fixed by the footprint geometry and cannot be changed by layout.

        Returns:
            True if this is a CLEARANCE_PAD_PAD violation where both items
            reference pads on the same component.
        """
        if self.type != ViolationType.CLEARANCE_PAD_PAD:
            return False

        refs = _extract_component_refs(self.items)
        return len(refs) == 1

    def is_fine_pitch_inherent(self, min_solder_mask_dam_mm: float = 0.1) -> bool:
        """Check if this solder mask bridge is inherent to a fine-pitch IC.

        A solder mask bridge violation is fine-pitch-inherent when:
        - The violation type is SOLDER_MASK_BRIDGE
        - Both items reference pads on the same component (same ref designator)
        - The actual solder mask dam width is below the manufacturer's minimum

        Args:
            min_solder_mask_dam_mm: The manufacturer's minimum solder mask dam
                width. Defaults to 0.1mm (JLCPCB standard).

        Returns:
            True if this violation is inherent to the IC footprint geometry
            and cannot be fixed by layout changes.
        """
        if self.type != ViolationType.SOLDER_MASK_BRIDGE:
            return False

        # Both items must be on the same component
        refs = _extract_component_refs(self.items)
        if len(refs) != 1:
            return False

        # The actual mask dam must be below the manufacturer's minimum
        if self.actual_value_mm is not None:
            return self.actual_value_mm < min_solder_mask_dam_mm

        # No measurement available -- fall through to False
        return False

    @property
    def is_error(self) -> bool:
        """Check if this is an error (vs warning)."""
        return self.severity == Severity.ERROR

    @property
    def is_clearance(self) -> bool:
        """Check if this is a clearance violation."""
        return self.type in (
            ViolationType.CLEARANCE,
            ViolationType.CLEARANCE_SEGMENT_VIA,
            ViolationType.CLEARANCE_PAD_SEGMENT,
            ViolationType.CLEARANCE_PAD_VIA,
            ViolationType.CLEARANCE_PAD_PAD,
            ViolationType.CLEARANCE_SEGMENT_SEGMENT,
            ViolationType.CLEARANCE_VIA_VIA,
            ViolationType.COPPER_EDGE_CLEARANCE,
            ViolationType.EDGE_CLEARANCE_TRACE,
            ViolationType.EDGE_CLEARANCE_PAD,
            ViolationType.EDGE_CLEARANCE_PAD_HOLE,
            ViolationType.EDGE_CLEARANCE_VIA,
            ViolationType.EDGE_CLEARANCE_ZONE,
        )

    @property
    def is_connection(self) -> bool:
        """Check if this is a connection issue."""
        return self.type in (ViolationType.UNCONNECTED_ITEMS, ViolationType.SHORTING_ITEMS)

    @property
    def primary_location(self) -> Location | None:
        """Get the first location if available."""
        return self.locations[0] if self.locations else None

    @property
    def location_str(self) -> str:
        """Format location as file:line:col or board coordinates."""
        if self.source_position:
            return str(self.source_position)
        elif self.primary_location:
            return str(self.primary_location)
        return ""

    @property
    def delta_mm(self) -> float | None:
        """Calculate the difference between required and actual values.

        For clearance violations, this is how much more clearance is needed.
        Returns None if values are not available.
        """
        if self.required_value_mm is None or self.actual_value_mm is None:
            return None
        return self.required_value_mm - self.actual_value_mm

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        result: dict[str, Any] = {
            "type": self.type.value,
            "type_str": self.type_str,
            "severity": self.severity.value,
            "category": self.category.value,
            "message": self.message,
            "rule": self.rule,
            "locations": [
                {"x_mm": loc.x_mm, "y_mm": loc.y_mm, "layer": loc.layer} for loc in self.locations
            ],
            "items": self.items,
            "nets": self.nets,
            "required_value_mm": self.required_value_mm,
            "actual_value_mm": self.actual_value_mm,
            "delta_mm": self.delta_mm,
        }
        if self.source_position:
            result["source_position"] = self.source_position.to_dict()
        if self.suggestions:
            # Handle both FixSuggestion objects and plain strings
            result["suggestions"] = [
                s.to_dict() if hasattr(s, "to_dict") else str(s) for s in self.suggestions
            ]
        return result

    def __str__(self) -> str:
        # Prefer file:line:col format if available
        if self.source_position:
            return f"{self.source_position}: [{self.type_str}] {self.message}"
        loc_str = ""
        if self.primary_location:
            loc_str = f" at {self.primary_location}"
        return f"[{self.type_str}]: {self.message}{loc_str}"


# Regex to extract component reference designators from DRC item strings.
# Matches patterns like "Pad 1 of U3", "Pad A1 of U3", "of C12", etc.
_REF_PATTERN = re.compile(r"\bof\s+([A-Z]+\d+)\b", re.IGNORECASE)


def _extract_component_refs(items: list[str]) -> set[str]:
    """Extract unique component reference designators from DRC item strings.

    Looks for patterns like "Pad 1 of U3" or "Pad A1 of C12" in item
    descriptions and returns the set of unique references found.
    """
    refs: set[str] = set()
    for item in items:
        for match in _REF_PATTERN.finditer(item):
            refs.add(match.group(1).upper())
    return refs


# Initialize the type-to-category map now that ViolationType is defined
_init_type_category_map()
