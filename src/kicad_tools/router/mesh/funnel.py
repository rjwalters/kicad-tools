"""Simple Stupid Funnel (Mononen string-pull) for the mesh router (#4268).

Given a corridor of *portals* (the shared triangle edges along a navmesh A*
path, each oriented ``(left, right)`` relative to travel direction), the
funnel algorithm returns the Euclidean taut geodesic through the corridor --
the shortest path a point agent can take.  This is the ~40-line component the
P0.5 spike wrote and unit-tested (straight channel collapses to two points;
L-channel pulls taut around the inner reflex corner); those two tests ship
alongside in ``tests/router/mesh/test_funnel.py``.

The funnel produces an *arbitrary-angle* geodesic; converting it to 45-legal
copper is a separate downstream stage (``octilinear.py``), per the ADR's
Option-1 resolution of the octilinear-geometry fork.
"""

from __future__ import annotations

from .geometry import Pt, point_equal

Portal = tuple[Pt, Pt]


def _area(a: Pt, b: Pt, c: Pt) -> float:
    """Mononen's ``triarea2`` sign convention (used verbatim below).

    This is the negation of :func:`geometry.tri_area2`; the funnel's tighten /
    cross comparisons are transcribed directly from Mononen's reference
    implementation, so they must use his sign.
    """
    ax = b[0] - a[0]
    ay = b[1] - a[1]
    bx = c[0] - a[0]
    by = c[1] - a[1]
    return bx * ay - ax * by


def string_pull(portals: list[Portal]) -> list[Pt]:
    """Return the taut geodesic through an oriented portal corridor.

    ``portals`` must start with a degenerate portal ``(start, start)`` and end
    with ``(goal, goal)``; each intermediate portal is ``(left, right)`` with
    ``left`` on the left-hand side of travel.  The returned polyline begins at
    ``start`` and ends at ``goal``.
    """
    if not portals:
        return []

    path: list[Pt] = []

    portal_apex = portals[0][0]
    portal_left = portals[0][0]
    portal_right = portals[0][1]
    apex_index = 0
    left_index = 0
    right_index = 0

    path.append(portal_apex)

    i = 1
    while i < len(portals):
        left, right = portals[i]

        # --- tighten / cross the RIGHT side -------------------------------
        if _area(portal_apex, portal_right, right) <= 0.0:
            if point_equal(portal_apex, portal_right) or (
                _area(portal_apex, portal_left, right) > 0.0
            ):
                # Tighten the funnel on the right.
                portal_right = right
                right_index = i
            else:
                # Right crossed over left: left becomes the new apex.
                if not path or not point_equal(path[-1], portal_left):
                    path.append(portal_left)
                portal_apex = portal_left
                apex_index = left_index
                # Restart scan from the new apex.
                portal_left = portal_apex
                portal_right = portal_apex
                left_index = apex_index
                right_index = apex_index
                i = apex_index + 1
                continue

        # --- tighten / cross the LEFT side --------------------------------
        if _area(portal_apex, portal_left, left) >= 0.0:
            if point_equal(portal_apex, portal_left) or (
                _area(portal_apex, portal_right, left) < 0.0
            ):
                # Tighten the funnel on the left.
                portal_left = left
                left_index = i
            else:
                # Left crossed over right: right becomes the new apex.
                if not path or not point_equal(path[-1], portal_right):
                    path.append(portal_right)
                portal_apex = portal_right
                apex_index = right_index
                portal_left = portal_apex
                portal_right = portal_apex
                left_index = apex_index
                right_index = apex_index
                i = apex_index + 1
                continue

        i += 1

    # Append the goal apex.
    goal = portals[-1][0]
    if not path or not point_equal(path[-1], goal):
        path.append(goal)

    return path
