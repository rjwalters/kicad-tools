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
    rotate_pad_offset as _rotate_pad_offset,
)
from kicad_tools.core.geometry import (
    segment_to_segment_distance as _segment_to_segment_distance,
)
from kicad_tools.core.geometry import (
    segments_intersect as _segments_intersect,
)
from kicad_tools.core.layers import via_spans_layer as _via_spans_layer

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
    # For vias: the layer names explicitly declared on the via (the
    # endpoint pair, e.g. ("F.Cu", "B.Cu")).  A through-via barrel is
    # physical copper on every layer it SPANS, so the element is
    # collected on inner layers too (issue #3487) -- but via-via and
    # pad-via pairs are only evaluated on explicitly-declared layers to
    # avoid re-reporting the same geometric pair on every spanned layer
    # (the endpoint-layer scan already covers them).
    explicit_layers: tuple[str, ...] = ()

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
            explicit_layers=tuple(via.layers),
        )

    def on_layer(self, layer: str) -> bool:
        """Check if this element is on the specified layer."""
        if self.layer == "*":
            return True  # Pads and vias span multiple layers
        return self.layer == layer


def _transform_pad_position(pad: Pad, footprint: Footprint) -> tuple[float, float]:
    """Transform pad position from footprint-local to board coordinates.

    Uses KiCad's negated-angle pad rotation convention (see
    :func:`kicad_tools.core.geometry.rotate_pad_offset`).
    """
    # Rotate pad position around footprint origin (KiCad convention)
    local_x, local_y = pad.position
    rotated_x, rotated_y = _rotate_pad_offset(local_x, local_y, footprint.rotation)

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
    """Calculate clearance between a segment and a pad/via.

    For circular obstacles (vias and square pads) the distance is
    ``centerline_distance - seg_half_width - radius`` which models the
    obstacle as a disc.

    For rectangular pads, the previous "treat as a disc of radius
    ``max(w, h) / 2``" approach was overly conservative -- a 0.5 x 1.2 mm
    USB pad became a 1.2 mm-diameter disc, manufacturing 0.35 mm of
    phantom inflation along the pad's narrow axis and flagging
    ``clearance_pad_segment`` violations against traces that actually
    cleared the rectangle by hundreds of microns.  Issue #2781 traced
    the post-route DRC over-emission directly to this approximation
    (commit 6ec0344c fixed the analogous bug for pad-to-pad clearance
    but did not visit segment-to-pad).  Use axis-aligned rectangle
    geometry for rectangular pads, mirroring ``_rect_circle_clearance``.
    """
    x1, y1, x2, y2, seg_width = seg.geometry
    cx, cy, w, h = circle.geometry
    seg_half = seg_width / 2

    # Vias are always circular; square pads (w == h within a micron) are
    # equally well-modeled as discs and the circle path is simpler/faster.
    is_circular = circle.element_type == "via" or abs(w - h) < 0.001

    if is_circular:
        radius = max(w, h) / 2
        center_dist = _point_to_segment_distance(cx, cy, x1, y1, x2, y2)
        clearance = center_dist - seg_half - radius
    else:
        # Rectangular pad: compute true segment-to-rectangle distance.
        # ``_rect_segment_centerline_distance`` returns a signed
        # centerline distance (negative when the segment overlaps the
        # rectangle, mirroring ``_rect_circle_clearance``'s sign
        # convention for the rect-vs-disc case).
        center_dist = _rect_segment_centerline_distance(cx, cy, w, h, x1, y1, x2, y2)
        clearance = center_dist - seg_half

    # Location is at the pad/via center (sufficient for repair tooling
    # and human readability; the previous behaviour also reported the
    # pad center).
    return clearance, cx, cy


