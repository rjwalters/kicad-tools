"""Clearance rule implementation for DRC checks.

Validates minimum spacing between copper elements (traces, pads, vias)
on the same layer but different nets.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..violations import DRCResults, DRCViolation
from .base import DRCRule

if TYPE_CHECKING:
    from kicad_tools.manufacturers import DesignRules
    from kicad_tools.schema.pcb import PCB, Footprint, Pad, Segment, Via


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

    @classmethod
    def from_segment(cls, seg: Segment) -> CopperElement:
        """Create from a PCB segment."""
        return cls(
            element_type="segment",
            layer=seg.layer,
            net_number=seg.net_number,
            geometry=(seg.start[0], seg.start[1], seg.end[0], seg.end[1], seg.width),
            reference=f"Trace-{seg.uuid[:8]}" if seg.uuid else "Trace",
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


def _point_to_segment_distance(
    px: float, py: float, x1: float, y1: float, x2: float, y2: float
) -> float:
    """Calculate the distance from a point to a line segment."""
    # Vector from p1 to p2
    dx = x2 - x1
    dy = y2 - y1

    # Length squared of segment
    len_sq = dx * dx + dy * dy

    if len_sq == 0:
        # Segment is a point
        return math.sqrt((px - x1) ** 2 + (py - y1) ** 2)

    # Parameter t for the closest point on the line
    t = max(0, min(1, ((px - x1) * dx + (py - y1) * dy) / len_sq))

    # Closest point on segment
    closest_x = x1 + t * dx
    closest_y = y1 + t * dy

    # Distance from point to closest point
    return math.sqrt((px - closest_x) ** 2 + (py - closest_y) ** 2)


def _segment_to_segment_distance(
    x1: float, y1: float, x2: float, y2: float, x3: float, y3: float, x4: float, y4: float
) -> float:
    """Calculate minimum distance between two line segments."""
    # Check all four endpoint-to-segment distances
    d1 = _point_to_segment_distance(x1, y1, x3, y3, x4, y4)
    d2 = _point_to_segment_distance(x2, y2, x3, y3, x4, y4)
    d3 = _point_to_segment_distance(x3, y3, x1, y1, x2, y2)
    d4 = _point_to_segment_distance(x4, y4, x1, y1, x2, y2)

    return min(d1, d2, d3, d4)


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

        # Process each copper layer
        for layer in pcb.copper_layers:
            layer_name = layer.name
            violations = self._check_layer(pcb, layer_name, min_clearance)
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
    ) -> list[DRCViolation]:
        """Check clearance on a single copper layer."""
        violations: list[DRCViolation] = []

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

                # Calculate clearance
                clearance, loc_x, loc_y = _calculate_clearance(elem1, elem2)

                # Check against minimum
                if clearance < min_clearance:
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
        )
