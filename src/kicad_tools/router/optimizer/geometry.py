"""Geometry helper functions for trace optimization."""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ..primitives import Segment

if TYPE_CHECKING:
    from ...schema.pcb import PCB


def segment_direction(seg: Segment, tolerance: float = 1e-4) -> float:
    """Calculate the direction of a segment in degrees (0-360).

    0° is positive X (right), 90° is positive Y (up),
    180° is negative X (left), 270° is negative Y (down).

    Args:
        seg: The segment to analyze.
        tolerance: Tolerance for zero-length detection.

    Returns:
        Direction in degrees (0-360).
    """
    dx = seg.x2 - seg.x1
    dy = seg.y2 - seg.y1

    if abs(dx) < tolerance and abs(dy) < tolerance:
        return 0.0  # Zero-length segment

    angle = math.degrees(math.atan2(dy, dx))
    if angle < 0:
        angle += 360
    return angle


def is_connected(s1: Segment, s2: Segment, tolerance: float = 1e-4) -> bool:
    """Check if end of s1 connects to start of s2."""
    return abs(s1.x2 - s2.x1) < tolerance and abs(s1.y2 - s2.y1) < tolerance


def segments_touch(s1: Segment, s2: Segment, tolerance: float = 1e-4) -> bool:
    """Check if two segments share any endpoint (regardless of direction)."""
    # Check all four possible endpoint connections
    # s1.end -> s2.start
    if abs(s1.x2 - s2.x1) < tolerance and abs(s1.y2 - s2.y1) < tolerance:
        return True
    # s1.end -> s2.end
    if abs(s1.x2 - s2.x2) < tolerance and abs(s1.y2 - s2.y2) < tolerance:
        return True
    # s1.start -> s2.start
    if abs(s1.x1 - s2.x1) < tolerance and abs(s1.y1 - s2.y1) < tolerance:
        return True
    # s1.start -> s2.end
    if abs(s1.x1 - s2.x2) < tolerance and abs(s1.y1 - s2.y2) < tolerance:
        return True

    return False


def same_direction(s1: Segment, s2: Segment, tolerance: float = 1e-4) -> bool:
    """Check if two segments have the same direction."""
    dx1, dy1 = s1.x2 - s1.x1, s1.y2 - s1.y1
    dx2, dy2 = s2.x2 - s2.x1, s2.y2 - s2.y1

    # Handle zero-length segments
    len1 = math.sqrt(dx1 * dx1 + dy1 * dy1)
    len2 = math.sqrt(dx2 * dx2 + dy2 * dy2)

    if len1 < tolerance or len2 < tolerance:
        return True  # Zero-length segments are "same direction"

    # Normalize
    dx1, dy1 = dx1 / len1, dy1 / len1
    dx2, dy2 = dx2 / len2, dy2 / len2

    # Cross product should be ~0 for parallel
    cross = abs(dx1 * dy2 - dy1 * dx2)
    # Dot product should be positive (same direction, not opposite)
    dot = dx1 * dx2 + dy1 * dy2

    return cross < 0.01 and dot > 0


def angle_between(s1: Segment, s2: Segment, tolerance: float = 1e-4) -> float:
    """Calculate angle between two segments in degrees (0-180)."""
    dx1, dy1 = s1.x2 - s1.x1, s1.y2 - s1.y1
    dx2, dy2 = s2.x2 - s2.x1, s2.y2 - s2.y1

    len1 = math.sqrt(dx1 * dx1 + dy1 * dy1)
    len2 = math.sqrt(dx2 * dx2 + dy2 * dy2)

    if len1 < tolerance or len2 < tolerance:
        return 0

    # Dot product
    dot = dx1 * dx2 + dy1 * dy2
    cos_angle = dot / (len1 * len2)
    cos_angle = max(-1, min(1, cos_angle))  # Clamp for numerical stability

    return math.degrees(math.acos(cos_angle))


def is_90_degree_corner(s1: Segment, s2: Segment, tolerance: float = 1e-4) -> bool:
    """Check if two segments form a 90-degree corner."""
    angle = angle_between(s1, s2, tolerance)
    return 80 < angle < 100  # Allow some tolerance


def is_zigzag(s1: Segment, s2: Segment, s3: Segment, tolerance: float = 1e-4) -> bool:
    """Check if s2 is a zigzag (backtrack) between s1 and s3."""
    # Calculate angles
    angle12 = angle_between(s1, s2, tolerance)

    # Zigzag: s2 goes roughly opposite to s1, then s3 continues roughly same as s1
    # This means angle12 is close to 180 degrees
    return abs(angle12 - 180) < 30


