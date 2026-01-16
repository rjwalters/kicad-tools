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

import math
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .grid import RoutingGrid
    from .rules import DesignRules

from .layers import Layer
from .primitives import Pad, Route, Segment, Via


class PackageType(Enum):
    """Package type classification for escape routing."""

    UNKNOWN = auto()
    BGA = auto()  # Ball Grid Array
    QFP = auto()  # Quad Flat Package
    QFN = auto()  # Quad Flat No-lead
    TQFP = auto()  # Thin Quad Flat Package
    SOP = auto()  # Small Outline Package
    SOT = auto()  # Small Outline Transistor
    DIP = auto()  # Dual In-line Package
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

    # Calculate minimum pin pitch
    min_pitch = _calculate_min_pitch(pads)
    if min_pitch <= 0:
        return False

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
        return PackageType.DIP if _is_dual_row(pads) else PackageType.THROUGH_HOLE

    # Calculate bounding box and center
    xs = [p.x for p in pads]
    ys = [p.y for p in pads]
    width = max(xs) - min(xs)
    height = max(ys) - min(ys)
    center_x = (max(xs) + min(xs)) / 2
    center_y = (max(ys) + min(ys)) / 2

    # IMPORTANT: Check detection order matters!
    # 1. Dual-row packages (SOP) - only 2 rows of pads
    # 2. Quad packages (QFP/QFN) - pads on 4 edges, empty interior
    # 3. Grid packages (BGA) - filled grid throughout

    # Check for dual-row first (SOP) - most specific
    if _is_dual_row(pads):
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

    # Estimate rows/cols for grid packages
    rows, cols = 0, 0
    if package_type == PackageType.BGA:
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
    ):
        """Initialize the escape router.

        Args:
            grid: Routing grid to work with
            rules: Design rules for dimensions
            via_spacing: Minimum via-to-via spacing (defaults to via_diameter + clearance)
            escape_clearance: Clearance from package edge (defaults to trace_clearance * 2)
        """
        self.grid = grid
        self.rules = rules
        self.via_spacing = via_spacing or (rules.via_diameter + rules.via_clearance)
        self.escape_clearance = escape_clearance or (rules.trace_clearance * 2)

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
        - Other: Simple radial escape

        Args:
            package: Package info from analyze_package()

        Returns:
            List of EscapeRoute objects for each pin
        """
        if package.package_type == PackageType.BGA:
            return self._escape_bga_rings(package)
        elif package.package_type in (
            PackageType.QFP,
            PackageType.QFN,
            PackageType.TQFP,
        ):
            return self._escape_qfp_alternating(package)
        else:
            return self._escape_radial(package)

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
                    width=self.rules.trace_width,
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
                    width=self.rules.trace_width,
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
                    width=self.rules.trace_width,
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

        # Generate escapes for each edge
        for pads, primary_dir, alt_dir_cw, alt_dir_ccw in [
            (north_pads, EscapeDirection.NORTH, EscapeDirection.EAST, EscapeDirection.WEST),
            (south_pads, EscapeDirection.SOUTH, EscapeDirection.WEST, EscapeDirection.EAST),
            (east_pads, EscapeDirection.EAST, EscapeDirection.SOUTH, EscapeDirection.NORTH),
            (west_pads, EscapeDirection.WEST, EscapeDirection.NORTH, EscapeDirection.SOUTH),
        ]:
            for i, pad in enumerate(pads):
                if i % 2 == 0:
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
            width=self.rules.trace_width,
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
                width=self.rules.trace_width,
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
