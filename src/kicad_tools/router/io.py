"""
PCB file I/O for autorouting.

This module provides:
- route_pcb: Route a PCB given component placements and net assignments
- load_pcb_for_routing: Load a KiCad PCB file and create an Autorouter
- merge_routes_into_pcb: Merge routed traces into an existing PCB file
- generate_netclass_setup: Generate KiCad 7+ compatible net class setup
- parse_pcb_design_rules: Extract design rules from a KiCad PCB file
- validate_grid_resolution: Check grid resolution vs clearance for DRC compliance
- validate_routes: Post-route validation for clearance issues

These functions handle the translation between KiCad file formats and
the autorouter's internal representations.

Note on net class metadata:
    Generated routes embed trace widths and via sizes directly in their
    S-expressions, so net class metadata is NOT required for the routing
    to work correctly. The generate_netclass_setup() function is provided
    for users who want to add net class definitions for documentation or
    DRC purposes, using the KiCad 7+ compatible format.

    DO NOT use the old KiCad 6 format with (net_settings (net_class ...))
    as this is incompatible with KiCad 7+.

DRC Compliance (v0.5.1):
    For DRC-clean output, ensure:
    1. Grid resolution <= clearance / 2 (use validate_grid_resolution)
    2. Via sizes meet PCB minimums (use parse_pcb_design_rules)
    3. Post-route validation (use validate_routes)
"""

from __future__ import annotations

import contextlib
import itertools
import logging
import math
import re
import warnings
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from kicad_tools.progress import ProgressCallback

    from .primitives import Pad

from .core import Autorouter
from .geometry import (
    point_to_segment_distance as _geom_point_to_seg_dist,
)
from .geometry import (
    segment_to_segment_distance as _geom_seg_to_seg_dist,
)
from .layers import Layer, LayerDefinition, LayerStack, LayerType
from .rules import DEFAULT_NET_CLASS_MAP, DesignRules, NetClassRouting

# Floating-point tolerance for clearance comparisons in validate_routes
# (Issue #2465).  IEEE-754 rounding in radius/distance math can leave
# computed clearances at values like 0.14999999... when the design intent
# is exactly 0.150mm, producing spurious sub-micron false-positive
# violations.  0.1 micron is well below any manufacturing precision and
# matches the epsilon used in #2428 (validate/rules/edge.py) and the
# pad-pad DRC check in drc/incremental.py.
_CLEARANCE_EPSILON_MM = 1e-4

# =============================================================================
# DRC COMPLIANCE TYPES AND FUNCTIONS
# =============================================================================


class GridResolutionError(ValueError):
    """Raised when grid resolution is incompatible with DRC compliance.

    This exception is raised when the router detects that the grid resolution
    is too coarse to guarantee DRC-compliant output. This prevents wasted
    routing time that would produce unusable results.

    Attributes:
        grid_resolution: The problematic grid resolution in mm
        clearance: The required clearance in mm
        recommended: The recommended grid resolution (clearance / 2) in mm
    """

    def __init__(
        self,
        grid_resolution: float,
        clearance: float,
        message: str | None = None,
    ):
        self.grid_resolution = grid_resolution
        self.clearance = clearance
        self.recommended = clearance / 2

        if message is None:
            message = (
                f"Grid resolution {grid_resolution}mm is incompatible with "
                f"{clearance}mm clearance. Use grid_resolution <= {self.recommended}mm "
                f"for reliable DRC compliance."
            )
        super().__init__(message)


@dataclass
class GridAdjustment:
    """Result of automatic grid resolution adjustment.

    Returned when auto_adjust_grid=True and the grid resolution was adjusted
    to ensure DRC compliance.

    Attributes:
        original: The original grid resolution in mm
        adjusted: The adjusted grid resolution in mm
        clearance: The clearance requirement in mm
        was_adjusted: True if adjustment was made
    """

    original: float
    adjusted: float
    clearance: float
    was_adjusted: bool = False

    @property
    def message(self) -> str:
        """Human-readable message about the adjustment."""
        if self.was_adjusted:
            return (
                f"Grid resolution adjusted from {self.original}mm to {self.adjusted}mm "
                f"for DRC compliance with {self.clearance}mm clearance"
            )
        return f"Grid resolution {self.original}mm is compliant"


@dataclass
class PCBDesignRules:
    """Design rules extracted from a KiCad PCB file's setup section.

    These represent the board's actual constraints and should be used
    to configure the router for DRC-compliant output.
    """

    # Track constraints
    min_track_width: float = 0.2  # mm
    # Via constraints
    min_via_diameter: float = 0.6  # mm
    min_via_drill: float = 0.3  # mm
    # Clearances
    min_clearance: float = 0.2  # mm
    # Drill-to-drill spacing (including same-net vias)
    min_drill_clearance: float = 0.102  # mm
    # Copper to edge
    copper_edge_clearance: float = 0.3  # mm

    def to_design_rules(
        self,
        grid_resolution: float | None = None,
        manufacturer: str | None = None,
    ) -> DesignRules:
        """Convert to DesignRules for the router.

        Args:
            grid_resolution: Override grid resolution. If None, uses
                            clearance / 2 for DRC compliance.
            manufacturer: Optional manufacturer identifier (e.g.,
                            ``"jlcpcb-tier1"``) used by capability-gated
                            routing features such as via-in-pad escape.
                            See Issue #2708 — when omitted, the returned
                            rules behave as if no manufacturer-specific
                            capabilities are available.

        Returns:
            DesignRules configured with these constraints.
        """
        # Default to clearance / 2 for DRC compliance
        if grid_resolution is None:
            grid_resolution = self.min_clearance / 2

        return DesignRules(
            trace_width=self.min_track_width,
            trace_clearance=self.min_clearance,
            via_drill=self.min_via_drill,
            via_diameter=self.min_via_diameter,
            via_clearance=self.min_clearance,
            min_drill_clearance=self.min_drill_clearance,
            grid_resolution=grid_resolution,
            manufacturer=manufacturer,
        )


@dataclass
class ClearanceViolation:
    """A potential clearance violation detected during post-route validation.

    Shape contract per ``obstacle_type``:

    * ``"pad"``:    ``segment_index >= 0`` and ``(x1,y1)-(x2,y2)`` is the
                    offending segment; ``location`` is the pad centre.
                    OR ``segment_index == -1`` and ``x1==x2, y1==y2`` (the
                    via centre) when the violation is via-vs-pad.
    * ``"segment"``: ``segment_index >= 0`` and ``(x1,y1)-(x2,y2)`` is the
                    offending segment; ``location`` is the approximate
                    midpoint of closest approach.
    * ``"via"``:    Two distinct shapes share this tag:
                    (a) segment-vs-via: ``segment_index >= 0`` and
                        ``(x1,y1)-(x2,y2)`` is the offending segment;
                        ``location`` is the via centre.
                    (b) via-vs-via: ``segment_index == -1`` and
                        ``x1==x2, y1==y2`` (one of the two vias' centres);
                        ``location`` is the midpoint between the two vias.
                    Downstream handlers (e.g. ``drc_nudge.py``) MUST
                    distinguish (a) from (b) by checking ``segment_index``.
    * ``"edge"``:   Trace-vs-board-edge violation.  ``segment_index >= 0``
                    and ``(x1,y1)-(x2,y2)`` is the offending segment;
                    ``obstacle_net == 0`` and ``location`` is the closest
                    point on the board outline.  ``layer`` is the segment
                    layer.  Issue #2743.
    """

    segment_index: int
    x1: float
    y1: float
    x2: float
    y2: float
    net: int
    obstacle_type: str  # "pad", "via", "segment", "edge"
    obstacle_net: int
    distance: float  # Actual distance in mm
    required: float  # Required clearance in mm
    net_name: str = ""  # Human-readable net name
    obstacle_net_name: str = ""  # Human-readable obstacle net name
    location: tuple[float, float] | None = None  # Approximate violation location (x, y)
    component_inherent: bool = False  # True if both pads are on the same component
    layer: Layer | None = None  # Copper layer where the violation occurs


def parse_pcb_design_rules(pcb_text: str) -> PCBDesignRules:
    """Parse design rules from a KiCad PCB file's setup section.

    Extracts via/track minimums from the (setup ...) section of a KiCad PCB.
    This allows the router to respect the board's actual constraints.

    KiCad 7+ stores design rules in these locations:
    - (setup (pad_to_mask_clearance X)) - pad mask clearance
    - (setup (min_via_annular_width X)) - minimum via annular ring
    - Net class definitions for track widths and clearances

    Args:
        pcb_text: Contents of a .kicad_pcb file

    Returns:
        PCBDesignRules with extracted or default values.

    Example:
        >>> pcb_text = Path("board.kicad_pcb").read_text()
        >>> rules = parse_pcb_design_rules(pcb_text)
        >>> print(f"Min track: {rules.min_track_width}mm")
    """
    rules = PCBDesignRules()

    # Track whether we've found values from the PCB (vs using defaults)
    found_clearance = False
    found_track_width = False
    found_via_diameter = False
    found_via_drill = False

    # Extract setup section (optional - may not exist or be empty)
    setup_match = re.search(r"\(setup\s+(.*?)\n\s*\)", pcb_text, re.DOTALL)
    if setup_match:
        setup_text = setup_match.group(1)

        # Parse pad_to_mask_clearance (often indicates minimum clearance)
        mask_match = re.search(r"\(pad_to_mask_clearance\s+([\d.]+)\)", setup_text)
        if mask_match:
            mask_clearance = float(mask_match.group(1))
            if mask_clearance > 0:
                # Use as hint for clearance, but not definitive
                pass

        # Parse min_via_annular_width if present
        via_ann_match = re.search(r"\(min_via_annular_width\s+([\d.]+)\)", setup_text)
        if via_ann_match:
            ann_width = float(via_ann_match.group(1))
            # Via diameter = drill + 2 * annular width
            # We'll use this to calculate minimum via diameter
            pass

    # Look for net class definitions (KiCad 7+ format)
    # Note: These can exist even without a setup section
    # These are typically in the form: (net_class "Default" ...)
    # with (clearance X), (trace_width X), (via_dia X), (via_drill X)
    # Net class blocks can span multiple lines

    # Find all net_class blocks using bracket matching
    for nc_match in re.finditer(r'\(net_class\s+"([^"]+)"', pcb_text):
        class_name = nc_match.group(1)
        start_pos = nc_match.start()

        # Find the matching closing paren for this net_class block
        depth = 0
        end_pos = start_pos
        for i, char in enumerate(pcb_text[start_pos:], start_pos):
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth == 0:
                    end_pos = i + 1
                    break

        nc_block = pcb_text[start_pos:end_pos]

        # Extract values from the block
        # Use min() only after we've found a value from this PCB
        clearance_match = re.search(r"\(clearance\s+([\d.]+)\)", nc_block)
        if clearance_match:
            clearance = float(clearance_match.group(1))
            if clearance > 0:
                if found_clearance:
                    rules.min_clearance = min(rules.min_clearance, clearance)
                else:
                    rules.min_clearance = clearance
                    found_clearance = True

        trace_match = re.search(r"\(trace_width\s+([\d.]+)\)", nc_block)
        if trace_match:
            trace_width = float(trace_match.group(1))
            if trace_width > 0:
                if found_track_width:
                    rules.min_track_width = min(rules.min_track_width, trace_width)
                else:
                    rules.min_track_width = trace_width
                    found_track_width = True

        via_dia_match = re.search(r"\(via_dia\s+([\d.]+)\)", nc_block)
        if via_dia_match:
            via_dia = float(via_dia_match.group(1))
            if via_dia > 0:
                if found_via_diameter:
                    rules.min_via_diameter = min(rules.min_via_diameter, via_dia)
                else:
                    rules.min_via_diameter = via_dia
                    found_via_diameter = True

        via_drill_match = re.search(r"\(via_drill\s+([\d.]+)\)", nc_block)
        if via_drill_match:
            via_drill = float(via_drill_match.group(1))
            if via_drill > 0:
                if found_via_drill:
                    rules.min_via_drill = min(rules.min_via_drill, via_drill)
                else:
                    rules.min_via_drill = via_drill
                    found_via_drill = True

    # Also check for board-level constraints (KiCad 8 format)
    # (design_settings (min_clearance X) (min_track_width X) ...)
    design_match = re.search(r"\(design_settings\s+(.*?)\n\s*\)", pcb_text, re.DOTALL)
    if design_match:
        design_text = design_match.group(1)

        min_clear_match = re.search(r"\(min_clearance\s+([\d.]+)\)", design_text)
        if min_clear_match:
            rules.min_clearance = float(min_clear_match.group(1))

        min_track_match = re.search(r"\(min_track_width\s+([\d.]+)\)", design_text)
        if min_track_match:
            rules.min_track_width = float(min_track_match.group(1))

        min_via_match = re.search(r"\(min_via_diameter\s+([\d.]+)\)", design_text)
        if min_via_match:
            rules.min_via_diameter = float(min_via_match.group(1))

        min_drill_match = re.search(r"\(min_via_drill\s+([\d.]+)\)", design_text)
        if min_drill_match:
            rules.min_via_drill = float(min_drill_match.group(1))

        # Parse minimum drill-to-drill clearance (KiCad 8+: min_drill)
        min_drill_clearance_match = re.search(r"\(min_drill\s+([\d.]+)\)", design_text)
        if min_drill_clearance_match:
            rules.min_drill_clearance = float(min_drill_clearance_match.group(1))

    return rules


def validate_grid_resolution(
    grid_resolution: float,
    clearance: float,
    warn: bool = True,
    strict: bool = True,
) -> list[str]:
    """Validate grid resolution against clearance for DRC compliance.

    The discrete routing grid can cause clearance violations when the grid
    resolution is too coarse relative to the required clearance. For reliable
    DRC compliance, grid_resolution should be <= clearance / 2.

    Args:
        grid_resolution: Router grid resolution in mm
        clearance: Required trace/via clearance in mm
        warn: If True, emit warnings via warnings.warn() for non-fatal issues
        strict: If True (default), raise GridResolutionError for any DRC risk.
                If False, only raise for guaranteed violations (grid > clearance).

    Returns:
        List of warning messages (empty if compliant).

    Raises:
        GridResolutionError: When grid resolution will cause DRC violations
            (always when grid > clearance, or when strict=True and grid > clearance/2)

    Example:
        >>> # Strict mode (default) - fails fast on any risk
        >>> validate_grid_resolution(0.15, 0.2, strict=True)
        GridResolutionError: Grid resolution 0.15mm is incompatible with 0.2mm clearance

        >>> # Lenient mode - only fails on guaranteed violations
        >>> warnings = validate_grid_resolution(0.15, 0.2, strict=False)
        >>> if warnings:
        ...     print("Grid resolution may cause DRC violations")
    """
    issues: list[str] = []

    recommended = clearance / 2

    if grid_resolution > clearance:
        # This WILL cause DRC violations - always fail
        raise GridResolutionError(
            grid_resolution,
            clearance,
            f"Grid resolution {grid_resolution}mm exceeds clearance {clearance}mm. "
            f"This WILL cause DRC violations. Use grid_resolution <= {clearance}mm.",
        )

    elif grid_resolution > recommended:
        msg = (
            f"Grid resolution {grid_resolution}mm may cause clearance violations "
            f"with {clearance}mm clearance. Recommend grid_resolution <= {recommended}mm "
            f"for reliable DRC compliance."
        )

        if strict:
            # In strict mode, any DRC risk is a failure
            raise GridResolutionError(grid_resolution, clearance, msg)
        issues.append(msg)
        if warn:
            warnings.warn(msg, stacklevel=2)

    return issues


def adjust_grid_for_compliance(
    grid_resolution: float,
    clearance: float,
) -> GridAdjustment:
    """Adjust grid resolution if needed for DRC compliance.

    When the grid resolution is too coarse for the required clearance,
    this function calculates a DRC-compliant grid resolution (clearance / 2).

    Args:
        grid_resolution: Current grid resolution in mm
        clearance: Required trace/via clearance in mm

    Returns:
        GridAdjustment with original and adjusted values.

    Example:
        >>> adjustment = adjust_grid_for_compliance(0.25, 0.2)
        >>> if adjustment.was_adjusted:
        ...     print(f"Adjusted: {adjustment.original}mm -> {adjustment.adjusted}mm")
    """
    recommended = clearance / 2

    if grid_resolution > recommended:
        return GridAdjustment(
            original=grid_resolution,
            adjusted=recommended,
            clearance=clearance,
            was_adjusted=True,
        )

    return GridAdjustment(
        original=grid_resolution,
        adjusted=grid_resolution,
        clearance=clearance,
        was_adjusted=False,
    )


@dataclass
class FineZone:
    """A local fine-grid zone around a component.

    Attributes:
        ref: Component reference designator
        x_min: Bounding box minimum X (mm)
        y_min: Bounding box minimum Y (mm)
        x_max: Bounding box maximum X (mm)
        y_max: Bounding box maximum Y (mm)
        resolution: Fine grid resolution for this zone (mm)
        x_offset: Origin offset of the fine grid along X (mm, default 0.0).
            Grid points within this zone fall at ``x_offset + k * resolution``
            for integer k.  Used so the fine grid aligns to the component's
            actual pad positions even when those positions are off-grid in
            world coordinates (issue #2837).
        y_offset: Origin offset of the fine grid along Y (mm, default 0.0).
    """

    ref: str
    x_min: float
    y_min: float
    x_max: float
    y_max: float
    resolution: float
    x_offset: float = 0.0
    y_offset: float = 0.0

    @property
    def width(self) -> float:
        """Zone width in mm."""
        return self.x_max - self.x_min

    @property
    def height(self) -> float:
        """Zone height in mm."""
        return self.y_max - self.y_min

    @property
    def cell_count(self) -> int:
        """Estimated cell count for this zone (single layer)."""
        cols = int(self.width / self.resolution) + 1
        rows = int(self.height / self.resolution) + 1
        return cols * rows

    def contains(self, x: float, y: float) -> bool:
        """Check if a point (x, y) falls within this fine zone.

        Args:
            x: X coordinate in mm (world units)
            y: Y coordinate in mm (world units)

        Returns:
            True if the point is inside the zone bounding box.
        """
        return self.x_min <= x <= self.x_max and self.y_min <= y <= self.y_max