def segment_length(seg: Segment) -> float:
    """Calculate the length of a segment."""
    dx = seg.x2 - seg.x1
    dy = seg.y2 - seg.y1
    return math.sqrt(dx * dx + dy * dy)


def shorten_segment_end(seg: Segment, amount: float, min_length: float = 0.05) -> Segment | None:
    """Shorten a segment from its end by the given amount."""
    dx = seg.x2 - seg.x1
    dy = seg.y2 - seg.y1
    length = math.sqrt(dx * dx + dy * dy)

    if length <= amount + min_length:
        return None  # Can't shorten enough

    # New end point
    ratio = (length - amount) / length
    new_x2 = seg.x1 + dx * ratio
    new_y2 = seg.y1 + dy * ratio

    return Segment(
        x1=seg.x1,
        y1=seg.y1,
        x2=new_x2,
        y2=new_y2,
        width=seg.width,
        layer=seg.layer,
        net=seg.net,
        net_name=seg.net_name,
    )


def shorten_segment_start(seg: Segment, amount: float, min_length: float = 0.05) -> Segment | None:
    """Shorten a segment from its start by the given amount."""
    dx = seg.x2 - seg.x1
    dy = seg.y2 - seg.y1
    length = math.sqrt(dx * dx + dy * dy)

    if length <= amount + min_length:
        return None  # Can't shorten enough

    # New start point
    ratio = amount / length
    new_x1 = seg.x1 + dx * ratio
    new_y1 = seg.y1 + dy * ratio

    return Segment(
        x1=new_x1,
        y1=new_y1,
        x2=seg.x2,
        y2=seg.y2,
        width=seg.width,
        layer=seg.layer,
        net=seg.net,
        net_name=seg.net_name,
    )


def perpendicular_direction(seg: Segment, tolerance: float = 1e-4) -> tuple[float, float]:
    """Return a unit vector perpendicular to a segment.

    The perpendicular is rotated 90 degrees counter-clockwise from the
    segment direction.  For a zero-length segment the result is (0, 0).

    Args:
        seg: The segment to compute the perpendicular for.
        tolerance: Tolerance for zero-length detection.

    Returns:
        (px, py) unit perpendicular vector.
    """
    dx = seg.x2 - seg.x1
    dy = seg.y2 - seg.y1
    length = math.sqrt(dx * dx + dy * dy)
    if length < tolerance:
        return (0.0, 0.0)
    # Rotate 90 degrees CCW: (dx, dy) -> (-dy, dx)
    return (-dy / length, dx / length)


def project_point_onto_line(
    px: float,
    py: float,
    lx1: float,
    ly1: float,
    lx2: float,
    ly2: float,
) -> tuple[float, float]:
    """Project a point onto an infinite line defined by two points.

    Args:
        px, py: The point to project.
        lx1, ly1: First point on the line.
        lx2, ly2: Second point on the line.

    Returns:
        (x, y) projected point on the line.
    """
    dx = lx2 - lx1
    dy = ly2 - ly1
    len_sq = dx * dx + dy * dy
    if len_sq < 1e-12:
        return (lx1, ly1)
    t = ((px - lx1) * dx + (py - ly1) * dy) / len_sq
    return (lx1 + t * dx, ly1 + t * dy)


def translate_segment(
    seg: Segment,
    dx: float,
    dy: float,
) -> Segment:
    """Return a copy of *seg* translated by (dx, dy).

    Args:
        seg: The segment to translate.
        dx: Translation in x.
        dy: Translation in y.

    Returns:
        A new Segment with translated endpoints.
    """
    return Segment(
        x1=seg.x1 + dx,
        y1=seg.y1 + dy,
        x2=seg.x2 + dx,
        y2=seg.y2 + dy,
        width=seg.width,
        layer=seg.layer,
        net=seg.net,
        net_name=seg.net_name,
    )


def count_corners(segments: list[Segment], tolerance: float = 1e-4) -> int:
    """Count number of corners (direction changes) in a segment list."""
    if len(segments) < 2:
        return 0

    corners = 0
    for i in range(len(segments) - 1):
        if not same_direction(segments[i], segments[i + 1], tolerance):
            corners += 1
    return corners


def total_length(segments: list[Segment]) -> float:
    """Calculate total length of segments."""
    total = 0.0
    for seg in segments:
        total += segment_length(seg)
    return total