def _rect_segment_centerline_distance(
    cx: float,
    cy: float,
    w: float,
    h: float,
    x1: float,
    y1: float,
    x2: float,
    y2: float,
) -> float:
    """Signed centerline distance between an axis-aligned rectangle and a segment.

    Returns the minimum distance from the segment's centerline to the
    rectangle.  The sign convention matches ``_rect_circle_clearance``:

    - **Positive** -- segment is entirely outside the rectangle.
    - **Zero**     -- segment touches/crosses the rectangle boundary.
    - **Negative** -- segment centerline lies inside the rectangle; the
      magnitude is the smallest perpendicular distance from a segment
      endpoint to the nearest rectangle edge (i.e. how far the segment
      would need to move to escape the rectangle).

    The negative branch is what allows the DRC checker to flag
    "trace runs through pad" defects with a meaningful depth indicator
    (e.g. ``actual_value = -1.355mm`` on board 05's U1.5 TO-263 GND tab
    -- a real router defect that the old conservative-disc check
    correctly flagged but with a distorted magnitude).

    Args:
        cx, cy: Center of rectangle.
        w, h: Width and height of rectangle.
        x1, y1: Segment start.
        x2, y2: Segment end.

    Returns:
        Signed centerline-to-rectangle distance in millimetres.
    """
    half_w = w / 2
    half_h = h / 2
    left = cx - half_w
    right = cx + half_w
    bot = cy - half_h
    top = cy + half_h

    def _inside(px: float, py: float) -> bool:
        return left <= px <= right and bot <= py <= top

    p1_in = _inside(x1, y1)
    p2_in = _inside(x2, y2)

    if p1_in and p2_in:
        # Whole centerline inside rect -- return negative depth equal to
        # the deepest point's signed distance to the nearest rect edge.
        #
        # For an axis-aligned rectangle the depth function along a
        # straight segment is piecewise linear (the min of four linear
        # functions, one per rect edge), so its maximum is attained at
        # an endpoint or at one of the kinks where two adjacent edges
        # tie.  The deepest kinks lie on the rectangle's two interior
        # diagonals from the centre, which a horizontally or
        # vertically axis-aligned segment crosses at predictable
        # parametric ``t`` values.  Rather than enumerate cases, we
        # sample the segment at 33 uniformly spaced points (including
        # both endpoints) and return the most negative depth -- well
        # over-sampled for DRC reporting purposes, and ``O(1)``.
        def _signed_depth(px: float, py: float) -> float:
            gap_x = max(px - right, left - px)
            gap_y = max(py - top, bot - py)
            # gap_x <= 0 and gap_y <= 0 when (px, py) is inside the rect.
            return max(gap_x, gap_y)

        # Deepest = most negative signed_depth along the segment.
        deepest = min(_signed_depth(x1, y1), _signed_depth(x2, y2))
        steps = 32
        dx = x2 - x1
        dy = y2 - y1
        for i in range(1, steps):
            t = i / steps
            d = _signed_depth(x1 + t * dx, y1 + t * dy)
            if d < deepest:
                deepest = d
        return deepest

    if p1_in != p2_in:
        # Endpoint straddles the boundary -- centerline crosses an edge.
        return 0.0

    # Both endpoints outside.  Check whether the segment crosses any of
    # the four rectangle edges; if so the centerline touches the
    # boundary (distance 0).
    rect_edges = (
        (left, bot, right, bot),
        (right, bot, right, top),
        (right, top, left, top),
        (left, top, left, bot),
    )
    for ex1, ey1, ex2, ey2 in rect_edges:
        if _segments_intersect(x1, y1, x2, y2, ex1, ey1, ex2, ey2):
            return 0.0

    # No crossing -- the closest approach is either an endpoint of the
    # segment to the rectangle or a corner of the rectangle to the
    # segment.  Both are non-negative.
    def _point_to_rect(px: float, py: float) -> float:
        closest_x = max(left, min(px, right))
        closest_y = max(bot, min(py, top))
        return math.sqrt((px - closest_x) ** 2 + (py - closest_y) ** 2)

    candidates = [
        _point_to_rect(x1, y1),
        _point_to_rect(x2, y2),
    ]
    for corner_x, corner_y in (
        (left, bot),
        (right, bot),
        (right, top),
        (left, top),
    ):
        candidates.append(_point_to_segment_distance(corner_x, corner_y, x1, y1, x2, y2))

    return min(candidates)


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

                # Vias are collected on every copper layer their barrel
                # spans (issue #3487) so SEGMENT-via conflicts on inner
                # layers are caught.  Pairs of layer-spanning elements
                # (via-via, pad-via) have layer-independent geometry and
                # are already evaluated on the via's explicitly-declared
                # endpoint layers -- re-running them on each spanned
                # inner layer would only duplicate the same report, so
                # restrict non-segment pairs to declared layers.
                if elem1.element_type != "segment" and elem2.element_type != "segment":
                    if (
                        elem1.element_type == "via"
                        and elem1.explicit_layers
                        and layer_name not in elem1.explicit_layers
                    ) or (
                        elem2.element_type == "via"
                        and elem2.explicit_layers
                        and layer_name not in elem2.explicit_layers
                    ):
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

        # Add vias whose barrel passes through this layer.  KiCad
        # declares only the endpoint pair on the via (a through-via on a
        # 4-layer board reads ``(layers "F.Cu" "B.Cu")``), but the barrel
        # is physical copper on EVERY layer it spans -- including inner
        # layers that never appear in ``via.layers``.  The previous
        # ``layer_name in via.layers`` test silently skipped barrel-vs-
        # inner-layer-segment conflicts (issue #3487: three real shorts
        # on the softstart board were invisible to ``kct check``).
        for via in pcb.vias:
            if _via_spans_layer(via.layers, layer_name):
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