@dataclass
class MultiResolutionGridPlan:
    """Plan for adaptive multi-resolution grid routing.

    When auto_select_grid_resolution determines that a uniform grid would
    either exceed memory limits at a fine resolution or leave fine-pitch pads
    off-grid at a coarse resolution, it produces this plan instead.

    The plan specifies:
    - A coarse global resolution for channel routing
    - Per-component fine zones with local high-resolution grids

    Attributes:
        coarse_resolution: Global grid resolution for channel routing (mm)
        fine_zones: List of per-component fine-grid zones
        total_cell_estimate: Estimated total cells across all zones + global grid
        uniform_fallback: The resolution that uniform grid selection would use
    """

    coarse_resolution: float
    fine_zones: list[FineZone]
    total_cell_estimate: int = 0
    uniform_fallback: float = 0.0

    @property
    def is_multi_resolution(self) -> bool:
        """True if fine zones are present (adaptive routing needed)."""
        return len(self.fine_zones) > 0

    def summary(self) -> str:
        """Human-readable summary of the multi-resolution plan."""
        lines = [
            "Multi-Resolution Grid Plan (adaptive)",
            f"  Coarse grid: {self.coarse_resolution:.3f}mm",
            f"  Fine zones: {len(self.fine_zones)}",
        ]
        for zone in self.fine_zones:
            offset_str = ""
            if zone.x_offset != 0.0 or zone.y_offset != 0.0:
                offset_str = f" offset=({zone.x_offset:.4f},{zone.y_offset:.4f})"
            lines.append(
                f"    {zone.ref}: {zone.resolution:.4f}mm "
                f"({zone.width:.1f}x{zone.height:.1f}mm, "
                f"~{zone.cell_count:,} cells){offset_str}"
            )
        lines.append(f"  Total cell estimate: {self.total_cell_estimate:,}")
        if self.uniform_fallback > 0:
            lines.append(f"  Uniform fallback would be: {self.uniform_fallback:.3f}mm")
        return "\n".join(lines)


@dataclass
class GridAutoSelection:
    """Result of automatic grid resolution selection.

    Attributes:
        resolution: The selected grid resolution in mm
        off_grid_pads: Number of pads that don't align to the grid
        total_pads: Total number of pads analyzed
        off_grid_percentage: Percentage of pads that are off-grid
        candidates_tried: List of (resolution, off_grid_count) tuples tried
        memory_capped: True if the resolution was constrained by memory budget
        uncapped_resolution: The resolution that would have been selected without
            memory constraints (None if no capping occurred)
        origin_offset: Optimal grid origin offset (x_mm, y_mm) that maximizes
            on-grid pad count. Default (0.0, 0.0) means no offset.
        clearance_compliant_at_clearance_over_2: True if the selected resolution
            is <= clearance/2, satisfying the recommended DRC margin. Set by
            the clearance-aware retry path (issue #3239).
        memory_budget_used: The effective ``max_cells`` value the selector
            converged on. Equals the input ``max_cells`` for normal selection,
            or the bumped budget if the clearance-aware retry was applied.
        lattice_rescued: True if the issue #3441 lattice rescue bumped the
            memory budget to admit a candidate that aligns the board's
            dominant pad lattice (<= clearance but > clearance/2).
    """

    resolution: float
    off_grid_pads: int
    total_pads: int
    off_grid_percentage: float
    candidates_tried: list[tuple[float, int]]
    memory_capped: bool = False
    uncapped_resolution: float | None = None
    origin_offset: tuple[float, float] = (0.0, 0.0)
    clearance_compliant_at_clearance_over_2: bool = False
    memory_budget_used: int = 0
    lattice_rescued: bool = False

    def summary(self) -> str:
        """Human-readable summary of the selection."""
        lines = [
            f"Selected grid: {self.resolution}mm",
        ]
        if self.origin_offset != (0.0, 0.0):
            lines.append(
                f"  Grid origin offset: ({self.origin_offset[0]:.4f}, "
                f"{self.origin_offset[1]:.4f})mm"
            )
        if self.memory_capped and self.uncapped_resolution is not None:
            lines.append(f"  (capped from {self.uncapped_resolution}mm due to memory budget)")
        if self.lattice_rescued:
            lines.append(
                "  (lattice rescue: memory budget bumped to "
                f"{self.memory_budget_used:,} cells to admit the dominant "
                "pad-lattice grid -- issue #3441)"
            )
        lines.extend(
            [
                f"  Total pads: {self.total_pads}",
                f"  Off-grid pads: {self.off_grid_pads} ({self.off_grid_percentage:.1f}%)",
            ]
        )
        if self.candidates_tried:
            lines.append("  Candidates analyzed:")
            for res, off_grid in self.candidates_tried:
                pct = (off_grid / self.total_pads * 100) if self.total_pads > 0 else 0
                marker = " <- selected" if res == self.resolution else ""
                lines.append(f"    {res}mm: {off_grid} off-grid ({pct:.1f}%){marker}")
        return "\n".join(lines)


def _is_on_grid(value: float, resolution: float, threshold: float | None = None) -> bool:
    """Check if a value aligns to the grid within a threshold.

    Args:
        value: The coordinate value to check
        resolution: The grid resolution
        threshold: Maximum allowed deviation (default: resolution / 10)

    Returns:
        True if the value is on-grid within the threshold.
    """
    if threshold is None:
        threshold = resolution / 10

    # Calculate distance to nearest grid point
    remainder = abs(value % resolution)
    distance_to_grid = min(remainder, resolution - remainder)

    return distance_to_grid <= threshold


def _is_on_grid_with_offset(
    value: float, resolution: float, offset: float, threshold: float | None = None
) -> bool:
    """Check if a value aligns to a grid with a given origin offset.

    The grid is shifted by *offset*, so grid points are at
    ``offset + k * resolution`` for integer k.

    Args:
        value: The coordinate value to check
        resolution: The grid resolution
        offset: Grid origin offset along this axis
        threshold: Maximum allowed deviation (default: resolution / 10)

    Returns:
        True if the value is on the shifted grid within the threshold.
    """
    return _is_on_grid(value - offset, resolution, threshold)


def _count_off_grid_with_offset(
    pad_list: list,
    resolution: float,
    x_offset: float = 0.0,
    y_offset: float = 0.0,
) -> int:
    """Count pads that are off-grid at the given resolution and offset.

    Args:
        pad_list: List of pad objects with x, y attributes
        resolution: Grid resolution in mm
        x_offset: Grid origin X offset in mm
        y_offset: Grid origin Y offset in mm

    Returns:
        Number of off-grid pads.
    """
    off_grid = 0
    for pad in pad_list:
        x_on = _is_on_grid_with_offset(pad.x, resolution, x_offset)
        y_on = _is_on_grid_with_offset(pad.y, resolution, y_offset)
        if not (x_on and y_on):
            off_grid += 1
    return off_grid


def _find_optimal_origin_offset(
    pad_list: list,
    resolution: float,
) -> tuple[float, float]:
    """Find the grid origin offset that maximizes on-grid pad count.

    For a given resolution ``r``, each pad coordinate has a residue
    ``coord % r``.  Clustering these residues reveals the offset that
    places the most pads on-grid.

    The algorithm:
    1. For each axis, compute residues ``coord % resolution`` for every pad.
    2. For each unique residue, count how many pads would be on-grid if
       the grid origin were shifted to that residue.
    3. Pick the residue with the highest on-grid count.

    This is O(P^2) in the worst case (P = number of pads) but P is
    typically < 500 for real boards, so it runs in microseconds.

    Args:
        pad_list: List of pad objects with x, y attributes.
        resolution: Grid resolution in mm.

    Returns:
        (x_offset, y_offset) in mm that maximizes on-grid pad count.
    """
    if not pad_list or resolution <= 0:
        return (0.0, 0.0)

    threshold = resolution / 10

    def best_offset_for_axis(coords: list[float]) -> float:
        """Find the offset that places the most coordinates on-grid."""
        if not coords:
            return 0.0

        # Compute residues
        residues = [c % resolution for c in coords]

        # Try each residue as a candidate offset and count on-grid
        best_offset = 0.0
        best_count = 0

        # Also try offset 0.0 (no shift)
        candidates = [0.0] + residues

        for candidate in candidates:
            count = 0
            for r in residues:
                # Distance between residue and candidate, wrapped
                diff = abs(r - candidate)
                diff = min(diff, resolution - diff)
                if diff <= threshold:
                    count += 1
            if count > best_count:
                best_count = count
                best_offset = candidate

        return best_offset

    x_coords = [p.x for p in pad_list]
    y_coords = [p.y for p in pad_list]

    x_off = best_offset_for_axis(x_coords)
    y_off = best_offset_for_axis(y_coords)

    return (round(x_off, 6), round(y_off, 6))


def _compute_gcd_grid_candidates(
    pad_list: list[Pad] | list[PadPosition],
    min_grid: float = 0.005,
) -> list[float]:
    """Compute GCD-based grid candidates from pad spacings.

    Collects all unique x and y coordinates, computes deltas between
    consecutive sorted values, converts to integer microns to avoid
    floating-point issues, and returns the GCD plus simple multiples
    as candidate grid resolutions.

    Args:
        pad_list: List of pad objects with x, y attributes.
        min_grid: Minimum grid resolution in mm (default: 0.005mm).
                  GCD values below this are discarded.

    Returns:
        List of GCD-derived candidate resolutions in mm (may be empty).
    """
    if len(pad_list) < 2:
        return []

    # Collect unique x and y coordinates, rounded to nearest 5um
    # to absorb floating-point noise
    xs = sorted({round(p.x / 0.005) * 0.005 for p in pad_list})
    ys = sorted({round(p.y / 0.005) * 0.005 for p in pad_list})

    # Compute deltas between consecutive sorted coordinates
    deltas_mm: list[float] = []
    for coords in (xs, ys):
        for i in range(1, len(coords)):
            delta = coords[i] - coords[i - 1]
            if delta > 0.001:  # Ignore near-zero deltas
                deltas_mm.append(delta)

    if not deltas_mm:
        return []

    # Convert to integer microns (multiply by 1000) for integer GCD
    deltas_um = [round(d * 1000) for d in deltas_mm]
    # Filter out zeros after rounding
    deltas_um = [d for d in deltas_um if d > 0]

    if not deltas_um:
        return []

    # Compute GCD of all deltas
    gcd_um = deltas_um[0]
    for d in deltas_um[1:]:
        gcd_um = math.gcd(gcd_um, d)

    gcd_mm = gcd_um / 1000.0

    # Generate candidates: GCD itself plus 2x and 5x multiples
    raw_candidates = [gcd_mm, gcd_mm * 2, gcd_mm * 5]

    # Filter: must be >= min_grid to avoid absurdly fine grids
    result = [c for c in raw_candidates if c >= min_grid]

    # Deduplicate and sort descending
    result = sorted(set(result), reverse=True)

    return result


def _compute_zone_resolution_and_offset(
    comp_pads: list,
    coarse_resolution: float,
    min_fine_resolution: float = 0.005,
) -> tuple[float, float, float]:
    """Find the coarsest fine-grid (resolution, x_offset, y_offset) for a component.

    Searches a series of candidate fine-grid resolutions (coarsest first)
    and, for each, finds an origin offset that maximizes on-grid coverage
    of *this component's* pads.  Returns the coarsest resolution for which
    ALL of the component's pads are on-grid with the chosen offset.  If no
    candidate achieves full coverage, the finest candidate is returned with
    its best-fit offset.

    This is the pad-position-aware refinement for issue #2837: rather than
    deriving a fine resolution heuristically from ``min_pad_delta / 10``,
    we pick a (resolution, offset) pair that actually aligns the
    component's pads.  This is essential for off-grid components like J1
    USB-C (half-grid offsets) and Y1 crystals (sub-0.05mm spacing) that
    the previous heuristic could not handle.

    Args:
        comp_pads: List of Pad objects belonging to the component.
        coarse_resolution: The coarse global grid resolution in mm.  The
            chosen fine resolution will be strictly finer than this (a
            fine zone at the coarse resolution would be redundant).
        min_fine_resolution: Floor for the fine grid resolution (mm).
            Below this, the candidate is rejected as impractical.

    Returns:
        Tuple of (fine_resolution_mm, x_offset_mm, y_offset_mm).  When the
        component has fewer than 2 pads, returns
        ``(min_fine_resolution, 0.0, 0.0)`` as a conservative default.
    """
    if not comp_pads:
        return (min_fine_resolution, 0.0, 0.0)

    # Build candidate resolutions: fixed grid-fraction values plus
    # GCD-derived candidates from this component's pad spacings.  Coarsest
    # values come first so we prefer the cheapest fine zone that works.
    fixed_candidates = [0.1, 0.05, 0.04, 0.025, 0.02, 0.0125, 0.01]
    gcd_candidates = _compute_gcd_grid_candidates(comp_pads, min_grid=min_fine_resolution)
    raw_candidates = sorted(
        {round(c, 6) for c in (fixed_candidates + gcd_candidates)},
        reverse=True,
    )

    # Keep only candidates strictly finer than the coarse grid and at or
    # above the minimum floor.  A fine zone at >= coarse_resolution would
    # not refine anything (the coarse grid would suffice).
    candidates = [c for c in raw_candidates if c < coarse_resolution and c >= min_fine_resolution]

    if not candidates:
        # Fall back to half the coarse grid floored at the minimum.  This
        # preserves the legacy behaviour when the component genuinely
        # can't be refined below the coarse grid.
        return (max(coarse_resolution / 2.0, min_fine_resolution), 0.0, 0.0)

    # Walk candidates coarsest -> finest, returning the first (R, O) that
    # places *all* of the component's pads on-grid.  This minimises the
    # fine zone's cell count while still being pad-position-aware.
    best_finest: tuple[float, float, float, int] | None = None
    for res in candidates:
        offset = _find_optimal_origin_offset(comp_pads, res)
        off_grid = _count_off_grid_with_offset(comp_pads, res, offset[0], offset[1])
        if off_grid == 0:
            return (res, offset[0], offset[1])
        # Track best-effort fallback (lowest off-grid count, prefer coarser
        # at equal off-grid count).
        if best_finest is None or off_grid < best_finest[3]:
            best_finest = (res, offset[0], offset[1], off_grid)

    # No candidate achieved 0 off-grid; return the best-effort result.
    if best_finest is not None:
        return (best_finest[0], best_finest[1], best_finest[2])

    # Defensive fallback (should be unreachable given the candidate list).
    return (min_fine_resolution, 0.0, 0.0)