# ---------------------------------------------------------------------------
# Pad mask-opening zones (issue #9524)
#
# A pad's solder-mask opening exposes every conductor inside it, not just the
# pad: a same-net trace that crosses the opening without terminating there
# measures as ``same-net pass-through``, and foreign-net copper in the opening
# measures as ``different-net`` -- both are ``mask_to_copper`` findings.  The
# copper cleanup (``optimize-traces``, ``pcb prune``) can manufacture exactly
# that: it merges or re-bends copper across a same-net pad centre and places
# new bends inside the opening, undoing a recipe's mask-clean pad escapes.
#
# The zone below is the opening approximated conservatively: the pad copper's
# board-space AABB grown by the pad's effective mask expansion.  The grown
# AABB contains the real (rounded) opening, so a veto here is slightly
# conservative near pad corners and never misses the opening it guards.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PadMaskZone:
    """A pad's mask-opening keep-out zone (issue #9524).

    Attributes:
        x, y: Pad centre (board coordinates of the caller's segment space).
        half_w, half_h: Board-space half-extents of the pad copper's AABB
            (the ``pad_half_extents`` rotation formula, applied to the
            schema pad's absolute rotation).
        margin: Effective solder-mask expansion in mm (per-pad override,
            else footprint, else board ``pad_to_mask_clearance``, else 0).
            May be negative (opening smaller than copper).
        layers: Copper layer names the pad lands on (``"*.Cu"`` for
            through-hole, specific names for SMD).  A zone only guards
            segments on these layers -- B.Cu copper is not exposed by an
            F.Mask opening.
        net_name: The pad's net (empty-net pads have no opening worth
            guarding here).
    """

    x: float
    y: float
    half_w: float
    half_h: float
    margin: float
    layers: frozenset[str]
    net_name: str

    def covers_layer(self, layer: str) -> bool:
        """True when a segment on *layer* is exposed by this opening."""
        return "*.Cu" in self.layers or layer in self.layers

    def contains_point(self, x: float, y: float, *, copper: bool = False) -> bool:
        """Is *x, y* inside the opening (or the pad copper when ``copper``)?"""
        hw = self.half_w if copper else self.half_w + self.margin
        hh = self.half_h if copper else self.half_h + self.margin
        return abs(x - self.x) <= hw and abs(y - self.y) <= hh

    def segment_intersects(self, x1: float, y1: float, x2: float, y2: float) -> bool:
        """Does the segment cross the opening's (grown) AABB?

        Parametric slab clip (Liang-Barsky); exact for a segment against an
        axis-aligned box.
        """
        hw = self.half_w + self.margin
        hh = self.half_h + self.margin
        dx, dy = x2 - x1, y2 - y1
        t0, t1 = 0.0, 1.0
        for p, q in (
            (-dx, x1 - self.x + hw),
            (dx, self.x + hw - x1),
            (-dy, y1 - self.y + hh),
            (dy, self.y + hh - y1),
        ):
            if p == 0.0:
                if q < 0.0:
                    return False
            else:
                r = q / p
                if p < 0.0:
                    if r > t1:
                        return False
                    if r > t0:
                        t0 = r
                else:
                    if r < t0:
                        return False
                    if r < t1:
                        t1 = r
        return True


def _pad_aabb_half_extents(pad: Any) -> tuple[float, float]:
    """Board-space AABB half-extents of a schema pad's copper."""
    half_w, half_h = pad.size[0] / 2.0, pad.size[1] / 2.0
    rotation = pad.rotation or 0.0
    if rotation == 0.0:
        return half_w, half_h
    cos_t, sin_t = abs(math.cos(math.radians(rotation))), abs(math.sin(math.radians(rotation)))
    return cos_t * half_w + sin_t * half_h, sin_t * half_w + cos_t * half_h


