"""Circumcircle and swept-direction geometry shared by start/mid/end arcs.

KiCad stores a modern arc as three points *on* it -- ``start``, ``mid`` and
``end`` -- with no explicit centre, radius or direction. Recovering those is
the same short algorithm everywhere it is needed: circumcentre of three
points, then the signed sweep, classified by where ``mid`` falls between
``start`` and ``end`` (``mid`` is the only thing that distinguishes an arc
from its complement between the same endpoints).

``arc_sweep`` is that algorithm, once. It deliberately does **not** decide
what to do about a degenerate (collinear) arc: that policy differs per
consumer and belongs to the consumer.

* :mod:`kicad_tools.core.board_outline` refuses -- a collinear ``gr_arc`` on
  Edge.Cuts is a malformed board outline it will not guess at.
* :mod:`kicad_tools.router.fixed_copper` degrades -- a collinear "arc" is a
  straight segment, which its three authored points already bound, so it
  falls back to them rather than refusing an otherwise routable board
  (issue #5863).

So ``arc_sweep`` returns ``None`` for "no circle here" and lets each caller
raise, fall back, or skip as its own contract requires.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

Point = tuple[float, float]

#: Below this, the three points' circumcircle determinant is treated as zero:
#: they are collinear and no circle through them is recoverable. The value is
#: an absolute threshold on ``2 * cross(mid - start, end - start)``, i.e. four
#: times the triangle's area, computed in ``start``-relative coordinates.
COLLINEAR_DETERMINANT = 1e-12

#: Slack, in radians, when asking whether a sweep reaches a given angle. An
#: arc that ends exactly on an axis extreme must count as reaching it: both
#: consumers build *bounding* geometry, where including a point the sweep only
#: touches is harmless and excluding one it does reach is not.
SWEEP_TOLERANCE = 1e-12

#: The four angles at which a circle attains an axis extreme (+x, +y, -x, -y).
QUADRANT_ANGLES: tuple[float, float, float, float] = (
    0.0,
    math.pi / 2.0,
    math.pi,
    3.0 * math.pi / 2.0,
)


@dataclass(frozen=True)
class ArcSweep:
    """One arc's circle plus how far around it the arc actually travels.

    ``sweep`` is a magnitude in ``(0, tau]``; ``counter_clockwise`` carries its
    sign. Together with ``start_angle`` they describe the travelled interval,
    which is what decides whether the arc bulges past an axis extreme of its
    circle -- the part no set of points on the arc can tell you by itself.
    """

    center: Point
    radius: float
    start_angle: float
    sweep: float
    counter_clockwise: bool

    def point_at(self, angle: float) -> Point:
        """The point on this arc's circle at absolute ``angle``."""
        return (
            self.center[0] + self.radius * math.cos(angle),
            self.center[1] + self.radius * math.sin(angle),
        )

    def reaches(self, angle: float, *, tolerance: float = SWEEP_TOLERANCE) -> bool:
        """Whether travelling from ``start_angle`` along this arc hits ``angle``."""
        signed = angle - self.start_angle if self.counter_clockwise else self.start_angle - angle
        return signed % math.tau <= self.sweep + tolerance

    def axis_extreme_points(self, *, tolerance: float = SWEEP_TOLERANCE) -> list[Point]:
        """The axis extremes of the circle that this arc actually reaches.

        An arc bulges outside the hull of any points taken *on* it, so its
        bounding box needs these; but only the ones the swept interval covers,
        or the box inflates to the whole circle's and over-blocks.
        """
        return [
            self.point_at(angle)
            for angle in QUADRANT_ANGLES
            if self.reaches(angle, tolerance=tolerance)
        ]


def arc_sweep(start: Point, mid: Point, end: Point) -> ArcSweep | None:
    """The circle through three arc points and the sweep ``mid`` selects.

    Returns ``None`` -- never raises -- when the points are collinear (or their
    circle is otherwise unrepresentable), leaving the degeneracy policy to the
    caller. See this module's docstring for why that split exists.
    """
    # Translate to `start` first to avoid cancellation for geometry far from
    # the sheet origin: the determinant below is a difference of products of
    # coordinates, and board coordinates are large compared to arc radii.
    bx, by = mid[0] - start[0], mid[1] - start[1]
    cx, cy = end[0] - start[0], end[1] - start[1]
    det = 2.0 * (bx * cy - by * cx)
    if math.isnan(det) or abs(det) < COLLINEAR_DETERMINANT:
        return None
    b2, c2 = bx * bx + by * by, cx * cx + cy * cy
    center = (
        start[0] + (cy * b2 - by * c2) / det,
        start[1] + (bx * c2 - cx * b2) / det,
    )
    radius = math.hypot(start[0] - center[0], start[1] - center[1])
    if not math.isfinite(radius):
        return None

    start_angle = math.atan2(start[1] - center[1], start[0] - center[0])
    mid_angle = math.atan2(mid[1] - center[1], mid[0] - center[0])
    end_angle = math.atan2(end[1] - center[1], end[0] - center[0])
    to_mid = (mid_angle - start_angle) % math.tau
    to_end = (end_angle - start_angle) % math.tau
    # KiCad stores the arc through its midpoint, so the travelled direction is
    # whichever one reaches `mid` before `end`.
    counter_clockwise = to_mid <= to_end
    if to_end == 0.0:
        # `start` and `end` land on the same polar angle while still spanning a
        # real triangle: the arc closes the circle. Report the full turn, which
        # is the conservative reading for every bounding consumer.
        return ArcSweep(center, radius, start_angle, math.tau, True)
    # Each direction's magnitude comes from its own angular difference rather
    # than as `tau - other`, so neither direction inherits the other's rounding.
    sweep = to_end if counter_clockwise else (start_angle - end_angle) % math.tau
    return ArcSweep(center, radius, start_angle, sweep, counter_clockwise)