def auto_select_grid_resolution(
    pads: list[Pad] | list[PadPosition] | dict[tuple[str, str], Pad],
    clearance: float,
    board_width: float | None = None,
    board_height: float | None = None,
    max_cells: int = 500_000,
    candidates: list[float] | None = None,
) -> GridAutoSelection:
    """Automatically select optimal grid resolution based on pad positions.

    Analyzes pad positions to find a grid resolution that minimizes off-grid
    pads while respecting DRC constraints and memory limits.

    Args:
        pads: List of Pad or PadPosition objects, or dict mapping (ref, pin) to Pad
        clearance: Required trace clearance in mm (for DRC compliance)
        board_width: Board width in mm (for memory constraint check)
        board_height: Board height in mm (for memory constraint check)
        max_cells: Maximum grid cells to allow (default: 500k for performance)
        candidates: Optional list of candidate resolutions to try.
                   Default: [0.5, 0.25, 0.127, 0.1, 0.065, 0.05, 0.0508]
                   plus GCD-derived candidates from pad spacings.
                   When candidates is None, GCD-based candidates are
                   automatically computed from pad positions and added
                   to the fixed list.  This handles packages like
                   SSOP/TSSOP with 0.65mm pitch whose pads don't align
                   to any standard grid size.

    Returns:
        GridAutoSelection with the chosen resolution and analysis details.

    Example:
        >>> from kicad_tools.router import auto_select_grid_resolution, Pad
        >>> pads = [Pad(x=2.54, y=5.08, ...), Pad(x=1.27, y=2.54, ...)]
        >>> result = auto_select_grid_resolution(pads, clearance=0.15)
        >>> print(f"Use grid: {result.resolution}mm")
        Use grid: 0.127mm
        >>> print(result.summary())
    """
    # Convert dict to list if needed
    if isinstance(pads, dict):
        pad_list: list[Pad] = list(pads.values())
    else:
        pad_list = list(pads)

    total_pads = len(pad_list)

    # Default candidate resolutions (common PCB grid values in mm)
    # These are chosen to align with common footprint pitches:
    #   - 0.5mm: QFP (0.5mm pitch), 0805/0603 (1.0mm pitch)
    #   - 0.25mm: QFP (0.5mm / 0.25 = 2 exact). NOT imperial-compatible
    #             (2.54 / 0.25 = 10.16, off-grid by 0.04mm)
    #   - 0.127mm (5 mil): SOIC (1.27mm / 0.127 = 10 exact),
    #             imperial THT (2.54mm / 0.127 = 20, 5.08mm / 0.127 = 40)
    #   - 0.1mm: Metric footprints, QFP (0.5mm / 0.1 = 5)
    #   - 0.065mm: TSSOP (0.65mm / 0.065 = 10 exact)
    #   - 0.05mm: Good metric alignment but NOT imperial-compatible
    #             (2.54 / 0.05 = 50.8, off-grid by 0.04mm)
    #   - 0.0508mm (2 mil): Imperial-compatible for tight DRC constraints
    #             (2.54 / 0.0508 = 50 exact, 5.08 / 0.0508 = 100 exact)
    if candidates is None:
        candidates = [0.5, 0.25, 0.127, 0.1, 0.065, 0.05, 0.0508]

        # Add GCD-derived candidates from pad spacings.  This handles
        # packages like SSOP/TSSOP whose 0.65mm pitch doesn't align to
        # any fixed candidate.  The GCD of all pad-to-pad spacings
        # (computed in integer microns to avoid floating-point issues)
        # naturally produces a grid that places every pad on-grid.
        gcd_candidates = _compute_gcd_grid_candidates(pad_list)
        for gc in gcd_candidates:
            if gc not in candidates:
                candidates.append(gc)

    # Sort candidates from coarsest to finest
    candidates = sorted(candidates, reverse=True)

    # Calculate minimum resolution for DRC compliance fallback when nothing
    # else fits.  A grid <= clearance is sufficient: per-axis quantisation
    # error is at most resolution/2, and the negotiated router enforces
    # actual edge-to-edge clearance during pathing — so pad alignment, not
    # the half-clearance quantisation worst case, is the dominant signal
    # (issue #2387).
    min_resolution = clearance / 2

    # Filter candidates: must be DRC-compliant (grid <= clearance is
    # sufficient because the router enforces edge-to-edge clearance
    # directly).  Previously this used clearance/2 which excluded pad-aligned
    # grids like 0.1mm at clearance=0.15mm and forced selection of finer,
    # misaligned grids that placed most pads off-grid (issue #2387).
    valid_candidates = [c for c in candidates if c <= clearance]

    if not valid_candidates:
        # All candidates are too coarse, use minimum DRC-compliant resolution
        valid_candidates = [min_resolution]

    # Further filter by memory constraint if board dimensions provided
    memory_capped = False
    pre_memory_candidates = list(valid_candidates)
    effective_max_cells = max_cells

    def _apply_memory_filter(cands: list[float], budget: int) -> tuple[list[float], bool]:
        """Apply memory filter to a candidate list with the given budget.

        Returns (filtered_candidates, capped_flag).
        """
        if board_width is None or board_height is None:
            return cands, False
        board_area_local = board_width * board_height
        memory_valid_local = [res for res in cands if board_area_local / (res * res) <= budget]
        if memory_valid_local:
            return memory_valid_local, len(memory_valid_local) < len(cands)
        # All are too memory-intensive, keep the coarsest DRC-compliant one
        if cands:
            return [cands[0]], True
        return cands, False

    valid_candidates, memory_capped = _apply_memory_filter(valid_candidates, effective_max_cells)

    # Memoized off-grid analysis per candidate resolution (used by both
    # the issue #3441 lattice rescue below and the main analysis loop).
    # Returns (off_grid_count, origin_offset) using the optimal origin
    # offset search when zero-offset alignment is imperfect.
    _off_grid_cache: dict[float, tuple[int, tuple[float, float]]] = {}

    def _off_grid_for(resolution: float) -> tuple[int, tuple[float, float]]:
        cached = _off_grid_cache.get(resolution)
        if cached is not None:
            return cached
        off_no_offset = _count_off_grid_with_offset(pad_list, resolution, 0.0, 0.0)
        if off_no_offset == 0:
            result = (0, (0.0, 0.0))
        else:
            offset = _find_optimal_origin_offset(pad_list, resolution)
            off_with_offset = _count_off_grid_with_offset(
                pad_list, resolution, offset[0], offset[1]
            )
            # Keep zero offset if it's equal or better (simpler)
            if off_no_offset <= off_with_offset:
                result = (off_no_offset, (0.0, 0.0))
            else:
                result = (off_with_offset, offset)
        _off_grid_cache[resolution] = result
        return result

    # ------------------------------------------------------------------
    # Issue #3239: clearance-aware retry.
    #
    # The DRC filter above accepts any candidate ``<= clearance`` (issue
    # #2387 relaxation), but a grid coarser than ``clearance / 2`` is at
    # higher risk of clearance violations because the router's discrete
    # quantisation can shift traces by up to ``resolution / 2`` per axis.
    # If the memory cap forced the selector to a coarse-but-DRC-compliant
    # grid AND a finer ``<= clearance/2`` candidate was available before
    # the memory filter, try a one-shot budget bump (up to 4M cells, the
    # same ceiling ``compute_multi_resolution_plan`` already uses).
    #
    # This makes the memory budget self-correcting: instead of silently
    # picking a clearance-risky grid and letting the downstream warning
    # in ``validate_grid_resolution`` paper over the regression, the
    # auto-selector bumps the budget when (and only when) doing so unlocks
    # a clearance-safe grid.  If even 4M cells can't reach ``clearance/2``,
    # we keep the coarser-but-DRC-compliant selection -- the caller's
    # ``validate_grid_resolution`` still fires the original warning so
    # nothing is silently lost, plus we emit our own actionable warning
    # naming the memory cap as the cause.
    # ------------------------------------------------------------------
    BUMP_CEILING = 4_000_000  # Matches compute_multi_resolution_plan ceiling
    recommended_max = clearance / 2
    bumped = False
    if (
        memory_capped
        and board_width is not None
        and board_height is not None
        and valid_candidates
        and min(valid_candidates) > recommended_max
        and any(c <= recommended_max for c in pre_memory_candidates)
    ):
        # The memory cap is excluding candidates that would satisfy
        # clearance/2.  Try a budget bump.
        bumped_budget = min(max(max_cells * 4, max_cells + 1), BUMP_CEILING)
        if bumped_budget > effective_max_cells:
            bumped_candidates, bumped_capped = _apply_memory_filter(
                pre_memory_candidates, bumped_budget
            )
            if bumped_candidates and min(bumped_candidates) <= recommended_max:
                # The bump unlocked a clearance-safe grid -- adopt it.
                old_cells = effective_max_cells
                valid_candidates = bumped_candidates
                memory_capped = bumped_capped
                effective_max_cells = bumped_budget
                bumped = True
                logger.info(
                    "Auto-grid: bumped memory cap from %s to %s cells "
                    "to keep grid <= %.4fmm (clearance/2 = %.4f/2). "
                    "Reason: memory cap was forcing a clearance-risky grid.",
                    f"{old_cells:,}",
                    f"{bumped_budget:,}",
                    recommended_max,
                    clearance,
                )

    # ------------------------------------------------------------------
    # Issue #3441: lattice rescue.
    #
    # The #3239 bump above only adopts a bumped candidate set when it
    # reaches ``clearance/2`` -- a board-lattice-aligned grid that is
    # merely ``<= clearance`` (e.g. 0.1mm at clearance 0.15mm) can never
    # be rescued by it, even when the memory-capped selection puts the
    # vast majority of pads off-grid (board 07: auto-grid picked
    # 0.127mm with 190/244 pads off-grid while the excluded 0.1mm
    # candidate had only 53 -- the genuinely off-lattice BGA-49 at
    # 1.27mm pitch).
    #
    # Because the memory filter runs BEFORE the off-grid vote, the vote
    # never even sees the lattice-aligned candidate.  Fix: when the
    # clearance/2 bump did not fire, retry the budget bump and adopt it
    # iff it unlocks a candidate that (a) is already known to be DRC-
    # compliant (every pre_memory candidate passed the ``<= clearance``
    # filter, which is sufficient because the router enforces edge-to-
    # edge clearance during pathing -- issue #2387), (b) strictly
    # reduces the off-grid pad count vs. the best memory-capped
    # candidate, and (c) places >= 90% of pads ON-grid (the issue's
    # "dominant lattice" threshold).
    #
    # The 90% dominance bar is empirical, not cosmetic: board 07 has
    # 78% of pads on the 0.1mm lattice, but the off-lattice remainder
    # is a BGA-49 at 1.27mm pitch that aligns EXACTLY to 0.127mm
    # (1.27/0.127 = 10) -- routing it at 0.1mm measured 26/31 nets vs
    # 28/31 at 0.127mm (2026-06-10, this issue's worktree), because the
    # six TMDS nets terminating on the off-grid BGA row congest each
    # other once their pads need escape stubs.  A mixed-lattice board
    # is NOT a lattice-rescue candidate; only a genuinely dominant
    # lattice (>= 90% of pads, per the issue's own framing) justifies
    # trading a 4x memory bump for the alignment win.
    # ------------------------------------------------------------------
    lattice_rescued = False
    if (
        not bumped
        and memory_capped
        and board_width is not None
        and board_height is not None
        and valid_candidates
        and total_pads > 0
    ):
        bumped_budget = min(max(max_cells * 4, max_cells + 1), BUMP_CEILING)
        if bumped_budget > effective_max_cells:
            bumped_candidates, bumped_capped = _apply_memory_filter(
                pre_memory_candidates, bumped_budget
            )
            newly_unlocked = [c for c in bumped_candidates if c not in valid_candidates]
            if newly_unlocked:
                current_best_off = min(_off_grid_for(c)[0] for c in valid_candidates)
                # Prefer the unlocked candidate with the fewest off-grid
                # pads; break ties toward the coarser grid (fewer cells).
                unlocked_best = min(newly_unlocked, key=lambda c: (_off_grid_for(c)[0], -c))
                unlocked_off = _off_grid_for(unlocked_best)[0]
                if unlocked_off < current_best_off and unlocked_off * 10 <= total_pads:
                    old_cells = effective_max_cells
                    valid_candidates = bumped_candidates
                    memory_capped = bumped_capped
                    effective_max_cells = bumped_budget
                    lattice_rescued = True
                    logger.info(
                        "Auto-grid: lattice rescue bumped memory cap from "
                        "%s to %s cells -- %.4fmm aligns the board's "
                        "dominant pad lattice (%d/%d pads off-grid vs %d "
                        "at the memory-capped selection). Grid is > "
                        "clearance/2 (%.4fmm) but <= clearance (%.4fmm), "
                        "which the router's edge-to-edge clearance "
                        "enforcement accepts (issue #2387).",
                        f"{old_cells:,}",
                        f"{bumped_budget:,}",
                        unlocked_best,
                        unlocked_off,
                        total_pads,
                        current_best_off,
                        recommended_max,
                        clearance,
                    )

    # Analyze off-grid pads for each candidate, using optimal origin offset.
    # For each candidate resolution, we find the grid origin offset that
    # maximizes on-grid pad count.  This handles mixed metric/imperial boards
    # where no single zero-origin grid aligns with all pad pitches.
    candidates_tried: list[tuple[float, int]] = []
    best_resolution = valid_candidates[0]
    best_off_grid = total_pads  # Worst case
    best_offset: tuple[float, float] = (0.0, 0.0)

    for resolution in valid_candidates:
        # Memoized off-grid analysis (zero-offset quick check + optimal
        # origin offset search) -- shared with the #3441 lattice rescue.
        off_grid_count, offset = _off_grid_for(resolution)

        candidates_tried.append((resolution, off_grid_count))

        # Track best resolution (prefer coarser if equal off-grid count)
        if off_grid_count < best_off_grid:
            best_off_grid = off_grid_count
            best_resolution = resolution
            best_offset = offset
        elif off_grid_count == best_off_grid and resolution > best_resolution:
            # Prefer coarser resolution when off-grid counts are equal
            best_resolution = resolution
            best_offset = offset

    off_grid_pct = (best_off_grid / total_pads * 100) if total_pads > 0 else 0.0

    # Determine what the uncapped resolution would have been
    uncapped_resolution: float | None = None
    if memory_capped:
        # Find the finest candidate that was filtered out by memory budget
        # (pre_memory_candidates is sorted coarsest-first)
        uncapped_resolution = pre_memory_candidates[-1]

    # Issue #3239: if the memory cap (even after any bump) forced a grid
    # coarser than clearance/2 AND there was a finer candidate available
    # before memory filtering, emit an actionable warning that names the
    # specific knob the user can turn.  This is more informative than the
    # generic "may cause clearance violations" warning from
    # validate_grid_resolution -- it tells the user that the memory budget,
    # not the candidate list, is the binding constraint.
    if (
        memory_capped
        and best_resolution > recommended_max
        and any(c <= recommended_max for c in pre_memory_candidates)
    ):
        if lattice_rescued:
            # Issue #3441: the lattice rescue *chose* this grid because it
            # aligns the board's dominant pad lattice; it is <= clearance
            # (DRC-sufficient per #2387) but > clearance/2, so keep an
            # honest -- and accurate -- heads-up rather than the "forced"
            # phrasing of the generic memory-cap warning.
            warnings.warn(
                f"Auto-grid: lattice rescue selected grid {best_resolution}mm "
                f"(> clearance/2 = {recommended_max}mm, <= clearance = "
                f"{clearance}mm) because it aligns the board's dominant pad "
                f"lattice ({best_off_grid}/{total_pads} pads off-grid). "
                f"Quantisation margin is reduced at fine-pitch pads; the "
                f"router's edge-to-edge clearance enforcement is the "
                f"backstop.",
                stacklevel=2,
            )
        else:
            warnings.warn(
                f"Auto-grid: memory budget cap forces grid {best_resolution}mm > "
                f"clearance/2 ({recommended_max}mm) even at "
                f"max_cells={effective_max_cells:,}. Routing may produce clearance "
                f"violations at fine-pitch pads. Increase max_cells (currently "
                f"capped at {BUMP_CEILING:,} by the auto-grid retry) or use a "
                f"looser manufacturer profile.",
                stacklevel=2,
            )

    return GridAutoSelection(
        resolution=best_resolution,
        off_grid_pads=best_off_grid,
        total_pads=total_pads,
        off_grid_percentage=off_grid_pct,
        candidates_tried=candidates_tried,
        memory_capped=memory_capped,
        uncapped_resolution=uncapped_resolution,
        origin_offset=best_offset,
        clearance_compliant_at_clearance_over_2=best_resolution <= recommended_max,
        memory_budget_used=effective_max_cells,
        lattice_rescued=lattice_rescued,
    )


def compute_multi_resolution_plan(
    pads: list[Pad] | list[PadPosition] | dict[tuple[str, str], Pad],
    clearance: float,
    board_width: float | None = None,
    board_height: float | None = None,
    max_cells: int = 2_000_000,
    zone_padding: float = 2.0,
    min_fine_resolution: float = 0.05,
    fine_pitch_threshold: float = 0.8,
    off_grid_escalation_threshold: float = 10.0,
    min_off_grid_pads_to_escalate: int = 2,
    min_escalation_fine_resolution: float = 0.005,
) -> MultiResolutionGridPlan | None:
    """Compute a multi-resolution grid plan for adaptive routing.

    Analyzes pad positions to determine if a multi-resolution approach is
    beneficial. Returns a plan with coarse global grid and per-component
    fine zones when fine-pitch components are detected OR when the uniform
    grid leaves any structurally-significant cluster of pads off-grid.

    Returns None if all components are coarse-pitch and the uniform grid
    already places every pad on-grid (uniform grid is optimal).

    Args:
        pads: Pad objects or positions
        clearance: Required trace clearance in mm
        board_width: Board width in mm
        board_height: Board height in mm
        max_cells: Maximum total cell budget across all zones
        zone_padding: Padding around component bbox for fine zones (mm)
        min_fine_resolution: Minimum fine grid resolution floor (mm) for
            the fine-pitch path.  Default 0.05mm avoids generating absurdly
            fine zones for on-grid fine-pitch components (e.g. TQFP-32
            with 0.8mm pitch already at 0.1mm-aligned positions).
        fine_pitch_threshold: Pitch below this triggers fine-grid zone
        off_grid_escalation_threshold: When off-grid percentage exceeds
            this value after origin-offset optimization, escape zones are
            created for off-grid pad clusters even if they are not
            fine-pitch.  Default 10.0% (lowered from 50.0% in #2837 so
            small-but-real off-grid clusters like USB-C half-grid pads
            and crystals are caught instead of warned-about).
        min_off_grid_pads_to_escalate: Absolute floor on the number of
            off-grid pads required to trigger escalation independent of
            the percentage threshold.  Prevents spurious bumps from a
            single mis-snapped pad while still catching small clusters
            (e.g. a 2-pad crystal off by 0.010mm).  Default 2.
        min_escalation_fine_resolution: Floor for the escalation path's
            fine resolution (mm).  Lower than ``min_fine_resolution`` so
            the solver can find a (resolution, offset) that aligns
            sub-0.05mm pad coordinates (issue #2837).  Default 0.005mm.

    Returns:
        MultiResolutionGridPlan if fine-pitch components detected or
        off-grid escalation triggered, else None.
    """
    from .adaptive_grid import identify_fine_pitch_components

    # Convert to appropriate format
    if isinstance(pads, dict):
        pad_list: list = list(pads.values())
        pad_dict = pads
    else:
        pad_list = list(pads)
        pad_dict = None

    if not pad_list:
        return None

    # Run uniform selection first to get coarse resolution
    uniform_result = auto_select_grid_resolution(
        pads=pad_list,
        clearance=clearance,
        board_width=board_width,
        board_height=board_height,
    )
    coarse_resolution = uniform_result.resolution

    # Check if we have Pad objects (needed for identify_fine_pitch_components)
    # PadPosition objects don't have ref attribute
    has_ref = hasattr(pad_list[0], "ref") if pad_list else False
    if not has_ref:
        # Can't identify fine-pitch components without ref info
        return None

    # Identify fine-pitch components
    fine_components = identify_fine_pitch_components(
        pad_list,
        coarse_resolution=coarse_resolution,
        fine_pitch_threshold=fine_pitch_threshold,
    )

    # Escalation: when the uniform grid still leaves any meaningful cluster
    # of pads off-grid after origin-offset optimisation, create escape zones
    # for the off-grid components -- even if they are not fine-pitch in the
    # min-pad-pitch sense.  This handles mixed metric/imperial boards, USB-C
    # half-grid pads, and crystals whose pad positions don't divide evenly
    # into any common coarse grid (issue #2837).
    #
    # The escalation fires when EITHER:
    #   - the off-grid percentage exceeds ``off_grid_escalation_threshold``,
    #     OR
    #   - the absolute off-grid pad count is at or above
    #     ``min_off_grid_pads_to_escalate``.
    # The threshold path catches large clusters; the absolute floor catches
    # small but structurally-critical components (e.g. a 2-pad crystal).
    pad_position_offsets: dict[str, tuple[float, float]] = {}
    if has_ref and uniform_result.off_grid_pads > 0:
        # Find components with off-grid pads at the chosen resolution+offset
        off_grid_refs: dict[str, list] = {}
        offset = uniform_result.origin_offset
        for pad in pad_list:
            ref = getattr(pad, "ref", None)
            if not ref:
                continue
            x_on = _is_on_grid_with_offset(pad.x, coarse_resolution, offset[0])
            y_on = _is_on_grid_with_offset(pad.y, coarse_resolution, offset[1])
            if not (x_on and y_on):
                if ref not in off_grid_refs:
                    off_grid_refs[ref] = []
                off_grid_refs[ref].append(pad)

        # Decide whether the escalation actually fires.  We escalate if the
        # percentage threshold is exceeded OR if the absolute off-grid pad
        # count meets the minimum cluster size.  Both gates together prevent
        # spurious escalation on boards whose pads are already aligned
        # (uniform_result.off_grid_pads == 0 short-circuits above).
        should_escalate = (
            uniform_result.off_grid_percentage >= off_grid_escalation_threshold
            or uniform_result.off_grid_pads >= min_off_grid_pads_to_escalate
        )

        if should_escalate and off_grid_refs:
            # Add these components as needing fine-grid zones using the
            # pad-position-aware solver introduced in #2837.  For each
            # off-grid component we pick the coarsest fine resolution plus
            # an origin offset that puts ALL of its pads on-grid.
            #
            # Skip single-pad components: a fine zone around a single point
            # has nothing to refine -- the existing sub-grid escape routing
            # already handles isolated off-grid pads.  This preserves
            # parity with the pre-#2837 behaviour where ``if deltas`` was
            # the gate that filtered out single-pad off-grid refs (e.g.
            # mounting holes MH1-MH4 on board 05).
            escalated = 0
            for ref, ref_pads in off_grid_refs.items():
                if len(ref_pads) < 2:
                    continue
                fine_res, x_off, y_off = _compute_zone_resolution_and_offset(
                    ref_pads,
                    coarse_resolution=coarse_resolution,
                    min_fine_resolution=min_escalation_fine_resolution,
                )
                # The new solver supersedes any heuristic value that
                # identify_fine_pitch_components produced earlier -- it
                # actually checks pad-position alignment.
                fine_components[ref] = fine_res
                pad_position_offsets[ref] = (x_off, y_off)
                escalated += 1

            logger.info(
                "Off-grid escalation: %d/%d pads (%.1f%%) off-grid at "
                "%.3fmm coarse; creating fine zones for %d component(s)",
                uniform_result.off_grid_pads,
                uniform_result.total_pads,
                uniform_result.off_grid_percentage,
                coarse_resolution,
                escalated,
            )

    if not fine_components:
        # No fine-pitch components and off-grid is acceptable
        return None

    # Build fine zones from component bboxes
    # Group pads by component reference
    by_ref: dict[str, list] = {}
    for pad in pad_list:
        ref = getattr(pad, "ref", None)
        if ref and ref in fine_components:
            if ref not in by_ref:
                by_ref[ref] = []
            by_ref[ref].append(pad)

    fine_zones: list[FineZone] = []
    for ref, comp_pads in by_ref.items():
        if not comp_pads:
            continue

        # Compute bbox
        xs = [p.x for p in comp_pads]
        ys = [p.y for p in comp_pads]
        x_min = min(xs) - zone_padding
        y_min = min(ys) - zone_padding
        x_max = max(xs) + zone_padding
        y_max = max(ys) + zone_padding

        # Fine resolution for this component.  Escalation components use
        # the lower ``min_escalation_fine_resolution`` floor so the solver
        # can express the sub-0.05mm grids needed for half-grid USB-C and
        # crystal pads (issue #2837).  Plain fine-pitch components keep the
        # higher ``min_fine_resolution`` floor that avoids absurdly fine
        # zones for on-grid SOIC/TSSOP/QFP packages.
        fine_res = fine_components[ref]
        if ref in pad_position_offsets:
            fine_res = max(fine_res, min_escalation_fine_resolution)
        else:
            fine_res = max(fine_res, min_fine_resolution)

        # Per-zone origin offset.  If the escalation branch produced a
        # pad-position-aware (resolution, offset) tuple, use it.  Otherwise
        # default to (0, 0) which matches the legacy behaviour for plain
        # fine-pitch components whose pad positions already align to a
        # zero-origin fine grid (TSSOP, SOIC, etc.).
        x_off, y_off = pad_position_offsets.get(ref, (0.0, 0.0))

        fine_zones.append(
            FineZone(
                ref=ref,
                x_min=x_min,
                y_min=y_min,
                x_max=x_max,
                y_max=y_max,
                resolution=fine_res,
                x_offset=x_off,
                y_offset=y_off,
            )
        )

    if not fine_zones:
        return None

    # Estimate total cells
    total_cells = 0
    for zone in fine_zones:
        total_cells += zone.cell_count

    # Add coarse grid cells estimate
    if board_width and board_height:
        coarse_cols = int(board_width / coarse_resolution) + 1
        coarse_rows = int(board_height / coarse_resolution) + 1
        total_cells += coarse_cols * coarse_rows

    return MultiResolutionGridPlan(
        coarse_resolution=coarse_resolution,
        fine_zones=fine_zones,
        total_cell_estimate=total_cells,
        uniform_fallback=uniform_result.resolution,
    )


