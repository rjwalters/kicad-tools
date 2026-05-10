"""
Escape routing for dense packages (BGA, QFP, QFN).

This module provides escape routing patterns for dense packages where
standard routing fails due to pin congestion. Dense packages have pins
that can't all route outward simultaneously - inner pins get blocked
by outer pins trying to escape.

Escape routing strategies:
- Ring-based escape (BGA): Route outer pins first, inner pins via down
- Alternating direction (QFP/QFN): Alternate escape directions per pin
- Staggered via fanout: Place vias in staggered pattern for via-in-pad

Example::

    from kicad_tools.router.escape import EscapeRouter

    # Create escape router
    escape = EscapeRouter(grid, rules)

    # Detect dense packages and generate escape routes
    for pad in pads:
        if escape.needs_escape_routing(pad, all_pads):
            routes = escape.generate_escape_routes(pad, all_pads)
            for route in routes:
                grid.reserve_escape_path(route)
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .grid import RoutingGrid
    from .rules import DesignRules, NetClassRouting

from .layers import Layer, LayerType
from .primitives import Pad, Route, Segment, Via

logger = logging.getLogger(__name__)


class PackageType(Enum):
    """Package type classification for escape routing."""

    UNKNOWN = auto()
    BGA = auto()  # Ball Grid Array
    QFP = auto()  # Quad Flat Package
    QFN = auto()  # Quad Flat No-lead
    TQFP = auto()  # Thin Quad Flat Package
    SOP = auto()  # Small Outline Package
    SSOP = auto()  # Shrink Small Outline Package (0.65mm pitch)
    TSSOP = auto()  # Thin Shrink Small Outline Package (0.65mm pitch)
    SOT = auto()  # Small Outline Transistor
    DIP = auto()  # Dual In-line Package
    MULTI_ROW_CONNECTOR = auto()  # Multi-row through-hole connector (2xN, 3xN, 4xN, >= 20 pins)
    THROUGH_HOLE = auto()  # Generic through-hole


class EscapeDirection(Enum):
    """Direction for pin escape routing."""

    NORTH = auto()
    SOUTH = auto()
    EAST = auto()
    WEST = auto()
    NORTHEAST = auto()
    NORTHWEST = auto()
    SOUTHEAST = auto()
    SOUTHWEST = auto()
    VIA_DOWN = auto()  # Escape via layer change


@dataclass
class EscapeRoute:
    """An escape route from a pin to open routing space.

    Attributes:
        pad: The pad being escaped
        direction: Primary escape direction
        via_pos: Position for via if layer change needed (None if surface escape)
        escape_layer: Layer to route on after escape
        escape_point: Point where escape route ends (open for further routing)
        segments: Trace segments for the escape
        via: Via object if layer change needed
        ring_index: For BGA, which ring this pad is in (0=outer)
    """

    pad: Pad
    direction: EscapeDirection
    escape_point: tuple[float, float]
    escape_layer: Layer
    via_pos: tuple[float, float] | None = None
    segments: list[Segment] = field(default_factory=list)
    via: Via | None = None
    ring_index: int = 0


@dataclass
class PackageInfo:
    """Information about a detected package.

    Attributes:
        ref: Component reference (e.g., "U1")
        package_type: Detected package type
        center: Package center position (x, y)
        pads: List of pads belonging to this package
        pin_count: Number of pins
        pin_pitch: Estimated pin pitch in mm
        bounding_box: (min_x, min_y, max_x, max_y)
        is_dense: Whether this qualifies as a dense package
        rows: Number of rows (for grid packages like BGA)
        cols: Number of columns (for grid packages like BGA)
    """

    ref: str
    package_type: PackageType
    center: tuple[float, float]
    pads: list[Pad]
    pin_count: int
    pin_pitch: float
    bounding_box: tuple[float, float, float, float]
    is_dense: bool
    rows: int = 0
    cols: int = 0


def is_dense_package(
    pads: list[Pad],
    pin_pitch_threshold: float = 0.5,
    pin_count_threshold: int = 48,
    trace_width: float | None = None,
    clearance: float | None = None,
) -> bool:
    """Detect if a set of pads represents a dense package.

    A package is considered dense if:
    - Pin pitch is too small for traces to pass between pins, OR
    - Pin pitch < 0.5mm (when no clearance info provided), OR
    - Pin count > 48
    - Fine-pitch SSOP/TSSOP (0.65mm pitch or less) - always dense
    - TQFP-32-class quad packages: >= 32 pins on a quad arrangement with
      pitch <= 0.8 mm are always dense.  At common board-house defaults
      (trace=0.2 mm, clearance=0.15 mm) the dynamic threshold of
      2*(0.2+0.15) = 0.7 mm is JUST below the 0.8 mm pitch, so without
      this rule TQFP-32 packages are not flagged as dense and the inner
      pins of nets that route to them get blocked by the surrounding
      perimeter routing.  See issue #2513.

    When trace_width and clearance are provided, the threshold is calculated
    dynamically: a package is dense if there's insufficient space between
    adjacent pins to route a trace. This accounts for the fact that packages
    like TQFP-32 with 0.8mm pitch may need escape routing when clearance
    requirements are strict.

    Args:
        pads: List of pads from a single component
        pin_pitch_threshold: Maximum pin pitch to be considered dense (mm).
            This is overridden by dynamic calculation when trace_width and
            clearance are provided.
        pin_count_threshold: Minimum pin count to be considered dense
        trace_width: Trace width in mm. When provided with clearance,
            calculates dynamic threshold.
        clearance: Trace-to-pad clearance in mm. When provided with
            trace_width, calculates dynamic threshold.

    Returns:
        True if the package is dense and needs escape routing
    """
    if len(pads) < 2:
        return False

    # Pin count check
    if len(pads) > pin_count_threshold:
        return True

    # Multi-row through-hole connectors (>= 20 pins) are dense because
    # inner-row pads are blocked by outer-row escape paths
    if len(pads) >= 20 and _is_multi_row(pads):
        return True

    # Calculate minimum pin pitch
    min_pitch = _calculate_min_pitch(pads)
    if min_pitch <= 0:
        return False

    # Fine-pitch SSOP/TSSOP check (0.75mm or less is always dense)
    # These packages need escape routing regardless of design rules
    if min_pitch <= 0.75 and _is_dual_row(pads):
        return True

    # TQFP-32-class quad packages (issue #2513).
    # A quad arrangement with >= 32 pins at <= 0.8 mm pitch is dense
    # regardless of trace/clearance.  At common JLCPCB-style defaults
    # (trace=0.2, clearance=0.15) the dynamic threshold below works out
    # to 0.70 mm which is just under the 0.8 mm pitch of a TQFP-32, so
    # the dynamic check would otherwise miss this class of MCU.  This
    # is intentionally conservative: it requires both a quad layout AND
    # >= 32 pins, so leaded SOIC-32 (dual row) and small QFP/QFN parts
    # at 32 pins (e.g. QFN-32 at 0.5mm pitch) are unaffected -- the
    # SOIC case fails the quad arrangement check and the small-pitch
    # case is already covered by the TSSOP/dynamic threshold rules.
    if len(pads) >= 32 and min_pitch <= 0.8 + 1e-3 and _looks_like_quad_layout(pads):
        return True

    # Dynamic threshold based on design rules
    # A trace needs: trace_width + clearance on each side from adjacent pins
    # So minimum pitch to route between pins is: 2 * (trace_width/2 + clearance) + trace_width
    # Simplified: 2 * trace_width + 2 * clearance = 2 * (trace_width + clearance)
    if trace_width is not None and clearance is not None:
        # Calculate the minimum pitch needed to fit a trace between pins
        # Each pin needs clearance + half the trace width on the routing side
        # So for two adjacent pins: 2 * (clearance + trace_width/2) + trace_width
        # This equals: 2*clearance + 2*trace_width = 2*(clearance + trace_width)
        dynamic_threshold = 2 * (trace_width + clearance)
        if min_pitch < dynamic_threshold:
            return True
    elif min_pitch < pin_pitch_threshold:
        # Fall back to static threshold when no design rules provided
        return True

    return False


def _looks_like_quad_layout(pads: list[Pad]) -> bool:
    """Convenience wrapper around _is_quad_arrangement using the pads' bbox.

    Used by is_dense_package() so the TQFP-32 rule does not need to
    duplicate bbox-and-center math at the call site.

    Args:
        pads: List of pads from a single component

    Returns:
        True if pads form a QFP/QFN-style quad arrangement
    """
    if len(pads) < 8:
        return False
    xs = [p.x for p in pads]
    ys = [p.y for p in pads]
    width = max(xs) - min(xs)
    height = max(ys) - min(ys)
    if width <= 0 or height <= 0:
        return False
    center_x = (max(xs) + min(xs)) / 2
    center_y = (max(ys) + min(ys)) / 2
    return _is_quad_arrangement(pads, center_x, center_y, width, height)


def is_fine_pitch_ssop(pads: list[Pad], pitch_threshold: float = 0.75) -> bool:
    """Check if pads represent a fine-pitch SSOP/TSSOP package.

    Fine-pitch SSOP/TSSOP packages (0.65mm pitch) have adjacent pins too close
    together for standard routing between them. They require special escape
    routing with alternating layer assignments.

    Args:
        pads: List of pads from a single component
        pitch_threshold: Maximum pitch to be considered fine-pitch (mm).
            Default 0.75mm catches both SSOP (0.65mm) and TSSOP (0.5mm).

    Returns:
        True if the package is a fine-pitch SSOP/TSSOP needing special routing
    """
    if len(pads) < 4:  # Need at least 4 pads for SSOP
        return False

    # Check for dual-row arrangement (SSOP/TSSOP characteristic)
    if not _is_dual_row(pads):
        return False

    # Check pin pitch
    min_pitch = _calculate_min_pitch(pads)
    return 0 < min_pitch <= pitch_threshold


def detect_package_type(pads: list[Pad]) -> PackageType:
    """Detect the package type from pad arrangement.

    Uses pad positions and characteristics to classify the package.

    Args:
        pads: List of pads from a single component

    Returns:
        Detected PackageType
    """
    if len(pads) < 2:
        return PackageType.UNKNOWN

    # Check for through-hole pads
    through_hole_count = sum(1 for p in pads if p.through_hole)
    if through_hole_count > len(pads) * 0.8:
        if len(pads) <= 3:
            return PackageType.SOT
        # Multi-row through-hole connectors (2xN, 3xN, 4xN with >= 20 pins)
        # need BGA-style fanout escape with row-aware layer assignment
        if _is_multi_row(pads) and len(pads) >= 20:
            return PackageType.MULTI_ROW_CONNECTOR
        if _is_dual_row(pads):
            return PackageType.DIP
        return PackageType.THROUGH_HOLE

    # Calculate bounding box and center
    xs = [p.x for p in pads]
    ys = [p.y for p in pads]
    width = max(xs) - min(xs)
    height = max(ys) - min(ys)
    center_x = (max(xs) + min(xs)) / 2
    center_y = (max(ys) + min(ys)) / 2

    # IMPORTANT: Check detection order matters!
    # 1. Dual-row packages (SOP/SSOP/TSSOP) - only 2 rows of pads
    # 2. Quad packages (QFP/QFN) - pads on 4 edges, empty interior
    # 3. Grid packages (BGA) - filled grid throughout

    # Check for dual-row first (SOP/SSOP/TSSOP) - most specific
    if _is_dual_row(pads):
        # Distinguish between SOP, SSOP, TSSOP based on pin pitch
        min_pitch = _calculate_min_pitch(pads)
        if min_pitch < 0.55:
            # TSSOP: 0.5mm pitch (thin shrink)
            return PackageType.TSSOP
        elif min_pitch < 0.75:
            # SSOP: 0.65mm pitch (shrink)
            return PackageType.SSOP
        else:
            # Standard SOP/SOIC: 1.27mm pitch
            return PackageType.SOP

    # Check for quad arrangement (QFP/QFN/TQFP) before BGA
    # QFP/QFN have pads only on edges, not in interior
    if _is_quad_arrangement(pads, center_x, center_y, width, height):
        # Only classify as quad if there are no interior pads
        if not _has_interior_pads(pads, center_x, center_y, width, height):
            # QFN typically has an exposed thermal pad in center
            has_center_pad = any(
                abs(p.x - center_x) < 1.0 and abs(p.y - center_y) < 1.0 for p in pads
            )
            if has_center_pad and len(pads) <= 64:
                return PackageType.QFN

            # TQFP has finer pitch
            min_pitch = _calculate_min_pitch(pads)
            if min_pitch < 0.5:
                return PackageType.TQFP

            return PackageType.QFP

    # Check for grid pattern (BGA) - must have interior pads
    if _is_grid_pattern(pads, center_x, center_y):
        return PackageType.BGA

    return PackageType.UNKNOWN


def get_package_info(
    pads: list[Pad],
    trace_width: float | None = None,
    clearance: float | None = None,
) -> PackageInfo:
    """Get comprehensive information about a package.

    Args:
        pads: List of pads from a single component
        trace_width: Optional trace width for dynamic dense detection
        clearance: Optional clearance for dynamic dense detection

    Returns:
        PackageInfo with detected characteristics
    """
    if not pads:
        return PackageInfo(
            ref="",
            package_type=PackageType.UNKNOWN,
            center=(0, 0),
            pads=[],
            pin_count=0,
            pin_pitch=0,
            bounding_box=(0, 0, 0, 0),
            is_dense=False,
        )

    ref = pads[0].ref if pads else ""
    package_type = detect_package_type(pads)

    xs = [p.x for p in pads]
    ys = [p.y for p in pads]
    center = ((max(xs) + min(xs)) / 2, (max(ys) + min(ys)) / 2)
    bounding_box = (min(xs), min(ys), max(xs), max(ys))
    pin_pitch = _calculate_min_pitch(pads)

    # Estimate rows/cols for grid and multi-row packages
    rows, cols = 0, 0
    if package_type in (PackageType.BGA, PackageType.MULTI_ROW_CONNECTOR):
        rows, cols = _estimate_grid_dimensions(pads)

    return PackageInfo(
        ref=ref,
        package_type=package_type,
        center=center,
        pads=pads,
        pin_count=len(pads),
        pin_pitch=pin_pitch,
        bounding_box=bounding_box,
        is_dense=is_dense_package(pads, trace_width=trace_width, clearance=clearance),
        rows=rows,
        cols=cols,
    )


def _calculate_min_pitch(pads: list[Pad]) -> float:
    """Calculate minimum pin-to-pin distance."""
    if len(pads) < 2:
        return 0

    min_dist = float("inf")
    for i, p1 in enumerate(pads):
        for p2 in pads[i + 1 :]:
            dist = math.sqrt((p2.x - p1.x) ** 2 + (p2.y - p1.y) ** 2)
            if dist > 0.01:  # Ignore coincident pads
                min_dist = min(min_dist, dist)

    return min_dist if min_dist != float("inf") else 0


def _is_dual_row(pads: list[Pad]) -> bool:
    """Check if pads form a dual-row arrangement."""
    if len(pads) < 4:
        return False

    ys = sorted({round(p.y, 2) for p in pads})
    xs = sorted({round(p.x, 2) for p in pads})

    # Dual row: 2 distinct Y values, many X values
    if len(ys) == 2 and len(xs) >= len(pads) // 2 - 1:
        return True

    # Or 2 distinct X values, many Y values
    if len(xs) == 2 and len(ys) >= len(pads) // 2 - 1:
        return True

    return False


def _is_multi_row(pads: list[Pad]) -> bool:
    """Check if pads form a multi-row arrangement (2, 3, or 4+ rows).

    Multi-row connectors have a small number of rows (2-6) and many columns.
    This is more general than ``_is_dual_row`` which only detects exactly
    2 rows.  A 2-row connector passes both checks, but a 3xN or 4xN header
    only passes this one.

    The heuristic: count unique coordinate values along each axis.  If one
    axis has 2-6 unique values and the other has at least as many unique
    values as the smaller axis count, it is a multi-row arrangement.

    Args:
        pads: List of pads from a single component

    Returns:
        True if the pads form a multi-row arrangement
    """
    if len(pads) < 4:
        return False

    ys = sorted({round(p.y, 2) for p in pads})
    xs = sorted({round(p.x, 2) for p in pads})

    # Check if rows are along Y axis (few Y values, many X values)
    if 2 <= len(ys) <= 6 and len(xs) >= len(ys):
        # Verify roughly equal pad counts per row
        row_counts = []
        for y_val in ys:
            count = sum(1 for p in pads if round(p.y, 2) == y_val)
            row_counts.append(count)
        # Rows should have similar pad counts (within 2x)
        if min(row_counts) > 0 and max(row_counts) / min(row_counts) <= 2.0:
            return True

    # Check if rows are along X axis (few X values, many Y values)
    if 2 <= len(xs) <= 6 and len(ys) >= len(xs):
        row_counts = []
        for x_val in xs:
            count = sum(1 for p in pads if round(p.x, 2) == x_val)
            row_counts.append(count)
        if min(row_counts) > 0 and max(row_counts) / min(row_counts) <= 2.0:
            return True

    return False


def _has_interior_pads(
    pads: list[Pad],
    center_x: float,
    center_y: float,
    width: float,
    height: float,
) -> bool:
    """Check if pads exist in the interior (not just on edges).

    Used to distinguish BGA (interior pads) from QFP/QFN (edge-only pads).
    """
    if width < 0.1 or height < 0.1:
        return False

    # Define interior as 30% from each edge
    min_x = center_x - width / 2
    max_x = center_x + width / 2
    min_y = center_y - height / 2
    max_y = center_y + height / 2

    interior_margin_x = width * 0.25
    interior_margin_y = height * 0.25

    interior_pads = [
        p
        for p in pads
        if (min_x + interior_margin_x < p.x < max_x - interior_margin_x)
        and (min_y + interior_margin_y < p.y < max_y - interior_margin_y)
    ]

    # Consider interior if there are non-trivial interior pads
    # Allow 1 pad in center for QFN thermal pad
    return len(interior_pads) > 1


def _is_grid_pattern(pads: list[Pad], center_x: float, center_y: float) -> bool:
    """Check if pads form a grid pattern (BGA).

    BGA packages have pads distributed throughout the interior,
    not just on edges. This distinguishes them from QFP/QFN.

    Grid (BGA) detection requires:
    - At least 16 pads (room for at least a 4x4 grid)
    - At least 3 substantial rows AND 3 substantial cols (BGA is at least
      a 3x3 grid; this guards against 2-row connectors with mounting tabs
      that produce a tiny "third row" being misclassified as BGA -- see
      issue #2513 for USB-C with 2 SMT rows + 2 mounting tabs being
      reported as BGA-18 with 3 unique Y values).
    - Significant interior pads (not just edge pads, distinguishing BGA
      from QFP/QFN)
    - Roughly balanced quadrant distribution
    """
    if len(pads) < 16:  # Need at least 4x4 for BGA
        return False

    # Calculate bounding box
    xs = [p.x for p in pads]
    ys = [p.y for p in pads]
    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)
    width = max_x - min_x
    height = max_y - min_y

    if width < 0.1 or height < 0.1:
        return False

    # Issue #2513: A real BGA grid has many rows AND many cols, with each
    # row and col having a substantial number of pads.  A 2-row connector
    # (USB-C, etc.) with mounting tabs may produce a third "row" with just
    # 2 pads in it.  Filter outlier rows/cols (those whose pad count is
    # less than half the median) before counting -- only substantive rows
    # and cols qualify as BGA "axes".
    substantive_rows = _count_substantive_axis_groups(pads, axis="y")
    substantive_cols = _count_substantive_axis_groups(pads, axis="x")
    if substantive_rows < 3 or substantive_cols < 3:
        return False

    # For BGA, check that there are pads in the interior (not just on edges)
    # Define interior as 20% from each edge (relaxed to catch typical BGA grids)
    interior_margin_x = width * 0.2
    interior_margin_y = height * 0.2

    interior_pads = [
        p
        for p in pads
        if (min_x + interior_margin_x < p.x < max_x - interior_margin_x)
        and (min_y + interior_margin_y < p.y < max_y - interior_margin_y)
    ]

    # BGA should have significant interior pads (at least 10% of total)
    # For an 8x8 grid this means ~6+ interior pads
    if len(interior_pads) < len(pads) * 0.1:
        return False

    # Count pads in each quadrant relative to center
    quadrants = [0, 0, 0, 0]
    for p in pads:
        if p.x > center_x and p.y > center_y:
            quadrants[0] += 1
        elif p.x < center_x and p.y > center_y:
            quadrants[1] += 1
        elif p.x < center_x and p.y < center_y:
            quadrants[2] += 1
        else:
            quadrants[3] += 1

    # BGA should have roughly equal distribution across quadrants
    avg = len(pads) / 4
    return all(0.3 * avg <= q <= 1.7 * avg for q in quadrants if avg > 0)


def _count_substantive_axis_groups(pads: list[Pad], axis: str) -> int:
    """Count rows or columns that hold a substantial fraction of total pads.

    Used by _is_grid_pattern (and other classifiers) to ignore outlier
    "rows" or "cols" that are really just a few off-axis pads -- e.g.
    USB-C mounting tabs or alignment posts that share neither a row nor
    a column with the main signal grid.

    A group is "substantive" if its pad count is at least 50% of the
    median group count along that axis.  Singletons and tiny groups are
    therefore filtered out.

    Args:
        pads: List of pads from a single component
        axis: Which axis to group by - "x" counts unique X (i.e. column
            count), "y" counts unique Y (row count).

    Returns:
        Number of substantive groups along that axis.  Returns 0 for
        empty input.
    """
    if not pads:
        return 0
    if axis == "y":
        coords = [round(p.y, 2) for p in pads]
    else:
        coords = [round(p.x, 2) for p in pads]
    counts: dict[float, int] = {}
    for c in coords:
        counts[c] = counts.get(c, 0) + 1
    if not counts:
        return 0
    sorted_counts = sorted(counts.values())
    n = len(sorted_counts)
    median = (
        sorted_counts[n // 2]
        if n % 2 == 1
        else (sorted_counts[n // 2 - 1] + sorted_counts[n // 2]) / 2
    )
    threshold = max(1.0, median * 0.5)
    return sum(1 for v in counts.values() if v >= threshold)


def _is_quad_arrangement(
    pads: list[Pad],
    center_x: float,
    center_y: float,
    width: float,
    height: float,
) -> bool:
    """Check if pads form a quad arrangement (QFP/QFN)."""
    if len(pads) < 8:
        return False

    # Count pads on each edge (within margin of edge)
    margin = min(width, height) * 0.15
    edges = [0, 0, 0, 0]  # N, S, E, W

    min_x = center_x - width / 2
    max_x = center_x + width / 2
    min_y = center_y - height / 2
    max_y = center_y + height / 2

    for p in pads:
        if abs(p.y - max_y) < margin:
            edges[0] += 1  # North
        elif abs(p.y - min_y) < margin:
            edges[1] += 1  # South
        if abs(p.x - max_x) < margin:
            edges[2] += 1  # East
        elif abs(p.x - min_x) < margin:
            edges[3] += 1  # West

    # QFP/QFN should have pins on all 4 edges
    return all(e >= 2 for e in edges)


def _estimate_grid_dimensions(pads: list[Pad]) -> tuple[int, int]:
    """Estimate rows and columns for a grid package."""
    if len(pads) < 4:
        return (0, 0)

    # Count unique positions
    unique_x = len({round(p.x, 2) for p in pads})
    unique_y = len({round(p.y, 2) for p in pads})

    return (unique_y, unique_x)


class EscapeRouter:
    """Router for generating escape routes from dense packages.

    This class analyzes package pin arrangements and generates
    escape routing patterns that allow all pins to route outward
    without blocking each other.

    Example::

        router = EscapeRouter(grid, rules)
        package_info = router.analyze_package(component_pads)

        if package_info.is_dense:
            escapes = router.generate_escapes(package_info)
            for escape in escapes:
                router.apply_escape(escape)
    """

    def __init__(
        self,
        grid: RoutingGrid,
        rules: DesignRules,
        via_spacing: float | None = None,
        escape_clearance: float | None = None,
        net_class_map: dict[str, NetClassRouting] | None = None,
        edge_clearance: float | None = None,
        board_bounds: tuple[float, float, float, float] | None = None,
        manufacturer: str | None = None,
    ):
        """Initialize the escape router.

        Args:
            grid: Routing grid to work with
            rules: Design rules for dimensions
            via_spacing: Minimum via-to-via spacing (defaults to via_diameter + clearance)
            escape_clearance: Clearance from package edge (defaults to trace_clearance * 2)
            net_class_map: Optional net class map for per-net trace widths
            edge_clearance: Copper-to-board-edge clearance in mm. When set along
                with board_bounds, escape points and via positions are clamped so
                they do not violate the edge clearance zone.
            board_bounds: Board outline bounding box (min_x, min_y, max_x, max_y)
                in mm. Required together with edge_clearance for clamping.
            manufacturer: Manufacturer identifier (e.g. ``"jlcpcb"``,
                ``"jlcpcb-tier1"``).  When provided, capability flags such as
                ``via_in_pad_supported`` are looked up via
                ``mfr_limits.get_mfr_limits()`` and used to enable in-pad
                escape on fine-pitch SSOP/TSSOP packages (Issue #2605).
                Falls back to ``rules.manufacturer`` when not supplied.
        """
        self.grid = grid
        self.rules = rules
        self.via_spacing = via_spacing or (rules.via_diameter + rules.via_clearance)
        self.escape_clearance = escape_clearance or (rules.trace_clearance * 2)
        self.net_class_map = net_class_map or {}
        self.edge_clearance = edge_clearance
        self.board_bounds = board_bounds

        # Issue #2605: Resolve manufacturer capability flags.  Caller-supplied
        # arg wins; otherwise fall back to ``rules.manufacturer``.  If the
        # manufacturer is unknown we silently treat it as "no via-in-pad"
        # rather than raising -- the router should never crash because of an
        # unrecognized manufacturer string.
        self.manufacturer: str | None = manufacturer or getattr(rules, "manufacturer", None)
        self._mfr_limits = None
        if self.manufacturer is not None:
            try:
                from .mfr_limits import get_mfr_limits

                self._mfr_limits = get_mfr_limits(self.manufacturer)
            except (ValueError, ImportError):
                self._mfr_limits = None
        self.via_in_pad_supported: bool = bool(
            self._mfr_limits is not None and self._mfr_limits.via_in_pad_supported
        )

    def _get_trace_width_for_net(self, net_name: str) -> float:
        """Get the trace width for a net based on its net class.

        Args:
            net_name: Name of the net

        Returns:
            Trace width in mm
        """
        if self.net_class_map and net_name in self.net_class_map:
            return self.net_class_map[net_name].trace_width
        return self.rules.trace_width

    def _clamp_to_edge_clearance(self, x: float, y: float) -> tuple[float, float]:
        """Clamp a point so it respects the board edge clearance zone.

        When both edge_clearance and board_bounds are set, ensures that the
        point stays at least edge_clearance mm inside the board outline.
        Returns the point unchanged when edge clearance is not configured.

        Args:
            x: X coordinate in mm
            y: Y coordinate in mm

        Returns:
            Clamped (x, y) tuple
        """
        if self.edge_clearance is None or self.board_bounds is None:
            return (x, y)

        min_x, min_y, max_x, max_y = self.board_bounds
        ec = self.edge_clearance
        clamped_x = max(min_x + ec, min(x, max_x - ec))
        clamped_y = max(min_y + ec, min(y, max_y - ec))
        return (clamped_x, clamped_y)

    def analyze_package(self, pads: list[Pad]) -> PackageInfo:
        """Analyze a package to determine escape routing needs.

        Args:
            pads: All pads from a single component

        Returns:
            PackageInfo with detected characteristics
        """
        return get_package_info(
            pads,
            trace_width=self.rules.trace_width,
            clearance=self.rules.trace_clearance,
        )

    def generate_escapes(self, package: PackageInfo) -> list[EscapeRoute]:
        """Generate escape routes for all pins of a package.

        Routes are generated based on package type:
        - BGA: Ring-based escape with layer alternation
        - QFP/QFN/TQFP: Alternating direction escape
        - SSOP/TSSOP: Alternating layer escape for fine-pitch
        - SOP: Staggered via fanout
        - Other: Simple radial escape

        When edge_clearance and board_bounds are configured, escape points
        and segment endpoints are clamped to stay within the edge clearance
        zone so the escape router does not produce board-edge violations.

        Args:
            package: Package info from analyze_package()

        Returns:
            List of EscapeRoute objects for each pin
        """
        if package.package_type == PackageType.BGA:
            escapes = self._escape_bga_rings(package)
        elif package.package_type in (
            PackageType.QFP,
            PackageType.QFN,
            PackageType.TQFP,
        ):
            escapes = self._escape_qfp_alternating(package)
        elif package.package_type in (PackageType.SSOP, PackageType.TSSOP):
            # Fine-pitch SSOP/TSSOP needs alternating layer escape for adjacent pins
            escapes = self._escape_fine_pitch_dual_row(package)
        elif package.package_type == PackageType.SOP:
            escapes = self._escape_sop_staggered(package)
        elif package.package_type == PackageType.MULTI_ROW_CONNECTOR:
            escapes = self._escape_multi_row_connector(package)
        else:
            escapes = self._escape_radial(package)

        # Apply edge clearance clamping when configured
        if self.edge_clearance is not None and self.board_bounds is not None:
            escapes = self._apply_edge_clearance(escapes)

        # Issue #2350: Warn when an entire package gets 0 escapes.
        # Silent failure makes it very hard to diagnose routing problems.
        if not escapes and package.pin_count > 0:
            logger.warning(
                "Escape routing for %s (%s, %d pins, %.2fmm pitch): "
                "0 pins escaped -- all escapes failed clearance validation. "
                "Consider setting fine_pitch_clearance in DesignRules or "
                "adding a component_clearances override for %s.",
                package.ref,
                package.package_type.name,
                package.pin_count,
                package.pin_pitch,
                package.ref,
            )

        return escapes

    def _apply_edge_clearance(self, escapes: list[EscapeRoute]) -> list[EscapeRoute]:
        """Clamp escape route points to respect board edge clearance.

        Adjusts escape_point, via_pos, and segment endpoints so that no
        copper generated by the escape router falls within the edge
        clearance zone. The pad origin is never moved (the component is
        placed by the placer and is not our concern).

        Args:
            escapes: Escape routes to clamp

        Returns:
            The same list with coordinates adjusted in place
        """
        for escape in escapes:
            # Clamp escape point
            escape.escape_point = self._clamp_to_edge_clearance(*escape.escape_point)

            # Clamp via position if present
            if escape.via_pos is not None:
                escape.via_pos = self._clamp_to_edge_clearance(*escape.via_pos)
                if escape.via is not None:
                    clamped_x, clamped_y = escape.via_pos
                    escape.via = Via(
                        x=clamped_x,
                        y=clamped_y,
                        drill=escape.via.drill,
                        diameter=escape.via.diameter,
                        layers=escape.via.layers,
                        net=escape.via.net,
                        net_name=escape.via.net_name,
                    )

            # Clamp segment endpoints (skip x1/y1 of the first segment --
            # that is the pad origin which we must not move)
            for i, seg in enumerate(escape.segments):
                # For the first segment, only clamp the endpoint (x2, y2)
                if i == 0:
                    cx2, cy2 = self._clamp_to_edge_clearance(seg.x2, seg.y2)
                    escape.segments[i] = Segment(
                        x1=seg.x1, y1=seg.y1, x2=cx2, y2=cy2,
                        width=seg.width, layer=seg.layer,
                        net=seg.net, net_name=seg.net_name,
                    )
                else:
                    cx1, cy1 = self._clamp_to_edge_clearance(seg.x1, seg.y1)
                    cx2, cy2 = self._clamp_to_edge_clearance(seg.x2, seg.y2)
                    escape.segments[i] = Segment(
                        x1=cx1, y1=cy1, x2=cx2, y2=cy2,
                        width=seg.width, layer=seg.layer,
                        net=seg.net, net_name=seg.net_name,
                    )

        return escapes

    def _escape_bga_rings(self, package: PackageInfo) -> list[EscapeRoute]:
        """Generate ring-based escape routes for BGA packages.

        Outer ring pins escape horizontally/vertically on top layer.
        Inner ring pins drop via and escape on inner layer.
        Pattern alternates layers for each ring.

        Args:
            package: BGA package info

        Returns:
            List of escape routes, outer ring first
        """
        escapes: list[EscapeRoute] = []
        center_x, center_y = package.center

        # Group pads by ring (distance from center)
        rings = self._group_pads_by_ring(package.pads, center_x, center_y)

        for ring_idx, ring_pads in enumerate(rings):
            # Alternate layers: even rings on F.Cu, odd on B.Cu
            escape_layer = Layer.F_CU if ring_idx % 2 == 0 else Layer.B_CU
            needs_via = ring_idx > 0  # Outer ring stays on top

            for pad in ring_pads:
                escape = self._create_ring_escape(
                    pad=pad,
                    center=(center_x, center_y),
                    ring_idx=ring_idx,
                    escape_layer=escape_layer,
                    needs_via=needs_via,
                    package=package,
                )
                escapes.append(escape)

        return escapes

    def _group_pads_by_ring(
        self,
        pads: list[Pad],
        center_x: float,
        center_y: float,
    ) -> list[list[Pad]]:
        """Group pads into concentric rings based on distance from center.

        Args:
            pads: All pads of the package
            center_x: Package center X
            center_y: Package center Y

        Returns:
            List of rings, each containing pads at that distance
        """
        if not pads:
            return []

        # Calculate distance from center for each pad
        pad_distances: list[tuple[Pad, float]] = []
        for pad in pads:
            dist = math.sqrt((pad.x - center_x) ** 2 + (pad.y - center_y) ** 2)
            pad_distances.append((pad, dist))

        # Sort by distance
        pad_distances.sort(key=lambda x: x[1], reverse=True)

        # Group into rings by distance (allow some tolerance for grid irregularity)
        rings: list[list[Pad]] = []
        pitch = self.rules.trace_width * 3  # Approximate ring separation

        current_ring: list[Pad] = []
        current_dist = pad_distances[0][1] if pad_distances else 0

        for pad, dist in pad_distances:
            if current_dist - dist > pitch:
                if current_ring:
                    rings.append(current_ring)
                current_ring = [pad]
                current_dist = dist
            else:
                current_ring.append(pad)

        if current_ring:
            rings.append(current_ring)

        return rings

    def _create_ring_escape(
        self,
        pad: Pad,
        center: tuple[float, float],
        ring_idx: int,
        escape_layer: Layer,
        needs_via: bool,
        package: PackageInfo,
    ) -> EscapeRoute:
        """Create an escape route for a pad in a ring.

        Args:
            pad: The pad to escape
            center: Package center
            ring_idx: Which ring this pad is in (0=outer)
            escape_layer: Layer to escape to
            needs_via: Whether a via is needed
            package: Package info for bounds

        Returns:
            EscapeRoute for this pad
        """
        center_x, center_y = center

        # Determine escape direction based on quadrant
        direction = self._get_quadrant_direction(pad.x, pad.y, center_x, center_y)

        # Calculate escape point (beyond package edge + clearance)
        dx, dy = self._direction_to_vector(direction)
        min_x, min_y, max_x, max_y = package.bounding_box

        # Find distance to edge in this direction
        if dx > 0:
            edge_dist = max_x - pad.x + self.escape_clearance
        elif dx < 0:
            edge_dist = pad.x - min_x + self.escape_clearance
        else:
            edge_dist = 0

        if dy > 0:
            edge_dist = max(edge_dist, max_y - pad.y + self.escape_clearance)
        elif dy < 0:
            edge_dist = max(edge_dist, pad.y - min_y + self.escape_clearance)

        escape_x = pad.x + dx * edge_dist
        escape_y = pad.y + dy * edge_dist

        # Create segments and via if needed
        segments: list[Segment] = []
        via: Via | None = None
        via_pos: tuple[float, float] | None = None

        if needs_via:
            # Place via offset from pad
            via_offset = self.via_spacing
            via_x = pad.x + dx * via_offset
            via_y = pad.y + dy * via_offset
            via_pos = (via_x, via_y)

            # Short segment from pad to via
            segments.append(
                Segment(
                    x1=pad.x,
                    y1=pad.y,
                    x2=via_x,
                    y2=via_y,
                    width=self._get_trace_width_for_net(pad.net_name),
                    layer=pad.layer,
                    net=pad.net,
                    net_name=pad.net_name,
                )
            )

            # Create via
            via = Via(
                x=via_x,
                y=via_y,
                drill=self.rules.via_drill,
                diameter=self.rules.via_diameter,
                layers=(pad.layer, escape_layer),
                net=pad.net,
                net_name=pad.net_name,
            )

            # Segment from via to escape point on escape layer
            segments.append(
                Segment(
                    x1=via_x,
                    y1=via_y,
                    x2=escape_x,
                    y2=escape_y,
                    width=self._get_trace_width_for_net(pad.net_name),
                    layer=escape_layer,
                    net=pad.net,
                    net_name=pad.net_name,
                )
            )
        else:
            # Direct escape on same layer
            segments.append(
                Segment(
                    x1=pad.x,
                    y1=pad.y,
                    x2=escape_x,
                    y2=escape_y,
                    width=self._get_trace_width_for_net(pad.net_name),
                    layer=pad.layer,
                    net=pad.net,
                    net_name=pad.net_name,
                )
            )

        return EscapeRoute(
            pad=pad,
            direction=direction,
            escape_point=(escape_x, escape_y),
            escape_layer=escape_layer,
            via_pos=via_pos,
            segments=segments,
            via=via,
            ring_index=ring_idx,
        )

    def _escape_qfp_alternating(self, package: PackageInfo) -> list[EscapeRoute]:
        """Generate alternating direction escapes for QFP/QFN packages.

        Even-indexed pins escape perpendicular (outward).
        Odd-indexed pins escape parallel (along edge), alternating left/right.

        Args:
            package: QFP/QFN package info

        Returns:
            List of escape routes
        """
        escapes: list[EscapeRoute] = []
        center_x, center_y = package.center
        min_x, min_y, max_x, max_y = package.bounding_box

        # Group pads by edge
        north_pads: list[Pad] = []
        south_pads: list[Pad] = []
        east_pads: list[Pad] = []
        west_pads: list[Pad] = []

        edge_margin = min(max_x - min_x, max_y - min_y) * 0.2

        for pad in package.pads:
            # Skip center pad (thermal pad)
            if abs(pad.x - center_x) < edge_margin and abs(pad.y - center_y) < edge_margin:
                continue

            # Issue #2513: Skip pads that belong to skipped/plane nets (net=0).
            # Plane nets (GND, VCC, etc.) are stitched via planes, not routed
            # via escapes.  Generating escapes for them wastes perimeter
            # routing space (a TQFP-32 MCU may have 19/32 pins on plane nets;
            # without this filter the escape phase blocks the perimeter for
            # the actual signal nets that need to escape).
            if pad.net == 0:
                continue

            if abs(pad.y - max_y) < edge_margin:
                north_pads.append(pad)
            elif abs(pad.y - min_y) < edge_margin:
                south_pads.append(pad)
            elif abs(pad.x - max_x) < edge_margin:
                east_pads.append(pad)
            elif abs(pad.x - min_x) < edge_margin:
                west_pads.append(pad)

        # Sort each edge by position
        north_pads.sort(key=lambda p: p.x)
        south_pads.sort(key=lambda p: p.x)
        east_pads.sort(key=lambda p: p.y)
        west_pads.sort(key=lambda p: p.y)

        # Issue #2513: For lower-density QFP/TQFP (pitch >= 0.65 mm) the
        # alternating perpendicular/parallel scheme blocks more perimeter
        # space than it saves -- a TQFP-32 at 0.8 mm pitch has plenty of
        # room between pins to fit a 0.2 mm trace with 0.15 mm clearance,
        # so every pin can escape perpendicular and the parallel arms of
        # the alternating pattern just consume routing real-estate.  Use
        # the simpler perpendicular-only escape for these packages and
        # reserve the alternating pattern for true fine-pitch QFP/QFN.
        use_perpendicular_only = package.pin_pitch >= 0.65

        # Generate escapes for each edge
        for pads, primary_dir, alt_dir_cw, alt_dir_ccw in [
            (north_pads, EscapeDirection.NORTH, EscapeDirection.EAST, EscapeDirection.WEST),
            (south_pads, EscapeDirection.SOUTH, EscapeDirection.WEST, EscapeDirection.EAST),
            (east_pads, EscapeDirection.EAST, EscapeDirection.SOUTH, EscapeDirection.NORTH),
            (west_pads, EscapeDirection.WEST, EscapeDirection.NORTH, EscapeDirection.SOUTH),
        ]:
            for i, pad in enumerate(pads):
                if use_perpendicular_only or i % 2 == 0:
                    direction = primary_dir
                else:
                    direction = alt_dir_cw if (i // 2) % 2 == 0 else alt_dir_ccw

                escape = self._create_alternating_escape(
                    pad=pad,
                    direction=direction,
                    package=package,
                )
                escapes.append(escape)

        return escapes

    def _create_alternating_escape(
        self,
        pad: Pad,
        direction: EscapeDirection,
        package: PackageInfo,
    ) -> EscapeRoute:
        """Create an escape route with alternating direction.

        Args:
            pad: The pad to escape
            direction: Escape direction
            package: Package info

        Returns:
            EscapeRoute for this pad
        """
        dx, dy = self._direction_to_vector(direction)
        min_x, min_y, max_x, max_y = package.bounding_box

        # Calculate escape distance
        escape_dist = self.escape_clearance + self.rules.trace_width * 2

        escape_x = pad.x + dx * escape_dist
        escape_y = pad.y + dy * escape_dist

        # Create segment
        segment = Segment(
            x1=pad.x,
            y1=pad.y,
            x2=escape_x,
            y2=escape_y,
            width=self._get_trace_width_for_net(pad.net_name),
            layer=pad.layer,
            net=pad.net,
            net_name=pad.net_name,
        )

        return EscapeRoute(
            pad=pad,
            direction=direction,
            escape_point=(escape_x, escape_y),
            escape_layer=pad.layer,
            via_pos=None,
            segments=[segment],
            via=None,
            ring_index=0,
        )

    def _escape_fine_pitch_dual_row(self, package: PackageInfo) -> list[EscapeRoute]:
        """Generate escape routes with alternating layer escapes for fine-pitch SSOP/TSSOP.

        For fine-pitch dual-row packages (SSOP, TSSOP with 0.65mm or finer pitch),
        adjacent signal pins cannot route on the same layer due to clearance conflicts.
        This method implements alternating layer escape routing:

        - Even-indexed pins (0, 2, 4, ...): Escape on F.Cu (top layer)
        - Odd-indexed pins (1, 3, 5, ...): Via down to inner layer, escape there

        Pattern (for horizontal TSSOP-20):
        ```
        Pin row 1:  [1][2][3][4][5][6][7][8][9][10]
                     |  V  |  V  |  V  |  V  |  V    V = Via to inner layer
                    F.Cu  In1 F.Cu In1 F.Cu In1     Alternating layers

        Pin row 2:  [20][19][18][17][16][15][14][13][12][11]
        ```

        This ensures that adjacent pins with signal nets don't conflict with each
        other's escape routes, as they route on different layers.

        Args:
            package: SSOP/TSSOP package info

        Returns:
            List of escape routes with alternating layer assignment
        """
        escapes: list[EscapeRoute] = []
        center_x, center_y = package.center

        # Separate pads into two rows
        top_row: list[Pad] = []
        bottom_row: list[Pad] = []
        left_col: list[Pad] = []
        right_col: list[Pad] = []

        # Determine orientation by checking Y vs X spread
        xs = [p.x for p in package.pads]
        ys = [p.y for p in package.pads]
        x_spread = max(xs) - min(xs)
        y_spread = max(ys) - min(ys)

        is_horizontal = x_spread > y_spread  # pins arranged horizontally

        if is_horizontal:
            # Split by Y position
            for pad in package.pads:
                if pad.y > center_y:
                    top_row.append(pad)
                else:
                    bottom_row.append(pad)
            # Sort rows by X position
            top_row.sort(key=lambda p: p.x)
            bottom_row.sort(key=lambda p: p.x)

            # Generate escapes for each row with alternating layers
            escapes.extend(
                self._create_fine_pitch_row_escapes(
                    pads=top_row,
                    direction=EscapeDirection.NORTH,
                    package=package,
                )
            )
            escapes.extend(
                self._create_fine_pitch_row_escapes(
                    pads=bottom_row,
                    direction=EscapeDirection.SOUTH,
                    package=package,
                )
            )
        else:
            # Vertical orientation - split by X position
            for pad in package.pads:
                if pad.x > center_x:
                    right_col.append(pad)
                else:
                    left_col.append(pad)
            # Sort columns by Y position
            left_col.sort(key=lambda p: p.y)
            right_col.sort(key=lambda p: p.y)

            # Generate escapes for each column with alternating layers
            escapes.extend(
                self._create_fine_pitch_row_escapes(
                    pads=left_col,
                    direction=EscapeDirection.WEST,
                    package=package,
                )
            )
            escapes.extend(
                self._create_fine_pitch_row_escapes(
                    pads=right_col,
                    direction=EscapeDirection.EAST,
                    package=package,
                )
            )

        return escapes

    def _create_fine_pitch_row_escapes(
        self,
        pads: list[Pad],
        direction: EscapeDirection,
        package: PackageInfo,
    ) -> list[EscapeRoute]:
        """Create escape routes for fine-pitch SSOP/TSSOP with alternating layers.

        Adjacent pins escape on different layers to avoid clearance violations:
        - Even pins (index 0, 2, 4...): Stay on surface layer (F.Cu)
        - Odd pins (index 1, 3, 5...): Via to inner layer (In1.Cu or B.Cu)

        This is specifically designed for fine-pitch packages where the pitch
        (0.65mm or less) doesn't allow traces to pass between adjacent pads.

        Issue #1778: Escape segments use min_trace_width (manufacturer minimum)
        instead of net-class trace width. These segments are short (< 1mm) and
        only need to clear the pad congestion zone. Using the full trace width
        would violate clearances between adjacent fine-pitch pads.

        Issue #2319: Escape segments are validated against neighboring pad
        copper.  If a segment would violate clearance against an adjacent
        pad, the escape for that pin is omitted (deferred to the main router).
        The escape router also uses ``fine_pitch_clearance`` when configured.

        Args:
            pads: Row of pads sorted by position along the row
            direction: Primary escape direction (perpendicular to row)
            package: Package info for bounds

        Returns:
            List of escape routes with alternating layer assignment
        """
        escapes: list[EscapeRoute] = []
        dx, dy = self._direction_to_vector(direction)

        # Issue #2319: Use per-component clearance (respects fine_pitch_clearance)
        # instead of the raw trace_clearance everywhere.
        # Issue #2350: When fine_pitch_clearance is not configured in DesignRules,
        # auto-derive a clearance for fine-pitch packages based on pin pitch.
        # This method is only called for SSOP/TSSOP (confirmed fine-pitch), so we
        # can safely infer a tighter clearance when the user hasn't set one.
        ref = pads[0].ref if pads else ""
        effective_clearance = self.rules.get_clearance_for_component(
            ref, pin_pitch=package.pin_pitch,
        )

        # If get_clearance_for_component returned the default trace_clearance
        # (because fine_pitch_clearance was None), derive a workable clearance
        # from the pad geometry.  For a 0.65mm pitch SSOP with 0.35mm pads the
        # copper gap is (0.65 - 0.35) / 2 = 0.15mm; we use 80% of the
        # copper-to-copper gap to leave manufacturing margin.
        if (
            self.rules.fine_pitch_clearance is None
            and ref not in self.rules.component_clearances
            and package.pin_pitch < self.rules.fine_pitch_threshold
        ):
            # Estimate pad width along the row axis
            pad_widths = [min(p.width, p.height) for p in pads[:4]]
            avg_pad_width = sum(pad_widths) / len(pad_widths) if pad_widths else 0.3
            copper_gap = package.pin_pitch - avg_pad_width
            derived_clearance = copper_gap * 0.8
            if derived_clearance < effective_clearance:
                logger.info(
                    "Fine-pitch auto-clearance for %s: %.3fmm "
                    "(derived from %.2fmm pitch, %.2fmm pad width)",
                    ref, derived_clearance, package.pin_pitch, avg_pad_width,
                )
                effective_clearance = derived_clearance

        # Issue #1778: Use min_trace_width for escape segments in fine-pitch
        # packages. The escape segments are short and only need to clear the
        # pad congestion zone -- using the full trace width would violate
        # clearances between adjacent pads at 0.65mm pitch.
        escape_width = (
            self.rules.min_trace_width
            if self.rules.min_trace_width is not None
            else self._get_trace_width_for_net(pads[0].net_name if pads else "")
        )

        # For fine-pitch, use minimal escape distance
        # Vias placed just outside pad clearance zone
        pad_clearance = effective_clearance + package.pin_pitch / 4
        via_offset = pad_clearance + self.rules.via_diameter / 2

        # Issue #1784: Compute lateral fan-out offset for odd-pin vias when
        # adjacent escape traces would violate clearance.  The row direction
        # is perpendicular to the escape direction: if escape is (dx, dy),
        # the row axis is (-dy, dx).  Adjacent pads are separated by
        # pin_pitch along that axis.  Two parallel escape segments (one from
        # an even pin, one surface-segment from an odd pin) have edge-to-edge
        # gap = pin_pitch - escape_width.  When that gap is less than
        # trace_clearance we must shift the odd-pin via laterally.
        lateral_clearance = package.pin_pitch - escape_width
        if lateral_clearance < effective_clearance:
            lateral_offset = (effective_clearance - lateral_clearance + escape_width) / 2
        else:
            lateral_offset = 0.0

        # Row direction unit vector (perpendicular to escape direction).
        # Sign chosen so that a positive offset moves "forward" along the row.
        row_dx, row_dy = -dy, dx

        skipped_count = 0

        for i, pad in enumerate(pads):
            # Determine if this pin needs layer transition
            needs_via = i % 2 == 1  # Odd pins via down

            if needs_via:
                # Odd pin: Via to inner layer
                # Calculate via position - place via perpendicular to pin row,
                # with lateral fan-out offset to avoid clearance violations
                # against the adjacent even-pin escape segment.
                # Alternate the lateral offset direction (+/-) based on which
                # neighbour is closer, biasing away from the lower-indexed
                # (even) neighbour.
                sign = 1 if (i // 2) % 2 == 0 else -1
                # Issue #1840: Place via INWARD (toward IC body center)
                # instead of outward. The inward direction has more
                # available space under the IC body for via placement.
                via_x = pad.x - dx * via_offset + row_dx * lateral_offset * sign
                via_y = pad.y - dy * via_offset + row_dy * lateral_offset * sign

                # Issue #1840: Select inner signal layer from LayerStack
                # when available (e.g. In1.Cu on 4-layer boards), falling
                # back to B.Cu when no inner signal layers exist.
                escape_layer = self._select_inner_escape_layer(pad.layer)

                # Escape point is beyond the via on the escape layer,
                # continuing inward (same direction as via placement).
                escape_x = via_x - dx * (self.rules.via_diameter / 2 + effective_clearance)
                escape_y = via_y - dy * (self.rules.via_diameter / 2 + effective_clearance)

                # Create segments
                segments: list[Segment] = []

                # Segment from pad to via on surface layer (may be diagonal
                # when lateral_offset > 0)
                surface_seg = Segment(
                    x1=pad.x,
                    y1=pad.y,
                    x2=via_x,
                    y2=via_y,
                    width=escape_width,
                    layer=pad.layer,
                    net=pad.net,
                    net_name=pad.net_name,
                )
                segments.append(surface_seg)

                # Issue #2319: Check segment-to-pad clearance for the surface
                # segment against neighboring pads before committing.
                if self._segment_violates_pad_clearance(
                    surface_seg, i, pads, effective_clearance,
                ):
                    skipped_count += 1
                    logger.debug(
                        "Escape for pad %s (pin %d) skipped: segment-to-pad "
                        "clearance violation (deferred to main router)",
                        pad.net_name, i,
                    )
                    continue

                # Create via
                via = Via(
                    x=via_x,
                    y=via_y,
                    drill=self.rules.via_drill,
                    diameter=self.rules.via_diameter,
                    layers=(pad.layer, escape_layer),
                    net=pad.net,
                    net_name=pad.net_name,
                )

                # Segment from via to escape point on inner layer
                segments.append(
                    Segment(
                        x1=via_x,
                        y1=via_y,
                        x2=escape_x,
                        y2=escape_y,
                        width=escape_width,
                        layer=escape_layer,
                        net=pad.net,
                        net_name=pad.net_name,
                    )
                )

                escapes.append(
                    EscapeRoute(
                        pad=pad,
                        direction=direction,
                        escape_point=(escape_x, escape_y),
                        escape_layer=escape_layer,
                        via_pos=(via_x, via_y),
                        segments=segments,
                        via=via,
                        ring_index=0,
                    )
                )
            else:
                # Even pin: Stay on surface layer
                # Simple escape perpendicular to pin row
                escape_dist = self.escape_clearance + self.rules.trace_width
                escape_x = pad.x + dx * escape_dist
                escape_y = pad.y + dy * escape_dist

                # Create segment from pad to escape point
                segment = Segment(
                    x1=pad.x,
                    y1=pad.y,
                    x2=escape_x,
                    y2=escape_y,
                    width=escape_width,
                    layer=pad.layer,
                    net=pad.net,
                    net_name=pad.net_name,
                )

                # Issue #2319: Check segment-to-pad clearance before committing.
                if self._segment_violates_pad_clearance(
                    segment, i, pads, effective_clearance,
                ):
                    skipped_count += 1
                    logger.debug(
                        "Escape for pad %s (pin %d) skipped: segment-to-pad "
                        "clearance violation (deferred to main router)",
                        pad.net_name, i,
                    )
                    continue

                escapes.append(
                    EscapeRoute(
                        pad=pad,
                        direction=direction,
                        escape_point=(escape_x, escape_y),
                        escape_layer=pad.layer,
                        via_pos=None,
                        segments=[segment],
                        via=None,
                        ring_index=0,
                    )
                )

        if skipped_count:
            logger.warning(
                "Escape routing for %s: %d of %d pins deferred to main router "
                "(clearance violation)",
                ref, skipped_count, len(pads),
            )

        # Issue #1784: Post-generation pairwise clearance validation
        # Issue #2319: Use effective_clearance (respects fine_pitch_clearance)
        self._validate_escape_clearances(escapes, effective_clearance, pads)

        return escapes

    @staticmethod
    def _segment_to_pad_edge_gap(seg: Segment, pad: Pad) -> float:
        """Return the minimum edge-to-edge gap between a segment and a pad.

        The pad is modelled as a rectangle centred at (pad.x, pad.y) with
        half-extents (pad.width/2, pad.height/2).  The segment centre-line
        runs from (seg.x1, seg.y1) to (seg.x2, seg.y2).

        The closest distance from the segment centre-line to the pad
        rectangle boundary is computed, then both the segment half-width
        and pad half-extent (in the direction of the closest approach) are
        subtracted to yield the edge-to-edge gap.

        A negative return value means the segment copper overlaps the pad
        copper.
        """
        # Closest point on the segment to the pad centre
        sx, sy = seg.x2 - seg.x1, seg.y2 - seg.y1
        seg_len_sq = sx * sx + sy * sy
        if seg_len_sq < 1e-12:
            # Degenerate segment (zero length)
            cpx, cpy = seg.x1, seg.y1
        else:
            t = max(0.0, min(1.0,
                ((pad.x - seg.x1) * sx + (pad.y - seg.y1) * sy) / seg_len_sq))
            cpx = seg.x1 + t * sx
            cpy = seg.y1 + t * sy

        # Distance from closest point on segment to the pad rectangle edge.
        # The pad is axis-aligned (no rotation support needed for SOP pads).
        half_w = pad.width / 2
        half_h = pad.height / 2
        dx_abs = abs(cpx - pad.x)
        dy_abs = abs(cpy - pad.y)

        # Signed distance from pad rectangle (negative = inside)
        outside_x = max(0.0, dx_abs - half_w)
        outside_y = max(0.0, dy_abs - half_h)

        if outside_x == 0.0 and outside_y == 0.0:
            # Point is inside the pad rectangle
            rect_dist = -min(half_w - dx_abs, half_h - dy_abs)
        else:
            rect_dist = math.sqrt(outside_x * outside_x + outside_y * outside_y)

        # Edge-to-edge gap = centre-to-rect distance minus half-segment-width
        return rect_dist - seg.width / 2

    def _segment_violates_pad_clearance(
        self,
        seg: Segment,
        pad_index: int,
        pads: list[Pad],
        min_clearance: float,
    ) -> bool:
        """Check whether *seg* violates clearance against pads in the row.

        Issue #2350: Checks ALL pads in the row, not just immediate neighbors.
        On fine-pitch packages (e.g. 20-pin SSOP), a lateral escape may
        violate clearance against pad[i+2] while only pad[i+1] was previously
        checked.  The segment's own pad (at pad_index) is skipped because the
        segment originates from it.

        Returns True if any pad in the row is violated.
        """
        for neighbor_idx in range(len(pads)):
            if neighbor_idx == pad_index:
                continue
            neighbor = pads[neighbor_idx]
            # Only check pads on the same layer as the segment
            if neighbor.layer != seg.layer:
                continue
            gap = self._segment_to_pad_edge_gap(seg, neighbor)
            if gap < min_clearance - 1e-6:
                return True
        return False

    @staticmethod
    def _min_segment_distance(s1: Segment, s2: Segment) -> float:
        """Return the minimum centre-line distance between two segments.

        Uses closest-point-on-segment computation for each pair of
        endpoints/projections.  This is the geometric distance between the
        two line-segments (not accounting for trace width -- the caller
        subtracts half-widths separately).
        """

        def _dot(ax: float, ay: float, bx: float, by: float) -> float:
            return ax * bx + ay * by

        def _clamp01(v: float) -> float:
            return max(0.0, min(1.0, v))

        def _point_seg_dist(
            px: float, py: float, ax: float, ay: float, bx: float, by: float
        ) -> float:
            abx, aby = bx - ax, by - ay
            apx, apy = px - ax, py - ay
            len_sq = abx * abx + aby * aby
            if len_sq < 1e-12:
                return math.sqrt(apx * apx + apy * apy)
            t = _clamp01(_dot(apx, apy, abx, aby) / len_sq)
            cx, cy = ax + t * abx, ay + t * aby
            dx, dy_val = px - cx, py - cy
            return math.sqrt(dx * dx + dy_val * dy_val)

        # Check all four endpoint-to-segment distances, plus
        # segment-segment closest approach.
        d1 = _point_seg_dist(s1.x1, s1.y1, s2.x1, s2.y1, s2.x2, s2.y2)
        d2 = _point_seg_dist(s1.x2, s1.y2, s2.x1, s2.y1, s2.x2, s2.y2)
        d3 = _point_seg_dist(s2.x1, s2.y1, s1.x1, s1.y1, s1.x2, s1.y2)
        d4 = _point_seg_dist(s2.x2, s2.y2, s1.x1, s1.y1, s1.x2, s1.y2)
        return min(d1, d2, d3, d4)

    def _validate_escape_clearances(
        self,
        escapes: list[EscapeRoute],
        min_clearance: float,
        row_pads: list[Pad] | None = None,
    ) -> None:
        """Validate pairwise clearance between consecutive escape routes.

        Iterates through adjacent escape routes and checks that all
        surface-layer segments maintain at least *min_clearance* edge-to-edge
        distance.  Logs a warning for any violating pair so that regressions
        are visible without silently producing DRC violations.

        Issue #2319: When *row_pads* is provided, also validates each
        segment against neighboring pad copper (segment-to-pad clearance).
        """
        # Segment-to-segment validation (original)
        for idx in range(len(escapes) - 1):
            e1 = escapes[idx]
            e2 = escapes[idx + 1]
            for seg1 in e1.segments:
                for seg2 in e2.segments:
                    if seg1.layer != seg2.layer:
                        continue  # different layers cannot violate
                    centre_dist = self._min_segment_distance(seg1, seg2)
                    edge_gap = centre_dist - (seg1.width + seg2.width) / 2
                    if edge_gap < min_clearance - 1e-6:
                        logger.warning(
                            "Escape clearance violation between pads %s and %s "
                            "on %s: gap=%.4fmm (required %.4fmm)",
                            e1.pad.net_name,
                            e2.pad.net_name,
                            seg1.layer.kicad_name,
                            edge_gap,
                            min_clearance,
                        )

        # Issue #2319: Segment-to-pad validation
        if row_pads:
            # Build a quick lookup: pad -> index in row
            pad_indices: dict[int, int] = {id(p): idx for idx, p in enumerate(row_pads)}
            for escape in escapes:
                pad_idx = pad_indices.get(id(escape.pad))
                if pad_idx is None:
                    continue
                for seg in escape.segments:
                    # Check against neighboring pads (not the escape's own pad)
                    for neighbor_offset in (-1, 1):
                        ni = pad_idx + neighbor_offset
                        if ni < 0 or ni >= len(row_pads):
                            continue
                        neighbor = row_pads[ni]
                        if neighbor.layer != seg.layer:
                            continue
                        gap = self._segment_to_pad_edge_gap(seg, neighbor)
                        if gap < min_clearance - 1e-6:
                            logger.warning(
                                "Escape segment-to-pad clearance violation: "
                                "segment of %s vs pad %s on %s: "
                                "gap=%.4fmm (required %.4fmm)",
                                escape.pad.net_name,
                                neighbor.net_name,
                                seg.layer.kicad_name,
                                gap,
                                min_clearance,
                            )

    def _escape_sop_staggered(self, package: PackageInfo) -> list[EscapeRoute]:
        """Generate escape routes with staggered vias for SOP/TSSOP/SOIC packages.

        For dual-row packages (SOP, TSSOP, SOIC), pins escape perpendicular to
        the pin row, with vias placed in a staggered pattern to prevent blocking
        adjacent pins.

        Pattern (for horizontal dual-row):
        ```
        Pin row 1:  [1][2][3][4][5][6][7][8]
                     |  |  |  |  |  |  |  |
        Escape:     -+--|--+--|--+--|--+--|
                     |  |  |  |  |  |  |  |
        Via row 1:  [V]    [V]    [V]    [V]  (odd pins)
        Via row 2:     [V]    [V]    [V]    [V] (even pins, offset)

        Pin row 2:  [16][15][14][13][12][11][10][9]
        ```

        The staggered pattern ensures that vias from one pin don't block the
        escape path of adjacent pins, allowing all pins to route out successfully.

        Args:
            package: SOP/TSSOP/SOIC package info

        Returns:
            List of escape routes with staggered via placement
        """
        escapes: list[EscapeRoute] = []
        center_x, center_y = package.center

        # Separate pads into two rows
        top_row: list[Pad] = []
        bottom_row: list[Pad] = []
        left_col: list[Pad] = []
        right_col: list[Pad] = []

        # Determine orientation by checking Y vs X spread
        xs = [p.x for p in package.pads]
        ys = [p.y for p in package.pads]
        x_spread = max(xs) - min(xs)
        y_spread = max(ys) - min(ys)

        is_horizontal = x_spread > y_spread  # pins arranged horizontally (typical SOP)

        if is_horizontal:
            # Split by Y position
            for pad in package.pads:
                if pad.y > center_y:
                    top_row.append(pad)
                else:
                    bottom_row.append(pad)
            # Sort rows by X position
            top_row.sort(key=lambda p: p.x)
            bottom_row.sort(key=lambda p: p.x)

            # Generate escapes for each row
            escapes.extend(
                self._create_staggered_row_escapes(
                    pads=top_row,
                    direction=EscapeDirection.NORTH,
                    package=package,
                )
            )
            escapes.extend(
                self._create_staggered_row_escapes(
                    pads=bottom_row,
                    direction=EscapeDirection.SOUTH,
                    package=package,
                )
            )
        else:
            # Vertical orientation - split by X position
            for pad in package.pads:
                if pad.x > center_x:
                    right_col.append(pad)
                else:
                    left_col.append(pad)
            # Sort columns by Y position
            left_col.sort(key=lambda p: p.y)
            right_col.sort(key=lambda p: p.y)

            # Generate escapes for each column
            escapes.extend(
                self._create_staggered_row_escapes(
                    pads=left_col,
                    direction=EscapeDirection.WEST,
                    package=package,
                )
            )
            escapes.extend(
                self._create_staggered_row_escapes(
                    pads=right_col,
                    direction=EscapeDirection.EAST,
                    package=package,
                )
            )

        return escapes

    def _create_staggered_row_escapes(
        self,
        pads: list[Pad],
        direction: EscapeDirection,
        package: PackageInfo,
    ) -> list[EscapeRoute]:
        """Create escape routes for a row of pads with staggered via placement.

        Args:
            pads: Row of pads sorted by position
            direction: Primary escape direction (perpendicular to row)
            package: Package info for bounds

        Returns:
            List of escape routes with staggered vias
        """
        escapes: list[EscapeRoute] = []
        dx, dy = self._direction_to_vector(direction)

        # Calculate base escape distance and stagger offset
        base_escape_dist = self.escape_clearance + self.rules.trace_width
        stagger_offset = self.via_spacing / 2

        for i, pad in enumerate(pads):
            # Stagger: odd pins get extra offset (two via rows)
            is_odd = i % 2 == 1
            escape_dist = base_escape_dist + (stagger_offset if is_odd else 0)

            # Calculate via position (perpendicular to pin row)
            via_x = pad.x + dx * escape_dist
            via_y = pad.y + dy * escape_dist

            # Escape point is beyond the via
            escape_x = via_x + dx * (self.rules.via_diameter + self.rules.trace_clearance)
            escape_y = via_y + dy * (self.rules.via_diameter + self.rules.trace_clearance)

            # Determine escape layer (alternate layers for denser routing)
            escape_layer = Layer.B_CU if is_odd else Layer.F_CU

            # Create segments
            segments: list[Segment] = []

            # Segment from pad to via
            segments.append(
                Segment(
                    x1=pad.x,
                    y1=pad.y,
                    x2=via_x,
                    y2=via_y,
                    width=self._get_trace_width_for_net(pad.net_name),
                    layer=pad.layer,
                    net=pad.net,
                    net_name=pad.net_name,
                )
            )

            # Create via
            via = Via(
                x=via_x,
                y=via_y,
                drill=self.rules.via_drill,
                diameter=self.rules.via_diameter,
                layers=(pad.layer, escape_layer),
                net=pad.net,
                net_name=pad.net_name,
            )

            # Segment from via to escape point on escape layer
            segments.append(
                Segment(
                    x1=via_x,
                    y1=via_y,
                    x2=escape_x,
                    y2=escape_y,
                    width=self._get_trace_width_for_net(pad.net_name),
                    layer=escape_layer,
                    net=pad.net,
                    net_name=pad.net_name,
                )
            )

            escapes.append(
                EscapeRoute(
                    pad=pad,
                    direction=direction,
                    escape_point=(escape_x, escape_y),
                    escape_layer=escape_layer,
                    via_pos=(via_x, via_y),
                    segments=segments,
                    via=via,
                    ring_index=0,
                )
            )

        return escapes

    def _escape_multi_row_connector(self, package: PackageInfo) -> list[EscapeRoute]:
        """Generate BGA-style fanout escape routes for multi-row connectors.

        Multi-row through-hole connectors (e.g., 2x20 pin headers at 2.54mm
        pitch) cannot use simple radial escape because inner-row pads are
        blocked by outer-row escape paths.

        Strategy (row-aware, analogous to BGA ring escape):
        - Rows are sorted by distance from package center (outermost first).
        - Outer rows (ring_index 0): escape perpendicular on the surface
          layer (F.Cu).  No via needed.
        - Inner rows (ring_index >= 1): short trace to a staggered via,
          then escape on an inner/back layer selected via
          ``_select_inner_escape_layer()``.
        - Via positions within each inner row are staggered along the row
          axis to maintain via-to-via clearance.

        Works for 2xN, 3xN, and 4xN through-hole arrangements.

        Args:
            package: MULTI_ROW_CONNECTOR package info (>= 20 pins, multi-row, TH)

        Returns:
            List of escape routes with layer-aware escape
        """
        escapes: list[EscapeRoute] = []
        center_x, center_y = package.center

        # Determine connector orientation from pad positions
        xs = [p.x for p in package.pads]
        ys = [p.y for p in package.pads]
        x_spread = max(xs) - min(xs)
        y_spread = max(ys) - min(ys)

        # "horizontal" means the long axis is X (many columns, few rows of Y)
        is_horizontal = x_spread > y_spread

        # Group pads into rows.  For a horizontal connector the rows are
        # distinguished by their Y coordinate; for vertical, by X.
        if is_horizontal:
            row_coords = sorted({round(p.y, 2) for p in package.pads})
            rows_map: dict[float, list[Pad]] = {rc: [] for rc in row_coords}
            for pad in package.pads:
                rows_map[round(pad.y, 2)].append(pad)
            for rc in row_coords:
                rows_map[rc].sort(key=lambda p: p.x)
        else:
            row_coords = sorted({round(p.x, 2) for p in package.pads})
            rows_map = {rc: [] for rc in row_coords}
            for pad in package.pads:
                rows_map[round(pad.x, 2)].append(pad)
            for rc in row_coords:
                rows_map[rc].sort(key=lambda p: p.y)

        # Sort rows by distance from center (outermost first)
        if is_horizontal:
            sorted_coords = sorted(row_coords, key=lambda c: abs(c - center_y), reverse=True)
        else:
            sorted_coords = sorted(row_coords, key=lambda c: abs(c - center_x), reverse=True)

        # Select inner escape layer once (not hardcoded)
        inner_escape_layer = self._select_inner_escape_layer(Layer.F_CU)

        escape_dist = self.escape_clearance + self.rules.trace_width
        via_offset_base = (
            self.rules.via_diameter / 2
            + self.rules.via_clearance
            + self.rules.trace_clearance
        )

        for ring_idx, coord in enumerate(sorted_coords):
            row_pads = rows_map[coord]
            is_outer = ring_idx == 0

            # Determine perpendicular escape direction for this row
            if is_horizontal:
                direction = (
                    EscapeDirection.NORTH if coord > center_y else EscapeDirection.SOUTH
                )
            else:
                direction = (
                    EscapeDirection.EAST if coord > center_x else EscapeDirection.WEST
                )

            dx, dy = self._direction_to_vector(direction)

            for i, pad in enumerate(row_pads):
                trace_width = self._get_trace_width_for_net(pad.net_name)

                if is_outer:
                    # Outer row: surface escape, no via
                    ep_x = pad.x + dx * escape_dist
                    ep_y = pad.y + dy * escape_dist

                    segment = Segment(
                        x1=pad.x, y1=pad.y, x2=ep_x, y2=ep_y,
                        width=trace_width, layer=pad.layer,
                        net=pad.net, net_name=pad.net_name,
                    )

                    escapes.append(
                        EscapeRoute(
                            pad=pad,
                            direction=direction,
                            escape_point=(ep_x, ep_y),
                            escape_layer=pad.layer,
                            via_pos=None,
                            segments=[segment],
                            via=None,
                            ring_index=0,
                        )
                    )
                else:
                    # Inner row: via down to alternate layer with staggered
                    # via placement.  Alternate vias toward pin-1 / pin-N
                    # along the row axis, offset by via_spacing / 2.
                    stagger = (self.via_spacing / 2) * (1.0 if i % 2 == 0 else -1.0)

                    if is_horizontal:
                        via_x = pad.x + stagger
                        via_y = pad.y + dy * via_offset_base
                    else:
                        via_x = pad.x + dx * via_offset_base
                        via_y = pad.y + stagger

                    # Escape point beyond via on the inner layer
                    ep_x = via_x + dx * (
                        self.rules.via_diameter / 2 + self.rules.trace_clearance
                    )
                    ep_y = via_y + dy * (
                        self.rules.via_diameter / 2 + self.rules.trace_clearance
                    )

                    segments: list[Segment] = [
                        Segment(
                            x1=pad.x, y1=pad.y, x2=via_x, y2=via_y,
                            width=trace_width, layer=pad.layer,
                            net=pad.net, net_name=pad.net_name,
                        ),
                        Segment(
                            x1=via_x, y1=via_y, x2=ep_x, y2=ep_y,
                            width=trace_width, layer=inner_escape_layer,
                            net=pad.net, net_name=pad.net_name,
                        ),
                    ]

                    via = Via(
                        x=via_x, y=via_y,
                        drill=self.rules.via_drill,
                        diameter=self.rules.via_diameter,
                        layers=(pad.layer, inner_escape_layer),
                        net=pad.net, net_name=pad.net_name,
                    )

                    escapes.append(
                        EscapeRoute(
                            pad=pad,
                            direction=direction,
                            escape_point=(ep_x, ep_y),
                            escape_layer=inner_escape_layer,
                            via_pos=(via_x, via_y),
                            segments=segments,
                            via=via,
                            ring_index=ring_idx,
                        )
                    )

        # Validate pairwise clearances within each row
        self._validate_escape_clearances(escapes, self.rules.trace_clearance)

        return escapes

    def _escape_radial(self, package: PackageInfo) -> list[EscapeRoute]:
        """Generate simple radial escapes for non-dense packages.

        Each pin escapes directly outward from package center.

        Args:
            package: Package info

        Returns:
            List of escape routes
        """
        escapes: list[EscapeRoute] = []
        center_x, center_y = package.center

        for pad in package.pads:
            # Issue #2513: Skip plane-net pads (net=0) -- they are stitched
            # via planes, not routed via escapes.
            if pad.net == 0:
                continue

            direction = self._get_quadrant_direction(pad.x, pad.y, center_x, center_y)
            dx, dy = self._direction_to_vector(direction)

            escape_dist = self.escape_clearance
            escape_x = pad.x + dx * escape_dist
            escape_y = pad.y + dy * escape_dist

            segment = Segment(
                x1=pad.x,
                y1=pad.y,
                x2=escape_x,
                y2=escape_y,
                width=self._get_trace_width_for_net(pad.net_name),
                layer=pad.layer,
                net=pad.net,
                net_name=pad.net_name,
            )

            escapes.append(
                EscapeRoute(
                    pad=pad,
                    direction=direction,
                    escape_point=(escape_x, escape_y),
                    escape_layer=pad.layer,
                    segments=[segment],
                )
            )

        return escapes

    def staggered_via_fanout(
        self,
        pads: list[Pad],
        stagger_distance: float | None = None,
    ) -> list[Via]:
        """Generate staggered via pattern under dense package.

        Places vias in a dog-bone pattern, offsetting via positions
        based on row/column to prevent via-to-via DRC violations.

        Args:
            pads: Pads to create fanout vias for
            stagger_distance: Offset distance for stagger (defaults to via_spacing/2)

        Returns:
            List of Via objects in staggered pattern
        """
        if not pads:
            return []

        stagger = stagger_distance or (self.via_spacing / 2)
        vias: list[Via] = []

        # Group by approximate row/column
        rows = self._group_pads_to_grid(pads)

        for row_idx, row in enumerate(rows):
            for col_idx, pad in enumerate(row):
                # Offset based on row and column parity
                offset_x = (col_idx % 2) * stagger
                offset_y = (row_idx % 2) * stagger

                via_x = pad.x + offset_x
                via_y = pad.y + offset_y

                # Check if position is valid in grid
                if self._can_place_via(via_x, via_y):
                    via = Via(
                        x=via_x,
                        y=via_y,
                        drill=self.rules.via_drill,
                        diameter=self.rules.via_diameter,
                        layers=(Layer.F_CU, Layer.B_CU),
                        net=pad.net,
                        net_name=pad.net_name,
                    )
                    vias.append(via)

        return vias

    def _group_pads_to_grid(self, pads: list[Pad]) -> list[list[Pad]]:
        """Group pads into a 2D grid structure."""
        if not pads:
            return []

        # Find unique Y positions (rows)
        y_positions = sorted({round(p.y, 2) for p in pads})

        rows: list[list[Pad]] = []
        for y in y_positions:
            row = [p for p in pads if abs(p.y - y) < 0.1]
            row.sort(key=lambda p: p.x)
            rows.append(row)

        return rows

    def _can_place_via(self, x: float, y: float) -> bool:
        """Check if a via can be placed at the given position."""
        # Check grid bounds
        if not (0 <= x <= self.grid.width and 0 <= y <= self.grid.height):
            return False

        # Check for obstacles in grid
        gx, gy = self.grid.world_to_grid(x, y)
        if 0 <= gx < self.grid.cols and 0 <= gy < self.grid.rows:
            for layer_idx in range(self.grid.num_layers):
                cell = self.grid.grid[layer_idx][gy][gx]
                if cell.blocked and cell.is_obstacle:
                    return False

        return True

    def _get_quadrant_direction(
        self,
        x: float,
        y: float,
        center_x: float,
        center_y: float,
    ) -> EscapeDirection:
        """Determine escape direction based on quadrant relative to center."""
        dx = x - center_x
        dy = y - center_y

        # Determine primary direction based on which axis is dominant
        if abs(dx) > abs(dy):
            if dx > 0:
                return EscapeDirection.EAST
            else:
                return EscapeDirection.WEST
        else:
            if dy > 0:
                return EscapeDirection.NORTH
            else:
                return EscapeDirection.SOUTH

    def _select_inner_escape_layer(self, surface_layer: Layer) -> Layer:
        """Select the best inner layer for via escape routing.

        Queries the grid's LayerStack for available inner signal layers.
        Prefers the first inner signal layer (typically In1.Cu on 4-layer
        boards) over B.Cu, since inner layers provide shorter via stubs
        and better signal integrity.

        Falls back to B.Cu when no inner signal layers are available
        (e.g., on 2-layer boards or when all inner layers are planes).

        Args:
            surface_layer: The surface layer the pad is on (used as fallback
                reference -- the via must transition away from this layer).

        Returns:
            The selected escape layer (inner signal layer or B.Cu fallback).
        """
        if self.grid.layer_stack is not None:
            inner_indices = self.grid.layer_stack.get_inner_layer_indices()
            for idx in inner_indices:
                layer_def = self.grid.layer_stack.get_layer(idx)
                if layer_def is not None and layer_def.layer_type == LayerType.SIGNAL:
                    return layer_def.layer_enum
        # Fallback: use B.Cu (opposite outer layer)
        return Layer.B_CU

    def _direction_to_vector(self, direction: EscapeDirection) -> tuple[float, float]:
        """Convert escape direction to unit vector."""
        vectors = {
            EscapeDirection.NORTH: (0, 1),
            EscapeDirection.SOUTH: (0, -1),
            EscapeDirection.EAST: (1, 0),
            EscapeDirection.WEST: (-1, 0),
            EscapeDirection.NORTHEAST: (0.707, 0.707),
            EscapeDirection.NORTHWEST: (-0.707, 0.707),
            EscapeDirection.SOUTHEAST: (0.707, -0.707),
            EscapeDirection.SOUTHWEST: (-0.707, -0.707),
            EscapeDirection.VIA_DOWN: (0, 0),
        }
        return vectors.get(direction, (0, 0))

    def apply_escape_routes(self, escapes: list[EscapeRoute]) -> list[Route]:
        """Apply escape routes to the grid and return as Route objects.

        Marks escape paths on the grid to reserve them for routing,
        and converts escape routes to standard Route objects.

        Args:
            escapes: List of escape routes to apply

        Returns:
            List of Route objects representing the escapes
        """
        routes: list[Route] = []

        for escape in escapes:
            route = Route(
                net=escape.pad.net,
                net_name=escape.pad.net_name,
                segments=escape.segments,
                vias=[escape.via] if escape.via else [],
            )

            # Mark on grid
            self.grid.mark_route(route)
            routes.append(route)

        return routes