def pad_mask_zones_from_pcb(pcb: PCB) -> list[PadMaskZone]:
    """Build one :class:`PadMaskZone` per netted pad of a schema ``PCB``.

    Positions come out in the schema board-relative space (``fp.position`` +
    the rotated pad offset, no ``_board_origin`` correction).  Prune's model
    shares that space; callers that compare zones against raw file
    coordinates (``optimize-traces`` parses segment text directly) must add
    ``pcb._board_origin`` back.  The effective mask expansion is resolved the
    same way :mod:`kicad_tools.validate.mask_geometry` resolves it: per-pad
    ``solder_mask_margin``, else the footprint's, else the board's
    ``pad_to_mask_clearance``, else 0.
    """
    from ...core.geometry import rotate_pad_offset

    board_margin = 0.0
    sexp = getattr(pcb, "_sexp", None)
    setup = sexp.find_child("setup") if sexp is not None else None
    margin_node = setup.find_child("pad_to_mask_clearance") if setup is not None else None
    if margin_node is not None:
        value = margin_node.get_float(0)
        if value is not None:
            board_margin = value

    zones: list[PadMaskZone] = []
    for fp in pcb.footprints:
        fp_margin: float | None = None
        fp_node = getattr(fp, "_sexp_node", None)
        if fp_node is not None:
            node = fp_node.find_child("solder_mask_margin")
            if node is not None:
                value = node.get_float(0)
                if value is not None:
                    fp_margin = value
        for pad in fp.pads:
            net_name = pad.net_name or ""
            if not net_name:
                continue  # net-less pads (NPTH) get no opening from this guard
            layers = frozenset(layer for layer in pad.layers if layer.endswith(".Cu"))
            if not layers:
                continue
            dx, dy = rotate_pad_offset(pad.position[0], pad.position[1], fp.rotation)
            margin = pad.solder_mask_margin
            if margin is None:
                margin = fp_margin if fp_margin is not None else board_margin
            half_w, half_h = _pad_aabb_half_extents(pad)
            zones.append(
                PadMaskZone(
                    x=fp.position[0] + dx,
                    y=fp.position[1] + dy,
                    half_w=half_w,
                    half_h=half_h,
                    margin=margin,
                    layers=layers,
                    net_name=net_name,
                )
            )
    return zones


def _layer_token(layer: Any) -> str:
    """KiCad layer name of a router segment's ``layer``.

    Router segments carry :class:`~kicad_tools.core.types.CopperLayer` (whose
    ``kicad_name`` is the KiCad name) or the string-valued
    :class:`~kicad_tools.core.types.Layer`; zone checks compare against KiCad
    names either way.
    """
    kicad_name = getattr(layer, "kicad_name", None)
    if isinstance(kicad_name, str):
        return kicad_name
    value = getattr(layer, "value", layer)
    return value if isinstance(value, str) else str(value)


def _endpoints(segments: Sequence[Segment]) -> list[tuple[float, float]]:
    return [(s.x1, s.y1) for s in segments] + [(s.x2, s.y2) for s in segments]


def _same_geometry(a: Segment, b: Segment, tolerance: float) -> bool:
    """True when *a* and *b* span the same endpoints (either orientation)."""
    return (
        abs(a.x1 - b.x1) <= tolerance
        and abs(a.y1 - b.y1) <= tolerance
        and abs(a.x2 - b.x2) <= tolerance
        and abs(a.y2 - b.y2) <= tolerance
    ) or (
        abs(a.x1 - b.x2) <= tolerance
        and abs(a.y1 - b.y2) <= tolerance
        and abs(a.x2 - b.x1) <= tolerance
        and abs(a.y2 - b.y1) <= tolerance
    )


def _point_segment_distance(
    px: float, py: float, x1: float, y1: float, x2: float, y2: float
) -> float:
    """Distance from *px, py* to the segment *x1,y1 - x2,y2*."""
    dx, dy = x2 - x1, y2 - y1
    length_sq = dx * dx + dy * dy
    if length_sq <= 0.0:
        return math.hypot(px - x1, py - y1)
    t = ((px - x1) * dx + (py - y1) * dy) / length_sq
    t = max(0.0, min(1.0, t))
    return math.hypot(px - (x1 + t * dx), py - (y1 + t * dy))


def _terminates_on_pad(seg: Segment, zone: PadMaskZone, tolerance: float) -> bool:
    """Does *seg* terminate on *zone*'s pad?

    The pad-entry shape a mask check grants its own-pad allowance to: an
    authored end inside the pad copper, with the segment's body running into
    the pad centre (within half a trace width) -- never a pass-through that
    merely clips the copper, and never a bend parked on the pad land.
    """
    end_inside = zone.contains_point(seg.x1, seg.y1, copper=True) or zone.contains_point(
        seg.x2, seg.y2, copper=True
    )
    if not end_inside:
        return False
    reach = seg.width / 2.0 + tolerance
    return _point_segment_distance(zone.x, zone.y, seg.x1, seg.y1, seg.x2, seg.y2) <= reach