def recommend_grid_for_board_size(
    board_width: float,
    board_height: float,
    clearance: float = 0.15,
    small_board_threshold: tuple[float, float] = (100.0, 75.0),
    medium_board_threshold: tuple[float, float] = (150.0, 100.0),
) -> float:
    """Recommend a default grid resolution based on board dimensions.

    This function provides a starting point for grid selection based on
    board size and memory constraints. The recommendation balances:
    - Grid alignment with common footprint pitches (0.65mm TSSOP, 0.5mm QFP)
    - Memory usage (finer grids use more memory)
    - DRC compliance (grid must be <= clearance)

    Board size categories:
    - Small (<100x75mm): 0.05mm grid (best pitch alignment, ~3M cells max)
    - Medium (<150x100mm): 0.1mm grid (good balance, ~1.5M cells max)
    - Large (>=150x100mm): 0.25mm grid (memory efficient, ~240k cells max)

    Common footprint pitch alignment:
    | Grid    | TSSOP 0.65mm | QFP 0.5mm | SOIC 1.27mm | 100mil 2.54mm |
    |---------|--------------|-----------|-------------|---------------|
    | 0.05mm  | 13 exact     | 10 exact  | 25.4 (off)  | 50.8 (off)    |
    | 0.1mm   | 6.5 (off)    | 5 exact   | 12.7 (off)  | 25.4 (off)    |
    | 0.127mm | 5.12 (off)   | 3.94 (off)| 10 exact    | 20 exact      |
    | 0.25mm  | 2.6 (off)    | 2 exact   | 5.08 (off)  | 10.16 (off)   |

    Note: No single grid aligns with both metric (TSSOP/QFP) and imperial
    (SOIC/THT) pitches. Use auto_select_grid_resolution() for pad-aware
    selection that picks the best grid for the actual board contents.

    Args:
        board_width: Board width in mm
        board_height: Board height in mm
        clearance: Trace clearance in mm (grid must not exceed this)
        small_board_threshold: (width, height) below which board is "small"
        medium_board_threshold: (width, height) below which board is "medium"

    Returns:
        Recommended grid resolution in mm.

    Example:
        >>> grid = recommend_grid_for_board_size(65, 56, clearance=0.127)
        >>> print(f"Use {grid}mm grid")  # Small board: 0.05mm
        Use 0.05mm grid

        >>> grid = recommend_grid_for_board_size(200, 120, clearance=0.15)
        >>> print(f"Use {grid}mm grid")  # Large board: 0.15mm (clamped to clearance)
        Use 0.15mm grid
    """
    # Determine board size category
    is_small = board_width <= small_board_threshold[0] and board_height <= small_board_threshold[1]
    is_medium = (
        board_width <= medium_board_threshold[0] and board_height <= medium_board_threshold[1]
    )

    # Recommend grid based on size
    if is_small:
        # Small boards: finest grid for best footprint pitch alignment
        recommended = 0.05
    elif is_medium:
        # Medium boards: balanced grid
        recommended = 0.1
    else:
        # Large boards: coarser grid for memory efficiency
        recommended = 0.25

    # Grid resolution must not exceed clearance for DRC compliance
    return min(recommended, clearance)


@dataclass
class PadPosition:
    """Lightweight pad position for grid analysis."""

    x: float
    y: float


def extract_board_dimensions(pcb_path_or_text: str | Path) -> tuple[float, float] | None:
    """Extract board width and height from a KiCad PCB file.

    Parses the Edge.Cuts gr_rect to determine board dimensions. This is a
    lightweight extraction that avoids full PCB parsing.

    Args:
        pcb_path_or_text: Path to .kicad_pcb file or PCB file contents

    Returns:
        Tuple of (width_mm, height_mm) or None if no board outline found.

    Example:
        >>> dims = extract_board_dimensions("board.kicad_pcb")
        >>> if dims:
        ...     width, height = dims
        ...     print(f"Board: {width}mm x {height}mm")
    """
    # Read file if path provided
    if isinstance(pcb_path_or_text, Path):
        pcb_text = pcb_path_or_text.read_text()
    elif not pcb_path_or_text.startswith("("):
        # Looks like a path string
        pcb_text = Path(pcb_path_or_text).read_text()
    else:
        pcb_text = pcb_path_or_text

    edge_match = re.search(
        r"\(gr_rect\s+\(start\s+([\d.]+)\s+([\d.]+)\)\s+\(end\s+([\d.]+)\s+([\d.]+)\)",
        pcb_text,
    )
    if edge_match:
        x1, y1, x2, y2 = map(float, edge_match.groups())
        return (abs(x2 - x1), abs(y2 - y1))
    return None


def extract_board_origin(pcb_path_or_text: str | Path) -> tuple[float, float] | None:
    """Extract board outline origin (bottom-left corner) from a KiCad PCB file.

    Issue #3352 (P_AS4): companion to :func:`extract_board_dimensions`.
    Used by the auto-pcb-size escalation loop to normalise a recipe's
    mounting-hole-group anchor against the board outline origin -- KiCad's
    default origin is ``(100, 100)``, but a hole group declared at
    ``anchor=(5, 5)`` in the spec typically means "5 mm in from the
    envelope's bottom-left corner", not "absolute board coord (5, 5)".

    Parses the Edge.Cuts gr_rect to find the start coordinate, then
    returns ``(min_x, min_y)`` -- the bottom-left corner of the outline.

    Args:
        pcb_path_or_text: Path to .kicad_pcb file or PCB file contents.

    Returns:
        ``(origin_x, origin_y)`` in mm, or ``None`` if no board outline
        gr_rect is detected.
    """
    if isinstance(pcb_path_or_text, Path):
        pcb_text = pcb_path_or_text.read_text()
    elif not pcb_path_or_text.startswith("("):
        pcb_text = Path(pcb_path_or_text).read_text()
    else:
        pcb_text = pcb_path_or_text

    edge_match = re.search(
        r"\(gr_rect\s+\(start\s+([\d.]+)\s+([\d.]+)\)\s+\(end\s+([\d.]+)\s+([\d.]+)\)",
        pcb_text,
    )
    if edge_match:
        x1, y1, x2, y2 = map(float, edge_match.groups())
        return (min(x1, x2), min(y1, y2))
    return None


def extract_pad_positions(pcb_path_or_text: str | Path) -> list[PadPosition]:
    """Extract pad positions from a KiCad PCB file for grid analysis.

    This is a lightweight alternative to load_pcb_for_routing when you only
    need pad positions (e.g., for auto_select_grid_resolution).

    Args:
        pcb_path_or_text: Path to .kicad_pcb file or PCB file contents

    Returns:
        List of PadPosition objects with x, y coordinates.

    Example:
        >>> positions = extract_pad_positions("board.kicad_pcb")
        >>> result = auto_select_grid_resolution(positions, clearance=0.15)
    """
    # Read file if path provided
    if isinstance(pcb_path_or_text, Path):
        pcb_text = pcb_path_or_text.read_text()
    elif not pcb_path_or_text.startswith("("):
        # Looks like a path string
        pcb_text = Path(pcb_path_or_text).read_text()
    else:
        pcb_text = pcb_path_or_text

    positions: list[PadPosition] = []

    # Split by footprint for easier parsing
    footprint_sections = re.split(r"(?=\(footprint\s)", pcb_text)

    for section in footprint_sections:
        if not section.startswith("(footprint"):
            continue

        # Get footprint position and rotation
        at_match = re.search(r"\(at\s+([-\d.]+)\s+([-\d.]+)(?:\s+([-\d.]+))?\)", section)
        if not at_match:
            continue

        fp_x = float(at_match.group(1))
        fp_y = float(at_match.group(2))
        fp_rot = float(at_match.group(3)) if at_match.group(3) else 0

        # Precompute rotation values
        rot_rad = math.radians(fp_rot)
        cos_r, sin_r = math.cos(rot_rad), math.sin(rot_rad)

        # Find all pad blocks
        pad_blocks = _extract_pad_blocks(section)

        for pad_block in pad_blocks:
            # Extract at position
            pad_at = re.search(r"\(at\s+([-\d.]+)\s+([-\d.]+)", pad_block)
            if not pad_at:
                continue

            pad_x = float(pad_at.group(1))
            pad_y = float(pad_at.group(2))

            # Transform to absolute position
            abs_x = fp_x + pad_x * cos_r - pad_y * sin_r
            abs_y = fp_y + pad_x * sin_r + pad_y * cos_r

            positions.append(PadPosition(x=abs_x, y=abs_y))

    return positions


def load_pads_for_analysis(pcb_path_or_text: str | Path) -> list[Pad]:
    """Extract pad objects with ref/pin info for grid analysis.

    Unlike extract_pad_positions which only returns (x, y), this returns full
    Pad objects with component reference and pin information needed for
    identify_fine_pitch_components() and multi-resolution grid planning.

    Args:
        pcb_path_or_text: Path to .kicad_pcb file or PCB file contents

    Returns:
        List of Pad objects with x, y, ref, pin, net, and layer info.
    """
    from .primitives import Pad as PadObj

    # Read file if path provided
    if isinstance(pcb_path_or_text, Path):
        pcb_text = pcb_path_or_text.read_text()
    elif not pcb_path_or_text.startswith("("):
        pcb_text = Path(pcb_path_or_text).read_text()
    else:
        pcb_text = pcb_path_or_text

    pads: list[Pad] = []

    # Split by footprint for easier parsing
    footprint_sections = re.split(r"(?=\(footprint\s)", pcb_text)

    for section in footprint_sections:
        if not section.startswith("(footprint"):
            continue

        # Get footprint library name (e.g. "Package_QFP:TQFP-32_7x7mm_P0.8mm")
        footprint_name_match = re.search(r'\(footprint\s+"([^"]*)"', section)
        footprint_name = footprint_name_match.group(1) if footprint_name_match else ""

        # Get footprint reference
        ref_match = re.search(r'\(fp_text\s+reference\s+"?([^"\s)]+)"?', section)
        ref = ref_match.group(1) if ref_match else ""

        # Get footprint position and rotation
        at_match = re.search(r"\(at\s+([-\d.]+)\s+([-\d.]+)(?:\s+([-\d.]+))?\)", section)
        if not at_match:
            continue

        fp_x = float(at_match.group(1))
        fp_y = float(at_match.group(2))
        fp_rot = float(at_match.group(3)) if at_match.group(3) else 0

        # Precompute rotation values
        rot_rad = math.radians(fp_rot)
        cos_r, sin_r = math.cos(rot_rad), math.sin(rot_rad)

        # Find all pad blocks
        pad_blocks = _extract_pad_blocks(section)

        for pad_block in pad_blocks:
            # Extract pad number/pin
            pin_match = re.search(r'\(pad\s+"?([^"\s)]+)"?', pad_block)
            pin = pin_match.group(1) if pin_match else ""

            # Extract pad type for through_hole detection
            is_thru = "thru_hole" in pad_block

            # Extract at position
            pad_at = re.search(r"\(at\s+([-\d.]+)\s+([-\d.]+)", pad_block)
            if not pad_at:
                continue

            pad_x = float(pad_at.group(1))
            pad_y = float(pad_at.group(2))

            # Transform to absolute position
            abs_x = fp_x + pad_x * cos_r - pad_y * sin_r
            abs_y = fp_y + pad_x * sin_r + pad_y * cos_r

            # Extract pad size
            size_match = re.search(r"\(size\s+([-\d.]+)\s+([-\d.]+)\)", pad_block)
            width = float(size_match.group(1)) if size_match else 0.3
            height = float(size_match.group(2)) if size_match else 0.3

            # Rotate pad dimensions to PCB space (same fix as load_pcb_for_routing)
            pad_rot_match = re.search(r"\(at\s+[-\d.]+\s+[-\d.]+\s+([-\d.]+)\)", pad_block)
            pad_rot = float(pad_rot_match.group(1)) if pad_rot_match else 0.0
            total_rot = (fp_rot + pad_rot) % 360
            if abs(total_rot - 90) < 1 or abs(total_rot - 270) < 1:
                width, height = height, width

            # Extract net
            net_match = re.search(r"\(net\s+(\d+)", pad_block)
            net_num = int(net_match.group(1)) if net_match else 0

            # Extract net name
            net_name_match = re.search(r'\(net\s+\d+\s+"?([^"\)]+)"?\)', pad_block)
            net_name = net_name_match.group(1).strip() if net_name_match else ""

            # Determine layer
            layer = Layer.F_CU
            if "B.Cu" in pad_block and "F.Cu" not in pad_block:
                layer = Layer.B_CU

            pads.append(
                PadObj(
                    x=abs_x,
                    y=abs_y,
                    width=width,
                    height=height,
                    net=net_num,
                    net_name=net_name,
                    ref=ref,
                    pin=pin,
                    layer=layer,
                    through_hole=is_thru,
                    footprint_name=footprint_name,
                )
            )

    return pads


def _get_pair_clearance(
    net_a: int,
    net_b: int,
    default_clearance: float,
    net_names: dict[int, str],
    net_class_map: dict[str, NetClassRouting] | None,
) -> float:
    """Resolve the effective clearance between two nets.

    Uses the KiCad rule: ``max(net_a_class.clearance, net_b_class.clearance)``.
    Falls back to *default_clearance* when the net class map is unavailable or
    a net is not found in it.
    """
    if net_class_map is None:
        return default_clearance
    name_a = net_names.get(net_a, "")
    name_b = net_names.get(net_b, "")
    class_a = net_class_map.get(name_a)
    class_b = net_class_map.get(name_b)
    clearance_a = class_a.clearance if class_a else default_clearance
    clearance_b = class_b.clearance if class_b else default_clearance
    return max(clearance_a, clearance_b)