@dataclass
class _ZoneFill:
    """One filled polygon of a zone, resolved to a net and a layer."""

    net_number: int
    net_name: str
    layer: str
    polygon: object  # shapely (Multi)Polygon


def _repair_fill_polygon(poly):  # type: ignore[no-untyped-def]
    """Repair an invalid fill ring, preserving every copper lobe.

    Stale fills can carry self-touching outlines (KiCad traces knockout
    holes through the exterior ring) or, in the worst case, genuinely
    self-intersecting bowtie rings.  ``buffer(0)`` re-nodes the former
    losslessly but silently *drops* the negatively-wound lobe of a
    bowtie -- and dropped copper is exactly the copper this rule exists
    to check, so a real short under that lobe would be masked (Issue
    #3560).  ``shapely.make_valid`` instead keeps all lobes as a
    MultiPolygon.

    ``make_valid`` may return a GeometryCollection when part of the
    outline collapses to zero-area linework; only the polygonal
    components are copper, so extract those (an empty Polygon is
    returned if nothing polygonal survives, which callers skip via
    ``is_empty``).
    """
    from shapely import make_valid
    from shapely.geometry import GeometryCollection, MultiPolygon, Polygon

    repaired = make_valid(poly)
    if isinstance(repaired, (Polygon, MultiPolygon)):
        return repaired
    polys: list[Polygon] = []
    if isinstance(repaired, GeometryCollection):
        for geom in repaired.geoms:
            if isinstance(geom, Polygon):
                polys.append(geom)
            elif isinstance(geom, MultiPolygon):
                polys.extend(geom.geoms)
    # Pure linework / points (fully degenerate outline) -> no copper.
    if not polys:
        return Polygon()
    if len(polys) == 1:
        return polys[0]
    return MultiPolygon(polys)