def rewrite_keeps_pad_escapes(
    old: Sequence[Segment],
    new: Sequence[Segment],
    zones: Sequence[PadMaskZone],
    tolerance: float,
) -> bool:
    """Would the chain rewrite *old* -> *new* break a pad's mask-clean escape?

    Two rules, both raised by the cleanup chain on real boards (issue #9524):

    * **Endpoint preservation** -- every *old* endpoint inside a same-net
      opening must still map to a terminating end inside that pad's copper.
      Dropping it is the merge/straighten across a pad centre: the copper
      still runs over the pad, but no segment terminates there any more, so
      the escape measures as ``same-net pass-through``.
    * **No new copper in an opening** -- a *new* segment (one *old* does not
      already carry) may not enter a *foreign* pad's opening at all, and may
      not enter a *same-net* opening unless it terminates on the pad (see
      :func:`_terminates_on_pad`) or crosses only the opening's skirt: every
      endpoint outside the opening and the copper missing the pad centre.
      The two exempt-failure shapes are the cleanup's own defect classes
      (#9524) -- a new bend parked inside the opening, and copper merged or
      straightened across a same-net pad centre -- while a same-net crossing
      that clips only the opening's corner (the optimizer's corner chamfer
      landing on a sibling branch's route) is pre-existing behaviour the
      pinned suite requires the cleanup to keep allowing.

    A new bend inside an opening is always part of a new segment carrying an
    endpoint there, so the second rule covers the "new bend in the pad zone"
    case.  Copper *old* already carries is exempt throughout: the guard undoes
    regressions the cleanup would introduce, it does not repair pre-existing
    escapes.

    Args:
        old: The chain before the pass.
        new: The chain after the pass.
        zones: All pads' :class:`PadMaskZone` records.
        tolerance: Coordinate tolerance (mm), the optimizer's own.

    Returns:
        True when the rewrite may be kept.
    """
    if not zones or not old or old == new:
        return True
    layer = _layer_token(old[0].layer)
    net_name = old[0].net_name
    layer_zones = [z for z in zones if z.covers_layer(layer)]
    if not layer_zones:
        return True

    old_ends = _endpoints(old)
    new_ends = _endpoints(new)

    for px, py in old_ends:
        for zone in layer_zones:
            if zone.net_name != net_name or not zone.contains_point(px, py):
                continue
            # The terminating end may have been straightened along the pad
            # (issue #8325) but must remain inside the pad copper.
            if not any(zone.contains_point(nx, ny, copper=True) for nx, ny in new_ends):
                return False

    for seg in new:
        if any(_same_geometry(seg, prior, tolerance) for prior in old):
            continue  # copper the board already carries
        for zone in layer_zones:
            if not zone.segment_intersects(seg.x1, seg.y1, seg.x2, seg.y2):
                continue
            if zone.net_name != net_name:
                return False  # foreign opening: any new copper in it is an exposure
            if _terminates_on_pad(seg, zone, tolerance):
                continue
            if (
                not any(
                    zone.contains_point(ex, ey) for ex, ey in ((seg.x1, seg.y1), (seg.x2, seg.y2))
                )
                and _point_segment_distance(zone.x, zone.y, seg.x1, seg.y1, seg.x2, seg.y2)
                > seg.width / 2.0 + tolerance
            ):
                # A same-net crossing whose every endpoint stays outside the
                # opening and whose copper misses the pad centre (the
                # optimizer's corner chamfer clipping a sibling branch's
                # opening) is pre-existing behaviour, not a new escape: only
                # a new bend inside the opening or copper straightened
                # across the pad centre (AC1/#9524) is the cleanup's defect.
                continue
            return False
    return True


def snap_preserves_pad_zones(
    zones: Sequence[PadMaskZone],
    moves: Sequence[tuple[float, float, float, float, float, float, float, float, str, str]],
    target: tuple[float, float],
) -> bool:
    """Would the planned endpoint snaps swing copper into a pad opening?

    A snap re-aims a segment onto an anchor centre; the re-aimed body may
    sweep across a pad's mask opening that the old geometry avoided.  The
    snap's *target* pad is exempt: ending on a same-net pad centre is the
    terminating entry every escape wants.  Everything else -- crossing a
    same-net pad's opening without terminating, or any foreign-net opening --
    vetoes the snap.

    Args:
        zones: All pads' :class:`PadMaskZone` records.
        moves: ``(x1, y1, x2, y2, new_x1, new_y1, new_x2, new_y2, layer,
            net_name)`` per moved segment.
        target: The snap destination (the anchor centre ends land on).

    Returns:
        True when every move may be snapped.
    """
    if not zones:
        return True
    for (
        x1,
        y1,
        x2,
        y2,
        nx1,
        ny1,
        nx2,
        ny2,
        layer,
        net_name,
    ) in moves:
        for zone in zones:
            if not zone.covers_layer(layer):
                continue
            if zone.net_name == net_name and zone.contains_point(target[0], target[1]):
                continue  # the snap terminates on this pad
            if zone.segment_intersects(x1, y1, x2, y2):
                continue  # the opening was already crossed
            if zone.segment_intersects(nx1, ny1, nx2, ny2):
                return False
    return True