def validate_routes(
    router: Autorouter,
    rules: DesignRules | None = None,
) -> list[ClearanceViolation]:
    """Validate routed traces for potential clearance violations.

    Checks segment-to-pad, segment-to-segment, and segment-to-via clearances
    for all routed traces. Reports violations with net names and coordinates.

    Args:
        router: Autorouter instance with completed routes
        rules: DesignRules to check against (uses router.rules if None)

    Returns:
        List of potential ClearanceViolation issues.

    Note:
        This is a lightweight pre-save validation. For comprehensive DRC,
        export the PCB and run KiCad's built-in DRC checker.
    """
    if rules is None:
        rules = router.rules

    violations: list[ClearanceViolation] = []
    clearance = rules.trace_clearance
    via_clear = rules.via_clearance
    net_names = getattr(router, "net_names", {})

    # Resolve per-net-class clearance map (if available on the router)
    ncm: dict[str, NetClassRouting] | None = getattr(router, "net_class_map", None)

    def _resolve_net_name(net_id: int) -> str:
        return net_names.get(net_id, f"Net {net_id}")

    # Issue #3545: NET-AWARE component_inherent classification.  A
    # same-component FOREIGN-net pad violation is only "inherent" (and
    # thus filtered from ``drc_verify_and_nudge`` repair) when a
    # clearance relaxation is actually in effect for the component:
    # fine-pitch / explicit override (#1764) or a relaxed same-component
    # corridor (#2452).  Standard-pitch components (e.g. a 2.54mm THT
    # connector) get no relaxation, so a trace from one of their nets
    # grazing a sibling pad on another net is a repairable routing
    # defect, not component geometry.
    _relaxed_refs = getattr(getattr(router, "grid", None), "_relaxed_clearance_refs", None) or set()
    try:
        _pitches: dict[str, float] = router.component_pitches
    except Exception:
        _pitches = {}

    def _same_component_relaxation_active(ref: str) -> bool:
        if ref in _relaxed_refs:
            return True
        pitch = _pitches.get(ref)
        if rules.get_clearance_for_component(ref, pitch) < clearance:
            return True
        # Fine-pitch leg: boards routed with ``fine_pitch_clearance``
        # unset (the default) get no per-component relaxation signal,
        # but sub-clearance proximity on a fine-pitch footprint is
        # still forced by the component geometry -- inherent, not a
        # repairable routing defect.  Mirrors
        # ``RoutingGrid._same_component_carveout_active``.
        threshold = getattr(rules, "fine_pitch_threshold", None)
        return pitch is not None and threshold is not None and pitch < threshold

    # Check each route segment against pads of different nets
    for route_idx, route in enumerate(router.routes):
        route_net = route.net

        for seg_idx, segment in enumerate(route.segments):
            seg_half_width = segment.width / 2

            # --- Segment-to-pad checks ---
            # Build set of component refs that this route's net connects to
            route_component_refs: set[str] = set()
            if route_net in router.nets:
                for r, _p in router.nets[route_net]:
                    route_component_refs.add(r)

            for (ref, num), pad in router.pads.items():
                # Skip pads on the same net
                if pad.net == route_net:
                    continue

                # Skip truly unconnected pads -- pads with net == 0 AND no
                # net name are unconnected mechanical pads / pour leftovers
                # and do not represent copper that needs clearance from
                # routed traces.
                #
                # Issue #2757: pads with net == 0 but a non-empty net_name
                # are obstacles from *skipped* pour nets (GND, +3V3, +1V2,
                # etc.) -- ``load_pcb_for_routing`` rewrites their net to 0
                # so the autorouter doesn't try to route them, but they
                # are real pad copper that segments must keep clearance
                # from.  Surfacing these as violations lets
                # ``drc_verify_and_nudge`` repair them and lets the
                # ``format_clearance_violations`` summary report them so
                # users can see "trace XYZ grazes U2.A1 (GND)" instead of
                # silently emitting a routed PCB that fails ``kct check``.
                if pad.net == 0 and not pad.net_name:
                    continue

                # Skip SMD pads on a different layer than the segment
                if not pad.through_hole and pad.layer != segment.layer:
                    continue

                # Calculate minimum distance from the segment to the
                # pad's true axis-aligned rectangle.  Issue #3592: the
                # previous model treated every pad as a circle of radius
                # ``max(width, height) / 2``, which over-estimated the
                # extent of long, thin SMD pads (e.g. an LQFP land at
                # 1.475 x 0.3 mm) along their short axis by ~0.6 mm and
                # produced false-positive ``[pad]`` clearance violations
                # for traces that legally clear the rectangular copper.
                # Pad ``width``/``height`` are already rotated into PCB
                # space at load time, so the bounding box is correct.
                effective_dist = (
                    _segment_to_aabb_distance(
                        segment.x1,
                        segment.y1,
                        segment.x2,
                        segment.y2,
                        pad.x,
                        pad.y,
                        pad.width / 2,
                        pad.height / 2,
                    )
                    - seg_half_width
                )

                # For skipped-pour-net pads, look up the clearance under the
                # named net (GND, +3V3, ...) rather than net 0, so the
                # per-class clearance map is honoured.  Falls through to
                # ``clearance`` when no class match is found.
                pad_class_clear = clearance
                if pad.net == 0 and pad.net_name and ncm is not None:
                    pad_class = ncm.get(pad.net_name)
                    if pad_class is not None:
                        pad_class_clear = pad_class.clearance
                pair_clear = max(
                    _get_pair_clearance(route_net, pad.net, clearance, net_names, ncm),
                    pad_class_clear,
                )

                if effective_dist < pair_clear - _CLEARANCE_EPSILON_MM:
                    # Detect component-inherent violations: obstacle pad is
                    # on the same component as a pad in the route's net.
                    #
                    # Issue #2757: for skipped-pour-net pads (e.g. U2 BGA
                    # GND pads when the route is USB3_RX2+ on U2), the pad
                    # IS on the same component but it's still a real
                    # routing-clearance defect (the trace can be re-routed
                    # to avoid the pad).  Mark these as non-inherent so
                    # ``drc_verify_and_nudge`` (which filters out
                    # ``component_inherent=True``) attempts repair.
                    # Issue #3545: additionally require an active
                    # clearance relaxation for the component (fine-pitch
                    # #1764 or relaxed corridor #2452); otherwise the
                    # violation is actionable for the nudge pass.
                    is_component_inherent = (
                        ref in route_component_refs
                        and not (pad.net == 0 and pad.net_name)
                        and _same_component_relaxation_active(ref)
                    )

                    violations.append(
                        ClearanceViolation(
                            segment_index=seg_idx,
                            x1=segment.x1,
                            y1=segment.y1,
                            x2=segment.x2,
                            y2=segment.y2,
                            net=route_net,
                            obstacle_type="pad",
                            obstacle_net=pad.net,
                            distance=effective_dist,
                            required=pair_clear,
                            net_name=_resolve_net_name(route_net),
                            obstacle_net_name=(
                                pad.net_name
                                if pad.net == 0 and pad.net_name
                                else _resolve_net_name(pad.net)
                            ),
                            location=(pad.x, pad.y),
                            component_inherent=is_component_inherent,
                            layer=segment.layer,
                        )
                    )

            # --- Segment-to-segment checks ---
            # Check against segments from other routes on the same layer.
            # Only check routes with higher index to avoid duplicate violations.
            for other_route_idx, other_route in enumerate(router.routes):
                if other_route_idx <= route_idx or other_route.net == route_net:
                    continue

                for other_seg in other_route.segments:
                    # Skip segments on different layers
                    if other_seg.layer != segment.layer:
                        continue

                    dist = _segment_to_segment_distance(
                        segment.x1,
                        segment.y1,
                        segment.x2,
                        segment.y2,
                        other_seg.x1,
                        other_seg.y1,
                        other_seg.x2,
                        other_seg.y2,
                    )

                    # Edge-to-edge clearance (both segment half-widths)
                    other_half_width = other_seg.width / 2
                    effective_dist = dist - seg_half_width - other_half_width

                    pair_clear = _get_pair_clearance(
                        route_net, other_route.net, clearance, net_names, ncm
                    )

                    if effective_dist < pair_clear - _CLEARANCE_EPSILON_MM:
                        # Approximate violation location at midpoint of closest approach
                        loc_x = (segment.x1 + segment.x2 + other_seg.x1 + other_seg.x2) / 4
                        loc_y = (segment.y1 + segment.y2 + other_seg.y1 + other_seg.y2) / 4
                        violations.append(
                            ClearanceViolation(
                                segment_index=seg_idx,
                                x1=segment.x1,
                                y1=segment.y1,
                                x2=segment.x2,
                                y2=segment.y2,
                                net=route_net,
                                obstacle_type="segment",
                                obstacle_net=other_route.net,
                                distance=effective_dist,
                                required=pair_clear,
                                net_name=_resolve_net_name(route_net),
                                obstacle_net_name=_resolve_net_name(other_route.net),
                                location=(loc_x, loc_y),
                                layer=segment.layer,
                            )
                        )

            # --- Segment-to-via checks ---
            # Include pre-existing routes so new segments are checked against old vias.
            _all_routes_for_via = itertools.chain(
                router.routes, getattr(router, "existing_routes", [])
            )
            for other_route in _all_routes_for_via:
                if other_route.net == route_net:
                    continue

                for via in other_route.vias:
                    via_radius = via.diameter / 2

                    dist = _point_to_segment_distance(
                        via.x, via.y, segment.x1, segment.y1, segment.x2, segment.y2
                    )

                    effective_dist = dist - seg_half_width - via_radius

                    if effective_dist < via_clear - _CLEARANCE_EPSILON_MM:
                        violations.append(
                            ClearanceViolation(
                                segment_index=seg_idx,
                                x1=segment.x1,
                                y1=segment.y1,
                                x2=segment.x2,
                                y2=segment.y2,
                                net=route_net,
                                obstacle_type="via",
                                obstacle_net=other_route.net,
                                distance=effective_dist,
                                required=via_clear,
                                net_name=_resolve_net_name(route_net),
                                obstacle_net_name=_resolve_net_name(other_route.net),
                                location=(via.x, via.y),
                                layer=segment.layer,
                            )
                        )

    # --- Via-to-pad checks ---
    # Include pre-existing routes so old vias are checked against pads.
    for route in itertools.chain(router.routes, getattr(router, "existing_routes", [])):
        route_net = route.net

        # Build component refs connected to this route's net
        route_component_refs: set[str] = set()
        if route_net in router.nets:
            for r, _p in router.nets[route_net]:
                route_component_refs.add(r)

        for via in route.vias:
            via_radius = via.diameter / 2

            for (ref, num), pad in router.pads.items():
                if pad.net == route_net:
                    continue

                # Skip truly unconnected pads.  Pads on skipped pour nets
                # (Issue #2757) have net=0 but a non-empty net_name and ARE
                # real copper obstacles.
                if pad.net == 0 and not pad.net_name:
                    continue

                # Issue #3592: model the pad as its true axis-aligned
                # rectangle rather than a bounding circle so anisotropic
                # SMD lands do not produce false-positive via-clearance
                # violations along their short axis.
                effective_dist = (
                    _point_to_aabb_distance(
                        via.x, via.y, pad.x, pad.y, pad.width / 2, pad.height / 2
                    )
                    - via_radius
                )

                if effective_dist < via_clear - _CLEARANCE_EPSILON_MM:
                    # Issue #2757: cross-net pad on the same component is
                    # only "inherent" when it's a true net obstacle, not
                    # when the pad belongs to a skipped pour net (the trace
                    # / via can be re-routed to avoid it).
                    # Issue #3545: additionally require an active
                    # clearance relaxation for the component (fine-pitch
                    # #1764 or relaxed corridor #2452); otherwise the
                    # violation is actionable for the nudge pass.
                    is_component_inherent = (
                        ref in route_component_refs
                        and not (pad.net == 0 and pad.net_name)
                        and _same_component_relaxation_active(ref)
                    )

                    violations.append(
                        ClearanceViolation(
                            segment_index=-1,
                            x1=via.x,
                            y1=via.y,
                            x2=via.x,
                            y2=via.y,
                            net=route_net,
                            obstacle_type="pad",
                            obstacle_net=pad.net,
                            distance=effective_dist,
                            required=via_clear,
                            net_name=_resolve_net_name(route_net),
                            obstacle_net_name=(
                                pad.net_name
                                if pad.net == 0 and pad.net_name
                                else _resolve_net_name(pad.net)
                            ),
                            location=(pad.x, pad.y),
                            component_inherent=is_component_inherent,
                        )
                    )

    # --- Via-to-via checks ---
    for i, route_a in enumerate(router.routes):
        for route_b in router.routes[i + 1 :]:
            if route_a.net == route_b.net:
                continue

            for via_a in route_a.vias:
                for via_b in route_b.vias:
                    dist = math.sqrt((via_a.x - via_b.x) ** 2 + (via_a.y - via_b.y) ** 2)
                    effective_dist = dist - via_a.diameter / 2 - via_b.diameter / 2

                    if effective_dist < via_clear - _CLEARANCE_EPSILON_MM:
                        loc_x = (via_a.x + via_b.x) / 2
                        loc_y = (via_a.y + via_b.y) / 2
                        violations.append(
                            ClearanceViolation(
                                segment_index=-1,
                                x1=via_a.x,
                                y1=via_a.y,
                                x2=via_a.x,
                                y2=via_a.y,
                                net=route_a.net,
                                obstacle_type="via",
                                obstacle_net=route_b.net,
                                distance=effective_dist,
                                required=via_clear,
                                net_name=_resolve_net_name(route_a.net),
                                obstacle_net_name=_resolve_net_name(route_b.net),
                                location=(loc_x, loc_y),
                            )
                        )

    # --- Via-to-via checks: new routes vs pre-existing routes ---
    existing_routes = getattr(router, "existing_routes", [])
    for route_a in router.routes:
        for route_b in existing_routes:
            if route_a.net == route_b.net:
                continue

            for via_a in route_a.vias:
                for via_b in route_b.vias:
                    dist = math.sqrt((via_a.x - via_b.x) ** 2 + (via_a.y - via_b.y) ** 2)
                    effective_dist = dist - via_a.diameter / 2 - via_b.diameter / 2

                    if effective_dist < via_clear - _CLEARANCE_EPSILON_MM:
                        loc_x = (via_a.x + via_b.x) / 2
                        loc_y = (via_a.y + via_b.y) / 2
                        violations.append(
                            ClearanceViolation(
                                segment_index=-1,
                                x1=via_a.x,
                                y1=via_a.y,
                                x2=via_a.x,
                                y2=via_a.y,
                                net=route_a.net,
                                obstacle_type="via",
                                obstacle_net=route_b.net,
                                distance=effective_dist,
                                required=via_clear,
                                net_name=_resolve_net_name(route_a.net),
                                obstacle_net_name=_resolve_net_name(route_b.net),
                                location=(loc_x, loc_y),
                            )
                        )

    # --- Differential-pair intra-clearance residual safety net (Issue #3040) ---
    # Phase B repair pass runs after route_all_with_diffpairs() but can
    # fail to fix a violation when the path is too constrained (no
    # alternative spacing exists).  Surface residual violations from
    # the DiffPairRouter's intra_clearance_violations() buffer as
    # ``obstacle_type="segment"`` ClearanceViolation records so the
    # standard CLI seg-seg-violation accounting picks them up and the
    # routed PCB cannot silently ship with intra-pair clearance
    # defects.
    #
    # We emit one ClearanceViolation per ``segment_violations`` triple
    # to preserve segment-level location data for downstream consumers
    # (drc_nudge, format_clearance_violations).  The ``component_inherent``
    # flag is left False so the standard accounting in route_cmd.py
    # counts these violations toward the non-zero exit code.
    diffpair_violations_buffer = []
    try:
        diffpair_violations_buffer = router.diffpair_intra_clearance_violations()
    except AttributeError:  # pragma: no cover - tests may stub router
        diffpair_violations_buffer = []

    for ipv in diffpair_violations_buffer:
        for p_seg, n_seg, clearance in ipv.segment_violations:
            # Midpoint of the closest-approach between the two segments,
            # for location reporting.
            mid_x = (p_seg.x1 + p_seg.x2 + n_seg.x1 + n_seg.x2) / 4.0
            mid_y = (p_seg.y1 + p_seg.y2 + n_seg.y1 + n_seg.y2) / 4.0
            violations.append(
                ClearanceViolation(
                    segment_index=-1,
                    x1=p_seg.x1,
                    y1=p_seg.y1,
                    x2=p_seg.x2,
                    y2=p_seg.y2,
                    net=p_seg.net,
                    obstacle_type="segment",
                    obstacle_net=n_seg.net,
                    distance=clearance,
                    required=ipv.expected_clearance_mm,
                    net_name=ipv.positive_net_name,
                    obstacle_net_name=ipv.negative_net_name,
                    location=(mid_x, mid_y),
                    component_inherent=False,
                    layer=p_seg.layer,
                )
            )

    # --- Segment-to-board-edge checks (Issue #2743) ---
    # Edge keepout violations are otherwise invisible to the post-route
    # nudge pass because they are produced by a separate validator
    # (``validate/rules/edge.py``).  Emit them here as
    # ``obstacle_type="edge"`` violations so ``drc_nudge.py`` can consume
    # them through the same dispatch path.
    edge_clearance = getattr(router, "_edge_clearance", None)
    edge_segments = getattr(router, "_edge_segments", None)
    if edge_clearance is not None and edge_clearance > 0 and edge_segments:
        for route in router.routes:
            route_net = route.net
            for seg_idx, segment in enumerate(route.segments):
                seg_half_width = segment.width / 2

                # Find the minimum distance from the *trace centerline* to
                # any edge segment, plus the corresponding closest point on
                # the outline.  We must use segment-to-segment distance
                # (not point-to-segment) so a trace running parallel to an
                # edge sees the perpendicular distance, not the endpoint-
                # to-endpoint distance.  We then locate the closest point
                # on the edge by sampling: project each segment endpoint
                # onto the edge and pick whichever projection is closest
                # to the trace as a whole.
                closest_dist = float("inf")
                closest_pt: tuple[float, float] | None = None
                for (ex1, ey1), (ex2, ey2) in edge_segments:
                    d = _segment_to_segment_distance(
                        segment.x1,
                        segment.y1,
                        segment.x2,
                        segment.y2,
                        ex1,
                        ey1,
                        ex2,
                        ey2,
                    )
                    if d < closest_dist:
                        closest_dist = d
                        # Find the closest point on this edge segment to
                        # the trace centerline.  We sample the trace at
                        # its endpoints and midpoint and pick the
                        # projection that yields the smallest distance.
                        best_local = float("inf")
                        best_pt: tuple[float, float] = (ex1, ey1)
                        edge_dx = ex2 - ex1
                        edge_dy = ey2 - ey1
                        edge_len_sq = edge_dx * edge_dx + edge_dy * edge_dy
                        sample_points = [
                            (segment.x1, segment.y1),
                            (segment.x2, segment.y2),
                            ((segment.x1 + segment.x2) / 2, (segment.y1 + segment.y2) / 2),
                        ]
                        for px, py in sample_points:
                            if edge_len_sq < 1e-12:
                                cx, cy = ex1, ey1
                            else:
                                t = ((px - ex1) * edge_dx + (py - ey1) * edge_dy) / edge_len_sq
                                t = max(0.0, min(1.0, t))
                                cx = ex1 + t * edge_dx
                                cy = ey1 + t * edge_dy
                            pt_dist = math.sqrt((cx - px) ** 2 + (cy - py) ** 2)
                            if pt_dist < best_local:
                                best_local = pt_dist
                                best_pt = (cx, cy)
                        closest_pt = best_pt

                # actual_clearance = distance_from_centerline - half_width
                actual_clearance = closest_dist - seg_half_width
                if actual_clearance < edge_clearance - _CLEARANCE_EPSILON_MM:
                    violations.append(
                        ClearanceViolation(
                            segment_index=seg_idx,
                            x1=segment.x1,
                            y1=segment.y1,
                            x2=segment.x2,
                            y2=segment.y2,
                            net=route_net,
                            obstacle_type="edge",
                            obstacle_net=0,
                            distance=actual_clearance,
                            required=edge_clearance,
                            net_name=_resolve_net_name(route_net),
                            obstacle_net_name="Edge.Cuts",
                            location=closest_pt,
                            layer=segment.layer,
                        )
                    )

    return violations


