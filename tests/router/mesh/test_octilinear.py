"""Clearance-aware 45-fit regression tests, modeled on the #3906 short.

The load-bearing invariant: the octilinear best-fit must NEVER emit a leg that
bulges into a keep-out.  When the default dogleg orientation collides it must
try the other orientation (and subdivide) rather than shipping the colliding
leg -- the exact obstacle-consult discipline generalised from
``subgrid.py:1004-1030``.
"""

from __future__ import annotations

from kicad_tools.router.mesh.geometry import segments_intersect
from kicad_tools.router.mesh.obstacles import ObstacleModel
from kicad_tools.router.mesh.octilinear import octilinear_fit

# Keep-out that the DEFAULT (diagonal-first) dogleg of the chord (0,0)->(10,4)
# bulges into, but the FLIPPED (axis-first) dogleg avoids.
_KEEPOUT = (2.0, 2.0, 3.0, 5.0)


def _segment_enters_rect(
    p1: tuple[float, float], p2: tuple[float, float], rect: tuple[float, float, float, float]
) -> bool:
    """True if segment ``p1-p2`` intersects (or lies within) AABB ``rect``.

    Local test-only helper: standalone assertion of the anti-#3906 invariant
    below, independent of whatever clearance predicate ``ObstacleModel`` uses
    internally (formerly ``geometry.segment_intersects_rect``, retired as
    production-dead by #5685 once Epic #5509 Phase 3e moved ``is_clear`` onto
    the shared clearance kernel).
    """
    xmin, ymin, xmax, ymax = rect
    if xmin <= p1[0] <= xmax and ymin <= p1[1] <= ymax:
        return True
    if xmin <= p2[0] <= xmax and ymin <= p2[1] <= ymax:
        return True
    corners = [(xmin, ymin), (xmax, ymin), (xmax, ymax), (xmin, ymax)]
    return any(segments_intersect(p1, p2, corners[i], corners[(i + 1) % 4]) for i in range(4))


def _no_leg_enters(path: list[tuple[float, float]], rect) -> bool:
    return not any(_segment_enters_rect(path[i], path[i + 1], rect) for i in range(len(path) - 1))


def test_default_dogleg_into_keepout_is_rejected_and_flipped() -> None:
    """The bulging default dogleg is rejected; the clear orientation is used."""
    obstacles = ObstacleModel(outline=[], keepouts=[_KEEPOUT])
    chord = [(0.0, 0.0), (10.0, 4.0)]

    result = octilinear_fit(chord, obstacles.is_clear)

    assert result is not None
    # Anti-#3906 invariant: no emitted leg enters the keep-out.
    assert _no_leg_enters(result, _KEEPOUT)
    # It should have chosen the axis-first orientation whose bend is at (6, 0).
    assert (6.0, 0.0) in result


def test_every_leg_is_45_legal() -> None:
    """Every emitted leg lies on the {0,45,90,135} angle set."""
    from kicad_tools.router.quantize import is_45_aligned

    obstacles = ObstacleModel(outline=[], keepouts=[_KEEPOUT])
    result = octilinear_fit([(0.0, 0.0), (10.0, 4.0)], obstacles.is_clear)
    assert result is not None
    for i in range(len(result) - 1):
        dx = result[i + 1][0] - result[i][0]
        dy = result[i + 1][1] - result[i][1]
        assert is_45_aligned(dx, dy), f"leg {i} off-angle: {dx},{dy}"


def test_both_orientations_blocked_subdivides_clear() -> None:
    """When both doglegs clip a small keep-out, subdivision threads a clear path."""
    # A small keep-out sitting on the geodesic midpoint; both full-chord
    # doglegs bulge across it, but a subdivided staircase hugs the diagonal.
    small = (4.6, 1.6, 5.4, 2.4)
    obstacles = ObstacleModel(outline=[], keepouts=[small])
    result = octilinear_fit([(0.0, 0.0), (10.0, 4.0)], obstacles.is_clear)
    assert result is not None
    assert _no_leg_enters(result, small)


def test_fully_walled_chord_fails_rather_than_shorting() -> None:
    """A keep-out wall across the whole span yields None, not a colliding leg."""
    wall = (1.0, -5.0, 9.0, 5.0)  # spans the full y-band between the endpoints
    obstacles = ObstacleModel(outline=[], keepouts=[wall])
    result = octilinear_fit([(0.0, 0.0), (10.0, 4.0)], obstacles.is_clear)
    assert result is None