def _collect_zone_fills(pcb: PCB) -> dict[str, list[_ZoneFill]]:
    """Group every zone's filled polygons by copper layer, resolving nets.

    Shared by :class:`SegmentZoneClearanceRule` and
    :class:`ViaZoneClearanceRule` so segment-vs-fill and via/pad-vs-fill
    clearance both consult the *same* committed ``filled_polygon`` copper
    (this repo's source of truth -- issues #3482/#3523/#3527).

    Zones whose net cannot be resolved to a nonzero net number are
    skipped (keepout/rule areas have no fills anyway; a zone with no net
    has no foreign-net relationship to enforce).  Invalid fill rings are
    repaired with :func:`_repair_fill_polygon` (``make_valid``) so every
    copper lobe survives -- ``buffer(0)`` silently drops bowtie lobes and
    a real short under a dropped lobe would be masked (Issue #3560).

    Args:
        pcb: The PCB whose zones to collect.

    Returns:
        Mapping of copper-layer name -> list of :class:`_ZoneFill`, each
        carrying the resolved net number/name and a shapely (Multi)Polygon.
    """
    from shapely.geometry import Polygon

    name_to_number = {net.name: net.number for net in pcb.nets.values() if net.name}
    number_to_name = {net.number: net.name for net in pcb.nets.values()}

    fills_by_layer: dict[str, list[_ZoneFill]] = {}
    for zone in pcb.zones:
        net_number = zone.net_number
        if net_number == 0 and zone.net_name:
            # KiCad 9 name-only ``(net "X")`` format -- resolve by name.
            net_number = name_to_number.get(zone.net_name, 0)
        if net_number == 0:
            continue
        net_name = zone.net_name or number_to_name.get(net_number, "")
        for i, points in enumerate(zone.filled_polygons):
            if len(points) < 3:
                continue
            poly = Polygon(points)
            if not poly.is_valid:
                # make_valid keeps every copper lobe (buffer(0)
                # drops bowtie lobes -- see _repair_fill_polygon).
                poly = _repair_fill_polygon(poly)
            if poly.is_empty:
                continue
            layer = zone.filled_polygon_layer(i)
            if not layer:
                continue
            fills_by_layer.setdefault(layer, []).append(
                _ZoneFill(
                    net_number=net_number,
                    net_name=net_name,
                    layer=layer,
                    polygon=poly,
                )
            )
    return fills_by_layer