def format_clearance_violations(violations: list[ClearanceViolation]) -> str:
    """Format clearance violations as a human-readable summary.

    Separates routing-caused violations (reported as warnings) from
    component-inherent pad spacings (reported as informational).

    Args:
        violations: List of ClearanceViolation objects from validate_routes()

    Returns:
        Formatted string with violation summary, or empty string if no violations.
    """
    if not violations:
        return ""

    # Separate routing violations from component-inherent pad spacings
    routing_violations = [v for v in violations if not v.component_inherent]
    inherent_violations = [v for v in violations if v.component_inherent]

    lines: list[str] = []

    if routing_violations:
        lines.append(f"Found {len(routing_violations)} clearance violation(s):")

        # Group by obstacle type for summary
        by_type: dict[str, int] = {}
        for v in routing_violations:
            by_type[v.obstacle_type] = by_type.get(v.obstacle_type, 0) + 1

        for obs_type, count in sorted(by_type.items()):
            lines.append(f"  {obs_type}: {count}")

        # Show individual violations (limit to first 20 to avoid flooding output)
        max_detail = 20
        for i, v in enumerate(routing_violations[:max_detail]):
            net_label = v.net_name or f"Net {v.net}"
            obs_label = v.obstacle_net_name or f"Net {v.obstacle_net}"
            loc_str = ""
            if v.location:
                loc_str = f" at ({v.location[0]:.2f}, {v.location[1]:.2f})"
            layer_str = f" on {v.layer.kicad_name}" if v.layer is not None else ""
            lines.append(
                f"  [{v.obstacle_type}] {net_label} vs {obs_label}{loc_str}{layer_str}: "
                f"{v.distance:.3f}mm (required {v.required:.3f}mm)"
            )

        if len(routing_violations) > max_detail:
            lines.append(f"  ... and {len(routing_violations) - max_detail} more")

    if inherent_violations:
        lines.append(f"Info: {len(inherent_violations)} component-inherent pad spacing(s) excluded")

    return "\n".join(lines)


def _point_to_segment_distance(
    px: float, py: float, x1: float, y1: float, x2: float, y2: float
) -> float:
    """Calculate minimum distance from a point to a line segment."""
    return _geom_point_to_seg_dist(px, py, x1, y1, x2, y2)


def _segment_to_segment_distance(
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    x3: float,
    y3: float,
    x4: float,
    y4: float,
) -> float:
    """Calculate minimum distance between two line segments."""
    return _geom_seg_to_seg_dist(x1, y1, x2, y2, x3, y3, x4, y4)


def _point_to_aabb_distance(
    px: float, py: float, cx: float, cy: float, half_w: float, half_h: float
) -> float:
    """Minimum distance from a point to an axis-aligned rectangle.

    The rectangle is centred at ``(cx, cy)`` with half-extents
    ``half_w`` (X) and ``half_h`` (Y).  Returns 0.0 when the point is
    inside the rectangle.

    Issue #3592: the segment-to-pad clearance check previously modelled
    every pad as a CIRCLE of radius ``max(width, height) / 2``.  For a
    long, thin SMD pad (e.g. an LQFP land at 1.475 x 0.3 mm) that
    over-estimates the pad extent along the short axis by ~0.6 mm,
    producing false-positive clearance violations when a trace passes
    the pad's narrow side at a distance that is legal against the real
    rectangular copper.  Modelling the pad as its true axis-aligned
    bounding box (pad dimensions are already rotated into PCB space at
    load time, see ``load_pcb_for_routing``) removes that bias.
    """
    dx = max(abs(px - cx) - half_w, 0.0)
    dy = max(abs(py - cy) - half_h, 0.0)
    return math.hypot(dx, dy)


def _segment_to_aabb_distance(
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    cx: float,
    cy: float,
    half_w: float,
    half_h: float,
) -> float:
    """Signed centerline distance from a segment to an axis-aligned rect.

    Sign convention (matches ``validate.rules.clearance.
    _rect_segment_centerline_distance``):

    - **Positive** -- segment is entirely outside the rectangle.
    - **Zero**     -- segment touches/crosses the rectangle boundary.
    - **Negative** -- the segment centerline lies inside the rectangle;
      the magnitude is the deepest penetration (smallest distance to an
      escaping edge), so the post-route DRC summary can still flag
      "trace runs through pad metal" with a meaningful depth.

    Used by the segment-to-pad clearance check so that anisotropic pads
    are modelled by their true rectangular footprint rather than a
    bounding circle (issue #3592).
    """
    left, right = cx - half_w, cx + half_w
    bottom, top = cy - half_h, cy + half_h

    def _inside(px: float, py: float) -> bool:
        return left <= px <= right and bottom <= py <= top

    p1_in = _inside(x1, y1)
    p2_in = _inside(x2, y2)

    if p1_in and p2_in:
        # Whole centerline inside the rect -- report the deepest
        # (most negative) signed penetration along the segment.
        def _signed_depth(px: float, py: float) -> float:
            gap_x = max(px - right, left - px)
            gap_y = max(py - top, bottom - py)
            return max(gap_x, gap_y)

        deepest = min(_signed_depth(x1, y1), _signed_depth(x2, y2))
        steps = 32
        dx, dy = x2 - x1, y2 - y1
        for i in range(1, steps):
            t = i / steps
            d = _signed_depth(x1 + t * dx, y1 + t * dy)
            if d < deepest:
                deepest = d
        return deepest

    if p1_in != p2_in:
        # One endpoint inside, one outside -- the centerline crosses an
        # edge, so it touches the boundary.
        return 0.0

    # Both endpoints outside: the segment either clears the rectangle or
    # crosses straight through it.  Decompose the rectangle into its four
    # edges and take the minimum segment-to-edge distance; a crossing
    # produces distance 0 because the segment intersects an edge.
    corners = [
        (left, top),
        (right, top),
        (right, bottom),
        (left, bottom),
    ]
    best = math.inf
    for i in range(4):
        ax, ay = corners[i]
        bx, by = corners[(i + 1) % 4]
        d = _geom_seg_to_seg_dist(x1, y1, x2, y2, ax, ay, bx, by)
        if d < best:
            best = d
    return best


def detect_layer_stack(pcb_text: str) -> LayerStack:
    """Auto-detect layer stack configuration from a KiCad PCB file.

    Parses the PCB file to determine:
    1. How many copper layers exist (from the (layers ...) section)
    2. Which inner layers have zone fills (likely planes)

    For inner layers with power/ground zones, they are marked as PLANE layers
    and excluded from signal routing. This allows proper handling of common
    4-layer configurations where In1.Cu and In2.Cu are GND/PWR planes.

    Args:
        pcb_text: Contents of a .kicad_pcb file

    Returns:
        LayerStack configured for the detected layer count and plane assignments.

    Example:
        >>> pcb_text = Path("board.kicad_pcb").read_text()
        >>> stack = detect_layer_stack(pcb_text)
        >>> print(f"Detected: {stack.name} ({stack.num_layers} layers)")
    """
    # Parse the (layers ...) section to find copper layers
    copper_layers: list[tuple[int, str]] = []

    layers_match = re.search(r"\(layers\s+(.*?)\n\s*\)", pcb_text, re.DOTALL)
    if layers_match:
        layers_text = layers_match.group(1)
        # Match layer definitions like: (0 "F.Cu" signal) or (31 "B.Cu" signal)
        for layer_match in re.finditer(r'\((\d+)\s+"([^"]+\.Cu)"\s+(\w+)', layers_text):
            layer_num = int(layer_match.group(1))
            layer_name = layer_match.group(2)
            # Only include copper layers (*.Cu)
            copper_layers.append((layer_num, layer_name))

    # Sort by layer number to get correct order
    copper_layers.sort(key=lambda x: x[0])
    num_copper = len(copper_layers)

    if num_copper == 0:
        # Fallback to 2-layer if no layers found
        return LayerStack.two_layer()

    # Detect which layers have zone fills (likely planes)
    zone_layers: dict[str, str] = {}  # layer_name -> net_name

    # Parse zone definitions to find layers with fills
    for zone_match in re.finditer(
        r'\(zone\s+.*?\(net_name\s+"([^"]+)"\).*?\(layer\s+"([^"]+)"\)',
        pcb_text,
        re.DOTALL,
    ):
        net_name = zone_match.group(1)
        layer_name = zone_match.group(2)
        # Track the net for this layer (prefer GND/power nets as plane indicators)
        if layer_name.endswith(".Cu"):
            # If multiple zones on same layer, prefer power/GND nets
            existing = zone_layers.get(layer_name, "")
            if (
                not existing
                or net_name.upper() in ("GND", "GNDA", "GNDD")
                or existing.upper() not in ("GND", "GNDA", "GNDD")
                and any(c in net_name.upper() for c in ["+", "V", "PWR", "VCC", "VDD"])
            ):
                zone_layers[layer_name] = net_name

    # Build layer definitions based on detected configuration
    if num_copper <= 2:
        return LayerStack.two_layer()

    elif num_copper == 4:
        # 4-layer board - check if inner layers are planes
        inner_layers = [name for _, name in copper_layers if name not in ("F.Cu", "B.Cu")]

        # Check if inner layers have zones (power/ground planes)
        inner_zones = {name: zone_layers.get(name, "") for name in inner_layers}
        has_inner_planes = any(inner_zones.values())

        if has_inner_planes:
            # Inner layers are planes - use SIG-GND-PWR-SIG configuration
            layers = [
                LayerDefinition(
                    "F.Cu", 0, LayerType.SIGNAL, is_outer=True, reference_plane="In1.Cu"
                ),
            ]
            # Add inner layers as planes with detected net names
            in1_net = inner_zones.get("In1.Cu", "GND")
            in2_net = inner_zones.get("In2.Cu", "+3.3V")
            layers.append(LayerDefinition("In1.Cu", 1, LayerType.PLANE, plane_net=in1_net))
            layers.append(LayerDefinition("In2.Cu", 2, LayerType.PLANE, plane_net=in2_net))
            layers.append(
                LayerDefinition(
                    "B.Cu", 3, LayerType.SIGNAL, is_outer=True, reference_plane="In2.Cu"
                )
            )

            return LayerStack(
                name="4-Layer (auto-detected)",
                description="4-layer with inner planes (auto-detected from PCB zones)",
                layers=layers,
            )
        else:
            # No zones on inner layers - treat all as signal layers
            return LayerStack.four_layer_sig_sig_gnd_pwr()

    elif num_copper == 6:
        return LayerStack.six_layer_sig_gnd_sig_sig_pwr_sig()

    else:
        # Unsupported layer count - fall back to 2-layer
        # Could be extended to support 8+ layers in the future
        return LayerStack.two_layer()


def route_pcb(
    board_width: float,
    board_height: float,
    components: list[dict],
    net_map: dict[str, int],
    rules: DesignRules | None = None,
    origin_x: float = 0,
    origin_y: float = 0,
    skip_nets: list[str] | None = None,
    progress_callback: ProgressCallback | None = None,
) -> tuple[str, dict]:
    """
    Route a PCB given component placements and net assignments.

    Args:
        board_width: Board width in mm
        board_height: Board height in mm
        components: List of component dicts with:
            - ref: str (e.g., "U1")
            - x, y: float (placement position)
            - rotation: float (degrees)
            - pads: list of dicts with:
                - number: str (pad number)
                - x, y: float (relative to component center)
                - width, height: float
                - net: str (net name)
        net_map: Dict mapping net names to net numbers
        rules: DesignRules (optional)
        origin_x, origin_y: Board origin
        skip_nets: Net names to skip (e.g., ["GND", "+3.3V"] for plane nets)
        progress_callback: Optional callback for progress reporting.
            Signature: (progress: float, message: str, cancelable: bool) -> bool
            Returns False to cancel, True to continue.

    Returns:
        Tuple of (sexp_string, statistics_dict)
    """
    if rules is None:
        rules = DesignRules()

    skip_nets = skip_nets or []

    router = Autorouter(
        width=board_width,
        height=board_height,
        origin_x=origin_x,
        origin_y=origin_y,
        rules=rules,
    )

    # Add all component pads
    for comp in components:
        ref = comp["ref"]
        cx, cy = comp["x"], comp["y"]
        rotation = comp.get("rotation", 0)

        # Transform pad positions based on component placement.
        # KiCad stores rotation in degrees, positive = counter-clockwise;
        # the standard 2D rotation matrix applies directly (no negation).
        # See PCB.get_pad_position (schema/pcb.py) and the canonical
        # implementation later in this file (~line 2661) for reference.
        rot_rad = math.radians(rotation)
        cos_r, sin_r = math.cos(rot_rad), math.sin(rot_rad)

        pads: list[dict] = []
        for pad in comp.get("pads", []):
            # Rotate pad position around component center
            px, py = pad["x"], pad["y"]
            rx = px * cos_r - py * sin_r
            ry = px * sin_r + py * cos_r

            net_name = pad.get("net", "")
            if net_name in skip_nets:
                continue

            net_num = net_map.get(net_name, 0)
            if net_num == 0 and net_name:
                # Unknown net, assign a number
                net_num = len(net_map) + 1
                net_map[net_name] = net_num

            pads.append(
                {
                    "number": pad["number"],
                    "x": cx + rx,
                    "y": cy + ry,
                    "width": pad.get("width", 0.5),
                    "height": pad.get("height", 0.5),
                    "net": net_num,
                    "net_name": net_name,
                    "layer": Layer.F_CU,
                }
            )

        if pads:
            router.add_component(ref, pads)

    # Get all nets that need routing (exclude plane nets)
    nets_to_route: list[int] = []
    for net_name, net_num in net_map.items():
        if net_name and net_name not in skip_nets and net_num in router.nets:
            if len(router.nets[net_num]) >= 2:
                nets_to_route.append(net_num)

    # Route nets
    print(f"Autorouting {len(nets_to_route)} nets...")
    router.route_all(nets_to_route, progress_callback=progress_callback)

    # Run connectivity-aware cleanup before emitting S-expressions.
    # The cleanup is safe (it restores segments when removal would
    # fragment a net), but we still compute stats AFTER cleanup so
    # they reflect the data actually written to the file.
    router.cleanup_artifacts()
    sexp = router.to_sexp(skip_cleanup=True)
    stats = router.get_statistics()
    print(
        f"  Completed: {stats['routes']} routes, {stats['segments']} segments, {stats['vias']} vias"
    )

    return sexp, stats


def _extract_pad_blocks(section: str) -> list[str]:
    """
    Extract complete (pad ...) S-expression blocks from a footprint section.

    KiCad 7+ uses multi-line pad definitions like:
        (pad "1" smd roundrect
          (at -0.9500 0.9000)
          (size 0.6000 1.1000)
          ...
        )

    This function finds each (pad ...) block and extracts the complete
    content by counting parentheses to find the matching closing paren.

    Args:
        section: Footprint section text from a KiCad PCB file

    Returns:
        List of complete pad block strings
    """
    pad_blocks: list[str] = []

    # Find all positions where "(pad " starts
    start_pos = 0
    while True:
        pad_start = section.find("(pad ", start_pos)
        if pad_start == -1:
            break

        # Count parentheses to find the matching closing paren
        depth = 0
        in_string = False
        i = pad_start
        while i < len(section):
            char = section[i]

            if char == '"' and (i == 0 or section[i - 1] != "\\"):
                in_string = not in_string
            elif not in_string:
                if char == "(":
                    depth += 1
                elif char == ")":
                    depth -= 1
                    if depth == 0:
                        # Found the matching closing paren
                        pad_blocks.append(section[pad_start : i + 1])
                        break
            i += 1

        start_pos = i + 1

    return pad_blocks


def _extract_edge_segments(
    pcb_text: str,
) -> list[tuple[tuple[float, float], tuple[float, float]]]:
    """Extract board edge segments from Edge.Cuts layer.

    Parses gr_rect and gr_line elements on the Edge.Cuts layer to build
    a list of line segments defining the board outline.

    Args:
        pcb_text: Contents of a .kicad_pcb file

    Returns:
        List of ((x1, y1), (x2, y2)) tuples for each edge segment.
    """
    segments: list[tuple[tuple[float, float], tuple[float, float]]] = []

    # Look for gr_rect on Edge.Cuts (simple rectangular boards)
    # Use .*? with re.DOTALL to match nested parentheses in stroke/fill attributes
    for rect_match in re.finditer(
        r"\(gr_rect\s+\(start\s+([\d.]+)\s+([\d.]+)\)\s+\(end\s+([\d.]+)\s+([\d.]+)\)"
        r'.*?\(layer\s+"Edge\.Cuts"\)',
        pcb_text,
        re.DOTALL,
    ):
        x1, y1, x2, y2 = map(float, rect_match.groups())
        # Convert rectangle to 4 line segments
        segments.extend(
            [
                ((x1, y1), (x2, y1)),  # Top
                ((x2, y1), (x2, y2)),  # Right
                ((x2, y2), (x1, y2)),  # Bottom
                ((x1, y2), (x1, y1)),  # Left
            ]
        )

    # Also handle gr_rect where layer comes before coordinates
    for rect_match in re.finditer(
        r'\(gr_rect.*?\(layer\s+"Edge\.Cuts"\).*?'
        r"\(start\s+([\d.]+)\s+([\d.]+)\)\s*\(end\s+([\d.]+)\s+([\d.]+)\)",
        pcb_text,
        re.DOTALL,
    ):
        x1, y1, x2, y2 = map(float, rect_match.groups())
        segments.extend(
            [
                ((x1, y1), (x2, y1)),
                ((x2, y1), (x2, y2)),
                ((x2, y2), (x1, y2)),
                ((x1, y2), (x1, y1)),
            ]
        )

    # Look for gr_line elements on Edge.Cuts (complex board outlines)
    for line_match in re.finditer(
        r"\(gr_line\s+\(start\s+([\d.-]+)\s+([\d.-]+)\)\s+"
        r'\(end\s+([\d.-]+)\s+([\d.-]+)\).*?\(layer\s+"Edge\.Cuts"\)',
        pcb_text,
        re.DOTALL,
    ):
        x1, y1, x2, y2 = map(float, line_match.groups())
        segments.append(((x1, y1), (x2, y2)))

    return segments


