"""Clearance rule implementation for DRC checks.

Validates minimum spacing between copper elements (traces, pads, vias)
on the same layer but different nets.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING

from kicad_tools.core.geometry import (
    point_to_segment_distance as _point_to_segment_distance,
)
from kicad_tools.core.geometry import (
    segment_to_segment_distance as _segment_to_segment_distance,
)

from ..violations import DRCResults, DRCViolation
from .base import DRC_TOLERANCE, DRCRule

if TYPE_CHECKING:
    from kicad_tools.manufacturers import DesignRules
    from kicad_tools.schema.pcb import PCB, Footprint, Pad, Segment, Via


# Floating-point tolerance for detecting segment-endpoint / via-center
# co-location (0.1 micron).  The router's in-pad escape places the inner-
# layer segment endpoint at *exactly* the via center (router invariant --
# see ``_try_in_pad_escape`` in ``src/kicad_tools/router/escape.py``).
# However, when a different net's escape segment happens to terminate at
# the same coordinates (e.g. a neighboring pad's escape segment landing on
# top of a fine-pitch in-pad via center after pitch-aware placement), the
# clearance check sees a "segment endpoint at via center on a different
# net" pair and reports a false-positive ``clearance_segment_via``
# violation -- the segment endpoint is conceptually the via, not a piece
# of trace copper at that exact point.  An epsilon of 1e-4 mm (matching
# ``_CLEARANCE_EPSILON_MM`` in ``edge.py``) is far below any real-world
# manufacturing precision but well above IEEE-754 representation error
# for the router's coordinate space, so it suppresses the modeling
# artifact without masking real near-misses.  See Issue #2706.
_COLOCATION_EPSILON_MM = 1e-4


@dataclass
class CopperElement:
    """A copper element for clearance checking.

    Provides a unified interface for segments, pads, and vias
    to simplify distance calculations.
    """

    element_type: str  # "segment", "pad", "via"
    layer: str
    net_number: int
    # For segments: (start_x, start_y, end_x, end_y, width)
    # For pads/vias: (center_x, center_y, width, height)
    geometry: tuple[float, ...]
    # Reference for violation reporting
    reference: str
    # Net name for violation output (empty string for unconnected/net 0)
    net_name: str = ""

    @classmethod
    def from_segment(cls, seg: Segment) -> CopperElement:
        """Create from a PCB segment."""
        return cls(
            element_type="segment",
            layer=seg.layer,
            net_number=seg.net_number,
            geometry=(seg.start[0], seg.start[1], seg.end[0], seg.end[1], seg.width),
            reference=f"Trace-{seg.uuid[:8]}" if seg.uuid else "Trace",
            net_name=seg.net_name if seg.net_number != 0 else "",
        )

    @classmethod
    def from_pad(cls, pad: Pad, footprint: Footprint) -> CopperElement:
        """Create from a PCB pad with footprint context."""
        # Transform pad position from footprint-local to board coordinates
        abs_x, abs_y = _transform_pad_position(pad, footprint)
        # Transform pad dimensions to axis-aligned bounding box
        width, height = _transform_pad_dimensions(pad, footprint)
        return cls(
            element_type="pad",
            layer="*",  # Pads can span multiple layers
            net_number=pad.net_number,
            geometry=(abs_x, abs_y, width, height),
            reference=f"{footprint.reference}-{pad.number}",
            net_name=pad.net_name if pad.net_number != 0 else "",
        )

    @classmethod
    def from_via(cls, via: Via) -> CopperElement:
        """Create from a PCB via."""
        return cls(
            element_type="via",
            layer="*",  # Vias span multiple layers
            net_number=via.net_number,
            geometry=(via.position[0], via.position[1], via.size, via.size),
            reference=f"Via-{via.uuid[:8]}" if via.uuid else "Via",
            net_name=via.net_name if via.net_number != 0 else "",
        )

    def on_layer(self, layer: str) -> bool:
        """Check if this element is on the specified layer."""
        if self.layer == "*":
            return True  # Pads and vias span multiple layers
        return self.layer == layer


def _transform_pad_position(pad: Pad, footprint: Footprint) -> tuple[float, float]:
    """Transform pad position from footprint-local to board coordinates.

    KiCad uses counter-clockwise positive rotation (standard math convention).
    """
    # Apply rotation using standard 2D rotation matrix
    angle_rad = math.radians(footprint.rotation)
    cos_a = math.cos(angle_rad)
    sin_a = math.sin(angle_rad)

    # Rotate pad position around footprint origin
    local_x, local_y = pad.position
    rotated_x = local_x * cos_a - local_y * sin_a
    rotated_y = local_x * sin_a + local_y * cos_a

    # Translate to board coordinates
    abs_x = footprint.position[0] + rotated_x
    abs_y = footprint.position[1] + rotated_y

    return abs_x, abs_y


def _transform_pad_dimensions(pad: Pad, footprint: Footprint) -> tuple[float, float]:
    """Transform pad dimensions to axis-aligned bounding box in board coordinates.

    For rectangular pads, when the footprint is rotated, the pad's effective
    width and height in board coordinates change. This function computes the
    axis-aligned bounding box dimensions of the rotated pad.

    For cardinal rotations (90°, 270°), this simply swaps width and height.
    For arbitrary rotations, this computes the axis-aligned bounding box.

    Args:
        pad: The pad whose dimensions to transform
        footprint: The footprint containing the pad (provides rotation)

    Returns:
        Tuple of (width, height) representing the axis-aligned bounding box
    """
    width, height = pad.size

    # Get total rotation from footprint
    # Note: pad.rotation is relative to footprint, footprint.rotation is absolute
    total_rotation = footprint.rotation

    # Normalize rotation to [0, 360)
    total_rotation = total_rotation % 360

    # For cardinal rotations, we can simply swap dimensions
    if abs(total_rotation - 90) < 0.001 or abs(total_rotation - 270) < 0.001:
        return height, width
    elif abs(total_rotation) < 0.001 or abs(total_rotation - 180) < 0.001:
        return width, height

    # For arbitrary rotations, compute the axis-aligned bounding box
    # of the rotated rectangle (sign doesn't matter since we use abs values)
    angle_rad = math.radians(total_rotation)
    cos_a = abs(math.cos(angle_rad))
    sin_a = abs(math.sin(angle_rad))

    # The bounding box of a rotated rectangle
    new_width = width * cos_a + height * sin_a
    new_height = width * sin_a + height * cos_a

    return new_width, new_height


# _point_to_segment_distance and _segment_to_segment_distance are imported
# from kicad_tools.core.geometry (consolidated in #2349).


def _build_diff_pair_set(pcb: PCB) -> set[tuple[int, int]]:
    """Return ``{(min_net_id, max_net_id)}`` for every detected diff pair.

    Used by :class:`ClearanceRule` to skip same-pair segment-segment edges
    that are instead validated by ``DiffPairClearanceIntraRule`` against
    the per-class ``intra_pair_clearance`` threshold (see Issue #2560 /
    Epic #2556 Phase 1D).

    Detection currently uses the suffix-inference matcher in
    ``router/diffpair`` -- this matches what the autorouter sees when no
    explicit declarations or KiCad-group sources are present, and the
    refusal patterns (``USB_CC1``/``USB_CC2``, ``SBU1``/``SBU2``) are
    correctly excluded.  When the upstream rule consumer (the autorouter
    in #2559) gains the explicit-declaration plumbing, this helper can be
    extended to honor those sources too without a public API change.
    """
    from kicad_tools.router.diffpair import detect_differential_pairs

    pairs: set[tuple[int, int]] = set()
    net_names = {net.number: net.name for net in pcb.nets.values()}
    for diff_pair in detect_differential_pairs(net_names):
        p_id = diff_pair.positive.net_id
        n_id = diff_pair.negative.net_id
        if p_id == 0 or n_id == 0:
            continue
        key = (p_id, n_id) if p_id <= n_id else (n_id, p_id)
        pairs.add(key)
    return pairs


def _calculate_clearance(elem1: CopperElement, elem2: CopperElement) -> tuple[float, float, float]:
    """Calculate the clearance between two copper elements.

    Returns:
        Tuple of (clearance_mm, location_x, location_y)
        The location is the midpoint between the closest points.
    """
    t1, t2 = elem1.element_type, elem2.element_type

    if t1 == "segment" and t2 == "segment":
        return _segment_segment_clearance(elem1, elem2)
    elif t1 == "segment" and t2 in ("pad", "via"):
        return _segment_circle_clearance(elem1, elem2)
    elif t1 in ("pad", "via") and t2 == "segment":
        clearance, x, y = _segment_circle_clearance(elem2, elem1)
        return clearance, x, y
    else:
        # Both are pad or via (circles)
        return _circle_circle_clearance(elem1, elem2)


def _segment_segment_clearance(
    seg1: CopperElement, seg2: CopperElement
) -> tuple[float, float, float]:
    """Calculate clearance between two trace segments."""
    x1, y1, x2, y2, w1 = seg1.geometry
    x3, y3, x4, y4, w2 = seg2.geometry

    # Distance between segment centerlines
    center_dist = _segment_to_segment_distance(x1, y1, x2, y2, x3, y3, x4, y4)

    # Subtract half-widths to get edge-to-edge clearance
    clearance = center_dist - (w1 / 2) - (w2 / 2)

    # Location is midpoint of the two segments' midpoints
    loc_x = (x1 + x2 + x3 + x4) / 4
    loc_y = (y1 + y2 + y3 + y4) / 4

    return clearance, loc_x, loc_y


def _segment_circle_clearance(
    seg: CopperElement, circle: CopperElement
) -> tuple[float, float, float]:
    """Calculate clearance between a segment and a circle (pad/via)."""
    x1, y1, x2, y2, seg_width = seg.geometry
    cx, cy, w, h = circle.geometry

    # Use max dimension as radius for conservative check
    radius = max(w, h) / 2

    # Distance from circle center to segment centerline
    center_dist = _point_to_segment_distance(cx, cy, x1, y1, x2, y2)

    # Subtract half-width and radius for edge-to-edge clearance
    clearance = center_dist - (seg_width / 2) - radius

    # Location is at the circle center
    return clearance, cx, cy


def _circle_circle_clearance(c1: CopperElement, c2: CopperElement) -> tuple[float, float, float]:
    """Calculate clearance between two pads/vias.

    For vias (circular), uses circle-to-circle distance.
    For rectangular pads, uses axis-aligned rectangle-to-rectangle distance.
    For mixed (rect pad to via), uses rect-to-circle distance.
    """
    x1, y1, w1, h1 = c1.geometry
    x2, y2, w2, h2 = c2.geometry

    # Check if elements are circular (vias or square pads)
    is_circular_1 = c1.element_type == "via" or abs(w1 - h1) < 0.001
    is_circular_2 = c2.element_type == "via" or abs(w2 - h2) < 0.001

    if is_circular_1 and is_circular_2:
        # Both circular: use circle-to-circle distance
        r1 = w1 / 2
        r2 = w2 / 2
        center_dist = math.sqrt((x2 - x1) ** 2 + (y2 - y1) ** 2)
        clearance = center_dist - r1 - r2
    elif not is_circular_1 and not is_circular_2:
        # Both rectangular: use rectangle-to-rectangle distance
        clearance = _rect_rect_clearance(x1, y1, w1, h1, x2, y2, w2, h2)
    else:
        # Mixed: rectangle to circle
        if is_circular_1:
            # c1 is circle, c2 is rect
            clearance = _rect_circle_clearance(x2, y2, w2, h2, x1, y1, w1 / 2)
        else:
            # c1 is rect, c2 is circle
            clearance = _rect_circle_clearance(x1, y1, w1, h1, x2, y2, w2 / 2)

    # Location is midpoint between centers
    loc_x = (x1 + x2) / 2
    loc_y = (y1 + y2) / 2

    return clearance, loc_x, loc_y


def _rect_rect_clearance(
    cx1: float,
    cy1: float,
    w1: float,
    h1: float,
    cx2: float,
    cy2: float,
    w2: float,
    h2: float,
) -> float:
    """Calculate clearance between two axis-aligned rectangles.

    Args:
        cx1, cy1: Center of rectangle 1
        w1, h1: Width and height of rectangle 1
        cx2, cy2: Center of rectangle 2
        w2, h2: Width and height of rectangle 2

    Returns:
        Edge-to-edge clearance (negative if overlapping)
    """
    # Gap in each axis (distance between edges)
    gap_x = abs(cx2 - cx1) - (w1 + w2) / 2
    gap_y = abs(cy2 - cy1) - (h1 + h2) / 2

    if gap_x >= 0 and gap_y >= 0:
        # Rectangles separated in both axes - corner-to-corner distance
        return math.sqrt(gap_x * gap_x + gap_y * gap_y)
    elif gap_x >= 0:
        # Overlap in Y, separated in X - edge-to-edge in X direction
        return gap_x
    elif gap_y >= 0:
        # Overlap in X, separated in Y - edge-to-edge in Y direction
        return gap_y
    else:
        # Overlap in both axes - return least negative (closest to separating)
        return max(gap_x, gap_y)


def _rect_circle_clearance(
    cx: float,
    cy: float,
    w: float,
    h: float,
    circle_x: float,
    circle_y: float,
    radius: float,
) -> float:
    """Calculate clearance between an axis-aligned rectangle and a circle.

    Args:
        cx, cy: Center of rectangle
        w, h: Width and height of rectangle
        circle_x, circle_y: Center of circle
        radius: Radius of circle

    Returns:
        Edge-to-edge clearance (negative if overlapping)
    """
    # Find the closest point on the rectangle to the circle center
    half_w = w / 2
    half_h = h / 2

    # Clamp circle center to rectangle bounds
    closest_x = max(cx - half_w, min(circle_x, cx + half_w))
    closest_y = max(cy - half_h, min(circle_y, cy + half_h))

    # Distance from closest point to circle center
    dist = math.sqrt((circle_x - closest_x) ** 2 + (circle_y - closest_y) ** 2)

    # Clearance is distance minus radius
    return dist - radius


class ClearanceRule(DRCRule):
    """Check minimum clearance between copper elements.

    Validates that spacing between traces, pads, and vias on the same
    layer but different nets meets the manufacturer's minimum clearance
    requirement.

    Differential-pair within-pair edges (segment-to-segment edges where
    both segments belong to the P/N halves of a detected diff pair) are
    skipped here -- they are validated by
    :class:`~kicad_tools.validate.rules.diffpair_clearance_intra.DiffPairClearanceIntraRule`
    against the per-class ``intra_pair_clearance`` (which is allowed to
    be tighter than the manufacturer's ``min_clearance_mm``).  Without
    this skip users would see double violations on every diff-pair edge
    that's tighter than inter-pair clearance, making the new rule
    actively unhelpful.  See Issue #2560 / Epic #2556 Phase 1D.

    The skip is segment-to-segment only -- pad and via clearances are
    enforced inter-pair regardless of diff-pair membership, matching the
    scope of the new rule (segments only).
    """

    rule_id = "clearance"
    name = "Copper Clearance"
    description = "Validates minimum spacing between copper elements"

    def check(
        self,
        pcb: PCB,
        design_rules: DesignRules,
    ) -> DRCResults:
        """Check clearance rules on all copper layers.

        Args:
            pcb: The PCB to check
            design_rules: Design rules from the manufacturer profile

        Returns:
            DRCResults containing any clearance violations found
        """
        results = DRCResults()
        min_clearance = design_rules.min_clearance_mm

        # Build the diff-pair set once for the whole board (suffix
        # inference is cheap; we scan the net table once).  Used to
        # skip same-pair segment-segment edges that the new
        # DiffPairClearanceIntraRule validates against a tighter
        # per-class threshold.  See Issue #2560.
        diff_pair_set = _build_diff_pair_set(pcb)

        # Process each copper layer
        for layer in pcb.copper_layers:
            layer_name = layer.name
            violations = self._check_layer(pcb, layer_name, min_clearance, diff_pair_set)
            for v in violations:
                results.add(v)

        # Count rules checked (one per layer)
        results.rules_checked = len(pcb.copper_layers)

        return results

    def _check_layer(
        self,
        pcb: PCB,
        layer_name: str,
        min_clearance: float,
        diff_pair_set: set[tuple[int, int]] | None = None,
    ) -> list[DRCViolation]:
        """Check clearance on a single copper layer.

        Args:
            pcb: The PCB being checked.
            layer_name: Layer to scan.
            min_clearance: Manufacturer's minimum inter-pair clearance.
            diff_pair_set: Optional set of ``(min_net_id, max_net_id)``
                tuples identifying detected differential pairs.  When
                provided, segment-to-segment edges between the two
                halves of a pair are skipped (delegated to
                ``DiffPairClearanceIntraRule``).  See Issue #2560.
        """
        violations: list[DRCViolation] = []
        if diff_pair_set is None:
            diff_pair_set = set()

        # Collect all copper elements on this layer
        elements = self._collect_elements(pcb, layer_name)

        # Check all pairs (O(n²) - acceptable for typical board sizes)
        for i, elem1 in enumerate(elements):
            for elem2 in elements[i + 1 :]:
                # Skip if same net (same net elements can touch)
                if elem1.net_number == elem2.net_number:
                    continue

                # Skip net 0 (unconnected) elements
                if elem1.net_number == 0 or elem2.net_number == 0:
                    continue

                # Skip same-pair segment-to-segment edges -- they are
                # validated by DiffPairClearanceIntraRule against a
                # tighter per-class threshold.  Pad/via combinations
                # remain in scope here (issue #2560 scopes the new
                # rule to segments only).
                if (
                    elem1.element_type == "segment"
                    and elem2.element_type == "segment"
                    and diff_pair_set
                ):
                    key = (
                        (elem1.net_number, elem2.net_number)
                        if elem1.net_number <= elem2.net_number
                        else (elem2.net_number, elem1.net_number)
                    )
                    if key in diff_pair_set:
                        continue

                # Skip segment/via pairs where the segment endpoint
                # coincides with the via center.  The router's in-pad
                # escape places segment endpoints exactly at via centers
                # (router invariant); when a neighboring net's escape
                # segment terminates at the same coordinates as a
                # cross-net in-pad via, the geometric distance is zero
                # and the rule reports a spurious "negative clearance"
                # violation at the via center.  The Via schema has no
                # ``in_pad`` flag (dropped at serialization), so the
                # detection is geometric.  See Issue #2706 and the
                # ``_COLOCATION_EPSILON_MM`` constant above.
                if {elem1.element_type, elem2.element_type} == {"segment", "via"}:
                    seg = elem1 if elem1.element_type == "segment" else elem2
                    via = elem2 if elem1.element_type == "segment" else elem1
                    sx1, sy1, sx2, sy2, _ = seg.geometry
                    vx, vy, _, _ = via.geometry
                    if (
                        math.hypot(sx1 - vx, sy1 - vy) < _COLOCATION_EPSILON_MM
                        or math.hypot(sx2 - vx, sy2 - vy) < _COLOCATION_EPSILON_MM
                    ):
                        continue

                # Calculate clearance
                clearance, loc_x, loc_y = _calculate_clearance(elem1, elem2)

                # Check against minimum
                if clearance + DRC_TOLERANCE < min_clearance:
                    violation = self._create_violation(
                        elem1, elem2, clearance, min_clearance, layer_name, loc_x, loc_y
                    )
                    violations.append(violation)

        return violations

    def _collect_elements(self, pcb: PCB, layer_name: str) -> list[CopperElement]:
        """Collect all copper elements on a layer."""
        elements: list[CopperElement] = []

        # Add segments on this layer
        for seg in pcb.segments_on_layer(layer_name):
            elements.append(CopperElement.from_segment(seg))

        # Add pads that are on this layer
        for fp in pcb.footprints:
            for pad in fp.pads:
                if layer_name in pad.layers or "*.Cu" in pad.layers:
                    elements.append(CopperElement.from_pad(pad, fp))

        # Add vias (they span layers, so include if layer is in via's layer list)
        for via in pcb.vias:
            if layer_name in via.layers:
                elements.append(CopperElement.from_via(via))

        return elements

    def _create_violation(
        self,
        elem1: CopperElement,
        elem2: CopperElement,
        actual: float,
        required: float,
        layer: str,
        loc_x: float,
        loc_y: float,
    ) -> DRCViolation:
        """Create a DRC violation for a clearance issue."""
        # Determine rule ID suffix based on element types
        types = sorted([elem1.element_type, elem2.element_type])
        rule_suffix = f"{types[0]}_{types[1]}"

        return DRCViolation(
            rule_id=f"clearance_{rule_suffix}",
            severity="error",
            message=(
                f"{elem1.element_type.title()} to {elem2.element_type} clearance "
                f"{actual:.3f}mm < minimum {required:.3f}mm"
            ),
            location=(round(loc_x, 3), round(loc_y, 3)),
            layer=layer,
            actual_value=round(actual, 4),
            required_value=required,
            items=(elem1.reference, elem2.reference),
            nets=(elem1.net_name, elem2.net_name),
        )