class SegmentZoneClearanceRule(DRCRule):
    """Check track segments against foreign-net zone fill copper.

    This repo's convention (issues #3482/#3523) treats committed
    ``filled_polygon`` data as the copper source of truth for
    connectivity analysis, so it must also be the source of truth for
    clearance/short checks.  Before this rule, ``kct check`` validated
    segment-vs-segment, segment-vs-pad, and segment-vs-via spacing but
    never compared a segment to the *fill* copper of another net's zone
    -- a trace routed straight through a stale foreign fill (a hard
    manufacturing short) sailed through every gate.  PR #3526's judge
    found exactly that on board 05: a PWR_LED segment crossing ~3.1 mm
    of +3V3 fill with zero blocking violations reported.  See Issue
    #3527.

    Two violation flavours, both ``rule_id="clearance_segment_zone"``
    and severity ``error`` (blocking):

    - **Short**: the segment's copper overlaps the fill polygon
      (edge-to-edge clearance < 0).  ``actual_value`` is the negative
      overlap depth.
    - **Clearance**: the copper gap is positive but below the
      manufacturer's ``min_clearance_mm``.

    Same-net segment/fill pairs are skipped (a trace inside its own
    pour is legal and common).  Segments and zones on net 0 are
    skipped, matching :class:`ClearanceRule` (no net identity -> no
    foreign-net determination).

    Performance: fill polygons are loaded into one shapely ``STRtree``
    per layer; each segment only visits fills whose bounding box
    intersects its inflated bbox, and all distance math is exact
    shapely C geometry (no centerline sampling), so the rule is safe
    to run inside CI gates on large boards.
    """

    rule_id = "clearance_segment_zone"
    name = "Segment to Zone Fill Clearance"
    description = "Validates track segments against foreign-net zone fill copper"

    def check(
        self,
        pcb: PCB,
        design_rules: DesignRules,
    ) -> DRCResults:
        """Check all segments against foreign-net zone fills.

        Args:
            pcb: The PCB to check
            design_rules: Design rules from the manufacturer profile

        Returns:
            DRCResults containing segment-vs-zone-fill shorts and
            clearance violations
        """
        results = DRCResults()
        results.rules_checked = 1

        fills_by_layer = self._collect_fills(pcb)
        if not fills_by_layer:
            return results

        import shapely
        from shapely import STRtree
        from shapely.geometry import LineString

        min_clearance = design_rules.min_clearance_mm

        for layer, fills in fills_by_layer.items():
            tree = STRtree([f.polygon for f in fills])
            for seg in pcb.segments_on_layer(layer):
                if seg.net_number == 0:
                    continue
                half_w = seg.width / 2.0
                line = LineString([seg.start, seg.end])
                # Inflate the segment bbox by everything that could
                # matter: its own half-width plus the required gap.
                margin = half_w + min_clearance
                minx, miny, maxx, maxy = line.bounds
                query_box = shapely.box(minx - margin, miny - margin, maxx + margin, maxy + margin)
                for idx in tree.query(query_box):
                    fill = fills[int(idx)]
                    if fill.net_number == seg.net_number:
                        continue
                    violation = self._check_pair(seg, line, half_w, fill, min_clearance, layer)
                    if violation is not None:
                        results.add(violation)

        return results

    def _collect_fills(self, pcb: PCB) -> dict[str, list[_ZoneFill]]:
        """Group zone fill polygons by copper layer, resolving zone nets.

        Thin wrapper over the module-level :func:`_collect_zone_fills`
        helper, shared with :class:`ViaZoneClearanceRule` so both rules
        consume the same fill geometry (same net-resolution and
        invalid-ring repair semantics).
        """
        return _collect_zone_fills(pcb)

    def _check_pair(
        self,
        seg: Segment,
        line: object,
        half_w: float,
        fill: _ZoneFill,
        min_clearance: float,
        layer: str,
    ) -> DRCViolation | None:
        """Check one segment against one foreign fill polygon."""
        from shapely.ops import nearest_points

        poly = fill.polygon
        centerline_dist = line.distance(poly)  # type: ignore[attr-defined]
        seg_net = seg.net_name if seg.net_number != 0 else ""
        seg_ref = f"Trace-{seg.uuid[:8]}" if seg.uuid else "Trace"
        zone_ref = f"ZoneFill-{fill.net_name}" if fill.net_name else "ZoneFill"

        if centerline_dist == 0.0:
            # Centerline touches or enters the fill copper: hard short.
            # Depth = how far inside the fill the overlap's representative
            # point sits, plus the trace half-width.
            intersection = line.intersection(poly)  # type: ignore[attr-defined]
            if intersection.is_empty:
                # Grazing contact with no overlap length -- still a
                # zero-clearance touch.
                rep = nearest_points(line, poly)[0]
                depth = 0.0
            else:
                rep = intersection.representative_point()
                depth = poly.boundary.distance(rep)  # type: ignore[attr-defined]
            clearance = -(depth + half_w)
            return DRCViolation(
                rule_id=self.rule_id,
                severity="error",
                message=(
                    f"Short: segment on net '{seg_net}' overlaps zone fill of net "
                    f"'{fill.net_name}' on {layer} (overlap depth "
                    f"{depth + half_w:.3f}mm)"
                ),
                location=(round(rep.x, 3), round(rep.y, 3)),
                layer=layer,
                actual_value=round(clearance, 4),
                required_value=min_clearance,
                items=(seg_ref, zone_ref),
                nets=(seg_net, fill.net_name),
            )

        clearance = centerline_dist - half_w
        if clearance + DRC_TOLERANCE >= min_clearance:
            return None

        p_line, p_poly = nearest_points(line, poly)
        loc_x = (p_line.x + p_poly.x) / 2.0
        loc_y = (p_line.y + p_poly.y) / 2.0
        if clearance < 0:
            # Centerline outside the fill but the trace copper (width)
            # overlaps the fill edge: still a short.
            message = (
                f"Short: segment on net '{seg_net}' copper overlaps zone fill of "
                f"net '{fill.net_name}' on {layer} by {-clearance:.3f}mm"
            )
        else:
            message = (
                f"Segment to zone fill clearance {clearance:.3f}mm < minimum "
                f"{min_clearance:.3f}mm (net '{seg_net}' vs zone net "
                f"'{fill.net_name}')"
            )
        return DRCViolation(
            rule_id=self.rule_id,
            severity="error",
            message=message,
            location=(round(loc_x, 3), round(loc_y, 3)),
            layer=layer,
            actual_value=round(clearance, 4),
            required_value=min_clearance,
            items=(seg_ref, zone_ref),
            nets=(seg_net, fill.net_name),
        )