def _install_fine_pitch_regions_from_components(
    router: Autorouter,
    components: list[dict],
) -> int:
    """Install fine-pitch escape regions on ``router.grid`` from ``components``.

    Issue #3371 / P_FP3 -- runs the fine-pitch escape region detector
    against the parsed component dicts BEFORE the autorouter calls
    ``add_component`` for each component.  This ensures the Python
    pathfinder halo (``_clearance_for_pin_pitch``) sees the in-region
    escape clearance when each pad's blocked envelope is computed.

    The C++ validator picks up the regions lazily via
    ``CppGrid.from_routing_grid`` (which reads
    ``grid.get_fine_pitch_regions()`` at construction time), so no
    additional C++ plumbing is required here.

    Detection is **unconditional** when the Q_FP1 recipe-relative
    trigger fires (a fine-pitch package's corridor is geometrically
    infeasible at the current ``rules.trace_clearance`` +
    ``rules.trace_width``).  When the detector finds no qualifying
    package the helper is a strict no-op.

    Manufacturer source: resolves through
    :meth:`DesignRules.manufacturer`.  When that is unset the
    detector still runs but the region's escape clearance defaults to
    ``rules.trace_clearance`` (no shrink) -- the route_cmd wrapper
    logs a warning when this fallback path engages.

    Args:
        router: Autorouter freshly constructed (no pads added yet).
            ``router.rules`` is consulted for the recipe parameters
            and manufacturer; ``router.grid`` is the install target.
        components: List of parsed component dicts.  Each entry has
            ``"ref"`` (str) and ``"pads"`` (list of pad-info dicts
            with ``x``, ``y``, ``width``, ``height``, ``net``,
            ``net_name``, ``through_hole``, ``drill``, ``layer``
            keys -- the same shape :meth:`Autorouter.add_component`
            consumes).

    Returns:
        Number of installed regions (``0`` when nothing qualifies).
    """
    # Defer imports to avoid module-load cycles.  ``fine_pitch_escape``
    # transitively imports ``escape``, which imports ``grid``, which is
    # safe but only after this module finishes its top-level imports.
    from .fine_pitch_escape import detect_fine_pitch_regions
    from .layers import Layer
    from .mfr_limits import get_mfr_limits
    from .primitives import Pad

    # Build synthetic Pad objects from the components dict.  The
    # detector only reads ``x``, ``y``, ``width``, ``height``, ``ref``,
    # ``pin`` -- net / layer fields are immaterial here.  We construct
    # a temporary list (not stored on the router) so the detector can
    # run before ``add_component`` lands the real pads.
    synth_pads: list[Pad] = []
    for comp in components:
        ref = comp["ref"]
        for pad_info in comp["pads"]:
            try:
                synth_pads.append(
                    Pad(
                        x=float(pad_info["x"]),
                        y=float(pad_info["y"]),
                        width=float(pad_info.get("width", 0.0)),
                        height=float(pad_info.get("height", 0.0)),
                        net=int(pad_info.get("net", 0)),
                        net_name=str(pad_info.get("net_name", "")),
                        layer=pad_info.get("layer", Layer.F_CU),
                        ref=ref,
                        pin=str(pad_info.get("number", "")),
                        through_hole=bool(pad_info.get("through_hole", False)),
                        drill=float(pad_info.get("drill", 0.0)),
                    )
                )
            except (TypeError, ValueError, KeyError):
                # Detector is best-effort; bad pad data should not
                # block routing entirely.  Skip the malformed pad.
                continue

    # Resolve manufacturer limits via ``rules.manufacturer``.  The
    # route_cmd-side CLI ``--manufacturer`` flag is merged into the
    # rules upstream of this entry point (it sets
    # ``rules.manufacturer``), so reading off rules captures both the
    # CLI and the recipe paths.
    mfr_limits = None
    mfr_name = getattr(router.rules, "manufacturer", None)
    if mfr_name:
        try:
            mfr_limits = get_mfr_limits(mfr_name)
        except (ValueError, KeyError):
            mfr_limits = None

    regions = detect_fine_pitch_regions(
        synth_pads,
        router.rules,
        mfr_limits=mfr_limits,
    )

    if not regions:
        return 0

    router.grid.set_fine_pitch_regions(regions)
    return len(regions)


def load_pcb_for_routing(
    pcb_path: str,
    skip_nets: list[str] | None = None,
    netlist: dict[str, str] | None = None,
    rules: DesignRules | None = None,
    use_pcb_rules: bool = True,
    validate_drc: bool = True,
    strict_drc: bool = True,
    auto_adjust_grid: bool = False,
    edge_clearance: float | None = None,
    layer_stack: LayerStack | None = None,
    force_python: bool = False,
    load_existing_routes: bool = False,
    max_search_iterations: int = 0,
) -> tuple[Autorouter, dict[str, int]]:
    """
    Load a KiCad PCB file and create an Autorouter with all components.

    Args:
        pcb_path: Path to .kicad_pcb file
        skip_nets: Net names to skip (e.g., ["GND", "+3.3V"] for plane nets)
        netlist: Optional dict mapping "REF.PIN" to net name (e.g., {"U1.1": "+3.3V"})
                 If provided, overrides any net assignments in the PCB file.
        rules: DesignRules for routing (grid resolution, trace width, etc.)
               If None and use_pcb_rules=True, extracts rules from PCB.
               If None and use_pcb_rules=False, uses default rules.
        use_pcb_rules: If True and rules=None, parse design rules from the PCB
                       file's setup section and use them as defaults.
        validate_drc: If True, validate grid resolution against clearance.
                      When strict_drc=True (default), raises GridResolutionError
                      if the grid resolution could cause DRC violations.
        strict_drc: If True (default), raise GridResolutionError when grid
                    resolution may cause clearance violations. This prevents
                    wasted routing time producing DRC-failing output. Set to
                    False for lenient mode that only fails on guaranteed
                    violations (grid > clearance) and warns for risky settings.
        auto_adjust_grid: If True, automatically adjust grid resolution to a
                         DRC-compliant value (clearance / 2) instead of failing.
                         When enabled, logs an INFO message about the adjustment.
                         Default is False for backward compatibility.
        edge_clearance: Copper-to-edge clearance in mm. If specified, blocks
                        routing within this distance of the board edge. Common
                        values are 0.25-0.5mm. If None, no edge clearance is
                        applied (default for backward compatibility).
        layer_stack: Layer stack configuration for routing. Controls how many
                     layers are available for routing and which layers are
                     planes vs signal layers. If None, auto-detects from the
                     PCB file's layer definitions. This ensures pad layers
                     match the available routing layers.
                     Use LayerStack.four_layer_sig_gnd_pwr_sig() for 4-layer
                     boards with GND/PWR planes, which routes signals on outer
                     layers (F.Cu, B.Cu) with vias for layer transitions.
        force_python: If True, force use of Python router backend even if the
                     C++ backend is available. Default False uses C++ when
                     available for 10-100x performance improvement.
        load_existing_routes: If True, parse existing ``(segment ...)`` and
                     ``(via ...)`` elements from the PCB file and mark them as
                     obstacles on the routing grid. This is essential for
                     multi-pass routing where Pass 2 must see Pass 1 geometry
                     to avoid overlapping traces and co-located vias.
                     Default is False for backward compatibility.

    Returns:
        Tuple of (Autorouter instance, net_map dict)

    Example:
        >>> # Use PCB's design rules automatically
        >>> router, nets = load_pcb_for_routing("board.kicad_pcb")
        >>>
        >>> # Override with custom rules
        >>> custom = DesignRules(grid_resolution=0.1, trace_width=0.15)
        >>> router, nets = load_pcb_for_routing("board.kicad_pcb", rules=custom)
        >>>
        >>> # Skip DRC validation warnings
        >>> router, nets = load_pcb_for_routing("board.kicad_pcb", validate_drc=False)
        >>>
        >>> # Auto-adjust grid resolution for DRC compliance
        >>> router, nets = load_pcb_for_routing("board.kicad_pcb", auto_adjust_grid=True)
        >>>
        >>> # Apply 0.5mm edge clearance
        >>> router, nets = load_pcb_for_routing("board.kicad_pcb", edge_clearance=0.5)
        >>>
        >>> # Use 4-layer stack with GND/PWR planes
        >>> from kicad_tools.router import LayerStack
        >>> stack = LayerStack.four_layer_sig_gnd_pwr_sig()
        >>> router, nets = load_pcb_for_routing("board.kicad_pcb", layer_stack=stack)
        >>>
        >>> # Load existing routes as obstacles for multi-pass routing
        >>> router, nets = load_pcb_for_routing("pass1.kicad_pcb",
        ...     load_existing_routes=True)
    """
    pcb_text = Path(pcb_path).read_text()
    skip_nets = skip_nets or []

    # Parse PCB design rules if needed
    pcb_rules: PCBDesignRules | None = None
    if rules is None and use_pcb_rules:
        pcb_rules = parse_pcb_design_rules(pcb_text)

    # Parse board dimensions from Edge.Cuts gr_rect
    edge_match = re.search(
        r"\(gr_rect\s+\(start\s+([\d.]+)\s+([\d.]+)\)\s+\(end\s+([\d.]+)\s+([\d.]+)\)",
        pcb_text,
    )
    if edge_match:
        x1, y1, x2, y2 = map(float, edge_match.groups())
        board_width = x2 - x1
        board_height = y2 - y1
        origin_x = x1
        origin_y = y1
    else:
        # Default HAT dimensions
        board_width = 65.0
        board_height = 56.0
        origin_x = 115.0
        origin_y = 75.0

    # Parse nets
    net_map: dict[str, int] = {}
    for match in re.finditer(r'\(net\s+(\d+)\s+"([^"]+)"\)', pcb_text):
        net_num, net_name = int(match.group(1)), match.group(2)
        if net_num > 0:
            net_map[net_name] = net_num

    # Parse footprints and their pads
    components: list[dict] = []

    # Split by footprint for easier parsing
    footprint_sections = re.split(r"(?=\(footprint\s)", pcb_text)

    for section in footprint_sections:
        if not section.startswith("(footprint"):
            continue

        # Get footprint position
        # Note: coordinates can be negative (footprints outside board origin)
        at_match = re.search(r"\(at\s+([-\d.]+)\s+([-\d.]+)(?:\s+([-\d.]+))?\)", section)
        if not at_match:
            continue

        fp_x = float(at_match.group(1))
        fp_y = float(at_match.group(2))
        fp_rot = float(at_match.group(3)) if at_match.group(3) else 0

        # Get reference - try KiCad 9 property format first, then old fp_text format
        ref_match = re.search(r'\(property\s+"Reference"\s+"([^"]+)"', section)
        if not ref_match:
            ref_match = re.search(r'\(fp_text\s+reference\s+"([^"]+)"', section)
        if not ref_match:
            continue
        ref = ref_match.group(1)

        # Parse pads - extract complete (pad ...) blocks
        # KiCad 7+ uses multi-line pad definitions, so we need to extract
        # complete S-expression blocks rather than parsing line-by-line
        pads: list[dict] = []

        # Find all complete (pad ...) blocks using parenthesis matching
        pad_blocks = _extract_pad_blocks(section)

        for pad_block in pad_blocks:
            # Extract pad number and type
            # Handle both quoted ("A1") and unquoted (1) pad numbers
            # KiCad uses unquoted numbers for numeric pads, quoted for alphanumeric (BGA)
            pad_start = re.match(r'\(pad\s+(?:"([^"]+)"|(\S+))\s+(\w+)', pad_block)
            if not pad_start:
                continue
            pad_num = pad_start.group(1) or pad_start.group(2)
            pad_type = pad_start.group(3)  # smd or thru_hole

            # Extract at position (now searches entire multi-line block)
            at_match = re.search(r"\(at\s+([-\d.]+)\s+([-\d.]+)", pad_block)
            if not at_match:
                continue
            pad_x = float(at_match.group(1))
            pad_y = float(at_match.group(2))

            # Extract size
            size_match = re.search(r"\(size\s+([\d.]+)\s+([\d.]+)\)", pad_block)
            if not size_match:
                continue
            pad_w = float(size_match.group(1))
            pad_h = float(size_match.group(2))

            # Extract net (if present)
            # KiCad 7/8: (net <number> "name"), KiCad 9+: (net "name")
            net_match = re.search(r'\(net\s+(\d+)\s+"([^"]+)"\)', pad_block)
            if net_match:
                net_num = int(net_match.group(1))
                net_name = net_match.group(2)
            else:
                # KiCad 9 name-only format
                net_name_match = re.search(r'\(net\s+"([^"]+)"\)', pad_block)
                if net_name_match:
                    net_name = net_name_match.group(1)
                    net_num = net_map.get(net_name, 0)
                else:
                    net_num = 0
                    net_name = ""

            # Extract drill size if present
            drill_match = re.search(r"\(drill\s+([\d.]+)", pad_block)
            drill_size = float(drill_match.group(1)) if drill_match else 0.0

            # Extract copper layer from pad layers
            # KiCad pads have: (layers "F.Cu" "F.Paste" "F.Mask") or (layers "B.Cu" ...)
            # For thru-hole pads: (layers "*.Cu" "*.Paste" "*.Mask") - means all copper layers
            # We need to find the copper layer name (*.Cu pattern)
            pad_layer = Layer.F_CU  # Default to top layer
            layers_match = re.search(r"\(layers\s+([^)]+)\)", pad_block)
            if layers_match:
                layers_str = layers_match.group(1)
                # Find copper layer names (ending with .Cu)
                copper_layers = re.findall(r'"([^"]+\.Cu)"', layers_str)
                if copper_layers:
                    # Check for wildcard (*.Cu means all copper layers - thru-hole)
                    if copper_layers[0] != "*.Cu":
                        with contextlib.suppress(ValueError):
                            pad_layer = Layer.from_kicad_name(copper_layers[0])

            # Override with netlist if provided
            if netlist:
                pad_key = f"{ref}.{pad_num}"
                if pad_key in netlist:
                    net_name = netlist[pad_key]
                    # Assign net number from net_map or create new
                    if net_name in net_map:
                        net_num = net_map[net_name]
                    elif net_name:
                        net_num = max(net_map.values(), default=0) + 1
                        net_map[net_name] = net_num

            # For skipped nets (power/ground planes), still add pad as obstacle
            # but use net=0 so it blocks routing without being a routeable net
            if net_name in skip_nets:
                net_num = 0  # Treat as obstacle, not a routable net

            # Transform pad position by footprint rotation
            # KiCad stores rotation in degrees, positive = counter-clockwise
            # Standard 2D rotation matrix applies directly (no negation needed)
            rot_rad = math.radians(fp_rot)
            cos_r, sin_r = math.cos(rot_rad), math.sin(rot_rad)
            abs_x = fp_x + pad_x * cos_r - pad_y * sin_r
            abs_y = fp_y + pad_x * sin_r + pad_y * cos_r

            # Also extract per-pad rotation if present
            pad_rot_match = re.search(r"\(at\s+[-\d.]+\s+[-\d.]+\s+([-\d.]+)\)", pad_block)
            pad_rot = float(pad_rot_match.group(1)) if pad_rot_match else 0.0

            # Rotate pad dimensions to PCB space.  The combined rotation
            # of footprint + pad determines whether width/height swap.
            total_rot = (fp_rot + pad_rot) % 360
            # At 90° or 270° the pad's width and height axes swap
            if abs(total_rot - 90) < 1 or abs(total_rot - 270) < 1:
                pad_w, pad_h = pad_h, pad_w

            pads.append(
                {
                    "number": pad_num,
                    "x": abs_x,
                    "y": abs_y,
                    "width": pad_w,
                    "height": pad_h,
                    "net": net_num,
                    "net_name": net_name,
                    "through_hole": pad_type == "thru_hole",
                    "drill": drill_size,
                    "layer": pad_layer,
                }
            )

        if pads:
            components.append(
                {
                    "ref": ref,
                    "x": fp_x,
                    "y": fp_y,
                    "rotation": fp_rot,
                    "pads": pads,
                }
            )

    # Create router with provided rules, PCB rules, or defaults
    if rules is None:
        if pcb_rules is not None:
            # Use rules extracted from PCB file
            rules = pcb_rules.to_design_rules()
        else:
            # Fall back to conservative defaults
            rules = DesignRules(grid_resolution=0.1)

    # Auto-adjust grid resolution if enabled
    if auto_adjust_grid:
        adjustment = adjust_grid_for_compliance(
            rules.grid_resolution,
            rules.trace_clearance,
        )
        if adjustment.was_adjusted:
            logger.info(adjustment.message)
            # Update grid resolution while preserving all other rule settings
            rules = replace(rules, grid_resolution=adjustment.adjusted)

    # Validate grid resolution for DRC compliance
    if validate_drc:
        validate_grid_resolution(
            rules.grid_resolution,
            rules.trace_clearance,
            warn=True,
            strict=strict_drc,
        )

    # Auto-detect layer stack from PCB if not provided (Issue #949)
    # This ensures pad layers match the available routing layers
    if layer_stack is None:
        layer_stack = detect_layer_stack(pcb_text)
        logger.debug(
            f"Auto-detected layer stack: {layer_stack.name} ({layer_stack.num_layers} layers)"
        )

    # Build per-net classification map (Issue #2465).
    # DEFAULT_NET_CLASS_MAP only contains a hardcoded list of common net
    # names (+5V, +3.3V, GND, ...).  Any net whose name isn't in that list
    # falls through to the default DIGITAL class with priority 4, which
    # means motor phase outputs (PHASE_A/B/C) and similar high-current
    # signals get treated like ordinary signals despite being routed-
    # critical.  Enrich the map by running pattern-based auto-classification
    # over every net in the PCB and merging in the resulting routing
    # configurations -- entries already present in DEFAULT_NET_CLASS_MAP
    # are preserved so existing behavior for explicit power names is
    # unchanged.
    net_class_map = dict(DEFAULT_NET_CLASS_MAP)
    try:
        from .net_class import classify_and_apply_rules as _classify_rules

        _net_names_for_class: dict[int, str] = {}
        for _m in re.finditer(r'\(net\s+(\d+)\s+"([^"]+)"\)', pcb_text):
            _nid, _nm = int(_m.group(1)), _m.group(2)
            if _nid > 0:
                _net_names_for_class[_nid] = _nm
        if _net_names_for_class:
            _auto_rules = _classify_rules(_net_names_for_class)
            for _name, _routing in _auto_rules.items():
                # Don't overwrite explicit DEFAULT entries -- those reflect
                # known-good defaults and may have been hand-tuned.
                if _name not in net_class_map:
                    net_class_map[_name] = _routing
    except Exception as e:  # noqa: BLE001 - classification is best-effort
        logger.debug(f"Auto net classification skipped: {e}")

    router = Autorouter(
        width=board_width,
        height=board_height,
        origin_x=origin_x,
        origin_y=origin_y,
        rules=rules,
        net_class_map=net_class_map,
        layer_stack=layer_stack,
        force_python=force_python,
        # Issue #2610: thread --max-search-iterations through to the C++ A*
        # iteration backstop override.
        max_search_iterations=max_search_iterations,
    )

    # Issue #3371 / P_FP3 -- fine-pitch escape region detection.  Must run
    # BEFORE pads land on the grid so the per-pad halos
    # (``_clearance_for_pin_pitch``) reflect the in-region escape clearance
    # at the moment each pad's blocked envelope is computed.  Running
    # post-add-component would leave the Python-side ``pad_blocked``
    # cells stale (set with the wider standard halo) even though the C++
    # validator -- which reads regions live at ``from_routing_grid``
    # construction time -- would correctly see the shrunk clearance.
    # That asymmetry blocks the pathfinder from threading through the
    # corridor between inboard SOIC pins, which is exactly the gap this
    # phase closes.
    _install_fine_pitch_regions_from_components(router, components)

    # Add all components
    for comp in components:
        # Pads already have absolute positions
        router.add_component(comp["ref"], comp["pads"])

    # Extract edge segments for board bbox and optional edge clearance
    # (Issue #2039).  The bbox derived from actual edge cuts is more
    # accurate than grid origin/dimensions for OOB filtering.
    edge_segments = _extract_edge_segments(pcb_text)
    if edge_segments:
        all_xs = [p[0] for seg in edge_segments for p in seg]
        all_ys = [p[1] for seg in edge_segments for p in seg]
        router._board_bbox = (min(all_xs), min(all_ys), max(all_xs), max(all_ys))
        # Store the raw outline segments for post-route edge-clearance
        # validation in drc_nudge / validate_routes (Issue #2743).
        router._edge_segments = edge_segments

    # Attach Shapely-based board geometry when available (Issue #2340).
    # This enables accurate non-rectangular edge clearance checking.
    try:
        from kicad_tools.pcb.board_geometry import BoardGeometry, has_shapely

        if has_shapely():
            from kicad_tools.schema.pcb import PCB as SchemaPCB

            _schema_pcb = SchemaPCB.load(pcb_path)
            try:
                router._board_geometry = BoardGeometry.from_pcb(_schema_pcb)
            except (ValueError, Exception):
                pass
    except ImportError:
        pass

    # Apply edge clearance if specified
    if edge_clearance is not None and edge_clearance > 0:
        # Store on router so EscapeRouter can clamp escape points (Issue #2136)
        router._edge_clearance = edge_clearance
        if edge_segments:
            blocked_cells = router.grid.add_edge_keepout(edge_segments, edge_clearance)
            if blocked_cells > 0:
                print(f"  Edge clearance: {edge_clearance}mm, {blocked_cells} cells blocked")

    # Load existing routes as obstacles for multi-pass routing
    if load_existing_routes:
        from .optimizer.pcb import parse_segments, parse_vias
        from .primitives import Route

        existing_segments = parse_segments(pcb_text)
        existing_vias = parse_vias(pcb_text)

        # Collect all net names across segments and vias
        all_net_names = set(existing_segments.keys()) | set(existing_vias.keys())

        route_count = 0
        for net_name in all_net_names:
            segs = existing_segments.get(net_name, [])
            vias = existing_vias.get(net_name, [])
            if not segs and not vias:
                continue

            # Determine net ID from first available element
            net_id = segs[0].net if segs else vias[0].net

            route = Route(
                net=net_id,
                net_name=net_name,
                segments=segs,
                vias=vias,
            )
            # Mark on grid as obstacles (blocked cells) but do NOT add to
            # router.routes — these are fixed geometry, not re-routable nets.
            # Store in router.existing_routes so DRC and via-merge can see them.
            router.grid.mark_route(route)
            router.existing_routes.append(route)
            route_count += 1

        if route_count > 0:
            total_segs = sum(len(s) for s in existing_segments.values())
            total_vias = sum(len(v) for v in existing_vias.values())
            logger.info(
                "Loaded %d existing routes as obstacles (%d segments, %d vias)",
                route_count,
                total_segs,
                total_vias,
            )

    return router, net_map


def generate_netclass_setup(
    rules: DesignRules,
    net_classes: dict[str, list[str]] | None = None,
) -> str:
    """
    Generate KiCad 7+ compatible net class setup S-expression.

    This function generates net class definitions in the format compatible
    with KiCad 7.x and 8.x. Note that this is OPTIONAL - routes generated
    by this library already embed trace widths and via sizes directly in
    segment and via S-expressions.

    IMPORTANT: Do NOT use the old KiCad 6 format:
        (net_settings
          (net_class "Default" "Default net class" ...)
        )

    This old format causes parsing errors in KiCad 7+:
        "Error loading PCB '...'. Unexpected 'net_settings' in '...'"

    Args:
        rules: DesignRules containing trace width, clearance, via parameters
        net_classes: Optional dict mapping class name to list of net names
                     e.g., {"Power": ["+5V", "GND"], "Signal": ["SDA", "SCL"]}

    Returns:
        KiCad 7+ compatible S-expression string for net class setup.
        Returns empty string if not needed (routes are self-contained).

    Example:
        >>> rules = DesignRules(trace_width=0.2, via_diameter=0.6, via_drill=0.3)
        >>> sexp = generate_netclass_setup(rules)
        >>> # Usually you don't need this - routes are self-contained
        >>> print("Routes already have correct trace/via sizes embedded")
    """
    # Routes already embed trace widths and via sizes in their S-expressions.
    # Net class setup is only needed for:
    # 1. DRC checking in KiCad
    # 2. Documentation purposes
    # 3. Manual editing after autorouting
    #
    # If you do need net class definitions, here's the KiCad 7+ format:
    #
    # The net class definitions go in the setup section as part of
    # design rules, not in a separate net_settings block.

    if not net_classes:
        # No net classes specified, and routes are self-contained
        # so no net class setup is needed
        return ""

    # Generate KiCad 7+ compatible net class assignments
    # These go in the setup section under design rules
    parts = []
    parts.append("  ; Net class definitions (KiCad 7+ format)")
    parts.append("  ; Note: Routes already have trace/via sizes embedded")

    for class_name, nets in net_classes.items():
        for net_name in nets:
            # In KiCad 7+, net-to-class assignments use this format
            parts.append(f'  (net_class "{class_name}" "{net_name}")')

    return "\n".join(parts)


def merge_routes_into_pcb(
    pcb_content: str,
    route_sexp: str,
    detect_via_conflicts: bool = False,
    via_clearance: float = 0.2,
) -> str:
    """
    Merge routed traces into an existing PCB file content.

    This function safely inserts route S-expressions into a PCB file,
    placing them before the final closing parenthesis. It does NOT
    add any net_settings or net_class blocks, as routes already have
    correct trace widths and via sizes embedded.

    When ``detect_via_conflicts`` is enabled, the merged result is scanned
    for co-located vias belonging to different nets.  Any duplicate vias
    whose centres fall within ``via_clearance`` mm of each other on
    different nets are removed from the output and a warning is logged.

    Args:
        pcb_content: Original PCB file content as string
        route_sexp: Route S-expressions as a string. This must be the output
            of ``Autorouter.to_sexp()``, NOT the Autorouter object itself.
        detect_via_conflicts: If True, scan the merged output for co-located
            vias on different nets and remove duplicates.  Default False for
            backward compatibility.
        via_clearance: Minimum centre-to-centre distance (mm) below which
            two vias on different nets are considered co-located.  Only used
            when ``detect_via_conflicts`` is True.  Default is 0.2 mm.

    Returns:
        Modified PCB content with routes inserted.

    Raises:
        TypeError: If route_sexp is an Autorouter object instead of a string.
            Call ``router.to_sexp()`` to get the route string.

    Example:
        >>> router, nets = load_pcb_for_routing("board.kicad_pcb")
        >>> router.route_all_negotiated()
        >>>
        >>> # IMPORTANT: Call to_sexp() to get the S-expression string
        >>> route_sexp = router.to_sexp()
        >>>
        >>> original = Path("board.kicad_pcb").read_text()
        >>> merged = merge_routes_into_pcb(original, route_sexp)
        >>> Path("board_routed.kicad_pcb").write_text(merged)

    Common mistake:
        The following is INCORRECT and will raise TypeError::

            # Wrong - passing Autorouter object directly
            merged = merge_routes_into_pcb(original, router)

        Instead, call ``to_sexp()`` first::

            # Correct - passing the S-expression string
            merged = merge_routes_into_pcb(original, router.to_sexp())

    Note:
        Routes contain embedded trace widths and via sizes, so no
        net class metadata is required. Do NOT add (net_settings ...)
        blocks with the old KiCad 6 format - this will cause parsing
        errors in KiCad 7+.
    """
    # Validate that route_sexp is a string, not an Autorouter object
    if hasattr(route_sexp, "to_sexp"):
        raise TypeError(
            "Expected route S-expression string, got object with to_sexp() method "
            "(likely an Autorouter). Call router.to_sexp() to get the route string."
        )

    if not route_sexp:
        return pcb_content

    # Remove trailing whitespace and closing parenthesis
    content = pcb_content.rstrip()
    if content.endswith(")"):
        content = content[:-1].rstrip()

    # Insert routes and close the file
    result = content + "\n\n"
    result += f"  {route_sexp}\n"
    result += ")\n"

    # Post-merge co-located via detection
    if detect_via_conflicts:
        result = _remove_conflicting_vias(result, via_clearance)

    return result


def _remove_conflicting_vias(pcb_text: str, clearance: float) -> str:
    """Remove co-located vias on different nets from merged PCB text.

    Scans all ``(via ...)`` blocks, groups them by position (within
    *clearance* mm), and removes duplicates where two vias from
    different nets occupy the same location.  The first via encountered
    is kept; later conflicting vias are stripped.

    Args:
        pcb_text: Full PCB text content (after merge).
        clearance: Centre-to-centre distance threshold in mm.

    Returns:
        PCB text with conflicting vias removed.
    """
    # Issue #3447: use the same balanced-parentheses walker as
    # ``parse_vias`` (fixed in #3446) so that fields may appear in any
    # order.  The previous implementation used a single ordered regex
    # that required ``(net N)`` to immediately follow ``(layers ...)``.
    # KiCad 8+ files -- including every PCB written by ``kct route``
    # itself -- emit ``(uuid "...")`` between ``(layers)`` and
    # ``(net)``, so the ordered pattern matched ZERO vias on modern
    # output and conflict detection silently no-opped.  It also missed
    # ``(via micro ...)`` / ``blind`` / ``buried`` blocks (#3118),
    # whose type token sits between ``via`` and ``(at ...)``.
    from .optimizer.pcb import _extract_balanced_blocks

    _re_at = re.compile(r"\(at\s+([\d.eE+-]+)\s+([\d.eE+-]+)\)")
    _re_net = re.compile(r"\(net\s+(\d+)\)")

    # Collect all vias with their positions, nets, and block spans
    vias: list[tuple[float, float, int, tuple[int, int]]] = []
    for start, end, block in _extract_balanced_blocks(pcb_text, "via"):
        m_at = _re_at.search(block)
        m_net = _re_net.search(block)
        # Both core fields are required; skip malformed blocks.
        if not (m_at and m_net):
            continue
        x = float(m_at.group(1))
        y = float(m_at.group(2))
        net = int(m_net.group(1))
        vias.append((x, y, net, (start, end)))

    if len(vias) < 2:
        return pcb_text

    # Identify conflicting vias (different net, within clearance distance)
    # Keep the first via encountered at each location; remove later conflicts
    spans_to_remove: list[tuple[int, int]] = []
    for i in range(len(vias)):
        x_i, y_i, net_i, span_i = vias[i]
        # Skip vias already marked for removal
        if span_i in spans_to_remove:
            continue
        for j in range(i + 1, len(vias)):
            x_j, y_j, net_j, span_j = vias[j]
            if net_i == net_j:
                continue
            dist = math.sqrt((x_i - x_j) ** 2 + (y_i - y_j) ** 2)
            if dist < clearance:
                if span_j not in spans_to_remove:
                    spans_to_remove.append(span_j)
                    logger.warning(
                        "Removed conflicting via at (%.4f, %.4f) net %d "
                        "(co-located with net %d, distance %.4fmm)",
                        x_j,
                        y_j,
                        net_j,
                        net_i,
                        dist,
                    )

    if not spans_to_remove:
        return pcb_text

    # Remove spans in reverse order to preserve character offsets
    spans_to_remove.sort(reverse=True)
    result = pcb_text
    for start, end in spans_to_remove:
        # Also strip trailing whitespace/newline after the removed via
        trail = end
        while trail < len(result) and result[trail] in (" ", "\t", "\n", "\r"):
            trail += 1
        result = result[:start] + result[trail:]

    return result


# =============================================================================
# OUTPUT CONNECTIVITY VERIFICATION (Issue #2264)
# =============================================================================


def verify_output_connectivity(
    pcb_content: str,
    net_pads: dict[int, list[Pad]],
    net_names: dict[int, str] | None = None,
    tolerance: float = 0.01,
) -> dict[int, dict]:
    """Verify connectivity of routed nets in written PCB S-expression output.

    Re-parses ``(segment ...)`` and ``(via ...)`` S-expressions from the PCB
    content and runs union-find against known pad positions to check that all
    pads in each net are connected by the written traces.  This catches bugs
    in ``to_sexp()`` serialization, ``_insert_sexp_before_closing()`` merge,
    and any other transformation that occurs between the internal Route objects
    and the final file.

    Args:
        pcb_content: Full PCB file content (S-expression text).
        net_pads: Mapping of net ID to the list of ``Pad`` objects belonging
            to that net.  Only nets present here are validated.
        net_names: Optional mapping of net ID to human-readable net name.
            Used only for diagnostic messages in the returned report.
        tolerance: Coordinate snapping tolerance in mm (default 0.01).

    Returns:
        Dict mapping net ID to a connectivity report dict with keys:

        - ``net_name``: human-readable net name (or ``"Net <id>"`` fallback)
        - ``total_pads``: number of pads in the net
        - ``connected_pads``: pads in the largest connected component
        - ``connected``: ``True`` when all pads are in one component
        - ``disconnected_pads``: list of ``"<ref>:<pin>"`` strings for pads
          not in the main component (empty when ``connected`` is True)
    """
    from .observability import _pt, _UnionFind

    # Parse segments from PCB content: (segment (start X Y) (end X Y) ... (net N) ...)
    seg_pattern = re.compile(
        r"\(segment\s+"
        r".*?\(start\s+([\d.+-]+)\s+([\d.+-]+)\)"
        r".*?\(end\s+([\d.+-]+)\s+([\d.+-]+)\)"
        r".*?\(net\s+(\d+)\)"
        r".*?\)",
        re.DOTALL,
    )

    # Parse vias: (via (at X Y) ... (net N) ...)
    # NOTE (#3447 sweep): unlike the ordered regexes fixed in #3446
    # (parse_vias) and #3447 (_remove_conflicting_vias), this pattern
    # uses lazy ``.*?`` wildcards between fields, so it tolerates
    # KiCad-8 field order (uuid before net) and via type tokens
    # (micro/blind/buried).  Checked and intentionally left as-is.
    via_pattern = re.compile(
        r"\(via\s+"
        r".*?\(at\s+([\d.+-]+)\s+([\d.+-]+)\)"
        r".*?\(net\s+(\d+)\)"
        r".*?\)",
        re.DOTALL,
    )

    # Group parsed segments by net
    segments_by_net: dict[int, list[tuple[tuple[float, float], tuple[float, float]]]] = {}
    for m in seg_pattern.finditer(pcb_content):
        x1, y1, x2, y2 = float(m.group(1)), float(m.group(2)), float(m.group(3)), float(m.group(4))
        net_id = int(m.group(5))
        segments_by_net.setdefault(net_id, []).append(
            (_pt(x1, y1, tolerance), _pt(x2, y2, tolerance))
        )

    # Group parsed vias by net
    vias_by_net: dict[int, list[tuple[float, float]]] = {}
    for m in via_pattern.finditer(pcb_content):
        x, y = float(m.group(1)), float(m.group(2))
        net_id = int(m.group(3))
        vias_by_net.setdefault(net_id, []).append(_pt(x, y, tolerance))

    result: dict[int, dict] = {}
    net_names = net_names or {}

    for net_id, pads in net_pads.items():
        net_name = net_names.get(net_id, f"Net {net_id}")

        if len(pads) < 2:
            result[net_id] = {
                "net_name": net_name,
                "total_pads": len(pads),
                "connected_pads": len(pads),
                "connected": True,
                "disconnected_pads": [],
            }
            continue

        net_segs = segments_by_net.get(net_id, [])
        net_vias = vias_by_net.get(net_id, [])

        if not net_segs and not net_vias:
            disconnected = [f"{p.ref}:{p.pin}" for p in pads]
            result[net_id] = {
                "net_name": net_name,
                "total_pads": len(pads),
                "connected_pads": 0,
                "connected": False,
                "disconnected_pads": disconnected,
            }
            continue

        uf = _UnionFind()

        # Union segment endpoints
        for p1, p2 in net_segs:
            uf.union(p1, p2)

        # Ensure via points are in the union-find and union with co-located
        # segment endpoints
        for via_pt in net_vias:
            uf._ensure(via_pt)
            for p1, p2 in net_segs:
                if p1 == via_pt:
                    uf.union(via_pt, p1)
                if p2 == via_pt:
                    uf.union(via_pt, p2)

        # Link pads to nearest segment endpoints (same logic as observability)
        pad_points: list[tuple[tuple[float, float], Pad]] = []
        for pad in pads:
            pad_pt = _pt(pad.x, pad.y, tolerance)
            best_dist = float("inf")
            best_pt = pad_pt
            for p1, p2 in net_segs:
                for sp in (p1, p2):
                    dx = sp[0] - pad_pt[0]
                    dy = sp[1] - pad_pt[1]
                    d = dx * dx + dy * dy
                    if d < best_dist:
                        best_dist = d
                        best_pt = sp
            # Also check vias
            for vp in net_vias:
                dx = vp[0] - pad_pt[0]
                dy = vp[1] - pad_pt[1]
                d = dx * dx + dy * dy
                if d < best_dist:
                    best_dist = d
                    best_pt = vp

            if best_dist <= 4.0:  # 2mm squared
                uf.union(pad_pt, best_pt)
            else:
                uf._ensure(pad_pt)
            pad_points.append((pad_pt, pad))

        # Find largest component
        component_pads: dict[tuple[float, float], int] = {}
        for pp, _ in pad_points:
            root = uf.find(pp)
            component_pads[root] = component_pads.get(root, 0) + 1

        max_component = max(component_pads.values()) if component_pads else 0
        total = len(pads)

        # Find the main root (largest component)
        main_root = max(component_pads, key=component_pads.get) if component_pads else None

        disconnected = []
        if max_component < total:
            for pp, pad in pad_points:
                if main_root is None or uf.find(pp) != main_root:
                    disconnected.append(f"{pad.ref}:{pad.pin}")

        result[net_id] = {
            "net_name": net_name,
            "total_pads": total,
            "connected_pads": max_component,
            "connected": max_component == total,
            "disconnected_pads": disconnected,
        }

    return result