class ViaZoneClearanceRule(DRCRule):
    """Check vias and pads against foreign-net zone fill copper.

    The sibling of :class:`SegmentZoneClearanceRule`.  That rule closed
    the segment-vs-fill gap (Issue #3527), but vias and pads were still
    never compared to the committed ``filled_polygon`` copper of another
    net's zone -- so a via dropped sub-clearance to (or straight through)
    a stale foreign pour shipped uncaught: KiCad's own DRC flags the
    overlap, but every project gate stayed green because the strict CI
    gate runs ``kct check`` and ``kct check`` had no via/pad-vs-fill term.
    This is the exact failure class that motivated Issue #3556 (a surgical
    trace move on board 06 left copper 0.0347 mm from an un-refilled GND
    pour) -- but for the round copper of a via barrel rather than a track.

    A through-via barrel is physical copper on **every** layer it spans,
    so each via is checked against the fills on all spanned copper layers
    (via :func:`kicad_tools.core.layers.via_spans_layer`), matching the
    all-layer treatment :class:`ClearanceRule` already applies to
    segment-vs-via pairs (Issue #3487).  Pads are checked on each copper
    layer they declare.

    Two violation flavours, both severity ``error`` (blocking):

    - **Short**: the via/pad copper overlaps the fill polygon
      (edge-to-edge clearance < 0).  ``actual_value`` is the negative
      overlap depth.
    - **Clearance**: the copper gap is positive but below the
      manufacturer's ``min_clearance_mm``.

    Same-net pairs are skipped (a via stitching its own pour is legal).
    Net-0 vias/pads/zones are skipped, matching the sibling rule.

    The rule_id is ``clearance_via_zone`` for vias and ``clearance_pad_zone``
    for pads, so the existing strict CI gate (which counts every blocking
    ``clearance_*`` violation ``kct check`` reports) picks them up with no
    allowlist surgery.
    """

    rule_id = "clearance_via_zone"
    name = "Via/Pad to Zone Fill Clearance"
    description = "Validates vias and pads against foreign-net zone fill copper"

    def check(
        self,
        pcb: PCB,
        design_rules: DesignRules,
    ) -> DRCResults:
        """Check all vias and pads against foreign-net zone fills.

        Args:
            pcb: The PCB to check
            design_rules: Design rules from the manufacturer profile

        Returns:
            DRCResults containing via/pad-vs-zone-fill shorts and
            clearance violations.
        """
        results = DRCResults()
        results.rules_checked = 1

        fills_by_layer = _collect_zone_fills(pcb)
        if not fills_by_layer:
            return results

        import shapely
        from shapely import STRtree
        from shapely.geometry import Point

        min_clearance = design_rules.min_clearance_mm

        for layer, fills in fills_by_layer.items():
            tree = STRtree([f.polygon for f in fills])

            # --- Vias: a barrel is copper on every layer it spans. ---
            for via in pcb.vias:
                if via.net_number == 0:
                    continue
                if not _via_spans_layer(via.layers, layer):
                    continue
                radius = via.size / 2.0
                if radius <= 0:
                    continue
                shape = Point(via.position).buffer(radius)
                ref = f"Via-{via.uuid[:8]}" if via.uuid else "Via"
                self._query_and_check(
                    tree,
                    fills,
                    shape,
                    radius,
                    via.net_number,
                    via.net_name,
                    ref,
                    min_clearance,
                    layer,
                    results,
                    shapely,
                )

            # --- Pads: checked on each copper layer they declare. ---
            for fp in pcb.footprints:
                for pad in fp.pads:
                    if pad.net_number == 0:
                        continue
                    if not _pad_on_layer(pad, layer):
                        continue
                    abs_x, abs_y = _transform_pad_position(pad, fp)
                    width, height = _transform_pad_dimensions(pad, fp)
                    if width <= 0 or height <= 0:
                        continue
                    shape = shapely.box(
                        abs_x - width / 2.0,
                        abs_y - height / 2.0,
                        abs_x + width / 2.0,
                        abs_y + height / 2.0,
                    )
                    ref = f"{fp.reference}-{pad.number}"
                    self._query_and_check(
                        tree,
                        fills,
                        shape,
                        0.0,  # the box already models the full pad copper
                        pad.net_number,
                        pad.net_name,
                        ref,
                        min_clearance,
                        layer,
                        results,
                        shapely,
                        rule_id="clearance_pad_zone",
                        kind="pad",
                    )

        return results

    def _query_and_check(
        self,
        tree: object,
        fills: list[_ZoneFill],
        shape: object,
        inflate: float,
        net_number: int,
        net_name: str,
        ref: str,
        min_clearance: float,
        layer: str,
        results: DRCResults,
        shapely_mod: object,
        rule_id: str = "clearance_via_zone",
        kind: str = "via",
    ) -> None:
        """Query the fill STRtree for one via/pad shape and emit violations."""
        minx, miny, maxx, maxy = shape.bounds  # type: ignore[attr-defined]
        margin = inflate + min_clearance
        query_box = shapely_mod.box(  # type: ignore[attr-defined]
            minx - margin, miny - margin, maxx + margin, maxy + margin
        )
        for idx in tree.query(query_box):  # type: ignore[attr-defined]
            fill = fills[int(idx)]
            if fill.net_number == net_number:
                continue
            violation = self._check_shape(
                shape, net_number, net_name, ref, fill, min_clearance, layer, rule_id, kind
            )
            if violation is not None:
                results.add(violation)

    def _check_shape(
        self,
        shape: object,
        net_number: int,
        net_name: str,
        ref: str,
        fill: _ZoneFill,
        min_clearance: float,
        layer: str,
        rule_id: str,
        kind: str,
    ) -> DRCViolation | None:
        """Check one via/pad copper shape against one foreign fill polygon."""
        poly = fill.polygon
        net_label = net_name if net_number != 0 else ""
        zone_ref = f"ZoneFill-{fill.net_name}" if fill.net_name else "ZoneFill"

        # The shape already models the full copper footprint (via barrel
        # disc / pad box), so a positive shape-to-polygon distance IS the
        # edge-to-edge clearance -- no half-width subtraction needed.
        dist = shape.distance(poly)  # type: ignore[attr-defined]

        if dist > 0:
            clearance = dist
            if clearance + DRC_TOLERANCE >= min_clearance:
                return None
            from shapely.ops import nearest_points

            p_shape, p_poly = nearest_points(shape, poly)
            loc_x = (p_shape.x + p_poly.x) / 2.0
            loc_y = (p_shape.y + p_poly.y) / 2.0
            message = (
                f"{kind.title()} to zone fill clearance {clearance:.3f}mm < "
                f"minimum {min_clearance:.3f}mm (net '{net_label}' vs zone net "
                f"'{fill.net_name}')"
            )
            return DRCViolation(
                rule_id=rule_id,
                severity="error",
                message=message,
                location=(round(loc_x, 3), round(loc_y, 3)),
                layer=layer,
                actual_value=round(clearance, 4),
                required_value=min_clearance,
                items=(ref, zone_ref),
                nets=(net_label, fill.net_name),
            )

        # dist == 0: the copper overlaps (or grazes) the fill -- hard short.
        intersection = shape.intersection(poly)  # type: ignore[attr-defined]
        if intersection.is_empty:
            from shapely.ops import nearest_points

            rep = nearest_points(shape, poly)[0]
            depth = 0.0
        else:
            rep = intersection.representative_point()
            depth = poly.boundary.distance(rep)  # type: ignore[attr-defined]
        clearance = -depth
        return DRCViolation(
            rule_id=rule_id,
            severity="error",
            message=(
                f"Short: {kind} on net '{net_label}' overlaps zone fill of net "
                f"'{fill.net_name}' on {layer} (overlap depth {depth:.3f}mm)"
            ),
            location=(round(rep.x, 3), round(rep.y, 3)),
            layer=layer,
            actual_value=round(clearance, 4),
            required_value=min_clearance,
            items=(ref, zone_ref),
            nets=(net_label, fill.net_name),
        )


def _pad_on_layer(pad: Pad, layer: str) -> bool:
    """Return True if a pad's copper exists on ``layer``.

    Pads declare layers like ``["F.Cu", "F.Mask"]`` (SMD) or
    ``["*.Cu", "*.Mask"]`` (through-hole).  The ``*.Cu`` wildcard means
    every copper layer, so a through-hole pad is copper on ``layer``.
    """
    pad_layers = pad.layers
    if "*.Cu" in pad_layers:
        return True
    return layer in pad_layers
